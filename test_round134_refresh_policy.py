#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""round134：分层数据更新频率判据回归（老大 2026-11-06 裁决③）。

⚠️ 刻意**不测**「日更链当前是否接入」——那是本轮审计发现的临时事实，
   一旦 P1 接线就会变，写进测试就是新的死断言（项目已多次踩坑）。
   接线与否只用文档留痕。
"""
import json
import os
import subprocess
import sys
import unittest
from datetime import date, datetime

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import data_refresh_policy as P
import growth_factor_b as G
import schedule_guard as SG

PY = sys.executable


class TestDateUtils(unittest.TestCase):

    def test_01_as_date_variants(self):
        self.assertEqual(P.as_date("2026-11-06"), date(2026, 11, 6))
        self.assertEqual(P.as_date(date(2026, 11, 6)), date(2026, 11, 6))
        self.assertEqual(P.as_date(datetime(2026, 11, 6, 15, 30)), date(2026, 11, 6))
        self.assertEqual(P.as_date("2026-11-06 15:30:00"), date(2026, 11, 6))
        self.assertIsInstance(P.as_date(None), date)

    def test_02_deadline_reused_not_duplicated(self):
        """报告期截止日必须复用 growth_factor_b，禁止另立一份。"""
        self.assertIs(P._PERIOD_DEADLINE, G._PERIOD_DEADLINE)
        self.assertEqual(P._period_deadline("20260930"), "2026-10-31")
        self.assertEqual(P._period_deadline("20261231"), "2027-04-30")
        self.assertEqual(P._period_deadline("20260331"), "2026-04-30")
        self.assertEqual(P._period_deadline("20260630"), "2026-08-31")


class TestReportPeriod(unittest.TestCase):

    def test_10_effective_period_after_q3_closed(self):
        """11-06 → 三季报 20260930；三季报公布完后不再更新。"""
        self.assertEqual(P.effective_report_period("2026-11-06"), "20260930")
        self.assertEqual(P.effective_report_period("2026-11-30"), "20260930")
        self.assertEqual(P.effective_report_period("2027-01-15"), "20260930")

    def test_11_effective_period_strict_pit(self):
        """10-30 时三季报法定截止(10-31)未过 → 仍用中报，禁前视。"""
        self.assertEqual(P.effective_report_period("2026-10-30"), "20260630")
        self.assertEqual(P.effective_report_period("2026-10-31"), "20260930")
        self.assertEqual(P.effective_report_period("2026-03-15"), "20250930")

    def test_12_effective_period_after_annual(self):
        """2027-05-01 年报与一季报均已到截止 → 取最新报告期。"""
        self.assertEqual(P.effective_report_period("2027-05-01"), "20270331")

    def test_13_windows(self):
        w = P.active_disclosure_windows("2026-10-15")
        self.assertEqual([x["period"] for x in w], ["20260930"])
        self.assertEqual(w[0]["deadline"], "2026-10-31")
        self.assertEqual(P.active_disclosure_windows("2026-11-06"), [])
        self.assertEqual(P.active_disclosure_windows("2026-11-01"), [])

    def test_14_annual_and_q1_windows_overlap(self):
        w = P.active_disclosure_windows("2027-04-15")
        self.assertEqual(sorted(x["period"] for x in w), ["20261231", "20270331"])

    def test_15_latest_closed_window(self):
        c = P.latest_closed_window("2026-11-06")
        self.assertEqual(c["period"], "20260930")
        self.assertEqual(c["end"], "2026-10-31")


class TestPool(unittest.TestCase):

    def test_20_friday_only(self):
        self.assertTrue(P.pool_should_refresh("2026-11-06")["should_refresh"])
        self.assertFalse(P.pool_should_refresh("2026-11-05")["should_refresh"])
        self.assertFalse(P.pool_should_refresh("2026-11-09")["should_refresh"])
        self.assertFalse(P.pool_should_refresh("2026-11-07")["should_refresh"])

    def test_21_next_friday(self):
        self.assertTrue(P.pool_should_refresh("2026-11-13")["should_refresh"])

    def test_22_cfg_weekday_and_disabled(self):
        self.assertFalse(P.pool_should_refresh(
            "2026-11-06", {"schedule": {"pool_rerun_weekday": 3}})["should_refresh"])
        self.assertTrue(P.pool_should_refresh(
            "2026-11-05", {"schedule": {"pool_rerun_weekday": 3}})["should_refresh"])
        # enabled=false 是「关闭降采样」→ 退回每日更新
        d = P.pool_should_refresh("2026-11-05",
                                  {"schedule": {"enabled": False}})
        self.assertTrue(d["should_refresh"])
        self.assertIn("enabled=false", d["reason"])

    def test_23_config_matches_module_default(self):
        """config.json 与模块默认值必须一致，防口径漂移。"""
        sc = P.load_schedule()
        self.assertEqual(int(sc.get("pool_rerun_weekday", 4)), 4)
        self.assertTrue(sc.get("enabled", True))

    def test_24_schedule_guard_crosscheck(self):
        """与 schedule_guard.plan_mode 在周五 15:30 判定一致（同源交叉校验）。"""
        d = datetime(2026, 11, 6, 15, 30)
        mode, _ = SG.plan_mode(d)
        self.assertEqual(mode, "full")
        d2 = datetime(2026, 11, 5, 15, 30)
        mode2, _ = SG.plan_mode(d2)
        self.assertEqual(mode2, "quote_only")


class TestFinancial(unittest.TestCase):

    def test_30_frozen_after_q3(self):
        """老大原话「如三季报公布完后不再更新」：三季报窗口关闭后到年报窗口开启前冻结。

        ⚠️ 冻结是「报告期级」不是「永久级」：2027-01-01 年报披露窗口开启后恢复更新，
           见 test_34a。三季报窗口 10/31 关闭 → 下一次财务刷新是年报窗口。
        """
        for ds in ("2026-11-01", "2026-11-06", "2026-12-15", "2026-12-31"):
            d = P.financial_should_refresh(ds)
            self.assertFalse(d["should_refresh"], ds)
            self.assertIn("冻结期", d["reason"])
        self.assertEqual(P.financial_should_refresh("2026-11-06")["effective_period"],
                         "20260930")

    def test_34a_freeze_ends_when_annual_window_opens(self):
        """年报窗口 2027-01-01 开启 → 财务恢复按披露事件更新（不是永久冻结）。"""
        self.assertFalse(P.financial_should_refresh("2026-12-31")["should_refresh"])
        d = P.financial_should_refresh("2027-01-01", has_new_disclosure=False)
        self.assertIn("20261231", [w["period"] for w in d["active_windows"]])
        # 窗口开启但当日无新披露 → 仍不重算（事件驱动）
        self.assertFalse(d["should_refresh"])
        d2 = P.financial_should_refresh("2027-01-01", has_new_disclosure=True)
        self.assertTrue(d2["should_refresh"])

    def test_31_window_open_scans(self):
        d = P.financial_should_refresh("2026-10-15")
        self.assertTrue(d["should_refresh"])
        self.assertEqual(len(d["active_windows"]), 1)
        self.assertEqual(d["active_windows"][0]["period"], "20260930")

    def test_32_event_driven_no_new_disclosure(self):
        d = P.financial_should_refresh("2026-10-15", has_new_disclosure=False)
        self.assertFalse(d["should_refresh"])
        d2 = P.financial_should_refresh("2026-10-15", has_new_disclosure=True)
        self.assertTrue(d2["should_refresh"])

    def test_33_force_overrides_freeze(self):
        d = P.financial_should_refresh("2026-11-06", force=True)
        self.assertTrue(d["should_refresh"])
        self.assertIn("force", d["reason"])

    def test_34_annual_window(self):
        d = P.financial_should_refresh("2027-02-10")
        self.assertTrue(d["should_refresh"])
        self.assertIn("20261231", [w["period"] for w in d["active_windows"]])


class TestMarket(unittest.TestCase):

    def test_40_weekday_daily(self):
        for ds in ("2026-11-02", "2026-11-06", "2026-11-09"):
            self.assertTrue(P.market_should_refresh(ds)["should_refresh"], ds)

    def test_41_weekend_frozen(self):
        for ds in ("2026-11-07", "2026-11-08"):
            self.assertFalse(P.market_should_refresh(ds)["should_refresh"], ds)

    def test_42_injected_trade_day_overrides(self):
        """法定节假日由调用方注入真实日历；本模块不编节假日表。"""
        self.assertFalse(P.market_should_refresh(
            "2026-11-06", is_trade_day=False)["should_refresh"])
        self.assertTrue(P.market_should_refresh(
            "2026-11-07", is_trade_day=True)["should_refresh"])

    def test_43_reason_discloses_no_holiday_table(self):
        r = P.market_should_refresh("2026-11-06")["reason"]
        self.assertIn("check_data_fresh", r)
        self.assertIn("法定节假日", r)


class TestPlan(unittest.TestCase):

    def test_50_today_full(self):
        p = P.plan("2026-11-06")
        self.assertEqual(p["mode"], "full")
        self.assertTrue(p["decisions"][P.POOL]["should_refresh"])
        self.assertFalse(p["decisions"][P.FINANCIAL]["should_refresh"])
        self.assertTrue(p["decisions"][P.MARKET]["should_refresh"])
        self.assertEqual(p["effective_report_period"], "20260930")

    def test_51_monday_quote_only(self):
        p = P.plan("2026-11-09")
        self.assertEqual(p["mode"], "quote_only")
        self.assertFalse(p["decisions"][P.POOL]["should_refresh"])
        self.assertFalse(p["decisions"][P.FINANCIAL]["should_refresh"])
        self.assertTrue(p["decisions"][P.MARKET]["should_refresh"])

    def test_52_window_drives_full_even_on_monday(self):
        """财报窗口内有披露 → 即使非周五也要完整评分（规则是分层，不是只认周五）。"""
        p = P.plan("2026-10-12", has_new_disclosure=True)
        self.assertEqual(p["mode"], "full")
        self.assertTrue(p["decisions"][P.FINANCIAL]["should_refresh"])

    def test_53_weekend_all_frozen_except_force(self):
        p = P.plan("2026-11-07")
        self.assertEqual(p["mode"], "quote_only")
        self.assertFalse(p["decisions"][P.MARKET]["should_refresh"])

    def test_54_caveats_present(self):
        p = P.plan("2026-11-06")
        self.assertTrue(any("不编造节假日" in c or "无本地日历表" in c
                            for c in p["caveats"]))
        self.assertIn("data_refresh_policy", p["policy_source"])

    def test_55_cli(self):
        r = subprocess.run([PY, os.path.join(HERE, "data_refresh_policy.py"),
                            "--at", "2026-11-06"],
                           capture_output=True, text=True, cwd=HERE)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("POOL", r.stdout)
        self.assertIn("FINANCIAL", r.stdout)
        self.assertIn("MARKET", r.stdout)
        self.assertIn("冻结期", r.stdout)

    def test_56_cli_json(self):
        r = subprocess.run([PY, os.path.join(HERE, "data_refresh_policy.py"),
                            "--at", "2026-11-09", "--json"],
                           capture_output=True, text=True, cwd=HERE)
        self.assertEqual(r.returncode, 0, r.stderr)
        d = json.loads(r.stdout)
        self.assertEqual(d["mode"], "quote_only")


class TestSourceContract(unittest.TestCase):

    def setUp(self):
        with open(os.path.join(HERE, "data_refresh_policy.py"), encoding="utf-8") as f:
            self.src = f.read()

    def test_60_doc_holds_ruling_and_audit(self):
        for k in ("股票池周五更新", "财务数据", "行情数据每日更新",
                  "零接入", "不编造节假日表", "只判定、不执行"):
            self.assertIn(k, self.src, k)

    def test_61_no_hardcoded_holiday_list(self):
        """源码里不得出现自造的节假日日期表。"""
        for bad in ("春节", "国庆", "HOLIDAYS = [", "holiday_table"):
            self.assertNotIn(bad, self.src, bad)


if __name__ == "__main__":
    unittest.main(verbosity=2)
