#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
audit_round70_icir_fast.py —— 组合 ICIR 最大化（向量化重写版，替代 round68 的慢实现）

═══ 为什么要重写 round68 ═══

round68 的 rankdata 是纯 Python 双循环（每个截面一个 while 循环处理并列），
实测单次评估约 400ms；按 24 起点 × 20 轮 × 9 维 × 6 步长 ≈ 2.6 万次评估
→ 预计 3 小时以上。实际跑了 5 分钟仍无输出（stdout 还被 `| tail` 缓冲住）。

本版改用 scipy.stats.rankdata(axis=1, nan_policy='omit')，实测单次评估
（219×2029 全截面）**29.9ms**，提速约 13 倍。同等搜索预算降到 ~10 分钟。

═══ 目标函数的数学说明（必须让老大看懂为什么不能只堆单维高 IC）═══

组合 IC_c = spearman( total_c , fwd_ret_c )，其中 total_c = Σ w_i·x_i / Σ w_i
组合 ICIR = mean(IC_c) / sd(IC_c)

关键：sd(IC_c) 不是各维 IC 方差的加权和，而是**由各维 IC 的协方差矩阵决定**。
若两维 IC 高度正相关（本模型技术三簇 ρ≈0.85），同时加重只会同向放大
mean 和 sd，ICIR 几乎不动 —— 这就是「单维 IC 最高的因子未必让组合 ICIR 最大」。

故本脚本的目标是直接对 total 做截面秩相关，不拆维、不近似。

═══ 本轮必须同时处理的 P0 数据缺陷（否则优化的是错的东西）═══

`score_fund` 在长面板里只覆盖 457/2029 只（22%），且有值的截面只有 63/219。
而它在 v4.15 里是**最高权重 0.20**。round45 已实测：
  「是否被 fund 覆盖」这个哑变量的 h20 IC = **−0.1796（t=−8.53）**
⇒ fund 覆盖子集系统性跑输，fund 的 IC 与选择偏差**不可分离**。

所以本脚本跑三套面板口径，逐一对账：
  · FULL   —— 全 9 维，缺维从分母剔除（生产语义）
  · NOFUND —— 去掉 fund，8 维重归一
  · FUNDOK —— 只保留 fund 有覆盖的 63 个截面（n 太小，仅作方向参考）

唯一样本外裁决仍用段C（后30%），段A优化/段B选λ/段C只测一次。

用法：
  python audit_round70_icir_fast.py --n-starts 12
"""
import os
import sys
import json
import math
import time
import argparse
import numpy as np
from scipy.stats import rankdata

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

WEIGHTS = os.path.join(HERE, "factor_weights.json")
OUT = os.path.join(HERE, "audit_round70.json")

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


# ═══════════════ 评估器（scipy 向量化） ═══════════════

class Ev:
    """给定一个截面切片，评估任意权重的组合 IC 序列。"""

    def __init__(self, X, M, Y, dim_max, dims):
        self.dims = list(dims)
        self.X = X
        self.M = M
        self.Y = Y
        self.dmax = np.array([float(dim_max.get(k, 10.0)) for k in self.dims], np.float64)
        # min(v, dmax)/dmax*10 —— 缺维处 VALUE 置 0，但用它时必须乘 M
        self.V = np.where(M, np.minimum(np.where(np.isnan(X), 0.0, X), self.dmax)
                          / self.dmax * 10.0, 0.0)
        self.Mf = M.astype(np.float64)
        self.RY = rankdata(Y, axis=1, nan_policy="omit")
        self.okY = ~np.isnan(self.RY)
        # 有效股票数（用于 n>=100 门槛）
        self.nok = self.okY.sum(axis=1)
        self.cache = {}

    def total(self, w):
        w = np.asarray(w, np.float64)
        den = self.Mf @ w                      # (nD, nC)
        num = self.V @ w
        with np.errstate(invalid="ignore", divide="ignore"):
            s = np.where(den > 1e-12, num / den * 10.0, np.nan)
        return s

    def ics(self, w):
        key = w.tobytes()
        if key in self.cache:
            return self.cache[key]
        s = self.total(w)
        RS = rankdata(s, axis=1, nan_policy="omit")
        ok = (~np.isnan(RS)) & self.okY
        ok &= self.nok[:, None] >= 100
        a = np.where(ok, RS, 0.0)
        b = np.where(ok, self.RY, 0.0)
        n = ok.sum(axis=1).astype(np.float64)
        n_safe = np.where(n > 0, n, 1.0)
        ma = a.sum(axis=1) / n_safe
        mb = b.sum(axis=1) / n_safe
        da = np.where(ok, a - ma[:, None], 0.0)
        db = np.where(ok, b - mb[:, None], 0.0)
        num = (da * db).sum(axis=1)
        den = np.sqrt((da * da).sum(axis=1) * (db * db).sum(axis=1))
        with np.errstate(invalid="ignore", divide="ignore"):
            ic = np.where((den > 1e-12) & (n >= 100), num / den, np.nan)
        out = ic[~np.isnan(ic)]
        res = [float(v) for v in out]
        self.cache[key] = res
        return res


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
    n = len(ics)
    mu = sum(ics) / n
    var = sum((x - mu) ** 2 for x in ics) / (n - 1)
    sd = math.sqrt(var)
    return {"n": n, "ic": mu, "sd": sd,
            "icir": (mu / sd if sd > 1e-12 else None),
            "t": nw_t(ics),
            "pos_rate": sum(1 for x in ics if x > 0) / n}


# ═══════════════ 约束投影 ═══════════════

def project(wv, dims, w_cap=W_CAP, g_cap=G_CAP, groups=GROUPS):
    w = np.clip(np.asarray(wv, np.float64), 0.0, w_cap)
    gidx = [[dims.index(k) for k in ks if k in dims] for ks in groups.values()]
    gidx = [g for g in gidx if g]
    for _ in range(400):
        for idx in gidx:
            s = w[idx].sum()
            if s > g_cap:
                w[idx] *= g_cap / s
        tot = w.sum()
        if tot <= 1e-12:
            w = np.ones(len(dims)) / len(dims)
            tot = 1.0
        w = w / tot
        over = w > w_cap
        if not over.any():
            break
        w[over] = w_cap
    return w


def feasible(wv, dims, w_cap=W_CAP, g_cap=G_CAP, groups=GROUPS):
    w = np.asarray(wv)
    if w.min() < -1e-9 or w.max() > w_cap + 1e-9:
        return False
    if abs(w.sum() - 1.0) > 1e-6:
        return False
    for ks in groups.values():
        idx = [dims.index(k) for k in ks if k in dims]
        if idx and w[idx].sum() > g_cap + 1e-9:
            return False
    return True


# ═══════════════ 优化：多起点坐标上升 ═══════════════

def optimize(ev, dims, w_base, n_starts=12, n_sweeps=14, seed=20261029, verbose=True):
    rng = np.random.default_rng(seed)
    nb = len(dims)

    def score(wv):
        st = stat(ev.ics(wv))
        return (st["icir"] if st["icir"] is not None else -1e9), st

    starts = [project(w_base, dims), np.ones(nb) / nb]
    for _ in range(max(0, n_starts - 2)):
        v = rng.exponential(1.0, nb)
        starts.append(project(v / v.sum(), dims))

    deltas = (-0.06, -0.03, -0.015, -0.008, 0.008, 0.015, 0.03, 0.06)
    best_w, best_v = None, -1e9
    t0 = time.time()
    for si, s0 in enumerate(starts):
        w = project(s0, dims)
        cur, _ = score(w)
        for sw in range(n_sweeps):
            improved = False
            for i in range(nb):
                for d in deltas:
                    cand = w.copy()
                    cand[i] = w[i] + d
                    cand = project(cand, dims)
                    v, _ = score(cand)
                    if v > cur + 1e-10:
                        w, cur, improved = cand, v, True
            if not improved:
                break
        if cur > best_v:
            best_w, best_v = w, cur
        if verbose and (si + 1) % 4 == 0:
            print(f"      起点 {si+1}/{len(starts)} 最优ICIR {best_v:+.4f} "
                  f"评估 {len(ev.cache)} 次 {time.time()-t0:.0f}s")
    return best_w, best_v


# ═══════════════ 主流程 ═══════════════

def run_case(name, dims, X, M, Y, dim_max, w_base_full, n_starts, seg):
    """对一套维度子集跑完整三段流程。"""
    print(f"\n{'═'*78}\n【{name}】维度 {len(dims)} 个：{', '.join(d.replace('score_','') for d in dims)}")
    # 抽取对应维
    idx = [DIMS.index(d) for d in dims]
    Xs, Ms = X[:, :, idx], M[:, :, idx]
    w_base = np.array([w_base_full[DIMS.index(d)] for d in dims])
    w_base = w_base / w_base.sum()

    res = {"dims": dims, "n_dim": len(dims)}

    # 单维 IC
    single = {}
    for j, d in enumerate(dims):
        ev1 = Ev(Xs[:, :, j:j+1], Ms[:, :, j:j+1], Y, {d: dim_max.get(d, 10.0)}, [d])
        st = stat(ev1.ics(np.array([1.0])))
        cov = float(np.sum(Ms[:, :, j])) / float(Ms.shape[0] * Ms.shape[1])
        single[d] = {**st, "coverage": cov}
        print(f"   单维 {d.replace('score_',''):12s} n={st['n']:3d} IC={_f(st['ic'])} "
              f"t={_f(st['t'],2)} 覆盖{cov*100:5.1f}%")
    res["single_dim"] = single

    evs = {}
    for k, (a, b) in seg.items():
        evs[k] = Ev(Xs[a:b], Ms[a:b], Y[a:b], dim_max, dims)

    base = {k: stat(evs[k].ics(w_base)) for k in seg}
    print(f"   基线原权重（重归一）: 段A ICIR={_f(base['A']['icir'],3)} "
          f"段B={_f(base['B']['icir'],3)} 段C={_f(base['C']['icir'],3)} "
          f"全样本={_f(base['ALL']['icir'],3)}")
    res["baseline"] = base

    print(f"   优化（段A，Σ=1，单维≤{W_CAP}，大类≤{G_CAP}）...")
    t0 = time.time()
    w_opt, v_opt = optimize(evs["A"], dims, w_base, n_starts=n_starts)
    print(f"   → 段A ICIR {v_opt:+.4f}（{time.time()-t0:.0f}s，评估 {len(evs['A'].cache)} 次）")
    print("     权重 " + "  ".join(f"{d.replace('score_','')}={w_opt[i]:.4f}"
                                  for i, d in enumerate(dims)))
    res["opt"] = {"weights": dict(zip(dims, [float(x) for x in w_opt])),
                  "segA_icir": v_opt}

    # 段B 选 λ
    print("   段B 选 λ（向基线收缩）...")
    lam_rows = []
    best_lam, best_v = 0.0, -1e9
    for lam in (0.0, 0.1, 0.2, 0.3, 0.4, 0.5, 0.65, 0.8, 1.0):
        wm = project((1 - lam) * w_opt + lam * w_base, dims)
        st = stat(evs["B"].ics(wm))
        lam_rows.append({"lam": lam, **st,
                         "weights": dict(zip(dims, [float(x) for x in wm]))})
        print(f"     λ={lam:.2f} 段B ICIR={_f(st['icir'],3)} IC={_f(st['ic'])} t={_f(st['t'],2)}")
        if st["icir"] is not None and st["icir"] > best_v:
            best_lam, best_v = lam, st["icir"]
    w_final = project((1 - best_lam) * w_opt + best_lam * w_base, dims)
    print(f"   → λ*={best_lam}")
    res["lam_scan"] = lam_rows
    res["lam_best"] = best_lam

    # 段C 裁决
    print("   段C 样本外裁决（只测一次）...")
    cands = {
        "base": w_base,
        "opt_noShrink": w_opt,
        "opt_shrunk": w_final,
        "equal": np.ones(len(dims)) / len(dims),
    }
    final = {}
    for nm, w in cands.items():
        row = {k: stat(evs[k].ics(w)) for k in seg}
        final[nm] = {"weights": dict(zip(dims, [float(x) for x in w])),
                     "feasible": feasible(w, dims), "stat": row}
        print(f"     {nm:12s} 段C ICIR={_f(row['C']['icir'],3):>7} "
              f"IC={_f(row['C']['ic']):>7} t={_f(row['C']['t'],2):>6} "
              f"｜全样本 ICIR={_f(row['ALL']['icir'],3):>7} "
              f"t={_f(row['ALL']['t'],2):>6}")
    res["candidates"] = final
    return res


def _f(v, nd=4):
    return "  n/a " if v is None else f"{v:+.{nd}f}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--npz", default=os.path.join(HERE, "data", "icir_arrays_h20.npz"))
    ap.add_argument("--n-starts", type=int, default=12)
    ap.add_argument("--out", default=OUT)
    args = ap.parse_args()

    t_all = time.time()
    z = np.load(args.npz, allow_pickle=False)
    dates = [str(x) for x in z["dates"]]
    X, M, Y = z["X"], z["M"], z["Y"]
    # npz 里列顺序即为 DIMS
    wc = json.load(open(WEIGHTS, encoding="utf-8"))
    dim_max = {k: float(v) for k, v in wc["dim_max"].items()}
    w_base_full = np.array([float(wc["weights"].get(k, 0.0)) for k in DIMS])

    print(f"面板 {X.shape[0]} 截面 × {X.shape[1]} 只 × {X.shape[2]} 维  "
          f"{dates[0]} ~ {dates[-1]}")
    print(f"权重版本 {wc['version']}  Σw={w_base_full.sum():.6f}")
    print("原权重 " + "  ".join(f"{d.replace('score_','')}={w_base_full[i]:.4f}"
                                for i, d in enumerate(DIMS)))

    n = len(dates)
    iA, iB = int(n * 0.40), int(n * 0.70)
    seg = {"A": (0, iA), "B": (iA, iB), "C": (iB, n), "ALL": (0, n)}
    print("\n三段切分：")
    for k, (a, b) in seg.items():
        print(f"  段{k:3s} {b-a:3d} 截面  {dates[a]} ~ {dates[b-1]}")

    # fund 覆盖诊断
    kf = DIMS.index("score_fund")
    cov_f = M[:, :, kf].sum(axis=1)
    nz = int((cov_f > 0).sum())
    print(f"\n[诊断] score_fund 覆盖：有值截面 {nz}/{n}，"
          f"中位只数 {int(np.median(cov_f))}/{X.shape[1]}")
    if nz:
        print(f"        覆盖区间 {dates[int(np.argmax(cov_f>0))]} ~ {dates[n-1-int(np.argmax(cov_f[::-1]>0))]}")

    cases = {}
    cases["FULL"] = run_case("FULL 全 9 维（生产语义：缺维从分母剔除）",
                             DIMS, X, M, Y, dim_max, w_base_full, args.n_starts, seg)
    cases["NOFUND"] = run_case("NOFUND 去 fund 8 维",
                               [d for d in DIMS if d != "score_fund"],
                               X, M, Y, dim_max, w_base_full, args.n_starts, seg)

    # FUNDOK：只保留 fund 有覆盖的截面
    keep = np.where(cov_f > 0)[0]
    if len(keep) >= 40:
        Xf, Mf, Yf = X[keep], M[keep], Y[keep]
        nf = len(keep)
        jA, jB = int(nf * 0.40), int(nf * 0.70)
        segf = {"A": (0, jA), "B": (jA, jB), "C": (jB, nf), "ALL": (0, nf)}
        cases["FUNDOK"] = run_case(f"FUNDOK 仅 fund 有覆盖的 {nf} 截面（n 小，仅方向参考）",
                                   DIMS, Xf, Mf, Yf, dim_max, w_base_full, args.n_starts, segf)
        cases["FUNDOK"]["dates"] = [dates[i] for i in keep]
    else:
        print(f"\n[跳过 FUNDOK] 有覆盖截面仅 {len(keep)} 个，不足以切三段")

    meta = {
        "round": 70,
        "purpose": "目标①：组合 ICIR 最大化（向量化重写，替代 round68 慢实现）",
        "npz": os.path.relpath(args.npz, HERE),
        "weights_version": wc["version"],
        "n_dates": n, "date_range": [dates[0], dates[-1]],
        "hop": 20,
        "segments": {k: [dates[v[0]], dates[v[1]-1], v[1]-v[0]] for k, v in seg.items()},
        "constraints": {"w_cap": W_CAP, "group_cap": G_CAP, "sum": 1.0, "lower": 0.0},
        "objective": "ICIR = mean(IC_h20) / sd(IC_h20)，IC 为截面 Spearman",
        "fund_coverage": {"nz_dates": nz, "n_dates": n,
                          "median_stocks": int(np.median(cov_f)), "universe": int(X.shape[1])},
        "caveat": (
            "① 段C 是唯一样本外数字，段A/B 必然乐观；"
            "② 目标含秩相关（非凸），坐标上升只保证局部最优；"
            "③ score_fund 在长面板仅覆盖约 22% 只/29% 截面，其 IC 与选择偏差不可分离"
            "（round45：in-pool 哑变量 h20 IC −0.1796 t=−8.53），故同时给 NOFUND 口径；"
            "④ 面板 A/B 市值维用「股本中位数×收盘」近似，无拆股调整，只用于相对比较；"
            "⑤ 样本 2025-11~2026-10 为小盘投机主导 regime，跨 regime 未验证。"
        ),
    }
    json.dump({"meta": meta, "cases": cases},
              open(args.out, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"\n→ {args.out}   （总耗时 {time.time()-t_all:.0f}s）")


if __name__ == "__main__":
    main()
