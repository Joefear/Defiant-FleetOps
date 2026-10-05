/** Fixed upstream adapter: browser data never chooses a destination or supplies tenant authority. */
import type { NextRequest } from "next/server";

const MAX_BODY = 17 * 1024 * 1024;
export const dynamic = "force-dynamic";

async function forward(request: NextRequest, context: { params: Promise<{ path: string[] }> }) {
  const { path } = await context.params;
  if (path.some((segment) => !/^[a-zA-Z0-9_-]+$/.test(segment))) {
    return Response.json({ detail: "Invalid API path" }, { status: 400 });
  }
  // Next.js may normalize loopback names in nextUrl. Compare the browser's public
  // origin to the HTTP authority; a trusted reverse proxy can pin its HTTPS origin.
  let publicOrigin: string;
  try {
    publicOrigin = new URL(
      process.env.FLEETOPS_PUBLIC_ORIGIN ??
        request.nextUrl.protocol + "//" + request.headers.get("host"),
    ).origin;
  } catch {
    return Response.json({ detail: "Public origin configuration is invalid" }, { status: 503 });
  }
  if (request.method !== "GET" && request.headers.get("origin") !== publicOrigin) {
    return Response.json({ detail: "Cross-origin writes are not accepted" }, { status: 403 });
  }
  const base = new URL(process.env.FLEETOPS_API_URL ?? "http://127.0.0.1:8000");
  if (
    !["http:", "https:"].includes(base.protocol) ||
    base.username ||
    base.password ||
    base.search ||
    base.hash
  ) {
    return Response.json({ detail: "API configuration is invalid" }, { status: 503 });
  }
  // Keep the host administrator-owned even if a caller supplies path/query text.
  const target = new URL(base);
  target.pathname = base.pathname.replace(/\/$/, "") + "/" + path.map(encodeURIComponent).join("/");
  target.search = request.nextUrl.search;
  const headers = new Headers();
  for (const name of ["Authorization", "Content-Type"]) {
    const value = request.headers.get(name);
    if (value) headers.set(name, value);
  }
  let body: ArrayBuffer | undefined;
  if (request.method !== "GET") {
    const chunks: Uint8Array[] = [];
    const reader = request.body?.getReader();
    let size = 0;
    if (reader) {
      while (true) {
        const next = await reader.read();
        if (next.done) break;
        size += next.value.length;
        if (size > MAX_BODY) {
          await reader.cancel();
          return Response.json({ detail: "Upload is too large" }, { status: 413 });
        }
        chunks.push(next.value);
      }
    }
    const bytes = new Uint8Array(size);
    let offset = 0;
    for (const chunk of chunks) {
      bytes.set(chunk, offset);
      offset += chunk.length;
    }
    body = bytes.buffer;
  }
  try {
    const upstream = await fetch(target, {
      method: request.method,
      headers,
      body,
      cache: "no-store",
      redirect: "error",
      signal: AbortSignal.timeout(30_000),
    });
    return new Response(upstream.body, {
      status: upstream.status,
      headers: {
        "Content-Type": upstream.headers.get("Content-Type") ?? "application/json",
        "Cache-Control": "no-store",
        "X-Content-Type-Options": "nosniff",
      },
    });
  } catch {
    // A lost response can follow a committed operation. The queue retains its UUID
    // and replays it; a gateway failure is never proof that nothing happened.
    return Response.json({ detail: "FleetOps API is unreachable" }, { status: 503 });
  }
}
export const GET = forward;
export const POST = forward;
