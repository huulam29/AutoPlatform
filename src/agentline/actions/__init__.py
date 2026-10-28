"""Built-in actions and waiters. Each module registers itself in `register()`.

| action              | effect strategy                                          |
|---------------------|----------------------------------------------------------|
| jira.get_ticket     | read-only, no guard                                      |
| jira.comment        | check (marker in the comment body)                       |
| github.open_pr      | push: key; open_pr: check + find-or-create by head branch |
| github.read_reviews | read-only, no guard                                      |
| shell.run           | none (unknown -> on_unknown); idempotent: true -> retry  |
| env.deploy          | deploy-class; check if the env has `check`, else none    |
| env.test            | no guard (safe to repeat)                                |

Clients are built from env vars when an action runs, so registering never needs tokens.
"""

from __future__ import annotations

from agentline.actions.env import EnvDeploy, EnvTest
from agentline.actions.github import MergePR, OpenPR, ReadReviews, ReviewWaiter
from agentline.actions.jira import Comment, GetTicket
from agentline.actions.shell import ShellRun
from agentline.config import Config
from agentline.registry import Registry


def register(registry: Registry, config: Config | None = None) -> None:
    """Add jira.*, github.*, shell.run, env.* and the github.review waiter."""
    registry.add_action(GetTicket())
    registry.add_action(Comment())
    registry.add_action(OpenPR(config=config))
    registry.add_action(ReadReviews())
    registry.add_action(MergePR())
    registry.add_action(ShellRun())
    registry.add_action(EnvDeploy(config=config))
    registry.add_action(EnvTest(config=config))
    registry.add_waiter(ReviewWaiter())


__all__ = [
    "Comment",
    "EnvDeploy",
    "EnvTest",
    "GetTicket",
    "MergePR",
    "OpenPR",
    "ReadReviews",
    "ReviewWaiter",
    "ShellRun",
    "register",
]
