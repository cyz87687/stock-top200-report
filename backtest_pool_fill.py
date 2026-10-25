#!/usr/bin/env python3
"""P0-1 落地口径终审：池子规模 vs 价格倾斜强度的权衡。

## 为什么重做（记录一次 self-debug）
上一版本脚本的 `pick(rows, cheap_n, total=POOL)` 有逻辑缺陷：当母体规模
n < POOL 时，`total=min(POOL,n)=n`，补齐步骤会把剩下的股票全部加回来，
导致「价格倾斜 30%/60%/80%/100%」四个变体输出完全相同。已修正。

## 真实约束
线上 step2 需要打满 pool_size=500 才有足够的评分广度与板块覆盖。
但回测显示真正有效的口径是「滚动池内最便宜的 30%」≈ 93 只，**不补齐**。
两者冲突。本脚本直接测「最终池子规模」这个变量本身：
  在滚动20日母体（约 400 只）内按价格升序取 N 只，N = 60/80/100/150/200/300/全部
比较超额中位与胜率，找出「广度」与「alpha」的帕累托前沿。

## 已知局限（必须披露）
历史早期快照仅 190~195 只（v3.0 扩池前 TOP200 时代），母体最大约 400 只，
故 N=500 的口径无法在历史样本上验证——只能外推。这是真实的数据边界，
不是实现缺陷；落地时须在 README 标注「N>400 段未回测」。
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
    ordered = sorted([s for s in snaps if s["date"] in tidx],
                     key=lambda s: tidx[s["date"]])
    data = []
    for pos, s in enumerate(ordered):
        d0 = s["date"]
        i0 = tidx[d0]
        if i0 + HOP >= len(tdays) or i0 < 20:
            continue
        i1 = i0 + HOP
        cand = {}
        for r in s["rows"]:
            if r.get("price"):
                cand[r["code"]] = {"price": r["price"],
                                   "amt": r.get("amount") or r.get("turnover") or 0}
        for j in range(max(0, pos - 20), pos):
            for r in ordered[j]["rows"]:
                c = r["code"]
                if c in cand:
                    continue
                pr = (px.get(c) or {}).get(d0)
                if pr:
                    cand[c] = {"price": pr, "amt": 0}
        rows = []
        for c, v in cand.items():
            fwd = ret(px, c, tdays, i0, i1)
            if fwd is None:
                continue
            rows.append({"c": c, "fwd": fwd, **v})
        # 基线 = 当日成交额榜全取（现状口径）
        base_fwd = [ret(px, r["code"], tdays, i0, i1) for r in s["rows"]]
        base_fwd = [x for x in base_fwd if x is not None]
        if len(rows) < 100 or len(base_fwd) < 50:
            continue
        data.append({"date": d0, "rows": rows, "base_med": med(base_fwd)})
    return data


def pick_n(rows, n):
    """按价格升序取 n 只；不足则全取。"""
    byp = sorted(rows, key=lambda x: x["price"])
    return byp[:min(n, len(byp))]


def ev(name, fn, data):
    meds, exs, wins, sizes, worst = [], [], 0, [], []
    for rec in data:
        sub = fn(rec["rows"])
        if len(sub) < 20:
            continue
        sm = med([x["fwd"] for x in sub])
        if sm is None:
            continue
        meds.append(sm)
        exs.append(sm - rec["base_med"])
        sizes.append(len(sub))
        worst.append(min(x["fwd"] for x in sub))
        if sm > rec["base_med"]:
            wins += 1
    if not exs:
        return None
    return {"name": name, "n": len(exs), "med": round(med(meds) * 100, 2),
            "exc": round(med(exs) * 100, 2), "exc_mean": round(fe._mean(exs) * 100, 2),
            "beat": round(100.0 * wins / len(exs), 1),
            "size": int(fe._mean(sizes)), "worst": round(med(worst) * 100, 2)}


def main():
    data = build()
    print(f"有效截面 {len(data)}（{data[0]['date']} ~ {data[-1]['date']}）")
    print("母体 = 今日成交额榜 ∪ 滚动20日池（无前视）")
    print("基线 = 当日成交额榜全取（现状）\n")

    sizes = [60, 80, 100, 150, 200, 300, 400, 10 ** 6]
    print(f"{'最终池规模':<14} {'入场日':>5} {'池中位':>9} {'超额中位':>9} "
          f"{'超额均值':>9} {'胜率':>7} {'实际只数':>8} {'最差单票':>9}")
    print("-" * 92)
    res = []
    for n in sizes:
        lbl = f"价格取前{n}" if n < 10 ** 6 else "价格取全部"
        r = ev(lbl, lambda rows, _n=n: pick_n(rows, _n), data)
        if not r:
            print(f"{lbl:<14} 无数据")
            continue
        res.append(r)
        print(f"{lbl:<14} {r['n']:>5} {r['med']:>+8.2f}% {r['exc']:>+8.2f}pct "
              f"{r['exc_mean']:>+8.2f} {r['beat']:>6.1f}% {r['size']:>8} {r['worst']:>+8.2f}%")

    # 基线对照
    base = ev("基线(成交额榜全取)", lambda rows: rows, data)
    print(f"\n对照 {base['name']}: 超额 {base['exc']:+.2f}pct（此口径下恒为 0，"
          f"因为基线即自身）— 见 backtest_pool_rolling.py 的 R0 真实基线 -8.46%/中位")

    k = len(data) // 3
    print("\n=== 分三段超额（稳健性）===")
    print(f"{'最终池规模':<14} {'段1':>9} {'段2':>9} {'段3':>9}")
    for n in sizes:
        lbl = f"价格取前{n}" if n < 10 ** 6 else "价格取全部"
        vals = []
        for b in (data[:k], data[k:2 * k], data[2 * k:]):
            r = ev(lbl, lambda rows, _n=n: pick_n(rows, _n), b)
            vals.append(f"{r['exc']:>+8.2f}" if r else "     NA ")
        print(f"{lbl:<14} " + " ".join(vals))

    out = os.path.join(HERE, "pool_size_backtest.json")
    json.dump({"n_days": len(data), "results": res},
              open(out, "w"), ensure_ascii=False, indent=1)
    print(f"\n写入 {out}")


if __name__ == "__main__":
    main()
