#!/usr/bin/env python3
"""Ask Codex (read-only) to check Claude's review notes. Prints JSON."""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Callable

sys.path.insert(0, str(Path(__file__).resolve().parent))
from prlib import ReviewError, run_main  # noqa: E402

SKILL_DIR = Path(__file__).resolve().parent.parent
SCHEMA = SKILL_DIR / "codex-verdict.schema.json"
TEMPLATE = SKILL_DIR / "codex-check.md"
DEFAULT_MODEL = "gpt-6-sol"
DEFAULT_EFFORT = "xhigh"
CODEX_TIMEOUT_S = 1500
VERDICTS = ("agree", "disagree", "unsure")
SEVERITIES = ("blocker", "should-fix", "nit")


def normalize_verdict(raw: dict, note_ids: list[int]) -> dict:
    if not isinstance(raw, dict) or not isinstance(raw.get("verdicts"), list) or not isinstance(raw.get("missed"), list):
        raise ValueError("Codex answer is missing 'verdicts' or 'missed'")
    by_id = {}
    for v in raw["verdicts"]:
        if not isinstance(v, dict):
            raise ValueError(f"bad verdict item {v!r} is not a dict")
        if v.get("verdict") not in VERDICTS:
            raise ValueError(f"bad verdict {v.get('verdict')!r}")
        if v.get("id") in note_ids and v["id"] not in by_id:
            by_id[v["id"]] = {"id": v["id"], "verdict": v["verdict"], "reason": v.get("reason", "")}
    verdicts = [by_id.get(i, {"id": i, "verdict": "unsure", "reason": "Codex did not answer for this note."})
                for i in note_ids]
    for m in raw["missed"]:
        if not isinstance(m, dict):
            raise ValueError(f"bad missed item {m!r} is not a dict")
        if m.get("severity") not in SEVERITIES or not isinstance(m.get("line"), int):
            raise ValueError(f"bad missed item {m!r}")
    return {"verdicts": verdicts, "missed": raw["missed"], "overall": raw.get("overall", "")}


def should_stop(verdict: dict) -> bool:
    all_agree = all(v["verdict"] == "agree" for v in verdict["verdicts"])
    serious_missed = any(m["severity"] in ("blocker", "should-fix") for m in verdict["missed"])
    return all_agree and not serious_missed


def normalize_notes(raw) -> dict:
    """Accept the old shape (a list of notes) or {"notes": [...], "rejected_missed": [...]}."""
    if isinstance(raw, list):
        return {"notes": raw, "rejected_missed": []}
    if isinstance(raw, dict) and isinstance(raw.get("notes"), list):
        rejected = raw.get("rejected_missed", [])
        if not isinstance(rejected, list):
            raise ReviewError("notes file: 'rejected_missed' must be a list")
        return {"notes": raw["notes"], "rejected_missed": rejected}
    raise ReviewError("notes file must be a JSON list of notes, or an object with a 'notes' list "
                      "(and optional 'rejected_missed')")


def build_prompt(template: str, ctx: dict, notes, round_no: int) -> str:
    values = {
        "TITLE": ctx["pr"]["title"],
        "BODY": ctx["pr"].get("body") or "(no description)",
        "DIFF_PATH": ctx["diff_path"],
        "HEAD_SHA": ctx["pr"]["head_sha"],
        "ROUND": str(round_no),
        "NOTES_JSON": json.dumps(normalize_notes(notes), indent=2),
    }
    return re.sub(r"\{\{(\w+)\}\}", lambda m: values[m.group(1)], template)


def _run_codex(prompt: str, cwd: str, model: str, effort: str, schema: Path = SCHEMA) -> dict:
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "verdict.json"
        cmd = ["codex", "exec", "-m", model, "-c", f'model_reasoning_effort="{effort}"',
               "--sandbox", "read-only", "-C", cwd,
               "--output-schema", str(schema), "-o", str(out), prompt]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=CODEX_TIMEOUT_S, stdin=subprocess.DEVNULL)
        if r.returncode:
            # Auth failure, model refused, ...: retrying will not help, so fail fast.
            detail = (r.stderr or r.stdout or "").strip()[-800:] or f"codex exited {r.returncode}"
            raise ReviewError(f"Codex failed: {detail}")
        if not out.exists():
            raise RuntimeError("codex wrote no answer")
        return json.loads(out.read_text())


def run_with_retry(prompt: str, cwd: str, model: str, effort: str, schema: Path,
                   normalize: Callable[[dict], dict]) -> dict:
    errors: list[str] = []
    # Retry once, and only for a timeout, an unparsable/misshapen answer or no answer at all.
    # A nonzero codex exit raises ReviewError from _run_codex and is not retried.
    for _attempt in range(2):
        try:
            return normalize(_run_codex(prompt, cwd, model, effort, schema))
        except (RuntimeError, ValueError, json.JSONDecodeError, subprocess.TimeoutExpired) as e:
            errors.append(str(e))
    raise ReviewError(f"Codex check failed twice: {errors[0]} | then: {errors[1]}")


def check(ctx_path: str, notes_path: str, round_no: int, cwd: str, model: str, effort: str) -> dict:
    ctx = json.loads(Path(ctx_path).read_text())
    notes = normalize_notes(json.loads(Path(notes_path).read_text()))
    note_ids = [n["id"] for n in notes["notes"]]
    prompt = build_prompt(TEMPLATE.read_text(), ctx, notes, round_no)
    verdict = run_with_retry(prompt, cwd, model, effort, SCHEMA, lambda raw: normalize_verdict(raw, note_ids))
    return {**verdict, "stop": should_stop(verdict), "model": model, "effort": effort}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--context", required=True)
    ap.add_argument("--notes", required=True)
    ap.add_argument("--round", type=int, required=True)
    ap.add_argument("--cwd", default=os.getcwd())
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--effort", default=DEFAULT_EFFORT)
    a = ap.parse_args()
    run_main(lambda: check(a.context, a.notes, a.round, a.cwd, a.model, a.effort))


if __name__ == "__main__":
    main()
