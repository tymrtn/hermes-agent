"""Phase 2 destination adapters: memory, skill, and project-document writes.

Safety model (design §6 promotion algorithm, steps 5-9):

- An adapter is constructed with an explicit *home* directory. There are no
  ambient defaults; nothing in this module can name a live profile path on
  its own, so tests and dry runs operate on fixture homes by construction.
- Every mutation is: snapshot revision -> backup -> render -> bounded-diff
  check -> optimistic revision re-check -> atomic write -> production-
  compatible read-back -> retrieval proof. The caller (promotion.py) marks
  the candidate promoted only after all of it passes.
- Each promoted record is a keyed region owned by its `record_key`
  (canonical record identity = hash(destination, subject)). The bounded-diff
  check proves byte-exactly that stripping the record's own region from the
  old and new content leaves identical residue — a write can never touch
  anything outside the record it owns.
- Backups capture original bytes; `restore_backup` returns the destination
  to a byte-identical pre-write state (including deleting files that did not
  exist before).

Read-back is *production-compatible*: hot memory is verified through the
index file exactly as the session harness injects it (index line -> linked
fact file), skills through frontmatter parsing a skill loader requires, and
project docs through section lookup. Retrieval proofs exercise the intended
retrieval route (hot injection, warm term search, skill lookup, project doc
section) rather than re-reading the written bytes.
"""
from __future__ import annotations

import ctypes
import errno
import fcntl
import json
import os
import re
import shlex
import shutil
import stat
import sys
import tempfile
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from ..canonical import is_safe_identity, sha256_hex
from ..errors import (ConcurrentRevisionError, DestinationError, DiffBoundError,
                      ReadBackError, RetrievalProofError)

try:
    # PyYAML is optional by design: the runtime stays standard-library-only.
    # When a standard loader IS importable, rendered frontmatter must actually
    # load under it (and round-trip) before read-back may issue proof.
    import yaml as _yaml
except ImportError:  # pragma: no cover - depends on the host environment
    _yaml = None

# A single record region (markers + payload) may never exceed this; the
# normalized_claim contract cap is 4000 chars, envelope included.
MAX_RECORD_REGION_CHARS = 6000

_BEGIN = "<!-- dc3:begin {key} rev={rev} -->"
_END = "<!-- dc3:end {key} -->"
_REGION_RE_TMPL = r"<!-- dc3:begin {key} rev=\d+ -->\n.*?<!-- dc3:end {key} -->\n?"
_PROV_RE = re.compile(
    r"<!-- dc3:record (?P<key>[0-9a-f]{32}) rev=(?P<rev>\d+) "
    r"candidate=(?P<cand>\S+) run=(?P<run>\S+) -->")

BACKUP_MANIFEST_NAME = "dc3-backup-manifest.json"
WRITE_JOURNAL_NAME = "dc3-write-journal.json"
_SAFE_RELATIVE_PATH_RE = re.compile(r"^(?!/)(?!.*(?:^|/)\.\.(?:/|$)).+$")
# Receipt/manifest format remains v2 so existing validated receipts remain
# readable.  The mutable write journal is independently versioned: v3 adds
# per-target staged/exchange evidence needed for crash-safe reconciliation.
_BACKUP_FORMAT_VERSION = 2
_WRITE_JOURNAL_VERSION = 3


def _canonical_home_identity(home: Path) -> dict[str, str | int]:
    """Return the durable identity of one explicitly selected destination home.

    A path alone is not enough: a shared backup root can otherwise replay a
    journal into another shadow home with the same destination selector.  The
    resolved path plus directory device/inode binds a journal to both the
    caller-selected home and that concrete directory instance.
    """
    canonical = home.resolve()
    try:
        st = canonical.stat()
    except OSError as exc:
        raise DestinationError(
            f"cannot identify explicit destination home {canonical}: {exc}") from exc
    if not canonical.is_dir():
        raise DestinationError(
            f"explicit destination home {canonical} is not a directory")
    return {
        "canonical_path": str(canonical),
        "device": st.st_dev,
        "inode": st.st_ino,
    }


def _is_home_identity(value: object) -> bool:
    if not isinstance(value, dict) or set(value) != {
            "canonical_path", "device", "inode"}:
        return False
    path = value["canonical_path"]
    return (isinstance(path, str) and Path(path).is_absolute()
            and isinstance(value["device"], int)
            and not isinstance(value["device"], bool)
            and isinstance(value["inode"], int)
            and not isinstance(value["inode"], bool))


@dataclass(frozen=True)
class PromotionRecord:
    """The bounded write intent derived from a validated candidate."""
    candidate_id: str
    content_revision: int
    destination: str
    record_key: str
    subject: str
    claim: str
    retrieval_terms: tuple[str, ...]
    run_id: str
    memory_type: str = "project"


@dataclass(frozen=True)
class ExistingRecord:
    """A record already present at the destination (dc3-owned or pre-existing)."""
    record_key: str | None
    subject: str | None
    text: str
    location: str


@dataclass(frozen=True)
class BackupEntry:
    target: Path
    target_rel: str
    existed: bool
    backup_path: Path | None
    fingerprint: str | None


@dataclass(frozen=True)
class BackupRef:
    backup_dir: Path
    entries: tuple[BackupEntry, ...] = field(default_factory=tuple)
    home_identity: dict[str, str | int] = field(default_factory=dict)
    # The explicit shadow backup root this evidence namespace was allocated
    # under.  When present, every journal/manifest/artifact mutation re-walks
    # symlink-free confinement below it, so a namespace component swapped for
    # a symlink after allocation is refused instead of followed.
    backup_root: Path | None = None

    @property
    def ref(self) -> str:
        return str(self.backup_dir.resolve())

    @property
    def manifest_path(self) -> Path:
        return self.backup_dir / BACKUP_MANIFEST_NAME

    @property
    def journal_path(self) -> Path:
        return self.backup_dir / WRITE_JOURNAL_NAME

    def rollback_metadata(self) -> dict:
        """Structured, non-executable recovery data for the receipt.

        The destination paths are relative to the explicit adapter home.  This
        is intentionally sufficient for validation/audit but not a command the
        store can use to access arbitrary filesystem locations.
        """
        manifest_bytes = self.manifest_path.read_bytes()
        return {
            "version": _BACKUP_FORMAT_VERSION,
            "kind": "dc3_filesystem_backup",
            "manifest": BACKUP_MANIFEST_NAME,
            "manifest_fingerprint": "sha256:" + sha256_hex(manifest_bytes),
            "home_identity": self.home_identity,
            "entries": self.entry_metadata(),
        }

    def entry_metadata(self) -> list[dict]:
        return [
            {
                "target": e.target_rel,
                "existed": e.existed,
                "backup_file": e.backup_path.name if e.backup_path else None,
                "fingerprint": e.fingerprint,
            }
            for e in self.entries
        ]

    def mark_state(self, state: str) -> None:
        """Durably advance the local recovery journal state."""
        self._update_journal(lambda data: data.__setitem__("state", state))

    def journal_data(self) -> dict:
        data = json.loads(self.journal_path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise DestinationError(f"invalid write journal {self.journal_path}")
        return data

    def _assert_confined_artifact(self, path: Path) -> None:
        if self.backup_root is None:
            return
        _assert_confined_target(self.backup_root, path, what="backup artifact")

    def _update_journal(self, update) -> None:
        self._assert_confined_artifact(self.journal_path)
        data = self.journal_data()
        update(data)
        _atomic_write(self.journal_path, _canonical_json(data) + "\n")

    def _target_rel(self, target: Path) -> str:
        home = Path(str(self.home_identity["canonical_path"])).resolve()
        try:
            rel = target.resolve().relative_to(home).as_posix()
        except ValueError as exc:
            raise DestinationError(
                f"journal target {target} escapes destination home {home}") from exc
        if not _safe_relative_path(rel):
            raise DestinationError(f"unsafe journal target {rel!r}")
        return rel

    def _temp_rel(self, temp: Path) -> str:
        return self._target_rel(temp)

    def _update_target(self, target: Path, **changes: object) -> None:
        target_rel = self._target_rel(target)

        def update(data: dict) -> None:
            progress = data.setdefault("target_progress", {})
            if not isinstance(progress, dict):
                raise DestinationError(
                    f"invalid target progress in {self.journal_path}")
            item = progress.get(target_rel, {})
            if not isinstance(item, dict):
                raise DestinationError(
                    f"invalid target journal entry for {target_rel}")
            item = dict(item)
            item.update(changes)
            progress[target_rel] = item

        self._update_journal(update)

    def stage_path(self, target: Path) -> Path:
        """Return a unique, journalable staging name beside *target*."""
        return target.parent / (".dc3-tmp-" + uuid.uuid4().hex)

    def record_stage_intent(self, target: Path, temp: Path, *,
                            expected: bytes | None, desired: bytes,
                            mode: str, purpose: str = "commit") -> None:
        """Persist a temp name before it can contain transaction bytes.

        The intent is intentionally durable before creating the file.  A crash
        in either side of staging can therefore be reconciled without treating
        an unknown ``.dc3-tmp-*`` file as disposable.
        """
        if mode not in ("exchange", "create") or purpose not in ("commit", "rollback"):
            raise DestinationError("invalid journaled atomic-write operation")
        self._update_target(
            target,
            mode=mode,
            purpose=purpose,
            phase="stage_intent",
            temp_rel=self._temp_rel(temp),
            expected_fingerprint=_bytes_fingerprint(expected),
            desired_fingerprint=_bytes_fingerprint(desired),
            staged_identity=None,
            temp_present=True,
            quarantined_artifact=None,
        )

    def arm_atomic_operation(self, target: Path,
                             staged_identity: tuple[int, int]) -> None:
        self._update_target(target, phase="exchange_pending",
                            staged_identity=_identity_value(staged_identity))

    def mark_exchange_swapped(self, target: Path) -> None:
        self._update_target(target, phase="exchange_swapped")

    def mark_exchange_reversed(self, target: Path) -> None:
        self._update_target(target, phase="exchange_reversed")

    def mark_target_committed(self, target: Path) -> None:
        self._update_target(target, phase="committed")

    def mark_temp_cleaned(self, target: Path) -> None:
        self._update_target(target, temp_present=False)

    def mark_target_quarantined(self, target: Path, artifact: Path) -> None:
        try:
            artifact_rel = artifact.resolve().relative_to(
                self.backup_dir.resolve()).as_posix()
        except ValueError as exc:
            raise DestinationError(
                f"quarantine artifact {artifact} escapes backup {self.backup_dir}") from exc
        self._update_target(target, phase="quarantined",
                            quarantined_artifact=artifact_rel,
                            temp_present=False)

    def rollback_command(self) -> str:
        """Human-readable fallback only; rollback_metadata is authoritative."""
        parts = []
        for e in self.entries:
            if e.existed:
                parts.append("cp " + shlex.quote(str(e.backup_path)) + " "
                             + shlex.quote(str(e.target)))
            else:
                parts.append("rm -f " + shlex.quote(str(e.target)))
        return " && ".join(parts)


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".dc3-tmp-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(content)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
        try:
            directory_fd = os.open(str(path.parent), os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except OSError:
            # Filesystem durability support varies (notably on some fixture
            # filesystems); the already-atomic replacement still stands.
            pass
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _fsync_parent(path: Path) -> None:
    """Best-effort directory fsync after a namespace mutation."""
    try:
        directory_fd = os.open(str(path.parent), os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except OSError:
        # Some fixture filesystems do not support directory fsync.  The
        # namespace operation itself remains atomic on supported local files.
        pass


def _stage_content(path: Path, content: str, *, staged_path: Path | None = None,
                   dir_fd: int | None = None) -> Path:
    """Write and fsync a replacement beside *path*, returning its temp name.

    ``staged_path`` is used by the journalled CAS path.  Its name is recorded
    before the file is created, so recovery never has to guess whether a
    ``.dc3-tmp-*`` artifact may contain displaced editor bytes.  With
    ``dir_fd`` the temp is created relative to the pinned parent descriptor.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    if staged_path is None:
        fd, raw_tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".dc3-tmp-")
        tmp = Path(raw_tmp)
    else:
        tmp = staged_path
        if dir_fd is None:
            fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        else:
            fd = os.open(tmp.name, os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                         0o600, dir_fd=dir_fd)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(content)
            fh.flush()
            os.fsync(fh.fileno())
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise
    return tmp


def _file_identity(path: Path) -> tuple[int, int] | None:
    """Identity of the path entry itself; a symlink is never followed, so it
    can only present its own (dev, inode) — which no staged inode matches."""
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return None
    return st.st_dev, st.st_ino


def _identity_value(identity: tuple[int, int] | None) -> list[int] | None:
    if identity is None:
        return None
    return [identity[0], identity[1]]


def _identity_from_value(value: object) -> tuple[int, int] | None:
    if (not isinstance(value, list) or len(value) != 2
            or any(not isinstance(part, int) or isinstance(part, bool)
                   for part in value)):
        return None
    return value[0], value[1]


def _bytes_fingerprint(value: bytes | None) -> str | None:
    if value is None:
        return None
    return "sha256:" + sha256_hex(value)


def _atomic_exchange(left: Path | str, right: Path | str, *,
                     dir_fd: int | None = None) -> None:
    """Atomically swap two same-directory files or fail closed.

    POSIX ``rename`` overwrites its destination and has no conditional variant.
    Darwin's ``renameatx_np(RENAME_SWAP)`` and Linux's ``renameat2`` exchange
    provide the required linearization point: after a swap, the previous
    target can be inspected at the staged filename without ever having made a
    decision from a stale pre-replace read.  Platforms without either primitive
    must refuse an existing-file promotion rather than silently falling back to
    ``os.replace``.

    With ``dir_fd`` both names are resolved relative to the pinned parent
    directory descriptor, so a parent swapped for a symlink between check and
    syscall cannot redirect the exchange outside the destination home.
    """
    libc = ctypes.CDLL(None, use_errno=True)
    left_b, right_b = os.fsencode(left), os.fsencode(right)
    try:
        if sys.platform == "darwin":
            rename_swap = libc.renameatx_np
            rename_swap.argtypes = (ctypes.c_int, ctypes.c_char_p,
                                    ctypes.c_int, ctypes.c_char_p,
                                    ctypes.c_uint)
            rename_swap.restype = ctypes.c_int
            at_fd = dir_fd if dir_fd is not None else -2  # Darwin AT_FDCWD
            result = rename_swap(at_fd, left_b, at_fd, right_b,
                                 0x00000002)  # RENAME_SWAP
        elif sys.platform.startswith("linux"):
            rename_exchange = libc.renameat2
            rename_exchange.argtypes = (ctypes.c_int, ctypes.c_char_p,
                                        ctypes.c_int, ctypes.c_char_p,
                                        ctypes.c_uint)
            rename_exchange.restype = ctypes.c_int
            at_fd = dir_fd if dir_fd is not None else -100  # Linux AT_FDCWD
            result = rename_exchange(at_fd, left_b, at_fd, right_b,
                                     0x00000002)  # RENAME_EXCHANGE
        else:
            raise AttributeError("no native atomic exchange primitive")
    except AttributeError as exc:
        raise DestinationError(
            "this local filesystem platform has no atomic exchange primitive; "
            "refusing non-CAS replacement") from exc
    if result != 0:
        err = ctypes.get_errno()
        raise OSError(err, os.strerror(err), str(right))


def _open_confined_parent_fd(home: Path, target: Path) -> int:
    """Descriptor-pin *target*'s parent directory below the explicit home.

    Every component is opened with ``O_NOFOLLOW`` relative to the previous
    descriptor (``openat`` semantics), so a parent directory swapped for a
    symlink after the lexical confinement check can no longer redirect the
    native stage/link/exchange operations outside the home.
    """
    try:
        rel = target.relative_to(home)
    except ValueError as exc:
        raise DestinationError(
            f"target {target} escapes destination home {home}") from exc
    fd = os.open(str(home), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in rel.parts[:-1]:
            next_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                              dir_fd=fd)
            os.close(fd)
            fd = next_fd
        return fd
    except OSError as exc:
        os.close(fd)
        raise DestinationError(
            f"cannot descriptor-pin parent of {target} below {home}: "
            f"{exc}") from exc


def _atomic_compare_and_replace(path: Path, content: str,
                                expected: bytes | None, *,
                                backup: BackupRef | None = None,
                                purpose: str = "commit") -> None:
    """Commit *content* only if *path* still holds exactly ``expected``.

    For a new target, ``link`` is an atomic create-if-absent operation.  For an
    existing target, a native atomic exchange moves the old target to the
    staging name; inspecting those moved bytes detects a non-cooperating edit
    at the commit boundary.  On a mismatch the exchange is reversed only while
    our staged inode is still at the destination, preserving the concurrent
    bytes rather than overwriting them with a stale snapshot.
    """
    staged_bytes = content.encode("utf-8")
    # Journaled writes carry the explicit home identity: pin the target's
    # parent directory descriptor once and perform every native stage, link,
    # exchange, read, and unlink relative to it, so a parent swapped for a
    # symlink between check and syscall cannot redirect the operation.
    parent_fd: int | None = None
    if backup is not None and backup.home_identity:
        path.parent.mkdir(parents=True, exist_ok=True)
        parent_fd = _open_confined_parent_fd(
            Path(str(backup.home_identity["canonical_path"])), path)

    def _identity_at(p: Path) -> tuple[int, int] | None:
        if parent_fd is None:
            return _file_identity(p)
        try:
            st = os.stat(p.name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            return None
        return st.st_dev, st.st_ino

    def _read_at(p: Path) -> bytes | None:
        if parent_fd is None:
            return p.read_bytes() if p.is_file() else None
        try:
            fd = os.open(p.name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent_fd)
        except FileNotFoundError:
            return None
        with os.fdopen(fd, "rb") as fh:
            return fh.read()

    def _exchange_at(left: Path, right: Path) -> None:
        if parent_fd is None:
            _atomic_exchange(left, right)
        else:
            _atomic_exchange(left.name, right.name, dir_fd=parent_fd)

    def _fsync_at() -> None:
        if parent_fd is None:
            _fsync_parent(path)
            return
        try:
            os.fsync(parent_fd)
        except OSError:
            pass

    try:
        if backup is None:
            tmp = _stage_content(path, content)
        else:
            tmp = backup.stage_path(path)
            backup.record_stage_intent(
                path, tmp, expected=expected, desired=staged_bytes,
                mode="create" if expected is None else "exchange",
                purpose=purpose)
            tmp = _stage_content(path, content, staged_path=tmp,
                                 dir_fd=parent_fd)
        staged_identity = _identity_at(tmp)
        if staged_identity is None:
            raise DestinationError(f"{path}: staged replacement disappeared")
        preserve_temp = False
        can_clean_temp = False
        exchange_started = False
        try:
            if expected is None:
                if backup is not None:
                    backup.arm_atomic_operation(path, staged_identity)
                try:
                    # link(2) is an atomic compare-and-create: another editor
                    # that creates the target first wins and is never
                    # overwritten.
                    if parent_fd is None:
                        os.link(tmp, path)
                    else:
                        os.link(tmp.name, path.name, src_dir_fd=parent_fd,
                                dst_dir_fd=parent_fd, follow_symlinks=False)
                except FileExistsError as exc:
                    can_clean_temp = True
                    raise ConcurrentRevisionError(
                        f"{path}: target appeared during atomic create; "
                        "refusing to overwrite the concurrent edit") from exc
                if backup is not None:
                    backup.mark_target_committed(path)
                can_clean_temp = True
                _fsync_at()
                return

            try:
                if backup is not None:
                    # This marker is durable before the native linearization
                    # point.  A crash immediately after the syscall can
                    # therefore inspect this named path instead of replaying a
                    # stale backup over it.
                    backup.arm_atomic_operation(path, staged_identity)
                _exchange_at(tmp, path)
                exchange_started = True
            except FileNotFoundError as exc:
                can_clean_temp = True
                raise ConcurrentRevisionError(
                    f"{path}: target disappeared before atomic replacement; "
                    "refusing to overwrite") from exc

            if backup is not None:
                try:
                    backup.mark_exchange_swapped(path)
                except BaseException:
                    # tmp may now contain original or concurrent bytes.  Leave
                    # it for the durable exchange_pending marker to reconcile.
                    preserve_temp = True
                    raise

            # The former destination is now at tmp.  This check is after the
            # atomic exchange, so it observes an edit injected immediately
            # before the replacement primitive instead of racing it with
            # os.replace.
            try:
                observed = _read_at(tmp)
            except BaseException:
                preserve_temp = True
                raise
            if observed == expected:
                if backup is not None:
                    backup.mark_target_committed(path)
                can_clean_temp = True
                _fsync_at()
                return

            # A mismatch predates this transaction's commit.  Reverse only if
            # the destination is still our exact staged inode; if another
            # writer has already changed it after the exchange, leave that
            # newer edit in place and report the conflict without writing
            # anything further.
            current_identity = _identity_at(path)
            current = _read_at(path)
            if current_identity == staged_identity and current == staged_bytes:
                _exchange_at(tmp, path)
                if backup is not None:
                    backup.mark_exchange_reversed(path)
                restored = _read_at(path)
                if restored != observed:
                    raise DestinationError(
                        f"{path}: atomic conflict rollback did not preserve "
                        "the concurrent bytes")
                # tmp now contains only our staged bytes.
                can_clean_temp = True
                _fsync_at()
            else:
                # A later editor changed the destination.  tmp still owns the
                # displaced bytes, so treating it as disposable would lose
                # data.
                preserve_temp = True
            raise ConcurrentRevisionError(
                f"{path}: target changed before atomic replacement; "
                "preserving the concurrent edit")
        finally:
            # Only remove a post-exchange staging file after proving it
            # contains snapshot bytes or our staged bytes.  Otherwise recovery
            # owns it.
            if (can_clean_temp or not exchange_started) and not preserve_temp:
                try:
                    if parent_fd is None:
                        tmp.unlink()
                    else:
                        os.unlink(tmp.name, dir_fd=parent_fd)
                except FileNotFoundError:
                    pass
                if backup is not None:
                    try:
                        backup.mark_temp_cleaned(path)
                    except OSError:
                        # A journal that still names a missing safe temp
                        # remains recoverable from target identity plus
                        # immutable backup.
                        pass
    finally:
        if parent_fd is not None:
            os.close(parent_fd)


def _file_fingerprint(path: Path) -> str | None:
    """Fingerprint a regular file, refusing to read through a symlink.

    Every caller (revision snapshots, journal reconciliation, restore
    verification) fingerprints paths a hostile editor could have swapped for
    a symlink; following one here would leak or trust outside-home bytes.
    """
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return None
    if stat.S_ISLNK(st.st_mode):
        raise DestinationError(
            f"{path} is a symlink; refusing to fingerprint bytes through it")
    if not stat.S_ISREG(st.st_mode):
        return None
    fd = os.open(str(path), os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, "rb") as fh:
        return "sha256:" + sha256_hex(fh.read())


def _canonical_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False)


def _safe_relative_path(value: str) -> bool:
    return bool(_SAFE_RELATIVE_PATH_RE.fullmatch(value))


def _assert_confined_target(home: Path, target: Path, *,
                            what: str = "target") -> None:
    """Refuse a symlinked component at or below the explicit home.

    Every read, backup, stage, and exchange boundary calls this before
    touching bytes, so a planted symlink can never route DC3 traffic outside
    the declared destination home.  A missing path suffix is legitimate
    (creation targets); the home itself was canonicalized at construction.
    """
    try:
        rel = target.relative_to(home)
    except ValueError as exc:
        raise DestinationError(
            f"{what} {target} escapes explicit destination home {home}") from exc
    current = home
    for part in rel.parts:
        current = current / part
        if current.is_symlink():
            raise DestinationError(
                f"{what} {current} is a symlink; refusing to read or mutate "
                "bytes through it")
        if not current.exists():
            return


def _confined_read_bytes(home: Path, target: Path) -> bytes:
    """Read a confined regular file without following a final symlink."""
    _assert_confined_target(home, target, what="read target")
    fd = os.open(str(target), os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, "rb") as fh:
        return fh.read()


def _confined_read_bytes_or_none(home: Path, target: Path) -> bytes | None:
    """Confined read that maps a legitimately absent target to None.

    Anything present that is not a plain regular file — a symlink anywhere in
    the confined path, a directory, a fifo — is refused rather than read or
    treated as absent, so no destination decision is ever made from bytes
    that live outside the explicit home.
    """
    _assert_confined_target(home, target, what="read target")
    try:
        st = os.lstat(target)
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(st.st_mode):
        raise DestinationError(
            f"read target {target} is not a regular file; refusing to read "
            "bytes through it")
    return _confined_read_bytes(home, target)


def allocate_backup_dir(backup_root: Path | str, *components: str) -> Path:
    """Collision-refusing per-attempt backup directory below *backup_root*.

    Every promotion attempt gets a directory no earlier attempt or receipt
    has ever used, so a later revision or failed retry can never overwrite an
    earlier receipt's rollback evidence.  ``mkdir`` without ``exist_ok`` is
    the atomic claim; a raced name simply advances to the next attempt.

    Confinement here is primary, not a later validation: every namespace
    component (run, record key, revision, attempt) is created and opened
    relative to the previous pinned directory descriptor with ``O_NOFOLLOW``
    (``mkdirat``/``openat`` semantics), so a pre-existing symlink at any
    depth is refused — never followed — before a single byte of rollback
    evidence can be read, copied, or written outside the explicit root.  A
    symlink squatting on an attempt name only surrenders that name to the
    next fresh claim.  The returned path's identity is verified against the
    pinned descriptor so an allocation raced by a parent swap fails closed.
    """
    root = Path(backup_root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    for part in components:
        # The safe-identity grammar has no path separators or dot-only
        # segments, so a component can never rename the namespace upward.
        if not is_safe_identity(part):
            raise DestinationError(
                f"backup namespace component {part!r} is not a safe "
                "identity; refusing to allocate rollback evidence under it")
    try:
        fd = os.open(str(root), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError as exc:
        raise DestinationError(
            f"cannot descriptor-pin backup root {root}: {exc}") from exc
    opened = [fd]
    try:
        for part in components:
            try:
                os.mkdir(part, 0o700, dir_fd=fd)
            except FileExistsError:
                pass
            try:
                fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                             dir_fd=fd)
            except OSError as exc:
                raise DestinationError(
                    f"backup namespace component {part!r} under {root} is a "
                    "symlink or not a real directory; refusing to place "
                    f"rollback evidence through it: {exc}") from exc
            opened.append(fd)
        for attempt in range(1, 100000):
            name = f"attempt-{attempt:05d}"
            try:
                os.mkdir(name, 0o700, dir_fd=fd)
            except FileExistsError:
                continue
            try:
                attempt_fd = os.open(
                    name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                    dir_fd=fd)
            except OSError as exc:
                raise DestinationError(
                    f"freshly claimed backup attempt {name!r} under {root} "
                    f"is not a real directory: {exc}") from exc
            opened.append(attempt_fd)
            path = root.joinpath(*components, name)
            fd_stat = os.fstat(attempt_fd)
            try:
                path_stat = os.lstat(path)
            except OSError as exc:
                raise DestinationError(
                    f"backup namespace changed during allocation of {path}: "
                    f"{exc}") from exc
            if (fd_stat.st_dev, fd_stat.st_ino) != (path_stat.st_dev,
                                                    path_stat.st_ino):
                raise DestinationError(
                    f"backup namespace changed during allocation of {path}; "
                    "refusing to hand out an attempt directory that no "
                    "longer matches its pinned descriptor")
            return path
        raise DestinationError(
            f"could not allocate a fresh backup attempt directory under {root}")
    finally:
        for open_fd in opened:
            os.close(open_fd)


def _assert_safe_record_payload(record: PromotionRecord) -> None:
    """Keep untrusted candidate strings from becoming Markdown structure.

    Subject and claim are inserted into frontmatter, headings, and DC3-owned
    regions by different adapters.  A one-line, non-delimiter subset is a
    deliberately conservative common format: it makes forged frontmatter,
    headings, and nested DC3 marker regions impossible rather than trying to
    escape each renderer differently.
    """
    for field, value in (("subject", record.subject), ("claim", record.claim)):
        if any(token in value for token in ("\r", "\n", "<!--", "-->", "\x00")):
            raise DiffBoundError(
                f"record {record.record_key} {field} contains a reserved "
                "DC3 or structural delimiter")
        if value.lstrip().startswith(("#", "---")):
            raise DiffBoundError(
                f"record {record.record_key} {field} starts a Markdown "
                "heading or frontmatter delimiter")


_RECORD_KEY_RE = re.compile(r"[0-9a-f]{32}\Z")
_MAX_RENDER_IDENTITY_LEN = 128
# The only frontmatter memory types the destination format defines; anything
# else in this raw-interpolated YAML slot is refused, not escaped.
_ALLOWED_MEMORY_TYPES = ("user", "feedback", "project", "reference")


def _assert_safe_record_identity(record: PromotionRecord) -> None:
    """Keep untrusted identity strings from reopening the marker grammar.

    ``candidate_id`` and the provenance ``run_id`` are contract fields carried
    from the candidate; ``record_key`` is derived.  All three are interpolated
    verbatim into the DC3 begin/record/end markers.  Unlike subject/claim they
    are not free text, so the strict identity charset (no whitespace, newlines,
    or ``<!--`` / ``-->`` tokens) is the right, deterministic gate: a value
    that fails here could otherwise forge or nest a sibling record region.
    This is defense in depth — the candidate contract rejects the same shapes
    at ingest — for any caller that builds a PromotionRecord directly.

    The reader recognizes region ownership only for exactly-32-lowercase-hex
    keys, so the guard requires that exact shape; a merely "safe" key would
    render a region later treated as unowned.  ``content_revision`` is
    interpolated into the marker grammar and ``memory_type`` into raw YAML
    frontmatter, so both are enforced here as well.
    """
    if (not isinstance(record.content_revision, int)
            or isinstance(record.content_revision, bool)
            or record.content_revision < 1):
        raise DiffBoundError(
            f"record {record.record_key!r} content_revision="
            f"{record.content_revision!r} is not a positive integer identity; "
            "refusing to render marker metadata")
    if not (isinstance(record.record_key, str)
            and _RECORD_KEY_RE.fullmatch(record.record_key)):
        raise DiffBoundError(
            f"record_key {record.record_key!r} is not an exactly-32-lowercase-"
            "hex DC3 record identity; refusing to render marker metadata")
    for field, value in (("candidate_id", record.candidate_id),
                         ("run_id", record.run_id)):
        if not is_safe_identity(value) or len(value) > _MAX_RENDER_IDENTITY_LEN:
            raise DiffBoundError(
                f"record {record.record_key!r} {field}={value!r} is not a safe "
                "DC3 identity; refusing to render marker metadata")
    if record.memory_type not in _ALLOWED_MEMORY_TYPES:
        raise DiffBoundError(
            f"record {record.record_key!r} memory_type={record.memory_type!r} "
            f"is not one of {_ALLOWED_MEMORY_TYPES}; refusing to render "
            "frontmatter metadata")


def _combined_revision(paths: Iterable[Path]) -> str | None:
    """Deterministic revision over the adapter's target files.

    None when no target file exists yet (a fresh destination), which maps to
    the receipt's nullable target_revision_before.

    Private/trusted-only: the final component is fingerprinted ``O_NOFOLLOW``
    but this helper performs no home-confinement walk over parent components.
    The only sanctioned caller is ``snapshot_revision``, which asserts full
    confinement below the explicit home for every path first; direct external
    use is not part of the adapter contract.
    """
    parts = []
    for path in sorted(paths, key=str):
        fp = _file_fingerprint(path)
        parts.append(f"{path}\x1f{fp or 'absent'}")
    if all(p.endswith("absent") for p in parts):
        return None
    return "sha256:" + sha256_hex("\x1e".join(parts))


def render_record_region(record: PromotionRecord, body: str) -> str:
    _assert_safe_record_payload(record)
    _assert_safe_record_identity(record)
    region = (
        _BEGIN.format(key=record.record_key, rev=record.content_revision)
        + "\n" + body.rstrip("\n") + "\n"
        + f"<!-- dc3:record {record.record_key} rev={record.content_revision} "
          f"candidate={record.candidate_id} run={record.run_id} -->\n"
        + _END.format(key=record.record_key) + "\n")
    if len(region) > MAX_RECORD_REGION_CHARS:
        raise DiffBoundError(
            f"record {record.record_key} region is {len(region)} chars, "
            f"over the {MAX_RECORD_REGION_CHARS} bound")
    return region


def strip_record_region(text: str, record_key: str) -> str:
    return re.sub(_REGION_RE_TMPL.format(key=re.escape(record_key)), "",
                  text, flags=re.DOTALL)


def split_region_body(region_text: str) -> tuple[str | None, str]:
    """Separate a region's rendered ``## subject`` heading from its claim.

    Duplicate policy compares normalized claim text against normalized claim
    text; a heading that carried the subject would otherwise dilute the word
    set and let an identical claim under a different subject evade the exact
    and near-duplicate gates.
    """
    subject: str | None = None
    lines: list[str] = []
    for line in region_text.splitlines():
        if line.startswith("<!-- dc3:"):
            continue
        if subject is None and line.startswith("## "):
            subject = line[3:].strip()
            continue
        lines.append(line)
    return subject, "\n".join(lines).strip()


def upsert_record_region(old_text: str, record_key: str, region: str) -> str:
    """Replace the record's existing region or append it; nothing else moves."""
    pattern = re.compile(_REGION_RE_TMPL.format(key=re.escape(record_key)),
                         re.DOTALL)
    if pattern.search(old_text):
        return pattern.sub(lambda _: region, old_text, count=1)
    if old_text and not old_text.endswith("\n"):
        old_text += "\n"
    sep = "\n" if old_text.strip() else ""
    return old_text + sep + region


def assert_bounded_diff(old_text: str, new_text: str, record_key: str, *,
                        scaffold: str = "") -> None:
    """Proof that only the record's own region changed.

    Residues (text minus the record's region) are compared byte-for-byte,
    modulo leading/trailing whitespace introduced by region separators. Two
    legal cases: identical residues (replace/append), or a fresh file whose
    residue is exactly the adapter's declared scaffold (file creation).
    """
    old_res = strip_record_region(old_text, record_key).strip()
    new_res = strip_record_region(new_text, record_key).strip()
    if old_res == new_res:
        return
    if not old_text and new_res == scaffold.strip():
        return
    raise DiffBoundError(
        f"write for record {record_key} would modify content outside "
        "its own region; refusing")


class DestinationAdapter:
    """Shared mechanics; subclasses define targets, rendering, and readers."""

    adapter_name: str = "abstract"

    def __init__(self, home: Path | str, destination: str):
        # Resolve once at construction so every target and persisted identity
        # is rooted in the caller's explicit canonical home, never a later
        # process working directory or a sibling shadow home.
        self.home = Path(home).resolve()
        self.destination = destination
        # The lock identity must cover every destination that shares this
        # adapter's policy state (duplicate scan, conflict derivation, budget
        # inputs), not merely the exact destination string.  Skill and
        # project-doc adapters own disjoint per-id files, so their scope is
        # themselves; MemoryDestination overrides both because hot and warm
        # share one home-wide fact set.
        self.lock_scope = destination

    def policy_scope_destinations(self) -> tuple[str, ...]:
        """Destinations whose promoted state shares this adapter's policy
        surface (duplicates, derived conflicts, crash recovery)."""
        return (self.destination,)

    # -- subclass surface --------------------------------------------------
    def target_paths(self, record: PromotionRecord) -> list[Path]:
        raise NotImplementedError

    def render(self, record: PromotionRecord) -> dict[Path, str]:
        """Full new content for every target file."""
        raise NotImplementedError

    def existing_records(self) -> list[ExistingRecord]:
        raise NotImplementedError

    def read_back(self, record: PromotionRecord) -> str:
        raise NotImplementedError

    def retrieval_proof(self, record: PromotionRecord) -> str:
        raise NotImplementedError

    # -- shared mechanics ----------------------------------------------------
    def snapshot_revision(self, record: PromotionRecord) -> str | None:
        """Revision over confined targets only.

        Confinement is asserted for every target before any fingerprint read,
        and the fingerprint itself opens with O_NOFOLLOW: a symlinked target,
        index, or parent component is refused instead of hashing (and thereby
        trusting or later backing up) bytes outside the explicit home.
        """
        paths = self.target_paths(record)
        for path in paths:
            _assert_confined_target(self.home, path, what="snapshot target")
        return _combined_revision(paths)

    def _confined_text(self, path: Path, default: str = "") -> str:
        """Read a home-confined text file; refuse any symlinked component."""
        _assert_confined_target(self.home, path, what="read target")
        if not path.is_file():
            return default
        return _confined_read_bytes(self.home, path).decode("utf-8")

    def budget_context(self, record: PromotionRecord) -> str:
        """Destination text that shares this record's promotion budget but is
        not itself a write target (e.g. the hot tier's USER.md)."""
        return ""

    @contextmanager
    def destination_lock(self):
        """Serialize cooperating writers for this adapter's policy scope.

        The lock identity is ``lock_scope`` — one lock file per shared policy
        surface, so two destinations that make decisions over the same
        on-disk state (memory:hot and memory:warm scan one home-wide fact
        set) serialize against each other, never only against themselves.
        It is deliberately scope-local (not process-global) and is held by
        the orchestrator from policy evaluation through receipt persistence.
        Final byte checks below still defend against non-cooperating editors.

        Every path segment is opened relative to a pinned directory
        descriptor with ``O_NOFOLLOW`` (``openat`` semantics on Darwin), so a
        symlinked ``.dc3-locks`` — or a symlinked lock file, or a parent
        swapped after the check — can never create lock metadata outside the
        declared home.
        """
        self.home.mkdir(parents=True, exist_ok=True)
        lock_name = sha256_hex(self.lock_scope)[:32] + ".lock"
        try:
            home_fd = os.open(str(self.home),
                              os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        except OSError as exc:
            raise DestinationError(
                f"cannot pin destination home {self.home} for locking: {exc}"
            ) from exc
        locks_fd = lock_fd = None
        try:
            try:
                os.mkdir(".dc3-locks", 0o700, dir_fd=home_fd)
            except FileExistsError:
                pass
            try:
                locks_fd = os.open(".dc3-locks",
                                   os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                                   dir_fd=home_fd)
            except OSError as exc:
                raise DestinationError(
                    f"{self.home / '.dc3-locks'} is not a real directory "
                    f"inside the destination home (symlink or non-directory); "
                    f"refusing to lock: {exc}") from exc
            try:
                # Darwin's openat(O_CREAT|O_NOFOLLOW) can spuriously return
                # ENOENT when two callers race to create the same name; the
                # directory descriptor is pinned, so a bounded retry is safe.
                for attempt in range(8):
                    try:
                        lock_fd = os.open(
                            lock_name,
                            os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW,
                            0o600, dir_fd=locks_fd)
                        break
                    except FileNotFoundError:
                        if attempt == 7:
                            raise
            except OSError as exc:
                raise DestinationError(
                    f"destination lock file {lock_name!r} is not a real file "
                    f"inside {self.home / '.dc3-locks'}; refusing to lock: "
                    f"{exc}") from exc
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
        finally:
            for fd in (lock_fd, locks_fd, home_fd):
                if fd is not None:
                    os.close(fd)

    def home_identity(self) -> dict[str, str | int]:
        return _canonical_home_identity(self.home)

    def backup(self, record: PromotionRecord, backup_dir: Path | str, *,
               backup_root: Path | str | None = None) -> BackupRef:
        backup_dir = Path(backup_dir)
        if backup_root is not None:
            # The orchestrated path allocated backup_dir with descriptor-
            # pinned mkdir/openat below this explicit root.  Re-assert the
            # symlink-free walk immediately before the first evidence byte
            # is written, so a namespace component swapped after allocation
            # (parent-swap race) is refused rather than followed.
            backup_root = Path(backup_root).resolve()
            _assert_confined_target(backup_root, backup_dir,
                                    what="backup namespace")
            try:
                dir_stat = os.lstat(backup_dir)
            except OSError as exc:
                raise DestinationError(
                    f"allocated backup directory {backup_dir} disappeared "
                    f"before use: {exc}") from exc
            if not stat.S_ISDIR(dir_stat.st_mode):
                raise DestinationError(
                    f"allocated backup directory {backup_dir} is not a real "
                    "directory; refusing to write rollback evidence")
        else:
            backup_dir.mkdir(parents=True, exist_ok=True)
        # Rollback evidence is immutable per attempt: refuse any directory
        # that already holds a manifest or journal instead of overwriting an
        # earlier receipt's recovery data (see allocate_backup_dir).
        for name in (BACKUP_MANIFEST_NAME, WRITE_JOURNAL_NAME):
            if (backup_dir / name).exists():
                raise DestinationError(
                    f"backup directory {backup_dir} already holds rollback "
                    "evidence; every promotion attempt requires a fresh, "
                    "collision-refusing namespace")
        # Fresh skill/project homes are valid destinations.  Materialize the
        # explicit home before binding its stable directory identity.
        self.home.mkdir(parents=True, exist_ok=True)
        home_identity = self.home_identity()
        entries = []
        for i, target in enumerate(sorted(self.target_paths(record), key=str)):
            try:
                target_rel = target.relative_to(self.home).as_posix()
            except ValueError as exc:
                raise DestinationError(
                    f"target {target} escapes explicit destination home {self.home}") from exc
            if not _safe_relative_path(target_rel):
                raise DestinationError(f"unsafe destination-relative target {target_rel!r}")
            # Confinement before the first byte is read: a symlinked target
            # (or symlinked parent) must fail here, not leak external bytes
            # into a backup that rollback would later replay.
            _assert_confined_target(self.home, target, what="backup source")
            if target.is_file():
                dest = backup_dir / f"{i:04d}-{target.name}"
                data = _confined_read_bytes(self.home, target)
                if backup_root is not None:
                    _assert_confined_target(backup_root, dest,
                                            what="backup artifact")
                dest.write_bytes(data)
                entries.append(BackupEntry(
                    target=target, target_rel=target_rel, existed=True, backup_path=dest,
                    fingerprint="sha256:" + sha256_hex(data)))
            else:
                entries.append(BackupEntry(target=target, target_rel=target_rel,
                                           existed=False,
                                           backup_path=None, fingerprint=None))
        backup = BackupRef(backup_dir=backup_dir, entries=tuple(entries),
                           home_identity=home_identity,
                           backup_root=backup_root)
        manifest = {
            "version": _BACKUP_FORMAT_VERSION,
            "kind": "dc3_filesystem_backup",
            "destination": self.destination,
            "home_identity": home_identity,
            "entries": backup.entry_metadata(),
        }
        _atomic_write(backup.manifest_path, _canonical_json(manifest) + "\n")
        journal = {
            "version": _WRITE_JOURNAL_VERSION,
            "destination": self.destination,
            "home_identity": home_identity,
            "candidate_id": record.candidate_id,
            "content_revision": record.content_revision,
            "state": "prepared",
            # Every target operation writes a durable entry before staging.
            # Recovery uses this rather than guessing about .dc3-tmp-* files.
            "target_progress": {},
        }
        _atomic_write(backup.journal_path, _canonical_json(journal) + "\n")
        return backup

    def recover_pending_writes(self, backup_root: Path | str,
                               receipt_exists) -> None:
        """Recover abandoned shadow writes before accepting another one.

        A journal without a persisted receipt is restored to its backup.  If a
        process died after the SQLite commit but before the journal marker, the
        receipt is the authority and the journal is completed instead.

        Recovery covers the adapter's full policy scope, not only its exact
        destination: a crashed memory:warm write must be reconciled before a
        memory:hot promotion scans the shared home-wide fact set, or its
        uncommitted bytes would feed the hot duplicate/conflict decisions.
        """
        root = Path(backup_root).resolve()
        if not root.is_dir():
            return
        scope = self.policy_scope_destinations()
        expected_home_identity = self.home_identity()
        for journal_path in sorted(root.rglob(WRITE_JOURNAL_NAME)):
            try:
                journal = json.loads(journal_path.read_text(encoding="utf-8"))
                if journal.get("destination") not in scope:
                    continue
                # A shared backup root can contain journals for several
                # explicit shadow homes.  Never re-root a relative target from
                # one home into another: unknown/legacy identities are ignored
                # rather than restored, and a matching journal must carry the
                # current canonical directory identity exactly.
                if journal.get("home_identity") != expected_home_identity:
                    continue
                state = journal.get("state")
                if state in ("committed", "rolled_back", "conflict_aborted"):
                    continue
                if journal.get("version") != _WRITE_JOURNAL_VERSION:
                    # Earlier journals do not name the post-exchange temp or
                    # staged inode.  Replaying their snapshot would be a data
                    # loss guess, so leave them untouched for manual recovery.
                    raise DestinationError(
                        f"cannot safely recover legacy journal {journal_path}")
                backup = self._backup_from_manifest(
                    journal_path.parent,
                    expected_destination=journal["destination"],
                    backup_root=root)
                if receipt_exists(journal.get("candidate_id"),
                                  journal.get("content_revision")):
                    backup.mark_state("committed")
                else:
                    self._reconcile_uncommitted_backup(backup, journal)
            except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
                raise DestinationError(
                    f"cannot safely recover destination journal {journal_path}: {exc}") from exc

    def _backup_from_manifest(self, backup_dir: Path, *,
                              expected_destination: str | None = None,
                              backup_root: Path | None = None) -> BackupRef:
        manifest_path = backup_dir / BACKUP_MANIFEST_NAME
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
        expected_home_identity = self.home_identity()
        # Scope-recovery may reconcile a sibling tier's journal (shared home,
        # shared policy state); the manifest must still name that exact
        # destination and this exact home identity.
        expected_destination = expected_destination or self.destination
        if (expected_destination not in self.policy_scope_destinations()
                or data.get("version") != _BACKUP_FORMAT_VERSION
                or data.get("kind") != "dc3_filesystem_backup"
                or data.get("destination") != expected_destination
                or not _is_home_identity(data.get("home_identity"))
                or data.get("home_identity") != expected_home_identity
                or not isinstance(data.get("entries"), list)):
            raise DestinationError(f"invalid backup manifest {manifest_path}")
        entries = []
        for item in data["entries"]:
            if not isinstance(item, dict) or not _safe_relative_path(item.get("target", "")):
                raise DestinationError(f"invalid backup target in {manifest_path}")
            _assert_confined_target(self.home, self.home / item["target"],
                                    what="recovery target")
            target = (self.home / item["target"]).resolve()
            if not target.is_relative_to(self.home.resolve()):
                raise DestinationError(f"backup target escapes destination home: {target}")
            existed = item.get("existed") is True
            name = item.get("backup_file")
            if existed:
                if not isinstance(name, str) or Path(name).name != name:
                    raise DestinationError(f"invalid backup file in {manifest_path}")
                backup_path = backup_dir / name
            else:
                backup_path = None
            entries.append(BackupEntry(target=target, target_rel=item["target"],
                                       existed=existed, backup_path=backup_path,
                                       fingerprint=item.get("fingerprint")))
        return BackupRef(backup_dir=backup_dir, entries=tuple(entries),
                         home_identity=expected_home_identity,
                         backup_root=backup_root)

    def abort_pending_write(self, backup: BackupRef, *,
                            conflict: bool = False) -> None:
        """Reconcile one in-process abort with the same rules as recovery.

        A final CAS conflict is not an ordinary failure: some earlier target
        can already be committed, while the conflicted target belongs to an
        editor.  This routine restores only target inodes that still prove
        they are ours and leaves every other byte authoritative.
        """
        self._reconcile_uncommitted_backup(
            backup, backup.journal_data(), force_conflict=conflict)

    def _journal_temp_path(self, entry: BackupEntry, progress: dict) -> Path | None:
        raw = progress.get("temp_rel")
        if not isinstance(raw, str) or not _safe_relative_path(raw):
            return None
        temp = (self.home / raw).resolve()
        if (not temp.is_relative_to(self.home.resolve())
                or temp.parent != entry.target.parent.resolve()
                or not temp.name.startswith(".dc3-tmp-")):
            raise DestinationError(
                f"unsafe staged artifact for {entry.target_rel}: {raw!r}")
        return temp

    @staticmethod
    def _target_is_staged(entry: BackupEntry, progress: dict) -> bool:
        identity = _identity_from_value(progress.get("staged_identity"))
        desired = progress.get("desired_fingerprint")
        return (identity is not None
                and _file_identity(entry.target) == identity
                and _file_fingerprint(entry.target) == desired)

    def _quarantine_temp(self, backup: BackupRef, entry: BackupEntry,
                         progress: dict, temp: Path) -> None:
        """Move unclassifiable displaced bytes out of the destination home.

        We copy before unlinking and verify the copy.  A recovery cannot
        silently discard unknown bytes just to make a stale temp disappear.
        """
        try:
            temp_stat = os.lstat(temp)
        except FileNotFoundError:
            return
        if not stat.S_ISREG(temp_stat.st_mode):
            raise DestinationError(
                f"staged artifact for {entry.target_rel} is not a regular "
                "file; refusing to quarantine bytes through it")
        raw = _confined_read_bytes(self.home, temp)
        artifacts = backup.backup_dir / "dc3-recovery-artifacts"
        backup._assert_confined_artifact(artifacts)
        artifacts.mkdir(parents=True, exist_ok=True)
        name = (entry.target_rel.replace("/", "--") + "-"
                + sha256_hex(raw)[:16] + "-" + temp.name.removeprefix(".dc3-tmp-"))
        artifact = artifacts / name
        if artifact.exists():
            if artifact.read_bytes() != raw:
                raise DestinationError(
                    f"recovery artifact collision for {entry.target_rel}")
        else:
            shutil.copyfile(temp, artifact)
            if artifact.read_bytes() != raw:
                raise DestinationError(
                    f"recovery artifact copy failed for {entry.target_rel}")
        temp.unlink()
        backup.mark_target_quarantined(entry.target, artifact)

    def _cleanup_our_temp(self, backup: BackupRef, entry: BackupEntry,
                          progress: dict, temp: Path) -> bool:
        if not temp.exists():
            return True
        if (_file_fingerprint(temp) != progress.get("desired_fingerprint")
                or _file_identity(temp) != _identity_from_value(
                    progress.get("staged_identity"))):
            return False
        temp.unlink()
        backup.mark_temp_cleaned(entry.target)
        return True

    def _restore_owned_target(self, backup: BackupRef, entry: BackupEntry,
                              progress: dict) -> str:
        """Restore one target only while its staged inode is still current."""
        if not self._target_is_staged(entry, progress):
            return "conflict"
        if not entry.existed:
            # A create-if-absent target has no displaced bytes.  The identity
            # plus fingerprint prove it is ours at this instant; it is removed
            # only on that proof, never as a blanket stale-backup deletion.
            entry.target.unlink()
            return "restored"
        original = entry.backup_path.read_bytes()
        try:
            original_text = original.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise DestinationError(
                f"backup for text destination {entry.target} is not UTF-8") from exc
        current = _confined_read_bytes(self.home, entry.target)
        try:
            _atomic_compare_and_replace(
                entry.target, original_text, current, backup=backup,
                purpose="rollback")
        except ConcurrentRevisionError:
            return "conflict"
        if _file_fingerprint(entry.target) != entry.fingerprint:
            raise DestinationError(
                f"recovery did not restore backup bytes for {entry.target}")
        return "restored"

    def _reconcile_target(self, backup: BackupRef, entry: BackupEntry,
                          progress: dict) -> str:
        """Return ``restored``, ``unchanged``, or ``conflict`` for one target."""
        if not isinstance(progress, dict):
            raise DestinationError(f"invalid journal progress for {entry.target_rel}")
        phase = progress.get("phase")
        mode = progress.get("mode")
        purpose = progress.get("purpose")
        if phase == "quarantined":
            return "conflict"
        if mode not in ("exchange", "create") or purpose not in ("commit", "rollback"):
            raise DestinationError(
                f"invalid journal operation for {entry.target_rel}")
        temp = self._journal_temp_path(entry, progress)

        # No native operation can start before the staged identity is durable.
        # Thus a stage intent can only leave our replacement bytes in temp.
        if phase == "stage_intent":
            if temp is not None and temp.exists():
                if _file_fingerprint(temp) != progress.get("desired_fingerprint"):
                    self._quarantine_temp(backup, entry, progress, temp)
                    return "conflict"
                temp.unlink()
                backup.mark_temp_cleaned(entry.target)
            return "unchanged"

        target_is_staged = self._target_is_staged(entry, progress)
        if purpose == "rollback" and target_is_staged:
            # A recovery-side CAS has already installed the original bytes.
            # Its temp can only hold our former committed bytes, so remove it
            # when its identity proves that fact; otherwise quarantine it.
            if temp is not None and temp.exists():
                if _file_fingerprint(temp) != progress.get("expected_fingerprint"):
                    self._quarantine_temp(backup, entry, progress, temp)
                    return "conflict"
                temp.unlink()
                backup.mark_temp_cleaned(entry.target)
            return "restored"

        if target_is_staged:
            if mode == "exchange" and temp is not None and temp.exists():
                # The temp is the other side of the durable exchange.  Swap it
                # back instead of copying a stale backup: that returns either
                # the exact snapshot or the concurrent bytes to the target.
                _atomic_exchange(temp, entry.target)
                if not self._cleanup_our_temp(backup, entry, progress, temp):
                    self._quarantine_temp(backup, entry, progress, temp)
                    return "conflict"
                return ("restored" if _file_fingerprint(entry.target) == entry.fingerprint
                        else "conflict")
            outcome = self._restore_owned_target(backup, entry, progress)
            if (outcome == "restored" and mode == "create"
                    and temp is not None and temp.exists()):
                if not self._cleanup_our_temp(backup, entry, progress, temp):
                    self._quarantine_temp(backup, entry, progress, temp)
                    return "conflict"
            return outcome

        # The target no longer proves it contains our staged inode.  It may be
        # an editor's later change; do not copy a backup over it.  A leftover
        # staged temp is safe to remove only when it is demonstrably ours or a
        # duplicate of the immutable snapshot.  Everything else is retained in
        # a durable quarantine artifact for review.
        if temp is not None and temp.exists():
            temp_fp = _file_fingerprint(temp)
            if (temp_fp == progress.get("desired_fingerprint")
                    and _file_identity(temp) == _identity_from_value(
                        progress.get("staged_identity"))):
                temp.unlink()
                backup.mark_temp_cleaned(entry.target)
            elif temp_fp == entry.fingerprint:
                temp.unlink()
                backup.mark_temp_cleaned(entry.target)
            else:
                self._quarantine_temp(backup, entry, progress, temp)
        current_fp = _file_fingerprint(entry.target)
        return "unchanged" if current_fp == entry.fingerprint else "conflict"

    def _reconcile_uncommitted_backup(self, backup: BackupRef, journal: dict,
                                      *, force_conflict: bool = False) -> None:
        progress = journal.get("target_progress", {})
        if not isinstance(progress, dict):
            raise DestinationError(f"invalid target progress in {backup.journal_path}")
        if not progress:
            # A v3 orchestrated write persists progress before its first
            # staging mutation.  With no progress, the target is unchanged;
            # never guess by replaying a backup over unprovable bytes.
            backup.mark_state("conflict_aborted" if force_conflict else "rolled_back")
            return
        outcomes = []
        for entry in backup.entries:
            item = progress.get(entry.target_rel)
            if item is None:
                continue
            outcomes.append(self._reconcile_target(backup, entry, item))
        backup.mark_state(
            "conflict_aborted" if force_conflict or "conflict" in outcomes
            else "rolled_back")

    def apply_write(self, record: PromotionRecord,
                    expected_revision: str | None, *,
                    backup: BackupRef) -> str:
        """Bounded, optimistic, atomic, journaled write. Returns the new revision.

        The revision re-check happens here, immediately before writing: an
        edit made after the caller snapshotted/backed up (design §11
        'optimistic target revision check') raises ConcurrentRevisionError
        and nothing is written.

        ``backup`` is required: the Phase 2 contract demands backup and
        journaled recovery evidence for every destination mutation, so there
        is deliberately no unjournaled write surface.  The journal must have
        been prepared for exactly this record and this home.
        """
        if not isinstance(backup, BackupRef):
            raise DestinationError(
                f"{self.destination}: apply_write requires the journaled "
                "BackupRef prepared for this record; unjournaled writes are "
                "not part of the Phase 2 contract")
        journal = backup.journal_data()
        if (journal.get("destination") != self.destination
                or journal.get("home_identity") != self.home_identity()
                or journal.get("candidate_id") != record.candidate_id
                or journal.get("content_revision") != record.content_revision):
            raise DestinationError(
                f"{self.destination}: supplied backup journal was not "
                "prepared for this record and destination home; refusing to "
                "write")
        targets = sorted(self.target_paths(record), key=str)
        for path in targets:
            # Confinement before any target byte is read or staged (render
            # below re-reads current target content).
            _assert_confined_target(self.home, path, what="write target")
        rendered = self.render(record)
        if set(rendered) != set(targets):
            raise DestinationError(
                f"{self.destination}: render did not return exactly its target set")
        before = {path: _confined_read_bytes_or_none(self.home, path)
                  for path in targets}
        for path in targets:
            old_content = (before[path] or b"").decode("utf-8")
            new_content = rendered[path]
            self._check_file_diff(record, path, old_content, new_content)
        self._before_final_revalidation(record, rendered)
        current = self.snapshot_revision(record)
        if current != expected_revision:
            raise ConcurrentRevisionError(
                f"{self.destination}: target revision changed after backup "
                f"(expected {expected_revision}, found {current}); refusing "
                "to overwrite the concurrent edit")
        write_started = False
        try:
            for path in targets:
                self._before_replace(record, path)
                actual = _confined_read_bytes_or_none(self.home, path)
                if actual != before[path]:
                    raise ConcurrentRevisionError(
                        f"{self.destination}: exact target {path.name} changed "
                        "during final write preparation; refusing to overwrite")
                self._before_atomic_commit(record, path)
                # The compare-and-swap helper owns the final commit boundary.
                # It either leaves the concurrent bytes in place and raises
                # ConcurrentRevisionError, or commits a replacement.  Mark
                # before calling it because a filesystem error can occur after
                # the native exchange is visible.
                write_started = True
                _atomic_compare_and_replace(path, rendered[path], before[path],
                                            backup=backup)
        except ConcurrentRevisionError:
            # The atomic CAS helper reverses its own mismatch exchange before
            # raising.  Do not convert this into a generic failure: the caller
            # must make the prepared journal terminal, never restore its stale
            # bytes over the editor's concurrent change.
            raise
        except BaseException as exc:
            if write_started:
                raise DestinationError(
                    f"{self.destination}: write failed after replacement began; "
                    "restore the journaled backup") from exc
            raise
        after = self.snapshot_revision(record)
        if after is None:
            raise DestinationError(
                f"{self.destination}: write produced no target files")
        return after

    def _before_final_revalidation(self, record: PromotionRecord,
                                   rendered: dict[Path, str]) -> None:
        """Test seam immediately before the final revision revalidation."""

    def _before_replace(self, record: PromotionRecord, path: Path) -> None:
        """Test seam immediately before an exact per-target revalidation."""

    def _before_atomic_commit(self, record: PromotionRecord, path: Path) -> None:
        """Test seam immediately before the native compare-and-swap commit."""

    def scaffold(self, record: PromotionRecord) -> str:
        """Initial content a fresh target file legitimately starts with."""
        return ""

    def _check_file_diff(self, record: PromotionRecord, path: Path,
                         old: str, new: str) -> None:
        assert_bounded_diff(old, new, record.record_key,
                            scaffold=self.scaffold(record))


def restore_backup(backup: BackupRef) -> None:
    """Return every target to its byte-identical pre-write state."""
    failures: list[str] = []
    home = (Path(str(backup.home_identity["canonical_path"]))
            if backup.home_identity else None)
    for entry in backup.entries:
        try:
            if home is not None:
                _assert_confined_target(home, entry.target,
                                        what="restore target")
            if entry.existed:
                entry.target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(entry.backup_path, entry.target)
                if _file_fingerprint(entry.target) != entry.fingerprint:
                    raise DestinationError(
                        f"restore of {entry.target} did not reproduce the "
                        "backed-up bytes")
            elif entry.target.is_file():
                entry.target.unlink()
        except (OSError, DestinationError) as exc:
            failures.append(str(exc))
    if failures:
        raise DestinationError("backup restore incomplete: " + "; ".join(failures))
    backup.mark_state("rolled_back")


# -- frontmatter (production-compatible, standard library only) --------------

def yaml_frontmatter_scalar(value: str) -> str:
    """Deterministic double-quoted YAML scalar for one untrusted string.

    Every untrusted value the adapters place in rendered frontmatter goes
    through this: double-quoting is the one plain-YAML style in which colons,
    hashes, quotes, booleans, null, brackets, unicode, and leading/trailing
    spaces are all inert data.  Escapes are the standard YAML double-quote
    escapes, so ``yaml.safe_load`` — and any spec-compliant production
    loader — reproduces the exact input string.
    """
    out = ['"']
    for ch in value:
        code = ord(ch)
        if ch == "\\":
            out.append("\\\\")
        elif ch == '"':
            out.append('\\"')
        elif (code < 0x20 or code == 0x7F or 0x80 <= code <= 0x9F
                or code in (0x2028, 0x2029, 0xFEFF)):
            out.append("\\u%04X" % code)
        else:
            out.append(ch)
    out.append('"')
    return "".join(out)


def _decode_quoted_scalar(raw: str, location: str) -> str:
    """Decode the double-quoted scalar subset ``yaml_frontmatter_scalar``
    emits (plus what a standard loader would accept of it)."""
    if len(raw) < 2 or not raw.endswith('"'):
        raise ReadBackError(
            f"{location}: unterminated double-quoted frontmatter scalar")
    inner = raw[1:-1]
    out: list[str] = []
    i = 0
    while i < len(inner):
        ch = inner[i]
        if ch == '"':
            raise ReadBackError(
                f"{location}: stray quote inside quoted frontmatter scalar")
        if ch != "\\":
            out.append(ch)
            i += 1
            continue
        esc = inner[i + 1:i + 2]
        if esc == "\\":
            out.append("\\")
            i += 2
        elif esc == '"':
            out.append('"')
            i += 2
        elif esc == "u" and re.fullmatch(r"[0-9a-fA-F]{4}",
                                         inner[i + 2:i + 6] or ""):
            out.append(chr(int(inner[i + 2:i + 6], 16)))
            i += 6
        else:
            raise ReadBackError(
                f"{location}: unsupported escape in quoted frontmatter scalar")
    return "".join(out)


def parse_frontmatter(text: str, location: str) -> tuple[dict[str, str], str]:
    """Parse the `--- key: value ---` header production loaders require.

    Returns (fields, body). Raises ReadBackError when the file would not load
    as a memory/skill file (missing delimiters, empty required shape).

    Double-quoted scalars (the ``yaml_frontmatter_scalar`` form every
    untrusted rendered value uses) are decoded back to their exact original
    string.  When PyYAML is importable, the whole block must additionally
    load under ``yaml.safe_load`` and every top-level string field must
    round-trip identically — read-back can then never issue proof for a file
    a standard production YAML loader would refuse or reinterpret.  The
    runtime itself stays stdlib-only; that cross-check simply activates
    wherever the dependency exists (tests and dev hosts included).
    """
    if not text.startswith("---\n"):
        raise ReadBackError(f"{location}: missing frontmatter opening '---'")
    end = text.find("\n---\n", 4)
    if end < 0:
        raise ReadBackError(f"{location}: unterminated frontmatter")
    block = text[4:end]
    fields: dict[str, str] = {}
    for line in block.splitlines():
        if not line.strip() or line.startswith((" ", "\t")):
            continue  # nested metadata keys are opaque here
        key, sep, value = line.partition(":")
        if sep:
            value = value.strip()
            if value.startswith('"'):
                value = _decode_quoted_scalar(value, location)
            fields[key.strip()] = value
    if _yaml is not None:
        try:
            loaded = _yaml.safe_load(block)
        except _yaml.YAMLError as exc:
            raise ReadBackError(
                f"{location}: frontmatter does not load under a standard "
                f"YAML loader: {exc}") from None
        if not isinstance(loaded, dict):
            raise ReadBackError(
                f"{location}: frontmatter does not load as a YAML mapping")
        for key, value in fields.items():
            if not value:
                continue
            if not isinstance(loaded.get(key), str) or loaded[key] != value:
                raise ReadBackError(
                    f"{location}: frontmatter field {key!r} does not "
                    "round-trip through a standard YAML loader "
                    f"({loaded.get(key)!r} != {value!r})")
    return fields, text[end + 5:]


def _slugify(subject: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", subject.lower()).strip("-")
    return slug[:48] or "record"


def _index_line_target_re(fact_name: str) -> re.Pattern[str]:
    return re.compile(rf"^- \[[^\]]*\]\({re.escape(fact_name)}\)[^\n]*$",
                      re.MULTILINE)


def _hook(claim: str, limit: int = 140) -> str:
    hook = " ".join(claim.split())
    return hook if len(hook) <= limit else hook[:limit - 1].rstrip() + "…"


class MemoryDestination(DestinationAdapter):
    """Memory home: MEMORY.md index + one fact file per record.

    hot  -> fact file plus an index line in MEMORY.md (the injected file);
    warm -> fact file only, retrieved on demand by term search.
    """

    adapter_name = "memory"

    def __init__(self, home: Path | str, tier: str):
        if tier not in ("hot", "warm"):
            raise DestinationError(f"unknown memory tier {tier!r}")
        super().__init__(home, f"memory:{tier}")
        self.tier = tier
        # Hot and warm both scan the same home-wide ``*.md`` fact set for
        # duplicates and share its conflict/recovery state, so their
        # promotions must serialize on ONE lock identity — a per-tier lock
        # would let a hot and a warm writer make simultaneous, mutually
        # stale duplicate/conflict/budget decisions over shared files.
        self.lock_scope = "memory"

    def policy_scope_destinations(self) -> tuple[str, ...]:
        return ("memory:hot", "memory:warm")

    # naming ---------------------------------------------------------------
    def fact_name(self, record: PromotionRecord) -> str:
        return f"{_slugify(record.subject)}-{record.record_key[:8]}.md"

    def fact_path(self, record: PromotionRecord) -> Path:
        return self.home / self.fact_name(record)

    @property
    def index_path(self) -> Path:
        return self.home / "MEMORY.md"

    def target_paths(self, record: PromotionRecord) -> list[Path]:
        paths = [self.fact_path(record)]
        if self.tier == "hot":
            paths.append(self.index_path)
        return paths

    # rendering --------------------------------------------------------------
    def _fact_content(self, record: PromotionRecord) -> str:
        slug = self.fact_name(record)[:-3]
        # The subject is untrusted free text; the deterministic double-quoted
        # emitter keeps YAML-significant content (colons, hashes, quotes,
        # booleans, null, brackets, edge spaces) inert under any standard
        # loader.  name/type stay plain: both come from closed safe grammars.
        body = (f"---\n"
                f"name: {slug}\n"
                f"description: {yaml_frontmatter_scalar(record.subject)}\n"
                f"metadata:\n"
                f"  type: {record.memory_type}\n"
                f"---\n\n"
                f"{record.claim}\n\n")
        region = render_record_region(record, "")
        return body + region

    def render(self, record: PromotionRecord) -> dict[Path, str]:
        rendered = {self.fact_path(record): self._fact_content(record)}
        if self.tier == "hot":
            fact_name = self.fact_name(record)
            line = f"- [{record.subject}]({fact_name}) — {_hook(record.claim)}"
            old = self._confined_text(self.index_path, "# Memory index\n\n")
            pattern = _index_line_target_re(fact_name)
            if pattern.search(old):
                new = pattern.sub(lambda _: line, old, count=1)
            else:
                if not old.endswith("\n"):
                    old += "\n"
                new = old + line + "\n"
            rendered[self.index_path] = new
        return rendered

    def _check_file_diff(self, record: PromotionRecord, path: Path,
                         old: str, new: str) -> None:
        if path == self.fact_path(record):
            # The fact file is wholly owned by this record: it must be either
            # fresh or a previous revision of the same record.
            if old and not self._owned_by(old, record.record_key):
                raise DiffBoundError(
                    f"{path} exists and is not owned by record "
                    f"{record.record_key}; refusing to overwrite")
            return
        # Index: only this record's own line may change. Compare every other
        # content-bearing line byte-for-byte.
        strip = _index_line_target_re(self.fact_name(record))
        def residue(text: str) -> list[str]:
            return [line for line in text.splitlines()
                    if line.strip() and not strip.fullmatch(line)]
        if residue(old) != residue(new):
            raise DiffBoundError(
                f"index write for {record.record_key} would modify other "
                "index lines; refusing")

    @staticmethod
    def _owned_by(text: str, record_key: str) -> bool:
        m = _PROV_RE.search(text)
        return bool(m and m.group("key") == record_key)

    def budget_context(self, record: PromotionRecord) -> str:
        # Design §9: the 2,200-token hot budget covers the combined injected
        # USER.md + MEMORY.md text, not the index alone.
        if self.tier != "hot":
            return ""
        return self._confined_text(self.home / "USER.md", "")

    # inspection ---------------------------------------------------------------
    def existing_records(self) -> list[ExistingRecord]:
        out: list[ExistingRecord] = []
        if not self.home.is_dir():
            return out
        for path in sorted(self.home.glob("*.md")):
            if path.name == "MEMORY.md":
                continue
            text = self._confined_text(path)
            try:
                fields, body = parse_frontmatter(text, str(path))
            except ReadBackError:
                fields, body = {}, text
            m = _PROV_RE.search(text)
            out.append(ExistingRecord(
                record_key=m.group("key") if m else None,
                subject=fields.get("description"),
                text=strip_record_region(body, m.group("key")) if m else body,
                location=str(path)))
        return out

    # verification ---------------------------------------------------------
    def read_back(self, record: PromotionRecord) -> str:
        fact = self.fact_path(record)
        if not fact.is_file():
            raise ReadBackError(f"{fact}: fact file missing after write")
        fields, body = parse_frontmatter(self._confined_text(fact), str(fact))
        for key in ("name", "description"):
            if not fields.get(key):
                raise ReadBackError(f"{fact}: frontmatter missing '{key}'")
        if fields["description"] != record.subject:
            raise ReadBackError(
                f"{fact}: frontmatter description does not round-trip the "
                "canonical subject; refusing to issue read-back proof")
        if record.claim not in body:
            raise ReadBackError(f"{fact}: claim text not present in body")
        if self.tier == "hot":
            index = self._confined_text(self.index_path, "")
            m = _index_line_target_re(self.fact_name(record)).search(index)
            if not m:
                raise ReadBackError(
                    f"{self.index_path}: no index line links "
                    f"{self.fact_name(record)}")
            return (f"hot memory read-back: index line {m.group(0)!r} links "
                    f"to fact file carrying the claim")
        return f"warm memory read-back: {fact.name} carries claim and frontmatter"

    def retrieval_proof(self, record: PromotionRecord) -> str:
        if self.tier == "hot":
            # Production route: MEMORY.md is injected verbatim at session
            # start; the record is retrievable iff its line is in that text.
            injected = self._confined_text(self.index_path, "")
            m = _index_line_target_re(self.fact_name(record)).search(injected)
            if not m:
                raise RetrievalProofError(
                    "hot injection text does not surface the record")
            return f"hot_injection:{m.group(0)}"
        terms = record.retrieval_terms or tuple(record.subject.lower().split())
        hits = search_warm_memory(self.home, terms)
        for path, matched in hits:
            if path == self.fact_path(record):
                return f"warm_term_search:{matched!r} -> {path.name}"
        raise RetrievalProofError(
            f"warm term search over {list(terms)} did not return the record")


def search_warm_memory(home: Path, terms: Iterable[str]
                       ) -> list[tuple[Path, str]]:
    """Production-shaped warm recall: term match over description + body."""
    hits: list[tuple[Path, str]] = []
    home = Path(home)
    for path in sorted(home.glob("*.md")):
        if path.name == "MEMORY.md":
            continue
        text = _confined_read_bytes(home, path).decode("utf-8").lower()
        for term in terms:
            if term.lower() in text:
                hits.append((path, term))
                break
    return hits


class SkillDestination(DestinationAdapter):
    """Skill home: <skill_id>/SKILL.md with loader-required frontmatter."""

    adapter_name = "skill"

    def __init__(self, home: Path | str, skill_id: str):
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", skill_id):
            raise DestinationError(f"invalid skill_id {skill_id!r}")
        super().__init__(home, f"skill:{skill_id}")
        self.skill_id = skill_id

    @property
    def skill_path(self) -> Path:
        return self.home / self.skill_id / "SKILL.md"

    def target_paths(self, record: PromotionRecord) -> list[Path]:
        return [self.skill_path]

    def scaffold(self, record: PromotionRecord) -> str:
        # The subject is untrusted free text: quote it so a fresh skill's
        # frontmatter stays loadable by any standard YAML loader.
        return (f"---\nname: {self.skill_id}\n"
                f"description: {yaml_frontmatter_scalar(record.subject)}\n---\n")

    def render(self, record: PromotionRecord) -> dict[Path, str]:
        old = self._confined_text(self.skill_path, self.scaffold(record))
        body = f"## {record.subject}\n\n{record.claim}\n"
        region = render_record_region(record, body)
        return {self.skill_path:
                upsert_record_region(old, record.record_key, region)}

    def existing_records(self) -> list[ExistingRecord]:
        out: list[ExistingRecord] = []
        if not self.skill_path.is_file():
            return out
        text = self._confined_text(self.skill_path)
        for m in _PROV_RE.finditer(text):
            region_re = re.compile(
                _REGION_RE_TMPL.format(key=re.escape(m.group("key"))),
                re.DOTALL)
            region = region_re.search(text)
            subject, claim_text = (split_region_body(region.group(0))
                                   if region else (None, ""))
            out.append(ExistingRecord(
                record_key=m.group("key"), subject=subject,
                text=claim_text,
                location=str(self.skill_path)))
        residue = text
        for m in _PROV_RE.finditer(text):
            residue = strip_record_region(residue, m.group("key"))
        try:
            _, body = parse_frontmatter(residue, str(self.skill_path))
        except ReadBackError:
            body = residue
        if body.strip():
            out.append(ExistingRecord(record_key=None, subject=None,
                                      text=body, location=str(self.skill_path)))
        return out

    def read_back(self, record: PromotionRecord) -> str:
        if not self.skill_path.is_file():
            raise ReadBackError(f"{self.skill_path}: missing after write")
        fields, body = load_skill(self.home, self.skill_id)
        if record.claim not in body:
            raise ReadBackError(
                f"{self.skill_path}: claim not present in loaded skill body")
        return (f"skill read-back: loader parsed frontmatter "
                f"(name={fields['name']!r}) and body carries the claim")

    def retrieval_proof(self, record: PromotionRecord) -> str:
        # Production route: the skill is selected by id from the registry,
        # loaded, and the subject section located.
        if self.skill_id not in list_skills(self.home):
            raise RetrievalProofError(
                f"skill {self.skill_id} not discoverable in skill home")
        _, body = load_skill(self.home, self.skill_id)
        heading = f"## {record.subject}"
        if heading not in body:
            raise RetrievalProofError(
                f"skill body has no section {heading!r}")
        return f"skill_lookup:{self.skill_id} -> section {heading!r}"


def list_skills(home: Path | str) -> list[str]:
    home = Path(home)
    if not home.is_dir():
        return []
    return sorted(p.parent.name for p in home.glob("*/SKILL.md"))


def load_skill(home: Path | str, skill_id: str) -> tuple[dict[str, str], str]:
    """Production-shaped skill loader: strict frontmatter, then body."""
    home = Path(home)
    path = home / skill_id / "SKILL.md"
    if not path.is_file():
        raise ReadBackError(f"{path}: no such skill")
    fields, body = parse_frontmatter(
        _confined_read_bytes(home, path).decode("utf-8"), str(path))
    for key in ("name", "description"):
        if not fields.get(key):
            raise ReadBackError(f"{path}: frontmatter missing '{key}'")
    return fields, body


class ProjectDocDestination(DestinationAdapter):
    """Project docs home: <project_id>/<doc>.md (e.g. decisions, context)."""

    adapter_name = "project_doc"

    def __init__(self, home: Path | str, project_id: str, doc: str):
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", project_id) or \
                not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", doc):
            raise DestinationError(
                f"invalid project doc locator {project_id!r}/{doc!r}")
        super().__init__(home, f"project:{project_id}:{doc}")
        self.project_id = project_id
        self.doc = doc

    @property
    def doc_path(self) -> Path:
        return self.home / self.project_id / f"{self.doc}.md"

    def target_paths(self, record: PromotionRecord) -> list[Path]:
        return [self.doc_path]

    def scaffold(self, record: PromotionRecord) -> str:
        return f"# {self.project_id}: {self.doc}\n"

    def render(self, record: PromotionRecord) -> dict[Path, str]:
        old = self._confined_text(self.doc_path, self.scaffold(record))
        body = f"## {record.subject}\n\n{record.claim}\n"
        region = render_record_region(record, body)
        return {self.doc_path:
                upsert_record_region(old, record.record_key, region)}

    def existing_records(self) -> list[ExistingRecord]:
        out: list[ExistingRecord] = []
        if not self.doc_path.is_file():
            return out
        text = self._confined_text(self.doc_path)
        seen = set()
        for m in _PROV_RE.finditer(text):
            key = m.group("key")
            if key in seen:
                continue
            seen.add(key)
            region_re = re.compile(_REGION_RE_TMPL.format(key=re.escape(key)),
                                   re.DOTALL)
            region = region_re.search(text)
            subject, claim_text = (split_region_body(region.group(0))
                                   if region else (None, ""))
            out.append(ExistingRecord(
                record_key=key, subject=subject,
                text=claim_text,
                location=str(self.doc_path)))
        residue = text
        for key in seen:
            residue = strip_record_region(residue, key)
        if residue.strip():
            out.append(ExistingRecord(record_key=None, subject=None,
                                      text=residue, location=str(self.doc_path)))
        return out

    def read_back(self, record: PromotionRecord) -> str:
        sections = read_project_doc_sections(self.home, self.project_id, self.doc)
        for heading, content in sections:
            if heading == record.subject and record.claim in content:
                return (f"project doc read-back: section {heading!r} in "
                        f"{self.doc_path} carries the claim")
        raise ReadBackError(
            f"{self.doc_path}: no section {record.subject!r} carrying the claim")

    def retrieval_proof(self, record: PromotionRecord) -> str:
        sections = read_project_doc_sections(self.home, self.project_id, self.doc)
        for heading, _ in sections:
            if heading == record.subject:
                return (f"project_doc_lookup:{self.project_id}/{self.doc} -> "
                        f"section {heading!r}")
        raise RetrievalProofError(
            f"project doc lookup found no section {record.subject!r}")


def read_project_doc_sections(home: Path | str, project_id: str, doc: str
                              ) -> list[tuple[str, str]]:
    """Production-shaped doc reader: '## ' sections of the project document."""
    home = Path(home)
    path = home / project_id / f"{doc}.md"
    if not path.is_file():
        raise ReadBackError(f"{path}: no such project document")
    sections: list[tuple[str, str]] = []
    heading, buf = None, []
    for line in _confined_read_bytes(home, path).decode("utf-8").splitlines():
        if line.startswith("## "):
            if heading is not None:
                sections.append((heading, "\n".join(buf)))
            heading, buf = line[3:].strip(), []
        elif heading is not None:
            buf.append(line)
    if heading is not None:
        sections.append((heading, "\n".join(buf)))
    return sections


@dataclass(frozen=True)
class DestinationHomes:
    """Explicit fixture/shadow homes; there is no default pointing at live state."""
    memory: Path
    skills: Path
    projects: Path


def adapter_for_destination(destination: str, homes: DestinationHomes
                            ) -> DestinationAdapter:
    parts = destination.split(":")
    if parts[0] == "memory" and len(parts) == 2 and parts[1] in ("hot", "warm"):
        return MemoryDestination(homes.memory, parts[1])
    if parts[0] == "skill" and len(parts) == 2:
        return SkillDestination(homes.skills, parts[1])
    if parts[0] == "project" and len(parts) == 3:
        return ProjectDocDestination(homes.projects, parts[1], parts[2])
    raise DestinationError(
        f"no destination adapter for {destination!r}; promotion to this "
        "destination is not implemented and must quarantine")
