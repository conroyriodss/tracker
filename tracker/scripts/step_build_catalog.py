"""Build the v19 NIH Tracker static knowledge graph and public resource views.

Reviewed YAML cards and pinned inputs remain authoritative. This step is a
read-only compiler: it never contacts external services and never mutates a
card. It emits typed nodes, relationships, evidence-bearing claims, concise
public resource views, field-level coverage, change history, schemas, and a
sealed deterministic manifest under ``build/catalog``.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import shutil
import tempfile
from pathlib import Path
from typing import Any

import pandas as pd

from catalog_claims import parse_date, review_due_for
from catalog_exports import artifact_payloads, json_bytes, sha256_bytes, write_payloads
from catalog_graph import CatalogBuilder
from catalog_history import build_change_history
from catalog_resolver import resolve_public_resources
from catalog_schema import MANIFEST_SCHEMA, canonical_json, stable_hash, validate_graph
from catalog_vocab import VocabularyRegistry
from common import (
    BUILD,
    CARDS,
    PROJECT,
    RUNS,
    FileLock,
    all_cards,
    atomic_write_json,
    gate,
    ledger_append,
    new_run_dir,
    sha256_file,
)

CATALOG_DIR = Path(BUILD) / "catalog"
CATALOG_LOCK = Path(BUILD) / ".catalog.lock"


def _today() -> str:
    source_epoch = str(os.environ.get("SOURCE_DATE_EPOCH", "")).strip()
    if source_epoch:
        try:
            return dt.datetime.fromtimestamp(int(source_epoch), tz=dt.timezone.utc).date().isoformat()
        except (TypeError, ValueError, OSError):
            pass
    return dt.datetime.now(dt.timezone.utc).date().isoformat()


def _load_previous(directory: Path) -> tuple[list[dict[str, Any]], str]:
    resources_path = directory / "resources.json"
    manifest_path = directory / "manifest.json"
    resources: list[dict[str, Any]] = []
    catalog_id = ""
    try:
        loaded = json.loads(resources_path.read_text(encoding="utf-8"))
        if isinstance(loaded, list):
            resources = [item for item in loaded if isinstance(item, dict)]
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    try:
        loaded_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        catalog_id = str(loaded_manifest.get("catalog_id") or "")
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    return resources, catalog_id


def _fill_observation_dates(claims: list[dict[str, Any]], catalog_as_of: str) -> int:
    fallback_count = 0
    for claim in claims:
        observed = str(claim.get("observed_at") or "").strip()
        reviewed = str(claim.get("reviewed_at") or "").strip()
        source_date = str(claim.get("source_date") or "").strip()
        if not observed:
            claim["observed_at"] = source_date or reviewed or catalog_as_of
        if not claim.get("review_due"):
            claim["review_due"] = review_due_for(
                str(claim.get("predicate") or ""),
                source_date or reviewed or catalog_as_of,
            )
        if not source_date and not reviewed:
            fallback_count += 1
            limitations = set(claim.get("limitations") or [])
            limitations.add(
                "The underlying source publication or review date was not recorded; observed_at uses the catalog compilation date."
            )
            claim["limitations"] = sorted(limitations)
    return fallback_count


def _coverage_frame(resources: list[dict[str, Any]]) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for resource in resources:
        rows.append(
            {
                "repo_uid": resource.get("repo_uid", ""),
                "name": resource.get("name", ""),
                "resource_type_documented": int(
                    any(item.get("id") != "not_established" for item in resource.get("resource_types", []))
                ),
                "purpose_documented": int(bool(str(resource.get("purpose") or "").strip())),
                "data_asset_type_documented": int(bool(resource.get("best_fit", {}).get("data_asset_types"))),
                "subject_area_documented": int(bool(resource.get("best_fit", {}).get("subject_areas"))),
                "data_modality_documented": int(
                    any(item.get("id") != "not_established" for item in resource.get("best_fit", {}).get("modalities", []))
                ),
                "organism_scope_documented": int(
                    any(item.get("id") != "not_established" for item in resource.get("best_fit", {}).get("organism_scopes", []))
                ),
                "human_data_context_documented": int(
                    resource.get("best_fit", {}).get("human_data_sensitivity", {}).get("id") != "not_established"
                ),
                "taxonomy_documented": int(bool(resource.get("best_fit", {}).get("taxonomies"))),
                "model_system_documented": int(bool(resource.get("best_fit", {}).get("model_systems"))),
                "data_format_documented": int(bool(resource.get("best_fit", {}).get("data_formats"))),
                "stewardship_documented": int(bool(resource.get("stewardship"))),
                "operational_status_documented": int(resource.get("operational", {}).get("id") != "not_established"),
                "submission_model_documented": int(resource.get("deposit", {}).get("id") != "not_established"),
                "access_pathway_documented": int(resource.get("access", {}).get("id") != "not_established"),
                "programmatic_access_documented": int(
                    resource.get("interoperability", {}).get("programmatic_access", {}).get("id") != "not_established"
                ),
                "api_service_count": len(resource.get("interoperability", {}).get("api_services", [])),
                "standard_count": len(resource.get("interoperability", {}).get("all_standards", [])),
                "pid_system_count": len(resource.get("interoperability", {}).get("persistent_identifiers", {}).get("schemes", [])),
                "guid_system_count": len(resource.get("interoperability", {}).get("guid_systems", [])),
                "nih_relationship_documented": int(
                    any(item.get("id") != "not_established" for item in resource.get("nih_relationship", {}).get("categories", []))
                ),
                "nih_cadr_status": resource.get("policy", {}).get("nih_cadr_status", "not_established"),
                "last_reviewed": resource.get("evidence", {}).get("last_reviewed", ""),
                "next_review_due": resource.get("evidence", {}).get("next_review_due", ""),
                "evidence_currency": resource.get("evidence", {}).get("currency", "not_established"),
            }
        )
    return pd.DataFrame(rows).sort_values("repo_uid").reset_index(drop=True)


def _relationship_frame(edges: list[dict[str, Any]], nodes: list[dict[str, Any]]) -> pd.DataFrame:
    nodes_by_id = {node["id"]: node for node in nodes}
    rows = [
        {
            "subject": edge["subject"],
            "subject_label": (nodes_by_id.get(edge["subject"]) or {}).get("label", ""),
            "predicate": edge["predicate"],
            "object": edge["object"],
            "object_label": (nodes_by_id.get(edge["object"]) or {}).get("label", ""),
            "claim_count": len(edge.get("claim_ids") or []),
        }
        for edge in edges
    ]
    return pd.DataFrame(rows).sort_values(["subject", "predicate", "object"]).reset_index(drop=True) if rows else pd.DataFrame(columns=["subject", "subject_label", "predicate", "object", "object_label", "claim_count"])


def _freshness_frame(claims: list[dict[str, Any]], catalog_as_of: str) -> pd.DataFrame:
    as_of_date = parse_date(catalog_as_of)
    rows = []
    for claim in claims:
        due = parse_date(claim.get("review_due"))
        rows.append(
            {
                "claim_id": claim.get("id", ""),
                "subject": claim.get("subject", ""),
                "predicate": claim.get("predicate", ""),
                "evidence_state": claim.get("evidence_state", ""),
                "source_id": claim.get("source_id", ""),
                "source_url": claim.get("source_url", ""),
                "source_date": claim.get("source_date", ""),
                "observed_at": claim.get("observed_at", ""),
                "reviewed_at": claim.get("reviewed_at", ""),
                "review_due": claim.get("review_due", ""),
                "review_status": "due" if as_of_date and due and due < as_of_date else "current" if due else "not_established",
                "used_compilation_date_fallback": int(
                    any("catalog compilation date" in limitation for limitation in claim.get("limitations") or [])
                ),
            }
        )
    return pd.DataFrame(rows).sort_values(["subject", "predicate", "claim_id"]).reset_index(drop=True)


def _write_frame(path: Path, frame: pd.DataFrame) -> None:
    frame.to_csv(path, index=False, lineterminator="\n")


def build_catalog(
    *,
    vocab_path: Path,
    schemas_dir: Path,
    output_dir: Path,
    catalog_as_of: str,
) -> dict[str, Any]:
    cards = all_cards()
    if not cards:
        raise RuntimeError("No cards are available for catalog compilation")
    vocabularies = VocabularyRegistry.load(vocab_path)
    builder = CatalogBuilder(vocabularies)
    for uid, card in sorted(cards.items()):
        card_path = Path(CARDS) / f"{uid}.yaml"
        if not card_path.is_file():
            alternate = Path(CARDS) / f"{uid}.yml"
            card_path = alternate if alternate.is_file() else card_path
        digest = sha256_file(card_path) or stable_hash(card, length=64)
        builder.compile_card(uid, card, card_sha256=digest)
    nodes = builder.sorted_nodes()
    edges = builder.sorted_edges()
    claims = builder.sorted_claims()
    fallback_date_claims = _fill_observation_dates(claims, catalog_as_of)
    graph_errors = validate_graph(nodes, edges, claims)
    if graph_errors:
        raise RuntimeError("Catalog graph validation failed: " + "; ".join(graph_errors[:20]))

    previous_resources, prior_catalog_id = _load_previous(output_dir)
    base_identity = {
        "schema": "nih-tracker-catalog-identity/v1",
        "input_fingerprint": builder.input_fingerprint(),
        "vocabulary_sha256": sha256_file(vocab_path) or "",
        "catalog_as_of": catalog_as_of,
        "nodes_sha256": sha256_bytes(json_bytes(nodes, pretty=False)),
        "edges_sha256": sha256_bytes(json_bytes(edges, pretty=False)),
        "claims_sha256": sha256_bytes(json_bytes(claims, pretty=False)),
    }
    catalog_id = stable_hash(base_identity, length=64)
    resources = resolve_public_resources(
        nodes=nodes,
        edges=edges,
        claims=claims,
        vocabularies=vocabularies,
        catalog_as_of=catalog_as_of,
    )
    history = (
        build_change_history(
            previous_resources,
            resources,
            effective_date=catalog_as_of,
            prior_catalog_id=prior_catalog_id,
            current_catalog_id=catalog_id,
        )
        if previous_resources
        else []
    )
    payloads = artifact_payloads(
        nodes=nodes,
        edges=edges,
        claims=claims,
        resources=resources,
        history=history,
        vocabularies=vocabularies.payload,
    )

    build_root = Path(BUILD)
    build_root.mkdir(parents=True, exist_ok=True)
    with FileLock(CATALOG_LOCK, purpose="build v19 catalog", timeout=0):
        temporary = Path(tempfile.mkdtemp(prefix=".catalog.tmp.", dir=str(build_root)))
        old = build_root / f".catalog.old.{os.getpid()}"
        try:
            artifact_manifest = write_payloads(temporary, payloads)
            coverage = _coverage_frame(resources)
            relationships = _relationship_frame(edges, nodes)
            freshness = _freshness_frame(claims, catalog_as_of)
            _write_frame(temporary / "field_coverage.csv", coverage)
            _write_frame(temporary / "relationship_coverage.csv", relationships)
            _write_frame(temporary / "source_freshness.csv", freshness)
            migration = pd.DataFrame(
                [
                    {
                        "repo_uid": resource.get("repo_uid", ""),
                        "resource_node_id": resource.get("id", ""),
                        "card_sha256": builder.card_sha256.get(str(resource.get("repo_uid")), ""),
                        "migration_status": "mapped",
                    }
                    for resource in resources
                ]
            ).sort_values("repo_uid")
            _write_frame(temporary / "migration_audit.csv", migration)
            schema_output = temporary / "schemas"
            schema_output.mkdir(parents=True, exist_ok=True)
            for source in sorted(schemas_dir.glob("*.json")):
                shutil.copyfile(source, schema_output / source.name)
            for name in ("field_coverage.csv", "relationship_coverage.csv", "source_freshness.csv", "migration_audit.csv"):
                data = (temporary / name).read_bytes()
                artifact_manifest[name] = {"sha256": sha256_bytes(data), "bytes": len(data)}
            for source in sorted(schema_output.glob("*.json")):
                relative = f"schemas/{source.name}"
                data = source.read_bytes()
                artifact_manifest[relative] = {"sha256": sha256_bytes(data), "bytes": len(data)}
            manifest = {
                "schema": MANIFEST_SCHEMA,
                "catalog_id": catalog_id,
                "catalog_as_of": catalog_as_of,
                "input_fingerprint": builder.input_fingerprint(),
                "vocabulary": {
                    "path": (
                        vocab_path.relative_to(Path(PROJECT).resolve()).as_posix()
                        if vocab_path.is_relative_to(Path(PROJECT).resolve())
                        else vocab_path.name
                    ),
                    "version": vocabularies.payload.get("version"),
                    "sha256": sha256_file(vocab_path) or "",
                },
                "counts": {
                    "resources": len(resources),
                    "nodes": len(nodes),
                    "edges": len(edges),
                    "claims": len(claims),
                    "change_events": len(history),
                    "claims_using_compilation_date_fallback": fallback_date_claims,
                },
                "artifacts": {name: artifact_manifest[name] for name in sorted(artifact_manifest)},
                "source_of_truth": "reviewed YAML cards and pinned project snapshots",
                "derived_catalog": True,
                "network_access": False,
                "composite_score": False,
            }
            manifest_bytes = json_bytes(manifest)
            (temporary / "manifest.json").write_bytes(manifest_bytes)
            artifact_manifest["manifest.json"] = {"sha256": sha256_bytes(manifest_bytes), "bytes": len(manifest_bytes)}
            if old.exists():
                shutil.rmtree(old)
            if output_dir.exists():
                os.replace(output_dir, old)
            os.replace(temporary, output_dir)
            if old.exists():
                shutil.rmtree(old)
        except Exception:
            shutil.rmtree(temporary, ignore_errors=True)
            if old.exists() and not output_dir.exists():
                os.replace(old, output_dir)
            raise
    return {
        "catalog_id": catalog_id,
        "catalog_as_of": catalog_as_of,
        "resources": len(resources),
        "nodes": len(nodes),
        "edges": len(edges),
        "claims": len(claims),
        "change_events": len(history),
        "fallback_date_claims": fallback_date_claims,
        "output_dir": str(output_dir),
        "manifest": str(output_dir / "manifest.json"),
        "field_coverage": str(output_dir / "field_coverage.csv"),
        "relationship_coverage": str(output_dir / "relationship_coverage.csv"),
        "source_freshness": str(output_dir / "source_freshness.csv"),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--vocabularies",
        default=str(Path(PROJECT) / "reference" / "catalog_vocabularies_v1.json"),
    )
    parser.add_argument(
        "--schemas",
        default=str(Path(PROJECT) / "schemas"),
    )
    parser.add_argument("--out", default=str(CATALOG_DIR))
    parser.add_argument(
        "--as-of",
        default=os.environ.get("NIH_TRACKER_CATALOG_AS_OF", "") or _today(),
        help="Catalog compilation date (YYYY-MM-DD). Set for reproducible historical builds.",
    )
    args = parser.parse_args()
    if parse_date(args.as_of) is None:
        raise ValueError("--as-of must be an ISO date or year-month")
    result = build_catalog(
        vocab_path=Path(args.vocabularies).expanduser().resolve(),
        schemas_dir=Path(args.schemas).expanduser().resolve(),
        output_dir=Path(args.out).expanduser().resolve(),
        catalog_as_of=str(args.as_of),
    )
    manifest = json.loads(Path(result["manifest"]).read_text(encoding="utf-8"))
    checks = {
        "every card mapped": (result["resources"] == len(all_cards()), f"{result['resources']} resources"),
        "graph nodes present": (result["nodes"] >= result["resources"], f"{result['nodes']} nodes"),
        "claims present": (result["claims"] > 0, f"{result['claims']} claims"),
        "manifest sealed": (len(manifest.get("catalog_id", "")) == 64, manifest.get("catalog_id", "")),
        "no composite score": (manifest.get("composite_score") is False, "composite scores disabled"),
        "offline compiler": (manifest.get("network_access") is False, "no network access"),
    }
    ok = gate(checks)
    run_dir = Path(new_run_dir("build_catalog"))
    atomic_write_json(run_dir / "catalog_build.json", {**result, "gate_passed": ok})
    for name in ("manifest.json", "field_coverage.csv", "relationship_coverage.csv", "source_freshness.csv", "migration_audit.csv"):
        source = Path(result["output_dir"]) / name
        if source.is_file():
            shutil.copyfile(source, run_dir / name)
    ledger_append(
        "build_catalog",
        params={"as_of": args.as_of, "vocabularies": str(args.vocabularies)},
        inputs={
            "cards": stable_hash(sorted((uid, sha256_file(Path(CARDS) / f"{uid}.yaml") or "") for uid in all_cards()), length=64),
            "vocabularies": sha256_file(args.vocabularies),
        },
        outputs=result,
        gate_passed=ok,
        run_dir=str(run_dir),
    )
    print(f"Catalog: {result['output_dir']}")
    print(f"Catalog ID: {result['catalog_id']}")
    print(
        f"Resources={result['resources']} nodes={result['nodes']} edges={result['edges']} "
        f"claims={result['claims']} change_events={result['change_events']}"
    )
    print(f"Coverage: {result['field_coverage']}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
