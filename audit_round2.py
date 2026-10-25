#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""第二轮 P0/P1 审计（2026-10-25）：回答三个尚未被数据回答的问题。

Q1  已发现「综合分 IC < 基本面单维 IC」，但这是否只是权重没调好？
    → 若把 fund 权重提到 1.0（其余归零）就能复现 fund 单维 IC，
      则问题在权重；若不能，则问题在评分口径本身（fund 分档过粗）。
Q2  基本面因子 10 月 IC 转负，是因子失效还是市场逆风？
    → 对比同期池内等权基准收益。基准也跌 → 逆风；基准涨而 fund 头组跌 → 失效。
Q3  score_fund 的 5 档离散度是否足以支撑排序？
    → 统计 score_fund 的唯一取值数与档位分布。若只有 5 个取值，
      490 只股票挤在 5 档里，头组内部无法区分。
"""
import json
import os
import sys
from collections import Counter

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.argv = ["audit"]

import factor_eval as fe  # noqa: E402

COST = 20.0


def _load():
    h = fe.load_history()
    snaps = [s for s in h.get("snapshots", []) if s.get("rows")]
    tdays, px = fe.px_index(h)
    opens = fe.open_index(h)
    tidx = {d: i for i, d in enumerate(tdays)}
    aligned = [s for s in snaps if s["date"] in tidx]
    return aligned, tdays, px, opens, tidx


def _paired_t(a, b):
    import math
    n = min(len(a), len(b))
    if n < 10:
        return None, n
    d = [a[i] - b[i] for i in range(n)]
    m = sum(d) / n
    var = sum((x - m) ** 2 for x in d) / (n - 1)
    if var <= 0:
        return None, n
    return m / ((var ** 0.5) / (n ** 0.5)), n


def q1(aligned, tdays, px, opens):
    """把权重全给 fund，能否复现 fund 单维 IC？"""
    print("=" * 78)
    print("Q1  综合分低 IC 的根因：权重问题 还是 评分口径问题？")
    print("=" * 78)
    fund = [{"date": s["date"],
             "rows": [{"code": r["code"], "total": r.get("score_fund")} for r in s["rows"]]}
            for s in aligned]
    comp = [{"date": s["date"],
             "rows": [{"code": r["code"], "total": r.get("total")} for r in s["rows"]]}
            for s in aligned]
    print(f"{'周期':<6}{'综合分IC':>11}{'fund单维IC':>12}{'差':>9}{'配对t':>8}")
    for h in (1, 5, 20):
        f = fe._eval_tradable_ic(fund, [h], tdays, px, opens, cost_bps=COST)[str(h)]
        c = fe._eval_tradable_ic(comp, [h], tdays, px, opens, cost_bps=COST)[str(h)]
        ft, ct = f.get("ic_series") or [], c.get("ic_series") or []
        t, n = _paired_t(ft, ct)
        print(f"T+{h:<5}{c['ic_mean']:>+11.4f}{f['ic_mean']:>+12.4f}"
              f"{f['ic_mean'] - c['ic_mean']:>+9.4f}{(f'{t:+.2f}' if t is not None else 'n/a'):>8}")

    # ---- 关键：score_fund 的取值离散度 ----
    vals = Counter()
    for s in aligned[-5:]:
        for r in s["rows"]:
            v = r.get("score_fund")
            if isinstance(v, (int, float)):
                vals[round(float(v), 3)] += 1
    tot = sum(vals.values())
    print(f"\nscore_fund 唯一取值数（最近 5 个截面合并）：{len(vals)} 种，样本 {tot}")
    print("档位分布：")
    for v, c in sorted(vals.items()):
        print(f"   {v:>6.2f}  {c:>6} 只  ({100.0 * c / tot:>5.1f}%)")
    top = max(vals.values())
    print(f"\n→ 最大单一档位占比 {100.0 * top / tot:.1f}%（{top} 只挤在同一分值）")
    print("  若该比例 > 40%，说明 fund 分档过粗，Weighted 排序会把大量股票判为同分，")
    print("  头组构成近乎随机 → 加权综合分必然稀释 fund 的排序能力。")
    return len(vals), 100.0 * top / tot


def q2(aligned, tdays, px, opens, tidx):
    """10 月 IC 转负：因子失效 还是 市场逆风？"""
    print("\n" + "=" * 78)
    print("Q2  基本面 10 月 IC 转负：因子失效 vs 市场逆风？")
    print("=" * 78)
    bym = {}
    for s in aligned:
        bym.setdefault(s["date"][:7], []).append(s)
    print(f"{'月份':<9}{'n':>4}{'fund头组净收益%':>16}{'池内等权%':>12}"
          f"{'头组超额pct':>13}{'fund IC':>10}")
    for m in sorted(bym):
        sub = bym[m]
        fund = [{"date": s["date"],
                 "rows": [{"code": r["code"], "total": r.get("score_fund")}
                          for r in s["rows"]]} for s in sub]
        r = fe._eval_tradable_ic(fund, [5], tdays, px, opens, cost_bps=COST)["5"]
        # 池内等权基准（同口径 t+1 开盘、持有 5 日、扣费）
        pool = []
        for s in sub:
            i0 = tidx.get(s["date"])
            if i0 is None or i0 + 6 >= len(tdays):
                continue
            db, ds = tdays[i0 + 1], tdays[i0 + 6]
            rr = []
            for row in s["rows"]:
                mo = opens.get(row["code"])
                if not mo:
                    continue
                p0, p1 = mo.get(db), mo.get(ds)
                if p0 and p1 and p0 > 0:
                    rr.append((p1 - p0) / p0 * 100.0 - COST / 100.0)
            if rr:
                pool.append(sum(rr) / len(rr))
        pm = sum(pool) / len(pool) if pool else float("nan")
        hm = r.get("long_mean")
        ex = (hm - pm) if isinstance(hm, (int, float)) and pool else float("nan")
        print(f"{m:<9}{r.get('n') or 0:>4}{hm if hm is not None else float('nan'):>16.2f}"
              f"{pm:>12.2f}{ex:>+13.2f}{r.get('ic_mean') or 0:>+10.4f}")
    print("\n判读：若「fund头组净收益」与「池内等权」同向下跌、且头组超额仍为正，")
    print("      则为市场逆风而非因子失效；若头组超额转负，才是因子失效。")


def q3(aligned, tdays, px, opens):
    """头组收益率与因子排序的单调性检验：5 分档逐档平均收益。"""
    print("\n" + "=" * 78)
    print("Q3  分档单调性：score_fund 各档位的头组实际收益是否单调递增？")
    print("=" * 78)
    for h in (5, 20):
        print(f"\n【T+{h}】")
        buckets = {}
        for s in aligned:
            i0 = fe_tidx = None
        break
    return


def _mono(aligned, tdays, px, opens, tidx, h):
    buckets = {}
    for s in aligned:
        i0 = tidx.get(s["date"])
        if i0 is None or i0 + 1 + h >= len(tdays):
            continue
        db, ds = tdays[i0 + 1], tdays[i0 + 1 + h]
        for r in s["rows"]:
            v = r.get("score_fund")
            mo = opens.get(r["code"])
            if not isinstance(v, (int, float)) or not mo:
                continue
            p0, p1 = mo.get(db), mo.get(ds)
            if p0 and p1 and p0 > 0:
                buckets.setdefault(round(float(v), 2), []).append(
                    (p1 - p0) / p0 * 100.0 - COST / 100.0)
    out = []
    for v in sorted(buckets):
        arr = buckets[v]
        out.append((v, len(arr), sum(arr) / len(arr)))
    return out


def q3b(aligned, tdays, px, opens, tidx):
    print("\n" + "=" * 78)
    print("Q3  score_fund 分档单调性（各档实际持有收益，t+1 开盘买入·扣 20bp）")
    print("=" * 78)
    for h in (5, 20):
        rows = _mono(aligned, tdays, px, opens, tidx, h)
        print(f"\n【T+{h}】")
        print(f"{'fund分值':>9}{'样本数':>9}{'平均收益%':>12}")
        prev, inc, dec = None, 0, 0
        for v, n, m in rows:
            flag = ""
            if prev is not None:
                if m > prev:
                    inc += 1
                    flag = " ↑"
                else:
                    dec += 1
                    flag = " ↓"
            print(f"{v:>9.2f}{n:>9}{m:>+12.2f}{flag}")
            prev = m
        print(f"→ 单调上升 {inc} 次 / 下降 {dec} 次")
        if dec > inc:
            print("  ⚠️ 分档收益非单调（下降多于上升）→ fund 分值与真实收益关系不稳，")
            print("     说明 score_fund 的档位定义需要重做，而非权重问题。")


def main():
    aligned, tdays, px, opens, tidx = _load()
    print(f"样本：{len(aligned)} 个有效截面，"
          f"{aligned[0]['date']} → {aligned[-1]['date']}，"
          f"价格库 {len(tdays)} 个交易日\n")
    q1(aligned, tdays, px, opens)
    q2(aligned, tdays, px, opens, tidx)
    q3b(aligned, tdays, px, opens, tidx)


if __name__ == "__main__":
    main()
