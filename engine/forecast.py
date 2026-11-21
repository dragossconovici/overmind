"""Next-week forecast ("Previsioni").

Three independent, transparent ingredients:

1. Historical analogs (price only, ~10 years of weekly candles): weeks with the same
   set-up as now (trend vs 20-week average, 4-week momentum, last week's direction)
   -> how often the FOLLOWING week closed higher, and by how much. Shrunk towards the
   overall base rate when the sample is small.
2. Overmind's model (macro + COT + structure + momentum): the Weekly and Daily scores.
3. Volatility: weekly return dispersion of the last year, scaled to the time left until
   the target week closes and widened when market-moving USD data are scheduled.

P(up) = 50% analogs + 50% model, capped 30-70%: a week ahead, nobody honest is more sure.
Each forecast is frozen when the target week starts and checked when it closes, so the
site shows a real track record (direction hit rate and how often the close fell inside
the 68% range - a calibrated range should contain it ~2 times out of 3).
"""
from __future__ import annotations

import math
import re
import statistics
import time
from datetime import datetime, timezone

from .sources import now_iso

WEEK = 7 * 86400
DAY = 86400
P_CAP = (30, 70)
SHRINK = 20                 # pseudo-observations pulling analog stats to the base rate
DIR_THRESHOLD = 5           # |P(up) - 50| below this -> no direction ("incerta")
KEY_EVENTS = re.compile(r"CPI|PCE|Non-Farm|NFP|Federal Funds Rate|Rate Decision|FOMC Statement|Press Conference", re.I)
MONTHS_IT = ["gen", "feb", "mar", "apr", "mag", "giu", "lug", "ago", "set", "ott", "nov", "dic"]
HISTORY_MAX = 300


# ---------------------------------------------------------------- time
def week_start(ts: float) -> int:
    d = datetime.fromtimestamp(ts, timezone.utc)
    midnight = int(datetime(d.year, d.month, d.day, tzinfo=timezone.utc).timestamp())
    return midnight - d.weekday() * DAY


def target_week(now: float) -> tuple[int, int]:
    """Next trading week: Monday 00:00 UTC .. Friday 21:00 UTC (gold's weekly close)."""
    start = week_start(now) + WEEK
    return start, start + 4 * DAY + 21 * 3600


def week_label(start: int) -> str:
    a = datetime.fromtimestamp(start, timezone.utc)
    b = datetime.fromtimestamp(start + 4 * DAY, timezone.utc)
    if a.month == b.month:
        return f"{a.day}–{b.day} {MONTHS_IT[b.month - 1]} {b.year}"
    return f"{a.day} {MONTHS_IT[a.month - 1]} – {b.day} {MONTHS_IT[b.month - 1]} {b.year}"


# ---------------------------------------------------------------- analogs
def _state(closes: list[float], opens: list[float], i: int) -> tuple[int, int, int] | None:
    if i < 20:
        return None
    sma20 = sum(closes[i - 19:i + 1]) / 20
    return (int(closes[i] > sma20), int(closes[i] > closes[i - 4]), int(closes[i] > opens[i]))


STATE_TEXT = (("sotto la media a 20 settimane", "sopra la media a 20 settimane"),
              ("momentum a 4 settimane negativo", "momentum a 4 settimane positivo"),
              ("ultima settimana ribassista", "ultima settimana rialzista"))


def analogs(weekly: list[dict]) -> dict | None:
    """weekly: candles incl. the forming one (its close = current price)."""
    if len(weekly) < 60:
        return None
    closes = [c["c"] for c in weekly]
    opens = [c["o"] for c in weekly]
    now_state = _state(closes, opens, len(weekly) - 1)
    rets_all, rets_same = [], []
    for i in range(20, len(weekly) - 2):       # -2: the last candle may still be forming
        r = closes[i + 1] / closes[i] - 1
        rets_all.append(r)
        if _state(closes, opens, i) == now_state:
            rets_same.append(r)
    if not rets_all:
        return None
    base = sum(r > 0 for r in rets_all) / len(rets_all)
    n = len(rets_same)
    ups = sum(r > 0 for r in rets_same)
    p = (ups + SHRINK * base) / (n + SHRINK)
    return {
        "p_up": round(p * 100, 1),
        "raw_up_pct": round(ups / n * 100) if n else None,
        "n": n,
        "base_rate_pct": round(base * 100),
        "median_ret_pct": round(statistics.median(rets_same) * 100, 2) if n else None,
        "state": [STATE_TEXT[k][v] for k, v in enumerate(now_state)],
        "years": round(len(weekly) / 52, 1),
    }


# ---------------------------------------------------------------- volatility
def weekly_sigma(weekly_closed: list[dict], n: int = 52) -> float | None:
    c = [x["c"] for x in weekly_closed[-(n + 1):]]
    rets = [math.log(c[i + 1] / c[i]) for i in range(len(c) - 1)]
    return statistics.pstdev(rets) if len(rets) >= 20 else None


def typical_range(weekly_closed: list[dict], n: int = 26) -> float | None:
    w = weekly_closed[-n:]
    vals = [(x["h"] - x["l"]) / x["c"] for x in w if x["c"] > 0]
    return statistics.median(vals) if len(vals) >= 10 else None


# ---------------------------------------------------------------- forecast
def _zone_pub(z: dict, tf: str) -> dict:
    return {"low": z["low"], "high": z["high"], "type": z["type"], "dir": z["dir"], "tf": tf}


def _levels(tfs: dict, price: float) -> tuple[list, list]:
    above, below = [], []
    for tf in ("W", "D"):
        a = tfs.get(tf, {}).get("analysis") or {}
        above += [_zone_pub(z, tf) for z in a.get("zones_above", [])]
        below += [_zone_pub(z, tf) for z in a.get("zones_below", [])]
    above = sorted(above, key=lambda z: z["low"])[:3]
    below = sorted(below, key=lambda z: -z["high"])[:3]
    return above, below


def _drivers(tfs: dict) -> list[dict]:
    out = []
    for tf, mult in (("W", 1.0), ("D", 0.8)):
        for f in tfs.get(tf, {}).get("factors", []):
            if f.get("vote"):
                out.append({"tf": tf, "label": f["label"], "vote": f["vote"], "why": f["why"],
                            "impact": round(abs(f["vote"]) * f["weight"] * mult, 2)})
    out.sort(key=lambda d: -d["impact"])
    seen, top = set(), []
    for d in out:                      # one line per factor (the stronger timeframe wins)
        if d["label"] not in seen:
            seen.add(d["label"]); top.append(d)
    return top[:5]


def _fmt(p: float) -> str:
    return f"{p:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def build(weekly_all: list[dict], weekly_closed: list[dict], tfs: dict, calendar: list | None,
          price: float, now: float | None = None) -> dict | None:
    now = now or time.time()
    if not weekly_closed or not price:
        return None
    start, end = target_week(now)
    an = analogs(weekly_all)
    sw = tfs.get("W", {}).get("score", 0.0)
    sd = tfs.get("D", {}).get("score", 0.0)
    s = 0.6 * sw + 0.4 * sd
    p_model = 50 + 20 * s
    p_up = 0.5 * an["p_up"] + 0.5 * p_model if an else p_model
    p_up = round(max(P_CAP[0], min(P_CAP[1], p_up)))
    direction = "bullish" if p_up >= 50 + DIR_THRESHOLD else "bearish" if p_up <= 50 - DIR_THRESHOLD else "neutral"
    prob = p_up if direction == "bullish" else 100 - p_up if direction == "bearish" else max(p_up, 100 - p_up)

    events = [{"t": e["t"], "title": e["title"]} for e in (calendar or []) if now < e["t"] <= end]
    key = [e["title"] for e in events if KEY_EVENTS.search(e["title"])]
    boost = 1.15 if key else 1.0

    vol = None
    sig = weekly_sigma(weekly_closed)
    if sig:
        horizon = max(1.0, (end - now) / DAY * 5 / 7 / 5)        # in trading weeks
        sh = sig * math.sqrt(horizon) * boost
        vol = {"sigma_week_pct": round(sig * 100, 2), "horizon_weeks": round(horizon, 2),
               "event_boost": boost > 1,
               "range68": [round(price * math.exp(-sh), 2), round(price * math.exp(sh), 2)],
               "range95": [round(price * math.exp(-1.96 * sh), 2), round(price * math.exp(1.96 * sh), 2)]}
        tr = typical_range(weekly_closed)
        if tr:
            vol["typical_range_pct"] = round(tr * 100, 2)
            vol["typical_range_usd"] = round(tr * price, 2)

    above, below = _levels(tfs, price)
    scenarios = []
    if vol:
        lo68, hi68 = vol["range68"]
        if direction == "neutral":
            scenarios.append({"kind": "main", "dir": "neutral", "prob": prob,
                              "text": f"Settimana senza direzione netta: probabile oscillazione tra {_fmt(lo68)} e {_fmt(hi68)}."})
            if below:
                scenarios.append({"kind": "alt", "dir": "bearish", "prob": None,
                                  "text": f"Rottura sotto {_fmt(below[0]['low'])} ({below[0]['type']} {below[0]['tf']}): "
                                          f"spazio verso {_fmt(lo68)}."})
            if above:
                scenarios.append({"kind": "alt", "dir": "bullish", "prob": None,
                                  "text": f"Recupero sopra {_fmt(above[0]['high'])} ({above[0]['type']} {above[0]['tf']}): "
                                          f"spazio verso {_fmt(hi68)}."})
        else:
            bull = direction == "bullish"
            lo95, hi95 = vol["range95"]
            # target: nearest zone in the direction of the forecast, if reachable within the 95% range
            tgt_zone = [z for z in (above if bull else below)
                        if (z["low"] <= hi95 if bull else z["high"] >= lo95)][:1]
            inv_zone = (below if bull else above)[:1]
            tgt = (tgt_zone[0]["low"] if bull else tgt_zone[0]["high"]) if tgt_zone else (hi68 if bull else lo68)
            inv = (inv_zone[0]["high"] if bull else inv_zone[0]["low"]) if inv_zone else (lo68 if bull else hi68)
            word, opp = ("rialzo", "ribasso") if bull else ("ribasso", "rialzo")
            scenarios.append({"kind": "main", "dir": direction, "prob": prob,
                              "text": f"Prosecuzione al {word} verso {_fmt(tgt)}"
                                      + (f" ({tgt_zone[0]['type']} {tgt_zone[0]['tf']})" if tgt_zone else " (limite del range 68%)") + "."})
            scenarios.append({"kind": "alt", "dir": "bearish" if bull else "bullish", "prob": 100 - prob,
                              "text": f"Se il prezzo chiude {'sotto' if bull else 'sopra'} {_fmt(inv)} lo scenario cambia: "
                                      f"probabile {opp} verso {_fmt(lo68 if bull else hi68)}."})

    return {
        "week_start": start, "week_end": end, "label": week_label(start),
        "made_at": now_iso(), "ref_price": round(price, 2),
        "direction": direction, "p_up": p_up, "probability": prob,
        "components": {"analogs": an,
                       "model": {"p_up": round(p_model, 1), "score_w": sw, "score_d": sd}},
        "volatility": vol,
        "levels": {"resistance": above, "support": below},
        "scenarios": scenarios,
        "drivers": _drivers(tfs),
        "events": events[:12],
        "key_events": key[:6],
    }


# ---------------------------------------------------------------- track record
def update_history(history, forecast: dict | None, weekly_closed: list[dict]) -> dict:
    entries = history.get("entries") if isinstance(history, dict) else None
    entries = [e for e in (entries if isinstance(entries, list) else [])
               if isinstance(e, dict) and isinstance(e.get("week_start"), int)]
    by_week = {e["week_start"]: e for e in entries}
    if forecast:
        # Overwritten until the target week starts, then frozen (the forecast made over the weekend).
        prev = by_week.get(forecast["week_start"])
        if prev is None or prev.get("outcome") is None:
            by_week[forecast["week_start"]] = {
                "week_start": forecast["week_start"], "label": forecast["label"], "made_at": forecast["made_at"],
                "ref_price": forecast["ref_price"], "direction": forecast["direction"],
                "probability": forecast["probability"],
                "range68": (forecast.get("volatility") or {}).get("range68"), "outcome": None}
    for e in by_week.values():
        if e.get("outcome") is not None:
            continue
        wk = [c for c in weekly_closed if abs(c["t"] - e["week_start"]) <= 3 * DAY]
        if not wk:
            continue
        close = wk[-1]["c"]
        e["close"] = round(close, 2)
        ref = e.get("ref_price") or 0
        if e["direction"] == "neutral" or not ref:
            e["outcome"] = "skip"
        else:
            e["outcome"] = "hit" if (close > ref) == (e["direction"] == "bullish") else "miss"
        r = e.get("range68")
        if isinstance(r, list) and len(r) == 2:
            e["in_range68"] = r[0] <= close <= r[1]
    entries = sorted(by_week.values(), key=lambda e: e["week_start"])[-HISTORY_MAX:]
    ev = [e for e in entries if e.get("outcome") in ("hit", "miss")]
    rg = [e for e in entries if isinstance(e.get("in_range68"), bool)]
    stats = {"evaluated": len(ev),
             "accuracy": round(sum(e["outcome"] == "hit" for e in ev) / len(ev) * 100) if ev else None,
             "range_checked": len(rg),
             "in_range68_pct": round(sum(e["in_range68"] for e in rg) / len(rg) * 100) if rg else None}
    recent = [e for e in entries if e.get("outcome") is not None][-8:]
    return {"entries": entries, "stats": stats, "recent": recent, "updated_at": now_iso()}
