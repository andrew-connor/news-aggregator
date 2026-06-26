from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest

from fetcher import ALERT_DEDUP_HOURS, _should_create_alert


def test_dedup_window_is_24_hours():
    assert ALERT_DEDUP_HOURS == 24


def test_alert_within_window_is_within_cutoff():
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=ALERT_DEDUP_HOURS)
    alert_time = now - timedelta(hours=1)
    assert alert_time > cutoff


def test_alert_outside_window_is_past_cutoff():
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=ALERT_DEDUP_HOURS)
    alert_time = now - timedelta(hours=ALERT_DEDUP_HOURS + 1)
    assert alert_time < cutoff


async def test_should_not_create_when_recent_unhidden_alert_exists():
    """Returns False when a matching unhidden alert exists within the dedup window."""

    async def mock_stream():
        yield MagicMock()  # one matching doc — dedup fires

    mock_query = MagicMock()
    mock_query.stream = mock_stream
    mock_query.where = MagicMock(return_value=mock_query)

    mock_db = MagicMock()
    mock_db.collection = MagicMock(return_value=mock_query)

    result = await _should_create_alert(mock_db, "NPR News", "fetch_failed")
    assert result is False


async def test_should_create_when_no_recent_alert_exists():
    """Returns True when no matching alert exists."""

    async def mock_stream():
        return
        yield  # empty async generator

    mock_query = MagicMock()
    mock_query.stream = mock_stream
    mock_query.where = MagicMock(return_value=mock_query)

    mock_db = MagicMock()
    mock_db.collection = MagicMock(return_value=mock_query)

    result = await _should_create_alert(mock_db, "NPR News", "fetch_failed")
    assert result is True
