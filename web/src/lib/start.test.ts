import { describe, expect, it } from "vitest";
import type { Model } from "../types";
import { emptyFields, initialText, setStart, startBody, startInput, startNode, startNodes } from "./start";

function model(extra: Partial<Model> = {}): Model {
  return {
    name: "p",
    version: 1,
    description: null,
    trigger: null,
    budget_usd: null,
    nodes: [
      { id: "build", type: "action", fields: {} },
      { id: "plan", type: "agent", fields: {} },
    ],
    edges: [
      { src: "build", dst: "plan" },
      { src: "plan", dst: "build", max_loops: 2 },
    ],
    issues: [],
    ...extra,
  };
}

describe("start point", () => {
  it("defaults to the first node without an incoming edge (loops don't count)", () => {
    expect(startNodes(model())).toEqual(["build"]);
    expect(startNode(model())).toBe("build");
    expect(startInput(model())).toBe("json");
    expect(startNode(model({ start: { node: "plan" } }))).toBe("plan");
  });
  it("a chat node is the start point and takes chat", () => {
    const m = model();
    m.nodes.push({ id: "intake", type: "chat", fields: {} });
    expect(startNode(m)).toBe("intake");
    expect(startInput(m)).toBe("chat");
  });
  it("setStart drops empty keys, the template off json, and the section when empty", () => {
    let m = setStart(model(), "input", "text");
    expect(m.start).toEqual({ input: "text" });
    m = setStart({ ...m, start: { input: "json", template: { a: "" } } }, "input", "text");
    expect(m.start).toEqual({ input: "text" });
    expect(setStart(m, "input", undefined).start).toBeNull();
  });
});

describe("start body", () => {
  it("json: an object, pre-filled from the template", () => {
    const info = { node: "a", input: "json" as const, template: { task: "" }, fields: ["task"] };
    expect(initialText(info)).toBe('{\n  "task": ""\n}');
    expect(startBody("json", '{"task": "x"}')).toEqual({ payload: { task: "x" } });
    expect(startBody("json", "")).toEqual({ payload: {} });
    expect(startBody("json", "[1]")).toEqual({ error: "The payload must be a JSON object ({ … })." });
    expect("error" in startBody("json", "{")).toBe(true);
  });
  it("text and chat: the words, trimmed", () => {
    expect(initialText({ node: "a", input: "chat", template: null, fields: [] })).toBe("");
    expect(startBody("chat", "  fix it ")).toEqual({ text: "fix it" });
    expect(startBody("text", " ")).toEqual({ error: "Write what the run should do." });
  });
  it("lists template fields left empty", () => {
    expect(emptyFields({ task: "", repo: { name: "", owner: "me" } })).toEqual(["task", "repo.name"]);
  });
});
