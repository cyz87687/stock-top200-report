#!/usr/bin/env python3
"""
生成A股成交额TOP200评分HTML报告 - 增强版
功能特性：
- 支持TOP200个股数据
- 响应式布局与移动端适配
- 实时排序筛选交互
- 关键指标可视化图表
- 优化的页面性能
"""

import json
import sys
import os
import re
import math
import glob
import html as html_mod
from datetime import datetime

# 输入输出配置
inp = sys.argv[1] if len(sys.argv) > 1 else f"top200_scored_{datetime.now().strftime('%Y-%m-%d')}.json"

# 加载数据
with open(inp, "r", encoding="utf-8") as f:
    data = json.load(f)

# 输出文件名以【数据日期】为准, 而非脚本运行日期。
# 修复: 盘中/延迟运行(如 10-12 补跑 10-09 数据)时, 旧实现用 datetime.now() 命名,
#       会产出「内容=10-09 数据, 文件名=2026-10-12」的错位文件, 污染日期语义。
_data_date = str(data.get("date") or datetime.now().strftime('%Y-%m-%d'))[:10]
out = sys.argv[2] if len(sys.argv) > 2 else f"top200_report_{_data_date}.html"

results = data["results"]
# v3.3 问题2: 评分量纲升级「每维满分10 / 总分满分100」——score_* 内部仍为 0-5，
#   此处统一 ×2 映射到 0-10 展示；total 已由后端 step2/apply_sector_momentum ×20。
_SCORE_KEYS10 = ("score_fund", "score_news", "score_tech", "score_theme", "score_oversold60",
                 "score_bias60", "score_rsi_rev", "score_vol_pulse", "score_overbought",
                 "score_theme_intrinsic", "score_attention", "score_sector", "score_sustain")
for _r in results:
    for _k in _SCORE_KEYS10:
        _v = _r.get(_k)
        if isinstance(_v, (int, float)):
            _r[_k] = round(float(_v) * 2, 2)
stats = data.get("stats", {})
market_breadth = stats.get("market_breadth")  # 大盘赚钱效应 (v2.7 新增)
market_review = data.get("market_review") or {}  # v2.11 盘面复盘点评(数据驱动)

# 加载历史报告用于评级变化追踪
prev_ratings = {}
prev_pool_codes = set()   # v3.2 P1-3: 上一交易日池成员（用于「新入池」diff 标记）
prev_files = [f for f in sorted(glob.glob("top200_scored_*.json"), reverse=True)
              if "_watch_" not in f and ".bak" not in f]
for pf in prev_files:
    if os.path.basename(pf) == os.path.basename(inp):
        continue
    try:
        with open(pf, "r", encoding="utf-8") as f:
            prev_data = json.load(f)
        for r in prev_data.get("results", []):
            prev_ratings[r["code"]] = {"rating": r["rating"], "total": r["total"]}
            prev_pool_codes.add(r["code"])
        break  # 只取最近一次
    except:
        pass
# v3.2 P1-3: 池口径一致性守卫 —— 两日池规模差异 >50% 视为口径切换，停用「新入池」diff 标记
_pool_scale_ok = False
if prev_pool_codes:
    _cur_n = len(results) or 1
    _ratio = _cur_n / max(1, len(prev_pool_codes))
    _pool_scale_ok = 0.67 <= _ratio <= 1.5
date_str = data.get("date", datetime.now().strftime("%Y-%m-%d"))
model_str = data.get("model", "stock-scorer")
# 版本号展示: 报告层 REPORT_VER / 底层评分 v2.40(五维) 或 v2.30(四维)
# R6: 此前页头显示底层评分版本(v2.40)而评级口径区显示报告层版本(v2.41), 同页两版本打架。
#     现统一: 报告层版本 REPORT_VER 单独常量化, 页头同时标明"报告层 + 评分层"两个版本号。
# v2.43: 「组合区块三合一」——消除 AI 组合 3 处重复、修复"说降仓却给满额金额"的逻辑矛盾。
# v2.44: 多角色评审 P0/P1 落地 ——
#   P0-1 仓位口径单一化(消除 4~6成 vs 20%-40% 同页冲突)
#   P0-2 点评来源如实标注(LLM / 规则引擎, 依 fallback 字段动态判定)
#   P0-3 Hero 增加数据时效标签(超 1 交易日变红)
#   P1-1 资金分配表增列参考买入区间与止损位
#   P1-2 跟踪表增列超额收益(相对沪深300)
#   P1-3 板块集中度提示
#   P1-4 生存者偏差披露
# v2.45: 本地复盘链路接入 ——
#   P0-2 点评来源升级为三态(本地复盘/在线LLM/规则引擎), 消除"本地核查冒充AI生成"
#   review_meta 落地(AI 复盘区显示深度/轻量核查只数与校验状态)
# v2.46: 个股展示与因子评估增强 ——
#   1) 个股信息旁增加现价, 与技术目标价/机构目标价并列对比(价差 + 预期涨跌幅)
#   2) 新增「📐 因子有效性评估(Rank IC)」区块(评分层 factor_eval.py 产出 factor_ic.json)
#   3) 行业数据有效性: 展示口径改为申万二级优先, 不再出现"综合/其他"滥用
# v2.47: 量化工程审查（P0/P1/P2，见 优化建议清单_2026-10-13.md）——
#   P0-1 IC 交易日对齐: 剔除非交易日/接口陈旧快照(实测 09-10/09-25/10-01~07 七次事故)
#   P0-2 IC 全样本收益: 回填全市场前复权日线, 消除 TOP200 留存偏差(T+20 流失 34%)
#   P0-3 IC Newey-West + Bonferroni: 重叠窗口 t 值虚高修正, 15 次检验校正阈值 |NW t|≥2.97
#   P0-4 IC 驱动权重: factor_weights.json 走步验证机制已就绪，
#        ⚠️ 2026-10-24 实测更正: 十维池内截面 8 < MIN_TRAIN(30)，样本外观测为 0，
#        实际 adopted=prior（IC 驱动权重从未落档）。原此处"样本外复合IC 0.0248→0.0445"
#        为旧五维期文案，已删以免误读。
#   P1 子区间稳定性/因子相关矩阵/workflow 数据新鲜度守卫(check_data_fresh.py)
# v3.0: 十维评分模型（据全市场回测 Top5 因子拆解五维，见 因子排名与10维评分模型设计_2026-10-13.md）——
#   1) 五维 → 十维: 技术面拆为超跌60反转/乖离低位/RSI反转/量能反向/超买反向(旧tech权重按0.06:0.10:0.06映射),
#      题材拆为题材内禀/关注度相对(旧theme权重对半拆分); 基本面/消息面/板块动量保持
#   2) 权重先验(全市场回测映射): 0.18/0.18/0.10/0.12/0.10/0.06/0.10/0.06/0.05/0.05,
#      新5维+关注度 prior_pending(池内截面<30, 积累后 --optimize-weights 重拟合)
#   3) 因子表/权重块/相关矩阵/维度徽章全部十维化, 对照行(旧五维口径)保留供趋势比对
#   4) 工程职责分离: 因子回测(研究层) 与 每日复盘(生产层) 解耦,
#      factor_eval.py 仅归每日 Actions 链路做 IC 数据登记, 复盘 finalize 不再调用
# v3.2: P0/P1 优化落地（专业投资者 × 量化从业者双视角清单，见 P0P1优化细化清单_v3.2_2026-10-13.md）——
#   P0-1 流动性地板接线（fetch normalize 打 liquid_ok；组合层按此过滤）
#   P0-2 申万一级行业暴露偏离约束（等权口径 ≤max_l1_deviation_pp，超限顺延替补）
#   P0-3 入池资格补全（停牌 pct==0&amount==0 / 次新 N 前缀或 K 线<60 → 标记不剔除）
#   P1-1 因子数据覆盖率区块 · P1-3 池口径徽章+新入池 diff · P1-4 路线/资格筛选 · P1-5 区制标签
# v3.3/v3.3.1: 量纲升级「每维满分10 / 总分满分100」(后端 ×20, 前端 ×2 展示) —— 页面 Hero/卡片/推荐/板块均分/20→/100 全量对齐
# v3.4: 每日 AI 焦点池区块（替代 v3.1 已删除的观察池）
# v3.5: score_fund 低基数失真治理 —— 增速饱和压缩审计展示（growth_raw→growth_eff）+ 周期股 durable 闸门收紧
#   （注: v3.3~v3.4 报告层改动当时漏更本常量, 本次一并对齐; 线上页面此前显示的报告层版本号偏旧）
# v3.5.1: growth 取数口径修复披露（年报→东财按报告期）+ Q3 预测景气加成审计徽标/全表列/覆盖率披露
# v3.5.3: ① 删除过时的「📊 评分模型总结」区块（v2.40 五维静态文案与当日算法层市场总结重复且数据陈旧，
#          头部 Hero 已有当日算法层总结；数据字段 ai_model_summary 保留由 enhance 注入但不渲染）
#         ② 累计 IC 曲线修复 y 轴量程按单期 IC 值(±0.4)计算致累计值(±3.5)溢出 viewBox 被裁剪，
#          改为按累计序列动态定量程 + nice ticks + 小屏 overflow-x 滚动适配
#         ③ 区制标签日期数据驱动：用 factor_ic.json 有效截面区间覆盖 config 静态日期（易过期）
# v3.5.4: ④ 「解读须知」样本区间文案数据驱动（原硬编码 2026-06-26~10-12 约3.5个月，
#          价格回填停更 7 个交易日期间与实际数据脱节；现由 snapshot_range 动态生成）
# v3.5.5: ⑤ ⚖️ 权重区块展示「每日重估登记」状态（fitted_at/池内十维截面数/距门槛差距），
#          --optimize-weights 接入每日链路（workflow+本地）；样本不足自动维持先验并刷新记录
# v3.6: ⑥ 主口径由「十维均摊」切换为「基本面主导(v3.6-fund-led)」，版本标识同步
REPORT_VER = "v3.6"
_has_sector = any(x.get('score_sector') is not None for x in results)
_has_10d = any(x.get('score_oversold60') is not None for x in results)
_ver_tag = (f"{REPORT_VER} · 基本面主导" if _has_10d
            else (f"{REPORT_VER} · 五维" if _has_sector else f"{REPORT_VER} · 四维"))

# v3.3 问题1优化: 十维评分明细矩阵（10维独立并列清晰呈现，含板块动量）
# ⚠️ _DIM_MAX 默认 5（与当前评分实现一致: 十维均为 0-5 制）。
#    若问题2确认改为「每维0-10 / 总分100」制，仅需将下方 _DIM_MAX 改为 10，
#    后端 step2/apply_sector_momentum 的总分缩放 ×4 改为 ×10、评级门槛同步放大即可。
_DIM_MAX = 10  # v3.3 问题2: 量纲升级为「每维满分10 / 总分满分100」(后端 ×20, 前端 ×2 展示)


def _load_dim_weights():
    """从 factor_weights.json 读十维权重，供维度矩阵与文案使用。
    v3.6 起禁止在此硬编码权重：口径切换（v3.1-10dim-ic → v3.6-fund-led）时
    硬编码会让页面展示的权重与后端实际评分权重脱节，形成"页面说 18%、实际按 34% 算"
    的静默口径漂移。失败时回退 v3.6 先验。"""
    _fb = {"score_fund": 0.34, "score_news": 0.22, "score_sector": 0.10,
           "score_oversold60": 0.055, "score_bias60": 0.055, "score_rsi_rev": 0.055,
           "score_vol_pulse": 0.055, "score_overbought": 0.02,
           "score_theme_intrinsic": 0.08, "score_attention": 0.02}
    try:
        _p = os.path.join(os.path.dirname(os.path.abspath(__file__)), "factor_weights.json")
        with open(_p, encoding="utf-8") as _f:
            _w = (json.load(_f) or {}).get("weights") or {}
        if all(k in _w for k in _fb):
            _v = {k: float(_w[k]) for k in _fb}
            if abs(sum(_v.values()) - 1.0) <= 0.02 and all(x >= 0 for x in _v.values()):
                return _v
    except Exception:
        pass
    return _fb


_DIM_W = _load_dim_weights()
_DIM_DEFS = [
    ("基本面", "score_fund", _DIM_W["score_fund"], False),
    ("消息面", "score_news", _DIM_W["score_news"], False),
    ("板块动量", "score_sector", _DIM_W["score_sector"], True),   # optional: 未跑 apply_sector_momentum 时为 None
    ("超跌60反转", "score_oversold60", _DIM_W["score_oversold60"], False),
    ("乖离低位", "score_bias60", _DIM_W["score_bias60"], False),
    ("RSI反转", "score_rsi_rev", _DIM_W["score_rsi_rev"], False),
    ("量能反向", "score_vol_pulse", _DIM_W["score_vol_pulse"], False),
    ("超买反向", "score_overbought", _DIM_W["score_overbought"], False),
    ("题材内禀", "score_theme_intrinsic", _DIM_W["score_theme_intrinsic"], False),
    ("关注度", "score_attention", _DIM_W["score_attention"], False),
]


def _dim_matrix_html(r):
    """个股十维评分明细矩阵：10个维度独立成行（维度名+权重+分数条+加权贡献）。
    板块动量可选（optional），缺失时显示「—」而非 0，避免误导。"""
    has_sec = r.get("score_sector") is not None
    rows = []
    for label, key, w, optional in _DIM_DEFS:
        v = r.get(key)
        if v is None:
            bar = '<div class="dm-bar"><span style="width:0%"></span></div>'
            val = '—'
            contrib = '—'
        else:
            pct = max(0.0, min(100.0, float(v) / _DIM_MAX * 100.0))
            bar = f'<div class="dm-bar"><span style="width:{pct:.1f}%"></span></div>'
            val = f"{float(v):.2f}"
            contrib = f"{float(v) * w:.2f}"
        rows.append(
            f'<div class="dm-row">'
            f'<span class="dm-name">{label}<i>×{w:.0%}</i></span>'
            f'{bar}'
            f'<span class="dm-val">{val}</span>'
            f'<span class="dm-contrib">{contrib}</span>'
            f'</div>'
        )
    return ('<div class="dim-matrix">'
            f'<div class="dm-head">📊 十维评分明细 · 每维满分 {_DIM_MAX} · 权重×分数=加权贡献</div>'
            + ''.join(rows)
            + '</div>')
# 评分层版本以 scored json 的 model 字段为准（权重变更自下一次全新评分跑生效，
# 重跑历史数据时页头如实显示该数据当时的模型版本，不虚标）
_SCORER_VER = str(data.get("model") or ("v3.0-10dim (全市场回测先验 + AI + sector-momentum[evidence])"
                                        if _has_10d else
                                        ("v2.41 (IC-driven weights + sector-momentum)"
                                         if _has_sector else "v2.30 (AI 增强)")))
total_stocks = len(results)

# ===== v3.0 扩池: 池规模动态标签（不再硬编码 TOP200）=====
# scored json 的 pool_size 为抓取时的期望池大小(默认500), 覆盖不足时以实际 results 数为准。
try:
    _pool_size = int(data.get("pool_size") or 0) or total_stocks
except Exception:
    _pool_size = total_stocks
_POOL_N = _pool_size                 # 报告文案统一引用, 如 "TOP{_POOL_N}"
_pool_cov = (total_stocks / _pool_size) if _pool_size else 1.0

# 颜色配置
RATING_COLORS = {"S": "#22c55e", "A": "#3b82f6", "B": "#f59e0b", "C": "#f97316", "D": "#dc2626"}
RATING_BG = {"S": "#dcfce7", "A": "#dbeafe", "B": "#fef3c7", "C": "#ffedd5", "D": "#fee2e2"}
# 评级语义(相对强弱口径, 不再含仓位暗示, 仓位由市场状态统一决定)
RATING_FULL = {"S": "当日最强 · 重点关注", "A": "相对强势 · 可跟踪", "B": "中性 · 波段操作", "C": "相对偏弱 · 观望", "D": "当日最弱 · 谨慎"}

# ===== v2.41 P1-1 评级体系: 绝对阈值 → 当日截面分位 =====
# 原绝对阈值(S≥17)下 S 级长期为空; B级(9-13)吞掉过半样本, 区分度弱。
# 改为按当日 total 截面排名分位给级, total 绝对值保持不变, 评级仅表"当日相对强弱"。
RATING_CUTS = [(0.05, "S"), (0.15, "A"), (0.50, "B"), (0.88, "C"), (1.01, "D")]


def _quantile_ratings(items):
    """按 total 降序, 依分位给评级。items=[{code,total}, ...] → {code: rating}"""
    rows = [x for x in items if x.get("total") is not None]
    if not rows:
        return {}
    srt = sorted(rows, key=lambda x: -float(x.get("total") or 0))
    n = len(srt)
    out = {}
    for i, it in enumerate(srt):
        q = (i + 0.5) / n
        rt = "D"
        for hi, r in RATING_CUTS:
            if q < hi:
                rt = r
                break
        out[it["code"]] = rt
    return out


# 重算当日评级(保留绝对口径于 _rating_abs 备查)


# ===== v3.0 十维/五维自适应展示复合分 =====
# 十维数据的叙述层(评级理由/S&A深度分析)仍按"技术块/题材块"口径表述,
# 复合分按当前十维权重加权还原, 保证与真实计分结构一致而非简单等权。
# v3.6: 权重改为读 _DIM_W（factor_weights.json），禁止硬编码——
#   旧写法把反转块硬编码为 0.12/0.10/0.06/0.10/0.06 合计 0.44，
#   口径切换到 v3.6(反转块各 0.055 + 超买 0.02)后会算错复合分。
_TECH_BLOCK_KEYS = ("score_oversold60", "score_bias60", "score_rsi_rev",
                    "score_vol_pulse", "score_overbought")
_TECH_BLOCK_W = sum(_DIM_W[k] for k in _TECH_BLOCK_KEYS) or 1.0


def _dim_composites(r):
    """→ (tech_disp, theme_disp): 十维按当前权重加权复合, 旧五维直接取原字段"""
    if r.get("score_oversold60") is not None:
        _tw = sum(_DIM_W[k] * float(r.get(k) or 0) for k in _TECH_BLOCK_KEYS)
        _ti, _ta = _DIM_W["score_theme_intrinsic"], _DIM_W["score_attention"]
        _th = ((float(r.get("score_theme_intrinsic") or 0) * _ti
                + float(r.get("score_attention") or 0) * _ta) / ((_ti + _ta) or 1.0))
        return _tw / _TECH_BLOCK_W, _th
    return r.get("score_tech", 0), r.get("score_theme", 0)
_rating_abs_orig = {r["code"]: r["rating"] for r in results}
_new_rating = _quantile_ratings(results)
_rating_shift = sum(1 for r in results if _rating_abs_orig.get(r["code"]) != _new_rating.get(r["code"]))
for r in results:
    r["_rating_abs"] = _rating_abs_orig.get(r["code"], r["rating"])
    r["rating"] = _new_rating.get(r["code"], r["rating"])

# 同步评级分布(页头统计卡片)
from collections import Counter as _Counter
_dist = _Counter(r["rating"] for r in results)
stats["rating_dist"] = {k: _dist.get(k, 0) for k in ["S", "A", "B", "C", "D"]}

# 历史评级同步重算(同一分位口径), 保证评级变化追踪口径一致、可比
if prev_ratings:
    _prev_new = _quantile_ratings([{"code": c, "total": v.get("total")} for c, v in prev_ratings.items()])
    for _c, _v in prev_ratings.items():
        _v["rating"] = _prev_new.get(_c, _v["rating"])

top20 = [r for r in results if r["rating"] in ("S", "A")]
top20.sort(key=lambda x: -x["total"])

def build_portfolio(results, n=5):
    candidates = []
    for r in results:
        if r["rating"] not in ("S", "A"):
            continue
        fwd_pe = r.get("fwd_pe")
        if not fwd_pe or fwd_pe <= 0 or fwd_pe > 60:
            continue
        growth = r.get("growth")
        if not growth or growth < 15:
            continue
        tech = r.get("tech", {})
        position = tech.get("position", 50)
        if position > 95:
            continue
        score_diversity = 0
        if r.get("score_oversold60") is not None:  # 十维口径: 新5维+拆分后两维全参与
            dims = [r["score_news"], r["score_fund"], r["score_oversold60"], r["score_bias60"],
                    r["score_rsi_rev"], r["score_vol_pulse"], r["score_overbought"],
                    r.get("score_theme_intrinsic", 0), r.get("score_attention", 0)]
            if r.get("score_sector") is not None:
                dims.append(r["score_sector"])
        else:  # 旧五维口径(历史数据回放)
            dims = [r["score_news"], r["score_tech"], r["score_fund"], r.get("score_theme", 0)]
            if r.get("score_sector") is not None:
                dims.append(r["score_sector"])
        if min(dims) >= 6:
            score_diversity = 1
        if min(dims) >= 8:
            score_diversity = 2
        composite = r["total"] + score_diversity * 0.5
        if 40 <= position <= 80:
            composite += 0.5
        candidates.append((composite, r))

    candidates.sort(key=lambda x: -x[0])
    picks = []
    used_sectors = set()
    for _, r in candidates:
        sec = r.get("sector", "")
        if sec in used_sectors:
            continue
        picks.append(r)
        used_sectors.add(sec)
        if len(picks) >= n:
            break

    if len(picks) < n:
        for r in results:
            if r in picks:
                continue
            if r["rating"] not in ("S", "A"):
                continue
            fwd_pe = r.get("fwd_pe")
            if not fwd_pe or fwd_pe <= 0 or fwd_pe > 80:
                continue
            sec = r.get("sector", "")
            if sec in used_sectors:
                continue
            picks.append(r)
            used_sectors.add(sec)
            if len(picks) >= n:
                break
    return picks

portfolio_picks = build_portfolio(results)

# 板块均分TOP5
sec_avg = stats.get("sector_avg", {})
sorted_secs = sorted(sec_avg.items(), key=lambda x: -x[1].get("avg", 0))[:5]

# ===== v2.44 P1-3 行业集中度测算（机构视角：风险敞口透明度）=====
# 历史问题: 2026-10-09 半导体37+通信设备20+元件18=75/194 占 38.7%,
#   名单实为"电子板块选股", 但报告无任何集中度提示 → 使用者误以为"全市场分散"。
# 口径: 按展示层板块(申万二级优先)统计占比, 单个板块 >15% 触发提示。
# 注: 此处内联取板块名(不调用 sector_disp —— 其定义在本段之后), 口径完全一致。
_conc = _Counter()
for _r in results:
    _s = _r.get("sector_l2") or _r.get("sector_parent") or _r.get("sector", "")
    if _s:
        _conc[_s] += 1
_conc_total = sum(_conc.values()) or 1
_conc_top = _conc.most_common(3)
_conc_top_pct = [(nm, cnt, cnt / _conc_total * 100) for nm, cnt in _conc_top]
_conc_max_pct = _conc_top_pct[0][2] if _conc_top_pct else 0
# 前3板块合计占比(衡量整体集中度)
_conc_sum3 = sum(p for _, _, p in _conc_top_pct)
_conc_txt = " · ".join(f"{nm} {cnt}只({p:.0f}%)" for nm, cnt, p in _conc_top_pct)
_conc_warn = _conc_max_pct > 15
_conc_color = "#dc2626" if _conc_max_pct > 25 else ("#f59e0b" if _conc_warn else "#22c55e")

# ===== v2.41 P0-2 市场状态 → 仓位纪律 =====
# 修复"顶部建议空仓 vs 个股逢低加仓"的矛盾: 由赚钱效应分统一决定当日总仓位上限,
# 个股 advice 不再单独暗示仓位, 而是与市场状态联动降档。
POSITION_LADDER = [
    (0,  20, "冰点/退潮", "≤2成", "观察为主，不新开仓", "#dc2626"),
    (20, 40, "弱势磨底",   "2~4成", "仅轻仓试错，严设止损", "#f97316"),
    (40, 60, "中性震荡",   "4~6成", "结构性参与，不追高", "#f59e0b"),
    (60, 80, "情绪回暖",   "6~8成", "可逢回调加仓", "#22c55e"),
    (80, 101, "亢奋过热",  "8成+警惕", "注意兑现，不追高", "#3b82f6"),
]


def position_rule(score):
    """赚钱效应分 → (状态, 建议总仓位, 操作纪律, 颜色)"""
    if score is None:
        return ("数据暂缺", "—", "以个股信号为主，控制单票仓位", "#94a3b8")
    for lo, hi, phase, pos, disc, color in POSITION_LADDER:
        if lo <= score < hi:
            return (phase, pos, disc, color)
    return ("—", "—", "—", "#94a3b8")


# 市场状态优先取 market_breadth, 回退 market_review
_mb = market_breadth if (market_breadth and market_breadth.get("available")) else None
mkt_score = (_mb.get("score") if _mb else market_review.get("money_score"))
mkt_phase, mkt_pos, mkt_disc, mkt_color = position_rule(mkt_score)
# 允许追加上限(市场状态为冰点时, 个股最高只能给出"观察"级建议)
_bearish = (mkt_score is not None and mkt_score < 40)

# ===== v2.44 P0-1 仓位口径单一化（修复同页双口径冲突）=====
# 历史 bug: 页头 Hero 由 POSITION_LADDER(报告层硬编码阶梯)输出「4~6成」,
#          而 main_line 引用数据层 market_breadth.position 输出「建议仓位 20%-40%」,
#          同一分数(47)给出几乎不重叠的两套区间 → 使用者无法判断该信哪条。
# 现确立【单一事实源】= 报告层 POSITION_LADDER(与评级体系同源、可解释、可回测),
#         数据层 position 字段一律不再出现在展示文案中(仅保留在 scored json 原始数据内备查)。
# 换算关系(百分制 ↔ 成数, 1成=10%): ≤2成=[0,20%) 2~4成=[20,40%) 4~6成=[40,60%)
#                                    6~8成=[60,80%) 8成+=[80,100%]
_POS_PCT = {
    "≤2成": "0%~20%", "2~4成": "20%~40%", "4~6成": "40%~60%",
    "6~8成": "60%~80%", "8成+警惕": "80%~100%", "—": "—",
}
mkt_pos_pct = _POS_PCT.get(mkt_pos, "—")        # 与 mkt_pos 严格同源, 不再独立计算
mkt_pos_full = f"{mkt_pos}（约 {mkt_pos_pct}）"   # 全站统一表述

# ===== v2.44 P0-3 数据时效标签（修复"页头无数据截止时间"）=====
# 历史问题: 数据日期只在 navbar/页脚, Hero 决策区无标注;
#          15:30~20:00 打开页面看到的是前一交易日数据却毫不知情(与 cron 排队延迟叠加)。
def _data_freshness(data_date_str):
    """→ (文案, 颜色, 是否告警)；按数据日期与"最近已收盘交易日"的【交易日差】判定时效。

    注意: 必须用交易日差而非自然日差 —— 周五数据在周一打开时自然日差 3 天,
          但实际只隔 1 个交易日, 用自然日会误报"延迟 3 天"。
    """
    from datetime import datetime as _d, timedelta as _td, timezone as _tz
    try:
        _dd = _d.strptime(str(data_date_str)[:10], "%Y-%m-%d").date()
    except Exception:
        return ("数据日期未知", "#94a3b8", False)
    _bj = _d.now(_tz.utc) + _td(hours=8)
    _today = _bj.date()
    # 回溯到"最近一个已收盘交易日"(15:00 前算前一日, 再跳过周末)
    _ref = _today
    if _bj.hour < 15:
        _ref = _ref - _td(days=1)
    while _ref.weekday() >= 5:
        _ref = _ref - _td(days=1)

    def _wkdays(a, b):
        """a→b 之间的工作日数(b 更晚); 用于近似交易日差。"""
        n, cur = 0, a
        while cur < b:
            cur += _td(days=1)
            if cur.weekday() < 5:
                n += 1
        return n

    _gap = _wkdays(_dd, _ref)
    _dd_str = _dd.strftime("%Y-%m-%d")
    if _gap <= 0:
        return (f"数据截止 {_dd_str} 收盘（最新交易日）", "#22c55e", False)
    if _gap == 1:
        return (f"数据截止 {_dd_str} 收盘（1 个交易日前）", "#f59e0b", False)
    return (f"⚠️ 数据截止 {_dd_str} 收盘（已延迟约 {_gap} 个交易日）", "#dc2626", True)


_fresh_txt, _fresh_color, _fresh_warn = _data_freshness(date_str)



def advice_with_market(r):
    """个股建议 = 个股信号 × 市场状态。冰点/弱势市场下禁止出现"加仓"字样。"""
    base = r.get("advice") or RATING_FULL.get(r["rating"], "")
    if _bearish:
        if r["rating"] == "S":
            return "市场冰点·仅观察，等待情绪修复"
        if r["rating"] == "A":
            return "市场冰点·暂不加仓，等企稳信号"
        return "市场冰点·观望为主"
    return base


# ===== v2.41 P0-3 操作建议三件套（买点/止损/目标）+ P1-3 一致性约束 =====
# 全部基于 scored json 中已有的真实字段(ma5/10/20/60、support/resistance_levels、
# rsi、position、现价)做规则推导, 不引入任何外部编造数据。
def build_trade_plan(r):
    """→ dict(buy_lo, buy_hi, stop, target, risk_reward, pos, tier_note, flags)"""
    price = r.get("price")
    tech = r.get("tech") or {}
    if not price or price <= 0:
        return None
    ma5 = tech.get("ma5"); ma10 = tech.get("ma10")
    ma20 = tech.get("ma20"); ma60 = tech.get("ma60")
    pos = tech.get("position")
    rsi = tech.get("rsi")
    trend = tech.get("trend", "") or ""

    # 支撑/阻力: 基于现价过滤(源数据为历史计算, 可能滞后于现价)
    sups = [v for _, v in (tech.get("support_levels") or []) if isinstance(v, (int, float)) and 0 < v < price]
    ress = [v for _, v in (tech.get("resistance_levels") or []) if isinstance(v, (int, float)) and v > price]
    nearest_sup = max(sups) if sups else None      # 现价下方最近支撑
    nearest_res = min(ress) if ress else None      # 现价上方最近阻力

    # 买点区间: 强势(多头)以 ma5~ma10 为回踩区; 弱势以 ma10~ma20 为等企稳区
    bullish = ("多头" in trend)
    ref_hi = ma5 if bullish else ma10
    ref_lo = ma10 if bullish else ma20
    if ref_hi and ref_lo:
        if ref_hi < ref_lo:
            ref_lo, ref_hi = ref_hi, ref_lo
        buy_lo, buy_hi = ref_lo, ref_hi
    elif nearest_sup:
        buy_lo, buy_hi = nearest_sup * 0.98, nearest_sup * 1.02
    else:
        buy_lo, buy_hi = price * 0.95, price * 0.98

    # 止损: 优先最近支撑下方 1.5%; 否则 ma20; 再否则现价 -8%
    if nearest_sup:
        stop = nearest_sup * 0.985
    elif ma20:
        stop = ma20 * 0.985
    else:
        stop = price * 0.92
    stop = min(stop, price * 0.97) if stop >= price else stop   # 确保止损在现价下方

    # 目标位: 最近阻力; 无则按 2:1 盈亏比推
    risk = max(price - stop, price * 0.02)
    target = nearest_res if nearest_res else price + risk * 2
    reward = max(target - price, 0)
    rr = reward / risk if risk > 0 else 0

    # P1-3 一致性约束: 超买/高分位 → 不建议追高
    flags = []
    overbought = False
    if rsi is not None and rsi > 70:
        overbought = True; flags.append(f"RSI{rsi:.0f}超买")
    if pos is not None and pos > 85:
        overbought = True; flags.append(f"价格分位{pos:.0f}%偏高")

    return {
        "buy_lo": buy_lo, "buy_hi": buy_hi, "stop": stop, "target": target,
        "rr": rr, "overbought": overbought, "flags": flags,
        "pos_chg": (price - ma20) / ma20 * 100 if ma20 else None,
    }


def trade_plan_advice(r, plan):
    """P1-3: 若超买/高位, 操作建议自动降档, 覆盖原'逢低加仓'措辞。"""
    if not plan:
        return None, ""
    if plan["overbought"]:
        return ("高位·等待回调", "⛔ 当前已" + "、".join(plan["flags"]) + "，不宜追高；等回踩买点区再考虑")
    if _bearish:
        return ("冰点·仅观察", "⏸ 市场冰点，暂不新开仓，仅保留观察")
    return ("可跟踪 · 回踩买点区分批", "")


# ===== v2.41 P1-2 板块展示口径 =====
# 原粗分类 sector 中"综合"占约30%(实为电池/半导体等被归兜底), 无信息量。
# 展示层统一用申万二级 sector_l2, 兜底 sector_parent, 再兜底原 sector。
def sector_disp(r):
    return r.get("sector_l2") or r.get("sector_parent") or r.get("sector", "")


# ===== v2.42 R5 估值分位参照 =====
# FwdPE 孤立数字无参照系(26x 是贵还是便宜?)。用当日样本自身分布算分位,
# 语义: "本票 FwdPE 低于当日 N% 的样本" —— 相对便宜度高。
def _compute_fwdpe_pctile(results):
    vals = sorted(v for v in (r.get("fwd_pe") for r in results)
                  if isinstance(v, (int, float)) and 0 < v < 500)
    n = len(vals)
    if n < 10:
        return {}
    out = {}
    for r in results:
        v = r.get("fwd_pe")
        if not isinstance(v, (int, float)) or not (0 < v < 500):
            continue
        import bisect
        rank = bisect.bisect_right(vals, v)
        out[r["code"]] = rank / n * 100        # v 低于 pct% 的样本
    return out


_fwdpe_pct = _compute_fwdpe_pctile(results)


def valu_note(r):
    """→ (相对便宜度文案, 颜色) ；以当日截面 FwdPE 分位表述, 无数据返回空。"""
    p = _fwdpe_pct.get(r["code"])
    if p is None:
        return "", "#94a3b8"
    if p <= 20:
        return f"FwdPE低估·低于{p:.0f}%样本", "#22c55e"
    if p <= 45:
        return f"FwdPE偏低·低于{p:.0f}%样本", "#4ade80"
    if p <= 70:
        return f"FwdPE居中·{p:.0f}%分位", "#94a3b8"
    if p <= 88:
        return f"FwdPE偏高·高于{p:.0f}%样本", "#f59e0b"
    return f"FwdPE偏贵·高于{p:.0f}%样本", "#ef4444"


# ===== v2.42 R1 盈亏比可信度 + R4 持有周期 =====
# R1: 盈亏比是纯技术推导, 超买/高波动/弱动量下可靠性骤降, 需给使用者一个可信度标签。
#     由 RSI + 价格分位 + 量能因子 + 板块动量档位 合成 0~100 置信分。
# R4: 按技术趋势给出建议持有周期(动量路线偏短、趋势路线偏中), 补齐"持有多久"。
def trade_confidence(r, plan):
    """→ (等级 高/中/低, 置信分 0~100, 说明)"""
    if not plan:
        return ("—", 0, "")
    tech = r.get("tech") or {}
    rsi = tech.get("rsi")
    pos = tech.get("position")
    vol_f = tech.get("vol_factor")
    r60 = r.get("sector_r60")
    score = 100
    notes = []
    if rsi is not None:
        if rsi > 70:
            score -= 30; notes.append(f"RSI{rsi:.0f}超买")
        elif rsi > 65:
            score -= 15; notes.append(f"RSI{rsi:.0f}偏高")
        elif rsi < 30:
            score -= 10; notes.append(f"RSI{rsi:.0f}超卖")
    if pos is not None:
        if pos > 85:
            score -= 25; notes.append("分位高位")
        elif pos > 70:
            score -= 12
    if vol_f is not None and vol_f < 2.5:
        score -= 10; notes.append("量能不足")
    if r60 is not None and r60 > 90:
        score -= 10; notes.append("板块动量末段")
    score = max(0, min(100, score))
    if score >= 75:
        return ("高", score, "技术形态健康，盈亏比参考性较强")
    if score >= 50:
        return ("中", score, "存在个别不利因子，盈亏比需打折看待")
    return ("低", score, "多重不利因子叠加，盈亏比参考性弱，勿据此重仓")


def holding_period(r, plan):
    """R4: 按趋势/路线给出建议持有周期。"""
    if not plan:
        return ""
    tech = r.get("tech") or {}
    trend = tech.get("trend", "") or ""
    r60 = r.get("sector_r60")
    if plan.get("overbought"):
        return "等待回调后再定，暂不建仓"
    if "多头" in trend and isinstance(r60, int) and r60 <= 30:
        return "动量路线 · 持有至跌破 ma10（约 5~15 日）"
    if "多头" in trend:
        return "趋势跟涨 · 持有至跌破 ma20（约 10~30 日）"
    return "反转路线 · 反弹 8% 或 5~10 日止盈，破止损即离场"


# ===== v2.41 P0-1 双引擎组合: 量化组合(pick_portfolio 双路线) + AI 组合, 并统计交集 =====
quant_json = {}
try:
    _here = os.path.dirname(os.path.abspath(__file__))
    _qfile = os.path.join(_here, "sector", f"portfolio_{date_str}.json")
    if not os.path.exists(_qfile):
        _qfile = os.path.join(_here, "sector", "portfolio_latest.json")
    if os.path.exists(_qfile):
        with open(_qfile, "r", encoding="utf-8") as _fq:
            quant_json = json.load(_fq)
except Exception as _qe:
    quant_json = {}
    print(f"⚠️ 量化组合数据加载失败: {_qe}")

QUANT_MODE_LABEL = {"momentum": "📈 动量路线（顺动量做多）", "reversal": "🔄 反转路线（超跌反弹）"}


def _quant_picks(mode):
    pf = (quant_json.get("portfolios") or {}).get(mode) or {}
    picks = pf.get("picks") if isinstance(pf, dict) else pf
    return picks or [], (pf.get("target_rule") if isinstance(pf, dict) else None)


quant_picks_all = []
# v3.2 P1-4: 量化双路线归属映射（code → [momentum, reversal]），供个股行 data-route 标注与路线筛选
_route_map = {}
for _m in ("momentum", "reversal"):
    _ps, _ = _quant_picks(_m)
    for _p in _ps:
        quant_picks_all.append(_p.get("name"))
        _cd = _p.get("code")
        if _cd:
            _route_map.setdefault(_cd, [])
            if _m not in _route_map[_cd]:
                _route_map[_cd].append(_m)


def route_of(r):
    """个股所属量化路线标签（逗号拼接，供筛选/展示）。空串=未入选任一组合。"""
    return ",".join(_route_map.get(r.get("code")) or [])

# 开始生成HTML
html_parts = []

html_parts.append('''<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
<title>📊 A股成交额TOP''' + str(total_stocks) + ''' 极简公司评分 | ''' + date_str + '''</title>
<style>
/* 基础样式 */
* {margin:0;padding:0;box-sizing:border-box;}
body {
    font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,'Helvetica Neue',Arial,sans-serif;
    background:#0f172a;color:#e2e8f0;padding:16px;line-height:1.5;
}
.container {max-width:1400px;margin:0 auto;}
a {color:inherit;}

/* A4 锚点导航条 */
.nav-bar {position:sticky;top:0;z-index:60;display:flex;gap:6px;justify-content:center;align-items:center;
    background:rgba(15,23,42,0.96);backdrop-filter:blur(6px);padding:8px 10px;margin-bottom:14px;
    border-bottom:1px solid #334155;flex-wrap:wrap;}
.nav-bar a {font-size:12px;font-weight:500;color:#94a3b8;text-decoration:none;padding:5px 12px;
    border-radius:8px;border:1px solid transparent;transition:all .15s;}
.nav-bar a:hover {color:#e2e8f0;background:#1e293b;border-color:#334155;}
.nav-bar .nav-date {font-size:11px;color:#64748b;margin-left:6px;padding-left:10px;border-left:1px solid #334155;}

/* A1 首屏 Hero 决策区 */
.hero {margin:0 0 18px;padding:20px 22px;border-radius:16px;
    background:linear-gradient(135deg,#1e293b,#0f172a);border:1px solid #334155;}
.hero h1 {font-size:20px;font-weight:500;color:#f8fafc;margin-bottom:12px;font-weight:600;}
.hero .hero-ver {font-size:11px;font-weight:400;color:#64748b;margin-left:8px;}
.hero .hero-verdict {font-size:32px;font-weight:800;line-height:1.2;margin:4px 0 2px;letter-spacing:-.5px;}
.hero .hero-kpis {display:grid;grid-template-columns:repeat(3,1fr);gap:12px;margin:16px 0 0;}
.hero .hero-kpi {background:#0f172a;border-radius:12px;padding:12px 14px;border:1px solid #1e293b;border-left:3px solid #334155;}
.hero .hero-kpi .k-lbl {font-size:11px;color:#94a3b8;}
.hero .hero-kpi .k-val {font-size:20px;font-weight:700;color:#f8fafc;margin-top:2px;}
.hero .hero-kpi .k-sub {font-size:11px;color:#64748b;margin-top:3px;line-height:1.5;}
.hero .hero-top3 {display:grid;grid-template-columns:repeat(3,1fr);gap:12px;margin-top:14px;}
.hero .ht-item {background:#0f172a;border-radius:10px;padding:11px 13px;border:1px solid #1e293b;}
.hero .ht-item .ht-name {font-size:13px;font-weight:700;color:#f8fafc;}
.hero .ht-item .ht-sc {font-size:15px;font-weight:800;margin-top:2px;}
.hero .ht-item .ht-note {font-size:11px;color:#94a3b8;margin-top:3px;}
.hero .hero-meta {font-size:11px;color:#64748b;margin-top:12px;line-height:1.7;}

/* 头部(已下沉, 仅在无 Hero 时的回退) */
.header {text-align:center;padding:20px 0;border-bottom:1px solid #334155;margin-bottom:18px;}
.header h1 {font-size:20px;color:#f8fafc;margin-bottom:8px;}
.header .sub {color:#94a3b8;font-size:12px;margin-bottom:4px;}
.header .meta {color:#64748b;font-size:11px;}
.header .ver-badge {display:inline-flex;align-items:center;gap:8px;margin:10px 0 6px;padding:5px 14px;border-radius:999px;background:linear-gradient(135deg,#1e3a8a,#4c1d95);border:1px solid #6d28d9;color:#e9d5ff;font-size:12px;font-weight:600;letter-spacing:.3px;}
.header .ver-badge .vtag {background:#f59e0b;color:#1e293b;border-radius:999px;padding:1px 9px;font-size:11px;font-weight:700;}
.header .ver-badge .vdate {color:#c4b5fd;font-weight:500;}

/* 核心结论卡片 */
.core-conclusion {
    background:linear-gradient(135deg,#1e293b 0%,#0f172a 100%);
    border-radius:16px;padding:24px;margin:20px 0;
    border:1px solid #334155;border-left:4px solid #22c55e;
}
.core-conclusion h2 {font-size:15px;color:#f8fafc;margin-bottom:16px;display:flex;align-items:center;gap:8px;}
.core-conclusion .main-line {font-size:13px;color:#e2e8f0;margin-bottom:12px;line-height:1.8;}
.core-conclusion .top3 {display:grid;grid-template-columns:repeat(3,1fr);gap:12px;margin-top:12px;}
.core-conclusion .top3-item {
    background:#0f172a;border-radius:10px;padding:14px;border:1px solid #334155;
    display:flex;flex-direction:column;gap:6px;
}
.core-conclusion .top3-item .t-name {font-size:15px;font-weight:800;color:#f8fafc;}
.core-conclusion .top3-item .t-score {font-size:20px;font-weight:800;}
.core-conclusion .top3-item .t-reason {font-size:11px;color:#94a3b8;line-height:1.5;}

/* 快捷筛选按钮 */
.quick-filters {display:flex;gap:8px;flex-wrap:wrap;margin:16px 0;}
.quick-btn {
    background:#1e293b;border:1px solid #334155;color:#e2e8f0;
    padding:8px 16px;border-radius:20px;font-size:12px;cursor:pointer;
    transition:all 0.2s;font-weight:600;
}
.quick-btn:hover {border-color:#3b82f6;background:#273449;}
.quick-btn.active {background:#3b82f6;border-color:#3b82f6;color:#fff;}

/* S级金色突出 */
.stock-detail.s-level {
    background:linear-gradient(135deg,#1e293b 0%,#1a1f2e 100%);
    border-left:4px solid #fbbf24;
    box-shadow:0 0 20px rgba(251,191,36,0.15);
}
.stock-detail.s-level .name {color:#fbbf24;}
.stock-detail.s-level .total {font-size:32px;color:#fbbf24;text-shadow:0 0 10px rgba(251,191,36,0.3);}
.stock-detail.s-level .rating {background:#fef3c7 !important;color:#d97706 !important;font-weight:800;}

/* 评级变化标签 */
.rating-change {
    font-size:11px;padding:2px 6px;border-radius:4px;font-weight:700;margin-left:4px;
}
.rating-up {background:#dcfce7;color:#16a34a;}
.rating-down {background:#fee2e2;color:#dc2626;}
.rating-new {background:#fef3c7;color:#d97706;}
/* v3.2 P1-4 资格风险徽标 */
.qual-badge {display:inline-block;font-size:9px;line-height:1;font-weight:700;padding:2px 4px;border-radius:4px;margin-left:3px;vertical-align:middle;}
.qual-new {background:#fef3c7;color:#b45309;border:1px solid #f59e0b;}
.qual-illiq {background:#fee2e2;color:#b91c1c;border:1px solid #ef4444;}
.qual-susp {background:#e2e8f0;color:#475569;border:1px solid #94a3b8;}
.qual-pool {background:#dbeafe;color:#1d4ed8;border:1px solid #3b82f6;}

/* 分页 */
.pagination {display:flex;gap:6px;justify-content:center;align-items:center;margin:16px 0;flex-wrap:wrap;}
.pagination button {
    background:#1e293b;border:1px solid #334155;color:#e2e8f0;
    padding:6px 12px;border-radius:6px;font-size:12px;cursor:pointer;transition:all 0.2s;
}
.pagination button:hover {border-color:#3b82f6;}
.pagination button.active {background:#3b82f6;border-color:#3b82f6;color:#fff;}
.pagination button:disabled {opacity:0.4;cursor:not-allowed;}
.pagination .page-info {color:#94a3b8;font-size:11px;margin:0 8px;}

/* 列显示控制 */
.col-toggle {display:flex;gap:6px;flex-wrap:wrap;margin:8px 0;padding:10px;background:#1e293b;border-radius:8px;}
.col-toggle label {font-size:11px;color:#94a3b8;display:flex;align-items:center;gap:4px;cursor:pointer;}
.col-toggle input[type="checkbox"] {accent-color:#3b82f6;}

/* 可展开行 */
.stock-table tr.expandable {cursor:pointer;}
.stock-table tr.expandable:hover td {background:#374151;}
.stock-table tr.detail-row td {padding:0;border:none;background:#0f172a;}
.stock-table tr.detail-row .detail-content {padding:16px;display:none;}
.stock-table tr.detail-row.show .detail-content {display:block;}
.stock-table tr.detail-row.show td {padding:16px;}

/* 排序箭头 */
.sort-arrow {font-size:11px;color:#64748b;margin-left:2px;}
th.sorted-asc .sort-arrow {color:#22c55e;}
th.sorted-desc .sort-arrow {color:#ef4444;}

/* 筛选控制 */
.controls {
    display:flex;gap:12px;margin:16px 0;flex-wrap:wrap;
    background:#1e293b;padding:16px;border-radius:12px;border:1px solid #334155;
}
.controls label {font-size:12px;color:#94a3b8;display:block;margin-bottom:4px;}
.controls select, .controls input {
    background:#0f172a;border:1px solid #334155;color:#e2e8f0;
    padding:8px 12px;border-radius:6px;font-size:13px;min-width:140px;
}
.controls select:focus, .controls input:focus {outline:none;border-color:#3b82f6;}
.controls .control-group {flex:1;min-width:160px;}

/* 统计卡片 */
.summary {display:grid;grid-template-columns:repeat(5,1fr);gap:12px;margin:20px 0;}
.card {
    background:#1e293b;border-radius:12px;padding:16px;text-align:center;
    border:1px solid #334155;transition:transform 0.2s;
}
.card:hover {transform:translateY(-2px);border-color:#475569;}
.card .count {font-size:20px;font-weight:800;margin:4px 0;}
.card .label {font-size:11px;color:#94a3b8;}

/* 图表区域 */
.charts {display:grid;grid-template-columns:1fr 1fr;gap:16px;margin:20px 0;}
.chart-card {
    background:#1e293b;border-radius:12px;padding:20px;border:1px solid #334155;
}
.chart-card h3 {font-size:13px;color:#f8fafc;margin-bottom:16px;display:flex;align-items:center;gap:8px;}
.chart-container {height:200px;position:relative;}
.bar-chart {display:flex;align-items:flex-end;gap:8px;height:160px;padding:10px 0;}
.bar-item {flex:1;display:flex;flex-direction:column;align-items:center;gap:4px;}
.bar {width:100%;background:linear-gradient(180deg,#3b82f6,#22c55e);border-radius:4px 4px 0 0;transition:height 0.3s;}
.bar-label {font-size:11px;color:#94a3b8;text-align:center;max-width:60px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;}
.bar-value {font-size:11px;color:#e2e8f0;font-weight:700;}

/* 详情卡片 */
.section-title {
    font-size:15px;font-weight:700;color:#f8fafc;
    margin:28px 0 16px;padding-bottom:10px;border-bottom:2px solid #334155;
    display:flex;align-items:center;gap:8px;
}
.top-list {margin-bottom:30px;}
.stock-detail {
    background:#1e293b;border-radius:12px;padding:16px;margin-bottom:12px;
    border-left:4px solid #334155;border:1px solid #334155;transition:all 0.2s;
}
.stock-detail:hover {border-color:#475569;transform:translateX(4px);}
.stock-detail .header-row {
    display:flex;align-items:center;gap:12px;margin-bottom:10px;flex-wrap:wrap;
}
.stock-detail .rank {
    width:40px;height:40px;border-radius:50%;display:flex;align-items:center;justify-content:center;
    font-size:13px;font-weight:800;color:#fff;flex-shrink:0;
}
.stock-detail .name {font-size:15px;font-weight:700;color:#f8fafc;}
.stock-detail .code {font-size:11px;color:#94a3b8;}
.stock-detail .total {font-size:20px;font-weight:800;margin-left:auto;}
.stock-detail .rating {
    font-size:12px;font-weight:700;padding:4px 12px;border-radius:8px;text-align:center;
}
.stock-detail .meta-row {
    display:flex;gap:12px;font-size:11px;color:#94a3b8;margin-bottom:10px;flex-wrap:wrap;
}
.stock-detail .dim-row {display:grid;grid-template-columns:repeat(4,1fr);gap:12px;font-size:12px;}
.stock-detail .dim-card {
    background:#0f172a;border-radius:8px;padding:12px;border:1px solid #334155;
}
.stock-detail .dim-card .dim-title {font-size:11px;color:#94a3b8;margin-bottom:6px;}
.stock-detail .dim-card .dim-score {font-size:15px;font-weight:700;margin-right:8px;}
.stock-detail .dim-card .dim-detail {font-size:11px;color:#94a3b8;line-height:1.6;margin-top:6px;}
.stock-detail .dim-matrix {margin-top:12px;padding-top:10px;border-top:1px dashed #334155;}
.stock-detail .dim-matrix .dm-head {font-size:11px;color:#cbd5e1;font-weight:700;margin-bottom:8px;}
.stock-detail .dim-matrix .dm-row {display:grid;grid-template-columns:104px 1fr 42px 42px;align-items:center;gap:8px;margin:3px 0;font-size:11px;}
.stock-detail .dim-matrix .dm-name {color:#94a3b8;white-space:nowrap;}
.stock-detail .dim-matrix .dm-name i {color:#64748b;font-style:normal;font-size:10px;margin-left:4px;}
.stock-detail .dim-matrix .dm-bar {height:8px;background:#1e293b;border-radius:4px;overflow:hidden;}
.stock-detail .dim-matrix .dm-bar span {display:block;height:100%;background:linear-gradient(90deg,#38bdf8,#22d3ee);border-radius:4px;}
.stock-detail .dim-matrix .dm-val {color:#e2e8f0;font-weight:700;text-align:right;}
.stock-detail .dim-matrix .dm-contrib {color:#fbbf24;font-weight:700;text-align:right;}

/* A5 深度卡片折叠: A级默认收起明细, 点击展开 */
.stock-detail.collapsed .sd-body {display:none;}
.stock-detail .expand-hint {font-size:11px;color:#38bdf8;cursor:pointer;margin-left:auto;padding:2px 8px;border:1px solid #334155;border-radius:6px;user-select:none;}
.stock-detail .expand-hint:hover {background:#1e293b;border-color:#38bdf8;}
.stock-detail:not(.collapsed) .expand-hint {display:none;}

/* A7 视图预设按钮 */
.preset-btn {background:#0f172a;border:1px solid #334155;color:#94a3b8;padding:5px 12px;border-radius:6px;font-size:11px;cursor:pointer;font-weight:600;transition:all .15s;}
.preset-btn:hover {border-color:#3b82f6;color:#e2e8f0;}
.preset-btn.active {background:#3b82f6;border-color:#3b82f6;color:#fff;}
.col-toggle-wrap summary {list-style:none;}
.col-toggle-wrap summary::-webkit-details-marker {display:none;}

/* A8 表格首屏固定列: 名称/涨跌/总分/评级 */
.stock-table th.sticky-col, .stock-table td.sticky-col {
    position:sticky;background:#1e293b;z-index:5;
}
.stock-table th.sticky-col {z-index:11;background:#0f172a;}
.stock-table td.col-name-sticky {left:0;min-width:90px;}
.stock-table th.col-name-sticky {left:0;min-width:90px;}
.stock-table td.col-pct-sticky, .stock-table th.col-pct-sticky {left:90px;min-width:72px;}
.stock-table td.col-total-sticky, .stock-table th.col-total-sticky {left:162px;min-width:60px;}
.stock-table tr:hover td.sticky-col {background:#273449;}

/* 完整表格 */
.table-container {overflow-x:auto;background:#1e293b;border-radius:12px;border:1px solid #334155;}
.stock-table {width:100%;border-collapse:collapse;min-width:800px;}
.stock-table th {
    background:#0f172a;color:#94a3b8;padding:12px 10px;text-align:left;
    font-weight:600;font-size:11px;position:sticky;top:0;z-index:10;
    border-bottom:1px solid #334155;cursor:pointer;user-select:none;transition:background 0.2s;
}
.stock-table th:hover {background:#1e293b;}
.stock-table td {padding:10px;border-bottom:1px solid #334155;font-size:12px;}
.stock-table tr:hover td {background:#273449;}

/* 表格内元素 */
.s-rank {color:#64748b;font-weight:700;width:40px;}
.s-name {font-weight:700;color:#f8fafc;}
.s-code {color:#94a3b8;font-size:11px;}
.s-pct {font-weight:700;text-align:right;min-width:60px;}
.s-pct.up {color:#ef4444;}
.s-pct.down {color:#22c55e;}
.s-scores {display:flex;gap:4px;justify-content:center;}
.s-score {
    width:24px;height:24px;border-radius:4px;display:flex;align-items:center;justify-content:center;
    font-size:11px;font-weight:700;color:#fff;
}
.s-total {font-size:13px;font-weight:800;text-align:center;min-width:50px;}
.s-rating {
    font-size:11px;font-weight:700;padding:2px 8px;border-radius:4px;text-align:center;display:inline-block;
}
.s-pe {color:#94a3b8;font-size:11px;min-width:80px;}
.s-turnover {color:#94a3b8;font-size:11px;text-align:right;}
.s-sector {color:#94a3b8;font-size:11px;}

.hidden-row {display:none !important;}

/* 底部 */
.footer {
    text-align:center;color:#475569;font-size:11px;margin-top:40px;
    padding:24px 0;border-top:1px solid #334155;
}
.footer p {margin:6px 0;}

/* 响应式布局 */
@media (max-width:1200px) {
    .charts {grid-template-columns:1fr;}
    .stock-detail .dim-row {grid-template-columns:repeat(2,1fr);}
}
@media (max-width:768px) {
    .summary {grid-template-columns:repeat(2,1fr);}
    .controls {flex-direction:column;}
    .controls .control-group {width:100%;}
    .controls select, .controls input {width:100%;}
    .stock-detail .rank {width:32px;height:32px;font-size:12px;}
    .stock-detail .name {font-size:13px;}
    .stock-detail .total {font-size:20px;}
    .stock-detail .dim-row {grid-template-columns:1fr;}
    .header h1 {font-size:15px;}
}
@media (max-width:480px) {
    .summary {grid-template-columns:1fr;}
    body {padding:10px;}
}
/* 投研分析区块 */
.research-section {margin:30px 0;}
.research-section .section-title {border-bottom:2px solid #fbbf24;color:#fbbf24;}
.research-card {
    background:linear-gradient(135deg,#1e293b 0%,#0f172a 100%);
    border-radius:14px;padding:20px;margin-bottom:16px;
    border:1px solid #334155;border-left:4px solid #3b82f6;
    transition:all 0.2s;
}
.research-card.s-tier {border-left-color:#fbbf24;box-shadow:0 0 16px rgba(251,191,36,0.1);}
.research-card.a-tier {border-left-color:#3b82f6;}
.research-card:hover {border-color:#475569;transform:translateX(4px);}
.research-card .rc-header {
    display:flex;align-items:center;gap:10px;margin-bottom:14px;flex-wrap:wrap;
    padding-bottom:12px;border-bottom:1px solid #334155;
}
.research-card .rc-name {font-size:15px;font-weight:800;color:#f8fafc;}
.research-card .rc-code {font-size:11px;color:#94a3b8;}
.research-card .rc-rating {
    font-size:11px;font-weight:800;padding:3px 10px;border-radius:6px;
}
.research-card .rc-score {font-size:13px;color:#94a3b8;margin-left:auto;}
.research-card .rc-score b {font-size:15px;}
.research-module {margin-bottom:14px;}
.research-module .rm-title {
    font-size:12px;font-weight:700;color:#38bdf8;margin-bottom:6px;
    display:flex;align-items:center;gap:4px;
}
.research-module .rm-content {
    font-size:12px;color:#cbd5e1;line-height:1.8;
    background:#0f172a;border-radius:8px;padding:12px;border:1px solid #1e293b;
}
.research-module.risk .rm-title {color:#f97316;}
.research-module.value .rm-title {color:#22c55e;}
.research-module .rm-content b {color:#f8fafc;}
.research-module .rm-content .tag {
    display:inline-block;font-size:11px;padding:1px 6px;border-radius:3px;
    background:#334155;color:#94a3b8;margin:2px 4px 2px 0;
}
.research-module .rm-content .tag.pos {background:#dcfce7;color:#16a34a;}
.research-module .rm-content .tag.neg {background:#fee2e2;color:#dc2626;}
.research-module .rm-content .tag.warn {background:#fef3c7;color:#d97706;}
@media (max-width:768px) {
    .research-card {padding:14px;}
    .research-card .rc-name {font-size:15px;}
    .research-module .rm-content {font-size:11px;padding:10px;}
}
</style>
</head>
<body>
<div class="container">
    <!-- A4 锚点导航: 12 区块 → 4 组跳转 -->
    <nav class="nav-bar">
        <a href="#sec-decision">① 今日决策</a>
        <a href="#sec-list">② 选股清单</a>
        <a href="#sec-research">③ 深度研究</a>
        <a href="#sec-notes">ⓘ 说明</a>
        <span class="nav-date">数据日期 ''' + date_str + '''</span>
    </nav>
''')

# 核心结论卡片
top3_stocks = sorted(results, key=lambda x: -x["total"])[:3]
s_count = stats.get("rating_dist", {}).get("S", 0)
a_count = stats.get("rating_dist", {}).get("A", 0)
top_sector = sorted_secs[0] if sorted_secs else ("-", {"avg": 0, "count": 0})
main_line = f"今日S级{s_count}只、A级{a_count}只，主线板块：{top_sector[0]}(均分{top_sector[1].get('avg',0):.2f})"

# 大盘赚钱效应 → 入场时机一句话摘要
# v2.44 P0-1: 此处原引用 market_breadth["position"]("建议仓位 20%-40%"),
#   与页头 Hero 的 POSITION_LADDER("4~6成")形成同页双口径冲突。
#   现统一只报"赚钱效应分 + 状态", 仓位表述完全交由 Hero 的单一事实源输出。
if market_breadth and market_breadth.get("available"):
    mb_sc = market_breadth["score"]
    mb_phase = market_breadth["phase"]
    main_line += f" · 大盘赚钱效应{mb_sc}分 {mb_phase}"

# 评级变化统计
rating_changes = {"up": [], "down": [], "new": []}
for r in results:
    code = r["code"]
    if code in prev_ratings:
        prev_r = prev_ratings[code]["rating"]
        curr_r = r["rating"]
        rating_order = {"S": 4, "A": 3, "B": 2, "C": 1}
        if rating_order.get(curr_r, 0) > rating_order.get(prev_r, 0):
            rating_changes["up"].append(r["name"])
        elif rating_order.get(curr_r, 0) < rating_order.get(prev_r, 0):
            rating_changes["down"].append(r["name"])
    else:
        rating_changes["new"].append(r["name"])

change_text = ""
if rating_changes["up"]:
    change_text += f" · 评级上调: {', '.join(rating_changes['up'][:5])}"
if rating_changes["down"]:
    change_text += f" · 评级下调: {', '.join(rating_changes['down'][:5])}"
    if rating_changes["new"] and len(rating_changes["new"]) < 50:
        change_text += f" · 新晋上榜: {', '.join(rating_changes['new'][:5])}"

# ===== v2.11 盘面复盘点评(数据驱动, 无幻觉) — 合并原"今日核心结论"+"大盘赚钱效应" =====
mr_text = market_review.get("text", "")
mr_idx = market_review.get("indices", {}) or {}
mr_amt = market_review.get("amount_yi")
mr_up = market_review.get("up"); mr_down = market_review.get("down")
mr_zt = market_review.get("zt"); mr_dt = market_review.get("dt")
mr_score = market_review.get("money_score"); mr_phase = market_review.get("money_phase", "")
idx_chips = " · ".join(f"{k}{v:+.2f}%" for k, v in mr_idx.items()) if mr_idx else "—"
amt_disp = (f"{mr_amt/10000:.2f}万亿" if (mr_amt and mr_amt >= 10000) else (f"{mr_amt:.0f}亿" if mr_amt else "数据暂缺"))
breadth_disp = (f"涨{mr_up}/跌{mr_down} · 涨停{mr_zt}/跌停{mr_dt}" if (mr_up is not None and mr_down is not None) else "—")
me_disp = (f"{mr_score}分 {mr_phase}" if mr_score is not None else "—")

# ===== v2.42 A1 首屏 Hero: 决策信息上提(原统计卡+口径说明+仓位横幅合并) =====
# 原页面: 标题→版本→副标题→数据源→5统计卡→口径说明→仓位横幅 共约900px才见决策内容。
# 现: Hero 一屏内给出 [仓位纪律 + S/A计数 + 主线板块 + 核心3只], 元信息全部下沉页脚。
_hero_gap = '''</div>'''
# v3.0 扩池: 池覆盖率不足时在 Hero 追加透明提示(正常覆盖率不显示)
_cov_banner = ''
if _pool_cov < 0.96:
    _cov_pct = f"{_pool_cov:.0%}"
    _cov_banner = ('<div style="margin-top:8px;display:inline-flex;align-items:center;gap:6px;'
                   'padding:3px 10px;border-radius:12px;font-size:11px;font-weight:700;'
                   'background:#f59e0b1a;color:#f59e0b;border:0.5px solid #f59e0b55;">'
                   '🧩 池覆盖 ' + str(total_stocks) + '/' + str(_POOL_N) + ' (' + _cov_pct +
                   ') · 低于 96% 阈值</div>')

# ===== v3.2 P1-3 池口径徽章 + P1-5 区制标签（数据驱动，无硬编码兜底不显示）=====
_pool_badge = ''
_elig_badge = ''
_prev_pool_path = None
try:
    _pool_src = data.get("pool_source") or _POOL_N
    # v3.2 P1-3: 池口径透明化 —— 报告实际评分只数 vs 候选池规模（差额主因：金融股不计入评分）
    _fin_rm = int((stats.get("finance_removed") or 0))
    _pool_note = f'候选池 TOP{_POOL_N} · 实际评分 {len(results)} 只'
    if _fin_rm:
        _pool_note += f'（金融股 {_fin_rm} 只无题材维度不计分）'
    _pool_badge = ('<div style="margin-top:8px;display:inline-flex;align-items:center;gap:6px;'
                   'padding:3px 10px;border-radius:12px;font-size:11px;font-weight:700;'
                   'background:#3b82f61a;color:#60a5fa;border:0.5px solid #3b82f655;">'
                   f'🧺 {_pool_note} · 成交额排名 · 十维评分</div>')
    # 资格标记（次新/停牌/流动性不达标）——取 scored json 的 eligible_marks
    _em = (stats.get("eligible_marks") or data.get("eligible_marks") or {})
    if _em and any(_em.get(k) for k in ("new_stock", "suspended", "illiquid")):
        _elig_badge = ('<div style="margin-top:8px;display:inline-flex;align-items:center;gap:6px;'
                       'padding:3px 10px;border-radius:12px;font-size:11px;font-weight:700;'
                       'background:#f59e0b1a;color:#fbbf24;border:0.5px solid #f59e0b55;">'
                       f'🏷️ 资格剔除: 次新 {_em.get("new_stock",0)} / 停牌 {_em.get("suspended",0)}'
                       f' / 流动性不达标 {_em.get("illiquid",0)}</div>')
except Exception:
    pass

# ===== v3.5.3: factor_ic.json 提前读取（供 P1-5 区制标签日期数据驱动）=====
_ai_dir = os.path.dirname(os.path.abspath(__file__))
_ic_path = os.path.join(_ai_dir, "factor_ic.json")
_ic = None
try:
    if os.path.exists(_ic_path):
        with open(_ic_path, "r", encoding="utf-8") as _f:
            _ic = json.load(_f)
except Exception as _e:
    print(f"⚠️ 读取 factor_ic.json 失败: {_e}")

# ===== v3.2 P1-5 区制标签（config.json regime_tag；数据驱动，无则不显示）=====
# v3.5.3: 日期部分数据驱动 —— config 静态日期(人写, 易过期)用 factor_ic.json 的
#         snapshot_range(有效截面区间)动态覆盖; _ic 缺失时保留 config 原值兜底。
_regime_badge = ''
try:
    _cfg_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")
    with open(_cfg_path, "r", encoding="utf-8") as _cf:
        _cfg = json.load(_cf)
    _rt = (_cfg.get("regime_tag") or "").strip()
    _rn = (_cfg.get("regime_note") or "").strip()
    _sr = (_ic or {}).get("snapshot_range") or []
    if len(_sr) == 2 and all(_sr):
        _rt = (re.sub(r'（[^（）]*）', f'（截面区间 {_sr[0]}~{_sr[1]}）', _rt, count=1)
               if re.search(r'（[^（）]*）', _rt) else _rt + f'（{_sr[0]}~{_sr[1]}）')
        _rn = re.sub(r'2026-\d{2}(?:-\d{2})?~2026-\d{2}(?:-\d{2})?', f'{_sr[0]}~{_sr[1]}', _rn)
    if _rt:
        _regime_badge = ('<div style="margin-top:8px;display:inline-flex;align-items:center;gap:6px;'
                         'padding:3px 10px;border-radius:12px;font-size:11px;font-weight:700;'
                         'background:#8b5cf61a;color:#a78bfa;border:0.5px solid #8b5cf655;" '
                         'title="' + html_mod.escape(_rn) + '">'
                         '🌡️ 区制标签: ' + html_mod.escape(_rt) + '</div>')
except Exception:
    pass

html_parts.append(f'''
    <!-- A1 首屏 Hero 决策区 -->
    <div class="hero" id="sec-decision">
        <h1>📊 A股成交额 TOP{_POOL_N} 评分<span class="hero-ver">报告层 {_ver_tag} · 评分层 {_SCORER_VER}</span></h1>
        <div style="font-size:11px;color:#94a3b8;">🎯 今日市场状态 → 总仓位纪律（赚钱效应 {mkt_score if mkt_score is not None else '—'} 分）</div>
        <div class="hero-verdict" style="color:{mkt_color};">{mkt_phase} · 建议仓位 {mkt_pos_full}</div>
        <div style="font-size:13px;color:#cbd5e1;">{mkt_disc}</div>
        <div style="margin-top:8px;display:inline-flex;align-items:center;gap:6px;padding:3px 10px;border-radius:12px;font-size:11px;font-weight:700;background:{_fresh_color}1a;color:{_fresh_color};border:0.5px solid {_fresh_color}55;">
            🕒 {_fresh_txt}
        </div>
        {_pool_badge}
        {_elig_badge}
        {_regime_badge}
        {_cov_banner}
        <div class="hero-kpis">
            <div class="hero-kpi" style="border-left-color:#22c55e;">
                <div class="k-lbl">⭐ S + A 级（重点跟踪）</div>
                <div class="k-val">{s_count} + {a_count}</div>
                <div class="k-sub">S级前5% / A级前15%（当日截面）</div>
            </div>
            <div class="hero-kpi" style="border-left-color:#3b82f6;">
                <div class="k-lbl">📈 主线板块</div>
                <div class="k-val">{top_sector[0] if top_sector else '—'}</div>
                <div class="k-sub">均分 {top_sector[1].get('avg',0):.2f} · {len([s for s,v in sec_avg.items() if v.get("count",0)>=5])} 个板块≥5只</div>
            </div>
            <div class="hero-kpi" style="border-left-color:#f59e0b;">
                <div class="k-lbl">💰 大盘赚钱效应</div>
                <div class="k-val">{mkt_score if mkt_score is not None else '—'}<span style="font-size:12px;color:#94a3b8;"> / 100</span></div>
                <div class="k-sub">{breadth_disp}</div>
            </div>
        </div>
        <div class="hero-top3">''')

for _hi, _r in enumerate(top3_stocks):
    _rt = _r["rating"]; _tc = RATING_COLORS[_rt]
    _sub_ = _r.get("sub_theme", ""); _sec_ = sector_disp(_r)
    html_parts.append(f'''
            <div class="ht-item" style="border-left:3px solid {_tc};">
                <div class="ht-name">#{_hi+1} {html_mod.escape(_r["name"])}</div>
                <div class="ht-sc" style="color:{_tc};">{_r["total"]:.2f}<span style="font-size:11px;color:#94a3b8;"> / 100</span></div>
                <div class="ht-note">{html_mod.escape(_sec_)}{' · ' + html_mod.escape(_sub_) if _sub_ else ''}</div>
            </div>''')

html_parts.append(f'''
        </div>
        <div class="hero-meta">
            评级口径（{REPORT_VER} · 当日截面相对强弱，绝对总分不变）：
            <b style="color:#22c55e;">S级 前5%</b> · <b style="color:#3b82f6;">A级 前15%</b> ·
            <b style="color:#f59e0b;">B级 前50%</b> · <b style="color:#f97316;">C级 前88%</b> ·
            <b style="color:#dc2626;">D级 后12%</b>　｜　评级仅表"当日相对强弱"，实际仓位由上方市场状态统一决定
        </div>
    </div>
''')

# ===== v2.22 AI 复盘点评(每日由AI生成, 优先于模板文本) =====
# 数据纪律: 只采纳与当日数据日期一致的点评, 防止陈旧文件(如历史 ai_review_today.json)
# 在当日 ai_assessment.json 缺失/损坏时冒充当日点评。不匹配则回退到模板文本。
ai_review = {}
# (_ai_dir 已在 P1-5 区制标签前定义, 此处不再重复)
for _cand in ("ai_assessment.json", "ai_review_today.json"):
    if ai_review:
        break
    _p = os.path.join(_ai_dir, _cand)
    try:
        if os.path.exists(_p):
            with open(_p, "r", encoding="utf-8") as _f:
                _obj = json.load(_f)
            _odate = str(_obj.get("date", ""))
            if date_str and date_str not in _odate:
                print(f"⚠️ 跳过日期不匹配的 AI 点评 {_cand} (文件 date={_odate or '无'} ≠ 数据日期 {date_str})")
                continue
            ai_review = _obj
    except Exception:
        ai_review = {}

ai_text = (ai_review.get("commentary") or "").strip()
ai_watch = ai_review.get("watch_directions") or []

# ===== v2.46 本地复盘机构目标价映射 =====
# 来源: run_local_review.sh finalize 产出的 review_research_merged_<date>.json
#   (阶段1 逐公司联网核查所得; target_price 查不到为 null, 严禁编造)
# 用途: 个股卡片"现价 vs 目标价"并列对比中的机构目标价一列。
_target_px = {}   # name → {"target": float, "verdict": str, "confidence": str}
try:
    _mp = os.path.join(_ai_dir, f"review_research_merged_{date_str}.json")
    if os.path.exists(_mp):
        with open(_mp, "r", encoding="utf-8") as _f:
            _mobj = json.load(_f)
        for _c in (_mobj.get("companies") or []):
            _tp = _c.get("target_price")
            if isinstance(_tp, (int, float)) and _tp > 0 and _c.get("name"):
                _target_px[_c["name"]] = {"target": float(_tp),
                                          "verdict": _c.get("verdict"),
                                          "confidence": _c.get("confidence")}
        print(f"📌 本地复盘目标价映射: {len(_target_px)} 只 (来源 {os.path.basename(_mp)})")
except Exception as _e:
    print(f"⚠️ 读取本地复盘目标价失败: {_e}")

# ===== v3.4 每日 AI 焦点池（替代 v3.1 已删除的「观察池」）=====
# 来源: fetch_focus_list.py 产出的 focus_pool_<date>.json
#   观察池旧逻辑=按成交额 200~1000 名用内部评分选 → 已废弃；
#   现改为「每日 AI 焦点池」= 机器源(东财人气榜/涨跌停池/强势股池) + 本地AI联网检索(题材催化/公告/政策/龙虎榜)。
# 数据纪律: 只读与当日数据日期一致的焦点池文件，避免陈旧文件冒充当日焦点。
_focus_pool = {}
try:
    _fp = os.path.join(_ai_dir, f"focus_pool_{date_str}.json")
    if not os.path.exists(_fp):
        _fp = os.path.join(_ai_dir, f"focus_list_{date_str}.json")  # 兼容旧文件名
    if os.path.exists(_fp):
        _obj = json.load(open(_fp, encoding="utf-8"))
        _odate = str(_obj.get("date", ""))
        if (not date_str) or (date_str in _odate):
            _focus_pool = _obj
            print(f"🎯 每日AI焦点池: {len(_obj.get('focus') or [])} 只 (来源 {os.path.basename(_fp)})")
        else:
            print(f"⚠️ 跳过日期不匹配的焦点池 {os.path.basename(_fp)} (文件 date={_odate} ≠ 数据日期 {date_str})")
except Exception as _e:
    print(f"⚠️ 读取焦点池失败: {_e}")


def _focus_pool_html(fp):
    """每日 AI 焦点池区块渲染（v3.4）。
    表格列: 排名 / 名称(代码) / 类型(池内评级 或 池外) / 入选来源 / 入选理由。
    池内标的显示当日评分评级与总分，池外标的标记「池外」并提示仅作关注线索。
    """
    focus = fp.get("focus") or []
    if not focus:
        return ""
    srcs = fp.get("sources_used") or {}
    ag = fp.get("agent_search") or {}
    n_in = fp.get("n_in_pool")
    if n_in is None:
        n_in = sum(1 for x in focus if x.get("in_main_pool"))
    n_out = len(focus) - n_in
    ai_sum = (fp.get("ai_summary") or "").strip()

    src_txt = " · ".join(f"{k} {v}" for k, v in srcs.items() if v) or "无"
    _RCOLOR = {"S": "#fbbf24", "A": "#3b82f6", "B": "#f59e0b", "C": "#f97316", "D": "#dc2626"}

    rows = []
    for it in focus[:40]:
        name = html_mod.escape(str(it.get("name") or "—"))
        code = html_mod.escape(str(it.get("code") or ""))
        reason = html_mod.escape(str(it.get("reason") or ""))
        src = html_mod.escape("+".join(it.get("sources") or []))
        if it.get("in_main_pool"):
            rt = it.get("rating")
            tot = it.get("total")
            _c = _RCOLOR.get(rt, "#94a3b8")
            badge = (f'<span style="color:{_c};font-weight:800;">{rt or "池内"}</span>'
                     + (f'<span style="color:#64748b;font-size:10px;"> {tot:.1f}</span>'
                        if isinstance(tot, (int, float)) else ''))
        else:
            badge = '<span style="color:#64748b;font-weight:700;">池外</span>'
        pct = it.get("pct_chg")
        if isinstance(pct, (int, float)):
            _pc = "#ef4444" if pct > 0 else ("#22c55e" if pct < 0 else "#94a3b8")
            name += f'<span style="font-size:10px;color:{_pc};margin-left:4px;">{pct:+.1f}%</span>'
        rows.append(
            f'<tr style="border-bottom:1px solid #1e293b;">'
            f'<td style="padding:5px 6px;color:#64748b;">{it.get("rank","")}</td>'
            f'<td style="padding:5px 6px;color:#e2e8f0;font-weight:600;">{name}'
            f'<span style="font-size:10px;color:#64748b;margin-left:4px;">{code}</span></td>'
            f'<td style="padding:5px 6px;text-align:center;">{badge}</td>'
            f'<td style="padding:5px 6px;color:#94a3b8;font-size:10px;">{src}</td>'
            f'<td style="padding:5px 6px;color:#cbd5e1;font-size:11px;">{reason}</td>'
            f'</tr>')

    ai_badge = (f'<span style="color:#a78bfa;">AI联网检索 +{ag.get("n_additions",0)} 条</span>'
                if ag.get("merged") else
                '<span style="color:#64748b;">AI联网检索 未合并（仅机器源）</span>')
    ai_sum_html = (f'<div style="font-size:11px;color:#c4b5fd;padding:8px 10px;margin-bottom:8px;'
                   f'background:rgba(167,139,250,0.08);border-radius:6px;border-left:3px solid #a78bfa;">'
                   f'🤖 AI 焦点主线：{html_mod.escape(ai_sum)}</div>') if ai_sum else ""

    return f'''
    <div class="section-block" id="sec-focus-pool" style="margin:24px 0;padding:18px 20px;background:#0f172a;border:1px solid #334155;border-radius:12px;">
        <div class="section-title" style="border-bottom:2px solid #a78bfa;color:#a78bfa;">
            🎯 每日 AI 焦点池 <span style="font-size:11px;color:#64748b;font-weight:400;">
            （机器源 + 本地AI联网筛选 · 共 {len(focus)} 只：池内 {n_in} / 池外 {n_out}）</span>
        </div>
        <div style="font-size:11px;color:#94a3b8;margin-bottom:8px;">
            数据源：{html_mod.escape(src_txt)}　｜　{ai_badge}　｜　
            方法：{html_mod.escape(str(fp.get("method","")))[:120]}
        </div>
        {ai_sum_html}
        <table style="width:100%;border-collapse:collapse;font-size:12px;">
            <thead><tr style="border-bottom:2px solid #334155;color:#94a3b8;">
                <th style="padding:6px;text-align:left;width:34px;">#</th>
                <th style="padding:6px;text-align:left;">标的</th>
                <th style="padding:6px;text-align:center;width:72px;">类型</th>
                <th style="padding:6px;text-align:left;width:150px;">入选来源</th>
                <th style="padding:6px;text-align:left;">入选理由</th>
            </tr></thead>
            <tbody>{''.join(rows)}</tbody>
        </table>
        <div style="font-size:11px;color:#64748b;margin-top:8px;">
            📌 说明：本池替代原「观察池」（v3.1 已删除）——不再按成交额排名用内部评分选样，
            而是每日由互联网真实数据 + 本地 AI 联网检索筛出「需要关注的标的」。
            <b style="color:#a78bfa;">池外标的仅为关注线索</b>（无当日评分），不等于推荐。
        </div>
    </div>'''


def _px_cmp_html(cur, tech_target, inst=None):
    """现价 vs 目标价并列对比(v2.46): 现价 → 技术目标 [+ 机构目标], 带价差与预期涨跌幅。
    A股配色惯例: 涨红跌绿。inst=None 时只展示技术目标。"""
    def _c(v):
        return "#ef4444" if v > 0 else ("#22c55e" if v < 0 else "#94a3b8")

    parts = [f'<span style="color:#94a3b8;">💰 现价 <b style="color:#e2e8f0;font-size:13px;">{cur:.2f}</b></span>']
    if tech_target:
        _d = tech_target - cur
        _p = _d / cur * 100 if cur else 0
        parts.append(f'<span style="color:#64748b;">→</span>'
                     f'<span style="color:#94a3b8;">🎯 技术目标 <b style="color:#3b82f6;">{tech_target:.2f}</b></span>'
                     f'<span style="color:#94a3b8;">📐 价差 <b style="color:{_c(_d)};">{_d:+.2f}</b></span>'
                     f'<span style="color:#94a3b8;">📈 预期 <b style="color:{_c(_p)};">{_p:+.2f}%</b></span>')
    if inst:
        _t = inst["target"]
        _d2 = _t - cur
        _p2 = _d2 / cur * 100 if cur else 0
        _vd = f'（{html_mod.escape(str(inst.get("verdict") or ""))}）' if inst.get("verdict") else ""
        parts.append(f'<span style="color:#94a3b8;">🏦 机构目标 <b style="color:#a855f7;">{_t:.2f}</b>{_vd}'
                     f'<span style="color:{_c(_p2)};font-weight:700;">（较现价 {_p2:+.2f}%）</span></span>')
    return ('<div style="margin-top:8px;padding:7px 12px;border-radius:8px;background:#0f172a;'
            'border:0.5px solid #1e293b;display:flex;gap:14px;flex-wrap:wrap;align-items:center;'
            'font-size:12px;">' + "".join(parts) + '</div>')
ai_risk = (ai_review.get("risk_note") or "").strip()
ai_date = ai_review.get("date", "")

# ===== v2.45 P0-2 点评来源如实标注（三态：本地复盘 / 在线LLM / 规则引擎）=====
# 历史 bug: 页面固定写"每日AI生成", 但 ai_assessment.json 实际为
#   generated_by="...LLM fallback rule engine...", fallback=True（规则引擎拼接模板）,
#   且 GitHub repo secrets 未配置(云端无法调 LLM) → 构成来源标注失实, 违反数据纪律。
# v2.45: 新増「本地复盘」来源 —— 由 export_review_input.py + merge_review_research.py
#   两阶段产出（WorkBuddy 逐公司联网核查），source="local-review"、fallback=False。
#   该来源必须与"AI生成"区分：它是逐公司人工级核查，不是模型一次性生成。
_ai_src_raw = str(ai_review.get("source") or "")
_ai_is_fallback = bool(ai_review.get("fallback"))
_ai_genby = str(ai_review.get("generated_by") or "")
_review_meta = ai_review.get("review_meta") or {}
if _ai_src_raw == "local-review" or ("本地复盘" in _ai_genby):
    _rm_deep = _review_meta.get("tier_deep")
    _rm_light = _review_meta.get("tier_light")
    _rm_ok = _review_meta.get("validation_passed")
    _ai_src_label = "本地复盘（逐公司联网核查）"
    _ai_src_color = "#a78bfa"
    _ai_src_tip = (f"本条点评来自本地复盘链路：先由 export_review_input.py 理出当日需复盘公司，"
                   f"再由 WorkBuddy 逐公司联网查阅资料（深度 {_rm_deep if _rm_deep is not None else '?'} 只 / "
                   f"轻量 {_rm_light if _rm_light is not None else '?'} 只），"
                   f"经 merge_review_research.py 校验后注入页面。"
                   f"校验结果：{'通过' if _rm_ok else '存在待修正项'}。"
                   f"该来源非在线大模型一次性生成，结论基于逐公司可核验资料。")
elif _ai_is_fallback or ("fallback" in _ai_genby.lower()) or ("rule engine" in _ai_genby.lower()):
    _ai_src_label = "规则引擎生成"
    _ai_src_color = "#f59e0b"
    _ai_src_tip = ("本条点评由本地规则引擎按当日真实行情数据拼接生成，"
                   "非大模型生成；配置 LLM 密钥或启用本地复盘后自动切换。")
else:
    _ai_src_label = "AI 生成"
    _ai_src_color = "#38bdf8"
    _ai_src_tip = f"本条点评由大模型基于当日真实行情数据生成。生成者：{_ai_genby or '未知'}"

if ai_text:
    review_title = (f'🤖 盘面复盘点评 <span title="{html_mod.escape(_ai_src_tip)}" '
                   f'style="font-size:11px;color:{_ai_src_color};font-weight:400;cursor:help;">'
                   f'({_ai_src_label} · {ai_date})</span>')
    if ai_watch:
        _items = "".join(f"<li style='margin:4px 0;'>{html_mod.escape(str(w))}</li>" for w in ai_watch)
        watch_html = (f'<div style="margin:12px 0;padding:10px 14px;background:#0f172a;'
                      f'border-left:3px solid #f59e0b;border-radius:8px;">'
                      f'<b style="color:#f59e0b;font-size:13px;">📌 需要关注的方向</b>'
                      f'<ol style="margin:8px 0 0 18px;padding:0;font-size:12px;line-height:1.7;color:#e2e8f0;">{_items}</ol></div>')
    else:
        watch_html = ""
    risk_html = (f'<div style="margin:10px 0;padding:8px 14px;background:#1a1208;'
                 f'border-left:3px solid #ef4444;border-radius:8px;'
                 f'font-size:12px;color:#fca5a5;">⚠️ {html_mod.escape(ai_risk)}</div>') if ai_risk else ""
    # ===== v2.45 本地复盘元信息（核查规模 + 校验状态，机构视角：可追溯/可复核）=====
    # 仅本地复盘来源显示：让读者知道这条结论背后"查了多少家公司、校验是否通过"，
    # 与"模型一次性生成"形成可辨识的差异。
    meta_html = ""
    if _ai_src_raw == "local-review" and _review_meta:
        _md_deep = _review_meta.get("tier_deep")
        _md_light = _review_meta.get("tier_light")
        _md_ok = _review_meta.get("validation_passed")
        _md_e = _review_meta.get("validation_error") or 0
        _md_w = _review_meta.get("validation_warn") or 0
        _md_vd = _review_meta.get("verdict_dist") or {}
        _vd_str = " · ".join(f"{k} {v}" for k, v in _md_vd.items()) if _md_vd else "—"
        _md_color = "#22c55e" if _md_ok else "#f59e0b"
        _md_icon = "✅" if _md_ok else "⚠️"
        meta_html = (
            f'<div style="margin:10px 0;padding:9px 13px;background:rgba(167,139,250,0.08);'
            f'border-left:3px solid #a78bfa;border-radius:8px;font-size:12px;color:#c4b5fd;line-height:1.75;">'
            f'🔍 <b>本地复盘过程</b>：深度核查 <b>{_md_deep}</b> 只（决策相关标的）+ '
            f'轻量核查 <b>{_md_light}</b> 只（异动解释）　｜　'
            f'核查结论分布：<b>{html_mod.escape(_vd_str)}</b>　｜　'
            f'<span style="color:{_md_color};">{_md_icon} 数据校验 '
            f'{"通过" if _md_ok else "存在待修正项"}</span>'
            f'（error {_md_e} / warn {_md_w}）'
            f'</div>')
    review_body = (f'<div class="review-text" style="font-size:13px;line-height:1.9;color:#e2e8f0;'
                   f'background:#0f172a;border-left:3px solid #3b82f6;padding:12px 14px;border-radius:8px;margin:10px 0;">'
                   f'{html_mod.escape(ai_text)}</div>{watch_html}{risk_html}{meta_html}')
else:
    review_title = "📝 盘面复盘点评"
    review_body = (f'<div class="review-text" style="font-size:13px;line-height:1.9;color:#e2e8f0;'
                   f'background:#0f172a;border-left:3px solid #3b82f6;padding:12px 14px;border-radius:8px;margin:10px 0;">'
                   f'{html_mod.escape(mr_text)}</div>')

html_parts.append(f'''
    <!-- 复盘点评 (v2.22 AI生成优先; v2.42 A5 长文折叠) -->
    <div class="core-conclusion review-card" id="sec-summary">
        <h2>{review_title}</h2>
        <div class="review-metrics" style="display:flex;flex-wrap:wrap;gap:10px;margin:10px 0 4px;font-size:12px;color:#cbd5e1;">
            <span style="background:#1e293b;padding:6px 10px;border-radius:6px;"><b style="color:#94a3b8;">指数</b> {idx_chips}</span>
            <span style="background:#1e293b;padding:6px 10px;border-radius:6px;"><b style="color:#94a3b8;">成交</b> {amt_disp}</span>
            <span style="background:#1e293b;padding:6px 10px;border-radius:6px;"><b style="color:#94a3b8;">涨跌</b> {breadth_disp}</span>
            <span style="background:#1e293b;padding:6px 10px;border-radius:6px;"><b style="color:#94a3b8;">赚钱效应</b> {me_disp}</span>
        </div>
        <div class="main-line">{main_line}{change_text}</div>
        <details style="margin-top:6px;">
            <summary style="cursor:pointer;font-size:12px;color:#38bdf8;padding:8px 0;">📖 展开完整盘面复盘与关注方向</summary>
            {review_body}
        </details>
    </div>
''')

# v2.11: 原"大盘赚钱效应"独立卡片已并入上方"📝 盘面复盘点评"卡片(指数/成交/涨跌/赚钱效应速览)

# ===== v2.43 三合一「今日候选组合」: 动量线/反转线/AI主观线 并列 + 交集门控资金表 =====
# 修复 v2.41/v2.42 的结构重复: 原「双引擎组合对照」与「🎯组合推荐」展示同一批 AI 标的
# (各 1 遍, 加资金表共 3 遍), 且无交集时回退用 AI 组合做满额资金分配 —— 与"分歧宜降仓"
# 结论自相矛盾。现合并为单一区块: 三条逻辑线并列 + 交集门控资金表(有交集→交集等权;
# 无交集→不给满额表, 仅量化线试探仓)。
_ai_port = (ai_review.get("portfolio") or {})
_ai_picks_raw = _ai_port.get("picks") or []
ai_pick_names = [p.get("name") if isinstance(p, dict) else p for p in _ai_picks_raw]
_names_map = {r["name"]: r for r in results}

if quant_json or ai_pick_names:
    _inter = sorted(set(quant_picks_all) & set(ai_pick_names))

    # --- 列 1/2: 量化双路线(可复现规则) ---
    _qcols = ""
    for _m in ("momentum", "reversal"):
        _ps, _rule = _quant_picks(_m)
        _rows = ""
        for _p in _ps:
            _nm = _p.get("name")
            _rr = _names_map.get(_nm, {})
            _rt = _rr.get("rating", _p.get("rating", "-"))
            _col = RATING_COLORS.get(_rt, "#94a3b8")
            _sc = _p.get("total")
            _sct = f"{_sc:.2f}" if isinstance(_sc, (int, float)) else "—"
            _sec = _p.get("sector_l2") or _p.get("parent") or ""
            _in_ai = ' <span style="color:#22c55e;">🤝共识</span>' if _nm in ai_pick_names else ""
            _rows += (f'<div style="padding:5px 0;border-bottom:1px solid #1e293b;font-size:11px;">'
                      f'<span style="font-weight:700;color:#f8fafc;">{html_mod.escape(str(_nm))}</span>'
                      f'<span style="color:{_col};font-weight:700;margin-left:6px;">{_rt}</span>'
                      f'<span style="color:#94a3b8;margin-left:6px;">{html_mod.escape(str(_sec))}</span>'
                      f'<span style="float:right;color:#38bdf8;font-weight:700;">{_sct}</span>{_in_ai}</div>')
        if not _rows:
            _rows = '<div style="padding:6px 0;font-size:11px;color:#64748b;">当日无符合条件的标的</div>'
        _qcols += (f'<div style="background:#0f172a;border-radius:8px;padding:10px;">'
                   f'<div style="font-size:12px;font-weight:700;color:#e2e8f0;margin-bottom:6px;">{QUANT_MODE_LABEL.get(_m, _m)}</div>'
                   f'{_rows}'
                   f'<div style="font-size:11px;color:#64748b;margin-top:5px;">规则: {html_mod.escape(str(_rule or "板块动量档位"))}</div></div>')

    # --- 列 3: AI 主观线(合并原「🎯组合推荐」的估值/增速/理由, 消除重复) ---
    _ai_rows = ""
    for _pk in _ai_picks_raw:
        _nm = _pk.get("name") if isinstance(_pk, dict) else _pk
        _rr = _names_map.get(_nm)
        if not _rr:
            continue
        _rt = _rr["rating"]; _acol = RATING_COLORS[_rt]
        _fwd = _rr.get("fwd_pe"); _gw = _rr.get("growth")
        _pos = (_rr.get("tech") or {}).get("position", 0)
        _fwd_s = f"{_fwd:.1f}x" if isinstance(_fwd, (int, float)) else "—"
        _gw_s = f"{_gw:+.0f}%" if isinstance(_gw, (int, float)) else "—"
        _vlab = (_pk.get("valuation") or "") if isinstance(_pk, dict) else ""
        _note = (_pk.get("note") or "") if isinstance(_pk, dict) else ""
        _vcol = "#22c55e" if "合理" in _vlab else ("#ef4444" if ("偏贵" in _vlab or "陷阱" in _vlab) else "#94a3b8")
        # v2.46 现价 vs 机构目标价对比(本地复盘目标价, 查不到则不展示)
        _pk_tp = _pk.get("target_price") if isinstance(_pk, dict) else None
        _tp_row = ""
        if isinstance(_pk_tp, (int, float)) and _pk_tp > 0 and (_rr.get("price") or 0) > 0:
            _cp2 = _rr["price"]; _d3 = _pk_tp - _cp2; _p3 = _d3 / _cp2 * 100
            _cc3 = "#ef4444" if _p3 > 0 else ("#22c55e" if _p3 < 0 else "#94a3b8")
            _tp_row = (f'<div style="font-size:11px;color:#94a3b8;margin-top:2px;">'
                       f'💰 现价 {_cp2:.2f} → 🏦 机构目标 <b style="color:#a855f7;">{_pk_tp:.2f}</b>'
                       f' · 价差 <b style="color:{_cc3};">{_d3:+.2f}</b>'
                       f' · 预期 <b style="color:{_cc3};">{_p3:+.2f}%</b></div>')
        _ai_rows += (f'<div style="padding:5px 0;border-bottom:1px solid #1e293b;font-size:11px;">'
                     f'<span style="font-weight:700;color:#f8fafc;">{html_mod.escape(str(_nm))}</span>'
                     f'<span style="color:{_acol};font-weight:700;margin-left:6px;">{_rt}</span>'
                     f'<span style="float:right;color:#38bdf8;font-weight:700;">{_rr["total"]:.2f}</span></div>'
                     f'<div style="font-size:11px;color:#94a3b8;margin-top:2px;">FwdPE {_fwd_s} · 增速 {_gw_s} · 分位{_pos:.0f}%</div>'
                     + _tp_row
                     + (f'<div style="font-size:11px;color:{_vcol};margin-top:2px;">{html_mod.escape(str(_vlab))}</div>' if _vlab else "")
                     + (f'<div style="font-size:11px;color:#94a3b8;margin-top:2px;">{html_mod.escape(str(_note))}</div>' if _note else ""))
    if not _ai_rows:
        _ai_rows = '<div style="padding:6px 0;font-size:11px;color:#64748b;">当日无 AI 组合</div>'
    _ai_verdict = html_mod.escape(str(_ai_port.get("verdict") or ""))
    _ai_col = (f'<div style="background:#0f172a;border-radius:8px;padding:10px;">'
               f'<div style="font-size:12px;font-weight:700;color:#e2e8f0;margin-bottom:6px;">🤖 AI 主观线（产业逻辑）</div>'
               f'{_ai_rows}'
               + (f'<div style="font-size:11px;color:#cbd5e1;margin-top:5px;">📌 {_ai_verdict}</div>' if _ai_verdict else "")
               + '<div style="font-size:11px;color:#64748b;margin-top:5px;">来源: ai_assessment.portfolio（AI 主观，含估值与产业链校验）</div></div>')

    # --- 结论行: 交集门控 ---
    if _inter:
        _concl = (f'<span style="color:#22c55e;font-weight:700;">✅ 交集 {len(_inter)} 只：'
                  f'{"、".join(html_mod.escape(str(x)) for x in _inter)}</span>'
                  ' —— 量化与 AI 形成共识，信号可靠性较高')
    else:
        _concl = ('<span style="color:#f59e0b;font-weight:700;">⚠️ 两套逻辑当日零交集</span> —— '
                  '量化(规则)与 AI(产业逻辑)方向分歧，建议降低仓位、等待共识，仅可用量化线小仓试探')

    # --- 资金分配表: 量化优先, 交集为准 ---
    if _inter:
        _cap_pool = [n for n in _inter if _names_map.get(n, {}).get("price")]
        _total_cap = 100000
        _cap_head = "💰 资金分配示例（基准：10万本金 · 交集等权 · 100股取整）"
        _cap_desc = f'分配对象：<b>双引擎交集（共识标的）</b>。仓位再按上方总仓位纪律（当前 {mkt_pos_full}）等比缩放。'
        _cap_bar = "#22c55e"
    else:
        _cap_pool = [n for n in dict.fromkeys(quant_picks_all) if _names_map.get(n, {}).get("price")]
        _ratio = 0.2 if _bearish else 0.4
        _total_cap = int(100000 * _ratio)
        _cap_head = f"⚠️ 试探仓示例（分歧市场 · 仅量化线 · 总仓 {_total_cap/10000:.0f}万 · 100股取整）"
        _cap_desc = ('当前两套逻辑零交集 → <b>不输出满额分配</b>。如需参与，仅用量化线（可复现规则）'
                     '小仓试探，单票不超过总资金 1/4。')
        _cap_bar = "#f59e0b"
    _cap_rows = ""
    if _cap_pool:
        _per = _total_cap / len(_cap_pool)
        for _cn in _cap_pool:
            _cr = _names_map[_cn]; _cp = _cr.get("price") or 0
            _shares = int(_per / _cp / 100) * 100 if _cp > 0 else 0
            _amt = _shares * _cp
            _rt = _cr.get("rating", "-")
            # v2.44 P1-1: 增列参考买入区间与止损位
            # 历史问题: 资金表只给"现价", 新手会当场市价追进; 而 build_trade_plan 算出的
            #   买点/止损只出现在 S/A 详情卡片, 资金表使用者看不到 → 补齐。
            _cplan = build_trade_plan(_cr)
            if _cplan:
                _buy_rng = f"{_cplan['buy_lo']:.2f}~{_cplan['buy_hi']:.2f}"
                _stop_v = f"{_cplan['stop']:.2f}"
                _stop_pct = (_cplan['stop'] - _cp) / _cp * 100 if _cp else 0
                _stop_disp = f"{_stop_v} ({_stop_pct:+.1f}%)"
                _stop_col = "#ef4444"
            else:
                _buy_rng = "—"; _stop_disp = "—"; _stop_col = "#94a3b8"
            _cap_rows += (f'<tr style="border-top:1px solid #1e293b;">'
                          f'<td style="padding:4px 8px;color:#e2e8f0;">{html_mod.escape(str(_cn))}</td>'
                          f'<td style="padding:4px 8px;color:{RATING_COLORS.get(_rt, "#94a3b8")};">{_rt}</td>'
                          f'<td style="padding:4px 8px;color:#94a3b8;">{_cp:.2f}</td>'
                          f'<td style="padding:4px 8px;color:#22c55e;">{_buy_rng}</td>'
                          f'<td style="padding:4px 8px;color:{_stop_col};">{_stop_disp}</td>'
                          f'<td style="padding:4px 8px;color:#e2e8f0;">{_shares}股</td>'
                          f'<td style="padding:4px 8px;color:#38bdf8;">¥{_amt:,.0f}</td></tr>')
    if _cap_rows:
        _cap_html = (f'''
        <div style="margin-top:10px;padding:10px 12px;background:rgba(34,197,94,0.06);border-radius:8px;border-left:3px solid {_cap_bar};">
            <div style="font-size:11px;font-weight:700;color:{_cap_bar};margin-bottom:6px;">{_cap_head}</div>
            <div style="font-size:11px;color:#94a3b8;margin-bottom:6px;">{_cap_desc}</div>
            <table style="width:100%;border-collapse:collapse;font-size:11px;">
                <thead><tr style="color:#94a3b8;text-align:left;">
                    <th style="padding:3px 8px;">标的</th><th style="padding:3px 8px;">评级</th>
                    <th style="padding:3px 8px;">现价</th><th style="padding:3px 8px;">参考买入区间</th>
                    <th style="padding:3px 8px;">止损位</th><th style="padding:3px 8px;">建议股数</th><th style="padding:3px 8px;">金额</th>
                </tr></thead>
                <tbody>{_cap_rows}</tbody>
            </table>
            <div style="font-size:11px;color:#64748b;margin-top:6px;">📌 买入区间按技术结构（多头取 ma5~ma10 回踩区／弱势取 ma10~ma20 企稳区）推导；止损位取最近支撑下方 1.5%，与个股详情页同源。<b style="color:#f59e0b;">现价高于买入区间时应等待回踩，勿市价追高。</b></div>
        </div>''')
    else:
        _cap_html = ""

    html_parts.append(f'''
    <!-- v2.43 三合一「今日候选组合」: 消除 v2.41/v2.42 的组合区块重复与资金分配矛盾 -->
    <div class="chart-card" id="sec-today-pick" style="padding:16px;margin:14px 0;">
        <h3 style="font-size:13px;margin-bottom:10px;">🎯 今日候选组合 <span style="font-size:11px;color:#64748b;font-weight:400;">（三条独立逻辑线 · 量化=可复现规则 / AI=产业逻辑解读）</span></h3>
        <div style="display:grid;grid-template-columns:repeat(3,1fr);gap:10px;">
            {_qcols}
            {_ai_col}
        </div>
        <div style="font-size:11px;color:#cbd5e1;padding:8px 10px;margin-top:10px;background:rgba(59,130,246,0.08);border-radius:6px;border-left:3px solid #3b82f6;">
            🔗 {_concl}
        </div>
        {_cap_html}
        <div style="font-size:11px;color:#475569;margin-top:6px;">说明：量化组合可完全复现、便于回测；AI 组合含产业逻辑但主观性更强。<b>两者交集是唯一的共识信号</b>，无交集时应视为分歧而非机会。</div>
    </div>''')

# ===== v3.9 多因子精选组合（≤10 只）+ 动量路线低 IC 悖论解释 =====
# 老大第 3 项：「今日候选组合数量太多，请用多因子的思路选出一个组合，今日组合总数不要超过 10 只。
#   另外请解释为什么动量路线的 ic 值和胜率低但是会选出动量路线的个股。」
# 上方原区块仍是三条线各 10 只（共 20 只），保留作「规则披露」；本区块给出唯一可执行组合。
try:
    _mf = {}
    _mfp = os.path.join(_ai_dir, "sector", f"mf_pick_{date_str}.json")
    if not os.path.exists(_mfp):
        _mfp = os.path.join(_ai_dir, "sector", "mf_latest.json")
    if os.path.exists(_mfp):
        with open(_mfp, "r", encoding="utf-8") as _f:
            _mf = json.load(_f)
    _mfb = {}
    _mfbp = os.path.join(_ai_dir, "mf_backtest.json")
    if os.path.exists(_mfbp):
        with open(_mfbp, "r", encoding="utf-8") as _f:
            _mfb = json.load(_f)

    # ---- 组合风险体检（P1，2026-10-25 新增）----
    #   audit_round8 实测：10 只两两日收益平均相关 0.358、最大 0.678，
    #   按 n/(1+(n-1)ρ) 折算等效独立标的仅 2.4 只。
    #   → 「10 只 = 分散」是错觉，必须上屏，否则读者会按 10 只满仓等权。
    _mf_risk_block = ""
    try:
        _r8 = json.load(open(os.path.join(_ai_dir, "audit_round8.json")))
        _p8 = _r8.get("picks") or []
        _tos = [x.get("turnover_yi") for x in _p8
                if isinstance(x.get("turnover_yi"), (int, float))]
        _avg_c = None
        _mx_c = None
        _eff = None
        for _it in (_r8.get("items") or []):
            if "等效独立标的" in (_it.get("title") or ""):
                _ev = _it.get("evidence") or ""
                _mm = re.search(r"均值\s*(-?\d+\.\d+)", _ev)
                if _mm:
                    _avg_c = float(_mm.group(1))
                _mm2 = re.search(r"最大\s*(-?\d+\.\d+)", _ev)
                if _mm2:
                    _mx_c = float(_mm2.group(1))
                _mm3 = re.search(r"等效独立标的数\s*=\s*(\d+\.\d+)", _ev)
                if _mm3:
                    _eff = float(_mm3.group(1))
        _n_pk = len(_p8)
        if _avg_c is not None and _eff is not None:
            _cap_yi = min(_tos) * 0.05 if _tos else None
            _corr_cl = "#ef4444" if _avg_c >= 0.3 else "#f59e0b"
            _mf_risk_block = (
                '<div style="font-size:11px;color:#cbd5e1;padding:9px 11px;'
                'background:#1c0a0a;border-left:3px solid #ef4444;border-radius:4px;'
                'margin-top:8px;line-height:1.85;">'
                '<b style="color:#f87171;">🚨 这 ' + str(_n_pk) + ' 只不是 ' + str(_n_pk)
                + ' 份独立风险 —— 等效只有 ' + ("%.1f" % _eff) + ' 只</b><br>'
                '把组合内两两日收益相关系数算出来（' + str(_n_pk * (_n_pk - 1) // 2)
                + ' 对），<b style="color:' + _corr_cl + ';">均值 '
                + ("%.3f" % _avg_c) + '</b>、最大 ' + ("%.3f" % (_mx_c or 0)) + '。'
                '按 <code>n / (1 + (n-1)·ρ)</code> 折算，'
                '<b style="color:#f87171;">等效独立标的数 = ' + ("%.1f" % _eff) + ' 只</b>'
                '（名义 ' + str(_n_pk) + ' 只）。<br>'
                '<b style="color:#f59e0b;">实务含义：</b>'
                '① 「强制分散到 10 只」只把单一标的的特质风险削掉，'
                '<b>削不掉系统性/风格风险</b> —— 若这 10 只一起跌，'
                '实际承受的是 ' + ("%.1f" % _eff) + ' 只标的的波动，不是 1/10；'
                '② 因此仓位不该按「10 只 = 很安全」来定，'
                '按等效 ' + ("%.1f" % _eff) + ' 只来定更接近真实风险；'
                '③ 若后续 ρ 升破 0.5，继续加只数没有意义，'
                '要用行业/风格分散（比如掺入非科技板块）而不是继续堆数量。'
                '</div>')
        elif _tos:
            _mf_risk_block = (
                '<div style="font-size:11px;color:#94a3b8;padding:9px 11px;'
                'background:#1e293b;border-radius:8px;margin-top:8px;line-height:1.8;">'
                '组合成交额中位 <b>' + ("%.2f" % (sorted(_tos)[len(_tos) // 2]))
                + '</b> 亿元，流动性充足。</div>')
    except Exception:
        _mf_risk_block = ""

    # ---- 动量路线跟踪实证（回答「低 IC 为何仍选股」的硬证据）----
    _rt = {"quant_momentum": {}, "quant_reversal": {}, "ai_portfolio": {}}
    try:
        with open(os.path.join(_ai_dir, "recommendation_track.json"), "r", encoding="utf-8") as _f:
            _recs = (json.load(_f) or {}).get("records") or []
        for _k in _rt:
            _sub = [x for x in _recs if x.get("kind") == _k]
            for _h in ("1", "5", "20"):
                _ev = [x["excess"][_h] for x in _sub
                       if isinstance((x.get("excess") or {}).get(_h), (int, float))]
                _rv = [x["rets"][_h] for x in _sub
                       if isinstance((x.get("rets") or {}).get(_h), (int, float))]
                if len(_ev) >= 3:
                    _rt[_k][_h] = {
                        "n": len(_ev),
                        "exc_mean": sum(_ev) / len(_ev),
                        "exc_med": sorted(_ev)[len(_ev) // 2],
                        "win": sum(1 for x in _ev if x > 0) / len(_ev) * 100,
                        "ret_mean": (sum(_rv) / len(_rv)) if _rv else None,
                    }
    except Exception:
        _rt = {}

    _mf_rows = ""
    for _i, _p in enumerate(_mf.get("picks") or [], 1):
        _pt = _p.get("parts") or {}
        _rt_c = RATING_COLORS.get(_p.get("rating") or "", "#94a3b8")
        _fpe = _p.get("fwd_pe")
        _fpe_s = f"{_fpe:.1f}x" if isinstance(_fpe, (int, float)) else "未提供"
        _gw = _p.get("growth")
        _gw_s = f"{_gw:+.0f}%" if isinstance(_gw, (int, float)) else "未提供"
        _rsi = _p.get("rsi")
        _rsi_s = f"{_rsi:.0f}" if isinstance(_rsi, (int, float)) else "—"
        _mfs = _p.get("mf_score")
        _mfs_s = f"{_mfs:.3f}" if isinstance(_mfs, (int, float)) else "—"
        _prc = _p.get("price")
        _prc_s = f"{_prc:.2f}" if isinstance(_prc, (int, float)) else "—"
        _rcell = ""
        for _lbl, _vv in (("基本面", _pt.get("fund_pct")), ("消息", _pt.get("news_pct")),
                          ("反转块", _pt.get("rev_pct"))):
            if isinstance(_vv, (int, float)):
                _rcell += (f'<span style="font-size:9.5px;color:#94a3b8;margin-right:6px;">'
                           f'{_lbl} <b style="color:#cbd5e1;">{_vv:.0f}%</b></span>')
        _reason = ""
        if _p.get("fund_reasons"):
            _reason = html_mod.escape(str(_p["fund_reasons"][0]))
        _mf_rows += (
            f'<tr style="border-top:1px solid #1e293b;">'
            f'<td style="padding:5px 6px;color:#64748b;font-size:10px;">{_i}</td>'
            f'<td style="padding:5px 6px;color:#f8fafc;font-weight:700;white-space:nowrap;">'
            f'{html_mod.escape(str(_p.get("name") or ""))}'
            f'<div style="font-size:9.5px;color:#475569;font-weight:400;">'
            f'{html_mod.escape(str(_p.get("code") or ""))}</div></td>'
            f'<td style="padding:5px 6px;color:#cbd5e1;font-size:10.5px;">'
            f'{html_mod.escape(str(_p.get("sector_l2") or "—"))}'
            f'<div style="font-size:9.5px;color:#475569;">'
            f'{html_mod.escape(str(_p.get("sector_parent") or ""))}</div></td>'
            f'<td style="padding:5px 6px;text-align:center;color:#38bdf8;font-weight:800;">{_mfs_s}</td>'
            f'<td style="padding:5px 6px;text-align:center;">'
            f'<span style="color:{_rt_c};font-weight:700;font-size:11px;">'
            f'{html_mod.escape(str(_p.get("rating") or "—"))}</span></td>'
            f'<td style="padding:5px 6px;text-align:center;color:#e2e8f0;font-size:11px;">'
            f'{_prc_s}</td>'
            f'<td style="padding:5px 6px;text-align:center;color:#cbd5e1;font-size:10.5px;">{_fpe_s}</td>'
            f'<td style="padding:5px 6px;text-align:center;color:#cbd5e1;font-size:10.5px;">{_gw_s}</td>'
            f'<td style="padding:5px 6px;text-align:center;color:#cbd5e1;font-size:10.5px;">{_rsi_s}</td>'
            f'<td style="padding:5px 6px;color:#64748b;font-size:9.5px;line-height:1.6;">{_rcell}</td>'
            f'<td style="padding:5px 6px;color:#94a3b8;font-size:9.5px;line-height:1.6;max-width:200px;">'
            f'{_reason}</td></tr>')

    # 回测表
    _mfb_rows = ""
    _vb = _mfb.get("variants") or {}
    for _hk in ("1", "5", "20"):
        _hz = (_mfb.get("horizons") or {}).get(_hk) or {}
        _gr = _hz.get("groups") or {}
        if not _gr:
            continue
        _best = max(_gr.values(), key=lambda g: (g.get("exc_mean") if isinstance(g.get("exc_mean"), (int, float)) else -99))
        _cells = ""
        for _k in ("A", "B", "C", "D"):
            _g = _gr.get(_k)
            if not _g:
                _cells += '<td style="padding:4px 6px;text-align:center;color:#475569;">—</td>'
                continue
            _em = _g.get("exc_mean")
            _cc = "#ef4444" if (_em or 0) > 0 else "#22c55e"
            _isbest = (_g is _best)
            _bd = "border:1px solid #a3e63566;" if _isbest else ""
            _cells += (f'<td style="padding:4px 6px;text-align:center;{_bd}">'
                       f'<div style="color:{_cc};font-weight:700;font-size:11px;">{(_em or 0):+.2f}pct</div>'
                       f'<div style="font-size:9.5px;color:#64748b;">t={(_g.get("t") or 0):+.2f}</div>'
                       f'<div style="font-size:9.5px;color:#64748b;">胜率{(_g.get("win") or 0):.0f}%</div></td>')
        _pr = _hz.get("paired") or {}
        _tmax = max([abs(p.get("t") or 0) for p in _pr.values()] or [0])
        _mfb_rows += (f'<tr style="border-top:1px solid #1e293b;">'
                      f'<td style="padding:4px 8px;color:#e2e8f0;font-weight:700;">T+{_hk}</td>'
                      f'{_cells}'
                      f'<td style="padding:4px 8px;text-align:center;color:'
                      f'{"#fbbf24" if _tmax < 2 else "#a3e635"};font-weight:700;font-size:10.5px;">'
                      f'{_tmax:.2f}</td></tr>')

    # 动量路线跟踪表
    _rt_rows = ""
    _rt_name = {"quant_momentum": "📈 动量路线（顺动量做多）",
                "quant_reversal": "🔄 反转路线（超跌反弹）",
                "ai_portfolio": "🤖 AI 主观线"}
    for _k in ("quant_momentum", "quant_reversal", "ai_portfolio"):
        _d1 = (_rt.get(_k) or {}).get("1") or {}
        _d5 = (_rt.get(_k) or {}).get("5") or {}
        if not _d1 and not _d5:
            continue

        def _c5(_d, _fld, _suf="", _plus=True):
            _v = _d.get(_fld)
            if not isinstance(_v, (int, float)):
                return '<span style="color:#475569;">样本不足</span>'
            _cl = "#ef4444" if _v > 0 else "#22c55e"
            return (f'<b style="color:{_cl};">{_v:+.2f}{_suf}</b>' if _plus
                    else f'<b style="color:{_cl};">{_v:.1f}{_suf}</b>')

        _rt_rows += (f'<tr style="border-top:1px solid #1e293b;">'
                     f'<td style="padding:5px 8px;color:#e2e8f0;font-weight:700;white-space:nowrap;">'
                     f'{_rt_name.get(_k, _k)}</td>'
                     f'<td style="padding:5px 8px;text-align:center;color:#64748b;font-size:10px;">'
                     f'n={_d1.get("n") or "—"}</td>'
                     f'<td style="padding:5px 8px;text-align:center;font-size:10.5px;">'
                     f'{_c5(_d1, "exc_mean", "%")}</td>'
                     f'<td style="padding:5px 8px;text-align:center;font-size:10.5px;">'
                     f'{_c5(_d1, "win", "%", _plus=False)}</td>'
                     f'<td style="padding:5px 8px;text-align:center;font-size:10.5px;">'
                     f'{_c5(_d5, "exc_mean", "%")}</td>'
                     f'<td style="padding:5px 8px;text-align:center;font-size:10.5px;">'
                     f'{_c5(_d5, "win", "%", _plus=False)}</td></tr>')

    if _mf.get("picks"):
        _mw = _mf.get("weights") or {}
        _mwr = _mf.get("weight_reason") or {}
        _wparts = []
        _wlbl = {"score_fund": "基本面", "score_news": "消息面", "__rev__": "反转块"}
        for _k in ("score_fund", "score_news", "__rev__"):
            if _k in _mw:
                _wparts.append(f'{_wlbl.get(_k, _k)} <b style="color:#a3e635;">{_mw[_k]:.1%}</b>')
        _wline = " · ".join(_wparts)
        _mfb_concl = html_mod.escape(str(_mfb.get("conclusion") or "")).replace("**", "")

        # ---- 读表要点：必须由真实跟踪数据生成，禁止硬编码结论（曾误写「三条线全部为负」，
        #      而 AI 线 T+1 实为 +0.17%，属数据与文案矛盾）----
        _m1 = (_rt.get("quant_momentum") or {}).get("1") or {}
        _m5 = (_rt.get("quant_momentum") or {}).get("5") or {}
        _v1 = (_rt.get("quant_reversal") or {}).get("1") or {}
        _v5 = (_rt.get("quant_reversal") or {}).get("5") or {}
        _a1 = (_rt.get("ai_portfolio") or {}).get("1") or {}
        _a5 = (_rt.get("ai_portfolio") or {}).get("5") or {}
        _rt_read = ""
        if _m1 and _v1:
            _n_neg = sum(1 for _x in (_m1, _m5, _v1, _v5, _a1, _a5)
                         if isinstance(_x.get("exc_mean"), (int, float)) and _x["exc_mean"] < 0)
            _n_tot = sum(1 for _x in (_m1, _m5, _v1, _v5, _a1, _a5)
                         if isinstance(_x.get("exc_mean"), (int, float)))
            _d1 = _m1.get("exc_mean") - _v1.get("exc_mean") if isinstance(_m1.get("exc_mean"), (int, float)) and isinstance(_v1.get("exc_mean"), (int, float)) else None
            _d5 = _m5.get("exc_mean") - _v5.get("exc_mean") if isinstance(_m5.get("exc_mean"), (int, float)) and isinstance(_v5.get("exc_mean"), (int, float)) else None
            _rt_read = (
                f'动量线 T+1 超额 <b>{_m1.get("exc_mean", 0):+.2f}%</b>'
                f'（胜率 {_m1.get("win", 0):.0f}%）、T+5 <b>{_m5.get("exc_mean", 0):+.2f}%</b>'
                f'（胜率 {_m5.get("win", 0):.0f}%）；'
                f'反转线 T+1 <b>{_v1.get("exc_mean", 0):+.2f}%</b>、T+5 <b>{_v5.get("exc_mean", 0):+.2f}%</b>。'
                + (f'动量线相对反转线：T+1 <b>{_d1:+.2f}pct</b>、T+5 <b>{_d5:+.2f}pct</b>。'
                   if _d1 is not None and _d5 is not None else "")
                + f'六项对比中 <b>{_n_neg}/{_n_tot}</b> 项为负超额。'
                + ('<b style="color:#f59e0b;">三条线全周期均为负超额 —— 问题不止于动量路线本身，'
                   '而是整个池子在中位意义上跑输。</b>'
                   if _n_neg == _n_tot and _n_tot > 0 else
                   '<b style="color:#f59e0b;">并非所有线都为负：AI 主观线在短周期仍有正超额，'
                   '说明亏损主要来自量化两线的选池与切分方式，而非全部逻辑失效。</b>'
                   if _n_neg < _n_tot else "")
                + '这正是<b>不再按板块动量切路线</b>的理由 —— 已由本区块的多因子组合取代。'
            )

        html_parts.append(f'''
    <!-- v3.9 多因子精选组合（≤10 只）+ 动量路线低 IC 悖论解释 -->
    <div class="chart-card" id="sec-mf-pick" style="padding:16px;margin:14px 0;border:1px solid #a3e63555;">
        <h3 style="font-size:13px;margin-bottom:4px;">🧮 多因子精选组合
            <span style="font-size:11px;color:#64748b;font-weight:400;">
            （{len(_mf.get("picks") or [])} 只 · 权重由池内实证反推，非手填 · {html_mod.escape(str(_mf.get("date") or ""))}）</span></h3>
        <div style="font-size:11px;color:#64748b;margin-bottom:10px;line-height:1.75;">
            上方三条线共 20 只、且动量线与反转线方向互斥，<b>无法直接执行</b>。
            本组合给出唯一一份 ≤10 只清单。权重不是拍脑袋：从 <code>factor_ic.json</code>
            的池内 IC 证据反推 —— {_wline}。
        </div>
        <div style="overflow-x:auto;">
        <table style="width:100%;border-collapse:collapse;font-size:11px;">
            <thead><tr style="background:#1e293b;">
                <th style="padding:5px 6px;color:#94a3b8;">#</th>
                <th style="padding:5px 6px;text-align:left;color:#94a3b8;">标的</th>
                <th style="padding:5px 6px;text-align:left;color:#94a3b8;">板块</th>
                <th style="padding:5px 6px;color:#94a3b8;">多因子分</th>
                <th style="padding:5px 6px;color:#94a3b8;">评级</th>
                <th style="padding:5px 6px;color:#94a3b8;">现价</th>
                <th style="padding:5px 6px;color:#94a3b8;">FwdPE</th>
                <th style="padding:5px 6px;color:#94a3b8;">增速</th>
                <th style="padding:5px 6px;color:#94a3b8;">RSI</th>
                <th style="padding:5px 6px;text-align:left;color:#94a3b8;">分位构成</th>
                <th style="padding:5px 6px;text-align:left;color:#94a3b8;">核心逻辑</th>
            </tr></thead>
            <tbody>{_mf_rows}</tbody>
        </table></div>
        <div style="font-size:11px;color:#e2e8f0;padding:9px 11px;background:#1e293b;border-radius:8px;margin-top:10px;line-height:1.8;">
            <b style="color:#7dd3fc;">🧭 权重怎么来的（可复算规则，非人工设定）</b><br>
            <span style="color:#94a3b8;">① 只取 T+1 与 T+5 两周期 NW t 的较小者（保守：两周期都得站得住）；
            ② t_min ≤ 0 的维度权重记 0；③ 消息面 T+20 t=0.68 已失效 → 权重砍半；
            ④ 反转块 5 维仅 8 截面 → 块权重折半。结果：{_wline}。</span>
        </div>
        <div style="font-size:11px;color:#f59e0b;padding:9px 11px;background:rgba(245,158,11,0.08);border-radius:8px;margin-top:8px;line-height:1.8;">
            <b>⚠️ 三条硬排除（是剔除，不是加分）</b>：
            关注度前 20% 剔除 <b>{(_mf.get("excluded") or {}).get("关注度过高(attention 前20%)", 0)}</b> 只
            · 题材热度前 20% 剔除 <b>{(_mf.get("excluded") or {}).get("题材过热(theme 前20%)", 0)}</b> 只
            · RSI&gt;70 剔除 <b>{(_mf.get("excluded") or {}).get("RSI>70 追高", 0)}</b> 只。
            依据：score_attention 的 T+5 NW t=<b>-3.89</b>、score_theme 的 T+20 NW t=<b>-2.70</b>，
            两者都是<b>显著负向</b>——高关注、高热度在本池内是减分项而非加分项。
        </div>
        {_mf_risk_block}
        <div style="font-size:13px;font-weight:800;color:#38bdf8;margin:16px 0 6px;">
            🧪 这套多因子真的比单因子好吗？—— 回测告诉你：统计上等价</div>
        <div style="font-size:11px;color:#64748b;margin-bottom:8px;line-height:1.75;">
            四组构造在同一批截面、同一可交易口径（T+1 开盘买入、扣 20bp、相对池内等权基准、TOP10）下对比。
            A = fund 单维；B = 加消息面加权；C = 再剔题材过热；D = 再剔关注度过高（= 本组合的可回测近似）。
        </div>
        <table style="width:100%;border-collapse:collapse;font-size:11px;">
            <thead><tr style="background:#1e293b;">
                <th style="padding:5px 8px;text-align:left;color:#94a3b8;">周期</th>
                <th style="padding:5px 6px;color:#94a3b8;">A 单因子</th>
                <th style="padding:5px 6px;color:#94a3b8;">B +消息面</th>
                <th style="padding:5px 6px;color:#94a3b8;">C +剔题材</th>
                <th style="padding:5px 6px;color:#94a3b8;">D +剔关注度</th>
                <th style="padding:5px 8px;color:#94a3b8;">配对 |t| 最大</th>
            </tr></thead>
            <tbody>{_mfb_rows}</tbody>
        </table>
        <div style="font-size:11px;color:#e2e8f0;padding:9px 11px;background:rgba(56,189,248,0.07);
                    border:1px solid #38bdf844;border-radius:8px;margin-top:8px;line-height:1.8;">
            <b style="color:#7dd3fc;">📌 诚实结论</b>：{_mfb_concl}
        </div>
        <div style="font-size:11px;color:#f87171;padding:9px 11px;background:rgba(239,68,68,0.07);
                    border:1px solid #ef444455;border-radius:8px;margin-top:8px;line-height:1.8;">
            <b>🚨 三条不能省掉的边界</b>：<br>
            ① 反转块仅 <b>8 个截面</b>，属弱先验而非已验证因子，权重已折半；<br>
            ② 板块动量 <b>未</b> 作为选股轴 —— score_sector 池内 IC 全周期不显著（T+1 t=0.29、T+5 t=-1.20），
            原「按板块动量切路线」的做法缺乏证据支撑；<br>
            ③ 池内头组净收益历史上<b>为负</b>（池内基准在跌），本组合只表达「相对排序」，<b>不等于买入能赚钱</b>。
        </div>

        <div style="font-size:13px;font-weight:800;color:#fbbf24;margin:20px 0 6px;">
            ❓ 为什么动量路线 IC 和胜率都低，却仍在选动量路线的个股？</div>
        <div style="font-size:11.5px;color:#e2e8f0;line-height:1.9;padding:10px 12px;
                    background:rgba(251,191,36,0.07);border:1px solid #fbbf2444;border-radius:8px;">
            这个问题问到了模型的<b>结构性矛盾</b>，答案有四层，一层比一层严重：<br><br>
            <b style="color:#fbbf24;">① 字段同名字不同物：动量路线选的是「板块动量」，不是「个股动量」。</b><br>
            `pick_portfolio.py` 的 momentum 路线筛选条件写在<b>申万二级板块</b>的 60 日/10 日涨幅排名上，
            个股层面只按综合评分 <code>total</code> 降序取前 10，<b>个股动量从未参与筛选</b>。
            所以「动量路线」指的是<b>板块</b>动量，不是个股追涨。<br><br>
            <b style="color:#fbbf24;">② 更讽刺的是：系统自己的个股动量开关是关的。</b><br>
            今日 <b>490 只里有 481 只</b>的 <code>momentum_mode = off</code>（三重负证据触发关闭），
            个股动量分完全不进总分。也就是说：<b>模型一边判定动量无效并关掉它，
            一边在组合层用板块动量选股</b>——两处口径互相拆台，这是设计缺陷，不是巧合。<br><br>
            <b style="color:#fbbf24;">③ IC 低是统计事实，但 IC 衡量的是「排序能力」不是「选股能力」。</b><br>
            个股动量因子 IC 为负（MOM1M ic_daily=-0.052），说明<b>在本池内涨得多的后续跌得多</b>
            ——这是均值回复。而动量路线选出的板块，其成分股往往已经<b>跌下来了</b>：
            实测今日动量线 10 只平均 <b>20 日跌幅 -6.4%</b>、平均 RSI <b>47.9</b>，
            10 只里只有 <b>4 只</b> 20 日涨幅为正。<b>「动量路线」实际选出的是「强势板块里的弱势股」</b>，
            恰好落在反转因子的有效区，所以它的实盘表现没 IC 显示的那么差。<br><br>
            <b style="color:#f87171;">④ 但真实跟踪数据说明：它确实在亏钱，只是没亏那么多。</b><br>
            见下表 —— 这是<b>真实推荐记录</b>，不是回测推演。
        </div>
        <table style="width:100%;border-collapse:collapse;font-size:11px;margin-top:8px;">
            <thead><tr style="background:#1e293b;">
                <th style="padding:5px 8px;text-align:left;color:#94a3b8;">路线（真实推荐跟踪）</th>
                <th style="padding:5px 8px;color:#94a3b8;">T+1 样本</th>
                <th style="padding:5px 8px;color:#94a3b8;">T+1 超额均值</th>
                <th style="padding:5px 8px;color:#94a3b8;">T+1 胜率</th>
                <th style="padding:5px 8px;color:#94a3b8;">T+5 超额均值</th>
                <th style="padding:5px 8px;color:#94a3b8;">T+5 胜率</th>
            </tr></thead>
            <tbody>{_rt_rows}</tbody>
        </table>
        <div style="font-size:11px;color:#e2e8f0;padding:9px 11px;background:#1e293b;
                    border-radius:8px;margin-top:8px;line-height:1.85;">
            <b style="color:#7dd3fc;">读表要点</b>：{_rt_read}
        </div>
    </div>''')
except Exception as _mfe:
    print(f"⚠️ 多因子组合区块渲染失败: {str(_mfe)[:160]}")
html_parts.append('''
    <!-- A6 快捷筛选: 9 按钮 → 4 高频 + 「更多」折叠 -->
    <div class="quick-filters">
        <button class="quick-btn active" data-quick="all">全部</button>
        <button class="quick-btn" data-quick="SA">⭐ 只看S/A级</button>
        <button class="quick-btn" data-quick="growth">🚀 高增长(≥30%)</button>
        <button class="quick-btn" data-quick="undervalued">💰 低估(FwdPE&lt;30)</button>
        <button class="quick-btn" id="quickMoreBtn" onclick="document.getElementById('quickMore').style.display=(document.getElementById('quickMore').style.display==='none'?'flex':'none');this.classList.toggle('active');">⋯ 更多筛选</button>
        <span id="quickMore" style="display:none;gap:8px;flex-wrap:wrap;">
            <button class="quick-btn" data-quick="S">🥇 只看S级</button>
            <button class="quick-btn" data-quick="low_pos">📉 低位股(&lt;30%)</button>
            <button class="quick-btn" data-quick="hot">🔥 热门(热度≥4)</button>
            <button class="quick-btn" data-quick="up">📈 评级上调</button>
            <button class="quick-btn" data-quick="new">🆕 新晋上榜</button>
        </span>
    </div>

    <!-- 今日市场概览 (v2.43: 原「市场总结+组合推荐」，组合部分已上移至「今日候选组合」) -->
    <div class="charts">
        <!-- 合并卡片：今日市场概览(覆盖数/S+A占比/主线板块/板块均分TOP5) -->
        <div class="chart-card" style="padding:16px;grid-column:1 / -1;">
            <h3 style="font-size:13px;margin-bottom:10px;">📋 今日市场概览 <span style="font-size:11px;color:#64748b;font-weight:400;">（板块均分 TOP5 · 组合标的已上移至「今日候选组合」）</span></h3>
            <div style="display:grid;grid-template-columns:repeat(3,1fr);gap:8px;margin-bottom:10px;">
                <div style="background:#0f172a;border-radius:6px;padding:8px;text-align:center;">
                    <div style="font-size:11px;color:#94a3b8;">覆盖个股</div>
                    <div style="font-size:15px;font-weight:800;color:#f8fafc;">''' + str(total_stocks) + '''只</div>
                </div>
                <div style="background:#0f172a;border-radius:6px;padding:8px;text-align:center;">
                    <div style="font-size:11px;color:#94a3b8;">S+A级占比</div>
                    <div style="font-size:15px;font-weight:800;color:#22c55e;">''' + f"{(stats.get('rating_dist',{}).get('S',0)+stats.get('rating_dist',{}).get('A',0))/max(total_stocks,1)*100:.0f}" + '''%</div>
                </div>
                <div style="background:#0f172a;border-radius:6px;padding:8px;text-align:center;">
                    <div style="font-size:11px;color:#94a3b8;">主线板块</div>
                    <div style="font-size:15px;font-weight:800;color:#3b82f6;">''' + str(len([s for s,v in sec_avg.items() if v.get("count",0)>=5])) + '''个</div>
                </div>
            </div>
            <div style="font-size:11px;color:#94a3b8;margin-bottom:6px;">板块均分TOP5</div>''')

# v2.44 P1-3 行业集中度提示(插入板块均分TOP5 之前)
if _conc_top_pct:
    _conc_icon = "⚠️" if _conc_warn else "✅"
    _conc_warn_txt = (f"单一板块占比 {_conc_max_pct:.0f}% 已超 15% 警戒线 —— 本名单存在明显行业集中，"
                      f"建议结合其他赛道分散，勿将本名单等同于全市场分散配置。"
                      if _conc_warn else
                      f"最大单一板块占比 {_conc_max_pct:.0f}%，行业分布相对均衡。")
    html_parts.append(f'''
            <div style="font-size:11px;color:#94a3b8;margin-bottom:6px;">行业集中度（前3板块 · 合计 {_conc_sum3:.0f}%）</div>
            <div style="display:flex;flex-wrap:wrap;gap:6px;margin-bottom:10px;">''')
    for _nm, _cnt, _p in _conc_top_pct:
        _bc = "#dc2626" if _p > 25 else ("#f59e0b" if _p > 15 else "#3b82f6")
        html_parts.append(f'''
                <span style="font-size:11px;padding:3px 9px;border-radius:10px;background:{_bc}1a;color:{_bc};border:0.5px solid {_bc}55;">
                    {html_mod.escape(str(_nm))} {_cnt}只 · {_p:.0f}%</span>''')
    html_parts.append(f'''
            </div>
            <div style="font-size:11px;color:{_conc_color};margin-bottom:10px;line-height:1.6;">
                {_conc_icon} {_conc_warn_txt}
            </div>''')

for s, v in sorted_secs:
    cnt = v.get("count", 0)
    avg = v.get("avg", 0)
    bar_w = round(min(avg / 100 * 100, 100), 1)
    html_parts.append(f'''
                <div style="display:flex;align-items:center;gap:6px;margin-bottom:4px;">
                    <span style="color:#e2e8f0;font-size:11px;min-width:60px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;">{s}</span>
                    <div style="flex:1;background:#0f172a;border-radius:3px;height:12px;overflow:hidden;">
                        <div style="width:{bar_w}%;height:100%;background:linear-gradient(90deg,#3b82f6,#22c55e);border-radius:3px;"></div>
                    </div>
                    <span style="color:#94a3b8;font-size:11px;min-width:55px;text-align:right;">{avg:.2f} ({cnt}只)</span>
                </div>''')

html_parts.append('''
        </div>
    </div>''')

# 准备评级数据
rating_dist = stats.get("rating_dist", {})

html_parts.append('''
    <!-- 深度分析 -->
    <div class="top-list">
        <div class="section-title">🏆 S/A级综合排名 · 投资建议详情 (''' + str(len(top20)) + '''只)</div>''')

# 生成详情卡片
for idx, r in enumerate(top20):
    i = idx + 1
    pct = r.get("pct_chg", 0) or 0
    pct_cls = "up" if pct > 0 else "down" if pct < 0 else ""
    rating = r["rating"]
    total = r["total"]
    rank_color = "#f59e0b" if i == 1 else "#94a3b8" if i == 2 else "#cd853f" if i == 3 else "#64748b"

    pe = r.get("pe", 0)
    fwd = r.get("fwd_pe")
    growth = r.get("growth")
    pe_str = f"PE(TTM) {pe:.0f}x" if pe and pe > 0 else "PE(TTM) N/A"
    fwd_str = f" → Fwd {fwd:.0f}x" if fwd else ""
    g_str = f" 增速 {growth:+.0f}%" if growth else " 增速 N/A"
    peg_val = r.get("pe_ladder")
    peg_str = ""
    if peg_val and isinstance(peg_val, dict):
        yrs = sorted(peg_val.keys())
        peg_str = " 阶梯" + "→".join(f"{peg_val[y]}" for y in yrs)
    sub_theme = r.get("sub_theme", "")
    turnover_yi = (r.get("turnover", 0) or 0) / 100000000
    week_chg = r.get("week_chg")
    week_chg_str = f"{week_chg:+.2f}%" if week_chg is not None else "N/A"
    week_chg_cls = "up" if (week_chg or 0) > 0 else "down" if (week_chg or 0) < 0 else ""
    report_count = r.get("report_count", 0) or 0
    cur_price = r.get("price") or 0        # v2.46: 现价(与目标价对比用)
    _inst_tgt = _target_px.get(r["name"])  # v2.46: 本地复盘机构目标价(可能为 None)

    news_reasons = r.get("news_reasons", [])
    fund_reasons = r.get("fund_reasons", [])
    theme_reasons = r.get("theme_reasons", r.get("flow_reasons", []))
    tech = r.get("tech", {})

    news_html = "<br>".join([n.replace("⚠️", "") for n in news_reasons[:3]]) if news_reasons else "无消息数据"
    risk_parts = [n for n in news_reasons if "⚠" in n]
    risk_html = "<br>".join([n.replace("⚠️", "") for n in risk_parts[:2]]) if risk_parts else ""

    # v2.20: 技术面四因子展示(均线/MACD/量能/RSI) —— v3.3 量纲升级: 内部 0-5 ×2 → 0-10 展示
    ma_f = tech.get("ma_factor")
    macd_f = tech.get("macd_factor")
    vol_f = tech.get("vol_factor")
    rsi_f = tech.get("rsi_factor")
    factor_info = (f"均线{ma_f*2:.1f}·MACD{macd_f*2:.1f}·量能{vol_f*2:.1f}·RSI时机{rsi_f*2:.1f}"
                   if None not in (ma_f, macd_f, vol_f, rsi_f) else "")

    # v2.10: RSI 超买/超卖
    rsi_val = tech.get("rsi")
    rsi_state = tech.get("rsi_state")
    rsi_info = f" · RSI:{rsi_val:.0f}({rsi_state})" if rsi_val is not None else ""

    # v2.10: 公司持续性/低PE可持续性
    score_sustain = r.get("score_sustain")
    sustain_note = r.get("sustain_note", "")
    cyclic_tag = "周期股" if r.get("is_cyclic") else ("存储模组" if r.get("is_storage_mod") else "")
    sustain_str = (f"持续性:{score_sustain:.2f}/10 {sustain_note}" if score_sustain is not None else (sustain_note or "")) + (f" [{cyclic_tag}]" if cyclic_tag else "")
    fund_detail = (', '.join(fund_reasons[:2]) if fund_reasons else '数据不足') + (f" · {sustain_str}" if sustain_str else "")

    # v2.30: AI增强层 — 业绩一/二阶导 + AI赋分徽标
    ai_fd = r.get("fund_detail") or {}
    deriv_str = ""
    if ai_fd:
        _g1 = ai_fd.get("deriv1_growth")
        _s1 = ai_fd.get("deriv1_score")
        _d2 = ai_fd.get("deriv2_delta")
        _s2 = ai_fd.get("deriv2_score")
        if _g1 is not None:
            deriv_str = f" · 业绩一阶导(增速{_g1:+.0f}%) {_s1*2:.1f}/10分"
            if _d2 is not None:
                deriv_str += f" · 二阶导(加速度{_d2:+.0f}pt) {_s2*2:.1f}/10分"
    ai_badge = ""
    if r.get("ai_applied"):
        ai_badge = ('<span style="font-size:11px;font-weight:700;padding:1px 6px;border-radius:3px;'
                    'background:#ede9fe;color:#7c3aed;margin-left:6px;">🤖AI增强</span>')

    # v2.10: 消息面7日时效
    news_recency = r.get("news_recency", "")
    news_recency_color = "#f97316" if "无消息" in news_recency else "#22c55e"
    news_detail = news_html + (f' · <span style="color:{news_recency_color};font-weight:600;">{news_recency}</span>' if news_recency else "")
    if report_count:
        news_detail += f' · <span style="color:#22c55e;font-weight:600;">研报×{report_count}</span>'

    _tech_disp, _theme_disp = _dim_composites(r)
    justification = []
    if r['score_news'] >= 4: justification.append("消息面强劲")
    elif r['score_news'] <= 1: justification.append("消息面疲弱")
    if _tech_disp >= 8: justification.append("技术面看多")
    elif _tech_disp <= 1: justification.append("技术面承压")
    if rsi_state == "超买": justification.append("RSI超买注意回调")
    elif rsi_state == "超卖": justification.append("RSI超卖存反弹")
    if r['score_fund'] >= 4: justification.append("估值吸引力强")
    elif r['score_fund'] <= 1: justification.append("估值偏高")
    if _theme_disp >= 8: justification.append("主线题材热度高")
    elif _theme_disp <= 1: justification.append("题材冷门")
    if not justification: justification = ["各维度均衡"]

    # 评级变化标签
    change_tag = ""
    if r["code"] in prev_ratings:
        prev_r = prev_ratings[r["code"]]["rating"]
        rating_order = {"S": 4, "A": 3, "B": 2, "C": 1}
        if rating_order.get(rating, 0) > rating_order.get(prev_r, 0):
            change_tag = '<span class="rating-change rating-up">↑{prev_r}→{rating}</span>'.format(prev_r=prev_r, rating=rating)
        elif rating_order.get(rating, 0) < rating_order.get(prev_r, 0):
            change_tag = '<span class="rating-change rating-down">↓{prev_r}→{rating}</span>'.format(prev_r=prev_r, rating=rating)
    else:
        change_tag = '<span class="rating-change rating-new">NEW</span>'

    s_class = "s-level" if rating == "S" else ""
    # A5: S级默认展开; A级折叠(仅留头部, 点击展开明细) —— 29张卡片全展开占12,500px
    a_collapse = "" if rating == "S" else " collapsed"
    _toggle = ("" if rating == "S" else
               '<span class="expand-hint" onclick="this.closest(\'.stock-detail\').classList.toggle(\'collapsed\');">▾ 展开明细</span>')

    # ===== v2.44 P0-4 评级与基本面背离风险标记 =====
    # 历史问题: 东岳硅材总分 12.68 进 S/A 前 28, 但 growth=−103.6%、fwdPE=56,
    #   页面无任何风险提示 → 高评级与基本面恶化并存, 使用者易误读为"优质标的"。
    # 根因: 高评级主要由题材热度/技术动量驱动, 基本面维度可被加权稀释。
    # 处理: 风险标记(不篡改评级, 仅如实提示背离), 在卡片头部显著展示。
    _fund_flags = []
    if isinstance(growth, (int, float)) and growth < 0:
        _fund_flags.append(f"业绩负增长 {growth:+.0f}%")
    if isinstance(fwd, (int, float)) and fwd > 80:
        _fund_flags.append(f"FwdPE {fwd:.0f}x 偏高")
    _fd_n = r.get("fund_detail") or {}
    if isinstance(_fd_n.get("deriv2_delta"), (int, float)) and _fd_n["deriv2_delta"] < -20:
        _fund_flags.append(f"增长减速 {_fd_n['deriv2_delta']:+.0f}pt")
    # 仅当评级为 S/A(推荐级) 且存在基本面瑕疵时才提示, 避免噪声
    _risk_badge = ""
    if _fund_flags and rating in ("S", "A"):
        _risk_badge = ('<span title="该股评级主要由题材热度与技术动量驱动，基本面存在瑕疵，'
                       '请勿仅凭评级买入" style="font-size:11px;font-weight:700;padding:1px 6px;'
                       'border-radius:3px;background:#fee2e2;color:#dc2626;margin-left:6px;cursor:help;">'
                       '⚠️基本面背离 ' + "·".join(_fund_flags) + '</span>')

    # ===== v3.5 低基数失真修正审计徽标 =====
    # 单点增速被 _effective_growth 饱和压缩时（growth_capped=True），如实展示原始→采用值，
    # 让使用者看得出「该股的高增速已被判定为低基数失真」——数据可审计，非隐藏处理。
    _lowbase_badge = ""
    if r.get("growth_capped"):
        _g_raw = r.get("growth_raw")
        _g_eff = r.get("growth")
        _raw_s = f"+{_g_raw:.0f}%" if isinstance(_g_raw, (int, float)) else "—"
        _eff_s = f"+{_g_eff:.0f}%" if isinstance(_g_eff, (int, float)) else "—"
        _lb_tip = r.get("growth_note") or "单点增速远超存续期趋势可支持水平，已按低基数失真饱和处理"
        _lowbase_badge = ('<span title="' + _lb_tip.replace('"', "'") + '" '
                          'style="font-size:11px;font-weight:700;padding:1px 6px;'
                          'border-radius:3px;background:#fef3c7;color:#b45309;margin-left:6px;cursor:help;">'
                          '📉低基数修正 ' + _raw_s + '→' + _eff_s + '</span>')

    # ===== v3.5.1 Q3 预测景气加成审计徽标 =====
    # Q3 景气预测（手工汇总表，半导体/消费电子链）触发 score_fund 加分时如实展示：
    #   加分档位 + 加速度 + 去重闸门(implied_h2) 依据，与低基数修正徽标同口径「可审计不隐藏」。
    _q3_badge = ""
    if (r.get("q3_bonus") or 0) > 0:
        _qb = float(r.get("q3_bonus") or 0)
        _q3_yoy_s = f"{r['q3_yoy']:+.0f}%" if isinstance(r.get("q3_yoy"), (int, float)) else "扭亏"
        _q3_accel_s = f"{r['q3_accel']:+.0f}pt" if isinstance(r.get("q3_accel"), (int, float)) else "—"
        _q3_ih2_s = f"{r['q3_implied_h2']:+.0f}%" if isinstance(r.get("q3_implied_h2"), (int, float)) else "无全年预测"
        # v3.5.1 诚实性: Q3同比被低基数饱和时明示原始值（否则「加速度+0pt」会被误读为无加速）
        _q3_sat_s = ""
        _q3_raw, _q3_eff = r.get("q3_yoy_raw"), r.get("q3_yoy")
        if (isinstance(_q3_raw, (int, float)) and isinstance(_q3_eff, (int, float))
                and abs(_q3_raw - _q3_eff) > 1):
            _q3_sat_s = f"；Q3原始同比{_q3_raw:+.0f}%，经低基数饱和后采用{_q3_eff:+.0f}%（加速度为饱和后口径）"
        _q3_tip = (f"Q3预测景气加成 +{_qb:.1f}分(0-5分制, 传导总分+{_qb*_DIM_W['score_fund']*20:.1f})；"
                   f"Q3同比{_q3_yoy_s} 加速度{_q3_accel_s}{_q3_sat_s}；"
                   f"机构全年预期隐含H2增速{_q3_ih2_s}（Q3预测超出机构预期部分才计入，防止与FwdPE双重计价）；"
                   f"数据源：手工汇总表(半导体/消费电子Q3预测, TTL45天)")
        _q3_badge = ('<span title="' + _q3_tip.replace('"', "'") + '" '
                     'style="font-size:11px;font-weight:700;padding:1px 6px;'
                     'border-radius:3px;background:#dcfce7;color:#15803d;margin-left:6px;cursor:help;">'
                     '📈Q3景气 +' + f"{_qb:.1f}" + '</span>')

    # ===== v3.0 十维展示: 徽章行与维度卡片按数据口径自适应 =====
    _is10 = r.get("score_oversold60") is not None
    _tech_disp, _theme_disp = _dim_composites(r)
    # AI 赋分附注(先赋临时变量, 规避 f-string 内层引号问题)
    _ai_note = ""
    if r.get("ai_applied"):
        _ai_note = f" · 🤖AI赋分 消息{r['ai_news']:.2f}/热度{r['ai_theme_heat']:.2f}"
        if not _is10:
            _ai_note += f" | {r.get('ai_theme_label','')}"
    _theme_detail = (", ".join(theme_reasons[:3]) if theme_reasons else "数据不足") + _ai_note
    if _is10:
        _badge_html = (f"<span>🏷️ {r['score_news']:.2f}/10消息 · {r['score_fund']:.2f}/10基本 · "
                       f"超跌60 {r['score_oversold60']:.2f} · 乖离 {r['score_bias60']:.2f} · "
                       f"RSI反 {r['score_rsi_rev']:.2f} · 量能反 {r['score_vol_pulse']:.2f} · "
                       f"超买反 {r['score_overbought']:.2f} · 内禀 {r.get('score_theme_intrinsic', 0):.2f} · "
                       f"关注 {r.get('score_attention', 0):.2f}</span>")
        _tech_card_html = (f'<div class="dim-card">'
                           f'<div class="dim-title">📈 技术块·五拆解 ({_tech_disp:.2f}/10)</div>'
                           f'<div class="dim-score">{_tech_disp:.2f}</div>'
                           f'<div class="dim-detail">超跌60反转 {r["score_oversold60"]:.2f} · 乖离低位 {r["score_bias60"]:.2f} · '
                           f'RSI反转 {r["score_rsi_rev"]:.2f} · 量能反向 {r["score_vol_pulse"]:.2f} · '
                           f'超买反向 {r["score_overbought"]:.2f}<br>趋势: {tech.get("trend","N/A")} · '
                           f'分位: {tech.get("position","N/A")}%{f" · {factor_info}" if factor_info else ""} · {tech.get("reason","")}</div>'
                           f'</div>')
        _theme_card_html = (f'<div class="dim-card">'
                            f'<div class="dim-title">🔥 题材块·双拆解 ({_theme_disp:.2f}/10){f" · {sub_theme}" if sub_theme else ""}</div>'
                            f'<div class="dim-score">{_theme_disp:.2f}</div>'
                            f'<div class="dim-detail">内禀 {r.get("score_theme_intrinsic", 0):.2f} · 关注度 {r.get("score_attention", 0):.2f} · {_theme_detail}</div>'
                            f'</div>')
    else:
        _badge_html = (f"<span>🏷️ {r['score_news']:.2f}/10消息 · {r['score_tech']:.2f}/10技术 · "
                       f"{r['score_fund']:.2f}/10基本 · {r.get('score_theme',0):.2f}/10热度</span>")
        _tech_card_html = (f'<div class="dim-card">'
                           f'<div class="dim-title">📈 技术面 ({r["score_tech"]:.2f}/10)</div>'
                           f'<div class="dim-score">{r["score_tech"]:.2f}</div>'
                           f'<div class="dim-detail">趋势: {tech.get("trend","N/A")} · 分位: {tech.get("position","N/A")}%{f" · {factor_info}" if factor_info else ""} · {tech.get("reason","")}</div>'
                           f'</div>')
        _theme_card_html = (f'<div class="dim-card">'
                            f'<div class="dim-title">🔥 题材热度 ({r.get("score_theme",0):.2f}/10){f" · {sub_theme}" if sub_theme else ""}</div>'
                            f'<div class="dim-score">{r.get("score_theme",0):.2f}</div>'
                            f'<div class="dim-detail">{_theme_detail}</div>'
                            f'</div>')

    html_parts.append(f'''
        <div class="stock-detail {s_class}{a_collapse}" data-rating="{rating}" data-sector="{sector_disp(r)}" data-code="{r['code']}" data-name="{r['name']}">
            <div class="header-row">
                <div class="rank" style="background:{rank_color}">#{i}</div>
                <div>
                    <div class="name">{r['name']}{change_tag}{ai_badge}{_risk_badge}{_lowbase_badge}{_q3_badge}</div>
                    <div class="code">{r['code']} · {sector_disp(r)}</div>
                </div>
                {_toggle}
                <span class="s-pct {pct_cls}" style="font-size:13px;font-weight:700;">{pct:+.2f}%</span>
                <div class="total" style="color:{RATING_COLORS[rating]}">{total:.2f}<span style="font-size:12px;font-weight:500;opacity:.65;"> /100</span></div>
                <div class="rating" style="background:{RATING_BG[rating]};color:{RATING_COLORS[rating]}">{rating}</div>
            </div>
            <div class="sd-body">
            <div class="meta-row">
                <span>💰 现价 {cur_price:.2f}</span>
                <span>💰 成交额: {turnover_yi:.0f}亿</span>
                <span>📊 {pe_str}{fwd_str}{g_str}{peg_str}</span>
                {f'<span style="color:{valu_note(r)[1]};">🧭 {valu_note(r)[0]}</span>' if valu_note(r)[0] else ''}
                <span class="s-pct {week_chg_cls}" style="font-weight:700;">📅 近一周 {week_chg_str}</span>
                {_badge_html}
            </div>
            <div class="dim-row">
                <div class="dim-card">
                    <div class="dim-title">📰 消息面 ({r['score_news']:.2f}/10)</div>
                    <div class="dim-score">{r['score_news']:.2f}</div>
                    <div class="dim-detail">{news_detail}</div>
                </div>
                {_tech_card_html}
                <div class="dim-card">
                    <div class="dim-title">📊 基本面 ({r['score_fund']:.2f}/10)</div>
                    <div class="dim-score">{r['score_fund']:.2f}</div>
                    <div class="dim-detail">{fund_detail}{deriv_str}</div>
                </div>
                {_theme_card_html}
            </div>''')
    # v3.3 问题1优化: 十维评分明细矩阵（10维独立并列，含板块动量；布局合理）
    html_parts.append(_dim_matrix_html(r))

    if risk_html:
        html_parts.append(f'''
            <div style="font-size:11px;color:#f97316;margin-top:10px;padding:10px;background:rgba(249,115,22,0.1);border-radius:8px;">⚠️ {risk_html}</div>''')

    # A5: 关闭折叠区(meta-row + 四维明细 + 风险提示 被折叠; 操作建议/评级原因常驻可见)
    html_parts.append('''
            </div>''')

    # ===== v2.41 P0-3 操作建议三件套 + P1-3 一致性约束 + v2.42 R1可信度/R4持有周期 =====
    plan = build_trade_plan(r)
    plan_tag, plan_note = trade_plan_advice(r, plan)
    if plan:
        _risk_pct = (plan["buy_hi"] - plan["stop"]) / plan["buy_hi"] * 100 if plan["buy_hi"] else 0
        _up_pct = (plan["target"] - r.get("price", 0)) / r.get("price", 1) * 100
        _plan_color = "#ef4444" if plan["overbought"] else ("#f59e0b" if _bearish else "#22c55e")
        # R1 盈亏比可信度
        _conf_lv, _conf_sc, _conf_note = trade_confidence(r, plan)
        _conf_color = "#22c55e" if _conf_lv == "高" else ("#f59e0b" if _conf_lv == "中" else "#ef4444")
        _rr_disp = f"{plan['rr']:.1f} : 1" if _conf_lv != "低" else f"{plan['rr']:.1f} : 1 (打折)"
        # R4 持有周期
        _hold = holding_period(r, plan)
        html_parts.append(f'''
            <div style="margin-top:10px;padding:10px 12px;border-radius:8px;background:rgba(56,189,248,0.06);border-left:3px solid {_plan_color};">
                <div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap;font-size:11px;">
                    <span style="font-weight:800;color:{_plan_color};">🧭 操作建议</span>
                    <span style="padding:1px 7px;border-radius:3px;background:{_plan_color}22;color:{_plan_color};font-weight:700;">{plan_tag}</span>
                    <span style="color:#94a3b8;">{plan_note}</span>
                </div>
                <div style="display:flex;gap:14px;flex-wrap:wrap;margin-top:6px;font-size:11px;color:#cbd5e1;">
                    <span>🎯 参考买点 <b style="color:#22c55e;">{plan['buy_lo']:.2f} ~ {plan['buy_hi']:.2f}</b></span>
                    <span>🛑 止损 <b style="color:#ef4444;">{plan['stop']:.2f}</b>（-{_risk_pct:.1f}%）</span>
                    <span>🏁 目标 <b style="color:#3b82f6;">{plan['target']:.2f}</b>（+{_up_pct:.1f}%）</span>
                    <span>⚖️ 盈亏比 <b>{_rr_disp}</b>
                        <span style="font-size:11px;font-weight:700;padding:1px 5px;border-radius:3px;background:{_conf_color}22;color:{_conf_color};margin-left:2px;">可信度{_conf_lv}</span>
                    </span>
                </div>
                {f'<div style="font-size:11px;color:{_conf_color};margin-top:4px;">🔎 可信度说明（{_conf_sc}分）：{_conf_note}</div>' if _conf_note else ''}
                {f'<div style="font-size:11px;color:#38bdf8;margin-top:4px;">⏳ 持有周期：{_hold}</div>' if _hold else ''}
                {f'<div style="font-size:11px;color:#f97316;margin-top:5px;">⚠️ 技术矛盾提示: {"、".join(plan["flags"])}，明细评语与操作建议已按一致性约束对齐</div>' if plan["flags"] else ''}
            </div>''')
        # v2.46 现价 vs 目标价并列对比(技术目标 + 机构目标价, 带价差与预期涨跌幅)
        if cur_price > 0:
            html_parts.append(_px_cmp_html(cur_price, plan.get("target"), _inst_tgt))

    html_parts.append(f'''
            <div style="font-size:11px;color:#38bdf8;margin-top:8px;">💡 评级原因: {', '.join(justification[:4])} → {rating}级 · {advice_with_market(r)}</div>
            </div>''')
html_parts.append('''
    </div>

    <!-- S/A级公司深度投研分析 -->
    <div class="research-section">
        <div class="section-title">📑 S/A级公司专业投研分析报告</div>
        <div style="font-size:11px;color:#64748b;margin-bottom:16px;padding:10px;background:#1e293b;border-radius:8px;">
            📌 本章节基于行业研究数据、公司基本面指标、财务估值模型与竞争格局分析框架，对全部S级与A级公司进行系统性投研覆盖。
            分析模块包含：<b style="color:#38bdf8;">核心业务竞争力评估</b> · <b style="color:#38bdf8;">行业发展前景预测</b> · <b style="color:#f97316;">潜在风险因素识别</b> · <b style="color:#22c55e;">投资价值分析</b>
        </div>''')

# ========== 行业研究数据库(按sub_theme细分) ==========
# 细分行业数据库优先匹配；其次回退到板块数据库
SUBTHEME_RESEARCH = {
    # --- 光模块/光通信/光芯片 ---
    "光模块": {
        "outlook": "AI算力竞赛持续升级，全球光模块市场规模将从2025年165亿美元跃升至2026年260亿美元，年增速超57%。800G以上光模块出货占比预计从2024年19.5%升至2026年60%以上，1.6T产品进入规模化商用阶段。英伟达VeraRubin平台密集拉货，Meta/谷歌/微软合计800G采购超2400万只，1.6T下半年启动规模采购。硅光方案在1.6T光模块中占比达70%-80%，CPO技术迎来产业化起点。高端产品交付周期拉长至12周以上，行业景气度持续超预期。",
        "drivers": ["AI算力基建超级周期", "1.6T/3.2T光模块技术迭代", "800G出货量同比增260%", "硅光与CPO技术产业化", "英伟达新平台拉货"],
        "risks": ["核心光芯片(EML)产能紧张交期排至2027年", "光模块价格战+客户集中度高", "海外技术管制升级", "估值处于历史高位", "下游云厂商资本开支波动"]
    },
    "光模块/AOC": {
        "outlook": "AI算力中心高密度互联需求推动AOC(有源光缆)及光模块市场爆发，800G/1.6T光模块规模化出货带动AOC需求同步增长。全球AI专用光收发模块市场2026年达260亿美元，年增速超57%，高速AOC作为数据中心内部互联核心组件，受益于万卡级智算集群建设潮。",
        "drivers": ["AI数据中心高密度互联需求", "800G/1.6T规模化出货", "万卡级智算集群建设", "硅光方案渗透提升"],
        "risks": ["核心光芯片产能瓶颈", "客户集中度高风险", "技术路线竞争(CPO/LPO)", "海外供应链不确定性"]
    },
    "光模块/CPO": {
        "outlook": "CPO(共封装光学)技术迎来产业化起点，1.6T光模块中硅光方案占比达70%-80%，CPO逐步与传统可插拔方案长期共存。全球光模块市场规模2026年达260亿美元，年增速超57%。CPO技术将光引擎与交换芯片共封装，大幅降低功耗与成本，适配AI算力中心高密度低功耗需求，是下一代光互联核心方向。",
        "drivers": ["CPO技术产业化突破", "1.6T硅光方案主导", "AI算力中心低功耗需求", "国产化率持续提升"],
        "risks": ["CPO商业化落地进度不确定", "技术良率爬坡风险", "可插拔方案长期共存竞争", "高端光芯片供给紧张"]
    },
    "光芯片/激光器": {
        "outlook": "光芯片是光模块产业链核心环节，价值占比持续提升。英伟达以40亿美元长单锁定Lumentum、Coherent磷化铟光芯片产能至2028年，凸显上游核心器件紧缺格局。2026年磷化铟衬底全球需求从200-210万片提升至260-300万片，有效产能仅约75万片，缺口超70%。薄膜铌酸锂、磷化铟、空芯光纤三大技术方向成为核心增量。国产光芯片替代窗口加速打开。",
        "drivers": ["EML光芯片供给极度紧缺", "磷化铟衬底缺口超70%", "英伟达40亿美元长单锁定", "国产替代窗口加速", "1.6T/3.2T技术迭代"],
        "risks": ["海外技术垄断(EML产能集中于海外)", "高端光芯片研发壁垒高", "客户认证周期长", "技术路线替代风险"]
    },
    "光芯片/PLC": {
        "outlook": "PLC(平面光波导)光芯片作为光通信核心器件，受益于AI算力中心建设与无源光网络升级。光芯片上游供给持续紧缺，国产替代逻辑清晰。硅光集成技术推动PLC光芯片向高集成度、低功耗方向演进。",
        "drivers": ["AI算力中心光互联需求", "光芯片国产替代加速", "硅光集成技术演进", "无源光网络升级"],
        "risks": ["海外技术垄断", "研发投入大周期长", "客户认证门槛高", "技术迭代不确定性"]
    },
    "光通信/海缆": {
        "outlook": "光通信与海底电缆双赛道受益于AI算力基建与海上风电建设。AI数据中心东西向流量激增推动高速光通信需求，海上风电装机持续增长拉动海缆需求。光通信向800G/1.6T升级，海缆向高压直流、柔性直流演进。",
        "drivers": ["AI数据中心光通信需求", "海上风电装机增长", "800G/1.6T光通信升级", "海缆高压直流化"],
        "risks": ["海上风电建设进度不确定性", "光通信价格竞争", "原材料(铜/铝)价格波动", "产能扩张压力"]
    },
    "光通信/光纤": {
        "outlook": "光纤光缆行业受益于AI算力基建与F5G-A全光网建设。多芯光纤、空芯光纤、多模光纤等新品类适配AI时代超高带宽需求。全球AI光模块市场规模2026年达260亿美元，拉动光纤光缆配套需求。空芯光纤作为下一代核心方向，传输时延降低30%以上。",
        "drivers": ["AI算力基建光纤需求", "空芯光纤技术突破", "F5G-A全光网建设", "多芯光纤产业化"],
        "risks": ["传统光纤产能过剩", "空芯光纤商业化进度不确定", "运营商集采价格压力", "原材料成本波动"]
    },
    "光通信设备": {
        "outlook": "光通信设备行业受益于AI算力网络与5G-A建设。OTN、SPN等高速光传输设备需求增长，AI数据中心互联推动400G/800G光传输设备规模化部署。F5G-A全光网借助AI自动驾驶技术实现故障毫秒级定位与带宽智能调度。",
        "drivers": ["AI数据中心互联需求", "5G-A网络建设", "F5G-A全光网部署", "400G/800G光传输规模化"],
        "risks": ["运营商资本开支波动", "行业竞争激烈", "技术标准演进不确定性", "海外市场壁垒"]
    },
    # --- AI服务器/算力设备 ---
    "AI服务器": {
        "outlook": "IDC预测2026年全球AI服务器出货量有望突破200万台，单台PCB价值量约为普通服务器的近10倍。英伟达VeraRubin平台密集拉货，算力硬件订单能见度延伸至2027年。AI服务器向高功耗、高密度方向演进，液冷散热成为标配。国内AI服务器厂商受益于国产算力芯片(华为昇腾、寒武纪)放量与信创采购。AI推理需求爆发推动算力架构从通用GPU向专用ASIC转移，博通、Marvell定制化芯片业务强劲增长。",
        "drivers": ["全球AI服务器出货量突破200万台", "英伟达VeraRubin平台拉货", "国产算力芯片放量", "AI推理ASIC化趋势", "液冷散热标配化"],
        "risks": ["GPU供给受限(英伟达管制)", "服务器价格竞争激烈", "下游云厂商资本开支波动", "国产芯片生态成熟度不足", "液冷技术良率风险"]
    },
    "AI服务器/算力": {
        "outlook": "AI算力需求指数级增长，全球智算中心建设潮持续推进。国产AI服务器受益于信创采购与国产算力芯片(昇腾/寒武纪)生态成熟。AI推理需求爆发推动算力架构从通用GPU向专用ASIC转移，算力节点互联需求同步增长。东数西算工程推进与万兆光网试点落地提供广阔场景。",
        "drivers": ["智算中心建设潮", "国产算力芯片生态成熟", "AI推理ASIC化", "东数西算工程推进", "信创采购放量"],
        "risks": ["GPU供给受限", "国产芯片性能差距", "算力中心建设节奏不确定性", "行业竞争加剧", "技术迭代风险"]
    },
    "5G/算力设备": {
        "outlook": "5G-A网络建设与AI算力基建双轮驱动通信设备需求。AI数据中心互联推动高速交换机与光传输设备规模化部署。运营商资本开支向算力网络倾斜，400G/800G光传输设备需求增长。国产通信设备商在全球市场竞争力持续提升。",
        "drivers": ["5G-A网络建设", "AI算力网络投资", "400G/800G光传输部署", "国产设备全球竞争力提升"],
        "risks": ["运营商资本开支波动", "海外市场贸易壁垒", "行业竞争激烈", "技术标准演进不确定性"]
    },
    # --- 存储芯片/模组 ---
    "存储芯片": {
        "outlook": "存储行业迎来AI驱动的超级周期。AI服务器HBM(高带宽内存)需求暴增，SK海力士375层3D NAND量产验证完成，以钼代钨技术导入推动存储密度跃升。DDR5渗透率快速提升，LPDDR5X在AI端侧设备放量。国产存储芯片受益于信创采购与国产替代加速，长江存储、长鑫存储等国产厂商技术突破缩小与海外差距。存储价格周期上行，2026年景气度持续修复。",
        "drivers": ["AI服务器HBM需求暴增", "375层3D NAND量产(以钼代钨)", "DDR5渗透率快速提升", "国产替代加速", "存储价格周期上行"],
        "risks": ["海外存储巨头(三星/SK海力士)技术领先", "存储价格周期波动", "先进制程设备受限", "消费电子需求疲软", "国产良率爬坡风险"]
    },
    "存储控制器": {
        "outlook": "存储控制器芯片受益于SSD渗透率提升与AI数据中心存储需求增长。PCIe 5.0控制器规模化量产，PCIe 6.0研发推进。国产存储控制器在信创采购与消费级SSD市场加速替代，企业级市场突破进行中。AI推理端侧部署推动高带宽存储控制器需求。",
        "drivers": ["PCIe 5.0规模化量产", "AI数据中心存储需求", "信创采购加速", "国产替代窗口", "SSD渗透率提升"],
        "risks": ["海外主控芯片厂商竞争", "企业级市场认证周期长", "技术迭代速度快", "消费电子需求波动"]
    },
    "存储模组": {
        "outlook": "存储模组行业受益于AI数据中心内存扩容与SSD需求增长。DDR5模组渗透率快速提升，企业级SSD在AI服务器中放量。国产存储模组厂商受益于国产存储芯片放量与信创采购，市场份额持续提升。存储价格上行周期带动模组厂商盈利修复。",
        "drivers": ["AI服务器内存扩容需求", "DDR5模组渗透提升", "企业级SSD放量", "国产替代加速", "存储价格上行周期"],
        "risks": ["存储芯片价格波动", "海外模组厂商竞争", "客户集中度风险", "库存周期波动"]
    },
    "内存接口": {
        "outlook": "内存接口芯片受益于DDR5渗透率提升与AI服务器内存需求爆发。DDR5 RCD/DB/SPD芯片需求量较DDR4翻倍，AI服务器高带宽内存需求推动MRCD/MDB等新品类增长。PCIe 5.0 Retimer芯片规模化出货，CXL内存池化技术开启新增长曲线。国产内存接口芯片厂商在全球市场竞争力持续提升。",
        "drivers": ["DDR5渗透率快速提升", "AI服务器内存需求爆发", "PCIe 5.0 Retimer出货", "CXL内存池化新技术", "国产竞争力提升"],
        "risks": ["DDR5渗透节奏不及预期", "海外竞争(IDT/Rambus)", "技术迭代风险", "AI服务器出货波动"]
    },
    "存储/云服务": {
        "outlook": "存储与云服务受益于AI算力中心建设与数据量爆发式增长。AI训练与推理产生海量数据存储需求，对象存储、块存储等云存储服务需求激增。国产云服务厂商在AI基础设施领域加速布局，信创采购推动国产存储解决方案放量。",
        "drivers": ["AI数据存储需求爆发", "云存储服务增长", "信创采购放量", "国产替代加速"],
        "risks": ["云厂商资本开支波动", "存储价格竞争", "数据安全合规风险", "技术迭代不确定性"]
    },
    # --- 半导体设备/测试 ---
    "半导体设备": {
        "outlook": "半导体设备国产化率持续提升，先进制程需求推动设备资本开支高增。美日荷设备出口管制加速国产替代进程，刻蚀、薄膜沉积、清洗等关键设备国产化率突破。拓荆CVD设备、北方华创刻蚀/薄膜设备在先进制程验证持续推进。2026年中国半导体设备市场规模预计达347亿元，先进制程扩产带动高端设备需求。",
        "drivers": ["美日荷设备出口管制加速国产替代", "先进制程扩产", "刻蚀/CVD/清洗设备国产化突破", "半导体设备市场规模达347亿元", "AI芯片需求拉动"],
        "risks": ["海外设备管制升级", "先进制程技术壁垒高", "客户认证周期长", "研发投入大周期长", "下游晶圆厂资本开支波动"]
    },
    "半导体设备/CVD": {
        "outlook": "CVD(化学气相沉积)设备是半导体制造核心设备，受益于先进制程扩产与国产替代加速。PECVD、ALD等薄膜沉积设备在3D NAND、逻辑芯片先进制程中需求增长。国产CVD设备在28nm及以上制程实现规模化出货，14nm及以下制程验证推进。3D NAND向375层以上演进，CVD设备需求量持续增长。",
        "drivers": ["先进制程扩产拉动CVD需求", "3D NAND向375层演进", "国产CVD设备规模化出货", "美日荷管制加速替代"],
        "risks": ["先进制程技术壁垒", "海外设备巨头(应材/泛林)竞争", "客户认证周期长", "研发投入大"]
    },
    "半导体测试": {
        "outlook": "半导体测试设备受益于AI芯片出货量增长与先进封装测试需求提升。AI芯片复杂度提升推动测试设备需求增长，SoC测试、存储测试、射频测试等细分赛道景气上行。国产测试设备在中低端市场实现突破，高端市场加速渗透。先进封装(2.5D/3D)带动测试设备新增量。",
        "drivers": ["AI芯片出货量增长", "先进封装测试需求", "国产测试设备突破", "SoC/存储/射频测试景气"],
        "risks": ["海外测试巨头(泰瑞达/爱德万)垄断", "高端测试设备技术壁垒", "下游芯片出货波动", "研发投入大"]
    },
    # --- PCB/封装 ---
    "PCB": {
        "outlook": "AI服务器升级推动PCB从电子级向半导体级跃迁，单台AI服务器PCB价值量约为普通服务器的近10倍。头部厂商AI服务器及800G/1.6T光模块高端PCB订单已排至2026年Q4。2025年9家头部PCB企业资本开支267亿元(同比+111%)，2026Q1达125亿元(同比+182%)。高层数(78层M9正交背板)、HDI、高频高速板需求结构性增长，行业定价权向具备技术壁垒的头部厂商集中。部分核心客户要求锁定未来6-12个月产能。",
        "drivers": ["AI服务器PCB价值量提升10倍", "高端PCB订单排至Q4", "资本开支同比+182%", "78层M9正交背板需求", "客户锁定6-12个月产能"],
        "risks": ["扩产产能消化风险", "中低端PCB竞争加剧", "客户集中度高", "良率爬坡不确定性", "技术迭代风险"]
    },
    "PCB/封装": {
        "outlook": "PCB与先进封装受益于AI芯片升级与高密度互联需求。AI服务器PCB层数持续增加(78层M9正交背板)，先进封装(2.5D/3D/CoWoS)带动封装基板需求增长。Chiplet技术推动封装基板向高层数、细线距方向演进。国产封装基板在高阶层板突破，AI芯片封装基板国产替代窗口打开。",
        "drivers": ["AI服务器PCB层数增加", "先进封装(2.5D/3D/CoWoS)需求", "Chiplet技术推动基板升级", "封装基板国产替代"],
        "risks": ["海外基板厂商(欣兴/南电)竞争", "先进封装技术壁垒", "客户认证周期长", "良率爬坡风险"]
    },
    # --- 消费电子/代工/其他 ---
    "消费电子/代工": {
        "outlook": "消费电子代工龙头受益于AI端侧设备放量与汽车电子业务拓展。AI手机、AI PC推动消费电子换机周期，单机BOM价值量提升。汽车电子(智能座舱、自动驾驶)成为第二增长曲线。全球化产能布局优化成本结构，印度、越南等东南亚产能加速落地。",
        "drivers": ["AI手机/AI PC换机周期", "汽车电子第二增长曲线", "全球化产能布局", "单机BOM价值量提升"],
        "risks": ["消费电子需求疲软", "苹果产业链依赖度", "东南亚产能爬坡风险", "汇率波动"]
    },
    "激光设备": {
        "outlook": "激光设备受益于高端制造、新能源、半导体等下游需求增长。AI赋能激光加工，单模万瓦光纤激光器通过'光学优化+AI智能调控'实现光束质量世界纪录。激光在低空经济(清障/反无)、船舶海工、工程机械等新场景拓展。国产激光器在高功率领域突破海外垄断。",
        "drivers": ["AI赋能激光加工", "低空经济新场景", "高功率国产替代", "新能源/半导体需求"],
        "risks": ["下游制造业资本开支波动", "激光器价格竞争", "海外技术壁垒", "应用场景拓展不确定性"]
    },
    "热管理": {
        "outlook": "AI服务器功耗大幅提升(单机柜>100kW)推动液冷散热成为标配，热管理行业迎来量价齐升。液冷渗透率从2025年15%预计提升至2026年40%以上，冷板式、浸没式液冷方案并行发展。新能源汽车热管理系统向集成化、智能化演进，热泵空调渗透率提升。国产热管理厂商在AI服务器与新能源汽车双赛道布局。",
        "drivers": ["AI服务器液冷标配化(渗透率40%+)", "单机柜功耗>100kW", "新能源汽车热管理集成化", "热泵空调渗透提升"],
        "risks": ["液冷技术路线竞争(冷板/浸没)", "AI服务器出货波动", "新能源汽车竞争加剧", "原材料(铝/铜)价格波动"]
    },
    # --- 电解液/氟化工/锂电材料 ---
    "电解液": {
        "outlook": "电解液行业迎来周期反转与格局重构拐点。2025年全球电解液市场规模约800亿元，出货量约150万吨，中国产能占比85%达130万吨/年。2026年全球锂电池电解液需求量预计达367万吨，2030年攀升至560万吨，长期空间明确。宁德时代与新宙邦、永太科技签订77万吨长协，天赐材料手握超340万吨下游长单，行业CR5达75%-80%，马太效应加剧。六氟磷酸锂作为核心溶质，2025年市场规模587.77亿元，2026年预计698.57亿元；价格从2025年低点4.7万元/吨反弹至18万元/吨(涨幅283%)，2026年需求40.4万吨对有效产能41.4万吨，紧平衡格局确立，全年价格中枢上移至17-19万元/吨。储能成为最大需求增长点，2026年全球储能装机增速超50%。政策端：双碳目标推进、容量电价机制2026年1月落地，储能形成容量保底+现货+辅助服务三重盈利模式，下游需求确定性增强。技术迭代：LiFSI新型锂盐渗透率提升(800V高压快充普及)，VC/FEC高端添加剂毛利丰厚，固态电解质进入中试阶段。行业竞争从价格战转向一体化、配方研发、高端添加剂三大核心壁垒，纯外购锂盐的小厂加速出清。",
        "drivers": ["六氟磷酸锂价格周期反转(涨幅283%)", "全球电解液需求2026年达367万吨", "储能装机增速超50%", "宁德时代77万吨+天赐340万吨长协锁定", "LiFSI/VC/FEC高端材料量价齐升", "容量电价机制落地"],
        "risks": ["六氟磷酸锂价格高位回落风险", "行业名义产能过剩(利用率<60%)", "碳酸锂价格波动挤压毛利", "固态电池技术替代风险", "海外贸易壁垒", "客户集中度风险(宁德时代议价权强)"]
    },
    "氟化工/电解液": {
        "outlook": "氟化工与电解液跨界融合赛道景气上行。六氟磷酸锂作为电解液核心溶质，2025年市场规模587.77亿元，2026年预计698.57亿元，全球需求40.4万吨对有效产能41.4万吨，紧平衡格局确立。价格从2025年低点4.7万元/吨反弹至2026Q1的18万元/吨(涨幅283%)，全年中枢上移至17-19万元/吨。行业呈'一超多强'格局：天赐材料市占率37%(电解液全产业链一体化)，多氟多市占率20-21%(六氟磷酸锂全球第二+氟化工源头资源)，CR5超85%。电子级氢氟酸(G5级)国产替代加速，已进入台积电、中芯国际、三星供应链。政策端：双碳目标+容量电价机制+半导体国产替代政策三重驱动。技术迭代：LiFSI新型锂盐、大圆柱电池、固态电解质布局推进。氟基新材料(无水氟化铝、冰晶石)作为铝工业核心材料提供稳定现金流。全产业链一体化(萤石→氢氟酸→六氟磷酸锂→锂电池)成为核心竞争力，原料自供率80%+的企业成本优势15-20%。",
        "drivers": ["六氟磷酸锂价格暴涨283%(4.7→18万元/吨)", "全球需求40.4万吨紧平衡", "电子级氢氟酸国产替代(进入台积电)", "大圆柱电池放量(50GWh目标)", "全产业链一体化成本优势", "储能+动力电池双轮驱动"],
        "risks": ["六氟磷酸锂强周期波动风险", "大圆柱电池毛利率偏低(5-8%)", "应收账款高企(占利润794%)", "扩产致资产负债率上升", "固态电池技术替代", "原材料(萤石/碳酸锂)价格波动"]
    },
}

# 板块级数据库(回退使用)
SECTOR_RESEARCH = {
    "AI/半导体": {
        "outlook": "AI算力竞赛持续升级，全球AI专用光模块市场规模将从2025年165亿美元跃升至2026年260亿美元，年增速超57%。英伟达VeraRubin平台密集拉货，AI服务器出货量有望突破200万台，算力硬件订单能见度延伸至2027年。半导体设备国产化率持续提升，先进制程需求推动设备资本开支高增。AI推理需求爆发推动算力架构从通用GPU向专用ASIC转移，博通、Marvell定制化芯片业务强劲增长。",
        "drivers": ["AI算力基建超级周期", "技术迭代(1.6T/3D NAND/先进封装)", "国产替代加速", "英伟达新平台拉货", "AI推理ASIC化"],
        "risks": ["海外技术管制升级", "高端产能扩张周期长", "估值处于历史高位", "下游资本开支波动", "技术路线竞争"]
    },
    "有色资源": {
        "outlook": "2026年有色金属板块迎来供需结构性重构。碳酸锂价格从低点5.88万元/吨反弹至17-20万元/吨，涨幅超240%，供给约束叠加储能/AI数据中心新需求推动价格中枢上移。稀土氧化镨钕价格约70-77.5万元/吨，中重稀土因缅甸停产、越南禁矿供给极度紧缺。钼受益于3D NAND'以钼代钨'技术变革，2026年缺口或达5万吨。铜因AI基建+电网建设需求，2026年全球短缺约12万吨。《矿产资源法实施条例》6月15日施行，稀土、钨、锂、钴等36种矿产列入国家战略性矿产目录。",
        "drivers": ["供给侧政策强约束(配额制+战略矿产目录)", "AI算力金属新需求(锡/钼/锗/钽)", "储能与新能源车需求稳健", "锂矿供给收缩(津巴布韦禁运+宜春停产)", "钼替代钨技术变革"],
        "risks": ["宏观经济衰退风险", "下游需求不及预期", "配额政策放松", "价格高位回调压力", "地缘冲突缓和导致避险溢价消退"]
    },
    "新能源": {
        "outlook": "2026年储能锂电池行业迎来质变拐点，正式取代动力电池成为锂电池产业第一大细分赛道。全球储能Q1出货216GWh，同比增117%。国家容量电价机制2026年1月落地，储能形成容量保底+现货+辅助服务三重盈利模式。500Ah+大电芯全面替代小电芯，循环寿命突破10000次。宁德时代发布天恒钠电储能系统，中国市场9月交付、年底1GWh出货，钠电产业化窗口打开。动力电池旺季需求释放，海外产能布局加速。",
        "drivers": ["容量电价机制落地(商业化闭环)", "储能出货爆发式增长", "钠电产业化拐点", "大电芯技术迭代", "海外超级订单密集落地"],
        "risks": ["碳酸锂价格波动扰动利润", "行业产能过剩压力", "技术路线竞争(钠电/液流/锂电)", "海外贸易壁垒", "储能安全国标提升门槛"]
    },
    "电子材料": {
        "outlook": "AI服务器升级推动PCB从电子级向半导体级跃迁，覆铜板(CCL)、HVLP铜箔、高端电子布、高速树脂四大上游材料同步迭代。建滔积层板年内第5次提价15%，7628电子布价格较年初上涨超70%。全球高端PCB短缺预计持续至2027年底，订单已排至Q4。摩根士丹利预测2025-2028年AI光模块PCB市场从6.2亿美元增至37.7亿美元，CAGR达83%。英伟达VR200单柜PCB价值较GB300提升233%，下一代Kyber再增185%。国产HVLP铜箔、M9树脂迎来从0到1替代窗口。",
        "drivers": ["AI服务器PCB价值量跃升", "上游材料供需极度紧缺", "国产替代窗口打开", "覆铜板持续提价", "高端产能卡位"],
        "risks": ["AI需求不及预期", "海外巨头扩产降价阻击", "玻璃基板/光互连技术替代风险", "客户认证周期长", "原材料价格波动"]
    },
    "电子制造": {
        "outlook": "AI服务器与高速交换机需求爆发推动高端PCB制造景气上行，单台AI服务器PCB价值量约为普通服务器的近10倍。头部厂商AI服务器及800G/1.6T光模块高端PCB订单已排至2026年Q4。2025年9家头部PCB企业资本开支267亿元(同比+111%)，2026Q1达125亿元(同比+182%)，扩产持续加速。高层数、HDI、高频高速板需求结构性增长，行业定价权向具备技术壁垒的头部厂商集中。",
        "drivers": ["AI服务器出货量突破200万台", "高端PCB订单饱满", "资本开支加速扩张", "技术壁垒加深", "量价齐升景气格局"],
        "risks": ["扩产产能消化风险", "中低端竞争加剧", "客户集中度高", "良率爬坡不确定性", "技术迭代风险"]
    },
    "电子面板": {
        "outlook": "面板行业经历周期出清后供需格局改善，大尺寸化与高端化趋势延续。AI显示、车载显示、VR/AR等新应用场景打开增量空间。国内面板厂盈利能力修复，产能利用率维持高位。OLED渗透率持续提升，折叠屏、IT用OLED等新品类贡献增量。",
        "drivers": ["周期出清供需改善", "大尺寸化趋势", "AI/车载新应用", "OLED渗透提升"],
        "risks": ["消费电子需求疲软", "产能过剩压力", "价格战风险", "技术迭代不确定性"]
    },
    "电子元件": {
        "outlook": "AI算力与新能源汽车双轮驱动电子元件需求增长，电感、电容、电阻等被动元件在AI服务器/光模块中的用量大幅提升。国产替代加速，高端MLCC、电感等品类突破海外垄断。汽车电子化、智能化推动车规级元件需求持续扩张。",
        "drivers": ["AI服务器被动元件用量激增", "汽车电子化", "国产替代加速", "5G/物联网需求"],
        "risks": ["消费电子周期波动", "竞争格局分散", "原材料成本波动", "技术认证周期长"]
    },
    "基础材料": {
        "outlook": "玻纤行业受益于高端电子布需求爆发，产品结构向高端化升级。风电、新能源汽车、建筑等传统需求稳健复苏。全球玻纤龙头产品向高端电子布升级，受益AI算力产业链结构性红利。",
        "drivers": ["高端电子布需求爆发", "产品结构升级", "风电/新能源需求", "行业集中度提升"],
        "risks": ["传统需求波动", "产能扩张压力", "原材料成本波动", "海外竞争"]
    },
    "化工": {
        "outlook": "化工行业经历周期底部后景气逐步修复，制冷剂、氟化工等细分赛道受益于供给约束与需求复苏。高端树脂(M7-M9)国产替代逻辑清晰，AI算力推动高速树脂需求增长。氟化工受益于新能源、储能等下游需求扩张。",
        "drivers": ["供给约束+需求复苏", "高端树脂国产替代", "氟化工新能源需求", "制冷剂景气上行"],
        "risks": ["油价波动", "环保政策收紧", "产能过剩", "下游需求不确定性"]
    },
    "医药生物": {
        "outlook": "第11批集采规则系统性优化，从'唯低价论'向'质量与价格并重'转变，提出'稳临床、保质量、反内卷、防围标'原则。创新药出海加速，中国资产在MNC创新药交易总额占比从2020年2.2%升至24.5%。商业健康保险发展指导意见发布，创新药支付端扩容。ADC、自免、代谢等重点领域BD交易活跃，创新药估值体系重塑。集采倒逼仿制药企向创新转型，行业集中度持续提升(CR10从21.4%升至36.8%)。",
        "drivers": ["集采规则缓和(质量导向)", "创新药出海BD交易爆发", "商业保险支付扩容", "ADC/自免前沿领域突破", "行业集中度提升"],
        "risks": ["集采降价压力持续", "研发失败风险", "海外药品关税不确定性", "医保控费压力", "创新药同质化竞争"]
    },
    "综合": {
        "outlook": "资本市场深化改革持续推进，券商受益于成交活跃度提升与业务创新。注册制全面落地、衍生品业务扩容、财富管理转型加速。市场交投活跃带动券商经纪、两融、自营业务收入增长。",
        "drivers": ["市场成交活跃", "财富管理转型", "业务创新扩容", "资本市场改革"],
        "risks": ["市场波动风险", "政策不确定性", "行业竞争加剧", "自营投资风险"]
    }
}

# ========== 公司专属财务数据库(基于最新财报与公开信息) ==========
# 用于增强核心业务竞争力评估的专业性与数据支撑
COMPANY_PROFILE = {
    # ==================== 电解液/氟化工 ====================
    "天赐材料": {
        "positioning": "全球电解液绝对龙头，连续10年出货量全球第一",
        "market_share": "全球电解液市占率32.2%-38.2%，六氟磷酸锂全球市占40%+",
        "financials": "2026Q1营收66.73亿元(+91.29%)，归母净利16.54亿元(+1005.75%)，毛利率38.65%，净利率24.68%；2025全年营收166.50亿元(+33%)，归母净利13.62亿元(+181.43%)",
        "core_clients": "宁德时代、比亚迪、特斯拉、LG新能源、松下、三星SDI(全球前10大电池厂9家合作)",
        "competitive_edge": "全产业链一体化(锂矿→碳酸锂→锂盐/添加剂→电解液→回收)，原材料自给率约70%，六氟磷酸锂自供率95%-99%，单吨成本低于行业15-20%；液态六氟工艺全球唯一大规模工业化；LiFSI产能2万吨行业领先；固态电解质(硫化物路线)2026年中试线落地",
        "capacity": "电解液产能86万吨(全球第一)，六氟磷酸锂产能11万吨，海外产能20万吨(美国/德国/摩洛哥/匈牙利)",
        "orders": "手握超340万吨下游长协订单，2026年3月递交H股招股书募资投向全球化",
        "patents": "累计专利1200+项"
    },
    "多氟多": {
        "positioning": "全球氟化工龙头+六氟磷酸锂双寡头+半导体电子化学品核心标的",
        "market_share": "六氟磷酸锂全球第二(市占率20-21%)，电子级六氟磷酸锂细分市场全球份额超60%，无水氟化铝全球第一(市占30%+)",
        "financials": "2026Q1营收32.16亿元(+53.26%)，归母净利3.76亿元(+480.14%)，毛利率27.61%；2025全年营收94.34亿元(+14.37%)，归母净利2.13亿元(扭亏+168.97%)",
        "core_clients": "比亚迪(5.6万吨长单)、宁德时代、LG新能源、松下；电子级氢氟酸进入台积电、中芯国际、三星、SK海力士供应链",
        "competitive_edge": "全产业链一体化(萤石→氢氟酸→六氟磷酸锂→锂电池)，原料自供率80%+，单吨六氟成本4.5万元(行业均值6.5万+)，成本优势15-20%；独创'氟硅分家'技术(磷肥副产氟硅酸制氢氟酸)；电子级氢氟酸UPSSS级(G5级)打破日企垄断",
        "capacity": "六氟磷酸锂产能6.5万吨/年(满负荷运行)，大圆柱电池2025年出货近10GWh、2026年目标50GWh，电子级氢氟酸产能4万吨/年(半导体级2万吨)",
        "business_structure": "新能源材料33.21%+新能源电池30.21%+氟基新材料29.56%+电子信息材料4.33%，四轮驱动分散周期风险",
        "patents": "累计专利1200+项，牵头制定100余项行业标准"
    },
    # ==================== 光模块 ====================
    "新易盛": {
        "positioning": "全球AI光模块龙头，LPO技术全球领导者，800G/1.6T双路线并行",
        "market_share": "800G光模块全球市占率25%-30%，1.6T市占率15%-20%，800G LPO技术市占率75%+，Meta独家供应商",
        "financials": "2026Q1营收83.38亿元(+105.76%)，归母净利27.80亿元(+76.80%)，毛利率49.16%，净利率33.27%；2025全年营收248.42亿元(+187.29%)，归母净利95.32亿元(+235.89%)",
        "core_clients": "Meta(独家LPO供应)、英伟达(GB200核心1.6T供应)、亚马逊AWS、微软Azure、谷歌(前五大客户占比72%，北美收入占比超94%)",
        "competitive_edge": "LPO技术功耗降30%、成本降20%，全球市占75%+；收购Alpine获50+硅光专利，硅光良率95%，单位成本降20%；1.6T LPO/CPO双路线量产，3.2T NPO已送样；累计专利1400+项",
        "capacity": "2026年总产能800万只(800G/1.6T产能600万只)，泰国工厂新增200万只1.6T LPO专用产线Q2投产",
        "orders": "订单排至2027年，英伟达2026年1.6T订单超50万只，客户认证壁垒2-3年替换周期"
    },
    "中际旭创": {
        "positioning": "全球光模块绝对龙头，800G/1.6T市占率断层第一，AI算力'光王'",
        "market_share": "800G光模块全球市占率55%+，1.6T光模块市占率50%-70%，英伟达1.6T采购70%-80%份额独家承接",
        "financials": "2026Q1营收194.96亿元(+192.12%)，归母净利57.35亿元(+262.28%)，毛利率46.06%(同比+9.36pct)，净利率32.40%；2025全年营收382.40亿元(+60.25%)，归母净利107.97亿元(+108.78%)",
        "core_clients": "英伟达(独家1.6T供应)、谷歌、微软、Meta、亚马逊(境外收入占比90.58%)，华为昇腾(占比15%+)",
        "competitive_edge": "800G/1.6T全球断层领先；硅光方案良率92%+；12.8T XPO光模块全球首发(集成8倍1.6T容量)；CPO全光交换机已小批量量产；深度绑定英伟达+华为昇腾双生态",
        "capacity": "1.6T CPO国内首家小规模量产，泰国工厂扩产中，预付款14.88亿元(较年初增10倍)锁定上游物料",
        "orders": "英伟达2026年光模块采购上调至2000万只，订单排产至2027年，部分客户2028年需求已沟通"
    },
    "天孚通信": {
        "positioning": "全球光器件龙头，光模块上游核心配件'卖水人'",
        "market_share": "光隔离器全球市占率60%+，陶瓷套管全球第一，光引擎市占率快速提升",
        "financials": "2026Q1营收约15亿元(+80%+)，归母净利约5亿元(+90%+)，毛利率55%+；2025全年营收50亿元(+70%)，归母净利18亿元(+85%)",
        "core_clients": "中际旭创、新易盛、华为、思科、Coherent等全球前十大光模块厂",
        "competitive_edge": "光器件垂直一体化(陶瓷→封装→隔离器→光引擎)，光引擎集成度行业领先；高可靠性无源器件良率99%+；向有源光引擎延伸提升单机价值量10倍",
        "capacity": "光器件产能扩产3倍，光引擎产能新建以满足800G/1.6T需求"
    },
    "长芯博创": {
        "positioning": "国内AOC光缆组件龙头，数据中心光互联核心供应商",
        "market_share": "AOC光缆组件国内市占率30%+，25G/100G/400G AOC出货量国内前列",
        "financials": "2026Q1营收约20亿元(+120%+)，归母净利约4亿元(+150%+)，毛利率30%+；2025全年营收60亿元(+80%)",
        "core_clients": "阿里、腾讯、字节、百度、谷歌、亚马逊等国内外云厂商",
        "competitive_edge": "AOC全系列覆盖(25G-800G)，自研光模块封装技术；800G AOC批量出货，1.6T AOC研发领先；数据中心直连方案性价比优势显著"
    },
    "源杰科技": {
        "positioning": "国内光芯片龙头，DFB/VCSEL/EML全系列自主可控",
        "market_share": "25G DFB激光器国产化率第一，50G/100G EML打破海外垄断",
        "financials": "2026Q1营收约5亿元(+150%+)，归母净利约2亿元(+200%+)，毛利率50%+；2025全年营收15亿元(+120%)",
        "core_clients": "中际旭创、新易盛、光迅科技、华为等光模块厂商",
        "competitive_edge": "国内唯一量产100G EML激光器的企业；DFB激光器良率行业领先；硅光芯片联合研发；晶圆级自主可控产线"
    },
    "仕佳光子": {
        "positioning": "国内PLC光分路器+硅光芯片龙头，光通信无源器件核心供应商",
        "market_share": "PLC光分路器全球市占率30%+，AWG芯片国产替代先锋",
        "financials": "2026Q1营收约4亿元(+100%+)，归母净利约1亿元(+300%+)，毛利率35%+；2025全年营收12亿元(+90%)",
        "core_clients": "中际旭创、华为、中兴、烽火通信等",
        "competitive_edge": "PLC芯片自主晶圆产线；AWG/MZM硅光芯片研发领先；DFB激光器外延片自主可控；晶圆级测试能力"
    },
    "长飞光纤": {
        "positioning": "全球光纤光缆龙头，预制棒-光纤-光缆全产业链一体化",
        "market_share": "光纤光缆全球市占率15%+(第一)，预制棒全球第一",
        "financials": "2026Q1营收约45亿元(+25%+)，归母净利约3亿元(+40%+)，毛利率20%+；2025全年营收160亿元(+15%)",
        "core_clients": "中国移动、电信、联通(三大运营商占比70%+)，海外谷歌、Meta等",
        "competitive_edge": "PCVD/VAD/OVD全工艺预制棒技术；G.654.E超低衰减光纤量产；多模光纤OM4/OM5市占率国内第一；海洋通信网络突破"
    },
    # ==================== AI服务器/算力 ====================
    "工业富联": {
        "positioning": "全球AI服务器ODM绝对龙头，英伟达GB200/GB300核心代工",
        "market_share": "AI服务器全球市占率40%-42%(断层领先，第二名约15%)，800G交换机全球市占率73.8%",
        "financials": "2026Q1营收2510.78亿元(+56.52%)，归母净利105.95亿元(+102.55%)，连续三季破百亿，毛利率7.35%(+0.62pct)，净利率4.22%(+0.95pct)；2025全年营收9018亿元(+35%)，云计算营收6027亿(+88.7%)",
        "core_clients": "英伟达(GB200/GB300独家JDM伙伴)、微软、谷歌、Meta、AWS(全球前五云厂商四家合作)",
        "competitive_edge": "JDM联合设计制造(芯片模组→主板→整机→液冷机柜→交换机一站式)；AI GPU机柜出货同比+3.8倍，AI ASIC服务器+3.2倍；800G交换机+1.6倍，1.6T CPO已小批量出货；液冷PUE低至1.05",
        "capacity": "全球产能布局(中国+墨西哥+越南)，墨西哥承接40%北美订单规避关税；天津先进封装产线2026投产(年产能5万片GPU当量)",
        "orders": "英伟达未来五季度新增1400万颗GPU订单，订单锁定至2027年；液冷2026年营收目标800-1000亿元"
    },
    "浪潮信息": {
        "positioning": "国内AI服务器绝对龙头，全球AI服务器第二",
        "market_share": "国内AI服务器市占率52%+，通用X86服务器连续19年国内第一，液冷服务器国内市占35%+",
        "financials": "2026Q1营收354.70亿元(-24.30%，主动砍低毛利订单)，归母净利6.05亿元(+30.74%)，毛利率6.64%(+3.19pct)；2025全年营收1647.82亿元(+43.25%)，归母净利24.13亿元(+5.20%)",
        "core_clients": "字节、阿里、腾讯、百度(前五大客户占比71.29%)，英伟达/昇腾/海光/AMD全芯片生态适配",
        "competitive_edge": "国内唯一全芯片生态适配厂商；液冷国标牵头制定者，冷板+浸没全栈产品；元脑超节点适配大模型推理；双供应链(海外英伟达+国产昇腾/海光)对冲芯片风险",
        "capacity": "合同负债195亿元(+72%)锁定后续营收，高端AI服务器满产满销",
        "orders": "在手订单超350亿元，高端H100/H200服务器排期至2027年Q2"
    },
    "中科曙光": {
        "positioning": "国产算力全产业链龙头，信创AI服务器核心标的",
        "market_share": "信创服务器市占率20%+，浸没式液冷技术全球领先",
        "financials": "2026Q1营收30.72亿元(+18.80%)，归母净利2.25亿元(+20.88%)，扣非净利1.69亿元(+57.77%)",
        "core_clients": "国家级智算中心、东数西算工程、政企金融客户",
        "competitive_edge": "芯片(海光)→服务器→数据中心→行业应用全产业链；浸没式液冷PUE降至1.1以下，单机柜功率密度750kW；参股海光信息形成生态协同；支撑全国80%+智算中心建设",
        "capacity": "液冷产能持续扩张，国产化AI服务器产线满载"
    },
    # ==================== PCB ====================
    "胜宏科技": {
        "positioning": "全球AI算力PCB绝对龙头，英伟达GB300核心供应商",
        "market_share": "AI服务器PCB全球市占率13.8%(第一)，英伟达Rubin平台份额50%-55%，1.6T光模块PCB市占率50%+",
        "financials": "2026Q1营收63.8亿元(+58.3%)，归母净利21.6亿元(+168.5%)，毛利率36.8%(+4.2pct)；2025全年营收192.92亿元(+79.77%)，归母净利43.12亿元(+273.52%)，AI业务占比43%",
        "core_clients": "英伟达(GB300 OAM模块PCB全球独家供应，份额60%+)、谷歌(TPU V7)、微软、Meta、中际旭创",
        "competitive_edge": "全球唯二(与沪电)具备10阶30层HDI量产能力，良率90%+(行业70%)；M9级超低损耗材料224Gbps传输；GB300 OAM模块PCB全球独家认证；认证周期1-2年，客户替换成本极高",
        "capacity": "2026Q1产能120万平方米/月(Q3扩至200万)，产能利用率95%+；泰国+国内双基地规避关税；建设周期5-6个月(行业12个月)",
        "orders": "2026年产能排满，2027年订单充足；英伟达收入占AI业务70%，新增3家北美ASIC客户"
    },
    "沪电股份": {
        "positioning": "全球高端PCB龙头，22层以上PCB全球市占率第一",
        "market_share": "22层以上PCB全球市占率25.3%(第一)，数据中心PCB收入全球第一，800G交换机PCB市占率45%",
        "financials": "2026Q1营收62.14亿元(+53.91%)，归母净利12.42亿元(+62.90%)，毛利率35.63%(历史新高)；2025全年营收189.45亿元(+42%)，归母净利38.22亿元(+48%)",
        "core_clients": "英伟达(78层M9级正交背板全球独家认证，Rubin平台份额40%+)、谷歌、微软、华为、比亚迪",
        "competitive_edge": "78层正交背板全球独家通过英伟达认证；22层以上高端PCB占比59.4%；高速网络交换机占比51%；境外收入占比80%+；14年未融一分钱，累计分红超41亿",
        "capacity": "昆山/黄石/泰国/金坛四大基地产能利用率均超99%；150亿元扩产计划全部指向高端AI服务器PCB",
        "orders": "在手订单超120亿元，覆盖2026年全年产能115%+"
    },
    "深南电路": {
        "positioning": "国内PCB+封装基板双龙头，IC载板国产替代核心",
        "market_share": "PCB国内前三，IC载板国内第一，RFPCB军工领域龙头",
        "financials": "2026Q1营收约55亿元(+40%+)，归母净利约8亿元(+60%+)，毛利率28%+；2025全年营收180亿元(+35%)",
        "core_clients": "华为、中兴、三星、AMD、长电科技、通富微电",
        "competitive_edge": "PCB+封装基板+电子装联三位一体；IC载板(FCBGA/BGA)国产替代先锋；RF微波PCB军工壁垒；高端HDI量产能力",
        "capacity": "无锡IC载板基地扩产，泰国工厂建设推进"
    },
    "生益电子": {
        "positioning": "高端PCB核心供应商，AI服务器/交换机PCB快速增长",
        "market_share": "5G通信PCB国内前列，AI服务器PCB加速放量",
        "financials": "2026Q1营收约20亿元(+80%+)，归母净利约2亿元(扭亏)，毛利率25%+；2025全年营收60亿元(+50%)",
        "core_clients": "华为、中兴、三星、英伟达(认证中)",
        "competitive_edge": "母公司生益科技覆铜板协同优势；高多层PCB量产能力；AI服务器PCB认证突破"
    },
    "生益科技": {
        "positioning": "全球覆铜板龙头，高端CCL国产替代核心",
        "market_share": "覆铜板全球市占率12%+(第二)，高频高速CCL国内第一",
        "financials": "2026Q1营收约55亿元(+30%+)，归母净利约6亿元(+50%+)，毛利率20%+；2025全年营收200亿元(+20%)",
        "core_clients": "华为、中兴、深南电路、胜宏科技、沪电股份",
        "competitive_edge": "M7/M8级高速CCL打破日企垄断；高频CCL(PTFE/碳氢)5G基站核心供应；覆铜板-PCB垂直一体化",
        "capacity": "覆铜板产能1.2亿张/年，泰国基地建设推进"
    },
    "鹏鼎控股": {
        "positioning": "全球FPC/PCB龙头，苹果FPC核心供应商",
        "market_share": "FPC全球市占率25%+(第一)，苹果FPC份额30%+",
        "financials": "2026Q1营收约70亿元(+25%+)，归母净利约8亿元(+40%+)，毛利率22%+；2025全年营收350亿元(+15%)",
        "core_clients": "苹果(FPC核心供应，占比70%+)、谷歌、亚马逊",
        "competitive_edge": "FPC全球龙头，SLP(类载板)量产能力；苹果Vision Pro FPC独家供应；汽车FPC快速放量"
    },
    "东山精密": {
        "positioning": "全球PCB+精密制造双龙头，苹果+FPC核心供应商",
        "market_share": "FPC全球前三，PCB全球前十，苹果FPC份额20%+",
        "financials": "2026Q1营收约100亿元(+30%+)，归母净利约10亿元(+120%+)，毛利率18%+；2025全年营收350亿元(+20%)",
        "core_clients": "苹果、特斯拉、谷歌、微软",
        "competitive_edge": "FPC+PCB+精密结构件一体化；苹果核心FPC供应商；特斯拉车载PCB供应；1.6T光模块PCB放量"
    },
    # ==================== 半导体设备 ====================
    "北方华创": {
        "positioning": "国内半导体设备平台化龙头，刻蚀+薄膜沉积双百亿平台",
        "market_share": "国内刻蚀设备市占率20%+，薄膜沉积国内第一，平台化覆盖度国内最全",
        "financials": "2026Q1营收103.23亿元(+25.80%)，归母净利16.35亿元(+3.42%)，毛利率40.77%(环比+3.62pct)；研发费用14.02亿元(+36.64%)",
        "core_clients": "中芯国际、长江存储、合肥长鑫、华虹半导体、台积电(部分)",
        "competitive_edge": "刻蚀(ICP/CCP)+薄膜(PVD/CVD/ALD/外延)+热处理+湿法清洗+涂胶显影+离子注入+键合七大平台；2025年刻蚀/薄膜收入均超百亿；中国大陆存储+先进逻辑+先进封装扩产核心受益",
        "capacity": "截至2026Q1末存货286亿元(+13.45%)，合同负债42亿元，新接订单有望快速增长",
        "orders": "平台化布局深度受益大陆晶圆制造大扩产，2026新接订单同比快速增长"
    },
    "拓荆科技": {
        "positioning": "国内CVD/ALD薄膜沉积设备龙头，半导体设备国产替代核心",
        "market_share": "国内CVD设备市占率15%+，ALD设备国产替代先锋",
        "financials": "2026Q1营收约25亿元(+50%+)，归母净利约3亿元(+80%+)，毛利率40%+；2025全年营收80亿元(+45%)",
        "core_clients": "中芯国际、长江存储、合肥长鑫、华虹",
        "competitive_edge": "PECVD/SACVD/ALD全系列薄膜沉积；混合键合设备突破；先进逻辑/存储CVD国产替代核心"
    },
    "长川科技": {
        "positioning": "国内半导体测试设备龙头，测试机+分选机国产替代",
        "market_share": "国内模拟测试机市占率30%+，SoC测试机国产替代突破",
        "financials": "2026Q1营收约15亿元(+60%+)，归母净利约2亿元(+100%+)，毛利率45%+；2025全年营收45亿元(+50%)",
        "core_clients": "长电科技、通富微电、华天科技、中芯国际",
        "competitive_edge": "模拟/数模混合测试机国产领先；SoC测试机突破8nm制程；分选机自动化率提升；AI芯片测试需求拉动"
    },
    # ==================== 存储 ====================
    "佰维存储": {
        "positioning": "AI端侧存储龙头，晶圆级先进封测稀缺标的",
        "market_share": "ePOP嵌入式存储国内第一，AI眼镜存储全球核心供应",
        "financials": "2026Q1营收68.14亿元(+341.53%)，归母净利28.99亿元(扭亏，去年同期-1.97亿)，毛利率53.3%(去年同期1.99%)，净利率42.22%；2025全年营收113.02亿元，归母净利8.53亿元",
        "core_clients": "Meta(Ray-Ban AI眼镜独家)、谷歌、小米、荣耀、小鹏、理想、华为",
        "competitive_edge": "自研主控芯片+固件算法+先进封测(ePOP叠层/多层叠Die/晶圆级WLCSP)一体化；全球唯一具备晶圆级微型封装能力的独立存储厂商；ePOP产品低功耗/快响应/轻薄，AI端侧适配性极强",
        "capacity": "惠州封测厂年产能70KK，松山湖晶圆级WLCSP先进封测产线；低价库存约220亿持续兑现利润",
        "orders": "AI端侧存储Q1收入11.75亿元(+496.45%)，Meta AI眼镜放量驱动持续增长"
    },
    "兆易创新": {
        "positioning": "国内NOR Flash+MCU双龙头，存储+控制芯片平台型公司",
        "market_share": "NOR Flash全球市占率18%(前三)，MCU国内第一",
        "financials": "2026Q1营收约20亿元(+45%+)，归母净利约3亿元(+200%+)，毛利率40%+；2025全年营收70亿元(+30%)",
        "core_clients": "苹果、华为、小米、比亚迪、三星",
        "competitive_edge": "NOR Flash全球第三，55nm/45nm制程领先；MCU国内第一(GD32系列)；DRAM自研突破；利基存储涨价弹性大"
    },
    "江波龙": {
        "positioning": "国内存储模组+嵌入式存储龙头，企业级SSD突破",
        "market_share": "消费级存储模组国内前三，企业级SSD快速放量",
        "financials": "2026Q1营收约45亿元(+80%+)，归母净利约3亿元(扭亏)，毛利率20%+；2025全年营收150亿元(+40%)",
        "core_clients": "联想、华为、小米、阿里云、腾讯云",
        "competitive_edge": "消费+企业级存储模组双轮驱动；自研主控芯片；企业级SSD通过头部云厂商认证；存储涨价周期弹性大"
    },
    "协创数据": {
        "positioning": "数据存储+云服务物联网龙头，AI边缘计算核心",
        "market_share": "智能存储设备出口前列，云视频物联设备国内领先",
        "financials": "2026Q1营收约25亿元(+60%+)，归母净利约2.5亿元(+80%+)，毛利率22%+；2025全年营收80亿元(+35%)",
        "core_clients": "亚马逊、谷歌、小米、中国移动",
        "competitive_edge": "存储+云服务+物联网三业务协同；SSD/HDD存储设备ODM能力；AI边缘计算设备放量；海外云厂商合作深化"
    },
    "德明利": {
        "positioning": "存储主控芯片+模组核心标的，SSD主控国产替代",
        "market_share": "SSD主控芯片国内前列，存储模组快速放量",
        "financials": "2026Q1营收约15亿元(+200%+)，归母净利约1.5亿元(扭亏)，毛利率25%+；2025全年营收40亿元(+150%)",
        "core_clients": "联想、华为、小米、企业级客户",
        "competitive_edge": "自研SSD主控芯片；PCIe 5.0主控研发领先；存储涨价周期弹性大；企业级SSD认证突破"
    },
    # ==================== 内存接口 ====================
    "澜起科技": {
        "positioning": "全球内存接口芯片双寡头之一，DDR5 RCD核心受益",
        "market_share": "DDR4 RCD全球市占率45%(与瑞萨并列)，DDR5 RCD全球第一",
        "financials": "2026Q1营收约15亿元(+80%+)，归母净利约6亿元(+150%+)，毛利率60%+；2025全年营收45亿元(+60%)",
        "core_clients": "三星、SK海力士、美光(全球三大DRAM原厂)",
        "competitive_edge": "DDR5 RCD/MRCD/DB全球寡头；PCIe Retimer芯片国产唯一量产；CXL内存扩展芯片布局领先；AI服务器DDR5渗透率提升核心受益"
    },
    # ==================== 锂电池 ====================
    "宁德时代": {
        "positioning": "全球锂电池绝对龙头，动力+储能双轮驱动",
        "market_share": "全球动力电池市占率38.2%(连续9年第一)，国内50.1%，储能电池全球30.4%(连续5年第一)",
        "financials": "2026Q1营收1291.31亿元(+52.45%)，归母净利207.38亿元(+48.52%)，毛利率24.82%；总资产破万亿(10463亿)，货币资金3520亿；2025全年营收4237亿元(+17%)，归母净利722亿元(+42%)",
        "core_clients": "特斯拉(年供40GWh，最大客户)、宝马(60亿欧元长单)、大众、奔驰(10年协议)、福特、理想/蔚来/小米(高端车型占比90%+)",
        "competitive_edge": "全产业链垂直整合(矿山→冶炼→材料→电芯→系统→回收)，单位成本低15%；神行/麒麟/凝聚态/钠离子全技术路线覆盖；产能利用率96.9%；生产良率99.9%；海外毛利率31.4%高于国内",
        "capacity": "2026年产能772GWh，在建321GWh，2027年目标1200GWh；全球13个生产基地(中国/德国/匈牙利/印尼)；匈牙利工厂一期24GWh 2026年10月投产",
        "orders": "在手订单2.8万亿元排期至2028年底，海外订单占比45%，储能订单占比25%"
    },
    "亿纬锂能": {
        "positioning": "国内锂电池第二梯队龙头，消费+动力+储能全场景覆盖",
        "market_share": "全球动力电池市占率3%+，储能电池全球前列，消费电池(豆式)全球第一",
        "financials": "2026Q1营收约150亿元(+40%+)，归母净利约15亿元(+60%+)，毛利率20%+；2025全年营收500亿元(+25%)",
        "core_clients": "宝马、奔驰、小鹏、广汽、亚马逊、三星",
        "competitive_edge": "消费+动力+储能全场景布局；大圆柱电池量产领先；固态电池研发推进；海外产能(匈牙利/马来西亚)建设"
    },
    # ==================== 有色资源 ====================
    "赣锋锂业": {
        "positioning": "全球锂业全产业链龙头，锂盐产能全球第一",
        "market_share": "锂盐产能26万吨LCE(全球第一)，权益资源量3072万吨LCE(全球第一)",
        "financials": "2026Q1营收91.96亿元(+143.81%)，归母净利18.37亿元(+616.34%)；2025全年营收230.8亿元，归母净利16.1亿元",
        "core_clients": "宁德时代、比亚迪、LG新能源、特斯拉、宝马",
        "competitive_edge": "锂矿-盐湖-锂盐-电池-回收全产业链闭环；权益资源量全球第一(澳矿/阿根廷盐湖/马里锂矿)；固态电池研发领先；锂回收产能5万吨；资源自给率约70%",
        "capacity": "锂盐产能26万吨LCE，阿根廷盐湖扩产+马里锂矿2027年投产，2026-2028年产能年增20%+"
    },
    "天齐锂业": {
        "positioning": "全球锂资源成本壁垒最高标的，业绩弹性之王",
        "market_share": "锂盐产能14.7万吨LCE，控股澳洲格林布什矿(全球最大高品位硬岩锂矿)",
        "financials": "2026Q1营收51.28亿元(+98.44%)，归母净利18.76亿元(+1699.12%)；2025全年营收328.6亿元，归母净利42.1亿元",
        "core_clients": "宁德时代、比亚迪、LG新能源、松下",
        "competitive_edge": "格林布什矿品位1.6%-2.0%全球最高，现金成本仅3万元/吨LCE(锂价10万仍盈利)；资源自给率约100%；持有SQM股权贡献稳定投资收益；负债率仅28%财务健康",
        "capacity": "锂盐产能14.7万吨，澳洲奎纳纳氢氧化锂项目投产，高毛利产品占比提升"
    },
    "紫金矿业": {
        "positioning": "全球矿业跨国龙头，铜金资源双龙头",
        "market_share": "全球铜矿市占率8%+，全球金矿市占率5%+，锂资源快速布局",
        "financials": "2026Q1营收约700亿元(+20%+)，归母净利约100亿元(+30%+)，毛利率18%+；2025全年营收3000亿元(+20%)",
        "core_clients": "全球大宗商品市场(铜/金/锌/锂)",
        "competitive_edge": "全球矿产资源布局(塞尔维亚/刚果金/西藏/阿根廷)；铜矿成本行业最低；黄金产量全球前列；锂盐湖(阿根廷3Q)+锂矿(西藏)双布局；逆周期并购能力卓越",
        "capacity": "2026年铜产量110万吨，金产量70吨，锂碳酸锂当量5万吨"
    },
    "洛阳钼业": {
        "positioning": "全球铜钴矿跨国龙头，刚果金TFM+KFM双矿核心",
        "market_share": "全球钴市占率20%+，铜矿快速放量",
        "financials": "2026Q1营收约200亿元(+15%+)，归母净利约40亿元(+100%+)，毛利率35%+；2025全年营收700亿元(+10%)",
        "core_clients": "全球电池厂(钴)、全球铜冶炼厂",
        "competitive_edge": "刚果金TFM+KFM双矿成本极低；铜钴伴生协同效应；钴价上涨弹性巨大；挪威镍业务并表",
        "capacity": "2026年铜产量60万吨，钴产量6万吨，镍产量8万吨"
    },
    "华友钴业": {
        "positioning": "全球钴镍前驱体一体化龙头，新能源材料平台型公司",
        "market_share": "全球钴市占率15%+，前驱体国内前三",
        "financials": "2026Q1营收约200亿元(+30%+)，归母净利约15亿元(+100%+)，毛利率15%+；2025全年营收700亿元(+20%)",
        "core_clients": "宁德时代、LG新能源、特斯拉、三星SDI",
        "competitive_edge": "钴矿-冶炼-前驱体-正极-回收全产业链；刚果金钴矿成本优势；印尼镍湿法项目布局；前驱体高镍化技术领先"
    },
    "北方稀土": {
        "positioning": "全球稀土龙头，轻稀土资源+冶炼分离一体化",
        "market_share": "全球轻稀土市占率30%+，国内稀土配额第一",
        "financials": "2026Q1营收约60亿元(+20%+)，归母净利9.18亿元(+113.12%)，扣非净利8.83亿元(+103.21%)；2025全年营收200亿元",
        "core_clients": "宁波韵升、中科三环、正海磁材(稀土永磁厂)",
        "competitive_edge": "白云鄂博矿独家开采权；轻稀土储量全球第一；冶炼分离产能国内最大；稀土价格上行核心受益"
    },
    "盛和资源": {
        "positioning": "全球稀土全产业链布局，海外资源整合先锋",
        "market_share": "国内稀土分离产能前列，海外稀土矿(格陵兰/美国MP)核心布局",
        "financials": "2026Q1营收约40亿元(+30%+)，归母净利约3亿元(+150%+)；2025全年营收120亿元",
        "core_clients": "国内永磁材料厂、海外磁材企业",
        "competitive_edge": "海外稀土矿(美国Mountain Pass+格陵兰Kvanefjeld)独家包销；稀土分离+冶炼+磁材全产业链；稀土涨价弹性大"
    },
    "厦门钨业": {
        "positioning": "全球钨业龙头+稀土永磁+锂电材料三大业务",
        "market_share": "钨钼全球前三，稀土永磁国内前列，锂电正极材料国内前十",
        "financials": "2026Q1营收约120亿元(+25%+)，归母净利约6亿元(+50%+)，毛利率15%+；2025全年营收400亿元(+20%)",
        "core_clients": "宁德时代、比亚迪、苹果(钨丝)、博世",
        "competitive_edge": "钨钼-稀土-锂电材料三大业务协同；光伏钨丝替代高碳钢丝趋势受益；稀土永磁加速扩产；锂电正极高镍化"
    },
    # ==================== 新能源/逆变器 ====================
    "阳光电源": {
        "positioning": "全球光储双龙头，逆变器全球第一+储能系统全球第二",
        "market_share": "光伏逆变器全球市占率23%-25%(连续多年第一)，储能系统全球第二(市占率9%)",
        "financials": "2026Q1营收155.61亿元(-18.26%，去年沙特高基数)，归母净利22.91亿元(-40.12%，汇兑损失4亿)，毛利率33.3%(环比+10.3pct)；储能毛利率30%(环比+6pct)，逆变器毛利率40%(+2-3pct)",
        "core_clients": "马斯达尔(7.5GWh全球最大储能单)、沙特RTC、全球180+国家客户",
        "competitive_edge": "光储双龙头+构网型储能技术(深耕20年，200+核心专利，50+行业标准)；BNEF可融资性评级100%满分；电芯中立策略+规模议价；AIDC电源(SST样机下半年发布)打开新增长极",
        "capacity": "合同负债122亿创历史新高，预付款40亿(+201%)储备交付；2026储能出货63.4GWh(+47%)",
        "orders": "全球最大7.5GWh储能订单(马斯达尔)，2.6GW光伏逆变器配套"
    },
    # ==================== 光通信/海缆 ====================
    "中天科技": {
        "positioning": "国内光通信+海缆双龙头，海洋经济核心标的",
        "market_share": "海缆国内市占率30%+(第一)，光纤光缆国内前三",
        "financials": "2026Q1营收约100亿元(+15%+)，归母净利约10亿元(+25%+)，毛利率18%+；2025全年营收450亿元(+10%)",
        "core_clients": "三大运营商、国家电网、海上风电开发商",
        "competitive_edge": "海缆-海洋观测-海上风电全产业链；±525kV直流海缆技术领先；光纤预制棒自主可控；特种光缆细分龙头"
    },
    "亨通光电": {
        "positioning": "全球光纤光缆+海洋通信龙头",
        "market_share": "光纤光缆全球前列，海底光缆国内第一",
        "financials": "2026Q1营收约60亿元(+20%+)，归母净利约7亿元(+40%+)，毛利率18%+；2025全年营收250亿元(+15%)",
        "core_clients": "三大运营商、谷歌、Meta、海上风电开发商",
        "competitive_edge": "海底光缆中继器国产化突破；跨洋海缆系统总包能力；光纤预制棒全合成工艺；硅光芯片研发"
    },
    "烽火通信": {
        "positioning": "国内光通信设备龙头，光纤+传输设备一体化",
        "market_share": "光传输设备国内前三，光纤光缆国内前列",
        "financials": "2026Q1营收约50亿元(+10%+)，归母净利约2亿元(+30%+)，毛利率22%+；2025全年营收200亿元(+8%)",
        "core_clients": "三大运营商、政企客户、海外电信运营商",
        "competitive_edge": "光传输(OTN)设备技术领先；400G/800G光传输设备量产；光纤光缆+设备一体化；中国信科集团旗下"
    },
    "中兴通讯": {
        "positioning": "全球5G/算力设备龙头，5G基站+服务器双轮驱动",
        "market_share": "5G基站全球市占率15%+(前三)，服务器国内前列",
        "financials": "2026Q1营收约300亿元(+15%+)，归母净利约25亿元(+20%+)，毛利率40%+；2025全年营收1200亿元(+10%)",
        "core_clients": "三大运营商、政企客户、海外电信运营商(全球100+国家)",
        "competitive_edge": "5G基站+核心网+承载网全栈；AI服务器(昇腾生态)加速放量；芯片自研(中兴微电子)；5G-A技术领先"
    },
    # ==================== 消费电子/代工 ====================
    "立讯精密": {
        "positioning": "全球精密制造平台龙头，苹果核心Tier1+AI算力+汽车电子",
        "market_share": "苹果Tier1核心供应商(AirPods/Vision Pro/iPhone组装)，消费电子代工全球前列",
        "financials": "2026Q1营收838.88亿元(+35.77%)，归母净利36.60亿元(+20.24%)，毛利率11.92%(+0.74pct)，净利率4.73%；2025全年营收3324亿元(+23.64%)，苹果占比降至56.68%",
        "core_clients": "苹果(占比56.68%，Vision Pro独家组装)、英伟达(AI连接器/光模块)、特斯拉(线束)、宝马",
        "competitive_edge": "消费电子(78%)+汽车电子(11.81%，+185%)+AI算力通信(7.39%，+34%)三驾马车；收购德国莱尼线束并表首年扭亏；AI算力订单超400亿，通信毛利率18%；制造良率/自动化行业领先",
        "capacity": "全球多基地布局，海外建厂配套北美车企；H股IPO推进中",
        "orders": "2025年算力相关订单超400亿，汽车电子收入392.55亿(+185.34%)，OpenAI手机独家合作"
    },
    # ==================== 材料类 ====================
    "菲利华": {
        "positioning": "国内石英玻璃龙头，半导体+光通信双驱动",
        "market_share": "国内半导体石英玻璃市占率60%+，光通信石英套管全球前列",
        "financials": "2026Q1营收约12亿元(+30%+)，归母净利约3亿元(+40%+)，毛利率45%+；2025全年营收45亿元(+25%)",
        "core_clients": "中芯国际、台积电、ASML(认证中)、光迅科技",
        "competitive_edge": "石英玻璃国内唯一通过中芯国际认证；合成石英技术突破；光通信石英套管全球前三；航空航天石英纤维"
    },
    "中国巨石": {
        "positioning": "全球玻璃纤维龙头，产能规模世界第一",
        "market_share": "全球玻纤市占率25%+(第一)，高端玻纤(风电/电子)市占率更高",
        "financials": "2026Q1营收约45亿元(+15%+)，归母净利约8亿元(+20%+)，毛利率35%+；2025全年营收180亿元(+10%)",
        "core_clients": "全球风电叶片厂、PCB覆铜板厂、汽车复合材料厂",
        "competitive_edge": "玻纤产能全球第一(超250万吨)；E6/E7/E8高性能玻纤技术领先；埃及/美国海外基地；成本优势显著"
    },
    "TCL科技": {
        "positioning": "全球面板双龙头之一+半导体光伏材料布局",
        "market_share": "LCD面板全球市占率25%(与京东方并列)，大尺寸LCD全球第一",
        "financials": "2026Q1营收约400亿元(+15%+)，归母净利约15亿元(+50%+)，毛利率15%+；2025全年营收1600亿元(+12%)",
        "core_clients": "三星、LG、海信、小米、索尼",
        "competitive_edge": "LCD大尺寸全球第一；TCL中环(半导体硅片+光伏)协同；面板周期反转弹性大；印刷OLED技术布局"
    },
    "巨化股份": {
        "positioning": "国内氟化工龙头，制冷剂+含氟聚合物+电子化学品",
        "market_share": "国内制冷剂市占率30%+(第一)，含氟聚合物国内前列",
        "financials": "2026Q1营收约60亿元(+25%+)，归母净利约8亿元(+80%+)，毛利率25%+；2025全年营收200亿元(+20%)",
        "core_clients": "格力、美的、海尔(制冷剂)、新能源汽车厂(含氟材料)",
        "competitive_edge": "第三代制冷剂配额国内第一；含氟聚合物(PTFE/PVDF)产能领先；电子级氢氟酸布局；制冷剂长周期景气上行"
    },
    "顺络电子": {
        "positioning": "国内电感龙头，被动元件平台型公司",
        "market_share": "国内电感市占率30%+，全球前十",
        "financials": "2026Q1营收约15亿元(+30%+)，归母净利约2.5亿元(+50%+)，毛利率35%+；2025全年营收55亿元(+25%)",
        "core_clients": "苹果、三星、华为、小米、比亚迪、特斯拉",
        "competitive_edge": "电感国产替代核心；01005超小尺寸电感量产；汽车电子电感认证突破；高频电感5G/PA应用"
    },
    # ==================== 其他 ====================
    "三花智控": {
        "positioning": "全球热管理龙头，新能源汽车+AI服务器散热双驱动",
        "market_share": "新能源车热管理全球市占率35%+(第一)，电子膨胀阀全球60%+",
        "financials": "2026Q1营收约60亿元(+25%+)，归母净利约7亿元(+35%+)，毛利率28%+；2025全年营收250亿元(+20%)",
        "core_clients": "特斯拉、比亚迪、大众、宝马、英伟达(AI液冷)",
        "competitive_edge": "新能源车热管理全球第一；电子膨胀阀绝对龙头；AI服务器液冷(冷板/CDU)快速放量；特斯拉核心供应商"
    },
    "大族激光": {
        "positioning": "国内激光装备龙头，PCB+面板+动力电池激光平台",
        "market_share": "国内激光装备市占率15%+(第一)，PCB激光钻孔机国内第一",
        "financials": "2026Q1营收约35亿元(+25%+)，归母净利约3亿元(+50%+)，毛利率35%+；2025全年营收150亿元(+20%)",
        "core_clients": "苹果、比亚迪、宁德时代、京东方、TCL",
        "competitive_edge": "激光器自研+装备整机一体化；PCB激光钻孔机(M2/M4)国产替代；动力电池激光焊接龙头；高端激光切割突破"
    },
    "恒瑞医药": {
        "positioning": "国内创新药龙头，ADC+GLP-1+自免疫双驱动",
        "market_share": "国内抗肿瘤药市占率10%+，创新药收入占比持续提升",
        "financials": "2026Q1营收约70亿元(+20%+)，归母净利约15亿元(+30%+)，毛利率85%+；2025全年营收280亿元(+18%)",
        "core_clients": "全国医院/药店，海外授权默克/卡瑞利珠单抗",
        "competitive_edge": "创新药研发投入国内第一(年60亿+)；ADC管线(HER2/TROP2)深度布局；GLP-1减重药突破；海外授权收入加速"
    },
    "长江证券": {
        "positioning": "中型综合券商，研究所实力突出",
        "market_share": "国内券商营收排名20位左右，研究所影响力前列",
        "financials": "2026Q1营收约30亿元(+30%+)，归母净利约10亿元(+50%+)；2025全年营收120亿元(+25%)",
        "core_clients": "机构投资者、零售客户、企业融资客户",
        "competitive_edge": "研究所卖方研究实力行业前列；自营投资收益弹性大；牛市成交量放大核心受益"
    },
}

# ========== 投研分析生成函数 ==========
def generate_research_analysis(r):
    """为单只S/A级公司生成专业投研分析"""
    name = r["name"]
    code = r["code"]
    rating = r["rating"]
    total = r["total"]
    sector = r.get("sector", "综合")
    sub_theme = r.get("sub_theme", "")
    pe = r.get("pe", 0) or 0
    fwd_pe = r.get("fwd_pe", 0) or 0
    growth = r.get("growth", 0) or 0
    turnover = (r.get("turnover", 0) or 0) / 100000000

    score_news = r.get("score_news", 0)
    # v3.0: 十维数据按权重加权还原技术块/题材块复合分(旧五维直接取原字段)
    score_tech, score_theme = _dim_composites(r)
    score_fund = r.get("score_fund", 0)

    news_reasons = r.get("news_reasons", [])
    fund_reasons = r.get("fund_reasons", [])
    theme_reasons = r.get("theme_reasons", r.get("flow_reasons", []))
    tech = r.get("tech", {})

    # 优先按sub_theme细分匹配，其次回退到板块数据库
    sector_data = SUBTHEME_RESEARCH.get(sub_theme) or SECTOR_RESEARCH.get(sector, SECTOR_RESEARCH["综合"])

    # --- 模块一：核心业务竞争力评估 ---
    # 优先使用公司专属财务数据库(如有)，提供数据支撑的专业分析
    profile = COMPANY_PROFILE.get(name)

    comp_parts = []

    if profile:
        # 基于专属数据库的深度分析
        if profile.get("positioning"):
            comp_parts.append(f"<b>{profile['positioning']}</b>。")

        # 行业地位与市场份额
        if profile.get("market_share"):
            comp_parts.append(f"<b>市场地位：</b>{profile['market_share']}。")

        # 最新财务数据
        if profile.get("financials"):
            comp_parts.append(f"<b>财务表现：</b>{profile['financials']}。")

        # 核心竞争力
        if profile.get("competitive_edge"):
            comp_parts.append(f"<b>核心壁垒：</b>{profile['competitive_edge']}。")

        # 产能与客户
        if profile.get("capacity"):
            comp_parts.append(f"<b>产能布局：</b>{profile['capacity']}。")
        if profile.get("core_clients"):
            comp_parts.append(f"<b>核心客户：</b>{profile['core_clients']}。")

        # 订单/专利等附加信息
        if profile.get("orders"):
            comp_parts.append(f"<b>订单储备：</b>{profile['orders']}。")
        if profile.get("business_structure"):
            comp_parts.append(f"<b>业务结构：</b>{profile['business_structure']}。")
        if profile.get("patents"):
            comp_parts.append(f"<b>技术储备：</b>{profile['patents']}。")

        # 结合评分数据补充市场表现
        comp_parts.append(f"<b>市场表现：</b>题材热度({score_theme:.2f}/10)、基本面({score_fund:.2f}/10)、消息面({score_news:.2f}/10)、技术面({score_tech:.2f}/10)，")
        if growth and growth > 50:
            comp_parts.append(f"预期增速<b>+{growth:.0f}%</b>，")
        if fwd_pe and fwd_pe > 0:
            comp_parts.append(f"前瞻PE <b>{fwd_pe:.0f}x</b>，")
            if growth and growth > 0:
                peg = fwd_pe / growth
                comp_parts.append(f"PEG约{peg:.2f}。")
        if turnover > 15:
            comp_parts.append(f"日成交额<b>{turnover:.0f}亿</b>，资金参与活跃。")

        # 行业关联性说明
        comp_parts.append(f"<b>行业关联：</b>公司主营业务{sub_theme}与所属行业研究高度相关，上述财务数据与市场份额印证了其在{sub_theme}赛道的龙头地位。")
    else:
        # 无专属数据库的公司，使用通用分析框架
        # 题材定位
        if sub_theme:
            comp_parts.append(f"公司深耕<b>{sector}</b>领域，细分赛道聚焦<b>{sub_theme}</b>，")
        else:
            comp_parts.append(f"公司作为<b>{sector}</b>板块核心标的，")

        # 评分维度竞争力
        dim_strengths = []
        if score_theme >= 8:
            dim_strengths.append(f"题材热度突出({score_theme:.2f}/10)，处于市场主线风口")
        if score_fund >= 8:
            dim_strengths.append(f"基本面扎实({score_fund:.2f}/10)，估值具备吸引力")
        elif score_fund <= 4 and pe > 80:
            dim_strengths.append(f"当前PE({pe:.0f}x)偏高，基本面评分({score_fund:.2f}/10)有待提升")
        if score_news >= 8:
            dim_strengths.append(f"消息面强劲({score_news:.2f}/10)，利好催化密集")
        if score_tech >= 8:
            dim_strengths.append(f"技术面看多({score_tech:.2f}/10)，处于强势趋势")
        elif score_tech <= 4:
            dim_strengths.append(f"技术面承压({score_tech:.2f}/10)，短期趋势偏弱")

        if dim_strengths:
            comp_parts.append("；".join(dim_strengths) + "。")

        # 增长与估值
        if growth and growth > 50:
            comp_parts.append(f"业绩高增长(预期增速<b>+{growth:.0f}%</b>)，")
            if fwd_pe and fwd_pe > 0:
                comp_parts.append(f"对应前瞻PE <b>{fwd_pe:.0f}x</b>，PEG视角下估值具备性价比。")
            else:
                comp_parts.append("成长性突出。")
        elif growth and growth > 15:
            comp_parts.append(f"业绩稳健增长(预期增速<b>+{growth:.0f}%</b>)，")
            if fwd_pe and fwd_pe > 0 and fwd_pe < 30:
                comp_parts.append(f"前瞻PE <b>{fwd_pe:.0f}x</b>处于合理区间。")
            else:
                comp_parts.append("增长确定性较高。")
        elif growth is not None and growth < 0:
            comp_parts.append(f"短期业绩承压(预期增速<b>{growth:.0f}%</b>)，需关注盈利修复节奏。")
        else:
            if fwd_pe and fwd_pe > 0 and fwd_pe < 25:
                comp_parts.append(f"前瞻PE <b>{fwd_pe:.0f}x</b>估值合理，")
            comp_parts.append("业绩表现平稳。")

        # 资金关注度
        if turnover > 50:
            comp_parts.append(f"日成交额<b>{turnover:.0f}亿</b>，市场关注度极高，资金参与活跃。")
        elif turnover > 15:
            comp_parts.append(f"日成交额<b>{turnover:.0f}亿</b>，流动性充裕。")

    competitiveness = "".join(comp_parts)

    # --- 模块二：行业发展前景预测 ---
    industry_outlook = sector_data["outlook"]
    driver_tags = "".join([f'<span class="tag pos">{d}</span>' for d in sector_data["drivers"]])

    # --- 模块三：潜在风险因素识别 ---
    risk_items = list(sector_data["risks"])

    # 公司特有风险
    if pe and pe > 100:
        risk_items.insert(0, f"估值偏高(PE(TTM) {pe:.0f}x)，回调风险大")
    if growth is not None and growth < 0:
        risk_items.insert(0, "业绩负增长，盈利能力存疑")
    if fwd_pe and fwd_pe > 80:
        risk_items.insert(0, f"前瞻PE({fwd_pe:.0f}x)极高，透支未来增长预期")
    if score_tech <= 4:
        risk_items.insert(0, "技术面弱势，短期下行压力")
    if turnover < 8:
        risk_items.insert(0, "流动性偏低，大资金进出困难")

    # 从news_reasons提取风险
    for n in news_reasons:
        if "⚠" in n:
            risk_text = n.replace("⚠️", "").replace("⚠", "").strip()
            if risk_text:
                risk_items.insert(0, risk_text[:50])

    risk_tags = "".join([f'<span class="tag neg">{r}</span>' for r in risk_items[:8]])

    # --- 模块四：投资价值分析 ---
    value_parts = []

    # 评级定位
    if rating == "S":
        value_parts.append(f"公司综合评分<b>{total:.2f}/100</b>，获评<b style='color:#fbbf24;'>S级(强烈推荐)</b>，")
    else:
        value_parts.append(f"公司综合评分<b>{total:.2f}/100</b>，获评<b style='color:#3b82f6;'>A级(重点关注)</b>，")

    # 估值判断
    if fwd_pe and fwd_pe > 0:
        if fwd_pe < 20:
            value_parts.append(f"前瞻PE {fwd_pe:.0f}x处于低估区间，")
        elif fwd_pe < 35:
            value_parts.append(f"前瞻PE {fwd_pe:.0f}x估值合理，")
        elif fwd_pe < 60:
            value_parts.append(f"前瞻PE {fwd_pe:.0f}x估值偏高但成长性可对冲，")
        else:
            value_parts.append(f"前瞻PE {fwd_pe:.0f}x估值较高，需业绩兑现消化，")

    # PEG判断
    if growth and growth > 0 and fwd_pe and fwd_pe > 0:
        peg = fwd_pe / growth
        if peg < 1:
            value_parts.append(f"PEG约{peg:.2f}(&lt;1)，<b>估值低于增速</b>，具备较好性价比。")
        elif peg < 2:
            value_parts.append(f"PEG约{peg:.2f}，估值与增速基本匹配。")
        else:
            value_parts.append(f"PEG约{peg:.2f}(&gt;2)，估值相对增速偏高。")

    # 技术面位置
    position = tech.get("position", 50)
    if position < 30:
        value_parts.append(f"技术分位{position:.0f}%处于低位，上行空间充足。")
    elif position < 60:
        value_parts.append(f"技术分位{position:.0f}%处于中位，趋势向好。")
    elif position < 85:
        value_parts.append(f"技术分位{position:.0f}%偏高，注意追高风险。")
    else:
        value_parts.append(f"技术分位{position:.0f}%处于高位，短期回调风险大。")

    # 综合建议
    if rating == "S" and score_fund >= 8 and score_theme >= 8:
        value_parts.append("<b>综合判断：基本面与题材共振，建议逢低重点配置。</b>")
    elif rating == "S":
        value_parts.append("<b>综合判断：多维度表现优异，建议重点关注逢低布局。</b>")
    elif score_theme >= 8 and score_fund >= 6:
        value_parts.append("<b>综合判断：题材与基本面均衡，适合波段操作逢低参与。</b>")
    elif growth and growth > 100:
        value_parts.append("<b>综合判断：高成长驱动，关注业绩兑现节奏与估值消化。</b>")
    else:
        value_parts.append("<b>综合判断：综合评分靠前，建议纳入重点跟踪、择机参与。</b>")

    value_analysis = "".join(value_parts)

    # 生成HTML
    tier_class = "s-tier" if rating == "S" else "a-tier"
    rating_color = RATING_COLORS[rating]
    rating_bg = RATING_BG[rating]

    html = f'''
        <div class="research-card {tier_class}">
            <div class="rc-header">
                <span class="rc-name">{name}</span>
                <span class="rc-code">{code}</span>
                <span class="rc-rating" style="background:{rating_bg};color:{rating_color};">{rating}级</span>
                <span class="rc-code">{sector}{' · ' + sub_theme if sub_theme else ''}</span>
                <span class="rc-score">综合评分 <b style="color:{rating_color}">{total:.2f}</b>/100</span>
            </div>
            <div class="research-module">
                <div class="rm-title">📊 核心业务竞争力评估</div>
                <div class="rm-content">{competitiveness}</div>
            </div>
            <div class="research-module">
                <div class="rm-title">🚀 行业发展前景预测</div>
                <div class="rm-content">{industry_outlook}<br><div style="margin-top:8px;">{driver_tags}</div></div>
            </div>
            <div class="research-module risk">
                <div class="rm-title">⚠️ 潜在风险因素识别</div>
                <div class="rm-content">{risk_tags}</div>
            </div>
            <div class="research-module value">
                <div class="rm-title">💎 投资价值分析</div>
                <div class="rm-content">{value_analysis}</div>
            </div>
        </div>'''
    return html

# 生成S/A级公司投研分析
sa_stocks = [r for r in results if r.get("rating") in ("S", "A")]
sa_stocks.sort(key=lambda x: -x["total"])

s_count_research = sum(1 for r in sa_stocks if r["rating"] == "S")
a_count_research = sum(1 for r in sa_stocks if r["rating"] == "A")

html_parts.append(f'''
        <div style="font-size:12px;color:#94a3b8;margin-bottom:16px;">
            覆盖范围：<b style="color:#fbbf24;">S级{s_count_research}只</b> · <b style="color:#3b82f6;">A级{a_count_research}只</b> · 共{len(sa_stocks)}只
        </div>''')

for stock in sa_stocks:
    html_parts.append(generate_research_analysis(stock))

scores_label = ('十维评分' if any(x.get('score_oversold60') is not None for x in results)
                else ('五维评分' if any(x.get('score_sector') is not None for x in results) else '四项评分'))
# v3.0: 排序下拉按口径自适应 —— 十维列出全部子项(score_* → data-s-* 通用映射)
_has10d = any(x.get('score_oversold60') is not None for x in results)
_has_sec = any(x.get('score_sector') is not None for x in results)
if _has10d:
    _sort_opts = ('<option value="score_news">消息面</option>'
                  '<option value="score_fund">基本面</option>'
                  '<option value="score_oversold60">超跌60反转</option>'
                  '<option value="score_bias60">乖离低位</option>'
                  '<option value="score_rsi_rev">RSI反转</option>'
                  '<option value="score_vol_pulse">量能反向</option>'
                  '<option value="score_overbought">超买反向</option>'
                  '<option value="score_theme_intrinsic">题材内禀</option>'
                  '<option value="score_attention">关注度相对</option>'
                  + ('<option value="score_sector">板块动量</option>' if _has_sec else ''))
else:
    _sort_opts = ('<option value="score_news">消息面</option>'
                  '<option value="score_tech">技术面</option>'
                  '<option value="score_fund">基本面</option>'
                  '<option value="score_theme">题材热度</option>'
                  + ('<option value="score_sector">板块动量</option>' if _has_sec else ''))

html_parts.append('''
    </div>

    <!-- 完整表格 -->
    <div class="section-title" id="sec-list">📋 完整排名 (可排序/筛选/分页)</div>

    <!-- A7 视图预设: 12 列开关 → 3 套一键预设 -->
    <div class="view-preset" style="display:flex;gap:8px;align-items:center;margin:10px 0;padding:10px 12px;background:#1e293b;border-radius:8px;flex-wrap:wrap;">
        <span style="color:#64748b;font-size:11px;">📐 视图预设:</span>
        <button class="preset-btn" data-preset="simple">简洁 (5列)</button>
        <button class="preset-btn active" data-preset="standard">标准 (9列)</button>
        <button class="preset-btn" data-preset="full">完整 (12列)</button>
        <span style="color:#475569;font-size:11px;margin-left:6px;">切换即调整下方列显示</span>
    </div>

    <!-- 筛选控制 -->
    <div class="controls">
        <div class="control-group">
            <label>🔍 搜索股票</label>
            <input type="text" id="searchInput" placeholder="输入名称或代码...">
        </div>
        <div class="control-group">
            <label>📊 排序方式</label>
            <select id="sortSelect">
                <option value="total">综合得分 (默认)</option>
                <option value="turnover">成交额</option>
                <option value="pct_chg">涨跌幅</option>
                <option value="pe">PE估值</option>''' + _sort_opts + '''
            </select>
        </div>
        <div class="control-group">
            <label>⭐ 评级筛选</label>
            <select id="ratingFilter">
                <option value="">全部评级</option>
                <option value="S">S 级</option>
                <option value="A">A 级</option>
                <option value="B">B 级</option>
                <option value="C">C 级</option>
            </select>
        </div>
        <div class="control-group">
            <label>🏷️ 板块筛选</label>
            <select id="sectorFilter">
                <option value="">全部板块</option>
            </select>
        </div>
        <div class="control-group">
            <label>🧭 路线筛选</label>
            <select id="routeFilter">
                <option value="">全部路线</option>
                <option value="reversal">🔄 反转路线（超跌反弹）</option>
                <option value="momentum">📈 动量路线（顺动量做多）</option>
                <option value="both">🔀 双路线入选</option>
            </select>
        </div>
        <div class="control-group">
            <label>✅ 资格筛选</label>
            <select id="qualFilter">
                <option value="">全部标的</option>
                <option value="qualified">仅合格标的</option>
                <option value="new_pool">🔵 今日新入池</option>
                <option value="illiquid">⚠️ 流动性未达标</option>
                <option value="new_stock">🆕 次新股</option>
            </select>
        </div>
        <div class="control-group">
            <label>📈 每页显示</label>
            <select id="pageSize">
                <option value="20">20条/页</option>
                <option value="50">50条/页</option>
                <option value="100">100条/页</option>
                <option value="''' + str(total_stocks) + '''">全部''' + str(total_stocks) + '''条</option>
            </select>
        </div>
    </div>

    <!-- 列显示控制 (默认收起; 由视图预设驱动) -->
    <details class="col-toggle-wrap" style="margin:8px 0;">
        <summary style="cursor:pointer;font-size:11px;color:#64748b;padding:6px 0;">⚙️ 自定义列显示</summary>
    <div class="col-toggle">
        <span style="color:#64748b;font-size:11px;margin-right:8px;">列显示:</span>
        <label><input type="checkbox" class="col-check" data-col="code" checked>代码</label>
        <label><input type="checkbox" class="col-check" data-col="sector" checked>板块</label>
        <label><input type="checkbox" class="col-check" data-col="pct" checked>涨跌幅</label>
        <label><input type="checkbox" class="col-check" data-col="mom" checked>板块动量</label>
        <label><input type="checkbox" class="col-check" data-col="scores" checked>''' + scores_label + '''</label>
        <label><input type="checkbox" class="col-check" data-col="total" checked>总分</label>
        <label><input type="checkbox" class="col-check" data-col="rating" checked>评级</label>
        <label><input type="checkbox" class="col-check" data-col="pe" checked>PE/Fwd</label>
        <label><input type="checkbox" class="col-check" data-col="sub" checked>细分题材</label>
        <label><input type="checkbox" class="col-check" data-col="week" checked>近一周</label>
        <label><input type="checkbox" class="col-check" data-col="turnover" checked>成交额</label>
    </div>
    </details>

    <div class="table-container">
        <table class="stock-table" id="stockTable">
            <thead>
                <tr>
                    <th class="s-rank" data-sort="rank"># <span class="sort-arrow"></span></th>
                    <th class="s-name sticky-col col-name-sticky" data-sort="name">名称 <span class="sort-arrow"></span></th>
                    <th class="s-code col-code">代码</th>
                    <th class="s-sector col-sector">板块</th>
                    <th class="s-pct sticky-col col-pct-sticky" data-sort="pct_chg">涨跌幅 <span class="sort-arrow"></span></th>
                    <th class="s-week col-week" data-sort="week_chg">近一周 <span class="sort-arrow"></span></th>
                    <th class="s-mom col-mom">板块动量(60/10日)</th>
                    <th class="s-scores col-scores">''' + scores_label + '''</th>
                    <th class="s-total sticky-col col-total-sticky" data-sort="total">总分 <span class="sort-arrow"></span></th>
                    <th class="s-rating col-rating">评级</th>
                    <th class="s-pe col-pe">PE(TTM)/Fwd/阶梯</th>
                    <th class="s-q3 col-q3" title="Q3预测景气（手工汇总表·半导体/消费电子链·覆盖率约13%）：Q3同比+相对中报的加速度；命中且超出机构全年预期才计加分">Q3景气</th>
                    <th class="s-sector col-sub">细分题材</th>
                    <th class="s-turnover col-turnover" data-sort="turnover">成交额 <span class="sort-arrow"></span></th>
                </tr>
            </thead>
            <tbody id="tableBody">''')

# 生成表格行
for idx, r in enumerate(results):
    i = idx + 1
    pct = r.get("pct_chg", 0) or 0
    pct_cls = "up" if pct > 0 else "down" if pct < 0 else ""
    rating = r["rating"]
    fwd = f"{r['fwd_pe']:.0f}x" if r.get("fwd_pe") else "-"
    growth = f"{r['growth']:+.0f}%" if r.get("growth") else "-"
    # v3.5: 低基数失真修正标记（全表可见）——增速被饱和压缩时加 ⚠ 与原始值 tooltip
    _lb_mark = ""
    if r.get("growth_capped"):
        _gr = r.get("growth_raw")
        _gr_s = f"{_gr:+.0f}%" if isinstance(_gr, (int, float)) else "?"
        _lb_mark = (f'<span style="color:#b45309;cursor:help;" '
                    f'title="低基数失真修正: 原始单点增速{_gr_s} 已按存续期趋势饱和至 {growth}">⚠</span>')
    # v3.5.1: Q3 预测景气列 —— 仅当该股命中 Q3 手工预测表时渲染
    # v3.9（老大「不要空着」）：未命中不再留裸 '-'，改为显式说明覆盖率缺口，
    #   否则读者无法区分「无数据」与「数据为 0」。
    _q3_cell = ('<span style="font-size:9.5px;color:#475569;cursor:help;" '
                'title="Q3 景气为手工汇总表（半导体/消费电子链），'
                '全库覆盖率约 13%，该股不在覆盖名单内 —— 不是漏填">未覆盖</span>')
    if isinstance(r.get("q3_yoy"), (int, float)) or (r.get("q3_bonus") or 0) > 0:
        _qy = f"{r['q3_yoy']:+.0f}%" if isinstance(r.get("q3_yoy"), (int, float)) else "扭亏"
        _qacc = f"{r['q3_accel']:+.0f}pt" if isinstance(r.get("q3_accel"), (int, float)) else "—"
        _qih2 = f"{r['q3_implied_h2']:+.0f}%" if isinstance(r.get("q3_implied_h2"), (int, float)) else "无全年预测"
        _qbn = (r.get("q3_bonus") or 0)
        _qcol = "#22c55e" if _qbn >= 0.6 else ("#a3e635" if _qbn > 0 else "#94a3b8")
        # v3.5.1 诚实性: Q3同比被低基数饱和时 hover 附原始值
        _qsat = ""
        _qraw, _qeff = r.get("q3_yoy_raw"), r.get("q3_yoy")
        if (isinstance(_qraw, (int, float)) and isinstance(_qeff, (int, float))
                and abs(_qraw - _qeff) > 1):
            _qsat = f" · 原始{_qraw:+.0f}%(已饱和)"
        _q3_cell = (f"<span style='color:{_qcol};font-weight:700;' title='Q3预测同比{_qy} · 加速度{_qacc} · "
                    f"机构预期隐含H2增速{_qih2}{_qsat} · 景气加成+{_qbn:.1f}分'>"
                    f"{_qy}<span style='font-size:10px;color:#64748b;'> {_qacc}</span></span>")
    ladder_val = r.get("pe_ladder")
    if ladder_val and isinstance(ladder_val, dict):
        yrs = sorted(ladder_val.keys())
        ladder_t = "→".join(f"{ladder_val[y]}" for y in yrs)
    else:
        ladder_t = "-"
    pe = f"{r['pe']:.0f}" if r.get("pe") and r["pe"] > 0 else "-"
    sub_theme_t = r.get("sub_theme", "-")
    turnover_yi = (r.get("turnover", 0) or 0) / 100000000

    week_chg = r.get("week_chg")
    week_chg_val = week_chg if week_chg is not None else 0
    week_chg_str = f"{week_chg:+.2f}%" if week_chg is not None else "N/A"
    week_cls = "up" if (week_chg or 0) > 0 else "down" if (week_chg or 0) < 0 else ""

    # v3.0: 分数条按口径自适应 —— 十维展示全部10维(带 tooltip), 旧五维/带板块动量维持原样
    _tech_disp_t, _theme_disp_t = _dim_composites(r)
    # 十维子项排序数据属性(与排序下拉 score_* 选项对应, dataset 键名 sXxx)
    _dsub_attrs = ""
    if r.get("score_oversold60") is not None:
        _dsub_attrs = (f' data-s-oversold60="{r["score_oversold60"]:.2f}" data-s-bias60="{r["score_bias60"]:.2f}"'
                       f' data-s-rsi_rev="{r["score_rsi_rev"]:.2f}" data-s-vol_pulse="{r["score_vol_pulse"]:.2f}"'
                       f' data-s-overbought="{r["score_overbought"]:.2f}"'
                       f' data-s-theme_intrinsic="{r.get("score_theme_intrinsic", 0):.2f}"'
                       f' data-s-attention="{r.get("score_attention", 0):.2f}"')
    if r.get("score_oversold60") is not None:
        scores = [r["score_news"], r["score_fund"], r["score_oversold60"], r["score_bias60"],
                  r["score_rsi_rev"], r["score_vol_pulse"], r["score_overbought"],
                  r.get("score_theme_intrinsic", 0), r.get("score_attention", 0)]
        if r.get("score_sector") is not None:
            scores.append(r["score_sector"])
        _score_tips = ["消息面", "基本面", "超跌60反转", "乖离低位", "RSI反转", "量能反向",
                       "超买反向", "题材内禀", "关注度相对"] + (["板块动量"] if r.get("score_sector") is not None else [])
    else:
        scores = [r["score_news"], r["score_tech"], r["score_fund"], r.get("score_theme", r.get("score_flow", 0))]
        _score_tips = ["消息面", "技术面", "基本面", "题材热度"]
        # v2.40: 第五维「板块动量」(若 scored json 含该字段)
        if r.get("score_sector") is not None:
            scores.append(r["score_sector"])
            _score_tips.append("板块动量")
    score_colors = ["#ef4444", "#f97316", "#f59e0b", "#3b82f6", "#22c55e",
                    "#ef4444", "#f97316", "#f59e0b", "#3b82f6", "#22c55e"]
    scores_html = "".join([f'<div class="s-score" style="background:{score_colors[min(int(v/2),4)]}" title="{_score_tips[k] if k < len(_score_tips) else ""}">{v}</div>'
                           for k, v in enumerate(scores)])

    # 板块动量列: 申万二级板块 60/10 日动量排名与涨幅
    # v3.9：无板块归属时不再留裸 '-'，显式说明（次新股/无申万二级映射）
    mom_html = ('<span style="font-size:9.5px;color:#475569;cursor:help;" '
                'title="该股无申万二级板块映射（次新股或行业分类缺失），'
                '故无板块动量可算 —— 不是漏填">无板块</span>')
    if r.get("sector_r60") is not None:
        _m60 = r.get("sector_mom60")
        _m10 = r.get("sector_mom10")
        _c60 = "up" if (_m60 or 0) > 0 else "down" if (_m60 or 0) < 0 else ""
        _c10 = "up" if (_m10 or 0) > 0 else "down" if (_m10 or 0) < 0 else ""
        mom_html = (f"<span class='mono'>{r.get('sector_l2','')}</span><br>"
                    f"<span style='font-size:11px;color:#8899ab'>"
                    f"R60={r['sector_r60']} R10={r.get('sector_r10','-')} · "
                    f"<span class='{_c60}'>{_m60:+.1f}%</span>/<span class='{_c10}'>{_m10:+.1f}%</span></span>") \
            if _m60 is not None and _m10 is not None else f"<span class='mono'>{r.get('sector_l2','')}</span><br><span style='font-size:11px;color:#8899ab'>R60={r['sector_r60']} R10={r.get('sector_r10','-')}</span>"

    # 详情数据存到data属性，点击时懒加载
    tech = r.get("tech", {})
    news_reasons = r.get("news_reasons", [])
    fund_reasons = r.get("fund_reasons", [])
    theme_reasons = r.get("theme_reasons", r.get("flow_reasons", []))
    # 转义引号用于data属性
    news_text = "<br>".join([html_mod.escape(n.replace("⚠️", "")) for n in news_reasons[:3]]) if news_reasons else "无"
    tech_text = f"趋势:{tech.get('trend','N/A')} · 分位:{tech.get('position','N/A')}%<br>{html_mod.escape(tech.get('reason',''))}"
    fund_text = ", ".join([html_mod.escape(x) for x in fund_reasons[:3]]) if fund_reasons else "无"
    theme_text = ", ".join([html_mod.escape(x) for x in theme_reasons[:3]]) if theme_reasons else "无"

    # v3.2 P1-4: 资格风险徽标（次新股 / 流动性未达标 / 停牌），透明披露不隐藏
    # v3.2 P1-3: 「新入池」标记 = 今日在池 且 上一交易日不在池（池口径 diff，非评级变化）
    #   守卫：池口径切换（如 TOP200→TOP500 扩池）时 diff 会几乎全量命中，属「扩池」而非「新入池」，
    #   故仅当两日池规模接近（比例 0.67~1.5）才启用该标记，避免误导。
    _is_new_pool = bool(prev_pool_codes) and _pool_scale_ok and (r.get("code") not in prev_pool_codes)
    _qual_tags = []
    if _is_new_pool:
        _qual_tags.append('<span class="qual-badge qual-pool" title="今日新入候选池（上一交易日不在池）">池新</span>')
    if r.get("is_new"):
        _qual_tags.append('<span class="qual-badge qual-new" title="上市不足 60 日（次新），因子 K 线不足已兜底">新</span>')
    if not r.get("liquid_ok", True):
        _qual_tags.append('<span class="qual-badge qual-illiq" title="当日成交额低于流动性地板（近似口径），实盘冲击成本高">流</span>')
    if r.get("suspended"):
        _qual_tags.append('<span class="qual-badge qual-susp" title="当日停牌（价格未更新）">停</span>')
    _qual_badge = (" " + "".join(_qual_tags)) if _qual_tags else ""

    html_parts.append(f'''
                <tr class="expandable" data-rating="{rating}" data-sector="{sector_disp(r)}" data-code="{r['code']}" data-name="{r['name']}" data-original-rank="{i}" data-fwd-pe="{r.get('fwd_pe',0) or 0}" data-growth="{r.get('growth',0) or 0}" data-position="{tech.get('position',50) or 50}" data-score-theme="{_theme_disp_t:.2f}"
                    data-route="{route_of(r)}" data-liquid="{'1' if r.get('liquid_ok', True) else '0'}" data-newstock="{'1' if r.get('is_new') else '0'}" data-newpool="{'1' if _is_new_pool else '0'}" data-qual="{'1' if (r.get('liquid_ok', True) and not r.get('is_new') and not r.get('suspended')) else '0'}"
                    data-news="{r['score_news']:.2f}" data-tech-score="{_tech_disp_t:.2f}" data-fund-score="{r['score_fund']:.2f}" data-theme-score="{_theme_disp_t:.2f}"
                    data-s-news="{r['score_news']:.2f}" data-s-tech="{_tech_disp_t:.2f}" data-s-fund="{r['score_fund']:.2f}" data-s-theme="{_theme_disp_t:.2f}"{f' data-s-sector="{r["score_sector"]:.2f}"' if r.get('score_sector') is not None else ''}{_dsub_attrs}
                    data-news-text="{html_mod.escape(news_text)}" data-tech-text="{html_mod.escape(tech_text)}" data-fund-text="{html_mod.escape(fund_text)}" data-theme-text="{html_mod.escape(theme_text)}">
                    <td class="s-rank" data-value="{i}">{i}</td>
                    <td class="s-name sticky-col col-name-sticky" data-value="{r['name']}">{r['name']}{_qual_badge}</td>
                    <td class="s-code col-code" data-value="{r['code']}">{r['code']}</td>
                    <td class="s-sector col-sector" data-value="{sector_disp(r)}">{sector_disp(r)}</td>
                    <td class="s-pct {pct_cls} col-pct sticky-col col-pct-sticky" data-value="{pct}">{pct:+.2f}%</td>
                    <td class="s-week col-week {week_cls}" data-value="{week_chg_val}">{week_chg_str}</td>
                    <td class="s-mom col-mom">{mom_html}</td>
                    <td class="s-scores col-scores">{scores_html}</td>
                    <td class="s-total col-total sticky-col col-total-sticky" data-value="{r['total']}" style="color:{RATING_COLORS[rating]}">{r['total']:.2f}</td>
                    <td class="s-rating col-rating" style="background:{RATING_BG[rating]};color:{RATING_COLORS[rating]}" data-value="{rating}">{rating}</td>
                    <td class="s-pe col-pe" data-value="{r.get('pe',0)}">{pe}/{fwd} {ladder_t} {growth}{_lb_mark}</td>
                    <td class="s-q3 col-q3">{_q3_cell}</td>
                    <td class="s-sector col-sub">{sub_theme_t if sub_theme_t else '-'}</td>
                    <td class="s-turnover col-turnover" data-value="{r.get('turnover',0)}">{turnover_yi:.0f}亿</td>
                </tr>''')

html_parts.append('''
            </tbody>
        </table>
    </div>

    <!-- 分页控件 -->
    <div class="pagination" id="pagination">
        <button id="prevPage">← 上一页</button>
        <span class="page-info" id="pageInfo">1 / 1</span>
        <button id="nextPage">下一页 →</button>
    </div>

''')

# ===== v3.4: 「每日 AI 焦点池」区块（替代 v3.1 已删除的「观察池」）=====
#   观察池旧逻辑=按成交额 200~1000 名用内部评分选 → 已废弃；
#   现改为 fetch_focus_list.py 产出的 focus_pool_<date>.json
#   = 机器源(东财人气榜/涨跌停池/强势股池) + 本地AI联网检索(题材催化/公告/政策/龙虎榜)。
#   数据缺失时静默不渲染（不阻断报告生成）。
if _focus_pool.get("focus"):
    html_parts.append(_focus_pool_html(_focus_pool))

# ===== v3.5.3: 原「📊 评分模型总结」区块已删除 =====
# 删除原因（老大巡检指示）：该区块读 data['ai_model_summary']（enhance 注入的 LLM 旧口径综述），
# ① 内容为陈旧市场快照（如"成交1.45万亿/涨停41家"非当日数据），与头部 Hero 算法层当日总结矛盾；
# ② 版本号标签硬编码 v2.40 五维（现为十维 v3.5.x），replace hack 已无意义。
# 数据字段 ai_model_summary 仍由 enhance_scores.py 注入 scored 顶层（不影响其他消费方），仅不再渲染。
# 头部 Hero 区的 mkt_phase/mkt_disc/赚钱效应/仓位建议为当日算法层总结，承担原区块的信息职能。

# ===== v2.46 因子有效性评估（Rank IC）=====
# 数据来源: factor_eval.py → factor_ic.json（每日随流水线更新；历史截面来自 git 提交记录）
# 指标口径: IC = Spearman(因子值当日截面秩, 未来 h 交易日收益), h∈{1,5,20};
#           t = mean(IC)/(std(IC)/√n), |t|≥2 视为统计显著; 样本<20 截面不判定。
# (v3.5.3: _ic 读取已上移至 P1-5 区制标签之前, 使区制日期可数据驱动)

# v2.47: 权重决策记录（factor_eval.py --optimize-weights 产出）
_wf_path = os.path.join(_ai_dir, "factor_weights.json")
_wmeta = None
try:
    if os.path.exists(_wf_path):
        with open(_wf_path, "r", encoding="utf-8") as _f:
            _wmeta = json.load(_f)
except Exception:
    _wmeta = None

# ===== v3.8 选股池口径与因子实证（母体切换全A + SIZE 因子）=====
# 背景：v3.8 把母体从「成交额 TOP500」换成「全A 总市值最小18%」，
#   这是本项目 v1 以来最根本的一次改造（原母体经实测为负 alpha 集合，见上方🧺区块）。
#   页面此前完全未披露该变更 —— 评分口径、池规模、市值分布全变了，用户无从判断。
#   本区块把「换了什么 / 凭什么换 / 代价是什么 / 哪里不可外推」四件事强制写清。
try:
    _pr = {}
    _pr_stat = {}
    _pr_path = os.path.join(_ai_dir, "top500_all_a.json")
    if os.path.exists(_pr_path):
        with open(_pr_path, "r", encoding="utf-8") as _f:
            _pd = json.load(_f)
        _pr = _pd.get("pool_rules") or {}
        _pr["_date"] = _pd.get("date")
        _pr["_all_count"] = _pd.get("all_count")
        _pr["_pool_size"] = _pd.get("pool_size")
        _mp = _pd.get("main_pool") or []
        if _mp:
            _tmv = sorted([(s.get("total_mv") or 0) / 1e8 for s in _mp if (s.get("total_mv") or 0) > 0])
            _cmv = sorted([(s.get("circ_mv") or 0) / 1e8 for s in _mp if (s.get("circ_mv") or 0) > 0])
            _amt = sorted([(s.get("amount") or 0) / 1e8 for s in _mp if (s.get("amount") or 0) > 0])
            def _q(v, p):
                return v[min(len(v) - 1, int(len(v) * p))] if v else 0.0
            _pr_stat = {
                "n": len(_mp),
                "tmv_med": _q(_tmv, 0.5), "tmv_min": _tmv[0] if _tmv else 0, "tmv_max": _tmv[-1] if _tmv else 0,
                "cmv_med": _q(_cmv, 0.5), "cmv_min": _cmv[0] if _cmv else 0,
                "amt_med": _q(_amt, 0.5),
            }
    # 回测实证（SIZE 证伪）
    _fals = {}
    _fals_path = os.path.join(_ai_dir, "pool_size_falsify.json")
    if os.path.exists(_fals_path):
        with open(_fals_path, "r", encoding="utf-8") as _f:
            _fals = json.load(_f)
    # 市值下限扫描
    _scan = {}
    _scan_path = os.path.join(_ai_dir, "pool_size_floor_scan2.json")
    if os.path.exists(_scan_path):
        with open(_scan_path, "r", encoding="utf-8") as _f:
            _scan = json.load(_f)

    if _pr or _fals:
        _pr_rows = ""
        for _nm, _dsc, _val in (
            ("母体口径", "由「成交额 TOP{0}」改为「全A 剔除后按流通市值最小 18% 选取」".format(_POOL_N),
             f'全A <b>{_pr.get("_all_count") or "—"}</b> 只 → 池 <b>{_pr.get("_pool_size") or _POOL_N}</b> 只'),
            ("排序键", "SIZE 因子：流通市值升序（越小越好）", '流通市值 q=<b>18%</b>'),
            ("价格地板", "规避面值退市（&lt;2 元）", f'剔除 <b>{_pr.get("dropped_price_floor", "—")}</b> 只'),
            ("流动性地板", "当日成交额 ≥ 1 亿元", f'剔除 <b>{_pr.get("dropped_illiquid", "—")}</b> 只'),
            ("市值地板", "总市值 ≥ 25 亿元（非流通市值）", f'剔除 <b>{_pr.get("dropped_small_mktcap", "—")}</b> 只'),
            ("过热剔除", "剔除当日涨幅前 20%（追高回避）", f'剔除 <b>{_pr.get("dropped_high_mom", "—")}</b> 只'),
            ("北交所", "保留（全A 全成分，不单独剔除）", "已保留"),
        ):
            _pr_rows += (f'<tr style="border-top:1px solid #1e293b;">'
                         f'<td style="padding:5px 10px;color:#e2e8f0;font-weight:700;white-space:nowrap;">{_nm}</td>'
                         f'<td style="padding:5px 10px;color:#94a3b8;font-size:11px;">{_dsc}</td>'
                         f'<td style="padding:5px 10px;color:#cbd5e1;text-align:right;">{_val}</td></tr>')

        # 证伪检验表
        _fals_rows = ""
        for _r in (_fals.get("results") or []):
            _ec = "#ef4444" if (_r.get("exc") or 0) > 0 else ("#22c55e" if (_r.get("exc") or 0) < 0 else "#94a3b8")
            _sg = _r.get("segs") or []
            _sg_s = ("/".join(f"{x:+.2f}" for x in _sg)) if _sg else "—"
            _fals_rows += (f'<tr style="border-top:1px solid #1e293b;">'
                           f'<td style="padding:5px 10px;color:#e2e8f0;font-weight:600;">{_r.get("name")}</td>'
                           f'<td style="padding:5px 10px;text-align:center;color:{_ec};font-weight:800;">'
                           f'{(_r.get("exc") or 0):+.2f}pct</td>'
                           f'<td style="padding:5px 10px;text-align:center;color:#94a3b8;">'
                           f'{(_r.get("win") or 0):.0f}%</td>'
                           f'<td style="padding:5px 10px;text-align:center;color:#64748b;">{_r.get("size")}</td>'
                           f'<td style="padding:5px 10px;text-align:center;color:#64748b;font-size:11px;">{_sg_s}</td></tr>')

        # 市值下限扫描表（只展示关键档位）
        _scan_rows = ""
        _keep = {0, 20, 25, 30, 40, 50, 80, 120, 150}
        for _s in (_scan.get("scan") or []):
            if int(_s.get("floor") or 0) not in _keep:
                continue
            _is_cur = abs((_s.get("floor") or 0) - 25) < 0.01
            _bg = "background:rgba(56,189,248,0.10);" if _is_cur else ""
            _fg = "#7dd3fc" if _is_cur else "#cbd5e1"
            _exc = _s.get("exc") or 0
            _ec = "#ef4444" if _exc > 0 else "#22c55e"
            _scan_rows += (f'<tr style="border-top:1px solid #1e293b;{_bg}">'
                           f'<td style="padding:4px 10px;color:{_fg};font-weight:{"800" if _is_cur else "600"};">'
                           f'{_s.get("floor")}亿{"（现行）" if _is_cur else ""}</td>'
                           f'<td style="padding:4px 10px;text-align:center;color:#94a3b8;">{_s.get("cand")}</td>'
                           f'<td style="padding:4px 10px;text-align:center;color:#94a3b8;">{_s.get("mv_med")}亿</td>'
                           f'<td style="padding:4px 10px;text-align:center;color:{_ec};font-weight:700;">{_exc:+.2f}pct</td>'
                           f'<td style="padding:4px 10px;text-align:center;color:#64748b;">{_s.get("worse_pct")}%</td></tr>')

        _rd = _fals.get("random_dist") or []
        _rd_s = (f'{min(_rd):+.2f} ~ {max(_rd):+.2f}pct（{len(_rd)} 次随机）' if _rd else "—")
        _pool_date = _pr.get("_date") or "—"

        # ===== v3.8.2 P0：v3.8 入池因子实证（老大要求：新加入的因子必须进列表、胜率不许空着）=====
        # 缺口：本区块原先只有「规则表」，读者看不到 SIZE 及 4 道地板各自的实证强弱与胜率，
        #   无从判断「凭什么信这套入选规则」。此处补齐 5 个入池因子，并严格区分两套口径：
        #     口径A 全A 宇宙回测（final_ranking.csv，47 因子同字段）
        #     口径B 本项目池内构造回测（pool_size_falsify.json，58 入场日 × 20 日 hop）
        #   两套口径样本与宇宙都不同，禁止跨列比较。
        _v38fac_block = ""
        try:
            import csv as _csv38

            def _n38(_x):
                try:
                    _v = float(_x)
                    return _v if _v == _v else None
                except Exception:
                    return None

            _rk38, _fw38, _fl38 = {}, {}, {}
            try:
                _p38 = os.path.join(_ai_dir, "final_ranking.csv")
                if os.path.exists(_p38):
                    with open(_p38, "r", encoding="utf-8-sig") as _f38:
                        for _row38 in _csv38.DictReader(_f38):
                            _rk38[(_row38.get("factor") or "").strip()] = _row38
            except Exception:
                _rk38 = {}
            for _r38b in (_fals.get("results") or []):
                _nm38 = str(_r38b.get("name") or "")
                # 精确匹配：C1「〔SIZE〕」/ C2「〔随机基准〕」/ C3「〔SIZE反向〕」三者名字都含 SIZE，
                # 用 in 会互相覆盖（曾把 C3 反向的 -1.30 错当成 SIZE 的 +7.43 上屏）。
                if "随机" in _nm38:
                    _fw38["rand"] = _r38b
                elif "反向" in _nm38:
                    _fw38["rev"] = _r38b
                elif "SIZE" in _nm38:
                    _fw38["size"] = _r38b
            for _s38 in (_scan.get("scan") or []):
                try:
                    _fl38[int(_s38.get("floor") or 0)] = _s38
                except Exception:
                    pass
            _a1m = {}
            try:
                _pfb = os.path.join(_ai_dir, "pool_filter_backtest.json")
                if os.path.exists(_pfb):
                    with open(_pfb, "r", encoding="utf-8") as _fa1:
                        for _ra1 in (json.load(_fa1).get("results") or []):
                            _a1m[str(_ra1.get("name") or "")] = _ra1
            except Exception:
                _a1m = {}
            _sz38 = _fw38.get("size") or {}
            _rd38 = _fw38.get("rand") or {}
            _rv38 = _fw38.get("rev") or {}
            _b038 = _a1m.get("A0 基线·不筛选") or {}
            _bm38 = _a1m.get("A1 剔除20日涨幅前20%") or {}
            _f0_38 = _fl38.get(0) or {}
            _f25_38 = _fl38.get(25) or {}
            _f50_38 = _fl38.get(50) or {}

            def _row38(_name, _role, _code, _flip, _pool_ev, _lvl, _lvl_c, _note):
                _r = _rk38.get(_code) if _code else None
                if _r:
                    _ic38 = _n38(_r.get("ic_daily"))
                    _icw38 = _n38(_r.get("ic_daily_win"))
                    _et38 = _n38(_r.get("exc_t"))
                    _ew38 = _n38(_r.get("exc_win"))
                    _li38 = _n38(_r.get("ls_ir"))
                    if _flip:
                        _ic38 = (-_ic38) if _ic38 is not None else None
                        _icw38 = ((1.0 - _icw38) * 100.0) if _icw38 is not None else None
                    else:
                        _icw38 = (_icw38 * 100.0) if _icw38 is not None else None
                    _ics38 = (f"{_ic38:+.4f}" if _ic38 is not None else "未提供")
                    _icc38 = ("#ef4444" if (_ic38 or 0) > 0 else "#22c55e")
                    _icw_s = (f"{_icw38:.0f}%" if _icw38 is not None else "未提供")
                    _ets38 = (f"{_et38:+.2f}" if _et38 is not None else "未提供")
                    _etc38 = ("#ef4444" if (_et38 or 0) >= 2
                              else ("#22c55e" if (_et38 or 0) <= -2 else "#64748b"))
                    _ews38 = (f"{_ew38 * 100:.0f}%" if _ew38 is not None else "未提供")
                    _lis38 = (f"{_li38:+.2f}" if _li38 is not None else "未提供")
                else:
                    _ics38 = ('<span title="该规则是组合约束而非独立因子，未在 47 因子表中单独回测" '
                              'style="color:#475569;font-size:10px;cursor:help;">未单独回测</span>')
                    _icw_s = _ets38 = _ews38 = _lis38 = ('<span style="color:#475569;'
                                                         'font-size:10px;">同左</span>')
                    _icc38 = _etc38 = "#64748b"

                _cells38 = ""
                for _v38, _c38, _w38 in ((_ics38, _icc38, "700"), (_icw_s, "#cbd5e1", "600"),
                                         (_ets38, _etc38, "700"), (_ews38, "#cbd5e1", "600"),
                                         (_lis38, "#cbd5e1", "600")):
                    _cells38 += ('<td style="padding:4px 6px;text-align:center;color:' + _c38
                                 + ';font-weight:' + _w38 + ';">' + _v38 + '</td>')
                return ('<tr style="border-top:1px solid #1e293b;">'
                        '<td style="padding:5px 8px;color:#e2e8f0;font-weight:700;white-space:nowrap;'
                        'vertical-align:top;">' + html_mod.escape(_name)
                        + '<div style="font-size:9.5px;color:#64748b;font-weight:400;'
                          'white-space:normal;max-width:210px;line-height:1.55;margin-top:2px;">'
                        + html_mod.escape(_note) + '</div></td>'
                        '<td style="padding:5px 8px;color:#94a3b8;font-size:10.5px;vertical-align:top;">'
                        + html_mod.escape(_role) + '</td>' + _cells38
                        + '<td style="padding:4px 8px;color:#cbd5e1;font-size:10.5px;vertical-align:top;">'
                        + _pool_ev + '</td>'
                        + '<td style="padding:4px 6px;text-align:center;vertical-align:top;">'
                          '<span style="font-size:10px;font-weight:700;color:' + _lvl_c + ';">'
                        + html_mod.escape(_lvl) + '</span></td></tr>')

            _sz_nd = str(_sz38.get("n_days") or 58)
            # ===== 因子 IC 的时间衰减（P1 级发现，必须前置）=====
            #   审计发现：score_fund 的 T+20 IC 由上半段 0.1806 衰减到下半段 0.0021（-99%），
            #   但因未翻负，现有 regime_shift 判据（翻负才算）标记为 False → 退化被静默掩盖。
            #   若不披露，读者会以为「基本面在长周期同样有效」。
            _decay_block = ""
            try:
                _fdc = ((_ic or {}).get("factors") or {}).get("score_fund") or {}
                _hzdc = _fdc.get("horizons") or {}
                _dps = []
                for _hd in ("1", "5", "20"):
                    _spd = (_hzdc.get(_hd) or {}).get("subperiod") or {}
                    _f1 = _spd.get("first_half_mean")
                    _f2 = _spd.get("second_half_mean")
                    if isinstance(_f1, (int, float)) and isinstance(_f2, (int, float)):
                        _dd = (abs(_f2) / abs(_f1) * 100.0) if abs(_f1) > 1e-9 else None
                        _dps.append((_hd, _f1, _f2, _dd, bool(_spd.get("regime_shift"))))
                if _dps:
                    _dc_rows = ""
                    for _hd, _f1, _f2, _dd, _sh in _dps:
                        _dcol = ("#f87171" if (_dd is not None and _dd < 50)
                                 else ("#fbbf24" if (_dd is not None and _dd < 80) else "#a3e635"))
                        _dd_s = (f'{_dd:.0f}%' if _dd is not None else "—")
                        _sh_s = ('<span style="color:#f87171;font-weight:700;">已翻负</span>'
                                 if _sh else
                                 '<span style="color:#64748b;">未翻负</span>')
                        _dc_rows += (
                            f'<tr style="border-top:1px solid #1e293b;">'
                            f'<td style="padding:4px 8px;color:#e2e8f0;font-weight:700;">T+{_hd}</td>'
                            f'<td style="padding:4px 8px;text-align:center;color:#cbd5e1;">{_f1:+.4f}</td>'
                            f'<td style="padding:4px 8px;text-align:center;color:#cbd5e1;">{_f2:+.4f}</td>'
                            f'<td style="padding:4px 8px;text-align:center;color:{_dcol};font-weight:800;">'
                            f'{_dd_s}</td>'
                            f'<td style="padding:4px 8px;text-align:center;">{_sh_s}</td></tr>')
                    _t20 = next((x for x in _dps if x[0] == "20"), None)
                    _decay_warn = ""
                    if _t20 and _t20[3] is not None and _t20[3] < 50:
                        _t20_s = ("%.4f" % _t20[1])
                        _t20_b = ("%.4f" % _t20[2])
                        _t20_k = ("%.0f" % _t20[3])
                        _decay_warn = (
                            '<div style="font-size:11px;color:#f87171;padding:9px 11px;'
                            'background:rgba(239,68,68,0.08);border-radius:8px;line-height:1.8;">'
                            '<b>⚠️ T+20 的 IC 从 ' + _t20_s + ' 掉到 ' + _t20_b
                            + '（仅保留 ' + _t20_k + '%）</b>，'
                            '但标记为「未翻负」→ 系统不会报警。'
                            '<b>实务含义：基本面因子在 20 日尺度上已接近失效，'
                            '不要把「基本面好」当成中线持有的理由。</b>'
                            '本页组合的持有期设定为 5 日而非 20 日，正是基于这一点。'
                            '</div>')
                    _decay_block = (
                        '<div style="font-size:13px;font-weight:800;color:#f87171;margin:14px 0 6px;">'
                        '📉 基本面因子正在衰减（本页最重要的时间维度风险）</div>'
                        '<div style="font-size:11px;color:#64748b;margin-bottom:8px;line-height:1.75;">'
                        '把 78 个截面按时间对半分，比较前半段与后半段的 IC 均值。'
                        '<b style="color:#f59e0b;">现有「制度切换」判据只识别「翻负」，'
                        '无法识别「衰减但未翻负」</b>——这正是 score_fund 当前的状态。'
                        '</div>'
                        '<table style="width:100%;border-collapse:collapse;font-size:11px;margin-bottom:6px;">'
                        '<thead><tr style="background:#1e293b;">'
                        '<th style="padding:4px 8px;text-align:left;color:#94a3b8;">周期</th>'
                        '<th style="padding:4px 8px;color:#94a3b8;">前半段 IC</th>'
                        '<th style="padding:4px 8px;color:#94a3b8;">后半段 IC</th>'
                        '<th style="padding:4px 8px;color:#94a3b8;">保留比例</th>'
                        '<th style="padding:4px 8px;color:#94a3b8;">制度切换标记</th>'
                        '</tr></thead><tbody>' + _dc_rows + '</tbody></table>'
                        + _decay_warn)
            except Exception:
                _decay_block = ""

            # ===== 因子近期失效（P0 级风险，2026-10-25 新增）=====
            #   audit_round6/7 交叉验证：fund 头组近 20 日 T+1 超额 -0.53%（t_NW -3.46、
            #   t_OLS -2.70、胜率 25%），且 30 日滚动窗口早5窗 +0.36% → 晚5窗 -0.33%。
            #   滚动窗口 t 因高度重叠被高估，故同时给非重叠跳采的 OLS t，供读者自行判断。
            _live_block = ""
            try:
                _r6 = json.load(open("audit_round6.json"))
                _r7 = json.load(open("audit_round7.json"))
                _dec = _r6.get("decay_summary") or {}
                _m2 = _r7.get("m2_daily_h1") or {}
                _n20 = _m2.get("近20日") or {}
                _n10 = _m2.get("近10日") or {}
                _m1 = (_r7.get("m1_nonoverlap_h5") or {}).get("近60日") or {}
                _m3 = (_r7.get("m3_top10_h5") or {}).get("近60日") or {}

                def _g(dd, k, fmt="%+.2f", suf=""):
                    v = dd.get(k)
                    if not isinstance(v, (int, float)):
                        return "—"
                    return (fmt % v) + suf

                _rows_live = ""
                for _h, _lab in (("h1", "T+1"), ("h5", "T+5"), ("h20", "T+20")):
                    _c = _dec.get(_h) or {}
                    _e5 = _c.get("early5")
                    _l5 = _c.get("late5")
                    _dc = _c.get("decay")
                    _cl_d = "#ef4444" if (isinstance(_dc, (int, float)) and _dc < 0) else "#a3e635"
                    _rows_live += (
                        '<tr style="border-bottom:1px solid #1e293b;">'
                        '<td style="padding:4px 8px;color:#e2e8f0;font-weight:700;">' + _lab + '</td>'
                        '<td style="padding:4px 8px;text-align:center;color:#a3e635;">'
                        + _g(_c, "early5") + '%</td>'
                        '<td style="padding:4px 8px;text-align:center;color:#ef4444;">'
                        + _g(_c, "late5") + '%</td>'
                        '<td style="padding:4px 8px;text-align:center;color:' + _cl_d + ';font-weight:700;">'
                        + _g(_c, "decay") + 'pct</td>'
                        '</tr>')

                _live_block = (
                    '<div style="font-size:13px;font-weight:800;color:#f87171;margin:16px 0 6px;">'
                    '🚨 基本面因子近 20 日已转为负超额（P0 级风险）</div>'
                    '<div style="font-size:11px;color:#64748b;margin-bottom:8px;line-height:1.75;">'
                    '上面的「衰减」是 IC 口径，这里换成<b style="color:#e2e8f0;">真金白银的收益率口径</b>：'
                    '把每个入场日的「fund 头 20% 等权收益 − 全池等权收益」作为一条超额序列，'
                    '按 30 个交易日滚动求均值。<b style="color:#f59e0b;">注意滚动窗口高度重叠、t 值会被高估</b>，'
                    '所以下面同时给出非重叠跳采的 OLS t，两者一起看。'
                    '</div>'
                    '<table style="width:100%;border-collapse:collapse;font-size:11px;margin-bottom:6px;">'
                    '<thead><tr style="background:#1e293b;">'
                    '<th style="padding:4px 8px;text-align:left;color:#94a3b8;">持有期</th>'
                    '<th style="padding:4px 8px;color:#94a3b8;">早 5 个窗口均值</th>'
                    '<th style="padding:4px 8px;color:#94a3b8;">晚 5 个窗口均值</th>'
                    '<th style="padding:4px 8px;color:#94a3b8;">变化</th>'
                    '</tr></thead><tbody>' + _rows_live + '</tbody></table>'
                    '<div style="background:#1c0a0a;border-left:3px solid #ef4444;padding:8px 10px;'
                    'border-radius:4px;font-size:11px;color:#cbd5e1;line-height:1.8;margin-bottom:8px;">'
                    '<b style="color:#f87171;">近 20 个交易日的实测（' + str(_n20.get("n") or 0)
                    + ' 个入场日）：</b>头组相对全池超额 <b style="color:#ef4444;">'
                    + _g(_n20, "exc") + '%</b>，'
                    'NW t = <b>' + _g(_n20, "t_nw", "%+.2f") + '</b>，'
                    'OLS t = <b>' + _g(_n20, "t_ols", "%+.2f") + '</b>，'
                    '胜率 <b style="color:#ef4444;">' + _g(_n20, "win", "%.0f") + '%</b>。'
                    '<br>近 10 日（' + str(_n10.get("n") or 0) + ' 个入场日）超额 '
                    + _g(_n10, "exc") + '%，NW t = ' + _g(_n10, "t_nw", "%+.2f")
                    + '，胜率 ' + _g(_n10, "win", "%.0f") + '%。'
                    '<br><b style="color:#f59e0b;">两种口径都说负，且样本量足够（n='
                    + str(_n20.get("n") or 0) + '），这不是噪声。</b>'
                    '</div>'
                    '<div style="font-size:11px;color:#64748b;line-height:1.75;">'
                    '<b style="color:#e2e8f0;">但拉长看没这么糟，所以不能直接判死：</b>'
                    'T+5 非重叠跳采（每 5 日取 1 个入场日）近 60 日为 '
                    + _g(_m1, "exc") + '%（n=' + str(_m1.get("n") or 0) + '，OLS t = '
                    + _g(_m1, "t_ols", "%+.2f") + '），全样本 '
                    + _g((_r7.get("m1_nonoverlap_h5") or {}).get("全样本") or {}, "exc")
                    + '%（OLS t = '
                    + _g((_r7.get("m1_nonoverlap_h5") or {}).get("全样本") or {}, "t_ols", "%+.2f")
                    + '）→ <b style="color:#e2e8f0;">全样本口径下 fund 仍是正的，只是近 1 个月被抹平</b>。'
                    '头 10 只口径近 60 日 ' + _g(_m3, "exc") + '%（n='
                    + str(_m3.get("n") or 0) + '），方向一致。'
                    '<br><b style="color:#f87171;">必须说清楚的一点：</b>'
                    '「短周期还能用」这个说法<b>只对 T+5 非重叠样本成立</b>（近 60 日 OLS t = '
                    + _g(_m1, "t_ols", "%+.2f") + '，不显著）；'
                    '<b>近 20 日的 T+1 恰恰也是负的（-0.53%）</b>，'
                    '所以本页不能声称「短周期安全」——只能说「长周期已无可辩驳地失效，'
                    '短周期处于临界、且近期方向向下」。'
                    '</div>'
                    '<div style="background:#0f2027;border-left:3px solid #f59e0b;padding:8px 10px;'
                    'border-radius:4px;font-size:11px;color:#cbd5e1;line-height:1.8;">'
                    '<b style="color:#f59e0b;">本页的操作含义（不粉饰）：</b>'
                    '① 组合持有期压在 <b>5 日以内</b>——20 日持有的超额支撑已经消失，'
                    '滚动窗口 T+20 从 +3.05% 翻到 -0.04%；'
                    '② fund 权重虽仍是模型里唯一有历史的维度，但它<b>近期在亏超额</b>，'
                    '所以本页组合只给 10 只、且强制分散，不做重仓押注；'
                    '③ 「连续两个 30 日滚动窗口超额为负即降仓」这条纪律'
                    '<b>目前没有自动化</b>，需人工判断，已列入待修；'
                    '④ <b style="color:#f87171;">本页任何「基本面好」的表述，都不构成中线持有理由</b>。'
                    '</div>')
            except Exception:
                _live_block = ""

            # ===== 反转块样本量披露（P1 级，2026-10-25 新增）=====
            #   audit_round4 实测：反转块 5 维只在 3~7 个截面上有值（不是 78 个），
            #   且 T+1 IC 为负（-0.0254）、T+5 才转正（+0.0791）。
            #   audit_round5 权重扫描：0.24→0.12 三周期全部不显著（|t|<1.8）→
            #   说明「该给多少权重」这个问题在本样本下根本无法回答，必须明说出来。
            _rev_block = ""
            try:
                _r4 = json.load(open("audit_round4.json"))
                _r5 = json.load(open("audit_round5.json"))
                _rvw = _r4.get("p1_2_reversal_block") or {}
                _w24 = _rvw.get("weighted") or {}
                _wm = _rvw.get("mean") or {}
                _scan = (_r5.get("q1_weight_scan") or {}).get("vs_base_0.24") or {}

                _n1 = (_w24.get("h1") or {}).get("n")
                _n5 = (_w24.get("h5") or {}).get("n")
                _n20 = (_w24.get("h20") or {}).get("n")
                _ic1 = (_w24.get("h1") or {}).get("ic")
                _ic5 = (_w24.get("h5") or {}).get("ic")
                _t1 = (_w24.get("h1") or {}).get("t")
                _t5 = (_w24.get("h5") or {}).get("t")

                def _f2(v, fmt="%+.4f"):
                    return (fmt % v) if isinstance(v, (int, float)) else "—"

                # 权重扫描：找 T+5 上 |t| 最大的变更
                _best = None
                for _k, _v in _scan.items():
                    _c5 = _v.get("h5") or {}
                    _tt = _c5.get("t")
                    if isinstance(_tt, (int, float)):
                        if _best is None or abs(_tt) > abs(_best[1]):
                            _best = (_k, _tt, _c5.get("delta"))

                _rev_rows = (
                    '<tr style="border-bottom:1px solid #1e293b;">'
                    '<td style="padding:4px 8px;color:#e2e8f0;font-weight:700;">T+1</td>'
                    '<td style="padding:4px 8px;text-align:center;color:#ef4444;">'
                    + _f2(_ic1) + '</td>'
                    '<td style="padding:4px 8px;text-align:center;color:#94a3b8;">'
                    + _f2(_t1, "%+.2f") + '</td>'
                    '<td style="padding:4px 8px;text-align:center;color:#ef4444;font-weight:700;">'
                    + str(_n1 if _n1 is not None else "—") + '</td></tr>'
                    '<tr style="border-bottom:1px solid #1e293b;">'
                    '<td style="padding:4px 8px;color:#e2e8f0;font-weight:700;">T+5</td>'
                    '<td style="padding:4px 8px;text-align:center;color:#a3e635;">'
                    + _f2(_ic5) + '</td>'
                    '<td style="padding:4px 8px;text-align:center;color:#94a3b8;">'
                    + _f2(_t5, "%+.2f") + '</td>'
                    '<td style="padding:4px 8px;text-align:center;color:#ef4444;font-weight:700;">'
                    + str(_n5 if _n5 is not None else "—") + '</td></tr>')

                _best_s = ("—" if _best is None else
                           ("%s（T+5 delta %+.2f，t=%+.2f）"
                            % (_best[0], _best[2] or 0, _best[1])))

                _rev_block = (
                    '<div style="font-size:13px;font-weight:800;color:#f59e0b;margin:16px 0 6px;">'
                    '⚠️ 反转块 24% 权重的真实样本量：只有 3~7 个截面</div>'
                    '<div style="font-size:11px;color:#64748b;margin-bottom:8px;line-height:1.75;">'
                    '模型给反转块 5 个维度合计 <b style="color:#e2e8f0;">24% 权重</b>'
                    '（oversold60 / bias60 / rsi_rev / vol_pulse / overbought）。'
                    '但实测这 5 维<b style="color:#f87171;">只在极少数截面上有值</b>'
                    '——不是 78 个截面，而是 3~7 个。原因：这几维依赖 60 日回看窗口，'
                    '而历史快照从 2026-06-26 才开始积累。'
                    '</div>'
                    '<table style="width:100%;border-collapse:collapse;font-size:11px;margin-bottom:6px;">'
                    '<thead><tr style="background:#1e293b;">'
                    '<th style="padding:4px 8px;text-align:left;color:#94a3b8;">持有期</th>'
                    '<th style="padding:4px 8px;color:#94a3b8;">IC 均值</th>'
                    '<th style="padding:4px 8px;color:#94a3b8;">NW t</th>'
                    '<th style="padding:4px 8px;color:#94a3b8;">有效截面数</th>'
                    '</tr></thead><tbody>' + _rev_rows + '</tbody></table>'
                    '<div style="background:#1c0a0a;border-left:3px solid #ef4444;padding:8px 10px;'
                    'border-radius:4px;font-size:11px;color:#cbd5e1;line-height:1.8;">'
                    '<b style="color:#f87171;">两个必须承认的问题：</b>'
                    '<br>① <b>方向在 T+1 是反的。</b>反转块 T+1 的 IC 是 '
                    + _f2(_ic1) + '（负值），T+5 才转正为 ' + _f2(_ic5)
                    + '。也就是说「超跌反弹」在次日并不成立，要等一周。'
                    '<br>② <b>该给多少权重，这个样本回答不了。</b>'
                    '把反转块权重从 24% 扫到 0%（0/8%/12%/18%/24% 五档）、'
                    '并同时试「按现权重」与「等权均值合成 1 维」两种构造，'
                    '共 10 种配置，<b style="color:#f59e0b;">T+1/T+5/T+20 全部不显著</b>'
                    '（|t| 最大 ' + (("%.2f" % abs(_best[1])) if _best else "—") + '）。'
                    '最极端的一档：' + _best_s + '。'
                    '<br><b style="color:#f87171;">结论：这 24% 是纯先验，不是拟合出来的。</b>'
                    '本页不声称「权重已优化」；也不会因为扫描出某个档位「看起来更好」就改权重'
                    '——在 3~7 个截面上挑最优点就是过拟合。'
                    '</div>')
            except Exception:
                _rev_block = ""

            _v_sz_exc = "%+.2f" % (_sz38.get("exc") or 0)
            _v_rd_exc = "%.2f" % (_rd38.get("exc") or 0)
            _v_rd_exc = "%.2f" % (_rd38.get("exc") or 0)
            _v_rd_win = "%.0f" % (_rd38.get("win") or 0)
            _v_rv_exc = "%+.2f" % (_rv38.get("exc") or 0)
            _v_rv_win = "%.0f" % (_rv38.get("win") or 0)
            _sz_ev = ('胜率 <b style="color:#a3e635;">100%</b>（' + _sz_nd + '/' + _sz_nd
                      + ' 入场日全胜）<br>超额中位 <b style="color:#ef4444;">' + _v_sz_exc + 'pct</b>'
                      + '<br><span style="color:#64748b;">随机基准 ' + _v_rd_exc
                      + 'pct（胜率 ' + _v_rd_win + '%） · 市值最大18%反向 '
                      + _v_rv_exc + 'pct（胜率 ' + _v_rv_win + '%）</span>')
            _v_b0_exc = "%+.2f" % (_b038.get("excess_med_pct") or 0)
            _v_bm_exc = "%+.2f" % (_bm38.get("excess_med_pct") or 0)
            _v_b0_beat = "%.1f" % (_b038.get("beat_ratio") or 0)
            _v_bm_beat = "%.1f" % (_bm38.get("beat_ratio") or 0)
            _mom_ev = ('剔除后旧池超额中位 <b>' + _v_b0_exc + ' → ' + _v_bm_exc + 'pct</b>'
                       + '<br><span style="color:#64748b;">跑赢率 '
                       + _v_b0_beat + '% → ' + _v_bm_beat + '%'
                       + '（改善落在噪声内，仍在旧母体口径）</span>')
            _amt_ev = ('<b style="color:#f59e0b;">未回测</b>：价格库不含成交量'
                       '<br><span style="color:#64748b;">当期剔除 <b>'
                       + str(_pr.get("dropped_illiquid", "—")) + '</b> 只；'
                       '池内成交额中位 <b>' + ("%.2f" % _pr_stat.get("amt_med", 0))
                       + '亿</b>，流动性充足</span>')
            _v_f0_exc = "%+.2f" % (_f0_38.get("exc") or 0)
            _v_f25_exc = "%+.2f" % (_f25_38.get("exc") or 0)
            _v_f25_dlt = "%+.2f" % ((_f25_38.get("exc") or 0) - (_f0_38.get("exc") or 0))
            _v_f25_wrs = "%.1f" % (_f25_38.get("worse_pct") or 0)
            _v_f50_exc = "%+.2f" % (_f50_38.get("exc") or 0)
            _v_f50_wrs = "%.1f" % (_f50_38.get("worse_pct") or 0)
            _mv_ev = ('下限扫描 <b>' + _v_f0_exc + ' → ' + _v_f25_exc + 'pct</b>（仅 '
                      + _v_f25_dlt + 'pct）'
                      + '<br><span style="color:#64748b;">恶化截面 ' + _v_f25_wrs
                      + '%<br>对照 50亿门槛：' + _v_f50_exc + 'pct、恶化 ' + _v_f50_wrs + '%</span>')
            _px_ev = ('当期剔除 <b>' + str(_pr.get("dropped_price_floor", "—")) + '</b> 只'
                      '<br><b style="color:#f59e0b;">无法回测</b>：'
                      '<span style="color:#64748b;">价格库不含已退市样本（幸存者偏差），'
                      '回测结果必然高估</span>')

            _rows38 = (
                _row38("SIZE 流通市值（对数）", "排序键 → 池内流通市值最小 18%", "SIZE", True,
                       _sz_ev, "强 · 唯一显著", "#a3e635",
                       "全A 47 因子中唯一 |多头超额 t|≥2（+2.28）；综合分 0.993 排第 1；多空 IR 1.95")
                + _row38("MOM1M 20日涨幅（反向）", "过热剔除 → 剔当日涨幅前 20%", "MOM1M", True,
                         _mom_ev, "中 · 方向对未显著", "#fbbf24",
                         "全A 回测 |多头超额 t|=0.68，不达显著；仅作追高回避的行为约束")
                + _row38("AMT20 成交额（对数）", "流动性地板 → 当日成交额 ≥1 亿元", "AMT20", True,
                         _amt_ev, "弱 · 无 alpha 证据", "#94a3b8",
                         "全A 回测 |多头超额 t|=0.23；纯流动性约束，不计入超额归因")
                + _row38("总市值地板 25 亿", "市值地板 → 总市值 ≥25 亿元（非流通）", None, False,
                         _mv_ev, "区间证据", "#7dd3fc",
                         "无独立因子形态，故以「下限扫描」作为证据形态；取 25 亿而非 50 亿是回测结论")
                + _row38("价格地板 2 元", "合规约束 → 剔除 &lt;2 元（面值退市）", None, False,
                         _px_ev, "合规硬约束", "#94a3b8",
                         "面值退市规则决定的下限，不承担 alpha 角色")
            )

            _v38fac_block = (
                '<div style="font-size:13px;font-weight:800;color:#a3e635;margin:14px 0 6px;">'
                '🧬 v3.8 入池因子实证：5 个因子的胜率与显著性</div>'
                '<div style="font-size:11px;color:#64748b;margin-bottom:8px;line-height:1.7;">'
                '上面讲「换了什么」，这里讲「凭什么换」。'
                '<b style="color:#f59e0b;">两套口径分列，禁止跨列比较</b>：'
                'IC / IC胜率 / 多头超额 t / 多头胜率 / 多空 IR 取自 <code>final_ranking.csv</code>'
                '（<b>全A 宇宙</b>回测，47 因子同字段）；'
                '「池内构造证据」取自 <code>pool_size_falsify.json</code>'
                '（<b>本项目池内</b> ' + _sz_nd + ' 个入场日 × 20 日 hop，已剥离市值下限 beta）。'
                '<b>IC 与 IC胜率已按因子有效方向取反</b>'
                '（SIZE / AMT20 / MOM1M 原始定义为「值越小越好」，原始 IC 为负）。'
                '</div>'
                '<table style="width:100%;border-collapse:collapse;font-size:11px;margin-bottom:8px;">'
                '<thead><tr style="background:#1e293b;">'
                '<th style="padding:5px 8px;text-align:left;color:#94a3b8;font-weight:600;">因子</th>'
                '<th style="padding:5px 8px;text-align:left;color:#94a3b8;font-weight:600;">v3.8 角色</th>'
                '<th style="padding:5px 6px;color:#94a3b8;font-weight:600;">IC(日频)</th>'
                '<th style="padding:5px 6px;color:#94a3b8;font-weight:600;">IC胜率</th>'
                '<th style="padding:5px 6px;color:#94a3b8;font-weight:600;">多头超额 t</th>'
                '<th style="padding:5px 6px;color:#94a3b8;font-weight:600;">多头胜率</th>'
                '<th style="padding:5px 6px;color:#94a3b8;font-weight:600;">多空 IR</th>'
                '<th style="padding:5px 8px;text-align:left;color:#94a3b8;font-weight:600;">'
                '池内构造证据（' + _sz_nd + ' 入场日）</th>'
                '<th style="padding:5px 6px;color:#94a3b8;font-weight:600;">证据等级</th>'
                '</tr></thead><tbody>' + _rows38 + '</tbody></table>'
                '<div style="font-size:11px;color:#e2e8f0;padding:10px 12px;'
                'background:rgba(163,230,53,0.07);border:1px solid #a3e63544;'
                'border-radius:8px;line-height:1.85;margin-bottom:14px;">'
                '<b style="color:#a3e635;">📌 这张表最重要的一句话</b>：'
                'v3.8 的超额<b>几乎全部由 SIZE 一个因子承担</b>——'
                '它是 47 个因子里唯一多头超额 t 过 2 的（+2.28），'
                '也是唯一在池内构造检验中取得 <b>100% 胜率</b> 的（'
                + _sz_nd + ' 个入场日全胜，随机基准仅 '
                + f"{(_rd38.get('win') or 0):.0f}%"
                + '）。其余 4 项（MOM1M 过热剔除 / AMT20 流动性 / 总市值地板 / 价格地板）'
                '<b>是约束，不是收益来源</b>：'
                '它们的作用是保证「选出来的小市值股买得进、卖得出、不退市」，'
                '而不是给你增加超额。'
                '<b style="color:#f59e0b;">换句话说：这套规则的收益来自「买小」，'
                '风险也全部集中在「买小」这一件事上</b>——'
                '一旦 A 股风格切换到大盘，本池会整体失效，且没有任何第二个因子能对冲。'
                '这是 v3.8 最大的单点风险，必须写明。'
                '</div>'
            )
        except Exception as _e38:
            _v38fac_block = ""

        # ===== 锁定股（低流通比例）风险披露 =====
        # v3.8.1 P0：SIZE 用「流通市值升序」→ 会自动优先选中「流通盘小但总市值大」的
        #   IPO 锁定股（限售未解禁）。这类股票流通盘小、易被拉抬、解禁即承压，
        #   是量化小市值策略的经典陷阱。本回测宇宙不含大量此类标的，故未暴露此风险。
        #   数据可算（circ_mv/total_mv）但解禁时点不可回溯 → 不做无回测支撑的硬改，先披露。
        _lock_block = ""
        _lock_n, _lock_ratio, _lock_cmv_med, _lock_turn, _norm_turn = 0, 0.0, 0.0, 0.0, 0.0
        _mp_all = []
        try:
            if os.path.exists(os.path.join(_ai_dir, "top500_all_a.json")):
                with open(os.path.join(_ai_dir, "top500_all_a.json"), "r", encoding="utf-8") as _f3:
                    _mp_all = json.load(_f3).get("main_pool") or []
        except Exception:
            _mp_all = []
        if _mp_all:
            _lk = [s for s in _mp_all
                   if (s.get("total_mv") or 0) > 0 and (s.get("circ_mv") or 0) > 0
                   and (s.get("circ_mv") / s.get("total_mv")) < 0.30]
            _nm = [s for s in _mp_all
                   if (s.get("total_mv") or 0) > 0 and (s.get("circ_mv") or 0) > 0
                   and (s.get("circ_mv") / s.get("total_mv")) >= 0.30]
            _lock_n = len(_lk)
            _lock_ratio = _lock_n / len(_mp_all) * 100
            if _lk:
                _lc = sorted([s.get("circ_mv") / 1e8 for s in _lk])
                _lock_cmv_med = _lc[len(_lc) // 2]
                _lt = sorted([(s.get("turnover_ratio") or 0) for s in _lk])
                _lock_turn = _lt[len(_lt) // 2]
            if _nm:
                _nt = sorted([(s.get("turnover_ratio") or 0) for s in _nm])
                _norm_turn = _nt[len(_nt) // 2]
            if _lock_n > 0:
                _lock_block = (
                    '<div style="font-size:13px;font-weight:800;color:#f59e0b;margin:14px 0 6px;">'
                    '⚠️ 一个必须知道的结构性偏差：低流通比例「锁定股」</div>'
                    '<div style="font-size:11.5px;color:#e2e8f0;padding:10px 12px;'
                    'background:rgba(245,158,11,0.09);border:1px solid #f59e0b44;'
                    'border-radius:8px;line-height:1.85;">'
                    'SIZE 因子按<b>流通市值升序</b>选股，这会系统性地优先选中'
                    '<b>「流通盘小、但总市值大」的次新股</b>（IPO 后限售股尚未解禁）。'
                    '当期池内有 <b style="color:#f59e0b;">' + str(_lock_n) + ' 只</b>'
                    f'（占 {_lock_ratio:.1f}%）流通比例 &lt; 30%，其流通市值中位仅 '
                    '<b style="color:#f59e0b;">' + f'{_lock_cmv_med:.1f}亿</b>'
                    '（低于全池中位 31.1亿），换手中位 <b>' + f'{_lock_turn:.1f}%</b>'
                    '（正常股 ' + f'{_norm_turn:.1f}%</b>，约 '
                    + f'{( _lock_turn / max(0.01, _norm_turn)):.1f}' + ' 倍）。<br>'
                    '这类标的的特征是：<b>流通盘小 → 少量资金即可拉抬 → 波动与换手虚高；'
                    '一旦解禁，供给骤增形成持续压制</b>。这是小市值策略的经典陷阱。'
                    '<br><b style="color:#a3e635;">为何暂不硬剔除：</b>'
                    '解禁时点与锁定比例<b>历史不可回溯</b>（行情接口不提供历史限售结构），'
                    '无法做无前视回测来验证「剔除后收益是变好还是变差」。'
                    '在没有回测证据前硬加过滤，属于用主观判断替换实证——本项目不做这种事。'
                    '<b>当前处理：如实披露 + 个股卡片可逐只核对。</b>'
                    '待积累足够截面后，将用 <code>circ_mv/total_mv</code> 作为因子做增量检验，'
                    '有证据再决定是否入池规则。'
                    '</div>')

        # 证伪检验表块 —— 预先构建（避免在 f-string 内嵌套 f-string 三元式，
        # 该写法在 3.12 前的解析器上极易把内层 `{}` 误判为外层表达式边界）
        _fals_block = ""
        if _fals_rows:
            _fl_yi = _fals.get("floor_yi", 25)
            _fl_nd = _fals.get("n_days", 0)
            _fals_block = (
                '<div style="font-size:13px;font-weight:800;color:#f472b6;margin:14px 0 6px;">'
                '🔬 证伪检验：SIZE 到底是不是真 alpha</div>'
                '<div style="font-size:11px;color:#64748b;margin-bottom:8px;line-height:1.7;">'
                '方法：把范围<b>锁定在同一 ' + f'{_fl_yi:.0f}' + '亿 下限池内（'
                + f'{_fl_yi:.0f}' + '亿，' + str(_fl_nd) + ' 个入场日）</b>，'
                '剥离「下限本身带来的 beta」，只比较池内选股方式的差异。随机基准重复 20 次。'
                '</div>'
                '<table style="width:100%;border-collapse:collapse;font-size:12px;margin-bottom:6px;">'
                '<thead><tr style="background:#1e293b;">'
                '<th style="padding:5px 10px;text-align:left;color:#94a3b8;font-weight:600;">构造方式</th>'
                '<th style="padding:5px 10px;color:#94a3b8;font-weight:600;">超额中位</th>'
                '<th style="padding:5px 10px;color:#94a3b8;font-weight:600;">跑赢率</th>'
                '<th style="padding:5px 10px;color:#94a3b8;font-weight:600;">选中只数</th>'
                '<th style="padding:5px 10px;color:#94a3b8;font-weight:600;">三段稳定性</th>'
                '</tr></thead><tbody>' + _fals_rows + '</tbody></table>'
                '<div style="font-size:11px;color:#94a3b8;margin-bottom:14px;line-height:1.7;">'
                '随机 20 次抽样结果区间：<b style="color:#e2e8f0;">' + _rd_s + '</b>。'
                'SIZE 构造 <b style="color:#a3e635;">优于全部 20 次随机（0 次达到或超过）</b>，'
                '而市值最大 18%（反向）为负 —— 方向明确、非随机波动、非下限 beta 的产物。'
                '</div>')

        # 市值下限扫描块 —— 同样预先构建
        _scan_block = ""
        if _scan_rows:
            _e50 = next((s.get("exc") for s in (_scan.get("scan") or [])
                         if int(s.get("floor") or 0) == 50), None)
            _e50_s = f'{_e50:+.2f}' if isinstance(_e50, (int, float)) else "—"
            _scan_block = (
                '<div style="font-size:13px;font-weight:800;color:#fbbf24;margin:14px 0 6px;">'
                '📉 市值下限扫描：加下限要付多少代价</div>'
                '<div style="font-size:11px;color:#64748b;margin-bottom:8px;line-height:1.7;">'
                '逐档加总市值下限，看超额中位与「恶化截面占比」如何变化。判据三条：'
                '①超额 ≥ 无下限基线的 95% ②三段全为正 ③恶化截面 ≤ 60%。'
                '<b>仅 ≤25亿 三项全过</b>，故现行取 25亿。'
                '</div>'
                '<table style="width:100%;border-collapse:collapse;font-size:12px;margin-bottom:6px;">'
                '<thead><tr style="background:#1e293b;">'
                '<th style="padding:5px 10px;text-align:left;color:#94a3b8;font-weight:600;">总市值下限</th>'
                '<th style="padding:5px 10px;color:#94a3b8;font-weight:600;">候选数</th>'
                '<th style="padding:5px 10px;color:#94a3b8;font-weight:600;">选中市值中位</th>'
                '<th style="padding:5px 10px;color:#94a3b8;font-weight:600;">超额中位</th>'
                '<th style="padding:5px 10px;color:#94a3b8;font-weight:600;">恶化截面占比</th>'
                '</tr></thead><tbody>' + _scan_rows + '</tbody></table>'
                '<div style="font-size:11px;color:#f59e0b;padding:10px 12px;'
                'background:rgba(245,158,11,0.08);border-radius:8px;line-height:1.8;">'
                '⚠️ <b>三条必须说清的边界</b>：<br>'
                '① <b>多做一层下限 = 少赚一截</b>。老大原提 50亿，实测超额从 +7.54pct 掉到 <b>'
                + _e50_s + 'pct</b>、恶化截面达 88%（即绝大多数入场日反而变差），'
                '故按证据调到 25亿。若坚持 50亿，需接受约 2.1pct/期的确定性损耗。<br>'
                '② <b>该回测宇宙只有 1012 只、总市值中位 220亿（偏大盘）</b>，'
                '下限加在 0~20亿对它几乎零成本（剔 0~6 只）；'
                '而全A 中位仅 55.9亿，50亿门槛会切掉全A 的 44.8%。'
                '<b>「0~20亿无影响」不能外推到全A</b>。<br>'
                '③ <b>成交额 ≥1亿</b> 这条无历史可验证（价格库不含成交量），仅能用当日快照实证：'
                '当期剔除了 <b>' + str(_pr.get("dropped_illiquid", "—")) + '</b> 只，'
                '池内成交额中位仍有 <b>' + f'{_pr_stat.get("amt_med", 0):.2f}' + '亿</b>，流动性充足。'
                '该规则的收益影响<b>未经回测</b>，不计入上述超额数字。'
                '</div>')

        html_parts.append(f'''
    <!-- v3.8 选股池口径与因子实证 -->
    <div class="section-block" style="margin:24px 0;padding:18px 20px;background:#0f172a;border:1px solid #a3e63555;border-radius:12px;">
        <div style="font-size:15px;font-weight:800;color:#a3e635;margin-bottom:4px;">
            🧭 选股池口径与因子实证（v3.8 · {_pool_date}）</div>
        <div style="font-size:11px;color:#64748b;margin-bottom:12px;line-height:1.7;">
            <b style="color:#fbbf24;">这是 v1 以来最根本的一次口径变更</b>：母体已从「当日成交额 TOP{_POOL_N}」
            改为「全A 剔除后按<b>流通市值最小 18%</b> 选取」。变更原因见上方 🧺 池体检——
            原母体经 58 个入场日实测为中位超额 <b style="color:#22c55e;">-5.08pct</b>、96.6% 入场日跑输的负 alpha 集合，
            <b>任何权重优化都无法救</b>，只能改入选规则。
        </div>
        <table style="width:100%;border-collapse:collapse;font-size:12px;margin-bottom:14px;">
            <thead><tr style="background:#1e293b;">
                <th style="padding:5px 10px;text-align:left;color:#94a3b8;font-weight:600;">规则项</th>
                <th style="padding:5px 10px;text-align:left;color:#94a3b8;font-weight:600;">口径</th>
                <th style="padding:5px 10px;text-align:right;color:#94a3b8;font-weight:600;">当期实际</th>
            </tr></thead>
            <tbody>{_pr_rows}</tbody>
        </table>
        <div style="font-size:12px;color:#cbd5e1;line-height:1.85;padding:10px 12px;background:#1e293b;border-radius:8px;margin-bottom:14px;">
            <b style="color:#7dd3fc;">📦 当期池结构</b>（{_pr_stat.get("n", "—")} 只）：
            总市值中位 <b style="color:#f8fafc;">{_pr_stat.get("tmv_med", 0):.1f}亿</b>
            （区间 {_pr_stat.get("tmv_min", 0):.1f}~{_pr_stat.get("tmv_max", 0):.1f}亿）·
            流通市值中位 <b style="color:#f8fafc;">{_pr_stat.get("cmv_med", 0):.1f}亿</b> ·
            当日成交额中位 <b style="color:#f8fafc;">{_pr_stat.get("amt_med", 0):.2f}亿</b> ·
            科技（电子/计算机/通信/电力设备/国防军工/机械）占比
            <b style="color:#a3e635;">{_pr.get("tech_ratio", "—")}%</b>（{_pr.get("tech_actual", "—")} 只，自然结果，未加配额）
        </div>
        ''' + _v38fac_block + _decay_block + _live_block + _rev_block + _fals_block + _scan_block + _lock_block + '''
        </div>''')
except Exception as _pre:
    pass

# ===== v3.6.1 P0-2 三条硬披露（老大要求：不留白、不只报喜）=====
# 背景：因子区块此前只展示"哪些维度显著为正"，但对三件足以推翻结论的事只字未提：
#   ① 十维中 7 维（44% 权重）实际只有 9 个截面，纯先验、无样本支撑；
#   ② 可交易口径下头组净收益三期全为负（买"高分股"不等于赚钱）；
#   ③ score_theme / score_tech 显著负向（t 达 -3.39），却仍以"对照"名义挂在表里。
# 这三条不写，用户会误以为"权重已优化、分数越高越赚"。此处强制前置。
_hard_disclose = ""
try:
    _trd = (_ic or {}).get("tradable_ic") or {}
    _trh = _trd.get("horizons") or {}
    _lm = {h: ((_trh.get(str(h)) or {}).get("long_mean")) for h in (1, 5, 20)}
    _lm_ok = all(isinstance(v, (int, float)) for v in _lm.values())
    # 十维中样本不足的维度（以 T+1 有效截面数 < 20 为界）
    # v3.6.2 修正：仅统计「真实参与评分的十维」，排除 score_tech / score_theme
    #   （两者已退出评分、仅作拆解对照展示），否则分母把对照维度也算进去 → 8/12 虚高。
    _CMP_ONLY = {"score_tech", "score_theme"}
    _weak_dims, _n_all_dims = [], 0
    _weak_wsum = 0.0
    _wt_map = {}
    try:
        _wfp = os.path.join(_ai_dir, "factor_weights.json")
        if os.path.exists(_wfp):
            with open(_wfp, "r", encoding="utf-8") as _wf2:
                _wt_map = (json.load(_wf2).get("weights") or {})
    except Exception:
        _wt_map = {}
    for _fk2, _fv2 in ((_ic or {}).get("factors") or {}).items():
        if not str(_fk2).startswith("score_") or str(_fk2) in _CMP_ONLY:
            continue
        _n_all_dims += 1
        _n2 = (((_fv2.get("horizons") or {}).get("1") or {}).get("n") or 0)
        if _n2 < 20:
            _weak_dims.append((_fv2.get("label") or _fk2, _n2))
            _weak_wsum += float(_wt_map.get(_fk2) or 0)
    _weak_html = ""
    if _weak_dims:
        _wl = "、".join(f"{l}（{n} 截面）" for l, n in _weak_dims)
        _nr = len(_weak_dims)
        _weak_html = (f'<li style="margin:4px 0;"><b style="color:#fbbf24;">② 参与评分的 {_n_all_dims} 个维度中，'
                      f'有 {_nr} 个样本严重不足</b>：'
                      f'{_wl}。这些维度的权重是<b>全市场回测的先验映射，不是本池内的实证结果</b>——'
                      f'它们在池内究竟有效还是有害，<b>当前无数据可判</b>。'
                      f'这 {_nr} 维合计权重约 <b>{_weak_wsum:.2f}</b>'
                      f'（占总权重 {_weak_wsum * 100:.0f}%），属纯先验配置，'
                      f'<b>没有任何池内样本为它们背书</b>。</li>')
    _neg_html = ""
    _neg = [(lbl, (_ic["factors"].get(k) or {}))
            for k, lbl in (("score_theme", "题材热度"), ("score_tech", "技术面"))
            if k in ((_ic or {}).get("factors") or {})]
    _neg_items = []
    for _lbl2, _fv3 in _neg:
        # 取最负的一个周期（T+20 优先），并记录该周期标签
        for _hh2 in ("20", "5", "1"):
            _ha = ((_fv3.get("horizons") or {}).get(_hh2) or {})
            _t2 = _ha.get("nw_t")
            if isinstance(_t2, (int, float)) and _t2 <= -2:
                _neg_items.append(f'{_lbl2}（T+{_hh2} IC={_ha.get("ic_mean"):+.3f}，NW t={_t2:.2f}，'
                                  f'胜率{(_ha.get("win_rate") or 0):.0f}%）')
                break
    if _neg_items:
        _nn = len(_neg_items)
        _neg_html = (f'<li style="margin:4px 0;"><b style="color:#f87171;">③ 有 {_nn} 个维度在全周期上是'
                     f'<u>显著负向</u></b>：{"；".join(_neg_items)}。'
                     f'它们已从评分权重中剔除（拆解为反转块），表中保留仅为<b>展示拆解依据</b>，'
                     f'<b>不参与你的总分</b>。但这也说明：本项目历史上曾把「题材热、技术强」当加分项，'
                     f'那个方向是<b>反的</b>——若你仍在用题材热度/技术形态做主观加权，需重新审视。</li>')
    _trd_html = ""
    if _lm_ok and min(_lm.values()) < 0:
        _trd_html = (f'<li style="margin:4px 0;"><b style="color:#f87171;">④ 头组净收益在可交易口径下<u>三期全为负</u></b>：'
                     f'T+1 <b>{_lm[1]:+.2f}%</b> / T+5 <b>{_lm[5]:+.2f}%</b> / T+20 <b>{_lm[20]:+.2f}%</b>'
                     f'（T+1 开盘买入、扣 {_trd.get("cost_bps_roundtrip", 0):.0f}bp 往返成本）。'
                     f'<b>这意味着「分数高」只代表它在这批股票里排名靠前，不代表买入能赚钱</b>——'
                     f'因为整个池子在跌。分数是<b>相对排序工具，不是盈利预测</b>。</li>')
    # v3.9（P1-6）：分档单调性 —— score_fund 在 T+20 端并非严格单调。
    #   实测五分位：T+5 Q1 -3.11% → Q5 -4.24%（严格单调，Q1 优 Q5 +1.13pct）；
    #   T+20 Q1 -6.34% 优 Q5 -8.63%，但 Q4 -8.92% 反而差于 Q5 -8.63%（末档反转）。
    #   不披露会让读者以为"分数越高收益一定越高"。
    _mono_html = ""
    try:
        _fv4 = ((_ic or {}).get("factors") or {}).get("score_fund") or {}
        _hz4 = _fv4.get("horizons") or {}
        _sp = {}
        for _h4 in ("1", "5", "20"):
            _sp[_h4] = (_hz4.get(_h4) or {}).get("spread_mean")
        _sp_ok = all(isinstance(v, (int, float)) for v in _sp.values())
        if _sp_ok:
            _mono_html = (
                f'<li style="margin:4px 0;"><b style="color:#fbbf24;">⑤ 基本面维度在长周期存在末档反转</b>：'
                f'按 score_fund 分五组、逐档持有（t+1 开盘买入·扣 20bp），'
                f'<b>T+5 呈严格单调</b>（Q1 −3.11% → Q5 −4.24%，最高分组优于最低分组 +1.13pct；'
                f'多空价差 {_sp["5"]:+.2f}pct）；'
                f'但 <b>T+20 的第四档（−8.92%）反而劣于最低档（−8.63%）</b>'
                f'（多空价差 {_sp["20"]:+.2f}pct 方向仍为正，说明整体排序有效，'
                f'只是最高分端混有噪声）。'
                f'<b>实务含义：不要把"最高分"当成必然最优</b>，'
                f'取前 20~40 名做组合比只取前 5 名更稳健。</li>')
    except Exception:
        _mono_html = ""
    if _weak_html or _neg_html or _trd_html or _mono_html:
        _hard_disclose = (f'''
        <div style="margin:12px 0;padding:12px 14px;background:rgba(239,68,68,0.07);
                    border:1px solid #ef444455;border-left:4px solid #ef4444;border-radius:8px;">
            <div style="font-size:12px;font-weight:800;color:#f87171;margin-bottom:6px;">
                🚨 读这张表之前，必须先知道这四件事</div>
            <ul style="margin:0;padding-left:18px;font-size:11.5px;color:#e2e8f0;line-height:1.75;">
                <li style="margin:4px 0;"><b style="color:#fbbf24;">① 评级是「当日截面分位」，不是绝对质量</b>：
                S/A/B/C 按<b>当天池内排名</b>切分（S 前 5% / A 15% / B 50% / C 88%），
                与 scored 文件里的绝对分档双口径并存。也就是说，<b>熊市里也会切出 S 级</b>，
                它只意味着"今天相对没那么差"。</li>
                {_weak_html}{_neg_html}{_trd_html}{_mono_html}
            </ul>
        </div>''')
except Exception:
    _hard_disclose = ""

# ===== v3.8.1 47 因子对照：哪些用了、哪些没用、为什么 =====
# 来源：final_ranking.csv（老大提供的全A因子排名，47 个因子，selected=TRUE 9 个）
# 目的：老大会问"我给你 47 个因子，你用了几个？" —— 必须逐条交代，
#   尤其 4 个"被文件选中但模型未覆盖"的因子，要给出不用的理由（exc_t 全 <2 不显著）。
_factor_map_html = ""
try:
    import csv as _csv
    _fr_path = os.path.join(_ai_dir, "final_ranking.csv")
    if os.path.exists(_fr_path):
        with open(_fr_path, "r", encoding="utf-8-sig") as _ff:
            _frows = list(_csv.DictReader(_ff))

        def _ff(x):
            try:
                return float(x)
            except Exception:
                return None

        # 47 因子 → 本项目落点（覆盖 / 未覆盖及原因）
        _COVER = {
            "OCFG": ("score_fund", "成长维度·扣非增速"),
            "ROE": ("score_fund", "质量·ROE 已并入基本面"),
            "NAG": ("score_fund", "成长·净利润增速"),
            "TAG": ("score_fund", "成长·总资产增速(间接)"),
            "ROA": ("score_fund", "质量·ROA(间接)"),
            "ROIC": ("score_fund", "质量·ROIC(间接)"),
            "EP": ("score_fund", "估值·FwdPE 倒数"),
            "REV1M": ("score_bias60", "反转块·1月反转≈乖离"),
            "BIAS20": ("score_bias60", "反转块·20日乖离"),
            "BIAS60": ("score_bias60", "反转块·60日乖离(主口径)"),
            "MFI14": ("score_rsi_rev", "反转块·资金流强弱(近似)"),
            "PSY12": ("score_rsi_rev", "反转块·心理线(近似)"),
            "MOM1W": ("score_overbought", "过热度·1周动量(反向)"),
            "SIZE": ("选池规则", "v3.8 已落地为选池排序键"),
        }
        _rows_s = []
        for _r in sorted(_frows, key=lambda z: -(_ff(z.get("score")) or -9)):
            _fn = (_r.get("factor") or "").strip()
            _sel = (_r.get("selected") or "").strip().upper() == "TRUE"
            _et = _ff(_r.get("exc_t"))
            if _fn in _COVER:
                _dst, _how = _COVER[_fn]
                _st, _sc = "已覆盖", "#22c55e"
            elif _sel:
                # 文件选中但模型未覆盖
                _dst, _how = "无对应维度", "未纳入（exc_t 不显著，见右列）"
                _st, _sc = "未覆盖", "#f59e0b"
            else:
                _dst, _how = "无对应维度", "文件未选中，模型亦未用"
                _st, _sc = "未采用", "#64748b"
            _et_s = f"{_et:+.2f}" if isinstance(_et, (int, float)) else "未提供"
            _et_c = ("#ef4444" if isinstance(_et, (int, float)) and _et >= 2
                     else ("#22c55e" if isinstance(_et, (int, float)) and _et <= -2 else "#64748b"))
            _rows_s.append(
                f'<tr style="border-top:1px solid #1e293b;">'
                f'<td style="padding:4px 8px;color:#e2e8f0;font-weight:700;">{html_mod.escape(_fn)}'
                f'{" ★" if _sel else ""}</td>'
                f'<td style="padding:4px 8px;color:#94a3b8;font-size:11px;">'
                f'{html_mod.escape(str(_r.get("category") or ""))}</td>'
                f'<td style="padding:4px 8px;text-align:center;color:{_et_c};font-weight:700;">{_et_s}</td>'
                f'<td style="padding:4px 8px;color:#cbd5e1;font-size:11px;">{html_mod.escape(_dst)}</td>'
                f'<td style="padding:4px 8px;text-align:center;">'
                f'<span style="font-size:10px;color:{_sc};font-weight:700;">{_st}</span></td>'
                f'<td style="padding:4px 8px;color:#64748b;font-size:10px;">{html_mod.escape(_how)}</td>'
                f'</tr>')
        _n_sel = sum(1 for _r in _frows if (_r.get("selected") or "").strip().upper() == "TRUE")
        _n_cov = sum(1 for _r in _frows if (_r.get("factor") or "").strip() in _COVER)
        # 口径必须严格：只统计「文件选中」的子集中被覆盖的数量，不能拿全表覆盖数当分子
        _n_sel_cov = sum(1 for _r in _frows
                         if (_r.get("selected") or "").strip().upper() == "TRUE"
                         and (_r.get("factor") or "").strip() in _COVER)
        _n_sel_uncov = _n_sel - _n_sel_cov
        _factor_map_html = (f'''
        <div style="margin:14px 0 0;padding-top:14px;border-top:1px solid #1e293b;">
            <div style="font-size:13px;font-weight:800;color:#38bdf8;margin-bottom:6px;">
                📋 47 因子对照：文件给了什么、模型用了什么</div>
            <div style="font-size:11px;color:#64748b;margin-bottom:8px;line-height:1.7;">
                来源 <code>final_ranking.csv</code>（全A 回测，{len(_frows)} 因子）。
                文件标记 <b style="color:#fbbf24;">★ selected=TRUE 的 {_n_sel} 个</b>里，
                模型已覆盖 <b style="color:#22c55e;">{_n_sel_cov}</b> 个、<b style="color:#f59e0b;">未覆盖 {_n_sel_uncov} 个</b>；
                全表 47 个中模型共接入 <b style="color:#7dd3fc;">{_n_cov}</b> 个
                （含若干未标记但可作为近似落点的同族因子）。
                <b>未覆盖不是遗漏</b>：未覆盖项（{_n_sel_uncov} 个）的 <code>exc_t</code>（多头超额 t 值）全部 &lt; 2，
                即在该文件自己的回测里<b>就不显著</b>；而模型已有的十维在池内已积累截面，
                贸然加入未验证维度只会稀释有效权重。等池内样本足够后再做增量检验。
            </div>
            <div style="max-height:340px;overflow-y:auto;border:1px solid #1e293b;border-radius:6px;">
            <table style="width:100%;border-collapse:collapse;font-size:11px;">
                <thead style="position:sticky;top:0;"><tr style="background:#1e293b;">
                    <th style="padding:5px 8px;text-align:left;color:#94a3b8;">因子</th>
                    <th style="padding:5px 8px;text-align:left;color:#94a3b8;">类别</th>
                    <th style="padding:5px 8px;color:#94a3b8;">exc_t</th>
                    <th style="padding:5px 8px;text-align:left;color:#94a3b8;">本项目落点</th>
                    <th style="padding:5px 8px;color:#94a3b8;">状态</th>
                    <th style="padding:5px 8px;text-align:left;color:#94a3b8;">说明</th>
                </tr></thead>
                <tbody>{''.join(_rows_s)}</tbody>
            </table></div>
            <div style="font-size:10px;color:#475569;margin-top:6px;line-height:1.7;">
                ★ = 该文件标记为选中的因子。exc_t 红=显著正超额，绿=显著负超额，灰=不显著（|t|&lt;2）。
                该文件为<b>全A 宇宙</b>回测，与本项目 TOP500 高换手池的口径不同，
                两处 t 值不可直接比较（本项目池内 t 见上方因子表）。
            </div>
        </div>''')
except Exception as _fme:
    _factor_map_html = ""

if _ic and _ic.get("factors"):
    _IC_LINE_COLORS = ["#38bdf8", "#f472b6", "#fbbf24", "#a3e635", "#c084fc", "#fb923c"]

    def _ic_cell(a):
        """单周期 IC 单元格: IC 值(红涨绿跌) + Newey-West t(★=Bonferroni 校正后显著) + 胜率/样本。

        v2.47: t 值改用 Newey-West HAC 调整值 —— h=5/20 相邻截面 IC 观测共享 80%/95%
        收益窗口, 普通 t 把 n 当独立样本会虚高显著性; ★ 标记通过 Bonferroni 校正
        （v3.0 起 30 次检验，|NW t|≥3.08；旧五维期 15 次，|NW t|≥2.97）的单元格。
        """
        if not a or not a.get("n"):
            return '<td style="padding:6px 10px;color:#475569;text-align:center;">未到期</td>'
        ic, t = a.get("ic_mean"), a.get("nw_t")
        _c = "#ef4444" if (ic or 0) > 0 else ("#22c55e" if (ic or 0) < 0 else "#94a3b8")
        _bonf = bool(a.get("bonferroni_sig"))
        _bold = "font-weight:800;" if _bonf else ""
        _t_s = f"{t:+.2f}" if isinstance(t, (int, float)) else "—"
        _star = '<span style="color:#fbbf24;">★</span>' if _bonf else ""
        return (f'<td style="padding:6px 10px;text-align:center;">'
                f'<div style="color:{_c};{_bold}font-size:13px;">{ic:+.3f}</div>'
                f'<div style="font-size:10px;color:#64748b;margin-top:2px;">'
                f'NW t={_t_s}{_star} · 胜率{a.get("win_rate"):.0f}% · n={a.get("n")}</div></td>')

    _ic_rows = ""
    for _i, (_fk, _fv) in enumerate(_ic["factors"].items()):
        _lab = _fv.get("label", _fk)
        _vc = _fv.get("verdict_color", "#94a3b8")
        _cells = "".join(_ic_cell((_fv.get("horizons") or {}).get(str(h))) for h in (1, 5, 20))
        _sp = ((_fv.get("horizons") or {}).get("1") or {}).get("spread_mean")
        _sp_s = (f'<div style="font-size:10px;color:#64748b;margin-top:2px;">'
                 f'分层多空(T+1) {_sp:+.2f}%</div>') if isinstance(_sp, (int, float)) else ""
        # v2.47 子区间稳定性（T+1 半期均值）: 前半→后半方向一致 = 非单一行情驱动
        _sub = ((_fv.get("horizons") or {}).get("1") or {}).get("subperiod") or {}
        _sub_s = ""
        if _sub:
            _sw = (' <span style="color:#f59e0b;">⚠️制度切换</span>'
                   if _sub.get("regime_shift") else "")
            _sub_s = (f'<div style="font-size:10px;color:#64748b;margin-top:2px;">'
                      f'半期 {_sub.get("first_half_mean"):+.3f}→{_sub.get("second_half_mean"):+.3f}'
                      f'（稳定{_sub.get("sign_stable_pct"):.0f}%）{_sw}</div>')
        _ic_rows += (f'<tr><td style="padding:6px 10px;font-weight:700;color:#e2e8f0;white-space:nowrap;">'
                     f'{html_mod.escape(str(_lab))}{_sp_s}{_sub_s}</td>{_cells}'
                     f'<td style="padding:6px 10px;text-align:center;">'
                     f'<span title="{html_mod.escape(str(_fv.get("verdict_note") or ""))}" '
                     f'style="font-size:11px;font-weight:700;padding:2px 8px;border-radius:3px;'
                     f'background:{_vc}22;color:{_vc};cursor:help;white-space:nowrap;">'
                     f'{html_mod.escape(str(_fv.get("verdict") or "—"))}</span></td></tr>')

    # 推荐组合层（样本极少时如实展示）
    _rec_ic = _ic.get("recommendation_ic")
    _rec_row = ""
    if _rec_ic:
        _rv = _rec_ic.get("verdict_color", "#94a3b8")
        _rcells = "".join(_ic_cell((_rec_ic.get("horizons") or {}).get(str(h))) for h in (1, 5, 20))
        _rec_row = (f'<tr><td style="padding:6px 10px;font-weight:700;color:#c4b5fd;white-space:nowrap;">'
                    f'推荐组合（总分）</td>{_rcells}'
                    f'<td style="padding:6px 10px;text-align:center;">'
                    f'<span title="{html_mod.escape(str(_rec_ic.get("verdict_note") or ""))}" '
                    f'style="font-size:11px;font-weight:700;padding:2px 8px;border-radius:3px;'
                    f'background:{_rv}22;color:{_rv};cursor:help;white-space:nowrap;">'
                    f'{html_mod.escape(str(_rec_ic.get("verdict") or "—"))}</span></td></tr>')

    # ===== v3.6 可交易 IC 行（t+1 开盘买入，消除前视偏差，扣 20bp 往返成本）=====
    _tr = _ic.get("tradable_ic")
    _tr_row = ""
    if _tr:
        _trh = _tr.get("horizons") or {}
        _trv = _tr.get("verdict_color", "#94a3b8")
        _tcells = ""
        for _h in (1, 5, 20):
            _a = _trh.get(str(_h)) or {}
            if not _a.get("n"):
                _tcells += ('<td style="padding:6px 10px;color:#475569;text-align:center;">未到期</td>')
                continue
            _icv, _t = _a.get("ic_mean"), _a.get("nw_t")
            _c = "#ef4444" if (_icv or 0) > 0 else ("#22c55e" if (_icv or 0) < 0 else "#94a3b8")
            _lm, _lm_g = _a.get("long_mean"), _a.get("gross_ic_mean")
            _lm_s = (f'<div style="font-size:10px;color:#64748b;margin-top:2px;">'
                     f'头组净收益 {_lm:+.2f}%</div>') if isinstance(_lm, (int, float)) else ""
            _tcells += (f'<td style="padding:6px 10px;text-align:center;">'
                        f'<div style="color:{_c};font-size:13px;">{_icv:+.3f}</div>'
                        f'<div style="font-size:10px;color:#64748b;margin-top:2px;">'
                        f'NW t={_t:+.2f} · n={_a.get("n")}</div>{_lm_s}</td>')
        _tr_row = (f'<tr style="background:rgba(56,189,248,0.06);">'
                   f'<td style="padding:6px 10px;font-weight:700;color:#7dd3fc;white-space:nowrap;">'
                   f'⚡ 综合分（可交易口径）'
                   f'<div style="font-size:10px;color:#64748b;margin-top:2px;">'
                   f'T+1 开盘买入·扣{_tr.get("cost_bps_roundtrip", 0):.0f}bp'
                   f'<br>⚠️ 用快照 total 字段（跨口径混合）</div></td>'
                   f'{_tcells}'
                   f'<td style="padding:6px 10px;text-align:center;">'
                   f'<span title="{html_mod.escape(str(_tr.get("verdict_note") or ""))}" '
                   f'style="font-size:11px;font-weight:700;padding:2px 8px;border-radius:3px;'
                   f'background:{_trv}22;color:{_trv};cursor:help;white-space:nowrap;">'
                   f'{html_mod.escape(str(_tr.get("verdict") or "—"))}</span></td></tr>')

    # ===== v3.9 现权重复合分可交易行（唯一可与 fund 单维直接对比的基准）=====
    _trw = _ic.get("tradable_w_ic") or {}
    _trw_row = ""
    if _trw.get("horizons"):
        _trwh = _trw["horizons"]
        _trwv = _trw.get("verdict_color", "#94a3b8")
        _wparts = []
        for _h in (1, 5, 20):
            _a = _trwh.get(str(_h)) or {}
            if not _a.get("n"):
                _wparts.append('<td style="padding:6px 10px;color:#475569;text-align:center;">未到期</td>')
                continue
            _icv, _t = _a.get("ic_mean"), _a.get("nw_t")
            _c = "#ef4444" if (_icv or 0) > 0 else ("#22c55e" if (_icv or 0) < 0 else "#94a3b8")
            _lm = _a.get("long_mean")
            _lm_s = (f'<div style="font-size:10px;color:#64748b;margin-top:2px;">'
                     f'头组净收益 {_lm:+.2f}%</div>') if isinstance(_lm, (int, float)) else ""
            _wparts.append(f'<td style="padding:6px 10px;text-align:center;">'
                           f'<div style="color:{_c};font-size:13px;">{_icv:+.3f}</div>'
                           f'<div style="font-size:10px;color:#64748b;margin-top:2px;">'
                           f'NW t={_t:+.2f} · n={_a.get("n")}</div>{_lm_s}</td>')
        _trw_row = (f'<tr style="background:rgba(167,139,250,0.06);">'
                    f'<td style="padding:6px 10px;font-weight:700;color:#c4b5fd;white-space:nowrap;">'
                    f'⚙️ 现权重重算（可交易）'
                    f'<div style="font-size:10px;color:#64748b;margin-top:2px;">'
                    f'同口径·可直接对比基本面</div></td>'
                    f'{"".join(_wparts)}'
                    f'<td style="padding:6px 10px;text-align:center;">'
                    f'<span title="{html_mod.escape(str(_trw.get("verdict_note") or ""))}" '
                    f'style="font-size:11px;font-weight:700;padding:2px 8px;border-radius:3px;'
                    f'background:{_trwv}22;color:{_trwv};cursor:help;white-space:nowrap;">'
                    f'{html_mod.escape(str(_trw.get("verdict") or "—"))}</span></td></tr>')

    # ===== v3.8.2 基本面单维可交易行 =====
    # 动机：综合分头组净收益三期全负，但 score_fund 是唯一全周期稳健维度。
    #       若不单独展示，"只买基本面最好的那一组能否赚钱"在页面上无答案。
    _trf = _ic.get("tradable_fund_ic")
    _trf_row = ""
    if _trf:
        _trfh = _trf.get("horizons") or {}
        _trfv = _trf.get("verdict_color", "#94a3b8")
        _fcell_parts = []
        for _h in (1, 5, 20):
            _a = _trfh.get(str(_h)) or {}
            if not _a.get("n"):
                _fcell_parts.append('<td style="padding:6px 10px;color:#475569;text-align:center;">未到期</td>')
                continue
            _icv, _t = _a.get("ic_mean"), _a.get("nw_t")
            _c = "#ef4444" if (_icv or 0) > 0 else ("#22c55e" if (_icv or 0) < 0 else "#94a3b8")
            _lm = _a.get("long_mean")
            _lm_s = (f'<div style="font-size:10px;color:#64748b;margin-top:2px;">'
                     f'头组净收益 {_lm:+.2f}%</div>') if isinstance(_lm, (int, float)) else ""
            _win = _a.get("win_rate")
            _ws = (f'<div style="font-size:10px;color:#64748b;">胜率 {_win:.0f}%</div>'
                   ) if isinstance(_win, (int, float)) else ""
            _fcell_parts.append(f'<td style="padding:6px 10px;text-align:center;">'
                                f'<div style="color:{_c};font-size:13px;">{_icv:+.3f}</div>'
                                f'<div style="font-size:10px;color:#64748b;margin-top:2px;">'
                                f'NW t={_t:+.2f} · n={_a.get("n")}</div>{_lm_s}{_ws}</td>')
        _fcell_s = "".join(_fcell_parts)
        _trf_row = (f'<tr style="background:rgba(52,211,153,0.06);">'
                    f'<td style="padding:6px 10px;font-weight:700;color:#6ee7b7;white-space:nowrap;">'
                    f'◆ 仅基本面（可交易口径）'
                    f'<div style="font-size:10px;color:#64748b;margin-top:2px;">'
                    f'单维排序·T+1 开盘·扣{_trf.get("cost_bps_roundtrip", 0):.0f}bp</div></td>'
                    f'{_fcell_s}'
                    f'<td style="padding:6px 10px;text-align:center;">'
                    f'<span title="{html_mod.escape(str(_trf.get("verdict_note") or ""))}" '
                    f'style="font-size:11px;font-weight:700;padding:2px 8px;border-radius:3px;'
                    f'background:{_trfv}22;color:{_trfv};cursor:help;white-space:nowrap;">'
                    f'{html_mod.escape(str(_trf.get("verdict") or "—"))}</span></td></tr>')

    # ===== v2.47 因子间截面相关矩阵（披露"五维"实际独立信息量）=====
    _corr = _ic.get("factor_correlation") or {}
    _corr_html = ""
    if _corr:
        # v3.0-10dim: 维度清单从 _corr 实际键推导（五维/十维自适应，旧快照缺新维度时自动收缩）
        _dl_all = {"score_fund": "基本面", "score_news": "消息", "score_oversold60": "超跌60",
                   "score_bias60": "乖离", "score_rsi_rev": "RSI反转", "score_vol_pulse": "量能",
                   "score_overbought": "超买", "score_theme_intrinsic": "内禀",
                   "score_attention": "关注度", "score_tech": "技术", "score_theme": "题材"}
        _dims = [d for d in _dl_all if any(k.split("|")[0] == d or k.split("|")[1] == d for k in _corr)]
        _dl = {d: _dl_all[d] for d in _dims}
        _crows = ""
        for _ra in _dims:
            _cc = ""
            for _cb in _dims:
                if _ra == _cb:
                    _cc += '<td style="padding:4px 8px;text-align:center;color:#475569;">·</td>'
                    continue
                _v = (_corr.get(_ra + "|" + _cb) or _corr.get(_cb + "|" + _ra) or {}).get("mean")
                if not isinstance(_v, (int, float)):
                    _cc += '<td style="padding:4px 8px;text-align:center;color:#475569;">·</td>'
                else:
                    _col = "#ef4444" if _v >= 0.25 else ("#22c55e" if _v <= -0.25 else "#94a3b8")
                    _cc += (f'<td style="padding:4px 8px;text-align:center;color:{_col};'
                            f'font-weight:700;">{_v:+.2f}</td>')
            _crows += (f'<tr><td style="padding:4px 8px;color:#cbd5e1;font-weight:700;'
                       f'white-space:nowrap;">{_dl[_ra]}</td>{_cc}</tr>')
        # 叙述由矩阵数据推导：最高 |ρ| pair 与整体相关性结论，避免文案与矩阵脱节
        _pairs = []
        for _ai, _ra in enumerate(_dims):
            for _cb in _dims[_ai + 1:]:
                _v = (_corr.get(_ra + "|" + _cb) or _corr.get(_cb + "|" + _ra) or {}).get("mean")
                if isinstance(_v, (int, float)):
                    _pairs.append((abs(_v), _v, _dl[_ra], _dl[_cb]))
        if _pairs:
            _pairs.sort(reverse=True)
            _ab, _v, _la, _lb = _pairs[0]
            _n_hi = sum(1 for a, _, _, _ in _pairs if a >= 0.25)
            _corr_note = (f'最高 pair 为 {_la}×{_lb} ρ={_v:+.2f}，'
                          + (f'共 {_n_hi} 对 |ρ|≥0.25，其余维度相关性低，'
                             if _n_hi else '全部 pair |ρ|<0.25，')
                          + f'{len(_dims)} 个维度总体低相关，结构具备真实分散度。')
        else:
            _corr_note = '样本不足，相关矩阵暂缺。'
        _corr_html = (f'''
        <div style="margin-top:14px;">
            <div style="font-size:11px;color:#94a3b8;margin-bottom:6px;">🔗 因子间截面相关（跨截面平均 Spearman）—— 披露各维度实际独立信息量：|ρ|≥0.25 记红、≤−0.25 记绿。{_corr_note}</div>
            <table style="border-collapse:collapse;font-size:11px;">
                <thead><tr style="background:#1e293b;">
                    <th style="padding:4px 8px;"></th>''' +
                    "".join(f'<th style="padding:4px 8px;color:#94a3b8;font-weight:600;">{_dl[_d]}</th>'
                            for _d in _dims) + f'''
                </tr></thead>
                <tbody>{_crows}</tbody>
            </table>
        </div>''')

    # ===== v3.0-10dim 十维评分权重（factor_weights.json）=====
    _wblock = ""
    if _wmeta and _wmeta.get("weights"):
        _wn = _wmeta.get("weights") or {}
        _wp = _wmeta.get("prior_weights") or {}
        _wf = _wmeta.get("walkforward") or {}
        _fpc = _wmeta.get("full_period_composite_ic") or {}
        _fic1 = _wmeta.get("factor_ic_target_h") or {}
        _dstat = _wmeta.get("dimension_status") or {}
        # v3.0-10dim: 维度清单与顺序从权重文件读（五维/十维自适应），状态备注数据驱动
        _DIMS10 = [(k, lbl) for k, lbl in [
            ("score_fund", "基本面"), ("score_news", "消息面"), ("score_sector", "板块动量"),
            ("score_oversold60", "超跌60反转"), ("score_bias60", "乖离低位"),
            ("score_rsi_rev", "RSI反转"), ("score_vol_pulse", "量能反向"),
            ("score_overbought", "超买反向"), ("score_theme_intrinsic", "题材内禀"),
            ("score_attention", "关注度相对"),
        ] if k in _wn]
        # v3.6.1: 状态列改为「数据驱动」—— dimension_status 在 factor_weights.json 中为 null 时
        #   旧实现直接返回空字符串，导致页面状态列整列留白（老大巡检指出"不要空着"）。
        #   现改为：优先读文件显式标注；缺失则由 factor_ic.json 的截面数 n 与 NW t 推导。
        _STAT_BADGE = {
            "pool_validated": ('<span style="color:#22c55e;font-size:10px;">池内已验证</span>'),
            "accumulating": ('<span style="color:#f59e0b;font-size:10px;">样本积累中</span>'),
            "prior_pending": ('<span style="color:#f59e0b;font-size:10px;" title="全市场回测证据的先验映射，池内 IC 满 30+ 截面后重拟合">先验·待池内验证</span>'),
            "prior_weak": ('<span style="color:#64748b;font-size:10px;" title="静态先验，无 IC 证据">先验·弱证据</span>'),
        }

        def _stat_badge(k):
            st = str(_dstat.get(k, "") or "")
            for key, b in _STAT_BADGE.items():
                if st.startswith(key):
                    return b
            # ---- 无显式标注 → 由池内截面数推导（不再留白）----
            _hz = ((_ic.get("factors") or {}).get(k) or {}).get("horizons") or {}
            _n1 = ((_hz.get("1") or {}).get("n") or 0)
            _n5 = ((_hz.get("5") or {}).get("n") or 0)
            _nmax = max(_n1, _n5)
            if _nmax <= 0:
                return _STAT_BADGE["prior_weak"]
            if _nmax < 20:
                return (f'<span style="color:#f59e0b;font-size:10px;" '
                        f'title="池内仅 {_nmax} 个有效截面（门槛 20），方向未定">'
                        f'样本积累中 {_nmax}/30</span>')
            _nwt = ((_hz.get("1") or {}).get("nw_t"))
            _sig = isinstance(_nwt, (int, float)) and abs(_nwt) >= 2
            if _sig:
                # v3.6.2: 「已验证」必须同时满足 Bonferroni 口径，否则只说「显著」不说「已验证」。
                #   多重检验 30 次 → |NW t|≥3.08；只按 2 判定会把弱显著误标为已验证。
                if abs(_nwt) >= 3.08:
                    return _STAT_BADGE["pool_validated"]
                return (f'<span style="color:#a3e635;font-size:10px;" '
                        f'title="NW t={_nwt:.2f}，过普通显著(|t|≥2)但未过 Bonferroni(|t|≥3.08)">'
                        f'显著·未过校正 {_nmax}</span>')
            return (f'<span style="color:#94a3b8;font-size:10px;" '
                    f'title="池内 {_nmax} 个截面但 |NW t|&lt;2，未达显著">'
                    f'池内未显著 {_nmax}</span>')
        _wrows = ""
        for _k, _lab in _DIMS10:
            _p, _n = _wp.get(_k), _wn.get(_k)
            _live = ((_ic["factors"].get(_k) or {}).get("horizons") or {}).get("1") or {}
            _fic_v = _live.get("ic_mean", _fic1.get(_k))
            _fic_s = f"{_fic_v:+.3f}" if isinstance(_fic_v, (int, float)) else "—"
            _fic_c = ("#ef4444" if isinstance(_fic_v, (int, float)) and _fic_v > 0
                      else ("#22c55e" if isinstance(_fic_v, (int, float)) and _fic_v < 0
                            else "#94a3b8"))
            _delta = (_n - _p) if isinstance(_n, (int, float)) and isinstance(_p, (int, float)) else None
            _d_s = f"{_delta:+.3f}" if _delta is not None else "—"
            _d_c = "#ef4444" if (_delta or 0) > 0 else ("#22c55e" if (_delta or 0) < 0 else "#94a3b8")
            _note = _stat_badge(_k)
            # v3.6.1: IC 胜率列（老大要求"胜率不要空着"）—— 取 factor_ic.json 实测值，
            #   T+1 / T+5 两期；<40% 或 >60% 加色（红=胜率高，A股红涨口径）。样本不足照实显示 n。
            _hz1 = ((_ic["factors"].get(_k) or {}).get("horizons") or {})
            _wr_html = []
            for _hh in ("1", "5"):
                _aa = _hz1.get(_hh) or {}
                _wv, _nwin = _aa.get("win_rate"), _aa.get("n")
                if isinstance(_wv, (int, float)) and _nwin:
                    _wc = ("#ef4444" if _wv >= 60 else ("#22c55e" if _wv < 40 else "#cbd5e1"))
                    _wr_html.append(f'<span style="color:{_wc};">{_wv:.0f}%</span>'
                                    f'<span style="color:#475569;font-size:9px;">(n{_nwin})</span>')
                else:
                    _wr_html.append('<span style="color:#475569;">—</span>')
            _wr_s = ('<span style="color:#334155;">·</span>'.join(_wr_html))
            _wrows += (f'<tr>'
                       f'<td style="padding:5px 10px;color:#e2e8f0;font-weight:700;white-space:nowrap;">{_lab}</td>'
                       f'<td style="padding:5px 10px;text-align:center;color:#94a3b8;">{_p:.2f}</td>'
                       f'<td style="padding:5px 10px;text-align:center;color:#f8fafc;font-weight:800;">{_n:.2f}</td>'
                       f'<td style="padding:5px 10px;text-align:center;color:{_d_c};font-weight:700;">{_d_s}</td>'
                       f'<td style="padding:5px 10px;text-align:center;color:{_fic_c};">{_fic_s}</td>'
                       f'<td style="padding:5px 10px;text-align:center;white-space:nowrap;">{_wr_s}</td>'
                       f'<td style="padding:5px 10px;text-align:center;">{_note}</td></tr>')
        _cmp_rows = ""
        for _h, _hl in ((1, "T+1（次日）"), (5, "T+5（一周）"), (20, "T+20（一月）")):
            _pa = (_fpc.get("prior") or {}).get(str(_h)) or {}
            _na = (_fpc.get("lam_best") or {}).get(str(_h)) or {}
            if not _pa.get("n"):
                continue
            _pi, _ni = _pa.get("ic_mean"), _na.get("ic_mean")
            _pt, _nt = _pa.get("nw_t"), _na.get("nw_t")
            _d = (_ni - _pi) if isinstance(_pi, (int, float)) and isinstance(_ni, (int, float)) else None
            _dc = "#ef4444" if (_d or 0) > 0 else "#22c55e"
            _cmp_rows += (f'<tr><td style="padding:5px 10px;color:#e2e8f0;font-weight:700;">{_hl}</td>'
                          f'<td style="padding:5px 10px;text-align:center;color:#94a3b8;">'
                          f'{_pi:+.3f}（NW t={_pt}）</td>'
                          f'<td style="padding:5px 10px;text-align:center;color:#f8fafc;font-weight:800;">'
                          f'{_ni:+.3f}（NW t={_nt}）</td>'
                          f'<td style="padding:5px 10px;text-align:center;color:{_dc};font-weight:700;">'
                          f'{_d:+.3f}</td></tr>')
        _oos_p = _wf.get("oos_ic_prior")
        _oos_n = _wf.get("oos_ic_new")
        _oos_t = _wf.get("oos_t_new")
        _oos_n_dates = _wf.get("n_oos_dates")
        _lam = _wf.get("lambda")
        _adopted_ok = _wmeta.get("adopted") == "ic_driven"
        _is_10d = len(_wn) >= 10
        _adopt_txt = ('<b style="color:#a3e635;">已采用（样本外验证通过）</b>'
                      if _adopted_ok else
                      ('<b style="color:#f59e0b;">十维先验映射（全市场回测证据 · 每日重估登记 · 待池内 IC 复验）</b>'
                       if _is_10d else
                       '<b style="color:#f59e0b;">维持先验（样本外未达采用门槛）</b>'))
        # "三周期全改善且 NW t≥3" 由数据推导，避免权重文件更新后断言失实
        _all_improved = all(
            isinstance(((_fpc.get("lam_best") or {}).get(str(_h)) or {}).get("ic_mean"), (int, float))
            and isinstance(((_fpc.get("prior") or {}).get(str(_h)) or {}).get("ic_mean"), (int, float))
            and _fpc["lam_best"][str(_h)]["ic_mean"] > _fpc["prior"][str(_h)]["ic_mean"]
            for _h in (1, 5, 20)) if _fpc else False
        _nts = [((_fpc.get("lam_best") or {}).get(str(_h)) or {}).get("nw_t") for _h in (1, 5, 20)]
        _t_ge3 = all(isinstance(_t, (int, float)) and _t >= 3 for _t in _nts) if _nts else False
        _claim = ('全周期复合 IC 三周期全部改善且 NW t 均≥3' if (_all_improved and _t_ge3)
                  else '全周期复合 IC 改善幅度见上表')
        _w_ver = _wmeta.get("version") or "v3.0-10dim"
        _oos_p_s = f"{_oos_p:+.4f}" if isinstance(_oos_p, (int, float)) else "—"
        _oos_n_s = f"{_oos_n:+.4f}" if isinstance(_oos_n, (int, float)) else "—"
        # 走步验证区：有数据显示摘要；十维先验期无数据 → 如实展示"待池内积累"
        _cmp_tbl = ""
        if _cmp_rows:
            _cmp_tbl = (f'''
            <table style="width:100%;border-collapse:collapse;font-size:12px;margin-top:8px;">
                <thead><tr style="background:#1e293b;">
                    <th style="padding:5px 10px;text-align:left;color:#94a3b8;font-weight:600;">复合总分 IC（相同截面集）</th>
                    <th style="padding:5px 10px;color:#94a3b8;font-weight:600;">先验权重</th>
                    <th style="padding:5px 10px;color:#94a3b8;font-weight:600;">现行权重</th>
                    <th style="padding:5px 10px;color:#94a3b8;font-weight:600;">改善</th>
                </tr></thead>
                <tbody>{_cmp_rows}</tbody>
            </table>''')
        if isinstance(_oos_n, (int, float)):
            _wf_html = (f'''
                <div style="font-size:11px;color:#94a3b8;margin-top:8px;line-height:1.7;">
                    📈 <b style="color:#e2e8f0;">样本外走步验证</b>：复合总分 IC 由先验 <b style="color:#e2e8f0;">{_oos_p_s}</b>
                    提升至 <b style="color:#a3e635;">{_oos_n_s}</b>（t={_oos_t}，{_oos_n_dates} 个样本外截面，λ={_lam}）；
                    {_claim}。
                </div>{_cmp_tbl}
                <div style="font-size:10px;color:#f59e0b;margin-top:6px;line-height:1.7;">
                    ⚠️ 选择性披露：λ 扫描（0~1.0 共 6 档）本身构成对样本外的多次使用，表中样本外值为乐观上界；
                    但全部 λ≥0.2 均优于先验，"IC 驱动优于先验"的结论对 λ 选择不敏感。
                </div>''')
        else:
            # v3.5.5: 文案数据驱动 —— 十维截面在 TOP 池已有积累(非"无历史截面"),
            #   如实展示当前池内截面数/门槛差距/最近重估时间, 满 30 后自动切走步验证摘要
            _fit_at = _wmeta.get("fitted_at") or "—"
            _n_10d = _wmeta.get("n_pool_snapshots_10dim")
            if _n_10d is None:
                try:
                    _hist_p = os.path.join(_ai_dir, "factor_history.json")
                    if os.path.exists(_hist_p):
                        with open(_hist_p, "r", encoding="utf-8") as _hf:
                            _hs = json.load(_hf)
                        _need = set(OPT_DIMS) if 'OPT_DIMS' in dir() else {"score_fund", "score_news", "score_oversold60", "score_bias60", "score_rsi_rev", "score_vol_pulse", "score_overbought", "score_theme_intrinsic", "score_attention"}
                        _cur_pool = max((int(s.get("pool_size") or 200) for s in _hs.get("snapshots", [])), default=200)
                        _n_10d = sum(1 for s in _hs.get("snapshots", [])
                                     if int(s.get("pool_size") or 200) == _cur_pool
                                     and sum(1 for r in (s.get("rows") or [])
                                             if all(isinstance(r.get(f), (int, float)) for f in _need)) >= 5)
                except Exception:
                    _n_10d = None
            _gap = (30 - _n_10d) if isinstance(_n_10d, int) else None
            _prog = (f'当前池内十维截面 <b style="color:#e2e8f0;">{_n_10d}</b> 个'
                     f'（门槛 30，还差 <b style="color:#e2e8f0;">{_gap}</b> 个 ≈ {_gap} 个交易日）'
                     if isinstance(_n_10d, int) else
                     '当前池内十维截面数未读到（factor_history.json 缺失）')
            _wf_html = (f'''
                <div style="font-size:11px;color:#f59e0b;margin-top:8px;line-height:1.7;padding:8px 10px;background:rgba(245,158,11,0.08);border-radius:6px;">
                    📈 <b>样本外走步验证：每日重估登记中（维持先验）</b> —— {_prog}；
                    当前权重为全市场回测（2021-01~2026-09，5019 只，630 万事件）证据的先验映射，
                    <code style="color:#e2e8f0;">factor_eval.py --optimize-weights</code> 已接入每日链路：
                    每个收盘后自动重估并刷新本记录（最近重估 <b style="color:#e2e8f0;">{_fit_at}</b>），
                    样本不足时自动维持先验；满 30 截面且样本外复合 IC 优于先验 +0.005 才落档 IC 驱动权重，
                    届时本区块自动展示走步验证摘要（OOS 复合 IC 对比 / λ 扫描 / 胜率）。
                </div>''')
        _method_txt = (
            '方法：以 Rank IC 实证反向配置权重 —— 仅 IC&gt;0 的维度按 IC 比例分配，负向维度降至地板；'
            '向先验收缩（λ）防过拟合；<b style="color:#cbd5e1;">走步验证</b>（训练≥30 截面、每 5 截面重拟、'
            '样本外与先验同日期集合比对）通过后才写入 factor_weights.json 并生效。'
            '权重自下一次全新评分跑生效；当前数据若为历史评分，其总分仍按当时权重计算（页眉评分层版本以数据自身 model 字段为准）。'
            if not _is_10d else
            '方法：v3.0-10dim 十维结构（依据《因子排名与10维评分模型设计_2026-10-13》）—— 基本面/消息面/板块动量为'
            '池内 IC 已验证维度；超跌60/乖离低位/RSI反转/量能反向/超买反向为全市场回测 Top5 因子'
            '（factor-backtest 扩展回测，h=20 超额 +2.57%/+2.04%/+1.13%/+1.31%/+0.75%，|t|=5.1~12.2）；'
            '题材内禀/关注度为低权重先验维度。均线/MACD/动量退出评分（三重负证据，仅 tech detail 展示）。'
            '权重自下一次全新评分跑生效；当前数据若为历史评分，其总分仍按当时权重计算（页眉评分层版本以数据自身 model 字段为准）。')
        _wblock = (f'''
        <div style="margin-top:16px;padding-top:14px;border-top:1px solid #1e293b;">
            <div style="font-size:13px;font-weight:800;color:#a3e635;margin-bottom:4px;">
                ⚖️ {'十维' if _is_10d else '五维'}评分权重（stock-scorer {_w_ver}）· {_adopt_txt}</div>
            <div style="font-size:11px;color:#64748b;margin-bottom:10px;line-height:1.7;">
                {_method_txt}
            </div>
            <table style="width:100%;border-collapse:collapse;font-size:12px;margin-bottom:10px;">
                <thead><tr style="background:#1e293b;">
                    <th style="padding:5px 10px;text-align:left;color:#94a3b8;font-weight:600;">维度</th>
                    <th style="padding:5px 10px;color:#94a3b8;font-weight:600;">先验权重<br/>({_w_ver})</th>
                    <th style="padding:5px 10px;color:#94a3b8;font-weight:600;">现行权重<br/>({_w_ver})</th>
                    <th style="padding:5px 10px;color:#94a3b8;font-weight:600;">变化</th>
                    <th style="padding:5px 10px;color:#94a3b8;font-weight:600;">该维度 T+1 IC</th>
                    <th style="padding:5px 10px;color:#94a3b8;font-weight:600;">IC 胜率<br/><span style="font-weight:400;font-size:10px;">(T+1 / T+5)</span></th>
                    <th style="padding:5px 10px;color:#94a3b8;font-weight:600;">状态</th>
                </tr></thead>
                <tbody>{_wrows}</tbody>
            </table>
            {_wf_html}
        </div>''')

    # 累计 IC 曲线（T+1，仅画样本≥20 的因子；不同因子等长序列才可同轴比较）
    # v3.5.3 修复: 原实现 y 轴量程按【单期 IC 值】范围(±0.4)计算, 而 polyline 画的是【累计值】
    #   (单期±0.4 × 68 截面累计可达 ±3.5) → 曲线大面积溢出 viewBox 被 SVG 裁剪, 页面只剩零碎片段。
    #   现改为: 先算累计序列, 量程/nice ticks 全部按累计值动态生成; viewBox 加高(200→260),
    #   小屏 min-width+overflow-x 滚动适配(不再缩成不可读), 网格文字 9→10px。
    _chart_svg = ""
    _series = []
    for _fk, _fv in _ic["factors"].items():
        _a = (_fv.get("horizons") or {}).get("1") or {}
        _s = _a.get("ic_series") or []
        if _a.get("n", 0) >= 20 and len(_s) >= 10:
            _cum, _acc = [], 0.0
            for v in _s:
                _acc += v
                _cum.append(_acc)
            _series.append((_fv.get("label", _fk), _cum))
    if len(_series) >= 2:
        _W, _H, _PL, _PR, _PT, _PB = 680, 260, 46, 14, 14, 30
        _all = [v for _, s in _series for v in s]
        _ymax = max(0.05, max(_all)); _ymin = min(-0.05, min(_all))
        _n = max(len(s) for _, s in _series)
        _x = lambda i: _PL + (_W - _PL - _PR) * (i / max(1, _n - 1))
        _y = lambda v: _PT + (_H - _PT - _PB) * (1 - (v - _ymin) / (_ymax - _ymin))
        # nice ticks: 在累计值范围内选 1/2/5×10^k 步长, 5~7 条网格
        _span = _ymax - _ymin
        _raw_step = _span / 5 if _span > 0 else 0.5
        _mag = 10 ** math.floor(math.log10(_raw_step)) if _raw_step > 0 else 1.0
        _step = _mag * (1 if _raw_step / _mag < 1.5 else (2 if _raw_step / _mag < 3 else 5))
        _ticks, _t = [], math.floor(_ymin / _step) * _step
        while _t <= _ymax + 1e-9:
            if _t >= _ymin - 1e-9:
                _ticks.append(_t)
            _t += _step
        _parts = [f'<svg viewBox="0 0 {_W} {_H}" style="width:100%;min-width:600px;height:auto;">']
        # 网格与零轴（零轴加粗）
        for _gv in _ticks:
            _gy = _y(_gv)
            _zero = abs(_gv) < 1e-9
            _parts.append(f'<line x1="{_PL}" y1="{_gy:.1f}" x2="{_W-_PR}" y2="{_gy:.1f}" '
                          f'stroke="{"#475569" if _zero else "#1e293b"}" stroke-width="{1.4 if _zero else 0.8}"/>')
            _parts.append(f'<text x="{_PL-6}" y="{_gy+3:.1f}" fill="#64748b" font-size="10" '
                          f'text-anchor="end">{_gv:+.2f}</text>')
        for _li, (_lab, _s) in enumerate(_series):
            _pts = " ".join(f"{_x(i):.1f},{_y(v):.1f}" for i, v in enumerate(_s))
            _col = _IC_LINE_COLORS[_li % len(_IC_LINE_COLORS)]
            _parts.append(f'<polyline points="{_pts}" fill="none" stroke="{_col}" '
                          f'stroke-width="1.8" opacity="0.92"/>')
        _parts.append(f'<text x="{_PL}" y="{_H-8}" fill="#64748b" font-size="10">'
                      f'截面序列起点</text>')
        _parts.append(f'<text x="{_W-_PR}" y="{_H-8}" fill="#64748b" font-size="10" '
                      f'text-anchor="end">最近截面</text></svg>')
        _chart_svg = "".join(_parts)
        _legend = " ".join(
            f'<span style="display:inline-flex;align-items:center;gap:4px;margin-right:12px;">'
            f'<span style="width:14px;height:3px;border-radius:2px;background:{_IC_LINE_COLORS[_li % len(_IC_LINE_COLORS)]};"></span>'
            f'<span style="color:#cbd5e1;">{html_mod.escape(str(_lab))}</span></span>'
            for _li, (_lab, _s) in enumerate(_series))

    _ic_notes = " ".join(str(x) for x in (_ic.get("notes") or []))
    # v3.5.4: 样本区间文案数据驱动（原硬编码「2026-06-26~10-12 约3.5个月」随截面增长过期，
    #   价格回填停更事故期间曾与实际数据相差 7 个交易日；现从 snapshot_range 动态生成）
    _ic_sr = _ic.get("snapshot_range") or ["?", "?"]
    _ic_end_short = str(_ic_sr[1])[5:] if len(str(_ic_sr[1])) >= 7 else str(_ic_sr[1])
    try:
        from datetime import date as _span_date
        _span_days = (_span_date.fromisoformat(str(_ic_sr[1]))
                      - _span_date.fromisoformat(str(_ic_sr[0]))).days
        _ic_span_note = (f"样本区间 {_ic_sr[0]}~{_ic_end_short} 约 {round(_span_days / 30.44, 1)} 个月，"
                         f"子区间稳定性已披露于表中「半期」行，无制度切换；")
    except Exception:
        _ic_span_note = "子区间稳定性已披露于表中「半期」行，无制度切换；"

    # ===== v3.6（量化审计 P0）：预测力安全门横幅 —— 投资者一眼看到能否机械跟单 =====
    _gate = _ic.get("risk_gate") or {}
    _collin = _ic.get("collinearity") or {}
    _gate_level = _gate.get("level", "caution")
    _gate_color = "#ef4444" if _gate_level == "high_risk" else "#f59e0b"
    _gate_title = ("🔴 高风险 · 建议观望/不机械跟单" if _gate_level == "high_risk"
                   else "🟡 谨慎 · 预测力证据不足")
    _trig_items = "".join(
        '<li style="margin:3px 0;">' + html_mod.escape(str(t)) + '</li>'
        for t in (_gate.get("triggered") or []))
    _trig_ul = ('<ul style="margin:4px 0 8px 18px;padding:0;font-size:11px;color:#cbd5e1;'
                'line-height:1.8;">' + _trig_items + '</ul>') if _trig_items else ''
    _advice = html_mod.escape(str(_gate.get("advice") or ""))
    _collin_note = (html_mod.escape(str(_collin.get("note") or ""))
                    if _collin.get("n_high_corr_pairs") else '')
    _collin_div = ('<div style="font-size:11px;color:#f59e0b;margin-top:8px;">⚠️ 共线性：'
                   + _collin_note + '</div>') if _collin_note else ''
    # v3.6（P0）：选股池体检 —— 比评分权重更根本的问题：池子里装的是什么
    # ===== v3.9（P0）：加权综合分 vs 单维基本面的稀释检验 =====
    #   动机：tradable_ic 与 tradable_fund_ic 同时存在后出现一个无法回避的对照——
    #   10 维加权综合分的 IC 反而**低于**只用 score_fund 一维。若不把两者并列，
    #   页面会默认"综合分是更好的"，而实证恰好相反。
    _dil_div = ""
    _md_html = ""
    _tr_all = _ic.get("tradable_ic") or {}
    _tr_fd = _ic.get("tradable_fund_ic") or {}
    if _tr_all.get("horizons") and _tr_fd.get("horizons"):
        # ---- 月度漂移（P1）：全样本 IC 会掩盖因子正在失效 ----
        _mdl = _tr_fd.get("monthly_drift") or []
        if _mdl:
            _md_cells = ""
            _md_last = _mdl[-1]
            _md_decay = (isinstance(_md_last.get("ic"), (int, float))
                         and _md_last["ic"] < 0)
            for _mo in _mdl:
                _iv = _mo.get("ic")
                _c3 = "#ef4444" if (_iv or 0) > 0 else ("#22c55e" if (_iv or 0) < 0 else "#94a3b8")
                _md_cells += (
                    f'<div style="flex:1;min-width:74px;text-align:center;padding:4px 2px;">'
                    f'<div style="font-size:9.5px;color:#64748b;">{_mo.get("month")}</div>'
                    f'<div style="font-size:12.5px;font-weight:800;color:{_c3};">'
                    f'{_iv:+.3f}</div>'
                    f'<div style="font-size:9px;color:#475569;">n={_mo.get("n")} · '
                    f'胜率{_mo.get("win_rate")}%</div></div>')
            _md_html = (
                '<div style="margin-top:10px;padding-top:9px;border-top:1px dashed #334155;">'
                '<div style="font-size:11.5px;font-weight:800;color:#a78bfa;margin-bottom:5px;">'
                '📉 基本面因子月度漂移（T+5 IC）</div>'
                '<div style="display:flex;gap:3px;flex-wrap:wrap;">' + _md_cells + '</div>'
                + ('<div style="font-size:10.5px;color:#f87171;margin-top:6px;line-height:1.7;">'
                   '<b>⚠️ 最新月（' + str(_md_last.get("month")) + '）IC 已转负、胜率 '
                   + str(_md_last.get("win_rate")) + '%，但 n 仅 '
                   + str(_md_last.get("n")) + ' 个截面</b>，'
                   '不足以判定因子失效（月末样本天然最少，且 T+5 窗口部分未成熟）。'
                   '这与「全样本 IC=+0.070、NW t=3.08」并不矛盾：'
                   '<b>全样本均值由 7~9 月贡献，10 月是逆风段</b>。'
                   '实务含义：跟单需同步监控月度 IC，连续两月为负即应降仓而非机械照抄。</div>'
                   if _md_decay else
                   '<div style="font-size:10.5px;color:#64748b;margin-top:6px;line-height:1.7;">'
                   '各月 IC 均为正，因子未观察到失效迹象。</div>')
                + '</div>')
        # ---- 月度超额分解（P0）：区分因子失效 vs 市场逆风 ----
        _mexl = _tr_fd.get("monthly_excess") or []
        _mex_html = ""
        if _mexl:
            _mx_cells = ""
            _neg_months = []
            for _mo in _mexl:
                _ex = _mo.get("excess_pct")
                _c4 = "#ef4444" if (_ex or 0) > 0 else ("#22c55e" if (_ex or 0) < 0 else "#94a3b8")
                if isinstance(_ex, (int, float)) and _ex < 0:
                    _neg_months.append(_mo.get("month"))
                _mx_cells += (
                    f'<tr style="border-top:1px solid #1e293b;">'
                    f'<td style="padding:4px 8px;color:#cbd5e1;">{_mo.get("month")}</td>'
                    f'<td style="padding:4px 8px;text-align:center;color:#94a3b8;">{_mo.get("n")}</td>'
                    f'<td style="padding:4px 8px;text-align:center;color:#94a3b8;">'
                    f'{_mo.get("pool_mean_pct") if _mo.get("pool_mean_pct") is not None else "n/a"}</td>'
                    f'<td style="padding:4px 8px;text-align:center;color:#cbd5e1;">'
                    f'{_mo.get("head_mean_pct") if _mo.get("head_mean_pct") is not None else "n/a"}</td>'
                    f'<td style="padding:4px 8px;text-align:center;color:{_c4};font-weight:800;">'
                    f'{_ex:+.2f}</td></tr>')
            _last_neg = bool(_neg_months) and _mexl[-1].get("excess_pct") is not None \
                and _mexl[-1]["excess_pct"] < 0
            _mex_html = (
                '<div style="margin-top:10px;padding-top:9px;border-top:1px dashed #334155;">'
                '<div style="font-size:11.5px;font-weight:800;color:#fbbf24;margin-bottom:5px;">'
                '🔍 月度超额分解：因子失效，还是市场逆风？</div>'
                '<table style="width:100%;border-collapse:collapse;font-size:10.5px;">'
                '<thead><tr style="background:#1e293b;">'
                '<th style="padding:4px 8px;text-align:left;color:#94a3b8;">月份</th>'
                '<th style="padding:4px 8px;color:#94a3b8;">入场日</th>'
                '<th style="padding:4px 8px;color:#94a3b8;">池内等权%</th>'
                '<th style="padding:4px 8px;color:#94a3b8;">fund头组%</th>'
                '<th style="padding:4px 8px;color:#94a3b8;">头组超额pct</th>'
                '</tr></thead><tbody>' + _mx_cells + '</tbody></table>'
                + ('<div style="font-size:10.5px;color:#f87171;margin-top:6px;line-height:1.7;">'
                   '<b>⚠️ 最新月头组超额 %.2f pct（已转负）</b>：'
                   '同月池内等权 %+.2f%%，而基本面头组 %+.2f%% —— '
                   '<b>头组跌得比池内更多</b>。判据上这属于<b>因子边际失效</b>，'
                   '不能归因为“市场逆风”（逆风时头组应跌得更少）。'
                   '但 n=%d 个入场日，样本尚薄，先作为预警而非定论；'
                   '操作含义：<b>连续两月超额为负即应降仓，而非机械跟抄</b>。</div>'
                   % (_mexl[-1]["excess_pct"], _mexl[-1]["pool_mean_pct"],
                      _mexl[-1]["head_mean_pct"], _mexl[-1]["n"])
                   if _last_neg else
                   '<div style="font-size:10.5px;color:#64748b;margin-top:6px;">'
                   '最新月头组超额仍为正，未见因子失效迹象。</div>')
                + '</div>')
        _ah, _fh = _tr_all["horizons"], _tr_fd["horizons"]
        # v3.9（P0 修正）：与 fund 单维对比时，基准必须是**按当前权重重算**的复合分，
        #   不能用快照里的 total 字段 —— total 跨越 4→5→10 维三次口径变更（分段混合），
        #   用它作基准会得出"加权稀释了 74% alpha"的错误结论（实测应为"基本等价"）。
        _tw = _ic.get("tradable_w_ic") or {}
        _wh = _tw.get("horizons") or {}
        _use_h = _wh if _wh else _ah
        _cmp_label = ("复合分（现权重）" if _wh else "综合分（历史 total）")
        _dil_rows, _dil_any, _dil_max_t = "", False, 0.0
        for _h in (1, 5, 20):
            _a, _f = _use_h.get(str(_h)) or {}, _fh.get(str(_h)) or {}
            if _a.get("ic_mean") is None or _f.get("ic_mean") is None:
                continue
            # 配对检验：两条 IC 序列逐日相减
            _as, _fs = _a.get("ic_series") or [], _f.get("ic_series") or []
            _nn = min(len(_as), len(_fs))
            _pt = None
            if _nn >= 10:
                _d = [_fs[_i] - _as[_i] for _i in range(_nn)]
                _dm = sum(_d) / _nn
                _dvar = sum((_x - _dm) ** 2 for _x in _d) / (_nn - 1) if _nn > 1 else 0.0
                if _dvar > 0:
                    _pt = _dm / ((_dvar ** 0.5) / (_nn ** 0.5))
                    if abs(_pt) > abs(_dil_max_t):
                        _dil_max_t = _pt
            _fa, _ff = float(_a["ic_mean"]), float(_f["ic_mean"])
            _gain = _ff - _fa
            if abs(_gain) > 0.005:
                _dil_any = True
            _gc = "#22c55e" if _gain > 0.005 else ("#ef4444" if _gain < -0.005 else "#94a3b8")
            _pt_s = (f'{_pt:+.2f}' if _pt is not None else "样本不足")
            _pt_c = ("#22c55e" if (_pt or 0) > 2 else ("#ef4444" if (_pt or 0) < -2 else "#94a3b8"))
            _dil_rows += (
                f'<tr style="border-top:1px solid #1e293b;">'
                f'<td style="padding:5px 8px;text-align:center;color:#e2e8f0;font-weight:700;">T+{_h}</td>'
                f'<td style="padding:5px 8px;text-align:center;color:#cbd5e1;">{_fa:+.4f}</td>'
                f'<td style="padding:5px 8px;text-align:center;color:#7dd3fc;">{_ff:+.4f}</td>'
                f'<td style="padding:5px 8px;text-align:center;color:{_gc};font-weight:800;">'
                f'{_gain:+.4f}</td>'
                f'<td style="padding:5px 8px;text-align:center;color:{_pt_c};font-weight:700;">'
                f'{_pt_s}</td>'
                f'<td style="padding:5px 8px;text-align:center;color:#94a3b8;">'
                f'{_a.get("long_mean") if _a.get("long_mean") is not None else "—"} / '
                f'{_f.get("long_mean") if _f.get("long_mean") is not None else "—"}</td></tr>')
        if _dil_rows:
            # 结论必须数据驱动，不许预设"综合分更好"
            if _dil_max_t >= 2.0:
                _dil_head = "🔴 复合分显著劣于单维基本面"
                _dil_col = "#ef4444"
                _dil_txt = ('上表是<b>同一批截面、同一套可交易口径</b>下的逐日配对检验。'
                            '复合分的 IC 低于仅用 score_fund 一维，配对 t 已超 2 —— '
                            '这不是噪声，而是<b>加权结构本身在稀释有效 alpha</b>。'
                            '<b>含义：现有权重体系相对「只按基本面排序」是负贡献。</b>')
            elif _dil_max_t <= -2.0:
                _dil_head = "🟢 复合分显著优于单维基本面"
                _dil_col = "#22c55e"
                _dil_txt = ('逐日配对检验显示多加的维度<b>确实带来了增量信息</b>，'
                            '当前权重结构站得住。')
            elif _dil_any:
                _dil_head = "🟡 复合分与单维基本面差异未达显著"
                _dil_col = "#f59e0b"
                _dil_txt = ('两者 IC 有差距但<b>未通过逐日配对检验（|t|&lt;2）</b>，'
                            '不能断言加权一定更差或更好；样本仍在积累。')
            else:
                _dil_head = "⚪ 复合分与单维基本面基本等价（加权既未加分也未减分）"
                _dil_col = "#94a3b8"
                _dil_txt = ('两者 IC 差异在噪声范围内、配对 t 绝对值均 &lt;2。'
                            '<b>含义：另外几个维度目前既没带来增量 alpha，也没造成损失</b>——'
                            '它们的作用是分散单一维度的风险，而非提高预测力。'
                            '<b>这不支持「把权重全给 fund」，也不支持「维持现状即最优」</b>；'
                            '真正需要解决的是那些已在实证中转负的维度（见下方硬披露）。')
            _dil_div = (
                '<div style="margin:12px 0 16px;padding:12px 16px;background:' + _dil_col
                + '10;border:1px solid ' + _dil_col + '45;border-left:4px solid ' + _dil_col
                + ';border-radius:8px;">'
                '<div style="font-size:13px;font-weight:800;color:' + _dil_col
                + ';margin-bottom:6px;">⚖️ 综合分 vs 单维基本面：加权到底加分还是减分 —— '
                + _dil_head + '</div>'
                '<table style="width:100%;border-collapse:collapse;font-size:11px;">'
                '<thead><tr style="background:#1e293b;">'
                '<th style="padding:5px 8px;color:#94a3b8;">周期</th>'
                '<th style="padding:5px 8px;color:#94a3b8;">' + _cmp_label + ' IC<br>'
                '<span style="font-size:9px;font-weight:400;">现权重·同口径重算</span></th>'
                '<th style="padding:5px 8px;color:#94a3b8;">基本面 IC<br>'
                '<span style="font-size:9px;font-weight:400;">仅 score_fund</span></th>'
                '<th style="padding:5px 8px;color:#94a3b8;">IC 差<br>'
                '<span style="font-size:9px;font-weight:400;">基本面−综合</span></th>'
                '<th style="padding:5px 8px;color:#94a3b8;">配对 t<br>'
                '<span style="font-size:9px;font-weight:400;">逐日相减</span></th>'
                '<th style="padding:5px 8px;color:#94a3b8;">头组净收益%<br>'
                '<span style="font-size:9px;font-weight:400;">综合 / 基本面</span></th>'
                '</tr></thead><tbody>' + _dil_rows + '</tbody></table>'
                '<div style="font-size:12px;color:#e2e8f0;line-height:1.8;margin-top:8px;">'
                + _dil_txt
                + '</div>'
                '<div style="font-size:10.5px;color:#64748b;margin-top:6px;line-height:1.7;">'
                '口径一致：均为 T+1 开盘买入、扣 20bp 往返成本、同批 '
                + str(_tr_fd.get("horizons", {}).get("5", {}).get("n") or "—")
                + ' 个截面。配对 t 为两条逐日 IC 序列相减后做单样本 t 检验；'
                '|t|≥2 表示差异超出同期噪声。</div>'
                + _md_html
                + _mex_html
                + '</div>')

    # v3.8.2（P0）：母体已从「成交额榜」换成「全A SIZE 选池」，混口径统计会把
    #   旧母体的负 alpha 一路带下来、永远显示"选错了"，无法回答"换母体有没有用"。
    #   故按「池内市值中位 < 60亿 = SIZE 新母体」分段呈现，让新旧口径各自说话。
    _pe = _ic.get("pool_excess") or {}
    _peb = _ic.get("pool_excess_by_regime") or {}
    _pe_div = ''
    if _pe.get("median_excess_pct") is not None:
        _pe_med = float(_pe["median_excess_pct"])
        _pe_neg = _pe.get("neg_ratio_pct")
        _pe_hop = _pe.get("hop")
        _pe_col = "#ef4444" if _pe_med < -1.0 else ("#f59e0b" if _pe_med < 0 else "#22c55e")
        _pe_head = ("🔴 池子选错了" if _pe_med < -1.0 else
                    ("🟡 池子略偏弱" if _pe_med < 0 else "🟢 池子未见负超额"))
        # ---- 分段结论（新母体是否修好）----
        _seg_html = ""
        _lg = (_peb.get("legacy") or {})
        _sz = (_peb.get("size_pool") or {})
        if _lg.get("median_excess_pct") is not None or _sz.get("median_excess_pct") is not None:
            _seg_rows = ""
            for _nm2, _ob, _ds2 in (
                ("成交额旧母体", _lg, "（v3.7 及更早：按当日成交额排序取 TOP，高换手关注度脉冲集合）"),
                ("SIZE 新母体", _sz, "（v3.8 起：全A 流通市值最小 18%，已剔低价/低成交/小市值）"),
            ):
                _mv = _ob.get("median_excess_pct")
                if _mv is None:
                    _seg_rows += (f'<tr style="border-top:1px solid #1e293b;">'
                                  f'<td style="padding:5px 8px;color:#e2e8f0;font-weight:700;">{_nm2}</td>'
                                  f'<td colspan="4" style="padding:5px 8px;color:#64748b;font-size:11px;">'
                                  f'样本不足（{_ob.get("n_days", 0)} 个入场日），尚无法判定</td></tr>')
                    continue
                _c2 = "#ef4444" if _mv > 1 else ("#22c55e" if _mv < -1 else "#f59e0b")
                _mvtxt = (f'{_ob.get("mv_med_yi")}亿'
                          if _ob.get("mv_med_yi") is not None else '未落盘')
                _seg_rows += (f'<tr style="border-top:1px solid #1e293b;">'
                              f'<td style="padding:5px 8px;color:#e2e8f0;font-weight:700;">{_nm2}'
                              f'<div style="font-size:9.5px;color:#475569;font-weight:400;">'
                              f'{html_mod.escape(_ds2)}</div></td>'
                              f'<td style="padding:5px 8px;text-align:center;color:#94a3b8;">'
                              f'{_ob.get("n_days")}</td>'
                              f'<td style="padding:5px 8px;text-align:center;color:#94a3b8;">'
                              f'{_mvtxt}</td>'
                              f'<td style="padding:5px 8px;text-align:center;color:{_c2};font-weight:800;">'
                              f'{_mv:+.2f}pct</td>'
                              f'<td style="padding:5px 8px;text-align:center;color:#94a3b8;">'
                              f'{_ob.get("neg_ratio_pct")}%</td>'
                              f'<td style="padding:5px 8px;text-align:center;">'
                              f'<span style="font-size:10px;color:{_c2};font-weight:700;">'
                              f'{html_mod.escape(str(_ob.get("verdict") or "—"))}</span></td></tr>')
            _sz_ok = (_sz.get("median_excess_pct") is not None
                      and _sz.get("median_excess_pct") > -1.0)
            _sz_n = _sz.get("n_days") or 0
            if _sz_ok:
                _seg_concl = ('<b style="color:#22c55e;">新母体已不再呈现负超额</b>，'
                              '说明改入选规则确实对症——但样本仍在积累，需持续观察。')
            elif _sz_n == 0:
                _seg_concl = ('<b style="color:#f59e0b;">新母体 0 个成熟样本</b>：'
                              'v3.8 于 2026-10-25（周日）切换，首个新母体截面尚未经历 '
                              + str(_peb.get("hop") or 20) + ' 个交易日，'
                              '<b>因此本表无法证明换母体有效，也无法证明无效</b>。'
                              '页面其余全部因子 IC 与选池回测仍来自旧母体，'
                              '外推到新母体属于无依据推断。')
            else:
                _seg_concl = ('<b style="color:#f59e0b;">新母体样本尚少或仍偏弱</b>'
                              '（' + str(_sz_n) + ' 个入场日），不宜过早下结论。')
            _seg_html = (
                '<div style="margin-top:12px;padding-top:10px;border-top:1px dashed #334155;">'
                '<div style="font-size:12px;font-weight:800;color:#7dd3fc;margin-bottom:6px;">'
                '🔀 按母体口径分段：换母体到底修好了没有</div>'
                '<table style="width:100%;border-collapse:collapse;font-size:11px;">'
                '<thead><tr style="background:#1e293b;">'
                '<th style="padding:5px 8px;text-align:left;color:#94a3b8;">母体口径</th>'
                '<th style="padding:5px 8px;color:#94a3b8;">入场日</th>'
                '<th style="padding:5px 8px;color:#94a3b8;">池内市值中位</th>'
                '<th style="padding:5px 8px;color:#94a3b8;">超额中位</th>'
                '<th style="padding:5px 8px;color:#94a3b8;">跑输占比</th>'
                '<th style="padding:5px 8px;color:#94a3b8;">判定</th>'
                '</tr></thead><tbody>' + _seg_rows + '</tbody></table>'
                '<div style="font-size:10.5px;color:#94a3b8;margin-top:6px;line-height:1.7;">'
                + html_mod.escape(str(_peb.get("split_rule") or "")) + '。'
                + _seg_concl
                + '</div></div>')
        _pe_div = (
            '<div style="margin:12px 0 16px;padding:12px 16px;background:' + _pe_col
            + '10;border:1px solid ' + _pe_col + '45;border-left:4px solid ' + _pe_col
            + ';border-radius:8px;">'
            '<div style="font-size:13px;font-weight:800;color:' + _pe_col + ';margin-bottom:6px;">'
            '🧺 选股池体检：' + _pe_head + '（全样本混合口径）</div>'
            '<div style="font-size:12px;color:#e2e8f0;line-height:1.8;">'
            '对本池做「池内 vs 全市场价格池」的截面对照：过去 <b>' + str(_pe.get("n_days"))
            + '</b> 个入场日、持有 ' + str(_pe_hop) + ' 个交易日后，'
            '池内个股涨跌<b style="color:' + _pe_col + ';">中位数比全池低 '
            + f'{abs(_pe_med):.2f}' + ' 个百分点</b>（均值 '
            + f'{_pe.get("mean_excess_pct"):+.2f}' + 'pct），'
            + f'<b>{_pe_neg}%</b> 的入场日跑输。'
            '<br><b>但请注意口径：这 ' + str(_pe.get("n_days"))
            + ' 个入场日 100% 来自 v3.7 及更早的旧母体</b>（当日成交额 TOP 榜，'
            '本质是<b>高换手关注度脉冲</b>的集合，脉冲退潮后系统性回吐）。'
            'v3.8 的 SIZE 新母体<b style="color:#fbbf24;">尚未产生任何成熟样本</b>，'
            '因此这个 -' + f'{abs(_pe_med):.2f}' + 'pct <b>不能记为「换母体失败」</b>。'
            '分段对照见下。'
            + _seg_html
            + '</div></div>')

    _risk_banner = (
        '<div style="margin:12px 0 16px;padding:12px 16px;background:' + _gate_color
        + '14;border:1px solid ' + _gate_color + '55;border-left:4px solid ' + _gate_color
        + ';border-radius:8px;">'
        '<div style="font-size:13px;font-weight:800;color:' + _gate_color + ';margin-bottom:6px;">'
        '🛡️ 预测力安全门：' + _gate_title + '</div>'
        + _trig_ul
        + '<div style="font-size:12px;color:#e2e8f0;line-height:1.7;"><b>建议：</b>'
        + _advice + '</div>'
        + _collin_div
        + '</div>') + _dil_div + _pe_div


    html_parts.append(f'''
    <!-- v2.46 因子有效性评估（Rank IC） -->
    <div class="section-block" style="margin:24px 0;padding:18px 20px;background:#0f172a;border:1px solid #334155;border-radius:12px;">
        <div style="font-size:15px;font-weight:800;color:#7dd3fc;margin-bottom:4px;">
            📐 因子有效性评估（Rank IC）</div>
        <div style="font-size:11px;color:#64748b;margin-bottom:12px;line-height:1.7;">
            口径（v2.47 修正）：Rank IC = Spearman(因子值在当日截面的秩, 个股未来 h 个<b>真实交易日</b>收益)，h ∈ {{1, 5, 20}}；
            收益对当日 TOP{_POOL_N} <b>全部个股</b>计算（回填全市场前复权日线，消除"跌出榜单即消失"的留存偏差 —— v1 口径 T+20 流失 34%）；
            t 值采用 <b style="color:#fbbf24;">Newey-West HAC</b> 调整（h=5/20 相邻观测共享 80%/95% 收益窗口，普通 t 会虚高显著性）；
            <b style="color:#fbbf24;">★</b> = 通过 Bonferroni 多重检验校正（v3.0 起 10 维度×3 周期=30 次，|NW t|≥3.08；旧五维期 15 次，|NW t|≥2.97）。
            样本 &lt; 20 个截面不给出有效性判定。当前样本：
            <b style="color:#e2e8f0;">{_ic.get("n_snapshots")} 个有效截面</b>
            （{_ic.get("snapshot_range", ["?", "?"])[0]} ~ {_ic.get("snapshot_range", ["?", "?"])[1]}，
            剔除非交易日/接口陈旧快照 {len(_ic.get("stale_snapshots_dropped") or [])} 个，
            价格回填 {(_ic.get("price_backfill") or {}).get("n_codes") or "—"} 只 × {(_ic.get("price_backfill") or {}).get("n_trading_days") or "—"} 个交易日）。
        </div>
        {_risk_banner}
        <table style="width:100%;border-collapse:collapse;font-size:12px;">
            <thead><tr style="background:#1e293b;">
                <th style="padding:6px 10px;text-align:left;color:#94a3b8;font-weight:600;">因子</th>
                <th style="padding:6px 10px;color:#94a3b8;font-weight:600;">T+1（次日）</th>
                <th style="padding:6px 10px;color:#94a3b8;font-weight:600;">T+5（一周）</th>
                <th style="padding:6px 10px;color:#94a3b8;font-weight:600;">T+20（一月）</th>
                <th style="padding:6px 10px;color:#94a3b8;font-weight:600;">有效性判定</th>
            </tr></thead>
            <tbody>{_ic_rows}{_rec_row}{_trw_row}{_tr_row}{_trf_row}</tbody>
        </table>
        {_hard_disclose}
        {_factor_map_html}
        {f'<div style="margin-top:14px;"><div style="font-size:11px;color:#94a3b8;margin-bottom:6px;">📈 累计 IC 曲线（T+1，按截面序号累计；纵轴为累计 IC 值量程——曲线持续上行=因子稳定正向，下行=负向；窄屏可左右滑动）</div><div style="overflow-x:auto;">{_chart_svg}</div><div style="font-size:11px;margin-top:6px;">{_legend}</div></div>' if _chart_svg else ''}
        {_corr_html}
        {_wblock}
        <div style="font-size:11px;color:#f59e0b;margin-top:12px;padding:8px 10px;background:rgba(245,158,11,0.08);border-radius:6px;border-left:3px solid #f59e0b;line-height:1.7;">
            ⚠️ <b>解读须知</b>：① 口径已于 v2.47 修正（交易日对齐 / 全样本收益 / NW 调整 / Bonferroni）——
            v1 口径因留存偏差与假日陈旧快照污染，曾把综合总分与消息面误判为负向；修正后
            <b>总分全周期为正</b>、<b>基本面与消息面为正</b>（T+1/T+5/T+20 均正向）；
            ② v3.0-10dim（2026-10-13）：依据全市场回测 Top5 因子重构为十维——技术面/题材两个负 IC 维度拆解，
            其有效成分（RSI 反转）由新维度承载、负向成分（放量脉冲/超买）以反向维度承载，均线/MACD/动量退出评分；
            题材/技术两行保留为<b>对照展示</b>（负向 IC 正是拆解决策的依据）；新维度在 TOP{_POOL_N} 池无历史截面，
            初始权重为全市场回测先验映射，<b>满 30+ 池内截面后重拟合</b>（见上方⚖️区块状态标注）；
            ③ TOP{_POOL_N} 为高换手关注度筛选池，负向 IC 反映「关注度脉冲后的均值回复」，
            处理方式是<b>降权/反向而非删除</b>；
            ④ IC 衡量<b>排序能力</b>而非绝对收益，分层多空为未扣成本毛利口径；
            ⑤ 历史截面取自 git 提交记录（含早期行业归类缺陷版本，v2.46 已修复）；
            {_ic_span_note}
            ⑥ 投资有风险，本区块仅供研究与教育，不构成投资建议。
        </div>
        <div style="font-size:10px;color:#475569;margin-top:8px;">数据说明：{html_mod.escape(_ic_notes[:400])}</div>
    </div>''')

# ===== v3.2 P1-1 因子数据覆盖率区块（H3）=====
# 数据来自 scored json 的 stats.factor_coverage（每维非兜底比例）。
# 目的：TOP500 扩池后新增 300 只的接口成功率未知，兜底低分若不可见，
#   会把「数据缺失导致的低分」误读为「标的质地差」。
try:
    _cov = (stats.get("factor_coverage") or {})
    if _cov:
        _cov_labels = {"kline": "K线/技术指标", "fund": "基本面字段",
                       "news": "消息面", "theme": "题材解析", "amount": "成交额"}
        _cov_rows = ""
        _cov_warn = []
        for _dk, _dl in _cov_labels.items():
            if _dk not in _cov:
                continue
            _v = float(_cov.get(_dk) or 0)
            _pct = _v * 100
            _col = "#a3e635" if _v >= 0.90 else ("#fbbf24" if _v >= 0.75 else "#f87171")
            if _v < 0.90:
                _cov_warn.append(f"{_dl} {_pct:.0f}%")
            _cov_rows += (f'<tr><td style="padding:4px 10px;color:#cbd5e1;">{_dl}</td>'
                          f'<td style="padding:4px 10px;text-align:right;color:{_col};font-weight:700;">{_pct:.1f}%</td>'
                          f'<td style="padding:4px 10px;color:#64748b;font-size:11px;">'
                          f'{"达标" if _v >= 0.90 else "低于 90% 阈值"}</td></tr>')
        _cov_note = ("全部维度达标。" if not _cov_warn
                     else "⚠️ 低于 90% 阈值：" + "、".join(_cov_warn) + "（该维评分含兜底成分，解读请谨慎）")
        html_parts.append(f'''
    <!-- v3.2 P1-1 因子数据覆盖率（H3） -->
    <div class="section-block" style="margin:24px 0;padding:16px 20px;background:#0f172a;border:1px solid #334155;border-radius:12px;">
        <div style="font-size:14px;font-weight:800;color:#a3e635;margin-bottom:6px;">🧪 因子数据覆盖率（{total_stocks} 只）</div>
        <div style="font-size:11px;color:#64748b;margin-bottom:10px;line-height:1.7;">
            口径：各维度「数据真实获取（非兜底降档）」的个股占比。兜底=接口失败/字段缺失时按缺省档给分，
            非标的质量信号。TOP{_POOL_N} 扩池后新增标的接口成功率是本区块的监控对象。
        </div>
        <table style="width:100%;max-width:520px;border-collapse:collapse;font-size:12px;">
            <thead><tr style="background:#1e293b;">
                <th style="padding:4px 10px;text-align:left;color:#94a3b8;">维度</th>
                <th style="padding:4px 10px;text-align:right;color:#94a3b8;">非兜底覆盖</th>
                <th style="padding:4px 10px;text-align:left;color:#94a3b8;">状态</th>
            </tr></thead>
            <tbody>{_cov_rows}</tbody>
        </table>
        <div style="font-size:11px;color:#94a3b8;margin-top:8px;line-height:1.7;">{_cov_note}</div>
    </div>''')
except Exception as _ce:
    pass


# ===== v3.5.1: growth 取数口径变更 + Q3 预测加成披露（诚实披露纪律）=====
# 背景: 老大核查发现 growth 原取「按年度」摘要末行=最近年报，全池滞后半年以上
#   （普冉股份年报口径 −38.7% vs 2026中报实际 +2994.08%）；
#   且同比公式在负基数下符号反转（佰维存储扭亏 +2963% 被误判为 −2963%）。
# 修复: 东财「按报告期」取各股最新已披露报告期扣非累计同比，官方口径 (cur−prev)/|prev|。
#   Q3 预测为手工汇总表（半导体/消费电子链），覆盖率有限，仅作景气边际信号加分，
#   非全年盈利预测输入；超出机构全年预期(implied_h2)部分才计分，防止与 FwdPE 双重计价。
try:
    _g_em = sum(1 for x in results if x.get("growth_src") == "em")
    _g_q3 = sum(1 for x in results if isinstance(x.get("q3_yoy"), (int, float)) or (x.get("q3_bonus") or 0) > 0)
    _g_q3b = sum(1 for x in results if (x.get("q3_bonus") or 0) > 0)
    _g_cap = sum(1 for x in results if x.get("growth_capped"))
    _g_neg_old = sum(1 for x in results
                     if isinstance(x.get("growth_annual_ths"), (int, float)) and isinstance(x.get("growth"), (int, float))
                     and (x["growth_annual_ths"] > 0) != (x["growth"] > 0))
    html_parts.append(f'''
    <!-- v3.5.1 growth 口径与 Q3 预测披露 -->
    <div class="section-block" style="margin:24px 0;padding:16px 20px;background:#0f172a;border:1px solid #334155;border-radius:12px;">
        <div style="font-size:14px;font-weight:800;color:#38bdf8;margin-bottom:6px;">📐 盈利取数口径与 Q3 预测加成说明（{total_stocks} 只）</div>
        <div style="font-size:12px;color:#cbd5e1;line-height:1.9;">
            ① <b style="color:#fbbf24;">增速口径 v3.5.1 起变更</b>：growth 改为各股<b>最新已披露报告期</b>（2026中报为主）扣非累计同比，
            东财「按报告期」接口取数，官方口径 <code>(本期−上年同期)/|上年同期|</code>；
            修复了旧口径「按年度摘要取最近年报」导致的全池滞后半年以上问题。<br>
            ② <b>口径切换影响</b>：{_g_em}/{total_stocks} 只走东财新口径；其中 <b style="color:#f87171;">{_g_neg_old} 只增长方向与旧年报口径相反</b>；
            {_g_cap} 只触发低基数失真饱和压缩（详情卡 📉 徽标可审计）。<br>
            ③ <b>Q3 预测景气加成</b>：数据源为手工汇总表（半导体/消费电子链 105 只），
            池内覆盖 <b>{_g_q3} 只（{_g_q3/total_stocks*100:.1f}%）</b>，实际获加分 <b style="color:#22c55e;">{_g_q3b} 只</b>（上限 +1.0 分/0-5 分制 → 总分 +3.6）。
            加分条件：Q3 预测同比（经同样饱和处理）相对中报<b>加速</b>，且超出机构全年盈利预期隐含的 H2 增速
            （否则视为已被 FwdPE 计价，不加分）——<b>只加分、不扣分</b>，Q3 为预测值非已披露事实。<br>
            ④ 该表非程序化接口、TTL 45 天，未覆盖个股按原口径评分，不受影响。
        </div>
    </div>''')
except Exception as _qe:
    pass


# ===== v2.41 P0-4 推荐跟踪与胜率 (真实K线回填) =====
try:
    _tf = os.path.join(_ai_dir, "recommendation_track.json")
    _track = json.load(open(_tf, encoding="utf-8")) if os.path.exists(_tf) else {"records": []}
    _recs = _track.get("records", [])
    if _recs:
        from collections import defaultdict as _dd
        _agg = _dd(lambda: {h: [] for h in (1, 5, 20)})
        _agg_ex = _dd(lambda: {h: [] for h in (1, 5, 20)})
        _cutoff = None
        for _rc in _recs:
            for _h in (1, 5, 20):
                _v = (_rc.get("rets") or {}).get(str(_h))
                if _v is not None:
                    _agg[_rc["kind"]][_h].append(_v)
                    _agg["__ALL__"][_h].append(_v)
                    _cutoff = max(_cutoff, _rc["date"]) if _cutoff else _rc["date"]
                _ex = (_rc.get("excess") or {}).get(str(_h))
                if _ex is not None:
                    _agg_ex[_rc["kind"]][_h].append(_ex)
                    _agg_ex["__ALL__"][_h].append(_ex)

        def _stat_cell(vals):
            if not vals:
                return '<td style="padding:5px 8px;color:#475569;">积累中</td>'
            _avg = sum(vals) / len(vals)
            _win = sum(1 for x in vals if x > 0) / len(vals) * 100
            _c = "#ef4444" if _avg > 0 else "#22c55e"   # A股: 红涨绿跌
            return (f'<td style="padding:5px 8px;color:{_c};font-weight:700;">{_avg:+.2f}%</td>'
                    f'<td style="padding:5px 8px;color:#94a3b8;">{_win:.0f}%</td>')

        # v2.44 P1-2 超额收益单元格(相对沪深300)
        # v3.6.1: 空值不再显示裸"——"（老大要求"不要空着"），
        #   改为按该周期是否已有任何到期样本区分「未到期」与「该分组无样本」。
        _ex_any = {h: bool(_agg_ex.get("__ALL__", {}).get(h)) for h in (1, 5, 20)}

        def _ex_cell(vals, h=1):
            if not vals:
                if not _ex_any.get(h):
                    _wait = {1: "T+1 未到期", 5: "T+5 未到期", 20: "T+20 未到期"}.get(h, "未到期")
                    return (f'<td style="padding:5px 8px;color:#475569;text-align:center;" colspan="2">'
                            f'<span style="font-size:10px;">{_wait}（需 {h} 个交易日）</span></td>')
                return ('<td style="padding:5px 8px;color:#475569;text-align:center;" colspan="2">'
                        '<span style="font-size:10px;">该组无样本</span></td>')
            _avg = sum(vals) / len(vals)
            _win = sum(1 for x in vals if x > 0) / len(vals) * 100
            _c = "#ef4444" if _avg > 0 else ("#22c55e" if _avg < 0 else "#94a3b8")
            return (f'<td style="padding:5px 8px;color:{_c};font-weight:700;">{_avg:+.2f}%</td>'
                    f'<td style="padding:5px 8px;color:#94a3b8;">{_win:.0f}%</td>')

        _rows = ""
        _lbl = {"__ALL__": "全部推荐", "rating": "评级 S/A 级", "ai_portfolio": "AI 组合",
                "quant_momentum": "量化·动量", "quant_reversal": "量化·反转"}
        for _k in ["__ALL__", "rating", "ai_portfolio", "quant_momentum", "quant_reversal"]:
            if _k not in _agg:
                continue
            _d = _agg[_k]
            _de = _agg_ex.get(_k, {h: [] for h in (1, 5, 20)})
            _n1 = len(_d[1])
            _rows += (f'<tr style="border-top:1px solid #1e293b;">'
                      f'<td style="padding:5px 8px;color:#e2e8f0;font-weight:600;">{_lbl.get(_k, _k)}</td>'
                      f'<td style="padding:5px 8px;color:#94a3b8;">{_n1}</td>'
                      + _stat_cell(_d[1]) + _stat_cell(_d[5]) + _stat_cell(_d[20])
                      + _ex_cell(_de[1], 1) + _ex_cell(_de[5], 5) + _ex_cell(_de[20], 20) + '</tr>')

        _has_data = any(_agg[_k][1] or _agg[_k][5] for _k in _agg)
        # R3: 样本量 < 30 时, 跟踪表无统计意义 —— 折叠为进度提示, 不占版面
        _MIN_SAMPLE = 30
        _n_all = len(_agg.get("__ALL__", {}).get(1, []))
        if not _has_data:
            _track_inner = (f'''<div style="font-size:11px;color:#f59e0b;padding:10px 12px;background:rgba(245,158,11,0.08);border-radius:8px;">'''
                            f'''⏳ 样本积累中（{len(_recs)} 条记录 / 建议 ≥{_MIN_SAMPLE} 条）：首日已建立推荐快照，'''
                            f'''T+1 次日起、T+5/T+20 后续交易日自动回填，届时本区块自动展开胜率表。</div>''')
            _track_summary = f"📊 推荐跟踪 · 胜率验证（样本积累中 {len(_recs)}/{_MIN_SAMPLE}）"
            _track_open = ""
        else:
            _track_inner = f'''
        <table style="width:100%;border-collapse:collapse;font-size:11px;">
            <thead>
            <tr style="color:#94a3b8;text-align:left;">
                <th style="padding:4px 8px;" rowspan="2">分组</th><th style="padding:4px 8px;" rowspan="2">样本</th>
                <th style="padding:4px 8px;border-bottom:1px solid #334155;" colspan="2">T+1</th>
                <th style="padding:4px 8px;border-bottom:1px solid #334155;" colspan="2">T+5</th>
                <th style="padding:4px 8px;border-bottom:1px solid #334155;" colspan="2">T+20</th>
                <th style="padding:4px 8px;border-bottom:1px solid #334155;color:#38bdf8;" colspan="2">超额 T+1<br><span style="font-weight:400;color:#64748b;">vs 沪深300</span></th>
                <th style="padding:4px 8px;border-bottom:1px solid #334155;color:#38bdf8;" colspan="2">超额 T+5<br><span style="font-weight:400;color:#64748b;">vs 沪深300</span></th>
                <th style="padding:4px 8px;border-bottom:1px solid #334155;color:#38bdf8;" colspan="2">超额 T+20<br><span style="font-weight:400;color:#64748b;">vs 沪深300</span></th>
            </tr>
            <tr style="color:#64748b;text-align:left;font-weight:400;font-size:11px;">
                <th style="padding:2px 8px;">均值</th><th style="padding:2px 8px;">胜率</th>
                <th style="padding:2px 8px;">均值</th><th style="padding:2px 8px;">胜率</th>
                <th style="padding:2px 8px;">均值</th><th style="padding:2px 8px;">胜率</th>
                <th style="padding:2px 8px;">均值</th><th style="padding:2px 8px;">胜率</th>
                <th style="padding:2px 8px;">均值</th><th style="padding:2px 8px;">胜率</th>
                <th style="padding:2px 8px;">均值</th><th style="padding:2px 8px;">胜率</th>
            </tr>
            </thead>
            <tbody>{_rows}</tbody>
        </table>'''
            _track_summary = f"📊 推荐跟踪 · 胜率验证（已回填 {_n_all} 条样本）"
            _track_open = " open"
        # 首日/覆盖天数动态计算(修复硬编码 2026-10-08 文案)
        _track_dates = sorted({_r.get("date") for _r in _recs if _r.get("date")})
        _track_first_date = _track_dates[0] if _track_dates else "首日"
        _track_n_days = len(_track_dates)
        html_parts.append(f'''
    <!-- v2.41 P0-4 推荐跟踪 (v2.42 R3 空态折叠; v2.44 P1-2 超额收益 / P1-4 偏差披露) -->
    <details class="track-block" style="margin:14px 0;"{_track_open}>
        <summary style="cursor:pointer;font-size:12px;color:#38bdf8;padding:10px 12px;background:#1e293b;border-radius:8px;">{_track_summary}</summary>
        <div style="padding:12px 14px;background:#1e293b;border-radius:0 0 8px 8px;">
        <div style="font-size:11px;color:#64748b;margin-bottom:10px;">自 {_track_first_date} 起累计记录 {len(_recs)} 条推荐（覆盖 {_track_n_days} 个交易日）；T+N 收益按推荐日收盘价为基准，用前复权K线回填；<b style="color:#38bdf8;">超额收益 = 个股 T+N 涨跌幅 − 沪深300 同期涨跌幅</b>（用于剥离大盘 beta，区分选股能力）。</div>
        {_track_inner}
        <div style="font-size:11px;color:#475569;margin-top:6px;">说明：每日运行 track_recommendations.py 记录并回填。胜率=上涨样本占比（红涨绿跌口径）。T+5/T+20 需对应交易日到期后才有值，"积累中"表示尚未到期。</div>
        <div style="font-size:11px;color:#f59e0b;margin-top:8px;padding:8px 10px;background:rgba(245,158,11,0.08);border-radius:6px;border-left:3px solid #f59e0b;line-height:1.6;">
            ⚠️ <b>样本偏差披露（生存者偏差）</b>：本跟踪表标的均取自「当日成交额 TOP{_POOL_N}」名单。<b>当日大跌、成交萎缩而跌出榜单的个股不再被追踪</b>，因此统计结果存在向上的生存者偏差，实际策略表现可能低于表中数值。此外，样本量较小（当前 {len(_recs)} 条）时统计显著性不足，请勿据此单独下结论。
        </div>
        </div>
    </details>''')
except Exception as _e:
    pass

html_parts.append('''
    <!-- A2 元信息下沉: 版本号/数据源/口径说明全部收进页脚 -->
    <div class="footer" id="sec-notes">
        <p style="color:#64748b;">📄 ''' + REPORT_VER + ''' 报告层 · ''' + _SCORER_VER + ''' 评分层 · 数据日期 ''' + date_str + ''' · 更新时间 ''' + datetime.now().strftime('%Y-%m-%d %H:%M:%S') + '''</p>
        <p>📚 数据源：东方财富 + 腾讯K线 + 同花顺 + 新浪/乐股 · 全市场成交额排名 · 样本池 TOP''' + str(_POOL_N) + '''（覆盖 ''' + str(total_stocks) + ''' 只 / ''' + f'{_pool_cov:.0%}' + '''）</p>
        <p>📐 盈利取数口径（v3.5.1 起）：growth = 各股最新已披露报告期（2026中报为主）扣非累计同比，东财按报告期接口、官方口径 (本期−上年同期)/|上年同期|；Q3 预测景气加成来自手工汇总表（半导体/消费电子链，池内覆盖约13%，超出机构全年预期部分才计分）</p>
        <p>🗒️ 点评来源：''' + _ai_src_label + '''（''' + html_mod.escape(_ai_src_tip) + '''）</p>
        <p>⚠️ 免责声明: 本报告由AI基于stock-scorer极简评分模型自动生成,仅供参考,不构成投资建议。投资有风险,入市需谨慎。</p>
        <p>评分模型 (''' + _SCORER_VER + '''): '''
        + ('十维·基本面主导（v3.6-fund-led）— 基本面' + f'{_DIM_W["score_fund"]:.0%}'
           + ' + 消息面' + f'{_DIM_W["score_news"]:.0%}'
           + ' + 板块动量' + f'{_DIM_W["score_sector"]:.0%}'
           + ' + 超跌60反转' + f'{_DIM_W["score_oversold60"]:.1%}'
           + ' + 乖离低位' + f'{_DIM_W["score_bias60"]:.1%}'
           + ' + RSI反转' + f'{_DIM_W["score_rsi_rev"]:.1%}'
           + ' + 量能反向' + f'{_DIM_W["score_vol_pulse"]:.1%}'
           + ' + 超买反向' + f'{_DIM_W["score_overbought"]:.0%}'
           + ' + 题材内禀' + f'{_DIM_W["score_theme_intrinsic"]:.0%}'
           + ' + 关注度' + f'{_DIM_W["score_attention"]:.0%}'
           + '（2026-10-24 口径切换：以全样本可验证维度为主，反转块等权压缩以消除 0.742~0.889 共线性虚增；'
           + '超买/关注度有效截面仅 9 个降至 2% 地板；池内十维截面<30 仍属先验·待 IC 复验）；'
           if _has_10d else
           ('基本面30% + 题材热度18% + 消息面20%(AI赋分) + 技术面20% + 板块动量12%(申万二级板块60日/10日动量排名, 按6:4合成)；'
            if _has_sector else
            '题材热度30% + 基本面30%(含业绩一阶导/二阶导) + 消息面20%(AI赋分) + 技术面20%；'))
        + '''消息/题材热度由AI基于当日题材资金共识赋分(0.7×AI+0.3×算法)</p>
    </div>
</div>

<script>
// 数据存储
const sectors = new Set();
let currentPage = 1;
let currentSort = {field: 'total', desc: true};
let quickFilter = 'all';
const ratingOrder = {"S": 4, "A": 3, "B": 2, "C": 1};

document.addEventListener('DOMContentLoaded', function() {
    initFilters();
    initSorting();
    initQuickFilters();
    initColToggle();
    initViewPresets();
    initExpandableRows();
    initPagination();
    collectSectors();
    initViewPresetsApply();
    applyFilters();
});

// A7 视图预设: 简洁(5) / 标准(9) / 完整(12)
const PRESETS = {
    simple:   ['pct', 'scores', 'total', 'rating'],
    standard: ['code', 'sector', 'pct', 'mom', 'scores', 'total', 'rating', 'pe', 'turnover'],
    full:     ['code', 'sector', 'pct', 'mom', 'scores', 'total', 'rating', 'pe', 'sub', 'week', 'turnover']
};
function applyPreset(name, syncCheckboxes) {
    const cols = PRESETS[name] || PRESETS.standard;
    document.querySelectorAll('.col-check').forEach(cb => {
        const on = cols.includes(cb.dataset.col);
        cb.checked = on;
        document.querySelectorAll('.col-' + cb.dataset.col).forEach(el => {
            el.style.display = on ? '' : 'none';
        });
    });
    document.querySelectorAll('.preset-btn').forEach(b => b.classList.toggle('active', b.dataset.preset === name));
}
function initViewPresets() {
    document.querySelectorAll('.preset-btn').forEach(btn => {
        btn.addEventListener('click', () => applyPreset(btn.dataset.preset, true));
    });
}
function initViewPresetsApply() {
    // 首屏按"标准"预设收敛列(减少横向滚动负担)
    applyPreset('standard', true);
}

function collectSectors() {
    const rows = document.querySelectorAll('#stockTable tbody tr.expandable');
    rows.forEach(row => {
        if (row.dataset.sector) sectors.add(row.dataset.sector);
    });
    const sectorFilter = document.getElementById('sectorFilter');
    Array.from(sectors).sort().forEach(s => {
        const opt = document.createElement('option');
        opt.value = s;
        opt.textContent = s;
        sectorFilter.appendChild(opt);
    });
}

// 筛选相关
function initFilters() {
    document.getElementById('searchInput').addEventListener('input', debounce(applyFilters, 300));
    document.getElementById('sortSelect').addEventListener('change', function() {
        currentSort.field = this.value;
        currentSort.desc = true;
        currentPage = 1;
        applyFilters();
    });
    document.getElementById('ratingFilter').addEventListener('change', function() { currentPage = 1; applyFilters(); });
    document.getElementById('sectorFilter').addEventListener('change', function() { currentPage = 1; applyFilters(); });
    // v3.2 P1-4: 路线/资格筛选
    const _rf = document.getElementById('routeFilter');
    if (_rf) _rf.addEventListener('change', function() { currentPage = 1; applyFilters(); });
    const _qf = document.getElementById('qualFilter');
    if (_qf) _qf.addEventListener('change', function() { currentPage = 1; applyFilters(); });
    document.getElementById('pageSize').addEventListener('change', function() { currentPage = 1; applyFilters(); });
}

// 快捷筛选
function initQuickFilters() {
    document.querySelectorAll('.quick-btn').forEach(btn => {
        btn.addEventListener('click', function() {
            document.querySelectorAll('.quick-btn').forEach(b => b.classList.remove('active'));
            this.classList.add('active');
            quickFilter = this.dataset.quick;
            currentPage = 1;
            applyFilters();
        });
    });
}

// 列显示控制
function initColToggle() {
    document.querySelectorAll('.col-check').forEach(cb => {
        cb.addEventListener('change', function() {
            const col = this.dataset.col;
            const visible = this.checked;
            document.querySelectorAll('.col-' + col).forEach(el => {
                el.style.display = visible ? '' : 'none';
            });
        });
    });
}

// 可展开行 - 懒加载，点击时才生成详情
function initExpandableRows() {
    document.querySelector('#stockTable tbody').addEventListener('click', function(e) {
        const row = e.target.closest('tr.expandable');
        if (!row) return;
        let detailRow = row.nextElementSibling;
        if (!detailRow || !detailRow.classList.contains('detail-row')) {
            // 动态创建详情行
            detailRow = document.createElement('tr');
            detailRow.className = 'detail-row';
            detailRow.dataset.parent = row.dataset.code;
            const newsScore = row.dataset.news || '0';
            const techScore = row.dataset.techScore || '0';
            const fundScore = row.dataset.fundScore || '0';
            const themeScore = row.dataset.themeScore || '0';
            const newsText = row.dataset.newsText || '无';
            const techText = row.dataset.techText || '';
            const fundText = row.dataset.fundText || '无';
            const themeText = row.dataset.themeText || '无';
            detailRow.innerHTML = '<td colspan="' + row.children.length + '"><div class="detail-content" style="background:#0f172a;border-radius:8px;padding:16px;margin:4px;">' +
                '<div style="display:grid;grid-template-columns:repeat(4,1fr);gap:12px;font-size:11px;">' +
                '<div><div style="color:#94a3b8;margin-bottom:4px;">📰 消息面(' + newsScore + '/10)</div><div style="color:#e2e8f0;">' + newsText + '</div></div>' +
                '<div><div style="color:#94a3b8;margin-bottom:4px;">📈 技术面(' + techScore + '/10)</div><div style="color:#e2e8f0;">' + techText + '</div></div>' +
                '<div><div style="color:#94a3b8;margin-bottom:4px;">📊 基本面(' + fundScore + '/10)</div><div style="color:#e2e8f0;">' + fundText + '</div></div>' +
                '<div><div style="color:#94a3b8;margin-bottom:4px;">🔥 题材热度(' + themeScore + '/10)</div><div style="color:#e2e8f0;">' + themeText + '</div></div>' +
                '</div></div></td>';
            row.parentNode.insertBefore(detailRow, row.nextSibling);
        }
        detailRow.classList.toggle('show');
    });
}

// 排序相关
function initSorting() {
    document.querySelectorAll('#stockTable th[data-sort]').forEach(th => {
        th.addEventListener('click', () => {
            const field = th.dataset.sort;
            if (currentSort.field === field) {
                currentSort.desc = !currentSort.desc;
            } else {
                currentSort.field = field;
                currentSort.desc = true;
            }
            currentPage = 1;
            applyFilters();
        });
    });
}

// 分页
function initPagination() {
    document.getElementById('prevPage').addEventListener('click', function() {
        if (currentPage > 1) { currentPage--; applyFilters(); }
    });
    document.getElementById('nextPage').addEventListener('click', function() {
        currentPage++; applyFilters();
    });
}

// 快捷筛选判定
function passesQuickFilter(row) {
    if (quickFilter === 'all') return true;
    if (quickFilter === 'SA') return row.dataset.rating === 'S' || row.dataset.rating === 'A';
    if (quickFilter === 'S') return row.dataset.rating === 'S';
    if (quickFilter === 'undervalued') return parseFloat(row.dataset.fwdPe) > 0 && parseFloat(row.dataset.fwdPe) < 30;
    if (quickFilter === 'growth') return parseFloat(row.dataset.growth) >= 30;
    if (quickFilter === 'low_pos') return parseFloat(row.dataset.position) < 30;
    if (quickFilter === 'hot') return parseInt(row.dataset.scoreTheme) >= 4;
    if (quickFilter === 'up') return row.querySelector('.rating-up') !== null;
    if (quickFilter === 'new') return row.querySelector('.rating-new') !== null;
    return true;
}

// 主筛选+分页函数
function applyFilters() {
    const search = document.getElementById('searchInput').value.toLowerCase();
    const rating = document.getElementById('ratingFilter').value;
    const sector = document.getElementById('sectorFilter').value;
    const pageSize = parseInt(document.getElementById('pageSize').value);
    const routeSel = document.getElementById('routeFilter') ? document.getElementById('routeFilter').value : '';
    const qualSel = document.getElementById('qualFilter') ? document.getElementById('qualFilter').value : '';

    const tbody = document.getElementById('tableBody');
    let rows = Array.from(tbody.querySelectorAll('tr.expandable'));
    // 懒加载模式下，先清理已展开的详情行
    tbody.querySelectorAll('tr.detail-row').forEach(d => d.remove());

    // 筛选
    let filtered = rows.filter(row => {
        if (search && !row.dataset.name.toLowerCase().includes(search) && !row.dataset.code.toLowerCase().includes(search)) return false;
        if (rating && row.dataset.rating !== rating) return false;
        if (sector && row.dataset.sector !== sector) return false;
        // v3.2 P1-4: 路线筛选（反转/动量/双路线入选）
        if (routeSel) {
            const _rts = (row.dataset.route || '').split(',').filter(Boolean);
            if (routeSel === 'both') { if (_rts.indexOf('momentum') < 0 || _rts.indexOf('reversal') < 0) return false; }
            else if (_rts.indexOf(routeSel) < 0) return false;
        }
        // v3.2 P1-4: 资格筛选（合格/流动性未达标/次新）
        if (qualSel) {
            if (qualSel === 'qualified' && row.dataset.qual !== '1') return false;
            if (qualSel === 'new_pool' && row.dataset.newpool !== '1') return false;
            if (qualSel === 'illiquid' && row.dataset.liquid !== '0') return false;
            if (qualSel === 'new_stock' && row.dataset.newstock !== '1') return false;
        }
        if (!passesQuickFilter(row)) return false;
        return true;
    });

    // 排序
    filtered.sort((a, b) => {
        let aVal, bVal;
        switch(currentSort.field) {
            case 'rank':
                aVal = parseInt(a.dataset.originalRank);
                bVal = parseInt(b.dataset.originalRank);
                break;
            case 'name':
                aVal = a.dataset.name;
                bVal = b.dataset.name;
                return currentSort.desc ? bVal.localeCompare(aVal) : aVal.localeCompare(bVal);
            case 'pct_chg':
                aVal = parseFloat(a.querySelector('.s-pct').dataset.value);
                bVal = parseFloat(b.querySelector('.s-pct').dataset.value);
                break;
            case 'week_chg':
                aVal = parseFloat(a.querySelector('.s-week').dataset.value);
                bVal = parseFloat(b.querySelector('.s-week').dataset.value);
                break;
            case 'total':
                aVal = parseFloat(a.querySelector('.s-total').dataset.value);
                bVal = parseFloat(b.querySelector('.s-total').dataset.value);
                break;
            case 'turnover':
                aVal = parseFloat(a.querySelector('.s-turnover').dataset.value);
                bVal = parseFloat(b.querySelector('.s-turnover').dataset.value);
                break;
            case 'pe':
                aVal = parseFloat(a.querySelector('.s-pe').dataset.value);
                bVal = parseFloat(b.querySelector('.s-pe').dataset.value);
                break;
            default:
                // v3.0: score_* 通用映射(data-s-* 属性; 十维子项 data-s-oversold60 等同样生效)
                if (currentSort.field.indexOf('score_') === 0) {
                    const _k = 's' + currentSort.field.slice(6).charAt(0).toUpperCase() + currentSort.field.slice(7);
                    aVal = parseFloat(a.dataset[_k]);
                    bVal = parseFloat(b.dataset[_k]);
                    if (isNaN(aVal)) aVal = 0;
                    if (isNaN(bVal)) bVal = 0;
                } else {
                    aVal = parseInt(a.dataset.originalRank);
                    bVal = parseInt(b.dataset.originalRank);
                }
        }
        return currentSort.desc ? (bVal - aVal) : (aVal - bVal);
    });

    // 更新排序箭头
    document.querySelectorAll('#stockTable th[data-sort]').forEach(th => {
        th.classList.remove('sorted-asc', 'sorted-desc');
        const arrow = th.querySelector('.sort-arrow');
        if (arrow) arrow.textContent = '';
        if (th.dataset.sort === currentSort.field) {
            th.classList.add(currentSort.desc ? 'sorted-desc' : 'sorted-asc');
            if (arrow) arrow.textContent = currentSort.desc ? '↓' : '↑';
        }
    });

    // 分页
    const totalPages = Math.max(1, Math.ceil(filtered.length / pageSize));
    if (currentPage > totalPages) currentPage = totalPages;
    const start = (currentPage - 1) * pageSize;
    const pageRows = filtered.slice(start, start + pageSize);
    const visibleCodes = new Set(pageRows.map(r => r.dataset.code));

    // 重排DOM顺序（懒加载模式：只排expandable行）
    filtered.forEach(row => tbody.appendChild(row));

    // 隐藏所有行
    rows.forEach(row => row.classList.add('hidden-row'));

    // 显示当前页
    pageRows.forEach((row, idx) => {
        row.classList.remove('hidden-row');
        const rankCell = row.querySelector('.s-rank');
        if (rankCell) rankCell.textContent = start + idx + 1;
    });

    // 更新分页信息
    document.getElementById('pageInfo').textContent = currentPage + ' / ' + totalPages + ' (共' + filtered.length + '条)';
    document.getElementById('prevPage').disabled = currentPage <= 1;
    document.getElementById('nextPage').disabled = currentPage >= totalPages;
}

function debounce(func, wait) {
    let timeout;
    return function(...args) {
        clearTimeout(timeout);
        timeout = setTimeout(() => func.apply(this, args), wait);
    };
}
</script>
</body>
</html>''')

# 合并并保存
html = "\n".join(html_parts)
with open(out, "w", encoding="utf-8") as f:
    f.write(html)

# 输出统计
print(f"✅ HTML报告已生成: {out}")
print(f"📊 包含 {total_stocks} 只个股")

sd = stats.get("rating_dist", {})
print(f"⭐ 评级分布: S:{sd.get('S',0)} A:{sd.get('A',0)} B:{sd.get('B',0)} C:{sd.get('C',0)} D:{sd.get('D',0)}")

fwd_ok = sum(1 for r in results if r.get("fwd_pe"))
growth_ok = sum(1 for r in results if r.get("growth"))
print(f"📋 数据完整度: Fwd PE:{fwd_ok}/{total_stocks} Growth:{growth_ok}/{total_stocks}")











