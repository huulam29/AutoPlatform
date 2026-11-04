// Sign in with the engine's API token (spec 006 US2), instead of a browser prompt. Shown by App
// while a request is refused for the token; the token is checked before it is kept.
import { useState } from "react";
import { checkToken } from "../api";

export function SignIn({ onDone }: { onDone: () => void }) {
  const [value, setValue] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const submit = async () => {
    if (!value.trim()) return;
    setBusy(true);
    setError(null);
    try {
      const r = await checkToken(value);
      if (r.ok) onDone();
      else setError("The engine refused that token. Check it and try again.");
    } catch (e) {
      setError(`Couldn't reach the engine: ${(e as Error).message}`);
    } finally {
      setBusy(false);
    }
  };
  return (
    <div className="signin">
      <form
        className="signin-card panel"
        aria-labelledby="signin-title"
        onSubmit={(e) => {
          e.preventDefault();
          void submit();
        }}
      >
        <span className="logo">Agentline Studio</span>
        <h2 id="signin-title">Sign in to this engine</h2>
        <p className="help">
          This engine is reachable from other machines, so it asks for a token. Use your own token (an admin makes one
          in Settings &gt; People, or <code>agentline users add &lt;name&gt;</code>), so what you approve is recorded under
          your name. The engine's shared token also works: <code>AGENTLINE_API_TOKEN</code> in the secrets file (
          <code>~/.agentline/secrets.env</code>) on the engine's machine.
        </p>
        <label className="field">
          <span>Your token</span>
          <input
            type="password"
            autoComplete="current-password"
            autoFocus
            value={value}
            onChange={(e) => setValue(e.target.value)}
          />
        </label>
        {error && (
          <p className="issue small" role="alert">
            {error}
          </p>
        )}
        <div className="modal-foot">
          <button type="submit" className="primary" disabled={busy || !value.trim()}>
            {busy ? "Checking…" : "Sign in"}
          </button>
        </div>
        <p className="small muted">The token is kept in this browser only. Sign out in Settings → General.</p>
      </form>
    </div>
  );
}
