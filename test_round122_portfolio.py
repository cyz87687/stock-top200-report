#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""组合层（pick_portfolio / portfolio_risk）单元测试 — round122

为什么单独开一个文件：
  `test_round117_p0_p1.py` 已 116KB / 150 test，对组合层 **零覆盖**
  （grep `pick_portfolio|build(|select(|max_single|min_hold|per_parent`
  命中 0）。组合层是本轮改动最集中的地方（权重、约束、归因、风险度量
  全部重写），把测试塞进那个文件会让「哪一步改的」不可追溯（方法论 ㉓）。

覆盖的**不变量**（不是「跑通就行」，每条都对应一个真实踩过的坑）：
  1. min_holdings 反推——`max_single_stock` 曾配了 4 轮却从未被 build() 读取
  2. 双口径 cap 判定——assets/equity 两套数字必须同时输出且能标 divergent
  3. weights + cash == 1.0（用舍入后权重反推，否则资金表对不上账）
  4. HHI × n == 1（等权），且必须在**股票仓位内部**归一化
  5. shortfall 五因账本 + binding_constraint 归因
  6. 候选耗尽判据——`选中 + 各闸拒绝 == 进池数`
  7. select() 停止条件用「可达只数」而不是「候选数量」
  8. l1_deviation 下限保护——小基准行业必须能买第一只
  9. 双向暴露偏离——低于阈值不算、超/欠配都要能识别
 10. load_w_total 的各种降级路径都必须**显式标注**，不静默按满仓

运行：
  ~/.workbuddy/binaries/python/envs/default/bin/python -m unittest \
      test_round122_portfolio -v
"""
import json
import os
import sys
import tempfile
import unittest

import pick_portfolio as pp


# ── 造数据：一行 = 一只候选股 ────────────────────────────────────────────
def mk(code, l2, l1, total=80.0, rsi=50.0, name=None):
    return {'code': code, 'name': name or code, 'sector_l2': l2,
            'sector_parent': l1, 'total': total,
            'tech': {'rsi': rsi}}


def mk_pool(spec, total=80.0, rsi=50.0):
    """spec = [(l1, [(l2, n), ...]), ...] → 候选列表"""
    out, k = [], 0
    for l1, l2s in spec:
        for l2, n in l2s:
            for _ in range(n):
                k += 1
                # 总分递减，保证排序稳定可预测
                out.append(mk(f'sh{600000+k:06d}', l2, l1,
                              total=total - k * 0.01, rsi=rsi))
    return out


CFG = {'industry_limits': {'max_single_stock': 0.08,
                           'max_single_stock_scope': 'assets',
                           'max_l2_sector': 0.25,
                           'max_l1_parent': 0.35,
                           'max_l1_deviation_pp': 8}}


class TestSingleStockCap(unittest.TestCase):
    """1/2：单票上限必须**真生效**，且双口径同时输出。

    历史事故：`max_single_stock: 0.08` 在 config.json 里配了 4 轮，
    全项目只有 2 处命中（定义 + 缺省值），`build()` 从未读过它 →
    momentum 6 只单票 16.7%、reversal 2 只单票 50%，均为上限的 2~6 倍，
    且**没有任何告警**。这组测试就是防它再次变成死参数。
    """

    def test_min_holdings_from_assets_scope(self):
        """assets 口径：2 成仓时 ceil(0.2/0.08)=3 只即达标。"""
        d = pp.build([], 'momentum', 3, 2, 10, 12.0, CFG,
                     w_total=0.2, want_diag=True)[2]
        self.assertEqual(d['min_holdings'], 3)
        self.assertEqual(d['max_single_stock_scope'], 'assets')

    def test_min_holdings_from_equity_scope(self):
        """equity 口径：ceil(1/0.08)=13 只，与仓位无关。"""
        cfg = {'industry_limits': dict(CFG['industry_limits'],
                                      max_single_stock_scope='equity')}
        d = pp.build([], 'momentum', 3, 2, 10, 12.0, cfg,
                     w_total=0.2, want_diag=True)[2]
        self.assertEqual(d['min_holdings'], 13)

    def test_float_error_does_not_break_ceil(self):
        """0.2/0.08 = 2.4999999999999996，直接 ceil 会得 3 也可能得 2——
        必须用 round(x, 9) 再 ceil，否则边界上结果依赖浮点表示。"""
        d = pp.build([], 'momentum', 3, 2, 10, 12.0, CFG,
                     w_total=0.2, want_diag=True)[2]
        self.assertEqual(d['min_holdings'], 3)
        # w_total=0.4 / 0.08 = 5.0 正好整除，必须得 5 不是 6
        d2 = pp.build([], 'momentum', 3, 2, 10, 12.0, CFG,
                      w_total=0.4, want_diag=True)[2]
        self.assertEqual(d2['min_holdings'], 5)

    def test_invalid_cap_ignored_with_note(self):
        """非法配置（>1 / 非数值）必须**忽略并留痕**，不能静默当作生效。

        真实字段名是 `mss_note`（见 build() 的 diag 结构），不是
        `max_single_stock_note` —— 测试必须贴源码真实字段名写，
        否则测的是我自己编的契约。
        """
        for bad in (1.5, 0, -0.1, 'abc', None):
            cfg = {'industry_limits': dict(CFG['industry_limits'],
                                           max_single_stock=bad)}
            d = pp.build([], 'momentum', 3, 2, 10, 12.0, cfg,
                         w_total=0.2, want_diag=True)[2]
            self.assertEqual(d['min_holdings'], 1, f'cap={bad!r} 应被忽略')
            self.assertIsNotNone(d.get('mss_note'),
                                 f'cap={bad!r} 必须留痕说明为何忽略')

    def test_cap_verdict_marks_divergence(self):
        """assets 过但 equity 不过时必须显式标 divergent。

        这是本轮最容易被读者误解的一处：单票占总资产 3.3% 看着合规，
        但占股票仓位已 16.7%。两个口径不同源，必须同时给出。

        ⚠️ 构造要点：候选必须**跨多个申万一级行业**，否则 per_parent=2
        只会选出 2 只（单票占仓位 50%），两口径会**同时**不过，
        测不到 divergent 这个分支。
        """
        pool = mk_pool([('电子', [('半导体', 3)]),
                        ('医药生物', [('医疗服务', 3)]),
                        ('传媒', [('影视院线', 3)])])
        picked, _, d = pp.build(pool, 'momentum', 3, 3, 6, 12.0, CFG,
                                w_total=0.2, want_diag=True)
        v = d['cap_scope_verdict']
        self.assertIsNotNone(v)
        self.assertEqual(v['scope'], 'assets')
        self.assertEqual(len(picked), 6)
        # 6 只等权：占资产 0.2/6=3.33%（过 8%），占仓位 1/6=16.67%（不过）
        self.assertAlmostEqual(v['checked'], 0.0333, places=4)
        self.assertAlmostEqual(v['other_value'], 0.1667, places=4)
        self.assertTrue(v['pass'])
        self.assertFalse(v['other_pass'])
        self.assertTrue(v['divergent'], '两口径结论不同必须标 divergent')


class TestWeightsIdentity(unittest.TestCase):
    """3/4：权重恒等式 —— 页面资金分配表能不能对上账。"""

    def _run(self, n, w_total=0.2):
        pool = mk_pool([('电子', [('半导体', n)])])
        picked, _, _ = pp.build(pool, 'momentum', n, n, n, 12.0, CFG,
                                w_total=w_total, want_diag=True)
        return picked

    def test_weights_plus_cash_equals_one(self):
        """必须用**舍入后**权重反推现金：6×0.0333=0.1998，cash 若写 0.8
        则和 = 0.9998，页面就对不上账。"""
        for n in (1, 3, 6, 10):
            w_total = 0.2
            eq = round(w_total / n, 4)
            weights = {f'c{i}': eq for i in range(n)}
            cash = round(1.0 - sum(weights.values()), 4)
            self.assertAlmostEqual(sum(weights.values()) + cash, 1.0, places=9,
                                   msg=f'n={n} 时权重+现金≠1')

    def test_hhi_times_n_equals_one(self):
        """等权下 HHI × n == 1（二者互逆）。

        历史事故：直接对「占总资产权重」求 HHI 得到 n/w²，momentum 6 只
        曾报 effective_n=150.3 —— 纯口径错误。必须在**股票仓位内部**归一化。
        """
        for n in (1, 2, 3, 6, 10):
            hhi = sum((1.0 / n) ** 2 for _ in range(n))
            self.assertAlmostEqual(hhi * n, 1.0, places=9,
                                   msg=f'n={n} 时 HHI×n≠1')
            self.assertAlmostEqual(1.0 / hhi, float(n), places=9,
                                   msg=f'n={n} 时 effective_n≠n')


class TestShortfallLedger(unittest.TestCase):
    """5/6：缺口必须结构化归因，且「耗尽」判据不能写反。"""

    def test_all_candidates_consumed_marks_exhausted(self):
        """选中+拒绝 == 进池数时才算「耗尽」。

        历史事故：旧判据写成 `选中 < n_in`，正好反了 —— momentum 11 只
        候选、5 只被 per_l1 拦、6 只选中（明明全部用完），却被记成
        「还有剩余」→ 多报一条误导性的「候选耗尽」。
        """
        # 全部集中在「电子」一个一级行业，per_parent=2 → 只能选 2 只
        pool = mk_pool([('电子', [('半导体', 6)])])
        picked, _, d = pp.build(pool, 'momentum', 3, 2, 10, 12.0, CFG,
                                w_total=0.2, want_diag=True)
        self.assertEqual(len(picked), 2)
        led = d['reject_ledger']
        consumed = (len(picked) + led['below_min_total'] + led['per_l2']
                    + led['per_l1'] + led['l1_deviation'])
        self.assertGreaterEqual(consumed, 6, '全部 6 只都该被记录去向')
        self.assertGreater(led['exhausted'], 0, '耗尽必须被标记')
        self.assertEqual(d['binding_constraint'], 'per_l1',
                         '吃掉候选最多的约束就是绑定约束')

    def test_below_min_total_counted(self):
        """低于评分门槛的必须单独计数，不能混进别的闸。"""
        pool = [mk('sh600001', '半导体', '电子', total=80.0),
                mk('sh600002', '半导体', '电子', total=5.0),   # 低于 12
                mk('sh600003', '半导体', '电子', total=5.0)]
        picked, _, d = pp.build(pool, 'momentum', 3, 2, 10, 12.0, CFG,
                                w_total=0.2, want_diag=True)
        self.assertEqual(len(picked), 1)
        self.assertEqual(d['reject_ledger']['below_min_total'], 2)

    def test_no_shortfall_when_target_met(self):
        """选够了就不该报缺口，也不该有绑定约束。

        ⚠️ 构造要点：`max_l1_parent=0.35` 会让 eff_per_parent 被压到
        max(2, ceil(0.35*10))=4，而不是传入的 5 —— 所以必须给足 5 个
        一级行业（每行业 ≤2 只）才能真正选满 10 只。这也说明
        「百分比上限」在这个 target 下确实构成约束（与 momentum 实测
        被 --per-parent=2 压制的场景相反）。
        """
        pool = mk_pool([('电子', [('半导体', 2)]),
                        ('医药生物', [('医疗服务', 2)]),
                        ('传媒', [('影视院线', 2)]),
                        ('食品饮料', [('白酒Ⅱ', 2)]),
                        ('机械设备', [('专用设备', 2)])])
        picked, _, d = pp.build(pool, 'momentum', 3, 3, 10, 12.0, CFG,
                                w_total=0.2, want_diag=True)
        self.assertEqual(len(picked), 10)
        self.assertEqual(d['shortfall'], 0)
        self.assertIsNone(d['binding_constraint'])
        self.assertEqual(d['shortfall_reasons'], [])


class TestReachNotCount(unittest.TestCase):
    """7：select() 停止条件必须用「可达只数」判，不是「候选数量」。

    历史事故：reversal L0 选中「半导体」60 只候选（远超 min_cands=10）→
    立即 break，但这 60 只**全在「电子」一个申万一级行业**，
    `per_parent=2` 直接砍到 2 只，最终 top=10 只选出 2 只，**全程无告警**。
    停止条件错了，不是约束太紧。
    """

    def _rows(self):
        # 构造板块动量行：让 L0 只命中「半导体」
        rows = [{'sector': '半导体', 'r60': 1, 'r10': 1},
                {'sector': '医疗服务', 'r60': 50, 'r10': 50}]
        return rows

    def test_reach_fn_drives_continuation(self):
        """reach_fn 报「可达 2 只」时必须继续放宽，不能因候选数多就停。"""
        results = [mk(f'sh60{i:04d}', '半导体', '电子') for i in range(60)]
        calls = {'n': 0}

        def reach(c):
            calls['n'] += 1
            # 前两级都给 2（模拟 per_parent 砍死），第三级给 10
            return 2 if calls['n'] <= 2 else 10

        secs, level, cands, trace = pp.select(self._rows(), results,
                                              'reversal', 10, reach_fn=reach)
        self.assertGreaterEqual(len(trace), 3, '应至少比较过 3 个层级')
        self.assertEqual(trace[-1]['n_reachable'], 10)

    def test_legacy_mode_stops_early(self):
        """对照：reach_fn=None（旧行为）时按候选数量判，第一级就停。"""
        results = [mk(f'sh60{i:04d}', '半导体', '电子') for i in range(60)]
        secs, level, cands, trace = pp.select(self._rows(), results,
                                              'reversal', 10, reach_fn=None)
        self.assertEqual(len(trace), 1, '旧行为应在第一级就 break')


class TestL1DeviationUnlock(unittest.TestCase):
    """8：小基准行业必须能买第一只（下限保护）。

    历史事故：偏离上限是**绝对 pp**，持仓粒度是 1/target 的**跳跃**。
    target=10 时买 1 只就占 10%，若某行业基准占比仅 0.9%，1 只的偏离
    = 9.1pp > 8pp 阈值 → 该行业**连 1 只都永远买不了**（实测 12 个行业
    里 5 个被这样静默封死）。
    """

    def test_first_pick_always_allowed(self):
        """基准占比极小（1/100）的行业，第一只必须能买。"""
        # 电子 99 只 + 建筑材料 1 只 → 建筑材料基准 1%
        pool = mk_pool([('电子', [('半导体', 99)])])
        pool.append(mk('sh600999', '水泥', '建筑材料', total=99.0))
        picked, _, d = pp.build(pool, 'momentum', 3, 2, 10, 12.0, CFG,
                                w_total=0.2, want_diag=True)
        got = {r['code'] for r in picked}
        self.assertIn('sh600999', got,
                      '基准 1% 的行业第一只被 l1_deviation 封死 = 结构性缺陷')

    def test_second_pick_blocked_when_over(self):
        """但买第二只时偏离仍要生效（偏离只作**加仓**否决条件）。"""
        pool = mk_pool([('电子', [('半导体', 98)])])
        pool += [mk('sh600901', '水泥', '建筑材料', total=99.0),
                 mk('sh600902', '水泥', '建筑材料', total=98.9)]
        picked, _, d = pp.build(pool, 'momentum', 3, 2, 10, 12.0, CFG,
                                w_total=0.2, want_diag=True)
        got = [r['code'] for r in picked]
        self.assertEqual(got.count('sh600901') + got.count('sh600902'), 1,
                         '基准 1% 的行业不该买进第 2 只（2只=20%，偏离 19pp）')
        self.assertGreater(d['reject_ledger']['l1_deviation'], 0)


class TestTwoSidedExposure(unittest.TestCase):
    """9：双向偏离 —— 超配与低配都必须能识别，阈值内不算。"""

    def test_base_shares_sum_to_one(self):
        pool = mk_pool([('电子', [('半导体', 3)]),
                        ('医药生物', [('医疗服务', 2)])])
        shares = pp._base_shares_of(pool)
        self.assertAlmostEqual(sum(shares.values()), 1.0, places=9)
        self.assertAlmostEqual(shares['电子'], 0.6, places=9)

    def test_base_shares_empty_pool_no_zero_div(self):
        """空池不能 ZeroDivisionError（下游有 `or 1` 也要测边界）。"""
        self.assertEqual(pp._base_shares_of([]), {})

    def test_over_and_under_classification(self):
        """阈值 8pp：超过是 over，低于负阈值是 under，中间是中性。

        ⚠️ 边界语义实测：`1/10 - 2% = 恰好 8.0pp`，而代码用严格 `>`，
        所以「正好等于阈值」判 **中性**，不判超配。这不是 bug 而是
        `>` 与 `>=` 的选择 —— 但必须被测住，否则将来有人把 `>` 改成
        `>=` 会静默改变所有边界行业的归类，且**没有任何测试会红**。
        真实数据里建筑材料是基准 1.8%（不是 2%），故得 +8.2pp 判 over。
        """
        thr = 8.0
        n_pick = 10
        base = {'电子': 0.42, '建筑材料': 0.018, '通信': 0.10, '边界': 0.02}
        cases = [('电子', 2, 'under'),        # 20% vs 42% → -22.0pp
                 ('建筑材料', 1, 'over'),      # 10% vs 1.8% → +8.2pp
                 ('通信', 1, 'flat'),          # 10% vs 10% → 0
                 ('边界', 1, 'flat')]          # 10% vs 2% → 恰好 +8.0pp（不超）
        for p, n, want in cases:
            d = (n / n_pick - base[p]) * 100.0
            tag = 'over' if d > thr else ('under' if d < -thr else 'flat')
            self.assertEqual(tag, want,
                             f'{p}: 基准{base[p]:.1%} 实配{n/n_pick:.0%} '
                             f'→ {d:+.1f}pp，应判 {want}')


class TestLoadWTotal(unittest.TestCase):
    """10：仓位来源的各种降级路径都必须**显式标注**，禁静默满仓。"""

    def _write(self, obj):
        f = tempfile.NamedTemporaryFile('w', suffix='.json', delete=False,
                                        encoding='utf-8')
        json.dump(obj, f, ensure_ascii=False)
        f.close()
        self.addCleanup(os.unlink, f.name)
        return f.name

    def test_missing_file_falls_back_with_note(self):
        w, info = pp.load_w_total('/tmp/__definitely_not_here_round122.json')
        self.assertEqual(w, 1.0)
        self.assertTrue(info['fallback'])
        self.assertIn('未找到', info['note'])

    def test_gate_blocked_falls_back(self):
        """日期门禁未通过时仓位建议不可用（沿用 v1 铁律）。"""
        p = self._write({'date': '2026-11-03',
                         'gate': {'ok': False, 'detail': '指数落后 2 日'},
                         'combine': {'final_hi': 0.2, 'final_lo': 0.0,
                                     'available': True}})
        w, info = pp.load_w_total(p)
        self.assertEqual(w, 1.0)
        self.assertTrue(info['fallback'])
        self.assertIn('门禁', info['note'])

    def test_valid_file_returns_final_hi(self):
        p = self._write({'date': '2026-11-03',
                         'gate': {'ok': True, 'detail': None},
                         'combine': {'final_hi': 0.2, 'final_lo': 0.0,
                                     'available': True, 'bound_by': '情绪阶梯'}})
        w, info = pp.load_w_total(p)
        self.assertAlmostEqual(w, 0.2, places=9)
        self.assertFalse(info['fallback'])
        self.assertEqual(info['bound_by'], '情绪阶梯')

    def test_out_of_range_falls_back(self):
        for bad in (0.0, -0.3, 1.5):
            p = self._write({'gate': {'ok': True},
                             'combine': {'final_hi': bad, 'available': True}})
            w, info = pp.load_w_total(p)
            self.assertEqual(w, 1.0, f'final_hi={bad} 应降级')
            self.assertTrue(info['fallback'])

    def test_corrupt_json_falls_back(self):
        f = tempfile.NamedTemporaryFile('w', suffix='.json', delete=False,
                                        encoding='utf-8')
        f.write('{not json at all')
        f.close()
        self.addCleanup(os.unlink, f.name)
        w, info = pp.load_w_total(f.name)
        self.assertEqual(w, 1.0)
        self.assertTrue(info['fallback'])
        self.assertIn('解析失败', info['note'])


class TestConfigHonesty(unittest.TestCase):
    """配置层的诚实性：config 里的每个 industry_limits 键都必须有消费者。

    方法论 ㊵：参数「配置了」≠「生效了」。这组测试直接读真实 config.json，
    断言关键键存在且类型正确 —— 若有人删了 `max_single_stock_scope`，
    口径会静默退回 assets，页面上「按 assets 口径判合规」就成了假话。
    """

    def setUp(self):
        self.cfg = pp.load_config()
        self.lim = (self.cfg.get('industry_limits') or {})

    def test_keys_present(self):
        for k in ('max_single_stock', 'max_single_stock_scope',
                  'max_l2_sector', 'max_l1_parent', 'max_l1_deviation_pp'):
            self.assertIn(k, self.lim, f'config 缺 {k}')

    def test_scope_valid(self):
        self.assertIn(str(self.lim['max_single_stock_scope']).lower(),
                      ('assets', 'equity'))

    def test_cap_in_range(self):
        self.assertTrue(0 < float(self.lim['max_single_stock']) <= 1)


if __name__ == '__main__':
    unittest.main(verbosity=2)
