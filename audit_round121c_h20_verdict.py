#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
audit_round121c_h20_verdict.py —— 以模型主目标 h=20 为主判据的终版裁决

═══ 为什么必须重做 ═══
factor_weights.json 的 target_horizon = 20。而 round120f 的所有分档单调性结论
（「5 分档 −0.066 差于 4 分档 +0.214」）都是 hop=5 跑出来的。
hop=20 的细分档扫描（round121b）方向与 hop=5 **不一致**：

  h=5 : RSI<30 三档 +1.193 / +0.352 / +0.009，[70,80) −0.615、 ≥80 +1.463
  h=20: RSI<30 三档 +6.459 / +2.418 / +1.408 ← 越深越好，且方向一致单调
        [40,60) 全负、≥80 +3.428

  →「现行反转映射方向错了」这个判断只在 h=5 成立；在 h=20 下**方向是对的**，
    真正的问题变成：①同分不同收益（<30 全给 5.0，但内部差 5pp）
                    ②无确认/有确认在 h=20 有 +1.32pp 差，却拿同一个满分

═══ 本脚本回答三个问题 ═══
  Q1 h=20 下现行 score_rsi_rev / score_bias60 各分档 vs 全池，是否单调？
  Q2 生益科技型（RSI<30 + 创20日新低 + 收<MA5<MA20）在 h=20 的超额是多少？
  Q3 候选方案（含新增 M5「确认门 ±0.5 微调」）在 h=5 与 h=20 双周期下的表现

═══ M5 设计动机 ═══
round121 已实测：M1~M4（未确认 → 封顶到 3.0~4.0 或置缺维）在 h=20 下
块分 IC 与 Q5−Q1 **全部低于 M0**。原因不是确认门没用，而是：

    无确认的深跌股相对全池仍是正超额（RSI<30 无确认 +0.600），
    把它从 5.0 压到 3.0 = 把「较好的」打成「中等的」，
    同时也把它和真正中性的 RSI 40~60 段（−0.4~−0.6）混在一起。

所以 M5 改为**不封顶、只微调**：有确认 +0.5（封顶 5.0）、无确认 −0.5（不低于 1.0）。
这样保留「深跌给高分」的主信号，只把止跌确认的增量信息作为一档幅度体现。

输出 data/audit_round121c_h20.json
"""
import os
import json
import math
from collections import defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "data", "audit_round121c_h20.json")

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
            above20 = bool(m20 is not None and c > m20)
            ma5up = bool(m5 is not None and i >= 1 and ma5s[i - 1] is not None
                         and m5 > ma5s[i - 1])
            yang = bool(c > o)
            bear_align = bool(m5 is not None and m20 is not None and c < m5 < m20)
            confirm = (not new_low) or above5 or yang
            obs.append({
                "sym": sym, "date": dates[i],
                "fwd": (closes[i + hop] / c - 1.0) * 100.0,
                "rsi": r, "bias60": bias60,
                "s_rsi": s_rsi_base(r), "s_bias": s_bias_base(bias60),
                "new_low": new_low, "above5": above5, "above20": above20,
                "ma5up": ma5up, "yang": yang, "bear_align": bear_align,
                "confirm": confirm,
            })
    return obs


# ───────── 候选方案 ─────────
def plan_scores(plan, x):
    """返回 (s_rsi, s_bias)；None = 置缺维"""
    sr, sb = x["s_rsi"], x["s_bias"]
    cf = x["confirm"]
    if plan == "M0_current":
        return sr, sb
    if plan == "M1_none":
        if sr >= 5.0 and not cf: sr = None
        if sb >= 5.0 and not cf: sb = None
        return sr, sb
    if plan == "M2_cap30":
        if sr >= 5.0 and not cf: sr = 3.0
        if sb >= 5.0 and not cf: sb = 3.0
        return sr, sb
    if plan == "M3_cap30_35":
        if sr >= 5.0 and not cf: sr = 3.0
        if sb >= 5.0 and not cf: sb = 3.5
        return sr, sb
    if plan == "M4_cap40":
        if sr >= 5.0 and not cf: sr = 4.0
        if sb >= 5.0 and not cf: sb = 4.0
        return sr, sb
    if plan == "M5_shift05":
        # 未确认 −0.5（不低于 1.0）；有确认且已达 5.0 的封顶不动，其余 +0.5（封顶 5.0）
        if cf:
            sr = min(5.0, sr + 0.5) if sr < 5.0 else 5.0
            sb = min(5.0, sb + 0.5) if sb < 5.0 else 5.0
        else:
            sr = max(1.0, sr - 0.5)
            sb = max(1.0, sb - 0.5)
        return sr, sb
    if plan == "M5b_only_down":
        # 只做减法（不给任何股加分，避免「抬分」争议）
        if not cf:
            if sr >= 5.0: sr = 4.5
            if sb >= 5.0: sb = 4.5
        return sr, sb
    raise ValueError(plan)


PLANS = ["M0_current", "M1_none", "M2_cap30", "M3_cap30_35", "M4_cap40",
         "M5_shift05", "M5b_only_down"]


def block(plan, x):
    sr, sb = plan_scores(plan, x)
    a = 2.5 if sr is None else sr
    b = 2.5 if sb is None else sb
    return (a + b) / 2.0


def daily_ic(obs, plan, hop):
    by = defaultdict(list)
    for x in obs:
        by[x["date"]].append((block(plan, x), x["fwd"]))
    vals = []
    for d, pairs in by.items():
        if len(pairs) < 50:
            continue
        rho = spearman([p[0] for p in pairs], [p[1] for p in pairs])
        if rho is not None:
            vals.append(rho)
    t, n = tstat(vals)
    return {"mean_rho": round(mean(vals), 5) if vals else None,
            "t": round(t, 3) if t is not None else None,
            "t_nw": round(nw_t(vals, max(0, hop - 1)), 3) if vals else None,
            "t_sub": round(tstat(vals[::hop])[0], 3) if vals and len(vals[::hop]) >= 3 else None,
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
        for rank, z in enumerate(xs2):
            q = min(4, int(rank * 5 / n))
            q_rows[q + 1][d].append(z["fwd"])
            pool[d].append(z["fwd"])

    def _stat(rows_a, rows_b):
        days = sorted(d for d in set(rows_a) & set(rows_b)
                      if len(rows_a[d]) >= MIN_GROUP_N and len(rows_b[d]) >= MIN_GROUP_N)
        dm = [median(rows_a[d]) - median(rows_b[d]) for d in days]
        t, n = tstat(dm)
        return {"diff_median_pp": round(mean(dm), 3) if dm else None,
                "t": round(t, 3) if t is not None else None,
                "t_nw": round(nw_t(dm, max(0, hop - 1)), 3) if dm else None,
                "t_sub": round(tstat(dm[::hop])[0], 3) if len(dm[::hop]) >= 3 else None,
                "n_days_paired": n}

    rows = []
    for q in range(1, 6):
        s = _stat(q_rows[q], pool)
        s["q"] = q
        s["n"] = sum(len(q_rows[q][d]) for d in q_rows[q])
        rows.append(s)
    spread = _stat(q_rows[5], q_rows[1])
    spread["label"] = "Q5−Q1(块分高−低)"
    return rows, spread


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
    allv = [v for d in ba for v in ba[d]]
    return {"n": len(allv), "n_days_paired": n,
            "raw_median": round(median(allv), 3) if allv else None,
            "diff_median_pp": round(mean(dm), 3) if dm else None,
            "t": round(t, 3) if t is not None else None,
            "t_nw": round(nw_t(dm, max(0, hop - 1)), 3) if dm else None,
            "t_sub": round(tstat(dm[::hop])[0], 3) if len(dm[::hop]) >= 3 else None,
            "n_days_sub": len(dm[::hop]) if dm else 0}


def f(v, w=9, d=3):
    return "—".rjust(w) if v is None else f"{v:>{w}.{d}f}"


def main():
    K = json.load(open(os.path.join(HERE, "data", "klines_long.json"), encoding="utf-8"))
    print("═══ round121c 以 target_horizon=20 为主判据的终版裁决 ═══")
    out = {"plans": PLANS,
           "confirm_rule": "confirm = (!创20日新低) or 站上MA5 or 当日收阳",
           "note": "站上MA20 未纳入确认门：RSI<30 内样本=0（结构性空集）",
           "horizons": {}}

    for hop in (5, 20):
        obs = build(K, hop)
        ds = sorted({x["date"] for x in obs})
        pd_ = defaultdict(list)
        for x in obs:
            pd_[x["date"]].append(x["fwd"])
        run_med = mean([median(v) for v in pd_.values()])
        print(f"\n{'='*104}\nhop={hop}  (target_horizon=20 → "
              f"{'★ 主判据' if hop == 20 else '参考'})  观测 {len(obs):,}  "
              f"{len(ds)} 交易日  全池当日中位均值 {run_med:+.3f}%")
        H = {"n_obs": len(obs), "n_days": len(ds),
             "date_range": [ds[0], ds[-1]],
             "pool_daily_median_mean_pct": round(run_med, 3)}

        # ── Q1 现行各分档 ──
        print("\n── Q1 现行 score_rsi_rev / score_bias60 各分档 vs 全池 ──")
        print(f"  {'档':>4} {'n':>8} {'配对天':>6} {'配对差':>9} {'t':>7} {'t_NW':>7} "
              f"{'t_sub':>7} │ {'bias档':>6} {'n':>8} {'配对天':>6} {'配对差':>9} "
              f"{'t':>7} {'t_NW':>7} {'t_sub':>7}")
        Q1 = []
        for sc in [1.0, 2.0, 3.0, 4.0, 5.0]:
            a = paired_vs_pool(obs, lambda x, sc=sc: x["s_rsi"] == sc, hop)
            b = paired_vs_pool(obs, lambda x, sc=sc: x["s_bias"] == sc, hop)
            a["score"] = sc; a["dim"] = "score_rsi_rev"
            b["score"] = sc; b["dim"] = "score_bias60"
            Q1 += [a, b]
            print(f"  {sc:>4.0f} {a['n']:>8} {a['n_days_paired']:>6} "
                  f"{f(a['diff_median_pp'])} {f(a['t'],7,2)} {f(a['t_nw'],7,2)} "
                  f"{f(a['t_sub'],7,2)} │ {sc:>6.0f} {b['n']:>8} {b['n_days_paired']:>6} "
                  f"{f(b['diff_median_pp'])} {f(b['t'],7,2)} {f(b['t_nw'],7,2)} "
                  f"{f(b['t_sub'],7,2)}")
        H["Q1_bucket_abs"] = Q1

        # ── Q2 生益科技型 ──
        print("\n── Q2 生益科技型（RSI<30 + 创20日新低 + 收<MA5<MA20）──")
        sg = lambda x: x["rsi"] < 30 and x["new_low"] and x["bear_align"]
        r = paired_vs_pool(obs, sg, hop)
        r["label"] = "生益科技型 vs 全池"
        r["n_type"] = sum(1 for x in obs if sg(x))
        r["days_type"] = len({x["date"] for x in obs if sg(x)})
        H["Q2_shengyi_type"] = r
        print(f"  n={r['n_type']}  天={r['days_type']}  "
              f"绝对中位={f(r['raw_median'])}%  配对差={f(r['diff_median_pp'])} "
              f"t={f(r['t'],6,2)} t_NW={f(r['t_nw'],6,2)} t_sub={f(r['t_sub'],6,2)} "
              f"配对天={r['n_days_paired']}")
        # 对照：RSI<30 全体
        r2 = paired_vs_pool(obs, lambda x: x["rsi"] < 30, hop)
        r2["label"] = "RSI<30 全体 vs 全池"
        H["Q2_rsi30_all"] = r2
        print(f"  RSI<30 全体 n={r2['n']} 配对差={f(r2['diff_median_pp'])} "
              f"t={f(r2['t'],6,2)} t_NW={f(r2['t_nw'],6,2)} t_sub={f(r2['t_sub'],6,2)}")

        # ── Q3 候选方案 ──
        print("\n── Q3 候选方案对照（块分 = 两维等权，与页面 _TECH_BLOCK_KEYS 同口径）──")
        print(f"{'方案':<14} {'mean_rho':>10} {'t':>7} {'t_NW':>7} {'t_sub':>7} "
              f"{'Q5−Q1':>9} {'t_NW':>7} {'t_sub':>7} {'生益块分':>9}")
        Q3 = {}
        sg_obs = [x for x in obs if sg(x)]
        for plan in PLANS:
            ic = daily_ic(obs, plan, hop)
            rows, spread = quintile(obs, plan, hop)
            bs = mean([block(plan, x) for x in sg_obs]) if sg_obs else None
            Q3[plan] = {"ic": ic, "quintile": rows, "q5_q1": spread,
                        "shengyi_block_mean": round(bs, 3) if bs is not None else None}
            print(f"{plan:<14} {f(ic['mean_rho'],10,4)} {f(ic['t'],7,2)} "
                  f"{f(ic['t_nw'],7,2)} {f(ic['t_sub'],7,2)} "
                  f"{f(spread['diff_median_pp'])} {f(spread['t_nw'],7,2)} "
                  f"{f(spread['t_sub'],7,2)} {f(bs,9,2)}")
        H["Q3_plans"] = Q3

        # ── 五分位明细（仅 M0 / M5 / M5b）──
        print("\n── 五分位明细（M0 vs M5 vs M5b）──")
        for plan in ("M0_current", "M5_shift05", "M5b_only_down"):
            if plan not in Q3:
                continue
            seq = "  ".join(f"Q{r['q']}={r['diff_median_pp']:+.3f}(t_NW"
                            f"{r['t_nw'] if r['t_nw'] is not None else float('nan'):+.2f})"
                            for r in Q3[plan]["quintile"])
            print(f"  {plan:<14} {seq}")

        out["horizons"][f"h{hop}"] = H

    json.dump(out, open(OUT, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"\n✅ 已写 {OUT}")


if __name__ == "__main__":
    main()
