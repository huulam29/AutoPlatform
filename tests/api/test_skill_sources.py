"""Installing skills from GitHub repos and the skills.sh marketplace (agentline.skill_sources),
against a fake codeload + search server."""

from __future__ import annotations

import io
import json
import tarfile
import threading
import urllib.parse
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from agentline import skill_sources
from agentline.api.skills import create_skills_router
from agentline.errors import ValidationError
from agentline.skill_sources import Source, parse_source
from agentline.skills import SkillLibrary
from demo.fakes.server import _Server

SHA = "0123456789abcdef0123456789abcdef01234567"


def skill_md(name: str, desc: str = "does things") -> str:
    return f"---\nname: {name}\ndescription: {desc}\n---\n\n# {name}\n\nSteps.\n"


def tarball(top: str, files: dict[str, str], sha: str = SHA) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz", format=tarfile.PAX_FORMAT,
                      pax_headers={"comment": sha}) as tar:  # fmt: skip
        for name, text in files.items():
            data = text.encode()
            info = tarfile.TarInfo(f"{top}/{name}")
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
        link = tarfile.TarInfo(f"{top}/skills/evil")
        link.type = tarfile.SYMTYPE
        link.linkname = "/etc"
        tar.addfile(link)
    return buf.getvalue()


REPOS: dict[str, dict[str, str]] = {
    "docker/skills": {
        "README.md": "hi",
        "skills/docker-compose-patterns/SKILL.md": skill_md("docker-compose-patterns", "compose"),
        "skills/docker-compose-patterns/references/a.md": "ref",
        "skills/docker-build/SKILL.md": skill_md("docker-build", "multi-line\n  build"),
        "skills/broken/SKILL.md": "no front matter",
        "examples/demo/SKILL.md": skill_md("not-a-real-one"),  # outside the known folders
    },
    "solo/one": {"SKILL.md": skill_md("Solo Skill", "a root skill")},
    "deep/tree": {"a/b/c/SKILL.md": skill_md("deep-one")},
    "agent/dirs": {".claude/skills/claude-one/SKILL.md": skill_md("claude-one")},
}


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> Iterator[dict[str, int]]:
    hits = {"tar": 0, "search": 0}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a: object) -> None:
            pass

        def do_GET(self) -> None:
            u = urllib.parse.urlparse(self.path)
            if u.path == "/api/search":
                hits["search"] += 1
                q = urllib.parse.parse_qs(u.query)["q"][0]
                body = json.dumps({"skills": [
                    {"id": "docker/skills/docker-build", "source": "docker/skills",
                     "skillId": "docker-build", "name": "docker-build", "installs": 5},
                    {"id": "docker/skills/docker-compose-patterns", "source": "docker/skills",
                     "skillId": "docker-compose-patterns", "name": "docker-compose-patterns",
                     "installs": 50},
                    {"bad": True},
                ] if q == "docker" else []}).encode()  # fmt: skip
                self._send(200, body, "application/json")
                return
            parts = u.path.strip("/").split("/")  # owner/repo/tar.gz/ref
            slug = "/".join(parts[:2])
            if len(parts) == 4 and parts[2] == "tar.gz" and slug in REPOS:
                hits["tar"] += 1
                self._send(200, tarball(f"{parts[1]}-{parts[3]}", REPOS[slug]), "application/gzip")
            else:
                self._send(404, b"nope", "text/plain")

        def _send(self, code: int, body: bytes, ctype: str) -> None:
            self.send_response(code)
            self.send_header("content-type", ctype)
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    srv = _Server(("127.0.0.1", 0), Handler)
    t = threading.Thread(target=srv.serve_forever, args=(0.02,), daemon=True)
    t.start()
    url = f"http://127.0.0.1:{srv.server_port}"
    monkeypatch.setenv("AGENTLINE_GITHUB_CODELOAD", url)
    monkeypatch.setenv("AGENTLINE_SKILLS_API", url)
    skill_sources._cache.clear()
    yield hits
    srv.shutdown()


@pytest.mark.parametrize(
    "text, want",
    [
        ("docker/skills", Source("docker", "skills")),
        ("npx skills add docker/skills", Source("docker", "skills")),
        ("npx -y skills add docker/skills --skill docker-build -y",
         Source("docker", "skills", skill="docker-build")),
        ("docker/skills@docker-build", Source("docker", "skills", skill="docker-build")),
        ("docker/skills/skills/x", Source("docker", "skills", path="skills/x")),
        ("docker/skills#v2", Source("docker", "skills", ref="v2")),
        ("github:docker/skills", Source("docker", "skills")),
        ("https://github.com/docker/skills", Source("docker", "skills")),
        ("https://github.com/docker/skills.git", Source("docker", "skills")),
        ("https://github.com/docker/skills/tree/main/skills/x",
         Source("docker", "skills", ref="main", path="skills/x")),
        ("https://github.com/docker/skills/blob/main/skills/x/SKILL.md",
         Source("docker", "skills", ref="main", path="skills/x")),
    ],
)  # fmt: skip
def test_parse_source(text: str, want: Source) -> None:
    assert parse_source(text) == want
    assert parse_source(want.label) == want


@pytest.mark.parametrize("text", ["docker", "", "a/b/../c", "https://gitlab.com/a/b", "a b/c"])
def test_parse_source_rejects(text: str) -> None:
    with pytest.raises(ValidationError):
        parse_source(text)


def test_preview_finds_skills_like_the_cli(fake: dict[str, int], tmp_path: Path) -> None:
    lib = SkillLibrary(tmp_path)
    p = skill_sources.preview("docker/skills", lib)
    assert p["sha"] == SHA and p["repo"] == "docker/skills"
    names = [s["name"] for s in p["skills"]]
    assert names == ["docker-build", "docker-compose-patterns"]
    assert p["skills"][0]["description"] == "multi-line build"
    assert p["skipped"] == [
        {"path": "skills/broken", "reason": "SKILL.md has no name or description"}
    ]
    one = skill_sources.preview("solo/one", lib)["skills"]
    assert [(s["name"], s["path"]) for s in one] == [("solo-skill", "")]
    deep = skill_sources.preview("deep/tree", lib)["skills"]  # nothing in the known places
    assert [s["path"] for s in deep] == ["a/b/c"]
    assert [s["name"] for s in skill_sources.preview("agent/dirs", lib)["skills"]] == ["claude-one"]
    with pytest.raises(ValidationError, match="no skill"):
        skill_sources.preview("docker/skills@nope", lib)


def test_install_update_remove(
    fake: dict[str, int], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lib = SkillLibrary(tmp_path)
    skill_sources.preview("docker/skills", lib)
    r = skill_sources.install("docker/skills", ["docker-compose-patterns"], lib)
    assert r["installed"] == ["docker-compose-patterns"]
    assert fake["tar"] == 1  # the preview's download was reused
    skill = lib.get("docker-compose-patterns")
    assert skill is not None and skill.source == "installed"
    assert (skill.path / "references" / "a.md").read_text() == "ref"
    assert not (skill.path.parent / "evil").exists()
    lock = skill_sources.installed(lib)["docker-compose-patterns"]
    assert lock["repo"] == "docker/skills" and lock["path"] == "skills/docker-compose-patterns"
    assert lock["sha"] == SHA
    assert lib.resolve(["docker-compose-patterns"])  # usable by nodes' `skills:`

    with pytest.raises(ValidationError, match="no skill"):
        skill_sources.install("docker/skills", ["ghost"], lib)

    # your own skill of the same name wins
    lib.save_local("docker-compose-patterns", "mine", "my body")
    assert lib.get("docker-compose-patterns").source == "local"  # type: ignore[union-attr]
    lib.delete_local("docker-compose-patterns")

    new = skill_md("docker-compose-patterns", "compose v2")
    monkeypatch.setitem(REPOS["docker/skills"], "skills/docker-compose-patterns/SKILL.md", new)
    u = skill_sources.update(lib)
    assert u == {"updated": ["docker-compose-patterns"], "missing": []}
    assert lib.get("docker-compose-patterns").description == "compose v2"  # type: ignore[union-attr]

    assert skill_sources.remove(lib, "docker-compose-patterns")
    assert lib.get("docker-compose-patterns") is None
    assert skill_sources.installed(lib) == {}
    assert not skill_sources.remove(lib, "docker-compose-patterns")


def test_search(fake: dict[str, int]) -> None:
    out = skill_sources.search("docker")
    assert [s["name"] for s in out] == ["docker-build", "docker-compose-patterns"]
    assert out[1]["source"] == "docker/skills" and out[1]["skill"] == "docker-compose-patterns"
    assert skill_sources.search("x") == []  # too short: no call
    assert fake["search"] == 1


def test_api(fake: dict[str, int], tmp_path: Path) -> None:
    lib = SkillLibrary(tmp_path)
    app = FastAPI()
    app.include_router(create_skills_router(lib), prefix="/api")
    c = TestClient(app)

    s = c.get("/api/skills/search", params={"q": "docker"}).json()
    assert [x["installed"] for x in s["skills"]] == [False, False]
    p = c.post("/api/skills/preview", json={"source": "npx skills add docker/skills"}).json()
    assert len(p["skills"]) == 2
    r = c.post("/api/skills/install", json={"source": "docker/skills", "names": ["docker-build"]})
    assert r.status_code == 200, r.text
    row = next(x for x in r.json()["skills"] if x["name"] == "docker-build")
    assert row["source"] == "installed" and row["installed_from"]["repo"] == "docker/skills"
    s = c.get("/api/skills/search", params={"q": "docker"}).json()
    assert [x["installed"] for x in s["skills"]] == [True, False]

    assert c.post("/api/skills/preview", json={"source": "nobody/here"}).status_code == 502
    assert c.post("/api/skills/preview", json={"source": "nope"}).status_code == 422
    assert c.post("/api/skills/update", json={}).json()["result"]["updated"] == ["docker-build"]
    assert c.delete("/api/skills/docker-build").status_code == 200
    assert c.delete("/api/skills/docker-build").status_code == 404


def test_cli(fake: dict[str, int], capsys: pytest.CaptureFixture[str]) -> None:
    from agentline.cli import main

    assert main(["skills", "search", "docker"]) == 0
    assert "docker/skills@docker-compose-patterns" in capsys.readouterr().out
    assert main(["skills", "add", "npx skills add docker/skills", "--skill", "docker-build"]) == 0
    assert "installed from docker/skills: docker-build" in capsys.readouterr().out
    assert main(["skills", "update"]) == 0
    assert "updated: docker-build" in capsys.readouterr().out
    assert main(["skills", "remove", "docker-build"]) == 0
    assert main(["skills", "remove", "docker-build"]) == 1
