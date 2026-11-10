# NIH Tracker policy-context evidence model

## Scope

`blocks.policy_context` records dated, source-bound policy and registry context. It is descriptive. It is not a compliance determination, endorsement, repository-quality rating, or completeness score.

The model keeps three evidence classes separate:

1. official NIH CADR list membership;
2. dbGaP External Data Source context; and
3. textual association in a pinned public dbGaP study export.

## Canonical structure

```yaml
blocks:
  policy_context:
    _meta:
      block_version: "1.0"
      as_of: YYYY-MM-DD
      tier: policy
      sources: []
    nih_cadr:
      status: listed | not_listed | not_evaluated
      listed_name: ""
      access_system: ""
      nih_designated_genomic_data_repository: true | false | null
      url: ""
      source_url: ""
      source_page_last_updated: ""
      source_sha256: ""
      match_basis: reference_uid | unique_homepage_host | not_in_snapshot
    dbgap_external_data_source:
      status: considered_eds_via_cadr_definition | registered_eds | former_eds | not_current | not_applicable_dbgap_repository | not_established
      listed_name: ""
      uses_dbgap_access_system: true | false | null
      specific_registration_established: true | false | null
      evidence_basis: ""
      source_url: ""
      definition_source_urls: []
      source_as_of: ""
      note: ""
    dbgap_public_studies:
      association_status: observed_in_public_study_export | not_observed | not_evaluated
      study_count: 0
      accessions: []
      study_names: []
      match_terms: []
      match_fields: []
      source_file: ""
      source_sha256: ""
      source_as_of: ""
      limitation: ""
```

## NIH CADR status

`listed` means the repository appears in the pinned official NIH CADR table. The source snapshot records the official name, access system, NIH-designated genomic repository indicator, URL, page date, and CSV SHA-256.

`not_listed` means no row in that pinned snapshot was assigned to the active card. It does not assert that the resource is ineligible, noncompliant, or permanently outside the CADR program.

Assignments are made by a curated active `repo_uid` or, when no UID is supplied, a unique exact homepage-host match. Fuzzy name matching is not used for automatic CADR assignment.

## dbGaP EDS context

The dbGaP guide defines an External Data Source as a non-dbGaP entity that stores data. It states that an EDS meeting the applicable NIH quality, data-management, security, and operational standards is known as an NIH CADR. The project pins this interpretation, its verification date, and the official guide URLs in `reference/dbgap_eds_definition.json`; the file hash is recorded in each run.

### `considered_eds_via_cadr_definition`

Used for a non-dbGaP repository listed in the official NIH CADR snapshot. It records an EDS-class relationship supported by the CADR list plus the dbGaP definition. It does not establish that a particular dbGaP study is registered to use that repository.

### `registered_eds`

Used only when reviewed authoritative evidence establishes a specific current dbGaP EDS registration or equivalent current designation. The evidence URL and as-of date are required.

### `former_eds` and `not_current`

Used for reviewed historical or superseded evidence. The block retains `specific_registration_established: true` because the authoritative source establishes a specific historical registration or designation, while the status itself indicates that it is not current. These statuses must not be inferred from absence in the current public study export.

### `not_applicable_dbgap_repository`

Used for dbGaP itself. dbGaP is listed as an NIH CADR, but the dbGaP definition specifies a non-dbGaP entity for EDS status.

### `not_established`

No authoritative local source establishes the EDS relationship. This is an evidence state, not a negative determination.

## Public dbGaP study associations

The pinned public export is searched conservatively across:

- study name;
- study description; and
- collection label.

Acronyms require token boundaries. Description matching requires a reasonably specific multiword phrase. Each association records its matched term and field.

These matches may help locate relevant studies, but the export has no explicit EDS registration field. They therefore never set `registered_eds` and never establish a particular storage or authorization relationship.

## Source hierarchy

| Evidence | Tier | Permitted assertion |
|---|---|---|
| Official NIH CADR table snapshot | policy | CADR list membership and listed access-system fields |
| Official dbGaP guide | policy | EDS definition and CADR relationship |
| Reviewed authoritative EDS source | curated/policy | Specific current or historical EDS registration |
| Public dbGaP study export | public metadata | Textual study association only |
| Name or homepage similarity | discovery | Candidate for review, not a policy assertion |

## Public reporting

The public site reports direct statuses and evidence fields. It must display the limitation that public study-title, description, or collection text does not by itself establish a specific dbGaP EDS registration. It must not collapse CADR listing, EDS class, specific registration, and public study association into one binary badge or score.
