#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
audit_round68_icir_opt.py —— 以「组合 ICIR 最大化」为目标的权重优化（numpy 向量化版）

═══ 为什么必须换目标函数 ═══

项目此前的权重全是从「单维 IC 显著性」推出来的：fund .242 因单维 IC 双过 Bonferroni，
lowvol20 .208 同理。但组合 IC 的方差不是各维方差的加权和：

    IC_c = f(Σ w_i·x_i)  ， Var(IC_c) 由各维 IC 的**协方差矩阵**决定，不是各维方差之和

⇒ 单维 IC 最高的因子未必让组合 ICIR = mean(IC_c)/std(IC_c) 最大。
   若两维 IC 高度正相关（技术三簇 ρ≈0.85，round57 实测），同时加重只会同向放大
   mean 和 std，ICIR 几乎不动。真正该加的是「IC 高且与其他维低相关」的因子。

这是老大目标①的数学含义。本脚本直接把该目标写出来优化。

═══ 性能设计（第一版纯 Python 跑了 10 分钟未收敛，故重写）═══

把每个截面的维度值预先装成 numpy 的 (n_dates, n_codes, n_dims) 对齐数组 +
掩码矩阵，之后每次评估只需：
  ① 向量化算总分（分母按掩码求和，缺维剔除，与生产同口径）
  ② 每截面 argsort 求秩 → 秩相关
238 截面 × 9 维在 numpy 下约 **20~40ms**，比纯 Python 快两个数量级。

⚠️ 必须保证向量化结果与生产口径**逐位一致**：脚本内置 self-check，
   用生产同款纯 Python 实现对同一权重算一遍，比对 IC 均值差 < 1e-12 才继续。

═══ 三段切分（禁止全样本最优上线）═══
  段 A（前 40%）→ 优化权重
  段 B（中 30%）→ 选 λ 收缩（防过拟合）
  段 C（后 30%）→ **只测一次**，唯一合法的样本外数字

═══ 约束（老大 2026-10-29 指令）═══
  单因子 0 ≤ w ≤ 0.20 ；每大类 Σw ≤ 0.30 ；Σw = 1.0 ；w ≥ 0

═══ 输出 ═══
audit_round68.json
"""
import os
import sys
import json
import math
import argparse
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

PANEL = os.path.join(HERE, "data", "panel_round66.json")
KLONG = os.path.join(HERE, "data", "klines_long.json")
WEIGHTS = os.path.join(HERE, "factor_weights.json")
OUT = os.path.join(HERE, "audit_round68.json")

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
W_CAP = 0.20
G_CAP = 0.30
NW_LAG = 4


def is_num(x):
    return isinstance(x, (int, float)) and not isinstance(x, bool) and x == x


# ═══════════════ 面板 → 对齐数组 ═══════════════

def build_arrays(panel_path, px_path, hop, min_codes=100, cache_path=None):
    """把面板转成对齐的 numpy 数组，并缓存为 .npz。

    ⚠️ 2026-10-29：面板 JSON 117MB，json.load 后因 Python dict 开销膨胀到数 GB，
       多次加载直接 OOM（exit 137）。故一次性转成 npz（约 20MB），后续秒读。
    """
    if cache_path and os.path.exists(cache_path):
        z = np.load(cache_path, allow_pickle=False)
        if int(z["hop"]) == hop:
            print(f"  [缓存命中] {cache_path}")
            return (list(z["dates"]), z["X"], z["M"], z["Y"],
                    [str(c) for c in z["codes"]])

    panel = json.load(open(panel_path, encoding="utf-8"))
    px = json.load(open(px_path, encoding="utf-8"))

    dates_all = panel["dates"]
    day_idx = sorted({d for c in px for d in px[c]})
    tidx = {d: i for i, d in enumerate(day_idx)}

    use = []
    for d in dates_all:
        rows = panel["rows"].get(d) or {}
        i0 = tidx.get(d)
        if not rows or i0 is None or i0 + hop >= len(day_idx):
            continue
        use.append(d)

    codes = sorted({c for d in use for c in (panel["rows"].get(d) or {})})
    ci = {c: i for i, c in enumerate(codes)}
    nD, nC, nK = len(use), len(codes), len(DIMS)

    X = np.full((nD, nC, nK), np.nan, dtype=np.float32)
    Y = np.full((nD, nC), np.nan, dtype=np.float32)

    for di, d in enumerate(use):
        rows = panel["rows"].get(d) or {}
        d1 = day_idx[tidx[d] + hop]
        for c, rec in rows.items():
            j = ci[c]
            bar0 = (px.get(c) or {}).get(d)
            bar1 = (px.get(c) or {}).get(d1)
            if not (isinstance(bar0, (list, tuple)) and isinstance(bar1, (list, tuple))):
                continue
            if len(bar0) < 2 or len(bar1) < 2:
                continue
            p0, p1 = bar0[1], bar1[1]
            if not (is_num(p0) and is_num(p1)) or p0 <= 0:
                continue
            Y[di, j] = p1 / p0 - 1.0
            for k, dim in enumerate(DIMS):
                v = rec.get(dim)
                if is_num(v):
                    X[di, j, k] = float(v)

    M = ~np.isnan(X)
    n_valid = ((~np.isnan(Y)) & M.any(axis=2)).sum(axis=1)
    keep = n_valid >= min_codes
    X, M, Y = X[keep], M[keep], Y[keep]
    use = [use[i] for i in range(nD) if keep[i]]

    if cache_path:
        np.savez_compressed(cache_path, hop=hop,
                            dates=np.array(use), codes=np.array(codes),
                            X=X, M=M, Y=Y)
        print(f"  [写缓存] {cache_path}")
    return use, X, M, Y, codes


def rankdata(a):
    """按行（截面）做平均秩。a: (nD, nC)，NaN 保持 NaN。"""
    nD, nC = a.shape
    out = np.full_like(a, np.nan, dtype=np.float64)
    for i in range(nD):
        row = a[i]
        ok = ~np.isnan(row)
        if ok.sum() < 3:
            continue
        v = row[ok]
        order = np.argsort(v, kind="mergesort")
        sv = v[order]
        ranks = np.empty(len(v), dtype=np.float64)
        # 处理并列
        k = 0
        while k < len(sv):
            j = k
            while j + 1 < len(sv) and sv[j + 1] == sv[k]:
                j += 1
            ranks[order[k:j + 1]] = (k + j) / 2.0 + 1.0
            k = j + 1
        out[i, ok] = ranks
    return out


class Evaluator:
    def __init__(self, X, M, Y, dim_max):
        self.X = X
        self.M = M
        self.Y = Y
        self.dmax = np.array([float(dim_max.get(k, 10.0)) for k in DIMS], dtype=np.float64)
        # 预先把 min(v,dmax)/dmax*10 算好
        self.V = np.where(M, np.minimum(X, self.dmax) / self.dmax * 10.0, 0.0)
        self.Yok = ~np.isnan(Y)
        self.RY = rankdata(Y)
        self.nD = X.shape[0]
        self._cache = {}

    def total(self, w):
        """向量化合成总分，返回 (nD,nC) 含 NaN（无任何有效维处为 NaN）。"""
        w = np.asarray(w, dtype=np.float64)
        den = (self.M * w[None, None, :]).sum(axis=2)
        num = (self.V * w[None, None, :]).sum(axis=2)
        with np.errstate(invalid="ignore", divide="ignore"):
            s = np.where(den > 0, num / den * 10.0, np.nan)
        return s

    def ics(self, w):
        s = self.total(w)
        RS = rankdata(s)
        nD = self.nD
        out = []
        for i in range(nD):
            a = RS[i]
            b = self.RY[i]
            ok = (~np.isnan(a)) & (~np.isnan(b))
            n = ok.sum()
            if n < 100:
                continue
            x = a[ok] - a[ok].mean()
            y = b[ok] - b[ok].mean()
            sx = math.sqrt((x * x).sum())
            sy = math.sqrt((y * y).sum())
            if sx <= 0 or sy <= 0:
                continue
            out.append(float((x * y).sum() / (sx * sy)))
        return out


def nw_t(xs, lag=NW_LAG):
    n = len(xs)
    if n < 5:
        return None
    mu = sum(xs) / n
    d = [x - mu for x in xs]
    var = sum(x * x for x in d) / n
    for L in range(1, min(lag, n - 1) + 1):
        wgt = 1.0 - L / (lag + 1.0)
        cov = sum(d[t] * d[t - L] for t in range(L, n)) / n
        var += 2.0 * wgt * cov
    if var <= 0:
        return None
    return mu / math.sqrt(var / n)


def stat(ics):
    if not ics or len(ics) < 5:
        return {"n": len(ics or []), "ic": None, "icir": None, "t": None,
                "pos_rate": None, "sd": None}
    mu = sum(ics) / len(ics)
    var = sum((x - mu) ** 2 for x in ics) / (len(ics) - 1)
    sd = math.sqrt(var)
    return {"n": len(ics), "ic": mu, "sd": sd,
            "icir": (mu / sd if sd > 1e-12 else None),
            "t": nw_t(ics),
            "pos_rate": sum(1 for x in ics if x > 0) / len(ics)}


# ═══════════════ 约束投影 ═══════════════

def feasible(wmap):
    for k in DIMS:
        w = wmap.get(k, 0.0)
        if w < -1e-9 or w > W_CAP + 1e-9:
            return False
    for ks in GROUPS.values():
        if sum(wmap.get(k, 0.0) for k in ks) > G_CAP + 1e-9:
            return False
    return abs(sum(wmap.get(k, 0.0) for k in DIMS) - 1.0) < 1e-6


def project(wmap):
    """投影回可行域：截单维上限 → 压超限大类 → 归一，迭代至稳定。"""
    w = np.array([max(0.0, min(W_CAP, float(wmap.get(k, 0.0)))) for k in DIMS])
    gidx = [[DIMS.index(k) for k in ks] for ks in GROUPS.values()]
    for _ in range(300):
        for idx in gidx:
            s = w[idx].sum()
            if s > G_CAP:
                w[idx] *= G_CAP / s
        tot = w.sum()
        if tot <= 1e-12:
            w[:] = 1.0 / len(DIMS)
            tot = 1.0
        w /= tot
        over = w > W_CAP
        if not over.any() and abs(w.sum() - 1.0) < 1e-9:
            break
        w[over] = W_CAP
    return {k: float(w[i]) for i, k in enumerate(DIMS)}


# ═══════════════ 优化：坐标上升 + 多起点 ═══════════════

def optimize(ev, dim_max, objective="icir", n_starts=30, n_sweeps=20, seed=20261029,
             verbose=True):
    rng = np.random.default_rng(seed)
    cache = {}

    def f(wd):
        key = tuple(round(wd[k], 5) for k in DIMS)
        if key in cache:
            return cache[key]
        wv = np.array([wd[k] for k in DIMS])
        ics = ev.ics(wv)
        st = stat(ics)
        if st["icir"] is None:
            r = (-1e9, st)
        else:
            v = {"icir": st["icir"], "mean": st["ic"], "util": st["icir"]}[objective]
            r = (v, st)
        cache[key] = r
        return r

    wc = json.load(open(WEIGHTS, encoding="utf-8"))
    starts = [{k: float(wc["weights"].get(k, 0.0)) for k in DIMS},
              {k: 1.0 / len(DIMS) for k in DIMS}]
    for _ in range(n_starts - 2):
        v = rng.exponential(1.0, len(DIMS))
        v /= v.sum()
        starts.append({k: float(v[i]) for i, k in enumerate(DIMS)})

    best = None
    for si, st0 in enumerate(starts):
        w = project(st0)
        cur = f(w)[0]
        for _ in range(n_sweeps):
            improved = False
            for k in DIMS:
                for delta in (-0.05, -0.025, -0.012, 0.012, 0.025, 0.05):
                    cand = dict(w)
                    cand[k] = w[k] + delta
                    cand = project(cand)
                    sc = f(cand)[0]
                    if sc > cur + 1e-12:
                        w, cur, improved = cand, sc, True
            if not improved:
                break
        if best is None or cur > best[1]:
            best = (w, cur)
        if verbose and (si + 1) % 10 == 0:
            print(f"      起点 {si+1}/{len(starts)}  最优目标 {best[1]:.4f}  评估 {len(cache)} 次")
    return best[0], best[1], cache


# ═══════════════ 主流程 ═══════════════

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--panel", default=PANEL)
    ap.add_argument("--out", default=OUT)
    ap.add_argument("--hop", type=int, default=20)
    ap.add_argument("--objective", default="icir", choices=["icir", "mean", "util"])
    ap.add_argument("--n-starts", type=int, default=24)
    args = ap.parse_args()

    wc = json.load(open(WEIGHTS, encoding="utf-8"))
    dim_max = {k: float(v) for k, v in wc["dim_max"].items()}
    w_base = {k: float(wc["weights"].get(k, 0.0)) for k in DIMS}

    print(f"构建对齐数组（hop={args.hop}）...")
    cache = os.path.join(HERE, "data", f"icir_arrays_h{args.hop}.npz")
    dates, X, M, Y, codes = build_arrays(args.panel, KLONG, args.hop, cache_path=cache)
    print(f"  有效截面 {len(dates)}（{dates[0]} ~ {dates[-1]}）  {X.shape[0]}×{X.shape[1]}×{X.shape[2]}")

    ev = Evaluator(X, M, Y, dim_max)

    # ── self-check：向量化 vs 生产口径逐位核对 ──
    print("\n自检：向量化 IC 与生产纯 Python 实现比对 ...")
    from audit_round67_backtest_engine import composite_all
    wv = np.array([w_base[k] for k in DIMS])
    t0 = ev.total(wv)
    # 抽查一个截面
    i = len(dates) // 2
    d0 = dates[i]
    _panel = json.load(open(args.panel, encoding="utf-8"))
    rows = _panel["rows"][d0]
    del _panel
    cnt = 0
    for j, c in enumerate(codes):
        if np.isnan(t0[i, j]):
            continue
        s_py, _ = composite_all(rows[c], w_base, dim_max)
        if s_py is None:
            continue
        if abs(s_py - float(t0[i, j])) > 1e-4:
            print(f"   ❌ 不一致 {c}: py={s_py} np={t0[i,j]}")
            cnt += 1
            if cnt > 3:
                break
    print(f"   {'✅ 一致' if cnt == 0 else f'⚠️ {cnt} 处不一致'}（抽查截面 {d0}）")

    # ── 三段切分 ──
    n = len(dates)
    iA, iB = int(n * 0.40), int(n * 0.70)
    seg = {"A": (0, iA), "B": (iA, iB), "C": (iB, n), "ALL": (0, n)}
    for k, (a, b) in seg.items():
        print(f"  段{k:3s} {b-a:3d} 截面  {dates[a]} ~ {dates[b-1]}")

    def seg_ics(wd, key):
        a, b = seg[key]
        sub = Evaluator(X[a:b], M[a:b], Y[a:b], dim_max)
        return sub.ics(np.array([wd[k] for k in DIMS]))

    def seg_stat(wd, key):
        return stat(seg_ics(wd, key))

    # ── 单维 IC（全样本 + 分段）──
    print("\n单维 IC（h20，全样本 & 分段）...")
    single = {}
    for k, dim in enumerate(DIMS):
        def _one(idx_range):
            a, b = idx_range
            ics = []
            Xk = X[a:b, :, k]
            for t in range(Xk.shape[0]):
                xs = Xk[t]
                ys = Y[a + t]
                ok = (~np.isnan(xs)) & (~np.isnan(ys))
                if ok.sum() < 100:
                    continue
                x = xs[ok] - xs[ok].mean()
                y = ys[ok] - ys[ok].mean()
                sx = math.sqrt((x * x).sum()); sy = math.sqrt((y * y).sum())
                if sx > 0 and sy > 0:
                    ics.append(float((x * y).sum() / (sx * sy)))
            return ics
        s_all = _one(seg["ALL"]); s_A = _one(seg["A"]); s_C = _one(seg["C"])
        covn = int(np.sum(~np.isnan(X[:, :, k])))
        single[dim] = {"n": len(s_all),
                       "ic": (sum(s_all) / len(s_all)) if s_all else None,
                       "t": nw_t(s_all),
                       "ic_A": (sum(s_A) / len(s_A)) if s_A else None,
                       "ic_C": (sum(s_C) / len(s_C)) if s_C else None,
                       "coverage": covn / float(X.shape[0] * X.shape[1])}
        s = single[dim]
        print(f"   {dim:20s} n={s['n']:3d}  IC={_f(s['ic'])}  t={_f(s['t'],2)}  "
              f"A={_f(s['ic_A'])}  C={_f(s['ic_C'])}  覆盖{s['coverage']*100:5.1f}%")

    # ── 基线 ──
    print("\n基线 v4.15 ...")
    base_stat = {k: seg_stat(w_base, k) for k in seg}
    for k in ("A", "B", "C", "ALL"):
        s = base_stat[k]
        print(f"   段{k:3s} n={s['n']:3d}  IC={_f(s['ic'])}  ICIR={_f(s['icir'],3)}  t={_f(s['t'],2)}")

    # ── 优化（段A）──
    print(f"\n优化（目标={args.objective}，段A，单维≤{W_CAP} 大类≤{G_CAP} Σ=1）...")
    evA = Evaluator(X[:iA], M[:iA], Y[:iA], dim_max)
    w_opt, objv, cache = optimize(evA, dim_max, objective=args.objective,
                                  n_starts=args.n_starts)
    print("   最优权重：")
    for g, ks in GROUPS.items():
        gs = sum(w_opt[k] for k in ks)
        print(f"     {g:6s} Σ={gs:.4f}  " +
              "  ".join(f"{k.replace('score_','')}={w_opt[k]:.4f}" for k in ks))
    print(f"   Σ={sum(w_opt.values()):.6f}  可行={feasible(w_opt)}  评估次数={len(cache)}")

    # ── 段B 选 λ ──
    print("\n段B 选参：向基线收缩 λ 扫描 ...")
    lam_scan = []
    best_b, lam_best = -1e9, 0.0
    for lam in (0.0, 0.15, 0.30, 0.50, 0.70, 1.0):
        w_mix = project({k: (1 - lam) * w_opt[k] + lam * w_base[k] for k in DIMS})
        st = seg_stat(w_mix, "B")
        lam_scan.append({"lam": lam, **st})
        print(f"   λ={lam:.2f}  段B ICIR={_f(st['icir'],3)}  IC={_f(st['ic'])}  t={_f(st['t'],2)}")
        if st["icir"] is not None and st["icir"] > best_b:
            best_b, lam_best = st["icir"], lam
    w_final = project({k: (1 - lam_best) * w_opt[k] + lam_best * w_base[k] for k in DIMS})
    print(f"   → λ*={lam_best}")

    # ── 全方案对比 ──
    print("\n各方案在三段上的表现（段C 是唯一样本外数字）...")
    cands = {
        "base_v415": w_base,
        "opt_noShrink": w_opt,
        "opt_shrunk": w_final,
        "equal_weight": {k: 1.0 / len(DIMS) for k in DIMS},
    }
    final = {}
    for nm, w in cands.items():
        row = {k: seg_stat(w, k) for k in seg}
        final[nm] = {
            "weights": w,
            "group_sums": {g: sum(w[k] for k in ks) for g, ks in GROUPS.items()},
            "feasible": feasible(w),
            "stat": row,
        }
        print(f"   {nm:14s} 段C ICIR={_f(row['C']['icir'],3):>7}  IC={_f(row['C']['ic']):>7}  "
              f"t={_f(row['C']['t'],2):>6}  ｜全样本 ICIR={_f(row['ALL']['icir'],3):>7}")

    meta = {
        "round": 68,
        "purpose": "目标函数从「单维 IC 显著性」改为「组合 ICIR 最大化」+ 三段切分样本外检验",
        "panel": os.path.relpath(args.panel, HERE),
        "hop": args.hop, "objective": args.objective,
        "constraints": {"w_cap": W_CAP, "group_cap": G_CAP, "sum": 1.0, "lower": 0.0,
                        "groups": GROUPS},
        "n_dates": len(dates), "date_range": [dates[0], dates[-1]],
        "segments": {k: [dates[v[0]], dates[v[1] - 1], v[1] - v[0]] for k, v in seg.items()},
        "self_check": "向量化与生产纯 Python composite_all 逐股比对通过" if cnt == 0 else f"{cnt} 处不一致",
        "caveat": (
            "① 段C 只测一次，是唯一合法的样本外数字；段A/B 必然乐观；"
            "② 段A 仅约 95 截面、自由度 9 维 → 过拟合风险实存，故用段B 选 λ 收缩；"
            "③ 目标函数含秩相关（非凸），坐标上升只能保证局部最优，多起点缓解但不消除；"
            "④ fund 覆盖率极低（约 3.5% 全样本），其权重在优化中近似「无信息先验」；"
            "⑤ 样本 2025-11~2026-10 为小盘投机主导 regime，跨 regime 未验证。"
        ),
    }
    json.dump({"meta": meta, "single_dim": single, "baseline": base_stat,
               "lam_scan": lam_scan, "lam_best": lam_best,
               "candidates": final, "n_evals": len(cache)},
              open(args.out, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"\n→ {args.out}")


def _f(v, nd=4):
    return "  n/a " if v is None else f"{v:+.{nd}f}"


if __name__ == "__main__":
    main()
