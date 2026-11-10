# NIH Tracker v19.2.2

**New in v19.2.2:** the installer hashes the sealed plan for its audit instead of
the entire project, compares each target once, stages only changed/new files,
and streams progress. Setup checks the installed release and eight critical
file hashes before loading the runtime. Preview explicitly says code has not
been installed. Existing review pins can be inspected without changing them.

**New in v19.2.1:** an absent card lock is a successful audited no-op. The
maintenance notebook lists saved cleanup plans, requires inspection and both
acknowledgments, and binds Apply to the inspected SHA-256. CI now covers
Python 3.12 and 3.13. See [OPERATIONS_AND_VERSIONING.md](OPERATIONS_AND_VERSIONING.md).

**New in v19.2:** stable review-source ordering and explicit hash pins; inventory
and coverage reports; dependency-aware retention with verified recovery archives;
input/code SHA-256 provenance; preservation of live reference snapshots during
code refresh; and a source tree with regression CI alongside the existing public
site. Start with [OPERATIONS_AND_VERSIONING.md](OPERATIONS_AND_VERSIONING.md).

**New in v19.1.1:** resumable GCP review of card/name, identifier and crosswalk
queues; optional independent Claude review of the same frozen evidence;
retrieval of permitted cached-source excerpts; and promotion through the
existing named approval and alias transactions. See
[GCP_SEMIAUTOMATED_REVIEW.md](GCP_SEMIAUTOMATED_REVIEW.md) for setup, request
bounds, search limitations and the 132-card plus one-identifier workflow.
No live model-access or recommendation-accuracy validation is claimed.

**New in v19.1:** optional Gemini on GCP using the `GCP_ID` Colab secret;
content-addressed Drive/GCS evidence storage and BigQuery search; interactive
Google Search research; and a 22-criterion A–D evidence assessment.
See [GCP_INTEGRATION_AND_ASSESSMENT.md](GCP_INTEGRATION_AND_ASSESSMENT.md) for
installation, cloud setup, caching boundaries and the first pilot, and
[ASSESSMENT_CRITERIA.md](ASSESSMENT_CRITERIA.md) for the automation boundary.
All live actions remain explicit. Existing human approval and card mutation
controls remain authoritative. New assessment reviews do not auto-publish.


NIH Tracker v19.1 retains a deterministic, evidence-bearing knowledge graph and a task-oriented public **NIH Data Resource Navigator** while preserving reviewed YAML cards and pinned project snapshots as the authoritative source of truth.

```text
reviewed cards + pinned references
                |
                v
      step_build_catalog.py
                |
                +-- typed nodes
                +-- typed relationships
                +-- claim-level evidence
                +-- controlled vocabularies
                +-- field-level change history
                +-- static JSON/JSONL/CSV/JSON-LD
                |
                v
      step_build_frontend.py
                |
                v
       GitHub Pages /docs
```

The v19.0.2 hotfix also makes the guided review state-aware: durable human adjudications take precedence over later model recommendations, and empty queue controls are hidden.

The public interface is organized around four decisions:

1. Find a place to share data.
2. Find data to reuse.
3. Explore NIH-supported infrastructure.
4. Compare resource capabilities.

The public site uses progressive disclosure: an at-a-glance decision card, a researcher-facing profile, and an expandable evidence dossier. Discovery facets include resource type, NIH relationship and ICO, data modality, organism/model-system scope, cautious human-data access context, deposit and access pathways, operational state, APIs, NIH CADR context, dbGaP context, and publisher-policy mentions. It does not calculate a composite completeness, maturity, FAIRness, confidence, or repository-fit score.

## Primary notebooks

- `nih_tracker_colab_v19_pipeline.ipynb`
- `nih_tracker_colab_v19_review_workbench.ipynb`
- `nih_tracker_colab_v19_maintenance.ipynb`

All notebook code cells are hidden by default. Production remains the only notebook environment.

## New catalog artifacts

`step_build_catalog.py` writes `build/catalog/`:

- `nodes.jsonl`
- `edges.jsonl`
- `claims.jsonl`
- `resources.json`, `resources.jsonl`, and `resources.csv`
- `catalog.jsonld`
- `change_history.jsonl`
- `vocabularies.json`
- `field_coverage.csv`
- `relationship_coverage.csv`
- `source_freshness.csv`
- `migration_audit.csv`
- JSON Schemas
- `manifest.json`

`step_build_frontend.py` mirrors the public artifacts to `build/data/` and schemas to `build/schemas/`. `step_publish_github.py` publishes those directories recursively with the site.

## Evidence states

Every public claim uses one of four categorical states:

- `verified`
- `officially_reported`
- `registry_reported`
- `not_established`

Numeric model confidence and internal matching scores remain curator-side information and are not used as a public ranking signal.

## Installation

```bash
NIH_TRACKER_PRODUCTION_ONLY=1 \
NIH_TRACKER_ENV=prod \
python scripts/step_initialize_environments.py \
  --source-env prod \
  --mode refresh-code \
  --release-root <release-root> \
  --apply \
  --confirm INSTALL_PRODUCTION_RELEASE
```

The installer updates only release-managed code, notebooks, schemas, templates, references, and documentation. It preserves cards, inputs, runs, snapshots, builds, caches, locks, and credentials.

## Operational boundary

- The catalog compiler is read-only and offline.
- External API and website refresh remains opt-in in the pipeline notebook.
- Model output remains a recommendation requiring named human approval.
- Existing locks, atomic card transactions, snapshots, rollback, optimistic concurrency, and sealed release provenance remain authoritative.
- A data-asset schema is included as an extension point; v19.0 does not bulk-create data-asset records.
