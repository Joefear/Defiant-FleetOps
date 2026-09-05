Markdown transcription of the frozen DOCX architecture document. The DOCX remains the original frozen source artifact. Any discrepancy must be resolved against the DOCX.

**Defiant FleetOps**

Architecture & Boundary v0.1

Defiant Inds. Inc. — Frozen 4 September 2026

Status: FROZEN. Sections 5 through 10 are controlled and change only by revising this document. Appendices are non-normative.

*Internal document. Contains no schema. The schema is derived from this document, not the reverse.*

---

# 1. Purpose and Status

This document freezes the architectural decisions for Defiant FleetOps before any code or schema exists. It records what FleetOps is, what it owns, what it deliberately does not own, and the small set of decisions that are prohibitively expensive to retrofit once operational data exists.

It exists because the failure mode of this class of system is well understood: the schema becomes the architecture by accident, boundaries erode one reasonable-sounding feature at a time, and the result is a half-built ERP that nobody trusts and everybody works around with spreadsheets. The decisions below make that outcome require a deliberate amendment rather than a quiet afternoon.

Sections 5 through 10 are controlled. Section 11 lists what has been deliberately left undecided, so that deferral is visible rather than accidental. Section 12 states how the document is changed. The appendices carry a glossary, non-normative notes for later versions, and the public problem reference that informed the design; none of them binds implementation.

# 2. What FleetOps Is

FleetOps is the authoritative record of physical operational truth for Defiant: what a physical thing is, who owns it, who has custody of it, where it is, what state it is in, where it came from, what went into it, who changed it, why, and what evidence supports that history.

It is not an ERP, an MES, an MRP system, a QMS, a WMS, a CRM, or an accounting system. Those categories are large, mature, and purchasable. FleetOps is the layer those systems all need and none of them own: the reconciled physical reality that every one of them holds a partial and eventually incorrect copy of.

The design case is deliberately double. The near-term problem is a factory-support device fleet — workstations, scanners, printers, test equipment, network gear, and the software licenses attached to them — procured, received, configured, assigned, maintained, replaced, and retired. The later problem is manufacturing materials for PCB and LED production: lots, reels, trays, consumption, and genealogy. The architecture serves both from the start. Only the first is built first.

Positioned internally, FleetOps is a physical asset and operational-truth control plane for factory systems, with procurement provenance, lifecycle management, reconciliation, and licensing extensions. It is not an ERP replacement and it is not a clone of anyone else's system.

# 3. The Boundary

## 3.1 FleetOps owns

- Identity of physical things, permanent and internal
- Ownership — whose property a thing is
- Custody — who physically holds it, which is a different question
- Location, at whatever granularity has been captured
- Physical state and its transitions
- Provenance — vendor, purchase order, receipt, manufacturer, lot
- Physical movement and inventory balance
- Composition and genealogy — what is installed in what, what went into what
- Assignment — to a person, a station, a deployment, a customer
- Evidence attached to all of the above
- Exceptions where reality and expectation disagree

## 3.2 FleetOps does not own

- General ledger, payables, receivables, payroll, multi-currency
- Purchase requisition, approval workflow, purchase authorization, and supplier payment state
- Cost allocation, splits, accruals, or any accounting treatment — FleetOps records attribution references only
- Production scheduling and machine process execution
- Formal MRP planning and demand forecasting
- Quality management system documentation and corrective-action workflow
- Customer relationship management and quoting
- Carrier rating, manifesting, and tracking as a source of truth

FleetOps integrates with systems that own these. It does not reimplement them, and it does not accept their version of physical reality over its own.

# 4. Domain Vocabulary

Four concepts carry most of the model. Conflating any two of them is the most common and most expensive modelling error in this domain.

| Concept | Question it answers | Examples |
| --- | --- | --- |
| Item | What kind of thing is this? | Catalog entry: manufacturer, MPN, revision, unit of measure, whether serialized |
| Asset | Which individually identified thing is this? | A workstation, a scanner, an SMT feeder, an LED controller, a serialized board |
| Lot | Which manufacturing or supplier batch did this material come from? | Manufacturer lot A1734 — provenance, immutable, not a container |
| Stock Unit | Which physical container holds it, and how much is left? | A reel, a tray, a tube, a bag, a bin quantity |

Supporting concepts: Party (any external or internal entity — vendor, manufacturer, customer, carrier, subcontractor, internal organization); Facility and Location (space); Purchase Order and PO Line (what was expected); Receipt and Receipt Line (what actually arrived); Actor (who or what caused a change); Attachment (evidence); Exception (a recorded disagreement between expectation and reality). Appendix A defines each.

One invariant governs stock units: a stock unit is exactly one Item, from exactly one Lot, in exactly one current Location. Splitting a reel creates a new stock unit referencing the same lot. Material physically combined beyond distinction is handled under D9, not by pretending the original units still exist.

# 5. Normative Decisions

These are binding. Each exists because retrofitting it after operational data accumulates is expensive, destructive, or impossible.

### D1. Identity is internal, opaque, and permanent

Every entity carries an opaque internal identifier (ULID or UUID) that never changes, never repeats, and never means anything. Human-readable identifiers are separate, editable fields that may follow production naming conventions.

For an asset that means at minimum: an internal id; a Defiant asset tag; a manufacturer serial; and, where applicable, a production-issued serial. Each is a distinct field with a distinct owner. None of them is the key.

Semantic identifiers rot. Asset tags get reprinted, serials get reissued after a board swap, sites renumber their racks. Meaning lives in display codes; reference lives in the internal id.

### D2. External identifiers never become FleetOps identity

A vendor order line, a carrier tracking number, an ERP item code, an MES material id, a ticket or requisition number, a cost-center code, or a CFX unit identifier is recorded as an external reference against a FleetOps entity. One entity may hold many external identities from many systems. No external identifier is ever promoted to a primary key or used as one.

This is also how upstream context enters FleetOps without FleetOps owning the workflow: the need, the approval, the security review, and the cost attribution that preceded a purchase are references to the systems that own them, never FleetOps records with their own state.

### D3. History is authoritative; current state is a maintained projection

Asset transitions and stock movements are the authoritative record. Current state and current balance exist as projections maintained in the same transaction that writes history.

The application role cannot directly update a projection column. A transition validates the expected from-state, inserts the immutable transition record, updates the projection, and commits atomically.

The reconciliation invariants are written on day one and run in tests and in operational health checks: the latest transition to-state equals the stored current state, and the sum of stock movement deltas equals the stored balance.

One dependency must not close on itself. A projection may be read for convenience and for concurrency checks, but it may never be the only basis on which authoritative history is admitted: expected-state validation must be resolvable against the transition record, and reconciliation must compare the projection to history rather than to itself. Otherwise a corrupted projection admits an invalid transition, which then becomes the history that validates the projection.

This is not pure event sourcing and is not intended to be. A privileged database user can always bypass an application rule; the goal is to make bypass deliberate, privileged, and detectable rather than to pretend SQL can make it impossible.

### D4. Quantity changes are ledger entries; reservations are not

Physical quantity is never assigned. It is derived from an append-only ledger of receipts, consumptions, scrap, transfers, and cycle-count adjustments.

A reservation or allocation changes available quantity but not physical quantity on hand. Allocations live in their own record and never enter the physical ledger.

Availability has exactly one definition, computed in exactly one place: on hand, minus allocated, minus quality-blocked. No availability calculation exists anywhere else in the system.

### D5. Expectation and reality are separate records, permanently

A purchase order records what was expected. A receipt records what physically arrived. Neither is ever edited to make the other agree.

Where they disagree, the disagreement becomes a first-class exception record. The v0.1 exception types are: short, over, substitution, damaged, opened, unreadable identifier, serial mismatch, unexpected item, quantity variance. Missing lot is added with the materials layer.

Receiving personnel must always be able to record what is actually in front of them.

### D6. Corrections are new entries, never edits

No ledger row and no transition record is ever modified or deleted. A correction is a reversing entry plus a correct entry, both carrying a reference to what they correct and a stated reason.

People will fix mistakes. If this doctrine is unwritten they will fix them with an UPDATE, and the history becomes fiction with no way to detect it.

### D7. Ownership and custody are independent and temporal

Every asset and every stock unit carries an owner. Defiant may hold material it does not own — customer-consigned components, vendor-managed inventory, customer hardware in for repair — and may own material held elsewhere.

This is captured from the first migration. Retrofitting it means revisiting every historical record to decide whose material it was, and it changes the meaning of every inventory figure the system has ever produced.

Ownership and custody are temporal facts. Their current values may be maintained as projections, but every change is recorded as immutable history with actor and time; prior ownership or custody is never overwritten. The same principle governs location and assignment, since the acceptance test requires reconstructing every location a unit occupied.

Vendor and manufacturer are also distinct: the distributor who sold the part is not the manufacturer whose lot code the material carries.

### D8. Quality disposition is independent of location; state is orthogonal to everything else

A stock unit carries a disposition — available, on hold, quarantine, rejected, scrapped — independent of where it physically sits. Rejected material must be blockable without being moved, because moving it destroys location truth and confuses genealogy.

For assets in v0.1 this is expressed through lifecycle state; the separate disposition concept activates with the materials layer.

Asset lifecycle state is orthogonal to location, custody, ownership, and assignment. Changing state does not implicitly change any of them. A workstation may be assigned to a station, located on the production floor, and on hold pending investigation simultaneously.

A second asset-disposition subsystem is not built until evidence demonstrates that genuinely simultaneous states — a failed unit also under security hold — cannot be expressed this way. That would be an amendment, made with the evidence attached.

### D9. FleetOps never claims more traceability than the evidence supports

Every consumption record carries an explicit capture granularity: work order, production run, feeder interval, unit, or unknown. Every stock unit carries a provenance resolution, which degrades when material is physically mixed.

Effective traceability resolution is the minimum over a record's ancestry — the lowest capture granularity and the lowest provenance resolution anywhere in its derivation chain — computed at query time and never stored as a conclusion. Resolution is derived from parent records only, never from sibling or downstream records, so the derivation terminates at receipt and cannot loop. Genealogy results are returned as confirmed or possible accordingly.

Where two lots are combined beyond distinction, FleetOps records an explicit mixing operation producing a mixed stock unit with both sources and reduced resolution. It does not preserve fictional precision.

This matters most in exactly the situations where the system will be trusted least deservedly: recalls, counterfeit-part investigations, warranty disputes, and customer audits.

### D10. Every consequential change carries an authenticated actor

Actors are typed: human, system, device, or integration. A transition caused by an SMT line controller, an automated provisioning service, or an ERP integration is attributable without forcing machines into the user table.

Unattributed transitions are worthless as evidence and are not permitted.

### D11. Occurrence time and record time are different facts

Every event records when it happened and when it was recorded. These diverge whenever capture is offline, and neither may overwrite the other.

Occurrence time reported by a capture device is a claim, not a fact — device clocks drift, get reset, and occasionally report 1970. Each capture client supplies a sequence number that is monotonic within a client instance and epoch, so its own queued operations can be ordered correctly regardless of its wall clock. A reinstalled or reset client begins a new epoch rather than reusing the sequence.

All timestamps are stored in UTC. Local time zone is a property of the facility.

### D12. Offline capture, not offline authority

Capture clients may operate disconnected and queue operations. Queued operations carry an operation id, actor, client id and epoch, entity, expected entity version, operation, payload, occurrence time, record time, and sync state.

Conflict handling differs by operation class, deliberately:

- State and location changes are rejected on version mismatch and raised as a sync conflict for human resolution. Last-writer-wins is prohibited.
- A unique, authenticated and authorized relative physical-quantity observation is committed even when the recorded balance is insufficient. The resulting negative balance is permitted and immediately raises an exception requiring reconciliation by cycle count.
- This is not an exemption from idempotency, authorization, disposition, or trust rules. A duplicate, unauthorized, or untrusted quantity operation is rejected like any other.

The reasoning is that a rejected consumption would leave FleetOps claiming material exists that was physically consumed — the same falsification D5 prohibits. A negative balance is an honest signal; a phantom balance is a lie.

### D13. Units of measure are immutable on the records that used them

Every quantity is recorded with the unit of measure under which it was captured. Changing an item default later must never reinterpret historical lines or movements.

This applies to purchase order lines and receipt lines from v0.1, before the materials ledger exists.

### D14. Machine-readable identity carries nothing but the identifier

Barcodes and tags contain the opaque internal identifier and nothing else. No composite strings, no embedded location, lot, part number, or date. No URL, since a URL introduces a durable dependency on a domain and routing scheme.

The printed human-readable portion of a label is unconstrained and may carry asset tag, description, and revision for human use.

This is the least reversible decision in the document. It cannot be changed after labels are in the field.

### D15. Capture is architecture, not interface polish

All physical capture workflows are scan-first when introduced. In v0.1 this applies to receiving, location movement, and assignment; consumption and shipping inherit the same rule when they arrive. D15 is not a licence to pull deferred workflows into v0.1.

A factory system that requires a person to navigate a browser and type a serial is a system that will be abandoned for paper.

Label generation is abstracted: label template, print service, printer adapter. No printer vendor appears in the domain model.

### D16. Handling metadata is captured at receipt; clocks start when they physically start

Static handling and shelf-life metadata — moisture-sensitivity class, floor-life budget, manufacturer shelf expiration, storage requirements — is captured at receipt as a property of the stock unit.

Clock-start events are captured when they physically occur, as events, not as receipt fields: dry-pack opening, solder-paste thaw, pot-life start, bake or reset completion. A sealed dry pack may sit for weeks before anyone opens it, so an opened-at recorded at receipt would be false.

Neither category is reconstructed retroactively. A clock that was never observed starting is unknown, not assumed.

The alerting, exposure calculation, and bake workflow come later. The capture points exist before the features do, because they cannot be backfilled.

### D17. Evidence is immutable and content-addressed

Attachments — packing slips, serial photographs, inspection results, calibration certificates, RMA failure analyses, disposal records — record their source, capturer, and capture time at the moment of attachment, and are content-hashed. A modified attachment is a new attachment recording what it supersedes.

Provenance cannot be reconstructed later, and for defense and aerospace customers it is purchase-blocking.

### D18. Multi-tenancy and export classification are cheap now and miserable later

Every tenant-owned record is organization-scoped from the first migration, whether or not a second organization ever exists. Static reference and system-internal data need not be.

Items carry export classification fields — ECCN or USML category and a controlled flag — captured at catalog creation. FleetOps does not enforce export control in v0.1; it records the classification so enforcement is possible without a catalog-wide archaeology exercise.

### D19. The core is vendor-neutral

No vendor API, machine protocol, or hardware driver appears in the core domain model. All external interaction crosses an adapter boundary governed by Section 7.

# 6. Source-of-Truth Matrix

The source-of-truth matrix assigns which system owns each fact. Integration authority is assigned in FleetOps by an authorized human; an integration never declares its own authority. Without this matrix, divergence gets resolved by whichever nightly job runs last.

| Fact | Owner | FleetOps role |
| --- | --- | --- |
| Requisition, approval, purchase authorization, supplier payment state | Procurement / ERP / accounting | References the PO and any upstream ticket; never asserts approval or payment state |
| Cost allocation and accounting treatment | Accounting | Records a cost-center or business-unit reference only |
| What physically arrived, in what condition | FleetOps | Authoritative; never overwritten by ERP quantities |
| Physical inventory, location, custody, movement | FleetOps | Authoritative |
| Machine and process execution detail | MES / equipment | Consumes as evidence; does not reimplement |
| Genealogy accepted from that evidence | FleetOps | Authoritative, at the declared resolution |
| Carrier tracking events | Carrier | Records as observation; owns the shipment-to-asset relationship |
| Software license terms and entitlement counts | Vendor / procurement | Owns seat assignment to assets and people |
| Quality disposition of physical material | FleetOps | Authoritative; QMS owns the corrective-action process |

Nightly bidirectional synchronization that overwrites FleetOps physical records from an external system is prohibited. Divergence produces a visible exception, never a silent correction.

# 7. Integration and Physical Interface Boundary

FleetOps is designed to talk to scanners, printers, scales, RFID readers, sensors, SMT equipment, carriers, suppliers, and enterprise systems. None of them may leak vendor-specific behaviour into the core. The following are binding:

- The FleetOps core is vendor-neutral. Hardware and protocol specifics live in adapters.
- External identifiers never replace FleetOps identity (D2).
- Every integration has its source-of-truth authority assigned before it is built (Section 6). Authority is granted in FleetOps configuration by an authorized human. A source never declares, asserts, or negotiates its own authority, and a payload claiming authority is data, not permission.
- Inbound integration events are idempotent: source system, source event id, payload hash, processing state. A repeated event has its consequence applied exactly once.
- Outbound integration uses an outbox written in the same transaction as the state change, and is retryable. Never change state, call an external API, and hope the network holds.
- Connection never confers authority. Each source is assigned an explicit trust level in FleetOps — capture only, observation, business record, manufacturing evidence, authorized operation, permitted transition — and is evaluated against the assigned level, never against a level it reports about itself.
- Integration must preserve provenance: what system said this, when, and on what evidence.
- Integration failures create visible exceptions, never silent divergence.
- Factory-floor protocol work and local hardware access will eventually run through a local edge service. The seam is named now; the service is not built now.
- The event model must not make later GS1 EPCIS import or export unnecessarily painful.

Relevant standards to leave seams for, none implemented in v0.1: IPC-CFX and IPC-2591 for electronics assembly data exchange, IPC-HERMES-9852 for SMT machine-to-machine handoff, OPC UA for general industrial equipment, GS1 EPCIS for supply-chain traceability exchange, and carrier APIs for shipping.

Environmental conditions are modelled as observations against a location, not as properties of a stock unit. A stock unit inherits exposure from where it physically was. This gives a future route to dry-cabinet, cold-storage, and ESD-area monitoring without FleetOps becoming a SCADA system.

Governed operations — consequential actions evaluated by a policy layer before execution — belong to the integration stage in Section 8 and are not part of v0.1 through v0.4. Appendix B records the non-normative design notes for that stage.

# 8. Build Order

The architecture serves both the factory-support fleet and the manufacturing materials layer. Only the first is built first. Later versions activate additional primitives; none of them require redesign.

| Version | Scope |
| --- | --- |
| v0.1 — Asset Fleet Core | Parties, items, assets and identifiers, facilities and locations, purchase orders and lines, receipts and lines, asset transitions, movement and custody and ownership history, assignment, configuration records, actors, evidence, exceptions, corrections, labels, scan-first capture, offline queue, audit |
| v0.2 — Software Licensing | License products, subscriptions, seats, assignment to assets and people, renewals, cost type and license type attribution, reclaim on decommission |
| v0.3 — Logistics | Transfers, shipments, returns, RMA, vendor returns, multi-site movement |
| v0.4 — Manufacturing Materials | Lots, stock units, stock movements, allocations, unit of measure on movements, disposition, moisture-sensitivity and expiry, cycle counts |
| v0.5+ — Genealogy, Integration, Governance | Work orders, consumption, composition, traceability resolution in production use, MES and ERP adapters, edge service, governed operations |

Software licensing at v0.2 is deliberately the ordinary kind: seats, assignments, renewals, cost categories, and reclaim. It is not the cryptographic product-entitlement system — signed entitlements permitting a shipped device to execute a purchased feature — which remains a later and separate concern with separate vocabulary.

The scope guard for v0.1: nothing enters it that Defiant's own hardware cannot exercise with real data immediately. The problem shape is drawn from a factory-scale device fleet; the build target is Defiant's own machines, test hardware, and network gear. Software licenses are activated in v0.2. FleetOps is used before it is ever marketed.

# 9. v0.1 Acceptance Scenario

v0.1 passes when the following runs end to end with no spreadsheet and no manual correction outside the system. The scenario is deliberately ugly, because happy-path receiving is not where these systems fail.

**Ordered:**

```
6 workstations, 2 barcode scanners, 1 label printer, 1 network appliance
```

**Actually delivered:**

```
6 workstations: 5 at the ordered configuration, one of which has an unreadable
   serial label; 1 at a substituted configuration
1 of the 2 barcode scanners (one short)
1 label printer and 1 network appliance, as ordered
1 accessory not on the purchase order
```

Expected exceptions, exactly: substitution, unreadable identifier, short, unexpected item.

FleetOps must:

- Receive what physically arrived without altering the purchase order
- Raise the four exceptions above as records, each referencing the relevant lines
- Create permanent asset identities for the nine serialized units and print labels carrying only the opaque identifier
- Scan assets into locations and record configuration
- Assign assets to a person or a station, with an authenticated actor on every transition
- Attach receiving evidence at the moment of receipt
- Continue operating with the network down, then resolve a deliberate conflicting state change on reconnect
- Accept a correction of a mis-keyed entry as a reversing plus corrected record

Then, over the subsequent lifecycle:

- One workstation fails and transitions out of service
- A replacement is assigned
- The failed unit is retired, with disposal evidence attached

Outbound RMA, vendor return, and re-entry into inventory are deliberately absent. They belong to the v0.3 acceptance test, in line with Sections 8 and 10.

Final test: select any unit and reconstruct its complete history — vendor, purchase order line, receipt, cost, configuration changes, every actor who touched it, every location it occupied, every exception raised against it, and its disposal record.

# 10. v0.1 Non-Goals

The following are normative exclusions. Each is a plausible-sounding way for a one-person build to disappear for a year. Adding any of them requires amending this document.

**Not built in v0.1:**

- Bills of material, work orders, kitting, production scheduling
- Lots, stock units, consumption, genealogy (v0.4)
- Software licensing (v0.2)
- Shipments, transfers, RMA workflow (v0.3)
- Requisition or request records, approval workflows, cost allocation logic
- Costing beyond unit purchase price on the receipt line; no costing method, no multi-currency
- Bin capacity, slotting, or pick optimization
- Role hierarchies beyond authenticated identity
- A reporting or analytics layer
- Customer-facing views
- Cryptographic product entitlements
- Governed operations or any policy-layer integration
- Any barcode content other than the opaque identifier

**No integration built in v0.1:**

- ERP or accounting synchronization
- MES implementation or integration
- IPC-CFX, IPC-HERMES-9852, OPC UA
- RFID automation, conveyor or PLC control
- Carrier and supplier APIs
- Environmental monitoring
- The edge service daemon
- EDI or EPCIS exchange

The architecture must nonetheless leave explicit seams for every item on the integration list. That distinction — seams now, implementations later — is the point of this document.

# 11. Deliberately Deferred

The following are open and are to be settled by operational evidence rather than by further design:

- Serialization policy per item class. The test is operational cost, not row count: serialize where individual identity matters for warranty, recall, field replacement, customer requirement, compliance, configuration, maintenance, unit value, or failure investigation. Defense work will push this further than commercial practice.
- Location granularity — facility, room, rack, bin, and how much of that is worth scanning
- Whether the entity version counter is single per entity or split by change class. Single is simpler and produces more false conflicts; that is the right trade at current scale.
- Attachment storage location and retention period, which is contract-dependent and measured in years for aerospace work
- Configuration capture depth for assets — image version, installed software, hardware revision — and how much is captured manually versus imported

# 12. Change Control

Sections 5 through 10 are controlled: the decisions, the source-of-truth matrix, the integration boundary rules, the build order, the v0.1 acceptance scenario, and the non-goals. They are changed by revising this document with a stated reason, not by a commit, a schema migration, or a conversation. Build order and pass conditions are contracts for the same reason the decisions are — a version whose scope can be quietly widened is not scoped.

Sections 1 through 4 and 11 are descriptive and may be clarified without an amendment, provided the clarification does not alter a controlled section's meaning. The appendices are non-normative and bind nothing.

The schema is derived from this document. Where the two disagree, this document is correct and the schema is a defect. Implementation that cannot satisfy a controlled section files an amendment request; it does not solve the conflict in code.

This document is authoritative because Defiant has designated it the governing contract for the implementation, not because it says so. Section 12 gives developers, reviewers, and future readers a test for detecting drift; the enforcement is a refused merge.

## 12.1 Document history

- 4 Sep 2026 — v0.1 freeze candidate.
- 4 Sep 2026 — Correction pass 1: D12 scope qualified; D8 orthogonality; D16 receipt-versus-clock-start; v0.1/RMA contradiction removed from Section 9; scope guard; Sections 8 and 9 brought under change control; D18 and D11 wording; circularity closures in D3, D9, and Section 7.
- 4 Sep 2026 — Correction pass 2: Section 6 declaration wording; D7 temporal history; D15 scoped to introduced workflows.
- 4 Sep 2026 — Consolidated rewrite (this version): D5 exception list made explicit and unreadable identifier added; Section 9 delivery disambiguated (six workstations, one scanner short, nine serialized units) so that the expected exception set is exact; Section 3.2 and Section 6 clarified for requisition, approval, and cost attribution; Section 10 excludes request records and governed operations explicitly; Section 8 scope text aligned; appendices added. Frozen.

# Appendix A. Glossary (non-normative)

| Term | Meaning |
| --- | --- |
| Item | A catalog entry: what kind of thing. Manufacturer, MPN, revision, unit of measure, serialized or not. |
| Asset | An individually identified physical thing with a lifecycle state, an owner, a custodian, a location, and history. |
| Lot | A manufacturer or supplier batch. Provenance only; never a container; immutable. |
| Stock unit | A physical container of one item from one lot in one location, with a quantity derived from its ledger. |
| Party | Any organization or person FleetOps needs to name: vendor, manufacturer, customer, carrier, subcontractor, internal organization. |
| Actor | The authenticated cause of a change. Typed: human, system, device, integration. |
| Projection | A maintained current-value column (state, balance, location, owner) derived from history and reconcilable against it. |
| Transition | An immutable record of an asset moving from one lifecycle state to another, with actor, reason, time, and evidence. |
| Movement | An immutable record of a change in location (assets) or a quantity delta (stock units). |
| Exception | A first-class record of a disagreement between expectation and reality, or of an anomaly requiring human resolution. |
| Correction | A reversing entry plus a correct entry, both referencing the original. Never an edit. |
| Attachment | Content-hashed, immutable evidence with capture-time provenance. |
| Disposition | Quality status of a stock unit, independent of location: available, on hold, quarantine, rejected, scrapped. |
| Traceability resolution | The confidence with which genealogy can be asserted, derived from capture granularity and provenance resolution over a record's ancestry. |
| Capture operation | A scanned or client-originated request to change something, carrying idempotency and version-check fields, applied server-side. |
| External reference | An identifier belonging to another system, attached to a FleetOps entity, never used as a key. |
| Governed operation | (Future) A consequential requested operation evaluated by a policy layer before FleetOps executes it. |

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

# Appendix C. Public Problem Reference (non-normative)

Design provenance for the factory-support fleet problem shape. This is a public problem reference only. It is not a customer requirement, no relationship with the company exists or is implied, and nothing here obligates FleetOps to any external system, scale, or workflow.

| Field | Value |
| --- | --- |
| Source | Job posting: "Senior Operations and Logistics Associate, Factory Systems" (with an associate-level variant), Anduril Industries |
| Locations named | Ashville, Ohio; Costa Mesa, California |
| Accessed | 4 September 2026, via the company's public careers listing |
| Use | Problem-shape reference for the v0.1 asset fleet scope. Not copied into the repository; summarized here. |

Summary in our words. A rapidly scaling factory footprint has a growing fleet of support devices — the hardware and software that keep production lines running — and the procurement, logistics, and licensing processes to absorb that growth do not yet exist. The role is a business-operations builder, not a functional IT or logistics seat: decompose how hardware and software actually flow from need to production floor, design the workflows, tooling, and standards that make it repeatable, run it by hand at first, and systematize it so a dedicated administrator can operate it at steady state.

Three phrases from the posting were directly useful. The role must "make physical status match system status" across warehouse and ticketing tools, which is the FleetOps problem statement. It must "verify received matches ordered," which is D5. And its stated core requirement is a track record of building from zero, weighted above years in any single function.

What FleetOps takes from it: the factory-support fleet is the right first domain; expectation-versus-reality reconciliation is central; software licensing is a real second domain rather than a spreadsheet; the back half of the lifecycle — refresh, RMA, warranty, returns, decommissioning, e-waste — matters as much as receiving; and turnkey operation by a non-architect is the target operator experience.

What FleetOps deliberately does not take from it: vendor management, purchase approval, cost allocation, security review, SOP authorship, and dashboarding are human and business processes or belong to other systems. FleetOps participates by reference (D2) and never owns them (Section 3.2).

---

*Copyright © 2026 Defiant Inds. Inc. All rights reserved. Internal document.*
