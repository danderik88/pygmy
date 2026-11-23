// Pygmy service worker: push alerts, and the app shell kept at hand so the installed app opens at once on a slow
// link (5G through a relay) instead of staying black until the page arrives. Network first, the copy is the fallback.
// Only the page and its icons are kept: nothing of the printers, no key (it lives in the device's storage).
const CACHE = "pygmy-shell-1";
const SHELL = ["/app", "/manifest.json", "/icon-192.png", "/icon-512.png"];
self.addEventListener("install", e => {
  self.skipWaiting();
  e.waitUntil(caches.open(CACHE).then(c => c.addAll(SHELL)).catch(() => {}));
});
self.addEventListener("activate", e => e.waitUntil(Promise.all([
  clients.claim(),
  caches.keys().then(keys => Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k)))),
])));
self.addEventListener("fetch", e => {
  const url = new URL(e.request.url);
  if (e.request.method !== "GET" || url.origin !== location.origin || !SHELL.includes(url.pathname)) return;
  e.respondWith((async () => {
    const cache = await caches.open(CACHE);
    try {
      const fresh = await Promise.race([fetch(e.request), new Promise((_, no) => setTimeout(no, 4000))]);
      if (fresh.ok) cache.put(url.pathname, fresh.clone());
      return fresh;
    } catch (err) {
      return (await cache.match(url.pathname)) || Response.error();
    }
  })());
});
self.addEventListener("push", e => {
  let m = {};
  try { m = e.data.json(); } catch (x) { m = { title: "Pygmy", body: e.data ? e.data.text() : "" }; }
  e.waitUntil(self.registration.showNotification(m.title || "Pygmy", { body: m.body || "", data: { url: m.url || "/app" }, tag: m.tag, image: m.image }));
});
self.addEventListener("notificationclick", e => {
  e.notification.close();
  const url = new URL(e.notification.data.url, self.location.origin).href;
  e.waitUntil(clients.matchAll({ type: "window", includeUncontrolled: true }).then(list => {
    for (const c of list) if (new URL(c.url).pathname === "/app")
      return c.navigate(url).then(w => (w || c).focus()).catch(() => clients.openWindow(url));
    return clients.openWindow(url);
  }));
});
