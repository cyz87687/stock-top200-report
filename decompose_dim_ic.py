"""decompose_dim_ic.py —— 分维度单因子 IC 分解 + 共线性 + Bonferroni 判定。

目的：回答三个必须回答的问题，否则权重无从调整。

D1 每一维单独的 h=20 IC 与 NW t 是多少？
   现行 9 维权重里 fund(.20)+dominant(.10)=.30 在长面板是**结构性缺失**
   （fund 无历史快照、dominant 无业务份额数据）→ 实际只有 7 维在起作用，
   但权重仍按 9 维口径标注。必须量化：这 7 维各自的真实贡献。

D2 三维共线（P0-3）：bias60 / rsi_rev / tech_neg 相关系数多少？
   若 ρ>0.7 → 名义 0.05+0.05+0.10=0.20 的实际独立信息量远小于 0.20。
   记忆已实测 bias60 vs rsi_rev ρ=+0.7625，本轮在 1653 天上复核。

D3 Bonferroni：本轮检验了多少次？
   n_trials 现算（记忆：364~804，阈值 3.8129~4.0043）。
   ⚠️ 禁抄历史数字 → 用 trial_log 现算，并在输出里写清 k 的来源。

输出 data/dim_ic_long.json
"""
import json
import math
import os
import sys
import time

import numpy as np
from scipy.stats import rankdata

HERE = os.path.dirname(os.path.abspath(__file__))
MAT = os.path.join(HERE, "data", "panel_r124_mat.npz")
KL = os.path.join(HERE, "data", "klines_2020.npz")
OUT = os.path.join(HERE, "data", "dim_ic_long.json")

W = {"score_fund": 0.20, "score_size_growth": 0.20, "score_tech_neg": 0.10,
     "score_quality": 0.10, "score_ep": 0.10, "score_dominant": 0.10,
     "score_growth": 0.10, "score_bias60": 0.05, "score_rsi_rev": 0.05}
MIN_CS = 200
MIN_AMOUNT = 2000.0
H = 20


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
    t0 = time.time()
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
    nD, nN = S.shape[0], S.shape[1]

    # ── D1 分维度 IC ────────────────────────────────────────────────
    print("D1 分维度单因子 IC（h=20, liq 过滤）...")
    per_dim = {}
    for ki, name in enumerate(dims):
        ics = []
        for n in range(nD):
            di = di_map.get(dates[n])
            if di is None or di + H >= close.shape[1]:
                continue
            v = S[n, :, ki]
            sel = np.isfinite(v) & liq[n]
            if sel.sum() < MIN_CS:
                continue
            p0 = close[:, di].astype(float)
            p1 = close[:, di + H].astype(float)
            ok = sel & (p0 > 0) & np.isfinite(p1)
            if ok.sum() < MIN_CS:
                continue
            r = p1[ok] / p0[ok] - 1.0
            ic = spearman(v[ok], r)
            if ic is not None:
                ics.append(ic)
        if len(ics) < 5:
            per_dim[name] = {"n": len(ics), "usable": False}
            continue
        nn = len(ics)
        tnw = nw_t(ics, H - 1)
        per_dim[name] = {
            "n": nn, "n_effective_obs": round(nn / H, 1),
            "ic_mean": round(float(np.mean(ics)), 4),
            "nw_t": round(tnw, 2) if tnw else None,
            "win_rate": round(sum(1 for x in ics if x > 0) / nn, 3),
            "weight": W.get(name, 0.0),
        }
        g = per_dim[name]
        print(f"  {name:<20} w={g['weight']:.2f}  n={nn:<5} "
              f"IC={g['ic_mean']:+.4f}  NWt={g['nw_t']:>6}  "
              f"胜率={g['win_rate']:.3f}", flush=True)

    # ── D2 共线性 ───────────────────────────────────────────────────
    print("\nD2 维度间 Spearman 相关（全池混合，检验共线）...")
    tri = [d for d in ("score_bias60", "score_rsi_rev", "score_tech_neg")
           if d in dims]
    corr = {}
    # 抽样日期算相关（全 1653 日 × 2000 只太慢，取 200 日）
    rng = np.random.default_rng(7)
    pick = sorted(rng.choice(nD, size=min(200, nD), replace=False))
    for i, a in enumerate(tri):
        for b in tri[i + 1:]:
            va, vb = [], []
            ia, ib = dims.index(a), dims.index(b)
            for n in pick:
                x, y = S[n, :, ia], S[n, :, ib]
                m = np.isfinite(x) & np.isfinite(y)
                if m.sum() >= MIN_CS:
                    va.append(x[m]); vb.append(y[m])
            if not va:
                continue
            ca = np.concatenate(va); cb = np.concatenate(vb)
            rho = spearman(ca, cb)
            corr[f"{a}|{b}"] = round(rho, 4) if rho is not None else None
            flag = "  ⚠️ 高共线" if rho and abs(rho) > 0.7 else ""
            print(f"  {a:<18} vs {b:<18} ρ={rho:+.4f}{flag}")

    # 三维等权合成的 IC，与单独比
    if len(tri) == 3:
        ia, ib, ic_ = [dims.index(x) for x in tri]
        comp_ics = []
        for n in range(nD):
            di = di_map.get(dates[n])
            if di is None or di + H >= close.shape[1]:
                continue
            M = np.isfinite(S[n, :, ia]) & np.isfinite(S[n, :, ib]) & np.isfinite(S[n, :, ic_])
            sel = M & liq[n]
            if sel.sum() < MIN_CS:
                continue
            p0 = close[:, di].astype(float); p1 = close[:, di + H].astype(float)
            ok = sel & (p0 > 0) & np.isfinite(p1)
            if ok.sum() < MIN_CS:
                continue
            # 三维各按自身量程归一后等权（S 是 (D,N,K)，ok 是对 N 的掩码）
            mx = {"score_bias60": 5.0, "score_rsi_rev": 5.0}
            s = (S[n][ok, ia] / mx[tri[0]] + S[n][ok, ib] / mx[tri[1]]
                 + S[n][ok, ic_] / 10.0)
            r = p1[ok] / p0[ok] - 1.0
            v = spearman(s, r)
            if v is not None:
                comp_ics.append(v)
        if len(comp_ics) > 5:
            t3 = nw_t(comp_ics, H - 1)
            corr["_merged3"] = {
                "n": len(comp_ics),
                "ic_mean": round(float(np.mean(comp_ics)), 4),
                "nw_t": round(t3, 2) if t3 else None,
                "note": "三维合成 vs 各单维：若合并 IC 不优于任一单维 → 共线冗余",
            }
            print(f"  三维合成       n={len(comp_ics)} "
                  f"IC={np.mean(comp_ics):+.4f} NWt={t3:.2f}")

    # ── D3 Bonferroni ───────────────────────────────────────────────
    print("\nD3 Bonferroni（走 trial_log 权威函数，**禁手算**）...")
    # ⚠️ 血案（本轮）：第一版直接遍历 trial_log.json 数
    #    kind∈(factor_test,strategy_backtest) → 得 k=60。
    #    这**漏掉了 legacy.k=304**（round13~65 的聚合声明，
    #    verifiable=false 但按「凡产出决策结论的检验都算一次试验」口径必须计入）。
    #    项目已有权威入口 trial_log.n_trials() / k_range() / k_summary()，
    #    自算是重复实现且必然漏算 → 必须调用它。
    k_lo = k_hi = None
    k_src = "trial_log 不可用"
    k_detail = {}
    try:
        import trial_log as T
        k_lo = int(T.n_trials())
        kr = T.k_range()
        k_hi = int(kr.get("k_upper_estimate") or k_lo)
        k_detail = dict(kr)
        k_detail["k_summary"] = T.k_summary()
        k_src = ("trial_log.n_trials()=legacy.k+试验轮次；"
                 "k_range().k_upper_estimate 为估计上界")
    except Exception as e:
        k_src = f"trial_log 调用失败：{e}"
    # 本轮新增的检验次数
    this_round = (len([d for d in per_dim if per_dim[d].get("n", 0) >= 5])
                  + 3 + len(corr) + 1)
    print(f"  trial_log.n_trials() = {k_lo}  （{k_src}）")
    print(f"  k_range 上界 = {k_hi}")
    print(f"  本轮新增检验 ≈ {this_round} 次")
    from scipy.stats import norm
    k_use_lo = (k_lo or 0) + this_round
    k_use_hi = (k_hi or k_lo or 0) + this_round
    thr = thr_hi = None
    if k_use_lo > 0:
        thr = round(float(norm.ppf(1 - 0.05 / (2 * k_use_lo))), 4)
        thr_hi = round(float(norm.ppf(1 - 0.05 / (2 * k_use_hi))), 4)
        print(f"  k={k_use_lo} → |t| ≥ {thr}（下界口径，较宽松）")
        print(f"  k={k_use_hi} → |t| ≥ {thr_hi}（上界口径，较严格）")
        print("  ⚠️ 两个都要报：只报下界会夸大显著性，只报上界则依赖估计值")

    out = {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "horizon": H, "min_cross_section": MIN_CS,
        "min_amount_wan": MIN_AMOUNT,
        "dim_ic": per_dim,
        "collinearity": corr,
        "bonferroni": {
            "k_lower": k_use_lo, "k_upper": k_use_hi,
            "k_trial_log_n": k_lo, "k_range_upper": k_hi,
            "k_this_round": this_round, "k_source": k_src,
            "threshold_abs_t_lower_k": thr,
            "threshold_abs_t_upper_k": thr_hi,
            "k_detail": k_detail,
            "note": ("k 必须走 trial_log.n_trials()/k_range()，禁手算；"
                     "下界=legacy.k(304)+轮次，上界=估计补齐。"
                     "两口径都要报，只报一个会误导。"),
        },
        "missing_dims": [d for d in W if d not in dims],
        "weight_declared_sum": round(sum(W.values()), 2),
        "weight_effective_sum": round(
            sum(W.get(d, 0.0) for d in dims), 2),
    }
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print(f"\n写出 {OUT}  耗时 {time.time()-t0:.0f}s")

    print("\n=== 关键判定 ===")
    if thr:
        passed = [n for n, v in per_dim.items()
                  if v.get("nw_t") and abs(v["nw_t"]) >= thr]
        passed_hi = [n for n, v in per_dim.items()
                     if v.get("nw_t") and abs(v["nw_t"]) >= thr_hi]
        print(f"  过 Bonferroni 下界(|t|≥{thr}) 的维度: {passed or '无'}")
        print(f"  过 Bonferroni 上界(|t|≥{thr_hi}) 的维度: {passed_hi or '无'}")
    print(f"  声明权重和 {out['weight_declared_sum']} / "
          f"实际生效维权重和 {out['weight_effective_sum']}")
    print(f"  结构性缺失维: {out['missing_dims']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
