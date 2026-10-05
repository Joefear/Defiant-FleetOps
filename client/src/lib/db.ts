/** Durable capture claims (D11/D12): IDB allocation and insertion share one transaction.
 * Ordinals order across epoch resets; occurrence clocks never order a replay.
 * Browser storage is an operator buffer, not an authorization or history boundary.
 */
import {
  scopeOf,
  type Session,
  type Scope,
  type Operation,
  type QueueRow,
  type Photo,
} from "./types";
const NAME = "fleetops-capture-v1";
let connection: Promise<IDBDatabase> | undefined;
type Meta = {
  key: string;
  client: string;
  epoch: string;
  scope: Scope;
  seq: number;
  ordinal: number;
};
function result<T>(request: IDBRequest<T>): Promise<T> {
  return new Promise((resolve, reject) => {
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error);
  });
}
function done(tx: IDBTransaction): Promise<void> {
  return new Promise((resolve, reject) => {
    tx.oncomplete = () => resolve();
    tx.onabort = () => reject(tx.error ?? new Error("Local capture could not be saved"));
    tx.onerror = () => reject(tx.error);
  });
}
export function database(): Promise<IDBDatabase> {
  if (!connection)
    connection = new Promise((resolve, reject) => {
      const open = indexedDB.open(NAME, 1);
      open.onupgradeneeded = () => {
        open.result.createObjectStore("meta", { keyPath: "key" });
        const queue = open.result.createObjectStore("queue", { keyPath: "id" });
        queue.createIndex("scope", "scope");
        open.result.createObjectStore("cache", { keyPath: "key" });
      };
      open.onsuccess = () => {
        open.result.onversionchange = () => {
          open.result.close();
          connection = undefined;
        };
        resolve(open.result);
      };
      open.onerror = () => {
        connection = undefined;
        reject(open.error);
      };
      open.onblocked = () =>
        reject(new Error("Close other FleetOps tabs to upgrade local storage"));
    });
  return connection;
}
export async function closeDatabase() {
  if (connection) (await connection).close();
  connection = undefined;
}
export type Claim = Pick<
  Operation,
  "operation" | "entity_id" | "entity_type" | "expected_version" | "payload"
>;
async function allocate(store: IDBObjectStore, scope: Scope, reset = false): Promise<Meta> {
  const saved = await result<Meta | undefined>(store.get("device"));
  const meta = saved ?? {
    key: "device",
    client: crypto.randomUUID(),
    epoch: crypto.randomUUID(),
    scope,
    seq: 0,
    ordinal: 0,
  };
  if (reset || meta.scope !== scope) {
    meta.epoch = crypto.randomUUID();
    meta.seq = 0;
    meta.scope = scope;
  }
  if (meta.seq >= 2147483647) {
    meta.epoch = crypto.randomUUID();
    meta.seq = 0;
  }
  meta.seq++;
  meta.ordinal++;
  store.put(meta);
  return meta;
}
function envelope(meta: Meta, session: Session, claim: Claim, occurred: string): Operation {
  return {
    ...claim,
    operation_id: crypto.randomUUID(),
    actor_id: session.actor_id,
    client_id: meta.client,
    client_epoch: meta.epoch,
    client_seq: meta.seq,
    occurred_at: occurred,
  };
}
export async function enqueue(
  session: Session,
  claim: Claim,
  photo?: Photo,
  occurred = new Date().toISOString(),
): Promise<QueueRow> {
  if (
    !Number.isFinite(Date.parse(session.expires_at)) ||
    Date.parse(session.expires_at) <= Date.now()
  )
    throw new Error("Sign in again before capturing");
  const scope = scopeOf(session),
    db = await database(),
    tx = db.transaction(["meta", "queue"], "readwrite"),
    complete = done(tx);
  const meta = await allocate(tx.objectStore("meta"), scope);
  const op = envelope(meta, session, claim, occurred);
  const row: QueueRow = {
    id: op.operation_id,
    scope,
    ordinal: meta.ordinal,
    envelope: op,
    status: "QUEUED",
    ...(photo ? { photo } : {}),
  };
  tx.objectStore("queue").add(row);
  await complete;
  return row;
}
export async function resetEpoch(session: Session): Promise<void> {
  // Reset changes only FUTURE captures. Already captured UUIDs, epochs, sequences and
  // expected versions remain immutable, even when the operator changes credentials.
  const db = await database(),
    tx = db.transaction("meta", "readwrite"),
    complete = done(tx);
  const store = tx.objectStore("meta");
  const saved = await result<Meta | undefined>(store.get("device"));
  if (saved) store.put({ ...saved, epoch: crypto.randomUUID(), seq: 0, scope: scopeOf(session) });
  await complete;
}
export async function rows(scope: Scope): Promise<QueueRow[]> {
  const db = await database();
  const values = await result<QueueRow[]>(
    db.transaction("queue").objectStore("queue").index("scope").getAll(scope),
  );
  return values.sort((a, b) => a.ordinal - b.ordinal);
}
export async function updateRow(
  id: string,
  changes: Partial<Omit<QueueRow, "id" | "scope" | "ordinal" | "envelope">>,
): Promise<QueueRow> {
  const db = await database(),
    tx = db.transaction("queue", "readwrite"),
    complete = done(tx),
    store = tx.objectStore("queue");
  const old = await result<QueueRow | undefined>(store.get(id));
  if (!old) throw new Error("Capture record is missing");
  const row = { ...old, ...changes };
  store.put(row);
  await complete;
  return row;
}
export async function evidenceOperation(
  session: Session,
  row: QueueRow,
  lineId: string,
): Promise<Operation> {
  const db = await database(),
    tx = db.transaction(["meta", "queue"], "readwrite"),
    complete = done(tx),
    store = tx.objectStore("queue");
  const saved = await result<QueueRow | undefined>(store.get(row.id));
  if (!saved || saved.scope !== scopeOf(session) || !saved.attachment_id) {
    tx.abort();
    await complete.catch(() => {});
    throw new Error("Evidence capture has no matching actor or upload");
  }
  if (saved.evidence_operation) {
    await complete;
    return saved.evidence_operation;
  }
  const meta = await allocate(tx.objectStore("meta"), saved.scope);
  const op = envelope(
    meta,
    session,
    {
      operation: "ATTACH_EVIDENCE",
      entity_id: lineId,
      entity_type: "RECEIPT_LINE",
      expected_version: null,
      payload: { attachment_id: saved.attachment_id, link_role: "RECEIVING_EVIDENCE" },
    },
    saved.photo!.captured_at,
  );
  store.put({ ...saved, evidence_operation: op, evidence_ordinal: meta.ordinal });
  await complete;
  return op;
}
export async function cachePut(scope: Scope, path: string, value: unknown) {
  const db = await database(),
    tx = db.transaction("cache", "readwrite"),
    complete = done(tx);
  tx.objectStore("cache").put({ key: scope + "|" + path, value });
  await complete;
}
export async function cacheGet<T>(scope: Scope, path: string): Promise<T | undefined> {
  const db = await database();
  const row = await result<{ value: T } | undefined>(
    db
      .transaction("cache")
      .objectStore("cache")
      .get(scope + "|" + path),
  );
  return row?.value;
}
export async function cacheDelete(scope: Scope, path: string) {
  const db = await database(),
    tx = db.transaction("cache", "readwrite"),
    complete = done(tx);
  tx.objectStore("cache").delete(scope + "|" + path);
  await complete;
}
export async function lease(scope: Scope, owner: string, release = false): Promise<boolean> {
  // A crashed page/worker cannot strand a SENDING flag. Renewable leases coordinate
  // tabs, while the server's stable operation UUID is the final replay safety net.
  const db = await database(),
    tx = db.transaction("meta", "readwrite"),
    complete = done(tx),
    store = tx.objectStore("meta"),
    key = "lease|" + scope;
  const old = await result<{ owner: string; expires: number } | undefined>(store.get(key));
  const allowed = !old || old.owner === owner || old.expires < Date.now();
  if (allowed) {
    if (release) store.delete(key);
    else store.put({ key, owner, expires: Date.now() + 60_000 });
  }
  await complete;
  return allowed;
}
