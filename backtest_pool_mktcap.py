#!/usr/bin/env python3
"""验证老大新约束下 SIZE 因子是否仍有效（剔除低价后改用 final_ranking 因子）。

## 老大 2026-10-25 指令
1. 不剔除北交所，用全A 所有成分股
2. 剔除成交额 < 1 亿（流动性）
3. 剔除总市值 < 50 亿（流动性）

## 本脚本要回答的致命问题
SIZE（小市值）是 47 因子里唯一有显著超额的因子（exc_t=+2.28 / LS_IR=1.95 / score=0.993）。
但「总市值 ≥ 50 亿」这条约束会**系统性切掉 SIZE 最有效的小微盘区间**。
必须先测：在 50 亿以上的子集里，SIZE 方向是否还成立、幅度还剩多少？
  若仍显著 → 按老大口径上线，并如实说明幅度衰减
  若失效   → 必须如实报告「50 亿下限与 SIZE 因子冲突」，请老大裁决
不许凭直觉上线。

## 数据边界（诚实披露）
· 总市值重建：今日 mktcap/price 反推总股本 × 历史价格。股本视为不变（忽略增发/回购，
  在 5 个月窗口内对横截面排序影响很小，但对绝对市值有小幅误差）。
· 成交额无法历史重建：价格 store 未存 volume。故「成交额 ≥1 亿」这条**无法回测**，
  只能用今日快照做实证约束。这是本脚本的能力边界，必须标明。
· 1012 宇宙 = 历史曾入成交额榜并集，偏大盘（市值中位 187.7 亿 vs 全A 48.1 亿），
  故其结果对「小市值更强」是保守估计。

## 网格
  mktcap_floor ∈ {0, 20亿, 30亿, 50亿, 80亿, 150亿}
  每个 floor 下测：全宇宙基准 / 市值最小18% / 市值最小9%
"""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import factor_eval as fe

HOP = 20
POOL = 500


def med(a):
    if not a:
        return None
    a = sorted(a)
    n = len(a)
    return a[n // 2] if n % 2 else (a[n // 2 - 1] + a[n // 2]) / 2.0


def load_shares():
    """今日快照反推总股本（mktcap/price）与流通股本（nmc/price）。"""
    snap = "/tmp/alla_snapshot.json"
    if not os.path.exists(snap):
        raise SystemExit(f"缺 {snap}")
    rows = json.load(open(snap))
    tot, circ = {}, {}
    for r in rows:
        try:
            p = float(r.get("trade") or 0)
            mk = float(r.get("mktcap") or 0) * 1e4
            nm = float(r.get("nmc") or 0) * 1e4
        except Exception:
            continue
        if p > 0:
            if mk > 0:
                tot[r["code"]] = mk / p
            if nm > 0:
                circ[r["code"]] = nm / p
    return tot, circ


def build():
    h = json.load(open(os.path.join(HERE, "factor_history.json")))
    snaps, _ = fe._drop_stale_price_snapshots(h["snapshots"])
    tdays, px = fe.px_index(h)
    tot_sh, circ_sh = load_shares()
    tidx = {d: i for i, d in enumerate(tdays)}
    codes = [c for c in px if c in tot_sh]
    print(f"宇宙 {len(px)} 只，可反推总股本 {len(codes)} 只")

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
            rows.append({
                "c": c, "fwd": p1 / p0 - 1.0, "price": p0,
                "mv_tot": tot_sh[c] * p0,          # 总市值
                "mv_circ": circ_sh.get(c, tot_sh[c]) * p0,
            })
        if len(rows) < 200:
            continue
        data.append({"date": d0, "rows": rows,
                     "all_med": med([x["fwd"] for x in rows])})
    return data


def ev(name, fn, data, floor=0.0):
    meds, exs, wins, sizes = [], [], 0, []
    for rec in data:
        base = [x for x in rec["rows"] if x["mv_tot"] >= floor]
        if len(base) < 30:
            continue
        base_med = med([x["fwd"] for x in base])
        sub = fn(base)
        if len(sub) < 20:
            continue
        sm = med([x["fwd"] for x in sub])
        if sm is None:
            continue
        meds.append(sm)
        exs.append(sm - base_med)
        sizes.append(len(sub))
        if sm > base_med:
            wins += 1
    if len(exs) < 10:
        return None
    return {"name": name, "floor_yi": round(floor / 1e8, 0), "n": len(exs),
            "med": round(med(meds) * 100, 2), "exc": round(med(exs) * 100, 2),
            "exc_mean": round(fe._mean(exs) * 100, 2),
            "beat": round(100.0 * wins / len(exs), 1),
            "size": int(fe._mean(sizes)), "_exs": exs}


def main():
    data = build()
    print(f"有效截面 {len(data)}  持有 {HOP} 日")
    print("超额 = 子集中位 − **同 floor 内宇宙中位**（控制住市值下限的影响，看纯选股能力）\n")

    def smallest(frac):
        def f(rows):
            v = sorted(rows, key=lambda x: x["mv_tot"])
            return v[:max(20, int(len(v) * frac))]
        return f

    floors = [0, 20e8, 30e8, 50e8, 80e8, 150e8]
    print(f"{'市值下限':>9}{'宇宙剩余':>9}{'策略':<14}{'入场日':>5}{'超额中位':>10}"
          f"{'超额均值':>10}{'胜率':>8}{'只数':>6}")
    print("-" * 78)
    res = []
    for fl in floors:
        # 该 floor 下宇宙规模
        szs = [len([x for x in r["rows"] if x["mv_tot"] >= fl]) for r in data]
        universe_n = int(fe._mean(szs))
        for lbl, fn in (("市值最小18%", smallest(0.18)), ("市值最小9%", smallest(0.09))):
            r = ev(lbl, fn, data, floor=fl)
            if not r:
                print(f"{fl/1e8:>8.0f}亿{universe_n:>9}{lbl:<14} 样本不足")
                continue
            res.append(r)
            print(f"{fl/1e8:>8.0f}亿{universe_n:>9}{lbl:<14}{r['n']:>5}{r['exc']:>+9.2f}pct"
                  f"{r['exc_mean']:>+10.2f}{r['beat']:>7.1f}%{r['size']:>6}")

    print("\n=== 关键判断：SIZE 在不同市值下限下的衰减 ===")
    print(f"{'市值下限':>9}{'最小18%超额':>14}{'最小9%超额':>13}  结论")
    by_floor = {}
    for r in res:
        by_floor.setdefault(r["floor_yi"], {})[r["name"]] = r
    for fl in sorted(by_floor):
        d = by_floor[fl]
        a = d.get("市值最小18%", {}).get("exc")
        b = d.get("市值最小9%", {}).get("exc")
        if a is None:
            continue
        verdict = "✅ 强" if a > 5 else ("△ 中" if a > 2 else ("弱" if a > 0 else "✗ 失效"))
        print(f"{fl:>8.0f}亿{a:>+13.2f}pct{(b if b is not None else 0):>+12.2f}pct  {verdict}")

    # 配对检验：50亿下限 vs 无下限（同一策略）
    print("\n=== 配对检验：50亿下限 是否显著削弱 SIZE ===")
    import statistics
    for lbl in ("市值最小18%", "市值最小9%"):
        f0 = next((r for r in res if r["floor_yi"] == 0 and r["name"] == lbl), None)
        f50 = next((r for r in res if r["floor_yi"] == 50 and r["name"] == lbl), None)
        if not (f0 and f50):
            continue
        n = min(len(f0["_exs"]), len(f50["_exs"]))
        d = [f50["_exs"][i] - f0["_exs"][i] for i in range(n)]
        mu = statistics.mean(d)
        sd = statistics.stdev(d) if n > 1 else 0
        t = mu / (sd / (n ** 0.5)) if sd > 0 else 0
        tag = ("显著削弱" if t < -2 else
               ("显著增强" if t > 2 else "不显著（噪声内，可放心加约束）"))
        print(f"  {lbl:<12} 50亿 − 无下限 = {mu*100:>+7.2f}pct  t={t:>+6.2f}  {tag}")

    for r in res:
        r.pop("_exs", None)
    out = os.path.join(HERE, "pool_mktcap_floor_test.json")
    json.dump({"n_days": len(data), "results": res},
              open(out, "w"), ensure_ascii=False, indent=1)
    print(f"\n写入 {out}")


if __name__ == "__main__":
    main()
