import { describe, expect, it } from "vitest";
import { engineAnswers, HealthTracker, HEALTH_EVERY_MS, MISSES_FOR_DOWN } from "./engine";
import { badgedIcon, BASE_TITLE, titleFor } from "./badge";
import { crashToShow } from "../components/EngineBanner";
import type { IncidentSummary } from "../api";

describe("engine health", () => {
  it("is down only after two misses in a row, inside 10 s", () => {
    expect(HEALTH_EVERY_MS * (MISSES_FOR_DOWN + 1)).toBeLessThanOrEqual(10_000);
    const t = new HealthTracker();
    expect(t.next(false)).toBe("up");
    expect(t.next(true)).toBe("up");
    expect(t.next(false)).toBe("up");
    expect(t.next(false)).toBe("down");
    expect(t.next(true)).toBe("up");
  });

  it("a restart shows restarting until it answers again", () => {
    const t = new HealthTracker();
    t.restarting = true;
    expect(t.next(false)).toBe("restarting");
    expect(t.next(false)).toBe("restarting");
    expect(t.next(true)).toBe("up");
    expect(t.restarting).toBe(false);
  });

  it("a network error or a 5xx counts as no answer", async () => {
    const ok = (async () => ({ ok: true })) as unknown as typeof fetch;
    const bad = (async () => ({ ok: false })) as unknown as typeof fetch;
    const dead = (async () => {
      throw new TypeError("fetch failed");
    }) as unknown as typeof fetch;
    expect(await engineAnswers(ok)).toBe(true);
    expect(await engineAnswers(bad)).toBe(false);
    expect(await engineAnswers(dead)).toBe(false);
  });
});

describe("badges", () => {
  it("the title shows the count, nothing at zero", () => {
    expect(titleFor(0)).toBe(BASE_TITLE);
    expect(titleFor(3)).toBe(`(3) ${BASE_TITLE}`);
  });
  it("the icon carries the count", () => {
    expect(decodeURIComponent(badgedIcon(4))).toContain(">4</text>");
    expect(decodeURIComponent(badgedIcon(250))).toContain(">99+</text>");
  });
});

describe("crash banner", () => {
  const i = (id: string, kind: string, status: string, last_seen: number) =>
    ({ id, kind, status, last_seen, source: "engine", title: id, count: 1, first_seen: 0 }) as IncidentSummary;
  it("shows the newest unseen unclean exit only", () => {
    expect(crashToShow([])).toBeNull();
    expect(crashToShow([i("a", "api_error", "new", 9)])).toBeNull();
    expect(crashToShow([i("a", "engine_unclean_exit", "seen", 9)])).toBeNull();
    expect(crashToShow([i("a", "engine_unclean_exit", "new", 1), i("b", "engine_unclean_exit", "new", 5)])?.id).toBe("b");
  });
});
