# t2_worlds_odds

Python scripts that pull [Polymarket](https://polymarket.com) betting odds for tier-2 League of Legends matches — built for the **World Star Challengers Invitational** — and save them to CSV so you can track how lines move over time.

Uses only Polymarket's free public APIs (Gamma for market discovery, CLOB for prices). No API key needed.

## Setup

```bash
pip install requests
```

## Scripts

### `polymarket_lol_t2.py` — main tracker

Finds LoL markets involving the 16 configured teams (Groups A–D) and writes two files:

- `polymarket_lol_t2_latest.csv` — one row per market, overwritten each run (the sheet you look at)
- `polymarket_lol_t2_history.csv` — one row per outcome per run, appended (your line-movement history)

Includes implied probability, bid/ask, American odds, vig, volume, and liquidity.

```bash
python polymarket_lol_t2.py                 # matches in the next 21 days
python polymarket_lol_t2.py --list-all      # show every LoL market found (good first run)
python polymarket_lol_t2.py --days 35 --deep
python polymarket_lol_t2.py --loop 600      # refresh every 10 minutes
```

If a team isn't being picked up, run `--list-all`, see how Polymarket spells the name, and add it to that team's `aliases` in the `TEAMS` config near the top of the file.

### `pm_t2.py` — simpler version

Matches any event with "World Star Challengers Invitational" in the title and appends a row per market to `pm_t2.csv`.

```bash
python pm_t2.py                  # moneylines + spreads, next 60 days
python pm_t2.py --types all      # every market type
python pm_t2.py --raw            # no filtering, for debugging
```

### `pm_fetch.py` — manual list

Fetches odds only for the event slugs you list in a `markets.txt` file (one per line — the last part of the Polymarket URL) and appends to `odds.csv`.

```bash
python pm_fetch.py
python pm_fetch.py --out mysheet.csv
python pm_fetch.py --quiet       # for cron jobs
```

## Output

All CSVs are saved next to the scripts. The included CSVs are sample snapshots from September 2026.
