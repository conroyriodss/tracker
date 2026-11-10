# v19.2.1 — maintenance reporting and plan selection

An already-absent card lock now returns PASS with removed=false and an explicit
already_unlocked audit outcome. A remaining lock or unlink failure still fails;
the confirmation requirement remains in force.

The maintenance notebook replaces the manually entered cleanup path with a list
of valid unapplied sealed plans. No plan is initially selected. Inspection shows
the exact path, SHA-256, eligible and protected paths. Apply requires both review
and stopped-writer acknowledgments, and rejects a plan changed since inspection.
Refreshing or changing selection clears review. Existing ledger history is retained.

CI runs the full suite with pinned dependencies on Python 3.12 and 3.13. Live
Colab rendering and production Drive operations were not exercised here.

# v19.2 — provenance, maintenance and source versioning

See OPERATIONS_AND_VERSIONING.md for installation and the behavior changes.
Code refresh preserves existing reference snapshots; recreate older install
plans. Cleanup preserves structured run evidence and card history, and archives
eligible paths before removal. Optional review-source pins require explicit
unpinning to select refreshed sources. Human approval rules are unchanged.

# v19.1.1 GCP-assisted candidate review

Card/name candidates now support sealed model recommendations and existing
human-approved finalization. GCP queue plans freeze selected rows, evidence,
code and model configuration; progress is recorded per provider and batch.
Successes are skipped on resume, failures require explicit retry, and stale
card/name evidence cannot be automatically rebound at promotion.

Optional Claude on GCP supplies a second assessment without seeing Gemini's
answer. Project enablement and structured-output policy must be checked by the
operator; the default remains Gemini alone. Permitted source-cache excerpts can
supplement registry snapshots. Interactive Google Search remains separate.

Install with **Refresh release code**, reopen the v19.1.1 notebook, and follow
GCP_SEMIAUTOMATED_REVIEW.md. Existing settings are preserved; new optional fields
have defaults in code. No cloud resource, approval or card is changed by install.
See VALIDATION_REPORT.txt for actual checks and unresolved runtime limitations.

---

# v19.1 GCP and assessment extension

The existing v19.0.2 workflow remains in place. Install this release through the
production code-refresh operation, then reopen the workbench. Optional GCP tools
are appended to the workbench and disabled by default. The main pipeline remains
offline unless its existing external-refresh controls are explicitly enabled.

New files, source snapshots, model candidates, human review decisions and cloud
archive receipts are described in GCP_INTEGRATION_AND_ASSESSMENT.md.
ASSESSMENT_CRITERIA.md defines all 22 criteria and their verification limits.
Read VALIDATION_REPORT.txt for executed checks and unresolved test limitations.

---

# NIH Tracker v19.0.2 release notes

## v19.0.2 guided-review state correction

- A later model recommendation no longer reopens an item that already has a non-pending human operational adjudication for the same stable review key.
- Such recommendations remain in the audit inventory and are counted as covered by current decisions.
- The review workbench now presents one state-appropriate next action and hides empty candidate selectors.
- A state with zero pending queue rows and two unapplied identifier adjudications resolves to `READY_TO_FINALIZE`, not `APPROVAL_REQUIRED`.
- No card, catalog, recommendation, approval, or adjudication content is rewritten by the hotfix.

## v19.0.1 operational corrections

- GitHub publication accepts the valid zero-record `data/change_history.jsonl` stream while continuing to reject empty required catalog artifacts.
- Sealed publication plans record and revalidate every intentionally empty payload.
- The normal maintenance notebook no longer exposes the completed dev/staging deletion workflow.
- The compatibility deletion planner no longer hashes the entire My Drive root and returns a clean no-op when legacy roots are absent.

v19.0 is a major public-information-architecture and data-contract release. It converts the tracker from a wide repository directory into a small, static, evidence-bearing knowledge graph while retaining the reviewed card collection as the sole mutable source of truth.

## Knowledge graph compiler

A new offline `step_build_catalog.py` compiles every current repository card into:

- typed nodes;
- typed relationships;
- claim-level provenance and review dates;
- controlled public vocabulary terms;
- resolved public resource views;
- meaningful field-level change history;
- coverage, relationship, and evidence-currency audits;
- JSON, JSONL, CSV, and JSON-LD artifacts;
- a sealed deterministic manifest.

The compiler does not contact external services or write cards. Repeated builds from identical inputs and the same `--as-of` date are byte-identical.

## Public interface

The public site is now the **NIH Data Resource Navigator + Evidence Graph**. It provides:

- four task-based entry points;
- explainable faceted search;
- at-a-glance result cards;
- progressive resource profiles;
- comparison of up to five resources;
- explicit no-match guidance;
- a portfolio observatory;
- public methods, scope, glossary, limitations, and evidence dossier;
- keyboard-visible controls and textual equivalents for status badges.

Results use deterministic fit classes—meets selected criteria, meets with caveats, near match, insufficient evidence, or excluded by selected criteria—rather than an opaque numerical score.

## Public terminology

v19.0 introduces policy-safe labels for:

- NIH relationship;
- deposit eligibility;
- access pathway;
- operational status;
- evidence state;
- programmatic access;
- PID level;
- resource type;
- data modality;
- organism and model-system scope;
- human-data access/sensitivity context.

`Not established` remains distinct from `No` and `Not applicable`.

## Interoperability, API, and GUID representation

APIs, standards, PID schemes, and participant GUID systems are first-class graph nodes connected to resources through typed relationships and source-bound claims. API evidence preserves endpoint, documentation, authentication/access context where present, and categorical evidence state. Endpoint normalization accepts both legacy `url` and explicit `endpoint_url` fields. Organism/model-system scope is derived from source-bound taxonomy, BMIC model-system, and descriptive terms. Human-data context combines that evidence with the documented resource-level access pathway and explicitly does not determine consent, de-identification, privacy, or legal sensitivity for individual assets.

## Policy and publisher relationships

NIH Controlled-Access Data Repository status, dbGaP external-data-source context, access systems, and publisher-policy mentions are represented as separate claims and relationships. The frontend makes clear that CADR listing, current operational availability, and study-level association are not interchangeable.

## Static publication

`step_publish_github.py` now seals and publishes:

```text
docs/index.html
docs/data/**
docs/schemas/**
```

The managed-file manifest allows obsolete catalog files from an earlier publication to be removed safely without owning unrelated repository content.

## Review workflow correction

Imported recommendations awaiting human approval now take precedence over downstream finalization. The guided finalizer refuses validation or application while unapproved imported recommendations remain and records that no operational mutation was attempted.

## Data-asset readiness

The release includes a JSON Schema and template for future `DataAsset` records and reserves graph relationships for repository-to-asset, access-system, GUID, award, publication, and standards links. No bulk data-asset ingestion occurs in v19.0.

## Compatibility

- Existing cards and card schemas are not rewritten.
- Existing review, recommendation, approval, adjudication, and receipt files remain valid.
- `step_build_spine.py` remains as a compatibility export.
- The frontend builder retains a legacy rendering entry point for the regression suite; production v19 always consumes `build/catalog`.
- Production-only installation publishes only v19 notebooks.
