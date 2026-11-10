# NIH Tracker publisher-policy evidence model (v17.1)

## Purpose

`blocks.publisher_context` records whether a repository is named in an authoritative publisher or journal data policy. It does not infer that the publisher generally endorses the repository, that the repository accepts the investigator's data, or that use of the repository satisfies NIH, consent, legal, or institutional requirements.

## Mention postures

| `mention_type` | Meaning | Sets `publisher_recommended` |
|---|---|---:|
| `mandatory` | Required for a stated data type or submission context | Yes |
| `recommended` | Explicitly recommended | Yes |
| `preferred` | Preferred over alternatives for the stated context | Yes |
| `acceptable_fallback` | Acceptable when a domain repository is unavailable | No |
| `example` | Illustrative named repository | No |
| `directory_referral` | Referral to FAIRsharing, re3data, or another selection directory | No |

`publisher_mandated` is true only when at least one `mandatory` relationship is present. `publisher_mentioned` is true for any relationship. The public flag requested by the project is `publisher_recommended`, which deliberately excludes examples, fallback options, and directory referrals.

## Canonical structure

```yaml
blocks:
  publisher_context:
    _meta:
      block_version: "1.0"
      as_of: "2026-09-16"
      tier: policy
      review_state: auto
      sources: []
    publisher_mentioned: true
    publisher_recommended: true
    publisher_mandated: false
    publisher_acceptable_fallback: false
    publisher_count: 2
    mentions:
      - publisher_family: PLOS
        journal_title: ""
        policy_title: Recommended Repositories
        policy_url: https://...
        policy_level: recommended
        mention_type: recommended
        data_domain: general
        access_model: mixed
        repository_name_as_listed: Example Repository
        source_location: Recommended repositories
        evidence_note: ""
        date_checked: "2026-09-16"
        source_sha256: ""
        source_snapshot: ""
    limitation: >-
      Publisher policies vary by journal, article type, and data type. A mention
      does not supersede NIH, consent, legal, institutional, or repository intake requirements.
```

## Source architecture

Two pinned tables are used:

- `reference/publisher_policy_sources.csv` identifies the official policy pages, expected anchor terms, response hash, snapshot path, and refresh state.
- `reference/publisher_repository_mentions.csv` contains the human-coded semantic assertions linking a policy to a repository and mention posture.

`step_update_publisher_info.py` refreshes source pages and detects drift. It does **not** scrape a changed page into new semantic assertions. A content-hash change or missing expected term produces `review_required`, after which a human reviews and revises the mention table.

## Matching and safeguards

The builder attaches a mention only by:

1. Explicit active `repo_uid` in the pinned table.
2. Exact normalized card name or alias.
3. Unique exact non-generic homepage host.

Fuzzy publisher-policy matching is prohibited. Ambiguous matches fail the gate. Unmatched mentions remain visible in `publisher_repository_match_report.csv` and do not modify cards.

## Update workflow

1. Run `step_update_publisher_info.py` in dry-run mode.
2. Inspect `publisher_source_drift_report.csv`.
3. Review changed source pages and revise the mention inventory when needed.
4. Apply the source refresh to update hashes and content-addressed snapshots.
5. Run `step_build_publisher_context.py` and inspect matching/proposal outputs.
6. Apply through the fixed pipeline and republish the public build.
