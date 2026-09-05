# NON-NORMATIVE

# Appendix B. Future Notes (non-normative)

Design notes for versions beyond v0.1. They bind nothing. They exist so that later decisions are made with these considerations already on the table, and so that none of them is used as an argument to widen v0.1.

## B.1 Procurement and cost

- A procurement need, requisition, ticket, or approval remains an external reference on the purchase order (D2), not a FleetOps workflow record. The moment FleetOps holds approval state, it owns procurement workflow.
- Cost allocation remains attribution only: a cost-center or business-unit reference on a PO line or license product. FleetOps never calculates splits, accruals, or accounting treatment.

## B.2 Operator experience

- The operator interface exposes verbs and workflows — Receive, Move, Assign, Replace, Retire, Resolve — not tables, states, projections, or reconciliation concepts. The complexity belongs underneath. A steady-state administrator should be able to run FleetOps without knowing its architecture exists.
- The system must support the small, ugly work well: scanning a serial, marking a label unreadable, finding a missing box, correcting a receipt, swapping a workstation. Dashboards are downstream of that, not a substitute for it.

## B.3 Governed operations (v0.5+)

- A policy layer (Defiant Guardrail, reached through a local policy adapter consistent with other Defiant products) applies only to designated consequential operations. Ordinary recordkeeping never depends on it.
- Candidate governed classes: bulk transitions above a threshold; any action on non-Defiant-owned property; quarantine release; movement crossing an export-sensitive boundary; automated or agent-originated requests of any kind.
- FleetOps supplies the facts — identity, ownership, custody, location, state, version, disposition, evidence, assigned trust level. The policy layer supplies the decision: allow, block, require approval, escalate. FleetOps knows what is true; the policy layer decides whether a requested change is permitted.
- A governance decision becomes immutable execution evidence, referenced from the transition or movement it authorized (D17). The decision must bind to the exact request: request id, actor, operation, target entities and their expected versions, relevant state, policy pack and version, decision, decision time, and any approval. A decision bound to a different request or a changed version is not evidence for this one.
- Execution of a governed operation runs the same expected-version check as D3 and D12. If any target moved between decision and execution, the decision is stale and the operation returns to evaluation. Decisions carry time-bounded validity.
- Governance unavailability: governed operations enter PENDING_GOVERNANCE and are not executable until evaluation succeeds — fail closed. Not "pending approval," which would imply the action has been found approvable. PENDING_GOVERNANCE is a state of the requested operation, never of the asset (D8).
- Governed requests reuse the capture-operation record shape (operation id, actor, target, expected version, payload, sync state). The offline queue and the governance queue are one contract.
- The existing FleetOps trust assignment (Section 7) remains an input to policy evaluation. Section 7 is revisited only if the future implementation moves authority assignment or enforcement out of FleetOps in a way that contradicts it; that is a possible amendment trigger, not a known required amendment.
- The market for governed physical-operations control is narrow today. The generic buyer wants to know where the machines are and who has them. Governance is the story for the second customer, not the first, and it must not influence the v0.1 through v0.4 build.
