#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
audit_round114c_gap_reversal_robust.py —— round114b 通过 6/6 后的最后两道致命检验

═══ 为什么不能直接采信 round114b ═══

round114b 报 t=11.79、均值 +1.96%、胜率 59.5%，六重检验 6/6 通过。
**但这个 t 值本身就是危险信号**。三个机制会让它虚高：

  ① 样本重叠（横向）：1361 个样本只来自 209 个信号日，平均每天 6.5 只。
     同一天的股票高度相关（同一波市场情绪）→ 有效独立样本 ≈ 209，不是 1361。
     按 1361 算的 t 约被放大 √(1361/209) ≈ 2.55 倍。
     → **必须按日聚类（cluster-robust）重算 t**。round113b 刚踩过同类坑（重叠窗口）。

  ② 市场 beta 冒充 alpha（最致命）：
     「低开买入，日内持有」这个动作本质是**做多 beta**。
     样本期 2025-06~2026-11 若有反弹行情，随便什么"开盘买收盘卖"都赚钱。
     必须检验：**减去当日全市场 intra 均值后，超额还在不在？**
     （这正是 round77 的教训：池内等权基准 +29.34%，分子分母都比指数好看，
      超额一律以 POOL_EW 为基准，禁归因于指数。）

  ③ 选择偏差：低开 5%+ 的股票天然是「利空股」。
     如果这些股票在后续连日下跌，那 open→close 的正收益只是
     「日内反弹、隔夜继续跌」的碎片 → 不可持续。

═══ 三道追加检验 ═══

  R1 按日聚类 t 值（cluster-robust）：以「信号日」为单位算 t
     聚类数 209，远少于样本数 1361 → t 必须重算，门槛仍 |t| ≥ 2.0

  R2 beta 中性：超额 = intra(信号股) − 当日全市场 intra 均值
     若超额不再显著 → 该信号是 beta 不是 alpha

  R3 次日延续性：信号日收盘后继续持有（close[t+1]/close[t]）
     若为负 → 「日内反弹」次日回吐，说明只是噪声波动不是真反转（可接受，
     因为策略是日内平仓）；若为负且幅度大 → 需明确策略必须收盘平仓

输出：audit_round114c_gap_reversal_robust.json
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
OUT = os.path.join(HERE, "audit_round114c_gap_reversal_robust.json")

EXPAND_MIN_HIST = 60


def stat(v, label=""):
    v = np.asarray(v, float); v = v[np.isfinite(v)]
    if len(v) < 10:
        return None
    mu, sd = float(v.mean()), float(v.std(ddof=1))
    return {"label": label, "n": int(len(v)),
            "mean_pct": round(mu * 100, 4),
            "median_pct": round(float(np.median(v)) * 100, 4),
            "win_rate": round(float((v > 0).mean()), 4),
            "std_pct": round(sd * 100, 4),
            "t": round(mu / (sd / math.sqrt(len(v))), 3) if sd > 0 else None}


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
    O = np.full((T, N), np.nan); C = np.full((T, N), np.nan)
    H = np.full((T, N), np.nan); L = np.full((T, N), np.nan)
    A = np.full((T, N), np.nan)
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
            O[i, j] = o_; C[i, j] = c_; H[i, j] = h_; L[i, j] = l_
            A[i, j] = v_ * vol_coef(cd) * c_
    del kl
    print(f"  {N} 只 × {T} 日")

    prev = np.vstack([np.full((1, N), np.nan), C[:-1]])
    with np.errstate(invalid="ignore", divide="ignore"):
        gap = O / prev - 1.0
        intra = C / O - 1.0
        nxt = np.full((T, N), np.nan)      # close[t+1]/close[t] − 1
        nxt[:-1] = C[1:] / C[:-1] - 1.0

    amt_ok = np.isfinite(A) & (A >= a.min_amt)
    base = (np.isfinite(gap) & np.isfinite(intra) & amt_ok
            & np.isfinite(O) & np.isfinite(C))
    tradable = base & ~(gap <= -0.095)

    # 当日全市场 intra 均值（等权，有效样本≥50 才算）
    mkt_intra = np.full(T, np.nan)
    for t in range(T):
        m = tradable[t]
        if m.sum() >= 50:
            mkt_intra[t] = float(np.nanmean(intra[t][m]))
    print(f"  全市场日内均值：有效 {int(np.isfinite(mkt_intra).sum())} 日，"
          f"均值 {np.nanmean(mkt_intra)*100:+.4f}%/日")

    # ── 生成信号（扩展窗口 p1，与 round114b 完全同口径）
    sig = []      # (t, j, intra, gap, excess_intra, nxt)
    for t in range(EXPAND_MIN_HIST, T - 1):
        if not np.isfinite(intra[t]).any():
            continue
        hist = gap[:t][base[:t]]
        if len(hist) < 2000:
            continue
        p1 = float(np.percentile(hist, 1))
        m = tradable[t] & (gap[t] <= p1)
        for j in np.where(m)[0]:
            sig.append({
                "t": t, "j": int(j),
                "intra": float(intra[t, j]),
                "gap": float(gap[t, j]),
                "excess": (float(intra[t, j] - mkt_intra[t])
                           if np.isfinite(mkt_intra[t]) else np.nan),
                "nxt": (float(nxt[t, j]) if np.isfinite(nxt[t, j]) else np.nan),
            })
    if not sig:
        print("❌ 无信号")
        return 1
    print(f"  信号样本 {len(sig)}，来自 {len({s['t'] for s in sig})} 个信号日")

    # ═══ R1 按日聚类 t ═══
    by_day = {}
    for s in sig:
        by_day.setdefault(s["t"], []).append(s["intra"])
    day_means = np.array([np.mean(v) for v in by_day.values()], float)
    r1 = stat(day_means, "按日聚类（每日等权）")
    # 朴素的样本级 t（错的那种）作对照
    r1["naive_sample_t"] = stat([s["intra"] for s in sig]).get("t")
    r1["n_days"] = int(len(day_means))
    r1["n_samples"] = len(sig)
    r1["t_inflation"] = (round(abs(r1["naive_sample_t"]) / abs(r1["t"]), 2)
                         if r1["t"] and r1["naive_sample_t"] else None)
    print(f"\nR1 按日聚类：{r1}")

    # ═══ R2 beta 中性 ═══
    ex = np.array([s["excess"] for s in sig], float)
    r2_sample = stat(ex, "超额（样本级）")
    ex_by_day = {}
    for s in sig:
        if np.isfinite(s["excess"]):
            ex_by_day.setdefault(s["t"], []).append(s["excess"])
    ex_day = np.array([np.mean(v) for v in ex_by_day.values()], float)
    r2_day = stat(ex_day, "超额（按日聚类）")
    r2 = {"sample_level": r2_sample, "day_level": r2_day,
          "market_intra_mean_pct": round(float(np.nanmean(mkt_intra)) * 100, 4)}
    print(f"R2 beta 中性：样本级 {r2_sample}")
    print(f"              按日聚类 {r2_day}")

    # ═══ R3 次日延续 ═══
    nx = np.array([s["nxt"] for s in sig], float)
    nx = nx[np.isfinite(nx)]
    r3 = stat(nx, "次日 close→close")
    print(f"R3 次日延续：{r3}")

    # ═══ 综合判定 ═══
    chk = {}
    chk["R1_day_cluster_t_ge_2"] = bool(r1.get("t") is not None
                                        and abs(r1["t"]) >= 2.0)
    chk["R2_excess_day_t_ge_2"] = bool(r2_day and r2_day.get("t") is not None
                                       and abs(r2_day["t"]) >= 2.0
                                       and r2_day["mean_pct"] > 0)
    # R3 不设"必须为正"：策略是日内平仓，次日回吐属正常，只做记录
    n_pass = sum(1 for k, v in chk.items() if v)

    if chk["R2_excess_day_t_ge_2"] and chk["R1_day_cluster_t_ge_2"]:
        verdict = (
            f"按日聚类 t={r1['t']}（朴素 t={r1['naive_sample_t']}，虚高 "
            f"{r1['t_inflation']} 倍），beta 中性后超额 {r2_day['mean_pct']:+.3f}% "
            f"t={r2_day['t']} 仍显著 → **该信号不是纯 beta，值得作为候选继续验证**。"
            f"但仍受限于单一样本期（{alld[0]}~{alld[-1]}，341 日）与无分钟线，"
            "**不得直接用于实盘 T0**；下一步必须扩样本到 ≥1500 日（跨牛熊）+ 接入分钟线。"
        )
    elif chk["R1_day_cluster_t_ge_2"] and not chk["R2_excess_day_t_ge_2"]:
        verdict = (
            f"按日聚类 t={r1['t']} 仍显著，但 **beta 中性后超额 {r2_day['mean_pct']:+.3f}% "
            f"t={r2_day['t']} 不显著** → 该信号的收益主要来自「做多市场 beta」"
            "（低开日大盘本身就反弹），**不是 alpha**。"
            "在牛市/反弹市它会好看，在跌市会反噬。**判为 beta 冒充 alpha，不可用于 T0**。"
        )
    else:
        verdict = (
            f"按日聚类 t={r1['t']} 已不显著（朴素 t={r1['naive_sample_t']} 是样本重叠虚高）"
            f"→ **round114b 的 6/6 通过是聚类伪影，该信号不成立**。"
            "T0 仍无可行证据。"
        )
    print(f"\n{'='*76}")
    for k, v in chk.items():
        print(f"  {'✅' if v else '❌'} {k}")
    print(f"\n结论：{verdict}")
    print(f"{'='*76}")

    json.dump({
        "meta": {
            "purpose": "对 round114b 的 6/6 通过做聚类稳健性与 beta 中性检验",
            "n_days_total": int(T), "date_range": [alld[0], alld[-1]],
            "n_signal_samples": len(sig),
            "n_signal_days": int(len({s['t'] for s in sig})),
            "why_needed": ("1361 样本仅来自 209 个信号日，同日股票高度相关；"
                           "且「低开买、收盘卖」本质做多 beta，必须减市场均值。"),
            "cluster_note": ("按日聚类 t 是正确口径（cluster = 信号日）；"
                             "朴素样本级 t 因横向相关性被高估约 √(样本数/日数) 倍。"),
            "beta_note": ("超额 = 个股 intra − 当日全市场 intra 等权均值。"
                          "这是 round77 立的规矩：超额一律以池内等权为基准。"),
        },
        "R1_day_cluster": r1,
        "R2_beta_neutral": r2,
        "R3_next_day": r3,
        "checks": chk,
        "n_pass": n_pass,
        "verdict": verdict,
        "must_not_claim": [
            "不得用朴素样本级 t 值声称显著（须用按日聚类 t）",
            "不得把 beta 收益说成选股/择时能力",
            "扩样本（≥1500 日跨牛熊）+ 分钟线验证前，不得用于实盘 T0",
        ],
    }, open(OUT, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"\n✅ 已写出 {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
