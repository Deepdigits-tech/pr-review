import json
import sys
from pathlib import Path

import pytest

import prlib
from prlib import ReviewError, account_for_owner, parse_patch_lines, parse_pr_url


@pytest.mark.parametrize("text", [
    "https://github.com/acme-corp/api/pull/12",
    "https://github.com/acme-corp/api/pull/12/files",
    "https://github.com/acme-corp/api/pull/12?w=1",
    "  https://github.com/acme-corp/api/pull/12  ",
    "acme-corp/api#12",
])
def test_parse_pr_url_shapes(text):
    assert parse_pr_url(text) == ("acme-corp", "api", 12)


def test_parse_pr_url_rejects_non_pr():
    with pytest.raises(ReviewError):
        parse_pr_url("https://github.com/acme-corp/api/issues/12")


@pytest.mark.parametrize("owner,account", [
    ("alice-work", "alice-work"),
    ("Alice-Work", "alice-work"),
    ("example-org", "alice-dev"),
    ("acme-corp", "alice-dev"),
    ("ACME-Corp", "alice-dev"),
])
def test_account_for_owner(owner, account):
    assert account_for_owner(owner) == account


def test_account_for_unknown_owner_stops():
    with pytest.raises(ReviewError, match="stranger-org") as exc:
        account_for_owner("stranger-org")
    assert "owner_accounts" in str(exc.value)


def test_parse_patch_lines():
    patch = (
        "@@ -10,4 +10,5 @@ def f():\n"
        " keep = 1\n"      # old 10 / new 10 (context)
        "-old = 2\n"       # old 11
        "+new = 2\n"       # new 11
        "+extra = 3\n"     # new 12
        " tail = 4\n"      # old 12 / new 13
        "\\ No newline at end of file\n"
    )
    right, left = parse_patch_lines(patch)
    assert right == {10, 11, 12, 13}
    assert left == {11}


def test_parse_patch_lines_two_hunks():
    patch = "@@ -1 +1 @@\n-a\n+b\n@@ -50,2 +50,2 @@\n x\n-y\n+z\n"
    right, left = parse_patch_lines(patch)
    assert right == {1, 50, 51}
    assert left == {1, 51}


def test_parse_patch_lines_no_patch():
    assert parse_patch_lines(None) == (set(), set())


def test_parse_patch_lines_form_feed_in_context():
    """Regression: splitlines() splits on \x0c (form feed), breaking line tracking."""
    patch = "@@ -1,3 +1,3 @@\n a\x0cb\n-x\n+y\n z\n"
    right, left = parse_patch_lines(patch)
    assert right == {1, 2, 3}
    assert left == {2}


def test_parse_patch_lines_u2028_in_context():
    """Regression: splitlines() splits on U+2028 (line separator), breaking line tracking."""
    patch = "@@ -1,3 +1,3 @@\n context line\n-x\n+y\n z\n"
    right, left = parse_patch_lines(patch)
    assert right == {1, 2, 3}
    assert left == {2}


def test_parse_patch_lines_trailing_newline():
    """Patch ending with \\n should not create a phantom context line."""
    patch_with_newline = "@@ -1 +1 @@\n-a\n+b\n"
    patch_without_newline = "@@ -1 +1 @@\n-a\n+b"
    right_with, left_with = parse_patch_lines(patch_with_newline)
    right_without, left_without = parse_patch_lines(patch_without_newline)
    assert right_with == right_without
    assert left_with == left_without
    assert right_with == {1}
    assert left_with == {1}


def test_run_main_success(capsys):
    prlib.run_main(lambda: {"result": "ok"})
    captured = capsys.readouterr()
    data = json.loads(captured.out)
    assert data == {"result": "ok"}


def test_run_main_review_error(capsys):
    with pytest.raises(SystemExit) as exc:
        prlib.run_main(lambda: (_ for _ in ()).throw(ReviewError("bad input")))
    assert exc.value.code == 2
    captured = capsys.readouterr()
    data = json.loads(captured.out)
    assert data == {"error": "bad input"}


def test_run_main_file_not_found(capsys):
    with pytest.raises(SystemExit) as exc:
        prlib.run_main(lambda: (_ for _ in ()).throw(FileNotFoundError("no file")))
    assert exc.value.code == 2
    captured = capsys.readouterr()
    data = json.loads(captured.out)
    assert data["error"].startswith("FileNotFoundError")


def test_run_main_value_error(capsys):
    with pytest.raises(SystemExit) as exc:
        prlib.run_main(lambda: (_ for _ in ()).throw(ValueError("bad value")))
    assert exc.value.code == 2
    captured = capsys.readouterr()
    data = json.loads(captured.out)
    assert data["error"].startswith("ValueError")


from prlib import commit_identity, is_own_account, may_fix_author, repo_url


@pytest.mark.parametrize("login,own", [
    ("alice-dev", True), ("Alice-Dev", True), ("alice-work", True), ("ALICE-WORK", True),
    ("bob-dev", False), ("ghost", False), (None, False), ("", False),
])
def test_is_own_account(login, own):
    assert is_own_account(login) is own


@pytest.mark.parametrize("login,ok", [("bob-dev", True), ("BOB-Dev", True),
                                      ("carol-dev", False), ("alice-dev", False), (None, False)])
def test_may_fix_author(login, ok):
    assert may_fix_author(login) is ok


def test_commit_identity():
    assert commit_identity("alice-dev") == ("Alice Example", "alice@example.com")
    assert commit_identity("alice-work") == ("Alice Example", "alice@work.example.com")
    with pytest.raises(ReviewError, match="someone-else") as exc:
        commit_identity("someone-else")
    assert "commit_identities" in str(exc.value)


def test_repo_url():
    assert repo_url("acme-corp", "web-app") == "https://github.com/acme-corp/web-app.git"


def test_missing_config_names_the_path(tmp_path, monkeypatch):
    missing = tmp_path / "nowhere" / "config.json"
    monkeypatch.setenv("PR_REVIEW_CONFIG", str(missing))
    prlib._CONFIG_CACHE.clear()
    with pytest.raises(ReviewError, match="config.example.json") as exc:
        account_for_owner("acme-corp")
    assert str(missing) in str(exc.value)
    assert "No pr-review config at" in str(exc.value)


@pytest.mark.parametrize("text", ["{not json", "[1, 2]", '{"owner_accounts": []}',
                                  '{"owner_accounts": {}, "own_accounts": "alice"}', "{}"])
def test_malformed_config_is_a_review_error(tmp_path, monkeypatch, text):
    bad = tmp_path / "bad.json"
    bad.write_text(text)
    monkeypatch.setenv("PR_REVIEW_CONFIG", str(bad))
    prlib._CONFIG_CACHE.clear()
    with pytest.raises(ReviewError, match="config"):
        is_own_account("alice-dev")


def test_env_override_is_honoured(tmp_path, monkeypatch):
    other = tmp_path / "other.json"
    other.write_text(json.dumps({
        "owner_accounts": {"zeta-org": "zed"}, "own_accounts": ["zed"],
        "commit_identities": {"zed": ["Zed", "zed@example.com"]}, "fix_allowed_authors": ["yan"],
    }))
    monkeypatch.setenv("PR_REVIEW_CONFIG", str(other))
    assert account_for_owner("zeta-org") == "zed"
    assert is_own_account("ZED") and not is_own_account("alice-dev")
    assert commit_identity("zed") == ("Zed", "zed@example.com")
    assert may_fix_author("yan") and not may_fix_author("bob-dev")
    with pytest.raises(ReviewError):
        account_for_owner("acme-corp")


def test_config_is_cached_per_path(tmp_path, monkeypatch):
    cfg = tmp_path / "c.json"
    cfg.write_text(json.dumps({"owner_accounts": {"a": "x"}, "own_accounts": [],
                               "commit_identities": {}, "fix_allowed_authors": []}))
    monkeypatch.setenv("PR_REVIEW_CONFIG", str(cfg))
    assert account_for_owner("a") == "x"
    cfg.write_text("garbage")  # already read: not read again
    assert account_for_owner("a") == "x"


def test_default_config_location(monkeypatch):
    monkeypatch.delenv("PR_REVIEW_CONFIG", raising=False)
    assert prlib.config_path() == Path.home() / ".config" / "pr-review" / "config.json"


def test_example_config_loads(monkeypatch):
    example = Path(__file__).resolve().parent.parent / "config.example.json"
    monkeypatch.setenv("PR_REVIEW_CONFIG", str(example))
    prlib._CONFIG_CACHE.clear()
    assert account_for_owner("acme-corp") == "alice-dev"
    assert may_fix_author("bob-dev")
