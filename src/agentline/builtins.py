"""The default registry: every built-in runner, action, waiter and workspace provider."""

from __future__ import annotations

from agentline import actions, runners, workspaces
from agentline.config import Config
from agentline.registry import Registry


def default_registry(config: Config | None = None) -> Registry:
    reg = Registry()
    runners.register(reg, config)
    actions.register(reg, config)
    workspaces.register(reg, config)
    from agentline.chatlink import steps as chat_steps

    chat_steps.register(reg)
    return reg
