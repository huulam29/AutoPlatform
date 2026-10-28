"""Runner `python`: call your own function. `with: { function: "pkg.module:fn" }`.

The function gets the `RunRequest` (sync or async) and returns a `RunResult` or a plain
output. Sync functions run in a thread so they don't block the worker's event loop.
"""

from __future__ import annotations

import asyncio
import importlib
import inspect
from collections.abc import Callable
from typing import Any

from agentline.errors import PermanentError
from agentline.registry import RunRequest, RunResult


def load_function(ref: str) -> Callable[..., Any]:
    """`pkg.module:fn` (or `pkg.module:Class.method`) -> the callable."""
    module_name, sep, attr = ref.partition(":")
    if not sep or not module_name or not attr:
        raise PermanentError(f"python runner: function must look like 'pkg.module:fn', got {ref!r}")
    try:
        obj: Any = importlib.import_module(module_name)
    except ImportError as e:
        raise PermanentError(f"python runner: cannot import {module_name!r}: {e}") from None
    for part in attr.split("."):
        if not hasattr(obj, part):
            raise PermanentError(f"python runner: {module_name!r} has no attribute {attr!r}")
        obj = getattr(obj, part)
    if not callable(obj):
        raise PermanentError(f"python runner: {ref!r} is not callable")
    return obj  # type: ignore[no-any-return]


class PythonRunner:
    name = "python"

    async def run(self, request: RunRequest) -> RunResult:
        ref = request.params.get("function")
        if not isinstance(ref, str):
            raise PermanentError("python runner needs `with: { function: 'pkg.module:fn' }`")
        fn = load_function(ref)
        call = fn(request) if inspect.iscoroutinefunction(fn) else asyncio.to_thread(fn, request)

        async def invoke() -> Any:
            out = await call
            if inspect.isawaitable(out):  # a sync wrapper that returned a coroutine
                out = await out
            return out

        out = await asyncio.wait_for(invoke(), request.limits.timeout)
        return out if isinstance(out, RunResult) else RunResult(output=out)
