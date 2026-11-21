"""Entry point: run every collector, then export site/latest.json.

    python run.py              # collect + export (backfills automatically on an empty database)
    python run.py --backfill   # force a price-history backfill first
    python run.py --export-only
"""
import argparse
import json
import os
import sys
import traceback
from datetime import timedelta

from collectors import advisories, notams, ovx, usni
from collectors import markets as mk
from collectors.common import recent_changes
from db import connect, now_iso, parse_iso, write_obs
from events import cluster as ev_cluster
from events import extract as ev_extract
from events import fetch as ev_fetch

ROOT = os.path.dirname(os.path.abspath(__file__))
LATEST = os.path.join(ROOT, "site", "latest.json")
SERIES_DAYS = 30
SIGNAL_MODULES = [notams, advisories, ovx, usni]

# Expected data interval per source, in minutes (a source is stale when its newest data is older than 2x this).
FREQ_MIN = {mk.SOURCE_COMBINED: 30, mk.SOURCE_IL: 30, mk.SOURCE_US: 30, mk.SOURCE_KALSHI: 30}
COLLECTORS = list(mk.COLLECTORS)
for _m in SIGNAL_MODULES:
    COLLECTORS += _m.COLLECTORS
    FREQ_MIN.update(_m.FREQ_MIN)
# Phase 2: event layer (fetch -> extract -> cluster), in this order.
COLLECTORS += [(ev_fetch.SOURCE, ev_fetch.collect), (ev_extract.SOURCE, ev_extract.collect),
               (ev_cluster.SOURCE_EVENTS, ev_cluster.collect_or_skip)]
FREQ_MIN.update({ev_fetch.SOURCE: 30, ev_extract.SOURCE: 30, ev_cluster.SOURCE_EVENTS: 30,
                 ev_cluster.SOURCE_COVERAGE: 30})
NOT_BACKFILL = "AND raw_json NOT LIKE '%\"backfill\": true%'"


def run_collectors(con) -> bool:
    all_ok = True
    for source, fn in COLLECTORS:
        try:
            fn(con)
            print(f"[ok]   {source}")
        except Exception as e:  # one failing source must not stop the rest
            all_ok = False
            write_obs(con, source, None, {"error": f"{type(e).__name__}: {e}"}, ok=False)
            print(f"[fail] {source}: {e}", file=sys.stderr)
            traceback.print_exc()
        con.commit()
    return all_ok


def _latest_live(con, source):
    row = con.execute(f"SELECT observed_at, value, raw_json FROM observations WHERE source=? AND ok=1"
                      f" AND value IS NOT NULL {NOT_BACKFILL} ORDER BY observed_at DESC LIMIT 1", (source,)).fetchone()
    return {"observed_at": row["observed_at"], **json.loads(row["raw_json"])} if row else None


def indicators(con) -> dict:
    """Latest value of each Phase-1 signal, for display. None = missing (never 0)."""
    def latest(source, n=1):
        rows = con.execute("SELECT observed_at, value, raw_json FROM observations WHERE source=? AND ok=1"
                           " ORDER BY observed_at DESC LIMIT ?", (source, n)).fetchall()
        return [{"observed_at": r["observed_at"], "value": r["value"], **json.loads(r["raw_json"] or "{}")}
                for r in rows]

    out = {}
    n = latest(notams.SOURCE)
    out["notams"] = None if not n else {
        "observed_at": n[0]["observed_at"], "value": n[0]["value"],
        "no_coverage": n[0].get("no_coverage"), "per_fir": n[0].get("per_fir")}
    for src in (advisories.SOURCE_US, advisories.SOURCE_UK):
        a = latest(src)
        out[src] = None if not a else {
            "observed_at": a[0]["observed_at"], "value": a[0]["value"], "levels": a[0].get("levels"),
            "recent": [c for c in recent_changes(con, src, 24 * 7) if c["kind"] != "updated"][-5:]}
    o = latest(ovx.SOURCE)
    out["ovx"] = None if not o else {"date": o[0]["date"], "value": o[0]["value"], "z30": o[0]["z30"]}
    u = latest(usni.SOURCE, 2)
    out["usni"] = None if not u else {
        "date": u[0]["observed_at"], "carriers": u[0]["carriers"], "amphibs": u[0]["amphibs"],
        "change_vs_prev": (len(u[0]["carriers"]) - len(u[1]["carriers"])) if len(u) > 1 else None,
        "title": u[0]["title"], "url": u[0]["url"]}
    e, c = latest(ev_cluster.SOURCE_EVENTS), latest(ev_cluster.SOURCE_COVERAGE)
    out["events"] = None if not e else {
        "observed_at": e[0]["observed_at"], "high_new_24h": e[0]["value"],
        "by_actor": e[0].get("high_new_24h_by_actor"), "events_24h": e[0].get("events_24h"), "top": e[0].get("top")}
    out["coverage"] = None if not c else {
        "observed_at": c[0]["observed_at"], "outlets_24h": c[0]["value"],
        "relevant_articles_24h": c[0].get("relevant_articles_24h"), "unclassified_24h": c[0].get("unclassified_24h")}
    return out


def export(con) -> dict:
    now = now_iso()
    since = (parse_iso(now) - timedelta(days=SERIES_DAYS)).isoformat().replace("+00:00", "Z")

    series = [{"t": r["observed_at"], "p": round(r["value"], 5),
               "il": round(json.loads(r["raw_json"])["p_il"], 5), "us": round(json.loads(r["raw_json"])["p_us"], 5)}
              for r in con.execute("SELECT observed_at, value, raw_json FROM observations WHERE source=? AND ok=1"
                                   " AND value IS NOT NULL AND observed_at>=? ORDER BY observed_at",
                                   (mk.SOURCE_COMBINED, since))]

    switches, prev = [], None
    for r in con.execute("SELECT observed_at, raw_json FROM observations WHERE source=? AND ok=1"
                         " AND value IS NOT NULL AND observed_at>=? ORDER BY observed_at", (mk.SOURCE_IL, since)):
        raw = json.loads(r["raw_json"])
        if prev and prev["market_id"] != raw["market_id"]:
            switches.append({"t": r["observed_at"], "from": prev["question"], "to": raw["question"]})
        prev = raw

    sources = []
    for source, freq in FREQ_MIN.items():
        last = con.execute("SELECT fetched_at, ok, raw_json FROM observations WHERE source=?"
                           f" {NOT_BACKFILL} ORDER BY fetched_at DESC LIMIT 1", (source,)).fetchone()
        last_ok, last_data = con.execute(f"SELECT MAX(fetched_at), MAX(observed_at) FROM observations"
                                         f" WHERE source=? AND ok=1 {NOT_BACKFILL}", (source,)).fetchone()
        stale = last_data is None or (parse_iso(now) - parse_iso(last_data)) > timedelta(minutes=2 * freq)
        raw = json.loads(last["raw_json"]) if last and last["raw_json"] else {}
        sources.append({"source": source, "freq_min": freq, "last_attempt": last["fetched_at"] if last else None,
                        "last_ok": last_ok, "last_data": last_data, "stale": stale,
                        "error": raw.get("error") if last and not last["ok"] else None, "note": raw.get("note")})

    out = {
        "generated_at": now,
        "horizon_days": mk.HORIZON_DAYS,
        "series_def": {"il": mk.IL, "us": mk.US},
        "current": {"combined": _latest_live(con, mk.SOURCE_COMBINED),
                    "il": _latest_live(con, mk.SOURCE_IL), "us": _latest_live(con, mk.SOURCE_US)},
        "series": series,
        "switches": switches,
        "sources": sources,
        "indicators": indicators(con),
    }
    os.makedirs(os.path.dirname(LATEST), exist_ok=True)
    with open(LATEST, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--backfill", action="store_true")
    ap.add_argument("--export-only", action="store_true")
    args = ap.parse_args()
    con = connect()
    if not args.export_only:
        empty = con.execute("SELECT COUNT(*) FROM observations WHERE source=?",
                            (mk.SOURCE_COMBINED,)).fetchone()[0] == 0
        if args.backfill or empty:
            try:
                print(f"[ok]   backfill: {mk.backfill(con)}")
                con.commit()
            except Exception as e:
                print(f"[fail] backfill: {e}", file=sys.stderr)
                traceback.print_exc()
        run_collectors(con)
    out = export(con)
    c = out["current"]
    if c["combined"]:
        print(f"exported: P={c['combined']['p_7d']:.3f}  (IL {c['combined']['p_il']:.3f}, US {c['combined']['p_us']:.3f})")
    else:
        print("exported (no combined value)")
    con.close()


if __name__ == "__main__":
    main()
