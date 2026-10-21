import numpy as np
import pandas as pd

from brief.indicators import add_daily
from brief.levels import key_levels, nearest_beyond, round_numbers, swing_points


def test_swing_points_on_zigzag():
    closes = [10, 11, 12, 13, 12, 11, 10, 9, 10, 11, 12, 11, 10]
    idx = pd.date_range("2026-01-01", periods=len(closes), freq="D", tz="UTC")
    df = pd.DataFrame({"open": closes, "high": closes, "low": closes, "close": closes, "volume": 1.0}, index=idx, dtype=float)
    pts = swing_points(df, k=3)
    assert {(p["kind"], p["price"]) for p in pts} == {("swing_high", 13.0), ("swing_low", 9.0)}


def test_round_numbers_bitcoin_like():
    assert round_numbers(104250, 2700) == [102500, 100000, 97500, 105000, 107500, 110000]


def test_key_levels_sides_and_rounding():
    rng = np.random.default_rng(3)
    c = 100 + rng.normal(0, 1, 400).cumsum()
    idx = pd.date_range("2025-01-01", periods=400, freq="D", tz="UTC")
    df = pd.DataFrame({"open": c, "high": c + 0.8, "low": c - 0.8, "close": c, "volume": 1.0}, index=idx)
    d = add_daily(df)
    price = float(c[-1])
    lv = key_levels(d, None, price, float(d["atr14"].iloc[-1]))
    assert all(s["price"] < price for s in lv["supports"])
    assert all(r["price"] > price for r in lv["resistances"])
    assert all(round(level["price"], 2) == level["price"] for level in lv["all"])
    inv = nearest_beyond(lv["all"], price, "below", 0.0, 100.0)
    assert inv is None or inv["price"] < price
