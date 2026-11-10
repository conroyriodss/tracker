# A–D assessment: automation boundary

**Automate evidence collection, candidate extraction and narrow reproducible tests.**
Use human review for entity scope, applicability, policy interpretation and claims
that exceed the tested examples. No composite score or repository ranking is
implemented. All 22 criteria are descriptive and versioned in
`reference/capability_assessment_rubric_v1.json`.

The submitted item D ends after access/reuse terms for data, models and code.
This release covers that text and documented interfaces from the heading. It does
not assume additional omitted requirements or treat them as assessed.

| ID | Criterion | Implemented automation | Human decision or additional instrument |
|---|---|---|---|
| A1 | Identifier systems | Extract quoted namespaces, issuers, object scope and examples | Verify exact DOI/accession against its authoritative service; distinguish repository, collection, item and participant IDs |
| A2 | Resolution | HTTP result for explicitly selected PID URLs, with original observation date and snapshot | Confirm resolved identity; expand to a predefined representative sample before generalizing |
| A3 | Version identity | Extract version-specific IDs, relations, releases and tombstone documentation | Verify that versions are immutable and relationships are implemented as claimed |
| A4 | Persistence | Extract preservation, withdrawal and succession commitments | A present-day response or policy cannot establish future persistence |
| A5 | Collection identifiers | Extract explicitly scoped collection-level PID statements | Confirm collection granularity, membership and relationship to deposited objects |
| A6 | Citation | Extract actual citation examples and documented export formats | Verify identifier, version and attribution accuracy in the authoritative record |
| B1 | Operations and maintenance | Extract dated published release, maintenance and operational practices | Assess whether practice follows policy; longitudinal operational measurements are a separate instrument |
| B2 | Contacts | Identify documented role-based support/operational routes | Test responsiveness through an authorized process; do not infer staffing from a contact page |
| B3 | Support | Extract stated funding and responsible organizations with source dates | Verify awards/support period against the authoritative funding record; assess sustainability separately |
| B4 | Policies | Extract relevant policy statements, scope, dates and versions | Determine sufficiency, conflicts, current applicability and implementation |
| B5 | RRID | Reuse existing tracker identity workflow; extract exact record claims; explicit registry JSON field comparison where configured | Adjudicate resource versus parent/component identity; listing is not certification |
| B6 | FAIRsharing | Reuse linked retained snapshots; extract record/type claims; exact selected JSON field comparison | Confirm exact resource and record scope; avoid mistaking a linked standard for a resource registration |
| B7 | NLM Dataset Catalog | Extract documented dataset coverage; exact selected catalog field comparison | Decide applicability and coverage unit; dataset inclusion differs from repository registration |
| C1 | Schemas, formats and versions | Extract declarations; validate explicitly selected public JSON instance against a pinned JSON Schema | Select appropriate profiles and representative samples; unsupported formats and unpinned remote references remain instrument gaps |
| C2 | Dictionaries, units and values | Extract documented definitions, units, datatypes and value sets | Test actual data against dictionaries and review scientific meaning; extraction alone does not establish conformance |
| C3 | Provenance | Extract documented lineage, transformation and source/release provenance | Verify end-to-end implementation and completeness on defined examples |
| C4 | Terminologies and CDEs | Extract exact references, versions, bindings and local extensions | Resolve references against authoritative terminology/CDE services and assess binding quality |
| C5 | Mappings and ambiguities | Extract crosswalks, direction, version, declared loss and ambiguity | Evaluate semantic equivalence, coverage and errors on a preregistered test set |
| D1 | Data access and reuse | Extract data-specific access conditions, license references and restrictions | Resolve license/consent/DUA applicability and contradictions; no automated legal conclusion |
| D2 | Model access and reuse | Extract model/weights/output terms separately from data terms | Establish model version, intended use and interaction among license/use restrictions |
| D3 | Code access and reuse | Extract code version, source availability and software-license statements | Confirm the actual repository/version/license and applicable dependencies |
| D4 | Interfaces | Extract documented APIs, downloads, submission routes, authentication, versions and rate limits | Run protocol-specific/authenticated conformance tests in an authorized environment; HTTP reachability alone is insufficient |

## Result semantics

`CANDIDATE_QUOTE_LOCATED` means the quotation appears in the exact captured text
supplied to the model. It does not mean that the candidate is true, applicable,
complete, technically implemented or independently verified. The public catalog's
existing evidence states are not assigned by this extractor.

`NOT_OBSERVED_IN_SOURCES` is used only when extraction completed without a
supporting candidate in the bounded source set. `NOT_ASSESSED` applies when
extraction did not occur or complete. Named gaps distinguish missing data,
missing analysis and missing instruments. A blocked source, missing parser,
missing OCR or missing browser rendering does not imply capability absence.

Technical results are separate: `OBSERVED_HTTP_SUCCESS`, `TEST_PASSED`,
`TEST_FAILED`, `EXACT_RECORD_VALUE_MATCH`, `RECORD_VALUE_MISMATCH` and
`NOT_ASSESSED`. They name the selected target and source snapshots. A failing
sample is not generalized to an entire repository; a passing sample is not a
certification. A test can succeed while a broader descriptive criterion remains
unassessed. Technical results appear alongside the relevant evidence row.

A named reviewer can accept a documented claim, reject it, defer it or mark a
criterion not applicable with a rationale. An evidence gap cannot be accepted as
a documented capability. Human decisions are stored separately from model
inputs, and automatic extraction cannot change them.

## Registry distinction

NLM describes the Dataset Catalog as a dataset discovery service drawing from
multiple repositories. Its documented public API is relevant to future bounded
coverage checks. This release does not invent an API endpoint or infer complete
coverage from a website search. Exact catalog records must be independently
selected and mapped to the relevant dataset/resource before a claim is established.

Official sources checked 10 November 2026:
https://datasetcatalog.nlm.nih.gov/
https://www.nlm.nih.gov/pubs/techbull/nd26/nd26_discoverwhr.html
https://www.nlm.nih.gov/pubs/techbull/ja25/ja25_reimagined_nlm_dataset_catalog.html

## Validation design before comparative use

Freeze the rubric version, object types, inclusion rules, source cutoff, sample
selection and hypotheses before scoring or comparing resources. Select a stratified
pilot across repository/knowledgebase types, data access models and documentation
formats. Keep independent expert judgments outside the model prompts. Evaluate
claim precision, source/identity attribution, omitted supported claims, handling
of contradictory evidence and missingness, and technical-test scope against
human-adjudicated records. Report these dimensions separately. No acceptance
thresholds, performance statistics, expert rankings or validated findings are
claimed by this release.
