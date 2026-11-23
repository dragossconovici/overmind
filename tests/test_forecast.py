import random
import unittest
from datetime import datetime, timezone

from engine import forecast as fc

WEEK = 7 * 86400


def weekly(n=300, drift=0.002, seed=1, start=1_500_000_000):
    rnd = random.Random(seed)
    out, p, t = [], 2000.0, fc.week_start(start) + 5 * 3600
    for _ in range(n):
        o = p
        p = p * (1 + drift + rnd.gauss(0, 0.02))
        out.append({"t": t, "o": o, "h": max(o, p) * 1.01, "l": min(o, p) * 0.99, "c": p})
        t += WEEK
    return out


def tfs(sw, sd):
    return {"W": {"score": sw, "factors": [{"label": "Dollaro", "vote": -2, "weight": 2, "why": "x"}],
                  "analysis": {"zones_above": [{"low": 99999, "high": 99999.5, "type": "FVG", "dir": "bear"}],
                               "zones_below": []}},
            "D": {"score": sd, "factors": [], "analysis": {"zones_above": [], "zones_below": []}}}


class ForecastTest(unittest.TestCase):
    def test_target_week_is_next_monday(self):
        sat = datetime(2026, 11, 21, 12, tzinfo=timezone.utc).timestamp()
        wed = datetime(2026, 11, 18, 12, tzinfo=timezone.utc).timestamp()
        mon = datetime(2026, 11, 23, tzinfo=timezone.utc).timestamp()
        prev_mon = datetime(2026, 11, 16, tzinfo=timezone.utc).timestamp()
        self.assertEqual(fc.target_week(sat)[0], mon)        # weekend -> the week ahead
        self.assertEqual(fc.target_week(wed)[0], prev_mon)   # mid-week -> the current week
        self.assertEqual(fc.target_week(mon)[0], mon)        # Monday 00:00 -> this week
        monday_morning = datetime(2026, 11, 23, 9, 10, tzinfo=timezone.utc).timestamp()
        self.assertEqual(fc.target_week(monday_morning)[0], mon)
        before_close = datetime(2026, 11, 27, 20, 59, tzinfo=timezone.utc).timestamp()
        at_close = datetime(2026, 11, 27, 21, 0, tzinfo=timezone.utc).timestamp()
        self.assertEqual(fc.target_week(before_close)[0], mon)
        self.assertEqual(fc.target_week(at_close)[0], mon + fc.WEEK)
        self.assertEqual(fc.week_label(int(mon)), "23–27 nov 2026")

    def test_probability_capped_and_direction(self):
        w = weekly()
        now = w[-1]["t"] + 3 * 86400
        bear = fc.build(w, w[:-1], tfs(-1, -1), None, w[-1]["c"], now)
        bull = fc.build(w, w[:-1], tfs(1, 1), None, w[-1]["c"], now)
        for f in (bear, bull):
            self.assertTrue(30 <= f["p_up"] <= 70)
        self.assertLess(bear["p_up"], bull["p_up"])
        self.assertEqual(bull["direction"], "bullish")
        lo, hi = bull["volatility"]["range68"]
        self.assertTrue(lo < w[-1]["c"] < hi)
        lo95, hi95 = bull["volatility"]["range95"]
        self.assertTrue(lo95 < lo and hi95 > hi)
        # unreachable resistance is not used as target
        self.assertNotIn("99.999", bull["scenarios"][0]["text"])

    def test_key_event_widens_range(self):
        w = weekly()
        now = w[-1]["t"] + 3 * 86400
        a = fc.build(w, w[:-1], tfs(0, 0), None, w[-1]["c"], now)
        b = fc.build(w, w[:-1], tfs(0, 0), [{"t": now + 3600, "title": "CPI m/m"}], w[-1]["c"], now)
        self.assertGreater(b["volatility"]["range68"][1], a["volatility"]["range68"][1])
        self.assertEqual(b["key_events"], ["CPI m/m"])

    def test_range_narrows_during_the_week(self):
        w = weekly()
        mon = fc.week_start(w[-1]["t"])
        early = fc.build(w, w[:-1], tfs(0, 0), None, w[-1]["c"], mon + 3600)
        late = fc.build(w, w[:-1], tfs(0, 0), None, w[-1]["c"], mon + 4 * 86400)
        self.assertEqual(early["week_start"], late["week_start"])
        self.assertLess(late["volatility"]["range68"][1], early["volatility"]["range68"][1])

    def test_analogs_ignore_the_target_weeks_candle(self):
        w = weekly()
        now = w[-1]["t"] + 86400
        spiked = w[:-1] + [dict(w[-1], c=w[-1]["o"] * 1.5, h=w[-1]["o"] * 1.5)]
        a = fc.build(w, w[:-1], tfs(0, 0), None, w[-1]["c"], now)
        b = fc.build(spiked, w[:-1], tfs(0, 0), None, w[-1]["c"], now)
        self.assertEqual(a["components"]["analogs"], b["components"]["analogs"])

    def test_week_in_progress_does_not_touch_track_record(self):
        w = weekly(100)
        sat = w[-2]["t"] - 2 * 86400
        before = fc.build(w[:-2], w[:-2], tfs(1, 1), None, w[-3]["c"], sat)
        self.assertFalse(before["in_progress"])
        h = fc.update_history({}, before, w[:-2])
        during = fc.build(w[:-1], w[:-2], tfs(-1, -1), None, 1.0, w[-2]["t"] + 86400)
        self.assertTrue(during["in_progress"])
        h = fc.update_history(h, during, w[:-2])
        self.assertEqual(len(h["entries"]), 1)
        self.assertEqual(h["entries"][0]["ref_price"], before["ref_price"])

    def test_short_history_does_not_crash(self):
        w = weekly(30)
        f = fc.build(w, w[:-1], tfs(0.5, 0.5), None, w[-1]["c"], w[-1]["t"])
        self.assertIsNone(f["components"]["analogs"])

    def test_history_freezes_and_evaluates(self):
        w = weekly(100)
        now = w[-2]["t"] - 2 * 86400              # weekend before the week of w[-2]
        f = fc.build(w[:-2], w[:-3], tfs(1, 1), None, w[-3]["c"], now)
        h = fc.update_history({}, f, w[:-3])
        self.assertIsNone(h["entries"][0]["outcome"])
        h = fc.update_history(h, None, w[:-1])     # target week closed
        e = h["entries"][0]
        self.assertIn(e["outcome"], ("hit", "miss"))
        self.assertIsInstance(e["in_range68"], bool)
        frozen = dict(e)
        f2 = dict(f, ref_price=1.0)
        h = fc.update_history(h, f2, w[:-1])     # evaluated entries are never overwritten
        self.assertEqual(h["entries"][0]["ref_price"], frozen["ref_price"])
        self.assertEqual(h["stats"]["evaluated"], 1)

    def test_history_garbage(self):
        for junk in (None, [], "x", {"entries": "x"}, {"entries": [1, None, {"week_start": "a"}]}):
            h = fc.update_history(junk, None, weekly(50))
            self.assertEqual(h["entries"], [])


if __name__ == "__main__":
    unittest.main()
