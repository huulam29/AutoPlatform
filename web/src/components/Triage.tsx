// Failure triage on the run view and Problems: the verdict (AI instruction / agentline issue /
// external), why, the report, and the proposals as the same cards the Chat shows. Nothing is
// applied until the user clicks (decision 63); "Open in Chat" hands the card to the Chat.
import { useCallback, useEffect, useState } from "react";
import { ApiError, api } from "../api";
import { applyAction, assistantInbox, type ActionState, type Applied } from "../lib/assistant";
import {
  canTriage,
  githubIssueMarkdown,
  latestTriage,
  reportLine,
  triageReply,
  triageStatus,
  verdictHelp,
  type GithubStatus,
  type TriageRecord,
} from "../lib/triage";
import { ActionCard, deps } from "./Assistant";
import { panels } from "./Chrome";
import { Status } from "./Icon";

const FAILED = new Set(["dead", "needs-review"]);

/** The run's triage records, polled while one is running or a failed run has none yet. */
export function useTriage(run: string | null, runStatus?: string, version?: number) {
  const [records, setRecords] = useState<TriageRecord[]>([]);
  const [github, setGithub] = useState<GithubStatus | null>(null);
  const [tick, setTick] = useState(0);
  const reload = useCallback(() => setTick((n) => n + 1), []);
  useEffect(() => {
    if (!run) return;
    let live = true;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const load = async () => {
      try {
        const r = await api.triageList(run);
        if (!live) return;
        setRecords(r.records);
        setGithub(r.github);
        const waiting = r.records.some((x) => x.status === "running") || (FAILED.has(runStatus ?? "") && !r.records.length);
        if (waiting) timer = setTimeout(load, 4000);
      } catch {
        if (live) timer = setTimeout(load, 8000);
      }
    };
    void load();
    return () => {
      live = false;
      if (timer) clearTimeout(timer);
    };
  }, [run, runStatus, version, tick]);
  return { records, github, reload };
}

/** A small verdict chip for toolbars and lists. */
export function TriageChip({ record, onClick }: { record: TriageRecord; onClick?: () => void }) {
  const st = triageStatus(record);
  return (
    <button type="button" className="link triage-chip" onClick={onClick} title={record.summary || record.error || ""}>
      <Status s={st.s}>{`Triage: ${st.word}`}</Status>
    </button>
  );
}

export function TriagePanel({
  run,
  node,
  records,
  github,
  reload,
  status,
  onRunChanged,
}: {
  run: string;
  node?: string | null;
  records: TriageRecord[];
  github: GithubStatus | null;
  reload: () => void;
  /** the node's (or run's) status: offers "Triage" when nothing was triaged yet */
  status?: string;
  onRunChanged?: () => void;
}) {
  const rec = latestTriage(records, node);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [copied, setCopied] = useState<"" | "copied" | "failed">("");
  const [states, setStates] = useState<Record<number, { state: ActionState; error?: string; result?: string; run?: string }>>({});
  useEffect(() => {
    setStates({});
    setCopied("");
  }, [rec?.id, rec?.updated_at]);

  const again = async () => {
    setBusy(true);
    setError(null);
    try {
      await api.triageRun(run, node ?? undefined);
    } catch (e) {
      setError(e instanceof ApiError ? e.message : String(e));
    } finally {
      setBusy(false);
      reload();
    }
  };

  if (!rec) {
    if (!canTriage(status) && !FAILED.has(status ?? "")) return null;
    return (
      <section className="triage" aria-label="Triage">
        <div className="triage-head">
          <h4>Triage</h4>
          <button type="button" disabled={busy} onClick={() => void again()} title="Ask the Chat whose fault this is and what to change">
            {busy ? "Triaging…" : "Triage"}
          </button>
        </div>
        <p className="small muted">
          {FAILED.has(status ?? "")
            ? "New failures are triaged automatically. Triage counts against the run's budget."
            : "Does the result look wrong? Triage finds out whose fault it is and proposes a fix."}
        </p>
        {error && <p className="issue small">{error}</p>}
      </section>
    );
  }

  const st = triageStatus(rec);
  const line = reportLine(rec, github);
  const apply = async (i: number, input: { value?: string; file?: Blob } = {}) => {
    const a = rec.actions[i];
    setStates((s) => ({ ...s, [i]: { state: "applying" } }));
    try {
      const r: Applied = await applyAction(a, deps, input);
      setStates((s) => ({ ...s, [i]: { state: "applied", result: r.result, run: r.run } }));
      if (a.type === "retry") onRunChanged?.();
      if (a.type === "report_issue" || a.type === "triage") reload();
    } catch (e) {
      setStates((s) => ({ ...s, [i]: { state: "failed", error: e instanceof Error ? e.message : String(e) } }));
    }
  };
  return (
    <section className="triage" aria-label="Triage">
      <div className="triage-head">
        <h4>Triage</h4>
        <Status s={st.s} pill>
          {st.word}
        </Status>
        {rec.node && !node && <span className="mono small">{rec.node}</span>}
        <span className="spacer" />
        {rec.cost_usd > 0 && <span className="small muted">${rec.cost_usd.toFixed(3)}</span>}
      </div>
      {rec.summary && <p className="triage-summary">{rec.summary}</p>}
      {rec.verdict && <p className="small muted">{verdictHelp(rec.verdict)}</p>}
      {rec.error && <p className={rec.status === "failed" ? "issue small" : "small muted"}>{rec.error}</p>}
      {rec.reasoning && (
        <details>
          <summary className="small">Why</summary>
          <p className="small triage-why">{rec.reasoning}</p>
        </details>
      )}
      {line && <p className="small">{rec.github?.url ? <a href={rec.github.url} target="_blank" rel="noreferrer">{line}</a> : line}</p>}
      {rec.report && (
        <details open={copied === "failed"}>
          <summary className="small">{copied === "failed" ? "Report (select and copy it)" : "Report"}</summary>
          <pre className="mono small issue-text">{copied === "failed" ? githubIssueMarkdown(rec) : rec.report.markdown}</pre>
        </details>
      )}
      <div className="row triage-buttons">
        {rec.actions.length > 0 && (
          <button
            type="button"
            onClick={() => {
              assistantInbox.push(triageReply(rec));
              panels.set("assistant", false);
            }}
            title="Show this triage in the Chat, where you can ask about it"
          >
            Open in Chat
          </button>
        )}
        {rec.report && !rec.github?.url && !github?.ready && (
          <button
            type="button"
            onClick={async () => {
              try {
                await navigator.clipboard.writeText(githubIssueMarkdown(rec));
                setCopied("copied");
              } catch {
                setCopied("failed"); // no clipboard access here: show the text to copy by hand
              }
            }}
          >
            {copied === "copied" ? "Copied" : copied === "failed" ? "Couldn't copy" : "Copy as GitHub issue"}
          </button>
        )}
        {rec.issue != null && (
          <a className="small" href={`#/problems/${rec.issue}`}>
            Issue #{rec.issue}
          </a>
        )}
        <button type="button" className="link" disabled={busy || rec.status === "running"} onClick={() => void again()}>
          {busy ? "Triaging…" : "Triage again"}
        </button>
      </div>
      {error && <p className="issue small">{error}</p>}
      {rec.actions.length > 0 && (
        <div className="triage-cards">
          {rec.actions.map((a, i) => (
            <ActionCard
              key={`${rec.id}-${i}`}
              action={a}
              state={states[i]?.state ?? "pending"}
              error={states[i]?.error}
              result={states[i]?.result}
              run={states[i]?.run && states[i]?.run !== run ? states[i]?.run : undefined}
              onApply={(input) => void apply(i, input)}
            />
          ))}
        </div>
      )}
    </section>
  );
}
