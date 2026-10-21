"""The two steps a scheduled Claude run calls.

    python -m brief.run prepare --run full|london|ny --data DATA --ledger LEDGER --work WORK [--now ISO] [--allow-stale]
    python -m brief.run finish  --data DATA --work WORK

prepare
  * checks the snapshot is fresh enough for this run (exit 3 if not)
  * grades every call whose window has closed            -> WORK/ledger_graded.json
  * drafts this run's calls from the snapshot            -> WORK/draft.json
  * writes a short digest for Claude to read             -> WORK/brief.md
  * writes the file Claude fills in                      -> WORK/claude.json

finish
  * merges WORK/claude.json into the draft and narrative
  * validates them (exit 1 with the errors if anything is wrong)
  * appends the calls to the ledger                      -> WORK/pub/ledger.json
  * builds the dashboard data                            -> WORK/pub/bundle.json
  * prints the one-line phone summary
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

from .calls import RUN_LABELS, build_template
from .dashboard_data import build_bundle, note_rows, notification
from .grade import compact, grade_rows, load_ledger, rows_from_run, save_ledger, upsert
from .timeutil import iso, local_label, ny_time, parse_ts, utcnow
from .validate import validate

NOTE_DAYS = 14


def _load(p: Path):
    with open(p, encoding="utf-8") as fh:
        return json.load(fh)


def _dump(obj, p: Path, compact_json: bool = False) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "w", encoding="utf-8") as fh:
        if compact_json:
            json.dump(obj, fh, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
        else:
            json.dump(obj, fh, indent=1, ensure_ascii=False, allow_nan=False)


# ---------------------------------------------------------------- freshness
def freshness(snapshot: dict, run_type: str, now: datetime) -> dict:
    gen = parse_ts(snapshot["generated_at"])
    if run_type == "full":
        need = now.replace(hour=0, minute=2, second=0)
        if need > now:
            need -= timedelta(days=1)
        why = "needs data from after the 07:00 daily close"
    elif run_type == "ny":
        need = max(ny_time(now.date(), 8, 31), now - timedelta(minutes=40))
        why = "needs data from after the 08:30 New York data releases"
    else:
        need = now - timedelta(minutes=40)
        why = "needs data less than 40 minutes old"
    fresh = gen >= need
    return {"fresh": fresh, "generated_at": iso(gen), "need_after": iso(need),
            "age_min": round((now - gen).total_seconds() / 60), "why": "" if fresh else why}


# ---------------------------------------------------------------- digest
def _pct(x):
    return "n/a" if x is None else f"{x:+.1f}%"


def _p(x):
    return "n/a" if x is None else f"{x * 100:.0f}%"


def _fmt(x, dec):
    if x is None:
        return "n/a"
    return f"{x:,.{dec}f}"


def digest(snapshot: dict, draft: dict, fresh: dict, graded_now: int) -> str:
    now = parse_ts(draft["made_at"])
    L = [f"# {RUN_LABELS[draft['run_type']]} · {draft['made_local']} Phnom Penh",
         f"Data: {snapshot['generated_local']} ({fresh['age_min']} min old, {snapshot['mode']} refresh)"
         + ("" if fresh["fresh"] else f" · STALE: {fresh['why']}"),
         f"Calls graded this run: {graded_now}"]
    fg = snapshot.get("fear_greed")
    if fg:
        L.append(f"Crypto Fear & Greed: {fg['value']} ({fg['label']}), previous {fg['previous']}")
    if snapshot.get("errors"):
        L.append(f"Source problems ({len(snapshot['errors'])}): " + " | ".join(snapshot["errors"][:6]))
    L.append("")
    L.append("## Calendar, next 36 hours (Phnom Penh time)")
    evs = [e for e in snapshot.get("calendar") or []
           if now - timedelta(hours=12) <= parse_ts(e["time_utc"]) <= now + timedelta(hours=36)]
    if not evs:
        L.append("- nothing high or medium impact")
    for e in evs:
        when = "done" if parse_ts(e["time_utc"]) <= now else "upcoming"
        fc = f", forecast {e['forecast']}" if e.get("forecast") else ""
        pv = f", previous {e['previous']}" if e.get("previous") else ""
        L.append(f"- {e['time_local']} {e['country']} {e['impact']}: {e['title']}{fc}{pv} ({when})")
    L.append("")

    by_asset: dict = {}
    for c in draft["calls"]:
        by_asset.setdefault(c["locked"]["asset"], []).append(c)
    skipped: dict = {}
    for s in draft.get("skipped") or []:
        skipped.setdefault(s["asset"], []).append(f"{s['horizon']}: {s['why']}")

    for aid in snapshot["order"]:
        e = snapshot["assets"].get(aid) or {}
        if e.get("status") != "ok":
            L.append(f"## {aid}: NO DATA ({'; '.join(e.get('notes') or [])})")
            L.append("")
            continue
        if draft["run_type"] != "full" and aid not in by_asset and e.get("calls"):
            continue
        p = e["price"]
        dec = p["decimals"]
        d = e["indicators"]["daily"]
        L.append(f"## {aid}: {e['name']} · {e['source']['label']}" + ("" if e.get("calls") else " · context only"))
        L.append(f"Price {_fmt(p['last'], dec)} · since last daily close {_pct(p['chg_since_close_pct'])}"
                 f" · 24h {_pct(p['chg_24h_pct'])} · 5 bars {_pct(p['chg_5d_pct'])} · 20 bars {_pct(p['chg_20d_pct'])}")
        L.append("TA: " + " · ".join(e.get("state_words") or []) +
                 f" · trend daily {e['trend']['daily']}, weekly {e['trend']['weekly']}"
                 f" · 50d {_fmt(d.get('ema50'), dec)} · 200d {_fmt(d.get('ema200'), dec)}")
        lv = e["levels"]
        sup = "; ".join(f"{_fmt(x['price'], dec)} ({x['label']})" for x in lv["supports"]) or "none near"
        res = "; ".join(f"{_fmt(x['price'], dec)} ({x['label']})" for x in lv["resistances"]) or "none near"
        L.append(f"Support: {sup} · Resistance: {res}")
        dv = e.get("derivatives")
        if dv:
            oi = dv.get("open_interest_usd")
            L.append(f"Derivatives ({dv['provider']}): funding {dv['funding_8h_pct']:+.4f}%/8h "
                     f"({dv['funding_apr_pct']:+.1f}%/yr) · open interest ${oi / 1e6:,.0f}M"
                     + (f" ({dv['oi_change_24h_pct']:+.1f}% 24h)" if dv.get("oi_change_24h_pct") is not None else ""))
        if e.get("relative") and e["relative"].get("pp_5d") is not None:
            L.append(f"Relative to {e['relative']['vs']} over 5 bars: {e['relative']['pp_5d']:+.1f} points")
        s = e.get("sessions")
        if s:
            parts = []
            for k, name in (("prev_ny", "prev NY"), ("asia", "Asia"), ("london", "London")):
                if s.get(k):
                    parts.append(f"{name} {_fmt(s[k]['low'], dec)}-{_fmt(s[k]['high'], dec)}")
            L.append("Sessions: " + " · ".join(parts) + (" · " + " · ".join(s["flags"]) if s.get("flags") else ""))
        for c in by_asset.get(aid, []):
            k = c["locked"]
            inv = f" · suggested invalidation {_fmt(c['invalidation'], dec)} ({c['invalidation_label']})" if c["invalidation"] else ""
            why = f" ({k['lean_why']})" if k.get("lean_why") else ""
            mb = f" · morning call {k['morning_bias']}" if k.get("morning_bias") else ""
            edge = "n/a" if k["edge_pp"] is None else f"{k['edge_pp']:+.1f}"
            low = ", LOW SAMPLE" if k["low_sample"] else ""
            L.append(f"- {k['horizon']}: lean {k['lean'].upper()}{why}, cap {k['max_conf']} · "
                     f"history {_p(k['p_up'])} up vs {_p(k['p_base'])} normal "
                     f"({edge} pts, n~{k['n_eff']}, {k['conditioned_on']}{low}) · "
                     f"signals: {'; '.join(k['signals'])}{inv}{mb}")
        for sk in skipped.get(aid, []):
            L.append(f"- skipped {sk}")
        L.append("")
    return "\n".join(L)


def claude_skeleton(draft: dict) -> dict:
    assets: dict = {}
    for c in draft["calls"]:
        k = c["locked"]
        a = assets.setdefault(k["asset"], {})
        if draft["run_type"] == "full":
            a.setdefault("summary", "")
            a.setdefault("what_would_change", "")
        else:
            a.setdefault("changed", "")
        a.setdefault("drivers", [])
        call = {"bias": c["bias"], "confidence": c["confidence"], "invalidation": c["invalidation"],
                "news_driven": False, "reason": ""}
        if k["horizon"] == "session":
            call["today_view"] = c.get("today_view")
            call["flip_trigger"] = c.get("flip_trigger")
        a.setdefault("calls", {})[k["horizon"]] = call
    return {"headline": "", "overview": "", "assets": assets}


# ---------------------------------------------------------------- commands
def cmd_prepare(args) -> int:
    data = Path(args.data)
    work = Path(args.work)
    work.mkdir(parents=True, exist_ok=True)
    snapshot = _load(data / "snapshot.json")
    candles = _load(data / "candles.json")["assets"]
    now = parse_ts(args.now) if args.now else utcnow()
    fresh = freshness(snapshot, args.run, now)
    if not fresh["fresh"] and not args.allow_stale:
        print(json.dumps({"status": "stale", **fresh}))
        return 3
    ledger = load_ledger(args.ledger)
    graded, n_graded = grade_rows(ledger, candles, now)
    graded = compact(graded, now)
    cutoff = now - timedelta(days=NOTE_DAYS)
    graded = [r for r in graded if r.get("kind") != "note" or parse_ts(r["made_at"]) >= cutoff]
    save_ledger(work / "ledger_graded.json", graded)
    draft = build_template(snapshot, args.run, now, graded)
    _dump(draft, work / "draft.json")
    (work / "brief.md").write_text(digest(snapshot, draft, fresh, n_graded), encoding="utf-8")
    _dump(claude_skeleton(draft), work / "claude.json")
    print(json.dumps({"status": "ok", "run_id": draft["run_id"], "calls": len(draft["calls"]),
                      "skipped": len(draft["skipped"]), "graded_now": n_graded, "fresh": fresh["fresh"],
                      "data_age_min": fresh["age_min"]}))
    return 0


def merge_claude(draft: dict, claude: dict) -> tuple[dict, dict]:
    out = json.loads(json.dumps(draft))
    assets = claude.get("assets") or {}
    for c in out["calls"]:
        k = c["locked"]
        mine = ((assets.get(k["asset"]) or {}).get("calls") or {}).get(k["horizon"]) or {}
        for f in ("bias", "confidence", "invalidation", "news_driven", "reason"):
            if f in mine:
                c[f] = mine[f]
        if k["horizon"] == "session":
            for f in ("today_view", "flip_trigger"):
                if f in mine:
                    c[f] = mine[f]
    narrative = {"headline": claude.get("headline", ""), "overview": claude.get("overview", ""),
                 "assets": {aid: {kk: vv for kk, vv in a.items() if kk != "calls"} for aid, a in assets.items()}}
    return out, narrative


def cmd_finish(args) -> int:
    data = Path(args.data)
    work = Path(args.work)
    snapshot = _load(data / "snapshot.json")
    candles = _load(data / "candles.json")["assets"]
    draft = _load(work / "draft.json")
    claude = _load(work / "claude.json")
    ledger = load_ledger(work / "ledger_graded.json")
    final, narrative = merge_claude(draft, claude)
    res = validate(snapshot, final, narrative, ledger)
    if not res["ok"]:
        print(json.dumps(res, indent=1))
        return 1
    new_rows = rows_from_run(final, narrative, snapshot) + note_rows(final, narrative)
    ledger = upsert(ledger, new_rows)
    pub = work / "pub"
    save_ledger(pub / "ledger.json", ledger)
    bundle = build_bundle(snapshot, candles, final, narrative, ledger)
    _dump(bundle, pub / "bundle.json", compact_json=True)
    _dump(final, work / "final.json")
    _dump(narrative, work / "narrative.json")
    print(json.dumps({"status": "ok", "warnings": res["warnings"],
                      "ledger_rows": len(ledger), "bundle_rows": len(bundle["rows"]),
                      "ledger_kb": round((pub / "ledger.json").stat().st_size / 1024),
                      "bundle_kb": round((pub / "bundle.json").stat().st_size / 1024),
                      "notification": notification(final, narrative, ledger)}, ensure_ascii=False))
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("prepare")
    p.add_argument("--run", choices=["full", "london", "ny"], required=True)
    p.add_argument("--data", required=True)
    p.add_argument("--ledger", default=None)
    p.add_argument("--work", required=True)
    p.add_argument("--now", default=None)
    p.add_argument("--allow-stale", action="store_true")
    f = sub.add_parser("finish")
    f.add_argument("--data", required=True)
    f.add_argument("--work", required=True)
    args = ap.parse_args(argv)
    return cmd_prepare(args) if args.cmd == "prepare" else cmd_finish(args)


if __name__ == "__main__":
    sys.exit(main())
