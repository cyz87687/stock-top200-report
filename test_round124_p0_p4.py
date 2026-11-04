"""test_round124_p0_p4.py —— 把 round124 的 P0-4 实证结论固化为回归断言。

为什么要写这个：
    本轮对 P0-4 的核心发现是**三个反直觉事实**，它们都是「下次有人拍脑袋改权重
    就会立刻踩回去」的类型，因此必须写进测试而不是只写在报告里：

    F1  IC 与 Top10 超额**方向相反**
        score_lowvol20  IC +0.0828 / NW t 4.96（全维最强），但 TopN 全区间负超额。
        score_size_growth IC +0.0337 / t 2.27（全维最弱之一），Top10 却 +1.29%。
        ⇒ 「按 IC 排权重」在本项目生产口径（6~13 只）下是错的。

    F2  fund / dominant 的缺失**不是随机的**
        生产面板实测：score_fund 覆盖 10.78%，有值股市值中位 369 亿 vs 缺失 93 亿，
        KS p≈0（非随机）；score_dominant 覆盖 24.77%，KS p≈0。
        ⇒ 这两个维度让「有值的股票」和「没值的股票」由不同维度组合决定，
          跨股票 total 不可比。不是精度问题，是口径分裂。

    F3  tech_neg 的 0.10 额度**给谁都更好**
        把 0.10 给 growth / ep / quality / bias60 / lowvol20 / size_growth，
        N10 超额相对「留给 tech_neg」全部改善（+0.26 ~ +0.91pp）。
        ⇒ 问题不是「tech_neg 有害」，而是它占着额度却 IC 与 0 无法区分
          （−0.0069 / NW t −0.67）→ 这是机会成本，不是负 alpha。

数据源：全部复用 data/*.json 里已落盘的实测结果，不重算（测试要快）。
"""
import json
import os
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))


def _load(name):
    p = os.path.join(HERE, "data", name)
    if not os.path.exists(p):
        return None
    with open(p, encoding="utf-8") as f:
        return json.load(f)


class TestF1_ICInverseOfHeadExcess(unittest.TestCase):
    """F1：IC 排名与 Top10 超额排名方向相反。"""

    def setUp(self):
        self.d = _load("weight_scheme_head.json")
        self.c = _load("weight_scheme_curve.json")
        if self.d is None:
            self.skipTest("weight_scheme_head.json 未生成")

    def test_lowvol_high_ic_but_negative_head(self):
        r = self.d["results"]["ONLY_lowvol20"]
        # IC 是全维最强一档
        self.assertGreater(r["ic_mean"], 0.08, "lowvol20 的 IC 应 >0.08")
        self.assertGreater(r["ic_nw_t"], 4.5, "lowvol20 的 IC t 应 >4.5")
        # 但 Top10 为负
        self.assertLess(r["topN"]["top10"]["mean_pct"], 0,
                        "lowvol20 的 Top10 超额实测为负 —— 这是 F1 的核心证据")

    def test_size_growth_low_ic_but_positive_head(self):
        r = self.d["results"]["ONLY_size_growth"]
        self.assertLess(r["ic_mean"], 0.04, "size_growth 的 IC 应 <0.04")
        self.assertGreater(r["topN"]["top10"]["mean_pct"], 0,
                           "size_growth 的 Top10 超额实测为正 —— 与 IC 排名相反")
        # 且统计上不是纯噪声
        self.assertGreater(r["topN"]["top10"]["nw_t"], 1.9,
                           "该正超额 NW t 应 >1.9，否则不能作为反例")

    def test_rank_correlation_negative(self):
        """八维的 IC 排名 vs Top10 排名应为负相关。"""
        rows = []
        for name, r in self.d["results"].items():
            if not name.startswith("ONLY_"):
                continue
            rows.append((r["ic_mean"], r["topN"]["top10"]["mean_pct"]))
        if len(rows) < 4:
            self.skipTest("ONLY_* 方案不足 4 个")
        n = len(rows)
        # 用简单秩差（n 很小，无需 scipy）
        ic_order = {v[0]: i for i, v in enumerate(sorted(rows))}
        hd_order = {v[1]: i for i, v in enumerate(sorted(rows, key=lambda x: x[1]))}
        dsum = sum((ic_order[v[0]] - hd_order[v[1]]) ** 2 for v in rows)
        rho = 1 - 6 * dsum / (n * (n * n - 1))
        self.assertLess(rho, 0, f"IC 与 Top10 的秩相关应 <0，实测 {rho:.3f}")

    def test_curve_is_monotone_in_N_for_composite(self):
        """现行组合的头部为负、中段转正 —— 生产只持 6~13 只，正好在负区间。"""
        if self.c is None:
            self.skipTest("weight_scheme_curve.json 未生成")
        r = self.c["results"]["S0_v417_composite"]
        self.assertLess(r["N10"]["mean_pct"], 0, "现行 Top10 应为负超额")
        self.assertGreater(r["N100"]["mean_pct"], 0, "现行 Top100 应为正超额")


class TestF2_CoverageNotRandom(unittest.TestCase):
    """F2：fund / dominant 的缺失非随机。"""

    def setUp(self):
        self.d = _load("audit_p04_coverage_bias.json")
        if self.d is None:
            self.skipTest("audit_p04_coverage_bias.json 未生成")

    def test_fund_missing_not_random(self):
        m = self.d["missing_vs_mv"]["score_fund"]
        self.assertIsNotNone(m.get("ks_p"), "必须能算出 KS p 值")
        self.assertLess(m["ks_p"], 0.05,
                        "fund 缺失与市值应显著相关（p<0.05）→ 非随机")
        # 有值股应系统性更大
        self.assertGreater(m["mv_median_have"], m["mv_median_miss"] * 1.5,
                           "有 fund 值的股票市值中位应显著更高")

    def test_dominant_missing_not_random(self):
        m = self.d["missing_vs_mv"]["score_dominant"]
        self.assertLess(m["ks_p"], 0.05,
                        "dominant 缺失与市值应显著相关（p<0.05）→ 非随机")

    def test_coverage_rates_match_report(self):
        """覆盖率数字须与报告一致（防止静默漂移）。"""
        cov = self.d["dim_coverage"]
        self.assertAlmostEqual(cov["score_fund"]["coverage"], 0.1078, delta=0.02)
        self.assertAlmostEqual(cov["score_dominant"]["coverage"], 0.2477, delta=0.02)

    def test_full_coverage_dims(self):
        """quality / growth 应满覆盖 —— 它们是唯一「全池可比」的维度。"""
        cov = self.d["dim_coverage"]
        self.assertGreaterEqual(cov["score_quality"]["coverage"], 0.999)
        self.assertGreaterEqual(cov["score_growth"]["coverage"], 0.999)


class TestF3_TechnegOpportunityCost(unittest.TestCase):
    """F3：tech_neg 的 0.10 额度给谁都更好 —— 是机会成本不是负 alpha。"""

    def setUp(self):
        self.d = _load("weight_p04_attribution.json")
        if self.d is None:
            self.skipTest("weight_p04_attribution.json 未生成")

    def test_techneg_ic_indistinguishable_from_zero(self):
        """tech_neg 的 IC 与 0 无法区分 → 不能称「负向因子」。"""
        r = self.d["results"]["T0_keep_tech_neg"]
        # 该方案是组合，非单维；用 dim_ic 的单维结果更直接
        dim = _load("dim_ic_long.json")
        if dim is None:
            self.skipTest("dim_ic_long.json 未生成")
        t = dim["dim_ic"]["score_tech_neg"]["nw_t"]
        self.assertLess(abs(t), 2.0,
                        f"tech_neg 的 |NW t| 应 <2（实测 {t}）→ 与 0 无法区分")

    def test_most_redestinations_improve_head(self):
        """除 rsi_rev 外，其余去向的 N10 超额都应优于「留给 tech_neg」。"""
        rs = self.d["results"]
        base = rs["T0_keep_tech_neg"]["N10"]["mean_pct"]
        better = [n for n, r in rs.items()
                  if n != "T0_keep_tech_neg" and r["N10"]["mean_pct"] > base]
        self.assertGreaterEqual(len(better), 5,
                                f"应有 ≥5 个去向优于保留 tech_neg，实测 {len(better)} 个")

    def test_ep_is_best_n10_destination(self):
        rs = self.d["results"]
        best = max((n for n in rs if n != "T0_keep_tech_neg"),
                   key=lambda n: rs[n]["N10"]["mean_pct"])
        self.assertEqual(best, "T3_to_ep",
                         f"N10 最优去向实测为 T3_to_ep，实际 {best}")


class TestConstraintsRespected(unittest.TestCase):
    """所有落盘方案必须逐条满足 单因子≤0.20 / 大类≤0.30 / Σ=1。"""

    GROUPS = {
        "基本面": ["score_fund", "score_quality"],
        "估值": ["score_ep"],
        "低波": ["score_lowvol20"],
        "技术反转": ["score_bias60", "score_rsi_rev", "score_tech_neg"],
        "市值": ["score_size_growth", "score_dominant"],
        "业绩动量": ["score_growth"],
    }

    def _check(self, w, tag):
        self.assertAlmostEqual(sum(w.values()), 1.0, places=4,
                               msg=f"{tag}: Σ 应为 1.0")
        for d, v in w.items():
            self.assertLessEqual(v, 0.20 + 1e-9, f"{tag}: {d}={v} 超单因子上限 0.20")
            self.assertGreaterEqual(v, -1e-9, f"{tag}: {d}={v} 为负")
        for g, ks in self.GROUPS.items():
            t = sum(w.get(k, 0.0) for k in ks)
            self.assertLessEqual(t, 0.30 + 1e-9, f"{tag}: 大类[{g}]={t} 超上限 0.30")

    def test_constrained_schemes_all_compliant(self):
        d = _load("weight_p04_constrained.json")
        if d is None:
            self.skipTest("weight_p04_constrained.json 未生成")
        for name, r in d["results"].items():
            self._check(r["weights"], name)

    def test_violating_scheme_would_be_caught(self):
        """反证：把 size_growth 推到 0.50 的方案必须被约束判据抓住。"""
        bad = {"score_size_growth": 0.50, "score_quality": 0.10, "score_ep": 0.10,
               "score_growth": 0.10, "score_bias60": 0.10, "score_rsi_rev": 0.10}
        with self.assertRaises(AssertionError):
            self._check(bad, "BAD_size_growth_0.50")


class TestWeightsUntouched(unittest.TestCase):
    """本轮只测算、不改权重 —— factor_weights.json 指纹必须未变。"""

    def test_fingerprint_unchanged(self):
        import hashlib
        p = os.path.join(HERE, "factor_weights.json")
        h = hashlib.sha1(open(p, "rb").read()).hexdigest()
        self.assertEqual(h, "c98df667b5f9353060bedb3d35866edcc06dc1de",
                         "factor_weights.json 在本轮测算中不应被修改")

    def test_version_still_v417(self):
        with open(os.path.join(HERE, "factor_weights.json"), encoding="utf-8") as f:
            d = json.load(f)
        self.assertEqual(d["version"], "v4.17-cycle-rules")
        self.assertEqual(d["target_horizon"], 20)


if __name__ == "__main__":
    unittest.main(verbosity=2)
