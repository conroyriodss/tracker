from __future__ import annotations

import base64
import hashlib
import importlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

import pandas as pd
import yaml

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))


def clear_modules() -> None:
    for name in list(sys.modules):
        if name in {
            "common",
            "interoperability_schema",
            "step_build_interoperability",
            "step_build_spine",
            "step_build_frontend",
            "step_initialize_environments",
        }:
            sys.modules.pop(name, None)


def configure(root: Path):
    clear_modules()
    for key in list(os.environ):
        if key.startswith("NIH_TRACKER_ROOT_") or key in {
            "NIH_TRACKER",
            "NIH_TRACKER_ENV",
            "NIH_TRACKER_RUN_ID",
            "NIH_TRACKER_ALLOW_PROJECT_OVERRIDE",
            "NIH_TRACKER_SHARED_CACHE_ROOT",
            "NIH_TRACKER_PROD_WRITE_CONFIRMATION",
            "NIH_TRACKER_ALLOW_NONPROD_CREDENTIALS",
            "NIH_TRACKER_RE3DATA_CACHE_READ_ONLY",
        }:
            os.environ.pop(key, None)
    os.environ["NIH_TRACKER_ENV"] = "dev"
    for environment in ("dev", "staging", "prod"):
        os.environ[f"NIH_TRACKER_ROOT_{environment.upper()}"] = str(root / environment)
    os.environ["NIH_TRACKER_SHARED_CACHE_ROOT"] = str(root / "shared" / "cache")
    return importlib.import_module("common")


def example_card() -> dict:
    return {
        "schema_version": "1.0",
        "repo_uid": "R1",
        "kind": "repository",
        "identity": {
            "name": "Federal Interagency Traumatic Brain Injury Research (FITBIR) Informatics System",
            "homepage": "https://fitbir.nih.gov/",
        },
        "links": [
            {
                "id_type": "csv_id",
                "id_value": "1",
                "url": None,
                "tier": "registry",
            },
            {
                "id_type": "re3data",
                "id_value": "r3d100000001",
                "url": "https://www.re3data.org/repository/r3d100000001",
                "tier": "registry",
            },
        ],
        "blocks": {
            "interoperability": {
                "_meta": {
                    "as_of": "2026-07",
                    "block_version": "1.1",
                    "review_state": "auto",
                    "sources": ["probe:v4"],
                },
                "access_class": "public",
                "api_declared": True,
                "api_evidence_state": "declared_unverified",
                "api_verified": False,
                "gated_reachable": False,
                "standards": [
                    {
                        "declared": True,
                        "name": "openapi",
                        "source_declared": "declared_api",
                        "verified": False,
                    }
                ],
            }
        },
        "curation": {"review_state": "human_confirmed", "revision": 2},
    }


class SchemaTests(unittest.TestCase):
    def test_legacy_schema_is_normalized_and_guid_is_separate_from_pid(self):
        with tempfile.TemporaryDirectory() as temporary:
            configure(Path(temporary))
            schema = importlib.import_module("interoperability_schema")
            block = schema.normalize_interoperability(
                example_card()["blocks"]["interoperability"], repo_uid="R1"
            )
            self.assertEqual(block["_meta"]["block_version"], "2.0")
            self.assertEqual(block["standards"][0]["name"], "OpenAPI Specification")
            self.assertEqual(len(block["guid_systems"]), 1)
            self.assertEqual(block["guid_systems"][0]["scope"], "research_participant")
            self.assertEqual(block["pid_systems"], [])
            self.assertTrue(block["guid_systems"][0]["documentation_url"].startswith("https://"))

    def test_re3data_schema_4_fields_are_parsed(self):
        with tempfile.TemporaryDirectory() as temporary:
            configure(Path(temporary))
            schema = importlib.import_module("interoperability_schema")
            xml = b'''<?xml version="1.0" encoding="UTF-8"?>
            <r3d:repository xmlns:r3d="http://www.re3data.org/schema/4-0">
              <r3d:repositoryName language="eng">Example Repository</r3d:repositoryName>
              <r3d:repositoryURL>https://example.org/</r3d:repositoryURL>
              <r3d:api>
                <r3d:apiType>REST</r3d:apiType>
                <r3d:apiUrl>https://example.org/api</r3d:apiUrl>
                <r3d:apiDocumentation>https://example.org/api/docs</r3d:apiDocumentation>
              </r3d:api>
              <r3d:metadataStandard>
                <r3d:metadataStandardName>DataCite Metadata Schema</r3d:metadataStandardName>
                <r3d:metadataStandardURL>https://schema.datacite.org/</r3d:metadataStandardURL>
              </r3d:metadataStandard>
              <r3d:pidSystem>DOI</r3d:pidSystem>
              <r3d:repositoryIdentifier>
                <r3d:repositoryIdentifierType>RRID</r3d:repositoryIdentifierType>
                <r3d:repositoryIdentifierValue>RRID:SCR_000001</r3d:repositoryIdentifierValue>
              </r3d:repositoryIdentifier>
            </r3d:repository>'''
            parsed = schema.parse_re3data_record(xml, r3d_id="r3d100000001")
            self.assertEqual(parsed["name"], "Example Repository")
            self.assertEqual(parsed["api_endpoints"][0]["family"], "REST")
            self.assertEqual(parsed["standards"][0]["name"], "DataCite Metadata Schema")
            self.assertEqual(parsed["pid_systems"][0]["name"], "DOI")
            self.assertEqual(parsed["identifiers"][0]["value"], "RRID:SCR_000001")


class InteroperabilityBuildTests(unittest.TestCase):
    def test_re3data_fetch_pins_exact_response_bytes(self):
        with tempfile.TemporaryDirectory() as temporary:
            configure(Path(temporary))
            builder = importlib.import_module("step_build_interoperability")
            xml = b'''<?xml version="1.0" encoding="UTF-8"?>
            <r3d:repository xmlns:r3d="http://www.re3data.org/schema/4-0">
              <r3d:repositoryName language="eng">Pinned Repository</r3d:repositoryName>
              <r3d:repositoryURL>https://example.org/</r3d:repositoryURL>
            </r3d:repository>'''

            class Response:
                ok = True
                content = xml
                status_code = 200
                headers = {"content-type": "application/xml"}

            class Session:
                def get(self, endpoint, headers=None, timeout=None):
                    self.endpoint = endpoint
                    return Response()

            parsed, error = builder._fetch_re3data("r3d100000001", Session())
            self.assertEqual(error, "")
            self.assertEqual(parsed["response_sha256"], hashlib.sha256(xml).hexdigest())
            self.assertEqual(base64.b64decode(parsed["raw_xml_base64"]), xml)
            self.assertEqual(parsed["content_type"], "application/xml")

    def test_dry_run_apply_and_idempotence(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            common = configure(root)
            common.save_card("R1", example_card())
            probe_path = Path(common.INP) / "repo_api_results_v4_230_strict_endpoints.csv"
            pd.DataFrame(
                [
                    {
                        "csv_id": "1",
                        "repository": "FITBIR",
                        "api_evidence_state": "verified_documented",
                        "api_verified": "true",
                        "api_declared": "true",
                        "access_class": "public",
                        "rest_api_url": "https://fitbir.nih.gov/api",
                        "openapi_documentation_url": "https://fitbir.nih.gov/api/docs",
                        "openapi_standard": "true",
                    }
                ]
            ).to_csv(probe_path, index=False)
            cache_path = root / "shared" / "cache" / "re3data_interoperability_cache.json"
            cache_path.parent.mkdir(parents=True)
            cache_path.write_text(
                json.dumps(
                    {
                        "schema": "nih-tracker-re3data-interoperability-cache/v1",
                        "records": {
                            "r3d100000001": {
                                "r3d_id": "r3d100000001",
                                "record_url": "https://www.re3data.org/repository/r3d100000001",
                                "fetched_at": "2026-09-15T00:00:00+00:00",
                                "api_endpoints": [
                                    {
                                        "url": "https://fitbir.nih.gov/re3-api",
                                        "family": "REST",
                                        "declared": True,
                                        "verified": False,
                                        "source": "re3data:r3d100000001",
                                        "tier": "registry",
                                    }
                                ],
                                "standards": [
                                    {
                                        "name": "DataCite Metadata Schema",
                                        "url": "https://schema.datacite.org/",
                                        "declared": True,
                                        "verified": False,
                                        "source": "re3data:r3d100000001",
                                        "tier": "registry",
                                    }
                                ],
                                "pid_systems": [
                                    {
                                        "name": "DOI",
                                        "url": "https://www.doi.org/",
                                        "scope": "repository_items",
                                        "source": "re3data:r3d100000001",
                                        "tier": "registry",
                                    }
                                ],
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )
            builder = importlib.import_module("step_build_interoperability")
            before = Path(common.CARDS, "R1.yaml").read_bytes()
            ok, dry_outputs, _ = builder.run(
                probe_path=probe_path,
                curated_path=Path(common.INP) / "interoperability_curated.csv",
                cache_path=cache_path,
                re3data_policy="none",
                re3data_max_age_days=30,
                apply=False,
                strict_re3data=False,
            )
            self.assertTrue(ok)
            self.assertGreater(dry_outputs["changed"], 0)
            self.assertEqual(Path(common.CARDS, "R1.yaml").read_bytes(), before)
            dry_runs = sorted((Path(common.RUNS) / "build_interoperability").glob("*/interoperability_gaps.csv"))
            self.assertTrue(dry_runs)
            gaps = pd.read_csv(dry_runs[-1], dtype=str).fillna("")
            self.assertIn("gap_type", gaps.columns)
            self.assertIn("status", gaps.columns)

            ok, apply_outputs, _ = builder.run(
                probe_path=probe_path,
                curated_path=Path(common.INP) / "interoperability_curated.csv",
                cache_path=cache_path,
                re3data_policy="none",
                re3data_max_age_days=30,
                apply=True,
                strict_re3data=False,
            )
            self.assertTrue(ok)
            self.assertEqual(apply_outputs["applied"], 1)
            card = yaml.safe_load(Path(common.CARDS, "R1.yaml").read_text(encoding="utf-8"))
            block = card["blocks"]["interoperability"]
            self.assertEqual(block["api_evidence_state"], "verified_documented")
            self.assertGreaterEqual(len(block["api"]["endpoints"]), 2)
            standard_names = {item["name"] for item in block["standards"]}
            self.assertIn("OpenAPI Specification", standard_names)
            self.assertIn("DataCite Metadata Schema", standard_names)
            self.assertNotIn("true", {name.lower() for name in standard_names})
            self.assertEqual(block["pid_systems"][0]["name"], "DOI")
            self.assertEqual(block["guid_systems"][0]["name"], "FITBIR Global Unique Identifier (GUID)")

            ok, second_outputs, _ = builder.run(
                probe_path=probe_path,
                curated_path=Path(common.INP) / "interoperability_curated.csv",
                cache_path=cache_path,
                re3data_policy="none",
                re3data_max_age_days=30,
                apply=True,
                strict_re3data=False,
            )
            self.assertTrue(ok)
            self.assertEqual(second_outputs["changed"], 0)
            self.assertEqual(second_outputs["applied"], 0)


class PublicArtifactTests(unittest.TestCase):
    def test_spine_and_frontend_have_no_completeness_score_and_render_re3data_search(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            common = configure(root)
            card = example_card()
            card["repo_uid"] = "R9"
            card["identity"]["name"] = "Search-only repository"
            card["links"] = [
                {
                    "id_type": "re3data",
                    "id_value": None,
                    "url": "https://www.re3data.org/search?query=Search-only%20repository",
                    "tier": "registry",
                }
            ]
            common.save_card("R9", card)
            spine_builder = importlib.import_module("step_build_spine")
            row = spine_builder.flatten_card("R9", card, ["identity", "interoperability"], 1)
            self.assertNotIn("completeness", row)
            self.assertNotIn("blocks_filled", row)
            pd.DataFrame([row]).to_csv(Path(common.BUILD) / "spine.csv", index=False)
            frontend_path = ROOT / "frontend.html"
            output_path = Path(common.BUILD) / "index.html"
            frontend_builder = importlib.import_module("step_build_frontend")
            result = frontend_builder.build_frontend(
                frontend_path=frontend_path,
                spine_path=Path(common.BUILD) / "spine.csv",
                output_path=output_path,
                cards_dir=Path(common.CARDS),
            )
            rendered = output_path.read_text(encoding="utf-8")
            self.assertFalse(result["template_has_completeness_score"])
            self.assertNotIn("completeness", result["spine_header"].lower())
            self.assertIn("Search re3data (candidate record not confirmed)", rendered)
            self.assertIn("Participant GUID systems", rendered)
            self.assertIn("Interoperability standards", rendered)

    def test_notebooks_include_interoperability_pipeline_and_gap_browser(self):
        pipeline = (ROOT / "nih_tracker_colab_v18_pipeline.ipynb").read_text(encoding="utf-8")
        review = (ROOT / "nih_tracker_colab_v18_review_workbench.ipynb").read_text(encoding="utf-8")
        self.assertIn("step_build_interoperability.py", pipeline)
        self.assertIn("interoperability_gaps.csv", review)
        self.assertIn("interoperability_curated_template.csv", review)
        self.assertNotIn("widgets.Text(", review)

    def test_frontend_is_release_code_not_source_project_data(self):
        with tempfile.TemporaryDirectory() as temporary:
            configure(Path(temporary))
            initializer = importlib.import_module("step_initialize_environments")
            release = {item.relative_path: item for item in initializer.collect_release_files(ROOT)}
            self.assertIn("frontend.html", release)
            self.assertTrue(release["frontend.html"].overwrite_allowed)
            self.assertEqual(initializer.SOURCE_TOP_LEVEL_FILES, ())


if __name__ == "__main__":
    unittest.main()
