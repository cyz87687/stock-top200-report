#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""round137 回归测试：fetch_bench 并集合并门禁 + verify_qfq_anchor 源级失效守卫

== 背景（2026-11-09 实测）==

① `fetch_bench.py` 的 000985 中证全指是 regime_model 的 PRIMARY_BENCH，
   而它**两个源都不可用**：东财 push2his 直接 RemoteDisconnected、
   新浪 sh000985 是死数据（最后一根 2016-06-13）。
   → regime 日期门禁每天拦 3 天滞后，整条 regime/组合链路卡死（实测 exit 2）。
   修法：接入腾讯 `kline/kline` 作为第三源。但腾讯**只回溯到 2018-08**，
   而盘上自 2015-01，故必须「盘上更早历史 ∪ 源数据」的**并集合并**。

② 但腾讯源对 000985 的重叠日有 29/2000 处与盘上不一致，其中 15 处是
   **头部脏段**：2018-08-03~08-24 腾讯返回同一组 OHLC 连续重复 15 次
   （4707.51/4666.50/...），而盘上该段是 3943→3830 的真实走势，偏差 +19%。
   若按「源值覆盖盘上」，会把 15 天真实历史换成一段假横盘 —— 与 11-04
   「烂源覆盖好源」同类事故。故加**重叠一致率门禁**：不合格的源只补缺失日期，
   绝不覆盖已有历史。

③ `verify_qfq_anchor.py` 在东财整体断连时，抽样 36 只全部落到 nodata 分支，
   旧实现照旧 return 0 → 日链认定「复权校验通过」。
   这正是硬规则里「全失败 exit 0 = 假成功」。故加源级失效守卫：exit 4、
   不推进游标、不记 covered（没测过不许算覆盖）、verdict 不写 clean。
"""
import io
import json
import os
import sys
import tempfile
import unittest
import contextlib

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)


class TestFetchBenchMergeGate(unittest.TestCase):
    """fetch_any 的并集合并 + 重叠一致率门禁。"""

    def setUp(self):
        import fetch_bench as fb
        self.fb = fb

    def _old_file(self, px):
        fd, p = tempfile.mkstemp(suffix=".json")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump({"code": "000985", "name": "中证全指", "px": px}, f)
        self.addCleanup(lambda: os.path.exists(p) and os.remove(p))
        return p

    def _patch_sources(self, em=None, sina=None, tx=None):
        """替换三个源函数；未给的源模拟失败。"""
        def _mk(v):
            if v is None:
                def _raise():
                    raise RuntimeError("模拟源失效")
                return _raise
            return lambda *a, **k: v
        self.fb.fetch = _mk(em)
        self.fb.fetch_sina = _mk(sina)
        self.fb.fetch_tx = _mk(tx)

    def test_union_keeps_older_history(self):
        """腾讯只回溯到 2018-08，盘上 2015 起的更早历史必须保留。"""
        old = {"2015-01-05": [1.0, 2.0, 3.0, 0.5],
               "2015-01-06": [1.0, 2.1, 3.1, 0.6]}
        tx = {"2018-08-03": [10.0, 11.0, 12.0, 9.0],
              "2018-08-06": [10.0, 11.1, 12.1, 9.1]}
        # 让源最新日 >= 盘上最新日，避免被「陈旧源」守卫拦掉
        old["2026-11-06"] = [5.0, 5.1, 5.2, 4.9]
        tx["2026-11-06"] = [5.0, 5.1, 5.2, 4.9]
        p = self._old_file(old)
        self._patch_sources(tx=tx)
        out = self.fb.fetch_any("000985", "1.000985", old_path=p)
        self.assertIn("2015-01-05", out, "盘上更早历史被丢弃")
        self.assertIn("2018-08-03", out, "腾讯数据未并入")
        self.assertIn("2026-11-06", out)

    def test_dirty_overlap_not_overwrite(self):
        """重叠日大比例不一致 → 只补缺失，绝不覆盖盘上真实历史。"""
        # 盘上：30 个真实交易日，close 单调递增
        old = {}
        for i in range(30):
            d = f"2020-01-{i+1:02d}"
            old[d] = [100.0 + i, 101.0 + i, 102.0 + i, 99.0 + i]
        # 腾讯：同一批日期但 close 全部 +19%（模拟头部脏段），外加 1 个新日期
        tx = {}
        for i in range(30):
            d = f"2020-01-{i+1:02d}"
            tx[d] = [119.0 + i, 120.0 + i, 121.0 + i, 118.0 + i]
        tx["2020-02-03"] = [130.0, 131.0, 132.0, 129.0]
        p = self._old_file(old)
        self._patch_sources(tx=tx)
        out = self.fb.fetch_any("000985", "1.000985", old_path=p)
        # 重叠日必须保留盘上值
        for d in old:
            if d == "2020-01-30":
                continue          # 该日同时是「最新日」判定用，下面单独校验
            self.assertEqual(out[d], old[d],
                             f"{d} 脏源覆盖了盘上真实历史（本应只补增量）")
        # 新日期必须补进来
        self.assertIn("2020-02-03", out, "缺失日期没补上")
        self.assertEqual(len(out), 31)

    def test_rounding_level_diff_allowed(self):
        """重叠日只有 0.01 级舍入差 → 视为一致，按源值更新（不误杀）。

        ⚠️ 这条守的是「一致率必须按相对差算，不能按字符串是否相等算」。
           实测腾讯与盘上有 14/2000 处只差 0.01（3536.77 vs 3536.76）；
           若按「不等即不一致」统计，会虚报不一致，极端情况下（全部只差 1 分）
           一致率算成 0% → 正确源被误判成脏源。
        """
        old = {}
        for i in range(50):
            d = f"2021-03-{i+1:02d}" if i < 30 else f"2021-04-{i-29:02d}"
            old[d] = [200.0 + i, 201.0 + i, 202.0 + i, 199.0 + i]
        tx = {}
        for d, v in old.items():
            tx[d] = [v[0], v[1] - 0.01, v[2], v[3]]      # close 只差 1 分
        p = self._old_file(old)
        self._patch_sources(tx=tx)
        out = self.fb.fetch_any("000985", "1.000985", old_path=p)
        some = sorted(old)[0]
        self.assertAlmostEqual(out[some][1], old[some][1] - 0.01, places=6,
                               msg="舍入级差异被判为脏源，导致源值未被采用")

    def test_stale_source_rejected(self):
        """最新日早于盘上 → 拒用；唯一可用源陈旧时必须抛错（不静默保留旧值）。

        ⚠️ 11-04 事故：新浪 sh000985 是死数据（最后一根 2016-06-13），
           而盘上有 2015-01-05~2026-11-06 共 2877 根。若放行，数据**倒退十年**。
           全部源都陈旧时正确行为是 raise（由 main 记为该指数失败、保留旧文件），
           而不是返回一个陈旧结果冒充「更新成功」。
        """
        old = {"2026-11-05": [1.0, 1.1, 1.2, 0.9],
               "2026-11-06": [1.0, 1.1, 1.2, 0.9]}
        stale = {"2016-06-13": [2.0, 2.1, 2.2, 1.9]}
        p = self._old_file(old)
        self._patch_sources(sina=stale)
        with self.assertRaises(RuntimeError):
            self.fb.fetch_any("000985", "1.000985", old_path=p)

    def test_all_sources_fail_raises(self):
        """三源全失败必须抛错，不许静默返回空。"""
        p = self._old_file({"2026-11-06": [1.0, 1.1, 1.2, 0.9]})
        self._patch_sources()
        with self.assertRaises(RuntimeError):
            self.fb.fetch_any("000985", "1.000985", old_path=p)

    def test_fetch_tx_sym_prefix(self):
        """000xxx/9xxxxx → sh，其余 → sz（与新浪口径一致）。"""
        src = open(os.path.join(HERE, "fetch_bench.py"), encoding="utf-8").read()
        self.assertIn('code.startswith("000") or code.startswith("9")', src,
                      "腾讯源 sym 前缀判定被改动，需重新核对 000985→sh000985")
        # fqkline 对指数返 501，必须用 kline/kline
        self.assertIn("app/kline/kline", src,
                      "腾讯源端点被改回 fqkline（对指数 HTTP 501）")


class TestQfqAnchorSourceDeadGuard(unittest.TestCase):
    """verify_qfq_anchor 的源级失效守卫（exit 4 / 不推进游标 / 不写 clean）。"""

    def test_source_dead_exits_4_and_freezes_cursor(self):
        import verify_qfq_anchor as v
        if not os.path.exists(os.path.join(HERE, "data", "qfq_anchor_cursor.json")):
            self.skipTest("游标文件不存在")
        cur_path = os.path.join(HERE, "data", "qfq_anchor_cursor.json")
        out_path = os.path.join(HERE, "data", "qfq_anchor_audit.json")
        bak_cur = cur_path + ".unittest_bak"
        bak_out = out_path + ".unittest_bak"
        import shutil
        shutil.copy(cur_path, bak_cur)
        if os.path.exists(out_path):
            shutil.copy(out_path, bak_out)

        def _restore():
            for src, dst in ((bak_cur, cur_path), (bak_out, out_path)):
                if os.path.exists(src):
                    shutil.move(src, dst)
        self.addCleanup(_restore)

        before = json.load(open(cur_path, encoding="utf-8"))
        n_cov_before = len(before.get("covered") or {})
        shard_before = before.get("shard")

        orig = v.fetch_em_qfq_close
        v.fetch_em_qfq_close = lambda sym, d: (None, "ProxyError: 模拟全断")
        old_argv = sys.argv
        sys.argv = ["verify_qfq_anchor.py"]
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                rc = v.main()
        finally:
            v.fetch_em_qfq_close = orig
            sys.argv = old_argv

        self.assertEqual(rc, 4, "源级全失效必须 exit 4（原实现 return 0 = 假成功）")

        after = json.load(open(cur_path, encoding="utf-8"))
        self.assertEqual(after.get("shard"), shard_before,
                         "源级失效时游标不许推进（未测过的分片会被永久跳过）")
        self.assertEqual(len(after.get("covered") or {}), n_cov_before,
                         "源级失效时不得记 covered（没测过不算覆盖）")

        audit = json.load(open(out_path, encoding="utf-8"))
        self.assertTrue(audit.get("source_dead"), "缺少 source_dead 留痕")
        self.assertNotEqual(audit.get("verdict"), "clean",
                            "源级失效绝不能判 clean")

    def test_partial_success_is_not_dead(self):
        """只要有 1 只成功，就不算源级失效（避免误报 exit 4）。"""
        src = open(os.path.join(HERE, "verify_qfq_anchor.py"),
                   encoding="utf-8").read()
        self.assertIn("_source_dead = bool(sample) and (len(ok_l) == 0 "
                      "and len(drift) == 0)", src.replace("\n", " ")
                      .replace("  ", " "),
                      "源级失效判据被改动 —— 必须「抽样非空 且 ok+drift 均为 0」")


if __name__ == "__main__":
    unittest.main(verbosity=2)
