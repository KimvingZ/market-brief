"""Draft the calls for one run, every number pre-filled from the snapshot.

Run types:
  full    pre-Asia report: today / short / long call for every eligible asset
  london  pre-London update: one "session" call (until 09:00 New York) for
          24-hour markets (crypto, gold, oil)
  ny      pre-New York update: one "session" call (until 16:00 New York) for
          crypto, gold, oil and the US stocks

Claude may edit only these fields of a call: bias, confidence, invalidation,
reason, news_driven, and on session calls today_view and flip_trigger.
Everything under "locked" is fixed. The validator rebuilds this template from
the same snapshot and compares.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from .levels import nearest_beyond, rnd
from .setup import INVALIDATION_BAND, decide, ta_signals
from .timeutil import (
    bar_close_time,
    iso,
    last_bar_closed_by,
    local_label,
    ny_time,
    parse_ts,
    session_bounds,
    to_date,
    trading_days_between,
)

RUN_TYPES = ("full", "london", "ny")
RUN_LABELS = {"full": "Pre-Asia report", "london": "Pre-London update", "ny": "Pre-New York update"}
DAILY_HORIZONS = ("today", "short", "long")
SESSION_MAX_AGE = timedelta(minutes=40)


def daily_data_problem(snapshot: dict, entry: dict, h: dict, now: datetime) -> str:
    """Why a daily-horizon call can't be made from this snapshot at time `now`, or ''.

    The snapshot must include the latest daily close (one missing weekday bar is
    accepted only when the snapshot was taken well after that close, which
    means it was a market holiday), and the call's window must still be open.
    """
    cal = entry["calendar"]
    gen = parse_ts(snapshot["generated_at"])
    expected = last_bar_closed_by(cal, now)
    ref_bar = to_date(h["ref_bar"])
    lag = trading_days_between(cal, ref_bar, expected)
    holiday = cal != "crypto" and lag == 1 and gen >= bar_close_time(cal, expected) + timedelta(hours=3)
    if lag > 0 and not holiday:
        return "prices missed the latest daily close"
    if parse_ts(h["window_end"]) <= now + timedelta(hours=1):
        return "window already over"
    return ""


def session_data_problem(snapshot: dict, run_type: str, now: datetime) -> str:
    """Why session calls can't be made from this snapshot at time `now`, or ''."""
    gen = parse_ts(snapshot["generated_at"])
    if now - gen > SESSION_MAX_AGE:
        return f"prices are {int((now - gen).total_seconds() // 60)} min old"
    if run_type == "ny" and gen < ny_time(now.date(), 8, 31):
        return "prices are from before the 08:30 New York data"
    return ""


def run_id(run_type: str, made_at: datetime) -> str:
    return f"{parse_ts(made_at).strftime('%Y-%m-%dT%H:%MZ')}-{run_type}"


def _morning_call(ledger_rows: list[dict], asset: str, now: datetime) -> dict | None:
    """The latest pre-Asia 'today' call for this asset whose window is still open."""
    best = None
    for r in ledger_rows or []:
        if r.get("kind") != "call" or r.get("asset") != asset or r.get("horizon") != "today":
            continue
        if r.get("run_type") != "full":
            continue
        if parse_ts(r["made_at"]) > now or parse_ts(r["window_end"]) <= now:
            continue
        if best is None or r["made_at"] > best["made_at"]:
            best = r
    return best


def _session_base(entry: dict, run_type: str) -> tuple[dict | None, str]:
    """Pick the session statistic that matches what has happened so far today."""
    stats = entry.get("session_stats")
    if not stats:
        return None, "no session statistics"
    price = entry["price"]["last"]
    sess = entry.get("sessions") or {}
    if stats.get("kind") == "gap":
        prev_close = entry["price"]["ref_close"]
        move = price / prev_close - 1 if prev_close else 0.0
        block = stats["ny"]
        cond = "after_up" if move > 0 else "after_down" if move < 0 else None
        what = "pre-market above yesterday's close" if move > 0 else "pre-market below yesterday's close"
        base_txt = "US cash session closed above its open"
    else:
        key = "london" if run_type == "london" else "ny"
        prior = sess.get("asia") if key == "london" else sess.get("london")
        if not prior:
            return None, "no prior session range yet"
        move = price / prior["open"] - 1 if prior["open"] else 0.0
        block = stats[key]
        cond = "after_up" if move > 0 else "after_down" if move < 0 else None
        prior_name = "Asia" if key == "london" else "London"
        what = f"{prior_name} session up so far" if move > 0 else f"{prior_name} session down so far"
        base_txt = ("London window (to 09:00 New York) closed higher" if key == "london"
                    else "New York window (09:00-16:00) closed higher")
    if cond is None:
        return None, "prior session flat"
    sub = block[cond]
    basel = block["baseline"]
    if sub.get("p_up") is None or basel.get("p_up") is None:
        return None, "not enough session history"
    n = int(sub["n"])
    return {
        "p_up": sub["p_up"], "p_base": basel["p_up"], "lo": sub["lo"], "hi": sub["hi"],
        "n": n, "n_eff": n, "edge_pp": round((sub["p_up"] - basel["p_up"]) * 100.0, 1),
        "conditioned_on": what, "measures": base_txt, "low_sample": n < 30,
    }, ""


def _signals_text(sig: list[dict]) -> list[str]:
    return [s["text"] for s in sig if s.get("text")]


def build_template(snapshot: dict, run_type: str, made_at, ledger_rows: list[dict] | None = None) -> dict:
    if run_type not in RUN_TYPES:
        raise ValueError(run_type)
    now = parse_ts(made_at)
    rid = run_id(run_type, now)
    calls: list[dict] = []
    skipped: list[dict] = []

    for aid in snapshot["order"]:
        e = snapshot["assets"].get(aid) or {}
        if not e.get("calls"):
            continue
        if e.get("status") != "ok":
            skipped.append({"asset": aid, "horizon": "all", "why": "no price data"})
            continue

        if run_type == "full":
            for hz in DAILY_HORIZONS:
                h = e["horizons"][hz]
                br = e["base_rates"].get(hz)
                if not h["eligible"]:
                    skipped.append({"asset": aid, "horizon": hz, "why": h["why_not"]})
                    continue
                problem = daily_data_problem(snapshot, e, h, now)
                if problem:
                    skipped.append({"asset": aid, "horizon": hz, "why": problem})
                    continue
                inv = h["suggested_invalidation"]
                locked = {
                    "asset": aid, "horizon": hz, "run_type": run_type, "run_id": rid,
                    "ref_price": h["ref_price"], "ref_time": h["ref_time"], "ref_bar": h["ref_bar"],
                    "h_bars": h["h_bars"], "window_end": h["window_end"], "end_bar": h["end_bar"],
                    "p_up": br["p_up"], "p_base": br["p_base"], "lo": br["lo"], "hi": br["hi"],
                    "n_eff": br["n_eff"], "edge_pp": br["edge_pp"], "conditioned_on": br["conditioned_on"],
                    "low_sample": br["low_sample"],
                    "lean": h["lean"], "lean_why": h["lean_why"], "max_conf": h["max_conf"],
                    "allowed": h["allowed"], "news_only": h["news_only"], "ta_score": h["ta_score"],
                    "signals": _signals_text(h["signals"]), "morning_bias": None,
                }
                calls.append(_call(rid, locked, inv))
            continue

        # session updates
        if run_type == "london" and e["calendar"] == "us_equity":
            continue
        problem = session_data_problem(snapshot, run_type, now)
        if problem:
            skipped.append({"asset": aid, "horizon": "session", "why": problem})
            continue
        if e["calendar"] != "us_equity" and not e.get("sessions"):
            skipped.append({"asset": aid, "horizon": "session", "why": "no intraday data"})
            continue
        if e["calendar"] == "us_equity" and not e["indicators"].get("h1"):
            skipped.append({"asset": aid, "horizon": "session", "why": "no pre-market data"})
            continue
        if e["calendar"] == "cme" and now.weekday() >= 5:
            skipped.append({"asset": aid, "horizon": "session", "why": "market closed"})
            continue
        b = session_bounds(now.date())
        window_end = b["london"][1] if run_type == "london" else b["ny"][1]
        if window_end <= now + timedelta(minutes=30):
            skipped.append({"asset": aid, "horizon": "session", "why": "session already under way"})
            continue
        br, why = _session_base(e, run_type)
        ind = {"daily": e["indicators"]["daily"], "h4": e["indicators"].get("h4"), "h1": e["indicators"].get("h1"),
               "weekly": e["indicators"]["weekly"], "price": e["price"]["last"]}
        sig = ta_signals("session", ind)
        score = sum(s["vote"] for s in sig)
        dec = decide(score, br)
        if br is None:
            dec["why"] = why
        ref = e["price"]["last"]
        atr_d = e["indicators"]["daily"].get("atr14") or ref * 0.02
        inv = None
        if dec["lean"] in ("long", "short"):
            lo_m, hi_m = INVALIDATION_BAND["session"]
            side = "below" if dec["lean"] == "long" else "above"
            inv = nearest_beyond(e["levels"]["all"], ref, side, lo_m * atr_d, hi_m * atr_d) or \
                nearest_beyond(e["levels"]["all"], ref, side, 0.5 * lo_m * atr_d, 1.5 * hi_m * atr_d)
            if inv is None:
                dec = {"lean": "neutral", "max_conf": "low", "allowed": ["neutral"],
                       "news_only": ["long", "short"], "why": "no nearby level to define risk"}
        morning = _morning_call(ledger_rows, aid, now)
        # Default: keep the morning view. If the code's session lean turns against the
        # morning call, flip only when the morning invalidation has actually broken;
        # otherwise default to neutral and let Claude decide (and justify) a flip.
        default_bias, tv, trig = dec["lean"], ("kept" if morning else "n/a"), None
        mb = morning["bias"] if morning else None
        if mb in ("long", "short") and dec["lean"] in ("long", "short") and dec["lean"] != mb:
            minv = morning.get("invalidation")
            broken = minv is not None and ((mb == "long" and ref < minv) or (mb == "short" and ref > minv))
            if broken:
                tv, trig = "flipped", f"level:{minv}"
            else:
                default_bias = "neutral"
        locked = {
            "asset": aid, "horizon": "session", "run_type": run_type, "run_id": rid,
            "ref_price": rnd(ref, ref), "ref_time": e["price"]["last_time"], "ref_bar": None,
            "h_bars": None, "window_end": iso(window_end), "end_bar": None,
            "p_up": br["p_up"] if br else None, "p_base": br["p_base"] if br else None,
            "lo": br["lo"] if br else None, "hi": br["hi"] if br else None,
            "n_eff": br["n_eff"] if br else None, "edge_pp": br["edge_pp"] if br else None,
            "conditioned_on": br["conditioned_on"] if br else why,
            "low_sample": br["low_sample"] if br else True,
            "lean": dec["lean"], "lean_why": dec.get("why", ""), "max_conf": dec["max_conf"],
            "allowed": dec["allowed"], "news_only": dec["news_only"], "ta_score": score,
            "signals": _signals_text(sig),
            "morning_bias": morning["bias"] if morning else None,
            "morning_invalidation": morning["invalidation"] if morning else None,
            "morning_made_at": morning["made_at"] if morning else None,
        }
        c = _call(rid, locked, inv, default_bias)
        c["today_view"] = tv
        c["flip_trigger"] = trig
        calls.append(c)

    return {
        "schema": 1,
        "run_type": run_type,
        "run_label": RUN_LABELS[run_type],
        "run_id": rid,
        "made_at": iso(now),
        "made_local": local_label(now),
        "snapshot_generated_at": snapshot["generated_at"],
        "calls": calls,
        "skipped": skipped,
    }


def _call(rid: str, locked: dict, inv: dict | None, bias: str | None = None) -> dict:
    bias = bias or locked["lean"]
    directional = bias in ("long", "short")
    return {
        "id": f"{rid}|{locked['asset']}|{locked['horizon']}",
        "bias": bias,
        "confidence": locked["max_conf"] if directional else None,
        "invalidation": inv["price"] if inv and directional else None,
        "invalidation_label": inv["label"] if inv and directional else None,
        "news_driven": False,
        "reason": "",
        "locked": locked,
    }
