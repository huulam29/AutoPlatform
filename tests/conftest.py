"""Every test: no automatic failure triage in `agentline serve` subprocesses (it would call
the real Chat agent, e.g. a `claude` CLI on the machine). tests/ai/test_triage.py drives
triage directly."""

import os

import pytest

os.environ.setdefault("AGENTLINE_TRIAGE", "off")
os.environ.setdefault("AGENTLINE_REVIEW", "off")  # run reviews call the Chat agent too
os.environ.setdefault("AGENTLINE_RUNCHAT", "off")  # and so do chat start nodes' answers


@pytest.fixture(autouse=True)
def _never_the_real_home(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every test gets a throwaway $AGENTLINE_HOME, so nothing (the secrets file, setup.json,
    incidents) can land in the real ~/.agentline. Fixtures that set their own still win."""
    monkeypatch.setenv("AGENTLINE_HOME", str(tmp_path_factory.mktemp("agentline-home")))


@pytest.fixture(autouse=True)
def _cli_harnesses_set_up(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    """Most tests were written when claude-code, codex and aider were built in (the examples
    and templates name `harness: claude-code`); they run as an install that has them set up,
    which is what the one-time migration gives an existing install (phase 44). Tests of the
    builtin-only behavior use `@pytest.mark.builtin_only`."""
    if request.node.get_closest_marker("builtin_only"):
        return
    from agentline import agents

    monkeypatch.setattr(
        agents, "BUILTIN_HARNESSES", {**agents.BUILTIN_HARNESSES, **agents.HARNESS_PRESETS}
    )
    monkeypatch.setattr(agents, "FALLBACK_HARNESS", "claude-code")  # the fallback back then
