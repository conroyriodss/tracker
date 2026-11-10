# NIH Tracker v19.0.2 hotfix notes

v19.0.2 is a bounded guided-review state correction for the v19 knowledge-graph release. It preserves all cards, catalog semantics, graph schemas, controlled vocabularies, imported recommendations, human approvals, and operational adjudications.

## Incident corrected

The review dashboard could report both:

```text
APPROVAL_REQUIRED
Imported items awaiting approval: 2
Identifier adjudications awaiting application: 2
```

while every current queue row was already decided. The candidate selectors were therefore correctly empty, but the state engine had reopened two newer model recommendations after older approvals ended as `superseded:` or `quarantined:`. It did not account for the fact that durable human identifier decisions already existed for the same stable review keys.

## Decision precedence

v19.0.2 applies this order:

1. A non-pending operational adjudication is the authoritative human decision for its stable review key.
2. A later provider recommendation for that key remains in the audit inventory but does not reopen human approval automatically.
3. A recommendation may reopen an item only when no current operational decision exists and every prior approval is terminally superseded or quarantined.
4. Reconsidering an existing adjudication requires an explicit human edit or manual reopen; model output alone cannot displace it.

This changes workflow state only. It neither applies nor rewrites the two identifier adjudications.

## Workbench correction

The workbench now has a state-aware next-action panel:

- `APPROVAL_REQUIRED` shows one prominent approval button.
- `REVIEW_REQUIRED` shows only nonempty candidate queues.
- `READY_TO_FINALIZE` hides empty queue selectors and directs the reviewer to finalization.
- `COMPLETE` directs the user to the main pipeline.

The dashboard also reports how many later recommendations are already covered by current human decisions.

## Expected effect for the reported state

After installation and **Refresh review status**, the supplied state should become approximately:

```text
READY_TO_FINALIZE
Imported items awaiting approval: 0
Later recommendations already covered by current decisions: 2
Identifier adjudications awaiting application: 2
```

Run **Validate**, inspect the identifier decision plan, then **Apply all**. No re-import or repeated model review is required.
