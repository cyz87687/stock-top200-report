#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""v5.1 审计重算：年报一致预期统一 + 前20偏高项口径修正。

不做生产覆盖：读 top200_scored_2026-11-23.json，输出独立的
  top200_scored_2026-11-23_v51.json
  data/fwd_annual_audit_2026-11-23.json
  audit_v51_top20_2026-11-23.md

三条修正（均为全池同规则，不按头部名单调参）：
  A. fwd 年报化：Top20 用 Wind FY2026/27/28 一致预期替换 THS 阶梯反推值；
     陈旧者沿用 fwd_stale_guard 的确定性年化锚，并保留 Wind 原值留档。
     其余股票未逐只复核，只打口径/来源标签（ths 年报缓存，unverified）。
     ⚠️ score_fund 重建残差最大 1.46 分（old/new 同参重建也大面积同档封顶），
        本轮**不据 fwd 变化调分**，只更新数据字段并把冲突逐只标注；
        待 score_fund 输入链可完整复现生产分后再做逐分重算。
  B. 低基数二次封顶：growth_capped 且 (OCF/NI<0 或 ROE<10%) → score_growth 封顶 8.0。
  C. 质量硬约束：score_quality>9 且 (ROE<10% 或 OCF/NI 缺失/为负) → 封顶 8.5。
  D. 成长当期背离封顶：无 Q3 实际、modeB.g_q<1% 且 deriv1_growth<1% → score_growth 封顶 8.0。

总分只用唯一内核 scoring_core.composite_total_matrix 重算；评级按 gen_report 的
分位口径 RATING_CUTS=(0.05,0.15,0.50,0.88,1.01) 重算。
"""
from __future__ import annotations

import copy
import json
import os
from datetime import datetime

import numpy as np

import scoring_core as sc
import fwd_stale_guard as fsg

try:
    import step2_stockscorer_v2 as s2
except Exception as exc:                                  # pragma: no cover
    raise SystemExit(f"无法导入 step2_stockscorer_v2: {exc}")

HERE = os.path.dirname(os.path.abspath(__file__))
SCORED = os.path.join(HERE, "top200_scored_2026-11-23.json")
WIND = os.path.join(HERE, "data", "fwd_annual_consensus_2026-11-23_wind.json")
WEIGHTS = os.path.join(HERE, "factor_weights.json")
OUT_SCORED = os.path.join(HERE, "top200_scored_2026-11-23_v51.json")
OUT_AUDIT = os.path.join(HERE, "data", "fwd_annual_audit_2026-11-23.json")
OUT_MD = os.path.join(HERE, "audit_v51_top20_2026-11-23.md")

RATING_CUTS = [(0.05, "S"), (0.15, "A"), (0.50, "B"), (0.88, "C"), (1.01, "D")]
DIM_ORDER = ["score_size_comb", "score_fund", "score_growth", "score_quality",
             "score_ep", "score_es", "score_bias60", "score_rsi_rev"]


def is_num(x):
    return isinstance(x, (int, float)) and not isinstance(x, bool) and x == x


def q3_info_from_row(r):
    raw = r.get("q3_yoy_raw")
    if not is_num(raw):
        return None
    return {
        "a_yoy": float(raw),
        "b_yoy": None,
        "implied_h2": r.get("q3_implied_h2") if is_num(r.get("q3_implied_h2")) else None,
        "flag": "none",
        "src": r.get("q3_src") or "manual_q3_table",
    }


def call_score_fund(r, fwd_pe, ladder, fwd_np):
    """用 scored 行重建 score_fund 入参；old/new 用同一套重建，差值抵消重建误差。"""
    return s2.score_fund(
        r.get("pe") if is_num(r.get("pe")) else None,
        fwd_pe,
        r.get("growth") if is_num(r.get("growth")) else None,
        r.get("code"),
        r.get("sector"),
        pe_ladder_data=ladder or None,
        roe=r.get("roe") if is_num(r.get("roe")) else None,
        gross_margin=None,
        sub_theme_label=r.get("sub_theme"),
        hist_loss_free=False,
        fwd_np=fwd_np,
        sw_l2=r.get("sector_l2"),
        cagr3=r.get("cagr3") if is_num(r.get("cagr3")) else None,
        q3_info=q3_info_from_row(r),
        roe_unit=r.get("roe_unit"),
    )[0]


def implied_np(mk_yi, ladder):
    out = {}
    for y, pe in (ladder or {}).items():
        try:
            yi, pep = int(y), float(pe)
        except Exception:
            continue
        if yi > 0 and pep > 0 and mk_yi and mk_yi > 0:
            out[yi] = mk_yi / pep
    return out


def quantile_ratings(rows):
    srt = sorted(rows, key=lambda x: -(x.get("total") or 0))
    n = len(srt)
    out = {}
    for i, r in enumerate(srt):
        q = (i + 0.5) / n
        rt = "D"
        for hi, name in RATING_CUTS:
            if q < hi:
                rt = name
                break
        out[r["code"]] = rt
    return out


def main():
    scored = json.load(open(SCORED, encoding="utf-8"))
    wind = json.load(open(WIND, encoding="utf-8"))
    fw = json.load(open(WEIGHTS, encoding="utf-8"))
    weights = {k: float(v) for k, v in fw["weights"].items() if float(v) > 0}
    dim_max = fw["dim_max"]
    dims = DIM_ORDER
    assert set(dims) == set(weights), (set(dims) ^ set(weights))

    wind_by = {r["code"]: r for r in wind["rows"]}
    src_rows = scored["results"]
    new_rows = []

    mk, growth, em_info, ladders, fwds = {}, {}, {}, {}, {}
    for r in src_rows:
        c = r["code"]
        mv = r.get("total_mv") or r.get("circ_mv")
        if is_num(mv) and mv > 0:
            mk[c] = float(mv) / 1e8
        g = r.get("growth_raw") if is_num(r.get("growth_raw")) else r.get("growth")
        if is_num(g):
            growth[c] = float(g)
        em_info[c] = {"growth": g}
        if isinstance(r.get("pe_ladder"), dict) and r.get("pe_ladder"):
            ladders[c] = dict(r["pe_ladder"])
        if is_num(r.get("fwd_pe")):
            fwds[c] = float(r["fwd_pe"])

    cons_wind = {}
    for c, w in wind_by.items():
        cons_wind[c] = {2026: w["fy2026_yi"], 2027: w["fy2027_yi"], 2028: w["fy2028_yi"]}

    ladder_copy = {c: dict(v) for c, v in ladders.items()}
    fwd_copy = dict(fwds)
    guard_meta = fsg.guard_pe_ladder(
        ladder_copy, fwd_copy, cons_wind, em_info, mk, growth_by_code=growth)

    fwd_audit, cap_audit = [], []
    fund_recon_diffs = []

    for r in src_rows:
        nr = copy.deepcopy(r)
        c = r["code"]
        w = wind_by.get(c)
        if w:
            cons = cons_wind[c]
            m = guard_meta.get(c) or {}
            ytd = fsg.extract_em_nm_ytd(c[2:])
            ytd_yi = round(float(ytd["ytd_yuan"]) / 1e8, 4) if ytd else None
            ytd_period = ytd.get("period") if ytd else None
            if m.get("stale"):
                fwd_new = fwd_copy.get(c, r.get("fwd_pe"))
                ladder_new = dict(ladder_copy.get(c) or r.get("pe_ladder") or {})
                fwd_src = "wind_consensus_stale_anchor_" + str(m.get("src") or "guard")
                anchor = m.get("anchor_yi")
                stale = True
            else:
                fwd_new = round(mk[c] / cons[2026], 1) if mk.get(c) and cons[2026] > 0 else r.get("fwd_pe")
                ladder_new = {}
                for y in (2026, 2027, 2028):
                    v = cons.get(y)
                    if mk.get(c) and is_num(v) and v > 0:
                        ladder_new[str(y)] = round(mk[c] / v, 1)
                if not ladder_new:
                    ladder_new = dict(r.get("pe_ladder") or {})
                fwd_src = "wind_consensus_fy_annual"
                anchor = round(cons[2026], 4)
                stale = False

            old_np = implied_np(mk.get(c), r.get("pe_ladder") or {})
            new_np = {str(y): float(v) for y, v in cons.items()}
            old_fund = call_score_fund(r, r.get("fwd_pe"), r.get("pe_ladder") or {}, old_np)
            new_fund = call_score_fund(r, fwd_new, ladder_new, new_np)
            if is_num(old_fund) and is_num(r.get("score_fund")):
                fund_recon_diffs.append(abs(old_fund - r["score_fund"]))
            # 重建残差过大且 old/new 同档封顶 → 不据此调分，只记录诊断与冲突
            # 生产 score_fund 保持不变；new_fund 仅作为口径差异诊断留痕
            nr["fwd_score_fund_action"] = "data_only_no_rescore"
            nr["fwd_fund_recon_delta"] = (round(new_fund - old_fund, 3)
                                          if is_num(new_fund) and is_num(old_fund) else None)
            nr["fwd_pe"] = fwd_new
            nr["pe_ladder"] = ladder_new
            nr["fwd_2026_consensus"] = round(cons[2026], 4)
            nr["fwd_2027_consensus"] = round(cons[2027], 4)
            nr["fwd_2028_consensus"] = round(cons[2028], 4)
            nr["fwd_2026_src"] = fwd_src
            nr["fwd_caliber"] = "FY2026E/FY2027E/FY2028E 归母净利润一致预期（年报）"
            nr["fwd_wind_as_of"] = wind.get("as_of")
            nr["fwd_latest_forecast_date"] = w.get("latest_forecast_date")
            nr["fwd_2026_ytd"] = ytd_yi
            nr["fwd_2026_period"] = ytd_period
            nr["fwd_2026_anchor"] = anchor
            nr["fwd_stale"] = bool(stale)
            nr["fwd_stale_soft"] = bool(m.get("stale_soft"))
            fwd_audit.append({
                "code": c, "name": r.get("name"),
                "old_fwd_pe": r.get("fwd_pe"), "new_fwd_pe": fwd_new,
                "old_implied_consensus_yi": round(old_np.get(2026), 4) if old_np.get(2026) else None,
                "wind_consensus_yi": round(cons[2026], 4),
                "wind_as_of": wind.get("as_of"),
                "latest_forecast_date": w.get("latest_forecast_date"),
                "ytd_yi": ytd_yi, "ytd_period": ytd_period,
                "stale": bool(stale), "soft": bool(m.get("stale_soft")),
                "src": fwd_src,
                "old_score_fund": r.get("score_fund"),
                "recon_old_score_fund": round(old_fund, 3) if is_num(old_fund) else None,
                "recon_new_score_fund": round(new_fund, 3) if is_num(new_fund) else None,
                "score_fund_unchanged": True,
                "recon_delta_diagnostic_only": nr["fwd_fund_recon_delta"],
                "note": m.get("note") or "",
            })
        else:
            nr["fwd_caliber"] = "FY2026E 年报一致预期（THS 11-23 缓存，未逐只复核）"
            nr["fwd_2026_src"] = "ths_annual_consensus_cached_unverified"

        # 全池同规则口径修正
        reasons = list(r.get("audit_v51_notes") or [])
        fd = r.get("fund_detail") or {}
        mb = fd.get("modeB") or {}
        g_new = nr.get("score_growth")
        if is_num(g_new):
            ocfg = r.get("ocfg")
            roe = r.get("roe")
            lowbase = bool(r.get("growth_capped")) and (
                (is_num(ocfg) and ocfg < 0) or (is_num(roe) and roe < 10))
            diverge = (r.get("q3_yoy") is None
                       and is_num(mb.get("g_q")) and mb["g_q"] < 1
                       and is_num(fd.get("deriv1_growth")) and fd["deriv1_growth"] < 1)
            if lowbase and g_new > 8.0:
                g_new = 8.0
                reasons.append("低基数二次封顶8.0(OCF<0或ROE<10)")
            if diverge and g_new > 8.0:
                g_new = 8.0
                reasons.append("成长与当期增速背离封顶8.0(g_q<1%且deriv1<1%)")
            nr["score_growth"] = round(g_new, 2)

        q_new = nr.get("score_quality")
        if is_num(q_new) and q_new > 9.0:
            ocfg = r.get("ocfg")
            roe = r.get("roe")
            if (not is_num(roe) or roe < 10) or (not is_num(ocfg) or ocfg < 0):
                nr["score_quality"] = 8.5
                reasons.append("质量硬约束封顶8.5(ROE<10或OCF缺失/为负)")
        if reasons:
            nr["audit_v51_notes"] = reasons
            cap_audit.append({"code": c, "name": r.get("name"), "notes": reasons,
                              "old_growth": r.get("score_growth"), "new_growth": nr.get("score_growth"),
                              "old_quality": r.get("score_quality"), "new_quality": nr.get("score_quality")})
        new_rows.append(nr)

    # 唯一内核重算总分
    S = np.array([[r.get(d) if is_num(r.get(d)) else np.nan for d in dims]
                  for r in new_rows], dtype=float)
    missing_mask = {}
    for d in dims:
        flag = np.array([(r.get("missing_reason") or {}).get(d) == "structural" for r in new_rows])
        if flag.any():
            missing_mask[d] = flag
    totals, meta = sc.composite_total_matrix(S, dims, weights, dim_max,
                                             missing_rule="structural",
                                             missing_mask=missing_mask)
    old_by_code = {r["code"]: r.get("total") for r in src_rows}
    for r, t in zip(new_rows, totals):
        r["total"] = round(float(t), 2)
        r["total_v4_prev"] = old_by_code.get(r["code"])
        r["total_delta_v51"] = round(r["total"] - (old_by_code.get(r["code"]) or 0), 2)
    ratings = quantile_ratings(new_rows)
    for r in new_rows:
        r["rating"] = ratings.get(r["code"], r.get("rating"))

    out = dict(scored)
    out["model"] = (str(scored.get("model") or "") +
                    " | v5.1-audit-fwd-annual（审计重算，未覆盖生产）")
    out["weights_version"] = "v5.0-audit-revg-es（v5.1 仅改分值/数据口径，权重不变）"
    out["results"] = new_rows
    generated_at = datetime.now().astimezone().isoformat(timespec="minutes")
    out["audit_v51"] = {
        "generated_at": generated_at,
        "wind_as_of": wind.get("as_of"),
        "wind_source": wind.get("source"),
        "fwd_rows_verified": len(fwd_audit),
        "fwd_rows_unverified": len(new_rows) - len(fwd_audit),
        "fund_reconstruction_max_diff": round(max(fund_recon_diffs) if fund_recon_diffs else 0, 3),
        "kernel_meta": meta,
        "rules": {
            "lowbase_growth_cap": "growth_capped 且 OCF/NI<0 或 ROE<10 → score_growth≤8.0",
            "quality_hard_cap": "score_quality>9 且 ROE<10 或 OCF/NI 缺失/为负 → score_quality≤8.5",
            "q3_divergence_cap": "q3_yoy 缺失 且 g_q<1% 且 deriv1_growth<1% → score_growth≤8.0",
            "fwd": "Wind FY2026/27/28 一致预期年报口径；陈旧者用 fwd_stale_guard 年化锚",
        },
    }
    with open(OUT_SCORED, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)

    audit = {
        "generated_at": out["audit_v51"]["generated_at"],
        "wind_source": wind.get("source"), "wind_as_of": wind.get("as_of"),
        "caliber": wind.get("caliber"),
        "fwd_audit": fwd_audit,
        "cap_audit": cap_audit,
        "unverified_note": "非 Top20 的 1895 只未逐只从 Wind 重拉；沿用 11-23 THS 年报一致预期缓存并打 unverified 标签，待下一轮全池刷新。",
        "reconstruction_note": "score_fund 本轮不据 fwd 变化调分（data_only_no_rescore）：old/new 同参重建残差最大 " + str(round(max(fund_recon_diffs) if fund_recon_diffs else 0, 3)) + " 分，且 old/new 大面积同档封顶，无法可靠复现生产链；生产 score_fund 原值保留，重建差值仅作诊断字段 recon_delta_diagnostic_only 留痕。待 score_fund 输入链可完整复现生产分后再逐分重算。",
    }
    with open(OUT_AUDIT, "w", encoding="utf-8") as f:
        json.dump(audit, f, ensure_ascii=False, indent=1)

    old_rank = {r["code"]: i + 1 for i, r in enumerate(
        sorted(src_rows, key=lambda x: -(x.get("total") or 0)))}
    new_sorted = sorted(new_rows, key=lambda x: -x["total"])[:20]
    lines = ["# v5.1 审计重算 · 前20（2026-11-23 数据）", "",
             f"- 内核：{meta['scale']} / missing_rule={meta['missing_rule']} / denominator={meta['denominator']}",
             f"- fwd：Top20 已换 Wind 年报一致预期（as_of {wind.get('as_of')}）；其余打 unverified 标签",
             f"- score_fund 重建最大残差：{out['audit_v51']['fund_reconstruction_max_diff']} → 本轮 data_only_no_rescore，生产分不变、fwd 只更新数据字段",
             "", "| 新排名 | 对象 | 新总分 | 旧排名 | 变化 | 市值合并 | 成长 | 基本面 | 质量 | EP | ES | 主要调整 |",
             "|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|"]
    for i, r in enumerate(new_sorted, 1):
        o = old_rank.get(r["code"])
        delta = (o - i) if o else None
        notes = "、".join(r.get("audit_v51_notes") or []) or "fwd年报口径校准"
        lines.append(f"| {i} | {r['name']}({r['code']}) | {r['total']:.2f} | {o} | "
                     f"{('+' + str(delta)) if delta and delta > 0 else str(delta or 0)} | "
                     f"{r.get('score_size_comb')} | {r.get('score_growth')} | {r.get('score_fund')} | "
                     f"{r.get('score_quality')} | {r.get('score_ep')} | {r.get('score_es')} | {notes} |")
    with open(OUT_MD, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")

    print(f"✅ v51 重算完成：{OUT_SCORED}")
    print(f"   Wind fwd 核验 {len(fwd_audit)} 只；未复核 {len(new_rows)-len(fwd_audit)} 只")
    print(f"   score_fund 重建最大差 {out['audit_v51']['fund_reconstruction_max_diff']}")
    for i, r in enumerate(new_sorted[:20], 1):
        o = old_rank.get(r["code"])
        print(f"  {i:02d} {r['name']:<6} {r['total']:>6.2f} (旧#{o}, {r['total_delta_v51']:+.2f})")


if __name__ == "__main__":
    main()
