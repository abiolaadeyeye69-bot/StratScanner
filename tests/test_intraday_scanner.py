from datetime import datetime
from zoneinfo import ZoneInfo

import intraday_scanner as isc

ET = ZoneInfo("America/New_York")


def _bar(h, m):
    return isc.Bar60(timestamp=datetime(2026, 11, 13, h, m, tzinfo=ET),
                     open=1, high=2, low=0.5, close=1.5, volume=1)


BARS = [_bar(9, 30), _bar(10, 0), _bar(10, 30)]


def _kept(h, m):
    now = datetime(2026, 11, 13, h, m, tzinfo=ET)
    return [b.timestamp.strftime("%H:%M") for b in isc.drop_forming_bars(BARS, now)]


def test_brand_new_bar_is_dropped():
    # Real incident: scan ran 10:31, the 10:30 bar was one minute old.
    assert _kept(10, 31) == ["09:30", "10:00"]


def test_on_time_run_keeps_provisional_bar():
    # :55 run: the 10:30 bar is 25 minutes in, kept (flagged provisional in the alert)
    assert _kept(10, 55) == ["09:30", "10:00", "10:30"]


def test_bar_under_20_minutes_is_dropped():
    assert _kept(10, 49) == ["09:30", "10:00"]


def test_complete_bar_is_kept():
    assert _kept(11, 2) == ["09:30", "10:00", "10:30"]


def test_alert_flags_forming_bar():
    hit = isc.PatternHit(symbol="SPY", pattern="inside_bar", direction="neutral",
                         combo="2d-1", close=600.0, bar_time=datetime(2026, 11, 13, 10, 30, tzinfo=ET), bar_type="1")
    base = {"simultaneous": [], "individual": [hit], "overview": [],
            "latest_bar": datetime(2026, 11, 13, 10, 30, tzinfo=ET)}
    forming = isc.format_alert(dict(base, scan_time=datetime(2026, 11, 13, 10, 56, tzinfo=ET)))[0]
    assert "still forming, closes 11:00" in forming
    done = isc.format_alert(dict(base, scan_time=datetime(2026, 11, 13, 11, 2, tzinfo=ET)))[0]
    assert "still forming" not in done
