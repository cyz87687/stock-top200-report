#!/usr/bin/env python3
"""P0-1 终局：全A母体选池规则回测（老大决定：换母体，放弃成交额榜）。

## 背景
前序全部实验证明「成交额榜」这个母体本身选不出正 alpha：
  成交额榜全取      vs 全池 -3.99pct
  最优低价倾斜      vs 全池 -2.13pct（仍是负的）
故必须放弃成交额榜，改为在**全A**范围内按规则选池。

## 数据边界（必须坦白）
`factor_history.prices` 只有 1012 只（历史上曾入成交额榜的并集），
**不包含全A 约 5500 只的完整历史价格**。因此本脚本无法直接回测"全A选股"的
绝对收益，只能做以下两件有价值的事：

  (A) 在 1012 只宇宙内，用不同规则选 500 只，比较「相对宇宙中位」的超额
      —— 这等价于「在全A内选股」的相对排序效果，可迁移
  (B) 检验规则对「股票池进入门槛」的敏感性：若某规则在 1012 宇宙内选出的
      子集显著优于其他规则，则该排序逻辑可外推到全A（排序函数本身不含
      宇宙规模依赖）

## 结论用途
选出排序规则后，线上 fetch 改为：抓全A 5576 只 → 按该规则选 pool_size 只。
线上首次运行后，未来 20 个交易日即可积累全A 真实样本，届时再做绝对收益验证。
"""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import factor_eval as fe

HOP = 20
POOL = 500


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
    # 每个 code 在每个快照日的 fund/theme 分（用于质量门槛；无前视：用当日快照值）
    score_map = {}
    for s in snaps:
        d = s["date"]
        for r in s["rows"]:
            score_map.setdefault(d, {})[r["code"]] = {
                "fund": r.get("score_fund"), "theme": r.get("score_theme"),
                "sector": r.get("score_sector")}
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
            sc = (score_map.get(d0) or {}).get(c) or {}
            rows.append({"c": c, "fwd": fwd, "price": pr,
                         "r20": ret(px, c, tdays, i0 - 20, i0),
                         "r60": ret(px, c, tdays, i0 - 60, i0),
                         "fund": sc.get("fund"), "theme": sc.get("theme")})
        if len(rows) < 300:
            continue
        data.append({"date": d0, "rows": rows,
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
    print(f"有效截面 {len(data)}  持有 {HOP} 日  宇宙 1012 只（历史并集）")
    print("超额 = 子集中位收益 − 宇宙中位收益（宇宙内相对排序效果）\n")

    def cheap_pct(rows, rec, frac):
        v = sorted(rows, key=lambda x: x["price"])
        return v[:max(30, int(len(v) * frac))]

    def fund_then_cheap(rows, rec, q, n):
        fv = [x["fund"] for x in rows if x.get("fund") is not None]
        if len(fv) < 30:
            return []
        thr = sorted(fv)[min(len(fv) - 1, int(len(fv) * q))]
        v = [x for x in rows if x.get("fund") is not None and x["fund"] >= thr]
        v.sort(key=lambda x: x["price"])
        return v[:n]

    def rev_nofund(rows, rec, n):
        """反转：20日跌幅最大，且剔除基本面垫底30%。"""
        fv = [x["fund"] for x in rows if x.get("fund") is not None]
        if len(fv) < 30:
            return []
        thr = sorted(fv)[int(len(fv) * 0.30)]
        v = [x for x in rows if x.get("r20") is not None
             and (x.get("fund") is None or x["fund"] > thr)]
        v.sort(key=lambda x: x["r20"])
        return v[:n]

    def combo(rows, rec, n):
        """综合：fund 质量门槛 p60 + 价格倾斜 + 20日跌幅过滤。"""
        fv = [x["fund"] for x in rows if x.get("fund") is not None]
        if len(fv) < 30:
            return []
        thr = sorted(fv)[min(len(fv) - 1, int(len(fv) * 0.60))]
        v = [x for x in rows if x.get("fund") is not None and x["fund"] >= thr]
        r20s = [x["r20"] for x in v if x["r20"] is not None]
        if len(r20s) >= 30:
            t20 = sorted(r20s)[int(len(r20s) * 0.70)]
            v = [x for x in v if x["r20"] is None or x["r20"] <= t20]
        v.sort(key=lambda x: x["price"])
        return v[:n]

    rules = [
        ("Q0 宇宙全体（基准）", lambda r, rec: r),
        ("Q1 成交额榜模拟（现状≈193只）", lambda r, rec: r[:193]),
        ("Q2 最便宜 500", lambda r, rec: cheap_pct(r, rec, 1.0)[:500]),
        ("Q3 最便宜 9%（≈500/5576）", lambda r, rec: cheap_pct(r, rec, 0.09)),
        ("Q4 fund≥p60 + 最便宜500", lambda r, rec: fund_then_cheap(r, rec, 0.60, 500)),
        ("Q5 fund≥p70 + 最便宜500", lambda r, rec: fund_then_cheap(r, rec, 0.70, 500)),
        ("Q6 fund≥p60 + 最便宜150", lambda r, rec: fund_then_cheap(r, rec, 0.60, 150)),
        ("Q7 20日超跌+剔fund垫底30%", lambda r, rec: rev_nofund(r, rec, 500)),
        ("Q8 fund≥p60+超跌70%+低价500", lambda r, rec: combo(r, rec, 500)),
    ]

    print(f"{'规则':<32} {'入场日':>5} {'子集中位':>9} {'超额中位':>9} "
          f"{'超额均值':>9} {'胜率':>7} {'只数':>5} {'最差单票':>9}")
    print("-" * 100)
    res = []
    for name, fn in rules:
        r = ev(name, fn, data)
        if not r:
            print(f"{name:<32} 无数据")
            continue
        res.append(r)
        print(f"{r['name']:<32} {r['n']:>5} {r['med']:>+8.2f}% {r['exc']:>+8.2f}pct "
              f"{r['exc_mean']:>+8.2f} {r['beat']:>6.1f}% {r['size']:>5} {r['worst']:>+8.2f}%")

    k = len(data) // 3
    print("\n=== 三段稳健性 ===")
    print(f"{'规则':<32} {'段1':>9} {'段2':>9} {'段3':>9}")
    for name, fn in rules:
        vals = []
        for b in (data[:k], data[k:2 * k], data[2 * k:]):
            r = ev(name, fn, b)
            vals.append(f"{r['exc']:>+8.2f}" if r else "     NA ")
        print(f"{name:<32} " + " ".join(vals))

    print()
    valid = [x for x in res if not x["name"].startswith("Q0")]
    valid.sort(key=lambda x: -x["exc"])
    print("排序：")
    for r in valid:
        print(f"  {r['name']:<34} {r['exc']:+.2f}pct  胜率 {r['beat']:.1f}%  {r['size']} 只")

    out = os.path.join(HERE, "pool_alla_backtest.json")
    json.dump({"n_days": len(data), "results": res},
              open(out, "w"), ensure_ascii=False, indent=1)
    print(f"\n写入 {out}")


if __name__ == "__main__":
    main()
