#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""round123b：IC 集中度诊断 —— 剔除最强季度后结论是否还成立。

动机：整体 h=20 IC=+0.058 / NW t=1.85 看似接近显著，
但分季度看 2026Q3 单季 IC=+0.1137 / 超额 +3.37%，远超其他季度。
必须回答：**去掉这一段，结论还在吗？**
"""
import json
import math
import os
from collections import defaultdict


def main():
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

    def nwt(xs, lag):
        n = len(xs)
        if n < 3:
            return None
        m = sum(xs) / n
        e = [x - m for x in xs]
        g0 = sum(v * v for v in e) / n
        s = g0
        for k in range(1, lag + 1):
            gk = sum(e[i] * e[i - k] for i in range(k, n)) / n
            s += 2.0 * (1.0 - k / (lag + 1.0)) * gk
        return m / math.sqrt(s / n) if s > 0 else None

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
        y, m = int(d0[:4]), int(d0[5:7])
        recs.append({"d": d0, "q": "%dQ%d" % (y, (m - 1) // 3 + 1),
                     "ic": ic, "ex": rt - pe, "pe": pe})

    def summarize(tag, arr):
        ics = [x["ic"] for x in arr if x["ic"] is not None]
        exs = [x["ex"] for x in arr]
        if not ics:
            print("  %-26s 无数据" % tag)
            return
        m = sum(ics) / len(ics)
        print("  %-26s n=%3d  IC=%+.4f  NWt=%+.2f  超额=%+.2f%%  胜率=%.1f%%"
              % (tag, len(ics), m, nwt(ics, h - 1),
                 sum(exs) / len(exs) * 100,
                 sum(1 for x in exs if x > 0) / len(exs) * 100))

    print("=== 全样本 vs 剔除单季 ===")
    summarize("全样本", recs)
    for qq in sorted({x["q"] for x in recs}):
        summarize("剔除 %s" % qq, [x for x in recs if x["q"] != qq])
    print()
    print("=== 剔除最强季 2026Q3 后，逐季表现 ===")
    rest = [x for x in recs if x["q"] != "2026Q3"]
    byq = defaultdict(list)
    for x in rest:
        byq[x["q"]].append(x)
    for qq in sorted(byq):
        summarize(qq, byq[qq])
    print()
    # 集中度：Q3 贡献占比
    all_ex = sum(x["ex"] for x in recs)
    q3_ex = sum(x["ex"] for x in recs if x["q"] == "2026Q3")
    print("超额总贡献: 全样本 %+.2f%%（按月累加），其中 2026Q3 占 %.1f%%"
          % (all_ex * 100, q3_ex / all_ex * 100 if all_ex else float("nan")))
    print()
    print("⚠️ 判据：若「剔除最强季」后 NW t 跌破 1.0 或转负，")
    print("   则整体结论由单一 regime 支撑，不得宣称「跨期有效」。")


if __name__ == "__main__":
    main()
