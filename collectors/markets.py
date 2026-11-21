"""Prediction-market collector.

No Polymarket market asks "will the US or Israel strike Iran" directly (all such markets resolved
after the war that began on 2026-02-28). The market probability is therefore assembled from two
ceasefire series, each turned into a 7-day strike probability:

  IL  "Israel x Iran ceasefire continues through <D>?"   Yes = no Israel<->Iran strike through D.
      Converted with a constant hazard over the market's remaining window. We stay on one market
      until fewer than MIN_HORIZON_DAYS remain, then roll to the nearest-expiry market in the series.

  US  "US x Iran Effective Ceasefire by <D>?"            Yes = a 14-day run with no US strike on Iran
      begins by D. Not a "strike by D" question, so we fit a daily US strike probability q (and the
      unknown number of quiet days so far, r) to *all* open maturities at once, then
      P_US(7d) = 1 - (1-q)^7.

  Combined: P(7d) = 1 - (1 - P_IL)(1 - P_US), i.e. assuming the two are independent. They are
  positively correlated in reality, so this leans high; max(P_IL, P_US) is the floor.

Kalshi is collected for comparison only and never enters the number.
"""
import json
import math
import time
from datetime import datetime, timedelta, timezone

import requests

from db import now_iso, parse_iso, to_iso, write_obs

GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"
KALSHI = "https://api.elections.kalshi.com/trade-api/v2"
TIMEOUT = 30
HORIZON_DAYS = 7
MIN_HORIZON_DAYS = 7
BACKFILL_DAYS = 30

IL = {
    "key": "il",
    "label": "Israel x Iran",
    "title_prefix": "Israel x Iran ceasefire continues through",
    "search_query": "Israel x Iran ceasefire continues through",
}
US = {
    "key": "us",
    "label": "US x Iran",
    "title_prefix": "US x Iran Effective Ceasefire",
    "search_query": "US x Iran Effective Ceasefire",
    "quiet_days": 14,
}
SERIES = [IL, US]

# Kalshi tickers with a comparable criterion. None existed on 2026-11-21, so this stays empty.
KALSHI_EVENT_TICKERS: list[str] = []

SOURCE_IL = "polymarket_il"
SOURCE_US = "polymarket_us"
SOURCE_KALSHI = "kalshi"
SOURCE_COMBINED = "combined"

_session = requests.Session()
_session.headers["User-Agent"] = "iran-strike-tracker/0.1"


def _get(url, params=None):
    for attempt in range(3):
        try:
            r = _session.get(url, params=params, timeout=TIMEOUT)
            r.raise_for_status()
            return r.json()
        except requests.RequestException:
            if attempt == 2:
                raise
            time.sleep(2 * (attempt + 1))


# ---- Polymarket series discovery ---------------------------------------------------------------
def series_markets(series: dict, include_closed: bool = False) -> list[dict]:
    """All markets of a series, across every event that carries it."""
    slugs = set()
    for status in (["active", "resolved"] if include_closed else ["active"]):
        res = _get(f"{GAMMA}/public-search",
                   {"q": series["search_query"], "limit_per_type": 50, "events_status": status})
        for e in res.get("events") or []:
            if e.get("title", "").startswith(series["title_prefix"]):
                slugs.add(e["slug"])
    markets = []
    for slug in slugs:
        for e in _get(f"{GAMMA}/events", {"slug": slug}):
            for m in e.get("markets") or []:
                if m.get("question", "").startswith(series["title_prefix"]) and m.get("endDate"):
                    m["_event_slug"] = e["slug"]
                    m["_event_start"] = e.get("startDate") or m.get("startDate") or m.get("createdAt")
                    markets.append(m)
    return markets


def _start(m) -> datetime:
    return parse_iso(m.get("startDate") or m.get("createdAt"))


def _end(m) -> datetime:
    return parse_iso(m["endDate"])


def _yes(m) -> float:
    return float(json.loads(m["outcomePrices"])[0])


def _is_open(m) -> bool:
    return not m.get("closed") and m.get("acceptingOrders", True)


def market_url(m) -> str:
    return f"https://polymarket.com/event/{m['_event_slug']}"


def _market_info(m, yes) -> dict:
    return {"market_id": m["id"], "question": m["question"], "url": market_url(m), "end": m["endDate"],
            "yes_price": yes, "best_bid": m.get("bestBid"), "best_ask": m.get("bestAsk"),
            "volume_24h": m.get("volume24hr"), "liquidity": m.get("liquidityNum")}


# ---- IL: constant hazard on one rolling market -------------------------------------------------
def pick_il_market(markets, at, live, current_id=None):
    """Keep the current market while its end is >= at + MIN_HORIZON_DAYS; otherwise roll to the
    nearest-expiry qualifying market (stickiness stops hopping when new nearer markets are listed)."""
    cutoff = at + timedelta(days=MIN_HORIZON_DAYS)
    cands = [m for m in markets if _start(m) <= at and _end(m) >= cutoff and (not live or _is_open(m))]
    for m in cands:
        if m["id"] == current_id:
            return m
    return min(cands, key=_end) if cands else None


def il_prob(yes: float, days_to_end: float) -> tuple[float, float]:
    """(P(strike before market end), P(strike within HORIZON_DAYS)) from the 'ceasefire holds' price."""
    p_hold = min(max(yes, 0.0), 1.0)
    if days_to_end <= HORIZON_DAYS or p_hold == 0:
        return 1 - p_hold, 1 - p_hold
    return 1 - p_hold, 1 - math.pow(p_hold, HORIZON_DAYS / days_to_end)


# ---- US: fit a daily strike probability to the quiet-run markets -------------------------------
def first_quiet_run_start_dist(q: float, r: int, horizon: int, need: int) -> list[float]:
    """Distribution of the start day (0 = today) of the first completed `need`-day run without a strike,
    when strikes happen independently each day with probability q and `r` quiet days have already
    passed. Index s holds P(first run starts on day s); runs already under way start at day <= 0, which
    are folded into index 0. Yes(D) for a market = sum over s <= D."""
    out = [0.0] * (horizon + 1)
    if r >= need:
        out[0] = 1.0
        return out
    dist = [0.0] * need          # dist[k] = P(current run length k, no completed run yet)
    dist[r] = 1.0
    for t in range(horizon + need):
        nd = [0.0] * need
        total = sum(dist)
        if total < 1e-12:
            break
        nd[0] = total * q        # strike today: run resets, next run starts tomorrow
        for k in range(need):
            if dist[k] == 0:
                continue
            if k + 1 >= need:    # run completes today; it started on day t - need + 1
                s = max(t - need + 1, 0)
                if s <= horizon:
                    out[s] += dist[k] * (1 - q)
            else:
                nd[k + 1] += dist[k] * (1 - q)
        dist = nd
    return out


def us_model_prices(q, r, quotes, need):
    """quotes: list of (days_to_D, days_since_event_start). Returns model Yes prices."""
    horizon = int(max(d for d, _ in quotes)) + 1
    cache, out = {}, []
    for d, since in quotes:
        r_eff = min(r, int(since))   # strikes before market creation do not count
        if r_eff not in cache:
            dist = first_quiet_run_start_dist(q, r_eff, horizon, need)
            cum, acc = [], 0.0
            for v in dist:
                acc += v
                cum.append(acc)
            cache[r_eff] = cum
        cum = cache[r_eff]
        out.append(cum[min(max(int(math.floor(d)), 0), len(cum) - 1)])
    return out


def fit_us(quotes, prices, need, r_hint=None):
    """Least-squares fit of (q, r). With a single quote r is not identified, so r_hint is used."""
    rs = [r_hint] if (len(quotes) < 2 and r_hint is not None) else range(0, need)
    best = None
    for r in rs:
        lo, hi = 1e-4, 0.6
        f = lambda q: sum((a - b) ** 2 for a, b in zip(us_model_prices(q, r, quotes, need), prices))
        for _ in range(40):  # golden-section search on q (model price is monotone in q)
            m1, m2 = lo + (hi - lo) * 0.382, lo + (hi - lo) * 0.618
            if f(m1) < f(m2):
                hi = m2
            else:
                lo = m1
        q = (lo + hi) / 2
        err = f(q)
        if best is None or err < best[0]:
            best = (err, q, r)
    return best  # (sse, q, r)


def us_estimate(markets, at, prices_at, r_hint=None):
    """prices_at: {market_id: yes price at `at`}. Returns dict or None."""
    use = [m for m in markets if m["id"] in prices_at and _start(m) <= at and _end(m) > at]
    if not use:
        return None
    use.sort(key=_end)
    quotes = [((_end(m) - at).total_seconds() / 86400, (at - parse_iso(m["_event_start"])).total_seconds() / 86400)
              for m in use]
    prices = [prices_at[m["id"]] for m in use]
    sse, q, r = fit_us(quotes, prices, US["quiet_days"], r_hint)
    fitted = us_model_prices(q, r, quotes, US["quiet_days"])
    return {
        "q_daily": round(q, 5), "quiet_days_so_far": r,
        "est_last_strike": to_iso(at - timedelta(days=r)) if r < US["quiet_days"] else None,
        "rmse": round(math.sqrt(sse / len(prices)), 4),
        "p_7d": 1 - (1 - q) ** HORIZON_DAYS,
        "markets": [{"market_id": m["id"], "question": m["question"], "end": m["endDate"],
                     "yes_price": p, "fitted": round(fp, 4)} for m, p, fp in zip(use, prices, fitted)],
        "url": market_url(use[0]),
    }


# ---- Combination -------------------------------------------------------------------------------
def combine(p_il, p_us):
    if p_il is None or p_us is None:
        return None
    return 1 - (1 - p_il) * (1 - p_us)


def _last_raw(con, source):
    row = con.execute("SELECT raw_json FROM observations WHERE source=? AND ok=1 AND value IS NOT NULL"
                      " ORDER BY observed_at DESC LIMIT 1", (source,)).fetchone()
    return json.loads(row[0]) if row else {}


# ---- Live collection ---------------------------------------------------------------------------
_run_values: dict = {}


def collect_il(con):
    fetched = now_iso()
    now = parse_iso(fetched)
    m = pick_il_market(series_markets(IL), now, live=True, current_id=_last_raw(con, SOURCE_IL).get("market_id"))
    if m is None:
        raise RuntimeError(f"no open '{IL['title_prefix']}' market ending >= {MIN_HORIZON_DAYS}d out")
    yes = _yes(m)
    days = (_end(m) - now).total_seconds() / 86400
    p_window, p_7d = il_prob(yes, days)
    raw = {**_market_info(m, yes), "days_to_end": round(days, 3), "p_window": round(p_window, 5),
           "p_7d": round(p_7d, 5)}
    write_obs(con, SOURCE_IL, p_7d, raw, ok=True, observed_at=fetched, fetched_at=fetched)
    _run_values["il"] = p_7d


def collect_us(con):
    fetched = now_iso()
    now = parse_iso(fetched)
    markets = [m for m in series_markets(US) if _is_open(m) and _end(m) > now]
    if not markets:
        raise RuntimeError(f"no open '{US['title_prefix']}' market")
    prev = _last_raw(con, SOURCE_US)
    r_hint = None
    if prev.get("est_last_strike"):
        r_hint = min(int((now - parse_iso(prev["est_last_strike"])).total_seconds() // 86400), US["quiet_days"] - 1)
    est = us_estimate(markets, now, {m["id"]: _yes(m) for m in markets}, r_hint)
    by_id = {m["id"]: m for m in markets}
    for x in est["markets"]:
        m = by_id[x["market_id"]]
        x.update(best_bid=m.get("bestBid"), best_ask=m.get("bestAsk"), volume_24h=m.get("volume24hr"),
                 liquidity=m.get("liquidityNum"))
    est["p_7d"] = round(est["p_7d"], 5)
    write_obs(con, SOURCE_US, est["p_7d"], est, ok=True, observed_at=fetched, fetched_at=fetched)
    _run_values["us"] = est["p_7d"]


def write_forecast(con):
    """After both collectors: store the combined market probability (Phase 0: model = market)."""
    p = combine(_run_values.get("il"), _run_values.get("us"))
    if p is None:
        raise RuntimeError("a component is missing this run; combined value not written")
    ts = now_iso()
    parts = {"p_il": _run_values["il"], "p_us": _run_values["us"]}
    con.execute("INSERT OR REPLACE INTO forecasts(ts, p_market, p_model, adjustments_json) VALUES (?,?,?,?)",
                (ts, p, p, json.dumps(parts)))
    write_obs(con, SOURCE_COMBINED, p, {**parts, "p_7d": round(p, 5)}, ok=True, observed_at=ts, fetched_at=ts)


def collect_kalshi(con):
    fetched = now_iso()
    if not KALSHI_EVENT_TICKERS:
        write_obs(con, SOURCE_KALSHI, None, {"note": "no Kalshi market with a comparable criterion"},
                  ok=True, observed_at=fetched, fetched_at=fetched)
        return
    out = []
    for t in KALSHI_EVENT_TICKERS:
        e = _get(f"{KALSHI}/events/{t}", {"with_nested_markets": "true"})
        for m in e.get("markets") or e.get("event", {}).get("markets") or []:
            out.append({k: m.get(k) for k in ("ticker", "yes_sub_title", "close_time", "last_price_dollars",
                                              "yes_bid_dollars", "yes_ask_dollars", "volume", "open_interest")})
    write_obs(con, SOURCE_KALSHI, None, {"markets": out}, ok=True, observed_at=fetched, fetched_at=fetched)


# ---- Backfill ----------------------------------------------------------------------------------
def _history(m) -> list[tuple[int, float]]:
    token_yes = json.loads(m["clobTokenIds"])[0]
    h = _get(f"{CLOB}/prices-history", {"market": token_yes, "interval": "max", "fidelity": 60})
    return sorted((int(x["t"]), float(x["p"])) for x in h.get("history", []))


def _price_at(hist, ts, max_age=3 * 3600):
    pts = [p for (pt, p) in hist if ts - max_age <= pt <= ts]
    return pts[-1] if pts else None


def backfill(con, days: int = BACKFILL_DAYS) -> dict:
    """Rebuild hourly IL, US and combined series for the last `days` with the live rules."""
    now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    start = now - timedelta(days=days)
    fetched = now_iso()

    il_all = [m for m in series_markets(IL, include_closed=True)
              if _end(m) >= start + timedelta(days=MIN_HORIZON_DAYS) and _start(m) <= now]
    us_all = [m for m in series_markets(US, include_closed=True) if _end(m) >= start and _start(m) <= now]
    hist = {m["id"]: _history(m) for m in il_all + us_all}

    counts = {"il": 0, "us": 0, "combined": 0}
    il_cur, last_strike, t = None, None, start
    while t <= now:
        ts = t.timestamp()
        p_il = p_us = None

        m = pick_il_market(il_all, t, live=False, current_id=il_cur)
        if m is not None and (yes := _price_at(hist[m["id"]], ts)) is not None:
            il_cur = m["id"]
            days_left = (_end(m) - t).total_seconds() / 86400
            p_window, p_il = il_prob(yes, days_left)
            write_obs(con, SOURCE_IL, p_il, {**_market_info(m, yes), "backfill": True,
                                             "days_to_end": round(days_left, 3), "p_window": round(p_window, 5),
                                             "p_7d": round(p_il, 5)},
                      ok=True, observed_at=to_iso(t), fetched_at=fetched)
            counts["il"] += 1

        prices = {}
        for um in us_all:
            if _start(um) <= t < _end(um) and (p := _price_at(hist[um["id"]], ts)) is not None:
                prices[um["id"]] = p
        if prices:
            r_hint = None
            if last_strike is not None:
                r_hint = min(int((t - last_strike).total_seconds() // 86400), US["quiet_days"] - 1)
            est = us_estimate(us_all, t, prices, r_hint)
            if est:
                p_us = est["p_7d"]
                if est["est_last_strike"]:
                    last_strike = parse_iso(est["est_last_strike"])
                est["p_7d"] = round(p_us, 5)
                write_obs(con, SOURCE_US, p_us, {**est, "backfill": True},
                          ok=True, observed_at=to_iso(t), fetched_at=fetched)
                counts["us"] += 1

        p = combine(p_il, p_us)
        if p is not None:
            parts = {"p_il": p_il, "p_us": p_us}
            con.execute("INSERT OR REPLACE INTO forecasts(ts, p_market, p_model, adjustments_json) VALUES (?,?,?,?)",
                        (to_iso(t), p, p, json.dumps({**parts, "backfill": True})))
            write_obs(con, SOURCE_COMBINED, p, {**parts, "p_7d": round(p, 5), "backfill": True},
                      ok=True, observed_at=to_iso(t), fetched_at=fetched)
            counts["combined"] += 1
        t += timedelta(hours=1)
    return counts


COLLECTORS = [(SOURCE_IL, collect_il), (SOURCE_US, collect_us), (SOURCE_COMBINED, write_forecast),
              (SOURCE_KALSHI, collect_kalshi)]
