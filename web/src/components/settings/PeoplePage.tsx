// Settings > People (spec 007): everyone who uses this engine with their own token, so the
// audit trail names who approved, rejected or retried. Admins (and the engine's own machine, and
// the shared API token) add, reissue and revoke; anyone else sees only themselves.

import { useEffect, useState } from "react";
import { ApiError, api } from "../../api";
import { copyText } from "../../lib/clipboard";
import { identityLabel, personNameProblem, type Identity, type Person } from "../../lib/people";
import { Rows, RowItem, NewRow, SettingsPage } from "../SettingsRows";

const errText = (e: unknown) => (e instanceof ApiError || e instanceof Error ? e.message : String(e));

/** A new token, shown once. Closing it forgets the token. */
export function TokenDialog({ name, token, onClose }: { name: string; token: string; onClose: () => void }) {
  const [copied, setCopied] = useState(false);
  return (
    <div className="modal-backdrop" role="dialog" aria-modal="true" aria-labelledby="token-title">
      <div className="modal">
        <h2 id="token-title">Token for {name}</h2>
        <p className="muted">
          Send it to {name} privately. It is shown only now: the engine keeps a fingerprint of it, not the token.
          They sign in to Studio with it.
        </p>
        <code className="mono token-once" aria-label="token">
          {token}
        </code>
        <div className="modal-foot">
          <button type="button" onClick={() => void copyText(token).then(setCopied)}>
            {copied ? "Copied" : "Copy"}
          </button>
          <button type="button" className="primary" onClick={onClose}>
            Done
          </button>
        </div>
      </div>
    </div>
  );
}

export function PeoplePage() {
  const [people, setPeople] = useState<Person[] | null>(null);
  const [me, setMe] = useState<Identity | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [adding, setAdding] = useState(false);
  const [name, setName] = useState("");
  const [admin, setAdmin] = useState(false);
  const [issued, setIssued] = useState<{ name: string; token: string } | null>(null);
  const [busy, setBusy] = useState(false);

  const load = () =>
    api
      .people()
      .then((r) => {
        setPeople(r.people);
        setMe(r.me);
      })
      .catch((e) => setError(errText(e)));
  useEffect(() => {
    void load();
  }, []);

  const run = async (fn: () => Promise<unknown>) => {
    setBusy(true);
    setError(null);
    try {
      await fn();
      await load();
    } catch (e) {
      setError(errText(e));
    } finally {
      setBusy(false);
    }
  };

  const problem = adding ? personNameProblem(name, (people ?? []).map((p) => p.name)) : null;
  const add = () =>
    run(async () => {
      const r = await api.addPerson(name.trim(), admin);
      setIssued({ name: r.person.name, token: r.token });
      setAdding(false);
      setName("");
      setAdmin(false);
    });

  const canManage = !!me?.admin;
  return (
    <SettingsPage
      id="people"
      title="People"
      intro={
        <>
          Everyone who uses this engine with their own token. Approvals, rejections and retries are recorded under
          their name.{me ? ` You are ${identityLabel(me)}.` : ""}
        </>
      }
      actions={
        canManage && !adding ? (
          <button type="button" className="primary" onClick={() => setAdding(true)}>
            Add person
          </button>
        ) : undefined
      }
    >
      {error && <p className="issue">{error}</p>}
      <Rows label="people">
        {adding && (
          <NewRow title="New person">
            <label className="field">
              <span className="field-name">Name</span>
              <input value={name} autoFocus onChange={(e) => setName(e.target.value)} placeholder="alice" />
            </label>
            <label className="field">
              <span className="field-name">
                <input type="checkbox" checked={admin} onChange={(e) => setAdmin(e.target.checked)} />
                Admin: can add and revoke people
              </span>
            </label>
            {name && problem && <p className="issue">{problem}</p>}
            <div className="modal-foot">
              <button type="button" onClick={() => setAdding(false)} disabled={busy}>
                Cancel
              </button>
              <button type="button" className="primary" disabled={busy || !!problem} onClick={() => void add()}>
                Add and show token
              </button>
            </div>
          </NewRow>
        )}
        {people?.length === 0 && !adding && (
          <li className="ri">
            <div className="ri-line">
              <div className="ri-main static muted">
                No people yet: everyone uses the shared API token, recorded as "shared-token".
              </div>
            </div>
          </li>
        )}
        {people?.map((p) => (
          <RowItem
            key={p.name}
            name={p.name}
            badges={p.admin ? <span className="badge">admin</span> : undefined}
            meta={`added ${new Date(p.created_at * 1000).toLocaleDateString()}`}
            trailing={
              canManage ? (
                <>
                  <button
                    type="button"
                    disabled={busy}
                    onClick={() => void run(() => api.setPersonAdmin(p.name, !p.admin))}
                  >
                    {p.admin ? "Remove admin" : "Make admin"}
                  </button>
                  <button
                    type="button"
                    disabled={busy}
                    onClick={() =>
                      void run(async () => {
                        const r = await api.reissuePerson(p.name);
                        setIssued({ name: r.person.name, token: r.token });
                      })
                    }
                  >
                    New token
                  </button>
                  <button
                    type="button"
                    className="danger"
                    disabled={busy}
                    onClick={() => {
                      if (window.confirm(`Revoke ${p.name}? Their token stops working at once.`))
                        void run(() => api.revokePerson(p.name));
                    }}
                  >
                    Revoke
                  </button>
                </>
              ) : undefined
            }
          />
        ))}
      </Rows>
      {issued && <TokenDialog name={issued.name} token={issued.token} onClose={() => setIssued(null)} />}
    </SettingsPage>
  );
}
