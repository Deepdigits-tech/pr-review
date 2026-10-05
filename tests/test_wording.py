import json

import codex_check
import wording

TEXTS = [{"id": "reply-1", "kind": "reply", "text": "fixed it in abc1234 the test is t1"},
         {"id": "note-2", "kind": "note", "text": "orig note"},
         {"id": "reply-3", "kind": "reply", "text": "keep me"}]


def test_normalize_keeps_original_for_missing_or_empty():
    raw = {"texts": [{"id": "reply-1", "text": "**Fixed in `abc1234`:** done. **Test:** `t1`."},
                     {"id": "note-2", "text": "   "}, {"id": "zzz", "text": "unknown"}]}
    out = wording.normalize_wording(raw, TEXTS)
    assert [(t["id"], t["changed"]) for t in out] == [("reply-1", True), ("note-2", False), ("reply-3", False)]
    assert out[1]["polished"] == "orig note" and out[2]["polished"] == "keep me"
    assert out[0]["original"] == TEXTS[0]["text"]


def test_normalize_rejects_bad_shape():
    import pytest
    with pytest.raises(ValueError):
        wording.normalize_wording({"nope": []}, TEXTS)


def test_polish_runs_codex_with_wording_schema(tmp_path, monkeypatch):
    seen = {}

    def fake_run(cmd, **kw):
        seen["cmd"] = cmd
        open(cmd[cmd.index("-o") + 1], "w").write(json.dumps({"texts": [{"id": "reply-1", "text": "P"}]}))

        class R:
            returncode = 0
            stdout = stderr = ""
        return R()

    monkeypatch.setattr(codex_check.subprocess, "run", fake_run)
    f = tmp_path / "texts.json"
    f.write_text(json.dumps({"texts": TEXTS}))
    out = wording.polish(str(f), "/copy", "gpt-6-sol", "xhigh")
    assert out["texts"][0]["polished"] == "P"
    cmd = seen["cmd"]
    assert cmd[cmd.index("--output-schema") + 1].endswith("wording.schema.json")
    prompt = cmd[-1]
    assert '"id": "note-2"' in prompt
    assert "<untrusted_texts>" in prompt and "</untrusted_texts>" in prompt


def test_normalize_keeps_original_if_backtick_token_dropped():
    raw = {"texts": [{"id": "reply-1", "text": "Fixed in abc1234. Test: `t1`."}]}
    texts = [{"id": "reply-1", "kind": "reply", "text": "Fixed in `abc1234`. Test: `t1`."}]
    out = wording.normalize_wording(raw, texts)
    assert out[0]["polished"] == texts[0]["text"]
    assert out[0]["changed"] is False
    assert "kept_original" in out[0]
    assert "`abc1234`" in out[0]["kept_original"]
