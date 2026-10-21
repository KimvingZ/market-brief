# Market Brief

A daily market report for BTC, ETH, SOL, ZEC, AAVE, HYPE, gold, WTI oil and US
AI-chip and memory stocks (NVDA, AMD, AVGO, TSM, MU, SNDK, WDC, STX), with
news, technical analysis and a direction for today, the next week and the next
month.

It is built to be honest about one thing most market reports hide: whether a
market goes up or down on a given day is close to a coin flip. So the numbers
come from code, the probabilities are measured from history, and every call is
graded afterwards.

## What you get

| When (Phnom Penh) | Run | What it covers |
|---|---|---|
| 07:35 every day | **Pre-Asia report** | Full report: news and narrative, technical readout, levels, and a call for today, the next week and the next month for every asset |
| 13:20 (14:20 from 26 Oct) weekdays | **Pre-London update** | What changed overnight, the Asia range, and a call for the London session (crypto, gold, oil) |
| 19:47 (20:47 from 2 Nov) weekdays | **Pre-New York update** | After the 08:30 US data: what changed, the London range, and a call for the New York session, including US pre-market for the stocks |

The update times follow UK and US daylight saving automatically: they are set
at 07:20 London time and 08:47 New York time.

Everything lands on one dashboard (today's table, one card per asset with a
chart and levels, the scorecard, the history), and a phone notification
carries the one-line summary.

## What it will not do

* Place trades, set entries or stops, or log in to any exchange. It reads
  public prices only.
* Print a probability nobody measured. Every percentage is a count from
  history, shown with its sample size.

## How it works

```
GitHub Actions, every 30 min          Claude scheduled task, 3x a day          Dashboard
---------------------------------     ------------------------------------     -------------------
fetch prices (Bitget / Coinbase /     read the data branch                     summary table
  Hyperliquid / Yahoo / Stooq)        grade calls whose window has closed      asset cards + charts
indicators, levels, sessions          search the news                          scorecard
base rates                    ----->  write the calls and narrative    -----> history
write snapshot.json to `data`         validator checks every number            phone notification
```

Claude's cloud workspace cannot reach price APIs, so all prices and maths run
on GitHub's servers and are published to the `data` branch of this repository.
The Claude runs only read that branch, search the news and write words.

## How "how likely" is measured

For each asset, the code labels every past day by three things:

* **trend**: price above both its 50- and 200-day averages (up), below both
  (down), or in between (mixed)
* **momentum**: RSI under 40 (weak), 40 to 60 (neutral), over 60 (strong)
* **volatility**: the day's typical range (ATR) in the bottom, middle or top
  third of the past year (calm, normal, wild)

It finds every past day with the same label as today and counts how often
price was higher one bar, one week and one month later. The report shows that
percentage, how many days it is based on, a likely range, and the asset's
normal up-rate on any day.

What matters is the **gap** between the two. "56% against a normal 53%" is a
3-point edge, not a 56% signal. Longer windows overlap (each one-week window
shares six days with the next), so the sample size is divided by the window
length. When too few matching days exist, the label gets coarser: volatility
is dropped, then momentum, then all days are used and the call is marked
**low sample**.

Session updates use a different count: how often the London (or New York)
window closed higher after the previous session went up or down. For US
stocks: how often the cash session closed above its open after a gap up or
gap down.

## The rules every call follows

For each horizon three technical signals vote bullish, bearish or neutral:

* **today**: 4-hour trend, 4-hour RSI, daily MACD
* **next week**: price vs 20-day average, 20- vs 50-day average, daily MACD
* **next month**: price vs 200-day average, weekly trend, 50- vs 200-day average
* **session**: 1-hour trend, 1-hour RSI, 4-hour trend

Two or three votes the same way, with a base rate that doesn't disagree, give
the code's **lean**. The confidence cap depends on the size of the measured
edge and the sample:

| Cap | Needs |
|---|---|
| high | all three signals agree, edge of 5+ points, 60+ independent samples, likely range clear of normal |
| medium | 2+ signals agree, edge of 2+ points, 30+ samples |
| low | anything else |

Claude reads the news and may **keep the lean or go neutral, and may lower the
confidence**. It cannot raise the confidence or take the opposite side. A
long or short against a neutral lean is allowed only as a news-driven call, at
low confidence, with a link to the news. Every long or short names an
**invalidation price** picked from the computed levels: the price at which
the idea is wrong.

A session update can turn against the morning's call only if the morning's
invalidation level broke, a scheduled event came out, or there is linked news,
and it has to say which.

`brief/validate.py` enforces all of this before anything is published.

## How the calls are judged

When a call's window closes, the next run grades it:

* **right**: a long closed above its reference price, a short below
* **invalidated**: price touched the invalidation level before the window closed
* **calibration**: for every call, whether the base-rate percentage matched what happened

The scorecard groups the results by period (a day, a week or a month) so that
calls on correlated assets on the same day don't count as independent, and
gives a plain verdict: *too early*, *can't tell apart from a coin flip yet*,
*beating a coin flip so far*, or *worse than a coin flip so far*. It takes
about 30 periods before the verdict means much.

## Repository map

| Path | What it is |
|---|---|
| `config/assets.yaml` | assets, sources in order of preference, horizons |
| `brief/sources.py` | price, funding, open interest, calendar and sentiment fetchers |
| `brief/indicators.py` | EMA, RSI, MACD, ATR, ADX, Bollinger width |
| `brief/levels.py` | support and resistance levels |
| `brief/base_rates.py` | measured probabilities and session statistics |
| `brief/setup.py` | the lean and the confidence cap |
| `brief/analyze.py`, `brief/snapshot.py` | build `snapshot.json` and `candles.json` |
| `brief/calls.py`, `brief/validate.py` | draft the calls, check Claude's edits |
| `brief/grade.py` | the ledger, grading and the scorecard |
| `brief/dashboard_data.py` | the dashboard's data file |
| `brief/run.py` | the `prepare` and `finish` steps a Claude run calls |
| `scripts/probe_sources.py` | which sources answer from GitHub's servers |
| `.github/workflows/` | `snapshot` (every 30 min), `probe` (by hand), `tests` |

## Running it yourself

```bash
pip install -r requirements.txt -r requirements-dev.txt
python -m pytest -q                                   # offline tests, synthetic prices
python -m brief.snapshot --out out --mode full        # needs internet access to the price APIs
python -m brief.run prepare --run full --data out --work work
# edit work/claude.json, then
python -m brief.run finish --data out --work work
```

## Known limits

* **Gold and oil** use the front-month futures contract (COMEX GC, NYMEX CL)
  unless a spot feed is configured. Gold futures trade a few tens of dollars
  above spot, and continuous futures charts jump when the contract rolls.
* **Crypto prices** come from Bitget when GitHub's US servers can reach it,
  otherwise from Coinbase or Hyperliquid, usually within about 0.1% of Bitget.
* **Short histories** (HYPE, SNDK) give small samples; their calls will often
  say low sample.
* **Holidays** are not modelled: a missing bar just means grading uses the
  next one.
* **Hourly grading** uses whole hourly bars, so the start of a session window
  can include a few minutes from before the call.
* **GitHub's scheduler** can run late or skip runs (on this repository it
  hadn't fired at all on its first day). So each report starts its own price
  snapshot when it begins and waits up to 15 minutes for it. If the prices
  are still stale after that, the report shows a "data delayed" banner and
  makes no calls from old prices.
* **GitHub pauses scheduled workflows** in a public repository after 60 days
  without activity. If the dashboard keeps saying "data delayed", open the
  repository's Actions tab and re-enable the `snapshot` workflow.
* **Futures rolls**: WTI rolls around the 20th of each month and gold every
  two months. History is adjusted for the switch, and calls that span a roll
  are voided rather than graded across two contracts.
