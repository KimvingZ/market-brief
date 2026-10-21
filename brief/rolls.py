"""Futures contract rolls.

Yahoo's continuous futures series (CL=F, GC=F) switch from the expiring
contract to the next one. The two contracts trade at different prices (WTI's
November and December contracts were about 5% apart in October 2026), so the
switch shows up as a fake overnight gap that would distort indicators, levels
and grading.

We know when each contract expires, so we look for the gap only on those days:
on the expected roll day (one business day either side), the biggest gap
between a close and the next open, if it is above the threshold, is a roll.
All earlier bars are then scaled by that gap ("ratio back-adjustment"), so the
history is in the current contract's prices. The grader voids calls made
before a roll instead of grading them across two contracts.

Expected roll days (holidays ignored):
  monthly    WTI: the business day after the last trading day, which is 3
             business days before the 25th (4 if the 25th is not a business day)
  bimonthly  COMEX gold: the last business day of Jan, Mar, May, Jul, Sep, Nov
             (first notice day for the next active contract)
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta

import pandas as pd

from .timeutil import NEW_YORK, UTC, iso

PRICE_COLS = ["open", "high", "low", "close"]


def bday_add(d: date, n: int) -> date:
    step = 1 if n > 0 else -1
    while n != 0:
        d = d + timedelta(days=step)
        if d.weekday() < 5:
            n -= step
    return d


def expected_rolls(rule: str, start: date, end: date) -> list[tuple[date, date, date]]:
    """(expected roll day, window start, window end) for each roll between start and end."""
    out = []
    y, m = start.year, start.month
    while (y, m) <= (end.year, end.month):
        if rule == "monthly":
            d25 = date(y, m, 25)
            ltd = bday_add(d25, -3) if d25.weekday() < 5 else bday_add(d25, -4)
            roll = bday_add(ltd, 1)
            out.append((roll, bday_add(roll, -1), bday_add(roll, 1)))
        elif rule == "bimonthly" and m in (1, 3, 5, 7, 9, 11):
            last = date(y + (m == 12), m % 12 + 1, 1) - timedelta(days=1)
            while last.weekday() >= 5:
                last -= timedelta(days=1)
            out.append((last, bday_add(last, -2), bday_add(last, 1)))
        m += 1
        if m == 13:
            y, m = y + 1, 1
    return out


def detect(daily: pd.DataFrame, rule: str, threshold: float) -> list[dict]:
    """Rolls as [{'date': first trading date on the new contract, 'factor': new open / old close}]."""
    if daily is None or len(daily) < 2:
        return []
    prev_close = daily["close"].shift(1)
    gap = (daily["open"] / prev_close - 1.0).dropna()
    if gap.empty:
        return []
    dates = pd.Series([ts.date() for ts in gap.index], index=gap.index)
    out = []
    for _, lo, hi in expected_rolls(rule, dates.iloc[0], dates.iloc[-1]):
        win = gap[(dates >= lo) & (dates <= hi)]
        if win.empty:
            continue
        ts = win.abs().idxmax()
        if abs(win[ts]) >= threshold:
            out.append({"date": ts.date(), "factor": float(daily.at[ts, "open"] / prev_close.at[ts])})
    return out


def _session_start(d: date) -> datetime:
    """CME trading date d starts at 18:00 New York the evening before."""
    return datetime.combine(d - timedelta(days=1), time(18, 0), NEW_YORK).astimezone(UTC)


def back_adjust(daily: pd.DataFrame, hourly: pd.DataFrame | None, rolls: list[dict]):
    """Scale every bar before each roll by that roll's factor. Returns new frames."""
    d = daily.copy()
    h = hourly.copy() if hourly is not None else None
    for r in sorted(rolls, key=lambda x: x["date"], reverse=True):
        cut_day = pd.Timestamp(r["date"]).tz_localize(UTC)
        d.loc[d.index < cut_day, PRICE_COLS] *= r["factor"]
        if h is not None and len(h):
            # the hourly series may switch at a slightly different hour: use the
            # biggest hourly gap within a day of the session start, if there is one
            start = pd.Timestamp(_session_start(r["date"]))
            win = h[(h.index >= start - pd.Timedelta(hours=24)) & (h.index <= start + pd.Timedelta(hours=24))]
            cut = start
            if len(win) > 1:
                hg = (win["open"] / win["close"].shift(1) - 1.0).abs()
                if hg.max() > 0.5 * abs(r["factor"] - 1.0):
                    cut = hg.idxmax()
            h.loc[h.index < cut, PRICE_COLS] *= r["factor"]
            r["hourly_cut"] = iso(cut.to_pydatetime())
    return d, h


def recent(rolls: list[dict], since: date) -> list[dict]:
    return [{"date": r["date"].isoformat(), "change_pct": round((r["factor"] - 1) * 100, 2),
             "hourly_cut": r.get("hourly_cut")} for r in rolls if r["date"] >= since]
