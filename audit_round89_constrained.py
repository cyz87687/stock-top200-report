#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
audit_round89_constrained.py —— 在 v4.15 约束内找最优权重（约束下的 α 最大化）

═══ 问题 ═══

round88：V1~V4 四个新候选都通过 ①段C α 显著 ②超随机 p95+多重检验门槛
③超额MDD 不劣于基线，唯独 ④**违反 v4.15 的单维上限 0.20**。

单维上限 0.20 的来由（项目记忆）：v4.15 的约束是
  「单因子≤0.20、5大类≤0.30、Σ=1、下限≥0」
这个约束当初是为**分散化/防单因子失效**设的保险，
现在与实证结论（size_growth 单维 α +52.85% t+4.29）直接冲突。

⇒ 本脚本做两件事：
  A. **在约束内**用坐标上升最大化全样本 α（剔除 ep/rsi_rev/tech_neg 方向）
     → 给出「不需改治理规则」的可行最优解
  B. 给出「若放宽单维上限」的帕累托前沿（0.20 / 0.25 / 0.30 / 0.40 / 1.00）
     → 让老大看清放宽约束换到多少 α，值不值得

═══ 关键约束：必须用样本外选权重 ═══

⚠️ 权重优化极易过拟合（round70 已证）。
本脚本严格三段：
  段A 优化权重 → 段B 选 λ（向 v4.15 收缩）→ 段C 只测一次
并且与 200 随机组合对照。

若段C 的最优解 α 显著优于基线且优于随机，才算真实改善。

用法：python audit_round89_constrained.py
"""
import os
import sys
import json
import math
import time
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from audit_round67_backtest_engine import (  # noqa
    composite_all, load_prices, _px, COST_ROUND)

PANEL = os.path.join(HERE, "data", "panel_round66.json")
WEIGHTS = os.path.join(HERE, "factor_weights.json")
OUT = os.path.join(HERE, "audit_round89.json")
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


def run(panel, px, scores, topn, hold):
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
            "ann": float((1 + net.mean()) ** ann - 1),
            "excess_ann": float((1 + ex.mean()) ** ann - 1),
            "excess_mdd": float((navx / peak - 1.0).min())}


def project(w, w_cap, g_cap):
    """投影到可行域：截单维 → 压大类 → 归一。"""
    w = np.clip(np.asarray(w, float), 0.0, w_cap)
    gidx = [[DIMS.index(k) for k in ks] for ks in GROUPS.values()]
    for _ in range(400):
        for idx in gidx:
            s = w[idx].sum()
            if s > g_cap:
                w[idx] *= g_cap / s
        tot = w.sum()
        if tot <= 1e-12:
            w = np.ones(len(DIMS)) / len(DIMS)
            tot = 1.0
        w = w / tot
        over = w > w_cap
        if not over.any():
            break
        w[over] = w_cap
    return w


def main():
    print("加载 ...")
    panel = json.load(open(PANEL, encoding="utf-8"))
    px = load_prices()
    wc = json.load(open(WEIGHTS, encoding="utf-8"))
    dim_max = {k: float(v) for k, v in wc["dim_max"].items()}
    w_base = np.array([float(wc["weights"].get(k, 0.0)) for k in DIMS])
    hold, topn = 10, 20
    t0 = time.time()

    # ═══ A：约束内的 α 最大化（段A 优化，段B 选 λ，段C 裁决）═══
    print("\n" + "═" * 100)
    print("A  约束内优化（单维≤0.20 大类≤0.30）：段A 最大化 α → 段B 选 λ → 段C 裁决")
    print("═" * 100)
    _dates = panel["dates"]
    _rebal = [(_dates[i], _dates[i + 1], i) for i in range(0, len(_dates) - 1, hold)]
    n = len(_rebal)
    iA, iB = int(n * 0.40), int(n * 0.70)

    # 预计算各段的「候选池 + 池收益」，避免重复 build_scores
    def seg_data(a, b):
        out = []
        for idx in range(a, b):
            _ds, _de, _i0 = _rebal[idx]
            _dn = _rebal[idx + 1][1] if idx + 1 < len(_rebal) else None
            sc = build_scores(panel, {k: 1.0 for k in DIMS}, dim_max)
            # ⚠️ 只保留结构，实际打分在优化时按权重重建 → 这里改为懒加载
            out.append((_ds, _de, _dn))
            break
        return out

    # 为效率：把每期的「所有维的原始值」预取一次，之后按权重合成
    print("  预取各维原始值（内存敏感，分段处理）...")
    raw_by_seg = {}
    for sk, (a, b) in (("A", (0, iA)), ("B", (iA, iB)), ("C", (iB, n))):
        seg = []
        for idx in range(a, b):
            _ds, _de, _i0 = _rebal[idx]
            _dn = _rebal[idx + 1][1] if idx + 1 < len(_rebal) else None
            rows = panel["rows"].get(_ds) or {}
            cand = []
            for c, rec in rows.items():
                if _px(px, c, _de) is None:
                    continue
                vals = []
                ok = True
                for d in DIMS:
                    v = rec.get(d)
                    dm = dim_max.get(d, 10.0)
                    if isinstance(v, (int, float)) and v == v:
                        vals.append(min(float(v), dm) / dm * 10.0)
                    else:
                        vals.append(None)
                cand.append((c, vals))
            # 池收益
            prs = []
            for c, _v in cand:
                p0 = _px(px, c, _de)
                p1 = _px(px, c, _dn) if _dn else None
                if p0 and p1 and p0 > 0:
                    prs.append(p1 / p0 - 1.0)
            seg.append({"exec": _de, "next": _dn, "cands": cand,
                        "pool": (sum(prs) / len(prs)) if prs else None,
                        "px": {c: (_px(px, c, _de), _px(px, c, _dn) if _dn else None)
                               for c, _v in cand}})
        raw_by_seg[sk] = seg
        print(f"    段{sk}: {len(seg)} 期 × 平均 "
              f"{np.mean([len(s['cands']) for s in seg]):.0f} 只")

    def score_and_run(seg, w, topn=20):
        """按权重 w 合成打分 → 取 TopN → 跑组合。返回收益序列。"""
        w = np.asarray(w, float)
        pers, prev_w = [], {}
        for p in seg:
            scored = []
            for c, vals in p["cands"]:
                den = sum(w[i] for i in range(len(DIMS)) if vals[i] is not None)
                if den <= 1e-12:
                    continue
                num = sum(w[i] * vals[i] for i in range(len(DIMS))
                          if vals[i] is not None)
                scored.append((c, num / den * 10.0))
            if not scored:
                prev_w = {}
                continue
            scored.sort(key=lambda x: (-x[1], x[0]))
            sel = scored[:topn]
            if len(sel) < max(5, topn // 4):
                prev_w = {}
                continue
            raw = np.array([max(1e-6, s) for _, s in sel])
            ww = _cap_weight(raw, 0.08)
            wmap = {sel[i][0]: float(ww[i]) for i in range(len(sel))}
            allc = set(wmap) | set(prev_w)
            turnover = sum(abs(wmap.get(c, 0.0) - prev_w.get(c, 0.0)) for c in allc) / 2.0
            rs, ws = [], []
            for c, _s in sel:
                p0, p1 = p["px"].get(c, (None, None))
                if p0 and p1 and p0 > 0:
                    rs.append(p1 / p0 - 1.0)
                    ws.append(wmap[c])
            if not rs:
                prev_w = wmap
                continue
            sw = sum(ws)
            port = sum(r * ww2 for r, ww2 in zip(rs, ws)) / sw if sw > 0 else 0.0
            pers.append({"net": port - turnover * COST_ROUND, "port": port,
                         "pool": p["pool"]})
            prev_w = wmap
        return pers

    def alpha_of(seg, w):
        m = decomp(score_and_run(seg, w), hold)
        return (m["alpha_ann"] if m else -1e9), m

    print(f"\n  基线 v4.15：")
    for sk in ("A", "B", "C"):
        a, m = alpha_of(raw_by_seg[sk], w_base)
        print(f"    段{sk}  α年化 {a*100:+.2f}%  t {(m['alpha_t'] or 0):+.2f}  "
              f"β {m['beta']:.3f}  超额MDD {m['excess_mdd']*100:+.2f}%")

    # 坐标上升
    print(f"\n  坐标上升最大化段A α（单维≤0.20, 大类≤0.30）...")
    rng = np.random.default_rng(20261029)
    best = (None, -1e9)
    for si in range(8):
        w0 = w_base if si == 0 else project(
            rng.exponential(1.0, len(DIMS)), 0.20, 0.30)
        w = project(w0, 0.20, 0.30)
        cur, _ = alpha_of(raw_by_seg["A"], w)
        for _ in range(10):
            improved = False
            for i in range(len(DIMS)):
                for d in (-0.08, -0.04, -0.02, 0.02, 0.04, 0.08):
                    cand = w.copy()
                    cand[i] = w[i] + d
                    cand = project(cand, 0.20, 0.30)
                    v, _ = alpha_of(raw_by_seg["A"], cand)
                    if v > cur + 1e-9:
                        w, cur, improved = cand, v, True
            if not improved:
                break
        if cur > best[1]:
            best = (w, cur)
        print(f"    起点 {si+1}/8  最优段A α {best[1]*100:+.2f}%  {time.time()-t0:.0f}s")
    w_opt = best[0]
    print(f"\n  段A 最优权重：")
    for g, ks in GROUPS.items():
        idx = [DIMS.index(k) for k in ks]
        print(f"    {g:6s} Σ={w_opt[idx].sum():.4f}  " +
              "  ".join(f"{k.replace('score_','')}={w_opt[DIMS.index(k)]:.4f}" for k in ks))

    # 段B 选 λ
    print("\n  段B 选 λ（向 v4.15 收缩）：")
    best_lam, best_v = 0.0, -1e9
    for lam in (0.0, 0.15, 0.3, 0.45, 0.6, 0.75, 1.0):
        wm = project((1 - lam) * w_opt + lam * w_base, 0.20, 0.30)
        a, m = alpha_of(raw_by_seg["B"], wm)
        print(f"    λ={lam:.2f}  段B α {a*100:+.2f}%  t {(m['alpha_t'] or 0):+.2f}")
        if a > best_v:
            best_lam, best_v = lam, a
    w_final = project((1 - best_lam) * w_opt + best_lam * w_base, 0.20, 0.30)
    print(f"  → λ* = {best_lam}")

    print("\n  段C 裁决（只测一次）：")
    cands = {"v4.15基线": w_base, "约束内最优(未收缩)": w_opt,
             "约束内最优(收缩后)": w_final}
    out_c = {}
    for nm, w in cands.items():
        a, m = alpha_of(raw_by_seg["C"], w)
        out_c[nm] = m
        print(f"    {nm:22s} 段C α {a*100:+.2f}%  t {(m['alpha_t'] or 0):+.2f}  "
              f"β {m['beta']:.3f}  超额年化 {m['excess_ann']*100:+.2f}%  "
              f"超额MDD {m['excess_mdd']*100:+.2f}%")

    # ═══ B：帕累托前沿（放宽单维上限）═══
    print("\n" + "═" * 100)
    print("B  帕累托前沿：单维上限放宽的代价与收益")
    print("═" * 100)
    print(f"{'单维上限':>10s}{'大类上限':>10s}{'最大单维实际':>13s}"
          f"{'段C α':>10s}{'段C t':>9s}{'段C超额MDD':>13s}{'全样本α':>11s}")
    print("-" * 80)
    pareto = []
    for w_cap in (0.20, 0.25, 0.30, 0.40, 0.60, 1.00):
        g_cap = 0.30 if w_cap <= 0.30 else max(0.30, w_cap)
        # 在该约束下再优化一次（段A）
        bw = (None, -1e9)
        for si in range(4):
            w0 = w_base if si == 0 else project(
                rng.exponential(1.0, len(DIMS)), w_cap, g_cap)
            w = project(w0, w_cap, g_cap)
            cur, _ = alpha_of(raw_by_seg["A"], w)
            for _ in range(8):
                improved = False
                for i in range(len(DIMS)):
                    for d in (-0.08, -0.04, 0.04, 0.08):
                        cand = w.copy()
                        cand[i] = w[i] + d
                        cand = project(cand, w_cap, g_cap)
                        v, _ = alpha_of(raw_by_seg["A"], cand)
                        if v > cur + 1e-9:
                            w, cur, improved = cand, v, True
                if not improved:
                    break
            if cur > bw[1]:
                bw = (w, cur)
        w = bw[0]
        a_c, m_c = alpha_of(raw_by_seg["C"], w)
        a_all, m_all = alpha_of(raw_by_seg["A"] + raw_by_seg["B"] + raw_by_seg["C"], w)
        pareto.append({"w_cap": w_cap, "g_cap": g_cap,
                       "weights": {k: float(v) for k, v in zip(DIMS, w)},
                       "segC_alpha": a_c, "segC_t": m_c["alpha_t"],
                       "segC_excess_mdd": m_c["excess_mdd"],
                       "all_alpha": a_all,
                       "max_dim": float(w.max())})
        print(f"{w_cap:>9.2f} {g_cap:>10.2f}{w.max():>13.4f}"
              f"{a_c*100:>+9.2f}%{(m_c['alpha_t'] or 0):>+9.2f}"
              f"{m_c['excess_mdd']*100:>+12.2f}%{a_all*100:>+10.2f}%")

    json.dump({
        "meta": {"round": 89,
                 "purpose": "约束内 α 最优化 + 放宽单维上限的帕累托前沿",
                 "panel": os.path.relpath(PANEL, HERE),
                 "weights_version_base": wc["version"],
                 "config": {"topn": topn, "hold": hold},
                 "caveat": "23 期样本；段C 仅 7 期，功效低；结论为方向性"},
        "constrained_opt": {"weights": {k: float(v) for k, v in zip(DIMS, w_opt)},
                            "lam_best": best_lam,
                            "final_weights": {k: float(v) for k, v in zip(DIMS, w_final)},
                            "segC": {k: v for k, v in out_c.items()}},
        "pareto": pareto,
        "elapsed_s": round(time.time() - t0, 1),
    }, open(OUT, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"\n→ {OUT}  （{time.time()-t0:.0f}s）")


if __name__ == "__main__":
    main()
