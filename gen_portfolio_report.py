#!/usr/bin/env python3
"""生成「板块动量 × 评分模型」每日组合报告 (单页 HTML)

输入: sector/portfolio_<date>.json + sector/sector_momentum_*.json + sector/backtest_ref.json
输出: sector/portfolio_report_<date>.html
用法: python3 gen_portfolio_report.py [portfolio_json]
"""
import os
import sys
import json
import glob
import argparse

HERE = os.path.dirname(os.path.abspath(__file__))
SECTOR_DIR = os.path.join(HERE, 'sector')

CSS = """
*{box-sizing:border-box}
body{margin:0;padding:28px;background:#f5f7fa;color:#1b2a3d;
 font-family:-apple-system,BlinkMacSystemFont,'PingFang SC','Microsoft YaHei',sans-serif;line-height:1.6}
.wrap{max-width:1180px;margin:0 auto}
h1{font-size:24px;margin:0 0 4px;color:#12263f}
h2{font-size:18px;margin:30px 0 12px;color:#12263f;border-left:4px solid #2f6fd0;padding-left:10px}
h3{font-size:15px;margin:18px 0 8px;color:#223}
.sub{color:#6b7c90;font-size:13px;margin-bottom:18px}
.card{background:#fff;border:1px solid #e3e9f0;border-radius:10px;padding:18px 20px;margin-bottom:16px;
 box-shadow:0 1px 3px rgba(20,40,70,.05)}
.kpis{display:flex;gap:14px;flex-wrap:wrap;margin-bottom:16px}
.kpi{flex:1;min-width:180px;background:#fff;border:1px solid #e3e9f0;border-radius:10px;padding:14px 16px}
.kpi .lab{font-size:12px;color:#7b8ca0}
.kpi .val{font-size:22px;font-weight:600;color:#12263f;margin-top:2px}
.kpi .note{font-size:12px;color:#8899ab;margin-top:2px}
table{width:100%;border-collapse:collapse;font-size:13px}
th{background:#eef3f9;color:#33475f;font-weight:600;text-align:left;padding:8px 9px;border-bottom:1px solid #dde5ee;white-space:nowrap}
td{padding:7px 9px;border-bottom:1px solid #f0f4f8;white-space:nowrap}
tr:hover td{background:#fafcfe}
.up{color:#c62828;font-weight:600}.down{color:#2e7d32;font-weight:600}.flat{color:#5a6a7d}
.tag{display:inline-block;padding:1px 7px;border-radius:4px;font-size:11.5px;font-weight:600}
.t-S{background:#c62828;color:#fff}.t-A{background:#ef6c00;color:#fff}.t-B{background:#f9a825;color:#fff}
.t-C{background:#90a4ae;color:#fff}.t-D{background:#607d8b;color:#fff}
.badge{display:inline-block;background:#e8f0fe;color:#2f6fd0;border-radius:4px;padding:1px 7px;font-size:11.5px;margin-right:4px}
.badge.warn{background:#fdeaea;color:#c62828}
.badge.ok{background:#e6f4ea;color:#2e7d32}
.mono{font-family:'SF Mono',Menlo,monospace;font-size:12.5px}
.split{display:flex;gap:16px;flex-wrap:wrap}
.split>div{flex:1;min-width:430px}
ol,ul{margin:8px 0 8px 20px;padding:0}
li{margin:4px 0;font-size:13.5px}
.note{font-size:12.5px;color:#7b8ca0;margin-top:8px}
.warnbox{background:#fff8e1;border:1px solid #ffe082;border-radius:8px;padding:12px 16px;font-size:13px;color:#795548}
.hl{background:#fff3cd;padding:1px 4px;border-radius:3px}
.sec-hd{display:flex;justify-content:space-between;align-items:baseline;margin-bottom:8px}
.sec-hd .lv{font-size:12.5px;color:#2f6fd0}
"""


def pct(x, dec=2):
    if x is None:
        return '<span class="flat">—</span>'
    cls = 'up' if x > 0 else ('down' if x < 0 else 'flat')
    return f'<span class="{cls}">{x:+.{dec}f}%</span>'


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('portfolio', nargs='?', default=None)
    ap.add_argument('--out', default=None)
    args = ap.parse_args()

    pf_path = args.portfolio
    if pf_path is None:
        fs = sorted(glob.glob(os.path.join(SECTOR_DIR, 'portfolio_*.json')), reverse=True)
        pf_path = fs[0] if fs else None
    if not pf_path or not os.path.exists(pf_path):
        print("❌ 未找到 portfolio json")
        return 1
    pf = json.load(open(pf_path, encoding='utf-8'))
    mdate = pf.get('momentum_date') or pf.get('date')
    mpath = os.path.join(SECTOR_DIR, f"sector_momentum_{mdate}.json")
    if not os.path.exists(mpath):
        cands = sorted(glob.glob(os.path.join(SECTOR_DIR, 'sector_momentum_2*.json')), reverse=True)
        mpath = cands[0] if cands else None
    mom = json.load(open(mpath, encoding='utf-8')) if mpath else {'sectors': []}
    ref = json.load(open(os.path.join(SECTOR_DIR, 'backtest_ref.json'), encoding='utf-8')) \
        if os.path.exists(os.path.join(SECTOR_DIR, 'backtest_ref.json')) else {}

    rows = mom.get('sectors', [])
    n = len(rows)
    sdate = pf.get('date')
    meta = ref.get('meta', {})

    # ---------- 板块动量全景 ----------
    top_sectors = rows[:10]
    bot_sectors = rows[-10:][::-1]
    top10_short = sorted(rows, key=lambda x: x['r10'])[:10]

    def sector_rows(rs):
        out = []
        for r in rs:
            out.append(f"<tr><td><b>{r['sector']}</b></td><td>{r.get('parent','')}</td>"
                       f"<td>{r['r60']}</td><td>{r['r10']}</td>"
                       f"<td>{pct(r['mom60'])}</td><td>{pct(r['mom10'])}</td><td>{r['n_members']}</td></tr>")
        return "\n".join(out)

    # ---------- 组合表 ----------
    def pick_rows(picks):
        out = []
        for i, p in enumerate(picks, 1):
            rsi = p.get('rsi')
            rsi_s = f"{rsi:.1f}" if isinstance(rsi, (int, float)) else '—'
            rsi_cls = 'up' if isinstance(rsi, (int, float)) and rsi > 70 else ('down' if isinstance(rsi, (int, float)) and rsi < 30 else '')
            out.append(
                f"<tr><td>{i}</td><td><b>{p['name']}</b></td><td class='mono'>{p['code']}</td>"
                f"<td>{p.get('sector_l2') or '—'}</td>"
                f"<td><span class='tag t-{p.get('rating','B')}'>{p.get('rating','-')}</span> {p['total']:.1f}</td>"
                f"<td>{p.get('price') if p.get('price') is not None else '—'}</td>"
                f"<td>{pct(p.get('pct_chg'))}</td>"
                f"<td>{p.get('sector_r60','—')} / {p.get('sector_r10','—')}</td>"
                f"<td>{pct(p.get('sector_mom60'))} / {pct(p.get('sector_mom10'))}</td>"
                f"<td class='{rsi_cls}'>{rsi_s}</td><td>{p.get('trend') or '—'}</td>"
                f"<td class='mono'>{p.get('score_news')}/{p.get('score_tech')}/{p.get('score_fund')}/{p.get('score_theme')}/{p.get('score_sector')}</td></tr>")
        return "\n".join(out)

    pm = pf['portfolios']['momentum']
    pr = pf['portfolios']['reversal']

    # ---------- 回测表 ----------
    r60b = ref.get('r60_bins', {})
    r10b = ref.get('r10_bins', {})
    cov = ref.get('coverage', {})
    rules = ref.get('rules', {})
    filters = ref.get('filters', {})
    slevel = ref.get('sector_level', {})
    yr_rev = ref.get('rev_rsi30_by_year', {})
    yr_strict = ref.get('strict_by_year', {})

    def bin_rows(bins):
        out = []
        for k, v in bins.items():
            out.append(f"<tr><td>{k}</td>" + "".join(f"<td>{pct(v.get(str(h)))}</td>" for h in (5, 10, 20, 60)) + "</tr>")
        return "\n".join(out)

    def rule_rows():
        out = []
        for name in ['L0_strict_R60=1_R10<=3', 'L1_fb_R60<=3_R10<=5', 'L2_fb_R60<=5_R10<=10',
                     'only_R60=1', 'only_R10<=3', 'rev_R60_last3']:
            d = rules.get(name, {}).get('all', {})
            if not d:
                continue
            c = cov.get(name, {})
            out.append(f"<tr><td><b>{name}</b></td><td>{c.get('pct_days','—')}%</td>"
                       + "".join(f"<td>{pct((d.get(str(h)) or {}).get('day_exc'))}</td>" for h in (5, 10, 20, 60))
                       + f"<td class='mono'>{(d.get('20') or {}).get('t_stat','—')}</td>"
                       + f"<td>{pct((slevel.get(name, {}).get('20') or {}).get('day_exc'))}</td></tr>")
        return "\n".join(out)

    def year_rows(d):
        out = []
        for y, v in d.items():
            out.append(f"<tr><td>{y}</td><td>{v['n_days']}</td><td>{v['avg_stocks']}</td>"
                       f"<td>{pct(v['day_exc'], 3)}</td><td>{pct(v['ann_exc'])}</td>"
                       f"<td>{v['day_win']}%</td><td class='mono'>{v['t_stat']}</td></tr>")
        return "\n".join(out)

    def filt_rows():
        out = []
        for k, d in filters.items():
            if 'L1_fb' not in k and 'rev_R60_last3' not in k:
                continue
            if 'no_filter' not in k and 'ex_RSI>70+bear' not in k:
                continue
            out.append(f"<tr><td class='mono'>{k}</td>"
                       + "".join(f"<td>{pct((d.get(str(h)) or {}).get('day_exc'))}</td>" for h in (5, 10, 20, 60)) + "</tr>")
        return "\n".join(out)

    rev_all = filters.get('rev_R60_last3 | all | no_filter', {})
    rev_rsi = filters.get('rev_R60_last3 | all | only_RSI<30', {})

    html = f"""<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>板块动量 × 评分模型 每日组合 {sdate}</title><style>{CSS}</style></head><body><div class="wrap">
<h1>板块动量 × 评分模型 · 每日组合</h1>
<div class="sub">数据基准日 <b>{sdate}</b>（板块动量 {mdate}）｜ 申万二级 {n} 个板块 · 自建等权指数 ｜
回测样本 {meta.get('n_days','—')} 交易日 / {meta.get('n_stocks','—')} 只股票 / {meta.get('n_sectors','—')} 个板块（{meta.get('start','')} ~ {meta.get('end','')}）</div>

<div class="kpis">
  <div class="kpi"><div class="lab">A 路线（动量·原始思路）</div><div class="val">{len(pm['picks'])} 只</div>
    <div class="note">{len(pm['sectors'])} 个板块 · {pm['level_used']}</div></div>
  <div class="kpi"><div class="lab">B 路线（反转·回测证据）</div><div class="val">{len(pr['picks'])} 只</div>
    <div class="note">{len(pr['sectors'])} 个板块 · {pr['level_used']}</div></div>
  <div class="kpi"><div class="lab">板块60日动量第1</div><div class="val">{rows[0]['sector'] if rows else '—'}</div>
    <div class="note">{pct(rows[0]['mom60']) if rows else ''} ｜ 10日排名 R10={rows[0]['r10'] if rows else '—'}</div></div>
  <div class="kpi"><div class="lab">动量第1板块 后续20日超额</div><div class="val" style="color:#2e7d32">-1.82%</div>
    <div class="note">回测：R60=1 板块内个股等权</div></div>
</div>

<div class="warnbox"><b>核心结论（回测证据）：「板块60日动量第一」是负向指标，不是选股优势。</b><br>
按板块 R60 分组后，20 日超额呈<b>完美负单调</b>：动量最强的 R60=1 为 <b>-1.82%</b>，随排名后移逐步改善，R60&gt;70 的 weakest 板块为 <b>+0.20%</b>；
短期动量 R10 同理（R10≤3 为 -0.82%）。用户原始规则「60日第一 ∩ 10日前三」在 1386 个交易日中仅 <b>38.4%</b> 的日子能选出板块（其余为空集，因为中期强势板块往往短期已回调），
且这些日子买入的 20 日超额为 <b>-1.31%</b>（t=-1.97），6 年里 5 年为负。<br>
真正有效的是反向用法：<b>弱势板块里挑超跌股</b>——60日动量末3位板块 ∩ RSI&lt;30，20 日超额 <b>+1.91%</b>（年化 +22.9%，胜率 60.7%，t=2.09），优于全市场 RSI&lt;30 单因子的 +1.13%。</div>

<h2>一、板块动量全景（{mdate}）</h2>
<div class="split">
  <div class="card"><div class="sec-hd"><h3 style="margin:0">60日动量 Top10</h3><span class="lv">中期最强</span></div>
    <table><thead><tr><th>板块</th><th>一级</th><th>R60</th><th>R10</th><th>60日</th><th>10日</th><th>成分</th></tr></thead>
    <tbody>{sector_rows(top_sectors)}</tbody></table>
    <div class="note">这些板块中期涨幅最大，但回测显示买入后 20 日相对全市场等权平均跑输 1.8 个百分点。</div>
  </div>
  <div class="card"><div class="sec-hd"><h3 style="margin:0">60日动量 Bottom10</h3><span class="lv">中期最弱</span></div>
    <table><thead><tr><th>板块</th><th>一级</th><th>R60</th><th>R10</th><th>60日</th><th>10日</th><th>成分</th></tr></thead>
    <tbody>{sector_rows(bot_sectors)}</tbody></table>
    <div class="note">中期跌得越深的板块，后续 20 日反弹超额越高（A股反转特征）。</div>
  </div>
</div>
<div class="card"><div class="sec-hd"><h3 style="margin:0">10日动量 Top10</h3><span class="lv">短期最强</span></div>
  <table><thead><tr><th>板块</th><th>一级</th><th>R60</th><th>R10</th><th>60日</th><th>10日</th><th>成分</th></tr></thead>
  <tbody>{sector_rows(top10_short)}</tbody></table>
  <div class="note">注意：短期动量前 3 名的板块，其 60 日动量排名往往在中后段——这解释了为什么「60日第一 ∩ 10日前三」经常是空集。</div>
</div>

<h2>二、A 路线组合（动量：60日第一 ∩ 10日前三）</h2>
<div class="card">
  <div class="sec-hd"><h3 style="margin:0">命中层级</h3><span class="lv">{pm['level_used']}　目标规则：{pm['target_rule']}</span></div>
  <p style="font-size:13px;margin:4px 0">选中板块：<span class="badge">{'　'.join(pm['sectors'])}</span>　候选 {pm['n_candidates']} 只</p>
  <table><thead><tr><th>#</th><th>名称</th><th>代码</th><th>申万二级</th><th>评级/总分</th><th>现价</th><th>当日</th>
    <th>板块R60/R10</th><th>板块60日/10日</th><th>RSI</th><th>均线</th><th>消/技/基/题/板</th></tr></thead>
  <tbody>{pick_rows(pm['picks'])}</tbody></table>
  <div class="note">等权持有；剔除 RSI&gt;70。回测提示：此路线历史超额为负，建议作为对照观察，不宜作为主仓依据。</div>
</div>

<h2>三、B 路线组合（反转：60日动量末段 + 超跌优先）</h2>
<div class="card">
  <div class="sec-hd"><h3 style="margin:0">命中层级</h3><span class="lv">{pr['level_used']}　目标规则：{pr['target_rule']}</span></div>
  <p style="font-size:13px;margin:4px 0">选中板块：<span class="badge ok">{'　'.join(pr['sectors'])}</span>　候选 {pr['n_candidates']} 只</p>
  <table><thead><tr><th>#</th><th>名称</th><th>代码</th><th>申万二级</th><th>评级/总分</th><th>现价</th><th>当日</th>
    <th>板块R60/R10</th><th>板块60日/10日</th><th>RSI</th><th>均线</th><th>消/技/基/题/板</th></tr></thead>
  <tbody>{pick_rows(pr['picks'])}</tbody></table>
  <div class="note">板块内 RSI&lt;30 优先，其次按综合评分降序；剔除 RSI&gt;70。持有期建议 20 个交易日以上（回测中 5 日超额仅 +0.78%，扣成本后薄）。</div>
</div>

<h2>四、两个指标如何并入评分模型</h2>
<div class="card">
  <p style="font-size:13.5px;margin-top:0">评分模型 <b>stock-scorer v2.40</b>：新增第五维 <b>板块动量 score_sector</b>，权重从题材维度切出，四维改五维，总量纲不变（0–20 分）。</p>
  <table><thead><tr><th>维度</th><th>权重</th><th>说明</th></tr></thead><tbody>
    <tr><td>消息面</td><td>0.20</td><td>AI 题材新闻 × 0.7 + 算法 × 0.3</td></tr>
    <tr><td>技术面</td><td>0.20</td><td>均线结构 40% + MACD/量能/RSI</td></tr>
    <tr><td>基本面</td><td>0.30</td><td>基础分 0.6 + 业绩一阶导 0.2 + 二阶导 0.2</td></tr>
    <tr><td>题材热度</td><td>0.18</td><td>AI 题材热度 + 当日涨跌 day_mod 修正</td></tr>
    <tr><td><b>板块动量</b></td><td><b>0.12</b></td><td>0.6 × 板块60日动量排名分 + 0.4 × 板块10日动量排名分</td></tr>
  </tbody></table>
  <p style="font-size:13.5px">个股同时保留两个方向的分数，实际并入总分由 <span class="mono">--mode</span> 决定：</p>
  <ul>
    <li><span class="badge">momentum</span>板块动量排名越靠前分越高（R60=1 → 5.0 分）—— 即您提出的原始思路。</li>
    <li><span class="badge ok">evidence</span>按回测实测超额映射，动量末段分高（R60&gt;70 → 3.6 分，R60=1 → 0.3 分）—— <b>默认模式</b>。</li>
  </ul>
  <p style="font-size:13px">分档映射（60日部分，共 8 档）：动量模式 5.0 / 4.2 / 3.5 / 2.8 / 2.0 / 1.3 / 0.7 / 0.3；
  证据模式 0.3 / 0.8 / 0.4 / 1.0 / 1.6 / 2.4 / 3.0 / 3.6。10日部分按同样方式 7 档映射，两者按 6:4 合成。</p>
</div>

<h2>五、回测证据</h2>
<div class="card">
  <h3 style="margin-top:0">5.1 板块 R60 分档 → 板块内个股等权后续超额</h3>
  <table><thead><tr><th>板块动量档位</th><th>5日</th><th>10日</th><th>20日</th><th>60日</th></tr></thead><tbody>{bin_rows(r60b)}</tbody></table>
  <div class="note">负单调关系明确：越强的板块，后续表现越差。</div>
  <h3>5.2 板块 R10 分档 → 后续超额</h3>
  <table><thead><tr><th>板块短期动量档位</th><th>5日</th><th>10日</th><th>20日</th><th>60日</th></tr></thead><tbody>{bin_rows(r10b)}</tbody></table>
  <h3>5.3 各选股规则对比（板块内全部个股等权；"板块层面"= 直接持有选中板块等权指数）</h3>
  <table><thead><tr><th>规则</th><th>命中天数占比</th><th>5日</th><th>10日</th><th>20日</th><th>60日</th><th>20日 t</th><th>板块层面20日</th></tr></thead>
  <tbody>{rule_rows()}</tbody></table>
  <div class="note">基准为全市场等权；交易口径 T+1 开盘买入、T+1+h 收盘卖出，剔除一字涨停无法成交样本。</div>
  <h3>5.4 过滤条件的影响</h3>
  <table><thead><tr><th>组合</th><th>5日</th><th>10日</th><th>20日</th><th>60日</th></tr></thead><tbody>{filt_rows()}</tbody></table>
  <div class="note">剔除 RSI&gt;70 明显改善（L1 路线 20 日由 -1.38% → +0.50%）；剔除均线空头排列则<b>反而变差</b>（反转路线 20 日 +0.47% → +0.11%），故不采用。</div>
  <h3>5.5 反转路线分年度稳定性（60日动量末3板块 ∩ RSI&lt;30，持有20日）</h3>
  <table><thead><tr><th>年份</th><th>有效天数</th><th>日均持仓</th><th>日均超额</th><th>年化超额</th><th>日胜率</th><th>t 值</th></tr></thead>
  <tbody>{year_rows(yr_rev)}</tbody></table>
  <div class="note">6 年中 4 年为正、2 年为负（2023 -4.3%、2025 -18.7%），说明该效应存在但不稳定，需控制仓位与持有期。</div>
  <h3>5.6 A 路线（严格规则）分年度</h3>
  <table><thead><tr><th>年份</th><th>有效天数</th><th>日均持仓</th><th>日均超额</th><th>年化超额</th><th>日胜率</th><th>t 值</th></tr></thead>
  <tbody>{year_rows(yr_strict)}</tbody></table>
</div>

<h2>六、每日执行流程（SOP）</h2>
<div class="card">
  <ol>
    <li><span class="mono">python3 fetch_top200_sina.py</span> — 取全 A 成交额 TOP200 名单</li>
    <li><span class="mono">python3 step2_stockscorer_v2.py top200_all_a.json top200_scored_&lt;日期&gt;.json</span> — 四维自动评分（约 15 分钟）</li>
    <li><span class="mono">python3 generate_ai_assessment.py &lt;scored&gt; ai_assessment.json</span> — LLM 生成盘面点评与题材（失败自动降级规则引擎）</li>
    <li><span class="mono">python3 enhance_scores.py &lt;scored&gt; ai_assessment.json</span> — 叠加 AI 层与基本面导数</li>
    <li><span class="mono">python3 sector_momentum_daily.py</span> — 抓全市场近 90 日 K 线，自建申万二级板块指数，输出 60/10 日动量与排名（约 70 秒）</li>
    <li><span class="mono">python3 apply_sector_momentum.py &lt;scored&gt; --mode evidence</span> — 并入第五维，重算总分与评级</li>
    <li><span class="mono">python3 pick_portfolio.py &lt;scored&gt; --top 10 --per-sector 3</span> — 按板块规则生成 A/B 两套组合</li>
    <li><span class="mono">python3 gen_portfolio_report.py</span> — 生成本报告</li>
  </ol>
  <p style="font-size:13px">行业映射 <span class="mono">sector/sw_map.json</span> 建议每周更新一次：<span class="mono">python3 fetch_sw_map.py</span>。</p>
</div>

<h2>七、已知局限</h2>
<div class="card">
  <ul>
    <li>股票池为当前存续股票，存在<b>幸存者偏差</b>，会抬高绝对收益水平（但本报告的结论均基于相对全市场等权的超额，偏差影响较小）。</li>
    <li>申万二级行业成分采用<b>当前快照</b>，历史回测存在轻微前视偏差。</li>
    <li>板块指数为<b>等权自建</b>（非官方申万指数），成分股过少的行业（&lt;5 只）已剔除，124/131 个行业参与计算。</li>
    <li>回测未计入滑点与冲击成本；实际交易还需扣除双边约 0.2% 的费用，故 5 日持有期的薄超额基本不可执行。</li>
    <li>「60日动量末3板块 ∩ RSI&lt;30」样本 12,494 笔，分年度波动较大，建议小仓位分批、并配合 20 日以上持有期。</li>
  </ul>
</div>

<div class="note" style="text-align:center;margin:26px 0 10px">
数据来源：腾讯前复权日线（行情）、申万行业成分（akshare）｜模型 stock-scorer v2.40｜生成时间 {__import__('datetime').datetime.now().strftime('%Y-%m-%d %H:%M')}
</div>
</div></body></html>"""

    op = args.out or os.path.join(SECTOR_DIR, f"portfolio_report_{sdate}.html")
    with open(op, 'w', encoding='utf-8') as f:
        f.write(html)
    print(f"✅ 报告已生成: {op} ({len(html)} 字符)")
    return 0


if __name__ == '__main__':
    sys.exit(main())
