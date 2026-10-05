import json
import subprocess
import sys
from pathlib import Path

import pytest

import draft as d
from prlib import ReviewError


def make(**over):
    base = {
        "pr": {"owner": "acme-corp", "repo": "api", "number": 7,
               "url": "https://github.com/acme-corp/api/pull/7", "title": "Add x", "head_sha": "abc"},
        "account": "alice-dev", "rounds": 2, "codex_checked": True, "summary": "Looks solid.",
        "notes": [
            {"id": 1, "path": "a.py", "line": 3, "side": "RIGHT", "severity": "nit", "body": "rename", "status": "agreed", "keep": True},
            {"id": 2, "path": "a.py", "line": 9, "side": "RIGHT", "severity": "blocker", "body": "null deref", "status": "fixed", "keep": True},
            {"id": 3, "path": "b.py", "line": 1, "side": "RIGHT", "severity": "should-fix", "body": "race", "status": "you-decide", "keep": True,
             "debate": "Claude: real race. Codex: lock already held."},
        ],
        "posted": None,
    }
    base.update(over)
    return base


@pytest.fixture(autouse=True)
def tmp_reviews(tmp_path, monkeypatch):
    monkeypatch.setattr(d, "REVIEWS_DIR", tmp_path)
    return tmp_path


def test_scorecard_counts():
    assert d.scorecard(make()) == (
        "3 notes · 1 both agreed · 1 fixed after review · 1 still disputed (you decide) · stopped after 2 rounds"
    )


def test_scorecard_unchecked_and_dropped():
    dr = make(codex_checked=False, rounds=0)
    for n in dr["notes"]:
        n["status"] = "unchecked"
    dr["notes"][0]["keep"] = False
    assert d.scorecard(dr) == "3 notes · NOT CHECKED BY CODEX · 1 dropped by you"


def test_markdown_puts_you_decide_first_then_severity():
    md = d.render_markdown(make())
    assert md.index("race") < md.index("null deref") < md.index("rename")
    assert "Claude: real race. Codex: lock already held." in md


def test_validate_rejects_bad_severity():
    dr = make()
    dr["notes"][0]["severity"] = "critical"
    with pytest.raises(ReviewError, match="severity"):
        d.validate(dr)


def test_save_keeps_previous_draft_as_v1(tmp_reviews):
    first = d.save(make(summary="first"))
    second = d.save(make(summary="second"))
    assert first["kept_previous"] is None
    assert second["kept_previous"].endswith("acme-corp-api-7-v1.json")
    assert json.loads((tmp_reviews / "acme-corp-api-7-v1.json").read_text())["summary"] == "first"
    assert json.loads((tmp_reviews / "acme-corp-api-7.json").read_text())["summary"] == "second"
    third = d.save(make(summary="third"))
    assert third["kept_previous"].endswith("-v2.json")


def test_drop_marks_notes_not_kept(tmp_reviews):
    out = d.save(make())
    d.drop(out["json"], [1, 3])
    saved = json.loads(open(out["json"]).read())
    assert [n["keep"] for n in saved["notes"]] == [False, True, False]


def test_drop_unknown_id_errors(tmp_reviews):
    out = d.save(make())
    with pytest.raises(ReviewError, match="99"):
        d.drop(out["json"], [99])


def test_render_missing_file_cli():
    """render on a missing file via CLI subprocess returns JSON error and exit 2."""
    result = subprocess.run(
        [sys.executable, str(Path(__file__).parent.parent / "scripts" / "draft.py"), "render", "/nonexistent.json"],
        capture_output=True,
        text=True
    )
    assert result.returncode == 2
    data = json.loads(result.stdout)
    assert "error" in data
    assert "FileNotFoundError" in data["error"]


def test_drop_no_ids_cli():
    """drop with no ids via CLI returns JSON error and exit 2."""
    result = subprocess.run(
        [sys.executable, str(Path(__file__).parent.parent / "scripts" / "draft.py"), "drop", "dummy.json"],
        capture_output=True,
        text=True
    )
    assert result.returncode == 2
    data = json.loads(result.stdout)
    assert "error" in data
    assert "usage" in data["error"]


def test_keep_marks_notes_kept(tmp_reviews):
    dr = make()
    for n in dr["notes"]:
        n["keep"] = False
    out = d.save(dr)
    res = d.keep(out["json"], [3, 1])
    saved = json.loads(open(out["json"]).read())
    assert [n["keep"] for n in saved["notes"]] == [True, False, True]
    assert set(res) == {"json", "md", "scorecard"}
    assert "DROPPED" in open(out["md"]).read()  # note 2 still dropped in the rendered file


def test_keep_unknown_id_errors(tmp_reviews):
    out = d.save(make())
    with pytest.raises(ReviewError, match="99"):
        d.keep(out["json"], [99])


def test_keep_no_ids_cli():
    result = subprocess.run(
        [sys.executable, str(Path(__file__).parent.parent / "scripts" / "draft.py"), "keep", "dummy.json"],
        capture_output=True, text=True)
    assert result.returncode == 2
    assert "usage" in json.loads(result.stdout)["error"]


def test_keep_cli_end_to_end(tmp_path):
    dr = make()
    dr["notes"][2]["keep"] = False
    p = tmp_path / "acme-corp-api-7.json"
    p.write_text(json.dumps(dr))
    result = subprocess.run(
        [sys.executable, str(Path(__file__).parent.parent / "scripts" / "draft.py"), "keep", str(p), "3,"],
        capture_output=True, text=True)
    assert result.returncode == 0, result.stdout
    assert json.loads(p.read_text())["notes"][2]["keep"] is True


def test_scorecard_untouched_you_decide_is_left_out_not_dropped():
    dr = make()
    dr["notes"][2]["keep"] = False  # you-decide note, default keep:false
    assert d.scorecard(dr).endswith("1 still disputed (you decide) · stopped after 2 rounds · 1 left out until you keep it")
    assert "dropped by you" not in d.scorecard(dr)


def test_scorecard_counts_dropped_and_left_out_separately():
    dr = make()
    dr["notes"][0]["keep"] = False  # owner dropped an agreed note
    dr["notes"][2]["keep"] = False  # you-decide, not kept
    assert d.scorecard(dr).endswith("1 dropped by you · 1 left out until you keep it")


def test_markdown_labels_left_out_you_decide_notes():
    dr = make()
    dr["notes"][2]["keep"] = False
    md = d.render_markdown(dr)
    assert "LEFT OUT (reply keep 3 to include)" in md
    assert "DROPPED" not in md


def test_validate_title_must_be_string():
    dr = make()
    dr["notes"][0]["title"] = "Short title"
    d.validate(dr)
    dr["notes"][0]["title"] = 5
    with pytest.raises(ReviewError, match="title"):
        d.validate(dr)


def test_actions_validate_and_default_leave():
    dr = make()
    d.validate(dr)
    dr["notes"][0]["action"] = "fix"
    dr["notes"][0]["fixed_in"] = "abc1234"
    dr["notes"][0]["fix_summary"] = "renamed"
    d.validate(dr)
    dr["notes"][0]["action"] = "maybe"
    with pytest.raises(ReviewError, match="action"):
        d.validate(dr)


def test_verdict_blocker_left_requests_changes():
    dr = make()  # note 2 is a kept blocker, action defaults to leave
    assert d.decide_verdict(dr) == "REQUEST_CHANGES"


def test_verdict_blocker_fixed_or_dropped_approves():
    dr = make()
    dr["notes"][1]["action"] = "fix"
    assert d.decide_verdict(dr) == "APPROVE"
    dr = make()
    dr["notes"][1]["keep"] = False
    assert d.decide_verdict(dr) == "APPROVE"


def test_verdict_nothing_found_approves():
    assert d.decide_verdict(make(notes=[])) == "APPROVE"


def test_plan_line():
    dr = make()
    dr["notes"][1]["action"] = "fix"
    dr["notes"][2]["keep"] = False  # you-decide left out
    assert d.plan_line(dr) == "Approve · fix 1 · 0 new issues · 1 optional · 0 left open"
    dr["notes"][2]["keep"] = True  # should-fix, leave → issue
    assert d.plan_line(dr) == "Approve · fix 1 · 1 new issue · 1 optional · 0 left open"
    dr["notes"][1]["action"] = "leave"  # blocker left
    assert d.plan_line(dr) == "Request changes · fix 0 · 0 new issues · 0 optional · 3 left open"


def test_set_action_cli_function(tmp_reviews):
    out = d.save(make())
    d.set_action(out["json"], "fix", [2])
    import json as _json
    saved = _json.loads(open(out["json"]).read())
    assert [n.get("action", "leave") for n in saved["notes"]] == ["leave", "fix", "leave"]
    with pytest.raises(ReviewError, match="99"):
        d.set_action(out["json"], "fix", [99])
    with pytest.raises(ReviewError, match="action"):
        d.set_action(out["json"], "maybe", [2])


def test_set_action_leave_clears_fix_fields(tmp_reviews):
    """When a note is set to 'leave', remove any stale fixed_in and fix_summary."""
    dr = make()
    # Set note 2 to fix with metadata
    dr["notes"][1]["action"] = "fix"
    dr["notes"][1]["fixed_in"] = "abc1234"
    dr["notes"][1]["fix_summary"] = "added null check"
    out = d.save(dr)

    import json as _json
    # Verify the fields are there before changing to leave
    saved = _json.loads(open(out["json"]).read())
    assert saved["notes"][1]["fixed_in"] == "abc1234"
    assert saved["notes"][1]["fix_summary"] == "added null check"

    # Set action to leave
    d.set_action(out["json"], "leave", [2])

    # Verify the fields are removed
    saved = _json.loads(open(out["json"]).read())
    assert saved["notes"][1].get("action", "leave") == "leave"
    assert "fixed_in" not in saved["notes"][1]
    assert "fix_summary" not in saved["notes"][1]


def _saved_with_dry_run():
    out = d.save(make())
    dr = json.loads(open(out["json"]).read())
    dr["dry_run"] = {"at": "t", "head": "abc", "plan": "fp"}
    open(out["json"], "w").write(json.dumps(dr))
    return out["json"]


def test_render_drops_the_dry_run_record():
    path = _saved_with_dry_run()
    d.render(path)
    assert "dry_run" not in json.loads(open(path).read())


@pytest.mark.parametrize("edit", [lambda p: d.drop(p, [1]), lambda p: d.keep(p, [3]),
                                  lambda p: d.set_action(p, "fix", [1]), lambda p: d.set_action(p, "leave", [1])])
def test_any_edit_drops_the_dry_run_record(edit):
    path = _saved_with_dry_run()
    edit(path)
    assert "dry_run" not in json.loads(open(path).read())
