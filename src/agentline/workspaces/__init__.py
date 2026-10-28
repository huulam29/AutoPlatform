"""Workspace providers for agent nodes. Each module registers itself in `register()`."""

from __future__ import annotations

from typing import Any

from agentline.config import Config
from agentline.registry import Registry, Workspace
from agentline.workspaces.container import ContainerProvider, ContainerWorkspace
from agentline.workspaces.images import propose
from agentline.workspaces.local import LocalProvider, LocalWorkspace


def register(registry: Registry, config: Config | None = None) -> None:
    """Add the built-in workspace providers: container, local."""
    registry.add_workspace(ContainerProvider(config))
    registry.add_workspace(LocalProvider(config))


async def finalize_output(ws: Workspace | None, output: Any) -> Any:
    """Merge the workspace's branch artifact into an agent's output (if it has one).

    A dict output keeps its keys at the top level (`{**finalize(), **output}`, the agent's keys
    win), so templates like `{{plan.summary}}` work whether or not the node had a workspace.
    Any other output (text, a list) goes under `agent`: `{**finalize(), "agent": output}`."""
    finalize = getattr(ws, "finalize", None)
    if finalize is None:
        return output
    branch = await finalize()
    if isinstance(output, dict):
        return {**branch, **output}
    return {**branch, "agent": output}


def image_proposal(ws: Workspace | None, missing_tools: list[str]) -> dict[str, Any]:
    """Draft an image change for missing tools (deterministic proposer by default)."""
    return propose(
        list(missing_tools),
        getattr(ws, "image_profile", None),
        list(getattr(ws, "markers", None) or ()),
    )


__all__ = [
    "ContainerProvider",
    "ContainerWorkspace",
    "LocalProvider",
    "LocalWorkspace",
    "finalize_output",
    "image_proposal",
    "register",
]
