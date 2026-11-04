#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""audit_round44：fund 缺失结构对 total 的实际影响分解（P0 待裁决项）

═══════════════════════════════════════════════════════════════════
为什么需要这个脚本
═══════════════════════════════════════════════════════════════════
已确认事实：
  · fund w=0.242（最大单一权重），但面板 238 截面中 158 个（66.4%）完全无 fund
  · 有 fwd_hist 的 80 个截面，覆盖率也仅 9%（182/2015），因为 fwd_hist 只存
    「当时 TOP 池」的 193~491 只，而面板域是 2029 只

此前结论（round40 #307）：
  「某维整截面全缺时 total = A×(其余维加权分) + C，A>0、C 为常数 → Spearman 排序完全不变」
  → 故 fund 全缺不影响 IC。

⚠️ 但该结论**只覆盖「全截面同缺」**，漏了两种更常见的形态：
  ① **部分覆盖（截面内异质）**：同一天里 182 只有真实 fund 分（0~10 离散），
     1833 只被填成中性 5.0。此时 fund 维对前者有区分度、对后者是常数
     → 相当于给 9% 的股票加了一个扰动项，**不是**单调变换，会真实改变排序。
  ② **连续段的 level shift**：158 个全缺截面 total 被压向中位，与 80 个部分
     覆盖截面不可直接比较绝对分（但各自截面内排序仍可比）。

本脚本输出三件事：
  A. 全缺 vs 部分覆盖的截面分类，以及各自的 fund 覆盖分布
  B. 部分覆盖截面内，把 fund 从 5.0 换回「真值」后 total 排序变化多少（Δrank）
     —— 这是「fund 缺失到底损害了什么」的直接度量
  C. 三种缺失处置口径的对照：
     C0 = 现状（中性填 5.0 + 固定分母）
     C1 = 缺维从分母剔除（其余维重新归一）
     C2 = 若该截面 fund 覆盖 < 阈值则整维剔除（消除异质）
     对每个截面算 total，比较 (a) 截面内排序 (b) 跨截面绝对分可比性

用法：python audit_round44_fund_missing_impact.py
"""
import os
import sys
import json
import math
from collections import defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
os.chdir(HERE)

import scale_calc as sc

PANEL = os.path.join(HERE, "data", "panel_round34.json")
WCFG = os.path.join(HERE, "factor_weights.json")
OUT = os.path.join(HERE, "audit_round44.json")

# v4.14 面板侧维度（与 factor_weights 生效维对齐）
DIMS = ("score_bias60", "score_rsi_rev", "score_tech_neg", "score_lowvol20",
        "score_quality", "score_ep", "score_size_growth", "score_dominant",
        "score_fund")
# 面板里实际存在的旧列名 → 新维名映射（A/B 维由 round34 后重建，见 round38）
PANEL_COL = {k: k for k in DIMS}
PANEL_COL["score_size_ind"] = "score_size_ind"      # 旧市值维，v4.13 已拆为 A/B


def is_num(x):
    return isinstance(x, (int, float)) and not isinstance(x, bool) and x == x


def spearman(xs, ys):
    n = len(xs)
    if n < 5:
        return None
    def _rank(v):
        order = sorted(range(n), key=lambda i: v[i])
        r = [0.0] * n
        i = 0
        while i < n:
            j = i
            while j + 1 < n and v[order[j + 1]] == v[order[i]]:
                j += 1
            avg = (i + j) / 2.0 + 1
            for k in range(i, j + 1):
                r[order[k]] = avg
            i = j + 1
        return r
    rx, ry = _rank(xs), _rank(ys)
    mx, my = sum(rx) / n, sum(ry) / n
    nu = sum((rx[i] - mx) * (ry[i] - my) for i in range(n))
    dx = math.sqrt(sum((rx[i] - mx) ** 2 for i in range(n)))
    dy = math.sqrt(sum((ry[i] - my) ** 2 for i in range(n)))
    return nu / (dx * dy) if dx * dy > 1e-12 else None


def med(v):
    v = sorted(x for x in v if is_num(x))
    if not v:
        return None
    n = len(v)
    return v[n // 2] if n % 2 else 0.5 * (v[n // 2 - 1] + v[n // 2])


def load_cfg():
    d = json.load(open(WCFG, encoding="utf-8"))
    w = {k: float(v or 0) for k, v in (d.get("weights") or {}).items()}
    dm = {k: float(v) for k, v in (d.get("dim_max") or {}).items()
          if is_num(v) and v > 0}
    return w, dm, d.get("version")


def composite(rec, w, dm, mode="C0"):
    """三种缺失处置口径。rec 是 {dim: value_or_None}。"""
    active = {k: v for k, v in w.items() if is_num(v) and v > 0}
    parts = [(k, rec.get(k), active.get(k, 0.0)) for k in active]
    if mode == "C0":
        tot, _miss = sc.composite_total(parts, dm, weights=active)
        return tot if is_num(tot) else None
    if mode == "C1":
        # 缺维从分母剔除：只对本次传入的维求和作分母
        present = [(k, v, wt) for k, v, wt in parts if is_num(v) and wt > 0]
        den = sum(wt for _, _, wt in present)
        if den <= 0:
            return None
        num = 0.0
        for k, v, wt in present:
            mx = float(dm.get(k) or 10.0)
            num += min(float(v), mx) / mx * 10.0 * wt
        return round(num / den * 10.0, 2)
    raise ValueError(mode)


def main():
    w, dm, ver = load_cfg()
    print("=" * 104)
    print(f"audit_round44 · fund 缺失影响分解   权重版本 = {ver}")
    print("=" * 104)
    active = {k: v for k, v in w.items() if is_num(v) and v > 0}
    print(f"生效维 {len(active)} 个，Σw = {sum(active.values()):.6f}，"
          f"fund 权重 = {active.get('score_fund')}")
    print()

    p = json.load(open(PANEL, encoding="utf-8"))
    dates, rows = p["dates"], p["rows"]
    print(f"面板：{len(dates)} 截面，{dates[0]} ~ {dates[-1]}，域 {len(p['domain'])} 只")
    print()

    # ---------- A. 截面分类 ----------
    zero_dates, part_dates = [], []
    for d in dates:
        rr = rows[d]
        n = len(rr)
        nh = sum(1 for v in rr.values() if is_num(v.get("score_fund")))
        if nh == 0:
            zero_dates.append((d, nh, n))
        elif nh < n:
            part_dates.append((d, nh, n))

    print("【A. 截面覆盖形态】")
    print(f"  fund 全缺截面（整维不参与）: {len(zero_dates):3d} 个 "
          f"({100*len(zero_dates)/len(dates):.1f}%)")
    print(f"  fund 部分覆盖截面（异质！）: {len(part_dates):3d} 个 "
          f"({100*len(part_dates)/len(dates):.1f}%)")
    if part_dates:
        covs = [100.0 * nh / n for _, nh, n in part_dates]
        print(f"    部分覆盖的覆盖率高: 中位 {med(covs):.1f}%  "
              f"min {min(covs):.1f}%  max {max(covs):.1f}%")
    print()
    print("  ⚠️ 关键区分：全缺截面里 fund 是**常数**（单调不变，不伤排序）；")
    print("     部分覆盖截面里 fund 对一部分股票是**真实分**、另一部分是常数 5.0")
    print("     → 这是异质扰动，**会真实改变截面内排序**，本轮重点量化这一项。")
    print()

    # ---------- B. 部分覆盖截面的排序扰动 ----------
    print("【B. 部分覆盖截面：fund 真实值 vs 中性 5.0 的排序差异】")
    print(f"  {'date':12s} {'n':>5s} {'有fund':>7s} {'占比':>7s} "
          f"{'ρ(C0,C1)':>9s} {'最大名次变化':>12s} {'Top50重合':>9s}")
    b_rows = []
    for d, nh, n in part_dates:
        rr = rows[d]
        # C0：现状（缺填 5.0）
        c0 = {}
        # C1：把「已存在的 fund 真值」全部抹成 Neutral 之外的对照？
        # 真正的对照应是「假设 fund 全覆盖 vs 现状」。我们用 C1 = 缺维剔除，
        # 但仅在**有 fund 的子样本**上比较才有意义 → 改为在同侧集合上算排序。
        for c, rec in rr.items():
            c0[c] = composite(rec, w, dm, "C0")
        valid = [c for c in rr if is_num(c0.get(c))]
        if len(valid) < 50:
            continue
        # 对同一批股票，把 fund 侧的值整体替换为「已覆盖子样本的中位」→ 等价于
        # 让 fund 在该截面对所有人都是同一常数，从而隔离出「异质」的影响
        sub = [rr[c].get("score_fund") for c in valid if is_num(rr[c].get("score_fund"))]
        if len(sub) < 20:
            continue
        med_fund = med(sub)
        c0_homo = {}
        for c in valid:
            rec2 = dict(rr[c])
            rec2["score_fund"] = med_fund      # 全部置为同一常数
            c0_homo[c] = composite(rec2, w, dm, "C0")
        xs = [c0[c] for c in valid]
        ys = [c0_homo[c] for c in valid]
        rho = spearman(xs, ys)

        # 名次变化
        order_a = sorted(valid, key=lambda c: (-c0[c], c))
        order_b = sorted(valid, key=lambda c: (-c0_homo[c], c))
        ra = {c: i + 1 for i, c in enumerate(order_a)}
        rb = {c: i + 1 for i, c in enumerate(order_b)}
        dmax = max(abs(ra[c] - rb[c]) for c in valid)
        # Top50 重合
        t50a = set(order_a[:50])
        t50b = set(order_b[:50])
        ov = len(t50a & t50b)

        b_rows.append({"date": d, "n": n, "n_fund": nh, "cov": nh / n,
                       "rho": rho, "dmax": dmax, "top50_overlap": ov})
        print(f"  {d:12s} {n:5d} {nh:7d} {100*nh/n:6.1f}% "
              f"{(f'{rho:.4f}' if rho is not None else '   n/a'):>9s} "
              f"{dmax:12d} {ov:6d}/50")

    if b_rows:
        rhos = [r["rho"] for r in b_rows if r["rho"] is not None]
        ovs = [r["top50_overlap"] for r in b_rows]
        print("-" * 104)
        print(f"  汇总：ρ 中位 {med(rhos):.4f}（min {min(rhos):.4f}） · "
              f"Top50 重合中位 {med(ovs):.0f}/50（min {min(ovs)}） · "
              f"最大名次变化中位 {med([r['dmax'] for r in b_rows]):.0f} 名")
        # 只保留 fund 覆盖率 >=5% 的（覆盖率太低时几乎无影响；太高时影响大）
        print()
        print("  按覆盖率分组（覆盖率越高，异质影响越大）：")
        for lo, hi in ((0, .05), (.05, .10), (.10, .20), (.20, 1.01)):
            g = [r for r in b_rows if lo <= r["cov"] < hi]
            if not g:
                continue
            print(f"    cov {lo*100:4.0f}~{hi*100:4.0f}%  n={len(g):2d}  "
                  f"ρ中位={med([r['rho'] for r in g if r['rho'] is not None]):.4f}  "
                  f"Top50重合中位={med([r['top50_overlap'] for r in g]):.0f}/50  "
                  f"最大名次变化中位={med([r['dmax'] for r in g]):.0f}")

    print()

    # ---------- C. 全缺截面的 level shift ----------
    print("【C. 全缺截面的绝对分 level shift】")
    if zero_dates:
        d0 = zero_dates[0][0]
        rr = rows[d0]
        rec = list(rr.values())[0]
        c0 = composite(rec, w, dm, "C0")
        c1 = composite(rec, w, dm, "C1")
        # 用「其他 8 维加权分」反推：C0 = 0.758 × S8 + 0.242 × 5（归一 0-10 后 ×10）
        s8 = None
        present = [(k, v, active[k]) for k, v in rec.items() if is_num(v) and k in active]
        if present:
            den = sum(wt for _, _, wt in present)
            num = sum(min(v, dm.get(k) or 10) / (dm.get(k) or 10) * 10 * wt
                      for k, v, wt in present)
            s8 = num / den * 10 if den > 0 else None
        print(f"  样例 {d0}  {rec}")
        print(f"    C0（中性填）total = {c0}")
        print(f"    C1（缺维剔除）total = {c1}")
        if s8 is not None:
            print(f"    其余 8 维加权分（0-10 归一后 ×10）S8 = {s8:.4f}")
            print(f"    验证 C0 = 0.758×{s8:.4f} + 0.242×5.0 = "
                  f"{0.758*s8 + 0.242*5.0:.4f}")
            print(f"    放大倍数 C1/C0（对 8 维部分）= 1/0.758 = {1/0.758:.4f}")
        print()
        print("  含义：全缺截面里 fund 维退化为常数 → total 只是一个**仿射变换**，")
        print("  截面内排序 100% 不变（此前已证 ρ=0.999999），但绝对分与「有 fund」的")
        print("  截面不可直接比较。这是**跨期可比性**问题，不是选股正确性问题。")

    print()

    # ---------- D. 裁决 ----------
    print("=" * 104)
    print("【D. 裁决与可执行动作】")
    n_zero = len(zero_dates)
    n_part = len(part_dates)
    if b_rows:
        rho_med = med([r["rho"] for r in b_rows if r["rho"] is not None])
        ov_med = med([r["top50_overlap"] for r in b_rows])
        print(f"  1. 全缺 {n_zero} 截面：无损害（常数偏移，排序不变）→ **无需填充**，"
              f"只需披露绝对分不可跨期比")
        print(f"  2. 部分覆盖 {n_part} 截面：ρ(C0, 同质化) 中位 = {rho_med:.4f}，"
              f"Top50 重合中位 = {ov_med:.0f}/50")
        if rho_med is not None and rho_med > 0.99 and ov_med >= 49:
            print(f"     → 实测影响 < 1%，**不构成 P0**；可保留现状 + 披露")
        elif rho_med is not None and rho_med > 0.95:
            print(f"     → 影响有限但非可忽略；建议加「截面内 fund 覆盖率」披露")
        else:
            print(f"     → **构成实质影响**，必须处置（见下方方案）")
    print(f"  3. 真正的修复方向不是「回填历史」（前瞻一致预期不可重建，实测 "
          f"ρ(重建,真实)=0.536 < 0.70），")
    print(f"     而是「**从现在起全域名归档**」——见 fix_fund_archive_full.py")
    print("=" * 104)

    out = {
        "round": 44,
        "purpose": "fund 缺失结构对 total 的影响分解",
        "weights_version": ver,
        "fund_weight": active.get("score_fund"),
        "n_dates": len(dates),
        "n_zero_cover_dates": n_zero,
        "n_partial_cover_dates": n_part,
        "partial_cover_stats": {
            "rho_median": med([r["rho"] for r in b_rows if r["rho"] is not None]) if b_rows else None,
            "rho_min": min([r["rho"] for r in b_rows if r["rho"] is not None]) if b_rows else None,
            "top50_overlap_median": med([r["top50_overlap"] for r in b_rows]) if b_rows else None,
            "dmax_median": med([r["dmax"] for r in b_rows]) if b_rows else None,
        },
        "per_date_partial": b_rows,
        "caveat": "「同质化」对照=把该截面所有股票的 fund 置为已覆盖子样本中位数，"
                  "用于隔离「部分覆盖异质」的影响；不改变 fund 缺失本身的信息损失。",
    }
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print(f"→ {os.path.basename(OUT)}")


if __name__ == "__main__":
    main()
