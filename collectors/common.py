"""Shared helpers for collectors."""
import json
import time
from datetime import timedelta

import requests

from db import now_iso, parse_iso

TIMEOUT = 30
session = requests.Session()
session.headers["User-Agent"] = "iran-strike-tracker/0.1 (+https://github.com/shmulik158-design/strikeprobability)"


def get(url, params=None, headers=None, as_json=True):
    for attempt in range(3):
        try:
            r = session.get(url, params=params, headers=headers, timeout=TIMEOUT)
            r.raise_for_status()
            return r.json() if as_json else r.text
        except requests.RequestException:
            if attempt == 2:
                raise
            time.sleep(2 * (attempt + 1))


def last_snapshot(con, source) -> dict | None:
    """Most recent full snapshot stored for a snapshot-on-change source."""
    row = con.execute("SELECT raw_json FROM observations WHERE source=? AND ok=1 AND raw_json LIKE '%\"snapshot\"%'"
                      " ORDER BY observed_at DESC LIMIT 1", (source,)).fetchone()
    return json.loads(row[0])["snapshot"] if row else None


def recent_changes(con, source, hours) -> list[dict]:
    since = (parse_iso(now_iso()) - timedelta(hours=hours)).isoformat().replace("+00:00", "Z")
    out = []
    for (raw,) in con.execute("SELECT raw_json FROM observations WHERE source=? AND ok=1 AND observed_at>=?"
                              " AND raw_json LIKE '%\"changes\"%' ORDER BY observed_at", (source, since)):
        out.extend(json.loads(raw).get("changes", []))
    return out
