import { Icon, type IconName } from "./Icon";
import { useEffect, useState } from "react";
import {
  compatibleModels,
  emptyLabels,
  isAiIssue,
  BUILTIN_HARNESS,
  pickerHarness,
  routerMode,
  setHarness,
  setModel,
  setRouterMode,
  setRunner,
} from "../lib/ai";
import type { Defaults, FieldSpec, Issue, Model, ModelEdge, ModelNode, NodeTypes } from "../types";
import { HarnessSelect, IssueLines, ModelSelect } from "./AiPicker";
import {
  JsonField,
  ListField,
  MapField,
  NumberField,
  PromptField,
  SelectField,
  TextField,
  WorkspaceField,
} from "./fields";

function FieldEditor({
  spec,
  value,
  types,
  onChange,
}: {
  spec: FieldSpec;
  value: unknown;
  types: NodeTypes;
  onChange: (v: unknown) => void;
}) {
  const options = spec.source ? types[spec.source] : (spec.options ?? []);
  switch (spec.kind) {
    case "select":
    case "enum":
      return <SelectField value={value} options={options} onChange={onChange} />;
    case "list":
      return <ListField value={value} suggest={spec.suggest} onChange={onChange} />;
    case "map":
      return value === undefined || (typeof value === "object" && !Array.isArray(value)) ? (
        <MapField value={value} keys={spec.keys} onChange={onChange} />
      ) : (
        <JsonField value={value} onChange={onChange} />
      );
    case "prompt":
      return <PromptField value={value} onChange={onChange} />;
    case "workspace":
      return <WorkspaceField value={value} options={types.workspaces} onChange={onChange} />;
    case "int":
      return <NumberField value={value} integer min={spec.min} onChange={onChange} />;
    case "number":
      return <NumberField value={value} min={spec.min} onChange={onChange} />;
    case "string":
    case "duration":
      return typeof value === "object" && value !== null ? (
        <JsonField value={value} onChange={onChange} />
      ) : (
        <TextField value={value} onChange={onChange} />
      );
    default:
      return <JsonField value={value} onChange={onChange} />;
  }
}

// Fields with their own controls at the top of the panel (not in the generic field list).
const CUSTOM_FIELDS: Record<string, string[]> = {
  agent: ["harness", "agent", "model", "runner"],
  chat: ["harness", "agent", "model"],
  router: ["mode", "switch", "prompt", "model"],
};

export function NodePanel({
  node,
  types,
  defaults,
  issues,
  onFields,
  onRename,
  onDelete,
}: {
  node: ModelNode;
  types: NodeTypes;
  defaults?: Defaults | null;
  issues: Issue[];
  onFields: (fields: Record<string, unknown>) => void;
  onRename: (id: string) => void;
  onDelete: () => void;
}) {
  const [id, setId] = useState(node.id);
  useEffect(() => setId(node.id), [node.id]);
  const schema = types.types[node.type ?? ""];
  const custom = new Set(CUSTOM_FIELDS[node.type ?? ""] ?? []);
  const allSpecs = schema?.fields ?? [];
  const specs = allSpecs.filter((s) => !custom.has(s.name));
  const shown = specs.filter((s) => s.required || node.fields[s.name] !== undefined);
  const addable = specs.filter((s) => !shown.includes(s));
  const known = new Set(["type", ...allSpecs.map((s) => s.name)]);
  const unknown = Object.keys(node.fields).filter((k) => !known.has(k));
  const set = (name: string, v: unknown) => {
    const next = { ...node.fields };
    if (v === undefined) delete next[name];
    else next[name] = v;
    onFields(next);
  };
  const aiIssues = custom.size ? issues.filter(isAiIssue) : [];
  const issuesFor = (name: string | null) =>
    issues.filter((i) =>
      name === null
        ? (!i.field || !known.has(i.field)) && !aiIssues.includes(i)
        : i.field === name && !aiIssues.includes(i),
    );
  return (
    <div className="panel">
      <div className="panel-head">
        <h3>
          <span className={`tag tag-${node.type}`}>
            <Icon name={(node.type ?? "circleDashed") as IconName} />
            {node.type ?? "?"}
          </span>
          <input
            className="id-input"
            value={id}
            aria-label="node id"
            onChange={(e) => setId(e.target.value)}
            onBlur={() => id !== node.id && onRename(id)}
            onKeyDown={(e) => e.key === "Enter" && (e.target as HTMLInputElement).blur()}
          />
        </h3>
        <button type="button" className="danger" onClick={onDelete}>
          Delete
        </button>
      </div>
      {issuesFor(null).map((i, k) => (
        <p className="issue" key={k}>
          {i.message}
          {i.hint && <em> ({i.hint})</em>}
        </p>
      ))}
      {!schema && <p className="muted">Unknown type: edit it in the YAML tab.</p>}
      {(node.type === "agent" || node.type === "chat") && (
        <HarnessFields
          lowLevel={node.type === "agent"}
          fields={node.fields}
          types={types}
          defaults={defaults}
          aiIssues={aiIssues}
          runnerIssues={issuesFor("runner")}
          onFields={onFields}
        />
      )}
      {node.type === "router" && (
        <RouterFields
          fields={node.fields}
          types={types}
          defaults={defaults}
          aiIssues={aiIssues}
          issuesFor={issuesFor}
          onFields={onFields}
        />
      )}
      {/* a div, not a <label>: a click anywhere in a label (its help text) would "click" its
          first control, which is the remove button */}
      {shown.map((s) => (
        <div className="field" role="group" aria-label={s.name} key={s.name}>
          <span className="field-name">
            {s.name}
            {s.required && <span className="req">*</span>}
            {!s.required && (
              <button
                type="button"
                className="link"
                title="remove field"
                onClick={(e) => {
                  e.preventDefault();
                  set(s.name, undefined);
                }}
              >
                remove
              </button>
            )}
          </span>
          <FieldEditor
            spec={s}
            value={node.fields[s.name]}
            types={types}
            onChange={(v) => set(s.name, v)}
          />
          {s.help && <span className="help">{s.help}</span>}
          {issuesFor(s.name).map((i, k) => (
            <span className="issue" key={k}>
              {i.message}
              {i.hint && <em> ({i.hint})</em>}
            </span>
          ))}
        </div>
      ))}
      {unknown.map((k) => (
        <div className="field" role="group" aria-label={k} key={k}>
          <span className="field-name">
            {k} <span className="muted">(not a {node.type} field)</span>
            <button type="button" className="link" onClick={() => set(k, undefined)}>
              remove
            </button>
          </span>
          <JsonField value={node.fields[k]} onChange={(v) => set(k, v)} />
        </div>
      ))}
      {addable.length > 0 && (
        <select
          className="add-field"
          value=""
          onChange={(e) => {
            const spec = specs.find((s) => s.name === e.target.value);
            if (!spec) return;
            const empty =
              spec.kind === "list" ? [] : spec.kind === "map" ? {} : spec.kind === "int" ? 1 : "";
            set(spec.name, empty);
          }}
        >
          <option value="">+ add field…</option>
          {addable.map((s) => (
            <option key={s.name} value={s.name}>
              {s.name}
            </option>
          ))}
        </select>
      )}
    </div>
  );
}

function HarnessFields({
  fields,
  types,
  defaults,
  aiIssues,
  runnerIssues,
  onFields,
  lowLevel,
}: {
  fields: Record<string, unknown>;
  types: NodeTypes;
  defaults?: Defaults | null;
  aiIssues: Issue[];
  runnerIssues: Issue[];
  lowLevel: boolean; // agent nodes: the Advanced low-level runner
  onFields: (fields: Record<string, unknown>) => void;
}) {
  const runner = typeof fields.runner === "string" && fields.runner ? fields.runner : undefined;
  const harness = typeof fields.harness === "string" && fields.harness ? fields.harness : undefined;
  const model = typeof fields.model === "string" && fields.model ? fields.model : undefined;
  const labels = emptyLabels(fields, defaults, types.ai);
  const listFor = pickerHarness(fields, defaults);
  return (
    <div className="ai-group">
      <label className="field">
        <span className="field-name">Harness</span>
        <HarnessSelect
          value={harness}
          ai={types.ai}
          emptyLabel={labels.harness}
          onChange={(a) => onFields(setHarness(fields, a))}
        />
        {runner && (
          <span className="help">
            Uses the low-level runner <code>{runner}</code> (Advanced). Picking a harness removes it.
          </span>
        )}
      </label>
      <label className="field">
        <span className="field-name">Model</span>
        <ModelSelect
          value={model}
          options={compatibleModels(types.ai, listFor)}
          emptyLabel={labels.model}
          onChange={(m) => onFields(setModel(fields, m))}
        />
        <span className="help">
          {harness || defaults?.harness
            ? `models that ${listFor} can use; or provider:model-id`
            : `no harness: a model alone makes this a direct ${BUILTIN_HARNESS} call`}
        </span>
      </label>
      <IssueLines issues={aiIssues} />
      {lowLevel && (
      <details className="advanced" open={!!runner || runnerIssues.length > 0}>
        <summary>Advanced</summary>
        <label className="field">
          <span className="field-name">runner</span>
          <SelectField
            value={runner}
            options={types.runners}
            onChange={(r) => onFields(setRunner(fields, r as string | undefined))}
          />
          <span className="help">
            a low-level runner instead of a harness; picking one removes the harness
          </span>
          <IssueLines issues={runnerIssues} />
        </label>
      </details>
      )}
    </div>
  );
}

function RouterFields({
  fields,
  types,
  defaults,
  aiIssues,
  issuesFor,
  onFields,
}: {
  fields: Record<string, unknown>;
  types: NodeTypes;
  defaults?: Defaults | null;
  aiIssues: Issue[];
  issuesFor: (name: string) => Issue[];
  onFields: (fields: Record<string, unknown>) => void;
}) {
  const mode = routerMode(fields);
  const model = typeof fields.model === "string" && fields.model ? fields.model : undefined;
  const dModel = defaults?.model;
  return (
    <div className="ai-group">
      <div className="field">
        <span className="field-name">Mode</span>
        <div className="seg" role="radiogroup" aria-label="router mode">
          <label>
            <input
              type="radio"
              checked={mode === "switch"}
              onChange={() => onFields(setRouterMode(fields, "switch"))}
            />{" "}
            switch
          </label>
          <label>
            <input
              type="radio"
              checked={mode === "llm"}
              onChange={() => onFields(setRouterMode(fields, "llm"))}
            />{" "}
            LLM
          </label>
        </div>
        <span className="help">
          {mode === "switch"
            ? "follows the edge whose when equals an artifact field"
            : "a model reads the prompt and picks one of the out-edges' when labels"}
        </span>
        <IssueLines issues={issuesFor("mode")} />
      </div>
      {mode === "switch" ? (
        <label className="field">
          <span className="field-name">
            switch<span className="req">*</span>
          </span>
          <TextField
            value={fields.switch}
            placeholder="review.state"
            onChange={(v) => onFields({ ...fields, switch: v ?? "" })}
          />
          <span className="help">&lt;artifact&gt;.&lt;field&gt;, e.g. review.state</span>
          <IssueLines issues={issuesFor("switch")} />
        </label>
      ) : (
        <>
          <label className="field">
            <span className="field-name">
              prompt<span className="req">*</span>
            </span>
            <PromptField
              value={fields.prompt}
              onChange={(v) => onFields({ ...fields, prompt: v ?? "" })}
            />
            <span className="help">what to decide; inline text or a file path</span>
            <IssueLines issues={issuesFor("prompt")} />
          </label>
          <label className="field">
            <span className="field-name">Model</span>
            <ModelSelect
              value={model}
              options={compatibleModels(types.ai, BUILTIN_HARNESS)}
              emptyLabel={dModel ? `(pipeline default: ${dModel})` : "(none: set one)"}
              onChange={(m) => onFields(setModel(fields, m))}
            />
            <IssueLines issues={aiIssues} />
          </label>
        </>
      )}
    </div>
  );
}

export function EdgePanel({
  model,
  index,
  issues,
  onChange,
  onDelete,
}: {
  model: Model;
  index: number;
  issues: Issue[];
  onChange: (patch: Partial<ModelEdge>) => void;
  onDelete: () => void;
}) {
  const e = model.edges[index];
  if (!e) return null;
  const src = model.nodes.find((n) => n.id === e.src);
  const isRouter = src?.type === "router";
  const exhausted = ["needs-review", "fail", ...model.nodes.map((n) => n.id)];
  return (
    <div className="panel">
      <div className="panel-head">
        <h3>
          <code>{e.src}</code> → <code>{e.dst}</code>
        </h3>
        <button type="button" className="danger" onClick={onDelete}>
          Delete
        </button>
      </div>
      {issues.map((i, k) => (
        <p className="issue" key={k}>
          {i.message}
          {i.hint && <em> ({i.hint})</em>}
        </p>
      ))}
      {isRouter ? (
        <label className="field">
          <span className="field-name">
            when<span className="req">*</span>
          </span>
          <TextField
            value={e.when}
            placeholder="case label, or default"
            onChange={(v) => onChange({ when: (v as string) ?? null })}
          />
          <span className="help">
            followed when the router's <code>{String(src?.fields.switch ?? "switch")}</code>{" "}
            equals this
          </span>
        </label>
      ) : (
        e.when && (
          <p className="issue">
            'when' is only for router edges.{" "}
            <button type="button" className="link" onClick={() => onChange({ when: null })}>
              remove
            </button>
          </p>
        )
      )}
      <label className="field">
        <span className="field-name">max_loops</span>
        <NumberField
          value={e.max_loops ?? undefined}
          integer
          min={1}
          onChange={(v) => onChange({ max_loops: (v as number | undefined) ?? null })}
        />
        <span className="help">set on the edge that loops back to an earlier node</span>
      </label>
      <label className="field">
        <span className="field-name">on_exhausted</span>
        <SelectField
          value={e.on_exhausted ?? ""}
          options={exhausted}
          onChange={(v) => onChange({ on_exhausted: (v as string | undefined) ?? null })}
        />
        <span className="help">what happens when max_loops is used up (default needs-review)</span>
      </label>
    </div>
  );
}
