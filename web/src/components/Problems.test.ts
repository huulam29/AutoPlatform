import { describe, expect, it } from "vitest";
import { issueShown, summaryMarkdown } from "./Problems";

const issue = (n: number, status: string) =>
  ({ number: n, status, title: `t${n}`, source: { kind: "run", ref: `r${n}` }, notes: [], history: [] }) as never;

describe("problems", () => {
  it("filters by issue status", () => {
    expect(issueShown("active", issue(1, "in_progress"))).toBe(true);
    expect(issueShown("active", issue(1, "fixed"))).toBe(false);
    expect(issueShown("closed", issue(1, "wont_fix"))).toBe(true);
    expect(issueShown("all", issue(1, "fixed"))).toBe(true);
  });
  it("writes a markdown summary", () => {
    expect(summaryMarkdown([issue(2, "open")], [])).toContain("- #2 [Open] t2 (run r2)");
  });
});
