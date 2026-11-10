from __future__ import annotations

import ast
import hashlib
import importlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

import jsonschema
import nbformat
import yaml

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))


def clear_modules() -> None:
    for name in list(sys.modules):
        if name.startswith("catalog_") or name in {
            "common",
            "step_build_catalog",
            "step_build_frontend",
            "step_publish_github",
            "step_initialize_environments",
            "review_workflow",
            "step_review_workflow",
        }:
            sys.modules.pop(name, None)


def configure(project: Path) -> None:
    for key in list(os.environ):
        if key.startswith("NIH_TRACKER_ROOT_") or key in {
            "NIH_TRACKER",
            "NIH_TRACKER_ENV",
            "NIH_TRACKER_PRODUCTION_ONLY",
            "NIH_TRACKER_SHARED_CACHE_ROOT",
            "NIH_TRACKER_CATALOG_AS_OF",
        }:
            os.environ.pop(key, None)
    os.environ["NIH_TRACKER_PRODUCTION_ONLY"] = "1"
    os.environ["NIH_TRACKER_ENV"] = "prod"
    os.environ["NIH_TRACKER_ROOT_PROD"] = str(project)
    os.environ["NIH_TRACKER_SHARED_CACHE_ROOT"] = str(project.parent / "shared" / "cache")
    os.environ["NIH_TRACKER_CATALOG_AS_OF"] = "2026-09-28"
    clear_modules()


def write_card(project: Path, uid: str = "R1") -> None:
    card = {
        "schema_version": "1.0",
        "repo_uid": uid,
        "kind": "repository",
        "identity": {
            "name": "Example Biomedical Repository",
            "abbreviation": "EBR",
            "homepage": "https://example.org/repository",
            "aliases": ["Example Repository"],
        },
        "links": [
            {
                "id_type": "re3data",
                "id_value": "r3d100000001",
                "url": "https://www.re3data.org/repository/r3d100000001",
                "tier": "registry",
            },
            {"id_type": "nih_ic", "id_value": "NCI", "tier": "attestation"},
        ],
        "blocks": {
            "repository_status": {
                "_meta": {
                    "as_of": "2026-09-25",
                    "tier": "attestation",
                    "sources": ["https://example.org/status"],
                },
                "operational": {"status": "active", "label": "Active"},
                "submissions": {
                    "status": "accepting_broad_submissions",
                    "accepting_user_contributed_data": True,
                    "policy_url": "https://example.org/deposit",
                },
                "access": {"status": "mixed", "access_url": "https://example.org/access"},
            },
            "resource_profile": {
                "_meta": {
                    "as_of": "2026-09-25",
                    "tier": "attestation",
                    "sources": ["https://example.org/about"],
                },
                "descriptions": [
                    {
                        "text": "Repository for human cancer participants, biomedical imaging, genomic datasets, and mouse models.",
                        "source": "https://example.org/about",
                        "source_url": "https://example.org/about",
                        "as_of": "2026-09-25",
                        "tier": "attestation",
                    }
                ],
                "resource_types": [
                    {"name": "data repository", "source": "https://example.org/about", "tier": "attestation"}
                ],
                "data_asset_types": [
                    {"name": "Dataset", "source": "https://example.org/about", "tier": "attestation"}
                ],
                "subject_areas": [
                    {"name": "Cancer research", "source": "https://example.org/about", "tier": "attestation"}
                ],
                "data_formats": [
                    {"name": "DICOM", "source": "https://example.org/docs", "tier": "attestation"}
                ],
                "taxonomies": [
                    {"name": "Homo sapiens", "source": "https://example.org/about", "tier": "attestation"},
                    {"name": "Mus musculus", "source": "https://example.org/about", "tier": "attestation"}
                ],
                "organizations": [
                    {"name": "Example Institute", "role": "operator", "source": "https://example.org/about", "tier": "attestation"}
                ],
            },
            "bmic": {
                "_meta": {"as_of": "2026-09", "source": "BMIC-list", "tier": "attestation"},
                "model_system": {"value": "Human participants and mouse models", "source": "BMIC-list", "tier": "attestation", "as_of": "2026-09"}
            },
            "interoperability": {
                "_meta": {
                    "as_of": "2026-09-25",
                    "tier": "attestation",
                    "sources": ["https://example.org/api"],
                },
                "access_class": "public",
                "api_declared": True,
                "api_verified": True,
                "api_evidence_state": "verified_documented",
                "api": {
                    "endpoints": [
                        {
                            "family": "REST",
                            "endpoint_url": "https://api.example.org/v1",
                            "documentation_url": "https://example.org/api",
                            "verified": True,
                            "source": "https://example.org/api",
                            "tier": "attestation",
                        }
                    ]
                },
                "standards": [
                    {
                        "name": "DICOM",
                        "role": "data exchange",
                        "evidence_url": "https://example.org/docs",
                        "source": "https://example.org/docs",
                        "tier": "attestation",
                        "declared": True,
                        "verified": False,
                    }
                ],
                "pid_systems": [
                    {"name": "DOI", "role": "dataset-level", "source": "https://example.org/citation", "tier": "attestation"}
                ],
                "guid_systems": [
                    {
                        "name": "Example participant GUID",
                        "documentation_url": "https://example.org/guid",
                        "source": "https://example.org/guid",
                        "tier": "attestation",
                    }
                ],
            },
            "policy_context": {
                "_meta": {
                    "as_of": "2026-09-25",
                    "tier": "official",
                    "sources": ["https://example.org/policy"],
                },
                "nih_cadr": {
                    "status": "listed",
                    "access_system": "Example Access System",
                    "nih_designated_genomic_data_repository": True,
                },
                "dbgap_external_data_source": {"status": "considered_eds_via_cadr_definition"},
            },
            "publisher_context": {
                "_meta": {
                    "as_of": "2026-09-25",
                    "tier": "attestation",
                    "sources": ["https://publisher.example/policy"],
                },
                "mentions": [
                    {
                        "publisher_family": "Example Publisher",
                        "mention_type": "recommended",
                        "policy_url": "https://publisher.example/policy",
                    }
                ],
            },
        },
        "curation": {"review_state": "human_confirmed", "last_reviewed": "2026-09-25"},
    }
    path = project / "cards" / f"{uid}.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(card, sort_keys=False), encoding="utf-8")


def project_layout(project: Path) -> None:
    for name in ("cards", "inputs", "blocks", "runs", "latest", "build", "snapshots"):
        (project / name).mkdir(parents=True, exist_ok=True)


def tree_hash(directory: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(p for p in directory.rglob("*") if p.is_file()):
        digest.update(path.relative_to(directory).as_posix().encode())
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


class V19KnowledgeGraphTests(unittest.TestCase):
    def setUp(self) -> None:
        self.old_env = os.environ.copy()

    def tearDown(self) -> None:
        os.environ.clear()
        os.environ.update(self.old_env)
        clear_modules()

    def _build(self, project: Path):
        configure(project)
        module = importlib.import_module("step_build_catalog")
        return module.build_catalog(
            vocab_path=ROOT / "reference" / "catalog_vocabularies_v1.json",
            schemas_dir=ROOT / "schemas",
            output_dir=project / "build" / "catalog",
            catalog_as_of="2026-09-28",
        )

    def test_catalog_compiler_is_deterministic_and_preserves_card_source_of_truth(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary) / "NIH_Tracker"
            project_layout(project)
            write_card(project)
            original = (project / "cards" / "R1.yaml").read_bytes()
            first = self._build(project)
            first_hash = tree_hash(project / "build" / "catalog")
            second = self._build(project)
            second_hash = tree_hash(project / "build" / "catalog")
            self.assertEqual(first["catalog_id"], second["catalog_id"])
            self.assertEqual(first_hash, second_hash)
            self.assertEqual((project / "cards" / "R1.yaml").read_bytes(), original)
            manifest = json.loads((project / "build" / "catalog" / "manifest.json").read_text())
            self.assertTrue(manifest["derived_catalog"])
            self.assertFalse(manifest["network_access"])
            self.assertFalse(manifest["composite_score"])
            jsonschema.validate(manifest, json.loads((ROOT / "schemas" / "catalog_manifest.schema.json").read_text()))
            public_resources = json.loads((project / "build" / "catalog" / "resources.json").read_text())
            jsonschema.validate(public_resources[0], json.loads((ROOT / "schemas" / "public_resource.schema.json").read_text()))

    def test_graph_has_typed_nodes_edges_and_claim_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary) / "NIH_Tracker"
            project_layout(project)
            write_card(project)
            self._build(project)
            catalog = project / "build" / "catalog"
            nodes = [json.loads(line) for line in (catalog / "nodes.jsonl").read_text().splitlines() if line]
            edges = [json.loads(line) for line in (catalog / "edges.jsonl").read_text().splitlines() if line]
            claims = [json.loads(line) for line in (catalog / "claims.jsonl").read_text().splitlines() if line]
            node_types = {row["type"] for row in nodes}
            self.assertTrue({"Resource", "Organization", "NIHOrganizationalUnit", "APIService", "Standard", "IdentifierScheme", "GUIDSystem", "PolicyDesignation", "PublisherPolicy", "EvidenceSource"}.issubset(node_types))
            self.assertIn("EXPOSES_API", {row["predicate"] for row in edges})
            self.assertIn("SUPPORTS_GUID_SYSTEM", {row["predicate"] for row in edges})
            self.assertIn("RECOMMENDED_BY_PUBLISHER_POLICY", {row["predicate"] for row in edges})
            for claim in claims:
                self.assertIn(claim["evidence_state"], {"verified", "officially_reported", "registry_reported", "not_established"})
                self.assertTrue(claim["source_id"])
                self.assertTrue(claim["observed_at"])
                self.assertTrue(claim["review_due"])
            resource = json.loads((catalog / "resources.json").read_text())[0]
            self.assertIn("human", resource["facets"]["organism_scope"])
            self.assertIn("non_human", resource["facets"]["organism_scope"])
            self.assertIn("multi_species", resource["facets"]["organism_scope"])
            self.assertEqual(resource["best_fit"]["human_data_sensitivity"]["id"], "mixed")
            coverage = (catalog / "field_coverage.csv").read_text()
            self.assertIn("organism_scope_documented", coverage)
            self.assertIn("human_data_context_documented", coverage)

    def test_material_change_history_is_generated_and_embedded(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary) / "NIH_Tracker"
            project_layout(project)
            write_card(project)
            self._build(project)
            card_path = project / "cards" / "R1.yaml"
            card = yaml.safe_load(card_path.read_text(encoding="utf-8"))
            card["blocks"]["repository_status"]["submissions"]["status"] = "accepting_limited_or_programmatic_submissions"
            card_path.write_text(yaml.safe_dump(card, sort_keys=False), encoding="utf-8")
            self._build(project)
            events = [
                json.loads(line)
                for line in (project / "build" / "catalog" / "change_history.jsonl").read_text().splitlines()
                if line
            ]
            self.assertTrue(any(event.get("field") == "deposit.id" for event in events))
            configure(project)
            frontend = importlib.import_module("step_build_frontend")
            frontend.build_frontend(
                frontend_path=ROOT / "frontend.html",
                catalog_dir=project / "build" / "catalog",
                output_path=project / "build" / "index.html",
                data_output=project / "build" / "data",
                schema_output=project / "build" / "schemas",
            )
            text = (project / "build" / "index.html").read_text()
            self.assertIn("window.CATALOG_HISTORY", text)
            self.assertIn("Deposit pathway", text)

    def test_data_asset_extension_schema_validates_template_shape(self) -> None:
        schema = json.loads((ROOT / "schemas" / "data_asset.schema.json").read_text())
        example = {
            "asset_uid": "A:example",
            "title": "Example data asset",
            "asset_type": "dataset",
            "identifiers": [{"type": "doi", "value": "10.1234/example", "url": "https://doi.org/10.1234/example"}],
            "repository_uids": ["R1"],
            "access_system_uids": [],
            "creators": [],
            "stewards": [],
            "ror_ids": [],
            "nih_awards": [],
            "nih_ics": [],
            "subject_areas": [],
            "populations": [],
            "modalities": [],
            "data_types": [],
            "formats": [],
            "standards": [],
            "consent_or_use_terms": [],
            "duo_terms": [],
            "guid_systems": [],
            "api_or_distribution_links": [],
            "related_publications": [],
            "provenance": {"source": "fixture", "source_snapshot_sha256": "a" * 64, "as_of": "2026-09-28"},
        }
        jsonschema.validate(example, schema)

    def test_frontend_uses_task_paths_progressive_disclosure_and_exports(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary) / "NIH_Tracker"
            project_layout(project)
            write_card(project)
            self._build(project)
            configure(project)
            frontend = importlib.import_module("step_build_frontend")
            result = frontend.build_frontend(
                frontend_path=ROOT / "frontend.html",
                catalog_dir=project / "build" / "catalog",
                output_path=project / "build" / "index.html",
                data_output=project / "build" / "data",
                schema_output=project / "build" / "schemas",
            )
            text = (project / "build" / "index.html").read_text()
            for phrase in ("Find a place to share data", "Find data to reuse", "Explore NIH-supported infrastructure", "Compare resource capabilities", "Organism or model-system scope", "Human-data access context", "Evidence, review history, and limitations"):
                self.assertIn(phrase, text)
            self.assertTrue(result["task_entry_points"])
            self.assertTrue(result["comparison"])
            self.assertTrue(result["no_composite_score"])
            self.assertTrue((project / "build" / "data" / "resources.json").is_file())
            self.assertTrue((project / "build" / "data" / "catalog.jsonld").is_file())
            self.assertTrue((project / "build" / "data" / "change_history.jsonl").is_file())
            self.assertIn("window.CATALOG_HISTORY", text)
            self.assertTrue((project / "build" / "schemas" / "catalog_claim.schema.json").is_file())

    def test_publication_plan_seals_nested_catalog_payload(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary) / "NIH_Tracker"
            project_layout(project)
            configure(project)
            build = project / "build"
            (build / "index.html").write_text("<html>v19</html>", encoding="utf-8")
            (build / "data").mkdir()
            (build / "data" / "resources.json").write_text("[]\n", encoding="utf-8")
            (build / "schemas").mkdir()
            (build / "schemas" / "catalog_node.schema.json").write_text("{}\n", encoding="utf-8")
            publisher = importlib.import_module("step_publish_github")
            run_dir = project / "runs" / "publish_github" / "test"
            plan_path = publisher.build_publication_plan(
                source=build / "index.html",
                repository="owner/repository",
                branch="main",
                publish_dir="docs",
                run_dir=run_dir,
            )
            plan = json.loads(plan_path.read_text())
            self.assertEqual(plan["schema"], "nih-tracker-github-publication/v2")
            paths = {row["path"] for row in plan["payload_files"]}
            self.assertEqual(paths, {"index.html", "data/resources.json", "schemas/catalog_node.schema.json"})
            self.assertTrue((run_dir / publisher.PLAN_SEAL_NAME).is_file())

    def test_publication_allows_empty_change_history_but_rejects_other_empty_payloads(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary) / "NIH_Tracker"
            project_layout(project)
            configure(project)
            build = project / "build"
            (build / "index.html").write_text("<html>v19.0.1</html>", encoding="utf-8")
            (build / "data").mkdir()
            (build / "data" / "resources.json").write_text("[]\n", encoding="utf-8")
            (build / "data" / "change_history.jsonl").write_bytes(b"")
            publisher = importlib.import_module("step_publish_github")
            run_dir = project / "runs" / "publish_github" / "empty-history"
            plan_path = publisher.build_publication_plan(
                source=build / "index.html",
                repository="owner/repository",
                branch="main",
                publish_dir="docs",
                run_dir=run_dir,
            )
            plan = publisher.load_publication_plan(plan_path)
            self.assertEqual(plan["empty_payload_files"], ["data/change_history.jsonl"])
            history_record = next(
                row for row in plan["payload_files"]
                if row["path"] == "data/change_history.jsonl"
            )
            self.assertEqual(history_record["bytes"], 0)

            (build / "data" / "nodes.jsonl").write_bytes(b"")
            with self.assertRaisesRegex(ValueError, "Public payload is empty"):
                publisher.build_publication_plan(
                    source=build / "index.html",
                    repository="owner/repository",
                    branch="main",
                    publish_dir="docs",
                    run_dir=project / "runs" / "publish_github" / "empty-required",
                )

    def test_review_state_prioritizes_unapproved_imports_and_finalizer_blocks(self) -> None:
        source = (ROOT / "scripts" / "review_workflow.py").read_text(encoding="utf-8")
        approval_index = source.index('state = "APPROVAL_REQUIRED"')
        finalize_index = source.index('state = "READY_TO_FINALIZE"')
        self.assertLess(approval_index, finalize_index)
        finalizer = (ROOT / "scripts" / "step_review_workflow.py").read_text(encoding="utf-8")
        self.assertIn("still require named human approval", finalizer)
        self.assertIn("No promotion or operational application was attempted", finalizer)

    def test_v19_notebooks_are_hidden_deterministic_and_pipeline_builds_catalog(self) -> None:
        for path in sorted(ROOT.glob("nih_tracker_colab_v19_*.ipynb")):
            notebook = nbformat.read(path, as_version=4)
            nbformat.validate(notebook)
            self.assertTrue(notebook.cells)
            for cell in notebook.cells:
                if cell.cell_type == "code":
                    ast.parse(cell.source)
                    self.assertEqual(cell.outputs, [])
                    self.assertIsNone(cell.execution_count)
                    self.assertEqual(cell.metadata.get("cellView"), "form")
                    self.assertTrue(cell.metadata.get("jupyter", {}).get("source_hidden"))
        pipeline = (ROOT / "nih_tracker_colab_v19_pipeline.ipynb").read_text(encoding="utf-8")
        self.assertIn("step_build_catalog.py", pipeline)
        self.assertLess(pipeline.index("step_build_catalog.py"), pipeline.index("step_build_frontend.py"))
        maintenance = (ROOT / "nih_tracker_colab_v19_maintenance.ipynb").read_text(encoding="utf-8")
        self.assertNotIn("step_remove_nonproduction_environments.py", maintenance)
        self.assertNotIn("DELETE_DEV_STAGING_ENVIRONMENTS", maintenance)

    def test_production_installer_collects_v19_notebooks_and_schemas_only(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary) / "NIH_Tracker"
            project_layout(project)
            configure(project)
            initializer = importlib.import_module("step_initialize_environments")
            release = {item.relative_path: item for item in initializer.collect_release_files(ROOT)}
            self.assertIn("nih_tracker_colab_v19_pipeline.ipynb", release)
            self.assertIn("schemas/catalog_node.schema.json", release)
            self.assertNotIn("nih_tracker_colab_v18_pipeline.ipynb", release)


if __name__ == "__main__":
    unittest.main()
