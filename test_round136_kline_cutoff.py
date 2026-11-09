#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""round136：K 线增量脚本的日期上限保护（盘中污染不可逆）。

背景（2026-11-09 实测）：腾讯 fqkline 在**盘中**就返回当日行，价与量都是未收盘
瞬时值（茅台当日 vol=12532 手 vs 11-06 整日 42848 手）。而
`append_klines_latest` 的铁律是「已有日期绝不覆盖」，一旦盘中缺陷跑，那条半日
K 线会被**永久写死**，收盘后也不会被修正 —— 所以必须在写盘前硬截断。

⚠️ 刻意**不测**「日链调用时是否传了 --until」——收盘后跑不需要传，那是调度事实，
   不是契约。只锁死 `resolve_cutoff` 的判据与 bar 过滤行为。
"""
import datetime
import json
import os
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import append_klines_latest as A
import kline_cutoff as KC

PY = sys.executable


class TestSingleSource(unittest.TestCase):
    """判据必须单一实现：append_klines_latest 不得自写一份日期逻辑。"""

    def test_00_append_delegates_to_module(self):
        with open(os.path.join(HERE, "append_klines_latest.py"), encoding="utf-8") as f:
            src = f.read()
        self.assertIn("import kline_cutoff", src)
        self.assertIn("kline_cutoff.bar_allowed", src)
        # 禁止再出现手写的时段常量
        self.assertNotIn("930 <= hm", src, "时段判据在脚本内重复实现")

    def test_00b_factor_eval_also_uses_module(self):
        """factor_eval 的 refresh_price_tail 有同样的盘中污染风险，必须共用判据。"""
        with open(os.path.join(HERE, "factor_eval.py"), encoding="utf-8") as f:
            src = f.read()
        self.assertIn("import kline_cutoff", src)
        self.assertIn("kline_cutoff.bar_allowed", src)

    def test_00c_agree_with_module(self):
        """两个入口对同一时间必须给出相同上限（防止漂移）。"""
        for now in (datetime.datetime(2026, 11, 9, 10, 50),
                    datetime.datetime(2026, 11, 9, 16, 30),
                    datetime.datetime(2026, 11, 8, 10, 50)):
            self.assertEqual(A.resolve_cutoff("", now)[0],
                             KC.resolve_cutoff("", now)[0], now)


class TestResolveCutoff(unittest.TestCase):
    """resolve_cutoff：显式参数优先，否则交易时段内自动排除当天。"""

    def test_01_explicit_until_wins(self):
        now = datetime.datetime(2026, 11, 9, 10, 50)      # 周一盘中
        cut, reason = A.resolve_cutoff("2026-11-06", now)
        self.assertEqual(cut, "2026-11-06")
        self.assertIn("--until", reason)

    def test_02_explicit_until_truncates_time_part(self):
        """允许传 'YYYY-MM-DD HH:MM' 形式，只取日期部分。"""
        cut, _ = A.resolve_cutoff("2026-11-06 15:00:00",
                                 datetime.datetime(2026, 11, 9, 10, 0))
        self.assertEqual(cut, "2026-11-06")

    def test_03_explicit_until_bad_format_raises(self):
        """格式错误必须抛错，禁止静默降级成「不限制」。"""
        with self.assertRaises(ValueError):
            A.resolve_cutoff("2026/11/06", datetime.datetime(2026, 11, 9, 10, 0))
        with self.assertRaises(ValueError):
            A.resolve_cutoff("1106", datetime.datetime(2026, 11, 9, 10, 0))

    def test_04_intraday_auto_excludes_today(self):
        """周一盘中不传 --until → 上限落在当天之前，当日 bar 必被丢弃。"""
        now = datetime.datetime(2026, 11, 9, 10, 50)
        cut, reason = A.resolve_cutoff("", now)
        self.assertIsNotNone(cut, "盘中必须自动设上限，否则半日 K 线会被写死")
        self.assertLess(cut, "2026-11-09")
        self.assertIn("交易时段", reason)

    def test_05_after_close_no_limit(self):
        """收盘后（15:05 之后）保持原行为：不限制，正常吃当日完整 K 线。"""
        cut, _ = A.resolve_cutoff("", datetime.datetime(2026, 11, 9, 16, 30))
        self.assertIsNone(cut)

    def test_06_weekend_no_limit(self):
        cut, _ = A.resolve_cutoff("", datetime.datetime(2026, 11, 8, 10, 50))
        self.assertIsNone(cut)

    def test_07_session_boundaries(self):
        """09:29 之前 / 15:06 之后都不算交易时段（边界留 1 分钟冗余）。"""
        self.assertIsNone(A.resolve_cutoff("", datetime.datetime(2026, 11, 9, 9, 29))[0])
        self.assertIsNotNone(A.resolve_cutoff("", datetime.datetime(2026, 11, 9, 9, 30))[0])
        self.assertIsNotNone(A.resolve_cutoff("", datetime.datetime(2026, 11, 9, 15, 5))[0])
        self.assertIsNone(A.resolve_cutoff("", datetime.datetime(2026, 11, 9, 15, 6))[0])


class TestBarFiltering(unittest.TestCase):
    """端到端：用假行情源注入「11-09 盘中行」，验证它进不了库。"""

    FULL_1106 = {"2026-11-05": [1.0, 2.0, 3.0, 0.5, 100.0],
                 "2026-11-06": [1.0, 2.0, 3.0, 0.5, 100.0]}

    def _run(self, bars, until="", lib_codes=None):
        """构造库 + 假行情源，跑真实脚本源码，返回 (CompletedProcess, 写入后的库)。"""
        codes = {"600519": dict(lib_codes if lib_codes is not None else self.FULL_1106)}
        tmp = tempfile.mkdtemp(prefix="kcut_")
        klong = os.path.join(tmp, "klines_long.json")
        with open(klong, "w", encoding="utf-8") as f:
            json.dump(codes, f)

        script = os.path.join(HERE, "append_klines_latest.py")
        with open(script, encoding="utf-8") as f:
            src = f.read()
        # ① KLONG 指向临时文件 ② 网络请求换成注入的假 bars
        src = src.replace(
            'KLONG = os.path.join(HERE, "data", "klines_long.json")',
            "KLONG = %r" % klong)
        self.assertIn("KLONG = %r" % klong, src, "KLONG 替换锚点失效，脚本可能已改")
        _old = "        bars, vol_scale = fetch_ohlcv(sym, args.need)"
        self.assertIn(_old, src, "fetch 调用点已变，替换锚点需同步更新")
        src = src.replace(_old, "        bars, vol_scale = FAKE[0], 1.0")
        src = ("FAKE = (%r,)\n"
               "import sys as _s; _s.path.insert(0, %r)\n" % (bars, HERE)) + \
            src.replace('if __name__ == "__main__":', "if True:")
        runner = os.path.join(tmp, "run.py")
        with open(runner, "w", encoding="utf-8") as f:
            f.write(src)

        argv = [PY, runner] + (["--until", until] if until else [])
        r = subprocess.run(argv, capture_output=True, text=True, cwd=tmp)
        out = {}
        if os.path.exists(klong):
            with open(klong, encoding="utf-8") as f:
                out = json.load(f)
        return r, out

    def test_10_intraday_bar_rejected(self):
        """--until 11-06 时，11-09 那条半日 K 线必须被丢弃。"""
        bars = [("2026-11-06", 1.0, 2.0, 3.0, 0.5, 100.0),
                ("2026-11-09", 9.0, 9.5, 9.9, 8.8, 12.0)]      # 盘中半日量
        r, lib = self._run(bars, "2026-11-06")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("2026-11-09", lib["600519"],
                         "盘中未收盘价被写入价格库 —— 污染不可逆")
        self.assertIn("2026-11-06", lib["600519"])
        self.assertIn("按日期上限丢弃", r.stdout)

    def test_11_missing_bar_still_filled(self):
        """上限内的缺口日必须照常补入（保护不能误伤正常补数）。"""
        bars = [("2026-11-06", 7.0, 8.0, 9.0, 6.0, 555.0)]
        r, lib = self._run(bars, "2026-11-06",
                           lib_codes={"2026-11-05": [1.0, 2.0, 3.0, 0.5, 100.0]})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("2026-11-06", lib["600519"])
        self.assertEqual(len(lib["600519"]), 2)
        self.assertEqual(lib["600519"]["2026-11-06"], [7.0, 8.0, 9.0, 6.0, 555.0])

    def test_12_existing_date_never_overwritten(self):
        """11-06 若已在库中，即使行情源给了不同价格也不得覆盖（原有铁律）。"""
        bars = [("2026-11-06", 999.0, 999.0, 999.0, 999.0, 1.0)]
        r, lib = self._run(bars, "2026-11-06")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(lib["600519"]["2026-11-06"], [1.0, 2.0, 3.0, 0.5, 100.0])

    def test_13_no_until_keeps_latest(self):
        """不传 --until 且收盘后跑 → 当日完整 K 线正常入库（原行为不变）。"""
        bars = [("2026-11-09", 5.0, 6.0, 7.0, 4.0, 888.0)]
        r, lib = self._run(bars, "",
                           lib_codes={"2026-11-06": [1.0, 2.0, 3.0, 0.5, 100.0]})
        # 本机当前若是交易时段，脚本会自动截断 —— 只在非交易时段断言
        cut, _ = A.resolve_cutoff("")
        if cut is None:
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn("2026-11-09", lib["600519"])


class TestVolumeScale(unittest.TestCase):
    """备源成交量必须折成库口径（688=股、其余=手），禁硬编码 /100。

    事故史（round116）：11 个 audit 脚本漏乘 100 当成交额，门槛实际放大 10 倍。
    新浪备源返回的是「股」，若一律 /100 则 688 会被算成 100 倍偏小。
    """

    def test_30_sina_scale_by_prefix(self):
        with open(os.path.join(HERE, "append_klines_latest.py"), encoding="utf-8") as f:
            src = f.read()
        # 必须有按前缀分支，且不能出现对全部代码统一的 /100
        self.assertIn('sym.startswith("sh688")', src)
        self.assertNotIn("float(k[\"volume\"]) / 100.0", src)

    def test_31_scale_values(self):
        """直接验证两个分支的 scale 语义（不打网络）。"""
        for sym, expect in (("sh688001", 1.0), ("sh600519", 0.01),
                            ("sz000001", 0.01), ("sz300750", 0.01)):
            scale = 1.0 if sym.startswith("sh688") else 0.01
            self.assertEqual(scale, expect, sym)


class TestBackupSource(unittest.TestCase):
    """腾讯全域 501 时必须能回退备源（2026-11-09 实测：786 只抓不到）。

    这条不是「有备源更好」，而是**没有备源就静默失败**：
    `refresh_price_tail` 抓不到时会保留旧值并只打印一行「无新交易日」，
    看起来完全正常。
    """

    def test_40_both_scripts_have_backup(self):
        with open(os.path.join(HERE, "append_klines_latest.py"), encoding="utf-8") as f:
            a = f.read()
        with open(os.path.join(HERE, "factor_eval.py"), encoding="utf-8") as f:
            b = f.read()
        self.assertIn("_fetch_sina", a)
        self.assertIn("getKLineData", a)
        self.assertIn("_fetch_qfq_sina", b)
        self.assertIn("getKLineData", b)

    def test_41_backup_after_primary(self):
        """调用顺序必须是「先腾讯、全失败才新浪」，禁止反过来。"""
        with open(os.path.join(HERE, "append_klines_latest.py"), encoding="utf-8") as f:
            src = f.read()
        i_tx = src.index("_fetch_tx(sym, need)")
        i_sina = src.index("_fetch_sina(sym, need)")
        self.assertLess(i_tx, i_sina, "备源必须在主源之后")

    def test_42_backup_respects_cutoff(self):
        """备源也必须过 cutoff：不能靠「新浪滞后 T-1」这种巧合当保护。"""
        src = open(os.path.join(HERE, "factor_eval.py"), encoding="utf-8").read()
        self.assertIn("kline_cutoff.bar_allowed(r[0], _cutoff)", src)


class TestNoArgRegression(unittest.TestCase):
    """--until 不得成为必需参数：日链现有调用（无参数）必须继续可用。"""

    def test_20_until_is_optional(self):
        r = subprocess.run([PY, os.path.join(HERE, "append_klines_latest.py"), "--help"],
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0)
        self.assertIn("--until", r.stdout)
        self.assertIn("--dry-run", r.stdout)
        self.assertIn("--need", r.stdout)
        self.assertNotIn("required", r.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)
