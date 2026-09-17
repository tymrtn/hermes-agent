"""Read-only local Messages search: the opt-in gate, the Apple-epoch boundary, and the query.

The tool reads macOS ``chat.db`` directly, so these build a real SQLite file with the columns the
query joins on and point ``IMESSAGE_CHAT_DB`` at it. Nothing touches a real Messages database, and
no test enables the tool for the process beyond its own monkeypatched env.
"""

import json
import sqlite3
from datetime import datetime, timezone

import pytest

from tools.imessage_search_tool import (
    APPLE_EPOCH_OFFSET, check_imessage_search_requirements, imessage_search,
)

_SCHEMA = """
CREATE TABLE handle (ROWID INTEGER PRIMARY KEY, id TEXT, uncanonicalized_id TEXT);
CREATE TABLE chat (ROWID INTEGER PRIMARY KEY, guid TEXT, chat_identifier TEXT, display_name TEXT);
CREATE TABLE message (
    ROWID INTEGER PRIMARY KEY, guid TEXT, date INTEGER, is_from_me INTEGER,
    service TEXT, text TEXT, handle_id INTEGER);
CREATE TABLE chat_message_join (chat_id INTEGER, message_id INTEGER);
"""


def _apple_ns(iso: str) -> int:
    """Apple stores nanoseconds since 2001-01-01, which is what the tool has to translate."""
    unix = int(datetime.fromisoformat(iso).replace(tzinfo=timezone.utc).timestamp())
    return (unix - APPLE_EPOCH_OFFSET) * 1_000_000_000


@pytest.fixture()
def chat_db(tmp_path, monkeypatch):
    path = tmp_path / "chat.db"
    conn = sqlite3.connect(path)
    conn.executescript(_SCHEMA)
    conn.execute("INSERT INTO handle VALUES (1, '+15551234567', '5551234567')")
    conn.execute("INSERT INTO chat VALUES (1, 'iMessage;-;+15551234567', '+15551234567', 'Sam')")
    rows = [
        (1, "g1", _apple_ns("2026-03-01T10:00:00"), 0, "iMessage", "dinner at eight?", 1),
        (2, "g2", _apple_ns("2026-03-02T10:00:00"), 1, "iMessage", "dinner works", 1),
        (3, "g3", _apple_ns("2026-06-01T10:00:00"), 0, "SMS", "unrelated later message", 1),
        (4, "g4", _apple_ns("2026-06-02T10:00:00"), 0, "iMessage", None, 1),  # attachment-only row
    ]
    conn.executemany("INSERT INTO message VALUES (?, ?, ?, ?, ?, ?, ?)", rows)
    conn.executemany("INSERT INTO chat_message_join VALUES (1, ?)", [(r[0],) for r in rows])
    conn.commit()
    conn.close()
    monkeypatch.setenv("IMESSAGE_CHAT_DB", str(path))
    return path


def _search(**kwargs) -> dict:
    return json.loads(imessage_search(**kwargs))


class TestGate:

    def test_the_tool_is_off_unless_explicitly_enabled(self, monkeypatch):
        monkeypatch.delenv("IMESSAGE_SEARCH_ENABLED", raising=False)
        assert check_imessage_search_requirements() is False

    def test_enabling_it_is_a_deliberate_env_opt_in(self, monkeypatch):
        monkeypatch.setenv("IMESSAGE_SEARCH_ENABLED", "true")
        assert check_imessage_search_requirements() is True

    def test_a_missing_database_says_so_instead_of_raising(self, tmp_path, monkeypatch):
        monkeypatch.setenv("IMESSAGE_CHAT_DB", str(tmp_path / "nope.db"))
        result = _search(query="anything")
        assert result["success"] is False
        assert "not found" in result["error"]


class TestSearch:

    def test_a_text_query_matches_a_substring(self, chat_db):
        result = _search(query="dinner")
        assert result["total_matches"] == 2
        assert [r["text"] for r in result["results"]] == ["dinner at eight?", "dinner works"]

    def test_rows_with_no_text_are_never_returned(self, chat_db):
        """Attachment-only rows have NULL text; they would otherwise read as empty messages."""
        assert all(r["text"] for r in _search()["results"])
        assert _search()["total_matches"] == 3

    def test_dates_bound_the_range_inclusively_at_both_ends(self, chat_db):
        result = _search(start_date="2026-03-01", end_date="2026-03-02")
        assert [r["message_id"] for r in result["results"]] == [1, 2]

    def test_apple_epoch_dates_come_back_as_iso(self, chat_db):
        assert _search(query="dinner at")["results"][0]["date"].startswith("2026-03-01T10:00:00")

    def test_a_handle_filter_matches_either_canonical_form(self, chat_db):
        assert _search(handle="5551234567")["total_matches"] == 3
        assert _search(handle="+15559999999")["total_matches"] == 0

    def test_results_carry_their_chat_identity(self, chat_db):
        row = _search(query="dinner works")["results"][0]
        assert row["is_from_me"] is True
        assert row["chat_identifier"] == "+15551234567"
        assert row["display_name"] == "Sam"

    def test_paging_walks_the_match_set_without_losing_the_total(self, chat_db):
        first = _search(query="dinner", limit=1)
        second = _search(query="dinner", limit=1, offset=1)
        assert first["total_matches"] == second["total_matches"] == 2
        assert first["results"][0]["message_id"] != second["results"][0]["message_id"]

    def test_limit_is_clamped_to_the_documented_range(self, chat_db):
        assert _search(limit=10_000)["limit"] == 500
        assert _search(limit=-5)["limit"] == 1
        assert _search(limit=0)["limit"] == 50  # 0 reads as "unset" and takes the schema default

    def test_an_unparseable_date_is_refused_with_the_value_named(self, chat_db):
        result = _search(start_date="last tuesday")
        assert result["success"] is False
        assert "last tuesday" in result["error"]
