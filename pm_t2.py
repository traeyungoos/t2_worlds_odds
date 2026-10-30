#!/usr/bin/env python3
"""
pm_t2.py - Polymarket tier-2 LoL odds -> CSV.

  pip install requests
  python pm_t2.py                 # moneylines + spreads, next 60 days
  python pm_t2.py --types all     # every live market type
  python pm_t2.py --raw           # no filters at all, for debugging

Appends a timestamped row per market to pm_t2.csv next to this file.
Sort by pulled_at_utc descending for the current picture; the older rows are
your line-movement history.
"""
import argparse, csv, json, os, re, sys, time
from datetime import datetime, timedelta, timezone

import requests

# --- config ---------------------------------------------------------------
# An event is ours if its title contains any of these (case-insensitive).
MATCH = ["world star challengers invitational"]

GAMMA, CLOB = "https://gamma-api.polymarket.com", "https://clob.polymarket.com"
HERE = os.path.dirname(os.path.abspath(__file__))

# First pattern to hit wins, so narrow types come before the moneyline rule.
TYPES = [
    ("spread", r"handicap|spread|[+-]\s?\d+\.?\d*\s*(maps?|games?)|win \d\s?-\s?\d"),
    ("total", r"\b(over|under)\b|total\s+(maps?|games?|kills)|how many|odd\s*/?\s*even"),
    ("correct_score", r"correct score|exact score|series score"),
    ("prop", r"baron|nashor|dragon|drake|herald|inhibitor|tower|turret|"
             r"(quadra|penta|triple|double)\s*kill|first blood|any player|both teams"),
    ("outright", r"win the|champion|qualify|advance|winner of|top \d|eliminated"),
]


# --- helpers --------------------------------------------------------------
def jl(v):
    """Gamma returns some list fields as JSON-encoded strings."""
    if isinstance(v, list):
        return v
    try:
        return json.loads(v) if v else []
    except (json.JSONDecodeError, TypeError):
        return []


def num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def call(method, url, **kw):
    for i in range(3):
        try:
            r = requests.request(method, url, timeout=25, **kw)
            if r.status_code == 429:
                time.sleep(2 * (i + 1))
                continue
            r.raise_for_status()
            return r.json()
        except requests.RequestException as e:
            if i == 2:
                print(f"  ! {url}: {e}", file=sys.stderr)
    return None


def classify(text, outcomes, teams):
    t = re.sub(r"\s+", " ", f"{text} {' '.join(outcomes)}".lower().replace("_", " "))
    for label, pat in TYPES:
        if re.search(pat, t):
            return label
    # A real moneyline's two outcomes are the two team names.
    if len(outcomes) == 2 and all(o.strip().lower() in teams for o in outcomes):
        return "moneyline"
    if len(outcomes) == 2 and re.search(r"\bvs\.?\b|\bbeat\b|\bdefeat\b", t):
        return "moneyline"
    return "other"


def is_live(mk):
    """Gamma returns prop markets that were created but never launched - no
    order book, no funding. They don't appear on the website. Drop only on an
    explicit False so a missing field never hides a real market."""
    if mk.get("closed") or mk.get("archived"):
        return False
    return not any(mk.get(f) is False for f in
                   ("active", "acceptingOrders", "enableOrderBook", "ready", "funded"))


# --- fetch ----------------------------------------------------------------
def find_events():
    seen, out = set(), []
    for q in MATCH:
        for src in (call("GET", f"{GAMMA}/public-search",
                         params={"q": q, "limit_per_type": 40}) or {}).get("events", []):
            if src.get("id") not in seen:
                seen.add(src.get("id"))
                out.append(src)
    offset = 0
    while offset < 2000:  # tag sweep, in case search misses one
        page = call("GET", f"{GAMMA}/events", params={
            "tag_slug": "league-of-legends", "related_tags": "true",
            "active": "true", "closed": "false", "limit": 250, "offset": offset})
        if not page:
            break
        for e in page:
            if e.get("id") not in seen:
                seen.add(e.get("id"))
                out.append(e)
        if len(page) < 250:
            break
        offset += 250
    return out


def book(token_ids):
    """{token_id: (mid, bid, ask)} from the CLOB."""
    mids, sides = {}, {}
    for i in range(0, len(token_ids), 100):
        chunk = token_ids[i:i + 100]
        for k, v in (call("POST", f"{CLOB}/midpoints",
                          json=[{"token_id": t} for t in chunk]) or {}).items():
            mids[k] = num(v.get("mid") if isinstance(v, dict) else v)
        payload = [{"token_id": t, "side": s} for t in chunk for s in ("BUY", "SELL")]
        for k, v in (call("POST", f"{CLOB}/prices", json=payload) or {}).items():
            if isinstance(v, dict):
                a, b = num(v.get("BUY")), num(v.get("SELL"))
                # BUY/SELL name the side of the book, not the price you'd pay.
                # min/max guards it: a bid above an ask is a crossed book.
                sides[k] = (min(a, b), max(a, b)) if None not in (a, b) else (a, b)
        time.sleep(0.15)
    return {t: (mids.get(t), *sides.get(t, (None, None))) for t in token_ids}


# --- main -----------------------------------------------------------------
COLS = ["pulled_at_utc", "match_utc", "group", "market_type", "market",
        "team_a", "prob_a_pct", "bid_a", "ask_a", "team_b", "prob_b_pct",
        "bid_b", "ask_b", "vig_pct", "volume_24h", "liquidity", "url"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=60)
    ap.add_argument("--types", default="moneyline,spread", help="or 'all'")
    ap.add_argument("--raw", action="store_true", help="no filtering, for debugging")
    ap.add_argument("--out", default="pm_t2.csv")
    a = ap.parse_args()

    keep = None if a.raw or a.types == "all" else set(a.types.lower().split(","))
    horizon = datetime.now(timezone.utc) + timedelta(days=a.days)
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    path = a.out if os.path.isabs(a.out) else os.path.join(HERE, a.out)

    events = [e for e in find_events()
              if any(m in (e.get("title") or "").lower() for m in MATCH)]
    print(f"{len(events)} matching events")

    rows, tokens, counts = [], [], {}
    for ev in events:
        title = ev.get("title") or ""
        grp = (re.search(r"group ([a-d])", title, re.I) or [None, ""])[1].upper()
        teams = {o.strip().lower() for m in ev.get("markets") or []
                 for o in jl(m.get("outcomes"))
                 if o.strip().lower() not in ("yes", "no", "odd", "even")}
        for mk in ev.get("markets") or []:
            if not a.raw and not is_live(mk):
                continue
            outs, tids = [str(o) for o in jl(mk.get("outcomes"))], \
                         [str(t) for t in jl(mk.get("clobTokenIds"))]
            if len(outs) < 2 or len(tids) < 2:
                continue
            q = mk.get("question") or title
            mt = classify(f"{q} {mk.get('slug') or ''}", outs, teams)
            counts[mt] = counts.get(mt, 0) + 1
            if keep and mt not in keep:
                continue
            when = (mk.get("gameStartTime") or mk.get("startDate")
                    or ev.get("startDate") or "")
            if when and not a.raw:
                try:
                    if datetime.fromisoformat(when.replace("Z", "+00:00")) > horizon:
                        continue
                except ValueError:
                    pass
            tokens += tids[:2]
            rows.append((when[:16].replace("T", " "), grp, mt, q, outs[:2], tids[:2],
                         num(mk.get("volume24hr")), num(mk.get("liquidity")),
                         ev.get("slug") or ""))

    print("  types:", ", ".join(f"{k}={v}" for k, v in sorted(counts.items(),
                                                              key=lambda x: -x[1])))
    if not rows:
        print("Nothing kept. Try --raw to see everything.")
        return

    prices = book(tokens)
    out = []
    for when, grp, mt, q, outs, tids, vol, liq, slug in sorted(rows):
        (ma, ba, aa), (mb, bb, ab) = prices[tids[0]], prices[tids[1]]
        tot = (ma or 0) + (mb or 0)
        out.append(dict(zip(COLS, [
            stamp, when, grp, mt, q,
            outs[0], round(ma * 100, 2) if ma else "", ba or "", aa or "",
            outs[1], round(mb * 100, 2) if mb else "", bb or "", ab or "",
            round((tot - 1) * 100, 2) if tot else "", vol or "", liq or "",
            f"https://polymarket.com/event/{slug}" if slug else ""])))

    new = not os.path.exists(path)
    with open(path, "a", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=COLS)
        if new:
            w.writeheader()
        w.writerows(out)

    print(f"\n+{len(out)} rows -> {path}")
    for r in out:
        print(f"  {r['match_utc']:<16} [{r['group']}] {r['team_a']} "
              f"{r['prob_a_pct']}%  vs  {r['team_b']} {r['prob_b_pct']}%")


if __name__ == "__main__":
    main()
