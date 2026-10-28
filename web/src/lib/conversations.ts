// The Chat's past conversations (api/assistant.py /assistant/conversations, kept on the server
// by agentline.conversations): the History view's pure helpers (tested in
// conversations.test.ts). The archive rule matches conversations.is_archived on the server.

export const ARCHIVE_DAYS = 7;
const DAY = 86_400;

export interface ConversationSummary {
  id: string;
  title: string;
  created_at: number;
  /** the last message (seconds) */
  updated_at: number;
  /** unarchived at: counts as activity */
  kept_at: number | null;
  /** archived by hand at */
  archived_at: number | null;
  messages: number;
  archived: boolean;
}

/** Archived by hand (and quiet since), or no activity for ARCHIVE_DAYS. */
export function isArchived(
  c: Pick<ConversationSummary, "updated_at" | "kept_at" | "archived_at">,
  now: number,
): boolean {
  const active = Math.max(c.updated_at || 0, c.kept_at || 0);
  if (c.archived_at != null && c.archived_at >= active) return true;
  return now - active > ARCHIVE_DAYS * DAY;
}

/** The History list: recent and archived, each newest message first. */
export function historyGroups(
  list: ConversationSummary[],
  now: number,
): { recent: ConversationSummary[]; archived: ConversationSummary[] } {
  const sorted = [...list].sort((a, b) => (b.updated_at || 0) - (a.updated_at || 0));
  return {
    recent: sorted.filter((c) => !isArchived(c, now)),
    archived: sorted.filter((c) => isArchived(c, now)),
  };
}

/** "just now", "5 min ago", "3 h ago", "yesterday", "4 days ago", else the date. */
export function lastActive(ts: number, now: number): string {
  const s = Math.max(0, now - ts);
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.floor(s / 60)} min ago`;
  if (s < DAY) return `${Math.floor(s / 3600)} h ago`;
  const days = Math.floor(s / DAY);
  if (days === 1) return "yesterday";
  if (days < 30) return `${days} days ago`;
  return new Date(ts * 1000).toLocaleDateString();
}

/** A new conversation's id (the first save creates it on the server). Not crypto.randomUUID:
 * the Mac app's page isn't a secure context. */
export function newConversationId(now = Date.now(), rand = Math.random): string {
  return `c-${now.toString(36)}${Math.floor(rand() * 36 ** 6).toString(36).padStart(6, "0")}`;
}

/* ------------------------------------------------------------------ storage */

const CURRENT_KEY = "agentline.assistant.conversation";

/** The open conversation's id; "" = a new chat not saved yet; null = none remembered (open
 * the newest recent one). */
export function loadCurrent(storage: () => Storage | null): string | null {
  try {
    return storage()?.getItem(CURRENT_KEY) ?? null;
  } catch {
    return null;
  }
}

export function saveCurrent(storage: () => Storage | null, id: string): void {
  try {
    storage()?.setItem(CURRENT_KEY, id);
  } catch {
    /* ignore */
  }
}
