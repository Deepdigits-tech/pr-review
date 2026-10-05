#!/usr/bin/env python3
"""Publish a fix draft: push, reply on threads, resolve own threads, re-request review. Prints JSON."""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fixdraft  # noqa: E402
from fixcopy import main_checkout  # noqa: E402
from prlib import ReviewError, account_for_owner, gh_api, is_own_account, run_main  # noqa: E402
from pushlib import git as _git, head_decision, push, push_failure, uncommitted  # noqa: E402,F401

RESOLVE = "mutation($id: ID!) { resolveReviewThread(input: {threadId: $id}) { thread { isResolved } } }"


def _requestable(login: str | None, pr_author: str) -> bool:
    return bool(login) and not is_own_account(login) and login != pr_author and not login.endswith("[bot]")


def reviewers_to_request(reviews: list[dict], pr_author: str) -> list[str]:
    out: list[str] = []
    for r in reviews:
        login = (r.get("user") or {}).get("login")
        if _requestable(login, pr_author) and login not in out:
            out.append(login)
    return out


def _summary_comment(items: list[dict]) -> str | None:
    by_reviewer: dict[str, list[dict]] = {}
    for i in items:
        if i["kind"] == "summary" and not i.get("skip_reply"):
            by_reviewer.setdefault(i["source"], []).append(i)
    if not by_reviewer:
        return None
    parts = []
    for reviewer, its in by_reviewer.items():
        parts.append(f"**Re: @{reviewer}'s review**\n\n" + "\n".join(f"- {i['reply']}" for i in its))
    return "\n\n".join(parts)


def _save(path: Path, d: dict) -> None:
    path.write_text(json.dumps(d, indent=2))


def _ensure_publishable_state(d: dict, live: dict, owner_repo: str) -> None:
    pr = d["pr"]
    login = (live.get("user") or {}).get("login")
    if not is_own_account(login):
        raise ReviewError(f"PR #{pr['number']} was opened by {login}, not one of your accounts. "
                          "Nothing was pushed or posted.")
    head_repo = ((live.get("head") or {}).get("repo") or {}).get("full_name", "")
    if head_repo.lower() != owner_repo.lower():
        raise ReviewError(f"PR #{pr['number']} comes from another repository (a fork); pushing to "
                          f"{owner_repo} would not update it. Nothing was pushed or posted.")


def _commit_problems(copy: str, d: dict) -> list[str]:
    shas = [s.strip().lower() for s in _git(copy, "rev-list", f"{d['pr']['base_sha']}..HEAD").splitlines()]
    out = []
    for i in d["items"]:
        if i["status"] != "fixed":
            continue
        c = str(i["commit"]).strip().lower()
        if not c or not any(s.startswith(c) for s in shas):
            out.append(f"item {i['id']} says it is fixed in {i['commit']}, which is not one of the fix commits.")
    return out


def publish(path: str, dry_run: bool, reviewers: list[str] | None) -> dict:
    p = Path(path)
    d = json.loads(p.read_text())
    fixdraft.validate(d)
    undecided = [str(i["id"]) for i in d["items"] if i["status"] == "you_decide"]
    if undecided:
        raise ReviewError(f"Item(s) {', '.join(undecided)} still need your decision (you_decide). "
                          "Decide them first; nothing was posted.")
    pub = d.setdefault("published", json.loads(json.dumps(fixdraft.EMPTY_PUBLISHED)))
    pr = d["pr"]
    account = account_for_owner(pr["owner"])
    base = f"repos/{pr['owner']}/{pr['repo']}"
    live = gh_api(account, f"{base}/pulls/{pr['number']}")
    _ensure_publishable_state(d, live, f"{pr['owner']}/{pr['repo']}")
    if live["state"] != "open":
        raise ReviewError(f"PR #{pr['number']} is no longer open. Nothing was posted.")
    copy = d["copy_path"]
    try:
        local = _git(copy, "rev-parse", "HEAD").strip()
    except ReviewError:
        raise ReviewError(f"The fix copy at {copy} cannot be read (already published and removed, or deleted?). "
                          "Nothing was pushed or posted.") from None
    dirty = uncommitted(copy)
    if dirty and not dry_run:
        raise ReviewError("The fix copy has uncommitted changes: " + "; ".join(l.strip() for l in dirty) +
                          ". Commit or discard them, then re-run. Nothing was pushed or posted.")
    problems = _commit_problems(copy, d)
    if problems and not dry_run:
        raise ReviewError(" ".join(problems) + " Nothing was pushed or posted.")
    live_sha = live["head"]["sha"]
    need_push = head_decision(local, live_sha, pr["base_sha"], pub["pushed"], f"PR #{pr['number']}") == "push"
    replies = [i for i in d["items"] if i["kind"] == "thread" and not i.get("skip_reply")
               and str(i["comment_id"]) not in pub["replies"]]
    summary = None if pub["summary_comment"] else _summary_comment(d["items"])
    to_resolve = [i for i in d["items"] if i["kind"] == "thread" and i.get("own") and not i.get("skip_reply")
                  and is_own_account(i["source"]) and i["thread_id"] not in pub["resolved"]]
    author = live["user"]["login"]
    if reviewers is not None:
        want = [r for r in reviewers if _requestable(r, author)]
    else:
        want = reviewers_to_request(gh_api(account, f"{base}/pulls/{pr['number']}/reviews", slurp=True), author)
    want = [r for r in want if r not in pub["rerequested"]]

    if dry_run:
        commits = _git(copy, "log", "--oneline", f"{pr['base_sha']}..HEAD").splitlines()
        if not problems and not dirty:
            d["dry_run"] = {"at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"), "head": local}
            _save(p, d)
        return {"dry_run": True,
                "push": {"from": local, "to": pr["head"], "commits": commits} if need_push else None,
                "uncommitted": dirty, "commit_problems": problems,
                "replies": [{"id": i["id"], "comment_id": i["comment_id"], "body": i["reply"]} for i in replies],
                "summary_comment": summary, "resolve": [i["thread_id"] for i in to_resolve],
                "rerequest": want, "no_reviewers": not want and not pub["rerequested"]}

    if (d.get("dry_run") or {}).get("head") != local:
        raise ReviewError("Run publish.py --dry-run first (and again after any change to the fixes). "
                          "Nothing was pushed or posted.")
    if need_push:
        push(copy, pr["owner"], pr["repo"], pr["head"])
    pub["pushed"] = local
    _save(p, d)
    posted = 0
    for i in replies:
        res = gh_api(account, f"{base}/pulls/{pr['number']}/comments/{i['comment_id']}/replies",
                     method="POST", body={"body": i["reply"]})
        pub["replies"][str(i["comment_id"])] = res["html_url"]
        posted += 1
        _save(p, d)
    if summary:
        res = gh_api(account, f"{base}/issues/{pr['number']}/comments", method="POST", body={"body": summary})
        pub["summary_comment"] = res["html_url"]
        _save(p, d)
    for i in to_resolve:
        gh_api(account, "graphql", method="POST", body={"query": RESOLVE, "variables": {"id": i["thread_id"]}})
        pub["resolved"].append(i["thread_id"])
        _save(p, d)
    if want:
        gh_api(account, f"{base}/pulls/{pr['number']}/requested_reviewers", method="POST", body={"reviewers": want})
        pub["rerequested"] += want
        _save(p, d)
    # Everything is on GitHub now; a copy that can't be removed (already gone, say) is not a failure.
    try:
        _git(str(main_checkout(Path(copy))), "worktree", "remove", "--force", copy)
        removed = True
    except ReviewError:
        removed = False
    pub["done"] = True
    _save(p, d)
    fixdraft.render(str(p))
    return {"pushed": pub["pushed"], "replies_posted": posted, "summary_comment": pub["summary_comment"],
            "resolved": len(to_resolve), "rerequested": want, "no_reviewers": not want and not pub["rerequested"],
            "copy_removed": removed}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("fix")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--reviewers", default=None, help="comma-separated GitHub logins")
    a = ap.parse_args()
    who = [r.strip() for r in a.reviewers.split(",") if r.strip()] if a.reviewers else None
    run_main(lambda: publish(a.fix, a.dry_run, who))


if __name__ == "__main__":
    main()
