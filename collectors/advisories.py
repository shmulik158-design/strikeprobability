"""Travel advisories: US State Department (RSS) and UK FCDO (gov.uk Content API).

Signal (per SPEC): a binary, strong event. We keep a per-country snapshot and record a change whenever
a level goes up or a departure order appears. `value` = number of escalations in the last 48 hours.
A full snapshot is stored only when something changed; otherwise the row just marks a successful check.
"""
import html
import re

from collectors.common import get, last_snapshot, recent_changes
from db import now_iso, write_obs

SOURCE_US = "advisory_us"
SOURCE_UK = "advisory_uk"
WINDOW_HOURS = 48

STATE_RSS = "https://travel.state.gov/_res/rss/TAsTWs.xml"
# RSS title prefix -> short key
US_COUNTRIES = {
    "Iran": "IR", "Israel": "IL", "Iraq": "IQ", "Lebanon": "LB", "Jordan": "JO", "Qatar": "QA",
    "Bahrain": "BH", "Kuwait": "KW", "United Arab Emirates": "AE", "Saudi Arabia": "SA", "Oman": "OM",
}
UK_COUNTRIES = {
    "iran": "IR", "israel": "IL", "iraq": "IQ", "lebanon": "LB", "jordan": "JO", "qatar": "QA",
    "bahrain": "BH", "kuwait": "KW", "united-arab-emirates": "AE", "saudi-arabia": "SA", "oman": "OM",
}
UK_ALERT_LEVEL = {
    "avoid_all_travel_to_whole_country": 4,
    "avoid_all_travel_to_parts": 3,
    "avoid_all_but_essential_travel_to_whole_country": 3,
    "avoid_all_but_essential_travel_to_parts": 2,
}
DEPARTURE_RE = re.compile(r"\b(ordered|authorized)\s+(?:the\s+)?departure\b", re.I)


def _diff(prev: dict | None, snap: dict, at: str) -> list[dict]:
    if prev is None:
        return []
    changes = []
    for k, cur in snap.items():
        old = prev.get(k)
        if old is None:
            continue
        if cur["level"] > old["level"]:
            changes.append({"t": at, "country": k, "kind": "level_up", "from": old["level"], "to": cur["level"],
                            "text": cur.get("title") or cur.get("change")})
        elif cur["level"] < old["level"]:
            changes.append({"t": at, "country": k, "kind": "level_down", "from": old["level"], "to": cur["level"],
                            "text": cur.get("title") or cur.get("change")})
        if cur.get("departure") and cur["departure"] != old.get("departure"):
            changes.append({"t": at, "country": k, "kind": f"{cur['departure']}_departure",
                            "text": cur.get("title")})
        if cur.get("updated") != old.get("updated") and cur.get("change"):
            changes.append({"t": at, "country": k, "kind": "updated", "text": cur["change"]})
    return changes


def _store(con, source, snap):
    at = now_iso()
    prev = last_snapshot(con, source)
    changes = _diff(prev, snap, at)
    escalations = [c for c in changes if c["kind"] in ("level_up", "ordered_departure", "authorized_departure")]
    recent = [c for c in recent_changes(con, source, WINDOW_HOURS)
              if c["kind"] in ("level_up", "ordered_departure", "authorized_departure")] + escalations
    raw = {"levels": {k: v["level"] for k, v in snap.items()}}
    if prev is None or snap != prev:
        raw["snapshot"] = snap
    if changes:
        raw["changes"] = changes
    write_obs(con, source, float(len(recent)), raw, ok=True, observed_at=at, fetched_at=at)


def collect_us(con):
    xml = get(STATE_RSS, as_json=False)
    snap = {}
    for item in re.findall(r"<item>(.*?)</item>", xml, re.S):
        title = html.unescape(re.search(r"<title>(.*?)</title>", item, re.S).group(1)).strip()
        name, _, rest = title.partition(" - ")
        key = US_COUNTRIES.get(name.split(",")[0].strip())
        m = re.search(r"Level (\d)", rest)
        if not key or not m:
            continue
        desc = html.unescape(re.sub(r"<[^>]+>", " ", html.unescape(
            (re.search(r"<description>(.*?)</description>", item, re.S) or [None, ""])[1])))
        dep = DEPARTURE_RE.search(desc)
        pub = re.search(r"<pubDate>(.*?)</pubDate>", item)
        snap[key] = {"level": int(m.group(1)), "title": title, "updated": pub.group(1).strip() if pub else None,
                     "departure": dep.group(1).lower() if dep else None}
    missing = set(US_COUNTRIES.values()) - set(snap)
    if missing:
        raise RuntimeError(f"State Dept RSS missing countries: {sorted(missing)}")
    _store(con, SOURCE_US, snap)


def collect_uk(con):
    snap = {}
    for slug, key in UK_COUNTRIES.items():
        d = get(f"https://www.gov.uk/api/content/foreign-travel-advice/{slug}")
        det = d.get("details", {})
        alerts = det.get("alert_status") or []
        level = max([UK_ALERT_LEVEL.get(a, 1) for a in alerts] or [1])
        snap[key] = {"level": level, "alerts": alerts, "updated": d.get("public_updated_at"),
                     "change": (det.get("change_description") or "").strip() or None}
    _store(con, SOURCE_UK, snap)


COLLECTORS = [(SOURCE_US, collect_us), (SOURCE_UK, collect_uk)]
FREQ_MIN = {SOURCE_US: 60, SOURCE_UK: 60}
