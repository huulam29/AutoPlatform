// Model <-> React Flow conversion and model edits. Pure functions (unit tested).

import type { Edge, Node } from "@xyflow/react";
import type { AiSummary, Issue, Model, ModelEdge, ModelNode, NodeType } from "../types";
import { aiLine, routerMode, type AiLine } from "./ai";
import { layout } from "./layout";
import { startNode } from "./start";

export interface NodeData extends Record<string, unknown> {
  id: string;
  nodeType: string;
  summary: string;
  issues: Issue[];
  ai?: AiLine | null; // agent nodes and LLM routers: effective agent and model
  start?: boolean; // the node that takes a run's input (`start:`)
  status?: string; // run view: latest execution status
  iterations?: number; // run view: iteration count (badge when > 1)
  tasks?: import("../types").TaskCounts; // run view: task counts (shown when total > 1)
}

export interface EdgeData extends Record<string, unknown> {
  index: number;
  back: boolean;
  issues: Issue[];
}

export type FlowNode = Node<NodeData, "pipeline">;
export type FlowEdge = Edge<EdgeData>;

export interface IssueIndex {
  byNode: Map<string, Issue[]>;
  byEdge: Map<number, Issue[]>;
  general: Issue[];
}

/** Attach each issue to its node, its edges, or the general list. */
export function indexIssues(issues: Issue[]): IssueIndex {
  const byNode = new Map<string, Issue[]>();
  const byEdge = new Map<number, Issue[]>();
  const general: Issue[] = [];
  for (const issue of issues) {
    if (issue.node_id) {
      byNode.set(issue.node_id, [...(byNode.get(issue.node_id) ?? []), issue]);
    } else if (issue.edge_indexes?.length) {
      for (const i of issue.edge_indexes) byEdge.set(i, [...(byEdge.get(i) ?? []), issue]);
    } else {
      general.push(issue);
    }
  }
  return { byNode, byEdge, general };
}

/** The node an issue should focus when clicked (an edge issue focuses the edge's source). */
export function issueTarget(issue: Issue, model: Model): string | null {
  if (issue.node_id) return issue.node_id;
  const i = issue.edge_index ?? issue.edge_indexes?.[0];
  if (i != null && model.edges[i]) {
    const e = model.edges[i];
    return model.nodes.some((n) => n.id === e.src) ? e.src : e.dst;
  }
  return null;
}

export function nodeSummary(node: ModelNode): string {
  const f = node.fields;
  const s = (v: unknown) => (typeof v === "string" ? v : v == null ? "" : JSON.stringify(v));
  switch (node.type) {
    case "agent": // with `agent:` (or nothing) the effective agent/model line shows instead
      return f.runner ? [s(f.runner), f.model ? s(f.model) : ""].filter(Boolean).join(" · ") : "";
    case "action":
      return s(f.uses);
    case "router":
      if (routerMode(f) === "llm") return "llm router";
      return f.switch ? `switch ${s(f.switch)}` : "";
    case "gate": {
      const p = s(f.prompt);
      return p.length > 40 ? p.slice(0, 39) + "…" : p;
    }
    case "wait":
      return s(f.for);
    default:
      return "";
  }
}

export function edgeLabel(e: ModelEdge): string {
  const parts: string[] = [];
  if (e.when) parts.push(e.when);
  if (e.max_loops != null) parts.push(`≤${e.max_loops}×`);
  if (e.on_exhausted && e.on_exhausted !== "needs-review") parts.push(`→ ${e.on_exhausted}`);
  return parts.join(" ");
}

export function toFlow(
  model: Model,
  ai?: AiSummary | null,
): { nodes: FlowNode[]; edges: FlowEdge[] } {
  const ids = model.nodes.map((n) => n.id);
  const { positions, backEdges } = layout(ids, model.edges);
  const issues = indexIssues(model.issues ?? []);
  const first = startNode(model);
  const nodes: FlowNode[] = model.nodes.map((n) => ({
    id: n.id,
    type: "pipeline",
    position: positions[n.id] ?? { x: 0, y: 0 },
    data: {
      id: n.id,
      nodeType: n.type ?? "?",
      summary: nodeSummary(n),
      ai: aiLine(n, model.defaults, ai),
      issues: issues.byNode.get(n.id) ?? [],
      start: n.id === first,
    },
  }));
  const known = new Set(ids);
  const edges: FlowEdge[] = [];
  model.edges.forEach((e, i) => {
    if (!known.has(e.src) || !known.has(e.dst)) return; // shown in the issues list instead
    const back = backEdges.has(i);
    const edgeIssues = issues.byEdge.get(i) ?? [];
    edges.push({
      id: `e${i}`,
      source: e.src,
      target: e.dst,
      sourceHandle: back ? "back-out" : "out",
      targetHandle: back ? "back-in" : "in",
      label: edgeLabel(e) || undefined,
      type: back ? "smoothstep" : "default",
      animated: back,
      className: [back ? "edge-back" : "", edgeIssues.length ? "edge-issue" : ""]
        .filter(Boolean)
        .join(" "),
      data: { index: i, back, issues: edgeIssues },
    });
  });
  return { nodes, edges };
}

// -- edits -----------------------------------------------------------------------------------

export function clone<T>(x: T): T {
  return JSON.parse(JSON.stringify(x)) as T;
}

export function newNodeId(type: string, existing: Iterable<string>): string {
  const taken = new Set(existing);
  for (let i = 1; ; i++) {
    const id = `${type}_${i}`;
    if (!taken.has(id)) return id;
  }
}

/** Minimal valid-ish fields per type (required fields left empty show as issues). */
export function defaultFields(type: NodeType): Record<string, unknown> {
  switch (type) {
    case "agent":
      return { type, prompt: "" }; // harness + model come from the pipeline defaults
    case "action":
      return { type, uses: "" };
    case "router":
      return { type, switch: "" };
    case "gate":
      return { type, prompt: "Approve?" };
    case "wait":
      return { type, for: "" };
    case "chat":
      return { type, prompt: "" }; // harness + model come from the pipeline defaults
  }
}

export function addNode(model: Model, type: NodeType, id?: string): Model {
  const m = clone(model);
  const nid = id ?? newNodeId(type, m.nodes.map((n) => n.id));
  m.nodes.push({ id: nid, type, fields: defaultFields(type) });
  return m;
}

export function removeNode(model: Model, id: string): Model {
  const m = clone(model);
  m.nodes = m.nodes.filter((n) => n.id !== id);
  m.edges = m.edges.filter((e) => e.src !== id && e.dst !== id);
  return m;
}

export function renameNode(model: Model, from: string, to: string): Model {
  if (from === to || !to || model.nodes.some((n) => n.id === to)) return model;
  const m = clone(model);
  for (const n of m.nodes) {
    if (n.id === from) {
      n.prev_id = n.prev_id ?? from;
      n.id = to;
    }
  }
  for (const e of m.edges) {
    if (e.src === from) e.src = to;
    if (e.dst === from) e.dst = to;
    if (e.on_exhausted === from) e.on_exhausted = to;
  }
  return m;
}

export function setFields(model: Model, id: string, fields: Record<string, unknown>): Model {
  const m = clone(model);
  const n = m.nodes.find((x) => x.id === id);
  if (n) {
    n.fields = fields;
    if (typeof fields.type === "string") n.type = fields.type;
  }
  return m;
}

/** Connect two nodes. Edges out of a router need a `when`: the next free `case_N`. */
export function connect(model: Model, src: string, dst: string): Model {
  const m = clone(model);
  const edge: ModelEdge = { src, dst };
  const source = m.nodes.find((n) => n.id === src);
  if (source?.type === "router") {
    const used = new Set(m.edges.filter((e) => e.src === src).map((e) => e.when));
    let i = 1;
    while (used.has(`case_${i}`)) i++;
    edge.when = `case_${i}`;
  }
  if (m.edges.some((e) => e.src === src && e.dst === dst && e.when === edge.when)) return model;
  m.edges.push(edge);
  return m;
}

export function updateEdge(model: Model, index: number, patch: Partial<ModelEdge>): Model {
  const m = clone(model);
  const e = { ...m.edges[index], ...patch };
  for (const k of ["when", "max_loops", "on_exhausted"] as const) {
    if (e[k] === "" || e[k] === null || e[k] === undefined) delete e[k];
  }
  m.edges[index] = e;
  return m;
}

export function removeEdge(model: Model, index: number): Model {
  const m = clone(model);
  m.edges.splice(index, 1);
  return m;
}

/** The model as sent to the server (issues are derived, not stored). */
export function forSave(model: Model): Omit<Model, "issues"> {
  const { issues: _ignored, ...rest } = clone(model);
  void _ignored;
  return rest;
}
