"""A pipeline's lesson library: what run reviews learned about its instructions (phase 38).

One Markdown file next to the pipeline, so history, export and bundles carry it:
`<name>/pipeline.yaml` -> `<name>/lessons.md`; a pipeline file at the top of the folder,
`p.yaml` -> `p.lessons.md`. Later reviews of the pipeline read it, and so does the Chat. It
never reaches a node's prompt: prompts change only through edit cards the user applies.

    ### L3 · write_code · prompt
    - cue: when a reviewer checks a repo convention the writer is never told about
    - lesson: state the convention in the writer's prompt
    - evidence: run r_8f2: 2 review loops on a missing CHANGELOG line; probe better
    - confidence: medium
    - status: adopted
    - review: v0123456789ab
    - added: 2026-10-21

Statuses: proposed (recorded, its edit not applied), adopted, helped, hurt, no effect,
dismissed. Lines in an entry that aren't `- key: value` are kept as its notes.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from agentline.errors import ValidationError

STATUSES = ("proposed", "adopted", "helped", "hurt", "no effect", "dismissed")
CONFIDENCE = ("high", "medium", "low")
COMPONENTS = (
    "prompt",
    "output_schema",
    "skills",
    "mcp",
    "model",
    "harness",
    "allowed_tools",
    "max_turns",
    "max_loops",
    "router",
    "pipeline",
)
FIELDS = ("cue", "lesson", "evidence", "confidence", "status", "review", "runs", "added")
MAX_TEXT = 400
_HEAD = re.compile(r"^###\s+L(\d+)\s*·\s*([^·]+?)\s*·\s*(.+?)\s*$")
_FIELD = re.compile(r"^-\s+([a-z ]+):\s*(.*)$")
PREAMBLE = (
    "Learned from agentline's run reviews. Reviews of this pipeline read this file before"
    " proposing changes; nothing here is added to a node's prompt. Edit or delete freely."
)


@dataclass
class Lesson:
    id: int
    node: str
    component: str
    cue: str = ""
    lesson: str = ""
    evidence: str = ""
    confidence: str = "low"
    status: str = "proposed"
    review: str = ""
    runs: str = ""
    added: str = ""
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "node": self.node,
            "component": self.component,
            **{k: getattr(self, k) for k in FIELDS},
            "notes": list(self.notes),
        }


def path_for(pipeline_file: Path) -> Path:
    if pipeline_file.name == "pipeline.yaml":
        return pipeline_file.with_name("lessons.md")
    return pipeline_file.with_name(f"{pipeline_file.stem}.lessons.md")


def parse(text: str) -> tuple[list[str], list[Lesson]]:
    """(the lines before the first entry, the entries)."""
    head: list[str] = []
    out: list[Lesson] = []
    for line in text.splitlines():
        m = _HEAD.match(line)
        if m:
            out.append(Lesson(int(m.group(1)), m.group(2).strip(), m.group(3).strip()))
            continue
        if not out:
            head.append(line)
            continue
        f = _FIELD.match(line.strip())
        cur = out[-1]
        if f and f.group(1).strip() in FIELDS:
            setattr(cur, f.group(1).strip(), f.group(2).strip())
        elif line.strip():
            cur.notes.append(line.rstrip())
    return head, out


def render(title: str, head: list[str], items: list[Lesson]) -> str:
    lines = list(head) if any(x.strip() for x in head) else [f"# Lessons: {title}", "", PREAMBLE]
    while lines and not lines[-1].strip():
        lines.pop()
    for x in items:
        lines += ["", f"### L{x.id} · {x.node} · {x.component}"]
        lines += [f"- {k}: {getattr(x, k)}" for k in FIELDS if getattr(x, k)]
        lines += x.notes
    return "\n".join(lines) + "\n"


def read(pipeline_file: Path) -> list[Lesson]:
    p = path_for(pipeline_file)
    try:
        return parse(p.read_text())[1]
    except OSError:
        return []


def text_for_prompt(pipeline_file: Path, limit: int = 8000) -> str:
    """The library as the reviewer and the Chat read it ('' when there is none)."""
    p = path_for(pipeline_file)
    try:
        return p.read_text()[:limit]
    except OSError:
        return ""


def _one_line(v: Any, what: str) -> str:
    s = " ".join(str(v or "").split())
    if len(s) > MAX_TEXT:
        raise ValidationError(f"the lesson's {what} is too long (at most {MAX_TEXT} characters)")
    return s


def check(a: dict[str, Any]) -> list[str]:
    """Issues with a `pipeline_lesson` proposal (cleans its text fields in place)."""
    issues = []
    try:
        for k in ("cue", "lesson", "evidence"):
            a[k] = _one_line(a.get(k), k)
    except ValidationError as e:
        return [str(e)]
    if not a["cue"] or not a["lesson"]:
        issues.append("a lesson needs a cue (the situation) and the lesson (what to change)")
    if not str(a.get("pipeline") or "").strip():
        issues.append("which pipeline?")
    if not str(a.get("node") or "").strip():
        issues.append("which node does the lesson concern?")
    comp = str(a.get("component") or "prompt")
    if comp not in COMPONENTS:
        issues.append(f"component must be one of {', '.join(COMPONENTS)}")
    a["component"] = comp
    word = re.match(r"\s*([a-z]+)", str(a.get("confidence") or "").lower())
    a["confidence"] = word.group(1) if word and word.group(1) in CONFIDENCE else "low"
    return issues


def add(pipeline_file: Path, title: str, a: dict[str, Any]) -> Lesson:
    """Append a lesson (a `pipeline_lesson` card applied). The same cue + lesson for the same
    node isn't added twice: the existing entry is returned."""
    issues = check(a)
    if issues:
        raise ValidationError("; ".join(issues))
    p = path_for(pipeline_file)
    head, items = parse(p.read_text()) if p.exists() else ([], [])
    for x in items:
        if (x.node, x.cue, x.lesson) == (str(a["node"]), a["cue"], a["lesson"]):
            return x
    new = Lesson(
        id=max((x.id for x in items), default=0) + 1,
        node=str(a["node"]).strip(),
        component=a["component"],
        cue=a["cue"],
        lesson=a["lesson"],
        evidence=a["evidence"],
        confidence=a["confidence"],
        status="adopted" if a.get("adopted") else "proposed",
        review=str(a.get("review") or ""),
        runs=_one_line(a.get("runs"), "runs"),
        added=time.strftime("%Y-%m-%d"),
    )
    items.append(new)
    _write(p, render(title, head, items))
    return new


def set_status(
    pipeline_file: Path, title: str, lesson_id: int, status: str, note: str = ""
) -> Lesson:
    if status not in STATUSES:
        raise ValidationError(f"status must be one of {', '.join(STATUSES)}")
    p = path_for(pipeline_file)
    head, items = parse(p.read_text()) if p.exists() else ([], [])
    hit = next((x for x in items if x.id == lesson_id), None)
    if hit is None:
        raise KeyError(lesson_id)
    hit.status = status
    if note:
        hit.notes.append(f"  ({time.strftime('%Y-%m-%d')}: {_one_line(note, 'note')})")
    _write(p, render(title, head, items))
    return hit


def for_review(pipeline_file: Path, review_id: str) -> list[Lesson]:
    return [x for x in read(pipeline_file) if x.review == review_id]


def _write(p: Path, text: str) -> None:
    from agentline.api.editor import _write as write

    write(p, text)
