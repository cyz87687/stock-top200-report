# -*- coding: utf-8 -*-
"""FY2026 卖方预期数据刷新审计（round131，2026-11-06）。

任务（老大 2026-11-06）：更新 2026 年报的预期数据。根因裁决：
  华正新材 fwd_pe 36.2 > pe 22.9 看着像「预期下修」，实际是卖方 2026 预测
  未更新（THS/东财/Wind 三源一致预期停在 ~6.42 亿 < 前三季已实现归母 8.00 亿）。

本脚本产出 data/fwd_2026_refresh_2026-11-06.json：
  ① 全池陈旧扫描（fwd_stale_guard 单一实现，含锚定前后对照）；
  ② 华正新材三源交叉证据（THS/东财/Wind + 三季报实际 + 研报 + 渠道跟踪）；
  ③ 口径披露：锚定=确定性年化（9M×4/3），敏感性区间（已实现下限/年化/Q4=Q3）。

只审计不改写生产 scored（分数刷新由 step2 陈旧守卫在日更链落地）。
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime

import fwd_stale_guard as fsg

HERE = os.path.dirname(os.path.abspath(__file__))

# ---- 华正新材三源交叉证据（2026-11-06 会话实取，逐条标注来源与获取时间）----
HUAZHENG_EVIDENCE = {
    "code": "sh603186",
    "name": "华正新材",
    "retrieved_at": "2026-11-06T09:15+08:00",
    "sell_side_consensus": [
        {"src": "同花顺 ak.stock_profit_forecast_ths(预测年报净利润)",
         "as_of": "2026-11-06 实时", "fy2026_yi": 6.42, "n_inst": 1,
         "fy2027_yi": 9.57, "fy2028_yi": 11.54},
        {"src": "东方财富 机构预测统计（mx-ds-mcp mx_ashare_finance_data）",
         "as_of": "2026-11-05", "fy2026_yi": 6.42, "eps2026": 4.09,
         "pe2026_implied": 36.18, "fy2025a_yi": 2.767},
        {"src": "Wind 一致预测（wind-finance get_financial_data）",
         "as_of": "latest", "fy2026_yi": 6.4176,
         "drift_6m": [{"window": "2026-05-07~09-15", "fy2026_yi": 5.2298},
                      {"window": "2026-09-16~09-24", "fy2026_yi": 5.5738},
                      {"window": "latest", "fy2026_yi": 6.4176}]},
    ],
    "disclosed_actuals": [
        {"src": "2026年三季报（公告 2026-10-22，东财 fin_cache_em 同源核对）",
         "ytd9m_nm_yi": 8.00, "ytd9m_deduct_yi": 7.93,
         "q1_nm_yi": 0.31, "q2_nm_yi": 1.42, "q3_nm_yi": 6.27,
         "q3_yoy_pct": 3044.60, "ytd_rev_yi": 56.10, "q3_gpm_pct": 36.06},
    ],
    "broker_reports_pre_q3": [
        {"src": "申万宏源 2026-04-04 首次覆盖", "fy2026_yi": 4.7,
         "fy2027_yi": 7.7, "fy2028_yi": 9.7},
        {"src": "浙商证券 2026-03-25 年报点评", "fy2026_yi": 5.73,
         "fy2027_yi": 8.02, "fy2028_yi": 10.54},
    ],
    "channel_tracking_l3": [
        {"src": "中信建材 2026-10-22 三季报拆解（韭研公社转载，L3 非正式，仅参考不入模）",
         "note": "月度利润 6-9月 0.9/1.5/2.0/2.7亿；预计10月单月~3.3亿、12月单月4亿+"},
    ],
    "falsification": {
        "claim": "一致预期 FY2026E 6.42亿 < 前三季已实现归母 8.00亿",
        "implied_q4_nm_yi": round(6.42 - 8.00, 2),
        "verdict": "全年预测低于已实现利润 = 被事实证伪（除非 Q4 巨亏 1.58亿，"
                   "与 Q3 单季 6.27亿、毛利率 36% 环比抬升的经营轨迹相悖）→ 判定预测陈旧",
    },
    "anchor_sensitivity": [
        {"method": "已实现下限（9M 归母，事实）", "fy2026_yi": 8.00,
         "fwd_pe": round(232.2538 / 8.00, 1), "use": "证伪下限"},
        {"method": "确定性年化 9M×4/3（入模锚定）", "fy2026_yi": round(8.00 * 4 / 3, 2),
         "fwd_pe": round(232.2538 / (8.00 * 4 / 3), 1), "use": "生产锚定（无季节假设）"},
        {"method": "Q4=Q3（乐观上限参考）", "fy2026_yi": round(8.00 + 6.27, 2),
         "fwd_pe": round(232.2538 / (8.00 + 6.27), 1), "use": "仅敏感性，不入模"},
    ],
    "mktcap_yi_note": "锚定前后 FwdPE 按 11-05 scored 的 total_mv 232.25亿折算，"
                      "与建阶梯口径（腾讯总市值）一致；华正 circ_mv=total_mv",
}


def main():
    scored_path = os.path.join(
        HERE, sys.argv[1] if len(sys.argv) > 1 and not sys.argv[1].startswith("-")
        else "top200_scored_2026-11-05.json")
    verify_ths = "--verify-ths" in sys.argv
    with open(scored_path, encoding="utf-8") as f:
        scored = json.load(f)
    rows = scored.get("results") or []

    ladder_copy, cons_map, mk_map, growth_map = {}, {}, {}, {}
    row_by_code = {}
    n_ladder = 0
    for r in rows:
        code = r.get("code")
        row_by_code[code] = r
        lad = r.get("pe_ladder")
        if isinstance(lad, dict) and lad:
            n_ladder += 1
            ladder_copy[code] = dict(lad)      # 拷贝：本脚本只审计不改写
        # ⚠️ 阶梯建模口径 = 腾讯 total_mv（总市值，亿元）——2026-11-06 实证：
        #   长鑫科技 total 28871亿/16.7=1728.8亿 ≈ THS 一致预期 1728.17亿；
        #   中国海油 total 14724.8亿/9.1=1618.1亿 ≈ THS 1620.41亿。
        #   用 circ_mv 反推会把 A/H 价差、大小非流通差异灌进一致预期（初版踩坑）。
        mv = r.get("total_mv") or r.get("circ_mv")
        if isinstance(mv, (int, float)) and mv > 0:
            mk_map[code] = mv / 1e8
        g = r.get("growth_raw")
        if isinstance(g, (int, float)):
            growth_map[code] = g
        elif isinstance(r.get("growth"), (int, float)):
            growth_map[code] = r.get("growth")

    meta = fsg.guard_pe_ladder(
        ladder_copy, {}, cons_map, {}, mk_map,
        growth_by_code=growth_map)

    # 可选复核：对触发个股实拉 THS 一致预期（真实来源），修正反推误差并留档
    ths_live = {}
    if verify_ths and meta:
        import akshare as ak
        for code in list(meta.keys()):
            try:
                df = ak.stock_profit_forecast_ths(
                    symbol=str(code)[2:], indicator="预测年报净利润")
                rec = None
                if df is not None and len(df) > 0:
                    for _, row in df.iterrows():
                        try:
                            if int(row["年度"]) == 2026:
                                rec = {"fy2026_yi": float(row["均值"]),
                                       "n_inst": int(row["预测机构数"])}
                        except Exception:
                            continue
                if rec:
                    ths_live[code] = rec
            except Exception:
                pass
        print("   THS 实拉复核 %d/%d 只" % (len(ths_live), len(meta)))

    scan, soft = [], []
    for code, m in sorted(meta.items(), key=lambda kv: -kv[1]["anchor_yi"]):
        r = row_by_code.get(code) or {}
        lad = r.get("pe_ladder") or {}
        m = dict(m)
        live = ths_live.get(code)
        if live:
            # 实拉复核：一致预期精确值替换反推值；若实值 >= 已实现累计 → 反推
            # 舍入假触发，剔除并留痕
            m["consensus_yi"] = round(live["fy2026_yi"], 4)
            m["consensus_src"] = "ths_mean_live"
            m["n_inst"] = live.get("n_inst")
            if live["fy2026_yi"] >= m["ytd_yi"]:
                m["stale"] = False
                m["stale_soft"] = False
                m["dropped_reason"] = "ths_live_consensus_ge_ytd(反推假触发)"
            elif live["fy2026_yi"] >= m["ytd_yi"] * fsg.MATERIAL_FALSIFY_RATIO:
                m["stale"] = False
                m["stale_soft"] = True
                m["src"] = "flag_only"
                m["anchor_yi"] = round(live["fy2026_yi"], 4)
            else:
                m["anchor_yi"] = round(max(live["fy2026_yi"],
                                           m["ytd_yi"] * m["annualize_factor"]), 4)
        if not (m.get("stale") or m.get("stale_soft")):
            continue
        fwd_before = r.get("fwd_pe")
        pe = r.get("pe")
        lad = r.get("pe_ladder") or {}
        mktcap = mk_map.get(code)
        fwd_after = (round(mktcap / m["anchor_yi"], 1)
                     if (mktcap and m["anchor_yi"] and m.get("stale")) else None)
        rec = {
            "code": code, "name": r.get("name"), "sector": r.get("sector"),
            "total": r.get("total"), "pe": pe, "fwd_pe_before": fwd_before,
            "fwd_pe_after": fwd_after,
            "ladder_before": lad,
            "ladder_after": ladder_copy.get(code) if m.get("stale") else None,
            "growth_raw": r.get("growth_raw"), "growth_period": r.get("growth_period"),
            **{k: m[k] for k in ("consensus_yi", "consensus_src", "ytd_yi", "period",
                                 "annualize_factor", "anchor_yi", "src", "note")}
               | ({"n_inst": m["n_inst"]} if "n_inst" in m else {}),
        }
        (scan if m.get("stale") else soft).append(rec)

    # 参考口径：fwd_pe > 1.15×pe 的形态集（含真实下修与陈旧两类，供对照）
    pattern = []
    for r in rows:
        pe, fpe = r.get("pe"), r.get("fwd_pe")
        if isinstance(pe, (int, float)) and isinstance(fpe, (int, float)) \
                and pe > 0 and fpe > pe * 1.15:
            pattern.append(r.get("code"))

    out = {
        "generated_at": datetime.now().strftime("%Y-%m-%dT%H:%M:%S+08:00"),
        "writer": "refresh_fwd_2026.py",
        "scored_source": os.path.basename(scored_path),
        "rule": {
            "stale_test": "一致预期FY2026E>0 且 2026报告期归母累计>0 且 一致预期<累计 且 核心增速>0",
            "materiality": "一致预期<累计×0.9（implied Q4 亏损>一成累计）→ stale 改数；"
                           "0.9~1.0 倍区间 → stale_soft 只留痕不改数",
            "anchor": "max(一致预期, 归母累计×12/已实现月数)（确定性年化，禁季节假设）",
            "scope": "只覆盖 pe_ladder/computed_fwd_pe 的 2026 位；2027/2028 保留卖方原值；"
                     "fwd_np（持续性闸门）与 implied_h2（Q3去重闸门）口径不变",
            "guard_impl": "fwd_stale_guard.py（step2 与本脚本共用单一实现）",
            "interpretation": "fwd_pe>pe 且增速>0 且一致预期<已实现累计 → 卖方预测陈旧伪象，"
                              "禁止读作「预期下修」",
        },
        "pool_stats": {
            "n_rows": len(rows),
            "n_with_ladder": n_ladder,
            "n_stale_overridden": len(scan),
            "n_stale_soft_flag_only": len(soft),
            "n_fwd_gt_115pe_pattern": len(pattern),
        },
        "stale_scan": scan,
        "soft_scan": soft,
        "huazheng_evidence": HUAZHENG_EVIDENCE,
        "caveats": [
            "锚定值=确定性年化推算（真实已披露利润+固定年化系数），非卖方一致预期，"
            "行级以 fwd_2026_src 区分；卖方原值保留于 fwd_2026_consensus 留档",
            "若个别公司 Q4 真实巨亏使 FY<已实现累计，锚定偏高——触发条件已要求核心增速>0，"
            "残余误伤可按 fwd_stale 行级复核",
            "生产 scored 的分数刷新由 step2 陈旧守卫在日更链落地（本脚本只审计不改写）",
        ],
    }
    out_path = os.path.join(HERE, "data", "fwd_2026_refresh_2026-11-06.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print("✅ 陈旧改数 %d 只 / 轻微留痕 %d 只 / 阶梯覆盖 %d / 形态参照 %d → %s"
          % (len(scan), len(soft), n_ladder, len(pattern), out_path))
    for s in scan[:15]:
        print("   %-10s %-8s 一致预期%.2f亿→锚定%.2f亿  FwdPE %s→%s  (pe=%s)"
              % (s["code"], s.get("name") or "", s["consensus_yi"], s["anchor_yi"],
                 s["fwd_pe_before"], s["fwd_pe_after"], s["pe"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
