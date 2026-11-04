// Agentline Studio's service worker (spec 006 US4). It only routes notification clicks back to
// a Studio page: Approve / Reject / open. It never calls the API (it has no token) and caches
// nothing, so the app always talks to the live engine.
self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", (event) => event.waitUntil(self.clients.claim()));

self.addEventListener("notificationclick", (event) => {
  const n = event.notification;
  const data = n.data || {};
  const action = event.action || "open";
  n.close();
  event.waitUntil(
    (async () => {
      const all = await self.clients.matchAll({ type: "window", includeUncontrolled: true });
      const client = all.find((c) => c.focused) || all[0];
      if (!client) {
        await self.clients.openWindow("/#" + (data.route || "/approvals"));
        return;
      }
      await client.focus();
      client.postMessage({ type: "agentline.notification", action, data });
    })(),
  );
});
