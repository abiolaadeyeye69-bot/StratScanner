from datetime import datetime
from zoneinfo import ZoneInfo

import intraday_scanner as isc

ET = ZoneInfo("America/New_York")


def _bar(h, m):
    return isc.Bar60(timestamp=datetime(2026, 11, 13, h, m, tzinfo=ET),
                     open=1, high=2, low=0.5, close=1.5, volume=1)


BARS = [_bar(9, 30), _bar(10, 0), _bar(10, 30)]


def test_brand_new_bar_is_dropped():
    # Real incident: scan ran 10:31:00, the 10:30 bar was one minute old.
    now = datetime(2026, 11, 13, 10, 31, tzinfo=ET)
    kept = isc.drop_forming_bars(BARS, now)
    assert [b.timestamp.strftime("%H:%M") for b in kept] == ["09:30", "10:00"]


def test_bar_four_minutes_from_close_is_dropped():
    now = datetime(2026, 11, 13, 10, 56, tzinfo=ET)
    kept = isc.drop_forming_bars(BARS, now)
    assert kept[-1].timestamp.strftime("%H:%M") == "10:00"
    assert all(b.timestamp.strftime("%H:%M") != "10:30" for b in kept)


def test_bar_complete_after_settle_is_kept():
    now = datetime(2026, 11, 13, 11, 2, tzinfo=ET)
    kept = isc.drop_forming_bars(BARS, now)
    assert [b.timestamp.strftime("%H:%M") for b in kept] == ["09:30", "10:00", "10:30"]


def test_exactly_at_close_waits_for_settle():
    now = datetime(2026, 11, 13, 11, 0, tzinfo=ET)
    assert all(b.timestamp.strftime("%H:%M") != "10:30" for b in isc.drop_forming_bars(BARS, now))
