#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
audit_round114e_main_board_only.py —— 剔除 20cm 板块后，主板子样本是否成立

═══ 为什么这一步是决定性的 ═══

round114d 拆板块后出现反转（与我的假设相反，必须照实记录）：

  板块          样本占比   均值      胜率    按日聚类t   近跌停占比
  主板(10cm)     30.6%   +2.543%  59.2%   **5.16**    10.3%
  创业板(20cm)   12.9%   +0.961%  49.1%   1.16        0.0%
  科创板(20cm)   56.5%   +1.873%  62.0%   1.54        0.0%

**原以为"撬板效应"（20cm 接近跌停）是主因，数据否掉了这个假设**：
  · 20cm 板块虽占 69% 样本，但按日聚类 t 都不过 2（不显著）
  · 主板只占 30.6%，却是**唯一按日聚类显著**的板块（t=5.16）
  · 主板近跌停占比仅 10.3% → 不是撬板

→ 所以真正值得检验的是**主板子样本**：低开幅度达到主板跌停的 30%+ 时，
  开盘价相对容易成交（主板流动性好、5% 低开不罕见），这才是可执行候选。

═══ 本脚本做的检验 ═══

只保留主板（60/00 开头）+ 成交额 ≥ min_amt，重跑全套：
  M1 扩展窗口 p1 阈值（无前视）
  M2 按日聚类 t（必须 ≥2.0）
  M3 beta 中性超额（必须 t≥2.0 且 >0）
  M4 时间折半稳定
  M5 三档成本扣费（ETF 不适用，用主板 0.16% 与小市值 0.28%）
  M6 剔除最赚 5% 日 / 5% 股票
  M7 低开幅度分档单调性（若仍强单调 = 依赖极端样本）
  M8 单只股票最大贡献占比（防止个别票撑起全部收益）

判据：M1~M6 全过 AND M7 单调性温和（末档/首档 < 3 倍） AND M8 单票占比 < 15%
  全过 → 「主板低开反转」是可执行候选（仍需扩样本+分钟线）
  否则 → 记录为行为纪律，不做量化信号

输出：audit_round114e_main_board_only.json
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
OUT = os.path.join(HERE, "audit_round114e_main_board_only.json")

EXPAND_MIN_HIST = 60
COST_MAIN = 0.0016
COST_SMALL = 0.0028


def stat(v, label=""):
    v = np.asarray(v, float); v = v[np.isfinite(v)]
    if len(v) < 8:
        return None
    mu, sd = float(v.mean()), float(v.std(ddof=1))
    return {"label": label, "n": int(len(v)),
            "mean_pct": round(mu * 100, 4),
            "median_pct": round(float(np.median(v)) * 100, 4),
            "win_rate": round(float((v > 0).mean()), 4),
            "t": round(mu / (sd / math.sqrt(len(v))), 3) if sd > 0 else None}


def is_main_board(code):
    c = code.lower()
    num = c[2:] if c[:2] in ("sh", "sz", "bj") else c
    return num.startswith(("60", "00"))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--min-amt", type=float, default=3e7)
    a = ap.parse_args()

    print(f"读取 {KL} ...")
    kl = json.load(open(KL, encoding="utf-8"))
    codes_all = sorted(kl.keys())
    # 只保留主板
    codes = [c for c in codes_all if is_main_board(c)]
    print(f"  主板 {len(codes)} 只 / 全部 {len(codes_all)} 只")
    alld = sorted({d for c in codes for d in kl[c]})
    di = {d: i for i, d in enumerate(alld)}
    T, N = len(alld), len(codes)
    O = np.full((T, N), np.nan); C = np.full((T, N), np.nan)
    A = np.full((T, N), np.nan)
    for j, cd in enumerate(codes):
        for dt, v in kl[cd].items():
            i = di.get(dt)
            if i is None or not isinstance(v, (list, tuple)) or len(v) < 5:
                continue
            try:
                o_, c_, v_ = float(v[0]), float(v[1]), float(v[4])
            except (TypeError, ValueError):
                continue
            if not (c_ > 0 and o_ > 0):
                continue
            O[i, j] = o_; C[i, j] = c_; A[i, j] = v_ * vol_coef(cd) * c_
    del kl
    print(f"  {N} 只 × {T} 日")

    prev = np.vstack([np.full((1, N), np.nan), C[:-1]])
    with np.errstate(invalid="ignore", divide="ignore"):
        gap = O / prev - 1.0
        intra = C / O - 1.0

    base = (np.isfinite(gap) & np.isfinite(intra)
            & np.isfinite(A) & (A >= a.min_amt))
    # 主板跌停 −10%，剔除 gap ≤ −9.5%（保守：一律当不可成交）
    tradable = base & ~(gap <= -0.095)
    print(f"  有效 {int(base.sum()):,} → 剔除近跌停后 {int(tradable.sum()):,}")

    mkt = np.full(T, np.nan)
    for t in range(T):
        m = tradable[t]
        if m.sum() >= 30:
            mkt[t] = float(np.nanmean(intra[t][m]))

    sig = []
    thr_series = []
    for t in range(EXPAND_MIN_HIST, T - 1):
        if not np.isfinite(intra[t]).any():
            continue
        hist = gap[:t][base[:t]]
        if len(hist) < 1000:
            continue
        p1 = float(np.percentile(hist, 1))
        thr_series.append({"date": alld[t], "p1_pct": round(p1 * 100, 3)})
        m = tradable[t] & (gap[t] <= p1)
        for j in np.where(m)[0]:
            sig.append({"t": t, "j": int(j), "code": codes[j],
                        "intra": float(intra[t, j]), "gap": float(gap[t, j]),
                        "excess": (float(intra[t, j] - mkt[t])
                                   if np.isfinite(mkt[t]) else np.nan)})
    if not sig:
        print("❌ 主板无信号"); return 1
    sig_days = sorted({s["t"] for s in sig})
    print(f"  信号 {len(sig)} 样本 / {len(sig_days)} 日")

    v = np.array([s["intra"] for s in sig], float)
    byd = {}
    for s in sig:
        byd.setdefault(s["t"], []).append(s["intra"])
    dm = np.array([np.mean(x) for x in byd.values()], float)

    res = {}
    res["M0_overall"] = stat(v, "主板全部信号")
    res["M1_day_cluster"] = stat(dm, "按日聚类")
    res["M1_day_cluster"]["naive_t"] = stat(v).get("t")
    res["M1_day_cluster"]["n_days"] = len(dm)

    # M3 beta 中性
    ex = np.array([s["excess"] for s in sig], float)
    exd = {}
    for s in sig:
        if np.isfinite(s["excess"]):
            exd.setdefault(s["t"], []).append(s["excess"])
    exdm = np.array([np.mean(x) for x in exd.values()], float)
    res["M3_excess_day_cluster"] = stat(exdm, "超额·按日聚类")
    res["M3_excess_day_cluster"]["sample_level"] = stat(ex)

    # M4 折半
    half = len(sig_days) // 2
    s1d = set(sig_days[:half]); s2d = set(sig_days[half:])
    v1 = np.array([s["intra"] for s in sig if s["t"] in s1d], float)
    v2 = np.array([s["intra"] for s in sig if s["t"] in s2d], float)
    d1 = np.array([np.mean([s["intra"] for s in sig if s["t"] == t]) for t in sig_days[:half]], float)
    d2 = np.array([np.mean([s["intra"] for s in sig if s["t"] == t]) for t in sig_days[half:]], float)
    r1, r2 = stat(d1), stat(d2)
    res["M4_half_split"] = {
        "first_half": r1, "second_half": r2,
        "split_date": alld[sig_days[half]],
        "same_sign": bool(r1 and r2 and np.sign(r1["mean_pct"]) == np.sign(r2["mean_pct"])),
        "both_sig": bool(r1 and r2 and r1["t"] and r2["t"]
                         and abs(r1["t"]) >= 1.5 and abs(r2["t"]) >= 1.5),
    }

    # M5 扣费
    res["M5_after_cost"] = {
        "main_cap": {"gross_pct": round(float(v.mean()) * 100, 4),
                     "cost_pct": COST_MAIN * 100,
                     "net_pct": round(float(v.mean()) * 100 - COST_MAIN * 100, 4)},
        "small_cap": {"gross_pct": round(float(v.mean()) * 100, 4),
                      "cost_pct": COST_SMALL * 100,
                      "net_pct": round(float(v.mean()) * 100 - COST_SMALL * 100, 4)},
    }

    # M6 集中度
    ds = np.sort(dm)
    cut = max(1, int(len(ds) * 0.05))
    tr_day = ds[:-cut]
    by_stk = {}
    for s in sig:
        by_stk.setdefault(s["code"], []).append(s["intra"])
    stk_means = np.array([np.mean(x) for x in by_stk.values()], float)
    ss = np.sort(stk_means)
    cut2 = max(1, int(len(ss) * 0.05))
    tr_stk = ss[:-cut2]
    res["M6_concentration"] = {
        "day_trim5pct_mean_pct": round(float(tr_day.mean()) * 100, 4),
        "day_trim_positive": bool(tr_day.mean() > 0),
        "stock_trim5pct_mean_pct": round(float(tr_stk.mean()) * 100, 4),
        "stock_trim_positive": bool(tr_stk.mean() > 0),
        "n_stocks": int(len(stk_means)),
    }

    # M7 低开分档（主板跌停 −10%）
    ratio = np.abs(np.array([s["gap"] for s in sig], float)) / 0.10
    mono = []
    for lo, hi in [(0, 0.3), (0.3, 0.5), (0.5, 0.7), (0.7, 0.9)]:
        m = (ratio >= lo) & (ratio < hi)
        if m.sum() >= 8:
            r = stat(v[m], f"{int(lo*100)}~{int(hi*100)}%跌停幅度")
            if r:
                r["bucket"] = f"{int(lo*100)}~{int(hi*100)}%"
                mono.append(r)
    res["M7_monotonic"] = mono
    mono_ratio = (mono[-1]["mean_pct"] / mono[0]["mean_pct"]
                  if len(mono) >= 2 and mono[0]["mean_pct"] > 0 else None)

    # M8 单票最大贡献
    stk_contrib = {}
    for s in sig:
        stk_contrib[s["code"]] = stk_contrib.get(s["code"], 0) + s["intra"]
    tot = sum(stk_contrib.values())
    top_code = max(stk_contrib, key=stk_contrib.get)
    res["M8_single_stock"] = {
        "top_stock": top_code,
        "top_stock_contrib_share": round(stk_contrib[top_code] / tot, 4) if tot else None,
        "n_stocks": len(stk_contrib),
    }

    # ── 判定
    chk = {}
    chk["M1_day_cluster_t>=2"] = bool(res["M1_day_cluster"]["t"]
                                      and abs(res["M1_day_cluster"]["t"]) >= 2.0)
    chk["M3_excess_t>=2_and_pos"] = bool(
        res["M3_excess_day_cluster"]["t"]
        and abs(res["M3_excess_day_cluster"]["t"]) >= 2.0
        and res["M3_excess_day_cluster"]["mean_pct"] > 0)
    chk["M4_half_stable"] = bool(res["M4_half_split"]["same_sign"]
                                 and res["M4_half_split"]["both_sig"])
    chk["M5_net_positive"] = bool(res["M5_after_cost"]["small_cap"]["net_pct"] > 0)
    chk["M6_day_robust"] = res["M6_concentration"]["day_trim_positive"]
    chk["M6_stock_robust"] = res["M6_concentration"]["stock_trim_positive"]
    chk["M7_monotonic_tame"] = bool(mono_ratio is not None and mono_ratio < 3.0)
    chk["M8_single_stock<15pct"] = bool(
        res["M8_single_stock"]["top_stock_contrib_share"] is not None
        and abs(res["M8_single_stock"]["top_stock_contrib_share"]) < 0.15)
    n_pass = sum(1 for x in chk.values() if x)

    print(f"\n{'='*78}")
    print(f"主板信号：{len(sig)} 样本 / {len(sig_days)} 日")
    print(f"  整体     {res['M0_overall']}")
    print(f"  按日聚类 {res['M1_day_cluster']}")
    print(f"  超额聚类 {res['M3_excess_day_cluster']}")
    print(f"  折半     {res['M4_half_split']}")
    print(f"  扣费     {res['M5_after_cost']}")
    print(f"  集中度   {res['M6_concentration']}")
    print(f"  分档：")
    for r in mono:
        print(f"    {r['bucket']:<12} n={r['n']:>4} 均值 {r['mean_pct']:>+6.3f}% 胜率 {r['win_rate']*100:>5.1f}%")
    print(f"  单调比（末档/首档）= {mono_ratio}")
    print(f"  单票贡献 {res['M8_single_stock']}")
    print(f"\n检验：")
    for k, x in chk.items():
        print(f"  {'✅' if x else '❌'} {k}")
    print(f"通过 {n_pass}/8")
    print(f"{'='*78}")

    if n_pass == 8:
        verdict = ("主板子样本 8/8 全过 → 「主板低开反转」在日线口径下是**可执行候选**。"
                   f"按日聚类 t={res['M1_day_cluster']['t']}、"
                   f"beta 中性超额 {res['M3_excess_day_cluster']['mean_pct']:+.3f}% "
                   f"(t={res['M3_excess_day_cluster']['t']})、"
                   f"扣费后净 {res['M5_after_cost']['small_cap']['net_pct']:+.3f}%。"
                   "⚠️ 但仍须：①扩样本至 ≥1500 日跨牛熊 ②接入分钟线验证开盘成交与盘中滑点 "
                   "③验证「开盘集合竞价以开盘价买入」的实际成交率。在此之前**不得用于实盘**。")
    else:
        failed = [k for k, x in chk.items() if not x]
        verdict = (f"主板子样本通过 {n_pass}/8，未过：{failed} → "
                   "不足以作为可执行信号。若 M7 单调性过强或 M8 单票集中，"
                   "说明收益依赖极端/个别样本，判为伪信号。")

    print(f"\n结论：{verdict}")

    json.dump({
        "meta": {
            "purpose": "剔除 20cm 板块后检验主板低开反转是否可执行",
            "why": ("round114d 意外发现：20cm 板块按日聚类 t 均不过 2，"
                    "主板（占 30.6%）却是唯一显著板块（t=5.16），"
                    "且近跌停占比仅 10.3% → 「撬板效应」假设被数据否掉，"
                    "真正值得检验的是主板子样本"),
            "universe": f"主板（60/00 开头）{N} 只，成交额≥{a.min_amt:.0e}",
            "date_range": [alld[0], alld[-1]],
            "untradable_rule": "gap ≤ −9.5% 视为开盘接近跌停并剔除（主板跌停 −10%）",
            "threshold": "扩展窗口 p1（只用历史，最小 60 日）",
            "cost_main": COST_MAIN, "cost_small": COST_SMALL,
        },
        "n_signal_samples": len(sig),
        "n_signal_days": len(sig_days),
        "results": res,
        "checks": chk,
        "n_pass": n_pass,
        "monotonic_ratio": mono_ratio,
        "verdict": verdict,
        "must_not_claim": [
            "扩样本（≥1500 日跨牛熊）+ 分钟线验证前不得用于实盘",
            "不得声称已验证「以开盘价成交」的可行性（日线无撮合数据）",
            "不得把日内收益外推为隔夜收益（round114c 已测次日 close→close 为负）",
        ],
    }, open(OUT, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"\n✅ 已写出 {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
