# -*- coding: utf-8 -*-
"""fwd_stale_guard 单测（2026-11-06 round131）。

覆盖：期间年化系数、东财缓存归母取数、陈旧判定四条件、锚定公式、
阶梯 int/str 年键、implied 反推兜底、行级留痕字段完整性。
"""
import json
import os
import shutil
import tempfile
import unittest

import fwd_stale_guard as fsg


def _write_cache(d, sym, nm_yuan, period):
    rec = {"选项": "常用指标", "指标": "归母净利润",
           "20251231": 1.0, period: nm_yuan}
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "%s.json" % sym), "w", encoding="utf-8") as f:
        json.dump({"sym": sym, "records": [rec]}, f)


class TestPeriodMath(unittest.TestCase):
    def test_months_and_factor(self):
        self.assertEqual(fsg.months_of_period("20260930"), 9)
        self.assertEqual(fsg.months_of_period("20260630"), 6)
        self.assertEqual(fsg.months_of_period("20260331"), 3)
        self.assertIsNone(fsg.months_of_period("20250930"))
        self.assertIsNone(fsg.months_of_period(None))
        self.assertAlmostEqual(fsg.annualize_factor("20260930"), 12.0 / 9)
        self.assertAlmostEqual(fsg.annualize_factor("20260630"), 2.0)
        self.assertIsNone(fsg.annualize_factor("20250930"))
        self.assertIsNone(fsg.annualize_factor("20261231"))


class TestEmExtract(unittest.TestCase):
    def test_real_cache_603186(self):
        got = fsg.extract_em_nm_ytd("603186")
        self.assertIsNotNone(got)
        self.assertEqual(got["period"], "20260930")
        self.assertAlmostEqual(got["ytd_yuan"] / 1e8, 7.9996, places=3)

    def test_missing_cache(self):
        self.assertIsNone(fsg.extract_em_nm_ytd("999999"))


class TestGuard(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run(self, ladder, cons, ytd_yuan, period, growth=50.0,
             mk=232.2538, code="sh603186", cons_map=None):
        _write_cache(self.tmp, code[2:], ytd_yuan, period)
        ladder = {code: dict(ladder)}
        fwd = {code: 36.2}
        meta = fsg.guard_pe_ladder(
            ladder, fwd, cons_map if cons_map is not None else {code: {2026: cons}},
            {code: {"growth": growth}},
            {code: mk}, cache_dir=self.tmp)
        return ladder[code], fwd.get(code), meta

    def test_stale_trigger_huazheng(self):
        lad, fwd, meta = self._run(
            {2026: 36.2, 2027: 24.3, 2028: 20.1},
            cons=6.42, ytd_yuan=7.9996e8, period="20260930")
        m = meta["sh603186"]
        self.assertTrue(m["stale"])
        self.assertAlmostEqual(m["anchor_yi"], 7.9996 * 4.0 / 3.0, places=3)
        # 阶梯 2026 位 = mktcap/anchor；2027/2028 原样
        self.assertAlmostEqual(lad[2026], round(232.2538 / (7.9996 * 4.0 / 3.0), 1))
        self.assertEqual(lad[2027], 24.3)
        self.assertEqual(lad[2028], 20.1)
        self.assertEqual(fwd, lad[2026])
        self.assertAlmostEqual(m["consensus_yi"], 6.42)
        self.assertEqual(m["period"], "20260930")
        self.assertIn("非'预期下修'", m["note"])

    def test_no_trigger_consensus_ge_ytd(self):
        lad, _, meta = self._run({2026: 36.2}, cons=9.0, ytd_yuan=8.0e8,
                                 period="20260930")
        self.assertEqual(meta, {})
        self.assertEqual(lad[2026], 36.2)

    def test_no_trigger_negative_growth(self):
        _, _, meta = self._run({2026: 36.2}, cons=6.42, ytd_yuan=8.0e8,
                               period="20260930", growth=-30.0)
        self.assertEqual(meta, {})

    def test_no_trigger_loss_ytd(self):
        _, _, meta = self._run({2026: 36.2}, cons=6.42, ytd_yuan=-8.0e8,
                               period="20260930")
        self.assertEqual(meta, {})

    def test_no_trigger_old_period(self):
        _, _, meta = self._run({2026: 36.2}, cons=6.42, ytd_yuan=8.0e8,
                               period="20250930")
        self.assertEqual(meta, {})

    def test_str_year_keys(self):
        lad, _, meta = self._run({"2026": 36.2, "2027": 24.3}, cons=6.42,
                                 ytd_yuan=8.0e8, period="20260930")
        self.assertIn("sh603186", meta)
        self.assertIn("2026", lad)
        self.assertEqual(lad["2027"], 24.3)

    def test_implied_consensus_fallback(self):
        lad, _, meta = self._run({2026: 36.2}, cons=None, ytd_yuan=8.0e8,
                                 period="20260930", cons_map={})
        m = meta["sh603186"]
        self.assertEqual(m["consensus_src"], "implied_from_ladder")
        self.assertAlmostEqual(m["consensus_yi"], 232.2538 / 36.2, places=3)

    def test_soft_tier_flag_only(self):
        # cons 7.5 落入 [ytd*0.9, ytd) = [7.1996, 7.9996) → 只留痕不改数
        lad, fwd, meta = self._run(
            {2026: 36.2, 2027: 24.3}, cons=7.5, ytd_yuan=7.9996e8,
            period="20260930")
        m = meta["sh603186"]
        self.assertFalse(m["stale"])
        self.assertTrue(m["stale_soft"])
        self.assertEqual(lad[2026], 36.2)
        self.assertEqual(fwd, 36.2)
        self.assertIn("未改数仅留痕", m["note"])

    def test_h1_factor(self):
        _, _, meta = self._run({2026: 36.2}, cons=2.0, ytd_yuan=3.0e8,
                               period="20260630")
        self.assertAlmostEqual(meta["sh603186"]["anchor_yi"], 6.0, places=6)


if __name__ == "__main__":
    unittest.main()
