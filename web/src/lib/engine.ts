// Engine state for a plain browser (spec 006 US1): is the engine answering, and restart it.
// The Mac app shows its own engine banner and restarts through the native bridge, so in the app
// `useEngineHealth` stays "up" and `restartEngine` calls `engine.restart`.
import { useEffect, useState } from "react";
import { api } from "../api";
import { isMacApp, nativeCall } from "./native";

/** Pages reload their data (the engine came back, or the person asked for Refresh). */
export const REFRESH_EVENT = "agentline:refresh";
export const refresh = (): void => {
  window.dispatchEvent(new Event(REFRESH_EVENT));
};

export type EngineState = "up" | "down" | "restarting";

export const HEALTH_EVERY_MS = 3000;
/** Misses in a row before "down": 2 × 3 s keeps the banner inside FR-001's 10 s. */
export const MISSES_FOR_DOWN = 2;

/** One probe of `/healthz` (open, never logged on success). */
export async function engineAnswers(fetcher: typeof fetch = fetch): Promise<boolean> {
  try {
    const r = await fetcher("/healthz", { cache: "no-store" });
    return r.ok;
  } catch {
    return false;
  }
}

/** The health state machine, pure for tests: `next(answered)` → the new state. */
export class HealthTracker {
  state: EngineState = "up";
  misses = 0;
  restarting = false;

  next(answered: boolean): EngineState {
    if (answered) {
      this.misses = 0;
      this.restarting = false;
      this.state = "up";
      return this.state;
    }
    this.misses += 1;
    if (this.restarting) this.state = "restarting";
    else if (this.misses >= MISSES_FOR_DOWN) this.state = "down";
    return this.state;
  }
}

const tracker = new HealthTracker();
const listeners = new Set<(s: EngineState) => void>();

function publish(s: EngineState): void {
  for (const fn of listeners) fn(s);
}

/** Polls the engine while mounted; fires REFRESH_EVENT when it comes back. */
export function useEngineHealth(): EngineState {
  const [state, setState] = useState<EngineState>(tracker.state);
  useEffect(() => {
    if (isMacApp()) return;
    listeners.add(setState);
    let live = true;
    const tick = async () => {
      const before = tracker.state;
      const now = tracker.next(await engineAnswers());
      if (!live) return;
      publish(now);
      if (before !== "up" && now === "up") refresh();
    };
    const t = setInterval(() => void tick(), HEALTH_EVERY_MS);
    return () => {
      live = false;
      listeners.delete(setState);
      clearInterval(t);
    };
  }, []);
  return state;
}

/** Restart Engine (Settings, the command palette, the Chat). */
export async function restartEngine(): Promise<void> {
  if (isMacApp()) {
    await nativeCall("engine.restart");
    return;
  }
  await api.restartEngine();
  tracker.restarting = true;
  tracker.state = "restarting";
  publish("restarting");
}
