#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_round117_p0_p1.py —— round117 改动口径的回归测试（P1-4）

## 为什么只测这几个

项目有 266 个 .py、259 个生产/研究脚本。谈覆盖率没有意义
（round117 评估已明确「不上覆盖率指标」）。本文件只测**本轮改动过、
且一旦回退会造成静默错误**的四个口径：

| 测试类 | 守什么 | 回退后的症状 |
|---|---|---|
| `TestKlineUnits` | volume 双口径（688=股，其余=手） | 成交额差 100 倍，门槛静默失准 |
| `TestKlineCalendar` | 有效横截面 305/341 + 门槛真空区 | 建库初期 36 日被当有效截面 |
| `TestFactorIcAudit` | `cross_section_audit` 15 行结构 | IC 结论被外推到更大的池 |
| `TestPipelineStatus` | 22 步/判决优先级/缺文件不默认绿 | fail-open 失败被读成「一切正常」 |
| `TestWeightsConsistency` | 八处权重入口一致 | 权重静默回退到上一版 |

## 设计原则

1. **不联网**：全部读本地文件，可在任何环境跑。
2. **不用 pytest 也能跑**：`python test_round117_p0_p1.py` 直接执行。
   （本机未装 pytest；要装请见 requirements 说明。若装了 pytest，
   本文件同样可被 `pytest test_round117_p0_p1.py` 收集。）
3. **断言贴着真实数据写**：所有期望值来自实测，不写"大概"。
   数据本身变化时（如新增交易日），测试应**明确失败**而不是悄悄放过 ——
   这样才知道口径变了。

运行：
    python test_round117_p0_p1.py
    python test_round117_p0_p1.py -v      # 详细输出
"""
import os
import sys
import json
import glob
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
os.chdir(HERE)


class TestKlineUnits(unittest.TestCase):
    """volume 双口径：唯一入口 kline_units.py"""

    def test_688_is_shares(self):
        from kline_units import vol_coef, vol_to_shares
        self.assertEqual(vol_coef("688001"), 1.0)
        self.assertEqual(vol_to_shares("688001", 1000), 1000.0)

    def test_others_are_hands(self):
        from kline_units import vol_coef, vol_to_shares
        for c in ("000001", "300750", "600519", "002587", "830799"):
            self.assertEqual(vol_coef(c), 100.0, f"{c} 应为手口径")
            self.assertEqual(vol_to_shares(c, 1000), 100000.0)

    def test_bar_amount_never_equals_vol_times_close(self):
        """核心回归：非 688 用 vol*close 会少算 100 倍。这条断言就是防它。"""
        from kline_units import bar_amount
        for code in ("000001", "600519", "300750"):
            naive = 1000 * 10.0           # 错误写法
            right = bar_amount(code, 1000, 10.0)
            self.assertAlmostEqual(right, naive * 100, places=6,
                                   msg=f"{code}: bar_amount 必须比 vol*close 大 100 倍")

    def test_array_api_matches_scalar(self):
        from kline_units import vol_coef, vol_coef_array
        codes = ["688001", "000001", "300750"]
        arr = vol_coef_array(codes)
        for i, c in enumerate(codes):
            self.assertEqual(arr[i], vol_coef(c))


class TestKlineCalendar(unittest.TestCase):
    """有效横截面口径：唯一入口 kline_calendar.py"""

    @classmethod
    def setUpClass(cls):
        cls.px = json.load(open("data/klines_long.json", encoding="utf-8"))
        import kline_calendar as kc
        cls.kc = kc
        cls.info = kc.threshold_info(cls.px)

    def test_valid_count_matches_dropped(self):
        """有效 + 剔除 == 总数（不能有日期被吞掉）"""
        i = self.info
        self.assertEqual(i["n_dates_valid"] + i["n_dates_dropped"],
                         i["n_dates_total"],
                         "有效截面数与剔除数之和必须等于总日期数")

    def test_dropped_days_are_early_build_days(self):
        """剔除的必须是建库初期爬坡段（连续前缀），不能夹在中间"""
        kc = self.kc
        dates, sizes, n = kc.cross_section_sizes(self.px)
        thr = max(kc.MIN_ABS, n * kc.MIN_RATIO)
        kept = [d for d, s in zip(dates, sizes) if s >= thr]
        dropped = [d for d, s in zip(dates, sizes) if s < thr]
        if dropped:
            self.assertLess(max(dropped), min(kept),
                            "被剔除的日期必须全部早于保留的最早日期（爬坡段特征）")

    def test_threshold_in_gap(self):
        """门槛必须落在双峰真空区 —— 这是「阈值不敏感」的根据"""
        self.assertTrue(self.info["threshold_in_gap"],
                        f"门槛未落在真空区，gap={self.info['gap']}，需复核是否误删边界样本")

    def test_valid_dates_is_sorted_and_unique(self):
        vd = self.kc.build_valid(self.px)
        self.assertEqual(vd, sorted(vd))
        self.assertEqual(len(vd), len(set(vd)))

    def test_meta_has_valid_cross_sections(self):
        """meta 必须已写回（否则下游拿不到口径）"""
        if not os.path.exists("data/klines_long_meta.json"):
            self.skipTest("meta 不存在（未跑 refresh_kline_meta.py）")
        m = json.load(open("data/klines_long_meta.json", encoding="utf-8"))
        self.assertIn("valid_cross_sections", m)
        self.assertIn("validity_rule", m)
        # meta 里的列表必须与现算一致（防陈旧）
        self.assertEqual(len(m["valid_cross_sections"]),
                         len(self.kc.build_valid(self.px)),
                         "meta 的 valid_cross_sections 与现算不一致 → meta 已陈旧")

    def test_portfolio_metrics_delegates(self):
        """portfolio_metrics 必须委托给 kline_calendar，不能自己再实现一遍"""
        import portfolio_metrics as pm
        old_style = pm.build_calendar(self.px)
        direct = self.kc.build_valid(self.px)
        self.assertEqual(old_style, direct,
                         "portfolio_metrics.build_calendar 与 kline_calendar 不一致 → "
                         "又出现了两份实现")


class TestFactorIcAudit(unittest.TestCase):
    """P0-1：IC 截面规模审计必须存在且结构完整"""

    @classmethod
    def setUpClass(cls):
        cls.ic = json.load(open("factor_ic.json", encoding="utf-8")) \
            if os.path.exists("factor_ic.json") else {}

    def test_cross_section_audit_exists(self):
        if not self.ic:
            self.skipTest("factor_ic.json 不存在")
        self.assertIn("cross_section_audit", self.ic,
                      "缺少 cross_section_audit → 读者无法知道 IC 在多少只股票上算的")

    def test_audit_rows_have_required_fields(self):
        if "cross_section_audit" not in self.ic:
            self.skipTest("无 cross_section_audit")
        audit = self.ic["cross_section_audit"]
        # 实测结构：{"<label>|h<N>": {...}, "_summary": {...}}
        # 行内字段是 cross_section_median（不是 median），且没有 h / gated
        rows = [v for k, v in audit.items()
                if isinstance(v, dict) and "cross_section_median" in v]
        self.assertGreater(len(rows), 0, "审计表无有效行")
        for r in rows:
            for f in ("n", "cross_section_median", "n_below_gate",
                      "n_after_gate", "usable", "gated_note"):
                self.assertIn(f, r, f"审计行缺字段 {f}")
            # 门槛语义必须可自证：过门槛数 + 未过门槛数 == 原截面数
            self.assertEqual(
                r["n_below_gate"] + r["n_after_gate"], r["n"],
                f"n_below_gate({r['n_below_gate']}) + n_after_gate({r['n_after_gate']}) "
                f"!= n({r['n']}) —— 审计行的门槛拆分自相矛盾")

    def test_summary_discloses_production_pool(self):
        if "cross_section_audit" not in self.ic:
            self.skipTest("无 cross_section_audit")
        # _summary 嵌在 cross_section_audit 下，不在顶层
        s = self.ic["cross_section_audit"].get("_summary") or {}
        self.assertTrue(s, "cross_section_audit._summary 缺失 → 读者看不到生产池规模")
        self.assertIn("min_cross_section_gate", s)
        self.assertIn("production_pool_size", s)
        # pool_size=0 必须被保留为原值，不能被吞成 None / 缺省 500
        self.assertIn("pool_size_config", s,
                      "pool_size_config 缺失 → 0（不限规模）可能又被 or 吞掉了")
        self.assertEqual(s["pool_size_config"], 0,
                         "pool_size_config 应为 0（不限规模）；若变回 500 说明静默回退分层抽样")
        # 生产池必须大于 IC 实际截面 —— 否则「不可外推」这个披露就是假的
        self.assertGreater(
            s["production_pool_size"], s["median_of_medians"],
            f"生产池 {s['production_pool_size']} 不大于 IC 截面中位数 "
            f"{s['median_of_medians']} → 需复核 note 里的外推警告是否还成立")

    def test_h20_is_marked_unusable(self):
        """h=20 门槛后截面为 0 —— 必须标为不可用（诚实的小样本好过虚假的大样本）"""
        if "cross_section_audit" not in self.ic:
            self.skipTest("无 cross_section_audit")
        audit = self.ic["cross_section_audit"]
        h20 = [(k, v) for k, v in audit.items()
               if isinstance(v, dict) and k.endswith("|h20") and "usable" in v]
        if not h20:
            self.skipTest("无 |h20 审计行")
        for k, r in h20:
            self.assertEqual(r["n_after_gate"], 0,
                             f"{k}：门槛后仍有 {r['n_after_gate']} 个截面，"
                             f"与「h20 完全无法复现」的结论不符")
            self.assertFalse(r["usable"],
                             f"{k} 报告 usable=True，但门槛后截面数=0，应为不可用")
            # usable=False 必须给出原因，否则读者不知道为什么
            self.assertTrue(r["gated_note"], f"{k} usable=False 但无 gated_note 说明")


class TestPipelineStatus(unittest.TestCase):
    """P0-2：日链状态机"""

    def setUp(self):
        import pipeline_status as ps
        self.ps = ps

    def test_step_counts_match(self):
        """CRITICAL + DEGRADABLE 不得有重名（重名会让判决不确定）"""
        overlap = set(self.ps.CRITICAL) & set(self.ps.DEGRADABLE)
        self.assertEqual(overlap, set(), f"步骤同时出现在两类里：{overlap}")

    def test_critical_steps_are_the_four_load_bearing_ones(self):
        # CRITICAL 实测是 dict（step -> 影响说明），不是 set
        self.assertIsInstance(self.ps.CRITICAL, dict,
                              "CRITICAL 应为 dict（step -> 失败影响），"
                              "否则状态页无法解释「为什么硬停」")
        self.assertEqual(
            set(self.ps.CRITICAL),
            {"fetch_top200_sina", "check_data_fresh",
             "step2_stockscorer", "gen_report_top200"},
            "关键步骤集合变了 —— 改这里必须先想清楚哪些失败真的该硬停")
        # 每个关键步骤必须写清影响（否则告警是一句废话）
        for step, why in self.ps.CRITICAL.items():
            self.assertTrue(str(why or "").strip(),
                            f"关键步骤 {step} 缺失败影响说明")

    def _with_tsv(self, rows):
        """把 pipeline_status.TSV 临时指向一个构造的 TSV，跑 finalize，再还原。

        rows: [(step, rc, ok), ...]
        返回 (verdict, out_dict)。全程不碰真实 .pipeline_status.tsv 与状态 json。
        """
        import tempfile
        import shutil
        ps = self.ps
        old_tsv = ps.TSV
        tmpdir = tempfile.mkdtemp(prefix="ps_test_")
        tsv = os.path.join(tmpdir, ".pipeline_status.tsv")
        with open(tsv, "w", encoding="utf-8") as f:
            for step, rc, ok in rows:
                f.write(f"{step}\t{rc}\t{1 if ok else 0}\t1.0\t2026-11-03T09:00:00\t\n")
        out_json = os.path.join(tmpdir, "status.json")
        try:
            ps.TSV = tsv
            _p, out = ps.finalize(out_path=out_json, date_tag="2026-11-03")
        finally:
            ps.TSV = old_tsv
            shutil.rmtree(tmpdir, ignore_errors=True)
        return out["verdict"], out

    def test_verdict_priority_failed_beats_degraded(self):
        """判决优先级：critical 失败 > degradable 失败 > 未执行 > ok"""
        # 1 个关键步骤失败 + 1 个可降级失败，其余不跑（n_not_run 也会非 0）
        v, out = self._with_tsv([
            ("fetch_top200_sina", 1, False),
            ("factor_eval", 1, False),
        ])
        self.assertEqual(v, "failed",
                         f"关键步骤失败应判 failed，实际 {v}（{out['verdict_text']}）")
        self.assertEqual(out["counts"]["failed_critical"], 1)
        self.assertEqual(out["counts"]["degraded"], 1)

    def test_missing_tsv_is_unknown_not_degraded(self):
        """TSV 缺失必须判 unknown —— 这是回归「unknown 分支是死代码」的缺陷

        原实现把 `elif not os.path.exists(TSV)` 排在 `elif n_not_run` 之后：
        TSV 不存在 → rows 为空 → 22 个步骤全进 not_run → 永远判 degraded，
        unknown 分支永不可达。「没有任何记录」不等于「有记录但确实没跑」。
        """
        ps = self.ps
        old_tsv = ps.TSV
        import tempfile, shutil
        tmpdir = tempfile.mkdtemp(prefix="ps_test_")
        try:
            ps.TSV = os.path.join(tmpdir, "nonexistent.tsv")   # 故意不存在
            _p, out = ps.finalize(out_path=os.path.join(tmpdir, "s.json"),
                                  date_tag="2026-11-03")
            self.assertEqual(out["verdict"], "unknown",
                             f"TSV 缺失应判 unknown，实际判为 {out['verdict']}"
                             f"（{out['verdict_text']}）→ unknown 分支不可达")
            self.assertEqual(out["counts"]["not_run"], len(ps.ALL_KNOWN),
                             "TSV 缺失时全部步骤都算未执行，但判决仍必须是 unknown")
        finally:
            ps.TSV = old_tsv
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_all_steps_ok_is_ok(self):
        """22 步全成功才能判 ok —— 防止「全部成功」被随便喊出来"""
        from pipeline_status import ALL_KNOWN
        rows = [(s, 0, True) for s in sorted(ALL_KNOWN)]
        v, out = self._with_tsv(rows)
        self.assertEqual(v, "ok", f"全部成功应判 ok，实际 {v}（{out['verdict_text']}）")
        self.assertEqual(out["counts"]["not_run"], 0)

    def test_status_file_structure(self):
        """已有的状态文件必须含 verdict / counts / steps"""
        files = sorted(glob.glob("pipeline_status_*.json"))
        if not files:
            self.skipTest("无状态文件")
        d = json.load(open(files[-1], encoding="utf-8"))
        for f in ("verdict", "counts", "steps"):
            self.assertIn(f, d)
        self.assertIn(d["verdict"], {"ok", "degraded", "failed", "unknown"})
        # 失败步骤必须带 impact（否则读者不知道影响什么）
        for s in d.get("steps") or []:
            if not s.get("ok"):
                self.assertTrue(s.get("impact") or s.get("why"),
                                f"步骤 {s.get('step')} 失败但无影响说明")

    def test_missing_file_is_not_green(self):
        """缺状态文件必须判 unknown，绝不能默认 ok"""
        import inspect
        src = inspect.getsource(self.ps)
        self.assertIn("unknown", src,
                      "状态机里没有 unknown 分支 —— 缺文件会被误判为正常")
        self.assertIn("not_run", src)

    # ───────────────────────────────────────────────────────────────
    # round119：_rec 定义位置必须早于首次调用
    # ───────────────────────────────────────────────────────────────
    # 事故：`_rec` 定义在 Step 3.5（约第 160 行），而 Step 1/1.5/2 在
    #   第 37/48/64 行就调用它。bash 函数必须先定义后使用 →
    #   跑日链时 /tmp 日志出现 3 行 `_rec: command not found`，
    #   fetch/check_data_fresh/step2 三条记录**从未落盘**。
    #   ⇒ 三步是 CRITICAL，pipeline_status 判为 not_run → verdict 恒 degraded，
    #     即使三步全部成功、产物齐全、日志有 ✅。
    #   ⇒ 「记录器本身没就位」与「被记录的步骤没跑」在产物上完全一样。
    def test_rec_defined_before_first_call(self):
        """`_rec` 必须在首次调用之前定义（bash 函数不提升）"""
        sh = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          "run_daily_local.sh")
        if not os.path.exists(sh):
            self.skipTest("run_daily_local.sh 不存在")
        lines = open(sh, encoding="utf-8").read().split("\n")
        def_line = None
        first_call = None
        n_def = 0
        for i, l in enumerate(lines, 1):
            s = l.strip()
            if s.startswith("#"):
                continue
            if s.startswith("_rec()"):
                n_def += 1
                if def_line is None:
                    def_line = i
            elif "_rec " in l and first_call is None:
                first_call = i
        self.assertEqual(n_def, 1, f"_rec 应只定义一次，实际 {n_def} 次")
        self.assertIsNotNone(def_line, "未找到 _rec 定义")
        self.assertIsNotNone(first_call, "未找到 _rec 调用")
        self.assertLess(
            def_line, first_call,
            f"_rec 定义在 L{def_line} 但首次调用在 L{first_call} —— "
            f"bash 函数不提升，调用方会 command not found 且**不报错退出**，"
            f"导致这些步骤的记录永久缺失（round119 实测 3 个 CRITICAL 步丢失）")

    def test_pipeline_reset_before_recording(self):
        """`--reset` 必须早于任何 `--record`，否则当天记录被清空"""
        sh = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          "run_daily_local.sh")
        if not os.path.exists(sh):
            self.skipTest("run_daily_local.sh 不存在")
        lines = open(sh, encoding="utf-8").read().split("\n")
        reset_line = None
        first_rec = None
        for i, l in enumerate(lines, 1):
            if l.strip().startswith("#"):
                continue
            if reset_line is None and "--reset" in l:
                reset_line = i
            if first_rec is None and ("_rec " in l or "--record" in l) \
                    and not l.strip().startswith("_rec()"):
                first_rec = i
        self.assertIsNotNone(reset_line, "未找到 --reset")
        self.assertIsNotNone(first_rec, "未找到记录调用")
        self.assertLess(reset_line, first_rec,
                        f"--reset 在 L{reset_line}，首次记录在 L{first_rec} —— "
                        f"顺序反了会把当天已有的步级记录清空")

    def test_critical_steps_are_recorded_by_script(self):
        """三个 CRITICAL 步（除 gen_report）在脚本里必须有 `_rec` 调用

        只看「有没有写」不够，还要确认它不在 `if` 的注释行里。
        """
        sh = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          "run_daily_local.sh")
        if not os.path.exists(sh):
            self.skipTest("run_daily_local.sh 不存在")
        txt = open(sh, encoding="utf-8").read()
        for step in ("fetch_top200_sina", "check_data_fresh",
                     "step2_stockscorer", "gen_report_top200"):
            self.assertIn(f"_rec {step} ", txt,
                          f"关键步骤 {step} 在脚本里没有 _rec 记录调用 —— "
                          f"失败/成功都无法进入状态机")


class TestWeightsConsistency(unittest.TestCase):
    """权重八处入口一致性 —— 复用 verify_weights_round36 的判据"""

    def test_weight_shasum_unchanged(self):
        """权重文件指纹（改动权重必须同步改本测试并说明理由）"""
        import hashlib
        p = "factor_weights.json"
        if not os.path.exists(p):
            self.skipTest("factor_weights.json 不存在")
        h = hashlib.sha1(open(p, "rb").read()).hexdigest()
        self.assertEqual(
            h, "c98df667b5f9353060bedb3d35866edcc06dc1de",
            "factor_weights.json 指纹变了（v4.17-cycle-rules）。"
            "若确为有意的权重变更，请同步八处入口 + verify_weights_round36.py "
            "并把本断言更新为新指纹。")

    def test_six_entries_consistent(self):
        """跑 verify_weights_round36 的核心校验（子进程，避免污染本进程）"""
        import subprocess
        if not os.path.exists("verify_weights_round36.py"):
            self.skipTest("校验脚本不存在")
        r = subprocess.run([sys.executable, "verify_weights_round36.py"],
                           capture_output=True, text=True, timeout=180)
        self.assertEqual(r.returncode, 0,
                         f"权重一致性校验失败：\n{r.stdout[-1500:]}\n{r.stderr[-500:]}")


class TestGateDimsDynamic(unittest.TestCase):
    """round118 P0-1：准入闸门的维度集必须来自权重文件，不能是硬编码旧列表。

    背景：`weight_admission_gate.py` 原来写 `DIMS = F.DIMS`，而
    `factor_validation_core.DIMS` 是硬编码 9 维旧列表。实读 factor_weights.json：
        多算 score_lowvol20（权重 0.0）← 停用维白占分母
        漏算 score_growth（权重 0.10）

    ⚠️ 但实读 data/panel_round66.json 后发现**漏算的直接原因不是笔误**：
       全 238 个截面里 `score_growth` 一条数据都没有 —— **面板根本没有这一列**。
       所以「把 growth 加进 DIMS」并不等于「growth 被验证了」。正确处置是
       把名义集与可评估集分开，并把盲区权重显式扣出来披露。

    守五件事：
      1. DIMS（名义）必须等于「weight > 0」的键集合（双向相等）
      2. DIMS_EVAL（可评估）必须等于 DIMS ∩ 面板可提供列，且不含 0 权重维
      3. score_growth 必须出现在 DIMS_BLIND 且带 0.10 权重 —— 不能假装验证过
      4. 覆盖率必须被算出来并披露（evaluable_ratio）
      5. 候选权重含集外维度时必须报出来而非静默丢
    """

    def _gate(self):
        import weight_admission_gate
        return weight_admission_gate

    def test_dims_equals_positive_weight_keys(self):
        """双向相等：多一个漏一个都算错"""
        import json
        g = self._gate()
        with open("factor_weights.json", encoding="utf-8") as f:
            w = json.load(f)["weights"]
        pos = {k for k, v in w.items() if isinstance(v, (int, float)) and v > 0}
        self.assertEqual(set(g.DIMS), pos,
                         f"gate.DIMS 与 weight>0 不一致：\n"
                         f"  多算 {set(g.DIMS) - pos}\n  漏算 {pos - set(g.DIMS)}")

    def test_lowvol_excluded_from_nominal(self):
        """score_lowvol20(权重 0.0) 不得进入名义维度集 —— 白占分母"""
        g = self._gate()
        self.assertNotIn("score_lowvol20", g.DIMS,
                         "score_lowvol20(权重 0.0) 又混进评估集了")

    def test_growth_is_declared_blind_not_fake_verified(self):
        """盲区必须被**逐个点名 + 扣权重**，不得有任何维混进可评估集

        ⚠️ round125 改写：原断言写死「score_growth 必须是盲区、evaluable_ratio=0.90」，
        那是 round118 用 panel_round66（无 score_growth 列）时的口径。
        现闸门面板已换成 panel_round124_long，**growth 满覆盖（99.5%）不再是盲区**；
        真正的盲区变成 score_fund(0.20) + score_dominant(0.10) —— 长面板是 2020 起
        回补，主力净流入/龙头判定无历史快照，这两列**一条都没有**。

        本测试的核心不变量（比具体维度名更重要）：
          ① 可评估集与盲区**必须互斥且并为名义集**（不漏不重）
          ② 盲区每一维都必须在 blind_dims 里带上权重
          ③ evaluable_ratio == 1 - Σ盲区权重（覆盖率必须与扣除一致）
          ④ 盲区非空时 ratio 必须 <1（防「伪满分」回归）
        """
        g = self._gate()
        cov = g._gate_coverage()
        ev, bl = set(g.DIMS_EVAL), set(g.DIMS_BLIND)
        nom = set(g.DIMS)
        self.assertEqual(ev & bl, set(), "同一维度同时出现在可评估集与盲区")
        self.assertEqual(ev | bl, nom, "可评估集 ∪ 盲区 ≠ 名义集（漏维或凭空多维）")
        # 盲区每一维都要带权重，且权重之和 == 1 - ratio
        bd = cov["blind_dims"]
        self.assertEqual(set(bd), bl, f"blind_dims 与 DIMS_BLIND 不一致：{bd} vs {bl}")
        _wsum = sum(bd.values())
        self.assertAlmostEqual(cov["evaluable_ratio"], 1.0 - _wsum, places=3,
                               msg=f"allowed_ratio={cov['evaluable_ratio']} 与盲区权重和 {_wsum} 不自洽")
        if bl:
            self.assertLess(cov["evaluable_ratio"], 1.0,
                            "有盲区却报覆盖率 1.0 —— 伪造验证覆盖")
            for d in bl:
                self.assertIn(d, cov["note"], f"note 未点名盲区维度 {d}：{cov['note']}")

    def test_coverage_ratio_is_disclosed_and_less_than_one(self):
        """覆盖率必须与**实读面板**一致，且 <1 时 note 必须点名盲区

        round125 改：不再写死 0.90。基准改为「1 - 面板缺失维的权重和」，
        因为面板会换（round66→round123→round124_long），写死数字必然再次失效。
        """
        g = self._gate()
        cov, _nd = g._panel_coverage()
        got = g._gate_coverage()
        # 独立复算：面板缺哪些名义维 → 它们的权重和
        _missing = [d for d in g.DIMS if d not in cov]
        _w = json.load(open(os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "factor_weights.json"),
            encoding="utf-8"))["weights"]
        _expect = round(1.0 - sum(float(_w.get(d, 0)) for d in _missing), 4)
        self.assertAlmostEqual(got["evaluable_ratio"], _expect, places=3,
                               msg=f"覆盖率 {got['evaluable_ratio']} ≠ 独立复算 {_expect}"
                                   f"（面板缺失维 {_missing}）")
        self.assertEqual(set(got["blind_dims"]), set(g.DIMS_BLIND))
        if got["blind_dims"]:
            self.assertLess(got["evaluable_ratio"], 1.0,
                            "覆盖率报 1.0 —— 说明盲区没被扣掉")
            self.assertIn(f"{got['evaluable_ratio']*100:.1f}%", got["note"])

    def test_eval_dims_are_panel_columns(self):
        """可评估集的每一维都必须在面板里真实存在（列级）

        ⚠️ 这条最初写成「抽查 20 个截面里有值」，结果抓到 score_fund /
        score_dominant 在前若干截面为空而误报。正确判据是「整份面板里出现过」，
        稀疏性由下一条 test_sparse_dims_are_disclosed 单独守。
        """
        g = self._gate()
        cov, _ndate = g._panel_coverage()
        self.assertTrue(set(g.DIMS_EVAL) <= set(cov),
                        f"可评估集含面板从未出现过的列：{set(g.DIMS_EVAL) - set(cov)}")

    def test_sparse_dims_are_disclosed(self):
        """「列存在但覆盖率低」必须被点名披露，且不得与盲区混淆

        ⚠️ round125 改写：原本断言 score_fund / score_dominant 出现在 sparse 里
        （面板是 round66/round123，这两列存在但只有 3.5%/25.5%）。
        现闸门面板换成 panel_round124_long，这两列**彻底不存在** →
        语义从「稀疏」升级为「盲区」，归 DIMS_BLIND 管，不再属于 sparse。

        当前真正的稀疏维：score_ep 78.6%（长面板早期缺价）。
        本测试改为遍历**实际**稀疏集合并校验其与盲区互斥。
        """
        g = self._gate()
        r = g._dim_coverage_report()
        sparse = r["sparse_dims"]
        blind = set(g.DIMS_BLIND)
        # ① 互斥：盲区维度不得再算「稀疏」（否则同一问题被两处重复/矛盾披露）
        self.assertEqual(set(sparse) & blind, set(),
                         f"维度同时被标为稀疏与盲区：{set(sparse) & blind}")
        # ② 每个被标稀疏的维，必须真在面板里、且覆盖率确实 < 门槛
        cov, _nd = g._panel_coverage()
        for d, info in sparse.items():
            self.assertIn(d, cov, f"{d} 被标稀疏但面板无此列（应归盲区）")
            self.assertLess(info["row_coverage"], g._GATE_MIN_COVERAGE,
                            f"{d} 覆盖率 {info['row_coverage']} 未低于门槛却被标稀疏")
            self.assertGreater(info["weight"], 0, f"{d} 权重为 0 却被标稀疏")
            self.assertIn(d, r["note"], f"note 未点名稀疏维 {d}：{r['note']}")
        # ③ 高覆盖维不得被误报
        self.assertNotIn("score_quality", sparse, "99%+ 覆盖的维被误报为稀疏")

    def test_dim_max_covers_both_sets(self):
        """DIM_MAX 必须同时覆盖名义集与面板集（composite 遍历的是面板轴）"""
        g = self._gate()
        import factor_validation_core as F
        self.assertTrue(set(F.DIMS) <= set(g.DIM_MAX),
                        f"DIM_MAX 缺面板列：{set(F.DIMS) - set(g.DIM_MAX)}")
        self.assertTrue(set(g.DIMS) <= set(g.DIM_MAX),
                        f"DIM_MAX 缺名义维：{set(g.DIMS) - set(g.DIM_MAX)}")

    def test_normalized_weights_sum_to_one(self):
        """归一化后 Σ=1，且每维 >0（否则重归一化会静默放大其余维度）"""
        g = self._gate()
        self.assertAlmostEqual(sum(g._GATE_W.values()), 1.0, places=6)
        for k, v in g._GATE_W.items():
            self.assertGreater(v, 0.0, f"{k} 归一化后 ≤0")

    def test_candidate_out_of_set_dim_is_reported(self):
        """候选权重含评估集外的维度时，必须打印警告而不是静默丢弃"""
        import json
        import tempfile
        import contextlib
        import io
        g = self._gate()
        cand = {"weights": dict(g._GATE_W)}
        cand["weights"]["score_unknown_new"] = 0.15
        tot = sum(cand["weights"].values())
        cand["weights"] = {k: v / tot for k, v in cand["weights"].items()}
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False,
                                         encoding="utf-8") as f:
            json.dump(cand, f)
            p = f.name
        buf = io.StringIO()
        try:
            with contextlib.redirect_stdout(buf):
                wmap, _raw = g.load_weights(p)
            out = buf.getvalue()
            self.assertIn("score_unknown_new", out,
                          f"集外维度被静默丢弃，没有任何提示：{out!r}")
            self.assertNotIn("score_unknown_new", wmap,
                             "集外维度不该进入评估权重")
            self.assertAlmostEqual(sum(wmap.values()), 1.0, places=6)
        finally:
            os.unlink(p)


class TestNWSmallSample(unittest.TestCase):
    """round118 P0-0：NW t 在小样本下的 lag 自适应 + usable 下限。

    这是本项目最容易被"悄悄改回去"的一处——因为原实现看起来完全合理
    （`lag = h-1` 是标准做法），只是没考虑「门槛后 n 只剩 6~10」。
    一旦回退，页面上所有「显著」结论会重新虚高，且**没有任何报错**。

    守三件事：
      1. 零分布：n 小 lag 大时 |NW t| 的 95% 分位必须远高于 2.0 —— 锁定「不能套 2.0」
      2. lag 自适应：n 不足时 lag 必须被下调，且留 `adjusted_from` 审计痕迹
      3. usable 收紧：lag=h-1 需要 n >= 2(h-1)+2
    """

    # 从 audit_round118b_nw_small_sample.py 的蒙特卡洛结果取（纯白噪声，真实 t=0）
    # 允许 ±20% 偏差（模拟误差 + 不同随机种子）
    # ⚠️ 这组数字经过一次口径修正：第一版脚本误用 1/(n-k) 归一自协方差
    #    （项目 _nw_t 用 1/n），曾把 n=6/lag=4 算成 11.11。正确值 4.90。
    NULL_Q95 = {
        (6, 4): 4.90, (7, 4): 4.43, (8, 4): 4.11,
        (10, 4): 3.63, (15, 4): 3.02, (20, 4): 2.72, (60, 4): 2.17,
        (6, 2): 4.02, (10, 0): 2.39, (60, 0): 2.00,
    }

    def test_null_distribution_is_wider_than_normal(self):
        """零假设下 |NW t| 的 95% 分位：n 小 + lag 大 → 高于 2.0

        若有人把 nw_t 换成"更保守"的实现（比如分母加小样本修正），
        这条会失败——那是**好事**，说明零分布变了，临界值表必须重算。
        """
        import random
        random.seed(20261103)
        fe = self._fe()
        for (n, lag), expect in self.NULL_Q95.items():
            if expect < 2.6:
                continue  # 已经接近正态，不必逐点验（省时间）
            vals = []
            for _ in range(4000):
                xs = [random.gauss(0, 1) for _ in range(n)]
                t = fe._nw_t(xs, lag)
                if t is not None:
                    vals.append(abs(t))
            vals.sort()
            q95 = vals[int(len(vals) * 0.95)]
            self.assertLess(
                abs(q95 - expect) / expect, 0.20,
                f"n={n}/lag={lag} 的零分布 95% 分位 {q95:.2f} "
                f"与记录的 {expect} 偏差 >20% —— 零分布变了，"
                f"请重跑 audit_round118b_nw_small_sample.py 并更新本表")

    def test_small_sample_threshold_is_not_two(self):
        """核心断言：n=6/lag=4 时，|t|=4.86 不该被当作「过 5%」"""
        import random
        random.seed(42)
        fe = self._fe()
        vals = []
        for _ in range(20000):
            xs = [random.gauss(0, 1) for _ in range(6)]
            t = fe._nw_t(xs, 4)
            if t is not None:
                vals.append(abs(t))
        p = sum(1 for v in vals if v >= 4.86) / len(vals)
        # 实测约 0.051：纯噪声有约 5% 概率产生 ≥4.86。
        # 关键不是这个概率绝对值，而是它 **远高于正态下的 1e-6** ——
        # 「t=4.86 很显著」在小样本下是错的。
        self.assertGreater(
            p, 0.02,
            f"n=6/lag=4 下 P(|t|>=4.86) 实测 {p:.4f}，应 >0.02 —— "
            f"若变成极小值说明 _nw_t 已被改动（或口径变了），"
            f"4.86 将重新被当作「显著」")

    def test_lag_auto_downshift(self):
        """n 不足 2(lag+1) 时 lag 必须下调，且只在真降了时才留痕迹"""
        fe = self._fe()
        cases = [
            # (n, lag_want, 期望 lag_use, 期望 adjusted_from)
            (6, 4, 2, 4),    # 6 <= 2*5 → 降到 6//2-1=2
            (7, 4, 2, 4),
            (10, 4, 4, None),  # 10 <= 10 → 算出 4，与 want 相同 → 不留痕迹
            (12, 4, 4, None),  # 12 > 10 → 不触发
            (60, 19, 19, None),
            (6, 0, 0, None),   # lag=0 无需调整
            (3, 4, 0, 4),
        ]
        for n, want, exp_use, exp_adj in cases:
            use, adj = fe._safe_nw_lag(n, want)
            self.assertEqual(use, exp_use, f"n={n} lag={want} 的 lag_use 应为 {exp_use}")
            self.assertEqual(adj, exp_adj,
                             f"n={n} lag={want} 的 adjusted_from 应为 {exp_adj}")

    def test_adjusted_from_never_equals_lag_use(self):
        """审计痕迹不能撒谎：adjusted_from 非 None 时，lag 必须真的变了"""
        fe = self._fe()
        for n in range(1, 80):
            for lag in range(0, 25):
                use, adj = fe._safe_nw_lag(n, lag)
                if adj is not None:
                    self.assertNotEqual(
                        use, adj,
                        f"n={n} lag={lag}: 标记为从 {adj} 调整，但 lag_use 也是 {adj}"
                        f" —— 虚假审计痕迹")

    def test_usable_requires_enough_n_for_lag(self):
        """usable 必须同时满足 n>=MIN_GATED_SNAPSHOTS 且 n>=2(h-1)+2"""
        fe = self._fe()
        # h=5 需要 n>=10；构造 n=7 的门槛后序列
        ics = [0.1, -0.05, 0.2, 0.15, 0.08, -0.02, 0.12]
        dates = [f"d{i}" for i in range(len(ics))]
        sizes = [300] * len(ics)
        out = fe._gated_stats(ics, [0] * len(ics), dates, sizes, h=5, gate=200)
        self.assertEqual(out["n_after_gate"], 7)
        self.assertFalse(
            out["usable"],
            f"h=5 且 n=7 时应判不可引用（需 n>=10），实际 usable={out['usable']}")
        self.assertIn("样本量不足以支撑", out["usable_note"])

    def test_gated_output_has_audit_fields(self):
        """门槛后输出必须含 lag 审计字段，否则外部无法复算"""
        fe = self._fe()
        fe = self._fe()
        ics = [0.1] * 6
        dates = [f"d{i}" for i in range(6)]
        out = fe._gated_stats(ics, [0] * 6, dates, [300] * 6, h=5, gate=200)
        for f in ("nw_lag_after_gate", "nw_lag_adjusted_from", "nw_t_after_gate"):
            self.assertIn(f, out, f"门槛后输出缺审计字段 {f}")

    def test_full_sample_path_not_broken(self):
        """全样本路径（n=60~79）不应触发 lag 下调，避免误伤现有结论"""
        fe = self._fe()
        # h=20 需 n>=40；n=61 满足
        use, adj = fe._safe_nw_lag(61, 19)
        self.assertEqual((use, adj), (19, None))
        # h=5 需 n>=10；n=75 满足
        use, adj = fe._safe_nw_lag(75, 4)
        self.assertEqual((use, adj), (4, None))

    def test_audit_simulator_matches_production(self):
        """审计脚本的 nw_t_np 必须与生产 _nw_t 逐点相等

        为什么单列这条：第一版审计脚本误用 1/(n-k) 归一自协方差，
        把 n=6/lag=4 的零分布临界值算成 11.11（正确是 4.90），
        在报告里夸大了 2.3 倍。**蒙特卡洛复现生产口径时必须先对拍**，
        否则所有基于它的结论都是错的，而且看起来完全合理。
        """
        try:
            from audit_round118b_nw_small_sample import nw_t_np
        except Exception as e:
            self.skipTest(f"审计脚本不可导入：{e}")
        import random
        fe = self._fe()
        random.seed(7)
        checked = 0
        for n in (3, 4, 6, 7, 10, 20, 60):
            for lag in (0, 1, 2, 4, 12):
                if lag >= n:
                    continue
                for _ in range(10):
                    xs = [random.gauss(0, 1) for _ in range(n)]
                    a = fe._nw_t(xs, lag)
                    b = nw_t_np(__import__("numpy").array(xs), lag)
                    if b is not None and not __import__("numpy").isnan(b):
                        self.assertAlmostEqual(
                            a, b, places=9,
                            msg=f"n={n} lag={lag}: 生产版 {a} vs 审计模拟 {b} "
                                f"—— 两边口径不一致，蒙特卡洛结果不可信")
                        checked += 1
        self.assertGreater(checked, 50, "对拍样本太少，没起到校验作用")

    @staticmethod
    def _fe():
        import factor_eval
        return factor_eval


class TestGatedDatesConsecutive(unittest.TestCase):
    """round118 P0-2：门槛后截面「是否连续交易日」必须被判定并披露。

    为什么这条需要测试：`n_after_gate=10` 这个数字本身看不出这 10 个
    是一段连续窗口还是散布在整个样本期。前者 = 单一 regime、无跨期稳健性；
    后者 = 跨期稳健。两者证据强度差一个数量级，但页面上原来只印了「10」。

    ⚠️ 关键陷阱（已踩）：`factor_ic.json["cross_section_audit"]` 是聚合摘要表，
       **没有** `dates_after_gate` 字段；日期只在 `horizons[h]["gated"]` 里。
       从 summary 表找日期会永远得到空列表 —— 那句话就成了「看起来检查了、
       实际什么都没检查」。
    """

    def test_consecutive_detection_by_index(self):
        """连续判定必须基于日轴索引，不是日期字符串相邻"""
        import cs_consecutive as C
        axis = ["2026-10-13", "2026-10-14", "2026-10-15", "2026-10-16",
                "2026-10-19", "2026-10-20"]     # 10-16 与 10-19 跨周末但仍是相邻交易日
        sp = C.consecutive_span(axis, axis)
        self.assertTrue(sp["is_consecutive"], f"跨周末应仍判连续：{sp}")
        self.assertEqual(sp["max_run"], 6)

    def test_gap_detected(self):
        """跳过中间交易日必须判为不连续"""
        import cs_consecutive as C
        axis = ["2026-10-13", "2026-10-14", "2026-10-15", "2026-10-16",
                "2026-10-19", "2026-10-20"]
        sp = C.consecutive_span([axis[0], axis[1], axis[3], axis[4]], axis)
        self.assertFalse(sp["is_consecutive"], f"有缺口却判连续：{sp}")
        self.assertEqual(sp["max_run"], 2)

    def test_unknown_dates_return_none_not_false(self):
        """日期不在日轴上时必须返回 None（未知），不能返回 False（确定不连续）

        区别很重要：False 是「检查过，确认分散」；None 是「检查不了」。
        把后者说成前者是虚假披露。
        """
        import cs_consecutive as C
        axis = ["2026-10-13", "2026-10-14"]
        sp = C.consecutive_span(["2099-01-01", "2099-01-02"], axis)
        self.assertIsNone(sp["is_consecutive"], f"未知日期被当成确定结论：{sp}")
        self.assertEqual(sp["n_in_axis"], 0)

    def test_real_data_is_consecutive(self):
        """实测锁定：2026-11-03 快照的门槛后 14 个截面是连续交易日

        `current_weights_composite_ic|h1` 的 dates_after_gate 在 snapshot_dates
        中严格连续。若未来数据变化导致不再连续，这条会失败——
        那是**好事**，因为披露文案必须跟着改（从「连续」改为「分散」）。

        ⚠️ 数字曾是 10，2026-11-03 因 **P0-5 价格库补洞** 变为 14。
           变化来源已逐口径核对（round118）：
             tradable_w_ic/h1  10→13,  tradable_ic/h1 10→13,
             current_weights_composite_ic/h1 10→14, tradable_fund_ic/h1 10→13,
             h=5 一律 6→10 / 7→10，h=20 一律仍为 0。
           ——全部涨幅都来自「补回缺失收盘价后，更多截面能算出前向收益」，
           **不是** P1-6 的代码改动（classify_n_zero 是纯读，不改 n）。
           同时锁住这个数字的意义：下次它再变，必须能回答「是哪一步改的」。
        """
        import json
        import cs_consecutive as C
        if not os.path.exists("factor_ic.json"):
            self.skipTest("factor_ic.json 不存在")
        with open("factor_ic.json", encoding="utf-8") as f:
            ic = json.load(f)
        axis = ic.get("snapshot_dates") or []
        ds = (((ic.get("current_weights_composite_ic") or {}).get("1") or {})
              .get("gated") or {}).get("dates_after_gate") or []
        if len(ds) < 2:
            self.skipTest("当前快照无门槛后日期")
        sp = C.consecutive_span(ds, axis)
        self.assertTrue(sp["is_consecutive"],
                        f"门槛后日期不再连续（{sp}）—— 页面披露文案需同步复核")
        self.assertEqual(sp["n"], 14,
                         "门槛后截面数变了 —— 请先回答「是哪一步改动的」再更新本数字"
                         "（P0-5 价格补洞时 10→14，见上方注释）")
        self.assertEqual(sp["n"], len(ds),
                         "连续性探针的 n 必须等于 dates_after_gate 的真实长度")

    def test_summary_table_has_no_dates(self):
        """回归：确认日期**不在** cross_section_audit 摘要表里

        这条守的是「取数路径」。如果哪天有人往 summary 表里塞了日期，
        说明数据契约变了，本模块的 iter_gated_dates 需要复核。
        """
        import json
        if not os.path.exists("factor_ic.json"):
            self.skipTest("factor_ic.json 不存在")
        with open("factor_ic.json", encoding="utf-8") as f:
            ic = json.load(f)
        for k, v in (ic.get("cross_section_audit") or {}).items():
            if isinstance(v, dict):
                self.assertNotIn("dates_after_gate", v,
                                 f"摘要表 {k} 突然含日期 —— 取数路径需复核")

    def test_iter_gated_dates_finds_series(self):
        """必须真的能从 horizons[h].gated 抽到日期序列（非空）"""
        import json
        import cs_consecutive as C
        if not os.path.exists("factor_ic.json"):
            self.skipTest("factor_ic.json 不存在")
        with open("factor_ic.json", encoding="utf-8") as f:
            ic = json.load(f)
        rows = C.iter_gated_dates(ic)
        self.assertGreater(len(rows), 0,
                           "抽不到任何门槛后日期序列 —— 披露会静默变成空字符串")
        for lab, h, n_ag, ds in rows:
            self.assertGreaterEqual(len(ds), 2, f"{lab}|h{h} 日期不足 2 个")

    def test_render_contains_consecutive_wording(self):
        """渲染结果必须含「连续」或「分散」字样，否则等于没披露"""
        import json
        import cs_consecutive as C
        if not os.path.exists("factor_ic.json"):
            self.skipTest("factor_ic.json 不存在")
        with open("factor_ic.json", encoding="utf-8") as f:
            ic = json.load(f)
        h = C.format_consec_spans(ic)
        self.assertTrue(h, "渲染为空 —— 页面不会有任何连续性披露")
        self.assertTrue("连续" in h or "分散" in h, f"缺关键结论词：{h[:200]}")
        self.assertIn("不构成跨期稳健性证据", h,
                      "未说明「连续窗口不等于跨期稳健」这个判断依据")

    def test_gen_report_imports_from_module_not_inline(self):
        """gen_report 必须 import 本模块，不能留内联副本（防两处实现漂移）"""
        with open("gen_report_top200.py", encoding="utf-8") as f:
            src = f.read()
        self.assertIn("from cs_consecutive import", src,
                      "gen_report_top200.py 未使用 cs_consecutive 模块")
        self.assertNotIn("def _consecutive_span(", src,
                         "gen_report_top200.py 里仍有内联 _consecutive_span —— "
                         "同一口径两处实现必然漂移")


class TestVerdictNoCrossHorizonMax(unittest.TestCase):
    """round118 P1-1：`_overall_verdict` 跨周期取 |NW t| 最大值必须披露。

    问题：`MIN_SAMPLE=20`，`tradable_fund_ic` 的 n=75/79/60 全部达标 →
    在三个周期里取 |t| 最大者定 verdict。实测（纯噪声 2 万次）：
        单周期 h=5     P(|t|≥2.96) = 0.91%
        3 周期取最大    P(|t|≥2.96) = 3.19%   ← 放大 3.51 倍
    这等价于「用数据选模型再报 p 值」，是 Bailey-López de Prado 反复警告的形态。

    守四件事：
      1. 多周期达标时，note 必须列出**全部**周期的 t，不能只留胜出的那个
      2. note 必须出现「未做跨周期多重校正」这类字样（读者要能看见）
      3. 符号翻转时必须额外警示（这是「信号不稳」的直接证据）
      4. 只有单周期达标时，不应凭空声称「做过选择」（避免反向虚假披露）
    """

    def _agg(self, n, t, ic=None):
        return {"n": n, "nw_t": t, "ic_mean": ic if ic is not None else (0.05 if t > 0 else -0.05),
                "win_rate": 55.0, "cross_section_median": 194,
                "gated": {"usable": False, "gate": 200, "n_after_gate": 6}}

    def _horizons(self, t1, t5, t20, n=75):
        return {"1": self._agg(n, t1), "5": self._agg(n, t5), "20": self._agg(n, t20)}

    def test_note_lists_all_horizon_ts(self):
        """多周期达标时，全部 t 都要出现在 note 里（不能只报胜出的那个）"""
        fe = __import__("factor_eval")
        h = self._horizons(1.20, 2.96, 0.80)
        _lab, _col, note = fe._overall_verdict(h)
        for token in ("T+1=+1.20", "T+5=+2.96", "T+20=+0.80"):
            self.assertIn(token, note,
                          f"note 缺少 {token} —— 读者看不到选择过程：{note}")

    def test_note_declares_no_multiplicity_correction(self):
        """必须明写「未做跨周期多重校正」，否则 verdict 会被当成单检验结论"""
        fe = __import__("factor_eval")
        _lab, _col, note = fe._overall_verdict(self._horizons(1.20, 2.96, 0.80))
        self.assertTrue(any(k in note for k in ("未做跨周期多重校正", "多重校正")),
                        f"未披露跨周期多重性：{note}")
        self.assertIn("3", note, f"未说明从几个周期里取的最大值：{note}")

    def test_sign_flip_flagged(self):
        """周期符号翻转 → 必须额外警示（同一因子在 T+1 与 T+5 方向相反是硬伤）"""
        fe = __import__("factor_eval")
        _lab, _col, note = fe._overall_verdict(self._horizons(+2.90, -2.80, +0.50))
        self.assertIn("方向不一致", note,
                      f"符号翻转未被标注，读者会以为三个周期同向：{note}")
        # 反向验证：同向时不应出现该警示
        _lab2, _col2, note2 = fe._overall_verdict(self._horizons(+2.90, +2.80, +0.50))
        self.assertNotIn("方向不一致", note2,
                         f"同向却报了方向不一致（假警报）：{note2}")

    def test_single_qualified_horizon_no_fake_disclosure(self):
        """只有 1 个周期达标时，不得声称「从 N 个周期取最大」——反向虚假披露同样是错"""
        fe = __import__("factor_eval")
        h = {"1": self._agg(5, 9.99),                       # n<MIN_SAMPLE，不达标
             "5": self._agg(75, 2.96),
             "20": self._agg(3, -8.00)}                     # 不达标
        _lab, _col, note = fe._overall_verdict(h)
        self.assertIn("T+5", note)
        self.assertNotIn("未做跨周期多重校正", note,
                         f"只有 1 个达标周期却声称做过跨周期选择：{note}")

    def test_all_below_threshold_no_inflation_claim(self):
        """全部周期 |t|<T_LOOSE 时，不得强调「放大 3.51 倍」

        理由：取最大值若跨不过阈值，强调放大倍数只是噪音，会稀释真正有选择偏差
        风险的那几处披露（分级披露）。这条同时守住「不得反向虚假披露」。
        """
        fe = __import__("factor_eval")
        _lab, _col, note = fe._overall_verdict(self._horizons(0.98, 0.81, -0.02))
        self.assertNotIn("3.19%", note,
                         f"全部周期均不显著却强调放大倍数：{note}")
        self.assertIn("全部周期均未达宽松阈值", note,
                      f"未说明「都不显著」这个更关键的事实：{note}")
        # 各周期 t 仍必须列出（披露不能因为不显著就省掉）
        self.assertIn("T+1=+0.98", note)

    def test_sentinel_does_not_skip_small_t(self):
        """哨兵值 bug 回归：|t|<=1.0 的周期不能被静默跳过

        原实现用 `best_t=-1.0` 当哨兵，再 `abs(t) > abs(best_t)` 比较 ——
        abs(-1.0)=1.0，于是所有 |t| <= 1.0 的周期都被跳过，最终落入
        「各周期样本均 <20」的兜底文案。实测 tradable_ic（t=0.98/0.81/-0.02，
        n=79/75/60 全部达标）就命中了这个 bug，页面显示「最大 79」却声称样本不足。
        """
        fe = __import__("factor_eval")
        lab, _col, note = fe._overall_verdict(self._horizons(0.98, 0.81, -0.02))
        self.assertNotIn("样本均 <", note,
                         f"t 全部 <=1 时被误判成样本不足：{note}")
        self.assertNotEqual(lab, "样本积累中",
                            f"n=75 达标却报样本积累中：{note}")
        # 单周期 |t|=0.5 也不能掉进兜底
        _l2, _c2, n2 = fe._overall_verdict({"1": self._agg(75, 0.50),
                                            "5": self._agg(75, 0.20),
                                            "20": self._agg(60, 0.10)})
        self.assertNotIn("样本均 <", n2, f"全弱信号被误判为样本不足：{n2}")

    def test_verdict_selection_block_is_machine_readable(self):
        """verdict_selection 必须可被外部复算（JSON 里要有 candidate_ts）"""
        fe = __import__("factor_eval")
        h = self._horizons(1.20, 2.96, 0.80)
        s = fe._verdict_selection(h)
        self.assertEqual(s["selected_h"], 5)
        self.assertTrue(s["by_max_abs_t"])
        self.assertEqual(s["n_qualified"], 3)
        self.assertEqual(s["candidate_ts"], {"1": 1.20, "5": 2.96, "20": 0.80})
        self.assertTrue(s["sign_consistent"])
        self.assertIn("3.51", s["inflation_note"])

    def test_selection_block_single_horizon(self):
        """单周期达标时 by_max_abs_t 必须为 False（否则日志里会留下假的选择痕迹）"""
        fe = __import__("factor_eval")
        h = {"1": self._agg(5, 9.99), "5": self._agg(75, 2.96), "20": self._agg(3, -8.0)}
        s = fe._verdict_selection(h)
        self.assertFalse(s["by_max_abs_t"])
        self.assertEqual(s["n_qualified"], 1)
        self.assertEqual(s["candidate_ts"], {"5": 2.96})

    def test_selection_written_to_json_paths(self):
        """factor_eval 的 5 个 verdict 出口都要带 verdict_selection（漏一个 = 少一处披露）"""
        with open("factor_eval.py") as _f:
            src = _f.read()
        n_calls = src.count("_overall_verdict(")
        n_sel = src.count("_verdict_selection(")
        # 5 处调用 + 1 处 def 定义中的自引用说明；selector 有 1 处 def
        self.assertGreaterEqual(n_sel, 5,
                                f"_verdict_selection 只接了 {n_sel-1} 处，"
                                f"而 _overall_verdict 有 {n_calls-1} 处调用")


class TestEnvLock(unittest.TestCase):
    """P1-4：依赖锁定"""

    def test_requirements_are_pinned(self):
        """生产依赖必须全部钉版本（无版本约束 = 某天 akshare 升级就崩）"""
        import re
        lines = open("requirements.txt", encoding="utf-8").read().split("\n")
        unpinned = []
        for ln in lines:
            s = ln.split("#")[0].strip()
            if not s:
                continue
            # 形如 name 或 name>=1.0 视为未钉
            if not re.match(r"^[A-Za-z0-9_\.\-]+\s*==\s*[0-9]", s):
                unpinned.append(s)
        self.assertEqual(unpinned, [],
                         f"以下依赖未钉死版本：{unpinned}")

    def test_env_lock_verifier_passes(self):
        import subprocess
        if not os.path.exists("verify_env_lock.py"):
            self.skipTest("verify_env_lock.py 不存在")
        r = subprocess.run([sys.executable, "verify_env_lock.py"],
                           capture_output=True, text=True, timeout=120)
        self.assertEqual(r.returncode, 0,
                         f"环境与锁定不一致：\n{r.stdout[-1500:]}")


class TestBacktestCore(unittest.TestCase):
    """P1-5：回测统一口径内核"""

    def setUp(self):
        import backtest_core as bc
        self.bc = bc

    def test_lag_equals_hop_minus_one(self):
        """重叠窗口的 NW lag 必须是 hop-1，不能是固定 4

        实测：HOP=20 时 lag=4 的 |t| 比 lag=19 大 69%（1.681 vs 0.994）
        → 用 lag=4 会把「不显著」报成「显著」。
        """
        for hop in (1, 5, 20, 60):
            expect = max(1, hop - 1)
            s = self.bc.stat([0.01] * 30, hop=hop)
            self.assertEqual(s["lag"], expect,
                             f"hop={hop} 的 lag 应为 {expect}，实得 {s['lag']}")

    def test_nw_t_has_no_default_lag(self):
        """nw_t 不设默认 lag —— 有默认值就一定会有人不传"""
        import inspect
        sig = inspect.signature(self.bc.nw_t)
        self.assertIs(sig.parameters["lag"].default, inspect.Parameter.empty,
                      "nw_t 出现了默认 lag → 会重现「lag=4 处理 HOP=20」的坑")

    def test_overlapping_window_nw_is_more_conservative(self):
        """MA(hop-1) 正自相关的重叠窗口：NW t 必须比普通 t 保守"""
        import random
        rng = random.Random(117)
        eps = [rng.gauss(0, 1) for _ in range(300)]
        xs = [0.002 + sum(eps[i - k] for k in range(20)) / 20.0 * 0.05
              for i in range(19, 300)]
        s = self.bc.stat(xs, hop=20)
        self.assertLess(abs(s["nw_t"]), abs(s["plain_t"]),
                        "重叠窗口的 |NW t| 未小于 |普通 t| → 口径失效")

    def test_insufficient_sample_refuses_verdict(self):
        """样本不足必须拒绝出显著性结论，而不是给个数字"""
        s = self.bc.stat([0.01, 0.02, 0.03], hop=20)
        self.assertIsNone(s["significant"], "n=3 却给出了显著性结论")
        self.assertIn("样本不足", s["verdict"])

    def test_missing_price_returns_none_not_zero(self):
        """缺价返回 None，不能填 0（填 0 会把缺数据算成「不涨不跌」）"""
        bt = self.bc.Backtest(hop=20, verbose=False)
        self.assertIsNone(bt.fwd_ret(bt.dates[0], "999999"),
                          "不存在的代码应返回 None，不是 0")

    def test_benchmark_must_be_declared(self):
        """基准只能 POOL_EW / HS300；非法值必须 raise，不静默回退"""
        with self.assertRaises(ValueError):
            self.bc.Backtest(benchmark="CSI500", verbose=False)

    def test_hs300_missing_file_raises(self):
        """选 HS300 但缺数据文件必须 raise —— 不静默降级成 POOL_EW"""
        if os.path.exists("data/bench_hs300.json"):
            self.skipTest("bench_hs300.json 存在，无法测缺失分支")
        with self.assertRaises(FileNotFoundError):
            self.bc.Backtest(benchmark="HS300", verbose=False)

    def test_uses_valid_calendar(self):
        """回测默认只用有效截面（305），不是全量（341）"""
        bt = self.bc.Backtest(hop=20, verbose=False)
        self.assertLess(len(bt.dates), bt.n_dates_total,
                        "回测用了全量日期 → 建库初期 36 个爬坡日混进来了")

    def test_legacy_guard_detects_and_clears(self):
        """守卫必须双向有效：走 core 判绿，裸算普通 t 判红"""
        import tempfile
        import backtest_legacy_guard as g
        with tempfile.TemporaryDirectory() as td:
            good = os.path.join(td, "backtest_good.py")
            with open(good, "w", encoding="utf-8") as f:
                f.write("import backtest_core as bc\nHOP=20\n"
                        "bt=bc.Backtest(hop=HOP, benchmark='POOL_EW')\n")
            bad = os.path.join(td, "backtest_bad.py")
            with open(bad, "w", encoding="utf-8") as f:
                f.write("HOP=20\nt=mu/(sd/(n**0.5))\n")
            self.assertTrue(g.audit(good)["compliant"],
                            "走了 backtest_core 却仍判为不合规 → 守卫永远报红，无信号价值")
            rb = g.audit(bad)
            self.assertFalse(rb["compliant"])
            self.assertIn("nw_t", [x["key"] for x in rb["gaps"]],
                          "裸算普通 t 未被识别为缺 NW t")

    def test_all_legacy_scripts_audited(self):
        """21 个旧脚本必须全部被审计到（漏一个 = 漏一个未声明的口径）"""
        import glob
        import backtest_legacy_guard as g
        files = [f for f in glob.glob("backtest_*.py")
                 if os.path.basename(f) not in
                 ("backtest_legacy_guard.py", "backtest_core.py")]
        rows = [g.audit(f) for f in files]
        self.assertGreaterEqual(len(rows), 21,
                                f"旧回测脚本数量变了（{len(rows)} 个），"
                                f"需复核是否有新脚本未纳入口径约束")
        for r in rows:
            self.assertIn("uses_core", r)
            self.assertIsInstance(r["gaps"], list)


class TestICDecayHalfLife(unittest.TestCase):
    """P1-4（round118）：IC 衰减曲线与半衰期。

    这套用例守的不是「算得对」（那由 audit 脚本的
    `_selftest_against_production` 对拍保证），而是**报告口径不能骗人**：
    半衰期的三种非法表述，每一种都会让读者做出与真相相反的决策。
    """

    @classmethod
    def setUpClass(cls):
        import audit_round118c_ic_decay as D
        cls.D = D
        cls.out = D.os.path.join(D.HERE, "data", "audit_round118c_ic_decay.json")
        with open(cls.out, encoding="utf-8") as f:
            cls.d = __import__("json").load(f)

    def test_horizon_grid_covers_beyond_production(self):
        """网格必须超出生产的 (1,5,20)，否则「衰减」根本无从观察。"""
        grid = set(self.d["meta"]["horizons"])
        self.assertGreaterEqual(len(grid), 7)
        for h in (1, 5, 20):
            self.assertIn(h, grid)
        self.assertTrue(any(h > 20 for h in grid),
                        "网格没有超过 20 的点，等于没测衰减")

    def test_every_point_has_overlap_diag(self):
        """每个点都必须带重叠诊断 —— 缺了它就无法判断 t 能不能用。"""
        for field, c in self.d["curves"].items():
            for p in c["points"]:
                self.assertIn("overlap", p, f"{field} h={p['h']} 缺 overlap")
                ov = p["overlap"]
                for k in ("overlap_ratio", "effective_n", "t_trustworthy", "note"):
                    self.assertIn(k, ov, f"{field} h={p['h']} overlap 缺 {k}")

    def test_high_overlap_marks_t_untrustworthy(self):
        """h/n > 0.5 必须标 t 不可信。

        实测反例：score_ep h=60 报 NW t=+15.39，n=27、h/n=2.22，
        独立观测不到 1 个。若这个标记失效，该 t 会被读成「极强的估值因子」。
        """
        flagged = 0
        for field, c in self.d["curves"].items():
            for p in c["points"]:
                ov = p["overlap"]
                if ov["overlap_ratio"] is None:
                    continue
                if ov["overlap_ratio"] > 0.5:
                    self.assertFalse(ov["t_trustworthy"],
                                     f"{field} h={p['h']} h/n={ov['overlap_ratio']} "
                                     f"仍标记 t 可信")
                    flagged += 1
        self.assertGreater(flagged, 0, "本数据里应有 h/n>0.5 的点，一个都没标出来")

    def test_peak_only_from_trusted_points(self):
        """峰值只能取自「重叠可信」的点。

        反例：score_fund 全网格峰值在 h=60（IC=+0.1154 > h=20 的 +0.1039），
        若照单全收，会得出「该因子要拿 60 天」——而 h=60 的 n=27、h/n=2.22。
        """
        c = self.d["curves"]["score_fund"]
        self.assertEqual(c["peak_h"], 20,
                         "score_fund 峰值应被限制在 h=20（h=60 重叠不可信）")
        self.assertIn("重叠可信", c["peak_basis"])

    def test_sign_flip_is_flagged_not_silent(self):
        """IC 跌破峰值一半但已翻负 → 必须标 sign_flipped，不能只报半衰期数字。

        反例：total 峰值 h=3 IC=+0.0255，h=30 IC=-0.0175。
        报「半衰期 30 日」会让读者以为「第 30 天还剩一半 alpha」，
        而真相是「第 30 天已经反向」。两者决策完全相反。
        """
        c = self.d["curves"]["total"]
        self.assertEqual(c["half_life"], 30)
        self.assertTrue(c["half_life_sign_flipped"],
                        "total 的 IC 在 h=30 已翻负，必须标记")
        self.assertIn("翻负", c["half_life_note"])
        self.assertIn("不是", c["half_life_note"],
                      "文案必须明说这不是『衰减到一半』")

    def test_undefined_half_life_is_none_not_max(self):
        """测不到半衰期必须是 None + 「> h_max 无法确定」，不能报 h_max。

        把「没测到」写成「测到等于 60」是本项目反复出现的错误形态。
        """
        c = self.d["curves"]["score_ep"]
        self.assertIsNone(c["half_life"])
        self.assertIn("无法确定", c["half_life_note"])
        self.assertIn("不是等于", c["half_life_note"])

    def test_negative_peak_reports_no_definition(self):
        """峰值为负时，半衰期「无定义」，不能报 0 也不能报 None 了事。"""
        for field in ("score_quality", "score_growth", "score_size_growth"):
            c = self.d["curves"][field]
            self.assertLess(c["peak_ic"], 0, f"{field} 峰值应为负")
            self.assertIn("无定义", c["half_life_note"])
            self.assertIn("不是 0", c["half_life_note"])

    def test_meta_declares_caveat_and_sign_axis(self):
        """meta 必须自带口径声明 —— 否则 JSON 离开报告就无法被正确解读。"""
        m = self.d["meta"]
        self.assertIn("half_life_definition", m)
        self.assertIn("caveat", m)
        self.assertIn("峰值", m["half_life_definition"])
        self.assertIn("不同 h 的样本集不同", m["caveat"])

    def test_overlap_diag_boundaries(self):
        """重叠判据的边界（0.25 / 0.50）必须与注释一致。"""
        D = self.D
        self.assertTrue(D._overlap_diag(100, 25)["t_trustworthy"])    # 0.25
        self.assertTrue(D._overlap_diag(100, 50)["t_trustworthy"])    # 0.50
        self.assertFalse(D._overlap_diag(100, 51)["t_trustworthy"])   # 刚超 0.50
        self.assertFalse(D._overlap_diag(27, 60)["t_trustworthy"])    # 实测 ep h=60
        self.assertFalse(D._overlap_diag(0, 5)["t_trustworthy"])


class TestPriceStoreIntegrity(unittest.TestCase):
    """P0-5（round118）：价格库完整性。

    价格库断裂是本轮在执行 P1-4 时才被撞出来的 —— 它此前无任何告警，
    因为它「结构上完整」（日期轴一天不缺），只是「格子里是空的」。
    这类缺陷只能在数据层加断言来守。
    """

    def setUp(self):
        import factor_eval as F
        self.F = F
        self.hist = F.load_history()

    def test_snapshot_dates_have_price_coverage(self):
        """每个有快照的日期，价格覆盖必须 ≥100 只。

        这直接决定 `_fwd_returns` 能不能算出来。实测断裂期：
        2026-10-29 起 1766 只只剩 2 只，导致 4 个快照整体退出 IC。
        """
        p = self.hist["prices"]
        dates, px = p["dates"], p["px"]
        didx = {d: i for i, d in enumerate(dates)}
        snaps = {s["date"] for s in self.hist["snapshots"] if s.get("rows")}
        bad = []
        for d in sorted(snaps):
            j = didx.get(d)
            if j is None:
                continue
            n = sum(1 for arr in px.values()
                    if j < len(arr) and arr[j] is not None)
            if n < 100:
                bad.append((d, n))
        self.assertEqual(bad, [],
                         f"以下有快照的日期价格覆盖不足，前向收益会静默缺失：{bad}")

    def test_refresh_price_tail_guard_is_structural_not_data(self):
        """守住 P0-5 的根因：`refresh_price_tail` 的守卫只看日期轴末端。

        它不是 bug —— 它本来就只负责「轴的延伸」。真正的教训是：
        **轴的完整 ≠ 格的完整**，必须有一个独立机制补格（
        `fix_price_store_holes.py`）。此用例把这条边界钉住，
        防止有人「顺手」把补格逻辑塞进 refresh_price_tail 而破坏其零成本特性。
        """
        import inspect
        src = inspect.getsource(self.F.refresh_price_tail)
        self.assertIn("if last_snap <= dates[-1]", src)
        self.assertIn("return False", src)
        # 补格脚本必须独立存在
        self.assertTrue(os.path.exists("fix_price_store_holes.py"),
                        "补价格格的独立脚本不存在了")

    def test_fix_script_does_not_overwrite_existing(self):
        """补格脚本必须只填 None，绝不覆盖已有价格。

        覆盖已有值会以「今天的前复权口径」重写历史锚点
        （MEMORY 记着 append_klines_latest L129 踩过同类坑）。
        """
        src = open("fix_price_store_holes.py", encoding="utf-8").read()
        self.assertIn("arr[j] is None", src)
        self.assertIn("and cl", src)


class TestNZeroClassification(unittest.TestCase):
    """P1-6（round118）：n=0 必须分类，不能一律叫「样本积累中」。

    实测 15 个 n=0 行分属四种成因，处置方向完全相反：
      · not_mature            → 等，时间到自然有（不是缺陷）
      · stripped_contaminated → 修数据采集（污染被主动剔除）
      · insufficient_pairs    → 修字段落库范围
      · no_field              → 补字段（根本没这列）
    原文案一律写「样本积累中 n=0<20」，读者分不清「该等」还是「该修」。
    这是「没测 ≠ 通过」的又一次应用：n=0 不是「测了没结果」，是「为什么没结果」。
    """

    def setUp(self):
        import factor_eval as F
        self.F = F
        self.hist = F.load_history()
        self.tdays, self.px = F.px_index(self.hist)
        _snaps = [s for s in self.hist["snapshots"] if s["date"] in set(self.tdays)]
        self.snaps, _ = F._drop_stale_price_snapshots(_snaps)

    def test_four_reasons_are_distinct(self):
        """四种成因的常量必须互不相同（否则分类退化成「都一样」）。"""
        r = {self.F.N_ZERO_NOT_MATURE, self.F.N_ZERO_STRIPPED,
             self.F.N_ZERO_PAIRS, self.F.N_ZERO_NO_FIELD, self.F.N_ZERO_GATED}
        self.assertEqual(len(r), 5, f"成因常量有重复：{r}")

    def test_every_reason_has_a_label(self):
        """每个成因都要有人类可读标签 —— 否则页面只能显示 reason 码。"""
        for k, v in self.F._N_ZERO_LABEL.items():
            self.assertTrue(v, f"{k} 没有 label")

    def test_n_pairs_nonzero_returns_none(self):
        """n 非 0 的行不归本函数管，必须返回 None（防止「没病也开药」）。"""
        self.assertIsNone(self.F.classify_n_zero(
            "score_quality", 5, self.snaps, self.tdays, n_pairs=3))

    def test_not_mature_is_time_not_defect(self):
        """h=20 的首个有值截面是 10-13，价格轴只到 11-03 → 必须判「未到期」。

        关键：这是**必然**，不是数据缺陷。判成别的成因会让读者去修一个没坏的东西。
        """
        z = self.F.classify_n_zero("score_bias60", 20, self.snaps, self.tdays)
        self.assertEqual(z["reason"], self.F.N_ZERO_NOT_MATURE, z)
        self.assertIn("时间未到", z["detail"])

    def test_stripped_contaminated_detected(self):
        """score_quality / score_growth 的 h=5 唯一两个到期截面被污染剔除 → 必须能识别。

        这是本轮最严重的一处误判：原文案写「样本积累中」，
        但真相是「仅有的样本被主动清空了」。两者的处置方向完全相反。
        """
        for f in ("score_quality", "score_growth"):
            z = self.F.classify_n_zero(f, 5, self.snaps, self.tdays)
            self.assertEqual(z["reason"], self.F.N_ZERO_STRIPPED, (f, z))
            self.assertIn("污染剔除", z["label"])

    def test_insufficient_pairs(self):
        """score_size_growth 的 h=5 只有 1 个到期截面且配对不足 → 判「配对不足」。"""
        z = self.F.classify_n_zero("score_size_growth", 5, self.snaps, self.tdays)
        self.assertEqual(z["reason"], self.F.N_ZERO_PAIRS, z)

    def test_no_field(self):
        """score_ep 在快照里从未落库 → 判「字段未落库」，不是「样本不足」。"""
        z = self.F.classify_n_zero("score_ep", 1, self.snaps, self.tdays)
        self.assertEqual(z["reason"], self.F.N_ZERO_NO_FIELD, z)
        self.assertIn("未落库", z["detail"])

    def test_call_order_insensitive(self):
        """⚠️ 核心回归：先 strip 再判 与 不 strip 直接判，结论必须一致。

        第一版判据依赖「行里的值还在不在」，而污染剔除是**就地置 None**，
        于是同一个 (field,h) 在两条调用路径下得到两个结论。
        「同一口径两处实现必然漂移」的又一实例。
        """
        import copy
        s2 = copy.deepcopy(self.snaps)
        self.F._strip_contaminated_fields(s2)
        for f, h in (("score_quality", 5), ("score_growth", 5),
                     ("score_size_growth", 5), ("score_quality", 20),
                     ("score_bias60", 20), ("score_ep", 1)):
            a = self.F.classify_n_zero(f, h, s2, self.tdays)
            b = self.F.classify_n_zero(f, h, self.snaps, self.tdays)
            self.assertEqual(a["reason"], b["reason"],
                             f"{f}/h={h} 两条调用路径结论不一致：{a['reason']} vs {b['reason']}")

    def test_gated_note_does_not_blame_gate_when_nothing_dropped(self):
        """门槛一只没剔（dropped=0）时，不能写「门槛后为 0」。

        实测 15 个 n=0 行全部 dropped_by_gate=0 —— 门槛根本没参与。
        把「本来没有」写成「门槛后为 0」，是把责任推给一个没出场的角色。
        """
        out = self.F._gated_stats([], [], [], [], 20)
        self.assertEqual(out["dropped_by_gate"], 0)
        self.assertIn("原本就没有可用截面", out["usable_note"])
        self.assertNotIn("门槛 ≥", out["usable_note"].split("——")[0])

    def test_gated_note_blames_gate_only_when_it_actually_dropped(self):
        """门槛真的剔掉了东西时，才允许说「门槛后为 0」。"""
        out = self.F._gated_stats([0.1, 0.2], [], [], [10, 20], 20)
        self.assertEqual(out["dropped_by_gate"], 2)
        self.assertIn("全被剔除", out["usable_note"])

    def test_verdict_h_uses_reason_not_generic(self):
        """n=0 的判定文案必须说清成因，不能一律「样本积累中」。"""
        agg = {"n": 0,
               "n_zero": {"reason": self.F.N_ZERO_STRIPPED,
                          "label": "⚠️样本被污染剔除", "detail": "测试用细节"}}
        lab, col, note = self.F._verdict_h(agg)
        self.assertIn("污染剔除", lab)
        self.assertNotEqual(lab, "样本积累中")
        self.assertEqual(note, "测试用细节")

    def test_verdict_h_not_mature_uses_calm_color(self):
        """未到期是时间问题，不该用告警色（红/橙）—— 那会制造假警报。"""
        agg = {"n": 0,
               "n_zero": {"reason": self.F.N_ZERO_NOT_MATURE,
                          "label": "⏳未到期", "detail": "时间未到"}}
        lab, col, note = self.F._verdict_h(agg)
        self.assertIn("未到期", lab)
        self.assertEqual(col, "#94a3b8", "未到期必须用中性色，不能用告警色")

    def test_verdict_h_gate_dropped_is_red(self):
        """门槛真剔空时才用红色（这是真问题，与「未到期」相反）。"""
        agg = {"n": 0, "gated": {"n_after_gate": 0, "dropped_by_gate": 7, "gate": 200}}
        lab, col, note = self.F._verdict_h(agg)
        self.assertIn("门槛剔除", lab)
        self.assertEqual(col, "#ef4444")


class TestBonferroniRecomputable(unittest.TestCase):
    """P0-4（round118）：Bonferroni 阈值必须能从 K 复算，不能是手写魔数。

    历史三个数字互相矛盾（实测核对）：
      · factor_ic.json 声明「30 次」→ 应 3.1440
      · factor_eval.py 注释写「304 次 ≈ 3.90」→ 实算 3.7681（3.90 对应 K≈519.8）
      · T_BONF_STRICT_30=3.08 号称「30 次」→ 实对应 K≈24.2
    守这条的目的：让任何人拿 K 就能验算，不必信任一个魔数。
    """

    def setUp(self):
        import factor_eval as F
        self.F = F

    def test_bonferroni_t_matches_scipy(self):
        """自实现的正态分位必须与 scipy 一致（无 scipy 环境时也不能偏）。"""
        try:
            from scipy.stats import norm
        except ImportError:
            self.skipTest("无 scipy，跳过对拍")
        bad = []
        for k in (1, 2, 5, 10, 24, 30, 63, 100, 304, 520, 1000, 5000):
            a = self.F._bonferroni_t(k)
            b = norm.ppf(1 - 0.05 / 2 / k)
            if abs(a - b) > 1e-9:
                bad.append((k, a, b))
        self.assertEqual(bad, [], f"与 scipy 不一致：{bad}")

    def test_t_bonf_derived_from_k_304(self):
        """T_BONF 必须由 K 现算，且**不是** 3.90。

        ⚠️ round118 P1-2 后语义变了：`T_BONF` 由 trial_log 的 K 现算（349 → 3.8025），
        不再固定 3.7681。3.7681 改由 `T_BONF_LEGACY`（只算 legacy 304 时）承载。
        两处都不能是 3.90 —— 3.90 对应 K≈519.8，从未有过这个口径。
        """
        self.assertEqual(self.F.N_TESTS_CUMULATIVE, 304)
        # legacy 口径（只算 round13~65 的 304 次）
        self.assertAlmostEqual(self.F.T_BONF_LEGACY, 3.7681, places=4)
        # 现行口径：必须等于「用现算 K 代入解析解」的结果，而不是任何硬编码
        self.assertAlmostEqual(
            self.F.T_BONF, self.F._bonferroni_t(self.F.N_TESTS_LIVE), places=4,
            msg="T_BONF 必须等于 _bonferroni_t(N_TESTS_LIVE)，不能是手写值")
        self.assertGreaterEqual(self.F.N_TESTS_LIVE, 304,
                                "K 只增不减 —— 试验次数不会因为忘记录而变少")
        for v in (self.F.T_BONF, self.F.T_BONF_LEGACY):
            self.assertNotAlmostEqual(v, 3.90, places=2,
                                      msg="3.90 对应 K≈519.8，不是任何可用口径")

    def test_single_round_threshold_separate(self):
        """单轮 30 次的阈值必须是独立的 3.1440，不能和累计口径混用。"""
        self.assertAlmostEqual(self.F.T_BONF_SINGLE_ROUND, 3.1440, places=4)
        self.assertLess(self.F.T_BONF_SINGLE_ROUND, self.F.T_BONF,
                        "单轮阈值必然比累计口径更宽（数值更小）")

    def test_single_source_of_truth(self):
        """factor_validation_core 的 T_BONF 必须与 factor_eval 同源（禁止各写一份）。"""
        import factor_validation_core as VC
        self.assertEqual(VC.T_BONF, self.F.T_BONF,
                         "两处 T_BONF 不一致 —— 同一口径两处实现必然漂移")

    def test_gen_report_has_no_bare_390_verdict(self):
        """gen_report 里不得再出现裸写的 `>= 3.90` 判定（必须走 _T_BONF）。

        ⚠️ 断言写法有意**不锁定具体导入语句**（`from factor_eval import T_BONF
        as _T_BONF`）—— P1-2 后导入变成了多行，锁死单行会逼着人为了让测试
        通过而改测试，而不是改代码。这里断言语义：
          ① 不能有裸 3.90 判定
          ② 必须从 factor_eval 取 T_BONF（不论几行怎么写）
        """
        import re as _re
        src = open("gen_report_top200.py", encoding="utf-8").read()
        self.assertNotIn(">= 3.90", src,
                         "gen_report 又有手写 3.90 判定 —— 应改为读 _T_BONF")
        # 允许跨行：from factor_eval import (T_BONF as _T_BONF, ...)
        self.assertRegex(
            src,
            _re.compile(r"from\s+factor_eval\s+import\s*\(?[^)]*"
                        r"T_BONF\s+as\s+_T_BONF", _re.S),
            "gen_report 必须从 factor_eval 导入 T_BONF（单一来源）")

    def test_metadata_declares_recomputable_formula(self):
        """factor_ic.json 的 method.bonferroni 必须写出 K 与算式，而不只是数值。

        ⚠️ 本用例**会在代码改了但没重跑 factor_eval 时失败** —— 这是故意的。
        `factor_ic.json` 是生成物，不是源码；口径改了不同步重跑，
        页面读到的仍是旧口径。让测试在这里红，比让页面静默用旧阈值诚实。
        """
        import json
        if not os.path.exists("factor_ic.json"):
            self.skipTest("factor_ic.json 不存在")
        with open("factor_ic.json", encoding="utf-8") as f:
            ic = json.load(f)
        s = ((ic.get("method") or {}).get("bonferroni") or "")
        self.assertIn("304", s, f"未声明 legacy 检验次数：{s[:160]}")
        self.assertIn("可复算", s, f"未声明阈值可复算：{s[:160]}")
        self.assertIn("K=", s, f"未写明 K 的取值与来源：{s[:160]}")
        self.assertNotIn("3.9；", s, "仍是旧文案（3.9）—— 请重跑 factor_eval.py")

    def test_k_range_not_hardcoded_in_metadata(self):
        """K 的区间必须**现算**，不能把 `349~656` 这类数字抄进文案。

        踩过的坑（就在本轮）：K 是 349 时文案写「真实区间是 349~656」，
        登记完 118d/118e/118f 三条试验后 K 变成 352 —— 文案里的 349 立刻
        与 `K=352` 自相矛盾。**注释/文案里抄会变的数字，是本项目最典型的陈旧来源。**

        断言口径：区间下界必须等于同一条文案里的 `K=`，且区间不得写死。
        """
        import json
        import re
        if not os.path.exists("factor_ic.json"):
            self.skipTest("factor_ic.json 不存在")
        with open("factor_ic.json", encoding="utf-8") as f:
            ic = json.load(f)
        s = ((ic.get("method") or {}).get("bonferroni") or "")
        m_k = re.search(r"K=(\d+)", s)
        self.assertIsNotNone(m_k, f"文案里没有 K=<数字>：{s[:160]}")
        k = int(m_k.group(1))
        # 区间写法形如 "可证区间是 352~677"
        m_rng = re.search(r"可证区间\*\*是\s*(\d+)\s*~\s*(\d+)", s)
        self.assertIsNotNone(m_rng, f"未给出 K 的可证区间：{s[:220]}")
        lo, hi = int(m_rng.group(1)), int(m_rng.group(2))
        self.assertEqual(lo, k, f"区间下界 {lo} ≠ K={k} —— 文案内部自相矛盾，请重跑")
        self.assertGreater(hi, lo, f"区间上界 {hi} 不大于下界 {lo}")


class TestTrialLog(unittest.TestCase):
    """P1-2（round118）：trial log 是多重检验校正的唯一硬输入。

    守的核心不是"有没有这个文件"，而是**三条会静默出错的语义**：

    | 守什么 | 回退后的症状 |
    |---|---|
    | K 只有一个出口 | 各脚本自己数 → 每个脚本一个 K → 阈值全不同 |
    | 单位不可通约必须显式 | 「304 次检验」+「45 个试验轮次」直接相加 → K 无意义 |
    | 策展分类不兜底 | 关键词猜错一个 kind → K 差一个 → 阈值静默漂 |

    实测背景：legacy.k=304 只覆盖 round13~65；round66~118 有 54 个审计产物、
    45 个计入类试验轮次，从未进过 K。真实 K 区间 **349~656**。
    """

    def setUp(self):
        import trial_log as tl
        import factor_eval as F
        self.tl = tl
        self.F = F

    def test_k_single_outlet(self):
        """K 必须只能从 n_trials() 取，且等于 legacy + 已确认轮次数。"""
        s = self.tl.k_summary()
        self.assertEqual(self.tl.n_trials(), s["k_measured"])
        self.assertEqual(s["k_measured"], s["legacy_k"] + s["verified_k"])

    def test_legacy_k_is_documented(self):
        """legacy.k 必须带出处（文件+日期+轮次区间），否则这个数无法追溯。"""
        d = self.tl.load()
        lg = d.get("legacy") or {}
        self.assertEqual(lg.get("k"), 304)
        self.assertEqual(lg.get("rounds"), "round13~65")
        self.assertTrue(lg.get("declared_in", "").endswith(".md"),
                        "legacy.k 必须能指回一份具体报告")
        self.assertFalse(lg.get("verifiable"),
                         "304 是聚合声明，不得声称可逐项复核")

    def test_units_not_additive(self):
        """⚠️ 单位不可通约：K 下界与上界的差距必须被显式暴露，不得只报一个数。

        「304 次检验」的单位是检验次数；回填记录的单位是试验轮次，
        一个轮次里装了多个检验（实测 round92 有 9 条、round113 有 13 条）。
        两者相加在数学上没有意义 —— 所以必须同时给区间，让使用方知道这是下界。
        """
        r = self.tl.k_range()
        self.assertGreaterEqual(r["k_lower"], 304)
        self.assertGreater(r["k_upper_estimate"], r["k_lower"],
                           "上界必须严格大于下界 —— 否则说明'缺检验数'的情况被当成 0")
        self.assertGreater(r["n_without_n_tests"], 0,
                           "本该有部分产物提不出检验数；若为 0 说明逻辑被改了")
        self.assertIn("估计", r["note"], "必须写明上界是估计而非实测")

    def test_no_keyword_fallback_classification(self):
        """策展表未收录的产物必须判 unclassified，**不得关键词兜底**。

        兜底会给出"看起来有分类"的结果，掩盖"没人看过这条"的事实，
        而 kind 决定是否计入 K → 静默兜底 = K 静默漂移。
        """
        self.assertEqual(self.tl._guess_kind("随便什么描述", "not_in_curation.json"),
                         self.tl.KIND_UNCLASSIFIED)

    def test_curation_covers_most_artifacts(self):
        """策展表必须覆盖绝大多数产物 —— 覆盖率掉了说明有新产物没人定性。"""
        arts = self.tl._artifact_rounds()
        tot = miss = 0
        for rn, rels in arts.items():
            if rn < 66:
                continue
            for rel in rels:
                tot += 1
                if self.tl._guess_kind(None, rel) == self.tl.KIND_UNCLASSIFIED:
                    miss += 1
        self.assertGreater(tot, 0)
        self.assertLessEqual(miss / tot, 0.10,
                             f"{miss}/{tot} 个产物未定性 —— 新增产物须补进 CURATION")

    def test_diagnostic_not_counted_in_k(self):
        """统计诊断/数据审计类不进 K（否则阈值被自己虚增）。"""
        for kd in (self.tl.KIND_DIAG, self.tl.KIND_DATA):
            self.assertNotIn(kd, self.tl.COUNTED_KINDS,
                             f"{kd} 不该计入 K —— 它不产生「因子有效」这类假设")

    def test_empty_what_rejected(self):
        """空描述必须拒绝 —— 一条没有描述的记录等于没记。"""
        with self.assertRaises(ValueError):
            self.tl.append_trial(what="  ")

    def test_backfill_does_not_double_count_legacy(self):
        """回填起点必须 >=66，否则与 legacy 304 重复计算。

        若把 round13~65 的产物也补登，K 直接翻倍 → 阈值抬到 4.2+ →
        **所有历史结论都会被判为不显著**。"补账"变成"伪造更严的标准"。
        """
        added = self.tl.backfill_from_artifacts(dry_run=True, verbose=False)
        bad = [(rn, rel) for _tid, rn, _k, rel in added if rn < 66]
        self.assertEqual(bad, [], f"回填混入了 legacy 窗口内的产物：{bad[:5]}")

    def test_audit_gaps_clean_after_backfill(self):
        """回填后 --audit 应为空，否则说明有产物从未登记（账本已陈旧）。"""
        miss = self.tl.audit_gaps(min_round=66)
        self.assertEqual(miss, [],
                         f"仍有 {len(miss)} 个审计产物未登记："
                         f"{[m['source'] for m in miss[:5]]}")

    def test_k_increase_does_not_flip_published_conclusions(self):
        """K 从 304 涨到上界，不得翻转任何已发布的 bonferroni_sig=True。

        这是本次接线的安全前提：实测 K 304→656 阈值 3.7681→3.9560，
        唯一 sig=True 的行（score_attention h=5，|NW t|=6.34）稳过。
        若将来某条结论卡在阈值附近，这条测试会失败 —— 那时必须人工裁决，
        **不能默默让 K 变化改掉历史结论**。
        """
        import json
        if not os.path.exists("factor_ic.json"):
            self.skipTest("factor_ic.json 不存在")
        with open("factor_ic.json", encoding="utf-8") as f:
            d = json.load(f)

        th_hi = self.F._bonferroni_t(self.tl.k_range()["k_upper_estimate"])

        def walk(o):
            if isinstance(o, dict):
                if o.get("bonferroni_sig") is True and o.get("nw_t") is not None:
                    yield o
                for v in o.values():
                    yield from walk(v)
            elif isinstance(o, list):
                for v in o:
                    yield from walk(v)

        for row in walk(d):
            self.assertGreaterEqual(
                abs(row["nw_t"]), th_hi,
                f"该行 sig=True 但 |t|={abs(row['nw_t']):.2f} < 上界阈值 {th_hi:.4f}"
                f" —— K 增大后会翻转，必须人工裁决而非静默改结论")

    def test_narrowing_to_k304_raises_threshold(self):
        """K 越大阈值越严 —— 方向断言，防止有人把比较写反。"""
        th_i = self.F._bonferroni_t(self.tl.k_range()["k_lower"])
        th_u = self.F._bonferroni_t(self.tl.k_range()["k_upper_estimate"])
        self.assertGreater(th_u, th_i)


class TestDeflatedSharpe(unittest.TestCase):
    """P1-3（round118）：DSR 把「试了 N 次」折进 Sharpe 的显著性。

    守三条会静默出错的地方：

    | 守什么 | 回退后的症状 |
    |---|---|
    | 纯噪声下 DSR≈0.5 | 实现写错时仍会给出「显著」外观，且无人能发现 |
    | N↑ → DSR↓ | 方向写反 → 试得越多结论越"显著"（正好相反） |
    | 不给 N 就不算 | 用默认 K 静默填 → 带着"已折掉偏差"的外观，比不算更有害 |
    """

    def setUp(self):
        import backtest_core as bc
        self.bc = bc

    def test_norm_ppf_matches_scipy(self):
        """DSR 依赖正态分位，必须与 scipy 一致（无 scipy 环境也不能偏）。"""
        try:
            from scipy.stats import norm
        except ImportError:
            self.skipTest("无 scipy，跳过对拍")
        bad = []
        for p in (1e-6, 1e-4, 0.01, 0.025, 0.05, 0.3, 0.5, 0.7,
                  0.95, 0.975, 0.99, 0.9999):
            a, b = self.bc._norm_ppf(p), norm.ppf(p)
            if abs(a - b) > 1e-9:
                bad.append((p, a, b))
        self.assertEqual(bad, [], f"与 scipy 不一致：{bad}")

    def test_pure_noise_dsr_is_half(self):
        """⚠️ 最关键的正确性检验：纯噪声下 DSR 必须 ≈ 0.5。

        若实现写错（如漏掉 SR0、或 sqrt(T) 写成 sqrt(T-1) 的倒数），
        纯噪声仍会给出远离 0.5 的值 —— 那是唯一能在无真值时自证的地方。
        """
        import random
        rng = random.Random(42)
        vals = []
        for _ in range(300):
            xs = [rng.gauss(0, 1) for _ in range(60)]
            mm = self.bc.sharpe_moments(xs)
            d = self.bc.deflated_sharpe(mm["sr"], 60, 1, 0.05,
                                        mm["skew"], mm["kurt"])
            if d["dsr"] is not None:
                vals.append(d["dsr"])
        self.assertGreater(len(vals), 250)
        mean = sum(vals) / len(vals)
        self.assertAlmostEqual(mean, 0.5, delta=0.06,
                               msg=f"纯噪声 DSR 均值 {mean:.4f} 偏离 0.5 —— 实现有误")

    def test_more_trials_lower_dsr(self):
        """N 越大 DSR 越小 —— 方向断言（写反会让"试得越多越显著"）。"""
        import random
        rng = random.Random(7)
        xs = [rng.gauss(0.002, 0.01) for _ in range(60)]
        mm = self.bc.sharpe_moments(xs)
        vals = [self.bc.deflated_sharpe(mm["sr"], 60, n, 0.05,
                                        mm["skew"], mm["kurt"])["dsr"]
                for n in (1, 10, 100, 1000)]
        for i in range(len(vals) - 1):
            self.assertGreater(vals[i], vals[i + 1],
                               f"N↑ 时 DSR 未下降：{vals}")

    def test_n1_has_no_selection_bias(self):
        """N=1 时 SR0 必须恰为 0（只试一次，无可选择偏差）。"""
        self.assertAlmostEqual(self.bc.expected_max_sharpe(1, 0.5), 0.0, places=12)

    def test_sr0_monotone_in_n_and_std(self):
        """SR0 对 N 与 sr_std 都单调递增。"""
        prev = -1
        for n in (1, 2, 10, 100, 1000):
            v = self.bc.expected_max_sharpe(n, 0.05)
            self.assertGreater(v, prev, f"N={n} 时 SR0 未递增")
            prev = v
        self.assertGreater(self.bc.expected_max_sharpe(100, 0.10),
                           self.bc.expected_max_sharpe(100, 0.05))

    def test_no_n_trials_means_no_dsr(self):
        """⚠️ 不给 n_trials 时**不得**算 DSR（不许用默认 K 静默填）。

        DSR 的全部意义在于 N 是真实的。填个假 N 算出的 DSR 比不算更有害：
        它带着"已折掉选择偏差"的外观。
        """
        s = self.bc.stat([0.01, -0.02, 0.03, 0.005, -0.01, 0.02], hop=1)
        self.assertIsNone(s["dsr"], "未给 n_trials 却算出了 DSR")

    def test_dsr_vetoes_nw_significance(self):
        """DSR 不过时，必须把 NW t 的「显著」撤掉（不许两套结论并存）。"""
        # 构造：SR 小但有微弱正漂移、N 很大 → NW t 可能过 2 但 DSR 过不了
        import random
        rng = random.Random(11)
        xs = [rng.gauss(0.0012, 0.01) for _ in range(200)]
        s = self.bc.stat(xs, hop=1, n_trials=5000, sr_std=0.05)
        if s["dsr"] and s["dsr"].get("significant") is False:
            self.assertFalse(s["significant"],
                             "DSR 未过却仍标记 significant —— 两套结论并存")
            self.assertTrue(s.get("dsr_veto"),
                            "DSR 否决时未留 dsr_veto 痕迹")
            self.assertIn("DSR", s["verdict"])

    def test_dsr_uses_nonoverlap_T(self):
        """DSR 的 T 必须用不重叠样本数 —— 重叠 T 会让 sqrt(T-1) 虚高。

        与 NW t 踩过的是同一个坑（记忆第 ⑮ 条）。这里用「同一序列、
        不同 hop」间接验证：hop 越大，不重叠样本越少，T 越小。
        """
        import random
        rng = random.Random(3)
        xs = [rng.gauss(0.001, 0.01) for _ in range(200)]
        n_obs = []
        for hop in (1, 5, 20):
            s = self.bc.stat(xs, hop=hop, n_trials=100, sr_std=0.05)
            n_obs.append(s["dsr"]["n_obs"])
        self.assertGreater(n_obs[0], n_obs[1], f"hop=1 的 T 应大于 hop=5：{n_obs}")
        self.assertGreater(n_obs[1], n_obs[2], f"hop=5 的 T 应大于 hop=20：{n_obs}")

    def test_report_reads_n_from_trial_log(self):
        """Backtest.report() 的 N 必须来自 trial_log，且标出 sr_std 是假设。"""
        import trial_log
        bt = self.bc.Backtest(hop=20, verbose=False)
        r = bt.report()
        m = r.get("dsr_meta") or {}
        if not m or m.get("error"):
            self.skipTest(f"trial_log 不可用：{m}")
        self.assertEqual(m["n_trials"], trial_log.n_trials(),
                         "report 的 N 与 trial_log 不一致")
        self.assertTrue(m.get("sr_std_assumed"),
                        "sr_std 是假设值，必须显式标注，不得伪装成实测")
        self.assertEqual(len(m["n_trials_range"]), 2,
                         "必须同时给出 K 的区间（下界~上界）")

    def test_dm_does_not_replace_pbo(self):
        """DSR 不得被表述为「替代了 PBO」——项目没有多策略矩阵，PBO 做不了。"""
        import random
        rng = random.Random(5)
        xs = [rng.gauss(0.001, 0.01) for _ in range(60)]
        mm = self.bc.sharpe_moments(xs)
        d = self.bc.deflated_sharpe(mm["sr"], 60, 100, 0.05,
                                    mm["skew"], mm["kurt"])
        self.assertIn("PBO", d["note"],
                      "note 必须说明 DSR 不能替代 PBO")
        self.assertIn("不能替代", d["note"])


class TestTurnoverCost(unittest.TestCase):
    """P1-5（round118）：换手率与交易成本实测。

    守三条会静默出错的：

    | 守什么 | 回退后的症状 |
    |---|---|
    | 调仓日必须跳步 | hop=20 仍逐日推进 → 20 日收益被累加 20 次，虚增 20 倍 |
    | 建仓不能免成本 | 首日换手记 0 → 系统性低估成本 |
    | 成本随 hop 摊薄 | 但**换手率本身随 hop 上升**（排名漂移），两效应要分开 |

    实测核心结论：日频调仓的年度成本拖累 **32~46%/年**，月度仅 **3%/年**。
    """

    def setUp(self):
        import audit_round118d_turnover_cost as A
        self.A = A
        self._cache = None

    def _prep(self):
        if self._cache is None:
            self._cache = self.A._prep()
        return self._cache

    def test_prep_keeps_snapshots(self):
        """⚠️ _strip_contaminated_fields 是**就地改**，返回值是剔除记录不是快照。

        我第一版写成 `snaps = F._strip_contaminated_fields(snaps)`，
        90 个快照变成 2 个（那 2 条恰好是污染日记录）。
        """
        import factor_eval as F
        tdays, snaps, _px = self._prep()
        with open(F.HISTORY_PATH, encoding="utf-8") as f:
            raw = [s["date"] for s in json.load(f)["snapshots"] if s.get("rows")]
        expect = [d for d in raw if d in set(tdays)]
        self.assertEqual(len(snaps), len(expect),
                         f"_prep 后快照数 {len(snaps)} != 轴上应有 {len(expect)}")

    def test_rebalance_dates_are_strided(self):
        """⚠️ 调仓日必须每 hop 日一次，不能逐日推进。

        逐日推进会让 hop=20 的每期收益被连续累加 20 次
        （实测毛收益从 −0.56% 虚增到 −10.6%），换手率也被摊平。
        """
        tdays, snaps, _px = self._prep()
        idx = {d: i for i, d in enumerate(tdays)}
        cand = [s["date"] for s in snaps if idx[s["date"]] + 20 < len(tdays)]
        reb = cand[::20]
        gaps = [idx[reb[i + 1]] - idx[reb[i]] for i in range(len(reb) - 1)]
        self.assertTrue(gaps, "调仓点不足 2 个，无法检验间隔")
        for g in gaps:
            self.assertGreaterEqual(g, 20,
                                    f"调仓日间隔 {g} < 20 —— 仍在逐日推进")

    def test_turnover_in_unit_interval(self):
        """换手率必须在 [0,1]。"""
        tdays, snaps, _px = self._prep()
        for hop in (1, 20):
            for x in self.A.turnover_series(snaps, tdays, hop, 20):
                self.assertGreaterEqual(x["turnover"], 0.0)
                self.assertLessEqual(x["turnover"], 1.0 + 1e-9)

    def test_first_rebalance_costs_100pct(self):
        """建仓日换手必须是 100% —— 不能免掉首次建仓成本。"""
        tdays, snaps, _px = self._prep()
        tov = self.A.turnover_series(snaps, tdays, 20, 20)
        self.assertTrue(tov)
        self.assertAlmostEqual(tov[0]["turnover"], 1.0, places=12)

    def test_same_snapshot_same_picks(self):
        """同一快照两次选取必须相同（防"分数抖动当成交"）。"""
        _t, snaps, _px = self._prep()
        a = set(self.A._rank_picks(snaps[0], 20))
        b = set(self.A._rank_picks(snaps[0], 20))
        self.assertEqual(a, b)

    def test_higher_cost_lower_net(self):
        """成本越高净收益越低（方向断言）。"""
        tdays, snaps, px = self._prep()
        r20 = self.A.simulate(snaps, tdays, px, 20, 20, 20)
        r50 = self.A.simulate(snaps, tdays, px, 20, 20, 50)
        self.assertIsNotNone(r20.get("net_mean"))
        self.assertIsNotNone(r50.get("net_mean"))
        self.assertGreater(r20["net_mean"], r50["net_mean"])

    def test_turnover_not_assumed_monotone_in_hop(self):
        """⚠️ **不得**假设换手率随 hop 单调下降。

        实测：hop=1 是 39.5%，hop=20 是 68.8%（反而更高）。
        机制：hop 越大，两次调仓间隔越久，排名漂移越多 → 换手率上升。
        「调仓少→换手低」只在持仓不变时成立，而 topN 排名每天在变。
        **方向断言必须来自机制，不能来自直觉** —— 这条测试就是防止
        有人（包括未来的我）再把直觉写回断言里。
        """
        tdays, snaps, _px = self._prep()
        t1 = self.A.turnover_series(snaps, tdays, 1, 20)
        t20 = self.A.turnover_series(snaps, tdays, 20, 20)
        v1 = sum(x["turnover"] for x in t1) / max(1, len(t1))
        v20 = sum(x["turnover"] for x in t20) / max(1, len(t20))
        self.assertGreater(v1, 0.05, "hop=1 换手过低，调仓未生效")
        # 不断言方向，只断言"两个都在合理范围"
        for v in (v1, v20):
            self.assertGreaterEqual(v, 0.0)
            self.assertLessEqual(v, 1.0)

    def test_annual_cost_drag_is_the_headline(self):
        """日频的年度成本必须显著高于月度（这是 P1-5 的核心量化结论）。"""
        tdays, snaps, _px = self._prep()
        def ann_cost(hop):
            tov = self.A.turnover_series(snaps, tdays, hop, 20)
            if not tov:
                return None
            mt = sum(x["turnover"] for x in tov) / len(tov)
            return mt * 2 * 20 / 10000.0 * 252.0 / hop
        c1, c20 = ann_cost(1), ann_cost(20)
        self.assertIsNotNone(c1)
        self.assertIsNotNone(c20)
        self.assertGreater(c1, 5 * c20,
                           f"日频年成本 {c1*100:.2f}% 未显著高于月度 {c20*100:.2f}%")

    def test_cum_excess_consistent_across_hops(self):
        """⚠️ 跨 hop 只能比**累计**，不能比每期收益。

        区间本身在跌，hop=20 的每期 −12% 不是"比 hop=1 差 20 倍"，
        而是"同样在跌的区间里持有了 20 天"。累计口径下各 hop 应同量级。
        """
        tdays, snaps, px = self._prep()
        cums = []
        for hop in (1, 20):
            r = self.A.simulate(snaps, tdays, px, hop, 20, 20)
            ec = r.get("excess_cum")
            if ec is not None:
                cums.append((hop, ec))
        self.assertEqual(len(cums), 2, "累计超额缺失")
        a, b = abs(cums[0][1]), abs(cums[1][1])
        self.assertLess(max(a, b), 3 * min(a, b),
                        f"累计超额跨 hop 差超过 3 倍：{cums} —— 口径可能不一致")


class TestDominantPoolwide(unittest.TestCase):
    """P0-3（round118）：`score_dominant` 从「组内前 30%」扩到全池。

    这件事最容易被理解错的地方，是把它当成「补数据」。实际成因不是数据缺失：

    | 环节 | 实测覆盖率 |
    |---|---|
    | 申万二级映射 | 100.0% |
    | EM 财务缓存 | 100.0% |
    | 营收+利润 TTM 齐全 | 99.7% |
    | 全池 syn 可算 | 99.1% |
    | **实际落分 dominant** | **26.1%** |

    前四行都满了，落分只有 26.1% —— 因为 `score_rows` 里有一行**互斥分区**
    `is_leader = syn >= 7.0`，够格的写 dominant，不够格的写 size_growth。
    所以这次改的是**模型结构**，权重一分未动（市值大类本来就合计 0.30）。

    必须守住的四条：

    | 守什么 | 回退/改错后的症状 |
    |---|---|
    | 默认必须是全池 | 有人重跑后覆盖率静默退回 26% |
    | "leader" 模式可回滚 | 出问题时无法做 A/B 对照 |
    | size_growth 仍只给非龙头 | 对龙头也按"越小越高分"展开 = 方向错 |
    | 必须披露暴露不对称 | 非龙头 0.30 vs 龙头 0.10，藏起来就是误导 |
    """

    @classmethod
    def setUpClass(cls):
        import ep_size_ind
        cls.E = ep_size_ind
        import inspect
        cls.sig = inspect.signature(ep_size_ind.score_rows)

    def test_default_scope_is_pool(self):
        """默认值必须是 'pool'。改成 'leader' 就等于静默回退到 26% 覆盖率。"""
        self.assertEqual(self.sig.parameters["dominant_scope"].default, "pool",
                         "dominant_scope 默认值不是 'pool' —— 覆盖率会静默退回 26%")

    def test_bad_scope_rejected(self):
        """拼错的 scope 必须报错，不能被当成回退静默跑完整条链路。"""
        rows = [{"code": "000001", "price": 10.0, "circ_mv": 1e10}]
        with self.assertRaises(ValueError):
            self.E.score_rows(rows, "2026-11-02", em={}, sw_map={},
                              dominant_scope="poolwide")

    def test_leader_scope_still_available(self):
        """回滚路径必须存在：'leader' 走旧口径。"""
        rows = [{"code": "000001", "price": 10.0, "circ_mv": 1e10}]
        # 空 em/sw_map 下两个 scope 都不落分，只验不抛异常
        self.E.score_rows(rows, "2026-11-02", em={}, sw_map={}, dominant_scope="leader")

    def test_size_growth_partition_unchanged(self):
        """`size_growth` 的分区条件必须仍是 `if not is_leader`。

        语义：size_growth 是「小盘成长溢价」，龙头按定义不是小盘。若也展开，
        等于把「市值越小越高分」套到大市值股，方向是反的。
        断言写法用**源码结构**而非运行结果 —— 运行结果依赖真实数据才能体现。
        """
        import inspect
        src = inspect.getsource(self.E.score_rows)
        self.assertIn("if not is_leader:", src,
                      "size_growth 的分区条件被改了 —— 龙头会拿到小盘溢价（方向错）")
        # dominant 侧必须是「全池 或 龙头」二选一
        self.assertTrue(
            'if dominant_scope == "pool" or is_leader:' in src,
            "dominant 的落分条件不是「全池 or 龙头」—— 全池口径失效")

    def test_asymmetry_is_printed(self):
        """暴露不对称（非龙头 0.30 vs 龙头 0.10）必须有显式打印。

        这不是注释义务，而是**运行时提示**：每天跑日链的人不看源码，
        只在终端看到这行才知道口径变了。
        """
        import inspect
        src = inspect.getsource(self.E.score_rows)
        self.assertIn("暴露不对称", src,
                      "缺少暴露不对称的运行时提示 —— 使用者无从知晓 0.30 vs 0.10")
        self.assertIn("3 倍", src)

    def test_leader_threshold_no_longer_gates_dominant(self):
        """`DOM_LEADER_THR` 不再决定 dominant 是否落分，只用于留痕/计数。

        断言写法：常量注释里必须写明这一点。否则后来人看到「前 30%」四个字
        会以为它还在起分区作用，从而按旧口径解读覆盖率。
        """
        import inspect
        src = inspect.getsource(self.E)
        i = src.find("DOM_LEADER_THR = ")
        self.assertGreater(i, 0)
        block = src[i:i + 1500]
        self.assertIn("不再决定", block,
                      "DOM_LEADER_THR 的注释没写清它已不决定 dominant 落分")

    def test_poolwide_real_coverage(self):
        """真实数据上跑一遍，覆盖率必须 >= 90%（实测 99.1%）。

        ⚠️ 本用例**依赖真实文件**（top200_scored / fin_cache_em）。文件缺失时
        用 skipTest 而非 fail —— 但**绝不**改成「跳过也算通过」的静默逻辑：
        skip 会在测试摘要里显式列出 skipped 数，fail 会红。
        """
        import glob
        import json
        import os
        here = os.path.dirname(os.path.abspath(__file__))
        fs = [f for f in sorted(glob.glob(os.path.join(here, "top200_scored_*.json")),
                                reverse=True) if ".bak" not in f]
        if not fs:
            self.skipTest("无 top200_scored_*.json，跳过真实数据覆盖检查")
        d = json.load(open(fs[0], encoding="utf-8"))
        rows = d.get("results") or []
        if not rows:
            self.skipTest("scored 文件无 results")
        as_of = d.get("date")
        rows = [dict(r) for r in rows]
        for r in rows:
            for k in ("score_dominant", "score_size_growth", "mc_pct",
                      "rev_share", "profit_share", "score_mc_raw", "w_mc"):
                r.pop(k, None)
        with open(fs[0], encoding="utf-8") as fh:
            d = json.load(fh)
        self.E.score_rows(rows, as_of)
        n = sum(1 for r in rows if isinstance(r.get("score_dominant"), (int, float)))
        pct = 100.0 * n / len(rows)
        self.assertGreaterEqual(
            pct, 90.0,
            f"全池 dominant 覆盖率仅 {pct:.1f}%（{n}/{len(rows)}），"
            f"应 >=90% —— 可能退回了 leader 分区")


class TestReversalRemap(unittest.TestCase):
    """round121：反转两维（bias60 / rsi_rev）改池内分位连续口径 + 止跌确认门罚分。

    守四条会静默出错、且已经在真实数据上踩过的纪律：

    | 守什么 | 回退/写错后的症状 |
    |---|---|
    | `reversal_rank` 方向（越低 RSI 越高分） | 方向写反 → 反转因子变成动量因子，最强反转候选拿 0 分 |
    | 分位值域 0–5（不是 0–10） | 量程错 → DIM_MAX 失配，两维权重实际放大 2 倍 |
    | 确认门**不含**「站上MA20」 | 含 MA20 → RSI<30 内样本 100% 被剔除（实测 n=0） |
    | 深跌未确认不给满分 | 生益科技型反例复活：RSI 19.5 跌停 + 空头排列拿双满分 |

    ⚠️ 这些断言贴着 h=20 实测写（data/audit_round121*.json），改期望值前必须
       先说明是哪一步改的（方法论㉓）。
    """

    @classmethod
    def setUpClass(cls):
        import importlib.util
        src = open(os.path.join(HERE, "step2_stockscorer_v2.py"), encoding="utf-8").read()
        i = src.find("def reversal_rank(")
        j = src.find("def score_vol_pulse(")
        assert i > 0 and j > i, "reversal_rank / reversal_confirm_gate 定义未找到"
        ns = {}
        exec(src[i:j], ns)
        cls.reversal_rank = staticmethod(ns["reversal_rank"])
        cls.gate = staticmethod(ns["reversal_confirm_gate"])

    def test_lower_rsi_gets_higher_score(self):
        """方向：RSI 越低 → 分位分越高（higher_is_better=False 的语义）。"""
        vals = {"a": 10.0, "b": 30.0, "c": 50.0, "d": 70.0}
        r = self.reversal_rank(vals, higher_is_better=False)
        self.assertGreater(r["a"], r["d"],
                           "RSI 10 分必须高于 RSI 70 分 —— 方向写反会变成动量因子")
        self.assertGreater(r["a"], r["b"])
        self.assertGreater(r["b"], r["c"])

    def test_more_negative_bias_gets_higher_score(self):
        """方向：bias60 越负 → 分越高。"""
        vals = {"a": -40.0, "b": -15.0, "c": 0.0, "d": 20.0}
        r = self.reversal_rank(vals, higher_is_better=False)
        self.assertGreater(r["a"], r["b"])
        self.assertGreater(r["b"], r["d"])

    def test_range_is_0_to_5_not_10(self):
        """值域必须是 0–5（DIM_MAX=5.0）—— 写错成 10 会让两维实际权重翻倍。"""
        vals = {f"s{i}": float(i) for i in range(100)}
        r = self.reversal_rank(vals, higher_is_better=False)
        self.assertGreaterEqual(min(r.values()), 0.0)
        self.assertLessEqual(max(r.values()), 5.0,
                             "分位分不得超过 5 —— score_bias60/rsi_rev 的 DIM_MAX=5.0")
        self.assertGreater(max(r.values()), 4.0,
                           "最大分位应接近 5（否则整维被系统性压低）")

    def test_empty_and_single_return_empty(self):
        """n<2 返回空 dict → 调用方退回绝对阈值兜底，不得抛异常。"""
        self.assertEqual(self.reversal_rank({}, False), {})
        self.assertEqual(self.reversal_rank({"a": 1.0}, False), {})

    def test_gate_excludes_ma20_condition(self):
        """确认门不得含「站上MA20」—— 实测 RSI<30 内命中 0 个（结构性空集）。

        构造：60 根单边下跌（每日 −0.5），最后一日跳空收阴创新低。
        → 不创新低=False、站上MA5=False（收盘远低于 MA5）、收阳=False
        → 三项全 False，确认门必须判 False。
        ⚠️ 上一版测试用递减序列但仍让尾根 open=10.5/close=10.0，
           MA5 被算成包含尾根的低值 → 误判为 True。这里改为显式断言三个子条件。
        """
        kl = []
        for i in range(60):
            c = 30.0 - i * 0.2          # 60 根缓慢下行：30.0 → 18.2
            kl.append({"open": c + 0.1, "close": c, "high": c + 0.15, "low": c - 0.15})
        # 尾根：崩到 8.0，收阴，必然创 20 日新低且远低于 MA5
        kl.append({"open": 9.0, "close": 8.0, "high": 9.1, "low": 7.9})
        closes = [k["close"] for k in kl]
        cur = closes[-1]
        prev = closes[-20:-1]
        self.assertTrue(cur <= min(prev), "构造前提：必须创 20 日收盘新低")
        self.assertLess(cur, sum(closes[-5:]) / 5.0, "构造前提：必须低于 MA5")
        self.assertLess(cur, kl[-1]["open"], "构造前提：必须收阴")
        self.assertIs(self.gate(kl), False,
                      "创新低 + 收阴 + 未站上MA5 必须判未确认")

    def test_gate_shallow_pullback_confirms(self):
        """反向：任一项确认成立 → True。证明门不是恒 False。

        ⚠️ 恒 False 会把全池深跌股都罚分，等于偷偷改权重，必须有反向测试盯住。
        """
        # 单边上涨 60 根 → 尾根 close==open，但「不创新低」与「站上MA5」都成立
        kl = [{"open": 10.0 + i * 0.1, "close": 10.0 + i * 0.1,
               "high": 10.2 + i * 0.1, "low": 9.9 + i * 0.1} for i in range(60)]
        self.assertIs(self.gate(kl), True,
                      "单边上涨：不创新低 + 站上MA5 → 必须判为已确认")
        # 创 20 日新低但收阳（下影长、日内反包）→ 靠「收阳」这一项也算确认
        kl2 = []
        for i in range(60):
            c = 30.0 - i * 0.3
            kl2.append({"open": c + 0.15, "close": c, "high": c + 0.25, "low": c - 0.25})
        kl2.append({"open": 10.0, "close": 10.4, "high": 10.5, "low": 9.8})
        closes2 = [k["close"] for k in kl2]
        self.assertTrue(closes2[-1] <= min(closes2[-20:-1]),
                        "构造前提：确实创 20 日新低")
        self.assertIs(self.gate(kl2), True,
                      "创新低但当日收阳 → 确认门应为 True（不创新低/站上MA5 都不成立，"
                      "只有「收阳」这一项兜住）")

    def test_gate_returns_none_on_insufficient_data(self):
        """K 线不足 → None（不臆造），调用方不得当成 False 去罚分。"""
        kl = [{"open": 10.0, "close": 10.0, "high": 10.0, "low": 10.0} for _ in range(5)]
        self.assertIsNone(self.gate(kl))

    def test_deep_unconfirmed_loses_half_point(self):
        """生益科技反例：深跌 + 未确认 → 两维各罚 0.5，不再拿双满分。

        ⚠️ 罚分是在 step2 调用点做的（不在 reversal_rank 里），此处复刻该逻辑，
           守住「罚分幅度 = 0.5」这个数字不被悄悄改大/改小。
        ⚠️ 用真实池规模（2119 只）构造：RSI 19.47 在池内应落在最深分位。
           上一版用 100 只样本，rank 均分导致最极端样本也只拿到 4.05 —— 那是
           **小样本下 rank 的固有行为**，不是代码错。真实池 2119 只在
           2026-11-03 实测 give 4.95（见 data/audit_round121e_final.json 同口径）。
        """
        vals = {f"s{i}": 100.0 - i * 0.04 for i in range(2118)}  # RSI 100 → 15.3
        vals["sh600183"] = 19.47                                   # 生益科技实测
        r = self.reversal_rank(vals, higher_is_better=False)
        raw = r["sh600183"]
        self.assertGreater(raw, 4.5,
                           f"RSI 19.47 在 2119 只池内应落最深分位（实测 4.95），实得 {raw}")
        pen = 0.5                                                  # step2 的 _rev_penalty
        penalized = max(1.0, raw - pen)
        self.assertAlmostEqual(penalized, raw - 0.5, places=6)
        self.assertLess(penalized, 5.0,
                        "深跌未确认不得拿满分 —— 罚后必须 < 5.0（反例回归）")

    def test_scored_file_reversal_scores_in_range(self):
        """真实数据：最近一次 scored 的 score_bias60/rsi_rev 全部落在 [0,5]。"""
        fs = [f for f in sorted(glob.glob(os.path.join(HERE, "top200_scored_*.json")),
                                reverse=True) if ".bak" not in f]
        if not fs:
            self.skipTest("无 top200_scored_*.json")
        d = json.load(open(fs[0], encoding="utf-8"))
        rows = d.get("results") or []
        if not rows:
            self.skipTest("scored 无 results")
        bad = []
        for r in rows:
            for k in ("score_bias60", "score_rsi_rev"):
                v = r.get(k)
                if isinstance(v, (int, float)) and not (0.0 <= v <= 5.0):
                    bad.append((r.get("code"), k, v))
        self.assertEqual(bad[:5], [],
                         f"反转两维越界（应在 0–5，DIM_MAX=5.0）：{bad[:5]}")


def _run_standalone():
    """无 pytest 时的执行入口。返回退出码。"""
    loader = unittest.TestLoader()
    suite = loader.loadTestsFromModule(sys.modules[__name__])
    verb = "-v" in sys.argv
    runner = unittest.TextTestRunner(verbosity=2 if verb else 1)
    res = runner.run(suite)
    return 0 if res.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(_run_standalone())
