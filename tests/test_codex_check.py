import json
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import codex_check
from prlib import ReviewError
from codex_check import (
    DEFAULT_EFFORT,
    DEFAULT_MODEL,
    build_prompt,
    check,
    normalize_verdict,
    should_stop,
)


def test_missing_ids_become_unsure():
    raw = {"verdicts": [{"id": 1, "verdict": "agree", "reason": "ok"}], "missed": [], "overall": "fine"}
    v = normalize_verdict(raw, [1, 2])
    assert [(x["id"], x["verdict"]) for x in v["verdicts"]] == [(1, "agree"), (2, "unsure")]
    assert "did not answer" in v["verdicts"][1]["reason"]


def test_unknown_ids_dropped():
    raw = {"verdicts": [{"id": 1, "verdict": "agree", "reason": ""}, {"id": 9, "verdict": "agree", "reason": ""}],
           "missed": [], "overall": ""}
    assert [x["id"] for x in normalize_verdict(raw, [1])["verdicts"]] == [1]


def test_bad_shape_raises():
    with pytest.raises(ValueError):
        normalize_verdict({"verdicts": [{"id": 1, "verdict": "maybe", "reason": ""}], "missed": [], "overall": ""}, [1])
    with pytest.raises(ValueError):
        normalize_verdict({"missed": [], "overall": ""}, [1])


def _v(verdicts, missed=()):
    return {"verdicts": [{"id": i, "verdict": x, "reason": ""} for i, x in verdicts],
            "missed": [{"path": "a", "line": 1, "severity": s, "body": ""} for s in missed], "overall": ""}


def test_stop_when_all_agree_and_only_nits_missed():
    assert should_stop(_v([(1, "agree"), (2, "agree")], missed=["nit"]))


def test_no_stop_on_disagree_or_unsure():
    assert not should_stop(_v([(1, "agree"), (2, "disagree")]))
    assert not should_stop(_v([(1, "unsure")]))


def test_no_stop_when_serious_issue_missed():
    assert not should_stop(_v([(1, "agree")], missed=["should-fix"]))
    assert not should_stop(_v([(1, "agree")], missed=["blocker"]))


def test_stop_with_no_notes_and_nothing_missed():
    assert should_stop(_v([]))


def test_build_prompt_fills_everything():
    ctx = {"pr": {"title": "Add x", "body": "Why x", "head_sha": "abc123"}, "diff_path": "/tmp/d.diff"}
    p = build_prompt("{{TITLE}}|{{BODY}}|{{DIFF_PATH}}|{{ROUND}}|{{NOTES_JSON}}", ctx, [{"id": 1}], 2)
    assert p.startswith("Add x|Why x|/tmp/d.diff|2|")
    assert '"id": 1' in p and "{{" not in p


def test_build_prompt_fills_head_sha():
    ctx = {"pr": {"title": "T", "body": "B", "head_sha": "deadbeef"}, "diff_path": "/tmp/d.diff"}
    p = build_prompt("at {{HEAD_SHA}}: git show {{HEAD_SHA}}:<path>", ctx, [], 1)
    assert p == "at deadbeef: git show deadbeef:<path>"


def test_build_prompt_missing_head_sha_raises():
    ctx = {"pr": {"title": "T", "body": "B"}, "diff_path": "/tmp/d.diff"}
    with pytest.raises(KeyError):
        build_prompt("{{TITLE}}", ctx, [], 1)


def test_template_mentions_head_sha():
    from codex_check import TEMPLATE
    assert "{{HEAD_SHA}}" in TEMPLATE.read_text()


def test_build_prompt_one_pass_injection_safe():
    """Body containing {{NOTES_JSON}} stays literal; notes appear exactly once."""
    ctx = {"pr": {"title": "Fix", "body": "Contains {{NOTES_JSON}} injection", "head_sha": "abc123"}, "diff_path": "/tmp/d.diff"}
    notes = [{"id": 1, "body": "issue"}]
    p = build_prompt("{{BODY}}\n{{NOTES_JSON}}", ctx, notes, 1)
    # The literal {{NOTES_JSON}} in the body should NOT be replaced
    assert "Contains {{NOTES_JSON}} injection" in p
    # The actual notes JSON should appear exactly once
    assert p.count('"id": 1') == 1
    notes_json_count = p.count(json.dumps({"notes": notes, "rejected_missed": []}, indent=2))
    assert notes_json_count == 1


def test_normalize_verdict_rejects_non_dict_verdicts():
    raw = {"verdicts": ["not a dict"], "missed": [], "overall": ""}
    with pytest.raises(ValueError, match="is not a dict"):
        normalize_verdict(raw, [])


def test_normalize_verdict_rejects_non_dict_missed():
    raw = {"verdicts": [], "missed": ["not a dict"], "overall": ""}
    with pytest.raises(ValueError, match="is not a dict"):
        normalize_verdict(raw, [])


@patch("codex_check.subprocess.run")
def test_run_codex_first_fails_second_succeeds(mock_run, tmp_path):
    """First codex call writes bad JSON, second succeeds -> returns result, called twice."""
    verdict_data = {"verdicts": [{"id": 1, "verdict": "agree", "reason": "ok"}], "missed": [], "overall": "good"}

    # First call fails, second succeeds
    call_count = [0]
    def run_side_effect(cmd, *args, **kwargs):
        call_count[0] += 1
        if call_count[0] == 1:
            # first attempt: exit 0 but the answer file is not JSON
            o_idx = cmd.index("-o")
            Path(cmd[o_idx + 1]).write_text("not json {")
            return MagicMock(returncode=0)
        else:
            # Write output file for second call (extract -o path from cmd)
            try:
                o_idx = cmd.index("-o")
                output_path = Path(cmd[o_idx + 1])
                output_path.write_text(json.dumps(verdict_data))
            except (ValueError, IndexError):
                pass
            return MagicMock(returncode=0)

    mock_run.side_effect = run_side_effect

    ctx_file = tmp_path / "ctx.json"
    notes_file = tmp_path / "notes.json"
    ctx_file.write_text('{"pr": {"title": "T", "body": "B", "head_sha": "abc123"}, "diff_path": "/tmp/d"}')
    notes_file.write_text('[{"id": 1}]')

    result = check(str(ctx_file), str(notes_file), 1, str(tmp_path), "gpt-6-sol", "xhigh")
    assert result["stop"] is True  # all agree and no serious missed
    assert result["model"] == "gpt-6-sol"
    assert result["effort"] == "xhigh"
    assert call_count[0] == 2


@patch("codex_check.subprocess.run")
def test_run_codex_both_fail_raises_review_error(mock_run, tmp_path):
    """Both codex calls write bad JSON -> ReviewError mentioning 'Codex check failed twice'."""
    def bad_json(cmd, *args, **kwargs):
        o_idx = cmd.index("-o")
        Path(cmd[o_idx + 1]).write_text("garbage")
        return MagicMock(returncode=0)
    mock_run.side_effect = bad_json

    ctx_file = tmp_path / "ctx.json"
    notes_file = tmp_path / "notes.json"
    ctx_file.write_text('{"pr": {"title": "T", "body": "B", "head_sha": "abc123"}, "diff_path": "/tmp/d"}')
    notes_file.write_text('[{"id": 1}]')

    with pytest.raises(Exception, match="Codex check failed twice"):
        check(str(ctx_file), str(notes_file), 1, str(tmp_path), "gpt-6-sol", "xhigh")


@patch("codex_check.subprocess.run")
def test_run_codex_no_output_file_treated_as_failure(mock_run, tmp_path):
    """returncode 0 but no output file -> treated as failure, retried."""
    mock_run.return_value = MagicMock(returncode=0)

    ctx_file = tmp_path / "ctx.json"
    notes_file = tmp_path / "notes.json"
    ctx_file.write_text('{"pr": {"title": "T", "body": "B", "head_sha": "abc123"}, "diff_path": "/tmp/d"}')
    notes_file.write_text('[{"id": 1}]')

    with pytest.raises(Exception, match="Codex check failed twice"):
        check(str(ctx_file), str(notes_file), 1, str(tmp_path), "gpt-6-sol", "xhigh")


@patch("codex_check.subprocess.run")
def test_run_codex_passes_stdin_devnull(mock_run, tmp_path):
    """subprocess.run is called with stdin=subprocess.DEVNULL."""
    verdict_data = {"verdicts": [], "missed": [], "overall": ""}

    def capture_run(cmd, *args, **kwargs):
        # Verify stdin=subprocess.DEVNULL is passed
        assert kwargs.get("stdin") == subprocess.DEVNULL
        # Write output file (extract -o path from cmd)
        try:
            o_idx = cmd.index("-o")
            output_path = Path(cmd[o_idx + 1])
            output_path.write_text(json.dumps(verdict_data))
        except (ValueError, IndexError):
            pass
        return MagicMock(returncode=0)

    mock_run.side_effect = capture_run

    ctx_file = tmp_path / "ctx.json"
    notes_file = tmp_path / "notes.json"
    ctx_file.write_text('{"pr": {"title": "T", "body": "B", "head_sha": "abc123"}, "diff_path": "/tmp/d"}')
    notes_file.write_text('[]')

    check(str(ctx_file), str(notes_file), 1, str(tmp_path), "gpt-6-sol", "xhigh")


@patch("codex_check.subprocess.run")
def test_run_codex_argv_structure(mock_run, tmp_path):
    """Codex argv has correct command structure with defaults (gpt-6-sol, xhigh)."""
    verdict_data = {"verdicts": [], "missed": [], "overall": ""}
    cmd_captured = []

    def capture_run(cmd, *args, **kwargs):
        cmd_captured.append(cmd)
        # Write output file
        try:
            o_idx = cmd.index("-o")
            output_path = Path(cmd[o_idx + 1])
            output_path.write_text(json.dumps(verdict_data))
        except (ValueError, IndexError):
            pass
        return MagicMock(returncode=0)

    mock_run.side_effect = capture_run

    ctx_file = tmp_path / "ctx.json"
    notes_file = tmp_path / "notes.json"
    ctx_file.write_text('{"pr": {"title": "T", "body": "B", "head_sha": "abc123"}, "diff_path": "/tmp/d"}')
    notes_file.write_text('[]')

    # Call with DEFAULT_MODEL and DEFAULT_EFFORT to pin the defaults
    check(str(ctx_file), str(notes_file), 1, str(tmp_path), DEFAULT_MODEL, DEFAULT_EFFORT)

    assert len(cmd_captured) == 1
    cmd = cmd_captured[0]

    # Check: cmd[0] == "codex" and cmd[1] == "exec"
    assert cmd[0] == "codex"
    assert cmd[1] == "exec"

    # Check: "--sandbox" immediately followed by "read-only"
    sandbox_idx = cmd.index("--sandbox")
    assert cmd[sandbox_idx + 1] == "read-only"

    # Check: "-m" immediately followed by "gpt-6-sol"
    m_idx = cmd.index("-m")
    assert cmd[m_idx + 1] == "gpt-6-sol"

    # Check: "-c" immediately followed by 'model_reasoning_effort="xhigh"'
    c_idx = cmd.index("-c")
    assert cmd[c_idx + 1] == 'model_reasoning_effort="xhigh"'

    # Check: "--output-schema" and "-o" are both present
    assert "--output-schema" in cmd
    assert "-o" in cmd


def _write_inputs(tmp_path, notes_text='[{"id": 1}]'):
    ctx_file = tmp_path / "ctx.json"
    notes_file = tmp_path / "notes.json"
    ctx_file.write_text('{"pr": {"title": "T", "body": "B", "head_sha": "abc123"}, "diff_path": "/tmp/d"}')
    notes_file.write_text(notes_text)
    return str(ctx_file), str(notes_file)


def test_codex_timeout_is_25_minutes():
    assert codex_check.CODEX_TIMEOUT_S == 1500


@patch("codex_check.subprocess.run")
def test_nonzero_exit_fails_fast_without_retry(mock_run, tmp_path):
    """A nonzero codex exit (auth failure etc.) is not retried: exactly one call."""
    mock_run.return_value = MagicMock(returncode=1, stderr="auth failed: please log in", stdout="")
    ctx, notes = _write_inputs(tmp_path)
    with pytest.raises(ReviewError, match="Codex failed: auth failed"):
        check(ctx, notes, 1, str(tmp_path), "gpt-6-sol", "xhigh")
    assert mock_run.call_count == 1


@patch("codex_check.subprocess.run")
def test_nonzero_exit_message_keeps_last_800_chars(mock_run, tmp_path):
    mock_run.return_value = MagicMock(returncode=1, stderr="x" * 2000 + "THE-END", stdout="")
    ctx, notes = _write_inputs(tmp_path)
    with pytest.raises(ReviewError) as ei:
        check(ctx, notes, 1, str(tmp_path), "gpt-6-sol", "xhigh")
    msg = str(ei.value)
    assert msg.endswith("THE-END") and len(msg) < 900


@patch("codex_check.subprocess.run")
def test_timeout_retried_once_then_succeeds(mock_run, tmp_path):
    verdict_data = {"verdicts": [{"id": 1, "verdict": "agree", "reason": ""}], "missed": [], "overall": ""}
    calls = [0]

    def side(cmd, *args, **kwargs):
        calls[0] += 1
        if calls[0] == 1:
            raise subprocess.TimeoutExpired(cmd, 1500)
        Path(cmd[cmd.index("-o") + 1]).write_text(json.dumps(verdict_data))
        return MagicMock(returncode=0)

    mock_run.side_effect = side
    ctx, notes = _write_inputs(tmp_path)
    assert check(ctx, notes, 1, str(tmp_path), "gpt-6-sol", "xhigh")["stop"] is True
    assert calls[0] == 2


@patch("codex_check.subprocess.run")
def test_timeout_twice_reports_both_attempts(mock_run, tmp_path):
    mock_run.side_effect = subprocess.TimeoutExpired("codex", 1500)
    ctx, notes = _write_inputs(tmp_path)
    with pytest.raises(ReviewError, match="Codex check failed twice") as ei:
        check(ctx, notes, 1, str(tmp_path), "gpt-6-sol", "xhigh")
    assert mock_run.call_count == 2
    assert str(ei.value).count("timed out") >= 2


def _capture_prompt_run(captured, verdict_data):
    def side(cmd, *args, **kwargs):
        captured.append(cmd[-1])
        Path(cmd[cmd.index("-o") + 1]).write_text(json.dumps(verdict_data))
        return MagicMock(returncode=0)
    return side


@patch("codex_check.subprocess.run")
def test_notes_object_shape_reaches_prompt(mock_run, tmp_path):
    obj = {
        "notes": [{"id": 1, "path": "a.py", "line": 3, "body": "b", "reply": "Fixed it: guard added on line 2"},
                  {"id": 4, "path": "a.py", "line": 8, "body": "c"}],
        "rejected_missed": [{"path": "z.py", "line": 7, "severity": "should-fix", "body": "race",
                             "reason": "lock is held by caller"}],
    }
    captured = []
    verdict = {"verdicts": [{"id": 1, "verdict": "agree", "reason": ""}, {"id": 4, "verdict": "agree", "reason": ""}],
               "missed": [], "overall": ""}
    mock_run.side_effect = _capture_prompt_run(captured, verdict)
    ctx, notes = _write_inputs(tmp_path, json.dumps(obj))
    result = check(ctx, notes, 2, str(tmp_path), "gpt-6-sol", "xhigh")
    assert [v["id"] for v in result["verdicts"]] == [1, 4]  # ids come from the notes list
    prompt = captured[0]
    assert "lock is held by caller" in prompt
    assert "Fixed it: guard added on line 2" in prompt
    assert '"rejected_missed"' in prompt and '"notes"' in prompt


@patch("codex_check.subprocess.run")
def test_notes_list_shape_still_works_and_is_wrapped(mock_run, tmp_path):
    captured = []
    verdict = {"verdicts": [{"id": 1, "verdict": "agree", "reason": ""}], "missed": [], "overall": ""}
    mock_run.side_effect = _capture_prompt_run(captured, verdict)
    ctx, notes = _write_inputs(tmp_path, '[{"id": 1, "body": "old shape"}]')
    result = check(ctx, notes, 1, str(tmp_path), "gpt-6-sol", "xhigh")
    assert result["stop"] is True
    assert "old shape" in captured[0]
    assert '"rejected_missed": []' in captured[0]


def test_notes_object_without_notes_list_is_an_error(tmp_path):
    ctx, notes = _write_inputs(tmp_path, '{"rejected_missed": []}')
    with pytest.raises(ReviewError, match="notes"):
        check(ctx, notes, 1, str(tmp_path), "gpt-6-sol", "xhigh")


def test_template_explains_reply_and_rejected_missed():
    text = codex_check.TEMPLATE.read_text()
    assert "reply" in text and "rejected_missed" in text
    assert "<untrusted_pr_title>" in text and "SECURITY NOTE" in text
