// Settings: AI harnesses, model sources (providers), named models and API keys.
// Everything is stored on the server: agentline.toml (harnesses/providers/models, edited table by
// table so comments survive) and the secrets file (keys, write-only).

import { useEffect, useState, type ReactNode } from "react";
import { api } from "../api";
import {
  compatibleForKind,
  SETTINGS_CHANGED_EVENT,
  formBody,
  NAME_RE,
} from "../lib/ai";
import type {
  HarnessInfo,
  DetectResult,
  Settings as SettingsData,
  SettingsSection,
} from "../types";
import { ModelSelect } from "./AiPicker";
import { ListField } from "./fields";
import { NewRow, RowItem, Rows, SettingsPage } from "./SettingsRows";
import { ChatSection, GeneralSection, SkillsSection } from "./SettingsExtra";
import { ModelsPage } from "./settings/ModelsPage";
import { AddHarnessDialog } from "./AddHarness";
import { harnessStatus } from "../lib/models";
import { OtherKeys } from "./settings/parts";
import { McpSection } from "./Mcp";
import { PeoplePage } from "./settings/PeoplePage";

type Tab = "models" | "harnesses" | "mcp" | "chat" | "skills" | "people" | "general";
const TABS: { id: Tab; label: string }[] = [
  { id: "models", label: "Models" },
  { id: "harnesses", label: "Harnesses" },
  { id: "mcp", label: "MCP" },
  { id: "skills", label: "Skills" },
  { id: "chat", label: "Chat apps" },
  { id: "people", label: "People" },
  { id: "general", label: "General" },
];

type Selected = { name: string } | { new: Record<string, unknown> } | null;
type Form = Record<string, unknown>;

const errText = (e: unknown) => (e instanceof Error ? e.message : String(e));

export function Settings({ onChanged }: { onChanged?: () => void }) {
  const [data, setData] = useState<SettingsData | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [note, setNote] = useState<string | null>(null);
  const [tab, setTab] = useState<Tab>(TABS[0].id);
  const [selected, setSelected] = useState<Selected>(null);

  useEffect(() => {
    const load = () =>
      api
        .settings()
        .then(setData)
        .catch((e: Error) => setError(e.message));
    void load();
    // a harness set up from a picker popup elsewhere (the Chat panel, the editor)
    window.addEventListener(SETTINGS_CHANGED_EVENT, load);
    return () => window.removeEventListener(SETTINGS_CHANGED_EVENT, load);
  }, []);

  const applied = (r: SettingsData) => {
    setData(r);
    setNote(r.note ?? null);
    onChanged?.();
  };

  /** PUT one entry; throws the server's message (422) for the form to show inline. */
  const save = async (section: SettingsSection, name: string, body: Form) => {
    const r = await api.putSetting(section, name, body);
    applied(r);
    setSelected({ name });
  };
  const remove = async (section: SettingsSection, name: string, confirmText?: string) => {
    if (!window.confirm(confirmText ?? `Delete ${section.replace(/s$/, "")} ${name} from agentline.toml?`)) return;
    try {
      applied(await api.deleteSetting(section, name));
      setSelected(null);
      setError(null);
    } catch (e) {
      setError(errText(e));
    }
  };
  const goTab = (t: Tab, sel: Selected = null) => {
    setTab(t);
    setSelected(sel);
    setError(null);
  };

  if (!data) return error ? <div className="error pad">{error}</div> : <p className="muted pad">Loading…</p>;

  return (
    <div className="settings">
      <div className="toolbar">
        <strong>Settings</strong>
        <div className="tabs" role="tablist">
          {TABS.map((t) => (
            <button
              key={t.id}
              role="tab"
              aria-selected={tab === t.id}
              className={tab === t.id ? "on" : ""}
              onClick={() => goTab(t.id)}
            >
              {t.label}
            </button>
          ))}
        </div>
        <span className="spacer" />
        <span className="muted small path" title="harnesses, model sources and models are saved here">
          {data.config_path}
        </span>
      </div>
      {note && (
        <div className="banner banner-info">
          {note} <button onClick={() => setNote(null)}>Dismiss</button>
        </div>
      )}
      {error && <div className="banner banner-error">{error}</div>}
      <div className="settings-body">
        {tab === "harnesses" && (
          <HarnessesSection
            data={data}
            selected={selected}
            onSelect={setSelected}
            onSave={(n, b) => save("harnesses", n, b)}
            onDelete={(n) => void remove("harnesses", n)}
          />
        )}
        {tab === "models" && <ModelsPage data={data} onApplied={applied} onReload={() => void api.settings().then(setData)} />}
        {tab === "mcp" && (
          <McpSection
            servers={data.mcp ?? []}
            keys={data.keys}
            onSave={(n, b) => save("mcp", n, b)}
            onDelete={(n, text) => void remove("mcp", n, text)}
            onApplied={applied}
          />
        )}
        {tab === "chat" && (
          <ChatSection
            harnesses={data.harnesses.map((a) => a.name)}
            compatible={data.compatible_models}
            presets={data.presets}
            onHarnessAdded={() => void api.settings().then(applied)}
          />
        )}
        {tab === "general" && (
          <GeneralSection>
            <OtherKeys keys={data.keys} onKeys={(keys) => setData({ ...data, keys })} />
          </GeneralSection>
        )}
        {tab === "skills" && <SkillsSection />}
        {tab === "people" && <PeoplePage />}
      </div>
    </div>
  );
}

// -- shared ----------------------------------------------------------------------------------

/** The page's entries as rows; the selected one (or a new one, at the top) shows `editor`. */
function EntryRows<T extends { name: string }>({
  label,
  items,
  selected,
  onSelect,
  newTitle,
  editor,
  summary,
}: {
  label: string;
  items: T[];
  selected: Selected;
  onSelect: (s: Selected) => void;
  newTitle: string;
  editor: ReactNode;
  summary: (item: T) => { badges?: ReactNode; meta?: ReactNode };
}) {
  const current = selected && "name" in selected ? selected.name : null;
  const isNew = !!selected && "new" in selected;
  return (
    <Rows label={label}>
      {isNew && <NewRow title={newTitle}>{editor}</NewRow>}
      {items.map((it) => {
        const open = current === it.name;
        const sm = summary(it);
        return (
          <RowItem
            key={it.name}
            name={it.name}
            badges={sm.badges}
            meta={sm.meta}
            open={open}
            onToggle={() => onSelect(open ? null : { name: it.name })}
            editor={open ? editor : undefined}
          />
        );
      })}
    </Rows>
  );
}

const builtIn = <span className="badge">built-in</span>;

function Row({ label, children, help }: { label: string; children: ReactNode; help?: ReactNode }) {
  return (
    <label className="field">
      <span className="field-name">{label}</span>
      {children}
      {help && <span className="help">{help}</span>}
    </label>
  );
}

function Text({
  value,
  onChange,
  placeholder,
  type = "text",
}: {
  value: unknown;
  onChange: (v: string) => void;
  placeholder?: string;
  type?: string;
}) {
  return (
    <input
      type={type}
      value={value == null ? "" : String(value)}
      placeholder={placeholder}
      step={type === "number" ? "any" : undefined}
      onChange={(e) => onChange(e.target.value)}
    />
  );
}

/** A row's editor: name (new entries only), fields in a grid, Save / Cancel / Delete, and the
 * server's 422 message inline. */
function EntryForm({
  isNew,
  form,
  setForm,
  onSave,
  onCancel,
  onDelete,
  deleteLabel,
  children,
}: {
  isNew: boolean;
  form: Form;
  setForm: (f: Form) => void;
  onSave: () => Promise<void>;
  onCancel: () => void;
  onDelete?: () => void;
  deleteLabel?: string;
  children: ReactNode;
}) {
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const name = String(form.name ?? "");
  const badName = isNew && name !== "" && !NAME_RE.test(name);
  return (
    <form
      className="ri-form"
      onSubmit={(e) => {
        e.preventDefault();
        setBusy(true);
        setErr(null);
        onSave()
          .catch((x) => setErr(errText(x)))
          .finally(() => setBusy(false));
      }}
    >
      <div className="ri-fields">
        {isNew && (
          <Row label="Name" help="Letters, digits, _ . - (used in pipelines and agentline.toml)">
            <input
              type="text"
              value={name}
              className={badName ? "invalid" : undefined}
              onChange={(e) => setForm({ ...form, name: e.target.value })}
              autoFocus
            />
          </Row>
        )}
        {children}
      </div>
      {err && <p className="issue">{err}</p>}
      <div className="form-actions">
        <button className="primary" type="submit" disabled={busy || !name || badName}>
          {busy ? "Saving…" : isNew ? "Create" : "Save"}
        </button>
        <button type="button" onClick={onCancel}>
          {isNew ? "Cancel" : "Close"}
        </button>
        {onDelete && (
          <>
            <span className="spacer" />
            <button type="button" className="danger" onClick={onDelete}>
              {deleteLabel ?? "Delete"}
            </button>
          </>
        )}
      </div>
    </form>
  );
}

function useForm(selected: Selected, find: (name: string) => Form | undefined): [Form, (f: Form) => void, boolean] {
  const isNew = !!selected && "new" in selected;
  const key = selected ? ("name" in selected ? `n:${selected.name}` : "new") : "";
  const initial = (): Form =>
    !selected ? {} : "name" in selected ? { ...(find(selected.name) ?? { name: selected.name }) } : { ...selected.new };
  const [form, setForm] = useState<Form>(initial);
  // reset when another entry is picked (or after a save returns fresh data for it)
  useEffect(() => setForm(initial()), [key]);
  return [form, setForm, isNew];
}

// -- harnesses -------------------------------------------------------------------------------

/** A harness row's default model, said only as usable or not ready (Models page: connect its source). */
function defaultNote(a: HarnessInfo, data: SettingsData): string {
  if (!a.model) return "";
  const ready = (data.compatible_models[a.name] ?? []).includes(a.model);
  return ready ? ` · default ${a.model}` : ` · default ${a.model} isn't ready`;
}

function HarnessesSection({
  data,
  selected,
  onSelect,
  onSave,
  onDelete,
}: {
  data: SettingsData;
  selected: Selected;
  onSelect: (s: Selected) => void;
  onSave: (name: string, body: Form) => Promise<void>;
  onDelete: (name: string) => void;
}) {
  const find = (n: string) => data.harnesses.find((a) => a.name === n) as Form | undefined;
  const [form, setForm, isNew] = useForm(selected, find);
  const [detect, setDetect] = useState<DetectResult | string | null>(null);
  const name = String(form.name ?? "");
  const saved = data.harnesses.find((a) => a.name === name);
  useEffect(() => setDetect(null), [name]);
  const kind = String(form.kind ?? (isNew ? "" : name));
  const kinfo = data.kinds[kind];
  const set = (k: string, v: unknown) => setForm({ ...form, [k]: v });
  const [adding, setAdding] = useState(false); // the Add harness popup
  const editor = selected ? (
        <EntryForm
          key={isNew ? "new" : name}
          isNew={isNew}
          form={form}
          setForm={setForm}
          onCancel={() => onSelect(null)}
          onSave={() => onSave(name, formBody({ ...form, implicit: undefined, kind: kind || undefined }))}
          onDelete={saved && !saved.implicit ? () => onDelete(name) : undefined}
          deleteLabel={name === "builtin" ? "Reset to built-in" : "Delete"}
        >
          {saved?.implicit && (
            <p className="help">Built-in: saving writes it to agentline.toml with your changes.</p>
          )}
          <Row
            label="Kind"
            help={
              kinfo
                ? `${kinfo.description} Model sources: ${kinfo.provider_types.join(", ")}${kinfo.subscription ? " (or a Claude subscription login)" : ""}.${kinfo.needs_model ? " Needs a model." : ""}`
                : undefined
            }
          >
            <select value={kind} onChange={(e) => set("kind", e.target.value)}>
              <option value="">(none)</option>
              {Object.keys(data.kinds).map((k) => (
                <option key={k} value={k}>
                  {k}
                </option>
              ))}
            </select>
          </Row>
          
          {kinfo?.default_bin && (
            <Row label="Binary" help={`Empty: ${kinfo.default_bin} on PATH`}>
              <div className="inline">
                <Text value={form.bin} placeholder={kinfo.default_bin} onChange={(v) => set("bin", v)} />
                <button
                  type="button"
                  disabled={!saved}
                  title={saved ? "run <bin> --version on the server" : "save first"}
                  onClick={() =>
                    api
                      .detectHarness(name)
                      .then(setDetect)
                      .catch((e) => setDetect(errText(e)))
                  }
                >
                  Detect
                </button>
              </div>
            </Row>
          )}
          {detect && <DetectLine result={detect} />}
          <Row label="Default model" help="Used when a node picks this harness without a model">
            <ModelSelect
              value={form.model ? String(form.model) : undefined}
              options={compatibleForKind(data, kind)}
              emptyLabel={kinfo?.needs_model ? "(none: nodes must pick one)" : "(the harness's own default)"}
              onChange={(m) => set("model", m)}
            />
          </Row>
          <Row label="Extra args" help="Added to every call">
            <ListField value={form.extra_args} onChange={(v) => set("extra_args", v ?? [])} />
          </Row>
          <Row label="Env" help="Names of host env vars passed through (never values)">
            <ListField value={form.env} onChange={(v) => set("env", v ?? [])} />
          </Row>
          <Row label="Description">
            <Text value={form.description} onChange={(v) => set("description", v)} />
          </Row>
        </EntryForm>
  ) : null;
  return (
    <SettingsPage
      id="harnesses"
      title="Harnesses"
      intro={
        <>
          What drives the model for agent steps and the Chat. Built-in needs only a model from the Models page;
          Claude Code, Codex and Aider are programs on this computer. Only these can be picked anywhere.
        </>
      }
      actions={
        <button type="button" className="primary" onClick={() => setAdding(true)}>
          Add harness
        </button>
      }
    >
      {adding && (
        <AddHarnessDialog
          presets={data.presets ?? []}
          onAdded={() => setAdding(false)}
          onCancel={() => setAdding(false)}
          onCustom={() => {
            setAdding(false);
            onSelect({ new: {} });
          }}
        />
      )}
      <EntryRows<HarnessInfo>
        label="harnesses"
        items={data.harnesses}
        selected={selected}
        onSelect={onSelect}
        newTitle="New harness"
        editor={editor}
        summary={(a) => {
          const st = harnessStatus(a, data);
          return {
            badges: (
              <>
                {a.implicit ? builtIn : undefined}
                <span className={st.ok ? "source-ok" : "source-attn"}>
                  <span className="dot" aria-hidden="true" />
                  {st.word}
                </span>
              </>
            ),
            meta: `${a.name === "builtin" ? "agentline's own" : `${a.kind} program`}${defaultNote(a, data)}`,
          };
        }}
      />
    </SettingsPage>
  );
}

function DetectLine({ result }: { result: DetectResult | string }) {
  if (typeof result === "string") return <p className="issue">{result}</p>;
  if (!result.found) return <p className="issue">{result.error ?? "not found"}</p>;
  return (
    <p className="help ok">
      Found{result.bin ? <> at <code>{result.bin}</code></> : null}
      {result.version ? `: ${result.version}` : ""}
      {result.note ? ` (${result.note})` : ""}
      {result.error ? ` (${result.error})` : ""}
    </p>
  );
}
