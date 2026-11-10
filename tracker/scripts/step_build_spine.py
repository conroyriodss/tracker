"""Flatten cards into the public NIH Tracker table.

The spine carries evidence-bearing summaries, not a synthetic completeness
score. Missing fields remain missing data rather than being converted into a
single ordinal metric.
"""
from __future__ import annotations

import argparse
import os
from typing import Any

import pandas as pd

from common import BUILD, all_cards, gate, ledger_append, new_run_dir
from interoperability_schema import interoperability_summary
from resource_profile_schema import resource_profile_summary
from policy_context_schema import policy_context_summary
from repository_status_schema import repository_status_summary
from publisher_context_schema import publisher_context_summary

TIER_RANK = {
    "declared": 1,
    "registry": 2,
    "verified": 3,
    "curated": 4,
    "attestation": 2,
    "policy": 4,
    None: 0,
    "": 0,
}


def _scalar(value: Any):
    if isinstance(value, dict) and "value" in value:
        return value.get("value"), value.get("tier"), value.get("as_of")
    return value, None, None


def _parquet_ok(frame: pd.DataFrame) -> bool:
    try:
        import pyarrow as pa

        pa.Table.from_pandas(frame, preserve_index=False)
        return True
    except Exception as exc:
        print("  (parquet dry-run error:", str(exc)[:160], ")")
        return False


def flatten_card(uid: str, card: dict[str, Any], blocks: list[str], min_rank: int) -> dict[str, Any]:
    identity = card.get("identity", {}) or {}
    name = identity.get("name")
    status_value, status_tier, status_asof = _scalar(identity.get("status"))
    links = card.get("links", []) or []
    nih_ics = sorted(
        {
            str(link.get("id_value") or "").strip()
            for link in links
            if str(link.get("id_type") or "").strip() == "nih_ic"
            and str(link.get("id_value") or "").strip()
        }
    )
    link_types = {
        str(link.get("id_type") or "").strip()
        for link in links
        if link.get("id_value") or link.get("url")
    }
    re3data_links = [
        link for link in links if str(link.get("id_type") or "").strip() == "re3data"
    ]
    re3data_confirmed = any(link.get("id_value") for link in re3data_links)
    re3data_inferred = not re3data_confirmed and any(link.get("url") for link in re3data_links)

    row: dict[str, Any] = {
        "repo_uid": uid,
        "kind": card.get("kind", "repository"),
        "name": name,
        "abbreviation": identity.get("abbreviation"),
        "aliases": "; ".join(identity.get("aliases", []) or []),
        "homepage": identity.get("homepage"),
        "primary_ic": "; ".join(nih_ics),
        "status": status_value,
        "status_tier": status_tier,
        "status_asof": status_asof,
        "review_state": (card.get("curation") or {}).get("review_state"),
        "confidence": (card.get("curation") or {}).get("confidence"),
        "has_rrid": int("rrid" in link_types),
        "has_fairsharing": int("fairsharing_doi" in link_types),
        "has_re3data": int(re3data_confirmed or re3data_inferred),
        "has_re3data_confirmed": int(re3data_confirmed),
        "has_re3data_inferred": int(re3data_inferred),
    }

    card_blocks = card.get("blocks", {}) or {}
    interoperability = card_blocks.get("interoperability") or {}
    row.update(interoperability_summary(interoperability, repo_uid=uid))
    resource_profile = card_blocks.get("resource_profile") or {}
    row.update(resource_profile_summary(resource_profile))
    policy_context = card_blocks.get("policy_context") or {}
    row.update(policy_context_summary(policy_context))
    repository_status = card_blocks.get("repository_status") or {}
    row.update(repository_status_summary(repository_status))
    publisher_context = card_blocks.get("publisher_context") or {}
    row.update(publisher_context_summary(publisher_context))
    pid_block = card_blocks.get("pid_culture") or {}
    row["pid_culture"] = pid_block.get("pid_culture")

    for block_name in blocks:
        if block_name == "identity":
            continue
        block = card_blocks.get(block_name)
        if not block:
            row[f"{block_name}_present"] = "absent"
            continue
        meta = block.get("_meta", {}) or {}
        tier = meta.get("tier")
        if TIER_RANK.get(tier, 0) < min_rank and tier != "attestation":
            row[f"{block_name}_present"] = "below_tier"
            continue
        row[f"{block_name}_present"] = "present"
        row[f"{block_name}_asof"] = meta.get("as_of")
        row[f"{block_name}_tier"] = tier
        row[f"{block_name}_state"] = meta.get("review_state")
        if block_name == "pid_culture":
            row["pid_culture"] = block.get("pid_culture")
        elif block_name == "access":
            row["access_model"] = block.get("access_model")
            row["nih_cadr"] = block.get("nih_cadr")
    return row


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--blocks", default="identity,interoperability")
    parser.add_argument("--min-tier", default="declared")
    parser.add_argument("--publish-confirmed-only", action="store_true")
    args = parser.parse_args()

    blocks = [value.strip() for value in args.blocks.split(",") if value.strip()]
    min_rank = TIER_RANK.get(args.min_tier, 1)
    cards = all_cards()
    rows = [flatten_card(uid, card, blocks, min_rank) for uid, card in cards.items()]
    spine = pd.DataFrame(rows).sort_values("repo_uid").reset_index(drop=True)
    if args.publish_confirmed_only:
        spine = spine.loc[spine.review_state == "human_confirmed"].reset_index(drop=True)

    n_b = int(spine.repo_uid.astype(str).str.startswith("B").sum())
    n_r = int(spine.repo_uid.astype(str).str.startswith("R").sum())
    forbidden = {"completeness", "blocks_filled"} & set(spine.columns)
    checks = {
        "one row per card": (spine.repo_uid.is_unique, f"{spine.repo_uid.nunique()} uids"),
        "every row has name": (spine.name.notna().all(), f"{int(spine.name.isna().sum())} missing names"),
        "no undefined leaks": (
            not spine.astype(str).apply(lambda series: series.str.contains("undefined", case=False, na=False)).any().any(),
            "no undefined strings",
        ),
        "no completeness score": (not forbidden, f"forbidden columns: {sorted(forbidden)}" if forbidden else "removed"),
        "parquet-serializable": (_parquet_ok(spine), "no mixed-type columns"),
    }
    ok = gate(checks)

    os.makedirs(BUILD, exist_ok=True)
    spine.to_parquet(os.path.join(BUILD, "spine.parquet"), index=False)
    spine.to_csv(os.path.join(BUILD, "spine.csv"), index=False)
    run_dir = new_run_dir("build_spine")
    spine.to_csv(os.path.join(run_dir, "spine.csv"), index=False)

    print(f"\n  rows          : {len(spine)}")
    print(f"  R ids / B ids : {n_r} / {n_b}")
    print(f"  columns       : {list(spine.columns)}")
    for column in (
        "api_evidence_state",
        "api_endpoint_count",
        "api_documentation_count",
        "standard_count",
        "pid_system_count",
        "guid_system_count",
        "resource_type_count",
        "data_asset_type_count",
        "subject_area_count",
        "organization_count",
        "data_format_count",
        "nih_cadr_status",
        "dbgap_eds_status",
        "dbgap_public_study_count",
        "operational_status",
        "submission_status",
        "accepting_user_contributed_data",
        "publisher_recommended",
        "publisher_mandated",
        "publisher_count",
    ):
        if column in spine.columns:
            print(f"\n  {column}:")
            print(spine[column].value_counts(dropna=False).sort_index().to_string())

    ledger_append(
        "build_spine",
        params={
            "blocks": blocks,
            "min_tier": args.min_tier,
            "confirmed_only": args.publish_confirmed_only,
            "completeness_scores": False,
        },
        inputs={},
        outputs={
            "rows": len(spine),
            "R_ids": n_r,
            "B_ids": n_b,
            "cols": len(spine.columns),
            "api_documented": int(
                (
                    spine.get("api_endpoint_count", pd.Series(0, index=spine.index)).fillna(0).astype(int)
                    + spine.get("api_documentation_count", pd.Series(0, index=spine.index)).fillna(0).astype(int)
                    > 0
                ).sum()
            ),
            "standards_documented": int((spine.get("standard_count", 0).fillna(0).astype(int) > 0).sum()) if "standard_count" in spine else 0,
            "guid_systems_documented": int((spine.get("guid_system_count", 0).fillna(0).astype(int) > 0).sum()) if "guid_system_count" in spine else 0,
            "descriptions_documented": int(spine.get("description", pd.Series("", index=spine.index)).fillna("").astype(str).str.strip().ne("").sum()),
            "data_asset_types_documented": int((spine.get("data_asset_type_count", pd.Series(0, index=spine.index)).fillna(0).astype(int) > 0).sum()),
            "organizations_documented": int((spine.get("organization_count", pd.Series(0, index=spine.index)).fillna(0).astype(int) > 0).sum()),
            "nih_cadr_listed": int((spine.get("is_nih_cadr", pd.Series(0, index=spine.index)).fillna(0).astype(int) > 0).sum()),
            "dbgap_eds_class_or_registered": int(
                spine.get("dbgap_eds_status", pd.Series("", index=spine.index))
                .fillna("")
                .astype(str)
                .isin({"considered_eds_via_cadr_definition", "registered_eds"})
                .sum()
            ),
            "dbgap_registered_eds": int(
                spine.get("dbgap_eds_status", pd.Series("", index=spine.index))
                .fillna("")
                .astype(str)
                .eq("registered_eds")
                .sum()
            ),
            "dbgap_public_study_associations": int((spine.get("dbgap_public_study_count", pd.Series(0, index=spine.index)).fillna(0).astype(int) > 0).sum()),
            "operational_status_documented": int(spine.get("operational_status", pd.Series("unknown", index=spine.index)).fillna("unknown").astype(str).ne("unknown").sum()),
            "submission_status_documented": int(spine.get("submission_status", pd.Series("not_documented", index=spine.index)).fillna("not_documented").astype(str).ne("not_documented").sum()),
            "accepting_user_submissions": int(spine.get("accepting_user_contributed_data", pd.Series("", index=spine.index)).fillna("").astype(str).eq("yes").sum()),
            "publisher_recommended": int(spine.get("publisher_recommended", pd.Series(0, index=spine.index)).fillna(0).astype(int).gt(0).sum()),
            "publisher_mandated": int(spine.get("publisher_mandated", pd.Series(0, index=spine.index)).fillna(0).astype(int).gt(0).sum()),
        },
        gate_passed=ok,
        run_dir=run_dir,
    )
    print(f"\nSpine: {os.path.join(BUILD, 'spine.csv')} (+ parquet)")
    print(f"Audit: {run_dir}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
