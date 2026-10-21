"""Synthetic price history for offline tests.

Hourly bars follow a random walk for the last `hourly_days`; daily bars before
that are a separate random walk that ends where the hourly path starts. Daily
bars inside the hourly period are built from the hourly bars, so grading has
consistent prices to work with.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

from brief.snapshot import load_config, write_hist
from brief.timeutil import UTC

START_PRICE = {"BTC": 100000.0, "ETH": 4000.0, "SOL": 200.0, "ZEC": 300.0, "AAVE": 250.0, "HYPE": 40.0,
               "XAU": 4000.0, "CL": 62.0, "NVDA": 180.0, "AMD": 220.0, "AVGO": 330.0, "TSM": 290.0,
               "MU": 200.0, "SNDK": 150.0, "WDC": 120.0, "STX": 230.0, "SPY": 660.0, "QQQ": 590.0,
               "SMH": 330.0, "DXY": 98.0, "US10Y": 41.0}


def _hour_open(calendar: str, t: pd.Timestamp) -> bool:
    if calendar == "crypto":
        return True
    wd = t.weekday()
    if calendar == "us_equity":
        return wd < 5 and 8 <= t.hour <= 23
    # cme: Sunday 22:00 UTC to Friday 21:00 UTC, daily break 21:00-22:00 UTC
    if t.hour == 21:
        return False
    if wd == 5:
        return False
    if wd == 6:
        return t.hour >= 22
    if wd == 4 and t.hour >= 21:
        return False
    return True


def _trading_date(calendar: str, t: pd.Timestamp):
    if calendar == "cme":
        return (t + pd.Timedelta(hours=2)).date()
    return t.date()


def make_asset(calendar: str, p0: float, end: datetime, years: int = 6, hourly_days: int = 400,
               seed: int = 0, daily_vol: float = 0.02, drift: float = 0.0003) -> tuple[pd.DataFrame, pd.DataFrame]:
    rng = np.random.default_rng(seed)
    end = pd.Timestamp(end).floor("h")
    h_start = end - pd.Timedelta(days=hourly_days)
    hours = pd.date_range(h_start, end - pd.Timedelta(hours=1), freq="h", tz=UTC)
    hours = hours[[_hour_open(calendar, t) for t in hours]]
    hv = daily_vol / np.sqrt(24)
    rets = rng.normal(drift / 24, hv, len(hours))
    gaps = np.zeros(len(hours))
    if calendar == "us_equity":
        new_day = np.r_[True, hours[1:].date != hours[:-1].date]
        gaps[new_day] = rng.normal(0, daily_vol / 3, int(new_day.sum()))
    close = p0 * np.exp(np.cumsum(rets + gaps))
    open_ = np.concatenate([[p0], close[:-1]]) * np.exp(gaps)
    wig = np.abs(rng.normal(0, hv / 2, len(hours)))
    high = np.maximum(open_, close) * (1 + wig)
    low = np.minimum(open_, close) * (1 - wig)
    vol = rng.uniform(100, 1000, len(hours))
    hourly = pd.DataFrame({"open": open_, "high": high, "low": low, "close": close, "volume": vol}, index=hours)

    tdates = [_trading_date(calendar, t) for t in hourly.index]
    g = hourly.groupby(pd.Index(tdates))
    d_recent = pd.DataFrame({"open": g["open"].first(), "high": g["high"].max(), "low": g["low"].min(),
                             "close": g["close"].last(), "volume": g["volume"].sum()})
    d_recent.index = pd.DatetimeIndex(pd.to_datetime(d_recent.index)).tz_localize(UTC)
    if calendar != "crypto":
        d_recent = d_recent[d_recent.index.weekday < 5]

    first = d_recent.index[0]
    n_old = 365 * years
    days = pd.date_range(first - pd.Timedelta(days=n_old), first - pd.Timedelta(days=1), freq="D", tz=UTC)
    if calendar != "crypto":
        days = days[days.weekday < 5]
    r = rng.normal(drift, daily_vol, len(days))
    path = d_recent["open"].iloc[0] * np.exp(-np.cumsum(r[::-1]))[::-1]
    o = np.concatenate([[path[0] / np.exp(r[0])], path[:-1]])
    if calendar == "us_equity":
        o = o * np.exp(rng.normal(0, daily_vol / 3, len(o)))
    wig = np.abs(rng.normal(0, daily_vol / 2, len(days)))
    d_old = pd.DataFrame({"open": o, "high": np.maximum(o, path) * (1 + wig), "low": np.minimum(o, path) * (1 - wig),
                          "close": path, "volume": rng.uniform(1e4, 1e5, len(days))}, index=days)
    daily = pd.concat([d_old, d_recent])
    return daily, hourly


def write_prev(prev: Path, end: datetime, seed: int = 1, cfg: dict | None = None) -> dict:
    """Write a fake data branch with history for every configured asset."""
    cfg = cfg or load_config()
    meta = {"assets": {}, "last_full": (end - timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ")}
    for i, a in enumerate(cfg["assets"]):
        daily, hourly = make_asset(a["calendar"], START_PRICE.get(a["id"], 100.0), end, seed=seed + i)
        src = a["sources"][0]
        write_hist(prev, a["id"], "1d", daily)
        write_hist(prev, a["id"], "1h", hourly)
        meta["assets"][a["id"]] = {"source_key": f"{src['provider']}:{src['symbol']}", "label": src.get("label")}
    (prev / "history").mkdir(parents=True, exist_ok=True)
    (prev / "history" / "meta.json").write_text(json.dumps(meta))
    return meta


def extend_prev(prev: Path, new_end: datetime, seed: int = 99, cfg: dict | None = None) -> None:
    """Append more hourly bars (and the daily bars they make) to an existing fake data branch."""
    from brief.snapshot import read_hist

    cfg = cfg or load_config()
    rng = np.random.default_rng(seed)
    for a in cfg["assets"]:
        h = read_hist(prev, a["id"], "1h")
        d = read_hist(prev, a["id"], "1d")
        last = h.index[-1]
        hours = pd.date_range(last + pd.Timedelta(hours=1), pd.Timestamp(new_end).floor("h") - pd.Timedelta(hours=1),
                              freq="h", tz=UTC)
        hours = hours[[_hour_open(a["calendar"], t) for t in hours]]
        if len(hours) == 0:
            continue
        p0 = float(h["close"].iloc[-1])
        rets = rng.normal(0, 0.02 / np.sqrt(24), len(hours))
        close = p0 * np.exp(np.cumsum(rets))
        open_ = np.concatenate([[p0], close[:-1]])
        add = pd.DataFrame({"open": open_, "high": np.maximum(open_, close) * 1.001,
                            "low": np.minimum(open_, close) * 0.999, "close": close,
                            "volume": rng.uniform(100, 1000, len(hours))}, index=hours)
        h2 = pd.concat([h, add])
        tdates = [_trading_date(a["calendar"], t) for t in add.index]
        g = add.groupby(pd.Index(tdates))
        dn = pd.DataFrame({"open": g["open"].first(), "high": g["high"].max(), "low": g["low"].min(),
                           "close": g["close"].last(), "volume": g["volume"].sum()})
        dn.index = pd.DatetimeIndex(pd.to_datetime(dn.index)).tz_localize(UTC)
        if a["calendar"] != "crypto":
            dn = dn[dn.index.weekday < 5]
        # merge a partially-built day: combine with existing bar of the same date
        for ts in dn.index:
            if ts in d.index:
                old = d.loc[ts]
                dn.loc[ts, "open"] = old["open"]
                dn.loc[ts, "high"] = max(old["high"], dn.loc[ts, "high"])
                dn.loc[ts, "low"] = min(old["low"], dn.loc[ts, "low"])
                dn.loc[ts, "volume"] = old["volume"] + dn.loc[ts, "volume"]
        d2 = pd.concat([d[~d.index.isin(dn.index)], dn]).sort_index()
        write_hist(prev, a["id"], "1h", h2)
        write_hist(prev, a["id"], "1d", d2)
