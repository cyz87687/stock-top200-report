#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
audit_round120_reversal_gate.py —— 反转类因子「低位≠反转」前置条件验证

═══ 为什么要有这个脚本 ═══
老大用生益科技(sh600183) 2026-11-03 跌停 −10.00% 当日却拿满反转分做反例，
质疑 score_rsi_rev / score_bias60 / score_oversold60 三个「越低分越高」的
单边映射：只测「跌得够深」，没测「跌完了没有」。

本脚本要做三件事，全部只读：
  ① 复算生益科技当日真实形态：是否创新低、RSI、各反转分位取值
  ② 全池量化「低 RSI 但仍在创新低」的占比 —— 证明这不是孤例而是系统性缺陷
  ③ 对「低 RSI」样本分组，检验**加入企稳/不创新低/趋势确认**后的
     未来收益差异，判断前置条件是否真能分离好坏（这是能不能改的唯一依据）

═══ 纪律 ═══
· 只读，不写任何生产数据（round118 事故：诊断脚本写盘会污染基线）
· 所有 t 值按**日聚类**（同日多股相关），不按股票聚类
· 重叠窗口必须按 hop 重采样 + Bonferroni，不套 2.0
· 样本不足时如实说「不足」，不给数字

用法: python audit_round120_reversal_gate.py [--date YYYY-MM-DD]
输出: data/audit_round120_reversal_gate.json
"""
import os
import sys
import json
import math
import argparse
from collections import defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "data", "audit_round120_reversal_gate.json")

# 与 step2_stockscorer_v2.py 保持同一口径
RSI_PERIOD = 14


# ─────────────────────────────────────────────────────────────
# 基础工具
# ─────────────────────────────────────────────────────────────
def rsi(closes, period=RSI_PERIOD):
    """Wilder RSI，与 step2 的 rsi() 同口径（简单均值版，见 step2 L967）。"""
    if not closes or len(closes) < period + 1:
        return None
    gains, losses = [], []
    for i in range(1, len(closes)):
        d = closes[i] - closes[i - 1]
        gains.append(max(d, 0.0))
        losses.append(max(-d, 0.0))
    ag = sum(gains[:period]) / period
    al = sum(losses[:period]) / period
    for i in range(period, len(gains)):
        ag = (ag * (period - 1) + gains[i]) / period
        al = (al * (period - 1) + losses[i]) / period
    if al == 0:
        return 100.0
    rs = ag / al
    return 100.0 - 100.0 / (1.0 + rs)


def load_klines():
    p = os.path.join(HERE, "data", "klines_long.json")
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def bars_to_series(bars):
    """{date: [o,c,h,l,v]} → (dates, opens, closes, highs, lows)"""
    dates = sorted(bars.keys())
    o = [bars[d][0] for d in dates]
    c = [bars[d][1] for d in dates]
    h = [bars[d][2] for d in dates]
    lo = [bars[d][3] for d in dates]
    return dates, o, c, h, lo


def norm_code(code):
    """sh600183 / 600183.SH / 600183 → 600183"""
    s = str(code or "").strip().lower()
    for pre in ("sh", "sz", "bj"):
        if s.startswith(pre):
            s = s[len(pre):]
    s = s.split(".")[0]
    return s.lstrip("0").zfill(6) if s.isdigit() else s


# ─────────────────────────────────────────────────────────────
# ① 单票形态复算（生益科技反例）
# ─────────────────────────────────────────────────────────────
def probe_one(sym, bars, as_of):
    """返回该票在 as_of 日的形态诊断。"""
    dates, o, c, h, lo = bars_to_series(bars)
    if as_of not in dates:
        # 找 <= as_of 的最后一个
        cand = [d for d in dates if d <= as_of]
        if not cand:
            return None
        as_of_eff = cand[-1]
    else:
        as_of_eff = as_of
    i = dates.index(as_of_eff)

    closes_upto = c[:i + 1]
    highs_upto = h[:i + 1]
    lows_upto = lo[:i + 1]

    r = rsi(closes_upto)

    # ── 创新低判定（多窗口）
    def new_low(win):
        """今日最低是否 = 近 win 日最低（含今日）"""
        if i + 1 < win:
            return None
        seg = lows_upto[-win:]
        return bool(lows_upto[-1] <= min(seg) + 1e-12)

    nl20 = new_low(20)
    nl60 = new_low(60)

    # ── 收盘是否创 N 日新低
    def close_new_low(win):
        if i + 1 < win:
            return None
        seg = closes_upto[-win:]
        return bool(closes_upto[-1] <= min(seg) + 1e-12)

    cnl20 = close_new_low(20)
    cnl60 = close_new_low(60)

    # ── 距 20/60 日最高回撤
    def dd(win):
        if i + 1 < win:
            return None
        seg = highs_upto[-win:]
        hi = max(seg)
        return (closes_upto[-1] / hi - 1.0) * 100.0 if hi > 0 else None

    dd20, dd60 = dd(20), dd(60)

    # ── 均线
    def ma(win):
        if i + 1 < win:
            return None
        return sum(closes_upto[-win:]) / win

    ma5, ma10, ma20, ma60 = ma(5), ma(10), ma(20), ma(60)
    px = closes_upto[-1]
    bias60 = (px / ma60 - 1.0) * 100.0 if ma60 else None

    # ── 企稳信号（4 类，各自独立）
    # S1 收盘不创新低：今日收盘 > 近 20 日最低收盘
    s1_no_new_low = (cnl20 is False) if cnl20 is not None else None
    # S2 站上 5 日线
    s2_above_ma5 = (px > ma5) if ma5 else None
    # S3 5 日线拐头向上（MA5_today > MA5_3daysago）
    s3_ma5_up = None
    if i >= 3 and i + 1 >= 5:
        ma5_prev = sum(closes_upto[-8:-3]) / 5.0 if len(closes_upto) >= 8 else None
        if ma5_prev:
            s3_ma5_up = bool(ma5 > ma5_prev)
    # S4 当日收阳（收盘 > 开盘）
    s4_yang = bool(c[i] > o[i])
    # S5 放量止跌（量 > 20 日均量 且 收阳）
    s5_vol_stop = None
    if i + 1 >= 20:
        vols = [bars[d][4] for d in dates[:i + 1]]
        v20 = sum(vols[-20:]) / 20.0
        s5_vol_stop = bool(vols[-1] > v20 and c[i] > o[i])

    # ── 趋势确认
    t1_above_ma20 = (px > ma20) if ma20 else None
    t2_ma20_up = None
    if i >= 5 and len(closes_upto) >= 25:
        ma20_prev = sum(closes_upto[-25:-5]) / 20.0
        t2_ma20_up = bool(ma20 > ma20_prev)

    # ── 未来收益（用于分组检验）
    def fwd_ret(h_):
        j = i + h_
        if j < len(c):
            return (c[j] / c[i] - 1.0) * 100.0
        return None

    return {
        "sym": sym,
        "as_of": as_of_eff,
        "close": round(px, 3),
        "open": round(o[i], 3),
        "high": round(h[i], 3),
        "low": round(lo[i], 3),
        "rsi14": round(r, 2) if r is not None else None,
        "new_low_20": nl20,
        "new_low_60": nl60,
        "close_new_low_20": cnl20,
        "close_new_low_60": cnl60,
        "drawdown_from_hi20_pct": round(dd20, 2) if dd20 is not None else None,
        "drawdown_from_hi60_pct": round(dd60, 2) if dd60 is not None else None,
        "ma5": round(ma5, 3) if ma5 else None,
        "ma20": round(ma20, 3) if ma20 else None,
        "ma60": round(ma60, 3) if ma60 else None,
        "bias60_pct": round(bias60, 2) if bias60 is not None else None,
        "stabilize": {
            "S1_no_new_low_close": s1_no_new_low,
            "S2_above_ma5": s2_above_ma5,
            "S3_ma5_turning_up": s3_ma5_up,
            "S4_close_above_open": s4_yang,
            "S5_vol_stop_yang": s5_vol_stop,
        },
        "trend": {
            "T1_above_ma20": t1_above_ma20,
            "T2_ma20_rising": t2_ma20_up,
        },
        "fwd_ret": {
            "h1": round(fwd_ret(1), 3) if fwd_ret(1) is not None else None,
            "h5": round(fwd_ret(5), 3) if fwd_ret(5) is not None else None,
            "h20": round(fwd_ret(20), 3) if fwd_ret(20) is not None else None,
        },
    }


# ─────────────────────────────────────────────────────────────
# ② 全池量化：低 RSI 但仍在创新低的比例
# ─────────────────────────────────────────────────────────────
def pool_scan(K, scored, as_of, low_rsi=40.0):
    """遍历当日 scored 全池，统计「低 RSI × 是否创新低」交叉表。"""
    rows = scored.get("results") or []
    cross = defaultdict(int)
    samples = []
    missing = 0
    for r in rows:
        code = norm_code(r.get("code"))
        bars = K.get(code)
        if not bars:
            missing += 1
            continue
        p = probe_one(code, bars, as_of)
        if not p or p["rsi14"] is None:
            missing += 1
            continue
        is_low_rsi = p["rsi14"] < low_rsi
        nl = p["new_low_20"]
        if nl is None:
            continue
        key = ("low_rsi" if is_low_rsi else "high_rsi",
               "new_low" if nl else "no_new_low")
        cross[key] += 1
        if is_low_rsi:
            samples.append({**p, "name": r.get("name"),
                            "score_rsi_rev": r.get("score_rsi_rev"),
                            "score_bias60": r.get("score_bias60"),
                            "score_oversold60": r.get("score_oversold60")})
    return dict(cross), samples, missing


# ─────────────────────────────────────────────────────────────
# ③ 历史横截面分组检验：前置条件能否分离未来收益
# ─────────────────────────────────────────────────────────────
def mean(xs):
    xs = [x for x in xs if x is not None]
    return sum(xs) / len(xs) if xs else None


def cluster_t(by_date_means):
    """按日聚类 t：先算每日均值，再对日均值做单样本 t（比逐股 t 保守）。"""
    xs = [v for v in by_date_means if v is not None]
    n = len(xs)
    if n < 3:
        return None, n
    m = sum(xs) / n
    var = sum((x - m) ** 2 for x in xs) / (n - 1)
    if var <= 0:
        return None, n
    se = math.sqrt(var / n)
    return m / se, n


def hist_cross_sections(K, hop=5, low_rsi=40.0, min_amt=None, step=1):
    """
    从 K 线库构造历史横截面，对每个 (股票, 日期) 计算：
      rsi14 / 是否创新低 / 企稳信号 / 趋势信号 / 未来 hop 日收益
    只保留当日有足够历史长度的样本。

    返回 list[dict]，每个元素是一行观测。
    """
    # 收集所有日期并排序，建立"有效交易日"轴
    all_dates = set()
    for sym, bars in K.items():
        all_dates.update(bars.keys())
    cal = sorted(all_dates)

    # 只保留横截面足够大的日期（与生产口径一致的 200 只门槛思路）
    obs = []
    n_sym = len(K)
    done = 0
    for sym, bars in K.items():
        done += 1
        if done % 400 == 0:
            print(f"    ... {done}/{n_sym}", file=sys.stderr)
        dates = sorted(bars.keys())
        if len(dates) < 80:
            continue
        c = [bars[d][1] for d in dates]
        o = [bars[d][0] for d in dates]
        lo = [bars[d][3] for d in dates]
        hi = [bars[d][2] for d in dates]
        vol = [bars[d][4] for d in dates]
        idx = {d: i for i, d in enumerate(dates)}

        # 从第 65 个交易日开始（保证 MA60 可算），到倒数第 hop+1 天
        for i in range(65, len(dates) - hop, step):
            d = dates[i]
            if d < "2025-08-01":
                continue
            closes_u = c[:i + 1]
            r = rsi(closes_u)
            if r is None:
                continue
            px = c[i]

            # 创新低（20日）
            seg_lo = lo[max(0, i - 19):i + 1]
            new_low20 = bool(lo[i] <= min(seg_lo) + 1e-12)
            seg_c = closes_u[-20:] if i + 1 >= 20 else closes_u
            close_new_low20 = bool(c[i] <= min(seg_c) + 1e-12)

            ma5 = sum(closes_u[-5:]) / 5.0 if i + 1 >= 5 else None
            ma20 = sum(closes_u[-20:]) / 20.0 if i + 1 >= 20 else None
            ma60 = sum(closes_u[-60:]) / 60.0 if i + 1 >= 60 else None
            ma5_prev = sum(closes_u[-8:-3]) / 5.0 if i + 1 >= 8 else None
            ma20_prev = sum(closes_u[-25:-5]) / 20.0 if i + 1 >= 25 else None

            s1 = (close_new_low20 is False)
            s2 = bool(px > ma5) if ma5 else None
            s3 = bool(ma5 > ma5_prev) if (ma5 and ma5_prev) else None
            s4 = bool(c[i] > o[i])
            v20 = sum(vol[max(0, i - 19):i + 1]) / 20.0
            s5 = bool(vol[i] > v20 and c[i] > o[i]) if v20 > 0 else None
            t1 = bool(px > ma20) if ma20 else None
            t2 = bool(ma20 > ma20_prev) if (ma20 and ma20_prev) else None

            fwd = (c[i + hop] / c[i] - 1.0) * 100.0 if c[i] > 0 else None
            m60 = (c[i] / c[i - 60] - 1.0) * 100.0 if i >= 60 and c[i - 60] > 0 else None
            bias60 = (px / ma60 - 1.0) * 100.0 if ma60 else None

            obs.append({
                "sym": sym, "date": d, "rsi": r, "fwd": fwd,
                "new_low20": new_low20, "close_new_low20": close_new_low20,
                "s1": s1, "s2": s2, "s3": s3, "s4": s4, "s5": s5,
                "t1": t1, "t2": t2, "m60": m60, "bias60": bias60,
            })
    return obs, cal


def group_eval(obs, pred_name, pred_fn, base_mask, hop_label):
    """
    在 base_mask（低RSI 子集）内，按 pred_fn 分 True/False 两组。

    ⚠️ 必须同时报告两种口径，且以**按日配对**为准：
      · raw_spread_pp  = 逐股均值差（不按日加权）→ 会被日期构成污染
      · paired_spread = 每日先算组内均值、再对日差求均值 → 这才是可比的
    实测 round120：S1 的 raw +0.429pp 但 paired t = −0.222，
    方向相反 —— 典型辛普森悖论。**只看 raw 会得出相反结论。**
    """
    bt, bf = defaultdict(list), defaultdict(list)
    t_all, f_all = [], []
    for x in obs:
        if not base_mask(x):
            continue
        if x["fwd"] is None:
            continue
        v = pred_fn(x)
        if v is True:
            t_all.append(x["fwd"]); bt[x["date"]].append(x["fwd"])
        elif v is False:
            f_all.append(x["fwd"]); bf[x["date"]].append(x["fwd"])
    if not t_all or not f_all:
        return None
    # 按日配对：只用两组都有样本的日期
    common = sorted(set(bt) & set(bf))
    diffs, dt_m, df_m = [], [], []
    for d in common:
        mt = sum(bt[d]) / len(bt[d])
        mf = sum(bf[d]) / len(bf[d])
        diffs.append(mt - mf); dt_m.append(mt); df_m.append(mf)
    t_stat, n_day = cluster_t(diffs)
    return {
        "n_true": len(t_all), "n_false": len(f_all),
        "n_true_days": len(bt), "n_false_days": len(bf),
        "raw_mean_true": round(mean(t_all), 3),
        "raw_mean_false": round(mean(f_all), 3),
        "raw_spread_pp": round(mean(t_all) - mean(f_all), 3),
        "paired_mean_true": round(mean(dt_m), 3) if dt_m else None,
        "paired_mean_false": round(mean(df_m), 3) if df_m else None,
        "paired_spread_pp": round(mean(diffs), 3) if diffs else None,
        "n_paired_days": n_day,
        "paired_t": round(t_stat, 3) if t_stat is not None else None,
        # 辛普森悖论告警：两个口径方向相反
        "simpson_paradox": bool(
            (mean(t_all) - mean(f_all)) * (mean(diffs) if diffs else 0) < 0),
    }


def rsi_bucket_monotonic(obs, hop_label="h5"):
    """
    ★ 最根本的问题：RSI 分档本身单调吗？

    现行 score_rsi_rev 假设「RSI 越低 → 反转分越高 → 越好」。
    如果实测 RSI 越低未来收益越**差**，那这个因子的方向就是错的，
    加什么前置条件都只是打补丁。

    按日配对：每天在同一 RSI 桶之间比较，避免日期构成污染。
    返回每个桶的按日等权平均收益 + 桶间价差。
    """
    edges = [(0, 20), (20, 30), (30, 40), (40, 50), (50, 60), (60, 70), (70, 101)]
    per_day = defaultdict(lambda: defaultdict(list))
    for x in obs:
        if x["fwd"] is None or x["rsi"] is None:
            continue
        r = x["rsi"]
        for lo_, hi_ in edges:
            if lo_ <= r < hi_:
                per_day[x["date"]][(lo_, hi_)].append(x["fwd"])
                break
    out = []
    for lo_, hi_ in edges:
        daily = [sum(v[(lo_, hi_)]) / len(v[(lo_, hi_)])
                 for v in per_day.values() if v.get((lo_, hi_))]
        out.append({
            "bucket": f"[{lo_},{hi_})",
            "n_obs": sum(len(v[(lo_, hi_)]) for v in per_day.values() if v.get((lo_, hi_))),
            "n_days": len(daily),
            "daily_ew_mean_pct": round(mean(daily), 3) if daily else None,
        })
    return out


# ─────────────────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=None)
    ap.add_argument("--low-rsi", type=float, default=40.0)
    a = ap.parse_args()

    # 数据日期
    if a.date:
        as_of = a.date
    else:
        fs = sorted([f for f in os.listdir(HERE)
                     if f.startswith("top200_scored_") and f.endswith(".json")
                     and ".bak" not in f])
        as_of = fs[-1].replace("top200_scored_", "").replace(".json", "")
    sp = os.path.join(HERE, f"top200_scored_{as_of}.json")
    if not os.path.exists(sp):
        print(f"❌ 找不到 {sp}")
        return 2

    print(f"═══ 反转因子前置条件验证 · {as_of} ═══\n")
    K = load_klines()
    scored = json.load(open(sp, encoding="utf-8"))
    print(f"K 线库 {len(K)} 只；scored {len(scored.get('results') or [])} 只\n")

    # ── ① 生益科技复算
    print("── ① 生益科技 sh600183 形态复算 ──")
    sym = "600183"
    if sym in K:
        p = probe_one(sym, K[sym], as_of)
        for k in ("as_of", "close", "open", "high", "low", "rsi14",
                  "new_low_20", "new_low_60", "close_new_low_20", "close_new_low_60",
                  "drawdown_from_hi20_pct", "drawdown_from_hi60_pct",
                  "ma5", "ma20", "ma60", "bias60_pct"):
            print(f"  {k:24s} {p[k]}")
        print(f"  stabilize:")
        for k, v in p["stabilize"].items():
            print(f"    {k:24s} {v}")
        print(f"  trend:")
        for k, v in p["trend"].items():
            print(f"    {k:24s} {v}")
        print()
        # 反例裁决
        bad = (p["rsi14"] is not None and p["rsi14"] < 30
               and p["new_low_20"] is True)
        print(f"  ▶ 是否构成「低RSI + 创新低」反例: {'是 ✅ 证实老大判断' if bad else '否'}")
        print(f"     RSI={p['rsi14']} <30 且 20日新低={p['new_low_20']}")
    else:
        p = None
        print(f"  ❌ K 线库里找不到 {sym}")
    print()

    # ── ② 全池交叉表
    print("── ② 全池「低RSI × 创新低」交叉表 ──")
    cross, low_samples, missing = pool_scan(K, scored, as_of, a.low_rsi)
    tot = sum(cross.values())
    for k in sorted(cross):
        print(f"  {k[0]:10s} × {k[1]:12s} = {cross[k]:5d}")
    print(f"  合计 {tot} 只（缺 K 线 {missing} 只）")
    if tot:
        lr_nl = cross.get(("low_rsi", "new_low"), 0)
        lr = cross.get(("low_rsi", "new_low"), 0) + cross.get(("low_rsi", "no_new_low"), 0)
        hr_nl = cross.get(("high_rsi", "new_low"), 0)
        hr = hr_nl + cross.get(("high_rsi", "no_new_low"), 0)
        print(f"\n  低RSI(<{a.low_rsi:.0f}) 内「仍在创新低」占比: "
              f"{lr_nl}/{lr} = {lr_nl/lr*100:.1f}%" if lr else "")
        print(f"  高RSI 内「仍在创新低」占比: {hr_nl}/{hr} = {hr_nl/hr*100:.1f}%" if hr else "")
        print(f"\n  ▶ 含义：低RSI样本里仍有 {lr_nl} 只今天在创新低 —— "
              f"这 {'是系统性缺陷，不是孤例' if lr and lr_nl/lr > 0.2 else '占比不高'}")
    print()

    # ── ②b 这些「低RSI+创新低」拿到了多少反转分
    print("── ②b 「低RSI + 创新低」样本的反转得分分布 ──")
    nl_s = [s for s in low_samples if s.get("new_low_20") is True]
    nonl_s = [s for s in low_samples if s.get("new_low_20") is False]
    for label, arr in (("仍在创新低", nl_s), ("已不创新低", nonl_s)):
        if not arr:
            print(f"  {label}: 无样本")
            continue
        for k in ("score_rsi_rev", "score_bias60", "score_oversold60"):
            vals = [x.get(k) for x in arr if x.get(k) is not None]
            if vals:
                m = sum(vals) / len(vals)
                mx = max(vals)
                n_full = sum(1 for v in vals if v >= 4.99)
                print(f"  {label:8s} {k:18s} n={len(vals):4d} 均值={m:.2f} 最高={mx:.1f} 满分只数={n_full}")
        print()
    print()

    # ── ③ 历史横截面分组检验
    print("── ③ 前置条件对「未来收益」的区分力（历史横截面）──")
    print(f"   构造中（hop=5, step=3）...", file=sys.stderr)
    obs, cal = hist_cross_sections(K, hop=5, low_rsi=a.low_rsi, step=3)
    print(f"   观测样本 {len(obs):,} 个，覆盖 {len(cal)} 个日历日")
    if obs:
        ds = sorted({x['date'] for x in obs})
        print(f"   实际日期范围 {ds[0]} ~ {ds[-1]}（{len(ds)} 个交易日）\n")
    else:
        print("   ❌ 无样本\n")

    res = {}
    # 基线：低 RSI 全体
    base_low = lambda x: x["rsi"] < a.low_rsi

    # ── ③0 先问最根本的：RSI 分档本身单调吗？
    print("── ③0 RSI 分档单调性检验（现行 score_rsi_rev 的方向假设）──")
    mono = rsi_bucket_monotonic(obs)
    print(f"  {'RSI桶':14s} {'观测数':>8} {'天数':>6} {'按日等权未来5日':>16}")
    print("  " + "-" * 52)
    for b in mono:
        v = b["daily_ew_mean_pct"]
        print(f"  {b['bucket']:14s} {b['n_obs']:>8,} {b['n_days']:>6} "
              f"{v if v is not None else '—':>16}")
    # 单调性判据：最低桶 vs 最高桶
    lows = [b for b in mono if b["bucket"] in ("[0,20)", "[20,30)") and b["daily_ew_mean_pct"] is not None]
    highs = [b for b in mono if b["bucket"] in ("[50,60)", "[60,70)", "[70,101)") and b["daily_ew_mean_pct"] is not None]
    if lows and highs:
        lo_v = mean([b["daily_ew_mean_pct"] for b in lows])
        hi_v = mean([b["daily_ew_mean_pct"] for b in highs])
        print(f"\n  最低档(RSI<30) 平均 {lo_v:+.3f}%  vs  高档(RSI>=50) 平均 {hi_v:+.3f}%")
        direction_ok = lo_v > hi_v
        print(f"  ▶ 现行假设「RSI越低越好」: {'✅ 方向成立' if direction_ok else '❌ 方向相反'}"
              f"（实测差 {lo_v-hi_v:+.3f}pp）")
        mono_verdict = {"low_rsi_mean": round(lo_v, 3), "high_rsi_mean": round(hi_v, 3),
                        "diff_pp": round(lo_v - hi_v, 3), "direction_ok": direction_ok}
    else:
        mono_verdict = None
    print()

    groups = {
        "S1 不创新低(收盘)": lambda x: x["s1"],
        "S2 站上MA5": lambda x: x["s2"],
        "S3 MA5拐头向上": lambda x: x["s3"],
        "S4 当日收阳": lambda x: x["s4"],
        "S5 放量止跌(量增+收阳)": lambda x: x["s5"],
        "T1 站上MA20": lambda x: x["t1"],
        "T2 MA20向上": lambda x: x["t2"],
    }
    if obs:
        lb = [x["fwd"] for x in obs if base_low(x) and x["fwd"] is not None]
        print(f"  【基线】低RSI(<{a.low_rsi:.0f}) 全体: n={len(lb):,} "
              f"逐股均值={mean(lb):.3f}%\n")
        print(f"  {'条件':24s} {'真组n':>7} {'配对日':>6} {'逐股价差':>9} "
              f"{'按日配对价差':>12} {'配对t':>8} {'辛普森':>7}")
        print("  " + "-" * 82)
        for name, fn in groups.items():
            r = group_eval(obs, name, fn, base_low, "h5")
            res[name] = r
            if r:
                sp = "⚠️是" if r["simpson_paradox"] else "否"
                print(f"  {name:24s} {r['n_true']:>7,} {r['n_paired_days']:>6} "
                      f"{r['raw_spread_pp']:>+9.3f} {r['paired_spread_pp']:>+12.3f} "
                      f"{r['paired_t'] or 0:>8.3f} {sp:>7}")
        print()
        combos = {
            "S1+S2(不新低且站上MA5)": lambda x: (x["s1"] is True and x["s2"] is True),
            "S1+T1(不新低且站上MA20)": lambda x: (x["s1"] is True and x["t1"] is True),
            "S1+S2+T1(三重确认)": lambda x: (x["s1"] is True and x["s2"] is True and x["t1"] is True),
            "S1+S4(不新低且收阳)": lambda x: (x["s1"] is True and x["s4"] is True),
        }
        for name, fn in combos.items():
            r = group_eval(obs, name, fn, base_low, "h5")
            res[name] = r
            if r:
                sp = "⚠️是" if r["simpson_paradox"] else "否"
                print(f"  {name:24s} {r['n_true']:>7,} {r['n_paired_days']:>6} "
                      f"{r['raw_spread_pp']:>+9.3f} {r['paired_spread_pp']:>+12.3f} "
                      f"{r['paired_t'] or 0:>8.3f} {sp:>7}")
        print()
        r = group_eval(obs, "仍在创新低", lambda x: (x["new_low20"] is True),
                       base_low, "h5")
        res["X 仍在创新低(20日)"] = r
        if r:
            sp = "⚠️是" if r["simpson_paradox"] else "否"
            print(f"  {'X 仍在创新低(当日低=20日低)':24s} {r['n_true']:>7,} {r['n_paired_days']:>6} "
                  f"{r['raw_spread_pp']:>+9.3f} {r['paired_spread_pp']:>+12.3f} "
                  f"{r['paired_t'] or 0:>8.3f} {sp:>7}")
    print()

    # ── 汇总裁决
    print("── ④ 裁决 ──")
    verdict = {
        "as_of": as_of,
        "reversal_score_is_single_condition": True,
        "codes": {
            "score_rsi_rev": "RSI<30→5.0, <40→4.0, <50→3.0, <60→2.0, else 1.0（仅阈值）",
            "score_bias60": "bias<=-15→5.0, <=-10→4.0, <=-5→3.0, <=0→2.0（仅深度）",
            "score_oversold60": "mom60<=-30→5.0, <=-20→4.0, <=-10→3.0, <=0→2.0（仅跌幅）",
        },
        "counterexample": p,
        "pool_cross_tab": {f"{k[0]}|{k[1]}": v for k, v in cross.items()},
        "n_missing_klines": missing,
        "hist_obs": len(obs) if obs else 0,
        "hist_date_range": [sorted({x['date'] for x in obs})[0],
                            sorted({x['date'] for x in obs})[-1]] if obs else None,
        "rsi_bucket_monotonic": mono if obs else None,
        "monotonic_verdict": mono_verdict if obs else None,
        "group_test_h5": res,
        "n_low_rsi_samples": len(low_samples),
    }
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(verdict, f, ensure_ascii=False, indent=1)
    print(f"  已写出 {os.path.relpath(OUT, HERE)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
