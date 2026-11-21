"""OVX (CBOE crude oil volatility index) from FRED series OVXCLS, via the public fredgraph CSV (no key).

One row per trading day, keyed by the data date, so re-fetching is idempotent. `value` is the index
level; raw_json carries the z-score against the previous 30 trading days (the signal in SPEC).
"""
import statistics
from datetime import date, timedelta

from collectors.common import get
from db import now_iso, write_obs

SOURCE = "ovx"
URL = "https://fred.stlouisfed.org/graph/fredgraph.csv"
WINDOW = 30
LOOKBACK_DAYS = 120


def collect(con):
    text = get(URL, {"id": "OVXCLS", "cosd": (date.today() - timedelta(days=LOOKBACK_DAYS)).isoformat()},
               as_json=False)
    rows = []
    for line in text.strip().splitlines()[1:]:
        d, _, v = line.partition(",")
        try:
            rows.append((d.strip(), float(v)))
        except ValueError:      # FRED uses "." for holidays
            continue
    if len(rows) < WINDOW + 1:
        raise RuntimeError(f"only {len(rows)} OVX observations returned")
    fetched = now_iso()
    for i in range(WINDOW, len(rows)):
        d, v = rows[i]
        prior = [x for _, x in rows[i - WINDOW:i]]
        mu, sd = statistics.fmean(prior), statistics.stdev(prior)
        z = (v - mu) / sd if sd > 0 else 0.0
        # 21:00Z ~ after the US close; the date itself is what matters.
        write_obs(con, SOURCE, v, {"date": d, "z30": round(z, 3), "mean30": round(mu, 3), "sd30": round(sd, 3)},
                  ok=True, observed_at=f"{d}T21:00:00Z", fetched_at=fetched)


COLLECTORS = [(SOURCE, collect)]
# Daily data; weekends and holidays leave gaps, so allow 2 days before calling it stale (2x = 4 days).
FREQ_MIN = {SOURCE: 2 * 1440}
