"""High-impact USD economic calendar (ForexFactory weekly JSON export, free, no key).

For each event Overmind publishes:
- a conditional scenario (data above forecast -> bullish/bearish gold, and why);
- a lean (most likely direction of the price reaction in the 4h after the release)
  with an honest, capped confidence (52-65%): the sign of a surprise is close to a coin flip;
- after the release: the real price reaction (1h / 4h), the hit/miss of the lean,
  and a trigger to re-run the full analysis.

All text coming from the feed is validated (allowed characters, length) because it is
also passed to the AI prompt and shown on the site.
"""
from __future__ import annotations

import re
import time
from datetime import datetime, timezone

from .http import FetchError, request_json
from .sources import Cache, SourceStatus, now_iso

FEEDS = ("https://nfs.faireconomy.media/ff_calendar_thisweek.json",)
CACHE_MAX_AGE = 6 * 3600          # calendar times are known in advance: refresh every few hours
POST_EVENT_DELAY = 20 * 60        # re-run the analysis 20 min after a release
POST_EVENT_WINDOW = 12 * 3600     # ...if it happened within the last 12h
FLAT_PCT = 0.10                   # reactions smaller than this are "flat" (not evaluated)

_TITLE_OK = re.compile(r"^[A-Za-z0-9 .,'&/()+%:\-]{2,80}$")
_VALUE_OK = re.compile(r"^[-+]?[0-9.,]{0,10}[%KMBTkmbt]?$")   # e.g. 0.3%, -1.2B, 215K

# (pattern, effect on gold if actual > forecast, family, explanation)
RULES = [
    (r"CPI|PPI|PCE|Inflation", -1, "Inflazione",
     "inflazione più alta → Fed più restrittiva → rendimenti reali e dollaro su"),
    (r"Unemployment|Jobless|Claims", +1, "Lavoro",
     "più disoccupazione → economia più debole → Fed più accomodante"),
    (r"Non-Farm|NFP|ADP|Employment Change|Hourly Earnings|JOLTS", -1, "Lavoro",
     "mercato del lavoro forte → meno tagli dei tassi → dollaro su"),
    (r"Federal Funds Rate|Rate Decision", -1, "Fed",
     "tassi più alti delle attese → costo opportunità dell'oro più alto"),
    (r"FOMC|Fed |Speaks|Minutes|Press Conference|Statement|Testifies", 0, "Fed",
     "nessun dato numerico: toni restrittivi (hawkish) → ribassista, accomodanti (dovish) → rialzista"),
    (r"GDP|Retail Sales|ISM|PMI|Durable|Confidence|Sentiment|Housing|Home Sales|Industrial", -1, "Crescita",
     "economia più forte → dollaro e rendimenti su"),
]


def classify(title: str) -> dict:
    for pat, sign, family, why in RULES:
        if re.search(pat, title, re.I):
            return {"sign": sign, "family": family, "why": why}
    return {"sign": -1, "family": "Altro", "why": "dato più forte per l'USD → di solito negativo per l'oro"}


def _parse(raw) -> list[dict]:
    if not isinstance(raw, list):
        raise FetchError("formato calendario inatteso")
    out = []
    for e in raw:
        if not isinstance(e, dict) or e.get("country") != "USD" or e.get("impact") != "High":
            continue
        title = str(e.get("title", "")).strip()
        if not _TITLE_OK.match(title):
            continue
        try:
            t = datetime.fromisoformat(str(e.get("date", ""))).astimezone(timezone.utc).timestamp()
        except ValueError:
            continue
        fc, pv = str(e.get("forecast", "")).strip(), str(e.get("previous", "")).strip()
        out.append({"id": f"{int(t)}|{title}", "t": int(t), "title": title,
                    "forecast": fc if _VALUE_OK.match(fc) else "",
                    "previous": pv if _VALUE_OK.match(pv) else ""})
    return sorted(out, key=lambda x: x["t"])


def fetch_calendar(cache: Cache, force: bool = False) -> tuple[list | None, SourceStatus]:
    st = SourceStatus("calendar", "Calendario eventi (ForexFactory)")
    data, saved_at = cache.load("calendar")
    if data is not None and not force and saved_at:
        age = time.time() - datetime.fromisoformat(saved_at).timestamp()
        if age < CACHE_MAX_AGE:
            st.last_success = saved_at
            return data, st
    try:
        events = []
        for url in FEEDS:
            events += _parse(request_json(url))
        # keep events of previous weeks already in cache (needed to evaluate reactions)
        merged = {e["id"]: e for e in (data or []) if e["t"] > time.time() - 14 * 86400}
        merged.update({e["id"]: e for e in events})
        data = sorted(merged.values(), key=lambda x: x["t"])
        cache.save("calendar", data)
        st.last_success = now_iso()
        return data, st
    except (FetchError, KeyError, TypeError, AttributeError, ValueError) as e:
        msg = str(e) if isinstance(e, FetchError) else "risposta inattesa"
        if data is not None:
            st.status, st.message, st.last_success = "degraded", f"Feed non disponibile ({msg}); uso il calendario salvato", saved_at
            return data, st
        st.status, st.message = "down", f"Feed non disponibile ({msg})"
        return None, st


def lean_for(score_d: float, score_h4: float) -> dict:
    s = 0.6 * score_d + 0.4 * score_h4
    if abs(s) < 0.15:
        return {"dir": None, "confidence": 50,
                "why": "trend non definito: nessuna inclinazione, la reazione dipenderà solo dalla sorpresa"}
    d = "bearish" if s < 0 else "bullish"
    conf = round(50 + 15 * min(1.0, abs(s)))
    trend = "ribassista" if s < 0 else "rialzista"
    return {"dir": d, "confidence": conf,
            "why": f"contesto {trend}: una sorpresa a favore del trend tende a estendere il movimento, "
                   f"una contraria spesso produce solo un rimbalzo temporaneo"}


def _price_at(h1: list[dict], t: float, after: bool) -> float | None:
    """Close of the last H1 candle ending <= t (after=False) or the first ending >= t (after=True)."""
    if after:
        for c in h1:
            if c["t"] + 3600 >= t:
                return c["c"]
        return None
    best = None
    for c in h1:
        if c["t"] + 3600 <= t:
            best = c["c"]
    return best


def reaction(ev: dict, h1: list[dict], now: float) -> dict | None:
    if not h1 or ev["t"] > now:
        return None
    p0 = _price_at(h1, ev["t"], after=False)
    if not p0:
        return None
    res = {"price_before": round(p0, 2)}
    last = h1[-1]["c"]
    if now < ev["t"] + 3600:
        res["so_far_pct"] = round((last / p0 - 1) * 100, 2)
    for key, delay in (("r1h_pct", 3600), ("r4h_pct", 4 * 3600)):
        if now >= ev["t"] + delay + 600:
            p = _price_at(h1, ev["t"] + delay, after=True)
            if p:
                res[key] = round((p / p0 - 1) * 100, 2)
    return res


def build_events(calendar: list | None, h1: list[dict], score_d: float, score_h4: float,
                 history: dict, now: float | None = None) -> tuple[dict, dict]:
    """Returns (events block for analysis.json, updated history)."""
    now = now or time.time()
    past = history.get("events") if isinstance(history, dict) else None
    hist = {e["id"]: e for e in (past if isinstance(past, list) else [])
            if isinstance(e, dict) and isinstance(e.get("id"), str) and isinstance(e.get("t"), int)}
    lean_now = lean_for(score_d, score_h4)
    week = []
    for ev in calendar or []:
        if ev["t"] < now - 7 * 86400:
            continue
        cls = classify(ev["title"])
        rec = hist.get(ev["id"])
        if rec is None and ev["t"] > now:
            item = dict(ev, **cls, lean=lean_now, status="upcoming")
        else:
            # lean is frozen at the last run before the release
            # first seen after the release: no honest pre-release lean exists -> never evaluated
            rec = rec or dict(ev, **cls, lean={"dir": None, "confidence": None,
                                               "why": "evento rilevato dopo l'uscita: nessuna previsione registrata"})
            rx = reaction(ev, h1, now)
            if rx:
                rec["reaction"] = rx
                r4 = rx.get("r4h_pct")
                ld = rec.get("lean", {}).get("dir")
                if r4 is not None and ld and rec.get("outcome") is None:
                    if abs(r4) < FLAT_PCT:
                        rec["outcome"] = "flat"
                    else:
                        rec["outcome"] = "hit" if (r4 < 0) == (ld == "bearish") else "miss"
            rec["status"] = "released" if ev["t"] <= now else "upcoming"
            item = rec
        if ev["t"] > now:
            hist.pop(ev["id"], None)
            hist[ev["id"]] = dict(item)     # keeps the latest pre-release lean
        else:
            hist[ev["id"]] = item
        week.append(item)

    # volatility: average absolute 1h reaction per event title
    for item in week:
        same = [abs(h["reaction"]["r1h_pct"]) for h in hist.values()
                if h.get("title") == item["title"] and isinstance(h.get("reaction"), dict)
                and h["reaction"].get("r1h_pct") is not None]
        item["avg_move_1h_pct"] = round(sum(same) / len(same), 2) if same else None
        item["samples"] = len(same)

    evaluated = [h for h in hist.values() if h.get("outcome") in ("hit", "miss")]
    stats = {"evaluated": len(evaluated),
             "accuracy": round(sum(h["outcome"] == "hit" for h in evaluated) / len(evaluated) * 100) if evaluated else None}
    recent = [w for w in week if w["status"] == "released" and now - w["t"] <= 24 * 3600 and w.get("reaction")]
    new_hist = {"events": sorted(hist.values(), key=lambda x: x["t"])[-500:], "stats": stats, "updated_at": now_iso()}
    return {"items": week, "stats": stats, "recent": recent}, new_hist


def pending_post_event(calendar: list | None, processed: set, now: float | None = None) -> list[dict]:
    """High-impact events released 20 min - 12 h ago that haven't triggered a re-analysis yet."""
    now = now or time.time()
    return [e for e in calendar or []
            if e["t"] + POST_EVENT_DELAY <= now <= e["t"] + POST_EVENT_WINDOW and e["id"] not in processed]


def event_vote(recent: list[dict]) -> tuple[int, str] | None:
    """Factor for the general forecast: the market's reaction to the latest high-impact release."""
    if not recent:
        return None
    ev = recent[-1]
    rx = ev["reaction"]
    r = rx.get("r1h_pct", rx.get("so_far_pct"))
    if r is None:
        return None
    v = 2 if r >= 0.8 else 1 if r >= 0.3 else -2 if r <= -0.8 else -1 if r <= -0.3 else 0
    label = "1h" if "r1h_pct" in rx else "finora"
    return v, f"Reazione a «{ev['title']}»: {r:+.2f}% ({label})"
