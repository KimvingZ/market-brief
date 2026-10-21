"""Offline end-to-end: full report -> London update -> New York update -> next
morning's report grades the previous day's calls."""

import copy
import json
from datetime import datetime
from pathlib import Path

import pytest

from brief import run as runmod
from brief.snapshot import build
from brief.timeutil import UTC
from brief.validate import validate
from conftest import load
from fake_claude import fill
from synth import extend_prev


def _prepare(run, data, ledger, work, now):
    args = ["prepare", "--run", run, "--data", str(data), "--work", str(work), "--now", now]
    if ledger:
        args += ["--ledger", str(ledger)]
    return runmod.main(args)


def _finish(data, work):
    return runmod.main(["finish", "--data", str(data), "--work", str(work)])


@pytest.fixture(scope="module")
def chain(day1, small_cfg):
    base, _ = day1
    data1 = base / "data"
    assert _prepare("full", data1, None, base / "w1", "2026-10-21T00:45:00Z") == 0
    fill(base / "w1", "full")
    assert _finish(data1, base / "w1") == 0

    extend_prev(data1, datetime(2026, 10, 21, 6, 25, tzinfo=UTC), seed=5, cfg=small_cfg)
    build(data1, base / "data2", "auto", datetime(2026, 10, 21, 6, 25, tzinfo=UTC), offline=True, cfg=small_cfg)
    assert _prepare("london", base / "data2", base / "w1/pub/ledger.json", base / "w2", "2026-10-21T06:27:00Z") == 0
    fill(base / "w2", "london")
    assert _finish(base / "data2", base / "w2") == 0

    extend_prev(base / "data2", datetime(2026, 10, 21, 12, 50, tzinfo=UTC), seed=6, cfg=small_cfg)
    build(base / "data2", base / "data3", "auto", datetime(2026, 10, 21, 12, 50, tzinfo=UTC), offline=True, cfg=small_cfg)
    assert _prepare("ny", base / "data3", base / "w2/pub/ledger.json", base / "w3", "2026-10-21T12:52:00Z") == 0
    fill(base / "w3", "ny")
    assert _finish(base / "data3", base / "w3") == 0

    extend_prev(base / "data3", datetime(2026, 10, 22, 0, 40, tzinfo=UTC), seed=7, cfg=small_cfg)
    build(base / "data3", base / "data4", "auto", datetime(2026, 10, 22, 0, 40, tzinfo=UTC), offline=True, cfg=small_cfg)
    assert _prepare("full", base / "data4", base / "w3/pub/ledger.json", base / "w4", "2026-10-22T00:45:00Z") == 0
    return base


def test_full_run_calls(chain):
    draft = load(chain / "w1/draft.json")
    # BTC, ETH, XAU, NVDA have calls; SMH is context only. Wednesday: all three horizons eligible.
    assert len(draft["calls"]) == 4 * 3
    bundle = load(chain / "w1/pub/bundle.json")["rows"]
    kinds = {r["kind"] for r in bundle}
    assert {"meta", "asset", "level", "candle", "score", "calib"} <= kinds
    # calls are measured from the latest price, not the last daily close
    snap = load(chain / "data/snapshot.json")
    for c in draft["calls"]:
        assert c["locked"]["ref_price"] == snap["assets"][c["locked"]["asset"]]["price"]["last"]


def test_session_updates(chain):
    london = load(chain / "w2/draft.json")
    assert {c["locked"]["asset"] for c in london["calls"]} == {"BTC", "ETH", "XAU"}  # no stocks before London
    ny = load(chain / "w3/draft.json")
    assert {c["locked"]["asset"] for c in ny["calls"]} == {"BTC", "ETH", "XAU", "NVDA"}
    assert all(c["locked"]["morning_bias"] is not None for c in ny["calls"])
    nvda = next(c for c in ny["calls"] if c["locked"]["asset"] == "NVDA")
    assert nvda["locked"]["p_up"] is not None  # gap statistics available


def test_next_morning_grades_yesterday(chain):
    rows = load(chain / "w4/ledger_graded.json")["rows"]
    today = [r for r in rows if r["kind"] == "call" and r["horizon"] == "today"]
    session = [r for r in rows if r["kind"] == "call" and r["horizon"] == "session"]
    assert today and all(r["status"] == "graded" for r in today)
    assert session and all(r["status"] == "graded" for r in session)
    assert all(r["status"] == "open" for r in rows if r["kind"] == "call" and r["horizon"] in ("short", "long"))


def test_weekend_skips_non_crypto(day1, small_cfg, tmp_path):
    base, _ = day1
    data = base / "data4"
    extend_prev(data, datetime(2026, 10, 24, 0, 40, tzinfo=UTC), seed=8, cfg=small_cfg)  # Saturday
    build(data, tmp_path / "sat", "auto", datetime(2026, 10, 24, 0, 40, tzinfo=UTC), offline=True, cfg=small_cfg)
    assert _prepare("full", tmp_path / "sat", None, tmp_path / "w", "2026-10-24T00:45:00Z") == 0
    draft = load(tmp_path / "w/draft.json")
    assert {c["locked"]["asset"] for c in draft["calls"]} == {"BTC", "ETH"}
    assert {s["asset"] for s in draft["skipped"]} == {"XAU", "NVDA"}


# ------------------------------------------------------------------ validator
def _valid(chain):
    snap = load(chain / "data/snapshot.json")
    final = load(chain / "w1/final.json")
    narrative = load(chain / "w1/narrative.json")
    assert validate(snap, final, narrative, [])["ok"]
    return snap, final, narrative


def _errs(snap, final, narrative, ledger=None):
    return " | ".join(validate(snap, final, narrative, ledger or [])["errors"])


def test_validator_rejects_changed_numbers(chain):
    snap, final, narrative = _valid(chain)
    f = copy.deepcopy(final)
    f["calls"][0]["locked"]["p_up"] = 0.99
    assert "locked fields changed: p_up" in _errs(snap, f, narrative)


def test_validator_rejects_wrong_side_and_unknown_level(chain):
    snap, final, narrative = _valid(chain)
    c = next(c for c in final["calls"] if c["bias"] == "long" or c["bias"] == "short")
    f = copy.deepcopy(final)
    target = next(x for x in f["calls"] if x["id"] == c["id"])
    target["invalidation"] = 123.456
    assert "is not one of" in _errs(snap, f, narrative)
    levels = snap["assets"][c["locked"]["asset"]]["levels"]["all"]
    ref = c["locked"]["ref_price"]
    wrong = [l["price"] for l in levels if (l["price"] > ref if c["bias"] == "long" else l["price"] < ref)]
    if wrong:
        target["invalidation"] = wrong[0]
        assert "invalidation must be" in _errs(snap, f, narrative)


def test_validator_rejects_opposite_bias_and_raised_confidence(chain):
    snap, final, narrative = _valid(chain)
    c = next(c for c in final["calls"] if c["bias"] in ("long", "short"))
    f = copy.deepcopy(final)
    t = next(x for x in f["calls"] if x["id"] == c["id"])
    t["bias"] = "short" if c["bias"] == "long" else "long"
    assert "not allowed" in _errs(snap, f, narrative)
    f = copy.deepcopy(final)
    t = next(x for x in f["calls"] if x["id"] == c["id"])
    if c["locked"]["max_conf"] != "high":
        t["confidence"] = "high"
        assert "above the cap" in _errs(snap, f, narrative)


def test_validator_neutral_rules_and_news_calls(chain):
    snap, final, narrative = _valid(chain)
    f = copy.deepcopy(final)
    t = f["calls"][0]
    t["bias"], t["confidence"], t["invalidation"] = "neutral", "low", None
    assert "neutral calls carry no confidence" in _errs(snap, f, narrative)
    neutral = [c for c in final["calls"] if c["locked"]["lean"] == "neutral"]
    if neutral:
        c = neutral[0]
        f = copy.deepcopy(final)
        t = next(x for x in f["calls"] if x["id"] == c["id"])
        levels = sorted(l["price"] for l in snap["assets"][c["locked"]["asset"]]["levels"]["all"]
                        if l["price"] < c["locked"]["ref_price"])
        t.update(bias="long", confidence="low", invalidation=levels[-1] if levels else None)
        assert "needs news_driven" in _errs(snap, f, narrative)
        t["news_driven"] = True
        if levels:
            assert _errs(snap, f, narrative) == ""


def test_validator_session_flip_rules(chain):
    snap = load(chain / "data3/snapshot.json")
    ledger = load(chain / "w3/ledger_graded.json")["rows"]
    final = load(chain / "w3/final.json")
    narrative = load(chain / "w3/narrative.json")
    assert validate(snap, final, narrative, ledger)["ok"]
    c = next(c for c in final["calls"] if c["locked"]["morning_bias"] in ("long", "short"))
    f = copy.deepcopy(final)
    t = next(x for x in f["calls"] if x["id"] == c["id"])
    t["today_view"] = "flipped"
    t["flip_trigger"] = None
    assert "flip needs flip_trigger" in _errs(snap, f, narrative, ledger)
    t["flip_trigger"] = "news:https://example.com/story"
    assert _errs(snap, f, narrative, ledger) == ""
    t["flip_trigger"] = "event:Imaginary Event"
    assert "no calendar event" in _errs(snap, f, narrative, ledger)


# ------------------------------------------------------------------ stale data
def test_stale_snapshot_makes_no_calls(day1, tmp_path):
    """A snapshot from the day before: the full report and the session updates make no calls."""
    base, _ = day1
    data = base / "data"  # generated Wed 21 Oct 00:40 UTC
    assert _prepare("full", data, None, tmp_path / "f", "2026-10-22T00:45:00Z") == 3  # stale: refused
    assert runmod.main(["prepare", "--run", "full", "--data", str(data), "--work", str(tmp_path / "f"),
                        "--now", "2026-10-22T00:45:00Z", "--allow-stale"]) == 0
    draft = load(tmp_path / "f/draft.json")
    assert draft["calls"] == []
    assert {s["why"] for s in draft["skipped"]} == {"prices missed the latest daily close"}
    assert runmod.main(["prepare", "--run", "ny", "--data", str(data), "--work", str(tmp_path / "n"),
                        "--now", "2026-10-21T12:52:00Z", "--allow-stale"]) == 0
    draft = load(tmp_path / "n/draft.json")
    assert draft["calls"] == [] and all("min old" in s["why"] for s in draft["skipped"])


def test_ny_session_needs_post_0830_data(day1, small_cfg, tmp_path):
    base, _ = day1
    build(base / "data3", tmp_path / "early", "auto", datetime(2026, 10, 21, 12, 20, tzinfo=UTC), offline=True,
          cfg=small_cfg)  # 08:20 New York: before the data releases
    assert runmod.main(["prepare", "--run", "ny", "--data", str(tmp_path / "early"), "--work", str(tmp_path / "w"),
                        "--now", "2026-10-21T12:47:00Z", "--allow-stale"]) == 0
    draft = load(tmp_path / "w/draft.json")
    assert draft["calls"] == []
    assert all(s["why"] == "prices are from before the 08:30 New York data" for s in draft["skipped"])
