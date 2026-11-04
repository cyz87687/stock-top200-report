#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""#293d 判定：round34 的 quality +0.0361 是真实 alpha 还是污染伪影？

假设：score_rows 的输入行序由 step2 决定 = 当期 total 降序。
     NaN 击穿 rank_pct 后，排名塌陷但仍**部分保留输入序**，
     于是 score_quality 被注入「当期 total」的信息 → 变成一个总分代理。
     总分本身有 alpha → quality 表现出正 IC，但这不是质量的功劳。

检验：把「上期 total」当作因子单独算 IC。
     · 若 quality(污染版) 的 IC ≈ 上期 total 的 IC  → 伪影成立
     · 若 quality(修复版) 的 IC 与 total 无关        → 修复后才是真质量
"""
import json
import math
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import factor_eval as fe     # noqa: E402
import audit_round34 as A34  # noqa: E402


def is_num(x):
    return isinstance(x, (int, float)) and not isinstance(x, bool) and x == x and not math.isinf(x)


def rank_corr(a, b):
    return fe.rank_ic(a, b)


def main():
    panel = json.load(open(os.path.join(HERE, "data", "panel_round30.json"), encoding="utf-8"))
    px = A34.load_px()
    tdays = A34.build_tdays(px)
    tidx = {x: i for i, x in enumerate(tdays)}
    dates = panel["dates"]

    fwj = json.load(open(os.path.join(HERE, "factor_weights.json"), encoding="utf-8"))
    W = {k: float(v or 0) for k, v in (fwj.get("weights") or {}).items()}
    DMAX = {k: float(v) for k, v in (fwj.get("dim_max") or {}).items()}

    def prod_total(vals):
        num = den = 0.0
        for k, w in W.items():
            if w <= 0:
                continue
            v = vals.get(k)
            if is_num(v):
                num += min(v, DMAX.get(k, 10.0)) / DMAX.get(k, 10.0) * 10.0 * w
                den += w
        return num / max(0.5, den) * 10.0 if den > 0 else None

    # 逐截面：当前 total、quality
    tot_now = {}
    qual = {}
    for i, d0 in enumerate(dates):
        cur = panel["rows"].get(d0) or {}
        tot_now[d0] = {c: prod_total(v) for c, v in cur.items() if is_num(v.get("score_size_ind"))}
        qual[d0] = {c: v.get("score_quality") for c, v in cur.items()}

    # 上期 total（前一个截面）
    tot_prev = {}
    for i, d0 in enumerate(dates):
        tot_prev[d0] = tot_now[dates[i - 1]] if i > 0 else {}

    print("=" * 94)
    print("【检验一】污染版 quality 与「上期 total」的截面相关性")
    print("=" * 94)
    cors = []
    for d0 in dates:
        cur = panel["rows"].get(d0) or {}
        xs, ys = [], []
        for c in cur:
            q = qual[d0].get(c)
            t = tot_prev[d0].get(c)
            if is_num(q) and is_num(t):
                xs.append(q)
                ys.append(t)
        if len(xs) >= 40:
            ic = rank_corr(xs, ys)
            if ic is not None:
                cors.append(ic)
    if cors:
        mu = sum(cors) / len(cors)
        sd = (sum((x - mu) ** 2 for x in cors) / (len(cors) - 1)) ** 0.5
        print(f"  rank corr(quality_污染, 上期total) = {mu:+.4f}  "
              f"t={mu/(sd/len(cors)**0.5):+.2f}  n={len(cors)}")
        print("  → 若显著为正，说明污染版 quality 里混入了上期总分（信息泄漏）")

    print()
    print("=" * 94)
    print("【检验二】上期 total 自身的 IC（若无 alpha，则 quality 的正 IC 不可能是它带来的）")
    print("=" * 94)
    for hop in (5, 20, 40):
        ics = []
        for d0 in dates:
            if tidx.get(d0) is None or tidx[d0] + hop >= len(tdays):
                continue
            cur = panel["rows"].get(d0) or {}
            xs, ys = [], []
            for c in cur:
                t = tot_prev[d0].get(c)
                if not is_num(t):
                    continue
                m = px.get(c)
                i0 = tidx.get(d0)
                if not m or i0 is None:
                    continue
                p0, p1 = m.get(d0), m.get(tdays[i0 + hop])
                if p0 and p1 and p0 > 0:
                    xs.append(t)
                    ys.append(p1 / p0 - 1.0)
            if len(xs) >= 40:
                ic = rank_corr(xs, ys)
                if ic is not None:
                    ics.append(ic)
        if len(ics) >= 5:
            mu = sum(ics) / len(ics)
            sd = (sum((x - mu) ** 2 for x in ics) / (len(ics) - 1)) ** 0.5
            print(f"  h{hop:<3d} 上期total IC {mu:+.4f}  t={mu/(sd/len(ics)**0.5):+.2f}  n={len(ics)}")

    print()
    print("=" * 94)
    print("【检验三】污染版 quality 的分数是否沿输入序（total 降序）单调？")
    print("=" * 94)
    d0 = dates[-1]
    cur = panel["rows"].get(d0) or {}
    pairs = [(c, v.get("score_quality"), tot_now[d0].get(c))
             for c, v in cur.items()]
    pairs = [(c, q, t) for c, q, t in pairs if is_num(q) and is_num(t)]
    pairs.sort(key=lambda x: -x[2])
    print(f"  截面 {d0} 按当期 total 降序，前 12 名的 quality：")
    for c, q, t in pairs[:12]:
        print(f"    {c}  total={t:6.2f}  quality={q:5.2f}")
    print(f"  后 6 名：")
    for c, q, t in pairs[-6:]:
        print(f"    {c}  total={t:6.2f}  quality={q:5.2f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
