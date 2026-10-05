/** Replay original claims in capture order. D12 forbids implicit rebase or last-writer-wins. */
import { request, jsonBody, ApiError } from "./api";
import { rows, updateRow, lease, evidenceOperation } from "./db";
import {
  scopeOf,
  isUuid,
  type Session,
  type Operation,
  type OperationResult,
  type QueueRow,
} from "./types";
function terminal(response: unknown, op: Operation): OperationResult {
  if (!Array.isArray(response) || response.length !== 1)
    throw new Error("Sync response could not be verified");
  const value = response[0] as OperationResult;
  if (
    value.operation_id !== op.operation_id ||
    !["APPLIED", "REJECTED", "DUPLICATE"].includes(value.sync_state) ||
    !value.recorded_at ||
    !Number.isFinite(Date.parse(value.recorded_at)) ||
    !value.result ||
    typeof value.result !== "object" ||
    Array.isArray(value.result)
  )
    throw new Error("Sync outcome is uncertain; the original capture is retained");
  return value;
}
function rejected(value: OperationResult): boolean {
  // DUPLICATE carries the ORIGINAL outcome, including a rejected operation's code.
  // A response loss must never turn a previously rejected claim into an applied one.
  return (
    value.sync_state === "REJECTED" ||
    (value.sync_state === "DUPLICATE" && typeof value.result.code === "string")
  );
}
export function unfinished(row: QueueRow) {
  return (
    row.status === "QUEUED" ||
    (row.status === "APPLIED" && !!row.photo && !row.evidence_done && !row.evidence_error)
  );
}
export async function synchronize(session: Session, changed: () => void = () => {}): Promise<void> {
  if (
    !Number.isFinite(Date.parse(session.expires_at)) ||
    Date.parse(session.expires_at) <= Date.now()
  )
    throw new ApiError(401, "Session expired");
  const scope = scopeOf(session),
    owner = crypto.randomUUID();
  if (!(await lease(scope, owner))) return;
  try {
    const identity = await request<{ org_id: string; actor_id: string }>(
      "/auth/me",
      session.access_token,
    );
    if (identity.org_id !== session.org_id || identity.actor_id !== session.actor_id)
      throw new ApiError(401, "Sign in again to verify the capture actor");
    for (let step = 0; step < 1000; step++) {
      if (!(await lease(scope, owner))) return;
      const records = await rows(scope);
      // Link claims get their own durable ordinal after upload. Captures queued
      // mid-upload must flush before the later link; replay restores the same order.
      const work = records
        .flatMap((row) => [
          ...(row.status === "QUEUED"
            ? [{ row, op: row.envelope, ordinal: row.ordinal, evidence: false }]
            : []),
          ...(row.status === "APPLIED" &&
          row.evidence_operation &&
          !row.evidence_done &&
          !row.evidence_error
            ? [{ row, op: row.evidence_operation, ordinal: row.evidence_ordinal!, evidence: true }]
            : []),
        ])
        .sort((a, b) => a.ordinal - b.ordinal)[0];
      const photoRow = records.find(
        (row) =>
          row.status === "APPLIED" &&
          row.photo &&
          !row.evidence_done &&
          !row.evidence_error &&
          !row.evidence_operation,
      );
      const active = work?.row ?? photoRow;
      if (!active) return;
      try {
        if (work) {
          const response = terminal(
            await request<unknown>(
              "/capture/operations",
              session.access_token,
              jsonBody({ operations: [work.op] }),
            ),
            work.op,
          );
          if (work.evidence) {
            if (rejected(response))
              await updateRow(active.id, {
                evidence_error: String(
                  response.result.message ?? response.result.code ?? "Evidence link rejected",
                ),
              });
            else
              await updateRow(active.id, {
                evidence_done: true,
                photo: undefined,
                error: undefined,
              });
          } else
            await updateRow(active.id, {
              status: rejected(response) ? "REJECTED" : "APPLIED",
              response,
              error: undefined,
            });
        } else if (photoRow?.photo) {
          let row = photoRow;
          const lineId = row.response?.result.line_id;
          if (typeof lineId !== "string" || !isUuid(lineId))
            throw new Error("Receipt capture returned no verifiable line for this photo");
          if (!row.attachment_id) {
            const photo = photoRow.photo;
            const query = new URLSearchParams({
              source_type: "PHOTO",
              captured_at: photo.captured_at,
              original_filename: photo.filename,
              media_type: photo.media_type,
            });
            const upload = await request<{ id: string }>(
              "/attachments?" + query,
              session.access_token,
              { method: "POST", headers: { "Content-Type": photo.media_type }, body: photo.blob },
            );
            if (!isUuid(upload.id)) throw new Error("Evidence upload returned no identity");
            row = await updateRow(row.id, { attachment_id: upload.id });
          }
          await evidenceOperation(session, row, lineId);
        }
        changed();
      } catch (error) {
        await updateRow(active.id, {
          error: error instanceof Error ? error.message : "Sync interrupted",
        });
        changed();
        // A lost response may follow a commit. Retain the UUID and stop; later
        // captures cannot overtake an uncertain predecessor or silently rebase it.
        throw error;
      }
    }
  } finally {
    await lease(scope, owner, true);
  }
}
