// App reports: agentline's own problems (crash / error reports, failures triaged as
// infrastructure). They aren't fixable in the app, so they're kept off Problems and reported on
// GitHub (/api/app-reports, agentline.triage): a card when [report] github_repo + GITHUB_TOKEN
// are set, otherwise the report to copy as a GitHub issue.
import { useEffect, useState } from "react";
import { ApiError, api } from "../api";
import { isMacApp, nativeCall } from "../lib/native";
import { formatTime } from "../lib/runs";
import { appReportSource, githubIssueMarkdown, reportLine, type GithubStatus, type TriageRecord } from "../lib/triage";
import { Status } from "./Icon";

export function AppReports() {
  const [reports, setReports] = useState<TriageRecord[]>([]);
  const [github, setGithub] = useState<GithubStatus | null>(null);
  const [msg, setMsg] = useState<string | null>(null);
  const [loaded, setLoaded] = useState(false);
  const load = async () => {
    try {
      const r = await api.appReports();
      setReports(r.reports);
      setGithub(r.github);
    } catch (e) {
      setMsg(e instanceof ApiError ? e.message : String(e));
    } finally {
      setLoaded(true);
    }
  };
  useEffect(() => {
    void load();
  }, []);
  return (
    <div className="page">
      <div className="toolbar">
        <strong>App reports</strong>
        <span className="small muted">Problems in agentline itself. They're reported on GitHub, not fixed here.</span>
        <span className="spacer" />
        {msg && <span className="small muted">{msg}</span>}
        {isMacApp() && (
          <button
            type="button"
            title="Look in ~/Library/Logs/DiagnosticReports for new crash reports"
            onClick={async () => {
              const r = await nativeCall<{ message: string }>("problems.scan");
              setMsg(r.message);
              await load();
            }}
          >
            Scan for crash reports
          </button>
        )}
        {isMacApp() && (
          <button type="button" title="A .zip with every report, attachments, system info and the engine log" onClick={() => void nativeCall("problems.export")}>
            Export reports…
          </button>
        )}
        <button
          type="button"
          className="danger"
          disabled={!reports.some((r) => r.incident)}
          onClick={async () => {
            if (!window.confirm("Delete every crash and error report?")) return;
            await api.clearIncidents();
            await load();
          }}
        >
          Clear crash reports
        </button>
      </div>
      <div className="pad app-reports">
        <p className="small muted">{githubLine(github)}</p>
        {loaded && !reports.length && <p className="muted">No problems in agentline itself.</p>}
        {reports.map((r) => (
          <AppReportCard key={r.id} report={r} github={github} onChanged={load} />
        ))}
      </div>
    </div>
  );
}

function githubLine(gh: GithubStatus | null): string {
  if (!gh) return "";
  if (gh.ready) return `Reports go to GitHub: ${gh.repo}${gh.auto ? " (created automatically)" : ""}.`;
  return 'GitHub reporting isn\'t set up: add github_repo = "owner/name" under [report] in agentline.toml and the GITHUB_TOKEN key. Until then, copy a report as a GitHub issue.';
}

function AppReportCard({ report: r, github, onChanged }: { report: TriageRecord; github: GithubStatus | null; onChanged: () => Promise<void> }) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [copied, setCopied] = useState<"" | "copied" | "failed">("");
  // where it went (the page header says how to set GitHub up)
  const line = r.github?.url || r.github_error ? reportLine(r, github) : "";
  const act = async (fn: () => Promise<unknown>) => {
    setBusy(true);
    setError(null);
    try {
      await fn();
      await onChanged();
    } catch (e) {
      setError(e instanceof ApiError ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };
  return (
    <section className="triage app-report" aria-label={r.summary}>
      <div className="triage-head">
        <Status s={r.github?.url ? "done" : "dead"}>{r.github?.url ? "Reported" : "Not reported"}</Status>
        <span className="small muted">{appReportSource(r)}</span>
        <span className="spacer" />
        <span className="small muted">{formatTime(r.updated_at)}</span>
      </div>
      <p className="triage-summary">{r.summary}</p>
      {r.reasoning && !r.incident && <p className="small">{r.reasoning}</p>}
      {line && <p className="small">{r.github?.url ? <a href={r.github.url} target="_blank" rel="noreferrer">{line}</a> : line}</p>}
      {r.report && (
        <details open={copied === "failed"}>
          <summary className="small">{copied === "failed" ? "Report (select and copy it)" : "Report"}</summary>
          <pre className="mono small issue-text">{copied === "failed" ? githubIssueMarkdown(r) : r.report.markdown}</pre>
        </details>
      )}
      <div className="row triage-buttons">
        {github?.ready && !r.github?.url && (
          <button type="button" className="primary" disabled={busy} onClick={() => void act(() => api.triageGithub(r.id))}>
            Report on GitHub
          </button>
        )}
        {!r.github?.url && (
          <button
            type="button"
            onClick={async () => {
              try {
                await navigator.clipboard.writeText(githubIssueMarkdown(r));
                setCopied("copied");
              } catch {
                setCopied("failed"); // no clipboard access here: show the text to copy by hand
              }
            }}
          >
            {copied === "copied" ? "Copied" : copied === "failed" ? "Couldn't copy" : "Copy as GitHub issue"}
          </button>
        )}
        {r.run && (
          <a className="small" href={`#/runs/${encodeURIComponent(r.run)}`}>
            Open run {r.run}
          </a>
        )}
        {r.incident && isMacApp() && (
          <button type="button" className="link" onClick={() => void nativeCall("finder.incident", { id: r.incident })}>
            Show in Finder
          </button>
        )}
        <button
          type="button"
          className="link"
          disabled={busy}
          title={r.incident ? "Delete the crash report" : "Remove it from App reports"}
          onClick={() => {
            if (window.confirm(r.incident ? "Delete this crash report?" : "Remove this report?")) void act(() => api.deleteAppReport(r.id));
          }}
        >
          Remove
        </button>
      </div>
      {error && <p className="issue small">{error}</p>}
    </section>
  );
}
