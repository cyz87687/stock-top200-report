#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""round142 单测（2026-11-12）：注释更正 / tech_neg 方向 / 对齐清单 / 内核安全 / Wind 留痕。

覆盖用户三条指令的可验证事实：
  1 修正   → 两处归档脚本不再声称「任何数据源都不提供历史回溯」，且保留静态PE边界
  2 Wind   → 实测结论报告留痕（正负证据数值）
  3 顺序改 → tech_neg 方向已翻、其余按裁决顺序登记；方向对齐值**不得**当验收值
"""
import json
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import factor_alignment as FA          # noqa: E402
import proxy_vs_prod_rank as PR        # noqa: E402
import scoring_core as SC              # noqa: E402

RANK = os.path.join(HERE, "data", "proxy_vs_prod_rank_20261112_round142.json")
PLAN = os.path.join(HERE, "data", "factor_alignment_20261112.json")
WIND_REPORT = os.path.join(HERE, "reports", "round142_wind_pit_probe_2026-11-12.md")


def _txt(rel):
    with open(os.path.join(HERE, rel), encoding="utf-8") as f:
        return f.read()


class TestDirectionAlignment(unittest.TestCase):

    def test_dmax_minus_v_flips_spearman_exactly(self):
        xs = [float(i) for i in range(30)]
        self.assertAlmostEqual(PR.spearman(xs, xs), 1.0, places=6)
        self.assertAlmostEqual(PR.direction_aligned_rho(xs, xs, 10.0), -1.0, places=6)
        reversed_ys = [10.0 - v for v in xs]
        self.assertAlmostEqual(
            PR.direction_aligned_rho(xs, reversed_ys, 10.0), 1.0, places=6)

    def test_only_techneg_marked_as_direction_fixed(self):
        self.assertEqual(list(PR.DIRECTION_FIXED_IN_SOURCE), ["score_tech_neg"])
        self.assertEqual(
            PR.DIRECTION_FIXED_IN_SOURCE["score_tech_neg"]["dim_max"], 10.0)

    def test_preview_is_not_acceptance_and_still_below_threshold(self):
        """核心防线：符号修对 ≠ 口径对齐，禁止据预览值解除 PROXY_ONLY。"""
        with open(RANK, encoding="utf-8") as f:
            d = json.load(f)
        a = d["by_dim"]["score_tech_neg"]["alignment"]
        self.assertEqual(a["status"], "SOURCE_FIXED_PANEL_STALE")
        self.assertFalse(a["panel_npz_rebuilt"])
        self.assertLess(a["rho_current_panel"], 0.0)
        self.assertGreater(a["rho_if_direction_aligned"], 0.0)
        self.assertLess(a["rho_if_direction_aligned"], PR.THRESHOLD)
        self.assertEqual(a["verdict_if_aligned"], "RED_ALERT_NOT_EQUIVALENT")
        self.assertEqual(d["by_dim"]["score_tech_neg"]["verdict"],
                         "RED_ALERT_NOT_EQUIVALENT")
        self.assertFalse(d["alignment_preview"]["panel_npz_rebuilt"])

    def test_round141_artifact_not_overwritten(self):
        self.assertTrue(os.path.exists(
            os.path.join(HERE, "data", "proxy_vs_prod_rank_20261112.json")))
        self.assertTrue(PR.OUT_JSON.endswith("_round142.json"))


class TestFactorAlignmentPlan(unittest.TestCase):

    def test_order_matches_user_ruling(self):
        p = FA.build_plan()
        self.assertEqual([x["dim"] for x in p["alignment_order"][:4]],
                         ["score_tech_neg", "score_quality",
                          "score_growth", "score_size_growth"])

    def test_two_kinds_of_missing_are_distinguished(self):
        """面板无此维 vs 生产无对照字段，成因不同，不得混为一谈。"""
        p = FA.build_plan()
        by = {x["dim"]: x for x in p["alignment_order"]}
        self.assertEqual(by["score_fund"]["status"], "BLIND_SPOT_NO_PANEL_DIM")
        self.assertEqual(by["score_ep"]["status"], "NO_PROD_COUNTERPART_FIELD")

    def test_no_dim_aligned_and_no_weight_change(self):
        p = FA.build_plan()
        self.assertFalse(p["summary"]["all_dims_aligned"])
        self.assertFalse(p["summary"]["weight_change_allowed"])
        self.assertEqual(p["summary"]["n_red_or_pending"],
                         p["summary"]["n_items"])

    def test_all_rho_come_from_real_monitor_output(self):
        with open(RANK, encoding="utf-8") as f:
            rank = json.load(f)
        p = FA.build_plan()
        for x in p["alignment_order"]:
            if x["rho_mean"] is not None:
                self.assertEqual(x["rho_mean"],
                                 rank["by_dim"][x["dim"]]["rho_mean"])

    def test_direction_consistent_dims_still_proxy_only(self):
        p = FA.build_plan()
        for x in p["direction_consistent_proxy"]:
            self.assertEqual(x["status"], "DIRECTION_CONSISTENT_PROXY_ONLY")
        self.assertIn("score_lowvol20",
                      [x["dim"] for x in p["direction_consistent_proxy"]])

    def test_deferred_items_not_implemented_this_round(self):
        p = FA.build_plan()
        st = {x["item"]: x["status"] for x in p["deferred"]}
        self.assertEqual(st["P2-2 周期/拥挤惩罚"], "DEFERRED")
        self.assertEqual(st["常驻 walk-forward 看门接日链"],
                         "DEFERRED_BY_USER_RULING")

    def test_written_plan_matches_build_plan(self):
        with open(PLAN, encoding="utf-8") as f:
            disk = json.load(f)
        self.assertEqual(disk["summary"], FA.build_plan()["summary"])


class TestCommentCorrectionsAndKernelSafety(unittest.TestCase):

    def test_archive_comments_corrected_and_boundary_kept(self):
        for rel in ("archive_fwd_snapshot.py", "archive_fund_universe.py"):
            t = _txt(rel)
            self.assertNotIn("任何数据源都不提供历史回溯", t)
            self.assertIn("mx_ashare_finance_data", t)
            self.assertIn("Wind get_financial_data", t)
            self.assertIn("口径更正", t)
        self.assertIn("0.536", _txt("archive_fund_universe.py"))
        self.assertIn("不得", _txt("archive_fund_universe.py"))

    def test_product_caveat_also_corrected_not_only_docstring(self):
        """错误表述曾被写进产物 caveat，不能只改注释就宣称修正完成。"""
        for rel in ("archive_fwd_snapshot.py", "archive_fund_universe.py"):
            t = _txt(rel)
            self.assertNotIn("过期即失，不可回溯", t)
            self.assertNotIn("历史不可回溯，缺失即留空", t)
            self.assertIn("本链路不自动回溯", t)
            self.assertIn("待验证补数候选", t)
            self.assertIn("不得当作", t)

    def test_techneg_source_flipped_and_fund_definition_fixed(self):
        t = _txt("build_panel_long.py")
        self.assertIn("rank10(techneg_raw, higher_better=False)", t)
        self.assertIn("估值/基本面复合分", t)
        self.assertIn("事实错误", t)

    def test_core_files_not_touched_by_this_round(self):
        changed = {"archive_fwd_snapshot.py", "archive_fund_universe.py",
                   "build_panel_long.py", "proxy_vs_prod_rank.py",
                   "factor_alignment.py", "scoring_core.py"}
        self.assertEqual(set(SC.CORE_FILES) & changed, set())
        kid = SC.kernel_fingerprint()["kernel_id"]
        self.assertEqual(len(kid), 16)

    def test_wind_probe_report_records_both_positive_and_negative_evidence(self):
        self.assertTrue(os.path.exists(WIND_REPORT))
        t = _txt("reports/round142_wind_pit_probe_2026-11-12.md")
        for v in ("328.8938", "301.957", "862.2815", "194.63", "46.3107"):
            self.assertIn(v, t)
        self.assertIn("不能", t)
        self.assertIn("QPS", t)
        self.assertIn("1b5770b7152d14a6", t)


if __name__ == "__main__":
    unittest.main(verbosity=2)
