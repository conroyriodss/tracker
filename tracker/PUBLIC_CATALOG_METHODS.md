# NIH Tracker v19.0.2 public catalog methods

## Scope

The catalog includes resources relevant to NIH-funded research under four nonexclusive public categories:

1. NIH-operated or NIH-maintained.
2. Currently NIH-funded.
3. Documented sustained or historical NIH support.
4. External but relevant to NIH-funded research.

Inclusion is not NIH designation, endorsement, or a determination of Data Management and Sharing Policy compliance.

## Public record layers

1. **Decision card** — the information needed for an initial resource-selection decision.
2. **Researcher profile** — deposit, access, data scope, identifiers, APIs, standards, contacts, and operational guidance.
3. **Evidence dossier** — source-bound claims, evidence state, dates, limitations, and change history.

## Evidence states

| State | Public meaning |
|---|---|
| Verified | Directly checked against authoritative documentation or a bounded live technical test |
| Officially reported | Reported by NIH, the resource operator, or another authoritative owner but not independently tested |
| Registry reported | Reported by re3data, FAIRsharing, RRID, or another third-party registry |
| Not established | Available evidence is insufficient to support the claim |

Missing evidence is not converted to `false`.

## Explainable matching

The Navigator applies selected facets without hidden statistical weights. It reports:

- criteria supported by current evidence;
- criteria contradicted by current evidence;
- criteria for which evidence is not established.

Fit classes are categorical and explanatory:

```text
meets selected criteria
meets with caveats
near match
insufficient evidence
excluded by selected criteria
```

Alphabetical order breaks ties within the same fit class. Organism/model-system and human-data context are searchable facets. The latter is deliberately cautious: it combines documented organism terms with resource-level access and does not determine consent, de-identification, privacy, or legal sensitivity for individual assets.

## Review intervals

| Claim class | Default review interval |
|---|---|
| High-stakes operations/policy | Quarterly or semiannual |
| APIs, standards, PIDs, modalities, contacts, sustainability | Annual |
| Stable mission and broad scope | 18–24 months or on correction |

The claim compiler derives a review-due date from the predicate class and the best available source/review date. If neither date exists, it records a limitation and uses the catalog compilation date as a transparent fallback.

## Change history

Public change history includes material field changes, such as deposit status, access pathway, operational status, NIH relationship, API documentation, or policy designation. It excludes routine rebuild events, unchanged hashes, internal locks, and review transaction details.

## Machine-readable release

The public data directory contains resource projections, graph nodes and edges, claims, vocabularies, change history, field coverage, relationship coverage, source freshness, JSON-LD, and a sealed manifest. JSON Schemas are published separately under `/schemas/`.

## Limitations

- A repository may expose asset-specific conditions that cannot be represented accurately at repository level.
- Organism/model-system scope and human-data context are derived discovery aids and should not be treated as asset-level classifications.
- API-reported, API-reachable, and API-conformance-tested are distinct states.
- CADR listing does not establish current user-facing availability.
- Public dbGaP study text can indicate association but does not by itself prove data residency or a formal external-data-source relationship.
- Publisher mentions differ in posture and scope.
- The current release does not provide a live service API or bulk data-asset catalog.
