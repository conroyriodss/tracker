# GCP-assisted review of the existing candidate queues (v19.1.1)

This release removes the manual-only limitation on card/name candidates and
adds a resumable plan for the current queues. It preserves the existing named
human approval, operational adjudication, transaction and finalization steps.
It does not connect to or change your production project during installation.

## Recommended division of work

| Stage | Implementation | What it establishes |
|---|---|---|
| Retrieve evidence | Exact-label lookup in a selected registry CSV; permitted source-cache lookup by resource UID; existing registry evidence | Which captured records and excerpts the models actually received |
| First assessment | Your configured Gemini model, default `gemini-3.5-flash` | Candidate ACCEPT, REJECT or DEFER recommendations |
| Independent discriminator, optional | Claude on GCP, default `claude-sonnet-5-5` | A second assessment of the same packet without seeing Gemini's answer |
| Mechanical checks | Queue/card/snapshot hashes, complete response coverage, allowed UIDs, valid evidence paths and decision schema | Binding and structural consistency, not factual truth |
| Human review | Existing recommendation panel, then Validate and Apply all | Named decisions and controlled changes to reviewed cards |

The second model is an independent call from a different provider family, not
an independent evidentiary source. Shared training data, shared source omissions
and common reasoning errors remain possible. Agreement is a triage signal.
Neither model's self-reported confidence is a calibrated error probability.

## Use with the reported 132-card plus one-identifier backlog

1. Install `nih_tracker_v19_1_1_code_and_tests.zip` with the maintenance
   notebook's existing **Refresh release code** operation. Reopen the supplied
   v19.1.1 review notebook and run its setup cells. Existing inputs, cards,
   adjudications, runs and caches are preserved by the release installer.
2. Keep `GCP_ID` in Colab Secrets with notebook access enabled. In the optional
   GCP cell near the end of the notebook, set `ENABLE_GCP_TOOLS = True` and run
   it. Google sign-in provides ADC. The existing reviewer identity is still
   required to approve recommendations. No service-account key is needed.
3. Open **Gemini review**. Refresh candidate queues. Card/name rows now appear
   alongside identifier and crosswalk rows. The dashboard no longer labels the
   card/name family manual-only.
4. If appropriate, select the registry CSV already used by this project. The
   field is optional and accepts a filename directly inside `inputs/`. The
   panel proposes `final_ultimate_enhanced_repos.csv` only when that file exists.
   Exact-label matches are included as candidate evidence; ambiguous duplicate
   labels do not establish identity. No current production rows were supplied
   with this release, so their evidentiary sufficiency has not been evaluated.
5. Start with **Row cap = 10**, leave the optional Claude reviewer unchecked,
   and click **Preview queue plan**, then **Freeze queue plan**. These actions
   make no model calls. The plan records source hashes, code hashes, model
   configuration, selected review keys and the maximum request count.
6. Set **Calls/click = 2** and click **Run / resume GCP review**. This is billable.
   It starts at most two model requests, each with at most five candidate rows
   by default. It imports complete, validated recommendations and stops after
   a failed request. The project has a shared execution lock.
7. Inspect the source evidence and recommendation quality on the pilot. To
   cover the full reported backlog, refresh queues, set **Row cap = 150**, and
   freeze another plan. Exact current recommendations are excluded, so the
   pilot is not needlessly repeated. With no prior recommendations, 132 card
   rows plus one identifier require 28 five-item-or-smaller batches; two
   providers require at most 56 first-pass requests. These are request bounds,
   not a dollar estimate. Retries and access smoke tests are additional calls.
8. Resume the selected saved plan as needed. Successful imports are skipped
   independently of the model-cache TTL. Complete sealed responses can recover
   an interrupted import without another model call. Failed or interrupted
   calls are not automatically resubmitted: explicitly check **Retry
   failed/interrupted batches** after inspecting the failure. An interrupted
   provider call may be billed again on retry. A stale lock requires the
   existing deliberate maintenance workflow; it is not broken automatically.
9. Open recommendations in the existing review dashboard. Examine agreement,
   disagreements and deferrals; check only the recommendations you approve and
   save named human approvals. Then use **Validate** followed by **Apply all**.
   Finish model drafting and human approvals for a plan before applying card
   changes. Otherwise, source-bound recommendations referring to changed cards
   become stale and need a fresh plan and approval.

For an approved card/name ACCEPT, the exact original candidate label is added
as an alias of the existing proposed UID through the existing ACCEPT_ALIAS
transaction. The model cannot choose another UID, create a card, invent an
alias, rename the canonical resource or merge records. REJECT records rejection
of the candidate. DEFER states an evidence gap and leaves a card/name or
identifier candidate unresolved; it does not silently mark review complete.
The existing separate handling of reviewed crosswalk deferrals is unchanged.

## Enable the optional Claude discriminator

Google documents Claude Sonnet 5.5 as a generally available managed model with
ID `claude-sonnet-5-5`. Project eligibility, IAM and organizational restrictions
must be checked in your own Google Cloud project. This release has not exercised
your project's Claude endpoint.

1. In Google Cloud Model Garden, select the Claude model and enable access for
   the project named by `GCP_ID`, following the model card's partner terms and
   access controls. Gemini access alone does not establish Claude access.
2. Have the project/organization administrator allow the model where model
   restrictions apply. This adapter uses structured outputs; Google's current
   documentation says the `structured_outputs` feature must be allowed through
   `constraints/vertexai.allowedPartnerModelFeatures`.
3. In Settings, use `claude_model` and `claude_location` to select the exact model
   ID and supported endpoint. Defaults are `claude-sonnet-5-5` and `global`.
   A global endpoint does not impose US-only processing. If your project
   requires a particular geography, select an approved supported endpoint
   before testing. No Anthropic API key is used.
4. Click **Test Claude access**. This makes a small structured-output request.
   If it succeeds, check **Add independent Claude review** before freezing a
   plan. Existing plans retain their frozen provider selection.

The two providers use the same item hashes and evidence excerpts. Their prompts
contain no other model recommendation and no human approval records. Both
responses are imported separately. Disagreements remain visible in the existing
consensus panel. Different Gemini models are still one provider family; they
cannot manufacture independent-provider agreement.

The adapter preserves the requested and returned model IDs, prompt, response,
token metadata when returned, environment and source hashes. It rejects
truncated/invalid JSON without a repair request. The Sonnet 5.5-specific
`between_tools` setting is sent only for that exact model ID. Changing to
another supported Claude model requires a new smoke test and frozen plan.

## Search, retrieval and the durable cache

The interactive **Web research** tab still offers Gemini with Google Search.
Those generated answers and search suggestions are not inserted into the
evidence store or used as automated crawler seeds. A web summary is not a source
record and is not passed to the discriminator as proof.

The automated queue path retrieves evidence that can be retained: existing
registry snapshots and explicitly permitted public-source captures. By default
it includes up to three recent parsed source captures per proposed resource UID,
each limited to 8,000 supplied characters. The original fetch date, URL,
observation ID, snapshot ID, raw/text hashes, excerpt hash, truncation flag and
identity-binding limitation travel with each excerpt. The source-cache TTL is
honored. A UID association in the cache is a retrieval aid, not identity proof.
Set `review_use_source_cache` to false to omit this lookup.

No live broad-web search or new source crawling is performed by queue execution.
When evidence is missing, collect approved independent sources through the
existing evidence tools or add the appropriate authoritative snapshot, then
freeze a new plan. BigQuery remains available for searching the captured-source
index; this release does not claim a new broad-web search service or semantic
retrieval benchmark.

Queue plans live under `runs/gcp_review_queue/`; immutable model runs under
`runs/gcp_review/`; append-only, content-hashed execution receipts under
`reviews/gcp_queue_execution/`; and response reuse records under
`cache/gcp_review/`. The plan contains copied sealed batches. Human approvals
and operational adjudications stay in their established files.

**Archive queue plan/results to GCS** explicitly archives the selected sealed
plan and its latest receipted model runs using the existing private,
create-only, generation-recording GCS archive. It does not upload human approval
files or Google-grounded answers. BigQuery indexes independent source evidence;
model recommendations remain in the review inventory rather than becoming
source evidence. Gemini review alone does not require a bucket or BigQuery.

## Validation boundary and evaluation before wider use

Local synthetic tests exercise source binding, bounded/resumable requests,
failures, interrupted imports, human precedence, separate provider results,
cached-source provenance and the actual existing alias transaction. They do not
measure recommendation correctness. See `VALIDATION_REPORT.txt` for exact
executed counts, blocked legacy tests and absence of live GCP validation.

Before scaling, freeze the protocol and a separate set of representative matches,
nonmatches, ambiguous parent/component cases and missing-evidence cases. Have
human reviewers adjudicate without seeing model judgments, then compare the
model outputs afterward. Measure false acceptance, rejection and deferral
separately. Do not tune prompts on that held-out set or infer accuracy from
agreement between models. No accuracy result or validated acceptance threshold
is supplied with this release.

## Official documentation checked November 10, 2026

- [Claude models on Google Cloud](https://docs.cloud.google.com/gemini-enterprise-agent-platform/models/partner-models/claude)
- [Claude Sonnet 5.5 model ID and endpoints](https://docs.cloud.google.com/gemini-enterprise-agent-platform/models/partner-models/claude/sonnet-5-5)
- [Claude structured outputs and organization policy](https://docs.cloud.google.com/gemini-enterprise-agent-platform/models/partner-models/claude/structured-outputs)
- [Managed partner models and endpoint selection](https://docs.cloud.google.com/gemini-enterprise-agent-platform/models/partner-models/use-partner-models)

These sources establish documented capabilities. They do not establish that a
particular model is enabled in your project or that it performs adequately on
NIH Tracker identity decisions.
