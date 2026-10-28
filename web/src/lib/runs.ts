// Run view helpers: latest execution per node and the graph colored by status.

import type { Execution, LineageEntry, RunDetail } from "../types";
import type { FlowNode } from "./graph";

export interface NodeRunState {
  latest: Execution;
  iterations: number;
  all: Execution[];
}

/** Latest iteration (then latest attempt row) per node. */
export function latestByNode(executions: Execution[]): Map<string, NodeRunState> {
  const out = new Map<string, NodeRunState>();
  for (const x of executions) {
    const cur = out.get(x.node_id);
    if (!cur) {
      out.set(x.node_id, { latest: x, iterations: x.iteration, all: [x] });
      continue;
    }
    cur.all.push(x);
    cur.iterations = Math.max(cur.iterations, x.iteration);
    if (
      x.iteration > cur.latest.iteration ||
      (x.iteration === cur.latest.iteration && x.created_at >= cur.latest.created_at)
    ) {
      cur.latest = x;
    }
  }
  return out;
}

export function withRunState(nodes: FlowNode[], detail: RunDetail): FlowNode[] {
  const state = latestByNode(detail.executions);
  return nodes.map((n) => {
    const s = state.get(n.id);
    return {
      ...n,
      data: {
        ...n.data,
        issues: [],
        status: s?.latest.status ?? "not-started",
        iterations: s?.iterations ?? 0,
        tasks: detail.node_counts?.[n.id],
      },
    };
  });
}

export function lineageFor(detail: RunDetail, executionId: string): LineageEntry | undefined {
  return detail.lineage.find((l) => l.execution_id === executionId);
}

export const TERMINAL = new Set(["done", "dead", "cancelled"]);

export function formatCost(usd: number | null | undefined): string {
  if (usd == null) return "–";
  return usd < 0.01 && usd > 0 ? `$${usd.toFixed(4)}` : `$${usd.toFixed(2)}`;
}

export function formatTime(ts: number | null | undefined): string {
  if (!ts) return "–";
  return new Date(ts * 1000).toLocaleString();
}

/** The status tone (decision 59): pass (done, mint), run (running, cyan), attn (a person must
 * act: needs-review, or a gate waiting; amber), fail (failed / dead, red), wait (pending, ready,
 * waiting on something else; steel), skip (skipped / cancelled; dimmed). */
export type ColorGroup = "pass" | "fail" | "run" | "attn" | "wait" | "skip";

export function colorGroup(status: string | undefined, nodeType?: string): ColorGroup {
  switch (status) {
    case "done":
      return "pass";
    case "failed":
    case "dead":
      return "fail";
    case "needs-review":
      return "attn";
    case "waiting":
      return nodeType === "gate" || nodeType === "chat" ? "attn" : "wait"; // waits for you
    case "running":
      return "run";
    case "skipped":
    case "cancelled":
      return "skip";
    default:
      return "wait"; // pending, ready, not-started
  }
}

/** Toolbar dashboard: nodes per color group and tasks running / total across the run. */
export function runSummary(detail: RunDetail): {
  nodes: Record<ColorGroup, number>;
  tasksRunning: number;
  tasksTotal: number;
  tasksFailed: number;
} {
  const nodes: Record<ColorGroup, number> = { pass: 0, fail: 0, run: 0, attn: 0, wait: 0, skip: 0 };
  for (const s of latestByNode(detail.executions).values()) nodes[colorGroup(s.latest.status)] += 1;
  let tasksRunning = 0;
  let tasksTotal = 0;
  let tasksFailed = 0;
  for (const c of Object.values(detail.node_counts ?? {})) {
    tasksRunning += c.running;
    tasksTotal += c.total;
    tasksFailed += c.failed;
  }
  return { nodes, tasksRunning, tasksTotal, tasksFailed };
}

/** Can the user retry this node now? (its latest execution is dead or needs-review) */
export function canRetry(status: string | undefined): boolean {
  return status === "dead" || status === "needs-review";
}
