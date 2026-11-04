#!/usr/bin/env python3
"""诊断 SIZE 因子随市值下限的衰减，并定位「不影响收益率的市值下限」。

## 老大 2026-10-25 追问
「核实剔除小于50亿后 +8.63pct 降到 +5.43pct 的原因。需要回测市值小于多少，
  不会影响这个收益率」

## 先纠正一个口径混用（必须说清，否则结论会误导）
上一轮两个数字口径不同，不可直接相减：
  · +8.63pct = backtest_pool_factor.py「SIZE 市值最小9%」，超额基准 = **全宇宙中位**
  · +5.43pct = backtest_pool_mktcap.py「50亿下限 + 市值最小18%」，超额基准 = **同 floor 内宇宙中位**
前者含「小市值整体跑赢大盘」的 beta，后者已把该 beta 剥离，只留纯选股能力。
→ 所以 +8.63 → +5.43 里，一部分是**口径差异**，一部分是**真实衰减**。
本脚本用统一口径（同 floor 内中位）重算，把两者彻底分开。

## 本脚本要定位的量
对每个市值下限 M，在 [M, +∞) 子集内做同样的「市值最小18%」选股，得到超额 Exc(M)。
  · Exc(0) = SIZE 的完整强度
  · 求最大 M*，使 Exc(M*) 与 Exc(0) 的配对差 **不显著**（|t| < 2）
  → M* 就是「不影响收益率」的市值下限上限
同时按市值分层给出 alpha 的分布，解释衰减从哪来。

## 口径
· 总市值 = 今日 mktcap/price 反推总股本 × 历史价格；流通市值 = nmc/price 反推
· 无前视：市值用当日价格算，选股只用当日可得信息
· 持有 20 日，超额 = 子集中位 − 同一 floor 内宇宙中位（控制下限本身带来的 beta）
· 1012 宇宙为「历史曾入成交额榜」并集，偏大盘（中位 187.7 亿），对小微盘 alpha 是保守估计
"""
import json
import os
import sys
import statistics

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import factor_eval as fe

HOP = 20


def med(a):
    if not a:
        return None
    a = sorted(a)
    n = len(a)
    return a[n // 2] if n % 2 else (a[n // 2 - 1] + a[n // 2]) / 2.0


def load_shares():
    snap = "/tmp/alla_snapshot.json"
    if not os.path.exists(snap):
        raise SystemExit(f"缺 {snap}，先跑 fetch 抓全A快照")
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
                "c": c, "fwd": p1 / p0 - 1.0,
                "mv_tot": tot_sh[c] * p0,
                "mv_circ": circ_sh.get(c, tot_sh[c]) * p0,
            })
        if len(rows) < 200:
            continue
        data.append({"date": d0, "rows": rows})
    return data


def exc_series(data, floor, key="mv_tot", frac=0.18):
    """返回每个截面的 (子集中位 − 同floor宇宙中位) 序列，以及只数。"""
    out, sizes = [], []
    for rec in data:
        base = [x for x in rec["rows"] if x[key] >= floor]
        if len(base) < 40:
            continue
        sub = sorted(base, key=lambda x: x[key])[:max(20, int(len(base) * frac))]
        if len(sub) < 20:
            continue
        sm = med([x["fwd"] for x in sub])
        bm = med([x["fwd"] for x in base])
        if sm is None or bm is None:
            continue
        out.append(sm - bm)
        sizes.append(len(sub))
    return out, sizes


def main():
    data = build()
    print(f"有效截面 {len(data)}  持有 {HOP} 日  宇宙 1012 只")
    print("超额 = 子集中位 − **同一市值下限内**宇宙中位（已剥离小市值 beta，只看选股能力）\n")

    floors_yi = [0, 5, 10, 15, 20, 25, 30, 35, 40, 45, 50, 60, 70, 80, 100, 120, 150]
    base_ex, _ = exc_series(data, 0, "mv_tot")

    print("=" * 88)
    print("【一】按总市值下限扫描（排序键=总市值，取该下限内市值最小18%）")
    print("=" * 88)
    print(f"{'下限':>6}{'宇宙剩余':>9}{'选股只数':>9}{'超额中位':>10}{'vs无下限':>10}{'t值':>8}  判定")
    print("-" * 88)
    scan = []
    for fl in floors_yi:
        r, sz = exc_series(data, fl * 1e8, "mv_tot")
        if len(r) < 10:
            continue
        un = int(fe._mean([len([x for x in rec["rows"] if x["mv_tot"] >= fl * 1e8]) for rec in data]))
        m = med(r) * 100
        n = min(len(r), len(base_ex))
        d = [r[i] - base_ex[i] for i in range(n)]
        mu = statistics.mean(d) * 100
        sd = statistics.stdev(d) if n > 1 else 0
        t = mu / (sd / (n ** 0.5)) if sd > 0 else 0
        tag = "无显著影响" if abs(t) < 2 else ("显著下降" if t < 0 else "显著上升")
        scan.append({"floor": fl, "universe": un, "exc": round(m, 2),
                     "delta": round(mu, 2), "t": round(t, 2), "verdict": tag})
        print(f"{fl:>5}亿{un:>9}{int(fe._mean(sz)):>9}{m:>+9.2f}pct{mu:>+9.2f}{t:>+7.2f}  {tag}")

    # 求最大「不影响」的下限
    ok = [x for x in scan if abs(x["t"]) < 2 and x["floor"] > 0]
    print()
    if ok:
        best = max(ok, key=lambda x: x["floor"])
        print(f"→ **市值下限 ≤ {best['floor']} 亿时，对收益率无显著影响**"
              f"（t={best['t']:+.2f}，超额 {best['exc']:+.2f}pct vs 无下限 {scan[0]['exc']:+.2f}pct）")
    first_sig = next((x for x in scan if x["floor"] > 0 and x["t"] < -2), None)
    if first_sig:
        print(f"→ 从 {first_sig['floor']} 亿 起开始显著下降"
              f"（t={first_sig['t']:+.2f}，超额降至 {first_sig['exc']:+.2f}pct）")

    print("\n" + "=" * 88)
    print("【二】衰减来源：alpha 按市值分层的分布（各市值桶内「最小20% − 其余」）")
    print("=" * 88)
    buckets = [(0, 30), (30, 50), (50, 80), (80, 120), (120, 200), (200, 400), (400, 99999)]
    print(f"{'市值区间':>14}{'平均只数':>9}{'桶内小市值超额':>15}{'胜率':>8}")
    print("-" * 62)
    for lo, hi in buckets:
        vals, ns = [], []
        for rec in data:
            bk = [x for x in rec["rows"] if lo * 1e8 <= x["mv_tot"] < hi * 1e8]
            if len(bk) < 12:
                continue
            v = sorted(bk, key=lambda x: x["mv_tot"])
            k = max(3, len(v) // 5)
            lo_g, rest = v[:k], v[k:]
            if len(rest) < 3:
                continue
            vals.append(med([x["fwd"] for x in lo_g]) - med([x["fwd"] for x in rest]))
            ns.append(len(bk))
        if len(vals) < 10:
            print(f"{lo:>6}-{hi if hi < 9999 else '∞':<7}   样本不足")
            continue
        wr = 100.0 * sum(1 for x in vals if x > 0) / len(vals)
        print(f"{lo:>6}-{hi if hi < 9999 else '∞':<7}{int(fe._mean(ns)):>9}"
              f"{med(vals)*100:>+14.2f}pct{wr:>7.1f}%")

    # 验证：讲义口径复现（vs 全宇宙中位，含 beta），说明 +8.63 的来源
    print("\n" + "=" * 88)
    print("【三】口径对照：为什么上一轮出现 +8.63 与 +5.43 的落差")
    print("=" * 88)
    for fl in (0, 50):
        for frac, lbl in ((0.09, "最小9%"), (0.18, "最小18%")):
            r_ex, sz = exc_series(data, fl * 1e8, "mv_tot", frac)
            # 同口径A: vs 同floor宇宙中位（纯选股）
            a = med(r_ex) * 100
            # 同口径B: vs 全宇宙中位（含小市值 beta）
            bvals = []
            for rec, i in zip(data, range(len(data))):
                base = [x for x in rec["rows"] if x["mv_tot"] >= fl * 1e8]
                if len(base) < 40:
                    continue
                sub = sorted(base, key=lambda x: x["mv_tot"])[:max(20, int(len(base) * frac))]
                if len(sub) < 20:
                    continue
                allm = med([x["fwd"] for x in rec["rows"]])
                sm = med([x["fwd"] for x in sub])
                if allm is None or sm is None:
                    continue
                bvals.append(sm - allm)
            b = med(bvals) * 100 if bvals else float("nan")
            print(f"{fl:>4}亿下限 {lbl:<8} | vs同floor中位 {a:>+7.2f}pct"
                  f"（纯选股） | vs全宇宙中位 {b:>+7.2f}pct（含beta）")

    out = os.path.join(HERE, "pool_size_floor_scan.json")
    json.dump({"n_days": len(data), "scan": scan}, open(out, "w"),
              ensure_ascii=False, indent=1)
    print(f"\n写入 {out}")


if __name__ == "__main__":
    main()
