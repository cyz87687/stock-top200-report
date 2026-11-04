#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""round38b：成长门限单调性的截面内配对检验（round38 第二章的修正）

## 为什么需要这一步

round38 第二章给出的分档前瞻收益是**池化均值**：
把所有截面、所有股票的前瞻收益堆在一起算一个 mean。
h20 读数：G<=4 → +1.70% / 4<G<=7 → +1.92% / G>7 → +2.15%（单调递增），
看上去"成长门限方向是对的"，但同章 一之补 里成长分单维 rank IC 仅 +0.0102（t 0.97，不显著）。

**这两句不可能同时为真。** 池化均值的标准误是假的：
- 每个桶 n≈13-17 万，若按独立样本算 SE≈15/sqrt(13万)≈0.04pct，0.45pct 差异"极显著"；
- 但同一天的两只股票收益率高度相关（共同的 market/beta 冲击），
  有效独立样本数≈**截面数 218**，不是 13 万。
- 所以 0.45pct 的真实 t 值大概率 <1，即噪声。

## 本脚本做什么（三项，全部在截面内做，日期为独立观测单元）

① **配对差序列**：每个截面 d 内，分别算 G>7 组与 G<=4 组的等权平均前瞻收益，
   取差的时序 {Δ_d}，再对 Δ 序列做 NW t（lag=4）。这才是同口径可比。
② **市值中性化后的配对差**：G 与行业内市值分有 −0.063 的弱负相关，
   分档差异可能混入市值效应。在每个截面内按市值分五分组，组内再比较成长档，
   即「同市值档内比成长」——排除市值混杂。
③ **成长分的分组 IC 稳定性**：按截面算 rank IC，统计其夏普（ICIR）与胜率。

## 输出
audit_round38b.json

⚠️ 本脚本只做诊断，不改任何生产代码权重。
"""
import os
import sys
import json

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from audit_round38_size_ablation import (
    build_ab_dims, fwd_ret, is_num, spearman, nw_t, HOPS, PANEL, KLONG,
)


def _cl(px, c6, d):
    m = px.get(c6)
    if not m:
        return None
    b = m.get(d)
    if isinstance(b, (list, tuple)) and len(b) >= 2 and is_num(b[1]):
        return float(b[1])
    return float(b) if is_num(b) else None


def _tdays(px, dates):
    """全市场交易日历（≥50% 个股出现即交易日），与 round38 同法。"""
    from collections import Counter
    cnt = Counter()
    for m in px.values():
        for d in m:
            cnt[d] += 1
    n = len(px)
    return sorted(d for d, c in cnt.items() if c >= n * 0.5)


# szir 是「低市值→高分」，故 szir 升序切五档时 Q1=市值最大、Q5=市值最小。
# 标签写死方向，避免读者把「Q1 最强」误读成「小市值最强」。
_QLAB = {0: "Q1最大市值", 1: "Q2", 2: "Q3", 3: "Q4", 4: "Q5最小市值"}


def main():
    panel = json.load(open(PANEL, encoding="utf-8"))
    px = json.load(open(KLONG, encoding="utf-8"))
    tdays = _tdays(px, panel["dates"])
    tidx = {d: i for i, d in enumerate(tdays)}

    print("重建 A 维与成长分（与 round38 完全同源）...")
    per_date, meta = build_ab_dims(panel)

    OUT = {}

    # ─────────── ① 配对差序列：G>7 vs G<=4 ───────────
    print("\n" + "-" * 100)
    print("① 截面内配对差：G>7 组 − G<=4 组 的等权平均前瞻收益（每个截面一个观测 → NW t）")
    print("-" * 100)
    print(f"{'周期':>6} {'截面数':>7} {'Δ均值':>9} {'Δ中位':>9} {'NW t':>8} {'Δ>0 胜率':>10} {'Δ 标准差':>10}")
    paired = {}
    for hop in HOPS:
        deltas = []
        for d0, rec in per_date.items():
            hi, lo = [], []
            for c, d in rec.items():
                g = d.get("g")
                if not is_num(g):
                    continue
                fr = fwd_ret(px, c, d0, hop, tdays, tidx)
                if fr is None:
                    continue
                if g > 7.0:
                    hi.append(fr)
                elif g <= 4.0:
                    lo.append(fr)
            # 两侧样本都要够厚，否则单截面噪声过大
            if len(hi) >= 20 and len(lo) >= 20:
                deltas.append(sum(hi) / len(hi) - sum(lo) / len(lo))
        if not deltas:
            paired[f"h{hop}"] = None
            continue
        m = sum(deltas) / len(deltas)
        sd = (sum((x - m) ** 2 for x in deltas) / (len(deltas) - 1)) ** 0.5 if len(deltas) > 1 else 0.0
        srt = sorted(deltas)
        med = srt[len(srt) // 2]
        t = nw_t(deltas, lag=4)
        win = sum(1 for x in deltas if x > 0) / len(deltas)
        paired[f"h{hop}"] = {"n_dates": len(deltas), "mean": m, "median": med,
                             "nw_t": t, "pos_rate": win, "sd": sd,
                             "t_naive": (m / (sd / len(deltas) ** 0.5)) if sd else None}
        print(f"{'h'+str(hop):>6} {len(deltas):>7} {m:+8.3f}% {med:+8.3f}% {t:+8.2f} "
              f"{win*100:9.1f}% {sd:9.3f}%")

    OUT["paired_diff_hi_minus_lo"] = paired

    # ─────────── ② 市值中性化后的配对差 ───────────
    # 每截面内先按「行业内市值分」五分组，组内再比成长档 → 同市值档内比成长
    print("\n" + "-" * 100)
    print("② 市值中性化：每截面内按行业内市值分五分组，组内再比「G>7 − G<=4」")
    print("-" * 100)
    neutral = {}
    for hop in HOPS:
        per_q = {q: [] for q in range(5)}
        for d0, rec in per_date.items():
            # 收集有市值分、成长分、前瞻收益的样本
            pool = []
            for c, d in rec.items():
                g, sz = d.get("g"), d.get("szir")
                if not is_num(g) or not is_num(sz):
                    continue
                fr = fwd_ret(px, c, d0, hop, tdays, tidx)
                if fr is None:
                    continue
                pool.append((sz, g, fr))
            if len(pool) < 100:
                continue
            pool.sort(key=lambda x: x[0])
            # ⚠️ szir（行业内市值分）=「低市值→高分」，所以 szir 升序 = 市值从大到小。
            #    因此 Q1 = 行业内市值最大组，Q5 = 最小组。标签必须带方向，否则结论会被读反。
            qn = len(pool) / 5.0
            for qi in range(5):
                lo_i, hi_i = int(qi * qn), int((qi + 1) * qn)
                seg = pool[lo_i:hi_i]
                ghi = [x[2] for x in seg if x[1] > 7.0]
                glo = [x[2] for x in seg if x[1] <= 4.0]
                if len(ghi) >= 10 and len(glo) >= 10:
                    per_q[qi].append(sum(ghi) / len(ghi) - sum(glo) / len(glo))
        rows = []
        for qi in range(5):
            arr = per_q[qi]
            if len(arr) < 20:
                rows.append({"q": qi, "n_dates": len(arr), "mean": None, "nw_t": None})
                continue
            m = sum(arr) / len(arr)
            rows.append({"q": qi, "n_dates": len(arr), "mean": m, "nw_t": nw_t(arr, lag=4)})
        neutral[f"h{hop}"] = rows
        # 汇总：五组 Δ 的加权平均
        allarr = [x for qi in range(5) for x in per_q[qi]]
        if allarr:
            mm = sum(allarr) / len(allarr)
            # ⚠️ 标签必须带方向：szir 越高=市值越小，故 Q1=最大市值、Q5=最小市值
            print(f"h{hop}: 五档合计 Δ={mm:+.3f}% (n={len(allarr)} 组-截面)")
            print("      " + " ".join(
                f"{_QLAB[r['q']]}:{r['mean']:+.2f}%(t{r['nw_t']:+.1f})"
                if r["mean"] is not None else f"{_QLAB[r['q']]}:—" for r in rows))
        else:
            print(f"h{hop}: 样本不足")

    OUT["by_mv_quintile"] = neutral

    # ─────────── ③ 成长分 rank IC 的稳定性 ───────────
    print("\n" + "-" * 100)
    print("③ 成长分 rank IC 时序稳定性（与 一之补 同口径，补 ICIR/胜率）")
    print("-" * 100)
    icstab = {}
    for hop in HOPS:
        ics = []
        for d0, rec in per_date.items():
            xs, ys = {}, {}
            for c, d in rec.items():
                g = d.get("g")
                if not is_num(g):
                    continue
                fr = fwd_ret(px, c, d0, hop, tdays, tidx)
                if fr is None:
                    continue
                xs[c] = g
                ys[c] = fr
            if len(xs) >= 50:
                # ⚠️ spearman 的入参是 dict、返回 (ic, n) 元组，不是标量
                ic, _n = spearman(xs, ys)
                if ic is not None:
                    ics.append(ic)
        if not ics:
            continue
        m = sum(ics) / len(ics)
        sd = (sum((x - m) ** 2 for x in ics) / (len(ics) - 1)) ** 0.5 if len(ics) > 1 else 0.0
        t = nw_t(ics, lag=4)
        icstab[f"h{hop}"] = {"n": len(ics), "ic": m, "t": t,
                             "icir": (m / sd) if sd else None,
                             "pos_rate": sum(1 for x in ics if x > 0) / len(ics)}
        print(f"h{hop}: IC={m:+.4f} t={t:+.2f} ICIR={(m/sd) if sd else 0:+.3f} "
              f"胜率={sum(1 for x in ics if x>0)/len(ics)*100:.1f}% n={len(ics)}")

    OUT["growth_ic_stability"] = icstab

    # ─────────── 裁决 ───────────
    print("\n" + "=" * 100)
    print("裁决：成长门限的「单调性」是真的吗？")
    print("=" * 100)
    verds = []
    for hop in HOPS:
        p = paired.get(f"h{hop}")
        if not p:
            continue
        tag = "显著" if abs(p["nw_t"]) >= 2.0 else "不显著"
        verds.append(f"  h{hop}: 配对 Δ={p['mean']:+.3f}%  NW t={p['nw_t']:+.2f} → {tag}"
                     f"（朴素 t 会误报为 {p['t_naive']:+.1f}，虚高 {abs(p['t_naive'])/max(abs(p['nw_t']),1e-9):.0f} 倍）"
                     if p["t_naive"] and p["nw_t"] else
                     f"  h{hop}: 配对 Δ={p['mean']:+.3f}%  NW t={p['nw_t']:+.2f} → {tag}")
    for v in verds:
        print(v)

    allns = all(abs(paired[f"h{h}"]["nw_t"]) < 2.0 for h in HOPS if paired.get(f"h{h}"))
    print()
    if allns:
        print("  ⇒ 配对检验下三个周期的 Δ 均未过 |t|≥2。")
        print("     round38 第二章「G>7 档前瞻收益更高」的单调性**是池化均值造成的假精度**，")
        print("     有效独立样本是 218 个截面而非 13 万个股票日，不能作为门限有效的证据。")
        print("     与 一之补 的结论（成长分 IC 不显著）一致 —— 两者不矛盾，是第二章此前算错了标准误。")
    else:
        print("  ⇒ 部分周期配对检验仍显著，需进一步排查。")

    OUT["verdict"] = {"all_paired_insignificant": allns,
                      "note": "池化均值 SE 用交易日数而非股票日数；本章为其修正"}
    OUT["meta"] = meta

    path = os.path.join(HERE, "audit_round38b.json")
    json.dump(OUT, open(path, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"\n📄 结果已落盘：{path}")


if __name__ == "__main__":
    main()
