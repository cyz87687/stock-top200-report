#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""生成 P0-4「混合量尺」三方案对比页（供老大裁决）

背景
----
`total = Σ min(v,DMAX)/DMAX×10×w / max(0.5, Σ可用w) × 10`
分母随缺维数量变化 → 同一截面上不同股票用不同量尺，Σw 0.75~1.00，
分数被放大 1.00~1.33 倍。实测 535 只（28.2%）分数被虚高，最高 +16.33 分。

三方案
------
  C_cur     现行：分子分母都剔除缺维（保持 P0-2 语义，但混合量尺）
  B_neutral 分母恒 Σ全部=1.0，缺维按中性分 DMAX/2 填充（量尺统一，IC 零损失）
  A_zero    分母恒 Σ全部=1.0，缺维分子贡献 0（量尺统一，但 IC 显著恶化）

输出
----
  p0_4_scale_compare.html  —— A股红涨绿跌配色，只展示结论不展示计算过程
"""

import os
import json
import statistics
from collections import Counter, defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
SCORED = os.path.join(HERE, "top200_scored_2026-10-27.json")
FW = os.path.join(HERE, "factor_weights.json")
OUT = os.path.join(HERE, "p0_4_scale_compare.html")

# 实测 IC（来自 audit_round30b/30c 与三方案对比实跑，见 reports/round30_fund_ic_2026-10-27.md）
IC = {
    "C_cur": {"h5": 0.1520, "h5_t0": 8.02, "h5_t3": 4.63, "h5_t6": 3.99,
              "h20": 0.1879, "h20_t0": 7.19, "h20_t3": 3.78, "h20_t6": 2.98},
    "B_neutral": {"h5": 0.1509, "h5_t0": 7.86, "h5_t3": 4.53, "h5_t6": 3.92,
                  "h20": 0.1889, "h20_t0": 7.14, "h20_t3": 3.76, "h20_t6": 2.96,
                  "pair_h5_t": -0.53, "pair_h20_t": 1.06},
    "A_zero": {"h5": 0.0926, "h5_t0": 3.92, "h5_t3": 2.21, "h5_t6": 1.89,
               "h20": 0.1071, "h20_t0": 3.31, "h20_t3": 1.74, "h20_t6": 1.37,
               "pair_h5_t": -5.76, "pair_h20_t": -8.94},
}
# 同质样本（仅入池，缺维模式一致）—— 排除入池偏误后的对照
IC_HOMO = {
    "C_cur":     {"h5": 0.1799, "h5_t0": 12.69, "h20": 0.2010, "h20_t0": 10.24},
    "B_neutral": {"h5": 0.1796, "h5_t0": 12.97, "h20": 0.2005, "h20_t0": 10.50,
                  "pair_h5_t": -0.21, "pair_h20_t": -0.33},
    "A_zero":    {"h5": 0.1756, "h5_t0": 12.45, "h20": 0.1958, "h20_t0": 9.89,
                  "pair_h5_t": -1.54, "pair_h20_t": -2.40},
}


def is_num(x):
    return isinstance(x, (int, float)) and not isinstance(x, bool) and x == x


def main():
    scored = json.load(open(SCORED, encoding="utf-8"))
    rows = scored["results"]
    date = scored.get("date", "")
    fwj = json.load(open(FW, encoding="utf-8"))
    W = {k: float(v or 0) for k, v in fwj["weights"].items() if (v or 0) > 0}
    DMAX = {k: float(v) for k, v in fwj["dim_max"].items()}
    SUM_ALL = sum(W.values())

    def score(r, mode):
        num = 0.0
        den = 0.0
        for k, w in W.items():
            v = r.get(k)
            mx = DMAX.get(k) or 10.0
            if is_num(v):
                num += min(float(v), mx) / mx * 10.0 * w
                den += w
            elif mode == "B_neutral":
                num += (mx / 2.0) / mx * 10.0 * w
            # A_zero: 分子贡献 0；C_cur: 分子分母都不含
        if mode == "C_cur":
            return (num / max(0.5, den) * 10.0) if den > 0 else None
        return (num / SUM_ALL * 10.0) if SUM_ALL > 0 else None

    recs = []
    for r in rows:
        sw = sum(w for k, w in W.items() if is_num(r.get(k)))
        miss = [k for k in W if not is_num(r.get(k))]
        c = score(r, "C_cur")
        b = score(r, "B_neutral")
        a = score(r, "A_zero")
        if c is None or b is None or a is None:
            continue
        recs.append({
            "code": r.get("code"), "name": r.get("name"),
            "sector": r.get("sector") or r.get("sector_l2") or "",
            "price": r.get("price"), "pct": r.get("pct_chg"),
            "sw": sw, "nmiss": len(miss), "miss": miss,
            "fund": r.get("score_fund"),
            "C": c, "B": b, "A": a,
            "dB": b - c, "dA": a - c,
        })

    # 排名
    def rank(pairs):
        s = sorted(pairs, key=lambda x: (-x[1], x[0]["code"]))
        return {x[0]["code"]: i + 1 for i, x in enumerate(s)}

    rkC = rank([(x, x["C"]) for x in recs])
    rkB = rank([(x, x["B"]) for x in recs])
    rkA = rank([(x, x["A"]) for x in recs])
    for x in recs:
        x["rkC"], x["rkB"], x["rkA"] = rkC[x["code"]], rkB[x["code"]], rkA[x["code"]]

    # 统计
    dA = sorted(x["dA"] for x in recs)
    dB = sorted(x["dB"] for x in recs)
    by_sw = defaultdict(list)
    for x in recs:
        by_sw[round(x["sw"], 4)].append(x)

    top50C = set(x["code"] for x in sorted(recs, key=lambda t: t["rkC"])[:50])
    top50B = set(x["code"] for x in sorted(recs, key=lambda t: t["rkB"])[:50])
    top50A = set(x["code"] for x in sorted(recs, key=lambda t: t["rkA"])[:50])

    # 评级迁移（用现行门槛 85/65/45/25）
    def rate(v):
        return "S" if v >= 85 else "A" if v >= 65 else "B" if v >= 45 else "C" if v >= 25 else "D"
    migA = Counter()
    migB = Counter()
    for x in recs:
        migA[(rate(x["C"]), rate(x["A"]))] += 1
        migB[(rate(x["C"]), rate(x["B"]))] += 1
    chgA = sum(1 for x in recs if rate(x["C"]) != rate(x["A"]))
    chgB = sum(1 for x in recs if rate(x["C"]) != rate(x["B"]))

    # ---------- 受影响清单（按 |ΔA| 降序，即最"被虚高"的）
    affected = sorted(recs, key=lambda x: x["dA"])[:120]

    def fmt(v, d=2):
        return f"{v:.{d}f}" if is_num(v) else "—"

    def pctcls(v):
        if not is_num(v):
            return ""
        return "up" if v > 0 else ("down" if v < 0 else "")

    # ---------- 构建 HTML
    H = []
    A = H.append
    A("""<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>P0-4 混合量尺 · 三方案对比（待裁决）</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,BlinkMacSystemFont,"PingFang SC","Microsoft YaHei",sans-serif;
background:#f5f6f8;color:#1a1d24;line-height:1.6;font-size:14px}
.wrap{max-width:1280px;margin:0 auto;padding:24px 20px 64px}
h1{font-size:24px;font-weight:700;margin-bottom:6px}
.sub{color:#6b7280;font-size:13px;margin-bottom:24px}
.card{background:#fff;border-radius:12px;padding:20px 22px;margin-bottom:18px;
box-shadow:0 1px 3px rgba(0,0,0,.06),0 1px 2px rgba(0,0,0,.04)}
.card h2{font-size:16px;font-weight:700;margin-bottom:14px;padding-bottom:10px;
border-bottom:1px solid #eef0f3;display:flex;align-items:center;gap:8px}
.badge{display:inline-block;font-size:11px;font-weight:600;padding:2px 8px;border-radius:5px}
.b-cur{background:#eef2ff;color:#4f46e5}.b-b{background:#ecfdf5;color:#059669}
.b-a{background:#fef2f2;color:#dc2626}.b-warn{background:#fffbeb;color:#b45309}
.alert{background:#fffbeb;border-left:4px solid #f59e0b;padding:12px 16px;
border-radius:0 8px 8px 0;margin-bottom:18px;font-size:13px}
.alert b{color:#b45309}
table{width:100%;border-collapse:collapse;font-size:13px}
th{background:#fafbfc;font-weight:600;text-align:left;padding:9px 10px;
border-bottom:2px solid #eef0f3;white-space:nowrap;font-size:12px;color:#4b5563}
td{padding:9px 10px;border-bottom:1px solid #f4f5f7;vertical-align:middle}
tr:hover td{background:#fafbfc}
.up{color:#dc2626;font-weight:600}.down{color:#059669;font-weight:600}
.mono{font-variant-numeric:tabular-nums;font-feature-settings:"tnum"}
.num{text-align:right}
.opt{border:2px solid #eef0f3;border-radius:10px;padding:16px 18px;margin-bottom:14px}
.opt.best{border-color:#10b981;background:#f0fdf9}
.opt.bad{border-color:#fca5a5;background:#fff7f7}
.opt.mid{border-color:#a5b4fc;background:#f8f9ff}
.opt-h{display:flex;align-items:center;gap:10px;margin-bottom:10px}
.opt-h .t{font-size:15px;font-weight:700}
.pros,.cons{font-size:13px;margin-top:8px}
.pros li,.cons li{margin-left:18px;margin-bottom:4px}
.pros li::marker{color:#10b981}.cons li::marker{color:#ef4444}
.kpi{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin:14px 0}
.kpi .k{background:#fafbfc;border-radius:8px;padding:10px 12px}
.kpi .k .lb{font-size:11px;color:#6b7280;margin-bottom:3px}
.kpi .k .vl{font-size:18px;font-weight:700;font-variant-numeric:tabular-nums}
.scroll{max-height:560px;overflow:auto;border-radius:8px;border:1px solid #eef0f3}
.scroll table th{position:sticky;top:0;z-index:2}
.miss-tag{display:inline-block;font-size:10px;padding:1px 5px;border-radius:4px;
background:#f3f4f6;color:#6b7280;margin-right:3px}
.tag-new{background:#fef2f2;color:#dc2626;font-size:10px;font-weight:600;padding:1px 5px;border-radius:4px}
.tag-out{background:#f0fdf4;color:#059669;font-size:10px;font-weight:600;padding:1px 5px;border-radius:4px}
.foot{color:#9ca3af;font-size:12px;margin-top:24px;padding-top:16px;border-top:1px solid #eef0f3}
</style></head><body><div class="wrap">""")

    A(f'<h1>P0-4 混合量尺 · 三方案对比</h1>')
    A(f'<div class="sub">数据：{date} · 池内 {len(recs)} 只 · 权重来源 factor_weights.json（Σ={SUM_ALL:.2f}） · '
      f'<b>本页仅本地预览，未推送线上</b></div>')

    # ---- 问题说明
    A('<div class="alert">')
    A('<b>问题</b>：现行公式 <code>total = Σ min(v,DMAX)/DMAX×10×w / max(0.5, Σ可用权重) × 10</code> '
      '的分母随「缺了几维」变化 → 同一页面上不同股票用不同尺子。')
    A(f'<br>实测：Σ可用权重从 0.75 到 1.00，分数被归一化放大 <b>1.00~1.33 倍</b>。'
      f'缺维越多，分数被抬得越高 —— 即<b>数据缺失反而占便宜</b>。')
    A(f'<br>受影响：{sum(1 for x in recs if x["dA"]<-0.01)} 只（{100*sum(1 for x in recs if x["dA"]<-0.01)/len(recs):.1f}%）'
      f'在方案 A 下分数下降，最高 <b>{dA[0]:+.2f} 分</b>。')
    A('</div>')

    # ---- 三方案卡片
    A('<div class="card"><h2>三方案对照</h2>')

    # C_cur
    A('<div class="opt mid">')
    A('<div class="opt-h"><span class="badge b-cur">C_cur</span>'
      '<span class="t">现行：分子分母都剔除缺维</span>'
      '<span class="badge b-warn">保持 P0-2 语义</span></div>')
    A('<div style="font-size:13px;color:#4b5563">缺维不计入分子，也不计入分母 → 分数按「已用权重」归一化。'
      '优点是无估值就是不计分（P0-2 刚修的语义保持），代价是每个股票的分母不同。</div>')
    A('<div class="kpi">')
    A(f'<div class="k"><div class="lb">h5 IC</div><div class="vl">{IC["C_cur"]["h5"]:+.4f}</div>'
      f'<div class="lb">t0={IC["C_cur"]["h5_t0"]:.2f} / t3={IC["C_cur"]["h5_t3"]:.2f} / t6={IC["C_cur"]["h5_t6"]:.2f}</div></div>')
    A(f'<div class="k"><div class="lb">h20 IC</div><div class="vl">{IC["C_cur"]["h20"]:+.4f}</div>'
      f'<div class="lb">t0={IC["C_cur"]["h20_t0"]:.2f} / t3={IC["C_cur"]["h20_t3"]:.2f} / t6={IC["C_cur"]["h20_t6"]:.2f}</div></div>')
    A(f'<div class="k"><div class="lb">量尺统一性</div><div class="vl" style="color:#dc2626">✗ 不统一</div>'
      f'<div class="lb">Σw 0.75~1.00（放大 1.33x）</div></div>')
    A(f'<div class="k"><div class="lb">绝对分数可比</div><div class="vl" style="color:#dc2626">✗ 否</div>'
      f'<div class="lb">缺维者被系统性抬高</div></div>')
    A('</div>')
    A('<ul class="pros"><li>完整保留 P0-2「无估值=不计分」语义，不会把 266 只无估值股重新赋予分数</li>'
      '<li>IC 表现最好（h5 +0.1520 / h20 +0.1879）</li></ul>')
    A('<ul class="cons"><li>同一页面上不同股票用不同尺子，绝对分数不可横向比较</li>'
      '<li>缺维越多分数越高，激励「数据不全」</li></ul>')
    A('</div>')

    # B_neutral
    A('<div class="opt best">')
    A('<div class="opt-h"><span class="badge b-b">B_neutral</span>'
      '<span class="t">分母恒定 Σ全部=1.0，缺维填中性分 DMAX/2</span>'
      '<span class="badge b-b">推荐</span></div>')
    A('<div style="font-size:13px;color:#4b5563">分母永远是 1.0（全部生效维权重和）。'
      '某维缺值时按该维量程中点（如 10 分制填 5.0、5 分制填 2.5）计入 → 缺维既不奖励也不惩罚。</div>')
    A('<div class="kpi">')
    A(f'<div class="k"><div class="lb">h5 IC</div><div class="vl">{IC["B_neutral"]["h5"]:+.4f}</div>'
      f'<div class="lb">配对 vs 现行 t={IC["B_neutral"]["pair_h5_t"]:+.2f}（不可区分）</div></div>')
    A(f'<div class="k"><div class="lb">h20 IC</div><div class="vl">{IC["B_neutral"]["h20"]:+.4f}</div>'
      f'<div class="lb">配对 vs 现行 t={IC["B_neutral"]["pair_h20_t"]:+.2f}（不可区分）</div></div>')
    A(f'<div class="k"><div class="lb">量尺统一性</div><div class="vl" style="color:#059669">✓ 完全统一</div>'
      f'<div class="lb">Σw 恒 = 1.00</div></div>')
    A(f'<div class="k"><div class="lb">绝对分数可比</div><div class="vl" style="color:#059669">✓ 是</div>'
      f'<div class="lb">同尺子，可直接横比</div></div>')
    A('</div>')
    A('<ul class="pros"><li>量尺完全统一，绝对分数可横向比较</li>'
      f'<li>IC 零损失：h5 配对 t={IC["B_neutral"]["pair_h5_t"]:+.2f}、h20 配对 t={IC["B_neutral"]["pair_h20_t"]:+.2f}，均远低于阈值 3.08</li>'
      '<li>缺维既不奖励也不惩罚，语义中立</li></ul>')
    A('<ul class="cons"><li><b>部分抵消 P0-2</b>：266 只无估值股票会重新拿到 2.5 分中性分（而非 None）</li>'
      '<li>需要页面同步增加「该维为中性填充值」的标注，否则用户看不出这是填的</li></ul>')
    A('</div>')

    # A_zero
    A('<div class="opt bad">')
    A('<div class="opt-h"><span class="badge b-a">A_zero</span>'
      '<span class="t">分母恒定 Σ全部=1.0，缺维贡献 0</span>'
      '<span class="badge b-a">不推荐</span></div>')
    A('<div style="font-size:13px;color:#4b5563">分母同样是 1.0，但缺维在分子上贡献 0 分 → '
      '等价于「该维得 0 分」。量尺最严格，但对数据缺失施加了实质惩罚。</div>')
    A('<div class="kpi">')
    A(f'<div class="k"><div class="lb">h5 IC</div><div class="vl" style="color:#dc2626">{IC["A_zero"]["h5"]:+.4f}</div>'
      f'<div class="lb">配对 vs 现行 t={IC["A_zero"]["pair_h5_t"]:+.2f}（显著恶化）</div></div>')
    A(f'<div class="k"><div class="lb">h20 IC</div><div class="vl" style="color:#dc2626">{IC["A_zero"]["h20"]:+.4f}</div>'
      f'<div class="lb">配对 vs 现行 t={IC["A_zero"]["pair_h20_t"]:+.2f}（显著恶化）</div></div>')
    A(f'<div class="k"><div class="lb">量尺统一性</div><div class="vl" style="color:#059669">✓ 完全统一</div>'
      f'<div class="lb">Σw 恒 = 1.00</div></div>')
    A(f'<div class="k"><div class="lb">分数下降面</div><div class="vl" style="color:#dc2626">{sum(1 for x in recs if x["dA"]<-0.01)} 只</div>'
      f'<div class="lb">最高 {dA[0]:+.2f} 分</div></div>')
    A('</div>')
    A('<ul class="pros"><li>量尺最严格统一，语义最直白（缺维=没贡献）</li></ul>')
    A('<ul class="cons"><li><b>IC 显著恶化</b>：h5 t=−5.76、h20 t=−8.94，远超阈值</li>'
      '<li>等于回到 P0-3 刚修掉的「缺维当 0 分」行为</li>'
      '<li>惩罚的是「数据没拉到」而非「公司质地差」，与基本面无关</li></ul>')
    A('</div>')
    A('</div>')

    # ---- 同质样本对照（排除入池偏误）
    A('<div class="card"><h2>同质样本对照（排除入池偏误）<span class="badge b-warn">关键校验</span></h2>')
    A('<div style="font-size:13px;color:#4b5563;margin-bottom:14px">'
      '全域样本里「有值/缺值」本身就编码了「是否曾入池」，而入池指示变量 IC=−0.23（t−14.5）。'
      '只看全域会量到选择偏误。下表限定「当日在历史池内」的同质样本重算：</div>')
    A('<table><thead><tr><th>方案</th><th class="num">h5 IC</th><th class="num">h5 t0</th>'
      '<th class="num">h20 IC</th><th class="num">h20 t0</th><th>判读</th></tr></thead><tbody>')
    for k, lbl in (("C_cur", "C_cur 现行"), ("B_neutral", "B_neutral 中性填充"), ("A_zero", "A_zero 缺维0分")):
        h = IC_HOMO[k]
        note = "基准" if k == "C_cur" else (
            f'配对 t={h["pair_h5_t"]:+.2f} / {h["pair_h20_t"]:+.2f} → '
            + ("不可区分" if abs(h["pair_h5_t"]) < 3.08 and abs(h["pair_h20_t"]) < 3.08 else "有差异"))
        A(f'<tr><td><b>{lbl}</b></td><td class="num mono">{h["h5"]:+.4f}</td>'
          f'<td class="num mono">{h["h5_t0"]:.2f}</td><td class="num mono">{h["h20"]:+.4f}</td>'
          f'<td class="num mono">{h["h20_t0"]:.2f}</td><td style="font-size:12px;color:#6b7280">{note}</td></tr>')
    A('</tbody></table>')
    A('<div style="font-size:12px;color:#6b7280;margin-top:10px">'
      '结论：同质样本里三方案几乎重合 → <b>混合量尺主要影响「绝对分数可比性」，而不是排序能力（rank IC）</b>。'
      '所以这是个「分数是否诚实」的问题，不是「模型是否有效」的问题。</div>')
    A('</div>')

    # ---- 按 Σw 分组的影响
    A('<div class="card"><h2>受影响分布（按缺维数量分组）</h2>')
    A('<table><thead><tr><th>Σ可用权重</th><th class="num">股票数</th><th class="num">占比</th>'
      '<th class="num">现行放大幅度</th><th class="num">A零分后 Δ 均值</th>'
      '<th class="num">Δ 范围</th><th>缺失维度</th></tr></thead><tbody>')
    for sw in sorted(by_sw):
        v = by_sw[sw]
        da = [x["dA"] for x in v]
        missc = Counter()
        for x in v:
            for m in x["miss"]:
                missc[m] += 1
        miss_txt = "、".join(f'{m.replace("score_","")}×{c}' for m, c in missc.most_common(3))
        A(f'<tr><td class="mono"><b>{sw:.4f}</b></td><td class="num mono">{len(v)}</td>'
          f'<td class="num mono">{100*len(v)/len(recs):.1f}%</td>'
          f'<td class="num mono">{1.0/max(0.5,sw):.3f}x</td>'
          f'<td class="num mono up">{sum(da)/len(da):+.2f}</td>'
          f'<td class="num mono" style="font-size:12px">[{min(da):+.2f}, {max(da):+.2f}]</td>'
          f'<td style="font-size:11px;color:#6b7280">{miss_txt}</td></tr>')
    A('</tbody></table>')
    A('<div style="font-size:12px;color:#b45309;margin-top:10px">'
      '⚠️ 单调性明确：Σw 越小（缺维越多）→ 现行放大幅度越大 → 统一分母后跌幅越大。'
      '这就是「数据缺失反而占便宜」的直接证据。</div>')
    A('</div>')

    # ---- Top50 差异
    A('<div class="card"><h2>Top50 名单差异</h2>')
    A('<div class="kpi">')
    A(f'<div class="k"><div class="lb">B_neutral vs 现行</div>'
      f'<div class="vl">{len(top50B & top50C)}/50 保留</div>'
      f'<div class="lb">替换 {len(top50B-top50C)} 只</div></div>')
    A(f'<div class="k"><div class="lb">A_zero vs 现行</div>'
      f'<div class="vl">{len(top50A & top50C)}/50 保留</div>'
      f'<div class="lb">替换 {len(top50A-top50C)} 只</div></div>')
    A('</div>')
    nm = {x["code"]: x["name"] for x in recs}
    A('<div style="display:grid;grid-template-columns:1fr 1fr;gap:18px;margin-top:6px">')
    for tag, s, cls in (("B_neutral", top50B, "b-b"), ("A_zero", top50A, "b-a")):
        A('<div>')
        A(f'<div style="font-size:13px;font-weight:600;margin-bottom:8px">'
          f'<span class="badge {cls}">{tag}</span> 相对现行 Top50 的变化</div>')
        A('<div style="font-size:12px;color:#dc2626;margin-bottom:4px">新进 '
          f'{len(s-top50C)} 只：</div>')
        A('<div style="font-size:12px;color:#4b5563;margin-bottom:10px;line-height:1.8">'
          + "、".join(nm[c] for c in sorted(s - top50C, key=lambda c: rkB.get(c, 999))[:20]) + '</div>')
        A('<div style="font-size:12px;color:#059669;margin-bottom:4px">被换出 '
          f'{len(top50C-s)} 只：</div>')
        A('<div style="font-size:12px;color:#4b5563;line-height:1.8">'
          + "、".join(nm[c] for c in sorted(top50C - s, key=lambda c: rkC.get(c, 999))[:20]) + '</div>')
        A('</div>')
    A('</div></div>')

    # ---- 明细表
    A(f'<div class="card"><h2>受影响明细（跌幅最大的 120 只）</h2>')
    A('<div style="font-size:12px;color:#6b7280;margin-bottom:12px">'
      '排序 = 方案 A 下的分数跌幅降序，即「现行分数被虚高最多」的股票。</div>')
    A('<div class="scroll"><table><thead><tr>'
      '<th>名称</th><th>代码</th><th class="num">现价</th><th class="num">涨跌</th>'
      '<th class="num">Σw</th><th>缺失维度</th>'
      '<th class="num">现行分</th><th class="num">B中性</th><th class="num">A零分</th>'
      '<th class="num">Δ(B)</th><th class="num">Δ(A)</th>'
      '<th class="num">排名 现行→B→A</th></tr></thead><tbody>')
    for x in affected:
        mt = "".join(f'<span class="miss-tag">{m.replace("score_","")}</span>' for m in x["miss"])
        if not mt:
            mt = '<span style="color:#9ca3af;font-size:11px">—</span>'
        A(f'<tr><td><b>{x["name"]}</b></td><td class="mono" style="font-size:12px;color:#6b7280">{x["code"]}</td>'
          f'<td class="num mono">{fmt(x["price"])}</td>'
          f'<td class="num mono {pctcls(x["pct"])}">{fmt(x["pct"],1)}%</td>'
          f'<td class="num mono">{x["sw"]:.3f}</td><td>{mt}</td>'
          f'<td class="num mono"><b>{x["C"]:.2f}</b></td>'
          f'<td class="num mono">{x["B"]:.2f}</td><td class="num mono">{x["A"]:.2f}</td>'
          f'<td class="num mono {"up" if x["dB"]>0.01 else ("down" if x["dB"]<-0.01 else "")}">{x["dB"]:+.2f}</td>'
          f'<td class="num mono down">{x["dA"]:+.2f}</td>'
          f'<td class="num mono" style="font-size:12px">{x["rkC"]}→{x["rkB"]}→{x["rkA"]}</td></tr>')
    A('</tbody></table></div></div>')

    # ---- 评级迁移
    A('<div class="card"><h2>评级迁移（门槛 S≥85 / A≥65 / B≥45 / C≥25）</h2>')
    A('<table><thead><tr><th>方案</th><th class="num">评级变化只数</th><th class="num">占比</th>'
      '<th>主要迁移</th></tr></thead><tbody>')
    for tag, mig, chg in (("B_neutral", migB, chgB), ("A_zero", migA, chgA)):
        moves = [(k, v) for k, v in mig.items() if k[0] != k[1]]
        moves.sort(key=lambda t: -t[1])
        txt = "、".join(f"{a}→{b}：{v}只" for (a, b), v in moves[:5])
        A(f'<tr><td><b>{tag}</b></td><td class="num mono">{chg}</td>'
          f'<td class="num mono">{100*chg/len(recs):.1f}%</td>'
          f'<td style="font-size:12px;color:#4b5563">{txt}</td></tr>')
    A('</tbody></table></div>')

    # ---- 建议
    A('<div class="card"><h2>建议</h2>')
    A('<div style="font-size:13.5px;line-height:1.9">')
    A('<p><b>若目标是「分数诚实、可横比」→ 选 B_neutral。</b>IC 零损失，量尺统一，'
      '缺维中立。代价是 P0-2 需要配套改造：把「无估值 → None」改成'
      '「无估值 → 中性分 + 页面标注『估值维为中性填充』」，而不是历史意义上的「不计分」。</p>')
    A('<p style="margin-top:12px"><b>若目标是「严格不奖励数据缺失」→ 维持 C_cur。</b>'
      '但需在页面明示「本页分数为各股自身可用维度的加权平均，缺维者不可与全维者直接比绝对分」。</p>')
    A('<p style="margin-top:12px;color:#b45309"><b>A_zero 不建议</b>：IC 显著恶化（h20 t=−8.94），'
      '且把「数据没拉到」当成「公司差」来罚，逻辑不成立。</p>')
    A('</div></div>')

    A(f'<div class="foot">生成时间 2026-10-27 · 数据源 top200_scored_2026-10-27.json · '
      f'IC 数据来自 audit_round30/30b/30c · 本页仅本地文件，未推送线上、未提交 git</div>')
    A('</div></body></html>')

    html = "".join(H)
    with open(OUT, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"已生成 {OUT}")
    print(f"  字符数 {len(html):,}")
    print(f"  股票数 {len(recs)}")
    print(f"  A_zero 下分数下降 {sum(1 for x in recs if x['dA']<-0.01)} 只，最高 {dA[0]:+.2f}")
    print(f"  B_neutral 分数变化范围 [{dB[0]:+.2f}, {dB[-1]:+.2f}]")
    print(f"  Top50: B保留 {len(top50B&top50C)}/50 · A保留 {len(top50A&top50C)}/50")
    print(f"  评级变化: B {chgB} 只 ({100*chgB/len(recs):.1f}%) · A {chgA} 只 ({100*chgA/len(recs):.1f}%)")


if __name__ == "__main__":
    main()
