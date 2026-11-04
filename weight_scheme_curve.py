"""weight_scheme_curve.py —— P0-4 决定性证据：TopN 超额随 N 的完整曲线。

起因（本轮最重要的一次自查）：
    weight_scheme_head.py 显示同一套分数在不同持仓规模下结论相反：
        IC 最高的两维（lowvol20 t=4.96 / bias60 t=4.78）在 Top10 上是负超额；
        IC 最低的 size_growth（t=2.27）在 Top10 上是 +1.29% 且 t=2.10。
    若这是真的，那「用 IC 调权重」这条路在生产口径（6~13 只）下就是错的。

    但只看 4 个点（10/20/30/50）不足以下结论，也可能是噪声。
    本脚本把 N 扫成连续曲线（5~300），并给出每个 N 的：
        mean 超额 / IR / NW t / 胜率 / 是否单调
    目的只有一个：回答「alpha 到底分布在头部还是中段」。

⚠️ 这里**不做任何权重选择**，只做诊断。
"""
import json
import math
import os
import time

import numpy as np
from scipy.stats import rankdata

HERE = os.path.dirname(os.path.abspath(__file__))
MAT = os.path.join(HERE, "data", "panel_r124_mat.npz")
KL = os.path.join(HERE, "data", "klines_2020.npz")
OUT = os.path.join(HERE, "data", "weight_scheme_curve.json")

# round125: dim_max 改为**单一来源**（原为硬编码，且漏了 score_fund: 5.0）。
#   铁律：量程只能来自 factor_weights.json；本文件用到的维度在此显式声明，
#   缺失由 dim_cfg 直接报错而非静默按 10.0 处理。
from dim_cfg import load_dim_max
DIM_MAX = load_dim_max(['score_bias60', 'score_rsi_rev', 'score_lowvol20', 'score_tech_neg', 'score_size_growth', 'score_ep', 'score_quality', 'score_growth'], strict=True)
MIN_CS = 200
MIN_AMOUNT = 2000.0
H = 20
# 5,10,15,20,...,100 然后 120..300
NS = list(range(5, 101, 5)) + [120, 140, 160, 180, 200, 250, 300]

W_CUR = {"score_fund": 0.20, "score_size_growth": 0.20, "score_tech_neg": 0.10,
         "score_quality": 0.10, "score_ep": 0.10, "score_dominant": 0.10,
         "score_growth": 0.10, "score_bias60": 0.05, "score_rsi_rev": 0.05}

# 只保留两个对照：现行组合分 + 纯 size_growth（用来定位「头部 alpha 来源」）
SCHEMES = {
    "S0_v417_composite": W_CUR,
    "ONLY_size_growth": {**{d: 0.0 for d in DIM_MAX}, "score_size_growth": 1.0},
    "ONLY_lowvol20": {**{d: 0.0 for d in DIM_MAX}, "score_lowvol20": 1.0},
}


def nw_t(xs, lag):
    n = len(xs)
    if n < 3:
        return None
    a = np.asarray(xs, dtype=np.float64)
    m = a.mean()
    e = a - m
    s = float(e @ e) / n
    for k in range(1, min(lag, n - 1) + 1):
        s += 2.0 * (1.0 - k / (lag + 1.0)) * (float(e[k:] @ e[:-k]) / n)
    return float(m / math.sqrt(s / n)) if s > 0 else None


def main():
    t0 = time.time()
    z = np.load(MAT, allow_pickle=False)
    pdates = [str(d) for d in z["dates"]]
    codes = [str(c) for c in z["codes"]]
    dims = [str(d) for d in z["dims"]]
    S, P = z["S"], z["P"]

    k = np.load(KL, allow_pickle=False)
    close = k["X"][:, :, 1].astype(np.float64)
    kdates = [str(d) for d in k["dates"]]
    kcodes = [str(c) for c in k["codes"]]
    if kcodes != codes:
        kmap = {c: i for i, c in enumerate(kcodes)}
        close = close[[kmap[c] for c in codes]]
    kdmap = {d: i for i, d in enumerate(kdates)}

    liq_ok = np.isfinite(P[:, :, 2]) & (P[:, :, 2] >= MIN_AMOUNT)
    mask = np.isfinite(S)
    Sf = np.where(mask, S, 0.0).astype(np.float64)

    wvec = {}
    for name, w in SCHEMES.items():
        v = np.zeros(len(dims), dtype=np.float64)
        for i, d in enumerate(dims):
            v[i] = w.get(d, 0.0) / DIM_MAX.get(d, 10.0)
        wvec[name] = v

    acc = {n: {t: [] for t in NS} for n in SCHEMES}
    nd = 0
    for n, d in enumerate(pdates):
        di = kdmap.get(d)
        if di is None or di + H >= close.shape[1]:
            continue
        p0, p1 = close[:, di], close[:, di + H]
        with np.errstate(invalid="ignore", divide="ignore"):
            ret = p1 / np.where(p0 > 0, p0, np.nan) - 1.0
        okret = np.isfinite(ret) & liq_ok[n]
        if okret.sum() < MIN_CS:
            continue
        nd += 1
        for name, v in wvec.items():
            num = Sf[n] @ v
            den = mask[n].astype(np.float64) @ v
            with np.errstate(invalid="ignore", divide="ignore"):
                comp = np.where(den > 1e-9, num / np.maximum(den, 1e-12), np.nan)
            sel = okret & np.isfinite(comp)
            if sel.sum() < MIN_CS:
                continue
            fv, rv = comp[sel], ret[sel]
            base = float(rv.mean())
            o = np.argsort(-fv)
            for t in NS:
                if len(o) < t:
                    continue
                acc[name][t].append(float(rv[o[:t]].mean()) - base)
        if n % 400 == 0:
            print(f"  {n}/{len(pdates)}", flush=True)

    res = {}
    for name in SCHEMES:
        res[name] = {}
        for t in NS:
            xs = acc[name][t]
            if not xs:
                continue
            a = np.asarray(xs, dtype=np.float64)
            mu, sd = float(a.mean()), float(a.std(ddof=1))
            res[name][f"N{t}"] = {
                "n_dates": len(a),
                "mean_pct": round(mu * 100, 3),
                "ir": round(mu / sd, 3) if sd > 0 else None,
                "nw_t": round(nw_t(xs, H - 1), 2) if len(xs) > 3 else None,
                "win_rate": round(float((a > 0).mean()), 3),
            }

    print(f"\n=== TopN 超额曲线（h=20, liq, {nd} 个截面, 未扣成本）===", flush=True)
    print(f"{'N':>5}" + "".join(f"{n[:18]:>20}" for n in SCHEMES), flush=True)
    for t in NS:
        row = f"{t:>5}"
        for name in SCHEMES:
            r = res[name].get(f"N{t}")
            row += f"{(str(r['mean_pct'])+'%'):>11}{('t='+str(r['nw_t'])):>9}" if r else f"{'-':>20}"
        print(row, flush=True)

    # 找每个方案的最优 N 与「正超额起点」
    summary = {}
    for name, r in res.items():
        best = max(r.items(), key=lambda kv: kv[1]["mean_pct"])
        first_pos = None
        for t in NS:
            if r.get(f"N{t}") and r[f"N{t}"]["mean_pct"] > 0:
                first_pos = t
                break
        summary[name] = {
            "best_N": int(best[0][1:]),
            "best_mean_pct": best[1]["mean_pct"],
            "first_positive_N": first_pos,
            "N10_pct": (r.get("N10") or {}).get("mean_pct"),
            "N50_pct": (r.get("N50") or {}).get("mean_pct"),
            "N100_pct": (r.get("N100") or {}).get("mean_pct"),
        }
    print("\n=== 摘要 ===", flush=True)
    for name, s in summary.items():
        print(f"  {name:<20} 最优N={s['best_N']:>4} 超额={s['best_mean_pct']:+.2f}%  "
              f"首个正超额N={s['first_positive_N']}  "
              f"N10={s['N10_pct']:+.2f}% N50={s['N50_pct']:+.2f}% N100={s['N100_pct']:+.2f}%",
              flush=True)

    with open(OUT, "w", encoding="utf-8") as f:
        json.dump({
            "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "horizon": H, "n_dates": nd,
            "min_cross_section": MIN_CS, "min_amount_wan": MIN_AMOUNT,
            "schemes": {k2: v for k2, v in SCHEMES.items()},
            "results": res, "summary": summary,
            "caveat": ("⚠️ 逐日等权持 N 只、h=20 后平仓、**未扣成本**的毛超额；"
                       "n_eff≈82；非样本外。N 与 N 之间高度相关（同一天同一批分数），"
                       "不能当作独立检验 → 不做 Bonferroni，仅作形态诊断。"),
        }, f, ensure_ascii=False, indent=1)
    print(f"\n写出 {OUT}  耗时 {time.time()-t0:.0f}s", flush=True)


if __name__ == "__main__":
    raise SystemExit(main())
