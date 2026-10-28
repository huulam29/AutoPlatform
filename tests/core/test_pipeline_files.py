"""One rule for which files are pipelines: the engine runs what the editor lists."""

from __future__ import annotations

from pathlib import Path

from agentline.pipeline.files import pipeline_files


def test_top_level_and_own_folders_but_not_history_or_tools(tmp_path: Path) -> None:
    for rel in ["a.yaml", "b.yml", "dev-cycle/pipeline.yaml", ".agentline-history/x.yaml",
                "node_modules/m.yaml", "dev-cycle/.cache/c.yaml", "notes.md"]:  # fmt: skip
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("x")
    got = [p.relative_to(tmp_path).as_posix() for p in pipeline_files(tmp_path)]
    assert got == ["a.yaml", "b.yml", "dev-cycle/pipeline.yaml"]
    assert pipeline_files(tmp_path / "missing") == []
