/** Browser storage denial must fail closed without crashing startup or claiming sign-in success. */
import { afterEach, expect, test, vi } from "vitest";
import { clearSession, loadSession, login, saveSession } from "../src/lib/session";
import type { Session } from "../src/lib/types";

const session: Session = {
  access_token: "test-session-token",
  expires_at: "2100-01-01T00:00:00Z",
  username: "operator",
  org_id: "22222222-2222-4222-8222-222222222222",
  user_id: "33333333-3333-4333-8333-333333333333",
  actor_id: "44444444-4444-4444-8444-444444444444",
};

afterEach(() => vi.unstubAllGlobals());

test("blocked storage returns signed-out state even when cleanup is also denied", () => {
  const denied = () => {
    throw new DOMException("Storage access blocked", "SecurityError");
  };
  vi.stubGlobal("sessionStorage", { getItem: denied, removeItem: denied });
  expect(loadSession()).toBeNull();
});

test("logout cleanup remains safe when the browser refuses removal", () => {
  vi.stubGlobal("sessionStorage", {
    removeItem() {
      throw new DOMException("Storage access blocked", "SecurityError");
    },
  });
  expect(() => clearSession()).not.toThrow();
});

test("invalid stored credentials are removed and return signed-out state", () => {
  const removeItem = vi.fn();
  vi.stubGlobal("sessionStorage", { getItem: () => "{invalid json", removeItem });
  expect(loadSession()).toBeNull();
  expect(removeItem).toHaveBeenCalledTimes(1);
});

test("a valid tab session survives storage round trip", () => {
  let value: string | null = null;
  vi.stubGlobal("sessionStorage", {
    setItem: (_key: string, next: string) => {
      value = next;
    },
    getItem: () => value,
    removeItem: () => {
      value = null;
    },
  });
  saveSession(session);
  expect(loadSession()).toEqual(session);
});

test.each(["SecurityError", "QuotaExceededError"])(
  "login refuses success when tab persistence raises %s",
  async (name) => {
    vi.stubGlobal("sessionStorage", {
      setItem() {
        throw new DOMException("Storage refused", name);
      },
    });
    const fetcher = vi
      .fn()
      .mockResolvedValueOnce(
        Response.json({
          access_token: session.access_token,
          expires_at: session.expires_at,
        }),
      )
      .mockResolvedValueOnce(
        Response.json({
          org_id: session.org_id,
          user_id: session.user_id,
          actor_id: session.actor_id,
        }),
      );
    vi.stubGlobal("fetch", fetcher);
    await expect(login("operator", "test-password")).rejects.toThrow(
      "Browser storage is unavailable. Allow storage for this site, then sign in again.",
    );
    expect(fetcher).toHaveBeenCalledTimes(2);
  },
);
