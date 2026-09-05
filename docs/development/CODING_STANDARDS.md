# Defiant FleetOps Coding Standards

Production code must be understandable to the next competent developer who has never seen this repository.

These commenting and documentation requirements are mandatory for both Codex and Claude Code. They remain subordinate to the frozen FleetOps Architecture & Boundary document and do not reinterpret, replace, or amend it. A conflict with a controlled architectural section requires STOP and a report, not a code workaround.

## 1. Explain why, not syntax

Comments and docstrings must explain, where applicable:

- Why non-obvious logic exists.
- Which invariant or architecture decision it protects.
- Concurrency assumptions.
- Permission/security assumptions.
- Reconciliation behavior.
- Historical-integrity requirements.
- Unusual edge cases.
- Why a simpler-looking implementation is deliberately not used.

Do not add comments that simply restate obvious code.

Bad:

```python
# Increment version by one.
version += 1
```

Good:

```python
# Version changes in the same transaction as history so an
# offline client can never observe a new state with an old version.
```

## 2. Required documentation surfaces

Provide useful comments and docstrings for:

- Important and public modules.
- Public or non-trivial classes.
- Public or non-trivial functions.
- Database SECURITY DEFINER functions.
- Migration helpers.
- Reconciliation queries.
- Permission-sensitive code.
- Offline/conflict handling.
- Correction/reversal logic.
- Integration boundaries.
- Any logic whose behavior is constrained by D1–D19.

Docstrings and comments should reference the governing decision when that materially helps:

```python
# D3: history is authoritative; current_state is only a projection.
```

Do not mechanically annotate every line with D-numbers.

## 3. Explain enduring reasons

A comment must explain an enduring reason, not temporary implementation history.

Avoid:

```python
# Changed this because the previous version failed tests.
```

Prefer:

```python
# Validate against authoritative transition history rather than
# trusting the projection, which may be corrupt. See D3.
```

## 4. Occasional dry humor is encouraged

Restrained snark or sarcasm is welcome in internal developer comments when clarity remains primary.

Examples:

```python
# A corrupted projection does not get to grade its own homework.

# The PO records what we expected. Reality remains stubbornly
# entitled to disagree.

# External IDs are references, not identity. Vendors may reorganize
# their universe without reorganizing ours.

# Last-writer-wins is wonderfully simple right up until two people
# move the same workstation to different buildings.

# Do not "fix" history with UPDATE. Time machines are out of scope.
```

Rules:

- Humor is occasional, not constant.
- Never sacrifice technical clarity.
- Never insult users, customers, coworkers, vendors, contributors, or maintainers.
- Never joke about safety incidents, security incidents, export control, compliance breaches, data loss, or other serious failures.
- Never use sarcasm in user-facing errors unless explicitly designed.
- Humor never replaces the actual technical explanation.

## 5. Architecture and invariant comments

Where code implements an architectural invariant, explain enough that a future developer understands what must not be casually simplified. Refer to the governing architecture for the contract; comments must not weaken or redefine it.

This is especially important for:

- Append-only history.
- Projection reconciliation.
- Expected-version concurrency.
- Immutable evidence.
- Correction by reversal.
- Ownership/custody/location temporal history.
- Source-of-truth boundaries.
- Offline conflict behavior.
- Database privilege boundaries.

## 6. Tests are documentation

Tests should have descriptive names that identify the contract being proved.

For complex negative/invariant tests, include a short comment explaining the failure mode being prevented. Do not comment obvious arrange/act/assert mechanics.

## 7. Comments must stay correct

If behavior changes, relevant comments and docstrings must change. Incorrect documentation is a defect.

If code and a comment disagree, do not preserve the comment merely because it sounds authoritative. Resolve the discrepancy against the governing contract; do not change the architecture through a comment.

## 8. Professional tone

Production code should remain maintainable and professional.

The occasional sarcastic comment gives the codebase personality; the repository must never read like a comedy routine.

## 9. Git identity and AI attribution

Git identity and attribution rules are mandatory.

- Never modify `git user.name`.
- Never modify `git user.email`.
- Never modify repository or global Git identity/configuration unless Sam explicitly directs it.
- Preserve the existing configured human Git identity.
- Never add AI/model/tool attribution to commits or repository content.
- Prohibited examples include `Co-Authored-By: Claude`, `Co-Authored-By: Codex`, and `Co-Authored-By` for any AI, model, agent, or tool.
- Never add `Generated-By`, `Assisted-By`, `Generated by Claude`, `Generated by Codex`, `AI-assisted`, `AI-generated`, model attribution, agent attribution, bot attribution, tool attribution, or similar signatures or trailers.
- Do not add AI attribution to source comments, docs, changelogs, PR text, release notes, or headers unless Sam explicitly requests it.
- Do not add `Signed-off-by` or other trailers unless the active task explicitly requires them.
- Codex does not commit or push unless explicitly authorized.
- Claude Code commits and pushes only when the active review workflow explicitly authorizes it after PASS.
- Lack of AI attribution is intentional repository policy and must not be "corrected."

AI attribution is intentionally excluded from this repository. Do not add it automatically.
