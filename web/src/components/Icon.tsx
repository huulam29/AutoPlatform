// Inline SVG icons (lucide-style: 24 grid, 2 px stroke, currentColor), drawn here so the UI has
// no icon font and no emoji. Status always shows as an icon plus its word (decision 59).
import type { ReactElement } from "react";
import { colorGroup, type ColorGroup } from "../lib/runs";

const PATHS: Record<string, ReactElement> = {
  check: <path d="M5 12.5 10 17l9-10" />,
  spinner: <path d="M12 3a9 9 0 1 1-9 9" />,
  alert: (
    <>
      <path d="M12 3.5 21.5 20h-19z" />
      <path d="M12 10v4M12 17v.01" />
    </>
  ),
  x: <path d="m6 6 12 12M18 6 6 18" />,
  circleDashed: <circle cx="12" cy="12" r="8" strokeDasharray="3 3" />,
  minus: <path d="M5 12h14" />,
  agent: (
    <>
      <rect x="4" y="6" width="16" height="12" rx="3" />
      <path d="M9.5 11v2M14.5 11v2M12 3v3" />
    </>
  ),
  action: <path d="M13 2 4 14h7l-1 8 9-12h-7z" />,
  router: <path d="M3 12h6l4-6h8M9 12l4 6h8" />,
  gate: (
    <>
      <rect x="5" y="10" width="14" height="10" rx="2" />
      <path d="M8 10V7a4 4 0 0 1 8 0v3" />
    </>
  ),
  wait: (
    <>
      <circle cx="12" cy="12" r="9" />
      <path d="M12 7v5l3 2" />
    </>
  ),
  plus: <path d="M12 5v14M5 12h14" />,
  chevronLeft: <path d="m15 5-7 7 7 7" />,
  chevronRight: <path d="m9 5 7 7-7 7" />,
  send: <path d="M4 12 20 4l-5 16-3.5-6.5z" />,
  paperclip: <path d="m20 11.5-7.8 7.8a5 5 0 0 1-7.1-7.1l8.5-8.5a3.3 3.3 0 0 1 4.7 4.7l-8.5 8.5a1.7 1.7 0 0 1-2.4-2.4l7.8-7.8" />,
  chat: <path d="M4 5h16v11H9l-5 4z" />,
  key: (
    <>
      <circle cx="8" cy="15" r="4" />
      <path d="m11 12 8-8M16 7l2 2" />
    </>
  ),
  chevronDown: <path d="m5 9 7 7 7-7" />,
  eye: (
    <>
      <path d="M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7S2 12 2 12z" />
      <circle cx="12" cy="12" r="3" />
    </>
  ),
  eyeOff: (
    <>
      <path d="M3 3l18 18" />
      <path d="M10.6 5.1A10.8 10.8 0 0 1 12 5c6.5 0 10 7 10 7a17 17 0 0 1-3 3.9M6.6 6.6C3.8 8.4 2 12 2 12s3.5 7 10 7c1.9 0 3.6-.6 5-1.4" />
    </>
  ),
  more: (
    <>
      <circle cx="5" cy="12" r="1" />
      <circle cx="12" cy="12" r="1" />
      <circle cx="19" cy="12" r="1" />
    </>
  ),
  search: (
    <>
      <circle cx="11" cy="11" r="7" />
      <path d="m20 20-3.5-3.5" />
    </>
  ),
  history: (
    <>
      <path d="M3 12a9 9 0 1 0 3-6.7L3 8" />
      <path d="M3 3v5h5M12 7v5l3 2" />
    </>
  ),
};

export type IconName = keyof typeof PATHS;

export function Icon({ name, label, className }: { name: IconName; label?: string; className?: string }) {
  return (
    <svg
      className={`icon ${className ?? ""}`}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth={2}
      strokeLinecap="round"
      strokeLinejoin="round"
      role={label ? "img" : undefined}
      aria-label={label}
      aria-hidden={label ? undefined : true}
    >
      {PATHS[name]}
    </svg>
  );
}

const GROUP_ICON: Record<ColorGroup, IconName> = {
  pass: "check",
  run: "spinner",
  attn: "alert",
  fail: "x",
  wait: "circleDashed",
  skip: "minus",
};

export const groupIcon = (g: ColorGroup): IconName => GROUP_ICON[g];

/** A status as icon + word: `<Status s="needs-review" />`. `pill` adds the chip outline. */
export function Status({ s, pill, nodeType, children }: { s?: string; pill?: boolean; nodeType?: string; children?: string }) {
  const g = colorGroup(s, nodeType);
  return (
    <span className={`st st-${g}${pill ? " pill" : ""}`}>
      <Icon name={GROUP_ICON[g]} />
      {children ?? s ?? "not started"}
    </span>
  );
}

/** Icon only, with the status as its accessible name (lists, table cells). */
export function StatusIcon({ s, nodeType }: { s?: string; nodeType?: string }) {
  const g = colorGroup(s, nodeType);
  return (
    <span className={`st st-${g}`}>
      <Icon name={GROUP_ICON[g]} label={s ?? "not started"} />
    </span>
  );
}
