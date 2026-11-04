#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
audit_round121_rev_remap.py —— 反转因子重映射前测（候选方案 M0~M4）

═══ 为什么改、改什么 ═══
round120f 已证实现行反转两维（score_bias60 / score_rsi_rev，各 0.05）：
  ① 分档不单调：rsi_rev 5 分档 −0.066 差于 4 分档 +0.214（t=1.74）
  ② 「站上MA20 趋势确认」结构性不存在：RSI<30 内 n=0（全样本站上MA20 的最小 RSI=35.45）
  ③ 「企稳信号个数」与未来收益**反向**：0个→0.000 / 2个→−1.497 / 3个→−2.609 / 4个→−1.531
     → 上一轮「把企稳信号当加分/筛选条件」的思路被自家数据推翻（方法论㉘）
  ④ 生益科技型（RSI<30+创新低+零企稳）vs RSI<30 全体 = +0.077（t=0.847，方向不稳）
     → 该组未来收益**与池内无异**，所以「筛掉它」不产生 alpha

于是本脚本把改动目标重新定位为**面值有效性（face validity）+ 不给无证据的满分**，
而不是宣称 alpha。核心动作：深跌/极端乖离在**缺少止跌确认**时**不给满分**。

═══ 候选 ═══
  M0 现行（基线）
  M1 双维「未确认 → None」（置缺维，由 scale_calc 填中性 DMAX/2=2.5）
  M2 双维「未确认 → 3.0」（温和封顶）
  M3 rsi 封 3.0 / bias 封 3.5（bias 侧 mean/hit_rate 确有正向迹象，故略高）
  M4 双维「未确认 → 4.0」（极温和，仅去掉满分）

确认门 confirm = 至少满足其一：不创20日新低 / 站上MA5 / 当日收阳
  ⚠️ 不用「站上MA20」——RSI<30 内样本=0，是空集（方法论㉛）
  ⚠️ 不用「企稳信号≥2」——F2b 实测收益反向

═══ 评估口径（全部按日、hop 可换）═══
  A 覆盖率与封顶占比（分布冲击）
  B 每日 Spearman(块分, fwd) + naive t + NW(lag=hop−1) + 不重叠子样本 t
  C 块分五分位 vs 全池（严格按日配对，单日每组≥2 只）
  D Δtotal 分布（Δtotal = 0.1×Δscore×2 维，量程 5、权重 0.05 各一）
  E 生益科技型路径学：改前/改后分数
输出 data/audit_round121_rev_remap.json
"""
import os
import json
import math
from collections import defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "data", "audit_round121_rev_remap.json")

MIN_GROUP_N = 2
WARMUP = 65


# ────────────────────────── 统计工具 ──────────────────────────
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


def pct(xs, q):
    xs = sorted(x for x in xs if x is not None)
    if not xs:
        return None
    i = min(len(xs) - 1, max(0, int(round(q * (len(xs) - 1)))))
    return xs[i]


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


def nw_t(xs, lag):
    """Newey-West t；lag 必须显式给（方法论⑮）"""
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


# ────────────────────────── 指标（逐字复刻生产） ──────────────────────────
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
    """现行 score_rsi_rev（step2_stockscorer_v2.py:2734）"""
    if x is None:
        return 2.5
    if x < 30: return 5.0
    if x < 40: return 4.0
    if x < 50: return 3.0
    if x < 60: return 2.0
    return 1.0


def s_bias_base(x):
    """现行 score_bias60（step2_stockscorer_v2.py:2721）"""
    if x is None:
        return 2.5
    if x <= -15: return 5.0
    if x <= -10: return 4.0
    if x <= -5:  return 3.0
    if x <= 0:   return 2.0
    return 1.0


# ────────────────────────── 候选映射 ──────────────────────────
def cap_rsi(rsi, confirm, cap):
    base = s_rsi_base(rsi)
    if base >= 5.0 and not confirm:
        return cap
    return base


def cap_bias(bias, confirm, cap):
    base = s_bias_base(bias)
    if base >= 5.0 and not confirm:
        return cap
    return base


# 方案名 -> (rsi 封顶值或 None 表示置缺维, bias 封顶值或 None)
PLANS = {
    "M0_current":  (None, None),      # 不改
    "M1_none":     (0.0, 0.0),        # 未确认 → None（缺维，中性 2.5）
    "M2_cap30":    (3.0, 3.0),
    "M3_cap30_35": (3.0, 3.5),
    "M4_cap40":    (4.0, 4.0),
}


def apply_plan(plan, rsi, bias, confirm):
    """返回 (s_rsi, s_bias)；None 表示该维置缺维。"""
    r_cap, b_cap = PLANS[plan]
    sr = s_rsi_base(rsi)
    sb = s_bias_base(bias)
    if plan == "M0_current":
        return sr, sb
    if r_cap is not None and sr >= 5.0 and not confirm:
        sr = None if r_cap == 0.0 else r_cap
    if b_cap is not None and sb >= 5.0 and not confirm:
        sb = None if b_cap == 0.0 else b_cap
    return sr, sb


# ────────────────────────── 构建观测 ──────────────────────────
def build(K, hop):
    """⚠️ bar = [open, close, high, low, volume]；date 是 dict key（非数组元素）"""
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

            prev_closes = [x for x in closes[i - 19:i]]
            if len(prev_closes) < 15:
                continue
            new_low = bool(c <= min(prev_closes) + 1e-12)
            bias60 = (c - m60) / m60 * 100.0

            m5p = ma5s[i - 1] if i >= 1 else None
            m20p = ma20s[i - 1] if i >= 1 else None
            above5 = bool(m5 is not None and c > m5)
            ma5up = bool(m5 is not None and m5p is not None and m5 > m5p)
            yang = bool(c > o)
            bear_align = bool(m5 is not None and m20 is not None and c < m5 < m20)

            # 确认门：至少一个止跌迹象（不含站上MA20 —— 空集）
            confirm = (not new_low) or above5 or yang

            obs.append({
                "sym": sym, "date": dates[i],
                "fwd": (closes[i + hop] / c - 1.0) * 100.0,
                "rsi": r, "bias60": bias60,
                "new_low": new_low, "above5": above5,
                "ma5up": ma5up, "yang": yang, "bear_align": bear_align,
                "confirm": confirm,
            })
    return obs


# ────────────────────────── 评分 ──────────────────────────
def block_score(plan, x):
    """块分 = 两维等权（与页面 _TECH_BLOCK_KEYS 口径一致）；None 维按中性 2.5（DMAX/2）。"""
    sr, sb = apply_plan(plan, x["rsi"], x["bias60"], x["confirm"])
    v = []
    for s in (sr, sb):
        v.append(2.5 if s is None else s)
    return (v[0] + v[1]) / 2.0


def daily_ic(obs, plan, hop_days):
    by = defaultdict(list)
    for x in obs:
        by[x["date"]].append((block_score(plan, x), x["fwd"]))
    vals = []
    for d, pairs in by.items():
        if len(pairs) < 50:
            continue
        rho = spearman([p[0] for p in pairs], [p[1] for p in pairs])
        if rho is not None:
            vals.append(rho)
    t, n = tstat(vals)
    tnw = nw_t(vals, max(0, hop_days - 1))
    tsub, nsub = tstat(vals[::hop_days])
    return {"mean_rho": round(mean(vals), 5) if vals else None,
            "t": round(t, 3) if t is not None else None,
            "t_nw": round(tnw, 3) if tnw is not None else None,
            "t_sub": round(tsub, 3) if tsub is not None else None,
            "n_days": n, "n_days_sub": nsub}


def quintile_vs_pool(obs, plan, hop_days):
    """按块分池内五分位（逐日分位），Q1..Q5 vs 全池，严格按日配对。"""
    bydate = defaultdict(list)
    for x in obs:
        bydate[x["date"]].append(x)

    rows = defaultdict(lambda: defaultdict(list))   # q -> date -> [fwd]
    pool = defaultdict(list)
    for d, xs in bydate.items():
        if len(xs) < 50:
            continue
        xs2 = sorted(xs, key=lambda z: block_score(plan, z))
        n = len(xs2)
        for rank, z in enumerate(xs2):
            q = min(4, int(rank * 5 / n))
            rows[q + 1][d].append(z["fwd"])
            pool[d].append(z["fwd"])

    out = []
    for q in range(1, 6):
        days = sorted(d for d in rows[q]
                      if len(rows[q][d]) >= MIN_GROUP_N and len(pool[d]) >= MIN_GROUP_N)
        dm = [median(rows[q][d]) - median(pool[d]) for d in days]
        m = mean(dm)
        t, n = tstat(dm)
        tnw = nw_t(dm, max(0, hop_days - 1))
        tsub, nsub = tstat(dm[::hop_days])
        out.append({"q": q, "n": sum(len(rows[q][d]) for d in rows[q]),
                    "n_days_paired": n,
                    "diff_median_pp": round(m, 3) if m is not None else None,
                    "t": round(t, 3) if t is not None else None,
                    "t_nw": round(tnw, 3) if tnw is not None else None,
                    "t_sub": round(tsub, 3) if tsub is not None else None,
                    "n_days_sub": nsub})
    # Q5−Q1
    days = sorted(d for d in set(rows[5]) & set(rows[1])
                  if len(rows[5][d]) >= MIN_GROUP_N and len(rows[1][d]) >= MIN_GROUP_N)
    dm = [median(rows[5][d]) - median(rows[1][d]) for d in days]
    t, n = tstat(dm)
    tnw = nw_t(dm, max(0, hop_days - 1))
    tsub, nsub = tstat(dm[::hop_days])
    spread = {"q5_minus_q1_pp": round(mean(dm), 3) if dm else None,
              "t": round(t, 3) if t is not None else None,
              "t_nw": round(tnw, 3) if tnw is not None else None,
              "t_sub": round(tsub, 3) if tsub is not None else None,
              "n_days_paired": n, "n_days_sub": nsub}
    return out, spread


def dist_stats(obs, plan):
    """分布冲击 + Δtotal（Δtotal = 2 维 × w0.05 × Δ/5×10 = 0.1×Δscore/维）"""
    n = len(obs)
    sr_max = sb_max = 0
    d_total = []
    changed = 0
    for x in obs:
        sr0, sb0 = apply_plan("M0_current", x["rsi"], x["bias60"], x["confirm"])
        sr1, sb1 = apply_plan(plan, x["rsi"], x["bias60"], x["confirm"])
        if sr1 is not None and sr1 >= 5.0:
            sr_max += 1
        if sb1 is not None and sb1 >= 5.0:
            sb_max += 1
        v0 = [2.5 if s is None else s for s in (sr0, sb0)]
        v1 = [2.5 if s is None else s for s in (sr1, sb1)]
        d = 0.0
        for a, b in zip(v0, v1):
            d += 0.05 * ((b - a) / 5.0 * 10.0)
        if abs(d) > 1e-9:
            changed += 1
        d_total.append(d)
    return {
        "n": n,
        "rsi_rev_max_share_pct": round(100.0 * sr_max / n, 2),
        "bias60_max_share_pct": round(100.0 * sb_max / n, 2),
        "n_changed": changed,
        "changed_pct": round(100.0 * changed / n, 2),
        "d_total_mean": round(mean(d_total), 4),
        "d_total_p05": round(pct(d_total, 0.05), 4),
        "d_total_p50": round(pct(d_total, 0.50), 4),
        "d_total_p95": round(pct(d_total, 0.95), 4),
        "d_total_min": round(min(d_total), 4),
    }


def shengyi_path(obs):
    """生益科技型：RSI<30 + 创20日新低 + 空头排列(收<MA5<MA20)"""
    grp = [x for x in obs if x["rsi"] < 30 and x["new_low"] and x["bear_align"]]
    if not grp:
        return None
    res = {"n": len(grp), "days": len({x["date"] for x in grp}),
           "raw_median_fwd": round(median([x["fwd"] for x in grp]), 3)}
    for plan in PLANS:
        bs = [block_score(plan, x) for x in grp]
        res[plan + "_block_mean"] = round(mean(bs), 3)
        res[plan + "_block_max"] = round(max(bs), 3)
        res[plan + "_block_ge9_share_pct"] = round(
            100.0 * sum(1 for v in bs if v / 5.0 * 10.0 >= 9.0) / len(bs), 2)
    return res


def confirm_rate(obs):
    lo = [x for x in obs if x["rsi"] < 30]
    lo_bias = [x for x in obs if x["bias60"] <= -15]
    return {
        "rsi_lt30_n": len(lo),
        "rsi_lt30_confirm_pct": round(100.0 * sum(1 for x in lo if x["confirm"]) / len(lo), 2) if lo else None,
        "rsi_lt30_not_confirm_n": sum(1 for x in lo if not x["confirm"]),
        "bias_le_m15_n": len(lo_bias),
        "bias_le_m15_confirm_pct": round(100.0 * sum(1 for x in lo_bias if x["confirm"]) / len(lo_bias), 2) if lo_bias else None,
    }


def fmt(v, w=8, d=3):
    return "—".rjust(w) if v is None else f"{v:>{w}.{d}f}"


def main():
    K = json.load(open(os.path.join(HERE, "data", "klines_long.json"), encoding="utf-8"))
    print("═══ round121 反转重映射前测（M0 基线 / M1~M4 候选）═══")
    out = {"plans": {k: list(v) for k, v in PLANS.items()},
           "confirm_rule": "confirm = (!创20日新低) or 站上MA5 or 当日收阳",
           "note": "站上MA20 未纳入确认门：RSI<30 内样本=0（结构性空集）",
           "horizons": {}}

    for hop in (5, 20):
        obs = build(K, hop)
        ds = sorted({x["date"] for x in obs})
        print(f"\n{'='*78}\nhop={hop}  观测 {len(obs):,} 个  {len(ds)} 交易日 "
              f"（{ds[0]} ~ {ds[-1]}）")
        H = {"n_obs": len(obs), "n_days": len(ds),
             "date_range": [ds[0], ds[-1]],
             "confirm": confirm_rate(obs),
             "plans": {}}

        print("\n── 确认门覆盖率 ──")
        print(f"  RSI<30           n={H['confirm']['rsi_lt30_n']:>6}  "
              f"有确认 {H['confirm']['rsi_lt30_confirm_pct']}%  "
              f"无确认 {H['confirm']['rsi_lt30_not_confirm_n']} 只")
        print(f"  bias60<=−15      n={H['confirm']['bias_le_m15_n']:>6}  "
              f"有确认 {H['confirm']['bias_le_m15_confirm_pct']}%")

        print("\n── A. 分布冲击 + Δtotal（2 维 × w0.05，量程 5）──")
        print(f"{'方案':<13} {'rsi满分%':>9} {'bias满分%':>10} {'变动股%':>9} "
              f"{'Δ均':>8} {'Δp05':>8} {'Δp50':>8} {'Δp95':>8} {'Δmin':>8}")
        for plan in PLANS:
            d = dist_stats(obs, plan)
            H["plans"].setdefault(plan, {})["dist"] = d
            print(f"{plan:<13} {d['rsi_rev_max_share_pct']:>9.2f} "
                  f"{d['bias60_max_share_pct']:>10.2f} {d['changed_pct']:>9.2f} "
                  f"{d['d_total_mean']:>8.3f} {d['d_total_p05']:>8.3f} "
                  f"{d['d_total_p50']:>8.3f} {d['d_total_p95']:>8.3f} "
                  f"{d['d_total_min']:>8.3f}")

        print("\n── B. 每日 Spearman(块分, fwd fwd) ──")
        print(f"{'方案':<13} {'mean_rho':>10} {'t':>7} {'t_NW':>7} {'t_sub':>7} {'天数':>5}")
        for plan in PLANS:
            ic = daily_ic(obs, plan, hop)
            H["plans"].setdefault(plan, {})["ic"] = ic
            print(f"{plan:<13} {fmt(ic['mean_rho'],10,4)} {fmt(ic['t'],7,2)} "
                  f"{fmt(ic['t_nw'],7,2)} {fmt(ic['t_sub'],7,2)} {ic['n_days']:>5}")

        print("\n── C. 块分五分位 vs 全池（严格按日配对，单日每组≥2 只）──")
        for plan in PLANS:
            rows, spread = quintile_vs_pool(obs, plan, hop)
            H["plans"].setdefault(plan, {})["quintile"] = rows
            H["plans"][plan]["q5_q1"] = spread
            seq = "  ".join(f"Q{r['q']}={r['diff_median_pp']:+.3f}"
                            f"(t{r['t_nw'] if r['t_nw'] is not None else float('nan'):+.2f})"
                            for r in rows)
            print(f"  {plan:<13} {seq}")
            print(f"  {'':13} Q5−Q1={spread['q5_minus_q1_pp']:+.3f} "
                  f"t={spread['t']} t_NW={spread['t_nw']} t_sub={spread['t_sub']} "
                  f"配对天={spread['n_days_paired']}")

        if hop == 5:
            print("\n── E. 生益科技型（RSI<30 + 创20日新低 + 收<MA5<MA20）路径学 ──")
            sp = shengyi_path(obs)
            H["shengyi_type"] = sp
            print(f"  n={sp['n']}  天={sp['days']}  未来{hop}日中位={sp['raw_median_fwd']:+.3f}%")
            for plan in PLANS:
                print(f"  {plan:<13} 块分均值 {sp[plan+'_block_mean']:>5.2f}  "
                      f"块分上限 {sp[plan+'_block_max']:>5.2f}  "
                      f"页面≥9分占比 {sp[plan+'_block_ge9_share_pct']:>5.1f}%")

        out["horizons"][f"h{hop}"] = H

    json.dump(out, open(OUT, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"\n✅ 已写 {OUT}")
    print("\n═══ 判据（写进报告必须同时满足）═══")
    print("  ① 块分 IC 不低于 M0（不能为了面值把预测力改坏）")
    print("  ② 五分位不出现「越高越差」的倒挂")
    print("  ③ Δtotal 量级可控（不能靠改分数偷改总分）")
    print("  ④ 生益型不再拿满分（这是改动的初衷，必须达成）")


if __name__ == "__main__":
    main()
