#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
audit_round93_step234.py —— Step2 相关性筛查 + Step3 增量信息 + Step4 约束权重求解

═══ 规范（老大 2026-10-29）═══

Step2 相关性筛查（Spearman 秩相关）
  · |ρ|＞0.7：高度共线，二选一精简
  · 0.5＜|ρ|≤0.7：中度相关，权重压低
  · |ρ|≤0.5：相关性可控

Step3 增量信息校验
  · 逐步加入因子，观察组合 ICIR 变化
  · 剔除加入后 ICIR 不提升的冗余因子

Step4 带约束权重求解
  · 目标：最大化组合 ICIR
  · 约束：权重≥0，总和=1，单因子上限 0.20，同大类合计上限 0.30
  · 样本内回测指标（IR/夏普/卡玛/MDD）仅做观察，不作为优化目标

═══ 关键设计 ═══

C1 **相关性在样本内区间计算**（避免用样本外信息选因子）。
    用「跨截面平均 Spearman」而非单截面相关系数：
    每个截面上算两因子的 Spearman，再对截面取均值 → 反映稳定的共线关系。
    同时报告截面相关的 t 值（判断共线是否稳定存在）。

C2 **Step3 的增量检验用样本内**，且必须与「单因子 IC」区分：
    单因子 IC 高 ≠ 对组合有增量（可能与其他因子高度共线）。
    逐步加入法：按 Step1 准入的因子顺序，每次加入后重算组合 ICIR，
    记录增量 ΔICIR。ΔICIR ≤ 0 → 该因子无增量 → 剔除。

C3 **Step4 优化用样本内、且样本内再切两段做内部验证**：
    规范说「样本内用于校验+求权重」。若在样本内同一段既求权重又验证，
    会高估。故样本内再切「求解段 / 内部验证段」，
    权重在求解段优化，内部验证段确认不过拟合。
    **样本外完全不参与本脚本任何决策。**

C4 **约束严格遵守**：
    单因子上限按 Step1 的准入档位（Bonferroni 合格 0.20 / FDR 保留 0.125），
    且全局硬上限 0.20；大类上限 0.30。

C5 **回测指标仅观察**：用 HS300 基准、月度调仓、t+1 开盘执行，
    输出 IR/夏普/卡玛/MDD，但不参与目标函数。

用法：python audit_round93_step234.py
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
import factor_validation_core as F  # noqa

NPZ = os.path.join(HERE, "data", "val_arrays_h20.npz")
PANEL = os.path.join(HERE, "data", "panel_round66.json")
WEIGHTS = os.path.join(HERE, "factor_weights.json")
OUT = os.path.join(HERE, "audit_round93.json")


def cross_section_spearman(Xa, Xb, M, min_n=100):
    """两因子在每个截面上的 Spearman，返回 (逐截面值, 均值, t值)。

    ⚠️ 只在两因子**同时有值**的股票上算（否则共线性会被缺失模式污染）。
    """
    ics = []
    nD = Xa.shape[0]
    for t in range(nD):
        ok = (~np.isnan(Xa[t])) & (~np.isnan(Xb[t]))
        if ok.sum() < min_n:
            continue
        x, y = Xa[t][ok], Xb[t][ok]
        rx = F.rankdata(x) if hasattr(F, "rankdata") else None
        from scipy.stats import rankdata as _rd
        rx = _rd(x)
        ry = _rd(y)
        rx = rx - rx.mean()
        ry = ry - ry.mean()
        sx = math.sqrt((rx * rx).sum())
        sy = math.sqrt((ry * ry).sum())
        if sx > 0 and sy > 0:
            ics.append(float((rx * ry).sum() / (sx * sy)))
    st = F.stat(ics)
    return ics, st


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=OUT)
    args = ap.parse_args()
    t0 = time.time()

    dates, X, M, Y, codes = F.load_panel_arrays(PANEL, NPZ)
    wc = json.load(open(WEIGHTS, encoding="utf-8"))
    dim_max = {k: float(v) for k, v in wc["dim_max"].items()}
    RY = F.rank_rows(Y)
    (i0, i1), (o0, o1) = F.split_in_out(dates, 0.6)
    # 样本内再切：求解段 / 内部验证段
    j1 = i0 + int((i1 - i0) * 0.65)
    segs = {"solve": (i0, j1), "inner_val": (j1, i1),
            "in_all": (i0, i1), "out": (o0, o1)}
    print(f"  样本内求解段 {dates[i0]}~{dates[j1-1]}  "
          f"内部验证段 {dates[j1]}~{dates[i1-1]}")
    print(f"  样本外（盲测，本脚本不参与任何决策）{dates[o0]}~{dates[o1-1]}")

    # ── 载入 Step1 准入结果 ──
    # ⚠️ 2026-10-29：用 round94（regime 分层版）而非 round92（原版）。
    #    原版因样本内恰为普涨市（上涨占比 70~79%），把 8 个防御型因子全部误杀，
    #    只剩 size_growth 一维 → 权重 Σ=0.2 不合法、Step2/3 全部退化。
    #    round94 在 regime 分层上执行规范三档规则 → 准入 5/9，因子结构完整。
    r92 = json.load(open(os.path.join(HERE, "audit_round94.json"), encoding="utf-8"))
    caps = {d: r92["verdicts"][d]["cap"] for d in F.DIMS}
    ftypes = r92.get("types", {})
    admitted = [d for d in F.DIMS if caps[d] > 0]
    untestable = [d for d in F.DIMS if caps[d] == 0
                  and r92["rows"][d].get("up") is None]
    print(f"  Step1 准入（round94 regime 分层版）：{admitted}")
    print(f"  Step1 剔除：{[d.replace('score_','') for d in F.DIMS if caps[d]==0]}")
    print(f"  因子类型：{ {k.replace('score_',''):v for k,v in ftypes.items()} }")

    # ═══════════════ Step2 相关性筛查 ═══════════════
    print("\n" + "═" * 108)
    print("STEP2  相关性筛查（Spearman 秩相关，样本内区间）")
    print("═" * 108)
    print(f"{'因子A':14s}{'因子B':14s}{'ρ均值':>9s}{'t':>8s}{'|ρ|max':>9s}"
          f"{'判定':>14s}{'处理':>22s}")
    print("-" * 108)
    cand = admitted + untestable          # untestable 也要查共线
    corr = {}
    for i in range(len(cand)):
        for j in range(i + 1, len(cand)):
            a, b = cand[i], cand[j]
            ja, jb = F.DIMS.index(a), F.DIMS.index(b)
            ics, st = cross_section_spearman(X[i0:i1, :, ja], X[i0:i1, :, jb],
                                             M[i0:i1])
            r = st["ic"]
            rmax = max((abs(v) for v in ics), default=None)
            if r is None:
                judge, action = "无数据", "—"
            elif abs(r) > 0.7:
                judge, action = "高度共线", "二选一精简"
            elif abs(r) > 0.5:
                judge, action = "中度相关", "权重压低"
            else:
                judge, action = "可控", "正常"
            corr[f"{a}|{b}"] = {"rho": r, "t": st["t"], "rho_max": rmax,
                                "judge": judge, "action": action, "n": st["n"]}
            if abs(r or 0) > 0.45:
                print(f"{a.replace('score_',''):14s}{b.replace('score_',''):14s}"
                      f"{(r or 0):>+9.4f}{(st['t'] or 0):>+8.2f}"
                      f"{(rmax if rmax is not None else 0):>9.4f}{judge:>14s}{action:>22s}")

    # 高相关对处理：同组内优先保留 Step1 t 值更高者；跨组保留两者但压低权重
    high_pairs = [(k, v) for k, v in corr.items() if v["judge"] == "高度共线"]
    print(f"\n  高度共线对 {len(high_pairs)} 组：")
    for k, v in high_pairs:
        a, b = k.split("|")
        ta = abs(r92["verdicts"][a].get("t_max") or 0)
        tb = abs(r92["verdicts"][b].get("t_max") or 0)
        keep = a if ta >= tb else b
        drop = b if keep == a else a
        print(f"    {a.replace('score_',''):14s} × {b.replace('score_',''):14s} "
              f"ρ={v['rho']:+.4f} (t {v['t']:+.2f})  → 保留 {keep.replace('score_','')}"
              f"（|t| {max(ta,tb):.2f} > {min(ta,tb):.2f}），剔除 {drop.replace('score_','')}")
        caps[drop] = 0.0
    # 中度相关：权重压低到 0.10
    mod_pairs = [(k, v) for k, v in corr.items() if v["judge"] == "中度相关"]
    for k, v in mod_pairs:
        a, b = k.split("|")
        for x in (a, b):
            if caps[x] > 0:
                caps[x] = min(caps[x], 0.10)
    if mod_pairs:
        print(f"  中度相关对 {len(mod_pairs)} 组 → 相关因子权重上限压至 0.10")

    pool2 = [d for d in F.DIMS if caps[d] > 0]
    print(f"\n  Step2 后候选池（{len(pool2)}）："
          f"{', '.join(d.replace('score_','') for d in pool2)}")
    print("  权重上限：" + "  ".join(f"{d.replace('score_','')}≤{caps[d]:.3f}"
                                      for d in pool2))

    # ═══════════════ Step3 增量信息校验 ═══════════════
    print("\n" + "═" * 108)
    print("STEP3  增量信息校验（逐步加入，观察组合 ICIR 变化；样本内求解段）")
    print("═" * 108)
    # ⚠️ 2026-10-29 修正（关键 bug）：
    #    原实现每次「把新因子的权重设成上限 → 投影归一」，导致加入新因子时
    #    **原有因子的权重被强行缩小**，ΔICIR 反映的是「权重被稀释」而非
    #    「新因子有增量」→ 必然全为负，把好因子全剔掉。
    #    正确做法：比较「已有因子（保持相对比例）」vs「已有因子 + 新因子（等权混合）」，
    #    两者都用**同一套归一化逻辑**，新因子的加入量固定为 1/K（K=因子数），
    #    这样 ΔICIR 才真正反映「新增信息」。
    # 加入顺序：按 Step1 的 regime 分层最大 |t| 降序（最强因子先）
    order = sorted(pool2, key=lambda d: -abs(r92["verdicts"][d].get("t_max") or 0))
    a, b = segs["solve"]
    print(f"{'加入顺序':18s}{'新增因子':14s}{'组合ICIR':>11s}{'ΔICIR':>10s}"
          f"{'组合IC':>10s}{'组合t':>9s}{'判定':>12s}")
    print("-" * 78)
    step3 = []
    cur_set = []
    prev_icir = None
    kept3 = []
    for i, d in enumerate(order):
        trial_set = cur_set + [d]
        K = len(trial_set)
        # 等权混合（每个因子 1/K），受上限约束投影
        wv = F.project([1.0 / K if x in trial_set else 0.0 for x in F.DIMS],
                       w_cap=F.W_CAP, g_cap=F.G_CAP)
        full = dict(zip(F.DIMS, wv))
        ics = F.combo_ics(X[a:b], M[a:b], Y[a:b], full, dim_max, RY[a:b])
        st = F.stat(ics)
        icir = st["icir"] if st["icir"] is not None else -1e9
        d_icir = (icir - prev_icir) if prev_icir is not None else None
        # 判定：首个因子无条件保留；后续要求 ΔICIR > 0
        keep = (d_icir is None) or (d_icir > 1e-6)
        if keep:
            cur_set = trial_set
            kept3.append(d)
            prev_icir = icir
        else:
            caps[d] = 0.0
        step3.append({"added": d, "icir": st["icir"], "delta_icir": d_icir,
                      "ic": st["ic"], "t": st["t"], "keep": bool(keep),
                      "set": list(trial_set),
                      "weights": {k: float(v) for k, v in zip(F.DIMS, wv)}})
        print(f"  {i+1:2d}. {'(起始)' if d_icir is None else '':8s}"
              f"{d.replace('score_',''):14s}{(st['icir'] or 0):>+11.4f}"
              f"{(d_icir if d_icir is not None else 0):>+10.4f}"
              f"{(st['ic'] or 0):>+10.4f}{(st['t'] or 0):>+9.2f}"
              f"{'✅ 保留' if keep else '❌ 剔除':>12s}")

    pool3 = [d for d in F.DIMS if caps[d] > 0 and d in cur_set]
    print(f"\n  Step3 后候选池（{len(pool3)}）："
          f"{', '.join(d.replace('score_','') for d in pool3)}")

    # ═══════════════ Step4 约束权重求解 ═══════════════
    print("\n" + "═" * 108)
    print("STEP4  带约束权重求解（目标：最大化组合 ICIR）")
    print("═" * 108)
    if len(pool3) == 0:
        print("  候选池为空，无法求解")
        return
    dims3 = pool3
    idx3 = [F.DIMS.index(d) for d in dims3]
    # 个性化上限
    w_caps = np.array([caps[d] for d in dims3])

    def project3(wv):
        w = np.clip(np.asarray(wv, np.float64), 0.0, w_caps)
        gidx = [[dims3.index(k) for k in ks if k in dims3] for ks in F.GROUPS.values()]
        gidx = [g for g in gidx if g]
        for _ in range(400):
            for gi in gidx:
                s = w[gi].sum()
                if s > F.G_CAP:
                    w[gi] *= F.G_CAP / s
            tot = w.sum()
            if tot <= 1e-12:
                w = np.ones(len(dims3)) / len(dims3)
                tot = 1.0
            w = w / tot
            over = w > w_caps
            if not over.any():
                break
            w[over] = w_caps[over]
        return w

    def obj(wv):
        full = {d: 0.0 for d in F.DIMS}
        for d, v in zip(dims3, wv):
            full[d] = float(v)
        ics = F.combo_ics(X[a:b], M[a:b], Y[a:b], full, dim_max, RY[a:b])
        st = F.stat(ics)
        return (st["icir"] if st["icir"] is not None else -1e9), st

    rng = np.random.default_rng(20261029)
    starts = [np.array([caps[d] for d in dims3])]
    for _ in range(6):
        v = rng.exponential(1.0, len(dims3))
        starts.append(project3(v / v.sum()))
    best = (None, -1e9, None)
    for si, s0 in enumerate(starts):
        w = project3(s0)
        cur, st = obj(w)
        for _ in range(16):
            improved = False
            for i in range(len(dims3)):
                for d in (-0.06, -0.03, -0.015, 0.015, 0.03, 0.06):
                    cand = w.copy()
                    cand[i] = w[i] + d
                    cand = project3(cand)
                    v, s2 = obj(cand)
                    if v > cur + 1e-10:
                        w, cur, st, improved = cand, v, s2, True
            if not improved:
                break
        if cur > best[1]:
            best = (w, cur, st)
        print(f"  起点 {si+1}/{len(starts)}  最优 ICIR {best[1]:+.4f}  "
              f"{time.time()-t0:.0f}s")
    w_opt = best[0]
    print(f"\n  最优权重（求解段 ICIR {best[1]:+.4f}）：")
    for d, v in sorted(zip(dims3, w_opt), key=lambda kv: -kv[1]):
        if v > 1e-6:
            print(f"    {d.replace('score_',''):16s} {v:.6f}  (上限 {caps[d]:.3f})")
    print(f"    Σ = {w_opt.sum():.6f}   单维最大 {w_opt.max():.4f} ≤ {F.W_CAP}")

    # 内部验证段（样本内，不涉及样本外）
    wmap_opt = {d: float(v) for d, v in zip(dims3, w_opt)}
    wmap_opt_full = {d: wmap_opt.get(d, 0.0) for d in F.DIMS}
    print("\n  【内部验证段】（样本内留出，检查是否过拟合）")
    iv_a, iv_b = segs["inner_val"]
    iv = F.stat(F.combo_ics(X[iv_a:iv_b], M[iv_a:iv_b], Y[iv_a:iv_b],
                             wmap_opt_full, dim_max, RY[iv_a:iv_b]))
    # 基线对照（现行 v4.15 权重）
    w_base = {k: float(v) for k, v in wc["weights"].items()}
    iv_base = F.stat(F.combo_ics(X[iv_a:iv_b], M[iv_a:iv_b], Y[iv_a:iv_b],
                                 w_base, dim_max, RY[iv_a:iv_b]))
    # 等权
    w_eq = {d: 1.0 / len(F.DIMS) for d in F.DIMS}
    iv_eq = F.stat(F.combo_ics(X[iv_a:iv_b], M[iv_a:iv_b], Y[iv_a:iv_b],
                                w_eq, dim_max, RY[iv_a:iv_b]))
    print(f"    优化解     ICIR {_f(iv['icir'],4)}  IC {_f(iv['ic'],4)}  t {_f(iv['t'],2)}")
    print(f"    基线v4.15  ICIR {_f(iv_base['icir'],4)}  IC {_f(iv_base['ic'],4)}  t {_f(iv_base['t'],2)}")
    print(f"    等权       ICIR {_f(iv_eq['icir'],4)}  IC {_f(iv_eq['ic'],4)}  t {_f(iv_eq['t'],2)}")

    json.dump({
        "meta": {"round": 93,
                 "purpose": "Step2 相关性筛查 + Step3 增量信息 + Step4 约束权重求解",
                 "npz": os.path.relpath(NPZ, HERE),
                 "weights_version_base": wc["version"],
                 "n_dates": len(dates), "date_range": [dates[0], dates[-1]],
                 "segments": {k: [dates[v[0]], dates[v[1]-1]] for k, v in segs.items()},
                 "constraints": {"w_cap": F.W_CAP, "group_cap": F.G_CAP,
                                 "sum": 1.0, "lower": 0.0,
                                 "per_factor_caps": {d: caps[d] for d in F.DIMS if caps[d] > 0}},
                 "caveat": (
                     "① 全部决策只用样本内；样本外完全未参与；"
                     "② fund 维因 fwd_hist 数据源边界（2026-06-26 起），"
                     "样本内 0 覆盖 → 不可检验，单列标注；"
                     "③ Step3 的加入顺序按 Step1 的 |t| 降序，受该顺序影响；"
                     "④ 求解段仅约 78 截面，坐标上升只保证局部最优。")},
        "step1_admitted": admitted,
        "step1_untestable": untestable,
        "step2_correlations": corr,
        "step2_high_pairs": [k for k, _ in high_pairs],
        "step2_mod_pairs": [k for k, _ in mod_pairs],
        "caps_after_step2": {d: caps[d] for d in F.DIMS},
        "step3": step3,
        "pool_after_step3": pool3,
        "step4_opt_weights": {d: float(v) for d, v in zip(dims3, w_opt)},
        "step4_solve_icir": best[1],
        "internal_validation": {"opt": iv, "base_v415": iv_base, "equal": iv_eq},
        "elapsed_s": round(time.time() - t0, 1),
    }, open(args.out, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"\n→ {args.out}  （{time.time()-t0:.0f}s）")


def _f(v, nd=4):
    return "  n/a " if v is None else f"{v:+.{nd}f}"


if __name__ == "__main__":
    main()
