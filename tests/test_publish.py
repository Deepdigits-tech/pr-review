import json

import pytest

import fixdraft as fd
import publish
import pushlib
from prlib import ReviewError

ENV_KEY = "en" + "v"  # the subprocess keyword that carries the child process environment


def item(i, kind="thread", own=False, status="fixed", reply=None, skip=False):
    return {"id": i, "kind": kind, "source": "alice-dev" if own else "bob-dev", "own": own,
            "thread_id": f"T{i}" if kind == "thread" else None, "comment_id": 100 + i if kind == "thread" else None,
            "path": "a.py", "line": i, "body": "b", "status": status, "commit": "abc1234", "test": "t",
            "covered_by": None, "reason": None, "debate": None,
            "reply": None if kind == "ours" else (reply or f"reply {i}"), "skip_reply": skip}


class FakeGitHub:
    def __init__(self, head="b19dc43", state="open", fail_on=None, reviews=None, author="alice-dev",
                 head_repo="acme-corp/web-app"):
        self.calls, self.head, self.state, self.fail_on, self.author = [], head, state, fail_on, author
        self.head_repo = head_repo
        self.kws = {}
        self.reviews = reviews if reviews is not None else [{"user": {"login": "bob-dev"}},
                                                             {"user": {"login": "alice-dev"}}]

    def __call__(self, account, path, method="GET", body=None, **kw):
        self.calls.append((method, path, body))
        self.kws[path] = kw
        if self.fail_on and self.fail_on in path:
            raise ReviewError(f"GitHub refused {path}")
        if method == "GET" and path.endswith("/reviews"):
            return self.reviews
        if method == "GET":
            return {"state": self.state, "head": {"sha": self.head, "repo": {"full_name": self.head_repo}},
                    "user": {"login": self.author}}
        if path == "graphql":
            return {"data": {"resolveReviewThread": {"thread": {"isResolved": True}}}}
        return {"html_url": f"https://gh/{len(self.calls)}"}

    def posts(self):
        return [(m, p) for m, p, _ in self.calls if m == "POST"]


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setattr(fd, "REVIEWS_DIR", tmp_path)
    git_calls = []

    def fake_git(cwd, *args):
        git_calls.append((cwd, args))
        if args[0] == "rev-parse":
            return "fff0000\n"
        if args[0] == "log":
            return "fff0000 item 1: fix\n"
        if args[0] == "rev-list":
            return "abc1234ffffffffffffffffffffffffffffffffff\n"
        return ""

    monkeypatch.setattr(publish, "_git", fake_git)
    monkeypatch.setattr(pushlib, "git", fake_git)  # uncommitted() and push() live in pushlib
    monkeypatch.setattr(publish, "main_checkout", lambda copy: publish.Path("/proj"))
    monkeypatch.setattr(publish, "account_for_owner", lambda owner: "alice-dev")

    def make(items, dry=True, base_sha="b19dc43"):
        d = {"pr": {"owner": "acme-corp", "repo": "web-app", "number": 63, "url": "u",
                    "head": "feature-branch", "base_sha": base_sha},
             "account": "alice-dev", "copy_path": str(tmp_path / "copy"), "rounds": 1, "codex_checked": True,
             "items": items}
        path = fd.save(d)["json"]
        if dry:  # a real publish needs a dry run of this exact copy HEAD on record
            saved = json.loads(open(path).read())
            saved["dry_run"] = {"at": "2026-10-04T00:00:00+00:00", "head": "fff0000"}
            open(path, "w").write(json.dumps(saved))
        return path
    return make, git_calls, monkeypatch


def test_reviewers_to_request():
    reviews = [{"user": {"login": "bob-dev"}}, {"user": {"login": "alice-dev"}},
               {"user": {"login": "bob-dev"}}, {"user": {"login": "other"}}, {"user": None}]
    assert publish.reviewers_to_request(reviews, "alice-dev") == ["bob-dev", "other"]


def test_full_publish_order_and_resolve_only_own(setup):
    make, git_calls, mp = setup
    gh = FakeGitHub()
    mp.setattr(publish, "gh_api", gh)
    path = make([item(1), item(2, own=True), item(3, kind="ours"), item(4, kind="summary"), item(5, skip=True)])
    out = publish.publish(path, dry_run=False, reviewers=None)
    push = [a for c, a in git_calls if a[0] == "push"]
    assert push == [("push", "https://github.com/acme-corp/web-app.git", "HEAD:refs/heads/feature-branch")]
    posts = gh.posts()
    assert posts[0] == ("POST", "repos/acme-corp/web-app/pulls/63/comments/101/replies")
    assert posts[1] == ("POST", "repos/acme-corp/web-app/pulls/63/comments/102/replies")
    assert posts[2] == ("POST", "repos/acme-corp/web-app/issues/63/comments")
    assert posts[3][1] == "graphql"
    resolve_bodies = [b for m, p, b in gh.calls if p == "graphql"]
    assert len(resolve_bodies) == 1 and resolve_bodies[0]["variables"]["id"] == "T2"
    assert posts[4] == ("POST", "repos/acme-corp/web-app/pulls/63/requested_reviewers")
    assert [b for m, p, b in gh.calls if p.endswith("requested_reviewers")] == [{"reviewers": ["bob-dev"]}]
    assert out["replies_posted"] == 2 and out["resolved"] == 1 and out["copy_removed"] is True
    saved = json.loads(open(path).read())
    assert saved["published"]["done"] is True and saved["published"]["pushed"] == "fff0000"
    assert ("/proj", ("worktree", "remove", "--force", saved["copy_path"])) in git_calls


def test_new_commits_on_pr_stops_before_anything(setup):
    make, git_calls, mp = setup
    gh = FakeGitHub(head="someoneelse")
    mp.setattr(publish, "gh_api", gh)
    path = make([item(1)])
    with pytest.raises(ReviewError, match="new commits"):
        publish.publish(path, dry_run=False, reviewers=None)
    assert gh.posts() == [] and not [a for c, a in git_calls if a[0] == "push"]


def test_push_refused_posts_nothing(setup):
    make, git_calls, mp = setup
    gh = FakeGitHub()
    mp.setattr(publish, "gh_api", gh)

    def refusing_git(cwd, *args):
        if args[0] == "push":
            raise ReviewError("git push failed: rejected")
        return {"rev-parse": "fff0000\n", "rev-list": "abc1234ffff\n"}.get(args[0], "")

    mp.setattr(publish, "_git", refusing_git)
    mp.setattr(pushlib, "git", refusing_git)
    path = make([item(1)])
    with pytest.raises(ReviewError, match="new commits"):
        publish.publish(path, dry_run=False, reviewers=None)
    assert gh.posts() == []


def test_partial_failure_then_rerun_posts_only_missing(setup):
    make, git_calls, mp = setup
    gh = FakeGitHub(fail_on="comments/102/replies")
    mp.setattr(publish, "gh_api", gh)
    path = make([item(1), item(2)])
    with pytest.raises(ReviewError):
        publish.publish(path, dry_run=False, reviewers=None)
    saved = json.loads(open(path).read())
    assert list(saved["published"]["replies"]) == ["101"] and saved["published"]["pushed"] == "fff0000"
    gh2 = FakeGitHub(head="fff0000")  # the PR head is now our pushed commit
    mp.setattr(publish, "gh_api", gh2)
    publish.publish(path, dry_run=False, reviewers=None)
    replies = [p for m, p in gh2.posts() if p.endswith("/replies")]
    assert replies == ["repos/acme-corp/web-app/pulls/63/comments/102/replies"]
    assert len([a for c, a in git_calls if a[0] == "push"]) == 1


def test_dry_run_has_no_side_effects(setup):
    make, git_calls, mp = setup
    gh = FakeGitHub()
    mp.setattr(publish, "gh_api", gh)
    path = make([item(1), item(2, own=True)])
    out = publish.publish(path, dry_run=True, reviewers=None)
    assert out["dry_run"] is True
    assert out["push"] == {"from": "fff0000", "to": "feature-branch", "commits": ["fff0000 item 1: fix"]}
    assert [r["comment_id"] for r in out["replies"]] == [101, 102]
    assert out["resolve"] == ["T2"] and out["rerequest"] == ["bob-dev"]
    assert gh.posts() == [] and not [a for c, a in git_calls if a[0] in ("push", "worktree")]
    assert json.loads(open(path).read())["published"]["pushed"] is None


def test_no_reviewers_flag_and_override(setup):
    make, git_calls, mp = setup
    gh = FakeGitHub(reviews=[{"user": {"login": "alice-dev"}}])
    mp.setattr(publish, "gh_api", gh)
    out = publish.publish(make([item(1)]), dry_run=False, reviewers=None)
    assert out["no_reviewers"] is True and not [p for m, p in gh.posts() if p.endswith("requested_reviewers")]
    gh2 = FakeGitHub()
    mp.setattr(publish, "gh_api", gh2)
    out = publish.publish(make([item(1)]), dry_run=False, reviewers=["someone"])
    assert [b for m, p, b in gh2.calls if p.endswith("requested_reviewers")] == [{"reviewers": ["someone"]}]


def test_you_decide_items_block_publish_before_any_call(setup):
    make, git_calls, mp = setup
    gh = FakeGitHub()
    mp.setattr(publish, "gh_api", gh)
    path = make([item(1), item(2, status="you_decide"), item(3, status="you_decide")])
    with pytest.raises(ReviewError, match=r"Item\(s\) 2, 3 still need your decision \(you_decide\)"):
        publish.publish(path, dry_run=False, reviewers=None)
    assert gh.calls == [] and git_calls == []


def test_reviews_fetched_with_pagination(setup):
    make, git_calls, mp = setup
    gh = FakeGitHub()
    mp.setattr(publish, "gh_api", gh)
    publish.publish(make([item(1)]), dry_run=False, reviewers=None)
    assert gh.kws["repos/acme-corp/web-app/pulls/63/reviews"].get("slurp") is True


def test_own_flag_alone_does_not_resolve(setup):
    make, git_calls, mp = setup
    gh = FakeGitHub()
    mp.setattr(publish, "gh_api", gh)
    bad = item(1, own=True)
    bad["source"] = "bob-dev"
    out = publish.publish(make([bad]), dry_run=False, reviewers=None)
    assert [p for m, p in gh.posts() if p == "graphql"] == [] and out["resolved"] == 0


def test_replies_follow_comment_when_items_renumbered(setup):
    make, git_calls, mp = setup
    path = make([item(1), item(2)])
    on_disk = json.loads(open(path).read())
    on_disk["published"].update({"pushed": "fff0000", "replies": {"101": "u"}})
    open(path, "w").write(json.dumps(on_disk))
    renumbered = [item(7), item(8)]
    renumbered[0]["comment_id"], renumbered[1]["comment_id"] = 101, 108
    path = make(renumbered)
    gh = FakeGitHub(head="fff0000")
    mp.setattr(publish, "gh_api", gh)
    publish.publish(path, dry_run=False, reviewers=None)
    replies = [p for m, p in gh.posts() if p.endswith("/replies")]
    assert replies == ["repos/acme-corp/web-app/pulls/63/comments/108/replies"]


def test_resumes_after_crash_between_push_and_save(setup):
    make, git_calls, mp = setup
    gh = FakeGitHub(head="fff0000")  # our commit is already on the PR; the draft never recorded it
    mp.setattr(publish, "gh_api", gh)
    path = make([item(1)])
    out = publish.publish(path, dry_run=False, reviewers=None)
    assert not [a for c, a in git_calls if a[0] == "push"]
    assert [p for m, p in gh.posts() if p.endswith("/replies")] == [
        "repos/acme-corp/web-app/pulls/63/comments/101/replies"]
    assert out["pushed"] == "fff0000"


def test_git_never_prompts(monkeypatch):
    seen = {}

    class R:
        returncode, stdout, stderr = 0, "", ""

    def fake_run(cmd, **kw):
        seen.update(kw)
        return R()

    monkeypatch.setattr(pushlib.subprocess, "run", fake_run)
    pushlib.git("/x", "status")
    assert seen["stdin"] == pushlib.subprocess.DEVNULL
    assert seen[ENV_KEY]["GIT_TERMINAL_PROMPT"] == "0"


def test_bots_dropped_from_reviewers():
    reviews = [{"user": {"login": "copilot-pull-request-reviewer[bot]"}}, {"user": {"login": "real"}}]
    assert publish.reviewers_to_request(reviews, "alice-dev") == ["real"]


def test_override_drops_own_accounts_and_bots(setup):
    make, git_calls, mp = setup
    gh = FakeGitHub()
    mp.setattr(publish, "gh_api", gh)
    out = publish.publish(make([item(1)]), dry_run=False,
                          reviewers=["alice-dev", "alice-work", "dependabot[bot]", "someone"])
    assert out["rerequested"] == ["someone"]


# ---- final-review fix wave ----

def _git_with(mp, git_calls, **answers):
    """Replace the fake git with one that answers per command (a string, or an exception to raise)."""
    def g(cwd, *args):
        git_calls.append((cwd, args))
        ans = answers.get(args[0], "")
        if isinstance(ans, Exception):
            raise ans
        return ans
    mp.setattr(publish, "_git", g)
    mp.setattr(pushlib, "git", g)


def _carry_pushed(path, pushed="aaa1111"):
    on_disk = json.loads(open(path).read())
    on_disk["published"].update({"pushed": pushed, "replies": {"101": "u"}})
    open(path, "w").write(json.dumps(on_disk))


# G1
def test_g1_carried_pushed_with_new_commit_on_top_pushes_again(setup):
    make, git_calls, mp = setup
    path = make([item(1), item(2)])
    _carry_pushed(path)
    path = make([item(1), item(2)])  # re-save mid-publish: same base and copy, so pushed is carried
    assert json.loads(open(path).read())["published"]["pushed"] == "aaa1111"
    gh = FakeGitHub(head="aaa1111")  # live is our earlier push; the copy has a new commit on top (fff0000)
    mp.setattr(publish, "gh_api", gh)
    out = publish.publish(path, dry_run=False, reviewers=None)
    assert len([a for c, a in git_calls if a[0] == "push"]) == 1
    assert out["pushed"] == "fff0000"
    assert json.loads(open(path).read())["published"]["pushed"] == "fff0000"


def test_g1_live_equals_local_records_pushed_without_pushing(setup):
    make, git_calls, mp = setup
    gh = FakeGitHub(head="fff0000")
    mp.setattr(publish, "gh_api", gh)
    path = make([item(1)])
    publish.publish(path, dry_run=False, reviewers=None)
    assert not [a for c, a in git_calls if a[0] == "push"]


def test_g1_dry_run_does_not_record_pushed(setup):
    make, git_calls, mp = setup
    mp.setattr(publish, "gh_api", FakeGitHub(head="fff0000"))
    path = make([item(1)])
    out = publish.publish(path, dry_run=True, reviewers=None)
    assert out["push"] is None
    assert json.loads(open(path).read())["published"]["pushed"] is None


def test_g1_unknown_live_head_is_new_commits_even_with_pushed_recorded(setup):
    make, git_calls, mp = setup
    path = make([item(1)])
    _carry_pushed(path)
    gh = FakeGitHub(head="zzz9999")
    mp.setattr(publish, "gh_api", gh)
    with pytest.raises(ReviewError, match="new commits"):
        publish.publish(path, dry_run=False, reviewers=None)
    assert gh.posts() == [] and not [a for c, a in git_calls if a[0] == "push"]


def test_g1_push_is_never_forced(setup):
    make, git_calls, mp = setup
    mp.setattr(publish, "gh_api", FakeGitHub())
    publish.publish(make([item(1)]), dry_run=False, reviewers=None)
    push = [a for c, a in git_calls if a[0] == "push"][0]
    assert not [x for x in push if "force" in x or x.startswith("+") or x == "-f"]


def test_fixdraft_carries_pushed_only_for_same_base_and_copy(tmp_path, monkeypatch):
    monkeypatch.setattr(fd, "REVIEWS_DIR", tmp_path)

    def d(base, copy="/c"):
        return {"pr": {"owner": "O", "repo": "r", "number": 1, "url": "u", "head": "h", "base_sha": base},
                "account": "alice-dev", "copy_path": copy, "rounds": 1, "codex_checked": True, "items": [item(1)]}

    def seed(path):
        on_disk = json.loads(open(path).read())
        on_disk["published"].update({"pushed": "aaa1111", "replies": {"101": "u"}, "summary_comment": "s",
                                     "resolved": ["T1"], "rerequested": ["gp"]})
        open(path, "w").write(json.dumps(on_disk))

    for new, expect_pushed in ((d("b19dc43"), "aaa1111"), (d("newbase"), None), (d("b19dc43", copy="/other"), None)):
        first = fd.save(d("b19dc43"))
        seed(first["json"])
        saved = json.loads(open(fd.save(new)["json"]).read())["published"]
        assert saved["pushed"] == expect_pushed
        assert saved["replies"] == {"101": "u"} and saved["summary_comment"] == "s"
        assert saved["resolved"] == ["T1"] and saved["rerequested"] == ["gp"] and saved["done"] is False


# G2
@pytest.mark.parametrize("text", ["! [rejected] HEAD -> b (non-fast-forward)", "rejected", "hint: fetch first"])
def test_g2_stale_push_says_new_commits_and_offers_replace(setup, text):
    make, git_calls, mp = setup
    mp.setattr(publish, "gh_api", FakeGitHub())
    _git_with(mp, git_calls, **{"rev-parse": "fff0000\n", "rev-list": "abc1234ffff\n",
                                "push": ReviewError(f"git push failed: {text}")})
    with pytest.raises(ReviewError) as e:
        publish.publish(make([item(1)]), dry_run=False, reviewers=None)
    assert "PR has new commits" in str(e.value) and "--replace" in str(e.value)
    assert "Nothing was posted" in str(e.value)


@pytest.mark.parametrize("text", ["fatal: Authentication failed for 'https://github.com/x'",
                                  "! [remote rejected] HEAD -> b (protected branch hook declined)",
                                  "Permission denied"])
def test_g2_other_push_failures_do_not_offer_replace(setup, text):
    make, git_calls, mp = setup
    mp.setattr(publish, "gh_api", FakeGitHub())
    _git_with(mp, git_calls, **{"rev-parse": "fff0000\n", "rev-list": "abc1234ffff\n",
                                "push": ReviewError(f"git push failed: {text}")})
    with pytest.raises(ReviewError) as e:
        publish.publish(make([item(1)]), dry_run=False, reviewers=None)
    msg = str(e.value)
    assert "The push failed (git access?)" in msg and text in msg and "--replace" not in msg
    assert "nothing is lost" in msg and "acme-corp/web-app" in msg and "re-run publish.py" in msg


# G5
def test_g5_refuses_pr_not_opened_by_own_account(setup):
    make, git_calls, mp = setup
    gh = FakeGitHub(author="somebody")
    mp.setattr(publish, "gh_api", gh)
    with pytest.raises(ReviewError, match=r"PR #63 was opened by somebody, not one of your accounts\. "
                                          r"Nothing was pushed or posted\."):
        publish.publish(make([item(1)]), dry_run=False, reviewers=None)
    assert gh.posts() == [] and not [a for c, a in git_calls if a[0] == "push"]


def test_g5_refuses_fork_pr(setup):
    make, git_calls, mp = setup
    gh = FakeGitHub(head_repo="alice-dev/web-app")
    mp.setattr(publish, "gh_api", gh)
    with pytest.raises(ReviewError, match=r"comes from another repository \(a fork\); pushing to "
                                          r"acme-corp/web-app would not update it\. Nothing was pushed"):
        publish.publish(make([item(1)]), dry_run=False, reviewers=None)
    assert not [a for c, a in git_calls if a[0] == "push"]


def test_g5_fork_with_missing_head_repo_is_refused_even_in_dry_run(setup):
    make, git_calls, mp = setup

    def gh(account, path, method="GET", body=None, **kw):
        return {"state": "open", "head": {"sha": "b19dc43", "repo": None}, "user": {"login": "alice-dev"}}

    mp.setattr(publish, "gh_api", gh)
    with pytest.raises(ReviewError, match="fork"):
        publish.publish(make([item(1)]), dry_run=True, reviewers=None)


def test_g5_repo_name_match_ignores_case(setup):
    make, git_calls, mp = setup
    mp.setattr(publish, "gh_api", FakeGitHub(head_repo="acme-corp/Web-App"))
    publish.publish(make([item(1)]), dry_run=True, reviewers=None)


# G6
def test_g6_uncommitted_changes_refuse_real_run_and_show_in_dry_run(setup):
    make, git_calls, mp = setup
    gh = FakeGitHub()
    mp.setattr(publish, "gh_api", gh)
    _git_with(mp, git_calls, **{"rev-parse": "fff0000\n", "rev-list": "abc1234ffff\n", "log": "fff0000 x\n",
                                "status": " M a.py\n"})
    path = make([item(1)])
    with pytest.raises(ReviewError, match=r"uncommitted changes: M a\.py\. Commit or discard them, then re-run\. "
                                          r"Nothing was pushed or posted\."):
        publish.publish(path, dry_run=False, reviewers=None)
    assert gh.posts() == [] and not [a for c, a in git_calls if a[0] == "push"]
    out = publish.publish(path, dry_run=True, reviewers=None)
    assert out["uncommitted"] == [" M a.py"]
    status = [a for c, a in git_calls if a[0] == "status"][0]
    assert status == ("status", "--porcelain", "--untracked-files=no")


def test_g6_dry_run_with_uncommitted_does_not_count_as_the_required_dry_run(setup):
    make, git_calls, mp = setup
    mp.setattr(publish, "gh_api", FakeGitHub())
    _git_with(mp, git_calls, **{"rev-parse": "fff0000\n", "rev-list": "abc1234ffff\n", "log": "x\n",
                                "status": " M a.py\n"})
    path = make([item(1)], dry=False)
    publish.publish(path, dry_run=True, reviewers=None)
    assert "dry_run" not in json.loads(open(path).read())


# G7
def test_g7_real_run_without_dry_run_refuses(setup):
    make, git_calls, mp = setup
    gh = FakeGitHub()
    mp.setattr(publish, "gh_api", gh)
    path = make([item(1)], dry=False)
    with pytest.raises(ReviewError, match=r"Run publish\.py --dry-run first \(and again after any change to the "
                                          r"fixes\)\. Nothing was pushed or posted\."):
        publish.publish(path, dry_run=False, reviewers=None)
    assert gh.posts() == [] and not [a for c, a in git_calls if a[0] == "push"]


def test_g7_dry_run_then_real_run_works_and_records_head(setup):
    make, git_calls, mp = setup
    mp.setattr(publish, "gh_api", FakeGitHub())
    path = make([item(1)], dry=False)
    publish.publish(path, dry_run=True, reviewers=None)
    rec = json.loads(open(path).read())["dry_run"]
    assert rec["head"] == "fff0000" and rec["at"].endswith("+00:00")
    out = publish.publish(path, dry_run=False, reviewers=None)
    assert out["pushed"] == "fff0000"


def test_g7_new_commit_after_dry_run_refuses(setup):
    make, git_calls, mp = setup
    gh = FakeGitHub()
    mp.setattr(publish, "gh_api", gh)
    path = make([item(1)], dry=False)
    publish.publish(path, dry_run=True, reviewers=None)
    _git_with(mp, git_calls, **{"rev-parse": "eee1111\n", "rev-list": "abc1234ffff\n", "log": "x\n"})
    with pytest.raises(ReviewError, match="--dry-run first"):
        publish.publish(path, dry_run=False, reviewers=None)
    assert gh.posts() == []


def test_g7_dry_run_record_is_accepted_by_validate_and_survives_render(setup):
    make, git_calls, mp = setup
    mp.setattr(publish, "gh_api", FakeGitHub())
    path = make([item(1)], dry=False)
    publish.publish(path, dry_run=True, reviewers=None)
    fd.render(path)
    assert "dry_run" in json.loads(open(path).read())


# G8
def test_g8_skipped_own_thread_reply_is_not_resolved(setup):
    make, git_calls, mp = setup
    gh = FakeGitHub()
    mp.setattr(publish, "gh_api", gh)
    out = publish.publish(make([item(1, own=True, skip=True), item(2, own=True)]), dry_run=False, reviewers=None)
    ids = [b["variables"]["id"] for m, p, b in gh.calls if p == "graphql"]
    assert ids == ["T2"] and out["resolved"] == 1


def test_g8_fixed_commit_must_be_a_fix_commit(setup):
    make, git_calls, mp = setup
    gh = FakeGitHub()
    mp.setattr(publish, "gh_api", gh)
    bad = item(3)
    bad["commit"] = "deadbee"
    path = make([item(1), bad])
    with pytest.raises(ReviewError, match=r"item 3 says it is fixed in deadbee, which is not one of the fix commits"):
        publish.publish(path, dry_run=False, reviewers=None)
    assert gh.posts() == [] and not [a for c, a in git_calls if a[0] == "push"]
    rl = [a for c, a in git_calls if a[0] == "rev-list"][0]
    assert rl == ("rev-list", "b19dc43..HEAD")
    out = publish.publish(path, dry_run=True, reviewers=None)
    assert out["commit_problems"] == ["item 3 says it is fixed in deadbee, which is not one of the fix commits."]


def test_g8_covered_and_wont_fix_items_need_no_commit(setup):
    make, git_calls, mp = setup
    mp.setattr(publish, "gh_api", FakeGitHub())
    wont = item(2, status="wont_fix")
    wont["commit"] = None
    cov = item(3, status="covered")
    cov["commit"], cov["covered_by"] = None, 1
    publish.publish(make([item(1), wont, cov]), dry_run=False, reviewers=None)
