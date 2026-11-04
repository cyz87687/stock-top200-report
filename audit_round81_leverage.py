#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
audit_round81_leverage.py —— β 敞口调整：把「低 beta」这一发现转成可执行方案

═══ 前置发现（round78/80）═══

  · 真实组合对池等权 **β = 0.321**，随机组合 β 中位 **1.003**
    ⇒ 低 β 是**模型选股的结果**（真实属性，可交易），不是池口径失真
  · 超额年化 +8.35%，σ_超额 25.3%，超额方差分解：
        β 失配贡献 **58.5%**，特质残差贡献 41.5%
  · 真实策略在 200 个严格匹配随机组合中 IR 分位 **66.0%**（微弱价值）

═══ 本脚本要回答的问题 ═══

既然组合是「低 β + 正 alpha」，那么**主动调整 β 敞口**应能同时改善 IR：

  超额 = (s·β − 1)·池 + s·残差
  ⇒ s = 1/β 时 β 失配项归零，σ_超额 最小

数学预测（用 round80 实测的分解参数）：
  s=1.0 (现状)  β=0.32  σ_超额 25.3%  IR 0.32
  s=1.5         β=0.48  σ_超额 29.2%  IR ≈0.73
  s=2.0         β=0.64  σ_超额 34.4%  IR ≈0.99

⚠️ 但这是**线性外推**，且有两个致命前提必须实测验证：
  P1 加杠杆后 IR 真的提升？（残差与 beta 项在样本内可能不是常数）
  P2 加杠杆后**超额 MDD 同步放大** → 目标③会更差，而不是更好。
     若 P2 成立，则「调 β」只能改善目标②，恶化目标③ → 仍是取舍不是解。

═══ 关键设计 ═══

杠杆方式：**不放杠杆借钱、不改变资金量**，而是把**选股范围扩大**
（TopN 从 20 → 40/60），使组合的池敞口自然接近 1。
这与加杠杆数学等价（都是提高 s），但**无融资风险**，是纯选股问题。

⇒ 这才是可落地的方案：**不是"加杠杆"，是"放宽选股集中度来匹配池 beta"**。

同时测一组「反向」：进一步收紧集中度（TopN=10/15）会怎样。

用法：python audit_round81_leverage.py
"""
import os
import sys
import json
import math
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from audit_round67_backtest_engine import (  # noqa
    composite_all, load_prices, _px, COST_ROUND)

PANEL = os.path.join(HERE, "data", "panel_round66.json")
WEIGHTS = os.path.join(HERE, "factor_weights.json")
OUT = os.path.join(HERE, "audit_round81.json")


def build_scores(panel, wmap, dim_max):
    out = {}
    for d, rows in panel["rows"].items():
        arr = []
        for c, rec in rows.items():
            s, uw = composite_all(rec, wmap, dim_max)
            if s is not None:
                arr.append((c, s))
        arr.sort(key=lambda x: (-x[1], x[0]))
        out[d] = arr
    return out


def _cap_weight(w, cap):
    w = w / w.sum()
    for _ in range(50):
        over = w > cap
        if not over.any():
            break
        ex = (w[over] - cap).sum()
        w[over] = cap
        free = ~over
        if free.sum() == 0:
            break
        w[free] += ex * w[free] / w[free].sum()
    return w / w.sum()


def run(panel, px, scores, topn, hold, cap, ret_only=False):
    dates = panel["dates"]
    rebal = [(dates[i], dates[i + 1], i) for i in range(0, len(dates) - 1, hold)]
    periods, prev_w = [], {}
    for idx, (d_sig, d_exec, i0) in enumerate(rebal):
        d_next = rebal[idx + 1][1] if idx + 1 < len(rebal) else None
        picks = scores.get(d_sig) or []
        sel = [(c, s) for c, s in picks if _px(px, c, d_exec) is not None][:topn]
        if len(sel) < max(5, topn // 4):
            prev_w = {}
            continue
        raw = np.array([max(1e-6, s) for _, s in sel])
        w = _cap_weight(raw, cap)
        wmap = {sel[i][0]: float(w[i]) for i in range(len(sel))}
        allc = set(wmap) | set(prev_w)
        turnover = sum(abs(wmap.get(c, 0.0) - prev_w.get(c, 0.0)) for c in allc) / 2.0
        rs, ws = [], []
        for c, _s in sel:
            p0 = _px(px, c, d_exec)
            p1 = _px(px, c, d_next) if d_next else None
            if p1 is None:
                sub = px.get(c) or {}
                cand = sorted(dd for dd in sub if d_exec <= dd <= (d_next or dates[-1]))
                if cand:
                    b = sub[cand[-1]]
                    p1 = float(b[0]) if isinstance(b, (list, tuple)) else None
            if p0 and p1 and p0 > 0:
                rs.append(p1 / p0 - 1.0)
                ws.append(wmap[c])
        if not rs:
            prev_w = wmap
            continue
        sw = sum(ws)
        port = sum(r * ww for r, ww in zip(rs, ws)) / sw if sw > 0 else 0.0
        net = port - turnover * COST_ROUND
        prs = []
        for c, _s, _u in [(c, s, 0) for c, s in picks]:
            p0 = _px(px, c, d_exec)
            p1 = _px(px, c, d_next) if d_next else None
            if p0 and p1 and p0 > 0:
                prs.append(p1 / p0 - 1.0)
        periods.append({"net": net, "port": port, "turnover": turnover,
                        "pool": (sum(prs) / len(prs)) if prs else None})
        prev_w = wmap
    return periods


def met(periods, hold):
    ann = 244.0 / hold
    net = np.array([p["net"] for p in periods])
    pool = np.array([p["pool"] if p["pool"] is not None else np.nan for p in periods])
    ok = ~np.isnan(pool)
    net, pool = net[ok], pool[ok]
    ex = net - pool
    navx = np.cumprod(1.0 + ex)
    peak = np.maximum.accumulate(navx)
    navp = np.cumprod(1.0 + net)
    peakp = np.maximum.accumulate(navp)
    b = float(np.cov(net, pool, ddof=1)[0, 1] / pool.var(ddof=1)) if pool.var(ddof=1) > 1e-12 else None
    resid = net - (net.mean() + (b or 0) * (pool - pool.mean()))
    return {
        "n": len(net),
        "ann": float((1 + net.mean()) ** ann - 1),
        "excess_ann": float((1 + ex.mean()) ** ann - 1),
        "ir": float(ex.mean() / ex.std(ddof=1) * math.sqrt(ann)) if ex.std(ddof=1) > 1e-12 else None,
        "sigma_excess_ann": float(ex.std(ddof=1) * math.sqrt(ann)),
        "excess_mdd": float((navx / peak - 1.0).min()),
        "port_mdd": float((navp / peakp - 1.0).min()),
        "beta": b,
        "resid_sigma_ann": float(resid.std(ddof=1) * math.sqrt(ann)),
        "turnover": float(np.mean([p["turnover"] for p in periods])),
        "resid_alpha_ann": float(resid.mean() * ann),
    }


def main():
    print("加载 ...")
    panel = json.load(open(PANEL, encoding="utf-8"))
    px = load_prices()
    wc = json.load(open(WEIGHTS, encoding="utf-8"))
    wmap = {k: float(v) for k, v in wc["weights"].items()}
    dim_max = {k: float(v) for k, v in wc["dim_max"].items()}
    scores = build_scores(panel, wmap, dim_max)
    print(f"  {len(panel['dates'])} 截面，权重 {wc['version']}")

    print("\n" + "═" * 104)
    print("L1  用「放宽集中度」提升 β 敞口（等价于提高 s，但无融资风险）")
    print("═" * 104)
    print(f"{'方案':26s}{'β':>7s}{'σ超额':>9s}{'σ残差':>9s}{'IR':>8s}"
          f"{'超额MDD':>10s}{'组合MDD':>10s}{'年化':>9s}{'超额年化':>10s}")
    print("-" * 104)
    res = {}
    for hold in (10, 20):
        for topn in (10, 15, 20, 30, 40, 60, 80, 100):
            nm = f"top{topn}_h{hold}"
            pers = run(panel, px, scores, topn, hold, 0.08)
            if len(pers) < 8:
                continue
            m = met(pers, hold)
            res[nm] = m
            print(f"{nm:26s}{(m['beta'] or 0):>7.3f}{m['sigma_excess_ann']*100:>8.1f}%"
                  f"{m['resid_sigma_ann']*100:>8.1f}%{(m['ir'] or 0):>+8.2f}"
                  f"{m['excess_mdd']*100:>+9.2f}%{m['port_mdd']*100:>+9.2f}%"
                  f"{m['ann']*100:>+8.2f}%{m['excess_ann']*100:>+9.2f}%")

    print("\n" + "═" * 104)
    print("L2  关键检验：提高 β 后，目标②与目标③是同时改善还是此消彼长？")
    print("═" * 104)
    base = res.get("top20_h10")
    if base:
        print(f"  基准 top20_h10：β={base['beta']:.3f}  IR={base['ir']:+.2f}  "
              f"超额MDD={base['excess_mdd']*100:+.2f}%  超额年化={base['excess_ann']*100:+.2f}%")
        print()
        print(f"{'方案':16s}{'ΔIR':>9s}{'Δ超额MDD':>11s}{'Δ超额年化':>11s}{'判定':>28s}")
        print("-" * 80)
        for nm, m in res.items():
            if not nm.endswith("h10") or nm == "top20_h10":
                continue
            d_ir = (m["ir"] or 0) - base["ir"]
            d_mdd = m["excess_mdd"] - base["excess_mdd"]
            d_ex = m["excess_ann"] - base["excess_ann"]
            if d_ir > 0.05 and d_mdd > -0.02:
                judge = "✅ 两目标同时改善"
            elif d_ir > 0.05 and d_mdd <= -0.02:
                judge = "⚠️ 夏普↑但回撤↓（取舍）"
            else:
                judge = "— 无改善"
            print(f"{nm:16s}{d_ir:>+9.3f}{d_mdd*100:>+10.2f}pp{d_ex*100:>+10.2f}pp{judge:>28s}")

    print("\n" + "═" * 104)
    print("L3  与 F1 可行域对照（MDD≤10% 且 IR≥0.5 的充要条件）")
    print("═" * 104)
    r77 = json.load(open(os.path.join(HERE, "audit_round77.json"), encoding="utf-8"))
    # ⚠️ round77 的数据都放在 meta 下（json.dump 时的层级）→ 取 meta
    feas = r77.get("feasibility") or r77["meta"]["feasibility"]
    print("  σ_ann → P(超额MDD≤10%) 厚尾假设 / IR=0.5 所需超额年化")
    for sa in ("0.05", "0.08", "0.1", "0.12", "0.15", "0.2", "0.25"):
        v = feas.get(sa)
        if not v:
            continue
        print(f"    σ_ann={float(sa)*100:>4.0f}%  P={v['p_ok_fat']*100:>5.1f}%  "
              f"需超额年化≥{v['need_excess_ann']*100:>5.2f}%")
    print()
    # 找出最优 IR 方案，看它离可行域多远
    best_ir = max(res.items(), key=lambda kv: (kv[1]["ir"] or -9))
    nm, m = best_ir
    print(f"  当前最优 IR 方案：{nm}  IR={m['ir']:+.2f}  "
          f"σ_超额={m['sigma_excess_ann']*100:.1f}%  超额MDD={m['excess_mdd']*100:+.2f}%")
    sa = m["sigma_excess_ann"]
    v = feas.get(f"{round(sa,2):.2f}") or feas.get(f"{round(sa,1):.1f}")
    if v:
        print(f"  该 σ 在可行域中：P(MDD≤10%) = {v['p_ok_fat']*100:.1f}%"
              f"（厚尾），要 IR=0.5 需超额年化 ≥ {v['need_excess_ann']*100:.2f}%"
              f"，实际 {m['excess_ann']*100:.2f}%")
        if m["excess_ann"] >= v["need_excess_ann"] and v["p_ok_fat"] >= 0.6:
            print("  ⇒ **落在可行域内！** 只需把 σ_超额 压到该量级即可同时达标")
        else:
            print("  ⇒ 仍在可行域外：需要同时①降 σ_超额 ②提 IR")

    json.dump({
        "meta": {"round": 81,
                 "purpose": "β 敞口调整：把「低 β」发现转成可执行方案（放宽集中度而非加杠杆）",
                 "panel": os.path.relpath(PANEL, HERE),
                 "weights_version": wc["version"],
                 "caveat": ("⚠️ 所有结果基于 23~48 期样本，标准误大；"
                            "「放宽集中度提高 β」的效果**未做样本外检验**，"
                            "上线前必须在独立区间复算")},
        "by_config": res,
    }, open(OUT, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"\n→ {OUT}")


if __name__ == "__main__":
    main()
