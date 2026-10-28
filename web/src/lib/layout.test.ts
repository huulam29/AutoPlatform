import { describe, expect, it } from "vitest";
import { assignLayers, findBackEdges, layout } from "./layout";
import { exampleModel } from "./fixtures";

describe("layout", () => {
  it("puts the example in one layer per step with the review loop as a back edge", () => {
    const m = exampleModel();
    const l = layout(
      m.nodes.map((n) => n.id),
      m.edges,
    );
    expect(l.backEdges).toEqual(new Set([5])); // route -> implement (max_loops)
    expect(l.layers).toEqual({
      read_ticket: 0,
      plan: 1,
      implement: 2,
      open_pr: 3,
      await_review: 4,
      route: 5,
      approve: 6,
      deploy: 7,
      test: 8,
    });
    // top to bottom: y grows with the layer
    expect(l.positions.plan.y).toBeGreaterThan(l.positions.read_ticket.y);
    expect(l.positions.test.y).toBeGreaterThan(l.positions.deploy.y);
  });

  it("detects a cycle without max_loops as a back edge so it still draws", () => {
    const back = findBackEdges(
      ["a", "b", "c"],
      [
        { src: "a", dst: "b" },
        { src: "b", dst: "c" },
        { src: "c", dst: "a" },
      ],
    );
    expect(back).toEqual(new Set([2]));
  });

  it("uses the longest path for layers", () => {
    const edges = [
      { src: "a", dst: "b" },
      { src: "b", dst: "c" },
      { src: "a", dst: "c" },
    ];
    expect(assignLayers(["a", "b", "c"], edges, new Set())).toEqual({ a: 0, b: 1, c: 2 });
  });

  it("stacks parallel branches in one layer without overlap", () => {
    const l = layout(
      ["s", "x", "y", "z", "j"],
      [
        { src: "s", dst: "x" },
        { src: "s", dst: "y" },
        { src: "s", dst: "z" },
        { src: "x", dst: "j" },
        { src: "y", dst: "j" },
        { src: "z", dst: "j" },
      ],
    );
    expect(l.order[1].sort()).toEqual(["x", "y", "z"]);
    const xs = ["x", "y", "z"].map((n) => l.positions[n].x);
    expect(new Set(xs).size).toBe(3);
    expect(l.positions.x.y).toBe(l.positions.y.y);
    // the single nodes are centered against the widest layer
    expect(l.positions.s.x).toBe(l.positions.y.x);
  });

  it("orders a layer to follow its parents (barycenter)", () => {
    const l = layout(
      ["a", "b", "d", "c"],
      [
        { src: "a", dst: "c" },
        { src: "b", dst: "d" },
      ],
    );
    expect(l.order[1]).toEqual(["c", "d"]);
  });

  it("handles self loops, unknown endpoints and an empty graph", () => {
    const l = layout(["a"], [
      { src: "a", dst: "a", max_loops: 2 },
      { src: "a", dst: "ghost" },
    ]);
    expect(l.layers).toEqual({ a: 0 });
    expect(l.backEdges.has(0)).toBe(true);
    expect(layout([], []).order).toEqual([]);
  });
});
