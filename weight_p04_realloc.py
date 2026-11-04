"""weight_p04_realloc.py —— P0-4 决策：fund(0.20)+dominant(0.10) 的 30% 死权重摊给谁。

背景（全部为本轮实测）：
    · score_fund     名义 0.20，长面板覆盖 0/1653
    · score_dominant 名义 0.10，长面板覆盖 0/1653
    · 两者合计 0.30，**完全没有数据** → 生产靠「逐行重归一化」绕过，
      等于这 30% 名义权重从未真正参与排序，也从未被任何 IC 验证过。

    同时实测出一个反直觉事实（weight_scheme_curve.py）：
      IC 最高的 score_lowvol20 (t=4.96) 在 TopN 全区间为负超额；
      IC 最低的 score_size_growth (t=2.27) 头部超额持续为正（N10 +1.29%, t=2.10）。
    ⇒ 不能按 IC 摊权重。必须按**生产口径（N=10~13）的超额**摊。

本脚本做法：
    以 S0 为底，**保持其余维原权重不变**，把 0.30 一次性加到某一个候选维上，
    在 N=10 / N=13（生产 max_single_stock=8% → 约 13 只；分散到 6 只）处比超额。
    候选：size_growth / ep / quality / growth / bias60 / rsi_rev / lowvol20 / tech_neg，
    以及三个「分给最强两维」的组合。

⚠️ 纪律：
    · 只测算，不改 factor_weights.json
    · 同一日期域、同一流动性档、同一截面门槛
    · 这是**单变量对照**（每次只加一维），不是搜索最优组合 → 比穷举搜索的过拟合风险低
    · 仍非样本外，仍受选择偏差；只用于「方向」，不作收益预期
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
OUT = os.path.join(HERE, "data", "weight_p04_realloc.json")

# round125: dim_max 改为**单一来源**（原为硬编码，且漏了 score_fund: 5.0）。
#   铁律：量程只能来自 factor_weights.json；本文件用到的维度在此显式声明，
#   缺失由 dim_cfg 直接报错而非静默按 10.0 处理。
from dim_cfg import load_dim_max
DIM_MAX = load_dim_max(['score_bias60', 'score_rsi_rev', 'score_lowvol20', 'score_tech_neg', 'score_size_growth', 'score_ep', 'score_quality', 'score_growth'], strict=True)
MIN_CS = 200
MIN_AMOUNT = 2000.0
H = 20
NS = (6, 10, 13)          # 生产持仓规模：6 只（max_single 8% + per_parent=2）/ 10 / 13

# S0 的**可算部分**（fund/dominant 已剔除，合计 0.70）
S0_EFF = {"score_size_growth": 0.20, "score_tech_neg": 0.10, "score_quality": 0.10,
          "score_ep": 0.10, "score_growth": 0.10, "score_bias60": 0.05,
          "score_rsi_rev": 0.05}

def _base():
    return dict(S0_EFF)


SCHEMES = {}
# 基准：什么都不加（= 现行实际生效口径）
SCHEMES["B0_eff070"] = ("现行实际生效（Σ0.70，不改）", _base())

# 单变量：把 0.30 全给某一维
for _d, _label in [
    ("score_size_growth", "全给 size_growth（IC最弱但头部分最强）"),
    ("score_lowvol20",    "全给 lowvol20（IC最强但头部为负）—— 预期反例"),
    ("score_ep",          "全给 ep"),
    ("score_quality",     "全给 quality（IC≈0）"),
    ("score_growth",      "全给 growth"),
    ("score_bias60",      "全给 bias60"),
    ("score_rsi_rev",     "全给 rsi_rev"),
    ("score_tech_neg",    "全给 tech_neg（IC为负）—— 预期反例"),
]:
    w = _base()
    w[_d] = w.get(_d, 0.0) + 0.30
    SCHEMES[f"A_{_d.replace('score_','')}"] = (_label, w)

# 二维摊分
for _pair, _lbl in [
    (("score_size_growth", "score_ep"), "size_growth + ep 各 0.15"),
    (("score_size_growth", "score_bias60"), "size_growth + bias60 各 0.15"),
    (("score_size_growth", "score_quality"), "size_growth + quality 各 0.15"),
]:
    w = _base()
    for _d in _pair:
        w[_d] = w.get(_d, 0.0) + 0.15
    SCHEMES["P_" + "_".join(d.replace("score_", "") for d in _pair)] = (_lbl, w)

# 反向：把 tech_neg 一起归零，再全给 size_growth（= 删负向维 + 摊死权重）
_w = _base()
_w["score_tech_neg"] = 0.0
_w["score_size_growth"] = _w["score_size_growth"] + 0.10 + 0.30
SCHEMES["A_size_growth_plus_techneg"] = ("删 tech_neg 后 0.40 全给 size_growth", _w)


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

    wvec, notes = {}, {}
    for name, (lbl, w) in SCHEMES.items():
        v = np.zeros(len(dims), dtype=np.float64)
        for i, d in enumerate(dims):
            v[i] = w.get(d, 0.0) / DIM_MAX.get(d, 10.0)
        wvec[name], notes[name] = v, lbl

    acc = {n: {t: [] for t in NS} for n in SCHEMES}
    ic_acc = {n: [] for n in SCHEMES}
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
    for name in SCHEMES:
        r = {"label": notes[name], "weights_eff": SCHEMES[name][1],
             "ic_mean": round(float(np.mean(ic_acc[name])), 4),
             "ic_nw_t": round(nw_t(ic_acc[name], H - 1), 2)}
        for t in NS:
            xs = acc[name][t]
            if not xs:
                continue
            a = np.asarray(xs, dtype=np.float64)
            mu, sd = float(a.mean()), float(a.std(ddof=1))
            r[f"N{t}"] = {
                "mean_pct": round(mu * 100, 3),
                "ir": round(mu / sd, 3) if sd > 0 else None,
                "nw_t": round(nw_t(xs, H - 1), 2),
                "win_rate": round(float((a > 0).mean()), 3),
            }
        res[name] = r

    print(f"\n=== P0-4 单变量摊分对照（h=20, liq, {nd} 截面, 未扣成本）===", flush=True)
    print(f"{'scheme':<30}{'IC':>8}{'ICt':>7}" +
          "".join(f"{'N'+str(t):>12}{'t':>7}" for t in NS), flush=True)
    for name, r in res.items():
        row = f"{name:<30}{r['ic_mean']:>+8.4f}{r['ic_nw_t']:>7}"
        for t in NS:
            s = r.get(f"N{t}")
            row += f"{s['mean_pct']:>+11.2f}%{s['nw_t']:>7}" if s else f"{'-':>19}"
        print(row, flush=True)

    print("\n=== 相对 B0（现生效）的增量 ===", flush=True)
    for name, r in res.items():
        if name == "B0_eff070":
            continue
        d = []
        for t in NS:
            s, b = r.get(f"N{t}"), res["B0_eff070"].get(f"N{t}")
            if s and b:
                d.append(f"ΔN{t}={s['mean_pct']-b['mean_pct']:+.3f}pp")
        print(f"  {name:<30} ΔIC={r['ic_mean']-res['B0_eff070']['ic_mean']:+.4f}  "
              + "  ".join(d), flush=True)

    with open(OUT, "w", encoding="utf-8") as f:
        json.dump({
            "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "horizon": H, "n_dates": nd,
            "min_cross_section": MIN_CS, "min_amount_wan": MIN_AMOUNT,
            "holdings_tested": list(NS),
            "dim_max": DIM_MAX,
            "results": res,
            "caveat": ("⚠️ 单变量对照：每次只把 fund+dominant 的 0.30 加到一个维度，其余保持不变。"
                       "护城河是不做组合搜索——避免在 1653 个截面上穷举出「最好看」的权重。"
                       "仍非样本外；N=6/10/13 之间高度相关，不构成独立检验。"
                       "结论只能用于「30% 死权重该往哪个方向摊」，不能当收益承诺。"),
        }, f, ensure_ascii=False, indent=1)
    print(f"\n写出 {OUT}  耗时 {time.time()-t0:.0f}s", flush=True)


if __name__ == "__main__":
    raise SystemExit(main())
