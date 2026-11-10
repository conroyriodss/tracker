from __future__ import annotations

import importlib
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

MODULES = {
    "common",
    "step_update_bmic_info",
    "step_update_dbgap_info",
    "step_update_publisher_info",
}


def configure(root: Path):
    for name in list(sys.modules):
        if name in MODULES or name.startswith("step_"):
            sys.modules.pop(name, None)
    for key in list(os.environ):
        if key.startswith("NIH_TRACKER_ROOT_") or key in {
            "NIH_TRACKER",
            "NIH_TRACKER_ENV",
            "NIH_TRACKER_RUN_ID",
            "NIH_TRACKER_ALLOW_PROJECT_OVERRIDE",
            "NIH_TRACKER_SHARED_CACHE_ROOT",
            "NIH_TRACKER_PROD_WRITE_CONFIRMATION",
        }:
            os.environ.pop(key, None)
    os.environ["NIH_TRACKER_ENV"] = "dev"
    os.environ["NIH_TRACKER_RUN_ID"] = "test-v17-1"
    for env in ("dev", "staging", "prod"):
        os.environ[f"NIH_TRACKER_ROOT_{env.upper()}"] = str(root / env)
    os.environ["NIH_TRACKER_SHARED_CACHE_ROOT"] = str(root / "shared" / "cache")
    return importlib.import_module("common")


class DbgapTransportTests(unittest.TestCase):
    def test_latin1_fhir_payload_uses_declared_charset(self):
        with tempfile.TemporaryDirectory() as temporary:
            configure(Path(temporary))
            module = importlib.import_module("step_update_dbgap_info")
            payload = (
                '{"resourceType":"Bundle","entry":[{"resource":'
                '{"resourceType":"ResearchStudy","id":"phs000001",'
                '"title":"Müller Study"}}]}'
            ).encode("iso-8859-1")
            parsed, encoding, decompressed = module._decode_json_payload(
                payload,
                declared_charset="ISO-8859-1",
            )
            self.assertEqual(encoding, "iso-8859-1")
            self.assertFalse(decompressed)
            self.assertEqual(parsed["entry"][0]["resource"]["title"], "Müller Study")

    def test_fhir_page_manifest_records_encoding_and_hash(self):
        with tempfile.TemporaryDirectory() as temporary:
            configure(Path(temporary))
            module = importlib.import_module("step_update_dbgap_info")
            bundle = {
                "resourceType": "Bundle",
                "entry": [
                    {
                        "resource": {
                            "resourceType": "ResearchStudy",
                            "id": "phs000001",
                            "title": "Müller Study",
                        }
                    }
                ],
                "link": [],
            }
            payload = b"latin1-response"
            details = {
                "content_type": "application/fhir+json; charset=ISO-8859-1",
                "content_encoding": "",
                "declared_charset": "iso-8859-1",
                "used_encoding": "iso-8859-1",
                "decompressed": False,
                "payload_bytes": len(payload),
                "payload_sha256": __import__("hashlib").sha256(payload).hexdigest(),
            }
            with mock.patch.object(
                module,
                "_fetch_json",
                return_value=(bundle, "https://example.test/page", payload, details),
            ):
                frame, pages, payloads = module._fhir_export("https://example.test/page")
            self.assertEqual(len(frame), 1)
            self.assertEqual(frame.loc[0, "name"], "Müller Study")
            self.assertEqual(pages[0]["used_encoding"], "iso-8859-1")
            self.assertEqual(payloads, [payload])


class PublisherPartialRefreshTests(unittest.TestCase):
    @staticmethod
    def _source_rows() -> list[dict[str, str]]:
        return [
            {
                "publisher_family": "Publisher A",
                "policy_title": "Policy A",
                "policy_url": "https://publisher-a.example/policy",
                "expected_terms": "repository",
                "date_checked": "2026-09-01",
                "content_sha256": "a" * 64,
                "snapshot_path": "reference/old-a.html.gz",
                "http_status": "200",
                "final_url": "https://publisher-a.example/policy",
                "refresh_status": "current",
            },
            {
                "publisher_family": "Publisher B",
                "policy_title": "Policy B",
                "policy_url": "https://publisher-b.example/policy",
                "expected_terms": "repository",
                "date_checked": "2026-08-01",
                "content_sha256": "b" * 64,
                "snapshot_path": "reference/old-b.html.gz",
                "http_status": "200",
                "final_url": "https://publisher-b.example/policy",
                "refresh_status": "current",
            },
        ]

    def test_continue_on_error_applies_successes_and_preserves_failed_row(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            common = configure(root)
            reference = Path(common.PROJECT) / "reference"
            reference.mkdir(parents=True, exist_ok=True)
            sources = reference / "publisher_policy_sources.csv"
            original = pd.DataFrame(self._source_rows())
            original.to_csv(sources, index=False)
            module = importlib.import_module("step_update_publisher_info")

            def fetch(url: str):
                if "publisher-b" in url:
                    raise TimeoutError("simulated transient timeout")
                return b"<html>repository</html>", 200, url, "text/html; charset=utf-8"

            with mock.patch.object(module, "_fetch", side_effect=fetch):
                ok, coverage, run_dir = module.run(
                    sources_path=sources,
                    apply=True,
                    continue_on_error=True,
                    max_fetch_errors=2,
                )
            self.assertTrue(ok)
            self.assertTrue(coverage["partial_success"])
            self.assertEqual(coverage["fetch_errors"], 1)
            self.assertEqual(coverage["successful_fetches"], 1)
            self.assertEqual(coverage["applied"], 1)
            updated = pd.read_csv(sources, dtype=str, keep_default_na=False)
            self.assertNotEqual(updated.loc[0, "content_sha256"], "a" * 64)
            for column in module.REQUIRED_COLUMNS:
                self.assertEqual(updated.loc[1, column], original.loc[1, column])
            report = pd.read_csv(Path(run_dir) / "publisher_source_drift_report.csv", dtype=str, keep_default_na=False)
            failed = report.loc[report["policy_url"].str.contains("publisher-b")].iloc[0]
            self.assertEqual(failed["row_action"], "retain_prior_row_unchanged")
            self.assertEqual(failed["refresh_status"], "fetch_error_retained")

    def test_strict_fetch_failure_does_not_install_staged_snapshots(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            common = configure(root)
            reference = Path(common.PROJECT) / "reference"
            reference.mkdir(parents=True, exist_ok=True)
            sources = reference / "publisher_policy_sources.csv"
            original_bytes = pd.DataFrame(self._source_rows()).to_csv(index=False).encode("utf-8")
            sources.write_bytes(original_bytes)
            module = importlib.import_module("step_update_publisher_info")
            calls = 0

            def fetch(url: str):
                nonlocal calls
                calls += 1
                if calls == 1:
                    return b"<html>repository</html>", 200, url, "text/html"
                raise TimeoutError("simulated timeout")

            with mock.patch.object(module, "_fetch", side_effect=fetch):
                ok, coverage, _ = module.run(
                    sources_path=sources,
                    apply=True,
                    continue_on_error=False,
                    max_fetch_errors=2,
                )
            self.assertFalse(ok)
            self.assertEqual(coverage["applied"], 0)
            self.assertEqual(sources.read_bytes(), original_bytes)
            self.assertFalse((reference / "publisher_source_snapshots").exists())


class BmicDiscoveryTests(unittest.TestCase):
    def test_absent_conventional_upload_falls_back_to_existing_snapshot_as_noop(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            common = configure(root)
            destination = Path(common.INP) / "BMIC_download.csv"
            frame = pd.DataFrame(
                [
                    {"Repository Name": f"Repository {index}", "Open Data Submission": "Yes"}
                    for index in range(3)
                ]
            )
            frame.to_csv(destination, index=False)
            module = importlib.import_module("step_update_bmic_info")
            with mock.patch.object(module, "_uploaded_candidates", return_value=[destination.resolve()]):
                ok, report, _ = module.run(
                    source_file=module.DEFAULT_LOCAL_SOURCE,
                    source_url="",
                    destination=destination,
                    minimum_rows=1,
                    maximum_drop_fraction=0.5,
                    apply=True,
                )
            self.assertTrue(ok)
            self.assertEqual(report["source_selection"], "existing_project_snapshot")
            self.assertTrue(report["no_op"])
            self.assertFalse(report["applied"])

    def test_cp1252_upload_is_normalized_to_utf8(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            common = configure(root)
            source = root / "BMIC_upload.csv"
            source.write_bytes(
                "Repository Name,Open Data Submission\nMüller Repository,Yes\n".encode("cp1252")
            )
            destination = Path(common.INP) / "BMIC_download.csv"
            module = importlib.import_module("step_update_bmic_info")
            ok, report, _ = module.run(
                source_file=source,
                source_url="",
                destination=destination,
                minimum_rows=1,
                maximum_drop_fraction=0.5,
                apply=True,
                require_source_file=True,
            )
            self.assertTrue(ok)
            self.assertTrue(report["applied"])
            self.assertEqual(report["source_encoding"], "cp1252")
            decoded = destination.read_text(encoding="utf-8")
            self.assertIn("Müller Repository", decoded)


if __name__ == "__main__":
    unittest.main()
