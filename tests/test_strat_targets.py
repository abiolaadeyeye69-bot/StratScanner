"""Tests for strat_targets.py — the Python port of the Pine indicator
"TheStrat Magnitude + Targets (MTF)".

Every expectation here was derived by reading the Pine source, NOT by running
TradingView (no TradingView reference run was available). These tests prove
the transcription matches the Pine's written rules; they cannot prove it
matches TradingView's rendering on live data.
"""
from __future__ import annotations

import pytest

from strat_targets import (
    Bar, TargetsConfig, TargetsEngine, scan_series, tick_for,
    is_hammer, is_shooter, MIN_BARS,
)

# ---------------------------------------------------------------------------
# bar builders
# ---------------------------------------------------------------------------

def _shape(l, h, colour):
    r = h - l
    if colour == "g":
        return l + 0.3 * r, l + 0.7 * r          # o, c  (mid body, no wicks rule hit)
    if colour == "r":
        return l + 0.7 * r, l + 0.3 * r
    if colour == "H":                            # green hammer
        return l + 0.7 * r, l + 0.8 * r
    if colour == "S":                            # red shooter
        return l + 0.25 * r, l + 0.15 * r
    raise ValueError(colour)


def nxt(prev: Bar, kind: str, colour: str, label: str = "") -> Bar:
    p_h, p_l = prev.h, prev.l
    if kind == "u":
        h, l = p_h + 2, p_l + 1
    elif kind == "d":
        h, l = p_h - 1, p_l - 2
    elif kind == "o":
        h, l = p_h + 2, p_l - 2
    elif kind == "i":
        h, l = p_h - 1, p_l + 1
    elif kind == "iu":                            # inside, sitting high
        h, l = p_h - 0.5, p_l + 1.5
    elif kind == "id":                            # inside, sitting low
        h, l = p_h - 1.5, p_l + 0.5
    else:
        raise ValueError(kind)
    o, c = _shape(l, h, colour)
    return Bar(o, h, l, c, label)


def seq(specs: list[str], start=(100.0, 140.0)) -> list[Bar]:
    """specs like ["u g", "i r"]; first bar is a neutral 'x' bar."""
    l, h = start
    bars = [Bar(l + 5, h, l, h - 5, "b0")]
    for n, sp in enumerate(specs, start=1):
        kind, col = sp.split()
        bars.append(nxt(bars[-1], kind, col, f"b{n}"))
    return bars


def run(bars, **cfgkw):
    cfgkw.setdefault("enPMG", False)
    eng = TargetsEngine(TargetsConfig(**cfgkw))
    for b in bars:
        eng.step(b)
    return eng


def lv(bars, name):
    """H1/L2/... relative to the developing (last) bar."""
    side, k = name[0], int(name[1:])
    b = bars[-1 - k]
    return b.h if side == "H" else b.l


# ---------------------------------------------------------------------------
# setup chain: one crafted sequence per branch
#   (case id, specs, expected pattern, dir, trigger level, Target-1 level)
# ---------------------------------------------------------------------------

CASES = [
    ("1-3-1-2u", ["i g", "o g", "i g", "u g"],  # x0,i,o,i,u
     "1-3-1-2 Contract→Expand→Contract→Expand", 1, "H1", "H2"),
    ("1-3-1-2d", ["i g", "o g", "i g", "d r"],
     "1-3-1-2 Contract→Expand→Contract→Expand", -1, "L1", "L2"),
    ("randy-up", ["i g", "u g", "d r", "u g"],
     "Bullish Randy Jackson (3-Bar Rev-Strat)", 1, "H1", "H2"),
    ("randy-dn", ["i g", "d r", "u g", "d r"],
     "Bearish Randy Jackson (3-Bar Rev-Strat)", -1, "L1", "L2"),
    ("1-3-2u", ["u g", "i g", "o g", "u g"],
     "Bullish 2-Bar Rev-Strat (1-3-2)", 1, "H1", "H3"),
    ("1-3-2d", ["u g", "i g", "o g", "d r"],
     "Bearish 2-Bar Rev-Strat (1-3-2)", -1, "L1", "L3"),
    ("1-3-2u-hammer", ["u g", "i g", "o H", "u g"],
     "Bullish 2-Bar Rev-Strat (1-3 Hammer)", 1, "H1", "H3"),
    ("1-3-2d-shooter", ["u g", "i g", "o S", "d r"],
     "Bearish 2-Bar Rev-Strat (1-3 Shooter)", -1, "L1", "L3"),
    ("3-2u", ["u g", "u g", "o g", "u g"],
     "Bullish 3-2", 1, "H1", None),
    ("3-2d", ["u g", "u g", "o g", "d r"],
     "Bearish 3-2", -1, "L1", None),
    ("3-2u-boom", ["u g", "u g", "o H", "u g"],
     "Bullish 3-2 Boom (Hammer)", 1, "H1", None),
    ("3-2d-boom", ["u g", "u g", "o S", "d r"],
     "Bearish 3-2 Boom (Shooter)", -1, "L1", None),
    ("shotgun-up", ["u g", "u g", "d r", "u g"],
     "Bullish Shot-Gun (2u-2d-2u)", 1, "H1", "H2"),
    ("shotgun-dn", ["d r", "d r", "u g", "d r"],
     "Bearish Shot-Gun (2d-2u-2d)", -1, "L1", "L2"),
    ("3-2-2u", ["u g", "o g", "d r", "u g"],
     "3-2-2 Fun to Say, Fun to Do", 1, "H1", "H2"),
    ("3-2-2d", ["u g", "o g", "u g", "d r"],
     "3-2-2 Fun to Say, Fun to Do", -1, "L1", "L2"),
    ("3-1-2u-rev", ["u g", "o r", "i g", "u g"],
     "3-1-2 Chicago Reversal", 1, "H1", "H2"),
    ("3-1-2u-cont", ["u g", "o g", "i g", "u g"],
     "3-1-2 Chicago Continuation", 1, "H1", "H2"),
    ("3-1-2d-rev", ["d r", "o g", "i g", "d r"],
     "3-1-2 Chicago Reversal", -1, "L1", "L2"),
    ("3-1-2d-cont", ["d r", "o r", "i g", "d r"],
     "3-1-2 Chicago Continuation", -1, "L1", "L2"),
    ("2-1-2u-rev", ["u g", "d r", "i g", "u g"],
     "2-1-2 Reversal", 1, "H1", "H2"),
    ("2-1-2d-rev", ["u g", "u g", "i g", "d r"],
     "2-1-2 Reversal", -1, "L1", "L2"),
    ("2-1-2u-momo", ["u g", "u g", "iu g", "u g"],
     "2-1-2 Momentum (continuation)", 1, "H1", None),
    ("2-1-2d-momo", ["u g", "d r", "id r", "d r"],
     "2-1-2 Momentum (continuation)", -1, "L1", None),
    ("1-3u", ["u g", "u g", "i g", "o g"],
     "Bullish 1-Bar Rev-Strat", 1, "H1", "H2"),
    ("1-3d", ["u g", "u g", "i g", "o r"],
     "Bearish 1-Bar Rev-Strat", -1, "L1", "L2"),
    ("1-2d-2u", ["u g", "i g", "d r", "u g"],
     "Bullish 2-Bar Rev-Strat", 1, "H1", "H2"),
    ("1-2u-2d", ["u g", "i g", "u g", "d r"],
     "Bearish 2-Bar Rev-Strat", -1, "L1", "L2"),
    ("2-2u", ["u g", "d r", "d r", "u g"],
     "Bullish 2-2 Reversal", 1, "H1", "H2"),
    ("2-2d", ["u g", "u g", "u g", "d r"],
     "Bearish 2-2 Reversal", -1, "L1", "L2"),
    ("inside-bo-up", ["u g", "u g", "id g", "u g"],
     "Inside-Bar Breakout (1-2u)", 1, "H1", "H2"),
    ("inside-bo-dn", ["u g", "d r", "iu r", "d r"],
     "Inside-Bar Breakout (1-2d)", -1, "L1", "L2"),
    ("3u-cont", ["u g", "u g", "u g", "o g"],
     "3u Outside Continuation", 1, "H1", None),
    ("3d-cont", ["u g", "u g", "d r", "o r"],
     "3d Outside Continuation", -1, "L1", None),
    ("going3-dn", ["u g", "u g", "u g", "u r"],
     "2u→3 (going 3)", -1, "H1", "L1"),
    ("going3-up", ["u g", "d r", "d r", "d g"],
     "2d→3 (going 3)", 1, "L1", "H1"),
    ("pot-1bar-dn", ["u g", "u g", "i g", "u r"],
     "Potential 1-Bar Rev-Strat", -1, "H1", "L1"),
    ("pot-1bar-up", ["u g", "d r", "i g", "d g"],
     "Potential 1-Bar Rev-Strat", 1, "L1", "H1"),
]


@pytest.mark.parametrize("cid,specs,name,d,trig,t1", CASES, ids=[c[0] for c in CASES])
def test_setup_chain(cid, specs, name, d, trig, t1):
    """What the chain detects on the last bar. (Whether it is *adopted* also
    depends on what is already held: a same-direction continuation does not
    replace a held signal, which is tested separately below.)"""
    bars = seq(specs)
    eng = run(bars[:-1])
    sig = eng._detect(bars[-1])
    assert sig.name_l == name, cid
    assert sig.dir == d, cid
    assert sig.trig == pytest.approx(lv(bars, trig)), cid
    if t1 is not None:
        assert eng._ladder(sig, bars[-1])[0] == pytest.approx(lv(bars, t1)), cid


# reversal setups are adopted outright; check the held snapshot too
_REV_IDS = [c[0] for c in CASES
            if "cont" not in c[0] and "momo" not in c[0] and "inside-bo" not in c[0]
            and "going3" not in c[0] and "pot-1bar" not in c[0]]


@pytest.mark.parametrize("cid", _REV_IDS)
def test_reversal_setups_are_held_after_the_bar(cid):
    c = next(x for x in CASES if x[0] == cid)
    bars = seq(c[1])
    snap = run(bars).snapshot()
    assert snap is not None and snap["pat"] == c[2] and snap["dir"] == c[3]
    assert snap["fresh"] is True and snap["age"] == 0


def test_gap_sell_and_buy():
    base = seq(["u g", "u g", "u g"])
    h1 = base[-1].h
    # opens above prior high by > 1 tick, closes red
    gap_up = Bar(h1 + 1.0, h1 + 1.5, h1 - 4, h1 - 1, "gap")
    snap = run(base + [gap_up]).snapshot()
    assert snap["pat"] == "Gap-Up Sell" and snap["dir"] == -1
    assert snap["trig"] == pytest.approx(gap_up.o)       # trigger = the open
    assert snap["tg"][0][0] == pytest.approx(h1)          # T1 = prior high
    assert snap["fr"] is True

    l1 = base[-1].l
    gap_dn = Bar(l1 - 1.0, l1 + 4, l1 - 1.5, l1 + 1, "gap")
    snap = run(base + [gap_dn]).snapshot()
    assert snap["pat"] == "Gap-Down Buy" and snap["dir"] == 1
    assert snap["tg"][0][0] == pytest.approx(l1)


def test_gap_has_priority_over_everything_else():
    # a gap-up red bar that is also an outside bar -> Gap-Up Sell wins
    base = seq(["u g", "u g", "u g"])
    h1, l1 = base[-1].h, base[-1].l
    b = Bar(h1 + 1.0, h1 + 3, l1 - 3, h1 - 1, "x")
    assert run(base + [b]).snapshot()["pat"] == "Gap-Up Sell"


def test_toggle_disables_setup():
    bars = seq(["u g", "d r", "d r", "u g"])            # 2-2u
    assert run(bars).snapshot()["pat"] == "Bullish 2-2 Reversal"
    snap = run(bars, en2_2=False).snapshot()
    assert snap is None or snap["pat"] != "Bullish 2-2 Reversal"


# ---------------------------------------------------------------------------
# ladder
# ---------------------------------------------------------------------------

def test_two_bar_revstrat_has_second_anchor():
    bars = seq(["u g", "i g", "d r", "u g"])            # 1-2d-2u
    snap = run(bars).snapshot()
    # anchor = H2, anchor2 = H3 (the d bar broke below the bar-3 low)
    assert snap["tg"][0][0] == pytest.approx(lv(bars, "H2"))
    assert snap["tg"][1][0] == pytest.approx(lv(bars, "H3"))


def test_ladder_is_ascending_for_long_and_respects_limit():
    # long ladder: strictly increasing, capped at ext_targets + 1
    bars = seq(["d r", "d r", "d r", "d r", "d r", "u g"])
    snap = run(bars, ext_targets=3).snapshot()
    levels = [t[0] for t in snap["tg"]]
    assert levels == sorted(levels) and len(levels) <= 4
    assert all(a < b for a, b in zip(levels, levels[1:]))


def test_ladder_is_descending_for_short():
    bars = seq(["u g", "u g", "u g", "u g", "u g", "d r"])
    snap = run(bars).snapshot()
    levels = [t[0] for t in snap["tg"]]
    assert levels == sorted(levels, reverse=True) and len(levels) >= 2


def test_level_source_label_is_the_bar_that_printed_it():
    bars = seq(["u g", "d r", "d r", "u g"])            # 2-2u -> T1 = H2
    snap = run(bars).snapshot()
    assert snap["tg"][0][1] == bars[-3].label


# ---------------------------------------------------------------------------
# held-signal state machine — hand-crafted bars with known numbers
# ---------------------------------------------------------------------------
# b3 = Bullish Shot-Gun (long, T1 = 150), b4 = Bearish Shot-Gun (short, T1 = 95),
# b5 = Bullish Shot-Gun (long, T1 = 149). None of the three reaches its own T1,
# so every flip is a reversal that "missed target".

ALT = [
    Bar(105, 140, 100, 135, "b0"),
    Bar(110, 150, 101, 145, "b1"),
    Bar(146, 148, 95, 100, "b2"),
    Bar(100, 149, 96, 147, "b3"),
    Bar(147, 148.5, 95.5, 100, "b4"),
    Bar(100, 148.9, 96.2, 147.5, "b5"),
]


def alt_engine(upto=6, **kw):
    kw.setdefault("enPMG", False)
    eng = TargetsEngine(TargetsConfig(**kw))
    revs = []
    for b in ALT[:upto]:
        eng.step(b)
        revs.append(eng.rev_since_hit)
    return eng, revs


def test_alt_fixture_is_what_the_comments_say():
    eng, _ = alt_engine()
    snap = eng.snapshot()
    assert snap["dir"] == 1 and snap["pat"] == "Bullish Shot-Gun (2u-2d-2u)"
    assert snap["tg"][0][0] == 149.0 and snap["tg"][0][4] == "o"
    assert snap["trig"] == 148.5


def test_each_flip_without_hitting_target_adds_a_reversal():
    _, revs = alt_engine()
    assert revs[4] == revs[3] + 1          # b4 flips short
    assert revs[5] == revs[4] + 1          # b5 flips long again
    assert revs[5] >= 2


def test_chop_flag_and_reset_on_target_1_hit():
    eng, _ = alt_engine()
    assert eng.snapshot()["chop"] is True
    # b6 trades up through T1 (149) and does not itself make a new setup
    eng.step(Bar(147.5, 150, 140, 149.5, "b6"))
    snap = eng.snapshot()
    assert snap["pat"] == "Bullish Shot-Gun (2u-2d-2u)"     # still the held one
    assert snap["revs"] == 0 and snap["chop"] is False
    assert snap["tg"][0][4] == "x"                          # taken


def test_opposite_reversal_replaces_the_held_signal():
    eng, _ = alt_engine(upto=4)             # long held after b3
    assert eng.snapshot()["dir"] == 1
    eng.step(ALT[4])
    snap = eng.snapshot()
    assert snap["dir"] == -1 and snap["pat"] == "Bearish Shot-Gun (2d-2u-2d)"
    assert snap["tg"][0][0] == 95.0


def test_taken_then_reclaimed_markers():
    eng, _ = alt_engine()
    eng.step(Bar(147.5, 150, 140, 149.5, "b6"))        # closes above T1 -> taken
    assert eng.snapshot()["tg"][0][4] == "x"
    eng.step(Bar(149.0, 150.0, 147.2, 148.0, "b7"))    # inside b6, red, back below T1
    snap = eng.snapshot()
    assert snap["tg"][0][4] == "r"                      # reclaimed
    assert snap["st"] == "RETRACEMENT"                  # inside + red against a long
    assert snap["pat"] == "Bullish Shot-Gun (2u-2d-2u)"  # still held


def test_open_target_distance_pct_togo_and_profit():
    eng, _ = alt_engine()
    snap = eng.snapshot()
    lvl, src, dist, pct, st = snap["tg"][0]
    assert (lvl, st, src) == (149.0, "o", "b3")
    assert dist == pytest.approx(1.5)
    assert pct == pytest.approx(1.5 / 147.5 * 100, rel=1e-3)
    assert snap["togo"] == pytest.approx(1.5)
    assert snap["pnl"] == pytest.approx(147.5 - 148.5)   # close minus trigger
    assert snap["pnlp"] == pytest.approx((147.5 - 148.5) / 148.5 * 100, rel=1e-3)
    assert snap["st"] == "IN-FORCE"                      # signal fired this bar


def test_signal_persists_and_goes_still_good():
    eng, _ = alt_engine()
    # inside bar closing green: no setup, no counter-trend -> STILL GOOD
    eng.step(Bar(100, 148.5, 97, 147, "b6"))
    snap = eng.snapshot()
    assert snap["pat"] == "Bullish Shot-Gun (2u-2d-2u)"
    assert snap["age"] == 1 and snap["fresh"] is False
    assert snap["st"] == "STILL GOOD"


def test_taking_the_prior_low_flags_potential_3_with_3_trigger_and_continuity():
    eng, _ = alt_engine()
    # 2d bar that closes green: weak long (same direction) -> not re-adopted;
    # the prior low is taken so the long is at risk.
    eng.step(Bar(100, 148, 95.5, 147, "b6"))
    snap = eng.snapshot()
    assert snap["dir"] == 1 and snap["age"] == 1
    assert snap["st"] == "POTENTIAL 3"
    assert snap["p3t"] == pytest.approx(96.2)            # prior bar's low
    assert snap["cont"] == pytest.approx(100.0)          # the live bar's open


def test_going3_does_not_override_opposite_held_signal():
    eng, _ = alt_engine(upto=5)                          # short held after b4
    assert eng.snapshot()["dir"] == -1
    # 2d green vs held short: weak long (going-3) must not replace it
    eng.step(Bar(100, 148, 94.5, 147, "b5x"))
    snap = eng.snapshot()
    assert snap["dir"] == -1 and snap["pat"] == "Bearish Shot-Gun (2d-2u-2d)"
    assert snap["st"] == "POTENTIAL 3"                   # but flagged at risk


def test_weak_signal_is_adopted_when_nothing_is_held_then_negated():
    eng, _ = alt_engine()
    eng._clear()                                         # nothing held
    eng.step(Bar(100, 148, 95.5, 147, "w"))              # 2d green -> weak long
    snap = eng.snapshot()
    assert snap["wk"] is True and snap["dir"] == 1
    assert snap["pat"] == "2d→3 (going 3)"
    # inside bar that closes red: not a breakout, so the unconfirmed signal is dropped
    eng.step(Bar(146, 147.5, 97, 100, "n"))
    assert eng.snapshot() is None


def test_negation_is_off_when_invalidate_disabled():
    eng, _ = alt_engine(invalidate=False)
    eng._clear()
    eng.step(Bar(100, 148, 95.5, 147, "w"))
    eng.step(Bar(146, 147.5, 97, 100, "n"))
    assert eng.snapshot() is not None


def test_continuation_does_not_replace_held_levels():
    eng, _ = alt_engine()
    before = eng.snapshot()
    # 3u outside continuation in the same direction as the held long
    eng.step(Bar(100, 150, 95, 149.5, "c"))
    after = eng.snapshot()
    assert after["trig"] == before["trig"] and after["pat"] == before["pat"]


def test_adopted_reversal_in_same_direction_replaces_levels():
    # a later *reversal* setup in the same direction as the held one re-adopts
    eng, _ = alt_engine(upto=4)                          # long (Shot-Gun) after b3
    held = eng.snapshot()["pat"]
    eng.step(ALT[4])                                     # flips short
    eng.step(ALT[5])                                     # flips long again: new Shot-Gun
    snap = eng.snapshot()
    assert snap["dir"] == 1 and snap["age"] == 0 and snap["sd"] == "b5"
    assert held == snap["pat"]


# ---------------------------------------------------------------------------
# PMG
# ---------------------------------------------------------------------------

def test_pmg_prefix_on_going3_after_descending_highs():
    bars = seq(["d r"] * 5 + ["d g"])                   # 5 lower highs then 2d green
    eng = run(bars[:-1], enPMG=True)
    sig = eng._detect(bars[-1])
    assert sig.name_l.startswith("PMG + ") and sig.name_s.startswith("PMG+")
    assert sig.dir == 1 and sig.weak is True


def test_pmg_never_prefixes_a_2u_reversal_because_streak_starts_at_the_forming_bar():
    # Observation about the Pine AS WRITTEN: the streak is measured from the
    # developing bar's high. A 2u bar has high >= prior high + 1 tick, so the
    # first comparison fails and the streak is 0 -> no PMG prefix on 2u setups.
    bars = seq(["d r"] * 5 + ["u g"])
    eng = run(bars[:-1], enPMG=True)
    assert not eng._detect(bars[-1]).name_l.startswith("PMG")


def test_pmg_toggle_off():
    bars = seq(["d r"] * 5 + ["d g"])
    eng = run(bars[:-1], enPMG=False)
    assert not eng._detect(bars[-1]).name_l.startswith("PMG")


# ---------------------------------------------------------------------------
# misc
# ---------------------------------------------------------------------------

def test_one_tick_break_is_not_lost_to_float_noise():
    # 100.01 - 100.00 is 0.00999999999999801 in float arithmetic
    tick = 0.01
    prev = Bar(99.5, 100.00, 99.0, 99.8, "p")
    cur = Bar(99.9, 100.01, 99.2, 100.0, "c")
    eng = TargetsEngine(TargetsConfig(tick=tick, enPMG=False))
    for b in [Bar(95, 101, 94, 99, "a"), Bar(96, 101, 95, 99, "b"),
              Bar(97, 100.5, 96, 99, "c0"), prev]:
        eng.step(b)
    assert eng._classify(cur)["bh0"] is True


def test_short_series_is_skipped():
    assert scan_series(seq(["u g", "d r"]), TargetsConfig()) is None
    assert MIN_BARS >= 4


def test_history_window_is_capped_like_pine():
    eng = TargetsEngine(TargetsConfig(enPMG=False))
    for i in range(400):
        eng.step(Bar(100 + i, 101 + i, 99 + i, 100.5 + i, str(i)))
    assert len(eng.hist) == 250


def test_tick_for():
    assert tick_for(150.0, crypto=False) == 0.01
    assert tick_for(0.5, crypto=False) == 0.0001
    assert tick_for(67000.0, crypto=True) == 0.01
    assert tick_for(150.0, crypto=True) == pytest.approx(0.01)
    assert tick_for(0.00001234, crypto=True) < 1e-8 * 2
    assert tick_for(float("nan"), crypto=True) == 0.01


def test_hammer_and_shooter_rules():
    assert is_hammer(2.0, 10.0, 0.0, 3.0) is False      # lower wick too short
    assert is_hammer(7.0, 10.0, 0.0, 8.0) is True
    assert is_shooter(2.0, 10.0, 0.0, 1.0) is True
    assert is_shooter(0.0, 0.0, 0.0, 0.0) is False       # zero range


def test_partial_flag_and_unconfirmed_bar_skips_chop():
    cfg = TargetsConfig(enPMG=False)
    flip = Bar(147, 148.5, 95.5, 100, "last")          # b4-style flip on the last bar
    base = [Bar(100, 138, 98, 130, "p")] + ALT[:4] + [flip]
    confirmed = scan_series(base, cfg, last_confirmed=True)
    unconfirmed = scan_series(base, cfg, last_confirmed=False)
    assert unconfirmed["partial"] is True and confirmed["partial"] is False
    # the flip on an unconfirmed bar is not counted by the chop logic
    assert confirmed["revs"] == unconfirmed["revs"] + 1
