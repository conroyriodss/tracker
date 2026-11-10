from __future__ import annotations

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
            "resource_profile_schema",
            "step_build_resource_profiles",
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
            "NIH_TRACKER_RESOURCE_PROFILE_CACHE_READ_ONLY",
        }:
            os.environ.pop(key, None)
    os.environ["NIH_TRACKER_ENV"] = "dev"
    for environment in ("dev", "staging", "prod"):
        os.environ[f"NIH_TRACKER_ROOT_{environment.upper()}"] = str(root / environment)
    os.environ["NIH_TRACKER_SHARED_CACHE_ROOT"] = str(root / "shared" / "cache")
    os.environ["NIH_TRACKER_RESOURCE_PROFILE_CACHE_READ_ONLY"] = "0"
    return importlib.import_module("common")


def example_card() -> dict:
    return {
        "schema_version": "1.0",
        "repo_uid": "R1",
        "kind": "repository",
        "identity": {
            "name": "Example Biomedical Data Repository",
            "abbreviation": "EBDR",
            "homepage": "https://example.org/",
        },
        "links": [
            {
                "id_type": "fairsharing_doi",
                "id_value": "10.25504/FAIRsharing.test01",
                "url": "https://fairsharing.org/10.25504/FAIRsharing.test01",
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
                    "as_of": "2026-09",
                    "block_version": "2.0",
                    "review_state": "auto",
                    "sources": ["probe:v4"],
                },
                "access_class": "public",
                "api_declared": False,
                "api_evidence_state": "absent",
                "api_verified": False,
                "gated_reachable": False,
                "api": {"families": [], "endpoints": [], "documentation_links": []},
                "standards": [],
                "pid_systems": [],
                "guid_systems": [],
                "relevant_links": [],
            }
        },
        "curation": {"review_state": "human_confirmed", "revision": 1},
    }


class ResourceProfileSchemaTests(unittest.TestCase):
    def test_website_metadata_is_bounded_and_does_not_mine_footer_people(self):
        with tempfile.TemporaryDirectory() as temporary:
            configure(Path(temporary))
            schema = importlib.import_module("resource_profile_schema")
            html = b'''<!doctype html><html lang="en"><head>
            <meta name="description" content="A repository of genomic and clinical research datasets.">
            <meta name="keywords" content="genomics, clinical research">
            <link rel="service-doc" href="/api/docs">
            <script type="application/ld+json">{
              "@context":"https://schema.org",
              "@type":"DataCatalog",
              "description":"Official biomedical data catalog.",
              "publisher":{"@type":"Organization","name":"Example University","url":"https://example.edu"},
              "about":[{"name":"Genomics"}],
              "keywords":["rare disease"],
              "dataset":[{"@type":"Dataset","encodingFormat":"text/csv"}],
              "conformsTo":{"name":"DCAT 3","url":"https://www.w3.org/TR/vocab-dcat-3/"}
            }</script></head><body><footer>Jane Person person@example.org</footer></body></html>'''
            digest = hashlib.sha256(html).hexdigest()
            profile = schema.parse_website_metadata(
                html,
                requested_url="https://example.org/",
                final_url="https://example.org/",
                content_sha256=digest,
                as_of="2026-09",
            )
            summary = schema.resource_profile_summary(profile)
            self.assertEqual(summary["description"], "Official biomedical data catalog.")
            self.assertIn("Data catalog", summary["resource_types"])
            self.assertIn("Dataset", summary["data_asset_types"])
            self.assertIn("Genomics", summary["subject_areas"])
            self.assertIn("text/csv", summary["data_formats"])
            self.assertEqual(profile["organizations"][0]["name"], "Example University")
            serialized = json.dumps(profile)
            self.assertNotIn("person@example.org", serialized)
            self.assertEqual(profile["standards"][0]["name"], "DCAT 3")
            self.assertEqual(
                profile["api_documentation_links"][0]["url"],
                "https://example.org/api/docs",
            )

    def test_website_description_fallback_is_limited_to_main_content(self):
        with tempfile.TemporaryDirectory() as temporary:
            configure(Path(temporary))
            schema = importlib.import_module("resource_profile_schema")
            html = b'''<!doctype html><html><body>
            <nav><p>This navigation paragraph is intentionally long enough to be ignored by the bounded extractor.</p></nav>
            <main><p>This repository provides carefully curated biomedical research datasets, associated metadata, and controlled access services for qualified investigators.</p></main>
            <footer><p>Copyright Example Person person@example.org. This footer paragraph is also intentionally long enough to be ignored.</p></footer>
            </body></html>'''
            digest = hashlib.sha256(html).hexdigest()
            profile = schema.parse_website_metadata(
                html,
                requested_url="https://example.org/",
                final_url="https://example.org/",
                content_sha256=digest,
                as_of="2026-09",
            )
            description = schema.resource_profile_summary(profile)["description"]
            self.assertTrue(description.startswith("This repository provides carefully curated"))
            serialized = json.dumps(profile)
            self.assertNotIn("person@example.org", serialized)
            self.assertIn("html:main p:first_paragraph", serialized)

    def test_fairsharing_and_re3data_profiles_preserve_roles_and_controlled_terms(self):
        with tempfile.TemporaryDirectory() as temporary:
            configure(Path(temporary))
            schema = importlib.import_module("resource_profile_schema")
            fairsharing = {
                "id": "record-1",
                "attributes": {
                    "doi": "10.25504/FAIRsharing.test01",
                    "description": "A FAIRsharing registry description.",
                    "type": {"name": "Database"},
                    "objectTypes": [{"name": "Dataset"}],
                    "subjects": [{"name": "Cancer"}],
                    "domains": [{"name": "Biomedical science"}],
                    "organisations": [
                        {
                            "name": "Example Institute",
                            "ror": "https://ror.org/012345678",
                            "role": "maintainer",
                        }
                    ],
                    "isMaintained": True,
                    "accessMethods": [{"name": "REST"}],
                    "recordAssociations": [
                        {
                            "record": {
                                "name": "MIAME",
                                "registry": "standards",
                                "doi": "10.25504/FAIRsharing.32b10v",
                            },
                            "relation": {"name": "implements"},
                        }
                    ],
                },
            }
            fs_profile = schema.parse_fairsharing_record(
                fairsharing,
                doi="10.25504/FAIRsharing.test01",
                as_of="2026-09",
            )
            self.assertEqual(fs_profile["organizations"][0]["roles"], ["maintainer"])
            self.assertTrue(fs_profile["maintainer_status"]["value"])
            self.assertEqual(fs_profile["standards"][0]["name"], "MIAME")
            self.assertIn("REST", fs_profile["api_families"])

            re3data = {
                "r3d_id": "r3d100000001",
                "record_url": "https://www.re3data.org/repository/r3d100000001",
                "fetched_at": "2026-09-16T00:00:00+00:00",
                "description": "A re3data registry description.",
                "repository_types": ["disciplinary"],
                "content_types": ["Scientific and statistical data formats"],
                "subjects": [
                    {"name": "Life Sciences", "scheme": "DFG", "identifier": "1"}
                ],
                "institutions": [
                    {
                        "name": "Example Institute",
                        "roles": ["provider"],
                        "country": "US",
                        "ror": "https://ror.org/012345678",
                    }
                ],
                "languages": ["eng"],
            }
            r3_profile = schema.parse_re3data_profile(
                re3data, r3d_id="r3d100000001"
            )
            self.assertEqual(r3_profile["organizations"][0]["country"], "US")
            self.assertIn("provider", r3_profile["organizations"][0]["roles"])
            self.assertEqual(r3_profile["subject_areas"][0]["scheme"], "DFG")


class ResourceProfileBuildTests(unittest.TestCase):
    def _write_inputs(self, common, schema):
        fairsharing_export = Path(common.INP) / "FAIRSHARING_EXPORT.csv"
        pd.DataFrame(
            [
                {
                    "Name": "Example Biomedical Data Repository",
                    "Abbreviation": "EBDR",
                    "FAIRsharing DOI": "10.25504/FAIRsharing.test01",
                    "Homepage": "https://example.org/",
                    "Description": "Export fallback description.",
                    "Object Types": "Dataset",
                    "Subjects": "Biomedical science",
                }
            ]
        ).to_csv(fairsharing_export, index=False)

        fair_record = {
            "id": "record-1",
            "attributes": {
                "doi": "10.25504/FAIRsharing.test01",
                "description": "Exact FAIRsharing description.",
                "type": {"name": "Database"},
                "objectTypes": [{"name": "Dataset"}],
                "subjects": [{"name": "Cancer"}],
                "organisations": [
                    {
                        "name": "Example Institute",
                        "ror": "https://ror.org/012345678",
                        "role": "maintainer",
                    }
                ],
                "isMaintained": True,
                "accessMethods": [{"name": "REST"}],
                "recordAssociations": [
                    {
                        "record": {
                            "name": "MIAME",
                            "registry": "standards",
                            "doi": "10.25504/FAIRsharing.32b10v",
                        },
                        "relation": {"name": "implements"},
                    }
                ],
            },
        }
        fair_profile = schema.parse_fairsharing_record(
            fair_record,
            doi="10.25504/FAIRsharing.test01",
            as_of="2026-09",
        )
        fair_cache = Path(common.SHARED_CACHE_ROOT) / "fairsharing_resource_profile_cache.json"
        fair_cache.parent.mkdir(parents=True, exist_ok=True)
        fair_cache.write_text(
            json.dumps(
                {
                    "schema": "nih-tracker-fairsharing-resource-profile-cache/v1",
                    "records": {
                        "10.25504/fairsharing.test01": {
                            "doi": "10.25504/FAIRsharing.test01",
                            "fetched_at": "2026-09-16T00:00:00+00:00",
                            "response_sha256": "a" * 64,
                            "snapshot_path": "/frozen/fairsharing.json.gz",
                            "record": fair_record,
                            "parsed_profile": fair_profile,
                        }
                    },
                }
            ),
            encoding="utf-8",
        )

        html = b'''<!doctype html><html lang="en"><head>
        <meta name="description" content="Website description of the repository.">
        <link rel="service-doc" href="/api/docs">
        <script type="application/ld+json">{
          "@context":"https://schema.org","@type":"DataCatalog",
          "publisher":{"@type":"Organization","name":"Example University"},
          "about":{"name":"Genomics"},
          "dataset":{"@type":"Dataset","encodingFormat":"text/csv"},
          "conformsTo":{"name":"DCAT 3","url":"https://www.w3.org/TR/vocab-dcat-3/"}
        }</script></head><body></body></html>'''
        digest = hashlib.sha256(html).hexdigest()
        website_profile = schema.parse_website_metadata(
            html,
            requested_url="https://example.org/",
            final_url="https://example.org/",
            content_sha256=digest,
            as_of="2026-09",
        )
        website_cache = Path(common.SHARED_CACHE_ROOT) / "website_resource_profile_cache.json"
        website_cache.write_text(
            json.dumps(
                {
                    "schema": "nih-tracker-website-resource-profile-cache/v1",
                    "records": {
                        "https://example.org/": {
                            "requested_url": "https://example.org/",
                            "final_url": "https://example.org/",
                            "fetched_at": "2026-09-16T00:00:00+00:00",
                            "response_sha256": digest,
                            "snapshot_path": "/frozen/homepage.html.gz",
                            "parsed_profile": website_profile,
                        }
                    },
                }
            ),
            encoding="utf-8",
        )

        re3_cache = Path(common.SHARED_CACHE_ROOT) / "re3data_interoperability_cache.json"
        re3_cache.write_text(
            json.dumps(
                {
                    "schema": "nih-tracker-re3data-interoperability-cache/v2",
                    "records": {
                        "r3d100000001": {
                            "profile_schema_version": "1.0",
                            "r3d_id": "r3d100000001",
                            "record_url": "https://www.re3data.org/repository/r3d100000001",
                            "fetched_at": "2026-09-16T00:00:00+00:00",
                            "response_sha256": "b" * 64,
                            "api_endpoint": "https://www.re3data.org/api/v1/repository/r3d100000001",
                            "description": "re3data fallback description.",
                            "repository_types": ["disciplinary"],
                            "content_types": ["Scientific and statistical data formats"],
                            "subjects": [{"name": "Life Sciences", "scheme": "DFG"}],
                            "languages": ["eng"],
                            "institutions": [
                                {
                                    "name": "Example Institute",
                                    "roles": ["provider"],
                                    "country": "US",
                                    "ror": "https://ror.org/012345678",
                                }
                            ],
                        }
                    },
                }
            ),
            encoding="utf-8",
        )
        return fairsharing_export, fair_cache, website_cache, re3_cache

    def test_dry_run_apply_and_idempotence_with_source_bound_profiles(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            common = configure(root)
            common.save_card("R1", example_card())
            schema = importlib.import_module("resource_profile_schema")
            fair_export, fair_cache, website_cache, re3_cache = self._write_inputs(
                common, schema
            )
            builder = importlib.import_module("step_build_resource_profiles")
            before = Path(common.CARDS, "R1.yaml").read_bytes()
            arguments = dict(
                fairsharing_export_path=fair_export,
                curated_path=Path(common.INP) / "resource_profile_curated.csv",
                fairsharing_cache_path=fair_cache,
                website_cache_path=website_cache,
                re3data_cache_path=re3_cache,
                snapshot_root=Path(common.SHARED_CACHE_ROOT) / "source_snapshots",
                fairsharing_policy="none",
                fairsharing_max_age_days=30,
                website_policy="none",
                website_max_age_days=90,
                website_max_fetches=0,
                website_timeout=5,
                website_max_bytes=500_000,
                strict_fairsharing=False,
                strict_website=False,
            )
            ok, dry_outputs, dry_run = builder.run(apply=False, **arguments)
            self.assertTrue(ok)
            self.assertEqual(Path(common.CARDS, "R1.yaml").read_bytes(), before)
            self.assertEqual(dry_outputs["changed"], 1)
            self.assertTrue((dry_run / "resource_profile_gaps.csv").is_file())
            snapshot = json.loads((dry_run / "source_snapshot.json").read_text())
            self.assertEqual(
                snapshot["fairsharing_export"]["sha256"],
                common.sha256_file(fair_export),
            )
            self.assertEqual(
                snapshot["records_used"]["R1"]["sources"]["website"]["response_sha256"],
                hashlib.sha256(
                    b'''<!doctype html><html lang="en"><head>\n        <meta name="description" content="Website description of the repository.">\n        <link rel="service-doc" href="/api/docs">\n        <script type="application/ld+json">{\n          "@context":"https://schema.org","@type":"DataCatalog",\n          "publisher":{"@type":"Organization","name":"Example University"},\n          "about":{"name":"Genomics"},\n          "dataset":{"@type":"Dataset","encodingFormat":"text/csv"},\n          "conformsTo":{"name":"DCAT 3","url":"https://www.w3.org/TR/vocab-dcat-3/"}\n        }</script></head><body></body></html>'''
                ).hexdigest(),
            )

            ok, apply_outputs, _ = builder.run(apply=True, **arguments)
            self.assertTrue(ok)
            self.assertEqual(apply_outputs["applied"], 1)
            card = yaml.safe_load(Path(common.CARDS, "R1.yaml").read_text())
            profile = card["blocks"]["resource_profile"]
            summary = schema.resource_profile_summary(profile)
            self.assertEqual(summary["description"], "Website description of the repository.")
            self.assertIn("Dataset", summary["data_asset_types"])
            self.assertIn("Cancer", summary["subject_areas"])
            self.assertIn("Genomics", summary["subject_areas"])
            self.assertIn("Example Institute", summary["organizations"])
            organization = next(
                item for item in profile["organizations"] if item["name"] == "Example Institute"
            )
            self.assertEqual(set(organization["roles"]), {"maintainer", "provider"})
            standards = card["blocks"]["interoperability"]["standards"]
            standard_names = {item["name"] for item in standards}
            self.assertIn("DCAT 3", standard_names)
            self.assertIn("MIAME", standard_names)
            self.assertTrue(
                card["blocks"]["interoperability"]["api"]["documentation_links"]
            )

            ok, second_outputs, _ = builder.run(apply=True, **arguments)
            self.assertTrue(ok)
            self.assertEqual(second_outputs["changed"], 0)
            self.assertEqual(second_outputs["applied"], 0)


class V15PublicArtifactTests(unittest.TestCase):
    def test_spine_and_frontend_render_profile_without_quality_score(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            common = configure(root)
            card = example_card()
            card["blocks"]["resource_profile"] = {
                "_meta": {
                    "block_version": "1.0",
                    "as_of": "2026-09",
                    "review_state": "auto",
                    "tier": "registry",
                    "sources": ["fairsharing:10.25504/FAIRsharing.test01"],
                },
                "resource_types": [
                    {"name": "Data repository", "source": "fairsharing:test", "tier": "registry"}
                ],
                "data_asset_types": [
                    {"name": "Dataset", "source": "fairsharing:test", "tier": "registry"}
                ],
                "descriptions": [
                    {
                        "text": "A public description.",
                        "source": "website:abc",
                        "source_url": "https://example.org/",
                        "tier": "attestation",
                    }
                ],
                "organizations": [
                    {
                        "name": "Example Institute",
                        "roles": ["maintainer", "provider"],
                        "country": "US",
                        "ror": "https://ror.org/012345678",
                        "source": "fairsharing:test",
                        "tier": "registry",
                    }
                ],
                "subject_areas": [
                    {"name": "Genomics", "source": "fairsharing:test", "tier": "registry"}
                ],
                "data_formats": [
                    {"name": "text/csv", "source": "website:abc", "tier": "attestation"}
                ],
                "taxonomies": [],
                "keywords": [],
                "languages": [],
                "service_roles": [],
                "access_conditions": [],
                "relevant_links": [],
                "maintainer_status": {
                    "value": True,
                    "source": "fairsharing:test",
                    "tier": "registry",
                },
            }
            card["blocks"]["interoperability"]["standards"] = [
                {
                    "name": "DCAT 3",
                    "url": "https://www.w3.org/TR/vocab-dcat-3/",
                    "role": "metadata exposure",
                    "version": "3",
                    "evidence_url": "https://example.org/docs",
                    "source": "website:abc",
                    "tier": "attestation",
                    "declared": True,
                    "verified": False,
                }
            ]
            common.save_card("R1", card)
            spine_builder = importlib.import_module("step_build_spine")
            row = spine_builder.flatten_card(
                "R1", card, ["identity", "resource_profile", "interoperability"], 1
            )
            self.assertNotIn("completeness", row)
            self.assertEqual(row["data_asset_types"], "Dataset")
            self.assertEqual(row["subject_areas"], "Genomics")
            self.assertEqual(row["organizations"], "Example Institute")
            pd.DataFrame([row]).to_csv(Path(common.BUILD) / "spine.csv", index=False)
            output_path = Path(common.BUILD) / "index.html"
            frontend_builder = importlib.import_module("step_build_frontend")
            result = frontend_builder.build_frontend(
                frontend_path=ROOT / "frontend.html",
                spine_path=Path(common.BUILD) / "spine.csv",
                output_path=output_path,
                cards_dir=Path(common.CARDS),
            )
            rendered = output_path.read_text(encoding="utf-8")
            self.assertTrue(result["template_has_resource_profile"])
            self.assertTrue(result["template_has_stewardship"])
            self.assertIn("A public description.", rendered)
            self.assertIn("Example Institute", rendered)
            self.assertIn("physical location of every data file", rendered)
            self.assertIn("metadata exposure", rendered)
            self.assertNotIn("Completeness", rendered)

    def test_v15_notebooks_include_profile_pipeline_and_gap_browser(self):
        pipeline = (ROOT / "nih_tracker_colab_v18_pipeline.ipynb").read_text(encoding="utf-8")
        review = (ROOT / "nih_tracker_colab_v18_review_workbench.ipynb").read_text(encoding="utf-8")
        self.assertIn("step_build_resource_profiles.py", pipeline)
        self.assertIn("resource_profile_gaps.csv", review)
        self.assertIn("resource_profile_curated_template.csv", review)
        self.assertNotIn("widgets.Text(", review)

    def test_resource_profile_templates_are_release_code(self):
        with tempfile.TemporaryDirectory() as temporary:
            configure(Path(temporary))
            initializer = importlib.import_module("step_initialize_environments")
            release = {item.relative_path: item for item in initializer.collect_release_files(ROOT)}
            self.assertIn("templates/resource_profile_curated_template.csv", release)
            self.assertIn("templates/RESOURCE_PROFILE_CURATION_GUIDE.md", release)
            self.assertTrue(release["templates/resource_profile_curated_template.csv"].overwrite_allowed)


if __name__ == "__main__":
    unittest.main()
