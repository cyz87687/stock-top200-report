#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
audit_round114d_gap_signal_nature.py —— 判定「极端低开日内反转」到底是什么东西

═══ 前置结论 ═══
round114b/114c：扩展窗口 p1 信号，1361 样本 / 209 信号日，
按日聚类 t=4.112、beta 中性后超额 +1.243% t=3.595 → 不是纯 beta。

**但「不是 beta」不等于「可交易的 alpha」**。这个信号有三个身份嫌疑，
必须逐一排除，否则写进 T+1/T0 建议就是误导：

  嫌疑 A：「跌停撬板」效应
     A 股经典现象：利空跌停后撬板，日内大幅反弹。
     若收益全部来自 **20cm 板块（创业板/科创板）接近跌停的样本**，那这本质是
     「跌停板流动性溢价」——真实交易需要抢筹、排队、Level-2 判断封单，
     散户以开盘价成交的概率极低。
     → 检验：按板块（主板 10cm / 创业板 20cm / 科创板 20cm）拆分，
       并看「低开幅度 / 该板块跌停幅度」的比例分布。

  嫌疑 B：「市场大跌日反弹」的代理
     信号触发 209/304 天 = 69% 的交易日，频率太高（真"极端"不该这么常见）。
     扩展窗口 p1 阈值会随下跌市自动放宽 → 下跌市里信号激增，
     而下跌市的低开反弹本就最强。
     → 检验：信号日 vs 非信号日的**市场当日收益**、以及 p1 阈值的时间漂移。

  嫌疑 C：开盘价不可成交
     gap = open/prev_close − 1，open 是集合竞价价。散户想「以开盘价买入」
     需要 9:25 前挂单且排队靠前；对于低开 5% 的恐慌股，卖单薄、
     但买盘排队极长。**这个摩擦无法用日线量化，但可以通过
     「gap 越极端、收益越高」的单调性间接暴露**——若强单调，
     说明收益来自越难成交的样本（好消息不成立）。
     → 检验：低开幅度分档的收益单调性。

═══ 输出 ═══
  board_split      各板块信号数 / 收益 / 占全样本比例
  limit_ratio      低开幅度相对该板块跌停幅度的分布（>0.8 = 接近跌停）
  market_state     信号日 vs 非信号日的市场收益、下跌日占比
  thr_drift        p1 阈值时间序列（检查放宽）
  monotonic        低开分档收益单调性

输出：audit_round114d_gap_signal_nature.json
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
OUT = os.path.join(HERE, "audit_round114d_gap_signal_nature.json")

EXPAND_MIN_HIST = 60


def stat(v, label=""):
    v = np.asarray(v, float); v = v[np.isfinite(v)]
    if len(v) < 8:
        return None
    mu, sd = float(v.mean()), float(v.std(ddof=1))
    return {"label": label, "n": int(len(v)),
            "mean_pct": round(mu * 100, 4),
            "median_pct": round(float(np.median(v)) * 100, 4),
            "win_rate": round(float((v > 0).mean()), 4),
            "t": round(mu / (sd / math.sqrt(len(v))), 3) if sd > 0 else None}


def board_of(code):
    """按代码前缀判板块 → 涨跌停幅度。"""
    c = code.lower()
    num = c[2:] if c[:2] in ("sh", "sz", "bj") else c
    if num.startswith("688"):
        return "科创板(20cm)", 0.20
    if num.startswith("30"):
        return "创业板(20cm)", 0.20
    if num.startswith(("60", "00")):
        return "主板(10cm)", 0.10
    if num.startswith(("8", "4", "9")):
        return "北交所(30cm)", 0.30
    return "其他", 0.10


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
    A = np.full((T, N), np.nan)
    for j, cd in enumerate(codes):
        for dt, v in kl[cd].items():
            i = di.get(dt)
            if i is None or not isinstance(v, (list, tuple)) or len(v) < 5:
                continue
            try:
                o_, c_, v_ = float(v[0]), float(v[1]), float(v[4])
            except (TypeError, ValueError):
                continue
            if not (c_ > 0 and o_ > 0):
                continue
            O[i, j] = o_; C[i, j] = c_; A[i, j] = v_ * vol_coef(cd) * c_
    del kl
    print(f"  {N} 只 × {T} 日")

    boards = np.array([board_of(c)[0] for c in codes])
    limits = np.array([board_of(c)[1] for c in codes])

    prev = np.vstack([np.full((1, N), np.nan), C[:-1]])
    with np.errstate(invalid="ignore", divide="ignore"):
        gap = O / prev - 1.0
        intra = C / O - 1.0

    amt_ok = np.isfinite(A) & (A >= a.min_amt)
    base = (np.isfinite(gap) & np.isfinite(intra) & amt_ok)
    tradable = base & ~(gap <= -0.095)

    mkt = np.full(T, np.nan)
    for t in range(T):
        m = tradable[t]
        if m.sum() >= 50:
            mkt[t] = float(np.nanmean(intra[t][m]))

    # 生成信号（同 round114b/114c 口径）
    sig = []
    thr_series = []
    for t in range(EXPAND_MIN_HIST, T - 1):
        if not np.isfinite(intra[t]).any():
            continue
        hist = gap[:t][base[:t]]
        if len(hist) < 2000:
            continue
        p1 = float(np.percentile(hist, 1))
        thr_series.append({"date": alld[t], "t": t, "p1_pct": round(p1 * 100, 3)})
        m = tradable[t] & (gap[t] <= p1)
        for j in np.where(m)[0]:
            sig.append({"t": t, "j": int(j), "intra": float(intra[t, j]),
                        "gap": float(gap[t, j]), "board": boards[j],
                        "lim": float(limits[j])})
    if not sig:
        print("❌ 无信号"); return 1
    print(f"  信号 {len(sig)} 样本 / {len({s['t'] for s in sig})} 日")

    # ═══ 嫌疑 A：板块拆分 ═══
    board_res = {}
    for b in sorted({s["board"] for s in sig}):
        ss = [s for s in sig if s["board"] == b]
        v = np.array([s["intra"] for s in ss], float)
        # 按日聚类的板块内 t
        byd = {}
        for s in ss:
            byd.setdefault(s["t"], []).append(s["intra"])
        dm = np.array([np.mean(x) for x in byd.values()], float)
        r = stat(v, b)
        if r:
            r["share_of_samples"] = round(len(ss) / len(sig), 4)
            r["day_cluster_t"] = stat(dm).get("t") if len(dm) >= 8 else None
            r["n_days"] = int(len(dm))
            # 相对跌停幅度的比例
            ratios = np.array([abs(s["gap"]) / s["lim"] for s in ss], float)
            r["limit_ratio_median"] = round(float(np.median(ratios)), 4)
            r["limit_ratio_p90"] = round(float(np.quantile(ratios, 0.9)), 4)
            r["frac_near_limit_ge_0_8"] = round(float((ratios >= 0.8).mean()), 4)
            board_res[b] = r

    # ═══ 嫌疑 B：市场状态 ═══
    sig_days = sorted({s["t"] for s in sig})
    nonsig_days = [t for t in range(EXPAND_MIN_HIST, T - 1)
                   if np.isfinite(mkt[t]) and t not in set(sig_days)]
    m_sig = np.array([mkt[t] for t in sig_days if np.isfinite(mkt[t])], float)
    m_non = np.array([mkt[t] for t in nonsig_days], float)
    market_state = {
        "signal_days": {"n": int(len(m_sig)),
                        "mkt_intra_mean_pct": round(float(m_sig.mean()) * 100, 4)
                        if len(m_sig) else None,
                        "down_day_frac": round(float((m_sig < 0).mean()), 4)
                        if len(m_sig) else None},
        "nonsignal_days": {"n": int(len(m_non)),
                           "mkt_intra_mean_pct": round(float(m_non.mean()) * 100, 4)
                           if len(m_non) else None,
                           "down_day_frac": round(float((m_non < 0).mean()), 4)
                           if len(m_non) else None},
        "signal_day_freq": round(len(sig_days) / (len(sig_days) + len(nonsig_days)), 4),
    }

    # p1 阈值漂移
    tv = np.array([x["p1_pct"] for x in thr_series], float)
    thr_drift = {
        "first_60d_mean_pct": round(float(tv[:60].mean()), 3) if len(tv) >= 60 else None,
        "last_60d_mean_pct": round(float(tv[-60:].mean()), 3) if len(tv) >= 60 else None,
        "min_pct": round(float(tv.min()), 3), "max_pct": round(float(tv.max()), 3),
        "std_pct": round(float(tv.std(ddof=1)), 3),
        "drift_note": ("p1 阈值随历史低开样本增多而漂移；若后段明显放宽，"
                       "说明下跌市信号激增、样本非平稳。"),
    }

    # ═══ 嫌疑 C：低开幅度分档单调性 ═══
    gv = np.array([s["gap"] for s in sig], float)
    iv = np.array([s["intra"] for s in sig], float)
    ratio = np.abs(gv) / np.array([s["lim"] for s in sig], float)
    mono = []
    for lo, hi, lab in [(0, 0.3, "0~30%跌停幅度"), (0.3, 0.5, "30~50%"),
                        (0.5, 0.7, "50~70%"), (0.7, 0.85, "70~85%"),
                        (0.85, 0.95, "85~95%"), (0.95, 1.01, "95%+（近跌停）")]:
        m = (ratio >= lo) & (ratio < hi)
        if m.sum() >= 8:
            r = stat(iv[m], lab)
            if r:
                r["ratio_bucket"] = lab
                mono.append(r)
    mono_means = [r["mean_pct"] for r in mono]

    print(f"\n{'='*78}")
    print("嫌疑 A：板块拆分")
    for b, r in board_res.items():
        print(f"  {b:<14} n={r['n']:>4} ({r['share_of_samples']*100:>5.1f}%)  "
              f"均值 {r['mean_pct']:>+6.3f}%  胜率 {r['win_rate']*100:>5.1f}%  "
              f"按日t={r['day_cluster_t']}  近跌停占比 {r['frac_near_limit_ge_0_8']*100:.1f}%")

    print(f"\n嫌疑 B：市场状态")
    print(f"  信号日 {market_state['signal_days']}")
    print(f"  非信号日 {market_state['nonsignal_days']}")
    print(f"  信号日占比 {market_state['signal_day_freq']*100:.1f}%")
    print(f"  阈值漂移 {thr_drift}")

    print(f"\n嫌疑 C：低开幅度分档（相对该板块跌停幅度）")
    for r in mono:
        print(f"  {r['ratio_bucket']:<18} n={r['n']:>4}  均值 {r['mean_pct']:>+6.3f}%  "
              f"胜率 {r['win_rate']*100:>5.1f}%")
    print(f"{'='*78}")

    # ═══ 判定 ═══
    flags = []
    # A：20cm 板块占比过高 → 撬板效应
    cm20 = sum(r["n"] for b, r in board_res.items() if "20cm" in b)
    share20 = cm20 / len(sig)
    if share20 > 0.6:
        flags.append(f"信号 {share20*100:.0f}% 来自 20cm 板块 → 接近跌停撬板，"
                     "开盘价可成交性高度存疑")
    # 近跌停占比
    near = np.mean(ratio >= 0.8)
    if near > 0.4:
        flags.append(f"{near*100:.0f}% 的信号股低开幅度已达自身跌停幅度的 80%+ → "
                     "开盘价买入在真实撮合中极难成交")
    # B：信号日集中在市场下跌日
    if (market_state["signal_days"]["down_day_frac"] or 0) > \
       (market_state["nonsignal_days"]["down_day_frac"] or 0) + 0.15:
        flags.append("信号日显著集中在市场下跌日 → 收益含「大跌日反弹」成分，"
                     "不可视为稳定 alpha")
    # 阈值漂移过大
    if thr_drift["max_pct"] and thr_drift["min_pct"] and \
       (thr_drift["max_pct"] - thr_drift["min_pct"]) > 1.5:
        flags.append(f"p1 阈值在 {thr_drift['min_pct']}%~{thr_drift['max_pct']}% 间漂移 "
                     f"(>1.5pp) → 样本非平稳，「极端低开」的定义本身不稳定")
    # C：强单调 → 收益依赖最难成交的样本
    if len(mono_means) >= 3 and mono_means[-1] > mono_means[0] * 2:
        flags.append(f"收益随「接近跌停程度」强单调（{mono_means[0]:+.2f}% → "
                     f"{mono_means[-1]:+.2f}%）→ alpha 集中在最难成交的样本上")

    if flags:
        verdict = ("**「极端低开日内反转」不能作为可执行 T0 信号**，理由："
                   + "；".join(flags)
                   + "。它更像 A 股「跌停撬板 + 大跌日反弹」的混合体，"
                   "其收益依赖散户拿不到的成交价。"
                   "方向上可记录为「T+1 不开盘追跌、不杀跌」的行为纪律，"
                   "但不得量化为仓位或胜率承诺。")
    else:
        verdict = ("板块拆分、市场状态、阈值稳定性、幅度单调性四关均未触发否决 → "
                   "该信号在日线口径下是**候选**。但受限于单一样本期 + 无分钟线，"
                   "**仍不得用于实盘**，须扩样本（≥1500 日跨牛熊）与分钟线验证。")

    print(f"\n否决标记 {len(flags)} 条：")
    for f in flags:
        print(f"  ⚠️ {f}")
    print(f"\n结论：{verdict}")

    json.dump({
        "meta": {
            "purpose": "判定「极端低开日内反转」是撬板效应 / 大跌日反弹 / 还是真 alpha",
            "n_signal_samples": len(sig),
            "n_signal_days": len(sig_days),
            "date_range": [alld[0], alld[-1]],
            "board_rule": "688→科创板20cm, 30→创业板20cm, 60/00→主板10cm, 8/4/9→北交所30cm",
            "limit_ratio_def": "|gap| / 该板块跌停幅度；≥0.8 视为接近跌停",
        },
        "board_split": board_res,
        "market_state": market_state,
        "threshold_drift": thr_drift,
        "monotonic_by_limit_ratio": mono,
        "veto_flags": flags,
        "verdict": verdict,
        "must_not_claim": [
            "不得把该信号量化为 T0 仓位比例或胜率承诺",
            "不得声称已通过实盘可成交性验证（日线无撮合数据）",
            "若作为行为纪律（不追跌/不杀跌）可用，但不得称其为 alpha",
        ],
    }, open(OUT, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"\n✅ 已写出 {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
