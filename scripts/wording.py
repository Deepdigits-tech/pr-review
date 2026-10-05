#!/usr/bin/env python3
"""Codex polishes the wording of everything that will be posted. Prints JSON."""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from codex_check import DEFAULT_EFFORT, DEFAULT_MODEL, run_with_retry  # noqa: E402
from prlib import run_main  # noqa: E402

SKILL_DIR = Path(__file__).resolve().parent.parent
SCHEMA = SKILL_DIR / "wording.schema.json"
TEMPLATE = SKILL_DIR / "wording-pass.md"


def normalize_wording(raw: dict, texts: list[dict]) -> list[dict]:
    if not isinstance(raw, dict) or not isinstance(raw.get("texts"), list):
        raise ValueError("Codex answer is missing 'texts'")
    polished = {}
    for t in raw["texts"]:
        if isinstance(t, dict) and isinstance(t.get("id"), str) and isinstance(t.get("text"), str):
            polished.setdefault(t["id"], t["text"])
    out = []
    for t in texts:
        new = polished.get(t["id"], "")
        keep_original = not new.strip()

        # Check if any backtick tokens from the original are missing in the polished version
        kept_original_reason = None
        if not keep_original:
            orig_tokens = set(re.findall(r"`[^`]+`", t["text"]))
            new_tokens = set(re.findall(r"`[^`]+`", new))
            dropped = orig_tokens - new_tokens
            if dropped:
                keep_original = True
                kept_original_reason = f"dropped {dropped.pop()}"

        entry = {"id": t["id"], "original": t["text"],
                 "polished": t["text"] if keep_original else new,
                 "changed": (not keep_original) and new != t["text"]}
        if kept_original_reason:
            entry["kept_original"] = kept_original_reason
        out.append(entry)
    return out


def polish(texts_path: str, cwd: str, model: str, effort: str) -> dict:
    texts = json.loads(Path(texts_path).read_text())["texts"]
    prompt = re.sub(r"\{\{TEXTS_JSON\}\}", lambda m: json.dumps(texts, indent=2), TEMPLATE.read_text())
    result = run_with_retry(prompt, cwd, model, effort, SCHEMA, lambda raw: {"texts": normalize_wording(raw, texts)})
    return {**result, "model": model, "effort": effort}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--texts", required=True)
    ap.add_argument("--cwd", default=os.getcwd())
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--effort", default=DEFAULT_EFFORT)
    a = ap.parse_args()
    run_main(lambda: polish(a.texts, a.cwd, a.model, a.effort))


if __name__ == "__main__":
    main()
