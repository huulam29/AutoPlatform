// The Chat panel's History view: past conversations (kept on the server), newest first. Ones
// with no activity for 7 days sit under Archived: hidden from the list, still searchable and
// openable, with Unarchive. Nothing here deletes a conversation.
import { useEffect, useState } from "react";
import { ApiError, api } from "../api";
import {
  ARCHIVE_DAYS,
  historyGroups,
  lastActive,
  type ConversationSummary,
} from "../lib/conversations";
import { Icon } from "./Icon";

export function ChatHistory({
  current,
  refresh,
  onOpen,
  onNew,
  onArchived,
}: {
  current: string;
  /** bumped when a Chat card archived or unarchived one */
  refresh: number;
  onOpen: (id: string) => void;
  onNew: () => void;
  onArchived: (id: string, archived: boolean) => void;
}) {
  const [q, setQ] = useState("");
  const [list, setList] = useState<ConversationSummary[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [showArchived, setShowArchived] = useState(false);
  const [tick, setTick] = useState(0);

  useEffect(() => {
    let stop = false;
    const t = setTimeout(
      () => {
        api
          .conversations(q)
          .then((r) => {
            if (stop) return;
            setList(r.conversations);
            setError(null);
          })
          .catch((e) => !stop && setError(e instanceof ApiError ? e.message : String(e)));
      },
      q ? 200 : 0,
    );
    return () => {
      stop = true;
      clearTimeout(t);
    };
  }, [q, refresh, tick]);

  const toggle = async (c: ConversationSummary) => {
    try {
      await api.archiveConversation(c.id, !c.archived);
      onArchived(c.id, !c.archived);
      setTick((n) => n + 1);
    } catch (e) {
      setError(e instanceof ApiError ? e.message : String(e));
    }
  };

  const now = Date.now() / 1000;
  const { recent, archived } = historyGroups(list ?? [], now);
  const searching = q.trim() !== "";
  const archivedOpen = showArchived || searching;
  const row = (c: ConversationSummary) => (
    <li key={c.id} className={`chat-history-row ${c.id === current ? "current" : ""}`}>
      <button
        type="button"
        className="chat-history-open"
        onClick={() => onOpen(c.id)}
        aria-current={c.id === current ? "true" : undefined}
      >
        <span className="chat-history-title">{c.title}</span>
        <span className="small muted">
          {lastActive(c.updated_at, now)} · {c.messages} message{c.messages === 1 ? "" : "s"}
          {c.id === current ? " · open" : ""}
        </span>
      </button>
      <button type="button" className="link small" onClick={() => void toggle(c)}>
        {c.archived ? "Unarchive" : "Archive"}
      </button>
    </li>
  );

  return (
    <div className="chat-history">
      <div className="chat-history-head">
        <input
          type="search"
          value={q}
          placeholder="Search chats"
          aria-label="search chats"
          onChange={(e) => setQ(e.target.value)}
        />
        <button type="button" onClick={onNew}>
          New chat
        </button>
      </div>
      {error && (
        <div className="issue" role="alert">
          {error}
        </div>
      )}
      {list === null && !error && <p className="muted small">Loading…</p>}
      {list !== null && (
        <>
          <ul className="chat-history-list" aria-label="recent chats">
            {recent.map(row)}
          </ul>
          {!recent.length && (
            <p className="muted small">
              {searching ? "No recent chat matches." : "No recent chats. Older ones are under Archived."}
            </p>
          )}
          <button
            type="button"
            className="chat-history-section"
            aria-expanded={archivedOpen}
            disabled={searching}
            onClick={() => setShowArchived((v) => !v)}
          >
            <Icon name={archivedOpen ? "chevronDown" : "chevronRight"} />
            Archived ({archived.length})
          </button>
          {archivedOpen && (
            <>
              <p className="help">
                No activity for more than {ARCHIVE_DAYS} days. Open one to read or continue it;
                nothing is deleted.
              </p>
              <ul className="chat-history-list" aria-label="archived chats">
                {archived.map(row)}
              </ul>
              {!archived.length && <p className="muted small">{searching ? "No archived chat matches." : "None yet."}</p>}
            </>
          )}
        </>
      )}
    </div>
  );
}
