// The settings pages' shared layout (Harnesses, MCP, Skills): a page header
// (title, one line on what the page is for, the primary action), an optional toolbar, then a
// full-width list of rows. A row opens in place into its editor; there is no side panel.
import type { ReactNode } from "react";
import { Icon } from "./Icon";

export function SettingsPage({
  id,
  title,
  intro,
  actions,
  toolbar,
  children,
}: {
  id: string;
  title: string;
  intro: ReactNode;
  actions?: ReactNode;
  toolbar?: ReactNode;
  children: ReactNode;
}) {
  return (
    <section className="sp" aria-labelledby={`sp-${id}`}>
      <header className="sp-head">
        <div className="sp-title">
          <h2 id={`sp-${id}`}>{title}</h2>
          <p className="sp-intro">{intro}</p>
        </div>
        {actions && <div className="sp-actions">{actions}</div>}
      </header>
      {toolbar && <div className="sp-toolbar">{toolbar}</div>}
      {children}
    </section>
  );
}

export function Rows({ children, label }: { children: ReactNode; label: string }) {
  return (
    <ul className="rows" aria-label={label}>
      {children}
    </ul>
  );
}

/** One entry. With `onToggle` the summary is a button that opens `editor` under it; `trailing`
 * holds the row's own buttons (outside that button), `below` always-visible detail lines. */
export function RowItem({
  name,
  badges,
  meta,
  trailing,
  below,
  open = false,
  onToggle,
  editor,
  off = false,
}: {
  name: ReactNode;
  badges?: ReactNode;
  meta?: ReactNode;
  trailing?: ReactNode;
  below?: ReactNode;
  open?: boolean;
  onToggle?: () => void;
  editor?: ReactNode;
  off?: boolean;
}) {
  const summary = (
    <>
      {onToggle && <Icon name="chevronRight" className="ri-chev" />}
      <span className="ri-name">{name}</span>
      {badges}
      {meta !== undefined && <span className="ri-meta">{meta}</span>}
    </>
  );
  return (
    <li className={`ri${open ? " open" : ""}${off ? " off" : ""}`}>
      <div className="ri-line">
        {onToggle ? (
          <button type="button" className="ri-main" aria-expanded={open} onClick={onToggle}>
            {summary}
          </button>
        ) : (
          <div className="ri-main static">{summary}</div>
        )}
        {trailing && <div className="ri-trail">{trailing}</div>}
      </div>
      {below && <div className="ri-below">{below}</div>}
      {open && editor && <div className="ri-editor">{editor}</div>}
    </li>
  );
}

/** A "new entry" row at the top of the list: just its editor, titled. */
export function NewRow({ title, children }: { title: string; children: ReactNode }) {
  return (
    <li className="ri open new">
      <div className="ri-line">
        <div className="ri-main static">
          <Icon name="plus" className="ri-chev" />
          <span className="ri-name">{title}</span>
        </div>
      </div>
      <div className="ri-editor">{children}</div>
    </li>
  );
}
