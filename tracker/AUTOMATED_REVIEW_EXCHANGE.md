# NIH Tracker automated-review exchange (v19.1.1)

The exchange remains file-based, source-bound, restart-safe, and human governed:

```text
candidate queue -> sealed bounded batch -> web review
-> strict local import -> named human approval -> guarded finalization
```

v19.1 adds an optional GCP ADC execution path for sealed Gemini batches. No provider API key is required. Existing web-file exchange remains available; both paths use strict import and named human approval.

## Supported queues

| Queue family | Manual review | Sealed web-review batch |
|---|---:|---:|
| Card/name candidates | Yes | Yes |
| Combined identifier candidates | Yes | Yes |
| RRID candidates | Yes | Yes |
| FAIRsharing candidates | Yes | Yes |
| Crosswalk references | Yes | Yes |

## Evidence boundary

v19.1.1 also executes frozen queue plans using Gemini on GCP and optional Claude
on GCP. Each provider receives the same source packet and does not receive the
other provider's answer. Exact current recommendations are reused; successful
batch imports are skipped on resume. See `GCP_SEMIAUTOMATED_REVIEW.md`.

Card/name ACCEPT selects only the existing proposed UID. After named approval,
the original source label enters the existing ACCEPT_ALIAS transaction. There
is no model-created alias, canonical rename, new card or merge. Card/name and
identifier DEFER recommendations leave the source candidate unresolved.

Transport validity, queue and item hashes, card hashes, and allowed-target checks establish source binding. They do not establish factual truth. Provider output remains a candidate judgment until a named human reviewer approves it.

## v19 finalization ordering

`APPROVAL_REQUIRED` has precedence over `READY_TO_FINALIZE`. When imported review items await human approval, the finalizer:

- launches no promotion script;
- launches no operational application script;
- writes an auditable blocked result;
- directs the user to recommendation triage.

After approval, the normal single finalization control delegates to the existing identifier, crosswalk, and card/name transaction-safe scripts.

## Existing human decisions take precedence

A later provider recommendation does not reopen a review item when a non-pending operational adjudication already exists for the same stable `review_key`. The model output remains available for audit, but the durable human decision remains authoritative until a reviewer explicitly edits or reopens it. This prevents recommendation inventory churn from blocking application of already-reviewed identifier or crosswalk decisions.

## Catalog boundary

Automated-review records and internal adjudication artifacts are not published as public catalog evidence. Only normalized, reviewed claims, sources, dates, limitations, and public evidence states enter the v19 catalog.
