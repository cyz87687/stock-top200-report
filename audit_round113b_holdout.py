#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
audit_round113b_holdout.py —— 对 round113 的「H=20 有 5 个因子通过」做三重证伪

═══ 为什么必须做 ═══

round113 输出：H=1/2/3/5/10 **全部 0 通过**，唯独 H=20 突然 5 个通过
（mom5 / dist_ma5 / dist_ma20 / vol_ratio / turn_z）。这个「只在最长周期通过」
的模式是**典型的假阳性候选**，三条机制都会制造它：

  ① 重叠窗口高估 t 值（最致命）
     304 个日截面算 H=20 收益 → 相邻 19 个截面的收益窗口重叠 95%。
     有效独立样本 ≈ 341/20 ≈ 17 个，但 t 值按 304 个算 → **t 被放大 √(20)≈4.5 倍**。
     这是教科书级的 Newey-West 问题，round109 已踩过一次同类坑（440日样本）。

  ② 多重检验（78 次：13 因子 × 6 持有期）
     Bonferroni α = 0.05/78 = 0.00064 → 需要 |t| ≥ 3.42（而非 2.0）。
     按 2.0 判据跑 78 次检验，期望假阳性 ≈ 78 × 0.0455 ≈ 3.5 个。
     **实测 5 个通过，比随机期望的 3.5 个只多 1.5 个 → 完全在噪声范围内。**

  ③ 样本期单一（2025-06 ~ 2026-11，341 日）
     这一段是单边行情 → 做空腿（factor 高组）天然亏 → 「多低波动、空高波动」
     在下跌市必然赚钱。这是 regime 暴露，不是 alpha（round77 已定性：
     模型是防御型选股器，4/9 维涨跌市符号翻转）。

═══ 三重检验 ═══

  T1 不重叠重采样：每 H 天只取 1 个截面 → 真实独立样本的 t
  T2 Bonferroni 阈值：按 78 次检验重算显著性门槛
  T3 时间折半稳定性：前 50% / 后 50% 分别算，要求同号同显著
       （单边行情下这一关最容易崩：前半段和后半段 regime 不同）

判据：三关全过才算「真信号」；任一关失败 → 判为假阳性，不得写进 T+1 建议。

输出：audit_round113b_holdout.json
"""
import os
import sys
import json
import math
import argparse
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from kline_units import vol_coef           # noqa: E402  成交量双口径唯一来源

HERE = os.path.dirname(os.path.abspath(__file__))
KL = os.path.join(HERE, "data", "klines_long.json")
SRC = os.path.join(HERE, "audit_round113_holding_period.json")
OUT = os.path.join(HERE, "audit_round113b_holdout.json")

FEATS = ["mom1", "mom5", "rev1", "rsi14", "dist_ma5", "dist_ma20",
         "vol_ratio", "vol20", "amp", "close_pos", "turn_z", "gap", "amt_z"]
HORIZONS = [1, 2, 3, 5, 10, 20]
N_TESTS = len(FEATS) * len(HORIZONS)      # 78


def rank_avg(a):
    n = len(a)
    o = np.argsort(a, kind="mergesort")
    r = np.empty(n, float)
    r[o] = np.arange(1, n + 1, dtype=float)
    sa = a[o]
    i = 0
    while i < n:
        j = i
        while j + 1 < n and sa[j + 1] == sa[i]:
            j += 1
        if j > i:
            r[o[i:j + 1]] = (i + j) / 2.0 + 1
        i = j + 1
    return r


def spearman_col(col, y):
    m = np.isfinite(col) & np.isfinite(y)
    if m.sum() < 30:
        return np.nan
    c = col[m].copy(); yy = y[m].copy()
    rx, ry = rank_avg(c), rank_avg(yy)
    rx -= rx.mean(); ry -= ry.mean()
    sx = math.sqrt((rx ** 2).sum()); sy = math.sqrt((ry ** 2).sum())
    if sx == 0 or sy == 0:
        return np.nan
    return float((rx * ry).sum() / (sx * sy))


def rsi_wilder(close, win=14):
    n = len(close)
    out = np.full(n, np.nan)
    if n <= win:
        return out
    d = np.diff(close)
    up = np.where(d > 0, d, 0.0)
    dn = np.where(d < 0, -d, 0.0)
    au = up[:win].mean(); ad = dn[:win].mean()
    for i in range(win, n):
        if i > win:
            au = (au * (win - 1) + up[i - 1]) / win
            ad = (ad * (win - 1) + dn[i - 1]) / win
        out[i] = 100.0 if ad == 0 else 100.0 - 100.0 / (1.0 + au / ad)
    return out


def roll_mean(a, w):
    n = len(a)
    o = np.full(n, np.nan)
    for i in range(w - 1, n):
        seg = a[i - w + 1:i + 1]
        if np.isfinite(seg).sum() >= w * 0.8:
            o[i] = np.nanmean(seg)
    return o


def stat(v):
    v = np.asarray(v, float); v = v[np.isfinite(v)]
    if len(v) < 8:
        return None
    mu, sd = float(v.mean()), float(v.std(ddof=1))
    return {"ic": round(mu, 5),
            "t": round(mu / (sd / math.sqrt(len(v))), 3) if sd > 0 else None,
            "pos_rate": round(float((v > 0).mean()), 4),
            "n": int(len(v))}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--min-amt", type=float, default=3e7)
    a = ap.parse_args()

    print(f"读取 {KL} ...")
    kl = json.load(open(KL, encoding="utf-8"))
    codes = sorted(kl.keys())
    alld = sorted({d for c in codes for d in kl[c]})
    di = {d: i for i, d in enumerate(alld)}
    T, N = len(alld), len(codes)
    P = {k: np.full((T, N), np.nan) for k in ("c", "o", "h", "l", "amt")}
    for j, cd in enumerate(codes):
        for dt, v in kl[cd].items():
            i = di.get(dt)
            if i is None or not isinstance(v, (list, tuple)) or len(v) < 5:
                continue
            try:
                o_, c_, h_, l_, v_ = (float(v[0]), float(v[1]), float(v[2]),
                                      float(v[3]), float(v[4]))
            except (TypeError, ValueError):
                continue
            if not (c_ > 0 and o_ > 0):
                continue
            P["o"][i, j] = o_; P["c"][i, j] = c_; P["h"][i, j] = h_
            P["l"][i, j] = l_; P["amt"][i, j] = v_ * vol_coef(cd) * c_
    del kl
    C, O, H, L = P["c"], P["o"], P["h"], P["l"]
    print(f"  {N} 只 × {T} 日")

    F = {k: np.full((T, N), np.nan, dtype=np.float32) for k in FEATS}
    for j in range(N):
        c = C[:, j]
        if np.isfinite(c).sum() < 80:
            continue
        prev = np.concatenate([[np.nan], c[:-1]])
        with np.errstate(invalid="ignore", divide="ignore"):
            F["mom1"][:, j] = c / prev - 1.0
            F["mom5"][:, j] = c / np.concatenate([np.full(5, np.nan), c[:-5]]) - 1.0
        F["rev1"][:, j] = -F["mom1"][:, j]
        F["rsi14"][:, j] = rsi_wilder(c, 14)
        with np.errstate(invalid="ignore", divide="ignore"):
            F["dist_ma5"][:, j] = c / roll_mean(c, 5) - 1.0
            F["dist_ma20"][:, j] = c / roll_mean(c, 20) - 1.0
        amt = P["amt"][:, j]
        with np.errstate(invalid="ignore", divide="ignore"):
            F["vol_ratio"][:, j] = amt / roll_mean(amt, 20)
        lr = np.full(T, np.nan)
        with np.errstate(invalid="ignore", divide="ignore"):
            lr[1:] = np.log(c[1:] / c[:-1])
        v = np.full(T, np.nan)
        for i in range(20, T):
            seg = lr[i - 19:i + 1]
            if np.isfinite(seg).all():
                v[i] = seg.std(ddof=1)
        F["vol20"][:, j] = v
        with np.errstate(invalid="ignore", divide="ignore"):
            F["amp"][:, j] = (H[:, j] - L[:, j]) / prev
            rng = H[:, j] - L[:, j]
            F["close_pos"][:, j] = np.where(rng > 0, (c - L[:, j]) / rng, 0.5)
            F["gap"][:, j] = O[:, j] / prev - 1.0
        lamt = np.log(np.where(amt > 0, amt, np.nan))
        mu, sd = roll_mean(lamt, 60), np.full(T, np.nan)
        for i in range(59, T):
            seg = lamt[i - 59:i + 1]
            if np.isfinite(seg).all():
                sd[i] = seg.std(ddof=1)
        with np.errstate(invalid="ignore", divide="ignore"):
            F["turn_z"][:, j] = (lamt - mu) / np.where(sd > 0, sd, np.nan)
            F["amt_z"][:, j] = lamt

    RH = {}
    for h in HORIZONS:
        r = np.full((T, N), np.nan)
        if h < T - 1:
            with np.errstate(invalid="ignore", divide="ignore"):
                r[:T - h] = C[h:] / O[1:T - h + 1] - 1.0
        RH[h] = r

    # 收集每日截面 IC（供三种重采样用）
    daily = {k: {h: {} for h in HORIZONS} for k in FEATS}
    for t in range(25, T - 1):
        base_ok = (np.isfinite(C[t]) & np.isfinite(O[t + 1])
                   & np.isfinite(P["amt"][t]) & (P["amt"][t] >= a.min_amt))
        if base_ok.sum() < 100:
            continue
        Xf = np.column_stack([F[k][t] for k in FEATS]).astype(float)
        for ci, k in enumerate(FEATS):
            col = Xf[:, ci]
            m0 = np.isfinite(col)
            if m0.sum() >= 30:
                lo, hi = np.nanpercentile(col[m0], [1, 99])
                col = np.clip(col, lo, hi)
            for h in HORIZONS:
                ok = base_ok & np.isfinite(col) & np.isfinite(RH[h][t])
                if ok.sum() < 100:
                    continue
                daily[k][h][t] = spearman_col(col[ok], RH[h][t][ok])

    # ⚠️ days 取自 H=1 的全集，但长持有期（H=20）尾部若干日没有收益
    #    → daily[k][h] 里缺这些键，直接 [d] 取值会 KeyError（踩过一次）。
    #    统一走 _d() 取，缺失返回 nan。
    days = sorted(daily["mom1"][1].keys())

    def _d(k, h, d):
        return daily[k][h].get(d, np.nan)
    print(f"  日截面 {len(days)} 个")

    # ── T1 不重叠重采样：每 H 天取一个截面
    #    这是最关键的一关：重叠窗口会让 t 虚高 √H 倍
    t1 = {}
    for k in FEATS:
        t1[k] = {}
        for h in HORIZONS:
            seq = [_d(k, h, d) for d in days[::h]
                   if np.isfinite(_d(k, h, d))]
            s = stat(seq)
            full = stat([_d(k, h, d) for d in days])
            if s:
                t1[k][str(h)] = {
                    "n_nonoverlap": s["n"],
                    "ic_nonoverlap": s["ic"], "t_nonoverlap": s["t"],
                    "ic_full": full["ic"] if full else None,
                    "t_full": full["t"] if full else None,
                    "t_deflate": (round(abs(full["t"]) / abs(s["t"]), 2)
                                  if s["t"] and full and full["t"] else None),
                }

    # ── T2 Bonferroni
    #    按 78 次检验，双尾 α=0.05 → 单尾 0.025/78 = 0.00032
    #    用正态近似反解阈值：z = Φ^-1(1 − 0.00032) ≈ 3.42
    def _norm_ppf(p):
        # Acklam 近似，精度 1e-9，够用
        aa = [-3.969683028665376e+01, 2.209460984245205e+02,
              -2.759285104469687e+02, 1.383577518672690e+02,
              -3.066479806614716e+01, 2.506628277459239e+00]
        bb = [-5.447609879822406e+01, 1.615858368580409e+02,
              -1.556989798598866e+02, 6.680131188771972e+01,
              -1.328068155288572e+01]
        cc = [-7.784894002430293e-03, -3.223964580411365e-01,
              -2.400758277161838e+00, -2.549732539343734e+00,
              4.374664141464968e+00, 2.938163982698783e+00]
        dd = [7.784695709041462e-03, 3.224671290700398e-01,
              2.445134137142996e+00, 3.754408661907416e+00]
        plow, phigh = 0.02425, 1 - 0.02425
        if p < plow:
            q = math.sqrt(-2 * math.log(p))
            return (((((cc[0]*q+cc[1])*q+cc[2])*q+cc[3])*q+cc[4])*q+cc[5]) / \
                   ((((dd[0]*q+dd[1])*q+dd[2])*q+dd[3])*q+1)
        if p > phigh:
            q = math.sqrt(-2 * math.log(1 - p))
            return -(((((cc[0]*q+cc[1])*q+cc[2])*q+cc[3])*q+cc[4])*q+cc[5]) / \
                   ((((dd[0]*q+dd[1])*q+dd[2])*q+dd[3])*q+1)
        q = p - 0.5; r = q * q
        return (((((aa[0]*r+aa[1])*r+aa[2])*r+aa[3])*r+aa[4])*r+aa[5])*q / \
               (((((bb[0]*r+bb[1])*r+bb[2])*r+bb[3])*r+bb[4])*r+1)

    alpha_bonf = 0.05 / N_TESTS
    z_thr = round(_norm_ppf(1 - alpha_bonf), 3)

    # ── T3 时间折半
    half = len(days) // 2
    t3 = {}
    for k in FEATS:
        t3[k] = {}
        for h in HORIZONS:
            s1 = stat([_d(k, h, d) for d in days[:half]])
            s2 = stat([_d(k, h, d) for d in days[half:]])
            if s1 and s2:
                same_sign = (np.sign(s1["ic"]) == np.sign(s2["ic"]))
                both_sig = abs(s1["t"]) >= 2.0 and abs(s2["t"]) >= 2.0
                t3[k][str(h)] = {"h1": s1, "h2": s2,
                                 "same_sign": bool(same_sign),
                                 "both_significant": bool(both_sig)}

    # ── 综合判定
    final = {}
    for k in FEATS:
        for h in HORIZONS:
            r1 = t1.get(k, {}).get(str(h))
            r3 = t3.get(k, {}).get(str(h))
            if not r1 or not r3:
                continue
            p1 = bool(r1["t_nonoverlap"] is not None
                      and abs(r1["t_nonoverlap"]) >= z_thr)          # T1: 不重叠后仍过 Bonferroni 阈值
            p2 = bool(r1["t_full"] is not None and abs(r1["t_full"]) >= z_thr)  # T2: 原始也过 Bonferroni
            p3 = bool(r3["same_sign"] and r3["both_significant"])        # T3: 折半稳定
            final.setdefault(k, {})[str(h)] = {
                "T1_nonoverlap_sig": p1, "T2_bonferroni_sig": p2,
                "T3_half_stable": p3,
                "all_pass": bool(p1 and p2 and p3),
                "t_nonoverlap": r1["t_nonoverlap"], "t_full": r1["t_full"],
                "ic_nonoverlap": r1["ic_nonoverlap"],
                "t_deflate": r1["t_deflate"],
            }

    survivors = [(k, h) for k in final for h in final[k]
                 if final[k][h]["all_pass"]]
    # round113 原判通过的那 5 个
    r113 = json.load(open(SRC, encoding="utf-8"))
    claimed = [(k, int(h)) for k in r113.get("results", {})
               for h, v in r113["results"][k].items() if v.get("pass")]

    print(f"\n{'='*70}")
    print(f"Bonferroni 阈值（78 次检验）= |t| ≥ {z_thr}")
    print(f"round113 原判通过: {claimed}")
    print(f"\n{'因子':<11}{'H':>4}{'IC(不重叠)':>12}{'t(不重叠)':>11}"
          f"{'t(原始)':>10}{'t虚高倍':>9}  T1 T2 T3  终判")
    for k, h in claimed:
        d = final.get(k, {}).get(str(h), {})
        r1 = t1.get(k, {}).get(str(h), {})
        print(f"{k:<11}{h:>4}{r1.get('ic_nonoverlap', 0):>+12.4f}"
              f"{r1.get('t_nonoverlap') or 0:>11.2f}"
              f"{r1.get('t_full') or 0:>10.2f}"
              f"{r1.get('t_deflate') or 0:>9.2f}"
              f"   {'✓' if d.get('T1_nonoverlap_sig') else '✗'}  "
              f"{'✓' if d.get('T2_bonferroni_sig') else '✗'}  "
              f"{'✓' if d.get('T3_half_stable') else '✗'}   "
              f"{'通过' if d.get('all_pass') else '**淘汰**'}")
    print(f"\n三重检验后存活: {survivors if survivors else '零'}")
    print(f"{'='*70}")

    verdict = (
        f"round113 声称 H=20 有 {len(claimed)} 个因子通过，但那是"
        f"「重叠窗口 + 未校正多重检验 + 单一样本期」三重放大下的结果。"
        f"按 Bonferroni 阈值 |t|≥{z_thr} 重算、并以不重叠截面重估 t 值后，"
        f"**存活 {len(survivors)} 个**。"
        + ("→ 结论：全部为假阳性，日频因子在本池不存在可利用的方向信息，"
           "T+1 建议不得基于因子择时。"
           if not survivors else
           "→ 结论：「拉长持有期」仅对下列因子成立，可作为 T+N 建议的依据。")
    )
    print(f"\n{verdict}")

    json.dump({
        "meta": {
            "purpose": "对 round113「H=20 有 5 个通过」做三重证伪",
            "n_tests": N_TESTS,
            "bonferroni_alpha": alpha_bonf,
            "z_threshold": z_thr,
            "expected_false_positives_at_t2": round(N_TESTS * 0.0455, 2),
            "overlap_note": ("H 日收益在相邻日截面上重叠 (H-1)/H，"
                             "t 值被高估约 √H 倍 → 必须不重叠重估"),
            "t1": "不重叠重采样（每 H 天取 1 截面）后 |t| 仍 ≥ Bonferroni 阈值",
            "t2": "原始 |t| ≥ Bonferroni 阈值",
            "t3": "时间折半：两半 IC 同号且 |t| 均 ≥ 2.0",
        },
        "claimed_by_round113": [{"factor": k, "horizon": h} for k, h in claimed],
        "t1_nonoverlap": t1,
        "t3_half_split": t3,
        "final": final,
        "survivors": [{"factor": k, "horizon": h} for k, h in survivors],
        "verdict": verdict,
    }, open(OUT, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"\n✅ 已写出 {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
