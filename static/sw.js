// LIFELOG service worker: offline app shell + notifications. Data (/api) is always fetched live.
const CACHE = "lifelog-shell-v4";
// "/" needs a session (it redirects to /login otherwise), so it is cached on the first signed-in visit, not at install
const SHELL = ["/static/style.css?v=13", "/static/app.js?v=17", "/static/icon.svg", "/manifest.webmanifest"];
const CACHEABLE = [...SHELL, "/"];

self.addEventListener("install", (e) => {
  e.waitUntil(caches.open(CACHE).then((c) => c.addAll(SHELL)).then(() => self.skipWaiting()));
});

self.addEventListener("activate", (e) => {
  e.waitUntil(caches.keys().then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
    .then(() => self.clients.claim()));
});

self.addEventListener("fetch", (e) => {
  const url = new URL(e.request.url);
  if (e.request.method !== "GET" || url.origin !== location.origin || url.pathname.startsWith("/api/")) return;
  // network first, fall back to the cached shell when offline
  e.respondWith(
    fetch(e.request)
      .then((res) => {
        if (res.ok && !res.redirected && CACHEABLE.includes(url.pathname + url.search)) caches.open(CACHE).then((c) => c.put(e.request, res.clone()));
        return res;
      })
      .catch(() => caches.match(e.request).then((hit) => hit || caches.match("/"))),
  );
});

self.addEventListener("notificationclick", (e) => {
  e.notification.close();
  e.waitUntil(self.clients.matchAll({ type: "window" }).then((wins) => {
    const target = "/#view=alerts";
    for (const w of wins) { if ("focus" in w) { w.navigate(target); return w.focus(); } }
    return self.clients.openWindow(target);
  }));
});
