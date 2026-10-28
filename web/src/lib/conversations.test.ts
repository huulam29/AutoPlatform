import { describe, expect, it } from "vitest";
import {
  ARCHIVE_DAYS,
  historyGroups,
  isArchived,
  lastActive,
  loadCurrent,
  newConversationId,
  saveCurrent,
  type ConversationSummary,
} from "./conversations";

const DAY = 86_400;
const T0 = 1_700_000_000;
const conv = (id: string, updated_at: number, more: Partial<ConversationSummary> = {}): ConversationSummary => ({
  id,
  title: id,
  created_at: updated_at,
  updated_at,
  kept_at: null,
  archived_at: null,
  messages: 2,
  archived: false,
  ...more,
});

describe("chat history", () => {
  it("archives after 7 days without activity, like the server", () => {
    expect(ARCHIVE_DAYS).toBe(7);
    const c = conv("a", T0);
    expect(isArchived(c, T0 + 7 * DAY)).toBe(false);
    expect(isArchived(c, T0 + 7 * DAY + 1)).toBe(true);
    // unarchived: active for another 7 days from then
    expect(isArchived({ ...c, kept_at: T0 + 6 * DAY }, T0 + 8 * DAY)).toBe(false);
    // archived by hand, and a message after that brings it back
    expect(isArchived({ ...c, archived_at: T0 + 1 }, T0 + 2)).toBe(true);
    expect(isArchived({ ...c, archived_at: T0 - 1 }, T0 + 2)).toBe(false);
  });

  it("splits recent and archived, each newest first", () => {
    const now = T0 + 30 * DAY;
    const list = [
      conv("old", T0),
      conv("new", now - 60),
      conv("older", T0 - DAY),
      conv("mid", now - 3 * DAY),
      conv("hand", now - DAY, { archived_at: now - 10 }),
    ];
    const g = historyGroups(list, now);
    expect(g.recent.map((c) => c.id)).toEqual(["new", "mid"]);
    expect(g.archived.map((c) => c.id)).toEqual(["hand", "old", "older"]);
    expect(list[0].id).toBe("old"); // not sorted in place
  });

  it("says when the last message was", () => {
    expect(lastActive(T0, T0 + 5)).toBe("just now");
    expect(lastActive(T0, T0 + 5 * 60)).toBe("5 min ago");
    expect(lastActive(T0, T0 + 3 * 3600)).toBe("3 h ago");
    expect(lastActive(T0, T0 + DAY + 5)).toBe("yesterday");
    expect(lastActive(T0, T0 + 4 * DAY)).toBe("4 days ago");
    expect(lastActive(T0, T0 + 40 * DAY)).toBe(new Date(T0 * 1000).toLocaleDateString());
  });

  it("makes ids the server accepts and remembers the open one", () => {
    const id = newConversationId(T0 * 1000, () => 0.5);
    expect(id).toMatch(/^[A-Za-z0-9_-]{6,64}$/);
    expect(newConversationId(T0 * 1000, () => 0)).not.toBe(id);
    const data: Record<string, string> = {};
    const storage = () =>
      ({
        getItem: (k: string) => data[k] ?? null,
        setItem: (k: string, v: string) => {
          data[k] = v;
        },
      }) as unknown as Storage;
    expect(loadCurrent(storage)).toBeNull();
    saveCurrent(storage, id);
    expect(loadCurrent(storage)).toBe(id);
    saveCurrent(storage, "");
    expect(loadCurrent(storage)).toBe("");
    const broken = () => {
      throw new Error("blocked");
    };
    expect(loadCurrent(broken)).toBeNull();
    expect(() => saveCurrent(broken, id)).not.toThrow();
  });
});
