// The waiting-approvals count where a browser can show it (spec 006 FR-021): the tab title, the
// tab icon, and the app icon when Studio is installed. The Mac app has its Dock badge instead.
import { isMacApp } from "./native";

export const BASE_TITLE = "Agentline Studio";
const ICON = "/icon.svg";

export const titleFor = (n: number): string => (n > 0 ? `(${n}) ${BASE_TITLE}` : BASE_TITLE);

/** The tab icon with a count dot, as an SVG data URL (the colours are the icon's own). */
export function badgedIcon(n: number): string {
  const label = n > 99 ? "99+" : String(n);
  const svg =
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">' +
    '<rect width="64" height="64" rx="14" fill="#0a0e14"/>' +
    '<path d="M12 44h12l8-24 8 24h12" fill="none" stroke="#4fd1e8" stroke-width="5" stroke-linecap="round" stroke-linejoin="round"/>' +
    '<circle cx="46" cy="18" r="17" fill="#f2b33d"/>' +
    `<text x="46" y="25" font-size="${label.length > 1 ? 16 : 22}" font-family="system-ui,sans-serif" font-weight="700" text-anchor="middle" fill="#241700">${label}</text>` +
    "</svg>";
  return `data:image/svg+xml,${encodeURIComponent(svg)}`;
}

let last = -1;

export function setBadge(n: number): void {
  if (isMacApp() || n === last) return;
  last = n;
  document.title = titleFor(n);
  const link = document.querySelector<HTMLLinkElement>('link[rel="icon"]');
  if (link) link.href = n > 0 ? badgedIcon(n) : ICON;
  const nav = navigator as Navigator & { setAppBadge?: (n: number) => Promise<void>; clearAppBadge?: () => Promise<void> };
  if (n > 0) void nav.setAppBadge?.(n).catch(() => {});
  else void nav.clearAppBadge?.().catch(() => {});
}
