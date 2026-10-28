// Small controlled editors for node fields. Each calls onChange with the new plain value, or
// `undefined` to remove the field from the YAML.

import { useEffect, useState } from "react";

type Change = (value: unknown) => void;

export function TextField({
  value,
  onChange,
  placeholder,
}: {
  value: unknown;
  onChange: Change;
  placeholder?: string;
}) {
  const [text, setText] = useState(value == null ? "" : String(value));
  useEffect(() => setText(value == null ? "" : String(value)), [value]);
  return (
    <input
      type="text"
      value={text}
      placeholder={placeholder}
      onChange={(e) => setText(e.target.value)}
      onBlur={() => text !== (value == null ? "" : String(value)) && onChange(text || undefined)}
      onKeyDown={(e) => e.key === "Enter" && (e.target as HTMLInputElement).blur()}
    />
  );
}

export function NumberField({
  value,
  onChange,
  integer,
  min,
}: {
  value: unknown;
  onChange: Change;
  integer?: boolean;
  min?: number;
}) {
  const [text, setText] = useState(value == null ? "" : String(value));
  useEffect(() => setText(value == null ? "" : String(value)), [value]);
  const commit = () => {
    if (text.trim() === "") return value !== undefined && onChange(undefined);
    const n = integer ? parseInt(text, 10) : parseFloat(text);
    if (!Number.isNaN(n) && n !== value) onChange(n);
  };
  return (
    <input
      type="number"
      value={text}
      min={min}
      step={integer ? 1 : "any"}
      onChange={(e) => setText(e.target.value)}
      onBlur={commit}
      onKeyDown={(e) => e.key === "Enter" && (e.target as HTMLInputElement).blur()}
    />
  );
}

export function SelectField({
  value,
  options,
  onChange,
  allowEmpty = true,
}: {
  value: unknown;
  options: string[];
  onChange: Change;
  allowEmpty?: boolean;
}) {
  const current = value == null ? "" : String(value);
  const all = current && !options.includes(current) ? [current, ...options] : options;
  return (
    <select value={current} onChange={(e) => onChange(e.target.value || undefined)}>
      {allowEmpty && <option value="">(none)</option>}
      {all.map((o) => (
        <option key={o} value={o}>
          {o}
          {o === current && !options.includes(o) ? " (unknown)" : ""}
        </option>
      ))}
    </select>
  );
}

export function ListField({
  value,
  onChange,
  suggest = [],
}: {
  value: unknown;
  onChange: Change;
  suggest?: string[];
}) {
  const items = Array.isArray(value) ? value.map(String) : value == null ? [] : [String(value)];
  const [draft, setDraft] = useState("");
  const listId = `sugg-${suggest.join("-")}`;
  const add = () => {
    const v = draft.trim();
    if (v && !items.includes(v)) onChange([...items, v]);
    setDraft("");
  };
  return (
    <div className="list-field">
      {items.map((x, i) => (
        <span className="chip" key={`${x}-${i}`}>
          {x}
          <button
            type="button"
            aria-label={`remove ${x}`}
            onClick={() => {
              const next = items.filter((_, j) => j !== i);
              onChange(next.length ? next : undefined);
            }}
          >
            ×
          </button>
        </span>
      ))}
      <input
        type="text"
        value={draft}
        list={suggest.length ? listId : undefined}
        placeholder="add…"
        onChange={(e) => setDraft(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === "Enter") {
            e.preventDefault();
            add();
          }
        }}
        onBlur={add}
      />
      {suggest.length > 0 && (
        <datalist id={listId}>
          {suggest.map((s) => (
            <option key={s} value={s} />
          ))}
        </datalist>
      )}
    </div>
  );
}

/** Parse a typed value: numbers and booleans become JSON, everything else stays text. */
export function parseScalar(text: string): unknown {
  if (/^-?\d+(\.\d+)?$/.test(text)) return Number(text);
  if (text === "true" || text === "false") return text === "true";
  return text;
}

function show(v: unknown): string {
  return typeof v === "string" ? v : JSON.stringify(v);
}

export function MapField({
  value,
  onChange,
  keys = [],
}: {
  value: unknown;
  onChange: Change;
  keys?: string[];
}) {
  const obj = value && typeof value === "object" && !Array.isArray(value) ? value : {};
  const entries = Object.entries(obj as Record<string, unknown>);
  const [newKey, setNewKey] = useState("");
  const set = (next: [string, unknown][]) =>
    onChange(next.length ? Object.fromEntries(next) : undefined);
  return (
    <div className="map-field">
      {entries.map(([k, v], i) => (
        <div className="map-row" key={k}>
          <code>{k}</code>
          <MapValue
            value={v}
            onChange={(nv) => set(entries.map((e, j) => (j === i ? [k, nv] : e)))}
          />
          <button
            type="button"
            aria-label={`remove ${k}`}
            onClick={() => set(entries.filter((_, j) => j !== i))}
          >
            ×
          </button>
        </div>
      ))}
      <div className="map-row">
        <input
          type="text"
          placeholder="new key"
          value={newKey}
          list={keys.length ? `keys-${keys.join("-")}` : undefined}
          onChange={(e) => setNewKey(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && newKey.trim() && !(newKey in obj)) {
              set([...entries, [newKey.trim(), ""]]);
              setNewKey("");
            }
          }}
        />
        {keys.length > 0 && (
          <datalist id={`keys-${keys.join("-")}`}>
            {keys.map((k) => (
              <option key={k} value={k} />
            ))}
          </datalist>
        )}
      </div>
    </div>
  );
}

function MapValue({ value, onChange }: { value: unknown; onChange: Change }) {
  const nested = value !== null && typeof value === "object";
  const [text, setText] = useState(show(value));
  const [bad, setBad] = useState(false);
  useEffect(() => setText(show(value)), [value]);
  const commit = () => {
    if (text === show(value)) return;
    if (nested) {
      try {
        onChange(JSON.parse(text));
        setBad(false);
      } catch {
        setBad(true);
      }
    } else {
      onChange(typeof value === "string" ? text : parseScalar(text));
    }
  };
  return (
    <input
      type="text"
      className={bad ? "invalid" : undefined}
      value={text}
      onChange={(e) => setText(e.target.value)}
      onBlur={commit}
      onKeyDown={(e) => e.key === "Enter" && (e.target as HTMLInputElement).blur()}
    />
  );
}

const PROMPT_FILE = /^[\w./-]+\.(md|txt|tmpl|prompt|j2)$/;

export function PromptField({
  value,
  onChange,
  allowFile = true,
}: {
  value: unknown;
  onChange: Change;
  allowFile?: boolean;
}) {
  const fileValue =
    value && typeof value === "object" && "file" in (value as object)
      ? String((value as { file: unknown }).file)
      : typeof value === "string" && PROMPT_FILE.test(value.trim())
        ? value.trim()
        : null;
  const [mode, setMode] = useState<"inline" | "file">(fileValue !== null ? "file" : "inline");
  const [text, setText] = useState(fileValue ?? (typeof value === "string" ? value : ""));
  useEffect(() => {
    setText(fileValue ?? (typeof value === "string" ? value : ""));
  }, [value, fileValue]);
  const commit = () => {
    const before = fileValue ?? (typeof value === "string" ? value : "");
    if (text !== before) onChange(text || undefined);
  };
  return (
    <div className="prompt-field">
      {allowFile && (
        <div className="seg">
          <label>
            <input
              type="radio"
              checked={mode === "inline"}
              onChange={() => setMode("inline")}
            />{" "}
            inline
          </label>
          <label>
            <input type="radio" checked={mode === "file"} onChange={() => setMode("file")} /> file
          </label>
        </div>
      )}
      {mode === "file" ? (
        <input
          type="text"
          value={text}
          placeholder="prompts/step.md"
          onChange={(e) => setText(e.target.value)}
          onBlur={commit}
        />
      ) : (
        <textarea
          rows={6}
          value={text}
          placeholder="Use {{artifact.field}} for inputs"
          onChange={(e) => setText(e.target.value)}
          onBlur={commit}
        />
      )}
    </div>
  );
}

export function WorkspaceField({
  value,
  options,
  onChange,
}: {
  value: unknown;
  options: string[];
  onChange: Change;
}) {
  const obj = value && typeof value === "object" ? (value as Record<string, unknown>) : null;
  const type = obj ? String(obj.type ?? "container") : value == null ? "" : String(value);
  const image = obj?.image == null ? "" : String(obj.image);
  const update = (t: string, img: string) => {
    if (!t) return onChange(undefined);
    if (obj) return onChange({ ...obj, type: t, image: img || undefined });
    onChange(img ? { type: t, image: img } : t);
  };
  return (
    <div className="ws-field">
      <SelectField value={type} options={options} onChange={(t) => update(String(t ?? ""), image)} />
      {type && (
        <TextField
          value={image}
          placeholder="image (auto)"
          onChange={(img) => update(type, String(img ?? ""))}
        />
      )}
    </div>
  );
}

export function JsonField({ value, onChange }: { value: unknown; onChange: Change }) {
  const [text, setText] = useState(JSON.stringify(value, null, 2));
  const [bad, setBad] = useState(false);
  useEffect(() => setText(JSON.stringify(value, null, 2)), [value]);
  return (
    <textarea
      rows={4}
      className={bad ? "invalid mono" : "mono"}
      value={text}
      onChange={(e) => setText(e.target.value)}
      onBlur={() => {
        try {
          const v = text.trim() ? JSON.parse(text) : undefined;
          setBad(false);
          if (JSON.stringify(v) !== JSON.stringify(value)) onChange(v);
        } catch {
          setBad(true);
        }
      }}
    />
  );
}
