#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
audit_round97_weight_enum.py —— Step4 权重求解（穷举版，严格校验全部约束）

═══ 为什么要改成穷举 ═══

round95 用坐标上升 + 投影求解，得到「5 因子各 0.200」的解。但**这个解违反大类约束**：
    基本面组 = fund 0.200 + quality 0.200 = **0.400 > 0.30**（规范上限）
原因：project3 的水填充只压了大类，但收尾的「回填未触顶项」又把它填回去了。
⇒ 坐标上升 + 投影在这个约束组合下**不可靠**。

本脚本改为**穷举**：5 个因子、步长 0.05、全部约束显式校验。
组合数 = C(24,4) 量级，完全可枚举，且**无局部最优风险**。

═══ 目标函数（与 round95 一致）═══

    max min( ICIR_涨市子集 , ICIR_跌市子集 )

⚠️ 两个子集都是**非连续截面集合**，必须用 combo_ics_at 逐截面取样
（用切片会混入另一 regime 的截面 → 结论完全失真，本项目已踩）。

═══ 附加产出 ═══

    · 帕累托前沿（涨 ICIR vs 跌 ICIR 的所有非支配解）
    · 敏感性：把目标换成「全样本 ICIR 最大化」会得到什么解（对比用）
    · 约束可行性报告（哪些约束组合根本无解）

用法：python audit_round97_weight_enum.py
"""
import os
import sys
import json
import math
import time
import itertools
import argparse
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import factor_validation_core as F  # noqa

NPZ = os.path.join(HERE, "data", "val_arrays_h20.npz")
PANEL = os.path.join(HERE, "data", "panel_round66.json")
WEIGHTS = os.path.join(HERE, "factor_weights.json")
OUT = os.path.join(HERE, "audit_round97.json")


def feasible(combo, pool, pos, w_cap=0.30, g_cap=0.30, total=1.0):
    """严格校验：Σ=1、单维≤w_cap、大类≤g_cap。"""
    if abs(sum(combo) - total) > 1e-9:
        return False, "Σ≠1"
    if max(combo) > w_cap + 1e-9:
        return False, "单维超限"
    for nm, ks in F.GROUPS.items():
        idx = [pos[k] for k in ks if k in pos]
        if idx and sum(combo[i] for i in idx) > g_cap + 1e-9:
            return False, f"大类[{nm}]超限"
    return True, ""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--step", type=float, default=0.05)
    ap.add_argument("--w-cap", type=float, default=0.30,
                    help="单因子上限（老大裁决：0.20→0.30）")
    ap.add_argument("--g-cap", type=float, default=0.30,
                    help="大类合计上限")
    ap.add_argument("--out", default=OUT)
    args = ap.parse_args()
    t0 = time.time()

    dates, X, M, Y, codes = F.load_panel_arrays(PANEL, NPZ)
    wc = json.load(open(WEIGHTS, encoding="utf-8"))
    dim_max = {k: float(v) for k, v in wc["dim_max"].items()}
    RY = F.rank_rows(Y)

    mkt = np.array([np.nanmean(Y[t]) for t in range(len(dates))])
    med = float(np.nanmedian(mkt))
    up = sorted(t for t in range(len(dates)) if mkt[t] > med)
    dn = sorted(t for t in range(len(dates)) if mkt[t] <= med)
    j1 = up[0] + int(len(up) * 0.7)
    solve_up, inner = up[:j1], up[j1:]
    # 求解段内的两个 regime 子集（跌市子集只取落在求解段时间内的）
    solve_dn = [t for t in dn if solve_up[0] <= t <= solve_up[-1]]

    r94 = json.load(open(os.path.join(HERE, "audit_round94.json"), encoding="utf-8"))
    pool = [d for d in F.DIMS if r94["verdicts"][d]["cap"] > 0]
    pos = {d: i for i, d in enumerate(pool)}
    types = r94.get("types", {})

    print("═" * 100)
    print("STEP4（穷举版）  带约束权重求解")
    print("═" * 100)
    print(f"  候选池 {len(pool)}：{', '.join(d.replace('score_','') for d in pool)}")
    print(f"  求解段：涨市子集 {len(solve_up)} 截面（池等权 "
          f"{np.nanmean([mkt[t] for t in solve_up])*100:+.2f}%）｜"
          f"跌市子集 {len(solve_dn)} 截面（{np.nanmean([mkt[t] for t in solve_dn])*100:+.2f}%）")
    print(f"  内部验证段 {len(inner)} 截面｜样本外（跌市段）{len(dn)} 截面")
    print(f"  约束：Σ=1，单维≤{args.w_cap}，大类≤{args.g_cap}，步长 {args.step}")
    print(f"  ⚠️ 单维上限 0.30 = 老大 2026-10-29 裁决（原规范 0.20，因 5 因子池下无解）")
    print()

    grid = [round(v * args.step, 4) for v in range(int(1 / args.step) + 1)]
    grid = [g for g in grid if g <= args.w_cap + 1e-9]
    n_all = 0
    n_feas = 0
    rows = []
    for combo in itertools.product(grid, repeat=len(pool)):
        n_all += 1
        ok, why = feasible(combo, pool, pos, args.w_cap, args.g_cap)
        if not ok:
            continue
        n_feas += 1
        w = {d: 0.0 for d in F.DIMS}
        for d, v in zip(pool, combo):
            w[d] = v
        stu = F.stat(F.combo_ics_at(X, M, Y, w, dim_max, RY, solve_up))
        std = F.stat(F.combo_ics_at(X, M, Y, w, dim_max, RY, solve_dn))
        if stu["icir"] is None or std["icir"] is None:
            continue
        rows.append({"combo": list(combo),
                     "icir_up": stu["icir"], "icir_dn": std["icir"],
                     "ic_up": stu["ic"], "ic_dn": std["ic"],
                     "t_up": stu["t"], "t_dn": std["t"],
                     "min_icir": min(stu["icir"], std["icir"])})
    print(f"  枚举 {n_all} 个组合 → 满足全部约束 {n_feas} 个 → 有效评估 {len(rows)} 个"
          f"  ({time.time()-t0:.0f}s)")

    if not rows:
        print("  ❌ 无可行解")
        return

    # ── minimax 解 ──
    rows.sort(key=lambda r: -r["min_icir"])
    best = rows[0]
    print()
    print("  【目标：max min(ICIR涨, ICIR跌)】前 10：")
    print(f"  {'minICIR':>9s}{'涨ICIR':>9s}{'跌ICIR':>9s}{'涨IC':>9s}{'跌IC':>9s}  权重")
    for r in rows[:10]:
        ws = "  ".join(f"{d.replace('score_','')}={v:.2f}"
                       for d, v in zip(pool, r["combo"]) if v > 0)
        print(f"  {r['min_icir']:>+9.4f}{r['icir_up']:>+9.4f}{r['icir_dn']:>+9.4f}"
              f"{r['ic_up']:>+9.4f}{r['ic_dn']:>+9.4f}  {ws}")

    w_best = {d: 0.0 for d in F.DIMS}
    for d, v in zip(pool, best["combo"]):
        w_best[d] = v
    print(f"\n  → minimax 解权重：")
    for d, v in sorted(zip(pool, best["combo"]), key=lambda kv: -kv[1]):
        if v > 0:
            print(f"      {d.replace('score_',''):16s} {v:.4f}  ({types.get(d,'?'):8s})")
    print(f"      Σ = {sum(best['combo']):.4f}")

    # ── 对照：全样本 ICIR 最大化 ──
    print()
    print("  【对照：全样本 ICIR 最大化】（同一枚举集，仅目标不同）")
    all_up = list(range(len(dates)))

    def icir_all(combo):
        w = {d: 0.0 for d in F.DIMS}
        for d, v in zip(pool, combo):
            w[d] = v
        st = F.stat(F.combo_ics(X, M, Y, w, dim_max, RY))
        return st["icir"], st

    best_all = None
    for combo in itertools.product(grid, repeat=len(pool)):
        ok, _ = feasible(combo, pool, pos, args.w_cap, args.g_cap)
        if not ok:
            continue
        ic, st = icir_all(combo)
        if ic is None:
            continue
        if best_all is None or ic > best_all[0]:
            best_all = (ic, combo, st)
    if best_all:
        ws = "  ".join(f"{d.replace('score_','')}={v:.2f}"
                       for d, v in zip(pool, best_all[1]) if v > 0)
        print(f"    全样本 ICIR {best_all[0]:+.4f}  权重 {ws}")
        print(f"    ⚠️ 该解在涨市子集 ICIR = "
              f"{next(r['icir_up'] for r in rows if r['combo']==list(best_all[1])):+.4f}"
              f"，跌市 {next(r['icir_dn'] for r in rows if r['combo']==list(best_all[1])):+.4f}")

    # ── 内部验证（样本内留出）──
    print()
    print("  【内部验证段】（样本内留出，19 截面，涨市）")
    w_eq5 = {d: (0.2 if d in pool else 0.0) for d in F.DIMS}
    cands = {
        "minimax解": w_best,
        "全样本ICIR最优": ({d: (v if d in pool else 0.0)
                       for d, v in zip(pool, best_all[1])} if best_all else w_best),
        "v4.15基线": {k: float(v) for k, v in wc["weights"].items()},
        "9因子等权": {d: 1.0 / len(F.DIMS) for d in F.DIMS},
    }
    for nm, w in cands.items():
        st = F.stat(F.combo_ics_at(X, M, Y, w, dim_max, RY, inner))
        print(f"    {nm:16s} ICIR {_f(st.get('icir'),4)}  IC {_f(st.get('ic'),4)}  "
              f"t {_f(st.get('t'),2)}  n={st.get('n')}")

    # ── 帕累托前沿 ──
    print()
    print("  【帕累托前沿】（涨 ICIR vs 跌 ICIR 的非支配解）")
    pareto = []
    for r in rows:
        dominated = any((o["icir_up"] >= r["icir_up"] and o["icir_dn"] >= r["icir_dn"]
                         and (o["icir_up"] > r["icir_up"] or o["icir_dn"] > r["icir_dn"]))
                        for o in rows)
        if not dominated:
            pareto.append(r)
    pareto.sort(key=lambda r: -r["icir_up"])
    print(f"    非支配解 {len(pareto)} 个，前 8：")
    for r in pareto[:8]:
        ws = "  ".join(f"{d.replace('score_','')}={v:.2f}"
                       for d, v in zip(pool, r["combo"]) if v > 0)
        print(f"      涨 {r['icir_up']:+.4f}  跌 {r['icir_dn']:+.4f}  {ws}")

    json.dump({
        "meta": {"round": 97,
                 "purpose": "Step4 权重求解（穷举版，严格校验单维/大类/Σ 全部约束）",
                 "npz": os.path.relpath(NPZ, HERE),
                 "weights_version_base": wc["version"],
                 "n_dates": len(dates), "date_range": [dates[0], dates[-1]],
                 "pool": pool, "factor_types": types,
                 "segments": {
                     "solve_up": [dates[solve_up[0]], dates[solve_up[-1]], len(solve_up)],
                     "solve_dn": [dates[solve_dn[0]], dates[solve_dn[-1]], len(solve_dn)],
                     "inner_val": [dates[inner[0]], dates[inner[-1]], len(inner)],
                     "out_blind": [dates[dn[0]], dates[dn[-1]], len(dn)]},
                 "constraints": {"w_cap": args.w_cap, "group_cap": args.g_cap,
                                 "sum": 1.0, "step": args.step,
                                 "w_cap_deviation": ("老大 2026-10-29 裁决：单因子上限由规范 0.20 放宽至 0.30，"
                                                    "因 5 因子池在原约束下无解（最大 Σ=0.90）")},
                 "n_enumerated": n_all, "n_feasible": n_feas, "n_evaluated": len(rows),
                 "caveat": ("① 穷举无局部最优风险，但步长 0.05 是离散网格；"
                            "② 两个子集均为非连续截面集合，用 combo_ics_at 逐截面取样；"
                            "③ regime 划分事后（用未来收益），仅用于划分区间；"
                            "④ 样本外仅 110 截面、月度调仓约 7 期，功效极低。")},
        "minimax_solution": {"weights": {d: float(v) for d, v in zip(pool, best["combo"])},
                              "metrics": best},
        "all_icir_solution": ({"weights": {d: float(v) for d, v in zip(pool, best_all[1])},
                               "all_icir": best_all[0]} if best_all else None),
        "top_minimax": rows[:30],
        "pareto": pareto[:30],
        "internal_validation": {
            nm: F.stat(F.combo_ics_at(X, M, Y, w, dim_max, RY, inner))
            for nm, w in cands.items()},
        "elapsed_s": round(time.time() - t0, 1),
    }, open(args.out, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"\n→ {args.out}  （{time.time()-t0:.0f}s）")


def _f(v, nd=4):
    return "  n/a " if v is None else f"{v:+.{nd}f}"


if __name__ == "__main__":
    main()
