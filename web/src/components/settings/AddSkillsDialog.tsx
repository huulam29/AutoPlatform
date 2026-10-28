// Settings > Skills > Add skills: search the skills.sh marketplace, or paste a GitHub source
// (`docker/skills`, `owner/repo@skill`, a URL, `npx skills add ...`), pick, install.
// Server: agentline/skill_sources.py via /api/skills/search|preview|install.
import { useEffect, useRef, useState } from "react";
import { ApiError, api, type MarketSkill, type SkillPreview } from "../../api";
import { clashNote, formatInstalls, looksLikeSource, resultSource } from "../../lib/skills";
import { Status } from "../Icon";
import { RowItem, Rows } from "../SettingsRows";

const errText = (e: unknown) => (e instanceof ApiError || e instanceof Error ? e.message : String(e));

export function AddSkillsDialog({ onInstalled, onClose }: { onInstalled: () => void; onClose: () => void }) {
  const [text, setText] = useState("");
  const [results, setResults] = useState<MarketSkill[] | null>(null);
  const [preview, setPreview] = useState<SkillPreview | null>(null);
  const [sel, setSel] = useState<Set<string>>(new Set());
  const [busy, setBusy] = useState<string | null>(null); // what's running: "search" | "preview" | "install" | a result id
  const [err, setErr] = useState<string | null>(null);
  const [done, setDone] = useState<string[]>([]); // installed in this dialog
  const [msg, setMsg] = useState<string | null>(null);
  const seq = useRef(0);
  const source = looksLikeSource(text);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && !busy && onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [busy, onClose]);

  // search as you type (not for sources: those are looked up on Enter / Find skills)
  useEffect(() => {
    const q = text.trim();
    if (source || q.length < 2) {
      setResults(null);
      return;
    }
    const id = ++seq.current;
    const t = window.setTimeout(async () => {
      setBusy("search");
      setErr(null);
      try {
        const r = await api.searchSkills(q);
        if (id === seq.current) setResults(r.skills);
      } catch (e) {
        if (id === seq.current) setErr(errText(e));
      } finally {
        if (id === seq.current) setBusy(null);
      }
    }, 350);
    return () => window.clearTimeout(t);
  }, [text, source]);

  async function find(src: string) {
    setBusy("preview");
    setErr(null);
    setMsg(null);
    try {
      const p = await api.previewSkills(src);
      setPreview(p);
      setSel(new Set(p.skills.length === 1 ? [p.skills[0].name] : []));
    } catch (e) {
      setErr(errText(e));
    } finally {
      setBusy(null);
    }
  }

  async function install(src: string, names: string[] | undefined, key: string) {
    setBusy(key);
    setErr(null);
    setMsg(null);
    try {
      const r = await api.installSkills(src, names);
      setDone((d) => [...d, ...r.result.installed]);
      setMsg(`Installed ${r.result.installed.join(", ")} from ${r.result.source}.`);
      setSel(new Set());
      onInstalled();
    } catch (e) {
      setErr(errText(e));
    } finally {
      setBusy(null);
    }
  }

  const picked = preview ? preview.skills.filter((s) => sel.has(s.name)).map((s) => s.name) : [];

  return (
    <div className="modal-backdrop" role="dialog" aria-modal="true" aria-labelledby="add-skills-title">
      <div className="modal wide add-skills">
        <h2 id="add-skills-title">Add skills</h2>
        <p className="help">
          Search <a href="https://skills.sh" target="_blank" rel="noreferrer">skills.sh</a>, or paste a GitHub source such as{" "}
          <code>docker/skills</code> (the same as <code>npx skills add docker/skills</code>). Skills are instructions your
          agents follow: install only from sources you trust.
        </p>
        <form
          className="inline"
          onSubmit={(e) => {
            e.preventDefault();
            if (source) void find(text);
          }}
        >
          <input
            type="search"
            autoFocus
            value={text}
            placeholder="docker, or owner/repo"
            aria-label="search skills or paste a source"
            onChange={(e) => {
              setText(e.target.value);
              setPreview(null);
            }}
          />
          {source && (
            <button type="submit" className="primary" disabled={!!busy}>
              {busy === "preview" ? "Looking…" : "Find skills"}
            </button>
          )}
        </form>

        {preview ? (
          <div className="pick-list">
            <div className="inline">
              <span>
                <a href={preview.url} target="_blank" rel="noreferrer">
                  <code>{preview.source}</code>
                </a>
                <span className="muted small">
                  {" "}
                  · {preview.skills.length} skill{preview.skills.length === 1 ? "" : "s"}
                  {preview.sha ? ` · ${preview.sha.slice(0, 7)}` : ""}
                </span>
              </span>
              <span className="grow" />
              {preview.skills.length > 1 && (
                <button type="button" onClick={() => setSel(new Set(picked.length === preview.skills.length ? [] : preview.skills.map((s) => s.name)))}>
                  {picked.length === preview.skills.length ? "Clear" : "Select all"}
                </button>
              )}
            </div>
            <ul aria-label="skills in this source">
              {preview.skills.map((s) => {
                const note = clashNote(s.exists, s.installed_from);
                return (
                  <li key={s.name}>
                    <label className="check skill-pick">
                      <input
                        type="checkbox"
                        aria-label={s.name}
                        checked={sel.has(s.name)}
                        disabled={busy === "install"}
                        onChange={() => {
                          const next = new Set(sel);
                          if (next.has(s.name)) next.delete(s.name);
                          else next.add(s.name);
                          setSel(next);
                        }}
                      />
                      <span className="skill-pick-text">
                        <code>{s.name}</code>
                        {done.includes(s.name) && <Status s="done">installed</Status>}
                        {note && !done.includes(s.name) && <span className="muted small"> · {note}</span>}
                        <span className="muted small skill-pick-desc" title={s.description}>
                          {s.description}
                        </span>
                      </span>
                    </label>
                  </li>
                );
              })}
            </ul>
            {!preview.skills.length && <p className="help">No skills found in this source.</p>}
            {preview.skipped.length > 0 && (
              <p className="help">
                Skipped {preview.skipped.map((x) => `${x.path || "(root)"}: ${x.reason}`).join("; ")}
              </p>
            )}
            <div className="inline">
              <button
                type="button"
                className="primary"
                disabled={!!busy || picked.length === 0}
                onClick={() => void install(preview.source, picked, "install")}
              >
                {busy === "install" ? "Installing…" : picked.length ? `Install ${picked.length} skill${picked.length === 1 ? "" : "s"}` : "Install"}
              </button>
            </div>
          </div>
        ) : (
          !source &&
          results !== null && (
            <>
              <Rows label="skills.sh results">
                {results.map((r) => {
                  const installed = r.installed || done.includes(r.skill);
                  return (
                    <RowItem
                      key={r.id}
                      name={r.name}
                      meta={`${r.source} · ${formatInstalls(r.installs)} installs`}
                      trailing={
                        <>
                          <button type="button" className="link" disabled={!!busy} title={`See every skill in ${r.source}`} onClick={() => void find(r.source)}>
                            All from repo
                          </button>
                          {installed ? (
                            <Status s="done">installed</Status>
                          ) : (
                            <button type="button" disabled={!!busy} onClick={() => void install(resultSource(r), undefined, r.id)}>
                              {busy === r.id ? "Installing…" : "Install"}
                            </button>
                          )}
                        </>
                      }
                    />
                  );
                })}
              </Rows>
              {!results.length && <p className="sp-empty">No skills match on skills.sh.</p>}
            </>
          )
        )}
        {busy === "search" && <p className="muted">Searching…</p>}
        {err && <p className="issue">{err}</p>}
        {msg && <p className="help ok">{msg}</p>}
        <div className="modal-foot">
          {preview && !source && (
            <button type="button" disabled={!!busy} onClick={() => setPreview(null)}>
              Back to results
            </button>
          )}
          <span className="grow" />
          <button type="button" disabled={busy === "install"} onClick={onClose}>
            {done.length ? "Done" : "Cancel"}
          </button>
        </div>
      </div>
    </div>
  );
}
