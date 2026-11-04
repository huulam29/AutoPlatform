// Sign-in (spec 006 US2): a 401 never opens a browser prompt; it forgets the token and asks
// App for the sign-in screen. A token is stored only after the engine accepts it.
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { api, AUTH_NEEDED_EVENT, checkToken, hasToken, setToken } from "./api";

let store: Record<string, string>;
let events: string[];
let prompted: number;

function respond(status: number, body: unknown) {
  return vi.fn(async () => ({
    ok: status < 400,
    status,
    statusText: String(status),
    text: async () => JSON.stringify(body),
    json: async () => body,
  }));
}

beforeEach(() => {
  store = {};
  events = [];
  prompted = 0;
  vi.stubGlobal("localStorage", {
    getItem: (k: string) => store[k] ?? null,
    setItem: (k: string, v: string) => {
      store[k] = v;
    },
    removeItem: (k: string) => {
      delete store[k];
    },
  });
  vi.stubGlobal("window", {
    dispatchEvent: (e: Event) => {
      events.push(e.type);
      return true;
    },
    prompt: () => {
      prompted += 1;
      return "typed";
    },
  });
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("sign-in", () => {
  it("a 401 forgets the token and fires AUTH_NEEDED_EVENT, with no prompt", async () => {
    setToken("old-token");
    vi.stubGlobal("fetch", respond(401, { detail: "missing or bad bearer token" }));
    await expect(api.status()).rejects.toMatchObject({ status: 401 });
    expect(events).toEqual([AUTH_NEEDED_EVENT]);
    expect(prompted).toBe(0);
    expect(hasToken()).toBe(false);
  });

  it("stores a token only when the engine accepts it, trimmed", async () => {
    vi.stubGlobal("fetch", respond(200, { token_required: true, ok: false }));
    expect((await checkToken("wrong")).ok).toBe(false);
    expect(hasToken()).toBe(false);
    const ok = respond(200, { token_required: true, ok: true });
    vi.stubGlobal("fetch", ok);
    expect((await checkToken("  right-token\n")).ok).toBe(true);
    expect(store["agentline.token"]).toBe("right-token");
    expect(ok.mock.calls[0]).toEqual(["/api/auth/check", { headers: { Authorization: "Bearer right-token" } }]);
  });

  it("sends the stored token with every call", async () => {
    setToken("t-1");
    const f = respond(200, { version: "1", pipelines: [] });
    vi.stubGlobal("fetch", f);
    await api.status();
    const init = (f.mock.calls[0] as unknown as [string, { headers: Record<string, string> }])[1];
    expect(init.headers.Authorization).toBe("Bearer t-1");
  });
});
