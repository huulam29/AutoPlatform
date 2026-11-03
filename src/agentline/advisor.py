"""The effect advisor (ARCHITECTURE decisions 3 and 78): the Chat's harness decides what to do
with an external side effect when the engine can't tell on its own.

It is asked when a crash left an effect's outcome unknown under `on_unknown: ai`, and when an
effect with neither an idempotency key nor a check failed cleanly on the previous attempt. It
answers `retry` (do it again), `halt` or `human` (stop for a person), with a reason. Anything it
can't answer, or an answer that doesn't parse, stops for a person. It is never asked about a
deploy-class effect. `serve` installs it; `AGENTLINE_ADVISOR=off` keeps it out (tests and harnesses
set it, because it would call a real agent). Its cost counts against the run's budget.
"""

from __future__ import annotations

import os
from typing import Any

from agentline.context import Advice, AdviceRequest, Advisor

PROMPT = """You decide whether an automated delivery pipeline may repeat an external side effect.

Run {run}, step {node} (iteration {iteration}), effect {effect!r}: {description}
{situation}
Repeating it could do the same thing twice (a second comment, a duplicate push, two charges).
Answer `retry` only when doing it again is safe or the earlier try clearly did not happen.
Answer `halt` when it must not be repeated, and `human` when a person should look.

Reply with JSON only: {{"decision": "retry" | "halt" | "human", "reason": "<one sentence>"}}"""

FAILED = (
    "The previous attempt's call failed with this error, so it may or may not have taken"
    " effect:\n{error}\n"
)
UNKNOWN = "The worker stopped around the call, so it is not known whether it happened.\n"


def enabled() -> bool:
    return os.environ.get("AGENTLINE_ADVISOR", "").strip().lower() not in ("off", "0", "false")


def build_prompt(req: AdviceRequest) -> str:
    failed = req.outcome == "failed"
    situation = FAILED.format(error=req.error or "(no message)") if failed else UNKNOWN
    return PROMPT.format(
        run=req.run_id,
        node=req.node_id,
        iteration=req.iteration,
        effect=req.effect,
        description=req.description or "(no description)",
        situation=situation,
    )


def parse_advice(output: Any, cost: float = 0.0) -> Advice:
    """The model's answer as Advice; anything unclear means a person decides."""
    from agentline.runners.common import parse_json_text

    value = output
    if isinstance(output, str):
        ok, parsed = parse_json_text(output)
        value = parsed if ok else None
    if not isinstance(value, dict):
        return Advice("human", "the advisor's answer was not JSON", cost)
    decision = str(value.get("decision") or "").strip().lower()
    reason = str(value.get("reason") or "").strip()[:500]
    if decision not in ("retry", "halt", "human"):
        return Advice("human", f"the advisor answered {decision!r}", cost)
    return Advice(decision, reason or "no reason given", cost)  # type: ignore[arg-type]


def make_advisor(files: Any) -> Advisor:
    """An advisor that asks the harness and model the Chat uses."""

    async def advise(req: AdviceRequest) -> Advice:
        from agentline import assistant
        from agentline.triage import _chat_harness

        harness, model = _chat_harness()
        try:
            output, cost = await assistant._ask(files, harness, model, build_prompt(req))
        except assistant.AssistantError as e:
            return Advice("human", f"the advisor could not run: {e}")
        return parse_advice(output, float(cost or 0.0))

    return advise
