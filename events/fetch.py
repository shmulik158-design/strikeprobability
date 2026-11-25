"""GDELT DOC API -> articles table, with cheap de-duplication before any model call (SPEC Phase 2, steps 1-2).

GDELT allows one request every 5 seconds; shared CI runners may hit 429 anyway, so a failed fetch writes
ok=0 and the rest of the run continues. Articles keep only title/url/outlet/time: artlist mode has no
summary, so extraction works from the headline.
"""
import re
import time
from datetime import datetime, timedelta, timezone

import requests

from collectors.common import session
from db import now_iso, parse_iso, to_iso, write_obs

SOURCE = "gdelt"
API = "https://api.gdeltproject.org/api/v2/doc/doc"
# GDELT rejects long queries ("too short or too long"); this form is known to work. Haiku filters relevance.
QUERY = ("(Iran OR Iranian OR Tehran) (strike OR attack OR missile OR military OR ceasefire OR evacuation "
         "OR drone OR airstrike OR IRGC) sourcelang:english")
MAX_RECORDS = 250
DEDUPE_DAYS = 3
FIRST_RUN_HOURS = 24


def normalize_title(title: str) -> str:
    t = title.lower()
    t = re.sub(r"\s[-|–—]\s[^-|–—]{2,40}$", "", t)   # trailing " - Outlet Name"
    t = re.sub(r"[^a-z0-9 ]+", " ", t)
    return re.sub(r"\s+", " ", t).strip()


BACKOFF_S = [10, 20, 40, 60]   # shared CI runners hit 429 often; 5 of 11 runs failed with 6-24s waits
SPLIT_DEPTH = 3                # a capped window is halved up to 3 times (at most 8 sub-windows)


def _gdelt(params) -> dict:
    for wait in BACKOFF_S + [None]:
        r = session.get(API, params=params, timeout=60)
        if r.status_code == 429:
            if wait is None:
                break
            time.sleep(wait)
            continue
        r.raise_for_status()
        if not r.text.strip():
            return {"articles": []}
        try:
            return r.json()
        except ValueError:
            raise RuntimeError(f"GDELT returned non-JSON: {r.text[:200]}")
    raise RuntimeError(f"GDELT rate limit (429) after {len(BACKOFF_S) + 1} attempts")


def _fetch_window(start, end, depth=0) -> tuple[list, int]:
    """Articles in [start, end]. If GDELT returns the cap, split the window so nothing is silently dropped.
    Returns (articles, number_of_windows_still_capped)."""
    d = _gdelt({"query": QUERY, "mode": "artlist", "format": "json", "maxrecords": MAX_RECORDS, "sort": "datedesc",
                "startdatetime": start.strftime("%Y%m%d%H%M%S"), "enddatetime": end.strftime("%Y%m%d%H%M%S")})
    arts = d.get("articles") or []
    if len(arts) < MAX_RECORDS or depth >= SPLIT_DEPTH:
        return arts, int(len(arts) >= MAX_RECORDS)
    mid = start + (end - start) / 2
    time.sleep(6)
    a1, c1 = _fetch_window(start, mid, depth + 1)
    time.sleep(6)
    a2, c2 = _fetch_window(mid, end, depth + 1)
    return a1 + a2, c1 + c2


def collect(con):
    fetched = now_iso()
    now = parse_iso(fetched)
    last = con.execute("SELECT MAX(fetched_at) FROM articles").fetchone()[0]
    start = parse_iso(last) - timedelta(minutes=15) if last else now - timedelta(hours=FIRST_RUN_HOURS)
    start = max(start, now - timedelta(days=1))
    articles, still_capped = _fetch_window(start, now)
    since = to_iso(now - timedelta(days=DEDUPE_DAYS))
    new = dup_url = dup_title = 0
    for a in articles:
        url, title = a.get("url"), (a.get("title") or "").strip()
        if not url or not title:
            continue
        if con.execute("SELECT 1 FROM articles WHERE url=?", (url,)).fetchone():
            dup_url += 1
            continue
        tn = normalize_title(title)
        if con.execute("SELECT 1 FROM articles WHERE title_norm=? AND published_at>=?", (tn, since)).fetchone():
            dup_title += 1
            continue
        seen = a.get("seendate")  # 20261121T214500Z
        published = to_iso(datetime.strptime(seen, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)) if seen else fetched
        con.execute("INSERT INTO articles(url, title, outlet, published_at, extracted_json, title_norm, fetched_at)"
                    " VALUES (?,?,?,?,NULL,?,?)", (url, title, a.get("domain"), published, tn, fetched))
        new += 1
    write_obs(con, SOURCE, float(new), {"returned": len(articles), "new": new, "dup_url": dup_url,
                                       "dup_title": dup_title, "window_start": to_iso(start),
                                       "capped_windows": still_capped},
              ok=True, observed_at=fetched, fetched_at=fetched)
