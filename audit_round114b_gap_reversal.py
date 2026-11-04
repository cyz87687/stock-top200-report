#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
audit_round114b_gap_reversal.py —— 严格检验「极端低开 → 日内反转」这个唯一候选信号

═══ 发现来源 ═══

audit_round114 分组时发现一个刺眼的 U 型模式：
  按 gap（今开/昨收−1）分组，日内收益（open→close）呈 U 型：
    gap ≤ −5.44%（p1）  → intra 均值 +2.392%，中位 +1.494%，**胜率 60.1%**（n=1194）
    gap 中间组          → intra 均值 +0.30%，胜率 50.5%
    gap ≥ +6.70%（p99） → intra 均值 −1.286%，胜率 37.4%
  两侧都反转 = 典型的短线过度反应修正。

**这是本轮唯一出现的「日内可执行」候选**（open→close 口径，开盘可下单），
所以必须严格按照项目纪律检验。**盲信这个数字就是找死**，因为：

  ① **阈值是事后定的**：p1 = −5.44% 是在**全体样本**上算的分位数。
     真实交易时你不知道「今天的 −5% 是不是历史 p1」→ 必须用**扩展窗口**
     （只用 t 时点之前的数据定阈值）重算，否则就是前视偏差。
  ② **胜率 60.1% 与中位 +1.49% 都好看，|intra| 均值却高达 5.23%**
     → 方差极大。均值被少数暴涨样本拉动，必须看逐日稳定性。
  ③ **低开 5%+ 的股票天然带事件风险**：利空、跌停、退市预警。
     开盘一字跌停根本买不到 → 必须剔除不可成交样本。
  ④ **多重检验**：这里试了 14 个分位分组 → 按 0.05 显著性期望假阳性 0.7 个。

═══ 六重检验 ═══

  T1 扩展窗口阈值（不用全样本分位，只用历史数据）→ 消灭前视偏差
  T2 扣费净值（ETF 0.06% / 主板 0.16% / 小市值 0.28%）
  T3 剔除不可成交（开盘跌停 / 停牌 / 成交额过低）
  T4 时间折半稳定性（前 50% / 后 50% 同号且显著）
  T5 逐日集中度（是否靠少数几天赚钱）
  T6 股票集中度（是否靠少数几只赚钱）

判据（全过才算真信号）：
  T1 扩展窗口下 |t| ≥ 2.0 且净收益 > 0
  T2 三档成本中至少 ETF 档净 > 0（ETF 是 T0 最现实载体）
  T3 剔除后仍成立
  T4 两半同号且均 |t| ≥ 1.5
  T5 收益不依赖 top 5% 交易日（去掉后仍 > 0）
  T6 收益不依赖 top 5% 股票（去掉后仍 > 0）

输出：audit_round114b_gap_reversal.json
"""
import os
import sys
import json
import math
import argparse
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from kline_units import vol_coef           # noqa: E402  成交量双口径唯一来源

HERE = os.path.dirname(os.path.abspath(__file__))
KL = os.path.join(HERE, "data", "klines_long.json")
OUT = os.path.join(HERE, "audit_round114b_gap_reversal.json")

COST_ETF = 0.0006      # ETF 单次回转 0.06%（round114 三档中的最低档）
COST_MAIN = 0.0016     # 主板大市值 0.16%
COST_SMALL = 0.0028    # 小市值 0.28%

GAP_THR_FIXED = -0.05   # 固定阈值（不做任何拟合，最诚实的反面）
EXPAND_MIN_HIST = 60    # 扩展窗口最少历史天数


def stat(v):
    v = np.asarray(v, float); v = v[np.isfinite(v)]
    if len(v) < 20:
        return None
    mu, sd = float(v.mean()), float(v.std(ddof=1))
    return {"n": int(len(v)), "mean_pct": round(mu * 100, 4),
            "median_pct": round(float(np.median(v)) * 100, 4),
            "win_rate": round(float((v > 0).mean()), 4),
            "std_pct": round(sd * 100, 4),
            "t": round(mu / (sd / math.sqrt(len(v))), 3) if sd > 0 else None}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--min-amt", type=float, default=3e7)
    a = ap.parse_args()

    print(f"读取 {KL} ...")
    kl = json.load(open(KL, encoding="utf-8"))
    codes = sorted(kl.keys())
    alld = sorted({d for c in codes for d in kl[c]})
    di = {d: i for i, d in enumerate(alld)}
    T, N = len(alld), len(codes)
    O = np.full((T, N), np.nan); C = np.full((T, N), np.nan)
    H = np.full((T, N), np.nan); L = np.full((T, N), np.nan)
    A = np.full((T, N), np.nan)
    for j, cd in enumerate(codes):
        for dt, v in kl[cd].items():
            i = di.get(dt)
            if i is None or not isinstance(v, (list, tuple)) or len(v) < 5:
                continue
            try:
                o_, c_, h_, l_, v_ = (float(v[0]), float(v[1]), float(v[2]),
                                      float(v[3]), float(v[4]))
            except (TypeError, ValueError):
                continue
            if not (c_ > 0 and o_ > 0):
                continue
            O[i, j] = o_; C[i, j] = c_; H[i, j] = h_; L[i, j] = l_
            A[i, j] = v_ * vol_coef(cd) * c_
    del kl
    print(f"  {N} 只 × {T} 日")

    prev = np.vstack([np.full((1, N), np.nan), C[:-1]])
    with np.errstate(invalid="ignore", divide="ignore"):
        gap = O / prev - 1.0
        intra = C / O - 1.0

    amt_ok = np.isfinite(A) & (A >= a.min_amt)
    base = (np.isfinite(gap) & np.isfinite(intra) & amt_ok
            & np.isfinite(O) & np.isfinite(C))

    # ── T3 剔除不可成交：开盘一字跌停（open==low 且 gap <= -9.5%）买不到
    #    ⚠️ 保守口径：只要 gap <= -9.5% 就剔除（不区分是否封死），
    #    因为主板 −10% / 创业板科创板 −20%，能开盘买到 −9.5% 的实际概率很低。
    #    这样处理只会让结论更保守，不会让假信号更容易通过。
    untradable = gap <= -0.095
    tradable = base & ~untradable
    print(f"  有效样本 {int(base.sum()):,}，剔除开盘跌停后 {int(tradable.sum()):,}"
          f"（剔除 {int((base & untradable).sum()):,}）")

    res = {}

    # ═══ T0 基准：全样本 gap 分位分组（含前视，仅作对照）═══
    g_all = gap[base]; i_all = intra[base]
    qs = [0, 1, 2, 5, 10, 20, 40, 60, 80, 90, 95, 98, 99, 100]
    edges = np.percentile(g_all, qs)
    buckets = []
    for k in range(len(qs) - 1):
        lo, hi = edges[k], edges[k + 1]
        m = (g_all > lo) & (g_all <= hi)
        if m.sum() > 50:
            s = stat(i_all[m])
            s["gap_lo_pct"] = round(float(lo) * 100, 3)
            s["gap_hi_pct"] = round(float(hi) * 100, 3)
            buckets.append(s)
    res["insample_buckets"] = {"note": "含前视偏差（阈值为全样本分位），仅作模式识别",
                               "buckets": buckets}

    # ═══ T1 固定阈值 −5%（零拟合）═══
    m_fix = tradable & (gap <= GAP_THR_FIXED)
    res["T1_fixed_threshold"] = {
        "rule": f"gap <= {GAP_THR_FIXED*100:.0f}%（固定，不拟合）",
        "all": stat(intra[m_fix]),
        "n_samples": int(m_fix.sum()),
    }
    print(f"\nT1 固定阈值 gap≤{GAP_THR_FIXED*100:.0f}%: n={int(m_fix.sum())} "
          f"{stat(intra[m_fix])}")

    # ═══ T1b 扩展窗口阈值（真·无前视）═══
    #    对每个交易日 t，用 [0, t) 的历史 gap 的 p1/p2/p5 作阈值
    sig_list = []   # (t, j, intra) 被信号触发的样本
    thr_hist = []
    for t in range(EXPAND_MIN_HIST, T - 1):
        if not np.isfinite(intra[t]).any():
            continue
        hist = gap[:t]
        hist = hist[base[:t]]
        if len(hist) < 2000:
            continue
        p1 = float(np.percentile(hist, 1))
        thr_hist.append({"t": t, "date": alld[t], "p1": round(p1, 5)})
        # 用历史 p1 作为当日阈值（扩展窗口，绝不看未来）
        m = tradable[t] & (gap[t] <= p1)
        for j in np.where(m)[0]:
            sig_list.append((t, j, float(intra[t, j]), float(gap[t, j])))

    if sig_list:
        arr = np.array([s[2] for s in sig_list], float)
        s_ext = stat(arr)
        s_ext["n_days_with_signal"] = len({s[0] for s in sig_list})
        s_ext["n_stocks"] = len({s[1] for s in sig_list})
        res["T1b_expanding_threshold"] = {
            "rule": f"gap[t] <= 历史[0,t) 的 p1 分位（扩展窗口，最小 {EXPAND_MIN_HIST} 日）",
            "result": s_ext,
        }
        print(f"T1b 扩展窗口 p1: {s_ext}")
    else:
        s_ext = None
        res["T1b_expanding_threshold"] = {"rule": "n/a", "result": None}
        print("T1b 扩展窗口 p1: 无信号")

    # ═══ T2 扣费净值 ═══
    cost_table = {}
    for name, c in (("etf", COST_ETF), ("main", COST_MAIN), ("small", COST_SMALL)):
        row = {}
        for tag, arr_in in (("fixed", intra[m_fix]),
                            ("expanding", np.array([s[2] for s in sig_list],
                                                   float) if sig_list else np.array([]))):
            v = np.asarray(arr_in, float); v = v[np.isfinite(v)]
            if len(v) < 20:
                row[tag] = None
                continue
            row[tag] = {
                "gross_pct": round(float(v.mean()) * 100, 4),
                "cost_pct": round(c * 100, 4),
                "net_pct": round(float(v.mean()) * 100 - c * 100, 4),
                "net_positive": bool(float(v.mean()) - c > 0),
            }
        cost_table[name] = row
    res["T2_after_cost"] = cost_table
    print("\nT2 扣费净值：")
    for name in cost_table:
        for tag in ("fixed", "expanding"):
            r = cost_table[name].get(tag)
            if r:
                print(f"  {name:<6}{tag:<11} 毛 {r['gross_pct']:>+6.3f}%  "
                      f"成本 {r['cost_pct']:.3f}%  净 {r['net_pct']:>+6.3f}%  "
                      f"{'✓' if r['net_positive'] else '✗'}")

    # ═══ T4 时间折半 ═══
    days_sig = sorted({s[0] for s in sig_list}) if sig_list else []
    half = len(days_sig) // 2 if days_sig else 0
    if half >= 10:
        set1 = set(days_sig[:half]); set2 = set(days_sig[half:])
        v1 = np.array([s[2] for s in sig_list if s[0] in set1], float)
        v2 = np.array([s[2] for s in sig_list if s[0] in set2], float)
        s1, s2 = stat(v1), stat(v2)
        same = bool(s1 and s2 and np.sign(s1["mean_pct"]) == np.sign(s2["mean_pct"]))
        both = bool(s1 and s2 and s1["t"] and s2["t"]
                    and abs(s1["t"]) >= 1.5 and abs(s2["t"]) >= 1.5)
        res["T4_half_split"] = {"first_half": s1, "second_half": s2,
                                "same_sign": same, "both_sig_1_5": both,
                                "split_date": (alld[days_sig[half]] if days_sig else None)}
        print(f"\nT4 折半：前半 {s1}\n         后半 {s2}\n         同号={same} 双显著={both}")
    else:
        res["T4_half_split"] = {"error": "信号日不足"}
        print("\nT4 折半：信号日不足")

    # ═══ T5 逐日集中度：把每日均值当样本，去掉最赚的 5% 日子 ═══
    if sig_list:
        by_day = {}
        for t, j, r, g in sig_list:
            by_day.setdefault(t, []).append(r)
        day_means = np.array([np.mean(v) for v in by_day.values()], float)
        ds = np.sort(day_means)
        cut = max(1, int(len(ds) * 0.05))
        trimmed = ds[:-cut] if cut < len(ds) else ds
        res["T5_day_concentration"] = {
            "n_signal_days": int(len(day_means)),
            "mean_all_pct": round(float(day_means.mean()) * 100, 4),
            "mean_trim_top5pct_pct": round(float(trimmed.mean()) * 100, 4),
            "positive_after_trim": bool(trimmed.mean() > 0),
            "best_day_pct": round(float(ds[-1]) * 100, 4),
            "worst_day_pct": round(float(ds[0]) * 100, 4),
            "top5_days_contribution_pct": round(
                float(ds[-cut:].sum() / day_means.sum() * 100)
                if day_means.sum() != 0 else np.nan, 2),
        }
        print(f"\nT5 逐日：{len(day_means)} 个信号日，全均值 {res['T5_day_concentration']['mean_all_pct']:+.3f}%，"
              f"去掉最赚5%日子后 {res['T5_day_concentration']['mean_trim_top5pct_pct']:+.3f}%")

        # ═══ T6 股票集中度 ═══
        by_stk = {}
        for t, j, r, g in sig_list:
            by_stk.setdefault(j, []).append(r)
        stk_means = np.array([np.mean(v) for v in by_stk.values()], float)
        ss = np.sort(stk_means)
        cut2 = max(1, int(len(ss) * 0.05))
        trimmed2 = ss[:-cut2] if cut2 < len(ss) else ss
        res["T6_stock_concentration"] = {
            "n_signal_stocks": int(len(stk_means)),
            "mean_all_pct": round(float(stk_means.mean()) * 100, 4),
            "mean_trim_top5pct_pct": round(float(trimmed2.mean()) * 100, 4),
            "positive_after_trim": bool(trimmed2.mean() > 0),
            "top5_stocks_contribution_pct": round(
                float(ss[-cut2:].sum() / stk_means.sum() * 100)
                if stk_means.sum() != 0 else np.nan, 2),
        }
        print(f"T6 股票：{len(stk_means)} 只，全均值 {res['T6_stock_concentration']['mean_all_pct']:+.3f}%，"
              f"去掉最赚5%股票后 {res['T6_stock_concentration']['mean_trim_top5pct_pct']:+.3f}%")
    else:
        res["T5_day_concentration"] = {"error": "无信号"}
        res["T6_stock_concentration"] = {"error": "无信号"}

    # ═══ 综合判定 ═══
    chk = {}
    e = res.get("T1b_expanding_threshold", {}).get("result") or {}
    chk["T1_expanding_sig"] = bool(e.get("t") is not None and abs(e["t"]) >= 2.0)
    net_etf = (res.get("T2_after_cost", {}).get("etf", {}).get("expanding") or {})
    chk["T2_etf_net_positive"] = bool(net_etf.get("net_positive"))
    chk["T3_untradable_removed"] = True    # 全部检验均已用 tradable 掩码
    h = res.get("T4_half_split", {})
    chk["T4_half_stable"] = bool(h.get("same_sign") and h.get("both_sig_1_5"))
    chk["T5_day_robust"] = bool(res.get("T5_day_concentration", {})
                                .get("positive_after_trim"))
    chk["T6_stock_robust"] = bool(res.get("T6_stock_concentration", {})
                                  .get("positive_after_trim"))
    n_pass = sum(1 for v in chk.values() if v)

    if n_pass == 6:
        verdict = ("六重检验全部通过 → 「极端低开 → 日内反转」是**候选可执行信号**。"
                   "但样本仅 341 日、单一样本期，须扩样本（≥1500 日）复核后方可上线；"
                   "**当前只能作为「待验证假设」，不得直接用于 T0 实盘信号。**")
    elif n_pass >= 4:
        failed = [k for k, v in chk.items() if not v]
        verdict = (f"六重检验通过 {n_pass}/6，未过：{failed} → "
                   "**不足以支撑 T0 信号**。该模式更像「低开样本自身的高波动特性」"
                   "而非可反复交易的 alpha，需分钟线 + 更长样本才能定论。")
    else:
        failed = [k for k, v in chk.items() if not v]
        verdict = (f"六重检验仅通过 {n_pass}/6，未过：{failed} → "
                   "**判为伪信号**。极端低开组的正收益来自「少数暴涨样本 + 高方差」，"
                   "不是可稳定复现的日内反转效应。T0 仍无可行证据。")

    print(f"\n{'='*74}")
    print("六重检验：")
    for k, v in chk.items():
        print(f"  {'✅' if v else '❌'} {k}")
    print(f"\n通过 {n_pass}/6")
    print(f"\n结论：{verdict}")
    print(f"{'='*74}")

    json.dump({
        "meta": {
            "purpose": "严格检验「极端低开 → 日内反转」这个 T0 候选信号",
            "n_days": int(T), "date_range": [alld[0], alld[-1]],
            "n_codes": int(N),
            "cost_assumptions": {"etf": COST_ETF, "main": COST_MAIN,
                                 "small": COST_SMALL},
            "untradable_rule": ("gap <= -9.5% 视为开盘跌停不可成交并剔除（保守口径，"
                                "不区分是否封死 → 只会让结论更保守）"),
            "six_tests": {
                "T1": "扩展窗口阈值（只用历史数据）下 |t| >= 2.0",
                "T2": "ETF 档扣费后净 > 0",
                "T3": "剔除不可成交样本",
                "T4": "时间折半同号且两半 |t| >= 1.5",
                "T5": "去掉最赚 5% 信号日后仍 > 0",
                "T6": "去掉最赚 5% 股票后仍 > 0",
            },
            "data_limit": ("日线口径只能验证 open→close；真实 T0 还需分钟线确认"
                           "「开盘能否成交」与「盘中滑点」"),
        },
        "n_pass": n_pass,
        "checks": chk,
        "T1_fixed_threshold": res.get("T1_fixed_threshold"),
        "T1b_expanding": res.get("T1b_expanding_threshold"),
        "T2_after_cost": res.get("T2_after_cost"),
        "T4_half_split": res.get("T4_half_split"),
        "T5_day_concentration": res.get("T5_day_concentration"),
        "T6_stock_concentration": res.get("T6_stock_concentration"),
        "insample_buckets": res.get("insample_buckets"),
        "verdict": verdict,
        "must_not_claim": [
            "不得在扩样本（≥1500 日）+ 分钟线验证前把它写进 T0 实盘信号",
            "不得声称已通过 T0 回测（本检验只验证 open→close，无盘中路径）",
            "不得用全样本 p1 阈值描述可执行性（那是前视）。可执行口径是扩展窗口 p1，见 T1b",
        ],
    }, open(OUT, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"\n✅ 已写出 {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
