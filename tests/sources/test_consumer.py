from __future__ import annotations

import sys
import types
from pathlib import Path
from typing import Any

import pytest

from agentline.sources import ConfluentConsumer, FakeBroker, KafkaConsumerError, TopicPartition

TP0 = TopicPartition("t", 0)
TP1 = TopicPartition("t", 1)


def test_fake_broker_keys_partitions_and_offsets(tmp_path: Path) -> None:
    b = FakeBroker(tmp_path / "b.db")
    b.create_topic("t", 3)
    first = b.produce("t", "a", key="same")
    second = b.produce("t", "b", key="same")
    assert first[0] == second[0]
    assert second[1] == first[1] + 1
    assert sum(b.end_offsets("t").values()) == 2
    with pytest.raises(KeyError):
        b.produce("missing", "x")


def test_fake_consumer_resumes_from_the_committed_offset(tmp_path: Path) -> None:
    b = FakeBroker(tmp_path / "b.db")
    b.create_topic("t", 1)
    for i in range(3):
        b.produce("t", str(i))
    c = b.consumer("g")
    assigned: list[TopicPartition] = []
    c.subscribe(["t"], on_assign=assigned.extend)
    assert [c.poll(0).offset for _ in range(3)] == [0, 1, 2]  # type: ignore[union-attr]
    assert assigned == [TP0]
    c.commit({TP0: 1})

    # a new process (new connection), same group: redelivers offsets 1 and 2
    c2 = FakeBroker(tmp_path / "b.db").consumer("g")
    c2.subscribe(["t"])
    assert [c2.poll(0).offset for _ in range(2)] == [1, 2]  # type: ignore[union-attr]
    assert c2.poll(0) is None
    assert {m["offset"]: m["deliveries"] for m in b.messages("t")} == {0: 1, 1: 2, 2: 2}


def test_fake_consumer_pause_keeps_position(tmp_path: Path) -> None:
    b = FakeBroker(tmp_path / "b.db")
    b.create_topic("t", 2)
    b.produce("t", "a", partition=0)
    b.produce("t", "b", partition=0)
    b.produce("t", "c", partition=1)
    c = b.consumer("g")
    c.subscribe(["t"])
    c.poll(0)
    c.pause([TP0])
    rest = [c.poll(0) for _ in range(3)]
    assert [(m.partition, m.offset) for m in rest if m] in ([(1, 0)], [])
    c.resume([TP0])
    seen = {(m.partition, m.offset) for m in (c.poll(0) for _ in range(4)) if m}
    assert (0, 1) in seen


def test_fake_consumer_rebalance_calls_back_inside_poll(tmp_path: Path) -> None:
    b = FakeBroker(tmp_path / "b.db")
    b.create_topic("t", 2)
    c = b.consumer("g")
    events: list[tuple[str, list[TopicPartition]]] = []
    c.subscribe(
        ["t"],
        on_assign=lambda ps: events.append(("assign", ps)),
        on_revoke=lambda ps: events.append(("revoke", ps)),
    )
    c.poll(0)
    c.reassign([TP1])
    assert events == [("assign", [TP0, TP1])]
    c.poll(0)
    assert events[-1] == ("revoke", [TP0])
    assert c.assignment() == {TP1}


# -- confluent adapter (against a stand-in module; no broker here) ------------------------------
class _TP:
    def __init__(self, topic: str, partition: int, offset: int = -1001) -> None:
        self.topic, self.partition, self.offset = topic, partition, offset


class _Err:
    def __init__(self, code: int, fatal: bool = False) -> None:
        self._code, self._fatal = code, fatal

    def code(self) -> int:
        return self._code

    def fatal(self) -> bool:
        return self._fatal

    def __str__(self) -> str:
        return f"err {self._code}"


class _Msg:
    def __init__(self, err: _Err | None = None) -> None:
        self._err = err

    def error(self) -> _Err | None:
        return self._err

    def topic(self) -> str:
        return "t"

    def partition(self) -> int:
        return 1

    def offset(self) -> int:
        return 7

    def value(self) -> bytes:
        return b"{}"

    def key(self) -> bytes:
        return b"k"

    def headers(self) -> None:
        return None


class _Consumer:
    def __init__(self, conf: dict[str, Any]) -> None:
        self.conf = conf
        self.calls: list[tuple[str, Any]] = []
        self.queue: list[Any] = []

    def subscribe(self, topics: list[str], **kw: Any) -> None:
        self.calls.append(("subscribe", (topics, sorted(kw))))
        self.cb = kw

    def poll(self, timeout: float) -> Any:
        return self.queue.pop(0) if self.queue else None

    def commit(self, *, offsets: list[_TP], asynchronous: bool) -> None:
        self.calls.append(("commit", ([(o.topic, o.partition, o.offset) for o in offsets],)))
        assert asynchronous is False

    def pause(self, ps: list[_TP]) -> None:
        self.calls.append(("pause", [(p.topic, p.partition) for p in ps]))

    def resume(self, ps: list[_TP]) -> None:
        self.calls.append(("resume", [(p.topic, p.partition) for p in ps]))

    def assignment(self) -> list[_TP]:
        return [_TP("t", 0)]

    def close(self) -> None:
        self.calls.append(("close", None))


@pytest.fixture
def fake_ck(monkeypatch: pytest.MonkeyPatch) -> types.ModuleType:
    mod = types.ModuleType("confluent_kafka")
    mod.Consumer = _Consumer  # type: ignore[attr-defined]
    mod.TopicPartition = _TP  # type: ignore[attr-defined]
    mod.KafkaError = types.SimpleNamespace(_PARTITION_EOF=-191)  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "confluent_kafka", mod)
    return mod


def test_confluent_adapter_forces_manual_commits(fake_ck: types.ModuleType) -> None:
    c = ConfluentConsumer({"bootstrap.servers": "x", "group.id": "g", "enable.auto.commit": True})
    inner: _Consumer = c._c
    assert inner.conf["enable.auto.commit"] is False
    assert inner.conf["enable.auto.offset.store"] is False
    assert inner.conf["auto.offset.reset"] == "earliest"

    got: list[list[TopicPartition]] = []
    c.subscribe(["t"], on_assign=got.append, on_revoke=got.append)
    assert inner.calls[0] == ("subscribe", (["t"], ["on_assign", "on_lost", "on_revoke"]))
    inner.cb["on_assign"](inner, [_TP("t", 2)])
    assert got == [[TopicPartition("t", 2)]]

    c.commit({TP1: 5})
    c.pause([TP0])
    c.resume([TP0])
    assert inner.calls[1:] == [
        ("commit", ([("t", 1, 5)],)),
        ("pause", [("t", 0)]),
        ("resume", [("t", 0)]),
    ]
    assert c.assignment() == {TP0}


def test_confluent_adapter_poll_maps_messages_and_errors(fake_ck: types.ModuleType) -> None:
    c = ConfluentConsumer({"group.id": "g"})
    inner: _Consumer = c._c
    inner.queue = [_Msg(), _Msg(_Err(-191)), _Msg(_Err(5)), _Msg(_Err(6, fatal=True))]
    m = c.poll(0)
    assert m is not None
    assert (m.topic, m.partition, m.offset, m.key, m.value) == ("t", 1, 7, b"k", b"{}")
    assert c.poll(0) is None  # partition EOF
    assert c.poll(0) is None  # non-fatal: logged
    with pytest.raises(KafkaConsumerError):
        c.poll(0)


def test_confluent_adapter_names_the_extra_when_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "confluent_kafka", None)
    with pytest.raises(ImportError, match="kafka"):
        ConfluentConsumer({"group.id": "g"})
