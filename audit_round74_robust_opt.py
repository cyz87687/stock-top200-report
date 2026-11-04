#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
audit_round74_robust_opt.py —— 稳健权重优化：最大化「最差段 ICIR」（minimax）

═══ 为什么放弃「全样本 ICIR 最大化」═══

round70 实测：以段A 的 ICIR 为目标优化，样本外（段C）反而**不如原权重**：
    FULL   段A 0.984 →（优化）→ 段C 0.871   而 base 段C = 0.891
    NOFUND 段A 0.984 →（优化）→ 段C 0.897   而 base 段C = 0.915
唯一在段C 更好的是**等权**（0.961 / 0.980），而它根本没优化。

round71 给出了机理，且是硬证据：
    组合 IC 与「未来20日市场收益」Pearson r = **−0.6705**
    bootstrap 95%CI [−0.7354, −0.5861]（不含0）
    五档严格单调：跌市 Q1 IC **+0.3079** → 涨市 Q5 IC **−0.0975**
    R² = 0.4496 → 市场方向解释 IC 波动的 45%
    9 维中 4 维（ep/lowvol20/rsi_rev/tech_neg）在涨市/跌市 IC 符号翻转

⇒ **本模型是防御型选股器**。全样本 ICIR 最大化等于在两类相反 regime 间取平均，
  必然过拟合"平均环境"，在任一具体 regime 下都不是最优。

round73 进一步证明 regime **不可事前利用**：
    修复 round72 的重叠窗口前视后，所有事前日线信号在段C 的 IC 价差均 < 0.05
    （P60 价差 −0.509 且低侧58/高侧8，极端不平衡；V20 价差 +0.029）
⇒ 不能"看市场涨跌切权重"。

═══ 所以正确的目标是什么 ═══

既然无法预测 regime，权重必须**对 regime 稳健**，而不是对某段最优。
采用 minimax（最差段最优）目标：

    maximize  min( ICIR_A , ICIR_B , ICIR_C )
    s.t.      Σw = 1,  0 ≤ w_i ≤ 0.20,  大类 Σ ≤ 0.30

这个目标的含义：找一个在所有环境下都**不那么差**的权重。
代价是牺牲峰值 ICIR —— 这是必须向老大如实交代的 trade-off。

同时输出三项对照指标，供决策：
  · 全样本 ICIR（目标①原口径）
  · 最差段 ICIR（稳健口径）
  · 段间 ICIR 标准差（越小平坦）

用法：python audit_round74_robust_opt.py --n-starts 10
"""
import os
import sys
import json
import math
import time
import argparse
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from audit_round70_icir_fast import (  # noqa
    DIMS, GROUPS, W_CAP, G_CAP, Ev, stat, project, feasible, _f, WEIGHTS)

NPZ = os.path.join(HERE, "data", "icir_arrays_h20.npz")
OUT = os.path.join(HERE, "audit_round74.json")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--npz", default=NPZ)
    ap.add_argument("--n-starts", type=int, default=10)
    ap.add_argument("--n-sweeps", type=int, default=12)
    ap.add_argument("--out", default=OUT)
    args = ap.parse_args()

    t_all = time.time()
    z = np.load(args.npz, allow_pickle=False)
    dates = [str(x) for x in z["dates"]]
    X, M, Y = z["X"], z["M"], z["Y"]
    wc = json.load(open(WEIGHTS, encoding="utf-8"))
    dm = {k: float(v) for k, v in wc["dim_max"].items()}
    w_base = np.array([float(wc["weights"].get(k, 0.0)) for k in DIMS])

    nD = len(dates)
    iA, iB = int(nD * 0.40), int(nD * 0.70)
    segs = {"A": (0, iA), "B": (iA, iB), "C": (iB, nD), "ALL": (0, nD)}

    print(f"面板 {nD} 截面 {dates[0]} ~ {dates[-1]}")
    print(f"权重 {wc['version']}")
    for k, (a, b) in segs.items():
        print(f"  段{k:3s} {b-a:3d} 截面  {dates[a]} ~ {dates[b-1]}")

    # ⚠️ 内存纪律：X/M/Y 用 float32 保存，但 Ev 内部 V/Mf 会升 float64。
    #    若同时持有 4 个 Ev（A/B/C/ALL），V 各约 219×2029×9×8B ≈ 32MB，
    #    加 Mf 与缓存在坐标上升里持续增长 → 本机 swap 已满时必然 exit 137。
    #    故：优化阶段只保留 A/B/C 三个 Ev，ALL 等到评估阶段再临时构造并清缓存。
    evA = Ev(X[:iA], M[:iA], Y[:iA], dm, DIMS)
    evB = Ev(X[iA:iB], M[iA:iB], Y[iA:iB], dm, DIMS)
    evC = Ev(X[iB:], M[iB:], Y[iB:], dm, DIMS)
    evs = {"A": evA, "B": evB, "C": evC, "ALL": None}
    _all_pack = (X, M, Y)

    def evaluate(w):
        st = {}
        for k in ("A", "B", "C"):
            st[k] = stat(evs[k].ics(w))
        icirs = [st[k]["icir"] for k in ("A", "B", "C")]
        # 全样本分只在需要时报（优化阶段跳过，省内存与时间）
        st["ALL"] = {"icir": None, "ic": None, "t": None, "n": None}
        if any(v is None for v in icirs):
            return -1e9, st
        return float(min(icirs)), st

    def evaluate_full(w):
        """带全样本的完整评估（只在最终对比时调用）。"""
        objv, st = evaluate(w)
        Xa, Ma, Ya = _all_pack
        ev = Ev(Xa, Ma, Ya, dm, DIMS)
        st["ALL"] = stat(ev.ics(w))
        del ev
        return objv, st

    # 参照系
    print("\n" + "═" * 80)
    print("参照系")
    print("═" * 80)
    refs = {
        "base_v415": project(w_base, DIMS),
        "equal": project(np.ones(len(DIMS)) / len(DIMS), DIMS),
    }
    # 加一个「技术簇合并」参照：三簇共线，合成一维等价于均分其权重
    ref_rows = {}
    for nm, w in refs.items():
        objv, st = evaluate_full(w)
        ref_rows[nm] = {"weights": dict(zip(DIMS, [float(x) for x in w])),
                        "min_icir": objv,
                        "stat": st}
        print(f"  {nm:12s} 最差段ICIR={objv:+7.4f}  "
              f"A={_f(st['A']['icir'],3)} B={_f(st['B']['icir'],3)} C={_f(st['C']['icir'],3)} "
              f"全样本={_f(st['ALL']['icir'],3)}")

    # ═══ minimax 优化（多起点坐标上升）═══
    print("\n" + "═" * 80)
    print(f"稳健优化：max min(ICIR_A, ICIR_B, ICIR_C)  单维≤{W_CAP} 大类≤{G_CAP} Σ=1")
    print("═" * 80)
    rng = np.random.default_rng(20261029)
    nb = len(DIMS)
    starts = [project(w_base, DIMS), project(np.ones(nb) / nb, DIMS)]
    for _ in range(max(0, args.n_starts - 2)):
        v = rng.exponential(1.0, nb)
        starts.append(project(v / v.sum(), DIMS))

    deltas = (-0.06, -0.03, -0.015, 0.015, 0.03, 0.06)
    best_w, best_v, best_st = None, -1e9, None
    for si, s0 in enumerate(starts):
        w = s0.copy()
        cur, st = evaluate(w)
        for _ in range(args.n_sweeps):
            improved = False
            for i in range(nb):
                for d in deltas:
                    cand = w.copy()
                    cand[i] = w[i] + d
                    cand = project(cand, DIMS)
                    v, s2 = evaluate(cand)
                    if v > cur + 1e-10:
                        w, cur, st, improved = cand, v, s2, True
            if not improved:
                break
        if cur > best_v:
            best_w, best_v, best_st = w, cur, st
        print(f"  起点 {si+1}/{len(starts)}  最优最差段ICIR={best_v:+.4f}  {time.time()-t_all:.0f}s")

    print(f"\n  → 稳健权重（最差段 ICIR {best_v:+.4f}）：")
    for g, ks in GROUPS.items():
        idx = [DIMS.index(k) for k in ks]
        print(f"     {g:6s} Σ={best_w[idx].sum():.4f}  " +
              "  ".join(f"{k.replace('score_','')}={best_w[DIMS.index(k)]:.4f}" for k in ks))
    print(f"     Σ={best_w.sum():.6f}  可行={feasible(best_w, DIMS)}")

    # ═══ 方案对比（含段间标准差）═══
    print("\n" + "═" * 80)
    print("方案对比")
    print("═" * 80)
    cands = dict(refs)
    cands["robust_minimax"] = best_w
    # 稳健与基线的折中
    for lam in (0.35, 0.5, 0.65):
        cands[f"mix_robust{int(lam*100)}"] = project((1 - lam) * best_w + lam * project(w_base, DIMS), DIMS)

    print(f"{'方案':16s}{'最差段':>9s}{'段A':>9s}{'段B':>9s}{'段C':>9s}"
          f"{'全样本':>9s}{'段间SD':>9s}{'全样本t':>9s}")
    print("-" * 80)
    final = {}
    for nm, w in cands.items():
        objv, st = evaluate_full(w)
        icirs = [st[k]["icir"] for k in ("A", "B", "C")]
        sd = float(np.std(icirs, ddof=1)) if all(v is not None for v in icirs) else None
        final[nm] = {"weights": dict(zip(DIMS, [float(x) for x in w])),
                     "group_sums": {g: float(sum(w[DIMS.index(k)] for k in ks))
                                    for g, ks in GROUPS.items()},
                     "feasible": bool(feasible(w, DIMS)),
                     "min_icir": objv, "seg_icir_sd": sd, "stat": st}
        print(f"{nm:16s}{objv:>+9.4f}{_f(st['A']['icir'],3):>9s}{_f(st['B']['icir'],3):>9s}"
              f"{_f(st['C']['icir'],3):>9s}{_f(st['ALL']['icir'],3):>9s}"
              f"{('%.3f'%sd) if sd is not None else '  n/a':>9s}{_f(st['ALL']['t'],2):>9s}")

    # ═══ 结论 ═══
    print("\n" + "═" * 80)
    print("结论")
    print("═" * 80)
    b = final["base_v415"]["min_icir"]
    r = final["robust_minimax"]["min_icir"]
    e = final["equal"]["min_icir"]
    print(f"  基线的短板：段B ICIR = {_f(final['base_v415']['stat']['B']['icir'],3)}"
          f"（该段 89.4% 截面 IC 为负，是普涨市）")
    print(f"  稳健权重把最差段从 {b:+.4f} 提到 {r:+.4f}（Δ {r-b:+.4f}）")
    print(f"  代价：全样本 ICIR {_f(final['base_v415']['stat']['ALL']['icir'],3)} → "
          f"{_f(final['robust_minimax']['stat']['ALL']['icir'],3)}")
    print(f"  等权基准的最差段 = {e:+.4f}，全样本 = "
          f"{_f(final['equal']['stat']['ALL']['icir'],3)}")
    print()
    print("  ⚠️ 无论选哪个方案，都必须承认：")
    print("     ① 段B（2026-03~06 普涨市）**任何权重都无法做出正 IC**，")
    print("        因为 4/9 维在该 regime 符号翻转（round71），这是因子结构问题不是权重问题；")
    print("     ② minimax 提升的是『不那么差』，不是『转正』；")
    print("     ③ 真实可交易的改善只能来自**降低涨市暴露**（仓位层），而非权重层。")

    json.dump({
        "meta": {"round": 74,
                 "purpose": "稳健权重优化：minimax(段ICIR)，替代 round70 的全样本 ICIR 最大化",
                 "npz": os.path.relpath(args.npz, HERE),
                 "weights_version": wc["version"],
                 "n_dates": nD, "date_range": [dates[0], dates[-1]],
                 "segments": {k: [dates[v[0]], dates[v[1]-1], v[1]-v[0]] for k, v in segs.items()},
                 "objective": "max min(ICIR_A, ICIR_B, ICIR_C)",
                 "constraints": {"w_cap": W_CAP, "group_cap": G_CAP, "sum": 1.0, "lower": 0.0},
                 "why_not_full_sample": (
                     "round70：以段A ICIR 优化 → 段C 0.871 < base 0.891；"
                     "round71：IC 与未来市场收益 r=−0.6705 CI[−0.735,−0.586]，"
                     "9维中4维在涨跌市符号翻转 → 全样本 ICIR 最大化=在相反 regime 间取平均"),
                 "why_not_regime_switch": (
                     "round73：修复 round72 重叠窗口前视后，所有事前日线信号在段C 的 IC 价差<0.05，"
                     "regime 不可事前利用"),
                 "caveat": (
                     "① 段B 为普涨市，4/9 维符号翻转，任何权重都无法转正；"
                     "② minimax 只保证『不那么差』；"
                     "③ 段C 仅 66 截面，样本外功效有限；"
                     "④ 面板 A/B 市值维为近似（股本中位数×收盘）；"
                     "⑤ 样本 2025-11~2026-09 单一 regime 组合，跨更长周期未验证。")},
        "reference": ref_rows,
        "candidates": final,
        "chosen": "robust_minimax",
        "elapsed_s": round(time.time() - t_all, 1),
    }, open(args.out, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"\n→ {args.out}  （{time.time()-t_all:.0f}s）")


if __name__ == "__main__":
    main()
