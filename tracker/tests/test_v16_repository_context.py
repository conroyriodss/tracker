from __future__ import annotations

import importlib
import json
import hashlib
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

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
            "policy_context_schema",
            "step_build_policy_context",
            "step_build_spine",
            "step_initialize_environments",
            "verify_external_ids",
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
    os.environ["NIH_TRACKER_RUN_ID"] = "test-v16-repository-context"
    for environment in ("dev", "staging", "prod"):
        os.environ[f"NIH_TRACKER_ROOT_{environment.upper()}"] = str(root / environment)
    os.environ["NIH_TRACKER_SHARED_CACHE_ROOT"] = str(root / "shared" / "cache")
    os.environ["NIH_TRACKER_RE3DATA_CACHE_READ_ONLY"] = "0"
    return importlib.import_module("common")


def write_dbgap_definition(root: Path) -> Path:
    path = root / "dbgap_eds_definition.json"
    path.write_text(
        json.dumps(
            {
                "schema": "nih-tracker-dbgap-eds-definition/v1",
                "verified_at": "2026-09-16",
                "source_urls": [
                    "https://www.ncbi.nlm.nih.gov/gap/docs/submissionguide/",
                    "https://www.ncbi.nlm.nih.gov/gap/docs/specialstudies/",
                ],
                "interpretation": {
                    "external_entity_required": True,
                    "qualifying_eds_is_nih_cadr": True,
                    "specific_study_designation_occurs_in_dbgap_registration_system": True,
                    "designation_role": "Genomic Program Administrator",
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )
    return path


def card(uid: str, name: str, homepage: str, *, aliases: list[str] | None = None):
    return {
        "schema_version": "1.0",
        "repo_uid": uid,
        "kind": "repository",
        "identity": {
            "name": name,
            "homepage": homepage,
            "aliases": list(aliases or []),
        },
        "links": [],
        "blocks": {},
        "curation": {"review_state": "human_confirmed", "revision": 1},
    }


class Re3dataV16Tests(unittest.TestCase):
    def test_catalog_alias_is_retained_and_exact_alias_match_is_direct_evidence(self):
        with tempfile.TemporaryDirectory() as temporary:
            configure(Path(temporary))
            verifier = importlib.import_module("verify_external_ids")
            xml = b'''<?xml version="1.0"?>
            <r3d:repository xmlns:r3d="http://www.re3data.org/schema/4-0">
              <r3d:repositoryName language="eng">National Institute of Mental Health Data Archive</r3d:repositoryName>
              <r3d:additionalName language="eng">NIMH Data Archive</r3d:additionalName>
              <r3d:additionalName language="eng">NDA</r3d:additionalName>
              <r3d:repositoryURL>https://nda.nih.gov/</r3d:repositoryURL>
            </r3d:repository>'''
            details = verifier.parse_re3data_record_details(xml)
            self.assertEqual(details["name"], "National Institute of Mental Health Data Archive")
            self.assertIn("NIMH Data Archive", details["aliases"])
            candidates = verifier.search_re3data_candidates_from_catalog(
                [
                    {
                        "id": "r3d100012653",
                        "name": details["name"],
                        "aliases": details["aliases"],
                        "homepage": details["homepage"],
                        "fairsharing_doi": [],
                        "rrid": [],
                    }
                ],
                "NIMH Data Archive",
                "https://nda.nih.gov/",
                repo_uid="R103",
                card_names=["NIMH Data Archive", "NDA"],
            )
            self.assertEqual(candidates[0]["id_value"], "r3d100012653")
            self.assertEqual(candidates[0]["match_basis"], "exact_normalized_alias")
            self.assertEqual(candidates[0]["confidence"], 1.0)
            self.assertEqual(
                verifier.has_independent_evidence(candidates[0]),
                (True, "exact_or_acronym_name_match"),
            )

    def test_only_missing_replaces_url_only_re3data_placeholder(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            common = configure(root)
            example = card(
                "R103",
                "NIMH Data Archive",
                "https://nda.nih.gov/",
                aliases=["NDA"],
            )
            example["links"] = [
                {
                    "id_type": "re3data",
                    "id_value": "",
                    "url": "https://www.re3data.org/search?query=NIMH%20Data%20Archive",
                    "tier": "registry",
                }
            ]
            common.save_card("R103", example)
            seed_path = root / "seeds.csv"
            pd.DataFrame(
                [
                    {
                        "repo_uid": "R103",
                        "expected_name": "NIMH Data Archive",
                        "r3d_id": "r3d100012653",
                        "record_url": "https://www.re3data.org/repository/r3d100012653",
                        "evidence_basis": "project_owner_supplied_exact_record",
                        "reviewed_at": "2026-09-16",
                    }
                ]
            ).to_csv(seed_path, index=False)
            verifier = importlib.import_module("verify_external_ids")
            verified = (
                200,
                "https://nda.nih.gov/",
                "National Institute of Mental Health Data Archive",
                0.85,
                "verified",
                "homepage_host",
                "test fixture",
            )
            with mock.patch.object(verifier, "get_re3data_catalog", return_value=[]), mock.patch.object(
                verifier, "verify_re3data", return_value=verified
            ):
                rows, proposals = verifier.run_scan(
                    Path(common.CARDS),
                    delay=0.5,
                    limit=0,
                    mode="search",
                    active_id_types={"re3data"},
                    only_missing=True,
                    re3data_catalog_cache=str(root / "catalog.json"),
                    re3data_cache_read_only=False,
                    re3data_seed_mappings=str(seed_path),
                )
            self.assertTrue(rows)
            self.assertEqual(len(proposals), 1)
            self.assertEqual(proposals[0]["action"], "fill_search_only_blank")
            self.assertEqual(proposals[0]["proposed_link"]["id_value"], "r3d100012653")
            self.assertEqual(
                proposals[0]["existing_link"]["url"],
                "https://www.re3data.org/search?query=NIMH%20Data%20Archive",
            )

    def test_re3data_cross_identifier_match_survives_name_and_host_drift(self):
        with tempfile.TemporaryDirectory() as temporary:
            configure(Path(temporary))
            verifier = importlib.import_module("verify_external_ids")
            candidates = verifier.search_re3data_candidates_from_catalog(
                [
                    {
                        "id": "r3d100099999",
                        "name": "Historic Repository Name",
                        "aliases": [],
                        "homepage": "https://legacy.example.org/",
                        "fairsharing_doi": [],
                        "rrid": ["RRID:SCR_007345"],
                    }
                ],
                "Current Repository Name",
                "https://current.example.org/",
                repo_uid="R127",
                card_names=["Current Repository Name"],
                card_identifiers={"rrid": {"RRID:SCR_007345"}},
            )
            self.assertEqual(candidates[0]["id_value"], "r3d100099999")
            self.assertEqual(
                candidates[0]["match_basis"], "cross_registry_identifier_rrid"
            )
            self.assertEqual(candidates[0]["confidence"], 0.95)
            self.assertEqual(
                verifier.has_independent_evidence(candidates[0]),
                (True, "cross_registry_identifier_match"),
            )

    def test_release_seed_file_contains_three_user_verified_records(self):
        frame = pd.read_csv(ROOT / "reference" / "re3data_confirmed_mappings.csv", dtype=str)
        actual = dict(zip(frame["repo_uid"], frame["r3d_id"]))
        self.assertEqual(actual["R103"], "r3d100012653")
        self.assertEqual(actual["R1"], "r3d100012837")
        self.assertEqual(actual["R127"], "r3d100011561")


class PolicyContextV16Tests(unittest.TestCase):
    def test_dry_run_apply_idempotence_and_eds_separation(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            common = configure(root)
            common.save_card(
                "R1",
                card(
                    "R1",
                    "Federal Interagency Traumatic Brain Injury Research (FITBIR) Informatics System",
                    "https://fitbir.nih.gov/",
                    aliases=["FITBIR"],
                ),
            )
            common.save_card(
                "R2",
                card(
                    "R2",
                    "Example External Repository",
                    "https://example.org/",
                    aliases=["EER"],
                ),
            )
            cadr_path = root / "cadr.csv"
            pd.DataFrame(
                [
                    {
                        "official_name": "Federal Interagency Traumatic Brain Injury Research (FITBIR) Informatics System",
                        "access_system": "FITBIR Access System (built from BRICS)",
                        "nih_designated_genomic_data_repository": "N",
                        "url": "https://fitbir.nih.gov/",
                        "repo_uid": "R1",
                        "match_terms": "FITBIR",
                        "source_page_last_updated": "2026-07-27",
                        "source_url": "https://grants.nih.gov/policy-and-compliance/policy-topics/sharing-policies/accessing-data/requirements",
                    }
                ]
            ).to_csv(cadr_path, index=False)
            studies_path = root / "dbGaP_Studies.csv"
            pd.DataFrame(
                [
                    {
                        "accession": "phs000001.v1.p1",
                        "name": "FITBIR traumatic brain injury study",
                        "description": "",
                        "Collections": "",
                        "Release Date": "2026-09-01",
                    },
                    {
                        "accession": "phs000002.v1.p1",
                        "name": "Example External Repository cohort",
                        "description": "",
                        "Collections": "",
                        "Release Date": "2026-09-02",
                    },
                ]
            ).to_csv(studies_path, index=False)
            curated_path = root / "missing_curated.csv"
            builder = importlib.import_module("step_build_policy_context")

            ok, coverage, _ = builder.run(
                cadr_snapshot=cadr_path,
                dbgap_studies=studies_path,
                dbgap_definition=write_dbgap_definition(root),
                curated_eds=curated_path,
                apply=False,
            )
            self.assertTrue(ok)
            self.assertEqual(coverage["nih_cadr_listed"], 1)
            self.assertNotIn("policy_context", common.all_cards()["R1"]["blocks"])

            ok, coverage, _ = builder.run(
                cadr_snapshot=cadr_path,
                dbgap_studies=studies_path,
                dbgap_definition=write_dbgap_definition(root),
                curated_eds=curated_path,
                apply=True,
            )
            self.assertTrue(ok)
            self.assertEqual(coverage["applied"], 2)
            cards = common.all_cards()
            fitbir = cards["R1"]["blocks"]["policy_context"]
            other = cards["R2"]["blocks"]["policy_context"]
            self.assertEqual(fitbir["nih_cadr"]["status"], "listed")
            self.assertEqual(
                fitbir["dbgap_external_data_source"]["status"],
                "considered_eds_via_cadr_definition",
            )
            self.assertIsNone(
                fitbir["dbgap_external_data_source"][
                    "specific_registration_established"
                ]
            )
            self.assertFalse(
                fitbir["dbgap_external_data_source"]["uses_dbgap_access_system"]
            )
            self.assertEqual(
                len(fitbir["dbgap_external_data_source"]["definition_source_urls"]),
                2,
            )
            self.assertEqual(other["nih_cadr"]["status"], "not_listed")
            self.assertEqual(
                other["dbgap_external_data_source"]["status"], "not_established"
            )
            self.assertGreater(other["dbgap_public_studies"]["study_count"], 0)
            self.assertIn(
                "does not by itself establish",
                other["dbgap_public_studies"]["limitation"],
            )

            ok, coverage, _ = builder.run(
                cadr_snapshot=cadr_path,
                dbgap_studies=studies_path,
                dbgap_definition=write_dbgap_definition(root),
                curated_eds=curated_path,
                apply=True,
            )
            self.assertTrue(ok)
            self.assertEqual(coverage["changed"], 0)
            self.assertEqual(coverage["applied"], 0)

    def test_dbgap_itself_is_cadr_but_not_an_external_source_to_itself(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            common = configure(root)
            common.save_card(
                "R207",
                card(
                    "R207",
                    "Database for Genotypes and Phenotypes (dbGaP)",
                    "https://www.ncbi.nlm.nih.gov/gap/",
                    aliases=["dbGaP"],
                ),
            )
            cadr_path = root / "cadr.csv"
            pd.DataFrame(
                [
                    {
                        "official_name": "Database of Genotypes and Phenotypes (dbGaP)",
                        "access_system": "dbGaP Access System",
                        "nih_designated_genomic_data_repository": "Y",
                        "url": "https://www.ncbi.nlm.nih.gov/gap/",
                        "repo_uid": "R207",
                        "match_terms": "dbGaP",
                        "source_page_last_updated": "2026-07-27",
                        "source_url": "https://grants.nih.gov/policy-and-compliance/policy-topics/sharing-policies/accessing-data/requirements",
                    }
                ]
            ).to_csv(cadr_path, index=False)
            studies_path = root / "dbGaP_Studies.csv"
            pd.DataFrame(
                [
                    {
                        "accession": "phs000001.v1.p1",
                        "name": "Example study",
                        "description": "",
                        "Collections": "",
                        "Release Date": "2026-09-01",
                    }
                ]
            ).to_csv(studies_path, index=False)
            builder = importlib.import_module("step_build_policy_context")
            ok, _, _ = builder.run(
                cadr_snapshot=cadr_path,
                dbgap_studies=studies_path,
                dbgap_definition=write_dbgap_definition(root),
                curated_eds=root / "missing.csv",
                apply=True,
            )
            self.assertTrue(ok)
            block = common.all_cards()["R207"]["blocks"]["policy_context"]
            self.assertEqual(block["nih_cadr"]["status"], "listed")
            self.assertEqual(
                block["dbgap_external_data_source"]["status"],
                "not_applicable_dbgap_repository",
            )

    def test_public_study_description_association_is_labeled_text_only(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            common = configure(root)
            common.save_card(
                "R79",
                card(
                    "R79",
                    "BioData Catalyst",
                    "https://biodatacatalyst.nhlbi.nih.gov/",
                ),
            )
            cadr_path = root / "cadr.csv"
            pd.DataFrame(
                [
                    {
                        "official_name": "BioData Catalyst",
                        "access_system": "dbGaP Access System",
                        "nih_designated_genomic_data_repository": "Y",
                        "url": "https://biodatacatalyst.nhlbi.nih.gov/",
                        "repo_uid": "R79",
                        "match_terms": "BioData Catalyst",
                        "source_page_last_updated": "2026-07-27",
                        "source_url": "https://grants.nih.gov/policy-and-compliance/policy-topics/sharing-policies/accessing-data/requirements",
                    }
                ]
            ).to_csv(cadr_path, index=False)
            studies_path = root / "dbGaP_Studies.csv"
            pd.DataFrame(
                [
                    {
                        "accession": "phs000001.v1.p1",
                        "name": "Example cohort",
                        "description": "Accessing data through the BioData Catalyst platform.",
                        "Collections": "",
                        "Release Date": "2026-09-01",
                    }
                ]
            ).to_csv(studies_path, index=False)
            builder = importlib.import_module("step_build_policy_context")
            ok, _, _ = builder.run(
                cadr_snapshot=cadr_path,
                dbgap_studies=studies_path,
                dbgap_definition=write_dbgap_definition(root),
                curated_eds=root / "missing.csv",
                apply=True,
            )
            self.assertTrue(ok)
            studies = common.all_cards()["R79"]["blocks"]["policy_context"][
                "dbgap_public_studies"
            ]
            self.assertEqual(studies["study_count"], 1)
            self.assertIn("description", studies["match_fields"])
            self.assertIn("does not by itself establish", studies["limitation"])

    def test_release_cadr_and_dbgap_snapshots_are_pinned(self):
        cadr = pd.read_csv(ROOT / "reference" / "nih_cadr_registry.csv", dtype=str).fillna("")
        studies = pd.read_csv(ROOT / "reference" / "dbGaP_Studies.csv", dtype=str).fillna("")
        self.assertEqual(len(cadr), 39)
        self.assertTrue((cadr["source_page_last_updated"] == "2026-07-27").all())
        self.assertEqual(len(studies), 3647)
        self.assertNotIn("External Data Source", studies.columns)
        self.assertNotIn("EDS", studies.columns)
        definition_path = ROOT / "reference" / "dbgap_eds_definition.json"
        definition = json.loads(definition_path.read_text(encoding="utf-8"))
        self.assertTrue(definition["interpretation"]["external_entity_required"])
        self.assertTrue(definition["interpretation"]["qualifying_eds_is_nih_cadr"])
        definition_meta = json.loads(
            (ROOT / "reference" / "dbgap_eds_definition.metadata.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(
            hashlib.sha256(definition_path.read_bytes()).hexdigest(),
            definition_meta["sha256"],
        )

    def test_reference_snapshots_ship_but_do_not_overwrite_live_evidence(self):
        with tempfile.TemporaryDirectory() as temporary:
            configure(Path(temporary))
            initializer = importlib.import_module("step_initialize_environments")
            release = {
                item.relative_path: item
                for item in initializer.collect_release_files(ROOT)
            }
            for relative in (
                "reference/nih_cadr_registry.csv",
                "reference/dbGaP_Studies.csv",
                "reference/dbgap_eds_definition.json",
                "reference/re3data_confirmed_mappings.csv",
            ):
                self.assertIn(relative, release)
                # v19.2 installs missing snapshots, preserving newer evidence
                # and curated mappings during a code-only refresh.
                self.assertFalse(release[relative].overwrite_allowed)

    def test_frontend_exposes_policy_context_without_completeness_score(self):
        text = (ROOT / "frontend.html").read_text(encoding="utf-8")
        self.assertIn('id="fCadr"', text)
        self.assertIn('id="fEds"', text)
        self.assertIn("NIH controlled-access and dbGaP context", text)
        self.assertIn(
            "Public study-title, description, or collection text does not by itself establish",
            text,
        )
        self.assertNotIn('data-k="completeness"', text)
        self.assertNotIn("complete</div>", text)

    def test_spine_flattens_policy_context(self):
        with tempfile.TemporaryDirectory() as temporary:
            configure(Path(temporary))
            spine = importlib.import_module("step_build_spine")
            example = card("R1", "FITBIR", "https://fitbir.nih.gov/")
            example["blocks"]["policy_context"] = {
                "nih_cadr": {
                    "status": "listed",
                    "access_system": "FITBIR Access System",
                    "nih_designated_genomic_data_repository": False,
                },
                "dbgap_external_data_source": {
                    "status": "considered_eds_via_cadr_definition",
                    "uses_dbgap_access_system": False,
                    "specific_registration_established": None,
                },
                "dbgap_public_studies": {"study_count": 3},
            }
            row = spine.flatten_card(
                "R1",
                example,
                ["identity", "policy_context"],
                min_rank=1,
            )
            self.assertEqual(row["nih_cadr_status"], "listed")
            self.assertEqual(row["is_nih_cadr"], 1)
            self.assertEqual(
                row["dbgap_eds_status"], "considered_eds_via_cadr_definition"
            )
            self.assertEqual(row["dbgap_public_study_count"], 3)


if __name__ == "__main__":
    unittest.main()
