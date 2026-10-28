// The Mac app (Agentline.app) hosts this UI in a WKWebView and answers a few requests only it can
// do: Keychain keys, restarting its engine, its own log (app + engine output), choosing the
// pipelines folder, exporting problem reports, showing files in Finder. In a browser these are
// absent and the UI falls back to the engine API (or hides the control).

type Handler = { postMessage: (m: unknown) => Promise<unknown> };

function handler(): Handler | null {
  const w = window as unknown as { webkit?: { messageHandlers?: { agentline?: Handler } } };
  return w.webkit?.messageHandlers?.agentline ?? null;
}

export const isMacApp = (): boolean => handler() !== null;

export async function nativeCall<T>(action: string, args: Record<string, unknown> = {}): Promise<T> {
  const h = handler();
  if (!h) throw new Error("only in the Mac app");
  const reply = (await h.postMessage({ action, ...args })) as { ok?: boolean; error?: string } & T;
  if (reply && reply.ok === false) throw new Error(reply.error ?? `${action} failed`);
  return reply;
}

export interface AppInfo {
  version: string;
  dataFolder: string;
  configPath: string;
  pipelinesDir: string;
  engineState: string;
  address: string | null;
  dev: boolean;
}

export interface NativeKey {
  name: string;
  where: "keychain" | "file" | "environment" | "none";
  jira?: boolean;
}
