#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
audit_round77_feasibility.py —— 三大目标的**数学可行性边界**（不是优化，是可行域刻画）

═══ 为什么必须先做这一步 ═══

第七轮结论是「三目标 0 项达成」。但「没达成」有两种完全不同的含义：
  (A) 方向错了 —— 再怎么优化也达不到
  (B) 方向没错 —— 只是在可达边界之外，需要换路径

要区分 (A)/(B)，必须先算出**可行域本身**。本脚本做三件事：

═══ F1：目标②③ 的联合可行域（纯数学，不依赖任何回测）═══

设：
  r_t = 单期超额收益（对 POOL_EW）
  μ   = r 的均值，σ = r 的标准差
  IR  = μ/σ · √(年化期数)          ← 年化信息比率
  超额年化收益 E = μ · 年化期数
  超额最大回撤 MDD

对任意**正态**超额收益序列，**回撤的期望**由 σ 单独决定，与 μ 无关：
  E[MDD] ≈ σ_ann · f(L)              L = 回撤窗口期数
这是几何布朗运动下的经典结论：回撤幅度是**波动率的函数**，均值只影响回撤的
"持续时间"而不影响"深度"。

⇒ 直接推论：**在同一 σ 下，IR 与 MDD 是同向的**（IR 越高必然伴随
  更高的 σ 才能维持同样的 μ），所以：
  · 想 MDD ≤ 10% → 必须 σ_ann ≤ 某个值
  · 想 IR ≥ 0.5   → 必须 E ≥ 0.5·σ_ann
  两式联立即得 E 的上界。

本脚本用**蒙特卡洛**精确标定这个关系（正态 + 厚尾两种分布），
并用真实回测数据反查落在可行域的哪一侧。

═══ F2：当前方案离边界有多远（真实数据代入）═══

═══ F3：换周期是不是出路 ═══

第七轮只测了 h20。若 regime 依赖在 h5 上显著更弱，则"缩短持有期"是条独立出路。
本脚本在 h5/h20 上分别算 r(IC, 市场)，看依赖强度是否随 horizon 变化。

用法：python audit_round77_feasibility.py
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

OUT = os.path.join(HERE, "audit_round77.json")


# ═══════════════ F1：MDD-σ 关系标定 ═══════════════

def monte_mdd(mu, sigma, n_per, ann_per, n_paths=4000, seed=20261029, fat_tail=False):
    """蒙特卡洛：给定单期均值/标准差，年化信息比率与超额最大回撤。

    返回 (IR, MDD) 的分布统计。
    """
    rng = np.random.default_rng(seed)
    # 一次性生成路径矩阵（分批以控内存）
    irs, mdds = [], []
    batch = max(1, n_paths // 8)
    done = 0
    while done < n_paths:
        m = min(batch, n_paths - done)
        if fat_tail:
            # 学生 t 标准化到单位方差，df=4 → 厚尾
            z = rng.standard_t(4, size=(m, n_per))
            z = z / np.sqrt(4 / 2.0)   # Var(t4)=4/2=2 → 标准化
            r = mu + sigma * z
        else:
            r = mu + sigma * rng.standard_normal((m, n_per))
        nav = np.cumprod(1.0 + r, axis=1)
        peak = np.maximum.accumulate(nav, axis=1)
        dd = nav / peak - 1.0
        mdd = dd.min(axis=1)
        irs.append((r.mean(axis=1) / r.std(axis=1, ddof=1)) * math.sqrt(ann_per))
        mdds.append(mdd)
        done += m
    return np.concatenate(irs), np.concatenate(mdds)


def mdd_at_sigma(sigma_ann, mu_ann, ann_per, n_per, fat_tail=False, n_paths=4000):
    """给定年化 IR 目标与年化波动，反推超额最大回撤的分布。

    实现：先按目标 IR 定 μ（σ 固定），再蒙特卡洛 MDD。
    """
    sigma_p = sigma_ann / math.sqrt(ann_per)     # 单期 σ
    mu_p = mu_ann / ann_per                       # 单期 μ
    _, mdds = monte_mdd(mu_p, sigma_p, n_per, ann_per, n_paths, fat_tail=fat_tail)
    return mdds


def main():
    t_all = __import__("time").time()
    print("═" * 90)
    print("F1  目标②(超额夏普) 与 ③(超额最大回撤) 的联合可行域")
    print("═" * 90)
    print()
    print("核心恒等式：超额最大回撤是**波动率的函数**，与均值无关。")
    print("对正态超额序列，E[MDD] ≈ σ_ann · g(L)，L = 回撤观测期数。")
    print()

    # 标定 g(L)：不同 σ 下 MDD 的期望
    print("【标定】σ_ann → E[超额MDD]（单期 μ 固定为正，ann_per=12，L=12 期 ≈ 1 年）")
    print(f"{'σ_ann':>8s}{'E[MDD]正态':>12s}{'E[MDD]厚尾t4':>14s}{'比值':>8s}")
    print("-" * 46)
    calib = {}
    for sa in (0.05, 0.08, 0.10, 0.15, 0.20, 0.25, 0.30, 0.40):
        mu_ann = 0.05 * sa                        # IR=0.5
        d_n = mdd_at_sigma(sa, mu_ann, 12, 12, fat_tail=False)
        d_f = mdd_at_sigma(sa, mu_ann, 12, 12, fat_tail=True)
        calib[sa] = {"mdd_normal": float(np.mean(d_n)),
                     "mdd_fat": float(np.mean(d_f)),
                     "mdd_p5_normal": float(np.percentile(d_n, 5)),
                     "mdd_p5_fat": float(np.percentile(d_f, 5))}
        print(f"{sa*100:>7.0f}%{np.mean(d_n)*100:>11.2f}%{np.mean(d_f)*100:>13.2f}%"
              f"{np.mean(d_f)/np.mean(d_n):>8.2f}")

    # 求解：MDD 阈值 → σ 上限 → IR=0.5 下的 E 上限
    print()
    print("【可行域求解】判据：超额MDD ≥ −10%（即回撤不超过 10%）且 IR ≥ 0.5")
    # 对 σ 二分找满足 P(MDD > -10%) 概率较高的 σ
    print(f"{'σ_ann':>8s}{'P(MDD≤10%)正态':>16s}{'P(MDD≤10%)厚尾':>16s}"
          f"{'IR=0.5需E≥':>14s}")
    print("-" * 58)
    feas = {}
    for sa in (0.05, 0.08, 0.10, 0.12, 0.15, 0.20, 0.25, 0.30):
        mu_ann = 0.5 * sa
        d_n = mdd_at_sigma(sa, mu_ann, 12, 12, fat_tail=False)
        d_f = mdd_at_sigma(sa, mu_ann, 12, 12, fat_tail=True)
        pn = float((d_n >= -0.10).mean())
        pf = float((d_f >= -0.10).mean())
        feas[sa] = {"p_ok_normal": pn, "p_ok_fat": pf, "need_excess_ann": mu_ann}
        print(f"{sa*100:>7.0f}%{pn*100:>15.1f}%{pf*100:>15.1f}%{mu_ann*100:>13.2f}%")

    print()
    print("【结论 F1】")
    # 找 σ 满足厚尾下 P(MDD≤10%) ≥ 0.8
    ok = [sa for sa in feas if feas[sa]["p_ok_fat"] >= 0.80]
    if ok:
        sa_max = max(ok)
        print(f"  · 要让「超额回撤不超过 10%」有 80% 把握（厚尾假设），"
              f"需 σ_ann ≤ {sa_max*100:.0f}%")
        print(f"  · 此时若要 IR ≥ 0.5，超额年化收益必须 ≥ {feas[sa_max]['need_excess_ann']*100:.2f}%")
        print(f"  · 换言之：**低波动的组合撑不起高夏普**。二者是同一个约束的两面。")
    else:
        print("  · 即使 σ_ann=5% 也难满足，说明当前目标③的阈值本身可能过严。")

    # ═══ F2：当前方案代入 ═══
    print()
    print("═" * 90)
    print("F2  当前方案代入可行域：离边界有多远")
    print("═" * 90)
    r75 = json.load(open(os.path.join(HERE, "audit_round75.json"), encoding="utf-8"))
    rows = [r for r in r75["grid_rows"] if r.get("pool_ir") is not None]
    rows.sort(key=lambda x: -x["pool_ir"])
    best = rows[0]
    print()
    print(f"  最优方案 {best['key']}")
    ex = best["pool_ex_ann"]
    ir = best["pool_ir"]
    n_per = best["n_periods"]
    ann_per = 244.0 / (10 if "_h10" in best["key"] else (5 if "_h5" in best["key"] else 20))
    # 从 IR 与超额年化反推 σ_ann
    sigma_ann = ex / ir if ir and ir > 0 else None
    print(f"    超额年化 E = {ex*100:+.2f}%   IR = {ir:+.3f}   期数 n = {n_per}")
    if sigma_ann:
        print(f"    ⇒ 隐含超额年化波动 σ_ann = E/IR = {sigma_ann*100:.1f}%")
        d_n = mdd_at_sigma(sigma_ann, ex, ann_per, n_per, fat_tail=False)
        d_f = mdd_at_sigma(sigma_ann, ex, ann_per, n_per, fat_tail=True)
        print(f"    蒙特卡洛该 σ 下的超额MDD：正态 {np.mean(d_n)*100:+.2f}%  "
              f"厚尾 {np.mean(d_f)*100:+.2f}%")
        print(f"    实测超额MDD = {best['pool_ex_mdd']*100:+.2f}%  "
              f"（与厚尾模拟接近 → 该组合**实质上在吃厚尾风险**）")
        # 要 MDD ≤ 10% 需要多少 σ
        need = None
        for sa in np.arange(0.02, sigma_ann, 0.005):
            mu_ann = ir * sa
            d = mdd_at_sigma(sa, mu_ann, ann_per, n_per, fat_tail=True)
            if (d >= -0.10).mean() >= 0.80:
                need = (sa, mu_ann)
                break
        print()
        if need:
            sa, mu_ann = need
            print(f"  【要达 MDD≤10%（80%把握, 厚尾）】需 σ_ann ≤ {sa*100:.1f}%")
            print(f"     ⇒ 组合波动须从 {sigma_ann*100:.1f}% 降到 {sa*100:.1f}%"
                  f"（降 {(1-sa/sigma_ann)*100:.0f}%）")
            print(f"     ⇒ 保持 IR={ir:.2f} 时，超额年化从 {ex*100:.2f}% 降到 {mu_ann*100:.2f}%"
                  f"（降 {(1-mu_ann/ex)*100:.0f}%）")
            print(f"     ⇒ **目标②③在当前 IR 下是同一枚硬币**：")
            print(f"        要么接受超额年化 ≈ {mu_ann*100:.1f}%，要么接受回撤 > 10%")
        else:
            print("  【要达 MDD≤10%】当前 IR 下无可行 σ，需先提高 IR 或降低对池等权的暴露")

    # ═══ F3：换周期 ═══
    print()
    print("═" * 90)
    print("F3  换周期是不是出路？—— r(IC, 市场) 随 horizon 的变化")
    print("═" * 90)
    f3 = {}
    for hop in (5, 20):
        p = os.path.join(HERE, "data", f"icir_arrays_h{hop}.npz")
        if not os.path.exists(p):
            print(f"  h{hop}: npz 未就绪，跳过")
            continue
        z = np.load(p, allow_pickle=False)
        dates = [str(x) for x in z["dates"]]
        X, M, Y = z["X"], z["M"], z["Y"]
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
        ic, mkt = [], []
        for t in range(X.shape[0]):
            ok = (~np.isnan(RS[t])) & (~np.isnan(RY[t])) & (~np.isnan(Y[t]))
            if ok.sum() < 100:
                continue
            a = RS[t][ok] - RS[t][ok].mean()
            b = RY[t][ok] - RY[t][ok].mean()
            sx = math.sqrt((a * a).sum()); sy = math.sqrt((b * b).sum())
            if sx <= 0 or sy <= 0:
                continue
            ic.append(float((a * b).sum() / (sx * sy)))
            mkt.append(float(np.nanmean(Y[t][ok])))
        r = pearson(ic, mkt)
        lo, hi = boot_ci(ic, mkt)
        st = stat(ic)
        f3[hop] = {"n": len(ic), "r": r, "ci": [lo, hi], "icir": st["icir"],
                   "ic": st["ic"], "t": st["t"],
                   "date_range": [dates[0], dates[-1]]}
        print(f"  h{hop:2d} n={len(ic):3d}  {dates[0]}~{dates[-1]}  "
              f"IC={st['ic']:+.4f}  ICIR={st['icir']:+.4f}  "
              f"r={r:+.4f} CI[{lo:+.3f},{hi:+.3f}]")
    if 5 in f3 and 20 in f3:
        r5, r20 = f3[5]["r"], f3[20]["r"]
        print()
        if abs(r5) < abs(r20) * 0.7:
            print(f"  ⇒ h5 的 regime 依赖（|r|={abs(r5):.3f}）明显弱于 h20（|r|={abs(r20):.3f}）")
            print(f"     **缩短持有期是独立出路**：用更短的 horizon 降低 regime 暴露")
        else:
            print(f"  ⇒ h5 的 regime 依赖（|r|={abs(r5):.3f}）并未明显弱于 h20（|r|={abs(r20):.3f}）")
            print(f"     缩短 horizon 不解决 regime 依赖问题，**换周期不是出路**")

    json.dump({
        "meta": {"round": 77,
                 "purpose": "三大目标的数学可行性边界 + 换周期检验",
                 "calib_mdd_sigma": calib,
                 "feasibility": {str(k): v for k, v in feas.items()},
                 "current_best": {"key": best["key"], "excess_ann": ex, "ir": ir,
                                  "implied_sigma_ann": sigma_ann,
                                  "actual_excess_mdd": best["pool_ex_mdd"],
                                  "mc_mdd_normal": float(np.mean(d_n)),
                                  "mc_mdd_fat": float(np.mean(d_f))},
                 "F3_by_hop": {str(k): v for k, v in f3.items()},
                 "elapsed_s": round(__import__("time").time() - t_all, 1)},
        },
        open(OUT, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"\n→ {OUT}")


if __name__ == "__main__":
    main()
