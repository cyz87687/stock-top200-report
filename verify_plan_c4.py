#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
verify_plan_c4.py —— 第四层验证：归零反转块的结论是否 regime 稳健（改权重前的最后一道闸门）

为什么必须做这一层：
  verify_plan_c3.py 的 Q7 发现：
    A 现行8维 h20 IC：前段(2025-11-03~2026-06-25, 156截面) -0.0021
                      后段(2026-06-26~2026-10-27,  62截面) +0.2786
    加权复原：0.7156×(-0.0021) + 0.2844×(+0.2786) = +0.0777 ≈ 审计的 +0.0778 ✅
  → 也就是说 round34 报告的「复合分 h20 +0.0778 过 Bonferroni」，
    其**全部功效来自后段 28% 的样本**；前段 72% 的样本 IC 为零。

  这直接威胁到「归零反转块」的裁决：decide_round34 的组合消融
  「去反转块三合一 → h20 +0.0778→+0.0933」如果也只来自后段，
  那这个裁决就只是「近期 regime 成立」，不能当全样本结论。

  本脚本要回答：
    Q10 去反转块在【前段】和【后段】分别的效果 → 裁决是否 regime 稳健？
    Q11 后段高 IC 是哪些维带来的（逐维单维 IC 分前后段）→ 找出真正的 alpha 源
    Q12 若归零反转块只在后段成立，则给「渐进方案」：
        保留反转块但大幅降权（而非归零），量化其 IC 代价

输出：verify_plan_c4.json
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
TOT_DEC = .13 + .10 + .06 + .075
SPLIT = "2026-06-26"


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

    def series(wmap, hop, seg=None):
        out = []
        for d0 in dates:
            i0 = tidx.get(d0)
            if i0 is None or i0 + hop >= len(tdays):
                continue
            if seg == "pre" and d0 >= SPLIT:
                continue
            if seg == "post" and d0 < SPLIT:
                continue
            rd = rows.get(d0) or {}
            xs, ys = [], []
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
            if len(xs) >= 40:
                ic = fe.rank_ic(xs, ys)
                if ic is not None:
                    out.append(ic)
        return out

    def st(a):
        if not a:
            return None, None
        return sum(a) / len(a), nw_t(a, 0)

    RES = {}

    # ══ Q10：去反转块在前段/后段的效果
    base = {k: W8[k] for k in W8}
    s_all = sum(base.values())
    norm = {k: v / s_all for k, v in base.items()}
    no_rev = {k: v for k, v in base.items()
              if k not in ("score_bias60", "score_rsi_rev", "score_tech_neg")}
    snr = sum(no_rev.values())
    no_rev_n = {k: v / snr for k, v in no_rev.items()}

    print("\n" + "=" * 106)
    print("【Q10】去反转块三合一 —— 前段 / 后段 / 全样本 分别看（裁决是否 regime 稳健）")
    print("=" * 106)
    print(f"  {'方案':26s} {'段':>6s} {'n':>5s} {'h5 IC':>9s} {'h20 IC':>9s} {'h20 t':>7s}")
    q10 = {}
    for nm, wm in (("A 现行8维", norm), ("去反转块3合1", no_rev_n)):
        for lbl, seg in (("前段", "pre"), ("后段", "post"), ("全样本", None)):
            a20 = series(wm, 20, seg)
            m20, t20 = st(a20)
            q10.setdefault(nm, {})[lbl] = {
                "n": len(a20), "h20": round(m20, 4) if m20 is not None else None,
                "t": round(t20, 2) if t20 is not None else None}
            print(f"  {nm:26s} {lbl:>6s} {len(a20):5d} "
                  f"{'':>9s} {(m20 if m20 is not None else 0):+9.4f} "
                  f"{(t20 if t20 is not None else 0):+7.2f}")
    a_pre = q10["A 现行8维"]["前段"]["h20"]
    r_pre = q10["去反转块3合1"]["前段"]["h20"]
    a_post = q10["A 现行8维"]["后段"]["h20"]
    r_post = q10["去反转块3合1"]["后段"]["h20"]
    print(f"\n  前段 Δ(去反转块−现行) = {r_pre - a_pre:+.4f}")
    print(f"  后段 Δ(去反转块−现行) = {r_post - a_post:+.4f}")
    pre_ok = (r_pre - a_pre) > -0.003
    post_ok = (r_post - a_post) > 0
    print(f"  → 前段剔除反转块{'不劣化 ✅' if pre_ok else '明显劣化 ❌'}；"
          f"后段{'提升 ✅' if post_ok else '下降 ❌'}")
    print(f"  → 判定：{'两段同向，裁决稳健 ✅' if (pre_ok and post_ok) else '结论 regime 依赖 ⚠️'}")
    RES["Q10_reversal_regime"] = q10
    RES["Q10_verdict"] = {"pre_delta": round(r_pre - a_pre, 4),
                          "post_delta": round(r_post - a_post, 4),
                          "robust": bool(pre_ok and post_ok)}

    # ══ Q11：逐维单维 IC，分前后段（找真正的 alpha 源）
    print("\n" + "=" * 106)
    print("【Q11】逐维单维 IC 分前后段（找出 alpha 到底来自哪几维）")
    print("=" * 106)
    print(f"  {'维度':18s} {'前段h20':>9s} {'前段n':>6s} {'后段h20':>9s} {'后段n':>6s} {'判读':>12s}")
    q11 = {}
    for d, wdisp in W8.items():
        row = {}
        for lbl, seg in (("前段", "pre"), ("后段", "post")):
            vals_x, out = [], []
            for d0 in dates:
                i0 = tidx.get(d0)
                if i0 is None or i0 + 20 >= len(tdays):
                    continue
                if seg == "pre" and d0 >= SPLIT:
                    continue
                if seg == "post" and d0 < SPLIT:
                    continue
                rd = rows.get(d0) or {}
                xs, ys = [], []
                for c, v in rd.items():
                    if not _num(v.get("score_size_ind")):
                        continue
                    x = v.get(d)
                    if not _num(x):
                        continue
                    m = px.get(c)
                    if not m:
                        continue
                    p0, p1 = m.get(d0), m.get(tdays[i0 + 20])
                    if not p0 or not p1:
                        continue
                    xs.append(float(x))
                    ys.append(p1 / p0 - 1.0)
                if len(xs) >= 40:
                    ic = fe.rank_ic(xs, ys)
                    if ic is not None:
                        out.append(ic)
            row[lbl] = {"ic": round(sum(out) / len(out), 4) if out else None, "n": len(out)}
        r1 = row["前段"]["ic"]
        r2 = row["后段"]["ic"]
        if r1 is None or r2 is None:
            verdict = "数据不足"
        elif r1 > 0.01 and r2 > 0.01:
            verdict = "两段皆正 ✅"
        elif r2 > 0.01 and r1 <= 0.01:
            verdict = "仅后段 ⚠️"
        elif r1 > 0.01 and r2 <= 0.01:
            verdict = "仅前段 ⚠️"
        else:
            verdict = "皆无效"
        row["verdict"] = verdict
        q11[d] = row
        print(f"  {d:18s} {(r1 if r1 is not None else 0):+9.4f} {row['前段']['n']:6d} "
              f"{(r2 if r2 is not None else 0):+9.4f} {row['后段']['n']:6d} {verdict:>12s}")
    RES["Q11_dim_by_regime"] = q11

    # ══ Q12：渐进方案（保留反转块但降权）vs 归零，IC 代价多少
    print("\n" + "=" * 106)
    print("【Q12】渐进方案：反转块降权(而非归零)的 IC 代价（全样本 h20）")
    print("=" * 106)
    rev_keys = ("score_bias60", "score_rsi_rev", "score_tech_neg")
    REST = .13 + .12 + .10 + .075          # lowvol+quality+size+fund = 0.425
    PL = {}
    for scale, tag in ((1.0, "归零(反转块0)"), (0.25, "反转块降至1/4"),
                       (0.50, "反转块降至1/2"), (0.75, "反转块降至3/4")):
        w = {}
        for k in rev_keys:
            if W8[k] * scale > 0:
                w[k] = W8[k] * scale
        rest_sum = REST
        for k in ("score_lowvol20", "score_quality", "score_size_ind", "score_fund"):
            w[k] = W8[k]
        s = sum(w.values())
        PL[tag] = {k: v / s for k, v in w.items()}
    print(f"  {'方案':22s} {'反转块Σ':>8s} {'h20 IC':>9s} {'h20 t':>7s} {'h5 IC':>9s}")
    q12 = {}
    for tag, wm in PL.items():
        a20 = series(wm, 20)
        a5 = series(wm, 5)
        m20, t20 = st(a20)
        m5, _ = st(a5)
        rv = sum(v for k, v in wm.items() if k in rev_keys)
        q12[tag] = {"rev_sum": round(rv, 4), "h20": round(m20, 4) if m20 else None,
                    "t20": round(t20, 2) if t20 else None, "h5": round(m5, 4) if m5 else None}
        print(f"  {tag:22s} {rv:8.3f} {(m20 if m20 else 0):+9.4f} "
              f"{(t20 if t20 else 0):+7.2f} {(m5 if m5 else 0):+9.4f}")
    RES["Q12_gradual"] = q12

    json.dump(RES, open(os.path.join(HERE, "verify_plan_c4.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=2)
    print("\n已写出 verify_plan_c4.json")


if __name__ == "__main__":
    main()
