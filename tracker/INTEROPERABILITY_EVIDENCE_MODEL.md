# NIH Tracker v16 interoperability evidence model

## v16 identity-resolution addendum

Confirmed re3data evidence remains part of the interoperability and resource-profile layers. v16 improves how the confirmed record is found: the verifier retains re3data primary and alternate names, canonical homepage, RRIDs, and FAIRsharing DOIs; accepts reviewed exact seed mappings only after live verification; and distinguishes a populated `r3d...` identifier from a search-only URL placeholder. These changes affect source identity resolution, not the evidence tier of the re3data claims themselves.

## Design principles

1. Record claims at the smallest useful unit: endpoint, documentation page, standard, PID system, GUID system, or operational link.
2. Keep source, evidence tier, as-of date, and verification state with each claim.
3. Do not equate missing documentation with confirmed absence.
4. Keep participant-linkage identifiers separate from repository/item identifiers.
5. Separate descriptive resource-profile evidence from technical implementation evidence.
6. Permit exact automated enrichment, but require reviewed curation for ambiguous identity or capability claims.

## Canonical card structure

```yaml
blocks:
  interoperability:
    _meta:
      block_version: "2.0"
      as_of: YYYY-MM
      review_state: auto | needs_review | human_confirmed
      tier: curated | verified | registry | attestation | declared
      sources: []
    api_evidence_state: verified_documented | verified_undocumented | gated_reachable | declared_unverified | absent
    api_verified: false
    api_declared: false
    gated_reachable: false
    access_class: public | authenticated | controlled | gated | not_reachable | not_documented
    api:
      families: []
      endpoints: []
      documentation_links: []
    standards: []
    pid_systems: []
    guid_systems: []
    relevant_links: []
```

## API endpoints and documentation

```yaml
url: https://example.org/api
family: REST
version: v1
access_class: public
documentation_url: https://example.org/api/docs
declared: true
verified: true
source: probe:v4
tier: verified
as_of: YYYY-MM
```

`api_verified=true` means the applicable probe or exact source test succeeded. It does not mean every operation, authorization path, or response schema was exhaustively tested.

A website or FAIRsharing record may provide an API-documentation link without an endpoint URL. That remains useful declared evidence and is not converted into a verified endpoint.

## Standards

```yaml
name: OpenAPI Specification
category: api_description
url: https://spec.openapis.org/oas/latest.html
version: "3.1"
profile: optional resource-specific profile
role: API description / metadata exchange / submission package / identifier scheme
declared: true
verified: false
source: fairsharing:10.25504/FAIRsharing.example
tier: registry
as_of: YYYY-MM-DD
evidence_url: https://...
implementation_note: Resource-specific evidence, when documented.
```

The normalizer recognizes common API, exchange, packaging, and metadata standards, including OpenAPI, GraphQL, OAI-PMH, SPARQL, HL7 FHIR, GA4GH DRS/WES/TRS/Beacon, DCAT, DataCite Metadata Schema, Dublin Core, Schema.org, Bioschemas, RO-Crate, BagIt, and DATS. Unknown standards are retained.

A standard named by a registry remains registry evidence. Independent endpoint or documentation verification is tracked separately.

## Persistent identifier systems

`pid_systems` describes identifiers assigned to repository records, datasets, files, studies, or other deposited objects. Examples include DOI, accession, Handle, ARK, RRID, URN, UUID, and Identifiers.org URI.

```yaml
name: DOI
scope: repository_items
url: https://www.doi.org/
source: re3data:r3d...
tier: registry
```

## Participant GUID systems

`guid_systems` is reserved for privacy-preserving or cross-study identifiers assigned to research participants or comparable entities. It is never populated from a generic registry `pidSystem` field.

```yaml
name: FITBIR Global Unique Identifier (GUID)
scope: research_participant
format: random alphanumeric participant identifier
documentation_url: https://fitbir.nih.gov/content/global-unique-identifier
tool_url: https://fitbir.nih.gov/content/global-unique-identifier
privacy_characteristics: PII remains local; one-way hashes support matching.
source: official_resource_documentation
tier: verified
as_of: YYYY-MM
```

A participant GUID entry requires exact resource-specific documentation or reviewed curation.

## Relevant links

Relevant links may include repository homepage, API/developer documentation, schema/data dictionary, data-access/query interface, submission/deposition interface, bulk download, authentication documentation, support page, and registry record. Each link should carry label, type, URL, source, tier, and as-of date where available.

## Automatic sources

### Strict endpoint probe

Exact card mapping uses `csv_id`. The probe may provide API state, endpoint URLs, documentation URLs, families, access class, and standard flags. Boolean capability fields are not converted into URLs unless a valid source URL is present.

### FAIRsharing and website evidence

Exact linked FAIRsharing records and bounded homepage metadata may contribute declared API families, documentation links, and named standards. These records retain their registry/attestation tier and do not become technically verified solely because they are published.

### re3data

Only confirmed `r3d...` identifiers are fetched. Parsed fields include APIs, documentation, metadata standards, PID systems, repository identifiers, and registry link. Each run binds the exact cache and response hashes used.

### Curated input

`inputs/interoperability_curated.csv` and `inputs/resource_profile_curated.csv` are optional reviewed inputs. Curated claims should include an authoritative evidence URL and as-of date.

## Public reporting

The public spine exposes direct counts and names, not a composite score. The card detail panel exposes structured evidence and provenance. Search-only re3data links remain distinct from confirmed re3data identifiers. An empty section means not documented in the current card evidence set.
