// Settings > Chat, Skills, Keys (Mac app) and General for the web UI. Chat: api/chat.py.
// Settings > MCP lives in Mcp.tsx.
import { useEffect, useState, type ReactNode } from "react";
import { ApiError, api, type ChatInstructions, type ChatPlatform, type ChatStatus, type VisionStatus } from "../api";
import { instructionSkills, instructionsState } from "../lib/assistant";
import { ADD_HARNESS, BUILTIN_HARNESS, chatModelOptions, modelAfterHarnessChange } from "../lib/ai";
import { AddHarnessDialog, AddHarnessOption } from "./AddHarness";
import { OPEN_SETUP_EVENT } from "../lib/setup";
import { isMacApp, nativeCall, type AppInfo } from "../lib/native";
import { AUTH_NEEDED_EVENT, type AppInfoApi, checkToken, clearToken, hasToken } from "../api";
import { identityLabel, type Identity } from "../lib/people";
import { CopyPath } from "./CopyPath";
import { FolderPicker } from "./FolderPicker";
import { restartEngine } from "../lib/engine";
import { NotificationsSetting } from "./NotificationsSetting";
import { NewRow, RowItem, Rows, SettingsPage } from "./SettingsRows";
import type { HarnessPreset } from "../types";
import { GitHubSetup } from "./GitHubSetup";
import { TelemetrySetting } from "./TelemetrySetting";
import { AddSkillsDialog } from "./settings/AddSkillsDialog";
import type { InstalledFrom } from "../api";

const errText = (e: unknown) => (e instanceof ApiError || e instanceof Error ? e.message : String(e));

export function ChatSection({
  harnesses = [],
  compatible = {},
  presets = [],
  onHarnessAdded,
}: {
  harnesses?: string[];
  compatible?: Record<string, string[]>;
  presets?: HarnessPreset[];
  onHarnessAdded?: () => void;
}) {
  const [s, setS] = useState<ChatStatus | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [codes, setCodes] = useState<Record<string, { code: string; link?: string | null }>>({});
  const load = () =>
    api
      .chat()
      .then(setS)
      .catch((e) => setErr(errText(e)));
  useEffect(() => {
    void load();
  }, []);
  // while a pairing code is out, notice the link without a click
  const waiting = Object.keys(codes).some((p) => s && !(s[p as "telegram"] as ChatPlatform | undefined)?.linked);
  useEffect(() => {
    if (!waiting) return;
    const t = setInterval(() => void load(), 3000);
    return () => clearInterval(t);
  }, [waiting]);
  if (!s) return err ? <p className="issue pad">{err}</p> : <p className="muted pad">Loading…</p>;
  const anyLinked = s.platforms.some((p) => (s[p as "telegram"] as ChatPlatform | undefined)?.linked);
  return (
    <div className="chat-settings">
      <p className="muted">
        Link a chat app to get approvals, questions and reports on your phone, and to talk to the Chat. Link one or
        several; only the account that sends the pairing code is answered.
      </p>
      <ChatHarnessBar
        harnesses={harnesses}
        compatible={compatible}
        presets={presets}
        onHarnessAdded={onHarnessAdded}
        openInstructions
      />
      {s.platforms.map((name) => {
        const p = s[name as "telegram"] as ChatPlatform | undefined;
        return p ? (
          <PlatformCard
            key={name}
            name={name}
            p={p}
            code={codes[name]}
            onPaired={(c) => setCodes((x) => ({ ...x, [name]: c }))}
            onChange={(next) => {
              setS(next);
              if (!(next[name as "telegram"] as ChatPlatform).linked) return;
              setCodes((x) => {
                const { [name]: _gone, ...rest } = x;
                return rest;
              });
            }}
            onError={setErr}
            reload={load}
          />
        ) : null;
      })}
      {anyLinked && (
        <>
          <h3>Send me</h3>
          {(
            [
              ["gates", "Approvals (with Approve / Reject buttons)"],
              ["questions", "Questions from pipelines (chat.ask)"],
              ["failures", "Failed and needs-review runs"],
              ["done", "Finished runs"],
              ["reviews", "Run reviews (what to change in a pipeline)"],
            ] as const
          ).map(([k, label]) => (
            <label key={k} className="check">
              <input
                type="checkbox"
                checked={s.notify[k]}
                onChange={async (e) => setS(await api.chatNotify({ [k]: e.target.checked }))}
              />{" "}
              {label}
            </label>
          ))}
          <p className="muted small">
            Send /help to the bot for commands; anything else goes to the Chat. API keys are only set here,
            never over chat.
          </p>
        </>
      )}
      {err && <p className="issue">{err}</p>}
    </div>
  );
}

function PlatformCard({
  name,
  p,
  code,
  onPaired,
  onChange,
  onError,
  reload,
}: {
  name: string;
  p: ChatPlatform;
  code?: { code: string; link?: string | null };
  onPaired: (c: { code: string; link?: string | null }) => void;
  onChange: (s: ChatStatus) => void;
  onError: (e: string | null) => void;
  reload: () => Promise<void>;
}) {
  const [values, setValues] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState(false);
  const run = async (op: () => Promise<void>) => {
    setBusy(true);
    onError(null);
    try {
      await op();
    } catch (e) {
      onError(errText(e));
    } finally {
      setBusy(false);
    }
  };
  const [replacing, setReplacing] = useState(false); // "Change token": a wrong or old one
  const missing = replacing ? p.keys : p.keys.filter((k) => !k.set);
  return (
    <section className="platform-card">
      <h3>
        {p.title} {p.linked ? <span className="badge badge-ok">linked</span> : null}
      </h3>
      {missing.length > 0 ? (
        <>
          <p className="small">{p.setup}</p>
          {missing.map((k) => (
            <form
              key={k.name}
              className="inline"
              onSubmit={(e) => {
                e.preventDefault();
                void run(async () => {
                  await api.putSecret(k.name, values[k.name] ?? "");
                  setValues((v) => ({ ...v, [k.name]: "" }));
                  setReplacing(false);
                  await reload();
                });
              }}
            >
              <input
                type="password"
                autoComplete="off"
                value={values[k.name] ?? ""}
                placeholder={k.name}
                aria-label={`${p.title} ${k.name}`}
                onChange={(e) => setValues((v) => ({ ...v, [k.name]: e.target.value }))}
              />
              <button className="primary" type="submit" disabled={busy || !values[k.name]}>
                Save
              </button>
            </form>
          ))}
        </>
      ) : p.linked ? (
        <>
          <p>
            Bot <strong>{p.bot ? `@${p.bot}` : "?"}</strong> is linked to{" "}
            <strong>{p.linked.username ? `@${p.linked.username}` : `chat ${p.linked.chat_id}`}</strong>.
            {p.acts_as ? (
              <>
                {" "}
                Approvals and retries from it are recorded as <strong>{p.acts_as}</strong>.
              </>
            ) : null}
          </p>
          <div className="row">
            <button type="button" disabled={busy} onClick={() => void run(() => api.chatTest(name))}>
              Send test message
            </button>
            <button
              type="button"
              className="danger"
              disabled={busy}
              onClick={() => void run(async () => onChange(await api.chatUnlink(name)))}
            >
              Unlink
            </button>
          </div>
        </>
      ) : (
        <>
          <p>
            Bot: <strong>{p.bot ? `@${p.bot}` : "token not accepted"}</strong>{" "}
            <button type="button" className="link" onClick={() => setReplacing(true)}>
              Change token
            </button>
          </p>
          {code ? (
            <>
              <p>
                {name === "telegram" ? "Send this code to your bot" : "Send this code to the bot in a direct message"}{" "}
                within 10 minutes{code.link ? ", or open the link on your phone" : ""}:
              </p>
              <p className="pair-code mono">{code.code}</p>
              {code.link && (
                <a href={code.link} target="_blank" rel="noreferrer">
                  Open in {p.title}
                </a>
              )}
            </>
          ) : (
            <button
              type="button"
              className="primary"
              disabled={busy || !p.bot}
              onClick={() =>
                void run(async () => {
                  const r = await api.chatPair(name);
                  onChange(r);
                  if (r.code) onPaired({ code: r.code, link: r.link });
                })
              }
            >
              Link my {p.title}
            </button>
          )}
        </>
      )}
    </section>
  );
}

type SkillRow = { name: string; description: string; source: string; installed_from?: InstalledFrom };

export function SkillsSection() {
  const [skills, setSkills] = useState<SkillRow[] | null>(null);
  const [fetched, setFetched] = useState(false);
  const [repo, setRepo] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  // the skill being edited: a new one (at the top), a copy of a library skill (under it), or one of yours
  const [edit, setEdit] = useState<{ name: string; description: string; body: string; isNew: boolean; copyOf?: string } | null>(null);
  const [filter, setFilter] = useState("");
  const [adding, setAdding] = useState(false);
  const [updating, setUpdating] = useState<string | null>(null); // a skill's name, or "*" for all
  const [note, setNote] = useState<string | null>(null);
  const update = async (names?: string[]) => {
    setUpdating(names ? names[0] : "*");
    setErr(null);
    setNote(null);
    try {
      const r = await api.updateSkills(names);
      const { updated, missing } = r.result;
      setNote(
        (updated.length ? `Updated ${updated.join(", ")}.` : "Nothing to update.") +
          (missing.length ? ` No longer in their source: ${missing.join(", ")}.` : ""),
      );
      await load();
    } catch (e) {
      setErr(errText(e));
    } finally {
      setUpdating(null);
    }
  };
  const load = () =>
    api
      .skills()
      .then((r) => {
        setSkills(r.skills as SkillRow[]);
        setFetched(r.fetched);
        setRepo(r.repo);
      })
      .catch((e) => setErr(errText(e)));
  useEffect(() => {
    void load();
  }, []);
  const open_ = async (name: string, copy: boolean) => {
    const s = await api.skill(name);
    setEdit({
      name: copy ? `our-${name}` : s.name,
      description: s.description,
      body: stripFrontMatter(s.body),
      isNew: copy,
      ...(copy ? { copyOf: name } : {}),
    });
  };
  const shown = (skills ?? []).filter(
    (s) => !filter || s.name.includes(filter.toLowerCase()) || s.description.toLowerCase().includes(filter.toLowerCase()),
  );
  const editor = edit ? (
    <form
      className="ri-form"
      onSubmit={async (e) => {
        e.preventDefault();
        try {
          await api.putSkill(edit.name, edit.description, edit.body);
          setEdit(null);
          await load();
        } catch (x) {
          setErr(errText(x));
        }
      }}
    >
      <div className="ri-fields">
        {edit.isNew && (
          <label className="field">
            <span className="field-name">Name</span>
            <input value={edit.name} placeholder="our-review-checklist" autoFocus onChange={(e) => setEdit({ ...edit, name: e.target.value })} />
          </label>
        )}
        <label className="field">
          <span className="field-name">Description (when to use it)</span>
          <input value={edit.description} onChange={(e) => setEdit({ ...edit, description: e.target.value })} />
        </label>
        <label className="field">
          <span className="field-name">Instructions (markdown)</span>
          <textarea className="mono" rows={14} value={edit.body} onChange={(e) => setEdit({ ...edit, body: e.target.value })} />
        </label>
      </div>
      <div className="form-actions">
        <button type="submit" className="primary" disabled={!edit.name || !edit.body.trim()}>
          Save
        </button>
        <button type="button" onClick={() => setEdit(null)}>
          Cancel
        </button>
      </div>
    </form>
  ) : null;
  const fromLib = edit?.isNew && edit.copyOf;
  return (
    <SettingsPage
      id="skills"
      title="Skills"
      intro={
        <>
          Instructions agent nodes load (<code>skills: [name]</code>). The library comes from GitHub ({repo || "addyosmani/agent-skills"});
          add more from skills.sh or any GitHub repo. Your own skills win on a name clash.
        </>
      }
      actions={
        <>
          <button
            type="button"
            disabled={busy}
            onClick={async () => {
              setBusy(true);
              setErr(null);
              try {
                await api.fetchSkills();
                await load();
              } catch (e) {
                setErr(errText(e));
              } finally {
                setBusy(false);
              }
            }}
          >
            {busy ? "Downloading…" : fetched ? "Refresh library" : "Download library"}
          </button>
          {(skills ?? []).some((s) => s.source === "installed") && (
            <button type="button" disabled={!!updating} title="Download installed skills again from their sources" onClick={() => void update()}>
              {updating === "*" ? "Updating…" : "Update installed"}
            </button>
          )}
          <button type="button" onClick={() => setEdit({ name: "", description: "", body: "", isNew: true })}>
            + New skill
          </button>
          <button type="button" className="primary" onClick={() => setAdding(true)}>
            + Add skills
          </button>
        </>
      }
      toolbar={
        <input
          type="search"
          className="sp-search"
          value={filter}
          placeholder={`Search ${skills?.length ?? 0} skills`}
          aria-label="search skills"
          onChange={(e) => setFilter(e.target.value)}
        />
      }
    >
      {adding && <AddSkillsDialog onInstalled={() => void load()} onClose={() => setAdding(false)} />}
      {err && <p className="issue">{err}</p>}
      {note && <p className="help ok">{note}</p>}
      {skills !== null && shown.length === 0 && !(edit?.isNew && !fromLib) && (
        <p className="sp-empty">{filter ? "No skills match." : "No skills yet: add some, download the library or write your own."}</p>
      )}
      <Rows label="skills">
        {edit?.isNew && !fromLib && <NewRow title="New skill">{editor}</NewRow>}
        {shown.map((s) => {
          const mine = s.source === "local";
          const from = s.installed_from;
          const open = !!edit && (edit.isNew ? edit.copyOf === s.name : edit.name === s.name);
          return (
            <RowItem
              key={s.name}
              name={s.name}
              badges={
                <span className={`badge ${mine ? "badge-ok" : ""}`} title={skillOriginTitle(s.source, repo, from)}>
                  {skillLibraryLabel(s.source, repo, from)}
                </span>
              }
              meta={<span title={s.description}>{oneLine(s.description)}</span>}
              open={open}
              editor={open ? editor : undefined}
              trailing={
                mine ? (
                  <>
                    <button type="button" onClick={() => void open_(s.name, false)}>
                      Edit
                    </button>
                    <button
                      type="button"
                      className="danger"
                      onClick={async () => {
                        if (!window.confirm(`Delete your skill ${s.name}?`)) return;
                        await api.deleteSkill(s.name);
                        await load();
                      }}
                    >
                      Delete
                    </button>
                  </>
                ) : s.source === "installed" ? (
                  <>
                    {from && (
                      <button type="button" disabled={!!updating} title={`Download it again from ${from.repo}`} onClick={() => void update([s.name])}>
                        {updating === s.name ? "Updating…" : "Update"}
                      </button>
                    )}
                    <button type="button" title="Start your own skill from this one" onClick={() => void open_(s.name, true)}>
                      Copy
                    </button>
                    <button
                      type="button"
                      className="danger"
                      onClick={async () => {
                        if (!window.confirm(`Remove ${s.name}${from ? ` (installed from ${from.repo})` : ""}?`)) return;
                        try {
                          await api.deleteSkill(s.name);
                        } catch (e) {
                          setErr(errText(e));
                        }
                        await load();
                      }}
                    >
                      Remove
                    </button>
                  </>
                ) : (
                  <button type="button" title="Start your own skill from this one" onClick={() => void open_(s.name, true)}>
                    Copy
                  </button>
                )
              }
            />
          );
        })}
      </Rows>
    </SettingsPage>
  );
}

/** The badge on a skill row: "yours", the repo an installed skill came from (`docker/skills`),
 * or the library (`agent-skills` for `owner/agent-skills`; the full repo is in the tooltip). */
export function skillLibraryLabel(source: string | undefined, repo: string, from?: InstalledFrom): string {
  if (source === "local") return "yours";
  if (source === "builtin") return "built-in"; // shipped with agentline (used when the library has none)
  if (source === "installed") return from?.repo || "installed";
  return repo.split("/").pop() || "library";
}

/** The badge's tooltip: where the skill came from, with the commit and date for installed ones. */
export function skillOriginTitle(source: string | undefined, repo: string, from?: InstalledFrom): string {
  if (source === "local") return "your own skill";
  if (source === "builtin") return "shipped with agentline";
  if (source === "installed") {
    if (!from) return "installed from a source";
    const when = from.installed_at ? new Date(from.installed_at * 1000).toLocaleDateString() : "";
    return [`installed from ${from.source}`, from.sha && `commit ${from.sha.slice(0, 7)}`, when && `on ${when}`]
      .filter(Boolean)
      .join(", ");
  }
  return `from the ${repo} library`;
}

/** A description on one line: whitespace and line breaks collapsed (CSS clips the rest). */
export function oneLine(text: string): string {
  return text.replace(/\s+/g, " ").trim();
}

/** A SKILL.md without its `---` front matter (name/description are separate fields). */
export function stripFrontMatter(text: string): string {
  if (!text.startsWith("---")) return text;
  const end = text.indexOf("\n---", 3);
  return end < 0 ? text : text.slice(end + 4).replace(/^\n+/, "");
}

/** Which harness (and model) the Chat uses: the first-run setup choice (PUT /api/setup/choice). */
export function ChatHarnessBar({
  harnesses,
  compatible,
  presets = [],
  onHarnessAdded,
  openInstructions = false,
}: {
  /** show the Chat instructions editor under the bar from the start */
  openInstructions?: boolean;
  /** the harnesses that are set up: the only ones the Chat can use */
  harnesses: string[];
  /** per harness, the models it can use (Settings `compatible_models`) */
  compatible: Record<string, string[]>;
  /** harnesses that can be set up from here ("+ Add a harness…") */
  presets?: HarnessPreset[];
  onHarnessAdded?: () => void;
}) {
  const [choice, setChoice] = useState<{ harness: string; model: string | null } | null>(null);
  const [harness, setHarness] = useState("");
  const [model, setModel] = useState("");
  const [msg, setMsg] = useState<string | null>(null);
  useEffect(() => {
    api
      .setup()
      .then((s) => {
        const c = s.choice ?? s.assistant;
        setChoice(c);
        setHarness(c?.harness ?? BUILTIN_HARNESS);
        setModel(c?.model ?? "");
      })
      .catch(() => setChoice(null));
  }, []);
  const changed = choice?.harness !== harness || (choice?.model ?? "") !== model;
  const [showInstructions, setShowInstructions] = useState(openInstructions);
  const [adding, setAdding] = useState(false);
  const pick = (h: string) => {
    setHarness(h);
    setModel(modelAfterHarnessChange(compatible, h, model));
  };
  return (
    <>
    <div className="chat-harness-bar">
      <strong>Chat uses</strong>
      <select
        value={harness}
        aria-label="harness the Chat uses"
        onChange={(e) => (e.target.value === ADD_HARNESS ? setAdding(true) : pick(e.target.value))}
      >
        {/* only harnesses that are set up; a saved choice that isn't any more is marked */}
        {!harnesses.includes(harness) && harness && <option value={harness}>{harness} (not set up)</option>}
        {harnesses.map((a) => (
          <option key={a} value={a}>
            {a === BUILTIN_HARNESS ? `${a} (works with just an API key)` : a}
          </option>
        ))}
        <AddHarnessOption presets={presets} />
      </select>
      {adding && (
        <AddHarnessDialog
          presets={presets}
          onAdded={(name) => {
            setAdding(false);
            pick(name);
            onHarnessAdded?.();
          }}
          onCancel={() => setAdding(false)}
        />
      )}
      <select value={model} aria-label="model the Chat uses" onChange={(e) => setModel(e.target.value)}>
        <option value="">{harness}'s default model</option>
        {chatModelOptions(compatible, harness, model).map((o) => (
          <option key={o.value} value={o.value}>
            {o.label}
          </option>
        ))}
      </select>
      <button
        type="button"
        className="primary"
        disabled={!changed || (!!model && !(compatible[harness] ?? []).includes(model))}
        onClick={async () => {
          try {
            const s = await api.setupChoice(harness, model || undefined);
            setChoice(s.choice);
            setMsg("Saved.");
          } catch (e) {
            setMsg(errText(e));
          }
        }}
      >
        Save
      </button>
      <button type="button" className="link" aria-expanded={showInstructions} onClick={() => setShowInstructions((v) => !v)}>
        {showInstructions ? "Hide instructions" : "Instructions"}
      </button>
      <span className="small muted">
        {msg ?? "The Chat panel and chat apps use this unless you pick another harness in the panel."}
      </span>
      <VisionSelect />
    </div>
    {showInstructions && <ChatInstructionsEditor />}
    </>
  );
}

/** "Images": who reads attached images when the Chat's harness can't see them (agentline/vision.py). */
function VisionSelect() {
  const [v, setV] = useState<VisionStatus | null>(null);
  const [err, setErr] = useState<string | null>(null);
  useEffect(() => {
    api.vision().then(setV).catch((e) => setErr(errText(e)));
  }, []);
  if (!v) return err ? <span className="issue small">{err}</span> : null;
  const now = v.effective === "@claude-code" ? "Claude Code" : v.effective;
  const auto = now ? `Auto (now ${now})` : "Auto (none set up)";
  return (
    <span className="row vision-select">
      <strong>Images</strong>
      <select
        value={v.choice}
        aria-label="who reads images for harnesses that can't see them"
        onChange={async (e) => {
          try {
            setV(await api.setVision(e.target.value));
            setErr(null);
          } catch (x) {
            setErr(errText(x));
          }
        }}
      >
        {v.options.map((o) => (
          <option key={o.id} value={o.id} disabled={!o.available && o.id !== v.choice}>
            {o.id === "@auto" ? auto : o.label}
            {o.note ? ` (${o.note})` : ""}
          </option>
        ))}
      </select>
      <span className="small muted">
        {err ?? "Claude Code sees images itself; other harnesses get a transcript read by this."}
      </span>
    </span>
  );
}

/** The Chat's general instructions: every Chat turn gets them (agentline.chat_instructions).
 * Skills named in backticks are the ones the Chat invokes when the instructions say so. */
export function ChatInstructionsEditor() {
  const [saved, setSaved] = useState<ChatInstructions | null>(null);
  const [draft, setDraft] = useState("");
  const [msg, setMsg] = useState<string | null>(null);
  const [skills, setSkills] = useState<string[] | null>(null);
  useEffect(() => {
    api
      .chatInstructions()
      .then((r) => {
        setSaved(r);
        setDraft(r.text);
      })
      .catch((e) => setMsg(errText(e)));
    api
      .skills()
      .then((r) => setSkills(r.skills.map((x) => x.name)))
      .catch(() => setSkills(null));
  }, []);
  if (!saved) return msg ? <p className="issue">{msg}</p> : null;
  const st = instructionsState(draft, saved.text, saved.default);
  const named = instructionSkills(draft);
  const unknown = skills ? named.filter((n) => !skills.includes(n)) : [];
  const done = (r: ChatInstructions, note: string) => {
    setSaved(r);
    setDraft(r.text);
    setMsg(note);
  };
  return (
    <section className="chat-instructions" aria-labelledby="chat-instructions-title">
      <h3 id="chat-instructions-title">Chat instructions</h3>
      <p className="small muted">
        Every Chat message gets these, in the panel and in chat apps. Name a skill in backticks (<code>`interview-me`</code>)
        and say when to use it: Claude Code invokes it, other harnesses get its text.
      </p>
      <textarea
        className="mono"
        rows={6}
        value={draft}
        aria-label="Chat instructions"
        onChange={(e) => {
          setDraft(e.target.value);
          setMsg(null);
        }}
      />
      {named.length > 0 && (
        <p className="small">
          Skills used: {named.join(", ")}
          {unknown.length > 0 && <span className="issue"> · not in your skills: {unknown.join(", ")}</span>}
        </p>
      )}
      <div className="row">
        <button
          type="button"
          className="primary"
          disabled={!st.dirty}
          onClick={async () => {
            try {
              done(await api.putChatInstructions(draft), "Saved. The next Chat message uses them.");
            } catch (e) {
              setMsg(errText(e));
            }
          }}
        >
          Save
        </button>
        <button
          type="button"
          disabled={!st.canReset}
          onClick={async () => {
            if (!window.confirm("Replace the Chat instructions with the defaults?")) return;
            try {
              done(await api.resetChatInstructions(), "Back to the defaults.");
            } catch (e) {
              setMsg(errText(e));
            }
          }}
        >
          Reset to defaults
        </button>
        {msg && <span className="small muted">{msg}</span>}
      </div>
    </section>
  );
}

/** Engine, folders and Jira polling. In the Mac app the folder, restart and Finder buttons go
 * through the native bridge; in a browser through the engine API (spec 006 US3). */
export function GeneralSection({ children }: { children?: ReactNode }) {
  const [info, setInfo] = useState<AppInfo | null>(null);
  const [web, setWeb] = useState<AppInfoApi | null>(null);
  const [status, setStatus] = useState<{ version: string; pipelines: string[]; jira_poll?: Record<string, unknown> } | null>(null);
  const [msg, setMsg] = useState<string | null>(null);
  const load = async () => {
    try {
      setStatus(await api.status());
      if (isMacApp()) setInfo(await nativeCall<AppInfo>("app.info"));
      else setWeb(await api.appInfo());
    } catch (e) {
      setMsg(errText(e));
    }
  };
  useEffect(() => {
    void load();
  }, []);
  const poll = (status?.jira_poll ?? {}) as { enabled?: boolean; every_s?: number; last_poll_at?: number; last_error?: string; pipelines?: string[] };
  return (
    <div className="settings-form panel wide general">
      <h3>Setup</h3>
      <p className="help">
        The first-run setup: connect a model API, then pick the harness the Chat uses.{" "}
        <button type="button" onClick={() => window.dispatchEvent(new Event(OPEN_SETUP_EVENT))}>
          Run setup again
        </button>
      </p>
      <h3>Engine</h3>
      <dl className="kv">
        <dt>Version</dt>
        <dd>{status?.version ?? "unknown"}</dd>
        {info && (
          <>
            <dt>Status</dt>
            <dd>{info.engineState}</dd>
            <dt>Address</dt>
            <dd className="mono">{info.address ?? "not running"}</dd>
            <dt>Data folder</dt>
            <dd className="mono">{info.dataFolder}</dd>
            <dt>Config</dt>
            <dd className="mono">{info.configPath}</dd>
          </>
        )}
        {web && (
          <>
            <dt>Address</dt>
            <dd className="mono">{web.address ?? window.location.origin}</dd>
            <dt>Data folder</dt>
            <dd>
              <CopyPath path={web.data_folder} canReveal={web.can_reveal} target="data" />
            </dd>
            <dt>Config</dt>
            <dd>
              <CopyPath path={web.config_path} canReveal={web.can_reveal} target="config" />
            </dd>
          </>
        )}
        <dt>Pipelines loaded</dt>
        <dd>{status?.pipelines.join(", ") || "none"}</dd>
      </dl>
      <p className="small">
        <a href="#/app-reports">App reports</a>: crash reports and other problems in agentline itself, ready to report on GitHub.
      </p>
      {web && <BrowserEngine info={web} onChanged={load} />}
      <GitHubSetup />
      <TelemetrySetting />
      {info?.dev && <p className="small warn">Development mode: AGENTLINE_DEV_PYTHON is set.</p>}
      {isMacApp() && (
        <div className="row">
          <button type="button" onClick={() => void nativeCall("engine.restart")}>
            Restart engine
          </button>
          <button type="button" onClick={() => void nativeCall("finder.dataFolder")}>
            Show data folder
          </button>
        </div>
      )}
      {info && (
        <>
          <h3>Pipelines folder</h3>
          <p className="mono small">{info.pipelinesDir}</p>
          <div className="row">
            <button
              type="button"
              onClick={async () => {
                const r = await nativeCall<{ path?: string }>("folder.choose");
                if (r.path) {
                  setMsg("Takes effect when the engine restarts.");
                  await load();
                }
              }}
            >
              Choose…
            </button>
            <button type="button" onClick={() => void nativeCall("finder.reveal", { path: info.pipelinesDir })}>
              Show in Finder
            </button>
          </div>
          <p className="small muted">An empty folder gets the ticket-to-eod template.</p>
        </>
      )}
      <h3>Jira polling</h3>
      <p className="small">
        {poll.enabled ? `On, every ${Math.round(poll.every_s ?? 0)} s` : "Off"}
        {poll.last_poll_at ? ` · last poll ${new Date(poll.last_poll_at * 1000).toLocaleString()}` : ""}
      </p>
      {poll.last_error && <p className="issue small">{poll.last_error}</p>}
      <p className="small muted">
        Polling is a pipeline trigger (<code>trigger: {"{ type: poll, source: jira, … }"}</code>); edit it in the pipeline.
        It needs JIRA_BASE_URL, JIRA_EMAIL and JIRA_API_TOKEN under Other keys below.
      </p>
      {msg && <p className="small muted">{msg}</p>}
      {children}
    </div>
  );
}

/** Settings > General in a browser: restart, the pipelines folder, notifications, sign out. */
function BrowserEngine({ info, onChanged }: { info: AppInfoApi; onChanged: () => Promise<void> }) {
  const [path, setPath] = useState(info.pipelines_dir);
  const [picking, setPicking] = useState(false);
  const [msg, setMsg] = useState<string | null>(null);
  const [who, setWho] = useState<Identity | null>(null);
  useEffect(() => setPath(info.pipelines_dir), [info.pipelines_dir]);
  useEffect(() => {
    if (hasToken()) void checkToken().then((r) => setWho(r.identity ?? null), () => setWho(null));
  }, []);
  const save = async (p: string) => {
    try {
      const r = await api.setPipelinesDir(p);
      setMsg([r.seeded ? "Saved; the empty folder got the ticket-to-eod template." : "Saved.", r.note].filter(Boolean).join(" "));
      await onChanged();
    } catch (e) {
      setMsg(errText(e));
    }
  };
  return (
    <>
      {info.can_restart && (
        <div className="row">
          <button
            type="button"
            onClick={() => restartEngine().catch((e: unknown) => setMsg(errText(e)))}
          >
            Restart engine
          </button>
        </div>
      )}
      <h3>Pipelines folder</h3>
      <CopyPath path={info.pipelines_dir} canReveal={info.can_reveal} target="pipelines" />
      <form
        className="row"
        onSubmit={(e) => {
          e.preventDefault();
          void save(path);
        }}
      >
        <input className="mono grow" aria-label="pipelines folder" value={path} onChange={(e) => setPath(e.target.value)} />
        <button type="button" onClick={() => setPicking(true)}>
          Browse…
        </button>
        <button type="submit" disabled={path.trim() === info.pipelines_dir}>
          Use this folder
        </button>
      </form>
      <p className="small muted">A folder on the engine's machine. An empty folder gets the ticket-to-eod template. No restart needed.</p>
      {msg && <p className="small muted">{msg}</p>}
      {picking && (
        <FolderPicker
          start={path}
          title="Choose the pipelines folder"
          onCancel={() => setPicking(false)}
          onChoose={(p) => {
            setPicking(false);
            setPath(p);
            void save(p);
          }}
        />
      )}
      <NotificationsSetting />
      {hasToken() && (
        <>
          <h3>Sign-in</h3>
          <p className="small">
            This browser is signed in{who ? ` as ${identityLabel(who)}` : " with an API token"}.{" "}
            <button
              type="button"
              onClick={() => {
                clearToken();
                window.dispatchEvent(new Event(AUTH_NEEDED_EVENT));
              }}
            >
              Sign out
            </button>
          </p>
        </>
      )}
    </>
  );
}
