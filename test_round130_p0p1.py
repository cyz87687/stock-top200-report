#!/usr/bin/env python3
"""test_round130_p0p1.py —— P0/P1 修复的回归锁（2026-11-05）。

覆盖：
  P0-1 统一内核 / 指纹守卫 / 四态判定 / 权重未变
  P0-2 最终权重硬验收 / 行情覆盖门禁
  P1-3 PIT 可用时点与前视断言 / 可执行模拟器自检
  P1-4 质量量纲标准化 / 预注册冻结
"""
import hashlib
import json
import os
import unittest

import numpy as np

import exec_backtest as eb
import pit_store as pit
import portfolio_risk as pr
import quality_normalize as qn
import research_preregistry as reg
import scoring_core as sc
import pick_portfolio as pp
from scale_calc import composite_total

HERE = os.path.dirname(os.path.abspath(__file__))
CAL = [f"2026-01-{i:02d}" for i in range(5, 17)]
CODES = ("000001", "000002")


class TestScoringKernelP01(unittest.TestCase):
    """P0-1：向量化内核必须与生产标量内核逐股等价。"""

    W = {"score_a": 0.5, "score_b": 0.3, "score_c": 0.2}
    DM = {"score_a": 10.0, "score_b": 5.0, "score_c": 10.0}
    DIMS = ["score_a", "score_b"]     # 故意不含 c → 模拟真盲区整列缺失

    def test_vectorized_matches_production_scalar(self):
        S = np.array([[10.0, 2.5], [5.0, np.nan], [0.0, 5.0],
                      [np.nan, np.nan], [3.3, 1.1]], dtype=np.float64)
        vec, meta = sc.composite_total_matrix(S, self.DIMS, self.W, self.DM)
        self.assertAlmostEqual(meta["denominator"], 1.0)
        self.assertAlmostEqual(meta["blind_weight"], 0.2)
        for i, row in enumerate(S):
            vals = [None if np.isnan(v) else float(v) for v in row]
            ref = sc.scalar_total(vals, self.DIMS, self.W, self.DM)
            self.assertLess(abs(float(vec[i]) - ref), 5.1e-3,
                            f"第 {i} 行两内核不一致: {vec[i]} vs {ref}")

    def test_fixed_denominator_does_not_change_with_missing_dims(self):
        """固定分母是 P0-1 的核心：缺维不得改变尺子（旧口径分母逐行变化）。"""
        _, m_full = sc.composite_total_matrix(
            np.array([[5.0, 2.5]]), self.DIMS, self.W, self.DM)
        _, m_missing = sc.composite_total_matrix(
            np.array([[5.0, np.nan]]), self.DIMS, self.W, self.DM)
        self.assertEqual(m_full["denominator"], m_missing["denominator"])
        self.assertEqual(m_full["blind_weight"], m_missing["blind_weight"])
        # 该维原值 2.5/DMAX5 归一后恰为中性 5.0 → 分数也不应跳变
        t_full = sc.composite_total_matrix(
            np.array([[5.0, 2.5]]), self.DIMS, self.W, self.DM)[0][0]
        t_missing = sc.composite_total_matrix(
            np.array([[5.0, np.nan]]), self.DIMS, self.W, self.DM)[0][0]
        self.assertAlmostEqual(float(t_full), float(t_missing), places=6)

    def test_selfcheck_real_data_contract(self):
        chk = sc.selfcheck(np.array([[5.0, 2.5], [np.nan, 1.0]]),
                           self.DIMS, self.W, self.DM, sample=2)
        self.assertTrue(chk["passed"])
        self.assertEqual(chk["n_checked"], 2)

    def test_kernel_fingerprint_stable_and_weights_frozen(self):
        a, b = sc.kernel_fingerprint(), sc.kernel_fingerprint()
        self.assertEqual(a["kernel_id"], b["kernel_id"])
        self.assertAlmostEqual(a["sum_active_weights"], 1.0)
        self.assertEqual(a["target_horizon"], 20)
        self.assertEqual(len(a["weights"]), 8, "本轮不得改权重或生效维数")

    def test_classify_artifact_four_states(self):
        self.assertEqual(sc.classify_artifact({})["status"], "UNKNOWN")
        self.assertEqual(sc.classify_artifact(
            {"kernel_id": "deadbeef", "factor_parity": True})["status"], "LEGACY")
        cur = sc.kernel_fingerprint()["kernel_id"]
        self.assertEqual(sc.classify_artifact(
            {"kernel_id": cur, "factor_parity": False})["status"], "PROXY_ONLY")
        self.assertTrue(sc.classify_artifact(
            {"kernel_id": cur, "factor_parity": True})["comparable"])
        # 非 COMPARABLE 必须明确禁止互证
        for meta in ({}, {"kernel_id": cur, "factor_parity": False}):
            self.assertFalse(sc.classify_artifact(meta)["comparable"])
            self.assertIn("禁止", sc.classify_artifact(meta)["verdict"])

    def test_production_scalar_unchanged(self):
        """生产 scale_calc 的四项自检仍成立（本轮未改生产量尺）。"""
        w = {"a": 0.5, "b": 0.5}
        dm = {"a": 10.0, "b": 10.0}
        t_all = composite_total([("a", 10.0, .5), ("b", 10.0, .5)], dm,
                                weights=w)[0]
        t_zero = composite_total([("a", 0.0, .5), ("b", 0.0, .5)], dm,
                                 weights=w)[0]
        t_neu = composite_total([("a", None, .5), ("b", None, .5)], dm,
                                weights=w)[0]
        self.assertEqual((t_all, t_zero, t_neu), (100.0, 0.0, 50.0))


class TestFinalWeightsP02(unittest.TestCase):
    """P0-2：最终权重硬验收 + 故障不扩大风险。"""

    def _pf(self, weights, w_total, cash, cap=None, scope="assets"):
        return {"weights": weights,
                "risk": {"w_total": w_total, "cash_weight": cash,
                         "single_weight_cap": cap,
                         "single_weight_cap_scope": scope,
                         "single_weight_max_of_assets": max(weights.values()),
                         "single_weight_max_of_equity": max(weights.values()) / w_total
                         if w_total else None}}

    def test_valid_weights_pass(self):
        r = pp.validate_final_weights(
            self._pf({"a": 0.3, "b": 0.3}, 0.6, 0.4, cap=0.4))
        self.assertTrue(r["ok"], r["violations"])

    def test_negative_weights_rejected(self):
        r = pp.validate_final_weights(
            self._pf({"a": -0.1, "b": 0.7}, 0.6, 0.4))
        self.assertFalse(r["ok"])
        self.assertTrue(any("负权重" in v for v in r["violations"]))

    def test_sum_not_one_rejected(self):
        r = pp.validate_final_weights(
            self._pf({"a": 0.3, "b": 0.3}, 0.6, 0.1))
        self.assertFalse(r["ok"])

    def test_shortfall_must_keep_cash_not_scale_up(self):
        """选不满时把预算全压到少数票 → 必须被拒。"""
        r = pp.validate_final_weights(
            self._pf({"a": 0.3, "b": 0.3}, 0.3, 0.4))
        self.assertFalse(r["ok"])
        self.assertTrue(any("突破风险预算" in v for v in r["violations"]))

    def test_single_cap_violation_rejected(self):
        r = pp.validate_final_weights(
            self._pf({"a": 0.1459, "b": 0.1459, "c": 0.1459, "d": 0.1459},
                     0.5836, 0.4164, cap=0.08))
        self.assertFalse(r["ok"])
        self.assertTrue(any("超上限" in v for v in r["violations"]))

    def test_coverage_gate_blocks_conclusions(self):
        """一半持仓无行情 → 覆盖率 0.5 < 0.8，不得输出波动/回撤结论。"""
        px = {CODES[0]: {d: [10.0, 10.0, 10.0, 10.0, 100] for d in CAL},
              CODES[1]: {CAL[0]: [10.0, 10.0, 10.0, 10.0, 100]}}
        pf = {"_date": CAL[-1],
              "picks": [{"code": c, "parent": "X"} for c in CODES],
              "weights": {CODES[0]: 0.5, CODES[1]: 0.5},
              "risk": {"w_total": 1.0, "cash_weight": 0.0}}
        out = pr.compute_one(pf, px, CAL)
        self.assertFalse(out["coverage_gate_ok"])
        self.assertLess(out["price_coverage_mean"], pr.MIN_PRICE_COVERAGE)
        self.assertIsNone(out["vol_ann"])
        self.assertIsNone(out["mdd"])
        self.assertFalse(out["computable"])
        self.assertIn("覆盖率", out["reason"])

    def test_full_coverage_gives_conclusions(self):
        px = {c: {d: [10.0, 10.0, 10.0, 10.0, 100] for d in CAL}
              for c in CODES}
        pf = {"_date": CAL[-1],
              "picks": [{"code": c, "parent": "X"} for c in CODES],
              "weights": {CODES[0]: 0.5, CODES[1]: 0.5},
              "risk": {"w_total": 1.0, "cash_weight": 0.0}}
        out = pr.compute_one(pf, px, CAL)
        self.assertTrue(out["coverage_gate_ok"])
        self.assertAlmostEqual(out["price_coverage_mean"], 1.0, places=3)
        self.assertIsNotNone(out["vol_ann"])


class TestPitAndExecutionP13(unittest.TestCase):

    def test_pit_record_provenance(self):
        actual = pit.make_pit_record("20250930", {"roe": 10},
                                     announcement_time="2025-10-28",
                                     revision_id="v1")
        approx = pit.make_pit_record("20251231", {"roe": 11})
        self.assertEqual(actual["provenance"], pit.PROVENANCE_ACTUAL)
        self.assertEqual(actual["available_time"], "2025-10-28")
        self.assertTrue(actual["revision_known"])
        self.assertEqual(approx["provenance"], pit.PROVENANCE_APPROX)
        self.assertEqual(approx["available_time"], "2026-04-30")
        self.assertFalse(approx["revision_known"])

    def test_asof_view_excludes_future_and_unknown_revisions(self):
        a = pit.make_pit_record("20250930", {"x": 1},
                                announcement_time="2025-10-28", revision_id="v1")
        b = pit.make_pit_record("20251231", {"x": 2})
        v = pit.asof_view([a, b], "2025-11-01")
        self.assertEqual(v["n_available"], 1)
        self.assertEqual(v["n_excluded_future"], 1)
        v2 = pit.asof_view([a, b], "2026-05-01", require_revision=True)
        self.assertEqual(v2["n_available"], 1, "修订号未知必须被严格模式排除")
        self.assertFalse(pit.asof_view([a, b], "2026-05-01")["strict_pit"])

    def test_no_lookahead_assertion(self):
        r = pit.make_pit_record("20251231", {"x": 1})
        self.assertTrue(pit.assert_no_lookahead(
            [pit.make_pit_record("20250930", {"x": 1},
                                 announcement_time="2025-10-28")], "2025-11-01"))
        with self.assertRaises(ValueError):
            pit.assert_no_lookahead([r], "2025-11-01")

    def test_exec_backtest_selfcheck(self):
        r = eb.selfcheck()
        self.assertTrue(r["passed"])
        self.assertEqual(r["n_rejected"], 1)
        self.assertGreater(r["cost_drag_pp"], 0)
        self.assertLess(r["net_pct"], r["gross_pct"])


class TestQualityAndPreregistryP14(unittest.TestCase):

    def test_rank_z_removes_scale_dominance(self):
        z = qn.standardize_column([1.0, 2.0, 3.0, 1000.0], "rank_z")
        self.assertTrue(all(abs(x[0]) <= qn.CLIP for x in z))
        self.assertLessEqual(z[-1][0], qn.CLIP)
        # 原始均值被极端值主导；标准化后应回到 0 附近
        self.assertLess(abs(sum(x[0] for x in z) / len(z)), 1e-6)

    def test_missing_is_neutral_not_zero_score(self):
        z = qn.standardize_column([1.0, None, 3.0, float("nan"), 5.0], "rank_z")
        self.assertFalse(z[1][1])
        self.assertEqual(z[1][0], 0.0)
        self.assertFalse(z[3][1])
        self.assertTrue(all(z[i][1] for i in (0, 2, 4)))

    def test_two_methods_registered_and_bounded(self):
        for m in qn.VALID_METHODS:
            z = qn.standardize_column([1.0, 2.0, 3.0, 1000.0], m)
            self.assertTrue(all(abs(x[0]) <= qn.CLIP + 1e-9 for x in z), m)

    def test_preregistry_frozen_and_not_in_production(self):
        v = reg.verify_registry()
        self.assertTrue(v["ok"], v["problems"])
        self.assertEqual(v["n_candidates"], 4)
        self.assertEqual(v["n_implemented"], 2)
        self.assertEqual(v["manifest_sha256"], reg.manifest_sha256())
        self.assertTrue(all(c["status"] != "IN_PRODUCTION"
                            for c in reg.PREREGISTERED))

    def test_manifest_detects_tampering(self):
        base = reg.manifest_sha256()
        c = reg.PREREGISTERED[0]
        old = c["formula"]
        try:
            c["formula"] = old + "（事后按名单调整）"
            self.assertNotEqual(reg.manifest_sha256(), base)
        finally:
            c["formula"] = old
        self.assertEqual(reg.manifest_sha256(), base)


if __name__ == "__main__":
    unittest.main(verbosity=2)
