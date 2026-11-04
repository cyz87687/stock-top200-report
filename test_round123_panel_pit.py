#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""round123 P0-1 测试：财务 vs 行情的更新频率分野 + PIT 修复。

老大 2026-11-03 指令触发的核心命题：
「把一年只需要更新 4 次的财务数据抽出来不用回填，真正需要回填的是
  依赖每日行情更新的数据」

本测试锁定 5 类不变量：
  1. PIT 截断：法定披露截止日边界精确（年报/一季报→4/30、中报→8/31、三季报→10/31）
  2. 假 PIT 已修：不同 as_of 必须给出不同成长分（修复前恒等，差异 0%）
  3. 财务分段：238 个交易日按严格 PIT 只分 3 段（证明「不需逐日回填」）
  4. 分类互斥完备：每日/每季/不可重建 三类权重之和 == 1.0（禁重复计数）
  5. 回填产物：238 截面全过门槛、growth 覆盖率 100%

运行：python -m pytest test_round123_panel_pit.py -q
"""
import json
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import growth_factor_b as G


class TestPeriodDeadline(unittest.TestCase):
    """① 法定披露截止日：唯一口径，两处实现（growth_factor_b / fund_split）必须一致。"""

    def test_quarter_deadlines(self):
        cases = {
            "20250331": "2025-04-30",   # 一季报 → 4/30
            "20250630": "2025-08-31",   # 中报   → 8/31
            "20250930": "2025-10-31",   # 三季报 → 10/31
            # ⚠️ 年报跨年：2025 年报的法定截止日是 **2026**-04-30
            "20251231": "2026-04-30",
        }
        for p, want in cases.items():
            self.assertEqual(G._period_deadline(p), want, f"{p} 截止日错")

    def test_matches_fund_split(self):
        """与 fund_split.disclosure_date 同源 —— 两处口径分叉=静默漂移。"""
        try:
            import fund_split as FS
        except ImportError:
            self.skipTest("fund_split 不可用")
        for p in ["20250331", "20250630", "20250930", "20251231",
                  "20260331", "20260630"]:
            self.assertEqual(G._period_deadline(p), FS.disclosure_date(p),
                             f"{p} 两处 PIT 口径不一致")

    def test_bad_input_returns_none(self):
        for bad in ["", None, "abc", "2025", "20251331"]:
            self.assertIsNone(G._period_deadline(bad))


class TestPitFilter(unittest.TestCase):
    """② _pit_filter：截断必须精确到日，且 as_of=None 不截断。"""

    CUM = {"20250630": 1, "20250930": 2, "20251231": 3,
           "20260331": 4, "20260630": 5, "20260930": 6}

    def test_boundary_exact(self):
        # 4/29 年报尚未到期 → 仍用三季报
        self.assertEqual(max(G._pit_filter(self.CUM, "2026-04-29")), "20250930")
        # 4/30 当天 → 年报与一季报同时可用，取较新的一季报
        self.assertEqual(max(G._pit_filter(self.CUM, "2026-04-30")), "20260331")
        # 8/30 → 中报未到期
        self.assertEqual(max(G._pit_filter(self.CUM, "2026-08-30")), "20260331")
        # 8/31 → 中报可用
        self.assertEqual(max(G._pit_filter(self.CUM, "2026-08-31")), "20260630")
        # 10/31 → 三季报可用
        self.assertEqual(max(G._pit_filter(self.CUM, "2026-10-31")), "20260930")

    def test_none_means_no_filter(self):
        """as_of=None 必须返回原样 —— 否则生产当日路径会被静默截断。"""
        self.assertEqual(sorted(G._pit_filter(self.CUM, None)), sorted(self.CUM))

    def test_future_periods_dropped(self):
        """回测 2025-11 时绝不能看到 2026 年的报告期（前视）。"""
        f = G._pit_filter(self.CUM, "2025-11-03")
        self.assertNotIn("20260331", f)
        self.assertNotIn("20260630", f)
        self.assertNotIn("20260930", f)
        self.assertEqual(max(f), "20250930")

    def test_grace_only_loosens(self):
        """grace>0 只能让截断更宽松，绝不可能更严。"""
        strict = set(G._pit_filter(self.CUM, "2026-10-26", grace_days=0))
        loose = set(G._pit_filter(self.CUM, "2026-10-26", grace_days=5))
        self.assertTrue(strict.issubset(loose))
        # grace=5：10/26+5=10/31 → 三季报可用
        self.assertEqual(max(loose), "20260930")
        self.assertEqual(max(strict), "20260630")


class TestNoFakePit(unittest.TestCase):
    """③ 假 PIT 回归测试 —— 这是本轮修复的核心缺陷。

    修复前：as_of 只写进 __meta__，选期恒为 max(报告期)
    ⇒ 4 个跨年日期返回完全相同的 score_growth（实测差异 0/300 = 0.0%）
    """

    @classmethod
    def setUpClass(cls):
        p = os.path.join(HERE, "data", "panel_round123.json")
        if not os.path.exists(p):
            raise unittest.SkipTest("需先跑 backfill_panel_round123.py")
        with open(p, encoding="utf-8") as f:
            d = json.load(f)
        cls.dates = d["dates"]
        cls.rows = d["rows"]

    def test_growth_varies_across_quarters(self):
        """跨报告期的两个截面，growth 必须显著不同（修复前为 0%）。"""
        codes = [c for c in self.rows[self.dates[0]]
                 if c in self.rows[self.dates[-1]]]
        codes = [c for c in codes
                 if self.rows[self.dates[0]][c].get("score_growth") is not None
                 and self.rows[self.dates[-1]][c].get("score_growth") is not None]
        self.assertGreater(len(codes), 100, "样本太少无法判断")
        diff = sum(1 for c in codes
                   if abs(self.rows[self.dates[0]][c]["score_growth"]
                          - self.rows[self.dates[-1]][c]["score_growth"]) > 1e-9)
        ratio = diff / len(codes)
        # 修复前恒 0.0；修复后实测 0.999
        self.assertGreater(ratio, 0.5,
                           f"growth 跨期几乎不变（{ratio:.1%}）→ PIT 又失效了")

    def test_effective_period_advances(self):
        """生效报告期必须随时间前进，且落在合法集合内。"""
        legal = {"20250930", "20260331", "20260630", "20260930"}
        seen = []
        for d in self.dates:
            f = G._pit_filter({"20250930": 1, "20251231": 2, "20260331": 3,
                               "20260630": 4, "20260930": 5}, d)
            self.assertIn(max(f), legal, f"{d} 生效期非法: {max(f)}")
            if not seen or seen[-1] != max(f):
                seen.append(max(f))
        self.assertGreaterEqual(len(seen), 3,
                                f"生效报告期只有 {len(seen)} 段，疑似未按 PIT 切换")


class TestFinanceSegmentCount(unittest.TestCase):
    """④ 财务分段数 —— 直接支撑「财务不需逐日回填」的结论。"""

    def test_three_segments_in_238_days(self):
        p = os.path.join(HERE, "data", "panel_round123.json")
        if not os.path.exists(p):
            self.skipTest("需先跑 backfill_panel_round123.py")
        with open(p, encoding="utf-8") as f:
            meta = json.load(f)["meta"]
        segs = meta["finance_segments"]
        self.assertEqual(len(segs), 3, f"财务分段应为 3，实际 {len(segs)}")
        self.assertEqual([s["period"] for s in segs],
                         ["20250930", "20260331", "20260630"])
        # 段起点必须是法定截止日
        self.assertEqual(segs[1]["from"], "2026-04-30")
        self.assertEqual(segs[2]["from"], "2026-08-31")
        # 节省倍数
        self.assertGreater(meta["n_dates"] / len(segs), 50,
                           "财务分段节省倍数异常")


class TestClassificationComplete(unittest.TestCase):
    """⑤ 分类互斥完备 —— 权重不得重复计数。

    血案：首版 DAILY_DIMS 用 startswith("daily") 把不可重建的 score_fund
    也算了进去 → 「须回填」虚报 0.80（实际 0.60），虚报 4 倍。
    """

    def setUp(self):
        import backfill_panel_round123 as B
        self.B = B

    def test_categories_disjoint(self):
        a, b, c = (set(self.B.DAILY_DIMS), set(self.B.QUARTERLY_DIMS),
                   set(self.B.UNREBUILDABLE))
        self.assertEqual(a & b, set(), "每日 与 每季 重叠")
        self.assertEqual(a & c, set(), "每日 与 不可重建 重叠")
        self.assertEqual(b & c, set(), "每季 与 不可重建 重叠")

    def test_categories_cover_all_nine(self):
        """三类之并必须**覆盖** v4.17 九维；允许并集更大（留档维）。

        ⚠️ 断言方向不能写反。`DIM_FREQ` 里还含 score_lowvol20(权重0) 与
        score_size_ind(权重0) —— 它们仍要逐日回填（否则回测引用时会缺列），
        只是不进总分。故正确关系是 **九维 ⊂ 分类并集**，而非相等。
        首版写成 assertEqual 会误报「分类与九维不一致」，把正确设计判成缺陷。
        """
        allc = (set(self.B.DAILY_DIMS) | set(self.B.QUARTERLY_DIMS)
                | set(self.B.UNREBUILDABLE))
        nine = set(self.B.V417_WEIGHTS)
        missing = nine - allc
        self.assertEqual(missing, set(), f"九维中未分类：{missing}")
        # 多出来的必须权重为 0（留档维），否则就是漏了权重同步
        extra = allc - nine
        for d in extra:
            self.assertEqual(self.B.V417_WEIGHTS.get(d, 0.0), 0.0,
                             f"{d} 不在九维里却有非零权重 → 权重未同步")

    def test_weights_sum_to_one(self):
        """三类权重之和（只算九维内、权重>0 的）必须 == 1.0。"""
        W = self.B.V417_WEIGHTS
        tot = (sum(W.get(d, 0.0) for d in self.B.DAILY_DIMS)
               + sum(W.get(d, 0.0) for d in self.B.QUARTERLY_DIMS)
               + sum(W.get(d, 0.0) for d in self.B.UNREBUILDABLE))
        self.assertAlmostEqual(tot, 1.0, places=6,
                               msg="三类权重之和 != 1.0，必然有重复或遗漏")

    def test_fund_not_in_daily(self):
        """专项：score_fund 绝不能出现在「须回填」里 —— 它历史不可重建。"""
        self.assertNotIn("score_fund", self.B.DAILY_DIMS)
        self.assertIn("score_fund", self.B.UNREBUILDABLE)


class TestArtifact(unittest.TestCase):
    """⑥ 产物完整性。"""

    @classmethod
    def setUpClass(cls):
        p = os.path.join(HERE, "data", "panel_round123.json")
        if not os.path.exists(p):
            raise unittest.SkipTest("需先跑 backfill_panel_round123.py")
        with open(p, encoding="utf-8") as f:
            cls.d = json.load(f)
        cls.m = cls.d["meta"]

    def test_gate_passes_all(self):
        """238 截面必须 100% 过 200 门槛（原 factor_history 仅 16.5%）。"""
        self.assertEqual(self.m["n_dates_passing_gate"], self.m["n_dates"])
        for dt in self.d["dates"]:
            self.assertGreaterEqual(len(self.m and self.d["rows"][dt]), 200)

    def test_span_covers_one_year(self):
        from datetime import date
        a, b = self.m["date_range"]
        span = (date.fromisoformat(b) - date.fromisoformat(a)).days
        self.assertGreaterEqual(span, 330, f"跨度仅 {span} 天，不足一年")

    def test_growth_coverage_full(self):
        """成长维必须 100% 覆盖（源 panel_round66 无此列）。"""
        self.assertAlmostEqual(self.m["coverage"]["score_growth"]["pct"], 100.0,
                               places=1)

    def test_fund_low_coverage_disclosed(self):
        """fund 覆盖率低是事实，必须有记录，不能静默。"""
        c = self.m["coverage"]["score_fund"]
        self.assertLess(c["pct"], 20.0)
        self.assertIn("score_fund", " ".join(self.m["caveats"]))

    def test_caveats_present(self):
        cav = " ".join(self.m["caveats"])
        for kw in ("score_fund", "score_dominant", "circ_mv", "PIT"):
            self.assertIn(kw, cav, f"caveats 缺 {kw} 的披露")


if __name__ == "__main__":
    unittest.main(verbosity=2)
