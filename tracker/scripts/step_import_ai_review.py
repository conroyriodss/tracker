"""Validate and import ChatGPT/Perplexity review recommendations.

Imported rows are written only to inputs/ai_review_recommendations.csv. They do
not enter operational adjudication files and cannot mutate cards or crosswalks.
The default is strict all-or-nothing validation for source identity, hashes,
and actionable targets. Harmless target/alias echoes on REJECT or DEFER are
cleared, preserved in the audit record, and reported as safe normalizations.
"""
from __future__ import annotations

import argparse

from automated_review import import_review_response
from common import ledger_append


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--response", required=True, help="JSON, JSONL, or ZIP returned by a provider.")
    parser.add_argument(
        "--require-complete-batches",
        action="store_true",
        help="Reject a response that omits any item from its exported batch.",
    )
    parser.add_argument(
        "--no-safe-json-repair",
        action="store_true",
        help=(
            "Disable narrowly scoped repair of common chat-export JSON syntax damage. "
            "Repairs never alter decisions, targets, identifiers, hashes, or review keys."
        ),
    )
    parser.add_argument("--output-dir", default="")
    args = parser.parse_args()

    try:
        report = import_review_response(
            args.response,
            strict_complete_batches=args.require_complete_batches,
            allow_safe_json_repair=not args.no_safe_json_repair,
            output_dir=args.output_dir or None,
        )
    except Exception as exc:
        print(f"IMPORT FAILED: {exc}")
        return 2
    print("Automated-review recommendations imported")
    print(f"  valid_recommendations={report['valid_recommendations']}")
    print(f"  normalized_recommendations={report.get('normalized_recommendations', 0)}")
    print(f"  normalization_events={report.get('normalization_events', 0)}")
    print(f"  syntax_repaired_files={report.get('syntax_repaired_files', 0)}")
    print(f"  syntax_repair_events={report.get('syntax_repair_events', 0)}")
    for repaired in report.get("repaired_response_files", []):
        if repaired:
            print(f"  repaired_response={repaired}")
    print(f"  inserted={report['recommendations_inserted']}")
    print(f"  updated={report['recommendations_updated']}")
    print(f"  destination={report['destination']}")
    print("  operational_effect=recommendations only; human approval still required")
    ledger_append(
        "import_ai_review",
        params={
            "require_complete_batches": args.require_complete_batches,
            "safe_json_repair": not args.no_safe_json_repair,
        },
        inputs={"response": args.response},
        outputs={
            "valid_recommendations": report["valid_recommendations"],
            "normalized_recommendations": report.get("normalized_recommendations", 0),
            "normalization_events": report.get("normalization_events", 0),
            "syntax_repaired_files": report.get("syntax_repaired_files", 0),
            "syntax_repair_events": report.get("syntax_repair_events", 0),
            "inserted": report["recommendations_inserted"],
            "updated": report["recommendations_updated"],
            "destination": report["destination"],
        },
        gate_passed=True,
        run_dir=report["run_dir"],
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
