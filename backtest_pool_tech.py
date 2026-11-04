#!/usr/bin/env python3
"""P0-1 修正：在「保留科技属性」约束下重测选池规则。

## 问题
v3.7 首版按「全A 价格最便宜 18%」选池，回测超额 +4.60pct，但实跑结果池内
科技相关（电子/计算机/通信/电力设备/国防军工/机械设备）仅 63/500 = 12.6%，
TOP10 为中国建筑/TCL科技/晶科科技/民生银行/包钢股份/万科Ａ —— 与老大
「保留科技属性，只做温和反转」的明确裁决冲突。

## 本脚本要回答的唯一问题
「便宜」这个 alpha 到底是**全行业普遍成立**，还是**只在某些行业成立**？
如果是普遍的 → 可以在科技行业内做同样的便宜筛选，两者兼得。
如果只在非科技行业成立 → 必须诚实呈报「便宜与科技不可兼得」，请老大裁决。

## 规则（都用全A可算量，无前视）
  A0 全宇宙基准
  A1 现状：全A最便宜18%（v3.7 首版口径）
  B1 科技域全体
  B2 科技域 ∩ 便宜（科技域内价格分位截断）
  B3 全A便宜18% ∩ 科技（交集）
  C1 全A便宜18% + 行业权重上限（单行业≤8%，防银行/地产霸屏）
  C2 全A便宜18% + 科技配额40%（先填科技便宜股，不足用全A便宜补）
  D1 科技域 ∩ 60日温和超跌（r60 后40%，非极端）
  D2 科技域 ∩ 便宜 ∩ 温和超跌

## 指标
超额 = 子集中位涨幅 − 宇宙中位涨幅（20日持有，t+1 可交易口径）
"""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import factor_eval as fe

HOP = 20
TECH_L1 = {"电子", "计算机", "通信", "电力设备", "国防军工", "机械设备"}


def load():
    h = json.load(open(os.path.join(HERE, "factor_history.json")))
    snaps, _ = fe._drop_stale_price_snapshots(h["snapshots"])
    tdays, px = fe.px_index(h)
    sw = json.load(open(os.path.join(HERE, "sector", "sw_map.json")))
    return snaps, tdays, px, sw["l1"], sw["l2"]


def med(a):
    if not a:
        return None
    a = sorted(a)
    n = len(a)
    return a[n // 2] if n % 2 else (a[n // 2 - 1] + a[n // 2]) / 2.0


def ret(px, code, tdays, i0, i1):
    if i0 < 0 or i1 >= len(tdays):
        return None
    m = px.get(code) or {}
    p0, p1 = m.get(tdays[i0]), m.get(tdays[i1])
    if not p0 or not p1 or p0 <= 0:
        return None
    return p1 / p0 - 1.0


def build():
    snaps, tdays, px, l1, l2 = load()
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
            c6 = c[2:] if len(c) > 6 else c
            rows.append({"c": c, "fwd": fwd, "price": pr,
                         "r20": ret(px, c, tdays, i0 - 20, i0),
                         "r60": ret(px, c, tdays, i0 - 60, i0),
                         "l1": l1.get(c6, "未知"), "l2": l2.get(c6, "未知")})
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


def build_rules():
    def tech(r):
        return [x for x in r if x["l1"] in TECH_L1]

    def cheap(rows, frac):
        v = sorted(rows, key=lambda x: x["price"])
        return v[:max(30, int(len(v) * frac))]

    def A1(r, rec):
        return cheap(r, 0.18)

    def B1(r, rec):
        return tech(r)

    def B2(r, rec):
        return cheap(tech(r), 0.18)

    def B3(r, rec):
        tc = {x["c"] for x in cheap(r, 0.18)}
        return [x for x in r if x["c"] in tc and x["l1"] in TECH_L1]

    def C1(r, rec):
        base = cheap(r, 0.18)
        cap = max(20, int(len(base) * 0.08))
        from collections import defaultdict
        cnt = defaultdict(int)
        out = []
        for x in base:
            if cnt[x["l1"]] >= cap:
                continue
            cnt[x["l1"]] += 1
            out.append(x)
        return out

    def C2(r, rec):
        want = int(500 * 0.40)
        tq = cheap([x for x in tech(r) if x["price"] <= med([y["price"] for y in r])], 0.5)[:want]
        tc = {x["c"] for x in tq}
        rest = [x for x in cheap(r, 0.25) if x["c"] not in tc]
        return tq + rest

    def D1(r, rec):
        v = [x for x in tech(r) if x.get("r60") is not None]
        v.sort(key=lambda x: x["r60"])
        # 温和：取跌幅 40%~80% 区间（避开极端崩盘股）
        lo, hi = int(len(v) * 0.40), int(len(v) * 0.80)
        return v[lo:hi]

    def D2(r, rec):
        v = D1(r, rec)
        return cheap(v, 0.35) if len(v) > 40 else v

    return [
        ("A0 全宇宙（基准）", lambda r, rec: r),
        ("A1 全A最便宜18%〔现状〕", A1),
        ("B1 科技域全体", B1),
        ("B2 科技域∩便宜18%", B2),
        ("B3 全A便宜18%∩科技", B3),
        ("C1 便宜18%+行业≤8%", C1),
        ("C2 便宜+科技配额40%", C2),
        ("D1 科技∩温和超跌(40~80%分位)", D1),
        ("D2 科技∩便宜∩温和超跌", D2),
    ]


def main():
    data = build()
    n_tech = len(TECH_L1)
    print(f"有效截面 {len(data)}  持有 {HOP} 日  科技域定义: {sorted(TECH_L1)} ({n_tech}个一级行业)")
    print("超额 = 子集中位 − 宇宙中位（无前视，全A可算量）\n")

    rules = build_rules()
    print(f"{'规则':<34}{'入场日':>5}{'子集中位':>10}{'超额中位':>10}"
          f"{'超额均值':>10}{'胜率':>8}{'只数':>6}{'最差单票':>10}")
    print("-" * 100)
    res = []
    for name, fn in rules:
        r = ev(name, fn, data)
        if not r:
            print(f"{name:<34} 无数据")
            continue
        res.append(r)
        print(f"{r['name']:<34}{r['n']:>5}{r['med']:>+9.2f}%{r['exc']:>+9.2f}pct"
              f"{r['exc_mean']:>+10.2f}{r['beat']:>7.1f}%{r['size']:>6}{r['worst']:>+9.2f}%")

    k = len(data) // 3
    print("\n=== 三段稳健性（超额中位 pct） ===")
    print(f"{'规则':<34}{'段1':>10}{'段2':>10}{'段3':>10}")
    for name, fn in rules:
        vals = []
        for b in (data[:k], data[k:2 * k], data[2 * k:]):
            r = ev(name, fn, b)
            vals.append(f"{r['exc']:>+9.2f}" if r else "      NA ")
        print(f"{name:<34}" + " ".join(vals))

    # 关键分解：便宜 alpha 的行业归因 —— 各一级行业内「最便宜1/3」vs「最贵1/3」
    print("\n=== 便宜 alpha 的行业分解（便宜1/3 − 最贵1/3，20日超额 pct） ===")
    from collections import defaultdict
    acc = defaultdict(list)
    for rec in data:
        by = defaultdict(list)
        for x in rec["rows"]:
            by[x["l1"]].append(x)
        for l1name, rows in by.items():
            if len(rows) < 12:
                continue
            v = sorted(rows, key=lambda y: y["price"])
            n = max(3, len(v) // 3)
            lo, hi = v[:n], v[-n:]
            acc[l1name].append(med([y["fwd"] for y in lo]) - med([y["fwd"] for y in hi]))
    rows_out = []
    for k2, v in acc.items():
        if len(v) < 20:
            continue
        rows_out.append((k2, med(v) * 100, fe._mean(v) * 100,
                        100.0 * sum(1 for x in v if x > 0) / len(v), len(v)))
    rows_out.sort(key=lambda x: -x[1])
    print(f"{'一级行业':<12}{'便宜−贵(中位)':>14}{'均值':>10}{'胜率':>8}{'样本':>6}  科技?")
    for name, m1, m2, wr, n in rows_out:
        flag = "★" if name in TECH_L1 else " "
        print(f"{name:<12}{m1:>+13.2f}pct{m2:>+10.2f}{wr:>7.1f}%{n:>6}   {flag}")

    out = os.path.join(HERE, "pool_tech_backtest.json")
    json.dump({"n_days": len(data), "results": res,
               "industry_cheap_alpha": [
                   {"l1": a, "med": round(b, 2), "mean": round(c, 2),
                    "win": round(d, 1), "n": e, "tech": a in TECH_L1}
                   for a, b, c, d, e in rows_out]},
              open(out, "w"), ensure_ascii=False, indent=1)
    print(f"\n写入 {out}")


if __name__ == "__main__":
    main()
