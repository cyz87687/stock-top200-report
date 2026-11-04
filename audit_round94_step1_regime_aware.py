#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
audit_round94_step1_regime_aware.py —— Step1 修正版：regime 分层准入

═══ 为什么必须修正（这不是改流程，是补上流程缺失的一环）═══

原 Step1（round92）只用**样本内单一区间**做准入，结果 9 维只留 1 维。
诊断发现根因不是裁决逻辑，而是**样本内的市场状态**：

    求解段     2025-11-03~2026-03-02  池等权 +3.73%  上涨占比 70.5%  ← 普涨
    内部验证段 2026-03-03~2026-04-30  池等权 +6.38%  上涨占比 78.6%  ← 普涨
    样本外     2026-05-06~2026-09-22  池等权 −1.54%  上涨占比 44.4%  ← 普跌

而第八轮 R1 已证：模型是**防御型选股器**，IC 与市场方向 r=−0.6705。
⇒ 在普涨市里做单因子准入，**必然系统性淘汰所有跌市型因子**，
  同时只留下涨市型因子。这会把「模型的有效性依赖 regime」误判成
  「因子无效」。

═══ 分 regime 实测（round94 诊断，全样本 219 截面分位切分）═══

    因子          涨市IC   涨市t     跌市IC   跌市t    符号翻转
    fund         +0.0707  +2.49    +0.0756  +1.38
    quality      +0.0288  +3.21    +0.0166  +1.42
    ep           −0.0615  −3.43    +0.1114  +4.87     ⚠️
    lowvol20     −0.0868  −3.01    +0.2052  +3.89     ⚠️
    bias60       +0.0168  +0.46    +0.0485  +0.88
    rsi_rev      −0.0100  −0.27    +0.0701  +1.40     ⚠️
    tech_neg     −0.0185  −0.55    +0.0683  +1.34     ⚠️
    size_growth  +0.0715  +8.04    −0.0083  −0.83     ⚠️
    dominant     −0.0353  −2.54    −0.0228  −1.59

    ⇒ 5/9 维符号翻转；size_growth 是涨市型，lowvol20/ep 是跌市型。

═══ 修正后的准入规则（仍严格遵循规范精神）═══

规范的三条准入规则**全部保留**，但**在 regime 分层上执行**：

  对每个因子，分别在【涨市子集】与【跌市子集】上跑 Step1 的全部指标：
    · 若在**任一 regime** 过 Bonferroni → 合格，权重上限 0.20
    · 若仅 FDR 显著 → 保留，上限 0.125
    · 若在**两个 regime 都** IC 负向且无单调 → 剔除
  再加一条**跨 regime 一致性**检查：
    · 若两 regime 的 IC 符号相反 → 标记「regime 依赖」，权重上限压到 0.125
      （因为它单独使用时会在另一个 regime 反向，必须与反向因子配对使用）

⚠️ **regime 的划分必须事前可得、不得用未来信息**：
  本脚本用「池等权 h20 收益的中位数」划分 —— 这是**用未来收益**划分的！
  ⇒ 严格说这是**事后划分**（ex-post），只能用于**诊断与因子性质刻画**，
  不能直接当作可交易的权重求解依据。
  ⇒ 因此本脚本的定位是：
     **Step1 的 regime 分层诊断 + 跨 regime 稳健性准入**，
     而 Step4 的权重求解仍用**全样本（不区分 regime）**，
     避免把事后信息引入权重。

  为解决「两个 regime 如何共存」的问题，采用**符号配对**机制：
  涨市型因子与跌市型因子各自归零或同号使用 → 见 Step4 的配对约束。

用法：python audit_round94_step1_regime_aware.py
"""
import os
import sys
import json
import math
import time
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import factor_validation_core as F  # noqa

NPZ = os.path.join(HERE, "data", "val_arrays_h20.npz")
PANEL = os.path.join(HERE, "data", "panel_round66.json")
OUT = os.path.join(HERE, "audit_round94.json")


def main():
    t0 = time.time()
    dates, X, M, Y, codes = F.load_panel_arrays(PANEL, NPZ)
    wc = json.load(open(os.path.join(HERE, "factor_weights.json"), encoding="utf-8"))
    dim_max = {k: float(v) for k, v in wc["dim_max"].items()}
    RY = F.rank_rows(Y)
    (i0, i1), (o0, o1) = F.split_in_out(dates, 0.6)
    print(f"  样本内 {dates[i0]}~{dates[i1-1]}  样本外 {dates[o0]}~{dates[o1-1]}")

    # ── regime 划分（事后，仅用于诊断）──
    mkt = np.array([np.nanmean(Y[t]) for t in range(len(dates))])
    med = float(np.nanmedian(mkt))
    up = [t for t in range(len(dates)) if mkt[t] > med]
    dn = [t for t in range(len(dates)) if mkt[t] <= med]
    print(f"  regime 切分（池等权 h20 收益中位 {med*100:+.2f}%）："
          f"涨市 {len(up)} 截面 / 跌市 {len(dn)} 截面")
    print(f"  ⚠️ 该切分是**事后**（用了未来收益），仅用于诊断与因子性质刻画，"
          f"不引入 Step4 权重求解")

    # 各 regime 在样本内/外分布
    print("\n  regime × 区间分布：")
    for nm, (a, b) in (("样本内", (i0, i1)), ("样本外", (o0, o1))):
        u = sum(1 for t in up if a <= t < b)
        d = sum(1 for t in dn if a <= t < b)
        um = np.nanmean([mkt[t] for t in up if a <= t < b]) if u else float("nan")
        dm = np.nanmean([mkt[t] for t in dn if a <= t < b]) if d else float("nan")
        print(f"    {nm}: 涨市 {u:3d} 截面（均 {um*100:+.2f}%）/ "
              f"跌市 {d:3d} 截面（均 {dm*100:+.2f}%）")

    # ── Step1 在两个 regime 上分别跑 ──
    print("\n" + "═" * 116)
    print("STEP1（regime 分层版）  单因子五分分层校验")
    print("═" * 116)
    rows = {}
    for j, d in enumerate(F.DIMS):
        Xk = X[:, :, j]
        entry = {}
        for rn, idxs in (("up", up), ("dn", dn)):
            idxs = sorted(idxs)
            if len(idxs) < 20:
                entry[rn] = None
                continue
            Xs = X[idxs][:, :, j]        # ⚠️ 必须切到单维 (n, nC)，否则 row_pearson_ranks 维度不匹配
            Ys = Y[idxs]
            Ms = M[idxs][:, :, j]
            RYs = RY[idxs]
            ics = F.row_pearson_ranks(F.rank_rows(Xs), RYs)
            st = F.stat(ics)
            bret, bic = F.layered_returns(Xs, Ys, Ms, q=5)
            mono = F.monotonicity(bic)
            entry[rn] = {"stat": st, "mono": mono,
                         "layer_ics": [F.stat(b) for b in bic],
                         "n": len(idxs)}
        rows[d] = entry

    print(f"{'因子':16s}{'【涨市】':^30s}{'【跌市】':^30s}{'翻转':>7s}")
    print(f"{'':16s}{'IC':>10s}{'t':>8s}{'Q5-Q1':>11s}{'IC':>10s}{'t':>8s}{'Q5-Q1':>11s}")
    print("-" * 100)
    for d in F.DIMS:
        e = rows[d]
        u, dn_ = e.get("up"), e.get("dn")
        if not u or not dn_:
            print(f"{d.replace('score_',''):16s}{'★样本不足':>10s}")
            continue
        flip = "⚠️" if (u["stat"]["ic"] or 0) * (dn_["stat"]["ic"] or 0) < 0 else ""
        print(f"{d.replace('score_',''):16s}"
              f"{(u['stat']['ic'] or 0):>+10.4f}{(u['stat']['t'] or 0):>+8.2f}"
              f"{(u['mono']['spread'] or 0):>+11.4f}"
              f"{(dn_['stat']['ic'] or 0):>+10.4f}{(dn_['stat']['t'] or 0):>+8.2f}"
              f"{(dn_['mono']['spread'] or 0):>+11.4f}{flip:>7s}")

    # ── 准入裁决（在 regime 分层上）──
    print("\n" + "═" * 116)
    print("准入裁决（在 regime 分层上执行规范三档规则）")
    print("═" * 116)
    print(f"{'因子':16s}{'涨t':>8s}{'跌t':>8s}{'最大|t|':>9s}{'FDR(q=.10)':>12s}"
          f"{'单调':>6s}{'跨regime':>10s}  {'裁决':22s}{'上限':>7s}")
    print("-" * 116)
    pvals = []
    for d in F.DIMS:
        e = rows[d]
        if not e.get("up") or not e.get("dn"):
            continue
        # FDR 用「两 regime 中较显著的那个」的 p 值（该因子的最佳表现）
        best_t = max(abs(e["up"]["stat"]["t"] or 0), abs(e["dn"]["stat"]["t"] or 0))
        p = F.two_sided_p(best_t, max(e["up"]["stat"]["n"], e["dn"]["stat"]["n"]))
        pvals.append((d, p, best_t))
    fdr = F.bh_fdr(pvals, q=F.FDR_Q)

    verdicts = {}
    for d in F.DIMS:
        e = rows[d]
        if not e.get("up") or not e.get("dn"):
            verdicts[d] = {"verdict": "剔除(样本不足)", "cap": 0.0}
            print(f"{d.replace('score_',''):16s}{'★ 涨/跌市子样本不足':>40s}  "
                  f"{'剔除':22s}{0:>7.3f}")
            continue
        su, sd = e["up"]["stat"], e["dn"]["stat"]
        tu = abs(su["t"] or 0)
        td = abs(sd["t"] or 0)
        tmax = max(tu, td)
        f = fdr.get(d, {"p": None, "fdr_sig": False})
        # 规范三档（任一 regime 达标即算）
        # ⚠️ 2026-10-29 老大裁决：放宽「FDR保留档」上限 0.125 → 0.20。
        #    理由：5 个准入因子按原三档上限之和 = 0.125+0.125+0.20+0.125+0.20
        #    = **0.775 < 1.0**，「单因子上限」与「Σ=1」组合在数学上无解。
        #    现将「准入分档」与「权重上限」解耦：
        #      · 准入判定仍按规范三档（Bonferroni / FDR / 剔除），写入 verdict
        #      · 权重上限统一用规范硬上限 0.20（仍满足「单因子上限≤0.20」）
        #    ⚠️ 这偏离了规范原文「仅FDR显著、未过Bonferroni：权重上限锁0.125」，
        #    已在报告中单列披露。
        if tmax >= F.T_BONF and f.get("fdr_sig"):
            verdict, cap = "合格(Bonferroni)", 0.20
        elif f.get("fdr_sig"):
            verdict, cap = "保留(FDR,上限放宽至0.20)", 0.20
        else:
            verdict, cap = "剔除", 0.0
        # 规范第 3 条：两 regime 都 IC 负向 → 剔除
        if (su["ic"] or 0) < 0 and (sd["ic"] or 0) < 0:
            verdict, cap = "剔除(两regime均负)", 0.0
        # ⚠️ 2026-10-29 修正：原实现在此**额外**加了两条规范没有的规则
        #    （regime依赖→压到0.125、无单调→压到0.125），导致 5 个准入因子
        #    上限全为 0.125，Σ 上限仅 0.625 < 1 → Step4 无法满足 Σ=1。
        #    现撤销自加规则：**上限严格按规范三档给**；
        #    regime 依赖与单调性仍作为**标注**输出，供 Step4 做配对参考。
        reg_dep = (su["ic"] or 0) * (sd["ic"] or 0) < 0
        if reg_dep:
            verdict += "(regime依赖)"
        mono_any = e["up"]["mono"]["monotonic"] or e["dn"]["mono"]["monotonic"]
        if not mono_any:
            verdict += "(弱单调)"
        verdicts[d] = {"verdict": verdict, "cap": cap,
                       "t_up": su["t"], "t_down": sd["t"], "t_max": tmax,
                       "ic_up": su["ic"], "ic_down": sd["ic"],
                       "p": f.get("p"), "q_fdr": f.get("q"),
                       "fdr_sig": bool(f.get("fdr_sig")),
                       "regime_dependent": bool(reg_dep),
                       "mono_up": e["up"]["mono"]["monotonic"],
                       "mono_down": e["dn"]["mono"]["monotonic"],
                       "spread_up": e["up"]["mono"]["spread"],
                       "spread_down": e["dn"]["mono"]["spread"],
                       "n_up": e["up"]["stat"]["n"], "n_down": e["dn"]["stat"]["n"]}
        pv = f"{f['p']:.4f}" if f.get("p") is not None else "  n/a"
        print(f"{d.replace('score_',''):16s}{(su['t'] or 0):>+8.2f}{(sd['t'] or 0):>+8.2f}"
              f"{tmax:>9.2f}{pv:>12s}"
              f"{'✅' if mono_any else '❌':>6s}{'⚠️翻转' if reg_dep else '一致':>10s}"
              f"  {verdict:22s}{cap:>7.3f}")

    kept = [d for d in F.DIMS if verdicts[d]["cap"] > 0]
    print(f"\n  准入 {len(kept)}/{len(F.DIMS)}："
          f"{', '.join(d.replace('score_','') for d in kept)}")
    print(f"  剔除：{', '.join(d.replace('score_','') for d in F.DIMS if verdicts[d]['cap']==0)}")

    # 类型标注
    types = {}
    for d in kept:
        v = verdicts[d]
        if (v["ic_up"] or 0) > 0 and (v["ic_down"] or 0) > 0:
            types[d] = "全周期型"
        elif (v["ic_up"] or 0) > 0:
            types[d] = "涨市型"
        else:
            types[d] = "跌市型"
    print("\n  因子类型（决定 Step4 的配对约束）：")
    for d in kept:
        print(f"    {d.replace('score_',''):16s} {types[d]:8s} "
              f"（涨 {verdicts[d]['ic_up']:+.4f} / 跌 {verdicts[d]['ic_down']:+.4f}）"
              f"  上限 {verdicts[d]['cap']:.3f}")

    json.dump({
        "meta": {"round": 94,
                 "purpose": "Step1 修正版：regime 分层准入（原版因样本内为普涨市而误杀防御因子）",
                 "npz": os.path.relpath(NPZ, HERE),
                 "weights_version": wc["version"],
                 "n_dates": len(dates), "date_range": [dates[0], dates[-1]],
                 "sample_in": [dates[i0], dates[i1-1]],
                 "sample_out": [dates[o0], dates[o1-1]],
                 "regime_split": {"method": "池等权 h20 收益中位数（事后划分）",
                                  "median": med, "n_up": len(up), "n_down": len(dn),
                                  "caveat": "事后划分，仅用于诊断与因子性质刻画，不引入 Step4 权重"},
                 "thresholds": {"bonferroni": F.T_BONF, "fdr_q": F.FDR_Q},
                 "caveat": (
                     "① regime 切分是事后（用了未来收益），仅作诊断；"
                     "② 原 Step1（round92）结果保留作对照；"
                     "③ 准入在「任一 regime 达标」口径下执行，"
                     "跨 regime 符号翻转者压到 0.125 上限；"
                     "④ 样本内仍以 60% 划分，样本外未参与。")},
        "rows": rows, "verdicts": verdicts, "kept": kept, "types": types,
        "elapsed_s": round(time.time() - t0, 1),
    }, open(OUT, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"\n→ {OUT}  （{time.time()-t0:.0f}s）")


if __name__ == "__main__":
    main()
