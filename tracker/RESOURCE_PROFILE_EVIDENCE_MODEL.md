# NIH Tracker v16 resource-profile evidence model

## Purpose

`blocks.resource_profile` records descriptive and stewardship evidence about a tracked data-sharing resource. It answers:

- What kind of resource is this?
- What kinds of data or other digital assets does it hold or serve?
- How does an authoritative source briefly describe it?
- Which organizations are publicly associated with maintaining, providing, hosting, publishing, owning, funding, or sponsoring it?
- Which subject areas, domains, formats, taxonomies, languages, service roles, and access conditions are documented?

It does not independently prove API behavior, standards conformance, service availability, repository quality, or physical data residency. Those questions require separate evidence.

## v16 policy-context separation

Resource descriptions, data types, subjects, formats, and organizational roles remain in `blocks.resource_profile`. NIH CADR listing and dbGaP EDS context are recorded separately in `blocks.policy_context`. A CADR or EDS designation is not converted into a resource type, stewardship role, hosting-location claim, or quality/completeness score.

## Canonical card structure

```yaml
blocks:
  resource_profile:
    _meta:
      block_version: "1.0"
      as_of: YYYY-MM
      review_state: auto | needs_review | human_confirmed
      tier: curated | verified | registry | attestation | declared
      sources: []
    resource_types: []
    data_asset_types: []
    descriptions: []
    organizations: []
    subject_areas: []
    data_formats: []
    taxonomies: []
    keywords: []
    languages: []
    service_roles: []
    access_conditions: []
    relevant_links: []
    maintainer_status: {}
```

## Resource types versus data-asset types

A resource type describes the service or organizational object:

```yaml
name: Data repository
scheme: FAIRsharing record type
source: fairsharing:10.25504/FAIRsharing.example
 tier: registry
```

A data-asset type describes what is held, indexed, or served:

```yaml
name: Genomic data
scheme: re3data content type
source: re3data:r3d...
tier: registry
```

One card may have multiple values. A portal can also be a catalog and serve datasets, software, models, protocols, or biospecimens.

## Descriptions

A description record contains the exact normalized source statement, not an LLM-written synthesis:

```yaml
text: Brief source description.
source_url: https://example.org/
source: website:<sha256>
tier: attestation
as_of: YYYY-MM
extraction_method: meta:description
content_sha256: <sha256>
```

Description precedence for public display is:

1. reviewed curation;
2. website JSON-LD;
3. website metadata;
4. exact FAIRsharing record;
5. exact re3data record; and
6. controlled main-content paragraph fallback.

All retained descriptions remain available in the card even when one is selected for compact display.

## Organizations and stewardship

```yaml
name: Example Institute
roles:
  - maintainer
  - provider
country: US
url: https://example.edu/
ror: https://ror.org/...
rrid: RRID:SCR_...
source: re3data:r3d...
tier: registry
as_of: YYYY-MM-DD
```

Equivalent organization records merge their roles and sources. A country is a property of the documented organization record; it is not treated as a physical storage location. Personal contacts and email addresses are excluded from the public profile.

`maintainer_status` is a source assertion about whether the record/resource is reported as maintained. It is not a service-uptime test.

## Subjects, taxonomies, keywords, and formats

Each term may preserve a source vocabulary:

```yaml
name: Life Sciences
scheme: DFG Classification of Subject Areas
identifier: "1"
url: https://...
source: re3data:r3d...
tier: registry
```

Unknown or source-specific values are retained. The normalizer does not silently map every source taxonomy into a single NIH-wide ontology.

Data formats may include file/media formats, metadata formats, schemas, and registry-described content types. Their category or source scheme should identify the intended meaning.

## Automatic sources

### FAIRsharing

Only an exact FAIRsharing DOI already linked to the card is eligible. Parsed evidence may include record type, object types, description, subjects/domains, taxonomies, organizations and relationship roles, maintained status, access methods/conditions, support links, and linked standards/models/formats/terminologies.

### re3data

Only a confirmed `r3d...` identifier already linked to the card is eligible. Parsed evidence may include repository/content types, description, subjects, institutions and responsibility roles, country/ROR/RRID, languages, access conditions, service roles, APIs, standards, PID systems, and the registry record link.

### Website

Structured extraction is bounded to metadata tags, JSON-LD, explicit link relations, and a controlled main-content paragraph fallback. Footer, navigation, forms, arbitrary person names, and email addresses are not used to create stewardship claims.

### Curated input

`inputs/resource_profile_curated.csv` is optional and human-reviewed. Its JSON fields preserve structured evidence items. Each curated claim should identify an authoritative source and as-of date.

## Relationship to interoperability evidence

Registry or website records may name a standard or API documentation page. Such evidence is merged into `blocks.interoperability` as declared/registry evidence. It does not become `verified=true` unless an applicable technical or source-verification rule supports that status.

## Gaps

The pipeline emits named evidence gaps such as:

- description missing;
- resource type missing;
- data-asset type missing;
- stewardship not documented;
- subject areas missing;
- data formats missing; and
- interoperability standard details missing.

A gap means the current frozen evidence set does not support the field. It is not proof of absence and is not aggregated into a score.
