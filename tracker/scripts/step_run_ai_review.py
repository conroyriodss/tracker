"""Compatibility stub: direct external-provider API review was removed in v18.1.

Use step_export_ai_review.py, review a downloaded batch in the provider web
interface, then import the returned JSON with step_import_ai_review.py. This
command performs no network request and loads no provider credentials.
"""
from __future__ import annotations

import argparse


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", default="", help=argparse.SUPPRESS)
    parser.add_argument("--provider", default="", help=argparse.SUPPRESS)
    parser.add_argument("--model", default="", help=argparse.SUPPRESS)
    parser.add_argument("--batch", default="", help=argparse.SUPPRESS)
    parser.add_argument("--web-research", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--import-results", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--confirm", default="", help=argparse.SUPPRESS)
    parser.parse_args()
    print("Direct API review is not included in NIH Tracker v18.1.")
    print("Use the sealed offline export, provider web interface, and upload/import workflow.")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
