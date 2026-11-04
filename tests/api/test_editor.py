"""Editor API: list/read/save pipelines, model edits, validation, path safety, auth."""

from __future__ import annotations

import shutil
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from agentline import App, SqliteLedger
from agentline.api import create_server

EXAMPLES = Path(__file__).resolve().parents[2] / "examples"
NAME = "ticket-to-eod/pipeline"


@pytest.fixture
def pdir(tmp_path: Path) -> Path:
    d = tmp_path / "pipelines"
    shutil.copytree(EXAMPLES / "ticket-to-eod", d / "ticket-to-eod")
    (tmp_path / "secret.yaml").write_text("name: outside\n")
    return d


@pytest.fixture
def client(tmp_path: Path, pdir: Path) -> Iterator[TestClient]:
    ledger = SqliteLedger(tmp_path / "ledger.db")
    server = create_server(
        App(ledger), [], pipelines_dir=pdir, static_dir=tmp_path / "nope", token=""
    )
    with TestClient(server) as c:
        yield c
    ledger.close()


def node(model: dict[str, Any], nid: str) -> dict[str, Any]:
    return next(n for n in model["nodes"] if n["id"] == nid)


def test_list_and_get(client: TestClient) -> None:
    r = client.get("/api/pipelines")
    assert r.status_code == 200
    (p,) = r.json()["pipelines"]
    assert p == {
        "name": NAME,
        "path": "ticket-to-eod/pipeline.yaml",
        "pipeline": "ticket-to-eod",
        "nodes": 9,
        "issues": 0,
    }
    r = client.get(f"/api/pipelines/{NAME}")
    body = r.json()
    assert body["source"] == (EXAMPLES / "ticket-to-eod" / "pipeline.yaml").read_text()
    assert body["model"]["name"] == "ticket-to-eod" and body["model"]["issues"] == []
    assert client.get("/api/pipelines/nope").status_code == 404


@pytest.mark.parametrize(
    "name", ["../secret", "ticket-to-eod/../../secret", "%2e%2e/secret", "/etc/passwd"]
)
def test_path_traversal_is_rejected(client: TestClient, name: str) -> None:
    r = client.get(f"/api/pipelines/{name}")
    assert r.status_code in (400, 404)
    r = client.put(f"/api/pipelines/{name}/source", json={"source": "x: 1\n"})
    assert r.status_code in (400, 404, 405)
    assert (Path(client.app.state.pipeline_files.root).parent / "secret.yaml").read_text() == (
        "name: outside\n"
    )


def test_put_model_makes_a_minimal_edit(client: TestClient, pdir: Path) -> None:
    got = client.get(f"/api/pipelines/{NAME}").json()
    model = got["model"]
    node(model, "plan")["fields"]["max_turns"] = 25
    r = client.put(
        f"/api/pipelines/{NAME}/model", json={"model": model, "base_source": got["source"]}
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["warnings"] == [] and body["issues"] == []
    on_disk = (pdir / "ticket-to-eod" / "pipeline.yaml").read_text()
    assert on_disk == body["source"]
    assert on_disk == got["source"].replace("max_turns: 20", "max_turns: 25")
    assert node(body["model"], "plan")["fields"]["max_turns"] == 25


def test_put_model_edits_defaults(client: TestClient, pdir: Path) -> None:
    got = client.get(f"/api/pipelines/{NAME}").json()
    assert got["model"]["defaults"] is None
    model = got["model"]
    model["defaults"] = {"harness": "nope", "max_turns": 5}
    r = client.put(
        f"/api/pipelines/{NAME}/model", json={"model": model, "base_source": got["source"]}
    )
    body = r.json()
    assert r.status_code == 200 and body["warnings"] == []
    assert body["model"]["defaults"] == {"harness": "nope", "max_turns": 5}
    assert "budget_usd: 5.0\ndefaults:\n  harness: nope\n  max_turns: 5\n\nnodes:" in body["source"]
    assert any(i["path"] == "defaults" and "nope" in i["message"] for i in body["issues"])
    model = body["model"]
    model["defaults"] = None
    r = client.put(
        f"/api/pipelines/{NAME}/model", json={"model": model, "base_source": body["source"]}
    )
    assert r.json()["source"] == got["source"]
    assert (pdir / "ticket-to-eod" / "pipeline.yaml").read_text() == got["source"]


def test_put_model_conflict(client: TestClient, pdir: Path) -> None:
    got = client.get(f"/api/pipelines/{NAME}").json()
    path = pdir / "ticket-to-eod" / "pipeline.yaml"
    path.write_text(got["source"] + "# edited elsewhere\n", newline="\n")  # no CRLF on Windows
    r = client.put(
        f"/api/pipelines/{NAME}/model", json={"model": got["model"], "base_source": got["source"]}
    )
    assert r.status_code == 409
    assert r.json()["source"].endswith("# edited elsewhere\n")
    assert path.read_text().endswith("# edited elsewhere\n")


def test_put_model_reports_issues_and_warnings(client: TestClient) -> None:
    got = client.get(f"/api/pipelines/{NAME}").json()
    model = got["model"]
    source = got["source"].replace(
        "  - approve -> deploy -> test", "  - approve -> deploy -> test  # tail"
    )
    client.put(f"/api/pipelines/{NAME}/source", json={"source": source})
    model["edges"] = [e for e in model["edges"] if e["src"] not in ("approve", "deploy")]
    r = client.put(f"/api/pipelines/{NAME}/model", json={"model": model, "base_source": source})
    body = r.json()
    assert r.status_code == 200
    assert any("# tail" in w for w in body["warnings"])
    assert any(i["node_id"] == "deploy" for i in body["issues"])  # now unreachable


def test_put_source_saves_even_when_invalid(client: TestClient, pdir: Path) -> None:
    r = client.put(f"/api/pipelines/{NAME}/source", json={"source": "nodes: [oops\n"})
    assert r.status_code == 200
    assert r.json()["issues"][0]["path"] == "$"
    assert (pdir / "ticket-to-eod" / "pipeline.yaml").read_text() == "nodes: [oops\n"
    r = client.put("/api/pipelines/new/source", json={"source": "name: new\n"})
    assert r.status_code == 200 and (pdir / "new.yaml").read_text() == "name: new\n"


def test_validate(client: TestClient) -> None:
    src = client.get(f"/api/pipelines/{NAME}").json()["source"]
    r = client.post("/api/validate", json={"source": src, "name": NAME})
    assert r.json()["issues"] == []
    bad = src.replace("uses: env.test", "uses: env.tset")
    (issue,) = client.post("/api/validate", json={"source": bad, "name": NAME}).json()["issues"]
    assert issue["node_id"] == "test" and issue["field"] == "uses"
    # without a name, prompt files resolve against the pipelines dir root (and are missing)
    issues = client.post("/api/validate", json={"source": src}).json()["issues"]
    assert {i["node_id"] for i in issues} >= {"plan", "implement"}


def test_validate_a_model_without_saving(client: TestClient, pdir: Path) -> None:
    got = client.get(f"/api/pipelines/{NAME}").json()
    model = got["model"]
    node(model, "test")["fields"]["uses"] = "env.nope"
    body = client.post(
        "/api/validate", json={"source": got["source"], "name": NAME, "model": model}
    ).json()
    assert "uses: env.nope" in body["source"] and body["warnings"] == []
    assert [i["node_id"] for i in body["issues"]] == ["test"]
    assert (pdir / "ticket-to-eod" / "pipeline.yaml").read_text() == got["source"]


def test_node_types(client: TestClient) -> None:
    t = client.get("/api/node-types").json()
    assert set(t["types"]) == {"agent", "action", "router", "gate", "wait", "chat"}
    agent = {f["name"]: f for f in t["types"]["agent"]["fields"]}
    assert not agent["runner"]["required"] and agent["runner"]["kind"] == "select"
    assert agent["harness"]["kind"] == "select" and agent["harness"]["source"] == "harnesses"
    assert "agent" not in agent  # the old name of `harness`: read, never offered
    assert agent["model"]["source"] == "models"
    assert "claude-code" in t["harnesses"] and "builtin" in t["harnesses"]
    assert agent["prompt"]["kind"] == "prompt" and agent["in"]["kind"] == "list"
    assert "type" not in agent
    assert "claude-code" in t["runners"] and "env.deploy" in t["deploy_class_actions"]
    assert t["waiters"] == ["chat.ask", "github.review"] and "container" in t["workspaces"]


def test_bearer_token(tmp_path: Path, pdir: Path) -> None:
    ledger = SqliteLedger(tmp_path / "l2.db")
    server = create_server(App(ledger), [], pipelines_dir=pdir, token="s3cret")
    with TestClient(server) as c:
        assert c.get("/api/pipelines").status_code == 401
        assert c.get("/api/pipelines", headers={"Authorization": "Bearer nope"}).status_code == 401
        ok = c.get("/api/pipelines", headers={"Authorization": "Bearer s3cret"})
        assert ok.status_code == 200
        assert c.get("/healthz").status_code == 200  # webhooks keep their own auth
        assert c.post("/webhooks/jira", content=b"{}").status_code == 401
    ledger.close()


def test_static_ui_is_served(tmp_path: Path, pdir: Path) -> None:
    static = tmp_path / "dist"
    static.mkdir()
    (static / "index.html").write_text("<!doctype html><title>agentline</title>")
    ledger = SqliteLedger(tmp_path / "l3.db")
    server = create_server(App(ledger), [], pipelines_dir=pdir, static_dir=static, token="")
    with TestClient(server) as c:
        assert "agentline" in c.get("/").text
        assert c.get("/api/pipelines").status_code == 200
    ledger.close()


def test_delete_a_pipeline_in_its_own_folder_takes_the_folder(
    client: TestClient, pdir: Path
) -> None:
    (pdir / "loose.yaml").write_text(
        "version: 1\nname: loose\nnodes:\n"
        "  a: { type: action, uses: shell.run, with: { cmd: 'true' } }\n"
    )
    (pdir / "shared.md").write_text("used by loose")
    r = client.delete(f"/api/pipelines/{NAME}")
    assert r.json() == {"deleted": NAME, "removed": "ticket-to-eod/"}
    assert not (pdir / "ticket-to-eod").exists()
    # a loose file goes alone: files beside it may be shared
    gone = client.delete("/api/pipelines/loose").json()
    assert gone == {"deleted": "loose", "removed": "loose.yaml"}
    assert not (pdir / "loose.yaml").exists() and (pdir / "shared.md").exists()
    assert client.get("/api/pipelines").json()["pipelines"] == []
    assert client.delete("/api/pipelines/loose").status_code == 404
    assert client.delete("/api/pipelines/..%2Fsecret").status_code in (400, 404)


def test_delete_keeps_a_folder_that_holds_another_pipeline(client: TestClient, pdir: Path) -> None:
    other = pdir / "ticket-to-eod" / "variant.yaml"
    other.write_text((pdir / "ticket-to-eod" / "pipeline.yaml").read_text().replace(
        "name: ticket-to-eod", "name: variant", 1))  # fmt: skip
    assert client.delete(f"/api/pipelines/{NAME}").json()["removed"] == f"{NAME}.yaml"
    assert other.exists() and (pdir / "ticket-to-eod" / "prompts").is_dir()
