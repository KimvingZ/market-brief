"""The ledger: every call ever made, graded once its window closes.

The ledger is one JSON file {"rows": [...]}. Rows are flat so the dashboard
can show them as a table:

  kind "call"  one call (asset, horizon, bias, numbers, outcome once graded)
  kind "run"   one report (headline, time)

Grading a call:
  * daily horizons: the close of the h-th daily bar after the reference bar
    (holidays are skipped naturally, because they have no bar)
  * session calls: the price at the window end, from hourly bars
  * correct     long: close above the reference price; short: below.
                Neutral calls are not scored for direction.
  * invalidated whether price touched the invalidation level before the window
                closed (hourly bars where available, otherwise daily highs/lows)
  * up          close above the reference price, for every call: used to check
                whether the base-rate percentages were calibrated
"""

from __future__ import annotations

import json
import math
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

from .timeutil import bar_close_time, iso, local_label, parse_ts, to_date

Z90 = 1.6448536269514722
VOID_AFTER = timedelta(days=10)
COMPACT_AFTER = timedelta(days=45)


# ---------------------------------------------------------------- io
def load_ledger(path: str | Path | None) -> list[dict]:
    if not path:
        return []
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        return []
    rows = data.get("rows") if isinstance(data, dict) else data
    return list(rows or [])


def save_ledger(path: str | Path, rows: list[dict]) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump({"rows": rows}, fh, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def upsert(rows: list[dict], new_rows: list[dict]) -> list[dict]:
    """Add rows, replacing any existing row with the same kind and id."""
    keys = {(r["kind"], r["id"]) for r in new_rows}
    kept = [r for r in rows if (r.get("kind"), r.get("id")) not in keys]
    return kept + new_rows


# ---------------------------------------------------------------- rows from a run
def rows_from_run(draft: dict, narrative: dict, snapshot: dict) -> list[dict]:
    out = []
    for c in draft["calls"]:
        L = c["locked"]
        entry = snapshot["assets"][L["asset"]]
        labels = {float(l["price"]): l["label"] for l in (entry.get("levels") or {}).get("all", [])}
        inv = c.get("invalidation")
        out.append({
            "kind": "call", "id": c["id"], "run_id": L["run_id"], "run_type": L["run_type"],
            "made_at": draft["made_at"], "made_local": draft["made_local"],
            "asset": L["asset"], "asset_name": entry.get("name", L["asset"]), "horizon": L["horizon"],
            "bias": c["bias"], "confidence": c.get("confidence"), "news_driven": bool(c.get("news_driven")),
            "invalidation": inv, "invalidation_label": labels.get(float(inv)) if inv is not None else None,
            "reason": c.get("reason", ""),
            "ref_price": L["ref_price"], "ref_time": L["ref_time"], "ref_bar": L["ref_bar"], "h_bars": L["h_bars"],
            "window_end": L["window_end"], "window_end_local": local_label(parse_ts(L["window_end"])),
            "end_bar": L["end_bar"],
            "p_up": L["p_up"], "p_base": L["p_base"], "lo": L["lo"], "hi": L["hi"], "n_eff": L["n_eff"],
            "edge_pp": L["edge_pp"], "conditioned_on": L["conditioned_on"], "low_sample": L["low_sample"],
            "lean": L["lean"], "max_conf": L["max_conf"], "ta_score": L["ta_score"],
            "today_view": c.get("today_view"), "flip_trigger": c.get("flip_trigger"),
            "morning_bias": L.get("morning_bias"),
            "status": "open", "close_price": None, "ret_pct": None, "up": None, "correct": None,
            "invalidated": None, "graded_at": None,
        })
    out.append({
        "kind": "run", "id": draft["run_id"], "run_type": draft["run_type"], "run_label": draft["run_label"],
        "made_at": draft["made_at"], "made_local": draft["made_local"],
        "headline": narrative.get("headline", ""), "overview": narrative.get("overview", ""),
        "data_generated_at": snapshot["generated_at"], "calls": len(draft["calls"]),
    })
    return out


# ---------------------------------------------------------------- grading
def _hourly(c: dict) -> list[tuple[datetime, float, float, float, float]]:
    return [(parse_ts(b[0]), b[1], b[2], b[3], b[4]) for b in c.get("hourly") or []]


def _extremes(bars) -> tuple[float | None, float | None]:
    if not bars:
        return None, None
    return min(b[3] for b in bars), max(b[2] for b in bars)


def grade_call(row: dict, candles: dict, now: datetime) -> dict:
    """Return the row, graded if its window has closed and the data is there."""
    if row.get("kind") != "call" or row.get("status") != "open":
        return row
    end = parse_ts(row["window_end"])
    if now < end + timedelta(minutes=5):
        return row
    c = candles.get(row["asset"])
    close = lo = hi = None
    if c:
        if row["horizon"] == "session":
            hb = _hourly(c)
            before = [b for b in hb if b[0] < end]
            if before and before[-1][0] >= end - timedelta(hours=3):
                close = before[-1][4]
                start = parse_ts(row["ref_time"]).replace(minute=0, second=0)
                lo, hi = _extremes([b for b in hb if start <= b[0] < end])
        else:
            cal = c["calendar"]
            ref_bar = to_date(row["ref_bar"])
            after = [b for b in c.get("daily") or [] if to_date(b[0]) > ref_bar]
            h = int(row["h_bars"])
            if len(after) >= h:
                target = after[h - 1]
                close = target[4]
                lo, hi = min(b[3] for b in after[:h]), max(b[2] for b in after[:h])
                if h == 1:
                    start = parse_ts(row["ref_time"])
                    stop = bar_close_time(cal, to_date(target[0]))
                    hb = [b for b in _hourly(c) if start <= b[0] < stop]
                    if len(hb) >= 0.8 * (stop - start).total_seconds() / 3600 and cal == "crypto":
                        lo, hi = _extremes(hb)
    if close is None:
        if now > end + VOID_AFTER:
            row = dict(row, status="void", graded_at=iso(now))
        return row
    ref = float(row["ref_price"])
    up = close > ref
    correct = None
    if row["bias"] == "long":
        correct = close > ref
    elif row["bias"] == "short":
        correct = close < ref
    invalidated = None
    inv = row.get("invalidation")
    if inv is not None and lo is not None:
        invalidated = (lo <= inv) if row["bias"] == "long" else (hi >= inv) if row["bias"] == "short" else None
    return dict(row, status="graded", close_price=close, ret_pct=round((close / ref - 1) * 100, 2),
                up=up, correct=correct, invalidated=invalidated, graded_at=iso(now))


def grade_rows(rows: list[dict], candles: dict, now: datetime) -> tuple[list[dict], int]:
    out, n = [], 0
    for r in rows:
        g = grade_call(r, candles, now)
        if g is not r and g.get("status") == "graded":
            n += 1
        out.append(g)
    return out, n


def compact(rows: list[dict], now: datetime) -> list[dict]:
    """Drop the long text from old graded calls so the file stays small."""
    out = []
    for r in rows:
        if r.get("kind") == "call" and r.get("status") in ("graded", "void") and r.get("graded_at"):
            if now - parse_ts(r["graded_at"]) > COMPACT_AFTER and r.get("reason"):
                r = dict(r, reason="")
        out.append(r)
    return out


# ---------------------------------------------------------------- scorecard
HORIZON_LABELS = {"today": "Today", "short": "Short (1 week)", "long": "Long (1 month)", "session": "Session"}


def _cluster(row: dict):
    we = parse_ts(row["window_end"])
    if row["horizon"] in ("today", "session"):
        return we.date().isoformat()
    if row["horizon"] == "short":
        y, w, _ = we.isocalendar()
        return f"{y}-W{w}"
    return f"{we.year}-{we.month:02d}"


def _mean(xs):
    return sum(xs) / len(xs) if xs else None


def horizon_score(rows: list[dict], hz: str) -> dict:
    graded = [r for r in rows if r.get("kind") == "call" and r.get("horizon") == hz
              and r.get("status") == "graded" and r.get("correct") is not None]
    n = len(graded)
    hits = sum(1 for r in graded if r["correct"])
    clusters = defaultdict(list)
    for r in graded:
        clusters[_cluster(r)].append(1.0 if r["correct"] else 0.0)
    means = [sum(v) / len(v) for v in clusters.values()]
    k = len(means)
    rate = hits / n if n else None
    lo = hi = None
    if k >= 2:
        m = sum(means) / k
        sd = math.sqrt(sum((x - m) ** 2 for x in means) / (k - 1))
        se = sd / math.sqrt(k)
        lo, hi = max(0.0, m - Z90 * se), min(1.0, m + Z90 * se)
    if k < 10:
        verdict = f"Too early: {n} graded calls over {k} periods. Needs about 30 periods."
    elif lo is not None and lo > 0.5:
        verdict = "Beating a coin flip so far."
    elif hi is not None and hi < 0.5:
        verdict = "Worse than a coin flip so far."
    else:
        verdict = "Can't tell apart from a coin flip yet."
    all_graded = [r for r in rows if r.get("kind") == "call" and r.get("horizon") == hz
                  and r.get("status") == "graded" and r.get("p_up") is not None]
    brier = _mean([(r["p_up"] - (1.0 if r["up"] else 0.0)) ** 2 for r in all_graded])
    brier_b = _mean([(r["p_base"] - (1.0 if r["up"] else 0.0)) ** 2 for r in all_graded])
    skill = None if not brier_b else round(1 - brier / brier_b, 3)
    inval = [r for r in graded if r.get("invalidated") is not None]
    open_n = sum(1 for r in rows if r.get("kind") == "call" and r.get("horizon") == hz and r.get("status") == "open")
    return {
        "horizon": hz, "label": HORIZON_LABELS[hz], "n": n, "hits": hits,
        "rate": None if rate is None else round(rate, 3),
        "lo": None if lo is None else round(lo, 3), "hi": None if hi is None else round(hi, 3),
        "periods": k, "verdict": verdict, "base_rate_skill": skill, "base_rate_n": len(all_graded),
        "invalidated_share": round(sum(1 for r in inval if r["invalidated"]) / len(inval), 3) if inval else None,
        "open": open_n,
    }


def calibration(rows: list[dict]) -> list[dict]:
    buckets = [(0.0, 0.45, "under 45%"), (0.45, 0.5, "45-50%"), (0.5, 0.55, "50-55%"),
               (0.55, 0.6, "55-60%"), (0.6, 1.01, "60% or more")]
    graded = [r for r in rows if r.get("kind") == "call" and r.get("status") == "graded" and r.get("p_up") is not None]
    out = []
    for lo, hi, name in buckets:
        sub = [r for r in graded if lo <= r["p_up"] < hi]
        out.append({
            "bucket": name, "n": len(sub),
            "predicted": round(_mean([r["p_up"] for r in sub]), 3) if sub else None,
            "actual": round(_mean([1.0 if r["up"] else 0.0 for r in sub]), 3) if sub else None,
        })
    return out


def by_asset(rows: list[dict]) -> list[dict]:
    acc = defaultdict(lambda: [0, 0])
    for r in rows:
        if r.get("kind") == "call" and r.get("status") == "graded" and r.get("correct") is not None:
            a = acc[(r["asset"], r["horizon"])]
            a[0] += 1
            a[1] += 1 if r["correct"] else 0
    return [{"asset": k[0], "horizon": k[1], "n": v[0], "hits": v[1], "rate": round(v[1] / v[0], 3)}
            for k, v in sorted(acc.items())]


def by_confidence(rows: list[dict]) -> list[dict]:
    acc = defaultdict(lambda: [0, 0])
    for r in rows:
        if r.get("kind") == "call" and r.get("status") == "graded" and r.get("correct") is not None:
            a = acc[r.get("confidence") or "none"]
            a[0] += 1
            a[1] += 1 if r["correct"] else 0
    order = ["high", "medium", "low"]
    return [{"confidence": k, "n": acc[k][0], "hits": acc[k][1],
             "rate": round(acc[k][1] / acc[k][0], 3) if acc[k][0] else None} for k in order if k in acc]


def scorecard(rows: list[dict]) -> dict:
    return {
        "horizons": [horizon_score(rows, h) for h in ("today", "short", "long", "session")],
        "calibration": calibration(rows),
        "by_asset": by_asset(rows),
        "by_confidence": by_confidence(rows),
    }
