/** Durable claims and deterministic failure schedules; fake IDB exercises real transaction code. */
import "fake-indexeddb/auto";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import { enqueue, rows, resetEpoch, cachePut, cacheGet, closeDatabase, lease } from "../src/lib/db";
import { synchronize } from "../src/lib/sync";
import { cached } from "../src/lib/cached";
import { scopeOf, type Session, type Operation, type OperationResult } from "../src/lib/types";
const session: Session = {
  org_id: crypto.randomUUID(),
  actor_id: crypto.randomUUID(),
  user_id: crypto.randomUUID(),
  access_token: "unit-bearer",
  expires_at: new Date(Date.now() + 3600_000).toISOString(),
  username: "operator",
};
const claim = {
  operation: "MOVE" as const,
  entity_type: "ASSET",
  entity_id: crypto.randomUUID(),
  expected_version: 2,
  payload: { to_location_id: crypto.randomUUID(), reason: "At station" },
};
function answer(
  op: Operation,
  result: Record<string, unknown> = { version: 3 },
  state = "APPLIED",
) {
  return new Response(
    JSON.stringify([
      {
        operation_id: op.operation_id,
        sync_state: state,
        recorded_at: new Date().toISOString(),
        result,
        sequence_flags: [],
      },
    ]),
    { status: 200 },
  );
}
function auth() {
  return new Response(JSON.stringify(session));
}
beforeEach(() => {
  vi.restoreAllMocks();
});
afterEach(async () => {
  vi.unstubAllGlobals();
  await closeDatabase();
  await new Promise<void>((resolve, reject) => {
    const req = indexedDB.deleteDatabase("fleetops-capture-v1");
    req.onsuccess = () => resolve();
    req.onerror = () => reject(req.error);
  });
});
test("concurrent tab allocations persist unique ascending sequences in one epoch", async () => {
  const results = await Promise.all(Array.from({ length: 20 }, () => enqueue(session, claim)));
  const stored = await rows(scopeOf(session));
  expect(stored.map((row) => row.envelope.client_seq)).toEqual(
    Array.from({ length: 20 }, (_, i) => i + 1),
  );
  expect(new Set(results.map((row) => row.envelope.operation_id)).size).toBe(20);
  expect(new Set(stored.map((row) => row.envelope.client_epoch)).size).toBe(1);
});
test("epoch reset preserves older envelopes and capture order despite backwards clocks", async () => {
  const old = await enqueue(session, claim, undefined, "2030-01-01T00:00:00Z");
  await resetEpoch(session);
  const next = await enqueue(session, claim, undefined, "2000-01-01T00:00:00Z");
  expect(next.envelope.client_id).toBe(old.envelope.client_id);
  expect(next.envelope.client_epoch).not.toBe(old.envelope.client_epoch);
  expect(next.envelope.client_seq).toBe(1);
  expect((await rows(scopeOf(session))).map((row) => row.id)).toEqual([old.id, next.id]);
  expect((await rows(scopeOf(session)))[0].envelope).toEqual(old.envelope);
});
test("actor and tenant buffers stay isolated; actor switch rotates only future captures", async () => {
  const a = await enqueue(session, claim);
  const other = { ...session, actor_id: crypto.randomUUID() };
  const b = await enqueue(other, claim);
  await cachePut(scopeOf(session), "/assets/example", { version: 7 });
  expect(await cacheGet(scopeOf(other), "/assets/example")).toBeUndefined();
  expect(await rows(scopeOf(other))).toHaveLength(1);
  expect((await rows(scopeOf(session)))[0].id).toBe(a.id);
  expect(b.envelope.client_epoch).not.toBe(a.envelope.client_epoch);
});
test("expired credentials cannot capture", async () => {
  await expect(enqueue({ ...session, expires_at: "2000-01-01T00:00:00Z" }, claim)).rejects.toThrow(
    "Sign in",
  );
  expect(await rows(scopeOf(session))).toHaveLength(0);
});
test("three disconnected captures replay in ordinal order with their original claims", async () => {
  const originals = await Promise.all([
    enqueue(session, claim),
    enqueue(session, claim),
    enqueue(session, claim),
  ]);
  const seen: Operation[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url, init) => {
      if (String(url).endsWith("/auth/me")) return auth();
      const op = JSON.parse(init.body).operations[0] as Operation;
      seen.push(op);
      return answer(op);
    }),
  );
  await synchronize(session);
  expect(seen).toEqual(originals.map((row) => row.envelope));
  expect((await rows(scopeOf(session))).map((row) => row.status)).toEqual([
    "APPLIED",
    "APPLIED",
    "APPLIED",
  ]);
});
test("lost committed response stops successors then replays the same UUID exactly once", async () => {
  const first = await enqueue(session, claim);
  const second = await enqueue(session, claim);
  const admitted = new Set<string>(),
    seen: string[] = [];
  let lose = true;
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url, init) => {
      if (String(url).endsWith("/auth/me")) return auth();
      const op = JSON.parse(init.body).operations[0];
      seen.push(op.operation_id);
      const duplicate = admitted.has(op.operation_id);
      admitted.add(op.operation_id);
      if (lose) {
        lose = false;
        throw new TypeError("response lost after admission");
      }
      return answer(op, { version: 3 }, duplicate ? "DUPLICATE" : "APPLIED");
    }),
  );
  await expect(synchronize(session)).rejects.toThrow("response lost");
  expect(seen).toEqual([first.id]);
  expect((await rows(scopeOf(session)))[1].status).toBe("QUEUED");
  await synchronize(session);
  expect(seen).toEqual([first.id, first.id, second.id]);
  expect(admitted.size).toBe(2);
  expect((await rows(scopeOf(session)))[0].envelope).toEqual(first.envelope);
});
test("duplicate rejection remains terminal and never rebases the expected version", async () => {
  const original = await enqueue(session, claim);
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url, init) =>
      String(url).endsWith("/auth/me")
        ? auth()
        : answer(
            JSON.parse(init.body).operations[0],
            { code: "SYNC_CONFLICT", expected: { version: 2 }, current: { version: 5 } },
            "DUPLICATE",
          ),
    ),
  );
  await synchronize(session);
  await synchronize(session);
  const row = (await rows(scopeOf(session)))[0];
  expect(row.status).toBe("REJECTED");
  expect(row.envelope).toEqual(original.envelope);
});
test("unavailable or mismatched outcomes retain uncertain captures", async () => {
  const original = await enqueue(session, claim);
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url) =>
      String(url).endsWith("/auth/me")
        ? auth()
        : new Response(
            JSON.stringify([
              {
                operation_id: original.id,
                sync_state: "REJECTED",
                recorded_at: null,
                result: { code: "OPERATION_UNAVAILABLE" },
                sequence_flags: [],
              },
            ]),
          ),
    ),
  );
  await expect(synchronize(session)).rejects.toThrow("uncertain");
  expect((await rows(scopeOf(session)))[0].status).toBe("QUEUED");
});
test("credential mismatch cannot submit another actor's captures", async () => {
  await enqueue(session, claim);
  const fetcher = vi.fn(
    async () => new Response(JSON.stringify({ ...session, actor_id: crypto.randomUUID() })),
  );
  vi.stubGlobal("fetch", fetcher);
  await expect(synchronize(session)).rejects.toThrow("verify");
  expect(fetcher).toHaveBeenCalledTimes(1);
  expect((await rows(scopeOf(session)))[0].status).toBe("QUEUED");
});
test("denied and missing API facts invalidate cache rather than enabling stale offline fallback", async () => {
  const path = "/assets/" + claim.entity_id;
  await cachePut(scopeOf(session), path, { version: 2 });
  vi.stubGlobal(
    "fetch",
    vi.fn(async () => new Response(JSON.stringify({ detail: "Asset not found" }), { status: 404 })),
  );
  await expect(cached(session, path)).rejects.toThrow("not found");
  expect(await cacheGet(scopeOf(session), path)).toBeUndefined();
});
test("network loss can use only this actor's previously loaded facts", async () => {
  const path = "/assets/" + claim.entity_id;
  await cachePut(scopeOf(session), path, { version: 2 });
  vi.stubGlobal(
    "fetch",
    vi.fn(async () => {
      throw new TypeError("offline");
    }),
  );
  expect(await cached(session, path)).toEqual({ version: 2 });
  await expect(cached({ ...session, org_id: crypto.randomUUID() }, path)).rejects.toThrow(
    "Connect once",
  );
});
test("lease serializes workers and crashed ownership expires", async () => {
  vi.spyOn(Date, "now").mockReturnValue(1000);
  expect(await lease(scopeOf(session), "first")).toBe(true);
  expect(await lease(scopeOf(session), "other")).toBe(false);
  vi.spyOn(Date, "now").mockReturnValue(62000);
  expect(await lease(scopeOf(session), "other")).toBe(true);
  expect(await lease(scopeOf(session), "first", true)).toBe(false);
});
test("photos link after earlier captures and survive upload and link response loss", async () => {
  const lineId = crypto.randomUUID(),
    attachment = crypto.randomUUID(),
    seen: Operation[] = [];
  const received = await enqueue(
    session,
    { ...claim, operation: "RECEIVE_SCAN", entity_type: "RECEIPT", expected_version: null },
    {
      blob: new Blob(["photo-bytes"], { type: "image/png" }),
      filename: "photo.png",
      media_type: "image/png",
      captured_at: new Date().toISOString(),
    },
  );
  const later = await enqueue(session, claim);
  let linkLost = true,
    uploads = 0;
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url, init) => {
      if (String(url).endsWith("/auth/me")) return auth();
      if (String(url).includes("/attachments?")) {
        uploads++;
        return new Response(JSON.stringify({ id: attachment }));
      }
      const op = JSON.parse(init.body).operations[0] as Operation;
      seen.push(op);
      if (op.operation === "ATTACH_EVIDENCE" && linkLost) {
        linkLost = false;
        throw new TypeError("link response lost");
      }
      return answer(
        op,
        op.operation === "RECEIVE_SCAN" ? { line_id: lineId } : { id: crypto.randomUUID() },
      );
    }),
  );
  await expect(synchronize(session)).rejects.toThrow("link response lost");
  let row = (await rows(scopeOf(session)))[0];
  expect(row.photo?.blob.size).toBe(11);
  expect(row.attachment_id).toBe(attachment);
  expect(row.evidence_operation?.entity_id).toBe(lineId);
  expect(seen.slice(0, 2).map((op) => op.operation_id)).toEqual([received.id, later.id]);
  await synchronize(session);
  row = (await rows(scopeOf(session)))[0];
  expect(row.photo).toBeUndefined();
  expect(row.evidence_done).toBe(true);
  expect(uploads).toBe(1);
  expect(seen.at(-1)).toEqual(seen.at(-2));
  expect(row.envelope).toEqual(received.envelope);
});
