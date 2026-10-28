import {
  Background,
  BackgroundVariant,
  Controls,
  ReactFlow,
  ReactFlowProvider,
  applyNodeChanges,
  useReactFlow,
  type Connection,
  type NodeChange,
} from "@xyflow/react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { ApiError, api } from "../api";
import {
  addNode,
  connect,
  forSave,
  indexIssues,
  issueTarget,
  removeEdge,
  removeNode,
  renameNode,
  setFields,
  toFlow,
  updateEdge,
  type FlowEdge,
  type FlowNode,
} from "../lib/graph";
import { compatibleModels, BUILTIN_HARNESS, setDefault } from "../lib/ai";
import { editorLink } from "../lib/assistant";
import {
  type Defaults,
  type Issue,
  type Model,
  type NodeType,
  type NodeTypes,
} from "../types";
import { HarnessSelect, IssueLines, ModelSelect } from "./AiPicker";
import {
  AddNodeButton,
  GrowingMiniMap,
  KeepCenterOnResize,
  PanelHeader,
  SidePanel,
  panels,
  usePanelCollapsed,
} from "./Chrome";
import { JsonField, NumberField, SelectField } from "./fields";
import { setStart, startInput, startNode, startNodes } from "../lib/start";
import { DryRunButton } from "./DryRun";
import { HistoryButton } from "./History";
import { LessonsTab } from "./Review";
import { ExportButton } from "./ImportExport";
import { EdgePanel, NodePanel } from "./Panels";
import { nodeTypes } from "./PipelineNode";

const DND_TYPE = "application/x-agentline-node";

type Selection = { kind: "node"; id: string } | { kind: "edge"; index: number } | null;

function useDebounced<T>(value: T, ms: number): T {
  const [v, setV] = useState(value);
  useEffect(() => {
    const t = setTimeout(() => setV(value), ms);
    return () => clearTimeout(t);
  }, [value, ms]);
  return v;
}

export function Editor(props: { name: string; types: NodeTypes; onDeleted?: (removed: string) => void }) {
  return (
    <ReactFlowProvider>
      <EditorInner key={props.name} {...props} />
    </ReactFlowProvider>
  );
}

function EditorInner({ name, types, onDeleted }: { name: string; types: NodeTypes; onDeleted?: (removed: string) => void }) {
  const flow = useReactFlow();
  const [base, setBase] = useState<string | null>(null); // the file on disk
  const [model, setModel] = useState<Model | null>(null);
  const [dirty, setDirty] = useState(false);
  const [issues, setIssues] = useState<Issue[]>([]);
  const [preview, setPreview] = useState<string>("");
  const [warnings, setWarnings] = useState<string[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [conflict, setConflict] = useState(false);
  const [saving, setSaving] = useState(false);
  const [tab, setTab] = useState<"graph" | "yaml" | "lessons">("graph");
  const [yaml, setYaml] = useState<string | null>(null); // unsaved YAML tab edits
  const [selection, setSelection] = useState<Selection>(null);
  const [nodes, setNodes] = useState<FlowNode[]>([]);
  const seq = useRef(0);
  const canvasRef = useRef<HTMLDivElement>(null);
  const reveal = useRef<string | null>(null); // a just-added node to pan to once laid out
  const inspectorCollapsed = usePanelCollapsed("inspector");
  // a selection made while the inspector is collapsed flags its rail instead of opening it
  const [attention, setAttention] = useState(false);
  const selKey = !selection
    ? ""
    : selection.kind === "node"
      ? `n:${selection.id}`
      : `e:${selection.index}`;
  const collapsedRef = useRef(inspectorCollapsed);
  collapsedRef.current = inspectorCollapsed;
  useEffect(() => {
    // only a new selection flags it, not collapsing the panel with one already made
    if (selKey && collapsedRef.current) setAttention(true);
  }, [selKey]);
  useEffect(() => {
    if (!inspectorCollapsed) setAttention(false);
  }, [inspectorCollapsed]);

  const load = useCallback(async () => {
    setError(null);
    setConflict(false);
    try {
      const p = await api.pipeline(name);
      setBase(p.source);
      setPreview(p.source);
      setModel(p.model);
      setIssues(p.model.issues);
      setDirty(false);
      setYaml(null);
      setWarnings([]);
    } catch (e) {
      setError(String((e as Error).message));
    }
  }, [name]);
  useEffect(() => {
    void load();
  }, [load]);
  // the assistant sees what's open (unsaved edits included) and reloads it after writing it
  useEffect(() => {
    if (base === null) return;
    editorLink.publish({ name, source: yaml ?? preview, dirty });
  }, [name, base, yaml, preview, dirty]);
  useEffect(() => () => editorLink.publish(null), [name]);
  useEffect(() => editorLink.onChanged((n) => n === name && void load()), [name, load]);

  // server-side validation of every change (debounced); also gives the YAML preview
  const pending = useDebounced(dirty ? model : null, 400);
  useEffect(() => {
    if (!pending || base === null) return;
    const n = ++seq.current;
    api
      .validate(base, name, forSave(pending))
      .then((r) => {
        if (n !== seq.current) return;
        setIssues(r.issues);
        setPreview(r.source);
      })
      .catch((e: Error) => n === seq.current && setError(e.message));
  }, [pending, base, name]);
  const pendingYaml = useDebounced(yaml, 400);
  useEffect(() => {
    if (pendingYaml === null) return;
    const n = ++seq.current;
    api
      .validate(pendingYaml, name)
      .then((r) => n === seq.current && setIssues(r.issues))
      .catch((e: Error) => n === seq.current && setError(e.message));
  }, [pendingYaml, name]);

  const edit = (next: Model) => {
    setModel(next);
    setDirty(true);
  };

  const graph = useMemo(
    () => (model ? toFlow({ ...model, issues }, types.ai) : { nodes: [], edges: [] }),
    [model, issues, types.ai],
  );
  useEffect(() => {
    setNodes(
      graph.nodes.map((n) => ({
        ...n,
        selected: selection?.kind === "node" && selection.id === n.id,
      })),
    );
    // selection is applied on the next graph change; clicks select natively
  }, [graph]);
  const edges: FlowEdge[] = useMemo(
    () =>
      graph.edges.map((e) => ({
        ...e,
        selected: selection?.kind === "edge" && selection.index === e.data?.index,
      })),
    [graph, selection],
  );

  const onNodesChange = useCallback(
    (changes: NodeChange<FlowNode>[]) =>
      setNodes((ns) => applyNodeChanges(changes.filter((c) => c.type !== "remove"), ns)),
    [],
  );

  const focus = (id: string) => {
    const n = nodes.find((x) => x.id === id);
    setSelection({ kind: "node", id });
    setTab("graph");
    setNodes((ns) => ns.map((x) => ({ ...x, selected: x.id === id })));
    if (n) flow.setCenter(n.position.x + 100, n.position.y + 32, { zoom: 1.2, duration: 300 });
  };

  const add = (type: NodeType) => {
    if (!model) return;
    const next = addNode(model, type);
    const id = next.nodes[next.nodes.length - 1].id;
    edit(next);
    setSelection({ kind: "node", id });
    reveal.current = id;
  };
  // layout is automatic, so a new node lands where the layout puts it: pan there (keeping the
  // zoom) so it shows up in the middle of the view, selected
  useEffect(() => {
    const id = reveal.current;
    const n = id ? nodes.find((x) => x.id === id) : undefined;
    if (!n) return;
    reveal.current = null;
    setNodes((ns) => ns.map((x) => ({ ...x, selected: x.id === n.id })));
    void flow.setCenter(n.position.x + 100, n.position.y + 32, {
      zoom: flow.getZoom(),
      duration: 250,
    });
  }, [nodes, flow]);

  const save = async () => {
    if (!model || base === null) return;
    setSaving(true);
    setError(null);
    try {
      const r = await api.saveModel(name, forSave(model), base);
      setBase(r.source);
      setPreview(r.source);
      setModel(r.model);
      setIssues(r.issues);
      setWarnings(r.warnings);
      setDirty(false);
      if (selection?.kind === "node" && !r.model.nodes.some((n) => n.id === selection.id))
        setSelection(null);
    } catch (e) {
      if (e instanceof ApiError && e.status === 409) setConflict(true);
      else setError((e as Error).message);
    } finally {
      setSaving(false);
    }
  };

  const saveYaml = async () => {
    if (yaml === null) return;
    setSaving(true);
    try {
      const r = await api.saveSource(name, yaml);
      setBase(r.source);
      setPreview(r.source);
      setModel(r.model);
      setIssues(r.issues);
      setYaml(null);
      setDirty(false);
      setWarnings([]);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setSaving(false);
    }
  };

  if (error && !model) return <div className="error">{error}</div>;
  if (!model) return <div className="muted pad">Loading…</div>;

  const idx = indexIssues(issues);
  const selectedNode =
    selection?.kind === "node" ? model.nodes.find((n) => n.id === selection.id) : undefined;

  return (
    <div className="editor">
      <div className="toolbar">
        <strong>{model.name ?? name}</strong>
        <span className="muted">{name}.yaml</span>
        <div className="tabs">
          <button className={tab === "graph" ? "on" : ""} onClick={() => setTab("graph")}>
            Graph
          </button>
          <button className={tab === "yaml" ? "on" : ""} onClick={() => setTab("yaml")}>
            YAML{yaml !== null ? " •" : ""}
          </button>
          <button
            className={tab === "lessons" ? "on" : ""}
            onClick={() => setTab("lessons")}
            title="What run reviews learned about this pipeline, and whether the edits adopted from them helped"
          >
            Lessons
          </button>
        </div>
        <span className="spacer" />
        {issues.length > 0 ? (
          <span className="badge badge-issue">{issues.length} issues</span>
        ) : (
          <span className="badge badge-ok">valid</span>
        )}
        <DryRunButton name={name} model={model} source={yaml ?? preview} />
        <HistoryButton name={name} current={base ?? ""} onRestored={() => void load()} />
        <ExportButton name={name} />
        <button
          type="button"
          className="danger"
          title="Delete this pipeline (its own folder too); pipeline history keeps a copy"
          onClick={async () => {
            const unsaved = dirty ? " Unsaved changes are lost." : "";
            if (!window.confirm(`Delete pipeline ${name}?${unsaved} Pipeline history keeps a copy.`)) return;
            try {
              const r = await api.deletePipeline(name);
              onDeleted?.(r.removed);
            } catch (e) {
              window.alert(`Couldn't delete ${name}: ${(e as Error).message}`);
            }
          }}
        >
          Delete
        </button>
        {tab !== "yaml" ? (
          <>
            <button disabled={!dirty || saving} onClick={() => void load()}>
              Discard
            </button>
            <button
              className="primary"
              disabled={!dirty || saving || yaml !== null}
              onClick={() => void save()}
            >
              {saving ? "Saving…" : dirty ? "Save" : "Saved"}
            </button>
          </>
        ) : (
          <>
            <button disabled={yaml === null || saving} onClick={() => setYaml(null)}>
              Discard
            </button>
            <button
              className="primary"
              disabled={yaml === null || saving}
              onClick={() => void saveYaml()}
            >
              Save YAML
            </button>
          </>
        )}
      </div>
      {conflict && (
        <div className="banner banner-error">
          The file changed on disk since it was loaded. Your graph edits were not saved.{" "}
          <button onClick={() => void load()}>Reload from disk</button>
        </div>
      )}
      {error && <div className="banner banner-error">{error}</div>}
      {warnings.length > 0 && (
        <div className="banner banner-warn">
          Saved, but some comments could not be kept:
          <ul>
            {warnings.map((w) => (
              <li key={w}>{w}</li>
            ))}
          </ul>
          <button onClick={() => setWarnings([])}>Dismiss</button>
        </div>
      )}
      {tab === "graph" && yaml !== null && (
        <div className="banner banner-warn">
          The YAML tab has unsaved edits; save or discard them before editing the graph.
        </div>
      )}
      <div className="editor-body">
        {tab === "graph" ? (
          <>
            <div
              className="canvas"
              ref={canvasRef}
              tabIndex={-1}
              aria-label="pipeline graph"
              onDragOver={(e) => {
                if (e.dataTransfer.types.includes(DND_TYPE)) {
                  e.preventDefault();
                  e.dataTransfer.dropEffect = "copy";
                }
              }}
              onDrop={(e) => {
                const t = e.dataTransfer.getData(DND_TYPE) as NodeType;
                if (t && yaml === null) {
                  e.preventDefault();
                  add(t);
                }
              }}
            >
              <AddNodeButton
                onAdd={add}
                disabled={yaml !== null}
                dndType={DND_TYPE}
                canvasRef={canvasRef}
              />
              <ReactFlow<FlowNode, FlowEdge>
                nodes={nodes}
                edges={edges}
                nodeTypes={nodeTypes}
                onNodesChange={onNodesChange}
                onConnect={(c: Connection) =>
                  c.source && c.target && yaml === null && edit(connect(model, c.source, c.target))
                }
                onNodeClick={(_, n) => setSelection({ kind: "node", id: n.id })}
                onNodeDoubleClick={(_, n) => {
                  setSelection({ kind: "node", id: n.id });
                  panels.set("inspector", false);
                }}
                onEdgeClick={(_, e) =>
                  e.data && setSelection({ kind: "edge", index: e.data.index })
                }
                onPaneClick={() => {
                  setSelection(null);
                  canvasRef.current?.focus({ preventScroll: true });
                }}
                onNodesDelete={(ns) => {
                  if (yaml !== null) return;
                  let m = model;
                  for (const n of ns) m = removeNode(m, n.id);
                  edit(m);
                  setSelection(null);
                }}
                onEdgesDelete={(es) => {
                  if (yaml !== null) return;
                  const gone = es
                    .map((e) => e.data?.index)
                    .filter((i): i is number => i !== undefined)
                    .sort((a, b) => b - a);
                  let m = model;
                  for (const i of gone) m = removeEdge(m, i);
                  edit(m);
                  setSelection(null);
                }}
                deleteKeyCode={["Delete", "Backspace"]}
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
            </div>
            <SidePanel
              name="inspector"
              side="right"
              label="Inspector"
              className="side"
              attention={attention}
            >
              <PanelHeader name="inspector" side="right" label="Inspector" />
              {selectedNode ? (
                <NodePanel
                  node={selectedNode}
                  types={types}
                  defaults={model.defaults}
                  issues={idx.byNode.get(selectedNode.id) ?? []}
                  onFields={(f) => edit(setFields(model, selectedNode.id, f))}
                  onRename={(to) => {
                    const next = renameNode(model, selectedNode.id, to);
                    if (next !== model) {
                      edit(next);
                      setSelection({ kind: "node", id: to });
                    }
                  }}
                  onDelete={() => {
                    edit(removeNode(model, selectedNode.id));
                    setSelection(null);
                  }}
                />
              ) : selection?.kind === "edge" ? (
                <EdgePanel
                  model={model}
                  index={selection.index}
                  issues={idx.byEdge.get(selection.index) ?? []}
                  onChange={(p) => edit(updateEdge(model, selection.index, p))}
                  onDelete={() => {
                    edit(removeEdge(model, selection.index));
                    setSelection(null);
                  }}
                />
              ) : (
                <PipelinePanel
                  model={model}
                  types={types}
                  issues={idx.general}
                  onChange={edit}
                />
              )}
            </SidePanel>
          </>
        ) : tab === "lessons" ? (
          <LessonsTab name={name} onChanged={() => void load()} />
        ) : (
          <div className="yaml-tab">
            {dirty && yaml === null && (
              <p className="help">
                Preview of the file with your unsaved graph edits. Editing here replaces them.
              </p>
            )}
            <textarea
              className="mono yaml"
              spellCheck={false}
              value={yaml ?? preview}
              onChange={(e) => setYaml(e.target.value)}
              aria-label="pipeline YAML"
            />
          </div>
        )}
      </div>
      <IssueList issues={issues} model={model} onFocus={focus} />
    </div>
  );
}

function PipelinePanel({
  model,
  types,
  issues,
  onChange,
}: {
  model: Model;
  types: NodeTypes;
  issues: Issue[];
  onChange: (m: Model) => void;
}) {
  const d = model.defaults ?? null;
  const set = (key: keyof Defaults, v: string | number | undefined) =>
    onChange({ ...model, defaults: setDefault(d, key, v) });
  const dIssues = issues.filter((i) => i.path === "defaults" || i.path.startsWith("defaults."));
  const own = d?.harness ? types.ai?.harnesses.find((a) => a.name === d.harness)?.model : null;
  return (
    <div className="panel">
      <div className="panel-head">
        <h3>Pipeline</h3>
      </div>
      <dl className="kv">
        <dt>name</dt>
        <dd>{model.name}</dd>
        <dt>version</dt>
        <dd>{model.version}</dd>
        <dt>trigger</dt>
        <dd>
          <code>{JSON.stringify(model.trigger)}</code>
        </dd>
        <dt>budget</dt>
        <dd>
          <input
            type="number"
            aria-label="pipeline budget (USD per run)"
            step="any"
            min={0}
            defaultValue={model.budget_usd ?? ""}
            key={String(model.budget_usd)}
            onBlur={(e) => {
              const v = e.target.value === "" ? null : Number(e.target.value);
              if (v !== model.budget_usd) onChange({ ...model, budget_usd: v });
            }}
          />
        </dd>
        <dt>nodes</dt>
        <dd>{model.nodes.length}</dd>
        <dt>edges</dt>
        <dd>{model.edges.length}</dd>
      </dl>
      <fieldset className="group" aria-label="defaults">
        <legend>Defaults</legend>
        <p className="help">
          What agent nodes use when they don't pick their own. A node that picks a different
          harness uses that harness's own default model.
        </p>
        <label className="field">
          <span className="field-name">Harness</span>
          <HarnessSelect
            value={d?.harness}
            ai={types.ai}
            emptyLabel="(none: claude-code, or builtin when a node only sets a model)"
            onChange={(a) => set("harness", a)}
          />
        </label>
        <label className="field">
          <span className="field-name">Model</span>
          <ModelSelect
            value={d?.model}
            options={compatibleModels(types.ai, d?.harness ?? BUILTIN_HARNESS)}
            emptyLabel={own ? `(harness default: ${own})` : "(harness default)"}
            onChange={(m) => set("model", m)}
          />
          <span className="help">also the model of LLM routers that don't set one</span>
        </label>
        <label className="field">
          <span className="field-name">max_turns</span>
          <NumberField
            value={d?.max_turns}
            integer
            min={1}
            onChange={(v) => set("max_turns", v as number | undefined)}
          />
        </label>
        <IssueLines issues={dIssues} />
      </fieldset>
      <StartFields model={model} issues={issues} onChange={onChange} />
      <p className="help">
        Select a node or an edge to edit it. Harnesses and models are set up under{" "}
        <a href="#/settings">Settings</a>. Trigger edits: YAML tab.
      </p>
      <p className="help">
        Add nodes with the <strong>+</strong> button on the canvas (or press <kbd>A</kbd>). Drag
        from a node's bottom handle to another node to connect. Select and press Delete to remove.
        Layout is automatic. <kbd>\</kbd> hides or shows the side panels.
      </p>
    </div>
  );
}

/** `start:` — which node takes a run's input, and how (Start run popup, chat apps' /run). */
function StartFields({
  model,
  issues,
  onChange,
}: {
  model: Model;
  issues: Issue[];
  onChange: (m: Model) => void;
}) {
  const node = startNode(model);
  const input = startInput(model);
  const chat = model.nodes.find((n) => n.type === "chat");
  const sIssues = issues.filter((i) => i.path === "start" || i.path.startsWith("start."));
  return (
    <fieldset className="group" aria-label="start point">
      <legend>Start point</legend>
      <p className="help">
        The node that takes a run&apos;s input. A <strong>chat</strong> node is always the start
        point: you talk to it, it hands the request to the pipeline and reports back.
      </p>
      <div className="field" role="group" aria-label="start node">
        <span className="field-name">Node</span>
        <SelectField
          value={model.start?.node}
          options={startNodes(model)}
          onChange={(v) => onChange(setStart(model, "node", (v as string | undefined) || undefined))}
        />
        {!model.start?.node && node && <span className="help">(default: {node})</span>}
      </div>
      <div className="field" role="group" aria-label="start input">
        <span className="field-name">Input</span>
        {chat ? (
          <span>chat (the {chat.id} node)</span>
        ) : (
          <SelectField
            value={model.start?.input}
            options={["json", "text"]}
            onChange={(v) => onChange(setStart(model, "input", (v as "json" | "text" | undefined) || undefined))}
          />
        )}
        <span className="help">
          {input === "json"
            ? "JSON: the Start run popup is pre-filled with a template of the fields the pipeline reads"
            : input === "text"
              ? "text: plain words; an agent start node gets them as its ## Input ({{payload.text}})"
              : "chat: a conversation; the first message is {{payload.text}}"}
        </span>
      </div>
      {input === "json" && (
        <div className="field" role="group" aria-label="start template">
          <span className="field-name">Template</span>
          <JsonField
            value={model.start?.template}
            onChange={(v) =>
              onChange(setStart(model, "template", (v as Record<string, unknown> | undefined) ?? undefined))
            }
          />
          <span className="help">empty: built from the {"{{payload.…}}"} fields the pipeline reads</span>
        </div>
      )}
      <IssueLines issues={sIssues} />
    </fieldset>
  );
}

function IssueList({
  issues,
  model,
  onFocus,
}: {
  issues: Issue[];
  model: Model;
  onFocus: (id: string) => void;
}) {
  if (!issues.length) return null;
  return (
    <div className="issues" aria-label="issues">
      {issues.map((i, k) => {
        const target = issueTarget(i, model);
        return (
          <button
            key={k}
            className="issue-row"
            disabled={!target}
            onClick={() => target && onFocus(target)}
            title={i.hint ?? undefined}
          >
            <span className="loc">{i.line ? `L${i.line}` : ""}</span>
            <code>{i.path}</code> {i.message}
            {i.hint && <em>. {i.hint}</em>}
          </button>
        );
      })}
    </div>
  );
}
