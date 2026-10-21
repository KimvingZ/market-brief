import numpy as np
import pandas as pd

from brief import indicators as ti


def _bars(closes, spread=1.0, start="2026-01-01"):
    idx = pd.date_range(start, periods=len(closes), freq="D", tz="UTC")
    c = pd.Series(closes, index=idx, dtype="float64")
    return pd.DataFrame({"open": c.shift(1).fillna(c.iloc[0]), "high": c + spread, "low": c - spread,
                         "close": c, "volume": 1.0})


def test_ema_matches_recursion():
    x = pd.Series(np.random.default_rng(1).normal(100, 5, 300))
    got = ti.ema(x, 20)
    a = 2 / 21
    y = x.iloc[0]
    for t in range(1, 300):
        y = (1 - a) * y + a * x.iloc[t]
        if t >= 19:
            assert abs(got.iloc[t] - y) < 1e-9
    assert got.iloc[:19].isna().all()


def test_rsi_extremes_and_flat():
    up = pd.Series(np.arange(1, 101, dtype=float))
    down = up[::-1].reset_index(drop=True)
    flat = pd.Series(np.full(100, 5.0))
    assert ti.rsi(up).iloc[-1] == 100.0
    assert ti.rsi(down).iloc[-1] == 0.0
    assert ti.rsi(flat).iloc[-1] == 50.0


def test_rsi_matches_wilder_loop():
    x = pd.Series(np.random.default_rng(2).normal(0, 1, 400).cumsum() + 100)
    got = ti.rsi(x, 14)
    d = x.diff()
    g = l = None
    for t in range(1, 400):
        gain, loss = max(d.iloc[t], 0.0), max(-d.iloc[t], 0.0)
        g = gain if g is None else g + (gain - g) / 14
        l = loss if l is None else l + (loss - l) / 14
        if t >= 14:
            assert abs(got.iloc[t] - (100 - 100 / (1 + g / l))) < 1e-9


def test_atr_constant_range():
    df = _bars([100.0] * 60, spread=1.0)
    assert abs(ti.atr(df, 14).iloc[-1] - 2.0) < 1e-9


def test_macd_zero_on_flat():
    line, sig, hist = ti.macd(pd.Series(np.full(100, 7.0)))
    assert abs(line.iloc[-1]) < 1e-12 and abs(hist.iloc[-1]) < 1e-12


def test_pct_rank():
    s = pd.Series(np.arange(300, dtype=float))
    r = ti.pct_rank(s, 252, 60)
    assert r.iloc[-1] == 1.0
    assert r.iloc[:59].isna().all() and not np.isnan(r.iloc[59])
    flat = ti.pct_rank(pd.Series(np.full(100, 3.0)), 252, 60)
    assert flat.iloc[-1] == 0.5
    rev = ti.pct_rank(pd.Series(np.arange(300, 0, -1, dtype=float)), 252, 60)
    assert rev.iloc[-1] == 0.0


def test_resample_weekly_starts_monday():
    df = _bars(np.arange(14, dtype=float) + 10, start="2026-10-12")  # Mon 12 Oct .. Sun 25 Oct
    wk = ti.resample(df, "W")
    assert [d.date().isoformat() for d in wk.index] == ["2026-10-12", "2026-10-19"]
    assert wk["close"].iloc[0] == df["close"].iloc[6]


def test_resample_4h_aligned_to_utc_midnight():
    idx = pd.date_range("2026-10-21 01:00", periods=10, freq="h", tz="UTC")
    df = pd.DataFrame({"open": 1.0, "high": 2.0, "low": 0.5, "close": 1.5, "volume": 1.0}, index=idx)
    h4 = ti.resample(df, "4h")
    assert [t.hour for t in h4.index] == [0, 4, 8]
