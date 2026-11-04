#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""round123b：分季度/分 regime 稳定性检验（h=20）。"""
import json
import math
import os
from collections import defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
W = {"score_fund": 0.20, "score_size_growth": 0.20, "score_tech_neg": 0.10,
     "score_quality": 0.10, "score_ep": 0.10, "score_dominant": 0.10,
     "score_growth": 0.10, "score_bias60": 0.05, "score_rsi_rev": 0.05}
DM = {"score_bias60": 5.0, "score_rsi_rev": 5.0}


def comp(r):
    t = w = 0.0
    for d, wt in W.items():
        v = r.get(d)
        if v is None:
            continue
        t += (float(v) / DM.get(d, 10.0)) * 10.0 * wt
        w += wt
    return t / w if w > 0 else None


def ranks(v):
    o = sorted(range(len(v)), key=lambda i: v[i])
    r = [0.0] * len(v)
    i = 0
    while i < len(o):
        j = i
        while j + 1 < len(o) and v[o[j + 1]] == v[o[i]]:
            j += 1
        a = (i + j) / 2.0 + 1.0
        for k in range(i, j + 1):
            r[o[k]] = a
        i = j + 1
    return r


def pear(x, y):
    n = len(x)
    if n < 3:
        return None
    mx, my = sum(x) / n, sum(y) / n
    sxy = sum((a - mx) * (b - my) for a, b in zip(x, y))
    sxx = sum((a - mx) ** 2 for a in x)
    syy = sum((b - my) ** 2 for b in y)
    if sxx <= 0 or syy <= 0:
        return None
    return sxy / math.sqrt(sxx * syy)


def main():
    with open(os.path.join(HERE, "data", "panel_round123.json"), encoding="utf-8") as f:
        p = json.load(f)
    dates, rows = p["dates"], p["rows"]
    with open(os.path.join(HERE, "data", "klines_long.json"), encoding="utf-8") as f:
        kl = json.load(f)

    h = 20
    recs = []
    for i, d0 in enumerate(dates):
        if i + h >= len(dates):
            break
        d1 = dates[i + h]
        sc, ret = {}, {}
        for c, r in rows[d0].items():
            s = comp(r)
            if s is None:
                continue
            b0 = kl.get(c, {}).get(d0)
            b1 = kl.get(c, {}).get(d1)
            if not b0 or not b1 or len(b0) < 2 or len(b1) < 2:
                continue
            if not b0[1] or not b1[1] or b0[1] <= 0:
                continue
            sc[c] = s
            ret[c] = b1[1] / b0[1] - 1.0
        if len(sc) < 200:
            continue
        ic = pear(ranks(list(sc.values())), ranks([ret[c] for c in sc]))
        pe = sum(ret.values()) / len(ret)
        rk = sorted(sc.items(), key=lambda x: -x[1])
        k = max(1, int(len(rk) * 0.10))
        rt = sum(ret[c] for c, _ in rk[:k]) / k
        recs.append((d0, ic, rt - pe, pe))

    q = defaultdict(list)
    for d0, ic, ex, pe in recs:
        y, m = int(d0[:4]), int(d0[5:7])
        q["%dQ%d" % (y, (m - 1) // 3 + 1)].append((ic, ex, pe))

    print("=== h=20 分季度稳定性 ===")
    print("%-8s %5s %10s %14s %12s" % ("季度", "n", "IC均值", "Top10%超额", "池EW收益"))
    print("-" * 56)
    for kk in sorted(q):
        arr = q[kk]
        ics = [x[0] for x in arr if x[0] is not None]
        exs = [x[1] for x in arr]
        pes = [x[2] for x in arr]
        ic_m = sum(ics) / len(ics) if ics else float("nan")
        print("%-8s %5d %+10.4f %+13.2f%% %+11.2f%%"
              % (kk, len(arr), ic_m, sum(exs) / len(exs) * 100,
                 sum(pes) / len(pes) * 100))
    print()
    neg = [kk for kk in sorted(q)
           if sum(x[1] for x in q[kk]) / len(q[kk]) < 0]
    negic = [kk for kk in sorted(q)
             if [x[0] for x in q[kk] if x[0] is not None]
             and sum(x[0] for x in q[kk] if x[0] is not None)
             / len([x[0] for x in q[kk] if x[0] is not None]) < 0]
    print("超额为负的季度: %s" % (neg if neg else "无"))
    print("IC为负的季度:   %s" % (negic if negic else "无"))
    print()
    # 池 EW 的季度收益（衡量 regime）
    print("=== 季度池EW收益（regime 代理）===")
    for kk in sorted(q):
        pes = [x[2] for x in q[kk]]
        print("  %-8s %+7.2f%%" % (kk, sum(pes) / len(pes) * 100))


if __name__ == "__main__":
    main()
