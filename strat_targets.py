"""
Strat Targets engine — a Python port of the Pine indicator
"TheStrat Magnitude + Targets (MTF)".

What the indicator does
-----------------------
On every bar of the signal timeframe it classifies the developing bar against
the previous three completed bars (inside / 2u / 2d / outside, colour), runs the
setup dictionary (first match wins), and when a setup is adopted it projects a
ladder of targets:

    TRIGGER  = the level the signal bar broke
    TARGET 1 = the reclaim level (anchor)
    TARGET 2.. = the next prior highs/lows NOT yet taken out

The MOST RECENT signal is held until a new one replaces it or it is negated.
While held, each target is tracked as open / taken / reclaimed and the
position is given a status: IN-FORCE, STILL GOOD, RETRACEMENT or POTENTIAL 3.
A chop counter tracks back-to-back reversals that never reached Target 1.

Why this is a replay and not a one-bar lookup
---------------------------------------------
The "held signal" is state carried from bar to bar (Pine `var` variables), so
the only faithful way to know what is held *today* is to replay the bars in
order. `TargetsEngine.step()` is one Pine bar. The history window the Pine
keeps (250 completed bars) is reproduced.

Fidelity notes (what is and is not identical to the Pine)
---------------------------------------------------------
* Setup chain, anchors, ladder, adopt/keep/negate rules, status and chop
  logic are transcribed branch for branch.
* Chart timeframe == signal timeframe is assumed: each bar is evaluated once,
  at its final value. TradingView re-evaluates the live bar tick by tick; the
  scanner sees the same state TradingView shows right after that bar closes
  (or, for an in-progress W/M bar, a snapshot of it).
* Pine's na-arithmetic is reproduced: any comparison involving a missing
  history bar is false (so `in1` is TRUE when fewer than 2 bars exist). The
  scanner skips series shorter than MIN_BARS so this never reaches output.
* "Break by one tick" compares with a tiny tolerance (1e-6 of a tick) so that
  float noise (100.01 - 100.00 = 0.00999999999999) cannot flip a one-tick
  break. Pine has the same float arithmetic but no tolerance; this only
  matters on exact one-tick edges and errs toward the stated intent.
* `syminfo.mintick` has no Yahoo equivalent; `tick_for()` approximates it.
* Chop counting (Pine: only on confirmed bars) is skipped for an
  in-progress bar via `confirmed=False`.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Optional

# Pine keeps at most this many completed bars in its arrays.
MAX_HIST = 250
# Series shorter than this are not scanned (Pine would be reading na).
MIN_BARS = 6
_EPS = 1e-6


# =========================================================================
# DATA TYPES
# =========================================================================

@dataclass
class Bar:
    o: float
    h: float
    l: float
    c: float
    label: str = ""          # printed on target levels (date / timestamp)


@dataclass
class TargetsConfig:
    """Mirror of the Pine inputs that affect logic (display inputs omitted)."""
    tick: float = 0.01
    ext_targets: int = 5           # targets beyond Target 1
    invalidate: bool = True
    chop_thr: int = 2
    pmg_bars: int = 5
    # Setup Dictionary toggles (names follow the Pine inputs)
    en2Bar: bool = True
    en2_2: bool = True
    en1_3: bool = True
    en1_3_2: bool = True
    en3_2: bool = True
    enShotgun: bool = True
    enRandy: bool = True
    en3_2_2: bool = True
    en3_1_2: bool = True
    en2_1_2rev: bool = True
    en2_1_2momo: bool = True
    en1_3_1_2: bool = True
    enGap: bool = True
    enInsideBO: bool = True
    enOut3: bool = True
    enGoing3: bool = True
    enPMG: bool = True

    @property
    def max_levels(self) -> int:
        return self.ext_targets + 1


@dataclass
class Signal:
    """What the setup chain found on the developing bar."""
    dir: int = 0
    trig: Optional[float] = None
    anchor: Optional[float] = None
    anchor2: Optional[float] = None
    weak: bool = False
    fragile: bool = False
    is_rev: bool = False
    pizazz: bool = False
    name_l: str = "—"
    name_s: str = "—"


# =========================================================================
# HELPERS
# =========================================================================

def tick_for(price: float, crypto: bool) -> float:
    """Approximate TradingView's syminfo.mintick from the last close.

    US stocks: $0.01 (0.0001 below $1, the SEC sub-dollar increment).
    Crypto: no single rule exists (it varies by exchange); use ~5 significant
    digits, capped at 0.01. The tick only decides whether two levels that are
    nearly equal count as a break, so this is a low-impact approximation.
    """
    if not (price and price > 0) or math.isnan(price):
        return 0.01
    if not crypto:
        return 0.0001 if price < 1.0 else 0.01
    return min(0.01, 10 ** (math.floor(math.log10(price)) - 4))


def _ge(a: Optional[float], b: Optional[float], thr: float) -> bool:
    """Pine `a - b >= thr`, false when either side is na."""
    if a is None or b is None:
        return False
    return (a - b) >= thr - thr * _EPS


def is_hammer(o: float, h: float, l: float, c: float) -> bool:
    rng = h - l
    return rng > 0 and min(o, c) - l >= 0.5 * rng and abs(c - o) <= 0.4 * rng


def is_shooter(o: float, h: float, l: float, c: float) -> bool:
    rng = h - l
    return rng > 0 and h - max(o, c) >= 0.5 * rng and abs(c - o) <= 0.4 * rng


def _r(x: Optional[float]) -> Optional[float]:
    """Round to 8 significant digits (keeps cents on 5-digit prices and
    sub-penny crypto intact)."""
    if x is None or (isinstance(x, float) and (math.isnan(x) or math.isinf(x))):
        return None
    return float(f"{x:.8g}")


# =========================================================================
# ENGINE
# =========================================================================

class TargetsEngine:
    """One instance == one (symbol, timeframe) Pine run. Feed bars oldest
    first with step(); read the held signal with snapshot()."""

    def __init__(self, cfg: TargetsConfig):
        self.cfg = cfg
        self.hist: list[Bar] = []        # completed bars (Pine arrH/arrL/...)
        self.bar_index = -1

        # most-recent-signal state (Pine `var mr*`)
        self.mr_dir = 0
        self.mr_trig: Optional[float] = None
        self.mr_patt = "—"
        self.mr_name_s = "—"
        self.mr_pizazz = False
        self.mr_bar = 0
        self.mr_bar_label = ""
        self.mr_max_h: Optional[float] = None
        self.mr_min_l: Optional[float] = None
        self.mr_max_c: Optional[float] = None
        self.mr_min_c: Optional[float] = None
        self.mr_weak = False
        self.mr_fragile = False
        self.mr_is_rev = False           # extra (not in Pine): for dashboard filters
        self.mr_levels: list[float] = []
        self.mr_lev_src: list[str] = []

        # chop tracking
        self.cur_sig_dir = 0
        self.mr_hit = False
        self.rev_since_hit = 0

        # per-bar results, read by snapshot()
        self.cur: Optional[Bar] = None
        self.sig_dir_now = 0
        self.pos_stat = "—"
        self.adopted_now = False
        self.confirmed = True

    # ------------------------------------------------------------------
    # classification + setup chain
    # ------------------------------------------------------------------
    def _classify(self, cur: Bar) -> dict:
        thr = self.cfg.tick
        hs = self.hist
        n = len(hs)

        def H(k):  # k-th completed bar back (1 = newest)
            return hs[n - k].h if n >= k else None

        def L(k):
            return hs[n - k].l if n >= k else None

        H1, H2, H3, H4 = H(1), H(2), H(3), H(4)
        L1, L2, L3, L4 = L(1), L(2), L(3), L(4)
        O1 = hs[n - 1].o if n >= 1 else None
        C1 = hs[n - 1].c if n >= 1 else None
        O2 = hs[n - 2].o if n >= 2 else None
        C2 = hs[n - 2].c if n >= 2 else None

        bh0 = _ge(cur.h, H1, thr)
        bl0 = _ge(L1, cur.l, thr)
        bh1 = _ge(H1, H2, thr)
        bl1 = _ge(L2, L1, thr)
        bh2 = _ge(H2, H3, thr)
        bl2 = _ge(L3, L2, thr)
        bh3 = H4 is not None and _ge(H3, H4, thr)
        bl3 = L4 is not None and _ge(L4, L3, thr)

        k = dict(
            n=n, H1=H1, H2=H2, H3=H3, L1=L1, L2=L2, L3=L3,
            bh0=bh0, bl0=bl0,
            in1=(not bh1 and not bl1),
            u1=(bh1 and not bl1),
            d1=(bl1 and not bh1),
            out1=(bh1 and bl1),
            in2=(not bh2 and not bl2),
            u2=(bh2 and not bl2),
            d2=(bl2 and not bh2),
            out2=(bh2 and bl2),
            in3=(n >= 4 and not bh3 and not bl3),
            u0=(bh0 and not bl0),
            d0=(bl0 and not bh0),
            in0=(not bh0 and not bl0),
            out0=(bh0 and bl0),
            g0=cur.c > cur.o,
            r0=cur.c < cur.o,
            g2=(C2 is not None and O2 is not None and C2 > O2),
            r2=(C2 is not None and O2 is not None and C2 < O2),
            ham1=(O1 is not None and is_hammer(O1, H1, L1, C1)),
            sho1=(O1 is not None and is_shooter(O1, H1, L1, C1)),
            gapUp=(H1 is not None and cur.o > H1 + thr),
            gapDn=(L1 is not None and cur.o < L1 - thr),
        )
        k["priorTag"] = ("2d" if k["d1"] else "2u" if k["u1"]
                         else "1" if k["in1"] else "x")
        return k

    def _pmg_runs(self, cur: Bar) -> tuple[int, int]:
        """Pine PMG streaks: consecutive lower highs / higher lows, newest
        completed bar backwards, starting from the developing bar."""
        thr = self.cfg.tick
        lh, prev = 0, cur.h
        for b in reversed(self.hist):
            if b.h > prev + thr:
                lh += 1
                prev = b.h
            else:
                break
        hl, prev = 0, cur.l
        for b in reversed(self.hist):
            if b.l < prev - thr:
                hl += 1
                prev = b.l
            else:
                break
        return lh, hl

    def _detect(self, cur: Bar) -> Signal:
        """The Pine `if enGap ... else if ...` chain, in the same order."""
        c = self.cfg
        k = self._classify(cur)
        thr = c.tick
        H1, H2, H3 = k["H1"], k["H2"], k["H3"]
        L1, L2, L3 = k["L1"], k["L2"], k["L3"]
        in0, in1, in2, in3 = k["in0"], k["in1"], k["in2"], k["in3"]
        u0, d0, out0 = k["u0"], k["d0"], k["out0"]
        u1, d1, out1 = k["u1"], k["d1"], k["out1"]
        u2, d2, out2 = k["u2"], k["d2"], k["out2"]
        g0, r0, g2, r2 = k["g0"], k["r0"], k["g2"], k["r2"]
        ham1, sho1 = k["ham1"], k["sho1"]
        s = Signal()

        def S(dir_, trig, anchor=None, anchor2=None, weak=False,
              fragile=False, rev=False, pz=False, nl="—", ns="—"):
            s.dir, s.trig, s.anchor, s.anchor2 = dir_, trig, anchor, anchor2
            s.weak, s.fragile, s.is_rev, s.pizazz = weak, fragile, rev, pz
            s.name_l, s.name_s = nl, ns

        if c.enGap and k["gapUp"] and r0:
            S(-1, cur.o, H1, rev=True, fragile=True,
              nl="Gap-Up Sell", ns="GAP-Sell")
        elif c.enGap and k["gapDn"] and g0:
            S(1, cur.o, L1, rev=True, fragile=True,
              nl="Gap-Down Buy", ns="GAP-Buy")
        elif c.en1_3_1_2 and in3 and out2 and in1 and u0 and g0:
            S(1, H1, H2, rev=True,
              nl="1-3-1-2 Contract→Expand→Contract→Expand", ns="1-3-1-2u")
        elif c.en1_3_1_2 and in3 and out2 and in1 and d0 and r0:
            S(-1, L1, L2, rev=True,
              nl="1-3-1-2 Contract→Expand→Contract→Expand", ns="1-3-1-2d")
        elif c.enRandy and in3 and u2 and d1 and u0 and g0:
            S(1, H1, H2, rev=True, pz=True,
              nl="Bullish Randy Jackson (3-Bar Rev-Strat)", ns="1-2-2-2u")
        elif c.enRandy and in3 and d2 and u1 and d0 and r0:
            S(-1, L1, L2, rev=True, pz=True,
              nl="Bearish Randy Jackson (3-Bar Rev-Strat)", ns="1-2-2-2d")
        elif c.en1_3_2 and in2 and out1 and u0 and g0:
            S(1, H1, H3, rev=True, pz=True,
              nl=("Bullish 2-Bar Rev-Strat (1-3 Hammer)" if ham1
                  else "Bullish 2-Bar Rev-Strat (1-3-2)"), ns="1-3-2u")
        elif c.en1_3_2 and in2 and out1 and d0 and r0:
            S(-1, L1, L3, rev=True, pz=True,
              nl=("Bearish 2-Bar Rev-Strat (1-3 Shooter)" if sho1
                  else "Bearish 2-Bar Rev-Strat (1-3-2)"), ns="1-3-2d")
        elif c.en3_2 and out1 and u0 and g0:
            S(1, H1, rev=True, pz=ham1,
              nl=("Bullish 3-2 Boom (Hammer)" if ham1 else "Bullish 3-2"),
              ns="3-2u")
        elif c.en3_2 and out1 and d0 and r0:
            S(-1, L1, rev=True, pz=sho1,
              nl=("Bearish 3-2 Boom (Shooter)" if sho1 else "Bearish 3-2"),
              ns="3-2d")
        elif c.enShotgun and u2 and d1 and u0 and g0:
            S(1, H1, H2, rev=True, pz=True,
              nl="Bullish Shot-Gun (2u-2d-2u)", ns="2-2-2u")
        elif c.enShotgun and d2 and u1 and d0 and r0:
            S(-1, L1, L2, rev=True, pz=True,
              nl="Bearish Shot-Gun (2d-2u-2d)", ns="2-2-2d")
        elif c.en3_2_2 and out2 and d1 and u0 and g0:
            S(1, H1, H2, rev=True,
              nl="3-2-2 Fun to Say, Fun to Do", ns="3-2-2u")
        elif c.en3_2_2 and out2 and u1 and d0 and r0:
            S(-1, L1, L2, rev=True,
              nl="3-2-2 Fun to Say, Fun to Do", ns="3-2-2d")
        elif c.en3_1_2 and out2 and in1 and u0 and g0:
            S(1, H1, H2, rev=r2,
              nl=("3-1-2 Chicago Reversal" if r2
                  else "3-1-2 Chicago Continuation"), ns="3-1-2u")
        elif c.en3_1_2 and out2 and in1 and d0 and r0:
            S(-1, L1, L2, rev=g2,
              nl=("3-1-2 Chicago Reversal" if g2
                  else "3-1-2 Chicago Continuation"), ns="3-1-2d")
        elif c.en2_1_2rev and d2 and in1 and u0 and g0:
            S(1, H1, H2, rev=True, nl="2-1-2 Reversal", ns="2-1-2u")
        elif c.en2_1_2rev and u2 and in1 and d0 and r0:
            S(-1, L1, L2, rev=True, nl="2-1-2 Reversal", ns="2-1-2d")
        elif (c.en2_1_2momo and u2 and in1 and u0 and g0
              and (H1 + L1) / 2 >= (H2 + L2) / 2):
            S(1, H1, rev=False,
              nl="2-1-2 Momentum (continuation)", ns="2-1-2u")
        elif (c.en2_1_2momo and d2 and in1 and d0 and r0
              and (H1 + L1) / 2 <= (H2 + L2) / 2):
            S(-1, L1, rev=False,
              nl="2-1-2 Momentum (continuation)", ns="2-1-2d")
        elif c.en1_3 and in1 and out0 and g0:
            S(1, H1, H2, rev=True, pz=True,
              nl="Bullish 1-Bar Rev-Strat", ns="1-3u")
        elif c.en1_3 and in1 and out0 and r0:
            S(-1, L1, L2, rev=True, pz=True,
              nl="Bearish 1-Bar Rev-Strat", ns="1-3d")
        elif c.en2Bar and in2 and d1 and u0 and g0:
            a2 = H3 if (H3 is not None and L1 is not None and L3 is not None
                        and L1 < L3 - thr) else None
            S(1, H1, H2, a2, rev=True, pz=True,
              nl="Bullish 2-Bar Rev-Strat", ns="1-2d-2u")
        elif c.en2Bar and in2 and u1 and d0 and r0:
            a2 = L3 if (L3 is not None and H1 is not None and H3 is not None
                        and H1 > H3 + thr) else None
            S(-1, L1, L2, a2, rev=True, pz=True,
              nl="Bearish 2-Bar Rev-Strat", ns="1-2u-2d")
        elif c.en2_2 and d1 and u0 and g0:
            S(1, H1, H2, rev=True, nl="Bullish 2-2 Reversal", ns="2-2u")
        elif c.en2_2 and u1 and d0 and r0:
            S(-1, L1, L2, rev=True, nl="Bearish 2-2 Reversal", ns="2-2d")
        elif c.enInsideBO and in1 and u0 and g0:
            S(1, H1, H2, rev=False,
              nl="Inside-Bar Breakout (1-2u)", ns="1-2u")
        elif c.enInsideBO and in1 and d0 and r0:
            S(-1, L1, L2, rev=False,
              nl="Inside-Bar Breakout (1-2d)", ns="1-2d")
        elif c.enOut3 and out0 and g0:
            S(1, H1, rev=False,
              nl="3u Outside Continuation", ns=k["priorTag"] + "-3u")
        elif c.enOut3 and out0 and r0:
            S(-1, L1, rev=False,
              nl="3d Outside Continuation", ns=k["priorTag"] + "-3d")
        elif c.enGoing3 and u0 and r0:
            S(-1, H1, L1, weak=True, rev=True,
              nl=("Potential 1-Bar Rev-Strat" if in1 else "2u→3 (going 3)"),
              ns=("1-2u(r)" if in1 else "2u›3"))
        elif c.enGoing3 and d0 and g0:
            S(1, L1, H1, weak=True, rev=True,
              nl=("Potential 1-Bar Rev-Strat" if in1 else "2d→3 (going 3)"),
              ns=("1-2d(g)" if in1 else "2d›3"))

        # PMG context prefix
        if s.dir != 0 and s.is_rev and c.enPMG:
            lh, hl = self._pmg_runs(cur)
            if (s.dir == 1 and lh >= c.pmg_bars) or (s.dir == -1 and hl >= c.pmg_bars):
                s.name_l = "PMG + " + s.name_l
                s.name_s = "PMG+" + s.name_s
        return s

    def _ladder(self, s: Signal, cur: Bar) -> list[float]:
        """Anchors first, then prior extremes not yet taken out, nearest
        first, up to ext_targets + 1 levels."""
        thr = self.cfg.tick
        lad: list[float] = []
        if s.dir == 1:
            if s.anchor is not None:
                lad.append(s.anchor)
            if s.anchor2 is not None:
                lad.append(s.anchor2)
            if s.anchor2 is not None:
                run = s.anchor2
            elif s.anchor is not None:
                run = s.anchor
            elif s.trig is not None and not s.weak:
                run = s.trig
            else:
                run = cur.h
            j = len(self.hist) - 1
            while j >= 0 and len(lad) < self.cfg.max_levels:
                hv = self.hist[j].h
                if hv > run + thr:
                    lad.append(hv)
                    run = hv
                j -= 1
        elif s.dir == -1:
            if s.anchor is not None:
                lad.append(s.anchor)
            if s.anchor2 is not None:
                lad.append(s.anchor2)
            if s.anchor2 is not None:
                run = s.anchor2
            elif s.anchor is not None:
                run = s.anchor
            elif s.trig is not None and not s.weak:
                run = s.trig
            else:
                run = cur.l
            k = len(self.hist) - 1
            while k >= 0 and len(lad) < self.cfg.max_levels:
                lv = self.hist[k].l
                if lv < run - thr:
                    lad.append(lv)
                    run = lv
                k -= 1
        return lad

    def _level_src(self, lvl: float) -> str:
        """Pine f_levelBar: newest-first search for the completed bar whose
        high (checked first) or low equals the level."""
        for b in reversed(self.hist):
            if b.h == lvl or b.l == lvl:
                return b.label
        return self.mr_bar_label

    def _clear(self):
        self.mr_dir = 0
        self.mr_trig = None
        self.mr_patt = "—"
        self.mr_name_s = "—"
        self.mr_pizazz = False
        self.mr_fragile = False
        self.mr_levels = []
        self.mr_lev_src = []

    # ------------------------------------------------------------------
    # one Pine bar
    # ------------------------------------------------------------------
    def step(self, cur: Bar, confirmed: bool = True) -> None:
        self.bar_index += 1
        i = self.bar_index
        c = self.cfg
        self.cur = cur
        self.confirmed = confirmed
        k = self._classify(cur)
        u0, d0, out0, in0 = k["u0"], k["d0"], k["out0"], k["in0"]
        g0, r0 = k["g0"], k["r0"]

        s = self._detect(cur)
        sig_dir = s.dir
        self.sig_dir_now = sig_dir

        # negation of the held signal by the forming bar
        negated = False
        if self.mr_dir == 1:
            negated = (self.mr_weak or self.mr_fragile) and r0 and not u0
        elif self.mr_dir == -1:
            negated = (self.mr_weak or self.mr_fragile) and g0 and not d0

        # a lone going-3 single does not override a still-good opposite entry
        is_weak = s.weak
        opposes_held = self.mr_dir != 0 and sig_dir != 0 and sig_dir != self.mr_dir
        keep_held = is_weak and opposes_held and not negated
        same_bar = self.mr_dir != 0 and self.mr_bar == i
        relabel = same_bar and sig_dir != 0 and s.name_l != self.mr_patt
        adopt = (sig_dir != 0 and not keep_held
                 and (self.mr_dir == 0 or sig_dir != self.mr_dir or relabel
                      or (s.is_rev and not s.weak and self.mr_bar != i)))
        self.adopted_now = adopt

        if adopt:
            self.mr_dir = sig_dir
            self.mr_trig = s.trig
            self.mr_patt = s.name_l
            self.mr_name_s = s.name_s
            self.mr_pizazz = s.pizazz
            self.mr_bar = i
            self.mr_bar_label = cur.label
            self.mr_max_h = cur.h
            self.mr_min_l = cur.l
            self.mr_max_c = cur.c
            self.mr_min_c = cur.c
            self.mr_weak = is_weak
            self.mr_fragile = s.fragile
            self.mr_is_rev = s.is_rev
            self.mr_levels = self._ladder(s, cur)
            self.mr_lev_src = [self._level_src(v) for v in self.mr_levels]
        elif same_bar and out0 and sig_dir == 0:
            self._clear()
        elif sig_dir == 0 and c.invalidate and negated:
            self._clear()

        # running extremes since adoption (taken-out detection)
        if self.mr_dir != 0:
            self.mr_max_h = cur.h if self.mr_max_h is None else max(self.mr_max_h, cur.h)
            self.mr_min_l = cur.l if self.mr_min_l is None else min(self.mr_min_l, cur.l)
            self.mr_max_c = cur.c if self.mr_max_c is None else max(self.mr_max_c, cur.c)
            self.mr_min_c = cur.c if self.mr_min_c is None else min(self.mr_min_c, cur.c)

        # chop tracking (Pine: only on confirmed bars)
        t1c = self.mr_levels[0] if self.mr_levels else None
        if confirmed:
            if adopt and sig_dir != self.cur_sig_dir:
                self.rev_since_hit += 1
                self.cur_sig_dir = sig_dir
                self.mr_hit = False
            if self.mr_dir == 0:
                self.cur_sig_dir = 0
            if (self.mr_dir != 0 and t1c is not None and not self.mr_hit
                    and ((self.mr_dir == 1 and cur.h >= t1c)
                         or (self.mr_dir == -1 and cur.l <= t1c))):
                self.mr_hit = True
                self.rev_since_hit = 0

        # position health
        counter_inside = (in0 and r0) if self.mr_dir == 1 else \
                         (in0 and g0) if self.mr_dir == -1 else False
        at_risk = self.mr_dir != 0 and (
            (d0 or (u0 and r0)) if self.mr_dir == 1 else (u0 or (d0 and g0)))
        stat = "—"
        if self.mr_dir != 0:
            if at_risk:
                stat = "POTENTIAL 3"
            elif sig_dir != 0 and sig_dir == self.mr_dir:
                stat = "POTENTIAL 3" if self.mr_weak else "IN-FORCE"
            elif counter_inside:
                stat = "RETRACEMENT"
            else:
                stat = "STILL GOOD"
        self.pos_stat = stat

        # this bar becomes history for the next one
        self.hist.append(cur)
        if len(self.hist) > MAX_HIST:
            self.hist.pop(0)

    # ------------------------------------------------------------------
    # read-out
    # ------------------------------------------------------------------
    def snapshot(self, partial: bool = False) -> Optional[dict]:
        """The held signal as of the last bar fed to step(), or None."""
        if self.mr_dir == 0 or self.cur is None:
            return None
        cur = self.cur
        d = self.mr_dir
        # hist already contains cur (appended at end of step)
        prev = self.hist[-2] if len(self.hist) >= 2 else None

        levels = []
        for lvl, src in zip(self.mr_levels, self.mr_lev_src):
            dist = (lvl - cur.c) if d == 1 else (cur.c - lvl)
            pct = dist / cur.c * 100 if cur.c else None
            if d == 1:
                reached_w = self.mr_max_h is not None and self.mr_max_h >= lvl
                reached_c = self.mr_max_c is not None and self.mr_max_c >= lvl
                near = cur.c < lvl
            else:
                reached_w = self.mr_min_l is not None and self.mr_min_l <= lvl
                reached_c = self.mr_min_c is not None and self.mr_min_c <= lvl
                near = cur.c > lvl
            reclaimed = reached_c and near
            taken = reached_w and not reclaimed
            levels.append([_r(lvl), src, _r(dist), _r(round(pct, 4)) if pct is not None else None,
                           "r" if reclaimed else "x" if taken else "o"])

        trig = self.mr_trig
        profit = None
        if trig is not None:
            profit = (cur.c - trig) if d == 1 else (trig - cur.c)
        profit_pct = (profit / trig * 100) if (profit is not None and trig) else None
        t1 = self.mr_levels[0] if self.mr_levels else None
        p3 = self.pos_stat == "POTENTIAL 3"
        three_trig = None
        if p3 and prev is not None:
            three_trig = prev.l if d == 1 else prev.h

        age = self.bar_index - self.mr_bar
        return {
            "dir": d,
            "pat": self.mr_patt,
            "ps": self.mr_name_s,
            "st": self.pos_stat,
            "fresh": age == 0,
            "age": age,
            "sd": self.mr_bar_label,
            "rev": bool(self.mr_is_rev),
            "pz": bool(self.mr_pizazz),
            "wk": bool(self.mr_weak),
            "fr": bool(self.mr_fragile),
            "partial": bool(partial),
            "lc": _r(cur.c),
            "trig": _r(trig),
            "tg": levels,
            "togo": _r(abs(t1 - cur.c)) if t1 is not None else None,
            "pnl": _r(profit),
            "pnlp": _r(round(profit_pct, 4)) if profit_pct is not None else None,
            "p3t": _r(three_trig),
            "cont": _r(cur.o) if p3 else None,
            "revs": self.rev_since_hit,
            "chop": self.rev_since_hit >= self.cfg.chop_thr,
        }


def scan_series(bars: list[Bar], cfg: TargetsConfig,
                last_confirmed: bool = True) -> Optional[dict]:
    """Replay `bars` (oldest first) and return the held-signal snapshot, or
    None when nothing is held / the series is too short."""
    if len(bars) < MIN_BARS:
        return None
    eng = TargetsEngine(cfg)
    last = len(bars) - 1
    for idx, b in enumerate(bars):
        eng.step(b, confirmed=(last_confirmed if idx == last else True))
    return eng.snapshot(partial=not last_confirmed)
