"""NOTAMs from the official FAA NOTAM API (https://api.faa.gov), which needs a free client_id/secret.

Set FAA_CLIENT_ID and FAA_CLIENT_SECRET (GitHub Actions secrets). Without them every run writes an ok=0
row saying so, so the page shows the source as missing rather than as zero.

Signal (per SPEC): number of new airspace restrictions issued in the last 48 hours across the FIRs of
Iran, Iraq, Jordan, Israel, Lebanon and the Gulf states. "Restriction" = ICAO Q-code subject in the
airspace-restriction / warning / closure families, or restriction wording in the text.
Whether the FAA API returns foreign-FIR NOTAMs is an open question in SPEC: per-FIR totals are kept so a
FIR that never returns anything is visible as "no coverage" instead of silently counting as calm.
"""
import os
import re
from datetime import timedelta

from collectors.common import get
from db import now_iso, parse_iso, write_obs

SOURCE = "notams"
API = "https://external-api.faa.gov/notamapi/v1/notams"
WINDOW_HOURS = 48
FIRS = {
    "OIIX": "Tehran", "ORBB": "Baghdad", "OJAC": "Amman", "LLLL": "Tel Aviv", "OLBB": "Beirut",
    "OBBB": "Bahrain", "OKAC": "Kuwait", "OEJD": "Jeddah", "OMAE": "Emirates", "OOMM": "Muscat", "OTDF": "Doha",
}
# Q-code subjects: R* = airspace restrictions (RA, RD danger, RP prohibited, RR restricted, RT temporary, RM military),
# W* = navigation warnings (missile/gun firing, air display, ...), AF/AR/AE = FIR/route/control-area changes.
QCODE_RE = re.compile(r"^Q(R[ADMPRTO]|W[A-Z]|AF|AR|AE)")
TEXT_RE = re.compile(r"\b(RESTRICTED|PROHIBITED|DANGER AREA|AIRSPACE CLSD|CLOSED|MISSILE|ROCKET|UNMANNED|UAS|"
                     r"MIL(ITARY)? (ACT|OPS)|NOT AVBL|AVOID)\b")


def _fetch_fir(fir, headers):
    items, page = [], 1
    while True:
        d = get(API, {"icaoLocation": fir, "pageSize": 1000, "pageNum": page}, headers=headers)
        items += d.get("items") or []
        if page >= (d.get("totalPages") or 1):
            return items, d.get("totalCount", len(items))
        page += 1


def collect(con):
    cid, secret = os.environ.get("FAA_CLIENT_ID"), os.environ.get("FAA_CLIENT_SECRET")
    if not cid or not secret:
        raise RuntimeError("FAA_CLIENT_ID / FAA_CLIENT_SECRET not set (register at https://api.faa.gov)")
    headers = {"client_id": cid, "client_secret": secret}
    now = parse_iso(now_iso())
    since = now - timedelta(hours=WINDOW_HOURS)
    per_fir, new_restrictions = {}, []
    for fir, name in FIRS.items():
        items, total = _fetch_fir(fir, headers)
        n_new = 0
        for it in items:
            n = (((it.get("properties") or {}).get("coreNOTAMData") or {}).get("notam") or {})
            issued = n.get("issued")
            if not issued or parse_iso(issued) < since:
                continue
            q = (n.get("selectionCode") or "").upper()
            text = (n.get("text") or "").upper()
            if QCODE_RE.match(q) or TEXT_RE.search(text):
                n_new += 1
                new_restrictions.append({"fir": fir, "number": n.get("number"), "issued": issued, "q": q,
                                         "start": n.get("effectiveStart"), "end": n.get("effectiveEnd"),
                                         "text": text[:300]})
        per_fir[fir] = {"name": name, "total_active": total, "new_restrictions_48h": n_new}
    no_coverage = [f for f, v in per_fir.items() if v["total_active"] == 0]
    fetched = now_iso()
    write_obs(con, SOURCE, float(len(new_restrictions)),
              {"per_fir": per_fir, "no_coverage": no_coverage, "new": new_restrictions[:50]},
              ok=True, observed_at=fetched, fetched_at=fetched)


COLLECTORS = [(SOURCE, collect)]
FREQ_MIN = {SOURCE: 30}
