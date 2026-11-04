#!/usr/bin/env python3
"""P0-1 第二阶段：真正的「选池规则」回测（不是池内过滤）。

## 第一阶段结论（backtest_pool_filter.py）
池内 9 种过滤规则全部无法把超额转正，最好 A7（追高∩热度双过滤）仅 -3.92pct
（基线 -5.08pct），跑赢占比 3.4%→10.5% 依然极低。
→ 证明：**从负 alpha 母体里抽子集，抽不出正 alpha。问题在母体（母池），不在筛选。**

## 本脚本：改母体
候选宇宙 = `factor_history.prices` 的 1012 只并集（= 历史上曾进入成交额 TOP500 的
股票全集，含未入池日的价格），比单截面池内 193 只大 5.2 倍。
在每个截面 t 按不同规则从这 1012 只中选 N 只，比较其持有 20 日的**中位收益**
相对宇宙中位的超额。

## 规则设计（只用价格可得信息，避免前视）
- U0 宇宙全体（基准，超额恒为 0）
- U1 当前池子（成交额型，等价于现状的近似：用当时快照 rows 的 193 只）
- U2 20 日跌幅最大 N 只（反转选池）
- U3 60 日跌幅最大 N 只
- U4 20 日跌幅最大但剔除最极端 10%（防接飞刀）
- U5 20 日跌幅居中段（30%~70% 分位，避免极端）
- U6 价格最低 N 只（小市值代理）
- U7 20 日跌幅前 40% 与 60 日跌幅前 40% 交集（多周期反转共振）
- U8 反转 + 剔除近期暴跌 >25%（回避基本面爆雷）

⚠️ 已知局限（必须披露）：1012 只并集本身即「曾入成交额 TOP500」的偏向集合，
非真全A（约 5000 只）。故本实验测的是「在同一偏向宇宙内换排序键」的效果，
不等于真全A 选池的绝对水平；但相对排序的结论可迁移。
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


def main():
    snaps, tdays, px = load()
    tidx = {d: i for i, d in enumerate(tdays)}
    codes = list(px.keys())

    # 只保留有足够历史的截面
    recs = []
    for s in snaps:
        d0 = s["date"]
        if d0 not in tidx:
            continue
        i0 = tidx[d0]
        if i0 + HOP >= len(tdays):
            continue
        if i0 < 60:          # 需要 60 日回看
            continue
        recs.append((s, i0))

    print(f"可用截面: {len(recs)}  宇宙: {len(codes)} 只  持有: {HOP} 日  选取: TOP{TOPN}\n")

    # 预计算每个截面的宇宙级基础数据
    data = []
    for s, i0 in recs:
        i1 = i0 + HOP
        uni = []      # (code, fwd, r20, r60, price)
        for c in codes:
            fwd = ret(px, c, tdays, i0, i1)
            if fwd is None:
                continue
            r20 = ret(px, c, tdays, i0 - 20, i0)
            r60 = ret(px, c, tdays, i0 - 60, i0)
            m = px.get(c) or {}
            pr = m.get(tdays[i0])
            uni.append({"c": c, "fwd": fwd, "r20": r20, "r60": r60, "price": pr})
        if len(uni) < 300:
            continue
        uni_med = med([x["fwd"] for x in uni])
        pool_codes = {r["code"] for r in s["rows"]}
        data.append({"date": d0, "uni": uni, "uni_med": uni_med,
                     "pool": pool_codes})

    print(f"有效截面（宇宙≥300只且有完整回看）: {len(data)}\n")

    def pct_thr(vals, q):
        a = sorted(vals)
        return a[min(len(a) - 1, int(len(a) * q))]

    def evaluate(name, pick_fn):
        sub_meds, exs, wins, sizes = [], [], 0, []
        for rec in data:
            sub = pick_fn(rec)
            if not sub or len(sub) < 50:
                continue
            sm = med([x["fwd"] for x in sub])
            if sm is None:
                continue
            sub_meds.append(sm)
            exs.append(sm - rec["uni_med"])
            sizes.append(len(sub))
            if sm > rec["uni_med"]:
                wins += 1
        if not exs:
            return None
        return {"name": name, "n_days": len(exs),
                "sub_med_pct": round(med(sub_meds) * 100, 2),
                "excess_med_pct": round(med(exs) * 100, 2),
                "excess_mean_pct": round(fe._mean(exs) * 100, 2),
                "beat_ratio": round(100.0 * wins / len(exs), 1),
                "avg_size": int(fe._mean(sizes))}

    defs = {}
    defs["U0 宇宙全体（基准）"] = lambda r: r["uni"]

    def u1(r):
        return [x for x in r["uni"] if x["c"] in r["pool"]]
    defs["U1 当前池子（成交额型）"] = u1

    def _worst(key, frac=1.0, drop_extreme=0.0):
        """按 key 升序（跌幅最大在前）取前 frac*TOPN 只；drop_extreme 剔除最极端部分。"""
        def f(r):
            v = [x for x in r["uni"] if x.get(key) is not None]
            if len(v) < 200:
                return []
            v.sort(key=lambda x: x[key])
            lo = int(len(v) * drop_extreme)
            hi = int(len(v) * frac)
            return v[lo:hi][:TOPN]
        return f

    defs["U2 20日跌幅最大500"] = _worst("r20")
    defs["U3 60日跌幅最大500"] = _worst("r60")
    defs["U4 20日跌幅最大·剔最极端10%"] = _worst("r20", drop_extreme=0.10)

    def u5(r):
        v = [x for x in r["uni"] if x["r20"] is not None]
        v.sort(key=lambda x: x["r20"])
        return v[int(len(v) * 0.30):int(len(v) * 0.70)][:TOPN]
    defs["U5 20日跌幅居中段30~70%"] = u5

    def u6(r):
        v = [x for x in r["uni"] if x.get("price")]
        v.sort(key=lambda x: x["price"])
        return v[:TOPN]
    defs["U6 价格最低500（小市值代理）"] = u6

    def u7(r):
        v = [x for x in r["uni"] if x["r20"] is not None and x["r60"] is not None]
        if len(v) < 200:
            return []
        t20 = pct_thr([x["r20"] for x in v], 0.40)
        t60 = pct_thr([x["r60"] for x in v], 0.40)
        return [x for x in v if x["r20"] <= t20 and x["r60"] <= t60][:TOPN]
    defs["U7 20日∩60日双周期超跌"] = u7

    def u8(r):
        v = [x for x in r["uni"] if x["r20"] is not None]
        v.sort(key=lambda x: x["r20"])
        return [x for x in v if x["r20"] > -0.25][:TOPN]
    defs["U8 超跌但剔除暴跌>25%"] = u8

    print(f"{'规则':<30} {'入场日':>5} {'子集中位':>9} {'超额中位':>9} "
          f"{'超额均值':>9} {'跑赢占比':>8} {'平均只数':>7}")
    print("-" * 90)
    results = []
    for name, fn in defs.items():
        r = evaluate(name, fn)
        if r is None:
            print(f"{name:<30} 无有效数据")
            continue
        results.append(r)
        print(f"{r['name']:<30} {r['n_days']:>5} {r['sub_med_pct']:>8}% "
              f"{r['excess_med_pct']:>8}pct {r['excess_mean_pct']:>8}pct "
              f"{r['beat_ratio']:>7}% {r['avg_size']:>7}")

    print()
    valid = [x for x in results if not x["name"].startswith("U0")]
    if valid:
        valid.sort(key=lambda x: -x["excess_med_pct"])
        print("按「超额中位」排序：")
        for r in valid[:6]:
            flag = "✅ 正超额" if r["excess_med_pct"] > 0 else ""
            print(f"  {r['name']:<30} {r['excess_med_pct']:+.2f}pct  "
                  f"跑赢 {r['beat_ratio']:>5}%  {r['avg_size']:>4} 只 {flag}")

    out = os.path.join(HERE, "pool_universe_backtest.json")
    json.dump({"hop": HOP, "topn": TOPN, "n_days": len(data), "results": results},
              open(out, "w"), ensure_ascii=False, indent=1)
    print(f"\n写入 {out}")


if __name__ == "__main__":
    main()
