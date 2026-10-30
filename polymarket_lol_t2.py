#!/usr/bin/env python3
"""
polymarket_lol_t2.py
--------------------
Pulls Polymarket odds for tier-2 League of Legends esports matches and writes
them to CSV so you can keep a running spreadsheet.

Uses only the free public endpoints:
  * Gamma API  https://gamma-api.polymarket.com   -> market discovery / metadata
  * CLOB  API  https://clob.polymarket.com        -> live midpoints / best bid+ask

Outputs two files:
  1. <out>_latest.csv   wide format, one row per match, OVERWRITTEN each run.
                        This is the sheet you eyeball.
  2. <out>_history.csv  long format, one row per outcome per run, APPENDED.
                        This is your time series for tracking line movement.

Usage
-----
  pip install requests

  # normal run: matches in the next 21 days involving the configured teams
  python polymarket_lol_t2.py

  # first time / sanity check: show every LoL market found, matched or not
  python polymarket_lol_t2.py --list-all

  # widen the window and dig harder for events
  python polymarket_lol_t2.py --days 35 --deep

  # refresh every 10 minutes until you ctrl-C
  python polymarket_lol_t2.py --loop 600

If nothing matches, run --list-all and look at how Polymarket actually spells
the team names, then add those spellings to the `aliases` lists in TEAMS below.
That is the only part of this file you should normally need to edit.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
import time
import unicodedata
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher

try:
    import requests
except ImportError:
    sys.exit("Missing dependency. Run:  pip install requests")

GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"
UA = {"User-Agent": "lol-t2-odds-tracker/1.0"}
TIMEOUT = 25

# CSVs land next to this .py file, regardless of where you run it from.
# Pass an absolute path to --out to override.
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

# --------------------------------------------------------------------------
# TEAM CONFIG
# --------------------------------------------------------------------------
# `aliases` are matched case-insensitively against the market question, slug
# and outcome labels. Accents are stripped, so "Fenix" matches "Fénix".
# Short aliases (<= 4 chars) are matched on word boundaries only, so "T1"
# will not accidentally fire inside "T1000".

TEAMS = {
    # ---- Group A -------------------------------------------------------
    "T1 Esports Academy": {
        "group": "A",
        "aliases": ["t1 esports academy", "t1 academy", "t1 challengers", "t1 ea"],
    },
    "EDward Gaming Youth Team": {
        "group": "A",
        "aliases": ["edward gaming youth", "edg youth", "edward gaming youth team",
                    "edgy", "edg y"],
    },
    "DN SOOPers Challengers": {
        "group": "A",
        "aliases": ["dn soopers challengers", "dn soopers", "soopers challengers",
                    "dns challengers", "dnsc"],
    },
    "Fuego": {
        "group": "A",
        "aliases": ["fuego", "fuego esports", "fuego gaming"],
    },
    # ---- Group B -------------------------------------------------------
    "Bilibili Gaming Junior": {
        "group": "B",
        "aliases": ["bilibili gaming junior", "blg junior", "bilibili junior",
                    "blgj"],
    },
    "KT Rolster Challengers": {
        "group": "B",
        "aliases": ["kt rolster challengers", "kt challengers", "ktc",
                    "kt rolster c"],
    },
    "CFO Academy": {
        "group": "B",
        "aliases": ["cfo academy", "ctbc flying oyster academy",
                    "flying oyster academy", "cfoa"],
    },
    "Movistar KOI Fénix": {
        "group": "B",
        "aliases": ["movistar koi fenix", "koi fenix", "mkoi fenix", "mkoif",
                    "movistar koi f"],
    },
    # ---- Group C -------------------------------------------------------
    "Dplus KIA Challengers": {
        "group": "C",
        "aliases": ["dplus kia challengers", "dplus challengers", "dk challengers",
                    "dplus kia c", "dkc"],
    },
    "Vivo Keyd Stars Academy": {
        "group": "C",
        "aliases": ["vivo keyd stars academy", "keyd stars academy", "vks academy",
                    "vivo keyd academy", "vksa"],
    },
    "Cupid Esports": {
        "group": "C",
        "aliases": ["cupid esports", "cupid"],
    },
    "FENNEL": {
        "group": "C",
        "aliases": ["fennel"],
    },
    # ---- Group D -------------------------------------------------------
    "Saigon Warriors": {
        "group": "D",
        "aliases": ["saigon warriors", "sgw", "saigon w"],
    },
    "Galions": {
        "group": "D",
        "aliases": ["galions", "les galions", "gal"],
    },
    "ZSK": {
        "group": "D",
        "aliases": ["zsk", "zero sense kings"],
    },
    "Solary": {
        "group": "D",
        "aliases": ["solary", "sly"],
    },
}

# Tag slugs tried when discovering LoL events on Gamma.
TAG_SLUG_CANDIDATES = ["league-of-legends", "lol", "esports", "e-sports", "gaming"]

# Fuzzy-match floor. Lower it to catch more (and more false positives).
FUZZY_THRESHOLD = 0.88


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
def norm(s: str) -> str:
    """lowercase, strip accents, drop punctuation, collapse whitespace"""
    if not s:
        return ""
    s = unicodedata.normalize("NFKD", str(s))
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = s.lower().replace("&", " and ")
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def jloads(v, default=None):
    """Gamma returns some list fields as JSON-encoded strings."""
    if v is None:
        return default if default is not None else []
    if isinstance(v, (list, dict)):
        return v
    try:
        return json.loads(v)
    except (json.JSONDecodeError, TypeError):
        return default if default is not None else []


def fnum(v):
    try:
        if v is None or v == "":
            return None
        return float(v)
    except (TypeError, ValueError):
        return None


def get(url, params=None, retries=3):
    for attempt in range(retries):
        try:
            r = requests.get(url, params=params, headers=UA, timeout=TIMEOUT)
            if r.status_code == 429:
                time.sleep(2 * (attempt + 1))
                continue
            r.raise_for_status()
            return r.json()
        except requests.RequestException as e:
            if attempt == retries - 1:
                print(f"  ! GET failed {url} {params}: {e}", file=sys.stderr)
                return None
            time.sleep(1.5 * (attempt + 1))
    return None


def post(url, payload, retries=3):
    for attempt in range(retries):
        try:
            r = requests.post(url, json=payload, headers=UA, timeout=TIMEOUT)
            if r.status_code == 429:
                time.sleep(2 * (attempt + 1))
                continue
            r.raise_for_status()
            return r.json()
        except requests.RequestException as e:
            if attempt == retries - 1:
                print(f"  ! POST failed {url}: {e}", file=sys.stderr)
                return None
            time.sleep(1.5 * (attempt + 1))
    return None


def parse_dt(s):
    if not s:
        return None
    s = str(s).replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


# --------------------------------------------------------------------------
# team matching
# --------------------------------------------------------------------------
_ALIAS_INDEX = []  # (canonical_name, group, normalized_alias)
for _name, _cfg in TEAMS.items():
    seen = set()
    for _a in [_name] + _cfg["aliases"]:
        n = norm(_a)
        if n and n not in seen:
            seen.add(n)
            _ALIAS_INDEX.append((_name, _cfg["group"], n))
_ALIAS_INDEX.sort(key=lambda x: -len(x[2]))  # longest alias wins


def match_team(text: str):
    """Return (canonical_name, group) if `text` references a configured team."""
    n = norm(text)
    if not n:
        return None
    for name, group, alias in _ALIAS_INDEX:
        if len(alias) <= 4:
            if re.search(rf"\b{re.escape(alias)}\b", n):
                return name, group
        elif alias in n:
            return name, group
    # fuzzy fallback for whole-string comparisons (outcome labels)
    for name, group, alias in _ALIAS_INDEX:
        if len(alias) > 4 and SequenceMatcher(None, n, alias).ratio() >= FUZZY_THRESHOLD:
            return name, group
    return None


def teams_in(text: str):
    """All configured teams referenced anywhere in a string."""
    n = norm(text)
    hits = {}
    for name, group, alias in _ALIAS_INDEX:
        if name in hits:
            continue
        if len(alias) <= 4:
            if re.search(rf"\b{re.escape(alias)}\b", n):
                hits[name] = group
        elif alias in n:
            hits[name] = group
    return hits


# --------------------------------------------------------------------------
# discovery
# --------------------------------------------------------------------------
def fetch_events_by_tag(slug, max_pages=20, page=250):
    out, offset = [], 0
    for _ in range(max_pages):
        data = get(f"{GAMMA}/events", {
            "tag_slug": slug, "related_tags": "true",
            "active": "true", "closed": "false",
            "limit": page, "offset": offset,
        })
        if isinstance(data, dict):
            data = data.get("data") or data.get("events") or []
        if not data:
            break
        out.extend(data)
        if len(data) < page:
            break
        offset += page
    return out


def fetch_events_by_search(query):
    data = get(f"{GAMMA}/public-search", {"q": query, "limit_per_type": 20})
    if isinstance(data, dict):
        return data.get("events") or []
    if isinstance(data, list):
        return data
    return []


def fetch_events_deep(max_pages=40, page=250):
    """Brute-force sweep of active events. Slow but catches odd tagging."""
    out, offset = [], 0
    for _ in range(max_pages):
        data = get(f"{GAMMA}/events", {
            "active": "true", "closed": "false",
            "order": "startDate", "ascending": "true",
            "limit": page, "offset": offset,
        })
        if isinstance(data, dict):
            data = data.get("data") or data.get("events") or []
        if not data:
            break
        out.extend(data)
        if len(data) < page:
            break
        offset += page
    return out


def discover_events(deep=False, verbose=True):
    events, seen = [], set()

    def add(batch, src):
        n = 0
        for ev in batch or []:
            eid = str(ev.get("id") or ev.get("slug"))
            if eid and eid not in seen:
                seen.add(eid)
                events.append(ev)
                n += 1
        if verbose and n:
            print(f"  + {n:5d} events from {src}")

    for slug in TAG_SLUG_CANDIDATES:
        add(fetch_events_by_tag(slug), f"tag:{slug}")

    queries = ["league of legends"] + [name for name in TEAMS]
    for q in queries:
        add(fetch_events_by_search(q), f"search:{q[:28]}")
        time.sleep(0.12)

    if deep:
        add(fetch_events_deep(), "deep sweep")

    return events


# --------------------------------------------------------------------------
# pricing
# --------------------------------------------------------------------------
def fetch_midpoints(token_ids):
    """POST /midpoints in chunks. Returns {token_id: float}."""
    out = {}
    ids = [t for t in token_ids if t]
    for i in range(0, len(ids), 100):
        chunk = ids[i:i + 100]
        res = post(f"{CLOB}/midpoints", [{"token_id": t} for t in chunk])
        if isinstance(res, dict):
            for k, v in res.items():
                val = fnum(v.get("mid") if isinstance(v, dict) else v)
                if val is not None:
                    out[k] = val
        time.sleep(0.15)
    return out


def fetch_book_prices(token_ids):
    """POST /prices for best bid (SELL) and best ask (BUY)."""
    out = {}
    ids = [t for t in token_ids if t]
    for i in range(0, len(ids), 50):
        chunk = ids[i:i + 50]
        payload = []
        for t in chunk:
            payload.append({"token_id": t, "side": "BUY"})
            payload.append({"token_id": t, "side": "SELL"})
        res = post(f"{CLOB}/prices", payload)
        if isinstance(res, dict):
            for tid, sides in res.items():
                if isinstance(sides, dict):
                    out[tid] = {
                        "best_ask": fnum(sides.get("BUY") or sides.get("buy")),
                        "best_bid": fnum(sides.get("SELL") or sides.get("sell")),
                    }
        time.sleep(0.15)
    return out


def american_odds(p):
    """Decimal probability -> American moneyline, for sportsbook comparison."""
    if not p or p <= 0 or p >= 1:
        return None
    return round(-100 * p / (1 - p)) if p >= 0.5 else round(100 * (1 - p) / p)


# --------------------------------------------------------------------------
# extraction
# --------------------------------------------------------------------------
def extract_markets(events, days, require_team=True, verbose=True):
    now = datetime.now(timezone.utc)
    horizon = now + timedelta(days=days)
    rows, skipped_date, skipped_team = [], 0, 0

    for ev in events:
        ev_title = ev.get("title") or ""
        ev_slug = ev.get("slug") or ""
        for mk in ev.get("markets") or []:
            if mk.get("closed") or mk.get("archived"):
                continue
            question = mk.get("question") or mk.get("groupItemTitle") or ev_title
            mk_slug = mk.get("slug") or ev_slug
            outcomes = [str(o) for o in jloads(mk.get("outcomes"))]
            token_ids = [str(t) for t in jloads(mk.get("clobTokenIds"))]
            gamma_prices = [fnum(p) for p in jloads(mk.get("outcomePrices"))]

            start = (parse_dt(mk.get("gameStartTime"))
                     or parse_dt(mk.get("startDate"))
                     or parse_dt(ev.get("startDate")))
            end = parse_dt(mk.get("endDate")) or parse_dt(ev.get("endDate"))
            when = start or end

            blob = f"{ev_title} | {question} | {mk_slug} | {' '.join(outcomes)}"
            hits = teams_in(blob)

            if require_team and not hits:
                skipped_team += 1
                continue
            if when and not (now - timedelta(days=1) <= when <= horizon):
                skipped_date += 1
                continue

            rows.append({
                "event_title": ev_title,
                "event_slug": ev_slug,
                "question": question,
                "market_slug": mk_slug,
                "condition_id": mk.get("conditionId") or "",
                "start": when,
                "outcomes": outcomes,
                "token_ids": token_ids,
                "gamma_prices": gamma_prices,
                "last_trade": fnum(mk.get("lastTradePrice")),
                "spread": fnum(mk.get("spread")),
                "volume_24h": fnum(mk.get("volume24hr")),
                "volume": fnum(mk.get("volume")),
                "liquidity": fnum(mk.get("liquidity") or mk.get("liquidityNum")),
                "teams": hits,
            })

    if verbose:
        print(f"  parsed {len(rows)} markets "
              f"(skipped {skipped_team} no-team, {skipped_date} out-of-window)")
    return rows


def price_markets(rows):
    all_tokens = [t for r in rows for t in r["token_ids"]]
    print(f"  pricing {len(all_tokens)} outcome tokens via CLOB...")
    mids = fetch_midpoints(all_tokens)
    books = fetch_book_prices(all_tokens)

    for r in rows:
        priced = []
        for i, label in enumerate(r["outcomes"]):
            tid = r["token_ids"][i] if i < len(r["token_ids"]) else ""
            gamma_p = r["gamma_prices"][i] if i < len(r["gamma_prices"]) else None
            book = books.get(tid, {})
            bid, ask = book.get("best_bid"), book.get("best_ask")
            mid = mids.get(tid)
            if mid is None and bid is not None and ask is not None:
                mid = (bid + ask) / 2
            spread = (ask - bid) if (bid is not None and ask is not None) else r["spread"]

            # Polymarket shows last trade instead of midpoint when spread > $0.10
            if spread is not None and spread > 0.10 and r["last_trade"] is not None:
                shown = r["last_trade"] if i == 0 else (1 - r["last_trade"])
            else:
                shown = mid if mid is not None else gamma_p

            m = match_team(label)
            priced.append({
                "label": label,
                "team": m[0] if m else label,
                "group": m[1] if m else "",
                "token_id": tid,
                "price": shown,
                "mid": mid,
                "best_bid": bid,
                "best_ask": ask,
                "spread": spread,
                "gamma_price": gamma_p,
            })
        r["priced"] = priced
    return rows


# --------------------------------------------------------------------------
# output
# --------------------------------------------------------------------------
WIDE_COLS = [
    "pulled_at_utc", "match_date_utc", "event", "market", "group",
    "team_a", "team_a_prob_pct", "team_a_price", "team_a_bid", "team_a_ask",
    "team_a_american",
    "team_b", "team_b_prob_pct", "team_b_price", "team_b_bid", "team_b_ask",
    "team_b_american",
    "vig_pct", "volume_24h", "volume_total", "liquidity", "url", "market_slug",
]

LONG_COLS = [
    "pulled_at_utc", "match_date_utc", "market_slug", "market", "group",
    "outcome", "team", "prob_pct", "price", "mid", "best_bid", "best_ask",
    "spread", "american_odds", "volume_24h", "liquidity", "token_id", "url",
]


def pct(p):
    return round(p * 100, 2) if p is not None else ""


def rnd(p, n=4):
    return round(p, n) if p is not None else ""


def build_rows(markets, stamp):
    wide, long = [], []
    for r in sorted(markets, key=lambda x: (x["start"] or datetime.max.replace(tzinfo=timezone.utc))):
        url = f"https://polymarket.com/event/{r['event_slug']}" if r["event_slug"] else ""
        when = r["start"].strftime("%Y-%m-%d %H:%M") if r["start"] else ""
        groups = sorted({g for g in r["teams"].values() if g})
        grp = "/".join(groups)
        p = r["priced"]

        for o in p:
            long.append({
                "pulled_at_utc": stamp, "match_date_utc": when,
                "market_slug": r["market_slug"], "market": r["question"],
                "group": grp, "outcome": o["label"], "team": o["team"],
                "prob_pct": pct(o["price"]), "price": rnd(o["price"]),
                "mid": rnd(o["mid"]), "best_bid": rnd(o["best_bid"]),
                "best_ask": rnd(o["best_ask"]), "spread": rnd(o["spread"]),
                "american_odds": american_odds(o["price"]) or "",
                "volume_24h": rnd(r["volume_24h"], 2),
                "liquidity": rnd(r["liquidity"], 2),
                "token_id": o["token_id"], "url": url,
            })

        a = p[0] if len(p) > 0 else None
        b = p[1] if len(p) > 1 else None
        total = sum(o["price"] for o in p if o["price"] is not None) or None
        wide.append({
            "pulled_at_utc": stamp, "match_date_utc": when,
            "event": r["event_title"], "market": r["question"], "group": grp,
            "team_a": a["team"] if a else "", "team_a_prob_pct": pct(a["price"]) if a else "",
            "team_a_price": rnd(a["price"]) if a else "",
            "team_a_bid": rnd(a["best_bid"]) if a else "",
            "team_a_ask": rnd(a["best_ask"]) if a else "",
            "team_a_american": (american_odds(a["price"]) or "") if a else "",
            "team_b": b["team"] if b else "", "team_b_prob_pct": pct(b["price"]) if b else "",
            "team_b_price": rnd(b["price"]) if b else "",
            "team_b_bid": rnd(b["best_bid"]) if b else "",
            "team_b_ask": rnd(b["best_ask"]) if b else "",
            "team_b_american": (american_odds(b["price"]) or "") if b else "",
            "vig_pct": round((total - 1) * 100, 2) if total else "",
            "volume_24h": rnd(r["volume_24h"], 2),
            "volume_total": rnd(r["volume"], 2),
            "liquidity": rnd(r["liquidity"], 2),
            "url": url, "market_slug": r["market_slug"],
        })
    return wide, long


def write_csv(path, cols, rows, append=False):
    exists = os.path.exists(path)
    mode = "a" if append and exists else "w"
    with open(path, mode, newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        if mode == "w":
            w.writeheader()
        w.writerows(rows)


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------
def run_once(args):
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    print(f"\n=== Polymarket tier-2 LoL pull @ {stamp} UTC ===")

    print("Discovering events...")
    events = discover_events(deep=args.deep)
    print(f"  {len(events)} unique events collected")

    markets = extract_markets(events, args.days, require_team=not args.list_all)
    if not markets:
        print("\nNo matching markets found.")
        print("Try:  python polymarket_lol_t2.py --list-all --days 60 --deep")
        print("then copy Polymarket's exact team spellings into TEAMS aliases.")
        return

    markets = price_markets(markets)
    wide, long = build_rows(markets, stamp)

    prefix = args.out if os.path.isabs(args.out) else os.path.join(SCRIPT_DIR, args.out)
    latest = f"{prefix}_latest.csv"
    history = f"{prefix}_history.csv"
    write_csv(latest, WIDE_COLS, wide, append=False)
    write_csv(history, LONG_COLS, long, append=True)

    print(f"\nWrote {len(wide)} matches -> {latest}  (overwritten)")
    print(f"Wrote {len(long)} rows    -> {history} (appended)\n")

    for row in wide[:40]:
        a = f"{row['team_a']} {row['team_a_prob_pct']}%" if row["team_a"] else ""
        b = f"{row['team_b']} {row['team_b_prob_pct']}%" if row["team_b"] else ""
        print(f"  {row['match_date_utc']:<16} {a:<38} vs  {b}")
    if len(wide) > 40:
        print(f"  ... {len(wide) - 40} more in the CSV")


def main():
    ap = argparse.ArgumentParser(description="Scrape Polymarket tier-2 LoL odds to CSV")
    ap.add_argument("--days", type=int, default=21,
                    help="look-ahead window in days (default 21)")
    ap.add_argument("--out", default="polymarket_lol_t2",
                    help="output file prefix; relative names go next to this "
                         "script, absolute paths are used as-is")
    ap.add_argument("--deep", action="store_true",
                    help="brute-force sweep all active events (slower, more complete)")
    ap.add_argument("--list-all", action="store_true",
                    help="skip the team filter and dump every LoL market found")
    ap.add_argument("--loop", type=int, default=0,
                    help="re-run every N seconds instead of exiting")
    args = ap.parse_args()

    if args.loop:
        while True:
            try:
                run_once(args)
            except KeyboardInterrupt:
                print("\nStopped.")
                return
            except Exception as e:  # keep the watcher alive
                print(f"  ! run failed: {e}", file=sys.stderr)
            time.sleep(args.loop)
    else:
        run_once(args)


if __name__ == "__main__":
    main()
