#!/usr/bin/env python3
"""P0-1 第三阶段：选池规则的稳健性检验（防单个时期/幸存偏差造成的假结论）。

## 第二阶段发现
在 1012 只宇宙内换排序键，超额从 -5.08pct（成交额型）翻正：
  U6 价格最低500    +2.87pct  跑赢 87.9%
  U5 跌幅居中段     +1.02pct  跑赢 69.0%
  U3 60日跌幅最大   +1.00pct  跑赢 70.8%

## 必须排除的三种假象
1. **时段集中**：超额可能只来自某一段（如 9~10 月小盘反弹）。→ 分月/分段检验
2. **幸存偏差**：宇宙 = 曾入成交额 TOP500，本身是热门股集合；低价股在该集合内
   可能只是「高价股跌下来的」，需看 U6 选中股的实际价格分布与前身。
3. **极端值驱动**：中位数为稳健统计，但仍需看胜率、均值/中位缺口。

## 本脚本
a) 分三段（前/中/后各约 19 个截面）看各规则超额是否稳定
b) U6 选中股的绝对价格分布（判断是否沦为「仙股集合」）
c) 规则两两相关性（避免选到同一批股票）
d) 组合规则：U6 ∩ U5（低价 + 中段跌幅）
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
                        "price": (px.get(c) or {}).get(tdays[i0])})
        if len(uni) < 300:
            continue
        data.append({"date": d0, "uni": uni,
                     "uni_med": med([x["fwd"] for x in uni]),
                     "pool": {r["code"] for r in s["rows"]}})
    return data, tdays


def pick_u6(r):
    v = [x for x in r["uni"] if x.get("price")]
    v.sort(key=lambda x: x["price"])
    return v[:TOPN]


def pick_u5(r):
    v = [x for x in r["uni"] if x["r20"] is not None]
    v.sort(key=lambda x: x["r20"])
    return v[int(len(v) * 0.30):int(len(v) * 0.70)][:TOPN]


def pick_u3(r):
    v = [x for x in r["uni"] if x["r60"] is not None]
    v.sort(key=lambda x: x["r60"])
    return v[:TOPN]


def pick_u1(r):
    return [x for x in r["uni"] if x["c"] in r["pool"]]


def pick_u6u5(r):
    """低价 ∩ 中段跌幅：先在低价前 60% 里取跌幅居中段。"""
    v = [x for x in r["uni"] if x.get("price") and x["r20"] is not None]
    v.sort(key=lambda x: x["price"])
    cand = v[:int(len(v) * 0.60)]
    cand.sort(key=lambda x: x["r20"])
    return cand[int(len(cand) * 0.25):int(len(cand) * 0.75)][:TOPN]


def eval_block(name, fn, recs):
    subs, exs, wins = [], [], 0
    for r in recs:
        sub = fn(r)
        if len(sub) < 50:
            continue
        sm = med([x["fwd"] for x in sub])
        if sm is None:
            continue
        subs.append(sm)
        exs.append(sm - r["uni_med"])
        if sm > r["uni_med"]:
            wins += 1
    if not exs:
        return None
    return {"name": name, "n": len(exs),
            "med_pct": round(med(subs) * 100, 2),
            "exc_med": round(med(exs) * 100, 2),
            "exc_mean": round(fe._mean(exs) * 100, 2),
            "beat": round(100.0 * wins / len(exs), 1)}


def main():
    data, tdays = build()
    n = len(data)
    print(f"有效截面 {n}（{data[0]['date']} ~ {data[-1]['date']}）\n")

    rules = {"U1 成交额型(现状)": pick_u1, "U3 60日跌幅最大": pick_u3,
             "U5 跌幅居中段": pick_u5, "U6 价格最低": pick_u6,
             "U6∩U5 低价+中段跌": pick_u6u5}

    # ---- a) 分三段稳定性 ----
    k = n // 3
    blocks = [("第1段 " + data[0]["date"] + "~" + data[k - 1]["date"], data[:k]),
              ("第2段 " + data[k]["date"] + "~" + data[2 * k - 1]["date"], data[k:2 * k]),
              ("第3段 " + data[2 * k]["date"] + "~" + data[-1]["date"], data[2 * k:])]

    print("=== a) 分三段超额（pct）稳定性 ===")
    hdr = f"{'规则':<20} " + "".join(f"{b[0][:3]+b[0][4:9]:>16}" for b in blocks)
    print(hdr + f"{'全样本':>12}")
    print("-" * (len(hdr) + 12))
    for name, fn in rules.items():
        line = f"{name:<20} "
        for bname, recs in blocks:
            r = eval_block(name, fn, recs)
            line += f"{r['exc_med']:>+15.2f} " if r else f"{'NA':>15} "
        full = eval_block(name, fn, data)
        line += f"{full['exc_med']:>+11.2f}"
        print(line)

    print("\n=== b) 各规则胜率（跑赢宇宙中位的入场日占比）===")
    for name, fn in rules.items():
        parts = []
        for bname, recs in blocks:
            r = eval_block(name, fn, recs)
            parts.append(f"{r['beat']:>5.1f}%" if r else "  NA ")
        full = eval_block(name, fn, data)
        print(f"  {name:<20} 段1 {parts[0]} 段2 {parts[1]} 段3 {parts[2]}  全样本 {full['beat']:>5.1f}%")

    # ---- c) U6 选中股价格分布 ----
    print("\n=== c) U6（价格最低500）选中股的价格分布 ===")
    last = data[-1]
    sel = pick_u6(last)
    prices = sorted([x["price"] for x in sel])
    print(f"  截面 {last['date']}  选出 {len(sel)} 只")
    for q, lbl in [(0, "最低"), (0.10, "p10"), (0.25, "p25"),
                   (0.50, "中位"), (0.75, "p75"), (0.90, "p90"), (0.99, "p99")]:
        idx = min(len(prices) - 1, int(len(prices) * q))
        print(f"    {lbl:>4}: {prices[idx]:.2f} 元")
    print(f"    >50元只数: {sum(1 for p in prices if p > 50)} / {len(prices)}")
    print(f"    <5元只数: {sum(1 for p in prices if p < 5)} / {len(prices)}")

    # ---- d) 规则重叠度 ----
    print("\n=== d) 规则选中重合度（2026-10-23 截面）===")
    sets = {name: {x["c"] for x in fn(last)} for name, fn in rules.items()}
    names = list(sets.keys())
    print(f"{'':<20} " + "".join(f"{nm[:6]:>8}" for nm in names))
    for a in names:
        line = f"{a:<20} "
        for b in names:
            inter = len(sets[a] & sets[b])
            denom = max(1, min(len(sets[a]), len(sets[b])))
            line += f"{100.0*inter/denom:>7.0f}%"
        print(line)

    out = os.path.join(HERE, "pool_universe_robust.json")
    json.dump({"n_days": n,
               "blocks": {b[0]: {nm: eval_block(nm, fn, b[1]) for nm, fn in rules.items()}
                          for b in blocks},
               "full": {nm: eval_block(nm, fn, data) for nm, fn in rules.items()}},
              open(out, "w"), ensure_ascii=False, indent=1)
    print(f"\n写入 {out}")


if __name__ == "__main__":
    main()
