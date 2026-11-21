"""USNI News Fleet and Marine Tracker (weekly): US carriers and amphibious ready groups in CENTCOM waters.

The tracker articles are organised under region headings ("In the Arabian Sea", "In the Red Sea", ...).
We split the text on those headings and collect hull numbers (CVN-xx for carriers, LHD/LHA-xx for
big-deck amphibs) that appear in CENTCOM sections. One row per article, keyed by its publish date.

A manual override can be supplied in data/manual/usni.json:
  {"2026-11-23": {"carriers": ["CVN-73", "CVN-77"], "amphibs": ["LHD-4"], "note": "..."}}
"""
import html
import json
import os
import re
from email.utils import parsedate_to_datetime

from collectors.common import get
from db import now_iso, to_iso, write_obs

SOURCE = "usni_carriers"
FEED = "https://news.usni.org/category/fleet-tracker/feed"
MANUAL = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "manual", "usni.json")

CENTCOM_WORDS = ("Arabian", "Red Sea", "Persian Gulf", "Gulf of Oman", "Gulf of Aden", "Middle East",
                 "Strait of Hormuz", "5th Fleet", "Fifth Fleet", "CENTCOM", "Bab el-Mandeb", "Suez")
# Capitalised "In the <Region>" / "In <Place>" headings mark section starts.
HEADING_RE = re.compile(r"\bIn (?:the )?((?:[A-Z][\w\.\-]*\s?){1,4}?)(?=\s+(?:[A-Z][a-z]|USS|Sailors?|Marines?|An?\s))")
CARRIER_RE = re.compile(r"\(CVN[\s\-–]?(\d{2})\)")
# Carriers are sometimes named without a hull number ("The George H.W. Bush Carrier Strike Group").
CARRIER_NAMES = {
    "Nimitz": 68, "Dwight D. Eisenhower": 69, "Eisenhower": 69, "Carl Vinson": 70, "Theodore Roosevelt": 71,
    "Abraham Lincoln": 72, "George Washington": 73, "John C. Stennis": 74, "Harry S. Truman": 75,
    "Ronald Reagan": 76, "George H.W. Bush": 77, "George H. W. Bush": 77, "Gerald R. Ford": 78,
    "John F. Kennedy": 79,
}
CARRIER_NAME_RE = re.compile(r"(?:USS\s+(" + "|".join(map(re.escape, CARRIER_NAMES)) + r")\b|\b("
                             + "|".join(map(re.escape, CARRIER_NAMES)) + r")\s+Carrier Strike Group)")
AMPHIB_RE = re.compile(r"\((LH[AD])[\s\-–]?(\d{1,2})\)")


def parse_article(body_html: str) -> dict:
    text = html.unescape(re.sub(r"<[^>]+>", " ", body_html))
    text = re.sub(r"\s+", " ", text)
    heads = [(m.start(), m.group(1).strip()) for m in HEADING_RE.finditer(text)]
    sections = []
    for i, (pos, name) in enumerate(heads):
        end = heads[i + 1][0] if i + 1 < len(heads) else len(text)
        sections.append((name, text[pos:end]))
    carriers, amphibs, regions = set(), set(), set()
    for name, chunk in sections:
        hit = next((w for w in CENTCOM_WORDS if w.lower() in name.lower()), None)
        if hit:
            regions.add(hit)
            carriers |= {f"CVN-{n}" for n in CARRIER_RE.findall(chunk)}
            carriers |= {f"CVN-{CARRIER_NAMES[a or b]}" for a, b in CARRIER_NAME_RE.findall(chunk)}
            amphibs |= {f"{t}-{n}" for t, n in AMPHIB_RE.findall(chunk)}
    return {"carriers": sorted(carriers), "amphibs": sorted(amphibs), "regions": sorted(regions)}


def collect(con):
    xml = get(FEED, as_json=False)
    items = re.findall(r"<item>(.*?)</item>", xml, re.S)
    if not items:
        raise RuntimeError("no items in USNI fleet tracker feed")
    manual = {}
    if os.path.exists(MANUAL):
        with open(MANUAL, encoding="utf-8") as f:
            manual = json.load(f)
    fetched = now_iso()
    for item in items:
        title = html.unescape(re.search(r"<title>(.*?)</title>", item, re.S).group(1))
        if "Fleet and Marine Tracker" not in title and "Fleet Tracker" not in title:
            continue
        published = parsedate_to_datetime(re.search(r"<pubDate>(.*?)</pubDate>", item).group(1))
        link = re.search(r"<link>(.*?)</link>", item).group(1).strip()
        body = re.search(r"<content:encoded><!\[CDATA\[(.*?)\]\]>", item, re.S)
        parsed = parse_article(body.group(1)) if body else {"carriers": [], "amphibs": [], "regions": []}
        day = published.date().isoformat()
        if day in manual:
            parsed = {**parsed, **manual[day], "manual": True}
        write_obs(con, SOURCE, float(len(parsed["carriers"])), {"title": title, "url": link, **parsed},
                  ok=True, observed_at=to_iso(published), fetched_at=fetched)


COLLECTORS = [(SOURCE, collect)]
FREQ_MIN = {SOURCE: 7 * 1440}
