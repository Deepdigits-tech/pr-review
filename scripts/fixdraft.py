#!/usr/bin/env python3
"""Save, render and edit fix drafts (~/pr-reviews/<owner>-<repo>-<n>.fix.json)."""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import prlib  # noqa: E402
from prlib import ReviewError, run_main  # noqa: E402

REVIEWS_DIR = prlib.REVIEWS_DIR
STATUSES = ("fixed", "wont_fix", "covered", "you_decide")
KINDS = ("thread", "ours", "summary")
EMPTY_PUBLISHED = {"pushed": None, "replies": {}, "summary_comment": None,
                   "resolved": [], "rerequested": [], "done": False}


def fix_paths(owner: str, repo: str, number: int) -> tuple[Path, Path]:
    stem = f"{owner}-{repo}-{number}.fix"
    return REVIEWS_DIR / f"{stem}.json", REVIEWS_DIR / f"{stem}.md"


def validate(d: dict) -> None:
    for key in ("pr", "account", "copy_path", "rounds", "codex_checked", "items"):
        if key not in d:
            raise ReviewError(f"fix draft is missing {key!r}")
    for key in ("owner", "repo", "number", "url", "head", "base_sha"):
        if key not in d["pr"]:
            raise ReviewError(f"fix draft pr is missing {key!r}")
    dry = d.get("dry_run")
    if dry is not None and not (isinstance(dry, dict) and isinstance(dry.get("head"), str)):
        raise ReviewError("fix draft dry_run must be {'at': <time>, 'head': <sha>}")
    ids = [i["id"] for i in d["items"]]
    if len(ids) != len(set(ids)):
        raise ReviewError("fix draft has a duplicate item id")
    for i in d["items"]:
        if i.get("status") not in STATUSES:
            raise ReviewError(f"item {i['id']} has status {i.get('status')!r}; use one of {STATUSES}")
        if i.get("kind") not in KINDS:
            raise ReviewError(f"item {i['id']} has kind {i.get('kind')!r}; use one of {KINDS}")
        if i["status"] == "fixed" and not i.get("commit"):
            raise ReviewError(f"item {i['id']} is fixed but has no commit")
        if i["status"] == "covered" and i.get("covered_by") not in ids:
            raise ReviewError(f"item {i['id']} is covered but covered_by {i.get('covered_by')!r} is not an item")
        if i.get("covered_by") == i["id"]:
            raise ReviewError(f"item {i['id']} cannot be covered by itself")
        if i["kind"] == "thread" and not (i.get("thread_id") and i.get("comment_id")):
            raise ReviewError(f"item {i['id']} is a thread item and needs thread_id and comment_id")
        if i["kind"] == "ours":
            if i.get("reply"):
                raise ReviewError(f"item {i['id']} is one of ours (private self-check) and must not have a reply")
        elif not (i.get("reply") or "").strip() and not i.get("skip_reply"):
            raise ReviewError(f"item {i['id']} needs a reply (or skip it)")


def scorecard(d: dict) -> str:
    items = d["items"]
    count = lambda s: sum(1 for i in items if i["status"] == s)  # noqa: E731
    parts = [f"{len(items)} items", f"{count('fixed')} fixed"]
    if count("wont_fix"):
        parts.append(f"{count('wont_fix')} won't fix")
    if count("you_decide"):
        parts.append(f"{count('you_decide')} you decide")
    if count("covered"):
        parts.append(f"{count('covered')} covered by another fix")
    if d["codex_checked"]:
        r = d["rounds"]
        parts.append(f"stopped after {r} round{'s' if r != 1 else ''}")
    else:
        parts.append("NOT CHECKED BY CODEX")
    skipped = sum(1 for i in items if i.get("skip_reply"))
    if skipped:
        parts.append(f"{skipped} repl{'ies' if skipped != 1 else 'y'} skipped")
    return " · ".join(parts)


def _row(i: dict) -> str:
    what = {"fixed": f"fixed in `{i.get('commit')}`", "covered": f"covered by item {i.get('covered_by')}",
            "wont_fix": "won't fix", "you_decide": "you decide"}[i["status"]]
    return f"| {i['id']} | {i['source']} | `{i['path']}:{i['line']}` | {what} | {i.get('test') or ''} |"


def render_markdown(d: dict) -> str:
    pr = d["pr"]
    out = ["---", f"pr: {pr['url']}", f"branch: {pr['head']}", f"base_sha: {pr['base_sha']}",
           f"copy: {d['copy_path']}", f"published: {'yes' if d.get('published', {}).get('done') else 'no'}", "---", "",
           f"# Fixes — #{pr['number']}", "", f"**Scorecard:** {scorecard(d)}", ""]
    decide = [i for i in d["items"] if i["status"] == "you_decide"]
    if decide:
        out += ["## You decide", ""]
        for i in decide:
            out += [f"### {i['id']}. `{i['path']}:{i['line']}` ({i['source']})", "", i["body"], "",
                    f"> **Both sides:** {i.get('debate') or ''}", ""]
    out += ["## Items", "", "| # | from | where | result | test |", "|---|---|---|---|---|"]
    out += [_row(i) for i in d["items"]]
    out += ["", "## Replies", ""]
    groups: dict[str, list[dict]] = {}
    for i in d["items"]:
        if i["kind"] == "ours":
            continue
        key = "your own notes" if i.get("own") else i["source"]
        groups.setdefault(key, []).append(i)
    for key in sorted(groups, key=lambda k: (k == "your own notes", k)):
        out += [f"### Replies to {key}", ""]
        for i in groups[key]:
            flag = " — SKIPPED" if i.get("skip_reply") else ""
            out += [f"**{i['id']}.** `{i['path']}:{i['line']}`{flag}", "", i.get("reply") or "", ""]
    return "\n".join(out)


def _write(d: dict, json_path: Path, md_path: Path) -> dict:
    json_path.write_text(json.dumps(d, indent=2))
    md_path.write_text(render_markdown(d))
    return {"json": str(json_path), "md": str(md_path), "scorecard": scorecard(d)}


def _in_progress(published: dict) -> bool:
    started = published.get("pushed") or any(
        published.get(k) for k in ("replies", "resolved", "rerequested", "summary_comment"))
    return bool(started) and not published.get("done")


def save(d: dict) -> dict:
    validate(d)
    d.setdefault("published", json.loads(json.dumps(EMPTY_PUBLISHED)))
    REVIEWS_DIR.mkdir(parents=True, exist_ok=True)
    json_path, md_path = fix_paths(d["pr"]["owner"], d["pr"]["repo"], d["pr"]["number"])
    kept = None
    if json_path.exists():
        # A re-save mid-publish must not forget what is already on GitHub, or a re-run posts duplicates.
        try:
            prev = json.loads(json_path.read_text())
        except (ValueError, OSError):
            prev = {}
        old = prev.get("published") or {}
        if _in_progress(old):
            carried = {k: old.get(k, v) for k, v in EMPTY_PUBLISHED.items()}
            carried["done"] = False
            # A pushed sha only means something for the same base and the same copy.
            same = (prev.get("pr") or {}).get("base_sha") == d["pr"]["base_sha"] \
                and prev.get("copy_path") == d["copy_path"]
            if not same:
                carried["pushed"] = None
            d["published"] = carried
        else:
            d["published"] = json.loads(json.dumps(EMPTY_PUBLISHED))
        v = 1
        while json_path.with_name(f"{json_path.stem}-v{v}.json").exists():
            v += 1
        kept_json = json_path.with_name(f"{json_path.stem}-v{v}.json")
        json_path.rename(kept_json)
        if md_path.exists():
            md_path.rename(md_path.with_name(f"{md_path.stem}-v{v}.md"))
        kept = str(kept_json)
    return {**_write(d, json_path, md_path), "kept_previous": kept}


def _load(path: str) -> tuple[dict, Path, Path]:
    p = Path(path)
    d = json.loads(p.read_text())
    validate(d)
    return d, p, p.with_suffix(".md")


def _set_skip(path: str, ids: list[int], value: bool) -> dict:
    d, json_path, md_path = _load(path)
    known = {i["id"] for i in d["items"]}
    unknown = [x for x in ids if x not in known]
    if unknown:
        raise ReviewError(f"no item with id {unknown[0]}")
    for i in d["items"]:
        if i["id"] in ids:
            i["skip_reply"] = value
    validate(d)
    return _write(d, json_path, md_path)


def skip(path: str, ids: list[int]) -> dict:
    return _set_skip(path, ids, True)


def unskip(path: str, ids: list[int]) -> dict:
    return _set_skip(path, ids, False)


def render(path: str) -> dict:
    d, json_path, md_path = _load(path)
    return _write(d, json_path, md_path)


USAGE = "usage: fixdraft.py save <file> | skip <fix.json> <id>... | unskip <fix.json> <id>... | render <fix.json>"


def main() -> None:
    args = sys.argv[1:]
    if len(args) < 2 or args[0] not in ("save", "skip", "unskip", "render") \
            or (args[0] in ("skip", "unskip") and len(args) < 3):
        print(json.dumps({"error": USAGE}))
        sys.exit(2)
    cmd, path, rest = args[0], args[1], args[2:]
    if cmd == "save":
        run_main(lambda: save(json.loads(Path(path).read_text())))
    elif cmd in ("skip", "unskip"):
        fn = skip if cmd == "skip" else unskip
        run_main(lambda: fn(path, [int(x.strip(",")) for x in rest]))
    else:
        run_main(lambda: render(path))


if __name__ == "__main__":
    main()
