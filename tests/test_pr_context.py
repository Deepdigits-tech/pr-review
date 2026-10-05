from pr_context import local_repo_matches

REMOTES = [
    "https://github.com/example-org/worker.git",
    "git@github.com:alice-work/notes.git",
]


def test_matches_https_remote_any_case():
    assert local_repo_matches(REMOTES, "Example-Org", "worker")


def test_matches_ssh_remote():
    assert local_repo_matches(REMOTES, "alice-work", "notes")


def test_wrong_project_does_not_match():
    assert not local_repo_matches(REMOTES, "acme-corp", "api")


def test_prefix_name_does_not_match():
    assert not local_repo_matches(["https://github.com/acme-corp/api-old.git"], "acme-corp", "api")


def test_build_context_writes_per_pr_ctx_file(tmp_path, monkeypatch):
    import json
    import pr_context

    monkeypatch.setattr(pr_context, "REVIEWS_DIR", tmp_path)
    monkeypatch.setattr(pr_context, "_remote_urls", lambda cwd: ["https://github.com/acme-corp/api.git"])
    monkeypatch.setattr(pr_context, "account_for_owner", lambda owner: "alice-dev")

    pr = {"state": "open", "html_url": "https://github.com/acme-corp/api/pull/7", "title": "Add x",
          "body": "why", "user": {"login": "alice-dev"}, "base": {"ref": "main"}, "head": {"ref": "feat", "sha": "abc123"}}
    files = [{"filename": "a.py", "status": "modified", "additions": 3, "deletions": 1, "patch": "@@ -1 +1 @@\n+x"}]

    def fake_gh_api(account, path, **kw):
        if kw.get("accept"):
            return "diff --git a/a.py b/a.py\n"
        return files if path.endswith("per_page=100") else pr

    monkeypatch.setattr(pr_context, "gh_api", fake_gh_api)
    ctx = pr_context.build_context("https://github.com/acme-corp/api/pull/7", "/some/cwd")

    expected = tmp_path / ".work" / "acme-corp-api-7.ctx.json"
    assert ctx["ctx_path"] == str(expected)
    saved = json.loads(expected.read_text())
    assert saved["pr"]["number"] == 7 and saved["pr"]["head_sha"] == "abc123"
    assert saved == ctx
    assert (tmp_path / ".work" / "acme-corp-api-7.diff").exists()


def test_build_context_records_author_and_own_pr(tmp_path, monkeypatch):
    import pr_context

    monkeypatch.setattr(pr_context, "REVIEWS_DIR", tmp_path)
    monkeypatch.setattr(pr_context, "_remote_urls", lambda cwd: ["https://github.com/acme-corp/api.git"])
    monkeypatch.setattr(pr_context, "account_for_owner", lambda owner: "alice-dev")

    def make_pr(login):
        return {"state": "open", "html_url": "u", "title": "t", "body": "", "user": {"login": login},
                "base": {"ref": "main"}, "head": {"ref": "feat", "sha": "abc123"}}

    for login, own in (("alice-dev", True), ("bob-dev", False)):
        pr = make_pr(login)

        def fake_gh_api(account, path, **kw):
            if kw.get("accept"):
                return ""
            return [] if path.endswith("per_page=100") else pr

        monkeypatch.setattr(pr_context, "gh_api", fake_gh_api)
        ctx = pr_context.build_context("https://github.com/acme-corp/api/pull/7", "/x")
        assert ctx["author"] == login
        assert ctx["own_pr"] is own
        assert ctx["commit_identity"] == ["Alice Example", "alice@example.com"]


def test_build_context_tolerates_no_user(tmp_path, monkeypatch):
    import pr_context

    monkeypatch.setattr(pr_context, "REVIEWS_DIR", tmp_path)
    monkeypatch.setattr(pr_context, "_remote_urls", lambda cwd: ["https://github.com/acme-corp/api.git"])
    monkeypatch.setattr(pr_context, "account_for_owner", lambda owner: "alice-dev")

    pr = {"state": "open", "html_url": "u", "title": "t", "body": "", "user": None,
          "base": {"ref": "main"}, "head": {"ref": "feat", "sha": "abc123"}}

    def fake_gh_api(account, path, **kw):
        if kw.get("accept"):
            return ""
        return [] if path.endswith("per_page=100") else pr

    monkeypatch.setattr(pr_context, "gh_api", fake_gh_api)
    ctx = pr_context.build_context("https://github.com/acme-corp/api/pull/7", "/x")
    assert ctx["author"] == "ghost"
    assert ctx["own_pr"] is False
