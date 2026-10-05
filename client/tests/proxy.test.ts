/** Proxy regression checks complement live-browser authority/origin acceptance. */
import { afterEach, expect, test, vi } from "vitest";
import type { NextRequest } from "next/server";
import { GET, POST } from "../src/app/api/[...path]/route";
function req(method = "GET", origin?: string, body?: BodyInit) {
  const value = new Request("http://localhost:3100/api/auth/login", {
    method,
    headers: {
      Host: "127.0.0.1:3100",
      ...(origin ? { Origin: origin } : {}),
      Authorization: "Bearer scoped-token",
      Cookie: "unrelated=private",
      "Content-Type": "application/json",
    },
    ...(body ? { body } : {}),
  });
  return Object.assign(value, { nextUrl: new URL(value.url) }) as unknown as NextRequest;
}
const context = { params: Promise.resolve({ path: ["auth", "login"] }) };
afterEach(() => {
  vi.unstubAllGlobals();
  vi.unstubAllEnvs();
});
test("loopback-normalized URL still accepts the browser's public authority", async () => {
  vi.stubEnv("FLEETOPS_API_URL", "http://127.0.0.1:4100");
  vi.stubEnv("FLEETOPS_PUBLIC_ORIGIN", undefined);
  const fetcher = vi.fn(async () => Response.json({ access_token: "issued" }));
  vi.stubGlobal("fetch", fetcher);
  const response = await POST(req("POST", "http://127.0.0.1:3100", "{}"), context);
  expect(response.status).toBe(200);
  expect(fetcher).toHaveBeenCalledTimes(1);
  const [target, options] = fetcher.mock.calls[0] as unknown as [URL, RequestInit];
  expect(target.href).toBe("http://127.0.0.1:4100/auth/login");
  expect(new Headers(options.headers).get("Authorization")).toBe("Bearer scoped-token");
  expect(new Headers(options.headers).get("Cookie")).toBeNull();
  expect(options.cache).toBe("no-store");
  expect(options.redirect).toBe("error");
  expect(response.headers.get("Cache-Control")).toBe("no-store");
});
test.each([undefined, "null", "https://other.invalid", "http://localhost:3100"])(
  "unmatched Origin %s never reaches upstream",
  async (origin) => {
    vi.stubEnv("FLEETOPS_PUBLIC_ORIGIN", undefined);
    const fetcher = vi.fn();
    vi.stubGlobal("fetch", fetcher);
    expect((await POST(req("POST", origin, "{}"), context)).status).toBe(403);
    expect(fetcher).not.toHaveBeenCalled();
  },
);
test("trusted HTTPS public origin supports a reverse proxy without client-selected upstream", async () => {
  vi.stubEnv("FLEETOPS_PUBLIC_ORIGIN", "https://capture.example.test");
  vi.stubEnv("FLEETOPS_API_URL", "http://127.0.0.1:4100/fixed");
  const fetcher = vi.fn(async () => new Response(null, { status: 204 }));
  vi.stubGlobal("fetch", fetcher);
  expect((await POST(req("POST", "https://capture.example.test", "{}"), context)).status).toBe(204);
  expect((fetcher.mock.calls[0] as unknown as [URL])[0].href).toBe(
    "http://127.0.0.1:4100/fixed/auth/login",
  );
});
test("path traversal cannot choose a destination", async () => {
  const fetcher = vi.fn();
  vi.stubGlobal("fetch", fetcher);
  expect((await GET(req(), { params: Promise.resolve({ path: ["..", "host"] }) })).status).toBe(
    400,
  );
  expect(fetcher).not.toHaveBeenCalled();
});
test("upstream response loss is a bounded no-store failure", async () => {
  vi.stubEnv("FLEETOPS_API_URL", "http://127.0.0.1:4100");
  vi.stubGlobal(
    "fetch",
    vi.fn(async () => {
      throw new TypeError("internal transport details");
    }),
  );
  const response = await GET(req(), context);
  expect(response.status).toBe(503);
  expect(await response.json()).toEqual({ detail: "FleetOps API is unreachable" });
});
test("oversize raw evidence body is rejected before upstream admission", async () => {
  vi.stubEnv("FLEETOPS_PUBLIC_ORIGIN", undefined);
  const fetcher = vi.fn();
  vi.stubGlobal("fetch", fetcher);
  const body = new Uint8Array(17 * 1024 * 1024 + 1);
  expect((await POST(req("POST", "http://127.0.0.1:3100", body), context)).status).toBe(413);
  expect(fetcher).not.toHaveBeenCalled();
});
