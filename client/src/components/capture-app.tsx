"use client";
/** Operator verbs retain captured claims locally before showing success. */
import { useCallback, useEffect, useState } from "react";
import { AssetFlow } from "./asset-flow";
import { ReceiveFlow } from "./receive-flow";
import { Inbox } from "./inbox";
import { loadSession, clearSession, login } from "@/lib/session";
import { request } from "@/lib/api";
import { rows } from "@/lib/db";
import { registerWorker, wakeSync } from "@/lib/pwa";
import { unfinished } from "@/lib/sync";
import { scopeOf, type Session, type QueueRow } from "@/lib/types";
const MODES = ["Receive", "Move", "Assign", "Transition", "Inbox"] as const;
type Mode = (typeof MODES)[number];
export function CaptureApp() {
  const [session, setSession] = useState<Session | null>(null),
    [loaded, setLoaded] = useState(false);
  const [mode, setMode] = useState<Mode>("Receive"),
    [records, setRecords] = useState<QueueRow[]>([]);
  const [online, setOnline] = useState(true),
    [offlineReady, setOfflineReady] = useState(false),
    [message, setMessage] = useState(""),
    [error, setError] = useState("");
  const [username, setUsername] = useState(""),
    [password, setPassword] = useState(""),
    [busy, setBusy] = useState(false);
  const refresh = useCallback(async () => {
    setRecords(session ? await rows(scopeOf(session)) : []);
  }, [session]);
  const sync = useCallback(async () => {
    if (!session) return;
    try {
      await wakeSync(session, () => void refresh());
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : "Sync interrupted");
    }
  }, [session, refresh]);
  useEffect(() => {
    setSession(loadSession());
    setLoaded(true);
    setOnline(navigator.onLine);
  }, []);
  useEffect(() => {
    void refresh().catch((failure) => setError(String(failure)));
  }, [refresh]);
  useEffect(() => {
    let remove = () => {};
    let stopped = false;
    registerWorker((value) => {
      if (value.type === "QUEUE_CHANGED") {
        void refresh();
        setError("");
      }
      if (value.type === "OFFLINE_READY") setOfflineReady(true);
      if (value.type === "AUTH_REQUIRED") {
        clearSession();
        setSession(null);
        setError("Sign in again. Your captures remain on this device.");
      }
      if (value.type === "SYNC_ERROR")
        setError(value.message ?? "Sync interrupted; captures remain queued");
    })
      .then((cleanup) => {
        if (stopped) cleanup();
        else remove = cleanup;
      })
      .catch(() =>
        setError("Offline shell is not ready. Keep this tab open until setup succeeds."),
      );
    return () => {
      stopped = true;
      remove();
    };
  }, [refresh]);
  useEffect(() => {
    const up = () => {
        setOnline(true);
        void sync();
      },
      down = () => setOnline(false);
    const visible = () => {
      if (document.visibilityState === "visible" && navigator.onLine) void sync();
    };
    window.addEventListener("online", up);
    window.addEventListener("offline", down);
    document.addEventListener("visibilitychange", visible);
    const timer = window.setInterval(() => {
      if (session && Date.parse(session.expires_at) <= Date.now()) {
        clearSession();
        setSession(null);
        void wakeSync(null);
      } else if (navigator.onLine) void sync();
    }, 15_000);
    void wakeSync(session).catch(() => {});
    return () => {
      clearInterval(timer);
      window.removeEventListener("online", up);
      window.removeEventListener("offline", down);
      document.removeEventListener("visibilitychange", visible);
    };
  }, [session, sync]);
  async function signIn(event: React.FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError("");
    try {
      setSession(await login(username, password));
      setPassword("");
      setMessage("Signed in. Scan a label to begin.");
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : "Sign in failed");
    } finally {
      setBusy(false);
    }
  }
  async function signOut() {
    if (!session) return;
    setBusy(true);
    setError("");
    try {
      await request("/auth/logout", session.access_token, { method: "POST" });
      clearSession();
      setSession(null);
      await wakeSync(null);
      setMessage("");
    } catch {
      setError("Connect to sign out and revoke this session. Captures remain queued.");
    } finally {
      setBusy(false);
    }
  }
  function saved() {
    setMessage("Capture saved on this device.");
    void refresh();
    if (navigator.onLine) void sync();
  }
  const pending = records.filter(unfinished).length,
    conflicts = records.filter((row) => row.status === "REJECTED" || row.evidence_error).length;
  return (
    <main>
      <header className="topbar">
        <a href="/" className="brand" aria-label="Defiant FleetOps">
          <img src="/icons/icon.svg" width="36" height="36" alt="" />
          <span>
            DEFIANT<span className="brand-sub">FLEETOPS</span>
          </span>
        </a>
        <span className={"network " + (online ? "" : "offline")}>
          <i />
          {online ? "Connected" : "Offline"}
        </span>
      </header>
      <div className="workspace">
        <section className="intro">
          <p className="eyebrow">Physical operations · capture</p>
          <h1>Keep things moving.</h1>
          <p>Scan the label. Capture what happened.</p>
        </section>
        <div className="layout">
          <aside className="sidebar">
            <p className="eyebrow">Capture a workflow</p>
            <nav aria-label="Capture workflows">
              {MODES.map((value, index) => (
                <button
                  key={value}
                  className={mode === value ? "active" : ""}
                  disabled={!session}
                  onClick={() => {
                    setMode(value);
                    setMessage("");
                    setError("");
                  }}
                >
                  <span className="nav-number">{String(index + 1).padStart(2, "0")}</span>
                  {value}
                  {value === "Inbox" && conflicts > 0 && <b>{conflicts}</b>}
                </button>
              ))}
            </nav>
            <div className="queue-summary">
              <p className="eyebrow">On this device</p>
              <strong data-testid="pending-count">{pending}</strong>
              <p>captures awaiting sync</p>
              <button className="secondary" disabled={!session || busy} onClick={() => void sync()}>
                Sync now
              </button>
              <small>{offlineReady ? "✓ Offline shell ready" : "Preparing offline shell…"}</small>
            </div>
            {session && (
              <div className="operator">
                <span>
                  Signed in as <strong>{session.username}</strong>
                </span>
                <button className="text-button" disabled={busy} onClick={() => void signOut()}>
                  Sign out
                </button>
              </div>
            )}
          </aside>
          <section className="panel" aria-label={session ? mode + " workflow" : "Sign in"}>
            {!loaded ? (
              <p>Loading capture…</p>
            ) : !session ? (
              <form className="login" onSubmit={(event) => void signIn(event)}>
                <p className="eyebrow">Operator session</p>
                <h2>Sign in to capture</h2>
                <p>Your scans stay on this device until the server accepts them.</p>
                <label>
                  Username
                  <input
                    aria-label="Username"
                    autoComplete="username"
                    value={username}
                    onChange={(event) => setUsername(event.target.value)}
                    required
                  />
                </label>
                <label>
                  Password
                  <input
                    aria-label="Password"
                    type="password"
                    autoComplete="current-password"
                    value={password}
                    onChange={(event) => setPassword(event.target.value)}
                    required
                  />
                </label>
                <button className="primary" disabled={busy}>
                  {busy ? "Signing in…" : "Sign in"}
                </button>
              </form>
            ) : (
              <>
                <div className="panel-heading">
                  <div>
                    <p className="eyebrow">Scan-first capture</p>
                    <h2>{mode}</h2>
                  </div>
                  <span className="pill">
                    {mode === "Inbox" ? conflicts + " to review" : "Ready to scan"}
                  </span>
                </div>
                <div key={scopeOf(session) + mode}>
                  {mode === "Receive" ? (
                    <ReceiveFlow session={session} saved={saved} />
                  ) : mode === "Inbox" ? (
                    <Inbox session={session} records={records} saved={saved} />
                  ) : (
                    <AssetFlow session={session} mode={mode} saved={saved} />
                  )}
                </div>
              </>
            )}
            {message && (
              <p role="status" className="notice">
                {message}
              </p>
            )}
            {error && (
              <p role="alert" className="error">
                {error}
              </p>
            )}
          </section>
        </div>
        <footer>Capture locally. Sync securely. Review conflicts with context.</footer>
      </div>
    </main>
  );
}
