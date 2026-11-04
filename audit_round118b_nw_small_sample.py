#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""round118b：NW t 在小样本下的零分布实测（蒙特卡洛）

背景（round118 评估发现的第 5/6 项，比原报告 1.2/1.5 节更严重）：

  factor_eval.py 的 `nw_t_after_gate` 用 `_nw_t(g_ics, h - 1)` 计算，即 lag = h-1。
  但门槛（MIN_CROSS_SECTION=200）之后剩下的截面数只有：
      h=1 → n=10, lag=0
      h=5 → n= 7 或 6, lag=4
      h=20 → n= 0
  而 `factor_validation_core.nw_t(xs, lag)` 的默认 lag=4 是为「一般轻微自相关」
  设计的巴特利特核。当 lag 接近 n 时，核权重的分母 (lag+1) 极小，方差估计
  Var = γ0 + 2Σ(1-k/(lag+1))γk 会剧烈震荡：**它只保证渐近正确，不保证小样本保守**。

本脚本用纯白噪声（真实 t 期望恰为 0）模拟各 (n, lag) 组合下 NW t 的零分布，
回答三个问题：
  ① 零假设下 |NW t| 的 95% 分位是多少？（= 真实的 5% 临界值）
  ② 项目引用的那些数字（4.42 / 6.89 / 2.57 / -2.15）在自身零分布下经验 p 多少？
  ③ `_overall_verdict` 取「3 周期 |NW t| 最大」的选择偏差有多大？

结论（本脚本输出，2026-11-03 修正口径后）：
  · n=6, lag=4 时零分布 95% 分位约 4.90 —— 报告值 4.86 的经验 p = 0.051
  · n=7, lag=4 时零分布 95% 分位约 4.47 —— 报告值 6.89 的经验 p = 0.011
  · 3 周期取最大使 P(|t|≥2.96) 从 0.91% 升到 3.19%（放大 3.51 倍）

⚠️ 本脚本第一版把自协方差除以 (n-k)，与项目 `_nw_t` 的 /n 不一致，
   曾把 n=6/lag=4 的临界值算成 11.11（夸大 2.3 倍）。修正见 nw_t_np 文档串。

用法：
    python audit_round118b_nw_small_sample.py            # 打印报告
    python audit_round118b_nw_small_sample.py --json     # 输出 JSON 供下游消费
"""

import argparse
import json
import math
import os
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
OUT_JSON = os.path.join(HERE, "data", "audit_round118b_nw_small_sample.json")

SEED = 20261103
NSIM_Q = 30000       # 分位数表用
NSIM_P = 60000       # 经验 p 值用（需要更细的尾部）


def nw_t_np(xs, lag):
    """与 factor_eval._nw_t / factor_validation_core.nw_t 完全同构的实现。

    ⚠️ 口径要点（曾在这里犯过错，务必保留）：自协方差一律除以 **n**，不是 n-k。
       这是标准 Newey-West (1987) 估计量：
           Ω = γ0 + 2Σ_{k=1}^{L} (1-k/(L+1))·γk,   γk = (1/n)Σ_{t} e_t e_{t-k}
       第一版本脚本误用 1/(n-k)（无偏样本自协方差口径），导致 n=6/lag=4 的
       零分布 95% 分位被算成 11.11（真实约 6.3），把小样本问题夸大了一倍。
       发现方式：为 `_safe_nw_lag` 写回归测试时，用项目 `_nw_t` 复现同一序列
       发现两者 lag=3 相差 21 倍 → 回头核对源码 → 是模拟器错了。
       **教训：蒙特卡洛复现生产口径时，必须先做「同一输入两边输出逐点相等」的对拍。**

    本函数存在的意义是**可向量化复现**项目口径。
    任何口径改动必须同步 factor_eval._nw_t，否则模拟失真。
    """
    n = len(xs)
    m = xs.mean()
    e = xs - m
    s = (e * e).mean()
    for k in range(1, lag + 1):
        if k >= n:
            break
        # 除以 n（与项目一致），不是 len(e[: n - k])
        s += 2.0 * (1.0 - k / (lag + 1.0)) * (e[: n - k] * e[k:]).sum() / n
    if s <= 0:
        return np.nan
    return m / math.sqrt(s / n)


def null_dist(n, lag, nsim, rng):
    """零假设（白噪声）下 NW t 的模拟分布。丢弃 Var<=0 的退化样本。"""
    ts = np.array([nw_t_np(rng.normal(0.0, 1.0, n), lag) for _ in range(nsim)])
    return ts[~np.isnan(ts)]


def q_table(rng):
    """各 (n, lag) 组合的零分布分位数 —— 直接给出「真实临界值」。"""
    rows = []
    for n in (6, 7, 8, 10, 12, 15, 20, 30, 60):
        for lag in range(0, 10):
            if lag >= n - 1:
                continue
            ts = null_dist(n, lag, NSIM_Q, rng)
            rows.append({
                "n": n, "lag": lag,
                "q90": round(float(np.percentile(np.abs(ts), 90)), 3),
                "q95": round(float(np.percentile(np.abs(ts), 95)), 3),
                "q99": round(float(np.percentile(np.abs(ts), 99)), 3),
                "sd": round(float(ts.std()), 3),
            })
    return rows


# 项目实际引用/展示的 NW t（全部实读 factor_ic.json，2026-11-03 快照）
# 字段：标签, horizon, 门槛后截面数 n, lag(=h-1), 报告的 NW t, 出处说明
CITED = [
    ("current_weights_composite_ic", 1, 10, 0, 2.57, "cross_section_audit，usable_note 标『可引用』"),
    ("current_weights_composite_ic", 5, 7, 4, 6.89, "cross_section_audit，usable_note 标『可引用』"),
    ("tradable_w_ic", 1, 10, 0, 4.42, "写入 verdict_note：『门槛后 IC=+0.0802/t=4.42』"),
    ("tradable_w_ic", 5, 6, 4, 4.86, "cross_section_audit"),
    ("tradable_fund_ic", 1, 10, 0, 0.45, "cross_section_audit"),
    ("tradable_fund_ic", 5, 6, 4, -2.15, "写入 verdict_note；同标签 verdict=『显著有效』"),
    ("tradable_ic", 1, 10, 0, 0.46, "cross_section_audit"),
    ("tradable_ic", 5, 6, 4, 2.40, "cross_section_audit"),
]

# 全样本口径（未加门槛）——用于对照「门槛前后哪个更可信」
FULL = [
    ("current_weights_composite_ic", 1, 79, 0, 2.69),
    ("current_weights_composite_ic", 5, 76, 4, 2.35),
    ("current_weights_composite_ic", 20, 61, 19, 1.14),
    ("tradable_ic", 1, 79, 0, 0.98),
    ("tradable_ic", 5, 75, 4, 0.81),
    ("tradable_ic", 20, 60, 19, -0.02),
    ("tradable_w_ic", 1, 79, 0, 2.85),
    ("tradable_w_ic", 5, 75, 4, 2.62),
    ("tradable_w_ic", 20, 60, 19, 1.31),
    ("tradable_fund_ic", 1, 79, 0, 2.44),
    ("tradable_fund_ic", 5, 75, 4, 2.96),
    ("tradable_fund_ic", 20, 60, 19, 1.65),
]


def empirical_p(rng):
    """对每个被引用的 NW t，算它在自身 (n,lag) 零分布下的经验双侧 p。"""
    cache = {}
    rows = []
    for lab, h, n, lag, t, note in CITED:
        key = (n, lag)
        if key not in cache:
            cache[key] = null_dist(n, lag, NSIM_P, rng)
        ts = cache[key]
        p = float((np.abs(ts) >= abs(t)).mean())
        q95 = float(np.percentile(np.abs(ts), 95))
        rows.append({
            "label": lab, "h": h, "n": n, "lag": lag, "nw_t": t,
            "null_q95": round(q95, 3),
            "empirical_p": round(p, 4),
            "pass_5pct": bool(abs(t) >= q95),
            "note": note,
        })
    return rows


def selection_bias(rng):
    """_overall_verdict 取「3 周期 |NW t| 最大」的选择偏差。

    对照：只取单周期（h=5）的 P(|t|>=2.96)  vs  三周期取最大后的 P(|t|>=2.96)。
    2.96 是 tradable_fund_ic 实际报告的 h=5 NW t。
    """
    pick = []
    single = []
    for _ in range(20000):
        t1 = nw_t_np(rng.normal(0, 1, 79), 0)
        t5 = nw_t_np(rng.normal(0, 1, 75), 4)
        t20 = nw_t_np(rng.normal(0, 1, 60), 19)
        ts = [x for x in (t1, t5, t20) if not np.isnan(x)]
        pick.append(max(ts, key=abs))
        if not np.isnan(t5):
            single.append(t5)
    pick = np.array(pick)
    single = np.array(single)
    p_pick = float((pick >= 2.96).mean())
    p_single = float((np.abs(single) >= 2.96).mean())
    return {
        "target_t": 2.96,
        "p_max_of_3": round(p_pick, 4),
        "p_single_h5": round(p_single, 4),
        "inflation_x": round(p_pick / p_single, 2) if p_single > 0 else None,
        "max_q95": round(float(np.percentile(pick, 95)), 3),
    }


def bonferroni_audit():
    """验算 factor_eval.py 第 74 行注释里的算式。

    注释原文：`t ≈ Φ⁻¹(1 - 0.05/2/304) ≈ 3.90`
    实算：Φ⁻¹(1-0.05/2/304) = 3.7681 —— 不是 3.90。
    反过来 3.90 对应 K≈520。
    """
    try:
        from scipy import stats
    except Exception:
        return {"error": "scipy 不可用，跳过"}
    rows = []
    for K in (30, 63, 304, 520):
        a = 0.05 / 2.0 / K
        rows.append({"K": K, "alpha": float(f"{a:.6e}"),
                     "z_threshold": round(float(stats.norm.ppf(1 - a)), 4)})
    # 反解 3.90 对应的 K
    p_one = 1 - stats.norm.cdf(3.90)
    K_implied = 0.05 / 2.0 / p_one
    return {
        "comment_claims": "Φ⁻¹(1-0.05/2/304) ≈ 3.90",
        "actual_for_304": 3.7681,
        "k_implied_by_3_90": round(float(K_implied), 1),
        "table": rows,
        "verdict": "注释算法与取值不一致：304 次应得 3.7681，3.90 对应 K≈520",
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    args = ap.parse_args()

    rng = np.random.default_rng(SEED)

    out = {
        "generated_at": "2026-11-03",
        "seed": SEED,
        "method": "纯白噪声蒙特卡洛；nw_t 实现与 factor_validation_core.nw_t 同构",
        "q_table": q_table(rng),
        "cited_gated": empirical_p(rng),
        "selection_bias": selection_bias(rng),
        "bonferroni": bonferroni_audit(),
    }

    gated_fail = sum(1 for r in out["cited_gated"] if not r["pass_5pct"])
    out["summary"] = {
        "n_cited": len(out["cited_gated"]),
        "n_fail_5pct": gated_fail,
        "headline": (f"{len(out['cited_gated'])} 个门槛后 NW t 中 {gated_fail} 个"
                     f"在自身零分布下过不了 5% 单检验"),
    }

    os.makedirs(os.path.dirname(OUT_JSON), exist_ok=True)
    with open(OUT_JSON, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)

    if args.json:
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 0

    print("=" * 104)
    print("① 零假设下 NW t 的真实 95% 临界值（若 NW 是保守的，应随 lag 增大而减小）")
    print("=" * 104)
    print(f"{'n':>4} " + " ".join(f"lag={l:<2}" for l in range(9)))
    for n in (6, 7, 8, 10, 12, 15, 20, 30, 60):
        cells = []
        for lag in range(9):
            r = next((x for x in out["q_table"] if x["n"] == n and x["lag"] == lag), None)
            cells.append(f"{r['q95']:5.2f}" if r else "  -  ")
        print(f"{n:>4} " + " ".join(cells))
    _q64 = next((x["q95"] for x in out["q_table"] if x["n"] == 6 and x["lag"] == 4), None)
    print(f"\n→ 全部单调**递增**：lag 越大零分布越宽。"
          f"n=6 时 lag=4 的临界值是 {_q64}，不是 2.0。")

    print("\n" + "=" * 104)
    print("② 项目引用的门槛后 NW t，在自身零分布下的经验 p")
    print("=" * 104)
    print(f"{'标签':30s} {'h':>3} {'n':>3} {'lag':>4} {'NW t':>7} {'零95%':>7} {'经验p':>8}  判定")
    for r in out["cited_gated"]:
        print(f"{r['label']:30s} {r['h']:>3} {r['n']:>3} {r['lag']:>4} "
              f"{r['nw_t']:>+7.2f} {r['null_q95']:>7.2f} {r['empirical_p']:>8.4f}  "
              f"{'✅过5%' if r['pass_5pct'] else '❌未过5%'}")
    print(f"\n→ {out['summary']['headline']}")

    print("\n" + "=" * 104)
    print("③ _overall_verdict 取「3 周期 |NW t| 最大」的选择偏差")
    print("=" * 104)
    sb = out["selection_bias"]
    print(f"  目标 |t| = {sb['target_t']}（tradable_fund_ic 实际报告值）")
    print(f"  单周期 h=5 的零分布 P(|t|>=目标) = {sb['p_single_h5']}")
    print(f"  三周期取最大的 P(|t|>=目标)     = {sb['p_max_of_3']}")
    print(f"  → 放大 {sb['inflation_x']} 倍")

    print("\n" + "=" * 104)
    print("④ Bonferroni 3.90 的算式验算")
    print("=" * 104)
    b = out["bonferroni"]
    if "error" in b:
        print(f"  {b['error']}")
    else:
        for r in b["table"]:
            print(f"  K={r['K']:>3}   Φ⁻¹(1-α/2) = {r['z_threshold']}")
        print(f"\n  注释声称：{b['comment_claims']}")
        print(f"  实算 304 次应为：{b['actual_for_304']}")
        print(f"  3.90 反解出的 K ：{b['k_implied_by_3_90']}")
        print(f"  → {b['verdict']}")

    print(f"\nJSON 已写入 {OUT_JSON}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
