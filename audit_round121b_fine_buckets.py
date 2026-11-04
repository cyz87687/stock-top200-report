#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
audit_round121b_fine_buckets.py —— 反转两维「细分档」扫描（决定要不要重设分档）

═══ 为什么要这一步 ═══
round120f F1 已实测：score_rsi_rev 5 分档（RSI<30）超额 −0.066，
反而比 4 分档（30≤RSI<40）+0.214 差 → 现行「越低越高分」末档不单调。
但在决定「加确认门封顶」之前，必须先回答一个更基础的问题：

    RSI 越低真的越好吗？还是存在一个最优点，极端深跌反而更差？

如果答案是「存在最优点」，那么只加确认门不够 —— 分档本身就得重设，
否则「RSI=15 但收阳」这种样本仍会拿满分。

═══ 扫描口径 ═══
  RSI   : 每 5 点一档，从 0 到 100（极端档 <20 / ≥80 单独）
  bias60: 每 5pp 一档，从 −45 到 +15
  每组 vs 全池，严格按日配对（单日每组≥MIN_GROUP_N），三重 t

  ⚠️ 同时给「组内中位绝对收益」和「配对差」：本区间是全池下跌市，
     绝对收益必负，只有配对差可比（方法论：禁拿绝对收益当超额定论）

输出 data/audit_round121b_fine.json
"""
import os
import json
import math
from collections import defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "data", "audit_round121b_fine.json")

MIN_GROUP_N = 2
WARMUP = 65
MIN_DAYS_FOR_REPORT = 20     # 配对天数 < 20 的档位标「样本不足，不作依据」


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


def nw_t(xs, lag):
    xs = [x for x in xs if x is not None]
    n = len(xs)
    if n < 5 or lag < 0:
        return None
    m = sum(xs) / n
    e = [x - m for x in xs]
    lrv = sum(v * v for v in e) / n
    for k in range(1, min(lag, n - 1) + 1):
        gk = sum(e[i] * e[i - k] for i in range(k, n)) / n
        w = 1.0 - k / (lag + 1.0)
        lrv += 2.0 * w * gk
    if lrv <= 0:
        return None
    return m / math.sqrt(lrv / n)


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


def sma(vals, n):
    out = [None] * len(vals)
    if len(vals) < n:
        return out
    run = 0.0
    for i, v in enumerate(vals):
        run += v
        if i >= n:
            run -= vals[i - n]
        if i >= n - 1:
            out[i] = run / n
    return out


def build(K, hop):
    obs = []
    for sym, daymap in K.items():
        if not isinstance(daymap, dict) or len(daymap) < WARMUP + hop + 5:
            continue
        dates = sorted(daymap.keys())
        opens, closes = [], []
        ok = True
        for d in dates:
            b = daymap[d]
            if not isinstance(b, (list, tuple)) or len(b) < 5:
                ok = False
                break
            try:
                o, c = float(b[0]), float(b[1])
            except (TypeError, ValueError):
                ok = False
                break
            if c <= 0:
                ok = False
                break
            opens.append(o); closes.append(c)
        if not ok or len(closes) < WARMUP + hop + 5:
            continue

        rsis = rsi_series(closes)
        ma5s = sma(closes, 5)
        ma20s = sma(closes, 20)
        ma60s = sma(closes, 60)

        for i in range(WARMUP, len(closes) - hop):
            r = rsis[i]; m60 = ma60s[i]
            if r is None or m60 is None or m60 <= 0:
                continue
            c = closes[i]; o = opens[i]
            prev_closes = closes[i - 19:i]
            if len(prev_closes) < 15:
                continue
            new_low = bool(c <= min(prev_closes) + 1e-12)
            bias60 = (c - m60) / m60 * 100.0
            above5 = bool(ma5s[i] is not None and c > ma5s[i])
            yang = bool(c > o)
            confirm = (not new_low) or above5 or yang
            obs.append({
                "sym": sym, "date": dates[i],
                "fwd": (closes[i + hop] / c - 1.0) * 100.0,
                "rsi": r, "bias60": bias60,
                "new_low": new_low, "confirm": confirm,
            })
    return obs


def paired_vs_pool(obs, mask, hop):
    ba, bp = defaultdict(list), defaultdict(list)
    for x in obs:
        bp[x["date"]].append(x["fwd"])
        if mask(x):
            ba[x["date"]].append(x["fwd"])
    days = sorted(d for d in ba
                  if len(ba[d]) >= MIN_GROUP_N and len(bp[d]) >= MIN_GROUP_N)
    dm = [median(ba[d]) - median(bp[d]) for d in days]
    t, n = tstat(dm)
    tnw = nw_t(dm, max(0, hop - 1))
    tsub, nsub = tstat(dm[::hop])
    allv = [v for d in ba for v in ba[d]]
    return {
        "n": len(allv), "days": len(ba), "n_days_paired": n,
        "raw_median": round(median(allv), 3) if allv else None,
        "diff_median_pp": round(mean(dm), 3) if dm else None,
        "t": round(t, 3) if t is not None else None,
        "t_nw": round(tnw, 3) if tnw is not None else None,
        "t_sub": round(tsub, 3) if tsub is not None else None,
        "n_days_sub": nsub,
        "thin": bool((n or 0) < MIN_DAYS_FOR_REPORT),
    }


RSI_EDGES = [0, 20, 25, 30, 35, 40, 45, 50, 55, 60, 65, 70, 80, 101]
BIAS_EDGES = [-100, -45, -40, -35, -30, -25, -20, -15, -10, -5, 0, 5, 100]


def label(lo, hi, unit=""):
    if lo <= -99:
        return f"<{hi}{unit}"
    if hi >= 100:
        return f"≥{lo}{unit}"
    return f"[{lo},{hi}){unit}"


def main():
    K = json.load(open(os.path.join(HERE, "data", "klines_long.json"), encoding="utf-8"))
    out = {"min_group_n": MIN_GROUP_N,
           "min_days_for_report": MIN_DAYS_FOR_REPORT,
           "note": "全池下跌市，绝对收益必负，只看配对差；thin=True 的档位样本不足、不得作依据",
           "horizons": {}}

    for hop in (5, 20):
        obs = build(K, hop)
        ds = sorted({x["date"] for x in obs})
        pool_med = mean([median(v) for v in
                         _pool_by_date(obs).values()])
        print(f"\n{'='*100}\nhop={hop}  观测 {len(obs):,}  {len(ds)} 交易日  "
              f"全池当日中位收益均值 {pool_med:+.3f}%")
        H = {"n_obs": len(obs), "n_days": len(ds),
             "date_range": [ds[0], ds[-1]],
             "pool_daily_median_mean_pct": round(pool_med, 3),
             "rsi": [], "bias60": []}

        # ── RSI 细分档 ──
        print("\n── RSI 细分档 vs 全池（配对）──")
        print(f"{'档位':<14} {'n':>7} {'配对天':>6} {'配对差':>9} {'t':>7} "
              f"{'t_NW':>7} {'t_sub':>7} {'绝对中位':>10} {'薄':>3}")
        for lo, hi in zip(RSI_EDGES[:-1], RSI_EDGES[1:]):
            r = paired_vs_pool(obs, lambda x, lo=lo, hi=hi: lo <= x["rsi"] < hi, hop)
            r["bucket"] = label(lo, hi)
            r["lo"], r["hi"] = lo, hi
            H["rsi"].append(r)
            print(f"{r['bucket']:<14} {r['n']:>7} {r['n_days_paired']:>6} "
                  f"{_f(r['diff_median_pp'],9)} {_f(r['t'],7,2)} "
                  f"{_f(r['t_nw'],7,2)} {_f(r['t_sub'],7,2)} "
                  f"{_f(r['raw_median'],10)} {('是' if r['thin'] else ''):>3}")

        # ── bias60 细分档 ──
        print("\n── bias60 细分档 vs 全池（配对）──")
        print(f"{'档位':<14} {'n':>7} {'配对天':>6} {'配对差':>9} {'t':>7} "
              f"{'t_NW':>7} {'t_sub':>7} {'绝对中位':>10} {'薄':>3}")
        for lo, hi in zip(BIAS_EDGES[:-1], BIAS_EDGES[1:]):
            r = paired_vs_pool(obs, lambda x, lo=lo, hi=hi: lo <= x["bias60"] < hi, hop)
            r["bucket"] = label(lo, hi)
            r["lo"], r["hi"] = lo, hi
            H["bias60"].append(r)
            print(f"{r['bucket']:<14} {r['n']:>7} {r['n_days_paired']:>6} "
                  f"{_f(r['diff_median_pp'],9)} {_f(r['t'],7,2)} "
                  f"{_f(r['t_nw'],7,2)} {_f(r['t_sub'],7,2)} "
                  f"{_f(r['raw_median'],10)} {('是' if r['thin'] else ''):>3}")

        # ── 极端档内 再按确认门拆 ──
        print("\n── 极端档内 × 确认门（拆解「是深跌本身好，还是止跌好」）──")
        H["extreme_x_confirm"] = []
        for name, mask_ext in [
            ("RSI<30", lambda x: x["rsi"] < 30),
            ("bias<=-15", lambda x: x["bias60"] <= -15),
            ("RSI<30 & bias<=-15", lambda x: x["rsi"] < 30 and x["bias60"] <= -15),
        ]:
            for cname, cmask in [("有确认", lambda x: x["confirm"]),
                                 ("无确认", lambda x: not x["confirm"])]:
                r = paired_vs_pool(obs, lambda x, m=mask_ext, c=cmask: m(x) and c(x), hop)
                r["extreme"] = name
                r["confirm"] = cname
                H["extreme_x_confirm"].append(r)
                print(f"  {name:<22} {cname:<6} n={r['n']:>6} 配对天={r['n_days_paired']:>4} "
                      f"差={_f(r['diff_median_pp'],8)} t={_f(r['t'],6,2)} "
                      f"t_NW={_f(r['t_nw'],6,2)} t_sub={_f(r['t_sub'],6,2)}")
            # 有确认 − 无确认 的直接对比
            r = _direct(obs, lambda x, m=mask_ext: m(x) and x["confirm"],
                        lambda x, m=mask_ext: m(x) and not x["confirm"], hop)
            r["extreme"] = name
            r["confirm"] = "有确认 − 无确认"
            H["extreme_x_confirm"].append(r)
            print(f"  {name:<22} 有确认−无确认  差={_f(r['diff_median_pp'],8)} "
                  f"t={_f(r['t'],6,2)} t_NW={_f(r['t_nw'],6,2)} "
                  f"t_sub={_f(r['t_sub'],6,2)} 配对天={r['n_days_paired']}")

        out["horizons"][f"h{hop}"] = H

    json.dump(out, open(OUT, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"\n✅ 已写 {OUT}")


def _pool_by_date(obs):
    d = defaultdict(list)
    for x in obs:
        d[x["date"]].append(x["fwd"])
    return d


def _direct(obs, mask_a, mask_b, hop):
    ba, bb = defaultdict(list), defaultdict(list)
    for x in obs:
        if mask_a(x):
            ba[x["date"]].append(x["fwd"])
        if mask_b(x):
            bb[x["date"]].append(x["fwd"])
    days = sorted(d for d in set(ba) & set(bb)
                  if len(ba[d]) >= MIN_GROUP_N and len(bb[d]) >= MIN_GROUP_N)
    dm = [median(ba[d]) - median(bb[d]) for d in days]
    t, n = tstat(dm)
    tnw = nw_t(dm, max(0, hop - 1))
    tsub, nsub = tstat(dm[::hop])
    return {"n_a": sum(len(ba[d]) for d in ba), "n_b": sum(len(bb[d]) for d in bb),
            "n_days_paired": n,
            "diff_median_pp": round(mean(dm), 3) if dm else None,
            "t": round(t, 3) if t is not None else None,
            "t_nw": round(tnw, 3) if tnw is not None else None,
            "t_sub": round(tsub, 3) if tsub is not None else None,
            "n_days_sub": nsub,
            "thin": bool((n or 0) < MIN_DAYS_FOR_REPORT)}


def _f(v, w=8, d=3):
    return "—".rjust(w) if v is None else f"{v:>{w}.{d}f}"


if __name__ == "__main__":
    main()
