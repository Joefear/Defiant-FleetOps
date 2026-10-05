"use client";
/** Rejected captures are terminal observations; human resolution does not reapply their intent. */
import { useState } from "react";
import { enqueue } from "@/lib/db";
import { request } from "@/lib/api";
import { type Session, type QueueRow } from "@/lib/types";
export function Inbox({
  session,
  records,
  saved,
}: {
  session: Session;
  records: QueueRow[];
  saved: () => void;
}) {
  const [error, setError] = useState(""),
    [note, setNote] = useState<Record<string, string>>({}),
    [busy, setBusy] = useState(false);
  async function resolve(row: QueueRow) {
    const id = row.response?.result.exception_id;
    if (typeof id !== "string" || !note[row.id]?.trim()) return;
    setBusy(true);
    setError("");
    try {
      const current = await request<{ status: "OPEN" | "ACKNOWLEDGED" | "RESOLVED" | "WAIVED" }>(
        "/exceptions/" + id,
        session.access_token,
      );
      if (!["OPEN", "ACKNOWLEDGED"].includes(current.status))
        throw new Error("This exception is already closed");
      await enqueue(session, {
        operation: "RESOLVE",
        entity_type: "EXCEPTION",
        entity_id: id,
        expected_version: null,
        payload: { expected_status: current.status, note: note[row.id] },
      });
      saved();
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : "Resolution was not saved");
    } finally {
      setBusy(false);
    }
  }
  const rejected = records.filter((row) => row.status === "REJECTED" || row.evidence_error);
  return (
    <div className="flow">
      <p className="eyebrow">Review captured intent</p>
      {!rejected.length && (
        <div className="empty">
          <h3>Inbox clear</h3>
          <p>Sync rejections appear here with the facts needed to review them.</p>
        </div>
      )}
      {rejected.map((row) => (
        <article className="conflict" key={row.id}>
          <span className="pill danger">
            {row.evidence_error
              ? "Photo link rejected"
              : String(row.response?.result.code ?? "Rejected")}
          </span>
          <h3>{row.envelope.operation.replaceAll("_", " ")}</h3>
          <p className="mono">{row.envelope.entity_id}</p>
          <div className="comparison">
            <section>
              <h4>Captured expectation</h4>
              <p>Version {row.envelope.expected_version ?? "—"}</p>
              <pre>
                {JSON.stringify(row.response?.result.expected ?? row.envelope.payload, null, 2)}
              </pre>
            </section>
            <section>
              <h4>Server facts</h4>
              <pre>
                {JSON.stringify(
                  row.response?.result.current ?? {
                    outcome: row.evidence_error ?? row.response?.result.code,
                  },
                  null,
                  2,
                )}
              </pre>
            </section>
          </div>
          <p>
            Review the unit before taking another action. To capture a new attempt, scan it again.
          </p>
          {typeof row.response?.result.exception_id === "string" && (
            <>
              <label>
                Resolution note
                <textarea
                  aria-label={"Resolution note " + row.id}
                  value={note[row.id] ?? ""}
                  onChange={(event) => setNote({ ...note, [row.id]: event.target.value })}
                  maxLength={4000}
                />
              </label>
              <button
                className="secondary"
                disabled={
                  busy ||
                  !note[row.id]?.trim() ||
                  records.some(
                    (value) =>
                      value.envelope.operation === "RESOLVE" &&
                      value.envelope.entity_id === row.response?.result.exception_id &&
                      value.status !== "REJECTED",
                  )
                }
                onClick={() => void resolve(row)}
              >
                Record human resolution
              </button>
            </>
          )}
          {row.photo && <small>The receipt photo remains on this device.</small>}
        </article>
      ))}
      {error && (
        <p role="alert" className="error">
          {error}
        </p>
      )}
    </div>
  );
}
