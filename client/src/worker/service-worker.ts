/// <reference lib="webworker" />
/** Static shell only: bearer sessions and authenticated API responses are never cached. */
import { synchronize } from "../lib/sync";
import type { Session } from "../lib/types";
declare const self: ServiceWorkerGlobalScope;
const SHELL = "fleetops-shell-v1";
let session: Session | null = null;
async function notify(value: Record<string, unknown>) {
  for (const client of await self.clients.matchAll({ type: "window" })) client.postMessage(value);
}
async function sync() {
  if (!session) return; // Browser background sync has no authority without a live credential.
  const current = session;
  try {
    await synchronize(current, () => {
      void notify({ type: "QUEUE_CHANGED" });
    });
    await notify({ type: "QUEUE_CHANGED" });
  } catch (error) {
    if (error instanceof Error && "status" in error && error.status === 401) {
      if (session === current) session = null;
      await notify({ type: "AUTH_REQUIRED" });
    } else
      await notify({
        type: "SYNC_ERROR",
        message: error instanceof Error ? error.message : "Sync interrupted",
      });
  }
}
self.addEventListener("install", (event) => {
  event.waitUntil(
    caches
      .open(SHELL)
      .then((cache) =>
        cache.addAll([
          "/",
          "/manifest.webmanifest",
          "/icons/icon.svg",
          "/icons/icon-192.png",
          "/icons/icon-512.png",
        ]),
      )
      .then(() => self.skipWaiting()),
  );
});
self.addEventListener("activate", (event) => {
  event.waitUntil(
    (async () => {
      for (const name of await caches.keys())
        if (name.startsWith("fleetops-shell-") && name !== SHELL) await caches.delete(name);
      await self.clients.claim();
    })(),
  );
});
function staticUrl(url: URL): boolean {
  return (
    url.origin === self.location.origin &&
    (url.pathname.startsWith("/_next/static/") ||
      url.pathname.startsWith("/icons/") ||
      url.pathname === "/manifest.webmanifest")
  );
}
self.addEventListener("fetch", (event) => {
  const req = event.request,
    url = new URL(req.url);
  if (
    req.method !== "GET" ||
    url.origin !== self.location.origin ||
    url.pathname.startsWith("/api/")
  )
    return;
  if (req.mode === "navigate" && url.pathname === "/") {
    event.respondWith(
      fetch(req)
        .then(async (response) => {
          if (response.ok) (await caches.open(SHELL)).put("/", response.clone());
          return response;
        })
        .catch(async () =>
          (await caches.open(SHELL)).match("/").then((value) => value ?? Response.error()),
        ),
    );
  } else if (staticUrl(url)) {
    event.respondWith(
      caches.open(SHELL).then(
        async (cache) =>
          (await cache.match(req)) ??
          fetch(req).then((response) => {
            if (response.ok) void cache.put(req, response.clone());
            return response;
          }),
      ),
    );
  }
});
self.addEventListener("message", (event) => {
  if (
    !event.source ||
    !("url" in event.source) ||
    new URL(event.source.url).origin !== self.location.origin
  )
    return;
  if (event.data?.type === "SESSION") session = event.data.session as Session | null;
  if (event.data?.type === "SYNC") event.waitUntil(sync());
  if (event.data?.type === "WARM_SHELL" && Array.isArray(event.data.urls)) {
    event.waitUntil(
      (async () => {
        const cache = await caches.open(SHELL);
        for (const raw of event.data.urls) {
          if (typeof raw !== "string") continue;
          const url = new URL(raw, self.location.origin);
          if (staticUrl(url)) await cache.add(url.href);
        }
        if (event.source && "postMessage" in event.source)
          event.source.postMessage({ type: "OFFLINE_READY" });
      })().catch(() => notify({ type: "SHELL_INCOMPLETE" })),
    );
  }
});
// Background Sync is progressive enhancement; the page also wakes sync on reconnect.
self.addEventListener("sync", (event: Event) => {
  if ("tag" in event && event.tag === "fleetops-capture")
    (event as unknown as ExtendableEvent).waitUntil(sync());
});
