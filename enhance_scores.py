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

W_NEWS, W_TECH, W_FUND, W_THEME = 0.20, 0.20, 0.30, 0.30

# v3.6: 十维权重的兜底先验（与 factor_weights.json 同源）。
# 计算 total 必须走十维公式，禁止再用旧的四维 0-20 量纲公式——
# 否则单独重跑本脚本会把 total 打回 20 分制，与 apply_sector_momentum 的
# 百分制十维口径冲突（历史事故：页面出现 11~18 与 28~93 两套量纲并存）。
_W_PRIOR_10D = {
    "score_fund": 0.34, "score_news": 0.22, "score_sector": 0.10,
    "score_oversold60": 0.055, "score_bias60": 0.055, "score_rsi_rev": 0.055,
    "score_vol_pulse": 0.055, "score_overbought": 0.02,
    "score_theme_intrinsic": 0.08, "score_attention": 0.02,
}


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


def _calc_total_10d(r):
    """按十维权重重算百分制 total（与 apply_sector_momentum 的 <has_10d> 分支一致）。"""
    w = _load_w10()
    s = 0.0
    for k, v in w.items():
        if v <= 0:
            continue
        s += float(r.get(k) or 0) * v
    return round(s * 20, 2)


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

    changed = 0
    for r in results:
        base_fund = float(r.get("score_fund") or 0)
        base_news = float(r.get("score_news") or 0)
        base_theme = float(r.get("score_theme") or 0)
        tech = float(r.get("score_tech") or 0)
        growth = r.get("growth")
        cagr3 = r.get("cagr3")

        # ---- 1) 基本面一阶导 / 二阶导 ----
        d1 = _map_growth_deriv1(growth)
        delta = None
        if growth is not None and cagr3 is not None:
            try:
                delta = float(growth) - float(cagr3)
            except Exception:
                delta = None
        d2 = _map_delta_deriv2(delta)
        # 新基本面 = 60% 原基础分(估值/质量/持续性) + 20% 一阶导 + 20% 二阶导
        new_fund = _clamp(base_fund * 0.6 + d1 * 0.2 + d2 * 0.2)

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
        r["score_fund"] = round(new_fund, 2)
        r["score_news"] = round(ai_news, 2)
        r["score_theme"] = round(ai_theme, 2)
        total = _calc_total_10d(r)
        rating, advice = _rate_100(total)
        r["fund_detail"] = {
            "deriv1_growth": growth, "deriv1_score": round(d1, 2),
            "cagr3": cagr3, "deriv2_delta": (round(delta, 1) if delta is not None else None),
            "deriv2_score": round(d2, 2),
        }
        r["ai_applied"] = ai_applied
        r["ai_theme_label"] = ai_label
        r["ai_news"] = round(ai_news, 2)
        r["ai_theme_heat"] = round(ai_theme, 2)
        r["ai_theme_day_mod"] = round(_day_mod, 3)
        if ai_applied:
            changed += 1

    # 重排 + 重算 stats
    results.sort(key=lambda x: (-x["total"], -(x.get("turnover") or 0)))
    from collections import Counter
    rc = Counter(r["rating"] for r in results)
    sec_avg = {}
    for r in results:
        sec_avg.setdefault(r["sector"], []).append(r["total"])
    data["results"] = results
    data["model"] = "stock-scorer v2.30 (AI-augmented)"
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
