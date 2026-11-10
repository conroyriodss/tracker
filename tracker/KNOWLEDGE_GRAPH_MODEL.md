# NIH Tracker v19.0 knowledge-graph model

## Authority model

Reviewed YAML cards and pinned project snapshots remain authoritative. The graph is regenerated and never edited directly.

```text
cards/*.yaml + inputs/reference snapshots -> graph compiler -> static release artifacts
```

## Node types

| Type | Meaning |
|---|---|
| `Resource` | Repository, archive, portal, registry, knowledge base, catalog, cloud environment, biobank, model resource, or access service |
| `Organization` | Operator, maintainer, host, owner, funder, publisher, or data-access authority |
| `NIHOrganizationalUnit` | NIH Institute, Center, Office, or program |
| `AccessSystem` | Named controlled-access or request-management system |
| `APIService` | Programmatic service or API documentation object |
| `Standard` | Metadata, exchange, semantic, workflow, or file-format specification |
| `IdentifierScheme` | DOI, accession, Handle, ARK, RRID, or another object identifier scheme |
| `GUIDSystem` | Participant GUID or privacy-preserving record-linkage system |
| `PolicyDesignation` | NIH CADR or another documented designation |
| `PublisherPolicy` | Publisher or journal-family repository-policy statement |
| `EvidenceSource` | First-party page, NIH document, registry record, snapshot, or API response |
| `ExternalIdentifier` | A specific re3data, FAIRsharing, RRID, or related identifier record |
| `DataAsset` | Reserved extension for a later bounded dataset/study/collection pilot |

## Relationship types

Examples include:

```text
OPERATED_BY
MAINTAINED_BY
HOSTED_BY
CURRENTLY_FUNDED_BY
HISTORICALLY_SUPPORTED_BY
ASSOCIATED_WITH_ICO
HAS_ACCESS_SYSTEM
EXPOSES_API
IMPLEMENTS_STANDARD
SUPPORTS_IDENTIFIER_SCHEME
SUPPORTS_GUID_SYSTEM
HAS_POLICY_DESIGNATION
MENTIONED_BY_PUBLISHER_POLICY
RECOMMENDED_BY_PUBLISHER_POLICY
REQUIRES_DEPOSITION_BY_PUBLISHER_POLICY
COMPONENT_OF
CATALOGS
MIGRATED_TO
SUPERSEDES
SAME_AS
```

`SAME_AS` is reserved for reviewed identity evidence. Related programs, components, portals, catalogs, and access systems use typed non-identity relationships.

## Claims

A claim binds a subject and predicate to either an object node or a literal value and includes:

```text
claim ID
source node
source URL
source date
observation date
review date
next review due
source SHA-256
method
evidence state
resolution status
limitations
```

Public evidence states are categorical:

```text
verified
officially_reported
registry_reported
not_established
```

Resolution states distinguish selected, supporting, conflicting, superseded, and not-established claims. Conflicting source assertions can therefore be preserved without silently overwriting one another.

## Stable identifiers

Node, edge, and claim identifiers are deterministic hashes of normalized identity components. The compiler sorts all records and serializes canonical JSON to support byte-identical repeated builds.

## Public resource views

The graph resolver creates one concise resource view per current card. The view contains decision-relevant fields, facets, API/PID/GUID/standards summaries, policy facts, evidence dates, and source-claim IDs. Organism/model-system scope is coarsely inferred from source-bound taxonomy, BMIC model-system, and descriptive terms. Human-data context then combines that scope with the resource-level access pathway; it never asserts asset-level consent, de-identification, privacy, or legal status. The public view is a projection of the graph, not an independent record.

## Data assets

The included DataAsset schema separates asset-level properties from repository capabilities. For example:

- a repository may support DICOM, while a specific asset actually uses DICOM;
- a repository may support a participant GUID system, while a specific study requires it;
- a repository may offer DOI assignment, while an asset holds one DOI and version;
- a repository may offer controlled access, while an asset has a specific consent/DUO condition.

v19.0 defines this extension but does not bulk-populate it.
