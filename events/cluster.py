"""Articles -> events, and the Phase 2 signals (SPEC Phase 2, steps 4-5).

Articles about the same event merge when actor and action match, their event dates are within 48 hours,
and the targets match (equal, one contains the other, or word overlap). As a fallback for differently
worded targets, headlines with high word overlap also merge. (SPEC suggests embeddings here; Anthropic has
no embeddings endpoint, so word overlap is used until a second provider is worth adding.)

Events from the last REBUILD_DAYS are rebuilt every run, so late-classified articles land in the right event.

Signals written to observations:
  events      value = new high-severity (>=4) events first seen in the last 24h; raw has the breakdown and
              the top events with a source link (feeds "what changed" in Phase 4)
  coverage    value = distinct outlets carrying relevant articles in the last 24h
"""
import json
import re
from datetime import date, timedelta

from db import now_iso, parse_iso, to_iso, write_obs

SOURCE_EVENTS = "events"
SOURCE_COVERAGE = "coverage"
REBUILD_DAYS = 14
WINDOW_DAYS = 2
HIGH_SEVERITY = 4
SIGNAL_ACTORS = ("US", "Israel", "Iran", "proxy")
STOP = {"the", "a", "an", "of", "in", "on", "at", "to", "for", "and", "or", "with", "by", "from", "as", "after",
        "says", "say", "said", "over", "amid", "is", "are", "was", "be", "its", "it", "new", "s"}


def _words(s: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", (s or "").lower()) if w not in STOP and len(w) > 1}


def _jaccard(a: set, b: set) -> float:
    return len(a & b) / len(a | b) if a and b else 0.0


def _targets_match(t1: str, t2: str) -> bool:
    t1, t2 = (t1 or "").strip().lower(), (t2 or "").strip().lower()
    return t1 == t2 or (t1 and t2 and (t1 in t2 or t2 in t1)) or _jaccard(_words(t1), _words(t2)) >= 0.5


def _day(s: str) -> date:
    try:
        return date.fromisoformat(s[:10])
    except (TypeError, ValueError):
        return None


def build_events(con) -> int:
    cutoff = to_iso(parse_iso(now_iso()) - timedelta(days=REBUILD_DAYS))
    rows = con.execute("SELECT url, title, outlet, published_at, extracted_json FROM articles"
                       " WHERE extracted_json IS NOT NULL AND published_at>=? ORDER BY published_at",
                       (cutoff,)).fetchall()
    clusters = []
    for r in rows:
        x = json.loads(r["extracted_json"])
        if not x.get("relevant"):
            continue
        d = _day(x.get("event_date")) or _day(r["published_at"])
        words = _words(r["title"])
        home = None
        for c in clusters:
            if c["actor"] != x["actor"] or c["action"] != x["action"]:
                continue
            if abs((c["date"] - d).days) > WINDOW_DAYS:
                continue
            if _targets_match(c["target"], x["target"]) or max(_jaccard(words, w) for w in c["title_words"]) >= 0.5:
                home = c
                break
        if home is None:
            home = {"actor": x["actor"], "action": x["action"], "target": x["target"], "date": d,
                    "severity": 0, "outlets": set(), "first_seen": r["published_at"], "is_new": False,
                    "urls": [], "title_words": [], "sample": None}
            clusters.append(home)
        home["date"] = min(home["date"], d)
        home["outlets"].add(r["outlet"])
        home["is_new"] = home["is_new"] or bool(x.get("is_new_event"))
        home["urls"].append(r["url"])
        home["title_words"].append(words)
        if x["severity"] > home["severity"]:
            home["severity"], home["sample"] = x["severity"], (r["url"], r["title"])

    con.execute("UPDATE articles SET event_id=NULL WHERE published_at>=?", (cutoff,))
    con.execute("DELETE FROM events WHERE first_seen>=?", (cutoff,))
    for c in clusters:
        cur = con.execute(
            "INSERT INTO events(actor, action, target, event_date, severity, n_sources, first_seen, is_new_event,"
            " sample_url, sample_title) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (c["actor"], c["action"], c["target"], c["date"].isoformat(), c["severity"], len(c["outlets"]),
             c["first_seen"], int(c["is_new"]), c["sample"][0], c["sample"][1]))
        con.executemany("UPDATE articles SET event_id=? WHERE url=?", [(cur.lastrowid, u) for u in c["urls"]])
    return len(clusters)


def collect(con):
    n = build_events(con)
    fetched = now_iso()
    since = to_iso(parse_iso(fetched) - timedelta(hours=24))
    recent = [dict(r) for r in con.execute(
        "SELECT * FROM events WHERE first_seen>=? ORDER BY severity DESC, n_sources DESC", (since,))]
    high = [e for e in recent if e["severity"] >= HIGH_SEVERITY and e["is_new_event"] and e["actor"] in SIGNAL_ACTORS]
    by_actor = {}
    for e in high:
        by_actor[e["actor"]] = by_actor.get(e["actor"], 0) + 1
    top = [{k: e[k] for k in ("actor", "action", "target", "event_date", "severity", "n_sources", "first_seen",
                              "sample_url", "sample_title")} for e in recent[:8]]
    write_obs(con, SOURCE_EVENTS, float(len(high)),
              {"high_new_24h_by_actor": by_actor, "events_24h": len(recent), "events_rebuilt": n, "top": top},
              ok=True, observed_at=fetched, fetched_at=fetched)

    outlets = con.execute(
        "SELECT COUNT(DISTINCT outlet), COUNT(*) FROM articles WHERE published_at>=? AND extracted_json IS NOT NULL"
        " AND json_extract(extracted_json, '$.relevant')=1", (since,)).fetchone()
    pending = con.execute("SELECT COUNT(*) FROM articles WHERE published_at>=? AND extracted_json IS NULL",
                          (since,)).fetchone()[0]
    write_obs(con, SOURCE_COVERAGE, float(outlets[0]),
              {"relevant_articles_24h": outlets[1], "unclassified_24h": pending},
              ok=True, observed_at=fetched, fetched_at=fetched)


def collect_or_skip(con):
    """If nothing has been classified yet, the signals would read as 0, which looks like calm. Fail instead."""
    if not con.execute("SELECT 1 FROM articles WHERE extracted_json IS NOT NULL LIMIT 1").fetchone():
        raise RuntimeError("no classified articles yet (extraction not running)")
    last = con.execute("SELECT MAX(fetched_at) FROM observations WHERE source='haiku_extract' AND ok=1").fetchone()[0]
    if not last or parse_iso(now_iso()) - parse_iso(last) > timedelta(minutes=90):
        raise RuntimeError(f"extraction has not succeeded since {last}; 24h counts would undercount")
    collect(con)
