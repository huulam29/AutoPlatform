"""ArtifactStore: inline and file round trips, dedupe, atomic writes, legacy blobs."""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest

from agentline import SqliteLedger
from agentline.artifacts import ArtifactStore
from agentline.ids import canonical_json, content_hash


@pytest.fixture
def ledger(tmp_path: Path) -> Iterator[SqliteLedger]:
    ledger = SqliteLedger(tmp_path / "ledger.db")
    yield ledger
    ledger.close()


@pytest.fixture
def store(tmp_path: Path) -> ArtifactStore:
    return ArtifactStore(tmp_path / "artifacts", inline_max=1024)


def count_rows(ledger: SqliteLedger) -> int:
    with ledger.tx() as tx:
        return int(tx.c.execute("SELECT COUNT(*) FROM artifacts").fetchone()[0])


def test_json_inline_round_trip(ledger: SqliteLedger, store: ArtifactStore) -> None:
    value = {"summary": "Fix login", "steps": ["a", "b", "c"]}
    with ledger.tx() as tx:
        art = store.put_json(tx, value)
        assert art.hash == content_hash(canonical_json(value).encode())
        assert (art.kind, art.storage, art.path, art.media_type) == (
            "json",
            "inline",
            None,
            "application/json",
        )
        assert store.load(tx, art.hash) == value
        assert tx.get_blob(art.hash) == canonical_json(value).encode()
    assert not store.root.exists()  # nothing touches disk for small artifacts


def test_large_json_goes_to_a_file(ledger: SqliteLedger, store: ArtifactStore) -> None:
    value = {"text": "x" * 5000}
    with ledger.tx() as tx:
        art = store.put_json(tx, value)
        assert art.storage == "file" and art.path is not None
        assert Path(art.path) == store.root / art.hash[:2] / art.hash
        assert tx.get_blob(art.hash) is None
        assert store.load(tx, art.hash) == value
    assert Path(art.path).read_bytes() == canonical_json(value).encode()


def test_file_artifacts_inline_and_on_disk(ledger: SqliteLedger, store: ArtifactStore) -> None:
    small, big = b"hello\nworld\n", os.urandom(4096)
    with ledger.tx() as tx:
        a = store.put_bytes(tx, small, "text/plain")
        b = store.put_bytes(tx, big, "application/octet-stream")
        assert (a.kind, a.storage, b.storage) == ("file", "inline", "file")
        assert store.load(tx, a.hash) == small and store.load(tx, b.hash) == big
        assert a.summary == "hello\nworld\n(2 lines)"
        assert b.summary == "application/octet-stream, 4.0 KiB"


def test_dedupe(ledger: SqliteLedger, store: ArtifactStore) -> None:
    calls: list[str] = []

    def summarizer(value: object, media_type: str) -> str:
        calls.append(media_type)
        return "s"

    store.summarizer = summarizer
    with ledger.tx() as tx:
        one = store.put_json(tx, {"a": 1, "b": "x" * 2000})
        two = store.put_json(tx, {"b": "x" * 2000, "a": 1})  # same canonical JSON
        assert one == two and calls == ["application/json"]
    assert count_rows(ledger) == 1
    files = [p for p in store.root.rglob("*") if p.is_file()]
    assert files == [store.root / one.hash[:2] / one.hash]


def test_file_writes_are_atomic(
    ledger: SqliteLedger, store: ArtifactStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(src: str, dst: str) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError, match="disk full"), ledger.tx() as tx:
        store.put_bytes(tx, b"y" * 4096, "text/plain")
    assert [p for p in store.root.rglob("*") if p.is_file()] == []  # no partial or temp file
    assert count_rows(ledger) == 0
    monkeypatch.undo()
    with ledger.tx() as tx:
        art = store.put_bytes(tx, b"y" * 4096, "text/plain")
        assert store.load(tx, art.hash) == b"y" * 4096


def test_materialize_inline_artifact(ledger: SqliteLedger, store: ArtifactStore) -> None:
    with ledger.tx() as tx:
        art = store.put_json(tx, {"k": "v"})
        path = store.materialize(tx, art)
        ref = store.ref(tx, art)
    assert path.read_bytes() == b'{"k":"v"}'
    assert ref.path == str(path) and ref.read_file() == {"k": "v"}
    assert ref.as_dict()["$artifact"] == art.hash


def test_legacy_result_blob_still_loads(ledger: SqliteLedger, store: ArtifactStore) -> None:
    with ledger.tx() as tx:
        ref = tx.put_blob(b'{"old":true}')  # written by a Phase 1/2 engine: no artifact row
        assert store.meta(tx, ref) is None
        assert store.load(tx, ref) == {"old": True}


def test_root_defaults_to_agentline_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENTLINE_HOME", str(tmp_path / "home"))
    assert ArtifactStore().root == tmp_path / "home" / "artifacts"
