import json

import threads


def thread(tid, login, *, resolved=False, outdated=False, line=10, original=10, body="b", replies=(),
           created="2026-01-01T00:00:00Z"):
    """replies: (author, text) or (author, text, createdAt)."""
    comments = [{"databaseId": 100 + tid, "author": ({"login": login} if login else None), "body": body,
                 "url": f"https://x/{tid}", "createdAt": created}]
    comments += [{"databaseId": 900 + i, "author": {"login": r[0]}, "body": r[1], "url": "u",
                  "createdAt": r[2] if len(r) > 2 else "2026-01-01T00:00:01Z"} for i, r in enumerate(replies)]
    return {"id": f"T{tid}", "isResolved": resolved, "isOutdated": outdated, "path": "a.py",
            "line": line, "originalLine": original, "comments": {"nodes": comments}}


def review(rid, login, state, body, at=None):
    return {"databaseId": rid, "author": {"login": login}, "state": state, "body": body, "submittedAt": at}


def pr_comment(login, at):
    return {"author": {"login": login}, "createdAt": at}


def test_collect_skips_resolved_and_flags_own():
    out = threads.collect_items(
        [thread(1, "bob-dev"), thread(2, "bob-dev", resolved=True), thread(3, "alice-dev")], [])
    assert [i["thread_id"] for i in out["items"]] == ["T1", "T3"]
    assert [i["id"] for i in out["items"]] == [1, 2]
    assert [i["own"] for i in out["items"]] == [False, True]
    assert out["items"][0]["comment_id"] == 101 and out["items"][0]["kind"] == "thread"
    assert out["next_id"] == 3


def test_ghost_author_is_not_own():
    item = threads.collect_items([thread(1, None)], [])["items"][0]
    assert item["source"] == "ghost" and item["own"] is False


def test_outdated_null_line_falls_back_to_original_line():
    item = threads.collect_items([thread(1, "gp", outdated=True, line=None, original=42)], [])["items"][0]
    assert item["line"] == 42 and item["outdated"] is True


def test_follow_ups_and_start_id():
    out = threads.collect_items([thread(1, "gp", replies=[("other", "on it")])], [], start_id=7)
    assert out["items"][0]["id"] == 7
    assert out["items"][0]["follow_ups"] == [{"author": "other", "body": "on it"}]


def test_summaries_latest_changes_requested_per_reviewer_only():
    reviews = [
        review(1, "gp", "CHANGES_REQUESTED", "old summary"),
        review(2, "gp", "CHANGES_REQUESTED", "new summary"),
        review(3, "other", "COMMENTED", "just a comment"),
        review(4, "alice-dev", "CHANGES_REQUESTED", "own review"),
        review(5, "empty", "CHANGES_REQUESTED", "   "),
    ]
    out = threads.collect_items([], reviews)
    assert out["summaries"] == [{"reviewer": "gp", "review_id": 2, "body": "new summary",
                                 "submitted_at": None, "answered": False}]


def test_thread_answered_by_us_is_awaiting_reviewer_not_an_item():
    out = threads.collect_items([thread(1, "gp", replies=[("alice-dev", "fixed")])], [])
    assert out["items"] == [] and out["awaiting_reviewer"] == 1


def test_reviewer_reply_after_ours_makes_it_an_item_again():
    out = threads.collect_items([thread(1, "gp", replies=[("alice-dev", "fixed"), ("gp", "still wrong")])], [])
    assert [i["thread_id"] for i in out["items"]] == ["T1"] and out["awaiting_reviewer"] == 0


def test_own_opened_thread_with_own_follow_up_stays_an_item():
    out = threads.collect_items([thread(1, "alice-dev", replies=[("alice-dev", "note")])], [])
    assert [i["thread_id"] for i in out["items"]] == ["T1"] and out["awaiting_reviewer"] == 0


def test_resolved_answered_thread_is_not_counted_as_awaiting():
    out = threads.collect_items([thread(1, "gp", resolved=True, replies=[("alice-dev", "fixed")])], [])
    assert out["items"] == [] and out["awaiting_reviewer"] == 0


def test_changes_requested_survives_a_later_comment():
    reviews = [review(1, "gp", "CHANGES_REQUESTED", "please fix"), review(2, "gp", "COMMENTED", "thanks")]
    out = threads.collect_items([], reviews)
    assert out["summaries"] == [{"reviewer": "gp", "review_id": 1, "body": "please fix",
                                 "submitted_at": None, "answered": False}]


def test_changes_requested_dropped_by_later_approval_or_dismissal():
    for state in ("APPROVED", "DISMISSED"):
        reviews = [review(1, "gp", "CHANGES_REQUESTED", "please fix"), review(2, "gp", state, "")]
        assert threads.collect_items([], reviews)["summaries"] == []


def test_new_changes_requested_after_approval_is_kept():
    reviews = [review(1, "gp", "APPROVED", ""), review(2, "gp", "CHANGES_REQUESTED", "again")]
    assert threads.collect_items([], reviews)["summaries"] == [
        {"reviewer": "gp", "review_id": 2, "body": "again", "submitted_at": None, "answered": False}]


def test_fetch_paginates_threads(monkeypatch):
    pages = [
        {"data": {"repository": {"pullRequest": {
            "reviewThreads": {"pageInfo": {"hasNextPage": True, "endCursor": "C1"}, "nodes": [thread(1, "gp")]},
            "reviews": {"nodes": [review(9, "gp", "COMMENTED", "x")]},
            "comments": {"nodes": [pr_comment("gp", "2026-01-02T00:00:00Z")]}}}}},
        {"data": {"repository": {"pullRequest": {
            "reviewThreads": {"pageInfo": {"hasNextPage": False, "endCursor": None}, "nodes": [thread(2, "gp")]},
            "reviews": {"nodes": [review(9, "gp", "COMMENTED", "x")]},
            "comments": {"nodes": [pr_comment("gp", "2026-01-02T00:00:00Z")]}}}}},
    ]
    calls = []

    def fake_gh_api(account, path, **kw):
        calls.append((path, kw["method"], kw["body"]["variables"]["after"]))
        return pages[len(calls) - 1]

    monkeypatch.setattr(threads, "gh_api", fake_gh_api)
    got_threads, got_reviews, got_comments = threads.fetch("alice-dev", "acme-corp", "web-app", 63)
    assert [t["id"] for t in got_threads] == ["T1", "T2"]
    assert len(got_reviews) == 1 and got_comments == [pr_comment("gp", "2026-01-02T00:00:00Z")]
    assert calls == [("graphql", "POST", None), ("graphql", "POST", "C1")]


def test_cli_writes_threads_file(tmp_path, monkeypatch):
    monkeypatch.setattr(threads, "REVIEWS_DIR", tmp_path)
    monkeypatch.setattr(threads, "fetch", lambda a, o, r, n: ([thread(1, "gp")], [], []))
    ctx = tmp_path / "ctx.json"
    ctx.write_text(json.dumps({"pr": {"owner": "acme-corp", "repo": "web-app", "number": 63}, "account": "alice-dev"}))
    out = threads.collect_to_file(str(ctx), 1)
    saved = json.loads((tmp_path / ".work" / "acme-corp-web-app-63.threads.json").read_text())
    assert out == {"path": str(tmp_path / ".work" / "acme-corp-web-app-63.threads.json"), "items": 1, "summaries": 0, "awaiting_reviewer": 0,
                   "unanswered_summaries": 0}
    assert saved["items"][0]["thread_id"] == "T1" and saved["unanswered_summaries"] == 0


def test_query_asks_for_the_timestamps_and_pr_comments():
    q = threads.QUERY
    assert "createdAt" in q and "submittedAt" in q
    assert "comments(last: 50) { nodes { author { login } createdAt } }" in q


def test_answered_list_describes_skipped_threads():
    long_body = "x" * 300
    out = threads.collect_items([thread(1, "gp", body=long_body, line=None, original=42, replies=[("alice-dev", "done")]),
                                 thread(2, "gp")], [])
    assert out["awaiting_reviewer"] == 1
    assert out["answered"] == [{"path": "a.py", "line": 42, "source": "gp", "excerpt": "x" * 200}]
    assert [i["thread_id"] for i in out["items"]] == ["T2"]


def test_summary_answered_by_own_later_follow_up_in_any_thread():
    reviews = [review(1, "gp", "CHANGES_REQUESTED", "fix", at="2026-02-01T00:00:00Z")]
    later = thread(1, "other", resolved=True, replies=[("alice-dev", "done", "2026-02-02T00:00:00Z")])
    out = threads.collect_items([later], reviews)
    assert out["summaries"][0]["answered"] is True and out["summaries"][0]["submitted_at"] == "2026-02-01T00:00:00Z"
    assert out["unanswered_summaries"] == 0


def test_summary_answered_by_own_later_review():
    reviews = [review(1, "gp", "CHANGES_REQUESTED", "fix", at="2026-02-01T00:00:00Z"),
               review(2, "alice-dev", "COMMENTED", "", at="2026-02-03T00:00:00Z")]
    assert threads.collect_items([], reviews)["summaries"][0]["answered"] is True


def test_summary_answered_by_own_later_pr_level_comment():
    reviews = [review(1, "gp", "CHANGES_REQUESTED", "fix", at="2026-02-01T00:00:00Z")]
    out = threads.collect_items([], reviews, pr_comments=[pr_comment("alice-dev", "2026-02-04T00:00:00Z")])
    assert out["summaries"][0]["answered"] is True and out["unanswered_summaries"] == 0


def test_older_own_activity_does_not_answer_a_newer_summary():
    reviews = [review(1, "alice-dev", "COMMENTED", "", at="2026-01-05T00:00:00Z"),
               review(2, "gp", "CHANGES_REQUESTED", "fix", at="2026-02-01T00:00:00Z")]
    old_thread = thread(1, "gp", replies=[("alice-dev", "done", "2026-01-10T00:00:00Z")])
    out = threads.collect_items([old_thread], reviews,
                                pr_comments=[pr_comment("alice-dev", "2026-01-20T00:00:00Z"),
                                             pr_comment("gp", "2026-03-01T00:00:00Z")])
    assert out["summaries"][0]["answered"] is False and out["unanswered_summaries"] == 1


def test_reviewers_later_activity_never_answers_and_unanswered_count_is_per_summary():
    reviews = [review(1, "gp", "CHANGES_REQUESTED", "a", at="2026-02-01T00:00:00Z"),
               review(2, "zed", "CHANGES_REQUESTED", "b", at="2026-02-10T00:00:00Z"),
               review(3, "alice-dev", "COMMENTED", "", at="2026-02-05T00:00:00Z")]
    out = threads.collect_items([], reviews)
    assert {s["reviewer"]: s["answered"] for s in out["summaries"]} == {"gp": True, "zed": False}
    assert out["unanswered_summaries"] == 1


def test_own_thread_opening_comment_is_not_an_answer():
    reviews = [review(1, "gp", "CHANGES_REQUESTED", "fix", at="2026-02-01T00:00:00Z")]
    own_note = thread(1, "alice-dev", created="2026-02-02T00:00:00Z")
    assert threads.collect_items([own_note], reviews)["summaries"][0]["answered"] is False


def test_cli_prints_and_stores_unanswered_summaries(tmp_path, monkeypatch):
    monkeypatch.setattr(threads, "REVIEWS_DIR", tmp_path)
    monkeypatch.setattr(threads, "fetch", lambda a, o, r, n: (
        [], [review(1, "gp", "CHANGES_REQUESTED", "fix", at="2026-02-01T00:00:00Z")], []))
    ctx = tmp_path / "ctx.json"
    ctx.write_text(json.dumps({"pr": {"owner": "O", "repo": "r", "number": 1}, "account": "alice-dev"}))
    out = threads.collect_to_file(str(ctx), 1)
    assert out["unanswered_summaries"] == 1 and out["summaries"] == 1
    assert json.loads((tmp_path / ".work" / "O-r-1.threads.json").read_text())["unanswered_summaries"] == 1
