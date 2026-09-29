from pathlib import Path

import pytest

from dream_cycle_v3.secretguard import classify_path, scan_content


@pytest.mark.parametrize("rel", [
    ".env", ".env.production", "prod.env", "server.pem", "id_rsa",
    "id_ed25519.pub", "auth.json", "auth.json.bak.123", "my-credentials.yaml",
    "client_secret.json", "apikey.txt", "api_key.md", "github-token.txt",
    "passwords.txt", ".netrc", ".npmrc", "store.db-wal",
])
def test_secret_filenames_are_flagged(rel):
    assert classify_path(Path(rel)) is not None


@pytest.mark.parametrize("rel", [
    ".ssh/config", ".aws/config", "secrets/notes.md", "credentials/x.json",
    "nested/.gnupg/trustdb.gpg",
])
def test_secret_directories_are_flagged(rel):
    reason = classify_path(Path(rel))
    assert reason is not None and reason.startswith("secret_dir:")


@pytest.mark.parametrize("rel", [
    "notes.md", "sessions/20260710.jsonl", "state/wake-up.md", "run.log",
])
def test_ordinary_paths_pass(rel):
    assert classify_path(Path(rel)) is None


@pytest.mark.parametrize("text,expected", [
    ("key: sk-FAKEFAKEFAKEFAKEFAKEFAKE", "sk_token"),
    ("ghp_SAMPLEFAKESAMPLEFAKESAMPLEFAKE000000", "github_token"),
    ("-----BEGIN RSA PRIVATE KEY-----", "private_key_block"),
    ("aws AKIAIOSFODNN7EXAMPLE here", "aws_access_key"),
    ("xoxb-123456789012-abcdefghij", "slack_token"),
    ("Authorization: Bearer abcdefghijklmnopqrstuv", "bearer_header"),
    ("api_key = supersecretvalue123", "assignment"),
    ("password: hunter2hunter2", "assignment"),
])
def test_secret_content_is_detected(text, expected):
    assert expected in scan_content(text)


def test_scan_reports_pattern_names_never_matches():
    text = "token = extremely-private-value-42"
    hits = scan_content(text)
    assert hits
    assert all("extremely-private" not in h for h in hits)


def test_clean_content_passes():
    assert scan_content("Decision: adopt manifest fingerprints for identity") == []
