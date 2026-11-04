#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
audit_round83_decomposed_alpha.py —— 把「组合超额」拆成 alpha 成分与 beta 成分

═══ 缘起 ═══

round82 的 walk-forward 出现一个必须解释的现象：
  · 段A（2025-11~2026-03）**所有 TopN 的 IR 均为负**（−0.72 ~ −1.57）
  · 段C（2026-07~2026-10）**所有 TopN 的 IR 均为正**（+2.5 ~ +5.1）
  · 段C 的 IR 还高得离谱（+3.92、+5.10）

而 round76 独立复算给出同样的格局（段A −1.30 / 段B −1.55 / 段C +2.56）。

⚠️ 这与 round71 的 r(IC, 市场) = −0.6705 完全吻合：
   段A 段B 是普涨/震荡市 → 模型跑输 → IR 为负；
   段C 是普跌市 → 模型少亏 → IR 暴增。

⇒ **段C 的 IR +3.92 极可能不是 alpha，而是「在普跌市里少亏」的 beta 效应。**
   若成立，那么「段C 优于随机组合 p90」这个结论也是假的（随机组合在跌市里
   同样少亏，只是少亏幅度不同）。

═══ 本脚本的判决性检验 ═══

对每一段，分解组合相对池等权的收益为两块：
    组合收益 = α + β·池收益 + ε
  · α（截距年化）= **真正的 alpha**：与市场无关的选股能力
  · β·池收益 = 市场暴露部分
  · 超额（组合−池）= (α−μ_pool) + (β−1)·池 + ε

然后问：**扣除 beta 失配贡献后，组合还剩多少 alpha？**

判据：
  · 若段C 的 alpha（截距）≈ 0 或为负 → 段C 的 IR 完全是 beta 效应，**不可归因**
  · 若段C 的 alpha 显著为正 → 有真实 alpha

同时用**同口径随机组合**算 α 的分布 → 得到 alpha 的随机基线，
判断真实组合的 alpha 是否显著优于随机（这比 IR 分位更严格，
因为它已经剥离了市场暴露的影响）。

用法：python audit_round83_decomposed_alpha.py
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
OUT = os.path.join(HERE, "audit_round83.json")


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


def run(panel, px, scores, topn, hold, rng=None, random_pick=False):
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
        if random_pick:
            if len(cands) <= topn:
                sel = cands
            else:
                ii = rng.choice(len(cands), size=topn, replace=False)
                sub = [cands[i] for i in ii]
                sc = rng.random(len(sub)) + 0.5
                sel = [(c, float(s)) for (c, _), s in zip(sub, sc)]
        else:
            sel = cands[:topn]
        if len(sel) < max(5, topn // 4):
            prev_w = {}
            continue
        raw = np.array([max(1e-6, s) for _, s in sel])
        w = _cap_weight(raw, 0.08)
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
                     "pool": (sum(prs) / len(prs)) if prs else None})
        prev_w = wmap
    return pers


def decomp(pers, hold):
    """分解：组合收益 = α + β·池收益 + ε"""
    if len(pers) < 4:
        return None
    ann = 244.0 / hold
    net = np.array([p["net"] for p in pers])
    pool = np.array([p["pool"] if p["pool"] is not None else np.nan for p in pers])
    ok = ~np.isnan(pool)
    net, pool = net[ok], pool[ok]
    n = len(net)
    if n < 4 or pool.var(ddof=1) < 1e-12:
        return None
    b = float(np.cov(net, pool, ddof=1)[0, 1] / pool.var(ddof=1))
    a = float(net.mean() - b * pool.mean())
    resid = net - (a + b * pool)
    ex = net - pool
    # alpha 的标准误（用回归标准误）
    s2 = float((resid ** 2).sum() / (n - 2))
    se_a = math.sqrt(s2 / n)          # 截距的标准误（pool 已去均值）
    # 但更常用的是 alpha 的 t 检验（H0: alpha=0），用整体回归
    t_a = a / se_a if se_a > 1e-12 else None
    return {
        "n": n,
        "alpha_per_period": a, "alpha_ann": float(a * ann),
        "alpha_t": t_a,
        "beta": b,
        "pool_ann": float((1 + pool.mean()) ** ann - 1),
        "port_ann": float((1 + net.mean()) ** ann - 1),
        "excess_ann": float((1 + ex.mean()) ** ann - 1),
        "beta_mismatch_contrib_ann": float((b - 1) * pool.mean() * ann),
        "resid_ann": float(resid.mean() * ann),
        "ex_sigma_ann": float(ex.std(ddof=1) * math.sqrt(ann)),
        "resid_sigma_ann": float(resid.std(ddof=1) * math.sqrt(ann)),
    }


def main():
    print("加载 ...")
    panel = json.load(open(PANEL, encoding="utf-8"))
    px = load_prices()
    wc = json.load(open(WEIGHTS, encoding="utf-8"))
    wmap = {k: float(v) for k, v in wc["weights"].items()}
    dim_max = {k: float(v) for k, v in wc["dim_max"].items()}
    scores = build_scores(panel, wmap, dim_max)

    hold = 10
    topn = 20
    pers_all = run(panel, px, scores, topn, hold)
    n = len(pers_all)
    iA, iB = int(n * 0.40), int(n * 0.70)
    segs = {"ALL": (0, n), "A": (0, iA), "B": (iA, iB), "C": (iB, n)}

    print("\n" + "═" * 100)
    print("D1  组合收益 = α + β·池收益 + ε 的分解（TopN=20, H=10）")
    print("═" * 100)
    print(f"{'段':5s}{'n':>4s}{'池年化':>10s}{'组合年化':>10s}{'超额年化':>10s}"
          f"{'α年化':>10s}{'α t值':>9s}{'β':>8s}{'β失配贡献':>12s}")
    print("-" * 84)
    res = {}
    for k, (a, b) in segs.items():
        m = decomp(pers_all[a:b], hold)
        if not m:
            continue
        res[k] = m
        print(f"{k:5s}{m['n']:>4d}{m['pool_ann']*100:>+9.2f}%{m['port_ann']*100:>+9.2f}%"
              f"{m['excess_ann']*100:>+9.2f}%{m['alpha_ann']*100:>+9.2f}%"
              f"{(m['alpha_t'] or 0):>+9.2f}{m['beta']:>8.3f}"
              f"{m['beta_mismatch_contrib_ann']*100:>+11.2f}%")

    print("\n【解读】α = 与市场无关的选股能力；β失配贡献 = 市场暴露带来的相对表现")
    if "C" in res and "A" in res:
        c, a = res["C"], res["A"]
        print(f"  段A：α年化 {a['alpha_ann']*100:+.2f}%（t {a['alpha_t']:+.2f}）"
              f"  β={a['beta']:.3f}  → 超额主要来自 β失配 {a['beta_mismatch_contrib_ann']*100:+.2f}%")
        print(f"  段C：α年化 {c['alpha_ann']*100:+.2f}%（t {c['alpha_t']:+.2f}）"
              f"  β={c['beta']:.3f}  → 超额主要来自 β失配 {c['beta_mismatch_contrib_ann']*100:+.2f}%")
        if c["alpha_t"] is not None and a["alpha_t"] is not None:
            if c["alpha_t"] < 2.0 and a["alpha_t"] < 2.0:
                print("\n  ⇒ ⚠️ **两段 α 的 t 值都不显著** → 组合相对池的「超额」")
                print("     **主要是 β 失配（市场暴露差异），不是选股 alpha**")
                print(f"     段C 的 IR {res['C']['excess_ann']/res['C']['ex_sigma_ann']:.2f} 之所以极高，")
                print("     是因为普跌市里低 β 组合「少亏」——这是**风格暴露的副产品**，")
                print("     **不可归因于 9 维打分**")
            elif c["alpha_t"] >= 2.0:
                print("\n  ⇒ ✅ 段C 的 α 显著为正 → 存在真实 alpha")

    # ═══ D2：随机基线的 α 分布 ═══
    print("\n" + "═" * 100)
    print("D2  同口径随机组合的 α 分布（200 次）——真实组合的 α 是否显著优于随机")
    print("═" * 100)
    print("  这比 IR 分位更严格：α 已剥离市场暴露的影响")
    print()
    print(f"{'段':5s}{'真实α年化':>12s}{'随机α中位':>12s}{'随机αp10':>12s}"
          f"{'随机αp90':>12s}{'α分位':>10s}")
    print("-" * 66)
    rand_alpha = {}
    for sk in ("A", "C", "ALL"):
        a, b = segs[sk]
        als = []
        for si in range(200):
            rng = np.random.default_rng(20261029 + si)
            m = decomp(run(panel, px, scores, topn, hold, rng, True)[a:b], hold)
            if m:
                als.append(m["alpha_ann"])
        if not als or sk not in res:
            continue
        arr = np.array(als)
        rv = res[sk]["alpha_ann"]
        pct = float((arr < rv).mean() * 100)
        rand_alpha[sk] = {"median": float(np.median(arr)),
                           "p10": float(np.percentile(arr, 10)),
                           "p90": float(np.percentile(arr, 90)),
                           "real": rv, "percentile": pct}
        print(f"{sk:5s}{rv*100:>+11.2f}%{np.median(arr)*100:>+11.2f}%"
              f"{np.percentile(arr,10)*100:>+11.2f}%{np.percentile(arr,90)*100:>+11.2f}%"
              f"{pct:>9.1f}%")

    print("\n" + "═" * 100)
    print("D3  结论")
    print("═" * 100)
    verdict_parts = []
    for sk in ("A", "C", "ALL"):
        if sk not in rand_alpha:
            continue
        r = rand_alpha[sk]
        if r["percentile"] >= 95:
            verdict_parts.append(f"段{sk}: α 优于随机 p{r['percentile']:.0f} → **真实 alpha**")
        elif r["percentile"] >= 60:
            verdict_parts.append(f"段{sk}: α 优于随机 p{r['percentile']:.0f} → 微弱")
        else:
            verdict_parts.append(f"段{sk}: α 仅优于随机 p{r['percentile']:.0f} → **不显著**")
    for v in verdict_parts:
        print(f"  · {v}")

    print()
    print("  【最重要的推论】")
    print("  若段C 的 α 不显著，则说明：")
    print("    · 此前所有「对 POOL_EW 的 IR」都混合了 beta 失配，不是纯 alpha；")
    print("    · round80 的「打分优于随机 p66」同样被 beta 效应污染；")
    print("    · 真正该检验的是 **α（对池回归的截距）**，而非 IR。")
    print("  而 9 维打分真正的价值，可能在于**它选出了低 β 股票** ——")
    print("  这是一个**仓位管理/风格配置**的价值，不是 alpha 选股的价值。")
    print("  ⇒ 模型的正确定位可能不是「选出好股票」，而是「识别出该防守的时候」")

    json.dump({
        "meta": {"round": 83,
                 "purpose": "把组合超额拆成 alpha(截距) 与 beta 失配 —— 判定真实选股能力",
                 "panel": os.path.relpath(PANEL, HERE),
                 "weights_version": wc["version"],
                 "config": {"topn": topn, "hold": hold, "n_seeds": 200},
                 "caveat": "段A/B/C 仅 9/7/8 期，α 的 t 值统计功效极低，只作方向判断"},
        "decomp": res,
        "random_alpha": rand_alpha,
    }, open(OUT, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"\n→ {OUT}")


if __name__ == "__main__":
    main()
