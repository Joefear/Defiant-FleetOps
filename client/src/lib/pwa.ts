/** Worker credentials stay in memory; reopening a tab supplies its verified session again. */
import { synchronize } from "./sync";
import type { Session } from "./types";
type WithSync = ServiceWorkerRegistration & { sync?: { register(tag: string): Promise<void> } };
export async function registerWorker(
  onMessage: (value: { type: string; message?: string }) => void,
): Promise<() => void> {
  if (!("serviceWorker" in navigator)) return () => {};
  const handler = (event: MessageEvent) => onMessage(event.data);
  navigator.serviceWorker.addEventListener("message", handler);
  await navigator.serviceWorker.register("/sw.js");
  const ready = await navigator.serviceWorker.ready;
  const urls = [
    ...performance.getEntriesByType("resource").map((value) => value.name),
    ...Array.from(document.querySelectorAll<HTMLScriptElement>("script[src]"), (node) => node.src),
    ...Array.from(document.querySelectorAll<HTMLLinkElement>("link[href]"), (node) => node.href),
  ];
  (navigator.serviceWorker.controller ?? ready.active)?.postMessage({ type: "WARM_SHELL", urls });
  return () => navigator.serviceWorker.removeEventListener("message", handler);
}
export async function wakeSync(session: Session | null, changed: () => void = () => {}) {
  if (!session) {
    navigator.serviceWorker?.controller?.postMessage({ type: "SESSION", session: null });
    return;
  }
  if ("serviceWorker" in navigator) {
    const registration = (await navigator.serviceWorker.getRegistration()) as WithSync | undefined;
    const worker = navigator.serviceWorker.controller ?? registration?.active;
    if (worker) {
      worker.postMessage({ type: "SESSION", session });
      worker.postMessage({ type: "SYNC" });
      await registration?.sync?.register("fleetops-capture").catch(() => {});
      return;
    }
  }
  await synchronize(session, changed);
}
