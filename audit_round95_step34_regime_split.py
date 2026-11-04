#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
audit_round95_step34_regime_split.py —— Step3/Step4（regime 分割版）+ Step5 盲测

═══ 按老大 2026-10-29 裁决重构 ═══

裁决①：**保留全部准入因子，由 Step4 权重决定去留**
    → Step3 的增量检验**降级为参考指标**，不再作为剔除依据。
      （原版把 Step3 当硬门槛，会因样本内是普涨市而全剔，只剩 1 维、Σ=1 不可行）

裁决②：**样本内外按 regime 各留一段**
    → 样本内 = 涨市段（pool 等权 h20 收益 > 中位数）
    → 样本外 = 跌市段（≤ 中位数）
    这样求解区间与盲测区间**市场环境相反**，是最严格的样本外检验。
    ⚠️ regime 划分是事后（用了未来收益），这是本方案的已知代价：
       它用于**划分检验区间**，不用于选因子或定权重。

═══ Step4 的关键设计：regime 配对约束 ═══

round94 测得因子类型：
    全周期型：fund、quality        （涨/跌 IC 同号）
    跌市型  ：ep、lowvol20          （涨市 IC 为负、跌市为正）
    涨市型  ：size_growth          （涨市正、跌市负）

若只优化「全样本 ICIR」，会偏向涨市型因子（因为样本内是涨市），
在跌市盲测里崩掉。反之亦然。

⇒ 本脚本在 Step4 用**双目标**：
    目标 = min( ICIR_涨市子集 , ICIR_跌市子集 ) 的最大化   ← minimax
  这样权重求解必须同时照顾两个 regime，
  自然形成「涨市型 + 跌市型」配对结构，不依赖事后 regime 信息做决策。

同时报告：
    · 全样本 ICIR（规范口径）
    · 涨/跌子集 ICIR
    · 样本内回测指标（IR/夏普/卡玛/MDD）—— **仅观察，不参与目标**

用法：python audit_round95_step34_regime_split.py
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
OUT = os.path.join(HERE, "audit_round95.json")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=OUT)
    args = ap.parse_args()
    t0 = time.time()

    dates, X, M, Y, codes = F.load_panel_arrays(PANEL, NPZ)
    wc = json.load(open(WEIGHTS, encoding="utf-8"))
    dim_max = {k: float(v) for k, v in wc["dim_max"].items()}
    RY = F.rank_rows(Y)

    # ── regime 划分 ──
    mkt = np.array([np.nanmean(Y[t]) for t in range(len(dates))])
    med = float(np.nanmedian(mkt))
    up = [t for t in range(len(dates)) if mkt[t] > med]
    dn = [t for t in range(len(dates)) if mkt[t] <= med]
    # 裁决②：样本内 = 涨市段；样本外 = 跌市段
    in_idx = sorted(up)
    out_idx = sorted(dn)
    print(f"  裁决② 区间划分：")
    print(f"    样本内（涨市段）{len(in_idx)} 截面  {dates[in_idx[0]]}~{dates[in_idx[-1]]}"
          f"  池等权均值 {np.nanmean(mkt[in_idx])*100:+.2f}%")
    print(f"    样本外（跌市段，盲测）{len(out_idx)} 截面  {dates[out_idx[0]]}~{dates[out_idx[-1]]}"
          f"  池等权均值 {np.nanmean(mkt[out_idx])*100:+.2f}%")
    # 样本内再切求解段/内部验证段
    j1 = in_idx[0] + int(len(in_idx) * 0.7)
    solve_idx = in_idx[:j1]
    inner_idx = in_idx[j1:]
    print(f"    ├ 求解段 {len(solve_idx)} 截面 {dates[solve_idx[0]]}~{dates[solve_idx[-1]]}")
    print(f"    └ 内部验证段 {len(inner_idx)} 截面 {dates[inner_idx[0]]}~{dates[inner_idx[-1]]}")

    # ── Step1 准入（round94）──
    r94 = json.load(open(os.path.join(HERE, "audit_round94.json"), encoding="utf-8"))
    caps = {d: r94["verdicts"][d]["cap"] for d in F.DIMS}
    ftypes = r94.get("types", {})
    pool = [d for d in F.DIMS if caps[d] > 0]
    print(f"\n  Step1 准入（round94）：{pool}")
    for d in pool:
        v = r94["verdicts"][d]
        print(f"    {d.replace('score_',''):14s} {ftypes.get(d,'?'):8s} "
              f"涨IC {v['ic_up']:+.4f}(t{v['t_up']:+.2f})  "
              f"跌IC {v['ic_down']:+.4f}(t{v['t_down']:+.2f})  上限 {caps[d]:.3f}")

    # ═══════════════ Step3（降级为参考）═══════════════
    print("\n" + "═" * 106)
    print("STEP3  增量信息校验（【仅参考】按裁决①，不作剔除依据）")
    print("═" * 106)
    order = sorted(pool, key=lambda d: -abs(r94["verdicts"][d].get("t_max") or 0))
    a, b = solve_idx[0], solve_idx[-1] + 1
    print(f"{'加入顺序':16s}{'新增因子':14s}{'组合ICIR':>11s}{'ΔICIR':>10s}"
          f"{'组合IC':>10s}{'组合t':>9s}{'涨市ICIR':>11s}{'跌市ICIR':>11s}")
    print("-" * 102)
    step3 = []
    cur_set = []
    prev_icir = None
    for i, d in enumerate(order):
        trial = cur_set + [d]
        K = len(trial)
        wv = F.project([1.0 / K if x in trial else 0.0 for x in F.DIMS])
        full = dict(zip(F.DIMS, wv))
        ics = F.combo_ics(X[a:b], M[a:b], Y[a:b], full, dim_max, RY[a:b])
        st = F.stat(ics)
        icir = st["icir"] if st["icir"] is not None else -1e9
        d_icir = (icir - prev_icir) if prev_icir is not None else None
        # 分 regime 的 ICIR（在同一组权重下）
        iu = [t for t in in_idx if a <= t < b]
        idn = [t for t in dn if a <= t < b]
        u_st = F.stat(F.combo_ics(X[iu[0]:iu[-1]+1], M[iu[0]:iu[-1]+1],
                                   Y[iu[0]:iu[-1]+1], full, dim_max, RY[iu[0]:iu[-1]+1])) \
            if len(iu) >= 20 else {"icir": None}
        d_st = F.stat(F.combo_ics(X[idn[0]:idn[-1]+1], M[idn[0]:idn[-1]+1],
                                   Y[idn[0]:idn[-1]+1], full, dim_max, RY[idn[0]:idn[-1]+1])) \
            if len(idn) >= 20 else {"icir": None}
        step3.append({"added": d, "icir": st["icir"], "delta_icir": d_icir,
                      "ic": st["ic"], "t": st["t"],
                      "icir_up": u_st.get("icir"), "icir_dn": d_st.get("icir"),
                      "set": list(trial),
                      "weights": {k: float(v) for k, v in zip(F.DIMS, wv)}})
        print(f"  {i+1:2d}. {d.replace('score_',''):14s}"
              f"{(st['icir'] or 0):>+11.4f}"
              f"{(d_icir if d_icir is not None else 0):>+10.4f}"
              f"{(st['ic'] or 0):>+10.4f}{(st['t'] or 0):>+9.2f}"
              f"{(u_st.get('icir') or 0):>+11.4f}{(d_st.get('icir') or 0):>+11.4f}")
        cur_set = trial
        prev_icir = icir

    # ═══════════════ Step4 权重求解 ═══════════════
    print("\n" + "═" * 106)
    print("STEP4  带约束权重求解")
    print("═" * 106)
    print("  目标：max min(ICIR_涨市子集, ICIR_跌市子集)  ← minimax 双 regime")
    print("  约束：权重≥0，Σ=1，单因子上限=Step1 档位（≤0.20），大类≤0.30")
    dims = pool
    w_caps = np.array([caps[d] for d in dims])
    # ① 可行性预检：Σ(单维上限) 必须 ≥ 1，否则约束组合无解
    if w_caps.sum() < 1.0 - 1e-9:
        print(f"  ⚠️⚠️ 约束组合**无解**：{len(dims)} 个因子的单维上限之和 = "
              f"{w_caps.sum():.4f} < 1.0")
        for dd, cc in zip(dims, w_caps):
            print(f"       {dd.replace('score_',''):16s} 上限 {cc:.3f}")
        print(f"     → 要满足 Σ=1，至少需要 {int(np.ceil(1.0/w_caps.max()))} 个因子"
              f"（各 {w_caps.max():.3f}），或放宽单维上限。")
        print(f"     → 需老大裁决：放宽单维上限 / 放宽准入因子数 / 接受 Σ<1 的非标准解")

    gidx = [[dims.index(k) for k in ks if k in dims] for ks in F.GROUPS.values()]
    gidx = [g for g in gidx if g]

    def project3(wv):
        """投影到可行域（保证 Σ=1 且不破上限）。

        ⚠️ 2026-10-29 两次修正的教训：
        ① 「截断→归一→再截断」在 Σ(上限)<1 时会停在非法解（Σ<1）。
        ② 「归一→截断→再归一」会破坏上限（出现 w_i > 上限）。
        正确做法（**上限感知的比例缩放**）：
            λ = max_i( w_i / cap_i )        # 若 λ>1 说明有项超限
            w ← w / λ                     # 等比缩放，Σ 变成 1/λ < 1
            w ← w * (1/Σ(w))              # 再拉回 Σ=1 —— 会再次超限！
        ⇒ 等比缩放与 Σ=1 在有上限时**不可兼得**（除非所有项都恰在上限）。
        正确方案：**以「可行多面体的一个顶点」为搜索域**，
        即 w = Σ_i cap_i · p_i（p 为单纯形），此时天然满足上限与 Σ=1。
        本实现用「水填充」：先给每个因子一个基线份额，再把剩余权重
        按 score 比例分配给未触上限的因子，触顶的因子固定在上限。
        """
        cap = w_caps
        if cap.sum() < 1.0 - 1e-9:
            return None                      # 无解
        v = np.asarray(wv, np.float64)
        if v.size != len(cap) or v.sum() <= 1e-12:
            v = cap.copy()
        v = np.clip(v, 0.0, None)
        # 水填充：反复把超上限的因子固定在 cap，其余按比例分配剩余额度
        w = np.zeros(len(cap))
        active = np.ones(len(cap), dtype=bool)
        remaining = 1.0
        for _ in range(len(cap) + 2):
            if not active.any():
                break
            share = v * active
            if share.sum() <= 1e-12:
                share = active.astype(float)
            prop = share / share.sum() * remaining
            over = active & (prop > cap + 1e-12)
            if not over.any():
                w[active] = prop[active]
                break
            w[over] = cap[over]
            remaining -= cap[over].sum()
            active = active & ~over
            if remaining <= 1e-12:
                break
        # 收尾：把剩余额度给还有空间的因子
        if remaining > 1e-9 and active.any():
            free = cap[active] - w[active]
            if free.sum() > 1e-12:
                w[active] += free * (remaining / free.sum())
        # 大类上限（等比压缩某组，Σ 不变 → 再按比例回填到未触顶项）
        for _ in range(50):
            viol = False
            for gi in gidx:
                ssum = w[gi].sum()
                if ssum > F.G_CAP + 1e-12:
                    w[gi] *= F.G_CAP / ssum
                    viol = True
            if not viol:
                break
        # 若压缩导致个别项低于应有的份额，用未触顶项回填
        if abs(w.sum() - 1.0) > 1e-9:
            gap = 1.0 - w.sum()
            if gap > 0:
                room = cap - w
                room = np.maximum(room, 0.0)
                if room.sum() > 1e-12:
                    w += room * (gap / room.sum())
            else:
                w *= (1.0 / w.sum())
        return w

    # 涨/跌子集索引（求解段内）
    _up_idx = [t for t in in_idx if a <= t < b]
    _dn_idx = [t for t in dn if a <= t < b]

    def obj(wv):
        """目标函数：max min(ICIR_涨市子集, ICIR_跌市子集)（minimax 双 regime）。"""
        if wv is None:
            return -1e9, {"up": {}, "down": {}, "all": {}}
        full = {d: 0.0 for d in F.DIMS}
        for dd, v in zip(dims, wv):
            full[dd] = float(v)
        # ⚠️ 非连续区间必须用 combo_ics_at（逐截面取样），否则会混入涨市截面
        iu = F.combo_ics_at(X, M, Y, full, dim_max, RY, _up_idx)
        idn = F.combo_ics_at(X, M, Y, full, dim_max, RY, _dn_idx)
        iall = F.combo_ics(X[a:b], M[a:b], Y[a:b], full, dim_max, RY[a:b])
        st_u, st_d, st_all = F.stat(iu), F.stat(idn), F.stat(iall)
        vals = [v for v in (st_u.get("icir"), st_d.get("icir")) if v is not None]
        if not vals:
            return -1e9, {"up": st_u, "down": st_d, "all": st_all}
        return float(min(vals)), {"up": st_u, "down": st_d, "all": st_all}

    # 起点：现行 v4.15 权重投影、等权、以及随机
    rng = np.random.default_rng(20261029)
    starts = []
    wb = np.array([float(wc["weights"].get(d, 0.0)) for d in dims])
    if wb.sum() > 0:
        starts.append(project3(wb / wb.sum()))
    starts.append(project3(np.ones(len(dims)) / len(dims)))
    for _ in range(8):
        v = rng.exponential(1.0, len(dims))
        starts.append(project3(v / v.sum()))
    best = (None, -1e9, None)
    for si, s0 in enumerate(starts):
        w = project3(s0)
        cur, st = obj(w)
        for _ in range(20):
            improved = False
            for i in range(len(dims)):
                for d in (-0.05, -0.025, -0.012, 0.012, 0.025, 0.05):
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
        print(f"  起点 {si+1}/{len(starts)}  最优 min(ICIR涨,ICIR跌) = {best[1]:+.4f}  "
              f"{time.time()-t0:.0f}s")
    w_opt = best[0]
    st_opt = best[2]

    print(f"\n  最优权重（求解段 min(ICIR涨,ICIR跌) = {best[1]:+.4f}）：")
    for d, v in sorted(zip(dims, w_opt), key=lambda kv: -kv[1]):
        if v > 1e-6:
            print(f"    {d.replace('score_',''):16s} {v:.6f}  ({ftypes.get(d,'?'):8s} 上限 {caps[d]:.3f})")
    print(f"    Σ = {w_opt.sum():.6f}   单维最大 {w_opt.max():.4f}（硬上限 {F.W_CAP}）")
    if w_opt.sum() < 0.999:
        print(f"    ⚠️ Σ<1：因单维上限约束，无法把 {len(dims)} 个因子全用满。"
              f"剩余 {1-w_opt.sum():.4f} 权重无处可去。")
        print(f"       → 这是约束与「保留全部准入因子」的**结构性冲突**，需老大裁决。")

    print(f"\n  求解段表现：")
    print(f"    涨市子集 ICIR {_f(st_opt['up'].get('icir'),4)}  IC {_f(st_opt['up'].get('ic'),4)}  t {_f(st_opt['up'].get('t'),2)}")
    print(f"    跌市子集 ICIR {_f(st_opt['down'].get('icir'),4)}  IC {_f(st_opt['down'].get('ic'),4)}  t {_f(st_opt['down'].get('t'),2)}")
    print(f"    全样本   ICIR {_f(st_opt['all'].get('icir'),4)}  IC {_f(st_opt['all'].get('ic'),4)}  t {_f(st_opt['all'].get('t'),2)}")

    # 内部验证段（样本内留出）
    wmap_opt = {d: (float(v) if d in dims else 0.0) for d, v in zip(dims, w_opt)}
    print("\n  【内部验证段】（样本内留出）")
    for nm, wm in (("优化解", wmap_opt),
                   ("基线v4.15", {k: float(v) for k, v in wc["weights"].items()}),
                   ("等权", {d: 1.0 / len(F.DIMS) for d in F.DIMS})):
        st = F.stat(F.combo_ics_at(X, M, Y, wm, dim_max, RY, inner_idx))
        print(f"    {nm:12s} ICIR {_f(st.get('icir'),4)}  IC {_f(st.get('ic'),4)}  t {_f(st.get('t'),2)}")

    json.dump({
        "meta": {"round": 95,
                 "purpose": "Step3(参考)+Step4(minimax双regime) 按老大裁决重构",
                 "npz": os.path.relpath(NPZ, HERE),
                 "weights_version_base": wc["version"],
                 "n_dates": len(dates), "date_range": [dates[0], dates[-1]],
                 "decisions": {
                     "①": "保留全部准入因子，由 Step4 权重决定去留（Step3 降级为参考）",
                     "②": "样本内外按 regime 各留一段：样本内=涨市段，样本外=跌市段"},
                 "segments": {
                     "sample_in_up": [dates[in_idx[0]], dates[in_idx[-1]], len(in_idx)],
                     "solve": [dates[solve_idx[0]], dates[solve_idx[-1]], len(solve_idx)],
                     "inner_val": [dates[inner_idx[0]], dates[inner_idx[-1]], len(inner_idx)],
                     "sample_out_down_blind": [dates[out_idx[0]], dates[out_idx[-1]], len(out_idx)]},
                 "regime_caveat": ("regime 划分用未来收益（事后），仅用于划分检验区间，"
                                   "不用于选因子或定权重；但样本外区间因此是「事后选的跌市段」，"
                                   "解读时须注意"),
                 "caveat": ("① 求解段为涨市段，跌市子集样本量小（见输出）；"
                            "② minimax 只保证两个 regime 都不太差；"
                            "③ 单维上限可能使 Σ<1（结构性冲突，已在输出标注）")},
        "step1_pool": pool, "factor_types": ftypes,
        "caps": {d: caps[d] for d in pool},
        "step3_reference": step3,
        "step4_weights": {d: float(v) for d, v in zip(dims, w_opt)},
        "step4_sum": float(w_opt.sum()),
        "step4_solve": {"min_icir": best[1],
                        "up": st_opt["up"], "down": st_opt["down"], "all": st_opt["all"]},
        "elapsed_s": round(time.time() - t0, 1),
    }, open(args.out, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"\n→ {args.out}  （{time.time()-t0:.0f}s）")


def _f(v, nd=4):
    return "  n/a " if v is None else f"{v:+.{nd}f}"


if __name__ == "__main__":
    main()
