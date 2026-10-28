"""Runner `llm`: one direct model call, no agent harness and no tools.

Normally configured through `harness: builtin` + `model: <name>` (see `agentline.agents`), which
fills these `with:` params from the provider and model:
- `provider_type`: `anthropic` (Messages API) or `openai-compatible` (`POST /chat/completions`:
  OpenRouter, OpenAI, Ollama, vLLM, LM Studio, private servers).
- `base_url`, `api_key_env` (the env var holding the key; none for a keyless local server),
  `headers` (extra HTTP headers, e.g. OpenRouter's `HTTP-Referer`/`X-Title`).
- `model`, `max_tokens` (default 2048), `temperature`, `system`, `timeout` (s).
- `input_usd_per_mtok` / `output_usd_per_mtok`: prices; else `[pricing.<model>]`. Unknown
  price -> cost 0 with a warning, except `:free` OpenRouter models (`price_unknown_ok`).

Output: the reply parsed as JSON when it is JSON (``` fences stripped), else the text.
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
from agentline.runners.anthropic import AnthropicRunner, price_for
from agentline.runners.common import RunnerError, parse_json_text


class LLMRunner:
    name = "llm"

    def __init__(self, pricing: dict[str, Any] | None = None) -> None:
        self.pricing = dict(pricing or {})
        self._anthropic = AnthropicRunner(self.pricing)

    async def run(self, request: RunRequest) -> RunResult:
        p = request.params
        ptype = p.get("provider_type") or "openai-compatible"
        if ptype == "anthropic":
            return await self._anthropic.run(request)
        if ptype != "openai-compatible":
            raise PermanentError(f"llm runner: unknown provider_type {ptype!r}")
        if request.limits.budget_usd is not None and request.limits.budget_usd <= 0:
            raise BudgetExceeded(0.0, 0.0, "call")
        base = str(p.get("base_url") or "").rstrip("/")
        if not base:
            raise PermanentError("llm runner: no base_url for the openai-compatible provider")
        model = p.get("model")
        if not model:
            raise PermanentError("llm runner: no model (set model: on the node or its agent)")
        headers = {"content-type": "application/json", **dict(p.get("headers") or {})}
        key_env = p.get("api_key_env")
        if key_env:
            key = os.environ.get(str(key_env))
            if not key:
                raise PermanentError(f"llm runner: {key_env} is not set (add it in Settings)")
            headers["authorization"] = f"Bearer {key}"
        messages: list[dict[str, Any]] = []
        if p.get("system"):
            messages.append({"role": "system", "content": str(p["system"])})
        images = p.get("images") or []  # [{media_type, data (base64)}]: a vision call
        if images:
            parts: list[dict[str, Any]] = [{"type": "text", "text": request.prompt}]
            parts += [
                {"type": "image_url",
                 "image_url": {"url": f"data:{i['media_type']};base64,{i['data']}"}}
                for i in images
            ]  # fmt: skip
            messages.append({"role": "user", "content": parts})
        else:
            messages.append({"role": "user", "content": request.prompt})
        body: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "max_tokens": int(p.get("max_tokens") or 2048),
        }
        if p.get("temperature") is not None:
            body["temperature"] = p["temperature"]
        timeout = float(p.get("timeout") or request.limits.timeout or 300)
        resp = await asyncio.to_thread(
            post_json, f"{base}/chat/completions", body, headers, timeout
        )
        choices = resp.get("choices") or []
        if not choices:
            raise RunnerError(f"llm runner: no choices in the response: {str(resp)[:300]}")
        message = choices[0].get("message") or {}
        text = message.get("content") or ""
        if isinstance(text, list):  # some servers return content parts
            text = "".join(str(x.get("text", "")) for x in text if isinstance(x, dict))
        usage = resp.get("usage") or {}
        meta: dict[str, Any] = {
            "model": resp.get("model", model),
            "id": resp.get("id"),
            "finish_reason": choices[0].get("finish_reason"),
            "usage": usage,
        }
        price = price_for(str(model), p, self.pricing)
        cost = 0.0
        if price is not None:
            cost = (
                int(usage.get("prompt_tokens") or 0) * price[0]
                + int(usage.get("completion_tokens") or 0) * price[1]
            ) / 1_000_000
        elif isinstance(usage.get("cost"), (int, float)):  # OpenRouter reports the charge
            cost = float(usage["cost"])
        elif not p.get("price_unknown_ok"):
            meta["warnings"] = [f"no price known for model {model!r}; cost recorded as 0"]
        if choices[0].get("finish_reason") == "length":
            meta.setdefault("warnings", []).append("output was cut off at max_tokens")
        shown = body if not images else {**body, "messages": "(with images, not stored)"}
        transcript = json.dumps({"request": shown, "response": resp}, indent=2)
        ok, value = parse_json_text(str(text))
        return RunResult(
            output=value if ok else text, transcript=transcript, cost_usd=cost, meta=meta
        )


def post_json(
    url: str, body: dict[str, Any], headers: dict[str, str], timeout: float
) -> dict[str, Any]:
    req = urllib.request.Request(
        url, data=json.dumps(body).encode(), method="POST", headers=headers
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())  # type: ignore[no-any-return]
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")[:500]
        msg = f"model API {e.code} at {url}: {detail}"
        if e.code in (408, 409, 429) or e.code >= 500:
            raise RunnerError(msg) from None  # retryable (rate limits on free models too)
        raise PermanentError(msg) from None
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise RunnerError(f"model API unreachable at {url}: {e}") from None
