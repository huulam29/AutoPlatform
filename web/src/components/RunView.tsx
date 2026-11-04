import {
  Background,
  BackgroundVariant,
  Controls,
  ReactFlow,
  ReactFlowProvider,
  applyNodeChanges,
  type NodeChange,
} from "@xyflow/react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { ApiError, api } from "../api";
import { toFlow, type FlowEdge, type FlowNode } from "../lib/graph";
import {
  TERMINAL,
  canRetry,
  colorGroup,
  type ColorGroup,
  formatCost,
  formatTime,
  latestByNode,
  lineageFor,
  runSummary,
  withRunState,
} from "../lib/runs";
import { Icon, Status, StatusIcon, groupIcon } from "./Icon";
import { FeedbackBox, MetricsPanel } from "./RunFeedback";
import type {
  ArtifactValue,
  Execution,
  JournalEntry,
  LineageItem,
  Run,
  RunDetail,
  TaskInfo,
} from "../types";
import {
  CollapseButton,
  GrowingMiniMap,
  KeepCenterOnResize,
  PanelHeader,
  SidePanel,
  panels,
  usePanelCollapsed,
} from "./Chrome";
import { nodeTypes } from "./PipelineNode";
import { RunChat } from "./RunChat";
import { StartRunButton } from "./StartRun";
import { ReviewChip, ReviewPanel, useReviews } from "./Review";
import { TriageChip, TriagePanel, useTriage } from "./Triage";
import { latestReview } from "../lib/review";
import { latestTriage } from "../lib/triage";

const TRIAGED = new Set(["dead", "needs-review"]);

const STATUSES = ["", "running", "waiting", "needs-review", "failed", "done", "dead", "cancelled"];

export function RunList({ selected, onSelect }: { selected?: string; onSelect: (id: string) => void }) {
  const [status, setStatus] = useState("");
  const [runs, setRuns] = useState<Run[]>([]);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    let live = true;
    const tick = () =>
      api
        .runs(status)
        .then((r) => live && (setRuns(r.runs), setError(null)))
        .catch((e: Error) => live && setError(e.message));
    void tick();
    const t = setInterval(tick, 5000);
    return () => {
      live = false;
      clearInterval(t);
    };
  }, [status]);
  return (
    <SidePanel name="runs" side="left" label="Runs" className="list">
      <div className="list-head">
        <h4>Runs</h4>
        <StartRunButton onStarted={(id) => onSelect(id)} />
        <select value={status} onChange={(e) => setStatus(e.target.value)} aria-label="status">
          {STATUSES.map((s) => (
            <option key={s} value={s}>
              {s || "all"}
            </option>
          ))}
        </select>
        <CollapseButton name="runs" side="left" label="Runs" />
      </div>
      {error && <p className="error">{error}</p>}
      {!runs.length && !error && <p className="muted pad">No runs yet.</p>}
      {runs.map((r) => (
        <button
          key={r.id}
          className={`list-item ${r.id === selected ? "on" : ""}`}
          onClick={() => onSelect(r.id)}
        >
          <StatusIcon s={r.status} />
          <span className="grow">
            <span className="mono small">{r.id}</span>
            <span className="muted small">
              {r.flow} · {formatTime(r.created_at)}
            </span>
          </span>
          <span className="small muted">{r.status}</span>
        </button>
      ))}
    </SidePanel>
  );
}

export function RunView({ id }: { id: string }) {
  const [detail, setDetail] = useState<RunDetail | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [node, setNode] = useState<string | null>(null);
  const [refresh, setRefresh] = useState(0); // bumped after a retry: poll again now
  const triage = useTriage(id, detail?.run.status, detail?.run.updated_at);
  const reviews = useReviews(id, detail?.run.status, detail?.run.updated_at);
  const detailCollapsed = usePanelCollapsed("run-detail");
  const [attention, setAttention] = useState(false);
  const collapsedRef = useRef(detailCollapsed);
  collapsedRef.current = detailCollapsed;
  useEffect(() => {
    if (node && collapsedRef.current) setAttention(true);
  }, [node]);
  useEffect(() => {
    if (!detailCollapsed) setAttention(false);
  }, [detailCollapsed]);
  useEffect(() => {
    let live = true;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const tick = async () => {
      try {
        const d = await api.run(id);
        if (!live) return;
        setDetail(d);
        setError(null);
        if (!TERMINAL.has(d.run.status)) timer = setTimeout(tick, 2000); // poll while live
      } catch (e) {
        if (live) setError((e as Error).message);
        timer = setTimeout(tick, 2000);
      }
    };
    void tick();
    return () => {
      live = false;
      if (timer) clearTimeout(timer);
    };
  }, [id, refresh]);

  const graph = useMemo(() => {
    if (!detail?.pipeline) return null;
    const g = toFlow({ ...detail.pipeline.model, issues: [] });
    return { nodes: withRunState(g.nodes, detail), edges: g.edges };
  }, [detail]);
  // React Flow reports node sizes through onNodesChange; keep them (across the 2 s polls) so the
  // minimap, which only draws measured nodes, has something to draw
  const [rfNodes, setRfNodes] = useState<FlowNode[]>([]);
  useEffect(() => {
    setRfNodes((prev) => {
      const measured = new Map(prev.map((n) => [n.id, n.measured]));
      return (graph?.nodes ?? []).map((n) => ({
        ...n,
        measured: measured.get(n.id),
        selected: n.id === node,
      }));
    });
  }, [graph, node]);
  const onNodesChange = useCallback(
    (changes: NodeChange<FlowNode>[]) =>
      setRfNodes((ns) => applyNodeChanges(changes.filter((c) => c.type === "dimensions"), ns)),
    [],
  );

  if (error && !detail) return <div className="error pad">{error}</div>;
  if (!detail) return <div className="muted pad">Loading…</div>;
  const r = detail.run;
  const gatekeeper = detail.pipeline?.model.nodes.find((n) => n.type === "chat")?.id;
  return (
    <div className="run">
      <div className="toolbar">
        <strong className="mono run-id">{r.id}</strong>
        <span className="run-flow">{r.flow}</span>
        <Status s={r.status} pill />
        <span className="muted">
          cost {formatCost(r.cost_usd)}
          {r.budget_usd != null ? ` / ${formatCost(r.budget_usd)}` : ""}
        </span>
        <RunSummary detail={detail} />
        {(r.status === "dead" || r.status === "needs-review") && (
          <a className="button-link" href={`#/fix/run/${r.id}`} title="Ask the Chat to find the cause and propose a fix">
            Fix with Chat
          </a>
        )}
        {latestTriage(triage.records) && (
          <TriageChip
            record={latestTriage(triage.records)!}
            onClick={() => {
              setNode(latestTriage(triage.records)?.node ?? null);
              panels.set("run-detail", false);
            }}
          />
        )}
        {latestReview(reviews.records) && (
          <ReviewChip
            record={latestReview(reviews.records)!}
            onClick={() => {
              setNode(null);
              panels.set("run-detail", false);
            }}
          />
        )}
        <span className="spacer" />
        {!r.terminal && <span className="muted small">refreshing every 2 s</span>}
      </div>
      {r.error && <div className="banner banner-error">{r.error}</div>}
      <Commands detail={detail} />
      <div className="run-body">
        <div className="canvas">
          {graph ? (
            <ReactFlowProvider>
              <ReactFlow<FlowNode, FlowEdge>
                nodes={rfNodes}
                onNodesChange={onNodesChange}
                edges={graph.edges}
                nodeTypes={nodeTypes}
                nodesConnectable={false}
                nodesDraggable={false}
                elementsSelectable
                onNodeClick={(_, n) => setNode(n.id)}
                onNodeDoubleClick={(_, n) => {
                  setNode(n.id);
                  panels.set("run-detail", false);
                }}
                onPaneClick={() => setNode(null)}
                fitView
                fitViewOptions={{ padding: 0.15, minZoom: 0.2, maxZoom: 1 }}
                minZoom={0.2}
                proOptions={{ hideAttribution: true }}
              >
                <Background variant={BackgroundVariant.Lines} gap={24} color="var(--canvas-grid)" />
                <Controls showInteractive={false} />
                <GrowingMiniMap />
                <KeepCenterOnResize />
              </ReactFlow>
            </ReactFlowProvider>
          ) : (
            <p className="muted pad">
              No pipeline file named <code>{r.flow}</code> in the pipelines directory, so the graph
              can't be drawn. Executions:
            </p>
          )}
        </div>
        <SidePanel
          name="run-detail"
          side="right"
          label="Run details"
          className="side wide"
          attention={attention}
        >
          <PanelHeader name="run-detail" side="right" label="Run details" />
          {gatekeeper && (!node || node === gatekeeper) && (
            <div className="panel">
              <RunChat runId={r.id} agentName={gatekeeper} onRunChanged={() => setRefresh((n) => n + 1)} />
            </div>
          )}
          {node ? (
            <NodeRun detail={detail} nodeId={node} onRetried={() => setRefresh((n) => n + 1)} triage={triage} />
          ) : (
            <>
              {(TRIAGED.has(r.status) || triage.records.length > 0) && (
                <div className="panel">
                  <TriagePanel run={r.id} {...triage} status={r.status} onRunChanged={() => setRefresh((n) => n + 1)} />
                </div>
              )}
              {(r.terminal || reviews.records.length > 0) && (
                <div className="panel">
                  <ReviewPanel run={r.id} status={r.status} {...reviews} />
                </div>
              )}
              <div className="panel">
                <FeedbackBox runId={r.id} />
              </div>
              <MetricsPanel runId={r.id} version={r.updated_at} />
              <ExecTable detail={detail} onPick={setNode} />
            </>
          )}
        </SidePanel>
      </div>
    </div>
  );
}

function Commands({ detail }: { detail: RunDetail }) {
  const cmds = detail.next_commands.filter((c) => c.node !== null);
  if (!cmds.length) return null;
  return (
    <div className="banner banner-info">
      <strong>Next (CLI only):</strong>
      {cmds.map((c) => (
        <div key={c.command} className="cmd">
          <code>{c.command}</code>
          <button
            className="link"
            onClick={() => void navigator.clipboard?.writeText(c.command)}
            title="copy"
          >
            copy
          </button>
          <span className="muted small">{c.description}</span>
        </div>
      ))}
    </div>
  );
}

function ExecTable({ detail, onPick }: { detail: RunDetail; onPick: (id: string) => void }) {
  return (
    <div className="panel">
      <h3>Executions</h3>
      <table className="table">
        <thead>
          <tr>
            <th>node</th>
            <th>status</th>
            <th>try</th>
            <th>cost</th>
          </tr>
        </thead>
        <tbody>
          {detail.executions.map((x) => (
            <tr key={x.id} onClick={() => onPick(x.node_id)} className="clickable">
              <td className="mono">{x.key}</td>
              <td>
                <Status s={x.status} />
              </td>
              <td>{x.attempt}</td>
              <td>{formatCost(x.cost_usd)}</td>
            </tr>
          ))}
        </tbody>
      </table>
      <p className="help">Click a node for its inputs, outputs and transcript.</p>
    </div>
  );
}

function RunSummary({ detail }: { detail: RunDetail }) {
  const s = runSummary(detail);
  const parts: [ColorGroup, number, string][] = [
    ["run", s.nodes.run, "running"],
    ["pass", s.nodes.pass, "done"],
    ["attn", s.nodes.attn, "need you"],
    ["fail", s.nodes.fail, "failed"],
    ["wait", s.nodes.wait, "pending"],
    ["skip", s.nodes.skip, "skipped"],
  ];
  return (
    <span className="run-summary" aria-label="nodes by status">
      {parts
        .filter(([, n]) => n > 0)
        .map(([g, n, label]) => (
          <span key={g} className={`st st-${g} pill`} title={`${n} node(s) ${label}`}>
            <Icon name={groupIcon(g)} />
            {n} {label}
          </span>
        ))}
      {s.tasksTotal > Object.keys(detail.node_counts ?? {}).length && (
        <span className="st st-wait pill" title="tasks across for_each nodes">
          tasks: {s.tasksRunning} running / {s.tasksTotal}
          {s.tasksFailed ? `, ${s.tasksFailed} failed` : ""}
        </span>
      )}
    </span>
  );
}

function NodeRun({
  detail,
  nodeId,
  onRetried,
  triage,
}: {
  detail: RunDetail;
  nodeId: string;
  onRetried: () => void;
  triage: ReturnType<typeof useTriage>;
}) {
  const state = latestByNode(detail.executions).get(nodeId);
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<string | null>(null);
  const status = state?.latest.status;
  useEffect(() => setMsg(null), [status]);
  if (!state) return <p className="muted pad">{nodeId} has not run yet.</p>;
  const execs = [...state.all].sort((a, b) => b.iteration - a.iteration);
  const tasks = (detail.tasks ?? []).filter((t) => t.execution_id === state.latest.id);
  const counts = detail.node_counts?.[nodeId];
  return (
    <div className="panel">
      <div className="node-run-head">
        <h3>{nodeId}</h3>
        <Status s={state.latest.status} pill />
        {canRetry(state.latest.status) && (
          <button
            type="button"
            className="primary"
            disabled={busy}
            title={tasks.length ? "Rerun the failed tasks; finished ones are kept" : "Run this node again"}
            onClick={async () => {
              setBusy(true);
              try {
                const r = await api.retryRun(detail.run.id, nodeId);
                setMsg(r.retried ? "Retrying…" : "Nothing to retry.");
                onRetried();
              } catch (e) {
                setMsg(e instanceof ApiError ? e.message : String(e));
              } finally {
                setBusy(false);
              }
            }}
          >
            {tasks.length ? `Retry failed tasks (${counts?.failed ?? 0})` : "Retry node"}
          </button>
        )}
      </div>
      {msg && <p className="small muted">{msg}</p>}
      {state.latest.status === "done" && (
        <FeedbackBox runId={detail.run.id} node={nodeId} iteration={state.latest.iteration} />
      )}
      <TriagePanel run={detail.run.id} node={nodeId} {...triage} status={state.latest.status} onRunChanged={onRetried} />
      {tasks.length > 0 && <TaskTable tasks={tasks} />}
      <Journal runId={detail.run.id} nodeId={nodeId} version={detail.run.updated_at} />
      {execs.map((x) => (
        <ExecCard key={x.id} x={x} detail={detail} />
      ))}
    </div>
  );
}

function preview(v: unknown): string {
  const t = typeof v === "string" ? v : JSON.stringify(v);
  return t && t.length > 60 ? `${t.slice(0, 57)}…` : (t ?? "");
}

function TaskTable({ tasks }: { tasks: TaskInfo[] }) {
  return (
    <section className="exec">
      <h4>Tasks</h4>
      <table className="table tasks">
        <thead>
          <tr>
            <th>#</th>
            <th>status</th>
            <th>tries</th>
            <th>item</th>
          </tr>
        </thead>
        <tbody>
          {tasks.map((t) => (
            <tr key={t.idx} className={`run-${colorGroup(t.status)}`}>
              <td>{t.idx}</td>
              <td>
                <Status s={t.status} />
              </td>
              <td>{t.attempts}</td>
              <td className="mono small" title={t.error ?? undefined}>
                {preview(t.item)}
                {t.error && <div className="issue small">{t.error}</div>}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </section>
  );
}

function Journal({ runId, nodeId, version }: { runId: string; nodeId: string; version: number }) {
  const [entries, setEntries] = useState<JournalEntry[] | null>(null);
  const [open, setOpen] = useState(true);
  useEffect(() => {
    let live = true;
    api
      .journal(runId, nodeId)
      .then((r) => live && setEntries(r.journal))
      .catch(() => live && setEntries([]));
    return () => {
      live = false;
    };
  }, [runId, nodeId, version]);
  return (
    <section className="exec">
      <h4>
        <button type="button" className="link" onClick={() => setOpen((v) => !v)}>
          {open ? "▾" : "▸"} Journal {entries ? `(${entries.length})` : ""}
        </button>
      </h4>
      {open && entries && (
        <ol className="journal">
          {[...entries].reverse().map((e) => (
            <li key={e.id} className={`j-${e.kind}`}>
              <span className="muted small">{formatTime(e.at)}</span>{" "}
              {e.iteration > 1 && <span className="badge">#{e.iteration}</span>}{" "}
              {e.task != null && <span className="badge">task {e.task}</span>} {e.message}
              {typeof e.detail.error === "string" && <div className="issue small">{e.detail.error}</div>}
            </li>
          ))}
          {entries.length === 0 && <li className="muted">No history yet.</li>}
        </ol>
      )}
    </section>
  );
}

function ExecCard({ x, detail }: { x: Execution; detail: RunDetail }) {
  const lin = lineageFor(detail, x.id);
  const attachments = (x.detail.attachments ?? {}) as Record<
    string,
    { ref: string; size: number; summary?: string }
  >;
  const rest = Object.fromEntries(Object.entries(x.detail).filter(([k]) => k !== "attachments"));
  return (
    <section className="exec">
      <h4>
        <span className="mono">{x.key}</span>{" "}
        <Status s={x.status} pill />
      </h4>
      <dl className="kv">
        <dt>attempt</dt>
        <dd>
          {x.attempt}
          {x.reclaims ? ` (${x.reclaims} reclaims)` : ""}
        </dd>
        <dt>cost</dt>
        <dd>{formatCost(x.cost_usd)}</dd>
        <dt>started</dt>
        <dd>{formatTime(x.started_at)}</dd>
        <dt>finished</dt>
        <dd>{formatTime(x.finished_at)}</dd>
        {x.reason && (
          <>
            <dt>reason</dt>
            <dd>{x.reason}</dd>
          </>
        )}
      </dl>
      {x.error && <pre className="error">{x.error}</pre>}
      {lin && lin.reads.length > 0 && (
        <>
          <h5>Inputs</h5>
          {lin.reads.map((r) => (
            <ArtifactRow key={`r-${r.name}-${r.hash}`} item={r} />
          ))}
        </>
      )}
      {lin && lin.writes.length > 0 && (
        <>
          <h5>Outputs</h5>
          {lin.writes
            .filter((w) => !(w.name in attachments))
            .map((w) => (
              <ArtifactRow key={`w-${w.name}-${w.hash}`} item={w} />
            ))}
        </>
      )}
      {Object.keys(attachments).length > 0 && (
        <>
          <h5>Transcript and attachments</h5>
          {Object.entries(attachments).map(([name, a]) => (
            <ArtifactRow
              key={name}
              item={{ name, hash: a.ref, summary: a.summary ?? "", size: a.size }}
            />
          ))}
        </>
      )}
      {Object.keys(rest).length > 0 && (
        <details>
          <summary>detail</summary>
          <pre className="mono small">{JSON.stringify(rest, null, 2)}</pre>
        </details>
      )}
    </section>
  );
}

function ArtifactRow({ item }: { item: LineageItem }) {
  const [open, setOpen] = useState(false);
  const [value, setValue] = useState<ArtifactValue | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const toggle = () => {
    setOpen(!open);
    if (!value && !open)
      api
        .artifact(item.hash)
        .then(setValue)
        .catch((e: Error) => setErr(e.message));
  };
  return (
    <div className="artifact">
      <button className="link artifact-head" onClick={toggle} aria-expanded={open}>
        {open ? "▾" : "▸"} <code>{item.name}</code>
        {item.from && <span className="muted small"> from {item.from}</span>}
        {item.size != null && <span className="muted small"> · {item.size} B</span>}
      </button>
      <div className="summary small">{item.summary}</div>
      {open && (
        <div className="artifact-body">
          {err && <p className="error">{err}</p>}
          {!value && !err && <p className="muted">Loading…</p>}
          {value && (
            <>
              <pre className="mono small">
                {value.encoding === "json"
                  ? JSON.stringify(value.value, null, 2)
                  : value.encoding === "text"
                    ? String(value.value)
                    : `(binary, ${value.media_type}, base64)\n${String(value.value).slice(0, 2000)}`}
              </pre>
              {value.truncated && <p className="muted small">Truncated at 1 MiB.</p>}
            </>
          )}
        </div>
      )}
    </div>
  );
}
