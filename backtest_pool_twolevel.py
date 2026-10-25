#!/usr/bin/env python3
"""P0-1 最终落地口径：核心池 + 广度补充（兼顾 alpha 与 500 只覆盖）。

## 冲突
· 价格倾斜取前 100~150 只：超额 +1.85~+2.71pct、胜率 74~79%（好）
· 但线上 step2 需要 ~500 只才有足够板块覆盖与组合分散（要广度）
· 直接「取前 150 再补成交额到 500」会把效果稀释回 +1.01pct（前测已证）

## 本脚本测「两级池」：不再是单一池，而是
  核心池 core = 价格最便宜的 N 只（供组合层优先选股）
  广池   wide = core ∪ 成交额补充（供评分/板块/报告展示）
评估时分别看 core 与 wide 的超额，确认 core 真的更优 → 落地两级结构。

## 为什么这样设计是对的
投资者真实动作是「从池里挑几只买」。若 core 的 20 日持有表现显著优于 wide，
则组合层应从 core 选，而 wide 只承担"看全市场"的信息职能。
这样既不牺牲广度，也不稀释 alpha。
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
    ordered = sorted([s for s in snaps if s["date"] in tidx],
                     key=lambda s: tidx[s["date"]])
    codes = list(px.keys())
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
        # 真·全池基准（1012 只并集）
        allr = []
        for c in codes:
            f = ret(px, c, tdays, i0, i1)
            if f is not None:
                allr.append(f)
        base_fwd = [ret(px, r["code"], tdays, i0, i1) for r in s["rows"]]
        base_fwd = [x for x in base_fwd if x is not None]
        if len(rows) < 100 or len(base_fwd) < 50 or len(allr) < 200:
            continue
        data.append({"date": d0, "rows": rows,
                     "base_med": med(base_fwd),      # 成交额榜基准
                     "all_med": med(allr)})          # 全池基准
    return data


def two_level(rows, core_n, pool=POOL):
    byp = sorted(rows, key=lambda x: x["price"])
    core = byp[:min(core_n, len(byp))]
    cs = {x["c"] for x in core}
    rest = [x for x in rows if x["c"] not in cs]
    rest.sort(key=lambda x: -x["amt"])
    wide = core + rest[:max(0, pool - len(core))]
    return core, wide


def ev(fn, data, key):
    meds, exs, wins, sizes, worst = [], [], 0, [], []
    for rec in data:
        sub = fn(rec["rows"])
        if isinstance(sub, tuple):
            sub = sub[key]
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
    if len(exs) < 10:
        return None
    # 相对全池的超额（关键：这是绝对意义的 alpha）
    exs_all = [med([x["fwd"] for x in (fn(rec["rows"])[key] if isinstance(fn(rec["rows"]), tuple) else fn(rec["rows"]))])
               - rec["all_med"] for rec in data
               if len((fn(rec["rows"])[key] if isinstance(fn(rec["rows"]), tuple) else fn(rec["rows"]))) >= 20]
    return {"med": round(med(meds) * 100, 2),
            "exc_vs_amt": round(med(exs) * 100, 2),
            "exc_vs_all": round(med(exs_all) * 100, 2) if exs_all else None,
            "beat": round(100.0 * wins / len(exs), 1),
            "size": int(fe._mean(sizes)), "worst": round(med(worst) * 100, 2),
            "n": len(exs)}


def main():
    data = build()
    print(f"有效截面 {len(data)}  持有 {HOP} 日  无前视\n")
    print("说明：「vs成交额榜」= 相对现状选池的超额；「vs全池」= 相对 1012 只并集的超额")
    print("      后者才是绝对意义上的 alpha（>0 才代表真正跑赢市场）\n")

    configs = [(60, "core60"), (100, "core100"), (150, "core150"),
               (200, "core200"), (250, "core250")]
    print(f"{'口径':<10} {'层级':<6} {'只数':>5} {'中位':>9} {'vs成交额榜':>11} "
          f"{'vs全池':>9} {'胜率':>7} {'最差单票':>9}")
    print("-" * 82)
    res = {}
    for cn, lbl in configs:
        for key, klbl in ((0, "核心"), (1, "广池")):
            r = ev(lambda rows, _cn=cn: two_level(rows, _cn), data, key)
            if not r:
                continue
            res[f"{lbl}|{klbl}"] = r
            print(f"{lbl:<10} {klbl:<6} {r['size']:>5} {r['med']:>+8.2f}% "
                  f"{r['exc_vs_amt']:>+10.2f} {r['exc_vs_all']:>+8.2f} "
                  f"{r['beat']:>6.1f}% {r['worst']:>+8.2f}%")

    # 三段
    k = len(data) // 3
    blocks = (data[:k], data[k:2 * k], data[2 * k:])
    print("\n=== 三段稳健性（vs全池 超额）===")
    print(f"{'口径':<10} {'层级':<6} {'段1':>9} {'段2':>9} {'段3':>9}")
    for cn, lbl in configs:
        for key, klbl in ((0, "核心"), (1, "广池")):
            vals = []
            for b in blocks:
                r = ev(lambda rows, _cn=cn: two_level(rows, _cn), b, key)
                vals.append(f"{r['exc_vs_all']:>+8.2f}" if r and r["exc_vs_all"] is not None else "     NA ")
            print(f"{lbl:<10} {klbl:<6} " + " ".join(vals))

    out = os.path.join(HERE, "pool_two_level_backtest.json")
    json.dump({"n_days": len(data), "results": res},
              open(out, "w"), ensure_ascii=False, indent=1)
    print(f"\n写入 {out}")


if __name__ == "__main__":
    main()
