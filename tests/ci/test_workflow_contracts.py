"""Semantic contracts for security-sensitive GitHub workflow wiring."""

from __future__ import annotations

from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[2]


def _workflow(name: str) -> dict:
    with (ROOT / ".github" / "workflows" / name).open(encoding="utf-8") as stream:
        return yaml.safe_load(stream)


def _on(workflow: dict) -> dict:
    # PyYAML implements YAML 1.1 and therefore parses the key ``on`` as True.
    return workflow.get("on", workflow.get(True, {}))


def _step(job: dict, name: str) -> tuple[int, dict]:
    for index, step in enumerate(job["steps"]):
        if step.get("name") == name:
            return index, step
    raise AssertionError(f"Missing workflow step {name!r}")


def test_pr_jobs_use_only_read_scoped_builtin_tokens_after_checkout():
    ci = _workflow("ci.yml")

    detect = ci["jobs"]["detect"]
    timings = ci["jobs"]["ci-timings"]

    for job in (detect, timings):
        assert all(step.get("name") != "Get GitHub App token" for step in job["steps"])
        assert "APP_CLIENT_ID" not in job.get("env", {})
        assert all("private-key" not in step.get("with", {}) for step in job["steps"])

    assert detect["permissions"] == {"contents": "read", "pull-requests": "read"}
    assert timings["permissions"] == {"contents": "read", "actions": "read"}

    _, classify = _step(detect, "Detect affected areas")
    _, collect = _step(timings, "Collect timings and generate report")
    assert classify["with"]["github-token"] == "${{ github.token }}"
    assert collect["env"]["GITHUB_TOKEN"] == "${{ github.token }}"


def test_supply_chain_pr_scan_receives_no_app_private_key():
    scan = _workflow("supply-chain-audit.yml")["jobs"]["scan"]

    assert all("private-key" not in step.get("with", {}) for step in scan["steps"])


def test_osv_artifact_and_reusable_output_contracts_are_wired():
    workflow = _workflow("osv-scanner.yml")
    emit = workflow["jobs"]["emit-status"]
    _, download = _step(emit, "Download SARIF result")

    assert download["with"] == {
        "name": "OSV Scanner SARIF file",
        "path": "/tmp/osv-results",
    }
    assert download.get("continue-on-error") is not True
    assert _on(workflow)["workflow_call"]["outputs"]["review_status"]["value"] == (
        "${{ jobs.emit-status.outputs.review_status }}"
    )


def test_supply_chain_failure_consumes_the_review_decision():
    scan = _workflow("supply-chain-audit.yml")["jobs"]["scan"]
    _, label_check = _step(scan, "Check ci-reviewed label")
    _, emit = _step(scan, "Emit review_status")
    _, failure = _step(scan, "Fail on critical findings")

    assert label_check["env"]["GH_TOKEN"] == "${{ github.token }}"
    assert emit["env"]["REVIEWED"] == "${{ steps.label-check.outputs.ci_reviewed }}"
    assert failure["if"] == "steps.emit-status.outputs.blocking == 'true'"
