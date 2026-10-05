#!/usr/bin/env python3
"""Make, find and remove the separate copy a PR is fixed in. Prints JSON."""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import prlib  # noqa: E402
from prlib import ReviewError, commit_identity, repo_url, run_main  # noqa: E402

REVIEWS_DIR = prlib.REVIEWS_DIR


def _git(cwd: str, *args: str) -> str:
    r = subprocess.run(["git", "-C", cwd, *args], capture_output=True, text=True)
    if r.returncode:
        raise ReviewError(f"git {' '.join(args[:2])} failed: {(r.stderr or r.stdout).strip()[-500:]}")
    return r.stdout


def _load(ctx_path: str) -> dict:
    return json.loads(Path(ctx_path).read_text())


def copy_path(ctx: dict) -> Path:
    pr = ctx["pr"]
    return REVIEWS_DIR / ".fix" / f"{pr['owner']}-{pr['repo']}-{pr['number']}"


def _config_path() -> Path:
    return REVIEWS_DIR / ".config.json"


def _read_config() -> dict:
    p = _config_path()
    return json.loads(p.read_text()) if p.exists() else {}


def detect_test_command(names: set[str]) -> str | None:
    if "uv.lock" in names:
        return "uv run pytest -q"
    if "poetry.lock" in names:
        return "poetry run pytest -q"
    if "package.json" in names:
        return "npm test"
    if "pyproject.toml" in names or "pytest.ini" in names:
        return "python3 -m pytest -q"
    return None


def _test_command(ctx: dict, path: Path) -> str | None:
    key = f"{ctx['pr']['owner']}/{ctx['pr']['repo']}"
    saved = _read_config().get("test_commands", {}).get(key)
    if saved:
        return saved
    names = {p.name for p in path.iterdir()} if path.exists() else set()
    return detect_test_command(names)


def create(ctx_path: str, cwd: str, replace: bool = False) -> dict:
    ctx = _load(ctx_path)
    pr = ctx["pr"]
    path = copy_path(ctx)
    if path.exists():
        if not replace:
            raise ReviewError(
                f"A fix copy already exists at {path}. Finish or publish that one first, or re-run with "
                "--replace to start over (its unpushed commits are lost)."
            )
        _git(cwd, "worktree", "remove", "--force", str(path))
    path.parent.mkdir(parents=True, exist_ok=True)
    _git(cwd, "fetch", "--quiet", repo_url(pr["owner"], pr["repo"]), f"pull/{pr['number']}/head")
    _git(cwd, "worktree", "prune")  # a copy deleted by hand leaves a stale entry that blocks `add`
    _git(cwd, "worktree", "add", "--detach", str(path), pr["head_sha"])
    name, email = commit_identity(ctx["account"])
    return {"path": str(path), "head_sha": pr["head_sha"], "branch": pr["head"],
            "commit_name": name, "commit_email": email, "test_command": _test_command(ctx, path)}


def main_checkout(copy: Path) -> Path:
    common = _git(str(copy), "rev-parse", "--path-format=absolute", "--git-common-dir").strip()
    return Path(common).parent


def remove(ctx_path: str, cwd: str) -> dict:
    path = copy_path(_load(ctx_path))
    if not path.exists():
        return {"removed": None}
    _git(cwd, "worktree", "remove", "--force", str(path))
    return {"removed": str(path)}


def set_test_command(ctx_path: str, command: str) -> dict:
    ctx = _load(ctx_path)
    key = f"{ctx['pr']['owner']}/{ctx['pr']['repo']}"
    cfg = _read_config()
    cfg.setdefault("test_commands", {})[key] = command
    REVIEWS_DIR.mkdir(parents=True, exist_ok=True)
    _config_path().write_text(json.dumps(cfg, indent=2))
    return {"repo": key, "test_command": command}


def main() -> None:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("create")
    c.add_argument("ctx")
    c.add_argument("--cwd", default=os.getcwd())
    c.add_argument("--replace", action="store_true")
    r = sub.add_parser("remove")
    r.add_argument("ctx")
    r.add_argument("--cwd", default=os.getcwd())
    s = sub.add_parser("set-test")
    s.add_argument("ctx")
    s.add_argument("command")
    a = ap.parse_args()
    if a.cmd == "create":
        run_main(lambda: create(a.ctx, a.cwd, a.replace))
    elif a.cmd == "remove":
        run_main(lambda: remove(a.ctx, a.cwd))
    else:
        run_main(lambda: set_test_command(a.ctx, a.command))


if __name__ == "__main__":
    main()
