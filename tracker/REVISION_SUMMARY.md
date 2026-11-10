# NIH Tracker v19.0.2 revision summary

## v19.0.2 workflow correction

- Made operational human adjudications authoritative over later provider recommendations for the same stable review key.
- Added a count of recommendation items already covered by current decisions.
- Replaced empty review dropdowns with a state-aware next-action panel.
- Preserved explicit manual reopening for genuine reconsideration.

## Data architecture

- Added a deterministic card-to-graph compiler.
- Added Resource, Organization, NIH Organizational Unit, Access System, API Service, Standard, Identifier Scheme, GUID System, Policy Designation, Publisher Policy, Evidence Source, and external-identifier nodes.
- Added typed edges such as `OPERATED_BY`, `EXPOSES_API`, `IMPLEMENTS_STANDARD`, `SUPPORTS_IDENTIFIER_SCHEME`, `SUPPORTS_GUID_SYSTEM`, `HAS_POLICY_DESIGNATION`, and publisher-policy relationships.
- Added evidence-bearing claim records with source, dates, SHA-256 binding, resolution status, review due date, and limitations.
- Restricted identity equivalence to reviewed identity evidence; related entities use typed relationships.

## Public product

- Replaced field-first browsing with four task pathways.
- Added faceted resource discovery, including organism/model-system scope and cautious human-data access context, explainable fit classes, no-match guidance, and comparison.
- Added progressive disclosure and an evidence dossier.
- Added policy-safe public terminology, explicit unknown states, methods, scope, glossary, and limitations.
- Added a descriptive infrastructure observatory without a composite score.

## Machine-readable release

- Added JSON, JSONL, CSV, JSON-LD, controlled-vocabulary, schema, change-history, coverage, and freshness exports.
- Added a sealed catalog manifest and recursive GitHub Pages publication.
- Added a migration audit mapping every card UID to one Resource node.

## Workflow

- Added `step_build_catalog.py` between spine generation and frontend generation.
- Kept the catalog compiler offline and read-only.
- Prioritized human approval before review finalization.
- Preserved the production-only installer, guided review, transaction safety, and opt-in live evidence refresh.

## Extension points

- Added a data-asset JSON Schema and template.
- Added first-class API and GUID node models.
- Reserved Award, Publication, and DataAsset graph types for later bounded pilots; v19.0 does not populate them at scale.
