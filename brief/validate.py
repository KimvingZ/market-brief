"""Check Claude's draft calls and narrative before anything is published.

    python -m brief.validate --snapshot data/snapshot.json --draft work/draft.json \
        --narrative work/narrative.json [--ledger work/ledger.json]

Exit code 0 when there are no errors. Prints JSON: {"ok", "errors", "warnings"}.

Rules enforced:
  * the set of calls and every "locked" field match a fresh template built from
    the same snapshot (no changed percentages, prices or windows)
  * bias is the code's lean or neutral; a long/short against a neutral lean is
    allowed only as news_driven, at low confidence, with a linked news driver
  * confidence never above the code's cap; neutral calls carry no confidence
  * directional calls name an invalidation price taken from the snapshot's
    levels for that asset, on the correct side of the reference price
  * session calls that turn against the morning call say so (today_view
    "flipped") and name the trigger: a broken level, a calendar event, or news
"""

from __future__ import annotations

import argparse
import json
import re
import sys

from .calls import build_template
from .setup import CONF_ORDER
from .timeutil import parse_ts

URL_RE = re.compile(r"^https://[^\s<>\"']+$")


def _level_prices(entry: dict) -> dict[float, str]:
    out = {}
    for lvl in (entry.get("levels") or {}).get("all", []):
        out[float(lvl["price"])] = lvl["label"]
    return out


def _txt(obj, key, lo, hi, where, errors, required=True):
    v = obj.get(key)
    if v is None or v == "":
        if required:
            errors.append(f"{where}: '{key}' is missing")
        return
    if not isinstance(v, str):
        errors.append(f"{where}: '{key}' must be text")
        return
    if len(v) < lo or len(v) > hi:
        errors.append(f"{where}: '{key}' must be {lo}-{hi} characters (has {len(v)})")
    if "<" in v and re.search(r"<\s*/?\s*(script|iframe|img|a)\b", v, re.I):
        errors.append(f"{where}: '{key}' contains HTML")


def validate(snapshot: dict, draft: dict, narrative: dict, ledger_rows: list[dict] | None = None) -> dict:
    errors: list[str] = []
    warnings: list[str] = []
    run_type = draft.get("run_type")
    try:
        expected = build_template(snapshot, run_type, draft.get("made_at"), ledger_rows)
    except Exception as exc:  # bad run_type or made_at
        return {"ok": False, "errors": [f"cannot rebuild template: {exc}"], "warnings": []}

    if draft.get("snapshot_generated_at") != snapshot.get("generated_at"):
        errors.append("draft was made from a different snapshot; rebuild it")

    exp_by_id = {c["id"]: c for c in expected["calls"]}
    got = draft.get("calls") or []
    got_ids = [c.get("id") for c in got]
    if len(set(got_ids)) != len(got_ids):
        errors.append("duplicate call ids")
    missing = set(exp_by_id) - set(got_ids)
    extra = set(got_ids) - set(exp_by_id)
    for m in sorted(missing):
        errors.append(f"missing call {m}")
    for x in sorted(extra):
        errors.append(f"unexpected call {x}")

    narr_assets = (narrative or {}).get("assets") or {}
    calendar = snapshot.get("calendar") or []
    now = parse_ts(draft.get("made_at"))

    for c in got:
        cid = c.get("id")
        if cid not in exp_by_id:
            continue
        exp = exp_by_id[cid]
        L = c.get("locked")
        if L != exp["locked"]:
            diff = [k for k in exp["locked"] if (L or {}).get(k) != exp["locked"][k]]
            errors.append(f"{cid}: locked fields changed: {', '.join(diff) or 'structure'}")
            continue
        aid = L["asset"]
        entry = snapshot["assets"][aid]
        bias = c.get("bias")
        conf = c.get("confidence")
        news = bool(c.get("news_driven"))
        inv = c.get("invalidation")
        ref = L["ref_price"]

        if bias not in ("long", "short", "neutral"):
            errors.append(f"{cid}: bias must be long, short or neutral")
            continue
        if bias in L["allowed"]:
            pass
        elif bias in L["news_only"]:
            if not news:
                errors.append(f"{cid}: {bias} against a neutral lean needs news_driven=true")
            if conf != "low":
                errors.append(f"{cid}: a news-driven call must be low confidence")
            drivers = (narr_assets.get(aid) or {}).get("drivers") or []
            if not any(URL_RE.match(str(d.get("url", ""))) for d in drivers):
                errors.append(f"{cid}: a news-driven call needs a linked news driver for {aid}")
        else:
            errors.append(f"{cid}: bias {bias} not allowed (code lean is {L['lean']}; allowed {L['allowed']})")

        if bias == "neutral":
            if conf is not None:
                errors.append(f"{cid}: neutral calls carry no confidence (use null)")
            if inv is not None:
                errors.append(f"{cid}: neutral calls carry no invalidation (use null)")
        else:
            if conf not in CONF_ORDER:
                errors.append(f"{cid}: confidence must be low, medium or high")
            elif bias in L["allowed"] and CONF_ORDER[conf] > CONF_ORDER[L["max_conf"]]:
                errors.append(f"{cid}: confidence {conf} is above the cap {L['max_conf']}")
            levels = _level_prices(entry)
            if inv is None:
                errors.append(f"{cid}: a {bias} call needs an invalidation price")
            elif not isinstance(inv, (int, float)) or float(inv) not in levels:
                errors.append(f"{cid}: invalidation {inv} is not one of {aid}'s snapshot levels")
            elif bias == "long" and not float(inv) < ref:
                errors.append(f"{cid}: a long's invalidation must be below the reference price {ref}")
            elif bias == "short" and not float(inv) > ref:
                errors.append(f"{cid}: a short's invalidation must be above the reference price {ref}")

        _txt(c, "reason", 10, 280, cid, errors)

        if L["horizon"] == "session":
            tv = c.get("today_view")
            morning = L.get("morning_bias")
            trig = c.get("flip_trigger")
            if tv not in ("kept", "flipped", "n/a"):
                errors.append(f"{cid}: today_view must be kept, flipped or n/a")
            if morning is None and tv == "flipped":
                errors.append(f"{cid}: nothing to flip, there is no morning call")
            opposite = morning in ("long", "short") and bias in ("long", "short") and bias != morning
            if opposite and tv != "flipped":
                errors.append(f"{cid}: session {bias} goes against the morning {morning}; set today_view to flipped")
            if tv == "flipped":
                errors += _check_trigger(cid, trig, L, entry, calendar, now)
            elif trig:
                warnings.append(f"{cid}: flip_trigger ignored because today_view is {tv}")

    errors += _check_narrative(narrative, draft, got)
    return {"ok": not errors, "errors": errors, "warnings": warnings}


def _check_trigger(cid: str, trig, L: dict, entry: dict, calendar: list[dict], now) -> list[str]:
    if not isinstance(trig, str) or ":" not in trig:
        return [f"{cid}: a flip needs flip_trigger 'level:<price>', 'event:<title>' or 'news:<https url>'"]
    kind, _, value = trig.partition(":")
    value = value.strip()
    if kind == "level":
        try:
            price = float(value)
        except ValueError:
            return [f"{cid}: flip level must be a number"]
        live = entry["price"]["last"]
        if price not in _level_prices(entry) and price != L.get("morning_invalidation"):
            return [f"{cid}: flip level {price} is not a snapshot level or the morning invalidation"]
        if L.get("morning_bias") == "long" and not live < price:
            return [f"{cid}: price {live} has not broken below {price}"]
        if L.get("morning_bias") == "short" and not live > price:
            return [f"{cid}: price {live} has not broken above {price}"]
        return []
    if kind == "event":
        since = parse_ts(L["morning_made_at"]) if L.get("morning_made_at") else None
        for ev in calendar:
            t = parse_ts(ev["time_utc"])
            if ev["title"].strip().lower() == value.lower() and (since is None or since <= t <= now):
                return []
        return [f"{cid}: no calendar event '{value}' between the morning report and now"]
    if kind == "news":
        return [] if URL_RE.match(value) else [f"{cid}: flip news must be an https link"]
    return [f"{cid}: unknown flip trigger type '{kind}'"]


def _check_narrative(n: dict, draft: dict, calls: list[dict]) -> list[str]:
    errors: list[str] = []
    if not isinstance(n, dict):
        return ["narrative must be a JSON object"]
    run_type = draft.get("run_type")
    _txt(n, "headline", 10, 140, "narrative", errors)
    _txt(n, "overview", 20, 1200 if run_type == "full" else 800, "narrative", errors)
    assets = n.get("assets")
    if not isinstance(assets, dict):
        return errors + ["narrative: 'assets' must be an object keyed by asset id"]
    called = sorted({c["locked"]["asset"] for c in calls if c.get("locked")})
    for aid in called:
        a = assets.get(aid)
        where = f"narrative {aid}"
        if not isinstance(a, dict):
            errors.append(f"{where}: missing")
            continue
        if run_type == "full":
            _txt(a, "summary", 20, 600, where, errors)
            _txt(a, "what_would_change", 10, 300, where, errors)
        else:
            _txt(a, "changed", 10, 400, where, errors)
        drivers = a.get("drivers") or []
        if not isinstance(drivers, list) or len(drivers) > 5:
            errors.append(f"{where}: 'drivers' must be a list of at most 5")
            continue
        for i, d in enumerate(drivers):
            if not isinstance(d, dict):
                errors.append(f"{where} driver {i}: must be an object")
                continue
            _txt(d, "text", 10, 240, f"{where} driver {i}", errors)
            _txt(d, "source", 2, 60, f"{where} driver {i}", errors)
            if not URL_RE.match(str(d.get("url", ""))):
                errors.append(f"{where} driver {i}: url must be an https link")
    return errors


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--snapshot", required=True)
    ap.add_argument("--draft", required=True)
    ap.add_argument("--narrative", required=True)
    ap.add_argument("--ledger", default=None)
    args = ap.parse_args(argv)
    snap = json.load(open(args.snapshot, encoding="utf-8"))
    draft = json.load(open(args.draft, encoding="utf-8"))
    narrative = json.load(open(args.narrative, encoding="utf-8"))
    rows = []
    if args.ledger:
        try:
            rows = json.load(open(args.ledger, encoding="utf-8")).get("rows", [])
        except FileNotFoundError:
            rows = []
    res = validate(snap, draft, narrative, rows)
    print(json.dumps(res, indent=1))
    return 0 if res["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
