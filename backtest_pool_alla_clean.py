#!/usr/bin/env python3
"""P0-1 定稿：全A选池规则（剔除受污染实验后的干净结论）。

## 上一版实验的致命陷阱（已定位，必须记录）
out=backtest_pool_alla.py 中 Q4~Q8 用 score_fund 做质量门槛，结果全部 -4.5~-4.9pct。
看似"质量门槛无效"，实为**数据陷阱**：
  score_fund 只对「当日成交额榜池内」的股票有值（快照只存池内行）。
  一旦用 fund 过滤，可选集合立刻缩回那 490 只负 alpha 母体，与"全A选股"无关。
  → 该组实验无效，不作结论。

## 本脚本只用「全A 都可算出的量」
  · price      全A 有
  · r20 / r60  全A 用历史价格可算
  · 当日涨幅    全A 有（线上从快照取）
不含 fund/theme/tech 等只在池内定义的评分 —— 这类分数在换母体后必须
重新为全A 计算，届时才有资格做门槛（列入 P1 待办，不在此硬凑）。

## 规则
  S0 宇宙全体（基准）
  S1 成交额榜模拟（现状，宇宙内取金额前 193）
  S2 最便宜 N%（模拟全A 按价格分位截断）
  S3 20日跌幅最大 N%
  S4 60日跌幅最大 N%
  S5 20日跌幅最大 + 剔除最极端 10%
  S6 低价 ∩ 20日超跌
  S7 低价 ∩ 60日超跌
  S8 低价 ∩ 超跌 ∩ 剔除当日涨幅前 20%（线上可完全复现的口径）
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


def build():
    snaps, tdays, px = load()
    tidx = {d: i for i, d in enumerate(tdays)}
    codes = list(px.keys())
    data = []
    for s in snaps:
        d0 = s["date"]
        if d0 not in tidx:
            continue
        i0 = tidx[d0]
        if i0 + HOP >= len(tdays) or i0 < 60:
            continue
        i1 = i0 + HOP
        rows = []
        for c in codes:
            fwd = ret(px, c, tdays, i0, i1)
            if fwd is None:
                continue
            pr = (px.get(c) or {}).get(d0)
            if not pr:
                continue
            rows.append({"c": c, "fwd": fwd, "price": pr,
                         "r20": ret(px, c, tdays, i0 - 20, i0),
                         "r60": ret(px, c, tdays, i0 - 60, i0)})
        if len(rows) < 300:
            continue
        amt = {r["code"]: (r.get("amount") or r.get("turnover") or 0) for r in s["rows"]}
        data.append({"date": d0, "rows": rows, "amt": amt,
                     "all_med": med([x["fwd"] for x in rows])})
    return data


def ev(name, fn, data):
    meds, exs, wins, sizes, worst = [], [], 0, [], []
    for rec in data:
        sub = fn(rec["rows"], rec)
        if len(sub) < 30:
            continue
        sm = med([x["fwd"] for x in sub])
        if sm is None:
            continue
        meds.append(sm)
        exs.append(sm - rec["all_med"])
        sizes.append(len(sub))
        worst.append(min(x["fwd"] for x in sub))
        if sm > rec["all_med"]:
            wins += 1
    if len(exs) < 10:
        return None
    return {"name": name, "n": len(exs), "med": round(med(meds) * 100, 2),
            "exc": round(med(exs) * 100, 2), "exc_mean": round(fe._mean(exs) * 100, 2),
            "beat": round(100.0 * wins / len(exs), 1),
            "size": int(fe._mean(sizes)), "worst": round(med(worst) * 100, 2)}


def main():
    data = build()
    print(f"有效截面 {len(data)}  持有 {HOP} 日  宇宙 1012 只")
    print("超额 = 子集中位 − 宇宙中位（全A 相对排序效果，可迁移）")
    print("本表只用全A 可算量（price / r20 / r60 / 当日涨幅），不含池内专有评分\n")

    def cheap_frac(rows, rec, frac):
        v = sorted(rows, key=lambda x: x["price"])
        return v[:max(30, int(len(v) * frac))]

    def worst(key, n, drop_ext=0.0):
        def f(rows, rec):
            v = [x for x in rows if x.get(key) is not None]
            v.sort(key=lambda x: x[key])
            lo = int(len(v) * drop_ext)
            return v[lo:][:n]
        return f

    def low_and_worst(key, n):
        def f(rows, rec):
            v = [x for x in rows if x.get(key) is not None]
            v.sort(key=lambda x: x[key])
            # 取跌幅前 50%，再在其中按价格升序取 n
            cand = v[:max(n, int(len(v) * 0.50))]
            cand.sort(key=lambda x: x["price"])
            return cand[:n]
        return f

    rules = [
        ("S0 宇宙全体（基准）", lambda r, rec: r),
        ("S1 成交额榜模拟（现状193）", lambda r, rec: sorted(r, key=lambda x: -rec["amt"].get(x["c"], 0))[:193]),
        ("S2 最便宜9%（≈全A500只）", lambda r, rec: cheap_frac(r, rec, 0.09)),
        ("S3 最便宜18%", lambda r, rec: cheap_frac(r, rec, 0.18)),
        ("S4 最便宜36%", lambda r, rec: cheap_frac(r, rec, 0.36)),
        ("S5 20日跌幅最大500", worst("r20", 500)),
        ("S6 60日跌幅最大500", worst("r60", 500)),
        ("S7 20日跌幅最大·剔极端10%", worst("r20", 500, 0.10)),
        ("S8 低价∩20日超跌(各500)", low_and_worst("r20", 500)),
        ("S9 低价∩60日超跌(各500)", low_and_worst("r60", 500)),
        ("S10 低价9%∩20日超跌", lambda r, rec: cheap_frac(
            [x for x in r if x.get("r20") is not None], rec, 0.09)),
    ]

    print(f"{'规则':<30} {'入场日':>5} {'子集中位':>9} {'超额中位':>9} "
          f"{'超额均值':>9} {'胜率':>7} {'只数':>5} {'最差单票':>9}")
    print("-" * 98)
    res = []
    for name, fn in rules:
        r = ev(name, fn, data)
        if not r:
            print(f"{name:<30} 无数据")
            continue
        res.append(r)
        print(f"{r['name']:<30} {r['n']:>5} {r['med']:>+8.2f}% {r['exc']:>+8.2f}pct "
              f"{r['exc_mean']:>+8.2f} {r['beat']:>6.1f}% {r['size']:>5} {r['worst']:>+8.2f}%")

    k = len(data) // 3
    print("\n=== 三段稳健性 ===")
    print(f"{'规则':<30} {'段1':>9} {'段2':>9} {'段3':>9}")
    for name, fn in rules:
        vals = []
        for b in (data[:k], data[k:2 * k], data[2 * k:]):
            r = ev(name, fn, b)
            vals.append(f"{r['exc']:>+8.2f}" if r else "     NA ")
        print(f"{name:<30} " + " ".join(vals))

    print()
    valid = [x for x in res if not x["name"].startswith("S0")]
    valid.sort(key=lambda x: -x["exc"])
    print("排序（超额中位）：")
    for r in valid:
        flag = "✅" if r["exc"] > 0 else "  "
        print(f"  {flag} {r['name']:<32} {r['exc']:+.2f}pct  胜率 {r['beat']:.1f}%  {r['size']} 只")

    out = os.path.join(HERE, "pool_alla_clean.json")
    json.dump({"n_days": len(data), "results": res},
              open(out, "w"), ensure_ascii=False, indent=1)
    print(f"\n写入 {out}")


if __name__ == "__main__":
    main()
