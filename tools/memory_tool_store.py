"""MemoryStore — bounded, file-backed curated memory (MEMORY.md / USER.md).
Entries are joined by ``ENTRY_DELIMITER``; budgets are in chars (model-independent).
Module state that tests monkeypatch (``get_memory_dir``, ``fcntl``/``msvcrt``) stays
in ``tools.memory_tool`` and is read lazily."""

import logging
import time
from contextlib import contextmanager, suppress
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from utils import atomic_write_text
DEFAULT_SHARED_USER_CHAR_LIMIT = 1200
SHARED_USER_BLOCK_NOTE = "Shared across every Hermes profile and read-only: save new or profile-specific facts with the memory tool's 'user' target instead."
from tools.threat_patterns import first_threat_message as _first_threat_message

logger = logging.getLogger("tools.memory_tool")

# Block header prefixes rendered by _render_block; agent/conversation_compression.py
# matches them to detect a leftover block for an emptied target — keep in lockstep.
MEMORY_BLOCK_HEADERS = {'memory': 'MEMORY (your personal notes)', 'user': 'USER PROFILE (who the user is)', 'shared_user': 'SHARED USER PROFILE (read-only, shared across profiles)'}

ENTRY_DELIMITER = "\n§\n"


def _scan_memory_content(content: str) -> Optional[str]:
    """Error string if *content* matches injection/exfil patterns. Strict scope:
    memory enters the system prompt, so a poisoned entry persists across sessions."""
    return _first_threat_message(content, scope="strict")


def _error(message: str, **extra) -> Dict[str, Any]:
    return {"success": False, "error": message, **extra}


def _load_shared_user_profile(path_value: Any, char_limit: int) -> Tuple[str, Optional[str]]:
    """Read the optional shared, read-only user profile file.

    Returns ``(body, warning)``.  ``body`` is the text to inject — or a
    ``[BLOCKED: …]`` placeholder when the strict threat scanner fires; an empty
    body means no shared block is injected at all.  ``warning`` carries the
    reason the configured file was rejected.

    Every failure mode fails closed for the shared block ONLY: the agent still
    starts, and the profile-local USER.md keeps working.  A configured path that
    is relative, missing, a directory, unreadable, or over the char limit is
    never silently swapped for another file — the block is simply dropped and
    the reason logged.
    """
    if path_value is None or path_value == '':
        raw = ''
    elif not isinstance(path_value, str):
        return ('', f'memory.shared_user_profile_path must be a string absolute path; got {type(path_value).__name__}.')
    else:
        raw = path_value.strip()
    if not raw:
        return ('', None)
    if char_limit <= 0:
        return ('', f'memory.shared_user_char_limit must be a positive number (got {char_limit!r})')
    path = Path(raw)
    if not path.is_absolute():
        return ('', f'memory.shared_user_profile_path must be an absolute path, got {raw!r}. A relative path would resolve differently per process, so the shared profile is disabled rather than guessed.')
    try:
        if not path.exists():
            return ('', f'shared user profile file does not exist: {raw}')
        if path.is_dir():
            return ('', f'shared user profile path is a directory, not a file: {raw}')
        if not path.is_file():
            return ('', f'shared user profile path is not a regular file: {raw}')
        size = path.stat().st_size
        if size > char_limit * 4:
            return ('', f'shared user profile is too large ({size:,} bytes) for memory.shared_user_char_limit ({char_limit:,} chars): {raw}')
        text = path.read_text(encoding='utf-8').strip()
    except (OSError, IOError, UnicodeDecodeError, ValueError) as e:
        return ('', f'shared user profile could not be read ({e.__class__.__name__}: {e}): {raw}')
    if not text:
        return ('', None)
    if len(text) > char_limit:
        return ('', f'shared user profile is {len(text):,} chars, over the memory.shared_user_char_limit of {char_limit:,}: {raw}. Trim the file or raise the limit — it is not truncated, because cutting a profile mid-sentence can invert what it says.')
    from tools.threat_patterns import scan_for_threats
    findings = scan_for_threats(text, scope='strict')
    if findings:
        ids = ', '.join(findings)
        return (f'[BLOCKED: the shared user profile contained threat pattern(s): {ids}. Removed from the system prompt.]', f'shared user profile blocked by the memory threat scanner ({ids}): {raw}')
    return (text, None)
def _drift_error(path: Path, bak_path: str) -> Dict[str, Any]:
    """External drift: the file wouldn't round-trip, so flushing would discard content."""
    return _error((
        f"Refusing to write {path.name}: file on disk has content that wouldn't round-trip "
        f"through the memory tool (likely added by the patch tool, a shell append, a manual edit, "
        f"or a concurrent session). A snapshot was saved to {bak_path}. Resolve the drift first — "
        f"either rewrite the file as a clean §-delimited list of entries, or move the extra "
        f"content out — then retry. This guard exists to prevent silent data loss (issue #26045)."
    ), drift_backup=bak_path, remediation=(
        "Open the .bak file, integrate the missing entries into the memory tool one at a time via "
        "memory(action=add, content=...), then remove or rewrite the original file to a clean state."))


def _read_failed_error(path: Path) -> Dict[str, Any]:
    """Existing-but-unreadable file: saving from an assumed-empty view would wipe it."""
    return _error(
        f"Refusing to write {path.name}: the file exists on disk but could not be read right now "
        f"(temporarily locked by another program, a permission change, invalid/corrupt text encoding, "
        f"or a filesystem error). Treating an unreadable file as empty and saving would wipe existing "
        f"memory, so the write is refused. Nothing was changed — retry in a moment.")


def _find_unique_match(entries: List[str], old_text: str) -> Tuple[Optional[int], bool]:
    """``(index, ambiguous)`` for entries containing *old_text*. Exact-duplicate
    matches are safe (first wins); distinct matches → ``(None, True)``."""
    matches = [i for i, e in enumerate(entries) if old_text in e]
    if len({entries[i] for i in matches}) > 1:
        return None, True
    return (matches[0] if matches else None), False


class MemoryStore:
    """Bounded curated memory with file persistence; one instance per AIAgent.
    ``_system_prompt_snapshot`` is frozen at load time (prefix-cache stable);
    ``memory_entries`` / ``user_entries`` are live state persisted to disk."""

    # Failed consolidation attempts (overflow / zero-match) allowed per turn before
    # a TERMINAL "save skipped" result, so a fragile replace/add can't loop the turn
    # to budget exhaustion and suppress the user's reply.
    # See #42405.
    _MAX_CONSOLIDATION_FAILURES_PER_TURN = 3

    def __init__(self, memory_char_limit: int=2200, user_char_limit: int=1375, *, memory_enabled: bool=True, user_profile_enabled: bool=True, warm_memory_enabled: bool=True, warm_memory_char_limit: int=50000, warm_user_char_limit: int=25000, shared_user_profile_path: str='', shared_user_char_limit: int=DEFAULT_SHARED_USER_CHAR_LIMIT):
        self.memory_entries: List[str] = []
        self.user_entries: List[str] = []
        self.warm_memory_entries: List[str] = []
        self.warm_user_entries: List[str] = []
        self.memory_char_limit, self.user_char_limit = memory_char_limit, user_char_limit
        self.memory_enabled, self.user_profile_enabled = memory_enabled, user_profile_enabled
        self.warm_memory_enabled = warm_memory_enabled
        self.warm_memory_char_limit = warm_memory_char_limit
        self.warm_user_char_limit = warm_user_char_limit
        self.shared_user_profile_path = shared_user_profile_path
        self.shared_user_char_limit = shared_user_char_limit
        self.shared_user_warning: Optional[str] = None
        self._shared_user_block = ''
        self._shared_user_loaded = False
        self._system_prompt_snapshot: Dict[str, str] = {'memory': '', 'user': '', 'shared_user': ''}
        self._consolidation_failures = 0  # per turn; reset by reset_consolidation_failures()

    # Per-turn counter of failed at-capacity consolidation attempts; reset at each turn boundary by
    # reset_consolidation_failures() (#42405).
    def target_enabled(self, target: str) -> bool:
        return self.user_profile_enabled if target == "user" else self.memory_enabled

    def reset_consolidation_failures(self) -> None:
        """Call at turn start."""
        self._consolidation_failures = 0

    def _consolidation_failure(self, response: Dict[str, Any]) -> Dict[str, Any]:
        """Count a consolidation failure: under the per-turn cap return ``response``
        (it says how to retry); past it a TERMINAL result so the model stops looping.

        Once the cap is exceeded, drop the retry instruction and return a TERMINAL result so the model stops
        looping memory calls and proceeds to answer the user — a failed memory side effect must never block
        the turn's reply (#42405).
        """
        self._consolidation_failures += 1
        if self._consolidation_failures <= self._MAX_CONSOLIDATION_FAILURES_PER_TURN:
            return response
        return {"success": False, "done": True, "error": (
            f"Memory consolidation failed {self._consolidation_failures} times this turn. Stop retrying "
            "memory calls — leave memory unchanged for now and continue with your reply to the user. "
            "The fact can be saved in a later turn.")}

    def load_from_disk(self):
        """Load MEMORY.md / USER.md and capture the frozen system-prompt snapshot.
        Threat hits are replaced by a ``[BLOCKED: …]`` placeholder in the SNAPSHOT only;
        live lists keep the raw text so the user can see and remove poisoned entries
        (dropping them silently would hide the attack)."""
        from tools.threat_patterns import scan_for_threats

        def _sanitize(entry, filename):
            # Strict scope, same as writes; empty / already-blocked entries pass through.
            findings = scan_for_threats(entry, scope="strict") if entry and not entry.startswith("[BLOCKED:") else None
            if not findings:
                return entry
            logger.warning("Memory entry from %s blocked at load time: %s", filename, ", ".join(findings))
            return (f"[BLOCKED: {filename} entry contained threat pattern(s): {', '.join(findings)}. "
                    f"Removed from system prompt; use memory(action=remove) to delete the original.]")

        self._load_shared_user_once()
        self._system_prompt_snapshot["shared_user"] = self._shared_user_block
        for target in ("memory", "user"):
            path = self._path_for(target)
            path.parent.mkdir(parents=True, exist_ok=True)
            # Deduplicate (order-preserving, first occurrence wins).
            entries = list(dict.fromkeys(self._read_file(path)))
            self._set_entries(target, entries)
            self._system_prompt_snapshot[target] = self._render_block(target, [_sanitize(e, path.name) for e in entries])
            warm_entries = list(dict.fromkeys(self._read_file(self._path_for(target, tier="warm"))))
            self._set_entries(target, warm_entries, tier="warm")
        self._snapshot_loaded = True
    def _load_shared_user_once(self) -> None:
        """Resolve the shared read-only user profile exactly once per store.

            ``load_from_disk()`` runs again on every ``memory(action=read)``.
            Re-reading the shared file there would let an external edit change the
            system prompt mid-session, so the shared block is frozen the first time
            instead — the same guarantee the local snapshot gives, one level
            stronger because nothing in-process can write this file.
            """
        if self._shared_user_loaded:
            return
        self._shared_user_loaded = True
        body, warning = _load_shared_user_profile(self.shared_user_profile_path, self.shared_user_char_limit)
        self.shared_user_warning = warning
        if warning:
            logger.warning('Shared user profile not injected: %s', warning)
        self._shared_user_block = self._render_shared_user_block(body) if body else ''
    def _render_shared_user_block(self, body: str) -> str:
        """Render the shared profile block, bounded by its own char limit."""
        limit = self.shared_user_char_limit
        current = len(body)
        pct = min(100, int(current / limit * 100)) if limit > 0 else 0
        header = f"{MEMORY_BLOCK_HEADERS['shared_user']} [{pct}% — {current:,}/{limit:,} chars]"
        separator = '═' * 46
        return f'{separator}\n{header}\n{separator}\n{SHARED_USER_BLOCK_NOTE}\n{body}'

    @staticmethod
    @contextmanager
    def _file_lock(path: Path):
        """Exclusive lock on a separate .lock file so the memory file itself can
        still be atomically replaced."""
        from tools import memory_tool as _mt  # fcntl/msvcrt live (and are patched) there
        fcntl, msvcrt = _mt.fcntl, _mt.msvcrt
        lock_path = path.with_suffix(path.suffix + ".lock")
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        if fcntl is None and msvcrt is None:
            yield
            return
        with open(lock_path, "a+", encoding="utf-8") as fd:
            def _flock(unlock: bool):
                if fcntl:
                    fcntl.flock(fd, fcntl.LOCK_UN if unlock else fcntl.LOCK_EX)
                else:
                    fd.seek(0)
                    msvcrt.locking(fd.fileno(), msvcrt.LK_UNLCK if unlock else msvcrt.LK_LOCK, 1)
            _flock(False)
            try:
                yield
            finally:
                with suppress(OSError):
                    _flock(True)

    @staticmethod
    def _path_for(target: str, tier: str = "hot") -> Path:
        # Fail loud rather than falling through to MEMORY.md: the shared
        # profile has no writable path, and silently writing a profile-local
        # file for a "shared_user" write would be the exact contamination this
        # store exists to prevent.
        if target == "shared_user":
            raise ValueError(
                "The shared user profile is read-only and has no writable memory "
                "file. Write profile-local facts with target='user'."
            )
        from tools.memory_tool import get_memory_dir
        mem_dir = get_memory_dir()
        if tier == "warm":
            if target == "user":
                return mem_dir / "WARM_USER.md"
            return mem_dir / "WARM_MEMORY.md"
        if target == "user":
            return mem_dir / "USER.md"
        return mem_dir / "MEMORY.md"

    def save_to_disk(self, target: str, tier: str = "hot"):
        """Persist entries to the appropriate file. Called after every mutation."""
        path = self._path_for(target, tier=tier)
        path.parent.mkdir(parents=True, exist_ok=True)
        self._write_file(path, self._entries_for(target, tier=tier))

    def _entries_for(self, target: str, tier: str = "hot") -> List[str]:
        if tier == "warm":
            if target == "user":
                return self.warm_user_entries
            return self.warm_memory_entries
        if target == "user":
            return self.user_entries
        return self.memory_entries

    def _set_entries(self, target: str, entries: List[str], tier: str = "hot"):
        if tier == "warm":
            if target == "user":
                self.warm_user_entries = entries
            else:
                self.warm_memory_entries = entries
            return
        if target == "user":
            self.user_entries = entries
        else:
            self.memory_entries = entries

    def _char_count(self, target: str, *, tier: str='hot') -> int:
        return len(ENTRY_DELIMITER.join(self._entries_for(target, tier=tier)))

    def _char_limit(self, target: str, tier: str = "hot") -> int:
        if tier == "warm":
            if target == "user":
                return self.warm_user_char_limit
            return self.warm_memory_char_limit
        if target == "user":
            return self.user_char_limit
        return self.memory_char_limit
    def _replacement_candidates(self, target: str, content_len: int, *, max_items: int = 3) -> List[Dict[str, Any]]:
        """Return compact hot-memory prune/replace candidates for full-memory UX."""
        entries = self._entries_for(target)
        current = self._char_count(target)
        limit = self._char_limit(target)
        needed = max(0, current + (len(ENTRY_DELIMITER) if entries else 0) + content_len - limit)
        ranked = sorted(entries, key=len, reverse=True)[:max_items]
        candidates: List[Dict[str, Any]] = []
        for entry in ranked:
            preview = entry[:120] + ("..." if len(entry) > 120 else "")
            old_text = entry[:80] + ("..." if len(entry) > 80 else "")
            candidates.append({
                "old_text": old_text,
                "preview": preview,
                "chars": len(entry),
                "would_free_enough": len(entry) >= needed,
            })
        return candidates

    def _usage(self, target: str) -> str:
        return f"{self._char_count(target):,}/{self._char_limit(target):,}"

    def _usage_pct(self, target: str, current: int) -> str:
        limit = self._char_limit(target)
        return f"{min(100, int((current / limit) * 100)) if limit > 0 else 0}% — {current:,}/{limit:,} chars"

    def _failure_with_entries(self, target: str, message: str) -> Dict[str, Any]:
        """Consolidation failure carrying the live entries so the model can consolidate."""
        return self._consolidation_failure(
            _error(message, current_entries=self._entries_for(target), usage=self._usage(target), replacement_candidates=self._replacement_candidates(target, 0)))

    def _mutate(self, target: str, mutate, *, skip_drift: bool = False, tier: str = "hot") -> Dict[str, Any]:
        """Lock, re-read from disk, run ``mutate(entries, limit)`` -> ``(new_entries, message)``
        or an error dict, then persist and return the success response. The reload aborts
        on an existing-but-unreadable file (even append-only ``add`` rewrites the whole
        file) and, unless *skip_drift*, on external drift (flushing would discard
        un-roundtrippable content). Drift check and parse use the SAME raw snapshot —
        a failed second read used to count as "no drift"."""
        if tier not in {"hot", "warm"}:
            return _error("tier must be 'hot' or 'warm'.")
        if not self.target_enabled(target):
            return _error("This memory target is disabled in config.")
        path = self._path_for(target, tier=tier)
        with self._file_lock(path):
            raw, read_ok = self._read_raw_checked(path)
            if not read_ok:
                return _read_failed_error(path)
            bak = None if skip_drift else self._detect_external_drift(target, raw, tier=tier)
            self._set_entries(target, list(dict.fromkeys(self._parse_entries(raw))), tier=tier)
            if bak:
                return _drift_error(path, bak)
            result = mutate(self._entries_for(target, tier=tier), self._char_limit(target, tier=tier))
            if isinstance(result, dict):
                return result
            self._set_entries(target, result[0], tier=tier)
            path.parent.mkdir(parents=True, exist_ok=True)
            self._write_file(path, result[0])
            return self._success_response(target, result[1], tier=tier)

    def add(self, target: str, content: str, *, tier: str='hot') -> Dict[str, Any]:
        """Append a new entry. Returns error if it would exceed the char limit."""
        content = content.strip()
        if not content:
            return _error("Content cannot be empty.")
        if tier not in {'hot', 'warm'}:
            return {'success': False, 'error': "tier must be 'hot' or 'warm'."}
        if scan_error := _scan_memory_content(content):
            return _error(scan_error)

        def _add(entries, limit):
            if content in entries:
                return self._success_response(target, 'Entry already exists (no duplicate added).', tier=tier)
            if len(ENTRY_DELIMITER.join(entries + [content])) > limit:
                if tier == "hot" and self.warm_memory_enabled:
                    warm_result = self.add(target, content, tier="warm")
                    if warm_result.get("success"):
                        warm_result.update(hot_full=True, hot_usage=self._usage(target),
                            replacement_candidates=self._replacement_candidates(target, len(content)),
                            message="Hot memory is full; entry was saved to warm memory instead. Warm memory is durable and retrievable with memory(action=read), but is not injected into the system prompt.")
                        return warm_result
                return self._failure_with_entries(target, (
                    f"Memory at {self._char_count(target):,}/{limit:,} chars. Adding this entry "
                    f"({len(content)} chars) would exceed the limit. Consolidate now: use 'replace' to merge "
                    f"overlapping entries into shorter ones or 'remove' stale or less important entries (see "
                    f"current_entries below), then retry this add — all in this turn."))
            return entries + [content], "Entry added."
        # Append-only: skip the drift guard (appending never clobbers foreign
        # content) but still refuse a failed read — add rewrites the WHOLE file.
        return self._mutate(target, _add, skip_drift=True, tier=tier)

    def replace(self, target: str, old_text: str, new_content: str, *, tier: str='hot') -> Dict[str, Any]:
        """Find entry containing old_text substring, replace it with new_content."""
        new_content = new_content.strip()
        if not old_text.strip():
            return _error("old_text cannot be empty.")
        if not new_content:
            return _error("new_content cannot be empty. Use 'remove' to delete entries.")
        if scan_error := _scan_memory_content(new_content):
            return _error(scan_error)
        return self._edit(target, old_text.strip(), new_content, tier=tier)

    def remove(self, target: str, old_text: str, *, tier: str='hot') -> Dict[str, Any]:
        """Remove the entry containing old_text substring."""
        if not old_text.strip():
            return _error("old_text cannot be empty.")
        return self._edit(target, old_text.strip(), None, tier=tier)

    def _edit(self, target: str, old_text: str, new_content: Optional[str], *, tier: str = "hot") -> Dict[str, Any]:
        """Locked replace (``new_content`` set) or remove (None) of the entry matching *old_text*."""
        def _apply(entries, limit):
            idx, ambiguous = _find_unique_match(entries, old_text)
            if ambiguous:
                return _error(f"Multiple entries matched '{old_text}'. Be more specific.",
                              matches=[e[:80] + ("..." if len(e) > 80 else "") for e in entries if old_text in e])
            if idx is None:
                return self._consolidation_failure(_error(
                    f"No entry matched '{old_text}'. Check current_entries below and retry with the exact text "
                    f"of the entry you want to {'replace' if new_content else 'remove'}.", current_entries=entries))
            replaced = entries[:idx] + ([] if new_content is None else [new_content]) + entries[idx + 1:]
            if new_content is None:
                return replaced, "Entry removed."
            new_total = len(ENTRY_DELIMITER.join(replaced))
            if new_total > limit:
                return self._failure_with_entries(target, (
                    f"Replacement would put memory at {new_total:,}/{limit:,} chars. Shorten the new content, "
                    f"or 'remove' other stale or less important entries to make room (see current_entries "
                    f"below), then retry — all in this turn."))
            return replaced, "Entry replaced."
        return self._mutate(target, _apply, tier=tier)

    @staticmethod
    def _apply_batch_op(working: List[str], act: str, content: str, old_text: str, pos: str) -> Optional[str]:
        """Apply one batch op to *working* in place; return an error message or None."""
        if act == "add":
            if not content:
                return f"{pos}: content is required."
            if content not in working:  # idempotent -- skip duplicate, don't fail the batch
                working.append(content)
            return None
        if act not in ("replace", "remove"):
            return f"{pos}: unknown action. Use add, replace, or remove."
        if not old_text:
            return f"{pos}: old_text is required."
        if act == "replace" and not content:
            return f"{pos}: content is required (use action='remove' to delete)."
        idx, ambiguous = _find_unique_match(working, old_text)
        if ambiguous:
            return f"{pos}: '{old_text}' matched multiple distinct entries -- be more specific."
        if idx is None:
            return f"{pos}: no entry matched '{old_text}'."
        working[idx:idx + 1] = [content] if act == "replace" else []
        return None

    def read(self, target: str, tier: str='all') -> Dict[str, Any]:
        """Return live memory entries without changing prompt injection state."""
        snapshot = dict(self._system_prompt_snapshot)
        self.load_from_disk()
        self._system_prompt_snapshot = snapshot
        if tier not in {'hot', 'warm', 'all'}:
            return {'success': False, 'error': "tier must be 'hot', 'warm', or 'all'."}
        result: Dict[str, Any] = {'success': True, 'target': target, 'tier': tier, 'hot_usage': f"{self._char_count(target, tier='hot'):,}/{self._char_limit(target, tier='hot'):,}", 'warm_usage': f"{self._char_count(target, tier='warm'):,}/{self._char_limit(target, tier='warm'):,}", 'note': 'Only hot entries are injected into the system prompt; warm entries are durable retrieval-only memory.'}
        if tier in {'hot', 'all'}:
            result['hot_entries'] = self._entries_for(target, tier='hot')
        if tier in {'warm', 'all'}:
            result['warm_entries'] = self._entries_for(target, tier='warm')
        return result
    def apply_batch(self, target: str, operations: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Apply add/replace/remove ops atomically against the FINAL budget, so one call
        can free space and add entries. All-or-nothing: any malformed / unmatched op or
        an over-limit result writes NOTHING and returns the first failure plus live state."""
        if not operations:
            return _error("operations list is empty.")
        ops = [op or {} for op in operations]
        # Scan every add/replace content BEFORE touching disk -- one poisoned op rejects the batch.
        for i, op in enumerate(ops):
            scan_error = op.get("action") in {"add", "replace"} and op.get("content") and _scan_memory_content(op["content"])
            if scan_error:
                return _error(f"Operation {i + 1}: {scan_error}")

        def _apply(entries, limit):
            working = list(entries)  # only committed if the whole batch validates
            for i, op in enumerate(ops):
                act = op.get("action")
                msg = self._apply_batch_op(working, act, (op.get("content") or op.get("new_text") or "").strip(),
                                           (op.get("old_text") or "").strip(), f"Operation {i + 1} ({act or 'unknown'})")
                if msg:
                    return self._failure_with_entries(target, msg + " No operations were applied (batch is all-or-nothing).")
            new_total = len(ENTRY_DELIMITER.join(working))  # budget check against the FINAL state only
            if new_total > limit:
                return self._failure_with_entries(target, (
                    f"After applying all {len(operations)} operations, memory would be at "
                    f"{new_total:,}/{limit:,} chars -- over the limit. Remove or shorten more "
                    f"entries in the same batch (see current_entries below), then retry."))
            return working, f"Applied {len(operations)} operation(s)."
        return self._mutate(target, _apply)

    def format_for_system_prompt(self, target: str) -> Optional[str]:
        """Frozen load-time snapshot (NOT live state — mid-session writes don't touch
        it, preserving the prefix cache); None if empty."""
        return self._system_prompt_snapshot.get(target, "") or None

    def _success_response(self, target: str, message: Optional[str] = None, tier: str = "hot") -> Dict[str, Any]:
        # A successful write means the consolidation loop made progress, so the
        # per-turn failure budget resets (#42405).
        self._consolidation_failures = 0
        entries = self._entries_for(target, tier=tier)
        current = self._char_count(target, tier=tier)
        limit = self._char_limit(target, tier=tier)
        pct = min(100, int((current / limit) * 100)) if limit > 0 else 0

        # The success response is intentionally TERMINAL: it confirms the write
        # landed and tells the model to stop. We do NOT echo the full entries
        # list here -- dumping it invites the model to "find more to fix" and
        # re-issue the same operations (observed thrash: the correct batch on
        # call 1, then 5 redundant repeats). Entries are only shown on the
        # error/over-budget paths, where the model genuinely needs them to
        # decide what to consolidate.
        resp = {
            "success": True,
            "done": True,
            "target": target,
            "tier": tier,
            "injected": tier == "hot",
            "usage": f"{pct}% — {current:,}/{limit:,} chars",
            "entry_count": len(entries),
        }
        if message:
            resp["message"] = message
        resp["note"] = "Write saved. This update is complete — do not repeat it."
        return resp

    def _render_block(self, target: str, entries: List[str]) -> str:
        """System prompt block: header + usage indicator + entries ("" when empty)."""
        if not entries:
            return ""
        content, sep = ENTRY_DELIMITER.join(entries), "═" * 46
        title = MEMORY_BLOCK_HEADERS["user" if target == "user" else "memory"]
        return f"{sep}\n{title} [{self._usage_pct(target, len(content))}]\n{sep}\n{content}"

    @staticmethod
    def _read_raw_checked(path: Path) -> Tuple[str, bool]:
        """``(raw, read_ok)``; ``read_ok`` is False ONLY when the file EXISTS but can't be
        read. Decoding stays STRICT (``errors="replace"`` would hand callers a lossy view
        a save then persists); ``utf-8-sig`` strips a Notepad BOM off the first entry."""
        if not path.exists():
            return "", True
        try:
            # utf-8-sig strips a leading UTF-8 BOM (Notepad-edited memory files on Windows) and is
            # byte-identical to utf-8 otherwise. Plain utf-8 kept U+FEFF glued to the first entry,
            # corrupting matching/dedup for that entry forever (#10878 / PR #10888). Decode errors stay
            # STRICT on purpose: errors="replace" would hand read-modify-write callers a lossy view that a
            # subsequent save persists over the real bytes — the wipe class documented above. Undecodable
            # bytes must surface as read_ok=False.
            return path.read_text(encoding="utf-8-sig"), True
        except (OSError, UnicodeDecodeError):
            return "", False

    @staticmethod
    def _parse_entries(raw: str) -> List[str]:
        """Stripped, non-empty entries; splits on the FULL delimiter so a bare "§" survives."""
        return [e for e in (x.strip() for x in raw.split(ENTRY_DELIMITER)) if e]

    @staticmethod
    def _read_file(path: Path) -> List[str]:
        """Entries of a memory file ([] on any error). Read-only callers only; mutation
        paths use ``_read_raw_checked`` so they can refuse to overwrite an unreadable file."""
        return MemoryStore._parse_entries(MemoryStore._read_raw_checked(path)[0])

    @staticmethod
    def _write_file(path: Path, entries: List[str]):
        """Atomic temp-file + rename: readers never see a truncated file. Also used by
        agent/learning_mutations.py."""
        try:
            atomic_write_text(path, ENTRY_DELIMITER.join(entries), tmp_prefix=".mem_")
        except OSError as e:
            raise RuntimeError(f"Failed to write memory file {path}: {e}")

    def _detect_external_drift(self, target: str, raw: str, *, tier: str='hot') -> Optional[str]:
        """``.bak.<ts>`` snapshot path if *raw* shows external drift, else None. Signals:
        round-trip mismatch, or one entry over the whole-file limit (no tool-written
        entry can be — an external writer appended free-form text)."""
        parsed = self._parse_entries(raw)
        if not raw.strip() or (raw.strip() == ENTRY_DELIMITER.join(parsed)
                               and max(map(len, parsed), default=0) <= self._char_limit(target, tier=tier)):
            return None
        path = self._path_for(target, tier=tier)
        bak_path = path.with_suffix(path.suffix + f".bak.{int(time.time())}")
        try:
            bak_path.write_text(raw, encoding="utf-8")
        except OSError:
            return str(bak_path) + " (BACKUP FAILED — file unchanged on disk)"
        return str(bak_path)
