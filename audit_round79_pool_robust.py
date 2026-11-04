#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
audit_round79_pool_robust.py —— 池稳健性检验：β=0.32 是「alpha」还是「池前视的口径产物」

═══ 核心问题 ═══

round78 实测：**组合对池等权的 β 只有 0.32**。
这有两���完全不同的解释，且导向完全相反的结论：

解释 A（乐观）：组合是低 beta 组合，只承担 1/3 的市场风险却跑赢池 → 真实 alpha
解释 B（悲观）：β=0.32 是**口径产物** —— 池等权是用「今天」筛出的名单回溯算的，
              而组合买的是**当时分数最高的 20 只**，两者根本不是同一批股票的可比集合。

若为 B，则：
  · 「跑赢池」在很大程度上是**选股口径不一致**的假象（分母是幸存者偏误集）
  · 之前所有「对 POOL_EW 的超额」都被系统性高估
  · 而 β=0.32 恰恰是「组合买的是另一类股票（大盘/高质量）」的直接证据

═══ 决定性检验：分位数对照（quantile-matched benchmark）═══

构造多个**同口径**基准，看组合相对谁赢：

  B0  池等权                      （当前基准，含幸存者偏误）
  B1  池等权，但只含「组合当期的持仓股」   ← 同股票集合，隔离选股 alpha
  B2  池内同市值分位等权           ← 控 size
  B3  池内按打分随机抽样等权       ← 控打分，给出「打分是否有效」的答案

关键对比：
  · 若 B1（同一批股票等权）也大幅跑输组合 → alpha 真实存在
  · 若 B1 与组合基本打平 → 之前的超额来自「选哪批股票」，不是「怎么排」

**B3 是最重要的一环**：它直接回答「这 9 个维的排序到底有没有用」。
若组合对 B3（随机打分）没有显著优势 → 打分无效，整个模型的价值要重估。

═══ 另一项检验：β 的分解 ═══

  · 组合 vs 池等权 β
  · 组合 vs 全市场等权 β（用真实指数）  ← 若这个 β 也低，说明是组合真低 beta
  · 组合持仓的市值中位数 vs 池的市值中位数 ← 直接看 size 暴露差

用法：python audit_round79_pool_robust.py
"""
import os
import sys
import json
import math
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from audit_round67_backtest_engine import (  # noqa
    composite_all, load_prices, load_bench, _px, _bpx, COST_ROUND, BENCHES)

PANEL = os.path.join(HERE, "data", "panel_round66.json")
WEIGHTS = os.path.join(HERE, "factor_weights.json")
OUT = os.path.join(HERE, "audit_round79.json")


def build_scores(panel, wmap, dim_max):
    out = {}
    for d, rows in panel["rows"].items():
        arr = []
        for c, rec in rows.items():
            s, uw = composite_all(rec, wmap, dim_max)
            if s is not None:
                arr.append((c, s, uw))
        arr.sort(key=lambda x: (-x[1], x[0]))
        out[d] = arr
    return out


def run(panel, scores, px, bench_px, topn, hold, seed=20261029):
    """回测，同时构造 4 个同口径基准。"""
    dates = panel["dates"]
    rebal = [(dates[i], dates[i + 1], i) for i in range(0, len(dates) - 1, hold)]
    rng = np.random.default_rng(seed)
    periods, nav, prev_w = [], 1.0, {}
    for k, (d_sig, d_exec, i0) in enumerate(rebal):
        d_next = rebal[k + 1][1] if k + 1 < len(rebal) else None
        picks = scores.get(d_sig) or []
        # 全部有开盘价的候选
        cands = [(c, s, uw) for c, s, uw in picks if _px(px, c, d_exec) is not None]
        sel = cands[:topn]
        if len(sel) < max(5, topn // 4):
            continue
        raw = np.array([max(1e-6, s) for _, s, _ in sel])
        w = raw / raw.sum()
        for _ in range(50):
            over = w > 0.08
            if not over.any():
                break
            ex = (w[over] - 0.08).sum()
            w[over] = 0.08
            free = ~over
            if free.sum() == 0:
                break
            w[free] += ex * w[free] / w[free].sum()
        w = w / w.sum()
        wmap = {sel[i][0]: float(w[i]) for i in range(len(sel))}
        allc = set(wmap) | set(prev_w)
        turnover = sum(abs(wmap.get(c, 0.0) - prev_w.get(c, 0.0)) for c in allc) / 2.0

        def avg_ret(codes):
            rs = []
            for c in codes:
                p0 = _px(px, c, d_exec)
                p1 = _px(px, c, d_next) if d_next else None
                if p0 and p1 and p0 > 0:
                    rs.append(p1 / p0 - 1.0)
            return (sum(rs) / len(rs)) if rs else None

        port = avg_ret([c for c, _s, _u in sel])
        if port is None:
            prev_w = wmap
            continue
        net = port - turnover * COST_ROUND

        # ── 四个基准 ──
        pool_codes = [c for c, _s, _u in cands]
        b0 = avg_ret(pool_codes)                      # 池等权（含幸存者偏误）
        b1 = avg_ret([c for c, _s, _u in sel])         # 同股票集合等权
        # B3 随机抽样：在候选池中随机抽同样多只
        rnd = rng.choice(len(cands), size=min(len(cands), max(topn * 3, 60)), replace=False)
        b3 = avg_ret([cands[i][0] for i in rnd])       # 随机打分等权

        nav *= (1.0 + net)
        periods.append({"exec_date": d_exec, "next_date": d_next, "net": net,
                        "port": port, "B0_pool": b0, "B1_held": b1, "B3_random": b3,
                        "turnover": turnover, "nav": nav,
                        "n_held": len(sel), "n_pool": len(cands)})
        prev_w = wmap
    return periods


def stats(periods, hold, key):
    v = [(p["net"], p[key]) for p in periods if p.get(key) is not None]
    if len(v) < 6:
        return None
    ann = 244.0 / hold
    net = np.array([x for x, _ in v])
    bm = np.array([y for _, y in v])
    ex = net - bm
    sig_e = float(ex.std(ddof=1) * math.sqrt(ann))
    navx = np.cumprod(1.0 + ex)
    peak = np.maximum.accumulate(navx)
    mdd = float((navx / peak - 1.0).min())
    b = float(np.cov(net, bm, ddof=1)[0, 1] / bm.var(ddof=1)) if bm.var(ddof=1) > 1e-12 else None
    return {
        "n": len(v),
        "bench_ann": float((1 + bm.mean()) ** ann - 1),
        "excess_ann": float((1 + ex.mean()) ** ann - 1),
        "ir": float(ex.mean() / ex.std(ddof=1) * math.sqrt(ann)) if ex.std(ddof=1) > 1e-12 else None,
        "sigma_excess_ann": sig_e,
        "excess_mdd": mdd,
        "beta": b,
    }


def main():
    print("加载 ...")
    panel = json.load(open(PANEL, encoding="utf-8"))
    px = load_prices()
    bench_px = {b: load_bench(b) for b in BENCHES if load_bench(b)}
    wc = json.load(open(WEIGHTS, encoding="utf-8"))
    wmap = {k: float(v) for k, v in wc["weights"].items()}
    dim_max = {k: float(v) for k, v in wc["dim_max"].items()}
    scores = build_scores(panel, wmap, dim_max)
    print(f"  {len(panel['dates'])} 截面，权重 {wc['version']}")

    print("\n" + "═" * 100)
    print("Q1  四个基准下的表现（top20_h10 打分加权 cap0.08）")
    print("═" * 100)
    pers = run(panel, scores, px, bench_px, 20, 10)
    bench_def = {
        "B0_pool": "池等权（含幸存者偏误，当前主基准）",
        "B1_held": "**仅持仓股**等权（同股票集合，隔离选股 alpha）",
        "B3_random": "池内随机抽样等权（打分是否有效的直接检验）",
    }
    res = {}
    print(f"{'基准':14s}{'定义':38s}{'基准年化':>10s}{'超额年化':>10s}{'IR':>7s}"
          f"{'σ超额':>9s}{'超额MDD':>10s}{'β':>7s}")
    print("-" * 100)
    for k, desc in bench_def.items():
        s = stats(pers, 10, k)
        if not s:
            continue
        res[k] = s
        print(f"{k:14s}{desc:38s}{s['bench_ann']*100:>+9.2f}%{s['excess_ann']*100:>+9.2f}%"
              f"{s['ir']:>+7.2f}{s['sigma_excess_ann']*100:>8.1f}%"
              f"{s['excess_mdd']*100:>+9.2f}%{(s['beta'] or 0):>7.3f}")

    # 对真实指数
    print("\n  对真实指数（含池前视偏误，仅作对照）：")
    for bc in BENCHES:
        bp = bench_px.get(bc)
        if not bp:
            continue
        br = []
        for p in pers:
            b0 = _bpx(bp, p["exec_date"])
            b1 = _bpx(bp, p["next_date"]) if p["next_date"] else None
            br.append((b1 / b0 - 1.0) if (b0 and b1 and b0 > 0) else None)
        v = [(p["net"], b) for p, b in zip(pers, br) if b is not None]
        if len(v) < 6:
            continue
        net = np.array([x for x, _ in v]); bm = np.array([y for _, y in v])
        ex = net - bm
        ann = 244.0 / 10
        ir = float(ex.mean() / ex.std(ddof=1) * math.sqrt(ann)) if ex.std(ddof=1) > 1e-12 else None
        b = float(np.cov(net, bm, ddof=1)[0, 1] / bm.var(ddof=1)) if bm.var(ddof=1) > 1e-12 else None
        res[bc] = {"n": len(v), "bench_ann": float((1 + bm.mean()) ** ann - 1),
                   "excess_ann": float((1 + ex.mean()) ** ann - 1), "ir": ir, "beta": b}
        print(f"    {bc}  基准年化 {(1+bm.mean())**ann-1:+7.2%}  "
              f"超额年化 {(1+ex.mean())**ann-1:+7.2%}  IR {ir:+5.2f}  β {b:.3f}")

    # ═══ Q2  关键裁决 ═══
    print("\n" + "═" * 100)
    print("Q2  关键裁决")
    print("═" * 100)
    s0 = res.get("B0_pool")
    s1 = res.get("B1_held")
    s3 = res.get("B3_random")
    if s1 and s0:
        print(f"  · B0（池等权）      超额年化 {s0['excess_ann']*100:+.2f}%  IR {s0['ir']:+.2f}  "
              f"超额MDD {s0['excess_mdd']*100:+.2f}%")
        print(f"  · B1（同持仓等权）  超额年化 {s1['excess_ann']*100:+.2f}%  IR {s1['ir']:+.2f}  "
              f"超额MDD {s1['excess_mdd']*100:+.2f}%")
        print(f"  · B3（随机抽样等权）超额年化 {s3['excess_ann']*100:+.2f}%  IR {s3['ir']:+.2f}  "
              f"超额MDD {s3['excess_mdd']*100:+.2f}%" if s3 else "")
        print()
        if s1["ir"] > 0.3:
            print("  ⇒ B1（同股票集合）仍有明显正 IR → **选股 alpha 真实存在**，")
            print("     之前对 POOL_EW 的超额不是纯口径产物")
        elif s1["ir"] > 0.05:
            print("  ⇒ B1（同股票集合）IR 微弱 → 选股 alpha 存在但很弱，")
            print("     之前的大额超额主要来自「选了另一批股票」")
        else:
            print("  ⇒ B1（同股票集合）几乎无超额 → **打分排序本身几乎不产生 alpha**，")
            print("     之前对 POOL_EW 的超额主要是「选股口径不一致」的产物")
        if s3:
            d = s0["ir"] - s3["ir"]
            print(f"  ⇒ 组合对随机打分基准的 IR 优势 = {d:+.3f}"
                  f"（B0 IR {s0['ir']:+.2f} vs B3 IR {s3['ir']:+.2f}）")
            if d > 0.2:
                print("     打分显著优于随机 → 排序有效")
            elif d > 0.05:
                print("     打分略优于随机 → 排序有微弱价值")
            else:
                print("     ⚠️ **打分不优于随机** → 9 维排序的有效性本身存疑")

    print("\n  【β=0.32 的解释】")
    if s0 and s0.get("beta"):
        print(f"    组合对池等权 β = {s0['beta']:.3f}")
        print(f"    组合对随机抽样 β = {s3['beta']:.3f}" if s3 else "")
        print("    ⇒ β<1 说明组合系统性持有**比池平均更抗跌/更小盘**的股票。")
        print("      这既可能是「低波+低估值选股的结果」，也可能是「池含幸存者偏误」的产物。")
        print("      判据在 B3：若对随机抽样也低 β，说明是选出来的股票本身的属性（可交易）；")
        print("      若对随机抽样 β≈1，说明是池等权口径失真（不可归因）。")

    json.dump({
        "meta": {"round": 79,
                 "purpose": "池稳健性：同口径基准对照 + 打分有效性直接检验",
                 "panel": os.path.relpath(PANEL, HERE),
                 "weights_version": wc["version"],
                 "n_dates": len(panel["dates"]),
                 "bench_defs": bench_def},
        "results": res,
    }, open(OUT, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"\n→ {OUT}")


if __name__ == "__main__":
    main()
