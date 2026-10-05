#!/usr/bin/env python3
"""Fetch what the review needs about one PR. Prints JSON."""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from prlib import REVIEWS_DIR, ReviewError, account_for_owner, commit_identity, gh_api, is_own_account, parse_pr_url, run_main  # noqa: E402

LARGE_PR_LINES = 3000
_REMOTE_RE = re.compile(r"github\.com[:/]([^/\s]+)/([^/\s]+?)(?:\.git)?/?$")


def local_repo_matches(remote_urls: list[str], owner: str, repo: str) -> bool:
    want = f"{owner}/{repo}".lower()
    for url in remote_urls:
        m = _REMOTE_RE.search(url.strip())
        if m and f"{m.group(1)}/{m.group(2)}".lower() == want:
            return True
    return False


def _remote_urls(cwd: str) -> list[str]:
    r = subprocess.run(["git", "-C", cwd, "remote", "-v"], capture_output=True, text=True)
    return [parts[1] for parts in (l.split() for l in r.stdout.splitlines()) if len(parts) >= 2]


def build_context(link: str, cwd: str) -> dict:
    owner, repo, number = parse_pr_url(link)
    if not local_repo_matches(_remote_urls(cwd), owner, repo):
        raise ReviewError(
            f"This folder ({cwd}) is not a copy of {owner}/{repo}. "
            "Open a Claude tab in that project and run /pr-review there."
        )
    account = account_for_owner(owner)
    pr = gh_api(account, f"repos/{owner}/{repo}/pulls/{number}")
    if pr["state"] != "open":
        state = "merged" if pr.get("merged_at") else "closed"
        raise ReviewError(f"PR #{number} is {state}; there is nothing to review.")
    files = gh_api(account, f"repos/{owner}/{repo}/pulls/{number}/files?per_page=100", slurp=True)
    diff = gh_api(account, f"repos/{owner}/{repo}/pulls/{number}", accept="application/vnd.github.diff")
    work = REVIEWS_DIR / ".work"
    work.mkdir(parents=True, exist_ok=True)
    diff_path = work / f"{owner}-{repo}-{number}.diff"
    diff_path.write_text(diff)
    changed = sum(f.get("additions", 0) + f.get("deletions", 0) for f in files)
    ctx_path = work / f"{owner}-{repo}-{number}.ctx.json"
    author = (pr.get("user") or {}).get("login") or "ghost"
    ctx = {
        "pr": {
            "owner": owner, "repo": repo, "number": number, "url": pr["html_url"],
            "title": pr["title"], "body": pr.get("body") or "",
            "base": pr["base"]["ref"], "head": pr["head"]["ref"], "head_sha": pr["head"]["sha"],
        },
        "account": account,
        "author": author,
        "own_pr": is_own_account(author),
        "commit_identity": list(commit_identity(account)),
        "files": [
            {"path": f["filename"], "status": f["status"], "additions": f.get("additions", 0),
             "deletions": f.get("deletions", 0), "has_patch": bool(f.get("patch"))}
            for f in files
        ],
        "changed_lines": changed,
        "large": changed > LARGE_PR_LINES,
        "diff_path": str(diff_path),
        "ctx_path": str(ctx_path),
    }
    # Per-PR file, so two reviews running in different tabs never share a work file.
    ctx_path.write_text(json.dumps(ctx, indent=2))
    return ctx


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("link")
    ap.add_argument("--cwd", default=os.getcwd())
    args = ap.parse_args()
    run_main(lambda: build_context(args.link, args.cwd))


if __name__ == "__main__":
    main()
