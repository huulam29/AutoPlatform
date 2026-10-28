"""Built-in runners for agent nodes. Each module registers itself in `register()`."""

from __future__ import annotations

from agentline.config import Config
from agentline.registry import Registry
from agentline.runners.aider import AiderRunner
from agentline.runners.anthropic import AnthropicRunner
from agentline.runners.claude_code import ClaudeCodeRunner
from agentline.runners.codex import CodexRunner
from agentline.runners.common import RunnerError
from agentline.runners.function import FunctionRunner
from agentline.runners.hermes import HermesRunner
from agentline.runners.llm import LLMRunner
from agentline.runners.opencode import OpenCodeRunner
from agentline.runners.python import PythonRunner
from agentline.runners.shell import ShellRunner


def register(registry: Registry, config: Config | None = None) -> None:
    """Add the built-in runners: python, shell, anthropic, claude-code, codex, aider, opencode,
    hermes."""
    pricing = dict(config.raw.get("pricing", {})) if config else {}
    registry.add_runner(PythonRunner())
    registry.add_runner(ShellRunner())
    registry.add_runner(AnthropicRunner(pricing))
    registry.add_runner(LLMRunner(pricing))
    registry.add_runner(ClaudeCodeRunner())
    registry.add_runner(CodexRunner(pricing))
    registry.add_runner(AiderRunner())
    registry.add_runner(OpenCodeRunner(pricing))
    registry.add_runner(HermesRunner(pricing))


__all__ = [
    "AiderRunner",
    "AnthropicRunner",
    "ClaudeCodeRunner",
    "CodexRunner",
    "FunctionRunner",
    "HermesRunner",
    "LLMRunner",
    "OpenCodeRunner",
    "PythonRunner",
    "RunnerError",
    "ShellRunner",
    "register",
]
