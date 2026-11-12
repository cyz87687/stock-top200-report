"""test_round141_scope_and_stability.py —— round141 P0-2 / P1-2 / P1-3 回归。

沿用 test_round117_p0_p1.py:19-31 的设计原则：
  1. 不联网  2. 不用 pytest 也能跑  3. 断言贴真实数据，不写「大概」

覆盖：
  - artifact_scope：四态映射、权重审批拒绝、禁止混报、徽标
  - pit_sample_label：三类标签、purge/h 边界、真实数据下 strict-oos = 0
  - weight_perturbation：确定性、输入不改、只标记不否决
  - proxy_vs_prod_rank：Spearman 正确性、长面板真实维度边界
"""
from __future__ import annotations

import json
import os
import unittest

import artifact_scope as AS
import pit_sample_label as SL
import proxy_vs_prod_rank as PR
import scoring_core as SC
import weight_perturbation as WP

HERE = os.path.dirname(os.path.abspath(__file__))

# 测试夹具：合成日历，仅用于验证判据顺序；不是节假日表，不用于生产。
FIXTURE_CAL = ["T%03d" % i for i in range(100)]


def _label_kw(**over):
    base = {
        "asof": "T099",
        "weight_fitted_at": "T030",
        "calendar": FIXTURE_CAL,
        "horizon": 20,
        "purge": 20,
        "factor_parity": True,
        "has_pit_vintage": True,
        "pit_mode": SL.STRICT_PIT_MODE,
    }
    base.update(over)
    return base


class TestArtifactScope(unittest.TestCase):

    def test_proxy_meta_goes_to_proxy_diagnostic(self):
        meta = SC.artifact_meta(factor_parity=False)
        g = AS.classify_scope(meta)
        self.assertEqual(g["scope"], AS.PROXY_DIAGNOSTIC)
        self.assertEqual(g["artifact_status"], "PROXY_ONLY")
        self.assertFalse(g["weight_approval_allowed"])
        self.assertEqual(g["watermark"], "red")
        self.assertIn("禁用于权重审批", AS.render_badge(meta))

    def test_parity_meta_goes_to_pit_valid(self):
        meta = SC.artifact_meta(factor_parity=True)
        g = AS.classify_scope(meta)
        self.assertEqual(g["scope"], AS.PIT_VALID)
        self.assertEqual(g["artifact_status"], "COMPARABLE")
        self.assertTrue(g["weight_approval_allowed"])
        self.assertEqual(g["proxy_dims"], {})
        self.assertEqual(g["watermark"], "none")

    def test_unknown_meta_never_approved(self):
        g = AS.classify_scope({"foo": "bar"})
        self.assertEqual(g["artifact_status"], "UNKNOWN")
        self.assertEqual(g["scope"], AS.PROXY_DIAGNOSTIC)
        self.assertFalse(g["weight_approval_allowed"])

    def test_weight_approval_rejects_proxy(self):
        with self.assertRaises(AS.ScopeViolation):
            AS.assert_weight_approval(
                SC.artifact_meta(factor_parity=False), "5y_diagnostic")

    def test_declared_scope_mismatch_fails(self):
        with self.assertRaises(AS.ScopeViolation):
            AS.assert_scope(SC.artifact_meta(factor_parity=False),
                            AS.PIT_VALID, "long_panel")

    def test_unknown_scope_name_fails(self):
        with self.assertRaises(AS.ScopeViolation):
            AS.assert_scope(SC.artifact_meta(), "something-else", "x")

    def test_mixed_report_fails(self):
        items = [("proxy_panel", SC.artifact_meta(factor_parity=False)),
                 ("pit_panel", SC.artifact_meta(factor_parity=True))]
        r = AS.audit_report_sources(items)
        self.assertTrue(r["mixed"])
        self.assertFalse(r["ok"])
        self.assertTrue(any("混入两条链路" in v for v in r["violations"]))

    def test_pure_diagnostic_report_ok(self):
        items = [("a", SC.artifact_meta(factor_parity=False)),
                 ("b", SC.artifact_meta(factor_parity=False))]
        r = AS.audit_report_sources(items)
        self.assertFalse(r["mixed"])
        self.assertTrue(r["ok"])

    def test_weight_approval_report_with_proxy_fails(self):
        r = AS.audit_report_sources(
            [("long_panel", SC.artifact_meta(factor_parity=False))],
            purpose="weight_approval")
        self.assertFalse(r["ok"])
        self.assertTrue(any("权重审批报告混入代理产物" in v
                            for v in r["violations"]))


class TestSampleLabels(unittest.TestCase):

    def test_proxy_when_no_pit_vintage(self):
        g = SL.label_snapshot("T060", **_label_kw(has_pit_vintage=False))
        self.assertEqual(g["label"], SL.PROXY)
        self.assertFalse(g["checks"]["has_pit_vintage"])

    def test_proxy_when_proxy_factors(self):
        g = SL.label_snapshot("T060", **_label_kw(factor_parity=False))
        self.assertEqual(g["label"], SL.PROXY)

    def test_proxy_when_approximate_pit(self):
        g = SL.label_snapshot("T060", **_label_kw(
            pit_mode="statutory_deadline_approx"))
        self.assertEqual(g["label"], SL.PROXY)

    def test_in_sample_is_proxy(self):
        g = SL.label_snapshot("T020", **_label_kw())
        self.assertEqual(g["label"], SL.PROXY)
        self.assertFalse(g["checks"]["after_weight_freeze"])

    def test_quasi_when_h_incomplete(self):
        g = SL.label_snapshot("T085", **_label_kw())
        self.assertEqual(g["label"], SL.QUASI_OOS)
        self.assertFalse(g["checks"]["h20_complete"])

    def test_quasi_when_purge_not_cleared(self):
        g = SL.label_snapshot("T045", **_label_kw())
        self.assertEqual(g["label"], SL.QUASI_OOS)
        self.assertFalse(g["checks"]["purge_ok"])

    def test_strict_oos(self):
        g = SL.label_snapshot("T060", **_label_kw())
        self.assertEqual(g["label"], SL.STRICT_OOS)
        self.assertTrue(g["checks"]["h20_complete"])
        self.assertTrue(g["checks"]["purge_ok"])
        self.assertEqual(g["fit_participation"], "forbidden")
        self.assertTrue(g["must_display"])

    def test_unknown_date_gets_no_label(self):
        g = SL.label_snapshot("X999", **_label_kw())
        self.assertIsNone(g["label"])

    def test_oos_gate_threshold(self):
        below = SL.oos_gate([SL.STRICT_OOS] * 29)
        at = SL.oos_gate([SL.STRICT_OOS] * 30)
        self.assertFalse(below["start_formal_validation"])
        self.assertEqual(below["verdict"], "WAIT_FOR_DATA")
        self.assertTrue(at["start_formal_validation"])
        self.assertEqual(at["verdict"], "READY")

    def test_real_project_defaults(self):
        d = SL.load_project_defaults()
        self.assertEqual(d["weight_fitted_at"], "2026-11-04")
        self.assertEqual(d["horizon"], 20)
        self.assertIn("2026-10-30", d["calendar"])
        self.assertIn("2026-11-11", d["calendar"])
        self.assertEqual(d["calendar"], sorted(set(d["calendar"])))

    def test_current_real_data_has_zero_strict_oos(self):
        d = SL.load_project_defaults()
        labels = SL.label_all(
            d["calendar"][-40:],
            asof=d["calendar"][-1],
            weight_fitted_at=d["weight_fitted_at"],
            calendar=d["calendar"],
            horizon=d["horizon"],
            factor_parity=False,
            has_pit_vintage=False)
        gate = SL.oos_gate(labels)
        self.assertEqual(gate["n_strict_oos"], 0)
        self.assertEqual(gate["verdict"], "WAIT_FOR_DATA")


class TestPerturbation(unittest.TestCase):

    def _rows(self):
        return [{"code": "%06d" % i,
                 "score_growth": float(i % 10),
                 "score_quality": float((i * 3) % 10),
                 "score_fund": float((i * 7) % 5),
                 "total": float(i)} for i in range(60)]

    def test_same_seed_is_deterministic(self):
        a, _ = WP.perturb_rows(self._rows(), "gaussian_noise", seed=7)
        b, _ = WP.perturb_rows(self._rows(), "gaussian_noise", seed=7)
        self.assertEqual(a, b)

    def test_input_not_mutated(self):
        rows = self._rows()
        snapshot = json.dumps(rows, sort_keys=True)
        WP.perturb_rows(rows, "gaussian_noise", seed=3)
        self.assertEqual(json.dumps(rows, sort_keys=True), snapshot)

    def test_all_modes_available(self):
        for m in WP.MODES:
            out, meta = WP.perturb_rows(self._rows(), m, seed=1)
            self.assertEqual(meta["mode"], m)
            self.assertEqual(len(out), 60)

    def test_noise_respects_dim_max(self):
        out, _ = WP.perturb_rows(self._rows(), "gaussian_noise",
                                 seed=1, sigma=0.5, dim_max=10.0)
        for r in out:
            for k in ("score_growth", "score_quality", "score_fund"):
                self.assertGreaterEqual(r[k], 0.0)
                self.assertLessEqual(r[k], 10.0)

    def test_noise_sensitive_is_mark_only(self):
        base = {"net_return_pct": 100.0, "weights": {"a": 0.5, "b": 0.5}}
        pert = [{"net_return_pct": 130.0, "weights": {"a": 0.6, "b": 0.4}}]
        s = WP.stability_score(base, pert)
        self.assertTrue(s["noise_sensitive"])
        self.assertEqual(s["verdict"], "NOISE_SENSITIVE_MARK_ONLY")
        self.assertIn("G1~G5", s["action"])
        self.assertIn("非第六道门禁", s["disclosure"])
        self.assertAlmostEqual(s["net_jump_max"], 0.30, places=4)
        self.assertAlmostEqual(s["l1_drift_max"], 0.20, places=4)

    def test_stable_case(self):
        base = {"net_return_pct": 100.0, "weights": {"a": 0.5, "b": 0.5}}
        s = WP.stability_score(base, [dict(base)])
        self.assertFalse(s["noise_sensitive"])
        self.assertEqual(s["verdict"], "STABLE")
        self.assertEqual(s["stability_score"], 1.0)

    def test_run_suite_does_not_veto(self):
        def ev(rows):
            return {"net_return_pct": sum(r["score_growth"] for r in rows),
                    "weights": {"a": 0.5, "b": 0.5}}
        r = WP.run_suite(ev, self._rows(), seeds=(1, 2))
        self.assertEqual(len(r["perturbations"]), len(WP.MODES) * 2)
        self.assertIn("stability", r)
        self.assertNotIn("reject", r["stability"])
        self.assertNotIn("veto", r["stability"])


class TestRankMonitorAndRealFacts(unittest.TestCase):

    def test_spearman_perfect_and_reversed(self):
        xs = list(range(30))
        self.assertAlmostEqual(PR.spearman(xs, xs), 1.0, places=6)
        self.assertAlmostEqual(PR.spearman(xs, xs[::-1]), -1.0, places=6)

    def test_spearman_small_sample_returns_none(self):
        self.assertIsNone(PR.spearman([1, 2], [1, 2]))

    def test_spearman_handles_nan_and_ties(self):
        xs = [1, 1, 1, 2, 2, 3, float("nan")] + list(range(4, 25))
        ys = list(range(len(xs)))
        v = PR.spearman(xs, ys)
        self.assertIsNotNone(v)

    def test_real_panel_has_nine_dims_and_no_fund(self):
        dates, codes, dims, S = PR.load_proxy(
            os.path.join(HERE, "data", "panel_r124_mat.npz"))
        self.assertEqual(len(dims), 9)
        self.assertNotIn("score_fund", dims)
        self.assertEqual(len(dates), 1653)
        self.assertEqual(dates[-1], "2026-10-30")
        self.assertEqual(len(codes), 2257)

    def test_real_pit_audit_zero_coverage(self):
        with open(os.path.join(HERE, "data", "pit_audit_round130.json"),
                  encoding="utf-8") as f:
            d = json.load(f)
        self.assertEqual(d["verdict"], "PIT_INCOMPLETE")
        self.assertEqual(d["coverage"]["rows_with_announcement_time"], 0)
        self.assertEqual(d["coverage"]["rows_with_revision_id"], 0)
        self.assertEqual(d["required_schema"],
                         ["announcement_time", "available_time",
                          "revision_id", "source"])

    def test_real_weight_fitted_at(self):
        with open(os.path.join(HERE, "factor_weights.json"),
                  encoding="utf-8") as f:
            fw = json.load(f)
        self.assertEqual(fw["version"], "v4.19-c1-sustain-gates")
        self.assertEqual(fw["fitted_at"][:10], "2026-11-04")
        self.assertEqual(fw["target_horizon"], 20)


class TestWalkforwardWatchdog(unittest.TestCase):

    def _real_diag(self):
        with open(os.path.join(HERE, "data", "weight_diagnostic_5y_20261111.json"),
                  encoding="utf-8") as f:
            return json.load(f)

    def test_real_diag_blocks_change(self):
        import walkforward_watchdog as WW
        r = WW.evaluate(self._real_diag())
        self.assertFalse(r["should_change_weights"])
        self.assertTrue(r["blockers"])
        self.assertFalse(r["weights_modified"])
        self.assertTrue(r["requires_manual_arbitration"])
        self.assertEqual(r["scope"]["scope"], AS.PROXY_DIAGNOSTIC)
        self.assertAlmostEqual(r["checks"]["lambda"], 0.0, places=4)
        self.assertAlmostEqual(r["checks"]["g5_pass_rate"], 0.3333, places=4)
        self.assertGreater(r["checks"]["purged_cross_L1_drift"], 0.25)
        self.assertEqual(r["checks"]["five_door_verdict"], "FAIL")

    def test_alerts_cover_drift_lambda_and_scope(self):
        import walkforward_watchdog as WW
        r = WW.evaluate(self._real_diag())
        joined = " ".join(r["alerts"])
        self.assertIn("L1 漂移", joined)
        self.assertIn("锁定", joined)
        self.assertIn("proxy-diagnostic", joined)

    def test_all_pass_still_requires_manual_arbitration(self):
        import walkforward_watchdog as WW
        diag = {
            "required_disclosures": [], "conclusion": "",
            "gate_five_doors": {
                "verdict": "PASS", "failed": [], "gates": {},
                "walkforward_summary": {
                    "n_folds": 6, "n_pass": 6, "pass_rate": 0.9,
                    "pass_rate_threshold": 0.5, "n_g1_strict": 6},
                "walkforward_folds": [{"gates": {"pass": True}} for _ in range(6)],
            },
            "shrinkage_rule": {"lambda": 0.5, "n_eff": 60},
            "bonferroni": {"threshold": 3.0},
            "candidate_half_L1_drift": 0.1,
            "factor_summary_5y": {
                "d1": {"top10_excess_nw_t19": 4.0, "top10_excess_mean": 0.01}},
        }
        r = WW.evaluate(diag, scope_meta=SC.artifact_meta(factor_parity=True))
        self.assertTrue(r["should_change_weights"])
        self.assertEqual(r["blockers"], [])
        self.assertFalse(r["weights_modified"])
        self.assertTrue(r["requires_manual_arbitration"])

    def test_tail_fail_streak(self):
        import walkforward_watchdog as WW
        folds = [{"gates": {"pass": True}},
                 {"gates": {"pass": False}},
                 {"gates": {"pass": False}}]
        self.assertEqual(WW._tail_fail_streak(folds), 2)

    def test_html_from_same_result(self):
        import walkforward_watchdog as WW
        r = WW.evaluate(self._real_diag())
        h = WW.render_html(r)
        self.assertIn("FALSE", h)
        self.assertIn("proxy-diagnostic", h)
        self.assertIn("经验证的 alpha", h)


if __name__ == "__main__":
    unittest.main(verbosity=2)
