"use client";
/** A wedge and camera share the same decode boundary. FleetOps labels contain ONLY UUIDs (D14).
 * Manufacturer serials are observations in an explicit receiving step, never identities.
 */
import { useEffect, useRef, useState } from "react";
import { BrowserMultiFormatReader, type IScannerControls } from "@zxing/browser";
import { BarcodeFormat, DecodeHintType } from "@zxing/library";
import { isUuid } from "@/lib/types";
export function Scanner({
  label,
  onScan,
  serial = false,
}: {
  label: string;
  onScan: (value: string) => Promise<void>;
  serial?: boolean;
}) {
  const [value, setValue] = useState(""),
    [camera, setCamera] = useState(false),
    [error, setError] = useState(""),
    [busy, setBusy] = useState(false);
  const video = useRef<HTMLVideoElement>(null),
    locked = useRef(false);
  const scan = useRef(onScan);
  useEffect(() => {
    scan.current = onScan;
  }, [onScan]);
  async function accept(raw: string) {
    if (locked.current) return;
    if (!(serial ? raw.trim().length > 0 && raw.length <= 500 : isUuid(raw))) {
      setError(
        serial
          ? "Scan a readable manufacturer serial"
          : "This label must contain only a FleetOps UUID",
      );
      return;
    }
    locked.current = true;
    setBusy(true);
    setError("");
    try {
      await scan.current(serial ? raw : raw.toLowerCase());
      setValue("");
      setCamera(false);
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : "Scan could not be loaded");
    } finally {
      locked.current = false;
      setBusy(false);
    }
  }
  useEffect(() => {
    if (!camera || !video.current) return;
    let cancelled = false;
    let controls: IScannerControls | undefined;
    const hints = new Map<DecodeHintType, unknown>([
      [DecodeHintType.POSSIBLE_FORMATS, [BarcodeFormat.CODE_128, BarcodeFormat.DATA_MATRIX]],
    ]);
    const reader = new BrowserMultiFormatReader(hints);
    reader
      .decodeFromConstraints(
        { video: { facingMode: { ideal: "environment" } }, audio: false },
        video.current,
        (decoded, _error, current) => {
          if (decoded && !cancelled) {
            current.stop();
            setCamera(false);
            void accept(decoded.getText());
          }
        },
      )
      .then((current) => {
        controls = current;
        if (cancelled) current.stop();
      })
      .catch(() => {
        if (!cancelled) {
          setError("Camera is unavailable. Use a barcode scanner instead.");
          setCamera(false);
        }
      });
    return () => {
      cancelled = true;
      controls?.stop();
    };
    // The active camera owns one attempt; scan.current supplies the latest flow handler.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [camera, serial]);
  return (
    <section className="scan-box">
      <form
        onSubmit={(event) => {
          event.preventDefault();
          void accept(value);
        }}
      >
        <label htmlFor={label}>{label}</label>
        <div className="scan-input">
          <span aria-hidden="true">⌁</span>
          <input
            id={label}
            aria-label={label}
            autoComplete="off"
            autoCapitalize="none"
            spellCheck={false}
            value={value}
            onChange={(event) => setValue(event.target.value)}
            placeholder={serial ? "Scan manufacturer barcode" : "Scan FleetOps label"}
            disabled={busy}
          />
          <button type="submit" disabled={busy || !value} aria-label={"Use " + label}>
            ↵
          </button>
        </div>
      </form>
      <button
        className="secondary"
        type="button"
        onClick={() => setCamera(!camera)}
        disabled={busy}
      >
        {camera ? "Close camera" : "Use camera"}
      </button>
      {camera && (
        <video ref={video} muted playsInline className="camera" aria-label="Barcode camera" />
      )}
      {error && (
        <p role="alert" className="error">
          {error}
        </p>
      )}
    </section>
  );
}
