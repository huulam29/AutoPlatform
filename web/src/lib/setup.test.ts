import { describe, expect, it } from "vitest";
import { firstStep, harnessModelOptions, sourcesNextLabel, suggestedHarness } from "./setup";
import type { SetupHarness, SetupSource, SetupStatus } from "../types";

const src = (id: string, available: boolean): SetupSource => ({
  id, title: id, description: "", type: "openai-compatible", key: `${id.toUpperCase()}_KEY`,
  available, hint: "", local_models: [],
});
const harness = (id: string, available: boolean, cli: string | null, models: string[] = []): SetupHarness => ({
  id, kind: id, title: id === "builtin" ? "Built-in" : id, description: "", available, hint: "",
  models, model: models[0] ?? null, cli,
});
const status = (over: Partial<SetupStatus>): SetupStatus => ({
  ready: false, first_run: true, needs_setup: true, choice: null, assistant: null, managed: null,
  sources: [src("openrouter", false)], harnesses: [harness("claude-code", false, "claude"), harness("builtin", false, null)],
  ...over,
});

describe("first-run setup", () => {
  it("starts with the model API on the first run, or when no source works", () => {
    expect(firstStep(status({}))).toBe("sources");
    expect(firstStep(status({ first_run: false }))).toBe("sources");
    expect(firstStep(status({ first_run: false, sources: [src("openrouter", true)] }))).toBe("harness");
    expect(firstStep(status({ sources: [src("openrouter", true)] }))).toBe("sources");
  });

  it("step 1 can be skipped until a source works", () => {
    expect(sourcesNextLabel(status({}))).toBe("Skip this step");
    expect(sourcesNextLabel(status({ sources: [src("openrouter", true)] }))).toBe("Next: pick a harness");
  });

  it("a CLI harness can keep its own model; the built-in one lists only working models", () => {
    expect(harnessModelOptions(harness("claude-code", true, "claude", ["opus"])).map((o) => o.value)).toEqual(["", "opus"]);
    expect(harnessModelOptions(harness("builtin", true, null, ["free", "auto"])).map((o) => o.value)).toEqual(["free", "auto"]);
  });

  it("suggests the saved harness when it can run, else the first that can", () => {
    const hs = [harness("claude-code", false, "claude"), harness("builtin", true, null, ["free"])];
    expect(suggestedHarness(status({ harnesses: hs }))).toBe("builtin");
    expect(suggestedHarness(status({ harnesses: hs, choice: { harness: "claude-code", model: null } }))).toBe("builtin");
    expect(suggestedHarness(status({ harnesses: hs.map((h) => ({ ...h, available: true })), choice: { harness: "claude-code", model: null } }))).toBe("claude-code");
  });
});
