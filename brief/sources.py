"""Price, derivatives, calendar and sentiment fetchers.

Candle fetchers return a DataFrame indexed by bar open time (tz-aware UTC),
float columns open, high, low, close, volume, sorted and de-duplicated.

Daily bars are indexed by trading date at 00:00 UTC. For crypto that is the
bar's real open time. For Yahoo and Stooq it is the exchange's trading date,
so a NVDA bar for 2026-10-20 is stored at 2026-10-20 00:00 UTC whatever time
the exchange opened.
"""

from __future__ import annotations

import io
import time as _time
from datetime import datetime, timedelta
from urllib.parse import quote

import pandas as pd

from .http import FetchError, get_json, get_text, post_json
from .timeutil import UTC, iso

COLUMNS = ["open", "high", "low", "close", "volume"]
INTERVALS = ("1d", "1h")


def _ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


def _frame(records: list[tuple], unit: str = "ms") -> pd.DataFrame:
    """records: (timestamp, open, high, low, close, volume)."""
    if not records:
        return pd.DataFrame(columns=COLUMNS, index=pd.DatetimeIndex([], tz=UTC))
    df = pd.DataFrame(records, columns=["t", *COLUMNS])
    df["t"] = pd.to_datetime(pd.to_numeric(df["t"]), unit=unit, utc=True)
    for c in COLUMNS:
        df[c] = pd.to_numeric(df[c], errors="coerce").astype("float64")
    df = df.dropna(subset=["open", "high", "low", "close"])
    df = df.drop_duplicates(subset="t", keep="last").set_index("t").sort_index()
    df.index.name = None
    df["volume"] = df["volume"].fillna(0.0)
    return df[COLUMNS]


def _clip(df: pd.DataFrame, start: datetime, end: datetime) -> pd.DataFrame:
    return df[(df.index >= pd.Timestamp(start)) & (df.index <= pd.Timestamp(end))]


# --------------------------------------------------------------------------
# Bitget (v2 public market data)
# --------------------------------------------------------------------------
BITGET = "https://api.bitget.com"
_BITGET_GRAN = {
    "futures": {"1d": "1Dutc", "1h": "1H"},
    "spot": {"1d": "1Dutc", "1h": "1h"},
}


def _bitget_call(url: str, params: dict) -> list:
    data = get_json(url, params=params)
    if not isinstance(data, dict) or str(data.get("code")) != "00000":
        msg = data.get("msg") if isinstance(data, dict) else str(data)[:120]
        raise FetchError(f"Bitget error: {msg}")
    return data.get("data") or []


def fetch_bitget(symbol: str, interval: str, start: datetime, end: datetime, market: str) -> pd.DataFrame:
    gran = _BITGET_GRAN[market][interval]
    if market == "futures":
        recent_url = f"{BITGET}/api/v2/mix/market/candles"
        hist_url = f"{BITGET}/api/v2/mix/market/history-candles"
        base = {"symbol": symbol, "productType": "USDT-FUTURES", "granularity": gran}
    else:
        recent_url = f"{BITGET}/api/v2/spot/market/candles"
        hist_url = f"{BITGET}/api/v2/spot/market/history-candles"
        base = {"symbol": symbol, "granularity": gran}

    rows = list(_bitget_call(recent_url, {**base, "limit": "1000"}))
    if not rows:
        raise FetchError(f"Bitget returned no candles for {symbol}")
    earliest = min(int(r[0]) for r in rows)
    start_ms = _ms(start)
    pages = 0
    while earliest > start_ms and pages < 90:
        batch = _bitget_call(hist_url, {**base, "endTime": str(earliest - 1), "limit": "200"})
        if not batch:
            break
        new_earliest = min(int(r[0]) for r in batch)
        if new_earliest >= earliest:
            break
        rows.extend(batch)
        earliest = new_earliest
        pages += 1
        _time.sleep(0.07)
    records = [(r[0], r[1], r[2], r[3], r[4], r[5]) for r in rows]
    return _clip(_frame(records), start, end)


# --------------------------------------------------------------------------
# Coinbase Exchange
# --------------------------------------------------------------------------
COINBASE = "https://api.exchange.coinbase.com"


def fetch_coinbase(symbol: str, interval: str, start: datetime, end: datetime) -> pd.DataFrame:
    gran = {"1d": 86400, "1h": 3600}[interval]
    step = timedelta(seconds=gran * 299)
    t0 = start
    records: list[tuple] = []
    while t0 < end:
        t1 = min(t0 + step, end)
        data = get_json(
            f"{COINBASE}/products/{symbol}/candles",
            params={"granularity": gran, "start": iso(t0), "end": iso(t1)},
        )
        if isinstance(data, dict):
            raise FetchError(f"Coinbase error: {data.get('message')}")
        # [time, low, high, open, close, volume]
        records.extend((r[0], r[3], r[2], r[1], r[4], r[5]) for r in data)
        t0 = t1
        _time.sleep(0.13)
    df = _frame(records, unit="s")
    if df.empty:
        raise FetchError(f"Coinbase returned no candles for {symbol}")
    return _clip(df, start, end)


# --------------------------------------------------------------------------
# Kraken (only the most recent 720 bars)
# --------------------------------------------------------------------------
def fetch_kraken(symbol: str, interval: str, start: datetime, end: datetime) -> pd.DataFrame:
    iv = {"1d": 1440, "1h": 60}[interval]
    data = get_json(
        "https://api.kraken.com/0/public/OHLC",
        params={"pair": symbol, "interval": iv, "since": int(start.timestamp())},
    )
    if data.get("error"):
        raise FetchError(f"Kraken error: {data['error']}")
    result = data.get("result") or {}
    keys = [k for k in result if k != "last"]
    if not keys:
        raise FetchError(f"Kraken returned no candles for {symbol}")
    # [time, open, high, low, close, vwap, volume, count]
    records = [(r[0], r[1], r[2], r[3], r[4], r[6]) for r in result[keys[0]]]
    return _clip(_frame(records, unit="s"), start, end)


# --------------------------------------------------------------------------
# Hyperliquid (only the most recent 5000 bars)
# --------------------------------------------------------------------------
HYPERLIQUID = "https://api.hyperliquid.xyz/info"


def fetch_hyperliquid(coin: str, interval: str, start: datetime, end: datetime) -> pd.DataFrame:
    bar = timedelta(days=1) if interval == "1d" else timedelta(hours=1)
    t0 = max(start, end - bar * 4990)
    records: list[tuple] = []
    while t0 < end:
        t1 = min(t0 + bar * 4000, end)
        data = post_json(
            HYPERLIQUID,
            {"type": "candleSnapshot", "req": {"coin": coin, "interval": interval, "startTime": _ms(t0), "endTime": _ms(t1)}},
        )
        if not isinstance(data, list):
            raise FetchError(f"Hyperliquid error: {str(data)[:120]}")
        records.extend((r["t"], r["o"], r["h"], r["l"], r["c"], r["v"]) for r in data)
        t0 = t1
        _time.sleep(0.1)
    df = _frame(records)
    if df.empty:
        raise FetchError(f"Hyperliquid returned no candles for {coin}")
    return _clip(df, start, end)


# --------------------------------------------------------------------------
# Yahoo Finance chart API
# --------------------------------------------------------------------------
def fetch_yahoo(symbol: str, interval: str, start: datetime, end: datetime, prepost: bool = False) -> pd.DataFrame:
    if interval == "1h":
        start = max(start, end - timedelta(days=729))
    params = {
        "period1": int(start.timestamp()),
        "period2": int(end.timestamp()),
        "interval": {"1d": "1d", "1h": "60m"}[interval],
        "includePrePost": "true" if prepost else "false",
        "events": "div,splits",
    }
    last_err: Exception | None = None
    data = None
    for host in ("query1", "query2"):
        try:
            data = get_json(f"https://{host}.finance.yahoo.com/v8/finance/chart/{quote(symbol)}", params=params)
            break
        except FetchError as exc:
            last_err = exc
    if data is None:
        raise last_err or FetchError("Yahoo unreachable")
    chart = data.get("chart") or {}
    if chart.get("error"):
        raise FetchError(f"Yahoo error: {chart['error']}")
    result = (chart.get("result") or [None])[0]
    if not result or not result.get("timestamp"):
        raise FetchError(f"Yahoo returned no candles for {symbol}")
    quote_ = result["indicators"]["quote"][0]
    df = pd.DataFrame(
        {c: pd.to_numeric(pd.Series(quote_.get(c)), errors="coerce").astype("float64") for c in COLUMNS},
    )
    df.index = pd.to_datetime(pd.Series(result["timestamp"]), unit="s", utc=True)
    df = df.dropna(subset=["open", "high", "low", "close"])
    df["volume"] = df["volume"].fillna(0.0)
    if interval == "1d":
        tzname = (result.get("meta") or {}).get("exchangeTimezoneName") or "America/New_York"
        local_dates = df.index.tz_convert(tzname).date
        df.index = pd.DatetimeIndex(pd.to_datetime(local_dates)).tz_localize(UTC)
    df = df[~df.index.duplicated(keep="last")].sort_index()
    df.index.name = None
    return _clip(df[COLUMNS], start, end + timedelta(days=1))


# --------------------------------------------------------------------------
# Yahoo Finance through the yfinance library (browser-like requests, which
# Yahoo blocks less than plain HTTP clients)
# --------------------------------------------------------------------------
def fetch_yfinance(symbol: str, interval: str, start: datetime, end: datetime) -> pd.DataFrame:
    try:
        import yfinance as yf
    except ImportError as exc:  # pragma: no cover
        raise FetchError("yfinance is not installed") from exc
    try:
        t = yf.Ticker(symbol)
        if interval == "1d":
            df = t.history(start=start.date().isoformat(), end=(end + timedelta(days=1)).date().isoformat(),
                           interval="1d", auto_adjust=False, actions=False, raise_errors=True, timeout=20)
        else:
            s = max(start, end - timedelta(days=729))
            df = t.history(start=s, end=end, interval="60m", prepost=True, auto_adjust=False,
                           actions=False, raise_errors=True, timeout=20)
    except Exception as exc:  # yfinance raises many types
        raise FetchError(f"yfinance {symbol}: {type(exc).__name__}: {str(exc)[:150]}") from exc
    if df is None or df.empty:
        raise FetchError(f"yfinance returned no candles for {symbol}")
    df = df.rename(columns=str.lower)
    df = df[[c for c in COLUMNS if c in df.columns]].astype("float64")
    if "volume" not in df:
        df["volume"] = 0.0
    idx = df.index
    if interval == "1d":
        dates = idx.date
        df.index = pd.DatetimeIndex(pd.to_datetime(dates)).tz_localize(UTC)
    else:
        df.index = idx.tz_convert(UTC) if idx.tz is not None else idx.tz_localize(UTC)
    df = df.dropna(subset=["open", "high", "low", "close"])
    df["volume"] = df["volume"].fillna(0.0)
    df = df[~df.index.duplicated(keep="last")].sort_index()
    df.index.name = None
    return _clip(df[COLUMNS], start, end + timedelta(days=1))


# --------------------------------------------------------------------------
# Stooq (daily CSV only)
# --------------------------------------------------------------------------
def fetch_stooq(symbol: str, interval: str, start: datetime, end: datetime) -> pd.DataFrame:
    if interval != "1d":
        raise FetchError("Stooq provides daily bars only")
    text = get_text(
        "https://stooq.com/q/d/l/",
        params={"s": symbol, "i": "d", "d1": start.strftime("%Y%m%d"), "d2": end.strftime("%Y%m%d")},
    )
    if not text.startswith("Date"):
        raise FetchError(f"Stooq: {text[:80].strip()!r}")
    raw = pd.read_csv(io.StringIO(text))
    if raw.empty:
        raise FetchError(f"Stooq returned no rows for {symbol}")
    raw.columns = [c.strip().lower() for c in raw.columns]
    if "volume" not in raw:
        raw["volume"] = 0.0
    df = raw[["open", "high", "low", "close", "volume"]].apply(pd.to_numeric, errors="coerce").astype("float64")
    df.index = pd.DatetimeIndex(pd.to_datetime(raw["date"])).tz_localize(UTC)
    df = df.dropna(subset=["open", "high", "low", "close"])
    df["volume"] = df["volume"].fillna(0.0)
    df.index.name = None
    return df[~df.index.duplicated(keep="last")].sort_index()


# --------------------------------------------------------------------------
# Dispatcher
# --------------------------------------------------------------------------
DAILY_ONLY = {"stooq"}


def fetch_candles(provider: str, symbol: str, interval: str, start: datetime, end: datetime) -> pd.DataFrame:
    if interval not in INTERVALS:
        raise ValueError(interval)
    if provider == "bitget_futures":
        return fetch_bitget(symbol, interval, start, end, "futures")
    if provider == "bitget_spot":
        return fetch_bitget(symbol, interval, start, end, "spot")
    if provider == "coinbase":
        return fetch_coinbase(symbol, interval, start, end)
    if provider == "kraken":
        return fetch_kraken(symbol, interval, start, end)
    if provider == "hyperliquid":
        return fetch_hyperliquid(symbol, interval, start, end)
    if provider == "yahoo":
        return fetch_yahoo(symbol, interval, start, end, prepost=interval == "1h")
    if provider == "yfinance":
        return fetch_yfinance(symbol, interval, start, end)
    if provider == "stooq":
        return fetch_stooq(symbol, interval, start, end)
    raise ValueError(f"unknown provider {provider!r}")


# --------------------------------------------------------------------------
# Derivatives: funding and open interest
# --------------------------------------------------------------------------
_HL_CTX_CACHE: dict = {}


def _hl_contexts() -> dict[str, dict]:
    if not _HL_CTX_CACHE:
        meta, ctxs = post_json(HYPERLIQUID, {"type": "metaAndAssetCtxs"})
        for u, c in zip(meta["universe"], ctxs):
            _HL_CTX_CACHE[u["name"]] = c
    return _HL_CTX_CACHE


def fetch_derivatives(provider: str, symbol: str) -> dict:
    if provider == "bitget_futures":
        params = {"symbol": symbol, "productType": "USDT-FUTURES"}
        fr = _bitget_call(f"{BITGET}/api/v2/mix/market/current-fund-rate", params)
        rate = float(fr[0]["fundingRate"])
        interval_h = float(fr[0].get("fundingRateInterval") or 8)
        oi = get_json(f"{BITGET}/api/v2/mix/market/open-interest", params=params)
        if str(oi.get("code")) != "00000":
            raise FetchError(f"Bitget OI error: {oi.get('msg')}")
        size = float(oi["data"]["openInterestList"][0]["size"])
        px = _bitget_call(f"{BITGET}/api/v2/mix/market/symbol-price", params)
        mark = float(px[0]["markPrice"])
    elif provider == "hyperliquid":
        ctx = _hl_contexts().get(symbol)
        if ctx is None:
            raise FetchError(f"Hyperliquid has no perp {symbol}")
        rate = float(ctx["funding"])
        interval_h = 1.0
        size = float(ctx["openInterest"])
        mark = float(ctx["markPx"])
    else:
        raise ValueError(f"no derivatives for provider {provider!r}")
    return {
        "provider": provider,
        "symbol": symbol,
        "funding_rate": rate,
        "funding_interval_h": interval_h,
        "funding_8h_pct": rate * (8.0 / interval_h) * 100.0,
        "funding_apr_pct": rate * (24.0 / interval_h) * 365.0 * 100.0,
        "open_interest": size,
        "open_interest_usd": size * mark,
        "mark_price": mark,
    }


# --------------------------------------------------------------------------
# Economic calendar (Forex Factory weekly feed) and crypto Fear & Greed
# --------------------------------------------------------------------------
FF_THIS_WEEK = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
FF_NEXT_WEEK = "https://nfs.faireconomy.media/ff_calendar_nextweek.json"

_KEEP_COUNTRIES_ALL_IMPACT = {"USD"}
_KEEP_COUNTRIES_HIGH = {"EUR", "GBP", "JPY", "CNY", "CHF", "CAD", "AUD"}


def _calendar_rows(url: str) -> list[dict]:
    data = get_json(url)
    rows = []
    for e in data if isinstance(data, list) else []:
        try:
            t = datetime.fromisoformat(str(e["date"]))
        except (KeyError, ValueError):
            continue
        impact = str(e.get("impact") or "")
        country = str(e.get("country") or "")
        keep = (country in _KEEP_COUNTRIES_ALL_IMPACT and impact in {"High", "Medium", "Holiday"}) or (
            country in _KEEP_COUNTRIES_HIGH and impact == "High"
        )
        if not keep:
            continue
        rows.append(
            {
                "time_utc": iso(t),
                "country": country,
                "title": str(e.get("title") or "").strip(),
                "impact": impact,
                "forecast": str(e.get("forecast") or ""),
                "previous": str(e.get("previous") or ""),
            }
        )
    return rows


def fetch_calendar(include_next_week: bool = False) -> list[dict]:
    rows = _calendar_rows(FF_THIS_WEEK)
    if include_next_week:
        try:
            rows += _calendar_rows(FF_NEXT_WEEK)
        except FetchError:
            pass
    seen = set()
    unique = []
    for r in sorted(rows, key=lambda r: r["time_utc"]):
        key = (r["time_utc"], r["country"], r["title"])
        if key not in seen:
            seen.add(key)
            unique.append(r)
    return unique


def fetch_fear_greed() -> dict:
    data = get_json("https://api.alternative.me/fng/", params={"limit": 2})
    rows = data.get("data") or []
    if not rows:
        raise FetchError("Fear & Greed returned nothing")
    now = rows[0]
    prev = rows[1] if len(rows) > 1 else rows[0]
    return {
        "value": int(now["value"]),
        "label": now["value_classification"],
        "time_utc": iso(datetime.fromtimestamp(int(now["timestamp"]), UTC)),
        "previous": int(prev["value"]),
    }
