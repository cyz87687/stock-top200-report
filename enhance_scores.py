#!/usr/bin/env python3
"""
后置增强脚本：在 step2 自动评分结果之上，叠加 AI 层结论（由 ai_assessment.json 提供）
实现 4 项增强：
  1) 基本面新增「业绩一阶导(增速)」与「业绩二阶导(加速度)」分项并显性披露
  2) 根据 AI 题材分析结论，对 消息面 / 题材热度 因子赋分（题材级覆盖+混合）
  3) 回写增强后的维度分，按十维权重（读 factor_weights.json）×20 重算百分制总分与评级
  4) AI 的 commentary / watch_directions / model_summary 由 ai_assessment.json 注入报告

用法: python3 enhance_scores.py [scored_json] [ai_assessment_json]
  - 默认 scored_json = 最新的 top200_scored_*.json
  - 默认 ai_assessment_json = ai_assessment.json
  - 原地覆盖 scored_json（仅改 results/stats，保留 market_review 等）
数据纪律：所有增强均基于 scored json 中真实字段(growth/cagr3/sub_theme/score_*)，不编造。
"""
import json
import os
import sys
import glob

# v4.10（2026-10-27 P0-4）：复合分量尺唯一实现来源（分母恒定 + 缺维中性填充）
import scale_calc

W_NEWS, W_TECH, W_FUND, W_THEME = 0.20, 0.20, 0.30, 0.30

# v3.6: 十维权重的兜底先验（与 factor_weights.json 同源）。
# 计算 total 必须走十维公式，禁止再用旧的四维 0-20 量纲公式——
# 否则单独重跑本脚本会把 total 打回 20 分制，与 apply_sector_momentum 的
# 百分制十维口径冲突（历史事故：页面出现 11~18 与 28~93 两套量纲并存）。
_W_PRIOR_10D = {"score_fund": 0.200000, "score_news": 0.000000, "score_theme_neg": 0.000000, "score_size": 0.000000, "score_sector": 0.000000, "score_quality": 0.100000, "score_growth": 0.100000, "score_tech_neg": 0.100000, "score_oversold60": 0.000000, "score_bias60": 0.050000, "score_rsi_rev": 0.050000, "score_vol_pulse": 0.000000, "score_overbought": 0.000000, "score_theme_intrinsic": 0.000000, "score_attention": 0.000000, "score_lowvol20": 0.000000, "score_ep": 0.100000, "score_size_ind": 0.000000, "score_size_growth": 0.200000, "score_dominant": 0.100000}  # v4.14-evidence-aligned：quality 0.212→0.12（实证 h20 IC −0.147 负），fund 0.12→0.242（消融 t=+4.41 唯一>4），# tech_neg 0.06→0.03（σ 贡献仅 2.1% 近乎常数）；Σ=1.0
def _load_w10():
    """读 factor_weights.json 的 weights（十维）；失败回退兜底先验。"""
    try:
        p = os.path.join(os.path.dirname(os.path.abspath(__file__)), "factor_weights.json")
        with open(p, encoding="utf-8") as f:
            w = (json.load(f) or {}).get("weights") or {}
        if all(k in w for k in _W_PRIOR_10D):
            vals = {k: float(w[k]) for k in _W_PRIOR_10D}
            if abs(sum(vals.values()) - 1.0) <= 0.02 and all(v >= 0 for v in vals.values()):
                return vals
    except Exception:
        pass
    return dict(_W_PRIOR_10D)


def _load_dim_max():
    """读 factor_weights.json 的 dim_max（各维满分量程）；失败回退内置默认。

    ⚠️ 2026-10-26 P0：量程不统一是总分层最深的静默缺陷。12 维为 0-5 制，
    只有 size/quality/growth/tech_neg 为 0-10 制；原公式按「每维满分 10」加权，
    导致天花板仅 67.5 且名义权重被扭曲（tech_neg 15%→22.2%，反转块 35%→26%）。
    回退默认值必须与 factor_weights.json 的 dim_max 一致，否则静默回退旧口径。
    """
    _DEFAULT = {
        "score_fund": 5.0, "score_news": 5.0, "score_theme_neg": 10.0,
        "score_size": 10.0, "score_sector": 5.0, "score_quality": 10.0,
        "score_growth": 10.0, "score_tech_neg": 10.0, "score_oversold60": 5.0,
        "score_bias60": 5.0, "score_rsi_rev": 5.0, "score_vol_pulse": 5.0,
        "score_overbought": 5.0, "score_theme_intrinsic": 5.0, "score_attention": 5.0,
        # v4.8/v4.9 池内分位族（量程 0-10）
        "score_lowvol20": 10.0, "score_ep": 10.0, "score_size_ind": 10.0,
        # v4.13-size2d：市值双维度拆分（A 小盘成长 / B 业务份额龙头），量程 0-10
        "score_size_growth": 10.0, "score_dominant": 10.0,
    }
    try:
        p = os.path.join(os.path.dirname(os.path.abspath(__file__)), "factor_weights.json")
        with open(p, encoding="utf-8") as f:
            dm = (json.load(f) or {}).get("dim_max") or {}
        out = dict(_DEFAULT)
        for k in _DEFAULT:
            v = dm.get(k)
            if isinstance(v, (int, float)) and v > 0:
                out[k] = float(v)
        return out
    except Exception:
        return dict(_DEFAULT)


def _calc_total_10d(r):
    """按十维权重重算百分制 total（与 apply_sector_momentum 的同名分支一致）。

    v4.1: 新增 score_theme_neg / score_size 两维。score_size 可能为 None（该股缺市值）。
    v4.4: 新增 quality/growth/tech_neg。
    v4.9: 新增 ep/size_ind。

    ⚠️ 2026-10-26 P0：量程不统一（12 维 0-5 制 / 4 维 0-10 制）导致
    ① 天花板仅 67.5 分 → S≥85/A≥65 永不可达；② 名义权重被扭曲
    （tech_neg 15%→22.2%，反转块 35%→26%）。汇总前按 DIM_MAX 归一化到 0-10。

    ⚠️ 2026-10-27 P0-4：量尺统一（详见 scale_calc.py 模块头）。
    修复前分母是「该股可用维权重之和」，随缺维数量变化 → 同页面不同股票不同尺子，
    缺维越多分数被抬得越高（实测放大 1.00~1.33 倍，最高虚高 16.33 分）。
    修复后分母恒为 Σ全部生效权重，缺维按中性分 DMAX/2 填充并在 r['neutral_filled']
    留痕。实测 B_neutral vs 现行复合 IC 配对 t −0.53(h5)/+1.06(h20) → 零损失；
    而「缺维=0 分」会使 IC 从 +0.1520 崩到 +0.0926（t −5.76）→ 中性填充是唯一可行解。
    """
    w = _load_w10()
    dm = _load_dim_max()
    parts = [(k, r.get(k), v) for k, v in w.items() if v > 0]
    _miss = []
    total, _m = scale_calc.composite_total(
        parts, dm, weights={k: v for k, v in w.items() if v > 0}, missing_out=_miss)
    if _m:
        r["neutral_filled"] = list(_m)
        r["composite_scale"] = "uniform_v1"
    elif "neutral_filled" in r:
        r["neutral_filled"] = []
    return total


def _rate_100(total):
    if total >= 85:
        return "S", "优先买入，可重仓"
    if total >= 65:
        return "A", "逢低加仓，重点关注"
    if total >= 45:
        return "B", "波段操作，轻仓参与"
    if total >= 25:
        return "C", "观望，不新开仓"
    return "D", "坚决回避，立即卖出"


def _clamp(x, lo=0.0, hi=5.0):
    return max(lo, min(hi, x))


def _map_growth_deriv1(growth):
    """一阶导(业绩增速) → 0-5：最新同比增速水平"""
    if growth is None:
        return 3.0  # 数据缺失给中性
    if growth >= 50:
        return 5.0
    if growth >= 30:
        return 4.0
    if growth >= 20:
        return 3.0
    if growth >= 10:
        return 2.0
    if growth >= 0:
        return 1.0
    return 0.0


def _map_delta_deriv2(delta):
    """二阶导(业绩加速度 = growth - cagr3) → 0-5：相对三年CAGR的趋势变化"""
    if delta is None:
        return 3.0
    if delta >= 20:
        return 5.0
    if delta >= 5:
        return 4.0
    if delta >= -5:
        return 3.0
    if delta >= -20:
        return 2.0
    return 1.0


def _find_theme(name, themes):
    """按题材 stocks 名单精确匹配该个股所属 AI 题材"""
    if not name:
        return None
    for tlabel, t in themes.items():
        stocks = t.get("stocks") or []
        if name in stocks:
            return tlabel, t
    return None


def main():
    here = os.path.dirname(os.path.abspath(__file__))
    scored_arg = sys.argv[1] if len(sys.argv) > 1 else None
    ai_arg = sys.argv[2] if len(sys.argv) > 2 else os.path.join(here, "ai_assessment.json")

    if scored_arg is None:
        fs = [f for f in sorted(glob.glob(os.path.join(here, "top200_scored_*.json")), reverse=True)
              if "_watch_" not in f and ".bak" not in f]
        scored_arg = fs[0] if fs else None
    if not scored_arg or not os.path.exists(scored_arg):
        print("❌ 未找到 scored json")
        return 1
    if not os.path.exists(ai_arg):
        print(f"⚠️ 未找到 ai_assessment.json({ai_arg})，仅做基本面导数增强")
        ai = {"themes": {}}
    else:
        with open(ai_arg, "r", encoding="utf-8") as f:
            ai = json.load(f)
    themes = ai.get("themes") or {}

    with open(scored_arg, "r", encoding="utf-8") as f:
        data = json.load(f)
    results = data.get("results", [])

    # ---- 成长因子·方式B（round106 口径，growth_factor_b 唯一实现）----
    #   单季同比 G_q = Q/Q_ly−1；一阶导 = 单季环比增速 QoQ；二阶导 = ΔG_q（同比增速之差, pp）。
    #   增速一律扣非净利润；负基数增速无定义 → 中性 0.5 + 留痕（禁 abs 分母失真）。
    _gf_rows = [{"code": x.get("code"), "name": x.get("name"),
                 "sector_parent": x.get("sector_parent")} for x in results]
    try:
        import growth_factor_b as _gfb_mod
        _gf_map = _gfb_mod.compute_growth(_gf_rows, style="steady", as_of=data.get("date"))
        _gf_meta = _gf_map.pop("__meta__", {}) or {}
        _gfb_ok = True
        print(f"🧬 方式B 成长因子: {_gf_meta.get('n_valid')}/{_gf_meta.get('n_pool')} 只有效"
              f"（三季报口径 {_gf_meta.get('n_q3_period')} 只，style={_gf_meta.get('style')}）")
    except Exception as _ge:
        # 🚨 禁静默降级：模块失败必须大声告警 + fund_detail 标 modeB_failed
        print(f"🚨 方式B 成长因子计算失败（回退旧口径 deriv，fund_detail 标 modeB_failed）: {_ge!r}")
        _gf_map, _gf_meta, _gfb_ok = {}, {}, False

    changed = 0
    for r in results:
        # ⚠️ 2026-10-27 P0：score_fund 可能是 None（= 缺有效估值 PE/FwdPE，本维不计分，
        #   由 step2 的 score_fund 兜底改造引入）。原写法 `float(... or 0)` 会把 None
        #   当成 0 分参与 `base_fund*0.6 + d1*0.2 + d2*0.2`，再在下方写回文件 ——
        #   等于用「增速一阶导/二阶导」把一只没有估值数据的股票重新伪造出一个 fund 分数，
        #   凭空加分路径②（路径①是 step2 的增速兜底）。必须保持 None 透传。
        _base_fund_raw = r.get("score_fund")
        fund_available = isinstance(_base_fund_raw, (int, float)) and not isinstance(_base_fund_raw, bool)
        base_fund = float(_base_fund_raw) if fund_available else 0.0
        base_news = float(r.get("score_news") or 0)
        base_theme = float(r.get("score_theme") or 0)
        tech = float(r.get("score_tech") or 0)
        growth = r.get("growth")
        cagr3 = r.get("cagr3")

        # ---- 1) 基本面一阶导 / 二阶导（方式B·round106 口径）----
        #   一阶导 = 单季环比增速 QoQ（deriv1）；二阶导 = ΔG_q 同比增速之差 pp（deriv2）。
        #   d1/d2 取方式B的行业中性 rank×5（0-5，对齐 new_fund 量程），
        #   旧口径（_map_growth_deriv1/growth−cagr3）仅在模块失败时兜底。
        _c6 = str(r.get("code") or "").strip().lower()
        if len(_c6) > 6 and _c6[:2] in ("sh", "sz", "bj"):
            _c6 = _c6[2:]
        _gfb = _gf_map.get(_c6) or {}
        if _gfb_ok and _gfb:
            d1 = float(_gfb.get("deriv1_score", 3.0))
            d2 = float(_gfb.get("deriv2_score", 3.0))
            _d1_disp = _gfb.get("deriv1_growth_pct")   # 一阶导 = QoQ 单季环比增速，% 展示值
            _d2_disp = _gfb.get("deriv2_delta_pt")     # 二阶导 = ΔG_q 同比增速之差，pp 展示值
        else:
            d1 = _map_growth_deriv1(growth)
            delta = None
            if growth is not None and cagr3 is not None:
                try:
                    delta = float(growth) - float(cagr3)
                except Exception:
                    delta = None
            d2 = _map_delta_deriv2(delta)
            _d1_disp, _d2_disp = growth, delta
        # 新基本面 = 60% 原基础分(估值/质量/持续性) + 20% 一阶导 + 20% 二阶导
        # ⚠️ 2026-10-27 P0：原基础分不可得（score_fund=None）时不能只留 0.2*d1+0.2*d2 ——
        #   那会用增速导数造出一个 0~2 的「伪估值分」，且权重结构（60%锚缺失）也崩了。
        #   保持 None，让 _calc_total_10d 从分母剔除该维（与 ep/size_ind 同构）。
        new_fund = (_clamp(base_fund * 0.6 + d1 * 0.2 + d2 * 0.2) if fund_available else None)

        # ---- 2) AI 题材结论赋分 (消息面 / 题材热度) ----
        matched = _find_theme(r.get("name"), themes)
        ai_news, ai_theme, ai_label, ai_applied = base_news, base_theme, "", False
        if matched:
            tlabel, t = matched
            t_news = t.get("news")
            t_heat = t.get("heat")
            if t_news is not None:
                ai_news = _clamp(0.7 * float(t_news) + 0.3 * base_news)
            if t_heat is not None:
                ai_theme = _clamp(0.7 * float(t_heat) + 0.3 * base_theme)
            else:
                ai_theme = base_theme
            ai_label = tlabel
            ai_applied = True
        # 当日涨跌修正: 题材热度必须与当日真实涨跌幅方向一致(涨停+1.0 / 跌停-1.0)。
        # 修复项——此前 ai_theme_heat 完全等于算法原始分(base_theme), 且 AI 覆盖层未生效,
        # 导致大涨题材(PCB/CXO)热度反而低于当日大跌的光模块; 现叠加个股当日涨跌幅偏移纠偏。
        _pct = r.get("pct_chg")
        _day_mod = 0.0
        if _pct is not None:
            try:
                _day_mod = _clamp(float(_pct) / 10.0, -1.0, 1.0)  # 涨跌幅映射到[-1,+1]
            except Exception:
                _day_mod = 0.0
        ai_theme = _clamp(ai_theme + _day_mod)

        # ---- 3) 回写增强后的维度分，再用十维公式重算总分与评级 ----
        # ⚠️ 2026-10-27 P0：new_fund 可能为 None（缺估值，本维不计分）→ 保持 None 落库，
        #   round(None,2) 会 TypeError，页面据 None 显示「不计分」而非 0 分。
        r["score_fund"] = (round(new_fund, 2) if new_fund is not None else None)
        r["score_news"] = round(ai_news, 2)
        r["score_theme"] = round(ai_theme, 2)
        if _gfb_ok and _gfb:
            # 成长维度改由方式B给出（round105 P0-2 起 W_GROWTH=0.05 进总分；缺失=None 不计分）
            _sg = _gfb.get("score_growth")
            r["score_growth"] = (round(float(_sg), 2) if isinstance(_sg, (int, float)) else None)
        total = _calc_total_10d(r)
        rating, advice = _rate_100(total)
        # ⚠️ round105 P0-1（2026-10-31）：算完必须写回。此前 total/rating/advice 算完即丢，
        #   落盘 total 一直是 step2 口径（fund 用混入 d1/d2 **之前**的 base 分），
        #   导致展示 score_fund 与 total 口径漂移、「业绩越差虚高越多」
        #   （QoQ 惩罚只停在展示层：生益 +1.95 / 中际 +2.49 / 巨人 +3.37）。
        #   三行写回闭合口径；round105 反事实：仅此修复巨人 83.60(#1)→80.23(#4)。
        r["total"] = total
        r["rating"] = rating
        r["advice"] = advice
        _fd = {
            "deriv1_growth": (round(_d1_disp, 1) if isinstance(_d1_disp, (int, float)) else None),
            "deriv1_score": round(d1, 2),
            "cagr3": cagr3,
            "deriv2_delta": (round(_d2_disp, 1) if isinstance(_d2_disp, (int, float)) else None),
            "deriv2_score": round(d2, 2),
        }
        if _gfb_ok and _gfb:
            _fd["modeB"] = {
                "g_q": (round(_gfb["g_q"], 4) if isinstance(_gfb.get("g_q"), (int, float)) else None),
                "qoq": (round(_gfb["qoq"], 4) if isinstance(_gfb.get("qoq"), (int, float)) else None),
                "accel_pp": (round(_gfb["accel_pp"], 2) if isinstance(_gfb.get("accel_pp"), (int, float)) else None),
                "neg_base": bool(_gfb.get("neg_base")), "turnaround": bool(_gfb.get("turnaround")),
                "qoq_chg": (round(_gfb["qoq_chg"], 4) if isinstance(_gfb.get("qoq_chg"), (int, float)) else None),
                "period": _gfb.get("period"), "is_cycle": bool(_gfb.get("is_cycle")),
                "rank_gq": _gfb.get("rank_gq"), "rank_qoq": _gfb.get("rank_qoq"),
                "rank_accel": _gfb.get("rank_accel"),
                "constraints": _gfb.get("constraints_hit"),
                "score_growth_100": _gfb.get("score_growth_100"),
                # ---- round108 周期规则留痕（审计/页面双用，必须透传否则扣分不可解释）----
                "sw_l2": _gfb.get("sw_l2"),
                "rule_gate": _gfb.get("rule_gate"),
                "rule1_hit": bool(_gfb.get("rule1_hit")),
                "rule2_hit": bool(_gfb.get("rule2_hit")),
                "rule_ded_pct": _gfb.get("rule_ded_pct"),
                "accel_demeaned_pp": _gfb.get("accel_demeaned_pp"),
                "gm_rise_pp": (round(_gfb["gm_rise_pp"], 2)
                               if isinstance(_gfb.get("gm_rise_pp"), (int, float)) else None),
                "gm_reason": _gfb.get("gm_reason"),
            }
        else:
            _fd["modeB_failed"] = True
        r["fund_detail"] = _fd
        r["ai_applied"] = ai_applied
        r["ai_theme_label"] = ai_label
        r["ai_news"] = round(ai_news, 2)
        r["ai_theme_heat"] = round(ai_theme, 2)
        r["ai_theme_day_mod"] = round(_day_mod, 3)
        if ai_applied:
            changed += 1

    # ---- 业绩预期差三档（超预期/符合预期/低于预期，2026-10-31 老大指令）----
    #   earnings_surprise 唯一实现：预测增速 vs 实际单季同比（口径对齐扣非/归母）。
    #   权重 0 不进总分；只写 r["es"] 字段。失败必须大声告警，禁静默降级。
    try:
        import earnings_surprise as _es_mod
        _es_map = _es_mod.compute_es_map({x.get("name"): x.get("code") for x in results})
        _es_n = 0
        for r in results:
            _e = _es_map.get(r.get("name"))
            if _e:
                r["es"] = {k: _e[k] for k in (
                    "tier", "delta_pp", "band", "fwd_mid", "act_yoy", "act_np_q",
                    "strength", "method", "caliber", "period", "source", "flags",
                    "dev_ratio", "note")}
                _es_n += 1
        print(f"🎯 业绩预期差三档: {_es_n}/{len(results)} 只标注"
              f"（超预期 {sum(1 for r in results if (r.get('es') or {}).get('tier') == '超预期')} / "
              f"符合 {sum(1 for r in results if (r.get('es') or {}).get('tier') == '符合预期')} / "
              f"低于 {sum(1 for r in results if (r.get('es') or {}).get('tier') == '低于预期')}）")
    except Exception as _ese:
        # 🚨 禁静默降级：es 缺失时页面该徽标消失即可，但必须留可见痕迹
        print(f"🚨 业绩预期差模块失败（本批不写 es 字段）: {_ese!r}")

    # 重排 + 重算 stats
    results.sort(key=lambda x: (-x["total"], -(x.get("turnover") or 0)))
    from collections import Counter
    rc = Counter(r["rating"] for r in results)
    sec_avg = {}
    for r in results:
        sec_avg.setdefault(r["sector"], []).append(r["total"])
    data["results"] = results
    # ⚠️ 2026-10-31 修复：原写法无条件覆写 model="v2.30"，会把 step2 已落的
    #   实际评分版本（如 v4.15-constraint-cap）改写成假版本 —— 违反 gen_report 的
    #   「评分层版本以 scored json 的 model 字段为准，重跑不虚标」口径。
    #   现仅在缺失时兜底填旧串，已有版本保持不动。
    data["model"] = data.get("model") or "stock-scorer v2.30 (AI-augmented)"
    data["stats"]["rating_dist"] = {k: rc.get(k, 0) for k in "SABCD"}
    data["stats"]["sector_avg"] = {
        s: {"count": len(v), "avg": round(sum(v) / len(v), 1)}
        for s, v in sorted(sec_avg.items(), key=lambda x: -sum(x[1]) / len(x[1]))
    }
    # 注入 AI 文本层（若提供）
    if ai.get("commentary"):
        mr = data.setdefault("market_review", {})
        mr["ai_commentary"] = ai["commentary"]
    if ai.get("watch_directions"):
        data["ai_watch_directions"] = ai["watch_directions"]
    if ai.get("model_summary"):
        data["ai_model_summary"] = ai["model_summary"]

    with open(scored_arg, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    print(f"✅ 增强完成: {scored_arg}")
    print(f"   AI赋分覆盖个股: {changed}/{len(results)}")
    print(f"   新评级分布: {dict(rc)}")
    top5 = results[:5]
    print("   Top5: " + " / ".join(f"{r['name']}({r['total']:.1f},{r['rating']})" for r in top5))
    return 0


if __name__ == "__main__":
    sys.exit(main())
