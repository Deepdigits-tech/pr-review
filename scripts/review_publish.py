#!/usr/bin/env python3
"""Publish a review of a teammate's PR: push fixes (allow-listed authors only), file issues, post notes, resolve, verdict."""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import draft as draft_mod  # noqa: E402
from fixcopy import main_checkout  # noqa: E402
from post_review import _comment_body, build_payload  # noqa: E402
from prlib import (ReviewError, account_for_owner, gh_api, is_own_account, may_fix_author,  # noqa: E402
                   parse_patch_lines, run_main)
from pushlib import git, head_decision, push, uncommitted  # noqa: E402
from pushlib import git as fix_git  # noqa: E402  (removing the copy; a separate name keeps it fakeable)

THREADS = """query($owner: String!, $repo: String!, $number: Int!, $after: String) {
  repository(owner: $owner, name: $repo) { pullRequest(number: $number) {
    reviewThreads(first: 100, after: $after) { pageInfo { hasNextPage endCursor }
      nodes { id isResolved comments(first: 1) { nodes { databaseId author { login } } } } } } } }"""
RESOLVE = "mutation($id: ID!) { resolveReviewThread(input: {threadId: $id}) { thread { isResolved } } }"
_sleep = time.sleep  # a name tests can replace
EMPTY_FLOW = {"pushed": None, "issues": {}, "notes_review": None, "verdict_review": None, "resolved": [], "done": False}


def _act(n: dict) -> str:
    return n.get("action", "leave")


def note_body(n: dict, verdict: str, issue_number: int | None) -> str:
    body = _comment_body(n)
    if _act(n) == "fix":
        return f"{body}\n\n**Fixed in `{n['fixed_in'][:7]}`:** {n['fix_summary']}"
    if verdict == "APPROVE" and n["severity"] == "should-fix" and issue_number:
        return f"{body}\n\n**Tracked in #{issue_number}** (not blocking)."
    if verdict == "APPROVE" and n["severity"] == "nit":
        return f"{body}\n\n_Optional, not blocking._"
    return body


def issue_payload(n: dict, pr_number: int, pr_url: str) -> dict:
    first_line = (n["body"].strip().splitlines() or [""])[0]
    title = n.get("title") or first_line[:80]
    return {"title": title, "body": f"Follow-up to #{pr_number}: {pr_url} (`{n['path']}:{n['line']}`)\n\n{n['body']}"}


def notes_to_resolve(draft: dict, verdict: str) -> list[int]:
    kept = [n for n in draft["notes"] if n["keep"]]
    if verdict == "APPROVE":
        return [n["id"] for n in kept]
    return [n["id"] for n in kept if _act(n) == "fix"]


def _compare_files(account: str, base: str, base_ref: str, head_sha: str) -> list[dict]:
    """The compare endpoint returns every changed file (GitHub caps it at 300) on its one page when no paging
    parameters are given, so it is fetched once."""
    res = gh_api(account, f"{base}/compare/{base_ref}...{head_sha}")
    return res.get("files") or []


def _plan_fingerprint(d: dict, verdict: str) -> str:
    """Everything the dry run showed the owner; the real run refuses if any of it changed since."""
    notes = [[n["id"], _act(n), n["severity"], n["body"], n.get("title"), n.get("fixed_in"), n.get("fix_summary"),
              n["path"], n["line"], n.get("side", "RIGHT")] for n in d["notes"] if n["keep"]]
    text = json.dumps({"verdict": verdict, "summary": d["summary"], "notes": notes}, sort_keys=True)
    return hashlib.sha256(text.encode()).hexdigest()


def _save(p: Path, d: dict) -> None:
    """Write the draft atomically: a crash mid-write must not leave half a file (it holds the resume state)."""
    text = json.dumps(d, indent=2)
    fd, tmp = tempfile.mkstemp(dir=str(p.parent), prefix=p.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
        os.replace(tmp, p)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def _check_fix_fields(d: dict, fixes: list[dict]) -> None:
    """Refuse a malformed fix plan before any git call or GitHub write."""
    for n in fixes:
        for key in ("fixed_in", "fix_summary"):
            v = n.get(key)
            if not isinstance(v, str) or not v.strip():
                raise ReviewError(f"note {n['id']} is a fix but has no {key}. Nothing was pushed or posted.")
    if fixes:
        fix = d.get("fix")
        for key in ("copy_path", "branch"):
            if not isinstance(fix, dict) or not isinstance(fix.get(key), str) or not fix[key].strip():
                raise ReviewError(f"The draft has fix notes but no fix.{key}. Nothing was pushed or posted.")


def _all_threads(account: str, pr: dict) -> list[dict]:
    out, after = [], None
    for _ in range(50):
        res = gh_api(account, "graphql", method="POST", body={"query": THREADS, "variables": {
            "owner": pr["owner"], "repo": pr["repo"], "number": pr["number"], "after": after}})
        conn = res["data"]["repository"]["pullRequest"]["reviewThreads"]
        out += conn["nodes"]
        info = conn.get("pageInfo") or {}
        if not info.get("hasNextPage"):
            break
        after = info["endCursor"]
    return out


def _norm(text: str | None) -> str:
    return (text or "").replace("\r\n", "\n").strip()


def _match_comments(wanted_notes: list[dict], posted: list[dict], body_fn, others: list[dict] = ()) -> dict[int, int]:
    """Note id -> id of the inline comment we posted for it. Matches on the body (line endings and edge
    whitespace ignored) and path, breaking ties on the line (outdated comments carry original_line, not line)
    and side. A note whose body matches nothing falls back to position (path, line, side) among the comments
    nobody else claimed, but only when exactly one fits. `others` are notes we post but do not resolve now:
    their comments are set aside first so a fallback can never pick one."""
    used: set[int] = set()
    found: dict[int, int] = {}

    def spot(c: dict, n: dict) -> bool:
        side = n.get("side", "RIGHT")
        return (c.get("path") == n["path"] and (c.get("original_line") or c.get("line")) == n["line"]
                and c.get("side", side) == side)

    for n in wanted_notes:
        body = _norm(body_fn(n))
        cands = [c for c in posted if c["id"] not in used and c.get("path") == n["path"]
                 and _norm(c.get("body")) == body]
        pick = ([c for c in cands if spot(c, n)] or cands or [None])[0]
        if pick is not None:
            used.add(pick["id"])
            found[n["id"]] = pick["id"]
    for n in others:
        body = _norm(body_fn(n))
        for c in posted:
            if c["id"] not in used and spot(c, n) and _norm(c.get("body")) == body:
                used.add(c["id"])
                break
    for n in wanted_notes:
        if n["id"] in found:
            continue
        fits = [c for c in posted if c["id"] not in used and spot(c, n)]
        if len(fits) == 1:
            used.add(fits[0]["id"])
            found[n["id"]] = fits[0]["id"]
    return found


def _earlier_threads(threads: list[dict], account: str, this_runs: set[int], handled: list[str]) -> list[str]:
    """Unresolved threads that our account started in an earlier round (not this run's comments)."""
    out = []
    for t in threads:
        first = (t["comments"]["nodes"] or [{}])[0]
        login = ((first.get("author") or {}).get("login") or "").lower()
        if (not t.get("isResolved") and login == account.lower() and first.get("databaseId") not in this_runs
                and t["id"] not in handled):
            out.append(t["id"])
    return out


def _wait_for_push(account: str, base: str, number: int, sha: str) -> None:
    for i in range(5):
        live = gh_api(account, f"{base}/pulls/{number}")
        if (live.get("head") or {}).get("sha") == sha:
            return
        if i < 4:
            _sleep(1)
    raise ReviewError("GitHub hasn't registered the push yet; re-run review_publish.py in a minute. "
                      "Nothing is lost: the push and the notes review are already on the PR.")


def _reverted_shas(copy: str, commits: list[tuple[str, str]]) -> list[str]:
    """Shas named by 'This reverts commit <sha>' in the bodies of the range's Revert commits."""
    out: list[str] = []
    for sha, subject in commits:
        if subject.startswith('Revert "'):
            out += re.findall(r"This reverts commit ([0-9a-f]{7,40})", git(copy, "log", "-1", "--format=%B", sha))
    return out


def _unclaimed_commits(commits: list[tuple[str, str]], fixes: list[dict],
                      reverted: list[str] = ()) -> list[tuple[str, str]]:
    """Commits in the copy that no fix note owns. A revert undoes a fix that went back to leave, so it needs no
    note of its own, and neither does the commit it reverted."""
    return [(sha, subj) for sha, subj in commits if not subj.startswith('Revert "')
            and not any(sha.startswith(n["fixed_in"]) or n["fixed_in"].startswith(sha) for n in fixes)
            and not any(sha.startswith(r) or r.startswith(sha) for r in reverted)]


def publish_review(path: str, dry_run: bool) -> dict:
    p = Path(path)
    d = json.loads(p.read_text())
    draft_mod.validate(d)
    flow = d.setdefault("flow", json.loads(json.dumps(EMPTY_FLOW)))
    pr = d["pr"]
    kept = [n for n in d["notes"] if n["keep"]]
    fixes = [n for n in kept if _act(n) == "fix"]
    _check_fix_fields(d, fixes)
    account = account_for_owner(pr["owner"])
    base = f"repos/{pr['owner']}/{pr['repo']}"
    live = gh_api(account, f"{base}/pulls/{pr['number']}")
    author = (live.get("user") or {}).get("login")
    if is_own_account(author):
        raise ReviewError("This is your PR — /pr-review fixes instead of posting. Nothing was posted.")
    if live["state"] != "open":
        raise ReviewError(f"PR #{pr['number']} is no longer open. Nothing was posted.")
    verdict = draft_mod.decide_verdict(d)

    local = None
    commit_lines: list[str] = []
    if fixes:
        if not may_fix_author(author):
            raise ReviewError(f"/pr-review only fixes PRs opened by the allow-listed teammates; PR #{pr['number']} "
                              f"was opened by {author}. Set those notes to leave. Nothing was pushed or posted.")
        head_repo = ((live["head"].get("repo") or {}).get("full_name") or "").lower()
        if head_repo != f"{pr['owner']}/{pr['repo']}".lower():
            raise ReviewError(f"PR #{pr['number']} comes from another repository (a fork); pushing to "
                              f"{pr['owner']}/{pr['repo']} would not update it. Nothing was pushed or posted.")
        copy = d["fix"]["copy_path"]
        local = git(copy, "rev-parse", "HEAD").strip()
        decision = head_decision(local, live["head"]["sha"], pr["head_sha"], flow["pushed"],
                                 label=f"PR #{pr['number']}", rerun="review_publish.py")
        dirty = uncommitted(copy)
        commits = []
        for line in git(copy, "log", "--format=%H%x09%s", f"{pr['head_sha']}..HEAD").splitlines():
            sha, _, subject = line.partition("\t")
            if sha:
                commits.append((sha, subject))
        for n in fixes:
            if not any(sha.startswith(n["fixed_in"]) or n["fixed_in"].startswith(sha) for sha, _ in commits):
                raise ReviewError(f"note {n['id']} says it is fixed in {n['fixed_in']}, which is not one of "
                                  "the fix commits. Nothing was pushed or posted.")
        unclaimed = _unclaimed_commits(commits, fixes, _reverted_shas(copy, commits))
        if unclaimed:
            names = "; ".join(f"commit {sha[:7]} {subject}" for sha, subject in unclaimed)
            raise ReviewError(f"{names} is not claimed by any fix note — revert it in the copy or set its note "
                              "back to fix. Nothing was pushed or posted." if len(unclaimed) == 1 else
                              f"{names}: these are not claimed by any fix note — revert them in the copy or set "
                              "their notes back to fix. Nothing was pushed or posted.")
        commit_lines = git(copy, "log", "--oneline", f"{pr['head_sha']}..HEAD").splitlines() if dry_run else []
    else:
        decision, dirty = None, []
        if live["head"]["sha"] != pr["head_sha"]:
            raise ReviewError(f"PR #{pr['number']} has new commits since the review. Re-run /pr-review. "
                              "Nothing was posted.")
    gate_head = local or live["head"]["sha"]
    to_issue = [n for n in kept if verdict == "APPROVE" and _act(n) == "leave" and n["severity"] == "should-fix"]
    if [n for n in to_issue if str(n["id"]) not in flow["issues"]]:
        if gh_api(account, base).get("has_issues") is False:
            raise ReviewError(f"Issues are turned off on {pr['owner']}/{pr['repo']}, so the should-fix leftovers "
                              "can't be tracked. Move them to fix, or turn issues on. "
                              "Nothing was pushed or posted.")

    warning = None
    if fixes:
        # The notes review is pinned at the pre-fix head, so judge lines against that diff, not the pushed one.
        files = _compare_files(account, base, live["base"]["ref"], pr["head_sha"])
        if len(files) >= 300:
            warning = "PR has 300+ files; some notes may move to the summary"
    else:
        files = gh_api(account, f"{base}/pulls/{pr['number']}/files?per_page=100", slurp=True)
    commentable = {f["filename"]: parse_patch_lines(f.get("patch")) for f in files}

    def pinned(n: dict) -> bool:  # posted inline, not moved into the summary
        right, left = commentable.get(n["path"], (set(), set()))
        return n["line"] in (right if n.get("side", "RIGHT") == "RIGHT" else left)

    wanted = set(notes_to_resolve(d, verdict))
    wanted_notes = [n for n in kept if n["id"] in wanted]

    if dry_run:
        earlier = 0
        if verdict == "APPROVE":
            this_runs: set[int] = set()
            for key in ("notes_review", "verdict_review"):
                if flow[key]:
                    got = gh_api(account, f"{base}/pulls/{pr['number']}/reviews/{flow[key]['id']}/comments"
                                          "?per_page=100", slurp=True)
                    this_runs |= {c["id"] for c in got}
            earlier = len(_earlier_threads(_all_threads(account, pr), account, this_runs, flow["resolved"]))
        if not dirty:
            d["dry_run"] = {"at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"), "head": gate_head,
                            "plan": _plan_fingerprint(d, verdict)}
            _save(p, d)
        pinned_kept = sum(1 for n in kept if pinned(n))
        reviews = ([{"event": "COMMENT", "comments": pinned_kept}, {"event": verdict, "comments": 0}] if fixes
                   else [{"event": verdict, "comments": pinned_kept}])
        out = {"dry_run": True, "verdict": verdict, "plan": draft_mod.plan_line(d),
               "push": ({"to": d["fix"]["branch"], "head": local} if fixes and decision == "push"
                        and not flow["pushed"] else None),
               "issues": [issue_payload(n, pr["number"], pr["url"])["title"] for n in to_issue
                          if str(n["id"]) not in flow["issues"]],
               "reviews": reviews, "resolve": sum(1 for n in wanted_notes if pinned(n)),
               "earlier_threads": earlier, "uncommitted": dirty}
        if fixes:
            out["commits"] = commit_lines
        if warning:
            out["warning"] = warning
        return out

    if dirty:
        raise ReviewError("The fix copy has uncommitted changes: " + "; ".join(dirty)
                          + ". Commit or discard them, then re-run. Nothing was pushed or posted.")
    record = d.get("dry_run") or {}
    if record.get("head") != gate_head or record.get("plan") != _plan_fingerprint(d, verdict):
        raise ReviewError("Run review_publish.py --dry-run first (and again after any change). "
                          "Nothing was pushed or posted.")

    if fixes and not flow["pushed"]:
        if decision == "push":
            push(d["fix"]["copy_path"], pr["owner"], pr["repo"], d["fix"]["branch"], rerun="review_publish.py")
        flow["pushed"] = local
        _save(p, d)

    for n in to_issue:
        if str(n["id"]) in flow["issues"]:
            continue
        res = gh_api(account, f"{base}/issues", method="POST", body=issue_payload(n, pr["number"], pr["url"]))
        flow["issues"][str(n["id"])] = {"number": res["number"], "url": res["html_url"]}
        _save(p, d)

    def body_fn(n: dict) -> str:
        return note_body(n, verdict, (flow["issues"].get(str(n["id"])) or {}).get("number"))

    def post_review(event: str, commit: str, with_notes: bool, summary: str) -> dict:
        nd = dict(d)
        nd["summary"] = summary
        if not with_notes:
            nd["notes"] = []
        payload, _moved = build_payload(nd, commentable, commit, body_fn=body_fn)
        payload["event"] = event
        if not with_notes:
            payload.pop("comments", None)
        return gh_api(account, f"{base}/pulls/{pr['number']}/reviews", method="POST", body=payload)

    review_with_notes = None
    if fixes:
        if not flow["notes_review"]:
            res = post_review("COMMENT", pr["head_sha"], True, "Review notes (fixes pushed to this branch).")
            flow["notes_review"] = {"id": res["id"], "url": res["html_url"]}
            _save(p, d)
        review_with_notes = flow["notes_review"]["id"]
    else:
        if not flow["verdict_review"]:
            res = post_review(verdict, pr["head_sha"], True, d["summary"])
            flow["verdict_review"] = {"id": res["id"], "url": res["html_url"]}
            _save(p, d)
        review_with_notes = flow["verdict_review"]["id"]

    posted = gh_api(account, f"{base}/pulls/{pr['number']}/reviews/{review_with_notes}/comments?per_page=100",
                    slurp=True)
    others = [n for n in kept if n["id"] not in wanted and pinned(n)]
    comment_of = _match_comments([n for n in wanted_notes if pinned(n)], posted, body_fn, others)
    threads = _all_threads(account, pr)
    thread_of = {}
    for t in threads:
        first = (t["comments"]["nodes"] or [{}])[0].get("databaseId")
        if first is not None:
            thread_of[first] = t["id"]
    missing = []
    for n in wanted_notes:
        if not pinned(n):
            continue
        tid = thread_of.get(comment_of.get(n["id"]))
        if tid is None:
            missing.append(n)
        elif tid not in flow["resolved"]:
            gh_api(account, "graphql", method="POST", body={"query": RESOLVE, "variables": {"id": tid}})
            flow["resolved"].append(tid)
            _save(p, d)
    if missing:
        names = ", ".join(f"{m['path']}:{m['line']}" for m in missing)
        raise ReviewError(f"Could not resolve {len(missing)} of our threads ({names}); nothing more was posted. "
                          "Re-run review_publish.py.")

    if verdict == "APPROVE":
        # The repo needs every thread resolved before merge, including the ones from our earlier rounds.
        for tid in _earlier_threads(threads, account, {c["id"] for c in posted}, flow["resolved"]):
            gh_api(account, "graphql", method="POST", body={"query": RESOLVE, "variables": {"id": tid}})
            flow["resolved"].append(tid)
            _save(p, d)

    if fixes and not flow["verdict_review"]:
        _wait_for_push(account, base, pr["number"], flow["pushed"])
        res = post_review(verdict, flow["pushed"], False, d["summary"])
        flow["verdict_review"] = {"id": res["id"], "url": res["html_url"]}
        _save(p, d)

    flow["done"] = True
    d["posted"] = {"url": flow["verdict_review"]["url"], "head_sha": flow["pushed"] or pr["head_sha"],
                   "at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")}
    _save(p, d)
    out = {"verdict": verdict, "pushed": flow["pushed"], "issues": flow["issues"],
           "notes_review": (flow["notes_review"] or {}).get("url"), "verdict_review": flow["verdict_review"]["url"],
           "resolved": len(flow["resolved"])}
    if fixes:
        # Everything is on GitHub; a copy that can't be removed (already gone, say) is not a failure.
        try:
            fix_git(str(main_checkout(Path(d["fix"]["copy_path"]))), "worktree", "remove", "--force",
                    d["fix"]["copy_path"])
            out["copy_removed"] = True
        except (ReviewError, OSError):
            out["copy_removed"] = False
    try:
        draft_mod.render(str(p))
    except Exception:  # noqa: BLE001
        pass
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("draft")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    run_main(lambda: publish_review(a.draft, a.dry_run))


if __name__ == "__main__":
    main()
