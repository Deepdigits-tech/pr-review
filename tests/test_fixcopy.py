import json

import pytest

import fixcopy
from prlib import ReviewError

CTX = {"pr": {"owner": "acme-corp", "repo": "web-app", "number": 63, "head": "feature-branch",
              "head_sha": "b19dc43"}, "account": "alice-dev"}


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(fixcopy, "REVIEWS_DIR", tmp_path)
    calls = []

    def fake_git(cwd, *args):
        calls.append((cwd, args))
        if args[:2] == ("worktree", "add"):
            p = fixcopy.Path(args[3])
            p.mkdir(parents=True)
            (p / "uv.lock").write_text("")
        return ""

    monkeypatch.setattr(fixcopy, "_git", fake_git)
    ctx = tmp_path / "ctx.json"
    ctx.write_text(json.dumps(CTX))
    return tmp_path, calls, str(ctx)


@pytest.mark.parametrize("names,cmd", [
    ({"uv.lock", "pyproject.toml"}, "uv run pytest -q"),
    ({"poetry.lock"}, "poetry run pytest -q"),
    ({"package.json"}, "npm test"),
    ({"pyproject.toml"}, "python3 -m pytest -q"),
    ({"pytest.ini"}, "python3 -m pytest -q"),
    ({"README.md"}, None),
])
def test_detect_test_command(names, cmd):
    assert fixcopy.detect_test_command(names) == cmd


def test_create_fetches_prunes_and_adds_detached_worktree(env):
    tmp, calls, ctx = env
    out = fixcopy.create(ctx, "/proj")
    path = tmp / ".fix" / "acme-corp-web-app-63"
    assert out == {"path": str(path), "head_sha": "b19dc43", "branch": "feature-branch",
                   "commit_name": "Alice Example", "commit_email": "alice@example.com",
                   "test_command": "uv run pytest -q"}
    assert calls[0] == ("/proj", ("fetch", "--quiet", "https://github.com/acme-corp/web-app.git", "pull/63/head"))
    assert calls[1] == ("/proj", ("worktree", "prune"))
    assert calls[2] == ("/proj", ("worktree", "add", "--detach", str(path), "b19dc43"))


def test_create_refuses_existing_copy_without_replace(env):
    tmp, calls, ctx = env
    (tmp / ".fix" / "acme-corp-web-app-63").mkdir(parents=True)
    with pytest.raises(ReviewError, match="--replace"):
        fixcopy.create(ctx, "/proj")


def test_create_replace_removes_old_copy_first(env):
    tmp, calls, ctx = env
    old = tmp / ".fix" / "acme-corp-web-app-63"
    old.mkdir(parents=True)

    def fake_git(cwd, *args):
        calls.append((cwd, args))
        if args[:2] == ("worktree", "remove"):
            import shutil
            shutil.rmtree(old)
        if args[:2] == ("worktree", "add"):
            fixcopy.Path(args[3]).mkdir(parents=True)
        return ""

    fixcopy._git = fake_git
    fixcopy.create(ctx, "/proj", replace=True)
    assert calls[0] == ("/proj", ("worktree", "remove", "--force", str(old)))


def test_saved_test_command_wins_over_detection(env):
    tmp, calls, ctx = env
    assert fixcopy.set_test_command(ctx, "make test") == {"repo": "acme-corp/web-app", "test_command": "make test"}
    assert json.loads((tmp / ".config.json").read_text()) == {"test_commands": {"acme-corp/web-app": "make test"}}
    assert fixcopy.create(ctx, "/proj")["test_command"] == "make test"


def test_remove(env):
    tmp, calls, ctx = env
    assert fixcopy.remove(ctx, "/proj") == {"removed": None}
    path = tmp / ".fix" / "acme-corp-web-app-63"
    path.mkdir(parents=True)
    assert fixcopy.remove(ctx, "/proj") == {"removed": str(path)}
    assert ("/proj", ("worktree", "remove", "--force", str(path))) in calls


def test_create_replace_prunes_after_remove_and_before_add(env):
    tmp, calls, ctx = env
    old = tmp / ".fix" / "acme-corp-web-app-63"
    old.mkdir(parents=True)

    def fake_git(cwd, *args):
        calls.append((cwd, args))
        if args[:2] == ("worktree", "remove"):
            import shutil
            shutil.rmtree(old)
        if args[:2] == ("worktree", "add"):
            fixcopy.Path(args[3]).mkdir(parents=True)
        return ""

    fixcopy._git = fake_git
    fixcopy.create(ctx, "/proj", replace=True)
    kinds = [a[:2] for c, a in calls]
    assert kinds.index(("worktree", "remove")) < kinds.index(("worktree", "prune")) < kinds.index(("worktree", "add"))
