#!/usr/bin/env python3
"""Collect a PR's open review comments (inline threads + review summaries). Prints JSON."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import prlib  # noqa: E402
from prlib import gh_api, is_own_account, run_main  # noqa: E402

REVIEWS_DIR = prlib.REVIEWS_DIR

QUERY = """
query($owner: String!, $repo: String!, $number: Int!, $after: String) {
  repository(owner: $owner, name: $repo) {
    pullRequest(number: $number) {
      reviewThreads(first: 100, after: $after) {
        pageInfo { hasNextPage endCursor }
        nodes {
          id isResolved isOutdated path line originalLine
          comments(first: 50) { nodes { databaseId author { login } body url createdAt } }
        }
      }
      reviews(last: 100) { nodes { databaseId author { login } state body submittedAt } }
      comments(last: 50) { nodes { author { login } createdAt } }
    }
  }
}
"""


def _login(node: dict | None) -> str:
    return ((node or {}).get("author") or {}).get("login") or "ghost"


def _own_activity(threads: list[dict], reviews: list[dict], pr_comments) -> list[str]:
    """Timestamps (ISO, so they sort as strings) of everything the owner's accounts did after a review."""
    times: list[str] = []
    for t in threads:
        for c in t["comments"]["nodes"][1:]:  # the opening comment is a note, not an answer
            if is_own_account(_login(c)) and c.get("createdAt"):
                times.append(c["createdAt"])
    times += [r["submittedAt"] for r in reviews if is_own_account(_login(r)) and r.get("submittedAt")]
    times += [c["createdAt"] for c in pr_comments if is_own_account(_login(c)) and c.get("createdAt")]
    return times


def collect_items(threads: list[dict], reviews: list[dict], start_id: int = 1, pr_comments=()) -> dict:
    items, answered, next_id, awaiting = [], [], start_id, 0
    for t in threads:
        comments = t["comments"]["nodes"]
        if t["isResolved"] or not comments:
            continue
        first = comments[0]
        source = _login(first)
        line = t["line"] if t["line"] is not None else t.get("originalLine")
        if not is_own_account(source) and is_own_account(_login(comments[-1])):
            awaiting += 1  # we already answered; the ball is with the reviewer
            answered.append({"path": t["path"], "line": line, "source": source,
                             "excerpt": (first.get("body") or "")[:200]})
            continue
        items.append({
            "id": next_id, "kind": "thread", "source": source, "own": is_own_account(source),
            "thread_id": t["id"], "comment_id": first["databaseId"], "path": t["path"],
            "line": line,
            "outdated": t["isOutdated"], "body": first["body"],
            "follow_ups": [{"author": _login(c), "body": c["body"]} for c in comments[1:]],
            "url": first.get("url"),
        })
        next_id += 1
    pending: dict[str, dict] = {}  # each reviewer's last CHANGES_REQUESTED, until approved or dismissed
    for r in reviews:  # GitHub returns these oldest first
        login = _login(r)
        if is_own_account(login):
            continue
        if r["state"] == "CHANGES_REQUESTED":
            pending[login] = r
        elif r["state"] in ("APPROVED", "DISMISSED"):
            pending.pop(login, None)
    own_times = _own_activity(threads, reviews, pr_comments)
    summaries = []
    for login, r in pending.items():
        if not (r.get("body") or "").strip():
            continue
        at = r.get("submittedAt")
        summaries.append({"reviewer": login, "review_id": r["databaseId"], "body": r["body"],
                          "submitted_at": at, "answered": bool(at) and any(t > at for t in own_times)})
    return {"items": items, "summaries": summaries, "answered": answered, "awaiting_reviewer": awaiting,
            "unanswered_summaries": sum(1 for s in summaries if not s["answered"]), "next_id": next_id}


def fetch(account: str, owner: str, repo: str, number: int) -> tuple[list[dict], list[dict], list[dict]]:
    threads: list[dict] = []
    reviews: list[dict] = []
    pr_comments: list[dict] = []
    after = None
    while True:
        data = gh_api(account, "graphql", method="POST", body={
            "query": QUERY, "variables": {"owner": owner, "repo": repo, "number": number, "after": after}})
        pr = data["data"]["repository"]["pullRequest"]
        threads += pr["reviewThreads"]["nodes"]
        reviews = pr["reviews"]["nodes"]
        pr_comments = (pr.get("comments") or {}).get("nodes") or []  # first page only
        page = pr["reviewThreads"]["pageInfo"]
        if not page["hasNextPage"]:
            return threads, reviews, pr_comments
        after = page["endCursor"]


def collect_to_file(ctx_path: str, start_id: int) -> dict:
    ctx = json.loads(Path(ctx_path).read_text())
    pr = ctx["pr"]
    got_threads, got_reviews, got_comments = fetch(ctx["account"], pr["owner"], pr["repo"], pr["number"])
    found = collect_items(got_threads, got_reviews, start_id=start_id, pr_comments=got_comments)
    work = REVIEWS_DIR / ".work"
    work.mkdir(parents=True, exist_ok=True)
    out = work / f"{pr['owner']}-{pr['repo']}-{pr['number']}.threads.json"
    out.write_text(json.dumps(found, indent=2))
    return {"path": str(out), "items": len(found["items"]), "summaries": len(found["summaries"]),
            "awaiting_reviewer": found["awaiting_reviewer"],
            "unanswered_summaries": found["unanswered_summaries"]}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("ctx")
    ap.add_argument("--start-id", type=int, default=1)
    a = ap.parse_args()
    run_main(lambda: collect_to_file(a.ctx, a.start_id))


if __name__ == "__main__":
    main()
