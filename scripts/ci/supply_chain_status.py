#!/usr/bin/env python3
"""Build supply-chain review status and the label-gated blocking decision."""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def build_decision(
    found: bool, reviewed: bool, detail: str = ""
) -> tuple[list[dict], bool]:
    """Return the review status and whether an unreviewed finding must block."""
    if not found:
        return [], False

    if reviewed:
        result = {
            "kind": "info",
            "title": "Critical supply chain risk reviewed",
            "summary": "Flagged patterns were acknowledged with the `ci-reviewed` label.",
            "detail": detail,
        }
    else:
        result = {
            "kind": "error",
            "title": "Critical supply chain risk",
            "summary": "Critical supply chain risk patterns detected in this PR.",
            "detail": detail,
            "how_to_fix": (
                "Review the flagged code carefully. If it is intentional, a maintainer "
                "can add the `ci-reviewed` label and rerun the check."
            ),
        }
    return [{"source": "supply chain", "results": [result]}], not reviewed


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--found", action="store_true")
    parser.add_argument("--reviewed", action="store_true")
    parser.add_argument("--detail-file", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()

    detail = ""
    if args.found:
        if args.detail_file is None:
            parser.error("--detail-file is required with --found")
        detail = args.detail_file.read_text(encoding="utf-8")

    status, blocking = build_decision(args.found, args.reviewed, detail)
    with args.output.open("a", encoding="utf-8") as output:
        output.write(f"review_status={json.dumps(status)}\n")
        output.write(f"blocking={str(blocking).lower()}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
