"""weight_scheme_test.py —— P0-4 决策依据：在**同一份长面板同一段样本**上
对照多个权重方案，用 composite IC / NW t / Top10% 超额 / 胜率裁决。

为什么必须做这一步：
    分维度 IC 只能回答「这一维有没有信息」，不能回答「把它加权重后组合会不会更好」。
    而 P0-4 要处置的正是**权重**：
      · score_fund     名义 0.20，长面板覆盖 0/1653 → 生产实际靠「逐行重归一化」绕过
      · score_dominant 名义 0.10，覆盖 0/1653 → 同上
      · score_tech_neg 名义 0.10，IC −0.0069 / NW t −0.67 → **有数据但是负向**
      · score_lowvol20 名义 0.00，IC +0.0828 / NW t 4.96 → **最强却被归零**
    合计 0.40 的名义权重存在口径矛盾。改之前必须证明「改成哪个更好」。

⚠️ 纪律：
    1) 不改 factor_weights.json。本脚本只做**测算**，不落权重。
    2) 权重改动若真要落地，须八处同步 + verify_weights_round36.py PASS。
    3) 所有方案都在**完全相同**的日期域、流动性档、截面门槛下比，否则不可比。
    4) 有值维度重归一化：den = Σ(w of dims with value)；缺维从分母剔除，禁 `or 0`。
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
OUT = os.path.join(HERE, "data", "weight_scheme_test.json")

# 维度量程：bias60/rsi_rev 是 0-5，其余 0-10。
# score_fund 在生产 dim_max 里是 5.0，但它 0 覆盖，本脚本不涉及。
# round125: dim_max 改为**单一来源**（原为硬编码，且漏了 score_fund: 5.0）。
#   铁律：量程只能来自 factor_weights.json；本文件用到的维度在此显式声明，
#   缺失由 dim_cfg 直接报错而非静默按 10.0 处理。
from dim_cfg import load_dim_max
DIM_MAX = load_dim_max(['score_bias60', 'score_rsi_rev', 'score_lowvol20', 'score_tech_neg', 'score_size_growth', 'score_ep', 'score_quality', 'score_growth'], strict=True)
MIN_CS = 200
MIN_AMOUNT = 2000.0     # 万元
HORIZONS = (5, 20)
NEFF_MIN = 30

# ── 待对照的方案 ─────────────────────────────────────────────────────────
# 每个方案用「名义权重」表达（可含 fund/dominant，它们会被自动从分母剔除）。
SCHEMES = {
    # S0 现行 v4.17 —— 基线。fund .20 + dominant .10 无数据 → 实际生效仅 .70
    "S0_v417_baseline": {
        "note": "现行 v4.17-cycle-rules（fund/dominant 缺数据，逐行重归一化后实际生效 0.70）",
        "w": {"score_fund": 0.20, "score_size_growth": 0.20, "score_tech_neg": 0.10,
              "score_quality": 0.10, "score_ep": 0.10, "score_dominant": 0.10,
              "score_growth": 0.10, "score_bias60": 0.05, "score_rsi_rev": 0.05},
    },
    # S1 最小改动：仅把唯一有**负向证据**的 tech_neg 归零，其余占比按比例放大
    "S1_drop_techneg": {
        "note": "只删 tech_neg（IC −0.0069 / t −0.67，唯一有直接负向证据的维）；其余维度按原比例重归一化",
        "w": {"score_fund": 0.20, "score_size_growth": 0.20, "score_tech_neg": 0.0,
              "score_quality": 0.10, "score_ep": 0.10, "score_dominant": 0.10,
              "score_growth": 0.10, "score_bias60": 0.05, "score_rsi_rev": 0.05},
    },
    # S2 删 tech_neg + 把 fund(.20)/dominant(.10) 这 0.30 摊到「有数据的 6 维」按原比例
    "S2_drop_techneg_realloc30": {
        "note": "删 tech_neg；fund+dominant 的 0.30 按其余 6 维原比例摊入（等效于现行实际生效比例，但去掉 0 覆盖维的占位）",
        "w": {"score_fund": 0.0, "score_size_growth": 0.30, "score_tech_neg": 0.0,
              "score_quality": 0.15, "score_ep": 0.15, "score_dominant": 0.0,
              "score_growth": 0.15, "score_bias60": 0.125, "score_rsi_rev": 0.125},
    },
    # S3 IC 导向：把 .30 的失效权重 + tech_neg 的 .10 = .40 集中给实测最强的两维
    "S3_ic_tilt": {
        "note": "删 tech_neg；fund+dominant（0.30）与 tech_neg（0.10）合计 0.40 全部给 lowvol20(0.20)+bias60(0.20)；其余维保留原值",
        "w": {"score_fund": 0.0, "score_size_growth": 0.20, "score_tech_neg": 0.0,
              "score_quality": 0.10, "score_ep": 0.10, "score_dominant": 0.0,
              "score_growth": 0.10, "score_bias60": 0.25, "score_rsi_rev": 0.05,
              "score_lowvol20": 0.20},
    },
    # S4 纯 IC 加权（信息比率口径），用于检验「数据驱动」的上界参考
    "S4_ic_weighted": {
        "note": "8 个有数据维按 |IC| 正部加权（负向直接置 0），检验「全数据驱动」能到哪里",
        "w": {"score_fund": 0.0, "score_dominant": 0.0, "score_tech_neg": 0.0,
              "score_lowvol20": 0.26, "score_bias60": 0.23, "score_rsi_rev": 0.12,
              "score_size_growth": 0.11, "score_ep": 0.09, "score_growth": 0.04,
              "score_quality": 0.02},
    },
    # S5 等权 8 维（无参数、无拟合），当作「不靠任何 IC 估计」的下限对照
    "S5_equal8": {
        "note": "8 个有数据维等权（0.125 each），零参数、零拟合 —— 用来检验上面的加权是不是在拟合噪声",
        "w": {d: 1.0 / 8 for d in DIM_MAX},
    },
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
        gk = float(e[k:] @ e[:-k]) / n
        s += 2.0 * (1.0 - k / (lag + 1.0)) * gk
    return float(m / math.sqrt(s / n)) if s > 0 else None


def spearman(a, b):
    ra, rb = rankdata(a), rankdata(b)
    da = ra - ra.mean()
    db = rb - rb.mean()
    den = math.sqrt(float(da @ da) * float(db @ db))
    return float(da @ db) / den if den > 0 else None


def main():
    t0 = time.time()
    print("加载面板与行情 ...", flush=True)
    z = np.load(MAT, allow_pickle=False)
    pdates = [str(d) for d in z["dates"]]
    codes = [str(c) for c in z["codes"]]
    dims = [str(d) for d in z["dims"]]
    S = z["S"]
    P = z["P"]

    k = np.load(KL, allow_pickle=False)
    close = k["X"][:, :, 1].astype(np.float64)
    kdates = [str(d) for d in k["dates"]]
    kcodes = [str(c) for c in k["codes"]]
    if kcodes != codes:
        kmap = {c: i for i, c in enumerate(kcodes)}
        close = close[[kmap[c] for c in codes]]
        print("  ⚠️ 代码顺序不同，已重排", flush=True)
    kdmap = {d: i for i, d in enumerate(kdates)}

    amt = P[:, :, 2]
    liq_ok = np.isfinite(amt) & (amt >= MIN_AMOUNT)
    mask = np.isfinite(S)                       # (D,N,8) bool
    Sf = np.where(mask, S, 0.0).astype(np.float64)

    # 预先把每个方案的权重向量算好（按 DIM_MAX 折算成「每分值得多少权重」）
    wvec = {}
    for name, sc in SCHEMES.items():
        w = sc["w"]
        missing = [d for d in w if w[d] > 0 and d not in dims]
        v = np.zeros(len(dims), dtype=np.float64)
        for i, d in enumerate(dims):
            v[i] = w.get(d, 0.0) / DIM_MAX.get(d, 10.0)
        wvec[name] = v
        eff = sum(w.get(d, 0.0) for d in dims)
        print(f"  {name:<28} 声明Σ={sum(w.values()):.3f} "
              f"面板可算Σ={eff:.3f} 缺维={missing or '无'}", flush=True)

    results = {}
    for h in HORIZONS:
        packs = {name: {"ic": [], "exc": [], "dates": []} for name in SCHEMES}
        for n, d in enumerate(pdates):
            di = kdmap.get(d)
            if di is None or di + h >= close.shape[1]:
                continue
            p0 = close[:, di]
            p1 = close[:, di + h]
            with np.errstate(invalid="ignore", divide="ignore"):
                ret = p1 / np.where(p0 > 0, p0, np.nan) - 1.0
            okret = np.isfinite(ret) & liq_ok[n]
            if okret.sum() < MIN_CS:
                continue
            for name, v in wvec.items():
                num = Sf[n] @ v                          # (N,)
                den = mask[n].astype(np.float64) @ v      # (N,)
                with np.errstate(invalid="ignore", divide="ignore"):
                    comp = np.where(den > 1e-9, num / np.maximum(den, 1e-12), np.nan)
                sel = okret & np.isfinite(comp)
                if sel.sum() < MIN_CS:
                    continue
                fv, rv = comp[sel], ret[sel]
                ic = spearman(fv, rv)
                if ic is None:
                    continue
                o = np.argsort(-fv)
                k10 = max(1, len(o) // 10)
                packs[name]["ic"].append(ic)
                packs[name]["exc"].append(float(rv[o[:k10]].mean() - rv.mean()))
                packs[name]["dates"].append(d)
            if n % 400 == 0:
                print(f"  h={h}  {n}/{len(pdates)}", flush=True)

        out = {}
        for name, p in packs.items():
            ics = p["ic"]
            if not ics:
                out[name] = {"n": 0}
                continue
            nn = len(ics)
            neff = nn / h
            out[name] = {
                "n": nn,
                "n_effective_obs": round(neff, 1),
                "ic_mean": round(float(np.mean(ics)), 4),
                "ic_std": round(float(np.std(ics, ddof=1)), 4),
                "nw_t": round(nw_t(ics, h - 1), 2) if nw_t(ics, h - 1) else None,
                "excess_pct": round(float(np.mean(p["exc"])) * 100, 3),
                "win_rate": round(sum(1 for x in ics if x > 0) / nn, 3),
                "usable": bool(neff >= NEFF_MIN),
                "dates": p["dates"],
                "ic_series": [round(float(x), 4) for x in ics],
            }
        results[f"h{h}"] = out
        print(f"\n=== h={h}（liq 档，n_eff 门槛 {NEFF_MIN}）===", flush=True)
        for name in SCHEMES:
            r = out[name]
            print(f"  {name:<28} n={r['n']:<5} IC={r['ic_mean']:+.4f} "
                  f"NWt={str(r['nw_t']):>6} n_eff={r['n_effective_obs']:>6} "
                  f"超额={r['excess_pct']:+.2f}% 胜率={r['win_rate']:.3f}", flush=True)

    # 分年稳健性（只对 h20，因为那是主目标周期）
    yearly = {}
    r20 = results.get("h20") or {}
    for name, r in r20.items():
        if not r.get("n"):
            continue
        buck = {}
        for d, ic in zip(r["dates"], r["ic_series"]):
            buck.setdefault(d[:4], []).append(ic)
        yearly[name] = {y: round(float(np.mean(v)), 4) for y, v in sorted(buck.items())}
    print("\n=== h=20 分年度 IC ===", flush=True)
    for name, y in yearly.items():
        print(f"  {name:<28} " + "  ".join(f"{k}={v:+.3f}" for k, v in y.items()),
              flush=True)

    # 与 S0 的差值（最关键的一栏：改动到底赚了多少）
    delta = {}
    base = (r20.get("S0_v417_baseline") or {})
    for name, r in r20.items():
        if name == "S0_v417_baseline" or not r.get("n"):
            continue
        delta[name] = {
            "d_ic": round(r["ic_mean"] - base.get("ic_mean", 0), 4),
            "d_nw_t": round((r["nw_t"] or 0) - (base.get("nw_t") or 0), 2),
            "d_excess_pct": round(r["excess_pct"] - base.get("excess_pct", 0), 3),
            "d_win_rate": round(r["win_rate"] - base.get("win_rate", 0), 3),
        }
    print("\n=== 相对 S0 的增量（h=20）===", flush=True)
    for name, dd in delta.items():
        print(f"  {name:<28} ΔIC={dd['d_ic']:+.4f} Δt={dd['d_nw_t']:+.2f} "
              f"Δ超额={dd['d_excess_pct']:+.2f}pp Δ胜率={dd['d_win_rate']:+.3f}", flush=True)

    with open(OUT, "w", encoding="utf-8") as f:
        json.dump({
            "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "src": "data/panel_r124_mat.npz + data/klines_2020.npz",
            "n_dates": len(pdates),
            "range": [pdates[0], pdates[-1]],
            "min_cross_section": MIN_CS,
            "min_amount_wan": MIN_AMOUNT,
            "neff_min": NEFF_MIN,
            "dim_max": DIM_MAX,
            "schemes": {k2: v["note"] for k2, v in SCHEMES.items()},
            "scheme_weights": {k2: v["w"] for k2, v in SCHEMES.items()},
            "results": {h: {n2: {kk: vv for kk, vv in r.items()
                                 if kk not in ("dates", "ic_series")}
                            for n2, r in res.items()}
                        for h, res in results.items()},
            "yearly_h20": yearly,
            "delta_vs_S0_h20": delta,
            "caveat": ("⚠️ 本表是**方案对照**，不是 alpha 验证：所有方案都在同一段 2020-01~2026-10 "
                       "历史上评估，没有真正的样本外；方案数量少（6 个）但仍在同一批数据上做了选择，"
                       "因此最优方案的 IC 本身已被选择偏差污染。DSR 的 n_trials 未计入本表。"
                       "⇒ 只能用于「要不要改、往哪个方向改」，不能用于「改了之后预期 IC 是多少」。"),
        }, f, ensure_ascii=False, indent=1)
    print(f"\n写出 {OUT}  耗时 {time.time()-t0:.0f}s", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
