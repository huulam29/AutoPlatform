"""Chat instructions (defaults on first use, API, Chat action) and the Chat's skills: Claude
Code gets every skill installed in its folder, other agents the named ones in the prompt."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from agentline import App, SqliteLedger, chat_instructions
from agentline.api.server import create_server
from agentline.assistant import Live, check_actions
from agentline.builtins import default_registry
from agentline.config import Config
from agentline.registry import RunRequest, RunResult
from agentline.runners.function import FunctionRunner
from agentline.skills import SkillLibrary


def test_defaults_written_on_first_use(isolated_home: Path) -> None:
    assert not chat_instructions.path().exists()
    text = chat_instructions.read()
    assert "invoke the `interview-me` skill" in text and "new pipeline" in text
    assert chat_instructions.path().read_text() == text
    chat_instructions.write("Answer in French.")
    assert chat_instructions.read() == "Answer in French.\n"  # edits stick
    assert chat_instructions.skill_names("use `interview-me`, then `code-review-and-quality`") == [
        "interview-me",
        "code-review-and-quality",
    ]
    with pytest.raises(Exception, match="longer than"):
        chat_instructions.write("x" * 30_000)
    assert chat_instructions.reset() == chat_instructions.DEFAULT


def test_interview_me_ships_built_in_and_the_library_wins(isolated_home: Path) -> None:
    lib = SkillLibrary()
    s = lib.get("interview-me")
    assert s is not None and s.source == "builtin"
    for part in ("Goal", "Trigger", "Steps", "AI or script", "Approvals", "Outputs"):
        assert part in s.body
    fetched = lib.dir / "skills" / "interview-me"
    fetched.mkdir(parents=True)
    (fetched / "SKILL.md").write_text("---\nname: interview-me\ndescription: lib\n---\nlib\n")
    assert lib.get("interview-me").source == "fetched"
    lib.save_local("interview-me", "mine", "my interview")
    assert lib.get("interview-me").source == "local"


def test_chat_action_is_checked(tmp_path: Path) -> None:
    ok, empty, big = check_actions(
        [
            {"type": "chat_instructions", "text": "- Keep answers short."},
            {"type": "chat_instructions", "text": " "},
            {"type": "chat_instructions", "text": "x" * 30_000},
        ],
        tmp_path,
        None,
        Config(),
        Live(),
    )
    assert ok["issues"] == []
    assert "empty" in empty["issues"][0] and "longer" in big["issues"][0]


def _server(tmp_path: Path, kind_runner: str) -> tuple[TestClient, list[RunRequest]]:
    seen: list[RunRequest] = []

    async def fake(req: RunRequest) -> RunResult:
        seen.append(req)
        return RunResult(output={"message": "ok", "actions": []}, cost_usd=0.0)

    cfg = Config.from_dict(
        {
            "providers": {"p": {"type": "openai-compatible", "base_url": "http://x/v1"}},
            "models": {"m": {"provider": "p", "model": "a/b"}},
        }
    )
    reg = default_registry(cfg)
    reg.add_runner(FunctionRunner(kind_runner, fake))
    (tmp_path / "pipelines").mkdir()
    app = App(SqliteLedger(tmp_path / "l.db"))
    server = create_server(app, [], pipelines_dir=tmp_path / "pipelines", registry=reg, config=cfg)
    return TestClient(server), seen


def test_api_and_claude_code_gets_skills_installed(
    tmp_path: Path, isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "home"))  # Path.home() on Windows
    c, seen = _server(tmp_path, "claude-code")
    r = c.get("/api/chat-instructions").json()
    assert r["is_default"] and "interview-me" in r["text"]
    r = c.put("/api/chat-instructions", json={"text": "- Always use `interview-me`.\n- Be brief."})
    assert r.status_code == 200 and not r.json()["is_default"]
    assert c.put("/api/chat-instructions", json={"text": "x" * 30_000}).status_code == 422
    SkillLibrary().save_local("our-style", "house style", "Use tabs.")
    msg = {"messages": [{"role": "user", "content": "build me a pipeline"}]}
    assert c.post("/api/assistant/chat", json=msg).status_code == 200
    req = seen[0]
    assert "## CHAT INSTRUCTIONS" in req.prompt and "- Be brief." in req.prompt
    assert "Skill tool" in req.prompt
    assert req.limits.allowed_tools == ("read", "Skill")
    skills = Path(req.workspace.path) / ".claude" / "skills"
    assert (skills / "interview-me" / "SKILL.md").is_file()
    assert (skills / "our-style" / "SKILL.md").is_file()
    SkillLibrary().delete_local("our-style")
    c.post("/api/assistant/chat", json=msg)
    assert not (skills / "our-style").exists()  # a deleted skill is gone next turn
    assert c.delete("/api/chat-instructions").json()["is_default"]


def test_other_agents_get_the_named_skills_in_the_prompt(
    tmp_path: Path, isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "home"))  # Path.home() on Windows
    c, seen = _server(tmp_path, "aider")  # a CLI harness that can't load skill folders
    body: dict[str, Any] = {
        "harness": "aider",
        "model": "m",
        "messages": [{"role": "user", "content": "build me a pipeline"}],
    }
    r = c.post("/api/assistant/chat", json=body)
    assert r.status_code == 200, r.text
    req = seen[0]
    assert req.workspace is None and req.limits.allowed_tools == ("read",)
    assert "## Skill: interview-me" in req.prompt and "AI or script" in req.prompt


def test_the_builtin_harness_reads_skills_with_a_tool(isolated_home: Path) -> None:
    from agentline.assistant import skills_for_chat
    from agentline.skills import SkillLibrary

    note, workspace, tools = skills_for_chat("builtin", "use `interview-me`", SkillLibrary())
    assert "read_skill" in note and "## Skill:" not in note
    assert workspace is None and tools == ("read",)
