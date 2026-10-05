#!/usr/bin/env python3
"""Ask Codex (read-only, inside the fix copy) whether each fix really fixes its item. Prints JSON."""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from codex_check import DEFAULT_EFFORT, DEFAULT_MODEL, SEVERITIES, run_with_retry  # noqa: E402
from prlib import run_main  # noqa: E402

SKILL_DIR = Path(__file__).resolve().parent.parent
SCHEMA = SKILL_DIR / "fix-verdict.schema.json"
TEMPLATE = SKILL_DIR / "fix-check.md"
ALL_VERDICTS = ("fixed", "not_fixed", "broke_something", "agree", "disagree", "unsure")
# Codex sometimes answers in the other vocabulary; map it instead of failing the round.
_FOR_FIXED = {"fixed": "fixed", "not_fixed": "not_fixed", "broke_something": "broke_something",
              "agree": "fixed", "disagree": "not_fixed", "unsure": "unsure"}
_FOR_WONT = {"agree": "agree", "disagree": "disagree", "unsure": "unsure"}


def normalize_fix_verdict(raw: dict, items: list[dict]) -> dict:
    if not isinstance(raw, dict) or not isinstance(raw.get("verdicts"), list) \
            or not isinstance(raw.get("new_issues"), list):
        raise ValueError("Codex answer is missing 'verdicts' or 'new_issues'")
    status = {i["id"]: i["status"] for i in items}
    by_id: dict[int, dict] = {}
    for v in raw["verdicts"]:
        if not isinstance(v, dict) or v.get("verdict") not in ALL_VERDICTS:
            raise ValueError(f"bad verdict item {v!r}")
        vid = v.get("id")
        if vid in status and vid not in by_id:
            table = _FOR_WONT if status[vid] == "wont_fix" else _FOR_FIXED
            by_id[vid] = {"id": vid, "verdict": table.get(v["verdict"], "unsure"), "reason": v.get("reason", "")}
    verdicts = [by_id.get(i["id"], {"id": i["id"], "verdict": "unsure",
                                    "reason": "Codex did not answer for this item."}) for i in items]
    for m in raw["new_issues"]:
        if not isinstance(m, dict) or m.get("severity") not in SEVERITIES or not isinstance(m.get("line"), int):
            raise ValueError(f"bad new issue {m!r}")
    return {"verdicts": verdicts, "new_issues": raw["new_issues"], "overall": raw.get("overall", "")}


def should_stop_fix(verdict: dict) -> bool:
    ok = all(v["verdict"] in ("fixed", "agree") for v in verdict["verdicts"])
    serious = any(m["severity"] in ("blocker", "should-fix") for m in verdict["new_issues"])
    return ok and not serious


def build_fix_prompt(template: str, ctx: dict, fixlist: dict, round_no: int) -> str:
    values = {
        "TITLE": ctx["pr"]["title"],
        "BASE_SHA": fixlist["base_sha"],
        "ROUND": str(round_no),
        "ITEMS_JSON": json.dumps(fixlist["items"], indent=2),
    }
    return re.sub(r"\{\{(\w+)\}\}", lambda m: values[m.group(1)], template)


def check_fixes(fixlist_path: str, ctx_path: str, round_no: int, cwd: str, model: str, effort: str) -> dict:
    ctx = json.loads(Path(ctx_path).read_text())
    fixlist = json.loads(Path(fixlist_path).read_text())
    items = fixlist["items"]
    prompt = build_fix_prompt(TEMPLATE.read_text(), ctx, fixlist, round_no)
    verdict = run_with_retry(prompt, cwd, model, effort, SCHEMA, lambda raw: normalize_fix_verdict(raw, items))
    return {**verdict, "stop": should_stop_fix(verdict), "model": model, "effort": effort}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--fixlist", required=True)
    ap.add_argument("--context", required=True)
    ap.add_argument("--round", type=int, required=True)
    ap.add_argument("--cwd", default=os.getcwd())
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--effort", default=DEFAULT_EFFORT)
    a = ap.parse_args()
    run_main(lambda: check_fixes(a.fixlist, a.context, a.round, a.cwd, a.model, a.effort))


if __name__ == "__main__":
    main()
