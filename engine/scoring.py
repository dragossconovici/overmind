"""Transparent scoring: each factor votes -2..+2, weights depend on the timeframe.

score      S = sum(w*v) / (2 * sum(w))                        in [-1, 1] -> direction
agreement  A = |sum(w*v)| / sum(w*|v|)                         how much factors agree
strength   F = sum(w*|v|) / (2 * sum(w))                       how strong the votes are
coverage   K = available weight / total weight                 penalty for missing data
confidence = 100 * K * (0.6*A + 0.4*F)

All factors aligned and strong -> ~100%; half bullish/half bearish -> ~40%;
all neutral -> 0%. Missing sources lower K, so confidence drops instead of lying.
"""
from __future__ import annotations

WEIGHTS = {
    "W":  {"real_yields": 3, "dollar": 2, "fed": 2, "cot": 2, "structure": 3, "momentum": 1},
    "D":  {"real_yields": 2, "dollar": 2, "fed": 2, "cot": 1.5, "structure": 3, "momentum": 1.5, "event": 1},
    "H4": {"real_yields": 1, "dollar": 1, "fed": 1, "cot": 0.5, "structure": 4, "momentum": 2, "event": 2},
}
# Factors that exist only sometimes: absent = not counted at all (no coverage penalty).
OPTIONAL = {"event"}
# macro change lookback in daily observations, per timeframe
MACRO_LOOKBACK = {"W": 60, "D": 20, "H4": 5}
BIAS_THRESHOLD = 0.15
LOW_CONFIDENCE = 40


def _clamp(v: float) -> int:
    return int(max(-2, min(2, round(v))))


def _change(series: list, lookback: int):
    if not series or len(series) <= lookback:
        return None
    return series[-1][1] - series[-1 - lookback][1], series[-1][1]


def _vote_rate(series, lookback, strong, weak, label, unit="pp"):
    """Rising yields = bearish gold."""
    ch = _change(series, lookback)
    if ch is None:
        return None
    d, last = ch
    v = -2 if d >= strong else -1 if d >= weak else 2 if d <= -strong else 1 if d <= -weak else 0
    verb = "in salita" if d > 0 else "in discesa" if d < 0 else "stabile"
    return v, f"{label} {verb}: {last:.2f}% ({d:+.2f} {unit} in {lookback} giorni)"


def macro_votes(fred: dict | None, tf: str) -> dict:
    out = {}
    if not fred:
        return out
    lb = MACRO_LOOKBACK[tf]
    scale = {"W": 1.6, "D": 1.0, "H4": 0.5}[tf]
    r = _vote_rate(fred.get("DFII10"), lb, 0.25 * scale, 0.08 * scale, "Rendimento reale 10Y")
    if r: out["real_yields"] = r
    r = _vote_rate(fred.get("DGS2"), lb, 0.25 * scale, 0.08 * scale, "Treasury 2Y (aspettative Fed)")
    if r: out["fed"] = r
    s = fred.get("DTWEXBGS")
    if s and len(s) > lb:
        pct = (s[-1][1] / s[-1 - lb][1] - 1) * 100
        strong, weak = 1.5 * scale, 0.5 * scale
        v = -2 if pct >= strong else -1 if pct >= weak else 2 if pct <= -strong else 1 if pct <= -weak else 0
        out["dollar"] = (v, f"Dollaro {'più forte' if pct > 0 else 'più debole'}: {pct:+.2f}% in {lb} giorni")
    return out


def cot_vote(cot: list | None) -> tuple[int, str, dict] | None:
    if not cot or len(cot) < 30:
        return None
    nets = [(r["long"] - r["short"]) / r["oi"] * 100 for r in cot]
    last = nets[-1]
    hist = nets[-156:]
    pctile = sum(1 for x in hist if x <= last) / len(hist) * 100
    change4 = last - nets[-5] if len(nets) >= 5 else 0.0
    trend = 1 if change4 > 2 else -1 if change4 < -2 else 0
    crowd = -1 if pctile >= 90 else 1 if pctile <= 10 else 0
    v = _clamp(trend + crowd)
    parts = [f"Fondi (managed money) netti long {last:.1f}% dell'open interest",
             f"{change4:+.1f} pt in 4 settimane",
             f"percentile {pctile:.0f} su 3 anni"]
    if crowd == -1:
        parts.append("posizionamento affollato: rischio prese di profitto")
    elif crowd == 1:
        parts.append("fondi molto scarichi: spazio per ricoperture")
    info = {"net_pct_oi": round(last, 2), "change_4w": round(change4, 2), "percentile_3y": round(pctile),
            "report_date": cot[-1]["date"]}
    return v, ", ".join(parts), info


def structure_vote(tfa: dict) -> tuple[int, str]:
    t, ev = tfa["trend"], tfa["last_event"]
    if not t or not ev:
        return 0, "Struttura non definita"
    if ev["type"] == "BOS":
        v = 2 if t == "up" else -2
        txt = f"Trend {'rialzista' if t == 'up' else 'ribassista'} confermato (BOS a {ev['level']:.2f})"
    else:
        v = 1 if t == "up" else -1
        txt = (f"Cambio di carattere {'rialzista' if t == 'up' else 'ribassista'} "
               f"(CHoCH a {ev['level']:.2f}): inversione da confermare")
    return v, f"{txt}, {ev['bars_ago']} candele fa"


def momentum_vote(tfa: dict) -> tuple[int, str]:
    p, e20, e50 = tfa["price"], tfa["ema20"], tfa["ema50"]
    if p > e20 > e50:
        return 1, f"Prezzo sopra EMA20 ({e20:.0f}) e EMA50 ({e50:.0f})"
    if p < e20 < e50:
        return -1, f"Prezzo sotto EMA20 ({e20:.0f}) e EMA50 ({e50:.0f})"
    return 0, f"Medie miste (EMA20 {e20:.0f}, EMA50 {e50:.0f})"


FACTOR_LABELS = {
    "real_yields": "Rendimenti reali", "dollar": "Dollaro", "fed": "Aspettative Fed",
    "cot": "Smart money (COT)", "structure": "Struttura di mercato", "momentum": "Momentum",
    "event": "Notizia recente",
}


def score_tf(tf: str, tfa: dict, fred: dict | None, cot_res, event_res=None) -> dict:
    weights = WEIGHTS[tf]
    votes = {}
    votes.update(macro_votes(fred, tf))
    if cot_res:
        votes["cot"] = (cot_res[0], cot_res[1])
    votes["structure"] = structure_vote(tfa)
    votes["momentum"] = momentum_vote(tfa)
    if event_res:
        votes["event"] = event_res

    factors, swv, sw, swabs = [], 0.0, 0.0, 0.0
    total_w = sum(w for k, w in weights.items() if k not in OPTIONAL or k in votes)
    for key, w in weights.items():
        if key in OPTIONAL and key not in votes:
            continue
        if key in votes:
            v, why = votes[key]
            swv += w * v; sw += w; swabs += w * abs(v)
            factors.append({"id": key, "label": FACTOR_LABELS[key], "vote": v, "weight": w, "why": why})
        else:
            factors.append({"id": key, "label": FACTOR_LABELS[key], "vote": None, "weight": w,
                            "why": "Dato non disponibile: fattore escluso"})
    S = swv / (2 * sw) if sw else 0.0
    A = abs(swv) / swabs if swabs else 0.0
    F = swabs / (2 * sw) if sw else 0.0
    K = sw / total_w
    conf = round(100 * K * (0.6 * A + 0.4 * F))
    bias = "bullish" if S >= BIAS_THRESHOLD else "bearish" if S <= -BIAS_THRESHOLD else "neutral"
    return {
        "bias": bias, "score": round(S, 3), "confidence": conf,
        "conflict": conf < LOW_CONFIDENCE,
        "components": {"agreement": round(A, 3), "strength": round(F, 3), "coverage": round(K, 3)},
        "factors": factors,
    }


BIAS_IT = {"bullish": "rialzista", "bearish": "ribassista", "neutral": "neutrale"}


def context_label(tf: str, bias: str, higher_bias: str | None, higher_name: str | None) -> str:
    b = BIAS_IT[bias]
    if tf == "W":
        return "Timeframe di riferimento"
    if not higher_bias or bias == "neutral" or higher_bias == "neutral":
        return b.capitalize()
    if bias != higher_bias:
        return f"{b.capitalize()} — in correzione rispetto al {higher_name}"
    return f"{b.capitalize()} — allineato al {higher_name}"


def zone_of_interest(bias: str, tfa: dict) -> dict | None:
    """Bullish -> nearest demand below (pullback buy area); bearish -> nearest supply above."""
    if bias == "bullish":
        cands = [z for z in tfa["zones_below"] if z["dir"] == "bull"]
    elif bias == "bearish":
        cands = [z for z in tfa["zones_above"] if z["dir"] == "bear"]
    else:
        return None
    return cands[0] if cands else None
