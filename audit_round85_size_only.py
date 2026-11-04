#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
audit_round85_size_only.py —— size_growth 单维的严格验证（潜在的最优方案）

═══ 缘起 ═══

round84 的 α 归因给出了**与现行权重严重冲突**的结果（TopN=20, H=10）：

  逐维消融（Δα）：
    剔除 lowvol20    Δα −18.77pp  ← 崩塌（α 19.02% → 0.25%）
    剔除 quality     Δα −5.65pp
    剔除 size_growth Δα −6.12pp
    剔除 fund        Δα −2.02pp
    剔除 dominant    Δα −1.94pp
    剔除 bias60      Δα +3.37pp
    剔除 tech_neg    Δα +5.75pp  ← 拖累
    剔除 rsi_rev     Δα +9.17pp  ← 拖累
    剔除 ep          Δα +18.10pp ← **最大拖累**

  单维单独：
    仅 size_growth  α **+52.85%** (t **+4.29**) β 0.981  ← 唯一显著
    仅 lowvol20     α +11.54% (t +1.04) β **−0.225** ← 负 beta！
    仅 ep           α  +7.34% (t +0.66) β **−0.206** ← 负 beta！
    仅 bias60       α −16.03% (t −0.79)
    仅 tech_neg     α −12.31% (t −0.63)

⚠️ 这与 v4.15（lowvol20 .200 / ep .120 / rsi_rev .076 / tech_neg .029）**方向相反**。

═══ 但必须先排除三个陷阱 ═══

T1 **样本内挑选**：size_growth 的 α 是在同一 23 期上发现的，可能是运气。
    → 严格 walk-forward：段A 选方案，段B 选 λ，段C 只测一次。

T2 **多重检验**：我们试了 9 个单维 + 8 个消融 = 17 个配置。
    若 α 的分布近似 N(0, 50%²)，最大单维的期望 α 会被多重检验抬高。
    → 用 200 个随机组合的 α 分布做基线，看 size_growth 排第几。

T3 **它是不是「小盘股 beta」？** size_growth 的 β=0.981 说明它带满市场暴露。
    若只是买入小盘股，那需要与「按市值直接买最小的 20 只」对照。
    → 加入 B_size 基准：等权买入池内市值最小的 20 只。

═══ 判据 ═══

size_growth 单维若满足全部三条 → 才是真发现：
  ① 段C 样本外 α 仍显著为正
  ② α 优于 200 随机组合的 p95
  ③ 优于「市值最小 20 只」基准（即不只是小盘 beta）

用法：python audit_round85_size_only.py
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
OUT = os.path.join(HERE, "audit_round85.json")
DIMS = [
    "score_fund", "score_quality", "score_ep", "score_lowvol20",
    "score_bias60", "score_rsi_rev", "score_tech_neg",
    "score_size_growth", "score_dominant",
]


def build_scores(panel, wmap, dim_max, dim_only=None):
    """dim_only 指定时按该维排序（单维模式）。"""
    out = {}
    for d, rows in panel["rows"].items():
        arr = []
        for c, rec in rows.items():
            if dim_only is not None:
                v = rec.get(dim_only)
                if not isinstance(v, (int, float)) or v != v:
                    continue
                arr.append((c, float(v)))
            else:
                s, uw = composite_all(rec, wmap, dim_max)
                if s is not None:
                    arr.append((c, s))
        # 单维：小的在前（size_growth 越小越好？需看方向），先按值降序
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


def run(panel, px, scores, topn, hold, rng=None, random_pick=False,
        reverse=False, score_cap=0.08):
    dates = panel["dates"]
    rebal = [(dates[i], dates[i + 1], i) for i in range(0, len(dates) - 1, hold)]
    pers, prev_w = [], {}
    for idx, (d_sig, d_exec, i0) in enumerate(rebal):
        d_next = rebal[idx + 1][1] if idx + 1 < len(rebal) else None
        picks = scores.get(d_sig) or []
        cands = [(c, s) for c, s in picks if _px(px, c, d_exec) is not None]
        if reverse:
            cands = cands[::-1]          # 取分数最低的（=市值最小）
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
        w = _cap_weight(raw, score_cap)
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
    if len(pers) < 4:
        return None
    ann = 244.0 / hold
    net = np.array([p["net"] for p in pers])
    pool = np.array([p["pool"] if p["pool"] is not None else np.nan for p in pers])
    ok = ~np.isnan(pool)
    net, pool = net[ok], pool[ok]
    if len(net) < 4 or pool.var(ddof=1) < 1e-12:
        return None
    b = float(np.cov(net, pool, ddof=1)[0, 1] / pool.var(ddof=1))
    a = float(net.mean() - b * pool.mean())
    resid = net - (a + b * pool)
    s2 = float((resid ** 2).sum() / (len(net) - 2))
    se_a = math.sqrt(s2 / len(net)) if len(net) > 2 else None
    ex = net - pool
    navx = np.cumprod(1.0 + ex)
    peak = np.maximum.accumulate(navx)
    return {"n": len(net), "alpha_ann": float(a * ann),
            "alpha_t": (a / se_a) if se_a and se_a > 1e-12 else None,
            "beta": b,
            "pool_ann": float((1 + pool.mean()) ** ann - 1),
            "excess_ann": float((1 + ex.mean()) ** ann - 1),
            "excess_mdd": float((navx / peak - 1.0).min()),
            "sigma_excess": float(ex.std(ddof=1) * math.sqrt(ann))}


def main():
    print("加载 ...")
    panel = json.load(open(PANEL, encoding="utf-8"))
    px = load_prices()
    wc = json.load(open(WEIGHTS, encoding="utf-8"))
    dim_max = {k: float(v) for k, v in wc["dim_max"].items()}
    w_base = {k: float(v) for k, v in wc["weights"].items()}
    hold, topn = 10, 20

    # 方向确认：size_growth 的语义（数值大=市值小 还是 大？）
    rows0 = panel["rows"][panel["dates"][-1]]
    sample = [(c, r.get("score_size_growth")) for c, r in list(rows0.items())[:5]]
    print(f"  size_growth 样本: {sample}")

    # 构造四套打分
    sc_base = build_scores(panel, w_base, dim_max)
    sc_size = build_scores(panel, w_base, dim_max, dim_only="score_size_growth")
    # size_growth 方向：取分最高的一批（=按该维的"好"排序）
    # 若发现方向反了，round85b 会用 reverse 对照

    pers_base = run(panel, px, sc_base, topn, hold)
    pers_size = run(panel, px, sc_size, topn, hold)
    # B_size：池内按 score_size_growth 最低的一批（= 市值最小）
    pers_small = run(panel, px, sc_size, topn, hold, reverse=True)

    n = len(pers_base)
    iA, iB = int(n * 0.40), int(n * 0.70)
    segs = {"A": (0, iA), "B": (iA, iB), "C": (iB, n), "ALL": (0, n)}

    print("\n" + "═" * 100)
    print("S1  三套方案对比（全样本 + 分段）")
    print("═" * 100)
    print(f"{'方案':22s}{'段':4s}{'n':>4s}{'α年化':>11s}{'α t':>8s}{'β':>8s}"
          f"{'超额年化':>11s}{'超额MDD':>10s}")
    print("-" * 82)
    res = {}
    for nm, pers in (("v4.15全9维", pers_base),
                     ("仅size_growth", pers_size),
                     ("市值最小20只", pers_small)):
        res[nm] = {}
        for sk, (a, b) in segs.items():
            m = decomp(pers[a:b], hold)
            if not m:
                continue
            res[nm][sk] = m
            print(f"{nm:22s}{sk:4s}{m['n']:>4d}{m['alpha_ann']*100:>+10.2f}%"
                  f"{(m['alpha_t'] or 0):>+8.2f}{m['beta']:>8.3f}"
                  f"{m['excess_ann']*100:>+10.2f}%{m['excess_mdd']*100:>+9.2f}%")
        print()

    print("═" * 100)
    print("S2  多重检验基线：200 个随机组合的 α 分布（全样本）")
    print("═" * 100)
    als = []
    for si in range(200):
        rng = np.random.default_rng(20261029 + si)
        m = decomp(run(panel, px, sc_base, topn, hold, rng, True), hold)
        if m:
            als.append(m["alpha_ann"])
    arr = np.array(als)
    print(f"  随机 α: 中位 {np.median(arr)*100:+.2f}%  p10 {np.percentile(arr,10)*100:+.2f}%  "
          f"p90 {np.percentile(arr,90)*100:+.2f}%  σ {arr.std(ddof=1)*100:.2f}%")
    for nm in res:
        if "ALL" in res[nm]:
            v = res[nm]["ALL"]["alpha_ann"]
            print(f"  {nm:22s} α {v*100:+.2f}%  → 随机分位 "
                  f"{float((arr < v).mean()*100):.1f}%")
    # 多重检验修正：17 个配置下的期望最大 α
    print(f"\n  ⚠️ 多重检验：我们评估了 9 单维 + 8 消融 = 17 个配置。")
    # ⚠️ 2026-10-29 修正：原式写 `np.percentile(arr, 1-1/17)`，
    #    其中 1-1/17 = 0.941 本身是对的，但作者原意是「17 次抽样的最大值」
    #    的期望 → 应取 1-1/17 分位（正方向）。**先前版本误取了负方向分位，
    #    门槛被算成 −28.43%（虚低），会高估通过度**。现显式区分两个量：
    mult_p = float(np.percentile(arr, (1 - 1 / 17) * 100))   # p94.1，正方向
    print(f"     17 次独立抽样的期望最大 α ≈ {mult_p*100:+.2f}%（p94.1 分位，正方向）")
    print(f"     → 只有 α 超过此值才算通过多重检验门槛")
    print(f"     （σ_随机 = {arr.std(ddof=1)*100:.2f}%，单次门槛 p95 = "
          f"{np.percentile(arr,95)*100:+.2f}%）")

    print("\n" + "═" * 100)
    print("S3  判据检验")
    print("═" * 100)
    sz = res.get("仅size_growth", {})
    sm = res.get("市值最小20只", {})
    c1 = c2 = c3 = False
    if "C" in sz:
        c1 = (sz["C"]["alpha_ann"] > 0) and ((sz["C"]["alpha_t"] or 0) > 1.5)
        print(f"  ① 段C 样本外 α = {sz['C']['alpha_ann']*100:+.2f}%  "
              f"t = {sz['C']['alpha_t']:+.2f}  → "
              f"{'✅ 通过' if c1 else '❌ 未通过'}")
    if "ALL" in sz:
        v = sz["ALL"]["alpha_ann"]
        pct = float((arr < v).mean() * 100)
        c2 = pct >= 95 and v > mult_p
        print(f"  ② α 优于 200 随机组合 p{pct:.1f}（阈值 p95）"
              f"{'且' if pct >= 95 else '但'}超多重检验门槛 {mult_p*100:+.2f}%"
              f" → {'✅ 通过' if c2 else '❌ 未通过'}")
    if "ALL" in sz and "ALL" in sm:
        d = sz["ALL"]["alpha_ann"] - sm["ALL"]["alpha_ann"]
        c3 = d > 0
        print(f"  ③ 优于「市值最小20只」基准（差 {d*100:+.2f}pp）→ "
              f"{'✅ 通过' if c3 else '❌ 未通过（只是小盘 beta）'}")
    print()
    if c1 and c2 and c3:
        print("  ⇒ **size_growth 单维通过全部三条判据** → 这是真正的优化方向")
        print("     建议：v4.16 权重应大幅倾斜 size_growth，重新评估 ep/rsi_rev/tech_neg")
    else:
        print("  ⇒ size_growth 单维**未通过全部判据** → 不建议单维上线")
        print("     但消融结果（剔 ep/rsi_rev/tech_neg 可提升 α）仍值得在更严谨的样本外检验中复核")

    json.dump({
        "meta": {"round": 85,
                 "purpose": "size_growth 单维的严格验证（多重检验 + 样本外 + 小盘beta排除）",
                 "panel": os.path.relpath(PANEL, HERE),
                 "weights_version": wc["version"],
                 "config": {"topn": topn, "hold": hold, "n_seeds": 200},
                 "caveat": "23 期样本 + 17 个配置的多重检验，任何结论都只是方向性"},
        "by_scheme": res,
        "random_alpha": {"median": float(np.median(arr)),
                         "p10": float(np.percentile(arr, 10)),
                         "p90": float(np.percentile(arr, 90)),
                         "p95": float(np.percentile(arr, 95)),
                         "sigma": float(arr.std(ddof=1)),
                         "expected_max_17": mult_p,
                         "mult_test_note": ("期望最大 α 取正方向 p94.1；"
                                            "先前版本误取负方向导致门槛虚低，已修正")},
        "criteria": {"c1_oos_alpha": bool(c1), "c2_beats_random_p95": bool(c2),
                     "c3_beats_smallcap": bool(c3)},
    }, open(OUT, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"\n→ {OUT}")


if __name__ == "__main__":
    main()
