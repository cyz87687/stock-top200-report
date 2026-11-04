#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
audit_round120b_reversal_focused.py —— 反转因子前置条件的聚焦检验

═══ 为什么要单独一个脚本 ═══
round120 测出 RSI 分档是 U 型（两端好、中间差），但 U 型本身可能来自
  · 涨跌停效应（极低 RSI 刚跌停→次日反弹；极高 RSI 刚涨停→惯性）
  · 波动率效应（两端都是高波动股，5日收益分布更宽，均值被右尾拉高）
  · 短周期噪声
⇒ 不能直接把「U 型」当成「现行单调映射错了」的证据。

本脚本要回答的**唯一问题**：
  「低 RSI」这一组内部，加入「不创新低 / 企稳 / 趋势确认」后，
   未来收益是否真的更好？改进方案能否跑赢现行方案？

═══ 四个检验 ═══
  T1 RSI<30 内部：创新低 vs 不创新低（严格互补口径，聚焦生益科技那一类）
  T2 现行方案 vs 各改进方案：组合层面的未来收益与命中率
  T3 波动率/涨跌停混淆排除：按 ATR 分档后前置条件是否仍有区分力
  T4 中位数对照：均值是否被右尾主导

纪律：只读；按日配对；重叠窗口按 hop 重采样；不套 2.0 临界值。
输出：data/audit_round120b_focused.json
"""
import os
import sys
import json
import math
import argparse
from collections import defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "data", "audit_round120b_focused.json")


def rsi_series(closes, period=14):
    """返回与 closes 等长的 RSI 序列（前 period 个为 None）。"""
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


def build_obs(K, hop, step=3, min_date="2025-09-01"):
    """构造观测：每行 = (sym, date, rsi, 形态信号, 未来hop收益, ATR%)"""
    obs = []
    n_sym = len(K)
    done = 0
    for sym, bars in K.items():
        done += 1
        if done % 500 == 0:
            print(f"    ... {done}/{n_sym}", file=sys.stderr)
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
            if r is None:
                continue
            px = c[i]
            if px <= 0:
                continue

            # 创新低（严格互补两个口径）
            seg_lo20 = lo[max(0, i - 19):i + 1]
            low_new_20 = bool(lo[i] <= min(seg_lo20) + 1e-12)
            seg_c20 = c[max(0, i - 19):i + 1]
            close_new_20 = bool(c[i] <= min(seg_c20) + 1e-12)
            seg_lo60 = lo[max(0, i - 59):i + 1]
            low_new_60 = bool(lo[i] <= min(seg_lo60) + 1e-12)

            ma5 = sum(c[i - 4:i + 1]) / 5.0 if i >= 4 else None
            ma20 = sum(c[i - 19:i + 1]) / 20.0 if i >= 19 else None
            ma5_prev = sum(c[i - 7:i - 2]) / 5.0 if i >= 7 else None

            # 涨跌停近似（主板 ±10%，创业/科创 ±20%）
            prev = c[i - 1]
            chg = (px / prev - 1.0) * 100.0 if prev > 0 else None
            is_st = sym.startswith("30") or sym.startswith("68")
            lim = 19.5 if is_st else 9.5
            at_limit_down = bool(chg is not None and chg <= -lim)
            at_limit_up = bool(chg is not None and chg >= lim)

            # ATR%（近 20 日真实波幅占价格）
            if i >= 20:
                trs = []
                for k in range(i - 19, i + 1):
                    hl = h[k] - lo[k]
                    hc = abs(h[k] - c[k - 1]) if k > 0 else hl
                    lc = abs(lo[k] - c[k - 1]) if k > 0 else hl
                    trs.append(max(hl, hc, lc))
                atr_pct = (sum(trs) / len(trs)) / px * 100.0
            else:
                atr_pct = None

            fwd = (c[i + hop] / px - 1.0) * 100.0
            obs.append({
                "sym": sym, "date": d, "rsi": r, "fwd": fwd,
                "low_new_20": low_new_20, "close_new_20": close_new_20,
                "low_new_60": low_new_60,
                "s2_ma5": bool(px > ma5) if ma5 else None,
                "s3_ma5up": bool(ma5 > ma5_prev) if (ma5 and ma5_prev) else None,
                "s4_yang": bool(c[i] > o[i]),
                "t1_ma20": bool(px > ma20) if ma20 else None,
                "atr_pct": atr_pct,
                "limit_down": at_limit_down, "limit_up": at_limit_up,
                "chg": chg,
            })
    return obs


def paired_diff(obs, mask_t, mask_f=None, min_day_n=1):
    """
    按日配对差：mask_t 组 vs 其余组（或 mask_f 组）。
    返回 (按日差均值, t, 天数, 真组均值, 假组均值, 真组n, 假组n, 真组中位, 假组中位)
    """
    bt, bf = defaultdict(list), defaultdict(list)
    for x in obs:
        if x["fwd"] is None:
            continue
        if mask_t(x):
            bt[x["date"]].append(x["fwd"])
        elif mask_f is None or mask_f(x):
            bf[x["date"]].append(x["fwd"])
    common = [d for d in set(bt) & set(bf)
              if len(bt[d]) >= min_day_n and len(bf[d]) >= min_day_n]
    diffs = [mean(bt[d]) - mean(bf[d]) for d in sorted(common)]
    t, nd = tstat(diffs)
    all_t = [v for d in bt for v in bt[d]]
    all_f = [v for d in bf for v in bf[d]]
    return {
        "n_true": len(all_t), "n_false": len(all_f),
        "mean_true": round(mean(all_t), 3) if all_t else None,
        "mean_false": round(mean(all_f), 3) if all_f else None,
        "median_true": round(median(all_t), 3) if all_t else None,
        "median_false": round(median(bf), 3) if False else (
            round(median(all_f), 3) if all_f else None),
        "paired_days": nd,
        "paired_diff": round(mean(diffs), 3) if diffs else None,
        "paired_t": round(t, 3) if t is not None else None,
        "hit_true": round(sum(1 for v in all_t if v > 0) / len(all_t), 3) if all_t else None,
        "hit_false": round(sum(1 for v in all_f if v > 0) / len(all_f), 3) if all_f else None,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--hop", type=int, default=5)
    ap.add_argument("--step", type=int, default=3)
    a = ap.parse_args()

    K = json.load(open(os.path.join(HERE, "data", "klines_long.json"), encoding="utf-8"))
    print(f"═══ 反转因子聚焦检验 · hop={a.hop} · step={a.step} ═══")
    print(f"K 线库 {len(K)} 只，构造观测中...", file=sys.stderr)
    obs = build_obs(K, a.hop, a.step)
    ds = sorted({x["date"] for x in obs})
    print(f"观测 {len(obs):,} 个，{len(ds)} 个交易日（{ds[0]} ~ {ds[-1]}）\n")

    R = {"hop": a.hop, "n_obs": len(obs), "date_range": [ds[0], ds[-1]]}

    # ══ T1：RSI<30 内部，创新低 vs 不创新低 ══
    print("── T1  RSI<30 内部：创新低 vs 不创新低（严格互补）──")
    base30 = lambda x: x["rsi"] < 30
    print(f"  RSI<30 总样本: {sum(1 for x in obs if base30(x) and x['fwd'] is not None):,}")
    rows = []
    for name, mt, mf in [
        ("当日最低创20日新低", lambda x: base30(x) and x["low_new_20"],
         lambda x: base30(x) and not x["low_new_20"]),
        ("收盘创20日新低", lambda x: base30(x) and x["close_new_20"],
         lambda x: base30(x) and not x["close_new_20"]),
        ("当日最低创60日新低", lambda x: base30(x) and x["low_new_60"],
         lambda x: base30(x) and not x["low_new_60"]),
    ]:
        r = paired_diff(obs, mt, mf)
        r["name"] = name
        rows.append(r)
        print(f"  {name:22s} 真n={r['n_true']:>6,} 假n={r['n_false']:>6,} "
              f"均值 真{r['mean_true']:>7} 假{r['mean_false']:>7} "
              f"中位 真{r['median_true']:>7} 假{r['median_false']:>7} "
              f"按日差{r['paired_diff']:>+7} t={r['paired_t']} 命中率 真{r['hit_true']} 假{r['hit_false']}")
    R["T1"] = rows
    print()

    # ══ T2：现行方案 vs 改进方案（组合层）══
    print("── T2  现行方案 vs 改进方案（组合层面，等权买入）──")
    schemes = {
        "现行: RSI<30→5.0（只看RSI）": lambda x: x["rsi"] < 30,
        "改A: RSI<30 且 不创20日新低": lambda x: x["rsi"] < 30 and not x["low_new_20"],
        "改B: RSI<30 且 收盘不创20日新低": lambda x: x["rsi"] < 30 and not x["close_new_20"],
        "改C: RSI<30 且 不创新低 且 站上MA5": lambda x: (x["rsi"] < 30
                                                    and not x["low_new_20"]
                                                    and x["s2_ma5"] is True),
        "改D: RSI<30 且 不创新低 且 站上MA20": lambda x: (x["rsi"] < 30
                                                     and not x["low_new_20"]
                                                     and x["t1_ma20"] is True),
        "对照: 全池(不筛RSI)": lambda x: True,
        "对照: RSI 30~50（中间档）": lambda x: 30 <= x["rsi"] < 50,
    }
    rows2 = []
    for name, fn in schemes.items():
        by_day = defaultdict(list)
        for x in obs:
            if x["fwd"] is not None and fn(x):
                by_day[x["date"]].append(x["fwd"])
        daily = [mean(v) for v in by_day.values() if v]
        # 每日平均持仓数
        n_per_day = [len(v) for v in by_day.values() if v]
        t, nd = tstat(daily)
        allr = [v for vv in by_day.values() for v in vv]
        rows2.append({
            "scheme": name,
            "n_obs": len(allr),
            "n_days": nd,
            "avg_holdings_per_day": round(mean(n_per_day), 1) if n_per_day else None,
            "daily_ew_mean_pct": round(mean(daily), 3) if daily else None,
            "raw_mean_pct": round(mean(allr), 3) if allr else None,
            "raw_median_pct": round(median(allr), 3) if allr else None,
            "hit_rate": round(sum(1 for v in allr if v > 0) / len(allr), 3) if allr else None,
            "t": round(t, 3) if t is not None else None,
        })
        rr = rows2[-1]
        def f(v, w=7, d=3):
            return "—".rjust(w) if v is None else f"{v:>{w}.{d}f}"
        print(f"  {name:34s} n={rr['n_obs']:>7,} 日均持仓={f(rr['avg_holdings_per_day'],6,1)} "
              f"按日等权={f(rr['daily_ew_mean_pct'])} "
              f"中位={f(rr['raw_median_pct'])} 命中={f(rr['hit_rate'],5,3)} t={f(rr['t'],6,3)}")
    R["T2"] = rows2

    # 相对现行方案的增量（按日配对）
    print("\n  相对「现行方案」的按日配对增量:")
    cur = "现行: RSI<30→5.0（只看RSI）"
    cur_mask = schemes[cur]
    R["T2_vs_current"] = []
    for name, fn in schemes.items():
        if name == cur:
            continue
        r = paired_diff(obs, fn, cur_mask)
        print(f"    {name:34s} 按日差={r['paired_diff'] if r['paired_diff'] is not None else '—':>7} "
              f"t={r['paired_t'] if r['paired_t'] is not None else '—':>7} "
              f"天数={r['paired_days']}")
        R["T2_vs_current"].append({"scheme": name, **r})
    print()

    # ══ T3：涨跌停 / 波动率混淆排除 ══
    print("── T3  混淆排除 ──")
    lim_dn = [x for x in obs if x["limit_down"] and x["fwd"] is not None]
    lim_up = [x for x in obs if x["limit_up"] and x["fwd"] is not None]
    print(f"  跌停样本 {len(lim_dn):,}  未来{a.hop}日均值={mean([x['fwd'] for x in lim_dn])}")
    print(f"  涨停样本 {len(lim_up):,}  未来{a.hop}日均值={mean([x['fwd'] for x in lim_up])}")
    R["T3_limit"] = {
        "limit_down_n": len(lim_dn),
        "limit_down_fwd_mean": round(mean([x['fwd'] for x in lim_dn]), 3) if lim_dn else None,
        "limit_up_n": len(lim_up),
        "limit_up_fwd_mean": round(mean([x['fwd'] for x in lim_up]), 3) if lim_up else None,
    }

    # 按 ATR 三分档后看「创新低与否」是否仍有区分力
    atrs = sorted([x["atr_pct"] for x in obs if x["atr_pct"] is not None and x["fwd"] is not None])
    if atrs:
        q1 = atrs[len(atrs) // 3]
        q2 = atrs[2 * len(atrs) // 3]
        print(f"  ATR% 三分档: 低<{q1:.2f}  中{q1:.2f}~{q2:.2f}  高>{q2:.2f}")
        R["T3_atr_terciles"] = {"q1": round(q1, 3), "q2": round(q2, 3)}
        R["T3_atr_groups"] = []
        for label, cond in [
            ("低波", lambda x: x["atr_pct"] < q1),
            ("中波", lambda x: q1 <= x["atr_pct"] < q2),
            ("高波", lambda x: x["atr_pct"] >= q2),
        ]:
            sub = [x for x in obs if cond(x) and x["fwd"] is not None and x["rsi"] < 40]
            r = paired_diff(sub, lambda x: not x["low_new_20"], lambda x: x["low_new_20"])
            print(f"    {label} RSI<40内「不创新低 vs 创新低」: 真n={r['n_true']:>6,} "
                  f"假n={r['n_false']:>6,} 按日差={r['paired_diff']:>+7} t={r['paired_t']}")
            R["T3_atr_groups"].append({"atr_group": label, **r})
    print()

    # ══ T4：分档明细（含中位数，排除右尾主导）══
    print("── T4  RSI 分档：均值 vs 中位数（排除右尾主导）──")
    edges = [(0, 20), (20, 30), (30, 40), (40, 50), (50, 60), (60, 70), (70, 101)]
    R["T4_buckets"] = []
    print(f"  {'桶':12s} {'n':>7} {'天数':>5} {'按日等权':>9} {'逐股均值':>9} {'逐股中位':>9} {'命中率':>7}")
    for lo_, hi_ in edges:
        sub = [x for x in obs if x["fwd"] is not None and lo_ <= x["rsi"] < hi_]
        by_day = defaultdict(list)
        for x in sub:
            by_day[x["date"]].append(x["fwd"])
        daily = [mean(v) for v in by_day.values() if v]
        allr = [x["fwd"] for x in sub]
        rec = {
            "bucket": f"[{lo_},{hi_})", "n": len(allr), "n_days": len(daily),
            "daily_ew_mean": round(mean(daily), 3) if daily else None,
            "raw_mean": round(mean(allr), 3) if allr else None,
            "raw_median": round(median(allr), 3) if allr else None,
            "hit_rate": round(sum(1 for v in allr if v > 0) / len(allr), 3) if allr else None,
        }
        R["T4_buckets"].append(rec)
        print(f"  {rec['bucket']:12s} {rec['n']:>7,} {rec['n_days']:>5} "
              f"{rec['daily_ew_mean']:>9} {rec['raw_mean']:>9} {rec['raw_median']:>9} {rec['hit_rate']:>7}")
    print()

    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(R, f, ensure_ascii=False, indent=1)
    print(f"已写出 {os.path.relpath(OUT, HERE)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
