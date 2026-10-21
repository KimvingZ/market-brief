import numpy as np
import pandas as pd
import pytest

from brief.base_rates import conditional, wilson


def test_wilson_known_value():
    lo, hi = wilson(0.5, 100)
    assert lo == pytest.approx(0.4189, abs=1e-3)
    assert hi == pytest.approx(0.5811, abs=1e-3)
    assert wilson(0.5, 0) == (0.0, 1.0)


def _frame_with_states(n: int, p_up_when_up: float, p_up_when_down: float, seed: int = 0) -> pd.DataFrame:
    """Alternate 'up'/'down' trend labels; next-day direction depends on the label."""
    rng = np.random.default_rng(seed)
    state_up = np.arange(n) % 2 == 0
    close = np.empty(n)
    close[0] = 100.0
    for t in range(1, n):
        p = p_up_when_up if state_up[t - 1] else p_up_when_down
        close[t] = close[t - 1] * (1.01 if rng.random() < p else 0.99)
    idx = pd.date_range("2015-01-01", periods=n, freq="D", tz="UTC")
    c = pd.Series(close, index=idx)
    ema50 = np.where(state_up, c * 0.9, c * 1.1)  # price above both averages on 'up' days
    return pd.DataFrame({"close": c, "ema50": ema50, "ema200": ema50, "rsi14": 50.0, "atr_pct_rank": 0.5})


def test_conditional_recovers_known_odds():
    d = _frame_with_states(4001, 0.7, 0.3)  # last bar (index 4000) is an 'up' day
    br = conditional(d, 1)
    assert br["state"]["trend"] == "up"
    assert br["conditioned_on"] == "trend + momentum + volatility"
    assert br["p_up"] == pytest.approx(0.7, abs=0.035)
    assert br["p_base"] == pytest.approx(0.5, abs=0.03)
    assert br["edge_pp"] > 15
    assert br["lo"] < br["p_up"] < br["hi"]
    assert not br["low_sample"]


def test_overlapping_windows_shrink_sample():
    d = _frame_with_states(4001, 0.6, 0.4)
    one = conditional(d, 1)
    week = conditional(d, 7)
    assert week["n_eff"] == round(week["n"] / 7)
    assert (week["hi"] - week["lo"]) > (one["hi"] - one["lo"])


def test_falls_back_when_sample_small():
    d = _frame_with_states(260, 0.6, 0.4)
    br = conditional(d, 30)
    assert br["conditioned_on"] == "all days"
    assert br["low_sample"]
