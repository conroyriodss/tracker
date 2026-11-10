# NIH Tracker repository-status evidence model (v17.1)

## Purpose

`blocks.repository_status` answers two separate questions:

1. Is the resource operational for discovery, access, or service delivery?
2. Does it currently accept new data contributed by investigators outside a closed program or project team?

A resource may be operational while deposit intake is closed, paused, or limited to a particular program. Conversely, a migration landing page may remain reachable although the original repository is no longer the operational data platform.

## Canonical structure

```yaml
blocks:
  repository_status:
    _meta:
      block_version: "1.0"
      as_of: "2026-09"
      tier: attestation | curated
      review_state: auto | human_confirmed
      sources: []
    operational:
      status: active | temporarily_paused | under_review | migrating | migrated | retired | inactive | unknown
      label: Active
      note: ""
      source_url: ""
      source_as_of: ""
      source: ""
      tier: ""
    submissions:
      status: accepting_broad_submissions | accepting_limited_or_programmatic_submissions | not_accepting_new_submissions | temporarily_paused | not_applicable | not_documented
      accepting_user_contributed_data: true | false | null
      open_data_submission: true | false | null
      open_time_frame_for_data_deposit: true | false | null
      policy_url: ""
      policy_text: ""
      note: ""
      source_url: ""
      source_as_of: ""
      source: ""
      tier: ""
    access:
      status: open | controlled | mixed | restricted | temporarily_paused | migrated | unavailable | not_documented
      access_type: ""
      access_url: ""
      note: ""
      source_url: ""
      source_as_of: ""
    support:
      current_nih_funding_support: true | false | null
      sustained_support: true | false | null
    bmic:
      matched: false
      repository_name: ""
      source_file: BMIC_download.csv
      source_sha256: ""
      source_row: null
      last_reviewed: ""
      match_basis: ""
    notices: []
```

## BMIC interpretation

The NIH repository table's `Open Data Submission` criterion is interpreted as broad intake from investigators rather than a closed project team. Therefore:

- `Yes` supports `accepting_broad_submissions` and `accepting_user_contributed_data=true`.
- `No` supports `accepting_limited_or_programmatic_submissions`, but leaves `accepting_user_contributed_data=null` unless resource-specific evidence establishes an explicit yes or no.
- A blank value produces `not_documented`.

This avoids converting “not open to broad submission” into the stronger and often unsupported claim “accepts no new contributed data.”

## Evidence precedence

1. Reviewed `inputs/repository_status_curated.csv` assertions.
2. Pinned exact-match reference assertions in `reference/repository_status_overrides.csv`.
3. Current BMIC fields.
4. Existing `identity.status` for operational state only.
5. `unknown` or `not_documented`.

Reference and curated rows require an authoritative URL and an as-of date. Matching uses an active UID, exact card name/alias, or a unique exact homepage host. Fuzzy matching is prohibited.

## Update workflow

- Refresh or validate BMIC with `step_update_bmic_info.py`.
- Build status proposals with `step_build_repository_status.py`.
- Inspect `repository_status_proposal.csv`, `repository_status_gaps.csv`, and the two match reports.
- Add resource-specific evidence only through the curated template when the source supports it.
- Apply in the fixed pipeline under the card lock.
