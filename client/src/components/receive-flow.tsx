"use client";
/** Receiving records physical observations against an explicitly acknowledged PO source.
 * Opening/finishing a receipt needs the server; observations and photos can queue offline.
 */
import { useState } from "react";
import { Scanner } from "./scanner";
import { request, jsonBody } from "@/lib/api";
import { cached } from "@/lib/cached";
import { cachePut, cacheGet, enqueue, rows } from "@/lib/db";
import {
  scopeOf,
  type Session,
  type Entity,
  type Item,
  type Order,
  type OrderLine,
  type Comparator,
  type Receipt,
  type Party,
  type Photo,
  type Json,
} from "@/lib/types";
type Context = { receipt: Receipt; comparators: Comparator[] };
export function ReceiveFlow({ session, saved }: { session: Session; saved: () => void }) {
  const [order, setOrder] = useState<Order | null>(null),
    [comparators, setComparators] = useState<Comparator[]>([]),
    [receipt, setReceipt] = useState<Receipt | null>(null);
  const [dock, setDock] = useState<Entity | null>(null),
    [parties, setParties] = useState<Party[]>([]),
    [owner, setOwner] = useState("");
  const [item, setItem] = useState<Item | null>(null),
    [comparator, setComparator] = useState(""),
    [serial, setSerial] = useState(""),
    [unreadable, setUnreadable] = useState(false),
    [unreadableReason, setUnreadableReason] = useState("");
  const [quantity, setQuantity] = useState("1"),
    [condition, setCondition] = useState(""),
    [photo, setPhoto] = useState<Photo | undefined>();
  const [message, setMessage] = useState(""),
    [error, setError] = useState(""),
    [busy, setBusy] = useState(false),
    [openingUnknown, setOpeningUnknown] = useState(false);
  async function orderComparators(
    poId: string,
    bound?: Receipt["comparator_bindings"],
  ): Promise<Comparator[]> {
    const lines = await cached<OrderLine[]>(session, "/purchase-orders/" + poId + "/lines");
    const selected = bound
      ? lines.filter((line) => bound.some((pin) => pin.po_line_id === line.id))
      : lines.filter((line) => line.active && !line.superseded);
    return Promise.all(
      selected.map(async (line) => {
        const pin = bound?.find((value) => value.po_line_id === line.id);
        if (pin) {
          let source = line;
          if (pin.source_id) {
            const history = await cached<(OrderLine & { correction_generation: number })[]>(
              session,
              "/purchase-orders/" + poId + "/lines/" + line.id + "/corrections",
            );
            const original = history.find(
              (entry) =>
                entry.id === pin.source_id && entry.correction_generation === pin.source_generation,
            );
            if (!original) throw new Error("Receipt expectation source could not be verified");
            source = original;
          }
          return {
            ...source,
            id: line.id,
            line_number: line.line_number,
            correction_generation: pin.source_generation,
            expected_item_id: source.item_id,
          };
        }
        const effective = await cached<{ effective: OrderLine; correction_generation: number }>(
          session,
          "/purchase-orders/" + poId + "/lines/" + line.id + "/effective",
        );
        return {
          ...effective.effective,
          id: line.id,
          line_number: line.line_number,
          correction_generation: effective.correction_generation,
          expected_item_id: effective.effective.item_id,
        };
      }),
    );
  }
  async function scanHeader(id: string) {
    const entity = await cached<Entity>(session, "/resolve/" + id);
    if (!["PURCHASE_ORDER", "RECEIPT"].includes(entity.entity_type))
      throw new Error("Begin with a PO or receipt label");
    const choices = await cached<Party[]>(session, "/parties");
    if (entity.entity_type === "PURCHASE_ORDER") {
      const po = await cached<Order>(session, "/purchase-orders/" + id);
      if (po.status !== "ISSUED") throw new Error("This purchase order is not issued");
      const next = await orderComparators(id);
      setOrder(po);
      setComparators(next);
      setReceipt(null);
      setDock(null);
    } else {
      const current = await cached<Receipt>(session, "/receipts/" + id);
      if (current.reconciled) throw new Error("This receipt is already finished");
      const remembered = await cacheGet<Context>(scopeOf(session), "capture/receipt/" + id);
      const next =
        remembered?.comparators ??
        (current.po_id ? await orderComparators(current.po_id, current.comparator_bindings) : []);
      setOrder(null);
      setReceipt(current);
      setComparators(next);
      await cachePut(scopeOf(session), "capture/receipt/" + id, {
        receipt: current,
        comparators: next,
      });
    }
    setParties(choices);
    setItem(null);
    setOwner("");
    setMessage("");
    setError("");
    setOpeningUnknown(false);
  }
  async function openReceipt() {
    if (!order || !dock) return;
    setBusy(true);
    setError("");
    try {
      const current = await request<Receipt>(
        "/receipts",
        session.access_token,
        jsonBody({
          vendor_party_id: order.vendor_party_id,
          po_id: order.id,
          dock_location_id: dock.entity_id,
          received_at: new Date().toISOString(),
          reconcile: false,
          comparator_bindings: comparators.map((line) => ({
            po_line_id: line.id,
            expected_generation: line.correction_generation,
          })),
        }),
      );
      await cachePut(scopeOf(session), "/receipts/" + current.id, current);
      await cachePut(scopeOf(session), "capture/receipt/" + current.id, {
        receipt: current,
        comparators,
      });
      setReceipt(current);
      setOrder(null);
    } catch (failure) {
      setOpeningUnknown(true);
      setError(
        (failure instanceof Error ? failure.message : "Receipt could not be opened") +
          ". Verify the receipt outcome before opening another; scan its label to continue.",
      );
    } finally {
      setBusy(false);
    }
  }
  async function scanItem(id: string) {
    const resolved = await cached<Entity>(session, "/resolve/" + id);
    if (resolved.entity_type !== "ITEM") throw new Error("Scan the catalog Item label");
    const current = await cached<Item>(session, "/items/" + id);
    setItem(current);
    setComparator(comparators.find((line) => line.expected_item_id === id)?.id ?? "");
    setSerial("");
    setUnreadable(false);
    setUnreadableReason("");
    setQuantity("1");
    setCondition("");
    setPhoto(undefined);
    setMessage("");
  }
  async function capture() {
    if (!receipt || !item) return;
    setBusy(true);
    setError("");
    try {
      const expected = comparators.find((line) => line.id === comparator);
      const line: Record<string, Json> = {
        item_id: item.id,
        po_line_id: comparator || null,
        expected_po_generation: expected?.correction_generation ?? 0,
        quantity,
        uom: item.uom,
        condition,
      };
      if (item.serialized)
        line.unit = {
          owner_party_id: owner,
          asset_tag: "UNIT-" + crypto.randomUUID(),
          description: item.description,
          identifier: {
            type: "MANUFACTURER_SERIAL",
            ...(unreadable ? { unreadable_reason: unreadableReason } : { value: serial }),
          },
        };
      await enqueue(
        session,
        {
          operation: "RECEIVE_SCAN",
          entity_type: "RECEIPT",
          entity_id: receipt.id,
          expected_version: null,
          payload: { line, reconcile: false },
        },
        photo,
      );
      setItem(null);
      setPhoto(undefined);
      setMessage("Unit captured. Scan the next Item.");
      saved();
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : "Unit was not saved");
    } finally {
      setBusy(false);
    }
  }
  async function finish() {
    if (!receipt) return;
    setBusy(true);
    setError("");
    try {
      const captures = (await rows(scopeOf(session))).filter(
        (row) => row.envelope.entity_id === receipt.id && row.envelope.operation === "RECEIVE_SCAN",
      );
      if (captures.some((row) => row.status !== "APPLIED" || (row.photo && !row.evidence_done)))
        throw new Error("Sync all units and photos, and review rejected captures before finishing");
      const current = await request<Receipt>(
        "/receipts/" + receipt.id + "/reconcile",
        session.access_token,
        jsonBody({}),
      );
      setReceipt(null);
      setItem(null);
      setMessage(
        "Receipt finished. " +
          current.lines.length +
          " lines · " +
          current.exceptions.length +
          " exceptions recorded.",
      );
      saved();
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : "Receipt could not be finished");
    } finally {
      setBusy(false);
    }
  }
  const ready =
    !!item &&
    !!condition &&
    Number(quantity) > 0 &&
    (!item.serialized ||
      (!!owner && quantity === "1" && (unreadable ? !!unreadableReason.trim() : !!serial)));
  return (
    <div className="flow">
      <p className="eyebrow">01 · start the delivery</p>
      <Scanner label="PO or receipt label" onScan={scanHeader} />
      {order && (
        <>
          <div className="fact-card">
            <h3>{order.po_number}</h3>
            <p>{comparators.length} expected item lines · explicitly acknowledged when opened</p>
          </div>
          <Scanner
            label="Receiving dock label"
            onScan={async (id) => {
              const value = await cached<Entity>(session, "/resolve/" + id);
              if (value.entity_type !== "LOCATION") throw new Error("Scan a dock Location label");
              setDock(value);
            }}
          />
          {dock && <p className="selection">✓ {String(dock.summary.name ?? dock.entity_id)}</p>}
          <button
            className="primary"
            disabled={!dock || busy || openingUnknown}
            onClick={() => void openReceipt()}
          >
            Open receipt
          </button>
        </>
      )}
      {receipt && (
        <>
          <div className="fact-card">
            <span className="pill">Receipt open</span>
            <h3>Capture this delivery</h3>
            <small>{receipt.id}</small>
          </div>
          <label>
            Owner of received units
            <select
              aria-label="Owner of received units"
              value={owner}
              onChange={(event) => setOwner(event.target.value)}
            >
              <option value="">Choose the owner explicitly</option>
              {parties.map((party) => (
                <option key={party.id} value={party.id}>
                  {party.display_name}
                </option>
              ))}
            </select>
          </label>
          <p className="eyebrow">02 · scan the actual Item</p>
          <Scanner label="Item label" onScan={scanItem} />
          {item && (
            <>
              <div className="fact-card">
                <h3>{item.description}</h3>
                <p>{item.serialized ? "One serialized unit" : "Quantity observation"}</p>
              </div>
              <label>
                Expected PO line
                <select
                  aria-label="Expected PO line"
                  value={comparator}
                  onChange={(event) => setComparator(event.target.value)}
                >
                  <option value="">Unexpected Item · no PO line</option>
                  {comparators.map((line) => (
                    <option key={line.id} value={line.id}>
                      Line {line.line_number} · {line.quantity} {line.uom} · generation{" "}
                      {line.correction_generation}
                    </option>
                  ))}
                </select>
              </label>
              {item.serialized ? (
                <>
                  <p className="eyebrow">03 · scan the manufacturer serial</p>
                  {!unreadable && (
                    <Scanner
                      serial
                      label="Manufacturer serial"
                      onScan={async (value) => {
                        setSerial(value);
                      }}
                    />
                  )}
                  {serial && !unreadable && <p className="selection">✓ Serial scanned: {serial}</p>}
                  <label className="checkbox">
                    <input
                      type="checkbox"
                      checked={unreadable}
                      onChange={(event) => {
                        setUnreadable(event.target.checked);
                        setSerial("");
                      }}
                    />
                    Serial is unreadable
                  </label>
                  {unreadable && (
                    <label>
                      Unreadable reason
                      <textarea
                        aria-label="Unreadable reason"
                        value={unreadableReason}
                        onChange={(event) => setUnreadableReason(event.target.value)}
                        maxLength={4000}
                      />
                    </label>
                  )}
                </>
              ) : (
                <label>
                  Received quantity
                  <input
                    aria-label="Received quantity"
                    type="number"
                    min="0.000001"
                    step="any"
                    value={quantity}
                    onChange={(event) => setQuantity(event.target.value)}
                  />
                </label>
              )}
              <label>
                Observed condition
                <select
                  aria-label="Observed condition"
                  value={condition}
                  onChange={(event) => setCondition(event.target.value)}
                >
                  <option value="">Choose condition</option>
                  {["GOOD", "DAMAGED", "OPENED", "UNKNOWN"].map((value) => (
                    <option key={value} value={value}>
                      {value}
                    </option>
                  ))}
                </select>
              </label>
              <label>
                Receipt photo
                <input
                  aria-label="Receipt photo"
                  type="file"
                  accept="image/*"
                  capture="environment"
                  onChange={(event) => {
                    const file = event.target.files?.[0];
                    if (!file) {
                      setPhoto(undefined);
                      return;
                    }
                    if (file.size > 16 * 1024 * 1024 || !file.type.startsWith("image/")) {
                      setError("Choose an image up to 16 MiB");
                      setPhoto(undefined);
                      return;
                    }
                    setPhoto({
                      blob: file,
                      filename: file.name,
                      media_type: file.type,
                      captured_at: new Date().toISOString(),
                    });
                  }}
                />
              </label>
              {photo && (
                <p className="selection">✓ Photo retained until its upload and link succeed</p>
              )}
              <button className="primary" disabled={!ready || busy} onClick={() => void capture()}>
                Save receiving capture
              </button>
            </>
          )}
          <button className="secondary finish" disabled={busy} onClick={() => void finish()}>
            Finish receipt
          </button>
        </>
      )}
      {message && (
        <p role="status" className="selection">
          {message}
        </p>
      )}
      {error && (
        <p role="alert" className="error">
          {error}
        </p>
      )}
    </div>
  );
}
