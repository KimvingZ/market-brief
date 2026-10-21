"""Technical indicators on OHLCV DataFrames (columns open/high/low/close/volume).

Smoothing follows the usual charting conventions: EMA with alpha 2/(n+1),
Wilder's smoothing (alpha 1/n) for RSI, ATR and ADX. Values can differ from a
charting site for the first ~100 bars because of how the series is seeded;
after that they converge.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

AGG = {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}


def ema(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(span=n, adjust=False, min_periods=n).mean()


def sma(s: pd.Series, n: int) -> pd.Series:
    return s.rolling(n, min_periods=n).mean()


def wilder(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(alpha=1.0 / n, adjust=False, min_periods=n).mean()


def rsi(close: pd.Series, n: int = 14) -> pd.Series:
    delta = close.diff()
    gain = wilder(delta.clip(lower=0.0), n)
    loss = wilder((-delta).clip(lower=0.0), n)
    with np.errstate(divide="ignore", invalid="ignore"):
        out = 100.0 - 100.0 / (1.0 + gain / loss)
    out = out.mask((loss == 0) & (gain > 0), 100.0)
    out = out.mask((loss == 0) & (gain == 0), 50.0)
    return out


def true_range(df: pd.DataFrame) -> pd.Series:
    prev = df["close"].shift(1)
    tr = pd.concat(
        [df["high"] - df["low"], (df["high"] - prev).abs(), (df["low"] - prev).abs()], axis=1
    ).max(axis=1, skipna=True)
    return tr


def atr(df: pd.DataFrame, n: int = 14) -> pd.Series:
    return wilder(true_range(df), n)


def macd(close: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9):
    line = ema(close, fast) - ema(close, slow)
    sig = line.ewm(span=signal, adjust=False, min_periods=signal).mean()
    return line, sig, line - sig


def adx(df: pd.DataFrame, n: int = 14) -> pd.Series:
    up = df["high"].diff()
    down = -df["low"].diff()
    plus_dm = up.where((up > down) & (up > 0), 0.0)
    minus_dm = down.where((down > up) & (down > 0), 0.0)
    tr_s = wilder(true_range(df), n)
    with np.errstate(divide="ignore", invalid="ignore"):
        plus_di = 100.0 * wilder(plus_dm, n) / tr_s
        minus_di = 100.0 * wilder(minus_dm, n) / tr_s
        dx = 100.0 * (plus_di - minus_di).abs() / (plus_di + minus_di)
    return wilder(dx.replace([np.inf, -np.inf], np.nan), n)


def bollinger_width(close: pd.Series, n: int = 20, k: float = 2.0) -> pd.Series:
    mid = sma(close, n)
    sd = close.rolling(n, min_periods=n).std(ddof=0)
    return (2.0 * k * sd) / mid


def pct_rank(s: pd.Series, window: int = 252, min_periods: int = 60) -> pd.Series:
    """Where each value sits within its trailing window (itself included), 0..1.

    0 means lower than every earlier value in the window, 1 higher than all of them.
    NaN until the window holds at least min_periods values.
    """
    a = s.to_numpy(dtype="float64")
    if a.size == 0:
        return s.astype("float64")
    pad = np.concatenate([np.full(window - 1, np.nan), a])
    w = np.lib.stride_tricks.sliding_window_view(pad, window)
    last = w[:, -1]
    prior = w[:, :-1]
    cnt = (~np.isnan(prior)).sum(axis=1)
    less = (prior < last[:, None]).sum(axis=1)
    equal = (prior == last[:, None]).sum(axis=1)
    with np.errstate(invalid="ignore", divide="ignore"):
        r = (less + 0.5 * equal) / cnt
    r[(cnt + 1) < min_periods] = np.nan
    r[np.isnan(last)] = np.nan
    return pd.Series(r, index=s.index)


def resample(df: pd.DataFrame, rule: str) -> pd.DataFrame:
    """Resample OHLCV. '4h' is aligned to 00:00 UTC; 'W-MON' gives Monday-start weeks."""
    if df.empty:
        return df.copy()
    if rule.upper().startswith("W"):
        out = df.resample("W-MON", label="left", closed="left").agg(AGG)
    else:
        out = df.resample(rule, label="left", closed="left", origin="epoch").agg(AGG)
    return out.dropna(subset=["close"])


def add_daily(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    c = out["close"]
    out["ema20"] = ema(c, 20)
    out["ema50"] = ema(c, 50)
    out["ema200"] = ema(c, 200)
    out["rsi14"] = rsi(c, 14)
    out["macd"], out["macd_signal"], out["macd_hist"] = macd(c)
    out["atr14"] = atr(out, 14)
    out["atr_pct"] = out["atr14"] / c * 100.0
    out["atr_pct_rank"] = pct_rank(out["atr_pct"], 252, 60)
    out["adx14"] = adx(out, 14)
    out["bbw"] = bollinger_width(c)
    out["bbw_rank"] = pct_rank(out["bbw"], 252, 60)
    vol_avg = sma(out["volume"], 20)
    out["vol_ratio"] = (out["volume"] / vol_avg).where(vol_avg > 0)
    return out


def add_intraday(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    c = out["close"]
    out["ema20"] = ema(c, 20)
    out["ema50"] = ema(c, 50)
    out["rsi14"] = rsi(c, 14)
    out["macd"], out["macd_signal"], out["macd_hist"] = macd(c)
    out["atr14"] = atr(out, 14)
    return out


def add_weekly(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    c = out["close"]
    out["ema10"] = ema(c, 10)
    out["ema40"] = ema(c, 40)
    out["rsi14"] = rsi(c, 14)
    return out


def last_values(df: pd.DataFrame, cols: list[str]) -> dict:
    if df.empty:
        return {c: None for c in cols}
    row = df.iloc[-1]
    return {c: (None if pd.isna(row.get(c)) else float(row.get(c))) for c in cols}
