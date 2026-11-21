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

from collectors import markets as mk
from db import connect, now_iso, parse_iso, write_obs

ROOT = os.path.dirname(os.path.abspath(__file__))
LATEST = os.path.join(ROOT, "site", "latest.json")
SERIES_DAYS = 30

# Expected update interval per source, in minutes (a source is stale after 2x this).
FREQ_MIN = {mk.SOURCE_COMBINED: 30, mk.SOURCE_IL: 30, mk.SOURCE_US: 30, mk.SOURCE_KALSHI: 30}
COLLECTORS = list(mk.COLLECTORS)
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
        last_ok = con.execute(f"SELECT MAX(fetched_at) FROM observations WHERE source=? AND ok=1 {NOT_BACKFILL}",
                              (source,)).fetchone()[0]
        stale = last_ok is None or (parse_iso(now) - parse_iso(last_ok)) > timedelta(minutes=2 * freq)
        raw = json.loads(last["raw_json"]) if last and last["raw_json"] else {}
        sources.append({"source": source, "freq_min": freq, "last_attempt": last["fetched_at"] if last else None,
                        "last_ok": last_ok, "stale": stale,
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
