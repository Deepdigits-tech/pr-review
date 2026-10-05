import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import prlib  # noqa: E402

TEST_CONFIG = {
    "owner_accounts": {"example-org": "alice-dev", "acme-corp": "alice-dev", "alice-work": "alice-work"},
    "own_accounts": ["alice-dev", "alice-work"],
    "commit_identities": {
        "alice-dev": ["Alice Example", "alice@example.com"],
        "alice-work": ["Alice Example", "alice@work.example.com"],
    },
    "fix_allowed_authors": ["bob-dev"],
}


@pytest.fixture(autouse=True)
def pr_review_config(tmp_path_factory, monkeypatch):
    """Every test reads a neutral config from tmp, never the real one in ~/.config/pr-review."""
    path = tmp_path_factory.mktemp("pr-review-config") / "config.json"
    path.write_text(json.dumps(TEST_CONFIG))
    monkeypatch.setenv("PR_REVIEW_CONFIG", str(path))
    prlib._CONFIG_CACHE.clear()
    yield path
    prlib._CONFIG_CACHE.clear()
