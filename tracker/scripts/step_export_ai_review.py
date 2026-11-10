"""Create a sealed, provider-ready automated-review evidence bundle.

The export contains public repository metadata only, is split into bounded
batches, and includes provider-specific prompts plus a strict response schema.
No card, crosswalk, remap, or adjudication file is changed.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
from pathlib import Path

from automated_review import EXPORT_RUN_ROOT, NoEligibleReviewRowsError, export_review_bundle
from common import ENV, atomic_write_json, ledger_append


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, help="Card/name, identifier or crosswalk review CSV.")
    parser.add_argument(
        "--provider",
        choices=["chatgpt", "perplexity", "both", "generic", "gemini", "claude", "gcp_dual"],
        default="chatgpt",
        help="Provider-specific prompt hint; output remains provider-neutral JSON.",
    )
    parser.add_argument("--batch-size", type=int, default=20)
    parser.add_argument("--include-decided", action="store_true")
    parser.add_argument(
        "--bundle-only",
        action="store_true",
        help="Instruct the model not to use external web research.",
    )
    parser.add_argument("--max-items", type=int, default=0)
    parser.add_argument("--output-dir", default="")
    args = parser.parse_args()

    try:
        report = export_review_bundle(
            args.source,
            provider=args.provider,
            batch_size=args.batch_size,
            include_decided=args.include_decided,
            web_research=not args.bundle_only,
            max_items=args.max_items,
            output_dir=Path(args.output_dir).expanduser() if args.output_dir else None,
        )
    except NoEligibleReviewRowsError as exc:
        stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        run_dir = EXPORT_RUN_ROOT / f"{stamp}-no-eligible-rows"
        run_dir.mkdir(parents=True, exist_ok=False)
        no_op_report = {
            "schema": "nih-tracker-automated-review-export-report/v1",
            "status": "no_eligible_rows",
            "bundle_id": run_dir.name,
            "queue_kind": exc.queue_kind,
            "source_file": str(exc.source_file),
            "reviewable_rows": exc.reviewable_rows,
            "item_count": 0,
            "batch_count": 0,
            "include_decided": exc.include_decided,
            "download_files": [],
            "run_dir": str(run_dir),
            "message": str(exc),
        }
        atomic_write_json(run_dir / "export_report.json", no_op_report)
        print("NO ELIGIBLE REVIEW ROWS")
        print(f"  queue_kind={exc.queue_kind}")
        print(f"  reviewable_rows={exc.reviewable_rows}")
        print(f"  include_decided={exc.include_decided}")
        print(f"  reason={exc}")
        print("  No review package was created.")
        ledger_append(
            "export_ai_review",
            params={
                "provider": args.provider,
                "batch_size": args.batch_size,
                "include_decided": args.include_decided,
                "web_research": not args.bundle_only,
            },
            inputs={"source": args.source},
            outputs={"items": 0, "batches": 0, "no_op": 1},
            gate_passed=True,
            run_dir=str(run_dir),
        )
        return 0
    print("Automated-review bundle created")
    print(f"  environment={ENV}")
    print(f"  queue_kind={report['queue_kind']}")
    print(f"  items={report['item_count']}")
    print(f"  batches={report['batch_count']}")
    print(f"  master_zip={report['master_zip']}")
    print(f"  master_zip_sha256={report['master_zip_sha256']}")
    for entry in report.get("download_files", []):
        print(
            f"  download={entry['kind']} | {entry['label']} | "
            f"{entry['path']} | sha256={entry['sha256']}"
        )
    print("  operational_effect=recommendations only; no adjudication or card changes")
    ledger_append(
        "export_ai_review",
        params={
            "provider": args.provider,
            "batch_size": args.batch_size,
            "include_decided": args.include_decided,
            "web_research": not args.bundle_only,
        },
        inputs={"source": args.source},
        outputs={
            "bundle_id": report["bundle_id"],
            "items": report["item_count"],
            "batches": report["batch_count"],
            "zip": report["master_zip"],
            "zip_sha256": report["master_zip_sha256"],
        },
        gate_passed=True,
        run_dir=report["run_dir"],
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
