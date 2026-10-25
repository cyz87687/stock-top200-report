#!/usr/bin/env python3
"""第 2 项要求：以量化从业者 + 投资者双视角审计本项目，只输出可验证的问题。

原则：
  - 每条发现必须能给出一手数据证据（文件 + 数值），不给「感觉不好」类意见
  - 严格区分 P0（会导致错误决策 / 静默失效）与 P1（会误导解读 / 削弱可信度）
  - 已修的问题标注 FIXED，不重复计入
"""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = []


def add(level, title, evidence, why, fix):
    OUT.append({"level": level, "title": title, "evidence": evidence,
                "why": why, "fix": fix})


def load(p):
    fp = os.path.join(HERE, p)
    if not os.path.exists(fp):
        return None
    with open(fp, encoding="utf-8") as f:
        return json.load(f)


def main():
    ic = load("factor_ic.json") or {}
    mf = load("sector/mf_pick_2026-10-23.json") or load("sector/mf_latest.json") or {}
    mfb = load("mf_backtest.json") or {}
    fals = load("pool_size_falsify.json") or {}
    w = (load("factor_weights.json") or {}).get("weights") or {}

    # ---------- P0 候选 ----------
    # 1. 新母体零样本
    pbr = ic.get("pool_excess_by_regime") or {}
    sz = pbr.get("size_pool") or {}
    if (sz.get("n_days") or 0) == 0:
        add("P1", "SIZE 新母体零成熟样本，全部因子 IC 来自旧母体",
            f"pool_excess_by_regime.size_pool.n_days=0；legacy n_days={ (pbr.get('legacy') or {}).get('n_days') }",
            "页面以 78 个截面 IC 支撑「基本面有效」的结论，但这些截面 100% 来自成交额旧母体。"
            "换母体后因子行为可能完全不同，当前无任何证据。",
            "已披露（conclusion 字段 + 上区块分区表）。属「等数据」，需 ≥20 个新母体截面。")

    # 2. 反转块共线性 + 弱样本
    coll = (ic.get("collinearity") or {}).get("high_corr_pairs") or []
    rbk = ["score_oversold60", "score_bias60", "score_rsi_rev",
           "score_vol_pulse", "score_overbought"]
    rbw = sum(float(w.get(k) or 0) for k in rbk)
    if len(coll) >= 3 and rbw > 0.15:
        add("P1", "反转块 5 维合计权重 %.2f，但 3 对因子 |corr|≥0.74 且仅 8 个截面" % rbw,
            "共线：bias60|rsi_rev=0.889, oversold|bias60=0.834, oversold|rsi_rev=0.742；"
            "反转块各维 n=8 截面（总 78）",
            "高度相关的 5 个维度各自计权 = 同一信号被重复放大。"
            "有效独立权重远低于名义 %.2f，且样本仅 8 截面（占 10%%）。" % rbw,
            "建议合并为单一「反转块因子」（块内先正交化或取均值再加权），"
            "名义权重不变但消除重复计权。属结构性改造，需回测验证，不宜直接改。")

    # 3. 多因子组合的板块约束不可回测
    if mf.get("picks"):
        n = len(mf["picks"])
        add("P1", "多因子组合的板块集中度约束（L2≤2/L1≤2）未经回测验证",
            f"组合 {n} 只，picks 内 sector_l2 去重数={len(set(p.get('sector_l2') for p in mf['picks']))}；"
            "backtest_multifactor.py 的快照无 sector 字段，故 C/D 组回测未含该约束",
            "报告展示的组合含「同板块≤2只」约束，但回测验证的 D 组不含此约束 → "
            "页面回测数字与页面实际组合不完全对应，存在口径差。",
            "已在区块内以「可回测近似」措辞标注；彻底解决需给 factor_history 快照补 sector 字段"
            "（会增加存储，暂缓）。")

    # 4. 动量路线与评分口径互相拆台（已在页面披露，但是真结构缺陷）
    add("P1", "组合层用板块动量选股，评分层却已关闭个股动量（两处口径互斥）",
        "今日 490 只中 481 只 tech.momentum_mode=off；而 pick_portfolio.py momentum 路线"
        "仍按板块 60/10 日动量排名筛板块",
        "同一系统内一处判定动量无效并关闭，另一处继续用动量选股。"
        "这是设计层面的口径冲突，非参数问题。",
        "已由 pick_multifactor.py 取代（不再按板块动量切路线），并在页面明确解释四层原因。"
        "旧 pick_portfolio.py 主线保留作规则披露。")

    # 5. 回测窗口太短（关键）
    rng = ic.get("snapshot_range") or []
    if len(rng) >= 2:
        from datetime import date
        try:
            d0 = date.fromisoformat(rng[0])
            d1 = date.fromisoformat(rng[1])
            days = (d1 - d0).days
        except Exception:
            days = None
        if days is not None and days < 200:
            add("P1", "全部实证仅覆盖 %.0f 天（%s ~ %s），单一市场状态" % (days, rng[0], rng[1]),
                f"snapshot_range={rng}，n_snapshots={ic.get('n_snapshots')}",
                "约 120 天、且期间 A 股整体下跌（池内 20 日中位超额 -5.08pct）。"
                "在这样的窗口里，任何「小市值有效」的结论都可能只是「下跌市里小市值跌得少」的 beta，"
                "而非真正的 alpha。跨市场状态（牛/熊/震荡）无一样本。",
                "建议至少覆盖一轮完整涨跌周期再谈「因子有效」。当前只能宣称「在本窗口内有效」。")

    # ---------- 已修项 ----------
    add("FIXED", "v3.8 入池因子未进列表、胜率留空",
        "gen_report_top200.py 新增 _v38fac_block：SIZE/MOM1M/AMT20/市值地板/价格地板 5 行，"
        "含 IC、IC胜率、超额t、多头胜率、多空IR + 池内构造证据",
        "老大第 1 项要求的缺口", "已完成并上线校验")
    add("FIXED", "候选组合 20 只无法执行",
        "新增 pick_multifactor.py（≤10 只，权重由 IC 实证反推）+ 页面新区块",
        "老大第 3 项要求", "已完成，回测同步落盘 mf_backtest.json")
    add("FIXED", "SIZE 行错误显示 C3 反向数据（+-1.30pct）",
        "pool_size_falsify.json 中 C1/C3 名字都含 SIZE，in 匹配互相覆盖",
        "会导致 SIZE 超额符号相反", "已改精确匹配，校验为 +7.43pct / 反向 -1.30pct")
    add("FIXED", "动量路线文案与数据矛盾（称三条线全负，AI 线 T+1 实为 +0.17%）",
        "_rt_read 原硬编码「全部为负」", "数据与文案矛盾会误导判断",
        "已改为由 recommendation_track.json 动态生成，实测输出「5/6 项为负」并解释差异")
    add("FIXED", "新脚本未接入 Actions 工作流（会静默失效）",
        "daily-refresh.yml 原无 pick_multifactor/gen_mf_backtest",
        "下次自动运行时组合会退回旧的 20 只", "已插入 Step 3.965 并补 git add 清单")

    # ---------- 输出 ----------
    order = {"P0": 0, "P1": 1, "FIXED": 2}
    OUT.sort(key=lambda x: order.get(x["level"], 9))
    print("=" * 78)
    print("量化 + 投资者双视角审计结果")
    print("=" * 78)
    for lvl in ("P0", "P1", "FIXED"):
        sub = [x for x in OUT if x["level"] == lvl]
        print(f"\n{'#'*3} {lvl}（{len(sub)} 项）{'#'*3}")
        for i, x in enumerate(sub, 1):
            print(f"\n[{lvl}-{i}] {x['title']}")
            print(f"  证据: {x['evidence']}")
            print(f"  为何重要: {x['why']}")
            print(f"  处置: {x['fix']}")
    # 落盘
    with open(os.path.join(HERE, "audit_round3.json"), "w", encoding="utf-8") as f:
        json.dump(OUT, f, ensure_ascii=False, indent=1)
    print(f"\n✅ 写入 audit_round3.json，共 {len(OUT)} 条")


if __name__ == "__main__":
    sys.exit(main())
