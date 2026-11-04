#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
audit_round121e_final_candidates.py —— 终选方案前测（决定落地的唯一依据）

═══ 前置实证（round121~121d，h=20 为模型主目标 target_horizon）═══
  ① score_rsi_rev 手数方向**是对的**（分档配对差 1→−0.594 … 5→+1.033），
     但 <30 内部细分档差 5pp（[0,20) +6.459 / [20,25) +2.418 / [25,30) +1.408）
     → 现行「<30 一律 5.0」浪费面值，不是方向错
  ② score_bias60 五档**全负且不单调**（−0.631/−0.235/−0.549/−0.355/−0.154）
  ③ 两维**高度共线**：分数 Spearman +0.7625、原始 +0.8597；
     合并 IC(0.0348) **不优于**任一单维（rsi 0.0341 / bias 0.0360）
  ④ 确认门只在 RSI<30 内为正（+1.321, t=3.15, t_NW=3.75, **t_sub=1.55 不显著**，
     子样本仅 8 天）；在 bias<=−10 内**为负**（−0.556, t=−1.79）
  ⑤ ⭐ **连续口径优于分档口径**：bias_raw IC **+0.0461**（全场最高）
     > bias_only 分档 +0.0360；rsi 两者近似（0.0342 vs 0.0341）
     → 现行分档（绝对阈值）**丢失了连续信息**，这是比「加确认门」更本质的缺陷

═══ 候选 ═══
  C0 现行（基线）
  C1 细分档：rsi <20/25/30/40/50/60/≥60 → 5/4.5/4/3.5/2.5/1.5/1（bias 不动）
  C2 确认门微调：RSI<30 内无确认 −0.5（叠加在 C1 上）
  C3 ⭐ 池内分位连续：两维都改为「池内 rank 分位 → 0–5」，保留绝对阈值做兜底
  C4 ⭐ 只改 rsi 为池内分位、bias 保持现行（因 bias 无增量证据）
  C5 bias 也改池内分位但仅用其连续值反号

评估：每日 Spearman(块分,fwd) + 三重 t；五分位 vs 全池 + Q5−Q1；
      逐季度稳定性（防单 regime）；生益型块分路径；Δtotal 量级
输出 data/audit_round121e_final.json
"""
import os
import json
import math
from collections import defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "data", "audit_round121e_final.json")

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
    rx, ry = rank_vec(xs), rank_vec(ys)
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


# ── 现行映射 ──
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


def s_rsi_fine(x):
    """C1 细分档：依据 h=20 细分档实测（+6.459/+2.418/+1.408/+0.527/−0.4~−0.8）"""
    if x is None:
        return 2.5
    if x < 20: return 5.0
    if x < 25: return 4.5
    if x < 30: return 4.0
    if x < 40: return 3.5
    if x < 50: return 2.5
    if x < 60: return 1.5
    return 1.0


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
            yang = bool(c > o)
            bear_align = bool(m5 is not None and m20 is not None and c < m5 < m20)
            confirm = (not new_low) or above5 or yang
            obs.append({
                "sym": sym, "date": dates[i],
                "fwd": (closes[i + hop] / c - 1.0) * 100.0,
                "rsi": r, "bias60": bias60,
                "s_rsi": s_rsi_base(r), "s_bias": s_bias_base(bias60),
                "s_rsi_fine": s_rsi_fine(r),
                "new_low": new_low, "above5": above5, "yang": yang,
                "bear_align": bear_align, "confirm": confirm,
            })
    # ── 池内分位（逐日 rank → 0..5，方向：RSI 越低越高分 / bias 越负越高分）──
    bydate = defaultdict(list)
    for x in obs:
        bydate[x["date"]].append(x)
    for d, xs in bydate.items():
        n = len(xs)
        if n < 30:
            for x in xs:
                x["q_rsi"] = None
                x["q_bias"] = None
            continue
        rr = rank_vec([-x["rsi"] for x in xs])       # 低 RSI → 高 rank
        rb = rank_vec([-x["bias60"] for x in xs])    # 负 bias → 高 rank
        for k, x in enumerate(xs):
            x["q_rsi"] = (rr[k] - 0.5) / n * 5.0
            x["q_bias"] = (rb[k] - 0.5) / n * 5.0
    return obs


def scores(plan, x):
    sr, sb = x["s_rsi"], x["s_bias"]
    cf = x["confirm"]
    if plan == "C0_current":
        return sr, sb
    if plan == "C1_fine":
        return x["s_rsi_fine"], sb
    if plan == "C2_fine_gate":
        sr2 = x["s_rsi_fine"]
        if x["rsi"] < 30 and not cf:
            sr2 = max(1.0, sr2 - 0.5)
        return sr2, sb
    if plan == "C3_both_quantile":
        qr, qb = x["q_rsi"], x["q_bias"]
        return (qr if qr is not None else sr, qb if qb is not None else sb)
    if plan == "C4_rsi_quantile":
        qr = x["q_rsi"]
        return (qr if qr is not None else sr), sb
    if plan == "C5_bias_quantile":
        qb = x["q_bias"]
        return sr, (qb if qb is not None else sb)
    if plan == "C6_fine_quantile_bias":
        # rsi 细分档 + 确认门微调；bias 用池内分位
        sr2 = x["s_rsi_fine"]
        if x["rsi"] < 30 and not cf:
            sr2 = max(1.0, sr2 - 0.5)
        qb = x["q_bias"]
        return sr2, (qb if qb is not None else sb)
    if plan == "C7_quantile_rsionly_bias_hold":
        # rsi 池内分位 + 确认门微调；bias 现行（最保守：只改一维）
        qr = x["q_rsi"]
        sr2 = qr if qr is not None else sr
        if x["rsi"] < 30 and not cf:
            sr2 = max(1.0, sr2 - 0.5)
        return sr2, sb
    raise ValueError(plan)


PLANS = ["C0_current", "C1_fine", "C2_fine_gate", "C3_both_quantile",
         "C4_rsi_quantile", "C5_bias_quantile", "C6_fine_quantile_bias",
         "C7_quantile_rsionly_bias_hold"]


def block(plan, x):
    sr, sb = scores(plan, x)
    a = 2.5 if sr is None else sr
    b = 2.5 if sb is None else sb
    return (a + b) / 2.0


def daily_ic(obs, plan, hop, min_n=50, dfilter=None):
    by = defaultdict(list)
    for x in obs:
        if dfilter and not dfilter(x["date"]):
            continue
        by[x["date"]].append((block(plan, x), x["fwd"]))
    vals = []
    for d, pairs in by.items():
        if len(pairs) < min_n:
            continue
        rho = spearman([p[0] for p in pairs], [p[1] for p in pairs])
        if rho is not None:
            vals.append(rho)
    t, n = tstat(vals)
    return {"mean_rho": round(mean(vals), 5) if vals else None,
            "t": round(t, 3) if t is not None else None,
            "t_nw": round(nw_t(vals, max(0, hop - 1)), 3) if vals else None,
            "t_sub": round(tstat(vals[::hop])[0], 3) if len(vals[::hop]) >= 3 else None,
            "n_days": n}


def quintile(obs, plan, hop):
    bydate = defaultdict(list)
    for x in obs:
        bydate[x["date"]].append(x)
    q_rows = defaultdict(lambda: defaultdict(list))
    pool = defaultdict(list)
    for d, xs in bydate.items():
        if len(xs) < 50:
            continue
        xs2 = sorted(xs, key=lambda z: block(plan, z))
        n = len(xs2)
        for rk, z in enumerate(xs2):
            q = min(4, int(rk * 5 / n))
            q_rows[q + 1][d].append(z["fwd"])
            pool[d].append(z["fwd"])

    def _st(ra, rb):
        days = sorted(d for d in set(ra) & set(rb)
                      if len(ra[d]) >= MIN_GROUP_N and len(rb[d]) >= MIN_GROUP_N)
        dm = [median(ra[d]) - median(rb[d]) for d in days]
        t, n = tstat(dm)
        return {"diff_median_pp": round(mean(dm), 3) if dm else None,
                "t": round(t, 3) if t is not None else None,
                "t_nw": round(nw_t(dm, max(0, hop - 1)), 3) if dm else None,
                "t_sub": round(tstat(dm[::hop])[0], 3) if len(dm[::hop]) >= 3 else None,
                "n_days_paired": n}

    rows = []
    for q in range(1, 6):
        s = _st(q_rows[q], pool)
        s["q"] = q
        s["n"] = sum(len(q_rows[q][d]) for d in q_rows[q])
        rows.append(s)
    return rows, _st(q_rows[5], q_rows[1])


def f(v, w=9, d=3):
    return "—".rjust(w) if v is None else f"{v:>{w}.{d}f}"


def main():
    K = json.load(open(os.path.join(HERE, "data", "klines_long.json"), encoding="utf-8"))
    print("═══ round121e 终选方案前测（h=20 主判据 / h=5 参考）═══")
    out = {"candidates": PLANS,
           "note": "分档依据 h=20 细分档实测；池内分位 = 逐日 rank→0..5；"
                   "确认门=(!创20日新低 or 站上MA5 or 收阳)，仅 RSI<30 内生效",
           "horizons": {}}

    for hop in (20, 5):
        obs = build(K, hop)
        ds = sorted({x["date"] for x in obs})
        months = sorted({d[:7] for d in ds})
        qmap = {m: f"{m[:4]}Q{(int(m[5:7])-1)//3+1}" for m in months}
        quarters = sorted(set(qmap.values()))
        print(f"\n{'='*112}\nhop={hop}  {'★ 主判据' if hop == 20 else '参考'}  "
              f"{len(obs):,} 观测  {len(ds)} 日")

        H = {"n_obs": len(obs), "n_days": len(ds),
             "date_range": [ds[0], ds[-1]], "plans": {}}
        sg = lambda x: x["rsi"] < 30 and x["new_low"] and x["bear_align"]
        sg_obs = [x for x in obs if sg(x)]

        print(f"\n{'方案':<28} {'mean_rho':>9} {'t':>6} {'t_NW':>6} {'t_sub':>6} "
              f"{'Q5−Q1':>8} {'t_NW':>6} {'t_sub':>6} {'生益块':>7} {'Δ总分':>7}")
        for plan in PLANS:
            ic = daily_ic(obs, plan, hop)
            rows, sp = quintile(obs, plan, hop)
            bs = mean([block(plan, x) for x in sg_obs]) if sg_obs else None
            # Δtotal vs C0（2 维 × w0.05，量程 5 → 系数 0.1/分）
            dts = []
            for x in obs:
                a0, b0 = scores("C0_current", x)
                a1, b1 = scores(plan, x)
                d = 0.0
                for p, q in ((a0, a1), (b0, b1)):
                    p = 2.5 if p is None else p
                    q = 2.5 if q is None else q
                    d += 0.05 * ((q - p) / 5.0 * 10.0)
                dts.append(d)
            H["plans"][plan] = {"ic": ic, "quintile": rows, "q5_q1": sp,
                                "shengyi_block_mean": round(bs, 3) if bs is not None else None,
                                "d_total_mean": round(mean(dts), 4),
                                "d_total_p05": round(sorted(dts)[int(0.05*len(dts))], 4),
                                "d_total_p95": round(sorted(dts)[int(0.95*len(dts))-1], 4)}
            print(f"{plan:<28} {f(ic['mean_rho'],9,4)} {f(ic['t'],6,2)} "
                  f"{f(ic['t_nw'],6,2)} {f(ic['t_sub'],6,2)} "
                  f"{f(sp['diff_median_pp'],8)} {f(sp['t_nw'],6,2)} "
                  f"{f(sp['t_sub'],6,2)} {f(bs,7,2)} {f(mean(dts),7,3)}")

        # ── 逐季度稳定性（只跑 top3 候选，防单 regime）──
        focus = ["C0_current", "C2_fine_gate", "C6_fine_quantile_bias",
                 "C7_quantile_rsionly_bias_hold"]
        print(f"\n── 逐季度 IC 稳定性（mean_rho / t，防单 regime 假象）──")
        print(f"  {'季度':<9} " + "  ".join(f"{p:>24}" for p in focus))
        Q = {}
        for q in quarters:
            cells, row = [], {}
            for plan in focus:
                ic = daily_ic(obs, plan, hop, min_n=30,
                              dfilter=lambda dt, q=q: qmap.get(dt[:7]) == q)
                row[plan] = ic
                cells.append(f"{f(ic['mean_rho'],9,4)}/t{f(ic['t'],5,2)}({ic['n_days']:>2}d)")
            Q[q] = row
            print(f"  {q:<9} " + "  ".join(f"{c:>24}" for c in cells))
        H["quarterly"] = Q
        out["horizons"][f"h{hop}"] = H

    json.dump(out, open(OUT, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"\n✅ 已写 {OUT}")


if __name__ == "__main__":
    main()
