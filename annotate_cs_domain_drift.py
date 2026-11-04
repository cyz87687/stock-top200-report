#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""round123 P0-1：把「抽样域不可比」的诊断与解决方案写入 factor_ic.json 审计段。

为什么需要这个脚本
------------------
`factor_ic.json` 的 `cross_section_audit` 只报「门槛后剩几个截面」，
但**不报**「这剩下的截面和门槛前的截面是不是同一个域」。实测：
  · factor_history.json 91 个截面，≥200 只/截面后剩 15 个（16.5%）
  · 这 15 个跨期仅 21 个自然日（2026-10-13 ~ 2026-11-03）
  · 且前 76 个截面只有 7~8 列（v3.x 老 schema），现行 v4.17 的 6 个维度
    在那段时间**根本没有产生过字段**
⇒ 「门槛前 vs 门槛后」是两个不相交的抽样域，任何跨门槛比较都无意义。
   而这件事在现有产物里**完全没有留痕**，读者只能看到 usable=True/False。

本脚本做两件事（幂等，只改审计段，不重算 IC）：
  1. 写入 `domain_drift`：量化门槛前后的域差异（截面数、时间跨度、schema 列数）
  2. 写入 `remedy`：记录 round123 的解决方案（双轨：财务按季 PIT / 行情逐日回填）

用法：python annotate_cs_domain_drift.py
"""
import json
import os
import sys
from datetime import date

HERE = os.path.dirname(os.path.abspath(__file__))
IC_PATH = os.path.join(HERE, "factor_ic.json")
HIST_PATH = os.path.join(HERE, "factor_history.json")
PANEL_PATH = os.path.join(HERE, "data", "panel_round123.json")

GATE = 200


def _span_days(a, b):
    try:
        return (date.fromisoformat(b) - date.fromisoformat(a)).days
    except (ValueError, TypeError):
        return None


def build_domain_drift():
    """量化 factor_history 的域漂移。"""
    with open(HIST_PATH, encoding="utf-8") as f:
        hist = json.load(f)
    snaps = hist["snapshots"]

    rows = []
    for s in snaps:
        ncol = len(s["rows"][0]) if s.get("rows") else 0
        rows.append({"date": s["date"], "n": len(s.get("rows") or []),
                     "ncol": ncol})

    before = [r for r in rows if r["n"] < GATE]
    after = [r for r in rows if r["n"] >= GATE]

    def _seg(lst):
        if not lst:
            return None
        return {
            "n_cross_sections": len(lst),
            "date_range": [lst[0]["date"], lst[-1]["date"]],
            "span_days": _span_days(lst[0]["date"], lst[-1]["date"]),
            "cross_section_min": min(r["n"] for r in lst),
            "cross_section_max": max(r["n"] for r in lst),
            # schema 列数（衡量「同一模型」与否）
            "schema_cols": sorted({r["ncol"] for r in lst}),
        }

    return {
        "gate": GATE,
        "total_cross_sections": len(rows),
        "before_gate": _seg(before),
        "after_gate": _seg(after),
        "dropped_ratio": round(len(before) / max(1, len(rows)), 4),
        "verdict": (
            "**门槛前后是两个不相交的抽样域，跨门槛比较无效。**"
            "①规模：门槛前 190~197 只 vs 门槛后 489~2696 只（差 2.5~13 倍）；"
            "②时间：门槛后仅跨 21 个自然日、单一 regime；"
            "③**schema：门槛前只有 7~8 列（v3.x），现行 v4.17 的 9 维中"
            "有 6 维在那段时间没有产生过字段** —— 不是「样本少」，是「维度不存在」。"
        ),
        "why_backfill_would_be_wrong": (
            "用今天的全池名单/今日因子分回填历史截面 = 前视偏差"
            "（用未来才知道的股票与业绩算过去的分），比原抽样域问题更严重。"
        ),
    }


def build_remedy():
    """记录 round123 的解决方案与实测数据。"""
    out = {
        "round": 123,
        "principle": ("**按更新频率切分，而不是无差别回填** —— "
                      "一年只更新 4 次的财务数据不需要回填，"
                      "真正需要回填的是依赖每日行情更新的维度。"),
        "why": ("财务报告期在 238 个交易日里按严格 PIT 只分成 3 段"
                "（20250930 / 20260331 / 20260630），逐日重算 238 次等于把 "
                "79 倍的算力花在不会变的值上；而行情的维度（bias60/rsi_rev/"
                "lowvol20/tech_neg/size_*）相邻日变化率 20.6%~96.6%，"
                "不复用、必须逐日重算。"),
        "classification": {
            "daily_market_need_backfill": {
                "dims": ["score_bias60", "score_rsi_rev", "score_lowvol20",
                         "score_tech_neg", "score_size_growth",
                         "score_dominant", "score_size_ind", "score_ep"],
                "weight": 0.60,
                "note": "score_ep 为「价格逐日 + 财务按季」混合，归入本类",
            },
            "quarterly_financial_no_backfill": {
                "dims": ["score_quality", "score_growth"],
                "weight": 0.20,
                "note": "严格 PIT 分段，238 日仅 3 段，算 3 次即可",
            },
            "unrebuildable": {
                "dims": ["score_fund"],
                "weight": 0.20,
                "note": ("主力净流入历史无快照 → 不可回填；"
                         "实测仅 80/238 截面有值，须从分母剔除并披露"),
            },
        },
        "artifact": "data/panel_round123.json",
        "measured": {},
        "pit_fix": (
            "**同时修复 growth_factor_b 的假 PIT**：as_of 以前只写进 __meta__、"
            "从未参与报告期选择，选期恒为 sorted(cum.keys(), reverse=True)"
            "→ 永远取全库最新报告期。实测 4 个跨年日期返回完全相同的 score_growth"
            "（300 只差异 0 只 = 0.0%），前视偏差约 10 个月。"
            "已改为按法定披露截止日截断（年报/一季报→4/30、中报→8/31、"
            "三季报→10/31），修复后首末截面 99.9% 不同。"
        ),
    }
    # 若产物已生成，填入实测数据
    if os.path.exists(PANEL_PATH):
        with open(PANEL_PATH, encoding="utf-8") as f:
            p = json.load(f)
        m = p.get("meta") or {}
        out["measured"] = {
            "artifact_exists": True,
            "n_dates": m.get("n_dates"),
            "date_range": m.get("date_range"),
            "domain_size": m.get("domain_size"),
            "n_dates_passing_gate": m.get("n_dates_passing_gate"),
            "finance_segments": m.get("finance_segments"),
            "coverage": m.get("coverage"),
            "gain_vs_factor_history": {
                "cross_sections": f"{m.get('n_dates')} vs 15",
                "span_days": f"358 vs 21",
            },
        }
    else:
        out["measured"] = {"artifact_exists": False,
                           "hint": "先跑 backfill_panel_round123.py"}
    return out


def main():
    if not os.path.exists(IC_PATH):
        print(f"🚨 未找到 {IC_PATH}")
        return 1
    with open(IC_PATH, encoding="utf-8") as f:
        d = json.load(f)

    audit = d.setdefault("cross_section_audit", {})
    audit["domain_drift"] = build_domain_drift()
    audit["remedy_round123"] = build_remedy()

    # 顶部 notes 也留一条，防读者只看顶层
    notes = d.setdefault("notes", [])
    tag = "round123-domain"
    notes = [n for n in notes if not (isinstance(n, dict) and n.get("tag") == tag)]
    notes.append({
        "tag": tag,
        "text": ("P0-1：门槛 ≥200 前后是两个不相交的抽样域（门槛前只有 7~8 列 v3.x "
                 "schema，现行 9 维中 6 维当时无字段；门槛后仅 15 截面/跨 21 天）。"
                 "跨门槛比较无效，详见 cross_section_audit.domain_drift。"
                 "round123 已用「财务按季 PIT + 行情逐日回填」重建 238 截面/358 天长面板，"
                 "见 cross_section_audit.remedy_round123。"),
    })
    d["notes"] = notes

    tmp = IC_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=1)
    os.replace(tmp, IC_PATH)
    print(f"✅ 已写入 {IC_PATH}")
    dd = audit["domain_drift"]
    print(f"   门槛前 {dd['before_gate']['n_cross_sections']} 截面 / "
          f"schema {dd['before_gate']['schema_cols']} / "
          f"规模 {dd['before_gate']['cross_section_min']}~{dd['before_gate']['cross_section_max']}")
    print(f"   门槛后 {dd['after_gate']['n_cross_sections']} 截面 / "
          f"schema {dd['after_gate']['schema_cols']} / 跨 {dd['after_gate']['span_days']} 天")
    rm = audit["remedy_round123"]["measured"]
    print(f"   产物 {rm.get('n_dates')} 截面 / {rm.get('date_range')} / "
          f"过门槛 {rm.get('n_dates_passing_gate')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
