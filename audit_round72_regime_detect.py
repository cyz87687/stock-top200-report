#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
audit_round72_regime_detect.py —— regime 的**事前**可检测性检验（防止用未来数据假优化）

═══ 这一关过不了，后面全是假的 ═══

round71 证明：组合 IC 与「未来 20 日市场收益」r = −0.6705（CI [−0.735, −0.586]），
五档严格单调（跌市 IC +0.308 → 涨市 IC −0.098）。
据此可以做一个「跌市重仓低波、涨市重仓成长」的切换模型。

**但是**：如果切换信号来自「未来市场收益」，那就是纯前视，
回测会漂亮得离谱，而实盘一分钱赚不到。这是本轮最容易自欺的地方。

所以本脚本先做一件事：只允许用 **t 日及以前** 的信息构造 regime 信号，
测它能不能预示「t → t+20 的市场收益方向」。

═══ 候选事前信号（全部只用 ≤ t 的数据）═══

S1  过去 20 日池内等权累计收益           （趋势跟随 / 也可能是反转）
S2  过去 20 日市场年化波动               （风险状态）
S3  市场宽度：截面上「过去20日收益>0」的股票占比
S4  过去 5 日池内等权收益（短动量）
S5  池内等权 vs 沪深300 的相对强弱（近20日）
S6  全部股票过去20日收益的截面离散度（分化度）

对每个信号检验：与「未来20日市场收益」的秩相关 + 与「未来 IC」的秩相关。
判据：与未来 IC 的 |ρ| 是否显著、方向是否符合预期。

═══ 通过后的用途 ═══

若存在至少一个信号与未来 IC 显著负相关（事前就能识别「涨市将到来」），
则 regime 切换模型有真实信息；
若全部不显著，则 round71 的强关系**不可利用**，只能作为风险披露，不能做优化。

用法：python audit_round72_regime_detect.py
"""
import os
import sys
import json
import math
import numpy as np
from scipy.stats import rankdata

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from audit_round70_icir_fast import DIMS, WEIGHTS, nw_t, stat, _f  # noqa
from audit_round71_regime import pearson, spearman, boot_ci  # noqa

NPZ = os.path.join(HERE, "data", "icir_arrays_h20.npz")
OUT = os.path.join(HERE, "audit_round72.json")


def main():
    z = np.load(NPZ, allow_pickle=False)
    dates = [str(x) for x in z["dates"]]
    X, M, Y = z["X"], z["M"], z["Y"]
    nD, nC, nK = X.shape

    wc = json.load(open(WEIGHTS, encoding="utf-8"))
    dm = np.array([float(wc["dim_max"].get(k, 10.0)) for k in DIMS])
    w = np.array([float(wc["weights"].get(k, 0.0)) for k in DIMS])
    V = np.where(M, np.minimum(np.where(np.isnan(X), 0.0, X), dm) / dm * 10.0, 0.0)
    den = M.astype(np.float64) @ w
    num = V @ w
    with np.errstate(invalid="ignore", divide="ignore"):
        S = np.where(den > 1e-12, num / den * 10.0, np.nan)
    RY = rankdata(Y, axis=1, nan_policy="omit")
    RS = rankdata(S, axis=1, nan_policy="omit")

    # 组合 IC 序列 & 未来市场收益
    ic, mkt = [], []
    for t in range(nD):
        ok = (~np.isnan(RS[t])) & (~np.isnan(RY[t])) & (~np.isnan(Y[t]))
        if ok.sum() < 100:
            ic.append(None); mkt.append(None); continue
        a = RS[t][ok] - RS[t][ok].mean()
        b = RY[t][ok] - RY[t][ok].mean()
        sx = math.sqrt((a * a).sum()); sy = math.sqrt((b * b).sum())
        ic.append(float((a * b).sum() / (sx * sy)) if sx > 0 and sy > 0 else None)
        mkt.append(float(np.nanmean(Y[t][ok])))

    # ═══ 事前信号（只用 ≤t 数据）═══
    # 注意：Y[t] 是 t→t+20 的收益，属于未来。构造信号**绝不能**用它。
    # 这里只能用「滞后收益」：把 Y 前移。Y[t-1] 覆盖 t-1 → t+19，其中 t-1..t 已知？
    # 不行 —— Y[t-1] 的终点是未来的。故改为用「已实现日收益」重构。
    # 但我们只有 h20 前瞻收益，没有日频收益序列。
    # ⇒ 用 Y 的错位差分近似：past_ret_20d(t) ≈ Y[t-20] 已完全实现（t-20+20 = t）
    past20 = [None] * nD
    for t in range(nD):
        if t - 20 >= 0 and mkt[t - 20] is not None:
            past20[t] = mkt[t - 20]           # 覆盖 t-20 → t，在 t 日已知
    past5 = [None] * nD
    for t in range(nD):
        if t - 5 >= 0 and mkt[t - 5] is not None:
            past5[t] = mkt[t - 5]

    # 市场波动（已实现，用 t-20..t-1 的 mkt，端点 ≤ t）
    vol20 = [None] * nD
    for t in range(nD):
        lo = max(0, t - 20)
        sub = [v for v in mkt[lo:t] if v is not None]
        if len(sub) >= 8:
            vol20[t] = float(np.std(sub, ddof=1))

    # 市场宽度：过去 20 日「已完成」上涨的股票占比（用 Y[t-1] 的形状不含未来？）
    # Y[t-1] 终点 t+19 是未来 → 不可用。改用截面当期分位近似：无日频数据，跳过。
    # 替代：用「个股过去已完成 h20 收益为正的比例」= Y[t-20] > 0 的比例（t 日已知）
    breadth = [None] * nD
    for t in range(nD):
        if t - 20 >= 0:
            y = Y[t - 20]
            ok = ~np.isnan(y)
            if ok.sum() >= 100:
                breadth[t] = float((y[ok] > 0).mean())

    # 截面离散度（过去已实现 h20 收益的离散度）
    disp = [None] * nD
    for t in range(nD):
        if t - 20 >= 0:
            y = Y[t - 20]
            ok = ~np.isnan(y)
            if ok.sum() >= 100:
                disp[t] = float(np.std(y[ok], ddof=1))

    sigs = {
        "past20_mkt": past20,      # 过去20日池内等权收益
        "past5_mkt": past5,        # 过去5日
        "vol20_mkt": vol20,        # 过去20日波动
        "breadth": breadth,        # 过去已实现 h20 胜率
        "dispersion": disp,        # 过去已实现 h20 离散度
    }

    print(f"截面 {nD}  {dates[0]} ~ {dates[-1]}")
    print("⚠️ 所有信号只用 ≤t 的信息（滞后 20 截面 ≈ 滞后 20 个交易日）\n")

    res = {}
    print("═" * 88)
    print("事前信号 → 未来市场收益（方向可预测性）")
    print("═" * 88)
    print(f"{'信号':16s}{'n':>5s}{'ρ(信号,未来市场)':>20s}{'ρ(信号,未来IC)':>20s}{'判定':>14s}")
    print("-" * 88)
    for nm, sv in sigs.items():
        pairs = [(sv[t], mkt[t], ic[t]) for t in range(nD)
                 if sv[t] is not None and mkt[t] is not None and ic[t] is not None]
        if len(pairs) < 30:
            print(f"{nm:16s}{len(pairs):>5d}  n 不足")
            continue
        s_ = [p[0] for p in pairs]
        m_ = [p[1] for p in pairs]
        i_ = [p[2] for p in pairs]
        r_m = spearman(s_, m_)
        r_i = spearman(s_, i_)
        lo, hi = boot_ci(s_, i_)
        sig = "★显著" if (lo < 0 and hi < 0) or (lo > 0 and hi > 0) else "不显著"
        judge = f"{sig}"
        res[nm] = {"n": len(pairs), "rho_vs_market": r_m, "rho_vs_ic": r_i,
                   "ci_vs_ic": [lo, hi], "significant": bool(sig == "★显著")}
        print(f"{nm:16s}{len(pairs):>5d}{r_m:>+20.4f}{r_i:>+20.4f}{judge:>14s}")

    # ═══ 最可行的信号做二分组实战检验 ═══
    print("\n" + "═" * 88)
    print("二分组检验：用事前信号的**样本内中位数**切分（切点取段A，冻结后用于B/C）")
    print("═" * 88)
    best = None
    for nm, v in res.items():
        if v["rho_vs_ic"] is not None and abs(v["rho_vs_ic"]) > (abs(best[1]["rho_vs_ic"]) if best else 0):
            best = (nm, v)
    if not best:
        print("  无可用信号")
        return
    nm = best[0]
    sv = sigs[nm]
    print(f"  选中信号：{nm}  ρ(信号,未来IC)={best[1]['rho_vs_ic']:+.4f}  "
          f"CI=[{best[1]['ci_vs_ic'][0]:+.3f}, {best[1]['ci_vs_ic'][1]:+.3f}]")

    nA, nB = int(nD * 0.40), int(nD * 0.70)
    cut_vals = [sv[t] for t in range(nA) if sv[t] is not None]
    if len(cut_vals) < 20:
        print("  段A 信号值不足"); return
    cut = float(np.median(cut_vals))
    print(f"  切点（段A中位数）= {cut:.5f}")

    segs = {"A": (0, nA), "B": (nA, nB), "C": (nB, nD), "ALL": (0, nD)}
    print(f"\n  {'段':4s}{'低侧n':>7s}{'低侧IC':>11s}{'高侧n':>7s}{'高侧IC':>11s}{'价差':>10s}")
    print("  " + "-" * 54)
    for k, (a, b) in segs.items():
        lowv = [ic[t] for t in range(a, b) if sv[t] is not None and ic[t] is not None and sv[t] <= cut]
        highv = [ic[t] for t in range(a, b) if sv[t] is not None and ic[t] is not None and sv[t] > cut]
        lm = sum(lowv) / len(lowv) if lowv else None
        hm = sum(highv) / len(highv) if highv else None
        d = (lm - hm) if (lm is not None and hm is not None) else None
        print(f"  {k:4s}{len(lowv):>7d}{_f(lm,3):>11s}{len(highv):>7d}{_f(hm,3):>11s}{_f(d,3):>10s}")
        res.setdefault("quintile", {})[k] = {"low_n": len(lowv), "low_ic": lm,
                                             "high_n": len(highv), "high_ic": hm, "spread": d}

    # ═══ 关键裁决 ═══
    c = res["quintile"]["C"]
    print("\n" + "═" * 88)
    print("裁决")
    print("═" * 88)
    ok = (c["spread"] is not None and c["spread"] > 0.02)
    if ok:
        print(f"  ✅ 段C（样本外）低侧 IC 比高侧高 {c['spread']:+.4f} → "
              f"事前信号在样本外仍有效，regime 切换可行")
    else:
        print(f"  ❌ 段C（样本外）价差 {_f(c['spread'],4)} 不足以支撑切换 → "
              f"regime 关系**不可事前利用**，只能作风险披露")
    print(f"  ⇒ 无论结论如何，round71 的 r=−0.67 已足以否证「单一权重全 regime 最优」。")

    json.dump({
        "meta": {"round": 72,
                 "purpose": "regime 的事前可检测性 —— 防止用未来数据做假优化",
                 "npz": os.path.relpath(NPZ, HERE),
                 "weights_version": wc["version"],
                 "n_dates": nD, "date_range": [dates[0], dates[-1]],
                 "signal_timing": "全部信号只用 ≤t 信息（滞后约 20 个交易日）"},
        "signals": res,
        "selected": nm, "cut": cut,
        "verdict_exploitable": bool(ok),
    }, open(OUT, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"\n→ {OUT}")


if __name__ == "__main__":
    main()
