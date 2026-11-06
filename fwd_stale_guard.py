# -*- coding: utf-8 -*-
"""FY2026 卖方预测陈旧守卫（fwd stale guard）。

背景（2026-11-06，老大裁决）：
  华正新材 fwd_pe 36.2 > pe 22.9 并非「预期下修」，而是卖方 2026 年报预测
  未更新——THS/东财/Wind 三源一致预期均停在 ~6.42 亿（THS 仅 1 家机构），
  而公司 2026 前三季度归母净利已实现 8.00 亿（三季报 2026-10-22 披露）。
  全年一致预期低于已实现利润，在「Q4 不巨亏」前提下逻辑上不成立 → 判定陈旧。

规则（保守、确定性、只用真实来源数字，禁止任何自由参数）：
  ① 陈旧判定（全部满足才进入候选）：
     · 一致预期 FY2026E 归母净利（亿元）> 0
     · 最新已披露 2026 报告期归母累计净利（亿元）> 0（东财 fin_cache_em 实际数）
     · 一致预期 < 已实现累计（全年预测低于已到手利润 = 被事实证伪）
     · 核心增速 > 0（em 报告期同比；增长为负时「预测低于累计」可能是真实
       Q4 亏损预期，只跳过不误伤）
     · 重大性分层（MATERIAL_FALSIFY_RATIO=0.9）：一致预期 < 累计×0.9（implied
       Q4 亏损 > 一成累计）→ stale 改数；介于 0.9~1.0 倍 → stale_soft 只留痕
  ② 锚定 anchor = max(一致预期, 已实现累计 × 年化系数)
     年化系数 = 12 / 已实现月数（Q1=12/3=4，H1=12/6=2，Q3=12/9=4/3）——
     确定性年化，不含任何季节性/景气假设；Q4=Q3 等乐观口径一律不用。
  ③ 只覆盖 2026 阶梯位（2027/2028 保留卖方原值）；一致预期原值必须留档
     （fwd_2026_consensus），禁止静默覆盖。每个受影响个股输出：
     fwd_stale / fwd_stale_soft / fwd_2026_src / fwd_2026_consensus /
     fwd_2026_ytd / fwd_2026_period / fwd_2026_anchor。

已知残余风险（披露，不隐瞒）：
  · 若个别公司 Q4 真实出现大额亏损/减值使 FY < 已实现累计，锚定会偏高——
    但此时一致预期 < 已实现累计本身就是「预测 Q4 亏损」的强主张，触发条件
    ①已要求核心增速 > 0，误伤面收窄；行级 fwd_stale 字段可逐只人工复核。
  · iFinD 文本口径 fwd_pe 优先于 computed_fwd_pe（step2 既有优先级不变），
    本守卫只修 THS 阶梯派生值；受影响个股以行级留痕为准。

单一实现：step2_stockscorer_v2（评分）与 refresh_fwd_2026（审计产物）共用本
模块，禁止在别处再写锚定公式。
"""
from __future__ import annotations

import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))
EM_FIN_CACHE_DIR = os.path.join(HERE, "data", "fin_cache_em")

# 重大性阈值：implied Q4 亏损 > 一成 × 已实现累计 才判定「被事实证伪」并改数。
# 校准：已实现累计≈3个标准季（0.33亿×3），implied Q4 < −0.10×累计 ≈ −30% 单季均值，
# 已超出单季常规波动；缺口更小的（一致预期≈已实现累计）只留痕（stale_soft）不改数，
# 因其更可能是「真实 Q4 走弱预期」而非预测滞后（多机构覆盖尤甚）。
MATERIAL_FALSIFY_RATIO = 0.9

# 报告期 → 已实现月数（仅 2026 年当期报告期参与年化）
_PERIOD_MONTHS = {"0331": 3, "0630": 6, "0930": 9, "1231": 12}


def months_of_period(period):
    """'20260930' → 9；非 2026 年或非法格式 → None。"""
    p = str(period or "")
    if len(p) != 8 or not p.isdigit() or not p.startswith("2026"):
        return None
    return _PERIOD_MONTHS.get(p[4:])


def annualize_factor(period):
    """已实现累计 → 全年口径的确定性年化系数；不适用返回 None。"""
    m = months_of_period(period)
    if not m or m >= 12:          # 年报期本身就是全年数，不需要年化
        return None
    return 12.0 / m


def extract_em_nm_ytd(sym, cache_dir=None):
    """从东财财务摘要缓存取归母净利润 2026 年最新报告期累计值（元）。

    返回 {'period': '20260930', 'ytd_yuan': 799955891.63} 或 None。
    口径：归母净利润（与卖方一致预期同口径），优先「常用指标」行；
    扣非净利润刻意**不用**（一致性预期是归母口径，混用会错位）。
    """
    d = cache_dir or EM_FIN_CACHE_DIR
    p = os.path.join(d, "%s.json" % str(sym).strip())
    if not os.path.exists(p):
        return None
    try:
        with open(p, encoding="utf-8") as f:
            recs = (json.load(f) or {}).get("records") or []
    except Exception:
        return None
    best = None
    for r in recs:
        if str(r.get("指标") or "") != "归母净利润":
            continue
        if str(r.get("选项") or "") != "常用指标":   # 同值重复行只取主口径
            continue
        for k, v in r.items():
            ks = str(k)
            if months_of_period(ks) is None:
                continue
            try:
                val = float(v)
            except Exception:
                continue
            if best is None or ks > best[0]:
                best = (ks, val)
    if not best:
        return None
    return {"period": best[0], "ytd_yuan": best[1]}


def _get_ladder_year(ladder, year):
    """阶梯年键兼容 int/str（json.dump 后 int 键变 str）。返回 (键, 值) 或 None。"""
    if not isinstance(ladder, dict):
        return None
    for key in (year, str(year)):
        if key in ladder:
            return key, ladder[key]
    return None


def _consensus_2026_yi(code, consensus_by_code, ladder, mktcap_yi):
    """一致预期 FY2026E 净利（亿元）。优先原始 THS 均值；缺失则按阶梯反推
    （mktcap/pe，与建阶梯同一公式，标注 implied）。返回 (值, src) 或 (None, None)。"""
    rec = (consensus_by_code or {}).get(code)
    if isinstance(rec, dict):
        for key in (2026, "2026"):
            v = rec.get(key)
            if isinstance(v, (int, float)) and v > 0:
                return float(v), "ths_mean"
    elif isinstance(rec, (int, float)) and rec > 0:
        return float(rec), "ths_mean"
    hit = _get_ladder_year(ladder, 2026)
    mk = (mktcap_yi or {}).get(code) or (mktcap_yi or {}).get(str(code)[2:])
    if hit and isinstance(mk, (int, float)) and mk > 0 and hit[1] and hit[1] > 0:
        return float(mk) / float(hit[1]), "implied_from_ladder"
    return None, None


def guard_pe_ladder(pe_ladder, computed_fwd_pe, consensus_by_code,
                    em_info_by_code, mktcap_yi, growth_by_code=None,
                    cache_dir=None):
    """对阶梯/fwd_pe 原地打陈旧锚定补丁，返回 {code: meta}（仅触发个股）。

    pe_ladder        : {code: {year: pe}}（原地修改 2026 位）
    computed_fwd_pe  : {code: pe}（原地同步）
    consensus_by_code: {code: {year: 亿}}（step2 的 fwd_pe_map，只读）
    em_info_by_code  : {code: {'cur': 元, 'period': 'yyyymmdd', 'growth': %}}
                       （step2 的 em_growth_map，只读；cur 为扣非累计不使用，
                        归母累计另从 fin_cache_em 缓存读取）
    mktcap_yi        : {code 或 6位码: 流通市值(亿元)}
    growth_by_code   : 可选 {code: 增速%}；缺省用 em_info 的 growth
    """
    meta = {}
    if not pe_ladder:
        return meta
    for code, ladder in list(pe_ladder.items()):
        hit = _get_ladder_year(ladder, 2026)
        if not hit or not (isinstance(hit[1], (int, float)) and hit[1] > 0):
            continue
        cons, cons_src = _consensus_2026_yi(code, consensus_by_code, ladder, mktcap_yi)
        if cons is None:
            continue
        em = (em_info_by_code or {}).get(code) or {}
        ytd = extract_em_nm_ytd(str(code)[2:], cache_dir=cache_dir)
        if not ytd:
            continue
        try:
            ytd_yi = float(ytd["ytd_yuan"]) / 1e8
        except Exception:
            continue
        if ytd_yi <= 0:
            continue
        g = (growth_by_code or {}).get(code)
        if g is None:
            try:
                g = em.get("growth")
            except Exception:
                g = None
        if g is None or g <= 0:
            continue                                   # 增长为负 → 疑似真实 Q4 亏损预期
        if cons >= ytd_yi:
            continue                                   # 未被事实证伪 → 不触发
        factor = annualize_factor(ytd["period"])
        if factor is None:
            continue
        if cons >= ytd_yi * MATERIAL_FALSIFY_RATIO:
            # 轻微缺口：一致预期 ≈ 已实现累计（implied Q4 微亏/走平）——可能是真实
            # Q4 走弱预期而非预测滞后，只留痕不改数。
            meta[code] = {
                "stale": False, "stale_soft": True,
                "src": "flag_only",
                "consensus_yi": round(cons, 4),
                "consensus_src": cons_src,
                "ytd_yi": round(ytd_yi, 4),
                "period": ytd["period"],
                "annualize_factor": round(factor, 6),
                "anchor_yi": round(cons, 4),
                "note": ("⚠️卖方FY2026一致预期%.2f亿略低于已实现累计%.2f亿"
                         "（implied Q4=%+.2f亿）：疑轻微预测滞后或真实Q4走弱，"
                         "未改数仅留痕，勿读作「预期下修」"
                         % (cons, ytd_yi, cons - ytd_yi)),
            }
            continue
        anchor = max(cons, ytd_yi * factor)
        mk = (mktcap_yi or {}).get(code) or (mktcap_yi or {}).get(str(code)[2:])
        src = "run_rate_anchor_9m_x4_3" if factor == 12.0 / 9 else \
              "run_rate_anchor_period_x%.4g" % factor
        note = ("⚠️卖方FY2026预测陈旧: 一致预期%.2f亿<已实现前三季归母%.2f亿"
                "(%s)，非'预期下修'；估值已按确定性年化锚定%.2f亿(FwdPE=%.1f)"
                % (cons, ytd_yi, ytd["period"], anchor,
                   (float(mk) / anchor) if (isinstance(mk, (int, float)) and mk > 0
                                            and anchor > 0) else 0.0))
        ladder[hit[0]] = round(float(mk) / anchor, 1) if (
            isinstance(mk, (int, float)) and mk > 0 and anchor > 0) else hit[1]
        if isinstance(mk, (int, float)) and mk > 0 and anchor > 0:
            if code in (computed_fwd_pe or {}):
                computed_fwd_pe[code] = ladder[hit[0]]
        meta[code] = {
            "stale": True,
            "src": src,
            "consensus_yi": round(cons, 4),
            "consensus_src": cons_src,
            "ytd_yi": round(ytd_yi, 4),
            "period": ytd["period"],
            "annualize_factor": round(factor, 6),
            "anchor_yi": round(anchor, 4),
            "ladder_2026_new": ladder[hit[0]],
            "note": note,
        }
    return meta
