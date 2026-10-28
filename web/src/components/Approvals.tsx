// Gates waiting for a person (GET /api/approvals): approve or reject with a comment.
import { useEffect, useState } from "react";
import { ApiError, api, type Approval } from "../api";
import { formatTime } from "../lib/runs";

export function Approvals({ onChanged }: { onChanged?: () => void }) {
  const [items, setItems] = useState<Approval[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [comments, setComments] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState<string | null>(null);
  const load = () =>
    api
      .approvals()
      .then(setItems)
      .catch((e) => setError(e instanceof ApiError ? e.message : String(e)));
  useEffect(() => {
    void load();
    const t = setInterval(() => void load(), 4000);
    return () => clearInterval(t);
  }, []);
  const decide = async (a: Approval, approve: boolean) => {
    setBusy(a.execution_id);
    setError(null);
    try {
      await (approve ? api.approve : api.reject)(a.run_id, a.node_id, comments[a.execution_id] ?? "");
      await load();
      onChanged?.();
    } catch (e) {
      setError(e instanceof ApiError ? e.message : String(e));
    } finally {
      setBusy(null);
    }
  };
  return (
    <div className="page">
      <div className="toolbar">
        <strong>Approvals</strong>
        <span className="muted small">gates waiting for a person</span>
      </div>
      <div className="page-body">
        {error && <p className="issue">{error}</p>}
        {items === null ? (
          <p className="muted">Loading…</p>
        ) : items.length === 0 ? (
          <p className="muted">Nothing is waiting for approval.</p>
        ) : (
          items.map((a) => (
            <section key={a.execution_id} className="card approval">
              <div className="row">
                <strong>{a.pipeline}</strong>
                <a className="mono small" href={`#/runs/${a.run_id}`}>
                  {a.run_id}
                </a>
                <span className="muted small">at {a.node_id}</span>
                <span className="spacer" />
                <span className="muted small">waiting since {formatTime(a.waiting_since)}</span>
              </div>
              <p className="approval-prompt">{a.prompt || "Approve?"}</p>
              {a.approvers.length > 0 && <p className="small muted">Approvers: {a.approvers.join(", ")}</p>}
              <div className="row">
                <input
                  value={comments[a.execution_id] ?? ""}
                  placeholder="Comment (optional)"
                  aria-label="comment"
                  onChange={(e) => setComments({ ...comments, [a.execution_id]: e.target.value })}
                />
                <button type="button" className="danger" disabled={busy !== null} onClick={() => void decide(a, false)}>
                  Reject
                </button>
                <button type="button" className="primary" disabled={busy !== null} onClick={() => void decide(a, true)}>
                  Approve
                </button>
              </div>
            </section>
          ))
        )}
      </div>
    </div>
  );
}
