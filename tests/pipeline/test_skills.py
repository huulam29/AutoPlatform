"""Agent skills: the fetched library, validation, and how a node's skills reach the agent."""

from __future__ import annotations

import asyncio
import contextlib
import io
import subprocess
import tarfile
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from agentline import App, RunStatus, SqliteLedger
from agentline.api.server import create_server
from agentline.errors import ValidationError
from agentline.pipeline import build_pipeline, parse_spec
from agentline.pipeline.validate import validate_spec
from agentline.registry import Registry, RunRequest, RunResult
from agentline.runners.function import FunctionRunner
from agentline.skills import SkillLibrary, install_into, prompt_section


def tarball(files: dict[str, str]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, text in files.items():
            data = text.encode()
            info = tarfile.TarInfo(f"agent-skills-main/{name}")
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
        link = tarfile.TarInfo("agent-skills-main/skills/evil")
        link.type = tarfile.SYMTYPE
        link.linkname = "/etc"
        tar.addfile(link)
    return buf.getvalue()


SKILLS = {
    "LICENSE": "MIT",
    "README.md": "not copied",
    "skills/planning-and-task-breakdown/SKILL.md": (
        "---\nname: planning-and-task-breakdown\ndescription: Breaks work into tasks.\n---\n"
        "# Planning\nWrite small tasks.\n"
    ),
    "skills/planning-and-task-breakdown/references/x.md": "ref",
    "skills/test-driven-development/SKILL.md": (
        '---\nname: test-driven-development\ndescription: "Red, green, refactor."\n---\nTDD.\n'
    ),
}


@pytest.fixture
def lib(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SkillLibrary:
    monkeypatch.setenv("AGENTLINE_HOME", str(tmp_path / "home"))
    library = SkillLibrary()
    library.install_tarball(tarball(SKILLS))
    return library


def test_install_keeps_only_skills_and_license(lib: SkillLibrary) -> None:
    assert [s.name for s in lib.list()] == [
        "interview-me",  # built in (agentline/builtin_skills), lowest priority
        "planning-and-task-breakdown",
        "test-driven-development",
    ]
    assert lib.get("test-driven-development").description == "Red, green, refactor."
    assert (lib.dir / "LICENSE").exists() and not (lib.dir / "README.md").exists()
    assert not (lib.dir / "skills" / "evil").exists()
    assert lib.info()["fetched"] and lib.info()["ref"] == "main"
    with pytest.raises(ValidationError, match="unknown skill"):
        lib.resolve(["nope"])
    # a refetch replaces the copy
    lib.install_tarball(tarball({"skills/idea-refine/SKILL.md": "---\nname: idea-refine\n---\n"}))
    assert [s.name for s in lib.list()] == ["idea-refine", "interview-me"]


def test_install_into_workspace_is_git_excluded(lib: SkillLibrary, tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    install_into(repo, lib.resolve(["planning-and-task-breakdown"]))
    assert (repo / ".claude/skills/planning-and-task-breakdown/references/x.md").exists()
    assert "/.claude/skills/" in (repo / ".git/info/exclude").read_text()
    out = subprocess.run(
        ["git", "-C", str(repo), "status", "--porcelain"], capture_output=True, text=True
    ).stdout
    assert out == ""
    text = prompt_section(lib.resolve(["test-driven-development"]))
    assert "## Skill: test-driven-development" in text and "TDD." in text


SPEC = """\
name: p
nodes:
  plan: {{ type: agent, runner: {runner}, prompt: "Plan it", skills: {skills}{ws} }}
"""


def run_one(tmp_path: Path, src: str, ws_path: Path | None = None) -> tuple[str, list[str]]:
    seen: list[str] = []

    async def agent(req: RunRequest) -> RunResult:
        seen.append(req.prompt)
        return RunResult(output={"ok": True})

    reg = Registry()
    reg.add_runner(FunctionRunner("claude-code", agent))
    reg.add_runner(FunctionRunner("codex", agent))

    class Workspaces:
        name = "local"

        def acquire(self, ctx: Any, spec: Any) -> Any:
            @contextlib.asynccontextmanager
            async def cm() -> Any:
                class WS:
                    path = ws_path

                    async def exec(self, cmd: list[str], **kw: Any) -> tuple[int, str, str]:
                        return 0, "", ""

                yield WS()

            return cm()

    reg.add_workspace(Workspaces())
    app = App(SqliteLedger(tmp_path / f"l{len(list(tmp_path.iterdir()))}.db"), backoff_base=0.0)
    app.register_pipeline(build_pipeline(parse_spec(src, base_dir=tmp_path), reg))
    sub = app.submit_sync("p", {})
    asyncio.run(app.worker(concurrency=1).run_until_idle())
    run = app.get_run(sub.run_id)
    assert run is not None
    return run.status, seen


def test_skills_reach_the_agent(lib: SkillLibrary, tmp_path: Path) -> None:
    # codex (or no workspace): in the prompt
    status, prompts = run_one(
        tmp_path, SPEC.format(runner="codex", skills="[test-driven-development]", ws="")
    )
    assert status == RunStatus.DONE and "## Skill: test-driven-development" in prompts[0]
    # claude-code with a workspace: installed as files, prompt unchanged
    ws = tmp_path / "ws"
    ws.mkdir()
    src = SPEC.format(
        runner="claude-code",
        skills="[planning-and-task-breakdown]",
        ws=", workspace: { type: local }",
    )
    status, prompts = run_one(tmp_path, src, ws)
    assert status == RunStatus.DONE and prompts == ["Plan it"]
    assert (ws / ".claude/skills/planning-and-task-breakdown/SKILL.md").exists()


def test_unknown_skill_is_a_validation_issue(lib: SkillLibrary) -> None:
    spec = parse_spec(SPEC.format(runner="codex", skills="[nope, test-driven-development]", ws=""))
    msgs = [i.message for i in validate_spec(spec)]
    assert any("unknown skill(s) nope" in m for m in msgs)
    spec = parse_spec(SPEC.format(runner="codex", skills="[Bad Name]", ws=""))
    assert any("bad skill name" in i.message for i in validate_spec(spec))


def test_missing_library_fails_the_run_with_a_hint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AGENTLINE_HOME", str(tmp_path / "empty"))
    status, prompts = run_one(
        tmp_path, SPEC.format(runner="codex", skills="[test-driven-development]", ws="")
    )
    assert status == RunStatus.DEAD and prompts == []


def test_skills_api_and_editor_suggestions(lib: SkillLibrary, tmp_path: Path) -> None:
    (tmp_path / "pipelines").mkdir()
    app = App(SqliteLedger(tmp_path / "api.db"))
    c = TestClient(create_server(app, [], pipelines_dir=tmp_path / "pipelines"))
    body = c.get("/api/skills").json()
    assert body["fetched"] and [s["name"] for s in body["skills"]] == [
        "interview-me",
        "planning-and-task-breakdown",
        "test-driven-development",
    ]
    assert "planning-and-task-breakdown" in body["suggested"]["plan"]
    types = c.get("/api/node-types").json()
    field = next(f for f in types["types"]["agent"]["fields"] if f["name"] == "skills")
    assert field["suggest"] == types["skills"] == [s["name"] for s in body["skills"]]


def test_local_skills_override_and_work_without_the_library(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AGENTLINE_HOME", str(tmp_path / "home2"))
    lib = SkillLibrary()
    assert not lib.fetched
    lib.save_local("our-review", "Our review checklist", "# Review\nCheck the changelog.")
    assert [(s.name, s.source) for s in lib.list()] == [
        ("interview-me", "builtin"),
        ("our-review", "local"),
    ]
    assert lib.resolve(["our-review"])[0].description == "Our review checklist"
    with pytest.raises(ValidationError, match="hasn't been fetched"):
        lib.resolve(["test-driven-development"])
    lib.install_tarball(tarball(SKILLS))
    lib.save_local("test-driven-development", "Our TDD", "Ours.")  # wins over the fetched one
    tdd = lib.get("test-driven-development")
    assert tdd.source == "local" and "Ours." in tdd.body
    assert lib.delete_local("test-driven-development")
    assert lib.get("test-driven-development").source == "fetched"
    assert not lib.delete_local("planning-and-task-breakdown")  # fetched ones can't be deleted
    with pytest.raises(ValidationError, match="bad skill name"):
        lib.save_local("Bad Name", "", "x")
    # a pipeline using only local skills validates and runs without the library
    status, prompts = run_one(tmp_path, SPEC.format(runner="codex", skills="[our-review]", ws=""))
    assert status == RunStatus.DONE and "Check the changelog." in prompts[0]


def test_local_skills_api(lib: SkillLibrary, tmp_path: Path) -> None:
    (tmp_path / "p").mkdir()
    app = App(SqliteLedger(tmp_path / "s.db"))
    c = TestClient(create_server(app, [], pipelines_dir=tmp_path / "p"))
    r = c.put("/api/skills/our-review", json={"description": "Checklist", "body": "Do X."})
    assert r.status_code == 200
    assert {"name": "our-review", "description": "Checklist", "source": "local"} in r.json()[
        "skills"
    ]
    assert c.get("/api/skills/our-review").json()["body"].endswith("Do X.\n")
    assert c.put("/api/skills/BAD", json={"body": "x"}).status_code == 422
    assert c.delete("/api/skills/test-driven-development").status_code == 404
    assert c.delete("/api/skills/our-review").status_code == 200
    assert c.get("/api/skills/our-review").status_code == 404
