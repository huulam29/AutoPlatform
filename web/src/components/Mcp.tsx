// Settings > MCP: the server list (enable/disable, test, edit, delete, which nodes use each),
// the guided add/edit popup and "Detect on this machine". Engine: api/settings.py section
// `mcp`, POST /api/mcp/{name}/test, POST /api/mcp/test, GET /api/mcp/detect, POST /api/mcp/import.
// Pure logic: ../lib/mcp.ts.
import { useEffect, useState } from "react";
import { ApiError, api, type McpTest } from "../api";
import {
  STEPS,
  draftFromServer,
  draftToSave,
  emptyDraft,
  importItems,
  missingKeys,
  newRow,
  stepProblems,
  suggestKeyName,
  usedByText,
  type McpDraft,
  type McpRow,
  type Transport,
} from "../lib/mcp";
import type { DetectedMcp, KeyStatus, McpServer, Settings } from "../types";
import { Status } from "./Icon";
import { RowItem, Rows, SettingsPage } from "./SettingsRows";

const errText = (e: unknown) => (e instanceof ApiError || e instanceof Error ? e.message : String(e));

function TestResult({ t }: { t: McpTest | "testing" | undefined }) {
  if (!t) return null;
  if (t === "testing") return <Status s="running">testing</Status>;
  if (!t.ok) return <Status s="failed">{`failed: ${t.error ?? "no answer"}`}</Status>;
  const tools = t.tools ?? [];
  const names = tools.slice(0, 8).map((x) => x.name).join(", ");
  return <Status s="done">{`ok: ${tools.length} tools${names ? ` (${names}${tools.length > 8 ? ", …" : ""})` : ""}${t.note ? ` · ${t.note}` : ""}`}</Status>;
}

export function McpSection({
  servers,
  keys,
  onSave,
  onDelete,
  onApplied,
}: {
  servers: McpServer[];
  keys: KeyStatus[];
  onSave: (name: string, body: Record<string, unknown>) => Promise<void>;
  onDelete: (name: string, confirmText: string) => void;
  onApplied: (s: Settings) => void;
}) {
  const [editing, setEditing] = useState<McpServer | "new" | null>(null);
  const [detecting, setDetecting] = useState(false);
  const [tests, setTests] = useState<Record<string, McpTest | "testing">>({});
  const [err, setErr] = useState<string | null>(null);
  const setKeys = new Set(keys.filter((k) => k.set).map((k) => k.name));
  const test = async (name: string) => {
    setTests((x) => ({ ...x, [name]: "testing" }));
    try {
      const r = await api.testMcp(name);
      setTests((x) => ({ ...x, [name]: r }));
    } catch (e) {
      setTests((x) => ({ ...x, [name]: { ok: false, error: errText(e) } }));
    }
  };
  return (
    <>
      <SettingsPage
        id="mcp"
        title="MCP servers"
        intro={
          <>
            Tools for agents. An agent node lists the servers it may use (<code>mcp: [name]</code>); Claude Code and
            Codex nodes only. A server that is off stays configured, and nodes that list it run without it.
          </>
        }
        actions={
          <>
            <button type="button" onClick={() => setDetecting(true)}>
              Detect on this machine
            </button>
            <button type="button" className="primary" onClick={() => setEditing("new")}>
              + Add server
            </button>
          </>
        }
      >
        {err && <p className="issue">{err}</p>}
        {servers.length === 0 ? (
          <p className="sp-empty">No MCP servers yet. Add one, or detect the ones your other tools already use.</p>
        ) : (
          <Rows label="MCP servers">
            {servers.map((m) => {
              const on = m.enabled !== false;
              const missing = (m.keys ?? []).filter((k) => !setKeys.has(k));
              const used = usedByText(m.used_by);
              const where = m.url ?? [m.command, ...(m.args ?? [])].join(" ");
              return (
                <RowItem
                  key={m.name}
                  off={!on}
                  name={m.name}
                  badges={
                    <>
                      <span className="badge">{m.transport}</span>
                      {m.probe_safe && (
                        <span className="badge" title="Run reviews may use it when they replay a node (probes)">
                          probe-safe
                        </span>
                      )}
                      {!on && <Status s="skipped">off</Status>}
                    </>
                  }
                  meta={m.description || undefined}
                  trailing={
                    <>
                      <label className="check" title="Off: nodes that list it run without it">
                        <input
                          type="checkbox"
                          checked={on}
                          aria-label={`${m.name} enabled`}
                          onChange={async (e) => {
                            setErr(null);
                            try {
                              const { body } = draftToSave({ ...draftFromServer(m), enabled: e.target.checked });
                              await onSave(m.name, body);
                            } catch (x) {
                              setErr(errText(x));
                            }
                          }}
                        />
                        On
                      </label>
                      <button type="button" disabled={tests[m.name] === "testing"} onClick={() => void test(m.name)}>
                        Test
                      </button>
                      <button type="button" onClick={() => setEditing(m)}>
                        Edit
                      </button>
                      <button
                        type="button"
                        className="danger"
                        onClick={() =>
                          onDelete(
                            m.name,
                            used
                              ? `Delete MCP server ${m.name}? These nodes list it and won't validate until you remove it there: ${used}`
                              : `Delete MCP server ${m.name} from agentline.toml?`,
                          )
                        }
                      >
                        Delete
                      </button>
                    </>
                  }
                  below={
                    <>
                      <div className="mono small muted one-line" title={where}>
                        {where}
                      </div>
                      <div className="small">
                        <span className="muted">Used by </span>
                        {used || <span className="muted">no pipeline nodes yet</span>}
                      </div>
                      {missing.length > 0 && (
                        <Status s="needs-review">{`needs ${missing.join(", ")} (Edit to type it)`}</Status>
                      )}
                      <TestResult t={tests[m.name]} />
                    </>
                  }
                />
              );
            })}
          </Rows>
        )}
      </SettingsPage>
      {editing && (
        <McpWizard
          key={editing === "new" ? "new" : editing.name}
          initial={editing === "new" ? null : editing}
          taken={servers.map((s) => s.name)}
          setKeys={setKeys}
          onClose={() => setEditing(null)}
          onSave={async (name, body, secrets) => {
            for (const [k, v] of Object.entries(secrets)) await api.putSecret(k, v);
            await onSave(name, body);
            setTests(({ [name]: _old, ...rest }) => rest); // that result was for the old settings
            setEditing(null);
          }}
        />
      )}
      {detecting && <McpDetect onClose={() => setDetecting(false)} onApplied={onApplied} />}
    </>
  );
}

/** The guided popup: kind → connection → keys and settings → test and save. Editing opens
 * on the connection step with every step reachable. */
export function McpWizard({
  initial,
  taken,
  setKeys,
  onClose,
  onSave,
}: {
  initial: McpServer | null;
  taken: string[];
  setKeys: Set<string>;
  onClose: () => void;
  onSave: (name: string, body: Record<string, unknown>, secrets: Record<string, string>) => Promise<void>;
}) {
  const editing = initial !== null;
  const [d, setD] = useState<McpDraft>(() => (initial ? draftFromServer(initial) : emptyDraft()));
  const [step, setStep] = useState(editing ? 1 : 0);
  const [test, setTest] = useState<McpTest | "testing" | undefined>();
  const [err, setErr] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const problems = stepProblems(d, step, taken, editing);
  const kind = d.transport === "stdio" ? "env" : "headers";
  const rowsOf = d.transport === "stdio" ? d.env : d.headers;
  const setRows = (rs: McpRow[]) => setD({ ...d, [kind]: rs });
  const setRow = (i: number, r: Partial<McpRow>) => setRows(rowsOf.map((x, k) => (k === i ? { ...x, ...r } : x)));
  const missing = missingKeys(d, setKeys);
  useEffect(() => setTest(undefined), [d]);
  const runTest = async () => {
    const { name, body, secrets } = draftToSave(d);
    setTest("testing");
    try {
      setTest(await api.testMcpDraft(name || "draft", body, secrets));
    } catch (e) {
      setTest({ ok: false, error: errText(e) });
    }
  };
  return (
    <div className="modal-backdrop" role="dialog" aria-modal="true" aria-labelledby="mcp-title">
      <div className="modal wide mcp-wizard">
        <h2 id="mcp-title">{editing ? `MCP server ${initial.name}` : "Add an MCP server"}</h2>
        <ol className="steps" aria-label="steps">
          {STEPS.map((label, i) => (
            <li key={label}>
              <button
                type="button"
                className={i === step ? "on" : ""}
                aria-current={i === step ? "step" : undefined}
                disabled={i > step && !editing && stepProblems(d, i - 1, taken, editing).length > 0}
                onClick={() => setStep(i)}
              >
                <span className="step-n">{i + 1}</span> {label}
              </button>
            </li>
          ))}
        </ol>

        {step === 0 && (
          <div className="choice-cards" role="radiogroup" aria-label="kind of server">
            {(
              [
                ["stdio", "Local command", "A program this machine starts, like npx @modelcontextprotocol/server-github. Most servers."],
                ["http", "Remote URL (HTTP)", "A hosted server you reach over HTTPS, like https://mcp.linear.app/mcp."],
                ["sse", "Remote URL (SSE)", "An older hosted server that streams with server-sent events."],
              ] as [Transport, string, string][]
            ).map(([t, label, help]) => (
              <button
                key={t}
                type="button"
                role="radio"
                aria-checked={d.transport === t}
                className={`choice-card${d.transport === t ? " on" : ""}`}
                onClick={() => setD({ ...d, transport: t })}
              >
                <strong>{label}</strong>
                <span className="small muted">{help}</span>
              </button>
            ))}
          </div>
        )}

        {step === 1 && (
          <>
            <label className="field">
              <span className="field-name">Name (what nodes list in mcp: [...])</span>
              <input value={d.name} disabled={editing} placeholder="github" autoFocus onChange={(e) => setD({ ...d, name: e.target.value })} />
            </label>
            {d.transport === "stdio" ? (
              <>
                <label className="field">
                  <span className="field-name">Command</span>
                  <input value={d.command} placeholder="npx" className="mono" onChange={(e) => setD({ ...d, command: e.target.value })} />
                </label>
                <label className="field">
                  <span className="field-name">Arguments (quote one that has spaces)</span>
                  <input value={d.args} placeholder="-y @modelcontextprotocol/server-github" className="mono" onChange={(e) => setD({ ...d, args: e.target.value })} />
                </label>
              </>
            ) : (
              <label className="field">
                <span className="field-name">URL</span>
                <input value={d.url} placeholder="https://mcp.example.com/mcp" className="mono" onChange={(e) => setD({ ...d, url: e.target.value })} />
              </label>
            )}
            <label className="field">
              <span className="field-name">Description (optional)</span>
              <input value={d.description} onChange={(e) => setD({ ...d, description: e.target.value })} />
            </label>
          </>
        )}

        {step === 2 && (
          <>
            <p className="small muted">
              {d.transport === "stdio" ? "Environment variables the command gets." : "HTTP headers sent with every request."} Tick
              Secret for tokens and keys: the value goes to the secrets file under the key name, and agentline.toml only
              gets <code>{"${KEY}"}</code>.
            </p>
            <div className="mcp-rows">
              {rowsOf.length > 0 && (
                <div className="mcp-row mcp-row-head small muted" aria-hidden="true">
                  <span>{d.transport === "stdio" ? "Variable" : "Header"}</span>
                  <span>Value</span>
                  <span>Secret</span>
                  <span>Key name</span>
                  <span />
                </div>
              )}
              {rowsOf.map((r, i) => (
                <div key={i} className="mcp-row">
                  <input
                    className="mono"
                    value={r.name}
                    aria-label="name"
                    placeholder={d.transport === "stdio" ? "GITHUB_TOKEN" : "Authorization"}
                    onChange={(e) => {
                      const name = e.target.value;
                      const auto = !r.keyName || r.keyName === suggestKeyName(d.name, r.name, kind);
                      setRow(i, { name, ...(auto ? { keyName: suggestKeyName(d.name, name, kind) } : {}) });
                    }}
                  />
                  <span className="mcp-value">
                    {r.secret && r.prefix && <span className="mono small muted">{r.prefix}</span>}
                    <input
                      className="mono"
                      type={r.secret ? "password" : "text"}
                      autoComplete="off"
                      value={r.value}
                      aria-label={`value of ${r.name || "row"}`}
                      placeholder={r.secret ? (setKeys.has(r.keyName) ? "set: leave empty to keep" : "paste the value") : ""}
                      onChange={(e) => setRow(i, { value: e.target.value })}
                    />
                  </span>
                  <input
                    type="checkbox"
                    checked={r.secret}
                    aria-label={`${r.name || "row"} is a secret`}
                    onChange={(e) =>
                      setRow(i, {
                        secret: e.target.checked,
                        keyName: r.keyName || suggestKeyName(d.name, r.name, kind),
                        prefix: e.target.checked && kind === "headers" && r.name.toLowerCase() === "authorization" ? "Bearer " : r.prefix,
                      })
                    }
                  />
                  <input
                    className="mono"
                    value={r.secret ? r.keyName : ""}
                    disabled={!r.secret}
                    aria-label={`key name for ${r.name || "row"}`}
                    onChange={(e) => setRow(i, { keyName: e.target.value.toUpperCase() })}
                  />
                  <button type="button" className="link" aria-label={`remove ${r.name || "row"}`} onClick={() => setRows(rowsOf.filter((_, k) => k !== i))}>
                    Remove
                  </button>
                </div>
              ))}
              <div className="row">
                <button type="button" onClick={() => setRows([...rowsOf, newRow(d.name, kind)])}>
                  + {d.transport === "stdio" ? "Variable" : "Header"}
                </button>
                {d.transport !== "stdio" && !d.headers.some((h) => h.name.toLowerCase() === "authorization") && (
                  <button type="button" onClick={() => setRows([...rowsOf, newRow(d.name, kind, "Authorization")])}>
                    + Bearer token
                  </button>
                )}
              </div>
            </div>
          </>
        )}

        {step === 3 && (
          <>
            <dl className="kv">
              <dt>Name</dt>
              <dd className="mono">{d.name}</dd>
              <dt>{d.transport === "stdio" ? "Command" : "URL"}</dt>
              <dd className="mono">{d.transport === "stdio" ? `${d.command} ${d.args}` : d.url}</dd>
              <dt>Secrets</dt>
              <dd>
                {rowsOf.filter((r) => r.secret && r.name).map((r) => r.keyName).join(", ") || "none"}
              </dd>
            </dl>
            <label className="check">
              <input type="checkbox" checked={d.enabled} onChange={(e) => setD({ ...d, enabled: e.target.checked })} /> On (nodes that list it
              can use it)
            </label>
            <label className="check">
              <input type="checkbox" checked={d.probeSafe} onChange={(e) => setD({ ...d, probeSafe: e.target.checked })} /> Safe for probes (it
              only reads: run reviews may use it when they replay a node; otherwise probes run without it)
            </label>
            {missing.length > 0 && <Status s="needs-review">{`${missing.join(", ")} not set: go back and type it`}</Status>}
            <div className="row">
              <button type="button" disabled={test === "testing"} onClick={() => void runTest()}>
                Test the connection
              </button>
              <TestResult t={test} />
            </div>
            <p className="small muted">The test starts the server (or calls the URL) and lists its tools. Typed values are used for it but not kept until you save.</p>
          </>
        )}

        {problems.length > 0 && step > 0 && (
          <ul className="issue small plain-list">
            {problems.map((p) => (
              <li key={p}>{p}</li>
            ))}
          </ul>
        )}
        {err && <p className="issue">{err}</p>}
        <div className="modal-foot row">
          <button type="button" onClick={onClose}>
            Cancel
          </button>
          <span className="spacer" />
          {step > 0 && (
            <button type="button" onClick={() => setStep(step - 1)}>
              Back
            </button>
          )}
          {step < STEPS.length - 1 ? (
            <button type="button" className="primary" disabled={problems.length > 0} onClick={() => setStep(step + 1)}>
              Next
            </button>
          ) : (
            <button
              type="button"
              className="primary"
              disabled={saving || stepProblems(d, 2, taken, editing).length > 0}
              onClick={async () => {
                setSaving(true);
                setErr(null);
                try {
                  const { name, body, secrets } = draftToSave(d);
                  await onSave(name, body, secrets);
                } catch (e) {
                  setErr(errText(e));
                } finally {
                  setSaving(false);
                }
              }}
            >
              {saving ? "Saving…" : "Save"}
            </button>
          )}
        </div>
      </div>
    </div>
  );
}

/** Servers found in Claude Code, Claude Desktop, Codex, Cursor and .mcp.json files. */
export function McpDetect({ onClose, onApplied }: { onClose: () => void; onApplied: (s: Settings) => void }) {
  const [found, setFound] = useState<DetectedMcp[] | null>(null);
  const [picked, setPicked] = useState<Record<string, string | undefined>>({});
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [done, setDone] = useState<string | null>(null);
  useEffect(() => {
    api
      .detectMcp()
      .then((r) => setFound(r.found))
      .catch((e) => setErr(errText(e)));
  }, []);
  const items = importItems(picked);
  return (
    <div className="modal-backdrop" role="dialog" aria-modal="true" aria-labelledby="detect-title">
      <div className="modal wide">
        <h2 id="detect-title">MCP servers on this machine</h2>
        <p className="small muted">
          Found in Claude Code, Claude Desktop, Codex, Cursor and project <code>.mcp.json</code> files. Importing copies a
          server's secret values into the secrets file; agentline.toml gets <code>{"${KEY}"}</code> placeholders.
        </p>
        {found === null && !err && <Status s="running">looking</Status>}
        {found?.length === 0 && <p className="muted">Nothing found.</p>}
        <div className="detect-list">
          {found?.map((f) => {
            const on = picked[f.id] !== undefined;
            return (
              <div key={f.id} className="entry">
                <div className="entry-head">
                  <label className="check">
                    <input
                      type="checkbox"
                      disabled={f.status !== "new"}
                      checked={on}
                      aria-label={`import ${f.name} from ${f.source}`}
                      onChange={(e) => setPicked((p) => ({ ...p, [f.id]: e.target.checked ? f.suggested : undefined }))}
                    />
                    <strong className="mono">{f.name}</strong>
                  </label>
                  <span className="muted small">
                    {f.source} · {f.transport}
                  </span>
                  <span className="spacer" />
                  {f.status === "configured" && <Status s="done">{`already added as ${f.suggested}`}</Status>}
                  {f.status === "problem" && <Status s="failed">{`can't import: ${f.problem}`}</Status>}
                </div>
                <div className="mono small muted one-line" title={f.path}>
                  {f.url ?? [f.command, ...(f.args ?? [])].join(" ")}
                </div>
                {f.secrets.length > 0 && (
                  <div className="small">
                    {f.status === "new" ? "Secrets that go to the secrets file" : "Secrets"}: {f.secrets.map((s) => `${s.key} (${s.from})`).join(", ")}
                  </div>
                )}
                {on && (
                  <label className="field">
                    <span className="field-name">Name in agentline</span>
                    <input value={picked[f.id]} onChange={(e) => setPicked((p) => ({ ...p, [f.id]: e.target.value }))} />
                  </label>
                )}
              </div>
            );
          })}
        </div>
        {err && <p className="issue">{err}</p>}
        {done && <p className="small">{done}</p>}
        <div className="modal-foot row">
          <button type="button" onClick={onClose}>
            {done ? "Close" : "Cancel"}
          </button>
          <button
            type="button"
            className="primary"
            disabled={busy || items.length === 0}
            onClick={async () => {
              setBusy(true);
              setErr(null);
              try {
                const r = await api.importMcp(items);
                onApplied(r);
                setDone(`Imported ${r.imported.join(", ")}${r.keys_set.length ? `; keys saved: ${r.keys_set.join(", ")}` : ""}.`);
                setPicked({});
                setFound((await api.detectMcp()).found);
              } catch (e) {
                setErr(errText(e));
              } finally {
                setBusy(false);
              }
            }}
          >
            {busy ? "Importing…" : `Import${items.length ? ` (${items.length})` : ""}`}
          </button>
        </div>
      </div>
    </div>
  );
}
