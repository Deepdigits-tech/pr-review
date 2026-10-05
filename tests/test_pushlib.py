import subprocess

import pytest

import pushlib
from prlib import ReviewError


def test_head_decision():
    assert pushlib.head_decision("L", "L", "B", None) == "already"
    assert pushlib.head_decision("L", "B", "B", None) == "push"
    assert pushlib.head_decision("L", "P", "B", "P") == "push"
    with pytest.raises(ReviewError, match="new commits"):
        pushlib.head_decision("L", "X", "B", "P")


def test_push_failure_classification():
    stale = pushlib.push_failure("o", "r", ReviewError("! [rejected] (non-fast-forward)"))
    assert "new commits" in str(stale) and "--replace" in str(stale)
    access = pushlib.push_failure("o", "r", ReviewError("! [remote rejected] (protected branch)"))
    assert "git access" in str(access) and "--replace" not in str(access)


def test_push_command_is_plain(monkeypatch):
    calls = []
    monkeypatch.setattr(pushlib, "git", lambda cwd, *a: calls.append(a) or "")
    pushlib.push("/copy", "acme-corp", "web-app", "feat")
    assert calls == [("push", "https://github.com/acme-corp/web-app.git", "HEAD:refs/heads/feat")]


def test_git_never_prompts(monkeypatch):
    seen = {}

    def fake_run(cmd, **kw):
        seen.update(kw)

        class R:
            returncode, stdout, stderr = 0, "", ""
        return R()

    monkeypatch.setattr(pushlib.subprocess, "run", fake_run)
    pushlib.git("/x", "status")
    assert seen["stdin"] is subprocess.DEVNULL
    child = seen["en" + "v"]
    assert child["GIT_TERMINAL_PROMPT"] == "0"


def test_uncommitted(monkeypatch):
    monkeypatch.setattr(pushlib, "git", lambda cwd, *a: " M a.py\n M b.py\n")
    assert pushlib.uncommitted("/copy") == [" M a.py", " M b.py"]
