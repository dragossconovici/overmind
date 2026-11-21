"""Market structure and Smart Money Concepts, defined with explicit rules.

Rules (so results are reproducible, not subjective):
- Swing high: a candle whose high is strictly higher than the N candles on each side
  (N = SWING_N). Swing low: mirror. A swing is only "known" N candles after it forms.
- BOS / CHoCH: a candle CLOSE beyond the last unbroken swing. Same direction as the
  current trend = BOS (continuation); opposite = CHoCH (change of character).
- Order block: the last opposite-colour candle before the impulse that produced a BOS/CHoCH.
  Valid until a candle closes through it.
- Fair Value Gap: 3-candle imbalance (low[i] > high[i-2] bullish, high[i] < low[i-2] bearish).
  Valid until price trades through the whole gap.
- Liquidity: unbroken swing highs (buy-side) / swing lows (sell-side), where stops cluster.
"""
from __future__ import annotations

SWING_N = 2


def ema(values: list[float], period: int) -> list[float]:
    k = 2 / (period + 1)
    out, prev = [], None
    for v in values:
        prev = v if prev is None else v * k + prev * (1 - k)
        out.append(prev)
    return out


def swings(c: list[dict], n: int = SWING_N) -> list[dict]:
    out = []
    for i in range(n, len(c) - n):
        win = c[i - n:i + n + 1]
        if all(c[i]["h"] > x["h"] for j, x in enumerate(win) if j != n):
            out.append({"i": i, "price": c[i]["h"], "kind": "H"})
        if all(c[i]["l"] < x["l"] for j, x in enumerate(win) if j != n):
            out.append({"i": i, "price": c[i]["l"], "kind": "L"})
    return out


def market_structure(c: list[dict], n: int = SWING_N) -> dict:
    sw = swings(c, n)
    trend, events = None, []
    last = {"H": None, "L": None}
    ptr = 0
    for j, candle in enumerate(c):
        while ptr < len(sw) and sw[ptr]["i"] + n <= j:
            s = dict(sw[ptr], broken=False)
            last[s["kind"]] = s
            ptr += 1
        h, l = last["H"], last["L"]
        if h and not h["broken"] and candle["c"] > h["price"]:
            events.append({"i": j, "dir": "up", "type": "BOS" if trend == "up" else "CHoCH",
                           "level": h["price"], "swing_i": h["i"]})
            h["broken"] = True
            trend = "up"
        elif l and not l["broken"] and candle["c"] < l["price"]:
            events.append({"i": j, "dir": "down", "type": "BOS" if trend == "down" else "CHoCH",
                           "level": l["price"], "swing_i": l["i"]})
            l["broken"] = True
            trend = "down"
    return {"trend": trend, "events": events, "swings": sw, "last_high": last["H"], "last_low": last["L"]}


def order_blocks(c: list[dict], events: list[dict], lookback: int = 15) -> list[dict]:
    obs = []
    for ev in events:
        j = ev["i"]
        for k in range(j - 1, max(j - lookback, 0) - 1, -1):
            bearish = c[k]["c"] < c[k]["o"]
            if (ev["dir"] == "up" and bearish) or (ev["dir"] == "down" and not bearish and c[k]["c"] > c[k]["o"]):
                obs.append({"i": k, "dir": "bull" if ev["dir"] == "up" else "bear",
                            "low": c[k]["l"], "high": c[k]["h"], "type": "Order block"})
                break
    valid = []
    for ob in obs:
        after = c[ob["i"] + 1:]
        if ob["dir"] == "bull" and any(x["c"] < ob["low"] for x in after):
            continue
        if ob["dir"] == "bear" and any(x["c"] > ob["high"] for x in after):
            continue
        valid.append(ob)
    # dedupe same candle
    seen, out = set(), []
    for ob in valid:
        if ob["i"] not in seen:
            seen.add(ob["i"]); out.append(ob)
    return out


def fair_value_gaps(c: list[dict]) -> list[dict]:
    out = []
    for i in range(2, len(c)):
        if c[i]["l"] > c[i - 2]["h"]:
            low, high = c[i - 2]["h"], c[i]["l"]
            if not any(x["l"] <= low for x in c[i + 1:]):
                out.append({"i": i, "dir": "bull", "low": low, "high": high, "type": "FVG"})
        elif c[i]["h"] < c[i - 2]["l"]:
            low, high = c[i]["h"], c[i - 2]["l"]
            if not any(x["h"] >= high for x in c[i + 1:]):
                out.append({"i": i, "dir": "bear", "low": low, "high": high, "type": "FVG"})
    return out


def liquidity(c: list[dict], sw: list[dict]) -> list[dict]:
    out = []
    for s in sw:
        after = c[s["i"] + 1:]
        if s["kind"] == "H" and not any(x["h"] > s["price"] for x in after):
            out.append({"i": s["i"], "dir": "bear", "low": s["price"], "high": s["price"],
                        "type": "Liquidità buy-side (massimo non violato)"})
        if s["kind"] == "L" and not any(x["l"] < s["price"] for x in after):
            out.append({"i": s["i"], "dir": "bull", "low": s["price"], "high": s["price"],
                        "type": "Liquidità sell-side (minimo non violato)"})
    return out


def analyze_tf(c: list[dict], c_all: list[dict] | None = None) -> dict:
    """Structure/BOS on CLOSED candles `c`; zone validity and current price on `c_all`
    (closed + forming candle), so a zone already traded through is not shown as valid."""
    c_all = c_all or c
    ms = market_structure(c)
    price = c_all[-1]["c"]
    closes = [x["c"] for x in c]
    e20, e50 = ema(closes, 20)[-1], ema(closes, 50)[-1]

    zones = order_blocks(c_all, ms["events"]) + fair_value_gaps(c_all) + liquidity(c_all, ms["swings"])
    for z in zones:
        z["time"] = c_all[z["i"]]["t"]
        z["mid"] = (z["low"] + z["high"]) / 2
        z["dist_pct"] = round((z["mid"] - price) / price * 100, 2)
    # keep zones within ±15% of price, nearest first, max 3 above + 3 below
    zones = [z for z in zones if abs(z["dist_pct"]) <= 15]
    above = sorted((z for z in zones if z["low"] > price), key=lambda z: z["low"])[:3]
    below = sorted((z for z in zones if z["high"] < price), key=lambda z: -z["high"])[:3]
    inside = [z for z in zones if z["low"] <= price <= z["high"]][:2]

    hi, lo = ms["last_high"], ms["last_low"]
    pd = None
    if hi and lo and hi["price"] > lo["price"]:
        pos = (price - lo["price"]) / (hi["price"] - lo["price"])
        label = ("sotto il range (rotto al ribasso)" if pos < 0 else
                 "sopra il range (rotto al rialzo)" if pos > 1 else
                 "premium" if pos > 0.5 else "discount")
        pd = {"range_low": lo["price"], "range_high": hi["price"], "position": round(pos, 3), "label": label}

    last_ev = ms["events"][-1] if ms["events"] else None
    return {
        "breaks": structure_breaks(c, ms, price),
        "price": price,
        "trend": ms["trend"],
        "last_event": last_ev and {"type": last_ev["type"], "dir": last_ev["dir"],
                                   "level": last_ev["level"], "time": c[last_ev["i"]]["t"],
                                   "bars_ago": len(c) - 1 - last_ev["i"]},
        "ema20": e20, "ema50": e50,
        "premium_discount": pd,
        "zones_above": [_pub(z) for z in above],
        "zones_below": [_pub(z) for z in below],
        "zones_inside": [_pub(z) for z in inside],
    }


MAX_BREAKS = 8


def structure_breaks(c: list[dict], ms: dict, price: float) -> dict:
    """Recent BOS/CHoCH (newest first) and the two levels whose break would come next.

    watch.up: the last unbroken swing high. A candle CLOSE above it is a bullish break:
    BOS if structure is already bullish, CHoCH (trend change) if it is bearish. watch.down mirrors it.
    """
    trend = ms["trend"]
    recent = []
    for ev in reversed(ms["events"][-MAX_BREAKS:]):
        recent.append({"type": ev["type"], "dir": ev["dir"], "level": round(ev["level"], 2),
                       "time": c[ev["i"]]["t"], "swing_time": c[ev["swing_i"]]["t"],
                       "bars_ago": len(c) - 1 - ev["i"]})

    def watch(s: dict | None, d: str) -> dict | None:
        if not s or s["broken"]:
            return None
        return {"level": round(s["price"], 2), "time": c[s["i"]]["t"],
                "type": "BOS" if trend == d else "CHoCH",
                "dist_pct": round((s["price"] - price) / price * 100, 2)}

    return {"trend": trend, "recent": recent,
            "watch": {"up": watch(ms["last_high"], "up"), "down": watch(ms["last_low"], "down")}}


def _pub(z: dict) -> dict:
    return {k: (round(v, 2) if isinstance(v, float) else v)
            for k, v in z.items() if k in ("dir", "low", "high", "type", "time", "dist_pct")}
