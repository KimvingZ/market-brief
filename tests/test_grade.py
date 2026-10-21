from datetime import datetime, timedelta

from brief.grade import grade_call, horizon_score
from brief.timeutil import UTC, iso


def _call(**kw):
    base = {"kind": "call", "id": "x", "asset": "BTC", "horizon": "today", "bias": "long", "status": "open",
            "ref_price": 100.0, "ref_time": "2026-10-21T00:00:00Z", "ref_bar": "2026-10-20", "h_bars": 1,
            "window_end": "2026-10-22T00:00:00Z", "invalidation": 95.0, "p_up": 0.55, "p_base": 0.5}
    base.update(kw)
    return base


def _candles(low=96.0, close=103.0, hourly=None):
    return {"BTC": {"calendar": "crypto", "daily": [["2026-10-20", 99, 101, 98, 100, 1],
                                                    ["2026-10-21", 100, 104, low, close, 1]],
                    "hourly": hourly or []}}


NOW = datetime(2026, 10, 22, 0, 40, tzinfo=UTC)


def test_long_right_and_not_invalidated():
    g = grade_call(_call(), _candles(), NOW)
    assert g["status"] == "graded" and g["correct"] is True and g["invalidated"] is False
    assert g["ret_pct"] == 3.0 and g["up"] is True


def test_long_invalidated_but_right_at_close():
    g = grade_call(_call(), _candles(low=94.0), NOW)
    assert g["correct"] is True and g["invalidated"] is True


def test_short_wrong():
    g = grade_call(_call(bias="short", invalidation=105.0), _candles(), NOW)
    assert g["correct"] is False and g["invalidated"] is False


def test_neutral_not_scored_for_direction():
    g = grade_call(_call(bias="neutral", invalidation=None), _candles(), NOW)
    assert g["status"] == "graded" and g["correct"] is None and g["up"] is True


def test_not_graded_before_window_closes():
    g = grade_call(_call(), _candles(), datetime(2026, 10, 21, 23, 0, tzinfo=UTC))
    assert g["status"] == "open"


def test_void_when_data_never_arrives():
    c = {"BTC": {"calendar": "crypto", "daily": [["2026-10-20", 99, 101, 98, 100, 1]], "hourly": []}}
    assert grade_call(_call(), c, NOW)["status"] == "open"
    assert grade_call(_call(), c, NOW + timedelta(days=11))["status"] == "void"


def test_session_call_uses_hourly_price_at_window_end():
    start = datetime(2026, 10, 21, 6, 0, tzinfo=UTC)
    hourly = [[iso(start + timedelta(hours=i)), 100, 101 + i, 99, 100 + i] for i in range(10)]
    row = _call(horizon="session", ref_time="2026-10-21T06:27:00Z", window_end="2026-10-21T13:00:00Z",
                ref_bar=None, h_bars=None, invalidation=98.0)
    g = grade_call(row, _candles(hourly=hourly), datetime(2026, 10, 21, 14, 0, tzinfo=UTC))
    assert g["close_price"] == 106  # bar opened 12:00 closes at 13:00
    assert g["correct"] is True and g["invalidated"] is False


def _graded(day: int, correct: bool):
    t = datetime(2026, 9, 1, tzinfo=UTC) + timedelta(days=day)
    return {"kind": "call", "horizon": "today", "status": "graded", "correct": correct, "up": correct,
            "p_up": 0.55, "p_base": 0.5, "window_end": iso(t), "bias": "long", "invalidated": False}


def test_scorecard_too_early_then_verdict():
    rows = [_graded(d, d % 2 == 0) for d in range(5)]
    assert horizon_score(rows, "today")["verdict"].startswith("Too early")
    rows = []
    for d in range(40):
        rows += [_graded(d, True), _graded(d, True), _graded(d, (d % 4) != 0)]
    s = horizon_score(rows, "today")
    assert s["periods"] == 40 and s["verdict"] == "Beating a coin flip so far."
    coin = [_graded(d, d % 2 == 0) for d in range(40)]
    assert horizon_score(coin, "today")["verdict"] == "Can't tell apart from a coin flip yet."
