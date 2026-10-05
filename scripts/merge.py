#!/usr/bin/env python3
"""Merge the owner's own approved PR and delete its branch. Prints JSON."""
from __future__ import annotations

import argparse
import sys
import urllib.parse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from prlib import ReviewError, account_for_owner, gh_api, is_own_account, parse_pr_url, run_main  # noqa: E402

QUERY = """query($owner: String!, $repo: String!, $number: Int!) { repository(owner: $owner, name: $repo) {
  pullRequest(number: $number) { author { login } state isDraft baseRefName headRefName headRefOid
    headRepository { nameWithOwner } reviewDecision mergeStateStatus
    latestReviews(first: 20) { nodes { state author { login } commit { oid } } }
    reviewThreads(first: 100) { nodes { isResolved } pageInfo { hasNextPage } }
    commits(last: 1) { nodes { commit { statusCheckRollup { state } } } } } } }"""
PREFERENCE = ["squash", "merge", "rebase"]


def choose_method(allowed: list[str]) -> str:
    for m in PREFERENCE:
        if m in allowed:
            return m
    return "merge"


def _approval_blockers(pr: dict) -> list[str]:
    """Ready needs an APPROVED latest review by someone else on the current head. A repo with no required
    review reports a null decision; that is fine when such an approval exists, but changes-requested and
    review-required always block."""
    decision = pr.get("reviewDecision")
    if decision not in ("APPROVED", None):
        return [f"not approved yet (review decision: {decision.lower()})"]
    approved = [r for r in (pr.get("latestReviews") or {}).get("nodes") or []
                if r.get("state") == "APPROVED" and not is_own_account((r.get("author") or {}).get("login"))]
    if any((r.get("commit") or {}).get("oid") == pr.get("headRefOid") for r in approved):
        return []
    if approved:
        return ["approved, but not the latest commit — new commits since the approval"]
    return [f"not approved yet (review decision: {decision or 'none'})"]


def readiness(pr: dict) -> list[str]:
    out = []
    if pr["state"] != "OPEN":
        out.append(f"the PR is {pr['state'].lower()}")
    if pr.get("isDraft"):
        out.append("the PR is still a draft")
    out += _approval_blockers(pr)
    merge_state = pr.get("mergeStateStatus")
    if merge_state == "DIRTY":
        out.append("the branch has conflicts")
    elif merge_state == "BEHIND":
        out.append("the branch is out of date with the base branch")
    elif merge_state == "BLOCKED":
        out.append("required status checks or reviews are not satisfied")
    open_threads = sum(1 for t in pr["reviewThreads"]["nodes"] if not t["isResolved"])
    if open_threads:
        out.append(f"{open_threads} open threads (only their authors resolve reviewers' threads)")
    if (pr.get("reviewThreads") or {}).get("pageInfo", {}).get("hasNextPage"):
        out.append("more than 100 review threads — check them on GitHub")
    nodes = pr["commits"]["nodes"]
    rollup = ((nodes[0]["commit"].get("statusCheckRollup") or {}).get("state")) if nodes else None
    if rollup not in (None, "SUCCESS"):
        out.append(f"checks are not green ({rollup.lower()})")
    return out


def _allowed_methods(account: str, owner: str, repo: str, base_ref: str) -> tuple[list[str], dict]:
    rules = gh_api(account, f"repos/{owner}/{repo}/rules/branches/{base_ref}") or []
    for r in rules:
        if r.get("type") == "pull_request":
            allowed = (r.get("parameters") or {}).get("allowed_merge_methods")
            if allowed:
                info = gh_api(account, f"repos/{owner}/{repo}")
                return allowed, info
    info = gh_api(account, f"repos/{owner}/{repo}")
    flags = {"squash": "allow_squash_merge", "merge": "allow_merge_commit", "rebase": "allow_rebase_merge"}
    return [m for m, f in flags.items() if info.get(f)], info


def _keep_reason(account: str, owner: str, repo: str, branch: str, repo_info: dict) -> str | None:
    """Why the branch must be kept, or None when it may be deleted. Raises ReviewError if the open-PR lookup fails."""
    if branch.lower() == (repo_info.get("default_branch") or "").lower():
        return "default branch"
    if branch.lower() in ("develop", "main", "master"):
        return "long-lived branch"
    quoted = urllib.parse.quote(branch, safe="")
    if gh_api(account, f"repos/{owner}/{repo}/pulls?state=open&base={quoted}&per_page=1") or []:
        return "base of an open PR"
    return None


def merge(link: str, dry_run: bool, confirm: str | None) -> dict:
    owner, repo, number = parse_pr_url(link)
    account = account_for_owner(owner)
    pr = gh_api(account, "graphql", method="POST", body={"query": QUERY, "variables": {
        "owner": owner, "repo": repo, "number": number}})["data"]["repository"]["pullRequest"]
    if pr is None:
        raise ReviewError(f"PR #{number} not found on {owner}/{repo}")
    if not is_own_account((pr.get("author") or {}).get("login")):
        raise ReviewError(f"/pr-review only merges your own PRs; #{number} was opened by "
                          f"{(pr.get('author') or {}).get('login')}. Its author merges it.")
    blockers = readiness(pr)
    allowed, repo_info = _allowed_methods(account, owner, repo, pr["baseRefName"])
    method = choose_method(allowed)
    head, branch = pr["headRefOid"], pr["headRefName"]
    if dry_run:
        return {"dry_run": True, "ready": not blockers, "blockers": blockers, "method": method,
                "head": head, "branch": branch}
    if blockers:
        raise ReviewError("PR is not ready to merge: " + "; ".join(blockers) + ". Nothing was merged.")
    if confirm != head:
        raise ReviewError(f"The PR changed since the dry run (head is now {head[:7]}). Run --dry-run again. "
                          "Nothing was merged.")
    res = gh_api(account, f"repos/{owner}/{repo}/pulls/{number}/merge", method="PUT",
                 body={"merge_method": method, "sha": head})
    out = {"merged": True, "method": method, "sha": res.get("sha"), "branch_deleted": False}
    same_repo = ((pr.get("headRepository") or {}).get("nameWithOwner") or "").lower() == f"{owner}/{repo}".lower()
    if not same_repo:
        out["kept_reason"] = "branch is in a fork"
        return out
    # The PR is already merged: nothing below may turn into an error.
    stage = "lookup"
    try:
        reason = _keep_reason(account, owner, repo, branch, repo_info)
        stage = "delete"
        if reason:
            out["kept_reason"] = reason
        else:
            gh_api(account, f"repos/{owner}/{repo}/git/refs/heads/{branch}", method="DELETE")
            out["branch_deleted"] = True
    except ReviewError as e:
        out["kept_reason"] = f"{stage} failed: {e}"
        out["warning"] = f"merged, but the branch could not be deleted: {e}"
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("link")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--dry-run", action="store_true")
    g.add_argument("--confirm", metavar="HEAD_SHA")
    a = ap.parse_args()
    run_main(lambda: merge(a.link, a.dry_run, a.confirm))


if __name__ == "__main__":
    main()
