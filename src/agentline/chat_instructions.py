"""The Chat's general instructions: what every Chat turn is told, on top of agentline's own
guide. One file per data folder, `$AGENTLINE_HOME/chat-instructions.md`, written with the
defaults on first use and editable afterwards (Settings > Chat apps, the API, or a Chat
`chat_instructions` action). A rule added to the defaults later is appended once to an existing
file (UPGRADES, recorded in `chat-instructions.upgrades.json`), so deleting it sticks.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path

from agentline.errors import ValidationError

MAX_BYTES = 20_000
APP_PROBLEMS = (
    "- If you encounter an agentline app/engine problem, report it to GitHub using the"
    " failure-triage procedure (a `report_issue` action for its triage in APP REPORTS), then"
    " remove it from the Problems page (delete its crash report with an `incident` action)."
)
FRESH_DESIGN = (
    "- When asked to build a new pipeline (fresh, or from an attached image), design it only"
    " from the requirements gathered in this session. Do not copy the structure, nodes or steps"
    " of existing pipelines or REFERENCE templates; use them only to check syntax. Do not add"
    " steps (deploy, test, gates, notifications) the user has not asked for."
)
NO_AI_FIRST = (
    "- When building nodes, prioritize tool or script nodes without AI help if they are"
    " sufficient for the task the node is performing. If dynamic decision-making is required,"
    " select the most appropriate AI harness and models for the task, and avoid using AI models"
    " that are unnecessary for the task."
)
DEFAULT = f"""\
- When the user asks to build a new pipeline, invoke the `interview-me` skill first.
{FRESH_DESIGN}
{APP_PROBLEMS}
{NO_AI_FIRST}
"""
# rules added to the defaults after a file was written: (id, line), appended once
UPGRADES = (("app-problems", APP_PROBLEMS), ("fresh-design", FRESH_DESIGN))
_SKILL_REF = re.compile(r"`([a-z0-9][a-z0-9_.-]*)`")


def path() -> Path:
    home = Path(os.environ.get("AGENTLINE_HOME") or Path.home() / ".agentline")
    return home / "chat-instructions.md"


def _upgrades_path() -> Path:
    return path().with_name("chat-instructions.upgrades.json")


def _applied() -> list[str]:
    try:
        data = json.loads(_upgrades_path().read_text())
        return [str(x) for x in data] if isinstance(data, list) else []
    except (OSError, ValueError):
        return []


def read() -> str:
    """The instructions; the defaults are written on first use, and rules added to the
    defaults since are appended once."""
    p = path()
    done = _applied()
    if not p.exists():
        write(DEFAULT)
        done = []
    todo = [(uid, line) for uid, line in UPGRADES if uid not in done]
    if todo:
        text = p.read_text()
        new = [line for _, line in todo if line not in text.splitlines()]
        if new:  # with the general rules, before any section (the lessons)
            lines = text.rstrip("\n").split("\n")
            at = next((i for i, ln in enumerate(lines) if ln.startswith("## ")), len(lines))
            while at > 0 and not lines[at - 1].strip():
                at -= 1
            write("\n".join([*lines[:at], *new, *lines[at:]]) + "\n")
        _upgrades_path().write_text(json.dumps(done + [uid for uid, _ in todo]))
    return p.read_text()


def write(text: str) -> str:
    if len(text.encode()) > MAX_BYTES:
        raise ValidationError(f"the Chat instructions are longer than {MAX_BYTES // 1000} KB")
    p = path()
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=p.parent, prefix=".chat-instructions.", suffix=".tmp")
    with os.fdopen(fd, "w") as f:
        f.write(text if text.endswith("\n") or not text else text + "\n")
    os.replace(tmp, p)
    return p.read_text()


LESSONS = "## Lessons from failed runs"


def add_lesson(text: str) -> str:
    """Append one lesson line (from failure triage) under LESSONS; a line already there is
    not added twice."""
    line = " ".join(text.split())
    if not line:
        raise ValidationError("the lesson is empty")
    if len(line) > 300:
        raise ValidationError("a lesson is one line of at most 300 characters")
    current = read()
    bullet = f"- {line.removeprefix('- ')}"
    if bullet in current.splitlines():
        return current
    lines = current.rstrip("\n").split("\n")
    if LESSONS not in lines:
        return write("\n".join([*lines, "", LESSONS, bullet]) + "\n")
    at = lines.index(LESSONS) + 1  # after the section's last bullet
    while at < len(lines) and lines[at].startswith("- "):
        at += 1
    return write("\n".join([*lines[:at], bullet, *lines[at:]]) + "\n")


def reset() -> str:
    return write(DEFAULT)


def skill_names(text: str) -> list[str]:
    """Skills the instructions name in backticks (`interview-me`): agents that can't load
    skill folders get these skills' full text in the prompt."""
    return list(dict.fromkeys(_SKILL_REF.findall(text)))
