"""The effect advisor's prompt and answer parsing (the model call itself is the Chat's)."""

from __future__ import annotations

import pytest

from agentline.advisor import build_prompt, enabled, parse_advice
from agentline.context import AdviceRequest


def req(**kw: object) -> AdviceRequest:
    base = dict(run_id="r1", node_id="post", iteration=1, effect="comment",
                description="comment on PROJ-1", has_check=False, has_key=False)  # fmt: skip
    return AdviceRequest(**{**base, **kw})  # type: ignore[arg-type]


def test_prompt_says_what_happened() -> None:
    assert "worker stopped" in build_prompt(req())
    failed = build_prompt(req(outcome="failed", error="ConnectionError: 503"))
    assert "ConnectionError: 503" in failed and "comment on PROJ-1" in failed


def test_answers_parse_and_anything_unclear_goes_to_a_person() -> None:
    assert parse_advice({"decision": "retry", "reason": "safe"}, 0.02).decision == "retry"
    assert parse_advice('```json\n{"decision": "HALT", "reason": "x"}\n```').decision == "halt"
    assert parse_advice("sure, retry it").decision == "human"
    assert parse_advice({"decision": "maybe"}).decision == "human"
    assert parse_advice({"decision": "retry"}, 0.5).cost_usd == 0.5


def test_off_switch(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENTLINE_ADVISOR", "off")
    assert not enabled()
    monkeypatch.setenv("AGENTLINE_ADVISOR", "")
    assert enabled()
