// Harness and model selects, used by agent nodes, LLM routers and the pipeline defaults.

import { useEffect, useState } from "react";
import { ADD_HARNESS, harnessOptions, type Option } from "../lib/ai";
import type { AiSummary, Issue } from "../types";
import { AddHarnessDialog, AddHarnessOption } from "./AddHarness";

const CUSTOM = "\u0000custom";

export function IssueLines({ issues }: { issues: Issue[] }) {
  return (
    <>
      {issues.map((i, k) => (
        <span className="issue" key={k}>
          {i.message}
          {i.hint && <em> ({i.hint})</em>}
        </span>
      ))}
    </>
  );
}

export function HarnessSelect({
  value,
  ai,
  emptyLabel,
  onChange,
  disabled,
}: {
  value: string | undefined;
  ai: AiSummary | null | undefined;
  emptyLabel: string;
  onChange: (harness: string | undefined) => void;
  disabled?: boolean;
}) {
  const options = harnessOptions(ai);
  const unknown = value && !options.some((o) => o.value === value);
  const presets = ai?.presets ?? [];
  const notSetUp = unknown && presets.some((p) => p.name === value);
  const [adding, setAdding] = useState<string | null>(null); // the preset to start on
  return (
    <>
      <select
        value={value ?? ""}
        disabled={disabled}
        aria-label="harness"
        onChange={(e) => {
          if (e.target.value === ADD_HARNESS) setAdding(value ?? "");
          else onChange(e.target.value || undefined);
        }}
      >
        <option value="">{emptyLabel}</option>
        {unknown && (
          <option value={value}>
            {value} ({notSetUp ? "not set up" : "unknown"})
          </option>
        )}
        {options.map((o) => (
          <option key={o.value} value={o.value}>
            {o.label}
          </option>
        ))}
        <AddHarnessOption presets={presets} />
      </select>
      {notSetUp && !adding && (
        <button type="button" className="link small" onClick={() => setAdding(value ?? "")}>
          Set up {value}
        </button>
      )}
      {adding !== null && (
        <AddHarnessDialog
          presets={presets}
          initial={adding}
          onAdded={(name) => {
            setAdding(null);
            onChange(name);
          }}
          onCancel={() => setAdding(null)} // the previous choice stays
        />
      )}
    </>
  );
}

/**
 * Named models compatible with the harness, plus a free-text `provider:model-id` entry. A value
 * that isn't one of the names is shown in the text entry.
 */
export function ModelSelect({
  value,
  options,
  emptyLabel,
  onChange,
}: {
  value: string | undefined;
  options: string[];
  emptyLabel: string;
  onChange: (model: string | undefined) => void;
}) {
  const named = !value || options.includes(value);
  const [custom, setCustom] = useState(!named);
  const [text, setText] = useState(named ? "" : (value ?? ""));
  const optionsKey = options.join("\n");
  useEffect(() => {
    const isNamed = !value || optionsKey.split("\n").includes(value);
    if (!isNamed) {
      setCustom(true);
      setText(value ?? "");
    } else {
      setCustom(false);
      setText("");
    }
  }, [value, optionsKey]);
  const opts: Option[] = options.map((o) => ({ value: o, label: o }));
  return (
    <div className="model-select">
      <select
        value={custom ? CUSTOM : (value ?? "")}
        aria-label="model"
        onChange={(e) => {
          if (e.target.value === CUSTOM) {
            setCustom(true);
            return;
          }
          setCustom(false);
          setText("");
          onChange(e.target.value || undefined);
        }}
      >
        <option value="">{emptyLabel}</option>
        {opts.map((o) => (
          <option key={o.value} value={o.value}>
            {o.label}
          </option>
        ))}
        <option value={CUSTOM}>other: provider:model-id…</option>
      </select>
      {custom && (
        <input
          type="text"
          value={text}
          placeholder="openrouter:meta-llama/llama-3.3-70b-instruct:free"
          aria-label="model id"
          onChange={(e) => setText(e.target.value)}
          onBlur={() => text.trim() !== (value ?? "") && onChange(text.trim() || undefined)}
          onKeyDown={(e) => e.key === "Enter" && (e.target as HTMLInputElement).blur()}
        />
      )}
    </div>
  );
}
