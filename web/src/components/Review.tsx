// Run reviews on the run view, and a pipeline's lesson library in the editor (Lessons tab). A
// review says what the run did the hard way and proposes edits as the Chat's own cards; an edit
// can be probed (replayed on this run's inputs) before it is applied. A review's pipeline edit
// is applied on the server, which keeps the files as they were and checks the next runs
// (helped / hurt / no effect; decision 71). Nothing changes until the user clicks.
import { useCallback, useEffect, useState } from "react";
import { ApiError, api } from "../api";
import { applyAction, assistantInbox, type ActionState, type Applied, type AssistantAction } from "../lib/assistant";
import {
  adoptionLine,
  canProbe,
  canReview,
  isApplied,
  latestReview,
  librarySummary,
  lessonStatus,
  probeStatus,
  reviewReply,
  reviewStatus,
  rowStatus,
  signalLines,
  type Adoption,
  type Lesson,
  type Probe,
  type ReviewConfig,
  type ReviewRecord,
} from "../lib/review";
import { formatTime } from "../lib/runs";
import { ActionCard, deps } from "./Assistant";
import { panels } from "./Chrome";
import { Status } from "./Icon";

const WAIT_FOR_AUTO = 180; // seconds after a run ends that an automatic review may still come

/** The run's reviews, polled while one (or a probe) runs, or a finished run may still get one. */
export function useReviews(run: string | null, runStatus?: string, runUpdated?: number) {
  const [records, setRecords] = useState<ReviewRecord[]>([]);
  const [config, setConfig] = useState<ReviewConfig | null>(null);
  const [tick, setTick] = useState(0);
  const reload = useCallback(() => setTick((n) => n + 1), []);
  useEffect(() => {
    if (!run) return;
    let live = true;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const load = async () => {
      try {
        const r = await api.reviewList({ run });
        if (!live) return;
        setRecords(r.records);
        setConfig(r.config);
        const busy = r.records.some((x) => x.status === "running" || Object.values(x.probes ?? {}).some((p) => p.status === "running"));
        const mayCome =
          !r.records.length && r.config.auto !== "off" && canReview(runStatus) && runUpdated != null && Date.now() / 1000 - runUpdated < WAIT_FOR_AUTO;
        if (busy || mayCome) timer = setTimeout(load, busy ? 3000 : 6000);
      } catch {
        if (live) timer = setTimeout(load, 10000);
      }
    };
    void load();
    return () => {
      live = false;
      if (timer) clearTimeout(timer);
    };
  }, [run, runStatus, runUpdated, tick]);
  return { records, config, reload };
}

export function ReviewChip({ record, onClick }: { record: ReviewRecord; onClick?: () => void }) {
  const st = reviewStatus(record);
  return (
    <button type="button" className="link triage-chip" onClick={onClick} title={record.summary || record.error || ""}>
      <Status s={st.s}>{`Review: ${st.word}`}</Status>
    </button>
  );
}

type CardState = { state: ActionState; error?: string; result?: string };

export function ReviewPanel({
  run,
  status,
  records,
  config,
  reload,
}: {
  run: string;
  status: string;
  records: ReviewRecord[];
  config: ReviewConfig | null;
  reload: () => void;
}) {
  const rec = latestReview(records);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [states, setStates] = useState<Record<number, CardState>>({});
  const [probing, setProbing] = useState<Record<number, boolean>>({});
  useEffect(() => setStates({}), [rec?.id, rec?.created_at]);

  const again = async () => {
    setBusy(true);
    setError(null);
    try {
      await api.reviewRun(run);
    } catch (e) {
      setError(e instanceof ApiError ? e.message : String(e));
    } finally {
      setBusy(false);
      reload();
    }
  };

  if (!rec) {
    if (!canReview(status)) return null;
    return (
      <section className="triage review" aria-label="Review">
        <div className="triage-head">
          <h4>Review</h4>
          <button type="button" disabled={busy} onClick={() => void again()} title="Ask the Chat how this pipeline's instructions could change, judging by this run">
            {busy ? "Reviewing…" : "Review"}
          </button>
        </div>
        <p className="small muted">
          {config?.auto === "off"
            ? "Automatic reviews are off ([review] auto in agentline.toml)."
            : "Runs that went the hard way (loops, rejections, retries, cost spikes) are reviewed automatically."}{" "}
          A review counts against the run's budget.
        </p>
        {error && <p className="issue small">{error}</p>}
      </section>
    );
  }

  const st = reviewStatus(rec);
  const sig = signalLines(rec.signals);
  const cost = rec.cost_usd + (rec.probe_cost_usd ?? 0);
  const apply = async (i: number, input: { value?: string; file?: Blob } = {}) => {
    const a = rec.actions[i];
    setStates((s) => ({ ...s, [i]: { state: "applying" } }));
    try {
      if (a.type === "pipeline") {
        await api.reviewApply(rec.id, i); // keeps the files as they were; checks the next runs
        const n = config?.followup_runs ?? 5;
        setStates((s) => ({ ...s, [i]: { state: "applied", result: `Applied. The next ${n} runs show whether it helped.` } }));
        reload();
        return;
      }
      const r: Applied = await applyAction(a, deps, input);
      setStates((s) => ({ ...s, [i]: { state: "applied", result: r.result } }));
      if (a.type === "pipeline_lesson") reload();
    } catch (e) {
      setStates((s) => ({ ...s, [i]: { state: "failed", error: e instanceof Error ? e.message : String(e) } }));
    }
  };
  const probe = async (i: number) => {
    setProbing((p) => ({ ...p, [i]: true }));
    setError(null);
    try {
      await api.reviewProbe(rec.id, i);
    } catch (e) {
      setError(e instanceof ApiError ? e.message : String(e));
    } finally {
      setProbing((p) => ({ ...p, [i]: false }));
      reload();
    }
  };
  return (
    <section className="triage review" aria-label="Review">
      <div className="triage-head">
        <h4>Review</h4>
        <Status s={st.s} pill>
          {st.word}
        </Status>
        <span className="spacer" />
        {cost > 0 && (
          <span className="small muted" title="The review and its probes, counted against the run's budget">
            ${cost.toFixed(3)}
          </span>
        )}
      </div>
      {rec.summary && <p className="triage-summary">{rec.summary}</p>}
      {rec.error && <p className={rec.status === "failed" ? "issue small" : "small muted"}>{rec.error}</p>}
      {sig.length > 0 && (
        <ul className="review-signals small">
          {sig.map((s) => (
            <li key={s}>{s}</li>
          ))}
        </ul>
      )}
      {rec.rows.length > 0 && (
        <details open={rec.rows.length <= 3}>
          <summary className="small">What it found ({rec.rows.length})</summary>
          <ul className="review-rows">
            {rec.rows.map((r, i) => (
              <li key={i}>
                <Status s={rowStatus(r.verdict)}>{r.verdict}</Status>{" "}
                <span className="mono small">
                  {r.node} · {r.component}
                </span>
                <p className="small">{r.observed}</p>
                {r.evidence && <p className="small muted">Evidence: {r.evidence}</p>}
              </li>
            ))}
          </ul>
        </details>
      )}
      <div className="row triage-buttons">
        {rec.actions.length > 0 && (
          <button
            type="button"
            onClick={() => {
              assistantInbox.push(reviewReply(rec));
              panels.set("assistant", false);
            }}
            title="Show this review in the Chat, where you can ask about it"
          >
            Open in Chat
          </button>
        )}
        <button type="button" className="link" disabled={busy || rec.status === "running"} onClick={() => void again()}>
          {busy ? "Reviewing…" : "Review again"}
        </button>
      </div>
      {error && <p className="issue small">{error}</p>}
      {rec.actions.length > 0 && (
        <div className="triage-cards">
          {rec.actions.map((a, i) => (
            <div key={`${rec.id}-${i}`} className="review-card">
              <ActionCard
                action={a}
                state={isApplied(rec, i) ? "applied" : (states[i]?.state ?? "pending")}
                error={states[i]?.error}
                result={states[i]?.result ?? appliedNote(rec, i)}
                onApply={(input) => void apply(i, input)}
              />
              {canProbe(rec, i) && (
                <ProbeLine probe={rec.probes?.[String(i)]} busy={!!probing[i]} confidence={(a as { confidence?: string }).confidence} onProbe={() => void probe(i)} />
              )}
            </div>
          ))}
        </div>
      )}
    </section>
  );
}

function appliedNote(rec: ReviewRecord, i: number): string | undefined {
  if (!isApplied(rec, i)) return undefined;
  return rec.actions[i]?.type === "pipeline_lesson"
    ? `Lesson L${rec.lesson_added} is in the pipeline's lesson library (Lessons tab in the editor).`
    : "Applied; its effect is checked on the next runs (Lessons tab in the editor).";
}

function ProbeLine({ probe, busy, confidence, onProbe }: { probe?: Probe; busy: boolean; confidence?: string; onProbe: () => void }) {
  const st = probeStatus(probe);
  const running = busy || probe?.status === "running";
  return (
    <div className="probe">
      <div className="row probe-head">
        {st ? <Status s={running ? "running" : st.s}>{running ? "Probing" : st.word}</Status> : <span className="small muted">Not probed yet</span>}
        {confidence && <span className="small muted">confidence {confidence}</span>}
        <span className="spacer" />
        <button
          type="button"
          className="link"
          disabled={running}
          onClick={onProbe}
          title="Replay the node with this edit on this run's inputs and compare with what it did (counts against the run's budget)"
        >
          {probe ? "Probe again" : "Probe"}
        </button>
      </div>
      {probe?.status === "done" && <p className="small">{probe.reasoning}</p>}
      {probe?.error && <p className="small muted">{probe.error}</p>}
      {probe && probe.status === "done" && (
        <details>
          <summary className="small">
            {probe.node} #{probe.iteration}, {probe.samples} run, ${probe.cost_usd.toFixed(3)}
          </summary>
          {probe.notes.length > 0 && (
            <ul className="small muted">
              {probe.notes.map((n) => (
                <li key={n}>{n}</li>
              ))}
            </ul>
          )}
          {probe.edit && <pre className="mono small action-yaml">{probe.edit}</pre>}
          <div className="probe-sides">
            {(
              [
                ["Before", probe.baseline],
                ["With the edit", probe.candidate],
              ] as const
            ).map(([label, side]) =>
              side ? (
                <div key={label}>
                  <p className="small">
                    <strong>{label}</strong> <span className="muted">({side.status})</span>
                  </p>
                  {side.error && <p className="issue small">{side.error}</p>}
                  <pre className="mono small issue-text">{side.output}</pre>
                  {side.diff && (
                    <details>
                      <summary className="small">Code diff</summary>
                      <pre className="mono small issue-text">{side.diff}</pre>
                    </details>
                  )}
                </div>
              ) : null,
            )}
          </div>
        </details>
      )}
    </div>
  );
}

// -- lesson libraries: the editor's Lessons tab and the Lessons page ------------------------
export interface Library {
  pipeline: string;
  flow: string;
  file: string;
  lessons: Lesson[];
  adoptions: Adoption[];
}

/** How many runs the follow-up waits for ([review] followup_runs). */
function useFollowupRuns(): number {
  const [n, setN] = useState(5);
  useEffect(() => {
    api
      .reviewList({})
      .then((r) => setN(r.config.followup_runs))
      .catch(() => undefined);
  }, []);
  return n;
}

const verdictTone = (v: Adoption["verdict"]) => (v === "helped" ? "done" : v === "hurt" ? "dead" : v ? "skipped" : "running");

/** One pipeline's lessons and adopted edits, side by side. */
function LibraryView({ lib, followup, onReload, onChanged }: { lib: Library; followup: number; onReload: () => void; onChanged: () => void }) {
  const [error, setError] = useState<string | null>(null);
  const [undo, setUndo] = useState<Record<string, CardState>>({});
  const [showDismissed, setShowDismissed] = useState(false);
  const setStatus = async (x: Lesson, status: Lesson["status"]) => {
    setError(null);
    try {
      await api.lessonStatus(lib.pipeline, x.id, status);
      onReload();
    } catch (e) {
      setError(e instanceof ApiError ? e.message : String(e));
    }
  };
  const applyUndo = async (ad: Adoption, a: AssistantAction) => {
    setUndo((s) => ({ ...s, [ad.id]: { state: "applying" } }));
    try {
      await applyAction(a, deps);
      setUndo((s) => ({ ...s, [ad.id]: { state: "applied" } }));
      onChanged();
    } catch (e) {
      setUndo((s) => ({ ...s, [ad.id]: { state: "failed", error: e instanceof Error ? e.message : String(e) } }));
    }
  };
  const dismissed = lib.lessons.filter((x) => x.status === "dismissed").length;
  const shown = showDismissed ? lib.lessons : lib.lessons.filter((x) => x.status !== "dismissed");
  return (
    <>
      {error && <p className="issue">{error}</p>}
      <div className="learning">
        <section>
          <div className="row">
            <h3>Lessons</h3>
            <span className="spacer" />
            {dismissed > 0 && (
              <button type="button" className="link small" onClick={() => setShowDismissed((v) => !v)}>
                {showDismissed ? "Hide dismissed" : `Show dismissed (${dismissed})`}
              </button>
            )}
          </div>
          {shown.length === 0 && (
            <p className="muted small">
              {dismissed > 0
                ? "No lessons in use: the ones here are dismissed."
                : "No lessons yet. A run review proposes one as a card when a run teaches something reusable."}
            </p>
          )}
          <ul className="learning-list">
            {shown.map((x) => (
              <li key={x.id}>
                <div className="row">
                  <Status s={lessonStatus(x.status)}>{x.status}</Status>
                  <span className="mono small">
                    L{x.id} · {x.node} · {x.component}
                  </span>
                  <span className="spacer" />
                  <span className="small muted">
                    {x.confidence} confidence{x.added ? `, ${x.added}` : ""}
                  </span>
                </div>
                <p className="small">
                  <span className="muted">When</span> {x.cue}
                </p>
                <p className="small">
                  <strong>{x.lesson}</strong>
                </p>
                {x.evidence && <p className="small muted">Evidence: {x.evidence}</p>}
                {x.runs && (
                  <p className="small muted">
                    From{" "}
                    {x.runs.split(/[,\s]+/).filter(Boolean).map((r, i) => (
                      <span key={r}>
                        {i > 0 && ", "}
                        <a href={`#/runs/${encodeURIComponent(r)}`}>run {r}</a>
                      </span>
                    ))}
                  </p>
                )}
                {x.notes.length > 0 && <pre className="mono small action-yaml">{x.notes.join("\n")}</pre>}
                <div className="row">
                  {x.status !== "dismissed" ? (
                    <button type="button" className="link" onClick={() => void setStatus(x, "dismissed")} title="Later reviews and the Chat stop using it">
                      Dismiss
                    </button>
                  ) : (
                    <button type="button" className="link" onClick={() => void setStatus(x, "proposed")}>
                      Bring back
                    </button>
                  )}
                </div>
              </li>
            ))}
          </ul>
        </section>
        <section>
          <h3>Adopted edits</h3>
          {lib.adoptions.length === 0 && <p className="muted small">None yet. Apply a review's edit card on a run to see here whether it helped.</p>}
          <ul className="learning-list">
            {lib.adoptions.map((ad) => (
              <li key={ad.id}>
                <div className="row">
                  <Status s={verdictTone(ad.verdict)}>{ad.verdict ?? "checking"}</Status>
                  <span className="mono small">{ad.node ?? "pipeline"}</span>
                  <span className="spacer" />
                  <span className="small muted">{formatTime(ad.at)}</span>
                </div>
                <p className="small">{ad.summary}</p>
                <p className="small muted">
                  {adoptionLine(ad, followup)} · from <a href={`#/runs/${encodeURIComponent(ad.run)}`}>run {ad.run}</a>
                </p>
                {ad.actions.map((a, i) => (
                  <ActionCard
                    key={i}
                    action={a}
                    state={undo[ad.id]?.state ?? "pending"}
                    error={undo[ad.id]?.error}
                    onApply={() => void applyUndo(ad, a)}
                  />
                ))}
              </li>
            ))}
          </ul>
        </section>
      </div>
    </>
  );
}

/** The editor's Lessons tab: the open pipeline's library. */
export function LessonsTab({ name, onChanged }: { name: string; onChanged: () => void }) {
  const [lib, setLib] = useState<Library | null>(null);
  const [error, setError] = useState<string | null>(null);
  const followup = useFollowupRuns();
  const load = useCallback(() => {
    setError(null);
    api
      .lessons(name)
      .then(setLib)
      .catch((e) => setError(e instanceof ApiError ? e.message : String(e)));
  }, [name]);
  useEffect(load, [load]);
  return (
    <div className="lessons-tab" aria-label="Lessons">
      <div className="row lessons-head">
        <p className="small muted">
          What run reviews learned about this pipeline, kept in <span className="mono">{lib?.file ?? "lessons.md"}</span> next to it. Later reviews and the Chat
          read the lessons; they never change a prompt by themselves. Edits are applied from a run's Review panel. Every pipeline's lessons:{" "}
          <a href="#/lessons">Lessons</a>.
        </p>
        <span className="spacer" />
        <button type="button" className="link" onClick={load}>
          Refresh
        </button>
      </div>
      {error && <p className="issue">{error}</p>}
      {lib && <LibraryView lib={lib} followup={followup} onReload={load} onChanged={onChanged} />}
    </div>
  );
}

/** The Lessons page (top bar): every pipeline's lessons and adopted edits. */
export function LessonsPage() {
  const [libs, setLibs] = useState<Library[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const followup = useFollowupRuns();
  const load = useCallback(() => {
    setError(null);
    api
      .allLessons()
      .then((r) => setLibs(r.pipelines))
      .catch((e) => setError(e instanceof ApiError ? e.message : String(e)));
  }, []);
  useEffect(load, [load]);
  const all = libs ?? [];
  const sum = librarySummary(all);
  return (
    <div className="lessons-page" aria-label="Lessons">
      <div className="row lessons-head">
        <h2>Lessons</h2>
        <span className="spacer" />
        <button type="button" className="link" onClick={load}>
          Refresh
        </button>
      </div>
      <p className="small muted">
        What run reviews learned about each pipeline (its <span className="mono">lessons.md</span>), and whether the edits applied from reviews helped. Later
        reviews and the Chat read the lessons; they never change a prompt by themselves.
      </p>
      {error && <p className="issue">{error}</p>}
      {libs && all.length > 0 && (
        <div className="row lessons-summary">
          <span>
            {sum.lessons} lesson{sum.lessons === 1 ? "" : "s"} in {sum.pipelines} pipeline{sum.pipelines === 1 ? "" : "s"}
          </span>
          {sum.edits > 0 && (
            <>
              <Status s="running">{`${sum.checking} checking`}</Status>
              <Status s="done">{`${sum.helped} helped`}</Status>
              <Status s="dead">{`${sum.hurt} hurt`}</Status>
              <Status s="skipped">{`${sum.noEffect} no effect`}</Status>
            </>
          )}
        </div>
      )}
      {libs && all.length === 0 && (
        <p className="muted">
          Nothing learned yet. Runs that go the hard way (review loops, rejections, retries, cost spikes) are reviewed automatically; a review proposes
          lessons and edits as cards on the run. A run's Review button reviews any run.
        </p>
      )}
      {all.map((lib) => (
        <section key={lib.pipeline} className="lessons-pipeline" aria-label={lib.flow || lib.pipeline}>
          <div className="row">
            <h3>{lib.flow || lib.pipeline}</h3>
            <span className="mono small muted">{lib.file}</span>
            <span className="spacer" />
            <a className="small" href={`#/pipelines/${lib.pipeline}`}>
              Open pipeline
            </a>
          </div>
          <LibraryView lib={lib} followup={followup} onReload={load} onChanged={load} />
        </section>
      ))}
    </div>
  );
}
