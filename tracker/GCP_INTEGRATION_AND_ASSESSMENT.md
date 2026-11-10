# NIH Tracker v19.1: GCP integration and capability assessment

## Architecture decision

Extend the existing tracker. Its reviewed YAML cards and stable `repo_uid` values
remain authoritative. Preserve the offline catalog compiler and the existing
Review → Approve → Finalize process. Add GCP as an optional execution and storage
layer; do not replace identities with BMIC name/URL fingerprints or make a second
competing public catalog.

The reviewed input release is v19.0.2. Its ZIP SHA-256 is
`7993bd166b58bdb216659ea573489354e3b9b91b5b3e1c182dc75ab79314a3ce`.
The attached workbench matches the workbench inside that ZIP byte for byte.
No production cards, live credentials or running cloud project were supplied.

| Component | Role and authority |
|---|---|
| Existing Drive project | Cards, reviewed inputs, operational adjudications, transactions and publication controls |
| Gemini on GCP | Bounded candidate extraction and review of sealed identifier/crosswalk batches |
| Drive evidence cache | Local working copy with content-addressed raw bytes and parsed text |
| Private GCS bucket | Immutable source objects and explicitly archived sealed assessment/review runs |
| BigQuery | Rebuildable search/index table over independently captured documentation |
| Interactive Google Search grounding | User-facing research display; excluded from collection, model assessment and durable indexing |
| Existing catalog build | Offline compilation from reviewed cards; no automatic consumption of new model candidates |

The Gemini adapter incorporates the prior BMIC fix: `GCP_ID`, ADC, explicit
`gemini-3.5-flash`, 16,384 maximum output tokens and LOW thinking. It rejects
incomplete JSON, saves failed-call provenance, and performs no automatic retry.
The existing model and cloud access check does not establish extraction accuracy.

## What is implemented

- A **Gemini review** tab exports one sealed pending identifier/crosswalk batch,
  executes it on GCP using only supplied evidence, validates the full local
  response schema and identities, then imports it into the existing named-human
  approval queue. No card or operational adjudication is changed by execution.
- An **A–D evidence** tab freezes a plan before collection and assesses 22
  descriptive criteria. It retains quote-located candidates, explicit gaps,
  selected technical tests, raw sources, prompts and provider responses.
- An evidence-review panel saves named decisions outside immutable runs. These
  reviews do not become public claims or capability certifications automatically.
  Public integration continues through the existing curated interoperability and
  resource-profile inputs and guarded build/apply workflow. There is no automatic
  prose-to-card conversion.
- Conservative loading-stub detection, bounded PDF text extraction, exact quote
  validation and explicit missing-instrument states. No browser rendering or OCR
  is bundled. PDFs requiring either remain gaps, not negative findings.
- Reuse of linked re3data/FAIRsharing/website snapshots retained by the tracker.
  Imports verify original response hashes and retain original observation dates.
  A normalized registry extract without raw source bytes is not represented as a
  raw source snapshot. Cached records are selected by exact card links/homepages,
  not name similarity.
- Private GCS synchronization, generation-specific source references, immutable
  run archive receipts, local cache search, parameterized BigQuery search and
  restoration of selected source observations by GCS generation.

## Google Search and durable caching

Google Search grounding is not a general-purpose web crawler API. Current Google
Cloud service terms restrict caching and repurposing Grounded Results and Search
Suggestions, and explicitly prohibit collecting their links to build an index or
identify pages for crawling. The limited chat-history/display exceptions do not
authorize this project's evidence warehouse. Therefore the implementation keeps
that feature interactive and does not persist its output or use it as model
assessment input. Results and associated Search Suggestions are displayed
together for the initiating user. Notebook output saving is disabled in the
template; clear the research display before sharing/exporting a session.

Durable evidence comes from independently selected official pages, exact registry
records, schemas and approved public examples. Each admitted source must have
`cache_permission: public_cache_permitted`. This is an operator assertion about
source-specific permission, not a legal determination by the software. The
collector also honors robots rules and rejects `Cache-Control: private/no-store`.
Unknown permissions remain `review_required`; controlled datasets, credentials,
personal review records and search-grounded content are not cache inputs.

Source: https://cloud.google.com/terms/service-terms (Grounding with Google Search,
checked 10 November 2026). Applicable institutional agreements may differ; obtain
a documented exception before altering the boundary.

## Identifier and version model

| Object | Identifier/version |
|---|---|
| Resource | Existing tracker `repo_uid`; never assigned by Gemini |
| Source URL | `urn:nih-tracker:source:<SHA-256 of canonical URL>` |
| Raw object | SHA-256 of exact response bytes; one content-addressed object |
| Parsed text | Separate SHA-256 and parser version |
| Source snapshot | Hash of source ID, raw hash, parsed-text hash and parser version |
| Retrieval observation | Hash of the source record, including original observation time and resource binding |
| GCS object version | Object key + returned generation + content SHA-256 |
| Assessment | Unique run directory, plan SHA-256, rubric version, code hashes and resolved environment |
| Logical candidate | `claim_key` binds resource, criterion, proposed value, scope and source snapshot |
| Finding instance | `finding_id` binds the candidate/gap to one assessment plan |
| Model response cache | Exact prompt/schema, model ID, generation settings, code and environment fingerprint; bounded reuse age |
| Human review | Separate append-only receipt bound to finding ID and run-manifest hash |

These are internal content identifiers, not registered DOIs or institutional PID
guarantees. Original DOI/accession/registry identifiers remain separately scoped
source assertions. A model-proposed identifier is not authoritative merely because
its surrounding quotation exists. Candidates explicitly retain
`authoritative_identifier_validation: NOT_PERFORMED_BY_EXTRACTION`; authoritative
API/registry verification remains necessary before treating an identifier as
established. Exact selected registry-field checks are available as dated tests.

Default reuse ages are 30 days for source observations and 7 days for matching
model responses. Source reuse retains the original observation date; it is not a
new availability observation. Set the source age to zero for a fresh fetch. These
ages are refresh policies, not deletion schedules. Changing prompts, schemas,
generation settings, code or environment invalidates model reuse. A cache hit is
recorded and incurs no new model call. A model alias can change upstream; the
returned model version is retained, and cache-age limits bound intentional reuse.

GCS writes use `if_generation_match=0`. An existing object is accepted only after
its bytes match the expected SHA-256. Bucket versioning provides additional
recovery; content hashes and generation references provide actual version
identity. No retention lock or deletion lifecycle is activated by the code.
References:
https://cloud.google.com/storage/docs/generations-preconditions
https://cloud.google.com/storage/docs/object-versioning

## Initial Google Cloud setup

Use the same project that passed the Gemini test. An administrator can create a
private evidence bucket and dataset. Choose locations consistent with institutional
requirements and preferably near each other. The example uses the US multiregion;
it is not an assertion about an existing deployment.

```bash
export TRACKER_PROJECT='your-project-id'
export TRACKER_BUCKET='your-globally-unique-evidence-bucket'
gcloud services enable aiplatform.googleapis.com storage.googleapis.com bigquery.googleapis.com --project="$TRACKER_PROJECT"
gcloud storage buckets create "gs://$TRACKER_BUCKET" --project="$TRACKER_PROJECT" --location=US --uniform-bucket-level-access
gcloud storage buckets update "gs://$TRACKER_BUCKET" --public-access-prevention --versioning
bq --project_id="$TRACKER_PROJECT" --location=US mk --dataset "$TRACKER_PROJECT:nih_tracker_evidence"
```

The runtime needs model invocation permission, BigQuery job creation, dataset
read/write permission for its managed table, and private bucket object read/create
permission plus bucket metadata access. Common roles are Vertex AI User and
BigQuery Job User at the project, BigQuery Data Editor on the dedicated dataset,
and Storage Object User on the bucket, with `storage.buckets.get` separately if
not already granted. Dataset creation is a setup privilege; the cloud initializer
can create a missing dataset/table only when the operator has that permission.
Use narrower custom roles where required. No broad Owner or Editor grant is part
of the notebook. Storage administrators manage versioning and recovery policy.

References:
https://cloud.google.com/storage/docs/access-control/iam-roles
https://cloud.google.com/bigquery/docs/access-control
https://cloud.google.com/sdk/gcloud/reference/storage/buckets/update

## Installation and first pilot

1. Install this release ZIP with the existing maintenance notebook's **Refresh
   release code** action. The installer is extended to include the new modules,
   configuration template, documentation and requirements. It preserves cards,
   inputs, runs, caches, reviews and credentials. Restart/reopen the workbench.
2. Add `GCP_ID` to Colab Secrets and allow this notebook to read it. Keep
   `NIH_TRACKER_REVIEWER` for named review. Enable **Optional GCP tools**; the cell
   installs pinned direct dependencies and uses normal Google sign-in. No cloud
   task runs automatically when the panel opens.
3. In **Settings**, enter the bucket, dataset/location and collector contact.
   Select up to five existing `resource_uids`. Review source caching permissions
   before admitting URLs. Settings changes retain before/after history.
4. In **Cloud cache**, optionally reuse linked tracker snapshots. Inspect skipped
   rows; missing raw bytes/date/hash are precise provenance gaps. Test Gemini,
   GCS and BigQuery access, then initialize/check the index when authorized.
5. Preview the A–D plan. Enable collection for approved public sources and Gemini
   only when ready. With both unchecked the run inspects reusable cached sources
   and explicit technical examples without network or model calls.
6. Inspect `summary.json`, `events.json`, `sources.json`, `technical_tests.json`
   and the candidate inventory. Review evidence in the panel; do not equate zero
   candidates with capability absence. A fresh run is exploratory, not a
   preregistered evaluation of repository quality.
7. Explicitly synchronize approved source snapshots and archive the selected
   sealed run to GCS. Search the independent evidence cache through BigQuery.
8. For pending identifier/crosswalk queues, run one Gemini batch, refresh the
   original dashboard, approve/reject recommendations and use existing guarded
   finalization. Completed human decisions retain precedence.

The GCP section is optional. Existing manual and external file-exchange review
remain available. The main catalog/compiler still needs no network.

### Explicit source and technical-test configuration

This is a synthetic shape example, not a verified resource record. Replace every
example value with an approved source belonging to an existing tracker UID.

```json
{
  "resource_uids": ["R1"],
  "resources": {
    "R1": {
      "cache_permission": "public_cache_permitted",
      "allowed_hosts": ["docs.example.org"],
      "sources": [
        {"url": "https://docs.example.org/schema.json", "source_kind": "schema", "cache_permission": "public_cache_permitted"},
        {"url": "https://docs.example.org/public-example.json", "source_kind": "public_sample", "cache_permission": "public_cache_permitted"},
        {"url": "https://registry.example.org/records/exact-id", "source_kind": "registry_record", "cache_permission": "public_cache_permitted"}
      ],
      "schema_tests": [{"schema_url": "https://docs.example.org/schema.json", "instance_url": "https://docs.example.org/public-example.json"}],
      "registry_checks": [{"url": "https://registry.example.org/records/exact-id", "criterion_id": "B6", "json_pointer": "/data/id", "expected_value": "exact-id"}],
      "pid_examples": []
    }
  }
}
```

PID example URLs must also be included as admitted sources. Cross-host resolver
redirects require explicit `allowed_redirect_hosts` for that source. This avoids
automatic expansion from arbitrary model-supplied URLs. Registry JSON checks only
compare the specified record field; they do not resolve ambiguous resource names.
External JSON Schema references are not fetched implicitly: pin/bundle them before
testing. No patient-level or controlled sample data should be provided.

## Validation and limits

See `VALIDATION_REPORT.txt` for actual executed tests and unresolved environment
limits. Cloud SDK behavior, account permissions, live website completeness and
Gemini extraction accuracy require a bounded live pilot. No live cloud writes,
model calls, production-card edits or deployment were performed while preparing
this release. New assessment artifacts have not been generated from the user's
production cards because those cards were not attached.

The extension does not add a scheduler, browser renderer, OCR, repository-wide
schema validator, unrestricted web crawler, legal license interpreter, automatic
public assessment publication, or a validated maturity/FAIRness score. These are
distinct instruments or decisions, not capabilities implied by an LLM response.
