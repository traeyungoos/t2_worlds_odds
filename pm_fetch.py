#!/usr/bin/env python3
"""


Setup
-----
  pip install requests

Create markets.txt with one event slug per line. The slug is the last path
segment of the Polymarket URL:

    https://polymarket.com/event/t1-esports-academy-vs-fuego
                                 ^^^^^^^^^^^^^^^^^^^^^^^^^^ this part

    # markets.txt - lines starting with # are ignored
    t1-esports-academy-vs-fuego
    zsk-vs-solary
    dplus-kia-challengers-vs-galions

Run
---
  python pm_fetch.py                     # append one snapshot to odds.csv
  python pm_fetch.py --out mysheet.csv
  python pm_fetch.py --quiet             # for cron

"""

import argparse
import csv
import json
import os
import sys
from datetime import datetime, timezone

try:
    import requests
except ImportError:
    sys.exit("Run: pip install requests")

GAMMA = "https://gamma-api.polymarket.com/events"

# markets.txt and odds.csv live next to this .py file, regardless of where

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
COLS = ["pulled_at_utc", "match_time_utc", "event", "market",
        "outcome_a", "prob_a_pct", "outcome_b", "prob_b_pct",
        "vig_pct", "volume_24h", "liquidity", "closed", "slug"]


def jl(v):
    """Gamma returns some list fields as JSON-encoded strings."""
    if isinstance(v, list):
        return v
    try:
        return json.loads(v) if v else []
    except (json.JSONDecodeError, TypeError):
        return []


def f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def fetch(slug):
    r = requests.get(GAMMA, params={"slug": slug}, timeout=20)
    r.raise_for_status()
    data = r.json()
    if isinstance(data, dict):
        data = data.get("data") or data.get("events") or []
    return data[0] if data else None


def rows_for(event, stamp):
    out = []
    for mk in event.get("markets") or []:
        names = [str(o) for o in jl(mk.get("outcomes"))]
        prices = [f(p) for p in jl(mk.get("outcomePrices"))]
        if len(names) < 2 or len(prices) < 2:
            continue
        total = sum(p for p in prices[:2] if p is not None)
        when = (mk.get("gameStartTime") or mk.get("startDate") or "")[:16].replace("T", " ")
        out.append({
            "pulled_at_utc": stamp,
            "match_time_utc": when,
            "event": event.get("title", ""),
            "market": mk.get("question", ""),
            "outcome_a": names[0],
            "prob_a_pct": round(prices[0] * 100, 2) if prices[0] is not None else "",
            "outcome_b": names[1],
            "prob_b_pct": round(prices[1] * 100, 2) if prices[1] is not None else "",
            "vig_pct": round((total - 1) * 100, 2) if total else "",
            "volume_24h": f(mk.get("volume24hr")) or "",
            "liquidity": f(mk.get("liquidity")) or "",
            "closed": mk.get("closed", False),
            "slug": mk.get("slug") or event.get("slug", ""),
        })
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--slugs", default="markets.txt")
    ap.add_argument("--out", default="odds.csv")
    ap.add_argument("--quiet", action="store_true")
    a = ap.parse_args()

    if not os.path.isabs(a.slugs):
        a.slugs = os.path.join(SCRIPT_DIR, a.slugs)
    if not os.path.isabs(a.out):
        a.out = os.path.join(SCRIPT_DIR, a.out)

    if not os.path.exists(a.slugs):
        sys.exit(f"No slug file at {a.slugs}. See the docstring at the top of this file.")

    with open(a.slugs, encoding="utf-8") as fh:
        slugs = [ln.strip() for ln in fh
                 if ln.strip() and not ln.strip().startswith("#")]

    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    rows = []
    for s in slugs:
        try:
            ev = fetch(s)
        except requests.RequestException as e:
            print(f"  ! {s}: {e}", file=sys.stderr)
            continue
        if not ev:
            print(f"  ! {s}: no such event (resolved and delisted, or typo)", file=sys.stderr)
            continue
        rows += rows_for(ev, stamp)

    if not rows:
        print("Nothing fetched.", file=sys.stderr)
        return

    new_file = not os.path.exists(a.out)
    with open(a.out, "a", newline="", encoding="utf-8-sig") as fh:
        w = csv.DictWriter(fh, fieldnames=COLS, extrasaction="ignore")
        if new_file:
            w.writeheader()
        w.writerows(rows)

    if not a.quiet:
        print(f"{stamp}  +{len(rows)} rows -> {a.out}")
        for r in rows:
            if not r["closed"]:
                print(f"  {r['outcome_a']} {r['prob_a_pct']}%  vs  "
                      f"{r['outcome_b']} {r['prob_b_pct']}%")


if __name__ == "__main__":
    main()
