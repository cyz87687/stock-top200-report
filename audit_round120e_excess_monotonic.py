#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
audit_round120e_excess_monotonic.py —— 反转分数档的「超额收益」单调性（最终裁决）

═══ 为什么必须看超额 ═══
round120d 测出三个反转因子的分数档「按日中位数」都是负的（−0.1 ~ −1.0），
但这 263 个交易日本身是下跌市 → 绝对收益被市场 beta 主导，
「所有股票都跌」不能说明「这个因子没用」。

判据必须是**超额**：每只股票的未来收益 − 当日全池中位数。
  超额 > 0  → 该因子确实挑出了跑赢同日的股票
  超额 = 0  → 无区分力
  超额 < 0  → 该因子在反向挑股票（越「低估」越差）

═══ 检验 ═══
  E1 各分数档的超额收益（按日中位 + 按日等权两个口径）
  E2 单调性秩相关（Spearman：分数 vs 超额，按日算再平均）
  E3 5分档 vs 全池的超额差 + t 值（这是「满分到底有没有用」的直接答案）
  E4 生益科技那一类（RSI<30 + 双创新低 + 零企稳）的实测超额

输出 data/audit_round120e_excess.json
"""
import os
import sys
import json
import math
from collections import defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "data", "audit_round120e_excess.json")


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


def spearman(xs, ys):
    """秩相关（无并列修正，足够用于单调性判断）"""
    n = len(xs)
    if n < 5:
        return None

    def rank(v):
        order = sorted(range(n), key=lambda i: v[i])
        r = [0.0] * n
        i = 0
        while i < n:
            j = i
            while j + 1 < n and v[order[j + 1]] == v[order[i]]:
                j += 1
            avg = (i + j) / 2.0 + 1
            for k in range(i, j + 1):
                r[order[k]] = avg
            i = j + 1
        return r

    rx, ry = rank(xs), rank(ys)
    mx, my = sum(rx) / n, sum(ry) / n
    num = sum((rx[i] - mx) * (ry[i] - my) for i in range(n))
    dx = math.sqrt(sum((rx[i] - mx) ** 2 for i in range(n)))
    dy = math.sqrt(sum((ry[i] - my) ** 2 for i in range(n)))
    if dx == 0 or dy == 0:
        return None
    return num / (dx * dy)


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


def s_rsi(x):
    if x is None: return 2.5
    if x < 30: return 5.0
    if x < 40: return 4.0
    if x < 50: return 3.0
    if x < 60: return 2.0
    return 1.0


def s_bias(x):
    if x is None: return 2.5
    if x <= -15: return 5.0
    if x <= -10: return 4.0
    if x <= -5: return 3.0
    if x <= 0: return 2.0
    return 1.0


def s_os(x):
    if x is None: return 2.5
    if x <= -30: return 5.0
    if x <= -20: return 4.0
    if x <= -10: return 3.0
    if x <= 0: return 2.0
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
                "s_rsi": s_rsi(r), "s_bias": s_bias(bias60), "s_os": s_os(mom60),
                "low_new": bool(lo[i] <= min(seg_lo) + 1e-12),
            })
    return obs


def add_excess(obs):
    """每日减全池中位数，得到超额（这是选股能力的口径）"""
    by_day = defaultdict(list)
    for x in obs:
        by_day[x["date"]].append(x["fwd"])
    med = {d: median(v) for d, v in by_day.items()}
    for x in obs:
        x["excess"] = x["fwd"] - med[x["date"]]
    return obs, med


def main():
    hop = 5
    K = json.load(open(os.path.join(HERE, "data", "klines_long.json"), encoding="utf-8"))
    print(f"═══ 反转分数档「超额收益」检验 · hop={hop} ═══")
    obs = build(K, hop)
    obs, day_med = add_excess(obs)
    ds = sorted({x["date"] for x in obs})
    # 区间涨跌
    dm = [day_med[d] for d in ds if day_med[d] is not None]
    print(f"观测 {len(obs):,} 个，{len(ds)} 个交易日（{ds[0]} ~ {ds[-1]}）")
    print(f"区间内「当日全池中位收益」的均值 = {mean(dm):.3f}% "
          f"→ {'下跌市' if mean(dm) < 0 else '上涨市'}（这解释了为何绝对收益全是负的）\n")

    R = {"hop": hop, "n_obs": len(obs), "date_range": [ds[0], ds[-1]],
         "market_daily_median_mean_pct": round(mean(dm), 3), "fields": {}}

    # ══ E1/E2：各字段分档超额 + 秩相关 ══
    for key, label in [("s_rsi", "score_rsi_rev（RSI反转）"),
                       ("s_bias", "score_bias60（乖离反转）"),
                       ("s_os", "score_oversold60（超跌60）")]:
        print(f"── {label} ──")
        rows = []
        # 每日每档的超额均值
        per_day = defaultdict(lambda: defaultdict(list))
        for x in obs:
            per_day[x["date"]][x[key]].append(x["excess"])
        for sc in [1.0, 2.0, 3.0, 4.0, 5.0]:
            d_med, d_mean, allv = [], [], []
            for d, buckets in per_day.items():
                v = buckets.get(sc)
                if v:
                    d_med.append(median(v))
                    d_mean.append(mean(v))
                    allv.extend(v)
            if not allv:
                rows.append({"score": sc, "n": 0})
                continue
            t_med, nd = tstat(d_med)
            rows.append({
                "score": sc, "n": len(allv), "n_days": nd,
                "excess_median": round(mean(d_med), 3) if d_med else None,
                "excess_mean": round(mean(d_mean), 3) if d_mean else None,
                "t_median": round(t_med, 3) if t_med is not None else None,
                "hit_rate_vs_pool": round(sum(1 for v in allv if v > 0) / len(allv), 3),
            })
        print(f"  {'分档':>5} {'n':>8} {'天数':>5} {'超额(按日中位)':>14} {'t':>7} "
              f"{'超额(按日均值)':>14} {'跑赢全池占比':>12}")
        for b in rows:
            if b["n"] == 0:
                print(f"  {b['score']:>5.1f} {0:>8}  (无样本)")
                continue
            print(f"  {b['score']:>5.1f} {b['n']:>8,} {b['n_days']:>5} "
                  f"{b['excess_median']:>14} {b['t_median']:>7} "
                  f"{b['excess_mean']:>14} {b['hit_rate_vs_pool']:>12}")

        # 单调性：秩相关（每日算一次，再平均）
        rhos = []
        for d, buckets in per_day.items():
            xs, ys = [], []
            for sc in [1.0, 2.0, 3.0, 4.0, 5.0]:
                v = buckets.get(sc)
                if v:
                    xs.append(sc)
                    ys.append(median(v))
            if len(xs) >= 4:
                r_ = spearman(xs, ys)
                if r_ is not None:
                    rhos.append(r_)
        rho_m, rho_n = tstat(rhos)
        # 5 分档超额 vs 0
        top = next((b for b in rows if b["score"] == 5.0), {})
        t_top = top.get("t_median")
        print(f"  ▶ 分数↔超额 Spearman(按日) 均值 = {mean(rhos):+.4f}  "
              f"t={rho_m:.2f}（n={rho_n} 天）" if rho_m is not None else "  ▶ 秩相关样本不足")
        print(f"  ▶ 5分档超额 = {top.get('excess_median')}pp，t={t_top} → "
              f"{'✅ 显著跑赢' if (t_top or 0) >= 3.0 else ('跑赢但不显著' if (t_top or 0) > 0 else '❌ 跑输全池')}")
        R["fields"][key] = {"rows": rows,
                            "spearman_mean": round(mean(rhos), 4) if rhos else None,
                            "spearman_t": round(rho_m, 3) if rho_m is not None else None,
                            "top_excess": top.get("excess_median"),
                            "top_t": t_top}
        print()

    # ══ E3：生益科技那一类的实测超额 ══
    print("── E3 「生益科技型」样本的实测超额 ──")
    print("  定义：RSI<30 且 当日最低创20日新低 且 (无任何企稳条件，用低RSI+新低代理)")
    typ = [x for x in obs if x["rsi"] < 30 and x["low_new"]]
    def desc(arr, name):
        by_d = defaultdict(list)
        for x in arr:
            by_d[x["date"]].append(x["excess"])
        dmed = [median(v) for v in by_d.values() if v]
        t_, nd = tstat(dmed)
        allv = [x["excess"] for x in arr]
        print(f"  {name:36s} n={len(arr):>6,} 天数={nd:>4} "
              f"超额(按日中位)={mean(dmed):>+7.3f} t={t_ if t_ is not None else '—':>7} "
              f"跑赢全池={sum(1 for v in allv if v>0)/len(allv)*100:>5.1f}%")
        return {"n": len(arr), "days": nd,
                "excess_median": round(mean(dmed), 3) if dmed else None,
                "t": round(t_, 3) if t_ is not None else None}
    R["E3"] = {}
    R["E3"]["typical_newlow"] = desc(typ, "RSI<30 且创新低（生益科技型）")
    R["E3"]["rsi30_nolow"] = desc(
        [x for x in obs if x["rsi"] < 30 and not x["low_new"]], "RSI<30 且不创新低")
    R["E3"]["all_rsi30"] = desc([x for x in obs if x["rsi"] < 30], "RSI<30 全部（现行满分）")
    print()

    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    json.dump(R, open(OUT, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"已写出 {os.path.relpath(OUT, HERE)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
