#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
verify_plan_c2.py —— 第二层验证：排除 regime / 池规模混淆，只测「fund 覆盖本身是否带选样偏差」

为什么还要这一层：
  verify_plan_c.py 的 Q1 把样本拆成「有 fund 段」与「无 fund 段」，但实算发现
  两段**日期完全不重叠**（有 fund 80 截面 2026-06-26~10-23；无 fund 158 截面
  以 2025-11-03~2026-06-25 为主）。于是那段差异混了三个因素：
    ① fund 数据有无  ② 时间段(regime)  ③ 选样偏差
  这正是项目记忆里 round29 Q3b 被判定「伪影作废」的同一个坑
  （「入池偏误 + 混合量尺 + 权重再分配」）。不能再犯。

  本脚本只做**同期同池**的干净检验：
    Q4 在「有 fund」的那 80 个截面内部，同一截面上比较
       『有 fund 的股票』vs『无 fund 的股票』的未来收益差
       → 直接量化 fund 覆盖指示变量本身的 IC（是不是「有钱覆盖」就等于「会涨」）
    Q5 在「有 fund」的截面上、只用**有 fund 的股票**，公平比较各方案（已由 Q2 给出），
       再补一条：D/E/C 都重仓 fund，若 fund 覆盖率低，它们的分数在无 fund 股上如何退化
    Q6 Σ 归一化的正确做法：把建议权重 rescale 到 Σ=1.0 后的 IC 是否与 Σ=0.365 一致
       （若一致 → 说明 IC 对 Σ 标度不敏感，可放心归一化；S 档才能保住）

输出：verify_plan_c2.json
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
    print("加载面板 ...")
    panel = json.load(open(os.path.join(HERE, "data", "panel_round34.json"), encoding="utf-8"))
    dates, rows = panel["dates"], panel["rows"]
    px = load_px()
    tdays = sorted({d for m in px.values() for d in m})
    tidx = {d: i for i, d in enumerate(tdays)}

    def has_fund(rd):
        return any(_num(v.get("score_fund")) for v in rd.values())

    RES = {}

    # ══ Q4：fund 覆盖指示变量的 IC（同期同池，最干净的选样偏差检验）
    print("\n" + "=" * 100)
    print("【Q4】『是否被 fund 覆盖』本身的预测力（同一截面内 有fund vs 无fund）")
    print("=" * 100)
    q4 = {}
    for hop in (5, 20):
        ics, diffs, ns = [], [], []
        for d0 in dates:
            i0 = tidx.get(d0)
            if i0 is None or i0 + hop >= len(tdays):
                continue
            rd = rows.get(d0) or {}
            if not has_fund(rd):
                continue                      # 只在有 fund 的截面上做
            a, b = [], []                     # a=有fund, b=无fund
            for c, v in rd.items():
                if not _num(v.get("score_size_ind")):
                    continue
                m = px.get(c)
                if not m:
                    continue
                p0, p1 = m.get(d0), m.get(tdays[i0 + hop])
                if not p0 or not p1:
                    continue
                r = p1 / p0 - 1.0
                (a if _num(v.get("score_fund")) else b).append(r)
            if len(a) >= 30 and len(b) >= 100:
                # 指示变量：1=有fund, 0=无fund
                xs = [1.0] * len(a) + [0.0] * len(b)
                ys = a + b
                ic = fe.rank_ic(xs, ys)
                if ic is not None:
                    ics.append(ic)
                    diffs.append(sum(a) / len(a) - sum(b) / len(b))
                    ns.append((len(a), len(b)))
        mu = sum(ics) / len(ics) if ics else None
        t = nw_t(ics, 0) if ics else None
        dm = sum(diffs) / len(diffs) if diffs else None
        q4[f"h{hop}"] = {"n_snap": len(ics), "ic": round(mu, 4) if mu is not None else None,
                         "nw_t": round(t, 2) if t is not None else None,
                         "mean_ret_diff": round(dm, 5) if dm is not None else None,
                         "avg_n_fund": round(sum(x[0] for x in ns) / len(ns), 0) if ns else None,
                         "avg_n_nofund": round(sum(x[1] for x in ns) / len(ns), 0) if ns else None}
        star = "★过Bonf" if t is not None and abs(t) >= 3.08 else ("显著" if t is not None and abs(t) >= 2 else "不显著")
        print(f"  h{hop:<3} 截面 {len(ics):>3}  IC {mu if mu is not None else 0:+.4f}  "
              f"NW t {t if t is not None else 0:+.2f}  {star}  "
              f"有fund均收益-无fund {dm*100 if dm is not None else 0:+.2f}pp  "
              f"(均 {q4[f'h{hop}']['avg_n_fund']:.0f} vs {q4[f'h{hop}']['avg_n_nofund']:.0f} 只)")
    RES["Q4_fund_coverage_IC"] = q4

    # ══ Q5：Σ 归一化后的 IC（验证 IC 是否对 Σ 标度不敏感）
    print("\n" + "=" * 100)
    print("【Q5】建议权重归一化到 Σ=1.0 后 IC 是否不变（决定能否安全归一化）")
    print("=" * 100)
    tot = .13 + .10 + .06 + .075
    PL = {
        "C 4维等权(Σ=1)": {"score_lowvol20": .25, "score_size_ind": .25,
                       "score_quality": .25, "score_fund": .25},
        "C' Σ=0.365 不归一": {"score_lowvol20": .09125, "score_size_ind": .09125,
                          "score_quality": .09125, "score_fund": .09125},
        "F 3维keep(Σ=1)": {"score_lowvol20": .13 / .305, "score_size_ind": .10 / .305,
                          "score_fund": .075 / .305},
        "F' Σ=0.305 不归一": {"score_lowvol20": .13, "score_size_ind": .10, "score_fund": .075},
        "A 现行8维": dict(W8),
    }
    q5 = {}
    for nm, wm in PL.items():
        for hop in (5, 20):
            a = []
            for d0 in dates:
                i0 = tidx.get(d0)
                if i0 is None or i0 + hop >= len(tdays):
                    continue
                rd = rows.get(d0) or {}
                if not has_fund(rd):
                    continue
                xs, ys = [], []
                for c, v in rd.items():
                    if not _num(v.get("score_size_ind")):
                        continue
                    s = prod_score(v, wm)
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
                        a.append(ic)
            if a:
                mu = sum(a) / len(a)
                t = nw_t(a, 0)
                q5.setdefault(nm, {})[f"h{hop}"] = {
                    "n": len(a), "ic": round(mu, 4),
                    "nw_t": round(t, 2) if t is not None else None}
        d5 = q5.get(nm, {})
        print(f"  {nm:22s} h5 {d5.get('h5',{}).get('ic',0):+.4f}(t{d5.get('h5',{}).get('nw_t',0):+.2f})"
              f"  h20 {d5.get('h20',{}).get('ic',0):+.4f}(t{d5.get('h20',{}).get('nw_t',0):+.2f})")
    RES["Q5_scale_invariance"] = q5

    # ══ Q6：分半稳定性（有 fund 段只有 62 截面，够不够下结论）
    print("\n" + "=" * 100)
    print("【Q6】有 fund 段的分半稳定性（62 截面虽少，看方向是否一致）")
    print("=" * 100)
    q6 = {}
    for nm in ("A 现行8维", "F 3维keep(Σ=1)", "C 4维等权(Σ=1)"):
        wm = PL[nm]
        for hop in (20,):
            a = []
            for d0 in dates:
                i0 = tidx.get(d0)
                if i0 is None or i0 + hop >= len(tdays):
                    continue
                rd = rows.get(d0) or {}
                if not has_fund(rd):
                    continue
                xs, ys = [], []
                for c, v in rd.items():
                    if not _num(v.get("score_size_ind")):
                        continue
                    s = prod_score(v, wm)
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
                        a.append(ic)
            h = len(a) // 2
            f_half = sum(a[:h]) / h if h else None
            s_half = sum(a[h:]) / (len(a) - h) if len(a) > h else None
            q6.setdefault(nm, {})[f"h{hop}"] = {
                "first_half": round(f_half, 4) if f_half is not None else None,
                "second_half": round(s_half, 4) if s_half is not None else None,
                "both_positive": bool(f_half is not None and s_half is not None
                                      and f_half > 0 and s_half > 0)}
            print(f"  {nm:22s} h20 前半 {f_half if f_half is not None else 0:+.4f}  "
                  f"后半 {s_half if s_half is not None else 0:+.4f}  "
                  f"{'双正 ✅' if q6[nm]['h20']['both_positive'] else '非双正 ⚠️'}")
    RES["Q6_half_split"] = q6

    json.dump(RES, open(os.path.join(HERE, "verify_plan_c2.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=2)
    print("\n已写出 verify_plan_c2.json")


if __name__ == "__main__":
    main()
