# -*- coding: utf-8 -*-
"""
cycle_sustain.py —— 周期行业盈利持续性校验模块（round37，2026-10-28 老大指令①）

适配现有估值锚打分引擎；作用：区分【永续成长】与【周期景气脉冲/一次性收益】，
修正盈利持续性分 G_sustain，约束周期标的估值锚上限。

────────────────────────────────────────────────────────────────────────────
⚠️ 数据契约（强制）：本模块的输入必须来自真实接口/文件，禁止编造。
   当前 scored 数据可得性（实测 top200_scored_2026-10-27.json，1895 只）：
     ✅ is_cycle_sector   ← step2 已有 is_cyclic / SW_CYCLICAL（320 只命中）
     ✅ G_sustain(基准)   ← score_sustain（step2 启发式 1.0~5.0，视作 0-10 量纲）
     ✅ net_growth_adjusted ← growth（低基数清洗后扣非净利增速，已落库）
     ❌ rev_growth           营收同比增速         —— 缺，需营收增长源
     ❌ gross_margin_trend   连续4季度毛利率趋势   —— 缺，需季度毛利序列
     ❌ commodity_price_trend 行业核心产品价格趋势 —— 缺，需商品/原料价格源
     ❌ sector_margin_percentile 行业毛利率5年分位 —— 缺，需行业毛利历史
     ❌ one_time_flag        一次性收益标记        —— 缺，需归母/扣非差额或公告标记
   缺输入的步骤一律【跳过（SKIP）】，不施加任何惩罚，并在返回里标注 steps_skipped，
   绝不静默用占位值顶替（禁 or 0 / 禁编造）。一旦补齐数据源，对应步骤自动激活。
────────────────────────────────────────────────────────────────────────────

行业分类（与用户清单对齐，映射到本项目申万一级/二级）：
   强周期(强制全套): 有色金属/煤炭/石油石化/钢铁/基础化工/建筑材料/交通运输(航运·港口)
                    /机械设备(工程机械)/非银金融(证券)/房地产
   准周期(建议开启): 电力设备(电池·光伏设备)/电子(半导体)/国防军工(航空装备)/农林牧渔(养殖)
"""
import math

# ===== 周期行业映射（申万一级 parent / 二级 l2）=====
# tier: "strong" 强周期（强制全套校验）| "quasi" 准周期（建议开启）
CYCLE_L1_STRONG = {
    "有色金属", "煤炭", "石油石化", "钢铁", "基础化工", "建筑材料",
    "房地产", "非银金融",  # 非银金融→券商；房地产→地产
}
CYCLE_L1_QUASI = {
    "电力设备", "电子", "国防军工", "农林牧渔",
}
# 二级细分（parent 不够细时兜底，例如 交通运输下的航运/港口、机械设备下的工程机械）
CYCLE_L2_STRONG = {
    "航运", "港口", "工程机械", "水泥", "玻璃玻纤", "化学纤维", "农化制品",
    "油气开采", "油服工程", "炼化及贸易", "煤炭开采", "焦炭",
}
CYCLE_L2_QUASI = {
    "电池", "光伏设备", "风电设备", "半导体", "航空装备", "养殖业", "饲料",
    "金属新材料",  # 有色下的新材料仍按强周期对待（福达合金即此）
}

# 估值锚约束参数
G_SUSTAIN_CAP = 6.0          # 景气高位持续性硬上限
VAL_ANCHOR_CAP = 8.5         # 周期股持续性不足时估值锚上限
SUSTAIN_THRESHOLD = 7.0      # 估值锚满分所需持续性门槛
GAP_THRESHOLD = 50           # 利润-营收增速差阈值


def _is_num(x):
    return isinstance(x, (int, float)) and not isinstance(x, bool) and x == x


def is_cycle_sector(row):
    """返回 (is_cycle: bool, tier: 'strong'|'quasi'|None)。

    优先用 step2 已有的 is_cyclic（含 SW_CYCLICAL 兜底）；否则按本模块映射补判
    （覆盖 step2 漏掉的准周期，如军工航空装备）。
    """
    if row.get("is_cyclic"):
        # step2 已判为周期（含存储模组），沿用其强/准由父级推断
        parent = row.get("sector_parent") or ""
        l2 = row.get("sector_l2") or ""
        if parent in CYCLE_L1_STRONG or l2 in CYCLE_L2_STRONG:
            return True, "strong"
        if parent in CYCLE_L1_QUASI or l2 in CYCLE_L2_QUASI:
            return True, "quasi"
        return True, "strong"  # step2 判了就当强周期处理，偏严不偏松
    parent = row.get("sector_parent") or ""
    l2 = row.get("sector_l2") or ""
    sec = row.get("sector") or ""
    if parent in CYCLE_L1_STRONG or l2 in CYCLE_L2_STRONG or sec in CYCLE_L1_STRONG:
        return True, "strong"
    if parent in CYCLE_L1_QUASI or l2 in CYCLE_L2_QUASI:
        return True, "quasi"
    return False, None


def cycle_sector_sustain_correction(row, verbose=False):
    """周期行业盈利持续性校验（五步，顺序不可调换）。

    入参 row 应含：
        is_cycle_sector(可选, 否则内部重算), score_sustain(基准 G_sustain),
        rev_growth, net_growth_adjusted(或 growth), gross_margin_trend,
        commodity_price_trend, sector_margin_percentile, one_time_flag
    返回 (g_sustain_final, info) —— info 含 steps_applied / steps_skipped / notes。
    缺输入的步骤跳过（不施加惩罚），绝不编造。
    """
    info = {"steps_applied": [], "steps_skipped": [], "notes": []}
    cyc, tier = is_cycle_sector(row)
    if not cyc:
        info["notes"].append("非周期，跳过全套校验")
        return _as_float(row.get("score_sustain")), info

    g = _as_float(row.get("score_sustain"))
    if g is None:
        g = 3.0  # 与 step2 默认值一致，非编造（缺评分时的中性值）
        info["notes"].append("G_sustain 缺，按中性 3.0 起算")

    # ---- Step1：营收-利润增速匹配校验 ----
    rev_g = _as_float(row.get("rev_growth"))
    net_g = _as_float(row.get("net_growth_adjusted") if row.get("net_growth_adjusted") is not None
                      else row.get("growth"))
    if rev_g is not None and net_g is not None:
        if net_g > rev_g + GAP_THRESHOLD:
            g *= 0.70
            info["steps_applied"].append("Step1 营收-利润增速差惩罚(×0.70)")
        else:
            info["steps_applied"].append("Step1 通过(增速匹配)")
    else:
        info["steps_skipped"].append("Step1(缺 rev_growth)")

    # ---- Step2：毛利率与核心产品价格联动校验 ----
    gm_trend = row.get("gross_margin_trend")
    cpt = row.get("commodity_price_trend")
    if gm_trend == "up" and cpt in ("flat", "down"):
        g *= 0.75
        info["steps_applied"].append("Step2 毛利-价格背离惩罚(×0.75)")
    elif gm_trend is not None and cpt is not None:
        info["steps_applied"].append("Step2 通过(毛利与价格同向/无背离)")
    else:
        info["steps_skipped"].append("Step2(缺 gross_margin_trend/commodity_price_trend)")

    # ---- Step3：一次性收益硬拦截 ----
    ocf = row.get("one_time_flag")
    if ocf is True:
        g = 2.0
        info["steps_applied"].append("Step3 一次性收益硬拦截(→2.0)")
    elif ocf is not None:
        info["steps_applied"].append("Step3 通过(无非一次性收益)")
    else:
        info["steps_skipped"].append("Step3(缺 one_time_flag)")

    # ---- Step4：景气高位封顶惩罚 ----
    smp = _as_float(row.get("sector_margin_percentile"))
    if smp is not None:
        if smp > 0.8:
            g = min(g, G_SUSTAIN_CAP)
            info["steps_applied"].append("Step4 景气高位封顶(clip≤%.1f)" % G_SUSTAIN_CAP)
        else:
            info["steps_applied"].append("Step4 通过(非景气高位)")
    else:
        info["steps_skipped"].append("Step4(缺 sector_margin_percentile)")

    # ---- Step5：输出边界约束 ----
    g_final = max(0.0, min(10.0, g))
    info["tier"] = tier
    if verbose:
        info["notes"].append("G_sustain_final=%.2f (tier=%s)" % (g_final, tier))
    return g_final, info


def val_anchor_limit(row, g_sustain_final):
    """估值锚后置约束（解决福达合金类满分问题）。
    周期行业：G_sustain_final>=7.0 → 正常；<7.0 → 估值锚封顶 8.5。
    非周期：不启用封顶。
    返回 (val_anchor_capped_score, capped: bool)。
    """
    cyc, _ = is_cycle_sector(row)
    val = _as_float(row.get("score_ep"))  # 估值锚原始分（0-10）
    if not cyc or val is None:
        return val, False
    if g_sustain_final >= SUSTAIN_THRESHOLD:
        return val, False
    if val > VAL_ANCHOR_CAP:
        return VAL_ANCHOR_CAP, True
    return val, False


def _as_float(x):
    if _is_num(x):
        return float(x)
    return None


# ===================== 自检 / 演示 =====================
def _demo(rows):
    print("=" * 72)
    print("周期行业盈利持续性校验模块 —— 演示（含数据缺口标注）")
    print("=" * 72)
    samples = ["sh603045", "sz002111", "sh601600", "sz002340", "sz300748"]
    keys = ["name", "sector_parent", "sector_l2", "is_cyclic", "growth",
            "score_sustain", "score_ep"]
    for code in samples:
        r = next((x for x in rows if x["code"] == code), None)
        if not r:
            print("  (池内无 %s)" % code)
            continue
        cyc, tier = is_cycle_sector(r)
        gf, info = cycle_sector_sustain_correction(r, verbose=True)
        cap, capped = val_anchor_limit(r, gf)
        print("\n  %s %s" % (code, r.get("name")))
        for k in keys:
            print("    %-16s %s" % (k, r.get(k)))
        print("    is_cycle_sector = %s (tier=%s)" % (cyc, tier))
        print("    G_sustain_final = %.2f" % gf)
        print("    估值锚 score_ep = %s → 封顶后 %s%s"
              % (r.get("score_ep"), cap, " (已封顶8.5)" if capped else ""))
        print("    已激活步骤: %s" % (info["steps_applied"] or "—"))
        print("    ⚠️ 跳过步骤(数据缺口): %s" % (info["steps_skipped"] or "—"))


if __name__ == "__main__":
    import json, os
    HERE = os.path.dirname(os.path.abspath(__file__))
    f = os.path.join(HERE, "top200_scored_2026-10-27.json")
    if not os.path.exists(f):
        print("未找到 scored 文件，仅做逻辑自检")
    else:
        d = json.load(open(f, encoding="utf-8"))
        _demo(d.get("results", []))
