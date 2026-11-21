import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from engine import explain, http, scoring, sources, structure


def candles_from(closes, spread=1.0, start=0, step=3600):
    out, prev = [], closes[0]
    for i, c in enumerate(closes):
        o = c - 0.1 if c >= prev else c + 0.1
        out.append({"t": start + i * step, "o": o, "h": c + spread, "l": c - spread, "c": c})
        prev = c
    return out


def zigzag(up=True, legs=12):
    """Higher highs/higher lows (up=True) or the mirror."""
    closes, p = [], 100.0
    for leg in range(legs):
        n = 6
        move = 10 if leg % 2 == 0 else -6
        if not up:
            move = -move
        for _ in range(n):
            p += move / n
            closes.append(p)
    return closes


class TestStructure(unittest.TestCase):
    def test_uptrend_detected(self):
        c = candles_from(zigzag(up=True))
        ms = structure.market_structure(c)
        self.assertEqual(ms["trend"], "up")
        self.assertEqual(ms["events"][-1]["type"], "BOS")

    def test_downtrend_detected(self):
        c = candles_from(zigzag(up=False))
        self.assertEqual(structure.market_structure(c)["trend"], "down")

    def test_choch_on_reversal(self):
        closes = zigzag(up=True) + [closes for closes in [190 - i * 3 for i in range(30)]]
        ms = structure.market_structure(candles_from(closes))
        self.assertEqual(ms["trend"], "down")
        self.assertIn("CHoCH", [e["type"] for e in ms["events"] if e["dir"] == "down"])

    def test_breaks_newest_first_and_watch_levels(self):
        c = candles_from(zigzag(up=True))
        ms = structure.market_structure(c)
        b = structure.structure_breaks(c, ms, c[-1]["c"])
        self.assertEqual(b["trend"], "up")
        self.assertLessEqual(len(b["recent"]), structure.MAX_BREAKS)
        times = [x["time"] for x in b["recent"]]
        self.assertEqual(times, sorted(times, reverse=True))
        for x in b["recent"]:
            self.assertLessEqual(x["swing_time"], x["time"])
        # in an uptrend a close below the last swing low would be a trend change
        if b["watch"]["down"]:
            self.assertEqual(b["watch"]["down"]["type"], "CHoCH")
            self.assertLess(b["watch"]["down"]["level"], c[-1]["c"])
        if b["watch"]["up"]:
            self.assertEqual(b["watch"]["up"]["type"], "BOS")

    def test_breaks_after_reversal(self):
        closes = zigzag(up=True) + [190 - i * 3 for i in range(30)]
        c = candles_from(closes)
        b = structure.analyze_tf(c)["breaks"]
        self.assertEqual(b["trend"], "down")
        self.assertEqual(b["recent"][0]["dir"], "down")
        self.assertIn("CHoCH", [x["type"] for x in b["recent"] if x["dir"] == "down"])

    def test_breaks_empty_on_flat(self):
        c = candles_from([100.0] * 20)
        b = structure.analyze_tf(c)["breaks"]
        self.assertEqual(b["recent"], [])
        self.assertIsNone(b["trend"])

    def test_fvg_filled_is_removed(self):
        c = [{"t": 0, "o": 100, "h": 101, "l": 99, "c": 100.5},
             {"t": 1, "o": 100.5, "h": 106, "l": 100.4, "c": 105.8},
             {"t": 2, "o": 105.8, "h": 108, "l": 104, "c": 107}]
        self.assertEqual(len(structure.fair_value_gaps(c)), 1)
        c.append({"t": 3, "o": 107, "h": 107, "l": 100.5, "c": 101})  # trades through the gap
        self.assertEqual(len(structure.fair_value_gaps(c)), 0)


class TestScoring(unittest.TestCase):
    def tfa(self, trend="up", ev="BOS", price=110, e20=105, e50=100):
        return {"trend": trend, "last_event": {"type": ev, "dir": trend, "level": 100.0, "bars_ago": 2},
                "price": price, "ema20": e20, "ema50": e50, "zones_above": [], "zones_below": []}

    def test_all_aligned_high_confidence(self):
        fred = {"DFII10": [("d", 2.0)] * 30 + [("d", 1.0)],      # real yields falling -> bullish
                "DGS2": [("d", 4.0)] * 30 + [("d", 3.0)],
                "DTWEXBGS": [("d", 120.0)] * 30 + [("d", 110.0)]}
        cot = (2, "x", {})
        r = scoring.score_tf("D", self.tfa(), fred, cot)
        self.assertEqual(r["bias"], "bullish")
        self.assertGreaterEqual(r["confidence"], 85)
        self.assertFalse(r["conflict"])

    def test_conflict_low_confidence(self):
        fred = {"DFII10": [("d", 1.0)] * 30 + [("d", 2.0)],      # bearish macro
                "DGS2": [("d", 3.0)] * 30 + [("d", 4.0)],
                "DTWEXBGS": [("d", 110.0)] * 30 + [("d", 120.0)]}
        r = scoring.score_tf("D", self.tfa(), fred, (2, "x", {}))  # bullish structure/cot/momentum
        self.assertLess(r["confidence"], 60)

    def test_missing_data_reduces_confidence(self):
        full = scoring.score_tf("D", self.tfa(), {"DFII10": [("d", 2.0)] * 30 + [("d", 1.0)],
                                                   "DGS2": [("d", 4.0)] * 30 + [("d", 3.0)],
                                                   "DTWEXBGS": [("d", 120.0)] * 30 + [("d", 110.0)]}, (2, "x", {}))
        partial = scoring.score_tf("D", self.tfa(), None, None)
        self.assertLess(partial["confidence"], full["confidence"])
        self.assertLess(partial["components"]["coverage"], 1)

    def test_confidence_bounds(self):
        r = scoring.score_tf("H4", self.tfa(trend=None, ev="BOS", price=100, e20=100, e50=100) | {"last_event": None}, None, None)
        self.assertTrue(0 <= r["confidence"] <= 100)
        self.assertEqual(r["bias"], "neutral")


class TestSecurity(unittest.TestCase):
    def test_redact_query_and_registered(self):
        http.register_secret("SUPERSECRET123")
        s = http.redact("https://x/?api_key=abcd1234&b=1 and SUPERSECRET123")
        self.assertNotIn("abcd1234", s)
        self.assertNotIn("SUPERSECRET123", s)

    def test_host_allowlist(self):
        for bad in ("http://api.stlouisfed.org/x", "https://evil.com/x", "https://api.stlouisfed.org.evil.com/",
                    "https://user:pw@api.stlouisfed.org/", "https://api.stlouisfed.org:8443/", "file:///etc/passwd",
                    "https://169.254.169.254/latest/meta-data/"):
            with self.assertRaises(http.FetchError, msg=bad):
                http._check_url(bad)
        http._check_url("https://api.stlouisfed.org/fred/series")

    def test_http_error_does_not_leak_url(self):
        import urllib.error
        err = urllib.error.HTTPError("https://api.stlouisfed.org/?api_key=LEAKME999", 400, "bad", {}, None)
        with mock.patch.object(http._OPENER, "open", side_effect=err):
            with self.assertRaises(http.FetchError) as cm:
                http.request_json("https://api.stlouisfed.org/x", params={"api_key": "LEAKME999"})
        self.assertNotIn("LEAKME999", str(cm.exception))
        self.assertIsNone(cm.exception.__cause__)

    def test_ai_output_validation(self):
        good = {"summary": "ok‮\x00", "W": "a", "D": "b", "H4": "c" * 5000, "risks": ["r", 3, ""]}
        v = explain.validate(good)
        self.assertEqual(v["summary"], "ok")
        self.assertLessEqual(len(v["H4"]), explain.MAX_LEN)
        self.assertEqual(v["risks"], ["r"])
        for bad in (None, [], {"summary": 1}, {"summary": "x", "W": "a", "D": "b", "H4": "c", "risks": "no"}):
            with self.assertRaises(ValueError):
                explain.validate(bad)

    def test_invalid_model_name_rejected(self):
        with mock.patch.dict(os.environ, {"GEMINI_API_KEY": "k" * 10, "GEMINI_MODEL": "../../evil?x="}):
            res, st = explain.explain({})
        self.assertIsNone(res)
        self.assertEqual(st.status, "down")

    def test_cache_name_path_traversal(self):
        with tempfile.TemporaryDirectory() as d:
            c = sources.Cache(Path(d))
            c.save("../../evil", {"a": 1})
            self.assertTrue((Path(d) / "evil.json").exists())

    def test_bad_candles_dropped(self):
        rows = [{"t": 1, "o": 100, "h": 102, "l": 99, "c": 101},
                {"t": 6, "o": 100, "h": 300, "l": 99, "c": 101},      # absurd spike
                {"t": 2, "o": float("nan"), "h": 2, "l": 1, "c": 1},
                {"t": 3, "o": 1, "h": 0.5, "l": 1, "c": 1},       # high < low
                {"t": 4, "o": -1, "h": 2, "l": -2, "c": 1},
                {"t": 5, "o": "x", "h": 2, "l": 1, "c": 1}]
        self.assertEqual([c["t"] for c in sources.clean_candles(rows)], [1])


class TestFallback(unittest.TestCase):
    def test_cache_used_when_api_down(self):
        with tempfile.TemporaryDirectory() as d:
            c = sources.Cache(Path(d))
            c.save("x", [1, 2])
            st = sources.SourceStatus("x", "X")

            def boom():
                raise http.FetchError("HTTP 503")
            self.assertEqual(sources.with_cache(c, "x", st, boom), [1, 2])
            self.assertEqual(st.status, "degraded")
            st2 = sources.SourceStatus("y", "Y")
            self.assertIsNone(sources.with_cache(c, "y", st2, boom))
            self.assertEqual(st2.status, "down")


if __name__ == "__main__":
    unittest.main()
