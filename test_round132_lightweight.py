#!/usr/bin/env python3
"""round132 P0：主页面轻量化（仅核心个股评分）+ 过程/预测内容迁独立页 的回归测试。

设计边界（与 gen_report_top200.py 的 SHOW_CORE_ONLY 契约一一对应）：
  ① 源码静态：开关、收集器、7 个迁移点、切片先存后删、独立页写盘在生成器内部
  ② 产物：主页不含任何过程分析/已证伪预测；核心评分与硬披露仍在；锚点无死链
  ③ 产物：独立页承接全部迁出内容、结构完整、固定入口与归档一致
  ④ 回退：SHOW_CORE_ONLY=False 分支仍在（禁止把旧单页分支删掉）

⚠️ 日期无关：从 index.html 的实际链接解析独立页文件名，不硬编码日期。
⚠️ 若产物尚未生成（如纯源码 checkout），产物类断言 skip，静态断言仍然跑。
"""

import os
import re
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(HERE, "gen_report_top200.py")
MAIN = os.path.join(HERE, "index.html")

# 迁出到独立页的内容（主页必须为 0，独立页必须出现）
MOVED_KEYS = (
    "🤖 Transformer 方向预测",
    "⏱ T+1 / T0 可执行性",
    "🧮 多因子精选组合",
    "低 IC 悖论",
    "📊 推荐跟踪",
    "🎯 每日 AI 焦点池",
    "盘面复盘点评",
    "id=\"sec-analysis\"",
    "id=\"sec-market\"",
    "id=\"sec-model-history\"",
    "47 因子排名无消息",       # 页脚沿革段（主页 0 / 独立页 ≥1）
    "round34「quality",        # 同上
)

# 主页必须保留的核心评分要素 + 项目硬规则要求留在决策页的披露
KEPT_KEYS = (
    "id=\"sec-decision\"",
    "id=\"sec-list\"",
    "id=\"sec-notes\"",
    "class=\"pagination\"",
    "<table",
    "面板盲区",          # 三条数据可信度披露之一（页面必须说实话）
    "截面规模",
    "目标周期",          # round122 P0-4 证据缺口横幅，不得依附可选区块
    "禁称「经验证的 alpha」",  # 页脚沿革迁走后必须保留的浓缩硬披露
)


def _read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


class TestSourceContract(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.src = _read(SRC)

    def test_01_switch_is_on_and_documented(self):
        self.assertIn("SHOW_CORE_ONLY = True", self.src)
        self.assertIn("round132 P0", self.src)

    def test_02_detail_collector_defined(self):
        self.assertIn("_DETAIL_PARTS = []", self.src)
        self.assertIn("def _detail_append(fragment):", self.src)
        # 收集器必须引用 html_parts（回退分支），且不能吞掉 fragment
        self.assertIn("        html_parts.append(fragment)", self.src)

    def test_03_seven_blocks_routed_to_detail(self):
        targets = (
            "_detail_append(f'''\n    <!-- 复盘点评",
            "_detail_append(_regime_fc_html)",
            "_detail_append(_transformer_card_html)",
            "_detail_append(_t1_t0_card_html)",
            "_detail_append(f'''\n    <!-- v3.9 多因子精选组合",
            "_detail_append(_focus_pool_html(_focus_pool))",
            "_detail_append(f'''\n    <!-- v2.41 P0-4 推荐跟踪",
        )
        for t in targets:
            with self.subTest(target=t[:48]):
                self.assertEqual(self.src.count(t), 1, f"迁移点缺失或重复: {t}")

    def test_04_analysis_slice_saved_before_delete(self):
        """原实现 del 后直接丢弃 → 现在必须先 append 到 _DETAIL_PARTS。"""
        m = re.search(
            r"if not SHOW_ANALYSIS:\n(.*?)    del html_parts\[_ANALYSIS_SLICE_START:\]",
            self.src, flags=re.S)
        self.assertIsNotNone(m, "找不到 SHOW_ANALYSIS 切片删除点")
        self.assertIn("_DETAIL_PARTS.append(", m.group(1))
        self.assertIn("html_parts[_ANALYSIS_SLICE_START:]", m.group(1))

    def test_05_detail_page_written_inside_generator(self):
        """项目铁律：一次性后处理脚本不在日链 = 必然陈旧。"""
        self.assertIn('f"analysis_{_data_date}.html"', self.src)
        self.assertIn('"analysis.html"', self.src)
        # 独立页生成必须包在 try/except 里，失败不得拖垮主页
        self.assertIn("独立分析页生成失败（主页不受影响）", self.src)

    def test_06_rollback_branch_preserved(self):
        self.assertIn("if SHOW_CORE_ONLY:", self.src)
        self.assertIn("else:\n        html_parts.append(fragment)", self.src)
        # 旧版导航链接完整保留在 else 分支
        self.assertIn('<a href="#sec-mf-pick">② 买什么</a>', self.src)
        self.assertIn('<a href="#sec-focus-pool">④ 焦点池</a>', self.src)

    def test_07_hero_market_and_digest_migrated(self):
        self.assertIn("_hero_market_html = f'''", self.src)
        self.assertIn("_ms_digest_full = _ms_digest_html", self.src)
        self.assertIn("{_hero_market_html}", self.src)

    def test_08_footer_history_migrated_but_disclosure_kept(self):
        """页脚沿革长文迁独立页，但硬披露必须留一句在主页。"""
        self.assertIn("id=\"sec-model-history\"", self.src)
        self.assertIn("页脚沿革长文已迁独立页", self.src)
        self.assertIn("禁称「经验证的 alpha」", self.src)
        self.assertIn("准入闸门 G5 未过", self.src)


class TestPageArtifacts(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not os.path.exists(MAIN):
            raise unittest.SkipTest("index.html 尚未生成，跳过产物断言")
        cls.main = _read(MAIN)
        links = re.findall(r'href="(analysis_[0-9-]+\.html)', cls.main)
        if not links:
            raise unittest.SkipTest("主页未找到独立页链接（SHOW_CORE_ONLY=False 或未重生成）")
        cls.detail_path = os.path.join(HERE, links[0])
        if not os.path.exists(cls.detail_path):
            raise unittest.SkipTest(f"独立页产物缺失: {cls.detail_path}")
        cls.detail = _read(cls.detail_path)

    def test_11_main_has_no_moved_blocks(self):
        for k in MOVED_KEYS:
            with self.subTest(key=k):
                self.assertEqual(self.main.count(k), 0,
                                 f"主页仍含已迁出内容: {k}")

    def test_12_main_keeps_core_scoring_and_disclosures(self):
        for k in KEPT_KEYS:
            with self.subTest(key=k):
                self.assertIn(k, self.main, f"主页丢失核心要素: {k}")

    def test_13_main_anchor_links_no_dead_links(self):
        nav = re.search(r'<nav class="nav-bar">(.*?)</nav>', self.main, flags=re.S)
        self.assertIsNotNone(nav)
        ids = set(re.findall(r'id="([^"]+)"', self.main))
        for anchor in re.findall(r'href="#([^"]+)"', nav.group(1)):
            with self.subTest(anchor=anchor):
                self.assertIn(anchor, ids, f"主页导航死链 #{anchor}")

    def test_14_detail_has_all_moved_blocks(self):
        for k in MOVED_KEYS:
            with self.subTest(key=k):
                self.assertIn(k, self.detail, f"独立页漏收: {k}")

    def test_15_detail_structure_complete(self):
        for tag in ("<!DOCTYPE html>", "<html", "<head", "</head>",
                    "<body", "</body>", "</html>"):
            with self.subTest(tag=tag):
                self.assertIn(tag, self.detail)
        self.assertIn("id=\"sec-methodology\"", self.detail)
        self.assertIn("不参与每日选股决策", self.detail)
        self.assertIn("禁当策略收益", self.detail)
        # 独立页不许夹带主页的表格交互脚本
        self.assertNotIn("<script", self.detail)

    def test_16_detail_anchor_links_no_dead_links(self):
        nav = re.search(r'<nav class="nav-bar">(.*?)</nav>', self.detail, flags=re.S)
        self.assertIsNotNone(nav)
        ids = set(re.findall(r'id="([^"]+)"', self.detail))
        for anchor in re.findall(r'href="#([^"]+)"', nav.group(1)):
            with self.subTest(anchor=anchor):
                self.assertIn(anchor, ids, f"独立页导航死链 #{anchor}")

    def test_17_fixed_entry_matches_dated_archive(self):
        fixed = os.path.join(HERE, "analysis.html")
        self.assertTrue(os.path.exists(fixed), "缺少 analysis.html 固定入口")
        self.assertEqual(_read(fixed), self.detail,
                         "固定入口与带日期归档内容不一致")

    def test_18_detail_page_is_meaningfully_lighter_than_main(self):
        self.assertLess(len(self.detail), len(self.main) / 5,
                        "独立页体积异常（可能误把主表格搬过去了）")


if __name__ == "__main__":
    unittest.main(verbosity=2)
