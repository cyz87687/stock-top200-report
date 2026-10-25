#!/usr/bin/env python3
"""证伪检验：25 亿下限下的超额，是否只是「市值下限本身」带来的 beta，而非 SIZE 选股？

## 为什么必须做这道检验
老大最终口径 = min_amount 1亿 + min_mktcap 25亿 + SIZE 排序取 500 只。
回测算出「25 亿下限 + 市值最小18%」超额 +7.43pct。
但存在一个替代解释：**25 亿下限本身就在剔除微盘，而微盘当期表现差**，
于是「剩余股票的均值」本身就跑赢全宇宙，与 SIZE 选股无关（纯 beta，不是 alpha）。

必须把这层剥掉，否则会把 beta 当 alpha 邀功——这是量化里最常见的自欺。

## 检验设计
在**同一个 25 亿下限的候选池内**（候选已固定，排除下限带来的影响），比较三种选法：
  C0 池内全体（= 基准，超额必然为 0）
  C1 按总市值升序取前 18%（SIZE 因子）
  C2 随机取 18%（随机基准，应接近 0）
  C3 按总市值**降序**取前 18%（SIZE 反向，若 SIZE 有效应为负）
若 C1 >> C2 ≈ 0 >> C3，则 SIZE 在池内确实有区分度（真 alpha）；
若 C1 ≈ C2，则超额全来自下限 beta，SIZE 无用。

另测：C1 相对 C0 的胜率与三段稳健性。
"""
import json
import os
import random
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import factor_eval as fe

HOP = 20
FLOOR = 25e8


def med(a):
    if not a:
        return None
    a = sorted(a)
    n = len(a)
    return a[n // 2] if n % 2 else (a[n // 2 - 1] + a[n // 2]) / 2.0


def load_shares():
    rows = json.load(open("/tmp/alla_snapshot.json"))
    tot = {}
    for r in rows:
        try:
            p = float(r.get("trade") or 0)
            mk = float(r.get("mktcap") or 0) * 1e4
        except Exception:
            continue
        if p > 0 and mk > 0:
            tot[r["code"]] = mk / p
    return tot


def build():
    h = json.load(open(os.path.join(HERE, "factor_history.json")))
    snaps, _ = fe._drop_stale_price_snapshots(h["snapshots"])
    tdays, px = fe.px_index(h)
    tot_sh = load_shares()
    tidx = {d: i for i, d in enumerate(tdays)}
    codes = [c for c in px if c in tot_sh]
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
            m = px.get(c) or {}
            p0, p1 = m.get(tdays[i0]), m.get(tdays[i1])
            if not p0 or not p1 or p0 <= 0:
                continue
            rows.append({"c": c, "fwd": p1 / p0 - 1.0, "mv": tot_sh[c] * p0})
        rows = [x for x in rows if x["mv"] >= FLOOR]
        if len(rows) < 100:
            continue
        data.append({"date": d0, "rows": rows})
    return data


def run(data, fn):
    ex, sizes = [], []
    for rec in data:
        base = rec["rows"]
        bm = med([x["fwd"] for x in base])
        sub = fn(base)
        if len(sub) < 20:
            continue
        sm = med([x["fwd"] for x in sub])
        if sm is None or bm is None:
            continue
        ex.append(sm - bm)
        sizes.append(len(sub))
    return ex, sizes


def report(name, ex, sizes):
    if len(ex) < 10:
        print(f"{name:<34} 样本不足")
        return None
    k = len(ex) // 3
    segs = [med(ex[:k]) * 100, med(ex[k:2 * k]) * 100, med(ex[2 * k:]) * 100]
    wr = 100.0 * sum(1 for x in ex if x > 0) / len(ex)
    print(f"{name:<34}{med(ex)*100:>+9.2f}pct{100.0*wr:>7.1f}%{int(fe._mean(sizes)):>6}"
          f"   {segs[0]:>+6.2f} / {segs[1]:>+6.2f} / {segs[2]:>+6.2f}")
    return {"name": name, "exc": round(med(ex) * 100, 2), "win": round(wr, 1),
            "size": int(fe._mean(sizes)), "segs": [round(s, 2) for s in segs]}


def main():
    data = build()
    print(f"有效截面 {len(data)}  持有 {HOP} 日")
    print(f"候选池固定为「总市值 ≥ {FLOOR/1e8:.0f} 亿」（与线上 min_mktcap 一致）")
    print("超额 = 子集中位 − **同一候选池内**全体中位（下限 beta 已被完全剥离）\n")
    print(f"{'选法':<34}{'超额中位':>10}{'胜率':>8}{'只数':>6}   三段超额")
    print("-" * 96)

    random.seed(42)
    res = []

    def c0(rows):
        return rows

    def c1(rows):
        return sorted(rows, key=lambda x: x["mv"])[:max(20, int(len(rows) * 0.18))]

    def c2(rows):
        r = list(rows)
        random.shuffle(r)
        return r[:max(20, int(len(rows) * 0.18))]

    def c3(rows):
        return sorted(rows, key=lambda x: -x["mv"])[:max(20, int(len(rows) * 0.18))]

    for nm, fn in (("C0 池内全体（基准）", c0),
                   ("C1 池内市值最小18%〔SIZE〕", c1),
                   ("C2 池内随机18%〔随机基准〕", c2),
                   ("C3 池内市值最大18%〔SIZE反向〕", c3)):
        ex, sz = run(data, fn)
        r = report(nm, ex, sz)
        if r:
            res.append(r)

    # 随机基准做 20 次，看分布，确认 C1 不是运气
    print("\n=== 随机基准 20 次分布（确认 C1 显著高于随机噪声） ===")
    rnd = []
    for seed in range(20):
        random.seed(seed)
        ex, _ = run(data, c2)
        if len(ex) >= 10:
            rnd.append(med(ex) * 100)
    if rnd:
        rnd.sort()
        print(f"  随机18% 超额: 最小 {rnd[0]:+.2f} / 中位 {med(rnd):+.2f} / 最大 {rnd[-1]:+.2f} pct")
        c1e = next(r["exc"] for r in res if r["name"].startswith("C1"))
        better = sum(1 for x in rnd if x >= c1e)
        print(f"  C1({c1e:+.2f}) 优于 {20-better}/20 次随机抽样"
              f"（随机达到或超过 C1 的次数: {better}）")

    print("\n=== 结论 ===")
    c1e = next(r["exc"] for r in res if r["name"].startswith("C1"))
    c2e = next(r["exc"] for r in res if r["name"].startswith("C2"))
    c3e = next(r["exc"] for r in res if r["name"].startswith("C3"))
    print(f"  SIZE(最小18%) {c1e:+.2f}pct  vs  随机 {c2e:+.2f}pct  vs  反向 {c3e:+.2f}pct")
    if c1e > c2e + 1.0 and c3e < c2e:
        print("  ✅ SIZE 在池内确实有区分度（真 alpha，非下限 beta）")
    elif abs(c1e - c2e) <= 1.0:
        print("  ⚠️ SIZE 与随机无显著差异 → 超额可能来自下限 beta，须警惕")
    else:
        print("  ⚠️ 结果异常，需人工复核")

    out = os.path.join(HERE, "pool_size_falsify.json")
    json.dump({"n_days": len(data), "floor_yi": FLOOR / 1e8,
               "results": res, "random_dist": [round(x, 2) for x in rnd]},
              open(out, "w"), ensure_ascii=False, indent=1)
    print(f"\n写入 {out}")


if __name__ == "__main__":
    main()
