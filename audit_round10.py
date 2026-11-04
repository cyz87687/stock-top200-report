#!/usr/bin/env python3
"""Round10：检验「score_theme 反号纳入评分」是否成立。

背景（老大 2026-10-25 裁决）：score_theme 反号纳入评分。
但盘查发现一个必须先说清的事实：
  · score_theme      当前**不在** total 里（total 用的是 theme_intrinsic 0.08 + attention 0.02）
  · corr(score_theme, theme_intrinsic) = 0.159
  · corr(score_theme, attention)      = 0.182
  → 也就是说 IC 表里那条最强的负 alpha（h20 IC=-0.108/t=-2.70, n=58）
    对应的因子，其实从未进入评分。这既是机会（77截面实证 vs 8截面先验），
    也是风险（反号≠有效，需独立检验单调性与共线性）。

本脚本检验四件事：
  T1 反号后 IC 是否显著（NW t，Bonferroni 门槛）
  T2 反号后五分位是否单调（比 IC 更硬的证据）
  T3 与现有维度是否共线（避免重复计权）
  T4 与 theme_intrinsic / attention 谁是更好的 0.10 权重载体
"""
import json
import os
import sys
import math

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import factor_eval as fe

HOP = 20


def mean(a):
    return sum(a) / len(a) if a else None


def sd(a):
    if len(a) < 2:
        return None
    m = mean(a)
    return math.sqrt(sum((x - m) ** 2 for x in a) / (len(a) - 1))


def spearman(xs, ys):
    n = len(xs)
    if n < 5:
        return None
    def rank(v):
        o = sorted(range(n), key=lambda i: v[i])
        r = [0.0] * n
        for pos, i in enumerate(o):
            r[i] = pos
        return r
    rx, ry = rank(xs), rank(ys)
    mx, my = mean(rx), mean(ry)
    vx = sum((v - mx) ** 2 for v in rx)
    vy = sum((v - my) ** 2 for v in ry)
    if vx <= 0 or vy <= 0:
        return None
    return sum((rx[i] - mx) * (ry[i] - my) for i in range(n)) / (vx * vy) ** 0.5


def nw_t(xs, lag=None):
    n = len(xs)
    if n < 3:
        return None
    m = mean(xs)
    if lag is None:
        lag = min(HOP - 1, max(1, int(n ** (1 / 3)) * 2))
    d = [x - m for x in xs]
    s2 = sum(v * v for v in d) / n
    for k in range(1, min(lag, n - 1) + 1):
        w = 1.0 - k / (lag + 1.0)
        g = sum(d[i] * d[i + k] for i in range(n - k)) / n
        s2 += 2 * w * g
    se = math.sqrt(max(s2, 1e-18) / n)
    return m / se if se > 0 else None


def main():
    h = json.load(open(os.path.join(HERE, "factor_history.json")))
    snaps, _ = fe._drop_stale_price_snapshots(h["snapshots"])
    tdays, px = fe.px_index(h)
    tidx = {d: i for i, d in enumerate(tdays)}

    FACTORS = ["score_theme", "score_theme_intrinsic", "score_attention",
               "score_fund", "score_news", "score_tech",
               "score_oversold60", "score_bias60", "score_rsi_rev",
               "score_vol_pulse", "score_overbought"]

    # 逐截面逐因子算 IC（各周期）
    res = {f: {hh: [] for hh in (1, 5, 20)} for f in FACTORS}
    res["score_theme_neg"] = {hh: [] for hh in (1, 5, 20)}

    # 五分位收益（T+20，等权）
    q_ret = {f: {k: [] for k in range(1, 6)} for f in
             ("score_theme", "score_theme_neg")}

    for s in snaps:
        d0 = s["date"]
        if d0 not in tidx:
            continue
        i0 = tidx[d0]
        rows = s["rows"]
        for hh in (1, 5, 20):
            i1 = i0 + hh
            if i1 >= len(tdays):
                continue
            d1 = tdays[i1]
            recs = []
            for r in rows:
                c = r.get("code")
                m = px.get(c)
                if not m:
                    continue
                p0, p1 = m.get(d0), m.get(d1)
                if not p0 or not p1 or p0 <= 0:
                    continue
                recs.append((r, p1 / p0 - 1.0))
            if len(recs) < 40:
                continue
            fwd = [x[1] for x in recs]
            for f in FACTORS:
                vals, ys = [], []
                for r, fr in recs:
                    v = r.get(f)
                    if isinstance(v, (int, float)):
                        vals.append(float(v))
                        ys.append(fr)
                if len(vals) < 40:
                    continue
                ic = spearman(vals, ys)
                if ic is not None:
                    res[f][hh].append(ic)
                    if f == "score_theme":
                        res["score_theme_neg"][hh].append(-ic)

        # 五分位（T+20）
        i1 = i0 + 20
        if i1 < len(tdays):
            d1 = tdays[i1]
            recs = []
            for r in rows:
                m = px.get(r.get("code"))
                if not m:
                    continue
                p0, p1 = m.get(d0), m.get(d1)
                v = r.get("score_theme")
                if isinstance(v, (int, float)) and p0 and p1 and p0 > 0:
                    recs.append((float(v), p1 / p0 - 1.0))
            if len(recs) >= 50:
                recs.sort(key=lambda x: x[0])       # 分数升序
                n = len(recs)
                for k in range(5):
                    lo, hi = n * k // 5, n * (k + 1) // 5
                    blk = recs[lo:hi]
                    if blk:
                        q_ret["score_theme"][k + 1].append(mean([x[1] for x in blk]))
                recs.sort(key=lambda x: -x[0])      # 反号 = 分数降序
                for k in range(5):
                    lo, hi = n * k // 5, n * (k + 1) // 5
                    blk = recs[lo:hi]
                    if blk:
                        q_ret["score_theme_neg"][k + 1].append(mean([x[1] for x in blk]))

    print("=" * 74)
    print("T1  反号后 IC 与显著性（Bonferroni 门槛 |NW t|≥3.08，30 次检验）")
    print("=" * 74)
    print(f"{'因子':<24}{'h':<4}{'n':<6}{'IC':<10}{'NW t':<9}{'显著?':<8}")
    summary = {}
    for f in ("score_theme", "score_theme_neg"):
        summary[f] = {}
        for hh in (1, 5, 20):
            xs = res[f][hh]
            if len(xs) < 5:
                continue
            ic, t = mean(xs), nw_t(xs)
            summary[f][hh] = {"n": len(xs), "ic": round(ic, 4),
                              "nw_t": round(t, 2) if t else None}
            sig = "✅" if t and abs(t) >= 3.08 else ("~" if t and abs(t) >= 2 else "")
            print(f"{f:<24}{hh:<4}{len(xs):<6}{ic:<10.4f}"
                  f"{(t if t else 0):<9.2f}{sig:<8}")

    print("\n" + "=" * 74)
    print("T2  五分位单调性（T+20 等权平均收益，升序排列）")
    print("=" * 74)
    for f in ("score_theme", "score_theme_neg"):
        line = []
        for k in range(1, 6):
            v = q_ret[f][k]
            line.append(f"Q{k} {mean(v)*100:+6.2f}%" if v else f"Q{k}  n/a ")
        ns = [len(q_ret[f][k]) for k in range(1, 6)]
        print(f"  {f:<20} " + " | ".join(line) + f"   (n≈{max(ns)})")
        vals = [mean(q_ret[f][k]) for k in range(1, 6) if q_ret[f][k]]
        if len(vals) == 5:
            mono = all(vals[i] <= vals[i + 1] for i in range(4))
            anti = all(vals[i] >= vals[i + 1] for i in range(4))
            print(f"      → 严格递增 {mono} / 严格递减 {anti} / "
                  f"Q5−Q1 = {(vals[4]-vals[0])*100:+.2f} pct")

    print("\n" + "=" * 74)
    print("T3  共线性：与已入评分维度的相关（截面平均 Spearman，避免重复计权）")
    print("=" * 74)
    cors = {}
    for other in ("score_fund", "score_news", "score_tech", "score_theme_intrinsic",
                  "score_attention", "score_oversold60", "score_bias60",
                  "score_rsi_rev", "score_vol_pulse", "score_overbought"):
        acc = []
        for s in snaps:
            a, b = [], []
            for r in s["rows"]:
                x, y = r.get("score_theme"), r.get(other)
                if isinstance(x, (int, float)) and isinstance(y, (int, float)):
                    a.append(float(x))
                    b.append(float(y))
            if len(a) >= 40:
                c = spearman(a, b)
                if c is not None:
                    acc.append(c)
        if acc:
            cors[other] = round(mean(acc), 3)
    for k, v in sorted(cors.items(), key=lambda x: -abs(x[1])):
        flag = " ⚠️高共线" if abs(v) >= 0.7 else ""
        print(f"  corr(score_theme, {k:<22}) = {v:+.3f}{flag}")

    print("\n" + "=" * 74)
    print("T4  谁是 0.10 权重（theme 0.08 + attention 0.02）的更好载体")
    print("=" * 74)
    print("  候选 A: theme_intrinsic 0.08 + attention 0.02（现状）")
    for f in ("score_theme_intrinsic", "score_attention"):
        for hh in (1, 5, 20):
            xs = res[f][hh]
            if len(xs) >= 3:
                print(f"    {f:<24} h{hh:<3} n={len(xs):<3} IC={mean(xs):+.4f} "
                      f"NW t={nw_t(xs):+.2f}" if nw_t(xs) else "")
            else:
                print(f"    {f:<24} h{hh:<3} n={len(xs):<3} 样本不足")
    print("\n  候选 B: score_theme 反号（n=77/73/58 满样本）")
    for hh in (1, 5, 20):
        xs = res["score_theme_neg"][hh]
        if len(xs) >= 5:
            print(f"    score_theme_neg          h{hh:<3} n={len(xs):<3} "
                  f"IC={mean(xs):+.4f} NW t={nw_t(xs):+.2f}")

    out = {"t1_ic": summary, "t3_collinearity": cors,
           "t2_quintile": {f: {str(k): (round(mean(v) * 100, 2) if v else None)
                               for k, v in q_ret[f].items()} for f in q_ret}}
    with open(os.path.join(HERE, "audit_round10.json"), "w", encoding="utf-8") as fp:
        json.dump(out, fp, ensure_ascii=False, indent=2)
    print("\n✅ 已写 audit_round10.json")


if __name__ == "__main__":
    main()
