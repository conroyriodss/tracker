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

# NIH Tracker v19.0.2 migration and operations

## Upgrade from v18.5

1. Freeze the current v18.5 production build, publication receipt, card hashes, and source snapshots.
2. Upload the v19.0.2 release bundle to `/content` or the top level of My Drive.
3. Open `nih_tracker_colab_v19_maintenance.ipynb`.
4. Select **Refresh release code** and check **Apply**.
5. Reopen the v19 notebooks so the runtime imports the new modules.
6. Run the review workbench and confirm `COMPLETE` before a production apply pipeline.
7. Run the pipeline first in validate/propose mode with external evidence refresh unchecked.
8. Inspect the catalog migration audit and coverage reports.
9. Run the production apply pipeline.
10. Create and review a new sealed GitHub publication plan, then publish.

The installer preserves cards, inputs, runs, snapshots, builds, caches, locks, credentials, recommendations, approvals, adjudications, and application receipts.

## Pipeline sequence

The production sequence remains source-builder oriented, then adds the catalog layer:

```text
identity and link builders
interoperability/resource/status/policy builders
step_build_spine.py                 # compatibility export
step_build_catalog.py               # v19 graph and public views
step_build_frontend.py              # task-oriented site + public data mirror
release validation
sealed GitHub publication
```

`step_build_catalog.py` has no `--apply` mode because it does not mutate the source cards. It uses `NIH_TRACKER_CATALOG_AS_OF` or `--as-of` for reproducible historical builds.

## Required post-upgrade inspection

Inspect:

```text
build/catalog/migration_audit.csv
build/catalog/field_coverage.csv
build/catalog/relationship_coverage.csv
build/catalog/source_freshness.csv
build/catalog/manifest.json
```

Required conditions:

- one Resource node per top-level card;
- no dangling node, edge, claim, or source reference;
- no duplicate stable IDs;
- no unexpected card mutation;
- controlled organism/model-system and human-data-context facets are present or explicitly not established;
- no composite score;
- no network access during catalog compilation;
- deterministic repeated build hashes.

## Publication behavior

The publication plan now contains a sealed payload directory and file-level hashes for `index.html`, all public data artifacts, and all public schemas. Apply validates every file before cloning or modifying the remote branch.

Managed publication scope is limited to the NIH Tracker files recorded in `docs/.nih_tracker_publish_manifest.json`. Unrelated GitHub repository content is preserved.

## Rollback

The v19 catalog and frontend are derived build artifacts. To roll them back:

1. restore or retain the prior release code;
2. rebuild from the unchanged reviewed cards and pinned snapshots;
3. create a new sealed publication plan.

No card rollback is required merely because a derived catalog or frontend build is rejected. Card-level rollback continues to use transaction snapshots and `step_restore_card_snapshot.py`.

## Data-asset boundary

`schemas/data_asset.schema.json` and `templates/data_asset_template.yaml` are extension points. Do not bulk-materialize dbGaP studies or repository collections as v19 assets. A later pilot should use a bounded, preregistered selection and authoritative accession/DOI snapshots.

## Hotfix-specific operation

After installing v19.0.2, rerun the final publication cell. A new sealed plan may include an empty `data/change_history.jsonl`; this is valid when there are no material prior-catalog changes. The maintenance notebook no longer offers the already-completed dev/staging deletion operation.
## v19.0.2 review-state recovery

After installation, reopen the v19.0.2 review workbench and run **Refresh review status**. If later model recommendations are already covered by durable human adjudications, the dashboard reports them separately and moves directly to `READY_TO_FINALIZE`.

For the reported two-row identifier state:

1. Select **Validate** under Finalize all reviewed decisions.
2. Inspect the generated identifier `decision_plan.csv`.
3. Enable production writes only after review.
4. Select **Apply all**.
5. Refresh status and confirm `COMPLETE`.

Do not re-export, re-import, or re-approve those items solely because a later provider recommendation exists. Reopening a current human decision must be an explicit manual action.
