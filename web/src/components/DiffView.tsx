import { diffStats, lineDiff, withContext } from "../lib/diff";

export function DiffView({ before, after }: { before: string; after: string }) {
  const lines = lineDiff(before, after);
  const s = diffStats(lines);
  if (!s.added && !s.removed) return <p className="muted small">No changes.</p>;
  return (
    <div className="diff">
      <div className="small muted">
        <span className="diff-add">+{s.added}</span> <span className="diff-del">−{s.removed}</span>
      </div>
      <pre className="mono small">
        {withContext(lines).map((l, k) =>
          l === null ? (
            <div key={k} className="diff-gap">
              ⋯
            </div>
          ) : (
            <div key={k} className={l.op === "+" ? "diff-add" : l.op === "-" ? "diff-del" : undefined}>
              {l.op} {l.text}
            </div>
          ),
        )}
      </pre>
    </div>
  );
}
