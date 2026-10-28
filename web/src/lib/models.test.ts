import { describe, expect, it } from "vitest";
import { harnessStatus, isFree, matches, modelGroups, needsAttention, sourceTitle } from "./models";
import type { ModelInfo, ProviderInfo } from "../types";

const prov = (name: string, connected: boolean, implicit = true, user_set = false): ProviderInfo => ({
  name, type: "openai-compatible", base_url: null, api_key_env: null, auth: "api-key", headers: {},
  description: "", implicit, connected, user_set,
});
const model = (name: string, provider: string, id: string, free = false): ModelInfo => ({
  name, provider, model: id, input_usd_per_mtok: free ? 0 : null, output_usd_per_mtok: free ? 0 : null,
  max_tokens: null, temperature: null, description: "",
});

const providers = [prov("openrouter", true), prov("openai", false), prov("homelab", true, false, true), prov("gone", false, false, true)];
const models = [
  model("free", "openrouter", "openrouter/free", true),
  model("auto", "openrouter", "openrouter/auto"),
  model("gpt", "openai", "gpt-6.1-sol"),
  model("qwen", "homelab", "qwen/qwen3"),
];

describe("Models page", () => {
  it("shows only connected sources, the ones you set up first", () => {
    const g = modelGroups({ providers, models });
    expect(g.map((x) => x.provider.name)).toEqual(["homelab", "openrouter"]);
    expect(g[1].models.map((m) => m.name)).toEqual(["free", "auto"]);
  });

  it("lists sources you set up that stopped working under Needs attention", () => {
    expect(needsAttention({ providers }).map((p) => p.name)).toEqual(["gone"]);
  });

  it("searches names, ids and sources; free finds free models", () => {
    expect(modelGroups({ providers, models }, "free").flatMap((g) => g.models.map((m) => m.name))).toEqual(["free"]);
    expect(modelGroups({ providers, models }, "qwen3").map((g) => g.provider.name)).toEqual(["homelab"]);
    expect(modelGroups({ providers, models }, "openrouter")[0].models).toHaveLength(2); // the source matches
    expect(matches(models[1], "")).toBe(true);
    expect(isFree(model("x", "p", "a/b:free"))).toBe(true);
  });

  it("names the built-in sources", () => {
    expect(sourceTitle(prov("claude", true))).toBe("Claude subscription");
    expect(sourceTitle(prov("homelab", true))).toBe("homelab");
  });
});

describe("harness status", () => {
  const data = { kinds: { builtin: { needs_model: true }, "claude-code": { needs_model: false } }, compatible_models: { builtin: [], cc: [] } } as never;
  it("says what's missing", () => {
    expect(harnessStatus({ name: "cc", kind: "claude-code", installed: false }, data).word).toBe("Program not found");
    expect(harnessStatus({ name: "builtin", kind: "builtin", installed: null }, data).word).toBe("No model ready");
    expect(harnessStatus({ name: "cc", kind: "claude-code", installed: true }, data)).toEqual({ word: "Ready", ok: true });
  });
});
