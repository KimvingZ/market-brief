"""Measured probabilities ("base rates").

For one asset, every past day gets a label made of three parts:

  trend       up    = close above both the 50- and 200-day averages
              down  = close below both
              mixed = anything else
  momentum    weak (RSI < 40), neutral (40-60), strong (RSI > 60)
  volatility  calm / normal / wild = ATR% in the bottom / middle / top third
              of the past year

We take the past days whose label matches today's, and count how often price
was higher h bars later. That share is p_up. The same count over *all* days is
the baseline, p_base. The edge is the gap between the two, in percentage
points. A 56% reading against a 53% baseline is a +3 point edge, not a 56%
signal.

Windows longer than one bar overlap (each 7-day window shares 6 days with the
next), so the effective sample size is n / h, and the likely range (a 90%
Wilson interval) is computed on that. If too few matching days exist, the
label is made coarser: drop volatility, then momentum, then use all days.
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta

import numpy as np
import pandas as pd

from .timeutil import session_bounds

Z90 = 1.6448536269514722
MIN_EFF = 30

LEVELS: list[tuple[str, ...]] = [("trend", "momentum", "vol"), ("trend", "momentum"), ("trend",), ()]
LEVEL_NAMES = {
    ("trend", "momentum", "vol"): "trend + momentum + volatility",
    ("trend", "momentum"): "trend + momentum",
    ("trend",): "trend only",
    (): "all days",
}


def wilson(p: float, n: float, z: float = Z90) -> tuple[float, float]:
    if n <= 0 or p is None or math.isnan(p):
        return 0.0, 1.0
    denom = 1.0 + z * z / n
    centre = (p + z * z / (2.0 * n)) / denom
    half = z * math.sqrt(p * (1.0 - p) / n + z * z / (4.0 * n * n)) / denom
    return max(0.0, centre - half), min(1.0, centre + half)


def state_labels(d: pd.DataFrame) -> pd.DataFrame:
    """d: daily bars with ema50, ema200, rsi14, atr_pct_rank columns."""
    close = d["close"]
    trend = pd.Series(
        np.where(
            (close > d["ema50"]) & (close > d["ema200"]),
            "up",
            np.where((close < d["ema50"]) & (close < d["ema200"]), "down", "mixed"),
        ),
        index=d.index,
        dtype="object",
    )
    trend[d["ema200"].isna() | d["ema50"].isna()] = None
    rsi = d["rsi14"]
    mom = pd.Series(np.where(rsi < 40, "weak", np.where(rsi > 60, "strong", "neutral")), index=d.index, dtype="object")
    mom[rsi.isna()] = None
    rank = d["atr_pct_rank"]
    vol = pd.Series(np.where(rank < 1 / 3, "calm", np.where(rank > 2 / 3, "wild", "normal")), index=d.index, dtype="object")
    vol[rank.isna()] = None
    return pd.DataFrame({"trend": trend, "momentum": mom, "vol": vol})


def _r(x: float | None, nd: int = 3):
    if x is None or (isinstance(x, float) and math.isnan(x)):
        return None
    return round(float(x), nd)


def conditional(d: pd.DataFrame, h: int, min_eff: int = MIN_EFF) -> dict | None:
    """Base rate for the next h bars given today's label. d = complete daily bars + indicators."""
    if len(d) < h + 20:
        return None
    labels = state_labels(d)
    close = d["close"]
    fwd = close.shift(-h) / close - 1.0
    has_fwd = fwd.notna()
    if has_fwd.sum() < 20:
        return None
    up = fwd > 0
    cur = labels.iloc[-1]

    chosen: tuple[str, ...] = ()
    mask = has_fwd
    for level in LEVELS:
        if any(cur[c] is None for c in level):
            continue
        m = has_fwd.copy()
        for c in level:
            m &= labels[c] == cur[c]
        if level == () or m.sum() / h >= min_eff:
            chosen, mask = level, m
            break

    n = int(mask.sum())
    n_eff = n / h
    p = float(up[mask].mean()) if n else float("nan")
    lo, hi = wilson(p, n_eff)
    n_base = int(has_fwd.sum())
    p_base = float(up[has_fwd].mean())
    sample = fwd[mask] * 100.0
    return {
        "horizon_bars": h,
        "p_up": _r(p),
        "n": n,
        "n_eff": int(round(n_eff)),
        "lo": _r(lo),
        "hi": _r(hi),
        "p_base": _r(p_base),
        "n_base_eff": int(round(n_base / h)),
        "edge_pp": _r((p - p_base) * 100.0, 1),
        "conditioned_on": LEVEL_NAMES[chosen],
        "state": {c: cur[c] for c in ("trend", "momentum", "vol")},
        "low_sample": bool(chosen == () or n_eff < min_eff),
        "median_move_pct": _r(float(sample.median()), 2) if n else None,
        "q20_pct": _r(float(sample.quantile(0.2)), 2) if n else None,
        "q80_pct": _r(float(sample.quantile(0.8)), 2) if n else None,
    }


# --------------------------------------------------------------------------
# Session statistics from hourly bars (crypto, gold, oil)
# --------------------------------------------------------------------------
def price_asof(h1: pd.DataFrame, t: datetime, max_stale: timedelta = timedelta(hours=3)) -> float | None:
    """Close of the last hourly bar that opened before t, if it is recent enough."""
    if h1 is None or h1.empty:
        return None
    ts = pd.Timestamp(t)
    pos = h1.index.searchsorted(ts, side="left") - 1
    if pos < 0:
        return None
    if h1.index[pos] < ts - pd.Timedelta(max_stale):
        return None
    return float(h1["close"].iloc[pos])


def _cond_block(prev: pd.Series, nxt: pd.Series) -> dict:
    up_prev = prev > 0
    dn_prev = prev < 0

    def blk(mask):
        n = int(mask.sum())
        p = float((nxt[mask] > 0).mean()) if n else None
        lo, hi = wilson(p, n) if n else (0.0, 1.0)
        return {"p_up": _r(p), "n": n, "lo": _r(lo), "hi": _r(hi)}

    return {"after_up": blk(up_prev), "after_down": blk(dn_prev), "baseline": blk(pd.Series(True, index=nxt.index))}


def session_continuation(h1: pd.DataFrame, calendar: str, now: datetime, lookback_days: int = 365) -> dict | None:
    """How often the London (NY) window went up, split by the direction of the window before it."""
    if h1 is None or len(h1) < 24 * 30:
        return None
    rows = []
    start = now.date() - timedelta(days=lookback_days)
    d = start
    while d <= now.date():
        if calendar == "crypto" or d.weekday() < 5:
            b = session_bounds(d)
            if b["ny"][1] <= now:
                pa0 = price_asof(h1, b["asia"][0])
                pa1 = price_asof(h1, b["asia"][1])
                pl1 = price_asof(h1, b["london"][1])
                pn1 = price_asof(h1, b["ny"][1])
                if None not in (pa0, pa1, pl1, pn1):
                    rows.append((pa1 / pa0 - 1, pl1 / pa1 - 1, pn1 / pl1 - 1))
        d += timedelta(days=1)
    if len(rows) < 30:
        return None
    df = pd.DataFrame(rows, columns=["asia", "london", "ny"])
    return {
        "days": len(df),
        "london": _cond_block(df["asia"], df["london"]),
        "ny": _cond_block(df["london"], df["ny"]),
    }


def gap_stats(d: pd.DataFrame) -> dict | None:
    """US stocks: how often the cash session closed above its open, after a gap up / gap down."""
    if len(d) < 60:
        return None
    prev_close = d["close"].shift(1)
    gap = d["open"] / prev_close - 1.0
    sess = d["close"] / d["open"] - 1.0
    valid = gap.notna() & sess.notna()
    return {"days": int(valid.sum()), "ny": _cond_block(gap[valid], sess[valid])}
