"""Check which data sources answer from GitHub's servers.

    python scripts/probe_sources.py --out probe

Tries every price source and derivatives source in config/assets.yaml, plus a
few extra candidates (spot-gold proxies), the economic calendar and the Fear &
Greed index. Writes probe/report.md and probe/report.json.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from brief.http import FetchError  # noqa: E402
from brief.snapshot import load_config  # noqa: E402
from brief.sources import (  # noqa: E402
    DAILY_ONLY,
    fetch_calendar,
    fetch_candles,
    fetch_derivatives,
    fetch_fear_greed,
)
from brief.timeutil import iso, utcnow  # noqa: E402

EXTRA = [
    # spot-gold candidates (tokens that track spot gold), with full history depth
    {"asset": "XAU (spot candidates)", "provider": "bitget_futures", "symbol": "XAUTUSDT", "deep": True},
    {"asset": "XAU (spot candidates)", "provider": "bitget_futures", "symbol": "PAXGUSDT", "deep": True},
    {"asset": "XAU (spot candidates)", "provider": "coinbase", "symbol": "PAXG-USD", "deep": True},
    {"asset": "XAU (spot candidates)", "provider": "hyperliquid", "symbol": "PAXG", "deep": True},
    {"asset": "XAU (spot candidates)", "provider": "yfinance", "symbol": "XAUUSD=X", "deep": True},
    # history depth on the main sources for the short-history coins
    {"asset": "HYPE depth", "provider": "bitget_futures", "symbol": "HYPEUSDT", "deep": True},
    {"asset": "ZEC depth", "provider": "bitget_futures", "symbol": "ZECUSDT", "deep": True},
    {"asset": "SOL depth", "provider": "bitget_futures", "symbol": "SOLUSDT", "deep": True},
    {"asset": "NVDA depth", "provider": "yfinance", "symbol": "NVDA", "deep": True},
]


def probe_candles(provider: str, symbol: str, deep: bool) -> dict:
    now = utcnow()
    out = {"provider": provider, "symbol": symbol}
    t0 = time.time()
    try:
        start = now - timedelta(days=3650 if deep else 20)
        d = fetch_candles(provider, symbol, "1d", start, now)
        out["daily_ok"] = True
        out["daily_bars"] = len(d)
        out["daily_first"] = d.index[0].date().isoformat() if len(d) else None
        out["daily_last"] = d.index[-1].date().isoformat() if len(d) else None
        out["last_close"] = float(d["close"].iloc[-1]) if len(d) else None
    except (FetchError, KeyError, ValueError, TypeError, IndexError) as exc:
        out["daily_ok"] = False
        out["daily_error"] = str(exc)[:200]
    if provider not in DAILY_ONLY:
        try:
            h = fetch_candles(provider, symbol, "1h", now - timedelta(days=3), now)
            out["hourly_ok"] = True
            out["hourly_last"] = iso(h.index[-1].to_pydatetime()) if len(h) else None
        except (FetchError, KeyError, ValueError, TypeError, IndexError) as exc:
            out["hourly_ok"] = False
            out["hourly_error"] = str(exc)[:200]
    out["seconds"] = round(time.time() - t0, 1)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", type=Path, default=Path("probe"))
    args = ap.parse_args()
    cfg = load_config()
    results = {"generated_at": iso(utcnow()), "candles": [], "derivatives": [], "other": {}}
    deep_done = set()
    for a in cfg["assets"]:
        for s in a["sources"]:
            deep = s["provider"] not in deep_done and a["calendar"] == "crypto"
            r = probe_candles(s["provider"], s["symbol"], deep)
            if deep and r.get("daily_ok"):
                deep_done.add(s["provider"])
            r["asset"] = a["id"]
            results["candles"].append(r)
            print(f"{a['id']:6} {s['provider']:15} {s['symbol']:10} daily={r.get('daily_ok')} hourly={r.get('hourly_ok')}",
                  r.get("daily_error", "") or r.get("hourly_error", ""), flush=True)
        for d in a.get("derivatives") or []:
            try:
                v = fetch_derivatives(d["provider"], d["symbol"])
                results["derivatives"].append({"asset": a["id"], "provider": d["provider"], "ok": True,
                                               "funding_8h_pct": round(v["funding_8h_pct"], 5),
                                               "oi_usd": round(v["open_interest_usd"])})
            except (FetchError, KeyError, ValueError, TypeError, IndexError) as exc:
                results["derivatives"].append({"asset": a["id"], "provider": d["provider"], "ok": False,
                                               "error": str(exc)[:200]})
    for x in EXTRA:
        r = probe_candles(x["provider"], x["symbol"], bool(x.get("deep")))
        r["asset"] = x["asset"]
        results["candles"].append(r)
    try:
        cal = fetch_calendar()
        results["other"]["calendar"] = {"ok": True, "events": len(cal), "first": cal[0] if cal else None}
    except FetchError as exc:
        results["other"]["calendar"] = {"ok": False, "error": str(exc)[:200]}
    try:
        results["other"]["fear_greed"] = {"ok": True, **fetch_fear_greed()}
    except FetchError as exc:
        results["other"]["fear_greed"] = {"ok": False, "error": str(exc)[:200]}

    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "report.json").write_text(json.dumps(results, indent=1))
    lines = [f"# Source probe {results['generated_at']}", "",
             "| Asset | Provider | Symbol | Daily | Bars | First | Last | Close | Hourly | Error |",
             "|---|---|---|---|---|---|---|---|---|---|"]
    for r in results["candles"]:
        err = r.get("daily_error") or r.get("hourly_error") or ""
        lines.append(f"| {r['asset']} | {r['provider']} | {r['symbol']} | {'ok' if r.get('daily_ok') else 'FAIL'} | "
                     f"{r.get('daily_bars', '')} | {r.get('daily_first', '')} | {r.get('daily_last', '')} | "
                     f"{r.get('last_close', '')} | {'' if 'hourly_ok' not in r else 'ok' if r['hourly_ok'] else 'FAIL'} | "
                     f"{err[:90]} |")
    lines += ["", "## Derivatives", "", "| Asset | Provider | OK | Funding %/8h | OI USD | Error |", "|---|---|---|---|---|---|"]
    for d in results["derivatives"]:
        lines.append(f"| {d['asset']} | {d['provider']} | {d['ok']} | {d.get('funding_8h_pct', '')} | "
                     f"{d.get('oi_usd', '')} | {d.get('error', '')[:90]} |")
    lines += ["", "## Other", "", "```", json.dumps(results["other"], indent=1)[:2000], "```"]
    (args.out / "report.md").write_text("\n".join(lines) + "\n")
    print("\n".join(lines[:5]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
