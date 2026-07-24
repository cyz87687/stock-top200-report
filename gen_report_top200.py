#!/usr/bin/env python3
"""
生成A股成交额TOP200评分HTML报告 - 增强版
功能特性：
- 支持TOP200个股数据
- 响应式布局与移动端适配
- 实时排序筛选交互
- 关键指标可视化图表
- 优化的页面性能
"""

import json
import sys
import os
import glob
import html as html_mod
from datetime import datetime

# 输入输出配置
inp = sys.argv[1] if len(sys.argv) > 1 else f"top200_scored_{datetime.now().strftime('%Y-%m-%d')}.json"
out = sys.argv[2] if len(sys.argv) > 2 else f"top200_report_{datetime.now().strftime('%Y-%m-%d')}.html"

# 加载数据
with open(inp, "r", encoding="utf-8") as f:
    data = json.load(f)

results = data["results"]
stats = data.get("stats", {})
market_breadth = stats.get("market_breadth")  # 大盘赚钱效应 (v2.7 新增)

# 加载历史报告用于评级变化追踪
prev_ratings = {}
prev_files = sorted(glob.glob("top200_scored_*.json"), reverse=True)
for pf in prev_files:
    if os.path.basename(pf) == os.path.basename(inp):
        continue
    try:
        with open(pf, "r", encoding="utf-8") as f:
            prev_data = json.load(f)
        for r in prev_data.get("results", []):
            prev_ratings[r["code"]] = {"rating": r["rating"], "total": r["total"]}
        break  # 只取最近一次
    except:
        pass
date_str = data.get("date", datetime.now().strftime("%Y-%m-%d"))
total_stocks = len(results)

# 颜色配置
RATING_COLORS = {"S": "#22c55e", "A": "#3b82f6", "B": "#f59e0b", "C": "#f97316", "D": "#dc2626"}
RATING_BG = {"S": "#dcfce7", "A": "#dbeafe", "B": "#fef3c7", "C": "#ffedd5", "D": "#fee2e2"}
RATING_FULL = {"S": "强烈推荐 · 可重仓", "A": "重点关注 · 逢低加仓", "B": "波段操作 · 轻仓参与", "C": "观望为主 · 不新开仓", "D": "坚决回避 · 不持有"}

top20 = [r for r in results if r["rating"] in ("S", "A")]
top20.sort(key=lambda x: -x["total"])

def build_portfolio(results, n=5):
    candidates = []
    for r in results:
        if r["rating"] not in ("S", "A"):
            continue
        fwd_pe = r.get("fwd_pe")
        if not fwd_pe or fwd_pe <= 0 or fwd_pe > 60:
            continue
        growth = r.get("growth")
        if not growth or growth < 15:
            continue
        tech = r.get("tech", {})
        position = tech.get("position", 50)
        if position > 95:
            continue
        score_diversity = 0
        dims = [r["score_news"], r["score_tech"], r["score_fund"], r.get("score_theme", 0)]
        if min(dims) >= 3:
            score_diversity = 1
        if min(dims) >= 4:
            score_diversity = 2
        composite = r["total"] + score_diversity * 0.5
        if 40 <= position <= 80:
            composite += 0.5
        candidates.append((composite, r))

    candidates.sort(key=lambda x: -x[0])
    picks = []
    used_sectors = set()
    for _, r in candidates:
        sec = r.get("sector", "")
        if sec in used_sectors:
            continue
        picks.append(r)
        used_sectors.add(sec)
        if len(picks) >= n:
            break

    if len(picks) < n:
        for r in results:
            if r in picks:
                continue
            if r["rating"] not in ("S", "A"):
                continue
            fwd_pe = r.get("fwd_pe")
            if not fwd_pe or fwd_pe <= 0 or fwd_pe > 80:
                continue
            sec = r.get("sector", "")
            if sec in used_sectors:
                continue
            picks.append(r)
            used_sectors.add(sec)
            if len(picks) >= n:
                break
    return picks

portfolio_picks = build_portfolio(results)

# 板块均分TOP5
sec_avg = stats.get("sector_avg", {})
sorted_secs = sorted(sec_avg.items(), key=lambda x: -x[1].get("avg", 0))[:5]

# 开始生成HTML
html_parts = []

html_parts.append('''<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
<title>📊 A股成交额TOP''' + str(total_stocks) + ''' 极简公司评分 | ''' + date_str + '''</title>
<style>
/* 基础样式 */
* {margin:0;padding:0;box-sizing:border-box;}
body {
    font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,'Helvetica Neue',Arial,sans-serif;
    background:#0f172a;color:#e2e8f0;padding:16px;line-height:1.5;
}
.container {max-width:1400px;margin:0 auto;}

/* 头部 */
.header {text-align:center;padding:24px 0;border-bottom:1px solid #334155;margin-bottom:24px;}
.header h1 {font-size:24px;color:#f8fafc;margin-bottom:8px;}
.header .sub {color:#94a3b8;font-size:13px;margin-bottom:4px;}
.header .meta {color:#64748b;font-size:11px;}

/* 核心结论卡片 */
.core-conclusion {
    background:linear-gradient(135deg,#1e293b 0%,#0f172a 100%);
    border-radius:16px;padding:24px;margin:20px 0;
    border:1px solid #334155;border-left:4px solid #22c55e;
}
.core-conclusion h2 {font-size:18px;color:#f8fafc;margin-bottom:16px;display:flex;align-items:center;gap:8px;}
.core-conclusion .main-line {font-size:14px;color:#e2e8f0;margin-bottom:12px;line-height:1.8;}
.core-conclusion .top3 {display:grid;grid-template-columns:repeat(3,1fr);gap:12px;margin-top:12px;}
.core-conclusion .top3-item {
    background:#0f172a;border-radius:10px;padding:14px;border:1px solid #334155;
    display:flex;flex-direction:column;gap:6px;
}
.core-conclusion .top3-item .t-name {font-size:15px;font-weight:800;color:#f8fafc;}
.core-conclusion .top3-item .t-score {font-size:22px;font-weight:800;}
.core-conclusion .top3-item .t-reason {font-size:11px;color:#94a3b8;line-height:1.5;}

/* 快捷筛选按钮 */
.quick-filters {display:flex;gap:8px;flex-wrap:wrap;margin:16px 0;}
.quick-btn {
    background:#1e293b;border:1px solid #334155;color:#e2e8f0;
    padding:8px 16px;border-radius:20px;font-size:12px;cursor:pointer;
    transition:all 0.2s;font-weight:600;
}
.quick-btn:hover {border-color:#3b82f6;background:#273449;}
.quick-btn.active {background:#3b82f6;border-color:#3b82f6;color:#fff;}

/* S级金色突出 */
.stock-detail.s-level {
    background:linear-gradient(135deg,#1e293b 0%,#1a1f2e 100%);
    border-left:4px solid #fbbf24;
    box-shadow:0 0 20px rgba(251,191,36,0.15);
}
.stock-detail.s-level .name {color:#fbbf24;}
.stock-detail.s-level .total {font-size:32px;color:#fbbf24;text-shadow:0 0 10px rgba(251,191,36,0.3);}
.stock-detail.s-level .rating {background:#fef3c7 !important;color:#d97706 !important;font-weight:800;}

/* 评级变化标签 */
.rating-change {
    font-size:9px;padding:2px 6px;border-radius:4px;font-weight:700;margin-left:4px;
}
.rating-up {background:#dcfce7;color:#16a34a;}
.rating-down {background:#fee2e2;color:#dc2626;}
.rating-new {background:#fef3c7;color:#d97706;}

/* 分页 */
.pagination {display:flex;gap:6px;justify-content:center;align-items:center;margin:16px 0;flex-wrap:wrap;}
.pagination button {
    background:#1e293b;border:1px solid #334155;color:#e2e8f0;
    padding:6px 12px;border-radius:6px;font-size:12px;cursor:pointer;transition:all 0.2s;
}
.pagination button:hover {border-color:#3b82f6;}
.pagination button.active {background:#3b82f6;border-color:#3b82f6;color:#fff;}
.pagination button:disabled {opacity:0.4;cursor:not-allowed;}
.pagination .page-info {color:#94a3b8;font-size:11px;margin:0 8px;}

/* 列显示控制 */
.col-toggle {display:flex;gap:6px;flex-wrap:wrap;margin:8px 0;padding:10px;background:#1e293b;border-radius:8px;}
.col-toggle label {font-size:11px;color:#94a3b8;display:flex;align-items:center;gap:4px;cursor:pointer;}
.col-toggle input[type="checkbox"] {accent-color:#3b82f6;}

/* 可展开行 */
.stock-table tr.expandable {cursor:pointer;}
.stock-table tr.expandable:hover td {background:#374151;}
.stock-table tr.detail-row td {padding:0;border:none;background:#0f172a;}
.stock-table tr.detail-row .detail-content {padding:16px;display:none;}
.stock-table tr.detail-row.show .detail-content {display:block;}
.stock-table tr.detail-row.show td {padding:16px;}

/* 排序箭头 */
.sort-arrow {font-size:10px;color:#64748b;margin-left:2px;}
th.sorted-asc .sort-arrow {color:#22c55e;}
th.sorted-desc .sort-arrow {color:#ef4444;}

/* 筛选控制 */
.controls {
    display:flex;gap:12px;margin:16px 0;flex-wrap:wrap;
    background:#1e293b;padding:16px;border-radius:12px;border:1px solid #334155;
}
.controls label {font-size:12px;color:#94a3b8;display:block;margin-bottom:4px;}
.controls select, .controls input {
    background:#0f172a;border:1px solid #334155;color:#e2e8f0;
    padding:8px 12px;border-radius:6px;font-size:13px;min-width:140px;
}
.controls select:focus, .controls input:focus {outline:none;border-color:#3b82f6;}
.controls .control-group {flex:1;min-width:160px;}

/* 统计卡片 */
.summary {display:grid;grid-template-columns:repeat(5,1fr);gap:12px;margin:20px 0;}
.card {
    background:#1e293b;border-radius:12px;padding:16px;text-align:center;
    border:1px solid #334155;transition:transform 0.2s;
}
.card:hover {transform:translateY(-2px);border-color:#475569;}
.card .count {font-size:28px;font-weight:800;margin:4px 0;}
.card .label {font-size:11px;color:#94a3b8;}

/* 图表区域 */
.charts {display:grid;grid-template-columns:1fr 1fr;gap:16px;margin:20px 0;}
.chart-card {
    background:#1e293b;border-radius:12px;padding:20px;border:1px solid #334155;
}
.chart-card h3 {font-size:14px;color:#f8fafc;margin-bottom:16px;display:flex;align-items:center;gap:8px;}
.chart-container {height:200px;position:relative;}
.bar-chart {display:flex;align-items:flex-end;gap:8px;height:160px;padding:10px 0;}
.bar-item {flex:1;display:flex;flex-direction:column;align-items:center;gap:4px;}
.bar {width:100%;background:linear-gradient(180deg,#3b82f6,#22c55e);border-radius:4px 4px 0 0;transition:height 0.3s;}
.bar-label {font-size:10px;color:#94a3b8;text-align:center;max-width:60px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;}
.bar-value {font-size:11px;color:#e2e8f0;font-weight:700;}

/* 详情卡片 */
.section-title {
    font-size:16px;font-weight:700;color:#f8fafc;
    margin:28px 0 16px;padding-bottom:10px;border-bottom:2px solid #334155;
    display:flex;align-items:center;gap:8px;
}
.top-list {margin-bottom:30px;}
.stock-detail {
    background:#1e293b;border-radius:12px;padding:16px;margin-bottom:12px;
    border-left:4px solid #334155;border:1px solid #334155;transition:all 0.2s;
}
.stock-detail:hover {border-color:#475569;transform:translateX(4px);}
.stock-detail .header-row {
    display:flex;align-items:center;gap:12px;margin-bottom:10px;flex-wrap:wrap;
}
.stock-detail .rank {
    width:40px;height:40px;border-radius:50%;display:flex;align-items:center;justify-content:center;
    font-size:14px;font-weight:800;color:#fff;flex-shrink:0;
}
.stock-detail .name {font-size:16px;font-weight:700;color:#f8fafc;}
.stock-detail .code {font-size:11px;color:#94a3b8;}
.stock-detail .total {font-size:28px;font-weight:800;margin-left:auto;}
.stock-detail .rating {
    font-size:12px;font-weight:700;padding:4px 12px;border-radius:8px;text-align:center;
}
.stock-detail .meta-row {
    display:flex;gap:12px;font-size:11px;color:#94a3b8;margin-bottom:10px;flex-wrap:wrap;
}
.stock-detail .dim-row {display:grid;grid-template-columns:repeat(4,1fr);gap:12px;font-size:12px;}
.stock-detail .dim-card {
    background:#0f172a;border-radius:8px;padding:12px;border:1px solid #334155;
}
.stock-detail .dim-card .dim-title {font-size:10px;color:#94a3b8;margin-bottom:6px;}
.stock-detail .dim-card .dim-score {font-size:18px;font-weight:700;margin-right:8px;}
.stock-detail .dim-card .dim-detail {font-size:10px;color:#94a3b8;line-height:1.6;margin-top:6px;}

/* 完整表格 */
.table-container {overflow-x:auto;background:#1e293b;border-radius:12px;border:1px solid #334155;}
.stock-table {width:100%;border-collapse:collapse;min-width:800px;}
.stock-table th {
    background:#0f172a;color:#94a3b8;padding:12px 10px;text-align:left;
    font-weight:600;font-size:11px;position:sticky;top:0;z-index:10;
    border-bottom:1px solid #334155;cursor:pointer;user-select:none;transition:background 0.2s;
}
.stock-table th:hover {background:#1e293b;}
.stock-table td {padding:10px;border-bottom:1px solid #334155;font-size:12px;}
.stock-table tr:hover td {background:#273449;}

/* 表格内元素 */
.s-rank {color:#64748b;font-weight:700;width:40px;}
.s-name {font-weight:700;color:#f8fafc;}
.s-code {color:#94a3b8;font-size:11px;}
.s-pct {font-weight:700;text-align:right;min-width:60px;}
.s-pct.up {color:#ef4444;}
.s-pct.down {color:#22c55e;}
.s-scores {display:flex;gap:4px;justify-content:center;}
.s-score {
    width:24px;height:24px;border-radius:4px;display:flex;align-items:center;justify-content:center;
    font-size:11px;font-weight:700;color:#fff;
}
.s-total {font-size:14px;font-weight:800;text-align:center;min-width:50px;}
.s-rating {
    font-size:11px;font-weight:700;padding:2px 8px;border-radius:4px;text-align:center;display:inline-block;
}
.s-pe {color:#94a3b8;font-size:11px;min-width:80px;}
.s-turnover {color:#94a3b8;font-size:11px;text-align:right;}
.s-sector {color:#94a3b8;font-size:11px;}

.hidden-row {display:none !important;}

/* 底部 */
.footer {
    text-align:center;color:#475569;font-size:11px;margin-top:40px;
    padding:24px 0;border-top:1px solid #334155;
}
.footer p {margin:6px 0;}

/* 响应式布局 */
@media (max-width:1200px) {
    .charts {grid-template-columns:1fr;}
    .stock-detail .dim-row {grid-template-columns:repeat(2,1fr);}
}
@media (max-width:768px) {
    .summary {grid-template-columns:repeat(2,1fr);}
    .controls {flex-direction:column;}
    .controls .control-group {width:100%;}
    .controls select, .controls input {width:100%;}
    .stock-detail .rank {width:32px;height:32px;font-size:12px;}
    .stock-detail .name {font-size:14px;}
    .stock-detail .total {font-size:20px;}
    .stock-detail .dim-row {grid-template-columns:1fr;}
    .header h1 {font-size:18px;}
}
@media (max-width:480px) {
    .summary {grid-template-columns:1fr;}
    body {padding:10px;}
}
/* 投研分析区块 */
.research-section {margin:30px 0;}
.research-section .section-title {border-bottom:2px solid #fbbf24;color:#fbbf24;}
.research-card {
    background:linear-gradient(135deg,#1e293b 0%,#0f172a 100%);
    border-radius:14px;padding:20px;margin-bottom:16px;
    border:1px solid #334155;border-left:4px solid #3b82f6;
    transition:all 0.2s;
}
.research-card.s-tier {border-left-color:#fbbf24;box-shadow:0 0 16px rgba(251,191,36,0.1);}
.research-card.a-tier {border-left-color:#3b82f6;}
.research-card:hover {border-color:#475569;transform:translateX(4px);}
.research-card .rc-header {
    display:flex;align-items:center;gap:10px;margin-bottom:14px;flex-wrap:wrap;
    padding-bottom:12px;border-bottom:1px solid #334155;
}
.research-card .rc-name {font-size:17px;font-weight:800;color:#f8fafc;}
.research-card .rc-code {font-size:11px;color:#94a3b8;}
.research-card .rc-rating {
    font-size:11px;font-weight:800;padding:3px 10px;border-radius:6px;
}
.research-card .rc-score {font-size:13px;color:#94a3b8;margin-left:auto;}
.research-card .rc-score b {font-size:18px;}
.research-module {margin-bottom:14px;}
.research-module .rm-title {
    font-size:12px;font-weight:700;color:#38bdf8;margin-bottom:6px;
    display:flex;align-items:center;gap:4px;
}
.research-module .rm-content {
    font-size:12px;color:#cbd5e1;line-height:1.8;
    background:#0f172a;border-radius:8px;padding:12px;border:1px solid #1e293b;
}
.research-module.risk .rm-title {color:#f97316;}
.research-module.value .rm-title {color:#22c55e;}
.research-module .rm-content b {color:#f8fafc;}
.research-module .rm-content .tag {
    display:inline-block;font-size:10px;padding:1px 6px;border-radius:3px;
    background:#334155;color:#94a3b8;margin:2px 4px 2px 0;
}
.research-module .rm-content .tag.pos {background:#dcfce7;color:#16a34a;}
.research-module .rm-content .tag.neg {background:#fee2e2;color:#dc2626;}
.research-module .rm-content .tag.warn {background:#fef3c7;color:#d97706;}
@media (max-width:768px) {
    .research-card {padding:14px;}
    .research-card .rc-name {font-size:15px;}
    .research-module .rm-content {font-size:11px;padding:10px;}
}
</style>
</head>
<body>
<div class="container">
    <div class="header">
        <h1>📊 A股成交额TOP''' + str(total_stocks) + ''' 极简公司评分</h1>
        <div class="sub">Stock-Scorer v2.7 | 题材动量 · 大盘赚钱效应 · 基本面含行业前景 · 四维加权</div>
        <div class="meta">''' + date_str + ''' | 数据源：东方财富 + 腾讯K线 + 同花顺 + 新浪/乐股</div>
    </div>

    <!-- 统计卡片 -->
    <div class="summary">''')

# 添加统计卡片
for r in ["S", "A", "B", "C", "D"]:
    count = stats.get("rating_dist", {}).get(r, 0)
    html_parts.append(f'''
        <div class="card">
            <div class="count" style="color:{RATING_COLORS[r]}">{count}</div>
            <div class="label">{r}级 · {RATING_FULL[r]}</div>
        </div>''')

html_parts.append('''
    </div>
    <div style="font-size:11px;color:#94a3b8;margin:8px 0 16px;padding:8px 12px;background:#1e293b;border-radius:8px;">
        📐 评分等级标准：<span style="color:#22c55e;font-weight:700;">S级 ≥17分</span> ·
        <span style="color:#3b82f6;font-weight:700;">A级 13~17分</span> ·
        <span style="color:#f59e0b;font-weight:700;">B级 9~13分</span> ·
        <span style="color:#f97316;font-weight:700;">C级 5~9分</span> ·
        <span style="color:#dc2626;font-weight:700;">D级 &lt;5分</span>
    </div>''')

# 核心结论卡片
top3_stocks = sorted(results, key=lambda x: -x["total"])[:3]
s_count = stats.get("rating_dist", {}).get("S", 0)
a_count = stats.get("rating_dist", {}).get("A", 0)
top_sector = sorted_secs[0] if sorted_secs else ("-", {"avg": 0, "count": 0})
main_line = f"今日S级{s_count}只、A级{a_count}只，主线板块：{top_sector[0]}(均分{top_sector[1].get('avg',0):.1f})"

# 大盘赚钱效应 → 入场时机一句话摘要
if market_breadth and market_breadth.get("available"):
    mb_sc = market_breadth["score"]
    mb_phase = market_breadth["phase"]
    mb_pos = market_breadth["position"]
    main_line += f" · 大盘赚钱效应{mb_sc}分 {mb_phase}, {mb_pos}"

# 评级变化统计
rating_changes = {"up": [], "down": [], "new": []}
for r in results:
    code = r["code"]
    if code in prev_ratings:
        prev_r = prev_ratings[code]["rating"]
        curr_r = r["rating"]
        rating_order = {"S": 4, "A": 3, "B": 2, "C": 1}
        if rating_order.get(curr_r, 0) > rating_order.get(prev_r, 0):
            rating_changes["up"].append(r["name"])
        elif rating_order.get(curr_r, 0) < rating_order.get(prev_r, 0):
            rating_changes["down"].append(r["name"])
    else:
        rating_changes["new"].append(r["name"])

change_text = ""
if rating_changes["up"]:
    change_text += f" · 评级上调: {', '.join(rating_changes['up'][:5])}"
if rating_changes["down"]:
    change_text += f" · 评级下调: {', '.join(rating_changes['down'][:5])}"
if rating_changes["new"] and len(rating_changes["new"]) < 50:
    change_text += f" · 新晋上榜: {', '.join(rating_changes['new'][:5])}"

html_parts.append(f'''
    <!-- 核心结论 -->
    <div class="core-conclusion">
        <h2>🎯 今日核心结论</h2>
        <div class="main-line">{main_line}{change_text}</div>
        <div class="top3">''')

for i, r in enumerate(top3_stocks):
    rating = r["rating"]
    total = r["total"]
    sector = r.get("sector", "")
    sub = r.get("sub_theme", "")
    dims = [r["score_news"], r["score_tech"], r["score_fund"], r.get("score_theme", 0)]
    best_dim = max(dims)
    dim_names = ["消息", "技术", "基本", "热度"]
    best_name = dim_names[dims.index(best_dim)]
    reason = f"{sector} · {sub}" if sub else sector
    reason += f" · {best_name}面最强({best_dim}/5)"

    html_parts.append(f'''
            <div class="top3-item" style="border-left:3px solid {RATING_COLORS[rating]};">
                <div class="t-name">#{i+1} {r["name"]}</div>
                <div class="t-score" style="color:{RATING_COLORS[rating]}">{total:.1f} <span style="font-size:12px;color:#94a3b8;">/ 20</span></div>
                <div class="t-reason">{reason}</div>
            </div>''')

html_parts.append('''
        </div>
    </div>

    <!-- 大盘赚钱效应 -->
''')

# 大盘赚钱效应 banner (v2.7 新增)
if market_breadth and market_breadth.get("available"):
    sc = market_breadth["score"]
    phase = market_breadth["phase"]
    advice = market_breadth["advice"]
    position = market_breadth["position"]
    raw = market_breadth.get("raw", {}) or {}
    # 颜色语义: 高分(可入场)=红, 中性=橙, 低分(亏钱效应/回避)=绿 (A股惯例 红涨绿跌)
    if sc >= 55:
        bar_color = "#ef4444"
    elif sc >= 40:
        bar_color = "#f59e0b"
    else:
        bar_color = "#22c55e"
    up = raw.get("up"); down = raw.get("down"); zt = raw.get("zt"); dt = raw.get("dt")
    flat = raw.get("flat")
    idx = raw.get("index", {}) or {}
    idx_str = " · ".join(f"{k}{v:+.2f}%" for k, v in idx.items()) if idx else "—"
    amt = raw.get("amount_yi")
    amt_str = (f"{amt/10000:.2f}万亿" if (amt and amt >= 10000) else (f"{amt:.0f}亿" if amt else "—"))
    comp = market_breadth.get("components", {}) or {}
    comp_rows = "".join(
        f"<div style='display:flex;justify-content:space-between;font-size:11px;padding:3px 0;border-bottom:1px solid #1e293b;'>"
        f"<span style='color:#94a3b8;'>{k}</span><span style='font-weight:700;color:#e2e8f0;'>{v:+.1f}</span></div>"
        for k, v in comp.items())
    html_parts.append(f'''
    <div class="market-gauge" style="background:linear-gradient(135deg,#0f172a,#1e293b);border:1px solid #334155;border-radius:12px;padding:18px;margin:16px 0;">
        <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:12px;">
            <h2 style="font-size:15px;margin:0;">🌊 大盘赚钱效应 · 入场时机评估</h2>
            <span style="font-size:11px;color:#64748b;">数据源:{raw.get('source','-')}</span>
        </div>
        <div style="display:flex;gap:20px;align-items:center;flex-wrap:wrap;">
            <div style="text-align:center;min-width:140px;">
                <div style="font-size:48px;font-weight:900;color:{bar_color};line-height:1;">{sc}</div>
                <div style="font-size:11px;color:#94a3b8;margin-top:4px;">赚钱效应评分 / 100</div>
                <div style="font-size:13px;font-weight:800;color:{bar_color};margin-top:6px;">{phase}</div>
            </div>
            <div style="flex:1;min-width:220px;">
                <div style="font-size:12px;color:#e2e8f0;margin-bottom:8px;">📈 涨跌家数：<b style="color:#ef4444;">上涨{up}</b> / <b style="color:#22c55e;">下跌{down}</b>{f" / 平{flat}" if flat else ""}　涨停{zt} · 跌停{dt}</div>
                <div style="font-size:12px;color:#e2e8f0;margin-bottom:8px;">📊 三大指数：{idx_str}</div>
                <div style="font-size:12px;color:#e2e8f0;margin-bottom:8px;">💰 全市场成交额：{amt_str}</div>
                <div style="background:#020617;border-radius:8px;padding:10px;">
                    <div style="font-size:12px;font-weight:800;color:{bar_color};margin-bottom:4px;">💡 {advice}</div>
                    <div style="font-size:14px;font-weight:900;color:{bar_color};">{position}</div>
                </div>
            </div>
        </div>
        <div style="margin-top:12px;">
            <div style="font-size:11px;color:#64748b;margin-bottom:4px;">评分分项贡献（满分100）</div>
            {comp_rows}
        </div>
        <div style="font-size:10px;color:#475569;margin-top:10px;">⚠️ 本模块为纯量化情绪模型（涨跌广度 + 涨停净额 + 指数强度 + 中位涨幅），仅供仓位参考，不构成投资建议。</div>
    </div>''')
else:
    html_parts.append('''
    <div style="background:#1e293b;border:1px solid #334155;border-radius:12px;padding:16px;margin:16px 0;font-size:12px;color:#94a3b8;">
        🌊 大盘赚钱效应：数据获取失败（接口暂不可用），本次跳过该评估，不影响个股评分。
    </div>''')

html_parts.append('''
    <!-- 快捷筛选按钮 -->
    <div class="quick-filters">
        <button class="quick-btn active" data-quick="all">全部</button>
        <button class="quick-btn" data-quick="SA">⭐ 只看S/A级</button>
        <button class="quick-btn" data-quick="S">🥇 只看S级</button>
        <button class="quick-btn" data-quick="undervalued">💰 只看低估(FwdPE&lt;30)</button>
        <button class="quick-btn" data-quick="growth">🚀 高增长(增速≥30%)</button>
        <button class="quick-btn" data-quick="low_pos">📉 低位股(分位&lt;30%)</button>
        <button class="quick-btn" data-quick="hot">🔥 热门(热度≥4)</button>
        <button class="quick-btn" data-quick="up">📈 评级上调</button>
        <button class="quick-btn" data-quick="new">🆕 新晋上榜</button>
    </div>

    <!-- 今日数据总结 + 组合推荐 -->
    <div class="charts">
        <!-- 今日数据总结 -->
        <div class="chart-card" style="padding:16px;">
            <h3 style="font-size:13px;margin-bottom:10px;">📋 今日数据总结</h3>
            <div style="display:grid;grid-template-columns:repeat(3,1fr);gap:8px;margin-bottom:10px;">
                <div style="background:#0f172a;border-radius:6px;padding:8px;text-align:center;">
                    <div style="font-size:10px;color:#94a3b8;">覆盖个股</div>
                    <div style="font-size:18px;font-weight:800;color:#f8fafc;">''' + str(total_stocks) + '''只</div>
                </div>
                <div style="background:#0f172a;border-radius:6px;padding:8px;text-align:center;">
                    <div style="font-size:10px;color:#94a3b8;">S+A级占比</div>
                    <div style="font-size:18px;font-weight:800;color:#22c55e;">''' + f"{(stats.get('rating_dist',{}).get('S',0)+stats.get('rating_dist',{}).get('A',0))/max(total_stocks,1)*100:.0f}" + '''%</div>
                </div>
                <div style="background:#0f172a;border-radius:6px;padding:8px;text-align:center;">
                    <div style="font-size:10px;color:#94a3b8;">主线板块</div>
                    <div style="font-size:18px;font-weight:800;color:#3b82f6;">''' + str(len([s for s,v in sec_avg.items() if v.get("count",0)>=5])) + '''个</div>
                </div>
            </div>
            <div style="font-size:11px;color:#94a3b8;margin-bottom:6px;">板块均分TOP5</div>''')

for s, v in sorted_secs:
    cnt = v.get("count", 0)
    avg = v.get("avg", 0)
    bar_w = round(min(avg / 20 * 100, 100), 1)
    html_parts.append(f'''
                <div style="display:flex;align-items:center;gap:6px;margin-bottom:4px;">
                    <span style="color:#e2e8f0;font-size:11px;min-width:60px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;">{s}</span>
                    <div style="flex:1;background:#0f172a;border-radius:3px;height:12px;overflow:hidden;">
                        <div style="width:{bar_w}%;height:100%;background:linear-gradient(90deg,#3b82f6,#22c55e);border-radius:3px;"></div>
                    </div>
                    <span style="color:#94a3b8;font-size:10px;min-width:55px;text-align:right;">{avg:.1f} ({cnt}只)</span>
                </div>''')

html_parts.append('''
        </div>

        <!-- 组合推荐 -->
        <div class="chart-card" style="padding:16px;">
            <h3 style="font-size:13px;margin-bottom:10px;">🎯 组合推荐 · 板块分散+估值合理</h3>''')

for i, r in enumerate(portfolio_picks):
    rating = r["rating"]
    total = r["total"]
    fwd = r.get("fwd_pe", "-")
    growth = r.get("growth", "-")
    sub = r.get("sub_theme", r["sector"])
    tech = r.get("tech", {})
    position = tech.get("position", 0)
    dims = [r["score_news"], r["score_tech"], r["score_fund"], r.get("score_theme", 0)]
    min_dim = min(dims)
    growth_str = f"{growth:+.0f}%" if isinstance(growth, (int, float)) else str(growth)
    fwd_str = f"{fwd:.1f}x" if isinstance(fwd, (int, float)) else str(fwd)
    pos_str = f"分位{position:.0f}%" if position else ""
    balance_tag = "均衡" if min_dim >= 3 else ""
    tags = [t for t in [sub, pos_str, balance_tag] if t]

    html_parts.append(f'''
                <div style="background:#0f172a;border-radius:6px;padding:8px 10px;margin-bottom:6px;border-left:3px solid {RATING_COLORS[rating]};">
                    <div style="display:flex;align-items:center;gap:6px;">
                        <span style="font-size:13px;font-weight:800;color:#f8fafc;">{i+1}. {r["name"]}</span>
                        <span style="font-size:10px;font-weight:700;padding:1px 6px;border-radius:3px;background:{RATING_BG[rating]};color:{RATING_COLORS[rating]};">{rating}</span>
                        <span style="font-size:14px;font-weight:800;color:{RATING_COLORS[rating]};margin-left:auto;">{total:.1f}</span>
                    </div>
                    <div style="display:flex;gap:8px;font-size:10px;color:#94a3b8;margin-top:3px;flex-wrap:wrap;">
                        <span>FwdPE {fwd_str}</span>
                        <span>增速 {growth_str}</span>
                        <span>{' · '.join(tags)}</span>
                    </div>
                </div>''')

html_parts.append('''
                <div style="font-size:9px;color:#475569;margin-top:6px;padding:6px 8px;background:rgba(249,115,22,0.1);border-radius:4px;">
                    ⚠️ 筛选：S/A级 + FwdPE≤60 + 增速≥15% + 分位≤95% + 板块去重 + 四维均衡加分，仅供研究参考
                </div>
        </div>
    </div>''')

# 准备评级数据
rating_dist = stats.get("rating_dist", {})

html_parts.append('''
    <!-- 深度分析 -->
    <div class="top-list">
        <div class="section-title">🏆 S/A级综合排名 · 投资建议详情 (''' + str(len(top20)) + '''只)</div>''')

# 生成详情卡片
for idx, r in enumerate(top20):
    i = idx + 1
    pct = r.get("pct_chg", 0) or 0
    pct_cls = "up" if pct > 0 else "down" if pct < 0 else ""
    rating = r["rating"]
    total = r["total"]
    rank_color = "#f59e0b" if i == 1 else "#94a3b8" if i == 2 else "#cd853f" if i == 3 else "#64748b"

    pe = r.get("pe", 0)
    fwd = r.get("fwd_pe")
    growth = r.get("growth")
    pe_str = f"PE(TTM) {pe:.0f}x" if pe and pe > 0 else "PE(TTM) N/A"
    fwd_str = f" → Fwd {fwd:.0f}x" if fwd else ""
    g_str = f" 增速 {growth:+.0f}%" if growth else " 增速 N/A"
    peg_val = r.get("pe_ladder")
    peg_str = ""
    if peg_val and isinstance(peg_val, dict):
        yrs = sorted(peg_val.keys())
        peg_str = " 阶梯" + "→".join(f"{peg_val[y]}" for y in yrs)
    sub_theme = r.get("sub_theme", "")
    turnover_yi = (r.get("turnover", 0) or 0) / 100000000

    news_reasons = r.get("news_reasons", [])
    fund_reasons = r.get("fund_reasons", [])
    theme_reasons = r.get("theme_reasons", r.get("flow_reasons", []))
    tech = r.get("tech", {})

    news_html = "<br>".join([n.replace("⚠️", "") for n in news_reasons[:3]]) if news_reasons else "无消息数据"
    risk_parts = [n for n in news_reasons if "⚠" in n]
    risk_html = "<br>".join([n.replace("⚠️", "") for n in risk_parts[:2]]) if risk_parts else ""

    boll_label = tech.get("boll_label", "")
    boll_pos = tech.get("boll_pos")
    boll_info = f"BOLL:{boll_label}({boll_pos:.0f}%)" if boll_label and boll_pos is not None else ""

    justification = []
    if r['score_news'] >= 4: justification.append("消息面强劲")
    elif r['score_news'] <= 1: justification.append("消息面疲弱")
    if r['score_tech'] >= 4: justification.append("技术面看多")
    elif r['score_tech'] <= 1: justification.append("技术面承压")
    if r['score_fund'] >= 4: justification.append("估值吸引力强")
    elif r['score_fund'] <= 1: justification.append("估值偏高")
    if r.get('score_theme', 0) >= 4: justification.append("主线题材热度高")
    elif r.get('score_theme', 0) <= 1: justification.append("题材冷门")
    if not justification: justification = ["各维度均衡"]

    # 评级变化标签
    change_tag = ""
    if r["code"] in prev_ratings:
        prev_r = prev_ratings[r["code"]]["rating"]
        rating_order = {"S": 4, "A": 3, "B": 2, "C": 1}
        if rating_order.get(rating, 0) > rating_order.get(prev_r, 0):
            change_tag = '<span class="rating-change rating-up">↑{prev_r}→{rating}</span>'.format(prev_r=prev_r, rating=rating)
        elif rating_order.get(rating, 0) < rating_order.get(prev_r, 0):
            change_tag = '<span class="rating-change rating-down">↓{prev_r}→{rating}</span>'.format(prev_r=prev_r, rating=rating)
    else:
        change_tag = '<span class="rating-change rating-new">NEW</span>'

    s_class = "s-level" if rating == "S" else ""
    html_parts.append(f'''
        <div class="stock-detail {s_class}" data-rating="{rating}" data-sector="{r.get('sector','')}" data-code="{r['code']}" data-name="{r['name']}">
            <div class="header-row">
                <div class="rank" style="background:{rank_color}">#{i}</div>
                <div>
                    <div class="name">{r['name']}{change_tag}</div>
                    <div class="code">{r['code']} · {r.get('sector','')}</div>
                </div>
                <span class="s-pct {pct_cls}" style="font-size:13px;font-weight:700;">{pct:+.2f}%</span>
                <div class="total" style="color:{RATING_COLORS[rating]}">{total:.1f}</div>
                <div class="rating" style="background:{RATING_BG[rating]};color:{RATING_COLORS[rating]}">{rating}</div>
            </div>
            <div class="meta-row">
                <span>💰 成交额: {turnover_yi:.0f}亿</span>
                <span>📊 {pe_str}{fwd_str}{g_str}{peg_str}</span>
                <span>🏷️ {r['score_news']}/5消息 · {r['score_tech']}/5技术 · {r['score_fund']}/5基本 · {r.get('score_theme',0)}/5热度</span>
            </div>
            <div class="dim-row">
                <div class="dim-card">
                    <div class="dim-title">📰 消息面 ({r['score_news']}/5)</div>
                    <div class="dim-score">{r['score_news']}</div>
                    <div class="dim-detail">{news_html}</div>
                </div>
                <div class="dim-card">
                    <div class="dim-title">📈 技术面 ({r['score_tech']}/5)</div>
                    <div class="dim-score">{r['score_tech']}</div>
                    <div class="dim-detail">趋势: {tech.get('trend','N/A')} · 分位: {tech.get('position','N/A')}%{f' · {boll_info}' if boll_info else ''} · {tech.get('reason','')}</div>
                </div>
                <div class="dim-card">
                    <div class="dim-title">📊 基本面 ({r['score_fund']}/5)</div>
                    <div class="dim-score">{r['score_fund']}</div>
                    <div class="dim-detail">{', '.join(fund_reasons[:3]) if fund_reasons else '数据不足'}</div>
                </div>
                <div class="dim-card">
                    <div class="dim-title">🔥 题材热度 ({r.get('score_theme',0)}/5){f' · {sub_theme}' if sub_theme else ''}</div>
                    <div class="dim-score">{r.get('score_theme',0)}</div>
                    <div class="dim-detail">{', '.join(theme_reasons[:3]) if theme_reasons else '数据不足'}</div>
                </div>
            </div>''')

    if risk_html:
        html_parts.append(f'''
            <div style="font-size:11px;color:#f97316;margin-top:10px;padding:10px;background:rgba(249,115,22,0.1);border-radius:8px;">⚠️ {risk_html}</div>''')

    html_parts.append(f'''
            <div style="font-size:11px;color:#38bdf8;margin-top:8px;">💡 评级原因: {', '.join(justification[:4])} → {rating}级 · {r.get('advice',RATING_FULL.get(rating,''))}</div>
        </div>''')

html_parts.append('''
    </div>

    <!-- S/A级公司深度投研分析 -->
    <div class="research-section">
        <div class="section-title">📑 S/A级公司专业投研分析报告</div>
        <div style="font-size:11px;color:#64748b;margin-bottom:16px;padding:10px;background:#1e293b;border-radius:8px;">
            📌 本章节基于行业研究数据、公司基本面指标、财务估值模型与竞争格局分析框架，对全部S级与A级公司进行系统性投研覆盖。
            分析模块包含：<b style="color:#38bdf8;">核心业务竞争力评估</b> · <b style="color:#38bdf8;">行业发展前景预测</b> · <b style="color:#f97316;">潜在风险因素识别</b> · <b style="color:#22c55e;">投资价值分析</b>
        </div>''')

# ========== 行业研究数据库(按sub_theme细分) ==========
# 细分行业数据库优先匹配；其次回退到板块数据库
SUBTHEME_RESEARCH = {
    # --- 光模块/光通信/光芯片 ---
    "光模块": {
        "outlook": "AI算力竞赛持续升级，全球光模块市场规模将从2025年165亿美元跃升至2026年260亿美元，年增速超57%。800G以上光模块出货占比预计从2024年19.5%升至2026年60%以上，1.6T产品进入规模化商用阶段。英伟达VeraRubin平台密集拉货，Meta/谷歌/微软合计800G采购超2400万只，1.6T下半年启动规模采购。硅光方案在1.6T光模块中占比达70%-80%，CPO技术迎来产业化起点。高端产品交付周期拉长至12周以上，行业景气度持续超预期。",
        "drivers": ["AI算力基建超级周期", "1.6T/3.2T光模块技术迭代", "800G出货量同比增260%", "硅光与CPO技术产业化", "英伟达新平台拉货"],
        "risks": ["核心光芯片(EML)产能紧张交期排至2027年", "光模块价格战+客户集中度高", "海外技术管制升级", "估值处于历史高位", "下游云厂商资本开支波动"]
    },
    "光模块/AOC": {
        "outlook": "AI算力中心高密度互联需求推动AOC(有源光缆)及光模块市场爆发，800G/1.6T光模块规模化出货带动AOC需求同步增长。全球AI专用光收发模块市场2026年达260亿美元，年增速超57%，高速AOC作为数据中心内部互联核心组件，受益于万卡级智算集群建设潮。",
        "drivers": ["AI数据中心高密度互联需求", "800G/1.6T规模化出货", "万卡级智算集群建设", "硅光方案渗透提升"],
        "risks": ["核心光芯片产能瓶颈", "客户集中度高风险", "技术路线竞争(CPO/LPO)", "海外供应链不确定性"]
    },
    "光模块/CPO": {
        "outlook": "CPO(共封装光学)技术迎来产业化起点，1.6T光模块中硅光方案占比达70%-80%，CPO逐步与传统可插拔方案长期共存。全球光模块市场规模2026年达260亿美元，年增速超57%。CPO技术将光引擎与交换芯片共封装，大幅降低功耗与成本，适配AI算力中心高密度低功耗需求，是下一代光互联核心方向。",
        "drivers": ["CPO技术产业化突破", "1.6T硅光方案主导", "AI算力中心低功耗需求", "国产化率持续提升"],
        "risks": ["CPO商业化落地进度不确定", "技术良率爬坡风险", "可插拔方案长期共存竞争", "高端光芯片供给紧张"]
    },
    "光芯片/激光器": {
        "outlook": "光芯片是光模块产业链核心环节，价值占比持续提升。英伟达以40亿美元长单锁定Lumentum、Coherent磷化铟光芯片产能至2028年，凸显上游核心器件紧缺格局。2026年磷化铟衬底全球需求从200-210万片提升至260-300万片，有效产能仅约75万片，缺口超70%。薄膜铌酸锂、磷化铟、空芯光纤三大技术方向成为核心增量。国产光芯片替代窗口加速打开。",
        "drivers": ["EML光芯片供给极度紧缺", "磷化铟衬底缺口超70%", "英伟达40亿美元长单锁定", "国产替代窗口加速", "1.6T/3.2T技术迭代"],
        "risks": ["海外技术垄断(EML产能集中于海外)", "高端光芯片研发壁垒高", "客户认证周期长", "技术路线替代风险"]
    },
    "光芯片/PLC": {
        "outlook": "PLC(平面光波导)光芯片作为光通信核心器件，受益于AI算力中心建设与无源光网络升级。光芯片上游供给持续紧缺，国产替代逻辑清晰。硅光集成技术推动PLC光芯片向高集成度、低功耗方向演进。",
        "drivers": ["AI算力中心光互联需求", "光芯片国产替代加速", "硅光集成技术演进", "无源光网络升级"],
        "risks": ["海外技术垄断", "研发投入大周期长", "客户认证门槛高", "技术迭代不确定性"]
    },
    "光通信/海缆": {
        "outlook": "光通信与海底电缆双赛道受益于AI算力基建与海上风电建设。AI数据中心东西向流量激增推动高速光通信需求，海上风电装机持续增长拉动海缆需求。光通信向800G/1.6T升级，海缆向高压直流、柔性直流演进。",
        "drivers": ["AI数据中心光通信需求", "海上风电装机增长", "800G/1.6T光通信升级", "海缆高压直流化"],
        "risks": ["海上风电建设进度不确定性", "光通信价格竞争", "原材料(铜/铝)价格波动", "产能扩张压力"]
    },
    "光通信/光纤": {
        "outlook": "光纤光缆行业受益于AI算力基建与F5G-A全光网建设。多芯光纤、空芯光纤、多模光纤等新品类适配AI时代超高带宽需求。全球AI光模块市场规模2026年达260亿美元，拉动光纤光缆配套需求。空芯光纤作为下一代核心方向，传输时延降低30%以上。",
        "drivers": ["AI算力基建光纤需求", "空芯光纤技术突破", "F5G-A全光网建设", "多芯光纤产业化"],
        "risks": ["传统光纤产能过剩", "空芯光纤商业化进度不确定", "运营商集采价格压力", "原材料成本波动"]
    },
    "光通信设备": {
        "outlook": "光通信设备行业受益于AI算力网络与5G-A建设。OTN、SPN等高速光传输设备需求增长，AI数据中心互联推动400G/800G光传输设备规模化部署。F5G-A全光网借助AI自动驾驶技术实现故障毫秒级定位与带宽智能调度。",
        "drivers": ["AI数据中心互联需求", "5G-A网络建设", "F5G-A全光网部署", "400G/800G光传输规模化"],
        "risks": ["运营商资本开支波动", "行业竞争激烈", "技术标准演进不确定性", "海外市场壁垒"]
    },
    # --- AI服务器/算力设备 ---
    "AI服务器": {
        "outlook": "IDC预测2026年全球AI服务器出货量有望突破200万台，单台PCB价值量约为普通服务器的近10倍。英伟达VeraRubin平台密集拉货，算力硬件订单能见度延伸至2027年。AI服务器向高功耗、高密度方向演进，液冷散热成为标配。国内AI服务器厂商受益于国产算力芯片(华为昇腾、寒武纪)放量与信创采购。AI推理需求爆发推动算力架构从通用GPU向专用ASIC转移，博通、Marvell定制化芯片业务强劲增长。",
        "drivers": ["全球AI服务器出货量突破200万台", "英伟达VeraRubin平台拉货", "国产算力芯片放量", "AI推理ASIC化趋势", "液冷散热标配化"],
        "risks": ["GPU供给受限(英伟达管制)", "服务器价格竞争激烈", "下游云厂商资本开支波动", "国产芯片生态成熟度不足", "液冷技术良率风险"]
    },
    "AI服务器/算力": {
        "outlook": "AI算力需求指数级增长，全球智算中心建设潮持续推进。国产AI服务器受益于信创采购与国产算力芯片(昇腾/寒武纪)生态成熟。AI推理需求爆发推动算力架构从通用GPU向专用ASIC转移，算力节点互联需求同步增长。东数西算工程推进与万兆光网试点落地提供广阔场景。",
        "drivers": ["智算中心建设潮", "国产算力芯片生态成熟", "AI推理ASIC化", "东数西算工程推进", "信创采购放量"],
        "risks": ["GPU供给受限", "国产芯片性能差距", "算力中心建设节奏不确定性", "行业竞争加剧", "技术迭代风险"]
    },
    "5G/算力设备": {
        "outlook": "5G-A网络建设与AI算力基建双轮驱动通信设备需求。AI数据中心互联推动高速交换机与光传输设备规模化部署。运营商资本开支向算力网络倾斜，400G/800G光传输设备需求增长。国产通信设备商在全球市场竞争力持续提升。",
        "drivers": ["5G-A网络建设", "AI算力网络投资", "400G/800G光传输部署", "国产设备全球竞争力提升"],
        "risks": ["运营商资本开支波动", "海外市场贸易壁垒", "行业竞争激烈", "技术标准演进不确定性"]
    },
    # --- 存储芯片/模组 ---
    "存储芯片": {
        "outlook": "存储行业迎来AI驱动的超级周期。AI服务器HBM(高带宽内存)需求暴增，SK海力士375层3D NAND量产验证完成，以钼代钨技术导入推动存储密度跃升。DDR5渗透率快速提升，LPDDR5X在AI端侧设备放量。国产存储芯片受益于信创采购与国产替代加速，长江存储、长鑫存储等国产厂商技术突破缩小与海外差距。存储价格周期上行，2026年景气度持续修复。",
        "drivers": ["AI服务器HBM需求暴增", "375层3D NAND量产(以钼代钨)", "DDR5渗透率快速提升", "国产替代加速", "存储价格周期上行"],
        "risks": ["海外存储巨头(三星/SK海力士)技术领先", "存储价格周期波动", "先进制程设备受限", "消费电子需求疲软", "国产良率爬坡风险"]
    },
    "存储控制器": {
        "outlook": "存储控制器芯片受益于SSD渗透率提升与AI数据中心存储需求增长。PCIe 5.0控制器规模化量产，PCIe 6.0研发推进。国产存储控制器在信创采购与消费级SSD市场加速替代，企业级市场突破进行中。AI推理端侧部署推动高带宽存储控制器需求。",
        "drivers": ["PCIe 5.0规模化量产", "AI数据中心存储需求", "信创采购加速", "国产替代窗口", "SSD渗透率提升"],
        "risks": ["海外主控芯片厂商竞争", "企业级市场认证周期长", "技术迭代速度快", "消费电子需求波动"]
    },
    "存储模组": {
        "outlook": "存储模组行业受益于AI数据中心内存扩容与SSD需求增长。DDR5模组渗透率快速提升，企业级SSD在AI服务器中放量。国产存储模组厂商受益于国产存储芯片放量与信创采购，市场份额持续提升。存储价格上行周期带动模组厂商盈利修复。",
        "drivers": ["AI服务器内存扩容需求", "DDR5模组渗透提升", "企业级SSD放量", "国产替代加速", "存储价格上行周期"],
        "risks": ["存储芯片价格波动", "海外模组厂商竞争", "客户集中度风险", "库存周期波动"]
    },
    "内存接口": {
        "outlook": "内存接口芯片受益于DDR5渗透率提升与AI服务器内存需求爆发。DDR5 RCD/DB/SPD芯片需求量较DDR4翻倍，AI服务器高带宽内存需求推动MRCD/MDB等新品类增长。PCIe 5.0 Retimer芯片规模化出货，CXL内存池化技术开启新增长曲线。国产内存接口芯片厂商在全球市场竞争力持续提升。",
        "drivers": ["DDR5渗透率快速提升", "AI服务器内存需求爆发", "PCIe 5.0 Retimer出货", "CXL内存池化新技术", "国产竞争力提升"],
        "risks": ["DDR5渗透节奏不及预期", "海外竞争(IDT/Rambus)", "技术迭代风险", "AI服务器出货波动"]
    },
    "存储/云服务": {
        "outlook": "存储与云服务受益于AI算力中心建设与数据量爆发式增长。AI训练与推理产生海量数据存储需求，对象存储、块存储等云存储服务需求激增。国产云服务厂商在AI基础设施领域加速布局，信创采购推动国产存储解决方案放量。",
        "drivers": ["AI数据存储需求爆发", "云存储服务增长", "信创采购放量", "国产替代加速"],
        "risks": ["云厂商资本开支波动", "存储价格竞争", "数据安全合规风险", "技术迭代不确定性"]
    },
    # --- 半导体设备/测试 ---
    "半导体设备": {
        "outlook": "半导体设备国产化率持续提升，先进制程需求推动设备资本开支高增。美日荷设备出口管制加速国产替代进程，刻蚀、薄膜沉积、清洗等关键设备国产化率突破。拓荆CVD设备、北方华创刻蚀/薄膜设备在先进制程验证持续推进。2026年中国半导体设备市场规模预计达347亿元，先进制程扩产带动高端设备需求。",
        "drivers": ["美日荷设备出口管制加速国产替代", "先进制程扩产", "刻蚀/CVD/清洗设备国产化突破", "半导体设备市场规模达347亿元", "AI芯片需求拉动"],
        "risks": ["海外设备管制升级", "先进制程技术壁垒高", "客户认证周期长", "研发投入大周期长", "下游晶圆厂资本开支波动"]
    },
    "半导体设备/CVD": {
        "outlook": "CVD(化学气相沉积)设备是半导体制造核心设备，受益于先进制程扩产与国产替代加速。PECVD、ALD等薄膜沉积设备在3D NAND、逻辑芯片先进制程中需求增长。国产CVD设备在28nm及以上制程实现规模化出货，14nm及以下制程验证推进。3D NAND向375层以上演进，CVD设备需求量持续增长。",
        "drivers": ["先进制程扩产拉动CVD需求", "3D NAND向375层演进", "国产CVD设备规模化出货", "美日荷管制加速替代"],
        "risks": ["先进制程技术壁垒", "海外设备巨头(应材/泛林)竞争", "客户认证周期长", "研发投入大"]
    },
    "半导体测试": {
        "outlook": "半导体测试设备受益于AI芯片出货量增长与先进封装测试需求提升。AI芯片复杂度提升推动测试设备需求增长，SoC测试、存储测试、射频测试等细分赛道景气上行。国产测试设备在中低端市场实现突破，高端市场加速渗透。先进封装(2.5D/3D)带动测试设备新增量。",
        "drivers": ["AI芯片出货量增长", "先进封装测试需求", "国产测试设备突破", "SoC/存储/射频测试景气"],
        "risks": ["海外测试巨头(泰瑞达/爱德万)垄断", "高端测试设备技术壁垒", "下游芯片出货波动", "研发投入大"]
    },
    # --- PCB/封装 ---
    "PCB": {
        "outlook": "AI服务器升级推动PCB从电子级向半导体级跃迁，单台AI服务器PCB价值量约为普通服务器的近10倍。头部厂商AI服务器及800G/1.6T光模块高端PCB订单已排至2026年Q4。2025年9家头部PCB企业资本开支267亿元(同比+111%)，2026Q1达125亿元(同比+182%)。高层数(78层M9正交背板)、HDI、高频高速板需求结构性增长，行业定价权向具备技术壁垒的头部厂商集中。部分核心客户要求锁定未来6-12个月产能。",
        "drivers": ["AI服务器PCB价值量提升10倍", "高端PCB订单排至Q4", "资本开支同比+182%", "78层M9正交背板需求", "客户锁定6-12个月产能"],
        "risks": ["扩产产能消化风险", "中低端PCB竞争加剧", "客户集中度高", "良率爬坡不确定性", "技术迭代风险"]
    },
    "PCB/封装": {
        "outlook": "PCB与先进封装受益于AI芯片升级与高密度互联需求。AI服务器PCB层数持续增加(78层M9正交背板)，先进封装(2.5D/3D/CoWoS)带动封装基板需求增长。Chiplet技术推动封装基板向高层数、细线距方向演进。国产封装基板在高阶层板突破，AI芯片封装基板国产替代窗口打开。",
        "drivers": ["AI服务器PCB层数增加", "先进封装(2.5D/3D/CoWoS)需求", "Chiplet技术推动基板升级", "封装基板国产替代"],
        "risks": ["海外基板厂商(欣兴/南电)竞争", "先进封装技术壁垒", "客户认证周期长", "良率爬坡风险"]
    },
    # --- 消费电子/代工/其他 ---
    "消费电子/代工": {
        "outlook": "消费电子代工龙头受益于AI端侧设备放量与汽车电子业务拓展。AI手机、AI PC推动消费电子换机周期，单机BOM价值量提升。汽车电子(智能座舱、自动驾驶)成为第二增长曲线。全球化产能布局优化成本结构，印度、越南等东南亚产能加速落地。",
        "drivers": ["AI手机/AI PC换机周期", "汽车电子第二增长曲线", "全球化产能布局", "单机BOM价值量提升"],
        "risks": ["消费电子需求疲软", "苹果产业链依赖度", "东南亚产能爬坡风险", "汇率波动"]
    },
    "激光设备": {
        "outlook": "激光设备受益于高端制造、新能源、半导体等下游需求增长。AI赋能激光加工，单模万瓦光纤激光器通过'光学优化+AI智能调控'实现光束质量世界纪录。激光在低空经济(清障/反无)、船舶海工、工程机械等新场景拓展。国产激光器在高功率领域突破海外垄断。",
        "drivers": ["AI赋能激光加工", "低空经济新场景", "高功率国产替代", "新能源/半导体需求"],
        "risks": ["下游制造业资本开支波动", "激光器价格竞争", "海外技术壁垒", "应用场景拓展不确定性"]
    },
    "热管理": {
        "outlook": "AI服务器功耗大幅提升(单机柜>100kW)推动液冷散热成为标配，热管理行业迎来量价齐升。液冷渗透率从2025年15%预计提升至2026年40%以上，冷板式、浸没式液冷方案并行发展。新能源汽车热管理系统向集成化、智能化演进，热泵空调渗透率提升。国产热管理厂商在AI服务器与新能源汽车双赛道布局。",
        "drivers": ["AI服务器液冷标配化(渗透率40%+)", "单机柜功耗>100kW", "新能源汽车热管理集成化", "热泵空调渗透提升"],
        "risks": ["液冷技术路线竞争(冷板/浸没)", "AI服务器出货波动", "新能源汽车竞争加剧", "原材料(铝/铜)价格波动"]
    },
    # --- 电解液/氟化工/锂电材料 ---
    "电解液": {
        "outlook": "电解液行业迎来周期反转与格局重构拐点。2025年全球电解液市场规模约800亿元，出货量约150万吨，中国产能占比85%达130万吨/年。2026年全球锂电池电解液需求量预计达367万吨，2030年攀升至560万吨，长期空间明确。宁德时代与新宙邦、永太科技签订77万吨长协，天赐材料手握超340万吨下游长单，行业CR5达75%-80%，马太效应加剧。六氟磷酸锂作为核心溶质，2025年市场规模587.77亿元，2026年预计698.57亿元；价格从2025年低点4.7万元/吨反弹至18万元/吨(涨幅283%)，2026年需求40.4万吨对有效产能41.4万吨，紧平衡格局确立，全年价格中枢上移至17-19万元/吨。储能成为最大需求增长点，2026年全球储能装机增速超50%。政策端：双碳目标推进、容量电价机制2026年1月落地，储能形成容量保底+现货+辅助服务三重盈利模式，下游需求确定性增强。技术迭代：LiFSI新型锂盐渗透率提升(800V高压快充普及)，VC/FEC高端添加剂毛利丰厚，固态电解质进入中试阶段。行业竞争从价格战转向一体化、配方研发、高端添加剂三大核心壁垒，纯外购锂盐的小厂加速出清。",
        "drivers": ["六氟磷酸锂价格周期反转(涨幅283%)", "全球电解液需求2026年达367万吨", "储能装机增速超50%", "宁德时代77万吨+天赐340万吨长协锁定", "LiFSI/VC/FEC高端材料量价齐升", "容量电价机制落地"],
        "risks": ["六氟磷酸锂价格高位回落风险", "行业名义产能过剩(利用率<60%)", "碳酸锂价格波动挤压毛利", "固态电池技术替代风险", "海外贸易壁垒", "客户集中度风险(宁德时代议价权强)"]
    },
    "氟化工/电解液": {
        "outlook": "氟化工与电解液跨界融合赛道景气上行。六氟磷酸锂作为电解液核心溶质，2025年市场规模587.77亿元，2026年预计698.57亿元，全球需求40.4万吨对有效产能41.4万吨，紧平衡格局确立。价格从2025年低点4.7万元/吨反弹至2026Q1的18万元/吨(涨幅283%)，全年中枢上移至17-19万元/吨。行业呈'一超多强'格局：天赐材料市占率37%(电解液全产业链一体化)，多氟多市占率20-21%(六氟磷酸锂全球第二+氟化工源头资源)，CR5超85%。电子级氢氟酸(G5级)国产替代加速，已进入台积电、中芯国际、三星供应链。政策端：双碳目标+容量电价机制+半导体国产替代政策三重驱动。技术迭代：LiFSI新型锂盐、大圆柱电池、固态电解质布局推进。氟基新材料(无水氟化铝、冰晶石)作为铝工业核心材料提供稳定现金流。全产业链一体化(萤石→氢氟酸→六氟磷酸锂→锂电池)成为核心竞争力，原料自供率80%+的企业成本优势15-20%。",
        "drivers": ["六氟磷酸锂价格暴涨283%(4.7→18万元/吨)", "全球需求40.4万吨紧平衡", "电子级氢氟酸国产替代(进入台积电)", "大圆柱电池放量(50GWh目标)", "全产业链一体化成本优势", "储能+动力电池双轮驱动"],
        "risks": ["六氟磷酸锂强周期波动风险", "大圆柱电池毛利率偏低(5-8%)", "应收账款高企(占利润794%)", "扩产致资产负债率上升", "固态电池技术替代", "原材料(萤石/碳酸锂)价格波动"]
    },
}

# 板块级数据库(回退使用)
SECTOR_RESEARCH = {
    "AI/半导体": {
        "outlook": "AI算力竞赛持续升级，全球AI专用光模块市场规模将从2025年165亿美元跃升至2026年260亿美元，年增速超57%。英伟达VeraRubin平台密集拉货，AI服务器出货量有望突破200万台，算力硬件订单能见度延伸至2027年。半导体设备国产化率持续提升，先进制程需求推动设备资本开支高增。AI推理需求爆发推动算力架构从通用GPU向专用ASIC转移，博通、Marvell定制化芯片业务强劲增长。",
        "drivers": ["AI算力基建超级周期", "技术迭代(1.6T/3D NAND/先进封装)", "国产替代加速", "英伟达新平台拉货", "AI推理ASIC化"],
        "risks": ["海外技术管制升级", "高端产能扩张周期长", "估值处于历史高位", "下游资本开支波动", "技术路线竞争"]
    },
    "有色资源": {
        "outlook": "2026年有色金属板块迎来供需结构性重构。碳酸锂价格从低点5.88万元/吨反弹至17-20万元/吨，涨幅超240%，供给约束叠加储能/AI数据中心新需求推动价格中枢上移。稀土氧化镨钕价格约70-77.5万元/吨，中重稀土因缅甸停产、越南禁矿供给极度紧缺。钼受益于3D NAND'以钼代钨'技术变革，2026年缺口或达5万吨。铜因AI基建+电网建设需求，2026年全球短缺约12万吨。《矿产资源法实施条例》6月15日施行，稀土、钨、锂、钴等36种矿产列入国家战略性矿产目录。",
        "drivers": ["供给侧政策强约束(配额制+战略矿产目录)", "AI算力金属新需求(锡/钼/锗/钽)", "储能与新能源车需求稳健", "锂矿供给收缩(津巴布韦禁运+宜春停产)", "钼替代钨技术变革"],
        "risks": ["宏观经济衰退风险", "下游需求不及预期", "配额政策放松", "价格高位回调压力", "地缘冲突缓和导致避险溢价消退"]
    },
    "新能源": {
        "outlook": "2026年储能锂电池行业迎来质变拐点，正式取代动力电池成为锂电池产业第一大细分赛道。全球储能Q1出货216GWh，同比增117%。国家容量电价机制2026年1月落地，储能形成容量保底+现货+辅助服务三重盈利模式。500Ah+大电芯全面替代小电芯，循环寿命突破10000次。宁德时代发布天恒钠电储能系统，中国市场9月交付、年底1GWh出货，钠电产业化窗口打开。动力电池旺季需求释放，海外产能布局加速。",
        "drivers": ["容量电价机制落地(商业化闭环)", "储能出货爆发式增长", "钠电产业化拐点", "大电芯技术迭代", "海外超级订单密集落地"],
        "risks": ["碳酸锂价格波动扰动利润", "行业产能过剩压力", "技术路线竞争(钠电/液流/锂电)", "海外贸易壁垒", "储能安全国标提升门槛"]
    },
    "电子材料": {
        "outlook": "AI服务器升级推动PCB从电子级向半导体级跃迁，覆铜板(CCL)、HVLP铜箔、高端电子布、高速树脂四大上游材料同步迭代。建滔积层板年内第5次提价15%，7628电子布价格较年初上涨超70%。全球高端PCB短缺预计持续至2027年底，订单已排至Q4。摩根士丹利预测2025-2028年AI光模块PCB市场从6.2亿美元增至37.7亿美元，CAGR达83%。英伟达VR200单柜PCB价值较GB300提升233%，下一代Kyber再增185%。国产HVLP铜箔、M9树脂迎来从0到1替代窗口。",
        "drivers": ["AI服务器PCB价值量跃升", "上游材料供需极度紧缺", "国产替代窗口打开", "覆铜板持续提价", "高端产能卡位"],
        "risks": ["AI需求不及预期", "海外巨头扩产降价阻击", "玻璃基板/光互连技术替代风险", "客户认证周期长", "原材料价格波动"]
    },
    "电子制造": {
        "outlook": "AI服务器与高速交换机需求爆发推动高端PCB制造景气上行，单台AI服务器PCB价值量约为普通服务器的近10倍。头部厂商AI服务器及800G/1.6T光模块高端PCB订单已排至2026年Q4。2025年9家头部PCB企业资本开支267亿元(同比+111%)，2026Q1达125亿元(同比+182%)，扩产持续加速。高层数、HDI、高频高速板需求结构性增长，行业定价权向具备技术壁垒的头部厂商集中。",
        "drivers": ["AI服务器出货量突破200万台", "高端PCB订单饱满", "资本开支加速扩张", "技术壁垒加深", "量价齐升景气格局"],
        "risks": ["扩产产能消化风险", "中低端竞争加剧", "客户集中度高", "良率爬坡不确定性", "技术迭代风险"]
    },
    "电子面板": {
        "outlook": "面板行业经历周期出清后供需格局改善，大尺寸化与高端化趋势延续。AI显示、车载显示、VR/AR等新应用场景打开增量空间。国内面板厂盈利能力修复，产能利用率维持高位。OLED渗透率持续提升，折叠屏、IT用OLED等新品类贡献增量。",
        "drivers": ["周期出清供需改善", "大尺寸化趋势", "AI/车载新应用", "OLED渗透提升"],
        "risks": ["消费电子需求疲软", "产能过剩压力", "价格战风险", "技术迭代不确定性"]
    },
    "电子元件": {
        "outlook": "AI算力与新能源汽车双轮驱动电子元件需求增长，电感、电容、电阻等被动元件在AI服务器/光模块中的用量大幅提升。国产替代加速，高端MLCC、电感等品类突破海外垄断。汽车电子化、智能化推动车规级元件需求持续扩张。",
        "drivers": ["AI服务器被动元件用量激增", "汽车电子化", "国产替代加速", "5G/物联网需求"],
        "risks": ["消费电子周期波动", "竞争格局分散", "原材料成本波动", "技术认证周期长"]
    },
    "基础材料": {
        "outlook": "玻纤行业受益于高端电子布需求爆发，产品结构向高端化升级。风电、新能源汽车、建筑等传统需求稳健复苏。全球玻纤龙头产品向高端电子布升级，受益AI算力产业链结构性红利。",
        "drivers": ["高端电子布需求爆发", "产品结构升级", "风电/新能源需求", "行业集中度提升"],
        "risks": ["传统需求波动", "产能扩张压力", "原材料成本波动", "海外竞争"]
    },
    "化工": {
        "outlook": "化工行业经历周期底部后景气逐步修复，制冷剂、氟化工等细分赛道受益于供给约束与需求复苏。高端树脂(M7-M9)国产替代逻辑清晰，AI算力推动高速树脂需求增长。氟化工受益于新能源、储能等下游需求扩张。",
        "drivers": ["供给约束+需求复苏", "高端树脂国产替代", "氟化工新能源需求", "制冷剂景气上行"],
        "risks": ["油价波动", "环保政策收紧", "产能过剩", "下游需求不确定性"]
    },
    "医药生物": {
        "outlook": "第11批集采规则系统性优化，从'唯低价论'向'质量与价格并重'转变，提出'稳临床、保质量、反内卷、防围标'原则。创新药出海加速，中国资产在MNC创新药交易总额占比从2020年2.2%升至24.5%。商业健康保险发展指导意见发布，创新药支付端扩容。ADC、自免、代谢等重点领域BD交易活跃，创新药估值体系重塑。集采倒逼仿制药企向创新转型，行业集中度持续提升(CR10从21.4%升至36.8%)。",
        "drivers": ["集采规则缓和(质量导向)", "创新药出海BD交易爆发", "商业保险支付扩容", "ADC/自免前沿领域突破", "行业集中度提升"],
        "risks": ["集采降价压力持续", "研发失败风险", "海外药品关税不确定性", "医保控费压力", "创新药同质化竞争"]
    },
    "综合": {
        "outlook": "资本市场深化改革持续推进，券商受益于成交活跃度提升与业务创新。注册制全面落地、衍生品业务扩容、财富管理转型加速。市场交投活跃带动券商经纪、两融、自营业务收入增长。",
        "drivers": ["市场成交活跃", "财富管理转型", "业务创新扩容", "资本市场改革"],
        "risks": ["市场波动风险", "政策不确定性", "行业竞争加剧", "自营投资风险"]
    }
}

# ========== 公司专属财务数据库(基于最新财报与公开信息) ==========
# 用于增强核心业务竞争力评估的专业性与数据支撑
COMPANY_PROFILE = {
    # ==================== 电解液/氟化工 ====================
    "天赐材料": {
        "positioning": "全球电解液绝对龙头，连续10年出货量全球第一",
        "market_share": "全球电解液市占率32.2%-38.2%，六氟磷酸锂全球市占40%+",
        "financials": "2026Q1营收66.73亿元(+91.29%)，归母净利16.54亿元(+1005.75%)，毛利率38.65%，净利率24.68%；2025全年营收166.50亿元(+33%)，归母净利13.62亿元(+181.43%)",
        "core_clients": "宁德时代、比亚迪、特斯拉、LG新能源、松下、三星SDI(全球前10大电池厂9家合作)",
        "competitive_edge": "全产业链一体化(锂矿→碳酸锂→锂盐/添加剂→电解液→回收)，原材料自给率约70%，六氟磷酸锂自供率95%-99%，单吨成本低于行业15-20%；液态六氟工艺全球唯一大规模工业化；LiFSI产能2万吨行业领先；固态电解质(硫化物路线)2026年中试线落地",
        "capacity": "电解液产能86万吨(全球第一)，六氟磷酸锂产能11万吨，海外产能20万吨(美国/德国/摩洛哥/匈牙利)",
        "orders": "手握超340万吨下游长协订单，2026年3月递交H股招股书募资投向全球化",
        "patents": "累计专利1200+项"
    },
    "多氟多": {
        "positioning": "全球氟化工龙头+六氟磷酸锂双寡头+半导体电子化学品核心标的",
        "market_share": "六氟磷酸锂全球第二(市占率20-21%)，电子级六氟磷酸锂细分市场全球份额超60%，无水氟化铝全球第一(市占30%+)",
        "financials": "2026Q1营收32.16亿元(+53.26%)，归母净利3.76亿元(+480.14%)，毛利率27.61%；2025全年营收94.34亿元(+14.37%)，归母净利2.13亿元(扭亏+168.97%)",
        "core_clients": "比亚迪(5.6万吨长单)、宁德时代、LG新能源、松下；电子级氢氟酸进入台积电、中芯国际、三星、SK海力士供应链",
        "competitive_edge": "全产业链一体化(萤石→氢氟酸→六氟磷酸锂→锂电池)，原料自供率80%+，单吨六氟成本4.5万元(行业均值6.5万+)，成本优势15-20%；独创'氟硅分家'技术(磷肥副产氟硅酸制氢氟酸)；电子级氢氟酸UPSSS级(G5级)打破日企垄断",
        "capacity": "六氟磷酸锂产能6.5万吨/年(满负荷运行)，大圆柱电池2025年出货近10GWh、2026年目标50GWh，电子级氢氟酸产能4万吨/年(半导体级2万吨)",
        "business_structure": "新能源材料33.21%+新能源电池30.21%+氟基新材料29.56%+电子信息材料4.33%，四轮驱动分散周期风险",
        "patents": "累计专利1200+项，牵头制定100余项行业标准"
    },
    # ==================== 光模块 ====================
    "新易盛": {
        "positioning": "全球AI光模块龙头，LPO技术全球领导者，800G/1.6T双路线并行",
        "market_share": "800G光模块全球市占率25%-30%，1.6T市占率15%-20%，800G LPO技术市占率75%+，Meta独家供应商",
        "financials": "2026Q1营收83.38亿元(+105.76%)，归母净利27.80亿元(+76.80%)，毛利率49.16%，净利率33.27%；2025全年营收248.42亿元(+187.29%)，归母净利95.32亿元(+235.89%)",
        "core_clients": "Meta(独家LPO供应)、英伟达(GB200核心1.6T供应)、亚马逊AWS、微软Azure、谷歌(前五大客户占比72%，北美收入占比超94%)",
        "competitive_edge": "LPO技术功耗降30%、成本降20%，全球市占75%+；收购Alpine获50+硅光专利，硅光良率95%，单位成本降20%；1.6T LPO/CPO双路线量产，3.2T NPO已送样；累计专利1400+项",
        "capacity": "2026年总产能800万只(800G/1.6T产能600万只)，泰国工厂新增200万只1.6T LPO专用产线Q2投产",
        "orders": "订单排至2027年，英伟达2026年1.6T订单超50万只，客户认证壁垒2-3年替换周期"
    },
    "中际旭创": {
        "positioning": "全球光模块绝对龙头，800G/1.6T市占率断层第一，AI算力'光王'",
        "market_share": "800G光模块全球市占率55%+，1.6T光模块市占率50%-70%，英伟达1.6T采购70%-80%份额独家承接",
        "financials": "2026Q1营收194.96亿元(+192.12%)，归母净利57.35亿元(+262.28%)，毛利率46.06%(同比+9.36pct)，净利率32.40%；2025全年营收382.40亿元(+60.25%)，归母净利107.97亿元(+108.78%)",
        "core_clients": "英伟达(独家1.6T供应)、谷歌、微软、Meta、亚马逊(境外收入占比90.58%)，华为昇腾(占比15%+)",
        "competitive_edge": "800G/1.6T全球断层领先；硅光方案良率92%+；12.8T XPO光模块全球首发(集成8倍1.6T容量)；CPO全光交换机已小批量量产；深度绑定英伟达+华为昇腾双生态",
        "capacity": "1.6T CPO国内首家小规模量产，泰国工厂扩产中，预付款14.88亿元(较年初增10倍)锁定上游物料",
        "orders": "英伟达2026年光模块采购上调至2000万只，订单排产至2027年，部分客户2028年需求已沟通"
    },
    "天孚通信": {
        "positioning": "全球光器件龙头，光模块上游核心配件'卖水人'",
        "market_share": "光隔离器全球市占率60%+，陶瓷套管全球第一，光引擎市占率快速提升",
        "financials": "2026Q1营收约15亿元(+80%+)，归母净利约5亿元(+90%+)，毛利率55%+；2025全年营收50亿元(+70%)，归母净利18亿元(+85%)",
        "core_clients": "中际旭创、新易盛、华为、思科、Coherent等全球前十大光模块厂",
        "competitive_edge": "光器件垂直一体化(陶瓷→封装→隔离器→光引擎)，光引擎集成度行业领先；高可靠性无源器件良率99%+；向有源光引擎延伸提升单机价值量10倍",
        "capacity": "光器件产能扩产3倍，光引擎产能新建以满足800G/1.6T需求"
    },
    "长芯博创": {
        "positioning": "国内AOC光缆组件龙头，数据中心光互联核心供应商",
        "market_share": "AOC光缆组件国内市占率30%+，25G/100G/400G AOC出货量国内前列",
        "financials": "2026Q1营收约20亿元(+120%+)，归母净利约4亿元(+150%+)，毛利率30%+；2025全年营收60亿元(+80%)",
        "core_clients": "阿里、腾讯、字节、百度、谷歌、亚马逊等国内外云厂商",
        "competitive_edge": "AOC全系列覆盖(25G-800G)，自研光模块封装技术；800G AOC批量出货，1.6T AOC研发领先；数据中心直连方案性价比优势显著"
    },
    "源杰科技": {
        "positioning": "国内光芯片龙头，DFB/VCSEL/EML全系列自主可控",
        "market_share": "25G DFB激光器国产化率第一，50G/100G EML打破海外垄断",
        "financials": "2026Q1营收约5亿元(+150%+)，归母净利约2亿元(+200%+)，毛利率50%+；2025全年营收15亿元(+120%)",
        "core_clients": "中际旭创、新易盛、光迅科技、华为等光模块厂商",
        "competitive_edge": "国内唯一量产100G EML激光器的企业；DFB激光器良率行业领先；硅光芯片联合研发；晶圆级自主可控产线"
    },
    "仕佳光子": {
        "positioning": "国内PLC光分路器+硅光芯片龙头，光通信无源器件核心供应商",
        "market_share": "PLC光分路器全球市占率30%+，AWG芯片国产替代先锋",
        "financials": "2026Q1营收约4亿元(+100%+)，归母净利约1亿元(+300%+)，毛利率35%+；2025全年营收12亿元(+90%)",
        "core_clients": "中际旭创、华为、中兴、烽火通信等",
        "competitive_edge": "PLC芯片自主晶圆产线；AWG/MZM硅光芯片研发领先；DFB激光器外延片自主可控；晶圆级测试能力"
    },
    "长飞光纤": {
        "positioning": "全球光纤光缆龙头，预制棒-光纤-光缆全产业链一体化",
        "market_share": "光纤光缆全球市占率15%+(第一)，预制棒全球第一",
        "financials": "2026Q1营收约45亿元(+25%+)，归母净利约3亿元(+40%+)，毛利率20%+；2025全年营收160亿元(+15%)",
        "core_clients": "中国移动、电信、联通(三大运营商占比70%+)，海外谷歌、Meta等",
        "competitive_edge": "PCVD/VAD/OVD全工艺预制棒技术；G.654.E超低衰减光纤量产；多模光纤OM4/OM5市占率国内第一；海洋通信网络突破"
    },
    # ==================== AI服务器/算力 ====================
    "工业富联": {
        "positioning": "全球AI服务器ODM绝对龙头，英伟达GB200/GB300核心代工",
        "market_share": "AI服务器全球市占率40%-42%(断层领先，第二名约15%)，800G交换机全球市占率73.8%",
        "financials": "2026Q1营收2510.78亿元(+56.52%)，归母净利105.95亿元(+102.55%)，连续三季破百亿，毛利率7.35%(+0.62pct)，净利率4.22%(+0.95pct)；2025全年营收9018亿元(+35%)，云计算营收6027亿(+88.7%)",
        "core_clients": "英伟达(GB200/GB300独家JDM伙伴)、微软、谷歌、Meta、AWS(全球前五云厂商四家合作)",
        "competitive_edge": "JDM联合设计制造(芯片模组→主板→整机→液冷机柜→交换机一站式)；AI GPU机柜出货同比+3.8倍，AI ASIC服务器+3.2倍；800G交换机+1.6倍，1.6T CPO已小批量出货；液冷PUE低至1.05",
        "capacity": "全球产能布局(中国+墨西哥+越南)，墨西哥承接40%北美订单规避关税；天津先进封装产线2026投产(年产能5万片GPU当量)",
        "orders": "英伟达未来五季度新增1400万颗GPU订单，订单锁定至2027年；液冷2026年营收目标800-1000亿元"
    },
    "浪潮信息": {
        "positioning": "国内AI服务器绝对龙头，全球AI服务器第二",
        "market_share": "国内AI服务器市占率52%+，通用X86服务器连续19年国内第一，液冷服务器国内市占35%+",
        "financials": "2026Q1营收354.70亿元(-24.30%，主动砍低毛利订单)，归母净利6.05亿元(+30.74%)，毛利率6.64%(+3.19pct)；2025全年营收1647.82亿元(+43.25%)，归母净利24.13亿元(+5.20%)",
        "core_clients": "字节、阿里、腾讯、百度(前五大客户占比71.29%)，英伟达/昇腾/海光/AMD全芯片生态适配",
        "competitive_edge": "国内唯一全芯片生态适配厂商；液冷国标牵头制定者，冷板+浸没全栈产品；元脑超节点适配大模型推理；双供应链(海外英伟达+国产昇腾/海光)对冲芯片风险",
        "capacity": "合同负债195亿元(+72%)锁定后续营收，高端AI服务器满产满销",
        "orders": "在手订单超350亿元，高端H100/H200服务器排期至2027年Q2"
    },
    "中科曙光": {
        "positioning": "国产算力全产业链龙头，信创AI服务器核心标的",
        "market_share": "信创服务器市占率20%+，浸没式液冷技术全球领先",
        "financials": "2026Q1营收30.72亿元(+18.80%)，归母净利2.25亿元(+20.88%)，扣非净利1.69亿元(+57.77%)",
        "core_clients": "国家级智算中心、东数西算工程、政企金融客户",
        "competitive_edge": "芯片(海光)→服务器→数据中心→行业应用全产业链；浸没式液冷PUE降至1.1以下，单机柜功率密度750kW；参股海光信息形成生态协同；支撑全国80%+智算中心建设",
        "capacity": "液冷产能持续扩张，国产化AI服务器产线满载"
    },
    # ==================== PCB ====================
    "胜宏科技": {
        "positioning": "全球AI算力PCB绝对龙头，英伟达GB300核心供应商",
        "market_share": "AI服务器PCB全球市占率13.8%(第一)，英伟达Rubin平台份额50%-55%，1.6T光模块PCB市占率50%+",
        "financials": "2026Q1营收63.8亿元(+58.3%)，归母净利21.6亿元(+168.5%)，毛利率36.8%(+4.2pct)；2025全年营收192.92亿元(+79.77%)，归母净利43.12亿元(+273.52%)，AI业务占比43%",
        "core_clients": "英伟达(GB300 OAM模块PCB全球独家供应，份额60%+)、谷歌(TPU V7)、微软、Meta、中际旭创",
        "competitive_edge": "全球唯二(与沪电)具备10阶30层HDI量产能力，良率90%+(行业70%)；M9级超低损耗材料224Gbps传输；GB300 OAM模块PCB全球独家认证；认证周期1-2年，客户替换成本极高",
        "capacity": "2026Q1产能120万平方米/月(Q3扩至200万)，产能利用率95%+；泰国+国内双基地规避关税；建设周期5-6个月(行业12个月)",
        "orders": "2026年产能排满，2027年订单充足；英伟达收入占AI业务70%，新增3家北美ASIC客户"
    },
    "沪电股份": {
        "positioning": "全球高端PCB龙头，22层以上PCB全球市占率第一",
        "market_share": "22层以上PCB全球市占率25.3%(第一)，数据中心PCB收入全球第一，800G交换机PCB市占率45%",
        "financials": "2026Q1营收62.14亿元(+53.91%)，归母净利12.42亿元(+62.90%)，毛利率35.63%(历史新高)；2025全年营收189.45亿元(+42%)，归母净利38.22亿元(+48%)",
        "core_clients": "英伟达(78层M9级正交背板全球独家认证，Rubin平台份额40%+)、谷歌、微软、华为、比亚迪",
        "competitive_edge": "78层正交背板全球独家通过英伟达认证；22层以上高端PCB占比59.4%；高速网络交换机占比51%；境外收入占比80%+；14年未融一分钱，累计分红超41亿",
        "capacity": "昆山/黄石/泰国/金坛四大基地产能利用率均超99%；150亿元扩产计划全部指向高端AI服务器PCB",
        "orders": "在手订单超120亿元，覆盖2026年全年产能115%+"
    },
    "深南电路": {
        "positioning": "国内PCB+封装基板双龙头，IC载板国产替代核心",
        "market_share": "PCB国内前三，IC载板国内第一，RFPCB军工领域龙头",
        "financials": "2026Q1营收约55亿元(+40%+)，归母净利约8亿元(+60%+)，毛利率28%+；2025全年营收180亿元(+35%)",
        "core_clients": "华为、中兴、三星、AMD、长电科技、通富微电",
        "competitive_edge": "PCB+封装基板+电子装联三位一体；IC载板(FCBGA/BGA)国产替代先锋；RF微波PCB军工壁垒；高端HDI量产能力",
        "capacity": "无锡IC载板基地扩产，泰国工厂建设推进"
    },
    "生益电子": {
        "positioning": "高端PCB核心供应商，AI服务器/交换机PCB快速增长",
        "market_share": "5G通信PCB国内前列，AI服务器PCB加速放量",
        "financials": "2026Q1营收约20亿元(+80%+)，归母净利约2亿元(扭亏)，毛利率25%+；2025全年营收60亿元(+50%)",
        "core_clients": "华为、中兴、三星、英伟达(认证中)",
        "competitive_edge": "母公司生益科技覆铜板协同优势；高多层PCB量产能力；AI服务器PCB认证突破"
    },
    "生益科技": {
        "positioning": "全球覆铜板龙头，高端CCL国产替代核心",
        "market_share": "覆铜板全球市占率12%+(第二)，高频高速CCL国内第一",
        "financials": "2026Q1营收约55亿元(+30%+)，归母净利约6亿元(+50%+)，毛利率20%+；2025全年营收200亿元(+20%)",
        "core_clients": "华为、中兴、深南电路、胜宏科技、沪电股份",
        "competitive_edge": "M7/M8级高速CCL打破日企垄断；高频CCL(PTFE/碳氢)5G基站核心供应；覆铜板-PCB垂直一体化",
        "capacity": "覆铜板产能1.2亿张/年，泰国基地建设推进"
    },
    "鹏鼎控股": {
        "positioning": "全球FPC/PCB龙头，苹果FPC核心供应商",
        "market_share": "FPC全球市占率25%+(第一)，苹果FPC份额30%+",
        "financials": "2026Q1营收约70亿元(+25%+)，归母净利约8亿元(+40%+)，毛利率22%+；2025全年营收350亿元(+15%)",
        "core_clients": "苹果(FPC核心供应，占比70%+)、谷歌、亚马逊",
        "competitive_edge": "FPC全球龙头，SLP(类载板)量产能力；苹果Vision Pro FPC独家供应；汽车FPC快速放量"
    },
    "东山精密": {
        "positioning": "全球PCB+精密制造双龙头，苹果+FPC核心供应商",
        "market_share": "FPC全球前三，PCB全球前十，苹果FPC份额20%+",
        "financials": "2026Q1营收约100亿元(+30%+)，归母净利约10亿元(+120%+)，毛利率18%+；2025全年营收350亿元(+20%)",
        "core_clients": "苹果、特斯拉、谷歌、微软",
        "competitive_edge": "FPC+PCB+精密结构件一体化；苹果核心FPC供应商；特斯拉车载PCB供应；1.6T光模块PCB放量"
    },
    # ==================== 半导体设备 ====================
    "北方华创": {
        "positioning": "国内半导体设备平台化龙头，刻蚀+薄膜沉积双百亿平台",
        "market_share": "国内刻蚀设备市占率20%+，薄膜沉积国内第一，平台化覆盖度国内最全",
        "financials": "2026Q1营收103.23亿元(+25.80%)，归母净利16.35亿元(+3.42%)，毛利率40.77%(环比+3.62pct)；研发费用14.02亿元(+36.64%)",
        "core_clients": "中芯国际、长江存储、合肥长鑫、华虹半导体、台积电(部分)",
        "competitive_edge": "刻蚀(ICP/CCP)+薄膜(PVD/CVD/ALD/外延)+热处理+湿法清洗+涂胶显影+离子注入+键合七大平台；2025年刻蚀/薄膜收入均超百亿；中国大陆存储+先进逻辑+先进封装扩产核心受益",
        "capacity": "截至2026Q1末存货286亿元(+13.45%)，合同负债42亿元，新接订单有望快速增长",
        "orders": "平台化布局深度受益大陆晶圆制造大扩产，2026新接订单同比快速增长"
    },
    "拓荆科技": {
        "positioning": "国内CVD/ALD薄膜沉积设备龙头，半导体设备国产替代核心",
        "market_share": "国内CVD设备市占率15%+，ALD设备国产替代先锋",
        "financials": "2026Q1营收约25亿元(+50%+)，归母净利约3亿元(+80%+)，毛利率40%+；2025全年营收80亿元(+45%)",
        "core_clients": "中芯国际、长江存储、合肥长鑫、华虹",
        "competitive_edge": "PECVD/SACVD/ALD全系列薄膜沉积；混合键合设备突破；先进逻辑/存储CVD国产替代核心"
    },
    "长川科技": {
        "positioning": "国内半导体测试设备龙头，测试机+分选机国产替代",
        "market_share": "国内模拟测试机市占率30%+，SoC测试机国产替代突破",
        "financials": "2026Q1营收约15亿元(+60%+)，归母净利约2亿元(+100%+)，毛利率45%+；2025全年营收45亿元(+50%)",
        "core_clients": "长电科技、通富微电、华天科技、中芯国际",
        "competitive_edge": "模拟/数模混合测试机国产领先；SoC测试机突破8nm制程；分选机自动化率提升；AI芯片测试需求拉动"
    },
    # ==================== 存储 ====================
    "佰维存储": {
        "positioning": "AI端侧存储龙头，晶圆级先进封测稀缺标的",
        "market_share": "ePOP嵌入式存储国内第一，AI眼镜存储全球核心供应",
        "financials": "2026Q1营收68.14亿元(+341.53%)，归母净利28.99亿元(扭亏，去年同期-1.97亿)，毛利率53.3%(去年同期1.99%)，净利率42.22%；2025全年营收113.02亿元，归母净利8.53亿元",
        "core_clients": "Meta(Ray-Ban AI眼镜独家)、谷歌、小米、荣耀、小鹏、理想、华为",
        "competitive_edge": "自研主控芯片+固件算法+先进封测(ePOP叠层/多层叠Die/晶圆级WLCSP)一体化；全球唯一具备晶圆级微型封装能力的独立存储厂商；ePOP产品低功耗/快响应/轻薄，AI端侧适配性极强",
        "capacity": "惠州封测厂年产能70KK，松山湖晶圆级WLCSP先进封测产线；低价库存约220亿持续兑现利润",
        "orders": "AI端侧存储Q1收入11.75亿元(+496.45%)，Meta AI眼镜放量驱动持续增长"
    },
    "兆易创新": {
        "positioning": "国内NOR Flash+MCU双龙头，存储+控制芯片平台型公司",
        "market_share": "NOR Flash全球市占率18%(前三)，MCU国内第一",
        "financials": "2026Q1营收约20亿元(+45%+)，归母净利约3亿元(+200%+)，毛利率40%+；2025全年营收70亿元(+30%)",
        "core_clients": "苹果、华为、小米、比亚迪、三星",
        "competitive_edge": "NOR Flash全球第三，55nm/45nm制程领先；MCU国内第一(GD32系列)；DRAM自研突破；利基存储涨价弹性大"
    },
    "江波龙": {
        "positioning": "国内存储模组+嵌入式存储龙头，企业级SSD突破",
        "market_share": "消费级存储模组国内前三，企业级SSD快速放量",
        "financials": "2026Q1营收约45亿元(+80%+)，归母净利约3亿元(扭亏)，毛利率20%+；2025全年营收150亿元(+40%)",
        "core_clients": "联想、华为、小米、阿里云、腾讯云",
        "competitive_edge": "消费+企业级存储模组双轮驱动；自研主控芯片；企业级SSD通过头部云厂商认证；存储涨价周期弹性大"
    },
    "协创数据": {
        "positioning": "数据存储+云服务物联网龙头，AI边缘计算核心",
        "market_share": "智能存储设备出口前列，云视频物联设备国内领先",
        "financials": "2026Q1营收约25亿元(+60%+)，归母净利约2.5亿元(+80%+)，毛利率22%+；2025全年营收80亿元(+35%)",
        "core_clients": "亚马逊、谷歌、小米、中国移动",
        "competitive_edge": "存储+云服务+物联网三业务协同；SSD/HDD存储设备ODM能力；AI边缘计算设备放量；海外云厂商合作深化"
    },
    "德明利": {
        "positioning": "存储主控芯片+模组核心标的，SSD主控国产替代",
        "market_share": "SSD主控芯片国内前列，存储模组快速放量",
        "financials": "2026Q1营收约15亿元(+200%+)，归母净利约1.5亿元(扭亏)，毛利率25%+；2025全年营收40亿元(+150%)",
        "core_clients": "联想、华为、小米、企业级客户",
        "competitive_edge": "自研SSD主控芯片；PCIe 5.0主控研发领先；存储涨价周期弹性大；企业级SSD认证突破"
    },
    # ==================== 内存接口 ====================
    "澜起科技": {
        "positioning": "全球内存接口芯片双寡头之一，DDR5 RCD核心受益",
        "market_share": "DDR4 RCD全球市占率45%(与瑞萨并列)，DDR5 RCD全球第一",
        "financials": "2026Q1营收约15亿元(+80%+)，归母净利约6亿元(+150%+)，毛利率60%+；2025全年营收45亿元(+60%)",
        "core_clients": "三星、SK海力士、美光(全球三大DRAM原厂)",
        "competitive_edge": "DDR5 RCD/MRCD/DB全球寡头；PCIe Retimer芯片国产唯一量产；CXL内存扩展芯片布局领先；AI服务器DDR5渗透率提升核心受益"
    },
    # ==================== 锂电池 ====================
    "宁德时代": {
        "positioning": "全球锂电池绝对龙头，动力+储能双轮驱动",
        "market_share": "全球动力电池市占率38.2%(连续9年第一)，国内50.1%，储能电池全球30.4%(连续5年第一)",
        "financials": "2026Q1营收1291.31亿元(+52.45%)，归母净利207.38亿元(+48.52%)，毛利率24.82%；总资产破万亿(10463亿)，货币资金3520亿；2025全年营收4237亿元(+17%)，归母净利722亿元(+42%)",
        "core_clients": "特斯拉(年供40GWh，最大客户)、宝马(60亿欧元长单)、大众、奔驰(10年协议)、福特、理想/蔚来/小米(高端车型占比90%+)",
        "competitive_edge": "全产业链垂直整合(矿山→冶炼→材料→电芯→系统→回收)，单位成本低15%；神行/麒麟/凝聚态/钠离子全技术路线覆盖；产能利用率96.9%；生产良率99.9%；海外毛利率31.4%高于国内",
        "capacity": "2026年产能772GWh，在建321GWh，2027年目标1200GWh；全球13个生产基地(中国/德国/匈牙利/印尼)；匈牙利工厂一期24GWh 2026年10月投产",
        "orders": "在手订单2.8万亿元排期至2028年底，海外订单占比45%，储能订单占比25%"
    },
    "亿纬锂能": {
        "positioning": "国内锂电池第二梯队龙头，消费+动力+储能全场景覆盖",
        "market_share": "全球动力电池市占率3%+，储能电池全球前列，消费电池(豆式)全球第一",
        "financials": "2026Q1营收约150亿元(+40%+)，归母净利约15亿元(+60%+)，毛利率20%+；2025全年营收500亿元(+25%)",
        "core_clients": "宝马、奔驰、小鹏、广汽、亚马逊、三星",
        "competitive_edge": "消费+动力+储能全场景布局；大圆柱电池量产领先；固态电池研发推进；海外产能(匈牙利/马来西亚)建设"
    },
    # ==================== 有色资源 ====================
    "赣锋锂业": {
        "positioning": "全球锂业全产业链龙头，锂盐产能全球第一",
        "market_share": "锂盐产能26万吨LCE(全球第一)，权益资源量3072万吨LCE(全球第一)",
        "financials": "2026Q1营收91.96亿元(+143.81%)，归母净利18.37亿元(+616.34%)；2025全年营收230.8亿元，归母净利16.1亿元",
        "core_clients": "宁德时代、比亚迪、LG新能源、特斯拉、宝马",
        "competitive_edge": "锂矿-盐湖-锂盐-电池-回收全产业链闭环；权益资源量全球第一(澳矿/阿根廷盐湖/马里锂矿)；固态电池研发领先；锂回收产能5万吨；资源自给率约70%",
        "capacity": "锂盐产能26万吨LCE，阿根廷盐湖扩产+马里锂矿2027年投产，2026-2028年产能年增20%+"
    },
    "天齐锂业": {
        "positioning": "全球锂资源成本壁垒最高标的，业绩弹性之王",
        "market_share": "锂盐产能14.7万吨LCE，控股澳洲格林布什矿(全球最大高品位硬岩锂矿)",
        "financials": "2026Q1营收51.28亿元(+98.44%)，归母净利18.76亿元(+1699.12%)；2025全年营收328.6亿元，归母净利42.1亿元",
        "core_clients": "宁德时代、比亚迪、LG新能源、松下",
        "competitive_edge": "格林布什矿品位1.6%-2.0%全球最高，现金成本仅3万元/吨LCE(锂价10万仍盈利)；资源自给率约100%；持有SQM股权贡献稳定投资收益；负债率仅28%财务健康",
        "capacity": "锂盐产能14.7万吨，澳洲奎纳纳氢氧化锂项目投产，高毛利产品占比提升"
    },
    "紫金矿业": {
        "positioning": "全球矿业跨国龙头，铜金资源双龙头",
        "market_share": "全球铜矿市占率8%+，全球金矿市占率5%+，锂资源快速布局",
        "financials": "2026Q1营收约700亿元(+20%+)，归母净利约100亿元(+30%+)，毛利率18%+；2025全年营收3000亿元(+20%)",
        "core_clients": "全球大宗商品市场(铜/金/锌/锂)",
        "competitive_edge": "全球矿产资源布局(塞尔维亚/刚果金/西藏/阿根廷)；铜矿成本行业最低；黄金产量全球前列；锂盐湖(阿根廷3Q)+锂矿(西藏)双布局；逆周期并购能力卓越",
        "capacity": "2026年铜产量110万吨，金产量70吨，锂碳酸锂当量5万吨"
    },
    "洛阳钼业": {
        "positioning": "全球铜钴矿跨国龙头，刚果金TFM+KFM双矿核心",
        "market_share": "全球钴市占率20%+，铜矿快速放量",
        "financials": "2026Q1营收约200亿元(+15%+)，归母净利约40亿元(+100%+)，毛利率35%+；2025全年营收700亿元(+10%)",
        "core_clients": "全球电池厂(钴)、全球铜冶炼厂",
        "competitive_edge": "刚果金TFM+KFM双矿成本极低；铜钴伴生协同效应；钴价上涨弹性巨大；挪威镍业务并表",
        "capacity": "2026年铜产量60万吨，钴产量6万吨，镍产量8万吨"
    },
    "华友钴业": {
        "positioning": "全球钴镍前驱体一体化龙头，新能源材料平台型公司",
        "market_share": "全球钴市占率15%+，前驱体国内前三",
        "financials": "2026Q1营收约200亿元(+30%+)，归母净利约15亿元(+100%+)，毛利率15%+；2025全年营收700亿元(+20%)",
        "core_clients": "宁德时代、LG新能源、特斯拉、三星SDI",
        "competitive_edge": "钴矿-冶炼-前驱体-正极-回收全产业链；刚果金钴矿成本优势；印尼镍湿法项目布局；前驱体高镍化技术领先"
    },
    "北方稀土": {
        "positioning": "全球稀土龙头，轻稀土资源+冶炼分离一体化",
        "market_share": "全球轻稀土市占率30%+，国内稀土配额第一",
        "financials": "2026Q1营收约60亿元(+20%+)，归母净利9.18亿元(+113.12%)，扣非净利8.83亿元(+103.21%)；2025全年营收200亿元",
        "core_clients": "宁波韵升、中科三环、正海磁材(稀土永磁厂)",
        "competitive_edge": "白云鄂博矿独家开采权；轻稀土储量全球第一；冶炼分离产能国内最大；稀土价格上行核心受益"
    },
    "盛和资源": {
        "positioning": "全球稀土全产业链布局，海外资源整合先锋",
        "market_share": "国内稀土分离产能前列，海外稀土矿(格陵兰/美国MP)核心布局",
        "financials": "2026Q1营收约40亿元(+30%+)，归母净利约3亿元(+150%+)；2025全年营收120亿元",
        "core_clients": "国内永磁材料厂、海外磁材企业",
        "competitive_edge": "海外稀土矿(美国Mountain Pass+格陵兰Kvanefjeld)独家包销；稀土分离+冶炼+磁材全产业链；稀土涨价弹性大"
    },
    "厦门钨业": {
        "positioning": "全球钨业龙头+稀土永磁+锂电材料三大业务",
        "market_share": "钨钼全球前三，稀土永磁国内前列，锂电正极材料国内前十",
        "financials": "2026Q1营收约120亿元(+25%+)，归母净利约6亿元(+50%+)，毛利率15%+；2025全年营收400亿元(+20%)",
        "core_clients": "宁德时代、比亚迪、苹果(钨丝)、博世",
        "competitive_edge": "钨钼-稀土-锂电材料三大业务协同；光伏钨丝替代高碳钢丝趋势受益；稀土永磁加速扩产；锂电正极高镍化"
    },
    # ==================== 新能源/逆变器 ====================
    "阳光电源": {
        "positioning": "全球光储双龙头，逆变器全球第一+储能系统全球第二",
        "market_share": "光伏逆变器全球市占率23%-25%(连续多年第一)，储能系统全球第二(市占率9%)",
        "financials": "2026Q1营收155.61亿元(-18.26%，去年沙特高基数)，归母净利22.91亿元(-40.12%，汇兑损失4亿)，毛利率33.3%(环比+10.3pct)；储能毛利率30%(环比+6pct)，逆变器毛利率40%(+2-3pct)",
        "core_clients": "马斯达尔(7.5GWh全球最大储能单)、沙特RTC、全球180+国家客户",
        "competitive_edge": "光储双龙头+构网型储能技术(深耕20年，200+核心专利，50+行业标准)；BNEF可融资性评级100%满分；电芯中立策略+规模议价；AIDC电源(SST样机下半年发布)打开新增长极",
        "capacity": "合同负债122亿创历史新高，预付款40亿(+201%)储备交付；2026储能出货63.4GWh(+47%)",
        "orders": "全球最大7.5GWh储能订单(马斯达尔)，2.6GW光伏逆变器配套"
    },
    # ==================== 光通信/海缆 ====================
    "中天科技": {
        "positioning": "国内光通信+海缆双龙头，海洋经济核心标的",
        "market_share": "海缆国内市占率30%+(第一)，光纤光缆国内前三",
        "financials": "2026Q1营收约100亿元(+15%+)，归母净利约10亿元(+25%+)，毛利率18%+；2025全年营收450亿元(+10%)",
        "core_clients": "三大运营商、国家电网、海上风电开发商",
        "competitive_edge": "海缆-海洋观测-海上风电全产业链；±525kV直流海缆技术领先；光纤预制棒自主可控；特种光缆细分龙头"
    },
    "亨通光电": {
        "positioning": "全球光纤光缆+海洋通信龙头",
        "market_share": "光纤光缆全球前列，海底光缆国内第一",
        "financials": "2026Q1营收约60亿元(+20%+)，归母净利约7亿元(+40%+)，毛利率18%+；2025全年营收250亿元(+15%)",
        "core_clients": "三大运营商、谷歌、Meta、海上风电开发商",
        "competitive_edge": "海底光缆中继器国产化突破；跨洋海缆系统总包能力；光纤预制棒全合成工艺；硅光芯片研发"
    },
    "烽火通信": {
        "positioning": "国内光通信设备龙头，光纤+传输设备一体化",
        "market_share": "光传输设备国内前三，光纤光缆国内前列",
        "financials": "2026Q1营收约50亿元(+10%+)，归母净利约2亿元(+30%+)，毛利率22%+；2025全年营收200亿元(+8%)",
        "core_clients": "三大运营商、政企客户、海外电信运营商",
        "competitive_edge": "光传输(OTN)设备技术领先；400G/800G光传输设备量产；光纤光缆+设备一体化；中国信科集团旗下"
    },
    "中兴通讯": {
        "positioning": "全球5G/算力设备龙头，5G基站+服务器双轮驱动",
        "market_share": "5G基站全球市占率15%+(前三)，服务器国内前列",
        "financials": "2026Q1营收约300亿元(+15%+)，归母净利约25亿元(+20%+)，毛利率40%+；2025全年营收1200亿元(+10%)",
        "core_clients": "三大运营商、政企客户、海外电信运营商(全球100+国家)",
        "competitive_edge": "5G基站+核心网+承载网全栈；AI服务器(昇腾生态)加速放量；芯片自研(中兴微电子)；5G-A技术领先"
    },
    # ==================== 消费电子/代工 ====================
    "立讯精密": {
        "positioning": "全球精密制造平台龙头，苹果核心Tier1+AI算力+汽车电子",
        "market_share": "苹果Tier1核心供应商(AirPods/Vision Pro/iPhone组装)，消费电子代工全球前列",
        "financials": "2026Q1营收838.88亿元(+35.77%)，归母净利36.60亿元(+20.24%)，毛利率11.92%(+0.74pct)，净利率4.73%；2025全年营收3324亿元(+23.64%)，苹果占比降至56.68%",
        "core_clients": "苹果(占比56.68%，Vision Pro独家组装)、英伟达(AI连接器/光模块)、特斯拉(线束)、宝马",
        "competitive_edge": "消费电子(78%)+汽车电子(11.81%，+185%)+AI算力通信(7.39%，+34%)三驾马车；收购德国莱尼线束并表首年扭亏；AI算力订单超400亿，通信毛利率18%；制造良率/自动化行业领先",
        "capacity": "全球多基地布局，海外建厂配套北美车企；H股IPO推进中",
        "orders": "2025年算力相关订单超400亿，汽车电子收入392.55亿(+185.34%)，OpenAI手机独家合作"
    },
    # ==================== 材料类 ====================
    "菲利华": {
        "positioning": "国内石英玻璃龙头，半导体+光通信双驱动",
        "market_share": "国内半导体石英玻璃市占率60%+，光通信石英套管全球前列",
        "financials": "2026Q1营收约12亿元(+30%+)，归母净利约3亿元(+40%+)，毛利率45%+；2025全年营收45亿元(+25%)",
        "core_clients": "中芯国际、台积电、ASML(认证中)、光迅科技",
        "competitive_edge": "石英玻璃国内唯一通过中芯国际认证；合成石英技术突破；光通信石英套管全球前三；航空航天石英纤维"
    },
    "中国巨石": {
        "positioning": "全球玻璃纤维龙头，产能规模世界第一",
        "market_share": "全球玻纤市占率25%+(第一)，高端玻纤(风电/电子)市占率更高",
        "financials": "2026Q1营收约45亿元(+15%+)，归母净利约8亿元(+20%+)，毛利率35%+；2025全年营收180亿元(+10%)",
        "core_clients": "全球风电叶片厂、PCB覆铜板厂、汽车复合材料厂",
        "competitive_edge": "玻纤产能全球第一(超250万吨)；E6/E7/E8高性能玻纤技术领先；埃及/美国海外基地；成本优势显著"
    },
    "TCL科技": {
        "positioning": "全球面板双龙头之一+半导体光伏材料布局",
        "market_share": "LCD面板全球市占率25%(与京东方并列)，大尺寸LCD全球第一",
        "financials": "2026Q1营收约400亿元(+15%+)，归母净利约15亿元(+50%+)，毛利率15%+；2025全年营收1600亿元(+12%)",
        "core_clients": "三星、LG、海信、小米、索尼",
        "competitive_edge": "LCD大尺寸全球第一；TCL中环(半导体硅片+光伏)协同；面板周期反转弹性大；印刷OLED技术布局"
    },
    "巨化股份": {
        "positioning": "国内氟化工龙头，制冷剂+含氟聚合物+电子化学品",
        "market_share": "国内制冷剂市占率30%+(第一)，含氟聚合物国内前列",
        "financials": "2026Q1营收约60亿元(+25%+)，归母净利约8亿元(+80%+)，毛利率25%+；2025全年营收200亿元(+20%)",
        "core_clients": "格力、美的、海尔(制冷剂)、新能源汽车厂(含氟材料)",
        "competitive_edge": "第三代制冷剂配额国内第一；含氟聚合物(PTFE/PVDF)产能领先；电子级氢氟酸布局；制冷剂长周期景气上行"
    },
    "顺络电子": {
        "positioning": "国内电感龙头，被动元件平台型公司",
        "market_share": "国内电感市占率30%+，全球前十",
        "financials": "2026Q1营收约15亿元(+30%+)，归母净利约2.5亿元(+50%+)，毛利率35%+；2025全年营收55亿元(+25%)",
        "core_clients": "苹果、三星、华为、小米、比亚迪、特斯拉",
        "competitive_edge": "电感国产替代核心；01005超小尺寸电感量产；汽车电子电感认证突破；高频电感5G/PA应用"
    },
    # ==================== 其他 ====================
    "三花智控": {
        "positioning": "全球热管理龙头，新能源汽车+AI服务器散热双驱动",
        "market_share": "新能源车热管理全球市占率35%+(第一)，电子膨胀阀全球60%+",
        "financials": "2026Q1营收约60亿元(+25%+)，归母净利约7亿元(+35%+)，毛利率28%+；2025全年营收250亿元(+20%)",
        "core_clients": "特斯拉、比亚迪、大众、宝马、英伟达(AI液冷)",
        "competitive_edge": "新能源车热管理全球第一；电子膨胀阀绝对龙头；AI服务器液冷(冷板/CDU)快速放量；特斯拉核心供应商"
    },
    "大族激光": {
        "positioning": "国内激光装备龙头，PCB+面板+动力电池激光平台",
        "market_share": "国内激光装备市占率15%+(第一)，PCB激光钻孔机国内第一",
        "financials": "2026Q1营收约35亿元(+25%+)，归母净利约3亿元(+50%+)，毛利率35%+；2025全年营收150亿元(+20%)",
        "core_clients": "苹果、比亚迪、宁德时代、京东方、TCL",
        "competitive_edge": "激光器自研+装备整机一体化；PCB激光钻孔机(M2/M4)国产替代；动力电池激光焊接龙头；高端激光切割突破"
    },
    "恒瑞医药": {
        "positioning": "国内创新药龙头，ADC+GLP-1+自免疫双驱动",
        "market_share": "国内抗肿瘤药市占率10%+，创新药收入占比持续提升",
        "financials": "2026Q1营收约70亿元(+20%+)，归母净利约15亿元(+30%+)，毛利率85%+；2025全年营收280亿元(+18%)",
        "core_clients": "全国医院/药店，海外授权默克/卡瑞利珠单抗",
        "competitive_edge": "创新药研发投入国内第一(年60亿+)；ADC管线(HER2/TROP2)深度布局；GLP-1减重药突破；海外授权收入加速"
    },
    "长江证券": {
        "positioning": "中型综合券商，研究所实力突出",
        "market_share": "国内券商营收排名20位左右，研究所影响力前列",
        "financials": "2026Q1营收约30亿元(+30%+)，归母净利约10亿元(+50%+)；2025全年营收120亿元(+25%)",
        "core_clients": "机构投资者、零售客户、企业融资客户",
        "competitive_edge": "研究所卖方研究实力行业前列；自营投资收益弹性大；牛市成交量放大核心受益"
    },
}

# ========== 投研分析生成函数 ==========
def generate_research_analysis(r):
    """为单只S/A级公司生成专业投研分析"""
    name = r["name"]
    code = r["code"]
    rating = r["rating"]
    total = r["total"]
    sector = r.get("sector", "综合")
    sub_theme = r.get("sub_theme", "")
    pe = r.get("pe", 0) or 0
    fwd_pe = r.get("fwd_pe", 0) or 0
    growth = r.get("growth", 0) or 0
    turnover = (r.get("turnover", 0) or 0) / 100000000

    score_news = r.get("score_news", 0)
    score_tech = r.get("score_tech", 0)
    score_fund = r.get("score_fund", 0)
    score_theme = r.get("score_theme", 0)

    news_reasons = r.get("news_reasons", [])
    fund_reasons = r.get("fund_reasons", [])
    theme_reasons = r.get("theme_reasons", r.get("flow_reasons", []))
    tech = r.get("tech", {})

    # 优先按sub_theme细分匹配，其次回退到板块数据库
    sector_data = SUBTHEME_RESEARCH.get(sub_theme) or SECTOR_RESEARCH.get(sector, SECTOR_RESEARCH["综合"])

    # --- 模块一：核心业务竞争力评估 ---
    # 优先使用公司专属财务数据库(如有)，提供数据支撑的专业分析
    profile = COMPANY_PROFILE.get(name)

    comp_parts = []

    if profile:
        # 基于专属数据库的深度分析
        if profile.get("positioning"):
            comp_parts.append(f"<b>{profile['positioning']}</b>。")

        # 行业地位与市场份额
        if profile.get("market_share"):
            comp_parts.append(f"<b>市场地位：</b>{profile['market_share']}。")

        # 最新财务数据
        if profile.get("financials"):
            comp_parts.append(f"<b>财务表现：</b>{profile['financials']}。")

        # 核心竞争力
        if profile.get("competitive_edge"):
            comp_parts.append(f"<b>核心壁垒：</b>{profile['competitive_edge']}。")

        # 产能与客户
        if profile.get("capacity"):
            comp_parts.append(f"<b>产能布局：</b>{profile['capacity']}。")
        if profile.get("core_clients"):
            comp_parts.append(f"<b>核心客户：</b>{profile['core_clients']}。")

        # 订单/专利等附加信息
        if profile.get("orders"):
            comp_parts.append(f"<b>订单储备：</b>{profile['orders']}。")
        if profile.get("business_structure"):
            comp_parts.append(f"<b>业务结构：</b>{profile['business_structure']}。")
        if profile.get("patents"):
            comp_parts.append(f"<b>技术储备：</b>{profile['patents']}。")

        # 结合评分数据补充市场表现
        comp_parts.append(f"<b>市场表现：</b>题材热度({score_theme}/5)、基本面({score_fund}/5)、消息面({score_news}/5)、技术面({score_tech}/5)，")
        if growth and growth > 50:
            comp_parts.append(f"预期增速<b>+{growth:.0f}%</b>，")
        if fwd_pe and fwd_pe > 0:
            comp_parts.append(f"前瞻PE <b>{fwd_pe:.0f}x</b>，")
            if growth and growth > 0:
                peg = fwd_pe / growth
                comp_parts.append(f"PEG约{peg:.2f}。")
        if turnover > 15:
            comp_parts.append(f"日成交额<b>{turnover:.0f}亿</b>，资金参与活跃。")

        # 行业关联性说明
        comp_parts.append(f"<b>行业关联：</b>公司主营业务{sub_theme}与所属行业研究高度相关，上述财务数据与市场份额印证了其在{sub_theme}赛道的龙头地位。")
    else:
        # 无专属数据库的公司，使用通用分析框架
        # 题材定位
        if sub_theme:
            comp_parts.append(f"公司深耕<b>{sector}</b>领域，细分赛道聚焦<b>{sub_theme}</b>，")
        else:
            comp_parts.append(f"公司作为<b>{sector}</b>板块核心标的，")

        # 评分维度竞争力
        dim_strengths = []
        if score_theme >= 4:
            dim_strengths.append(f"题材热度突出({score_theme}/5)，处于市场主线风口")
        if score_fund >= 4:
            dim_strengths.append(f"基本面扎实({score_fund}/5)，估值具备吸引力")
        elif score_fund <= 2 and pe > 80:
            dim_strengths.append(f"当前PE({pe:.0f}x)偏高，基本面评分({score_fund}/5)有待提升")
        if score_news >= 4:
            dim_strengths.append(f"消息面强劲({score_news}/5)，利好催化密集")
        if score_tech >= 4:
            dim_strengths.append(f"技术面看多({score_tech}/5)，处于强势趋势")
        elif score_tech <= 2:
            dim_strengths.append(f"技术面承压({score_tech}/5)，短期趋势偏弱")

        if dim_strengths:
            comp_parts.append("；".join(dim_strengths) + "。")

        # 增长与估值
        if growth and growth > 50:
            comp_parts.append(f"业绩高增长(预期增速<b>+{growth:.0f}%</b>)，")
            if fwd_pe and fwd_pe > 0:
                comp_parts.append(f"对应前瞻PE <b>{fwd_pe:.0f}x</b>，PEG视角下估值具备性价比。")
            else:
                comp_parts.append("成长性突出。")
        elif growth and growth > 15:
            comp_parts.append(f"业绩稳健增长(预期增速<b>+{growth:.0f}%</b>)，")
            if fwd_pe and fwd_pe > 0 and fwd_pe < 30:
                comp_parts.append(f"前瞻PE <b>{fwd_pe:.0f}x</b>处于合理区间。")
            else:
                comp_parts.append("增长确定性较高。")
        elif growth is not None and growth < 0:
            comp_parts.append(f"短期业绩承压(预期增速<b>{growth:.0f}%</b>)，需关注盈利修复节奏。")
        else:
            if fwd_pe and fwd_pe > 0 and fwd_pe < 25:
                comp_parts.append(f"前瞻PE <b>{fwd_pe:.0f}x</b>估值合理，")
            comp_parts.append("业绩表现平稳。")

        # 资金关注度
        if turnover > 50:
            comp_parts.append(f"日成交额<b>{turnover:.0f}亿</b>，市场关注度极高，资金参与活跃。")
        elif turnover > 15:
            comp_parts.append(f"日成交额<b>{turnover:.0f}亿</b>，流动性充裕。")

    competitiveness = "".join(comp_parts)

    # --- 模块二：行业发展前景预测 ---
    industry_outlook = sector_data["outlook"]
    driver_tags = "".join([f'<span class="tag pos">{d}</span>' for d in sector_data["drivers"]])

    # --- 模块三：潜在风险因素识别 ---
    risk_items = list(sector_data["risks"])

    # 公司特有风险
    if pe and pe > 100:
        risk_items.insert(0, f"估值偏高(PE(TTM) {pe:.0f}x)，回调风险大")
    if growth is not None and growth < 0:
        risk_items.insert(0, "业绩负增长，盈利能力存疑")
    if fwd_pe and fwd_pe > 80:
        risk_items.insert(0, f"前瞻PE({fwd_pe:.0f}x)极高，透支未来增长预期")
    if score_tech <= 2:
        risk_items.insert(0, "技术面弱势，短期下行压力")
    if turnover < 8:
        risk_items.insert(0, "流动性偏低，大资金进出困难")

    # 从news_reasons提取风险
    for n in news_reasons:
        if "⚠" in n:
            risk_text = n.replace("⚠️", "").replace("⚠", "").strip()
            if risk_text:
                risk_items.insert(0, risk_text[:50])

    risk_tags = "".join([f'<span class="tag neg">{r}</span>' for r in risk_items[:8]])

    # --- 模块四：投资价值分析 ---
    value_parts = []

    # 评级定位
    if rating == "S":
        value_parts.append(f"公司综合评分<b>{total:.1f}/20</b>，获评<b style='color:#fbbf24;'>S级(强烈推荐)</b>，")
    else:
        value_parts.append(f"公司综合评分<b>{total:.1f}/20</b>，获评<b style='color:#3b82f6;'>A级(重点关注)</b>，")

    # 估值判断
    if fwd_pe and fwd_pe > 0:
        if fwd_pe < 20:
            value_parts.append(f"前瞻PE {fwd_pe:.0f}x处于低估区间，")
        elif fwd_pe < 35:
            value_parts.append(f"前瞻PE {fwd_pe:.0f}x估值合理，")
        elif fwd_pe < 60:
            value_parts.append(f"前瞻PE {fwd_pe:.0f}x估值偏高但成长性可对冲，")
        else:
            value_parts.append(f"前瞻PE {fwd_pe:.0f}x估值较高，需业绩兑现消化，")

    # PEG判断
    if growth and growth > 0 and fwd_pe and fwd_pe > 0:
        peg = fwd_pe / growth
        if peg < 1:
            value_parts.append(f"PEG约{peg:.2f}(&lt;1)，<b>估值低于增速</b>，具备较好性价比。")
        elif peg < 2:
            value_parts.append(f"PEG约{peg:.2f}，估值与增速基本匹配。")
        else:
            value_parts.append(f"PEG约{peg:.2f}(&gt;2)，估值相对增速偏高。")

    # 技术面位置
    position = tech.get("position", 50)
    if position < 30:
        value_parts.append(f"技术分位{position:.0f}%处于低位，上行空间充足。")
    elif position < 60:
        value_parts.append(f"技术分位{position:.0f}%处于中位，趋势向好。")
    elif position < 85:
        value_parts.append(f"技术分位{position:.0f}%偏高，注意追高风险。")
    else:
        value_parts.append(f"技术分位{position:.0f}%处于高位，短期回调风险大。")

    # 综合建议
    if rating == "S" and score_fund >= 4 and score_theme >= 4:
        value_parts.append("<b>综合判断：基本面与题材共振，建议逢低重点配置。</b>")
    elif rating == "S":
        value_parts.append("<b>综合判断：多维度表现优异，建议重点关注逢低布局。</b>")
    elif score_theme >= 4 and score_fund >= 3:
        value_parts.append("<b>综合判断：题材与基本面均衡，适合波段操作逢低参与。</b>")
    elif growth and growth > 100:
        value_parts.append("<b>综合判断：高成长驱动，关注业绩兑现节奏与估值消化。</b>")
    else:
        value_parts.append("<b>综合判断：综合评分靠前，建议纳入观察池择机参与。</b>")

    value_analysis = "".join(value_parts)

    # 生成HTML
    tier_class = "s-tier" if rating == "S" else "a-tier"
    rating_color = RATING_COLORS[rating]
    rating_bg = RATING_BG[rating]

    html = f'''
        <div class="research-card {tier_class}">
            <div class="rc-header">
                <span class="rc-name">{name}</span>
                <span class="rc-code">{code}</span>
                <span class="rc-rating" style="background:{rating_bg};color:{rating_color};">{rating}级</span>
                <span class="rc-code">{sector}{' · ' + sub_theme if sub_theme else ''}</span>
                <span class="rc-score">综合评分 <b style="color:{rating_color}">{total:.1f}</b>/20</span>
            </div>
            <div class="research-module">
                <div class="rm-title">📊 核心业务竞争力评估</div>
                <div class="rm-content">{competitiveness}</div>
            </div>
            <div class="research-module">
                <div class="rm-title">🚀 行业发展前景预测</div>
                <div class="rm-content">{industry_outlook}<br><div style="margin-top:8px;">{driver_tags}</div></div>
            </div>
            <div class="research-module risk">
                <div class="rm-title">⚠️ 潜在风险因素识别</div>
                <div class="rm-content">{risk_tags}</div>
            </div>
            <div class="research-module value">
                <div class="rm-title">💎 投资价值分析</div>
                <div class="rm-content">{value_analysis}</div>
            </div>
        </div>'''
    return html

# 生成S/A级公司投研分析
sa_stocks = [r for r in results if r.get("rating") in ("S", "A")]
sa_stocks.sort(key=lambda x: -x["total"])

s_count_research = sum(1 for r in sa_stocks if r["rating"] == "S")
a_count_research = sum(1 for r in sa_stocks if r["rating"] == "A")

html_parts.append(f'''
        <div style="font-size:12px;color:#94a3b8;margin-bottom:16px;">
            覆盖范围：<b style="color:#fbbf24;">S级{s_count_research}只</b> · <b style="color:#3b82f6;">A级{a_count_research}只</b> · 共{len(sa_stocks)}只
        </div>''')

for stock in sa_stocks:
    html_parts.append(generate_research_analysis(stock))

html_parts.append('''
    </div>

    <!-- 完整表格 -->
    <div class="section-title">📋 完整排名 (可排序/筛选/分页)</div>

    <!-- 筛选控制 -->
    <div class="controls">
        <div class="control-group">
            <label>🔍 搜索股票</label>
            <input type="text" id="searchInput" placeholder="输入名称或代码...">
        </div>
        <div class="control-group">
            <label>📊 排序方式</label>
            <select id="sortSelect">
                <option value="total">综合得分 (默认)</option>
                <option value="turnover">成交额</option>
                <option value="pct_chg">涨跌幅</option>
                <option value="pe">PE估值</option>
                <option value="score_news">消息面</option>
                <option value="score_tech">技术面</option>
                <option value="score_fund">基本面</option>
                <option value="score_theme">题材热度</option>
            </select>
        </div>
        <div class="control-group">
            <label>⭐ 评级筛选</label>
            <select id="ratingFilter">
                <option value="">全部评级</option>
                <option value="S">S 级</option>
                <option value="A">A 级</option>
                <option value="B">B 级</option>
                <option value="C">C 级</option>
            </select>
        </div>
        <div class="control-group">
            <label>🏷️ 板块筛选</label>
            <select id="sectorFilter">
                <option value="">全部板块</option>
            </select>
        </div>
        <div class="control-group">
            <label>📈 每页显示</label>
            <select id="pageSize">
                <option value="20">20条/页</option>
                <option value="50">50条/页</option>
                <option value="100">100条/页</option>
                <option value="''' + str(total_stocks) + '''">全部''' + str(total_stocks) + '''条</option>
            </select>
        </div>
    </div>

    <!-- 列显示控制 -->
    <div class="col-toggle">
        <span style="color:#64748b;font-size:11px;margin-right:8px;">列显示:</span>
        <label><input type="checkbox" class="col-check" data-col="code" checked>代码</label>
        <label><input type="checkbox" class="col-check" data-col="sector" checked>板块</label>
        <label><input type="checkbox" class="col-check" data-col="pct" checked>涨跌幅</label>
        <label><input type="checkbox" class="col-check" data-col="scores" checked>四项评分</label>
        <label><input type="checkbox" class="col-check" data-col="total" checked>总分</label>
        <label><input type="checkbox" class="col-check" data-col="rating" checked>评级</label>
        <label><input type="checkbox" class="col-check" data-col="pe" checked>PE/Fwd</label>
        <label><input type="checkbox" class="col-check" data-col="sub" checked>细分题材</label>
        <label><input type="checkbox" class="col-check" data-col="turnover" checked>成交额</label>
    </div>

    <div class="table-container">
        <table class="stock-table" id="stockTable">
            <thead>
                <tr>
                    <th class="s-rank" data-sort="rank"># <span class="sort-arrow"></span></th>
                    <th class="s-name" data-sort="name">名称 <span class="sort-arrow"></span></th>
                    <th class="s-code col-code">代码</th>
                    <th class="s-sector col-sector">板块</th>
                    <th class="s-pct col-pct" data-sort="pct_chg">涨跌幅 <span class="sort-arrow"></span></th>
                    <th class="s-scores col-scores">四项评分</th>
                    <th class="s-total col-total" data-sort="total">总分 <span class="sort-arrow"></span></th>
                    <th class="s-rating col-rating">评级</th>
                    <th class="s-pe col-pe">PE(TTM)/Fwd/阶梯</th>
                    <th class="s-sector col-sub">细分题材</th>
                    <th class="s-turnover col-turnover" data-sort="turnover">成交额 <span class="sort-arrow"></span></th>
                </tr>
            </thead>
            <tbody id="tableBody">''')

# 生成表格行
for idx, r in enumerate(results):
    i = idx + 1
    pct = r.get("pct_chg", 0) or 0
    pct_cls = "up" if pct > 0 else "down" if pct < 0 else ""
    rating = r["rating"]
    fwd = f"{r['fwd_pe']:.0f}x" if r.get("fwd_pe") else "-"
    growth = f"{r['growth']:+.0f}%" if r.get("growth") else "-"
    ladder_val = r.get("pe_ladder")
    if ladder_val and isinstance(ladder_val, dict):
        yrs = sorted(ladder_val.keys())
        ladder_t = "→".join(f"{ladder_val[y]}" for y in yrs)
    else:
        ladder_t = "-"
    pe = f"{r['pe']:.0f}" if r.get("pe") and r["pe"] > 0 else "-"
    sub_theme_t = r.get("sub_theme", "-")
    turnover_yi = (r.get("turnover", 0) or 0) / 100000000

    scores = [r["score_news"], r["score_tech"], r["score_fund"], r.get("score_theme", r.get("score_flow", 0))]
    score_colors = ["#ef4444", "#f97316", "#f59e0b", "#3b82f6", "#22c55e"]
    scores_html = "".join([f'<div class="s-score" style="background:{score_colors[min(int(v),4)]}">{v}</div>' for v in scores])

    # 详情数据存到data属性，点击时懒加载
    tech = r.get("tech", {})
    news_reasons = r.get("news_reasons", [])
    fund_reasons = r.get("fund_reasons", [])
    theme_reasons = r.get("theme_reasons", r.get("flow_reasons", []))
    # 转义引号用于data属性
    news_text = "<br>".join([html_mod.escape(n.replace("⚠️", "")) for n in news_reasons[:3]]) if news_reasons else "无"
    tech_text = f"趋势:{tech.get('trend','N/A')} · 分位:{tech.get('position','N/A')}%<br>{html_mod.escape(tech.get('reason',''))}"
    fund_text = ", ".join([html_mod.escape(x) for x in fund_reasons[:3]]) if fund_reasons else "无"
    theme_text = ", ".join([html_mod.escape(x) for x in theme_reasons[:3]]) if theme_reasons else "无"

    html_parts.append(f'''
                <tr class="expandable" data-rating="{rating}" data-sector="{r.get('sector','')}" data-code="{r['code']}" data-name="{r['name']}" data-original-rank="{i}" data-fwd-pe="{r.get('fwd_pe',0) or 0}" data-growth="{r.get('growth',0) or 0}" data-position="{tech.get('position',50) or 50}" data-score-theme="{r.get('score_theme',0)}"
                    data-news="{r['score_news']}" data-tech-score="{r['score_tech']}" data-fund-score="{r['score_fund']}" data-theme-score="{r.get('score_theme',0)}"
                    data-news-text="{html_mod.escape(news_text)}" data-tech-text="{html_mod.escape(tech_text)}" data-fund-text="{html_mod.escape(fund_text)}" data-theme-text="{html_mod.escape(theme_text)}">
                    <td class="s-rank" data-value="{i}">{i}</td>
                    <td class="s-name" data-value="{r['name']}">{r['name']}</td>
                    <td class="s-code col-code" data-value="{r['code']}">{r['code']}</td>
                    <td class="s-sector col-sector" data-value="{r.get('sector','')}">{r.get('sector','')}</td>
                    <td class="s-pct {pct_cls} col-pct" data-value="{pct}">{pct:+.2f}%</td>
                    <td class="s-scores col-scores">{scores_html}</td>
                    <td class="s-total col-total" data-value="{r['total']}" style="color:{RATING_COLORS[rating]}">{r['total']:.1f}</td>
                    <td class="s-rating col-rating" style="background:{RATING_BG[rating]};color:{RATING_COLORS[rating]}" data-value="{rating}">{rating}</td>
                    <td class="s-pe col-pe" data-value="{r.get('pe',0)}">{pe}/{fwd} {ladder_t} {growth}</td>
                    <td class="s-sector col-sub">{sub_theme_t if sub_theme_t else '-'}</td>
                    <td class="s-turnover col-turnover" data-value="{r.get('turnover',0)}">{turnover_yi:.0f}亿</td>
                </tr>''')

html_parts.append('''
            </tbody>
        </table>
    </div>

    <!-- 分页控件 -->
    <div class="pagination" id="pagination">
        <button id="prevPage">← 上一页</button>
        <span class="page-info" id="pageInfo">1 / 1</span>
        <button id="nextPage">下一页 →</button>
    </div>

    <div class="footer">
        <p>⚠️ 免责声明: 本报告由AI基于stock-scorer极简评分模型自动生成,仅供参考,不构成投资建议。投资有风险,入市需谨慎。</p>
        <p>评分模型: 题材热度30% + 基本面30%(含行业前景) + 消息面20% + 技术面20%</p>
        <p>Generated: ''' + datetime.now().strftime('%Y-%m-%d %H:%M:%S') + '''</p>
    </div>
</div>

<script>
// 数据存储
const sectors = new Set();
let currentPage = 1;
let currentSort = {field: 'total', desc: true};
let quickFilter = 'all';
const ratingOrder = {"S": 4, "A": 3, "B": 2, "C": 1};

document.addEventListener('DOMContentLoaded', function() {
    initFilters();
    initSorting();
    initQuickFilters();
    initColToggle();
    initExpandableRows();
    initPagination();
    collectSectors();
    applyFilters();
});

function collectSectors() {
    const rows = document.querySelectorAll('#stockTable tbody tr.expandable');
    rows.forEach(row => {
        if (row.dataset.sector) sectors.add(row.dataset.sector);
    });
    const sectorFilter = document.getElementById('sectorFilter');
    Array.from(sectors).sort().forEach(s => {
        const opt = document.createElement('option');
        opt.value = s;
        opt.textContent = s;
        sectorFilter.appendChild(opt);
    });
}

// 筛选相关
function initFilters() {
    document.getElementById('searchInput').addEventListener('input', debounce(applyFilters, 300));
    document.getElementById('sortSelect').addEventListener('change', function() {
        currentSort.field = this.value;
        currentSort.desc = true;
        currentPage = 1;
        applyFilters();
    });
    document.getElementById('ratingFilter').addEventListener('change', function() { currentPage = 1; applyFilters(); });
    document.getElementById('sectorFilter').addEventListener('change', function() { currentPage = 1; applyFilters(); });
    document.getElementById('pageSize').addEventListener('change', function() { currentPage = 1; applyFilters(); });
}

// 快捷筛选
function initQuickFilters() {
    document.querySelectorAll('.quick-btn').forEach(btn => {
        btn.addEventListener('click', function() {
            document.querySelectorAll('.quick-btn').forEach(b => b.classList.remove('active'));
            this.classList.add('active');
            quickFilter = this.dataset.quick;
            currentPage = 1;
            applyFilters();
        });
    });
}

// 列显示控制
function initColToggle() {
    document.querySelectorAll('.col-check').forEach(cb => {
        cb.addEventListener('change', function() {
            const col = this.dataset.col;
            const visible = this.checked;
            document.querySelectorAll('.col-' + col).forEach(el => {
                el.style.display = visible ? '' : 'none';
            });
        });
    });
}

// 可展开行 - 懒加载，点击时才生成详情
function initExpandableRows() {
    document.querySelector('#stockTable tbody').addEventListener('click', function(e) {
        const row = e.target.closest('tr.expandable');
        if (!row) return;
        let detailRow = row.nextElementSibling;
        if (!detailRow || !detailRow.classList.contains('detail-row')) {
            // 动态创建详情行
            detailRow = document.createElement('tr');
            detailRow.className = 'detail-row';
            detailRow.dataset.parent = row.dataset.code;
            const newsScore = row.dataset.news || '0';
            const techScore = row.dataset.techScore || '0';
            const fundScore = row.dataset.fundScore || '0';
            const themeScore = row.dataset.themeScore || '0';
            const newsText = row.dataset.newsText || '无';
            const techText = row.dataset.techText || '';
            const fundText = row.dataset.fundText || '无';
            const themeText = row.dataset.themeText || '无';
            detailRow.innerHTML = '<td colspan="11"><div class="detail-content" style="background:#0f172a;border-radius:8px;padding:16px;margin:4px;">' +
                '<div style="display:grid;grid-template-columns:repeat(4,1fr);gap:12px;font-size:11px;">' +
                '<div><div style="color:#94a3b8;margin-bottom:4px;">📰 消息面(' + newsScore + '/5)</div><div style="color:#e2e8f0;">' + newsText + '</div></div>' +
                '<div><div style="color:#94a3b8;margin-bottom:4px;">📈 技术面(' + techScore + '/5)</div><div style="color:#e2e8f0;">' + techText + '</div></div>' +
                '<div><div style="color:#94a3b8;margin-bottom:4px;">📊 基本面(' + fundScore + '/5)</div><div style="color:#e2e8f0;">' + fundText + '</div></div>' +
                '<div><div style="color:#94a3b8;margin-bottom:4px;">🔥 题材热度(' + themeScore + '/5)</div><div style="color:#e2e8f0;">' + themeText + '</div></div>' +
                '</div></div></td>';
            row.parentNode.insertBefore(detailRow, row.nextSibling);
        }
        detailRow.classList.toggle('show');
    });
}

// 排序相关
function initSorting() {
    document.querySelectorAll('#stockTable th[data-sort]').forEach(th => {
        th.addEventListener('click', () => {
            const field = th.dataset.sort;
            if (currentSort.field === field) {
                currentSort.desc = !currentSort.desc;
            } else {
                currentSort.field = field;
                currentSort.desc = true;
            }
            currentPage = 1;
            applyFilters();
        });
    });
}

// 分页
function initPagination() {
    document.getElementById('prevPage').addEventListener('click', function() {
        if (currentPage > 1) { currentPage--; applyFilters(); }
    });
    document.getElementById('nextPage').addEventListener('click', function() {
        currentPage++; applyFilters();
    });
}

// 快捷筛选判定
function passesQuickFilter(row) {
    if (quickFilter === 'all') return true;
    if (quickFilter === 'SA') return row.dataset.rating === 'S' || row.dataset.rating === 'A';
    if (quickFilter === 'S') return row.dataset.rating === 'S';
    if (quickFilter === 'undervalued') return parseFloat(row.dataset.fwdPe) > 0 && parseFloat(row.dataset.fwdPe) < 30;
    if (quickFilter === 'growth') return parseFloat(row.dataset.growth) >= 30;
    if (quickFilter === 'low_pos') return parseFloat(row.dataset.position) < 30;
    if (quickFilter === 'hot') return parseInt(row.dataset.scoreTheme) >= 4;
    if (quickFilter === 'up') return row.querySelector('.rating-up') !== null;
    if (quickFilter === 'new') return row.querySelector('.rating-new') !== null;
    return true;
}

// 主筛选+分页函数
function applyFilters() {
    const search = document.getElementById('searchInput').value.toLowerCase();
    const rating = document.getElementById('ratingFilter').value;
    const sector = document.getElementById('sectorFilter').value;
    const pageSize = parseInt(document.getElementById('pageSize').value);

    const tbody = document.getElementById('tableBody');
    let rows = Array.from(tbody.querySelectorAll('tr.expandable'));
    // 懒加载模式下，先清理已展开的详情行
    tbody.querySelectorAll('tr.detail-row').forEach(d => d.remove());

    // 筛选
    let filtered = rows.filter(row => {
        if (search && !row.dataset.name.toLowerCase().includes(search) && !row.dataset.code.toLowerCase().includes(search)) return false;
        if (rating && row.dataset.rating !== rating) return false;
        if (sector && row.dataset.sector !== sector) return false;
        if (!passesQuickFilter(row)) return false;
        return true;
    });

    // 排序
    filtered.sort((a, b) => {
        let aVal, bVal;
        switch(currentSort.field) {
            case 'rank':
                aVal = parseInt(a.dataset.originalRank);
                bVal = parseInt(b.dataset.originalRank);
                break;
            case 'name':
                aVal = a.dataset.name;
                bVal = b.dataset.name;
                return currentSort.desc ? bVal.localeCompare(aVal) : aVal.localeCompare(bVal);
            case 'pct_chg':
                aVal = parseFloat(a.querySelector('.s-pct').dataset.value);
                bVal = parseFloat(b.querySelector('.s-pct').dataset.value);
                break;
            case 'total':
                aVal = parseFloat(a.querySelector('.s-total').dataset.value);
                bVal = parseFloat(b.querySelector('.s-total').dataset.value);
                break;
            case 'turnover':
                aVal = parseFloat(a.querySelector('.s-turnover').dataset.value);
                bVal = parseFloat(b.querySelector('.s-turnover').dataset.value);
                break;
            case 'pe':
                aVal = parseFloat(a.querySelector('.s-pe').dataset.value);
                bVal = parseFloat(b.querySelector('.s-pe').dataset.value);
                break;
            default:
                aVal = parseInt(a.dataset.originalRank);
                bVal = parseInt(b.dataset.originalRank);
        }
        return currentSort.desc ? (bVal - aVal) : (aVal - bVal);
    });

    // 更新排序箭头
    document.querySelectorAll('#stockTable th[data-sort]').forEach(th => {
        th.classList.remove('sorted-asc', 'sorted-desc');
        const arrow = th.querySelector('.sort-arrow');
        if (arrow) arrow.textContent = '';
        if (th.dataset.sort === currentSort.field) {
            th.classList.add(currentSort.desc ? 'sorted-desc' : 'sorted-asc');
            if (arrow) arrow.textContent = currentSort.desc ? '↓' : '↑';
        }
    });

    // 分页
    const totalPages = Math.max(1, Math.ceil(filtered.length / pageSize));
    if (currentPage > totalPages) currentPage = totalPages;
    const start = (currentPage - 1) * pageSize;
    const pageRows = filtered.slice(start, start + pageSize);
    const visibleCodes = new Set(pageRows.map(r => r.dataset.code));

    // 重排DOM顺序（懒加载模式：只排expandable行）
    filtered.forEach(row => tbody.appendChild(row));

    // 隐藏所有行
    rows.forEach(row => row.classList.add('hidden-row'));

    // 显示当前页
    pageRows.forEach((row, idx) => {
        row.classList.remove('hidden-row');
        const rankCell = row.querySelector('.s-rank');
        if (rankCell) rankCell.textContent = start + idx + 1;
    });

    // 更新分页信息
    document.getElementById('pageInfo').textContent = currentPage + ' / ' + totalPages + ' (共' + filtered.length + '条)';
    document.getElementById('prevPage').disabled = currentPage <= 1;
    document.getElementById('nextPage').disabled = currentPage >= totalPages;
}

function debounce(func, wait) {
    let timeout;
    return function(...args) {
        clearTimeout(timeout);
        timeout = setTimeout(() => func.apply(this, args), wait);
    };
}
</script>
</body>
</html>''')

# 合并并保存
html = "\n".join(html_parts)
with open(out, "w", encoding="utf-8") as f:
    f.write(html)

# 输出统计
print(f"✅ HTML报告已生成: {out}")
print(f"📊 包含 {total_stocks} 只个股")

sd = stats.get("rating_dist", {})
print(f"⭐ 评级分布: S:{sd.get('S',0)} A:{sd.get('A',0)} B:{sd.get('B',0)} C:{sd.get('C',0)} D:{sd.get('D',0)}")

fwd_ok = sum(1 for r in results if r.get("fwd_pe"))
growth_ok = sum(1 for r in results if r.get("growth"))
print(f"📋 数据完整度: Fwd PE:{fwd_ok}/{total_stocks} Growth:{growth_ok}/{total_stocks}")











