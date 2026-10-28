// A run's conversation with its chat start node (the gatekeeper): your messages, its answers,
// the pipeline's notes, and proposed actions that run only after a Confirm
// (src/agentline/runchat.py). The thread only grows; scrolled up, new lines show as a notice
// instead of moving what you read.
import { useEffect, useRef, useState } from "react";
import { ApiError, api } from "../api";
import type { RunCard, RunMessage } from "../types";
import { formatTime } from "../lib/runs";
import { Icon } from "./Icon";

const POLL_MS = 1500;

export function cardTitle(c: RunCard): string {
  const what = { approve: "Approve", reject: "Reject", retry: "Retry", cancel: "Cancel the run" }[c.type];
  return c.node ? `${what} ${c.node}` : c.type === "retry" ? "Retry what failed" : what;
}

export function RunChat({
  runId,
  agentName,
  onRunChanged,
}: {
  runId: string;
  agentName: string;
  onRunChanged?: () => void; // a confirmed card changed the run: refresh the run view
}) {
  const [messages, setMessages] = useState<RunMessage[]>([]);
  const [thinking, setThinking] = useState(false);
  const [draft, setDraft] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [unseen, setUnseen] = useState(0);
  const log = useRef<HTMLDivElement>(null);
  const atBottom = useRef(true);
  const seen = useRef(0);

  useEffect(() => {
    let live = true;
    let timer: ReturnType<typeof setTimeout> | undefined;
    setMessages([]);
    const tick = async () => {
      try {
        const r = await api.runMessages(runId, 0);
        if (!live) return;
        setMessages(r.messages);
        setThinking(r.thinking);
        setError(null);
      } catch (e) {
        if (live) setError((e as Error).message);
      }
      if (live) timer = setTimeout(tick, POLL_MS);
    };
    void tick();
    return () => {
      live = false;
      if (timer) clearTimeout(timer);
    };
  }, [runId]);

  // follow the end only while you're there; otherwise count what arrived below
  useEffect(() => {
    const el = log.current;
    const added = messages.length - seen.current;
    if (added <= 0) return;
    if (atBottom.current && el) {
      el.scrollTop = el.scrollHeight;
      seen.current = messages.length;
      setUnseen(0);
    } else {
      setUnseen(added);
    }
  }, [messages]);

  const toEnd = () => {
    const el = log.current;
    if (el) el.scrollTop = el.scrollHeight;
    atBottom.current = true;
    seen.current = messages.length;
    setUnseen(0);
  };

  const send = async () => {
    const text = draft.trim();
    if (!text) return;
    setBusy(true);
    setError(null);
    try {
      await api.sendRunMessage(runId, text);
      setDraft("");
      atBottom.current = true;
      const r = await api.runMessages(runId, 0);
      setMessages(r.messages);
      setThinking(true);
    } catch (e) {
      setError(e instanceof ApiError ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  const decide = async (m: RunMessage, index: number, dismiss: boolean) => {
    const card = m.detail.actions?.[index];
    if (!card) return;
    if (!dismiss && card.deploy && !window.confirm(`${cardTitle(card)} lets a deploy run. Go ahead?`)) return;
    setError(null);
    try {
      await api.applyRunCard(runId, m.id, index, dismiss);
      const r = await api.runMessages(runId, 0);
      setMessages(r.messages);
      if (!dismiss) onRunChanged?.();
    } catch (e) {
      setError(e instanceof ApiError ? e.message : String(e));
    }
  };

  return (
    <section className="run-chat" aria-label="conversation">
      <h3>
        <Icon name="chat" /> Conversation <span className="muted small">with {agentName}</span>
      </h3>
      <div
        className="run-chat-log"
        ref={log}
        onScroll={(e) => {
          const el = e.currentTarget;
          atBottom.current = el.scrollHeight - el.scrollTop - el.clientHeight < 24;
          if (atBottom.current && unseen) {
            seen.current = messages.length;
            setUnseen(0);
          }
        }}
      >
        {messages.length === 0 && !error && <p className="muted small">No messages yet.</p>}
        {messages.map((m) => (
          <div key={m.id} className={`msg msg-${m.role === "agent" ? "assistant" : m.role}`}>
            {m.role === "system" ? (
              <div className="msg-note small">
                <Icon name="alert" /> {m.text}
              </div>
            ) : (
              <div className={`msg-text${m.detail.error ? " muted" : ""}`}>{m.text}</div>
            )}
            {m.detail.actions && m.detail.actions.length > 0 && (
              <div className="actions">
                {m.detail.actions.map((c, i) => (
                  <div key={i} className={`action-card ${c.state === "done" ? "done" : ""}`}>
                    <strong>{cardTitle(c)}</strong>
                    {c.comment && <span className="small muted">{c.comment}</span>}
                    {c.state === "proposed" ? (
                      <div className="row">
                        <button type="button" className="primary" onClick={() => void decide(m, i, false)}>
                          Confirm
                        </button>
                        <button type="button" onClick={() => void decide(m, i, true)}>
                          Dismiss
                        </button>
                      </div>
                    ) : (
                      <span className="small muted">
                        {c.state}
                        {c.error ? `: ${c.error}` : ""}
                      </span>
                    )}
                  </div>
                ))}
              </div>
            )}
            <span className="msg-meta muted small">
              {m.role === "user" ? "you" : m.role === "agent" ? agentName : "pipeline"} · {formatTime(m.at)}
            </span>
          </div>
        ))}
        {thinking && (
          <div className="msg msg-assistant muted thinking">
            <Icon name="spinner" /> <span>{agentName} is thinking…</span>
          </div>
        )}
      </div>
      {unseen > 0 && (
        <button type="button" className="link run-chat-new" onClick={toEnd}>
          {unseen} new {unseen === 1 ? "message" : "messages"} ↓
        </button>
      )}
      {error && (
        <p className="issue" role="alert">
          {error}
        </p>
      )}
      <form
        className="assistant-input"
        onSubmit={(e) => {
          e.preventDefault();
          void send();
        }}
      >
        <textarea
          value={draft}
          rows={2}
          placeholder={`Message ${agentName}…`}
          aria-label={`message to ${agentName}`}
          onChange={(e) => setDraft(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) {
              e.preventDefault();
              void send();
            }
          }}
        />
        <button type="submit" className="primary" disabled={busy || !draft.trim()}>
          Send
        </button>
      </form>
    </section>
  );
}
