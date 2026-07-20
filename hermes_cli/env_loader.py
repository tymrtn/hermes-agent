"""Helpers for loading Hermes .env files consistently across entrypoints."""

from __future__ import annotations

import codecs
import io
import os
import sys
from pathlib import Path

from dotenv import dotenv_values, load_dotenv
from utils import atomic_replace


# Env var name suffixes that indicate credential values.  These are the
# only env vars whose values we sanitize on load — we must not silently
# alter arbitrary user env vars, but credentials are known to require
# pure ASCII (they become HTTP header values).
_CREDENTIAL_SUFFIXES = ("_API_KEY", "_TOKEN", "_SECRET", "_KEY")

# Names we've already warned about during this process, so repeated
# load_hermes_dotenv() calls (user env + project env, gateway hot-reload,
# tests) don't spam the same warning multiple times.
_WARNED_KEYS: set[str] = set()

# Paths we've already emitted a UTF-32 refuse-to-mangle warning for.
# load_hermes_dotenv can call _sanitize_env_file_if_needed multiple times
# for the same file (user env + project env + hot-reload); once per path
# is enough.
_WARNED_UTF32_PATHS: set[str] = set()

# Map of env-var name → source label ("bitwarden", etc.) for credentials
# that were injected by an external secret source during load_hermes_dotenv().
# Used by setup / `hermes model` flows to label detected credentials so
# users understand WHERE a key came from when their .env doesn't contain it
# directly (otherwise the "credentials detected ✓" line looks identical to
# the .env case and they don't know Bitwarden is wired up).
_SECRET_SOURCES: dict[str, str] = {}

# HERMES_HOME paths we've already pulled external secrets for during this
# process.  ``load_hermes_dotenv()`` is called at module-import time from
# several hot modules (cli.py, hermes_cli/main.py, run_agent.py,
# trajectory_compressor.py, gateway/run.py, ...), so without this guard the
# Bitwarden status line gets printed 3-5x per startup.  Bitwarden's own
# in-process cache prevents redundant network calls, but the print, the
# config re-parse, and the ASCII sanitization sweep still ran every time.
_APPLIED_HOMES: set[str] = set()


def get_secret_source(env_var: str) -> str | None:
    """Return the label of the secret source that supplied ``env_var``, if any.

    Returns ``"bitwarden"`` for keys pulled from Bitwarden Secrets Manager
    during the current process's ``load_hermes_dotenv()`` call.  Returns
    ``None`` for keys that came from ``.env``, the shell environment, or
    aren't tracked.  The returned label is metadata only: credential-pool
    persistence may store it to explain the origin of a borrowed secret, but
    must never treat it as authorization to persist the raw value.
    """
    return _SECRET_SOURCES.get(env_var)


def reset_secret_source_cache() -> None:
    """Forget which HERMES_HOME paths have already had external secrets applied.

    The first call to ``_apply_external_secret_sources(home_path)`` in a
    process pulls from Bitwarden (or other configured backend), records the
    applied keys in ``_SECRET_SOURCES``, and remembers ``home_path`` so
    subsequent calls in the same process are no-ops.  Call this to force the
    next call to re-pull — useful for tests, and for long-running processes
    that want to refresh after a config change.
    """
    _APPLIED_HOMES.clear()


def format_secret_source_suffix(env_var: str) -> str:
    """Return a human-readable suffix like ``" (from Bitwarden)"`` or ``""``.

    Use this when printing a detected credential so the user can see where
    it came from.  Empty string when the credential came from ``.env`` or
    the shell — those are the implicit / "default" cases users already
    understand.
    """
    source = get_secret_source(env_var)
    if not source:
        return ""
    if source == "bitwarden":
        return " (from Bitwarden)"
    # Ask the registry for the source's human label (e.g. "1Password").
    # Fall back to the raw source name for labels the registry doesn't
    # know (stale provenance from an uninstalled plugin, tests).
    try:
        from agent.secret_sources.registry import get_source

        registered = get_source(source)
        if registered is not None and registered.label:
            return f" (from {registered.label})"
    except Exception:  # noqa: BLE001 — label lookup must never raise
        pass
    return f" (from {source})"


def _format_offending_chars(value: str, limit: int = 3) -> str:
    """Return a compact 'U+XXXX ('c'), ...' summary of non-ASCII codepoints."""
    seen: list[str] = []
    for ch in value:
        if ord(ch) > 127:
            label = f"U+{ord(ch):04X}"
            if ch.isprintable():
                label += f" ({ch!r})"
            if label not in seen:
                seen.append(label)
            if len(seen) >= limit:
                break
    return ", ".join(seen)


def _shared_profile_env_key(key: str) -> bool:
    """Return True for root .env keys safe to inherit into profile homes."""
    # Provider/API credentials are shared. Platform runtime identity (bot
    # tokens, home channels, allowlists) must stay profile-local; inheriting a
    # root Telegram token makes planning/worker profiles collide with Nagatha.
    return key.endswith("_API_KEY") or key in {
        "OPENAI_API_KEY",
        "OPENROUTER_API_KEY",
        "ANTHROPIC_API_KEY",
        "GEMINI_API_KEY",
        "GOOGLE_API_KEY",
        "XAI_API_KEY",
        "DEEPSEEK_API_KEY",
        "MISTRAL_API_KEY",
        "GROQ_API_KEY",
        "TOGETHER_API_KEY",
        "FIRECRAWL_API_KEY",
        "PARALLEL_API_KEY",
    }


def _dotenv_keys(path: Path) -> set[str]:
    if not path.exists():
        return set()
    try:
        return {str(k) for k, v in dotenv_values(path).items() if k and v is not None}
    except Exception:
        return set()


def _discard_unshared_root_env(
    *,
    before: dict[str, str],
    root_keys: set[str],
    profile_keys: set[str],
) -> None:
    """Remove root .env values that profiles must not inherit."""
    for key in root_keys - profile_keys:
        if _shared_profile_env_key(key):
            continue
        if key in before:
            os.environ[key] = before[key]
        else:
            os.environ.pop(key, None)


def _is_placeholder_credential(value: str | None) -> bool:
    """Return True for dummy credential values that should not override real keys."""
    if value is None:
        return False
    stripped = value.strip().strip('"').strip("'")
    if not stripped:
        return True
    return stripped.lower() in {"xxx", "...", "todo", "changeme", "[redacted]"} or len(stripped) < 10


def _restore_credentials_overridden_by_placeholders(before: dict[str, str]) -> None:
    """Undo profile .env placeholder credential overrides.

    Some legacy profile .env files contain values like ``OPENROUTER_API_KEY=xxx``.
    When profile homes overlay the canonical user env, those dummy values should
    not erase a valid shared key already loaded from ~/.hermes/.env.
    """
    for key, previous in before.items():
        if not _shared_profile_env_key(key):
            continue
        current = os.environ.get(key)
        if previous and not _is_placeholder_credential(previous) and _is_placeholder_credential(current):
            os.environ[key] = previous


def _sanitize_loaded_credentials() -> None:
    """Strip non-ASCII characters from credential env vars in os.environ.

    Called after dotenv loads so the rest of the codebase never sees
    non-ASCII API keys.  Only touches env vars whose names end with
    known credential suffixes (``_API_KEY``, ``_TOKEN``, etc.).

    Emits a one-line warning to stderr when characters are stripped.
    Silent stripping would mask copy-paste corruption (Unicode lookalike
    glyphs from PDFs / rich-text editors, ZWSP from web pages) as opaque
    provider-side "invalid API key" errors (see #6843).
    """
    for key, value in list(os.environ.items()):
        if not any(key.endswith(suffix) for suffix in _CREDENTIAL_SUFFIXES):
            continue
        try:
            value.encode("ascii")
            continue
        except UnicodeEncodeError:
            pass
        cleaned = value.encode("ascii", errors="ignore").decode("ascii")
        os.environ[key] = cleaned
        if key in _WARNED_KEYS:
            continue
        _WARNED_KEYS.add(key)
        stripped = len(value) - len(cleaned)
        detail = _format_offending_chars(value) or "non-printable"
        print(
            f"  Warning: {key} contained {stripped} non-ASCII character"
            f"{'s' if stripped != 1 else ''} ({detail}) — stripped so the "
            f"key can be sent as an HTTP header.",
            file=sys.stderr,
        )
        print(
            "  This usually means the key was copy-pasted from a PDF, "
            "rich-text editor, or web page that substituted lookalike\n"
            "  Unicode glyphs for ASCII letters. If authentication fails "
            "(e.g. \"API key not valid\"), re-copy the key from the\n"
            "  provider's dashboard and run `hermes setup` (or edit the "
            ".env file in a plain-text editor).",
            file=sys.stderr,
        )


def _load_dotenv_with_fallback(path: Path, *, override: bool) -> None:
    try:
        load_dotenv(dotenv_path=path, override=override, encoding="utf-8")
    except UnicodeDecodeError:
        load_dotenv(dotenv_path=path, override=override, encoding="latin-1")
    # Strip non-ASCII characters from credential env vars that were just
    # loaded.  API keys must be pure ASCII since they're sent as HTTP
    # header values (httpx encodes headers as ASCII).  Non-ASCII chars
    # typically come from copy-pasting keys from PDFs or rich-text editors
    # that substitute Unicode lookalike glyphs (e.g. ʋ U+028B for v).
    _sanitize_loaded_credentials()


def _sanitize_env_file_if_needed(path: Path) -> None:
    """Pre-sanitize a .env file before python-dotenv reads it.

    python-dotenv does not handle corrupted lines where multiple
    KEY=VALUE pairs are concatenated on a single line (missing newline).
    This produces mangled values — e.g. a bot token duplicated 8×
    (see #8908).

    Also strips embedded null bytes which crash ``os.environ[k] = v``
    with ``ValueError: embedded null byte`` — typically introduced by
    copy-pasting API keys from terminals or rich-text editors.

    Encoding: sniffs a leading BOM *before* any text decode. UTF-16
    (Notepad "Unicode") is decoded correctly and rewritten as clean
    UTF-8. UTF-32 is refused (left untouched) so we never fall through
    to the errors=replace corruption path. Order of BOM checks matters:
    UTF-32-LE's BOM starts with UTF-16-LE's FF FE.

    We delegate to ``hermes_cli.config._sanitize_env_lines`` which
    already knows all valid Hermes env-var names and can split
    concatenated lines correctly.
    """
    if not path.exists():
        return
    try:
        from hermes_cli.config import _sanitize_env_lines
    except ImportError:
        return  # early bootstrap — config module not available yet

    try:
        raw = path.read_bytes()
    except Exception:
        return

    # Sniff leading BOM bytes BEFORE decoding. ORDER MATTERS:
    # codecs.BOM_UTF32_LE is FF FE 00 00, which startswith
    # codecs.BOM_UTF16_LE (FF FE). Checking UTF-16 first would
    # misdetect UTF-32-LE as UTF-16-LE and mangle the file.
    force_utf8_rewrite = False
    if raw.startswith(codecs.BOM_UTF32_LE) or raw.startswith(codecs.BOM_UTF32_BE):
        # Lazy import keeps the module import block identical to #65124's
        # codecs/io additions so the two PRs auto-merge either order.
        path_key = str(path.resolve())
        if path_key not in _WARNED_UTF32_PATHS:
            _WARNED_UTF32_PATHS.add(path_key)
            import logging

            logging.getLogger(__name__).warning(
                "Skipping .env sanitize for %s: UTF-32 BOM detected; "
                "leaving file untouched to avoid corruption",
                path,
            )
        return
    if raw.startswith(codecs.BOM_UTF16_LE) or raw.startswith(codecs.BOM_UTF16_BE):
        # "utf-16" uses the BOM to select endianness and strips it.
        # TextIOWrapper + newline=None matches open()'s universal-newlines
        # line splitting (\\n/\\r\\n/\\r only — not splitlines()'s extra
        # Unicode boundaries like U+2028), so sanitize sees the same lines
        # as the UTF-8 path.
        try:
            with io.TextIOWrapper(
                io.BytesIO(raw), encoding="utf-16", newline=None
            ) as f:
                original = f.readlines()
        except UnicodeDecodeError:
            return
        # Source is UTF-16 on disk; always rewrite as clean UTF-8 so
        # the subsequent utf-8 dotenv load sees a canonical file.
        force_utf8_rewrite = True
    else:
        # Default path: utf-8-sig (strips UTF-8 BOM if present) with
        # errors=replace so embedded NULs can be stripped below.
        try:
            with open(path, encoding="utf-8-sig", errors="replace") as f:
                original = f.readlines()
        except Exception:
            return
        # Defense-in-depth: errors=replace turns undecodable leading
        # bytes into U+FFFD. Persisting that glues replacement chars
        # onto the first key name and rewrites the file permanently
        # (the UTF-16-with-BOM corruption path before BOM sniffing).
        # Leave the file untouched rather than write the mangling.
        if original and original[0].startswith("\ufffd"):
            return

    try:
        # Strip null bytes before _sanitize_env_lines so they never
        # reach python-dotenv (which passes them to os.environ and
        # crashes with ValueError). Also intentionally repairs
        # BOM-less UTF-16 (NUL-padded ASCII) into clean UTF-8.
        stripped = [line.replace("\x00", "") for line in original]
        sanitized = _sanitize_env_lines(stripped)
        if sanitized != original or force_utf8_rewrite:
            import tempfile
            fd, tmp = tempfile.mkstemp(
                dir=str(path.parent), suffix=".tmp", prefix=".env_"
            )
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    f.writelines(sanitized)
                    f.flush()
                    os.fsync(f.fileno())
                atomic_replace(tmp, path)
            except BaseException:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise
    except Exception:
        pass  # best-effort — don't block gateway startup


def load_hermes_dotenv(
    *,
    hermes_home: str | os.PathLike | None = None,
    project_env: str | os.PathLike | None = None,
) -> list[Path]:
    """Load Hermes environment files with user config taking precedence.

    Behavior:
    - `~/.hermes/.env` overrides stale shell-exported values when present.
    - project `.env` acts as a dev fallback and only fills missing values when
      the user env exists.
    - if no user env exists, the project `.env` also overrides stale shell vars.
    """
    loaded: list[Path] = []

    home_path = Path(hermes_home or os.getenv("HERMES_HOME", Path.home() / ".hermes")).expanduser()
    user_env = home_path / ".env"
    project_env_path = Path(project_env) if project_env else None

    # Profile homes live under ~/.hermes/profiles/<name>.  Load the canonical
    # user env first so shared provider credentials (OPENROUTER_API_KEY, etc.)
    # are available without copying secrets into every profile.  Then overlay
    # the profile's own .env so bot tokens / profile-specific values still win.
    root_home = Path.home() / ".hermes"
    root_env = root_home / ".env"
    env_paths: list[Path] = []
    profile_home = False
    try:
        profile_home = home_path.resolve().is_relative_to((root_home / "profiles").resolve())
        if profile_home:
            env_paths.append(root_env)
    except Exception:
        pass
    if user_env not in env_paths:
        env_paths.append(user_env)

    # Fix corrupted .env files before python-dotenv parses them (#8908).
    for env_path in env_paths:
        if env_path.exists():
            _sanitize_env_file_if_needed(env_path)
    if project_env_path and project_env_path.exists():
        _sanitize_env_file_if_needed(project_env_path)

    root_keys = _dotenv_keys(root_env) if profile_home else set()
    profile_keys = _dotenv_keys(user_env) if profile_home else set()
    before_root_env = dict(os.environ) if profile_home else {}

    for i, env_path in enumerate(env_paths):
        if env_path.exists():
            previous_credentials = {
                key: value
                for key, value in os.environ.items()
                if any(key.endswith(suffix) for suffix in _CREDENTIAL_SUFFIXES)
            }
            _load_dotenv_with_fallback(env_path, override=True)
            if i > 0:
                _restore_credentials_overridden_by_placeholders(previous_credentials)
            loaded.append(env_path)

    if profile_home:
        _discard_unshared_root_env(
            before=before_root_env,
            root_keys=root_keys,
            profile_keys=profile_keys,
        )

    # Load .op.env AFTER .env so that .env values win, but the bootstrap
    # token (OP_SERVICE_ACCOUNT_TOKEN) becomes available for
    # apply_onepassword_secrets() even in cron / subprocess environments
    # that inherit no shell state (no systemd EnvironmentFile, no op run).
    # .op.env is gitignored — the service-account token never enters the
    # committed .env file.
    # Users on systemd can alternatively use:
    #   EnvironmentFile=-/path/to/.hermes/.op.env
    # in their gateway unit, which takes precedence (override=False below
    # ensures .op.env never clobbers a token already in the environment).
    op_env = home_path / ".op.env"
    if op_env.exists() and not os.environ.get("OP_SERVICE_ACCOUNT_TOKEN"):
        _load_dotenv_with_fallback(op_env, override=False)

    if project_env_path and project_env_path.exists():
        _load_dotenv_with_fallback(project_env_path, override=not loaded)
        loaded.append(project_env_path)

    _apply_external_secret_sources(home_path)
    _apply_managed_env()

    return loaded


def _apply_managed_env() -> None:
    """Apply the managed-scope .env last, with override, so it beats user/shell.

    Managed scope is machine-global (independent of HERMES_HOME / profile). v1
    enforcement is "applied last with override=True" — at the end of startup load
    ``os.environ`` holds the managed value for every managed key, beating both the
    user ``.env`` and any pre-existing shell export. This deliberately inverts the
    usual env-over-config precedence for the pinned keys (see
    ``docs/design/managed-scope.md`` §4.1).

    This does NOT prevent the agent from later mutating ``os.environ`` in-process
    or ``export``-ing in a subprocess shell; that hard boundary is a documented
    v2 item (design §8.1). v1 relies on filesystem permissions only.

    Fail-open: a missing managed dir or .env is the common case and a no-op; any
    error here is swallowed so managed scope can never block startup.
    """
    try:
        from hermes_cli import managed_scope

        managed_dir = managed_scope.get_managed_dir()
    except Exception:  # noqa: BLE001 — managed scope must never block startup
        return
    if managed_dir is None:
        return
    managed_env = managed_dir / ".env"
    if not managed_env.exists():
        return
    _sanitize_env_file_if_needed(managed_env)
    _load_dotenv_with_fallback(managed_env, override=True)


def _apply_external_secret_sources(home_path: Path) -> None:
    """Pull secrets from every enabled external source into env.

    Runs AFTER dotenv loads so .env values are visible (sources use them
    to locate bootstrap tokens) but BEFORE the rest of Hermes reads
    ``os.environ`` for credentials.  Any failure here is logged and
    swallowed — external secret sources must never block startup.

    The heavy lifting (source ordering, mapped-beats-bulk precedence,
    first-claim-wins conflict handling, override semantics, provenance)
    lives in ``agent.secret_sources.registry.apply_all``; this wrapper
    owns the once-per-HERMES_HOME guard, the post-apply ASCII
    sanitization sweep, the ``_SECRET_SOURCES`` provenance map that
    UI surfaces read, and the startup status lines.

    Idempotent within a process: subsequent calls for the same
    ``home_path`` are no-ops.  ``load_hermes_dotenv()`` runs at import
    time from several hot modules (cli.py, hermes_cli/main.py,
    run_agent.py, trajectory_compressor.py, ...), so without this guard
    the status lines would print 3-5x per CLI startup.  Use
    ``reset_secret_source_cache()`` if you need to force a re-pull
    (tests, long-running processes after a config change).
    """
    home_key = str(Path(home_path).resolve())
    if home_key in _APPLIED_HOMES:
        return
    _APPLIED_HOMES.add(home_key)

    try:
        cfg = _load_secrets_config(home_path)
    except Exception:  # noqa: BLE001 — config errors must not block startup
        return
    if not cfg:
        return

    try:
        from agent.secret_sources.registry import apply_all
    except ImportError:
        return

    try:
        report = apply_all(cfg, home_path)
    except Exception:  # noqa: BLE001 — belt-and-braces; apply_all shouldn't raise
        return

    if report.applied_any:
        # Re-run the ASCII sanitization pass: vault values are
        # user-supplied and might have the same copy-paste corruption as
        # a manually edited .env (see #6843).
        _sanitize_loaded_credentials()
        # Remember where each var came from so setup / `hermes model`
        # flows can label detected credentials with "(from Bitwarden)" /
        # "(from 1Password)" — otherwise users see "credentials ✓" with
        # no hint the value came from a vault rather than .env.
        for name, applied in report.provenance.items():
            _SECRET_SOURCES[name] = applied.source

    for src in report.sources:
        if src.applied:
            print(
                f"  {src.label}: applied {len(src.applied)} "
                f"secret{'s' if len(src.applied) != 1 else ''} "
                f"({', '.join(sorted(src.applied))})",
                file=sys.stderr,
            )
        if src.result.error:
            print(f"  {src.label}: {src.result.error}", file=sys.stderr)
        for warn in src.result.warnings:
            print(f"  {src.label}: {warn}", file=sys.stderr)
    for conflict in report.conflicts:
        print(f"  Secret sources: {conflict}", file=sys.stderr)


def _load_secrets_config(home_path: Path) -> dict:
    """Read just the ``secrets:`` section out of config.yaml.

    Imported lazily and isolated from the main config loader so a
    malformed config can't take down dotenv loading entirely.
    """
    config_path = home_path / "config.yaml"
    if not config_path.exists():
        return {}
    try:
        import yaml  # type: ignore
    except ImportError:
        return {}
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            data = fast_safe_load(f) or {}
    except Exception:  # noqa: BLE001
        return {}
    return data.get("secrets") or {}
