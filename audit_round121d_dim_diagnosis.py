#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
audit_round121d_dim_diagnosis.py —— 两维「增量信息」诊断（决定改哪一维、怎么改）

═══ 背景 ═══
round121c（h=20 主判据）实测：
  score_rsi_rev 分档配对差  1→−0.594  2→−0.794  3→−0.565  4→+0.366  5→+1.033  ← 基本单调，方向对
  score_bias60  分档配对差  1→−0.631  2→−0.235  3→−0.549  4→−0.355  5→−0.154  ← 全负，完全不单调
  → 两维问题不同源：rsi_rev 只是「分不够细/面值偏高」，bias60 是「在主目标周期无区分力」

本脚本回答：
  A 两维相关性（共线程度）——若高度共线，bias60 无增量是必然
  B 两维各自单独 IC vs 合并 IC —— 合并是否优于任一单维
  C 确认门对两维的增量（h=20，含「有确认−无确认」直接对比 + 分维）
  D bias60 若换成「bias60 分位 × 确认门」是否更好
  E 最终候选方案的 IC 稳定性（逐季/分段，防「只在一个 regime 成立」）

输出 data/audit_round121d_dim.json
"""
import os
import json
import math
from collections import defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "data", "audit_round121d_dim.json")

MIN_GROUP_N = 2
WARMUP = 65


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


def pearson(xs, ys):
    n = len(xs)
    if n < 8:
        return None
    mx, my = sum(xs) / n, sum(ys) / n
    num = sum((xs[i] - mx) * (ys[i] - my) for i in range(n))
    dx = math.sqrt(sum((xs[i] - mx) ** 2 for i in range(n)))
    dy = math.sqrt(sum((ys[i] - my) ** 2 for i in range(n)))
    if dx == 0 or dy == 0:
        return None
    return num / (dx * dy)


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


def rank_vec(v):
    n = len(v)
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


def spearman(xs, ys):
    n = len(xs)
    if n < 8:
        return None
    return pearson(rank_vec(xs), rank_vec(ys))


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


def s_rsi_base(x):
    if x is None:
        return 2.5
    if x < 30: return 5.0
    if x < 40: return 4.0
    if x < 50: return 3.0
    if x < 60: return 2.0
    return 1.0


def s_bias_base(x):
    if x is None:
        return 2.5
    if x <= -15: return 5.0
    if x <= -10: return 4.0
    if x <= -5:  return 3.0
    if x <= 0:   return 2.0
    return 1.0


def build(K, hop):
    obs = []
    for sym, daymap in K.items():
        if not isinstance(daymap, dict) or len(daymap) < WARMUP + hop + 5:
            continue
        dates = sorted(daymap.keys())
        opens, closes, lows = [], [], []
        ok = True
        for d in dates:
            b = daymap[d]
            if not isinstance(b, (list, tuple)) or len(b) < 5:
                ok = False
                break
            try:
                o, c, lo = float(b[0]), float(b[1]), float(b[3])
            except (TypeError, ValueError):
                ok = False
                break
            if c <= 0:
                ok = False
                break
            opens.append(o); closes.append(c); lows.append(lo)
        if not ok or len(closes) < WARMUP + hop + 5:
            continue
        rsis = rsi_series(closes)
        ma5s = sma(closes, 5)
        ma20s = sma(closes, 20)
        ma60s = sma(closes, 60)
        for i in range(WARMUP, len(closes) - hop):
            r = rsis[i]; m60 = ma60s[i]; m5 = ma5s[i]; m20 = ma20s[i]
            if r is None or m60 is None or m60 <= 0:
                continue
            c = closes[i]; o = opens[i]
            prev_closes = closes[i - 19:i]
            if len(prev_closes) < 15:
                continue
            new_low = bool(c <= min(prev_closes) + 1e-12)
            bias60 = (c - m60) / m60 * 100.0
            above5 = bool(m5 is not None and c > m5)
            ma5up = bool(m5 is not None and i >= 1 and ma5s[i - 1] is not None
                         and m5 > ma5s[i - 1])
            yang = bool(c > o)
            confirm = (not new_low) or above5 or yang
            obs.append({
                "sym": sym, "date": dates[i],
                "fwd": (closes[i + hop] / c - 1.0) * 100.0,
                "rsi": r, "bias60": bias60,
                "s_rsi": s_rsi_base(r), "s_bias": s_bias_base(bias60),
                "new_low": new_low, "above5": above5,
                "ma5up": ma5up, "yang": yang, "confirm": confirm,
            })
    return obs


def daily_ic(obs, keyfn, hop, min_n=50, date_filter=None):
    by = defaultdict(list)
    for x in obs:
        if date_filter and not date_filter(x["date"]):
            continue
        v = keyfn(x)
        if v is None:
            continue
        by[x["date"]].append((v, x["fwd"]))
    vals = []
    for d, pairs in by.items():
        if len(pairs) < min_n:
            continue
        rho = spearman([p[0] for p in pairs], [p[1] for p in pairs])
        if rho is not None:
            vals.append((d, rho))
    rs = [v for _, v in vals]
    t, n = tstat(rs)
    return {"mean_rho": round(mean(rs), 5) if rs else None,
            "t": round(t, 3) if t is not None else None,
            "t_nw": round(nw_t(rs, max(0, hop - 1)), 3) if rs else None,
            "t_sub": round(tstat(rs[::hop])[0], 3) if len(rs[::hop]) >= 3 else None,
            "n_days": n, "_series": vals}


def paired_direct(obs, mask_a, mask_b, hop):
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
    return {"n_a": sum(len(ba[d]) for d in ba), "n_b": sum(len(bb[d]) for d in bb),
            "n_days_paired": n,
            "diff_median_pp": round(mean(dm), 3) if dm else None,
            "t": round(t, 3) if t is not None else None,
            "t_nw": round(nw_t(dm, max(0, hop - 1)), 3) if dm else None,
            "t_sub": round(tstat(dm[::hop])[0], 3) if len(dm[::hop]) >= 3 else None,
            "n_days_sub": len(dm[::hop]) if dm else 0}


def f(v, w=9, d=3):
    return "—".rjust(w) if v is None else f"{v:>{w}.{d}f}"


def main():
    K = json.load(open(os.path.join(HERE, "data", "klines_long.json"), encoding="utf-8"))
    print("═══ round121d 两维增量信息诊断 ═══")
    out = {"horizons": {}}

    for hop in (20, 5):
        obs = build(K, hop)
        ds = sorted({x["date"] for x in obs})
        print(f"\n{'='*96}\nhop={hop}  {'★ 主判据' if hop == 20 else '参考'}  "
              f"{len(obs):,} 观测  {len(ds)} 日")

        # ── A 相关性 ──
        bydate = defaultdict(list)
        for x in obs:
            bydate[x["date"]].append(x)
        rhos = []
        rho_raw = []
        for d, xs in bydate.items():
            if len(xs) < 50:
                continue
            r = spearman([z["s_rsi"] for z in xs], [z["s_bias"] for z in xs])
            if r is not None:
                rhos.append(r)
            r2 = spearman([z["rsi"] for z in xs], [z["bias60"] for z in xs])
            if r2 is not None:
                rho_raw.append(r2)
        A = {"mean_spearman_score": round(mean(rhos), 4),
             "mean_spearman_raw": round(mean(rho_raw), 4),
             "n_days": len(rhos)}
        print(f"\n── A 两维共线 ──")
        print(f"  分数口径 Spearman(rsi_rev, bias60) = {A['mean_spearman_score']:+.4f}")
        print(f"  原始口径 Spearman(RSI, bias60)     = {A['mean_spearman_raw']:+.4f}")

        # ── B 单维 vs 合并 IC ──
        print("\n── B 单维 / 合并 IC（正=分数越高收益越高）──")
        B = {}
        cands = {
            "rsi_only": lambda x: x["s_rsi"],
            "bias_only": lambda x: x["s_bias"],
            "combine_avg": lambda x: (x["s_rsi"] + x["s_bias"]) / 2.0,
            "rsi_raw": lambda x: -x["rsi"],       # 越低越好 → 反号
            "bias_raw": lambda x: -x["bias60"],   # 越负越好 → 反号
        }
        print(f"  {'口径':<14} {'mean_rho':>10} {'t':>7} {'t_NW':>7} {'t_sub':>7} {'天数':>5}")
        for name, fn in cands.items():
            ic = daily_ic(obs, fn, hop)
            ic.pop("_series", None)
            B[name] = ic
            print(f"  {name:<14} {f(ic['mean_rho'],10,4)} {f(ic['t'],7,2)} "
                  f"{f(ic['t_nw'],7,2)} {f(ic['t_sub'],7,2)} {ic['n_days']:>5}")

        # ── C 确认门分维增量 ──
        print("\n── C 确认门增量（h=%d，有确认 − 无确认）──" % hop)
        C = {}
        for name, ext, lo_gate in [
            ("RSI<30", lambda x: x["rsi"] < 30, None),
            ("RSI<40", lambda x: x["rsi"] < 40, None),
            ("bias<=-10", lambda x: x["bias60"] <= -10, None),
            ("bias<=-15", lambda x: x["bias60"] <= -15, None),
            ("RSI<30|bias<=-15", lambda x: x["rsi"] < 30 or x["bias60"] <= -15, None),
            ("RSI<30&bias<=-15", lambda x: x["rsi"] < 30 and x["bias60"] <= -15, None),
        ]:
            r = paired_direct(obs,
                              lambda x, e=ext: e(x) and x["confirm"],
                              lambda x, e=ext: e(x) and not x["confirm"], hop)
            r["extreme"] = name
            C[name] = r
            print(f"  {name:<20} 有确认−无确认 = {f(r['diff_median_pp'])} "
                  f"t={f(r['t'],6,2)} t_NW={f(r['t_nw'],6,2)} t_sub={f(r['t_sub'],6,2)} "
                  f"配对天={r['n_days_paired']} 子样本={r['n_days_sub']} "
                  f"nA/nB={r['n_a']}/{r['n_b']}")

        # ── D 分段稳定性（按季度）──
        print("\n── D 最终候选的分段稳定性（按季度）──")
        months = sorted({d[:7] for d in ds})
        quarters = sorted({f"{m[:4]}Q{(int(m[5:7])-1)//3+1}" for m in months})
        qmap = {m: f"{m[:4]}Q{(int(m[5:7])-1)//3+1}" for m in months}

        def plan_block(plan, x):
            sr, sb = x["s_rsi"], x["s_bias"]
            cf = x["confirm"]
            if plan == "M0":
                pass
            elif plan == "M5b":          # 只做减法：未确认满分 → 4.5
                if not cf and sr >= 5.0: sr = 4.5
                if not cf and sb >= 5.0: sb = 4.5
            elif plan == "M5b_rsi_only":  # 只改 rsi（bias 无区分力，不动）
                if not cf and sr >= 5.0: sr = 4.5
            return (sr + sb) / 2.0

        D = {}
        print(f"  {'季度':<8} " + "  ".join(f"{p:>22}" for p in
                                          ("M0", "M5b", "M5b_rsi_only")))
        for q in quarters:
            row = {}
            cells = []
            for plan in ("M0", "M5b", "M5b_rsi_only"):
                ic = daily_ic(obs, lambda x, p=plan: plan_block(p, x), hop,
                              min_n=30,
                              date_filter=lambda dt, q=q: qmap.get(dt[:7]) == q)
                ic.pop("_series", None)
                row[plan] = ic
                cells.append(f"{f(ic['mean_rho'],9,4)}/t{f(ic['t'],5,2)}"
                             f"({ic['n_days']:>2}d)")
            D[q] = row
            print(f"  {q:<8} " + "  ".join(f"{c:>22}" for c in cells))

        out["horizons"][f"h{hop}"] = {
            "n_obs": len(obs), "n_days": len(ds),
            "date_range": [ds[0], ds[-1]],
            "A_collinearity": A, "B_single_vs_combo": B,
            "C_confirm_increment": C, "D_quarterly": D}

    json.dump(out, open(OUT, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"\n✅ 已写 {OUT}")


if __name__ == "__main__":
    main()
