"""Build the data snapshot that the Claude runs read.

    python -m brief.snapshot --prev PREV_DIR --out OUT_DIR [--mode auto|full|quick] [--now ISO] [--offline]

PREV_DIR is the previous contents of the `data` branch (may be missing).
OUT_DIR receives snapshot.json, candles.json, STATUS.md and history/.

Full mode refetches every asset's whole history from its preferred source.
Quick mode fetches only recent bars and merges them into the cached history,
keeping whichever source worked last time so one asset's history never mixes
two exchanges. Auto picks full for the first run after the crypto daily close
(00:00 UTC) or when the last full run is more than 20 hours old.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
import yaml

from .analyze import analyze_asset
from .http import FetchError
from .levels import rnd
from .sources import DAILY_ONLY, fetch_calendar, fetch_candles, fetch_derivatives, fetch_fear_greed
from .timeutil import iso, last_bar_closed_by, local_label, parse_ts, trading_days_between, utcnow

ROOT = Path(__file__).resolve().parent.parent
CONFIG = ROOT / "config" / "assets.yaml"
SCHEMA = 1


def load_config(path: Path | str = CONFIG) -> dict:
    with open(path, encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def horizons_for(cfg: dict, acfg: dict) -> dict:
    return dict(cfg["horizons"]["crypto" if acfg["calendar"] == "crypto" else "default"])


# ---------------------------------------------------------------- history io
def _hist_path(base: Path, aid: str, iv: str) -> Path:
    return Path(base) / "history" / f"{aid}_{iv}.csv.gz"


def read_hist(base: Path | None, aid: str, iv: str) -> pd.DataFrame | None:
    if base is None:
        return None
    p = _hist_path(base, aid, iv)
    if not p.exists():
        return None
    df = pd.read_csv(p, index_col=0)
    df.index = pd.to_datetime(df.index, utc=True)
    df.index.name = None
    return df.astype("float64")


def write_hist(base: Path, aid: str, iv: str, df: pd.DataFrame) -> None:
    p = _hist_path(base, aid, iv)
    p.parent.mkdir(parents=True, exist_ok=True)
    out = df.copy()
    out.index = [iso(ts.to_pydatetime()) for ts in out.index]
    out.to_csv(p, float_format="%.10g", compression="gzip")


def merge(old: pd.DataFrame | None, new: pd.DataFrame | None) -> pd.DataFrame | None:
    if old is None or old.empty:
        return new
    if new is None or new.empty:
        return old
    both = pd.concat([old, new])
    return both[~both.index.duplicated(keep="last")].sort_index()


def read_json(path: Path) -> dict:
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def needs_full(prev_meta: dict, now: datetime) -> bool:
    last_full = prev_meta.get("last_full")
    if not last_full:
        return True
    last_full = parse_ts(last_full)
    if now - last_full > timedelta(hours=20):
        return True
    return now.hour == 0 and last_full.date() < now.date()


# ---------------------------------------------------------------- loading
def _check_fresh(daily: pd.DataFrame, calendar: str, now: datetime) -> None:
    if daily is None or daily.empty:
        raise FetchError("no daily bars")
    last = daily.index[-1].date()
    if calendar == "crypto":
        if (now.date() - last).days > 1:
            raise FetchError(f"stale: last daily bar {last}")
    else:
        expected = last_bar_closed_by(calendar, now)
        if trading_days_between(calendar, last, expected) > 3:
            raise FetchError(f"stale: last daily bar {last}")


def load_asset(acfg: dict, cfg: dict, prev_dir: Path | None, prev_meta: dict, full: bool,
               now: datetime, offline: bool) -> tuple:
    aid = acfg["id"]
    years = int(cfg["history"]["daily_years"])
    hdays = int(cfg["history"]["hourly_days"])
    prev_key = (prev_meta.get("assets") or {}).get(aid, {}).get("source_key")
    sources = list(acfg["sources"])
    if not full and prev_key:
        sources.sort(key=lambda s: 0 if f"{s['provider']}:{s['symbol']}" == prev_key else 1)
    errors: list[str] = []
    for src in sources:
        key = f"{src['provider']}:{src['symbol']}"
        daily_only = src["provider"] in DAILY_ONLY
        try:
            hourly = None
            if offline:
                if key != prev_key:
                    raise FetchError("offline and no cached history for this source")
                daily = read_hist(prev_dir, aid, "1d")
                hourly = None if daily_only else read_hist(prev_dir, aid, "1h")
            elif not full and key == prev_key and read_hist(prev_dir, aid, "1d") is not None:
                daily = merge(read_hist(prev_dir, aid, "1d"),
                              fetch_candles(src["provider"], src["symbol"], "1d", now - timedelta(days=15), now))
                if not daily_only:
                    old_h = read_hist(prev_dir, aid, "1h")
                    try:
                        new_h = fetch_candles(src["provider"], src["symbol"], "1h", now - timedelta(days=3), now)
                        hourly = merge(old_h, new_h)
                    except FetchError as exc:
                        errors.append(f"{aid} {key} hourly: {exc}")
                        hourly = old_h
            else:
                daily = fetch_candles(src["provider"], src["symbol"], "1d", now - timedelta(days=365 * years), now)
                if not daily_only:
                    try:
                        hourly = fetch_candles(src["provider"], src["symbol"], "1h", now - timedelta(days=hdays), now)
                    except FetchError as exc:
                        errors.append(f"{aid} {key} hourly: {exc}")
            if daily is None:
                raise FetchError("no daily bars")
            daily = daily[daily.index >= pd.Timestamp(now - timedelta(days=365 * years))]
            if hourly is not None:
                hourly = hourly[hourly.index >= pd.Timestamp(now - timedelta(days=hdays))]
            _check_fresh(daily, acfg["calendar"], now)
            source = {"provider": src["provider"], "symbol": src["symbol"], "label": src.get("label", key),
                      "key": key, "daily_only": daily_only or hourly is None}
            return daily, hourly, source, errors
        except (FetchError, KeyError, ValueError, TypeError, IndexError) as exc:
            errors.append(f"{aid} {key}: {exc}")
    return None, None, None, errors


def load_derivatives(acfg: dict) -> tuple[dict | None, list[str]]:
    errors = []
    for d in acfg.get("derivatives") or []:
        try:
            return fetch_derivatives(d["provider"], d["symbol"]), errors
        except (FetchError, KeyError, ValueError, TypeError, IndexError) as exc:
            errors.append(f"{acfg['id']} derivatives {d['provider']}: {exc}")
    return None, errors


def _oi_change(oi_hist: pd.DataFrame, aid: str, provider: str, now: datetime, oi_now: float) -> float | None:
    if oi_hist is None or oi_hist.empty:
        return None
    sub = oi_hist[(oi_hist["asset"] == aid) & (oi_hist["provider"] == provider)]
    if sub.empty:
        return None
    target = pd.Timestamp(now - timedelta(hours=24))
    times = pd.to_datetime(sub["time"], utc=True)
    window = sub[(times >= target - pd.Timedelta(hours=3)) & (times <= target + pd.Timedelta(hours=3))]
    if window.empty:
        return None
    wtimes = pd.to_datetime(window["time"], utc=True)
    row = window.iloc[(wtimes - target).abs().argmin()]
    prev = float(row["oi_usd"])
    return round((oi_now / prev - 1) * 100, 2) if prev > 0 else None


# ---------------------------------------------------------------- build
def build(prev_dir: Path | None, out_dir: Path, mode: str = "auto", now: datetime | None = None,
          offline: bool = False, cfg: dict | None = None) -> dict:
    cfg = cfg or load_config()
    now = (now or utcnow()).replace(microsecond=0)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    prev_meta = read_json(Path(prev_dir) / "history" / "meta.json") if prev_dir else {}
    prev_snap = read_json(Path(prev_dir) / "snapshot.json") if prev_dir else {}
    full = (mode == "full") or (mode == "auto" and needs_full(prev_meta, now))
    if offline:
        full = False

    errors: list[str] = []
    assets: dict = {}
    candles: dict = {}
    meta = {"assets": {}, "last_full": iso(now) if full else prev_meta.get("last_full")}
    oi_hist = None
    if prev_dir and (Path(prev_dir) / "history" / "oi.csv.gz").exists():
        oi_hist = pd.read_csv(Path(prev_dir) / "history" / "oi.csv.gz")
    oi_rows = []

    for acfg in cfg["assets"]:
        aid = acfg["id"]
        daily, hourly, source, errs = load_asset(acfg, cfg, prev_dir, prev_meta, full, now, offline)
        errors += errs
        if daily is None:
            assets[aid] = {"id": aid, "name": acfg["name"], "group": acfg["group"], "calendar": acfg["calendar"],
                           "calls": bool(acfg.get("calls")), "status": "missing", "notes": ["no source returned data"]}
            continue
        deriv = None
        if acfg.get("derivatives") and not offline:
            deriv, derrs = load_derivatives(acfg)
            errors += derrs
            if deriv:
                deriv["oi_change_24h_pct"] = _oi_change(oi_hist, aid, deriv["provider"], now, deriv["open_interest_usd"])
                oi_rows.append({"time": iso(now), "asset": aid, "provider": deriv["provider"],
                                "oi_usd": deriv["open_interest_usd"], "funding_8h_pct": deriv["funding_8h_pct"]})
                for k in ("funding_8h_pct", "funding_apr_pct"):
                    deriv[k] = round(deriv[k], 4)
                deriv["open_interest_usd"] = round(deriv["open_interest_usd"])
        try:
            entry, cnd = analyze_asset(acfg, daily, hourly, now, horizons_for(cfg, acfg), source, deriv,
                                       int(cfg["history"]["hourly_days"]))
        except Exception as exc:  # keep the other assets going
            errors.append(f"{aid} analysis failed: {type(exc).__name__}: {exc}")
            entry, cnd = {"id": aid, "name": acfg["name"], "group": acfg["group"], "calendar": acfg["calendar"],
                          "calls": bool(acfg.get("calls")), "status": "missing", "notes": ["analysis failed"]}, {}
        if acfg.get("compare") and not offline and entry.get("status") == "ok":
            cmp = acfg["compare"]
            try:
                h = fetch_candles(cmp["provider"], cmp["symbol"], "1h", now - timedelta(hours=6), now)
                cp = float(h["close"].iloc[-1])
                last = entry["price"]["last"]
                entry["compare"] = {"label": cmp.get("label", cmp["symbol"]), "price": rnd(cp, last),
                                    "diff": rnd(cp - last, last), "diff_pct": round((cp / last - 1) * 100, 2)}
            except (FetchError, KeyError, ValueError, TypeError, IndexError) as exc:
                errors.append(f"{aid} compare {cmp['provider']}: {exc}")
        assets[aid] = entry
        if cnd:
            candles[aid] = cnd
        write_hist(out_dir, aid, "1d", daily)
        if hourly is not None and not hourly.empty:
            write_hist(out_dir, aid, "1h", hourly)
        meta["assets"][aid] = {"source_key": source["key"], "label": source["label"]}

    # relative strength
    def rel(a: str, b: str, key: str = "chg_5d_pct"):
        try:
            x = assets[a]["price"][key]
            y = assets[b]["price"][key]
            return None if x is None or y is None else round(x - y, 2)
        except KeyError:
            return None

    for aid, e in assets.items():
        if e.get("status") != "ok":
            continue
        if e["group"] == "crypto" and aid != "BTC":
            e["relative"] = {"vs": "BTC", "pp_5d": rel(aid, "BTC")}
        elif e["group"] in ("ai_chips", "memory"):
            e["relative"] = {"vs": "SMH", "pp_5d": rel(aid, "SMH")}

    # calendar and sentiment
    calendar = prev_snap.get("calendar") or []
    cal_at = prev_snap.get("calendar_fetched_at")
    stale_cal = not cal_at or (now - parse_ts(cal_at)) > timedelta(hours=3) or full
    if not offline and stale_cal:
        try:
            calendar = fetch_calendar(include_next_week=now.weekday() >= 4)
            cal_at = iso(now)
        except FetchError as exc:
            errors.append(f"calendar: {exc}")
    fng = prev_snap.get("fear_greed")
    if not offline:
        try:
            fng = fetch_fear_greed()
        except FetchError as exc:
            errors.append(f"fear & greed: {exc}")

    # OI history: keep 8 days
    if oi_rows:
        new = pd.DataFrame(oi_rows)
        oi_all = pd.concat([oi_hist, new]) if oi_hist is not None else new
        times = pd.to_datetime(oi_all["time"], utc=True)
        oi_all = oi_all[times >= pd.Timestamp(now - timedelta(days=8))]
        (out_dir / "history").mkdir(parents=True, exist_ok=True)
        oi_all.to_csv(out_dir / "history" / "oi.csv.gz", index=False, compression="gzip")
    elif prev_dir and (Path(prev_dir) / "history" / "oi.csv.gz").exists():
        (out_dir / "history").mkdir(parents=True, exist_ok=True)
        (out_dir / "history" / "oi.csv.gz").write_bytes((Path(prev_dir) / "history" / "oi.csv.gz").read_bytes())

    for e in calendar:
        e["time_local"] = local_label(parse_ts(e["time_utc"]))

    snapshot = {
        "schema": SCHEMA,
        "generated_at": iso(now),
        "generated_local": local_label(now),
        "mode": "full" if full else "quick",
        "display_timezone": cfg.get("display_timezone", "Asia/Phnom_Penh"),
        "order": [a["id"] for a in cfg["assets"]],
        "assets": assets,
        "calendar": calendar,
        "calendar_fetched_at": cal_at,
        "fear_greed": fng,
        "errors": errors,
    }
    with open(out_dir / "snapshot.json", "w", encoding="utf-8") as fh:
        json.dump(snapshot, fh, indent=1, ensure_ascii=False, allow_nan=False)
    with open(out_dir / "candles.json", "w", encoding="utf-8") as fh:
        json.dump({"schema": SCHEMA, "generated_at": iso(now), "assets": candles}, fh, separators=(",", ":"), allow_nan=False)
    (out_dir / "history").mkdir(parents=True, exist_ok=True)
    with open(out_dir / "history" / "meta.json", "w", encoding="utf-8") as fh:
        json.dump(meta, fh, indent=1)
    (out_dir / "STATUS.md").write_text(status_markdown(snapshot), encoding="utf-8")
    return snapshot


def status_markdown(s: dict) -> str:
    lines = [f"# Data snapshot {s['generated_at']} ({s['mode']})", "",
             "| Asset | Status | Source | Last | Last daily bar | Notes |", "|---|---|---|---|---|---|"]
    for aid in s["order"]:
        e = s["assets"].get(aid, {})
        src = (e.get("source") or {}).get("label", "-")
        price = (e.get("price") or {}).get("last", "-")
        bar = (e.get("price") or {}).get("ref_close_date", "-")
        lines.append(f"| {aid} | {e.get('status')} | {src} | {price} | {bar} | {'; '.join(e.get('notes') or [])} |")
    lines += ["", f"Calendar events: {len(s.get('calendar') or [])} (fetched {s.get('calendar_fetched_at')})", ""]
    if s["errors"]:
        lines += ["## Errors", ""] + [f"- {e}" for e in s["errors"]]
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--prev", type=Path, default=None)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--mode", choices=["auto", "full", "quick"], default="auto")
    ap.add_argument("--now", default=None, help="override the clock (ISO time), for tests")
    ap.add_argument("--offline", action="store_true", help="use cached history only")
    ap.add_argument("--config", type=Path, default=CONFIG)
    args = ap.parse_args(argv)
    now = parse_ts(args.now) if args.now else utcnow()
    prev = args.prev if args.prev and args.prev.exists() else None
    snap = build(prev, args.out, args.mode, now, args.offline, load_config(args.config))
    ok = sum(1 for e in snap["assets"].values() if e.get("status") == "ok")
    total = len(snap["assets"])
    print(f"snapshot {snap['generated_at']} mode={snap['mode']} assets ok={ok}/{total} errors={len(snap['errors'])}")
    for e in snap["errors"]:
        print("  -", e)
    return 0 if ok >= total // 2 else 2


if __name__ == "__main__":
    sys.exit(main())
