// Settings > General > GitHub: where failure-triage reports go (api/github.py). Sign in with the
// GitHub CLI's browser login (works with SSO orgs) or paste a token; pick the repo from the
// detected SSH / HTTPS remotes or type it; test that issues can be opened.
import { useCallback, useEffect, useState } from "react";
import { ApiError, api } from "../api";
import {
  detectedChoices,
  loginPending,
  setupStatus,
  tokenSourceLabel,
  type GithubLogin,
  type GithubSetup,
  type GithubTest,
} from "../lib/github";
import { Status } from "./Icon";

const errText = (e: unknown) => (e instanceof ApiError || e instanceof Error ? e.message : String(e));

export function GitHubSetup() {
  const [g, setG] = useState<GithubSetup | null>(null);
  const [repo, setRepo] = useState("");
  const [token, setToken] = useState("");
  const [login, setLogin] = useState<GithubLogin | null>(null);
  const [test, setTest] = useState<GithubTest | null>(null);
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const s = await api.github();
      setG(s);
      setRepo((r) => r || s.repo || "");
      setLogin(s.login.state === "idle" ? null : s.login);
    } catch (e) {
      setMsg(errText(e));
    }
  }, []);
  useEffect(() => {
    void load();
  }, [load]);

  // while the browser sign-in waits, poll it; when it ends, reload the status
  useEffect(() => {
    if (!loginPending(login)) return;
    const t = setTimeout(async () => {
      try {
        const l = await api.githubLoginState();
        setLogin(l);
        if (!loginPending(l)) await load();
      } catch (e) {
        setMsg(errText(e));
      }
    }, 2000);
    return () => clearTimeout(t);
  }, [login, load]);

  const run = async (fn: () => Promise<unknown>) => {
    setBusy(true);
    setMsg(null);
    try {
      await fn();
    } catch (e) {
      setMsg(errText(e));
    } finally {
      setBusy(false);
    }
  };
  const save = (body: { repo?: string; auto?: boolean }) =>
    run(async () => {
      const s = await api.githubSave(body);
      setG(s);
      if (body.repo !== undefined) setRepo(s.repo ?? "");
      setTest(null);
    });

  if (!g) return msg ? <p className="issue small">{msg}</p> : null;
  const st = setupStatus(g);
  return (
    <section className="github-setup" aria-label="GitHub">
      <div className="row">
        <h3>GitHub</h3>
        <Status s={st.s} pill>
          {st.word}
        </Status>
      </div>
      <p className="small muted">Where agentline reports its own problems (App reports). Nothing goes to GitHub until this is set up.</p>

      <h4>Repository</h4>
      <form
        className="row"
        onSubmit={(e) => {
          e.preventDefault();
          void save({ repo: repo.trim() });
        }}
      >
        <input value={repo} placeholder="owner/name, or a git@github.com:… / https://github.com/… URL" aria-label="GitHub repository" onChange={(e) => setRepo(e.target.value)} />
        <button type="submit" disabled={busy || repo.trim() === (g.repo ?? "")}>
          Save
        </button>
      </form>
      {g.error && <p className="issue small">{g.error}</p>}
      {detectedChoices(g).length > 0 && (
        <div className="row github-detected">
          <span className="small muted">Found in git remotes:</span>
          {detectedChoices(g).map((d) => (
            <button key={d.repo} type="button" className="link" disabled={busy} onClick={() => void save({ repo: d.repo })}>
              Use {d.label}
            </button>
          ))}
        </div>
      )}

      <h4>Sign-in</h4>
      <p className="small">
        Using: <strong>{tokenSourceLabel(g.token_source, g.gh.user)}</strong>
      </p>
      {g.gh.installed ? (
        g.gh.logged_in ? (
          <p className="small muted">Signed in with the GitHub CLI{g.gh.user ? ` as ${g.gh.user}` : ""}. A pasted token below takes over from it.</p>
        ) : loginPending(login) ? (
          <div className="github-code">
            <p className="small">Enter this code on GitHub, then approve agentline (SSO organisations ask you to authorise it too):</p>
            <p className="mono github-code-value">{login?.code ?? "…"}</p>
            <div className="row">
              <a className="button-link" href={login?.url ?? "https://github.com/login/device"} target="_blank" rel="noreferrer">
                Open GitHub
              </a>
              <button type="button" className="link" onClick={() => void run(async () => setLogin(await api.githubLoginCancel()))}>
                Cancel
              </button>
              <span className="small muted">Waiting for you to approve…</span>
            </div>
          </div>
        ) : (
          <div className="row">
            <button type="button" className="primary" disabled={busy} onClick={() => void run(async () => setLogin(await api.githubLogin()))}>
              Sign in with GitHub
            </button>
            <span className="small muted">Opens GitHub in your browser (works with SSO).</span>
          </div>
        )
      ) : (
        <p className="small muted">
          To sign in with your browser, install the GitHub CLI (<code>brew install gh</code>) and reopen this page. Or paste a token below.
        </p>
      )}
      {login?.state === "failed" && <p className="issue small">Sign-in failed: {login.error}</p>}
      <form
        className="row"
        onSubmit={(e) => {
          e.preventDefault();
          void run(async () => {
            await api.putSecret("GITHUB_TOKEN", token);
            setToken("");
            await load();
          });
        }}
      >
        <input
          type="password"
          autoComplete="off"
          value={token}
          placeholder="or paste a token (fine-grained, Issues: read and write)"
          aria-label="GitHub token"
          onChange={(e) => setToken(e.target.value)}
        />
        <button type="submit" disabled={busy || !token}>
          Save token
        </button>
      </form>

      <h4>Reports</h4>
      <label className="small check">
        <input type="checkbox" checked={g.auto} disabled={busy || !g.repo} onChange={(e) => void save({ auto: e.target.checked })} /> Create issues automatically
        (otherwise each report waits for your Report on GitHub click)
      </label>
      <div className="row">
        <button type="button" disabled={busy || !g.repo} onClick={() => void run(async () => setTest(await api.githubTest()))}>
          Test
        </button>
        {test && (test.ok ? <Status s="done">{`Can open issues in ${test.repo}${test.private ? " (private)" : ""}`}</Status> : <Status s="failed">{test.error ?? "failed"}</Status>)}
      </div>
      {msg && <p className="issue small">{msg}</p>}
    </section>
  );
}
