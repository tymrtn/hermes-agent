"""Secret and sensitive-content exclusion.

Two independent layers:

1. Path layer — files/directories whose names look credential-bearing are
   never opened, never fingerprinted, never excerpted. They appear only as
   excluded entries (path + reason).
2. Content layer — files that pass the path layer but whose bytes match a
   secret pattern keep their fingerprint (a hash reveals nothing) but have
   their excerpt suppressed entirely. Pattern names are recorded, matched
   text never is.

Over-exclusion is the intended failure direction.
"""
from __future__ import annotations

import fnmatch
import re
from pathlib import Path

# Directory names whose entire subtree is skipped without reading.
SECRET_DIR_NAMES = frozenset({
    ".ssh", ".aws", ".gnupg", ".gcloud", ".azure", ".kube",
    ".password-store", "secrets", "credentials", "auth-backups",
})

# Case-insensitive filename patterns that are never opened.
SECRET_PATH_PATTERNS: tuple[str, ...] = (
    ".env", ".env.*", "*.env",
    "*.pem", "*.key", "*.p12", "*.pfx", "*.keystore", "*.jks", "*.kdbx",
    "id_rsa*", "id_ed25519*", "id_ecdsa*", "*.ppk",
    ".netrc", ".npmrc", ".pypirc", ".htpasswd",
    "auth.json", "auth.json.*", "auth.json.bak*",
    "*credential*", "*secret*", "*apikey*", "*api-key*", "*api_key*",
    "*token*", "*passwd*", "*password*",
    "*.sqlite-shm", "*.sqlite-wal", "*.db-shm", "*.db-wal",
)

# High-signal secret content patterns. Names are reported; matches are not.
SECRET_CONTENT_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("private_key_block", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("sk_token", re.compile(r"\bsk-[A-Za-z0-9_-]{20,}")),
    ("github_token", re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{36,}\b")),
    ("github_pat", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{22,}\b")),
    ("slack_token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b")),
    ("aws_access_key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("google_api_key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{16,}\.eyJ[A-Za-z0-9_-]{16,}")),
    ("bearer_header", re.compile(r"(?i)\bauthorization\s*:\s*bearer\s+\S{16,}")),
    ("assignment", re.compile(
        r"(?i)\b[\w.-]*(?:api[_-]?key|secret|token|passwd|password)\b\s*[:=]\s*['\"]?[^\s'\"]{8,}")),
)


def classify_path(path: Path) -> str | None:
    """Return an exclusion reason when any path component looks secret-bearing."""
    for part in path.parts[:-1]:
        if part.lower() in SECRET_DIR_NAMES:
            return f"secret_dir:{part.lower()}"
    name = path.name.lower()
    for pattern in SECRET_PATH_PATTERNS:
        if fnmatch.fnmatchcase(name, pattern):
            return f"secret_path:{pattern}"
    return None


def scan_content(text: str) -> list[str]:
    """Return the names of all secret patterns present in `text` (never the matches)."""
    return [name for name, rx in SECRET_CONTENT_PATTERNS if rx.search(text)]
