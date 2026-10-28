"""A pipeline's start point: which node takes a run's input, and how.

    start:
      node: intake          # default: the graph's start node
      input: json | text    # default json; a `type: chat` start node is always chat
      template: { task: "" }  # json: what the Start run popup is pre-filled with

- json: the payload is the JSON you give; with no `template`, the template lists the payload
  fields the pipeline's templates read (`{{payload.task}}` -> `{"task": ""}`).
- text: the payload is `{"text": "..."}`; an agent start node whose prompt doesn't read
  `{{payload.text}}` gets it appended as an `## Input` section.
- chat: the payload is `{"text": <first message>}` and the start node is a gatekeeper you
  talk to (agentline.runchat).
"""

from __future__ import annotations

from typing import Any

from agentline.pipeline.spec import PipelineSpec

TEXT_SECTION = "\n\n## Input\n{text}\n"


def start_node(spec: PipelineSpec) -> str | None:
    """`start.node`, else the first node without an incoming (non-loop) edge."""
    if spec.start.node:
        return spec.start.node
    from agentline.pipeline.validate import skeleton

    starts = skeleton(spec).start_nodes()
    return starts[0] if starts else None


def input_kind(spec: PipelineSpec) -> str:
    node = spec.nodes.get(start_node(spec) or "")
    if node is not None and node.type == "chat":
        return "chat"
    return spec.start.input or "json"


def payload_fields(spec: PipelineSpec) -> list[str]:
    from agentline.dryrun import referenced_paths

    return sorted(referenced_paths(spec).get("payload", set()))


def default_template(spec: PipelineSpec) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for path in payload_fields(spec):
        cur = out
        *heads, last = path.split(".")
        for h in heads:
            nxt = cur.get(h)
            if not isinstance(nxt, dict):
                nxt = cur[h] = {}
            cur = nxt
        cur.setdefault(last, "")
    return out


def start_info(spec: PipelineSpec) -> dict[str, Any]:
    """What the Start run popup (and `/run` in chat apps) needs to know."""
    kind = input_kind(spec)
    template = None
    if kind == "json":
        template = (
            spec.start.template if spec.start.template is not None else default_template(spec)
        )
    return {
        "node": start_node(spec),
        "input": kind,
        "template": template,
        "fields": payload_fields(spec),
    }


def payload_for(spec: PipelineSpec, *, text: str | None = None, payload: Any = None) -> Any:
    """The run's payload from what the user gave: text for text/chat pipelines, JSON else.
    Text given to a json pipeline is kept as {"text": ...} too (a chat app can only send text)."""
    if text is not None:
        return {"text": text}
    return {} if payload is None else payload
