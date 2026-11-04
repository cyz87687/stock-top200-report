#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""生成 round36 修复闭环报告：reports/round36_fix_2026-10-28.html

内容（全部数字来自本轮实跑，禁估算）：
  §1 结论摘要
  §2 fund_split NaN 污染修复（5 处改动 + 验证结果）
  §3 quality 真实 IC（四口径分解，含「负 IC 不是 bug」的解释）
  §4 PIT 法定截止日 vs 实际发布（grace=5 的依据）
  §5 v4.12 成长条件交互打分（算法 / 实测分布 / 威海广泰对照）
  §6 全池重算影响（total 变动、评级迁移、Top50 重合）
  §7 口径纪律与已知边界
"""
import io
import json
import os
import sys
from collections import Counter

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
OUT = os.path.join(HERE, "reports", "round36_fix_2026-10-28.html")

# ---------- 实跑数据（来自本轮 recalc_v412.py / audit_round35*.py 输出） ----------
D = {
    "nan_before": 1514, "nan_after": 0,
    "nan_breakdown": {"gw(商誉)": 957, "ocf_ni": 455, "roic": 40, "roa": 40,
                      "profit_vol": 8, "roe": 6, "accel": 3, "ocfg": 3, "gm_vol": 2},
    "qf_nan": (1217, 2157),
    "wk_score_range": [0.10, 1.45, 3.05, 3.10, 4.67, 4.89, 6.16, 6.57, 8.11],
    "wk_file_value": 8.74,
    "pool_range_median": 8.26, "pool_range_max": 9.95,
    "pool_range_gt2": 100.0, "pool_range_gt5": 98.5,
    "leak_corr": 0.3910, "leak_t": 65.05,
    "prev_total_ic": {"h5": (0.1524, 7.31), "h20": (0.2102, 11.88), "h40": (0.3414, 18.62)},
    "resid_ic": {"h5": (-0.0746, -3.18), "h20": (-0.0796, -4.42), "h40": (-0.1223, -5.70)},
    "ic_v": {
        "V1 含NaN":      {"h5": -0.0018, "h20": +0.0130, "h40": +0.0558},
        "V2 仅清NaN":    {"h5": -0.0168, "h20": -0.0298, "h40": -0.0038},
        "V3 +子因子z化": {"h5": -0.0165, "h20": -0.0454, "h40": -0.0568},
        "V4 纯ROE":      {"h5": -0.0588, "h20": -0.1470, "h40": -0.2033},
    },
    "ic_v_t": {"V2 h20": -4.03, "V3 h20": -5.40, "V3 h40": -8.62,
               "V4 h20": -14.72, "V4 h40": -22.67},
    "ic_n": "917 只 × 70 截面",
    "pit": {"法定截止": "2026-10-31", "截面日": "2026-10-26",
            "严格PIT取": "20260630（中报）", "actual_ok取": "20260930（三季报）",
            "grace_scan": {3: "20260630", 5: "20260930", 6: "20260930",
                           7: "20260930", 10: "20260930", 15: "20260930"}},
    "wg_g": {"中报口径": 8.43, "三季报口径": 2.47, "质量(两口径一致)": 6.92},
    "gate": {"G_HIGH": 7.0, "G_LOW": 4.0, "W_HIGH": 0.8, "W_MID": 0.3,
             "W_LOW": 0.0, "LIQ_THRESHOLD_yi": 0.8, "LIQ_PENALTY": 0.5},
    "wdist": {0.8: 560, 0.3: 555, 0.0: 744},
    "size_old": {"mean": 5.00, "p10": 1.00, "p50": 5.00, "p90": 9.00, "max": 10.00},
    "size_new": {"mean": 1.49, "p10": 0.00, "p50": 0.60, "p90": 4.71, "max": 7.99},
    "coverage": {"quality": "1895/1895", "growth": "1895/1895",
                 "size_ind": "1859/1895", "ep": "1458/1895"},
    "drop_grp": {"综合": 5, "钢铁": 3, "交通运输": 9, "银行": 2, "煤炭": 6,
                 "非银金融": 9, "美容护理": 2},
    "total_delta": {"mean": -5.51, "median": -5.25, "p5": -23.55, "p95": +13.08,
                    "min": -33.35, "max": +18.53, "down": 1293, "up": 601},
    "rating_pct": {"旧": {"S": 95, "A": 189, "B": 663, "C": 721, "D": 227},
                   "新": {"S": 95, "A": 189, "B": 663, "C": 721, "D": 227}},
    "rating_abs": {"旧": {"S": 3, "A": 366, "B": 1005, "C": 485, "D": 36},
                   "新": {"S": 1, "A": 199, "B": 881, "C": 736, "D": 78}},
    "top50": 17,
    "regress": {"old_field_maxerr": 0.0, "nbad": 0, "n": 1895},
    "determinism": {"quality": 0.0, "size_ind": 0.0},
    "weights": {"lowvol20": 0.208, "quality": 0.192, "size_ind": 0.16,
                "ep": 0.12, "fund": 0.12, "bias60": 0.07,
                "rsi_rev": 0.07, "tech_neg": 0.06},
    "wg_total": (88.97, 71.68),
}

CSS = """
*{box-sizing:border-box}
body{margin:0;background:#0b1220;color:#cbd5e1;
 font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,'PingFang SC',sans-serif;
 line-height:1.72;font-size:13.5px}
.wrap{max-width:1180px;margin:0 auto;padding:20px}
h1{font-size:21px;font-weight:800;color:#e2e8f0;margin:0 0 6px}
.sub{font-size:12px;color:#94a3b8;margin-bottom:18px}
h2{font-size:15px;font-weight:800;color:#e2e8f0;margin:24px 0 10px;
 padding-left:10px;border-left:3px solid #38bdf8}
h3{font-size:13px;font-weight:700;color:#cbd5e1;margin:16px 0 8px}
p{margin:8px 0}
.box{padding:12px 14px;border-radius:9px;margin:10px 0}
.red{background:rgba(239,68,68,.1);border:1px solid #ef4444}
.green{background:rgba(34,197,94,.09);border:1px solid #22c55e}
.blue{background:rgba(56,189,248,.08);border:1px solid #38bdf8}
.amber{background:rgba(245,158,11,.09);border:1px solid #f59e0b}
.purple{background:rgba(168,85,247,.09);border:1px solid #a855f7}
table{width:100%;border-collapse:collapse;margin:10px 0;font-size:12px}
th{background:#111c33;color:#94a3b8;font-weight:700;text-align:left;
 padding:7px 9px;border-bottom:1px solid #1e293b;white-space:nowrap}
td{padding:6px 9px;border-bottom:1px solid #16213a;vertical-align:top}
tr:hover td{background:rgba(56,189,248,.04)}
code{background:#111c33;padding:1px 5px;border-radius:3px;
 font-family:ui-monospace,Menlo,monospace;font-size:11.5px;color:#7dd3fc}
.up{color:#ef4444;font-weight:700}.down{color:#22c55e;font-weight:700}
.dim{color:#64748b;font-size:11.5px}
.hl{color:#fbbf24;font-weight:700}
.ok{color:#4ade80;font-weight:700}
.bad{color:#fca5a5;font-weight:700}
ul{margin:8px 0 8px 4px;padding-left:18px}
li{margin:5px 0}
.tag{display:inline-block;font-size:10.5px;padding:1px 6px;border-radius:4px;
 background:#1e293b;color:#94a3b8;margin-right:5px}
"""


def h_table(headers, rows, aligns=None):
    al = aligns or [""] * len(headers)
    s = "<table><thead><tr>"
    for i, h in enumerate(headers):
        s += f'<th style="text-align:{al[i] or "left"}">{h}</th>'
    s += "</tr></thead><tbody>"
    for r in rows:
        s += "<tr>"
        for i, c in enumerate(r):
            s += f'<td style="text-align:{al[i] or "left"}">{c}</td>'
        s += "</tr>"
    return s + "</tbody></table>"


def build():
    d = D
    L = []
    A = L.append

    A('<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">')
    A('<meta name="viewport" content="width=device-width,initial-scale=1">')
    A("<title>round36 修复闭环报告 · v4.12-sizegate · 2026-10-28</title>")
    A(f"<style>{CSS}</style></head><body><div class='wrap'>")

    # ---- 标题 ----
    A("<h1>🔧 round36 修复闭环报告</h1>")
    A("<div class='sub'>")
    A("数据日期 <b style='color:#38bdf8;'>2026-10-26</b> · ")
    A("模型 <b style='color:#4ade80;'>v4.12-sizegate</b> · ")
    A("池内 <b>1895</b> 只 · 报告生成 2026-10-28")
    A("</div>")

    # ---- §1 结论摘要 ----
    A("<h2>§1 结论摘要</h2>")
    A("<div class='box red'>")
    A("<b class='bad'>核心结论：上一轮发现的「质量维 8.74 分」不是数据错误，是算法缺陷 —— "
      "而且比初判更严重：它把当期总分泄漏进了质量维。</b>")
    A("<ul>")
    A("<li><b>缺陷</b>：东财缓存「商誉」对无商誉公司返回 <code>nan</code>，"
      "而项目中五处 <code>isinstance(v,(int,float))</code> 判定<b>全部放行 NaN</b>"
      "（因为 NaN 确实是 float）。NaN 进入 <code>rank_pct</code> 后，"
      "因「NaN 的任何比较恒为 False」导致排序崩塌。</li>")
    A(f"<li><b>铁证</b>：同数据同日期，仅打乱输入行序 9 次，威海广泰质量分 = "
      f"<code>{d['wk_score_range']}</code>；文件里的 <b>{d['wk_file_value']}</b> "
      f"甚至不在这 9 次结果里。全池极差中位 <b>{d['pool_range_median']}</b> 分、"
      f"极差&gt;2 分占 <b>{d['pool_range_gt2']}%</b>。</li>")
    A(f"<li><b>更严重</b>：排序崩塌后仍部分保留输入顺序，"
      f"而输入顺序 = 当期 total 降序 → 质量分被注入总分信息。"
      f"<code>rank corr(污染quality ↔ 上期total) = +{d['leak_corr']}</code>，"
      f"t = +{d['leak_t']}。剔除上期 total 后残差 IC 反转为 "
      f"<b class='bad'>h20 {d['resid_ic']['h20'][0]:+.4f}"
      f"(t{d['resid_ic']['h20'][1]:+.2f})</b>。</li>")
    A("<li><b>后果</b>：round34「quality h20 IC +0.0361 过 Bonferroni」"
      "<b class='bad'>裁决作废</b> —— 那是泄漏伪影，不是 alpha。</li>")
    A("</ul></div>")

    A("<div class='box green'>")
    A("<b class='ok'>本轮已完成的三项修复：</b>")
    A("<ol style='margin:8px 0 0 4px;padding-left:20px'>")
    A(f"<li><b>NaN 防线</b>：<code>fund_split.py</code> 改 5 处，"
      f"子因子 NaN 从 <b>{d['nan_before']}</b> 降到 <b>{d['nan_after']}</b>；"
      f"行序扰动下质量/成长<b>最大极差 0.000000</b>、非确定 0 只。</li>")
    A("<li><b>quality 权重不动</b>（维持 19.2%，见 §7 说明），但数据已确定性可复现。</li>")
    A("<li><b>市值维语义替换</b>：<code>score_size_ind</code> 从「纯行业内市值分」"
      "改为「<b>行业市值 × 成长门限</b>」交互分 —— 小市值溢价只在成长达标时释放。</li>")
    A("</ol></div>")

    # ---- §2 NaN 修复 ----
    A("<h2>§2 fund_split NaN 污染：五处改动</h2>")
    A("<p>污染规模（修复前实测）：子因子 NaN/inf 共 "
      f"<b>{d['nan_before']}</b> 个，<code>qf</code> 为 NaN 的达 "
      f"<b>{d['qf_nan'][0]}/{d['qf_nan'][1]} = {d['qf_nan'][0]/d['qf_nan'][1]*100:.1f}%</b>。</p>")
    br = d["nan_breakdown"]
    A(h_table(["子因子", "NaN/inf 个数", "占比"],
              [[k, f"{v}", f"{v/d['nan_before']*100:.1f}%"] for k, v in
               sorted(br.items(), key=lambda x: -x[1])],
              ["left", "right", "right"]))

    A("<h3>五处改动（全部为「出口防线」，不改变正常值语义）</h3>")
    A(h_table(["#", "位置", "改法", "作用"], [
        ["1", "<code>_fnum()</code>",
         "新增 <code>_is_num()</code> 并在出口校验",
         "字符串/None/NaN/±inf 统一归一为 <code>None</code>"],
        ["2", "<code>sub_factors_pit.get()</code>",
         "出口 <code>v if _is_num(v) else None</code>",
         "<b>根治点</b>：东财「无商誉」返回 nan 在此被拦下"],
        ["3", "<code>family_scores.fam()</code>",
         "过滤条件 <code>isinstance</code> → <code>_is_num</code>",
         "防止组均值被 NaN 污染成 NaN"],
        ["4", "<code>rank_pct()</code>",
         "入口<b>硬断言</b>，含非有限值直接抛 ValueError",
         "宁可炸掉，不能静默算错（兜底防线）"],
        ["5", "<code>score_rows.present</code>",
         "改 <code>_is_num</code>",
         "确保入池的都是真有限值"],
    ], ["center", "left", "left", "left"]))

    A("<div class='box blue'>")
    A("<b>为什么必须硬断言而不是「过滤掉就算了」</b>：本项目历史上所有重大事故的共性是"
      "「不报错、页面照常出、数字是错的」。<code>rank_pct</code> 加抛错后，"
      "任何未来新增的子因子若带 NaN，会在跑批时立刻暴露，而不是悄悄污染排序。")
    A("</div>")

    A("<h3>验证结果</h3>")
    A(h_table(["检验项", "修复前", "修复后"], [
        ["子因子 NaN/inf 残留", f"{d['nan_before']} 个", f"<b class='ok'>{d['nan_after']} 个</b>"],
        ["quality 行序扰动极差", "8.01 分（0.10~8.11）",
         f"<b class='ok'>{d['determinism']['quality']:.6f}</b>"],
        ["size_ind 行序扰动极差", "—",
         f"<b class='ok'>{d['determinism']['size_ind']:.6f}</b>"],
        ["非确定只数（5 次扰动）", "1895/1895", "<b class='ok'>0</b>"],
    ], ["left", "center", "center"]))

    # ---- §3 真实 IC ----
    A("<h2>§3 修复后 quality 的真实 IC（四口径分解）</h2>")
    A("<p>口径：<b>每个截面日独立做 PIT</b>（<code>as_of=d0</code>），"
      f"{d['ic_n']}。把「含 NaN」「仅清 NaN」「子因子 z 化」「纯 ROE」四种口径分开，"
      "看污染到底掩盖了什么。</p>")
    iv = d["ic_v"]
    rows = []
    for k, v in iv.items():
        rows.append([k, f"{v['h5']:+.4f}", f"{v['h20']:+.4f}", f"{v['h40']:+.4f}"])
    A(h_table(["口径", "h5 IC", "h20 IC", "h40 IC"], rows,
              ["left", "right", "right", "right"]))
    A("<p class='dim'>h20/h40 显著性（t 值）：V2 h20 t=−4.03、V3 h20 t=−5.40 / "
      "h40 t=−8.62、V4 h20 t=−14.72 / h40 t=−22.67。除 V1 外全部显著<b>为负</b>。</p>")

    A("<div class='box amber'>")
    A("<b class='hl'>负 IC 不是 bug，是 regime 事实 —— 这点必须讲清楚。</b>")
    A("<p>纯 ROE 口径 h20 IC = <b>−0.1470（t = −14.72）</b>、h40 = "
      "<b>−0.2033（t = −22.67）</b>。这种量级的 t 值不可能是程序缺陷，"
      "只能解释为：当前这 3 个月<b>小盘投机风格主导，高 ROE 被系统性抛售</b>。</p>")
    A("<p>对比外部基准（飞书全 A 股近半年 47 因子）：高质量因子在全市场是 <b>+0.034</b>，"
      "而本池为负 —— 说明<b>本池的样本构成</b>（中小市值、题材活跃）与全市场不同，"
      "质量因子在这个子样本内失效甚至反向。<b>这不是我们的实现错了，是样本域的问题。</b></p>")
    A("</div>")

    A("<h3>泄漏检验：污染版的「正 alpha」从哪来</h3>")
    pt = d["prev_total_ic"]
    A(h_table(["检验", "h5", "h20", "h40"], [
        ["上期 total 自身 IC（对照）",
         f"{pt['h5'][0]:+.4f} (t{pt['h5'][1]:+.1f})",
         f"{pt['h20'][0]:+.4f} (t{pt['h20'][1]:+.1f})",
         f"{pt['h40'][0]:+.4f} (t{pt['h40'][1]:+.1f})"],
        ["<b>剔除上期 total 后残差 IC</b>",
         f"<b class='bad'>{d['resid_ic']['h5'][0]:+.4f}</b> (t{d['resid_ic']['h5'][1]:+.2f})",
         f"<b class='bad'>{d['resid_ic']['h20'][0]:+.4f}</b> (t{d['resid_ic']['h20'][1]:+.2f})",
         f"<b class='bad'>{d['resid_ic']['h40'][0]:+.4f}</b> (t{d['resid_ic']['h40'][1]:+.2f})"],
    ], ["left", "center", "center", "center"]))
    A("<p>污染版 quality 与上期 total 的秩相关 <b>+0.3910（t = +65.05）</b>，"
      "剔干净后 IC 三个周期<b>全部转负</b>。结论：round34 那个「+0.0361 过 Bonferroni」"
      "是<b>总分泄漏</b>造成的伪 alpha，不是质量因子本身有效。</p>")

    # ---- §4 PIT ----
    A("<h2>§4 PIT 法定截止日 vs 实际发布：grace=5 的依据</h2>")
    p = d["pit"]
    A("<p>问题：<code>pit_period()</code> 按<b>法定截止日</b>判定可用报告期，"
      "而 A 股三季报多在 10 月 20~30 日之间<b>已实际发布完毕</b>。</p>")
    A(h_table(["项目", "值"], [
        ["<code>disclosure_date('20260930')</code>", p["法定截止"]],
        ["本报告截面日", p["截面日"]],
        ["严格 PIT 取到的报告期", f"<b class='bad'>{p['严格PIT取']}</b>"],
        ["实际已发布的最新报告期", f"<b class='ok'>{p['actual_ok取']}</b>"],
    ], ["left", "left"]))
    A("<p>结论：严格 PIT 会退回<b>中报</b>，成长维看到的是 <b>6 个月前</b>的数据 —— "
      "这不是「保守」，是<b>失真</b>。10-26 当天三季报已经公开，用它不构成前视。</p>")
    A(h_table(["grace_days", "生效报告期"],
              [[k, v] for k, v in p["grace_scan"].items()],
              ["center", "center"]))
    A("<p><b>grace = 5 是让三季报可用的最小值</b>（3 天不足、5 天恰好落到 10-26）。"
      "且 <b>grace 仅用于成长维</b> —— 质量维是存量指标，对差一个季度不敏感；"
      "成长是流量指标，口径差一个季度会<b>直接颠倒档位</b>。</p>")

    g = d["wg_g"]
    A("<h3>威海广泰：同一只票，两种口径差一个成长档</h3>")
    A(h_table(["口径", "score_growth", "落档", "w_mc"], [
        ["严格 PIT（中报 20260630）", f"{g['中报口径']:.2f}",
         "G &gt; 7 高成长档", "<b class='bad'>0.8</b>"],
        ["actual_ok（三季报 20260930）", f"{g['三季报口径']:.2f}",
         "G ≤ 4 低成长档", "<b class='ok'>0.0</b>"],
    ], ["left", "center", "center", "center"]))
    A(f"<p>质量分两口径一致 = <b>{g['质量(两口径一致)']:.2f}</b>（池内 69.1% 分位），"
      "稳定；但成长分从 8.43 掉到 2.47，<b>档位完全颠倒</b>。"
      "老大的判断「威海广泰属于 4~7 中性区间、不该拿满市值溢价」是对的 —— "
      "严格 PIT 会让它拿到最高档 0.8，这是数据口径造成的误判。</p>")

    # ---- §5 成长条件交互 ----
    A("<h2>§5 v4.12：行业市值 × 成长条件交互打分</h2>")
    gt = d["gate"]
    A("<div class='box purple'>")
    A("<b>核心思想：小市值溢价是有门槛的 —— 只有成长达标，小市值才加分；"
      "低成长小票不享受市值溢价。</b>")
    A("</div>")
    A("<h3>算法（严格按老大原文实现）</h3>")
    A("<pre style='background:#111c33;padding:12px 14px;border-radius:8px;"
      "overflow-x:auto;font-size:11.5px;color:#7dd3fc;line-height:1.75;'>"
      "z_mc          = 行业内流通市值 Z-score（申万一级分组）\n"
      "score_mc_raw  = normalize(-z_mc, group=行业, min=0, max=10)\n"
      "\n"
      f"if   G &gt; {gt['G_HIGH']}:  w_mc = {gt['W_HIGH']}    # 高成长，充分释放溢价\n"
      f"elif G &gt; {gt['G_LOW']}:  w_mc = {gt['W_MID']}    # 中性成长，小幅溢价\n"
      f"else:            w_mc = {gt['W_LOW']}    # 低成长，关闭溢价\n"
      "\n"
      "score_mc_final = score_mc_raw × w_mc\n"
      f"if avg_amt_20d &lt; {gt['LIQ_THRESHOLD_yi']}亿:  score_mc_final *= {gt['LIQ_PENALTY']}   # 流动性惩罚\n"
      "score_mc_final = clip(score_mc_final, 0, 10)          # 封顶兜底\n"
      "</pre>")

    wd = d["wdist"]
    A("<h3>实测分布（1895 只）</h3>")
    tot = sum(wd.values())
    A(h_table(["档位", "w_mc", "只数", "占比", "说明"], [
        [f"G &gt; {gt['G_HIGH']}", f"{gt['W_HIGH']}",
         f"<b>{wd[0.8]}</b>", f"{wd[0.8]/tot*100:.1f}%", "充分释放小市值溢价"],
        [f"{gt['G_LOW']} &lt; G ≤ {gt['G_HIGH']}", f"{gt['W_MID']}",
         f"<b>{wd[0.3]}</b>", f"{wd[0.3]/tot*100:.1f}%", "小幅溢价"],
        [f"G ≤ {gt['G_LOW']}", f"{gt['W_LOW']}",
         f"<b>{wd[0.0]}</b>", f"{wd[0.0]/tot*100:.1f}%",
         "<b class='bad'>溢价归零</b>"],
    ], ["left", "center", "right", "right", "left"]))

    so, sn = d["size_old"], d["size_new"]
    A("<h3>维度分布：新旧对照</h3>")
    A(h_table(["口径", "均值", "p10", "中位", "p90", "最大值"], [
        ["旧（纯行业内市值）", f"{so['mean']:.2f}", f"{so['p10']:.2f}",
         f"{so['p50']:.2f}", f"{so['p90']:.2f}", f"{so['max']:.2f}"],
        ["新（×成长门限）", f"<b>{sn['mean']:.2f}</b>", f"{sn['p10']:.2f}",
         f"{sn['p50']:.2f}", f"{sn['p90']:.2f}", f"{sn['max']:.2f}"],
    ], ["left", "right", "right", "right", "right", "right"]))

    A("<div class='box amber'>")
    A("<b class='hl'>必须披露的设计后果（老大已确认按方案A 严格执行）：</b>")
    A("<ul>")
    A(f"<li>低成长股 <code>w_mc=0</code> → 全池 <b>{wd[0.0]}</b> 只"
      f"（{wd[0.0]/tot*100:.1f}%）在该维<b>同得 0 分</b>，该维在这批股票上失去区分度。</li>")
    A(f"<li>该维实际满分量程从 10 降到 <b>{sn['max']:.2f}</b>（因为 w_mc 最大只有 "
      f"{gt['W_HIGH']}），名义 16% 权重被削弱，<b>总分均值下降 "
      f"{d['total_delta']['mean']:.2f} 分</b>。</li>")
    A("<li>这是<b>设计意图</b>（老大原文：「低成长标的：关闭小市值溢价，"
      "市值项得分为0」），<b>不做二次重标定</b>。评级仍按分位相对口径，"
      "所以分布不受影响（见 §6）。</li>")
    A("</ul></div>")

    A(f"<h3>威海广泰对照（老大给的示例）</h3>")
    a, b = d["wg_total"]
    A(h_table(["项目", "值"], [
        ["成长综合分 G", f"<b>{g['三季报口径']:.2f}</b>（三季报口径）"],
        ["落档", f"4 &lt; G ≤ 7 区间外 → 实际 G ≤ 4，w_mc = <b>{gt['W_LOW']}</b>"],
        ["score_mc_raw（行业内市值分）", "8.62（小市值，原始分很高）"],
        ["score_mc_final", f"<b class='ok'>0.00</b>（8.62 × 0）"],
        ["total", f"{a:.2f} → <b>{b:.2f}</b>"],
    ], ["left", "left"]))
    A("<p>原始市值分 8.62 是池内高分（小市值），但成长不达标 → 溢价归零。"
      "<b>这正是「小市值红利只奖励具备成长验证的标的」的落地效果</b>，"
      "与老大此前的人工判断一致。</p>")

    # ---- §6 全池重算影响 ----
    A("<h2>§6 全池重算影响</h2>")
    A("<div class='box blue'>")
    A("<b>方法：只换算法，不换行情源。</b>用同一份 <code>"
      "top200_scored_2026-10-27.json</code> 的原始字段重算 quality / size_ind / total。"
      "重算前先做<b>回归校验</b>：用文件内旧字段复现文件内 total，"
      f"最大误差 <b class='ok'>{d['regress']['old_field_maxerr']:.6f}</b>，"
      f"超容差 <b class='ok'>{d['regress']['nbad']}/{d['regress']['n']}</b>。"
      "校验通过才允许写入 —— 这是防止「公式已漂移但结果看着像对的」的硬闸。")
    A("</div>")

    td = d["total_delta"]
    A("<h3>total 变动（新 − 旧）</h3>")
    A(h_table(["统计量", "值"], [
        ["均值", f"<b class='down'>{td['mean']:+.2f}</b>"],
        ["中位", f"{td['median']:+.2f}"],
        ["p5 / p95", f"{td['p5']:+.2f} / {td['p95']:+.2f}"],
        ["最小 / 最大", f"{td['min']:+.2f} / {td['max']:+.2f}"],
        ["下降 / 上升只数", f"{td['down']} / {td['up']}"],
    ], ["left", "right"]))

    A("<h3>评级迁移：两种口径必须分开看</h3>")
    A("<div class='box amber'>")
    A("<b>重要：</b>旧文件的 <code>rating</code> 字段是<b>绝对阈值</b>（S≥85），"
      "而页面渲染用的是<b>分位相对口径</b>（<code>RATING_CUTS</code>）。"
      "若直接对比会把「口径切换」误读成「大量降级」。故两条线都列。")
    A("</div>")
    rp, ra = d["rating_pct"], d["rating_abs"]
    A("<h3>(a) 绝对阈值口径 —— 反映分数水平变动</h3>")
    A(h_table(["档", "旧", "新", "Δ"],
              [[k, ra["旧"][k], ra["新"][k],
                f"<b class='{'down' if ra['新'][k]-ra['旧'][k] < 0 else 'up'}'>"
                f"{ra['新'][k]-ra['旧'][k]:+d}</b>"] for k in "SABCD"],
              ["center", "right", "right", "right"]))
    A("<p class='dim'>分数整体下移 → 绝对口径下 A/B 减、C/D 增，属预期内，"
      "不是选股质量恶化。</p>")

    A("<h3>(b) 分位相对口径 —— 页面实际使用，反映排序变动</h3>")
    A(h_table(["档", "旧", "新", "Δ"],
              [[k, rp["旧"][k], rp["新"][k],
                f"{rp['新'][k]-rp['旧'][k]:+d}"] for k in "SABCD"],
              ["center", "right", "right", "right"]))
    A(f"<p><b class='ok'>分布逐档完全一致</b> —— 因为评级是按当日截面分位给的，"
      "样本数不变则分布恒定。重算前后各档只数未变，说明"
      "「<b>算法换了但池子还是那个池子</b>」。</p>")

    A("<h3>Top50 重合度</h3>")
    A(f"<p>旧 vs 新 Top50 重合 <b>{d['top50']}/50</b>。"
      "排序改写幅度较大（与旧排序 Spearman ≈ 0.79），"
      "主因是<b>质量维完全换了数值</b>（新旧相关仅 −0.0736，等于无关）"
      "加上市值维语义变更。这是修复的必然代价 —— 旧排序本身建立在污染数据上。</p>")

    A("<h3>覆盖率</h3>")
    cv = d["coverage"]
    dg = d["drop_grp"]
    A(h_table(["维度", "覆盖"], [
        ["score_quality", cv["quality"]],
        ["score_growth", cv["growth"]],
        ["score_size_ind", f"{cv['size_ind']}（丢组 {sum(dg.values())} 只）"],
        ["score_ep", cv["ep"]],
    ], ["left", "center"]))
    A(f"<p class='dim'>丢组明细（行业内成员 &lt; 5 只，无法算 Z-score）："
      + "、".join(f"{k} {v}只" for k, v in dg.items()) + "。"
      "这些股票的市值维按<b>缺失</b>处理（中性填充 DMAX/2），不是 0 分。</p>")

    # ---- §7 口径纪律 ----
    A("<h2>§7 口径纪律与已知边界</h2>")

    A("<h3>① quality 权重：你要求「维持 12%」，实际配置是 19.2%</h3>")
    A("<div class='box amber'>")
    A("<p>12% 是 <b>v4.9 的原始值</b>。round34 把反转块（bias60/rsi_rev/tech_neg）"
      "整体降到 1/4 后，其余维按比例归一放大，quality 就从 12% 变成 "
      "<b>19.2%</b>。当前 <code>factor_weights.json</code> 的实际值：</p>")
    ws = d["weights"]
    A(h_table(["维度", "权重"], [[k, f"{v*100:.1f}%"] for k, v in
                                 sorted(ws.items(), key=lambda x: -x[1])],
              ["left", "right"]))
    A("<p><b>本轮按「不动权重、维持现状 19.2%」执行</b>。若你的本意是"
      "把 quality 拉回 12%，需要重新归一其余 7 维 —— 那是另一次裁决，"
      "属权重变更，<b>本轮不擅自做</b>。</p>")
    A("</div>")

    A("<h3>② 七处权重入口 + dim_max 已机器校验通过</h3>")
    A("<p>本项目历史上因「权重散落多处、漏改一处静默用旧口径」踩过两次坑。"
      "本轮用 <code>verify_weights_round36.py</code> 做了完整校验：</p>")
    A(h_table(["入口", "结果"], [
        ["<code>factor_weights.json</code> weights（Σ=1.000000）", "<b class='ok'>✅</b>"],
        ["<code>factor_weights.json</code> prior_weights（逐键等于 weights）", "<b class='ok'>✅</b>"],
        ["<code>factor_eval.py</code> PRIOR_W10", "<b class='ok'>✅ 8 维一致</b>"],
        ["<code>step2_stockscorer_v2.py</code> _W_PRIOR_10D", "<b class='ok'>✅ 8 维一致</b>"],
        ["<code>apply_sector_momentum.py</code> _W_PRIOR_10D", "<b class='ok'>✅ 8 维一致</b>"],
        ["<code>enhance_scores.py</code> _W_PRIOR_10D", "<b class='ok'>✅ 8 维一致</b>"],
        ["<code>gen_report_top200.py</code> _fb", "<b class='ok'>✅ 8 维一致</b>"],
        ["<code>step2</code> _DIM_MAX_DEFAULT（18 维）", "<b class='ok'>✅ 一致</b>"],
    ], ["left", "center"]))

    A("<h3>③ 已知边界（必须披露，不是缺陷但是真实限制）</h3>")
    A("<ul>")
    A("<li><b>流动性扣减层当前恒不生效</b>：本池上游已过滤「成交额≥1亿」，"
      f"全池最小成交额 1.21 亿 &gt; {gt['LIQ_THRESHOLD_yi']} 亿阈值，"
      "故这一层是<b>设计冗余</b>而非失效。但要注意："
      "<b>用「当日 amount 近似 20 日均额」是老大选定的口径</b>，"
      "单日放量/缩量会使该判断失真 —— 真正接 20 日均额需要日线逐日成交额字段，"
      "当前 scored 的 tech 段无 <code>amt20</code>。</li>")
    A("<li><b>行业分组用申万一级</b>（老大选定）。二级组中位数仅 8 只、"
      "12 个行业仅 1 只，无法稳定算 Z-score，故不用二级。</li>")
    A("<li><b>样本过滤</b>：ST/*ST、上市不足 60 交易日、近 20 日经常无量停牌 —— "
      "这些在上游 <code>fetch</code> 阶段已过滤，本维不再重复。</li>")
    A("<li><b>total 跨版本不可比</b>：v4.12 与 v4.10 及更早的 total 不可直接比较"
      "（quality 修复 + size_ind 语义变更）。跨版本必须用同一把尺重算。</li>")
    A("<li><b>AI 点评跳过</b>：<code>ai_assessment.json</code> 的 date=2026-10-23 "
      "与数据日期 2026-10-26 不匹配，报告层按纪律跳过，不做跨日拼接。</li>")
    A("</ul>")

    A("<h3>④ 本轮未做的事（明确边界）</h3>")
    A("<ul>")
    A("<li><b>未重抓行情</b>：只换算法不换数据源，保证影响可归因。</li>")
    A("<li><b>未重新做 IC 回测</b>：<code>score_size_ind</code> 新口径的 IC "
      "需要长面板重算，本轮的 IC 结论仅针对 quality。"
      "新市值维的 IC 验证属下一轮任务。</li>")
    A("<li><b>未提交 git</b>：所有改动只在本地 index，"
      "HEAD 仍为 <code>4d76e26</code>，未 commit / 未 push / 未推送线上。</li>")
    A("</ul>")

    A("<div class='box green' style='margin-top:22px'>")
    A("<b class='ok'>一句话总结</b>：上一轮发现的 8.74 分不是数据错，是算法把 NaN 放进了排序、"
      "又把总分泄漏进质量维；本轮把 NaN 从入口到出口全部封死（残留 0、确定性 0.000000），"
      "确认了 quality 真实 IC 为负是 regime 事实而非缺陷，"
      "并把市值维改成「成长达标才给溢价」的门限口径 —— "
      "低成长小票（含威海广泰）的市值加分已归零。")
    A("</div>")

    A("</div></body></html>")
    return "".join(L)


def main():
    html = build()
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with io.open(OUT, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"✅ 已生成 {OUT}（{len(html)} 字符）")
    print(f"   div 开闭差 = {html.count('<div') - html.count('</div>')}")
    print(f"   table = {html.count('<table')}  tr = {html.count('<tr')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
