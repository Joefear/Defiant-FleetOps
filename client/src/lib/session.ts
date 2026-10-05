/** Bearers live in this tab, never in IndexedDB, cached responses or persisted worker state. */
import { request, jsonBody } from "./api";
import type { Identity, Session } from "./types";

const KEY = "fleetops.session.v1";
export function loadSession(): Session | null {
  try {
    const session = JSON.parse(sessionStorage.getItem(KEY) ?? "null") as Session | null;
    if (
      !session ||
      !session.access_token ||
      !session.org_id ||
      !session.actor_id ||
      !Number.isFinite(Date.parse(session.expires_at)) ||
      Date.parse(session.expires_at) <= Date.now()
    ) {
      clearSession();
      return null;
    }
    return session;
  } catch {
    clearSession();
    return null;
  }
}
export function saveSession(session: Session) {
  try {
    sessionStorage.setItem(KEY, JSON.stringify(session));
  } catch {
    // Do not report successful sign-in when its tab-scoped credential cannot persist.
    throw new Error(
      "Browser storage is unavailable. Allow storage for this site, then sign in again.",
    );
  }
}
export function clearSession() {
  try {
    sessionStorage.removeItem(KEY);
  } catch {
    // A browser may deny cleanup as well as reads. Startup must still return
    // signed-out state; authorized logout revokes the server credential first.
  }
}
export async function login(username: string, password: string): Promise<Session> {
  const issued = await request<{ access_token: string; expires_at: string }>(
    "/auth/login",
    null,
    jsonBody({ username, password }),
  );
  const identity = await request<Identity>("/auth/me", issued.access_token);
  const session = { ...issued, ...identity, username };
  saveSession(session);
  return session;
}
