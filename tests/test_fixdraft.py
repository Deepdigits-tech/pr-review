import json

import pytest

import fixdraft as fd
from prlib import ReviewError


def item(i, status="fixed", kind="thread", **kw):
    base = {"id": i, "kind": kind, "source": "bob-dev", "own": False, "thread_id": f"T{i}",
            "comment_id": 100 + i, "path": "a.py", "line": i, "body": f"comment {i}", "status": status,
            "commit": "abc1234" if status == "fixed" else None, "test": "t" if status == "fixed" else None,
            "covered_by": None, "reason": None, "debate": None,
            "reply": None if kind == "ours" else f"reply {i}", "skip_reply": False}
    base.update(kw)
    return base


def make(items, **over):
    d = {"pr": {"owner": "acme-corp", "repo": "web-app", "number": 63, "url": "u",
                "head": "feature-branch", "base_sha": "b19dc43"},
         "account": "alice-dev", "copy_path": "/c", "rounds": 2, "codex_checked": True, "items": items}
    d.update(over)
    return d


@pytest.fixture(autouse=True)
def tmp_reviews(tmp_path, monkeypatch):
    monkeypatch.setattr(fd, "REVIEWS_DIR", tmp_path)
    return tmp_path


def test_scorecard():
    d = make([item(1), item(2), item(3, "wont_fix", reason="by design"), item(4, "covered", covered_by=1),
              item(5, "you_decide", debate="x"), item(6, kind="ours", source="ours", thread_id=None, comment_id=None)])
    assert fd.scorecard(d) == ("6 items · 3 fixed · 1 won't fix · 1 you decide · 1 covered by another fix"
                               " · stopped after 2 rounds")


def test_scorecard_unchecked_and_skipped():
    d = make([item(1), item(2, skip_reply=True)], codex_checked=False)
    assert fd.scorecard(d) == "2 items · 2 fixed · NOT CHECKED BY CODEX · 1 reply skipped"


def test_validate_rules():
    fd.validate(make([item(1)]))
    with pytest.raises(ReviewError, match="reply"):
        fd.validate(make([item(1, reply=None)]))
    fd.validate(make([item(1, reply=None, skip_reply=True)]))
    with pytest.raises(ReviewError, match="ours"):
        fd.validate(make([item(1, kind="ours", reply="x")]))
    with pytest.raises(ReviewError, match="covered_by"):
        fd.validate(make([item(1, "covered", covered_by=9)]))
    with pytest.raises(ReviewError, match="commit"):
        fd.validate(make([item(1, commit=None)]))
    with pytest.raises(ReviewError, match="status"):
        fd.validate(make([item(1, "done")]))


def test_save_sets_published_and_keeps_previous(tmp_reviews):
    first = fd.save(make([item(1)]))
    saved = json.loads(open(first["json"]).read())
    assert saved["published"] == {"pushed": None, "replies": {}, "summary_comment": None,
                                  "resolved": [], "rerequested": [], "done": False}
    second = fd.save(make([item(1)]))
    assert second["kept_previous"].endswith("acme-corp-web-app-63.fix-v1.json")


def test_markdown_you_decide_first_and_replies_grouped(tmp_reviews):
    d = make([item(1), item(2, "you_decide", debate="Claude: A. Codex: B."),
              item(3, source="alice-dev", own=True, reply="own reply")])
    md = fd.render_markdown(d)
    assert md.index("Claude: A. Codex: B.") < md.index("reply 1")
    assert "### Replies to bob-dev" in md and "### Replies to your own notes" in md


def test_skip_and_unskip(tmp_reviews):
    out = fd.save(make([item(1), item(2)]))
    fd.skip(out["json"], [2])
    assert [i["skip_reply"] for i in json.loads(open(out["json"]).read())["items"]] == [False, True]
    fd.unskip(out["json"], [2])
    assert [i["skip_reply"] for i in json.loads(open(out["json"]).read())["items"]] == [False, False]
    with pytest.raises(ReviewError, match="99"):
        fd.skip(out["json"], [99])


def test_resave_carries_over_mid_publish_progress(tmp_reviews):
    first = fd.save(make([item(1)]))
    on_disk = json.loads(open(first["json"]).read())
    on_disk["published"].update({"pushed": "fff0000", "replies": {"1": "u"}})
    open(first["json"], "w").write(json.dumps(on_disk))
    new = make([item(1)])
    new["published"] = {"pushed": None, "replies": {}, "summary_comment": None,
                        "resolved": [], "rerequested": [], "done": False, "stale": True}
    second = fd.save(new)
    saved = json.loads(open(second["json"]).read())
    assert saved["published"] == on_disk["published"]
    assert second["kept_previous"].endswith("-v1.json")


def test_resave_after_done_starts_fresh(tmp_reviews):
    first = fd.save(make([item(1)]))
    on_disk = json.loads(open(first["json"]).read())
    on_disk["published"].update({"pushed": "fff0000", "replies": {"1": "u"}, "done": True})
    open(first["json"], "w").write(json.dumps(on_disk))
    second = fd.save(make([item(1)]))
    assert json.loads(open(second["json"]).read())["published"] == fd.EMPTY_PUBLISHED


def test_resave_with_untouched_published_stays_empty(tmp_reviews):
    fd.save(make([item(1)]))
    second = fd.save(make([item(1)]))
    assert json.loads(open(second["json"]).read())["published"] == fd.EMPTY_PUBLISHED


def test_validate_thread_items_need_thread_and_comment_ids():
    for missing in ("thread_id", "comment_id"):
        with pytest.raises(ReviewError, match="thread_id and comment_id"):
            fd.validate(make([item(1, **{missing: None})]))
    fd.validate(make([item(1, kind="summary", thread_id=None, comment_id=None)]))


def test_validate_covered_by_cannot_be_the_item_itself():
    with pytest.raises(ReviewError, match="itself"):
        fd.validate(make([item(1, "covered", covered_by=1)]))


def test_validate_accepts_dry_run_record_and_rejects_junk():
    fd.validate(make([item(1)], dry_run={"at": "2026-10-04T00:00:00+00:00", "head": "abc"}))
    with pytest.raises(ReviewError, match="dry_run"):
        fd.validate(make([item(1)], dry_run="yes"))
