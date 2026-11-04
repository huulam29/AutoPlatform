// The Mac app's NotificationPlanner tests (macos/Tests/AgentlineCoreTests/CoreTests.swift),
// ported, plus the browser's incident rate limit and notification clicks.
import { describe, expect, it } from "vitest";
import { ApiError, type Approval, type IncidentSummary } from "../api";
import type { Run } from "../types";
import { APPROVED_FROM_NOTIFICATION, handleNotificationAction, NotificationPlanner, ticketKey } from "./notify";

const approval = (exec: string, run = "r1"): Approval => ({
  run_id: run,
  pipeline: "ticket-to-eod",
  node_id: "gate",
  execution_id: exec,
  prompt: "Ship it?",
  approvers: [],
  waiting_since: null,
  payload: { issue_key: "PROJ-1" },
});
const run = (id: string, status: string): Run =>
  ({ id, flow: "p", status, payload: {}, cost_usd: 0, budget_usd: null, error: null, created_at: 0, updated_at: 0, terminal: false }) as Run;
const incident = (id: string, count = 1, status = "new"): IncidentSummary => ({
  id,
  source: "engine",
  kind: "api_error",
  title: `boom ${id}`,
  count,
  first_seen: 0,
  last_seen: 0,
  status,
});
const keys = (es: { key: string }[]) => es.map((e) => e.key);

describe("NotificationPlanner (the Mac app's rules)", () => {
  it("announces each gate once, a new iteration again, and ignores failed polls", () => {
    const p = new NotificationPlanner();
    const first = p.events([approval("x1")], []);
    expect(first).toHaveLength(1);
    expect(first[0].title).toBe("Approval needed: ticket-to-eod PROJ-1");
    expect(first[0].body).toBe("Ship it?");
    expect(first[0]).toMatchObject({ run: "r1", node: "gate", execution: "x1", route: "/runs/r1" });
    expect(p.events([approval("x1")], [])).toEqual([]);
    expect(keys(p.events([approval("x1"), approval("x2")], []))).toEqual(["gate:x2"]);
    expect(p.events(null, null)).toEqual([]);
    expect(p.events([approval("x2")], null)).toEqual([]);
  });

  it("a gate with no prompt says which gate waits", () => {
    expect(new NotificationPlanner().events([{ ...approval("x"), prompt: "" }], [])[0].body).toBe("Gate gate is waiting.");
  });

  it("takes the first runs poll as a baseline, then announces transitions once", () => {
    const p = new NotificationPlanner();
    expect(p.events([], [run("a", "done"), run("b", "running")])).toEqual([]);
    expect(p.events([], [run("a", "done"), run("b", "running")])).toEqual([]);
    const e = p.events([], [run("a", "done"), run("b", "needs-review")]);
    expect(keys(e)).toEqual(["run:b:needs-review"]);
    expect(e[0].title).toBe("Run needs review: p");
    expect(p.events([], [run("b", "running")])).toEqual([]);
    expect(keys(p.events([], [run("b", "dead")]))).toEqual(["run:b:dead"]);
    expect(keys(p.events([], [run("c", "done")]))).toEqual(["run:c:done"]);
    p.events([], [run("b", "running")]);
    expect(p.events([], [run("b", "dead")])).toEqual([]);
    expect(p.events([], [run("d", "waiting")])).toEqual([]);
  });

  it("a failed run's error is the body, cut to 200 characters", () => {
    const p = new NotificationPlanner();
    p.events([], []);
    const e = p.events([], [{ ...run("f", "dead"), error: "x".repeat(300) }]);
    expect(e[0].body).toHaveLength(200);
  });

  it("announces new problem reports after a baseline, rate-limited", () => {
    const p = new NotificationPlanner();
    const t = 1_000_000;
    expect(p.events(null, null, [incident("old")], t)).toEqual([]); // baseline
    expect(keys(p.events(null, null, [incident("old"), incident("a")], t + 1))).toEqual(["incident:a:1"]);
    // a second one within the minute waits; seen reports never announce
    expect(p.events(null, null, [incident("b")], t + 2)).toEqual([]);
    expect(p.events(null, null, [incident("s", 1, "seen")], t + 120_000)).toEqual([]);
    // the same report again inside 10 minutes stays quiet; after that it speaks
    expect(p.events(null, null, [incident("a", 2)], t + 5 * 60_000)).toEqual([]);
    expect(keys(p.events(null, null, [incident("a", 3)], t + 11 * 60_000))).toEqual(["incident:a:3"]);
  });
});

describe("ticketKey", () => {
  it("reads the same fields as the Mac app", () => {
    expect(ticketKey({ issue_key: "A-1" })).toBe("A-1");
    expect(ticketKey({ ticket: "B-2" })).toBe("B-2");
    expect(ticketKey({ issue: { key: "C-3" } })).toBe("C-3");
    expect(ticketKey({ key: "D-4" })).toBe("D-4");
    expect(ticketKey({})).toBeNull();
    expect(ticketKey("x")).toBeNull();
  });
});

describe("notification clicks", () => {
  const deps = () => {
    const calls: string[] = [];
    return {
      calls,
      d: {
        approve: async (run: string, node: string, comment: string): Promise<unknown> => {
          calls.push(`approve ${run} ${node} ${comment}`);
          return {};
        },
        go: (r: string) => {
          calls.push(`go ${r}`);
        },
        note: (t: string) => {
          calls.push(`note ${t}`);
        },
      },
    };
  };

  it("Approve decides the gate with the Mac app's comment", async () => {
    const { calls, d } = deps();
    await handleNotificationAction("approve", { run: "r1", node: "gate", route: "/runs/r1" }, d);
    expect(calls).toEqual([`approve r1 gate ${APPROVED_FROM_NOTIFICATION}`]);
  });

  it("an already decided gate says so and opens the run", async () => {
    const { calls, d } = deps();
    d.approve = async () => {
      throw new ApiError(409, "not waiting", null);
    };
    await handleNotificationAction("approve", { run: "r1", node: "gate", route: "/runs/r1" }, d);
    expect(calls).toEqual(["note Already decided", "go /runs/r1"]);
  });

  it("Reject only opens the gate with its comment field; open goes to the route", async () => {
    const { calls, d } = deps();
    await handleNotificationAction("reject", { run: "r1", node: "gate", execution: "x9" }, d);
    await handleNotificationAction("open", { route: "/app-reports" }, d);
    expect(calls).toEqual(["go /approvals?reject=x9", "go /app-reports"]);
  });
});
