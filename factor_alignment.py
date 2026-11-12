#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""factor_alignment.py —— P0-3 代理因子逐条对齐清单（round142 / 2026-11-12）

用户裁决的对齐顺序（2026-11-12「按你的顺序去改」）：
  1. score_tech_neg  符号/量程差异
  2. score_quality
  3. score_growth
  4. score_size_growth
  P2-2 周期/拥挤惩罚按裁决落在**维度层**、不动唯一内核 composite_total_matrix，
  且排在 P0/P1 跑通之后 —— 本轮**不实现**，仅在清单里登记为后续项。

数据纪律（项目铁律）：
  · 所有 rho 均来自 proxy_vs_prod_rank.py 的实跑产物，本文件不手写任何 rho 数字；
  · 「方向对齐预览值」只是 DMAX−v 换算，不是验收值，npz 未重建前不得据此解除
    PROXY_ONLY 标记、不得进入权重审批链路；
  · 严格 OOS 的唯一判据是 pit_sample_label.py，本清单不另设门槛、不新增第六道门；
  · 权重变更入口由五门 + λ 收缩 + 口径隔离共同决定，本清单只输出对齐状态。

用法：
  python factor_alignment.py [输出json路径]
"""
from __future__ import annotations

import json
import os
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

import proxy_vs_prod_rank as PR  # noqa: E402  唯一判读阈值/实跑产物来源

RANK_JSON = os.path.join(ROOT, "data", "proxy_vs_prod_rank_20261112_round142.json")
OUT_JSON = os.path.join(ROOT, "data", "factor_alignment_20261112.json")

PIT_DEP = "外部多版本历史财务库（用户 2026-11-12 裁决：接入，尚待采购/授权）"

# 按裁决顺序；definitions 只写源码事实，不写结论性数字。
ALIGNMENT_ORDER = [
    {
        "dim": "score_tech_neg",
        "proxy_def": "偏离 20 日高点距离 (px/hi−1)×100 的池内分位，单指标",
        "prod_def": "step2_stockscorer_v2.py:5477  s_tech_neg = 10 − score_tech；"
                    "score_tech 为趋势/MACD/量能/形态四因子综合分",
        "issue": "原面板方向为动量（越接近高点分越高），生产为反号（反转/超跌优先）",
        "depends_on": "重建 data/panel_r124_mat.npz 后重跑秩相关监控",
        "acceptance": "重建后真实 rho ≥ 0.70；且不得改 scoring_core 唯一内核",
        "next_action": "源码已翻向 higher_better=False；待重建面板实测验收",
    },
    {
        "dim": "score_quality",
        "proxy_def": "ROE 单指标池内分位",
        "prod_def": "生产三族质量分（含量纲修正、缺维剔除）",
        "issue": "单指标 vs 多子因子复合，且无 PIT 公告版本",
        "depends_on": PIT_DEP,
        "acceptance": "生产等价 PIT 实现 + 调仓日 rho ≥ 0.70",
        "next_action": "等财务 PIT vintage 落地后逐日重算",
    },
    {
        "dim": "score_growth",
        "proxy_def": "PIT 归母净利润同比池内分位",
        "prod_def": "成长存量 + 方式B（G_q/ΔG_q）+ 三门槛；"
                    "growth_factor_b.anchor_gate 对 cagr3 缺失/≤0 封顶 5.0",
        "issue": "代理缺方式B逐日快照与可持续性门槛，口径差最远",
        "depends_on": PIT_DEP + " + cagr3 历史 + 方式B 逐日入参快照",
        "acceptance": "生产等价 PIT 实现 + 调仓日 rho ≥ 0.70",
        "next_action": "等 PIT 库；同时确认 anchor_gate 在每条链路都装（唯一实现）",
    },
    {
        "dim": "score_size_growth",
        "proxy_def": "流通市值分位（小市值优先）",
        "prod_def": "生产 size_growth 复合分（含行业内中性化 size_ind）",
        "issue": "代理是纯规模单指标，生产为复合分 + 行业中性化",
        "depends_on": "历史 circ_mv 逐日快照 + 生产复合分回溯",
        "acceptance": "生产等价实现 + 调仓日 rho ≥ 0.70",
        "next_action": "排在 quality/growth 之后；优先复用已有 circ_mv 面板字段",
    },
    {
        "dim": "score_dominant",
        "proxy_def": "业务份额回填（组内前 30% 有值），未含生产门禁",
        "prod_def": "生产龙头壁垒分，含门禁",
        "issue": "回填口径未过生产门禁",
        "depends_on": "生产门禁历史快照",
        "acceptance": "调仓日 rho ≥ 0.70",
        "next_action": "顺带项，不占本轮优先级",
    },
    {
        "dim": "score_fund",
        "proxy_def": "长面板**根本没有此维**（dims 为 9 项，不含 fund）",
        "prod_def": "step2_stockscorer_v2.score_fund：FwdPE / PEG / 估值阶梯 /"
                    "roe / gm / cagr3 / q3_info 等复合估值基本面分",
        "issue": "不是「一列全 NaN」，是整列不存在；且 fwd_pe / pe_ladder 需历史版本",
        "depends_on": PIT_DEP + " + 历史一致预期 vintage（FY 口径固定）",
        "acceptance": "补出该维并完成 PIT 对齐后才可解除盲区标记",
        "next_action": "Wind 历史一致预期已实测可点查，但 FY 口径/单位/阶梯不完整，"
                       "接入前不得当作已对齐 vintage",
    },
    {
        "dim": "score_ep",
        "proxy_def": "盈利收益率 = 归母净利润(PIT)/流通市值 分位",
        "prod_def": "生产侧 factor_history.json 快照**无同名字段**（prod_only 里也无）",
        "issue": "生产快照字段断层，缺可对照对象",
        "depends_on": "生产快照补齐 ep 字段或明确字段映射",
        "acceptance": "存在可对照字段后计算 rho ≥ 0.70",
        "next_action": "先做字段映射核查，不插值、不补齐",
    },
]

# 已达标方向一致项：仍属 PROXY_ONLY，只登记不宣称「生产等价」。
ALIGNED_DIMS = ("score_bias60", "score_rsi_rev", "score_lowvol20")

DEFERRED = [
    {
        "item": "P2-2 周期/拥挤惩罚",
        "layer": "维度层内置（用户裁决），不动 composite_total_matrix 唯一内核",
        "status": "DEFERRED",
        "reason": "排在 P0/P1 跑通之后；周期规则已在 growth_factor_b 维度层实现，"
                  "拥挤度尚无实现；本轮不改任何生产内核",
    },
    {
        "item": "常驻 walk-forward 看门接日链",
        "layer": "run_daily_local.sh",
        "status": "DEFERRED_BY_USER_RULING",
        "reason": "用户裁决：先做独立脚本 + 单测，暂不接日链",
    },
]


def _verdict_for(item, by_dim, panel_dims, panel_only):
    """由实跑产物推导状态；不手写数字、不把预览值当验收值。

    ⚠️ 必须区分两种「没数」：
      · 面板根本没有该维（score_fund）        → BLIND_SPOT_NO_PANEL_DIM
      · 面板有、生产快照无同名字段（score_ep）→ NO_PROD_COUNTERPART_FIELD
    二者成因与后续动作完全不同，不能混为一谈。
    """
    d = item["dim"]
    got = by_dim.get(d)
    if not got or "rho_mean" not in got:
        if d in panel_only:
            verdict, status = "NO_PROD_FIELD", "NO_PROD_COUNTERPART_FIELD"
        elif d not in panel_dims:
            verdict, status = "NO_DIM_IN_PANEL", "BLIND_SPOT_NO_PANEL_DIM"
        else:
            verdict = got.get("verdict", "NO_OVERLAP") if got else "NO_OVERLAP"
            status = "NO_PAIRS"
        return {"rho_mean": None, "verdict": verdict, "status": status}
    a = got.get("alignment")
    out = {
        "rho_mean": got["rho_mean"],
        "n_days": got.get("n_days"),
        "verdict": got["verdict"],
        "status": ("SOURCE_FIXED_PANEL_STALE" if a else
                   "BLOCKED_RED_ALERT" if got["rho_mean"] < PR.THRESHOLD else
                   "PROXY_ONLY_NEEDS_REVIEW"),
    }
    if a:
        out["direction_alignment"] = {
            "rho_if_aligned": a["rho_if_direction_aligned"],
            "verdict_if_aligned": a["verdict_if_aligned"],
            "panel_npz_rebuilt": a["panel_npz_rebuilt"],
            "is_acceptance_value": False,
            "note": a["note"],
        }
    return out


def build_plan(rank_path=RANK_JSON):
    with open(rank_path, encoding="utf-8") as f:
        rank = json.load(f)
    by_dim = rank.get("by_dim", {})
    panel_dims = rank.get("panel", {}).get("dims", [])
    panel_only = rank.get("panel", {}).get("panel_only_dims", [])

    items = []
    for i, it in enumerate(ALIGNMENT_ORDER, 1):
        row = dict(it)
        row["order"] = i
        row.update(_verdict_for(it, by_dim, panel_dims, panel_only))
        items.append(row)

    aligned = []
    for d in ALIGNED_DIMS:
        g = by_dim.get(d, {})
        aligned.append({
            "dim": d,
            "rho_mean": g.get("rho_mean"),
            "n_days": g.get("n_days"),
            "verdict": g.get("verdict", "NO_OVERLAP"),
            "status": "DIRECTION_CONSISTENT_PROXY_ONLY",
            "note": "方向一致且 rho 高，但仍是代理面板；"
                    "低样本天数（如 lowvol20）不得外推为长期等价",
        })

    n_red = sum(1 for x in items
                if x["rho_mean"] is None or x["rho_mean"] < PR.THRESHOLD)
    return {
        "purpose": "P0-3 代理因子逐条对齐清单（按 2026-11-12 用户裁决顺序）",
        "generated_at_note": "由 factor_alignment.py 读取实跑产物生成",
        "source": {
            "rank_monitor": os.path.relpath(rank_path, ROOT),
            "threshold": PR.THRESHOLD,
            "equivalence_threshold": PR.EQUIV_THRESHOLD,
            "threshold_source": "probe_fund_calib.py:229-233 唯一判读口径",
        },
        "honesty_boundary": (
            "所有 rho 来自实跑产物，未手写；方向对齐值仅为 DMAX−v 换算预览，"
            "panel_r124_mat.npz 未重建，不得当验收值；长面板 factor_parity=False / "
            "PROXY_ONLY，本清单不构成权重审批依据，也不新增第六道准入门；"
            "严格 OOS 判据唯一实现为 pit_sample_label.py。"),
        "summary": {
            "n_items": len(items),
            "n_red_or_pending": n_red,
            "n_direction_consistent_proxy": len(aligned),
            "all_dims_aligned": False,
            "weight_change_allowed": False,
            "weight_change_reason": (
                "PROXY_ONLY 口径 + 严格 OOS 干净截面未达门槛 + G5 未过 / λ≈0；"
                "权重入口由五门与 walkforward_watchdog 独立判定，本清单不单独放行"),
        },
        "alignment_order": items,
        "direction_consistent_proxy": aligned,
        "deferred": DEFERRED,
    }


def main():
    out = sys.argv[1] if len(sys.argv) > 1 else OUT_JSON
    r = build_plan()
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        json.dump(r, f, ensure_ascii=False, indent=1)
    print("对齐顺序（rho 来自实跑；方向对齐值非验收值）")
    print("%-3s %-22s %-9s %-9s %s" % ("#", "dim", "rho", "rho_align", "status"))
    for x in r["alignment_order"]:
        a = x.get("direction_alignment") or {}
        print("%-3d %-22s %-9s %-9s %s" % (
            x["order"], x["dim"],
            "n/a" if x["rho_mean"] is None else "%.4f" % x["rho_mean"],
            "n/a" if a.get("rho_if_aligned") is None
            else "%.4f" % a["rho_if_aligned"],
            x["status"]))
    print("\n方向一致的代理维（仍 PROXY_ONLY）:")
    for x in r["direction_consistent_proxy"]:
        print("  %-22s rho=%s n_days=%s" % (x["dim"], x["rho_mean"], x["n_days"]))
    print("\nall_dims_aligned =", r["summary"]["all_dims_aligned"],
          " weight_change_allowed =", r["summary"]["weight_change_allowed"])
    print("written:", out)


if __name__ == "__main__":
    main()
