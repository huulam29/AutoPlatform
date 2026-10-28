import { describe, expect, it } from "vitest";
import { diffStats, lineDiff, withContext } from "./diff";

describe("lineDiff", () => {
  it("finds changed lines and keeps the rest", () => {
    const d = lineDiff("a\nb\nc\nd", "a\nB\nc\nd\ne");
    expect(d).toEqual([
      { op: " ", text: "a" },
      { op: "-", text: "b" },
      { op: "+", text: "B" },
      { op: " ", text: "c" },
      { op: " ", text: "d" },
      { op: "+", text: "e" },
    ]);
    expect(diffStats(d)).toEqual({ added: 2, removed: 1 });
    expect(lineDiff("same", "same")).toEqual([{ op: " ", text: "same" }]);
  });
  it("collapses unchanged runs", () => {
    const before = Array.from({ length: 20 }, (_, i) => `l${i}`).join("\n");
    const after = before.replace("l10", "X");
    const c = withContext(lineDiff(before, after), 1);
    expect(c[0]).toBeNull();
    expect(c[c.length - 1]).toBeNull();
    expect(c.filter(Boolean).map((l) => l!.text)).toEqual(["l9", "l10", "X", "l11"]);
  });
});
