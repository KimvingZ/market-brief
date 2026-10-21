"""Code-side lean and confidence cap for each call.

Three technical signals vote +1 (bullish), -1 (bearish) or 0 for each horizon:

  today    4h close vs 4h 50-EMA, 4h RSI (>55 / <45), daily MACD histogram
           (assets without hourly data: daily close vs 20-day average instead of
           the two 4h signals)
  short    close vs 20-day average, 20- vs 50-day average, daily MACD histogram
  long     close vs 200-day average, weekly close vs 10-week average,
           50- vs 200-day average
  session  1h close vs 1h 20-EMA, 1h RSI (>55 / <45), 4h close vs 4h 50-EMA

Lean: long if the score is +2 or +3 and the base rate does not point the other
way (edge at least -1 point); short is the mirror image; otherwise neutral.

Confidence cap:
  high    score +-3, edge >= 5 points in the lean's direction, >= 60 effective
          samples, likely range clear of the baseline, not low-sample
  medium  score +-2 or more, edge >= 2 points, >= 30 effective samples,
          not low-sample
  low     everything else

Claude may keep the lean or choose neutral, and may lower the confidence. It
may not raise it or take the opposite side. When the lean is neutral, a long
or short is allowed only as a news-driven call at low confidence.
"""

from __future__ import annotations

CONF_ORDER = {"low": 1, "medium": 2, "high": 3}

# invalidation distance bands, in daily ATRs: (min, max)
INVALIDATION_BAND = {
    "today": (0.3, 2.5),
    "short": (0.7, 4.0),
    "long": (1.5, 6.0),
    "session": (0.2, 1.5),
}


def _vote(cond_up: bool | None, cond_down: bool | None) -> int:
    if cond_up:
        return 1
    if cond_down:
        return -1
    return 0


def _gt(a, b):
    if a is None or b is None:
        return None
    return a > b


def _lt(a, b):
    if a is None or b is None:
        return None
    return a < b


def ta_signals(horizon: str, ind: dict) -> list[dict]:
    """ind: {'daily': {...}, 'h4': {...} or None, 'h1': {...} or None, 'weekly': {...}, 'price': float}."""
    d = ind.get("daily") or {}
    h4 = ind.get("h4") or {}
    h1 = ind.get("h1") or {}
    w = ind.get("weekly") or {}
    price = ind.get("price")
    sig: list[dict] = []

    def add(name, up, down, text_up, text_down, text_flat):
        v = _vote(up, down)
        sig.append({"name": name, "vote": v, "text": text_up if v > 0 else text_down if v < 0 else text_flat})

    if horizon == "today":
        if h4.get("ema50") is not None and h4.get("close") is not None:
            add("4h trend", _gt(h4["close"], h4["ema50"]), _lt(h4["close"], h4["ema50"]),
                "4h close above its 50-EMA", "4h close below its 50-EMA", "4h trend flat")
            r = h4.get("rsi14")
            add("4h momentum", _gt(r, 55), _lt(r, 45),
                f"4h RSI {r:.0f} (firm)" if r is not None else "", f"4h RSI {r:.0f} (soft)" if r is not None else "",
                f"4h RSI {r:.0f} (neutral)" if r is not None else "4h RSI n/a")
        else:
            add("daily vs 20-day", _gt(price, d.get("ema20")), _lt(price, d.get("ema20")),
                "price above the 20-day average", "price below the 20-day average", "20-day average n/a")
        add("daily MACD", _gt(d.get("macd_hist"), 0), _lt(d.get("macd_hist"), 0),
            "daily MACD above its signal", "daily MACD below its signal", "daily MACD flat")
    elif horizon == "short":
        add("price vs 20-day", _gt(price, d.get("ema20")), _lt(price, d.get("ema20")),
            "price above the 20-day average", "price below the 20-day average", "20-day average n/a")
        add("20 vs 50-day", _gt(d.get("ema20"), d.get("ema50")), _lt(d.get("ema20"), d.get("ema50")),
            "20-day above 50-day average", "20-day below 50-day average", "50-day average n/a")
        add("daily MACD", _gt(d.get("macd_hist"), 0), _lt(d.get("macd_hist"), 0),
            "daily MACD above its signal", "daily MACD below its signal", "daily MACD flat")
    elif horizon == "long":
        add("price vs 200-day", _gt(price, d.get("ema200")), _lt(price, d.get("ema200")),
            "price above the 200-day average", "price below the 200-day average", "200-day average n/a")
        add("weekly trend", _gt(w.get("close"), w.get("ema10")), _lt(w.get("close"), w.get("ema10")),
            "weekly close above its 10-week average", "weekly close below its 10-week average", "weekly average n/a")
        add("50 vs 200-day", _gt(d.get("ema50"), d.get("ema200")), _lt(d.get("ema50"), d.get("ema200")),
            "50-day above 200-day average", "50-day below 200-day average", "200-day average n/a")
    elif horizon == "session":
        add("1h trend", _gt(h1.get("close"), h1.get("ema20")), _lt(h1.get("close"), h1.get("ema20")),
            "1h close above its 20-EMA", "1h close below its 20-EMA", "1h trend n/a")
        r = h1.get("rsi14")
        add("1h momentum", _gt(r, 55), _lt(r, 45),
            f"1h RSI {r:.0f} (firm)" if r is not None else "", f"1h RSI {r:.0f} (soft)" if r is not None else "",
            f"1h RSI {r:.0f} (neutral)" if r is not None else "1h RSI n/a")
        add("4h trend", _gt(h4.get("close"), h4.get("ema50")), _lt(h4.get("close"), h4.get("ema50")),
            "4h close above its 50-EMA", "4h close below its 50-EMA", "4h trend n/a")
    else:
        raise ValueError(horizon)
    return sig


def decide(score: int, br: dict | None) -> dict:
    """Lean and confidence cap from the TA score and the base rate."""
    if br is None or br.get("p_up") is None or br.get("p_base") is None:
        return {"lean": "neutral", "max_conf": "low", "allowed": ["neutral"], "news_only": ["long", "short"],
                "why": "no base rate available"}
    p, base = br["p_up"], br["p_base"]
    edge_long = (p - base) * 100.0
    n_eff = br.get("n_eff") or 0
    low_sample = bool(br.get("low_sample"))
    lo, hi = br.get("lo"), br.get("hi")

    if score >= 2 and edge_long >= -1.0:
        lean, edge, clear = "long", edge_long, (lo is not None and lo > base)
    elif score <= -2 and -edge_long >= -1.0:
        lean, edge, clear = "short", -edge_long, (hi is not None and hi < base)
    else:
        why = "technical signals mixed" if abs(score) < 2 else "base rate points the other way"
        return {"lean": "neutral", "max_conf": "low", "allowed": ["neutral"], "news_only": ["long", "short"], "why": why}

    if abs(score) == 3 and edge >= 5.0 and n_eff >= 60 and clear and not low_sample:
        cap = "high"
    elif edge >= 2.0 and n_eff >= 30 and not low_sample:
        cap = "medium"
    else:
        cap = "low"
    return {"lean": lean, "max_conf": cap, "allowed": [lean, "neutral"], "news_only": [], "why": ""}
