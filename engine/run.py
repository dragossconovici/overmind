"""Entry point: python -m engine.run

Fetches data, analyses W/D/H4, asks Gemini for the explanation, evaluates past
forecasts and writes site/data/analysis.json + site/data/history.json.
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

from . import calendar as cal
from . import forecast as fc
from . import scoring
from .explain import explain
from .http import register_secret, redact
from .sources import (TF_SECONDS, Cache, SourceStatus, drop_open_candle, fetch_cot, fetch_fred,
                      fetch_prices, now_iso)
from .structure import analyze_tf

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "site" / "data"
CACHE_DIR = ROOT / "data" / "cache"
TFS = ("W", "D", "H4")
HIGHER = {"W": (None, None), "D": ("W", "Weekly"), "H4": ("D", "Daily")}
HORIZON = {"H4": 6, "D": 5, "W": 4}       # candles after which a forecast is evaluated
HISTORY_MAX = 3000
CHART_CANDLES = 300
SCHEDULED_EVERY = 4 * 3600 - 10 * 60     # full analysis at least every ~4h


def load_dotenv(path: Path) -> None:
    """Minimal .env loader for local runs (the file is git-ignored)."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k, v = k.strip(), v.strip().strip('"').strip("'")
        if k in ("FRED_API_KEY", "GEMINI_API_KEY", "GEMINI_MODEL", "TWELVEDATA_API_KEY") and k not in os.environ:
            os.environ[k] = v


def read_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def write_json(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    tmp.replace(path)


# ---------------------------------------------------------------- track record
def update_history(history: dict, results: dict, closed: dict) -> dict:
    entries = history.get("entries", [])
    known = {(e["tf"], e["candle_t"]) for e in entries}
    for tf in TFS:
        if tf not in results or not closed.get(tf):
            continue
        last = closed[tf][-1]
        if (tf, last["t"]) in known:
            continue
        r = results[tf]
        entries.append({"tf": tf, "candle_t": last["t"], "made_at": now_iso(), "bias": r["bias"],
                        "confidence": r["confidence"], "price": round(last["c"], 2), "outcome": None})
    # evaluate
    for e in entries:
        if e["outcome"] is not None or e["tf"] not in closed:
            continue
        if e["bias"] == "neutral":
            e["outcome"] = "skip"
            continue
        target = e["candle_t"] + HORIZON[e["tf"]] * TF_SECONDS[e["tf"]]
        later = [c for c in closed[e["tf"]] if c["t"] >= target]
        if not later:
            continue
        after = later[0]["c"]
        sign = 1 if e["bias"] == "bullish" else -1
        e["price_after"] = round(after, 2)
        e["outcome"] = "hit" if (after - e["price"]) * sign > 0 else "miss"
    entries = entries[-HISTORY_MAX:]

    stats = {}
    for tf in TFS:
        ev = [e for e in entries if e["tf"] == tf and e["outcome"] in ("hit", "miss")]
        buckets = []
        for lo, hi in ((0, 40), (40, 60), (60, 80), (80, 101)):
            b = [e for e in ev if lo <= e["confidence"] < hi]
            buckets.append({"range": f"{lo}-{min(hi, 100)}%", "n": len(b),
                            "accuracy": round(sum(e["outcome"] == "hit" for e in b) / len(b) * 100) if b else None})
        stats[tf] = {"evaluated": len(ev),
                     "pending": sum(1 for e in entries if e["tf"] == tf and e["outcome"] is None),
                     "accuracy": round(sum(e["outcome"] == "hit" for e in ev) / len(ev) * 100) if ev else None,
                     "calibration": buckets, "horizon_candles": HORIZON[tf]}
    return {"entries": entries, "stats": stats, "updated_at": now_iso()}


# ---------------------------------------------------------------- main
def _age(iso: str | None) -> float:
    try:
        from datetime import datetime
        return time.time() - datetime.fromisoformat(iso).timestamp()
    except (TypeError, ValueError):
        return float("inf")


def run(auto: bool = False) -> int:
    load_dotenv(ROOT / ".env")
    for k in ("FRED_API_KEY", "GEMINI_API_KEY", "TWELVEDATA_API_KEY"):
        register_secret(os.environ.get(k))

    cache = Cache(CACHE_DIR)
    prev = read_json(DATA_DIR / "analysis.json", {})
    ev_hist = read_json(DATA_DIR / "events_history.json", {})
    if not isinstance(ev_hist, dict):
        ev_hist = {}
    processed = {p for p in ev_hist.get("processed", []) if isinstance(p, str)} \
        if isinstance(ev_hist.get("processed"), list) else set()
    calendar, st_cal = cal.fetch_calendar(cache)
    pending = cal.pending_post_event(calendar, processed)

    # --auto (every 15 min from GitHub Actions): only work when there is something to do.
    trigger = None
    if pending:
        trigger = "dopo la notizia: " + ", ".join(e["title"] for e in pending)
    elif _age(prev.get("generated_at")) >= SCHEDULED_EVERY or not auto:
        trigger = "aggiornamento programmato"
    if auto and not trigger:
        print("Nessuna azione: nessuna notizia appena uscita e analisi ancora recente.")
        return 0
    if pending:
        calendar, st_cal = cal.fetch_calendar(cache, force=True)

    prices, st_prices = fetch_prices(cache)
    fred, st_fred = fetch_fred(cache)
    cot, st_cot = fetch_cot(cache)
    statuses = [st_prices, st_fred, st_cot, st_cal]

    out = {"version": 1, "generated_at": now_iso(), "instrument": "XAUUSD", "trigger": trigger}

    if not prices:
        # Can't analyse without prices: republish the previous analysis, flagged as stale.
        st_ai = SourceStatus("gemini", "Spiegazione IA (Gemini)", "disabled", "Non eseguita: mancano i prezzi")
        statuses.append(st_ai)
        out.update({k: prev.get(k) for k in ("price", "provider", "timeframes", "macro", "cot", "ai", "chart", "events",
                                             "forecast", "forecast_track")})
        out["analysis_stale"] = True
        out["analysis_time"] = prev.get("analysis_time")
    else:
        candles = prices["candles"]
        closed = {tf: drop_open_candle(candles[tf], tf) for tf in TFS}
        cot_res = scoring.cot_vote(cot)
        tfs = {}
        for tf in TFS:
            tfa = analyze_tf(closed[tf], candles[tf])
            res = scoring.score_tf(tf, tfa, fred, cot_res)
            tfs[tf] = {"analysis": tfa, **res}

        # Calendar: the pre-release lean uses the scores WITHOUT the news factor; then the
        # market's reaction to the latest release becomes a factor of the D/H4 forecast.
        events, new_ev_hist = cal.build_events(calendar, candles.get("H1", []),
                                               tfs["D"]["score"], tfs["H4"]["score"], ev_hist)
        out["track_record_events"] = events["stats"]
        ev_vote = cal.event_vote(events["recent"])
        if ev_vote:
            for tf in ("D", "H4"):
                tfs[tf].update(scoring.score_tf(tf, tfs[tf]["analysis"], fred, cot_res, ev_vote))
        new_ev_hist["processed"] = sorted(processed | {e["id"] for e in pending})[-300:]
        write_json(DATA_DIR / "events_history.json", new_ev_hist)
        out["events"] = events
        if pending:
            out["updated_after_event"] = {"titles": [e["title"] for e in pending],
                                          "vote": ev_vote[1] if ev_vote else None}
        for tf in TFS:
            hk, hname = HIGHER[tf]
            tfs[tf]["context"] = scoring.context_label(tf, tfs[tf]["bias"], tfs[hk]["bias"] if hk else None, hname)
            tfs[tf]["zone_of_interest"] = scoring.zone_of_interest(tfs[tf]["bias"], tfs[tf]["analysis"])

        macro = {}
        if fred:
            for sid in ("DFII10", "DGS2", "DTWEXBGS"):
                s = fred.get(sid) or []
                if s:
                    macro[sid] = {"date": s[-1][0], "value": s[-1][1],
                                  "spark": [v for _, v in s[-120:]]}
        out.update({
            "price": round(candles["H4"][-1]["c"], 2),
            "provider": prices["provider"],
            "analysis_stale": False,
            "analysis_time": now_iso(),
            "timeframes": tfs,
            "macro": macro,
            "cot": cot_res[2] if cot_res else None,
            "chart": {tf: [[c["t"], round(c["o"], 2), round(c["h"], 2), round(c["l"], 2), round(c["c"], 2)]
                           for c in candles[tf][-CHART_CANDLES:]] for tf in TFS},
        })

        # Next-week forecast (frozen when the target week starts, checked when it closes).
        try:
            forecast = fc.build(candles["W"], closed["W"], tfs, calendar, out["price"])
        except (KeyError, TypeError, ValueError, ZeroDivisionError):
            forecast = None          # never let the forecast block the main analysis
        fc_hist = fc.update_history(read_json(DATA_DIR / "forecast_history.json", {}), forecast, closed["W"])
        write_json(DATA_DIR / "forecast_history.json", fc_hist)
        out["forecast"] = forecast
        out["forecast_track"] = {"stats": fc_hist["stats"], "recent": fc_hist["recent"]}

        payload = {
            "prezzo": out["price"], "fonte_prezzi": prices["provider"],
            "timeframes": {tf: {"bias": tfs[tf]["bias"], "contesto": tfs[tf]["context"],
                                "confidenza_pct": tfs[tf]["confidence"],
                                "fattori": [{k: f[k] for k in ("label", "vote", "weight", "why")} for f in tfs[tf]["factors"]],
                                "zona_interesse": tfs[tf]["zone_of_interest"],
                                "premium_discount": tfs[tf]["analysis"]["premium_discount"],
                                "zone_sopra": tfs[tf]["analysis"]["zones_above"],
                                "zone_sotto": tfs[tf]["analysis"]["zones_below"],
                                "rotture_recenti": tfs[tf]["analysis"]["breaks"]["recent"][:3],
                                "prossimi_livelli_rottura": tfs[tf]["analysis"]["breaks"]["watch"]} for tf in TFS},
            "fonti_non_disponibili": [s.label for s in statuses if s.status in ("down", "disabled")],
            "prossimi_eventi_alto_impatto": [
                {"evento": e["title"], "quando_utc": time.strftime("%a %d %H:%M", time.gmtime(e["t"])),
                 "previsione": e["forecast"], "precedente": e["previous"],
                 "se_sopra_attese": "rialzista" if e["sign"] > 0 else "ribassista" if e["sign"] < 0 else "dipende dai toni"}
                for e in events["items"] if e["status"] == "upcoming"][:6],
            "previsione_settimana_successiva": forecast and {
                "settimana": forecast["label"], "direzione": {"bullish": "rialzista", "bearish": "ribassista"}.get(forecast["direction"], "incerta (nessuna direzione netta)"),
                "probabilita_pct": forecast["probability"], "range_68pct": (forecast["volatility"] or {}).get("range68"),
                "analoghi_storici": forecast["components"]["analogs"], "scenari": forecast["scenarios"],
                "eventi_chiave": forecast["key_events"]},
            "reazioni_recenti": [{"evento": e["title"], "reazione": e["reaction"]} for e in events["recent"]],
        }
        ai, st_ai = explain(payload)
        statuses.append(st_ai)
        out["ai"] = ai

        history = update_history(read_json(DATA_DIR / "history.json", {}), tfs, closed)
        write_json(DATA_DIR / "history.json", history)
        out["track_record"] = history["stats"]

    # keep last known success time across runs
    prev_sources = {s.get("id"): s for s in prev.get("sources", []) if isinstance(s, dict)}
    for s in statuses:
        if not s.last_success and s.id in prev_sources:
            s.last_success = prev_sources[s.id].get("last_success")
    out["sources"] = [s.to_dict() for s in statuses]
    if "track_record" not in out:
        out["track_record"] = prev.get("track_record")
    levels = {s.status for s in statuses}
    out["health"] = ("down" if st_prices.status == "down" else
                     "degraded" if levels & {"down", "degraded", "disabled"} else "ok")

    write_json(DATA_DIR / "analysis.json", out)
    for s in statuses:
        print(redact(f"[{s.status:8}] {s.label}: {s.message or 'ok'}"))
    if out.get("timeframes"):
        for tf in TFS:
            t = out["timeframes"][tf]
            print(f"{tf:>2}: {t['context']} | confidenza {t['confidence']}%")
    return 0 if st_prices.status != "down" else 1


if __name__ == "__main__":
    sys.exit(run(auto="--auto" in sys.argv[1:]))
