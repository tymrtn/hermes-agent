"""Start-on-demand support for loopback OpenAI-compatible fallback endpoints.

A fallback entry can point at a local inference server (MLX, llama.cpp,
LM Studio, vLLM) that is simply not running when the primary provider fails.
Activating such an entry lands on a refused connection, so the whole chain
looks broken even though the model is sitting on disk.

An entry may therefore carry ``start_command``: an absolute path to a script
that brings the server up.  :func:`ensure_local_endpoint` probes the entry's
health URL, launches that command when nothing answers, and waits (bounded)
for the server to start serving before the caller activates the fallback.

Safety rules, all enforced here rather than by callers:

* the target must be loopback — we never start a process to serve a remote host
* ``start_command`` must be an absolute path to an existing executable file,
  and is executed as ``[path]`` with no shell, so nothing is interpolated
* the wait is bounded by ``start_timeout`` (default 90s)
* an endpoint that already answers is never started a second time
* nothing is ever killed — starting is the only process action taken
"""

from __future__ import annotations

import ipaddress
import logging
import os
import subprocess
import time
import urllib.error
import urllib.request
from typing import Any, Optional
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)

DEFAULT_START_TIMEOUT_S = 90.0
MAX_START_TIMEOUT_S = 600.0
HEALTH_PROBE_TIMEOUT_S = 1.0
HEALTH_POLL_INTERVAL_S = 1.0


def _hostname(url: Any) -> str:
    if not isinstance(url, str) or not url.strip():
        return ""
    try:
        return (urlsplit(url.strip()).hostname or "").strip()
    except ValueError:
        return ""


def is_loopback_url(url: Any) -> bool:
    """True when *url* targets this machine's loopback interface.

    ``localhost`` is accepted by name; everything else must parse as a
    loopback IP (``127.0.0.0/8``, ``::1``).  A hostname that merely resolves
    to loopback is NOT accepted — resolution is attacker-influenceable and
    this gate decides whether we are allowed to spawn a process.
    """
    host = _hostname(url)
    if not host:
        return False
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def endpoint_health_url(entry: Any) -> str:
    """Health URL for a fallback entry: explicit ``health_url``, else ``base_url`` + ``/models``."""
    if not isinstance(entry, dict):
        return ""
    health = str(entry.get("health_url") or "").strip()
    if health:
        return health
    base_url = str(entry.get("base_url") or "").strip()
    if not base_url:
        return ""
    return base_url.rstrip("/") + "/models"


def endpoint_healthy(url: Any, timeout: float = HEALTH_PROBE_TIMEOUT_S) -> bool:
    """True when *url* answers HTTP.

    Any status below 500 counts — a 401/404 still proves a server is
    listening and speaking HTTP, which is all the caller needs to know
    before pointing a client at it.  Non-loopback URLs are never probed.
    """
    if not is_loopback_url(url):
        return False
    try:
        request = urllib.request.Request(str(url).strip(), method="GET")
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return int(getattr(response, "status", 0) or 0) < 500
    except urllib.error.HTTPError as exc:
        return int(getattr(exc, "code", 500) or 500) < 500
    except Exception as exc:
        logger.debug("Health probe %s failed: %s", url, exc)
        return False


def entry_start_command(entry: Any) -> str:
    """The configured ``start_command`` for *entry* (empty when unset)."""
    if not isinstance(entry, dict):
        return ""
    return str(entry.get("start_command") or "").strip()


def is_local_endpoint_entry(entry: Any) -> bool:
    """True when the entry's endpoint lives on loopback."""
    return is_loopback_url(endpoint_health_url(entry))


def is_startable_local_entry(entry: Any) -> bool:
    """True when the entry is a loopback endpoint Hermes is allowed to start."""
    return bool(entry_start_command(entry)) and is_local_endpoint_entry(entry)


def select_local_fallback_entry(chain: Any) -> Optional[dict]:
    """First local entry in *chain* — startable ones win over plain loopback ones.

    Backs ``/model local``: a chain may hold several local endpoints, and the
    one Hermes can bring up itself is the useful default.
    """
    entries = [entry for entry in (chain or []) if isinstance(entry, dict)]
    for entry in entries:
        if is_startable_local_entry(entry):
            return entry
    for entry in entries:
        if is_local_endpoint_entry(entry):
            return entry
    return None


def resolve_start_timeout(entry: Any) -> float:
    """Bounded ``start_timeout`` for *entry* in seconds."""
    raw = entry.get("start_timeout") if isinstance(entry, dict) else None
    try:
        timeout = float(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return DEFAULT_START_TIMEOUT_S
    return min(max(timeout, 0.0), MAX_START_TIMEOUT_S)


def validate_start_command(command: Any) -> str:
    """Return why *command* may not be launched, or "" when it is acceptable.

    The command is a path, never a shell string: it is spawned as ``[path]``
    so quoting, ``;``, ``&&`` and ``$(...)`` carry no meaning.  A config value
    written as a shell one-liner therefore fails the existence check here
    instead of being executed.
    """
    if not isinstance(command, str) or not command.strip():
        return "start_command is empty"
    path = command.strip()
    if not os.path.isabs(path):
        return f"start_command must be an absolute path: {path!r}"
    if not os.path.isfile(path):
        return f"start_command does not exist: {path}"
    if not os.access(path, os.X_OK):
        return f"start_command is not executable: {path}"
    return ""


def ensure_local_endpoint(entry: Any) -> dict:
    """Make the local endpoint behind *entry* serve, starting it if needed.

    Returns ``{"started": bool, "ready": bool, "error": str}``:

    * ``ready`` — the endpoint answers now; the caller may activate the entry
    * ``started`` — this call launched ``start_command`` (used for messaging)
    * ``error`` — why the endpoint is unusable; empty when ``ready`` is True

    Never raises: a failure here means "skip this fallback", not "kill the turn".
    """
    result = {"started": False, "ready": False, "error": ""}
    if not isinstance(entry, dict):
        result["error"] = "fallback entry is not a mapping"
        return result

    health_url = endpoint_health_url(entry)
    if not health_url:
        result["error"] = "fallback entry has no base_url or health_url"
        return result

    base_url = str(entry.get("base_url") or "").strip()
    for label, url in (("base_url", base_url), ("health_url", health_url)):
        if url and not is_loopback_url(url):
            result["error"] = (
                f"{label} {url!r} is not loopback; refusing to start a local server"
            )
            return result

    if endpoint_healthy(health_url):
        result["ready"] = True
        return result

    command = entry_start_command(entry)
    if not command:
        result["error"] = (
            f"{health_url} is not answering and no start_command is configured"
        )
        return result

    invalid = validate_start_command(command)
    if invalid:
        result["error"] = invalid
        return result

    try:
        subprocess.Popen(
            [command],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            cwd=os.path.dirname(command) or None,
            # Detach: the server outlives this turn, and Ctrl-C in Hermes
            # must not take down a server the user may still be using.
            start_new_session=True,
        )
    except Exception as exc:
        result["error"] = f"failed to launch {command}: {exc}"
        return result

    result["started"] = True
    timeout = resolve_start_timeout(entry)
    logger.info("Started local endpoint %s; waiting up to %.0fs for %s", command, timeout, health_url)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        time.sleep(HEALTH_POLL_INTERVAL_S)
        if endpoint_healthy(health_url):
            result["ready"] = True
            return result

    result["error"] = (
        f"launched {command} but {health_url} did not answer within {timeout:.0f}s"
    )
    return result
