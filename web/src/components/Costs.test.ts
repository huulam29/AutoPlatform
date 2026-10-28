import { describe, expect, it } from "vitest";
import { budgetPercent } from "./Costs";

describe("budgetPercent", () => {
  it("is null without a budget", () => {
    expect(budgetPercent(5, null)).toBeNull();
    expect(budgetPercent(5, 0)).toBeNull();
    expect(budgetPercent(4, 5)).toBe(80);
  });
});
