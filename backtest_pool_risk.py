#!/usr/bin/env python3
"""P0-1 第四阶段：低价/反转选池方案的风险解剖（决定能否直接落地）。

第三阶段发现 U6∩U5（低价∩中段跌幅）三段全正、胜率 87.9%、超额 +3.57pct。
但 A 股低价股有明确的高危属性，若不解剖就落地属于赌博：
  1. 面值退市：连续 20 日收盘 <1 元即终止上市 → 低价策略天然含"退市彩票"
  2. ST 集中：低价股里 ST/*ST 占比远高于全市场
  3. 极端回撤：低价股涨跌弹性大，中位数好不代表尾部安全
  4. 流动性陷阱：低价股常伴低成交额，实盘冲击成本远高于 20bp

本脚本量化这四项，判断：
  · 该方案能否直接替换现池（结论很可能是「不能」）
  · 若不能，应如何加风控（价格地板、剔除 ST、成交额地板）
"""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import factor_eval as fe

HOP = 20
TOPN = 500


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
        uni = []
        for c in codes:
            fwd = ret(px, c, tdays, i0, i1)
            if fwd is None:
                continue
            uni.append({"c": c, "fwd": fwd,
                        "r20": ret(px, c, tdays, i0 - 20, i0),
                        "r60": ret(px, c, tdays, i0 - 60, i0),
                        "price": (px.get(c) or {}).get(tdays[i0]),
                        "price_next": (px.get(c) or {}).get(tdays[i1])})
        if len(uni) < 300:
            continue
        data.append({"date": d0, "uni": uni,
                     "uni_med": med([x["fwd"] for x in uni]),
                     "pool": {r["code"] for r in s["rows"]}})
    return data, tdays


def pick_u6u5(r, price_floor=0.0, exclude_pool=False):
    """低价∩中段跌幅；可选价格地板（元）与排除已在池内。"""
    v = [x for x in r["uni"] if x.get("price") and x["r20"] is not None]
    if price_floor > 0:
        v = [x for x in v if x["price"] >= price_floor]
    if exclude_pool:
        v = [x for x in v if x["c"] not in r["pool"]]
    if len(v) < 200:
        return []
    v.sort(key=lambda x: x["price"])
    cand = v[:int(len(v) * 0.60)]
    cand.sort(key=lambda x: x["r20"])
    return cand[int(len(cand) * 0.25):int(len(cand) * 0.75)][:TOPN]


def stats(name, fn, data):
    meds, exs, wins = [], [], 0
    worst = []      # 每截面最差单票
    losers = []     # 每截面跌幅>30% 占比
    delist = []     # 每截面跌破 1 元的只数占比
    for r in data:
        sub = fn(r)
        if len(sub) < 50:
            continue
        sm = med([x["fwd"] for x in sub])
        if sm is None:
            continue
        meds.append(sm)
        exs.append(sm - r["uni_med"])
        if sm > r["uni_med"]:
            wins += 1
        worst.append(min(x["fwd"] for x in sub))
        losers.append(100.0 * sum(1 for x in sub if x["fwd"] < -0.30) / len(sub))
        n_face = sum(1 for x in sub
                     if x.get("price_next") and x["price_next"] < 1.0)
        delist.append(100.0 * n_face / len(sub))
    if not exs:
        return None
    return {"name": name, "n": len(exs),
            "med_pct": round(med(meds) * 100, 2),
            "exc_med": round(med(exs) * 100, 2),
            "beat": round(100.0 * wins / len(exs), 1),
            "worst_med_pct": round(med(worst) * 100, 2),
            "loser30_pct": round(med(losers), 1),
            "face_risk_pct": round(med(delist), 2)}


def main():
    data, tdays = build()
    print(f"有效截面 {len(data)}（{data[0]['date']} ~ {data[-1]['date']}）\n")

    variants = {
        "① U1 现状（成交额型池）": lambda r: [x for x in r["uni"] if x["c"] in r["pool"]],
        "② U6∩U5 无地板（裸低价）": lambda r: pick_u6u5(r),
        "③ U6∩U5 + 价格≥2元": lambda r: pick_u6u5(r, price_floor=2.0),
        "④ U6∩U5 + 价格≥3元": lambda r: pick_u6u5(r, price_floor=3.0),
        "⑤ U6∩U5 + 价格≥5元": lambda r: pick_u6u5(r, price_floor=5.0),
    }

    print("=== 风险解剖 ===")
    print(f"{'方案':<26} {'超额中位':>9} {'胜率':>7} {'中位收益':>9} "
          f"{'最差单票中位':>12} {'跌>30%占比':>11} {'破面值占比':>10}")
    print("-" * 92)
    res = []
    for name, fn in variants.items():
        r = stats(name, fn, data)
        if not r:
            print(f"{name:<26} 无数据")
            continue
        res.append(r)
        print(f"{r['name']:<26} {r['exc_med']:>+8.2f} {r['beat']:>6.1f}% "
              f"{r['med_pct']:>+8.2f}% {r['worst_med_pct']:>+11.2f}% "
              f"{r['loser30_pct']:>10.1f}% {r['face_risk_pct']:>9.2f}%")

    # 价格地板的影响曲线
    print("\n=== 价格地板敏感性（找「保留超额 + 控住风险」的平衡点）===")
    print(f"{'地板(元)':>8} {'超额中位':>9} {'胜率':>7} {'破面值占比':>10} {'跌>30%占比':>11} {'选中数':>7}")
    for fl in (0, 1.5, 2.0, 2.5, 3.0, 4.0, 5.0, 8.0, 10.0):
        def f(r, _fl=fl):
            return pick_u6u5(r, price_floor=_fl)
        r = stats(f"floor={fl}", f, data)
        if not r:
            print(f"{fl:>8.1f} {'NA':>9}")
            continue
        sizes = [len(f(rec)) for rec in data]
        print(f"{fl:>8.1f} {r['exc_med']:>+8.2f} {r['beat']:>6.1f}% "
              f"{r['face_risk_pct']:>9.2f}% {r['loser30_pct']:>10.1f}% "
              f"{int(fe._mean(sizes)):>7}")

    out = os.path.join(HERE, "pool_universe_risk.json")
    json.dump(res, open(out, "w"), ensure_ascii=False, indent=1)
    print(f"\n写入 {out}")


if __name__ == "__main__":
    main()
