# Repository status curation

Copy `repository_status_curated_template.csv` to `inputs/repository_status_curated.csv` only when a resource-specific status needs to supplement or override BMIC.

## Operational status

Use one of:

- `active`
- `temporarily_paused`
- `under_review`
- `migrating`
- `migrated`
- `retired`
- `inactive`
- `unknown`

Operational status describes whether the resource is functioning for discovery, access, or service delivery. It is separate from whether the resource accepts new deposits.

## Submission status

Use one of:

- `accepting_broad_submissions`
- `accepting_limited_or_programmatic_submissions`
- `not_accepting_new_submissions`
- `temporarily_paused`
- `not_applicable`
- `not_documented`

`accepting_user_contributed_data=yes` should mean that current, authoritative documentation explicitly supports external investigator submission. A BMIC `Open Data Submission=No` indicates that broad intake is not open; it does not establish that every programmatic or invited intake route is closed.

Every substantive curated assertion requires a resource-specific authoritative `source_url` and `source_as_of`. Do not infer status from an old publication, a generic parent-program page, or the continued existence of a landing page.
