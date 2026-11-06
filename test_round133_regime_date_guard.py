#!/usr/bin/env python3
"""round133（2026-11-06 裁决 2）「日期不一致数据不要展示」回归测试。

覆盖三层，缺一不可：
  ① 守卫模块本身（唯一实现的日期判据）
  ② pick_portfolio.load_w_total：陈旧预算既不消费、也不把数值留给下游
  ③ 源码/产物契约：日更链必须传数据日期、页面不得展示陈旧数值、占位披露可见

背景：2026-11-05 `fetch_bench` 失效 → 盘上 `regime_forecast.json` 仍是 11-04，
而 `load_w_total` 只校验文件内部 `gate.ok`（11-04 自检为 True），于是 11-05
组合照常消费 `final_hi=0.5837` 并标 `budget_status=OK`。
"""
import json
import os
import re
import tempfile
import unittest

import regime_date_guard as g
import pick_portfolio as pp

HERE = os.path.dirname(os.path.abspath(__file__))
GEN_SRC = os.path.join(HERE, "gen_report_top200.py")
PR_SRC = os.path.join(HERE, "portfolio_risk.py")
AUDIT_SRC = os.path.join(HERE, "audit_round112_page_selftest.py")
PP_SRC = os.path.join(HERE, "pick_portfolio.py")


def _read(p):
    with open(p, encoding="utf-8") as f:
        return f.read()


def _regime_fixture(**over):
    d = {'date': '2026-11-05',
         'gate': {'ok': True, 'detail': None},
         'combine': {'final_hi': 0.5837, 'final_lo': 0.2,
                     'available': True, 'bound_by': '风险预算'}}
    d.update(over)
    f = tempfile.NamedTemporaryFile('w', suffix='.json', delete=False,
                                    encoding='utf-8')
    json.dump(d, f, ensure_ascii=False)
    f.close()
    return f.name


class TestGuardCore(unittest.TestCase):
    def test_01_normalize_date_tolerates_timestamp(self):
        self.assertEqual(g.normalize_date('2026-11-05 15:00:00'), '2026-11-05')
        self.assertEqual(g.normalize_date('截至2026-11-05收盘'), '2026-11-05')
        self.assertIsNone(g.normalize_date(None))
        self.assertIsNone(g.normalize_date('无日期'))

    def test_02_equal_date_is_ok(self):
        c = g.check_artifact_date('2026-11-05 15:00', '2026-11-05')
        self.assertTrue(c['ok'])
        self.assertEqual(c['status'], g.STATUS_OK)

    def test_03_stale_and_missing_date_both_block(self):
        c = g.check_artifact_date('2026-11-04', '2026-11-05')
        self.assertFalse(c['ok'])
        self.assertEqual(c['status'], g.STATUS_STALE)
        self.assertIn('≠', c['reason'])
        c2 = g.check_artifact_date(None, '2026-11-05')
        self.assertFalse(c2['ok'])
        self.assertEqual(c2['status'], g.STATUS_NO_ARTIFACT_DATE)
        c3 = g.check_artifact_date('2026-11-05', None)
        self.assertFalse(c3['ok'])
        self.assertEqual(c3['status'], g.STATUS_UNVERIFIED)

    def test_04_payload_uses_data_date_fallback_key(self):
        self.assertTrue(g.check_payload({'data_date': '2026-11-05'},
                                        '2026-11-05')['ok'])
        self.assertFalse(g.check_payload({'date': '2026-11-04'},
                                         '2026-11-05')['ok'])

    def test_05_hidden_notice_discloses_both_dates(self):
        c = g.check_artifact_date('2026-11-04', '2026-11-05')
        s = g.hidden_notice(c, '风险预算')
        self.assertIn('2026-11-04', s)
        self.assertIn('2026-11-05', s)
        self.assertIn('不展示', s)

    def test_06_budget_source_cannot_be_trusted_by_status_alone(self):
        # 历史产物仍标 OK，但日期是 T-1 → 必须判陈旧
        src = {'date': '2026-11-04', 'budget_status': 'OK', 'final_hi': 0.5837}
        c = g.budget_source_stale(src, '2026-11-05')
        self.assertFalse(c['ok'])
        # 日期一致但产物自带 STALE 标记 → 也不能放行
        c2 = g.budget_source_stale({'date': '2026-11-05',
                                    'budget_status': 'STALE_DATE_HIDDEN'},
                                   '2026-11-05')
        self.assertFalse(c2['ok'])
        self.assertTrue(g.budget_source_stale({'date': '2026-11-05',
                                               'budget_status': 'OK'},
                                              '2026-11-05')['ok'])


class TestLoadWTotalDateGate(unittest.TestCase):
    def test_11_stale_budget_is_hidden_and_scrubbed(self):
        p = _regime_fixture(date='2026-11-04')
        self.addCleanup(os.unlink, p)
        w, info = pp.load_w_total(p, data_date='2026-11-05')
        self.assertEqual(w, 0.0)
        self.assertEqual(info['budget_status'], 'STALE_DATE_HIDDEN')
        self.assertTrue(info['hide_data'])
        self.assertFalse(info['executable'])
        self.assertEqual(info['date_check'], g.STATUS_STALE)
        self.assertEqual(info['stale_date'], '2026-11-04')
        # 陈旧数值一律不得残留，防止下游误读 info 后照单展示
        for k in ('final_hi', 'final_lo', 'bound_by', 'gate_ok'):
            self.assertIsNone(info[k], f"{k} 仍残留陈旧数值")
        self.assertIn('不展示', info['note'])

    def test_12_fresh_budget_still_works(self):
        p = _regime_fixture()
        self.addCleanup(os.unlink, p)
        w, info = pp.load_w_total(p, data_date='2026-11-05')
        self.assertAlmostEqual(w, 0.5837, places=9)
        self.assertEqual(info['budget_status'], 'OK')
        self.assertEqual(info['date_check'], g.STATUS_OK)
        self.assertTrue(info['executable'])
        self.assertFalse(info['hide_data'])

    def test_13_offline_call_keeps_legacy_contract(self):
        """不传 data_date（单测/离线工具）时跳过时效校验，旧行为不变。"""
        p = _regime_fixture()
        self.addCleanup(os.unlink, p)
        w, info = pp.load_w_total(p)
        self.assertAlmostEqual(w, 0.5837, places=9)
        self.assertEqual(info['date_check'], 'SKIPPED_NO_DATA_DATE')
        self.assertEqual(info['budget_status'], 'OK')

    def test_14_undated_product_is_hidden(self):
        p = _regime_fixture(date=None)
        self.addCleanup(os.unlink, p)
        w, info = pp.load_w_total(p, data_date='2026-11-05')
        self.assertEqual(info['budget_status'], 'STALE_DATE_HIDDEN')
        self.assertEqual(info['date_check'], g.STATUS_NO_ARTIFACT_DATE)
        self.assertTrue(info['hide_data'])

    def test_15_full_fallback_still_hidden(self):
        """即使显式 --risk-fallback full 对照，日期不一致也不得给出可执行建议。"""
        p = _regime_fixture(date='2026-11-04')
        self.addCleanup(os.unlink, p)
        w, info = pp.load_w_total(p, fallback_mode='full',
                                  data_date='2026-11-05')
        self.assertEqual(info['budget_status'], 'STALE_DATE_HIDDEN')
        self.assertTrue(info['hide_data'])
        self.assertFalse(info['executable'])


class TestSourceContract(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.pp = _read(PP_SRC)
        cls.pr = _read(PR_SRC)
        cls.gen = _read(GEN_SRC)
        cls.audit = _read(AUDIT_SRC)

    def test_21_production_chain_passes_data_date(self):
        self.assertIn('data_date=sdate', self.pp)
        self.assertIn("load_w_total(fallback_mode=args.risk_fallback", self.pp)
        # 顺序：先拿 scored 的日期，再读预算（原顺序无法校验）
        self.assertLess(self.pp.index('sdate = data.get(\'date\')'),
                        self.pp.index('data_date=sdate'))

    def test_22_hidden_budget_writes_no_portfolio_data(self):
        self.assertIn("if rinfo.get('hide_data'):", self.pp)
        self.assertIn("out['portfolios'] = {}", self.pp)
        self.assertIn("'STALE_DATE_HIDDEN'", self.pp)

    def test_23_single_implementation_used_everywhere(self):
        for name, src in (('pick_portfolio', self.pp), ('portfolio_risk', self.pr),
                          ('gen_report', self.gen), ('page_selftest', self.audit)):
            with self.subTest(module=name):
                self.assertIn('regime_date_guard import', src)
        self.assertIn('check_payload(_o, date_str)', self.gen)
        self.assertIn('check_payload(_robj, date_str)', self.gen)
        self.assertIn("del _detail_target()[_combo_detail_at:]", self.gen)

    def test_24_audit_dead_assert_is_date_aware(self):
        self.assertIn('_rchk[\'ok\']', self.audit)
        self.assertIn('未展示', self.audit)


class TestPageArtifacts(unittest.TestCase):
    """产物断言：仅在重生成过（含 round133 占位标记）时执行。"""
    @classmethod
    def setUpClass(cls):
        main_p = os.path.join(HERE, 'index.html')
        if not os.path.exists(main_p):
            raise unittest.SkipTest('index.html 未生成')
        cls.main = _read(main_p)
        m = re.search(r'href="(analysis_[0-9-]+\.html)', cls.main)
        if not m:
            raise unittest.SkipTest('主页无独立页链接')
        det_p = os.path.join(HERE, m.group(1))
        if not os.path.exists(det_p):
            raise unittest.SkipTest('独立页缺失')
        cls.detail = _read(det_p)
        if '数据纪律：产物日期与数据日期不一致时' not in cls.detail:
            raise unittest.SkipTest('页面尚未按 round133 重生成')

    def test_31_stale_budget_number_not_shown(self):
        """陈旧 final_hi 的**风险预算语境**数值不得出现在两页。

        ⚠️ 不能用裸 '58%' 做判据：主页实测出现 156 次，全部来自个股详情卡
          （「3年收缩58%」/「+3.58%」/「技反 58% 低波」），与 regime 无关。

        ⚠️ 判据不得随当前 regime 文件漂移（2026-11-06 日更修）：
          `regime_model.py` 每日重跑会把 regime_forecast.json 更新为当日，
          final_hi 从 11-04 的 0.5837 变成 11-06 的 0.2。原实现直接拿当前
          文件的 final_hi 当「陈旧值」，当日合法数值被误判（裸 '0.2' 在
          个股详情小数里出现上千次）。根因是**判据没绑定日期守卫**。
          修法（不放宽断言）：① 固定历史事故值写法恒禁，与当前产物无关；
          ② 仅当 `regime_date_guard` 判当前产物陈旧时，才追加当前 final_hi
          的可归因写法禁令；③ 产物为当日时改验正向契约（不得残留陈旧标记）。
        """
        with open(os.path.join(HERE, 'regime_forecast.json'), encoding='utf-8') as f:
            rf = json.load(f)
        # 数据日期取主页标题（生成器写入，随当日变化，不锁死）
        m = re.search(r'\|\s*(\d{4}-\d{2}-\d{2})', self.main)
        self.assertIsNotNone(m, '主页标题缺少数据日期')
        chk = g.check_payload(rf, m.group(1))
        fh = (rf.get('combine') or {}).get('final_hi')

        # ① 固定历史陈旧值（round133 事故产物 11-04 / 0.5837）恒不得出现
        forbidden = ('0.5837', '58.37%', '风险预算 58%', '单日最大敞口 58%')
        # ② 守卫判陈旧 → 追加当前 final_hi 的可归因写法
        if not chk['ok'] and fh is not None:
            pct = f"{float(fh) * 100:.0f}%"
            forbidden = forbidden + (
                f"风险预算 {pct}", f"单日最大敞口 {pct}",
                f"{float(fh):.4f}".rstrip('0').rstrip('.'),
                f"{float(fh) * 100:.2f}%".replace('.00%', '%'),
            )
        for page, name in ((self.main, '主页'), (self.detail, '独立页')):
            with self.subTest(page=name):
                for w in forbidden:
                    self.assertNotIn(w, page, f'{name}仍展示陈旧预算写法 {w}')
                # ③ 当日产物不得残留陈旧标记（正向契约）
                if chk['ok']:
                    self.assertNotIn('STALE_DATE_HIDDEN', page,
                                     f'{name}当日预算仍被标为陈旧')

    def test_32_placeholder_is_visible_and_anchor_survives(self):
        self.assertIn('id="sec-mf-pick"', self.detail)
        self.assertIn('本期不展示', self.detail)
        self.assertIn('🧮 多因子精选组合', self.detail)
        self.assertIn('2026-11-05', self.detail)
        self.assertIn('2026-10-23', self.detail)  # 陈旧产物日期需如实披露

    def test_33_no_stale_pick_list(self):
        """被隐藏的清单不得漏出「≤10 只」标题式文案。"""
        self.assertNotIn('全页唯一的当日候选清单', self.detail)


if __name__ == '__main__':
    unittest.main(verbosity=2)
