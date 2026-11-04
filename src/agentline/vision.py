"""Images for Chat harnesses that can't see them: a vision model transcribes each attached image
into structured text once, and the Chat's agent gets the transcript instead of the picture.

Claude Code reads images itself (its Read tool), so it never needs this. For the other harnesses
(codex, aider, llm on a text model) the transcriber is the "Images" choice under Chat uses:

- `@auto` (the default): Claude Code when its CLI is installed, else the first `[models]` entry
  on an Anthropic API-key source whose key is set;
- `@claude-code`: Claude Code reads the image (subscription or API key, as it is set up);
- a `[models]` name: called directly through the `llm` runner with the image attached
  (Anthropic or an OpenAI-compatible vision model).

Transcripts are cached per image (its id is its content hash) in
`$AGENTLINE_HOME/assistant/transcripts/<id>.json`; an edit by the user wins and is kept.
"""

from __future__ import annotations

import base64
import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any

from agentline import agents as ai
from agentline.errors import ValidationError

# "@" is never in a [models] name, so these never clash with one (a built-in model is "auto")
AUTO = "@auto"
CLAUDE_CODE = "@claude-code"
MAX_TRANSCRIPT = 12_000

PROMPT = """\
Transcribe this image for someone who cannot see it. It is usually a diagram of a pipeline or
workflow (boxes and arrows), sometimes a screenshot. Write plain text, no preamble:

TITLE: what it shows, one line.
STEPS: one line per box or step, in flow order: `- <label>`, then what is written in or next
  to it (agent, model, tool, notes), exactly as written.
ARROWS: one line per arrow: `- <from> -> <to>`, plus its label or condition if any.
GROUPS: steps that run side by side, loops (where they go back, any limit such as "max 2"),
  joins ("wait for all").
OTHER TEXT: anything else written on it (legend, title, notes).
UNCLEAR: what you can't read or aren't sure of. Never guess a label: say it is unreadable.
"""


# -- the choice --------------------------------------------------------------------------------
def choice() -> str:
    from agentline import setup

    v = setup.read_state().get("vision")
    return str(v) if isinstance(v, str) and v else AUTO


def set_choice(value: str, cfg: Any) -> str:
    from agentline import setup

    value = (value or AUTO).strip()
    if value not in (AUTO, CLAUDE_CODE) and value not in ai.models(cfg):
        raise ValidationError(
            f"{value!r} is not a model name: use @auto, @claude-code or a name from [models]"
        )
    state = setup.read_state()
    state["vision"] = value
    setup.write_state(state)
    return value


def _cli_found(cfg: Any) -> bool:
    import shutil

    profile = ai.harnesses(cfg).get("claude-code")
    return shutil.which((profile.bin if profile else None) or "claude") is not None


def _model_ready(name: str, cfg: Any) -> tuple[bool, str]:
    m = ai.models(cfg).get(name)
    if m is None:
        return False, "no such model"
    prov = ai.providers(cfg).get(m.provider)
    if prov is None:
        return False, f"unknown source {m.provider}"
    if prov.type == "anthropic" and prov.auth == "subscription":
        return False, "a subscription source only works through Claude Code"
    if prov.api_key_env and not os.environ.get(prov.api_key_env):
        return False, f"{prov.api_key_env} isn't set"
    return True, prov.type


def options(cfg: Any) -> list[dict[str, Any]]:
    """What the Images select lists: auto, Claude Code, then every named model that could take
    an image (its source has a key, or needs none)."""
    out = [
        {"id": AUTO, "label": "Auto", "available": True},
        {
            "id": CLAUDE_CODE,
            "label": "Claude Code (reads the image itself)",
            "available": _cli_found(cfg),
            "note": None if _cli_found(cfg) else "the claude CLI isn't installed",
        },
    ]
    for name in sorted(ai.models(cfg)):
        ok, why = _model_ready(name, cfg)
        out.append({"id": name, "label": name, "available": ok, "note": None if ok else why})
    return out


def effective(cfg: Any) -> str | None:
    """The transcriber `auto` resolves to (or the explicit choice when it can run)."""
    c = choice()
    if c == CLAUDE_CODE:
        return c if _cli_found(cfg) else None
    if c != AUTO:
        return c if _model_ready(c, cfg)[0] else None
    if _cli_found(cfg):
        return CLAUDE_CODE
    for name in sorted(ai.models(cfg)):
        m = ai.models(cfg)[name]
        prov = ai.providers(cfg).get(m.provider)
        if prov and prov.type == "anthropic" and _model_ready(name, cfg)[0]:
            return name
    return None


def status(cfg: Any) -> dict[str, Any]:
    return {"choice": choice(), "effective": effective(cfg), "options": options(cfg)}


# -- cache -------------------------------------------------------------------------------------
def cache_dir() -> Path:
    home = Path(os.environ.get("AGENTLINE_HOME") or Path.home() / ".agentline")
    return home / "assistant" / "transcripts"


def _cache_path(att_id: str) -> Path:
    safe = "".join(c for c in att_id if c.isalnum() or c in "._-")
    if not safe or safe.startswith("."):
        raise ValidationError(f"bad attachment id {att_id!r}")
    return cache_dir() / f"{safe}.json"


def cached(att_id: str) -> dict[str, Any] | None:
    try:
        data = json.loads(_cache_path(att_id).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) and data.get("text") else None
    except (OSError, ValueError):
        return None


def save(att_id: str, text: str, by: str, *, edited: bool = False, cost: float = 0.0) -> dict:
    rec = {
        "id": att_id,
        "text": text.strip()[:MAX_TRANSCRIPT],
        "by": by,
        "edited": edited,
        "cost_usd": cost,
        "at": time.time(),
    }
    p = _cache_path(att_id)
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=p.parent, prefix=".t-", suffix=".json")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(rec, f, indent=2)
    os.replace(tmp, p)
    return rec


def forget(att_id: str) -> bool:
    try:
        _cache_path(att_id).unlink()
        return True
    except FileNotFoundError:
        return False


# -- transcribing ------------------------------------------------------------------------------
def _media_type(path: Path) -> str:
    return {
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".gif": "image/gif",
        ".webp": "image/webp",
    }.get(path.suffix.lower(), "image/png")


def _text(output: Any) -> str:
    if isinstance(output, str):
        return output
    if isinstance(output, dict):  # a JSON reply: take its text-ish field
        for k in ("transcript", "text", "message", "result"):
            if isinstance(output.get(k), str):
                return output[k]
    return json.dumps(output)


async def transcribe(files: Any, att_id: str, *, refresh: bool = False) -> dict[str, Any]:
    """The transcript of one attached image: the cache, else a call to the transcriber.
    Raises ValidationError when no transcriber can run."""
    from agentline.assistant import ChatWorkspace, attachment_path, attachments_dir
    from agentline.registry import Limits, RunRequest

    if not refresh and (hit := cached(att_id)):
        return {**hit, "cached": True}
    src = attachment_path(att_id)
    if src is None:
        raise ValidationError(f"no attached image {att_id!r}")
    by = effective(files.config)
    if by is None:
        raise ValidationError(
            "the Chat's agent can't see images and no vision model is set up: pick one under"
            " Settings > Harnesses > Chat uses > Images (Claude Code, or a model with a key)"
        )
    if by == CLAUDE_CODE:
        ws_dir = attachments_dir().parent / "vision"
        ws_dir.mkdir(parents=True, exist_ok=True)
        staged = ws_dir / src.name  # Claude Code reads only inside its folder
        if not staged.exists():
            staged.write_bytes(src.read_bytes())
        res = ai.resolve("claude-code", None, files.config, presets=True)
        request = RunRequest(
            prompt=f"Open {staged} with your Read tool.\n\n{PROMPT}",
            inputs={},
            limits=Limits(allowed_tools=("read",), max_turns=3, timeout=300),
            workspace=ChatWorkspace(ws_dir),
            params=dict(res.params),
            node_id="vision",
        )
    else:
        res = ai.resolve("builtin", by, files.config)
        params = dict(res.params)
        params["images"] = [
            {"media_type": _media_type(src), "data": base64.b64encode(src.read_bytes()).decode()}
        ]
        params.setdefault("max_tokens", 2048)
        request = RunRequest(
            prompt=PROMPT,
            inputs={},
            limits=Limits(timeout=300),
            params=params,
            node_id="vision",
        )
    runner = files.registry.get_runner(res.runner)
    result = await runner.run(request)
    text = _text(result.output).strip()
    if not text:
        raise ValidationError(f"{by} returned an empty transcript")
    shown = "Claude Code" if by == CLAUDE_CODE else by
    return {**save(att_id, text, shown, cost=result.cost_usd), "cached": False}


__all__ = [
    "AUTO",
    "CLAUDE_CODE",
    "PROMPT",
    "cached",
    "choice",
    "effective",
    "forget",
    "options",
    "save",
    "set_choice",
    "status",
    "transcribe",
]
