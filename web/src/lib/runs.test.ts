import { describe, expect, it } from "vitest";
import type { Execution, RunDetail } from "../types";
import { toFlow } from "./graph";
import { exampleModel } from "./fixtures";
import { canRetry, colorGroup, formatCost, latestByNode, runSummary, withRunState } from "./runs";

const ex = (node_id: string, iteration: number, status: string, created_at = 0): Execution => ({
  id: `${node_id}#${iteration}@${created_at}`,
  node_id,
  iteration,
  key: `${node_id}#${iteration}`,
  status,
  attempt: 1,
  reclaims: 0,
  cost_usd: 0,
  reason: null,
  error: null,
  detail: {},
  result_ref: null,
  created_at,
  started_at: null,
  finished_at: null,
});

describe("run state", () => {
  it("takes the latest iteration per node", () => {
    const s = latestByNode([
      ex("implement", 1, "done", 1),
      ex("implement", 2, "running", 5),
      ex("plan", 1, "done", 0),
    ]);
    expect(s.get("implement")!.latest.status).toBe("running");
    expect(s.get("implement")!.iterations).toBe(2);
    expect(s.get("plan")!.iterations).toBe(1);
  });

  it("colors the graph and leaves unstarted nodes marked", () => {
    const { nodes } = toFlow(exampleModel());
    const detail = {
      executions: [ex("read_ticket", 1, "done"), ex("route", 1, "skipped"), ex("route", 2, "done")],
    } as unknown as RunDetail;
    const colored = withRunState(nodes, detail);
    const by = Object.fromEntries(colored.map((n) => [n.id, n.data]));
    expect(by.read_ticket.status).toBe("done");
    expect(by.route.status).toBe("done");
    expect(by.route.iterations).toBe(2);
    expect(by.test.status).toBe("not-started");
  });

  it("formats costs", () => {
    expect(formatCost(0.5)).toBe("$0.50");
    expect(formatCost(0.0012)).toBe("$0.0012");
    expect(formatCost(null)).toBe("–");
  });
});


describe("run colors and summary", () => {
  it("groups statuses into the five colors", () => {
    expect(colorGroup("done")).toBe("pass");
    for (const s of ["failed", "dead"]) expect(colorGroup(s)).toBe("fail");
    expect(colorGroup("needs-review")).toBe("attn");
    expect(colorGroup("waiting", "gate")).toBe("attn");
    expect(colorGroup("running")).toBe("run");
    for (const s of ["pending", "ready", "waiting", "not-started", undefined]) expect(colorGroup(s)).toBe("wait");
    for (const s of ["skipped", "cancelled"]) expect(colorGroup(s)).toBe("skip");
    expect(canRetry("dead") && canRetry("needs-review") && !canRetry("done")).toBe(true);
  });

  it("counts nodes and tasks", () => {
    const x = (id: string, node: string, status: string) =>
      ({ id, node_id: node, iteration: 1, status, created_at: 1 }) as never;
    const s = runSummary({
      executions: [x("a", "plan", "done"), x("b", "work", "running"), x("c", "ship", "pending")],
      node_counts: {
        plan: { total: 1, pending: 0, running: 0, done: 1, failed: 0 },
        work: { total: 5, pending: 1, running: 2, done: 1, failed: 1 },
        ship: { total: 1, pending: 1, running: 0, done: 0, failed: 0 },
      },
    } as never);
    expect(s.nodes).toEqual({ pass: 1, fail: 0, run: 1, attn: 0, wait: 1, skip: 0 });
    expect([s.tasksRunning, s.tasksTotal, s.tasksFailed]).toEqual([2, 7, 1]);
  });
});
