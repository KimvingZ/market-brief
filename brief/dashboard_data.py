"""Build the dashboard's data file (bundle.json) from the snapshot, candles,
this run's calls and narrative, and the graded ledger.

bundle.json is {"rows": [...]}, every row tagged with a "kind":

  meta         one row: this run, data time, headline, banner, next run
  asset        one row per asset: price, TA readout, levels, latest calls, narrative
  level        support/resistance levels per asset
  candle       last 90 daily bars per asset (+ the bar in progress)
  event        economic calendar
  score        scorecard per horizon
  calib        base-rate calibration buckets
  asset_score  hit rate per asset and horizon
  conf_score   hit rate per confidence level
"""

from __future__ import annotations

import json
from datetime import datetime, time, timedelta

from .calls import RUN_LABELS
from .grade import scorecard
from .timeutil import LONDON, NEW_YORK, PHNOM_PENH, iso, local_label, parse_ts

GROUP_LABELS = {"crypto": "Crypto", "commodity": "Gold & oil", "ai_chips": "AI chips",
                "memory": "Memory & storage", "context": "Market context"}
HORIZONS = ("today", "short", "long", "session")

SCHEDULE = [  # (run type, tz, hh, mm, weekdays only)
    ("full", PHNOM_PENH, 7, 35, False),
    ("london", LONDON, 7, 20, True),
    ("ny", NEW_YORK, 8, 47, True),
]


def next_run(after: datetime) -> tuple[str, datetime]:
    best = None
    for rt, tz, hh, mm, weekdays in SCHEDULE:
        local = after.astimezone(tz)
        for add in range(0, 8):
            d = local.date() + timedelta(days=add)
            if weekdays and d.weekday() >= 5:
                continue
            t = datetime.combine(d, time(hh, mm), tz)
            if t > after:
                if best is None or t < best[1]:
                    best = (rt, t)
                break
    return best


def _latest_calls(ledger: list[dict]) -> dict:
    """Latest call per (asset, horizon): today/short/long from full runs, session from updates."""
    out: dict = {}
    for r in ledger:
        if r.get("kind") != "call":
            continue
        k = (r["asset"], r["horizon"])
        if k not in out or r["made_at"] > out[k]["made_at"]:
            out[k] = r
    return out


def _latest_notes(ledger: list[dict]) -> tuple[dict, dict]:
    full, upd = {}, {}
    for r in ledger:
        if r.get("kind") != "note":
            continue
        tgt = full if r["run_type"] == "full" else upd
        if r["asset"] not in tgt or r["made_at"] > tgt[r["asset"]]["made_at"]:
            tgt[r["asset"]] = r
    return full, upd


def note_rows(draft: dict, narrative: dict) -> list[dict]:
    rows = []
    for aid, a in (narrative.get("assets") or {}).items():
        rows.append({
            "kind": "note", "id": f"{draft['run_id']}|{aid}", "run_id": draft["run_id"],
            "run_type": draft["run_type"], "made_at": draft["made_at"], "made_local": draft["made_local"],
            "asset": aid, "summary": a.get("summary", ""), "what_would_change": a.get("what_would_change", ""),
            "changed": a.get("changed", ""), "drivers": json.dumps(a.get("drivers") or [], ensure_ascii=False),
        })
    return rows


def _vol_regime(rank):
    if rank is None:
        return None
    return "calm" if rank < 1 / 3 else "wild" if rank > 2 / 3 else "normal"


def build_bundle(snapshot: dict, candles: dict, draft: dict, narrative: dict, ledger: list[dict]) -> dict:
    made_at = parse_ts(draft["made_at"])
    gen = parse_ts(snapshot["generated_at"])
    age_min = round((made_at - gen).total_seconds() / 60)
    rows: list[dict] = []
    banner = []
    if age_min > 45:
        banner.append(f"Data delayed: prices are from {local_label(gen)} ({age_min} min before this report).")
    missing = [a for a in snapshot["order"] if snapshot["assets"].get(a, {}).get("status") != "ok"]
    if missing:
        banner.append("No price data for: " + ", ".join(missing) + ".")
    nrt, nt = next_run(made_at)
    fg = snapshot.get("fear_greed") or {}
    run_row = next((r for r in ledger if r.get("kind") == "run" and r.get("id") == draft["run_id"]), {})
    rows.append({
        "kind": "meta", "run_id": draft["run_id"], "run_type": draft["run_type"],
        "run_label": run_row.get("run_label") or RUN_LABELS[draft["run_type"]],
        "made_at": draft["made_at"], "made_local": draft["made_local"],
        "headline": narrative.get("headline", ""), "overview": narrative.get("overview", ""),
        "data_generated_at": snapshot["generated_at"], "data_generated_local": local_label(gen),
        "data_age_min": age_min, "data_mode": snapshot.get("mode"), "banner": " ".join(banner),
        "fear_greed": fg.get("value"), "fear_greed_label": fg.get("label"),
        "next_run_label": RUN_LABELS[nrt], "next_run_local": local_label(nt),
        "assets_ok": len(snapshot["order"]) - len(missing), "assets_total": len(snapshot["order"]),
        "source_errors": len(snapshot.get("errors") or []),
    })

    latest = _latest_calls(ledger)
    notes_full, notes_upd = _latest_notes(ledger)
    skipped = {(s["asset"], s["horizon"]): s["why"] for s in draft.get("skipped") or []}

    for i, aid in enumerate(snapshot["order"]):
        e = snapshot["assets"].get(aid) or {}
        row = {"kind": "asset", "id": aid, "name": e.get("name", aid), "group": e.get("group"),
               "group_label": GROUP_LABELS.get(e.get("group"), e.get("group")), "order": i,
               "status": e.get("status"), "has_calls": bool(e.get("calls"))}
        if e.get("status") == "ok":
            p = e["price"]
            d = e["indicators"]["daily"]
            row.update({
                "source": e["source"]["label"], "price": p["last"], "decimals": p["decimals"],
                "price_local": local_label(parse_ts(p["last_time"])),
                "chg_since_close": p["chg_since_close_pct"], "chg_24h": p["chg_24h_pct"],
                "chg_5d": p["chg_5d_pct"], "chg_20d": p["chg_20d_pct"],
                "trend_daily": e["trend"]["daily"], "trend_weekly": e["trend"]["weekly"],
                "rsi": None if d.get("rsi14") is None else round(d["rsi14"], 1),
                "atr_pct": None if d.get("atr_pct") is None else round(d["atr_pct"], 2),
                "vol_regime": _vol_regime(d.get("atr_pct_rank")),
                "adx": None if d.get("adx14") is None else round(d["adx14"], 1),
                "ema50": d.get("ema50"), "ema200": d.get("ema200"),
                "state_words": " · ".join(e.get("state_words") or []),
                "notes": "; ".join(e.get("notes") or []),
            })
            dv = e.get("derivatives") or {}
            row.update({"funding_8h": dv.get("funding_8h_pct"), "funding_apr": dv.get("funding_apr_pct"),
                        "oi_usd": dv.get("open_interest_usd"), "oi_chg_24h": dv.get("oi_change_24h_pct"),
                        "deriv_source": dv.get("provider")})
            rel = e.get("relative") or {}
            row.update({"rel_vs": rel.get("vs"), "rel_5d": rel.get("pp_5d")})
            cmp = e.get("compare") or {}
            row.update({"compare_label": cmp.get("label"), "compare_price": cmp.get("price"),
                        "compare_diff": cmp.get("diff")})
            lv = e["levels"]
            for side, key in (("sup", "supports"), ("res", "resistances")):
                for j in range(2):
                    lvl = lv[key][j] if len(lv[key]) > j else None
                    row[f"{side}{j + 1}"] = lvl["price"] if lvl else None
                    row[f"{side}{j + 1}_label"] = lvl["label"] if lvl else None
            s = e.get("sessions") or {}
            for k in ("asia", "london", "prev_ny"):
                r = s.get(k) or {}
                row[f"{k}_high"] = r.get("high")
                row[f"{k}_low"] = r.get("low")
            row["session_flags"] = " · ".join(s.get("flags") or [])
            for lvl in lv["all"]:
                rows.append({"kind": "level", "asset": aid, "price": lvl["price"], "label": lvl["label"],
                             "side": "support" if lvl["price"] < p["last"] else "resistance",
                             "dist_pct": round((lvl["price"] / p["last"] - 1) * 100, 2)})
        nf = notes_full.get(aid) or {}
        nu = notes_upd.get(aid) or {}
        upd_newer = nu and (not nf or nu["made_at"] > nf["made_at"])
        row.update({"summary": nf.get("summary", ""), "what_would_change": nf.get("what_would_change", ""),
                    "drivers": nf.get("drivers", "[]"), "notes_local": nf.get("made_local", ""),
                    "changed": nu.get("changed", "") if upd_newer else "",
                    "changed_local": nu.get("made_local", "") if upd_newer else "",
                    "update_drivers": nu.get("drivers", "[]") if upd_newer else "[]"})
        for hz in HORIZONS:
            c = latest.get((aid, hz))
            if hz == "session" and c and parse_ts(c["window_end"]) < made_at - timedelta(hours=12):
                c = None
            pre = f"{hz}_"
            if c:
                row.update({
                    pre + "bias": c["bias"], pre + "conf": c["confidence"], pre + "p": c["p_up"],
                    pre + "base": c["p_base"], pre + "edge": c["edge_pp"], pre + "n": c["n_eff"],
                    pre + "lo": c["lo"], pre + "hi": c["hi"], pre + "cond": c["conditioned_on"],
                    pre + "low_sample": c["low_sample"], pre + "inv": c["invalidation"],
                    pre + "inv_label": c["invalidation_label"], pre + "reason": c["reason"],
                    pre + "ref": c["ref_price"], pre + "end_local": c["window_end_local"],
                    pre + "status": c["status"], pre + "made_local": c["made_local"],
                    pre + "result": (None if c["status"] != "graded" or c["correct"] is None
                                     else "right" if c["correct"] else "wrong"),
                    pre + "skip": skipped.get((aid, hz), ""),
                    pre + "today_view": c.get("today_view"), pre + "flip": c.get("flip_trigger"),
                })
            else:
                row.update({pre + "bias": None, pre + "skip": skipped.get((aid, hz), "")})
        rows.append(row)

        cd = candles.get(aid) or {}
        for b in (cd.get("daily") or [])[-90:]:
            rows.append({"kind": "candle", "asset": aid, "t": b[0], "o": b[1], "h": b[2], "l": b[3], "c": b[4],
                         "partial": False})
        if cd.get("daily_partial"):
            b = cd["daily_partial"]
            rows.append({"kind": "candle", "asset": aid, "t": b[0], "o": b[1], "h": b[2], "l": b[3], "c": b[4],
                         "partial": True})

    for ev in snapshot.get("calendar") or []:
        t = parse_ts(ev["time_utc"])
        if t < made_at - timedelta(hours=24) or t > made_at + timedelta(days=4):
            continue
        rows.append({"kind": "event", "time_utc": ev["time_utc"], "time_local": local_label(t),
                     "country": ev["country"], "impact": ev["impact"], "title": ev["title"],
                     "forecast": ev.get("forecast", ""), "previous": ev.get("previous", ""),
                     "status": "passed" if t <= made_at else "upcoming",
                     "next_24h": made_at < t <= made_at + timedelta(hours=24)})

    sc = scorecard(ledger)
    for s in sc["horizons"]:
        rows.append({"kind": "score", **s})
    for s in sc["calibration"]:
        rows.append({"kind": "calib", **s})
    for s in sc["by_asset"]:
        rows.append({"kind": "asset_score", **s})
    for s in sc["by_confidence"]:
        rows.append({"kind": "conf_score", **s})
    return {"rows": rows}


def notification(draft: dict, narrative: dict, ledger: list[dict]) -> str:
    """One line, under 200 characters, for the phone."""
    head = narrative.get("headline", "").strip()
    calls = [c for c in draft["calls"] if c["bias"] in ("long", "short")]
    flips = [c for c in draft["calls"] if c.get("today_view") == "flipped"]
    bits = [f"{RUN_LABELS[draft['run_type']]}: {head}"]
    if flips:
        bits.append("Flipped: " + ", ".join(c["locked"]["asset"] for c in flips))
    elif draft["run_type"] == "full":
        bits.append(f"{len(calls)} directional calls")
    msg = " · ".join(bits)
    return msg if len(msg) <= 198 else msg[:195] + "..."
