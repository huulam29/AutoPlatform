from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Never touch the real ~/.agentline (secrets file) from tests."""
    home = tmp_path / "agentline-home"
    monkeypatch.setenv("AGENTLINE_HOME", str(home))
    # Settings writes fall back to $AGENTLINE_CONFIG or ./agentline.toml: never the repo's.
    monkeypatch.setenv("AGENTLINE_CONFIG", str(tmp_path / "agentline.toml"))
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("PRIVATE_LLM_KEY", raising=False)
    # never the machine's GitHub CLI login (tests that need one use a fake gh)
    from agentline import github_setup

    monkeypatch.setattr(github_setup, "gh_path", lambda: None)
    github_setup._cache.clear()
    yield home


OPENROUTER_TOML = """\
# my agentline config
[providers.openrouter]  # free models for small tasks
type = "openai-compatible"
base_url = "{base}"
api_key_env = "OPENROUTER_API_KEY"

[providers.claude]
type = "anthropic"
auth = "subscription"

[models.cheap]
provider = "openrouter"
model = "meta-llama/llama-3.3-70b-instruct:free"

[models.opus]
provider = "claude"
model = "claude-opus-5-5"
"""
