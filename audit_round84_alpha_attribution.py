#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
audit_round84_alpha_attribution.py —— α 的来源归因：哪一维、哪一种持仓方式

═══ 缘起 ═══

round83 分解出关键事实：
  · 段C：α年化 **+85.16%**（t +2.75），优于 200 随机组合 **p99.5** → 真实 alpha
  · 段A：α仅 p59 → 不显著
  · 全样本：α +19.02%，p91.5
⇒ **打分的 alpha 是真实的，但只在特定市场区间出现**。

这与 round71 的 r(IC,市场)=−0.6705 并不矛盾：
  · round71 测的是**截面 IC**（排序能力，被市场方向调制）
  · round83 测的是**组合 α**（已剥离 beta，在特定区间显现）
两者结合的图景是：
  · 模型在**跌市**里，截面排序能力与选股能力同时最强（IC +0.308，α +85%）
  · 模型在**涨市**里，两者同时最弱

═══ 本脚本要回答的三个问题 ═══

Q1 **α 来自哪些维？**
    逐维消融（leave-one-out）与逐维单独（single-dim）对比，
    在段C 上算 α。→ 找出真正贡献 alpha 的维，以及**拖后腿的维**。

Q2 **alpha 的实现路径是什么？**
    · 打分加权 vs 等权（同样持仓）→ alpha 来自「排序」还是来自「选股范围」
    · 不同 TopN 的 α
    · 不同 H 的 α

Q3 **alpha 是否只在「跌市」存在？**
    按市场方向分组看 α（不只是 IR）。若 α 与市场方向也负相关，
    则模型是「跌市 alpha 型」，定位必须写清楚。

用法：python audit_round84_alpha_attribution.py
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
OUT = os.path.join(HERE, "audit_round84.json")
DIMS = [
    "score_fund", "score_quality", "score_ep", "score_lowvol20",
    "score_bias60", "score_rsi_rev", "score_tech_neg",
    "score_size_growth", "score_dominant",
]
GROUPS = {
    "基本面": ["score_fund", "score_quality"],
    "估值": ["score_ep"],
    "低波": ["score_lowvol20"],
    "技术反转": ["score_bias60", "score_rsi_rev", "score_tech_neg"],
    "市值": ["score_size_growth", "score_dominant"],
}


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


def _cap_weight(w, cap=0.08):
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


def run(panel, px, scores, topn, hold, mode="score"):
    dates = panel["dates"]
    rebal = [(dates[i], dates[i + 1], i) for i in range(0, len(dates) - 1, hold)]
    pers, prev_w = [], {}
    for idx, (d_sig, d_exec, i0) in enumerate(rebal):
        d_next = rebal[idx + 1][1] if idx + 1 < len(rebal) else None
        picks = scores.get(d_sig) or []
        cands = [(c, s) for c, s in picks if _px(px, c, d_exec) is not None]
        if not cands:
            prev_w = {}
            continue
        sel = cands[:topn]
        if len(sel) < max(5, topn // 4):
            prev_w = {}
            continue
        if mode == "score":
            raw = np.array([max(1e-6, s) for _, s in sel])
            w = _cap_weight(raw, 0.08)
        else:
            w = np.ones(len(sel)) / len(sel)
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
        for c, _s in cands:
            p0 = _px(px, c, d_exec)
            p1 = _px(px, c, d_next) if d_next else None
            if p0 and p1 and p0 > 0:
                prs.append(p1 / p0 - 1.0)
        pers.append({"net": net, "port": port,
                     "pool": (sum(prs) / len(prs)) if prs else None,
                     "pool_dir": 1 if (prs and np.mean(prs) > 0) else -1})
        prev_w = wmap
    return pers


def decomp(pers, hold, only_up=None, only_dn=None):
    sub = []
    for p in pers:
        if only_up and p["pool_dir"] < 0:
            continue
        if only_dn and p["pool_dir"] > 0:
            continue
        sub.append(p)
    if len(sub) < 4:
        return None
    ann = 244.0 / hold
    net = np.array([p["net"] for p in sub])
    pool = np.array([p["pool"] if p["pool"] is not None else np.nan for p in sub])
    ok = ~np.isnan(pool)
    net, pool = net[ok], pool[ok]
    if len(net) < 4 or pool.var(ddof=1) < 1e-12:
        return None
    b = float(np.cov(net, pool, ddof=1)[0, 1] / pool.var(ddof=1))
    a = float(net.mean() - b * pool.mean())
    resid = net - (a + b * pool)
    s2 = float((resid ** 2).sum() / (len(net) - 2))
    se_a = math.sqrt(s2 / len(net)) if len(net) > 2 else None
    return {"n": len(net), "alpha_ann": float(a * ann),
            "alpha_t": (a / se_a) if se_a and se_a > 1e-12 else None,
            "beta": b,
            "pool_ann": float((1 + pool.mean()) ** ann - 1),
            "excess_ann": float((1 + (net - pool).mean()) ** ann - 1)}


def main():
    print("加载 ...")
    panel = json.load(open(PANEL, encoding="utf-8"))
    px = load_prices()
    wc = json.load(open(WEIGHTS, encoding="utf-8"))
    dim_max = {k: float(v) for k, v in wc["dim_max"].items()}
    w_base = {k: float(v) for k, v in wc["weights"].items()}

    hold = 10
    topn = 20
    scores_full = build_scores(panel, wmap=w_base, dim_max=dim_max)
    pers_full = run(panel, px, scores_full, topn, hold)

    print("\n" + "═" * 104)
    print("Q1  α 的来源：逐维消融（leave-one-out）与逐维单独（全样本 + 分市场方向）")
    print("═" * 104)
    print("  口径：TopN=20, H=10, 打分加权 cap0.08；α = 对池等权回归的截距年化")
    print()
    base_all = decomp(pers_full, hold)
    print(f"  基线（全 9 维）  α年化 {base_all['alpha_ann']*100:+.2f}%  "
          f"t {base_all['alpha_t']:+.2f}  β {base_all['beta']:.3f}")
    print()
    print(f"{'配置':26s}{'α年化':>11s}{'Δα':>11s}{'α t':>8s}{'β':>8s}{'池年化':>10s}")
    print("-" * 78)
    loo = {}
    for d in DIMS:
        w2 = {k: v for k, v in w_base.items()}
        w2[d] = 0.0
        # 剩余权重重新归一（保持 Σ=1）
        s = sum(w2.values())
        w2 = {k: v / s for k, v in w2.items()}
        sc = build_scores(panel, w2, dim_max)
        m = decomp(run(panel, px, sc, topn, hold), hold)
        if not m:
            continue
        loo[d] = m
        d_alpha = m["alpha_ann"] - base_all["alpha_ann"]
        flag = "← 关键贡献" if d_alpha < -0.05 else ("← 拖累" if d_alpha > 0.05 else "")
        print(f"{'剔除 '+d.replace('score_',''):26s}{m['alpha_ann']*100:>+10.2f}%"
              f"{d_alpha*100:>+10.2f}%{(m['alpha_t'] or 0):>+8.2f}{m['beta']:>8.3f}"
              f"{m['pool_ann']*100:>+9.2f}%  {flag}")

    print("\n  单维单独使用（其余归零）：")
    for d in DIMS:
        w2 = {k: 0.0 for k in w_base}
        w2[d] = 1.0
        sc = build_scores(panel, w2, dim_max)
        m = decomp(run(panel, px, sc, topn, hold), hold)
        if not m:
            continue
        print(f"    仅 {d.replace('score_',''):14s} α年化 {m['alpha_ann']*100:>+9.2f}%  "
              f"t {(m['alpha_t'] or 0):>+6.2f}  β {m['beta']:.3f}")

    print("\n" + "═" * 104)
    print("Q2  α 的实现路径：加权方式 / 持仓数 / 调仓频率")
    print("═" * 104)
    print(f"{'配置':24s}{'α年化':>11s}{'α t':>8s}{'β':>8s}{'超额年化':>11s}{'σ超额':>10s}")
    print("-" * 76)
    paths = {}
    for mode in ("score", "equal"):
        m = decomp(run(panel, px, scores_full, topn, hold, mode), hold)
        if m:
            paths[f"{mode}_top{topn}_h{hold}"] = m
            print(f"{mode+' Top'+str(topn)+' H'+str(hold):24s}{m['alpha_ann']*100:>+10.2f}%"
                  f"{(m['alpha_t'] or 0):>+8.2f}{m['beta']:>8.3f}"
                  f"{m['excess_ann']*100:>+10.2f}%")
    for t in (10, 15, 20, 30, 50, 80):
        m = decomp(run(panel, px, scores_full, t, hold), hold)
        if m:
            paths[f"score_top{t}_h{hold}"] = m
            print(f"{'score Top'+str(t)+' H'+str(hold):24s}{m['alpha_ann']*100:>+10.2f}%"
                  f"{(m['alpha_t'] or 0):>+8.2f}{m['beta']:>8.3f}"
                  f"{m['excess_ann']*100:>+10.2f}%")
    for h in (5, 20):
        m = decomp(run(panel, px, scores_full, topn, h), hold)
        if m:
            paths[f"score_top{topn}_h{h}"] = m
            print(f"{'score Top'+str(topn)+' H'+str(h):24s}{m['alpha_ann']*100:>+10.2f}%"
                  f"{(m['alpha_t'] or 0):>+8.2f}{m['beta']:>8.3f}"
                  f"{m['excess_ann']*100:>+10.2f}%")

    print("\n" + "═" * 104)
    print("Q3  α 是否只在跌市存在？（按每期池等权收益方向分组）")
    print("═" * 104)
    up = decomp(pers_full, hold, only_up=True)
    dn = decomp(pers_full, hold, only_dn=True)
    print(f"{'分组':12s}{'n':>5s}{'池年化':>11s}{'α年化':>11s}{'α t':>9s}{'β':>8s}")
    print("-" * 60)
    for nm, m in (("涨市期", up), ("跌市期", dn)):
        if m:
            print(f"{nm:12s}{m['n']:>5d}{m['pool_ann']*100:>+10.2f}%"
                  f"{m['alpha_ann']*100:>+10.2f}%{(m['alpha_t'] or 0):>+9.2f}{m['beta']:>8.3f}")
    if up and dn:
        print()
        if up["alpha_ann"] < 0 and dn["alpha_ann"] > 0:
            print("  ⇒ **α 与市场方向负相关** → 模型是「跌市 alpha 型」：")
            print("     在跌市里选出抗跌且能赚钱的股票，在涨市里反而拖累。")
            print("     ⇒ 定位必须写明：**这是跌市/震荡市选股器，不是全天候选股器**。")
        elif up["alpha_ann"] > 0 and dn["alpha_ann"] > 0:
            print("  ⇒ α 在涨跌市都为正 → 模型是**全天候 alpha**，可作长期持仓工具。")

    json.dump({
        "meta": {"round": 84,
                 "purpose": "α 的来源归因：维度消融 / 实现路径 / 市场方向依赖",
                 "panel": os.path.relpath(PANEL, HERE),
                 "weights_version": wc["version"],
                 "config": {"topn": topn, "hold": hold, "cap": 0.08},
                 "caveat": "全部基于 23 期样本，α 的 t 值功效极低，只作方向与排序参考"},
        "baseline": base_all,
        "leave_one_out": loo,
        "paths": paths,
        "by_market_dir": {"up": up, "down": dn},
    }, open(OUT, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"\n→ {OUT}")


if __name__ == "__main__":
    main()
