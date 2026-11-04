#!/usr/bin/env python3
"""P0-1 第五阶段：消除前视偏差后的可实施选池方案。

## 关键方法学问题（自我纠错）
第四阶段的 U6（1012 只并集内选最低价 500）存在**前视偏差**：
  1012 只 = 「历史上曾经进入过成交额 TOP500」的并集。在某个日期 t 选中一只
  当时并不热门、但**未来某个时刻才上榜**的股票，等于用了未来信息。
  这正是 U6 超额高达 +2.87pct 的部分来源——它偷偷选到了「后来变热的股票」。

## 修正口径（无前视）
在每个截面 t，只用**该日 t 实际可得的池子**（snapshot.rows，当日成交额 TOP N），
在其内按价格升序取最低的一定比例。这对应真实可实施流程：
  ① 每日抓成交额 TOP1500（现 fetch 已抓 15 页 ×100）
  ② 剔除 ST/退市/停牌（现 eligibility_filter 已做）
  ③ 按价格从低到高取足够数量（新增，纯快照字段，零额外接口成本）

## 同时对照
- P0 基线：今日池子全取（现状）
- P1 池内最低价 50%
- P2 池内最低价 30%
- P3 池内最低价 50% + 价格地板 2 元
- P4 池内最低价 50% + 价格地板 3 元
- P5 池内最低价 50% + 剔除 20 日涨幅前 20%（双条件，需 K 线）
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
    snaps, tdays = None, None
    snaps, tdays, px = load()
    tidx = {d: i for i, d in enumerate(tdays)}
    data = []
    for s in snaps:
        d0 = s["date"]
        if d0 not in tidx:
            continue
        i0 = tidx[d0]
        if i0 + HOP >= len(tdays) or i0 < 20:
            continue
        i1 = i0 + HOP
        rows = []
        for r in s["rows"]:
            c = r["code"]
            fwd = ret(px, c, tdays, i0, i1)
            if fwd is None:
                continue
            pr = r.get("price")
            if not pr:
                continue
            rows.append({"c": c, "fwd": fwd, "price": pr,
                         "r20": ret(px, c, tdays, i0 - 20, i0)})
        if len(rows) < 100:
            continue
        data.append({"date": d0, "rows": rows,
                     "all_med": med([x["fwd"] for x in rows])})
    return data, tdays


def evaluate(name, fn, data):
    meds, exs, wins, sizes = [], [], 0, []
    worst, losers = [], []
    for rec in data:
        sub = fn(rec["rows"])
        if len(sub) < 30:
            continue
        sm = med([x["fwd"] for x in sub])
        if sm is None:
            continue
        meds.append(sm)
        exs.append(sm - rec["all_med"])
        sizes.append(len(sub))
        worst.append(min(x["fwd"] for x in sub))
        losers.append(100.0 * sum(1 for x in sub if x["fwd"] < -0.30) / len(sub))
        if sm > rec["all_med"]:
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
    print("⚠️ 本表全部规则只用「当日快照可得信息」，无前视偏差\n")

    def keep_cheap(frac):
        def f(rows):
            v = sorted(rows, key=lambda x: x["price"])
            return v[:max(30, int(len(v) * frac))]
        return f

    def cheap_floor(frac, floor):
        def f(rows):
            v = sorted(rows, key=lambda x: x["price"])
            v = [x for x in v if x["price"] >= floor]
            return v[:max(30, int(len(rows) * frac))]
        return f

    def cheap_nomom(frac):
        """最低价 frac + 剔除 20 日涨幅前 20%。"""
        def f(rows):
            v = sorted(rows, key=lambda x: x["price"])
            v = v[:max(30, int(len(v) * frac))]
            r20s = [x["r20"] for x in v if x["r20"] is not None]
            if len(r20s) < 20:
                return v
            thr = sorted(r20s)[int(len(r20s) * 0.80)]
            return [x for x in v if x["r20"] is None or x["r20"] < thr]
        return f

    rules = {
        "P0 基线（今日池全取）": lambda rows: rows,
        "P1 池内最低价50%": keep_cheap(0.50),
        "P2 池内最低价30%": keep_cheap(0.30),
        "P3 最低价50%+地板2元": cheap_floor(0.50, 2.0),
        "P4 最低价50%+地板3元": cheap_floor(0.50, 3.0),
        "P5 最低价50%+剔前20%涨幅": cheap_nomom(0.50),
    }

    print(f"{'规则':<26} {'入场日':>5} {'子集中位':>9} {'超额中位':>9} "
          f"{'超额均值':>9} {'胜率':>7} {'只数':>5} {'最差单票':>9} {'跌>30%':>7}")
    print("-" * 96)
    res = []
    for name, fn in rules.items():
        r = evaluate(name, fn, data)
        if not r:
            print(f"{name:<26} 无数据")
            continue
        res.append(r)
        print(f"{r['name']:<26} {r['n']:>5} {r['med_pct']:>+8.2f}% "
              f"{r['exc_med']:>+8.2f}pct {r['exc_mean']:>+8.2f} "
              f"{r['beat']:>6.1f}% {r['size']:>5} {r['worst_med']:>+8.2f}% "
              f"{r['loser30']:>6.1f}%")

    print()
    valid = [x for x in res if not x["name"].startswith("P0")]
    valid.sort(key=lambda x: -x["exc_med"])
    print("排序（超额中位）：")
    for r in valid:
        print(f"  {r['name']:<26} {r['exc_med']:+.2f}pct  胜率 {r['beat']:.1f}%  "
              f"{r['size']} 只")

    # 分三段
    k = len(data) // 3
    blocks = [data[:k], data[k:2 * k], data[2 * k:]]
    print("\n=== 分三段超额（检验是否时段集中）===")
    print(f"{'规则':<26} {'段1':>9} {'段2':>9} {'段3':>9}")
    for name, fn in rules.items():
        vals = []
        for b in blocks:
            r = evaluate(name, fn, b)
            vals.append(f"{r['exc_med']:>+8.2f}" if r else "     NA ")
        print(f"{name:<26} " + " ".join(vals))

    out = os.path.join(HERE, "pool_nolookahead_backtest.json")
    json.dump({"n_days": len(data), "results": res},
              open(out, "w"), ensure_ascii=False, indent=1)
    print(f"\n写入 {out}")


if __name__ == "__main__":
    main()
