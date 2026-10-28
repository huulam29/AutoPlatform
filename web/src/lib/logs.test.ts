import { describe, expect, it } from "vitest";
import { filterLines, levelOf, newSince, splitMatches } from "./logs";

const LINES = [
  "2026-10-20 09:13:33,085 INFO    agentline.worker: claimed plan#1",
  "2026-10-20 09:13:34,001 WARNING agentline.poller: jira poll slow",
  "2026-10-20 09:13:35,211 ERROR   agentline.api: boom",
  "Traceback (most recent call last):",
  '  File "x.py", line 1',
  "2026-10-20 09:13:36,000 INFO    agentline.worker: done plan#1",
  "[app] starting engine on 127.0.0.1:58129",
];

describe("log levels and filters", () => {
  it("reads the level of formatted lines only", () => {
    expect(levelOf(LINES[0])).toBe("info");
    expect(levelOf(LINES[1])).toBe("warning");
    expect(levelOf(LINES[2])).toBe("error");
    expect(levelOf(LINES[3])).toBeNull();
    expect(levelOf(LINES[6])).toBeNull();
  });

  it("keeps a traceback with the error above it", () => {
    expect(filterLines(LINES, "error", "").map((l) => l.n)).toEqual([2, 3, 4]);
    expect(filterLines(LINES, "warning", "").map((l) => l.n)).toEqual([1, 2, 3, 4]);
    expect(filterLines(LINES, "all", "").length).toBe(LINES.length);
  });

  it("searches case-insensitively, combined with the level", () => {
    expect(filterLines(LINES, "all", "PLAN#1").map((l) => l.n)).toEqual([0, 5]);
    expect(filterLines(LINES, "error", "x.py").map((l) => l.n)).toEqual([4]);
  });

  it("splits a line around matches for highlighting", () => {
    expect(splitMatches("claimed Plan#1 and plan#1", "plan#1")).toEqual([
      { text: "claimed ", hit: false },
      { text: "Plan#1", hit: true },
      { text: " and ", hit: false },
      { text: "plan#1", hit: true },
    ]);
    expect(splitMatches("abc", "")).toEqual([{ text: "abc", hit: false }]);
  });

  it("counts lines that arrived after a frozen snapshot, even when the top was trimmed", () => {
    expect(newSince(["a", "b", "c"], ["b", "c", "d", "e"])).toBe(2);
    expect(newSince(["a", "b"], ["a", "b"])).toBe(0);
    expect(newSince([], ["a"])).toBe(1);
    expect(newSince(["gone"], ["x", "y"])).toBe(2);
  });
});
