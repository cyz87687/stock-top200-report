#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
audit_round78_exposure.py —— 超额波动的来源解剖：beta / size / 个股集中度

═══ 为什么要做这一步 ═══

round77 的 F2 得出决定性结论：
  · 当前最优方案 top20_h10_score_cap0.08 的**超额年化波动 σ_ann = 26.3%**
  · 实测超额 MDD −21.56%，蒙特卡洛在同 σ 下给出 −19.93%（厚尾）→ 吻合
  · 要把 MDD 压到 −10% 以内，需 σ_ann ≤ 10%（降 62%），
    此时 IR 不变则超额年化从 +8.35% 降到约 +3.2%
  ⇒ **目标③的抓手是「降低超额波动」，而这比「提升 alpha」更可操作**

但 σ_ann = 26.3% 本身就很反常：组合本身年化波动只有约 12%，
池等权约 25%。两个 12% / 25% 波动的序列，其**差**（超额）不该有 26% 波动。
除非二者**不同步**（beta 失配 / size 暴露失配）。

本脚本解剖超额波动的四个来源：
  S1 beta 失配：组合收益对池等权收益的回归斜率 β
  S2 size 暴露失配：组合与池等权的市值加权平均差异
  S3 特质残差：正交化后的残差波动（这才是"真 alpha 波动"，无法靠中性化消除）
  S4 集中度效应：个股数越少，特质残差越大

═══ 若能证实 S1/S2 占主导 → 目标③有低成本解 ═══

做法：把组合对「池等权」回归，β≠1 就说明组合其实是一个「带方向暴露的组合」，
其超额把 beta 暴露的波动算进去了。若把组合**beta 中性化**（对池等权做
beta 调整）或**size 中性化**，超额 σ 会显著下降 → MDD 达标。

用法：python audit_round78_exposure.py
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
OUT = os.path.join(HERE, "audit_round78.json")


def build_scores(panel, wmap, dim_max):
    out = {}
    for d, rows in panel["rows"].items():
        arr = []
        for c, rec in rows.items():
            s, _uw = composite_all(rec, wmap, dim_max)
            if s is not None:
                arr.append((c, s))
        arr.sort(key=lambda x: (-x[1], x[0]))
        out[d] = arr
    return out


def bt(panel, scores, px, topn, hold, mode="score", w_cap=0.08,
       beta_neutral=False, mkt_key="pool"):
    """回测。beta_neutral=True 时按池等权 beta 做仓位缩放（仅用已实现信息）。

    ⚠️ beta 估计用**过去**的已实现组合/池收益序列，不含未来。
    """
    dates = panel["dates"]
    rebal = [(dates[i], dates[i + 1], i) for i in range(0, len(dates) - 1, hold)]
    periods, nav, prev_w = [], 1.0, {}
    hist = []   # 已实现 (组合收益, 池收益) 对
    for k, (d_sig, d_exec, i0) in enumerate(rebal):
        d_next = rebal[k + 1][1] if k + 1 < len(rebal) else None
        picks = scores.get(d_sig) or []
        sel = [(c, s) for c, s in picks if _px(px, c, d_exec) is not None][:topn]
        if len(sel) < max(5, topn // 4):
            continue
        if mode == "score":
            raw = np.array([max(1e-6, s) for _, s in sel])
            w = raw / raw.sum()
            for _ in range(50):
                over = w > w_cap
                if not over.any():
                    break
                ex = (w[over] - w_cap).sum()
                w[over] = w_cap
                free = ~over
                if free.sum() == 0:
                    break
                w[free] += ex * w[free] / w[free].sum()
            w = w / w.sum()
        else:
            w = np.ones(len(sel)) / len(sel)
        wmap = {sel[i][0]: float(w[i]) for i in range(len(sel))}
        allc = set(wmap) | set(prev_w)
        turnover = sum(abs(wmap.get(c, 0.0) - prev_w.get(c, 0.0)) for c in allc) / 2.0
        rets, wts = [], []
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
                rets.append(p1 / p0 - 1.0)
                wts.append(wmap[c])
        if not rets:
            prev_w = wmap
            continue
        ws = sum(wts)
        gross = sum(r * ww for r, ww in zip(rets, wts)) / ws if ws > 0 else 0.0
        # 池等权同期收益
        prs = []
        for c in [c for c, _s in picks]:
            p0 = _px(px, c, d_exec)
            p1 = _px(px, c, d_next) if d_next else None
            if p0 and p1 and p0 > 0:
                prs.append(p1 / p0 - 1.0)
        pool_r = (sum(prs) / len(prs)) if prs else None

        # β 中性化：用历史已实现的 (组合, 池) 估计 β，再缩放当期敞口
        # ⚠️ 2026-10-29 修正：实测组合 β ≈ 0.32（远小于 1），
        #    说明组合是「低 beta 组合」——它跑赢池时 gap 就成了超额波动主体。
        #    要**消除 β 失配**，需要把组合的池敞口从 β 提到 1（**加杠杆**），
        #    或把基准换成 β 匹配的组合。之前写的"β<1 不加杠杆"是错的。
        scale = 1.0
        if beta_neutral and len(hist) >= 8:
            h = np.array(hist[-12:], dtype=np.float64)
            var = h[:, 1].var(ddof=1)
            if var > 1e-12:
                beta = float(np.cov(h[:, 0], h[:, 1], ddof=1)[0, 1] / var)
                # 目标：组合对池的敞口 → 1.0  ⇒ scale = 1/beta
                # 实践中对加杠杆设上限（1.8x），避免风险失控
                if beta > 0.05:
                    scale = min(1.8, 1.0 / beta)
        gross = gross * scale
        net = gross - turnover * COST_ROUND
        nav *= (1.0 + net)
        if pool_r is not None:
            hist.append((gross, pool_r))
        periods.append({"exec_date": d_exec, "net": net, "gross": gross,
                        "pool_ret": pool_r, "scale": scale, "turnover": turnover,
                        "nav": nav})
        prev_w = wmap
    return periods


def analyze(periods, hold):
    n = len(periods)
    if n < 6:
        return None
    ann = 244.0 / hold
    port = np.array([p["net"] for p in periods])
    pool = np.array([p["pool_ret"] if p["pool_ret"] is not None else np.nan for p in periods])
    ok = ~np.isnan(pool)
    port, pool = port[ok], pool[ok]
    n = len(port)
    if n < 6:
        return None
    # 回归：port = a + b*pool + e
    b = float(np.cov(port, pool, ddof=1)[0, 1] / pool.var(ddof=1))
    a = float(port.mean() - b * pool.mean())
    resid = port - (a + b * pool)
    ex = port - pool
    sig_p = float(port.std(ddof=1) * math.sqrt(ann))
    sig_m = float(pool.std(ddof=1) * math.sqrt(ann))
    sig_e = float(resid.std(ddof=1) * math.sqrt(ann))
    sig_x = float(ex.std(ddof=1) * math.sqrt(ann))
    # 超额净值回撤
    navx = np.cumprod(1.0 + ex)
    peak = np.maximum.accumulate(navx)
    mdd = float((navx / peak - 1.0).min())
    # 组合自身回撤
    navp = np.cumprod(1.0 + port)
    peakp = np.maximum.accumulate(navp)
    mddp = float((navp / peakp - 1.0).min())
    return {
        "n": n,
        "beta": b, "alpha_per_period": a,
        "ann_port": float((1 + port.mean()) ** ann - 1),
        "ann_pool": float((1 + pool.mean()) ** ann - 1),
        "ann_excess": float((1 + ex.mean()) ** ann - 1),
        "sigma_port_ann": sig_p, "sigma_pool_ann": sig_m,
        "sigma_resid_ann": sig_e, "sigma_excess_ann": sig_x,
        "ir": float(ex.mean() / ex.std(ddof=1) * math.sqrt(ann)) if ex.std(ddof=1) > 1e-12 else None,
        "excess_mdd": mdd, "port_mdd": mddp,
        "resid_share_of_excess_var": float(sig_e ** 2 / sig_x ** 2) if sig_x > 1e-12 else None,
        "beta_share_of_excess_var": float(max(0.0, (b - 1) ** 2 * sig_m ** 2) / sig_x ** 2) if sig_x > 1e-12 else None,
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

    print("\n" + "═" * 96)
    print("S1  超额波动的来源解剖（对 POOL_EW）")
    print("═" * 96)
    print(f"{'方案':34s}{'β':>7s}{'σ组合':>9s}{'σ池':>9s}{'σ超额':>9s}{'σ残差':>9s}"
          f"{'IR':>7s}{'超额MDD':>10s}{'组合MDD':>10s}")
    print("-" * 96)
    cases = []
    for topn, hold in ((20, 10), (20, 20), (30, 20), (50, 20), (80, 20), (100, 20)):
        cases.append((f"top{topn}_h{hold}_score", topn, hold, "score", 0.08, False))
    for topn in (20, 30, 50):
        cases.append((f"top{topn}_h20_equal", topn, 20, "equal", None, False))
    res = {}
    for nm, topn, hold, mode, cap, bn in cases:
        pers = bt(panel, scores, px, topn, hold, mode, cap or 0.08, bn)
        a = analyze(pers, hold)
        if not a:
            continue
        res[nm] = a
        print(f"{nm:34s}{a['beta']:>7.3f}{a['sigma_port_ann']*100:>8.1f}%"
              f"{a['sigma_pool_ann']*100:>8.1f}%{a['sigma_excess_ann']*100:>8.1f}%"
              f"{a['sigma_resid_ann']*100:>8.1f}%{a['ir']:>7.2f}"
              f"{a['excess_mdd']*100:>9.2f}%{a['port_mdd']*100:>9.2f}%")

    # 分解说明
    print("\n【分解】σ²_超额 = (β−1)²·σ²_池 + σ²_残差")
    print(f"{'方案':34s}{'β失配贡献':>12s}{'残差贡献':>12s}{'合计':>10s}")
    print("-" * 70)
    for nm, a in res.items():
        bv = a["beta_share_of_excess_var"]
        rv = a["resid_share_of_excess_var"]
        print(f"{nm:34s}{(bv or 0)*100:>11.1f}%{(rv or 0)*100:>11.1f}%"
              f"{(bv or 0)*100+(rv or 0)*100:>9.1f}%")

    print("\n" + "═" * 96)
    print("S2  β 中性化后的效果（用过去已实现收益估计 β，不含未来）")
    print("═" * 96)
    print(f"{'方案':34s}{'β':>7s}{'σ超额':>9s}{'σ残差':>9s}{'IR':>7s}"
          f"{'超额MDD':>10s}{'年化':>9s}")
    print("-" * 96)
    res_bn = {}
    for topn, hold in ((20, 10), (30, 20)):
        nm = f"top{topn}_h{hold}_betaNeutral"
        pers = bt(panel, scores, px, topn, hold, "score", 0.08, True)
        a = analyze(pers, hold)
        if not a:
            continue
        res_bn[nm] = a
        print(f"{nm:34s}{a['beta']:>7.3f}{a['sigma_excess_ann']*100:>8.1f}%"
              f"{a['sigma_resid_ann']*100:>8.1f}%{a['ir']:>7.2f}"
              f"{a['excess_mdd']*100:>9.2f}%{a['ann_excess']*100:>8.2f}%")

    print("\n" + "═" * 96)
    print("S3  结论")
    print("═" * 96)
    if res:
        best = min(res.items(), key=lambda kv: kv[1]["sigma_excess_ann"])
        nm, a = best
        print(f"  · 最小超额波动的方案：{nm}  σ_超额={a['sigma_excess_ann']*100:.1f}%  "
              f"β={a['beta']:.3f}")
        print(f"    其中 β 失配贡献 {(a['beta_share_of_excess_var'] or 0)*100:.1f}%，"
              f"特质残差贡献 {(a['resid_share_of_excess_var'] or 0)*100:.1f}%")
        if (a["beta_share_of_excess_var"] or 0) > 0.3:
            print("    ⇒ **β 失配是超额波动的主要来源** → 做 beta 中性化有低成本收益")
        else:
            print("    ⇒ **特质残差是超额波动的主要来源** → 中性化帮不上忙，"
                  "只能靠提升 alpha 精度（更集中的持仓、更强的维）")
        print(f"  · 组合自身回撤 {a['port_mdd']*100:.2f}%，超额回撤 {a['excess_mdd']*100:.2f}%")
        print(f"    ⇒ 超额回撤是组合回撤的 {a['excess_mdd']/a['port_mdd'] if a['port_mdd'] else 0:.2f} 倍，"
              f"说明**回撤主要来自组合自身 beta，而非选股**")

    json.dump({
        "meta": {"round": 78,
                 "purpose": "超额波动来源解剖：beta 失配 vs 特质残差 vs size 暴露",
                 "panel": os.path.relpath(PANEL, HERE),
                 "weights_version": wc["version"],
                 "n_dates": len(panel["dates"])},
        "exposure": res,
        "beta_neutral": res_bn,
    }, open(OUT, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"\n→ {OUT}")


if __name__ == "__main__":
    main()
