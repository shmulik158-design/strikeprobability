"""Headline -> structured event with Claude Haiku (SPEC Phase 2, step 3).

Articles are sent in batches; the reply is constrained to a JSON schema (structured outputs), one item per
article index. The result is stored in articles.extracted_json, which doubles as the per-URL cache: an
article is never classified twice. Needs ANTHROPIC_API_KEY (a GitHub Actions secret); without it the run
writes ok=0 and articles wait, unclassified, for a later run.
"""
import json
import os
from datetime import timedelta

from db import now_iso, parse_iso, to_iso, write_obs

SOURCE = "haiku_extract"
MODEL = "claude-haiku-5-5"
BATCH = 25
MAX_PER_RUN = 300
MAX_AGE_DAYS = 3

ACTORS = ["US", "Israel", "Iran", "proxy", "other"]
ACTIONS = ["strike", "threat", "deployment", "evacuation", "seizure", "talks", "ceasefire", "other"]

SCHEMA = {
    "type": "object",
    "properties": {
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "i": {"type": "integer"},
                    "relevant": {"type": "boolean"},
                    "actor": {"type": "string", "enum": ACTORS},
                    "action": {"type": "string", "enum": ACTIONS},
                    "target": {"type": "string"},
                    "event_date": {"type": "string"},
                    "is_new_event": {"type": "boolean"},
                    "severity": {"type": "integer"},
                    "diplomatic_context": {"type": "boolean"},
                },
                "required": ["i", "relevant", "actor", "action", "target", "event_date", "is_new_event",
                             "severity", "diplomatic_context"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["items"],
    "additionalProperties": False,
}

SYSTEM = """You classify news headlines for a tracker of US and Israeli military action against Iran.
For each numbered headline, return one item with the same index `i`.

- relevant: true only if the headline reports something concrete about the US-Iran or Israel-Iran military
  situation (strikes, threats, deployments, evacuations, seizures, talks, ceasefires, involving Iran or its
  proxies). Opinion pieces, market reports and unrelated stories are false; still fill the other fields.
- actor: who acts. US, Israel, Iran, proxy (Hezbollah, Houthis, Iraqi militias, ...) or other.
- action: strike (kinetic attack carried out), threat (warning or threat of force), deployment (forces moved
  or readied), evacuation (embassy, staff or civilian evacuation, travel warnings), seizure (ship or asset
  seized), talks, ceasefire, other.
- target: short lowercase name of the main target or counterpart, e.g. "isfahan", "natanz", "israel",
  "us base qatar", "iran". Use "iran" when no specific place is named.
- event_date: YYYY-MM-DD when the event itself happened (not when it was published). Resolve "today",
  "overnight", "yesterday" against the publication time given with each headline. If unclear, use the
  publication date.
- is_new_event: true if the headline reports a fresh development; false for analysis, recaps, anniversaries,
  or follow-ups on something already reported days ago.
- severity: 1 routine/diplomatic, 2 rhetoric or minor incident, 3 notable threat/deployment/proxy attack,
  4 direct strike or credible imminent-strike signal (evacuation order, explicit ultimatum), 5 major
  direct US or Israeli strike on Iranian territory or a large Iranian strike.
- diplomatic_context: true if talks, mediation or a ceasefire framework are part of the story."""


def _client():
    import anthropic  # imported lazily so collectors that don't need it never require the package
    return anthropic.Anthropic()


def _classify(client, batch):
    lines = [f"{i}. [{a['published_at']} | {a['outlet']}] {a['title']}" for i, a in enumerate(batch)]
    resp = client.messages.create(
        model=MODEL,
        max_tokens=8000,
        output_config={"effort": "low", "format": {"type": "json_schema", "schema": SCHEMA}},
        system=SYSTEM,
        messages=[{"role": "user", "content": "\n".join(lines)}],
    )
    if resp.stop_reason == "refusal":
        raise RuntimeError(f"model refused batch: {getattr(resp.stop_details, 'category', None)}")
    if resp.stop_reason == "max_tokens":
        raise RuntimeError("extraction reply truncated (max_tokens)")
    text = next(b.text for b in resp.content if b.type == "text")
    items = {it["i"]: it for it in json.loads(text)["items"]}
    return items, resp.usage


def collect(con):
    if not os.environ.get("ANTHROPIC_API_KEY") and not os.environ.get("ANTHROPIC_AUTH_TOKEN"):
        pending = con.execute("SELECT COUNT(*) FROM articles WHERE extracted_json IS NULL").fetchone()[0]
        raise RuntimeError(f"ANTHROPIC_API_KEY not set; {pending} articles waiting")
    since = to_iso(parse_iso(now_iso()) - timedelta(days=MAX_AGE_DAYS))
    rows = [dict(r) for r in con.execute(
        "SELECT url, title, outlet, published_at FROM articles WHERE extracted_json IS NULL AND published_at>=?"
        " ORDER BY published_at DESC LIMIT ?", (since, MAX_PER_RUN))]
    client = _client() if rows else None
    done = in_tok = out_tok = 0
    errors = []
    for k in range(0, len(rows), BATCH):
        batch = rows[k:k + BATCH]
        try:
            items, usage = _classify(client, batch)
        except Exception as e:  # one bad batch must not lose the others; they stay pending
            errors.append(f"{type(e).__name__}: {e}")
            continue
        in_tok += usage.input_tokens
        out_tok += usage.output_tokens
        for i, a in enumerate(batch):
            it = items.get(i)
            if it is None:
                continue
            it.pop("i", None)
            it["severity"] = min(max(int(it["severity"]), 1), 5)
            con.execute("UPDATE articles SET extracted_json=? WHERE url=?", (json.dumps(it), a["url"]))
            done += 1
        con.commit()
    fetched = now_iso()
    if rows and done == 0 and errors:
        raise RuntimeError(f"all batches failed: {errors[0]}")
    write_obs(con, SOURCE, float(done), {"classified": done, "candidates": len(rows), "input_tokens": in_tok,
                                        "output_tokens": out_tok, "errors": errors[:3], "model": MODEL},
              ok=True, observed_at=fetched, fetched_at=fetched)
