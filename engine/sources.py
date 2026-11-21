"""Data sources. Every fetch returns data + a SourceStatus that is shown on the site.

If an API is down, the last good copy from data/cache is reused (status "degraded")
so the analysis keeps working; with no cache the dependent factors are excluded
(status "down") and the confidence is reduced accordingly.
"""
from __future__ import annotations

import json
import math
import os
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone, timedelta
from pathlib import Path

from .http import FetchError, request_json, redact

TF_SECONDS = {"H1": 3600, "H4": 4 * 3600, "D": 86400, "W": 7 * 86400}


@dataclass
class SourceStatus:
    id: str
    label: str
    status: str = "ok"          # ok | degraded | down | disabled
    message: str = ""
    last_success: str | None = None
    detail: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["message"] = redact(d["message"])[:300]
        return d


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


# ---------------------------------------------------------------- cache
class Cache:
    def __init__(self, root: Path):
        self.root = root
        root.mkdir(parents=True, exist_ok=True)

    def _path(self, name: str) -> Path:
        safe = "".join(ch for ch in name if ch.isalnum() or ch in "_-")
        return self.root / f"{safe}.json"

    def save(self, name: str, data) -> None:
        p = self._path(name)
        tmp = p.with_suffix(".tmp")
        try:  # best effort: a cache write failure must never stop the analysis
            tmp.write_text(json.dumps({"saved_at": now_iso(), "data": data}), encoding="utf-8")
            tmp.replace(p)
        except OSError:
            pass

    def load(self, name: str):
        p = self._path(name)
        if not p.exists():
            return None, None
        try:
            obj = json.loads(p.read_text(encoding="utf-8"))
            return obj["data"], obj["saved_at"]
        except (ValueError, KeyError, OSError):
            return None, None


def with_cache(cache: Cache, name: str, status: SourceStatus, fetch):
    """Run fetch(); on failure fall back to cache. Returns data or None."""
    try:
        try:
            data = fetch()
        except (KeyError, IndexError, TypeError, AttributeError, ValueError):
            # Unexpected/hostile response shape: never crash, treat as an API failure.
            raise FetchError("risposta API in formato inatteso") from None
        cache.save(name, data)
        status.status = "ok"
        status.last_success = now_iso()
        return data
    except FetchError as e:
        data, saved_at = cache.load(name)
        if data is not None:
            status.status = "degraded"
            status.message = f"API non disponibile ({e}); uso i dati salvati il {saved_at[:16].replace('T', ' ')} UTC"
            status.last_success = saved_at
            return data
        status.status = "down"
        status.message = f"API non disponibile ({e}) e nessun dato salvato"
        return None


# ---------------------------------------------------------------- validation
def _num(x) -> float | None:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def clean_candles(rows: list[dict]) -> list[dict]:
    """Drop malformed/insane candles, sort, dedupe."""
    out = {}
    for r in rows:
        t, o, h, l, c = (r.get(k) for k in ("t", "o", "h", "l", "c"))
        if not all(isinstance(v, (int, float)) and math.isfinite(v) for v in (t, o, h, l, c)):
            continue
        if min(o, h, l, c) <= 0 or h < max(o, c) - 1e-9 or l > min(o, c) + 1e-9:
            continue
        if h / l > 1.5:  # >50% range in one candle on gold = bad data
            continue
        out[int(t)] = {"t": int(t), "o": o, "h": h, "l": l, "c": c}
    return [out[k] for k in sorted(out)]


def resample(candles: list[dict], seconds: int) -> list[dict]:
    buckets: dict[int, dict] = {}
    for c in candles:
        k = c["t"] - c["t"] % seconds
        b = buckets.get(k)
        if b is None:
            buckets[k] = {"t": k, "o": c["o"], "h": c["h"], "l": c["l"], "c": c["c"]}
        else:
            b["h"] = max(b["h"], c["h"]); b["l"] = min(b["l"], c["l"]); b["c"] = c["c"]
    return [buckets[k] for k in sorted(buckets)]


# ---------------------------------------------------------------- prices
def _twelvedata(interval: str, key: str) -> list[dict]:
    js = request_json("https://api.twelvedata.com/time_series", params={
        "symbol": "XAU/USD", "interval": interval, "outputsize": 600,
        "timezone": "UTC", "apikey": key,
    })
    if not isinstance(js, dict) or js.get("status") != "ok" or not isinstance(js.get("values"), list):
        raise FetchError("risposta Twelve Data non valida")
    rows = []
    for v in js["values"]:
        dt = str(v.get("datetime", ""))
        try:
            fmt = "%Y-%m-%d %H:%M:%S" if " " in dt else "%Y-%m-%d"
            t = datetime.strptime(dt, fmt).replace(tzinfo=timezone.utc).timestamp()
        except ValueError:
            continue
        rows.append({"t": t, "o": _num(v.get("open")), "h": _num(v.get("high")),
                     "l": _num(v.get("low")), "c": _num(v.get("close"))})
    return clean_candles(rows)


def _yahoo(interval: str, rng: str) -> list[dict]:
    js = request_json("https://query1.finance.yahoo.com/v8/finance/chart/GC=F",
                      params={"interval": interval, "range": rng})
    try:
        res = js["chart"]["result"][0]
        ts = res["timestamp"]
        q = res["indicators"]["quote"][0]
    except (KeyError, IndexError, TypeError):
        raise FetchError("risposta Yahoo non valida") from None
    rows = [{"t": t, "o": _num(o), "h": _num(h), "l": _num(l), "c": _num(c)}
            for t, o, h, l, c in zip(ts, q.get("open", []), q.get("high", []),
                                     q.get("low", []), q.get("close", []))]
    return merge_trailing_snapshot(clean_candles(rows), interval)


def merge_trailing_snapshot(c: list[dict], interval: str) -> list[dict]:
    """Yahoo appends a live 'snapshot' row (timestamp = now) to daily/weekly data.
    Fold it into the candle of the same period instead of showing a bogus extra candle."""
    period = {"1d": 86400, "1wk": 7 * 86400}.get(interval)
    if period and len(c) >= 2 and c[-1]["t"] - c[-2]["t"] < period:
        last, prev = c[-1], dict(c[-2])
        prev["h"] = max(prev["h"], last["h"]); prev["l"] = min(prev["l"], last["l"]); prev["c"] = last["c"]
        return c[:-2] + [prev]
    return c


def fetch_prices(cache: Cache) -> tuple[dict, SourceStatus]:
    key = os.environ.get("TWELVEDATA_API_KEY", "").strip()
    st = SourceStatus("prices", "Prezzi XAUUSD")

    def fetch():
        errors = []
        if key:
            try:
                data = {"H1": _twelvedata("1h", key), "H4": _twelvedata("4h", key),
                        "D": _twelvedata("1day", key), "W": _twelvedata("1week", key)}
                if all(len(v) >= 100 for v in data.values()):
                    return {"provider": "Twelve Data (spot XAU/USD)", "spot": True, "candles": data}
                errors.append("Twelve Data: storico insufficiente")
            except (FetchError, KeyError, IndexError, TypeError, AttributeError, ValueError) as e:
                errors.append(f"Twelve Data: {e if isinstance(e, FetchError) else 'risposta inattesa'}")
        try:
            h1 = _yahoo("60m", "1y")
            data = {"H1": h1[-600:], "H4": resample(h1, TF_SECONDS["H4"]), "D": _yahoo("1d", "5y"),
                    "W": _yahoo("1wk", "10y")}
        except FetchError as e:
            raise FetchError("; ".join(errors + [f"Yahoo: {e}"])) from None
        if not all(len(v) >= 100 for v in data.values()):
            raise FetchError("; ".join(errors + ["Yahoo: storico insufficiente"]))
        return {"provider": "Yahoo Finance (future GC=F)", "spot": False, "candles": data,
                "fallback_reason": "; ".join(errors) if errors else
                ("chiave Twelve Data non configurata" if not key else "")}

    data = with_cache(cache, "prices", st, fetch)
    if data:
        st.detail = {"provider": data["provider"]}
        if not data["spot"] and st.status == "ok":
            st.status = "degraded"
            st.message = ("Uso il future GC=F invece dello spot XAUUSD: i livelli possono differire "
                          "di qualche dollaro. " + (data.get("fallback_reason") or ""))
    return data, st


# ---------------------------------------------------------------- FRED macro
FRED_SERIES = {
    "DFII10": "Rendimento reale USA 10 anni (TIPS)",
    "DTWEXBGS": "Indice del dollaro (broad)",
    "DGS2": "Rendimento Treasury 2 anni (aspettative Fed)",
}


def fetch_fred(cache: Cache) -> tuple[dict, SourceStatus]:
    key = os.environ.get("FRED_API_KEY", "").strip()
    st = SourceStatus("fred", "Macro (FRED)")
    if not key:
        data, saved = cache.load("fred")
        st.status = "disabled" if data is None else "degraded"
        st.message = "Chiave FRED_API_KEY non configurata" + (f"; uso dati del {saved[:10]}" if data else "")
        return data, st

    def fetch():
        start = (datetime.now(timezone.utc) - timedelta(days=400)).strftime("%Y-%m-%d")
        out = {}
        for sid in FRED_SERIES:
            js = request_json("https://api.stlouisfed.org/fred/series/observations", params={
                "series_id": sid, "api_key": key, "file_type": "json", "observation_start": start})
            obs = js.get("observations") if isinstance(js, dict) else None
            if not isinstance(obs, list):
                raise FetchError(f"serie {sid} non valida")
            vals = [(o.get("date"), _num(o.get("value"))) for o in obs]
            vals = [(d, v) for d, v in vals if isinstance(d, str) and v is not None]
            if len(vals) < 70:
                raise FetchError(f"serie {sid}: dati insufficienti")
            out[sid] = vals
        return out

    return with_cache(cache, "fred", st, fetch), st


# ---------------------------------------------------------------- CFTC COT
def fetch_cot(cache: Cache) -> tuple[list | None, SourceStatus]:
    st = SourceStatus("cot", "Posizionamento fondi (CFTC COT)")

    def fetch():
        js = request_json("https://publicreporting.cftc.gov/resource/72hh-3qpy.json", params={
            "cftc_contract_market_code": "088691",
            "$select": "report_date_as_yyyy_mm_dd,open_interest_all,"
                       "m_money_positions_long_all,m_money_positions_short_all",
            "$order": "report_date_as_yyyy_mm_dd DESC",
            "$limit": 170,
        })
        if not isinstance(js, list):
            raise FetchError("risposta CFTC non valida")
        rows = []
        for r in js:
            if not isinstance(r, dict):
                continue
            d = str(r.get("report_date_as_yyyy_mm_dd", ""))[:10]
            oi, lo, sh = (_num(r.get(k)) for k in ("open_interest_all", "m_money_positions_long_all",
                                                   "m_money_positions_short_all"))
            if len(d) == 10 and oi and lo is not None and sh is not None:
                rows.append({"date": d, "oi": oi, "long": lo, "short": sh})
        if len(rows) < 30:
            raise FetchError("storico COT insufficiente")
        return sorted(rows, key=lambda r: r["date"])

    data = with_cache(cache, "cot", st, fetch)
    if data:
        st.detail = {"report_date": data[-1]["date"]}
    return data, st


def drop_open_candle(candles: list[dict], tf: str, now: float | None = None) -> list[dict]:
    """Analysis uses closed candles only (no flicker from a forming candle)."""
    now = now or time.time()
    if candles and candles[-1]["t"] + TF_SECONDS[tf] > now:
        return candles[:-1]
    return candles
