import { describe, expect, it } from "vitest";
import {
  draftFromServer,
  draftToSave,
  emptyDraft,
  importItems,
  joinArgs,
  looksSecret,
  missingKeys,
  newRow,
  splitArgs,
  stepProblems,
  suggestKeyName,
  usedByText,
  type McpDraft,
} from "./mcp";

const gh = (): McpDraft => ({
  ...emptyDraft(),
  name: "github",
  command: "npx",
  args: '-y @modelcontextprotocol/server-github "two words"',
  env: [
    { name: "GITHUB_PERSONAL_ACCESS_TOKEN", value: "ghp_secretvalue123", secret: true, keyName: "GITHUB_PERSONAL_ACCESS_TOKEN", prefix: "" },
    { name: "LOG_LEVEL", value: "info", secret: false, keyName: "", prefix: "" },
  ],
});

describe("MCP draft", () => {
  it("never writes a secret value into the table", () => {
    const { name, body, secrets } = draftToSave(gh());
    expect(name).toBe("github");
    expect(body).toEqual({
      transport: "stdio",
      command: "npx",
      args: ["-y", "@modelcontextprotocol/server-github", "two words"],
      env: { GITHUB_PERSONAL_ACCESS_TOKEN: "${GITHUB_PERSONAL_ACCESS_TOKEN}", LOG_LEVEL: "info" },
    });
    expect(JSON.stringify(body)).not.toContain("ghp_secret");
    expect(secrets).toEqual({ GITHUB_PERSONAL_ACCESS_TOKEN: "ghp_secretvalue123" });
  });

  it("keeps a header's Bearer prefix and only the transport's fields", () => {
    const d: McpDraft = {
      ...emptyDraft(),
      name: "linear",
      transport: "http",
      url: "https://mcp.linear.app/mcp",
      command: "ignored",
      headers: [{ ...newRow("linear", "headers", "Authorization"), value: "lin_123" }],
      enabled: false,
    };
    expect(d.headers[0]).toMatchObject({ secret: true, keyName: "LINEAR_TOKEN", prefix: "Bearer " });
    expect(draftToSave(d)).toEqual({
      name: "linear",
      body: { transport: "http", url: "https://mcp.linear.app/mcp", headers: { Authorization: "Bearer ${LINEAR_TOKEN}" }, enabled: false },
      secrets: { LINEAR_TOKEN: "lin_123" },
    });
    expect("probe_safe" in draftToSave(d).body).toBe(false); // off: not written
  });

  it("round-trips a saved server; its secrets come back empty (= keep)", () => {
    const d = draftFromServer({
      name: "linear",
      transport: "http",
      url: "https://x/mcp",
      headers: { Authorization: "Bearer ${LINEAR_TOKEN}", "X-Team": "eng" },
      enabled: false,
    });
    expect(d.headers).toEqual([
      { name: "Authorization", value: "", secret: true, keyName: "LINEAR_TOKEN", prefix: "Bearer " },
      { name: "X-Team", value: "eng", secret: false, keyName: "", prefix: "" },
    ]);
    expect(d.enabled).toBe(false);
    expect(d.probeSafe).toBe(false);
    expect(draftToSave({ ...d, probeSafe: true }).body.probe_safe).toBe(true);
    expect(draftFromServer({ name: "docs", transport: "stdio", command: "x", probe_safe: true }).probeSafe).toBe(true);
    const again = draftToSave(d);
    expect(again.body.headers).toEqual({ Authorization: "Bearer ${LINEAR_TOKEN}", "X-Team": "eng" });
    expect(again.secrets).toEqual({});
    expect(missingKeys(d, new Set())).toEqual(["LINEAR_TOKEN"]);
    expect(missingKeys(d, new Set(["LINEAR_TOKEN"]))).toEqual([]);
  });

  it("checks each step", () => {
    expect(stepProblems(emptyDraft(), 0)).toEqual([]);
    expect(stepProblems(emptyDraft(), 1).join(" ")).toMatch(/Name.*Command/);
    expect(stepProblems({ ...gh(), name: "github" }, 1, ["github"])).toEqual(["A server named github already exists"]);
    expect(stepProblems({ ...gh(), name: "github" }, 1, ["github"], true)).toEqual([]);
    expect(stepProblems({ ...emptyDraft(), name: "x", transport: "http", url: "ftp://x" }, 1)).toEqual(["URL: http(s)://…"]);
    const plain = { ...gh(), env: [{ name: "API_KEY", value: "abc", secret: false, keyName: "", prefix: "" }] };
    expect(stepProblems(plain, 2)[0]).toMatch(/looks like a secret/);
    const badKey = { ...gh(), env: [{ name: "T", value: "", secret: true, keyName: "bad key", prefix: "" }] };
    expect(stepProblems(badKey, 2)[0]).toMatch(/key name/);
  });

  it("names keys and spots secrets", () => {
    expect(suggestKeyName("my-srv", "Authorization", "headers")).toBe("MY_SRV_TOKEN");
    expect(suggestKeyName("my-srv", "X-Api-Key", "headers")).toBe("MY_SRV_X_API_KEY");
    expect(suggestKeyName("s", "gh_token", "env")).toBe("GH_TOKEN");
    expect(looksSecret("PATH", "/usr/bin", "env")).toBe(false);
    expect(looksSecret("GH_PAT", "x", "env")).toBe(true);
    expect(looksSecret("X", "sk-abcdefghijkl", "env")).toBe(true);
    expect(looksSecret("X", "${KEY}", "headers")).toBe(false);
  });

  it("splits and joins arguments", () => {
    expect(splitArgs(` -y  pkg "a b" 'c d' ""`)).toEqual(["-y", "pkg", "a b", "c d", ""]);
    expect(splitArgs(joinArgs(["-y", "a b", ""]))).toEqual(["-y", "a b", ""]);
  });

  it("lists the nodes that use a server and the picked imports", () => {
    expect(usedByText([{ pipeline: "dev", node: "plan" }, { pipeline: "dev", node: "build" }, { pipeline: "ops", node: "x" }])).toBe(
      "dev: plan, build · ops: x",
    );
    expect(usedByText(undefined)).toBe("");
    expect(importItems({ a: "gh ", b: undefined })).toEqual([{ id: "a", name: "gh" }]);
  });
});
