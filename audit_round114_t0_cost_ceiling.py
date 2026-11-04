#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
audit_round114_t0_cost_ceiling.py —— T0（日内回转）的成本天花板与盈亏平衡命中率

═══ 这个脚本能做什么、不能做什么（先说清，避免越界）═══

**不能做**：回测 T0 策略。
   T0 的全部信息在分钟级（甚至 tick）。本项目 K 线库只有日线
   （data/klines_long.json，341 日 × 2138 只，无任何分钟数据）。
   用日线"回测 T0"= 拿 close 假装能择时，是纯粹的造假。
   → 任何 T0 收益数字都不可能由此脚本产生。

**能做**（且这是 T0 决策真正需要的东西）：**成本门槛的反算**。
   T0 是否可行，不取决于"模型多准"，而取决于一个纯算术问题：
      日内可捕获空间 ÷ 单次成本 = 需要多高的方向命中率才不亏？
   这个问题只用日线就能算——因为**日内振幅与波动分布**日线可测，
   而成本是已知常数。算出来的命中率门槛，才是判断 T0 值不值得做的依据。

═══ 核心公式 ═══

  设单次回转成本 c（双边，含滑点），日内捕获幅度 e（相对开盘价），
  命中率 p（方向判对的比例）：

      期望收益 = p·e − (1−p)·e − c = e·(2p − 1) − c
      盈亏平衡：  p* = 0.5 + c / (2e)

  这就是为什么 T0 极其难做：c 固定，e 越小 p* 越接近 1。
  举例：c=0.06%（ETF，免印花税），若 e=0.5% → p* = 56%
        若 e=0.2% → p* = 65%（几乎不可能稳定达到）

═══ 输出 ═══

  · 日内振幅分布（相对前收、相对开盘两种口径）
  · 不同标的（ETF / 主板个股 / 小市值）在不同 e 分位下的 p*
  · 「日内方向可预测性」的日线上界：用日线能测的唯一代理 =
    开盘→收盘的方向与开盘前信息（gap）的关系。若 gap 对 intra 无预测力
    （round111b 已证：日内段 13 因子零显著），则 p 无理由偏离 50%
    → 结合 p* 表，直接给出 T0 是否成立的判断

输出：audit_round114_t0_cost_ceiling.json
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
OUT = os.path.join(HERE, "audit_round114_t0_cost_ceiling.json")

# ⚠️ T0 成本三档：A 股个股 T+0 必须"有底仓"（先卖后买或先买后卖），
#    实际是"底仓不动 + 日内做一次反向" → 成本只算一次开仓的对手方向，
#    但保守起见按"完整一次回转"算双边，避免低估。
COST_PROFILES = {
    "etf_no_stamp": {
        "label": "ETF（免印花税）",
        "commission_2way": 0.0002,   # 万二双边（部分券商万0.5）
        "stamp": 0.0,
        "slippage_2way": 0.0004,     # ETF 流动性好，双边滑点 0.04%
    },
    "large_cap": {
        "label": "主板大市值个股",
        "commission_2way": 0.0003,
        "stamp": 0.0005,             # 卖出印花税 0.05%
        "slippage_2way": 0.0008,
    },
    "small_cap": {
        "label": "小市值高波动个股",
        "commission_2way": 0.0003,
        "stamp": 0.0005,
        "slippage_2way": 0.0020,     # 冲击成本大
    },
}


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
    P = {k: np.full((T, N), np.nan) for k in ("c", "o", "h", "l", "amt")}
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
            P["o"][i, j] = o_; P["c"][i, j] = c_; P["h"][i, j] = h_
            P["l"][i, j] = l_; P["amt"][i, j] = v_ * vol_coef(cd) * c_
    del kl
    C, O, H, L = P["c"], P["o"], P["h"], P["l"]
    print(f"  {N} 只 × {T} 日")

    # ── 日内统计量（全部只用日线可观测的量）
    prev = np.vstack([np.full((1, N), np.nan), C[:-1]])
    with np.errstate(invalid="ignore", divide="ignore"):
        # 日内振幅：最高−最低，相对前收（衡量"理论可捕获空间"的上界）
        amp_prev = (H - L) / prev
        # 日内振幅相对开盘（T0 实际从开盘起算，更贴近可执行空间）
        amp_open = (H - L) / O
        # 日内实际涨跌：close/open − 1（otypic 方向的实际实现值）
        intra = C / O - 1.0
        # 开盘跳空（开盘瞬间已知）
        gap = O / prev - 1.0

    ok = (np.isfinite(C) & np.isfinite(O) & np.isfinite(H) & np.isfinite(L)
          & np.isfinite(prev) & np.isfinite(P["amt"])
          & (P["amt"] >= a.min_amt))
    print(f"  有效样本 {ok.sum():,}（成交额≥{a.min_amt:.0e}）")

    def q(arr, qs):
        v = arr[ok]
        v = v[np.isfinite(v)]
        return {f"p{int(q*100)}": round(float(np.quantile(v, q)) * 100, 4)
                for q in qs}

    qs = [0.10, 0.25, 0.50, 0.75, 0.90]
    dist = {
        "amp_over_prev_pct": q(amp_prev, qs),
        "amp_over_open_pct": q(amp_open, qs),
        "abs_intra_pct": q(np.abs(intra), qs),
        "abs_gap_pct": q(np.abs(gap), qs),
    }

    # ── 日内方向的可预测性（日线能测的唯一上界）
    #    若 gap 无法预测 intra 方向，则 p 无理由偏离 0.5
    m = ok & np.isfinite(gap) & np.isfinite(intra)
    gi, ii = gap[m], intra[m]
    if len(gi) > 100:
        # 同号率：gap 方向与 intra 方向一致的比例
        same = float((np.sign(gi) == np.sign(ii)).mean())
        # 分位分组：按 gap 分 5 组，看 intra 均值是否单调
        edges = np.nanquantile(gi, [0.2, 0.4, 0.6, 0.8])
        grp = np.digitize(gi, edges)
        grp_means = [round(float(ii[grp == g].mean()) * 100, 4) for g in range(5)]
        grp_n = [int((grp == g).sum()) for g in range(5)]
    else:
        same, grp_means, grp_n = None, None, None

    # ── T0 盈亏平衡命中率 p* = 0.5 + c/(2e)
    def breakeven(c, e):
        if e <= 0:
            return None
        return round(0.5 + c / (2 * e), 4)

    table = {}
    for pk, prof in COST_PROFILES.items():
        c = (prof["commission_2way"] + prof["stamp"]
             + prof["slippage_2way"])
        rows = {}
        # 用 abs_intra 的分位数作为"假设能捕获的幅度 e"
        for qn, ev in dist["abs_intra_pct"].items():
            e = ev / 100.0
            rows[qn] = {
                "capture_e_pct": ev,
                "p_star": breakeven(c, e),
            }
        # 额外：用日内振幅中位数当 e（最乐观上界：假设吃满全天振幅）
        for qn, ev in dist["amp_over_open_pct"].items():
            e = ev / 100.0
            rows[f"amp_{qn}"] = {
                "capture_e_pct": ev,
                "p_star": breakeven(c, e),
                "note": "吃满全天振幅（理论上界，实际不可能）",
            }
        table[pk] = {
            "label": prof["label"],
            "cost_roundtrip_pct": round(c * 100, 4),
            "breakeven": rows,
        }

    print(f"\n{'='*72}")
    print("日内空间分布（%）：")
    for k, v in dist.items():
        print(f"  {k:<20} " + "  ".join(f"{kk}={vv:>6.2f}" for kk, vv in v.items()))

    print(f"\ngap → intra 的方向一致性：同号率 = {same:.4f}" if same else "")
    print(f"  按 gap 五分组，intra 均值（%）：{grp_means}")
    print(f"  各组样本：{grp_n}")

    print(f"\nT0 盈亏平衡命中率 p*（= 0.5 + c/2e）：")
    for pk, v in table.items():
        print(f"\n  【{v['label']}】单次回转成本 {v['cost_roundtrip_pct']:.3f}%")
        for qn, r in v["breakeven"].items():
            if qn.startswith("amp_"):
                continue
            ps = r["p_star"]
            print(f"    若可捕获 e={r['capture_e_pct']:>6.2f}%  →  需要命中率 "
                  f"{ps*100:>5.1f}%" if ps else f"    {qn}: e 不足")

    # ── 结论
    # 判据：p* 是否显著高于「日线能证明的可预测性」
    #   日线上界：同号率与 0.5 的偏离（若无偏离，p 无理由 > 0.5）
    base_p = 0.5
    edge = abs(same - 0.5) if same else 0.0
    feas = {}
    for pk, v in table.items():
        c = v["cost_roundtrip_pct"] / 100
        # 用"日内实际涨跌绝对值中位数"当 e 的保守估计（不是振幅，是实际走出来的幅度）
        e_conservative = dist["abs_intra_pct"]["p50"] / 100
        p_star = breakeven(c, e_conservative)
        feas[pk] = {
            "cost_pct": round(c * 100, 4),
            "e_conservative_pct": round(e_conservative * 100, 4),
            "p_star_conservative": p_star,
            "p_star_minus_base": (round(p_star - base_p, 4)
                                  if p_star else None),
            "daily_data_provable_edge": round(edge, 4),
            "feasible_on_daily_evidence": (bool(p_star is not None
                                                and (p_star - base_p) <= edge)),
        }

    verdict = (
        f"T0 的核心矛盾是成本门槛而非模型精度：ETF 口径单次回转 {table['etf_no_stamp']['cost_roundtrip_pct']:.3f}%、"
        f"小市值口径 {table['small_cap']['cost_roundtrip_pct']:.3f}%。"
        f"以日内实际涨跌幅度中位数 {dist['abs_intra_pct']['p50']:.2f}% 作为可捕获空间 e，"
        f"盈亏平衡命中率 p* 分别为 "
        + "、".join(f"{v['label']} {feas[k]['p_star_conservative']*100:.1f}%"
                   for k, v in COST_PROFILES.items() if True)
        + f"。而日线数据能证明的方向可预测性偏离 50% 仅 {edge*100:.2f}pp"
        + "（gap→intra 同号率），远不足以覆盖 p* 与 50% 的差距。"
        "→ **T0 在当前数据与成本结构下不具备可行性证据，需接入分钟线后重新验证**；"
        "在此之前任何 T0 仓位/信号建议都只能是假设，不能声称已验证。"
    )
    print(f"\n{'='*72}\n{verdict}\n{'='*72}")

    json.dump({
        "meta": {
            "purpose": "T0 成本天花板与盈亏平衡命中率反算（**非 T0 回测**）",
            "what_this_is_not": (
                "这不是 T0 回测。K 线库只有日线（341 日 × 2138 只），"
                "无分钟数据 → T0 收益无法由此脚本产生，任何 T0 收益数字都不成立。"),
            "what_this_is": (
                "T0 决策真正需要的成本门槛反算：p* = 0.5 + c/(2e)。"
                "e 由日线可测的日内幅度分布给出，c 为已知成本常数。"),
            "formula": "期望收益 = e·(2p−1) − c；盈亏平衡 p* = 0.5 + c/(2e)",
            "cost_profiles": COST_PROFILES,
            "n_valid_samples": int(ok.sum()),
            "date_range": [alld[0], alld[-1]],
            "data_limit": "无分钟线 → T0 不可回测",
        },
        "distribution_pct": dist,
        "gap_to_intra": {
            "same_sign_rate": round(same, 4) if same else None,
            "group_mean_intra_pct": grp_means,
            "group_n": grp_n,
            "note": ("同号率≈0.5 且分组均值无单调 → 开盘前信息对日内方向无预测力，"
                     "与 round111b「日内段 13 因子零显著」一致。"),
        },
        "breakeven_table": table,
        "feasibility": feas,
        "verdict": verdict,
        "must_not_claim": [
            "不得声称本脚本验证了 T0 策略收益（无分钟线）",
            "不得把 BREAKEVEN 表当成'T0 能达到的胜率'（那是必要条件的反算）",
            "不得在无分钟线验证前给出 T0 具体仓位比例",
        ],
    }, open(OUT, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"\n✅ 已写出 {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
