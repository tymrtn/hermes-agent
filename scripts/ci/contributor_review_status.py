#!/usr/bin/env python3
"""Emit actionable review status for missing contributor mappings."""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def build_status(detail: str) -> list[dict]:
    """Build guidance that uses the conflict-free contributor mapping flow."""
    return [
        {
            "source": "contributor attribution",
            "results": [
                {
                    "kind": "action_required",
                    "title": "Unmapped contributor email(s)",
                    "summary": "New contributor email(s) lack a contributor mapping.",
                    "detail": detail,
                    "how_to_fix": (
                        "Add one conflict-free mapping file per email:\n"
                        "```\n"
                        'python3 scripts/add_contributor.py "<email>" "<github-username>"\n'
                        "```\n"
                        "Do not edit the frozen legacy map in `scripts/release.py`.\n\n"
                        "To find the GitHub username for an email:\n"
                        "```\n"
                        "gh api 'search/users?q=EMAIL+in:email' --jq '.items[0].login'\n"
                        "```\n"
                    ),
                }
            ],
        }
    ]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--detail", required=True)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()

    with args.output.open("a", encoding="utf-8") as output:
        output.write(f"review_status={json.dumps(build_status(args.detail))}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
