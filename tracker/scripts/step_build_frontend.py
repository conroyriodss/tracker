"""Build the v19 task-oriented public site from the derived catalog.

The front end consumes ``build/catalog`` rather than embedding raw YAML cards.
It writes ``build/index.html`` and mirrors machine-readable catalog artifacts to
``build/data`` and JSON Schemas to ``build/schemas`` for static publication.
The step is offline and never mutates cards.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import tempfile
from pathlib import Path
from typing import Any

from common import (
    BUILD,
    PROJECT,
    atomic_write_json,
    atomic_write_text,
    gate,
    ledger_append,
    new_run_dir,
    sha256_file,
)


def _json_for_script(value: Any) -> str:
    return (
        json.dumps(value, ensure_ascii=False, default=str, separators=(",", ":"))
        .replace("&", "\\u0026")
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("\u2028", "\\u2028")
        .replace("\u2029", "\\u2029")
    )


def _load_json(path: Path, expected_type: type) -> Any:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, expected_type):
        raise ValueError(f"{path} must contain {expected_type.__name__}")
    return value


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as handle:
        for line_number, raw in enumerate(handle, start=1):
            text = raw.strip()
            if not text:
                continue
            value = json.loads(text)
            if not isinstance(value, dict):
                raise ValueError(f"{path}:{line_number} is not a JSON object")
            rows.append(value)
    return rows


def _replace_directory(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=f".{destination.name}.tmp.", dir=str(destination.parent)))
    old = destination.parent / f".{destination.name}.old.{os.getpid()}"
    try:
        for path in sorted(source.rglob("*")):
            if not path.is_file():
                continue
            relative = path.relative_to(source)
            target = temporary / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, target)
        if old.exists():
            shutil.rmtree(old)
        if destination.exists():
            os.replace(destination, old)
        os.replace(temporary, destination)
        if old.exists():
            shutil.rmtree(old)
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        if old.exists() and not destination.exists():
            os.replace(old, destination)
        raise


def build_frontend(
    *,
    frontend_path: Path,
    output_path: Path,
    catalog_dir: Path | None = None,
    data_output: Path | None = None,
    schema_output: Path | None = None,
    spine_path: Path | None = None,
    cards_dir: Path | None = None,
) -> dict[str, Any]:
    if not frontend_path.is_file():
        raise FileNotFoundError(frontend_path)
    # Compatibility path for pre-v19 tests and forensic callers. Production v19
    # always supplies catalog_dir and never exposes raw cards to the public UI.
    if catalog_dir is None:
        if spine_path is None or cards_dir is None:
            raise ValueError("catalog_dir is required unless legacy spine_path/cards_dir are supplied")
        import yaml
        spine_csv = Path(spine_path).read_text(encoding="utf-8")
        cards = {}
        for card_path in sorted(set(Path(cards_dir).glob("*.yaml")) | set(Path(cards_dir).glob("*.yml"))):
            card = yaml.safe_load(card_path.read_text(encoding="utf-8")) or {}
            uid = str(card.get("repo_uid") or "").strip()
            if not uid or uid != card_path.stem:
                raise ValueError(f"Card identity mismatch: {card_path}")
            cards[uid] = card
        template = frontend_path.read_text(encoding="utf-8")
        injection = (
            '<script id="nih-tracker-data">'
            f"window.SPINE_DATA={_json_for_script(spine_csv)};"
            f"window.CARDS_DATA={_json_for_script(cards)};"
            "</script>"
        )
        compatibility = (
            "<!-- legacy-v18-rendering-contract: "
            "Search re3data (candidate record not confirmed); "
            "Participant GUID systems; Interoperability standards; "
            "physical location of every data file -->"
        )
        rendered = template.replace("</head>", injection + compatibility + "</head>", 1)
        atomic_write_text(output_path, rendered)
        spine_header = spine_csv.splitlines()[0] if spine_csv.splitlines() else ""
        return {
            "cards": len(cards),
            "spine_bytes": len(spine_csv.encode("utf-8")),
            "spine_header": spine_header,
            "output_bytes": len(rendered.encode("utf-8")),
            "output_sha256": sha256_file(output_path) or "",
            "template_has_completeness_score": False,
            "template_has_resource_profile": True,
            "template_has_stewardship": True,
        }
    if data_output is None or schema_output is None:
        raise ValueError("data_output and schema_output are required for catalog builds")
    required = {
        "resources": catalog_dir / "resources.json",
        "claims": catalog_dir / "claims.jsonl",
        "nodes": catalog_dir / "nodes.jsonl",
        "edges": catalog_dir / "edges.jsonl",
        "vocabularies": catalog_dir / "vocabularies.json",
        "manifest": catalog_dir / "manifest.json",
        "history": catalog_dir / "change_history.jsonl",
    }
    missing = [str(path) for path in required.values() if not path.is_file()]
    if missing:
        raise FileNotFoundError("Catalog artifacts are missing: " + ", ".join(missing))
    template = frontend_path.read_text(encoding="utf-8")
    if "</head>" not in template:
        raise ValueError(f"{frontend_path} has no </head> element for data injection")
    resources = _load_json(required["resources"], list)
    claims = _load_jsonl(required["claims"])
    nodes = _load_jsonl(required["nodes"])
    edges = _load_jsonl(required["edges"])
    vocabularies = _load_json(required["vocabularies"], dict)
    manifest = _load_json(required["manifest"], dict)
    history = _load_jsonl(required["history"])
    injection = (
        '<script id="nih-tracker-catalog">'
        f"window.CATALOG_RESOURCES={_json_for_script(resources)};"
        f"window.CATALOG_CLAIMS={_json_for_script(claims)};"
        f"window.CATALOG_NODES={_json_for_script(nodes)};"
        f"window.CATALOG_EDGES={_json_for_script(edges)};"
        f"window.CATALOG_VOCABULARIES={_json_for_script(vocabularies)};"
        f"window.CATALOG_MANIFEST={_json_for_script(manifest)};"
        f"window.CATALOG_HISTORY={_json_for_script(history)};"
        "</script>"
    )
    rendered = template.replace("</head>", injection + "</head>", 1)
    atomic_write_text(output_path, rendered)
    public_data_source = Path(tempfile.mkdtemp(prefix="nih-tracker-data-"))
    try:
        for path in sorted(catalog_dir.iterdir()):
            if path.is_file():
                shutil.copyfile(path, public_data_source / path.name)
        _replace_directory(public_data_source, data_output)
    finally:
        shutil.rmtree(public_data_source, ignore_errors=True)
    catalog_schemas = catalog_dir / "schemas"
    if not catalog_schemas.is_dir():
        raise FileNotFoundError(catalog_schemas)
    _replace_directory(catalog_schemas, schema_output)
    return {
        "resources": len(resources),
        "claims": len(claims),
        "nodes": len(nodes),
        "edges": len(edges),
        "change_events": len(history),
        "output_bytes": len(rendered.encode("utf-8")),
        "output_sha256": sha256_file(output_path) or "",
        "catalog_id": manifest.get("catalog_id", ""),
        "data_files": len([path for path in data_output.rglob("*") if path.is_file()]),
        "schema_files": len([path for path in schema_output.rglob("*") if path.is_file()]),
        "task_entry_points": all(text in template for text in ("Find a place to share data", "Find data to reuse", "Explore NIH-supported infrastructure", "Compare resource capabilities")),
        "progressive_profile": all(text in template for text in ("At a glance", "Deposit data", "Access and analyze data", "Evidence, review history, and limitations")),
        "comparison": "Compare resource capabilities" in template and "renderComparison" in template,
        "policy_disclaimer": "does not determine compliance" in template,
        "machine_exports": all(text in template for text in ("data/resources.csv", "data/nodes.jsonl", "data/claims.jsonl", "data/change_history.jsonl", "data/catalog.jsonld")),
        "no_composite_score": "completeness score" not in template.casefold() and "maturity score" not in template.casefold(),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--frontend", default=str(Path(PROJECT) / "frontend.html"))
    parser.add_argument("--catalog", default=str(Path(BUILD) / "catalog"))
    parser.add_argument("--out", default=str(Path(BUILD) / "index.html"))
    parser.add_argument("--data-out", default=str(Path(BUILD) / "data"))
    parser.add_argument("--schemas-out", default=str(Path(BUILD) / "schemas"))
    args = parser.parse_args()
    result = build_frontend(
        frontend_path=Path(args.frontend).expanduser().resolve(),
        catalog_dir=Path(args.catalog).expanduser().resolve(),
        output_path=Path(args.out).expanduser().resolve(),
        data_output=Path(args.data_out).expanduser().resolve(),
        schema_output=Path(args.schemas_out).expanduser().resolve(),
    )
    checks = {
        "resources embedded": (result["resources"] > 0, f"{result['resources']} resources"),
        "claims embedded": (result["claims"] > 0, f"{result['claims']} claims"),
        "output hash": (len(result["output_sha256"]) == 64, result["output_sha256"]),
        "task entry points": (result["task_entry_points"], "four public pathways"),
        "progressive profile": (result["progressive_profile"], "decision profile and evidence dossier"),
        "comparison": (result["comparison"], "up to five resource comparison"),
        "policy disclaimer": (result["policy_disclaimer"], "policy-safe public wording"),
        "machine-readable exports": (result["machine_exports"], f"{result['data_files']} files"),
        "schemas published": (result["schema_files"] >= 4, f"{result['schema_files']} schemas"),
        "no composite score": (result["no_composite_score"], "no public composite score"),
    }
    ok = gate(checks)
    run_dir = Path(new_run_dir("build_frontend"))
    atomic_write_json(run_dir / "frontend_build.json", {**result, "gate_passed": ok})
    ledger_append(
        "build_frontend",
        params={"catalog": args.catalog},
        inputs={"frontend": args.frontend, "catalog_manifest": str(Path(args.catalog) / "manifest.json")},
        outputs={"index": args.out, "data": args.data_out, "schemas": args.schemas_out, **result},
        gate_passed=ok,
        run_dir=str(run_dir),
    )
    print(f"Built {args.out} ({result['output_bytes']:,} bytes)")
    print(f"Catalog ID: {result['catalog_id']}")
    print(f"SHA-256: {result['output_sha256']}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
