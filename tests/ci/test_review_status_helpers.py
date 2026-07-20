"""Behavior tests for focused CI review-status helpers."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]


def _load(name: str):
    path = ROOT / "scripts" / "ci" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Could not load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


osv = _load("osv_review_status")
contributor = _load("contributor_review_status")
supply_chain = _load("supply_chain_status")


def test_osv_sarif_findings_become_a_warning(tmp_path: Path):
    sarif = tmp_path / "results.sarif"
    sarif.write_text(
        json.dumps({
            "runs": [
                {
                    "results": [
                        {
                            "ruleId": "GHSA-test",
                            "locations": [
                                {
                                    "physicalLocation": {
                                        "artifactLocation": {"uri": "uv.lock"},
                                    },
                                }
                            ],
                        }
                    ],
                }
            ],
        }),
        encoding="utf-8",
    )

    status = osv.build_status(osv.load_results(sarif))

    result = status[0]["results"][0]
    assert result["kind"] == "warning"
    assert result["summary"] == "1 known vulnerability found in pinned dependencies."
    assert result["detail"] == "- GHSA-test in uv.lock"


def test_osv_sarif_without_findings_emits_no_status(tmp_path: Path):
    sarif = tmp_path / "results.sarif"
    sarif.write_text('{"runs": [{"results": []}]}', encoding="utf-8")

    assert osv.build_status(osv.load_results(sarif)) == []


@pytest.mark.parametrize("content", ["not json", "{}", '{"runs": {}}'])
def test_osv_invalid_scanner_output_fails_observably(tmp_path: Path, content: str):
    sarif = tmp_path / "results.sarif"
    sarif.write_text(content, encoding="utf-8")

    with pytest.raises((json.JSONDecodeError, ValueError)):
        osv.load_results(sarif)


def test_osv_missing_scanner_output_fails_observably(tmp_path: Path):
    with pytest.raises(FileNotFoundError):
        osv.load_results(tmp_path / "results.sarif")


def test_contributor_guidance_uses_mapping_files_not_frozen_history():
    result = contributor.build_status("dev@example.com (Developer)")[0]["results"][0]

    assert result["summary"] == "New contributor email(s) lack a contributor mapping."
    assert (
        'scripts/add_contributor.py "<email>" "<github-username>"'
        in result["how_to_fix"]
    )
    assert "Do not edit the frozen legacy map" in result["how_to_fix"]
    assert "Add mappings to scripts/release.py" not in result["how_to_fix"]


@pytest.mark.parametrize(
    ("found", "reviewed", "expected_kind", "blocking"),
    [
        (False, False, None, False),
        (False, True, None, False),
        (True, False, "error", True),
        (True, True, "info", False),
    ],
)
def test_supply_chain_label_override_contract(
    found: bool,
    reviewed: bool,
    expected_kind: str | None,
    blocking: bool,
):
    statuses, should_block = supply_chain.build_decision(found, reviewed, "finding")

    assert should_block is blocking
    if expected_kind is None:
        assert statuses == []
    else:
        assert statuses[0]["results"][0]["kind"] == expected_kind
