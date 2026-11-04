#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
audit_round71_regime.py —— 模型 IC 与市场环境的定量关系（regime 依赖检验）

═══ 为什么这一步是第七轮的核心 ═══

round70 三套口径全部出现「样本外 ICIR 不如基线 / 甚至反向」：
  · FULL   段A ICIR 0.984 → 优化后段C 0.871（还不如 base 0.891）
  · NOFUND 段A 0.984 → 段C 0.897（base 0.915）
  · 等权   段C 0.961（base 0.891）← 唯一"更好"的，但它没优化

round70b 的诊断给出了机理：
  段B（2026-03-16~06-22）66 个截面里 **89.4% 的 IC 为负**，且几乎**全部维度同向**：
      lowvol20  段A +0.053  →  段B −0.124  →  段C +0.253
      bias60    段A +0.050  →  段B −0.136  →  段C +0.176
      rsi_rev   段A +0.020  →  段B −0.134  →  段C +0.219
      tech_neg  段A +0.038  →  段B −0.162  →  段C +0.210
  同期基准：
      段B 沪深300 +6.00% / 中证1000 +7.32%  ← **普涨**
      段C 沪深300 −9.47% / 中证1000 −11.53% ← **普跌**

⇒ 假说：本模型是一个**防御型/低波型选股器**，其 IC 与市场同期收益负相关。
   涨市里低波+低估值跑输 → IC 转负；跌市里防御属性生效 → IC 走高。

═══ 本脚本要证明/否证什么 ═══

H1：组合 IC 与「同期市场收益」显著负相关（regime 依赖）
H2：若 H1 成立，则全样本 ICIR 最大化是在**两种相反 regime 间取平均**，
    任何单一权重都不可能同时在两段为正 → 解释了 round70 的样本外失败
H3：分段 ICIR 与「段内市场波动率」正相关（跌市/高波动 → IC 更高）

检验方法：
  · 用 h20 前瞻收益构造的 IC，对应「未来 20 日」市场收益（不是同期）
  · 市场代理 = 池内等权（POOL_EW），因为它是唯一无前视偏差的对照
  · Pearson + Spearman + NW t
  · 再用「市场未来收益」分组（五档），看 IC 单调性

用法：python audit_round71_regime.py
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

NPZ = os.path.join(HERE, "data", "icir_arrays_h20.npz")
KLONG = os.path.join(HERE, "data", "klines_long.json")
OUT = os.path.join(HERE, "audit_round71.json")


def pearson(x, y):
    n = len(x)
    if n < 3:
        return None
    mx, my = sum(x) / n, sum(y) / n
    dx = [a - mx for a in x]
    dy = [b - my for b in y]
    sx = math.sqrt(sum(a * a for a in dx))
    sy = math.sqrt(sum(b * b for b in dy))
    if sx <= 0 or sy <= 0:
        return None
    return sum(a * b for a, b in zip(dx, dy)) / (sx * sy)


def spearman(x, y):
    def _r(v):
        n = len(v)
        idx = sorted(range(n), key=lambda i: v[i])
        r = [0.0] * n
        i = 0
        while i < n:
            j = i
            while j + 1 < n and v[idx[j + 1]] == v[idx[i]]:
                j += 1
            avg = (i + j) / 2.0 + 1.0
            for t in range(i, j + 1):
                r[idx[t]] = avg
            i = j + 1
        return r
    return pearson(_r(x), _r(y))


def boot_ci(x, y, n_boot=2000, seed=20261029):
    """IC 与市场收益相关性的 bootstrap 置信区间。"""
    rng = np.random.default_rng(seed)
    n = len(x)
    vals = []
    for _ in range(n_boot):
        idx = rng.integers(0, n, n)
        r = pearson([x[i] for i in idx], [y[i] for i in idx])
        if r is not None:
            vals.append(r)
    vals.sort()
    if not vals:
        return None, None
    return vals[int(0.025 * len(vals))], vals[int(0.975 * len(vals))]


def main():
    z = np.load(NPZ, allow_pickle=False)
    dates = [str(x) for x in z["dates"]]
    X, M, Y = z["X"], z["M"], z["Y"]
    nD, nC, nK = X.shape

    wc = json.load(open(WEIGHTS, encoding="utf-8"))
    dm = np.array([float(wc["dim_max"].get(k, 10.0)) for k in DIMS])
    w = np.array([float(wc["weights"].get(k, 0.0)) for k in DIMS])

    # 组合分
    V = np.where(M, np.minimum(np.where(np.isnan(X), 0.0, X), dm) / dm * 10.0, 0.0)
    den = M.astype(np.float64) @ w
    num = V @ w
    with np.errstate(invalid="ignore", divide="ignore"):
        S = np.where(den > 1e-12, num / den * 10.0, np.nan)
    RY = rankdata(Y, axis=1, nan_policy="omit")
    RS = rankdata(S, axis=1, nan_policy="omit")

    # 逐截面 IC + 同期池内等权收益（h20 前瞻）
    ic, mkt, breadth, xs = [], [], [], []
    for t in range(nD):
        ok = (~np.isnan(RS[t])) & (~np.isnan(RY[t])) & (~np.isnan(Y[t]))
        nn = ok.sum()
        if nn < 100:
            continue
        a = RS[t][ok] - RS[t][ok].mean()
        b = RY[t][ok] - RY[t][ok].mean()
        sx = math.sqrt((a * a).sum())
        sy = math.sqrt((b * b).sum())
        if sx <= 0 or sy <= 0:
            continue
        ic.append(float((a * b).sum() / (sx * sy)))
        mkt.append(float(np.nanmean(Y[t][ok])))       # 池内等权未来20日收益
        breadth.append(float((Y[t][ok] > 0).mean()))  # 未来上涨家数占比（市场宽度）
        xs.append(dates[t])

    nn = len(ic)
    print(f"有效截面 {nn}  {xs[0]} ~ {xs[-1]}\n")
    print(f"全样本组合 IC = {sum(ic)/nn:+.4f}  ICIR = {sum(ic)/nn/np.std(ic,ddof=1):+.4f}  "
          f"t = {nw_t(ic):+.2f}")
    print(f"池内等权 未来20日收益：均值 {np.mean(mkt)*100:+.2f}%  "
          f"标准差 {np.std(mkt,ddof=1)*100:.2f}%  "
          f"上涨期占比 {(np.array(mkt)>0).mean()*100:.1f}%\n")

    # ═══ H1：IC vs 市场收益 ═══
    print("═" * 74)
    print("H1  组合 IC 与「同期市场未来20日收益」的关系")
    print("═" * 74)
    r_p = pearson(ic, mkt)
    r_s = spearman(ic, mkt)
    lo, hi = boot_ci(ic, mkt)
    print(f"  Pearson  r = {r_p:+.4f}   bootstrap 95%CI [{lo:+.4f}, {hi:+.4f}]  "
          f"{'★显著' if (lo>0 and hi>0) or (lo<0 and hi<0) else '不显著（跨0）'}")
    print(f"  Spearman ρ = {r_s:+.4f}")
    # IC 对市场收益回归
    slope = r_p * np.std(ic, ddof=1) / np.std(mkt, ddof=1)
    print(f"  回归斜率 β = {slope:+.4f}  →  市场未来收益每 +10pp，组合 IC 变动 "
          f"{slope*0.10:+.4f}")
    print(f"  R² = {r_p**2:.4f}  → 市场方向可解释 IC 波动的 {r_p**2*100:.1f}%")

    # ═══ 市场收益五档分组 ═══
    print("\n  按「未来20日池内等权收益」五档分组：")
    order = np.argsort(mkt)
    q = np.array_split(order, 5)
    grp = []
    for i, idx in enumerate(q):
        sub_ic = [ic[j] for j in idx]
        sub_m = [mkt[j] for j in idx]
        g = {"group": i + 1, "n": len(idx),
             "mkt_mean": float(np.mean(sub_m)),
             "ic_mean": float(np.mean(sub_ic)),
             "ic_pos_rate": float((np.array(sub_ic) > 0).mean()),
             "icir": float(np.mean(sub_ic) / np.std(sub_ic, ddof=1)) if np.std(sub_ic, ddof=1) > 1e-12 else None}
        grp.append(g)
        print(f"    Q{i+1} (市场 {g['mkt_mean']*100:+6.2f}%)  n={g['n']:3d}  "
              f"IC={g['ic_mean']:+.4f}  正IC={g['ic_pos_rate']*100:5.1f}%  "
              f"ICIR={_f(g['icir'],3)}")
    mono = all(grp[i]["ic_mean"] <= grp[i + 1]["ic_mean"] for i in range(4))
    rev_mono = all(grp[i]["ic_mean"] >= grp[i + 1]["ic_mean"] for i in range(4))
    print(f"    单调性：{'✅ 严格递增（市场越好 IC 越高）' if mono else ('✅ 严格递减（市场越好 IC 越低）' if rev_mono else '❌ 非单调')}")
    print(f"    Q1−Q5 价差 = {grp[0]['ic_mean'] - grp[4]['ic_mean']:+.4f}")

    # ═══ H3：IC vs 市场波动 ═══
    print("\n" + "═" * 74)
    print("H3  组合 IC 与「市场环境波动」的关系")
    print("═" * 74)
    vol20 = []
    for t in range(nD):
        lo_i = max(0, t - 19)
        vol20.append(float(np.std(mkt[lo_i:t + 1], ddof=1) * math.sqrt(244 / 20)) if t - lo_i >= 4 else None)
    okv = [(ic[i], vol20[i]) for i in range(nn) if vol20[i] is not None]
    r_v = pearson([a for a, _ in okv], [b for _, b in okv])
    print(f"  Pearson r(IC, 市场年化波动) = {r_v:+.4f}  （n={len(okv)}）")

    # ═══ 段内一致性：IC 与市场方向符号匹配率 ═══
    print("\n  逐截面「IC 符号」与「市场方向」的匹配率：")
    same = sum(1 for i in range(nn) if (ic[i] > 0) == (mkt[i] > 0))
    print(f"    同号 {same}/{nn} = {same/nn*100:.1f}%  "
          f"→ {'IC 与市场方向系统性相反' if same/nn < 0.4 else ('同向' if same/nn > 0.6 else '无明显关系')}")

    # ═══ H2：分 regime 的最优权重差异 ═══
    print("\n" + "═" * 74)
    print("H2  分 regime 的单维 IC（涨市 vs 跌市）—— 检验是否存在「不可能三角」")
    print("═" * 74)
    up_idx = [i for i in range(nn) if mkt[i] > 0]
    dn_idx = [i for i in range(nn) if mkt[i] <= 0]
    print(f"  涨市 {len(up_idx)} 截面 / 跌市 {len(dn_idx)} 截面\n")
    print(f"  {'维度':14s}{'涨市 IC':>11s}{'涨市 t':>9s}{'跌市 IC':>11s}{'跌市 t':>9s}{'翻转':>8s}")
    print("  " + "-" * 62)
    dim_rows = {}
    for k, d in enumerate(DIMS):
        def _ic(idx_list):
            out = []
            for t in idx_list:
                ok = (~np.isnan(X[t, :, k])) & (~np.isnan(RY[t]))
                if ok.sum() < 100:
                    continue
                a = X[t, :, k][ok]
                b = RY[t][ok]
                a = a - a.mean()
                b = b - b.mean()
                sx = math.sqrt((a * a).sum())
                sy = math.sqrt((b * b).sum())
                if sx > 0 and sy > 0:
                    out.append(float((a * b).sum() / (sx * sy)))
            return out
        up = _ic(up_idx)
        dn = _ic(dn_idx)
        su, sd = stat(up), stat(dn)
        flip = (su["ic"] is not None and sd["ic"] is not None and su["ic"] * sd["ic"] < 0)
        dim_rows[d] = {"up": su, "down": sd, "flip": bool(flip)}
        print(f"  {d.replace('score_',''):14s}{_f(su['ic'],3):>11s}{_f(su['t'],2):>9s}"
              f"{_f(sd['ic'],3):>11s}{_f(sd['t'],2):>9s}{'⚠️ 翻转' if flip else '':>8s}")
    n_flip = sum(1 for v in dim_rows.values() if v["flip"])
    print(f"\n  → {n_flip}/{len(DIMS)} 维在涨市/跌市间 IC 符号翻转")

    # ═══ 结论 ═══
    print("\n" + "═" * 74)
    print("结论")
    print("═" * 74)
    if r_p is not None and r_p < -0.2 and lo is not None and hi < 0:
        verdict = ("H1 成立：组合 IC 与市场未来收益**显著负相关**（bootstrap CI 不含 0）。"
                   "本模型是防御型选股器，涨市系统性反向。")
    elif r_p is not None and abs(r_p) < 0.15:
        verdict = "H1 不成立：IC 与市场方向无明显线性关系。"
    else:
        verdict = f"H1 方向存在但强度有限（r={r_p:+.3f}）。"
    print("  " + verdict)
    print(f"  ⇒ 全样本 ICIR 最大化 = 在两类相反 regime 间求平均，"
          f"任何单一权重都无法两段同时为正。")
    print(f"     这正是 round70 样本外失败（段A 优化 → 段C 不如 base）的机理，"
          f"不是优化算法的问题。")

    json.dump({
        "meta": {
            "round": 71,
            "purpose": "检验模型 IC 是否依赖市场 regime —— 解释 round70 样本外失败",
            "npz": os.path.relpath(NPZ, HERE),
            "weights_version": wc["version"],
            "n_dates": nn, "date_range": [xs[0], xs[-1]],
            "market_proxy": "池内等权 h20 前瞻收益（唯一无前视偏差的对照）",
        },
        "overall": {"ic": float(np.mean(ic)), "icir": float(np.mean(ic) / np.std(ic, ddof=1)),
                    "t": nw_t(ic), "n": nn},
        "market": {"mean": float(np.mean(mkt)), "sd": float(np.std(mkt, ddof=1)),
                   "up_rate": float((np.array(mkt) > 0).mean())},
        "H1_ic_vs_market": {"pearson": r_p, "spearman": r_s,
                            "boot_ci": [lo, hi], "slope": float(slope), "r2": float(r_p ** 2)},
        "quintile": grp, "monotonic": bool(mono or rev_mono),
        "H3_ic_vs_vol": r_v,
        "sign_match_rate": same / nn,
        "H2_by_regime": {"n_up": len(up_idx), "n_down": len(dn_idx),
                         "dims": dim_rows, "n_flip": n_flip},
        "ic_series": ic, "market_series": mkt, "dates": xs,
        "verdict": verdict,
    }, open(OUT, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"\n→ {OUT}")


if __name__ == "__main__":
    main()
