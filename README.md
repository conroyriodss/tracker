# Resource Tracker

A published resource catalog with structured records, relationships, supporting claims, and provenance manifests.

This repository helps researchers, data-resource users, and program staff inspect the catalog and trace its contents to the available evidence. Developers can use the published machine-readable files and schemas to examine the representation.

**Repository role: source versioning, publication and inspection.** The source distribution, Colab notebooks and tests now live under [`tracker/`](tracker/), alongside the published catalog under `docs/`. Live reviewed YAML cards, operational inputs, run history and credentials remain outside this repository. A complete source-to-publication rebuild still requires those operational inputs.

## Start here

* Browse the [published site](https://conroyriodss.github.io/tracker/).
* Inspect the [catalog manifest](docs/data/manifest.json) for the snapshot date, catalog identifier, counts, artifact inventory, and SHA-256 values.
* Read the [publication manifest](docs/publication_manifest.json) for the published payload inventory and publication metadata.
* Explore the [data files](docs/data/) and [schemas](docs/schemas/).

## What is included

|Path|Purpose|
|-|-|
|`docs/index.html`|Published browser application.|
|`docs/data/resources.csv`|Tabular resource export.|
|`docs/data/resources.json` and `resources.jsonl`|Structured resource exports.|
|`docs/data/nodes.jsonl` and `edges.jsonl`|Catalog entities and relationships.|
|`docs/data/claims.jsonl`|Claim records for evidence inspection.|
|`docs/data/catalog.jsonld`|JSON-LD catalog representation.|
|`docs/data/field\_coverage.csv` and `relationship\_coverage.csv`|Coverage reports.|
|`docs/data/source\_freshness.csv`|Source-freshness report.|
|`docs/data/vocabularies.json`|Published vocabularies.|
|`docs/data/change\_history.jsonl`|Change-event export; check the manifest for whether any events are present.|
|`docs/schemas/`|Schemas for published record types.|

## Interpret the catalog

Use the manifest’s `catalog\_as\_of` field to identify the catalog snapshot. Do not assume it is the date every source was checked. The manifest also records the number of claims using a compilation-date fallback; distinguish that fallback from an observed source date.

Resource coverage, metadata completeness, and evidence strength answer different questions. Missing fields or links can reflect collection and representation limits. They do not establish that a resource lacks a capability or that an activity did not occur. The catalog manifest identifies this as a derived catalog and does not define a composite score.

## Inspect and cite a snapshot

1. Select a Git commit and use its permalink when citing repository content.
2. Save the catalog and publication manifests with the files used in your analysis.
3. Record the `catalog\_id`, `catalog\_as\_of`, `input\_fingerprint`, and code/publication commit separately.
4. Compare the SHA-256 of each downloaded artifact with the corresponding manifest entry. Data files are under `docs/data/`; schema entries correspond to files under `docs/schemas/`.
5. Report which files were actually checked. A matching digest establishes byte agreement with the recorded digest; it does not establish completeness or correctness of the underlying claims.

A fresh rebuild additionally requires the source cards, pinned input snapshots, vocabulary source and execution environment. Generation code and notebook instructions are in [`tracker/README.md`](tracker/README.md); inventory, cleanup, source pinning and GitHub practices are in [`tracker/OPERATIONS_AND_VERSIONING.md`](tracker/OPERATIONS_AND_VERSIONING.md). The [exploratory coverage example](tracker/examples/coverage_20260929/) uses a hash-checked historical catalog export and does not measure resource FAIRness.

## Corrections and reuse

Open an [issue](https://github.com/conroyriodss/tracker/issues) with the affected resource or claim, the commit/catalog identifier, the proposed correction, and a public source that supports it. Do not include credentials or nonpublic records.

This repository does not currently contain a project-wide LICENSE file. Project-code terms and upstream data terms need to be established separately; public visibility alone should not be treated as a license statement.

