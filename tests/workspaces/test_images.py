"""Image resolution order, hash tags, idempotent builds, and the deterministic proposer."""

from __future__ import annotations

import asyncio
from pathlib import Path

from agentline.config import Config, RepoConfig
from agentline.workspaces.images import (
    DEFAULT_BASE,
    build_image,
    profile_definition,
    propose_image_change,
    resolve_image,
    set_proposer,
)

from .helpers import FakeDocker

REPO = RepoConfig("app", "x")


def tree(tmp_path: Path, files: dict[str, str]) -> Path:
    for name, text in files.items():
        (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / name).write_text(text)
    return tmp_path


def test_resolution_order(tmp_path: Path) -> None:
    wt = tree(
        tmp_path,
        {
            ".devcontainer/devcontainer.json": '{\n  // comment\n  "image": "mcr/dev:1",\n}',
            "Dockerfile.agentline": "FROM debian\n",
            "pyproject.toml": "",
        },
    )
    assert resolve_image("ghcr.io/x:1", REPO, None, wt).source == "explicit"
    repo_img = RepoConfig("app", "x", image="repo/img:2", image_profile="go")
    assert resolve_image("auto", repo_img, None, wt).ref == "repo/img:2"
    prof = resolve_image("auto", RepoConfig("app", "x", image_profile="go"), None, wt)
    assert prof.source == "repo" and prof.profile == "go" and prof.needs_build
    dev = resolve_image("auto", REPO, None, wt)
    assert (dev.source, dev.ref, dev.needs_build) == ("devcontainer", "mcr/dev:1", False)
    (wt / ".devcontainer/devcontainer.json").unlink()
    df = resolve_image("auto", REPO, None, wt)
    assert df.source == "dockerfile" and df.dockerfile == "FROM debian\n" and df.context == wt
    (wt / "Dockerfile.agentline").unlink()
    py = resolve_image("auto", REPO, None, wt)
    assert py.source == "profile" and py.profile == "python" and py.markers == ["pyproject.toml"]
    (wt / "package.json").write_text("{}")
    (wt / "requirements-dev.txt").write_text("")
    both = resolve_image("auto", REPO, None, wt)
    assert both.profile == "node+python"
    assert "nodejs" in (both.dockerfile or "") and "python3" in (both.dockerfile or "")
    for f in ("package.json", "requirements-dev.txt", "pyproject.toml"):
        (wt / f).unlink()
    base = resolve_image("auto", REPO, None, wt)
    assert (base.source, base.ref) == ("base", DEFAULT_BASE)


def test_tag_is_a_content_hash(tmp_path: Path) -> None:
    wt = tree(tmp_path, {"go.mod": ""})
    a = resolve_image("auto", REPO, None, wt)
    b = resolve_image("auto", REPO, None, wt)
    assert a.ref == b.ref and a.ref.startswith("agentline/img-app-") and len(a.ref) == 30
    cfg = Config.from_dict({"images": {"go": {"from": "golang:1.23", "apt": ["make"]}}})
    c = resolve_image("auto", REPO, cfg, wt)
    assert c.ref != a.ref
    assert c.dockerfile == (
        "FROM golang:1.23\nUSER root\nRUN apt-get update && apt-get install -y "
        "--no-install-recommends make && rm -rf /var/lib/apt/lists/*\n"
    )


def test_profile_definition_extends_and_base() -> None:
    cfg = Config.from_dict(
        {
            "images": {
                "base": {"from": "acme/base:3"},
                "python": {"apt": ["python3"], "run": ["pip install uv"]},
                "python+psql": {"extends": ["python"], "apt": ["postgresql-client"]},
            }
        }
    )
    d = profile_definition("python+psql", cfg)
    assert d == {
        "from": "acme/base:3",
        "apt": ["python3", "postgresql-client"],
        "run": ["pip install uv"],
    }


def test_build_is_idempotent(tmp_path: Path) -> None:
    choice = resolve_image("auto", REPO, None, tree(tmp_path, {"go.mod": ""}))
    docker = FakeDocker()
    first = asyncio.run(build_image(choice, docker="dk", executor=docker))
    second = asyncio.run(build_image(choice, docker="dk", executor=docker))
    assert first == {"tag": choice.ref, "built": True}
    assert second == {"tag": choice.ref, "built": False}
    assert len(docker.argvs("build")) == 1 and len(docker.argvs("image")) == 2


def test_proposer() -> None:
    p = propose_image_change(["psql", "protoc", "psql", "frobnicate"], "python", [])
    assert p["profile_from"] == "python"
    assert p["profile_to"] == "python+protoc+psql"
    assert p["add_apt"] == ["postgresql-client", "protobuf-compiler"]
    assert p["unknown"] == ["frobnicate"]
    assert "psql" in p["reason"] and "frobnicate" in p["reason"]
    assert 'extends = ["python"]' in p["toml"]
    q = propose_image_change(["jq"], None, ["package.json"])
    assert q["profile_from"] == "node" and q["add_apt"] == ["jq"]
    r = propose_image_change(["zzz"], None, [])
    assert r["profile_to"] == "base" and r["add_apt"] == [] and r["toml"] is None


def test_proposer_seam() -> None:
    from agentline.workspaces.images import propose

    prev = set_proposer(lambda tools, profile, markers: {"llm": tools})
    try:
        assert propose(["x"], None, []) == {"llm": ["x"]}
    finally:
        set_proposer(prev)
    assert propose(["jq"], None, [])["add_apt"] == ["jq"]
