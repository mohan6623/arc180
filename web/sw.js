/* ARC/180 service worker.
 *
 * The app shell is network-first: a cache-first shell served a stale index.html
 * for days while app.js kept changing, and the mismatch broke the page. Assets
 * that never change (font, icons) stay cache-first. The API is never cached.
 */
const CACHE = "arc180-v3";
const SHELL = ["/", "/app.js", "/manifest.webmanifest"];
const IMMUTABLE = /\.(woff2|png|svg|ico)$/;

self.addEventListener("install", e => {
  e.waitUntil(caches.open(CACHE).then(c => c.addAll(SHELL)).catch(() => {}));
  self.skipWaiting();
});

self.addEventListener("activate", e => {
  e.waitUntil(caches.keys().then(keys =>
    Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k)))));
  self.clients.claim();
});

self.addEventListener("fetch", e => {
  const url = new URL(e.request.url);
  if (e.request.method !== "GET" || url.origin !== self.location.origin) return;
  if (url.pathname.startsWith("/api/")) return;               // always live

  if (IMMUTABLE.test(url.pathname)) {                          // cache-first
    e.respondWith(caches.match(e.request).then(hit => hit || fetch(e.request)
      .then(r => { const c = r.clone(); caches.open(CACHE).then(x => x.put(e.request, c)); return r; })));
    return;
  }

  e.respondWith(                                               // network-first
    fetch(e.request)
      .then(r => {
        const copy = r.clone();
        caches.open(CACHE).then(c => c.put(e.request, copy));
        return r;
      })
      .catch(() => caches.match(e.request).then(hit => hit || caches.match("/")))
  );
});
