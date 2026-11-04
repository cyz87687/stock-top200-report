#!/usr/bin/env python3
"""Round13：NW lag 敏感性审计 —— 检验「Bonferroni 显著性」是否稳健。

动机（2026-10-25 发现）：factor_eval._agg_stats 用 `_nw_t(ics, h-1)` 算 Newey-West
t 值，即 h=20 时 lag=19。在 n=58 的样本上，lag=19 意味着每个自协方差只用 39 个
观测估计，方差估计本身极不稳定 —— 显著性会随 lag 大幅摆动。

实证（score_theme, h=20, n=58, IC=-0.1076）：
    lag=0  → t=-5.25  ✅
    lag=3  → t=-3.35  ✅
    lag=6  → t=-2.86  ❌
    lag=19 → t=-2.70  ❌
也就是说「过没过 Bonferroni 3.08」这个结论，完全由 lag 这个隐含参数决定。

本脚本对全部因子、全部周期做 lag 扫描，输出：
  · nw_t 在各 lag 下的最小/最大值
  · 「全 lag 都显著」才算稳健显著（robust）
  · Andrews(1991) 自动带宽作为参考 lag

纪律：任何「某因子显著/不显著」的表述，必须注明 lag。只说"显著"而不说 lag，
  等于隐去了一个能翻转结论的自由度。
"""
import json
import math
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import factor_eval as fe

T_BONF = 3.08
LAGS = [0, 1, 2, 3, 4, 6, 8, 12, 19]


def nw_t(xs, lag):
    n = len(xs)
    if n < 3:
        return None
    m = sum(xs) / n
    e = [x - m for x in xs]
    s = sum(v * v for v in e)
    for k in range(1, min(lag, n - 1) + 1):
        gk = sum(e[i] * e[i - k] for i in range(k, n))
        s += 2.0 * (1.0 - k / (lag + 1.0)) * gk
    if s <= 0:
        return None
    return m / math.sqrt((s / n) / n)


def andrews_lag(xs):
    """Andrews(1991) AR(1) 自动带宽 —— 数据驱动，不预设 h-1。"""
    n = len(xs)
    if n < 5:
        return 0
    m = sum(xs) / n
    e = [x - m for x in xs]
    g0 = sum(v * v for v in e) / n
    if g0 <= 0:
        return 0
    rho = sum(e[i] * e[i - 1] for i in range(1, n)) / n / g0
    rho = max(min(rho, 0.97), -0.97)
    a = 4.0 * rho ** 2 / ((1 - rho) ** 2 * (1 + rho) ** 2)
    lag = int(math.ceil(1.1447 * (a * n) ** (1.0 / 3.0)))
    return max(0, min(lag, n - 1))


def ic_series(snaps, tdays, px, field, hop):
    tidx = {d: i for i, d in enumerate(tdays)}
    out = []
    for s in snaps:
        d0 = s["date"]
        if d0 not in tidx:
            continue
        i0 = tidx[d0]
        if i0 + hop >= len(tdays):
            continue
        d1 = tdays[i0 + hop]
        fv, rv = [], []
        for r in s["rows"]:
            m = px.get(r.get("code"))
            if not m:
                continue
            p0, p1 = m.get(d0), m.get(d1)
            if not p0 or not p1 or p0 <= 0:
                continue
            v = r.get(field)
            if not isinstance(v, (int, float)):
                continue
            fv.append(v)
            rv.append(p1 / p0 - 1.0)
        if len(fv) < 40:
            continue
        ic = fe.rank_ic(fv, rv)
        if ic is not None:
            out.append(ic)
    return out


def main():
    h = json.load(open(os.path.join(HERE, "factor_history.json")))
    snaps, _ = fe._drop_stale_price_snapshots(h["snapshots"])
    tdays, px = fe.px_index(h)

    fields = ["score_fund", "score_news", "score_theme", "score_sector",
              "score_oversold60", "score_bias60", "score_rsi_rev",
              "score_vol_pulse", "score_overbought",
              "score_theme_intrinsic", "score_attention", "score_tech"]

    out = {}
    print("=" * 108)
    print("NW lag 敏感性：同一因子同一周期，t 值随 lag 摆动到能翻转「是否过 3.08」")
    print("=" * 108)
    print(f"{'因子':<22}{'h':<4}{'n':<5}{'IC':<10}{'lag*':<6}{'t@lag*':<9}"
          f"{'t_min':<9}{'t_max':<9}{'稳健显著?':<12}{'h-1口径':<10}")
    print("-" * 108)

    for f in fields:
        out[f] = {}
        for hop in (1, 5, 20):
            xs = ic_series(snaps, tdays, px, f, hop)
            if len(xs) < 3:
                out[f][hop] = None
                continue
            m = sum(xs) / len(xs)
            lag_a = andrews_lag(xs)
            ts = {L: nw_t(xs, L) for L in LAGS}
            ts = {k: v for k, v in ts.items() if v is not None}
            if not ts:
                out[f][hop] = None
                continue
            tmin, tmax = min(ts.values()), max(ts.values())
            # 稳健显著 = 所有 lag 下 |t| 都 >= 门槛
            robust = all(abs(v) >= T_BONF for v in ts.values())
            t_h1 = nw_t(xs, hop - 1)
            rec = {"n": len(xs), "ic": round(m, 4),
                   "lag_andrews": lag_a,
                   "t_andrews": round(ts.get(lag_a) or nw_t(xs, lag_a), 2)
                   if (ts.get(lag_a) or nw_t(xs, lag_a)) is not None else None,
                   "t_by_lag": {str(k): round(v, 2) for k, v in sorted(ts.items())},
                   "t_min": round(tmin, 2), "t_max": round(tmax, 2),
                   "t_lag_h_minus_1": round(t_h1, 2) if t_h1 is not None else None,
                   "robust_bonferroni": bool(robust),
                   "flips": (min(ts.values()) < -T_BONF) != (max(ts.values()) < -T_BONF)
                   or (min(ts.values()) > T_BONF) != (max(ts.values()) > T_BONF)}
            out[f][str(hop)] = rec
            # 只打印有信号或样本充足的
            if abs(m) >= 0.02 or len(xs) >= 50:
                print(f"{f:<22}{hop:<4}{len(xs):<5}{m:<10.4f}{lag_a:<6}"
                      f"{(rec['t_andrews'] if rec['t_andrews'] is not None else 0):<9.2f}"
                      f"{tmin:<9.2f}{tmax:<9.2f}"
                      f"{('✅ 是' if robust else ('⚠️ 翻转' if rec['flips'] else '❌ 否')):<12}"
                      f"{(rec['t_lag_h_minus_1'] or 0):<10.2f}")

    print("-" * 108)
    print("lag* = Andrews(1991) AR(1) 自动带宽；最后一列为现行 factor_eval 的 lag=h-1 口径")

    # 关键结论
    print()
    print("=" * 108)
    print("结论")
    print("=" * 108)
    n_robust = sum(1 for f in out for hh in out[f].values()
                   if hh and hh.get("robust_bonferroni"))
    n_flip = sum(1 for f in out for hh in out[f].values() if hh and hh.get("flips"))
    print(f"  全 lag 稳健过 Bonferroni 的（因子×周期）组合数：{n_robust}")
    print(f"  「是否显著」会随 lag 翻转的组合数：{n_flip}")
    th = out.get("score_theme", {}).get("20")
    if th:
        print(f"  score_theme h=20：IC={th['ic']}, t范围[{th['t_min']}, {th['t_max']}], "
              f"h-1口径={th['t_lag_h_minus_1']}, 稳健={th['robust_bonferroni']}")
    print()
    print("  → 纪律：score_theme 反号（theme_neg 0.18 权重）的证据**不是稳健显著**，")
    print("     它的 t 值在 lag=3 时 -3.35（过），lag=6 时 -2.86（不过）。")
    print("     任何『theme 反号是全项目最强信号』的表述都必须标注 lag 依赖。")

    with open(os.path.join(HERE, "audit_round13.json"), "w", encoding="utf-8") as f:
        json.dump({"lags_tested": LAGS, "t_bonf": T_BONF,
                   "n_robust": n_robust, "n_flips": n_flip,
                   "by_factor": out}, f, ensure_ascii=False, indent=1)
    print("\n✅ 已写 audit_round13.json")


if __name__ == "__main__":
    main()
