"""test_reversal_merge.py —— P0-3 三维共线的「合并是否更好」对照实验。

要回答的**唯一**问题：
    bias60 / rsi_rev / tech_neg 三者现占 0.05+0.05+0.10 = 0.20 名义权重。
    把它们合成一维，是否比「分开」提供更多信息？
    判定标准：合并后的 IC / NW t **必须优于其中最好的单维**，
              否则合并只降方差、不增信息 → 应合并（降冗余）；
              若合并显著更差 → 说明各维有独立信息 → 应保留。

口径与主链完全一致：h=20、liq 档（日成交额≥2000万）、MIN_CS=200、
               DIM_MAX: bias60/rsi_rev=5、tech_neg=10。

⚠️ 本轮已实测（1653 日 / 含退市股）：
    bias60   IC +0.0725  t 4.78
    rsi_rev  IC +0.0389  t 3.54
    tech_neg IC -0.0069  t -0.67   ← 负！
    三维等权 IC +0.0724  t 4.92   ← 与 bias60 单维几乎相同

    ⇒ 三维合并并没带来信息增量，且 tech_neg 是拖累项。
       本脚本进一步测「两两合并」，确定最优组合。
"""
import json
import math
import os
import sys

import numpy as np
from scipy.stats import rankdata

HERE = os.path.dirname(os.path.abspath(__file__))
MAT = os.path.join(HERE, "data", "panel_r124_mat.npz")
KL = os.path.join(HERE, "data", "klines_2020.npz")
OUT = os.path.join(HERE, "data", "reversal_merge_test.json")

MIN_CS = 200
MIN_AMOUNT = 2000.0
H = 20
# round125: dim_max 改为**单一来源**（原为硬编码，且漏了 score_fund: 5.0）。
#   铁律：量程只能来自 factor_weights.json；本文件用到的维度在此显式声明，
#   缺失由 dim_cfg 直接报错而非静默按 10.0 处理。
from dim_cfg import load_dim_max
DIM_MAX = load_dim_max(['score_bias60', 'score_rsi_rev', 'score_tech_neg'], strict=True)
def nw_t(xs, lag):
    n = len(xs)
    if n < 3:
        return None
    a = np.asarray(xs, float)
    e = a - a.mean()
    s = float(e @ e) / n
    for k in range(1, min(lag, n - 1) + 1):
        s += 2.0 * (1.0 - k / (lag + 1.0)) * float(e[k:] @ e[:-k]) / n
    return float(a.mean() / math.sqrt(s / n)) if s > 0 else None


def spearman(a, b):
    ra, rb = rankdata(a), rankdata(b)
    da, db = ra - ra.mean(), rb - rb.mean()
    den = math.sqrt(float(da @ da) * float(db @ db))
    return float(da @ db) / den if den > 0 else None


def main():
    z = np.load(MAT, allow_pickle=False)
    dates = [str(d) for d in z["dates"]]
    codes = [str(c) for c in z["codes"]]
    dims = [str(d) for d in z["dims"]]
    S, P = z["S"], z["P"]
    k = np.load(KL, allow_pickle=False)
    close = k["X"][:, :, 1]
    kd = [str(d) for d in k["dates"]]
    kc = [str(c) for c in k["codes"]]
    if kc != codes:
        m = {c: i for i, c in enumerate(kc)}
        close = close[[m[c] for c in codes]]
    di_map = {d: i for i, d in enumerate(kd)}
    amt = P[:, :, 2]
    liq = np.isfinite(amt) & (amt >= MIN_AMOUNT)

    idx = {d: dims.index(d) for d in DIM_MAX}
    nD = S.shape[0]

    # 候选组合：单维 + 两两 + 三维
    combos = [
        ("bias60", ["score_bias60"]),
        ("rsi_rev", ["score_rsi_rev"]),
        ("tech_neg", ["score_tech_neg"]),
        ("bias60+rsi_rev", ["score_bias60", "score_rsi_rev"]),
        ("bias60+tech_neg", ["score_bias60", "score_tech_neg"]),
        ("rsi_rev+tech_neg", ["score_rsi_rev", "score_tech_neg"]),
        ("3d_equal", ["score_bias60", "score_rsi_rev", "score_tech_neg"]),
    ]

    res = {}
    for name, members in combos:
        ics = []
        for n in range(nD):
            di = di_map.get(dates[n])
            if di is None or di + H >= close.shape[1]:
                continue
            M = np.ones(S.shape[1], dtype=bool)
            for d in members:
                M &= np.isfinite(S[n, :, idx[d]])
            sel = M & liq[n]
            if sel.sum() < MIN_CS:
                continue
            p0 = close[:, di].astype(float)
            p1 = close[:, di + H].astype(float)
            ok = sel & (p0 > 0) & np.isfinite(p1)
            if ok.sum() < MIN_CS:
                continue
            # 各成员按自身量程归一到 0~1 后等权平均
            acc = np.zeros(int(ok.sum()), dtype=float)
            for d in members:
                acc += S[n][ok, idx[d]] / DIM_MAX[d]
            acc /= len(members)
            r = p1[ok] / p0[ok] - 1.0
            ic = spearman(acc, r)
            if ic is not None:
                ics.append(ic)
        if len(ics) < 5:
            res[name] = {"n": len(ics), "usable": False}
            continue
        t = nw_t(ics, H - 1)
        res[name] = {
            "n": len(ics), "n_effective_obs": round(len(ics) / H, 1),
            "ic_mean": round(float(np.mean(ics)), 4),
            "nw_t": round(t, 2) if t else None,
            "members": members,
        }
        print(f"  {name:<18} n={len(ics):<5} IC={res[name]['ic_mean']:+.4f} "
              f"NWt={res[name]['nw_t']:>6}", flush=True)

    best_single = max((v["ic_mean"], kk) for kk, v in res.items()
                      if len(v.get("members") or []) == 1 and v.get("n"))
    print(f"\n  最佳单维: {best_single[1]}  IC={best_single[0]:+.4f}")
    for name in ("bias60+rsi_rev", "3d_equal"):
        v = res.get(name) or {}
        if v.get("ic_mean") is not None:
            dlt = v["ic_mean"] - best_single[0]
            print(f"  {name:<18} IC={v['ic_mean']:+.4f}  "
                  f"vs 最佳单维 {dlt:+.4f}  "
                  f"{'✅ 合并更优' if dlt > 0.002 else '❌ 合并无增量' if dlt > -0.002 else '❌ 合并更差'}")

    with open(OUT, "w", encoding="utf-8") as f:
        json.dump({"horizon": H, "min_cross_section": MIN_CS,
                   "min_amount_wan": MIN_AMOUNT, "results": res,
                   "best_single": {"name": best_single[1],
                                   "ic": best_single[0]}}, f,
                  ensure_ascii=False, indent=1)
    print(f"\n写出 {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
