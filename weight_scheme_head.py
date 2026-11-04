"""weight_scheme_head.py —— P0-4 补测：按**真实持仓规模**对照方案，而不是 Top10%。

为什么必须补这一刀（本轮关键）：
    weight_scheme_test.py 用 Top10%（截面约 1700 只 → 约 170 只）算超额，
    结果出现「IC 全线上升、超额全线下降」的反向关系。
    但生产组合实际只持 6~13 只（pick_portfolio.py: w_total / max_single_stock）。
    ⇒ 170 只的「超额」根本不是产品收益，用它来裁决权重是**口径错位**。

本脚本测 TopN（10/20/30/50）与「TopN − 池等权」，并额外给出：
    · 头部集中度：excess(Top10) / excess(Top10%)  —— 信息是否集中在最头部
    · 多空两端：TopN − BotN  —— 检验维度是否只是「避开最差」而非「选出最好」
    · IR：excess 的均值/标准差，判断提升是否稳

⚠️ 仍然不是样本外。仍受选择偏差。仅用于「往哪个方向改」。
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
OUT = os.path.join(HERE, "data", "weight_scheme_head.json")

# round125: dim_max 改为**单一来源**（原为硬编码，且漏了 score_fund: 5.0）。
#   铁律：量程只能来自 factor_weights.json；本文件用到的维度在此显式声明，
#   缺失由 dim_cfg 直接报错而非静默按 10.0 处理。
from dim_cfg import load_dim_max
DIM_MAX = load_dim_max(['score_bias60', 'score_rsi_rev', 'score_lowvol20', 'score_tech_neg', 'score_size_growth', 'score_ep', 'score_quality', 'score_growth'], strict=True)
MIN_CS = 200
MIN_AMOUNT = 2000.0
H = 20
TOPS = (10, 20, 30, 50)

SCHEMES = {
    "S0_v417_baseline": {
        "score_fund": 0.20, "score_size_growth": 0.20, "score_tech_neg": 0.10,
        "score_quality": 0.10, "score_ep": 0.10, "score_dominant": 0.10,
        "score_growth": 0.10, "score_bias60": 0.05, "score_rsi_rev": 0.05},
    "S2_drop_techneg_realloc30": {
        "score_fund": 0.0, "score_size_growth": 0.30, "score_tech_neg": 0.0,
        "score_quality": 0.15, "score_ep": 0.15, "score_dominant": 0.0,
        "score_growth": 0.15, "score_bias60": 0.125, "score_rsi_rev": 0.125},
    "S3_ic_tilt": {
        "score_fund": 0.0, "score_size_growth": 0.20, "score_tech_neg": 0.0,
        "score_quality": 0.10, "score_ep": 0.10, "score_dominant": 0.0,
        "score_growth": 0.10, "score_bias60": 0.25, "score_rsi_rev": 0.05,
        "score_lowvol20": 0.20},
    "S5_equal8": {d: 1.0 / 8 for d in DIM_MAX},
    # 补两个单维方案：直接看「谁是头部收益的真正来源」
    "ONLY_lowvol20": {**{d: 0.0 for d in DIM_MAX}, "score_lowvol20": 1.0},
    "ONLY_bias60": {**{d: 0.0 for d in DIM_MAX}, "score_bias60": 1.0},
    "ONLY_size_growth": {**{d: 0.0 for d in DIM_MAX}, "score_size_growth": 1.0},
    "ONLY_ep": {**{d: 0.0 for d in DIM_MAX}, "score_ep": 1.0},
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


def spearman(a, b):
    ra, rb = rankdata(a), rankdata(b)
    da = ra - ra.mean()
    db = rb - rb.mean()
    den = math.sqrt(float(da @ da) * float(db @ db))
    return float(da @ db) / den if den > 0 else None


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

    # 累加器：per scheme × per topN
    acc = {n: {t: [] for t in list(TOPS) + ["pct10", "bot"]} for n in SCHEMES}
    ic_acc = {n: [] for n in SCHEMES}
    dates_all = []

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
        dates_all.append(d)
        for name, v in wvec.items():
            num = Sf[n] @ v
            den = mask[n].astype(np.float64) @ v
            with np.errstate(invalid="ignore", divide="ignore"):
                comp = np.where(den > 1e-9, num / np.maximum(den, 1e-12), np.nan)
            sel = okret & np.isfinite(comp)
            if sel.sum() < MIN_CS:
                continue
            fv, rv = comp[sel], ret[sel]
            ic = spearman(fv, rv)
            if ic is not None:
                ic_acc[name].append(ic)
            o = np.argsort(-fv)
            base = float(rv.mean())
            for t in TOPS:
                if len(o) < t:
                    continue
                acc[name][t].append(float(rv[o[:t]].mean()) - base)
            k10p = max(1, len(o) // 10)
            acc[name]["pct10"].append(float(rv[o[:k10p]].mean()) - base)
            bot = max(1, min(len(o) // 10, 200))
            acc[name]["bot"].append(float(rv[o[-bot:]].mean()) - base)
        if n % 400 == 0:
            print(f"  {n}/{len(pdates)}", flush=True)

    def stat(xs):
        if not xs:
            return None
        a = np.asarray(xs, dtype=np.float64)
        mu, sd = float(a.mean()), float(a.std(ddof=1))
        return {
            "n": len(a),
            "mean_pct": round(mu * 100, 3),
            "ir": round(mu / sd, 3) if sd > 0 else None,
            "nw_t": round(nw_t(xs, H - 1), 2) if nw_t(xs, H - 1) else None,
            "win_rate": round(float((a > 0).mean()), 3),
            "p25_pct": round(float(np.percentile(a, 25)) * 100, 3),
            "p75_pct": round(float(np.percentile(a, 75)) * 100, 3),
        }

    res = {}
    for name in SCHEMES:
        res[name] = {
            "ic_mean": round(float(np.mean(ic_acc[name])), 4) if ic_acc[name] else None,
            "ic_nw_t": round(nw_t(ic_acc[name], H - 1), 2) if len(ic_acc[name]) > 3 else None,
            "topN": {f"top{t}": stat(acc[name][t]) for t in TOPS},
            "top_pct10": stat(acc[name]["pct10"]),
            "bottom10": stat(acc[name]["bot"]),
        }
        # 头部集中度：Top10 超额 / Top10% 超额（>1 表示信息集中在最头部）
        t10 = (res[name]["topN"]["top10"] or {}).get("mean_pct")
        p10 = (res[name]["top_pct10"] or {}).get("mean_pct")
        res[name]["head_concentration"] = (
            round(t10 / p10, 2) if (t10 is not None and p10 and abs(p10) > 1e-9) else None)

    print("\n=== h=20 liq 档 · 按真实持仓规模 ===", flush=True)
    hdr = f"{'scheme':<28}{'IC':>8}{'ICt':>7}" + "".join(f"{'T'+str(t):>9}" for t in TOPS) \
          + f"{'T10%':>8}{'Bot10%':>9}{'集中度':>7}"
    print(hdr, flush=True)
    for name, r in res.items():
        row = f"{name:<28}{r['ic_mean']:>+8.4f}{str(r['ic_nw_t']):>7}"
        for t in TOPS:
            row += f"{r['topN']['top'+str(t)]['mean_pct']:>+9.2f}"
        row += f"{r['top_pct10']['mean_pct']:>+8.2f}{r['bottom10']['mean_pct']:>+9.2f}"
        row += f"{str(r['head_concentration']):>7}"
        print(row, flush=True)

    print("\n=== Top10 只的 IR / t / 胜率 / 四分位 ===", flush=True)
    for name, r in res.items():
        s = r["topN"]["top10"]
        print(f"  {name:<28} 均值={s['mean_pct']:+.3f}% IR={s['ir']:>6} "
              f"NWt={str(s['nw_t']):>6} 胜率={s['win_rate']:.3f} "
              f"[P25={s['p25_pct']:+.2f}%, P75={s['p75_pct']:+.2f}%]", flush=True)

    print("\n=== 相对 S0 的 Top10 增量 ===", flush=True)
    b = res["S0_v417_baseline"]["topN"]["top10"]["mean_pct"]
    for name, r in res.items():
        if name == "S0_v417_baseline":
            continue
        s = r["topN"]["top10"]["mean_pct"]
        print(f"  {name:<28} ΔTop10={s-b:+.3f}pp  ΔIC={r['ic_mean']-res['S0_v417_baseline']['ic_mean']:+.4f}",
              flush=True)

    with open(OUT, "w", encoding="utf-8") as f:
        json.dump({
            "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "src": "data/panel_r124_mat.npz + data/klines_2020.npz",
            "horizon": H, "n_dates": len(dates_all),
            "range": [dates_all[0], dates_all[-1]] if dates_all else None,
            "min_cross_section": MIN_CS, "min_amount_wan": MIN_AMOUNT,
            "dim_max": DIM_MAX,
            "schemes": {k2: v for k2, v in SCHEMES.items()},
            "results": res,
            "caveat": ("⚠️ TopN 为逐日等权持有 N 只、h=20 后平仓、**未扣交易成本**的毛超额；"
                       "n_eff≈82（不重叠）；仍非样本外，且方案是看过结果后选的 → 存在选择偏差。"
                       "仅用于判断「方向」，不得作为收益预期。"),
        }, f, ensure_ascii=False, indent=1)
    print(f"\n写出 {OUT}  耗时 {time.time()-t0:.0f}s", flush=True)


if __name__ == "__main__":
    raise SystemExit(main())
