"""Pipeline import / export bundles."""

from __future__ import annotations

import io
import json
import tomllib
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agentline import App, SqliteLedger
from agentline.api.server import create_server
from agentline.config import Config
from tests.fakebin import WINDOWS

TOML = """\
[providers.mine]
type = "openai-compatible"
base_url = "http://127.0.0.1:9/v1"
api_key_env = "MINE_KEY"

[models.small]
provider = "mine"
model = "tiny-1"

[mcp.gh]
command = "npx"
env = { T = "${GH_TOKEN}" }
"""

PIPE = """\
version: 1
name: review
nodes:
  lint:
    type: action
    uses: shell.run
    out: lint
    with: { cmd: '"{{pipeline.dir}}/scripts/lint.sh"', output: json }
  summarize:
    type: agent
    model: small
    prompt: prompts/sum.md
    in: [lint]
    out: summary
  look:
    type: agent
    agent: claude-code
    prompt: "look"
    mcp: [gh]
    skills: [code-review-and-quality]
edges:
  - lint -> summarize
"""


def server(tmp: Path, toml: str) -> tuple[TestClient, Path, Path]:
    cfg_path = tmp / "agentline.toml"
    cfg_path.write_text(toml)
    pipelines = tmp / "pipelines"
    pipelines.mkdir()
    app = App(SqliteLedger(tmp / "l.db"))
    c = TestClient(create_server(app, [], pipelines_dir=pipelines, config=Config.load(cfg_path)))
    return c, pipelines, cfg_path


@pytest.fixture
def source(tmp_path: Path) -> tuple[TestClient, Path]:
    (tmp_path / "a").mkdir()
    c, pipelines, _ = server(tmp_path / "a", TOML)
    d = pipelines / "review"
    (d / "prompts").mkdir(parents=True)
    (d / "scripts").mkdir()
    (d / "pipeline.yaml").write_text(PIPE)
    (d / "prompts/sum.md").write_text("Summarize {{lint}}")
    (d / "scripts/lint.sh").write_text("#!/bin/sh\necho '{}'\n")
    (d / ".hidden").write_text("skip me")
    return c, pipelines


def test_export_has_files_and_manifest_without_secrets(source: tuple[TestClient, Path]) -> None:
    c, _ = source
    r = c.get("/api/export/review/pipeline")
    assert r.status_code == 200 and r.headers["content-type"] == "application/zip"
    assert 'filename="review.agentline.zip"' in r.headers["content-disposition"]
    z = zipfile.ZipFile(io.BytesIO(r.content))
    assert sorted(z.namelist()) == [
        "agentline-export.json",
        "review/pipeline.yaml",
        "review/prompts/sum.md",
        "review/scripts/lint.sh",
    ]
    m = json.loads(z.read("agentline-export.json"))
    req = m["requires"]
    assert m["file"] == "review/pipeline.yaml" and m["pipeline"] == "review"
    assert req["models"] == ["small"] and req["providers"] == ["mine"] and req["mcp"] == ["gh"]
    assert req["keys"] == ["GH_TOKEN", "MINE_KEY"] and req["skills"] == ["code-review-and-quality"]
    assert set(req["settings"]) == {"models", "providers", "mcp"}
    assert c.get("/api/export/nope").status_code == 404


def test_import_reports_missing_then_adds_settings(
    source: tuple[TestClient, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    c, _ = source
    data = c.get("/api/export/review/pipeline").content
    (tmp_path / "b").mkdir()
    c2, pipelines2, cfg2 = server(tmp_path / "b", "")
    r = c2.post("/api/import", content=data)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["name"] == "review/pipeline" and body["applied"] == []
    assert body["missing"]["models"] == ["small"] and body["missing"]["mcp"] == ["gh"]
    assert body["issues"]  # unknown model / MCP server until the settings exist
    if not WINDOWS:  # no executable bit there
        assert (pipelines2 / "review/scripts/lint.sh").stat().st_mode & 0o111
    assert c2.post("/api/import", content=data).status_code == 409
    r = c2.post("/api/import?folder=review2&apply_settings=true", content=data).json()
    assert r["name"] == "review2/pipeline"
    assert set(r["applied"]) == {"providers.mine", "models.small", "mcp.gh"}
    assert r["missing"]["models"] == [] and r["issues"] == []
    assert tomllib.loads(cfg2.read_text())["mcp"]["gh"]["env"] == {"T": "${GH_TOKEN}"}
    assert c2.post("/api/import?overwrite=true", content=data).status_code == 200


def test_top_level_pipeline_exports_referenced_files(tmp_path: Path) -> None:
    c, pipelines, _ = server(tmp_path, "")
    (pipelines / "prompts").mkdir()
    (pipelines / "prompts/p.md").write_text("hi")
    (pipelines / "prompts/other.md").write_text("not used")
    (pipelines / "solo.yaml").write_text(
        "version: 1\nname: solo\nnodes:\n  a: { type: agent, prompt: prompts/p.md }\n"
    )
    z = zipfile.ZipFile(io.BytesIO(c.get("/api/export/solo").content))
    assert sorted(z.namelist()) == ["agentline-export.json", "solo/prompts/p.md", "solo/solo.yaml"]


def bad_zip(names: dict[str, bytes], manifest: dict | None = None) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        if manifest is not None:
            z.writestr("agentline-export.json", json.dumps(manifest))
        for n, b in names.items():
            z.writestr(n, b)
    return buf.getvalue()


@pytest.mark.parametrize(
    ("data", "error"),
    [
        (b"not a zip", "not a .zip"),
        (bad_zip({"x/p.yaml": b""}), "not an agentline export"),
        (bad_zip({"x/p.yaml": b""}, {"format": 9, "file": "x/p.yaml"}), "unsupported"),
        (bad_zip({"../evil.sh": b""}, {"format": 1, "file": "x/p.yaml"}), "unsafe path"),
        (bad_zip({"y/p.yaml": b""}, {"format": 1, "file": "x/p.yaml"}), "has no"),
    ],
)
def test_import_refuses_bad_bundles(tmp_path: Path, data: bytes, error: str) -> None:
    c, pipelines, _ = server(tmp_path, "")
    r = c.post("/api/import", content=data)
    assert r.status_code == 422 and error in r.text
    assert [p for p in pipelines.iterdir() if not p.name.startswith(".")] == []
