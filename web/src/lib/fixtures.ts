// The ticket-to-eod example as /api returns it (trimmed fields).
import type { AiSummary, Model } from "../types";

/** `agents.summary()` for a config with OpenRouter, a Claude subscription and three models. */
export function exampleAi(): AiSummary {
  const kind = (provider_types: string[], needs_model = false, subscription = false) => ({
    runner: "x",
    provider_types,
    needs_model,
    subscription,
    default_bin: null,
    description: "",
  });
  const provider = (name: string, type: string, auth = "api-key", implicit = false) => ({
    name,
    type,
    base_url: null,
    api_key_env: null,
    auth,
    headers: {},
    description: "",
    implicit,
  });
  const model = (name: string, provider: string, id: string) => ({
    name,
    provider,
    model: id,
    input_usd_per_mtok: null,
    output_usd_per_mtok: null,
    max_tokens: null,
    temperature: null,
    description: "",
  });
  const harness = (name: string, kind: string, model: string | null, implicit: boolean) => ({
    name,
    kind,
    bin: null,
    model,
    extra_args: [],
    env: null,
    description: "",
    implicit,
  });
  return {
    kinds: {
      "claude-code": kind(["anthropic"], false, true),
      codex: kind(["openai-compatible"]),
      aider: kind(["anthropic", "openai-compatible"]),
      builtin: kind(["anthropic", "openai-compatible"], true),
    },
    providers: [
      provider("anthropic", "anthropic", "api-key", true),
      provider("claude-sub", "anthropic", "subscription"),
      provider("openrouter", "openai-compatible"),
    ],
    models: [
      model("opus", "claude-sub", "claude-opus-4-1"),
      model("sonnet-api", "anthropic", "claude-sonnet-4-5"),
      model("free-llama", "openrouter", "meta-llama/llama-3.3-70b-instruct:free"),
    ],
    harnesses: [
      harness("claude-code", "claude-code", "opus", false),
      harness("codex", "codex", null, true),
      harness("aider", "aider", null, true),
      harness("builtin", "builtin", null, true),
      harness("cheap", "builtin", "free-llama", false),
    ],
    // as the server computes it
    compatible_models: {
      "claude-code": ["opus", "sonnet-api"],
      codex: ["free-llama"],
      aider: ["sonnet-api", "free-llama"],
      builtin: ["sonnet-api", "free-llama"],
      cheap: ["sonnet-api", "free-llama"],
    },
  };
}

export function exampleModel(): Model {
  const n = (id: string, type: string, fields: Record<string, unknown> = {}) => ({
    id,
    type,
    fields: { type, ...fields },
  });
  return {
    name: "ticket-to-eod",
    version: 1,
    description: null,
    trigger: { type: "webhook", source: "jira", event: "issue_assigned" },
    budget_usd: 5,
    nodes: [
      n("read_ticket", "action", { uses: "jira.get_ticket", out: "ticket" }),
      n("plan", "agent", { runner: "claude-code", prompt: "prompts/plan.md", in: ["ticket"] }),
      n("implement", "agent", { runner: "claude-code", in: ["ticket", "plan", "review?"] }),
      n("open_pr", "action", { uses: "github.open_pr" }),
      n("await_review", "wait", { for: "github.review" }),
      n("route", "router", { switch: "review.state" }),
      n("approve", "gate", { prompt: "Deploy?" }),
      n("deploy", "action", { uses: "env.deploy" }),
      n("test", "action", { uses: "env.test" }),
    ],
    edges: [
      { src: "read_ticket", dst: "plan", chain_index: 0 },
      { src: "plan", dst: "implement", chain_index: 0 },
      { src: "implement", dst: "open_pr", chain_index: 0 },
      { src: "open_pr", dst: "await_review", chain_index: 0 },
      { src: "await_review", dst: "route", chain_index: 0 },
      {
        src: "route",
        dst: "implement",
        when: "changes_requested",
        max_loops: 3,
        on_exhausted: "needs-review",
        chain_index: 1,
      },
      { src: "route", dst: "approve", when: "approved", chain_index: 2 },
      { src: "approve", dst: "deploy", chain_index: 3 },
      { src: "deploy", dst: "test", chain_index: 3 },
    ],
    issues: [],
  };
}
