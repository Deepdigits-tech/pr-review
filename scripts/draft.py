#!/usr/bin/env python3
"""Save, render and edit review drafts in ~/pr-reviews/."""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import prlib  # noqa: E402
from prlib import ReviewError, run_main  # noqa: E402

REVIEWS_DIR = prlib.REVIEWS_DIR
SEVERITIES = ("blocker", "should-fix", "nit")
STATUSES = ("agreed", "fixed", "added", "you-decide", "unchecked")
ACTIONS = ("fix", "leave")
_REQUIRED_NOTE = ("id", "path", "line", "severity", "body", "status", "keep")


def draft_paths(owner: str, repo: str, number: int) -> tuple[Path, Path]:
    stem = f"{owner}-{repo}-{number}"
    return REVIEWS_DIR / f"{stem}.json", REVIEWS_DIR / f"{stem}.md"


def _action(n: dict) -> str:
    return n.get("action", "leave")


def validate(draft: dict) -> None:
    for key in ("pr", "account", "rounds", "codex_checked", "summary", "notes"):
        if key not in draft:
            raise ReviewError(f"draft is missing {key!r}")
    for key in ("owner", "repo", "number", "url", "head_sha"):
        if key not in draft["pr"]:
            raise ReviewError(f"draft pr is missing {key!r}")
    seen = set()
    for n in draft["notes"]:
        for key in _REQUIRED_NOTE:
            if key not in n:
                raise ReviewError(f"note {n.get('id')} is missing {key!r}")
        if n["severity"] not in SEVERITIES:
            raise ReviewError(f"note {n['id']} has severity {n['severity']!r}; use one of {SEVERITIES}")
        if n["status"] not in STATUSES:
            raise ReviewError(f"note {n['id']} has status {n['status']!r}; use one of {STATUSES}")
        if n.get("side", "RIGHT") not in ("RIGHT", "LEFT"):
            raise ReviewError(f"note {n['id']} has side {n['side']!r}; use RIGHT or LEFT")
        if "title" in n and not isinstance(n["title"], str):
            raise ReviewError(f"note {n['id']} has a title that is not text")
        if _action(n) not in ACTIONS:
            raise ReviewError(f"note {n['id']} has action {n.get('action')!r}; use one of {ACTIONS}")
        for key in ("fixed_in", "fix_summary"):
            if key in n and not isinstance(n[key], str):
                raise ReviewError(f"note {n['id']} has a {key} that is not text")
        if n["id"] in seen:
            raise ReviewError(f"note id {n['id']} is used twice")
        seen.add(n["id"])
    if "dry_run" in draft and not (isinstance(draft["dry_run"], dict)
                                   and isinstance(draft["dry_run"].get("head"), str)):
        raise ReviewError("draft dry_run record is malformed")


def decide_verdict(draft: dict) -> str:
    blocker_left = any(n["keep"] and n["severity"] == "blocker" and _action(n) == "leave" for n in draft["notes"])
    return "REQUEST_CHANGES" if blocker_left else "APPROVE"


def plan_line(draft: dict) -> str:
    kept = [n for n in draft["notes"] if n["keep"]]
    verdict = decide_verdict(draft)
    fix = sum(1 for n in kept if _action(n) == "fix")
    leave = [n for n in kept if _action(n) == "leave"]
    if verdict == "APPROVE":
        issues = sum(1 for n in leave if n["severity"] == "should-fix")
        optional = sum(1 for n in leave if n["severity"] == "nit")
        open_ = 0
    else:
        issues = optional = 0
        open_ = len(leave)
    word = "issue" if issues == 1 else "issues"
    label = "Approve" if verdict == "APPROVE" else "Request changes"
    return f"{label} · fix {fix} · {issues} new {word} · {optional} optional · {open_} left open"


def scorecard(draft: dict) -> str:
    notes = draft["notes"]
    parts = [f"{len(notes)} notes"]
    if not draft["codex_checked"]:
        parts.append("NOT CHECKED BY CODEX")
    else:
        count = lambda s: sum(1 for n in notes if n["status"] == s)  # noqa: E731
        parts.append(f"{count('agreed')} both agreed")
        if count("fixed"):
            parts.append(f"{count('fixed')} fixed after review")
        if count("added"):
            parts.append(f"{count('added')} added from Codex")
        parts.append(f"{count('you-decide')} still disputed (you decide)")
        rounds = draft["rounds"]
        parts.append(f"stopped after {rounds} round{'s' if rounds != 1 else ''}")
    # A you-decide note starts with keep:false, so "not kept" means "left out until
    # kept" for those, and "dropped by you" only for the others.
    left_out = sum(1 for n in notes if not n["keep"] and n["status"] == "you-decide")
    dropped = sum(1 for n in notes if not n["keep"] and n["status"] != "you-decide")
    if dropped:
        parts.append(f"{dropped} dropped by you")
    if left_out:
        parts.append(f"{left_out} left out until you keep it")
    return " · ".join(parts)


def _order(n: dict) -> tuple[int, int, int]:
    return (0 if n["status"] == "you-decide" else 1, SEVERITIES.index(n["severity"]), n["id"])


def render_markdown(draft: dict) -> str:
    pr = draft["pr"]
    out = [
        "---",
        f"pr: {pr['url']}",
        f"head_sha: {pr['head_sha']}",
        f"account: {draft['account']}",
        f"posted: {draft['posted']['url'] if draft.get('posted') else 'no'}",
        "---",
        "",
        f"# Review — {pr.get('title', '')} (#{pr['number']})",
        "",
        f"**Scorecard:** {scorecard(draft)}",
        f"**Plan:** {plan_line(draft)}",
        "",
        "## Summary (posted)",
        "",
        draft["summary"],
        "",
        "## Notes",
        "",
    ]
    for n in sorted(draft["notes"], key=_order):
        if n["keep"]:
            flag = ""
        elif n["status"] == "you-decide":
            flag = f" — LEFT OUT (reply keep {n['id']} to include)"
        else:
            flag = " — DROPPED"
        action = _action(n)
        out.append(f"### {n['id']}. `{n['path']}:{n['line']}` · {n['severity']} · {n['status']} · {action}{flag}")
        out.append("")
        out.append(n["body"])
        if n.get("debate"):
            out += ["", f"> **Both sides:** {n['debate']}"]
        out.append("")
    return "\n".join(out)


def _write(draft: dict, json_path: Path, md_path: Path) -> dict:
    json_path.write_text(json.dumps(draft, indent=2))
    md_path.write_text(render_markdown(draft))
    return {"json": str(json_path), "md": str(md_path), "scorecard": scorecard(draft)}


def save(draft: dict) -> dict:
    validate(draft)
    draft.setdefault("posted", None)
    for n in draft["notes"]:
        n.setdefault("side", "RIGHT")
    REVIEWS_DIR.mkdir(parents=True, exist_ok=True)
    json_path, md_path = draft_paths(draft["pr"]["owner"], draft["pr"]["repo"], draft["pr"]["number"])
    kept = None
    if json_path.exists():
        v = 1
        while json_path.with_name(f"{json_path.stem}-v{v}.json").exists():
            v += 1
        kept_json = json_path.with_name(f"{json_path.stem}-v{v}.json")
        json_path.rename(kept_json)
        if md_path.exists():
            md_path.rename(md_path.with_name(f"{md_path.stem}-v{v}.md"))
        kept = str(kept_json)
    return {**_write(draft, json_path, md_path), "kept_previous": kept}


def _load(path: str) -> tuple[dict, Path, Path]:
    p = Path(path)
    draft = json.loads(p.read_text())
    validate(draft)
    return draft, p, p.with_suffix(".md")


def _set_field(path: str, ids: list[int], updates: dict) -> dict:
    draft, json_path, md_path = _load(path)
    known = {n["id"] for n in draft["notes"]}
    unknown = [i for i in ids if i not in known]
    if unknown:
        raise ReviewError(f"no note with id {unknown[0]}")
    for n in draft["notes"]:
        if n["id"] in ids:
            n.update(updates)
    draft.pop("dry_run", None)  # an edit makes any recorded dry run stale
    return _write(draft, json_path, md_path)


def _set_keep(path: str, ids: list[int], keep: bool) -> dict:
    return _set_field(path, ids, {"keep": keep})


def drop(path: str, ids: list[int]) -> dict:
    return _set_keep(path, ids, False)


def keep(path: str, ids: list[int]) -> dict:
    return _set_keep(path, ids, True)


def set_action(path: str, action: str, ids: list[int]) -> dict:
    if action not in ACTIONS:
        raise ReviewError(f"action must be one of {ACTIONS}")
    result = _set_field(path, ids, {"action": action})
    if action == "leave":
        # Remove stale fix metadata when leaving a note
        draft, json_path, md_path = _load(path)
        for n in draft["notes"]:
            if n["id"] in ids:
                n.pop("fixed_in", None)
                n.pop("fix_summary", None)
        result = _write(draft, json_path, md_path)
    return result


def render(path: str) -> dict:
    draft, json_path, md_path = _load(path)
    draft.pop("dry_run", None)  # a re-rendered (edited) draft needs a fresh dry run
    return _write(draft, json_path, md_path)


USAGE = ("usage: draft.py save <file> | drop <draft.json> <id>... | keep <draft.json> <id>... "
         "| action <draft.json> fix|leave <id>... | render <draft.json>")


def main() -> None:
    if len(sys.argv) < 3 or sys.argv[1] not in ("save", "drop", "keep", "action", "render"):
        print(json.dumps({"error": USAGE}))
        sys.exit(2)
    cmd, path, rest = sys.argv[1], sys.argv[2], sys.argv[3:]
    if cmd == "save":
        run_main(lambda: save(json.loads(Path(path).read_text())))
    elif cmd in ("drop", "keep"):
        if not rest:
            print(json.dumps({"error": USAGE}))
            sys.exit(2)
        fn = drop if cmd == "drop" else keep
        run_main(lambda: fn(path, [int(x.strip(",")) for x in rest]))
    elif cmd == "action":
        if not rest or len(rest) < 2:
            print(json.dumps({"error": USAGE}))
            sys.exit(2)
        action_name, ids_rest = rest[0], rest[1:]
        run_main(lambda: set_action(path, action_name, [int(x.strip(",")) for x in ids_rest]))
    else:
        run_main(lambda: render(path))


if __name__ == "__main__":
    main()
