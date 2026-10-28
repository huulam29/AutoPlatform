import { describe, expect, it } from "vitest";
import type { Issue } from "../types";
import {
  addNode,
  connect,
  edgeLabel,
  forSave,
  indexIssues,
  issueTarget,
  newNodeId,
  removeNode,
  renameNode,
  toFlow,
  updateEdge,
} from "./graph";
import { exampleModel } from "./fixtures";

const issue = (over: Partial<Issue>): Issue => ({
  path: "x",
  message: "m",
  line: null,
  col: null,
  hint: null,
  node_id: null,
  field: null,
  edge_index: null,
  edge_indexes: [],
  ...over,
});

describe("model -> React Flow", () => {
  it("makes one node per model node and one edge per model edge", () => {
    const { nodes, edges } = toFlow(exampleModel());
    expect(nodes.map((n) => n.id)).toEqual(exampleModel().nodes.map((n) => n.id));
    expect(nodes.every((n) => n.type === "pipeline")).toBe(true);
    expect(nodes.find((n) => n.id === "plan")!.data.summary).toBe("claude-code");
    expect(nodes.find((n) => n.id === "route")!.data.summary).toBe("switch review.state");
    expect(edges).toHaveLength(9);
    const loop = edges.find((e) => e.id === "e5")!;
    expect(loop.source).toBe("route");
    expect(loop.target).toBe("implement");
    expect(loop.data!.back).toBe(true);
    expect(loop.sourceHandle).toBe("back-out");
    expect(loop.label).toBe("changes_requested ≤3×");
    expect(edges.find((e) => e.id === "e6")!.data!.back).toBe(false);
  });

  it("skips edges whose endpoints don't exist", () => {
    const m = exampleModel();
    m.edges.push({ src: "test", dst: "ghost" });
    expect(toFlow(m).edges).toHaveLength(9);
  });

  it("puts issue badges on nodes and edges", () => {
    const m = exampleModel();
    m.issues = [
      issue({ node_id: "plan", field: "runner" }),
      issue({ edge_index: 6, edge_indexes: [6] }),
      issue({ path: "name" }),
    ];
    const { nodes, edges } = toFlow(m);
    expect(nodes.find((n) => n.id === "plan")!.data.issues).toHaveLength(1);
    const e6 = edges.find((e) => e.id === "e6")!;
    expect(e6.data!.issues).toHaveLength(1);
    expect(e6.className).toContain("edge-issue");
  });
});

describe("issues", () => {
  it("indexes issues by node, edge or general", () => {
    const idx = indexIssues([
      issue({ node_id: "a" }),
      issue({ node_id: "a" }),
      issue({ edge_index: 1, edge_indexes: [1, 2] }),
      issue({}),
    ]);
    expect(idx.byNode.get("a")).toHaveLength(2);
    expect(idx.byEdge.get(1)).toHaveLength(1);
    expect(idx.byEdge.get(2)).toHaveLength(1);
    expect(idx.general).toHaveLength(1);
  });

  it("maps an issue to the node to focus", () => {
    const m = exampleModel();
    expect(issueTarget(issue({ node_id: "deploy" }), m)).toBe("deploy");
    expect(issueTarget(issue({ edge_index: 6, edge_indexes: [6] }), m)).toBe("route");
    m.edges.push({ src: "ghost", dst: "test" });
    expect(issueTarget(issue({ edge_indexes: [9] }), m)).toBe("test");
    expect(issueTarget(issue({}), m)).toBeNull();
  });
});

describe("edits", () => {
  it("adds nodes with generated ids", () => {
    let m = addNode(exampleModel(), "gate");
    m = addNode(m, "gate");
    expect(m.nodes.slice(-2).map((n) => n.id)).toEqual(["gate_1", "gate_2"]);
    expect(m.nodes.at(-1)!.fields).toEqual({ type: "gate", prompt: "Approve?" });
    expect(newNodeId("agent", ["agent_1", "agent_3"])).toBe("agent_2");
  });

  it("removes a node with its edges", () => {
    const m = removeNode(exampleModel(), "open_pr");
    expect(m.nodes.some((n) => n.id === "open_pr")).toBe(false);
    expect(m.edges.some((e) => e.src === "open_pr" || e.dst === "open_pr")).toBe(false);
    expect(m.edges).toHaveLength(7);
  });

  it("renames a node everywhere and remembers the old id", () => {
    const m = renameNode(exampleModel(), "implement", "build");
    const n = m.nodes.find((x) => x.id === "build")!;
    expect(n.prev_id).toBe("implement");
    expect(m.edges.filter((e) => e.src === "build" || e.dst === "build")).toHaveLength(3);
    expect(renameNode(m, "build", "plan")).toBe(m); // taken: no-op
  });

  it("connects nodes; router edges get a case label", () => {
    let m = connect(exampleModel(), "test", "read_ticket");
    expect(m.edges.at(-1)).toEqual({ src: "test", dst: "read_ticket" });
    m = connect(m, "route", "test");
    expect(m.edges.at(-1)).toEqual({ src: "route", dst: "test", when: "case_1" });
    expect(connect(m, "test", "read_ticket")).toBe(m); // duplicate: no-op
  });

  it("edits edge attributes and drops empty ones", () => {
    let m = updateEdge(exampleModel(), 5, { max_loops: 5 });
    expect(m.edges[5].max_loops).toBe(5);
    expect(m.edges[5].chain_index).toBe(1);
    m = updateEdge(m, 5, { on_exhausted: "", when: null });
    expect(m.edges[5]).toEqual({ src: "route", dst: "implement", max_loops: 5, chain_index: 1 });
    expect(edgeLabel(m.edges[5])).toBe("≤5×");
  });

  it("does not send issues back to the server", () => {
    const m = exampleModel();
    m.issues = [issue({})];
    expect("issues" in forSave(m)).toBe(false);
  });
});
