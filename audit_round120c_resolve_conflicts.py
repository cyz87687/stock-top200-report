#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
audit_round120c_resolve_conflicts.py —— 解开 round120b 的三个矛盾

═══ 必须解开的三个矛盾 ═══
M1 逐股口径 vs 按日配对口径方向相反（T1：创新低组均值 +1.23 但配对差 −0.615）
   → 怀疑：创新低样本在时间上高度集中在市场底部，"日期构成"污染了逐股均值
M2 均值不单调但中位数完美单调（T4）
   → 怀疑：右尾（涨停/妖股）拉高了均值，均值不代表典型股票
M3 全池不筛RSI(+0.420) 竟优于 RSI<30(+0.382)
   → 怀疑：同理，全池含大量涨停股，按日等权均值被右尾主导

═══ 检验设计 ═══
所有比较统一用三个口径并列，以「按日中位数」为准（同时控日期效应与右尾）：
  口径1 raw_mean        逐股均值      —— 会被日期构成 + 右尾污染
  口径2 daily_ew_mean   按日等权均值  —— 控日期构成，仍被右尾污染
  口径3 daily_median    按日中位数    —— 两个都控，**这才是选股可比的**

另加 M1 的直接验证：创新低样本的日期分布是否集中在少数暴跌日。

输出：data/audit_round120c_resolve.json
"""
import os
import sys
import json
import math
import argparse
from collections import defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "data", "audit_round120c_resolve.json")


def median(xs):
    xs = [x for x in xs if x is not None]
    if not xs:
        return None
    s = sorted(xs)
    n = len(s)
    return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2.0


def mean(xs):
    xs = [x for x in xs if x is not None]
    return sum(xs) / len(xs) if xs else None


def tstat(xs):
    xs = [x for x in xs if x is not None]
    n = len(xs)
    if n < 3:
        return None, n
    m = sum(xs) / n
    var = sum((x - m) ** 2 for x in xs) / (n - 1)
    if var <= 0:
        return None, n
    return m / math.sqrt(var / n), n


def rsi_series(closes, period=14):
    n = len(closes)
    out = [None] * n
    if n < period + 1:
        return out
    gains, losses = [], []
    for i in range(1, n):
        d = closes[i] - closes[i - 1]
        gains.append(max(d, 0.0))
        losses.append(max(-d, 0.0))
    ag = sum(gains[:period]) / period
    al = sum(losses[:period]) / period
    out[period] = 100.0 - 100.0 / (1.0 + (ag / al if al > 0 else 1e9))
    for i in range(period, len(gains)):
        ag = (ag * (period - 1) + gains[i]) / period
        al = (al * (period - 1) + losses[i]) / period
        out[i + 1] = 100.0 - 100.0 / (1.0 + (ag / al if al > 0 else 1e9))
    return out


def build(K, hop, step=3, min_date="2025-09-01"):
    obs = []
    for sym, bars in K.items():
        dates = sorted(bars.keys())
        if len(dates) < 80:
            continue
        c = [bars[d][1] for d in dates]
        o = [bars[d][0] for d in dates]
        h = [bars[d][2] for d in dates]
        lo = [bars[d][3] for d in dates]
        rsis = rsi_series(c, 14)
        for i in range(65, len(dates) - hop, step):
            d = dates[i]
            if d < min_date:
                continue
            r = rsis[i]
            if r is None or c[i] <= 0:
                continue
            seg_lo20 = lo[max(0, i - 19):i + 1]
            low_new = bool(lo[i] <= min(seg_lo20) + 1e-12)
            prev = c[i - 1]
            chg = (c[i] / prev - 1.0) * 100.0 if prev > 0 else None
            is_st = sym.startswith("30") or sym.startswith("68")
            lim = 19.5 if is_st else 9.5
            obs.append({
                "sym": sym, "date": d, "rsi": r,
                "fwd": (c[i + hop] / c[i] - 1.0) * 100.0,
                "low_new": low_new,
                "limit_up": bool(chg is not None and chg >= lim),
                "chg": chg,
            })
    return obs


def three_metrics(obs, mask):
    sub = [x for x in obs if x["fwd"] is not None and mask(x)]
    if not sub:
        return None
    by_day = defaultdict(list)
    for x in sub:
        by_day[x["date"]].append(x["fwd"])
    daily_mean = [mean(v) for v in by_day.values() if v]
    daily_med = [median(v) for v in by_day.values() if v]
    allr = [x["fwd"] for x in sub]
    return {
        "n": len(allr), "n_days": len(daily_mean),
        "raw_mean": round(mean(allr), 3),
        "raw_median": round(median(allr), 3),
        "daily_ew_mean": round(mean(daily_mean), 3),
        "daily_median": round(mean(daily_med), 3),
        "hit_rate": round(sum(1 for v in allr if v > 0) / len(allr), 3),
        "ex_top1pct_mean": None,
    }


def trio_paired(obs, mask_t, mask_f):
    """按日中位数配对差（最干净口径）"""
    bt, bf = defaultdict(list), defaultdict(list)
    for x in obs:
        if x["fwd"] is None:
            continue
        if mask_t(x):
            bt[x["date"]].append(x["fwd"])
        elif mask_f(x):
            bf[x["date"]].append(x["fwd"])
    common = sorted(set(bt) & set(bf))
    d_med = [median(bt[d]) - median(bf[d]) for d in common]
    d_mean = [mean(bt[d]) - mean(bf[d]) for d in common]
    t_med, n1 = tstat(d_med)
    t_mean, _ = tstat(d_mean)
    return {
        "paired_days": n1,
        "paired_median_diff": round(mean(d_med), 3) if d_med else None,
        "paired_median_t": round(t_med, 3) if t_med is not None else None,
        "paired_mean_diff": round(mean(d_mean), 3) if d_mean else None,
        "paired_mean_t": round(t_mean, 3) if t_mean is not None else None,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--hop", type=int, default=5)
    a = ap.parse_args()
    K = json.load(open(os.path.join(HERE, "data", "klines_long.json"), encoding="utf-8"))
    print(f"═══ 解开 round120b 三项矛盾 · hop={a.hop} ═══")
    obs = build(K, a.hop)
    ds = sorted({x["date"] for x in obs})
    print(f"观测 {len(obs):,} 个，{len(ds)} 个交易日（{ds[0]} ~ {ds[-1]}）\n")
    R = {"hop": a.hop, "n_obs": len(obs), "date_range": [ds[0], ds[-1]]}

    # ══ M1：创新低样本的日期集中度 ══
    print("── M1 创新低样本是否集中在少数暴跌日？──")
    low_days = defaultdict(int)
    all_days = defaultdict(int)
    for x in obs:
        all_days[x["date"]] += 1
        if x["low_new"]:
            low_days[x["date"]] += 1
    # 创新低占比最高的 10 天
    ratio = [(d, low_days[d] / all_days[d], all_days[d]) for d in all_days if all_days[d] >= 50]
    ratio.sort(key=lambda t: -t[1])
    print("  创新低占比最高的 10 个交易日：")
    print(f"    {'日期':12s} {'创新低占比':>10} {'当日样本':>8}")
    for d, rt, n in ratio[:10]:
        print(f"    {d:12s} {rt*100:>9.1f}% {n:>8}")
    top10_share = sum(low_days[d] for d, _r, _n in ratio[:10]) / max(sum(low_days.values()), 1)
    print(f"  ▶ 创新低样本总量 {sum(low_days.values()):,}，最高10天占 {top10_share*100:.1f}%")
    R["M1_date_concentration"] = {
        "total_new_low_obs": sum(low_days.values()),
        "top10_days_share_pct": round(top10_share * 100, 2),
        "top10": [{"date": d, "ratio": round(rt, 3), "n": n} for d, rt, n in ratio[:10]],
    }
    print()

    # ══ M2/M3：三口径并列对照 ══
    print("── M2/M3 三口径并列（以「按日中位数」为准）──")
    schemes = {
        "现行 RSI<30": lambda x: x["rsi"] < 30,
        "RSI<30 且不创新低": lambda x: x["rsi"] < 30 and not x["low_new"],
        "RSI<30 且创新低": lambda x: x["rsi"] < 30 and x["low_new"],
        "RSI<20": lambda x: x["rsi"] < 20,
        "RSI 30~50": lambda x: 30 <= x["rsi"] < 50,
        "RSI>=70": lambda x: x["rsi"] >= 70,
        "全池": lambda x: True,
        "全池-剔除涨停": lambda x: not x["limit_up"],
    }
    print(f"  {'方案':22s} {'n':>7} {'逐股均值':>9} {'逐股中位':>9} "
          f"{'按日等权':>9} {'按日中位':>9} {'命中率':>7}")
    print("  " + "-" * 78)
    rows = []
    for name, fn in schemes.items():
        m = three_metrics(obs, fn)
        if not m:
            continue
        m["scheme"] = name
        rows.append(m)
        print(f"  {name:22s} {m['n']:>7,} {m['raw_mean']:>9} {m['raw_median']:>9} "
              f"{m['daily_ew_mean']:>9} {m['daily_median']:>9} {m['hit_rate']:>7}")
    R["M2_three_metrics"] = rows
    print()

    # ══ 关键：按日中位数配对（前置条件到底有没有用）══
    print("── 按日中位数配对差（最干净口径）──")
    base = lambda x: x["rsi"] < 30
    print(f"  {'对比':40s} {'按日中位差':>10} {'中位t':>7} {'按日均值差':>10} {'均值t':>7} {'天数':>5}")
    print("  " + "-" * 86)
    pairs = [
        ("不创新低  vs  创新低（RSI<30内）",
         lambda x: base(x) and not x["low_new"], lambda x: base(x) and x["low_new"]),
        ("不创新低  vs  创新低（RSI<40内）",
         lambda x: x["rsi"] < 40 and not x["low_new"], lambda x: x["rsi"] < 40 and x["low_new"]),
        ("RSI<30  vs  RSI30~50",
         lambda x: x["rsi"] < 30, lambda x: 30 <= x["rsi"] < 50),
        ("RSI<30  vs  全池",
         lambda x: x["rsi"] < 30, lambda x: True),
    ]
    R["M4_paired_median"] = []
    for name, mt, mf in pairs:
        r = trio_paired(obs, mt, mf)
        r["comparison"] = name
        R["M4_paired_median"].append(r)
        def f(v, w=7):
            return "—".rjust(w) if v is None else f"{v:>{w}.3f}"
        print(f"  {name:40s} {f(r['paired_median_diff'],10)} {f(r['paired_median_t'])} "
              f"{f(r['paired_mean_diff'],10)} {f(r['paired_mean_t'])} {r['paired_days']:>5}")
    print()

    # ══ 二维：RSI深度 × 企稳状态（按日中位数）══
    print("── 二维交叉：RSI 深度 × 是否创新低（按日中位数）──")
    edges = [(0, 20), (20, 30), (30, 40), (40, 50), (50, 101)]
    print(f"  {'RSI桶':12s} {'创新低: 按日中位':>16} {'n':>7} | {'不创新低: 按日中位':>18} {'n':>7}")
    cross = []
    for lo_, hi_ in edges:
        a_m = three_metrics(obs, lambda x, l=lo_, h_=hi_: l <= x["rsi"] < h_ and x["low_new"])
        b_m = three_metrics(obs, lambda x, l=lo_, h_=hi_: l <= x["rsi"] < h_ and not x["low_new"])
        rec = {
            "bucket": f"[{lo_},{hi_})",
            "new_low": a_m, "no_new_low": b_m,
        }
        cross.append(rec)
        print(f"  {rec['bucket']:12s} {a_m['daily_median'] if a_m else '—':>16} "
              f"{a_m['n'] if a_m else 0:>7} | "
              f"{b_m['daily_median'] if b_m else '—':>18} {b_m['n'] if b_m else 0:>7}")
    R["M5_cross_2d"] = cross
    print()

    # ══ 剔除涨停后再看（M3 的直接验证）══
    print("── 剔除「当日涨停」后重算（验证右尾污染）──")
    print(f"  {'方案':22s} {'按日等权(含涨停)':>16} {'按日等权(剔涨停)':>16} {'差':>8}")
    for name in ["现行 RSI<30", "RSI<30 且不创新低", "全池"]:
        fn = schemes[name]
        m1 = three_metrics(obs, fn)
        m2 = three_metrics(obs, lambda x: fn(x) and not x["limit_up"])
        if m1 and m2:
            d = m2["daily_ew_mean"] - m1["daily_ew_mean"]
            print(f"  {name:22s} {m1['daily_ew_mean']:>16} {m2['daily_ew_mean']:>16} {d:>+8.3f}")
    print()

    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    json.dump(R, open(OUT, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"已写出 {os.path.relpath(OUT, HERE)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
