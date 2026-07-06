/* Sahej service worker — offline-first for the field.
 *
 * Pages (navigations): network-first with cache fallback — route/content
 * changes reach users immediately when online, and the last good copy still
 * opens offline.
 * Static assets (icons, manifest): cache-first, refreshed in the background.
 * API (/api/*): network-first with cache fallback, so an ASHA who loses
 * signal mid-visit still sees the last computed plan for each mother.
 */
const VERSION = "sahej-v6";
const SHELL = [
  "/", "/asha", "/about",
  "/manifest.webmanifest",
  "/icon-192.png", "/icon-512.png", "/apple-touch-icon.png", "/favicon-32.png",
];

self.addEventListener("install", (e) => {
  e.waitUntil(caches.open(VERSION).then((c) => c.addAll(SHELL)).then(() => self.skipWaiting()));
});

self.addEventListener("activate", (e) => {
  e.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== VERSION).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", (e) => {
  const url = new URL(e.request.url);
  if (e.request.method !== "GET" || url.origin !== location.origin) return;

  if (url.pathname.startsWith("/api/") || e.request.mode === "navigate") {
    // Network-first; fall back to the last cached response (offline).
    e.respondWith(
      fetch(e.request)
        .then((res) => {
          if (res.ok) {
            const copy = res.clone();
            caches.open(VERSION).then((c) => c.put(e.request, copy));
          }
          return res;
        })
        .catch(() => caches.match(e.request).then((hit) => hit || caches.match("/")))
    );
    return;
  }

  // Shell & static: cache-first, refresh in background.
  e.respondWith(
    caches.match(e.request).then((cached) => {
      const refresh = fetch(e.request)
        .then((res) => {
          if (res.ok) {
            const copy = res.clone();
            caches.open(VERSION).then((c) => c.put(e.request, copy));
          }
          return res;
        })
        .catch(() => cached);
      return cached || refresh;
    })
  );
});
