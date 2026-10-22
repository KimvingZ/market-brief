from datetime import datetime

import numpy as np
import pandas as pd

from brief.grade import grade_call
from brief.rolls import back_adjust, detect, expected_rolls
from brief.timeutil import UTC


def _daily(dates, closes, opens=None):
    idx = pd.DatetimeIndex(pd.to_datetime(dates)).tz_localize(UTC)
    c = np.array(closes, dtype=float)
    o = np.array(opens if opens is not None else closes, dtype=float)
    return pd.DataFrame({"open": o, "high": np.maximum(o, c) + 0.5, "low": np.minimum(o, c) - 0.5,
                         "close": c, "volume": 1.0}, index=idx)


def test_expected_roll_days():
    d = lambda s: datetime.fromisoformat(s).date()  # noqa: E731
    wti = [r[0].isoformat() for r in expected_rolls("monthly", d("2026-08-01"), d("2026-10-31"))]
    # last trading days 20 Aug, 22 Sep, 20 Oct 2026 (25 Oct is a Sunday)
    assert wti == ["2026-08-21", "2026-09-23", "2026-10-21"]
    gold = [r[0].isoformat() for r in expected_rolls("bimonthly", d("2026-07-01"), d("2026-10-31"))]
    assert gold == ["2026-07-31", "2026-09-30"]


def test_detect_and_adjust_wti_roll():
    # Nov contract closes 103.66 on the 20th; Dec contract opens 98.6 on the 21st
    d = _daily(["2026-10-19", "2026-10-20", "2026-10-21"], [101.4, 103.66, 99.0], [101.0, 101.5, 98.6])
    rolls = detect(d, "monthly", 0.01)
    assert len(rolls) == 1 and rolls[0]["date"].isoformat() == "2026-10-21"
    adj, _ = back_adjust(d, None, rolls)
    # after adjustment the old close lines up with the new open: no gap left
    assert abs(adj["close"].iloc[1] - adj["open"].iloc[2]) < 1e-9
    assert adj["close"].iloc[2] == 99.0  # the current contract is untouched


def test_real_gap_outside_window_is_kept():
    d = _daily(["2026-10-02", "2026-10-05"], [90.0, 95.0], [89.0, 94.0])  # weekend news gap, 5 Oct
    assert detect(d, "monthly", 0.01) == []


def test_grader_voids_calls_across_a_roll():
    row = {"kind": "call", "id": "x", "asset": "CL", "horizon": "today", "bias": "short", "status": "open",
           "ref_price": 103.66, "ref_time": "2026-10-20T21:00:00Z", "ref_bar": "2026-10-20", "h_bars": 1,
           "window_end": "2026-10-21T21:00:00Z", "invalidation": 106.0, "p_up": 0.5, "p_base": 0.5}
    candles = {"CL": {"calendar": "cme", "daily": [["2026-10-20", 101.5, 104, 101, 103.66, 1],
                                                    ["2026-10-21", 98.6, 99.5, 98, 99.0, 1]],
                      "hourly": [], "rolls": [{"date": "2026-10-21", "change_pct": -4.9, "hourly_cut": None}]}}
    g = grade_call(row, candles, datetime(2026, 10, 22, 0, 40, tzinfo=UTC))
    assert g["status"] == "void" and "roll" in g["note"]


def test_missing_stock_day_built_from_hourly():
    from brief.snapshot import fill_from_hourly
    daily = _daily(["2026-10-19", "2026-10-20"], [217.09, 216.47])
    # hourly bars for 21 Oct: pre-market 08:00 New York, regular 09:30-15:30, post 16:00-19:00
    times = ["2026-10-21T12:00:00Z"] + [f"2026-10-21T{h:02d}:30:00Z" for h in range(13, 20)] + \
            [f"2026-10-21T{h:02d}:00:00Z" for h in range(20, 24)]
    idx = pd.DatetimeIndex(pd.to_datetime(times, utc=True))
    closes = [215.0, 216.0, 217.0, 218.5, 219.0, 220.0, 219.5, 219.26, 219.4, 219.3, 219.2, 219.1]
    hourly = pd.DataFrame({"open": closes, "high": [c + 1 for c in closes], "low": [c - 1 for c in closes],
                           "close": closes, "volume": 10.0}, index=idx)
    out = fill_from_hourly(daily, hourly, "us_equity", datetime(2026, 10, 22, 0, 10, tzinfo=UTC))
    assert out.index[-1].date().isoformat() == "2026-10-21"
    bar = out.iloc[-1]
    assert bar["open"] == 216.0 and bar["close"] == 219.26  # regular hours only: 13:30-19:30 UTC bars
    assert bar["high"] == 221.0 and bar["low"] == 215.0 and bar["volume"] == 70.0
    # crypto and futures are left alone; so is a stock whose daily feed is complete
    assert len(fill_from_hourly(daily, hourly, "cme", datetime(2026, 10, 22, 0, 10, tzinfo=UTC))) == 2
    assert len(fill_from_hourly(out, hourly, "us_equity", datetime(2026, 10, 22, 0, 10, tzinfo=UTC))) == 3
