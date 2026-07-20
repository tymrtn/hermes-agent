#!/usr/bin/env python3
"""Build the OSV workflow's review_status from a SARIF result file."""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def _result_location(result: dict) -> str:
    locations = result.get("locations") or []
    if not locations:
        return "unknown location"
    return (
        locations[0]
        .get("physicalLocation", {})
        .get("artifactLocation", {})
        .get("uri", "unknown location")
    )


def load_results(path: Path) -> list[dict]:
    """Load SARIF findings, rejecting malformed scanner output."""
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("runs"), list):
        raise ValueError("OSV SARIF must be an object containing a runs array")

    results: list[dict] = []
    for run in data.get("runs", []):
        if not isinstance(run, dict) or not isinstance(run.get("results", []), list):
            raise ValueError("OSV SARIF runs must contain results arrays")
        for result in run.get("results", []):
            if not isinstance(result, dict):
                raise ValueError("OSV SARIF results must be objects")
            results.append(result)
    return results


def build_status(results: list[dict]) -> list[dict]:
    """Translate OSV findings to the unified CI review contract."""
    if not results:
        return []

    count = len(results)
    noun = "vulnerability" if count == 1 else "vulnerabilities"
    detail = "\n".join(
        f"- {result.get('ruleId', 'unknown')} in {_result_location(result)}"
        for result in results[:20]
    )
    return [
        {
            "source": "osv scan",
            "results": [
                {
                    "kind": "warning",
                    "title": "OSV vulnerability scan",
                    "summary": f"{count} known {noun} found in pinned dependencies.",
                    "detail": detail,
                    "how_to_fix": (
                        "Review the findings in the [Security tab](../../security/code-scanning). "
                        "Update the affected dependencies if a patched version is available."
                    ),
                }
            ],
        }
    ]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("sarif", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()

    status = build_status(load_results(args.sarif))
    with args.output.open("a", encoding="utf-8") as output:
        output.write(f"review_status={json.dumps(status)}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
