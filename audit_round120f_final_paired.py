#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
audit_round120f_final_paired.py —— round120 终版裁决（统一严格配对口径）

═══ 为什么还要再跑一次 ═══
round120b/120c/120d/120e 各自用了不同口径，产生三处互相矛盾的数字：

  ① 120b T1 报 paired_diff = −0.615，而 120b T2_vs_current 同一对比报 +0.615。
     不是 bug，是 true/false 角色互换（创新低−不创新低 vs 不创新低−创新低），
     但字段同名、符号相反，直接引用必然出错。
  ② 120d/120e 的分档统计用「各自出现的交易日」，创新低 132 天、不创新低 108 天，
     两组不是同一批日期 → 不可比，结果被稀释成 0.069 / 0.120
     （120c 严格配对是 0.656，差 5~9 倍）。
  ③ 120e 的 excess_mean 在 15 个档位全为正（+0.43~+1.47），逻辑上不可能：
     超额基准是「当日全池中位数」，而全池 raw_mean − daily_median = 1.123pp，
     任何档位的均值超额都会被机械抬正。excess_mean 一律作废。

═══ 本脚本口径纪律 ═══
  1) 组间比较**必须按日配对**：只取两组当日都有 ≥MIN_GROUP_N 只样本的交易日；
  2) 每交易日先算组内中位数（抗右尾），再对日差序列求均值与 t；
  3) 同时给按日均值口径对照，两口径方向不符即标 sign_consistent=False；
  4) 差的方向显式写成 (A − B)，字段名带方向，不靠猜；
  5) 不产出 excess_mean 这类会被「均值−中位差」污染的指标。

═══ 检验 ═══
  F1 现行 score_rsi_rev 各分档 vs 全池
  F2 核心问题：RSI<30 内 企稳/趋势 各前置条件的增量
  F3 放宽到全池/RSI<40/RSI<50，「不创新低」是否本身有区分力
  F4 生益科技型（RSI<30 + 创新低 + 零企稳）vs RSI<30 全体
  F5 每日 Spearman（分数 vs 收益）→ 单调性的统计表达
  F6 加前置条件的样本代价

输出 data/audit_round120f_final.json
"""
import os
import json
import math
from collections import defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "data", "audit_round120f_final.json")

HOP = 5
MIN_GROUP_N = 2       # 单日某组最少样本数
WARMUP = 65           # 需要 MA60 + 20日窗口


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
    if n < 5:
        return None
    rx, ry = rank_vec(xs), rank_vec(ys)
    mx, my = sum(rx) / n, sum(ry) / n
    num = sum((rx[i] - mx) * (ry[i] - my) for i in range(n))
    dx = math.sqrt(sum((rx[i] - mx) ** 2 for i in range(n)))
    dy = math.sqrt(sum((ry[i] - my) ** 2 for i in range(n)))
    if dx == 0 or dy == 0:
        return None
    return num / (dx * dy)


# ────────────────────────── 指标与打分（逐字复刻生产） ──────────────────────────
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
    """生产 score_rsi_rev 逐字复刻（step2_stockscorer_v2.py:2734）"""
    if x is None:
        return 2.5
    if x < 30: return 5.0
    if x < 40: return 4.0
    if x < 50: return 3.0
    if x < 60: return 2.0
    return 1.0


def s_bias(x):
    """生产 score_bias60 逐字复刻（step2_stockscorer_v2.py:2721）"""
    if x is None:
        return 2.5
    if x <= -15: return 5.0
    if x <= -10: return 4.0
    if x <= -5:  return 3.0
    if x <= 0:   return 2.0
    return 1.0


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


# ────────────────────────── 构建观测 ──────────────────────────
def build(K, hop):
    """⚠️ bar 结构 = [open, close, high, low, volume]（非标准 OHLCV！）"""
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

            # ⚠️ 「创20日新低」的标准定义 = 当日值 < **前19日**（不含当日）的最低值。
            #   绝不能写成 c <= min(lows[i-19:i+1]) —— 那要求收盘价是 20 日绝对最低点，
            #   是另一回事，会把样本砍到 4%（实测 816/20449）。两个口径都算，分别披露。
            prev_lows = [x for x in lows[i - 19:i] if x is not None]
            prev_closes = [x for x in closes[i - 19:i] if x is not None]
            if len(prev_lows) < 15 or len(prev_closes) < 15:
                continue
            new_low_intraday = bool(lows[i] <= min(prev_lows) + 1e-12)   # 盘中创新低
            new_low_close = bool(c <= min(prev_closes) + 1e-12)          # 收盘创新低（主口径）

            bias60 = (c - m60) / m60 * 100.0
            fwd = (closes[i + hop] / c - 1.0) * 100.0

            m5p = ma5s[i - 1] if i >= 1 else None
            m20p = ma20s[i - 1] if i >= 1 else None

            above5 = bool(m5 is not None and c > m5)
            above20 = bool(m20 is not None and c > m20)
            ma5up = bool(m5 is not None and m5p is not None and m5 > m5p)
            ma20up = bool(m20 is not None and m20p is not None and m20 > m20p)
            yang = bool(c > o)

            n_stab = int(not new_low_close) + int(above5) + int(ma5up) + int(yang)

            obs.append({
                "sym": sym, "date": dates[i], "fwd": fwd,
                "rsi": r, "bias60": bias60,
                "s_rsi": s_rsi(r), "s_bias": s_bias(bias60),
                "new_low": new_low_close,
                "new_low_intra": new_low_intraday,
                "above5": above5, "above20": above20,
                "ma5up": ma5up, "ma20up": ma20up, "yang": yang,
                "n_stab": n_stab,
            })
    return obs


# ────────────────────────── 配对检验核心 ──────────────────────────
def nw_t(xs, lag):
    """Newey-West t（lag 必须显式给，不设默认 —— 方法论⑮）。

    ⚠️ 重叠窗口纪律：hop=5 的逐日观测存在 MA(4) 自相关，naive t 会虚高。
    这里用 Bartlett 核估计长期方差，lag 应取 hop−1。
    """
    xs = [x for x in xs if x is not None]
    n = len(xs)
    if n < 5 or lag < 0:
        return None
    m = sum(xs) / n
    e = [x - m for x in xs]
    s0 = sum(v * v for v in e) / n
    lrv = s0
    for k in range(1, min(lag, n - 1) + 1):
        gk = sum(e[i] * e[i - k] for i in range(k, n)) / n
        w = 1.0 - k / (lag + 1.0)
        lrv += 2.0 * w * gk
    if lrv <= 0:
        return None
    return m / math.sqrt(lrv / n)


def paired(obs, mask_a, mask_b, name_a, name_b, label, hop=HOP):
    """严格按日配对：只取两组当日都 ≥MIN_GROUP_N 的交易日。差 = A − B。

    ⚠️ t 值给三档：
      t_median       naive（日序列，未校正重叠）
      t_median_nw    NW lag=hop−1（重叠窗口校正）
      t_median_sub   不重叠重采样（每 hop 天取一天）→ 最保守，作为裁决依据
    """
    ba, bb = defaultdict(list), defaultdict(list)
    for x in obs:
        if x["fwd"] is None:
            continue
        if mask_a(x):
            ba[x["date"]].append(x["fwd"])
        if mask_b(x):
            bb[x["date"]].append(x["fwd"])

    days = sorted(d for d in set(ba) & set(bb)
                  if len(ba[d]) >= MIN_GROUP_N and len(bb[d]) >= MIN_GROUP_N)
    d_med = [median(ba[d]) - median(bb[d]) for d in days]
    d_avg = [mean(ba[d]) - mean(bb[d]) for d in days]

    t_med, n_med = tstat(d_med)
    t_avg, _ = tstat(d_avg)
    t_med_nw = nw_t(d_med, hop - 1)
    # 不重叠重采样：从第 0 天起每 hop 天取一天
    sub = d_med[::hop]
    t_med_sub, n_sub = tstat(sub)

    all_a = [v for d in ba for v in ba[d]]
    all_b = [v for d in bb for v in bb[d]]

    sign_ok = None
    if d_med and d_avg:
        sign_ok = (mean(d_med) > 0) == (mean(d_avg) > 0)

    return {
        "label": label,
        "comparison": f"({name_a}) − ({name_b})",
        "n_days_paired": n_med,
        "diff_median_pp": round(mean(d_med), 3) if d_med else None,
        "t_median": round(t_med, 3) if t_med is not None else None,
        "t_median_nw": round(t_med_nw, 3) if t_med_nw is not None else None,
        "t_median_sub": round(t_med_sub, 3) if t_med_sub is not None else None,
        "n_days_sub": n_sub,
        "diff_mean_pp": round(mean(d_avg), 3) if d_avg else None,
        "t_mean": round(t_avg, 3) if t_avg is not None else None,
        "n_a": len(all_a), "n_b": len(all_b),
        "days_a": len(ba), "days_b": len(bb),
        "raw_median_a": round(median(all_a), 3) if all_a else None,
        "raw_median_b": round(median(all_b), 3) if all_b else None,
        "sign_consistent": sign_ok,
    }


def bucket_stats(obs, key):
    by = defaultdict(list)
    for x in obs:
        if x["fwd"] is not None:
            by[x[key]].append(x["fwd"])
    rows = []
    for sc in sorted(by):
        v = by[sc]
        rows.append({
            "score": sc, "n": len(v),
            "raw_median": round(median(v), 3),
            "raw_mean": round(mean(v), 3),
            "hit_rate": round(sum(1 for z in v if z > 0) / len(v), 3),
        })
    return rows


def daily_spearman(obs, key, min_n=50):
    by = defaultdict(list)
    for x in obs:
        if x["fwd"] is not None:
            by[x["date"]].append((x[key], x["fwd"]))
    vals = []
    for d, pairs in by.items():
        if len(pairs) < min_n:
            continue
        rho = spearman([p[0] for p in pairs], [p[1] for p in pairs])
        if rho is not None:
            vals.append(rho)
    t, n = tstat(vals)
    return {"mean_rho": round(mean(vals), 4) if vals else None,
            "t": round(t, 3) if t is not None else None, "n_days": n}


def fmt(v, w=8, d=3):
    return "—".rjust(w) if v is None else f"{v:>{w}.{d}f}"


# ────────────────────────── 主流程 ──────────────────────────
def main():
    K = json.load(open(os.path.join(HERE, "data", "klines_long.json"),
                       encoding="utf-8"))
    print(f"═══ round120f 终版裁决 · hop={HOP} · 严格按日配对 ═══")
    obs = build(K, HOP)
    ds = sorted({x["date"] for x in obs})
    print(f"观测 {len(obs):,} 个，{len(ds)} 个交易日（{ds[0]} ~ {ds[-1]}）")
    print(f"配对纪律：单日每组至少 {MIN_GROUP_N} 只才纳入；差 = A − B\n")

    pool_days = defaultdict(list)
    for x in obs:
        pool_days[x["date"]].append(x["fwd"])
    run_med = mean([median(v) for v in pool_days.values()])
    print(f"全池当日中位收益的均值 = {run_med:.3f}% "
          f"→ {'下跌市（绝对收益必然全负，必须看配对差）' if run_med < 0 else '上涨市'}\n")

    R = {"hop": HOP, "n_obs": len(obs), "date_range": [ds[0], ds[-1]],
         "min_group_n": MIN_GROUP_N,
         "pool_daily_median_mean_pct": round(run_med, 3),
         "bar_layout": "[open, close, high, low, volume]",
         "sections": {}}

    # ══ F1 各分档 vs 全池 ══
    print("\n── F1 现行 score_rsi_rev 各分档 − 全池（配对）──")
    print("   t_naive=未校正重叠 / t_NW=lag4 / t_sub=不重叠(每5天取1天)")
    print(f"{'分档':>5} {'n':>8} {'配对天':>6} {'差(日中位)':>11} {'t_naive':>8} "
          f"{'t_NW':>7} {'t_sub':>7} {'方向一致':>8}")
    f1 = []
    for sc in [1.0, 2.0, 3.0, 4.0, 5.0]:
        r = paired(obs,
                   lambda x, sc=sc: x["s_rsi"] == sc,
                   lambda x: True,
                   f"s_rsi={sc:.0f}", "全池",
                   f"score_rsi_rev={sc:.0f} vs 全池")
        f1.append(r)
        print(f"{sc:>5.1f} {r['n_a']:>8} {r['n_days_paired']:>6} "
              f"{fmt(r['diff_median_pp'],11)} {fmt(r['t_median'],8,2)} "
              f"{fmt(r['t_median_nw'],7,2)} {fmt(r['t_median_sub'],7,2)} "
              f"{str(r['sign_consistent']):>8}")
    R["sections"]["F1_rsi_bucket_vs_pool"] = f1

    # 同样给 bias60
    print("\n── F1b score_bias60 各分档 − 全池 ──")
    f1b = []
    for sc in [1.0, 2.0, 3.0, 4.0, 5.0]:
        r = paired(obs, lambda x, sc=sc: x["s_bias"] == sc, lambda x: True,
                   f"s_bias={sc:.0f}", "全池", f"score_bias60={sc:.0f} vs 全池")
        f1b.append(r)
        print(f"{sc:>5.1f} {r['n_a']:>8} {r['n_days_paired']:>6} "
              f"{fmt(r['diff_median_pp'],11)} {fmt(r['t_median'],8,2)} "
              f"{fmt(r['t_median_nw'],7,2)} {fmt(r['t_median_sub'],7,2)} "
              f"{str(r['sign_consistent']):>8}")
    R["sections"]["F1b_bias_bucket_vs_pool"] = f1b

    # ══ F5 Spearman ══
    print("\n── F5 每日 Spearman（分数 vs 未来收益）—— 单调性的统计表达 ──")
    f5 = {}
    for key, lab in [("s_rsi", "score_rsi_rev"), ("s_bias", "score_bias60")]:
        st = daily_spearman(obs, key)
        f5[lab] = st
        print(f"  {lab:18s} mean_rho={fmt(st['mean_rho'],8,4)}  "
              f"t={fmt(st['t'],7,2)}  n_days={st['n_days']}")
    R["sections"]["F5_daily_spearman"] = f5

    print("\n── 各分档绝对收益（量级参考，不作裁决）──")
    for key, lab in [("s_rsi", "score_rsi_rev"), ("s_bias", "score_bias60")]:
        rows = bucket_stats(obs, key)
        print(f"  {lab:18s} " + "  ".join(
            f"{r['score']:.0f}→{r['raw_median']:+.2f}" for r in rows))
        R["sections"].setdefault("buckets_abs", {})[lab] = rows

    # ══ F2 核心：RSI<30 内各前置条件 ══
    print("\n── F2 核心问题：RSI<30 内，各前置条件的增量（差 = 有信号 − 无信号）──")
    print("   t_naive=未校正重叠 / t_NW=lag4 / t_sub=不重叠(每5天取1天，最保守)")
    lo30 = lambda x: x["rsi"] < 30
    f2 = []
    tests2 = [
        ("S1 收盘不创20日新低", lambda x: lo30(x) and not x["new_low"],
         lambda x: lo30(x) and x["new_low"], "不创新低", "创新低"),
        ("S2 站上MA5", lambda x: lo30(x) and x["above5"],
         lambda x: lo30(x) and not x["above5"], "站上MA5", "未站上MA5"),
        ("S3 MA5拐头向上", lambda x: lo30(x) and x["ma5up"],
         lambda x: lo30(x) and not x["ma5up"], "MA5向上", "MA5向下"),
        ("S4 当日收阳", lambda x: lo30(x) and x["yang"],
         lambda x: lo30(x) and not x["yang"], "收阳", "收阴"),
        ("T1 站上MA20", lambda x: lo30(x) and x["above20"],
         lambda x: lo30(x) and not x["above20"], "站上MA20", "未站上MA20"),
        ("T2 MA20向上", lambda x: lo30(x) and x["ma20up"],
         lambda x: lo30(x) and not x["ma20up"], "MA20向上", "MA20向下"),
    ]
    print(f"{'检验':<20} {'配对天':>6} {'差(日中位)':>11} {'t_naive':>8} "
          f"{'t_NW':>7} {'t_sub':>7} {'nA/nB':>14} {'一致':>5}")
    for lab, ma, mb, na, nb in tests2:
        r = paired(obs, ma, mb, na, nb, lab)
        f2.append(r)
        print(f"{lab:<20} {r['n_days_paired']:>6} {fmt(r['diff_median_pp'],11)} "
              f"{fmt(r['t_median'],8,2)} {fmt(r['t_median_nw'],7,2)} "
              f"{fmt(r['t_median_sub'],7,2)} "
              f"{str(r['n_a'])+'/'+str(r['n_b']):>14} "
              f"{str(r['sign_consistent']):>5}")
    R["sections"]["F2_gates_within_rsi30"] = f2

    # ══ F2b 企稳信号个数 ══
    print("\n── F2b RSI<30 内「企稳信号个数」0~4（含不创新低/站上MA5/MA5向上/收阳）──")
    base = [x for x in obs if lo30(x)]
    f2b = []
    for k in range(5):
        sub = [x for x in base if x["n_stab"] == k]
        if not sub:
            continue
        nd = len({x["date"] for x in sub})
        f2b.append({"n_stab": k, "n": len(sub), "days": nd,
                    "raw_median": round(median([x["fwd"] for x in sub]), 3),
                    "raw_mean": round(mean([x["fwd"] for x in sub]), 3)})
        print(f"  {k} 个: n={len(sub):>5} 天={nd:>4} "
              f"中位={median([x['fwd'] for x in sub]):+7.3f}%  "
              f"均值={mean([x['fwd'] for x in sub]):+7.3f}%")
    R["sections"]["F2b_stab_count"] = f2b

    print("  极值对比（4个 vs 0个，配对）:")
    r0 = paired(obs, lambda x: lo30(x) and x["n_stab"] == 4,
                lambda x: lo30(x) and x["n_stab"] == 0,
                "企稳信号4个", "企稳信号0个", "企稳信号 4个 vs 0个")
    print(f"    配对天={r0['n_days_paired']} 差(中位)={fmt(r0['diff_median_pp'],7)} "
          f"t={fmt(r0['t_median'],6,2)}  差(均值)={fmt(r0['diff_mean_pp'],7)} "
          f"t={fmt(r0['t_mean'],6,2)}  n={r0['n_a']}/{r0['n_b']}")
    R["sections"]["F2b_extreme_paired"] = r0

    # ══ F4 生益科技型 ══
    print("\n── F4 生益科技型（RSI<30 且创新低 且 站上MA5/MA5向上/收阳 全为 False）──")
    syn = lambda x: (lo30(x) and x["new_low"]
                     and (not x["above5"]) and (not x["ma5up"]) and (not x["yang"]))
    f4 = [
        paired(obs, syn, lambda x: lo30(x), "生益科技型", "RSI<30 全体",
               "生益科技型 vs RSI<30 全体"),
        paired(obs, syn, lambda x: True, "生益科技型", "全池",
               "生益科技型 vs 全池"),
        paired(obs, lambda x: lo30(x) and not x["new_low"], lambda x: True,
               "RSI<30且不创新低", "全池", "RSI<30且不创新低 vs 全池"),
    ]
    for r in f4:
        print(f"  {r['label']:<22} 配对天={r['n_days_paired']:>4} "
              f"差(中位)={fmt(r['diff_median_pp'],7)} "
              f"t_naive={fmt(r['t_median'],6,2)} t_NW={fmt(r['t_median_nw'],6,2)} "
              f"t_sub={fmt(r['t_median_sub'],6,2)} n={r['n_a']}")
    R["sections"]["F4_shengyi_type"] = f4

    # ══ F3 全池范围 ══
    print("\n── F3 放宽阈值：「不创新低」本身的区分力 ──")
    f3 = [
        paired(obs, lambda x: not x["new_low"], lambda x: x["new_low"],
               "全池不创新低", "全池创新低", "全池 不创新低 vs 创新低"),
        paired(obs, lambda x: x["rsi"] < 40 and not x["new_low"],
               lambda x: x["rsi"] < 40 and x["new_low"],
               "RSI<40不创新低", "RSI<40创新低", "RSI<40 不创新低 vs 创新低"),
        paired(obs, lambda x: x["rsi"] < 50 and not x["new_low"],
               lambda x: x["rsi"] < 50 and x["new_low"],
               "RSI<50不创新低", "RSI<50创新低", "RSI<50 不创新低 vs 创新低"),
        # 盘中口径（最低价创新低）对照 —— 看两种定义是否给出同一结论
        paired(obs, lambda x: not x["new_low_intra"], lambda x: x["new_low_intra"],
               "全池盘中不创新低", "全池盘中创新低", "全池(盘中口径) 不创新低 vs 创新低"),
    ]
    for r in f3:
        print(f"  {r['label']:<24} 配对天={r['n_days_paired']:>4} "
              f"差(中位)={fmt(r['diff_median_pp'],7)} "
              f"t_naive={fmt(r['t_median'],6,2)} t_NW={fmt(r['t_median_nw'],6,2)} "
              f"t_sub={fmt(r['t_median_sub'],6,2)} 一致={r['sign_consistent']}")
    R["sections"]["F3_newlow_poolwide"] = f3

    # ══ F7 结构性互斥：RSI 分档 × 站上MA20 ══
    print("\n── F7 【结构性】RSI 分档 × 站上MA20 交叉表 ──")
    bk = [(0, 20), (20, 30), (30, 40), (40, 50), (50, 60), (60, 70), (70, 101)]
    tab = {b: {"n": 0, "ab": 0} for b in bk}
    min_rsi_ab = None
    for x in obs:
        r = x["rsi"]
        for b in bk:
            if b[0] <= r < b[1]:
                tab[b]["n"] += 1
                if x["above20"]:
                    tab[b]["ab"] += 1
                    if min_rsi_ab is None or r < min_rsi_ab:
                        min_rsi_ab = r
                break
    print(f"{'RSI区间':>10} {'n':>9} {'站上MA20':>9} {'占比':>9}")
    f7 = []
    for b in bk:
        t_ = tab[b]
        pct = 100 * t_["ab"] / t_["n"] if t_["n"] else 0.0
        print(f"{b[0]:>3}~{b[1]:<6} {t_['n']:>9,} {t_['ab']:>9,} {pct:>8.3f}%")
        f7.append({"rsi_lo": b[0], "rsi_hi": b[1], "n": t_["n"],
                   "above_ma20": t_["ab"], "pct": round(pct, 3)})
    print(f"  ★ 全样本中「站上MA20」的最小 RSI = "
          f"{'无可达' if min_rsi_ab is None else f'{min_rsi_ab:.2f}'}")
    _n30 = tab[(20, 30)]["n"] + tab[(0, 20)]["n"]
    _a30 = tab[(20, 30)]["ab"] + tab[(0, 20)]["ab"]
    print(f"  ★ RSI<30 共 {_n30:,} 个样本，其中站上MA20 的 {_a30:,} 个 "
          f"（{100*_a30/_n30 if _n30 else 0:.3f}%）")
    print("  ★ 结论：RSI<30 与「站上MA20」结构性互斥 → "
          "「趋势确认」这条前置条件对低RSI反转因子不可用")
    R["sections"]["F7_structural_exclusion"] = {
        "table": f7, "min_rsi_above_ma20": min_rsi_ab,
        "rsi30_above_ma20": tab[(20, 30)]["ab"] + tab[(0, 20)]["ab"],
        "rsi30_n": tab[(20, 30)]["n"] + tab[(0, 20)]["n"],
    }

    # ══ F6 样本代价 ══
    print("\n── F6 加前置条件的样本代价（RSI<30 子集内）──")
    nd0 = len({x["date"] for x in base})
    print(f"  现行（仅 RSI<30）              n={len(base):>6}  日均 {len(base)/nd0:>5.1f} 只  保留 100.0%")
    f6 = {"current": {"n": len(base), "days": nd0,
                      "avg_per_day": round(len(base) / nd0, 1),
                      "retention_pct": 100.0}}
    for lab, m in [
        ("+ 不创新低", lambda x: not x["new_low"]),
        ("+ 不创新低 + 站上MA5", lambda x: (not x["new_low"]) and x["above5"]),
        ("+ 不创新低 + 站上MA5 + MA5向上",
         lambda x: (not x["new_low"]) and x["above5"] and x["ma5up"]),
        ("+ 不创新低 + 站上MA20", lambda x: (not x["new_low"]) and x["above20"]),
    ]:
        sub = [x for x in base if m(x)]
        nd = len({x["date"] for x in sub})
        avg = round(len(sub) / nd, 1) if nd else 0
        ret = round(100 * len(sub) / len(base), 1)
        print(f"  {lab:<30} n={len(sub):>6}  日均 {avg:>5.1f} 只  保留 {ret:>5.1f}%")
        f6[lab] = {"n": len(sub), "days": nd, "avg_per_day": avg,
                   "retention_pct": ret}
    R["sections"]["F6_sample_cost"] = f6

    json.dump(R, open(OUT, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"\n✅ 已写入 {OUT}")


if __name__ == "__main__":
    main()
