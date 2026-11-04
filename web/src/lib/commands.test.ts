import { describe, expect, it } from "vitest";
import { buildCommands, filterCommands, isTypingTarget, SequenceKeys } from "./commands";

const calls: string[] = [];
const commands = buildCommands({
  go: (r) => calls.push(`go ${r}`),
  refresh: () => calls.push("refresh"),
  restartEngine: () => calls.push("restart"),
  toggleChat: () => calls.push("chat"),
  chatKey: "Ctrl+J",
});
const titles = (q: string) => filterCommands(commands, q).map((c) => c.title);

describe("command palette", () => {
  it("has every Mac app menu action", () => {
    const all = commands.map((c) => c.title);
    for (const t of ["Start Run…", "Pipelines", "Runs", "Approvals", "Problems", "Settings", "Refresh", "Restart Engine", "Show Logs"]) {
      expect(all).toContain(t);
    }
  });

  it("filters by word starts and keywords, initials first", () => {
    expect(titles("appr")).toEqual(["Approvals"]);
    expect(titles("gates")).toEqual(["Approvals"]);
    expect(titles("re en")).toEqual(["Restart Engine"]);
    expect(titles("sl")).toEqual(["Show Logs"]);
    expect(titles("re").slice(0, 2)).toEqual(["Restart Engine", "Refresh"]); // initials first
    expect(titles("zzz")).toEqual([]);
    expect(titles("  ")).toHaveLength(commands.length);
  });

  it("runs a command", () => {
    calls.length = 0;
    filterCommands(commands, "approvals")[0].run();
    expect(calls).toEqual(["go /approvals"]);
  });
});

describe("g-sequences", () => {
  it("g then a letter within the window, n alone", () => {
    const s = new SequenceKeys(commands, 1500);
    expect(s.press("g", 0)).toBeNull();
    expect(s.press("r", 100)?.id).toBe("runs");
    expect(s.press("n", 200)?.id).toBe("start-run");
    expect(s.press("g", 300)).toBeNull();
    expect(s.press("a", 2000)).toBeNull(); // too late
    expect(s.press("g", 3000)).toBeNull();
    expect(s.press("q", 3100)).toBeNull(); // no such sequence
    expect(s.press("x", 3200)).toBeNull();
  });
});

describe("typing targets", () => {
  const el = (tag: string, extra: { isContentEditable?: boolean; inEditor?: boolean } = {}) =>
    ({
      tagName: tag,
      isContentEditable: extra.isContentEditable ?? false,
      closest: () => (extra.inEditor ? {} : null),
    }) as unknown as Element;

  it("fields and the code editor take keys as text", () => {
    expect(isTypingTarget(el("INPUT"))).toBe(true);
    expect(isTypingTarget(el("TEXTAREA"))).toBe(true);
    expect(isTypingTarget(el("SELECT"))).toBe(true);
    expect(isTypingTarget(el("DIV", { isContentEditable: true }))).toBe(true);
    expect(isTypingTarget(el("DIV", { inEditor: true }))).toBe(true);
    expect(isTypingTarget(el("BUTTON"))).toBe(false);
    expect(isTypingTarget(null)).toBe(false);
  });
});
