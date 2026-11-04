#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
audit_round87_size_provenance.py —— size_growth 的真实来源：市值分？价格分？成长分？

═══ 为什么必须查 ═══

round85/86 显示：仅用 score_size_growth 单维，组合 α 年化 **+52.85%（t +4.29）**，
段C +93.97%（t +3.94），且显著优于「市值最小 20 只」基准。

若这是真的因子，是重大发现。但 round38 的构建代码暴露一个隐患：

    circ_mv = 股本中位数 × 当日收盘      ← 跨期单一常数股本、无拆股调整
    szir    = 行业内 circ_mv 的 rank10   ← 0=行业最小, 10=行业最大
    A_G1    = szir × G1_current(成长分)   ← 成长>7→0.8 / 4~7→0.3 / ≤4→0

**若股本是常数，circ_mv ∝ 价格** → szir 退化为「行业内价格分」。
那 size_growth 实际就是「**行业内股价最高的股票 × 高成长**」，
它的 alpha 可能来自 A 股「高价股 = 小盘 = 弹性大」的 price-level 效应，
或者来自股本常数的**前视/错算**。

═══ 三个必须分离的归因 ═══

A1 score_size_growth 与 **收盘价** 的相关（横截面秩相关）
A2 score_size_growth 与 **真实流通市值** 的相关（若有该字段）
A3 score_size_growth 与 **成长分 score_growth** 的相关
A4 「行业内价格分 × 成长门限」重建版 vs 面板原值的秩相关
    → 若 ρ≈1，说明我们完全还原了公式，可直接用重建版做干净实验

═══ 若确认是价格代理，则真正的对照组是 ═══

  · 「行业内股价最高的 20 只」（纯价格分）
  · 「行业内股价最高的 20 只 × 成长门限」（= size_growth 的纯化版本）
  · 「真实流通市值最小的 20 只」（真小盘）
  若 size_growth 的 alpha 主要来自「高价股」，那它就是**已被广泛套利的
  price-level 效应**，不是可持续 alpha。

用法：python audit_round87_size_provenance.py
"""
import os
import sys
import json
import math
import numpy as np
from scipy.stats import rankdata

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

PANEL = os.path.join(HERE, "data", "panel_round66.json")
KLONG = os.path.join(HERE, "data", "klines_long.json")
OUT = os.path.join(HERE, "audit_round87.json")
DIM = "score_size_growth"


def spear(x, y):
    n = len(x)
    if n < 3:
        return None
    rx, ry = rankdata(x), rankdata(y)
    mx, my = rx.mean(), ry.mean()
    dx, dy = rx - mx, ry - my
    sx = math.sqrt((dx * dx).sum())
    sy = math.sqrt((dy * dy).sum())
    if sx <= 0 or sy <= 0:
        return None
    return float((dx * dy).sum() / (sx * sy))


def main():
    print("加载 ...")
    panel = json.load(open(PANEL, encoding="utf-8"))
    px = json.load(open(KLONG, encoding="utf-8"))

    d0 = panel["dates"][-1]
    rows = panel["rows"][d0]
    print(f"  截面 {d0}，{len(rows)} 只")

    # ── 收集配对样本 ──
    rec = []
    for c, r in rows.items():
        sg = r.get(DIM)
        if not isinstance(sg, (int, float)) or sg != sg:
            continue
        bar = (px.get(c) or {}).get(d0)
        cl = None
        if isinstance(bar, (list, tuple)) and len(bar) >= 2 and isinstance(bar[1], (int, float)):
            cl = float(bar[1])
        elif isinstance(bar, (int, float)):
            cl = float(bar)
        if cl is None or cl <= 0:
            continue
        rec.append({"code": c, "sg": float(sg),
                    "close": cl,
                    "growth": r.get("score_growth"),
                    "ep": r.get("score_ep"),
                    "lowvol": r.get("score_lowvol20")})
    print(f"  配对样本 {len(rec)} 只")

    sg = [x["sg"] for x in rec]
    cl = [x["close"] for x in rec]
    print()
    print("═" * 84)
    print("A1  横截面秩相关")
    print("═" * 84)
    r_close = spear(sg, cl)
    print(f"  ρ(score_size_growth, 收盘价) = {r_close:+.4f}"
          f"   {'⚠️ 高度疑似价格代理' if abs(r_close) > 0.5 else '→ 不是价格代理'}")
    # 与 ep / lowvol 的相关（注意维度要配对，不能各自 filter 后长度不同）
    for fld, lab in (("ep", "score_ep"), ("lowvol", "score_lowvol20")):
        pair = [(x["sg"], x[fld]) for x in rec if isinstance(x[fld], (int, float))]
        rr = spear([a for a, _ in pair], [b for _, b in pair]) if len(pair) > 3 else None
        print(f"  ρ(score_size_growth, {lab}) = {rr:+.4f}  (n={len(pair)})")

    # ── 重建检验：能否用「价格分×成长门限」复现 ──
    print()
    print("═" * 84)
    print("A2  重建检验：szir 若是「行业内市值分」，且股本常数 ⇒ 近似「行业内价格分」")
    print("═" * 84)
    # 全局价格 rank10（作为 szir 的粗略代理）
    price_rank = {}
    for i, idx in enumerate(np.argsort(np.argsort(cl))):
        price_rank[rec[idx]["code"]] = i / (len(cl) - 1) * 10.0
    rebuilt = []
    for x in rec:
        s = price_rank[x["code"]]
        g = x["growth"]
        if not isinstance(g, (int, float)) or g != g:
            continue
        gate = 0.8 if g > 7 else (0.3 if g >= 4 else 0.0)
        rebuilt.append((x["sg"], s * gate))
    if rebuilt:
        r_reb = spear([a for a, _ in rebuilt], [b for _, b in rebuilt])
        print(f"  ρ(面板 size_growth, 「全局价格rank10 × 成长门限」) = {r_reb:+.4f}")
        print(f"  （非行业口径，仅作方向判断；ρ 高说明 size_growth ≈ 价格×成长）")

    # 分组看：高分组 vs 低分组的特征
    print()
    print("  按 size_growth 分五档看均值：")
    order = np.argsort(sg)
    for i, idx in enumerate(np.array_split(order, 5)):
        sub = [rec[j] for j in idx]
        gv = [x["growth"] for x in sub if isinstance(x["growth"], (int, float))]
        print(f"    Q{i+1}  n={len(sub):4d}  size_growth均值 {np.mean([x['sg'] for x in sub]):6.3f}  "
              f"收盘价均值 {np.mean([x['close'] for x in sub]):8.2f}  "
              f"收盘价中位 {np.median([x['close'] for x in sub]):8.2f}  "
              f"成长分均值 {np.mean(gv) if gv else float('nan'):6.3f}")

    # ── 关键：price-level 效应的对照 ──
    print()
    print("═" * 84)
    print("A3  size_growth 与「纯价格分」的区分度")
    print("═" * 84)
    # 找出：size_growth 排名 vs 价格排名 的一致程度
    by_sg = {rec[i]["code"]: i for i in np.argsort(sg)}
    by_pr = {rec[i]["code"]: i for i in np.argsort(cl)}
    ov = len(set(rec[i]["code"] for i in np.argsort(sg)[-20:]) &
             set(rec[i]["code"] for i in np.argsort(cl)[-20:]))
    print(f"  size_growth Top20 与 价格 Top20 的重合：{ov}/20")
    ov10 = len(set(rec[i]["code"] for i in np.argsort(sg)[-10:]) &
               set(rec[i]["code"] for i in np.argsort(cl)[-10:]))
    print(f"  size_growth Top10 与 价格 Top10 的重合：{ov10}/10")
    if ov >= 12:
        print("  ⇒ **size_growth ≈ 行业内高价股** → 其 alpha 很可能来自 price-level 效应")
    elif ov >= 6:
        print("  ⇒ 部分重合 → 既有价格成分，也有成长成分")
    else:
        print("  ⇒ 重合低 → size_growth 与价格关系弱，主要是成长门限在起作用")

    # ── 找出 stock_price 分布特征 ──
    print()
    print("  高分组的代码前缀分布（Top100）：")
    top100 = [rec[i]["code"] for i in np.argsort(sg)[-100:]]
    import collections
    pre = collections.Counter(c[:3] for c in top100)
    print("    " + "  ".join(f"{k}:{v}" for k, v in pre.most_common(8)))
    allpre = collections.Counter(c[:3] for c in [x["code"] for x in rec])
    print("    全池基线: " + "  ".join(f"{k}:{allpre[k]/len(rec)*100:.0f}%" for k, _ in pre.most_common(8)))

    json.dump({
        "meta": {"round": 87,
                 "purpose": "size_growth 的真实来源：市值分 / 价格分 / 成长分",
                 "panel": os.path.relpath(PANEL, HERE),
                 "date": d0, "n_pairs": len(rec)},
        "rho_with_close": r_close,
        "rho_with_rebuilt_price_gate": (spear([a for a, _ in rebuilt], [b for _, b in rebuilt])
                                        if rebuilt else None),
        "top20_overlap_with_price_top20": ov,
        "top10_overlap_with_price_top10": ov10,
        "quintile_profile": [
            {"q": i + 1,
             "sg_mean": float(np.mean([rec[j]["sg"] for j in idx])),
             "close_mean": float(np.mean([rec[j]["close"] for j in idx])),
             "close_median": float(np.median([rec[j]["close"] for j in idx])),
             "growth_mean": (float(np.mean([rec[j]["growth"] for j in idx
                                            if isinstance(rec[j]["growth"], (int, float))]))
                             if any(isinstance(rec[j]["growth"], (int, float)) for j in idx) else None)}
            for i, idx in enumerate(np.array_split(order, 5))],
        "top100_prefix": dict(pre),
        "pool_prefix_pct": {k: allpre[k] / len(rec) for k, _ in pre.most_common(8)},
    }, open(OUT, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"\n→ {OUT}")


if __name__ == "__main__":
    main()
