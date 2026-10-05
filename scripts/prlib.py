"""Shared helpers for the pr-review skill. Standard library only."""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable

REVIEWS_DIR = Path(os.environ.get("PR_REVIEWS_DIR", str(Path.home() / "pr-reviews")))

class ReviewError(Exception):
    """A problem the owner must act on. The message is shown as-is."""


# Who-is-who lives in a config file outside the skill folder (see config.example.json).
CONFIG_ENV = "PR_REVIEW_CONFIG"
_CONFIG_CACHE: dict[str, dict] = {}


def config_path() -> Path:
    override = os.environ.get(CONFIG_ENV)
    return Path(override).expanduser() if override else Path.home() / ".config" / "pr-review" / "config.json"


def _str_list(cfg: dict, key: str, required: bool) -> list[str]:
    if key not in cfg and not required:
        return []
    value = cfg.get(key)
    if not isinstance(value, list) or not all(isinstance(v, str) and v for v in value):
        raise ValueError(f'"{key}" must be a list of GitHub logins')
    return value


def _parse_config(raw: Any) -> dict:
    if not isinstance(raw, dict):
        raise ValueError("the file must hold one JSON object")
    owners = raw.get("owner_accounts")
    if not isinstance(owners, dict) or not all(isinstance(k, str) and isinstance(v, str) and v
                                               for k, v in owners.items()):
        raise ValueError('"owner_accounts" must map a repo owner to a GitHub login')
    identities = raw.get("commit_identities")
    if not isinstance(identities, dict) or not all(
            isinstance(v, list) and len(v) == 2 and all(isinstance(x, str) and x for x in v)
            for v in identities.values()):
        raise ValueError('"commit_identities" must map a login to [name, email]')
    return {
        "owner_accounts": {k.lower(): v for k, v in owners.items()},
        "own_accounts": {a.lower() for a in _str_list(raw, "own_accounts", True)},
        "commit_identities": {k.lower(): (v[0], v[1]) for k, v in identities.items()},
        "fix_allowed_authors": {a.lower() for a in _str_list(raw, "fix_allowed_authors", False)},
    }


def load_config() -> dict:
    """The owner's settings, read once per config path. Raises ReviewError when missing or malformed."""
    path = config_path()
    key = str(path)
    if key not in _CONFIG_CACHE:
        try:
            text = path.read_text()
        except FileNotFoundError:
            raise ReviewError(
                f"No pr-review config at {path}. Copy config.example.json from the skill folder "
                "to that path and fill in your accounts."
            ) from None
        except OSError as e:
            raise ReviewError(f"The pr-review config at {path} cannot be read: {e}") from None
        try:
            _CONFIG_CACHE[key] = _parse_config(json.loads(text))
        except ValueError as e:  # includes json.JSONDecodeError
            raise ReviewError(
                f"The pr-review config at {path} is not valid: {e}. "
                "See config.example.json in the skill folder."
            ) from None
    return _CONFIG_CACHE[key]


_URL_RE = re.compile(r"github\.com/([^/\s]+)/([^/\s?#]+)/pull/(\d+)")
_SHORT_RE = re.compile(r"^([\w.-]+)/([\w.-]+)#(\d+)$")
_HUNK_RE = re.compile(r"^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@")


def parse_pr_url(text: str) -> tuple[str, str, int]:
    text = text.strip()
    m = _URL_RE.search(text) or _SHORT_RE.match(text)
    if not m:
        raise ReviewError(f"Not a GitHub PR link: {text!r}")
    repo = m.group(2)
    if repo.endswith(".git"):
        repo = repo[:-4]
    return m.group(1), repo, int(m.group(3))


def account_for_owner(owner: str) -> str:
    """The gh login that may post on repos owned by `owner` (config: owner_accounts)."""
    try:
        return load_config()["owner_accounts"][owner.lower()]
    except KeyError:
        raise ReviewError(
            f"No GitHub account is set for repos owned by {owner!r}. "
            f"Add it to owner_accounts in {config_path()}."
        ) from None


def is_own_account(login: str | None) -> bool:
    """The owner's own GitHub logins (config: own_accounts). PRs they opened get the private
    self-check and the fix step."""
    return bool(login) and login.lower() in load_config()["own_accounts"]


def commit_identity(account: str) -> tuple[str, str]:
    """Commit author for fix commits, per posting account (config: commit_identities). The fix copy
    has no git identity of its own."""
    try:
        return load_config()["commit_identities"][account.lower()]
    except KeyError:
        raise ReviewError(
            f"No commit identity is set for {account!r}. Add it to commit_identities in {config_path()}."
        ) from None


def repo_url(owner: str, repo: str) -> str:
    return f"https://github.com/{owner}/{repo}.git"


def _gh_token(account: str) -> str:
    r = subprocess.run(["gh", "auth", "token", "-u", account], capture_output=True, text=True)
    if r.returncode or not r.stdout.strip():
        raise ReviewError(f"gh has no login for {account!r}. Run: gh auth login")
    return r.stdout.strip()


def gh_api(account: str, path: str, *, method: str = "GET", body: dict | None = None,
           accept: str | None = None, slurp: bool = False) -> Any:
    cmd = ["gh", "api", path, "--method", method]
    if accept:
        cmd += ["-H", f"Accept: {accept}"]
    if slurp:
        cmd += ["--paginate", "--slurp"]
    if body is not None:
        cmd += ["--input", "-"]
    child_env = {**os.environ, "GH_TOKEN": _gh_token(account)}
    r = subprocess.run(cmd, input=json.dumps(body) if body is not None else None,
                       capture_output=True, text=True, env=child_env)
    if r.returncode:
        detail = (r.stderr or r.stdout).strip()[:500]
        raise ReviewError(f"GitHub refused {method} {path} as {account}: {detail}")
    if accept and "diff" in accept:
        return r.stdout
    data = json.loads(r.stdout) if r.stdout.strip() else None
    if slurp:
        data = [item for page in data for item in page]
    return data


def parse_patch_lines(patch: str | None) -> tuple[set[int], set[int]]:
    """Lines GitHub accepts inline comments on: RIGHT = new-file lines in a hunk
    (added or context), LEFT = deleted old-file lines."""
    right: set[int] = set()
    left: set[int] = set()
    if not patch:
        return right, left
    old = new = 0
    lines = patch.split("\n")
    # Ignore trailing empty element from split (patch ending with \n must not add a phantom line)
    if lines and lines[-1] == "":
        lines = lines[:-1]
    for line in lines:
        m = _HUNK_RE.match(line)
        if m:
            old, new = int(m.group(1)), int(m.group(2))
            continue
        if line.startswith("\\"):
            continue
        if line.startswith("+"):
            right.add(new)
            new += 1
        elif line.startswith("-"):
            left.add(old)
            old += 1
        else:
            right.add(new)
            old += 1
            new += 1
    return right, left


def run_main(fn: Callable[[], dict]) -> None:
    try:
        print(json.dumps(fn(), indent=2))
    except ReviewError as e:
        print(json.dumps({"error": str(e)}))
        sys.exit(2)
    except (OSError, ValueError, KeyError, TypeError) as e:
        print(json.dumps({"error": f"{type(e).__name__}: {e}"}))
        sys.exit(2)


def may_fix_author(login: str | None) -> bool:
    """Teammates who agreed that the owner's reviews may push fixes to their PR branches
    (config: fix_allowed_authors)."""
    return bool(login) and login.lower() in load_config()["fix_allowed_authors"]
