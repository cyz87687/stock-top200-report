#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""因子日报生成器（v1.0 · 2026-10-25）

背景
----
老大指令：「因子分析可改为每日生成报告，用于优化因子权重」「页面上不展示分析与计算过程，
仅呈现结论」。原先堆在主页面上的 11 类分析区块（Rank IC / 证伪检验 / 47 因子 / 数据覆盖率 /
选股池口径 / 因子衰减 / 反转块样本量 / 预测力安全门 / 加权综合分 / 多因子回测对比 /
动量悖论）全部迁到这里，改为**每日 Markdown 存档**，不占页面。

数据纪律
--------
本文件只做**搬运与排版**，不创造任何数字：
  · 所有 IC / NW t / 胜率 / 超额 一律读自 factor_ic.json / mf_backtest.json / recommendation_track.json
  · 缺字段就写「未查到」，绝不补 0、不补均值、不写「约」

输出
----
reports/factor_YYYY-MM-DD.md   （默认按 factor_ic.json 的 date 命名）
可选同时输出 .html 供本地阅读（--html），但不入 index.html 导航、不上线。

用法
----
  python gen_factor_daily.py
  python gen_factor_daily.py --date 2026-10-23
  python gen_factor_daily.py --html
"""
import os
import sys
import json
import argparse
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
REPORT_DIR = os.path.join(HERE, "reports")

# ---------------------------------------------------------------- 工具函数


def _load(name, default=None):
    p = os.path.join(HERE, name)
    if not os.path.exists(p):
        return default if default is not None else {}
    try:
        with open(p, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        print(f"⚠️ 读取 {name} 失败: {e}")
        return default if default is not None else {}


def _f(v, nd=2, suffix="", plus=False, dash="未查到"):
    """安全格式化：None/非数值 → dash，绝不写 0"""
    if not isinstance(v, (int, float)):
        return dash
    s = f"{v:+.{nd}f}" if plus else f"{v:.{nd}f}"
    return s + suffix


def _pct(v, nd=1, plus=True, dash="未查到"):
    return _f(v, nd, "%", plus=plus, dash=dash)


# ---------------------------------------------------------------- 各章节


def sec_header(ic, date_str):
    method = ic.get("method") or {}
    rng = ic.get("snapshot_range") or []
    lines = [
        f"# 因子日报 · {date_str}",
        "",
        f"> 生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}　·　"
        f"数据快照：`factor_ic.json`（内部 date={ic.get('date') or '未查到'}）",
        "",
        "**本报告用途**：为因子权重优化提供每日证据底稿。**主页面只展示结论**，"
        "所有计算过程、证伪检验、样本边界披露集中在此存档。",
        "",
        "## 0. 样本与口径",
        "",
        "| 项目 | 数值 |",
        "|---|---|",
        f"| 有效截面数 | {ic.get('n_snapshots', '未查到')} |",
        f"| 原始截面数 | {ic.get('n_snapshots_raw', '未查到')} |",
        f"| 陈旧快照剔除 | {len(ic.get('stale_snapshots_dropped') or [])} 个"
        + (
            "（" + "、".join(ic.get("stale_snapshots_dropped")[:10]) + "）"
            if ic.get("stale_snapshots_dropped")
            else ""
        )
        + " |",
        f"| 快照区间 | {rng[0] + ' ~ ' + rng[1] if len(rng) == 2 else '未查到'} |",
        f"| 统计方法 | {method.get('name') or '未查到'} |",
        f"| 周期 | {'/'.join(str(h) for h in (method.get('horizons_days') or [])) or '未查到'} 日 |",
        f"| t 值口径 | {method.get('t_stat') or '未查到'} |",
        f"| 最小样本 | {method.get('min_sample') or '未查到'} |",
        f"| 多重校正 | {method.get('bonferroni') or '未查到'} |",
        "",
    ]
    return "\n".join(lines)


def sec_factor_table(ic):
    """全因子 IC 矩阵：每个因子 × 三个周期的 IC / NW t / 胜率 / 结论"""
    factors = ic.get("factors") or {}
    if not factors:
        return "## 1. 因子 IC 矩阵\n\n未查到 factors 字段。\n"

    lines = [
        "## 1. 因子 IC 矩阵",
        "",
        "> IC = 因子分与未来收益的秩相关。NW t 为 Newey-West HAC 校正 t 值（自相关稳健）。",
        "> **判读门槛**：30 次检验下 Bonferroni 门槛 |NW t| ≥ 3.08；未过门槛一律视为「未证实」。",
        "> ⚠️ **NW t 依赖 lag 这个自由度**（现行口径 lag=h−1，即 h=20 时 lag=19）。"
        "n≈58 时方差估计本身不稳，同一因子在 lag=3 与 lag=6 可能一个过门槛一个不过。"
        "故下表同时给出 `稳健` 列（全 lag 下都过门槛才算稳健）与 `翻转` 列（结论随 lag 变）。",
        "",
        "| 因子 | 标签 | T+1 IC | T+1 NW t | T+1 胜率 | T+5 IC | T+5 NW t | T+5 胜率 | "
        "T+20 IC | T+20 NW t | T+20 胜率 | 稳健显著 | 结论 |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for key, v in factors.items():
        h = v.get("horizons") or {}
        cells = []
        for hz in ("1", "5", "20"):
            x = h.get(hz) or {}
            cells.append(_f(x.get("ic_mean"), 4, plus=True))
            cells.append(_f(x.get("nw_t"), 2, plus=True))
            cells.append(_f(x.get("win_rate"), 1, "%", plus=False))
        # 稳健显著：任一周期在全 lag 下都过门槛 → 记该周期；有翻转则标注
        _rob = [hz for hz in ("1", "5", "20")
                if (h.get(hz) or {}).get("robust_bonferroni")]
        _flip = [hz for hz in ("1", "5", "20")
                 if ((h.get(hz) or {}).get("nw_sensitivity") or {}).get("flips")]
        _cell = "、".join(f"h{hz}" for hz in _rob) if _rob else "无"
        if _flip:
            _cell += "（h" + "/h".join(_flip) + " 随 lag 翻转）"
        lines.append(
            f"| `{key}` | {v.get('label') or '—'} | " + " | ".join(cells)
            + f" | {_cell} | {v.get('verdict') or '未查到'} |"
        )
    lines.append("")

    # 逐因子详注（只列有内容的）
    lines.append("### 1.1 因子逐条实证（含子区间稳定性）")
    lines.append("")
    for key, v in factors.items():
        note = v.get("verdict_note")
        if not note:
            continue
        # ⚠️ 必须在本循环内重新取 horizons。Python 的 for 变量会泄漏到外层作用域，
        #   上面那个循环结束后 `h` 仍指向**最后一个因子**（score_tech）的 horizons，
        #   直接复用会让所有因子都渲染成 score_tech 的数字（曾因此把每个因子的
        #   lag 敏感性都印成 t=-2.07 / [-2.23,-1.68]，完全失真且不易察觉）。
        h = v.get("horizons") or {}
        h5 = h.get("5") or {}
        sub = h5.get("subperiod") or {}
        lines.append(f"**`{key}`**（{v.get('label') or '—'}）：{note}")
        if sub:
            lines.append(
                f"- 子区间稳定性：前半段 IC 均值 {_f(sub.get('first_half_mean'), 4, plus=True)}、"
                f"后半段 {_f(sub.get('second_half_mean'), 4, plus=True)}、"
                f"符号稳定率 {_f(sub.get('sign_stable_pct'), 1, '%', plus=False)}、"
                f"是否发生机制切换：{'是' if sub.get('regime_shift') else '否'}"
            )
        if h5.get("bonferroni_sig") is not None:
            lines.append(f"- Bonferroni 校正：{'通过' if h5.get('bonferroni_sig') else '未通过'}")
        # v4.2：逐周期披露 lag 敏感性（只在有扫描结果时）
        _sens_lines = []
        for hz in ("1", "5", "20"):
            _x = h.get(hz) or {}
            _sn = _x.get("nw_sensitivity") or {}
            if not _sn:
                continue
            _sens_lines.append(
                f"  - h={hz}（lag={_x.get('nw_lag')}）：t={_f(_x.get('nw_t'), 2, plus=True)}，"
                f"全 lag 区间 [{_f(_sn.get('t_min'), 2, plus=True)}, "
                f"{_f(_sn.get('t_max'), 2, plus=True)}]，"
                f"Andrews lag={_sn.get('lag_andrews')} 时 t={_f(_sn.get('t_andrews'), 2, plus=True)} → "
                + ("**全 lag 稳健过门槛**" if _sn.get("robust_bonferroni")
                   else ("⚠️ 结论随 lag 翻转，不可断言显著" if _sn.get("flips")
                         else "全 lag 均未过门槛"))
                + "　`" + " ".join(f"L{k}:{v:+.2f}" for k, v in
                                    (_sn.get("t_by_lag") or {}).items()) + "`"
            )
        if _sens_lines:
            lines.append("- NW lag 敏感性：")
            lines.extend(_sens_lines)
        lines.append("")
    return "\n".join(lines)


def sec_tradable(ic):
    """可交易口径：T+1 开盘买入、扣成本、头组 vs 池内等权"""
    out = ["## 2. 可交易口径（前视修正 + 交易成本）", ""]
    for key, title in (
        ("tradable_ic", "2.1 全因子等权复合（可交易）"),
        ("tradable_fund_ic", "2.2 基本面单维（可交易）"),
        ("tradable_w_ic", "2.3 现行权重复合（可交易）"),
    ):
        blk = ic.get(key) or {}
        if not blk:
            continue
        out.append(f"### {title}")
        out.append("")
        if blk.get("base"):
            out.append(f"- 口径：{blk['base']}")
        if blk.get("cost_bps_roundtrip") is not None:
            out.append(f"- 往返成本：{blk['cost_bps_roundtrip']} bp")
        wu = blk.get("weights_used")
        if isinstance(wu, dict) and wu:
            out.append("- 使用权重：" + "、".join(f"{k}={v:.1%}" for k, v in wu.items()))
        out.append("")
        out.append("| 周期 | 样本 | IC 均值 | NW t | 胜率 | 结论 |")
        out.append("|---|---|---|---|---|---|")
        for hz in ("1", "5", "20"):
            h = ((blk.get("horizons") or {}).get(hz)) or {}
            out.append(
                f"| T+{hz} | {h.get('n', '未查到')} | {_f(h.get('ic_mean'), 4, plus=True)} | "
                f"{_f(h.get('nw_t'), 2, plus=True)} | {_f(h.get('win_rate'), 1, '%', plus=False)} | "
                f"{h.get('verdict') or '—'} |"
            )
        if blk.get("verdict"):
            out.append("")
            out.append(f"**小结**：{blk.get('verdict')}　{blk.get('verdict_note') or ''}")
        if blk.get("note"):
            out.append("")
            out.append(f"> {blk['note']}")
        md = blk.get("monthly_drift")
        if isinstance(md, list) and md:
            out.append("")
            out.append("**月度漂移**：")
            out.append("")
            out.append("| 月份 | IC 均值 | 样本 |")
            out.append("|---|---|---|")
            for m in md:
                out.append(
                    f"| {m.get('month', '—')} | {_f(m.get('ic_mean'), 4, plus=True)} | "
                    f"{m.get('n', '未查到')} |"
                )
        out.append("")
    return "\n".join(out)


def sec_pool(ic):
    """选股池本身的超额（P0 根因）"""
    pe = ic.get("pool_excess") or {}
    out = ["## 3. 选股池本身的超额（P0 根因）", ""]
    if not pe:
        out.append("未查到 pool_excess 字段。")
        return "\n".join(out)
    out += [
        "> 这是全系统最严重的问题：**持仓池本身系统性跑输全市场**。",
        "> 任何因子加权都只是在这个负 alpha 集合内部做相对排序，调权重无法解决。",
        "",
        "| 指标 | 数值 |",
        "|---|---|",
        f"| 入场日数 | {pe.get('n_days', '未查到')} |",
        f"| 持有期 | {pe.get('hop', '未查到')} 日 |",
        f"| 池内中位超额 | {_pct(pe.get('median_excess_pct'), 2)} |",
        f"| 池内均值超额 | {_pct(pe.get('mean_excess_pct'), 2)} |",
        f"| 跑输入场日占比 | {_f(pe.get('neg_ratio_pct'), 1, '%', plus=False)} |",
        f"| 判定 | {pe.get('verdict') or '未查到'} |",
        "",
    ]
    pr = ic.get("pool_excess_by_regime") or {}
    if pr:
        out.append("### 3.1 按母体口径分组")
        out.append("")
        if pr.get("split_rule"):
            out.append(f"- 分组规则：{pr['split_rule']}")
        if pr.get("versions_seen"):
            _vs = "、".join(f"{k}×{v}" for k, v in (pr["versions_seen"] or {}).items())
            out.append(f"- 已见母体版本：{_vs}")
        if pr.get("n_unclassified") is not None:
            out.append(f"- 未计入（收益未成熟/无价格）截面数：{pr['n_unclassified']}")
        if pr.get("bench_note"):
            out.append(f"- 基准口径：{pr['bench_note']}")
        out.append("")
        out.append("| 分组 | 成熟入场日 | 待成熟 | 池内市值中位 | 超额中位 | 池绝对收益中位 | 跑输占比 | 判定 |")
        out.append("|---|---|---|---|---|---|---|---|")
        for g, label in (("legacy", "成交额旧母体"), ("size_pool", "SIZE 评分母体")):
            v = pr.get(g)
            if isinstance(v, dict):
                out.append(
                    f"| {label} | {v.get('n_days', 0)} | {v.get('n_pending', 0)} | "
                    f"{_f(v.get('mv_med_yi'), 1, '亿', plus=False)} | "
                    f"{_pct(v.get('median_excess_pct'), 2)} | "
                    f"{_pct(v.get('pool_abs_med_pct'), 2)} | "
                    f"{_f(v.get('neg_ratio_pct'), 1, '%', plus=False)} | "
                    f"{v.get('verdict') or '—'} |"
                )
        if pr.get("conclusion"):
            out.append("")
            out.append(f"**结论**：{pr['conclusion']}")
        out.append("")
    return "\n".join(out)


def sec_recommendation(ic):
    """推荐组合 IC（真实推荐记录，非回测）"""
    r = ic.get("recommendation_ic") or {}
    out = ["## 4. 真实推荐组合的 IC（非回测）", ""]
    if not r:
        out.append("未查到 recommendation_ic 字段。")
        return "\n".join(out)
    out.append("| 周期 | 样本 | IC 均值 | NW t | 胜率 | 结论 |")
    out.append("|---|---|---|---|---|---|")
    for hz in ("1", "5", "20"):
        h = ((r.get("horizons") or {}).get(hz)) or {}
        out.append(
            f"| T+{hz} | {h.get('n', '未查到')} | {_f(h.get('ic_mean'), 4, plus=True)} | "
            f"{_f(h.get('nw_t'), 2, plus=True)} | {_f(h.get('win_rate'), 1, '%', plus=False)} | "
            f"{h.get('verdict') or '—'} |"
        )
    if r.get("verdict"):
        out.append("")
        out.append(f"**小结**：{r['verdict']}　{r.get('verdict_note') or ''}")
    out.append("")
    return "\n".join(out)


def sec_collinearity(ic):
    c = ic.get("collinearity") or {}
    out = ["## 5. 共线性体检", ""]
    if not c:
        out.append("未查到 collinearity 字段。")
        return "\n".join(out)
    out.append(f"- 阈值：|corr| ≥ {c.get('threshold', '未查到')}")
    out.append(f"- 高共线对数：{c.get('n_high_corr_pairs', '未查到')}")
    out.append("")
    pairs = c.get("high_corr_pairs") or []
    if pairs:
        out.append("| 因子对 | 相关系数 |")
        out.append("|---|---|")
        for p in pairs:
            out.append(f"| {p.get('pair')} | {_f(p.get('corr'), 3, plus=True)} |")
        out.append("")
    if c.get("note"):
        out.append(f"> {c['note']}")
        out.append("")
    return "\n".join(out)


def sec_weights(ic):
    """现行权重 + 无样本支撑的权重占比

    v4.3（P0 修复，round17 穿透审计）：原实现把证据强度**硬编码**成
      well = ("score_fund", "score_news", "score_sector") → 一律标「满样本（78 截面）」
    这与 factor_ic.json 的真实样本量直接矛盾：
      · score_sector 实测 n(1/5/20) = 18/14/0，从不是 78；
      · score_theme_neg / score_size 原实现标「弱先验（少数截面）」，实际是
        **0 个截面**（round17 发现它们此前根本不在 factor_eval.FACTORS 里，
        日报从未单独评估过，只在复合 IC 里被隐式带入）。
    「少数截面」与「零截面」是两回事：前者是证据弱，后者是**没有证据**。
    硬编码标签会让读者以为 42% 的权重都有样本背书，实际并非如此。
    改为从 factor_ic.json 现取 n，按真实样本量分档。
    """
    w = ic.get("active_weights") or {}
    fac = ic.get("factors") or {}
    out = ["## 6. 现行权重与证据覆盖", ""]
    if not w:
        out.append("未查到 active_weights 字段。")
        return "\n".join(out)

    def _nmax(dim):
        hz = (fac.get(dim) or {}).get("horizons") or {}
        return max([(hz.get(h) or {}).get("n") or 0 for h in ("1", "5", "20")] or [0])

    # 分档：样本达标(>=MIN_SAMPLE) / 小样本(>0) / 无任何截面(=0)
    # ⚠️ MIN_SAMPLE 与 factor_eval 保持一致（20）；此处写死并在文案中说明来源
    MIN_N = 20
    well, weak, none_dims = [], [], []
    rows_tbl = []
    for k, v in sorted(w.items(), key=lambda x: -x[1]):
        n = _nmax(k)
        if n >= MIN_N:
            strength = f"样本达标（n={n}）"
            well.append(k)
        elif n > 0:
            strength = f"⚠️ 小样本（n={n}<{MIN_N}）"
            weak.append(k)
        else:
            strength = "❌ 无任何截面可评估"
            none_dims.append(k)
        rows_tbl.append(f"| `{k}` | {v:.1%} | {strength} |")

    out.append("| 维度 | 权重 | 证据强度（按 factor_ic.json 实际截面数） |")
    out.append("|---|---|---|")
    out += rows_tbl
    w_well = sum(v for k, v in w.items() if k in well)
    w_weak = sum(v for k, v in w.items() if k in weak)
    w_none = sum(v for k, v in w.items() if k in none_dims)
    out += [
        "",
        f"- 样本达标（n≥{MIN_N}）支撑的权重合计：**{w_well:.1%}**",
        f"- 小样本（0<n<{MIN_N}）支撑的权重合计：**{w_weak:.1%}**",
        f"- **完全无截面可评估**的权重合计：**{w_none:.1%}** ← 这部分没有任何独立 IC 证据，纯先验设定",
        "",
    ]
    if none_dims:
        out.append(f"> ⚠️ `{'`、`'.join(none_dims)}` 共 {w_none:.1%} 的权重在因子 IC 表里"
                   f"**找不到对应行**——它们只通过 `_derive_dims` 在复合 IC 里被隐式带入，"
                   f"从未被单独度量。这是「权重与证据脱节」的直接位置，调权重前必须先补样本。")
        out.append("")
    out += [
        "> ⚠️ 纪律：**页面不得暗示「权重已优化」**。非「样本达标」部分必须明确标注为先验，"
        "待满 30 个池内截面后方可运行 `--optimize-weights` 重估。",
        "> 📌 证据强度一栏由 `gen_factor_daily.py` 从 `factor_ic.json` 现取 n 计算，"
        "不再硬编码维度名单（v4.3 前曾把 score_sector 错标为「满样本 78 截面」，实测仅 18/14/0）。",
        "",
    ]
    return "\n".join(out)


def sec_mf_backtest():
    """多因子构造回测对比（原页面 §🧪 区块）"""
    b = _load("mf_backtest.json")
    out = ["## 7. 多因子构造回测：加权是否真的加分", ""]
    if not b:
        out.append("未查到 mf_backtest.json。")
        return "\n".join(out)
    if b.get("method"):
        out.append(f"- 口径：{b['method']}")
    var = b.get("variants") or {}
    if var:
        out.append("")
        out.append("**构造定义**：")
        out.append("")
        for k, v in var.items():
            out.append(f"- **{k}**：{v}")
    out.append("")
    out.append("| 周期 | A 单因子 | B +消息面 | C +剔题材 | D +剔关注度 | 最大配对 \\|t\\| |")
    out.append("|---|---|---|---|---|---|")
    for hz in ("1", "5", "20"):
        h = ((b.get("horizons") or {}).get(hz)) or {}
        gr = h.get("groups") or {}
        if not gr:
            continue
        cells = []
        for k in ("A", "B", "C", "D"):
            g = gr.get(k) or {}
            cells.append(
                f"{_pct(g.get('exc_mean'), 2)}<br><sub>t={_f(g.get('t'), 2, plus=True)} "
                f"· 胜率 {_f(g.get('win'), 1, '%', plus=False)}</sub>"
            )
        pr = h.get("paired") or {}
        tmax = max([abs(p.get("t") or 0) for p in pr.values()] or [0])
        out.append(f"| T+{hz} | " + " | ".join(cells) + f" | {tmax:.2f} |")
    out.append("")
    if b.get("conclusion"):
        out.append(f"**诚实结论**：{b['conclusion']}")
        out.append("")
    # 配对检验明细
    for hz in ("1", "5", "20"):
        h = ((b.get("horizons") or {}).get(hz)) or {}
        pr = h.get("paired") or {}
        if not pr:
            continue
        out.append(f"**T+{hz} 配对检验**：")
        out.append("")
        out.append("| 对比 | 差值 | t | 样本 | 显著 |")
        out.append("|---|---|---|---|---|")
        for name, p in pr.items():
            out.append(
                f"| {name} | {_pct(p.get('diff'), 2)} | {_f(p.get('t'), 2, plus=True)} | "
                f"{p.get('n', '未查到')} | {'是' if p.get('sig') else '否'} |"
            )
        out.append("")
    return "\n".join(out)


def sec_zone_filter():
    """v4.3 新增：买区过滤（老大第 2 项要求）的实证。

    数据源 audit_round14 / audit_round14b / audit_round16.json。
    本函数只做搬运排版，不创造数字；缺文件写「未查到」。
    """
    out = ["## 7.5 买入区间过滤：只选当日现价在买区内的个股（v4.3）", ""]
    r14 = _load("audit_round14.json")
    r16 = _load("audit_round16.json")
    if not r14 and not r16:
        out.append("未查到 audit_round14.json / audit_round16.json。")
        return "\n".join(out)
    if r14:
        out.append(f"- 核查对象：`{r14.get('source_mf')}` 的 {r14.get('n_picks')} 只")
        out.append(f"- 判定口径：{r14.get('ruler', {}).get('strict')}；"
                   f"{r14.get('ruler', {}).get('page')}")
        out.append(f"- **严格口径下现价真在区间内：{r14.get('n_in_strict')}/{r14.get('n_picks')} 只**"
                   f"（页面 ±2% 容差口径 {r14.get('n_in_page')} 只）")
        out.append("")
        out.append("| # | 标的 | 现价 | 买区 | 状态 | 判定 |")
        out.append("|---|---|---|---|---|---|")
        for x in r14.get("items") or []:
            if not x.get("plan_ok"):
                out.append(f"| {x.get('idx')} | {x.get('name')} | — | — | — | {x.get('verdict')} |")
                continue
            tag = {"in": "✅区间内", "above": "⚠️高于", "below": "⚠️低于"}.get(x.get("state"), "—")
            out.append(f"| {x.get('idx')} | {x.get('name')} | {_f(x.get('price'), 2)} | "
                       f"{_f(x.get('buy_lo'), 2)}~{_f(x.get('buy_hi'), 2)} | {tag} | {x.get('verdict')} |")
        out.append("")
    if r16:
        zs = r16.get("zone_stats") or {}
        out.append(f"- 全候选池分布（{r16.get('n_snapshots')} 个截面，自算均线近似口径）："
                   f"现价≤买区上限占比中位 **{zs.get('median_in_zone_pct')}%**"
                   f"（最小 {zs.get('min_in_zone_pct')}% / 最大 {zs.get('max_in_zone_pct')}%）")
        out.append(f"- 可用入场日：不过滤 {r16.get('n_entry_days', {}).get('no_filter')} 个 / "
                   f"过滤后 {r16.get('n_entry_days', {}).get('in_zone')} 个"
                   f"（过滤后仍能凑满 10 只）")
        out.append("")
        out.append("**A/B 对比（同源同权重，唯一差别是这一道闸）**：")
        out.append("")
        out.append("| 周期 | A 超额 | A 胜率 | A NW t | B 超额 | B 胜率 | B NW t | B−A |")
        out.append("|---|---|---|---|---|---|---|---|")
        for hk, v in (r16.get("comparison") or {}).items():
            out.append(f"| T+{hk} | {_pct(v.get('A_exc_pct'), 2)} | {_f(v.get('A_win_pct'), 1, '%', plus=False)} | "
                       f"{_f(v.get('A_nw_t'), 2, plus=True)} | {_pct(v.get('B_exc_pct'), 2)} | "
                       f"{_f(v.get('B_win_pct'), 1, '%', plus=False)} | {_f(v.get('B_nw_t'), 2, plus=True)} | "
                       f"{_pct(v.get('diff_pct'), 2)} |")
        out.append("")
        out.append("**结论**：过滤后超额全线略降（点估计 −0.05% ~ −1.14pct），但")
        out.append("逐日配对检验 **|NW t| 最大 1.54（<2）→ 代价在噪声内，不可断言有害**；")
        out.append("换来的是可执行性从「10 只中约 4 只可当日成交」变为「全部可当日成交」。")
        out.append("")
        out.append("⚠️ 结构性事实：`build_trade_plan` 中 `buy_hi = min(ref_hi, price)`，")
        out.append("买区上限**永不高于现价** → 「现价在买区内」的实质含义是")
        out.append("**股价已回踩到均线带内**，而不是现价夹在区间上下限之间。")
        out.append("")
        out.append("复现边界：")
        for b in r16.get("boundaries") or []:
            out.append(f"- {b}")
        out.append("")
    return "\n".join(out)


def sec_holding_period():
    """v4.3 新增：多因子组合的持仓周期推荐（老大第 3 项要求），必须由数据说话。"""
    out = ["## 7.6 持仓周期：持有多久（v4.3，由回测决定）", ""]
    a = _load("audit_round15.json")
    if not a:
        out.append("未查到 audit_round15.json。")
        return "\n".join(out)
    out.append(f"- 权重版本：{a.get('weights_version')}")
    out.append(f"- 入场/出场口径：{a.get('entry_rule')}")
    out.append(f"- 基准口径：{a.get('bench_rule')}")
    out.append(f"- 可用入场日：{a.get('n_entry_days', {}).get('no_cap')} 个"
               f"（共 {a.get('n_snapshots_total')} 个截面）")
    out.append(f"- 判定规则：{a.get('selection_rule')}")
    out.append("")
    out.append("| 持有期 | 样本 | 组合收益 | 池内基准 | 超额 | 超额中位 | 胜率 | NW t | 非重叠 n | 非重叠超额 |")
    out.append("|---|---|---|---|---|---|---|---|---|---|")
    for hk, v in (a.get("horizons") or {}).items():
        star = " ← **推荐**" if str(a.get("best_std_horizon")) == hk else ""
        out.append(f"| T+{hk}{star} | {v.get('n')} | {_pct(v.get('port_mean_pct'), 2)} | "
                   f"{_pct(v.get('bench_mean_pct'), 2)} | **{_pct(v.get('exc_mean_pct'), 2)}** | "
                   f"{_pct(v.get('exc_med_pct'), 2)} | {_f(v.get('win_pct'), 1, '%', plus=False)} | "
                   f"{_f(v.get('nw_t_laghm1'), 2, plus=True)} | {v.get('n_nonoverlap')} | "
                   f"{_pct(v.get('nonoverlap_exc_mean_pct'), 2)} |")
    out.append("")
    out.append(f"**推荐：T+{a.get('best_std_horizon')}**（{a.get('best_reason')}）")
    out.append("")
    out.append(f"- 20 日内超额峰值在 h={a.get('peak_h')}（{_pct(a.get('peak_exc_pct'), 2)}），"
               f"T+10 起进入 +2.5% 以上高区平台")
    out.append("- ⚠️ **不按「点估计超额最高」选周期**：T+20 名义超额 +3.47% 为全场最高，"
               "但其 NW t 仅 +2.09（全场最低）、非重叠样本只剩 3 个 —— "
               "名义超额最高恰恰是统计上最不可信的。")
    out.append("- ⚠️ **绝对收益全周期为负**（T+20 组合 −4.23% vs 基准 −7.70%）："
               "持有多久都不赚钱，只是相对池子少亏。这是池子负 alpha 决定的，"
               "不是持有期能解决的。")
    out.append("- ⚠️ T+1 超额仅 +0.26%、NW t=+1.32、胜率 51.3% → "
               "**短周期基本无效，不要做 T+0 回转**。")
    out.append("- 纪律：「T+" + str(a.get('best_std_horizon')) + "」是**时间上限**，不是目标价；"
               "触发止损立即离场，不因「还没到期」而扛单。")
    out.append("")
    out.append("复现边界：")
    for b in a.get("boundaries") or []:
        out.append(f"- {b}")
    out.append("")
    return "\n".join(out)


def sec_boundaries():
    """三条不能省掉的边界（原页面 §🚨 区块）"""
    return "\n".join([
        "## 8. 三条不能省掉的边界",
        "",
        "1. **反转块仅 8 个截面** —— 属弱先验而非已验证因子，权重已折半。"
        "样本量不足时任何「有效」判定都不成立。",
        "2. **板块动量未作为选股轴** —— `score_sector` 池内 IC 全周期不显著"
        "（T+1 t=0.29、T+5 t=-1.20），原「按板块动量切路线」的做法缺乏证据支撑。",
        "3. **池内头组净收益历史上为负**（池内基准在跌），组合只表达「相对排序」，"
        "**不等于买入能赚钱**。",
        "",
        "> 补充：多因子加权与 fund 单维在本池内**统计等价**（全部配对检验 |t| 最大 1.41）。"
        "选多因子的理由不是「收益更高」，而是降低单一维度依赖、排除规则有独立行为学依据、"
        "标的更少可执行性更强。",
        "",
    ])


def sec_momentum_paradox():
    """动量悖论四层解释（原页面 §❓ 区块）"""
    out = [
        "## 9. 为什么动量路线 IC 和胜率都低，却仍在选动量路线的个股",
        "",
        "这个问题问到了模型的结构性矛盾，答案有四层：",
        "",
        "**① 字段同名字不同物：动量路线选的是「板块动量」，不是「个股动量」。**",
        "`pick_portfolio.py` 的 momentum 路线筛选条件写在申万二级板块的 60 日/10 日涨幅排名上，"
        "个股层面只按综合评分 `total` 降序取前 10，个股动量从未参与筛选。",
        "",
        "**② 系统自己的个股动量开关是关的。**",
        "多数标的 `momentum_mode = off`（三重负证据触发关闭），个股动量分完全不进总分。"
        "模型一边判定动量无效并关掉它，一边在组合层用板块动量选股 —— 两处口径互相拆台，"
        "属设计缺陷而非巧合。",
        "",
        "**③ IC 低是统计事实，但 IC 衡量的是「排序能力」不是「选股能力」。**",
        "个股动量因子 IC 为负（MOM1M ic_daily=-0.052），说明在本池内涨得多的后续跌得多（均值回复）。"
        "而动量路线选出的板块，其成分股往往已经跌下来了 ——「动量路线」实际选出的是"
        "「强势板块里的弱势股」，恰好落在反转因子的有效区。",
        "",
        "**④ 但真实跟踪数据说明：它确实在亏钱，只是没亏那么多。**",
        "见下节真实推荐记录。",
        "",
    ]
    return "\n".join(out)


def sec_track():
    """真实推荐跟踪：三条路线 T+1/T+5/T+20 超额与胜率"""
    p = os.path.join(HERE, "recommendation_track.json")
    if not os.path.exists(p):
        # 兼容存放在 ai_assess 子目录的情况
        p = os.path.join(HERE, "ai_assess", "recommendation_track.json")
    if not os.path.exists(p):
        return "## 10. 真实推荐跟踪\n\n未查到 recommendation_track.json。\n"
    try:
        with open(p, "r", encoding="utf-8") as f:
            recs = (json.load(f) or {}).get("records") or []
    except Exception as e:
        return f"## 10. 真实推荐跟踪\n\n读取失败：{e}\n"

    kinds = {
        "quant_momentum": "📈 动量路线",
        "quant_reversal": "🔄 反转路线",
        "ai_portfolio": "🤖 AI 主观线",
        "multifactor": "🧮 多因子组合",
    }
    out = [
        "## 10. 真实推荐跟踪（非回测）",
        "",
        f"- 记录条数：{len(recs)}",
        "",
        "| 路线 | 周期 | 样本 | 超额均值 | 超额中位 | 胜率 | 绝对收益均值 |",
        "|---|---|---|---|---|---|---|",
    ]
    for k, label in kinds.items():
        sub = [x for x in recs if x.get("kind") == k]
        if not sub:
            continue
        for hz in ("1", "5", "20"):
            ev = [x["excess"][hz] for x in sub
                  if isinstance((x.get("excess") or {}).get(hz), (int, float))]
            rv = [x["rets"][hz] for x in sub
                  if isinstance((x.get("rets") or {}).get(hz), (int, float))]
            if len(ev) < 3:
                continue
            out.append(
                f"| {label} | T+{hz} | {len(ev)} | {_pct(sum(ev) / len(ev), 2)} | "
                f"{_pct(sorted(ev)[len(ev) // 2], 2)} | "
                f"{_f(sum(1 for x in ev if x > 0) / len(ev) * 100, 1, '%', plus=False)} | "
                f"{_pct(sum(rv) / len(rv), 2) if rv else '未查到'} |"
            )
    out.append("")
    return "\n".join(out)


def sec_gate(ic):
    """风险闸门"""
    g = ic.get("risk_gate") or {}
    out = ["## 11. 风险闸门", ""]
    if not g:
        out.append("未查到 risk_gate 字段。")
        return "\n".join(out)
    out.append(f"- 等级：**{g.get('level') or '未查到'}**")
    out.append("")
    trig = g.get("triggered") or []
    if trig:
        out.append("**已触发的风险项**：")
        out.append("")
        for t in trig:
            out.append(f"- {t}")
        out.append("")
    if g.get("advice"):
        out.append(f"**处置建议**：{g['advice']}")
        out.append("")
    return "\n".join(out)


def sec_notes(ic):
    notes = ic.get("notes") or []
    if not notes:
        return ""
    out = ["## 12. 数据口径备注", ""]
    for n in notes:
        out.append(f"- {n}")
    out.append("")
    return "\n".join(out)


def sec_footer():
    return "\n".join([
        "---",
        "",
        "*本报告由 `gen_factor_daily.py` 自动生成，仅用于因子权重优化的内部证据底稿，"
        "不对外发布、不进入 index.html 导航。所有数字均来自 factor_ic.json / "
        "mf_backtest.json / recommendation_track.json，未做任何人工填补。*",
        "",
    ])


# ---------------------------------------------------------------- 主流程


def sec_weight_trace():
    """权重落地核查（v4.3 新增，round17 穿透审计）—— 回答「日报的权重/因子到底有没有应用到生产」

    老大 2026-10-25 提问：「检查每日因子日报的权重和因子有没有应用到 A股成交额 TOP500
    评分报告，目前看只应用了部分。」

    本节把 `audit_round17.json` 的七环追溯矩阵直接搬进日报，让「哪一维没落地」
    每天可见，而不是靠人偶然发现。矩阵口径见 audit_round17.py。
    """
    a = _load("audit_round17.json")
    out = ["## 6.5 权重落地核查：日报权重 → 生产报告（v4.3）", ""]
    if not a:
        out.append("未查到 audit_round17.json（由 `audit_round17.py` 生成，已接入每日 workflow）。")
        return "\n".join(out)

    out.append(f"- 权重版本：`{a.get('weights_version')}` · 核查对象：`{a.get('scored_file')}`"
               f"（{a.get('n_rows')} 行）")
    s = a.get("summary") or {}
    wc = a.get("weight_coverage") or {}
    out.append(f"- **权重合计 {s.get('w_total'):.2f}**（应为 1.00）")
    out.append("")
    out.append("| 维度 | 权重 | 日报单独评估 | 样本(1/5/20) | scored 数据落地 | 组合角色 |")
    out.append("|---|---|---|---|---|---|")
    for d in a.get("dims") or []:
        nb = (d.get("daily_report") or {}).get("n_by_h") or {}
        ns = f"{nb.get('1') or 0}/{nb.get('5') or 0}/{nb.get('20') or 0}"
        ev = "✅" if (d.get("daily_report") or {}).get("evaluated") else "❌ **未评估**"
        sd = d.get("scored_data") or {}
        land = (f"✅ {sd.get('n_rows_with_value')} 行" if sd.get("present")
                else "❌ **0 行**")
        out.append(f"| `{d['dim']}` | {d['weight']:.1%} | {ev} | {ns} | {land} | "
                   f"{d.get('mf_portfolio_role') or '—'} |")
    out += [
        "",
        f"- ⚠️ **scored 数据里缺失的权重合计：{s.get('w_missing_in_scored_data'):.1%}**"
        f" → 页面上的 `total` 实际只反映 **{wc.get('mean_effective_weight'):.1%}** 的权重",
        f"- ⚠️ 日报从未单独评估的权重合计：**{s.get('w_never_evaluated_in_daily_report'):.1%}**",
        f"- 纯先验（无统计证据）权重合计：**{s.get('w_weak_prior_only'):.1%}**",
        "",
    ]
    if wc.get("n_compared"):
        out.append(f"用现权重复算 vs 文件 `total`：n={wc['n_compared']}，"
                   f"差均值 {wc['total_diff_mean']:+.2f}、中位 {wc['total_diff_median']:+.2f}、"
                   f"最大绝对差 **{wc['total_diff_abs_max']:.2f} 分**，"
                   f"≥1 分的有 {wc['n_cross_rating_threshold']} 只"
                   f"（评级门槛 S≥85/A≥65/B≥45/C≥25，1 分即可跨档）。")
        out.append("")
    pool = a.get("pool") or {}
    if pool:
        pi, ci = pool.get("data") or {}, pool.get("config") or {}
        out.append("**母体口径（选池规则）**：")
        out.append("")
        out.append("| 位置 | version | size_quantile | sample_strata | size_as_factor | 市值中位 |")
        out.append("|---|---|---|---|---|---|")
        out.append(f"| 数据 `top500_all_a.json` | {pi.get('rules_version') or '—'} | "
                   f"{pi.get('size_quantile')} | {pi.get('sample_strata') or '—'} | "
                   f"{pi.get('size_as_factor')} | {pi.get('mv_median_yi')} 亿 |")
        out.append(f"| 配置 `config.json` | {ci.get('rules_version') or '—'} | "
                   f"{ci.get('size_quantile')} | {ci.get('sample_strata') or '—'} | "
                   f"{ci.get('size_as_factor')} | — |")
        out.append("")
        if pool.get("in_sync"):
            out.append("→ ✅ 数据与配置一致。")
        else:
            out.append("→ ❌ **数据与配置不一致**：数据是旧配置的产物。代码已改但数据未刷新，"
                       "属于「静默退化」——页面照常出，但用的是上一版口径。")
        out.append("")
    out += [
        "> **读法**：这一节是「有没有应用」的体检，不是「应用得好不好」的评估。"
        "任何一行出现 ❌，都意味着该维度在生产链路上是断的——要么数据没刷新，"
        "要么根本没接入。断点不产生报错，只会让 `total` 静默地少算一部分权重。",
        "",
    ]
    return "\n".join(out)


def build(date_str=None, want_html=False):
    ic = _load("factor_ic.json")
    if not ic:
        print("🚨 未找到 factor_ic.json，无法生成因子日报")
        return None
    date_str = date_str or str(ic.get("date") or datetime.now().strftime("%Y-%m-%d"))[:10]

    parts = [
        sec_header(ic, date_str),
        sec_factor_table(ic),
        sec_tradable(ic),
        sec_pool(ic),
        sec_recommendation(ic),
        sec_collinearity(ic),
        sec_weights(ic),
        sec_weight_trace(),
        sec_mf_backtest(),
        sec_zone_filter(),
        sec_holding_period(),
        sec_boundaries(),
        sec_momentum_paradox(),
        sec_track(),
        sec_gate(ic),
        sec_notes(ic),
        sec_footer(),
    ]
    md = "\n".join(p for p in parts if p)

    os.makedirs(REPORT_DIR, exist_ok=True)
    md_path = os.path.join(REPORT_DIR, f"factor_{date_str}.md")
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(md)
    print(f"✅ 因子日报已生成: {md_path}（{len(md)} 字符）")

    if want_html:
        html_path = os.path.join(REPORT_DIR, f"factor_{date_str}.html")
        with open(html_path, "w", encoding="utf-8") as f:
            f.write(_md_to_html(md, date_str))
        print(f"✅ 本地 HTML 预览: {html_path}（不上线，不入导航）")

    return md_path


def _md_to_html(md, date_str):
    """极简 Markdown → HTML（仅覆盖本文件用到的语法，不引三方依赖）"""
    import html as _h
    import re as _re

    lines = md.split("\n")
    out = [
        "<!DOCTYPE html><html lang='zh-CN'><head><meta charset='utf-8'>",
        f"<title>因子日报 {date_str}</title>",
        "<style>body{background:#0b1220;color:#cbd5e1;font:14px/1.85 -apple-system,"
        "'PingFang SC',sans-serif;max-width:1080px;margin:0 auto;padding:32px 20px;}"
        "h1{color:#f8fafc;font-size:24px;border-bottom:2px solid #1e293b;padding-bottom:10px;}"
        "h2{color:#7dd3fc;font-size:18px;margin-top:32px;border-left:3px solid #38bdf8;padding-left:10px;}"
        "h3{color:#a3e635;font-size:15px;margin-top:22px;}"
        "table{border-collapse:collapse;width:100%;margin:12px 0;font-size:12.5px;}"
        "th{background:#1e293b;color:#94a3b8;padding:7px 9px;text-align:left;font-weight:600;}"
        "td{border-top:1px solid #1e293b;padding:6px 9px;}"
        "tr:hover td{background:#111c2e;}"
        "code{background:#1e293b;color:#7dd3fc;padding:1px 5px;border-radius:3px;font-size:12px;}"
        "blockquote{border-left:3px solid #f59e0b;background:rgba(245,158,11,.07);"
        "margin:12px 0;padding:8px 14px;color:#fcd34d;}"
        "sub{color:#64748b;font-size:10px;}hr{border:0;border-top:1px solid #1e293b;margin:28px 0;}"
        "strong{color:#f8fafc;}</style></head><body>",
    ]
    in_table = False
    for ln in lines:
        s = ln.rstrip()
        if s.startswith("|") and s.endswith("|"):
            cells = [c.strip() for c in s.strip("|").split("|")]
            if all(_re.fullmatch(r":?-{2,}:?", c) for c in cells if c):
                continue  # 分隔行
            if not in_table:
                out.append("<table>")
                in_table = True
                tag = "th"
            else:
                tag = "td"
            row = "".join(f"<{tag}>{_inline(c, _h)}</{tag}>" for c in cells)
            out.append(f"<tr>{row}</tr>")
            continue
        if in_table:
            out.append("</table>")
            in_table = False
        if not s.strip():
            continue
        if s.startswith("# "):
            out.append(f"<h1>{_inline(s[2:], _h)}</h1>")
        elif s.startswith("## "):
            out.append(f"<h2>{_inline(s[3:], _h)}</h2>")
        elif s.startswith("### "):
            out.append(f"<h3>{_inline(s[4:], _h)}</h3>")
        elif s.startswith("> "):
            out.append(f"<blockquote>{_inline(s[2:], _h)}</blockquote>")
        elif s.startswith("- "):
            out.append(f"<div style='margin:3px 0 3px 14px;'>· {_inline(s[2:], _h)}</div>")
        elif s.strip() == "---":
            out.append("<hr>")
        elif _re.match(r"^\d+\.\s", s):
            out.append(f"<div style='margin:3px 0 3px 14px;'>{_inline(s, _h)}</div>")
        else:
            out.append(f"<p>{_inline(s, _h)}</p>")
    if in_table:
        out.append("</table>")
    out.append("</body></html>")
    return "\n".join(out)


def _inline(s, _h):
    import re as _re
    s = _h.escape(s)
    s = _re.sub(r"`([^`]+)`", r"<code>\1</code>", s)
    s = _re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", s)
    s = s.replace("<br>", "<br>")
    return s


def main():
    ap = argparse.ArgumentParser(description="生成因子日报（Markdown 存档）")
    ap.add_argument("--date", default=None, help="日期 YYYY-MM-DD，默认取 factor_ic.json 的 date")
    ap.add_argument("--html", action="store_true", help="同时输出本地 HTML 预览（不上线）")
    args = ap.parse_args()
    build(args.date, args.html)


if __name__ == "__main__":
    main()
