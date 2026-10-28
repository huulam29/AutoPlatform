import { describe, expect, it } from "vitest";
import { parseRoutes, routerLabels } from "./DryRun";

describe("dry run helpers", () => {
  it("lists router labels in edge order and parses scripts", () => {
    const model = {
      nodes: [
        { id: "reviews", type: "router", fields: {} },
        { id: "build", type: "agent", fields: {} },
      ],
      edges: [
        { src: "reviews", dst: "build", when: "CHANGES" },
        { src: "reviews", dst: "approver", when: "PASS" },
        { src: "build", dst: "reviews" },
      ],
    } as never;
    expect(routerLabels(model)).toEqual({ reviews: ["CHANGES", "PASS"] });
    expect(parseRoutes({ reviews: " CHANGES, PASS ,", other: "" })).toEqual({ reviews: ["CHANGES", "PASS"] });
  });
});
