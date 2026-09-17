"""Media-download retry tests against the REAL python-telegram-bot errors.

The retry tests in ``test_telegram_documents.py`` raise a locally declared
``TimedOut`` stub under the mocked ``telegram`` package, which only exercises
the *name* half of :meth:`TelegramAdapter._looks_like_network_error`. This file
pins the behavioral classifier contract to the installed package instead of
to a stub that agrees with us by construction.

``tests/gateway/conftest.py`` installs its ``telegram`` MagicMock whenever the
real package is not *already imported* — which, at directory-conftest time, is
always. So the setup below evicts those mock entries and imports the real
package, guarded by ``PathFinder.find_spec`` (which searches ``sys.path`` and
ignores ``sys.modules``) so the mock is only ever removed when a real import is
guaranteed to succeed. Without PTB installed, the mock is left exactly as
conftest built it and this file skips rather than silently testing fakes.
``scripts/run_tests.sh`` runs each file in its own subprocess, so the eviction
cannot reach sibling files.

Nothing here is monkeypatched *into* ``sys.modules``: the eviction runs before
the adapter import, and ``test_telegram_error_types_are_the_real_package``
asserts what actually got loaded. No network is touched — only the Bot API
error types and the adapter are real; media/File objects are mocks.
"""

import importlib.machinery
import sys

import pytest

if importlib.machinery.PathFinder.find_spec("telegram") is None:
    pytest.skip(
        "python-telegram-bot not installed", allow_module_level=True
    )

for _name in [n for n in sys.modules if n == "telegram" or n.startswith("telegram.")]:
    # A MagicMock has no ``__file__`` — mock refuses to auto-create dunders,
    # which is the same real-vs-mock probe conftest itself uses.
    if not hasattr(sys.modules[_name], "__file__"):
        del sys.modules[_name]

from unittest.mock import AsyncMock, MagicMock  # noqa: E402

import telegram  # noqa: E402
from telegram.error import BadRequest, NetworkError, TimedOut  # noqa: E402

from gateway.config import PlatformConfig  # noqa: E402
from plugins.platforms.telegram.adapter import TelegramAdapter  # noqa: E402


@pytest.fixture()
def adapter(tmp_path, monkeypatch):
    """A normally constructed adapter rooted in a temp HERMES_HOME.

    No mock_bot, no connect() — the retry helper is reached directly, so
    nothing here can open a socket.
    """
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    config = PlatformConfig(enabled=True, token="fake-token")
    a = TelegramAdapter(config)
    a.config.extra.update(
        {"media_download_attempts": 3, "media_download_retry_delay_seconds": 0}
    )
    return a


def _file_obj(data: bytes = b"media-bytes"):
    f = MagicMock()
    f.file_path = "voice/file_1.oga"
    f.download_as_bytearray = AsyncMock(return_value=bytearray(data))
    return f


def _media(get_file):
    media = MagicMock()
    media.get_file = get_file
    return media


class TestRealTelegramErrorClassification:
    """Assert retry classification against the real PTB package."""

    def test_telegram_error_types_are_the_real_package(self):
        """Fail loudly rather than pass against mocks: if conftest's MagicMock
        ever survives the eviction above, every other assertion in this file
        becomes self-fulfilling."""
        assert hasattr(telegram, "__file__")
        assert TimedOut.__module__.startswith("telegram")
        assert BadRequest.__module__.startswith("telegram")
        assert type(sys.modules["telegram.error"]).__name__ == "module"

    def test_classifier_splits_the_real_error_types(self):
        assert TelegramAdapter._looks_like_network_error(TimedOut("flake")) is True
        assert TelegramAdapter._looks_like_network_error(NetworkError("flake")) is True
        assert TelegramAdapter._looks_like_network_error(BadRequest("bad file_id")) is False


class TestRealTelegramMediaDownloadRetry:
    @pytest.mark.asyncio
    async def test_retries_real_timed_out_on_get_file_then_succeeds(self, adapter):
        """One real telegram.error.TimedOut is absorbed; the caller sees bytes."""
        good = _file_obj(b"voice-bytes")
        media = _media(AsyncMock(side_effect=[TimedOut("CDN timeout"), good]))

        file_obj, data = await adapter._download_telegram_media_bytes(media, "voice message")

        assert media.get_file.await_count == 2
        assert data == b"voice-bytes"
        assert file_obj is good

    @pytest.mark.asyncio
    async def test_retries_real_network_error_on_download_then_succeeds(self, adapter):
        """The retry wraps the whole get_file + download pair, so a flake on the
        second call re-fetches the File rather than reusing a dead handle."""
        good = _file_obj(b"photo-bytes")
        flaky = _file_obj()
        flaky.download_as_bytearray = AsyncMock(side_effect=NetworkError("connection reset"))
        media = _media(AsyncMock(side_effect=[flaky, good]))

        _file, data = await adapter._download_telegram_media_bytes(media, "photo")

        assert media.get_file.await_count == 2
        assert flaky.download_as_bytearray.await_count == 1
        assert data == b"photo-bytes"

    @pytest.mark.asyncio
    async def test_real_bad_request_fails_on_the_first_attempt(self, adapter):
        """A permanently-invalid file_id must not be retried, even though the
        real BadRequest is a NetworkError subclass."""
        media = _media(AsyncMock(side_effect=BadRequest("wrong file_id")))

        with pytest.raises(BadRequest):
            await adapter._download_telegram_media_bytes(media, "document")

        assert media.get_file.await_count == 1

    @pytest.mark.asyncio
    async def test_non_telegram_error_fails_on_the_first_attempt(self, adapter):
        """Non-transport failures are not transport flakes."""
        media = _media(AsyncMock(side_effect=RuntimeError("disk full")))

        with pytest.raises(RuntimeError):
            await adapter._download_telegram_media_bytes(media, "document")

        assert media.get_file.await_count == 1

    @pytest.mark.asyncio
    async def test_exhaustion_reraises_the_real_timed_out_unchanged(self, adapter):
        """The bound holds and the original error propagates, so the call sites'
        fail-closed notification still names TimedOut to the user."""
        media = _media(AsyncMock(side_effect=TimedOut("CDN timeout")))

        with pytest.raises(TimedOut) as excinfo:
            await adapter._download_telegram_media_bytes(media, "voice message")

        assert media.get_file.await_count == 3  # media_download_attempts
        assert "CDN timeout" in str(excinfo.value)
