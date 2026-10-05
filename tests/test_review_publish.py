import json
from pathlib import Path

import pytest

import draft as d
import pushlib
import review_publish as rp
from prlib import ReviewError


def note(i, sev="should-fix", action="leave", keep=True, **kw):
    n = {"id": i, "path": "a.py", "line": 10 + i, "side": "RIGHT", "severity": sev, "title": f"T{i}",
         "body": f"body {i}", "status": "agreed", "keep": keep, "action": action}
    if action == "fix":
        n.update(fixed_in=kw.pop("fixed_in", "fff0001"), fix_summary=kw.pop("fix_summary", f"fixed {i}"))
    n.update(kw)
    return n


def make_draft(tmp_path, notes, fix=True):
    dr = {"pr": {"owner": "acme-corp", "repo": "web-app", "number": 9,
                 "url": "https://github.com/acme-corp/web-app/pull/9", "title": "t", "head_sha": "base000"},
          "account": "alice-dev", "rounds": 1, "codex_checked": True, "summary": "Sum.", "notes": notes,
          "posted": None}
    if fix:
        dr["fix"] = {"copy_path": str(tmp_path / "copy"), "branch": "bob-branch"}
    return d.save(dr)["json"]


class FakeGitHub:
    def __init__(self, author="bob-dev", head="base000", repo="acme-corp/web-app", fail_on=None,
                 outdated=False, omit_comments=(), thread_page=100, has_issues=True, extra_threads=(),
                 mangle=None, compare_files=1, registers_push_after=0):
        self.calls, self.author, self.head, self.repo, self.fail_on = [], author, head, repo, fail_on
        self.outdated, self.omit_comments, self.thread_page = outdated, set(omit_comments), thread_page
        self.has_issues, self.extra_threads, self.mangle = has_issues, list(extra_threads), mangle
        self.compare_files = compare_files
        self.registers_push_after = registers_push_after  # head polls that still show the old head after a push
        self.polls = 0
        self.review_n = 0
        self.posted_reviews = []  # review payloads by id-1; a resumed run can be seeded from the failed one

    def __call__(self, account, path, method="GET", body=None, **kw):
        self.calls.append((method, path, body))
        if self.fail_on and self.fail_on(method, path, body):
            raise ReviewError(f"GitHub refused {path}")
        if path == "graphql":
            if "resolveReviewThread" in body["query"]:
                return {"data": {"resolveReviewThread": {"thread": {"isResolved": True}}}}
            ours = max((len(r.get("comments", [])) for r in self.posted_reviews), default=0)
            allnodes = [{"id": f"TH{c}", "isResolved": False, "comments": {"nodes": [
                {"databaseId": c, "author": {"login": "alice-dev" if c - 500 < ours else "other-person"}}]}}
                for c in range(500, 520) if c not in self.omit_comments]
            allnodes += [{"id": f"TH{c}", "isResolved": done, "comments": {"nodes": [
                {"databaseId": c, "author": {"login": who}}]}} for c, who, done in self.extra_threads]
            start = int(body["variables"].get("after") or 0)
            page = allnodes[start:start + self.thread_page]
            more = start + self.thread_page < len(allnodes)
            info = {"hasNextPage": more, "endCursor": str(start + self.thread_page) if more else None}
            return {"data": {"repository": {"pullRequest": {"reviewThreads": {"nodes": page, "pageInfo": info}}}}}
        if method == "GET" and "/files" in path:
            return [{"filename": "a.py", "patch": "@@ -1,1 +1,30 @@\n" + "+x\n" * 30}]
        if method == "GET" and "/compare/" in path:
            return {"files": [{"filename": "a.py", "patch": "@@ -1,1 +1,30 @@\n" + "+x\n" * 30}]
                    + [{"filename": f"f{i}.py"} for i in range(self.compare_files - 1)]}
        if method == "GET" and "/comments" in path:
            rid = int(path.split("/reviews/")[1].split("/")[0])
            review = self.posted_reviews[rid - 1]
            return [{"id": 500 + i, "path": c["path"], "side": c.get("side", "RIGHT"),
                     "line": None if self.outdated else c["line"], "original_line": c["line"],
                     "body": self.mangle(c["body"]) if self.mangle else c["body"]}
                    for i, c in enumerate(review["comments"])]
        if method == "GET" and path == "repos/acme-corp/web-app":
            return {"has_issues": self.has_issues}
        if method == "GET":
            return {"state": "open", "user": {"login": self.author}, "base": {"ref": "develop"},
                    "head": {"sha": self.live_head(), "repo": {"full_name": self.repo}}}
        if path.endswith("/issues"):
            return {"number": 77 + len([1 for m, p, _ in self.calls if p.endswith("/issues")]), "html_url": "https://i"}
        if path.endswith("/reviews"):
            self.review_n += 1
            self.posted_reviews.append(body)
            return {"id": self.review_n, "html_url": f"https://r/{self.review_n}"}
        return {}

    def live_head(self):
        """After our push GitHub shows the pushed commit, a few polls late when registers_push_after > 0."""
        if self.head == "base000" and any(g[0] == "push" for g in GITCALLS):
            self.polls += 1
            return "fff0002" if self.polls > self.registers_push_after else "base000"
        return self.head

    def posts(self, suffix):
        return [b for m, p, b in self.calls if m == "POST" and p.endswith(suffix)]


class GitCalls(list):
    """Records git calls. `commits` is what `git log <pr head>..HEAD` shows in the fix copy; `removed` holds
    the worktree-remove calls."""

    def __init__(self):
        super().__init__()
        self.commits = [("fff0001", "Fix the thing")]
        self.removed = []
        self.messages = {}  # sha -> full commit message, for `git log -1 --format=%B <sha>`


GITCALLS = GitCalls()  # the fake GitHub looks at it to know whether we have pushed


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(d, "REVIEWS_DIR", tmp_path)
    gitcalls = GITCALLS
    gitcalls.clear()
    gitcalls.commits = [("fff0001", "Fix the thing")]
    gitcalls.removed = []
    gitcalls.messages = {}

    def fake_git(cwd, *args):
        gitcalls.append(args)
        if args[0] == "rev-parse":
            return "fff0002\n"
        if args[:3] == ("log", "-1", "--format=%B"):
            return gitcalls.messages.get(args[3], "")
        if args[0] == "log" and args[-1] == "base000..HEAD":
            if "--oneline" in args:
                return "".join(f"{sha} {subj}\n" for sha, subj in gitcalls.commits)
            return "".join(f"{sha}\t{subj}\n" for sha, subj in gitcalls.commits)
        return ""

    monkeypatch.setattr(rp, "git", fake_git)
    monkeypatch.setattr(rp, "uncommitted", lambda copy: [])
    monkeypatch.setattr(rp, "push", lambda copy, o, r, b, **kw: gitcalls.append(("push", b)))
    monkeypatch.setattr(rp, "account_for_owner", lambda owner: "alice-dev")
    monkeypatch.setattr(rp, "_sleep", lambda s: gitcalls.append(("sleep", s)))
    monkeypatch.setattr(rp, "main_checkout", lambda copy: Path("/main"))
    monkeypatch.setattr(rp, "fix_git", lambda cwd, *a: gitcalls.removed.append((cwd, a)))
    return tmp_path, gitcalls, monkeypatch


def run(path, gh, mp):
    mp.setattr(rp, "gh_api", gh)
    rp.publish_review(path, dry_run=True)
    return rp.publish_review(path, dry_run=False)


def test_note_body_suffixes():
    assert rp.note_body(note(1, action="fix", fixed_in="abcdef123", fix_summary="did it"), "APPROVE", None).endswith(
        "**Fixed in `abcdef1`:** did it")
    assert rp.note_body(note(2), "APPROVE", 81).endswith("**Tracked in #81** (not blocking).")
    assert rp.note_body(note(3, sev="nit"), "APPROVE", None).endswith("_Optional, not blocking._")
    assert rp.note_body(note(4), "REQUEST_CHANGES", None) == "**[should-fix] T4**\n\nbody 4"


def test_full_flow_with_fixes_orders_push_issues_notes_resolve_verdict(env):
    tmp, gitcalls, mp = env
    gh = FakeGitHub()
    path = make_draft(tmp, [note(1, action="fix"), note(2), note(3, sev="nit")])
    out = run(path, gh, mp)
    order = [p for m, p, _ in gh.calls if m == "POST" and p != "graphql"]
    assert ("push", "bob-branch") in gitcalls
    assert order == ["repos/acme-corp/web-app/issues",
                     "repos/acme-corp/web-app/pulls/9/reviews",
                     "repos/acme-corp/web-app/pulls/9/reviews"]
    notes_review, verdict_review = gh.posts("/reviews")
    assert notes_review["event"] == "COMMENT" and notes_review["commit_id"] == "base000"
    assert len(notes_review["comments"]) == 3
    assert verdict_review["event"] == "APPROVE" and verdict_review["commit_id"] == "fff0002"
    assert verdict_review["body"] == "Sum." and "comments" not in verdict_review
    resolves = [b for m, p, b in gh.calls if p == "graphql" and "resolveReviewThread" in b["query"]]
    assert len(resolves) == 3
    assert out["verdict"] == "APPROVE" and out["resolved"] == 3
    issue = gh.posts("/issues")[0]
    assert issue["title"] == "T2" and "Follow-up to #9" in issue["body"]


def test_no_fixes_single_review_with_verdict(env):
    tmp, gitcalls, mp = env
    gh = FakeGitHub(author="carol-dev")
    path = make_draft(tmp, [note(1, sev="blocker"), note(2, sev="nit")], fix=False)
    out = run(path, gh, mp)
    reviews = gh.posts("/reviews")
    assert len(reviews) == 1 and reviews[0]["event"] == "REQUEST_CHANGES" and len(reviews[0]["comments"]) == 2
    assert gh.posts("/issues") == [] and not [g for g in gitcalls if g[0] == "push"]
    assert out["resolved"] == 0


def test_fix_on_non_allowed_author_is_refused_before_anything(env):
    tmp, gitcalls, mp = env
    gh = FakeGitHub(author="carol-dev")
    mp.setattr(rp, "gh_api", gh)
    path = make_draft(tmp, [note(1, action="fix")])
    with pytest.raises(ReviewError, match="only fixes PRs opened by"):
        rp.publish_review(path, dry_run=True)
    assert [c for c in gh.calls if c[0] == "POST"] == [] and gitcalls == []


def test_fix_on_fork_is_refused(env):
    tmp, gitcalls, mp = env
    gh = FakeGitHub(repo="someone/fork")
    mp.setattr(rp, "gh_api", gh)
    with pytest.raises(ReviewError, match="fork"):
        rp.publish_review(make_draft(tmp, [note(1, action="fix")]), dry_run=True)


def test_own_pr_is_refused(env):
    tmp, gitcalls, mp = env
    gh = FakeGitHub(author="alice-dev")
    mp.setattr(rp, "gh_api", gh)
    with pytest.raises(ReviewError, match="your PR"):
        rp.publish_review(make_draft(tmp, [note(1)], fix=False), dry_run=True)


def test_real_run_requires_dry_run(env):
    tmp, gitcalls, mp = env
    mp.setattr(rp, "gh_api", FakeGitHub())
    with pytest.raises(ReviewError, match="dry-run first"):
        rp.publish_review(make_draft(tmp, [note(1)], fix=False), dry_run=False)


def test_new_commits_by_author_stop_before_posting(env):
    tmp, gitcalls, mp = env
    gh = FakeGitHub(head="someoneelse")
    mp.setattr(rp, "gh_api", gh)
    path = make_draft(tmp, [note(1, action="fix")])
    with pytest.raises(ReviewError, match="new commits"):
        rp.publish_review(path, dry_run=True)
    assert [c for c in gh.calls if c[0] == "POST"] == []


def test_new_commits_error_names_review_publish(env):
    tmp, gitcalls, mp = env
    mp.setattr(rp, "gh_api", FakeGitHub(head="someoneelse"))
    with pytest.raises(ReviewError) as e:
        rp.publish_review(make_draft(tmp, [note(1, action="fix")]), dry_run=True)
    assert "review_publish.py" in str(e.value) and "PR #9" in str(e.value)


def test_fix_note_missing_commit_in_copy_is_refused(env):
    tmp, gitcalls, mp = env
    mp.setattr(rp, "gh_api", FakeGitHub())
    path = make_draft(tmp, [note(1, action="fix", fixed_in="0000000")])
    with pytest.raises(ReviewError, match="not one of the fix commits"):
        rp.publish_review(path, dry_run=True)


def test_resume_after_failure_between_notes_and_verdict(env):
    tmp, gitcalls, mp = env
    path = make_draft(tmp, [note(1, action="fix"), note(2)])
    failing = FakeGitHub(fail_on=lambda m, p, b: m == "POST" and p.endswith("/reviews") and b["event"] == "APPROVE")
    mp.setattr(rp, "gh_api", failing)
    rp.publish_review(path, dry_run=True)
    with pytest.raises(ReviewError):
        rp.publish_review(path, dry_run=False)
    flow = json.loads(open(path).read())["flow"]
    assert flow["pushed"] and flow["notes_review"] and flow["issues"] and not flow["verdict_review"]
    again = FakeGitHub(head="fff0002")
    again.posted_reviews = list(failing.posted_reviews)  # the notes review already exists on GitHub
    again.review_n = len(again.posted_reviews)
    mp.setattr(rp, "gh_api", again)
    out = rp.publish_review(path, dry_run=False)
    assert [b["event"] for b in again.posts("/reviews")] == ["APPROVE"]
    assert again.posts("/issues") == []
    assert len([g for g in gitcalls if g[0] == "push"]) == 1
    assert out["verdict_review"]


def test_approve_resolves_nits_too(env):
    tmp, gitcalls, mp = env
    gh = FakeGitHub(author="carol-dev")
    out = run(make_draft(tmp, [note(1, sev="nit")], fix=False), gh, mp)
    assert gh.posts("/reviews")[0]["event"] == "APPROVE" and out["resolved"] == 1


def resolves(gh):
    return [b["variables"]["id"] for m, p, b in gh.calls if p == "graphql" and "resolveReviewThread" in b["query"]]


def test_same_line_leave_note_is_not_resolved_with_the_fixed_one(env):
    tmp, gitcalls, mp = env
    gh = FakeGitHub()
    path = make_draft(tmp, [note(1, action="fix", line=11), note(2, sev="blocker", line=11)])
    out = run(path, gh, mp)
    assert out["verdict"] == "REQUEST_CHANGES"
    assert resolves(gh) == ["TH500"]  # the fixed note is comment 500; the blocker we left stays open


def test_outdated_comments_with_null_line_still_resolve(env):
    tmp, gitcalls, mp = env
    gh = FakeGitHub(outdated=True)
    out = run(make_draft(tmp, [note(1, action="fix"), note(2)]), gh, mp)
    assert out["resolved"] == 2 and len(resolves(gh)) == 2


def test_review_threads_are_paginated(env):
    tmp, gitcalls, mp = env
    gh = FakeGitHub(author="carol-dev", thread_page=1)
    path = make_draft(tmp, [note(i) for i in range(1, 4)], fix=False)
    out = run(path, gh, mp)
    assert out["resolved"] == 3  # comment 502 sits on the third page of one-thread pages
    assert len([1 for m, p, b in gh.calls if p == "graphql" and "reviewThreads" in b["query"]]) >= 3


def test_missing_thread_stops_before_the_verdict_review(env):
    tmp, gitcalls, mp = env
    gh = FakeGitHub(omit_comments=[500])
    path = make_draft(tmp, [note(1, action="fix"), note(2)])
    mp.setattr(rp, "gh_api", gh)
    rp.publish_review(path, dry_run=True)
    with pytest.raises(ReviewError, match=r"Could not resolve 1 of our threads.*Re-run review_publish.py"):
        rp.publish_review(path, dry_run=False)
    assert [b["event"] for b in gh.posts("/reviews")] == ["COMMENT"]
    assert not json.loads(open(path).read())["flow"]["verdict_review"]


def test_fix_path_lines_come_from_compare_at_the_pre_fix_head(env):
    tmp, gitcalls, mp = env
    gh = FakeGitHub()
    run(make_draft(tmp, [note(1, action="fix"), note(2)]), gh, mp)
    gets = [p for m, p, _ in gh.calls if m == "GET"]
    assert any("/compare/develop...base000" in g for g in gets)
    assert not any("/pulls/9/files" in g for g in gets)


def test_no_fix_path_still_uses_pull_files(env):
    tmp, gitcalls, mp = env
    gh = FakeGitHub(author="carol-dev")
    run(make_draft(tmp, [note(1)], fix=False), gh, mp)
    gets = [p for m, p, _ in gh.calls if m == "GET"]
    assert any("/pulls/9/files" in g for g in gets) and not any("/compare/" in g for g in gets)


@pytest.mark.parametrize("bad", [{"fixed_in": ""}, {"fix_summary": ""}, {"fixed_in": "  "}])
def test_fix_note_needs_commit_and_summary(env, bad):
    tmp, gitcalls, mp = env
    gh = FakeGitHub()
    mp.setattr(rp, "gh_api", gh)
    path = make_draft(tmp, [note(1, action="fix", **bad)])
    with pytest.raises(ReviewError, match="note 1"):
        rp.publish_review(path, dry_run=True)
    assert gitcalls == [] and [c for c in gh.calls if c[0] == "POST"] == []


def test_fix_note_without_fix_summary_key_is_refused(env):
    tmp, gitcalls, mp = env
    mp.setattr(rp, "gh_api", FakeGitHub())
    path = make_draft(tmp, [note(1, action="fix")])
    dr = json.loads(open(path).read())
    del dr["notes"][0]["fix_summary"]
    open(path, "w").write(json.dumps(dr))
    with pytest.raises(ReviewError, match="note 1 is a fix but has no fix_summary"):
        rp.publish_review(path, dry_run=True)
    assert gitcalls == []


@pytest.mark.parametrize("fixkey", [None, {"copy_path": ""}, {"branch": ""}, {}])
def test_fix_notes_need_copy_path_and_branch(env, fixkey):
    tmp, gitcalls, mp = env
    gh = FakeGitHub()
    mp.setattr(rp, "gh_api", gh)
    path = make_draft(tmp, [note(1, action="fix")])
    dr = json.loads(open(path).read())
    if fixkey is None:
        del dr["fix"]
    else:
        dr["fix"] = {**dr["fix"], **fixkey} if fixkey else {}
    open(path, "w").write(json.dumps(dr))
    with pytest.raises(ReviewError, match="fix"):
        rp.publish_review(path, dry_run=True)
    assert gitcalls == [] and [c for c in gh.calls if c[0] == "POST"] == []


def test_push_access_failure_names_review_publish(env):
    tmp, gitcalls, mp = env
    gh = FakeGitHub()
    path = make_draft(tmp, [note(1, action="fix")])
    mp.setattr(rp, "gh_api", gh)
    rp.publish_review(path, dry_run=True)

    def denied(cwd, *args):
        raise ReviewError("git push failed: remote: Permission denied")

    mp.setattr(pushlib, "git", denied)
    mp.setattr(rp, "push", pushlib.push)  # the real push, so the real message is checked
    with pytest.raises(ReviewError, match="re-run review_publish.py") as e:
        rp.publish_review(path, dry_run=False)
    assert "publish.py" not in str(e.value).replace("review_publish.py", "")


def test_save_is_atomic(tmp_path, monkeypatch):
    target = tmp_path / "x.json"
    replaced = []
    real = rp.os.replace
    monkeypatch.setattr(rp.os, "replace", lambda a, b: (replaced.append((a, b)), real(a, b))[1])
    rp._save(target, {"a": 1})
    assert len(replaced) == 1 and replaced[0][1] == target and replaced[0][0] != str(target)
    assert json.loads(target.read_text()) == {"a": 1}
    with pytest.raises(TypeError):
        rp._save(target, {"a": object()})
    assert json.loads(target.read_text()) == {"a": 1}
    assert [f.name for f in tmp_path.iterdir()] == ["x.json"]


# ---- final-review fix wave -------------------------------------------------------------------------------

def dry(path, gh, mp):
    mp.setattr(rp, "gh_api", gh)
    return rp.publish_review(path, dry_run=True)


def real(path, gh, mp):
    mp.setattr(rp, "gh_api", gh)
    return rp.publish_review(path, dry_run=False)


def no_writes(gh, gitcalls):
    return [c for c in gh.calls if c[0] == "POST" and c[1] != "graphql"] == [] and \
        not [g for g in gitcalls if g[0] == "push"]


# H1: every commit in the copy must be claimed

def test_unclaimed_commit_is_refused_on_dry_run(env):
    tmp, gitcalls, mp = env
    gitcalls.commits = [("fff0001", "Fix the thing"), ("abc1234def", "Oops extra change")]
    gh = FakeGitHub()
    path = make_draft(tmp, [note(1, action="fix")])
    with pytest.raises(ReviewError, match=r"commit abc1234 Oops extra change is not claimed by any fix note.*"
                                          r"Nothing was pushed or posted"):
        dry(path, gh, mp)
    assert no_writes(gh, gitcalls)


def test_unclaimed_commit_is_refused_on_the_real_run_too(env):
    tmp, gitcalls, mp = env
    gh = FakeGitHub()
    path = make_draft(tmp, [note(1, action="fix")])
    dry(path, gh, mp)  # fine while the copy only holds the claimed commit
    gitcalls.commits.append(("abc1234def", "Late extra change"))
    with pytest.raises(ReviewError, match="commit abc1234 Late extra change is not claimed"):
        real(path, gh, mp)
    assert no_writes(gh, gitcalls)


def test_revert_commit_needs_no_claim_and_dry_run_lists_commits(env):
    tmp, gitcalls, mp = env
    gitcalls.commits = [("bbb2222aa", 'Revert "Fix the other thing"'), ("fff0001", "Fix the thing")]
    out = dry(make_draft(tmp, [note(1, action="fix")]), FakeGitHub(), mp)
    assert out["commits"] == ['bbb2222aa Revert "Fix the other thing"', "fff0001 Fix the thing"]


def test_claim_matches_by_prefix_either_way(env):
    tmp, gitcalls, mp = env
    gitcalls.commits = [("fff0001abcdef", "Fix the thing")]
    dry(make_draft(tmp, [note(1, action="fix", fixed_in="fff0001")]), FakeGitHub(), mp)
    gitcalls.commits = [("fff0001", "Fix the thing")]
    dry(make_draft(tmp, [note(1, action="fix", fixed_in="fff0001abcdef")]), FakeGitHub(), mp)


def test_dry_run_without_fixes_has_no_commits_key(env):
    tmp, gitcalls, mp = env
    out = dry(make_draft(tmp, [note(1)], fix=False), FakeGitHub(author="carol-dev"), mp)
    assert "commits" not in out


# H2: APPROVE closes our earlier threads

def test_approve_resolves_our_earlier_unresolved_thread_before_the_verdict(env):
    tmp, gitcalls, mp = env
    gh = FakeGitHub(extra_threads=[(900, "alice-dev", False)])
    out = run(make_draft(tmp, [note(1, action="fix"), note(2)]), gh, mp)
    assert resolves(gh) == ["TH500", "TH501", "TH900"]
    assert out["resolved"] == 3
    last_resolve = max(i for i, (m, p, b) in enumerate(gh.calls)
                       if p == "graphql" and "resolveReviewThread" in b["query"])
    verdict = max(i for i, (m, p, b) in enumerate(gh.calls) if p.endswith("/reviews") and m == "POST")
    assert last_resolve < verdict


def test_earlier_threads_by_someone_else_or_already_resolved_are_left(env):
    tmp, gitcalls, mp = env
    gh = FakeGitHub(author="carol-dev", extra_threads=[(901, "bob-dev", False),
                                                           (902, "alice-dev", True)])
    out = run(make_draft(tmp, [note(1)], fix=False), gh, mp)
    assert resolves(gh) == ["TH500"] and out["resolved"] == 1


def test_request_changes_leaves_earlier_threads_alone(env):
    tmp, gitcalls, mp = env
    gh = FakeGitHub(author="carol-dev", extra_threads=[(900, "alice-dev", False)])
    out = run(make_draft(tmp, [note(1, sev="blocker")], fix=False), gh, mp)
    assert out["verdict"] == "REQUEST_CHANGES" and "TH900" not in resolves(gh)


def test_dry_run_counts_earlier_threads_only_on_approve(env):
    tmp, gitcalls, mp = env
    threads = [(900, "alice-dev", False), (901, "someone", False)]
    out = dry(make_draft(tmp, [note(1)], fix=False), FakeGitHub(author="carol-dev", extra_threads=threads), mp)
    assert out["earlier_threads"] == 1
    out = dry(make_draft(tmp, [note(1, sev="blocker")], fix=False),
              FakeGitHub(author="carol-dev", extra_threads=threads), mp)
    assert out["earlier_threads"] == 0


def test_dry_run_records_the_earlier_thread_ids_sorted(env):
    tmp, gitcalls, mp = env
    threads = [(910, "alice-dev", False), (900, "alice-dev", False), (901, "someone", False)]
    path = make_draft(tmp, [note(1)], fix=False)
    out = dry(path, FakeGitHub(author="carol-dev", extra_threads=threads), mp)
    assert out["earlier_threads"] == 2
    assert json.loads(open(path).read())["dry_run"]["earlier_threads"] == ["TH900", "TH910"]


def test_dry_run_records_no_earlier_threads_without_approve(env):
    tmp, gitcalls, mp = env
    path = make_draft(tmp, [note(1, sev="blocker")], fix=False)
    dry(path, FakeGitHub(author="carol-dev", extra_threads=[(900, "alice-dev", False)]), mp)
    assert json.loads(open(path).read())["dry_run"]["earlier_threads"] == []


def test_earlier_thread_that_appeared_after_the_dry_run_is_not_resolved(env):
    tmp, gitcalls, mp = env
    path = make_draft(tmp, [note(1)], fix=False)
    dry(path, FakeGitHub(author="carol-dev", extra_threads=[(900, "alice-dev", False)]), mp)
    later = FakeGitHub(author="carol-dev", extra_threads=[(900, "alice-dev", False), (901, "alice-dev", False)])
    out = real(path, later, mp)
    assert resolves(later) == ["TH500", "TH900"]
    assert out["earlier_threads_skipped"] == 1 and out["verdict"] == "APPROVE"


def test_recorded_earlier_thread_that_is_gone_by_the_real_run_is_not_resolved(env):
    tmp, gitcalls, mp = env
    path = make_draft(tmp, [note(1)], fix=False)
    dry(path, FakeGitHub(author="carol-dev", extra_threads=[(900, "alice-dev", False)]), mp)
    later = FakeGitHub(author="carol-dev", extra_threads=[(900, "alice-dev", True)])
    out = real(path, later, mp)
    assert resolves(later) == ["TH500"] and out["earlier_threads_skipped"] == 0


def test_recorded_earlier_threads_are_still_resolved_on_approve_with_fixes(env):
    tmp, gitcalls, mp = env
    gh = FakeGitHub(extra_threads=[(900, "alice-dev", False), (901, "alice-dev", False)])
    out = run(make_draft(tmp, [note(1, action="fix"), note(2)]), gh, mp)
    assert resolves(gh) == ["TH500", "TH501", "TH900", "TH901"]
    assert out["earlier_threads_skipped"] == 0


def test_record_without_earlier_threads_is_refused(env):
    tmp, gitcalls, mp = env
    gh = FakeGitHub(author="carol-dev")
    path = make_draft(tmp, [note(1)], fix=False)
    dry(path, gh, mp)

    def strip(dr):
        dr["dry_run"].pop("earlier_threads")

    _edit(path, strip)
    with pytest.raises(ReviewError, match="Run review_publish.py --dry-run first"):
        real(path, gh, mp)
    assert no_writes(gh, [])


# H3: issues turned off

def test_issues_disabled_refuses_before_any_push_or_post(env):
    tmp, gitcalls, mp = env
    path = make_draft(tmp, [note(1, action="fix"), note(2)])
    with pytest.raises(ReviewError, match=r"Issues are turned off on acme-corp/web-app.*Nothing was pushed"):
        dry(path, FakeGitHub(has_issues=False), mp)
    dry(path, FakeGitHub(), mp)  # a dry run is recorded with issues on
    off = FakeGitHub(has_issues=False)
    with pytest.raises(ReviewError, match="Issues are turned off"):
        real(path, off, mp)
    assert no_writes(off, gitcalls) and not json.loads(open(path).read()).get("flow", {}).get("pushed")


def test_issues_check_is_skipped_when_no_issue_will_be_filed(env):
    tmp, gitcalls, mp = env
    gh = FakeGitHub(has_issues=False)
    dry(make_draft(tmp, [note(1, action="fix")]), gh, mp)
    assert not [c for c in gh.calls if c[1] == "repos/acme-corp/web-app"]


# H4: thread matching

def posted_comment(i, body, line=10, side="RIGHT", path="a.py"):
    return {"id": 500 + i, "path": path, "line": line, "original_line": line, "side": side, "body": body}


def test_body_match_ignores_crlf_and_trailing_space():
    n = note(1)
    posted = [posted_comment(0, "**[should-fix] T1**\r\n\r\nbody 1  \r\n", line=11)]
    assert rp._match_comments([n], posted, lambda x: rp.note_body(x, "APPROVE", None)) == {1: 500}


def test_falls_back_to_position_when_the_body_differs():
    n = note(1)
    posted = [posted_comment(0, "something GitHub rewrote", line=11)]
    assert rp._match_comments([n], posted, lambda x: "wanted") == {1: 500}


def test_ambiguous_position_fallback_matches_nothing():
    n = note(1)
    posted = [posted_comment(0, "rewritten a", line=11), posted_comment(1, "rewritten b", line=11)]
    assert rp._match_comments([n], posted, lambda x: "wanted") == {}


def test_fallback_never_takes_a_comment_that_belongs_to_a_note_we_are_not_resolving():
    wanted, other = note(1, line=1), note(2, sev="blocker", line=1)
    posted = [posted_comment(1, "other body", line=1)]  # ours was lost; only the other note's comment is left
    got = rp._match_comments([wanted], posted, lambda x: "wanted" if x["id"] == 1 else "other body", others=[other])
    assert got == {}


def test_flow_resolves_via_position_fallback(env):
    tmp, gitcalls, mp = env
    gh = FakeGitHub(author="carol-dev", mangle=lambda b: b.replace("body", "reworded"))
    out = run(make_draft(tmp, [note(1), note(2)], fix=False), gh, mp)
    assert out["resolved"] == 2


def test_flow_ambiguous_fallback_still_errors(env):
    tmp, gitcalls, mp = env
    gh = FakeGitHub(author="carol-dev", mangle=lambda b: "reworded")
    path = make_draft(tmp, [note(1, line=11), note(2, line=11)], fix=False)
    with pytest.raises(ReviewError, match="Could not resolve 2 of our threads"):
        run(path, gh, mp)


# H5: the dry run is bound to the whole plan

def _edit(path, fn):
    dr = json.loads(open(path).read())
    fn(dr)
    open(path, "w").write(json.dumps(dr))


@pytest.mark.parametrize("change", [
    lambda dr: dr["notes"][1].update(keep=False),
    lambda dr: dr["notes"][1].update(severity="blocker"),
    lambda dr: dr["notes"][1].update(body="reworded"),
    lambda dr: dr["notes"][1].update(title="New title"),
    lambda dr: dr["notes"][0].update(fix_summary="different summary"),
    lambda dr: dr["notes"][0].update(action="leave"),
    lambda dr: dr.update(summary="A different summary."),
])
def test_any_plan_change_after_the_dry_run_refuses_the_real_run(env, change):
    tmp, gitcalls, mp = env
    gh = FakeGitHub()
    path = make_draft(tmp, [note(1, action="fix"), note(2)])
    dry(path, gh, mp)
    _edit(path, change)
    with pytest.raises(ReviewError, match="Run review_publish.py --dry-run first"):
        real(path, gh, mp)
    assert no_writes(gh, gitcalls)


@pytest.mark.parametrize("change", [
    lambda dr: dr["fix"].update(branch="other-branch"),
    lambda dr: dr["fix"].update(copy_path="/elsewhere/copy"),
])
def test_fix_target_change_after_the_dry_run_refuses_the_real_run(env, change):
    tmp, gitcalls, mp = env
    gh = FakeGitHub()
    path = make_draft(tmp, [note(1, action="fix")])
    dry(path, gh, mp)
    _edit(path, change)
    with pytest.raises(ReviewError, match="Run review_publish.py --dry-run first"):
        real(path, gh, mp)
    assert no_writes(gh, gitcalls)


def test_dry_run_names_the_copy_it_will_remove(env):
    tmp, gitcalls, mp = env
    out = dry(make_draft(tmp, [note(1, action="fix")]), FakeGitHub(), mp)
    assert out["copy_removed_after"] == str(tmp / "copy")
    out = dry(make_draft(tmp, [note(1)], fix=False), FakeGitHub(author="carol-dev"), mp)
    assert "copy_removed_after" not in out


def test_unchanged_draft_proceeds_and_dry_run_records_a_plan_fingerprint(env):
    tmp, gitcalls, mp = env
    gh = FakeGitHub()
    path = make_draft(tmp, [note(1, action="fix"), note(2)])
    dry(path, gh, mp)
    rec = json.loads(open(path).read())["dry_run"]
    assert len(rec["plan"]) == 64 and rec["head"] == "fff0002"
    assert real(path, gh, mp)["verdict"] == "APPROVE"


def test_draft_py_edit_after_the_dry_run_also_refuses(env):
    tmp, gitcalls, mp = env
    gh = FakeGitHub(author="carol-dev")
    path = make_draft(tmp, [note(1), note(2)], fix=False)
    dry(path, gh, mp)
    d.drop(path, [2])
    with pytest.raises(ReviewError, match="dry-run first"):
        real(path, gh, mp)


# H7

def test_dry_run_counts_only_pinned_notes(env):
    tmp, gitcalls, mp = env
    notes = [note(1, action="fix"), note(2), note(3, line=500)]  # line 500 is outside the diff
    out = dry(make_draft(tmp, notes), FakeGitHub(), mp)
    assert out["reviews"] == [{"event": "COMMENT", "comments": 2}, {"event": "APPROVE", "comments": 0}]
    assert out["resolve"] == 2
    out = dry(make_draft(tmp, [note(1), note(2, line=500)], fix=False), FakeGitHub(author="carol-dev"), mp)
    assert out["reviews"] == [{"event": "APPROVE", "comments": 1}] and out["resolve"] == 1


def test_compare_is_fetched_once_and_warns_at_300_files(env):
    tmp, gitcalls, mp = env
    gh = FakeGitHub()
    out = dry(make_draft(tmp, [note(1, action="fix")]), gh, mp)
    assert len([1 for m, p, b in gh.calls if "/compare/" in p]) == 1 and "warning" not in out
    big = FakeGitHub(compare_files=300)
    out = dry(make_draft(tmp, [note(1, action="fix")]), big, mp)
    assert out["warning"] == "PR has 300+ files; some notes may move to the summary"
    assert len([1 for m, p, b in big.calls if "/compare/" in p]) == 1


def test_issue_title_fallback_is_the_first_line_max_80():
    n = note(1, title="", body="x" * 100 + "\nsecond line")
    assert rp.issue_payload(n, 9, "u")["title"] == "x" * 80
    n = note(1, title="", body="short first\nsecond line")
    assert rp.issue_payload(n, 9, "u")["title"] == "short first"
    assert rp.issue_payload(note(1), 9, "u")["title"] == "T1"


def test_copy_is_removed_after_a_successful_run_with_fixes(env):
    tmp, gitcalls, mp = env
    out = run(make_draft(tmp, [note(1, action="fix")]), FakeGitHub(), mp)
    assert out["copy_removed"] is True
    assert gitcalls.removed == [("/main", ("worktree", "remove", "--force", str(tmp / "copy")))]


def test_copy_removal_failure_is_not_an_error(env):
    tmp, gitcalls, mp = env

    def broken(cwd, *a):
        raise ReviewError("already gone")

    mp.setattr(rp, "fix_git", broken)
    out = run(make_draft(tmp, [note(1, action="fix")]), FakeGitHub(), mp)
    assert out["copy_removed"] is False and out["verdict"] == "APPROVE"


def test_no_copy_removal_without_fixes(env):
    tmp, gitcalls, mp = env
    out = run(make_draft(tmp, [note(1)], fix=False), FakeGitHub(author="carol-dev"), mp)
    assert "copy_removed" not in out and gitcalls.removed == []


def test_waits_for_github_to_register_the_push_before_the_verdict(env):
    tmp, gitcalls, mp = env
    gh = FakeGitHub(registers_push_after=2)
    out = run(make_draft(tmp, [note(1, action="fix")]), gh, mp)
    assert out["verdict_review"] and len([g for g in gitcalls if g[0] == "sleep"]) == 2


def test_push_never_registering_stops_before_the_verdict_and_rerun_is_safe(env):
    tmp, gitcalls, mp = env
    gh = FakeGitHub(registers_push_after=99)
    path = make_draft(tmp, [note(1, action="fix")])
    mp.setattr(rp, "gh_api", gh)
    rp.publish_review(path, dry_run=True)
    with pytest.raises(ReviewError, match=r"GitHub hasn't registered the push yet; re-run review_publish.py in a minute"):
        rp.publish_review(path, dry_run=False)
    assert [b["event"] for b in gh.posts("/reviews")] == ["COMMENT"]
    assert len([g for g in gitcalls if g[0] == "sleep"]) == 4
    again = FakeGitHub(head="fff0002")
    again.posted_reviews = list(gh.posted_reviews)
    again.review_n = len(again.posted_reviews)
    mp.setattr(rp, "gh_api", again)
    assert rp.publish_review(path, dry_run=False)["verdict_review"]
    assert len([g for g in gitcalls if g[0] == "push"]) == 1


# a fix that went back to leave is undone by a revert commit; the pair needs no claim

def test_reverted_fix_commit_counts_as_claimed(env):
    tmp, gitcalls, mp = env
    gitcalls.commits = [("bbb2222aa", 'Revert "Fix the thing"'), ("ccc3333dd", "Fix other"),
                        ("fff0001abc", "Fix the thing")]
    gitcalls.messages = {"bbb2222aa": 'Revert "Fix the thing"\n\nThis reverts commit fff0001abc.\n'}
    # note 1 went back to leave (no fixed_in); note 2 still fixes ccc3333
    out = dry(make_draft(tmp, [note(1, action="leave"), note(2, action="fix", fixed_in="ccc3333")]),
              FakeGitHub(), mp)
    assert out["dry_run"] and len(out["commits"]) == 3


def test_reverted_commit_claim_matches_by_prefix_either_way(env):
    tmp, gitcalls, mp = env
    gitcalls.commits = [("bbb2222aa", 'Revert "x"'), ("ccc3333", "Fix other"), ("fff0001", "Fix the thing")]
    gitcalls.messages = {"bbb2222aa": 'Revert "x"\n\nThis reverts commit fff0001abcdef0123.\n'}
    dry(make_draft(tmp, [note(2, action="fix", fixed_in="ccc3333")]), FakeGitHub(), mp)


def test_revert_of_something_outside_the_range_claims_nothing_else(env):
    tmp, gitcalls, mp = env
    gitcalls.commits = [("bbb2222aa", 'Revert "Old"'), ("abc1234def", "Oops extra change"),
                        ("fff0001", "Fix the thing")]
    gitcalls.messages = {"bbb2222aa": 'Revert "Old"\n\nThis reverts commit 9999999999.\n'}
    gh = FakeGitHub()
    with pytest.raises(ReviewError, match="commit abc1234 Oops extra change is not claimed"):
        dry(make_draft(tmp, [note(1, action="fix")]), gh, mp)
    assert no_writes(gh, gitcalls)


def test_revert_without_a_reverts_line_claims_nothing(env):
    tmp, gitcalls, mp = env
    gitcalls.commits = [("bbb2222aa", 'Revert "Fix the thing"'), ("ccc3333", "Fix other"),
                        ("fff0001abc", "Fix the thing")]
    with pytest.raises(ReviewError, match="commit fff0001 Fix the thing is not claimed"):
        dry(make_draft(tmp, [note(2, action="fix", fixed_in="ccc3333")]), FakeGitHub(), mp)
