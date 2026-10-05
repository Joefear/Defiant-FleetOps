"use client";
/** Physical intent retains the last observed GLOBAL version; queued work never predicts it. */
import { useState } from "react";
import { Scanner } from "./scanner";
import { cached } from "@/lib/cached";
import { enqueue } from "@/lib/db";
import {
  type Actor,
  type Json,
  type Asset,
  type Entity,
  type Options,
  type Session,
  isUuid,
} from "@/lib/types";
export function AssetFlow({
  session,
  mode,
  saved,
}: {
  session: Session;
  mode: "Move" | "Assign" | "Transition";
  saved: () => void;
}) {
  const [asset, setAsset] = useState<Asset | null>(null),
    [options, setOptions] = useState<Options | null>(null),
    [people, setPeople] = useState<Actor[]>([]);
  const [destination, setDestination] = useState<Entity | null>(null),
    [person, setPerson] = useState(""),
    [toState, setToState] = useState(""),
    [evidence, setEvidence] = useState("");
  const [reason, setReason] = useState(""),
    [error, setError] = useState(""),
    [busy, setBusy] = useState(false);
  async function scanAsset(id: string) {
    const resolved = await cached<Entity>(session, "/resolve/" + id);
    if (resolved.entity_type !== "ASSET") throw new Error("Scan an Asset label to begin");
    const current = await cached<Asset>(session, "/assets/" + id);
    const choices =
      mode === "Transition"
        ? await cached<Options>(session, "/assets/" + id + "/transition-options")
        : null;
    if (
      choices &&
      (choices.expected_version !== current.version || choices.from_state !== current.current_state)
    )
      throw new Error("Asset changed while loading. Scan it again.");
    const actors = mode === "Assign" ? await cached<Actor[]>(session, "/actors") : [];
    setAsset(current);
    setOptions(choices);
    setPeople(actors.filter((actor) => actor.active && actor.type === "HUMAN"));
    setDestination(null);
    setPerson("");
    setToState("");
    setEvidence("");
    setReason("");
    setError("");
  }
  async function scanLocation(id: string) {
    const value = await cached<Entity>(session, "/resolve/" + id);
    if (value.entity_type !== "LOCATION")
      throw new Error(
        mode === "Move" ? "Scan the destination Location" : "Scan the station Location",
      );
    setDestination(value);
    setPerson("");
  }
  async function capture() {
    if (!asset || !reason.trim()) return;
    setBusy(true);
    setError("");
    try {
      const payload: Record<string, Json> =
        mode === "Move"
          ? { to_location_id: destination!.entity_id, reason }
          : mode === "Assign"
            ? {
                assignee_type: person ? "ACTOR" : "LOCATION",
                assignee_id: person || destination!.entity_id,
                reason,
              }
            : {
                from_state: options!.from_state,
                to_state: toState,
                reason,
                ...(evidence ? { evidence_ref: evidence } : {}),
              };
      await enqueue(session, {
        operation: mode === "Move" ? "MOVE" : mode === "Assign" ? "ASSIGN" : "TRANSITION",
        entity_id: asset.id,
        entity_type: "ASSET",
        expected_version: asset.version,
        payload,
      });
      setAsset(null);
      setDestination(null);
      saved();
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : "Capture was not saved");
    } finally {
      setBusy(false);
    }
  }
  const needsEvidence = options?.options.find(
    (option) => option.to_state === toState,
  )?.requires_evidence;
  const ready =
    !!asset &&
    !!reason.trim() &&
    (mode === "Transition"
      ? !!toState && (!needsEvidence || isUuid(evidence))
      : !!destination || !!person);
  return (
    <div className="flow">
      <p className="eyebrow">01 · identify the unit</p>
      <Scanner label="Asset label" onScan={scanAsset} />
      {asset && (
        <>
          <div className="fact-card">
            <span className="pill">{asset.current_state.replaceAll("_", " ")}</span>
            <h3>{asset.asset_tag}</h3>
            <p>{asset.description}</p>
            <small>Observed version {asset.version}</small>
          </div>
          <p className="eyebrow">
            02 ·{" "}
            {mode === "Move"
              ? "scan the destination"
              : mode === "Assign"
                ? "choose who or where"
                : "choose the next state"}
          </p>
          {mode !== "Transition" && (
            <Scanner
              label={mode === "Move" ? "Destination label" : "Station label"}
              onScan={scanLocation}
            />
          )}
          {destination && (
            <p className="selection">
              ✓ {String(destination.summary.name ?? destination.entity_id)}
            </p>
          )}
          {mode === "Assign" && (
            <label>
              Or select a person
              <select
                aria-label="Person"
                value={person}
                onChange={(event) => {
                  setPerson(event.target.value);
                  setDestination(null);
                }}
              >
                <option value="">Choose person</option>
                {people.map((actor) => (
                  <option key={actor.id} value={actor.id}>
                    {actor.display_name}
                  </option>
                ))}
              </select>
            </label>
          )}
          {mode === "Transition" && (
            <>
              <label>
                Next state
                <select
                  aria-label="Next state"
                  value={toState}
                  onChange={(event) => setToState(event.target.value)}
                >
                  <option value="">Choose a state</option>
                  {options?.options.map((option) => (
                    <option key={option.to_state} value={option.to_state}>
                      {option.to_state.replaceAll("_", " ")}
                    </option>
                  ))}
                </select>
              </label>
              {options?.options.length === 0 && <p>This unit has no legal next state.</p>}
              {needsEvidence && (
                <>
                  <Scanner
                    label="Disposal evidence label"
                    onScan={async (id) => {
                      const entity = await cached<Entity>(session, "/resolve/" + id);
                      if (entity.entity_type !== "ATTACHMENT")
                        throw new Error("Scan an evidence attachment label");
                      setEvidence(id);
                    }}
                  />
                  <p>
                    {evidence
                      ? "✓ Evidence selected"
                      : "Select disposal evidence already linked to this Asset."}
                  </p>
                </>
              )}
            </>
          )}
          <label>
            Reason
            <textarea
              aria-label="Reason"
              value={reason}
              onChange={(event) => setReason(event.target.value)}
              maxLength={4000}
            />
          </label>
          <button className="primary" disabled={!ready || busy} onClick={() => void capture()}>
            Save {mode.toLowerCase()} capture
          </button>
        </>
      )}
      {error && (
        <p role="alert" className="error">
          {error}
        </p>
      )}
    </div>
  );
}
