// Browser notifications for gates, finished runs and new problem reports (spec 006 US4). The
// rules are the Mac app's (macos/Sources/AgentlineCore/NotificationPlanner.swift): every waiting
// gate once, runs only when they *become* done / dead / needs-review after a baseline poll.
// One tab speaks for all (a Web Lock); Approve / Reject buttons come through public/sw.js, which
// hands the click back to a Studio page (it has no token, so it never calls the API itself).
// Off in the Mac app: its native Notifier does this.
import { api, ApiError, type Approval, type IncidentSummary } from "../api";
import type { Run } from "../types";
import { isMacApp } from "./native";

export const ANNOUNCED_RUN_STATUSES = new Set(["done", "dead", "needs-review"]);
export const APPROVED_FROM_NOTIFICATION = "approved from a notification";
const PREF_KEY = "agentline.notify";
export const POLL_MS = 5000;
const INCIDENT_EACH_MS = 10 * 60 * 1000;
const INCIDENT_ANY_MS = 60 * 1000;

export interface NotifyEvent {
  kind: "gate" | "run" | "incident";
  key: string;
  title: string;
  body: string;
  /** hash route to open, without the leading "#" */
  route: string;
  run?: string;
  node?: string;
  execution?: string;
}

/** The ticket key a run payload carries (the Mac app's `JSONValue.ticketKey`). */
export function ticketKey(payload: unknown): string | null {
  if (!payload || typeof payload !== "object") return null;
  const p = payload as Record<string, unknown>;
  for (const k of ["issue_key", "ticket", "ticket_key"]) {
    if (typeof p[k] === "string" && p[k]) return p[k] as string;
  }
  const issue = p.issue as Record<string, unknown> | undefined;
  if (issue && typeof issue.key === "string" && issue.key) return issue.key;
  if (typeof p.key === "string" && p.key) return p.key;
  return null;
}

const joined = (...parts: (string | null | undefined)[]) => parts.filter(Boolean).join(" ");

export function gateEvent(a: Approval): NotifyEvent {
  return {
    kind: "gate",
    key: `gate:${a.execution_id}`,
    title: `Approval needed: ${joined(a.pipeline, ticketKey(a.payload))}`,
    body: a.prompt ? a.prompt : `Gate ${a.node_id} is waiting.`,
    route: `/runs/${a.run_id}`,
    run: a.run_id,
    node: a.node_id,
    execution: a.execution_id,
  };
}

export function runEvent(r: Run): NotifyEvent {
  const what = r.status === "needs-review" ? "needs review" : r.status;
  return {
    kind: "run",
    key: `run:${r.id}:${r.status}`,
    title: `Run ${what}: ${joined(r.flow, ticketKey(r.payload))}`,
    body: r.error ? r.error.slice(0, 200) : `Run ${r.id}`,
    route: `/runs/${r.id}`,
    run: r.id,
  };
}

export function incidentEvent(i: IncidentSummary): NotifyEvent {
  return {
    kind: "incident",
    key: `incident:${i.id}:${i.count}`,
    title: "agentline problem report",
    body: i.count > 1 ? `${i.title} (${i.count} times)` : i.title,
    route: "/app-reports",
  };
}

/** Which poll results deserve a notification, never twice for the same thing. `null` = that
 * part of the poll failed (changes nothing). */
export class NotificationPlanner {
  readonly notified = new Set<string>();
  private lastStatus = new Map<string, string>();
  private hasBaseline = false;
  private incidentBaseline = false;
  private lastIncidentAt = new Map<string, number>();
  private lastAnyIncidentAt = -Infinity;

  events(
    approvals: Approval[] | null,
    runs: Run[] | null,
    incidents: IncidentSummary[] | null = null,
    now = Date.now(),
  ): NotifyEvent[] {
    const out: NotifyEvent[] = [];
    for (const a of approvals ?? []) {
      const e = gateEvent(a);
      if (!this.notified.has(e.key)) {
        this.notified.add(e.key);
        out.push(e);
      }
    }
    if (runs) {
      for (const r of runs) {
        const before = this.lastStatus.get(r.id);
        this.lastStatus.set(r.id, r.status);
        if (!this.hasBaseline || before === r.status || !ANNOUNCED_RUN_STATUSES.has(r.status)) continue;
        const e = runEvent(r);
        if (!this.notified.has(e.key)) {
          this.notified.add(e.key);
          out.push(e);
        }
      }
      this.hasBaseline = true;
    }
    if (incidents) {
      for (const i of incidents) {
        const e = incidentEvent(i);
        if (this.notified.has(e.key)) continue;
        this.notified.add(e.key);
        if (!this.incidentBaseline || i.status !== "new") continue;
        // the Mac app's caller rate-limits these: one per report per 10 min, one a minute overall
        if (now - (this.lastIncidentAt.get(i.id) ?? -Infinity) < INCIDENT_EACH_MS) continue;
        if (now - this.lastAnyIncidentAt < INCIDENT_ANY_MS) continue;
        this.lastIncidentAt.set(i.id, now);
        this.lastAnyIncidentAt = now;
        out.push(e);
      }
      this.incidentBaseline = true;
    }
    return out;
  }
}

// -- settings ---------------------------------------------------------------------------------
export type NotifyState = "unsupported" | "off" | "on" | "blocked";

export function notifyState(): NotifyState {
  if (isMacApp() || typeof Notification === "undefined") return "unsupported";
  if (Notification.permission === "denied") return "blocked";
  let pref: string | null = null;
  try {
    pref = localStorage.getItem(PREF_KEY);
  } catch {
    /* no storage: off */
  }
  return pref === "on" && Notification.permission === "granted" ? "on" : "off";
}

/** Only from a click (browsers allow the permission prompt only then). */
export async function turnOnNotifications(): Promise<NotifyState> {
  if (typeof Notification === "undefined") return "unsupported";
  const p = Notification.permission === "granted" ? "granted" : await Notification.requestPermission();
  if (p === "granted") {
    try {
      localStorage.setItem(PREF_KEY, "on");
    } catch {
      /* lasts for this page */
    }
    startNotifier();
  }
  return notifyState();
}

export function turnOffNotifications(): void {
  try {
    localStorage.removeItem(PREF_KEY);
  } catch {
    /* nothing stored */
  }
  stopNotifier();
}

// -- showing ----------------------------------------------------------------------------------
async function show(e: NotifyEvent): Promise<void> {
  const data = { route: e.route, run: e.run, node: e.node, execution: e.execution, kind: e.kind };
  const reg = "serviceWorker" in navigator ? await navigator.serviceWorker.getRegistration() : undefined;
  if (reg?.active) {
    const actions =
      e.kind === "gate"
        ? [
            { action: "approve", title: "Approve" },
            { action: "reject", title: "Reject…" },
          ]
        : [];
    await reg.showNotification(e.title, { body: e.body, tag: e.key, data, actions } as NotificationOptions);
    return;
  }
  const n = new Notification(e.title, { body: e.body, tag: e.key, data });
  n.onclick = () => {
    window.focus();
    window.location.hash = e.route;
    n.close();
  };
}

/** What a click on a notification (or its buttons) does in this page. */
export async function handleNotificationAction(
  action: string,
  data: { route?: string; run?: string; node?: string; execution?: string },
  deps: { approve: typeof api.approve; go: (route: string) => void; note: (title: string, body: string) => void } = {
    approve: api.approve,
    go: (route) => {
      window.location.hash = route;
    },
    note: (title, body) => {
      if (typeof Notification !== "undefined") new Notification(title, { body });
    },
  },
): Promise<void> {
  const route = data.route ?? "/approvals";
  if (action === "approve" && data.run && data.node) {
    try {
      await deps.approve(data.run, data.node, APPROVED_FROM_NOTIFICATION);
    } catch (e) {
      if (e instanceof ApiError && (e.status === 404 || e.status === 409)) {
        deps.note("Already decided", `Gate ${data.node} of run ${data.run} was already decided.`);
      }
      deps.go(route);
    }
    return;
  }
  if (action === "reject" && data.execution) {
    // the Mac app's Reject…: open the gate with a comment field; nothing is rejected until
    // the person presses Reject there
    deps.go(`/approvals?reject=${encodeURIComponent(data.execution)}`);
    return;
  }
  deps.go(route);
}

// -- the poll loop (one tab speaks) -----------------------------------------------------------
let timer: ReturnType<typeof setInterval> | null = null;
let releaseLock: (() => void) | null = null;
let listening = false;

function listen(): void {
  if (listening || !("serviceWorker" in navigator)) return;
  listening = true;
  navigator.serviceWorker.addEventListener("message", (ev: MessageEvent) => {
    const m = ev.data as { type?: string; action?: string; data?: Record<string, string> } | null;
    if (m?.type === "agentline.notification") void handleNotificationAction(m.action ?? "open", m.data ?? {});
  });
}

function poll(planner: NotificationPlanner): void {
  const tick = async () => {
    const [a, r, i] = await Promise.all([
      api.approvals().catch(() => null),
      api.runs("", 50).then((x) => x.runs).catch(() => null),
      api.incidents().then((x) => x.incidents).catch(() => null),
    ]);
    for (const e of planner.events(a, r, i)) await show(e).catch(() => {});
  };
  void tick();
  timer = setInterval(() => void tick(), POLL_MS);
}

/** Start notifying when turned on (a no-op otherwise, and in the Mac app). */
export function startNotifier(): void {
  listen(); // every tab handles clicks; only the leader polls
  if (timer || releaseLock || notifyState() !== "on") return;
  const planner = new NotificationPlanner();
  const locks = (navigator as Navigator & { locks?: LockManager }).locks;
  if (!locks) {
    poll(planner);
    return;
  }
  void locks.request(
    "agentline-notify",
    () =>
      new Promise<void>((resolve) => {
        releaseLock = resolve; // held until this tab closes or notifications go off
        if (notifyState() === "on") poll(planner);
        else resolve();
      }),
  );
}

export function stopNotifier(): void {
  if (timer) clearInterval(timer);
  timer = null;
  releaseLock?.();
  releaseLock = null;
}
