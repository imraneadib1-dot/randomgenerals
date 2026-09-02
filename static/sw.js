/* Service worker for the installable app.
 *
 * WHAT IT DOES NOT DO IS THE IMPORTANT PART.
 *
 * Nothing under /api is ever cached, and no POST is ever touched. A cached
 * chat reply would be a stranger's answer served to the next person on a
 * shared device; a cached /api/credits would show a balance that no longer
 * exists; a cached /api/auth/me would show somebody as signed in after they
 * signed out. The cache holds the shell - markup, styles, script, icons -
 * and nothing that is about a particular person.
 *
 * The version string is what expires the old shell. Bump it whenever the
 * cached asset list changes; the activate handler deletes every cache that
 * is not the current one.
 */
const VERSION = "rg-shell-v1";

/* Deliberately short. Anything that can be fetched on demand is fetched on
 * demand - a precache list that mirrors the whole static folder means a
 * first visit pays for files most people never reach. */
const SHELL = [
  "/app",
  "/static/style.css",
  "/static/script.js",
  "/static/icons/icon-192.png",
  "/static/icons/icon-512.png",
  "/static/manifest.webmanifest",
];

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(VERSION).then((cache) =>
      // addAll rejects the whole install if any single file 404s, which
      // would leave the app with no worker at all. Each file is added on
      // its own so one missing asset cannot take the rest down.
      Promise.all(
        SHELL.map((url) =>
          cache.add(new Request(url, { cache: "reload" })).catch(() => {}),
        ),
      ),
    ),
  );
  // Take over as soon as installed rather than waiting for every tab to
  // close - otherwise a fix can sit undelivered for days.
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches
      .keys()
      .then((keys) =>
        Promise.all(keys.filter((k) => k !== VERSION).map((k) => caches.delete(k))),
      )
      .then(() => self.clients.claim()),
  );
});

self.addEventListener("message", (event) => {
  if (event.data === "skip-waiting") self.skipWaiting();
});

function isPrivate(url) {
  // Everything that is about a person, or that streams. /api/chat is an
  // SSE-style stream; putting a stream through the cache would buffer it
  // and the reply would arrive all at once at the end, or not at all.
  return (
    url.pathname.startsWith("/api/") ||
    url.pathname.startsWith("/v1/") ||
    url.pathname.startsWith("/profile") ||
    url.pathname.startsWith("/stats")
  );
}

self.addEventListener("fetch", (event) => {
  const request = event.request;

  // Only ever GET, and only ever this origin. A POST is an action, and an
  // action must not be replayed from a cache.
  if (request.method !== "GET") return;

  const url = new URL(request.url);
  if (url.origin !== self.location.origin) return;
  if (isPrivate(url)) return;

  // Documents: network first, so a deploy is picked up on the next load.
  // The cache is the fallback for being offline, not the primary source.
  if (request.mode === "navigate") {
    event.respondWith(
      fetch(request)
        .then((response) => {
          const copy = response.clone();
          caches.open(VERSION).then((c) => c.put("/app", copy));
          return response;
        })
        .catch(() =>
          caches.match("/app").then(
            (hit) =>
              hit ||
              new Response(
                "<!doctype html><meta charset=utf-8>" +
                  "<title>Offline</title>" +
                  "<style>body{font:16px system-ui;margin:0;display:grid;" +
                  "place-items:center;height:100vh;background:#141a22;" +
                  "color:#e9e8e1}</style>" +
                  "<p>You are offline. Reconnect and this page will load.</p>",
                { headers: { "Content-Type": "text/html; charset=utf-8" } },
              ),
          ),
        ),
    );
    return;
  }

  // Static assets: cache first. They carry a ?v= cache-buster from the
  // app's asset() helper, so a changed file arrives under a new URL and
  // never collides with the stored copy.
  event.respondWith(
    caches.match(request).then(
      (hit) =>
        hit ||
        fetch(request).then((response) => {
          if (response.ok && response.type === "basic") {
            const copy = response.clone();
            caches.open(VERSION).then((c) => c.put(request, copy));
          }
          return response;
        }),
    ),
  );
});
