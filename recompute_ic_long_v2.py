"""recompute_ic_long_v2.py —— 在 2020 起长面板上重算三周期 IC（npz 快速版）。

与 v1 的差别：
    v1 直接 json.load 938MB 单行面板 → 峰值 4~5 GB，本机必被 SIGTERM。
    v2 读 pack_panel_npz.py 产出的紧凑矩阵 data/panel_r124_mat.npz（78 MB），
       全程 numpy 向量化，峰值 < 1 GB。

口径（与生产 v4.17 严格一致）：
    权重 fund .20 / size_growth .20 / tech_neg .10 / quality .10 / ep .10 /
         dominant .10 / growth .10 / bias60 .05 / rsi_rev .05
    ⚠️ fund 与 dominant 在长面板全 None → **从分母剔除**再重归一化，
       不能用 `or 0`（那等于把「没测」当「0 分」，系统性压低总分）。
    实际生效：6 维（size_growth .20 / tech_neg .10 / quality .10 / ep .10 /
              growth .10 / bias60 .05 / rsi_rev .05）= Σ0.70 → 逐行除以 0.70

⚠️ 三档过滤（必须都报，否则无法判断 alpha 是否只在不可交易样本里）：
    raw   —— 无过滤（与旧结论可直接比）
    liq   —— 日成交额 ≥ MIN_AMOUNT 万元（可交易性下限）
    trad  —— liq + 剔除 t 日近似涨停（主板 9.8%，创/科 19.8%）
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
OUT = os.path.join(HERE, "data", "ic_long_2020.json")

# 生产权重（与 factor_weights.json v4.17-cycle-rules 一致）
W = {"score_fund": 0.20, "score_size_growth": 0.20, "score_tech_neg": 0.10,
     "score_quality": 0.10, "score_ep": 0.10, "score_dominant": 0.10,
     "score_growth": 0.10, "score_bias60": 0.05, "score_rsi_rev": 0.05}
# round125: dim_max 改为**单一来源**（原为硬编码，且漏了 score_fund: 5.0）。
#   铁律：量程只能来自 factor_weights.json；本文件用到的维度在此显式声明，
#   缺失由 dim_cfg 直接报错而非静默按 10.0 处理。
from dim_cfg import load_dim_max
DIM_MAX = load_dim_max(['score_bias60', 'score_rsi_rev'], strict=True)
MIN_CS = 200          # 单截面最少股票数
MIN_AMOUNT = 2000.0   # 万元 —— 日成交额下限（2000 万）
HORIZONS = (1, 5, 20)
NEFF_MIN = 30


def nw_t(xs, lag):
    """Newey-West HAC t 值。"""
    n = len(xs)
    if n < 3:
        return None
    a = np.asarray(xs, dtype=np.float64)
    m = a.mean()
    e = a - m
    s = float(e @ e) / n
    for k in range(1, min(lag, n - 1) + 1):
        gk = float(e[k:] @ e[:-k]) / n
        s += 2.0 * (1.0 - k / (lag + 1.0)) * gk
    if s <= 0:
        return None
    return float(m / math.sqrt(s / n))


def plain_t(xs):
    n = len(xs)
    if n < 3:
        return None
    a = np.asarray(xs, dtype=np.float64)
    sd = a.std(ddof=1)
    return float(a.mean() / (sd / math.sqrt(n))) if sd > 0 else None


def spearman(a, b):
    """向量化 Spearman（rankdata 平均秩处理并列）。"""
    ra, rb = rankdata(a), rankdata(b)
    ma, mb = ra.mean(), rb.mean()
    da = ra - ma
    db = rb - mb
    den = math.sqrt(float(da @ da) * float(db @ db))
    return float(da @ db) / den if den > 0 else None


def main():
    t0 = time.time()
    print("加载矩阵 ...")
    z = np.load(MAT, allow_pickle=False)
    pdates = [str(d) for d in z["dates"]]
    codes = [str(c) for c in z["codes"]]
    dims = [str(d) for d in z["dims"]]
    S = z["S"]                                   # (D, N, 8)
    P = z["P"]                                   # (D, N, 4)
    print(f"  面板 {len(pdates)} 日 × {len(codes)} 只 × {len(dims)} 维  {S.shape}")

    k = np.load(KL, allow_pickle=False)
    close = k["X"][:, :, 1]                       # (Nk, Kd)
    kdates = [str(d) for d in k["dates"]]
    kcodes = [str(c) for c in k["codes"]]
    print(f"  行情 {close.shape[0]} 只 × {close.shape[1]} 日")

    if kcodes != codes:
        kmap = {c: i for i, c in enumerate(kcodes)}
        order = [kmap[c] for c in codes]
        close = close[order]
        print("  ⚠️ 代码顺序不同，已重排对齐")
    kdmap = {d: i for i, d in enumerate(kdates)}

    # 权重数组（只含面板里真实存在的维）
    wv = np.zeros(len(dims), dtype=np.float64)
    for i, d in enumerate(dims):
        wv[i] = W.get(d, 0.0) / DIM_MAX.get(d, 10.0)
    print("  生效权重: " + ", ".join(
        f"{d}={wv[i]:.4f}" for i, d in enumerate(dims)))
    print(f"  Σ原始权重 = {sum(W.get(d,0) for d in dims):.2f}")

    # 预先算好「分母」（有值维的权重和），raw/liq/trad 三档共用
    mask = ~np.isnan(S)                            # (D, N, 8) bool
    Sf = np.where(mask, S, 0.0).astype(np.float64)
    num_all = Sf @ wv                              # (D, N)
    den_all = mask.astype(np.float64) @ wv          # (D, N)
    with np.errstate(invalid="ignore", divide="ignore"):
        comp = np.where(den_all > 1e-9, num_all / np.maximum(den_all, 1e-12), np.nan)
    print(f"  总分矩阵 {comp.shape}，有值行占比 "
          f"{np.mean(np.isfinite(comp)):.2%}")

    amt = P[:, :, 2]                                # 万元
    liq_ok = np.isfinite(amt) & (amt >= MIN_AMOUNT)
    print(f"  流动性过滤（≥{MIN_AMOUNT:.0f}万）保留 "
          f"{liq_ok.sum()/max(1,np.isfinite(amt).sum()):.1%}")

    # 近似涨停标记：用 t 日涨幅判断（主板 9.8%，创业板/科创板 19.8%）
    with np.errstate(invalid="ignore", divide="ignore"):
        r1 = np.full_like(close, np.nan, dtype=np.float64)
        r1[:, 1:] = close[:, 1:] / np.where(close[:, :-1] > 0, close[:, :-1], np.nan) - 1.0
    limit = np.where(np.isin([c[:3] for c in codes], ["300", "301", "688", "689"]),
                     0.198, 0.098)[:, None]
    limit_up = np.isfinite(r1) & (r1 >= limit)

    results = {}
    for h in HORIZONS:
        packs = {m: {"ic": [], "exc": [], "dates": [], "cs": []}
                 for m in ("raw", "liq", "trad")}
        for n, d in enumerate(pdates):
            di = kdmap.get(d)
            if di is None or di + h >= close.shape[1]:
                continue
            srow = comp[n]
            base = np.isfinite(srow)
            if base.sum() < MIN_CS:
                continue
            p0 = close[:, di].astype(np.float64)
            p1 = close[:, di + h].astype(np.float64)
            with np.errstate(invalid="ignore", divide="ignore"):
                ret = p1 / np.where(p0 > 0, p0, np.nan) - 1.0
            base &= np.isfinite(ret)
            if base.sum() < MIN_CS:
                continue
            cand = {
                "raw": base,
                "liq": base & liq_ok[n],
                "trad": base & liq_ok[n] & ~limit_up[:, di],
            }
            for m, sel in cand.items():
                if sel.sum() < MIN_CS:
                    continue
                fv, rv = srow[sel], ret[sel]
                ic = spearman(fv, rv)
                if ic is None:
                    continue
                o = np.argsort(-fv)
                k10 = max(1, len(o) // 10)
                exc = float(rv[o[:k10]].mean() - rv.mean())
                p = packs[m]
                p["ic"].append(ic)
                p["exc"].append(exc)
                p["dates"].append(d)
                p["cs"].append(int(sel.sum()))
            if n % 200 == 0:
                print(f"  h={h}  {n}/{len(pdates)}  {d}", flush=True)

        out = {}
        for m, p in packs.items():
            ics = p["ic"]
            if not ics:
                out[m] = {"n": 0, "usable": False}
                continue
            nn = len(ics)
            neff = nn / h
            tn, tnw = plain_t(ics), nw_t(ics, h - 1)
            out[m] = {
                "n": nn,
                "n_effective_obs": round(neff, 1),
                "ic_mean": round(float(np.mean(ics)), 4),
                "ic_std": round(float(np.std(ics, ddof=1)), 4),
                "t_naive": round(tn, 2) if tn else None,
                "nw_t": round(tnw, 2) if tnw else None,
                "nw_lag": h - 1,
                "t_inflation": round(tn / tnw, 2) if (tn and tnw) else None,
                "excess_pct": round(float(np.mean(p["exc"])) * 100, 3),
                "win_rate": round(sum(1 for x in ics if x > 0) / nn, 3),
                "median_cs": int(np.median(p["cs"])),
                "min_cs": int(min(p["cs"])),
                "date_range": [p["dates"][0], p["dates"][-1]],
                "usable": bool(neff >= NEFF_MIN),
                "usable_note": (f"有效独立观测 {neff:.1f}"
                                + ("（≥30，可引用，仍需叠加 Bonferroni）"
                                   if neff >= NEFF_MIN else
                                   "（<30 → NW t 无信息量，不得引用）")),
                "ic_series": [round(float(x), 4) for x in ics],
                "dates": p["dates"],
            }
        results[f"h{h}"] = out
        for m in ("raw", "liq", "trad"):
            r = out.get(m) or {}
            if not r.get("n"):
                continue
            print(f"  h={h:<3}[{m:<4}] n={r['n']:<5} IC={r['ic_mean']:+.4f} "
                  f"NWt={r['nw_t']:>7} n_eff={r['n_effective_obs']:>6.1f} "
                  f"超额={r['excess_pct']:+.2f}% 胜率={r['win_rate']:.3f} "
                  f"cs中位={r['median_cs']}", flush=True)

    # 分年度稳定性（用 liq 档，最贴近可执行）
    yearly = {}
    for h in HORIZONS:
        r = results[f"h{h}"]["liq"]
        if not r.get("n"):
            continue
        buck = {}
        for d, ic in zip(r["dates"], r["ic_series"]):
            buck.setdefault(d[:4], []).append(ic)
        yearly[f"h{h}"] = {
            y: {"n": len(v), "ic": round(float(np.mean(v)), 4),
                "nw_t": round(nw_t(v, h - 1), 2) if len(v) > 2 else None}
            for y, v in sorted(buck.items())}

    # LOO：逐段剔除，看结论是否只靠某一段
    loo = {}
    for h in HORIZONS:
        r = results[f"h{h}"]["liq"]
        if r.get("n", 0) < 30:
            continue
        ds, ics = r["dates"], r["ic_series"]
        yrs = sorted({d[:4] for d in ds})
        loo[f"h{h}"] = {}
        for y in yrs:
            sub = [ic for d, ic in zip(ds, ics) if d[:4] != y]
            loo[f"h{h}"][f"drop_{y}"] = {
                "n": len(sub),
                "ic": round(float(np.mean(sub)), 4),
                "nw_t": round(nw_t(sub, h - 1), 2) if len(sub) > 2 else None,
            }

    with open(OUT, "w", encoding="utf-8") as f:
        json.dump({
            "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "src": "data/panel_r124_mat.npz（源自 panel_round124_long.json）",
            "n_dates": len(pdates),
            "range": [pdates[0], pdates[-1]],
            "weight_used": W,
            "weight_effective": {d: round(float(wv[i]), 4) for i, d in enumerate(dims)},
            "missing_dims": [d for d in W if d not in dims],
            "dim_max": DIM_MAX,
            "min_cross_section": MIN_CS,
            "min_amount_wan": MIN_AMOUNT,
            "neff_min": NEFF_MIN,
            "results": results,
            "yearly_liq": yearly,
            "leave_one_year_out_liq": loo,
        }, f, ensure_ascii=False, indent=1)
    print(f"\n写出 {OUT}  耗时 {time.time()-t0:.0f}s")

    print("\n=== 汇总（liq 档）===")
    for h in HORIZONS:
        r = results[f"h{h}"]["liq"]
        print(f"  h={h:<3} n={r['n']:<5} IC={r['ic_mean']:+.4f} "
              f"NWt={r['nw_t']:>7} n_eff={r['n_effective_obs']:>6.1f} "
              f"{'✅' if r['usable'] else '❌'}")
    print("\n=== 分年度 IC（liq）===")
    for h in HORIZONS:
        y = yearly.get(f"h{h}") or {}
        print(f"  h={h}: " + "  ".join(
            f"{k}={v['ic']:+.3f}(n{v['n']})" for k, v in y.items()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
