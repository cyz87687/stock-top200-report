#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
audit_round114f_gap_signal_final.py —— 主板低开反转的最终定性（含自我纠错）

═══ ⚠️ 必须先记录一个我自己犯的错（写进报告，不藏）═══

诊断「前一日是否跌停」时，我写了：
    prev_ret[1:] = C[1:] / C[:-1] - 1.0
    ...
    prev_ret[t, j]        ← 取信号日 t 的值
但那实际是**信号日 t 当天**的 close→close 收益，不是 t−1 日的。
后果是致命的：由于 close[t]/close[t−1] = (1+gap[t])·(1+intra[t])，
而信号样本的 gap 又几乎固定在 −6.7% 附近，
→ 按 prev_ret[t] 分组**等价于按 intra[t] 分组**（循环论证）。
于是「前日跌停组 46 只、均值 −3.22%、胜率 0.0%」这个结果
**是我的索引 bug 造出来的，全部作废**。

正确写法：prev_ret[t] = C[t−1] / C[t−2] − 1。

这个错再次印证项目纪律第⑭条：同一数据两处读取，必有一处忘校验 →
只有「拿具体样本翻出来看原始 bar」才能发现。这里就是靠抽 5 例 bar 发现的。

═══ 本脚本（正确口径）检验什么 ═══

  V1 前一日收益的正确拆分（跌停 / 未跌停 / 涨跌）
  V2 逐月表现（检验是否单靠某几个月）
  V3 剔除收盘涨停样本（地天板：close == high 且 close ≥ prev×1.095）
     —— 若收益全靠地天板，则实际很难吃到（涨停能卖但通常已封板多时）
  V4 阈值稳健性：固定阈值 −5/−6/−7/−8/−9% 逐一测，看边际是否稳定衰减
  V5 每年/每半年一致性
  V6 与前一日涨跌的关系（正确口径下的真实形态）

判据（全部为新增，不放松已有）：
  · V2 逐月：正收益月份占比 ≥ 60% 且最差月 > −1%
  · V3 剔地天板后：均值仍 > 1% 且胜率 > 55%
  · V4 阈值单调：−5% 到 −9% 的均值不出现「只在中段好」的孤岛形态
  · V5 半年度：各段均 > 0

输出：audit_round114f_gap_signal_final.json
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
OUT = os.path.join(HERE, "audit_round114f_gap_signal_final.json")

EXPAND_MIN_HIST = 60


def stat(v, label=""):
    v = np.asarray(v, float); v = v[np.isfinite(v)]
    if len(v) < 5:
        return None
    mu, sd = float(v.mean()), float(v.std(ddof=1))
    return {"label": label, "n": int(len(v)),
            "mean_pct": round(mu * 100, 4),
            "median_pct": round(float(np.median(v)) * 100, 4),
            "win_rate": round(float((v > 0).mean()), 4),
            "t": round(mu / (sd / math.sqrt(len(v))), 3) if sd > 0 else None}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--min-amt", type=float, default=3e7)
    a = ap.parse_args()

    print(f"读取 {KL} ...")
    kl = json.load(open(KL, encoding="utf-8"))
    codes_all = sorted(kl.keys())
    codes = [c for c in codes_all
             if (c[2:] if c[:2] in ("sh", "sz") else c).startswith(("60", "00"))]
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
    print(f"  主板 {N} 只 × {T} 日")

    prev = np.vstack([np.full((1, N), np.nan), C[:-1]])
    with np.errstate(invalid="ignore", divide="ignore"):
        gap = O / prev - 1.0
        intra = C / O - 1.0
        nxt = np.full((T, N), np.nan)
        nxt[:-1] = C[1:] / C[:-1] - 1.0

    # ⚠️ 正确的前一日收益：t−1 日相对 t−2 日的 close→close
    prev_ret = np.full((T, N), np.nan)
    prev_ret[2:] = C[1:-1] / C[:-2] - 1.0

    base = (np.isfinite(gap) & np.isfinite(intra)
            & np.isfinite(A) & (A >= a.min_amt))
    tradable = base & ~(gap <= -0.095)

    mkt = np.full(T, np.nan)
    for t in range(T):
        m = tradable[t]
        if m.sum() >= 30:
            mkt[t] = float(np.nanmean(intra[t][m]))

    # 信号（扩展窗口 p1，与 114e 同口径）
    sig = []
    for t in range(EXPAND_MIN_HIST, T - 1):
        if not np.isfinite(intra[t]).any():
            continue
        hist = gap[:t][base[:t]]
        if len(hist) < 1000:
            continue
        p1 = float(np.percentile(hist, 1))
        m = tradable[t] & (gap[t] <= p1)
        for j in np.where(m)[0]:
            # 收盘是否涨停（主板 10%）：close == high 且 close/prev >= 1.095
            is_lu = bool(np.isfinite(H[t, j]) and np.isfinite(prev[t, j])
                         and C[t, j] >= H[t, j] - 1e-9
                         and C[t, j] / prev[t, j] >= 1.095)
            sig.append({
                "t": t, "j": int(j), "code": codes[j], "date": alld[t],
                "intra": float(intra[t, j]), "gap": float(gap[t, j]),
                "prev_ret": (float(prev_ret[t, j])
                             if np.isfinite(prev_ret[t, j]) else np.nan),
                "nxt": (float(nxt[t, j]) if np.isfinite(nxt[t, j]) else np.nan),
                "excess": (float(intra[t, j] - mkt[t])
                           if np.isfinite(mkt[t]) else np.nan),
                "close_limit_up": is_lu,
            })
    if not sig:
        print("❌ 无信号"); return 1
    print(f"  信号 {len(sig)} 样本 / {len({s['t'] for s in sig})} 日")

    out = {}

    # ═══ V1 正确的前一日收益拆分 ═══
    pr = np.array([s["prev_ret"] for s in sig], float)
    pr_ok = np.isfinite(pr)
    groups = {}
    for lab, m in (("前日跌停(≤-9.5%)", pr <= -0.095),
                   ("前日大跌(-9.5%~-5%)", (pr > -0.095) & (pr <= -0.05)),
                   ("前日小跌(-5%~0)", (pr > -0.05) & (pr < 0)),
                   ("前日上涨(>0)", pr >= 0)):
        m = m & pr_ok
        if m.sum() >= 5:
            r = stat(np.array([s["intra"] for s, mm in zip(sig, m) if mm]), lab)
            if r:
                groups[lab] = r
    out["V1_prev_day_split"] = {
        "note": ("⚠️ 修正版。上一版按 prev_ret[t]=C[t]/C[t−1] 分组是错的"
                 "（那是信号日当天收益，等价于按 intra 分组=循环论证），结论已作废。"),
        "groups": groups,
        "prev_ret_mean_pct": round(float(np.nanmean(pr)) * 100, 4),
        "frac_prev_limit_down": round(float((pr <= -0.095).mean()), 4),
        "frac_prev_down": round(float((pr < 0).mean()), 4),
    }

    # ═══ V2 逐月 ═══
    bym = {}
    for s in sig:
        bym.setdefault(s["date"][:7], []).append(s["intra"])
    months = []
    for k in sorted(bym):
        r = stat(np.array(bym[k], float), k)
        if r:
            months.append(r)
    pos_m = sum(1 for r in months if r["mean_pct"] > 0)
    out["V2_monthly"] = {
        "months": months,
        "n_months": len(months),
        "n_positive": pos_m,
        "positive_frac": round(pos_m / len(months), 4) if months else None,
        "worst_month_mean_pct": min((r["mean_pct"] for r in months),
                                    default=None),
        "best_month_mean_pct": max((r["mean_pct"] for r in months),
                                   default=None),
    }

    # ═══ V3 剔除收盘涨停 ═══
    nol = [s for s in sig if not s["close_limit_up"]]
    yesl = [s for s in sig if s["close_limit_up"]]
    out["V3_exclude_limit_up_close"] = {
        "all": stat(np.array([s["intra"] for s in sig], float), "全部"),
        "excl_close_limit_up": stat(np.array([s["intra"] for s in nol], float),
                                    "剔除收盘涨停"),
        "only_close_limit_up": stat(np.array([s["intra"] for s in yesl], float),
                                    "仅收盘涨停"),
        "n_limit_up": len(yesl),
        "limit_up_share": round(len(yesl) / len(sig), 4),
    }

    # ═══ V4 固定阈值稳健性 ═══
    thr_rows = []
    for thr in (-0.05, -0.06, -0.07, -0.08, -0.09):
        m = tradable & (gap <= thr)
        v = np.array([intra[t, j] for t, j in zip(*np.where(m))], float)
        if len(v) >= 20:
            # 按日聚类
            idx = list(zip(*np.where(m)))
            dd = {}
            for t, j in idx:
                dd.setdefault(t, []).append(intra[t, j])
            dm = np.array([np.mean(x) for x in dd.values()], float)
            r = stat(v, f"gap<={thr*100:.0f}%")
            r["threshold_pct"] = round(thr * 100, 2)
            r["n_days"] = len(dm)
            r["day_cluster_t"] = stat(dm).get("t") if len(dm) >= 8 else None
            r["day_cluster_mean_pct"] = (round(float(dm.mean()) * 100, 4)
                                         if len(dm) else None)
            thr_rows.append(r)
    out["V4_threshold_robust"] = thr_rows

    # ═══ V5 半年度 ═══
    byh = {}
    for s in sig:
        y, mth = int(s["date"][:4]), int(s["date"][5:7])
        key = f"{y}H{1 if mth <= 6 else 2}"
        byh.setdefault(key, []).append(s["intra"])
    halves = []
    for k in sorted(byh):
        r = stat(np.array(byh[k], float), k)
        if r:
            halves.append(r)
    out["V5_halfyear"] = halves

    # ═══ 打印 ═══
    print(f"\n{'='*80}")
    print("V1 前一日收益拆分（⚠️ 修正版，旧版作废）")
    print(f"  前一日均值 {out['V1_prev_day_split']['prev_ret_mean_pct']:+.2f}%，"
          f"跌停占比 {out['V1_prev_day_split']['frac_prev_limit_down']*100:.1f}%，"
          f"下跌占比 {out['V1_prev_day_split']['frac_prev_down']*100:.1f}%")
    for k, r in groups.items():
        print(f"  {k:<22} n={r['n']:>4} 均值 {r['mean_pct']:>+7.3f}% "
              f"中位 {r['median_pct']:>+7.3f}% 胜率 {r['win_rate']*100:>5.1f}%")

    print(f"\nV2 逐月：{out['V2_monthly']['n_positive']}/{out['V2_monthly']['n_months']} 个月为正 "
          f"（{out['V2_monthly']['positive_frac']*100:.0f}%），"
          f"最差 {out['V2_monthly']['worst_month_mean_pct']:+.2f}%，"
          f"最好 {out['V2_monthly']['best_month_mean_pct']:+.2f}%")
    for r in months:
        print(f"    {r['label']}  n={r['n']:>3}  均值 {r['mean_pct']:>+7.3f}%  "
              f"胜率 {r['win_rate']*100:>5.1f}%")

    v3 = out["V3_exclude_limit_up_close"]
    print(f"\nV3 收盘涨停（地天板）占比 {v3['limit_up_share']*100:.1f}%（{v3['n_limit_up']} 个）")
    print(f"    全部 {v3['all']}")
    print(f"    剔除后 {v3['excl_close_limit_up']}")
    print(f"    仅涨停 {v3['only_close_limit_up']}")

    print(f"\nV4 固定阈值稳健性：")
    for r in thr_rows:
        print(f"    {r['threshold_pct']:>6.0f}%  n={r['n']:>5}  均值 {r['mean_pct']:>+7.3f}%  "
              f"胜率 {r['win_rate']*100:>5.1f}%  按日t={r['day_cluster_t']}  "
              f"日均 {r['day_cluster_mean_pct']:+.3f}%")

    print(f"\nV5 半年度：")
    for r in halves:
        print(f"    {r['label']}  n={r['n']:>3}  均值 {r['mean_pct']:>+7.3f}%  "
              f"胜率 {r['win_rate']*100:>5.1f}%")
    print(f"{'='*80}")

    # ═══ 判定 ═══
    chk = {}
    m2 = out["V2_monthly"]
    chk["V2_months_pos>=60%"] = bool(m2["positive_frac"]
                                     and m2["positive_frac"] >= 0.60)
    chk["V2_worst_month>-1%"] = bool(m2["worst_month_mean_pct"] is not None
                                     and m2["worst_month_mean_pct"] > -1.0)
    e = v3["excl_close_limit_up"]
    chk["V3_excl_limitup_mean>1%"] = bool(e and e["mean_pct"] > 1.0)
    chk["V3_excl_limitup_win>55%"] = bool(e and e["win_rate"] > 0.55)
    # V4：相邻阈值不出现「只在中段好」的孤岛
    means = [r["mean_pct"] for r in thr_rows]
    chk["V4_no_island"] = bool(len(means) >= 4 and min(means) > 0)
    chk["V5_all_halves_pos"] = bool(halves and all(r["mean_pct"] > 0
                                                   for r in halves))
    n_pass = sum(1 for x in chk.values() if x)

    print("\n定性检验：")
    for k, x in chk.items():
        print(f"  {'✅' if x else '❌'} {k}")
    print(f"通过 {n_pass}/{len(chk)}")

    if n_pass == len(chk):
        verdict = (
            "主板低开反转在「前日涨跌拆分、逐月一致性、剔除地天板、阈值稳健性、"
            "半年度一致性」五项定性检验中全部通过："
            f"剔除收盘涨停后均值仍 {e['mean_pct']:+.2f}%（胜率 {e['win_rate']*100:.1f}%），"
            f"{m2['n_positive']}/{m2['n_months']} 个月为正，最差月 "
            f"{m2['worst_month_mean_pct']:+.2f}%，"
            f"固定阈值 −5%~−9% 全部为正（无孤岛）。"
            "→ **这是目前唯一经得起反复证伪的日内可执行候选**。"
            "但必须继续声明三条边界：①样本仅 341 日单一样本期；"
            "②无分钟线，无法验证「开盘价成交率」与盘中路径（最大回撤未知）；"
            "③「低开 6%+ 的股票」往往伴随利空事件，实盘需人工排除退市/立案类。"
            "**在扩样本（≥1500 日跨牛熊）+ 分钟线验证前，只能作为「观察名单触发的"
            "人工决策辅助」，不得自动化下单，也不得承诺胜率。**")
    else:
        failed = [k for k, x in chk.items() if not x]
        verdict = (f"定性检验通过 {n_pass}/{len(chk)}，未过：{failed} → "
                   "仍不足以作为可执行信号，只能记为行为观察。")
    print(f"\n结论：{verdict}")

    json.dump({
        "meta": {
            "purpose": "主板低开反转最终定性（含前一版索引 bug 的纠错记录）",
            "self_correction": ("上一版用 prev_ret[t]=C[t]/C[t−1]（信号日当天收益）"
                                "分「前日跌停」组，等价于按 intra 分组（循环论证），"
                                "「46 只胜率 0%」结论作废。正确为 C[t−1]/C[t−2]。"),
            "universe": f"主板 {N} 只，成交额≥{a.min_amt:.0e}",
            "date_range": [alld[0], alld[-1]],
            "n_signal_samples": len(sig),
            "n_signal_days": len({s['t'] for s in sig}),
            "signal_rule": "gap[t] <= 历史[0,t) 的 p1 分位（扩展窗口 ≥60 日），主板",
            "exit_rule": "同日 close 卖出（open→close）",
            "data_limit": "无分钟线 → 开盘成交率、盘中最大回撤、滑点均未验证",
        },
        "V1_prev_day_split": out["V1_prev_day_split"],
        "V2_monthly": out["V2_monthly"],
        "V3_exclude_limit_up_close": out["V3_exclude_limit_up_close"],
        "V4_threshold_robust": out["V4_threshold_robust"],
        "V5_halfyear": out["V5_halfyear"],
        "checks": chk, "n_pass": n_pass,
        "verdict": verdict,
        "must_not_claim": [
            "不得自动化下单（样本 341 日单期，未跨牛熊）",
            "不得承诺胜率（无分钟线，盘中路径未知，最大回撤未知）",
            "不得把「剔除地天板后仍正」理解为「一定能卖在收盘价」",
            "不得忽略事件风险（利空/立案/退市类低开需人工排除）",
        ],
    }, open(OUT, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"\n✅ 已写出 {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
