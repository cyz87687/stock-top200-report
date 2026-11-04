#!/usr/bin/env python3
"""P0-1 第六阶段：滚动历史池（无前视）——最接近真实可实施的选池设计。

## 设计动机
前两阶段证明：
  · 池内过滤（母体不变）      最好 -3.92pct，无法转正
  · 1012 只并集内选低价        +2.87pct，但**含前视偏差**（用到"未来才上榜"的股）
  · 池内最低价+剔追高（无前视）  +1.54pct，三段全正，但绝对收益仍 -6.9%

## 本阶段设计：滚动历史池
在交易日 t，可用信息集 = 「今日池 rows」∪「过去 K 个交易日内曾出现在池中的股票」
（后者对 t 时刻是完全已知的历史信息，无前视）。然后：
  ① 在合并母体上按价格升序取低价部分
  ② 剔除 20 日涨幅前 20%（追高过滤）
  ③ 可选价格地板

这比「只用今日池」母体更大、更少被单日热度左右，且比「1012 全并集」诚实。
滚动池的股票在 t 日未必热门，但它们仍在交易、有价格、可买入——这就是真实
可执行的选股范围（相当于维护一个候选池，每日更新）。

## 对照
R0 今日池（基线）
R1 今日池低价50%+剔追高（前阶段 P5）
R2 滚动5日池 → 低价50%+剔追高
R3 滚动20日池 → 低价50%+剔追高
R4 滚动20日池 → 低价30%+剔追高
R5 滚动20日池 → 低价50%+剔追高+地板2元
"""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import factor_eval as fe

HOP = 20


def load():
    h = json.load(open(os.path.join(HERE, "factor_history.json")))
    snaps, _ = fe._drop_stale_price_snapshots(h["snapshots"])
    tdays, px = fe.px_index(h)
    return snaps, tdays, px


def med(a):
    if not a:
        return None
    a = sorted(a)
    n = len(a)
    return a[n // 2] if n % 2 else (a[n // 2 - 1] + a[n // 2]) / 2.0


def ret(px, code, tdays, i0, i1):
    m = px.get(code) or {}
    p0, p1 = m.get(tdays[i0]), m.get(tdays[i1])
    if not p0 or not p1 or p0 <= 0:
        return None
    return p1 / p0 - 1.0


def build(KMAX=20):
    snaps, tdays, px = load()
    tidx = {d: i for i, d in enumerate(tdays)}
    # 按日期索引快照
    snap_by_date = {}
    for s in snaps:
        snap_by_date[s["date"]] = s
    ordered = [s for s in snaps if s["date"] in tidx]
    ordered.sort(key=lambda s: tidx[s["date"]])

    # 滚动历史：每个快照位置，累积过去 KMAX 个快照出现过的 code 集合
    data = []
    for pos, s in enumerate(ordered):
        d0 = s["date"]
        i0 = tidx[d0]
        if i0 + HOP >= len(tdays) or i0 < 20:
            continue
        i1 = i0 + HOP

        # 今日池（含价格、fwd、r20）
        today = {}
        for r in s["rows"]:
            c = r["code"]
            pr = r.get("price")
            if not pr:
                continue
            today[c] = pr

        # 滚动池：过去 KMAX 个快照的并集（仅用 pos 之前的信息）
        roll = {}
        lo = max(0, pos - KMAX)
        for j in range(lo, pos):
            for r in ordered[j]["rows"]:
                c = r["code"]
                if c not in roll:
                    pr = (px.get(c) or {}).get(d0)
                    if pr:
                        roll[c] = pr

        def make_rows(codes_prices):
            out = []
            for c, pr in codes_prices.items():
                fwd = ret(px, c, tdays, i0, i1)
                if fwd is None:
                    continue
                out.append({"c": c, "fwd": fwd, "price": pr,
                            "r20": ret(px, c, tdays, i0 - 20, i0)})
            return out

        today_rows = make_rows(today)
        roll_rows = make_rows(roll) if roll else []
        merged = {**today, **roll}
        merged_rows = make_rows(merged)

        if len(today_rows) < 100:
            continue
        data.append({
            "date": d0,
            "today": today_rows,
            "roll5": make_rows({**today, **{c: p for c, p in roll.items()
                                             if c in {r["code"] for s2 in ordered[max(0, pos - 5):pos]
                                                      for r in s2["rows"]}}}),
            "roll20": roll_rows if roll_rows else merged_rows,
            "merged": merged_rows,
        })
    return data, tdays


def pick(rows, frac, drop_mom=False, floor=0.0):
    v = [x for x in rows if x["price"] >= floor]
    v.sort(key=lambda x: x["price"])
    v = v[:max(30, int(len(v) * frac))]
    if drop_mom:
        r20s = [x["r20"] for x in v if x["r20"] is not None]
        if len(r20s) >= 20:
            thr = sorted(r20s)[int(len(r20s) * 0.80)]
            v = [x for x in v if x["r20"] is None or x["r20"] < thr]
    return v


def evaluate(name, key, fn, data):
    meds, exs, wins, sizes, worst, losers = [], [], 0, [], [], []
    for rec in data:
        base = rec["today"]
        sub = fn(rec[key])
        if len(sub) < 30:
            continue
        sm = med([x["fwd"] for x in sub])
        bm = med([x["fwd"] for x in base])
        if sm is None or bm is None:
            continue
        meds.append(sm)
        exs.append(sm - bm)
        sizes.append(len(sub))
        worst.append(min(x["fwd"] for x in sub))
        losers.append(100.0 * sum(1 for x in sub if x["fwd"] < -0.30) / len(sub))
        if sm > bm:
            wins += 1
    if not exs:
        return None
    return {"name": name, "n": len(exs),
            "med_pct": round(med(meds) * 100, 2),
            "exc_med": round(med(exs) * 100, 2),
            "exc_mean": round(fe._mean(exs) * 100, 2),
            "beat": round(100.0 * wins / len(exs), 1),
            "size": int(fe._mean(sizes)),
            "worst_med": round(med(worst) * 100, 2),
            "loser30": round(med(losers), 1)}


def main():
    data, tdays = build()
    print(f"有效截面 {len(data)}（{data[0]['date']} ~ {data[-1]['date']}）")
    print("基准 = 当日池全取；全部规则无前视偏差\n")

    rules = [
        ("R0 今日池（基线）", "today", lambda r: r),
        ("R1 今日池·低价50%+剔追高", "today", lambda r: pick(r, 0.50, True)),
        ("R2 滚动5日池·低价50%+剔追高", "roll5", lambda r: pick(r, 0.50, True)),
        ("R3 滚动20日池·低价50%+剔追高", "roll20", lambda r: pick(r, 0.50, True)),
        ("R4 滚动20日池·低价30%+剔追高", "roll20", lambda r: pick(r, 0.30, True)),
        ("R5 滚动20日池·低价50%+剔追高+地板2元", "roll20",
         lambda r: pick(r, 0.50, True, 2.0)),
    ]

    print(f"{'规则':<32} {'入场日':>5} {'子集中位':>9} {'超额中位':>9} "
          f"{'超额均值':>9} {'胜率':>7} {'只数':>5} {'最差单票':>9} {'跌>30%':>7}")
    print("-" * 104)
    res = []
    for name, key, fn in rules:
        r = evaluate(name, key, fn, data)
        if not r:
            print(f"{name:<32} 无数据")
            continue
        res.append(r)
        print(f"{r['name']:<32} {r['n']:>5} {r['med_pct']:>+8.2f}% "
              f"{r['exc_med']:>+8.2f}pct {r['exc_mean']:>+8.2f} "
              f"{r['beat']:>6.1f}% {r['size']:>5} {r['worst_med']:>+8.2f}% "
              f"{r['loser30']:>6.1f}%")

    k = len(data) // 3
    blocks = [data[:k], data[k:2 * k], data[2 * k:]]
    print("\n=== 分三段超额 ===")
    print(f"{'规则':<32} {'段1':>9} {'段2':>9} {'段3':>9}")
    for name, key, fn in rules:
        vals = []
        for b in blocks:
            r = evaluate(name, key, fn, b)
            vals.append(f"{r['exc_med']:>+8.2f}" if r else "     NA ")
        print(f"{name:<32} " + " ".join(vals))

    out = os.path.join(HERE, "pool_rolling_backtest.json")
    json.dump({"n_days": len(data), "results": res},
              open(out, "w"), ensure_ascii=False, indent=1)
    print(f"\n写入 {out}")


if __name__ == "__main__":
    main()
