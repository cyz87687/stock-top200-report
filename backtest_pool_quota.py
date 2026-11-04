#!/usr/bin/env python3
"""P0-1 终局：科技配额 × 价格分位的二维网格。

## 为什么需要这一轮
backtest_pool_tech.py 证明「便宜 alpha 在科技行业内同样成立且更强」：
  计算机 +7.61pct(胜率93.1%) / 电力设备 +7.11(94.8%) / 机械设备 +6.05(79.3%)
  / 电子 +4.45(100%) / 国防军工 +3.29(77.6%) / 通信 +2.10(51.7%)
  对比 银行 +0.28(53.4%) —— 便宜 alpha 在银行内部几乎无效
但 B1「科技域全体」= -2.35pct(胜率20.7%)，说明「偏科技」本身是负 alpha，
必须叠加「便宜」条件。

## 本脚本要解决的问题
v3.7 首版按「全A最便宜18%」选出 500 只，科技仅 63 只(12.6%)。
若按「科技域内便宜」筛，科技便宜股数量有限（B3 交集仅 43 只），
不足支撑 500 广度。

→ 需要找到「科技占比」与「池子超额」之间的真实权衡曲线，
  而不是拍脑袋定配额。

## 网格
  tech_quota ∈ {0%, 15%, 30%, 45%, 60%, 75%, 100%}
    · 科技股：科技域内按价格升序取，凑满 quota × 500
    · 非科技股：非科技域内按价格升序取，凑满余量
  每个 quota 下再测价格分位松紧（科技域内取最便宜 10%/20%/30%）
"""
import json
import os
import sys
from collections import defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import factor_eval as fe

HOP = 20
POOL = 500
TECH_L1 = {"电子", "计算机", "通信", "电力设备", "国防军工", "机械设备"}


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
    h = json.load(open(os.path.join(HERE, "factor_history.json")))
    snaps, _ = fe._drop_stale_price_snapshots(h["snapshots"])
    tdays, px = fe.px_index(h)
    sw = json.load(open(os.path.join(HERE, "sector", "sw_map.json")))
    l1 = sw["l1"]
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
                         "l1": l1.get(c6, "未知"), "tech": l1.get(c6) in TECH_L1})
        if len(rows) < 300:
            continue
        data.append({"date": d0, "rows": rows,
                     "all_med": med([x["fwd"] for x in rows])})
    return data


def pick_quota(rows, quota, tech_frac=0.20):
    """科技域按价格升序取 quota*POOL；非科技域按价格升序补齐。"""
    n_tech = int(POOL * quota)
    t = sorted([x for x in rows if x["tech"]], key=lambda x: x["price"])
    nt = sorted([x for x in rows if not x["tech"]], key=lambda x: x["price"])
    # 科技域内也做价格分位截断（tech_frac 比例的候选，再取前 n_tech）
    pool_t = t[:max(n_tech, int(len(t) * tech_frac))][:n_tech] if n_tech > 0 else []
    got_t = len(pool_t)
    need = POOL - got_t
    pool_nt = nt[:need]
    return pool_t + pool_nt, got_t


def ev(name, fn, data):
    meds, exs, wins, sizes, tshares, worst = [], [], 0, [], [], []
    for rec in data:
        sub, n_t = fn(rec["rows"], rec)
        if len(sub) < 30:
            continue
        sm = med([x["fwd"] for x in sub])
        if sm is None:
            continue
        meds.append(sm)
        exs.append(sm - rec["all_med"])
        sizes.append(len(sub))
        tshares.append(100.0 * n_t / len(sub))
        worst.append(min(x["fwd"] for x in sub))
        if sm > rec["all_med"]:
            wins += 1
    if len(exs) < 10:
        return None
    return {"name": name, "n": len(exs), "med": round(med(meds) * 100, 2),
            "exc": round(med(exs) * 100, 2), "exc_mean": round(fe._mean(exs) * 100, 2),
            "beat": round(100.0 * wins / len(exs), 1),
            "size": int(fe._mean(sizes)), "tech_pct": round(fe._mean(tshares), 1),
            "worst": round(med(worst) * 100, 2),
            "_exs": exs}


def main():
    data = build()
    print(f"有效截面 {len(data)}  持有 {HOP} 日  目标池规模 {POOL}\n")

    grid = []
    for quota in (0.00, 0.15, 0.30, 0.45, 0.60, 0.75, 1.00):
        for tf in (0.10, 0.20, 0.30):
            grid.append((quota, tf))

    print(f"{'科技配额':>7} {'科技域价格分位':>13} {'入场日':>5} {'子集中位':>9}"
          f"{'超额中位':>9}{'超额均值':>9}{'胜率':>7}{'只数':>5}{'实际科技%':>9}{'最差':>9}")
    print("-" * 100)
    res = []
    for quota, tf in grid:
        name = f"q{int(quota*100):>3}%_tf{int(tf*100):>2}%"
        r = ev(name, lambda rows, rec, q=quota, t=tf: pick_quota(rows, q, t), data)
        if not r:
            continue
        r.update({"quota": quota, "tech_frac": tf})
        res.append(r)
        print(f"{int(quota*100):>6}% {int(tf*100):>12}% {r['n']:>5} {r['med']:>+8.2f}%"
              f"{r['exc']:>+8.2f}pct{r['exc_mean']:>+9.2f}{r['beat']:>6.1f}%"
              f"{r['size']:>5}{r['tech_pct']:>8.1f}%{r['worst']:>+8.2f}%")

    # 配额维度的权衡曲线（取每个 quota 的 tech_frac=0.20 代表值）
    print("\n=== 权衡曲线（tech_frac=20%） ===")
    print(f"{'科技配额':>7}{'超额中位':>10}{'胜率':>8}{'实际科技%':>10}  判定")
    for r in res:
        if abs(r["tech_frac"] - 0.20) > 1e-9:
            continue
        verdict = "✅" if r["exc"] > 3.0 and r["beat"] > 75 else ("△" if r["exc"] > 0 else "✗")
        print(f"{int(r['quota']*100):>6}%{r['exc']:>+9.2f}pct{r['beat']:>7.1f}%"
              f"{r['tech_pct']:>9.1f}%  {verdict}")

    # 配对显著性：科技占比提升是否显著牺牲超额
    print("\n=== 配对检验（vs quota=0 基线，超额差） ===")
    base = next((r for r in res if r["quota"] == 0.0 and abs(r["tech_frac"] - 0.20) < 1e-9), None)
    if base:
        import statistics
        for r in res:
            if abs(r["tech_frac"] - 0.20) > 1e-9 or r["quota"] == 0.0:
                continue
            d = [a - b for a, b in zip(r["_exs"], base["_exs"])]
            n = len(d)
            mu = statistics.mean(d)
            sd = statistics.stdev(d) if n > 1 else 0
            t = mu / (sd / (n ** 0.5)) if sd > 0 else 0
            print(f"  quota {int(r['quota']*100):>3}%  超额差 {mu*100:>+7.2f}pct  "
                  f"t={t:>+6.2f}  {'显著' if abs(t) > 2 else '不显著(噪声内)'}")

    for r in res:
        r.pop("_exs", None)
    out = os.path.join(HERE, "pool_tech_quota.json")
    json.dump({"n_days": len(data), "results": res},
              open(out, "w"), ensure_ascii=False, indent=1)
    print(f"\n写入 {out}")


if __name__ == "__main__":
    main()
