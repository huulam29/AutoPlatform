import { describe, expect, it } from "vitest";
import {
  actionTitle,
  autoApplicable,
  editorLink,
  lookedAt,
  loadChat,
  pipelineName,
  forStorage,
  clearOldChat,
  settingLines,
  toHistory,
  type AssistantAction,
  type ChatMessage,
} from "./assistant";

const reply = (actions: AssistantAction[]): ChatMessage => ({
  role: "assistant",
  content: "",
  reply: {
    message: "Here you go",
    choices: [],
    actions,
    harness: "claude-code",
    cost_usd: 0,
  },
});

describe("assistant helpers", () => {
  it("titles actions", () => {
    expect(
      actionTitle({
        type: "setting",
        section: "models",
        name: "qwen",
        body: {},
        issues: [],
      }),
    ).toBe("Set model qwen");
    expect(actionTitle({ type: "key", name: "K", issues: [] })).toBe(
      "Set key K",
    );
    expect(
      actionTitle({
        type: "pipeline",
        path: "a/pipeline.yaml",
        yaml: "",
        exists: true,
        issues: [],
      }),
    ).toBe("Update pipeline a/pipeline.yaml");
    expect(pipelineName("a/pipeline.yaml")).toBe("a/pipeline");
    expect(
      settingLines({ provider: "openrouter", empty: "", headers: { a: 1 } }),
    ).toEqual(["provider = openrouter", 'headers = {"a":1}']);
  });

  it("tells the Chat what was applied", () => {
    const m = reply([
      {
        type: "setting",
        section: "models",
        name: "qwen",
        body: {},
        issues: [],
      },
      { type: "key", name: "K", issues: [] },
      { type: "fetch_skills", issues: [] },
    ]);
    m.states = {
      0: { state: "applied" },
      2: { state: "failed", error: "offline" },
    };
    const h = toHistory([{ role: "user", content: "hi" }, m]);
    expect(h[0]).toEqual({ role: "user", content: "hi" });
    // attached images travel with the message (they used to be dropped here)
    const withImage = toHistory([{ role: "user", content: "build this", attachments: ["abc.png"] }]);
    expect(withImage[0]).toEqual({ role: "user", content: "build this", attachments: ["abc.png"] });
    expect(h[1].content).toBe(
      "Here you go\n[proposals: Set model qwen: applied; Set key K: not applied yet; " +
        "Download the skill library: failed (offline)]",
    );
  });

  it("apply-all skips keys, invalid and applied actions", () => {
    const m = reply([
      { type: "setting", section: "models", name: "a", body: {}, issues: [] },
      { type: "key", name: "K", issues: [] },
      {
        type: "setting",
        section: "models",
        name: "b",
        body: {},
        issues: ["bad"],
      },
      { type: "fetch_skills", issues: [] },
    ]);
    m.states = { 3: { state: "applied" } };
    expect(autoApplicable(m)).toEqual([0]);
  });

  it("keeps the chat without in-flight states; reads and clears the old browser copy", () => {
    const m = reply([{ type: "fetch_skills", issues: [] }]);
    m.states = { 0: { state: "applying" }, 1: { state: "applied" } };
    expect(forStorage([m])[0].states).toEqual({ 0: { state: "pending" }, 1: { state: "applied" } });
    const data: Record<string, string> = { "agentline.assistant.chat": JSON.stringify([m]) };
    const storage = () =>
      ({
        getItem: (k: string) => data[k] ?? null,
        removeItem: (k: string) => {
          delete data[k];
        },
      }) as unknown as Storage;
    expect(loadChat(storage)).toHaveLength(1);
    clearOldChat(storage);
    expect(loadChat(storage)).toEqual([]);
    const broken = () => {
      throw new Error("blocked");
    };
    expect(loadChat(broken)).toEqual([]);
    expect(() => clearOldChat(broken)).not.toThrow();
  });

  it("links the editor and the assistant", () => {
    const seen: string[] = [];
    const off = editorLink.onChanged((n) => seen.push(n));
    editorLink.publish({ name: "p", source: "x", dirty: false });
    expect(editorLink.current()?.name).toBe("p");
    editorLink.notifyChanged("p");
    off();
    editorLink.notifyChanged("p");
    expect(seen).toEqual(["p"]);
    editorLink.publish(null);
  });
});

describe("image transcripts", () => {
  it("say who read the image, and when the user corrected it", async () => {
    const { transcriptByline } = await import("./assistant");
    expect(transcriptByline({ by: "haiku-api", edited: false })).toBe("read by haiku-api");
    expect(transcriptByline({ by: "claude-code", edited: false, cached: true })).toBe("read by claude-code (saved from an earlier turn)");
    expect(transcriptByline({ by: "you", edited: true })).toBe("corrected by you");
  });
});

describe("built-in harness notes", () => {
  it("lists the lookups, or nothing", () => {
    const base = { message: "m", choices: [], actions: [], harness: "builtin", cost_usd: 0 };
    expect(lookedAt({ ...base, steps: ["read a.md", "run r1"] })).toBe("Looked at: read a.md, run r1");
    expect(lookedAt({ ...base, steps: [] })).toBeNull();
    expect(lookedAt(undefined)).toBeNull();
  });
});
