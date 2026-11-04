#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
audit_round120d_score_monotonic.py —— 反转「分数档」与未来收益的单调性（决定性检验）

═══ 这是唯一能直接回答问题的检验 ═══
前面 round120/120b/120c 都是在测「原始变量（RSI/新低）」，但模型实际用的是
**离散分数 1.0~5.0**。如果分数档与未来收益不单调、甚至反向，那就证明
「低 RSI → 高分」这个映射本身是错的，加什么前置条件都是打补丁。

═══ 三个口径并列（必须同时看）═══
  raw_mean       逐股均值     —— 被日期构成 + 右尾污染
  daily_ew_mean  按日等权均值 —— 控日期构成，仍被右尾
  daily_median   按日中位数   —— 两个都控，**选股可比的真口径**

═══ 额外交付 ═══
  · 各分数档的样本量（防止「极端档样本太少」被当成结论）
  · 分数档与新低的交叉（看两套映射是否互相矛盾）
  · 若单调性不成立，给出「按未来收益重排后的分数」对照

⚠️ 不做任何写生产数据的动作。输出 data/audit_round120d_score_monotonic.json
"""
import os
import sys
import json
import math
from collections import defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "data", "audit_round120d_score_monotonic.json")


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


# ── 与 step2_stockscorer_v2.py 完全同口径的三个映射 ──
def score_rsi_rev(rsi14):
    """现行实现（step2 L2734）"""
    if rsi14 is None:
        return 2.5
    if rsi14 < 30: return 5.0
    if rsi14 < 40: return 4.0
    if rsi14 < 50: return 3.0
    if rsi14 < 60: return 2.0
    return 1.0


def score_bias60(bias):
    if bias is None:
        return 2.5
    if bias <= -15: return 5.0
    if bias <= -10: return 4.0
    if bias <= -5: return 3.0
    if bias <= 0: return 2.0
    return 1.0


def score_oversold60(mom60):
    if mom60 is None:
        return 2.5
    if mom60 <= -30: return 5.0
    if mom60 <= -20: return 4.0
    if mom60 <= -10: return 3.0
    if mom60 <= 0: return 2.0
    return 1.0


def build(K, hop, step=3, min_date="2025-09-01"):
    obs = []
    for sym, bars in K.items():
        dates = sorted(bars.keys())
        if len(dates) < 80:
            continue
        c = [bars[d][1] for d in dates]
        lo = [bars[d][3] for d in dates]
        rsis = rsi_series(c, 14)
        for i in range(65, len(dates) - hop, step):
            d = dates[i]
            if d < min_date or c[i] <= 0:
                continue
            r = rsis[i]
            if r is None:
                continue
            ma60 = sum(c[i - 59:i + 1]) / 60.0
            bias60 = (c[i] / ma60 - 1.0) * 100.0 if ma60 > 0 else None
            mom60 = (c[i] / c[i - 60] - 1.0) * 100.0 if c[i - 60] > 0 else None
            seg_lo = lo[max(0, i - 19):i + 1]
            obs.append({
                "sym": sym, "date": d,
                "fwd": (c[i + hop] / c[i] - 1.0) * 100.0,
                "rsi": r, "bias60": bias60, "mom60": mom60,
                "s_rsi": score_rsi_rev(r),
                "s_bias": score_bias60(bias60),
                "s_os": score_oversold60(mom60),
                "low_new": bool(lo[i] <= min(seg_lo) + 1e-12),
            })
    return obs


def bucket_stats(obs, key, label):
    """按分数档统计三口径"""
    rows = []
    for sc in [1.0, 2.0, 3.0, 4.0, 5.0]:
        sub = [x for x in obs if x.get(key) == sc and x["fwd"] is not None]
        if not sub:
            rows.append({"score": sc, "n": 0})
            continue
        by_day = defaultdict(list)
        for x in sub:
            by_day[x["date"]].append(x["fwd"])
        dm = [mean(v) for v in by_day.values() if v]
        dmed = [median(v) for v in by_day.values() if v]
        allr = [x["fwd"] for x in sub]
        rows.append({
            "score": sc, "n": len(allr), "n_days": len(dm),
            "raw_mean": round(mean(allr), 3),
            "raw_median": round(median(allr), 3),
            "daily_ew_mean": round(mean(dm), 3) if dm else None,
            "daily_median": round(mean(dmed), 3) if dmed else None,
            "hit_rate": round(sum(1 for v in allr if v > 0) / len(allr), 3),
        })
    # 单调性判定（按日中位）
    vals = [(r["score"], r["daily_median"]) for r in rows if r.get("daily_median") is not None]
    mono_up = all(vals[i][1] <= vals[i + 1][1] for i in range(len(vals) - 1)) if len(vals) > 1 else None
    # 关键：5分档 vs 1分档
    top = next((r["daily_median"] for r in rows if r["score"] == 5.0), None)
    bot = next((r["daily_median"] for r in rows if r["score"] == 1.0), None)
    spread = (top - bot) if (top is not None and bot is not None) else None
    return {"field": key, "label": label, "buckets": rows,
            "monotonic_increasing": mono_up,
            "top_minus_bottom_pp": round(spread, 3) if spread is not None else None,
            "direction_ok": (spread > 0) if spread is not None else None}


def main():
    hop = 5
    K = json.load(open(os.path.join(HERE, "data", "klines_long.json"), encoding="utf-8"))
    print(f"═══ 反转分数档单调性检验 · hop={hop} ═══")
    obs = build(K, hop)
    ds = sorted({x["date"] for x in obs})
    print(f"观测 {len(obs):,} 个，{len(ds)} 个交易日（{ds[0]} ~ {ds[-1]}）\n")

    R = {"hop": hop, "n_obs": len(obs), "date_range": [ds[0], ds[-1]], "fields": {}}

    for key, label in [("s_rsi", "score_rsi_rev（RSI反转）"),
                       ("s_bias", "score_bias60（乖离反转）"),
                       ("s_os", "score_oversold60（超跌60）")]:
        st = bucket_stats(obs, key, label)
        R["fields"][key] = st
        print(f"── {label} ──")
        print(f"  {'分档':>5} {'n':>8} {'天数':>5} {'逐股均值':>9} {'逐股中位':>9} "
              f"{'按日等权':>9} {'按日中位':>9} {'命中率':>7}")
        for b in st["buckets"]:
            if b["n"] == 0:
                print(f"  {b['score']:>5.1f} {0:>8}  (无样本)")
                continue
            print(f"  {b['score']:>5.1f} {b['n']:>8,} {b['n_days']:>5} "
                  f"{b['raw_mean']:>9} {b['raw_median']:>9} "
                  f"{b['daily_ew_mean']:>9} {b['daily_median']:>9} {b['hit_rate']:>7}")
        print(f"  ▶ 按日中位单调递增: {st['monotonic_increasing']}  "
              f"5分档−1分档 = {st['top_minus_bottom_pp']}pp  "
              f"方向{'✅正确' if st['direction_ok'] else '❌错误'}")
        print()

    # ── 交叉：分数档 × 是否创新低 ──
    print("── score_rsi_rev 分数档 × 是否创新低（看两套映射是否互相矛盾）──")
    print(f"  {'分档':>5} {'创新低: 按日中位':>16} {'n':>7} | {'不创新低: 按日中位':>18} {'n':>7}")
    cross = []
    for sc in [3.0, 4.0, 5.0]:
        row = {"score": sc}
        for flag, name in [(True, "new_low"), (False, "no_new_low")]:
            sub = [x for x in obs if x["s_rsi"] == sc and x["low_new"] is flag
                   and x["fwd"] is not None]
            if not sub:
                row[name] = None
                continue
            by_day = defaultdict(list)
            for x in sub:
                by_day[x["date"]].append(x["fwd"])
            dmed = [median(v) for v in by_day.values() if v]
            row[name] = {"n": len(sub), "daily_median": round(mean(dmed), 3) if dmed else None}
        cross.append(row)
        a = row.get("new_low") or {}
        b = row.get("no_new_low") or {}
        print(f"  {sc:>5.1f} {a.get('daily_median'):>16} {a.get('n', 0):>7} | "
              f"{b.get('daily_median'):>18} {b.get('n', 0):>7}")
    R["cross_score_newlow"] = cross
    print()

    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    json.dump(R, open(OUT, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"已写出 {os.path.relpath(OUT, HERE)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
