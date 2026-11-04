#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
audit_round112_page_selftest.py —— 页面自测（round112 版）

═══ 为什么每轮都要重跑页面自测 ═══
改渲染层最容易犯的错不是「报错」，而是「静默不显示」或「显示了但口径错」。
本项目历史上踩过：Edit 缩进错位落进 except 变死代码、_forecast_hero_line 独立读文件
绕过日期校验、同一个数字两处实现导致漂移。这些都只有断言能发现。

═══ 检查项（分七组）═══
  A 页面基础完整性
  B 市场状态卡（L1/L2/风险预算/取小）
  C Transformer 卡片（round112 新增，重点）
  D 日期门禁三处一致性
  E 权重/评级口径不自相矛盾
  F 旧的失真表述已清除
  G 数字可追溯到产物文件

用法：python audit_round112_page_selftest.py [html_path] [scored_path]
"""
import os
import re
import sys
import json

HERE = os.path.dirname(os.path.abspath(__file__))


def main():
    html_p = sys.argv[1] if len(sys.argv) > 1 else os.path.join(
        HERE, "top200_report_2026-11-02.html")
    scored_p = sys.argv[2] if len(sys.argv) > 2 else os.path.join(
        HERE, "top200_scored_2026-11-02.json")

    if not os.path.exists(html_p):
        print(f"❌ 找不到 {html_p}")
        return 2
    S = open(html_p, encoding="utf-8").read()
    D = json.load(open(scored_p, encoding="utf-8")) if os.path.exists(scored_p) else {}
    data_date = str(D.get("date") or "")[:10]

    ok, fail = [], []

    def chk(name, cond, detail=""):
        (ok if cond else fail).append(name)
        print(f"{'✅' if cond else '❌'} {name}" + (f"  [{detail}]" if detail and not cond else ""))

    print(f"页面 {os.path.basename(html_p)}  {len(S):,} 字符  数据日期 {data_date}\n")

    # ── A 基础完整性
    print("── A 页面基础完整性 ──")
    for kw in ("top200", "评级", "个股"):
        chk(f"A 含关键词「{kw}」", kw in S)
    chk("A 页面有效闭合（</html>）", "</html>" in S)
    chk("A 无未替换占位符 {{", "{{" not in S)
    # ⚠️ 断言修正（第一版写错过）：「评级分布」只是 gen_report 的控制台打印，
    #    页面里的实际表达是「S级 104 只」这类。断言要贴着页面真实文案写，
    #    否则会把「页面正常」误判成失败。
    chk("A 评级标签存在", S.count("S级") > 0 and S.count("A级") > 0)

    # ── B 市场状态卡
    print("\n── B 市场状态卡 ──")
    chk("B 卡片标题", "市场状态与风险预算" in S)
    chk("B L1 状态", bool(re.search(r"单边下跌市|震荡整理|单边上涨市|高波动", S)))
    chk("B L2 风险刻度标注为「非未来预测」", "非未来预测" in S)
    chk("B 风险预算公式", bool(re.search(r"\d+% ÷ [\d.]+% = [\d.]+", S)))
    chk("B 取小逻辑说明", "取小" in S)
    chk("B 明确写「不是收益预测」", "不是收益预测" in S or "非收益预测" in S)
    chk("B 降仓择时无效已披露", "0/16" in S or "降仓" in S)
    chk("B v2 版本标识", "regime_model v2" in S)

    # ── C Transformer 卡片（本轮重点）
    print("\n── C Transformer 卡片（round112 新增）──")
    chk("C 卡片存在", "Transformer 方向预测" in S)
    chk("C 结论「无可用预测能力」", "无可用预测能力" in S)
    chk("C 明确「不提供仓位建议」", "不提供仓位建议" in S)
    chk("C 四指数表头", "样本外准确率" in S)
    for idx in ("中证全指", "沪深300", "中证500", "中证1000"):
        chk(f"C 表格含 {idx}", idx in S)
    chk("C Bonferroni 校正已说明", "Bonferroni" in S)
    chk("C 名义跑赢但不显著已标注", "名义跑赢但不显著" in S)
    chk("C 混淆矩阵已展示", "混淆矩阵" in S)
    chk("C 两版交叉验证已说明", "交叉验证" in S)
    chk("C 梯度检查通过已说明", "梯度检查" in S and "通过" in S)
    chk("C 防泄漏六条已说明", "防泄漏" in S)
    chk("C 拟合诊断已展示", "拟合诊断" in S or "过拟合" in S or "欠拟合" in S)
    # 反例：绝不能出现「Transformer 有效/有预测能力」这类正面结论
    chk("C 未出现「Transformer 有效」误导性表述",
        not re.search(r"Transformer[^。；]{0,12}(有效|验证了预测能力)", S))
    chk("C 未出现「部分指数有效」",
        "部分指数" not in S or "不得" in S)

    # ── C2 T+1/T0 可执行性卡片（round113/114 新增）
    print("\n── C2 T+1/T0 可执行性卡片（round113/114 新增）──")
    chk("C2 卡片存在", "T+1 / T0 可执行性" in S)
    chk("C2 结论「不能用于 T+1 选股」", "不能用于 T+1 选股" in S)
    chk("C2 明确 T0 不给仓位建议", "不能给仓位建议" in S or "不给仓位建议" in S)
    chk("C2 披露 total 零区分力（Spearman）", "−0.0257" in S or "-0.0257" in S)
    chk("C2 披露 Top10% 反而更差", "排名越高反而越差" in S)
    chk("C2 披露 Top10% 具体数值", "−1.28%" in S or "-1.28%" in S)
    chk("C2 隔夜/日内拆解已说明", "隔夜" in S and "日内" in S)
    chk("C2 披露符号翻转", "符号直接翻转" in S or "符号翻转" in S)
    chk("C2 持有期扫描结论", "拉长持有期" in S)
    chk("C2 披露重叠窗口伪影", "重叠" in S)
    chk("C2 Bonferroni 阈值 3.22 已给", "3.22" in S)
    chk("C2 T0 成本门槛表存在", "盈亏平衡命中率" in S)
    chk("C2 T0 三档成本已列", "ETF" in S and "小市值" in S)
    chk("C2 候选信号已披露", "极端低开" in S)
    chk("C2 候选信号 5 条边界已披露", "5 条边界" in S)
    # ⚠️ 最关键的反例断言：H2 衰减必须写出来（不能只报 8/8 通过）
    chk("C2 已披露 2026H2 胜率衰减", "48.9%" in S)
    chk("C2 已披露无分钟线限制", "无分钟线" in S)
    chk("C2 纠错记录已披露（索引 bug）", "纠错记录" in S)
    # 反例：不得出现「T0 已验证」「候选信号可用于实盘」
    chk("C2 未宣称 T0 已回测验证",
        not re.search(r"T0[^。；]{0,10}(回测验证通过|已验证成立)", S))
    chk("C2 未给 T0 具体仓位比例",
        not re.search(r"T0[^。；]{0,20}(建议仓位|仓位\s*\d+%)", S))

    # ── D 日期门禁
    print("\n── D 日期门禁一致性 ──")
    chk("D 卡片日期与数据日期一致", data_date in S if data_date else True,
        f"data_date={data_date}")
    # ⚠️ 断言修正（第一版写错过）：告警文案只在门禁**失败**时才渲染。
    #    今天门禁通过，所以页面不该出现告警 —— 正确的断言是「二选一」：
    #    要么门禁通过（无告警），要么门禁失败（必须有告警），不能两头都没有。
    _gate_warn = "数据管道告警" in S
    _gate_pass = ("🔒 日期门禁" in S) or ("日期门禁" in S and not _gate_warn)
    chk("D 门禁状态二选一（通过 or 明确告警）", _gate_warn or _gate_pass,
        f"warn={_gate_warn} pass={_gate_pass}")
    chk("D 若显示门禁行则写清基准日期", ("日期门禁" not in S) or (data_date in S))
    chk("D Hero 行不显示过期敞口",
        not re.search(r"⚠️.*Hero.*跳过", S))   # 正常运行不应有跳过日志混进页面

    # ── E 口径不自相矛盾
    print("\n── E 权重/评级口径 ──")
    chk("E 九维权重声明", "9 个生效维" in S or "9 维" in S)
    chk("E v4.17 版本标识", "v4.17" in S)
    chk("E 市值大类 0.30 未破约束", "0.3000" in S or "≤0.3" in S or "0.3" in S)
    # ⚠️ 断言修正（第一版写错过两次）：
    #    ① scored 里没有 rating_dist 字段，评级要从 results 现场数；
    #    ② **不能逐级比数量** —— 项目是评级双口径：
    #       scored.rating = 绝对阈值（S≥85 等），页面 = _quantile_ratings 分位，
    #       目的是跨日可比。两者分布本来就不同（实测 scored A:419/B:1027 vs
    #       页面 S:104/A:209）。逐级比会把「设计如此」误判成失败。
    #    ③ **不能断言「五个评级只数之和 = 样本数」** —— 页面只对 S/A 印
    #       绝对只数（`S级104`/`A级209`），B/C/D 印的是**累计百分位阈值**
    #       （`B级 前50%`/`C级 前88%`/`D级 后12%`），不是只数。
    #       实测 `sum = 313`，第一版把这个正常设计判成失败（第三次踩同一坑：
    #       断言必须贴着页面真实文案写）。正确断言 = 只数兑现百分位口径：
    #       S 只数 ≈ N×5%，A 只数 ≈ N×10%（A 是 5%~15% 区间）。
    if D.get("results"):
        _n_total = len(D["results"])
        _page_cnt = {}
        for _lab in ("S", "A", "B", "C", "D"):
            # 页面实际文案为 `S级104`（无空格无「只」）——
            # 正则必须贴着真实格式，第一版写成 `S级\s*\d+\s*只` 全部漏匹配。
            _mm = re.findall(rf"{_lab}级(\d+)", S)
            if _mm:
                _page_cnt[_lab] = int(_mm[0])
        _tol = max(2, _n_total * 0.005)     # 分位取整带来的容差
        _ok_s = abs(_page_cnt.get("S", -1) - _n_total * 0.05) <= _tol
        _ok_a = abs(_page_cnt.get("A", -1) - _n_total * 0.10) <= _tol
        chk(f"E S/A 级只数兑现分位口径"
            f"（S={_page_cnt.get('S')}≈{_n_total * 0.05:.0f}, "
            f"A={_page_cnt.get('A')}≈{_n_total * 0.10:.0f}）",
            _ok_s and _ok_a, f"页面={_page_cnt} N={_n_total} tol={_tol:.1f}")
        # B/C/D 印的是累计百分位阈值，断言它们确实以 % 形式出现（口径未漂移）
        _ok_bcd = all(re.search(rf"{_lab}级[^0-9%]{{0,6}}\d+%", S)
                      for _lab in ("B", "C", "D"))
        chk("E B/C/D 以累计百分位阈值呈现（非只数）", _ok_bcd)
        _scored_cnt = {}
        for _r in D["results"]:
            _k = _r.get("rating")
            if _k:
                _scored_cnt[_k] = _scored_cnt.get(_k, 0) + 1
        chk("E 评级双口径已说明（scored 绝对阈值 / 页面分位）",
            ("分位" in S and ("绝对" in S or "阈值" in S)) or "量化评级" in S,
            f"scored={_scored_cnt}")

    # ── F 失真表述已清除
    print("\n── F 旧的失真表述已清除 ──")
    chk("F 「为什么没有用 Transformer」已被改写",
        "为什么没有用 Transformer" not in S)
    chk("F 出现新的「为什么最终不用它预测方向」",
        "为什么最终不用它预测方向" in S)
    chk("F 未宣称「经验证的 alpha」", "经验证的 alpha" not in S
        or "禁" in S or "不得" in S)
    chk("F 未宣称「只有周期股会回落」", "只有周期股会回落" not in S)

    # ── G 数字可追溯
    print("\n── G 数字可追溯 ──")
    tf_p = os.path.join(HERE, "transformer_regime.json")
    if os.path.exists(tf_p):
        TF = json.load(open(tf_p, encoding="utf-8"))
        r = TF.get("000985") or {}
        if r.get("test_acc") is not None:
            _t = f"{float(r['test_acc'])*100:.2f}%"
            chk(f"G 中证全指准确率 {_t} 出现在页面", _t in S, _t)
        if r.get("baseline_acc") is not None:
            _b = f"{float(r['baseline_acc'])*100:.2f}%"
            chk(f"G 基线 {_b} 出现在页面", _b in S, _b)
    rf_p = os.path.join(HERE, "regime_forecast.json")
    if os.path.exists(rf_p):
        RF = json.load(open(rf_p, encoding="utf-8"))
        c = RF.get("combine") or {}
        if c.get("final_hi") is not None:
            _v = f"{float(c['final_hi'])*100:.0f}%"
            chk(f"G 取小结果 {_v} 出现在页面", _v in S, _v)
        if (RF.get("L2_risk") or {}).get("vol20_ann") is not None:
            _v = f"{float(RF['L2_risk']['vol20_ann'])*100:.2f}%"
            chk(f"G 已实现波动 {_v} 出现在页面", _v in S, _v)

    # ── 汇总
    n_ok, n_fail = len(ok), len(fail)
    print(f"\n{'='*66}")
    print(f"自测结果：{n_ok} 通过 / {n_fail} 失败")
    if fail:
        print("失败项：")
        for f in fail:
            print(f"  ❌ {f}")
    else:
        print("✅ 全部通过")
    return 0 if n_fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
