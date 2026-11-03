"""Parse pipeline YAML into typed specs, collecting every problem with its line and column."""

from __future__ import annotations

import itertools
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap, CommentedSeq
from ruamel.yaml.error import YAMLError

from agentline.errors import ValidationError
from agentline.registry import EffectPolicy, WorkspaceSpec

NODE_TYPES = ("agent", "action", "router", "gate", "wait", "chat")
COMMON_FIELDS = {"type", "in", "out", "max_attempts", "budget_usd", "description"}
TIMEOUT_TYPES = ("agent", "action")  # what waits for a person or an event has no timeout
TYPE_FIELDS: dict[str, set[str]] = {
    "agent": {
        "harness",
        "agent",  # the pre-phase-35 name of `harness`
        "runner",
        "prompt",
        "output_schema",
        "allowed_tools",
        "workspace",
        "max_turns",
        "with",
        "model",
        "skills",
        "mcp",
        "for_each",
        "max_parallel",
        "timeout",
    },
    "action": {"uses", "with", "effect", "for_each", "max_parallel", "timeout"},
    "router": {"switch", "mode", "prompt", "model"},
    "gate": {"prompt", "approvers"},
    "wait": {"for", "with", "poll_every"},
    # the chat start node (gatekeeper): a harness you talk to, on the host (no workspace)
    "chat": {
        "harness",
        "agent",
        "model",
        "prompt",
        "output_schema",
        "allowed_tools",
        "max_turns",
        "skills",
        "mcp",
    },
}
REQUIRED: dict[str, tuple[str, ...]] = {
    "agent": ("prompt",),
    "action": ("uses",),
    "router": (),  # switch (mode: switch) or prompt (mode: llm); checked in _node
    "gate": ("prompt",),
    "wait": ("for",),
    "chat": ("prompt",),
}
TOP_FIELDS = {
    "version",
    "name",
    "description",
    "trigger",
    "start",
    "budget_usd",
    "defaults",
    "nodes",
    "edges",
}
TRIGGER_TYPES = ("manual", "webhook", "poll", "schedule")
START_FIELDS = {"node", "input", "template"}
START_INPUTS = ("json", "text")  # a chat start node takes "chat"
EDGE_FIELDS = {"from", "to", "when", "max_loops", "on_exhausted"}
DEFAULTS_FIELDS = {"harness", "agent", "model", "max_turns"}
OLD_FIELDS = {"agent"}  # read as `harness` (its name before phase 35); never offered
ROUTER_MODES = ("switch", "llm")
SUPPORTED_VERSIONS = (1,)
_DURATION = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*(ms|s|m|h)?\s*$")
_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]*$")
_PATHLIKE = re.compile(r"^[\w./-]+\.(md|txt|tmpl|prompt|j2)$")


@dataclass(frozen=True)
class Issue:
    path: str
    message: str
    line: int | None = None
    col: int | None = None
    hint: str | None = None
    file: str | None = None

    def __str__(self) -> str:
        where = f"{self.file or '<pipeline>'}:{self.line or '?'}:{self.col or '?'}"
        text = f"{where}: {self.path}: {self.message}"
        return f"{text} (hint: {self.hint})" if self.hint else text

    def as_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "message": self.message,
            "line": self.line,
            "col": self.col,
            "hint": self.hint,
        }


class SpecError(ValidationError):
    def __init__(self, issues: list[Issue]) -> None:
        self.issues = issues
        super().__init__("\n".join(str(i) for i in issues))


@dataclass
class TriggerSpec:
    type: str = "manual"
    source: str | None = None
    event: str | None = None
    filter: dict[str, Any] = field(default_factory=dict)
    schedule: str | None = None  # cron, for type: schedule (checked by the validator)
    timezone: str | None = None  # IANA name for the schedule; default UTC
    catchup: str = "latest"  # after downtime: latest | all | none (see agentline.schedules)
    every: float = 300.0  # poll triggers: seconds between polls (agentline.sources.jira_poll)
    line: int | None = None  # line of `schedule:` (else of `trigger:`)


@dataclass
class StartSpec:
    """`start:` — the node that takes a run's input, and how (see agentline.pipeline.start)."""

    node: str | None = None  # default: the graph's start node
    input: str | None = None  # json | text; a chat node is always "chat"
    template: dict[str, Any] | None = None  # json: what the Start run popup is pre-filled with
    line: int | None = None


@dataclass
class NodeSpec:
    id: str
    type: str
    inputs: tuple[str, ...] = ()
    out: str | None = None
    max_attempts: int = 3
    timeout: float | None = None
    budget_usd: float | None = None
    description: str = ""
    # agent
    runner: str | None = None
    harness: str | None = None  # a configured harness (Settings); else defaults / claude-code
    prompt: str | None = None  # template text (file contents if a path was given)
    prompt_file: str | None = None
    output_schema: dict[str, Any] | None = None
    allowed_tools: tuple[str, ...] = ("read",)
    workspace: WorkspaceSpec | None = None
    max_turns: int | None = None
    model: str | None = None
    skills: tuple[str, ...] = ()  # agent skills by name (agentline.skills)
    mcp: tuple[str, ...] = ()  # MCP servers this node may use (agentline.mcp)
    # agent / action: one task per item of a list (`plan.steps`), `max_parallel` at a time
    for_each: str | None = None
    max_parallel: int = 4
    # agent / action / wait
    params: dict[str, Any] = field(default_factory=dict)
    # action
    uses: str | None = None
    effect: EffectPolicy = field(default_factory=EffectPolicy)
    # router
    switch: str | None = None
    router_mode: str = "switch"  # switch | llm
    # gate
    approvers: tuple[str, ...] = ()
    # wait
    wait_for: str | None = None
    poll_every: float | None = None
    line: int | None = None


@dataclass
class EdgeSpec:
    src: str
    dst: str
    when: str | None = None
    max_loops: int | None = None
    on_exhausted: str = "needs-review"
    line: int | None = None


@dataclass
class DefaultsSpec:
    """`defaults:` — what agent nodes (and LLM routers) use unless they say otherwise."""

    harness: str | None = None
    model: str | None = None
    max_turns: int | None = None
    line: int | None = None


@dataclass
class PipelineSpec:
    name: str
    version: int = 1
    description: str = ""
    trigger: TriggerSpec = field(default_factory=TriggerSpec)
    start: StartSpec = field(default_factory=StartSpec)
    defaults: DefaultsSpec = field(default_factory=DefaultsSpec)
    budget_usd: float | None = None
    nodes: dict[str, NodeSpec] = field(default_factory=dict)
    edges: list[EdgeSpec] = field(default_factory=list)
    path: Path | None = None
    base_dir: Path = field(default_factory=Path.cwd)
    source: str = ""


def parse_duration(value: Any) -> float:
    if isinstance(value, bool):
        raise ValueError("expected a duration")
    if isinstance(value, (int, float)):
        return float(value)
    m = _DURATION.match(str(value))
    if not m:
        raise ValueError(f"bad duration {value!r} (use seconds or e.g. 30s, 10m, 2h)")
    n, unit = float(m.group(1)), m.group(2) or "s"
    return n * {"ms": 0.001, "s": 1, "m": 60, "h": 3600}[unit]


class _Parser:
    def __init__(self, source: str, path: Path | None, base_dir: Path) -> None:
        self.source = source
        self.path = path
        self.base_dir = base_dir
        self.issues: list[Issue] = []

    def issue(
        self, path: str, message: str, pos: tuple[int, int] | None = None, hint: str | None = None
    ) -> None:
        line, col = (pos[0] + 1, pos[1] + 1) if pos else (None, None)
        file = str(self.path) if self.path else None
        self.issues.append(Issue(path, message, line, col, hint, file))

    @staticmethod
    def key_pos(m: Any, key: Any) -> tuple[int, int] | None:
        if isinstance(m, CommentedMap):
            try:
                return tuple(m.lc.key(key))  # type: ignore[return-value]
            except (KeyError, TypeError):
                return (m.lc.line, m.lc.col)
        return None

    @staticmethod
    def item_pos(s: Any, i: int) -> tuple[int, int] | None:
        if isinstance(s, CommentedSeq):
            try:
                return tuple(s.lc.item(i))  # type: ignore[return-value]
            except (KeyError, TypeError, IndexError):
                return None
        return None

    def parse(self) -> PipelineSpec:
        try:
            data = YAML(typ="rt").load(self.source)
        except YAMLError as e:
            mark = getattr(e, "problem_mark", None)
            pos = (mark.line, mark.column) if mark else None
            self.issue("$", f"invalid YAML: {getattr(e, 'problem', e)}", pos)
            raise SpecError(self.issues) from None
        if not isinstance(data, dict):
            self.issue("$", "pipeline must be a mapping with name, nodes and edges")
            raise SpecError(self.issues)

        for k in data:
            if k not in TOP_FIELDS:
                self.issue(
                    str(k),
                    "unknown top-level field",
                    self.key_pos(data, k),
                    _suggest(k, TOP_FIELDS),
                )
        version = data.get("version", 1)
        if version not in SUPPORTED_VERSIONS:
            self.issue("version", f"unsupported version {version!r}", self.key_pos(data, "version"))
        name = data.get("name")
        if not isinstance(name, str) or not name:
            self.issue("name", "pipeline needs a non-empty 'name'", self.key_pos(data, "name"))
            name = "unnamed"
        spec = PipelineSpec(
            name=name,
            version=version if isinstance(version, int) else 1,
            description=str(data.get("description", "")),
            budget_usd=self._number(data, "budget_usd", "budget_usd"),
            path=self.path,
            base_dir=self.base_dir,
            source=self.source,
        )
        spec.trigger = self._trigger(data.get("trigger"), data)
        spec.defaults = self._defaults(data.get("defaults"), data)
        spec.start = self._start(data.get("start"), data)
        nodes = data.get("nodes")
        if not isinstance(nodes, dict) or not nodes:
            self.issue("nodes", "pipeline needs a 'nodes' mapping", self.key_pos(data, "nodes"))
        else:
            for node_id, raw in nodes.items():
                if (n := self._node(str(node_id), raw, nodes)) is not None:
                    spec.nodes[n.id] = n
        edges = data.get("edges", [])
        if not isinstance(edges, list):
            self.issue("edges", "edges must be a list", self.key_pos(data, "edges"))
        else:
            for i, raw in enumerate(edges):
                spec.edges.extend(self._edges(i, raw, edges))
        if self.issues:
            raise SpecError(self.issues)
        return spec

    def _defaults(self, raw: Any, parent: Any) -> DefaultsSpec:
        pos = self.key_pos(parent, "defaults")
        if raw is None:
            return DefaultsSpec()
        if not isinstance(raw, dict):
            self.issue("defaults", "defaults must be a mapping (harness, model, max_turns)", pos)
            return DefaultsSpec()
        for k in raw:
            if k not in DEFAULTS_FIELDS:
                self.issue(
                    f"defaults.{k}",
                    "unknown field",
                    self.key_pos(raw, k),
                    _suggest(k, DEFAULTS_FIELDS),
                )
        d = DefaultsSpec(line=pos[0] + 1 if pos else None)
        if "harness" in raw and "agent" in raw:
            self.issue("defaults.agent", "'agent' is the old name of 'harness': keep one",
                       self.key_pos(raw, "agent"))  # fmt: skip
        for k, attr in (("agent", "harness"), ("harness", "harness"), ("model", "model")):
            v = raw.get(k)
            if v is not None and (not isinstance(v, str) or not v):
                self.issue(f"defaults.{k}", "must be a name", self.key_pos(raw, k))
            elif v is not None:
                setattr(d, attr, v)
        v = raw.get("max_turns")
        if v is not None:
            if isinstance(v, bool) or not isinstance(v, int) or v < 1:
                self.issue(
                    "defaults.max_turns", "must be an integer >= 1", self.key_pos(raw, "max_turns")
                )
            else:
                d.max_turns = v
        return d

    def _start(self, raw: Any, parent: Any) -> StartSpec:
        pos = self.key_pos(parent, "start")
        if raw is None:
            return StartSpec()
        if not isinstance(raw, dict):
            self.issue("start", "start must be a mapping (node, input, template)", pos)
            return StartSpec()
        for k in raw:
            if k not in START_FIELDS:
                self.issue(f"start.{k}", "unknown field", self.key_pos(raw, k),
                           _suggest(k, START_FIELDS))  # fmt: skip
        s = StartSpec(line=pos[0] + 1 if pos else None)
        node = raw.get("node")
        if node is not None and (not isinstance(node, str) or not node):
            self.issue("start.node", "must be a node id", self.key_pos(raw, "node"))
        elif node is not None:
            s.node = node
        kind = raw.get("input")
        if kind is not None and kind not in (*START_INPUTS, "chat"):
            self.issue("start.input", f"unknown input {kind!r}", self.key_pos(raw, "input"),
                       "use json or text (a chat start node is always chat)")  # fmt: skip
        elif kind is not None:
            s.input = str(kind)
        if "template" in raw:
            t = raw["template"]
            if not isinstance(t, dict):
                self.issue("start.template", "template must be a mapping (the JSON to fill in)",
                           self.key_pos(raw, "template"))  # fmt: skip
            else:
                s.template = _plain(t)
        return s

    def _number(self, m: Any, key: str, path: str) -> float | None:
        if key not in m or m[key] is None:
            return None
        v = m[key]
        if isinstance(v, bool) or not isinstance(v, (int, float)) or v < 0:
            self.issue(path, f"expected a non-negative number, got {v!r}", self.key_pos(m, key))
            return None
        return float(v)

    def _trigger(self, raw: Any, parent: Any) -> TriggerSpec:
        if raw is None:
            return TriggerSpec()
        if not isinstance(raw, dict):
            self.issue("trigger", "trigger must be a mapping", self.key_pos(parent, "trigger"))
            return TriggerSpec()
        known = {"type", "source", "event", "filter", "schedule", "timezone", "catchup", "every"}
        for k in raw:
            if k not in known:
                self.issue(
                    f"trigger.{k}", "unknown field", self.key_pos(raw, k), _suggest(k, known)
                )
        t = TriggerSpec(
            type=str(raw.get("type", "manual")),
            source=raw.get("source"),
            event=raw.get("event"),
            filter=dict(raw.get("filter") or {}),
            schedule=raw.get("schedule"),
            timezone=raw.get("timezone"),
            catchup=str(raw.get("catchup") or "latest"),
        )
        pos = (
            self.key_pos(raw, "schedule") if "schedule" in raw else self.key_pos(parent, "trigger")
        )
        t.line = pos[0] + 1 if pos else None
        if t.schedule is not None and not isinstance(t.schedule, str):
            self.issue("trigger.schedule", "schedule must be a string", pos)
            t.schedule = None
        if t.catchup not in ("latest", "all", "none"):
            self.issue(
                "trigger.catchup",
                f"unknown catchup policy {t.catchup!r}",
                self.key_pos(raw, "catchup"),
                "use latest, all or none",
            )
        if t.timezone is not None and not isinstance(t.timezone, str):
            self.issue(
                "trigger.timezone", "timezone must be a string", self.key_pos(raw, "timezone")
            )
            t.timezone = None
        for k in ("schedule", "timezone", "catchup"):
            if k in raw and t.type != "schedule":
                self.issue(
                    f"trigger.{k}",
                    f"'{k}' only applies to schedule triggers",
                    self.key_pos(raw, k),
                    "set type: schedule or remove it",
                )
        if "every" in raw:
            if t.type != "poll":
                self.issue(
                    "trigger.every",
                    "'every' only applies to poll triggers",
                    self.key_pos(raw, "every"),
                )
            else:
                try:
                    t.every = parse_duration(raw["every"])
                    if t.every < 30:
                        raise ValueError("poll at most every 30s (APIs rate-limit)")
                except ValueError as e:
                    self.issue("trigger.every", str(e), self.key_pos(raw, "every"))
        if t.type not in TRIGGER_TYPES:
            self.issue(
                "trigger.type",
                f"unknown trigger type {t.type!r}",
                self.key_pos(raw, "type"),
                "use manual, webhook, poll or schedule",
            )
        if t.type == "poll" and (t.source, t.event) != ("jira", "issue_assigned"):
            self.issue(
                "trigger",
                "poll triggers support source: jira, event: issue_assigned",
                self.key_pos(parent, "trigger"),
            )
        if t.type == "webhook" and not (t.source and t.event):
            self.issue(
                "trigger",
                "webhook triggers need 'source' and 'event'",
                self.key_pos(parent, "trigger"),
            )
        if t.type == "schedule" and not t.schedule:
            self.issue(
                "trigger",
                "schedule triggers need 'schedule' (cron)",
                self.key_pos(parent, "trigger"),
            )
        return t

    def _node(self, node_id: str, raw: Any, parent: Any) -> NodeSpec | None:
        p = f"nodes.{node_id}"
        pos = self.key_pos(parent, node_id)
        if not _IDENT.match(node_id):
            self.issue(p, "node ids must be letters, digits, _ or -", pos)
        if not isinstance(raw, dict):
            self.issue(p, "node must be a mapping with at least 'type'", pos)
            return None
        ntype = raw.get("type")
        if ntype not in NODE_TYPES:
            self.issue(
                f"{p}.type",
                f"unknown node type {ntype!r}",
                self.key_pos(raw, "type") or pos,
                f"one of {', '.join(NODE_TYPES)}",
            )
            return None
        allowed = COMMON_FIELDS | TYPE_FIELDS[ntype]
        for k in raw:
            if k == "timeout" and ntype not in TIMEOUT_TYPES:
                self.issue(
                    f"{p}.timeout",
                    f"timeout does nothing on {ntype} nodes",
                    self.key_pos(raw, k),
                    "only agent and action nodes time out; remove it",
                )
            elif k not in allowed:
                self.issue(
                    f"{p}.{k}",
                    f"field not allowed on {ntype} nodes",
                    self.key_pos(raw, k),
                    _suggest(k, allowed),
                )
        for k in REQUIRED[ntype]:
            if raw.get(k) in (None, ""):
                self.issue(p, f"{ntype} nodes need {k!r}", pos)

        node = NodeSpec(id=node_id, type=ntype, line=pos[0] + 1 if pos else None)
        node.inputs = self._str_list(raw, "in", f"{p}.in")
        if "out" in raw:
            if not isinstance(raw["out"], str) or not _IDENT.match(raw["out"]):
                self.issue(f"{p}.out", "out must be an artifact name", self.key_pos(raw, "out"))
            else:
                node.out = raw["out"]
        if "max_attempts" in raw:
            v = raw["max_attempts"]
            if isinstance(v, bool) or not isinstance(v, int) or v < 1:
                self.issue(
                    f"{p}.max_attempts",
                    "must be an integer >= 1",
                    self.key_pos(raw, "max_attempts"),
                )
            else:
                node.max_attempts = v
        if "timeout" in raw:
            try:
                node.timeout = parse_duration(raw["timeout"])
            except ValueError as e:
                self.issue(f"{p}.timeout", str(e), self.key_pos(raw, "timeout"))
        node.budget_usd = self._number(raw, "budget_usd", f"{p}.budget_usd")
        node.description = str(raw.get("description", ""))
        if "with" in raw:
            if isinstance(raw["with"], dict):
                node.params = _plain(raw["with"])
            else:
                self.issue(f"{p}.with", "with must be a mapping", self.key_pos(raw, "with"))

        if ntype in ("agent", "action") and "for_each" in raw:
            fe = raw.get("for_each")
            if not isinstance(fe, str) or not fe.strip():
                self.issue(f"{p}.for_each", "for_each must be <artifact> or <artifact>.<field>",
                           self.key_pos(raw, "for_each"))  # fmt: skip
            else:
                node.for_each = fe.strip()
            mp = raw.get("max_parallel", 4)
            if isinstance(mp, bool) or not isinstance(mp, int) or mp < 1:
                self.issue(f"{p}.max_parallel", "must be an integer >= 1",
                           self.key_pos(raw, "max_parallel"))  # fmt: skip
            else:
                node.max_parallel = mp
        if ntype == "chat":
            if "harness" in raw and "agent" in raw:
                self.issue(f"{p}.agent", "'agent' is the old name of 'harness': keep one",
                           self.key_pos(raw, "agent"))  # fmt: skip
            node.harness = raw.get("harness", raw.get("agent"))
            node.prompt, node.prompt_file = self._prompt(raw, p)
            node.output_schema = self._schema(raw, p)
            node.allowed_tools = self._str_list(raw, "allowed_tools", f"{p}.allowed_tools")
            if "max_turns" in raw:
                v = raw["max_turns"]
                if isinstance(v, bool) or not isinstance(v, int) or v < 1:
                    self.issue(
                        f"{p}.max_turns", "must be an integer >= 1", self.key_pos(raw, "max_turns")
                    )
                else:
                    node.max_turns = v
            node.model = raw.get("model")
            if "skills" in raw:
                node.skills = self._str_list(raw, "skills", f"{p}.skills")
            if "mcp" in raw:
                node.mcp = self._str_list(raw, "mcp", f"{p}.mcp")
        elif ntype == "agent":
            node.runner = raw.get("runner")
            if "harness" in raw and "agent" in raw:
                self.issue(f"{p}.agent", "'agent' is the old name of 'harness': keep one",
                           self.key_pos(raw, "agent"))  # fmt: skip
            node.harness = raw.get("harness", raw.get("agent"))
            if node.runner and node.harness:
                self.issue(
                    p,
                    "use either 'harness' (from Settings) or 'runner' (a low-level runner)",
                    pos,
                    "harness: picks Claude Code/Codex/Aider/Built-in + model from Settings",
                )
            node.prompt, node.prompt_file = self._prompt(raw, p)
            node.output_schema = self._schema(raw, p)
            if "allowed_tools" in raw:
                node.allowed_tools = self._str_list(raw, "allowed_tools", f"{p}.allowed_tools")
            node.workspace = self._workspace(raw, p)
            if "max_turns" in raw:
                v = raw["max_turns"]
                if isinstance(v, bool) or not isinstance(v, int) or v < 1:
                    self.issue(
                        f"{p}.max_turns", "must be an integer >= 1", self.key_pos(raw, "max_turns")
                    )
                else:
                    node.max_turns = v
            node.model = raw.get("model")
            if "skills" in raw:
                node.skills = self._str_list(raw, "skills", f"{p}.skills")
            if "mcp" in raw:
                node.mcp = self._str_list(raw, "mcp", f"{p}.mcp")
        elif ntype == "action":
            node.uses = raw.get("uses")
            node.effect = self._effect(raw, p)
        elif ntype == "router":
            node.router_mode = str(raw.get("mode") or "switch")
            if node.router_mode not in ROUTER_MODES:
                self.issue(
                    f"{p}.mode",
                    f"router mode must be one of {', '.join(ROUTER_MODES)}",
                    self.key_pos(raw, "mode"),
                )
            if node.router_mode == "llm":
                if raw.get("switch") is not None:
                    self.issue(
                        f"{p}.switch",
                        "an llm router decides by prompt, not switch",
                        self.key_pos(raw, "switch"),
                    )
                node.prompt, node.prompt_file = self._prompt(raw, p)
                if not node.prompt:
                    self.issue(p, "llm routers need 'prompt' (what to decide)", pos)
                node.model = raw.get("model")
            else:
                for k in ("prompt", "model"):
                    if k in raw:
                        self.issue(f"{p}.{k}", f"'{k}' needs mode: llm", self.key_pos(raw, k))
                if raw.get("switch") in (None, ""):
                    self.issue(p, "router nodes need 'switch' (or mode: llm with a prompt)", pos)
            node.switch = raw.get("switch")
            if node.switch and "." not in str(node.switch):
                self.issue(
                    f"{p}.switch",
                    "switch must be <artifact>.<field>, e.g. review.state",
                    self.key_pos(raw, "switch"),
                )
            if not node.inputs and node.switch:
                node.inputs = (str(node.switch).split(".")[0],)
        elif ntype == "gate":
            node.prompt = str(raw.get("prompt", ""))
            node.approvers = self._str_list(raw, "approvers", f"{p}.approvers")
        elif ntype == "wait":
            node.wait_for = raw.get("for")
            if "poll_every" in raw:
                try:
                    node.poll_every = parse_duration(raw["poll_every"])
                except ValueError as e:
                    self.issue(f"{p}.poll_every", str(e), self.key_pos(raw, "poll_every"))
        return node

    def _str_list(self, m: Any, key: str, path: str) -> tuple[str, ...]:
        if key not in m or m[key] is None:
            return ()
        v = m[key]
        if isinstance(v, str):
            return (v,)
        if isinstance(v, list) and all(isinstance(x, str) for x in v):
            return tuple(v)
        self.issue(path, "expected a string or a list of strings", self.key_pos(m, key))
        return ()

    def _prompt(self, raw: Any, p: str) -> tuple[str | None, str | None]:
        v = raw.get("prompt")
        if v is None:
            return None, None
        file: str | None = None
        if isinstance(v, dict):
            if "file" in v:
                file = str(v["file"])
            elif "text" in v:
                return str(v["text"]), None
            else:
                self.issue(
                    f"{p}.prompt", "prompt map needs 'file' or 'text'", self.key_pos(raw, "prompt")
                )
                return None, None
        elif isinstance(v, str) and _PATHLIKE.match(v.strip()):
            file = v.strip()
        elif isinstance(v, str):
            return v, None
        else:
            self.issue(
                f"{p}.prompt", "prompt must be text or a file path", self.key_pos(raw, "prompt")
            )
            return None, None
        target = (self.base_dir / file).resolve()
        if not target.is_file():
            self.issue(
                f"{p}.prompt",
                f"prompt file {file!r} not found",
                self.key_pos(raw, "prompt"),
                f"looked in {self.base_dir}",
            )
            return None, file
        return target.read_text(), file

    def _schema(self, raw: Any, p: str) -> dict[str, Any] | None:
        v = raw.get("output_schema")
        if v is None:
            return None
        if isinstance(v, dict):
            return _plain(v)
        if isinstance(v, str):
            target = (self.base_dir / v).resolve()
            if not target.is_file():
                self.issue(
                    f"{p}.output_schema",
                    f"schema file {v!r} not found",
                    self.key_pos(raw, "output_schema"),
                )
                return None
            try:
                schema = json.loads(target.read_text())
            except json.JSONDecodeError as e:
                self.issue(
                    f"{p}.output_schema",
                    f"schema file is not valid JSON: {e}",
                    self.key_pos(raw, "output_schema"),
                )
                return None
            if not isinstance(schema, dict):
                self.issue(
                    f"{p}.output_schema",
                    "schema must be a JSON object",
                    self.key_pos(raw, "output_schema"),
                )
                return None
            return schema
        self.issue(
            f"{p}.output_schema",
            "expected a file path or a mapping",
            self.key_pos(raw, "output_schema"),
        )
        return None

    def _workspace(self, raw: Any, p: str) -> WorkspaceSpec | None:
        v = raw.get("workspace")
        if v is None or v == "none":
            return None
        if isinstance(v, str):
            return WorkspaceSpec(type=v)
        if isinstance(v, dict):
            known = {"type", "image"}
            options = {k: _plain(x) for k, x in v.items() if k not in known}
            return WorkspaceSpec(
                type=str(v.get("type", "container")),
                image=str(v.get("image", "auto")),
                options=options,
            )
        self.issue(
            f"{p}.workspace",
            "workspace must be a type name or a mapping",
            self.key_pos(raw, "workspace"),
        )
        return None

    def _effect(self, raw: Any, p: str) -> EffectPolicy:
        v = raw.get("effect")
        if v is None:
            return EffectPolicy()
        if not isinstance(v, dict):
            self.issue(f"{p}.effect", "effect must be a mapping", self.key_pos(raw, "effect"))
            return EffectPolicy()
        for k in v:
            if k not in ("key", "on_unknown"):
                self.issue(
                    f"{p}.effect.{k}", "unknown field", self.key_pos(v, k), "use key or on_unknown"
                )
        on_unknown = v.get("on_unknown")
        if on_unknown is not None and on_unknown not in ("halt", "retry", "ai"):
            self.issue(
                f"{p}.effect.on_unknown",
                f"must be halt, retry or ai, not {on_unknown!r}",
                self.key_pos(v, "on_unknown"),
            )
            on_unknown = None
        return EffectPolicy(key=v.get("key"), on_unknown=on_unknown)

    def _edges(self, i: int, raw: Any, parent: Any) -> list[EdgeSpec]:
        p = f"edges[{i}]"
        pos = self.item_pos(parent, i)
        line = pos[0] + 1 if pos else None
        if isinstance(raw, str):
            parts = [x.strip() for x in raw.split("->")]
            if len(parts) < 2 or not all(parts):
                self.issue(p, f"bad edge chain {raw!r}", pos, "write 'a -> b -> c'")
                return []
            if any("[" in x or "]" in x for x in parts):
                self.issue(
                    p,
                    "conditions aren't allowed in chain edges",
                    pos,
                    "use { from: a, to: b, when: label }",
                )
                return []
            return [EdgeSpec(a, b, line=line) for a, b in itertools.pairwise(parts)]
        if isinstance(raw, dict):
            for k in raw:
                if k not in EDGE_FIELDS:
                    self.issue(
                        f"{p}.{k}",
                        "unknown edge field",
                        self.key_pos(raw, k),
                        _suggest(k, EDGE_FIELDS),
                    )
            src, dst = raw.get("from"), raw.get("to")
            if not isinstance(src, str) or not isinstance(dst, str):
                self.issue(p, "edge needs 'from' and 'to'", pos)
                return []
            max_loops = raw.get("max_loops")
            if max_loops is not None and (
                isinstance(max_loops, bool) or not isinstance(max_loops, int)
            ):
                self.issue(f"{p}.max_loops", "must be an integer", self.key_pos(raw, "max_loops"))
                max_loops = None
            when = raw.get("when")
            return [
                EdgeSpec(
                    src,
                    dst,
                    when=None if when is None else str(when),
                    max_loops=max_loops,
                    on_exhausted=str(raw.get("on_exhausted", "needs-review")),
                    line=line,
                )
            ]
        self.issue(p, "edge must be 'a -> b' or a mapping", pos)
        return []


def _plain(value: Any) -> Any:
    """ruamel's CommentedMap/Seq -> plain dict/list."""
    if isinstance(value, dict):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_plain(v) for v in value]
    return value


def _suggest(key: Any, known: set[str]) -> str | None:
    import difflib

    match = difflib.get_close_matches(str(key), sorted(known), n=1)
    return f"did you mean {match[0]!r}?" if match else None


def parse_spec(
    source: str, *, path: Path | None = None, base_dir: Path | None = None
) -> PipelineSpec:
    base = base_dir or (path.parent if path else Path.cwd())
    return _Parser(source, path, base).parse()


def load_spec(path: str | Path) -> PipelineSpec:
    p = Path(path)
    return parse_spec(p.read_text(), path=p)
