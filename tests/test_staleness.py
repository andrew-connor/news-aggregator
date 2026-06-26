from datetime import datetime, timedelta, timezone

from fetcher import STALENESS_HOURS


def _is_stale(newest: datetime, now: datetime) -> bool:
    return (now - newest).total_seconds() / 3600 > STALENESS_HOURS


def test_fresh_feed_not_stale():
    now = datetime(2026, 5, 5, 12, 0, tzinfo=timezone.utc)
    assert not _is_stale(now - timedelta(hours=1), now)


def test_day_old_feed_not_stale():
    now = datetime(2026, 5, 5, 12, 0, tzinfo=timezone.utc)
    assert not _is_stale(now - timedelta(hours=24), now)


def test_feed_at_exact_threshold_not_stale():
    now = datetime(2026, 5, 5, 12, 0, tzinfo=timezone.utc)
    assert not _is_stale(now - timedelta(hours=STALENESS_HOURS), now)


def test_feed_one_minute_over_threshold_is_stale():
    now = datetime(2026, 5, 5, 12, 0, tzinfo=timezone.utc)
    assert _is_stale(now - timedelta(hours=STALENESS_HOURS, minutes=1), now)


def test_week_old_feed_is_stale():
    now = datetime(2026, 5, 5, 12, 0, tzinfo=timezone.utc)
    assert _is_stale(now - timedelta(days=7), now)


def test_staleness_threshold_is_48_hours():
    assert STALENESS_HOURS == 48
