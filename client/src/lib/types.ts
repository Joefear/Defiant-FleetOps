/** Wire records retain server-owned identity/version and capture claims separately. */
export type Json = null | string | number | boolean | Json[] | { [key: string]: Json };
export type Scope = string;
export type Identity = { org_id: string; user_id: string; actor_id: string };
export type Session = Identity & { access_token: string; expires_at: string; username: string };
export type Operation = {
  operation_id: string;
  actor_id: string;
  client_id: string;
  client_epoch: string;
  client_seq: number;
  entity_type: string;
  entity_id: string;
  expected_version: number | null;
  operation:
    "RECEIVE_SCAN" | "MOVE" | "ASSIGN" | "UNASSIGN" | "TRANSITION" | "ATTACH_EVIDENCE" | "RESOLVE";
  payload: Record<string, Json>;
  occurred_at: string;
};
export type OperationResult = {
  operation_id: string;
  sync_state: "APPLIED" | "REJECTED" | "DUPLICATE";
  recorded_at: string | null;
  result: Record<string, Json>;
  sequence_flags: string[];
};
export type Photo = { blob: Blob; filename: string; media_type: string; captured_at: string };
export type QueueRow = {
  id: string;
  scope: Scope;
  ordinal: number;
  envelope: Operation;
  status: "QUEUED" | "APPLIED" | "REJECTED";
  response?: OperationResult;
  photo?: Photo;
  attachment_id?: string;
  evidence_operation?: Operation;
  evidence_ordinal?: number;
  evidence_done?: boolean;
  evidence_error?: string;
  error?: string;
};
export type Entity = { entity_id: string; entity_type: string; summary: Record<string, Json> };
export type Asset = {
  id: string;
  item_id: string;
  asset_tag: string;
  description: string;
  version: number;
  current_state: string;
  current_location_id: string | null;
  current_assignment_id: string | null;
};
export type Options = {
  asset_id: string;
  expected_version: number;
  from_state: string;
  options: { to_state: string; requires_evidence: boolean }[];
};
export type Item = { id: string; description: string; serialized: boolean; uom: string };
export type Party = { id: string; display_name: string; roles: string[] };
export type Actor = { id: string; display_name: string; type: string; active: boolean };
export type Order = { id: string; vendor_party_id: string; po_number: string; status: string };
export type OrderLine = {
  id: string;
  item_id: string;
  quantity: string;
  uom: string;
  line_number: number;
  active: boolean;
  superseded: boolean;
};
export type Comparator = OrderLine & { correction_generation: number; expected_item_id: string };
export type Receipt = {
  id: string;
  vendor_party_id: string;
  po_id: string | null;
  dock_location_id: string | null;
  received_at: string;
  reconciled: boolean;
  comparator_bindings: {
    po_line_id: string;
    source_generation: number;
    source_id: string | null;
  }[];
  lines: { id: string; item_id: string; asset_id: string | null }[];
  exceptions: { id: string; exception_type: string }[];
};
export function scopeOf(identity: Identity): Scope {
  return identity.org_id + "|" + identity.actor_id;
}
export function isUuid(value: string): boolean {
  return /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(value);
}
