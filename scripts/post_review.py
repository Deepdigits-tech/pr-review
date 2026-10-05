#!/usr/bin/env python3
"""Post a saved review draft to GitHub as one COMMENT review with inline notes."""
from __future__ import annotations

import datetime as dt
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import draft as draft_mod  # noqa: E402
from prlib import ReviewError, account_for_owner, gh_api, is_own_account, parse_patch_lines, run_main  # noqa: E402


def _comment_body(n: dict) -> str:
    if n.get("title"):
        return f"**[{n['severity']}] {n['title']}**\n\n{n['body']}"
    return f"**[{n['severity']}]** {n['body']}"


def build_payload(draft: dict, commentable: dict, head_sha: str, body_fn=None) -> tuple[dict, list[dict]]:
    body_of = body_fn or _comment_body
    comments, moved = [], []
    for n in draft["notes"]:
        if not n["keep"]:
            continue
        side = n.get("side", "RIGHT")
        right, left = commentable.get(n["path"], (set(), set()))
        if n["line"] in (right if side == "RIGHT" else left):
            comments.append({"path": n["path"], "line": n["line"], "side": side, "body": body_of(n)})
        else:
            moved.append(n)
    body = draft["summary"]
    if moved:
        def format_moved_line(n):
            comment_body = body_of(n)
            body_lines = comment_body.split('\n')
            if len(body_lines) > 1:
                # Multi-line: first line after bullet, rest indented by 2 spaces
                first = body_lines[0]
                rest = '\n  '.join(body_lines[1:])
                return f"- `{n['path']}:{n['line']}` — {first}\n  {rest}"
            else:
                # Single line
                return f"- `{n['path']}:{n['line']}` — {comment_body}"

        lines = [format_moved_line(n) for n in moved]
        body += "\n\n**Notes on lines outside this PR's changes**\n\n" + "\n".join(lines)
    return {"commit_id": head_sha, "event": "COMMENT", "body": body, "comments": comments}, moved


def post(path: str, dry_run: bool, allow_new_commits: bool, again: bool) -> dict:
    p = Path(path)
    draft = json.loads(p.read_text())
    draft_mod.validate(draft)
    pr = draft["pr"]
    if draft.get("posted") and not again:
        raise ReviewError(f"Already posted: {draft['posted']['url']}. Use --again to post a second review.")
    account = account_for_owner(pr["owner"])
    base = f"repos/{pr['owner']}/{pr['repo']}/pulls/{pr['number']}"
    live = gh_api(account, base)
    if is_own_account((live.get("user") or {}).get("login")):
        raise ReviewError("This is your PR — /pr-review fixes instead of posting.")
    if live["state"] != "open":
        raise ReviewError(f"PR #{pr['number']} is no longer open.")
    head_sha = live["head"]["sha"]
    if head_sha != pr["head_sha"] and not allow_new_commits:
        raise ReviewError(
            f"PR #{pr['number']} has new commits since the review "
            f"({pr['head_sha'][:7]} → {head_sha[:7]}). Re-run /pr-review, or post anyway with --allow-new-commits."
        )
    files = gh_api(account, f"{base}/files?per_page=100", slurp=True)
    commentable = {f["filename"]: parse_patch_lines(f.get("patch")) for f in files}
    payload, moved = build_payload(draft, commentable, head_sha)
    counts = {"pinned": len(payload["comments"]), "moved_to_summary": len(moved)}
    if dry_run:
        return {"dry_run": True, "account": account, "payload": payload, **counts}
    result = gh_api(account, f"{base}/reviews", method="POST", body=payload)
    out = {"posted": result["html_url"], "account": account, **counts}
    # The review is already on GitHub. A bookkeeping failure must not look like a failed post.
    try:
        draft["posted"] = {"url": result["html_url"], "head_sha": head_sha,
                           "at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")}
        p.write_text(json.dumps(draft, indent=2))
        draft_mod.render(str(p))
    except Exception as e:  # noqa: BLE001
        out["warning"] = f"review posted but the draft file could not be updated: {e}"
    return out


def main() -> None:
    # Posting now goes through review_publish.py (verdict, resolves, fixes, issues); the functions above stay
    # as a library for it.
    print(json.dumps({"error": "post_review.py no longer posts; use review_publish.py"}))
    sys.exit(2)


if __name__ == "__main__":
    main()
