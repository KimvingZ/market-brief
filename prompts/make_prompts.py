"""Render the three scheduled-task prompts from prompts/run.md.

    python prompts/make_prompts.py        # writes prompts/full.txt, london.txt, ny.txt
"""

from pathlib import Path

HERE = Path(__file__).resolve().parent

RUNS = {
    "full": {
        "LABEL": "Pre-Asia report",
        "SEARCHES": "15",
        "NEWS_FOCUS": (
            "Cover: the overall crypto market (ETF flows, regulation, liquidations); each of BTC, ETH, SOL, ZEC, AAVE "
            "and HYPE (token unlocks, protocol news); gold and oil (Fed, dollar, OPEC, inventories, geopolitics); "
            "Nvidia, AMD, Broadcom, TSMC, Micron, SanDisk, Western Digital and Seagate (earnings dates, AI spending, "
            "memory prices, export rules); and what is scheduled today."
        ),
        "WRITE_FOCUS": (
            "- `headline`: the one thing that matters most today (10 to 140 characters).\n"
            "- `overview`: the market picture in 3 to 6 sentences: risk mood, dollar and yields, crypto flows, today's events.\n"
            "- For each asset: `summary` (what is driving it, tied to the technical picture, 20 to 600 characters), "
            "`what_would_change` (10 to 300 characters) and up to 5 `drivers` (`text`, `url` starting https://, `source`)."
        ),
    },
    "london": {
        "LABEL": "Pre-London update",
        "SEARCHES": "5",
        "NEWS_FOCUS": (
            "Only what is new since the morning report: overnight moves in crypto, gold and oil, anything from Asia "
            "or Europe that moved them, and events due before New York opens."
        ),
        "WRITE_FOCUS": (
            "- `headline`: what changed since the morning (10 to 140 characters).\n"
            "- `overview`: 2 to 4 sentences on the overnight session and what London has to deal with.\n"
            "- For each asset: `changed` (what moved and why since the morning report, 10 to 400 characters) and any "
            "new `drivers`.\n"
            "- Session calls also have `today_view` (`kept`, or `flipped` when the session call turns against the "
            "morning call) and `flip_trigger`: `level:<price>` (a level price from brief.md that price has broken), "
            "`event:<exact calendar title>` or `news:<https link>`. The draft already sets these when the morning "
            "invalidation broke."
        ),
    },
    "ny": {
        "LABEL": "Pre-New York update",
        "SEARCHES": "6",
        "NEWS_FOCUS": (
            "Only what is new: the 08:30 New York data if any came out (actual vs forecast), US pre-market moves and "
            "company news for the eight stocks, and what moved crypto, gold and oil during London."
        ),
        "WRITE_FOCUS": (
            "- `headline`: what changed since the morning (10 to 140 characters).\n"
            "- `overview`: 2 to 4 sentences: the data release if any, London's moves, and what New York has to deal with.\n"
            "- For each asset: `changed` (what moved and why since the morning report, 10 to 400 characters) and any "
            "new `drivers`.\n"
            "- Session calls also have `today_view` (`kept`, or `flipped` when the session call turns against the "
            "morning call) and `flip_trigger`: `level:<price>` (a level price from brief.md that price has broken), "
            "`event:<exact calendar title>` or `news:<https link>`. The draft already sets these when the morning "
            "invalidation broke."
        ),
    },
}


def render(run: str) -> str:
    text = (HERE / "run.md").read_text()
    values = {"RUN": run, **RUNS[run]}
    for k, v in values.items():
        text = text.replace("{" + k + "}", v)
    return text


if __name__ == "__main__":
    for run in RUNS:
        (HERE / f"{run}.txt").write_text(render(run))
        print(f"wrote prompts/{run}.txt ({len(render(run))} chars)")
