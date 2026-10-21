"""Support and resistance levels.

Candidates come from swing highs/lows (daily and 4h), the previous day's and
week's high/low, the week and month opens, the main moving averages, and the
current session ranges. Swing points within 0.35 x daily ATR of each other are
merged into one level; the more times price turned there, the stronger it is.

Every level price is rounded once, here. The rest of the system, including
the validator, compares against these exact rounded values.
"""

from __future__ import annotations

import math

import numpy as np
import pandas as pd


def price_decimals(price: float) -> int:
    p = abs(price)
    if p >= 10000:
        return 0
    if p >= 1000:
        return 1
    if p >= 1:
        return 2
    if p >= 0.01:
        return 4
    return 6


def rnd(value: float | None, ref: float) -> float | None:
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return None
    return round(float(value), price_decimals(ref))


def swing_points(df: pd.DataFrame, k: int = 3, lookback: int = 180) -> list[dict]:
    """Bars whose high (low) is the highest (lowest) of the k bars either side."""
    if len(df) < 2 * k + 1:
        return []
    sub = df.iloc[-lookback:]
    highs = sub["high"].to_numpy()
    lows = sub["low"].to_numpy()
    idx = sub.index
    out = []
    for i in range(k, len(sub) - k):
        win_h = highs[i - k : i + k + 1]
        win_l = lows[i - k : i + k + 1]
        if highs[i] == np.nanmax(win_h) and (win_h == highs[i]).sum() == 1:
            out.append({"price": float(highs[i]), "time": idx[i], "kind": "swing_high"})
        if lows[i] == np.nanmin(win_l) and (win_l == lows[i]).sum() == 1:
            out.append({"price": float(lows[i]), "time": idx[i], "kind": "swing_low"})
    return out


def cluster(points: list[dict], tol: float) -> list[dict]:
    """Merge points whose prices are within tol of the running cluster mean."""
    if not points:
        return []
    pts = sorted(points, key=lambda p: p["price"])
    groups: list[list[dict]] = [[pts[0]]]
    for p in pts[1:]:
        g = groups[-1]
        mean = sum(q["price"] for q in g) / len(g)
        if abs(p["price"] - mean) <= tol:
            g.append(p)
        else:
            groups.append([p])
    out = []
    for g in groups:
        out.append(
            {
                "price": sum(q["price"] for q in g) / len(g),
                "touches": len(g),
                "last_time": max(q["time"] for q in g),
            }
        )
    return out


def key_levels(
    daily: pd.DataFrame,
    h4: pd.DataFrame | None,
    last_price: float,
    atr_daily: float,
    extra: list[dict] | None = None,
) -> dict:
    """Build the level list for one asset.

    daily: complete daily bars with indicator columns (ema20/50/200).
    h4: 4-hour bars (may be None for daily-only sources).
    extra: additional named levels, e.g. session highs/lows.
    """
    ref = last_price
    tol = 0.35 * atr_daily if atr_daily and atr_daily > 0 else 0.005 * ref
    cands: list[dict] = []

    swings = swing_points(daily, k=3, lookback=180)
    if h4 is not None and not h4.empty:
        swings += swing_points(h4, k=3, lookback=180)
    for c in cluster(swings, tol):
        label = "swing level" if c["touches"] == 1 else f"swing level ({c['touches']} touches)"
        cands.append({"price": c["price"], "label": label, "weight": 1 + c["touches"], "kind": "swing"})

    if len(daily) >= 1:
        last = daily.iloc[-1]
        cands.append({"price": float(last["high"]), "label": "previous day high", "weight": 3, "kind": "pdh"})
        cands.append({"price": float(last["low"]), "label": "previous day low", "weight": 3, "kind": "pdl"})

    weeks = daily.resample("W-MON", label="left", closed="left").agg({"high": "max", "low": "min", "open": "first"})
    weeks = weeks.dropna()
    if len(weeks) >= 2:
        prev_w = weeks.iloc[-2]
        cands.append({"price": float(prev_w["high"]), "label": "previous week high", "weight": 4, "kind": "pwh"})
        cands.append({"price": float(prev_w["low"]), "label": "previous week low", "weight": 4, "kind": "pwl"})
    if len(weeks) >= 1:
        cands.append({"price": float(weeks.iloc[-1]["open"]), "label": "week open", "weight": 2, "kind": "wo"})
    months = daily.resample("MS").agg({"open": "first"}).dropna()
    if len(months) >= 1:
        cands.append({"price": float(months.iloc[-1]["open"]), "label": "month open", "weight": 2, "kind": "mo"})

    for col, label in (("ema20", "20-day average"), ("ema50", "50-day average"), ("ema200", "200-day average")):
        if col in daily and not pd.isna(daily[col].iloc[-1]):
            cands.append({"price": float(daily[col].iloc[-1]), "label": label, "weight": 2, "kind": col})

    for e in extra or []:
        if e.get("price") is not None:
            cands.append({"price": float(e["price"]), "label": e["label"], "weight": e.get("weight", 2), "kind": e.get("kind", "session")})

    for p in round_numbers(ref, atr_daily):
        cands.append({"price": p, "label": "round number", "weight": 1, "kind": "round"})

    # keep levels within 6 ATR of price, round, merge near-duplicates (keep the heavier)
    window = 6 * atr_daily if atr_daily and atr_daily > 0 else 0.15 * ref
    near = [c for c in cands if abs(c["price"] - ref) <= window]
    for c in near:
        c["price"] = rnd(c["price"], ref)
    near.sort(key=lambda c: (-c["weight"], c["price"]))
    merge_tol = 0.25 * (atr_daily if atr_daily and atr_daily > 0 else tol)
    merged: list[dict] = []
    for c in near:
        dup = next((m for m in merged if abs(m["price"] - c["price"]) <= merge_tol), None)
        if dup is None:
            merged.append(dict(c))
        else:
            if c["label"] not in dup["label"]:
                dup["also"] = sorted(set(dup.get("also", []) + [c["label"]]))
    merged.sort(key=lambda c: c["price"])

    min_gap = 0.1 * (atr_daily or tol)
    supports = sorted([m for m in merged if m["price"] < ref - min_gap], key=lambda m: -m["price"])
    resistances = sorted([m for m in merged if m["price"] > ref + min_gap], key=lambda m: m["price"])

    def _pub(m: dict) -> dict:
        out = {"price": m["price"], "label": m["label"], "kind": m["kind"]}
        if m.get("also"):
            out["also"] = m["also"]
        return out

    return {
        "supports": [_pub(m) for m in supports[:2]],
        "resistances": [_pub(m) for m in resistances[:2]],
        "all": [_pub(m) for m in merged],
    }


def round_numbers(price: float, atr_daily: float, each_side: int = 3) -> list[float]:
    """Round-number levels spaced about one daily ATR apart (e.g. every 2,500 on BTC)."""
    if not atr_daily or atr_daily <= 0 or not price or price <= 0:
        return []
    exp = math.floor(math.log10(atr_daily))
    steps = [m * 10 ** e for e in (exp - 1, exp, exp + 1) for m in (1, 2, 2.5, 5)]
    step = min(steps, key=lambda s: abs(math.log(s / atr_daily)))
    base = math.floor(price / step) * step
    out = [base - i * step for i in range(each_side)] + [base + (i + 1) * step for i in range(each_side)]
    return [p for p in out if p > 0]


def nearest_beyond(levels: list[dict], ref: float, side: str, min_dist: float, max_dist: float) -> dict | None:
    """The nearest level below (side='below') or above ref, at least min_dist away."""
    if side == "below":
        cands = [l for l in levels if ref - max_dist <= l["price"] <= ref - min_dist]
        return max(cands, key=lambda l: l["price"]) if cands else None
    cands = [l for l in levels if ref + min_dist <= l["price"] <= ref + max_dist]
    return min(cands, key=lambda l: l["price"]) if cands else None
