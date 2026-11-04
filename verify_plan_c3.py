#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
verify_plan_c3.py —— 第三层验证：判定「该选哪个再分配方案」，并锁定执行方案

前两层验证的关键结论（已有数据）：
  · Q0 全样本(233/218 截面)：A +0.0602/+0.0778 → B(裁决比例,Σ=1) +0.0648/+0.0912
  · Q1 拆分：A 在「有 fund 段」h20 +0.2786，在「无 fund 段」h20 -0.0021
    → 两段日期完全不重叠(有fund:2026-06-26~10-23 / 无fund:以 2025-11~2026-06-25 为主)
    → 差异混了 regime，**不能**用来评判方案优劣
  · Q2 同质(62 截面)：F +0.3023 > E +0.2976 > D +0.2945 > B +0.2896 > A +0.2786 > C +0.2497
    → **C（我上轮推荐的）在同质样本里最差**
  · Q4 同截面内「是否被 fund 覆盖」IC = -0.1088/-0.1792(t -8.3/-16.6)
    → fund 覆盖 = 当日 TOP200 名单，而 TOP200 被高权重的反转因子筛成「超跌股」
    → **fund 子样本本身带选样偏差，不能用来择优权重**
  · Q5 Σ 归一化：IC 变化不大（C: 0.2497→0.2156；F: 0.3023→0.2843），但
    **Σ=0.365 不归一会把满分上限压到 73，S 档(≥85)永不可达**

本脚本要给出的最终答案：
  Q7 regime 拆解：把全样本按时间切成两段，看各方案是否两段都成立（防 regime 依赖）
  Q8 全样本分半稳定性：A vs B vs C 谁稳
  Q9 覆盖率惩罚：B 只用 4 维、C 用 4 维，但在「无 fund」截面上它们实际只跑 3 维，
     分数退化到什么程度（量化「模型在两套行为之间跳变」的风险）
  → 结论：选 B（严格按裁决比例 + 归一到 Σ=1），不选 C

输出：verify_plan_c3.json
"""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import factor_eval as fe                      # noqa: E402
from audit_round29 import nw_t                # noqa: E402

DMAX = {"score_fund": 5.0, "score_bias60": 5.0, "score_rsi_rev": 5.0,
        "score_quality": 10.0, "score_lowvol20": 10.0, "score_size_ind": 10.0,
        "score_tech_neg": 10.0, "score_ep": 10.0}
W8 = {"score_bias60": .175, "score_rsi_rev": .175, "score_tech_neg": .15,
      "score_lowvol20": .13, "score_quality": .12, "score_size_ind": .10,
      "score_ep": .075, "score_fund": .075}
TOT_DEC = .13 + .10 + .06 + .075     # = 0.365


def _num(x):
    return isinstance(x, (int, float)) and not isinstance(x, bool)


def prod_score(vals, wmap):
    num = den = 0.0
    for k, w in wmap.items():
        if w <= 0:
            continue
        v = vals.get(k)
        if not _num(v):
            continue
        mx = DMAX.get(k) or 10.0
        num += min(float(v), mx) / mx * 10.0 * w
        den += w
    return (num / max(0.5, den) * 10.0) if den > 0 else None


def load_px():
    raw = json.load(open(os.path.join(HERE, "data", "klines_long.json"), encoding="utf-8"))
    px = {}
    for c, m in raw.items():
        c6 = str(c)[-6:]
        if len(c6) == 6 and c6.isdigit():
            px[c6] = {d: v[1] for d, v in m.items()
                      if v and len(v) >= 2 and _num(v[1]) and v[1] > 0}
    return px


def main():
    print("加载面板 / 价格 ...")
    panel = json.load(open(os.path.join(HERE, "data", "panel_round34.json"), encoding="utf-8"))
    dates, rows = panel["dates"], panel["rows"]
    px = load_px()
    tdays = sorted({d for m in px.values() for d in m})
    tidx = {d: i for i, d in enumerate(tdays)}

    PLANS = {
        "A 现行8维": dict(W8),
        "B 裁决比例(Σ=1)": {"score_lowvol20": .13 / TOT_DEC, "score_size_ind": .10 / TOT_DEC,
                        "score_quality": .06 / TOT_DEC, "score_fund": .075 / TOT_DEC},
        "C 4维等权(Σ=1)": {"score_lowvol20": .25, "score_size_ind": .25,
                        "score_quality": .25, "score_fund": .25},
    }

    def series(wmap, hop, keep=None):
        """返回 [(date, ic, mean_den_ratio)]；keep(d0,rd)->bool 可过滤截面。

        mean_den_ratio = 该截面实际参与加权和 / 计划权重和（衡量分数退化）。
        """
        tot_w = sum(w for w in wmap.values() if w > 0)
        out = []
        for d0 in dates:
            i0 = tidx.get(d0)
            if i0 is None or i0 + hop >= len(tdays):
                continue
            rd = rows.get(d0) or {}
            if keep and not keep(d0, rd):
                continue
            xs, ys, ratios = [], [], []
            for c, v in rd.items():
                if not _num(v.get("score_size_ind")):
                    continue
                s = prod_score(v, wmap)
                if s is None:
                    continue
                m = px.get(c)
                if not m:
                    continue
                p0, p1 = m.get(d0), m.get(tdays[i0 + hop])
                if not p0 or not p1:
                    continue
                xs.append(s)
                ys.append(p1 / p0 - 1.0)
                dsum = sum(w for k, w in wmap.items()
                           if w > 0 and _num(v.get(k)))
                ratios.append(dsum / tot_w if tot_w > 0 else 0.0)
            if len(xs) >= 40:
                ic = fe.rank_ic(xs, ys)
                if ic is not None:
                    out.append((d0, ic, sum(ratios) / len(ratios)))
        return out

    RES = {}

    # ── Q7：按时间切两段的 regime 检验
    SPLIT = "2026-06-26"
    print("\n" + "=" * 104)
    print(f"【Q7】regime 拆解：以 {SPLIT}（fund 数据起点）为界，看各方案是否两段都成立")
    print("=" * 104)
    print(f"  {'方案':20s} {'段':>6s} {'n':>5s} {'h5 IC':>9s} {'h20 IC':>9s}")
    q7 = {}
    for nm, wm in PLANS.items():
        for lbl, keep in (("前段", lambda d, r: d < SPLIT),
                          ("后段", lambda d, r: d >= SPLIT)):
            a5 = series(wm, 5, keep)
            a20 = series(wm, 20, keep)
            m5 = sum(x[1] for x in a5) / len(a5) if a5 else None
            m20 = sum(x[1] for x in a20) / len(a20) if a20 else None
            q7.setdefault(nm, {})[lbl] = {
                "n5": len(a5), "h5": round(m5, 4) if m5 is not None else None,
                "n20": len(a20), "h20": round(m20, 4) if m20 is not None else None}
            print(f"  {nm:20s} {lbl:>6s} {len(a20):5d} "
                  f"{(m5 if m5 is not None else 0):+9.4f} {(m20 if m20 is not None else 0):+9.4f}")
    RES["Q7_regime"] = q7

    # ── Q8：全样本分半稳定性
    print("\n" + "=" * 104)
    print("【Q8】全样本分半稳定性（h20，看方向是否两半都为正）")
    print("=" * 104)
    q8 = {}
    for nm, wm in PLANS.items():
        a20 = series(wm, 20)
        ics = [x[1] for x in a20]
        h = len(ics) // 2
        f = sum(ics[:h]) / h if h else None
        s = sum(ics[h:]) / (len(ics) - h) if len(ics) > h else None
        both = bool(f is not None and s is not None and f > 0 and s > 0)
        q8[nm] = {"first": round(f, 4) if f is not None else None,
                  "second": round(s, 4) if s is not None else None, "both_pos": both}
        print(f"  {nm:20s} 前半 {f if f is not None else 0:+.4f}  后半 "
              f"{s if s is not None else 0:+.4f}  {'双正 ✅' if both else '非双正 ⚠️'}")
    RES["Q8_half"] = q8

    # ── Q9：覆盖率退化（分数在两套行为间跳变的风险）
    print("\n" + "=" * 104)
    print("【Q9】覆盖率退化：无 fund 的截面上，各方案实际参与加权的比重")
    print("=" * 104)
    print(f"  {'方案':20s} {'来源':>8s} {'平均 Σ参与/Tot':>16s} {'最低':>8s} {'满分上限':>10s}")
    q9 = {}
    for nm, wm in PLANS.items():
        for lbl, keep in (("全样本", None),
                          ("仅无fund", lambda d, r: not any(_num(v.get("score_fund"))
                                                          for v in r.values()))):
            a20 = series(wm, 20, keep)
            if not a20:
                continue
            rs = [x[2] for x in a20]
            avg = sum(rs) / len(rs)
            mn = min(rs)
            # 分数上限 = 实际Σ参与 × 100 / max(0.5, 计划Σ)
            tw = sum(w for w in wm.values() if w > 0)
            cap = (avg * tw) / max(0.5, tw) * 100.0
            q9.setdefault(nm, {})[lbl] = {"avg_ratio": round(avg, 4), "min_ratio": round(mn, 4),
                                          "cap": round(cap, 2)}
            print(f"  {nm:20s} {lbl:>8s} {avg:16.3f} {mn:8.3f} {cap:10.2f}")
    RES["Q9_coverage_decay"] = q9

    json.dump(RES, open(os.path.join(HERE, "verify_plan_c3.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=2)
    print("\n已写出 verify_plan_c3.json")


if __name__ == "__main__":
    main()
