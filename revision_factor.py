#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
revision_factor.py — FY1 一致预期修正因子（Revision）唯一判据实现

2026-11-26 老大裁决：ES 手工源退役后，改用「一致预期净利润在披露前后两时点的变化率」
作为超预期的可观测代理（预期修正 Revision）。本模块是该因子的唯一计算实现，
任何报告/验证/回测必须调用本模块，禁止各自重写口径。

【口径边界 — 必须随产物披露】
1. 数据源：Wind MCP get_financial_data，字段「机构预测 2026 全年归母净利润一致预期平均值」，
   两时点 T0=2026-09-30（三季报披露前）、T1=2026-11-25（披露后）。这是真 PIT 截面（经探查证实随时点变化，非回填）。
2. 这是【全年 FY1 预期修正】，不是传统单季 SUE/ES：三源 MCP 均无单季预测，无法计算
   「披露前单季预测 vs 单季实际」。禁止把本因子称为 ES 或「单季超预期」。
3. 本因子当前为【研究层】：未过 factor_weights.json 的 pending_dims.score_es 六门槛之前，
   score_es 权重恒 0，本因子不写入 scored、不进总分、不进组合。禁止称经验证 alpha。

【唯一判据】
revision_pct = (FY1_T1 - FY1_T0) / |FY1_T0| * 100      （正=上修，负=下修）
分档（0~10，与项目其他维一致）：先对 revision_pct 做 1/99 缩尾，再线性映射到 [1,10]；
缩尾用 growth_factor_b._winsorize_bounds 同款百分位口径。

【缺失/异常分类 — 与项目既有规则一致】
- 缺任一预测时点          → missing_reason = data          （归 data，不罚 0）
- FY1_T0 <= 0（负/零基数）→ missing_reason = structural    （修订率无定义，分子分母同剔，禁按 zero 罚 0）
- T1 归负（预期转亏）且 T0>0 → 保留但 revision_pct 由缩尾约束；dirty_flag=sign_flip
- T0/T1 极端值（>2000 亿或 <Q3 单季实际的 0.8 倍）→ dirty_flag 标记，score 置 None（归 data）
"""

import json
import math
import os

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_INPUT = os.path.join(HERE, "data", "fy1_consensus_wind_2pt_20261126.json")

# 与 growth_factor_b._winsorize_bounds / project 维度上限一致
WINSOR_LO = 1.0
WINSOR_HI = 99.0
SCORE_MIN, SCORE_MAX = 1.0, 10.0
EXTREME_YI = 2000.0        # A 股全年净利预期极值（亿元），超出判脏
Q3_LT_RATIO = 0.8          # FY1_T0 < Q3单季实际 * 0.8 判脏


def _percentile(sorted_vals, p):
    """线性插值百分位，与 numpy.percentile 默认口径一致（避免依赖 numpy）。"""
    if not sorted_vals:
        return None
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    k = (len(sorted_vals) - 1) * p / 100.0
    f = math.floor(k)
    c = math.ceil(k)
    if f == c:
        return sorted_vals[int(k)]
    return sorted_vals[f] * (c - k) + sorted_vals[c] * (k - f)


def compute_revision_map(input_path=None, q3_path=None):
    """
    返回 {code6: {...}}，code6 为纯 6 位代码（与项目 klines_long 键口径一致）。
    每条含：revision_pct / revision_score / missing_reason / dirty_flag / fy1_t0 / fy1_t1 / name。
    """
    inp = input_path or DEFAULT_INPUT
    d = json.load(open(inp, encoding="utf-8"))
    rows = d["rows"]

    # Q3 实际单季净利（仅用于脏值守卫，不参与 revision 计算）
    q3_actual = {}
    if q3_path is None:
        q3_path = os.path.join(HERE, "data", "q3_2026_actual_em_20261126.json")
    if os.path.exists(q3_path):
        q3 = json.load(open(q3_path, encoding="utf-8"))
        for r in q3.get("rows", []):
            q3_actual[r["code"].split(".")[0]] = r.get("np_gs_q_yi")

    out = {}
    for r in rows:
        code6 = r["code"].split(".")[0]
        v0 = r.get("fy1_consensus_20260930_yi")
        v1 = r.get("fy1_consensus_20261125_yi")
        rec = {
            "name": r.get("name"),
            "fy1_t0": v0, "fy1_t1": v1,
            "t0_date": "2026-09-30", "t1_date": "2026-11-25",
            "revision_pct": None, "revision_score": None,
            "missing_reason": None, "dirty_flag": None,
        }
        # 脏值守卫（优先级高于缺失）
        dirty = None
        if (isinstance(v0, (int, float)) and v0 > EXTREME_YI) or \
           (isinstance(v1, (int, float)) and v1 > EXTREME_YI):
            dirty = "fy1_extreme_gt2000"
        q3np = q3_actual.get(code6)
        if dirty is None and isinstance(v0, (int, float)) and v0 > 0 and \
           isinstance(q3np, (int, float)) and v0 < q3np * Q3_LT_RATIO:
            dirty = "fy1_lt_q3_actual"
        if dirty is not None:
            rec["dirty_flag"] = dirty
            rec["missing_reason"] = "data"
            out[code6] = rec
            continue

        # 缺失：缺任一预测时点
        if v0 is None or v1 is None:
            rec["missing_reason"] = "data"
            out[code6] = rec
            continue
        # 负/零基数：修订率无定义 → structural，分子分母同剔
        if v0 <= 0:
            rec["missing_reason"] = "structural"
            out[code6] = rec
            continue

        rev = (v1 - v0) / abs(v0) * 100.0
        rec["revision_pct"] = round(rev, 2)
        if v1 <= 0:
            rec["dirty_flag"] = "sign_flip_to_loss"
        out[code6] = rec

    # 缩尾 + 线性分档（仅对 revision_pct 有效者）
    valid = [r["revision_pct"] for r in out.values() if r["revision_pct"] is not None]
    if valid:
        sv = sorted(valid)
        lo = _percentile(sv, WINSOR_LO)
        hi = _percentile(sv, WINSOR_HI)
        if hi == lo:
            lo, hi = sv[0] - 1.0, sv[-1] + 1.0
        for r in out.values():
            if r["revision_pct"] is None:
                continue
            x = min(hi, max(lo, r["revision_pct"]))
            score = SCORE_MIN + (x - lo) / (hi - lo) * (SCORE_MAX - SCORE_MIN)
            r["revision_score"] = round(score, 2)
            r["winsor_lo"] = round(lo, 2)
            r["winsor_hi"] = round(hi, 2)
    return out


def coverage_report(rmap):
    n = len(rmap)
    scored = sum(1 for r in rmap.values() if r["revision_score"] is not None)
    from collections import Counter
    miss = Counter(r["missing_reason"] for r in rmap.values() if r["missing_reason"])
    dirty = Counter(r["dirty_flag"] for r in rmap.values() if r["dirty_flag"])
    return {
        "n_universe": n,
        "n_scored": scored,
        "coverage_pct": round(scored / n * 100, 2) if n else 0,
        "missing_reason_counts": dict(miss),
        "dirty_flag_counts": dict(dirty),
        "threshold_2_coverage_ge_30pct": (n > 0 and scored / n >= 0.30),
        "threshold_2_n_ge_194": (scored >= 194),
    }


def main():
    rmap = compute_revision_map()
    rep = coverage_report(rmap)
    print(json.dumps(rep, ensure_ascii=False, indent=2))
    # 头部上修/下修（仅展示，非结论）
    valid = [(c, r) for c, r in rmap.items() if r["revision_pct"] is not None]
    print("\nTOP 上修:")
    for c, r in sorted(valid, key=lambda x: -x[1]["revision_pct"])[:10]:
        print(f"  {r['name']:<10} {r['revision_pct']:>8.2f}%  score={r['revision_score']}")
    print("TOP 下修:")
    for c, r in sorted(valid, key=lambda x: x[1]["revision_pct"])[:10]:
        print(f"  {r['name']:<10} {r['revision_pct']:>8.2f}%  score={r['revision_score']}  {r.get('dirty_flag') or ''}")


if __name__ == "__main__":
    main()
