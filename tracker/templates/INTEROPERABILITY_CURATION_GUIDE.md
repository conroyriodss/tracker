# Interoperability curation input

Copy `interoperability_curated_template.csv` to `inputs/interoperability_curated.csv`. One row represents one active card. Do not add a row simply to suppress an evidence gap; add only source-supported content.

JSON columns accept either one object or a list of objects. Use exact URLs and include `source`, `tier`, and `as_of` where available. Every participant GUID entry must include `documentation_url` or `tool_url`. Do not infer a participant GUID from a repository PID system.

## API endpoint

```json
{"url":"https://example.org/api","family":"REST","documentation_url":"https://example.org/docs","access_class":"public","declared":true,"verified":false,"source":"official_resource_documentation","tier":"declared","as_of":"2026-09"}
```

`verified=true` should be used only when the endpoint was instrumentally tested or the applicable verification evidence is recorded. A documentation page alone normally supports `declared=true`.

## Documentation or relevant link

```json
{"label":"Developer documentation","type":"api_documentation","url":"https://example.org/developers","source":"official_resource_documentation","tier":"declared","as_of":"2026-09"}
```

Useful link types include `api_documentation`, `schema`, `data_dictionary`, `data_access`, `submission`, `bulk_download`, and `authentication`.

## Standard

```json
{"name":"GA4GH Data Repository Service (DRS)","version":"1.4","role":"data_access","url":"https://www.ga4gh.org/product/data-repository-service-drs/","declared":true,"verified":true,"source":"official_resource_documentation","tier":"verified","as_of":"2026-09"}
```

Use the standard’s official specification URL when available. When a repository merely declares support, retain `declared=true`, `verified=false`.

## Repository or item PID system

```json
{"name":"DOI","scope":"datasets","url":"https://www.doi.org/","source":"official_resource_documentation","tier":"declared","as_of":"2026-09"}
```

PID systems describe identifiers for datasets, studies, files, records, or the repository. They are not participant GUID systems.

## Participant GUID system

```json
{"name":"NDA Global Unique Identifier (GUID)","scope":"research_participant","format":"alphanumeric participant identifier","documentation_url":"https://nda.nih.gov/s/guid/nda-guid.html","tool_url":"https://nda.nih.gov/s/guid/nda-guid.html","privacy_characteristics":"De-identified subject identifier used to link participants across NDA datasets.","source":"official_resource_documentation","tier":"verified","as_of":"2026-09"}
```

A participant GUID entry requires exact resource identity and official documentation or tool evidence. Name similarity, a generic UUID field, or a re3data `pidSystem` value is insufficient.
