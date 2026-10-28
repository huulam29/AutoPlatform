import { describe, expect, it, vi } from "vitest";
import type { Issue, ModelNode, TestedModel } from "../types";
import {
  aiLine,
  chatDefaultModelLabel,
  chatModelOptions,
  modelAfterHarnessChange,
  choose,
  compatibleForKind,
  compatibleModels,
  effective,
  emptyLabels,
  filterTested,
  formBody,
  isAiIssue,
  pickerHarness,
  PROVIDER_PRESETS,
  presetName,
  routerMode,
  setHarness,
  setDefault,
  setModel,
  setRouterMode,
  setRunner,
  submitSecret,
  selectShown,
  suggestModelName,
  toggled,
} from "./ai";
import { exampleAi, exampleModel } from "./fixtures";
import { toFlow } from "./graph";

const ai = exampleAi();

describe("choose (mirrors agents.choose)", () => {
  it("fills in from defaults as a pair", () => {
    expect(choose(null, null, "cheap", "free-llama")).toEqual(["cheap", "free-llama"]);
    expect(choose("cheap", null, "cheap", "free-llama")).toEqual(["cheap", "free-llama"]);
    expect(choose(null, "opus", "cheap", "free-llama")).toEqual(["cheap", "opus"]);
    expect(choose("claude-code", "opus", "cheap", "free-llama")).toEqual(["claude-code", "opus"]);
  });

  it("a node that picks a different harness does not inherit the default model", () => {
    expect(choose("claude-code", null, "cheap", "free-llama")).toEqual(["claude-code", null]);
  });

  it("no defaults: only the node's choice", () => {
    expect(choose(null, null, null, null)).toEqual([null, null]);
    expect(choose(null, "free-llama", null, null)).toEqual([null, "free-llama"]);
  });
});

describe("effective harness and model", () => {
  const defaults = { harness: "cheap", model: "sonnet-api" };

  it("inherits both from defaults", () => {
    expect(effective({}, defaults, ai)).toEqual({
      harness: "cheap",
      model: "sonnet-api",
      harnessSource: "defaults",
      modelSource: "defaults",
    });
  });

  it("a different harness gets its own default model, not the pipeline's", () => {
    expect(effective({ harness: "claude-code" }, defaults, ai)).toEqual({
      harness: "claude-code",
      model: "opus", // claude-code's own default in Settings
      harnessSource: "node",
      modelSource: "harness",
    });
    // a harness without a default model: no model at all
    expect(effective({ harness: "aider" }, defaults, ai).model).toBeNull();
  });

  it("the same harness as the default keeps the default model", () => {
    const e = effective({ harness: "cheap" }, defaults, ai);
    expect([e.harness, e.model, e.modelSource]).toEqual(["cheap", "sonnet-api", "defaults"]);
  });

  it("falls back like agents.resolve: builtin (the only built-in harness)", () => {
    expect(effective({}, null, ai)).toMatchObject({
      harness: "builtin",
      harnessSource: "fallback",
    });
    expect(effective({ model: "free-llama" }, null, ai)).toMatchObject({
      harness: "builtin",
      model: "free-llama",
      modelSource: "node",
    });
    expect(effective({}, { model: "free-llama" }, ai)).toMatchObject({
      harness: "builtin",
      model: "free-llama",
      harnessSource: "fallback",
      modelSource: "defaults",
    });
  });

  it("labels the empty select entries with what they mean", () => {
    expect(emptyLabels({}, defaults, ai)).toEqual({
      harness: "(pipeline default: cheap)",
      model: "(pipeline default: sonnet-api)",
    });
    expect(emptyLabels({ harness: "claude-code" }, defaults, ai).model).toBe(
      "(harness default: opus)",
    );
    expect(emptyLabels({ harness: "aider" }, null, ai).model).toBe("(harness default)");
  });
});

describe("compatible models", () => {
  it("uses the server's list per harness", () => {
    expect(compatibleModels(ai, "claude-code")).toEqual(["opus", "sonnet-api"]);
    expect(compatibleModels(ai, "codex")).toEqual(["free-llama"]);
    expect(compatibleModels(ai, "nope")).toEqual([]);
    expect(compatibleModels(null, "builtin")).toEqual([]);
  });

  it("lists the model picker for the node's or the default harness, else builtin", () => {
    expect(pickerHarness({ harness: "codex" }, { harness: "cheap" })).toBe("codex");
    expect(pickerHarness({}, { harness: "cheap" })).toBe("cheap");
    expect(pickerHarness({}, null)).toBe("builtin");
  });

  it("computes the same list per kind in the browser (unsaved harness forms)", () => {
    for (const a of ai.harnesses) {
      expect(compatibleForKind(ai, a.kind)).toEqual(ai.compatible_models[a.name]);
    }
    // claude-code: Anthropic only; a subscription login only for claude-code
    expect(compatibleForKind(ai, "claude-code")).not.toContain("free-llama");
    expect(compatibleForKind(ai, "builtin")).not.toContain("opus");
    expect(compatibleForKind(ai, "nope")).toEqual([]);
  });
});

describe("field edits", () => {
  it("harness and runner are mutually exclusive", () => {
    const f = { type: "agent", runner: "claude-code", prompt: "p" };
    const withAgent = setHarness(f, "cheap");
    expect(withAgent).toEqual({ type: "agent", prompt: "p", harness: "cheap" });
    expect(setRunner(withAgent, "python")).toEqual({ type: "agent", prompt: "p", runner: "python" });
    expect(setHarness(withAgent, undefined)).toEqual({ type: "agent", prompt: "p" });
    expect(setRunner(f, undefined)).toEqual({ type: "agent", prompt: "p" });
    expect(f.runner).toBe("claude-code"); // inputs are not mutated
  });

  it("sets and clears the model", () => {
    expect(setModel({ a: 1 }, "x")).toEqual({ a: 1, model: "x" });
    expect(setModel({ a: 1, model: "x" }, undefined)).toEqual({ a: 1 });
  });

  it("router mode switches the fields that apply", () => {
    const sw = { type: "router", switch: "review.state" };
    expect(routerMode(sw)).toBe("switch");
    const llm = setRouterMode(sw, "llm");
    expect(llm).toEqual({ type: "router", mode: "llm", prompt: "" });
    expect(routerMode(llm)).toBe("llm");
    const withModel = { ...llm, prompt: "Is it urgent?", model: "free-llama" };
    expect(setRouterMode(withModel, "llm")).toEqual(withModel);
    expect(setRouterMode(withModel, "switch")).toEqual({ type: "router", switch: "" });
    expect(setRouterMode({ type: "router", mode: "switch", switch: "a.b" }, "switch")).toEqual({
      type: "router",
      switch: "a.b",
    });
  });

  it("defaults: set, clear, and drop the section when empty", () => {
    let d = setDefault(null, "harness", "cheap");
    expect(d).toEqual({ harness: "cheap" });
    d = setDefault(d, "max_turns", 5);
    expect(d).toEqual({ harness: "cheap", max_turns: 5 });
    d = setDefault(d, "harness", "");
    expect(d).toEqual({ max_turns: 5 });
    expect(setDefault(d, "max_turns", undefined)).toBeNull();
  });
});

describe("canvas line", () => {
  const node = (fields: Record<string, unknown>, type = "agent"): ModelNode => ({
    id: "n",
    type,
    fields: { type, ...fields },
  });

  it("shows the effective harness and model, marking inherited parts", () => {
    expect(aiLine(node({ harness: "claude-code", model: "opus" }), null, ai)).toEqual({
      harness: "claude-code",
      model: "opus",
      harnessInherited: false,
      modelInherited: false,
    });
    expect(aiLine(node({}), { harness: "builtin", model: "free-llama" }, ai)).toEqual({
      harness: "builtin",
      model: "free-llama",
      harnessInherited: true,
      modelInherited: true,
    });
    expect(aiLine(node({ runner: "python" }), null, ai)).toBeNull();
    expect(aiLine(node({ uses: "x" }, "action"), null, ai)).toBeNull();
  });

  it("LLM routers show their model; the summary says llm router", () => {
    expect(aiLine(node({ mode: "llm", prompt: "p" }, "router"), { model: "free-llama" })).toEqual(
      { harness: null, model: "free-llama", harnessInherited: true, modelInherited: true },
    );
    const m = exampleModel();
    m.defaults = { harness: "cheap" };
    m.nodes.find((n) => n.id === "route")!.fields = { type: "router", mode: "llm", prompt: "p" };
    m.nodes.find((n) => n.id === "implement")!.fields = { type: "agent", prompt: "p" };
    const { nodes } = toFlow(m, ai);
    expect(nodes.find((n) => n.id === "route")!.data.summary).toBe("llm router");
    const impl = nodes.find((n) => n.id === "implement")!.data;
    expect(impl.summary).toBe("");
    expect(impl.ai).toMatchObject({ harness: "cheap", model: "free-llama", harnessInherited: true });
    expect(nodes.find((n) => n.id === "plan")!.data.summary).toBe("claude-code"); // a runner
  });
});

describe("settings helpers", () => {
  const tested: TestedModel[] = [
    { id: "meta-llama/llama-3.3-70b-instruct:free", name: "Llama 3.3", free: true, context_length: 1 },
    { id: "openai/gpt-4o", name: "GPT-4o", free: false, context_length: 1 },
    { id: "qwen/qwen-2.5:free", name: "Qwen", free: true, context_length: null },
  ];

  it("filters tested models by search text and free only", () => {
    expect(filterTested(tested, "", false)).toHaveLength(3);
    expect(filterTested(tested, "", true).map((m) => m.id)).toEqual([
      "meta-llama/llama-3.3-70b-instruct:free",
      "qwen/qwen-2.5:free",
    ]);
    expect(filterTested(tested, "LLAMA", false).map((m) => m.name)).toEqual(["Llama 3.3"]);
    expect(filterTested(tested, "gpt", true)).toEqual([]);
  });

  it("suggests a model name from an id", () => {
    expect(suggestModelName("meta-llama/llama-3.3-70b-instruct:free")).toBe(
      "llama-3.3-70b-instruct-free",
    );
    expect(suggestModelName("gpt-4o")).toBe("gpt-4o");
  });

  it("picks a free preset name; implicit entries can be taken over", () => {
    const sub = PROVIDER_PRESETS.find((p) => p.label === "Claude subscription")!;
    expect(presetName(sub, ai.providers)).toBe("anthropic"); // implicit
    const configured = ai.providers.map((p) => ({ ...p, implicit: false }));
    expect(presetName(sub, configured)).toBe("claude-subscription");
    const or = PROVIDER_PRESETS.find((p) => p.label === "OpenRouter")!;
    expect(or.body).toMatchObject({
      base_url: "https://openrouter.ai/api/v1",
      api_key_env: "OPENROUTER_API_KEY",
    });
    expect(presetName(or, configured)).toBe("openrouter-2");
    const ollama = PROVIDER_PRESETS.find((p) => p.label === "Ollama")!;
    expect(ollama.body.api_key_env).toBeUndefined();
  });

  it("builds PUT bodies without empties", () => {
    expect(
      formBody(
        {
          name: "x",
          implicit: true,
          builtin: true, // display-only fields from GET /settings
          ready: false,
          not_ready: "OPENROUTER_API_KEY isn't set",
          provider: " openrouter ",
          description: "",
          headers: {},
          extra_args: [],
          input_usd_per_mtok: "0",
          temperature: "",
          max_tokens: "abc",
        },
        ["input_usd_per_mtok", "temperature", "max_tokens"],
      ),
    ).toEqual({ provider: "openrouter", input_usd_per_mtok: 0 });
  });

  it("knows which issues are about the harness/model choice", () => {
    const issue = (over: Partial<Issue>): Issue => ({
      path: "nodes.plan",
      message: "m",
      line: null,
      col: null,
      hint: null,
      node_id: "plan",
      field: null,
      edge_index: null,
      edge_indexes: [],
      ...over,
    });
    expect(
      isAiIssue(
        issue({
          message: "harness kind 'claude-code' can't use model 'free-llama'",
          hint: "pick from Settings → Harnesses / Models",
        }),
      ),
    ).toBe(true);
    expect(isAiIssue(issue({ field: "model" }))).toBe(true);
    expect(isAiIssue(issue({ message: "llm router has no model" }))).toBe(true);
    expect(isAiIssue(issue({ field: "prompt", message: "missing" }))).toBe(false);
  });
});

describe("secret input", () => {
  it("is cleared after a successful save and the value is sent once", async () => {
    const put = vi.fn(async () => ({ keys: [{ name: "K", set: true, source: "secrets file" }] }));
    const r = await submitSecret("K", "sk-test-123", put);
    expect(put).toHaveBeenCalledWith("K", "sk-test-123");
    expect(put).toHaveBeenCalledTimes(1);
    expect(r.draft).toBe("");
    expect(r.keys?.[0].set).toBe(true);
    expect(JSON.stringify(r)).not.toContain("sk-test-123");
  });

  it("keeps the draft only when the save failed, and never sends an empty value", async () => {
    const failing = vi.fn(async () => {
      throw new Error("bad name");
    });
    const r = await submitSecret("K", "v", failing);
    expect(r).toEqual({ draft: "v", error: "bad name" });
    const put = vi.fn();
    expect(await submitSecret("K", "  ", put)).toEqual({ draft: "", error: "enter a value" });
    expect(put).not.toHaveBeenCalled();
  });
});

describe("Chat uses: models follow the harness", () => {
  const compatible = { "claude-code": ["opus", "sonnet"], codex: ["gpt-5"], builtin: ["opus", "gpt-5", "llama"] };

  it("lists only the harness's supported models", () => {
    expect(chatModelOptions(compatible, "claude-code", "").map((o) => o.value)).toEqual(["opus", "sonnet"]);
    expect(chatModelOptions(compatible, "codex", "").map((o) => o.value)).toEqual(["gpt-5"]);
    expect(chatModelOptions(compatible, "unknown", "")).toEqual([]);
  });

  it("keeps a saved model the harness can't use visible and marked", () => {
    const opts = chatModelOptions(compatible, "claude-code", "gpt-5");
    expect(opts[0]).toEqual({ value: "gpt-5", label: "gpt-5 (not supported by claude-code)" });
    expect(opts.slice(1).map((o) => o.value)).toEqual(["opus", "sonnet"]);
  });

  it("drops the model when the new harness doesn't support it", () => {
    expect(modelAfterHarnessChange(compatible, "codex", "opus")).toBe("");
    expect(modelAfterHarnessChange(compatible, "builtin", "opus")).toBe("opus");
    expect(modelAfterHarnessChange(compatible, "claude-code", "")).toBe("");
  });
});

describe("model source checklist", () => {
  const m = (id: string) => ({ id, name: id, free: false, context_length: null });
  it("toggles one model and selects the shown ones that aren't added", () => {
    let sel = toggled(new Set(), "a");
    expect([...sel]).toEqual(["a"]);
    sel = toggled(sel, "a");
    expect(sel.size).toBe(0);
    const shown = [m("a"), m("b"), m("c")];
    expect([...selectShown(new Set(["z"]), shown, new Set(["b"]))].sort()).toEqual(["a", "c", "z"]);
  });
});

describe("Chat panel: the default model entry", () => {
  it("says what runs when no model is picked", () => {
    const saved = { harness: "claude-code", model: "opus" };
    expect(chatDefaultModelLabel(ai, "claude-code", saved)).toBe("default: opus");
    expect(chatDefaultModelLabel(ai, "cheap", null)).toBe("default: free-llama");
    // the built-in harness needs a model; a profile without one says so
    expect(chatDefaultModelLabel(ai, "builtin", null)).toBe("pick a model");
    expect(chatDefaultModelLabel(ai, "codex", null)).toBe("codex's own model");
  });

  it("marks a default model whose source isn't ready", () => {
    const notReady = { ...ai, compatible_models: { ...ai.compatible_models, cheap: [] } };
    expect(chatDefaultModelLabel(notReady, "cheap", null)).toBe("default: free-llama (not ready)");
  });

  it("lists only the models the harness can use: builtin never gets the Claude login's", () => {
    expect(chatModelOptions(ai.compatible_models, "builtin", "").map((o) => o.value)).not.toContain("opus");
    expect(chatModelOptions(ai.compatible_models, "claude-code", "").map((o) => o.value)).toContain("opus");
  });
});
