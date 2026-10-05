import pytest

import merge
from prlib import ReviewError


def node(**over):
    n = {"author": {"login": "alice-dev"}, "state": "OPEN", "isDraft": False, "baseRefName": "develop",
         "headRefName": "feat", "headRefOid": "abc123", "headRepository": {"nameWithOwner": "acme-corp/web-app"},
         "reviewDecision": "APPROVED", "mergeStateStatus": "CLEAN",
         "latestReviews": {"nodes": [{"state": "APPROVED", "author": {"login": "bob-dev"},
                                      "commit": {"oid": "abc123"}}]},
         "reviewThreads": {"nodes": [{"isResolved": True}], "pageInfo": {"hasNextPage": False}},
         "commits": {"nodes": [{"commit": {"statusCheckRollup": {"state": "SUCCESS"}}}]}}
    n.update(over)
    return n


class FakeGitHub:
    def __init__(self, pr, allowed=("squash",), fail_delete=False, default_branch="main", open_pr_bases=None,
                 fail_lookup=False):
        self.fail_lookup = fail_lookup
        self.pr, self.allowed, self.fail_delete, self.calls = pr, list(allowed), fail_delete, []
        self.default_branch = default_branch
        self.open_pr_bases = open_pr_bases or []

    def __call__(self, account, path, method="GET", body=None, **kw):
        self.calls.append((method, path, body))
        if path == "graphql":
            return {"data": {"repository": {"pullRequest": self.pr}}}
        if "/rules/branches/" in path:
            return [{"type": "pull_request", "parameters": {"allowed_merge_methods": self.allowed}}]
        if path == "repos/acme-corp/web-app":
            return {"default_branch": self.default_branch}
        if "/pulls?" in path and "state=open" in path:
            if self.fail_lookup:
                raise ReviewError("lookup refused")
            return [{"base": {"ref": b}} for b in self.open_pr_bases]
        if method == "PUT":
            return {"merged": True, "sha": "m1"}
        if method == "DELETE":
            if self.fail_delete:
                raise ReviewError("refused")
            return None
        return {}


LINK = "https://github.com/acme-corp/web-app/pull/63"


@pytest.fixture(autouse=True)
def acct(monkeypatch):
    monkeypatch.setattr(merge, "account_for_owner", lambda o: "alice-dev")


def test_choose_method():
    assert merge.choose_method(["merge", "squash"]) == "squash"
    assert merge.choose_method(["rebase"]) == "rebase"
    assert merge.choose_method([]) == "merge"


def test_readiness_lists_every_blocker():
    bad = node(reviewDecision="REVIEW_REQUIRED", reviewThreads={"nodes": [{"isResolved": False}] * 3},
               commits={"nodes": [{"commit": {"statusCheckRollup": {"state": "FAILURE"}}}]}, isDraft=True)
    text = " | ".join(merge.readiness(bad))
    assert "not approved" in text and "3 open threads" in text and "checks" in text and "draft" in text
    assert merge.readiness(node()) == []


def test_dry_run_reports_plan(monkeypatch):
    gh = FakeGitHub(node())
    monkeypatch.setattr(merge, "gh_api", gh)
    out = merge.merge(LINK, dry_run=True, confirm=None)
    assert out == {"dry_run": True, "ready": True, "blockers": [], "method": "squash", "head": "abc123", "branch": "feat"}
    assert not [c for c in gh.calls if c[0] in ("PUT", "DELETE")]


def test_real_merge_needs_matching_confirm(monkeypatch):
    gh = FakeGitHub(node())
    monkeypatch.setattr(merge, "gh_api", gh)
    with pytest.raises(ReviewError, match="changed since"):
        merge.merge(LINK, dry_run=False, confirm="old999")
    out = merge.merge(LINK, dry_run=False, confirm="abc123")
    assert out == {"merged": True, "method": "squash", "sha": "m1", "branch_deleted": True}
    put = [b for m, p, b in gh.calls if m == "PUT"][0]
    assert put == {"merge_method": "squash", "sha": "abc123"}
    assert ("DELETE", "repos/acme-corp/web-app/git/refs/heads/feat", None) in gh.calls


def test_refuses_someone_elses_pr_and_unready(monkeypatch):
    monkeypatch.setattr(merge, "gh_api", FakeGitHub(node(author={"login": "bob-dev"})))
    with pytest.raises(ReviewError, match="only merges your own PRs"):
        merge.merge(LINK, dry_run=True, confirm=None)
    monkeypatch.setattr(merge, "gh_api", FakeGitHub(node(reviewDecision="CHANGES_REQUESTED")))
    with pytest.raises(ReviewError, match="not ready"):
        merge.merge(LINK, dry_run=False, confirm="abc123")


def test_fork_branch_not_deleted_and_delete_failure_is_soft(monkeypatch):
    gh = FakeGitHub(node(headRepository={"nameWithOwner": "x/fork"}))
    monkeypatch.setattr(merge, "gh_api", gh)
    assert merge.merge(LINK, dry_run=False, confirm="abc123")["branch_deleted"] is False
    assert not [c for c in gh.calls if c[0] == "DELETE"]
    gh2 = FakeGitHub(node(), fail_delete=True)
    monkeypatch.setattr(merge, "gh_api", gh2)
    out = merge.merge(LINK, dry_run=False, confirm="abc123")
    assert out["branch_deleted"] is False and "warning" in out


def test_null_pullrequest_raises_error(monkeypatch):
    gh = FakeGitHub(None)
    monkeypatch.setattr(merge, "gh_api", gh)
    with pytest.raises(ReviewError, match="PR #63 not found"):
        merge.merge(LINK, dry_run=True, confirm=None)


def test_readiness_blocks_on_merge_state_status(monkeypatch):
    # DIRTY = conflicts
    gh = FakeGitHub(node(mergeStateStatus="DIRTY"))
    monkeypatch.setattr(merge, "gh_api", gh)
    out = merge.merge(LINK, dry_run=True, confirm=None)
    assert "conflicts" in " ".join(out["blockers"])

    # BEHIND = branch out of date
    gh2 = FakeGitHub(node(mergeStateStatus="BEHIND"))
    monkeypatch.setattr(merge, "gh_api", gh2)
    out2 = merge.merge(LINK, dry_run=True, confirm=None)
    assert "out of date" in " ".join(out2["blockers"])

    # BLOCKED = required checks/reviews not satisfied
    gh3 = FakeGitHub(node(mergeStateStatus="BLOCKED"))
    monkeypatch.setattr(merge, "gh_api", gh3)
    out3 = merge.merge(LINK, dry_run=True, confirm=None)
    assert "required" in " ".join(out3["blockers"])


def test_readiness_blocks_on_too_many_threads(monkeypatch):
    gh = FakeGitHub(node(reviewThreads={"nodes": [{"isResolved": True}], "pageInfo": {"hasNextPage": True}}))
    monkeypatch.setattr(merge, "gh_api", gh)
    out = merge.merge(LINK, dry_run=True, confirm=None)
    assert "more than 100" in " ".join(out["blockers"])


def test_branch_delete_guards(monkeypatch):
    # Release PR with base=develop, head=develop — don't delete develop
    gh = FakeGitHub(node(headRefName="develop", baseRefName="main"))
    monkeypatch.setattr(merge, "gh_api", gh)
    out = merge.merge(LINK, dry_run=False, confirm="abc123")
    assert out["branch_deleted"] is False
    assert not [c for c in gh.calls if c[0] == "DELETE"]

    # Branch is "main" — don't delete
    gh2 = FakeGitHub(node(headRefName="main", baseRefName="develop"))
    monkeypatch.setattr(merge, "gh_api", gh2)
    out2 = merge.merge(LINK, dry_run=False, confirm="abc123")
    assert out2["branch_deleted"] is False
    assert not [c for c in gh2.calls if c[0] == "DELETE"]

    # Branch is "master" — don't delete
    gh3 = FakeGitHub(node(headRefName="master", baseRefName="develop"))
    monkeypatch.setattr(merge, "gh_api", gh3)
    out3 = merge.merge(LINK, dry_run=False, confirm="abc123")
    assert out3["branch_deleted"] is False
    assert not [c for c in gh3.calls if c[0] == "DELETE"]

    # Another open PR is based on this branch — don't delete
    gh4 = FakeGitHub(node(headRefName="feat"), open_pr_bases=["feat"])
    monkeypatch.setattr(merge, "gh_api", gh4)
    out4 = merge.merge(LINK, dry_run=False, confirm="abc123")
    assert out4["branch_deleted"] is False
    assert not [c for c in gh4.calls if c[0] == "DELETE"]


def test_sha_mismatch_and_not_ready_assert_no_writes(monkeypatch):
    # SHA mismatch test — ensure no PUT or DELETE
    gh = FakeGitHub(node())
    monkeypatch.setattr(merge, "gh_api", gh)
    with pytest.raises(ReviewError, match="changed since"):
        merge.merge(LINK, dry_run=False, confirm="old999")
    assert not [c for c in gh.calls if c[0] in ("PUT", "DELETE")]

    # Not ready test — ensure no PUT or DELETE
    gh2 = FakeGitHub(node(reviewDecision="CHANGES_REQUESTED"))
    monkeypatch.setattr(merge, "gh_api", gh2)
    with pytest.raises(ReviewError, match="not ready"):
        merge.merge(LINK, dry_run=False, confirm="abc123")
    assert not [c for c in gh2.calls if c[0] in ("PUT", "DELETE")]


def approval(state="APPROVED", login="bob-dev", oid="abc123"):
    return {"state": state, "author": {"login": login}, "commit": {"oid": oid}}


def blockers(**over):
    return " | ".join(merge.readiness(node(**over)))


def test_approval_on_an_older_commit_blocks():
    text = blockers(latestReviews={"nodes": [approval(oid="old000")]})
    assert "approved, but not the latest commit — new commits since the approval" in text


def test_own_approval_does_not_count():
    assert "not approved" in blockers(latestReviews={"nodes": [approval(login="alice-dev")]})
    assert "not approved" in blockers(latestReviews={"nodes": []})


def test_approval_that_is_not_latest_state_does_not_count():
    assert "not approved" in blockers(latestReviews={"nodes": [approval(state="COMMENTED")]})


def test_null_review_decision_is_fine_with_an_approval_on_head():
    assert merge.readiness(node(reviewDecision=None)) == []


def test_null_review_decision_without_approval_blocks():
    assert "not approved" in blockers(reviewDecision=None, latestReviews={"nodes": []})


@pytest.mark.parametrize("decision", ["CHANGES_REQUESTED", "REVIEW_REQUIRED"])
def test_blocking_review_decisions_still_block_despite_an_approval(decision):
    assert "not approved" in blockers(reviewDecision=decision)


def test_query_asks_for_latest_reviews_with_commit():
    assert "latestReviews" in merge.QUERY and "commit { oid }" in merge.QUERY


def test_lookup_failure_after_merge_is_a_warning_not_an_error(monkeypatch):
    gh = FakeGitHub(node(), fail_lookup=True)
    monkeypatch.setattr(merge, "gh_api", gh)
    out = merge.merge(LINK, dry_run=False, confirm="abc123")
    assert out["merged"] is True and out["branch_deleted"] is False
    assert out["kept_reason"].startswith("lookup failed: ") and "warning" in out
    assert not [c for c in gh.calls if c[0] == "DELETE"]


def test_open_pr_lookup_url_encodes_the_branch(monkeypatch):
    gh = FakeGitHub(node(headRefName="feat/my branch"))
    monkeypatch.setattr(merge, "gh_api", gh)
    merge.merge(LINK, dry_run=False, confirm="abc123")
    lookups = [p for m, p, b in gh.calls if "state=open" in p]
    assert lookups and "base=feat%2Fmy%20branch&" in lookups[0]


@pytest.mark.parametrize("head,base,bases,reason", [
    ("main", "develop", [], "default branch"),
    ("develop", "main", [], "long-lived branch"),
    ("feat", "develop", ["feat"], "base of an open PR"),
])
def test_skipped_delete_says_why(monkeypatch, head, base, bases, reason):
    gh = FakeGitHub(node(headRefName=head, baseRefName=base), open_pr_bases=bases)
    monkeypatch.setattr(merge, "gh_api", gh)
    out = merge.merge(LINK, dry_run=False, confirm="abc123")
    assert out["branch_deleted"] is False and out["kept_reason"] == reason


def test_deleted_branch_has_no_kept_reason(monkeypatch):
    monkeypatch.setattr(merge, "gh_api", FakeGitHub(node()))
    assert "kept_reason" not in merge.merge(LINK, dry_run=False, confirm="abc123")
