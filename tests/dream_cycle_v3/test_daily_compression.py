"""Daily continuity compression: one bounded durable record per profile/date.

Behaviour covered: a deterministic cycle produces a retrievable record; an
identical rerun neither duplicates it nor mutates source evidence; genuinely
new evidence for the same date appends a revision instead of rewriting one;
empty days abstain explicitly; the record never carries transcript bodies and
is sanitized; the wake path consumes it as bounded optional context without
displacing what the packet already carried; and the LLM seam stays disabled.
"""
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

from .conftest import write_tree  # noqa: F401 (package-relative test helper)

from dream_cycle_v3.daily_compression import (LLM_SUMMARY_ENABLED,
                                              OUTCOME_NO_NEW_EVIDENCE,
                                              OUTCOME_RECORDED,
                                              build_daily_compression,
                                              compress_day,
                                              current_daily_compression,
                                              read_daily_compression,
                                              wake_compression_line)
from dream_cycle_v3.dry_run import SAMPLE_DATA, _build_sample_kanban_db
from dream_cycle_v3.errors import DreamCycleError, StoreError
from dream_cycle_v3.runtime import RuntimeConfig, run_cycle
from dream_cycle_v3.store import ContinuityStore
from dream_cycle_v3.wake import PACKET_BUDGET, WakeInputs, build_wake_packet

WINDOW_START = datetime(2026, 7, 11, 0, 0, tzinfo=timezone.utc)
WINDOW_END = datetime(2026, 7, 12, 0, 0, tzinfo=timezone.utc)
DATE = "2026-07-11"
AS_OF = "2026-07-12T00:30:00+00:00"
MTIME = int(datetime(2026, 7, 11, 12, 0, tzinfo=timezone.utc).timestamp())
PROFILE = "nagatha-test"

SECRET_CANARY = "sk-canary12345678901234567890"
TRANSCRIPT_CANARY = "private transcript text that must never be compressed"


@pytest.fixture
def home(tmp_path, monkeypatch):
    """A temporary HERMES_HOME-shaped profile root.

    Every path the cycle touches hangs off this root, so nothing in the test
    can reach a live profile: sources, the v3 output root, and the continuity
    store all resolve below it.
    """
    root = tmp_path / "hermes-home"
    (root / "profiles").mkdir(parents=True)
    profile_home = root / "profiles" / PROFILE
    profile_home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(root))
    return profile_home


@pytest.fixture
def env(home):
    sources = home / "sources" / "profile"
    sources.mkdir(parents=True)
    write_tree(sources, {
        "state/wake-up.md": "## Wake\n- carry the offload thread forward\n",
        "sessions/20260711-session.jsonl":
            json.dumps({"role": "user", "content": TRANSCRIPT_CANARY}) + "\n",
    }, mtime=MTIME)

    registry = home / "registry.json"
    registry.write_text(
        (SAMPLE_DATA / "projects.json").read_text(encoding="utf-8"),
        encoding="utf-8")
    threads = home / "threads.json"
    threads.write_text(
        (SAMPLE_DATA / "threads.json").read_text(encoding="utf-8"),
        encoding="utf-8")
    kanban_db = _build_sample_kanban_db(home / "trackers" / "kanban" / "kanban.db")
    return {
        "home": home,
        "sources": sources,
        "v3_root": home / "dream-cycle-v3",
        "registry": registry,
        "threads": threads,
        "kanban_db": kanban_db,
    }


def make_config(env, **over):
    base = dict(
        profile=PROFILE,
        owner="nagatha",
        read_roots={"profile": env["sources"]},
        v3_root=env["v3_root"],
        mode="shadow",
        window_start=WINDOW_START,
        window_end=WINDOW_END,
        disposition_date=DATE,
        as_of=AS_OF,
        registry_path=env["registry"],
        threads_path=env["threads"],
        kanban_db=env["kanban_db"],
        kanban_board="sample-board",
    )
    base.update(over)
    return RuntimeConfig(**base)


def store_path(env):
    return env["v3_root"] / "continuity.db"


def open_store(env):
    return ContinuityStore(store_path(env), read_only=True)


def source_fingerprint(env) -> list:
    """Bytes + mtimes of every source the cycle reads, plus the manifests and
    reports it published. Used to prove a rerun mutates no evidence."""
    watched = [env["sources"], env["v3_root"] / "manifests",
               env["v3_root"] / "reports"]
    watched += [env["registry"], env["threads"], env["kanban_db"]]
    out = []
    for target in watched:
        target = Path(target)
        if target.is_file():
            out.append((str(target), target.stat().st_mtime,
                        target.read_bytes()))
        elif target.is_dir():
            out.extend((str(p), p.stat().st_mtime, p.read_bytes())
                       for p in sorted(target.rglob("*")) if p.is_file())
    return sorted(out)


# -- acceptance 1: produced and retrievable from a deterministic cycle -------

def test_cycle_produces_retrievable_daily_compression(env):
    result = run_cycle(make_config(env))
    assert result.ok is True

    section = result.report["daily_compression"]
    assert section["enabled"] is True
    assert section["date"] == DATE
    assert section["revision"] == 1
    assert section["outcome"] == OUTCOME_RECORDED
    assert f"compression={OUTCOME_RECORDED}/rev1" in result.status_line

    record = read_daily_compression(store_path(env), profile=PROFILE,
                                    compression_date=DATE,
                                    confine_root=env["home"])
    assert record is not None
    assert record["kind"] == "dream-cycle-v3-daily-compression"
    assert record["compression_id"] == section["compression_id"]
    assert record["outcome"] == OUTCOME_RECORDED
    assert record["abstained"] is False
    # Bounded, already-collected continuity evidence only.
    assert record["counts"]["active_projects"] >= 1
    assert record["counts"]["unresolved_threads"] >= 1
    assert record["counts"]["state_changes"] >= 1
    assert record["active_projects"], "active projects must be compacted"
    assert record["unresolved_threads"], "open threads must be compacted"


def test_record_states_provenance_and_source_references(env):
    run_cycle(make_config(env))
    record = read_daily_compression(store_path(env), profile=PROFILE,
                                    compression_date=DATE)

    prov = record["provenance"]
    assert prov["classifier_kind"] == "deterministic"
    assert prov["llm_summary"] is None
    assert prov["compressor_version"]
    assert prov["generated_at"] == AS_OF
    with open_store(env) as store:
        run = store._conn.execute("SELECT * FROM runs").fetchone()
    assert prov["run_id"] == run["run_id"]
    assert prov["manifest_fingerprint"] == run["manifest_fingerprint"]

    kinds = {ref["kind"] for ref in record["source_refs"]}
    assert "manifest" in kinds, "the raw manifest must stay referenced"
    assert "adapter_snapshot" in kinds
    # Threads carry their own evidence pointers back to preserved sources.
    assert any(t["source_refs"] for t in record["unresolved_threads"])


def test_retrieval_by_on_or_before_finds_the_most_recent_record(env):
    run_cycle(make_config(env))
    later = read_daily_compression(store_path(env), profile=PROFILE,
                                   on_or_before="2026-09-01")
    assert later is not None and later["compression_date"] == DATE
    # Nothing recorded on or before a date earlier than the only record.
    assert read_daily_compression(store_path(env), profile=PROFILE,
                                  on_or_before="2026-07-01") is None


# -- acceptance 2: idempotent rerun, no duplication, no source mutation ------

def test_identical_rerun_neither_duplicates_nor_mutates_evidence(env):
    cfg = make_config(env)
    first = run_cycle(cfg)
    with open_store(env) as store:
        dump_before = store.dump_canonical()
        counts_before = store.counts()
    sources_before = source_fingerprint(env)

    second = run_cycle(cfg)

    with open_store(env) as store:
        assert store.counts() == counts_before
        assert store.dump_canonical() == dump_before
        rows = store.daily_compression_revisions(PROFILE, DATE)
    assert len(rows) == 1, "an identical rerun must add no compression row"
    assert counts_before["daily_compressions"] == 1
    assert (second.report["daily_compression"]
            == first.report["daily_compression"])
    assert source_fingerprint(env) == sources_before, \
        "a rerun must not rewrite any source or published evidence"


def test_new_evidence_same_date_appends_a_revision(env):
    run_cycle(make_config(env))
    with open_store(env) as store:
        first = store.get_daily_compression(PROFILE, DATE)
        first_payload = first["payload"]

    # A genuinely new unresolved thread for the same date.
    with ContinuityStore(store_path(env)) as store:
        run_id = store._conn.execute("SELECT run_id FROM runs").fetchone()[0]
        project_id = store._conn.execute(
            "SELECT project_id FROM projects LIMIT 1").fetchone()[0]
        store.open_thread({
            "schema_version": 1,
            "thread_id": "daily-compression-new-0001",
            "project_id": project_id,
            "external_task_ref": None,
            "link_disposition": "needs_link",
            "title": "New evidence landed after the first compression",
            "normalized_next_action": "triage the new evidence",
            "owner": "nagatha",
            "state": "active",
            "opened_from": "test",
            "evidence_refs": [{
                "source_type": "file",
                "source_id": "profile:state/new.md",
                "fingerprint": "fp-daily-compression-0001",
                "observed_at": "2026-07-11T21:00:00+00:00",
            }],
            "last_disposition_date": DATE,
            "idempotency_key": "idem-daily-compression-new-0001",
        }, AS_OF)
        second = compress_day(store, profile=PROFILE, compression_date=DATE,
                              run_id=run_id, as_of=AS_OF, now=AS_OF)

    assert second.stored == "inserted"
    assert second.revision == 2
    with open_store(env) as store:
        rows = store.daily_compression_revisions(PROFILE, DATE)
        current = store.get_daily_compression(PROFILE, DATE)
    assert [r["revision"] for r in rows] == [1, 2]
    assert rows[0]["payload"] == first_payload, \
        "an earlier revision is never rewritten"
    assert current["revision"] == 2
    assert current["compression_id"] == second.compression_id


def test_compression_rows_are_append_only(env):
    run_cycle(make_config(env))
    with ContinuityStore(store_path(env)) as store:
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            store._conn.execute(
                "UPDATE daily_compressions SET outcome = 'no_new_evidence'")
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            store._conn.execute("DELETE FROM daily_compressions")


# -- acceptance 3: empty / no-new-evidence is explicit and safe --------------

def test_empty_store_abstains_explicitly(env):
    cfg = make_config(env, registry_path=None, threads_path=None,
                      kanban_db=None, kanban_board=None)
    result = run_cycle(cfg)

    section = result.report["daily_compression"]
    assert section["outcome"] == OUTCOME_NO_NEW_EVIDENCE
    record = read_daily_compression(store_path(env), profile=PROFILE,
                                    compression_date=DATE)
    assert record["abstained"] is True
    assert record["abstention_reason"]
    assert record["active_projects"] == []
    assert record["unresolved_threads"] == []
    assert record["state_changes"] == []
    assert record["counts"]["unresolved_threads"] == 0
    # Abstention is durable evidence, not silence.
    with open_store(env) as store:
        assert store.counts()["daily_compressions"] == 1


def test_abstained_record_contributes_nothing_to_the_wake_packet(env):
    run_cycle(make_config(env, registry_path=None, threads_path=None,
                          kanban_db=None, kanban_board=None))
    record = read_daily_compression(store_path(env), profile=PROFILE,
                                    compression_date=DATE)
    assert wake_compression_line(record) == ""


def test_missing_record_reads_as_absent_not_an_error(env):
    run_cycle(make_config(env))
    assert read_daily_compression(store_path(env), profile=PROFILE,
                                  compression_date="2026-07-10") is None
    assert read_daily_compression(store_path(env), profile="other-profile",
                                  compression_date=DATE) is None
    assert read_daily_compression(env["v3_root"] / "absent.db",
                                  profile=PROFILE) is None
    assert wake_compression_line(None) == ""


# -- containment: no transcripts, sanitized, confined -----------------------

def test_record_never_carries_transcript_or_secret_text(env):
    run_cycle(make_config(env))
    with ContinuityStore(store_path(env)) as store:
        run_id = store._conn.execute("SELECT run_id FROM runs").fetchone()[0]
        project_id = store._conn.execute(
            "SELECT project_id FROM projects LIMIT 1").fetchone()[0]
        store.open_thread({
            "schema_version": 1,
            "thread_id": "daily-compression-secret-0001",
            "project_id": project_id,
            "external_task_ref": None,
            "link_disposition": "needs_link",
            "title": f"rotate the key {SECRET_CANARY}",
            "normalized_next_action": "rotate and redeploy",
            "owner": "nagatha",
            "state": "active",
            "opened_from": "test",
            "evidence_refs": [{
                "source_type": "session",
                "source_id": "session:20260711-session",
                "location": "sessions/20260711-session.jsonl",
                "fingerprint": "fp-daily-compression-secret-01",
                "observed_at": "2026-07-11T21:00:00+00:00",
            }],
            "last_disposition_date": DATE,
            "idempotency_key": "idem-daily-compression-secret-0001",
        }, AS_OF)
        record = build_daily_compression(store, profile=PROFILE,
                                         compression_date=DATE,
                                         run_id=run_id, as_of=AS_OF)

    serialized = json.dumps(record)
    assert SECRET_CANARY not in serialized
    assert TRANSCRIPT_CANARY not in serialized
    assert "[privacy_withheld]" in serialized, \
        "the secret-shaped title must be withheld, not clipped"
    # Session evidence survives as a metadata pointer only.
    session_refs = [ref for thread in record["unresolved_threads"]
                    for ref in thread["source_refs"]
                    if ref["source_type"] == "session"]
    assert session_refs
    assert all("excerpt" not in ref for ref in session_refs)


def test_read_refuses_a_store_outside_the_confinement_root(env, tmp_path):
    run_cycle(make_config(env))
    outside = tmp_path / "another-profile"
    outside.mkdir()
    with pytest.raises(DreamCycleError):
        read_daily_compression(store_path(env), profile=PROFILE,
                               compression_date=DATE, confine_root=outside)


def test_build_rejects_malformed_inputs(env):
    run_cycle(make_config(env))
    with ContinuityStore(store_path(env), read_only=True) as store:
        run_id = store._conn.execute("SELECT run_id FROM runs").fetchone()[0]
        with pytest.raises(DreamCycleError, match="calendar date"):
            build_daily_compression(store, profile=PROFILE,
                                    compression_date="2026-13-99",
                                    run_id=run_id, as_of=AS_OF)
        with pytest.raises(DreamCycleError, match="ISO-8601"):
            build_daily_compression(store, profile=PROFILE,
                                    compression_date=DATE, run_id=run_id,
                                    as_of="not-a-time")
        with pytest.raises(DreamCycleError, match="profile"):
            build_daily_compression(store, profile="  ",
                                    compression_date=DATE, run_id=run_id,
                                    as_of=AS_OF)


def test_store_refuses_an_unknown_compression_outcome(env):
    run_cycle(make_config(env))
    with ContinuityStore(store_path(env)) as store:
        run_id = store._conn.execute("SELECT run_id FROM runs").fetchone()[0]
        with pytest.raises(StoreError, match="outcome"):
            store.record_daily_compression(
                profile=PROFILE, compression_date=DATE, run_id=run_id,
                outcome="made_up", payload={}, generated_at=AS_OF, now=AS_OF)


# -- acceptance 4: the wake path consumes it without displacing anything ----

def wake(env, *, include=True, now=AS_OF):
    return build_wake_packet(
        store_path=store_path(env),
        projects_home=None, kanban_root=None,
        confine_root=env["home"],
        include_daily_compression=include,
        inputs=WakeInputs(profile=PROFILE, owner="nagatha", now=now,
                          first_message="dream cycle status"))


def test_wake_packet_consumes_the_daily_compression(env):
    run_cycle(make_config(env))

    with_line = wake(env)
    without_line = wake(env, include=False)
    assert with_line is not None and not with_line.degraded
    assert "Daily continuity compression" in with_line.text
    assert DATE in with_line.text
    assert "Daily continuity compression" not in without_line.text
    assert len(with_line.text) <= PACKET_BUDGET
    # Optional context: the rest of the packet is untouched by the addition.
    assert with_line.project_id == without_line.project_id
    assert with_line.thread_ids == without_line.thread_ids
    for line in without_line.text.splitlines():
        assert line in with_line.text


def test_wake_packet_is_unchanged_when_no_compression_exists(env):
    run_cycle(make_config(env, daily_compression=False))
    with open_store(env) as store:
        assert store.counts()["daily_compressions"] == 0

    with_flag = wake(env)
    without_flag = wake(env, include=False)
    assert with_flag is not None
    assert with_flag.text == without_flag.text
    assert with_flag.content_hash == without_flag.content_hash


def test_wake_packet_binding_is_stable_for_the_same_inputs(env):
    run_cycle(make_config(env))
    first, second = wake(env), wake(env)
    assert first.content_hash == second.content_hash
    assert first.packet_id == second.packet_id


def test_wake_line_is_bounded_and_trimmed_before_existing_content(env):
    run_cycle(make_config(env))
    record = read_daily_compression(store_path(env), profile=PROFILE,
                                    compression_date=DATE)
    line = wake_compression_line(record)
    assert 0 < len(line) <= 400
    assert "\n" not in line

    # Under budget pressure the compression is the first element dropped:
    # a tiny budget leaves the packet exactly as it is without it.
    import dream_cycle_v3.wake as wake_mod
    original = wake_mod.PACKET_BUDGET
    try:
        wake_mod.PACKET_BUDGET = len(wake(env, include=False).text)
        squeezed = wake(env)
        assert "Daily continuity compression" not in squeezed.text
        assert squeezed.text == wake(env, include=False).text
    finally:
        wake_mod.PACKET_BUDGET = original


# -- the optional future LLM interface stays disabled -----------------------

def test_llm_summarizer_seam_is_disabled_and_fails_loud(env):
    assert LLM_SUMMARY_ENABLED is False
    run_cycle(make_config(env))

    class _Summarizer:
        def summarize(self, sections):  # pragma: no cover - never invoked
            raise AssertionError("the disabled seam must never be called")

    with ContinuityStore(store_path(env), read_only=True) as store:
        run_id = store._conn.execute("SELECT run_id FROM runs").fetchone()[0]
        with pytest.raises(DreamCycleError, match="disabled"):
            build_daily_compression(store, profile=PROFILE,
                                    compression_date=DATE, run_id=run_id,
                                    as_of=AS_OF, summarizer=_Summarizer())
        record = build_daily_compression(store, profile=PROFILE,
                                         compression_date=DATE,
                                         run_id=run_id, as_of=AS_OF)
    assert record["provenance"]["llm_summary"] is None
    assert record["provenance"]["classifier_kind"] == "deterministic"


# -- opt-out keeps the cycle exactly as it was ------------------------------

def test_cycle_can_be_run_without_daily_compression(env):
    result = run_cycle(make_config(env, daily_compression=False))
    assert result.ok is True
    assert result.report["daily_compression"] == {"enabled": False}
    assert "compression=off" in result.status_line
    with open_store(env) as store:
        assert store.counts()["daily_compressions"] == 0
    assert current_daily_compression(
        ContinuityStore(store_path(env), read_only=True),
        profile=PROFILE, compression_date=DATE) is None


# -- the CLI read surface ---------------------------------------------------

def test_cli_daily_compression_reads_the_record(env, capsys):
    from dream_cycle_v3.cli import main

    run_cycle(make_config(env))
    rc = main(["daily-compression", "--db", str(store_path(env)),
               "--profile", PROFILE, "--date", DATE])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["kind"] == "dream-cycle-v3-daily-compression"
    assert payload["compression_date"] == DATE

    rc = main(["daily-compression", "--db", str(store_path(env)),
               "--profile", PROFILE, "--date", "2026-07-01"])
    assert rc == 1
    assert json.loads(capsys.readouterr().out)["found"] is False
