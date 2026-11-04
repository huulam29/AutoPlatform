// Run view: people's feedback (👍 / 👎 + a comment) on the run or one node's output, the run's
// metrics per node, and the run report (phase 58; api/feedback.py).
import { useEffect, useState } from "react";
import { ApiError } from "../api";
import {
  OPEN_REPORT_EVENT,
  type FeedbackEntry,
  type FeedbackSummary,
  type ReportFormat,
  type RunMetrics,
  feedbackApi,
  formatDuration,
  formatTokens,
  myRating,
  nextRating,
  reportFilename,
  takePendingReport,
} from "../lib/feedback";
import { formatCost } from "../lib/runs";
import { isMacApp } from "../lib/native";

const errText = (e: unknown) => (e instanceof ApiError ? e.message : String(e));

export function FeedbackBox({
  runId,
  node = null,
  iteration,
  version,
}: {
  runId: string;
  node?: string | null;
  iteration?: number;
  version?: number;
}) {
  const [entries, setEntries] = useState<FeedbackEntry[]>([]);
  const [summary, setSummary] = useState<FeedbackSummary | null>(null);
  const [comment, setComment] = useState("");
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<string | null>(null);
  useEffect(() => {
    let live = true;
    feedbackApi
      .list(runId)
      .then((r) => {
        if (!live) return;
        setEntries(r.entries);
        setSummary(r.summary);
      })
      .catch((e) => live && setMsg(errText(e)));
    return () => {
      live = false;
    };
  }, [runId, version]);
  const mine = myRating(summary, node);
  const send = async (rating: "up" | "down" | "none", text = "") => {
    setBusy(true);
    setMsg(null);
    try {
      const r = await feedbackApi.rate(runId, rating, text, node, iteration);
      setEntries((xs) => [...xs, r.entry]);
      setSummary(r.summary);
      if (text) setComment("");
      setMsg(rating === "down" || text ? "Thanks. The run review will read this." : "Thanks.");
    } catch (e) {
      setMsg(errText(e));
    } finally {
      setBusy(false);
    }
  };
  const mineHere = entries.filter((e) => (e.node ?? null) === node);
  const counts = node ? summary?.nodes[node] : summary?.run;
  const what = node ? "this output" : "this run";
  return (
    <section className="feedback" aria-label={`Feedback on ${what}`}>
      <div className="feedback-head">
        <span className="small">How was {what}?</span>
        <button
          type="button"
          className="thumb"
          aria-pressed={mine === "up"}
          disabled={busy}
          title={mine === "up" ? "Withdraw your 👍" : "Good"}
          onClick={() => void send(nextRating(mine, "up"))}
        >
          👍 <span className="small">{counts?.up ?? 0}</span>
        </button>
        <button
          type="button"
          className="thumb"
          aria-pressed={mine === "down"}
          disabled={busy}
          title={mine === "down" ? "Withdraw your 👎" : "Not good: say why below"}
          onClick={() => void send(nextRating(mine, "down"))}
        >
          👎 <span className="small">{counts?.down ?? 0}</span>
        </button>
      </div>
      <form
        className="feedback-form"
        onSubmit={(e) => {
          e.preventDefault();
          if (comment.trim()) void send(mine ?? "down", comment.trim());
        }}
      >
        <textarea
          aria-label={`Comment on ${what}`}
          rows={2}
          maxLength={2000}
          placeholder={mine === "up" ? "What was good?" : "What was wrong? The run review reads it."}
          value={comment}
          onChange={(e) => setComment(e.target.value)}
        />
        <button type="submit" disabled={busy || !comment.trim()}>
          Send
        </button>
      </form>
      {msg && <p className="small muted">{msg}</p>}
      {mineHere.some((e) => e.comment) && (
        <ul className="feedback-list small">
          {mineHere
            .filter((e) => e.comment)
            .slice(-5)
            .map((e) => (
              <li key={e.id}>
                <span className="muted">{e.by}</span> {e.rating === "up" ? "👍" : e.rating === "down" ? "👎" : ""} {e.comment}
              </li>
            ))}
        </ul>
      )}
    </section>
  );
}

export function MetricsPanel({ runId, version }: { runId: string; version?: number }) {
  const [m, setM] = useState<RunMetrics | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [report, setReport] = useReportDialog(runId);
  useEffect(() => {
    let live = true;
    feedbackApi
      .metrics(runId)
      .then((x) => live && setM(x))
      .catch((e) => live && setErr(errText(e)));
    return () => {
      live = false;
    };
  }, [runId, version]);
  return (
    <div className="panel">
      <div className="node-run-head">
        <h3>Metrics</h3>
        <span className="spacer" />
        <button type="button" onClick={() => setReport("md")}>
          Report
        </button>
      </div>
      {err && <p className="issue small">{err}</p>}
      {m && (
        <>
          <dl className="kv metrics-kv">
            <dt>Wall time</dt>
            <dd>
              {formatDuration(m.wall_s)}
              {!m.finished && " so far"}
            </dd>
            <dt>Working</dt>
            <dd>{formatDuration(m.totals.work_s)}</dd>
            <dt>Waiting</dt>
            <dd title="gates, waits and questions">{formatDuration(m.totals.waiting_s)}</dd>
            <dt>Queued</dt>
            <dd>{formatDuration(m.totals.queued_s)}</dd>
            <dt>Cost</dt>
            <dd>{formatCost(m.cost_usd)}</dd>
            <dt>Tokens</dt>
            <dd>{formatTokens(m.totals.tokens)}</dd>
            <dt>Retries</dt>
            <dd>{m.totals.retries}</dd>
          </dl>
          <table className="table metrics-table">
            <thead>
              <tr>
                <th>Node</th>
                <th className="num">Runs</th>
                <th className="num">Work</th>
                <th className="num">Waiting</th>
                <th className="num">Cost</th>
                <th>Tokens</th>
                <th className="num" title="thumbs up / down">👍/👎</th>
              </tr>
            </thead>
            <tbody>
              {m.nodes.map((n) => (
                <tr key={n.node}>
                  <td className="mono">
                    {n.node}
                    {n.retries > 0 && <span className="muted small"> ({n.retries} retries)</span>}
                  </td>
                  <td className="num">{n.executions}</td>
                  <td className="num">{formatDuration(n.work_s)}</td>
                  <td className="num">{formatDuration(n.waiting_s)}</td>
                  <td className="num">{formatCost(n.cost_usd)}</td>
                  <td className="small" title={n.turns !== null ? `${n.turns} turns` : undefined}>
                    {formatTokens(n.tokens)}
                  </td>
                  <td className="num">
                    {n.feedback.up}/{n.feedback.down}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </>
      )}
      {report && <ReportDialog runId={runId} format={report} onFormat={setReport} onClose={() => setReport(null)} />}
    </div>
  );
}

/** The report dialog's state; also opened by the Chat's `run_report` action. */
export function useReportDialog(runId: string): [ReportFormat | null, (f: ReportFormat | null) => void] {
  const [open, setOpen] = useState<ReportFormat | null>(() => takePendingReport(runId));
  useEffect(() => {
    const on = (e: Event) => {
      const d = (e as CustomEvent<{ run: string; format: ReportFormat }>).detail;
      if (d?.run === runId) {
        takePendingReport(runId);
        setOpen(d.format);
      }
    };
    window.addEventListener(OPEN_REPORT_EVENT, on);
    return () => window.removeEventListener(OPEN_REPORT_EVENT, on);
  }, [runId]);
  return [open, setOpen];
}

export function ReportDialog({
  runId,
  format,
  onFormat,
  onClose,
}: {
  runId: string;
  format: ReportFormat;
  onFormat: (f: ReportFormat) => void;
  onClose: () => void;
}) {
  const [text, setText] = useState<string | null>(null);
  const [msg, setMsg] = useState<string | null>(null);
  useEffect(() => {
    let live = true;
    setText(null);
    feedbackApi
      .report(runId, format)
      .then((t) => live && setText(t))
      .catch((e) => live && setMsg(errText(e)));
    return () => {
      live = false;
    };
  }, [runId, format]);
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);
  const download = () => {
    if (text === null) return;
    const blob = new Blob([text], { type: format === "json" ? "application/json" : "text/markdown" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = reportFilename(runId, format);
    a.click();
    URL.revokeObjectURL(url);
  };
  return (
    <div className="modal-backdrop" role="dialog" aria-modal="true" aria-labelledby="report-title">
      <div className="modal wide">
        <h2 id="report-title">Report of run {runId}</h2>
        <div className="tabs" role="group" aria-label="report format">
          {(["md", "json"] as const).map((f) => (
            <button key={f} type="button" className={format === f ? "on" : ""} aria-pressed={format === f} onClick={() => onFormat(f)}>
              {f === "md" ? "Markdown" : "JSON"}
            </button>
          ))}
        </div>
        <p className="small muted">Secrets are redacted. Outputs appear as their short summaries.</p>
        {msg && <p className="issue">{msg}</p>}
        <pre className="report-text">{text ?? "Loading…"}</pre>
        <div className="modal-foot">
          <button type="button" onClick={onClose}>
            Close
          </button>
          <button
            type="button"
            disabled={text === null}
            onClick={async () => {
              try {
                await navigator.clipboard.writeText(text ?? "");
                setMsg("Copied.");
              } catch {
                setMsg("Couldn't copy here: select the text instead.");
              }
            }}
          >
            Copy
          </button>
          {!isMacApp() && (
            <button type="button" className="primary" disabled={text === null} onClick={download}>
              Download
            </button>
          )}
        </div>
      </div>
    </div>
  );
}
