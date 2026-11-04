"""weight_p04_constrained.py —— P0-4 正式方案对照（**加硬约束**）。

上一版 weight_p04_realloc.py 的致命缺陷（自查发现）：
    A_size_growth 把 score_size_growth 从 0.20 加到 0.50 —— 直接违反
    项目 v4.15 起立下的硬约束「单因子 ≤ 0.20」。
    更严重的是它恰好是「ΔN10 最好(+1.19pp)」的那一档 →
    如果照它改，等于用一个**违规方案**去替换合规方案。

    ⇒ 违规方案的高收益不能算数。必须回到约束里重算。

硬约束（factor_weights.json 明写，禁破）：
    · 单因子 ≤ 0.20
    · 同大类合计 ≤ 0.30
    · 权重 ≥ 0，Σ = 1.0
    · 大类划分取自 apply_round108.py: GROUPS_AFTER（v4.17 现行）

本脚本：
    只用 5 个**手工设计、全部逐条校验约束**的方案，与现行 v4.17 对照。
    不做权重搜索 —— 搜索会在同一段历史上挑出最好看的一组，属自欺。

⚠️ 长面板 fund/dominant 覆盖 0/1653（生产面板分别 3.53% / 25.48%）。
   因此本表所有方案都只能在「不含 fund/dominant」的口径下比较，
   对生产的映射须单列披露，不能直接当生产收益。
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
OUT = os.path.join(HERE, "data", "weight_p04_constrained.json")

# round125: dim_max 改为**单一来源**（原为硬编码，且漏了 score_fund: 5.0）。
#   铁律：量程只能来自 factor_weights.json；本文件用到的维度在此显式声明，
#   缺失由 dim_cfg 直接报错而非静默按 10.0 处理。
from dim_cfg import load_dim_max
DIM_MAX = load_dim_max(['score_bias60', 'score_rsi_rev', 'score_lowvol20', 'score_tech_neg', 'score_size_growth', 'score_ep', 'score_quality', 'score_growth'], strict=True)
GROUPS = {                                    # apply_round108.py: GROUPS_AFTER
    "基本面": ["score_fund", "score_quality"],
    "估值": ["score_ep"],
    "低波": ["score_lowvol20"],
    "技术反转": ["score_bias60", "score_rsi_rev", "score_tech_neg"],
    "市值": ["score_size_growth", "score_dominant"],
    "业绩动量": ["score_growth"],
}
SINGLE_CAP = 0.20
GROUP_CAP = 0.30

MIN_CS = 200
MIN_AMOUNT = 2000.0
H = 20
NS = (6, 10, 13, 30, 50)


def check(w):
    """逐条校验硬约束，返回 (ok, 违规列表, 各大类合计)。"""
    bad = []
    s = sum(w.values())
    if abs(s - 1.0) > 1e-6:
        bad.append(f"Σ={s:.4f} ≠ 1.0")
    for d, v in w.items():
        if v < -1e-9:
            bad.append(f"{d} 负权重 {v}")
        if v > SINGLE_CAP + 1e-9:
            bad.append(f"{d}={v:.3f} > 单因子上限 {SINGLE_CAP}")
    gs = {}
    for g, ks in GROUPS.items():
        t = sum(w.get(k, 0.0) for k in ks)
        gs[g] = round(t, 4)
        if t > GROUP_CAP + 1e-9:
            bad.append(f"大类[{g}]={t:.3f} > 上限 {GROUP_CAP}")
    return (not bad), bad, gs


ALL_DIMS = [d for ks in GROUPS.values() for d in ks]

SCHEMES = {}

# ── C0：现行 v4.17（含 fund .20 / dominant .10，长面板无数据）──────────
SCHEMES["C0_v417_asis"] = {
    "note": "现行 v4.17-cycle-rules 原样（fund/dominant 在长面板 0 覆盖 → 实际生效 0.70）",
    "w": {"score_fund": 0.20, "score_quality": 0.10, "score_ep": 0.10,
          "score_lowvol20": 0.0, "score_bias60": 0.05, "score_rsi_rev": 0.05,
          "score_tech_neg": 0.10, "score_size_growth": 0.20,
          "score_dominant": 0.10, "score_growth": 0.10},
}

# ── C1：最小改动 —— 只把 tech_neg（唯一有负向证据）的 0.10 移给 growth ──
SCHEMES["C1_min_techneg_to_growth"] = {
    "note": "仅 tech_neg .10→0，等额移给 growth .10→.20；fund/dominant 保持原样（承认长面板测不了）",
    "w": {"score_fund": 0.20, "score_quality": 0.10, "score_ep": 0.10,
          "score_lowvol20": 0.0, "score_bias60": 0.05, "score_rsi_rev": 0.05,
          "score_tech_neg": 0.0, "score_size_growth": 0.20,
          "score_dominant": 0.10, "score_growth": 0.20},
}

# ── C2：删 tech_neg + 把 fund/dominant 的 0.30 摊给「有数据的维」受限版 ──
# Σ 自查：quality .20 + ep .20 + lowvol .10 + bias60 .05 + rsi_rev .05
#         + size_growth .20 + growth .20 = 1.00
SCHEMES["C2_drop_dead_quality_ep"] = {
    "note": "删 tech_neg 与 fund/dominant；quality .20 / ep .20（基本面+估值各到单维上限）；lowvol20 .10；反转 .10；size_growth .20；growth .20",
    "w": {"score_fund": 0.0, "score_quality": 0.20, "score_ep": 0.20,
          "score_lowvol20": 0.10, "score_bias60": 0.05, "score_rsi_rev": 0.05,
          "score_tech_neg": 0.0, "score_size_growth": 0.20,
          "score_dominant": 0.0, "score_growth": 0.20},
}

# ── C3：反转簇吃满大类上限 + lowvol20 进 0.20（IC 最强两维的口径倾斜）──
# Σ 自查：quality .10 + ep .05 + lowvol .20 + bias60 .15 + rsi_rev .15
#         + size_growth .20 + growth .15 = 1.00
SCHEMES["C3_reversal_full_lowvol"] = {
    "note": "删 fund/dominant/tech_neg；技术反转簇 bias60 .15 + rsi_rev .15（=大类上限 .30）；lowvol20 .20；size_growth .20；quality .10；ep .05；growth .15",
    "w": {"score_fund": 0.0, "score_quality": 0.10, "score_ep": 0.05,
          "score_lowvol20": 0.20, "score_bias60": 0.15, "score_rsi_rev": 0.15,
          "score_tech_neg": 0.0, "score_size_growth": 0.20,
          "score_dominant": 0.0, "score_growth": 0.15},
}

# ── C4：均衡版（8 维都在约束内摊开，lowvol20 与反转簇各让一步）──────
# Σ 自查：quality .15 + ep .20 + lowvol .15 + bias60 .10 + rsi_rev .10
#         + size_growth .20 + growth .10 = 1.00
SCHEMES["C4_balanced"] = {
    "note": "删 fund/dominant/tech_neg；size_growth .20 / quality .15 / ep .20 / lowvol20 .15 / bias60 .10 / rsi_rev .10 / growth .10",
    "w": {"score_fund": 0.0, "score_quality": 0.15, "score_ep": 0.20,
          "score_lowvol20": 0.15, "score_bias60": 0.10, "score_rsi_rev": 0.10,
          "score_tech_neg": 0.0, "score_size_growth": 0.20,
          "score_dominant": 0.0, "score_growth": 0.10},
}

# ── C5：把 fund/dominant 的 0.30 摊到「基本面+业绩动量+低波」，不给反转 ──
# Σ 自查：quality .20 + ep .10 + lowvol .10 + bias60 .10 + rsi_rev .10
#         + size_growth .20 + growth .20 = 1.00
SCHEMES["C5_mkt_fundamental"] = {
    "note": "删 fund/dominant/tech_neg；quality .20 / ep .10 / lowvol20 .10 / 反转 .20 / size_growth .20 / growth .20（不加大反转簇）",
    "w": {"score_fund": 0.0, "score_quality": 0.20, "score_ep": 0.10,
          "score_lowvol20": 0.10, "score_bias60": 0.10, "score_rsi_rev": 0.10,
          "score_tech_neg": 0.0, "score_size_growth": 0.20,
          "score_dominant": 0.0, "score_growth": 0.20},
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
    print("=== 约束校验（先校验再算，违规的直接拒绝）===", flush=True)
    ok_schemes = {}
    for name, sc in SCHEMES.items():
        ok, bad, gs = check(sc["w"])
        print(f"  {name:<32} Σ={sum(sc['w'].values()):.3f} "
              f"{'✅ 合规' if ok else '❌ ' + '; '.join(bad)}", flush=True)
        print(f"      大类: " + "  ".join(f"{g}={v:.2f}" for g, v in gs.items()), flush=True)
        if ok:
            ok_schemes[name] = sc
    if len(ok_schemes) < len(SCHEMES):
        print(f"  ⚠️ {len(SCHEMES)-len(ok_schemes)} 个方案违规被剔除", flush=True)

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
    for name, sc in ok_schemes.items():
        v = np.zeros(len(dims), dtype=np.float64)
        for i, d in enumerate(dims):
            v[i] = sc["w"].get(d, 0.0) / DIM_MAX.get(d, 10.0)
        wvec[name] = v

    acc = {n: {t: [] for t in NS} for n in ok_schemes}
    ic_acc = {n: [] for n in ok_schemes}
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
        if n % 400 == 0:
            print(f"  {n}/{len(pdates)}", flush=True)

    res = {}
    for name in ok_schemes:
        _, _, gs = check(ok_schemes[name]["w"])
        r = {"note": ok_schemes[name]["note"],
             "weights": ok_schemes[name]["w"],
             "groups": gs,
             "ic_mean": round(float(np.mean(ic_acc[name])), 4),
             "ic_nw_t": round(nw_t(ic_acc[name], H - 1), 2)}
        for t in NS:
            xs = acc[name][t]
            a = np.asarray(xs, dtype=np.float64)
            mu, sd = float(a.mean()), float(a.std(ddof=1))
            r[f"N{t}"] = {
                "mean_pct": round(mu * 100, 3),
                "ir": round(mu / sd, 3) if sd > 0 else None,
                "nw_t": round(nw_t(xs, H - 1), 2),
                "win_rate": round(float((a > 0).mean()), 3),
            }
        res[name] = r

    print(f"\n=== 合规方案对照（h=20, liq, {nd} 截面, 未扣成本）===", flush=True)
    print(f"{'scheme':<32}{'IC':>8}{'ICt':>7}" +
          "".join(f"{'N'+str(t):>12}{'t':>7}" for t in NS), flush=True)
    for name, r in res.items():
        row = f"{name:<32}{r['ic_mean']:>+8.4f}{r['ic_nw_t']:>7}"
        for t in NS:
            s = r[f"N{t}"]
            row += f"{s['mean_pct']:>+11.2f}%{s['nw_t']:>7}"
        print(row, flush=True)

    print("\n=== 相对 C0（现行）的增量 ===", flush=True)
    b = res["C0_v417_asis"]
    for name, r in res.items():
        if name == "C0_v417_asis":
            continue
        ds = "  ".join(f"ΔN{t}={r[f'N{t}']['mean_pct']-b[f'N{t}']['mean_pct']:+.3f}pp"
                       for t in NS)
        print(f"  {name:<32} ΔIC={r['ic_mean']-b['ic_mean']:+.4f}  {ds}", flush=True)

    with open(OUT, "w", encoding="utf-8") as f:
        json.dump({
            "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "horizon": H, "n_dates": nd,
            "min_cross_section": MIN_CS, "min_amount_wan": MIN_AMOUNT,
            "holdings_tested": list(NS),
            "constraints": {"single_cap": SINGLE_CAP, "group_cap": GROUP_CAP,
                            "groups": GROUPS,
                            "source": "factor_weights.json（v4.15 起）+ apply_round108.py GROUPS_AFTER"},
            "dim_max": DIM_MAX,
            "results": res,
            "caveat": ("⚠️ (1) 只做 5 个手工方案，不做权重搜索 —— 搜索会在同一段历史挑出最好看的一组。"
                       "(2) 长面板 fund/dominant 覆盖 0/1653，故本表全部方案都在「不含这两维」的口径下比较，"
                       "不能直接映射为生产收益（生产面板 fund 3.53% / dominant 25.48% 部分覆盖）。"
                       "(3) 仍非样本外，n_eff≈82，N 之间高度相关不构成独立检验。"
                       "(4) 结论只能回答「方向」，不得作为收益承诺，也不得表述为『经验证的 alpha』。"),
        }, f, ensure_ascii=False, indent=1)
    print(f"\n写出 {OUT}  耗时 {time.time()-t0:.0f}s", flush=True)


if __name__ == "__main__":
    raise SystemExit(main())
