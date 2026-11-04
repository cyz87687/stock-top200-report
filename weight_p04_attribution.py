"""weight_p04_attribution.py —— C1 归因：提升来自「删 tech_neg」还是「加 growth」？

为什么必须归因（否则会得出错误结论）：
    weight_p04_constrained.py 里 C1（tech_neg .10 → growth .10）在**所有**持仓规模
    上都优于现行 C0（ΔN6 +1.13pp / ΔN10 +0.67pp / ΔN13 +0.66pp）。
    但 C1 同时改了两件事：
        ① 把 tech_neg 权重清零
        ② 把 growth 权重从 .10 提到 .20
    只看 C1 无法区分是哪一件起了作用。若提升其实来自 ②，那「删 tech_neg」这个
    结论就是错的；反之亦然。

    而且 tech_neg 的 IC = −0.0069 / NW t = −0.67 —— **与 0 无法区分**。
    用「点估计为负」去删一个维度，是在读噪声。所以必须把两半拆开实测。

本脚本用同一 0.10 额度的**去向对照**做纯单变量归因：
    起点统一为 C0 的「可算部分」并把 tech_neg 记 0.10：
        Tok_quality / Tok_ep / Tok_growth / Tok_reversal / Tok_lowvol / Tok_keep(tech_neg)
    每组只改「tech_neg 的 0.10 去哪」，其余完全相同 → 差异只能来自去向。
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
OUT = os.path.join(HERE, "data", "weight_p04_attribution.json")

# round125: dim_max 改为**单一来源**（原为硬编码，且漏了 score_fund: 5.0）。
#   铁律：量程只能来自 factor_weights.json；本文件用到的维度在此显式声明，
#   缺失由 dim_cfg 直接报错而非静默按 10.0 处理。
from dim_cfg import load_dim_max
DIM_MAX = load_dim_max(['score_bias60', 'score_rsi_rev', 'score_lowvol20', 'score_tech_neg', 'score_size_growth', 'score_ep', 'score_quality', 'score_growth'], strict=True)
MIN_CS = 200
MIN_AMOUNT = 2000.0
H = 20
NS = (6, 10, 13, 30)

# 共同基底（fund .20 / dominant .10 在长面板无数据，不参与）
BASE = {"score_size_growth": 0.20, "score_quality": 0.10, "score_ep": 0.10,
        "score_growth": 0.10, "score_bias60": 0.05, "score_rsi_rev": 0.05}
FREED = 0.10        # tech_neg 的额度

DEST = {
    "T0_keep_tech_neg": ("tech_neg", "保持原样：tech_neg 拿 0.10（= C0 基线）"),
    "T1_to_growth":     ("growth", "给 growth（= C1 方案）"),
    "T2_to_quality":    ("quality", "给 quality"),
    "T3_to_ep":         ("ep", "给 ep"),
    "T4_to_bias60":     ("bias60", "给 bias60（反转簇内）"),
    "T5_to_rsi_rev":    ("rsi_rev", "给 rsi_rev（反转簇内）"),
    "T6_to_lowvol":     ("lowvol", "给 lowvol20"),
    "T7_to_sizeg":      ("size_growth", "给 size_growth"),
}


def build(dest_key):
    w = dict(BASE)
    if dest_key == "tech_neg":
        w["score_tech_neg"] = FREED
    else:
        k = "score_" + dest_key
        w[k] = round(w.get(k, 0.0) + FREED, 6)
    return w


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
    for name, (dk, _) in DEST.items():
        w = build(dk)
        v = np.zeros(len(dims), dtype=np.float64)
        for i, d in enumerate(dims):
            v[i] = w.get(d, 0.0) / DIM_MAX.get(d, 10.0)
        wvec[name] = v
        print(f"  {name:<20} Σ={sum(w.values()):.2f}  {w}", flush=True)

    acc = {n: {t: [] for t in NS} for n in DEST}
    ic_acc = {n: [] for n in DEST}
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
            ic = spearman(fv, rv)
            if ic is not None:
                ic_acc[name].append(ic)
            base = float(rv.mean())
            o = np.argsort(-fv)
            for t in NS:
                if len(o) < t:
                    continue
                acc[name][t].append(float(rv[o[:t]].mean()) - base)
        if n % 500 == 0:
            print(f"  {n}/{len(pdates)}", flush=True)

    res = {}
    for name in DEST:
        r = {"label": DEST[name][1], "weights": build(DEST[name][0]),
             "ic_mean": round(float(np.mean(ic_acc[name])), 4),
             "ic_nw_t": round(nw_t(ic_acc[name], H - 1), 2)}
        for t in NS:
            xs = acc[name][t]
            a = np.asarray(xs, dtype=np.float64)
            mu, sd = float(a.mean()), float(a.std(ddof=1))
            r[f"N{t}"] = {"mean_pct": round(mu * 100, 3),
                          "ir": round(mu / sd, 3) if sd > 0 else None,
                          "nw_t": round(nw_t(xs, H - 1), 2),
                          "win_rate": round(float((a > 0).mean()), 3)}
        res[name] = r

    print(f"\n=== 0.10 额度去向对照（h=20, liq, {nd} 截面, 未扣成本）===", flush=True)
    print(f"{'dest':<20}{'IC':>8}{'ICt':>7}" +
          "".join(f"{'N'+str(t):>12}{'t':>7}" for t in NS), flush=True)
    for name, r in res.items():
        row = f"{name:<20}{r['ic_mean']:>+8.4f}{r['ic_nw_t']:>7}"
        for t in NS:
            s = r[f"N{t}"]
            row += f"{s['mean_pct']:>+11.2f}%{s['nw_t']:>7}"
        print(row, flush=True)

    b = res["T0_keep_tech_neg"]
    print("\n=== 相对 T0（tech_neg 保留）===", flush=True)
    for name, r in res.items():
        if name == "T0_keep_tech_neg":
            continue
        ds = "  ".join(f"ΔN{t}={r[f'N{t}']['mean_pct']-b[f'N{t}']['mean_pct']:+.3f}pp"
                       for t in NS)
        print(f"  {name:<20} ΔIC={r['ic_mean']-b['ic_mean']:+.4f}  {ds}", flush=True)

    print("\n=== 归因裁决 ===", flush=True)
    best = max((n for n in res if n != "T0_keep_tech_neg"),
               key=lambda n: res[n]["N10"]["mean_pct"])
    print(f"  N10 最优去向 = {best}（{res[best]['N10']['mean_pct']:+.2f}% vs "
          f"T0 {b['N10']['mean_pct']:+.2f}%）", flush=True)
    allpos = all(res[n]["N6"]["mean_pct"] > b["N6"]["mean_pct"] and
                 res[n]["N10"]["mean_pct"] > b["N10"]["mean_pct"]
                 for n in res if n != "T0_keep_tech_neg")
    print(f"  是否所有去向都优于 T0？{'是 → 说明提升来自「删 tech_neg」本身' if allpos else '否 → 提升取决于去哪，不是简单的「删」'}",
          flush=True)

    with open(OUT, "w", encoding="utf-8") as f:
        json.dump({"generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                   "horizon": H, "n_dates": nd,
                   "freed_weight": FREED, "base": BASE,
                   "min_cross_section": MIN_CS, "min_amount_wan": MIN_AMOUNT,
                   "results": res,
                   "caveat": ("⚠️ 纯单变量归因：每组只改 tech_neg 的 0.10 去向，其余固定。"
                              "但 tech_neg 自身 IC=−0.0069 / NW t=−0.67 与 0 无法区分，"
                              "因此「删它更好」不等于「它有害」，也可能只是「这 0.10 放别处更有用」。"
                              "非样本外，n_eff≈82。")}, f, ensure_ascii=False, indent=1)
    print(f"\n写出 {OUT}  耗时 {time.time()-t0:.0f}s", flush=True)


if __name__ == "__main__":
    raise SystemExit(main())
