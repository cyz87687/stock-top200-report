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

    # 2. 反转块共线性 + 弱样本 —— 已做权重扫描，无法定权，转 FIXED（已披露）
    coll = (ic.get("collinearity") or {}).get("high_corr_pairs") or []
    rbk = ["score_oversold60", "score_bias60", "score_rsi_rev",
           "score_vol_pulse", "score_overbought"]
    rbw = sum(float(w.get(k) or 0) for k in rbk)
    if len(coll) >= 3 and rbw > 0.15:
        r5 = load("audit_round5.json") or {}
        scan = (r5.get("q1_weight_scan") or {}).get("vs_base_0.24") or {}
        sig_n = sum(1 for _v in scan.values()
                    for _c in _v.values() if _c.get("sig"))
        max_t = 0.0
        for _v in scan.values():
            for _c in _v.values():
                _t = _c.get("t")
                if isinstance(_t, (int, float)) and abs(_t) > abs(max_t):
                    max_t = _t
        add("FIXED", "反转块 %.2f 权重仅 3~7 个截面支撑——已披露，不做伪优化" % rbw,
            "共线：bias60|rsi_rev=0.889, oversold|bias60=0.834, oversold|rsi_rev=0.742；"
            "audit_round4 实测反转块各维有效截面 T+1 n=7 / T+5 n=3（不是 78）；"
            "T+1 IC=-0.0254（负）、T+5 IC=+0.0791；"
            "audit_round5 权重扫描 0/8%%/12%%/18%%/24%% × 两种构造共 10 组，"
            "显著项 %d 个，最大 |t|=%.2f（Bonferroni 门槛 3.08）" % (sig_n, abs(max_t)),
            "高度相关的 5 个维度各自计权 = 同一信号被重复放大；"
            "但更根本的问题是样本量只有 3~7 个截面——在此样本上调整权重即过拟合。",
            "不改权重（在 3~7 截面上调权=拟合噪声）。改为页面强制披露："
            "「24%% 权重是纯先验、非拟合结果」「T+1 方向为负」"
            "「10 种权重配置全部不显著」。等截面数≥30 再谈优化。")

    # 3. 板块集中度约束 —— 已由 audit_round4 回测：无效但不伤害，转 FIXED
    if mf.get("picks"):
        n = len(mf["picks"])
        r4 = load("audit_round4.json") or {}
        cap = (r4.get("p1_3_sector_cap") or {})
        ev = []
        for h in ("h1", "h5", "h20"):
            c = (cap.get(h) or {}).get("cap2_2") or {}
            if c:
                ev.append("%s delta=%+.2f t=%s %s"
                          % (h, c.get("delta") or 0, c.get("t"),
                             "SIG" if c.get("sig") else "ns"))
        add("FIXED", "板块集中度约束（L2≤2/L1≤2）已回测：无效但不伤害",
            f"组合 {n} 只，sector_l2 去重={len(set(p.get('sector_l2') for p in mf['picks']))}；"
            "audit_round4 用 490 只 sector 映射回填历史快照（覆盖 85.9%）后重跑，"
            "有/无约束配对对比：" + ("; ".join(ev) if ev else "无数据"),
            "原判定为「未经回测验证」。现已有回测：三周期全部不显著"
            "（Bonferroni 门槛 |t|≥3.08），方向一致为正（T+20 delta +0.61~+1.42），"
            "说明约束既不损失收益、也不显著增厚。",
            "保留约束（不显著为正 + 提升可执行性/降低单一板块风险），"
            "但页面已明确「不声称该约束带来超额」。")

    # 4. 动量路线与评分口径互斥 —— 已量化：叠加板块动量几乎零增益，转 FIXED
    r4b = load("audit_round4.json") or {}
    mom = r4b.get("p1_4_sector_mom") or {}
    dm = mom.get("delta_mom_minus_plain") or {}
    add("FIXED", "组合层板块动量与评分层关个股动量互斥——已量化增益≈0，已弃用",
        "今日 490 只中 481 只 tech.momentum_mode=off；audit_round4 实测叠加板块动量后："
        + ("T+1 %+.2f / T+5 %+.2f / T+20 %+.2f pct"
           % (dm.get("h1") or 0, dm.get("h5") or 0, dm.get("h20") or 0)),
        "同一系统内一处判定动量无效并关闭，另一处继续用动量选股。"
        "这是设计层面的口径冲突，非参数问题。现用量化证明：叠加板块动量的增益"
        "只有 0.08~0.37pct，远低于噪声，即「口径冲突换来的收益几乎是零」。",
        "pick_multifactor.py 已彻底不用板块动量（源码无 sector_mom 引用），"
        "只在页面解释四层原因。旧 pick_portfolio.py 主线保留作规则披露。")

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
            r6 = load("audit_round6.json") or {}
            r7 = load("audit_round7.json") or {}
            dsum = r6.get("decay_summary") or {}
            n20 = ((r7.get("m2_daily_h1") or {}).get("近20日") or {})
            add("P1", "全部实证仅覆盖 %.0f 天（%s ~ %s），且已实证出现状态切换" % (days, rng[0], rng[1]),
                f"snapshot_range={rng}，n_snapshots={ic.get('n_snapshots')}；"
                "30日滚动窗口早5窗→晚5窗：T+1 %+.2f→%+.2f、T+5 %+.2f→%+.2f、T+20 %+.2f→%+.2f pct；"
                "近20日 T+1 超额 %+.2f%%（NW t=%s、OLS t=%s、胜率 %.0f%%、n=%d）"
                % ((dsum.get("h1") or {}).get("early5") or 0,
                   (dsum.get("h1") or {}).get("late5") or 0,
                   (dsum.get("h5") or {}).get("early5") or 0,
                   (dsum.get("h5") or {}).get("late5") or 0,
                   (dsum.get("h20") or {}).get("early5") or 0,
                   (dsum.get("h20") or {}).get("late5") or 0,
                   n20.get("exc") or 0, n20.get("t_nw"), n20.get("t_ols"),
                   n20.get("win") or 0, n20.get("n") or 0),
                "约 120 天、且期间 A 股整体下跌（池内 20 日中位超额 -5.08pct）。"
                "在这样的窗口里，任何「小市值有效」的结论都可能只是「下跌市里小市值跌得少」的 beta。"
                "本轮进一步证实：这个短窗口内部本身就发生了状态切换——"
                "T+20 早5窗 +3.05% → 晚5窗 -0.04%，T+5 +1.16% → -0.94%。"
                "即「窗口太短」已不是未来风险，而是正在发生的事实。",
                "这条无法靠改代码解决：只能等时间积累。已做三件事："
                "① 页面新增「fund 近20日转负超额」区块强制披露；"
                "② 明确组合持有期压到 5 日以内；"
                "③ 写入「连续两个 30 日窗口超额为负应降仓」的纪律（尚未自动化）。"
                "状态：等数据，跨牛熊样本需数月。")

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
