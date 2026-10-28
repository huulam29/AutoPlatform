"""The built-in harness's Chat turn: a small tool loop driven by Pydantic AI (phase 36).

Used when the Chat's harness kind is `builtin` (only an API key, no CLI). Pydantic AI speaks
both wire formats (OpenAI-compatible `/chat/completions` and Anthropic Messages), calls the
read-only lookup tools below, and ends with a typed `reply` ({message, choices, actions}) that
is checked like every Chat reply (`assistant.check_actions`); the first failed check goes back
to the model as a retry. Models that reject tool calling get the same turn as prompted JSON.

Self-improving: what the harness can't handle is recorded as an App report (an incident of kind
`chat-harness`, see incidents.py / triage.triage_incidents), which can become a GitHub issue:
  gap          the model called `cant_do`: the request needs something the harness lacks
  loop         the same lookup 3 times, or the lookup budget spent without a reply
  invalid      the reply's actions still fail the checks after the retry
  unparseable  no usable reply even after the fallbacks
  unknown      a tool or action type that doesn't exist
  error        an exception in this code or a tool (a bug here)
Reports carry the trace (tool names, argument sizes, action types, issues), never the
conversation, pipeline text or lookup results. Problems the user fixes (a missing or wrong
key, rate limits, an unreachable server, an unknown model) are not reported.

Needs the `server` extra (`pydantic-ai-slim[openai,anthropic]`).
"""

from __future__ import annotations

import asyncio
import json
import os
import traceback
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field
from pydantic_ai import RunContext

MAX_LOOKUPS = 5  # lookups per turn before the model must reply (the user's "5 times")
SAME_CALL_LOOP = 3  # the same lookup this many times is a loop
OUTPUT_RETRIES = 2  # the checks' retry + one more for a reply that doesn't parse
REQUEST_LIMIT = MAX_LOOKUPS + 4  # model requests per turn: lookups + reply + retries
CHAT_MAX_TOKENS = 16_000  # a pipeline action (YAML + prompt files) needs room
RESULT_CAP = 20_000  # characters of one lookup result
INCIDENT_KIND = "chat-harness"
USER_SIDE_STATUS = (401, 402, 403, 404, 408, 429)
_NO_TOOLS: set[str] = set()  # "<base url> <model>" that rejected tool calling (this process)

HARNESS_NOTE = """\
## THIS HARNESS (built-in)
You run inside agentline's built-in harness. Look things up with your tools when you need
them (a pipeline's prompt or script files, a run's steps and journal, the engine log, a skill's
text); LIVE STATE and the OPEN PIPELINE are already above. Then answer with the `reply` tool:
its arguments are the JSON object described above (message, choices, actions). At most
{max_lookups} lookups per turn.
If the user asks for something no action or tool here can do, call `cant_do` once (what and
why), then reply telling the user it isn't possible yet. Never pretend an action exists."""


class Reply(BaseModel):
    """The Chat's answer: what to say, quick replies, and proposed actions."""

    message: str = Field(description="what you say to the user (short markdown)")
    choices: list[str] = Field(default_factory=list, description="at most 5 quick replies")
    actions: list[dict[str, Any]] = Field(
        default_factory=list, description="proposed actions; each has a `type` (see the guide)"
    )


@dataclass
class Turn:
    """What one turn's tools can see, and what they did (the trace for reports)."""

    files: Any
    app: Any
    live: Any
    steps: list[str] = field(default_factory=list)  # shown under the reply ("Looked at")
    trace: list[dict[str, Any]] = field(default_factory=list)  # for App reports
    calls: Counter[str] = field(default_factory=Counter)
    lookups: int = 0
    loop: bool = False
    gaps: list[dict[str, str]] = field(default_factory=list)
    unknown: list[str] = field(default_factory=list)
    checks: int = 0
    issues: list[str] = field(default_factory=list)
    last: Any = None  # the last reply that reached the checks (kept if a retry goes nowhere)


class HarnessError(Exception):
    """A turn that couldn't run; `user_side` problems are the user's to fix (no report)."""

    def __init__(self, message: str, *, user_side: bool) -> None:
        super().__init__(message)
        self.user_side = user_side


# -- tools ------------------------------------------------------------------------------------
def _cap(text: str) -> str:
    return text if len(text) <= RESULT_CAP else text[:RESULT_CAP] + "\n[... cut]"


def _note(turn: Turn, name: str, args: dict[str, Any], step: str) -> str | None:
    """Count a lookup; a refusal text when it's a repeat or over budget."""
    key = name + json.dumps(args, sort_keys=True, default=str)
    turn.calls[key] += 1
    turn.lookups += 1
    turn.trace.append({"tool": name, "args": {k: len(str(v)) for k, v in args.items()}})
    if turn.calls[key] >= SAME_CALL_LOOP:
        turn.loop = True
        return "You already looked this up. Answer now with the reply tool."
    if turn.lookups > MAX_LOOKUPS:
        turn.loop = True
        return "No lookups left this turn. Answer now with the reply tool."
    turn.steps.append(step)
    return None


def read_pipeline_file(ctx: RunContext[Turn], path: str) -> str:
    """A file under the pipelines folder: a pipeline.yaml, or its prompts/, scripts/, schemas/."""
    turn = ctx.deps
    from agentline.assistant import _safe_rel

    if stop := _note(turn, "read_pipeline_file", {"path": path}, f"read {path}"):
        return stop
    root = Path(turn.files.root)
    if not _safe_rel(path):
        return f"error: {path!r} is not a relative path inside the pipelines folder"
    p = root / path
    if not p.is_file():
        return f"error: no file {path!r}"
    return _cap(p.read_text(errors="replace"))


def run_details(ctx: RunContext[Turn], run_id: str, node: str = "") -> str:
    """A run's status, its steps (executions) and the last journal lines, optionally one node."""
    turn = ctx.deps
    from dataclasses import asdict

    from agentline.api.runs import execution_dict, run_dict

    if stop := _note(turn, "run_details", {"run_id": run_id, "node": node}, f"run {run_id}"):
        return stop
    app = turn.app
    if app is None:
        return "error: runs aren't available here"
    with app.ledger.tx() as tx:
        run = tx.get_run(run_id)
        if run is None:
            return f"error: no run {run_id!r}"
        execs = [x for x in tx.list_executions(run_id) if not node or x.node_id == node]
        journal = tx.list_journal(run_id, node_id=node or None, limit=200)
    out = {
        "run": run_dict(run),
        "executions": [execution_dict(x) for x in execs],
        "journal": [asdict(e) for e in journal],
    }
    return _cap(json.dumps(out, indent=1, default=str))


def engine_logs(ctx: RunContext[Turn], limit: int = 200) -> str:
    """The engine's last log lines (secrets redacted), newest last."""
    turn = ctx.deps
    from agentline.logs import RING

    if stop := _note(turn, "engine_logs", {"limit": limit}, "engine log"):
        return stop
    n = max(1, min(int(limit), 300))
    return _cap("\n".join(list(RING.lines)[-n:]) or "(no log lines)")


def read_skill(ctx: RunContext[Turn], name: str) -> str:
    """The full text of a skill from SKILLS."""
    turn = ctx.deps
    from agentline.skills import SkillLibrary

    if stop := _note(turn, "read_skill", {"name": name}, f"skill {name}"):
        return stop
    for s in SkillLibrary().list():
        if s.name == name:
            return _cap(s.body)
    return f"error: no skill {name!r}"


def cant_do(ctx: RunContext[Turn], what: str, why: str) -> str:
    """The user asked for something no action or tool here can do. Call once, then reply."""
    turn = ctx.deps
    turn.gaps.append({"what": what[:300], "why": why[:500]})
    turn.trace.append({"tool": "cant_do"})
    return "Noted for agentline's maintainers. Now reply and tell the user it isn't possible yet."


TOOLS = (read_pipeline_file, run_details, engine_logs, read_skill, cant_do)


# -- the model --------------------------------------------------------------------------------
def build_model(res: Any) -> Any:
    """A Pydantic AI model for a resolved builtin harness (agents.resolve)."""
    from pydantic_ai.models.anthropic import AnthropicModel
    from pydantic_ai.models.openai import OpenAIChatModel
    from pydantic_ai.providers.anthropic import AnthropicProvider
    from pydantic_ai.providers.openai import OpenAIProvider

    p = res.params
    key_env = p.get("api_key_env")
    key = os.environ.get(str(key_env)) if key_env else None
    if key_env and not key:
        msg = f"{key_env} is not set (Settings > Models: the source's menu, Change key)"
        raise HarnessError(msg, user_side=True)
    # no SDK retries: the Chat is interactive, and a rate limit is for the user to see
    if p.get("provider_type") == "anthropic":
        from anthropic import AsyncAnthropic

        aclient = AsyncAnthropic(
            api_key=key or "none", base_url=p.get("base_url") or None, max_retries=0
        )
        return AnthropicModel(p["model"], provider=AnthropicProvider(anthropic_client=aclient))
    from openai import AsyncOpenAI

    client = AsyncOpenAI(
        base_url=p["base_url"],
        api_key=key or "none",
        default_headers=dict(p.get("headers") or {}),
        max_retries=0,
    )
    provider = OpenAIProvider(openai_client=client)
    profile = provider.model_profile(p["model"])
    if "api.openai.com" not in str(p["base_url"]):
        # OpenRouter, Ollama, vLLM, LM Studio... read `max_tokens`, not OpenAI's newer
        # `max_completion_tokens`: without it a pipeline reply is cut at their default limit
        from pydantic_ai.profiles import merge_profile
        from pydantic_ai.profiles.openai import OpenAIModelProfile

        profile = merge_profile(
            profile, OpenAIModelProfile(openai_chat_supports_max_completion_tokens=False)
        )
    return OpenAIChatModel(p["model"], provider=provider, profile=profile)


def _history(messages: list[dict[str, Any]], transcripts: dict[str, dict[str, Any]]) -> Any:
    """(earlier turns as Pydantic AI messages, the last user message's text)."""
    from pydantic_ai.messages import ModelRequest, ModelResponse, TextPart, UserPromptPart

    from agentline.assistant import MAX_HISTORY

    def text(m: dict[str, Any]) -> str:
        out = str(m.get("content") or "")[:20_000]
        for a in m.get("attachments") or []:
            t = transcripts.get(str(a))
            if t:
                who = "the user (edited)" if t.get("edited") else t.get("by")
                out += (
                    f"\n[The user attached an image. You can't see it; this transcript of it is"
                    f" by {who}. Build from it; if a part is UNCLEAR, ask.]\n{t['text']}"
                )
        return out

    msgs = messages[-MAX_HISTORY:]
    history: list[Any] = []
    for m in msgs[:-1]:
        if m.get("role") == "user":
            history.append(ModelRequest(parts=[UserPromptPart(content=text(m))]))
        else:
            history.append(ModelResponse(parts=[TextPart(content=str(m.get("content") or ""))]))
    return history, text(msgs[-1])


def _cost(usage: Any, res: Any, pricing: dict[str, Any]) -> float:
    from agentline.runners.anthropic import price_for

    price = price_for(str(res.params.get("model")), res.params, pricing)
    if price is None:
        return 0.0
    tokens_in, tokens_out = int(usage.input_tokens or 0), int(usage.output_tokens or 0)
    return (tokens_in * price[0] + tokens_out * price[1]) / 1_000_000


def _rejects_tools(e: Exception) -> bool:
    from pydantic_ai.exceptions import ModelHTTPError

    text = str(getattr(e, "body", "") or e).lower()
    return (
        isinstance(e, ModelHTTPError)
        and e.status_code in (400, 404, 422)
        and any(w in text for w in ("tool", "function"))
    )


def _user_side(e: Exception) -> str | None:
    """The message for an error the user fixes (key, rate limit, server, model), else None."""
    from pydantic_ai.exceptions import ModelAPIError, ModelHTTPError

    if isinstance(e, HarnessError) and e.user_side:
        return str(e)
    if isinstance(e, ModelHTTPError) and e.status_code in USER_SIDE_STATUS:
        what = {
            401: "the key was refused",
            403: "the key isn't allowed to do this",
            404: "the model or URL wasn't found",
            429: "rate limited (try again soon)",
            402: "the account is out of credit",
            408: "the model server timed out",
        }
        return f"model API {e.status_code}: {what[e.status_code]} ({str(e.body or '')[:200]})"
    if isinstance(e, ModelAPIError) and not isinstance(e, ModelHTTPError):
        return f"the model server can't be reached: {e}"
    if isinstance(e, (ConnectionError, TimeoutError, asyncio.TimeoutError)):
        return f"the model server can't be reached: {e}"
    return None


def _provider_failed(e: Exception) -> str | None:
    """The message when the provider itself failed the request (OpenRouter's
    `finish_reason: "error"`: the upstream model broke mid-reply), else None."""
    text = str(e)
    if "finish_reason" in text and "input_value='error'" in text:
        return (
            'the model\'s provider failed while answering (finish_reason "error"); try again,'
            " or pick another model if it keeps happening"
        )
    return None


# -- one turn ---------------------------------------------------------------------------------
async def run_turn(
    files: Any,
    res: Any,
    instructions: str,
    messages: list[dict[str, Any]],
    *,
    check: Any,
    app: Any = None,
    live: Any = None,
    incidents: Any = None,
    transcripts: dict[str, dict[str, Any]] | None = None,
    pricing: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """One Chat turn on the built-in harness. `res` is the agents.resolve() result, `check`
    turns raw actions into checked ones (assistant.check_actions bound to this turn).
    Returns {reply: {message, choices, actions (checked)}, cost_usd, steps, report}."""
    from pydantic_ai import Agent, ModelRetry, PromptedOutput, ToolOutput
    from pydantic_ai.exceptions import UnexpectedModelBehavior, UsageLimitExceeded
    from pydantic_ai.toolsets import FunctionToolset
    from pydantic_ai.usage import UsageLimits

    from agentline.assistant import ACTION_TYPES, repair_note

    turn = Turn(files, app, live)
    model = build_model(res)
    key = f"{res.params.get('base_url')} {res.params.get('model')}"
    history, prompt = _history(messages, transcripts or {})
    settings: dict[str, Any] = {"max_tokens": int(res.params.get("max_tokens") or CHAT_MAX_TOKENS)}
    if res.params.get("temperature") is not None:
        settings["temperature"] = float(res.params["temperature"])

    toolset: FunctionToolset[Turn] = FunctionToolset(list(TOOLS))

    def validate(ctx: RunContext[Turn], reply: Reply) -> Reply:
        t = ctx.deps
        t.checks += 1
        types = [str(a.get("type")) for a in reply.actions]
        t.unknown += [x for x in types if x not in ACTION_TYPES]
        checked = check(reply.actions)
        note = repair_note(checked)
        t.trace.append({"reply": types, "issues": sum(len(a["issues"]) for a in checked)})
        t.last, t.issues = reply, [i for a in checked for i in a["issues"]]
        if note and t.checks == 1:
            raise ModelRetry(note)
        return reply

    def agent(tools: bool) -> Any:
        out: Any = PromptedOutput(Reply)  # JSON in the text: models without tool calling
        if tools:
            out = ToolOutput(Reply, name="reply", max_retries=OUTPUT_RETRIES)
        a = Agent(
            model,
            output_type=out,
            deps_type=Turn,
            instructions=instructions + "\n\n" + HARNESS_NOTE.format(max_lookups=MAX_LOOKUPS),
            toolsets=[toolset] if tools else [],
            retries={"tools": 1, "output": OUTPUT_RETRIES},
            model_settings=settings,
        )
        a.output_validator(validate)
        return a

    limits = UsageLimits(request_limit=REQUEST_LIMIT)
    report = None
    reply: Reply | None = None
    cost = 0.0
    tools = key not in _NO_TOOLS
    try:
        try:
            result = await agent(tools).run(
                prompt, message_history=history, deps=turn, usage_limits=limits
            )
        except Exception as e:  # a model that can't call tools: the same turn as prompted JSON
            if not (tools and _rejects_tools(e)):
                raise
            _NO_TOOLS.add(key)
            turn.trace.append({"fallback": "prompted-json"})
            result = await agent(False).run(
                prompt, message_history=history, deps=turn, usage_limits=limits
            )
        reply = result.output
        cost = _cost(result.usage, res, pricing or {})
    except UsageLimitExceeded:
        turn.loop = True
    except UnexpectedModelBehavior as e:
        if user := _provider_failed(e):
            raise HarnessError(user, user_side=True) from None
        if turn.last is not None:  # the retry after the checks went nowhere: keep that reply
            reply = turn.last  # reported below as `invalid` (turn.issues)
        else:
            report = _report(incidents, "unparseable", turn, res, str(e))
    except Exception as e:
        user = _user_side(e)
        if user is not None:
            raise HarnessError(user, user_side=True) from None
        report = _report(incidents, "error", turn, res, f"{type(e).__name__}: {e}", exc=e)
        raise HarnessError(f"the built-in harness failed: {e}", user_side=False) from None

    if report is None:  # the first problem wins: one report per turn
        if turn.gaps:
            g = turn.gaps[0]
            report = _report(incidents, "gap", turn, res, f"{g['what']} ({g['why']})")
        elif turn.unknown:
            report = _report(incidents, "unknown", turn, res, ", ".join(sorted(set(turn.unknown))))
        elif turn.loop:
            report = _report(incidents, "loop", turn, res, "lookups without a reply")
        elif turn.issues:
            report = _report(incidents, "invalid", turn, res, "; ".join(turn.issues[:5]))
    if reply is None:
        reply = Reply(
            message="I couldn't finish this turn (the built-in harness got stuck)."
            + (" It's in Logs > App reports." if report else ""),
        )
    return {
        "reply": {"message": reply.message, "choices": [c for c in reply.choices if c][:5],
                  "actions": check(reply.actions)},
        "cost_usd": cost,
        "steps": turn.steps,
        "report": report,
    }  # fmt: skip


# -- App reports ------------------------------------------------------------------------------
TITLES = {
    "gap": "Chat can't do something users ask for",
    "loop": "Chat harness looped without answering",
    "invalid": "Chat harness proposed actions that fail the checks",
    "unparseable": "Chat harness got no usable reply from the model",
    "unknown": "Chat harness model used a tool or action that doesn't exist",
    "error": "Chat harness crashed",
}


def _report(
    incidents: Any, cls: str, turn: Turn, res: Any, detail: str, exc: BaseException | None = None
) -> dict[str, Any] | None:
    """Record the App report for a harness problem (never raises). None when nothing records."""
    from agentline import __version__
    from agentline.incidents import IncidentStore, top_frames

    store = incidents if incidents is not None else IncidentStore(source="chat")
    names = sorted({str(s.get("tool")) for s in turn.trace if s.get("tool")})
    context = {
        "class": cls,
        "detail": detail[:1000],
        "harness": res.harness.name,
        "provider_type": res.params.get("provider_type"),
        "model": res.params.get("model"),
        "trace": turn.trace[-30:],
        "agentline": __version__,
    }
    try:
        rec = store.record(
            INCIDENT_KIND,
            title=f"{TITLES[cls]} ({cls})",
            message=f"{cls}: {', '.join(names) or 'no tools'}"
            + (f" | {detail[:200]}" if cls in ("gap", "unknown") else ""),
            exception_type=type(exc).__name__ if exc else None,
            traceback="".join(traceback.format_exception(exc)) if exc else "",
            frames=top_frames(exc) if exc else None,
            context=context,
        )
    except Exception:  # reporting must never break the turn
        return None
    return {"id": rec.get("id"), "class": cls, "title": TITLES[cls]} if rec else None
