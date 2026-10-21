"""Turn one asset's candles into its snapshot entry: indicators, levels,
sessions, base rates, and the reference price / window / lean for each horizon."""

from __future__ import annotations

import math
from datetime import datetime, timedelta

import pandas as pd

from . import indicators as ti
from .base_rates import conditional, gap_stats, price_asof, session_continuation
from .levels import key_levels, nearest_beyond, price_decimals, rnd
from .setup import INVALIDATION_BAND, decide, ta_signals
from .timeutil import (
    bar_close_time,
    iso,
    last_bar_closed_by,
    nth_trading_date,
    session_bounds,
    trading_days_between,
)

DAILY_COLS = ["close", "ema20", "ema50", "ema200", "rsi14", "macd", "macd_signal", "macd_hist",
              "atr14", "atr_pct", "atr_pct_rank", "adx14", "bbw_rank", "vol_ratio"]
INTRA_COLS = ["close", "ema20", "ema50", "rsi14", "macd_hist", "atr14"]
WEEK_COLS = ["close", "ema10", "ema40", "rsi14"]


def _f(x, nd=4):
    if x is None:
        return None
    try:
        x = float(x)
    except (TypeError, ValueError):
        return None
    if math.isnan(x) or math.isinf(x):
        return None
    return round(x, nd)


def _round_dict(d: dict, price_keys: set, ref: float, nd: int = 3) -> dict:
    out = {}
    for k, v in d.items():
        out[k] = rnd(v, ref) if k in price_keys else _f(v, nd)
    return out


def split_daily(daily: pd.DataFrame, calendar: str, now: datetime) -> tuple[pd.DataFrame, pd.Series | None]:
    """Complete daily bars, and the in-progress bar (if any)."""
    if daily.empty:
        return daily, None
    closes = pd.Series([bar_close_time(calendar, ts.date()) for ts in daily.index], index=daily.index)
    complete = daily[closes <= pd.Timestamp(now)]
    partial = daily[closes > pd.Timestamp(now)]
    return complete, (partial.iloc[-1] if len(partial) else None)


def _range(h1: pd.DataFrame, a: datetime, b: datetime) -> dict | None:
    sub = h1[(h1.index >= pd.Timestamp(a)) & (h1.index < pd.Timestamp(b))]
    if sub.empty:
        return None
    return {"open": float(sub["open"].iloc[0]), "high": float(sub["high"].max()), "low": float(sub["low"].min()),
            "close": float(sub["close"].iloc[-1]), "start": iso(a), "end": iso(b)}


def session_ranges(h1: pd.DataFrame, now: datetime, ref: float) -> dict:
    """Asia and London ranges for the current UTC day so far, plus yesterday's New York range."""
    today = now.date()
    b = session_bounds(today)
    prev = session_bounds(today - timedelta(days=1))
    asia = _range(h1, b["asia"][0], min(now, b["asia"][1])) if now > b["asia"][0] else None
    london = _range(h1, b["london"][0], min(now, b["london"][1])) if now > b["london"][0] else None
    ny_prev = _range(h1, prev["ny"][0], prev["ny"][1])
    out = {"asia": asia, "london": london, "prev_ny": ny_prev, "flags": []}
    if asia and ny_prev:
        if asia["high"] > ny_prev["high"]:
            out["flags"].append("Asia traded above yesterday's New York high")
        if asia["low"] < ny_prev["low"]:
            out["flags"].append("Asia traded below yesterday's New York low")
    if london and asia and b["asia"][1] <= now:
        if london["high"] > asia["high"]:
            out["flags"].append("London took out the Asia high")
        if london["low"] < asia["low"]:
            out["flags"].append("London took out the Asia low")
    for k in ("asia", "london", "prev_ny"):
        if out[k]:
            for f in ("open", "high", "low", "close"):
                out[k][f] = rnd(out[k][f], ref)
    return out


def analyze_asset(acfg: dict, daily_raw: pd.DataFrame, hourly_raw: pd.DataFrame | None, now: datetime,
                  horizons: dict, source: dict, deriv: dict | None = None, hourly_days: int = 365) -> tuple[dict, dict]:
    """Returns (snapshot entry, candles entry)."""
    cal = acfg["calendar"]
    entry: dict = {
        "id": acfg["id"], "name": acfg["name"], "group": acfg["group"], "calendar": cal,
        "calls": bool(acfg.get("calls")), "source": source, "status": "ok", "notes": [],
    }
    daily, partial = split_daily(daily_raw, cal, now)
    if len(daily) < 30:
        entry["status"] = "missing"
        entry["notes"].append("not enough daily history")
        return entry, {}

    if acfg.get("unit") == "percent" and float(daily["close"].median()) > 20:
        daily = daily.copy()
        for c in ("open", "high", "low", "close"):
            daily[c] = daily[c] / 10.0
        if hourly_raw is not None:
            hourly_raw = hourly_raw.copy()
            for c in ("open", "high", "low", "close"):
                hourly_raw[c] = hourly_raw[c] / 10.0
        if partial is not None:
            partial = partial.copy()
            for c in ("open", "high", "low", "close"):
                partial[c] = partial[c] / 10.0

    di = ti.add_daily(daily)
    last_date = di.index[-1].date()
    ref_close = float(di["close"].iloc[-1])
    ref_time = bar_close_time(cal, last_date)

    has_hourly = hourly_raw is not None and len(hourly_raw) >= 60
    if has_hourly:
        live = float(hourly_raw["close"].iloc[-1])
        live_time = min(now, (hourly_raw.index[-1] + pd.Timedelta(hours=1)).to_pydatetime())
    elif partial is not None:
        live, live_time = float(partial["close"]), now
    else:
        live, live_time = ref_close, ref_time
    ref = live

    expected = last_bar_closed_by(cal, now)
    lag = trading_days_between(cal, last_date, expected)
    stale_daily = lag > (0 if cal == "crypto" else 1)
    if stale_daily:
        entry["notes"].append(f"daily data is {lag} bars behind")
    hourly_age_h = None
    if has_hourly:
        hourly_age_h = (pd.Timestamp(now) - hourly_raw.index[-1]).total_seconds() / 3600.0
        if cal == "crypto" and hourly_age_h > 2.5:
            entry["notes"].append(f"hourly data is {hourly_age_h:.1f}h old")

    h4 = ti.add_intraday(ti.resample(hourly_raw, "4h")) if has_hourly else None
    h1 = ti.add_intraday(hourly_raw) if has_hourly else None
    wk = ti.add_weekly(ti.resample(daily, "W"))

    ind = {
        "daily": ti.last_values(di, DAILY_COLS),
        "h4": ti.last_values(h4, INTRA_COLS) if h4 is not None else None,
        "h1": ti.last_values(h1, INTRA_COLS) if h1 is not None else None,
        "weekly": ti.last_values(wk, WEEK_COLS),
        "price": live,
    }
    atr_d = ind["daily"]["atr14"] or (ref_close * 0.02)

    # changes
    closes = di["close"]

    def chg(n):
        if len(closes) > n:
            return _f((ref_close / float(closes.iloc[-1 - n]) - 1) * 100, 2)
        return None

    p24 = price_asof(hourly_raw, now - timedelta(hours=24), timedelta(hours=6)) if has_hourly else None
    entry["price"] = {
        "last": rnd(live, ref), "last_time": iso(live_time),
        "ref_close": rnd(ref_close, ref), "ref_close_date": last_date.isoformat(), "ref_close_time": iso(ref_time),
        "chg_since_close_pct": _f((live / ref_close - 1) * 100, 2),
        "chg_24h_pct": _f((live / p24 - 1) * 100, 2) if p24 else None,
        "chg_5d_pct": chg(5), "chg_20d_pct": chg(20),
        "decimals": price_decimals(ref),
    }

    # trend words
    d = ind["daily"]
    if d["ema50"] and d["ema200"]:
        trend = "up" if live > d["ema50"] and live > d["ema200"] else "down" if live < d["ema50"] and live < d["ema200"] else "mixed"
    else:
        trend = None
    w = ind["weekly"]
    wtrend = None
    if w["ema10"] and w["ema40"] and w["close"]:
        wtrend = "up" if w["close"] > w["ema10"] > w["ema40"] else "down" if w["close"] < w["ema10"] < w["ema40"] else "mixed"
    entry["trend"] = {"daily": trend, "weekly": wtrend}

    price_keys = {"close", "ema20", "ema50", "ema200", "ema10", "ema40", "atr14"}
    entry["indicators"] = {
        "daily": _round_dict(ind["daily"], price_keys, ref),
        "h4": _round_dict(ind["h4"], price_keys, ref) if ind["h4"] else None,
        "h1": _round_dict(ind["h1"], price_keys, ref) if ind["h1"] else None,
        "weekly": _round_dict(ind["weekly"], price_keys, ref),
    }

    # sessions and levels
    sessions = session_ranges(hourly_raw, now, ref) if has_hourly else None
    extra = []
    if sessions:
        for key, label in (("asia", "Asia"), ("london", "London"), ("prev_ny", "previous New York")):
            r = sessions.get(key)
            if r:
                extra.append({"price": r["high"], "label": f"{label} high", "kind": f"{key}_high"})
                extra.append({"price": r["low"], "label": f"{label} low", "kind": f"{key}_low"})
    entry["sessions"] = sessions
    lv = key_levels(di, h4, live, atr_d, extra)
    entry["levels"] = lv

    # base rates
    hz_bars = horizons
    base = {name: conditional(di, h) for name, h in hz_bars.items()}
    entry["base_rates"] = base
    if cal == "us_equity":
        gs = gap_stats(daily)
        entry["session_stats"] = {"kind": "gap", **gs} if gs else None
    elif has_hourly:
        cont = session_continuation(hourly_raw, cal, now, hourly_days)
        entry["session_stats"] = {"kind": "continuation", **cont} if cont else None
    else:
        entry["session_stats"] = None

    # horizons: reference, window, lean, cap, suggested invalidation
    today_end = bar_close_time(cal, nth_trading_date(cal, last_date, 1))
    eligible_time = (today_end - now) <= timedelta(hours=38)
    level_list = lv["all"]
    hz_out = {}
    for name, h in hz_bars.items():
        br = base[name]
        sig = ta_signals(name, ind)
        score = sum(s["vote"] for s in sig)
        dec = decide(score, br)
        end_date = nth_trading_date(cal, last_date, h)
        reasons = []
        if not entry["calls"]:
            reasons.append("context only")
        if stale_daily:
            reasons.append("price data is behind")
        if not eligible_time:
            reasons.append("market closed until the next session")
        if br is None:
            reasons.append("not enough history for a base rate")
        inv = None
        if dec["lean"] in ("long", "short"):
            lo_m, hi_m = INVALIDATION_BAND[name]
            side = "below" if dec["lean"] == "long" else "above"
            inv = nearest_beyond(level_list, ref_close, side, lo_m * atr_d, hi_m * atr_d) or \
                nearest_beyond(level_list, ref_close, side, 0.5 * lo_m * atr_d, 1.5 * hi_m * atr_d)
            if inv is None:
                dec = {"lean": "neutral", "max_conf": "low", "allowed": ["neutral"], "news_only": ["long", "short"],
                       "why": "no nearby level to define risk"}
        hz_out[name] = {
            "h_bars": h,
            "ref_price": rnd(ref_close, ref),
            "ref_time": iso(ref_time),
            "ref_bar": last_date.isoformat(),
            "window_end": iso(bar_close_time(cal, end_date)),
            "end_bar": end_date.isoformat(),
            "eligible": not reasons,
            "why_not": ", ".join(reasons),
            "ta_score": score,
            "signals": sig,
            "lean": dec["lean"],
            "lean_why": dec.get("why", ""),
            "max_conf": dec["max_conf"],
            "allowed": dec["allowed"],
            "news_only": dec["news_only"],
            "suggested_invalidation": inv,
        }
    entry["horizons"] = hz_out
    entry["state_words"] = _state_words(entry, ind)
    if deriv:
        entry["derivatives"] = deriv

    # candles for charts and grading
    keep_d = di.iloc[-420:]
    candles = {
        "calendar": cal,
        "decimals": price_decimals(ref),
        "daily": [[ts.date().isoformat(), *(rnd(float(r[c]), ref) for c in ("open", "high", "low", "close")), _f(r["volume"], 2)]
                  for ts, r in keep_d.iterrows()],
        "daily_partial": ([partial.name.date().isoformat(), *(rnd(float(partial[c]), ref) for c in ("open", "high", "low", "close"))]
                          if partial is not None else None),
        "hourly": ([[iso(ts.to_pydatetime()), *(rnd(float(r[c]), ref) for c in ("open", "high", "low", "close"))]
                    for ts, r in hourly_raw.iloc[-240:].iterrows()] if has_hourly else []),
    }
    return entry, candles


def _state_words(entry: dict, ind: dict) -> list[str]:
    d = ind["daily"]
    words = []
    t = entry["trend"]["daily"]
    if t == "up":
        words.append("Daily trend up: price above the 50- and 200-day averages")
    elif t == "down":
        words.append("Daily trend down: price below the 50- and 200-day averages")
    elif t == "mixed":
        words.append("Daily trend mixed: price between the 50- and 200-day averages")
    if d.get("rsi14") is not None:
        r = d["rsi14"]
        words.append(f"Daily RSI {r:.0f}" + (" (overbought zone)" if r >= 70 else " (oversold zone)" if r <= 30 else ""))
    if d.get("atr_pct") is not None:
        rank = d.get("atr_pct_rank")
        regime = "" if rank is None else " (calm for this asset)" if rank < 1 / 3 else " (wild for this asset)" if rank > 2 / 3 else " (normal for this asset)"
        words.append(f"Typical daily range (ATR) {d['atr_pct']:.1f}%{regime}")
    if d.get("adx14") is not None:
        a = d["adx14"]
        words.append(f"ADX {a:.0f}: " + ("trending" if a >= 25 else "weak trend / range" if a < 20 else "trend building"))
    if d.get("bbw_rank") is not None and d["bbw_rank"] < 0.1:
        words.append("Bollinger bands very tight: a big move often follows")
    return words
