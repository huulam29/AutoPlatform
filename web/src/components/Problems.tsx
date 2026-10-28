// Problems: what can be fixed in the app (failed runs: pipelines, settings, config, keys), each
// tracked as an issue (/api/issues) with a status, notes and history; Fix this hands one to the
// Chat, and a run's failure triage shows on its issue. agentline's own problems (crash reports,
// infrastructure verdicts) are on App reports instead (components/AppReports.tsx).
import { useEffect, useMemo, useState } from "react";
import { ApiError, api, type IncidentSummary, type IssueRecord } from "../api";
import { formatTime } from "../lib/runs";
import { triageStatus, type Verdict } from "../lib/triage";
import { Status } from "./Icon";
import { TriagePanel, useTriage } from "./Triage";

const STATUSES = ["open", "in_progress", "fixed", "wont_fix"] as const;
const LABEL: Record<string, string> = { open: "Open", in_progress: "In progress", fixed: "Fixed", wont_fix: "Won't fix" };
type Filter = "active" | "closed" | "all";

export function issueShown(filter: Filter, issue: IssueRecord | undefined): boolean {
  const closed = issue?.status === "fixed" || issue?.status === "wont_fix";
  return filter === "all" || (filter === "closed" ? closed : !closed);
}

export function summaryMarkdown(issues: IssueRecord[], incidents: IncidentSummary[] = []): string {
  const lines = ["# agentline problems", ""];
  for (const i of issues) {
    const inc = incidents.find((x) => i.source.kind === "incident" && x.id === i.source.ref);
    lines.push(
      `- #${i.number} [${LABEL[i.status] ?? i.status}] ${i.title}` +
        (inc ? ` (${inc.source}/${inc.kind}, x${inc.count}, v${inc.version ?? "?"})` : ` (${i.source.kind} ${i.source.ref})`),
    );
  }
  return lines.join("\n") + "\n";
}

export function Problems({ onChanged, issue }: { onChanged?: () => void; issue?: number }) {
  const [issues, setIssues] = useState<IssueRecord[]>([]);
  const [filter, setFilter] = useState<Filter>("active");
  const [picked, setPicked] = useState<number | null>(issue ?? null);
  useEffect(() => {
    if (issue != null) setPicked(issue);
  }, [issue]);
  const [msg, setMsg] = useState<string | null>(null);
  const load = async () => {
    try {
      const a = await api.issues();
      setIssues(a.issues);
      onChanged?.();
    } catch (e) {
      setMsg(e instanceof ApiError ? e.message : String(e));
    }
  };
  useEffect(() => {
    void load();
  }, []);
  const shown = issues.filter((i) => issueShown(filter, i));
  const count = (f: Filter) => issues.filter((i) => issueShown(f, i)).length;
  const current = issues.find((i) => i.number === picked) ?? null;
  return (
    <div className="page">
      <div className="toolbar">
        <strong>Problems</strong>
        <div className="tabs">
          {(["active", "closed", "all"] as Filter[]).map((f) => (
            <button key={f} className={filter === f ? "on" : ""} onClick={() => setFilter(f)}>
              {f === "active" ? "Open" : f === "closed" ? "Closed" : "All"} ({count(f)})
            </button>
          ))}
        </div>
        <span className="spacer" />
        {msg && <span className="small muted">{msg}</span>}
        <a className="small" href="#/app-reports" title="Crash reports and other problems in agentline itself: reported on GitHub, not fixed here">
          App reports
        </a>
        <button type="button" disabled={!issues.length} onClick={async () => {
          await navigator.clipboard?.writeText(summaryMarkdown(issues));
          setMsg("Summary copied.");
        }}>
          Copy summary
        </button>
      </div>
      <div className="problems">
        <ul className="problem-list">
          {shown.length === 0 && <li className="muted pad">No problems with this status.</li>}
          {shown.map((i) => (
            <li key={i.number}>
              <button type="button" className={picked === i.number ? "on" : ""} onClick={() => setPicked(i.number)}>
                <span className={`badge issue-${i.status}`}>
                  #{i.number} {LABEL[i.status]}
                </span>{" "}
                <span className="problem-title">{i.title}</span>
                <span className="small muted">
                  {i.source.kind === "run" ? "failed run" : "crash / error"} · {formatTime(i.updated_at)}
                  {i.occurrences && i.occurrences > 1 ? ` · ×${i.occurrences}` : ""}
                </span>
                {i.triage && (
                  <Status s={triageStatus({ status: "done", verdict: i.triage.verdict as Verdict }).s}>
                    {triageStatus({ status: "done", verdict: i.triage.verdict as Verdict }).word}
                  </Status>
                )}
              </button>
            </li>
          ))}
        </ul>
        <div className="problem-detail">
          {current ? (
            <IssueDetail key={current.number} issue={current} onChanged={load} />
          ) : (
            <p className="muted pad">Pick a problem on the left.</p>
          )}
        </div>
      </div>
    </div>
  );
}

function IssueDetail({ issue, onChanged }: { issue: IssueRecord; onChanged: () => Promise<void> }) {
  const [note, setNote] = useState("");
  const [error, setError] = useState<string | null>(null);
  const update = async (body: { status?: string; note?: string }) => {
    try {
      await api.updateIssue(issue.number, body);
      setNote("");
      setError(null);
      await onChanged();
    } catch (e) {
      setError(e instanceof ApiError ? e.message : String(e));
    }
  };
  const timeline = useMemo(
    () =>
      [
        ...issue.history.map((h) => ({ at: h.at, text: `${h.from ? `${LABEL[h.from] ?? h.from} → ` : ""}${LABEL[h.to] ?? h.to}${h.note ? `: ${h.note}` : ""}` })),
        ...issue.notes.map((n) => ({ at: n.at, text: `Note: ${n.text}` })),
      ].sort((a, b) => b.at - a.at),
    [issue],
  );
  return (
    <div className="pad">
      <div className="row">
        <h3>
          #{issue.number} {issue.title}
        </h3>
      </div>
      <div className="row">
        <label className="small">
          Status{" "}
          <select value={issue.status} onChange={(e) => void update({ status: e.target.value, note: note || undefined })}>
            {STATUSES.map((s) => (
              <option key={s} value={s}>
                {LABEL[s]}
              </option>
            ))}
          </select>
        </label>
        <a className="button-link" href={`#/fix/issue/${issue.number}`} title="Open the pipeline and ask the Chat to find the cause">
          Fix this
        </a>
        {issue.source.kind === "run" && (
          <a href={`#/runs/${issue.source.ref}`} className="small">
            Open run {issue.source.ref}
          </a>
        )}
      </div>
      <form className="row" onSubmit={(e) => { e.preventDefault(); if (note.trim()) void update({ note }); }}>
        <input value={note} placeholder="Add a note (also saved with a status change)" aria-label="note" onChange={(e) => setNote(e.target.value)} />
        <button type="submit" disabled={!note.trim()}>
          Add note
        </button>
      </form>
      {error && <p className="issue">{error}</p>}
      <details open>
        <summary className="small">History ({timeline.length})</summary>
        <ol className="journal">
          {timeline.map((t, k) => (
            <li key={k}>
              <span className="muted small">{formatTime(t.at)}</span> {t.text}
            </li>
          ))}
        </ol>
      </details>
      {issue.detail && issue.source.kind === "run" && <pre className="mono small issue-text">{issue.detail}</pre>}
      {issue.source.kind === "run" && <IssueTriage run={issue.source.ref} />}
    </div>
  );
}

function IssueTriage({ run }: { run: string }) {
  const { records, github, reload } = useTriage(run);
  return <TriagePanel run={run} records={records} github={github} reload={reload} status="dead" />;
}
