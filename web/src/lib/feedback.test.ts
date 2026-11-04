import { describe, expect, it, vi } from "vitest";
import {
  OPEN_REPORT_EVENT,
  adoptionDays,
  barHeights,
  formatDuration,
  formatTokens,
  myRating,
  nextRating,
  percent,
  reportFilename,
  requestReport,
  takePendingReport,
  type FeedbackEntry,
  type FeedbackSummary,
  type Insights,
} from "./feedback";

const entry = (over: Partial<FeedbackEntry>): FeedbackEntry => ({
  id: 1,
  at: 0,
  run: "r1",
  node: null,
  iteration: 1,
  rating: "up",
  comment: "",
  by: "app",
  ...over,
});

const summary = (current: FeedbackEntry[]): FeedbackSummary => ({
  up: 0,
  down: 0,
  comments: 0,
  run: { up: 0, down: 0 },
  nodes: {},
  current,
});

describe("feedback helpers", () => {
  it("finds this browser's rating of the run or of a node", () => {
    const s = summary([
      entry({ id: 1, rating: "down" }),
      entry({ id: 2, by: "telegram:@ana", rating: "up" }),
      entry({ id: 3, node: "plan", rating: "up" }),
    ]);
    expect(myRating(s)).toBe("down");
    expect(myRating(s, "plan")).toBe("up");
    expect(myRating(s, "ship")).toBeNull();
    expect(myRating(s, null, "telegram:@ana")).toBe("up");
    expect(myRating(null)).toBeNull();
  });

  it("clicking the same thumb again withdraws it", () => {
    expect(nextRating(null, "up")).toBe("up");
    expect(nextRating("up", "up")).toBe("none");
    expect(nextRating("up", "down")).toBe("down");
  });

  it("formats durations, tokens and rates", () => {
    expect(formatDuration(0)).toBe("0 s");
    expect(formatDuration(42.4)).toBe("42 s");
    expect(formatDuration(600)).toBe("10.0 min");
    expect(formatDuration(7200)).toBe("2.0 h");
    expect(formatTokens(null)).toBe("–");
    expect(formatTokens({ input: 1200, output: 34_000, cache_read: 0, cache_write: 0 })).toBe("1,200 in / 34k out");
    expect(percent(0.875)).toBe("88%");
  });

  it("scales bars and marks adoption days", () => {
    expect(barHeights([0, 5, 10])).toEqual([0, 50, 100]);
    expect(barHeights([0, 0])).toEqual([0, 0]);
    const ins = { adoptions: [{ at: Date.UTC(2026, 10, 2, 12) / 1000, node: "w", verdict: null }] } as unknown as Insights;
    expect([...adoptionDays(ins)]).toEqual(["2026-11-02"]);
    expect(reportFilename("r9", "json")).toBe("run-r9.json");
  });

  it("a report asked for before the run's page is open waits for it", () => {
    vi.stubGlobal("window", new EventTarget()); // tests run in node
    const seen: unknown[] = [];
    const on = (e: Event) => seen.push((e as CustomEvent).detail);
    window.addEventListener(OPEN_REPORT_EVENT, on);
    requestReport("r1", "json");
    window.removeEventListener(OPEN_REPORT_EVENT, on);
    expect(seen).toEqual([{ run: "r1", format: "json" }]);
    expect(takePendingReport("r2")).toBeNull();
    expect(takePendingReport("r1")).toBe("json");
    expect(takePendingReport("r1")).toBeNull();
    vi.unstubAllGlobals();
  });
});
