#!/usr/bin/env python3
"""定位「市值下限不影响收益率」的准确答案 —— 用成分分布而非 t 值。

## 上一版的两个方法论错误（本脚本修正）
① t 值爆炸（-154 ~ -847）：配对差的跨截面方差极小（每个截面的差异几乎恒定，
   属**系统性**变化而非随机噪声），t 检验前提不成立 → 改用「方向一致性 + 三段
   稳健性 + 成分分布」三重判据。
② 「0~20 亿无影响」是**样本假象**：1012 宇宙本身偏大盘（中位 187.7 亿、最小 12.2 亿），
   下限设 10 亿只剔掉 0 只、设 20 亿只剔 6 只 → 当然没影响。
   这只说明「该宇宙内没有小票」，**不能推出「全A 设 20 亿下限无代价」**。

## 正确的问题分解
SIZE 选股超额 = 选中了宇宙中最小的那批 → 超额来源就是那批股票本身。
任何市值下限 M 都会切掉 [0, M) 的候选，代价 = 被切掉的那批股票所贡献的超额。
所以真正要测的是：
  Q1 无下限时选出的 178 只，市值落在哪些段？（= 超额由谁贡献）
  Q2 逐步抬高下限，选出的股票市值中位如何被迫上移？（= 机制）
  Q3 下限 M 时，[M,∞) 内还剩多少候选？若候选充足，选股逻辑不被扭曲。
  Q4 分段看：每段的最优下限是否一致？（= 稳健性）

## 结论口径
给老大一个**可执行**的答案：在保证「候选池充足 + 三段超额不转负」前提下，
市值下限最低可设到多少；并明确标注该结论的外推边界。
"""
import json
import os
import sys

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
            rows.append({"c": c, "fwd": p1 / p0 - 1.0,
                         "mv_tot": tot_sh[c] * p0,
                         "mv_circ": circ_sh.get(c, tot_sh[c]) * p0})
        if len(rows) < 200:
            continue
        data.append({"date": d0, "rows": rows})
    return data


def run(data, floor, frac=0.18):
    """返回 (超额序列, 选中市值中位序列, 候选数序列, 选中只数序列)"""
    ex, mvs, cand, picks = [], [], [], []
    for rec in data:
        base = [x for x in rec["rows"] if x["mv_tot"] >= floor]
        if len(base) < 40:
            continue
        k = max(20, int(len(base) * frac))
        sub = sorted(base, key=lambda x: x["mv_tot"])[:k]
        if len(sub) < 20:
            continue
        sm = med([x["fwd"] for x in sub])
        bm = med([x["fwd"] for x in base])
        if sm is None or bm is None:
            continue
        ex.append(sm - bm)
        mvs.append(med([x["mv_tot"] for x in sub]) / 1e8)
        cand.append(len(base))
        picks.append(len(sub))
    return ex, mvs, cand, picks


def main():
    data = build()
    print(f"有效截面 {len(data)}  持有 {HOP} 日  宇宙 1012 只（中位 187.7 亿，最小 12.2 亿）")
    print("超额 = 子集中位 − 同一市值下限内宇宙中位\n")

    # ---- Q1: 无下限时选出的 178 只，市值落在哪些段 ----
    print("=" * 92)
    print("【Q1】无下限时「市值最小18%」选出的股票，市值分布（超额由谁贡献）")
    print("=" * 92)
    buckets = [(0, 20), (20, 30), (30, 40), (40, 50), (50, 60), (60, 80), (80, 120), (120, 9999)]
    agg = {b: [] for b in buckets}
    for rec in data:
        base = rec["rows"]
        k = max(20, int(len(base) * 0.18))
        sub = sorted(base, key=lambda x: x["mv_tot"])[:k]
        for x in sub:
            yi = x["mv_tot"] / 1e8
            for b in buckets:
                if b[0] <= yi < b[1]:
                    agg[b].append(x["fwd"])
                    break
    print(f"{'市值区间':>14}{'累计只次':>10}{'占选中比例':>11}{'该段平均涨幅':>13}")
    print("-" * 52)
    tot_n = sum(len(v) for v in agg.values())
    cum = 0
    for b in buckets:
        v = agg[b]
        if not v:
            continue
        cum += len(v)
        lbl = f"{b[0]}-{b[1]}" if b[1] < 9999 else f"{b[0]}+"
        print(f"{lbl:>14}{len(v):>10}{100.0*len(v)/tot_n:>10.1f}%"
              f"{med(v)*100:>+12.2f}%   {'累计 %.1f%%' % (100.0*cum/tot_n)}")

    # ---- Q2 + Q3: 下限 → 市值中位上移 / 候选数 ----
    print("\n" + "=" * 92)
    print("【Q2+Q3】抬高下限的机制：选中股市值被迫上移 & 候选是否充足")
    print("=" * 92)
    floors = [0, 10, 15, 20, 25, 30, 35, 40, 45, 50, 60, 80, 100, 120, 150]
    print(f"{'下限':>6}{'候选只数':>9}{'选中市值中位':>13}{'超额中位':>10}"
          f"{'恶化截面占比':>13}  三段超额（段1/段2/段3）")
    print("-" * 100)
    base_ex = None
    scan = []
    for fl in floors:
        ex, mvs, cand, picks = run(data, fl * 1e8)
        if len(ex) < 10:
            continue
        if base_ex is None:
            base_ex = ex
        n = min(len(ex), len(base_ex))
        worse = sum(1 for i in range(n) if ex[i] < base_ex[i] - 1e-12)
        k = len(ex) // 3
        segs = []
        for b in (ex[:k], ex[k:2 * k], ex[2 * k:]):
            segs.append(f"{med(b)*100:>+6.2f}" if b else "    NA")
        scan.append({"floor": fl, "cand": int(fe._mean(cand)),
                     "mv_med": round(fe._mean(mvs), 1),
                     "exc": round(med(ex) * 100, 2),
                     "worse_pct": round(100.0 * worse / n, 1),
                     "segs": [round(med(b) * 100, 2) if b else None
                              for b in (ex[:k], ex[k:2 * k], ex[2 * k:])]})
        print(f"{fl:>5}亿{int(fe._mean(cand)):>9}{fe._mean(mvs):>12.1f}亿"
              f"{med(ex)*100:>+9.2f}pct{100.0*worse/n:>12.0f}%   " + " / ".join(segs))

    # ---- Q4: 判据汇总 ----
    print("\n" + "=" * 92)
    print("【Q4】判定：哪些下限「不影响收益率」")
    print("=" * 92)
    print("判据（三者同时满足才算「不影响」）：")
    print("  a) 超额中位 ≥ 无下限的 95%")
    print("  b) 三段超额全部为正")
    print("  c) 恶化截面占比 ≤ 60%（未系统性劣化）")
    print()
    base_e = scan[0]["exc"]
    print(f"{'下限':>6}{'超额中位':>10}{'占基线':>8}{'a':>4}{'b':>4}{'c':>4}  结论")
    print("-" * 62)
    safe_max = 0
    for r in scan:
        a = r["exc"] >= base_e * 0.95
        b = all(s is not None and s > 0 for s in r["segs"])
        c = r["worse_pct"] <= 60.0
        okk = a and b and c
        if okk and r["floor"] > safe_max:
            safe_max = r["floor"]
        print(f"{r['floor']:>5}亿{r['exc']:>+9.2f}pct{100.0*r['exc']/base_e:>7.0f}%"
              f"{' ✅' if a else ' ✗':>4}{' ✅' if b else ' ✗':>4}{' ✅' if c else ' ✗':>4}"
              f"  {'不影响' if okk else '有代价'}")

    print(f"\n→ **在本回测宇宙内，市值下限 ≤ {safe_max} 亿 不影响收益率**（三项判据全过）")
    print("→ ⚠️ 外推边界：该宇宙中位 187.7 亿、最小 12.2 亿，本就缺少小票；")
    print("   全A 中位仅 48.1 亿，同样的 50 亿下限在全A 上切掉的候选比例远高于本宇宙，")
    print("   故「不影响」的阈值在本宇宙内成立，全A 实盘须按实际候选覆盖复核。")

    out = os.path.join(HERE, "pool_size_floor_scan2.json")
    json.dump({"n_days": len(data), "scan": scan, "safe_max_yi": safe_max},
              open(out, "w"), ensure_ascii=False, indent=1)
    print(f"\n写入 {out}")


if __name__ == "__main__":
    main()
