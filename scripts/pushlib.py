"""Shared push logic for the fix and team-review flows: git helper, head rule, push, failure triage."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from prlib import ReviewError, repo_url  # noqa: E402


def git(cwd: str, *args: str) -> str:
    # Never let a push wait on a password prompt: no stdin, and git told not to ask the terminal.
    child_env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    r = subprocess.run(["git", "-C", cwd, *args], capture_output=True, text=True,
                       stdin=subprocess.DEVNULL, env=child_env)
    if r.returncode:
        raise ReviewError(f"git {args[0]} failed: {(r.stderr or r.stdout).strip()[-500:]}")
    return r.stdout


def uncommitted(copy: str) -> list[str]:
    return git(copy, "status", "--porcelain", "--untracked-files=no").splitlines()


def push_failure(owner: str, repo: str, e: ReviewError, rerun: str = "publish.py") -> ReviewError:
    text = str(e).lower()
    stale = "non-fast-forward" in text or "fetch first" in text or \
        ("rejected" in text and "remote rejected" not in text)
    if stale:
        return ReviewError("GitHub refused the push because the PR has new commits. Nothing was posted. "
                           "Rebuild the copy on the new code (fixcopy.py create --replace) and redo the fixes.")
    return ReviewError(f"The push failed (git access?): {e}. Nothing was posted and nothing is lost: "
                       f"fix git access to {owner}/{repo}, then re-run {rerun}.")


def head_decision(local: str, live: str, base_sha: str, pushed: str | None, label: str = "The PR",
                  rerun: str = "publish.py") -> str:
    """'already' when GitHub has our commit, 'push' when it is still at the base (or our last push), else refuse."""
    if live == local:
        return "already"  # already on GitHub, e.g. a crash between the push and saving it
    if live == base_sha or (pushed and live == pushed):
        return "push"
    expected = pushed or base_sha
    again = "check again" if rerun == "publish.py" else f"check again, then re-run {rerun}"
    raise ReviewError(
        f"{label} has new commits ({expected[:7]} → {live[:7]}). "
        f"Nothing was posted. Rebuild the fix copy on the new code and {again}."
    )


def push(copy: str, owner: str, repo: str, branch: str, rerun: str = "publish.py") -> None:
    try:
        git(copy, "push", repo_url(owner, repo), f"HEAD:refs/heads/{branch}")
    except ReviewError as e:
        raise push_failure(owner, repo, e, rerun) from None
