# Resource-profile curation input

Copy `resource_profile_curated_template.csv` to `inputs/resource_profile_curated.csv`. One row represents one active card. Use this file only when the authoritative website or exact registry record is incomplete, ambiguous, or requires a reviewed correction.

Every JSON column accepts either one object or a list of objects. Preserve `source`, `tier`, and `as_of` for every claim. Use `tier: curated` for a human-reviewed assertion and retain the exact evidence URL in the object when possible.

## Brief description

```json
{"text":"The repository archives ...","source_url":"https://example.org/about","source":"official_resource_documentation","tier":"curated","as_of":"2026-09"}
```

Descriptions should be concise, factual, and attributable. Do not synthesize a description from an LLM or combine unrelated sources without recording that analysis separately.

## Resource and data-asset types

```json
[{"name":"Data repository","scheme":"NIH Tracker resource type","source":"official_resource_documentation","tier":"curated","as_of":"2026-09"}]
```

```json
[{"name":"Genomic data","scheme":"NIH Tracker data asset type","source":"official_resource_documentation","tier":"curated","as_of":"2026-09"}]
```

A resource type describes the service, such as repository, archive, knowledgebase, data catalog, or portal. A data-asset type describes what the service stores or serves, such as clinical, genomic, imaging, phenotypic, survey, software, workflow, or ontology assets.

## Organizations and stewardship

```json
[{"name":"Example University","roles":["provider","responsible institution"],"url":"https://example.edu/","ror":"https://ror.org/012345678","country":"United States","source":"official_resource_documentation","tier":"curated","as_of":"2026-09"}]
```

Record only publicly documented organizational responsibility. An organization address or registry country is not, by itself, evidence of physical data residency. Do not enter personal email addresses or private maintainer account details.

## Subjects, formats, taxonomies, and access conditions

```json
[{"name":"Neuroscience","scheme":"MeSH","identifier":"D009488","url":"https://id.nlm.nih.gov/mesh/D009488.html","source":"official_resource_documentation","tier":"curated","as_of":"2026-09"}]
```

```json
[{"name":"JSON-LD","scheme":"media type","identifier":"application/ld+json","source":"official_resource_documentation","tier":"curated","as_of":"2026-09"}]
```

Use controlled terms and identifiers where the source supplies them. Do not infer a subject or format from a repository name alone.

## Interoperability details

The `standards_json` and `api_documentation_links_json` columns augment the canonical interoperability block.

```json
[{"name":"GA4GH Data Repository Service (DRS)","version":"1.4","role":"dataset object retrieval","url":"https://www.ga4gh.org/product/data-repository-service-drs/","evidence_url":"https://example.org/developers/drs","implementation_note":"Repository documents a DRS endpoint for dataset objects.","declared":true,"verified":false,"source":"official_resource_documentation","tier":"curated","as_of":"2026-09"}]
```

Keep `declared` and `verified` distinct. A documentation page supports a declaration; instrumental endpoint testing is required for a verified capability.
