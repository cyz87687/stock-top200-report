#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""audit_round45：部分覆盖的选择偏差检验 —— fund「有值」本身是否成为选股信号

═══════════════════════════════════════════════════════════════════
问题陈述
═══════════════════════════════════════════════════════════════════
round44 发现：部分覆盖截面（80 个）里，把 fund 全部置为同一常数后，
Top50 重合从 40/50（覆盖 9%）塌到 17/50（覆盖 22%）。覆盖率越高，扰动越大。

这不符合直觉 —— 覆盖率越高应该越接近真值。唯一解释是：
**「该股在当日 TOP 池内」这个事实本身被编码进了 fund 维**。
  · 在池内 → fund 有真实分（0~10 差异化）
  · 不在池内 → fund 被填 5.0（常数）
→ fund 维实际测的是「是否在池 + 池内估值好坏」的混合体。

若「是否在池」与未来收益相关，则 fund 维不是纯粹的估值因子。
项目既有结论（round29）：入池指示 IC(h) = **−0.1492**（负！）

本脚本检验三件事：
  A. 验证「fund 有值」与「在池」是否等价（应该 ~100%，因为数据同源）
  B. 直接测「fund 有值指示变量」的 Rank IC —— 若显著非零，则确认污染
  C. 三种权重落地口径的 IC 对照：
     M0 = 现状（缺填中性 5.0）
     M1 = 低覆盖截面整维剔除（覆盖率 < 阈值 → 该截面 fund 不计分）
     M2 = 全样本整维剔除（fund 完全不参与，作为下界参考）
     对每个口径算复合分，再算 h1/h5/h20 的 Rank IC

用法：python audit_round45_fund_selection_bias.py
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
KLONG = os.path.join(HERE, "data", "klines_long.json")
WCFG = os.path.join(HERE, "factor_weights.json")
OUT = os.path.join(HERE, "audit_round45.json")

HOPS = (1, 5, 20)
NW_LAG = 4


def is_num(x):
    return isinstance(x, (int, float)) and not isinstance(x, bool) and x == x


def spearman(xs, ys):
    n = len(xs)
    if n < 10:
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


def nw_t(xs, lag=NW_LAG):
    n = len(xs)
    if n < 10:
        return None
    m = sum(xs) / n
    d = [x - m for x in xs]
    g0 = sum(v * v for v in d) / n
    s = g0
    for k in range(1, min(lag, n - 1) + 1):
        gk = sum(d[i] * d[i - k] for i in range(k, n)) / n
        s += 2 * (1 - k / (lag + 1)) * gk
    if s <= 0:
        return None
    return m / math.sqrt(s / n)


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


def load_px():
    raw = json.load(open(KLONG, encoding="utf-8"))
    px = {}
    for c, m in raw.items():
        c6 = str(c)[-6:]
        if len(c6) == 6 and c6.isdigit():
            px[c6] = {d: v[1] for d, v in m.items()
                      if v and len(v) >= 2 and is_num(v[1]) and v[1] > 0}
    return px


def main():
    w, dm, ver = load_cfg()
    active = {k: v for k, v in w.items() if is_num(v) and v > 0}
    print("=" * 106)
    print(f"audit_round45 · fund 部分覆盖的选择偏差检验   权重 = {ver}")
    print("=" * 106)
    print(f"生效维 {len(active)}，Σw={sum(active.values()):.6f}，"
          f"fund w={active.get('score_fund')}")
    print()

    p = json.load(open(PANEL, encoding="utf-8"))
    dates, rows = p["dates"], p["rows"]
    px = load_px()
    tidx = {d: i for i, d in enumerate(dates)}

    # ---------- A. 「fund 有值」≡「在池」 ----------
    print("【A. 验证「fund 有值」是否等价于「在池」】")
    n_have = n_has_val = 0
    for d in dates:
        rr = rows[d]
        n_has_val += sum(1 for v in rr.values() if is_num(v.get("score_fund")))
        n_have += len(rr)
    print(f"  面板总单元格 {n_have}，fund 有值 {n_has_val} ({100*n_has_val/n_have:.2f}%)")
    print(f"  fwd_hist 数据源每日仅 193~491 行，面板域 2029 只")
    print(f"  ⇒ 「fund 有值」⊂「当日被 fwd_hist 归档」⊂「当日在 TOP 池内」")
    print(f"  故 fund 有值指示变量 ≡ 入池指示变量（数据同源，无法分离）")
    print()

    # ---------- B. 「fund 有值」指示变量的 IC ----------
    print("【B. 「fund 有值」指示变量的 Rank IC（若显著非零 → 确认污染）】")
    print(f"  {'hop':>4s} {'n截面':>7s} {'IC均值':>9s} {'NW t':>7s} {'胜率':>7s}")
    b_out = {}
    for hop in HOPS:
        ics = []
        for d in dates:
            i0 = tidx.get(d)
            if i0 is None or i0 + hop >= len(dates):
                continue
            d1 = dates[i0 + hop]
            rr = rows[d]
            idx, ret = [], []
            for c, rec in rr.items():
                sflag = 1.0 if is_num(rec.get("score_fund")) else 0.0
                m = px.get(c)
                if not m:
                    continue
                p0, p1 = m.get(d), m.get(d1)
                if not (is_num(p0) and is_num(p1) and p0 > 0):
                    continue
                idx.append(sflag)
                ret.append(p1 / p0 - 1.0)
            if len(idx) < 100:
                continue
            # 指示变量只有 0/1，Spearman 与点二列秩等价
            rho = spearman(idx, ret)
            if rho is not None:
                ics.append(rho)
        if ics:
            m = sum(ics) / len(ics)
            t = nw_t(ics)
            pos = sum(1 for x in ics if x > 0) / len(ics)
            b_out[f"h{hop}"] = {"n_dates": len(ics), "ic": m, "nw_t": t,
                                "pos_rate": pos}
            print(f"  {hop:4d} {len(ics):7d} {m:+9.4f} "
                  f"{(f'{t:+.2f}' if t is not None else '   n/a'):>7s} {pos:6.1%}")
    print()
    print("  判读：|t| ≥ 1.96 → 「是否在池」本身带了收益信息，")
    print("        fund 维就不再是纯估值因子，而是「估值 × 入池」的混合体。")
    print()

    # ---------- C. 三种口径的复合分 IC ----------
    print("【C. 三种权重落地口径的复合分 Rank IC】")
    THR = 0.50
    def build_total(mode):
        """返回 {date: {code: total}}"""
        out = {}
        for d in dates:
            rr = rows[d]
            n = len(rr)
            nh = sum(1 for v in rr.values() if is_num(v.get("score_fund")))
            cov = nh / max(1, n)
            cur = {}
            for c, rec in rr.items():
                use = dict(rec)
                if mode == "M1" and cov < THR:
                    use["score_fund"] = None      # 低覆盖 → 整维剔除
                if mode == "M2":
                    use["score_fund"] = None      # 全样本剔除
                if mode == "M1" and cov < THR:
                    # 剔除该维后，其余维重新归一（缺维从分母剔除）
                    present = [(k, v, active[k]) for k, v in use.items()
                               if k in active and is_num(v)]
                    den = sum(wt for _, _, wt in present)
                    if den <= 0:
                        continue
                    num = sum(min(v, dm.get(k) or 10) / (dm.get(k) or 10) * 10 * wt
                              for k, v, wt in present)
                    cur[c] = num / den * 10
                else:
                    parts = [(k, use.get(k), active.get(k, 0.0)) for k in active]
                    tot, _ = sc.composite_total(parts, dm, weights=active)
                    if is_num(tot):
                        cur[c] = tot
            out[d] = cur
        return out

    tot_maps = {m: build_total(m) for m in ("M0", "M1", "M2")}

    labels = {"M0": "现状（缺填中性 5.0）",
              "M1": f"低覆盖(<{THR:.0%})整维剔除",
              "M2": "全样本剔除 fund（下界参考）"}
    c_out = {}
    print(f"  {'口径':26s} {'hop':>4s} {'n截面':>7s} {'IC均值':>9s} {'NW t':>7s} {'胜率':>7s}")
    for mode in ("M0", "M1", "M2"):
        tm = tot_maps[mode]
        c_out[mode] = {}
        for hop in HOPS:
            ics = []
            for d in dates:
                i0 = tidx.get(d)
                if i0 is None or i0 + hop >= len(dates):
                    continue
                d1 = dates[i0 + hop]
                cur = tm.get(d) or {}
                xs, ys = [], []
                for c, tot in cur.items():
                    m = px.get(c)
                    if not m:
                        continue
                    p0, p1 = m.get(d), m.get(d1)
                    if not (is_num(p0) and is_num(p1) and p0 > 0):
                        continue
                    xs.append(tot)
                    ys.append(p1 / p0 - 1.0)
                if len(xs) < 100:
                    continue
                rho = spearman(xs, ys)
                if rho is not None:
                    ics.append(rho)
            if ics:
                m = sum(ics) / len(ics)
                t = nw_t(ics)
                pos = sum(1 for x in ics if x > 0) / len(ics)
                c_out[mode][f"h{hop}"] = {"n_dates": len(ics), "ic": m,
                                          "nw_t": t, "pos_rate": pos}
                print(f"  {labels[mode]:26s} {hop:4d} {len(ics):7d} {m:+9.4f} "
                      f"{(f'{t:+.2f}' if t is not None else '   n/a'):>7s} {pos:6.1%}")
        print()

    # 配对差
    print("【D. 配对差（同截面集合，消除截面构成差异）】")
    paired_out = {}
    for a, b in (("M1", "M0"), ("M2", "M0")):
        paired_out[f"{a}-{b}"] = {}
        for hop in HOPS:
            diffs = []
            for d in dates:
                i0 = tidx.get(d)
                if i0 is None or i0 + hop >= len(dates):
                    continue
                d1 = dates[i0 + hop]
                ca = tot_maps[a].get(d) or {}
                cb = tot_maps[b].get(d) or {}
                common = set(ca) & set(cb)
                if len(common) < 100:
                    continue
                xs, ys = [], []
                for c in common:
                    m = px.get(c)
                    if not m:
                        continue
                    p0, p1 = m.get(d), m.get(d1)
                    if not (is_num(p0) and is_num(p1) and p0 > 0):
                        continue
                    xs.append(ca[c])
                    ys.append(p1 / p0 - 1.0)
                if len(xs) < 100:
                    continue
                r_a = spearman(xs, ys)
                xs2 = [cb[c] for c in common if c in set(cb)]
                # 用同一组股票
                xs2, ys2 = [], []
                for c in common:
                    m = px.get(c)
                    if not m:
                        continue
                    p0, p1 = m.get(d), m.get(d1)
                    if not (is_num(p0) and is_num(p1) and p0 > 0):
                        continue
                    xs2.append(cb[c])
                    ys2.append(p1 / p0 - 1.0)
                r_b = spearman(xs2, ys2)
                if r_a is not None and r_b is not None:
                    diffs.append(r_a - r_b)
            if diffs:
                m = sum(diffs) / len(diffs)
                t = nw_t(diffs)
                v = ("✅ 改善" if (m > 0 and t is not None and abs(t) >= 1.96)
                     else ("❌ 恶化" if (m < 0 and t is not None and abs(t) >= 1.96)
                           else "— 不显著"))
                paired_out[f"{a}-{b}"][f"h{hop}"] = {
                    "n_dates": len(diffs), "delta_ic": m, "nw_t": t,
                    "verdict": ("improve" if (m > 0 and t is not None and abs(t) >= 1.96)
                                else ("worsen" if (m < 0 and t is not None and abs(t) >= 1.96)
                                      else "not_significant")),
                }
                print(f"  {a} − {b}  h{hop:<2d}  n={len(diffs):3d}  ΔIC={m:+.4f}  "
                      f"NW t={(f'{t:+.2f}' if t is not None else 'n/a'):>7s}  {v}")

    print()
    print("=" * 106)
    print("【E. 裁决】")
    # 自动裁决
    verdict = "hold"
    if "h20" in c_out.get("M1", {}):
        d20 = c_out["M1"]["h20"]["ic"] - c_out["M0"]["h20"]["ic"]
        print(f"  M1 vs M0 的 h20 IC 差 = {d20:+.4f}（见上方配对 t）")
    print("  见控制台配对结论；tainted 判定需 |ΔIC| 显著且方向明确。")
    print("=" * 106)

    out = {
        "round": 45,
        "purpose": "fund 部分覆盖的选择偏差检验 + 三种权重落地口径 IC 对照",
        "weights_version": ver,
        "fund_weight": active.get("score_fund"),
        "coverage_threshold_M1": THR,
        "A_coverage": {"cells": n_have, "with_fund": n_has_val,
                       "pct": 100 * n_has_val / max(1, n_have)},
        "B_inpool_indicator_ic": b_out,
        "C_composite_ic": c_out,
        "paired_delta": paired_out,
        "caveat": "指示变量=1 当且仅当该股当日 fund 有值；由于 fwd_hist 只归档当时的 "
                  "TOP 池，该指示变量与「入池」不可分离。",
    }
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print(f"→ {os.path.basename(OUT)}")


if __name__ == "__main__":
    main()
