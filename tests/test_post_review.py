import json

import pytest

import draft as draft_mod
import post_review
from post_review import build_payload
from prlib import ReviewError


def draft(notes, summary="Overall fine."):
    return {"pr": {"owner": "acme-corp", "repo": "api", "number": 7, "url": "u", "head_sha": "abc"},
            "account": "alice-dev", "rounds": 1, "codex_checked": True, "summary": summary,
            "notes": notes, "posted": None}


def note(i, path, line, side="RIGHT", keep=True, sev="should-fix"):
    return {"id": i, "path": path, "line": line, "side": side, "severity": sev,
            "body": f"body {i}", "status": "agreed", "keep": keep}


COMMENTABLE = {"a.py": ({10, 11, 12}, {11}), "img.png": (set(), set())}


def test_pins_notes_on_changed_lines():
    payload, moved = build_payload(draft([note(1, "a.py", 11), note(2, "a.py", 11, side="LEFT")]), COMMENTABLE, "abc")
    assert moved == []
    assert payload["event"] == "COMMENT" and payload["commit_id"] == "abc"
    assert payload["comments"] == [
        {"path": "a.py", "line": 11, "side": "RIGHT", "body": "**[should-fix]** body 1"},
        {"path": "a.py", "line": 11, "side": "LEFT", "body": "**[should-fix]** body 2"},
    ]
    assert payload["body"] == "Overall fine."


def test_unpinnable_notes_move_to_summary():
    notes = [note(1, "a.py", 99), note(2, "untouched.py", 5), note(3, "img.png", 1), note(4, "a.py", 10, side="LEFT")]
    payload, moved = build_payload(draft(notes), COMMENTABLE, "abc")
    assert payload["comments"] == []
    assert [n["id"] for n in moved] == [1, 2, 3, 4]
    assert "`a.py:99` — **[should-fix]** body 1" in payload["body"]
    assert "`untouched.py:5`" in payload["body"] and "`img.png:1`" in payload["body"]


def test_dropped_notes_are_not_posted():
    payload, moved = build_payload(draft([note(1, "a.py", 10, keep=False), note(2, "nope.py", 1, keep=False)]), COMMENTABLE, "abc")
    assert payload["comments"] == [] and moved == []
    assert payload["body"] == "Overall fine."


# ---- post(): safety checks and bookkeeping (gh_api is always faked; no network) ----

PATCH = "@@ -9,3 +10,3 @@\n ctx10\n+new11\n ctx12\n"


class FakeGitHub:
    def __init__(self, state="open", head="abc", author="someone-else"):
        self.state, self.head, self.calls, self.author = state, head, [], author

    def __call__(self, account, path, **kw):
        self.calls.append((path, kw.get("method", "GET")))
        if kw.get("method") == "POST":
            self.posted_body = kw["body"]
            return {"html_url": "https://github.com/acme-corp/api/pull/7#pullrequestreview-1"}
        if "/files" in path:
            return [{"filename": "a.py", "patch": PATCH}]
        return {"state": self.state, "head": {"sha": self.head}, "user": {"login": self.author}}

    @property
    def posts(self):
        return [c for c in self.calls if c[1] == "POST"]


@pytest.fixture
def saved_draft(tmp_path, monkeypatch):
    monkeypatch.setattr(draft_mod, "REVIEWS_DIR", tmp_path)
    monkeypatch.setattr(post_review, "account_for_owner", lambda owner: "alice-dev")
    out = draft_mod.save(draft([note(1, "a.py", 11), note(2, "a.py", 99)]))
    return out["json"]


def use(monkeypatch, **kw):
    gh = FakeGitHub(**kw)
    monkeypatch.setattr(post_review, "gh_api", gh)
    return gh


def test_post_refuses_second_post_without_again(saved_draft, monkeypatch):
    use(monkeypatch)
    post_review.post(saved_draft, dry_run=False, allow_new_commits=False, again=False)
    gh = use(monkeypatch)
    with pytest.raises(ReviewError, match="Already posted"):
        post_review.post(saved_draft, dry_run=False, allow_new_commits=False, again=False)
    assert gh.calls == []  # refused before touching GitHub
    post_review.post(saved_draft, dry_run=False, allow_new_commits=False, again=True)  # --again goes through


def test_post_refuses_when_pr_not_open(saved_draft, monkeypatch):
    gh = use(monkeypatch, state="closed")
    with pytest.raises(ReviewError, match="no longer open"):
        post_review.post(saved_draft, dry_run=False, allow_new_commits=False, again=False)
    assert gh.posts == []


def test_post_refuses_new_commits_unless_allowed(saved_draft, monkeypatch):
    gh = use(monkeypatch, head="def4567890")
    with pytest.raises(ReviewError, match="new commits"):
        post_review.post(saved_draft, dry_run=False, allow_new_commits=False, again=False)
    assert gh.posts == []
    out = post_review.post(saved_draft, dry_run=False, allow_new_commits=True, again=False)
    assert "posted" in out
    assert gh.posted_body["commit_id"] == "def4567890"  # posted against the live head


def test_dry_run_never_posts(saved_draft, monkeypatch):
    gh = use(monkeypatch)
    out = post_review.post(saved_draft, dry_run=True, allow_new_commits=False, again=False)
    assert out["dry_run"] is True
    assert out["account"] == "alice-dev"
    assert out["pinned"] == 1 and out["moved_to_summary"] == 1
    assert gh.posts == []
    assert json.loads(open(saved_draft).read())["posted"] is None


def test_real_post_sends_comment_review_and_records_url(saved_draft, monkeypatch):
    gh = use(monkeypatch)
    out = post_review.post(saved_draft, dry_run=False, allow_new_commits=False, again=False)
    assert gh.posts == [("repos/acme-corp/api/pulls/7/reviews", "POST")]
    assert gh.posted_body["event"] == "COMMENT" and gh.posted_body["commit_id"] == "abc"
    assert [c["line"] for c in gh.posted_body["comments"]] == [11]
    assert out["posted"] == "https://github.com/acme-corp/api/pull/7#pullrequestreview-1"
    assert out["pinned"] == 1 and out["moved_to_summary"] == 1 and "warning" not in out
    saved = json.loads(open(saved_draft).read())
    assert saved["posted"]["url"] == out["posted"] and saved["posted"]["head_sha"] == "abc"


def test_bookkeeping_failure_after_post_still_reports_posted(saved_draft, monkeypatch):
    use(monkeypatch)

    def boom(path):
        raise OSError("disk full")

    monkeypatch.setattr(post_review.draft_mod, "render", boom)
    out = post_review.post(saved_draft, dry_run=False, allow_new_commits=False, again=False)
    assert out["posted"].endswith("pullrequestreview-1")
    assert "review posted but the draft file could not be updated" in out["warning"]
    assert "disk full" in out["warning"]
    assert out["pinned"] == 1


def test_comment_body_with_title():
    from post_review import _comment_body
    n = note(1, "a.py", 10)
    assert _comment_body(n) == "**[should-fix]** body 1"
    n["title"] = "Commit runs late"
    assert _comment_body(n) == "**[should-fix] Commit runs late**\n\nbody 1"


def test_titled_note_in_moved_to_summary_is_indented():
    n = note(1, "a.py", 99)
    n["title"] = "T"
    payload, moved = build_payload(draft([n]), COMMENTABLE, "abc")
    assert payload["comments"] == []
    assert "`a.py:99` — **[should-fix] T**" in payload["body"]
    assert "  body 1" in payload["body"]


def test_post_refuses_the_owners_own_pr(saved_draft, monkeypatch):
    for author in ("alice-dev", "alice-work"):
        gh = use(monkeypatch, author=author)
        with pytest.raises(ReviewError, match=r"This is your PR — /pr-review fixes instead of posting\."):
            post_review.post(saved_draft, dry_run=False, allow_new_commits=False, again=False)
        assert gh.posts == []
    gh = use(monkeypatch, author="alice-dev")
    with pytest.raises(ReviewError, match="your PR"):
        post_review.post(saved_draft, dry_run=True, allow_new_commits=False, again=False)


def test_main_refuses_and_points_to_review_publish(monkeypatch, capsys):
    monkeypatch.setattr(post_review.sys, "argv", ["post_review.py", "whatever.json"])
    with pytest.raises(SystemExit) as e:
        post_review.main()
    assert e.value.code == 2
    assert json.loads(capsys.readouterr().out) == {
        "error": "post_review.py no longer posts; use review_publish.py"}
