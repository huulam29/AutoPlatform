"""Runner `anthropic`: one Messages API call over stdlib urllib, no SDK.

`with:` params: `model` (default `claude-sonnet-5-5`), `max_tokens` (4096), `system`,
`temperature`, `input_usd_per_mtok` / `output_usd_per_mtok` (prices; else
`[pricing.<model>] input = .., output = ..` in agentline.toml), `timeout` (s),
`api_key_env` (default `ANTHROPIC_API_KEY`), `base_url` (else `ANTHROPIC_BASE_URL`, else
https://api.anthropic.com).
Output: the text parsed as JSON when it is JSON (``` fences stripped), else the text.
Cost: from `usage` and the price table; unknown price -> cost 0 and a warning in `meta`.
"""

from __future__ import annotations

import asyncio
import json
import os
import urllib.error
import urllib.request
from typing import Any

from agentline.errors import BudgetExceeded, PermanentError
from agentline.registry import RunRequest, RunResult
from agentline.runners.common import RunnerError, parse_json_text

DEFAULT_MODEL = "claude-sonnet-5-5"
DEFAULT_BASE_URL = "https://api.anthropic.com"
API_VERSION = "2023-06-01"


def price_for(
    model: str, params: dict[str, Any], pricing: dict[str, Any]
) -> tuple[float, float] | None:
    """(input, output) USD per million tokens, from `with:` params or the pricing table."""
    if "input_usd_per_mtok" in params and "output_usd_per_mtok" in params:
        return float(params["input_usd_per_mtok"]), float(params["output_usd_per_mtok"])
    row = pricing.get(model)
    if isinstance(row, dict) and "input" in row and "output" in row:
        return float(row["input"]), float(row["output"])
    return None


def cost_of(usage: dict[str, Any], price: tuple[float, float]) -> float:
    # Cache writes/reads are billed as input here; a finer table can be added later.
    tokens_in = sum(
        int(usage.get(k) or 0)
        for k in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")
    )
    tokens_out = int(usage.get("output_tokens") or 0)
    return (tokens_in * price[0] + tokens_out * price[1]) / 1_000_000


def _content(prompt: str, images: Any) -> Any:
    """The user turn: the prompt, plus image blocks for a vision call."""
    if not images:
        return prompt
    blocks: list[dict[str, Any]] = [
        {"type": "image",
         "source": {"type": "base64", "media_type": i["media_type"], "data": i["data"]}}
        for i in images
    ]  # fmt: skip
    return [*blocks, {"type": "text", "text": prompt}]


class AnthropicRunner:
    name = "anthropic"

    def __init__(self, pricing: dict[str, Any] | None = None) -> None:
        self.pricing = dict(pricing or {})

    async def run(self, request: RunRequest) -> RunResult:
        if request.limits.budget_usd is not None and request.limits.budget_usd <= 0:
            raise BudgetExceeded(0.0, 0.0, "call")
        p = request.params
        key_env = str(p.get("api_key_env") or "ANTHROPIC_API_KEY")
        key = os.environ.get(key_env)
        if not key:
            raise PermanentError(f"anthropic runner: {key_env} is not set")
        model = str(p.get("model") or DEFAULT_MODEL)
        body: dict[str, Any] = {
            "model": model,
            "max_tokens": int(p.get("max_tokens", 4096)),
            "messages": [{"role": "user", "content": _content(request.prompt, p.get("images"))}],
        }
        for opt in ("system", "temperature", "stop_sequences"):
            if opt in p:
                body[opt] = p[opt]
        base = (
            p.get("base_url") or os.environ.get("ANTHROPIC_BASE_URL") or DEFAULT_BASE_URL
        ).rstrip("/")
        timeout = float(p.get("timeout") or request.limits.timeout or 600)
        resp = await asyncio.to_thread(_post, f"{base}/v1/messages", body, key, timeout)

        text = "".join(
            b.get("text", "") for b in resp.get("content", []) if b.get("type") == "text"
        )
        usage = resp.get("usage") or {}
        meta: dict[str, Any] = {
            "model": resp.get("model", model),
            "id": resp.get("id"),
            "stop_reason": resp.get("stop_reason"),
            "usage": usage,
        }
        price = price_for(model, p, self.pricing)
        if price is None:
            cost = 0.0
            meta["warnings"] = [f"no price known for model {model!r}; cost recorded as 0"]
        else:
            cost = cost_of(usage, price)
        shown = body if not p.get("images") else {**body, "messages": "(with images, not stored)"}
        transcript = json.dumps({"request": shown, "response": resp}, indent=2)
        if resp.get("stop_reason") == "max_tokens":
            meta.setdefault("warnings", []).append("output was cut off at max_tokens")
        ok, value = parse_json_text(text)
        if request.limits.budget_usd is not None and cost > request.limits.budget_usd:
            meta["budget_exceeded"] = True  # the engine's spend() turns this into needs-review
        return RunResult(
            output=value if ok else text, transcript=transcript, cost_usd=cost, meta=meta
        )


def _post(url: str, body: dict[str, Any], key: str, timeout: float) -> dict[str, Any]:
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode(),
        method="POST",
        headers={
            "content-type": "application/json",
            "x-api-key": key,
            "anthropic-version": API_VERSION,
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())  # type: ignore[no-any-return]
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")[:500]
        msg = f"anthropic API {e.code}: {detail}"
        if e.code in (408, 409, 429) or e.code >= 500:
            raise RunnerError(msg) from None  # retryable
        raise PermanentError(msg) from None
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise RunnerError(f"anthropic API unreachable: {e}") from None
