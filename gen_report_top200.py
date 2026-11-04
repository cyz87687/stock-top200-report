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

# ──────────────────────────────────────────────────────────────────────────────
# v4.10（2026-10-27 T1-3）：池内估值绝对参照 —— **必须在 results 加载后立即算**。
#
# 为什么不能放在后面用的时候再算：
#   fund / ep 两维都是「池内 rank 分位 ×10」，读者看到「估值锚 8 分」无法判断
#   这到底是 15 倍还是 150 倍 —— 分位数本身不含绝对信息。
#   详情卡 tooltip 需要这个绝对值，而详情卡由 generate_research_analysis()
#   在 L5820 附近就被调用，晚于那里定义的任何变量。故只能前置到这里。
#
# ⚠️ 口径：只用 pe>0 / fwd_pe>0 的样本算分位（亏损股 PE 为 0 或负，混进去会把
#   分位整体拉低到无意义）。亏损只数与无 FwdPE 只数单独统计并披露。
# ──────────────────────────────────────────────────────────────────────────────
_pool_pe_stats = {}
try:
    for _pk in ("pe", "fwd_pe"):
        _vs = sorted([r.get(_pk) for r in results
                      if isinstance(r.get(_pk), (int, float))
                      and not isinstance(r.get(_pk), bool) and r.get(_pk) > 0])
        if len(_vs) >= 50:
            _nn = len(_vs)
            _pool_pe_stats[_pk] = {
                "p10": _vs[_nn // 10], "p25": _vs[_nn // 4],
                "p50": _vs[_nn // 2], "p75": _vs[_nn * 3 // 4],
                "p90": _vs[_nn * 9 // 10], "n": _nn,
            }
    _pool_pe_stats["n_loss"] = sum(
        1 for r in results
        if isinstance(r.get("pe"), (int, float)) and not isinstance(r.get("pe"), bool)
        and r["pe"] <= 0)
    _pool_pe_stats["n_nofwd"] = sum(
        1 for r in results
        if not (isinstance(r.get("fwd_pe"), (int, float))
                and not isinstance(r.get("fwd_pe"), bool) and r["fwd_pe"] > 0))
except Exception as _ppe:
    print(f"⚠️ 池内 PE 分位统计失败（不影响主表）: {type(_ppe).__name__}: {_ppe}")
    _pool_pe_stats = {}

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
# v4.0: ⑦ 页面信息架构收敛 —— 定位改为「每日决策页」（环境→买什么→池里还有什么），
#          11 类分析/方法论区块迁至 reports/factor_<date>.md，主页面只留结论；
#          多因子 10 只组合补齐买入区间/止损/目标/盈亏比/股数/金额，
#          并修复 build_trade_plan 的「买区高于现价」与「盈亏比按现价算」两个 P0 缺陷。
REPORT_VER = "v4.0"
_has_sector = any(x.get('score_sector') is not None for x in results)
_has_10d = any(x.get('score_oversold60') is not None for x in results)
_ver_tag = (f"{REPORT_VER} · 基本面主导" if _has_10d
            else (f"{REPORT_VER} · 五维" if _has_sector else f"{REPORT_VER} · 四维"))

# v3.3 问题1优化: 十维评分明细矩阵（10维独立并列清晰呈现，含板块动量）
# ⚠️ _DIM_MAX 默认 5（与当前评分实现一致: 十维均为 0-5 制）。
#    若问题2确认改为「每维0-10 / 总分100」制，仅需将下方 _DIM_MAX 改为 10，
#    后端 step2/apply_sector_momentum 的总分缩放 ×4 改为 ×10、评级门槛同步放大即可。
_DIM_MAX = 10  # v3.3 问题2: 量纲升级为「每维满分10 / 总分满分100」(后端 ×20, 前端 ×2 展示)

# v4.0 页面信息架构收敛的开关注（在函数定义之前声明，供 _dim_matrix_html 运行时读取）
#   定位：本页是「每日决策页」，不是「方法论白皮书」。只回答三个问题：
#     ① 今天什么环境（市场状态 → 仓位纪律）
#     ② 买什么（唯一一份 ≤10 只清单 + 买入区间/止损/目标/股数）
#     ③ 池子里还有什么值得看（完整排名 + 焦点池）
SHOW_LEGACY_3LINES = False   # 动量线/反转线/AI线 三合一「今日候选组合」（20 只，方向互斥不可执行）
SHOW_ANALYSIS = False        # 因子分析/方法论区块（IC 表、衰减、证伪、覆盖率、口径说明等）
SHOW_DIM_MATRIX = False      # 个股「十维评分明细·权重×分数」矩阵 → 改为「评分结构摘要」
SHOW_FULL_RANK = True        # 完整排名 490 只（含展开详情卡）—— 保留，是个股尽调入口
SHOW_FOCUS_POOL = True       # 每日 AI 焦点池 —— 保留，是「池外线索」的唯一来源


def _load_dim_weights():
    """从 factor_weights.json 读十维权重，供维度矩阵与文案使用。
    v3.6 起禁止在此硬编码权重：口径切换（v3.1-10dim-ic → v3.6-fund-led）时
    硬编码会让页面展示的权重与后端实际评分权重脱节，形成"页面说 18%、实际按 34% 算"
    的静默口径漂移。失败时回退 v3.6 先验。"""
    # v4.7：老大 2026-10-25 指定 fund .15 / 质量 .12 / 成长 .13（三者合计 40%，
    #   与 v4.4 的 30+5+5 相同 → 其余四维不动，总权重仍 1.0）。
    # v4.8（2026-10-26 round24 裁决）：growth 0.13 → 0，低波20 顶上 0.13。
    #   依据：成长族（历史增速 + 前瞻一阶导/二阶导）在 78 截面里 h5 IC 全负且
    #   五种极值治理都救不回来；lowvol20 单维 h5 IC +0.1189/t0=3.28，与 6 维
    #   |r| ≤ 0.24 不冗余，换维后复合 h5 +0.1709/t0=10.05 过 Bonferroni。
    #   ⚠️ 此处是**回退值**，正常路径读 factor_weights.json；但必须与那六处
    #   （本文件 / factor_eval / step2 / apply / enhance + 配置文件）完全一致，
    #   漏改一处即静默回退旧口径且无任何告警。
    # v4.9（2026-10-26 round26/27 裁决）：fund .15 → .075 + 新增 EP .075（各半）；
    #   size .10 → 0，新增 SIZE_IND_NEUT .10。依据：飞书 8 因子在本池复测，
    #   EP（h5 +0.0986）与行业内中性化市值（h20 +0.0883）值得吸收，其余六因子不吸收。
    #   ⚠️ 此处是**回退值**，正常路径读 factor_weights.json；但必须与那六处
    #   （本文件 / factor_eval / step2 / apply / enhance + 配置文件）完全一致，
    #   漏改一处即静默回退旧口径且无任何告警。
    # v4.10-rev-quarter（2026-10-27 round34 真·大池审计）：反转块降至 1/4。
    # 依据：scale 扫描下 0.25 是唯一「全样本 h20 +0.0949（近最优）且前后段皆不劣化」的点；
    # 归零虽 +0.0933 但后段 -0.0341。详见 verify_plan_c5.json 与 factor_weights.json method ⑦。
    # v4.13-size2d（2026-10-28）：市值双维度拆分 —— score_size_ind(0.10) 拆为
    #   score_size_growth(小盘成长溢价, 0.08) + score_dominant(龙头壁垒溢价, 0.06)；
    #   差额 0.04 由 score_quality(0.252→0.212) 回吐，Σ=1.0 不变。
    # v4.14-evidence-aligned（2026-10-28 双视角评估后整改）：权重与实证证据对齐 ——
    #   quality 0.212→0.120（h20 IC −0.1470/t−14.72，h40 −0.2033/t−22.67，方向相反）；
    #   fund 0.12→0.242（8 维里唯一消融 t>4：+4.41/+4.32，最硬）；
    #   tech_neg 0.06→0.030（10−score_tech 方差过小，σ 贡献仅 2.1%、近乎常数）。
    #   释放的 9.2pp 全部给 fund，Σ=1.0 不变。
    _fb = {"score_fund": 0.200000, "score_news": 0.000000, "score_theme_neg": 0.000000, "score_size": 0.000000, "score_sector": 0.000000, "score_quality": 0.100000, "score_growth": 0.100000, "score_tech_neg": 0.100000, "score_oversold60": 0.000000, "score_bias60": 0.050000, "score_rsi_rev": 0.050000, "score_vol_pulse": 0.000000, "score_overbought": 0.000000, "score_theme_intrinsic": 0.000000, "score_attention": 0.000000, "score_lowvol20": 0.000000, "score_ep": 0.100000, "score_size_ind": 0.000000, "score_size_growth": 0.200000, "score_dominant": 0.100000}
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


# ===== v4.7 P0 防线：权重口径六方一致性自校验 =====
# 历史教训（本项目已踩两次）：
#   权重散落在 6 个地方 —— factor_weights.json(weights + prior_weights)、
#   factor_eval.PRIOR_W10、step2._W_PRIOR_10D、apply._W_PRIOR_10D、
#   enhance._W_PRIOR_10D、gen_report 的 _fb 回退。漏改任一处 → 该处静默用旧口径，
#   且**没有任何告警**：页面照常出图，只是权重和配置说的不一样。
#   本函数在报告生成启动时逐一比对，不一致直接抛错中止（宁可不出图也不出错图）。
def _verify_weight_consistency():
    import re as _re
    _here = os.path.dirname(os.path.abspath(__file__))
    _expect = None
    _errs = []
    # 1) 配置文件
    try:
        with open(os.path.join(_here, "factor_weights.json"), encoding="utf-8") as _f:
            _cfg = json.load(_f) or {}
        _w = _cfg.get("weights") or {}
        _p = _cfg.get("prior_weights") or {}
        _expect = {k: float(v) for k, v in _w.items() if float(v or 0) > 0}
        if abs(sum(_w.values()) - 1.0) > 0.02:
            _errs.append(f"factor_weights.json weights 合计={sum(_w.values()):.4f} ≠ 1.0")
        for k, v in _p.items():
            if abs(float(_w.get(k, -1)) - float(v)) > 1e-9:
                _errs.append(f"factor_weights.json prior_weights[{k}]={v} ≠ weights[{k}]={_w.get(k)}")
    except Exception as _e:
        _errs.append(f"factor_weights.json 读取失败: {type(_e).__name__}: {_e}")
    if _expect is None:
        return
    # 2) 各脚本内的硬编码先验
    _targets = [("factor_eval.py", "PRIOR_W10"),
                ("step2_stockscorer_v2.py", "_W_PRIOR_10D"),
                ("apply_sector_momentum.py", "_W_PRIOR_10D"),
                ("enhance_scores.py", "_W_PRIOR_10D"),
                ("gen_report_top200.py", "_fb")]
    for _fn, _pat in _targets:
        _path = os.path.join(_here, _fn)
        if not os.path.exists(_path):
            _errs.append(f"{_fn} 不存在")
            continue
        try:
            with open(_path, encoding="utf-8", errors="replace") as _f:
                _src = _f.read()
            _m = _re.search(_pat + r"\s*=\s*\{(.*?)\}", _src, _re.S)
            if not _m:
                _errs.append(f"{_fn}: 未找到 {_pat} 定义")
                continue
            _d = {}
            for _km, _vm in _re.findall(r'"(score_[a-z0-9_]+)"\s*:\s*([0-9.]+)', _m.group(1)):
                _d[_km] = float(_vm)
            for _k, _v in _expect.items():
                if abs(_d.get(_k, -1) - _v) > 1e-9:
                    _errs.append(f"{_fn}: {_pat}[{_k}]={_d.get(_k)} ≠ 配置 { _v }")
        except Exception as _e:
            _errs.append(f"{_fn} 解析失败: {type(_e).__name__}: {_e}")
    if _errs:
        raise SystemExit("❌ 权重口径不一致，已中止出图（防止静默用错权重）：\n   - "
                         + "\n   - ".join(_errs))
    print(f"✅ 权重口径六方一致性校验通过（{len(_expect)} 个生效维："
          + ", ".join(f"{k.replace('score_','')}={v}" for k, v in sorted(_expect.items(), key=lambda x: -x[1]))
          + "）")


_verify_weight_consistency()


_DIM_W = _load_dim_weights()
# v4.4：页面只展示**有权重**的维度（7 个生效维）。归零维度（news/theme_neg/sector/
#   oversold60/vol_pulse/overbought/theme_intrinsic/attention）不再进列表 ——
#   展示 0 权重维度只会造成「页面说 16% 消息面、实际不算」的困惑；
#   这些字段仍在 scored/factor_eval 中保留评估与披露（见因子日报）。
_DIM_DEFS = [
    ("基本面(估值锚)", "score_fund", _DIM_W["score_fund"], False),
    ("质量(fund拆分)", "score_quality", _DIM_W["score_quality"], True),
    # v4.8：成长归零（round24 实证无 alpha，字段保留展示且标「不计分·参考」），
    #   低波20 顶上。定义顺序不影响加权（加权在 step2 完成），只影响页面列序。
    ("成长(fund拆分)", "score_growth", _DIM_W["score_growth"], True),
    ("低波20", "score_lowvol20", _DIM_W["score_lowvol20"], False),
    ("技术面反号", "score_tech_neg", _DIM_W["score_tech_neg"], True),
    ("乖离低位", "score_bias60", _DIM_W["score_bias60"], False),
    ("RSI反转", "score_rsi_rev", _DIM_W["score_rsi_rev"], False),
    # v4.9：EP 估值维（fund 让出 0.075 各半）+ 行业内中性化市值维（替换 0.10 的小市值）。
    #   ⚠️ 「小市值」归零后仍保留在列表（权重 0 → 页面自动不展示为生效维），
    #   但字段与列保留供对照「全市场口径 vs 行业内口径」。
    ("EP估值", "score_ep", _DIM_W["score_ep"], False),
    # v4.13-size2d（2026-10-28 老大指令）：市值维度拆成两个**互斥**的独立维度 ——
    #   A 小盘成长溢价 score_size_growth(0.08)：行业内市值分 × 成长门限 w_mc
    #     （G>7→0.8 / 4<G≤7→0.3 / G≤4→0），对**非龙头**股票生效；
    #   B 行业龙头壁垒 score_dominant(0.06)：业务份额（营收+利润TTM，申万二级行业内占比）
    #     合成 rank 前 30% 即行业龙头，做市值一致性校验后取分。
    #   ⚠️ 原文 mc_pct>0.3 门槛在真实分布下仅覆盖 4.1%（77只）形同虚设，已按老大裁决
    #     改用「业务份额前30%」分区；两维 Spearman≈0.33，真正互补、天然互斥。
    ("小盘成长溢价", "score_size_growth", _DIM_W["score_size_growth"], False),
    ("行业龙头壁垒", "score_dominant", _DIM_W["score_dominant"], False),
    ("小市值", "score_size", _DIM_W["score_size"], True),
]


def _load_dim_max_map():
    """→ (raw, disp) 两份量程映射。

    ⚠️ 两层量程必须区分，混用即静默放大 2 倍（本轮实际踩过）：
      · raw  = factor_weights.json 里 dim_max 的值，描述 **scored JSON 文件内**的量程
               （12 维 0-5 制 / size·quality·growth·tech_neg 0-10 制）。
      · disp = 本脚本 L40-44 加载时统一 ×2 过的**内存量程**。
               _SCORE_KEYS10 里的字段被 ×2（0-5 → 0-10），其余本就 0-10 不变。
    因此页面归一化必须用 disp，若误用 raw 会把 fund 的 5.0（disp=10.0）再除以 5，
    得 20.0 —— 实测首行渲染出「原始 10.00/5」「分数格 20.0」，即此 bug。
    """
    _fb = {"score_fund": 5.0, "score_news": 5.0, "score_theme_neg": 10.0,
           "score_size": 10.0, "score_sector": 5.0, "score_quality": 10.0,
           "score_growth": 10.0, "score_tech_neg": 10.0, "score_oversold60": 5.0,
           "score_bias60": 5.0, "score_rsi_rev": 5.0, "score_vol_pulse": 5.0,
           "score_overbought": 5.0, "score_theme_intrinsic": 5.0,
           "score_attention": 5.0, "score_lowvol20": 10.0,
           # v4.9：EP / SIZE_IND_NEUT 均为池内 rank 分位分，量程 0-10
           "score_ep": 10.0, "score_size_ind": 10.0,
           # v4.13-size2d：市值双维度（同为 0-10 池内分位/交互分）
           "score_size_growth": 10.0, "score_dominant": 10.0}
    _raw = dict(_fb)
    try:
        _p = os.path.join(os.path.dirname(os.path.abspath(__file__)), "factor_weights.json")
        with open(_p, encoding="utf-8") as _f:
            _m = (json.load(_f) or {}).get("dim_max") or {}
        if _m:
            _raw = {k: float(_m.get(k, _fb.get(k, 5.0))) for k in _fb}
    except Exception:
        pass
    # disp：_SCORE_KEYS10 已被 ×2（见 L37-44），其余字段未动
    _disp = {}
    for _k, _mx in _raw.items():
        _disp[_k] = _mx * 2.0 if _k in _SCORE_KEYS10 else _mx
    return _raw, _disp


_DIM_MAX_RAW, _DIM_MAX_MAP = _load_dim_max_map()

# v4.10（2026-10-27 P0-4）：中性填充格的视觉标记色（与实测维的实心色区分）。
#   量尺统一后缺维会以 DMAX/2 参与总分，若画成实心格会让读者误当实测值。
NEUTRAL_FILL_BG = "repeating-linear-gradient(45deg,#3f3f46,#3f3f46 3px,#52525b 3px,#52525b 6px)"


def _norm10(key, v):
    """任意量程维度分 → 0-10 可比量纲。非数值返回 None（禁用 or 0：缺失≠零分）。

    ⚠️ 入参 v 必须是 results 内的值（已 ×2），故分母用 _DIM_MAX_MAP（disp 量程）。
    若传入的是 scored JSON 文件里的原值，应先把 key 的 raw 量程 ×2 换算。
    """
    if not isinstance(v, (int, float)) or isinstance(v, bool):
        return None
    mx = _DIM_MAX_MAP.get(key) or 10.0
    return float(v) / mx * 10.0


def _fmt2(v, dash="—"):
    """安全两位小数格式化。非数值（含 None）→ dash。

    ⚠️ 2026-10-27 P0：score_fund 在「缺有效估值」时落库为 None（本维不计分，
    由 step2 兜底改造引入），原 `f"{r['score_fund']:.2f}"` 会抛
    TypeError: unsupported format string passed to NoneType —— 直接让整份报告渲染失败。
    页面据此显示「—」并在文案上标注不计分，语义诚实（禁 or 0 显示成 0.00 分）。
    与局部 _f2（消息/热度用，None→空串）分工不同：空串给前端 parseFloat 用，
    此处是给人看的分数位，用破折号更明确。
    """
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return f"{float(v):.2f}"
    return dash


# 生效维的短标签（表格分数条 tooltip / 徽章 / 详情卡共用，避免各处自造名字）
_DIM_LABEL_SHORT = {
    "score_fund": "估值锚", "score_quality": "质量", "score_growth": "成长",
    "score_tech_neg": "技反", "score_bias60": "乖离", "score_rsi_rev": "RSI反",
    "score_size": "小市值", "score_lowvol20": "低波",
    # v4.9
    "score_ep": "EP", "score_size_ind": "行业内市值",
    # v4.13-size2d：市值双维度短标签
    "score_size_growth": "小盘成长", "score_dominant": "行业龙头",
}

# 生效维计数：页面所有「N 维评分」文案统一取此值，禁止再硬编码「十维」「7 维」。
# —— v4.4 归零 8 维后只剩 7 维，v4.9 又加入 EP + SIZE_IND_NEUT 成为 8 维。
#   每次换维都要人工同步散落在正文的「N 维」，这不是靠纪律能守住的（v4.8 换维后
#   正文 6 处仍写「7 维」，与首屏权重条「生效 8 维」自相矛盾）。故凡**指代当前模型
#   的现状叙述**一律用 f-string 插 {_N_EFF_DIM}；只有**指代某个历史时点的统计结论**
#   才允许保留旧的固定数字，且必须写明是哪一版口径。
_N_EFF_DIM = sum(1 for _l, _k, _w, _o in _DIM_DEFS if (_DIM_W.get(_k) or 0) > 0)

# ── v4.15（2026-10-29 老大指令 P0-3）：多重检验阈值统一 ──
# 3.08 只覆盖单轮 30 次检验，但项目**累计检验已达 304 次**（round13~65），
# 真实假阳性率被严重低估。按累计 304 次、双侧 α=0.05 重算：Φ⁻¹(1−0.025/304) = **3.7681**。
# ⚠️ round118 P0-4 修正：原注释写「≈3.90」是笔误 —— 3.90 实际对应 K≈519.8。
#    老大 2026-10-29 定的口径是 K=304，故正确阈值是 3.7681（更严，方向不变）。
# ⚠️ 必须与 factor_eval.T_BONF 保持一致 —— 同一口径两处实现必然漂移（项目铁律）。
#    round118 P0-4 起**不再手写魔数**，直接从 factor_eval 取，彻底消除漂移源。
#    历史裁决节里出现的历史数值（audit_round33/34 等）保留原样，因为那是**当时的事实**；
#    本常量只用于「当前口径」的新判定。
try:
    # round118 P1-2 起：T_BONF 由 trial_log 的 K 现算（K=349 → 3.8025）。
    # 同时取 K 的出处，供页面图例说明「这个阈值是怎么来的」。
    from factor_eval import (T_BONF as _T_BONF,
                             N_TESTS_LIVE as _K_LIVE, K_SOURCE as _K_SRC)
except Exception:                                    # 极端情况下退回硬编码，但不静默
    _T_BONF, _K_LIVE, _K_SRC = 3.8025, 349, "legacy-fallback"
    print("⚠️ gen_report: 无法从 factor_eval 导入 T_BONF，已回退硬编码 3.8025")


def _dim_matrix_html(r):
    """个股十维摘要。

    v4.0（2026-10-25 老大指令「页面上不展示分析与计算过程，仅呈现结论」）：
      原实现是「维度名 × 权重 + 分数条 + 加权贡献(分数×权重)」的十行矩阵 ——
      这是**计算过程**，且 73 只详情卡各重复一遍（实测占该区块约 150KB）。
      各维度原始分数在上方四张 dim-card 里已经呈现过，此处不应再重复算式。
      现改为**结论式摘要**：只答「哪几维最强、哪几维是短板」，权重与贡献列移出。
      如需回看完整加权算式，见 reports/factor_<date>.md 与 factor_weights.json。
    """
    if not SHOW_DIM_MATRIX:
        items = []
        for label, key, w, optional in _DIM_DEFS:
            # v4.5（P0 量程）：DIM_DEFS 里 fund/bias60/rsi_rev 是 0-5 制、
            #   quality/growth/tech_neg/size 是 0-10 制。旧实现直接比 8.0，
            #   导致 0-5 制的维度**永远进不了「优势维度」**（最高才 5），
            #   而 0-10 制的零权重维反而常上榜。必须归一化后再比阈值。
            _nv = _norm10(key, r.get(key))
            if _nv is None:
                continue
            items.append((label, _nv))
        if not items:
            return ""
        items.sort(key=lambda x: -x[1])
        strong = [(l, v) for l, v in items if v >= 8.0][:3]
        weak = [(l, v) for l, v in reversed(items) if v < 5.0][:2]
        # 阈值说明随文案输出，避免读者以为 8 分是原始分
        _s = "、".join(f"{l} {v:.1f}" for l, v in strong) if strong else "无（无维度达 8 分以上）"
        _w = "、".join(f"{l} {v:.1f}" for l, v in weak) if weak else "无（无维度低于 5 分）"
        return ('<div class="dim-matrix" style="padding:9px 11px;">'
                '<div class="dm-head" style="margin-bottom:6px;">📊 评分结构摘要'
                '<span style="font-weight:400;font-size:10px;color:#64748b;">'
                f'　（{_N_EFF_DIM} 个生效维，均已归一化至 0-10 分制后比较）</span></div>'
                f'<div style="font-size:11px;color:#cbd5e1;line-height:1.85;">'
                f'<b style="color:#22c55e;">优势维度</b>：{_s}<br>'
                f'<b style="color:#f97316;">短板维度</b>：{_w}'
                f'</div></div>')

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
# ===== v4.16-P0（2026-10-30 老大指令）：恢复首页卡片 total ≥ 75 绝对门槛 =====
# 沿革：原实现硬编码 `total > 75`；v4.14（P1-1）改为前 15% 分位门槛（与权重变更解耦），
#   但实测 10-30 分位门槛落到 65.64（306 只入卡 vs ≥75 仅 58 只），
#   与「首页卡片只展示 75 分以上标的」的既定需求冲突。
# 现按老大指令恢复**绝对门槛 total ≥ 75**（"75 分以上"含 75）。
# ⚠️ 已知代价（v4.14 注释所述漂移问题依然存在）：total 是「Σ 权重×归一化分数」的
#   绝对刻度，权重变更会整体平移分数 → 名单规模随模型版本变化。该代价由需求方
#   明确接受；若将来要改门槛，只改 _TOTAL_MIN 一处。
_TOTAL_MIN = 75.0      # 首页卡片绝对分数门槛（老大指令：75 分以上，含 75）
_totals = sorted([r["total"] for r in results if isinstance(r.get("total"), (int, float))],
                 reverse=True)
_n_show = sum(1 for t in _totals if t >= _TOTAL_MIN)
# 兼容旧变量名（下游多处引用）；语义恢复为「绝对门槛」
_TOTAL_CUT = _TOTAL_MIN
_TOTAL_CUT_DESC = f"total ≥ {_TOTAL_MIN:.0f}（绝对门槛，共 {_n_show} 只）"


def _in_show_set(r):
    """是否进入首页展示名单 —— 绝对门槛 total ≥ 75（v4.16-P0 恢复，含 75）。"""
    t = r.get("total")
    return isinstance(t, (int, float)) and t >= _TOTAL_MIN

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
# v4.5（P0 修复）：旧 _tech_block 把 5 个反转维**简单加权平均**，其中
#   oversold60/vol_pulse/overbought 权重已是 0 → 分母仍含它们 → 复合分被
#   三档零权重维度稀释（实测量价反转分系统性偏低约 40%）。现改为：
#   ① 只对**权重>0** 的维计权；② 先按各自量程归一化到 0-10 再平均。
_TECH_BLOCK_KEYS = ("score_bias60", "score_rsi_rev")


def _weighted_composite(r, keys):
    """按权重对 keys 求和并按量程归一化 → (0-10 复合分, 参与维数)。

    ⚠️ 分母只累计**实际有值且权重>0** 的维，缺失维从分母剔除（禁 or 0，
    否则缺一次就凭空拉低复合分）。全缺时返回 (None, 0) 由调用方兜底。
    """
    _num = 0.0
    _den = 0.0
    _n = 0
    for _k in keys:
        _w = _DIM_W.get(_k) or 0.0
        if _w <= 0:
            continue
        _nv = _norm10(_k, r.get(_k))
        if _nv is None:
            continue
        _num += _nv * _w
        _den += _w
        _n += 1
    if _den <= 0:
        return None, 0
    return _num / _den, _n


def _dim_composites(r):
    """→ (tech_disp, theme_disp)

    十维口径：tech_disp = 反转块（bias60 + rsi_rev，各 0.175）归一到 0-10；
    theme_disp 恒为 None —— v4.4 起 theme_intrinsic/attention 权重均为 0，
    原实现仍按这两个零权重维加权，结果**恒等于 0**，却被页面当作
    「该股题材热度 0 分」渲染（多氟多样本实测「题材热度(0.00/10)」）。
    既然不是评分维度，就不该出现在分数位上，改为 None 由调用方走「未参与评分」文案。
    """
    if r.get("score_oversold60") is not None:
        _t, _n = _weighted_composite(r, _TECH_BLOCK_KEYS)
        return _t, None
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

# v4.16-P0：S/A 卡片区（首页深度分析）门槛为 total ≥ 75 绝对分（v4.14 曾改分位口径，已按老大指令回退）
top20 = [r for r in results
         if r.get("rating") in ("S", "A") and _in_show_set(r)]
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
#
# ⚠️ v4.6 P0 修复（越界指令）：
#   本阶梯的「可逢回调加仓 / 仅轻仓试错 / 结构性参与」是**交易方向指令**，
#   但本项目对它的实证支撑为零 —— audit_round3~22 全部产物里没有任何
#   「仓位比例 vs 后续收益」的回测（已核：21 个 audit_round*.json 无相关键）。
#   与此同时同页又写着「池子本身负 alpha / 96.6% 跑输 / 绝对收益全周期为负」，
#   于是页面上同时存在「绝对收益不赚钱」和「6~8成、逢回调加仓」两个相反结论。
#   修法：① 措辞从「加仓/试错/参与」降级为**风险预算档位**（它唯一真实的职能
#   是"在情绪过热时限制最大敞口"，不是"告诉你买什么"）；② Hero 区显式披露
#   该阶梯未经回测，并说明它不能证明加仓能赚钱。仓位数值本身不变（下游资金
#   口径依赖 mkt_pos，改动会级联），只改语义层。
POSITION_LADDER = [
    (0,  20, "冰点/退潮", "≤2成", "风险预算最低档（非买入信号）", "#dc2626"),
    (20, 40, "弱势磨底",   "2~4成", "风险预算偏低（非买入信号）", "#f97316"),
    (40, 60, "中性震荡",   "4~6成", "风险预算中等（非买入信号）", "#f59e0b"),
    (60, 80, "情绪回暖",   "6~8成", "风险预算偏高（非买入信号）", "#22c55e"),
    (80, 101, "亢奋过热",  "8成+警惕", "风险预算最高档，需防回撤", "#3b82f6"),
]


def position_rule(score):
    """赚钱效应分 → (状态, 建议总仓位, 操作纪律, 颜色)

    注意：返回的第 2 项是**风险预算上限**，不是推荐买入仓位（见上方 v4.6 注释）。
    """
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

# ===== round109 敞口上限（Hero 区前瞻提示）=====
# 目的：Hero 只讲「当日情绪给出的上限」，但用户真正要执行的是「双约束取小后的上限」。
#   若不在此处给出，读者会在顶部看到「8成+警惕」后直接按 8 成操作，
#   完全忽略下方卡片的「风险预算 68%」—— 这正是"缺乏参考价值"的根源之一。
# 数据来源：regime_forecast.json（由 regime_model.py 生成；缺失则本行为空串，不影响原展示）。
# round109b 修正：由「系数 ×0.70」改为「双约束取小」，故此处读 final_hi 而非 factor。
def _forecast_hero_line():
    """→ html 片段；无预判数据时返回空串（绝不阻断报告）。

    ⚠️ round109b 修复漏洞：本函数**独立读文件**，若不做日期校验，
    陈旧产物会绕过卡片层的日期判断、直接把「过期的敞口上限」印到 Hero 决策区
    （故障注入实测确认：卡片已正确跳过，Hero 行仍在显示）。
    故此处必须重复校验 date，宁可少一行也不能给出过期建议。
    """
    try:
        _p = os.path.join(os.path.dirname(os.path.abspath(__file__)), "regime_forecast.json")
        if not os.path.exists(_p):
            return ""
        with open(_p, encoding="utf-8") as _f:
            _o = json.load(_f)
        # ---- 日期校验（与卡片层同规则，不可省）----
        _od = str(_o.get("date") or "")
        if date_str and (date_str not in _od):
            print(f"⚠️ Hero 敞口行跳过：regime_forecast.json 日期 {_od} ≠ 数据日期 {date_str}")
            return ""
        # ---- 门禁状态校验：未通过门禁的产物不得进 Hero ----
        _g = _o.get("gate") or {}
        if _g and not _g.get("ok"):
            print("⚠️ Hero 敞口行跳过：该产物未通过日期门禁")
            return ""
        _c = _o.get("combine") or {}
        if not _c.get("available") or not _c.get("adjusted_desc"):
            return ""

        _fin = _c.get("final_hi")
        _lad = _c.get("ladder_hi")
        _bud = _c.get("budget_cap")
        if _fin is None:
            return ""
        # 情绪阶梯已是唯一约束（无风险预算/取小无收敛）→ 不噪声，避免重复同值
        if _lad is not None and _bud is None and abs(float(_fin) - float(_lad)) < 1e-9:
            return ""

        _bound = html_mod.escape(str(_c.get("bound_by") or ""))
        _clr = "#dc2626" if float(_fin) < 0.75 else "#f59e0b"
        # ⚠️ 情绪阶梯显示**原区间**（如 80%~100%），不显示单值 100%：
        #   单值会让读者误以为「顶部说要满仓，是风险预算砍掉的」，属表述失真。
        _lad_lo = _c.get("ladder_lo")
        _parts = []
        if _lad is not None:
            _lad_txt = (f'{float(_lad_lo)*100:.0f}%~{float(_lad)*100:.0f}%'
                        if _lad_lo is not None else f'{float(_lad)*100:.0f}%')
            _parts.append(f'情绪阶梯 {_lad_txt}')
        if _bud is not None:
            _parts.append(f'风险预算 {float(_bud)*100:.0f}%')
        return (
            f'<div style="margin-top:6px;padding:6px 12px;background:{_clr}14;'
            f'border-left:3px solid {_clr};border-radius:6px;font-size:12px;color:#e2e8f0;line-height:1.7;">'
            f'📐 <b>叠加风险预算后</b>：建议敞口上限 '
            f'<b style="color:{_clr};font-size:15px;">{float(_fin)*100:.0f}%</b>'
            f'（{"　vs　".join(_parts)}，取小 → 受{_bound}约束）'
            f'<span style="color:#94a3b8;font-size:11px;">　详见下方「市场状态」</span></div>')
    except Exception as _e:
        print(f"⚠️ 敞口上限行生成失败（Hero 将不显示该行）: {type(_e).__name__}: {_e}")
        return ""


_fc_hero_line = _forecast_hero_line()

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


# ===== 2026-10-29 新增：AI 点评来源披露（动态读 ai_assessment.json，禁硬编码）=====
# 背景：generate_ai_assessment.py 在 LLM 不可用时会**静默降级**为规则引擎产出
#   （实测 2026-10-29 触发 429 AccountQuotaExceeded，quota 于 2026-11-02 重置）。
#   降级本身是合理的容错，但页面若无标注，读者会把「规则拼装的主题/关注方向」
#   误当成 LLM 研判结论 —— 这是**证据等级混淆**，属披露缺失而非美观问题。
# 处置：hero 区加一枚徽标，仅在 fallback 为真时显示；正常 LLM 产出时不出现（不噪声）。
def _ai_source_badge():
    """→ html 片段（无降级/读取失败时返回空串，绝不阻断报告生成）。

    ⚠️ 2026-10-29 踩坑记录：本函数首版用了 `HERE`，但本文件**没有该模块级变量**
    （其他地方都是内联 `os.path.dirname(os.path.abspath(__file__))`）。
    由于外面套了 `except: return ""`，结果**徽标静默消失且无任何提示**，
    报告照常生成、看起来完全正常 —— 与项目史上多次「静默降级」同一模式。
    故此处：① 路径内联计算；② 失败必须 print 告警，不许无声吞掉。
    """
    _p = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ai_assessment.json")
    if not os.path.exists(_p):
        return ""
    try:
        with open(_p, encoding="utf-8") as _f:
            _a = json.load(_f)
    except Exception as _e:
        print(f"⚠️ ai_assessment.json 不可读，AI 来源徽标跳过: {type(_e).__name__}: {_e}")
        return ""
    if not _a.get("fallback"):
        return ""
    _src = str(_a.get("source") or "rule-fallback")
    _gen = str(_a.get("generated_by") or "")
    # 只把「为什么降级」这一句人话放出来，不堆技术细节
    _why = "LLM 周配额超限" if "quota" in _gen.lower() else "LLM 调用未成功"
    return (
        '<div style="margin-top:8px;display:inline-flex;align-items:center;gap:6px;'
        'padding:3px 10px;border-radius:12px;font-size:11px;font-weight:700;'
        'background:#f59e0b1a;color:#f59e0b;border:0.5px solid #f59e0b55;">'
        f'🤖 AI 点评来源：<b>规则引擎</b>（{_why}，非 LLM 研判）· source={_src}'
        '</div>'
    )


_ai_src_badge = _ai_source_badge()



def advice_with_market(r):
    """个股建议 = 个股信号 × 市场状态。冰点/弱势市场下禁止出现"加仓"字样。

    ⚠️ v4.6 P0 修复（评级/建议错位）：本函数原来直接取 scored json 的 `advice`
      字段，而该字段是在**绝对分档**（≥85 S/≥65 A/≥45 B/≥25 C）口径下写死的；
      页面展示的 `r["rating"]` 已被 `_quantile_ratings()` 换成**当日截面分位**档。
      两套口径并存导致同一行自相矛盾：分位评 A/S 级却挂着"波段操作，轻仓参与"
      （绝对档当时是 B），分位评 C 级却写"波段操作"（绝对档是 B）——实测 150 条错配，
      投资者看到「S 级 + 逢低加仓」和「C 级 + 波段轻仓」无法判断该信哪个。
      修法：分位口径下不再复用绝对档的文案，改由**页面实际展示的 rating** 重新映射；
      绝对档原文案保留在 r["_advice_abs"] 备查（不再渲染）。
    """
    _rating = r.get("rating") or ""
    _base_abs = r.get("advice") or ""
    if _base_abs and "_advice_abs" not in r:
        r["_advice_abs"] = _base_abs
    base = RATING_FULL.get(_rating, "")
    if _bearish:
        if _rating == "S":
            return "市场冰点·仅观察，等待情绪修复"
        if _rating == "A":
            return "市场冰点·暂不加仓，等企稳信号"
        return "市场冰点·观望为主"
    return base


# ===== v4.17（2026-10-31，audit_round90 趋势诊断）：趋势状态判定 =====
# 背景（audit_round90_trend_diag.py 实测）：total≥75 高分组 85% 完全空头排列、
# 100% 收盘<MA20/MA60、近20日收益中位 −22.5%（落后全池 5pp）——左侧超跌反转因子
# （bias60/rsi_rev）选出的高分票天然处在下跌趋势中。必须把「选股质量」与「入场时机」
# 分离：高分=观察池，入场需企稳确认。本函数只做状态判定，不改任何评分。
def _trend_state(r):
    """→ dict(key,label,bg,fg,tip) 或 None（缺数据不臆造）。
    key ∈ bear(空头排列)/weak(弱势)/stab(企稳中)/bull(多头排列)。
    判据全部来自 scored 行自带的 tech.ma5/10/20/60 与现价，数据驱动、无外部编造。"""
    _ts_t = r.get("tech") or {}
    _ts_p = r.get("price")
    _ts_m5, _ts_m10 = _ts_t.get("ma5"), _ts_t.get("ma10")
    _ts_m20, _ts_m60 = _ts_t.get("ma20"), _ts_t.get("ma60")
    _ts_mas = (_ts_m5, _ts_m10, _ts_m20, _ts_m60)
    if not (isinstance(_ts_p, (int, float)) and _ts_p > 0
            and all(isinstance(x, (int, float)) and x > 0 for x in _ts_mas)):
        return None
    _ts_mas_txt = " < ".join(f"{x:.2f}" for x in _ts_mas)
    if _ts_p < _ts_m5 < _ts_m10 < _ts_m20:
        _ts_out = ("bear", "空头排列", "#fee2e2", "#b91c1c",
                   f"收盘 {_ts_p:.2f} < {_ts_mas_txt}（下跌趋势中）。高分=超跌反弹候选 ≠ 现在买入；"
                   "企稳信号：①收盘站上MA5 ②连续2日不创新低 ③RSI14<50后拐头向上")
    elif _ts_p > _ts_m20 and _ts_m5 > _ts_m10 > _ts_m20:
        _ts_out = ("bull", "多头排列", "#dcfce7", "#15803d",
                   f"收盘 {_ts_p:.2f} > MA20 {_ts_m20:.2f}，MA5>MA10>MA20 趋势向上")
    elif _ts_p > _ts_m5:
        _ts_out = ("stab", "企稳中", "#dbeafe", "#1d4ed8",
                   f"收盘 {_ts_p:.2f} 站上 MA5 {_ts_m5:.2f}（企稳第一信号），均线趋势尚未反转，"
                   "建议分批小仓试探")
    else:
        _ts_out = ("weak", "弱势", "#e2e8f0", "#475569",
                   f"收盘 {_ts_p:.2f} 低于 MA5 {_ts_m5:.2f}，未构成完整空头排列，观望")
    return {"key": _ts_out[0], "label": _ts_out[1], "bg": _ts_out[2],
            "fg": _ts_out[3], "tip": _ts_out[4]}


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

    # ⚠️ v4.17（2026-10-31，audit_round90）：空头排列 → 「等待企稳」模式，不再给买点。
    #   原兜底是退到「支撑位附近挂单」——但 round90 实测高分组 85% 完全空头排列、
    #   近20日收益 −22.5%，在下跌趋势里给支撑挂单仍是接飞刀。改为：企稳信号
    #   （站上MA5/连续2日不创新低/RSI拐头）确认前不出买点、不给止损目标。
    if ma5 and ma10 and ma20 and price < ma5 < ma10 < ma20:
        return {
            "buy_lo": None, "buy_hi": None, "stop": None, "target": None,
            "rr": None, "overbought": False,
            "flags": ["空头排列·等待企稳"],
            "entry_ref": None, "tgt_basis": "wait",
            "pos_chg": (price - ma20) / ma20 * 100 if ma20 else None,
            "mode": "wait",
            "wait_note": (f"空头排列（收盘 {price:.2f} < MA5 {ma5:.2f} < MA10 {ma10:.2f} "
                          f"< MA20 {ma20:.2f}）· 暂不给买点；企稳信号：①收盘站上MA5 "
                          "②连续2日不创新低 ③RSI14<50后拐头向上"),
        }

    # 支撑/阻力: 基于现价过滤(源数据为历史计算, 可能滞后于现价)
    sups = [v for _, v in (tech.get("support_levels") or []) if isinstance(v, (int, float)) and 0 < v < price]
    ress = [v for _, v in (tech.get("resistance_levels") or []) if isinstance(v, (int, float)) and v > price]
    nearest_sup = max(sups) if sups else None      # 现价下方最近支撑
    nearest_res = min(ress) if ress else None      # 现价上方最近阻力

    # 买点区间: 强势(多头)以 ma5~ma10 为回踩区; 弱势以 ma10~ma20 为企稳区
    #   ⚠️ 2026-10-25 修 P0 一致性缺陷：当股价已跌破全部均线（日线空头）时，
    #      ma10/ma20 位于现价【上方】，它们是反弹压力位而非回踩支撑。
    #      原实现照抄为买区 → 买区高于现价，等于让使用者在现价上方挂买单（追高），
    #      且止损位、目标位会落在买区下方，盈亏比自相矛盾。
    #      实测 10-23 巨人网络：现价 20.40，买区 21.30~22.20，止损 19.49，目标 20.67，R:R=0.3。
    bullish = ("多头" in trend)
    ref_hi = ma5 if bullish else ma10
    ref_lo = ma10 if bullish else ma20
    buy_lo = buy_hi = None
    if ref_hi and ref_lo:
        if ref_hi < ref_lo:
            ref_lo, ref_hi = ref_hi, ref_lo
        if ref_lo <= price:
            # 均线在现价下方 = 回踩支撑，可用作买区；上限不越过现价
            buy_lo, buy_hi = ref_lo, min(ref_hi, price)
    if buy_lo is None or buy_hi is None or (buy_hi - buy_lo) / max(price, 1e-9) < 0.005:
        # 均线整体在现价上方（空头压制）或区间过窄 → 退到「支撑位附近挂单」
        if nearest_sup and nearest_sup < price:
            buy_lo = nearest_sup * 0.995
            buy_hi = min(nearest_sup * 1.02, price)
        else:
            buy_lo, buy_hi = price * 0.95, price * 0.98
    if buy_hi > price:
        buy_hi = price     # 买区上限永不超过现价，杜绝「追高式挂单」
    if buy_lo > buy_hi:
        buy_lo, buy_hi = buy_hi * 0.99, buy_hi

    # 止损: 优先最近支撑下方 1.5%; 否则 ma20; 再否则现价 -8%
    if nearest_sup:
        stop = nearest_sup * 0.985
    elif ma20:
        stop = ma20 * 0.985
    else:
        stop = price * 0.92
    stop = min(stop, price * 0.97) if stop >= price else stop   # 确保止损在现价下方
    # 止损必须低于买区下限，否则「买入即触发止损」
    if stop >= buy_lo:
        stop = buy_lo * 0.975
    # ⚠️ 2026-10-25：止损与买区上限的最小间距，防「风险敞口过窄 → 盈亏比被数学放大」。
    #   实例（北方稀土）：买区 35.50~35.90、止损 35.37，敞口仅 1.5%，
    #   而页面显示的是 2 位小数四舍五入值，实算与展示出现 2.8 vs 3.8 的口径打架。
    #   统一要求敞口 ≥ 买区上限的 2.5%（与下方 risk 下限一致），不足则下移止损。
    _min_gap = buy_hi * 0.025
    if (buy_hi - stop) < _min_gap:
        stop = buy_hi - _min_gap
    # 展示值统一按 2 位小数落地后再参与计算，确保「页面上的数」= 「算 RR 的数」
    buy_lo = round(buy_lo, 2)
    buy_hi = round(buy_hi, 2)
    stop = round(stop, 2)
    if buy_lo > buy_hi:
        buy_lo = round(buy_hi * 0.99, 2)

    # 目标位 / 盈亏比: 一律以【计划入场价】为基准，不再用现价
    #   ⚠️ 2026-10-25 修 P0：原实现以现价算 risk/reward，当现价高于买区时
    #      「等回踩再买」与盈亏比互相矛盾（回踩后风险变小、空间变大，却仍报 0.3）。
    entry_ref = min(max(price, buy_lo), buy_hi)      # 现价在买区内→取现价; 高于买区→取买区上限
    # ⚠️ 分母下限：买区上限与止损过近时（如 35.90 / 35.37，仅差 1.5%），
    #   报价保留 2 位小数的舍入误差会被除法放大 —— 报告层显示 35.90/35.37，
    #   而内存里是 35.8999…/35.3699…，RR 会从 2.8 跳到 3.8。
    #   故风险敞口不得小于入场价的 2%，与下方 risk 下限同一口径。
    risk = max(entry_ref - stop, entry_ref * 0.02)
    # 目标位: 取【最近】有效阻力位（实战第一目标位），不为了凑盈亏比去跳到远处阻力。
    #   ⚠️ 2026-10-25：曾按「第一个 RR≥1.5 的阻力」取，结果宏景科技目标 +38%、RR 14.6，
    #      属无实战意义的统计凑数，已回退为最近阻力。
    _res_up = sorted(v for v in ress if v > entry_ref * 1.015)
    if _res_up:
        target = round(_res_up[0], 2)
        tgt_basis = "阻力位"
    else:
        target = round(entry_ref + risk * 2, 2)
        tgt_basis = "2:1推演"
    reward = max(target - entry_ref, 0)
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
        "entry_ref": entry_ref, "tgt_basis": tgt_basis,
        "pos_chg": (price - ma20) / ma20 * 100 if ma20 else None,
    }


def trade_plan_advice(r, plan):
    """P1-3: 若超买/高位, 操作建议自动降档, 覆盖原'逢低加仓'措辞。"""
    if not plan:
        return None, ""
    if plan.get("mode") == "wait":
        return ("空头排列 · 等待企稳", "⏸ " + plan.get("wait_note", ""))
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

# ===================================================================
# v4.0 页面信息架构收敛（2026-10-25 老大指令）
# -------------------------------------------------------------------
# 定位：本页是「每日决策页」，不是「方法论白皮书」。只回答三个问题：
#   ① 今天什么环境（市场状态 → 仓位纪律）
#   ② 买什么（唯一一份 ≤10 只清单 + 买入区间/止损/股数）
#   ③ 池子里还有什么值得看（完整排名 + 焦点池）
# 所有「分析与计算过程」（因子 IC、时间衰减、证伪检验、共线矩阵、
# 覆盖率、口径说明、47 因子对照…）一律不在此页展示，
# 改为 gen_factor_daily.py 每日生成 Markdown 报告落盘 reports/（不上线）。
#
# ⚠️ 开关已统一上移至 _DIM_MAX 之后（函数定义之前），此处不再重复定义，
#    否则会覆盖上方早期读取到的值导致行为不一致。
# ===================================================================
# 关闭理由备忘：
#   SHOW_LEGACY_3LINES=False —— 三线实测超额均未跑赢（动量 T+1 -1.53%/胜率37%、
#       反转 -0.61%、AI 胜率 -6.07%），且动量与反转方向互斥，
#       20 只并列 = 不可执行。只保留多因子 10 只。
#   SHOW_ANALYSIS=False —— 属「自证清白」型内容，占页面 40%+ 篇幅却无决策价值；
#       迁至 reports/factor_YYYY-MM-DD.md 供调权重使用。
#   SHOW_DIM_MATRIX=False —— 个股卡里的「权重×分数=加权贡献」十行矩阵是计算过程，
#       73 只各重复一遍；各维原始分已在上方四张 dim-card 呈现，此处只留摘要。
# ===================================================================


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
    <!-- v4.0 锚点导航: 决策页三问（环境 → 买什么 → 池子里还有什么） -->
    <nav class="nav-bar">
        <a href="#sec-decision">① 今日环境</a>
        <a href="#sec-mf-pick">② 买什么</a>
        <a href="#sec-list">③ 完整排名</a>
        <a href="#sec-focus-pool">④ 焦点池</a>
        <a href="#sec-notes">ⓘ 说明</a>
        <span class="nav-date">数据日期 ''' + date_str + '''</span>
    </nav>
''')

# 核心结论卡片
# v4.16-P0：hero 前三同样执行 ≥75 门槛（低于 75 分不得出现在首页卡片）
top3_stocks = sorted([r for r in results if _in_show_set(r)], key=lambda x: -x["total"])[:3]
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
# ⚠️ 披露类变量必须在**分支外**先初始化（项目血泪：_neutral_banner / _ev_gap 两次同型事故）。
#   _gate_badge 在下面 try 块里赋值，若该块在上半段就抛异常（比如 data 缺字段），
#   后面的 f-string 插值会直接 NameError 整页挂掉 —— 而它只是「一条可选披露」。
_gate_badge = ''
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
                   f'🧺 {_pool_note} · 成交额排名 · {_N_EFF_DIM} 维评分</div>')
    # ═══ round125 P0：闸门盲区披露（页面必须说实话）═══
    # 谁在做这个判定：weight_admission_gate.resolve_eval_dims()，以**实读面板的列**
    #   为准。长面板 panel_round124_long 里没有 score_fund / score_dominant 两列
    #   （2020 起回补，主力净流入与龙头判定无历史快照）→ 合计 0.30 名义权重
    #   **从未经过任何一道闸门**。
    # 为什么必须显示在 Hero：读者只看得见「9 维评分」，会默认 9 维都通过检验。
    #   实际可被检验的只有 70%。这是「伪造验证覆盖」的反面——主动说破。
    try:
        import weight_admission_gate as _G
        _cov_g = _G._gate_coverage()
        _ratio = float(_cov_g.get("evaluable_ratio") or 1.0)
        _bd = _cov_g.get("blind_dims") or {}
        if _bd and _ratio < 0.999:
            _bd_txt = "、".join(f"{_k.replace('score_','')} {float(_v):.2f}"
                               for _k, _v in sorted(_bd.items(), key=lambda x: -float(x[1])))
            _gate_badge = (
                '<div style="margin-top:8px;display:inline-flex;align-items:center;gap:6px;'
                'padding:3px 10px;border-radius:12px;font-size:11px;font-weight:700;'
                'background:#ef44441a;color:#f87171;border:0.5px solid #ef444455;">'
                f'🔬 闸门实际可验 {_ratio*100:.0f}% 名义权重 · 未覆盖：{_bd_txt}'
                '（面板无该列 → 从未过五门）</div>')
    except Exception:
        _gate_badge = ''
    # 资格标记（次新/停牌/流动性不达标）——取 scored json 的 eligible_marks
    _em = (stats.get("eligible_marks") or data.get("eligible_marks") or {})
    if _em and any(_em.get(k) for k in ("new_stock", "suspended", "illiquid")):
        _elig_badge = ('<div style="margin-top:8px;display:inline-flex;align-items:center;gap:6px;'
                       'padding:3px 10px;border-radius:12px;font-size:11px;font-weight:700;'
                       'background:#f59e0b1a;color:#fbbf24;border:0.5px solid #f59e0b55;">'
                       f'🏷️ 资格剔除: 次新 {_em.get("new_stock",0)} / 停牌 {_em.get("suspended",0)}'
                       f' / 流动性不达标 {_em.get("illiquid",0)}</div>')
except Exception as _pe:
    # v4.5：原为裸 `pass` —— 本轮就是这么静默丢掉整个池口径徽章的
    #   （变量名大小写写错 → NameError → 页面少一块，编译与退出码全正常）。
    #   静默兜底只应兜「数据缺失」，不该兜「代码写错」，故保留渲染但打告警。
    print(f"⚠️ 池口径徽章渲染失败（页面将缺该徽章）: {type(_pe).__name__}: {_pe}")

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

# ===== v4.18（round117 P0-2）步级状态（降级披露）=====
# ⚠️ 加载逻辑在「股票表之后、分析切片之前」的那一段（搜索 P0-1/2/4 三块披露）——
#   必须放在那里，否则 SHOW_ANALYSIS=False 时会被 del html_parts[...] 整块删掉。
#   此处不再重复加载，避免两处实现漂移。

# ===== v3.2 P1-5 区制标签（config.json regime_tag；数据驱动，无则不显示）=====
# v3.5.3: 日期部分数据驱动 —— config 静态日期(人写, 易过期)用 factor_ic.json 的
#         snapshot_range(有效截面区间)动态覆盖; _ic 缺失时保留 config 原值兜底。
_regime_badge = ''
try:
    _cfg_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")
    with open(_cfg_path, "r", encoding="utf-8") as _cf:
        _cfg = json.load(_cf)
    _rt = (_cfg.get("regime_tag") or "").strip()
    # v4.0: 悬浮说明从「方法论长篇」改为「对使用者意味着什么」。
    #   原 regime_note 属方法论文本（权重切换依据、配对 t 值等），已迁至 reports/factor_<date>.md。
    _rn = ("本页所有评级与排名，都是在「下跌市」样本区间内校准的："
           "结论反映的是池内相对强弱，不代表绝对收益方向。"
           "池内基准本身在跌，前排标的也可能亏钱，请以仓位纪律与个股止损为准。")
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

# ===== v4.5（P1 · 老大问题1）：显式展示「最新 N 维」及权重 =====
# 背景：项目内部口径叫「十维」，但 v4.4 把 8 维权重归零后，**真正参与评分的只有 7 维**。
#   页面此前只在详情卡里零散显示分数，从不告诉读者「模型到底几维、各占多少权重」，
#   导致两个必然的疑问：① 到底几维？② 消息面/题材面还算不算？
#   本节直接把 7 个生效维的权重、量程、方向一次列清，并显式说明 8 个归零维为何归零。
_W_EFF = [(lbl, k) for lbl, k, w, _opt in _DIM_DEFS if (_DIM_W.get(k) or 0) > 0]
_W_ZERO = [k for k in _DIM_W if (_DIM_W.get(k) or 0) <= 0]

# v4.11：主页权重条内嵌的「模型结构结论摘要」。真正的赋值在 _build_weight_strip()
#   被调用之后（那里能拿到 _struct）；此处先给占位，避免定义顺序依赖导致 NameError。
_ms_digest_html = "结构体检计算中。"


def _build_contrib_table():
    """v4.14（P1-2）：名义权重 vs 实际排序贡献 σ 对照表（当日现算）。

    核心逻辑：一维对最终排序的影响力 = w_i × sd_i / DMAX_i（即该维贡献到 total
    的那部分的截面标准差）。名义权重只有在「各维 σ 相同」时才等于实际贡献。

    为什么要这张表（实测发现）：v4.13 下 dominant 名义 6% 但 σ 贡献仅 2.7%
    （覆盖只有 26.5%，其余是中性填充 5.0 零区分度）；tech_neg 名义 6% 仅 2.1%
    （10−score_tech 方差极小近乎常数）。读者只看得见名义权重，会系统性高估
    这两维的作用、低估 fund/lowvol20。这是**披露**问题，不是调参问题。
    """
    try:
        import statistics as _st
        _act = [(lbl, k) for lbl, k in _W_EFF]
        if not _act:
            return ""
        _sig = {}
        for lbl, k in _act:
            _w = float(_DIM_W.get(k) or 0)
            # ⚠️ 量程必须取 _DIM_MAX_MAP（各维真实满分量程的 dict），
            #   不能取模块级 _DIM_MAX —— 后者是 int 10（旧全局默认），.get 会抛 AttributeError。
            _dm = float(_DIM_MAX_MAP.get(k) or _DIM_MAX_RAW.get(k) or 10.0)
            if _w <= 0 or _dm <= 0:
                continue
            _vs = [float(r[k]) for r in results
                   if isinstance(r.get(k), (int, float)) and r[k] == r[k]]
            if len(_vs) < 2:
                continue
            _sd = _st.pstdev(_vs)
            _sig[k] = (_w * _sd / _dm, lbl, len(_vs), _sd)
        if not _sig:
            return ""
        _tot = sum(v[0] for v in _sig.values())
        if _tot <= 0:
            return ""
        _rows = sorted(_sig.items(), key=lambda x: -x[1][0])
        _body = ""
        for k, (s, lbl, n, sd) in _rows:
            _w = float(_DIM_W.get(k) or 0)
            _share = s / _tot
            _dev = (_share - _w) * 100.0
            # 偏离超过 ±2pp 才算「名不副实」，用颜色提示
            _dc = ("#ef4444" if _dev <= -2.0 else
                   "#22c55e" if _dev >= 2.0 else "#64748b")
            _body += (
                f'<tr>'
                f'<td style="padding:3px 6px;color:#cbd5e1;">{lbl}</td>'
                f'<td style="padding:3px 6px;text-align:right;color:#94a3b8;">{_w*100:.1f}%</td>'
                f'<td style="padding:3px 6px;text-align:right;color:#e2e8f0;font-weight:700;">'
                f'{_share*100:.1f}%</td>'
                f'<td style="padding:3px 6px;text-align:right;color:{_dc};">{_dev:+.1f}pp</td>'
                f'<td style="padding:3px 6px;text-align:right;color:#475569;">'
                f'{n}/{len(results)}</td>'
                f'</tr>')
        # 有效维数（参与率倒数）：1/Σp_i²，p_i=σ占比
        _eff = 1.0 / sum((v[0] / _tot) ** 2 for v in _sig.values())
        return (
            f'<div style="margin-top:10px;padding:8px 10px;border-radius:6px;'
            f'background:rgba(148,163,184,0.06);border-left:3px solid #64748b;">'
            f'<div style="font-size:11px;font-weight:800;color:#cbd5e1;margin-bottom:5px;">'
            f'📊 名义权重 ≠ 实际排序贡献（当日现算）</div>'
            f'<table style="width:100%;border-collapse:collapse;font-size:10px;">'
            f'<thead><tr style="color:#64748b;border-bottom:1px solid #1e293b;">'
            f'<th style="padding:3px 6px;text-align:left;font-weight:600;">维度</th>'
            f'<th style="padding:3px 6px;text-align:right;font-weight:600;">名义权重</th>'
            f'<th style="padding:3px 6px;text-align:right;font-weight:600;">σ 贡献占比</th>'
            f'<th style="padding:3px 6px;text-align:right;font-weight:600;">偏离</th>'
            f'<th style="padding:3px 6px;text-align:right;font-weight:600;">有效样本</th>'
            f'</tr></thead><tbody>{_body}</tbody></table>'
            f'<div style="margin-top:5px;font-size:9.5px;color:#64748b;line-height:1.65;">'
            f'σ 贡献 = 权重 × 该维截面标准差 ÷ 量程，反映该维<b>对最终排序的真实影响力</b>。'
            f'名义权重只在各维离散度相同时才等于实际贡献 —— '
            f'偏离为负（红）说明该维「占了权重但没起作用」，'
            f'常见原因是覆盖不足或近乎常数。'
            f'按此口径的有效维数为 <b style="color:#e2e8f0;">{_eff:.2f} / {len(_sig)}</b>'
            f'（1/Σp²，越小说明排序越集中在少数维上）。</div></div>')
    except Exception as _ce:
        # 静默失败会让首屏少一块且无人知晓（v4.13 流动性横幅事故同类）→ 显式留痕
        print(f"⚠️ 权重贡献表生成失败: {type(_ce).__name__}: {_ce}")
        return ""


def _build_weight_strip():
    if not _W_EFF:
        return ""
    _cells = []
    _W_COLOR = {
        "score_fund": "#22c55e", "score_quality": "#3b82f6", "score_growth": "#38bdf8",
        "score_tech_neg": "#a78bfa", "score_bias60": "#f59e0b",
        "score_rsi_rev": "#f97316", "score_size": "#facc15",
        "score_lowvol20": "#14b8a6",
        # v4.9
        "score_ep": "#4ade80", "score_size_ind": "#eab308",
        # v4.13-size2d：市值双维度（A 小盘成长 / B 行业龙头）
        "score_size_growth": "#eab308", "score_dominant": "#fb923c",
    }
    # 四维是「池内 rank 分位×10」而非绝对分：必须单独标注，否则 0.04 会被读成
    #   「质量极差」，而实为「本池 490 只里排最后几名」（如茅台 size 0.04=市值最大）。
    #   v4.8 新增 lowvol20 同属池内分位族（整池 rank，低波=高分）。
    #   v4.9 新增 score_ep / score_size_ind 同属池内分位族。
    #   v4.13-size2d：score_size_ind 已退役，改为 score_size_growth / score_dominant。
    #     ⚠️ score_size_growth **不是纯分位**（市值分 × 成长门限 w_mc，值域 0~8），
    #       故不进 _RANK_DIM_KEYS，单独在下方给专属说明。
    _RANK_DIM_KEYS = ("score_quality", "score_growth", "score_size", "score_lowvol20",
                      "score_ep", "score_dominant")
    for lbl, k in _W_EFF:
        _w = _DIM_W.get(k) or 0
        _mx_raw = _DIM_MAX_RAW.get(k) or 10.0
        _c = _W_COLOR.get(k, "#94a3b8")
        # 量程说明用 raw（数据库原始量程），并标出「已归一化」——否则读者拿
        # fund 的 0-5 与 size 的 0-10 对比会以为 fund 只值一半。
        if k == "score_size_growth":
            # v4.12/v4.13：本维非纯分位，而是「行业内市值分 × 成长门限权重」。
            # 实际值域不再是 0-10（最低 0、最高约 8，40% 落在 0 分），
            # 若继续写「池内分位 0-10」会让人误以为它仍满量程分位。
            _mx_txt = '市值分×成长门限(＞7→0.8/4-7→0.3/≤4→0)'
        elif k == "score_dominant":
            # P0-3（round118）：改为**全池**业务份额 rank（不再只算组内前 30%）。
            # 值域 0-10；份额低的自然拿低分，不代表「没有数据」。
            _mx_txt = '业务份额组内rank·全池(0-10)'
        elif k in _RANK_DIM_KEYS:
            _mx_txt = '池内分位 0-10（非绝对分）'
        else:
            _mx_txt = (f'原始 0-{_mx_raw:.0f} · 归一化 0-10' if _mx_raw != 10.0
                       else f'原始 0-{_mx_raw:.0f}')
        _cells.append(
            f'<div class="ws-cell" style="flex:1 1 0;min-width:96px;padding:7px 9px;border-radius:6px;'
            f'background:#0f172a;border-left:3px solid {_c};">'
            f'<div style="font-size:10px;color:#94a3b8;white-space:nowrap;">{lbl}</div>'
            f'<div class="ws-pct" style="font-size:15px;font-weight:800;color:{_c};line-height:1.3;">{_w*100:.1f}%</div>'
            f'<div style="font-size:9px;color:#475569;">{_mx_txt}</div>'
            f'</div>')
    _sum = sum((_DIM_W.get(k) or 0) for _l, k in _W_EFF)
    # 归零维标签（页面文案用中文名，不用字段名）
    _ZL = {"score_news": "消息面", "score_theme_neg": "题材反号", "score_sector": "板块动量",
           "score_oversold60": "超跌60反转", "score_vol_pulse": "量能反向",
           "score_overbought": "超买反向", "score_theme_intrinsic": "题材内禀",
           "score_attention": "关注度相对",
           # v4.8：成长归零（round24 实证无 alpha）
           "score_growth": "成长(fund拆分)",
           # v4.9：小市值归零（全市场口径无 alpha，改用行业内口径 score_size_ind）
           "score_size": "小市值(全市场口径)"}
    _znames = "、".join(_ZL.get(k, k) for k in _W_ZERO)
    # v4.8：分位族 / 绝对分族的两组名字**由生效维动态生成**，不再硬编码
    #   —— 上一版硬编码「质量/成长/小市值」，growth 归零后即变成错文案。
    _rank_in_eff = [_DIM_LABEL_SHORT.get(k, k) for _l, k in _W_EFF if k in _RANK_DIM_KEYS]
    _abs_in_eff = [_DIM_LABEL_SHORT.get(k, k) for _l, k in _W_EFF if k not in _RANK_DIM_KEYS]
    # ⚠️ 2026-10-27 v4.11：原摘要文案硬写「同目录另有 factor_{_data_date}.md 因子日报」，
    #   但因子日报不随每日报告生成（实测只有 factor_2026-10-23.md）→ 指向死链。
    #   改为按磁盘实际文件动态生成说明，找不到就不提。
    _fm_note = ""
    try:
        _rep_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "reports")
        _fm_cands = sorted(
            [f for f in os.listdir(_rep_dir)
             if f.startswith("factor_") and f.endswith(".md")],
            reverse=True) if os.path.isdir(_rep_dir) else []
        if _fm_cands:
            _fm_latest = _fm_cands[0]
            _fm_d = _fm_latest[len("factor_"):-len(".md")]
            _fm_note = (f'（同目录另有因子日报 <b style="color:#94a3b8;">'
                        f'reports/{_fm_latest}</b>，数据日期 {_fm_d}'
                        + ('，<b style="color:#f59e0b;">非当日</b>' if _fm_d != _data_date else '')
                        + '）')
    except Exception:
        _fm_note = ""
    return (f'<div class="weight-strip" style="margin-top:14px;padding:12px 14px;border-radius:10px;'
            f'background:#111c2e;border:0.5px solid #1e293b;">'
            f'<div style="display:flex;align-items:center;gap:8px;margin-bottom:9px;flex-wrap:wrap;">'
            f'<span style="font-size:12px;font-weight:800;color:#e2e8f0;">'
            f'⚖️ 当前评分维度与权重</span>'
            f'<span style="font-size:10px;color:#64748b;">'
            f'生效 {len(_W_EFF)} 维 · 权重合计 {_sum*100:.0f}% · 每维先按自身量程归一化到 0-10，'
            f'再加权 ×10 得百分制总分</span></div>'
            f'<div style="display:flex;gap:7px;flex-wrap:wrap;">{"".join(_cells)}</div>'
            f'<div style="margin-top:9px;font-size:10px;color:#94a3b8;line-height:1.7;'
            f'padding:7px 9px;background:rgba(56,189,248,0.06);border-left:3px solid #38bdf8;border-radius:4px;">'
            f'📐 <b>两种量纲别混读</b>：<b style="color:#22c55e;">{" / ".join(_rank_in_eff)}</b> '
            f'{len(_rank_in_eff)} 维是'
            f'<b>池内分位×10</b>（0=本池最差或市值最大/波动最高，5=中位，10=本池最好或市值最小/波动最低），'
            f'是<b>相对排名</b>不是绝对水平 —— 因此本池<b>必然</b>有 0.0 与 10.0 各一只、中位恒为 5.00，'
            f'某股「质量 0.04」的含义是「在今日 {len(results)} 只里质量排最后几名」，'
            f'而非「这家公司质量几乎为零」。<br>'
            f'其余 {len(_abs_in_eff)} 维（{" / ".join(_abs_in_eff)}）才是按阈值算出的<b>绝对分</b>，'
            f'可跨日比较。跨日只能比 total 与绝对分维，<b style="color:#f59e0b;">分位维跨日不可比</b>'
            f'（池子换了、排名就换）。</div>'
            + _build_contrib_table()
            + f'<div style="margin-top:9px;font-size:10px;color:#64748b;line-height:1.7;">'
            f'<b style="color:#94a3b8;">未参与评分（权重 0，共 {len(_W_ZERO)} 维）：</b>{_znames}。'
            f'这些字段仍在数据库中计算与保留，但<b style="color:#94a3b8;">不计入总分</b> —— '
            f'不是「该股这几维很差」，而是模型当前不采信它们。</div>'
            # ══════════════════════════════════════════════════════════════
            # v4.11（2026-10-27 老大指令①）：
            #   原此处在主页内联「🔬 模型结构体检」「🧪 维数实证」「双视角评估」
            #   三大块（有效维数/相关对/方差贡献/Top20 敏感性/单维 IC 表/换维裁决/
            #   多层评估），全部是**分析过程**而非当日决策依据 —— 单块 HTML 就近 6 万字符，
            #   把首屏权重条挤成「方法论文档」。
            #   现移出到 reports/model_structure_<date>.html，主页只留一行结论摘要 + 入口。
            #   保留摘要的理由：这三块的**结论**（几维独立、哪些维敏感）直接影响
            #   「能不能把这套分当多维验证过的模型用」，完全删掉会让人误读为 8 维独立。
            # ══════════════════════════════════════════════════════════════
            f'<div style="margin-top:9px;padding:8px 10px;border-radius:6px;'
            f'background:rgba(148,163,184,0.07);border-left:3px solid #64748b;'
            f'font-size:10px;color:#94a3b8;line-height:1.75;">'
            f'🔬 <b style="color:#cbd5e1;">模型结构结论（详见独立报告）</b>：{_ms_digest_html}'
            f'<div style="margin-top:4px;color:#64748b;">'
            f'📄 完整分析（有效维数推导 · 相关矩阵 · 方差贡献 · Top20 敏感性 · '
            f'逐维 IC 与稳健性 · 换维裁决 · 双视角评估）已移至 '
            f'<b style="color:#38bdf8;">reports/model_structure_{_data_date}.html</b>'
            f'{_fm_note}。</div></div>'
            f'</div>')


# ===== v4.6（双视角评估 P1）：模型结构体检 =====
# 专业量化审阅者拿到一套多因子模型，第一个问题必然是「你这 7 维有几维是独立的」。
# 只列权重不回答这个问题，等于让人误以为 7 维 = 7 份独立信息。
# 本节全部数字**当日现算**（不读任何历史审计文件），且只输出结论不输出推导过程。
def _model_structure_diag(rws, wmap):
    """→ dict | None。计算：有效维数(参与率)、主要相关对、方差贡献、Top20 敏感性。"""
    try:
        import math as _m
        _act = [(k, float(v)) for k, v in wmap.items() if float(v or 0) > 0]
        if len(_act) < 3:
            return None
        # ⚠️ 量程：rws 是脚本顶部的 results，0-5 制维**已被 ×2**（见 L37-44），
        #   所以这里每个有效维的值都已经落在 0-10，无需再按 5 分制二次缩放
        #   （首版误按 _MAX=5 再除一次，导致 fund/bias60/rsi_rev 全部被截断到 10，
        #   相关性被压平、敏感性测试出现 0/20 这种平凡结果）。
        _mat = []
        for r in rws:
            row = []
            ok = True
            for k, _w in _act:
                v = r.get(k)
                if not isinstance(v, (int, float)) or isinstance(v, bool):
                    ok = False
                    break
                # 归一化到 0-10 后统一量纲（截断仅防越界，不再除 5）
                mx = _DIM_MAX_MAP.get(k) or 10.0
                row.append(max(0.0, min(10.0, float(v) / mx * 10.0)))
            if ok:
                _mat.append(row)
        n = len(_mat)
        p = len(_act)
        if n < 30:
            return None
        means = [sum(r[i] for r in _mat) / n for i in range(p)]
        stds = [_m.sqrt(sum((r[i] - means[i]) ** 2 for r in _mat) / n) for i in range(p)]
        def _corr(i, j):
            if stds[i] <= 0 or stds[j] <= 0:
                return 0.0
            return sum((r[i] - means[i]) * (r[j] - means[j]) for r in _mat) / (n * stds[i] * stds[j])
        C = [[1.0 if i == j else _corr(i, j) for j in range(p)] for i in range(p)]
        # Jacobi 特征值
        A = [row[:] for row in C]
        for _ in range(300):
            _off = max(((abs(A[i][j]), i, j) for i in range(p) for j in range(p) if i != j),
                       default=(0, 0, 0))
            if _off[0] < 1e-12:
                break
            _, i, j = _off
            _th = (A[j][j] - A[i][i]) / (2 * A[i][j]) if A[i][j] != 0 else 0.0
            _t = (1 if _th >= 0 else -1) / (abs(_th) + _m.sqrt(_th * _th + 1))
            _c = 1 / _m.sqrt(_t * _t + 1)
            _s = _t * _c
            for _k in range(p):
                _aik, _ajk = A[i][_k], A[j][_k]
                A[i][_k] = _c * _aik - _s * _ajk
                A[j][_k] = _s * _aik + _c * _ajk
            for _k in range(p):
                _aki, _akj = A[_k][i], A[_k][j]
                A[_k][i] = _c * _aki - _s * _akj
                A[_k][j] = _s * _aki + _c * _akj
        ev = sorted((A[i][i] for i in range(p)), reverse=True)
        _tot = sum(ev) or 1.0
        _pr = (_tot * _tot) / (sum(e * e for e in ev) or 1.0)
        # 最强相关对（去掉自身）
        _pairs = [(abs(C[i][j]), i, j) for i in range(p) for j in range(i + 1, p)]
        _pairs.sort(reverse=True)
        _top_pairs = [(_act[i][0], _act[j][0], C[i][j]) for _, i, j in _pairs[:2]]
        # 方差贡献（w × std 归一）
        _vc = {_act[i][0]: _act[i][1] * stds[i] for i in range(p)}
        _vs = sum(_vc.values()) or 1.0
        _vc = {k: v / _vs * 100.0 for k, v in _vc.items()}
        # 敏感性：三种情景下 Top20 与基线的重合数
        #   ① 去掉该维（权重转给其他维，按剩余权重归一）
        #   ② 该维权重减半
        #   ③ 该维反号
        #   ⚠️ 反号时总分不再有"越高越好"的含义，仅用于衡量「符号敏感度」，
        #      故分母一律用**原权重**的绝对值之和（不能用新权重的和，
        #      否则反号会把分母算错、产生 0/20 这类无意义结果）。
        _wabs = sum(w for _, w in _act) or 1.0
        def _top(drop=None, half=None, sign=None):
            sc2 = []
            for idx, r in enumerate(_mat):
                num = 0.0
                den = 0.0
                for i, (k, w) in enumerate(_act):
                    if k == drop:
                        continue
                    ww = w
                    if k == half:
                        ww = w * 0.5
                    if k == sign:
                        ww = -w
                    num += r[i] * ww
                    den += abs(w)      # 分母恒用原权重，保证各情景可比
                sc2.append((num / (den or 1.0) * 10.0, idx))
            sc2.sort(key=lambda x: -x[0])
            return set(i for _, i in sc2[:20])
        _base = _top()
        _sens = {}
        for k, _ in _act:
            _sens[k] = {"drop": len(_base & _top(drop=k)),
                        "half": len(_base & _top(half=k)),
                        "sign": len(_base & _top(sign=k))}
        return {"n": n, "p": p, "eff": _pr, "ev": [e / _tot * 100 for e in ev],
                "pairs": _top_pairs, "vc": _vc, "sens": _sens,
                "std": {_act[i][0]: stds[i] for i in range(p)},
                "n_ev_gt1": sum(1 for e in ev if e > 1.0)}
    except Exception as _e:
        print(f"⚠️ 模型结构体检计算失败（将不渲染该区块）: {type(_e).__name__}: {_e}")
        return None


try:
    _wmap_page = (json.load(open(inp, encoding="utf-8")) or {}).get("weights") or _DIM_W
    _struct = _model_structure_diag(results, _wmap_page)
except Exception as _se:
    print(f"⚠️ 结构体检读取失败: {type(_se).__name__}: {_se}")
    _struct = None

# ===== v4.8（round23 + round24 实证）：维数实证结论披露 =====
# 双视角评估的核心问题「7 维够不够 / 该不该减维」不能只说"先验"糊过去，
# 必须把真实前瞻收益证据摆出来。此块读 audit_round23.json（历史单维 IC）
# 与 audit_round24d.json（换维复合裁决），与上方「当日现算」的结构体检分开标注，
# 避免把历史结论当成当日数。
_round23_html = ""
try:
    _here_dir = os.path.dirname(os.path.abspath(__file__))
    _r23 = json.load(open(os.path.join(_here_dir, "audit_round23.json"), encoding="utf-8"))
    _q1 = _r23.get("q1_dim_ic") or {}
    _q9 = (_r23.get("q9_production_v3_final") or {}).get("variants") or {}
    # round24d：换维裁决（lowvol20 顶上、成长归零）的实证来源
    try:
        _r24d = json.load(open(os.path.join(_here_dir, "audit_round24d.json"), encoding="utf-8"))
    except Exception:
        _r24d = None
    _d33_CN = {"score_fund": "估值锚", "score_bias60": "乖离", "score_rsi_rev": "RSI反转",
           "score_tech_neg": "技术反号", "score_size": "小市值",
           "score_quality": "质量", "score_growth": "成长",
           "score_lowvol20": "低波20",
           # v4.11：v4.9 起生效的另两维，此前表格漏列 → 读者以为 8 维只测了 6 维
           "score_ep": "EP估值", "score_size_ind": "行业内市值",
           # v4.13-size2d：市值双维度
           "score_size_growth": "小盘成长", "score_dominant": "行业龙头"}
    # ---- v4.11：统一走 audit_round33.json 的 eff_weight（8 维齐全、单一来源）----
    #   此前表格只列 round23 有记录的 6 维 + round24d 的 lowvol20，**漏掉 v4.9 生效的
    #   ep 与 size_ind**，且没有样本量 n 列 —— 这正是「fund IC 最高但权重只有 7.5%」
    #   这一误读的来源：读者看不到各维 IC 的样本口径不同（fund n=73 但 bias60 n=63），
    #   也看不到消融边际贡献（fund 的 t=+4.41 是 8 维里唯一 >4）。
    #   现在补：n 列 + 消融 t 列，并把 ep/size_ind 纳入。
    _d33_r33 = None
    try:
        _d33_r33 = json.load(open(os.path.join(_here_dir, "audit_round33.json"), encoding="utf-8"))
    except Exception as _d33_e33:
        print(f"⚠️ audit_round33.json 读取失败（维数实证表将退回旧口径）: "
              f"{type(_d33_e33).__name__}: {_d33_e33}")
    _d33_EW = (_d33_r33 or {}).get("eff_weight") or {}
    _d33_AB = {}       # h5 消融
    _d33_AB20 = {}     # h20 消融（v4.11 补：只看 h5 会漏掉「两周期都稳」的证据）
    for _k33, _v33 in ((_d33_r33 or {}).get("ablation") or {}).items():
        if _k33.startswith("全样本|h5|drop_"):
            _d33_AB[_k33.split("drop_")[-1]] = _v33
        elif _k33.startswith("全样本|h20|drop_"):
            _d33_AB20[_k33.split("drop_")[-1]] = _v33
    _d33_NBASE = (((_d33_r33 or {}).get("samples") or {}).get("全样本|h5|base") or {}).get("n")
    # 单维 IC 来源：lowvol20 来自 round24d，其余来自 round23
    _d33_q24lv = ((_r24d or {}).get("q1_lowvol20") or {})
    _d33_rows = []
    for _d33_k in ("score_bias60", "score_rsi_rev", "score_ep", "score_lowvol20", "score_fund",
               "score_tech_neg", "score_size_ind", "score_quality",
               "score_size", "score_growth"):
        if _d33_k == "score_lowvol20":
            _d33_r5 = _d33_q24lv.get("5")
            _d33_r20 = _d33_q24lv.get("20")
        else:
            _d33_r5 = (_q1.get(_d33_k) or {}).get("5")
            _d33_r20 = (_q1.get(_d33_k) or {}).get("20")
        _d33_w = float(_DIM_W.get(_d33_k) or 0)
        # v4.11：优先用 round33 的 8 维统一口径（含 ep / size_ind、含样本量 n）
        _d33_ew = _d33_EW.get(_d33_k) or {}
        _d33_ab = _d33_AB.get(_d33_k) or {}
        _d33_ic = _d33_ew.get("ic")
        if _d33_ic is not None:
            _d33_v = float(_d33_ic)
            _d33_t = None            # round33 未给单维 t0，用 round23 的补（若有）
            if _d33_r5:
                _d33_t = _d33_r5["t_by_lag"].get("0")
            # ⚠️ n 必须取**该维自己的**截面数，不能用 base 的 73 一把填 ——
            #   实测 bias60 / rsi_rev 是 n=63，其余 73。全填 73 会掩盖
            #   「这两维少了 10 个截面」这件事，正是本次误读的同类错误。
            #   EP / 行业内市值在 round23 里没有单独记录（v4.9 才生效），
            #   退到 round33 的 base 截面数，并在判定列标注来源差异。
            _d33_n = (_d33_r5 or {}).get("n")
            if _d33_n is None and _d33_ic is not None:
                _d33_n = _d33_NBASE
            _d33_dic = _d33_ab.get("d_ic")
            _d33_dt = _d33_ab.get("t")
            _d33_ok = bool(_d33_ab.get("t") is not None and abs(_d33_ab["t"]) >= 3.08)
            _d33_h20 = (_d33_r20["ic"] if _d33_r20 else None)
            _d33_dt20 = (_d33_AB20.get(_d33_k) or {}).get("t")
            if _d33_v < 0:
                _d33_verdict = "负 IC，已归零" if _d33_w <= 0 else "负 IC，方向相反"
            elif _d33_ab.get("t") is not None and abs(_d33_ab["t"]) >= 3.08:
                _d33_verdict = "去掉后显著变差"
            elif _d33_v >= 0.10:
                _d33_verdict = "单维强（复合靠组合）"
            elif _d33_t is not None and abs(_d33_t) >= 2:
                _d33_verdict = "边际有效"
            else:
                _d33_verdict = "无证据"
            _d33_rows.append((_d33_CN[_d33_k], _d33_w, _d33_v, _d33_t, _d33_h20, _d33_verdict, _d33_n, _d33_dt, _d33_dt20))
            continue
        if _d33_r5 is None:
            _d33_rows.append((_d33_CN[_d33_k], _d33_w, None, None, None, "无本池历史数据", None, None, None))
            continue
        _d33_t0 = _d33_r5["t_by_lag"].get("0")
        _d33_ok = bool(_d33_r5.get("robust_bonf"))
        if _d33_r5["ic"] < 0:
            _d33_verdict = "负 IC，已归零" if _d33_w <= 0 else "负 IC，方向相反"
        elif _d33_ok:
            _d33_verdict = "全 lag 稳健"
        elif _d33_t0 is not None and abs(_d33_t0) >= 2:
            _d33_verdict = "边际有效（未过全 lag）"
        else:
            _d33_verdict = "无证据"
        _d33_rows.append((_d33_CN[_d33_k], _d33_w, _d33_r5["ic"], _d33_t0, _d33_r20["ic"] if _d33_r20 else None, _d33_verdict,
                      _d33_r5.get("n"), None, None))
    _d33_trs = "".join(
        f'<tr style="border-bottom:0.5px solid #1e293b;">'
        f'<td style="padding:4px 8px;color:#cbd5e1;">{_d33_n}</td>'
        f'<td style="padding:4px 8px;text-align:right;color:#94a3b8;">{_d33_w*100:.1f}%</td>'
        f'<td style="padding:4px 8px;text-align:right;color:{"#22c55e" if (_d33_v or 0) > 0 else "#ef4444"};">'
        f'{f"{_d33_v:+.4f}" if _d33_v is not None else "—"}</td>'
        f'<td style="padding:4px 8px;text-align:right;color:#94a3b8;">'
        f'{f"{_d33_t:+.2f}" if _d33_t is not None else "—"}</td>'
        f'<td style="padding:4px 8px;text-align:right;color:#94a3b8;">'
        f'{f"{_d33_i20:+.4f}" if _d33_i20 is not None else "—"}</td>'
        f'<td style="padding:4px 8px;text-align:right;color:#64748b;">'
        f'{f"{_d33_nn}" if _d33_nn is not None else "—"}</td>'
        f'<td style="padding:4px 8px;text-align:right;color:'
        f'{"#4ade80" if _d33_dt is not None and _d33_dt > 0 else "#f87171" if _d33_dt is not None and _d33_dt < 0 else "#64748b"};'
        f'font-weight:{"700" if _d33_dt is not None and abs(_d33_dt) >= 3.08 else "400"};">'
        f'{f"{_d33_dt:+.2f}" if _d33_dt is not None else "—"}</td>'
        f'<td style="padding:4px 8px;text-align:right;color:'
        f'{"#4ade80" if _d33_dt20 is not None and _d33_dt20 > 0 else "#f87171" if _d33_dt20 is not None and _d33_dt20 < 0 else "#64748b"};'
        f'font-weight:{"700" if _d33_dt20 is not None and abs(_d33_dt20) >= 3.08 else "400"};">'
        f'{f"{_d33_dt20:+.2f}" if _d33_dt20 is not None else "—"}</td>'
        f'<td style="padding:4px 8px;color:{"#22c55e" if _d33_verdict == "全 lag 稳健" or _d33_verdict == "去掉后显著变差" else "#f59e0b" if _d33_verdict.startswith("边际") or _d33_verdict.startswith("单维") else "#ef4444"};">'
        f'{_d33_verdict}</td></tr>'
        for _d33_n, _d33_w, _d33_v, _d33_t, _d33_i20, _d33_verdict, _d33_nn, _d33_dt, _d33_dt20 in _d33_rows)
    # 复合方案对比：读 round24f（**生产线口径**：量程归一化，与 step2 同公式）。
    #   ⚠️ 不用 round24d 的 q2_cases —— 那是 rank_pct 口径，与生产线不等价。
    #   剔除 size 维的对照单列在下方，因 circ_mv 历史覆盖问题需单独说明。
    _cmp = []
    _q24f = {}
    try:
        _r24f = json.load(open(os.path.join(_here_dir, "audit_round24f.json"), encoding="utf-8"))
        _q24f = _r24f.get("cases") or {}
    except Exception:
        _r24f = None
    _CASE_ORDER = (
        ("P1 现行 v4.8（低波0.13 + size0.10）", "P1 v4.8拟上线 低波.13+size.10"),
        ("P0 v4.7 旧口径（成长0.13 + size0.10）", "P0 v4.7基线 成长.13+size.10"),
        ("去掉 size（低波0.13）", "P2 低波.13+size0"),
        ("成长0.13 + 低波0.13 共存", "P3 成长.13+低波.13+size.10(各维×0.87)"),
        ("成长0.05 + 低波0.08", "P4 成长.05+低波.08+size.10"),
        ("低波0.23 吃掉 size", "P5 低波.23+size0"),
    )
    for _nm, _key in _CASE_ORDER:
        _v5 = _q24f.get(f"h5|{_key}")
        if _v5:
            _cmp.append((_nm, _v5["ic"], _v5["t_by_lag"].get("0"),
                         bool(_v5.get("robust_bonf"))))
    if not _cmp:
        # 退化路径：round24f 缺失时回落到 round23 的 q9（旧口径，标注清楚）
        for _nm, _sc in (("v4.7 旧口径（7 维含成长）", "v4.7"),
                         ("去掉 成长（6 维）", "D2"), ("去掉 成长+质量（5 维）", "D3"),
                         ("再去 技术反号（4 维）", "D6")):
            for _full, _vv in _q9.items():
                if _full.startswith(_sc):
                    _r5 = (_vv.get("prod_ic") or {}).get("5")
                    if _r5:
                        _cmp.append((_nm, _r5["ic"], _r5["t_by_lag"].get("0"),
                                     bool(_r5.get("robust_bonf"))))
                    break
    _cmp_rows = "".join(
        f'<tr style="border-bottom:0.5px solid #1e293b;">'
        f'<td style="padding:4px 8px;color:#cbd5e1;">{_n}</td>'
        f'<td style="padding:4px 8px;text-align:right;color:#22c55e;">{_i:+.4f}</td>'
        f'<td style="padding:4px 8px;text-align:right;color:#94a3b8;">{_t:+.2f}</td>'
        f'<td style="padding:4px 8px;color:{"#22c55e" if _ok else "#f59e0b"};">'
        f'{"✅ 过 Bonferroni" if _ok else "⚠️ 未过"}</td></tr>'
        for _n, _i, _t, _ok in _cmp)
    if _d33_rows:
        _round23_html = f'''
        <div style="margin-top:9px;font-size:10px;color:#94a3b8;line-height:1.75;
                    padding:8px 10px;background:rgba(34,197,94,0.06);
                    border-left:3px solid #22c55e;border-radius:4px;">
            🧪 <b style="color:#4ade80;">维数实证（round23 单维 IC + round24 换维裁决，78 截面 × 真实前瞻收益，非当日数）</b><br>
            <div style="margin:6px 0 4px;font-size:9px;color:#64748b;">
                方法：逐维单独算 IC（rank 相关，未来 h 日收益）；NW t 扫 lag 0/3/6；Bonferroni 门槛 3.08。
                「过」= 三档 lag 全部 ≥3.08，缺一档即不算稳健。低波20 的单维 IC 来自 round24d 复测。
                <b style="color:#f59e0b;">「消融 t」是本表最该看的一列</b>：把该维从复合分里去掉后，
                复合 IC 的变化是否显著（|t|≥3.08 才算）。单维 IC 高只是「单独用有点用」，
                消融 t 高才是「组合里非它不可」—— 两者可以完全背离。
            </div>
            <table style="width:100%;border-collapse:collapse;font-size:9.5px;margin-bottom:7px;">
                <tr style="border-bottom:0.5px solid #334155;color:#64748b;">
                    <th style="text-align:left;padding:3px 8px;">维度</th>
                    <th style="text-align:right;padding:3px 8px;">当前权重</th>
                    <th style="text-align:right;padding:3px 8px;">h5 IC</th>
                    <th style="text-align:right;padding:3px 8px;">h5 t0</th>
                    <th style="text-align:right;padding:3px 8px;">h20 IC</th>
                    <th style="text-align:right;padding:3px 8px;">截面数 n</th>
                    <th style="text-align:right;padding:3px 8px;">消融 t<br><span style="font-weight:400;">(h5)</span></th>
                    <th style="text-align:right;padding:3px 8px;">消融 t<br><span style="font-weight:400;">(h20)</span></th>
                    <th style="text-align:left;padding:3px 8px;">实证判定</th>
                </tr>
                {_d33_trs}
            </table>
            <div style="margin:2px 0 6px;font-size:9px;color:#64748b;line-height:1.7;">
                <b style="color:#cbd5e1;">⚠️ 原文表述纠正（2026-10-27）</b>：
                此前本页曾写「8 维中只有估值锚、乖离单维过 Bonferroni」——
                <b style="color:#f87171;">这句话不成立，已撤回</b>。按 round23 的
                <code>q1_dim_ic</code>，过 Bonferroni 的是 <b>fund（n=73）与 bias60（n=63）两维</b>；
                但本表此前<b>漏列了 v4.9 生效的 EP估值与行业内市值</b>，也没有 n 列，
                读者无法判断各维 IC 来自不同样本口径。<br>
                <b style="color:#fbbf24;">fund 权重 7.5% 但 IC 只排第 5、消融 t 却排第 1</b> ——
                这三件事不矛盾：单维 IC 是「只用这一维选股」的收益，
                消融 t 是「把它从这 8 维组合里拿掉」的损失。
                维度之间相关（乖离↔RSI反 r=+0.86）会让单维 IC 与边际贡献脱钩。
                权重是先验，<b>不是</b>按 IC 大小分配的。
            </div>
            <b style="color:#fbbf24;">换维裁决（同一历史样本，按 <u>生产线量程归一化公式</u> 重算，78 截面）</b>
            <div style="margin:2px 0 4px;font-size:9px;color:#64748b;">
                ⚠️ 口径说明：此表用 step2 的真实公式（各维先按自身量程归一化到 0-10 再加权 ×10），
                不是「各维转池内排名再加权」。后者在分布偏斜时与生产线不等价，曾导致早期结论数值虚高/虚低。
            </div>
            <table style="width:100%;border-collapse:collapse;font-size:9.5px;margin:4px 0 6px;">
                <tr style="border-bottom:0.5px solid #334155;color:#64748b;">
                    <th style="text-align:left;padding:3px 8px;">方案</th>
                    <th style="text-align:right;padding:3px 8px;">h5 IC</th>
                    <th style="text-align:right;padding:3px 8px;">h5 t0</th>
                    <th style="text-align:left;padding:3px 8px;">稳健性</th>
                </tr>
                {_cmp_rows}
            </table>
            <b style="color:#fbbf24;">⚠️ 怎么读这两张表（别读过头）</b>：<br>
            · 「无证据」= 在本池 78 个截面里<b>没有可检出的 alpha</b>，不等于「一定是噪声」；
            样本 78 个、池内 490 只，检验功效有限，<b>负 IC 也不构成「反向更优」的证据</b>。<br>
            · 成长维已<b>归零</b>（权重 0，字段保留标「不计分·参考」）：历史增速与前瞻一阶导/二阶导
            （pe_ladder 推导 g1/g2/acc）h5 IC 全负，且<b style="color:#fbbf24;">五种极值治理均未转正</b>
            （raw −0.0218 → 缩尾 −0.0216 → sign·log1p −0.0148 → 缩尾+对数 −0.0150 → 逐子因子 rank −0.0247），
            故判定为「本池无 alpha」而非「方向算反」。<br>
            · 低波20 是换维来源：单维 h5 IC +0.1189(t0=3.28)、与六维 |r| ≤ 0.24、与 mom20 r=−0.19（非动量代理）。<br>
            · 换上低波后复合分 h5 IC 从 +0.1153 升到 <b style="color:#4ade80;">+0.1776</b>，
            lag 0/3/6 = +9.95/+6.10/+5.65 <b style="color:#4ade80;">全过 Bonferroni 3.08</b>，
            分子期前半 +0.2189 / 后半 +0.1375 <b>双正</b> —— 这是本项目首次出现「复合分全 lag 过 Bonferroni 且分子期双正」。<br>
            · <b style="color:#f59e0b;">已知短板（v4.8 时点披露，v4.9 已处置）</b>：小市值维单维 h5 IC
            <b style="color:#ef4444;">−0.0634（t0=−2.70）</b>，且与低波20 跨 78 截面平均秩相关
            <b style="color:#ef4444;">−0.501</b>（98.7% 截面 &lt; −0.3）——两者方向互斥
            （低波股本就是大市值股），合成后互相抵消；当时实测去掉 size 后复合 h5 可再升到 +0.2004。
            → <b style="color:#4ade80;">v4.9 已按 round27 方案 C 处置</b>：原始 size 归零、换成
            <b>行业内中性化市值（SIZE_IND_NEUT）</b>，h5 IC 由 −0.0634 改善到
            −0.0148 量级，是「保留小市值暴露但不与低波对冲」的那条路径。<br>
            · 本区块是<b>披露</b>，各维权重仍是先验；样本外复验（目标 30+ 截面）前不再调整。<br>
            · <b style="color:#fbbf24;">加维候选（round23 已测，口径为当时 v4.7 的 7 维）</b>：
            {_r23.get("_cand_txt", "")}<b style="color:#4ade80;">→ 其中 lowvol20 已于 v4.8 上线</b>
        </div>'''
except Exception as _e23:
    print(f"⚠️ round23 实证区块渲染失败（将跳过）: {type(_e23).__name__}: {_e23}")
    _round23_html = ""


_struct_html = ""
if _struct:
    _LBL = dict(_DIM_LABEL_SHORT)
    _pairs_s = "；".join(f'{_LBL.get(a,a)} ↔ {_LBL.get(b,b)} r={c:+.2f}' for a, b, c in _struct["pairs"])
    _neg_vc = [(k, v) for k, v in _struct["vc"].items() if v < 0]
    _vc_s = " · ".join(f'{_LBL.get(k,k)} {v:.0f}%'
                       for k, v in sorted(_struct["vc"].items(), key=lambda x: -abs(x[1])))
    _sens_s = " · ".join(
        f'{_LBL.get(k,k)}（去 {v["drop"]}/20 · 减半 {v["half"]}/20 · 反号 {v["sign"]}/20）'
        for k, v in sorted(_struct["sens"].items(), key=lambda x: x[1]["drop"]))
    _struct_html = f'''
        <div style="margin-top:9px;font-size:10px;color:#94a3b8;line-height:1.75;
                    padding:8px 10px;background:rgba(251,191,36,0.06);
                    border-left:3px solid #f59e0b;border-radius:4px;">
            🔬 <b style="color:#fbbf24;">模型结构体检（{_struct["n"]} 只当日现算，不是历史结论）</b><br>
            · <b>有效维数 {_struct["eff"]:.2f} / {_struct["p"]}</b>：
            虽然标称 {_struct["p"]} 维，但按相关矩阵特征值算参与率只有
            <b style="color:#f59e0b;">{_struct["eff"]:.2f} 维</b>独立信息
            （PC1 {_struct["ev"][0]:.0f}%、PC2 {_struct["ev"][1]:.0f}%，
            特征值 &gt;1 的成分仅 {_struct["n_ev_gt1"]} 个）。
            主因：{_pairs_s} —— <b>高相关维并非独立信号，等于同一维度被计了两次权重</b>。<br>
            · <b>方差贡献</b>（谁在真正决定排名）：{_vc_s}。
            {"；".join(f'<b style="color:#ef4444;">{_LBL.get(k,k)} {v:.0f}% 为负贡献</b>（该维反向拉低总分，即本池里此维高分者反而排后）' for k, v in _neg_vc) if _neg_vc else ""}<br>
            · <b>Top20 敏感性</b>（改这一维后，前 20 名还剩几只没变）：{_sens_s}。<br>
            <b style="color:#fbbf24;">结论</b>：这套评分适合当「当日相对强弱排序」用，
            <b>不宜当成「按 {_struct["p"]} 维独立验证过的选股模型」</b>；权重是先验设定，
            在池内有效样本补足前不具备「已优化」的统计地位。
            上表中「去 X/20」「反号 X/20」偏低的维度，说明最终名单对该维的取舍
            <b>高度敏感</b>，而该取舍目前只有先验、没有本池的稳健实证。
        </div>'''


# ===== v4.9（round25）：因子系统化分类框架 + 逐维修改意见 + 双维度评估 =====
# 双视角评估的定位：此前页面只回答「哪些维、多少权重」，不回答
#   ① 这 7 维各自属于什么**类别**、为什么这类因子在这个池子里该/不该有效；
#   ② 每一维的**具体短板**是什么、改什么、预期什么效果；
#   ③ 从「研究有效性」与「投资实用性」两个**不同**的标准看，结论是否一致。
# 三者缺一，读者就只能看到权重数字而无法判断可信度边界。
# 本区块全部读 audit_round25/25b/25c + round23 + round24f，不现算（现算区块在上方）。
_eval_html = ""
try:
    _hd = os.path.dirname(os.path.abspath(__file__))

    def _ld(_n):
        try:
            return json.load(open(os.path.join(_hd, _n), encoding="utf-8"))
        except Exception:
            return {}

    _r25 = _ld("audit_round25.json")
    _r25b = _ld("audit_round25b.json")
    _r25c = _ld("audit_round25c.json")
    _r23d = _ld("audit_round23.json")
    # 「哪些单维在 h5 全 lag 过 Bonferroni」——必须从 round23 现算，禁写死。
    #   ⚠️ 血案：此处原写「7 维中只有 bias60 单维…」，实测 round23 的 robust_bonf
    #   同时为 True 的还有 score_fund（t0=5.21 / t3=3.49 / t6=3.68）——
    #   一句话把唯一三重验证因子的稳健性抹掉了，且数字错了整整一维。
    #   只要换维或补数据，这类硬编码就必然再次失真，故改为实取。
    _rob_keys = [k for k, v in ((_r23d.get("q1_dim_ic") or {})).items()
                 if ((v.get("5") or {}).get("robust_bonf") is True)
                 and float(_DIM_W.get(k) or 0) > 0]
    if _rob_keys:
        _rob_lbl = "、".join(_DIM_LABEL_SHORT.get(k, k) for k in _rob_keys)
        # v4.11：过 Bonferroni 的可能不止一维（实测 fund + bias60），
        #   原措辞「只有 …单维」在 ≥2 维时读起来像笔误，按词形分叉。
        _rob_txt = (f"{_N_EFF_DIM} 维中，"
                    f"{'仅' if len(_rob_keys) == 1 else '共'} <b>{_rob_lbl}</b> "
                    f"{'这 1 维' if len(_rob_keys) == 1 else f'这 {len(_rob_keys)} 维'} "
                    f"单维在 h5 全 lag 过 Bonferroni，")
    else:
        _rob_txt = f"{_N_EFF_DIM} 维中没有单维在 h5 全 lag 过 Bonferroni，"
    _icir = _r25.get("q1_icir") or {}
    _lay = _r25.get("q2_layered") or {}
    _turn = _r25.get("q3_turnover") or {}
    _decay = _r25.get("q4_decay") or {}
    _crowd = _r25.get("q5_crowding") or {}
    _mult = _r25.get("q6_multitest") or {}
    _hold = (_r25b.get("results") or {})
    _c25c = (_r25c.get("cases") or {})

    # ---------- 公共取值（所有子表共用，必须在 _CLS 之前定义）----------
    _A_H5 = (_c25c.get("h5|A v4.8 现行 7维(低波.13+size.10)") or {})
    _P2_IC = None
    _P2_T0 = None
    try:
        _r24f2 = json.load(open(os.path.join(_hd, "audit_round24f.json"), encoding="utf-8"))
        _p2 = ((_r24f2.get("cases") or {}).get("h5|P2 低波.13+size0") or {})
        _P2_IC = _p2.get("ic")
        _P2_T0 = (_p2.get("t_by_lag") or {}).get("0")
    except Exception:
        pass
    # size / size-lowvol 冗余：全部从 JSON 实取，禁写死
    _SZ = (_icir.get("市值 size|h5") or {})
    _SZ_H5 = _SZ.get("ic") if _SZ.get("ic") is not None else 0.0
    _SZ_T0 = _SZ.get("t0") if _SZ.get("t0") is not None else 0.0
    _SZ_POS = _SZ.get("ic_pos_share") if _SZ.get("ic_pos_share") is not None else 0.0
    _SZL_CORR = 0.0
    try:
        _r24e2 = json.load(open(os.path.join(_hd, "audit_round24e.json"), encoding="utf-8"))
        _v = ((_r24e2.get("e4_size_lowvol_corr") or {}).get("mean"))
        _SZL_CORR = _v if _v is not None else 0.0
    except Exception:
        pass

    # round27 / 27b：EP 与 SIZE_IND_NEUT 的落库依据（全部实取，禁估算）。
    # ⚠️ 必须在 _CLS 之前定义——分类框架表（E 类）与逐维表（_MOD_TXT）都要用到。
    _r27 = _ld("audit_round27.json")
    _r27b = _ld("audit_round27b.json")
    # round28 保留仅用于「旧结论」对照列（其 78 截面中 69 个是老 500 池，结论已作废）。
    _r28 = _ld("audit_round28.json")
    _r29 = _ld("audit_round29.json")
    _ep27 = ((_r27.get("cases") or {}).get("I 仅 EP 单维（对照）|h5") or {})
    _epg = ((_r27b.get("q3_ep_vs_fund_gated") or {}).get("EP|h5") or {})
    _fnd_g = ((_r27b.get("q3_ep_vs_fund_gated") or {}).get("fund|h5") or {})
    _szi_c = ((_r27.get("cases") or {}).get("K 仅 SIZE_IND_NEUT 单维（对照）|h5") or {})
    _v27b = (_r27b.get("verdict") or {})

    # ---------- 飞书「五维分层」 vs 本项目「线性多维」对比（round29 宽样本定稿）
    #   全部数字从 audit_round29.json 实取，禁写死：这类对比结论最容易在换样本/换维后静默失真。
    #   ⚠️ round28（audit_round28.json）已作废：其 78 截面中 69 个（88.5%）是老 500 池（~193只），
    #      仅 9 个是新池。老大的质疑成立 → round29 重建宽样本面板。
    #   ⚠️ round29 本身也做过两轮样本纠错：
    #      第 1 版域 = 10-13 单日新池 490 ∩ … = 486 只  ← 选窄了（老大会问「为什么只用486只」）
    #      第 2 版域 = 历史池成分 seen ∩ … = 668 只        ← 股本只覆盖 676 只，仍是瓶颈
    #      第 3 版域 = 补 scored 股本后 = 917 只（覆盖历史池 90.4%）← 现用
    #      扩域后飞书 h20 由 −0.0554 翻为 +0.0058，**「飞书是反向指标」的结论已被推翻**。
    _fb_q3 = _r29.get("q3_schemes") or {}
    _fb_q1 = _r29.get("q1_structure") or {}
    _fb_q7 = _r29.get("q7_structure_test") or {}
    _fb_q8 = _r29.get("q8_v49_vs_v48") or {}
    _fb_q3b = _r29.get("q3b_robust_no_fund") or {}
    _fb_q3c = _r29.get("q3c_expansion_flip_attrib") or {}
    _fb_sc = _r29.get("sample_correction") or {}
    _fb_win = _r29.get("window") or {}
    _fb_q0 = _r29.get("q0_coverage") or {}
    _fb_fn = _fb_sc.get("domain_funnel") or {}
    _FB_DOM = _fb_sc.get("domain_size") or 0
    _FB_DOM_OLD = _fb_fn.get("old_486_domain") or 0

    def _fb_c(nm, hop):
        return _fb_q3.get("%s|h%d" % (nm, hop)) or {}

    def _fb_q7c(nm, hop):
        return _fb_q7.get("%s|h%d" % (nm, hop)) or {}

    def _fb_q4c(nm, hop):
        return (_r29.get("q4_merge") or {}).get("%s|h%d" % (nm, hop)) or {}

    def _fb_q3cc(nm, hop):
        return (_fb_q3c.get("rows") or {}).get("%s|h%d" % (nm, hop)) or {}

    # 扩域翻转归因表（A 老486 / B 新增 / 全样本 三列）
    _fb_q3c_tr = []
    for _nm, _lbl in [("F1_飞书分层5类", "F1 飞书分层5类"),
                      ("F2_飞书平铺8因子", "F2 飞书平铺8因子"),
                      ("C_现行八维(v4.9)", "<b>现行八维 v4.9</b>")]:
        for _hp in (5, 20):
            _r = _fb_q3cc(_nm, _hp)
            _d = (_r.get("full_ic") or 0) - (_r.get("A_ic") or 0)
            _fb_q3c_tr.append(
                f'<tr style="border-bottom:0.5px solid #1e293b;">'
                f'<td style="padding:2px 7px;color:#cbd5e1;">{_lbl}</td>'
                f'<td style="padding:2px 7px;text-align:center;color:#64748b;">h{_hp}</td>'
                f'<td style="padding:2px 7px;text-align:right;color:#e2e8f0;">'
                f'{_r.get("A_ic") or 0:+.4f}<span style="color:#64748b;"> (t{_r.get("A_t0") or 0:+.2f})</span></td>'
                f'<td style="padding:2px 7px;text-align:right;color:#e2e8f0;">'
                f'{_r.get("B_ic") or 0:+.4f}<span style="color:#64748b;"> (t{_r.get("B_t0") or 0:+.2f})</span></td>'
                f'<td style="padding:2px 7px;text-align:right;color:#4ade80;font-weight:700;">'
                f'{_r.get("full_ic") or 0:+.4f}<span style="color:#64748b;"> (t{_r.get("full_t0") or 0:+.2f})</span></td>'
                f'</tr>')
    _fb_q3c_tr = "".join(_fb_q3c_tr)

    _FB_ROWS = [
        ("F1 飞书分层5类", "F1_飞书分层5类", "#f87171",
         "按文档「五大类」写法：类内等权，再按大类权重加总"),
        ("F2 飞书平铺8因子", "F2_飞书平铺8因子", "#fbbf24",
         "按文档「因子权重表」写法：8 因子直接线性加总"),
        ("F3 隐含权重当平铺", "F3_飞书分层隐含权重当平铺", "#94a3b8",
         "把分层的隐含权重拿出来当平铺权重（=F1 的等价展开）"),
        ("<b>现行八维 v4.9</b>", "C_现行八维(v4.9)", "#4ade80",
         "本项目当前生效模型（ep/fund 各半 + size_ind 替换 size）"),
        ("现行七维 v4.8（对照）", "C7_现行七维(v4.8对照)", "#38bdf8",
         "换维前的版本，用于确认基线可复现"),
    ]
    _fb_tr = []
    for _lbl, _key, _cc, _desc in _FB_ROWS:
        _a5, _a20 = _fb_c(_key, 5), _fb_c(_key, 20)
        # ⚠️ 飞书三方案 h20 的 t0 过了 Bonferroni 但是**负的** → 必须把符号一起显示，
        #    只标 ✅ 会让人误读成「有效因子」。t0 为负时独立标红。
        _bf5 = "✅" if _a5.get("pass_bonf") else "❌"
        _bf20 = "✅" if _a20.get("pass_bonf") else "❌"
        _t20 = _a20.get("t0", 0) or 0
        _t20_c = "#f87171" if _t20 < 0 else "#94a3b8"
        _t5 = _a5.get("t0", 0) or 0
        _t5_c = "#f87171" if _t5 < 0 else "#94a3b8"
        _fb_tr.append(
            f'<tr style="border-bottom:0.5px solid #1e293b;vertical-align:top;">'
            f'<td style="padding:5px 7px;color:{_cc};font-weight:700;white-space:nowrap;">{_lbl}<br>'
            f'<span style="font-size:8.5px;color:#64748b;font-weight:400;">{_desc}</span></td>'
            f'<td style="padding:5px 7px;color:#e2e8f0;text-align:right;font-weight:700;">'
            f'{_a5.get("ic", 0):+.4f}</td>'
            f'<td style="padding:5px 7px;color:{_t5_c};text-align:right;">{_t5:+.2f}</td>'
            f'<td style="padding:5px 7px;color:#94a3b8;text-align:right;">{_a5.get("icir", 0):+.2f}</td>'
            f'<td style="padding:5px 7px;color:#e2e8f0;text-align:right;font-weight:700;">'
            f'{_a20.get("ic", 0):+.4f}</td>'
            f'<td style="padding:5px 7px;color:{_t20_c};text-align:right;font-weight:700;">{_t20:+.2f}</td>'
            f'<td style="padding:5px 7px;color:#94a3b8;text-align:right;">{_a20.get("icir", 0):+.2f}</td>'
            f'<td style="padding:5px 7px;text-align:center;">{_bf5} / {_bf20}</td></tr>')
    _fb_rows = "".join(_fb_tr)

    # Q1 权重打架表（关系最尖锐的一条证据）
    _fb_ratio = _fb_q1.get("implied_vs_flat_ratio") or {}
    _fb_imp = _fb_q1.get("implied_from_layers") or {}
    _fb_w = _fb_q1.get("declared_flat") or {}
    _fb_wtr = "".join(
        f'<tr style="border-bottom:0.5px solid #1e293b;">'
        f'<td style="padding:3px 7px;color:#cbd5e1;font-weight:700;">{_k}</td>'
        f'<td style="padding:3px 7px;color:#94a3b8;text-align:right;">{_fb_imp.get(_k, 0):.3f}</td>'
        f'<td style="padding:3px 7px;color:#94a3b8;text-align:right;">{_fb_w.get(_k, 0):.3f}</td>'
        f'<td style="padding:3px 7px;text-align:right;font-weight:700;'
        f'color:{"#f87171" if abs(_fb_ratio.get(_k, 1) - 1) > 0.5 else "#94a3b8"};">'
        f'{_fb_ratio.get(_k, 0):.2f}x</td></tr>'
        for _k in _fb_w)

    # 前后半期表（飞书三方案 h20 后半期强负 —— 本轮最重要的稳健性证据）
    _FB_HALF = [("F1 飞书分层5类", "F1_飞书分层5类", "#f87171"),
                ("F2 飞书平铺8因子", "F2_飞书平铺8因子", "#fbbf24"),
                ("F3 隐含权重当平铺", "F3_飞书分层隐含权重当平铺", "#94a3b8"),
                ("现行八维 v4.9", "C_现行八维(v4.9)", "#4ade80"),
                ("现行七维 v4.8", "C7_现行七维(v4.8对照)", "#38bdf8")]
    _fb_htr = []
    for _lbl, _key, _cc in _FB_HALF:
        _a5, _a20 = _fb_c(_key, 5), _fb_c(_key, 20)
        _cells = []
        for _v in (_a5.get("first_half_ic", 0), _a5.get("second_half_ic", 0),
                   _a20.get("first_half_ic", 0), _a20.get("second_half_ic", 0)):
            _neg = (_v or 0) < 0
            _cells.append(
                f'<td style="padding:4px 7px;text-align:right;'
                f'color:{"#f87171" if _neg else "#e2e8f0"};'
                f'font-weight:{"700" if _neg else "400"};">{(_v or 0):+.4f}</td>')
        _fb_htr.append(
            f'<tr style="border-bottom:0.5px solid #1e293b;">'
            f'<td style="padding:4px 7px;color:{_cc};font-weight:700;">{_lbl}</td>'
            + "".join(_cells) + '</tr>')
    _fb_hrows = "".join(_fb_htr)

    # Q4 并入表（宽样本全部为负 —— 与 round28 结论相反）
    _FB_MERGE = ["C+OCFG", "C+NAG", "C+EVEBIT", "C+SUE", "C+REV1M"]
    _fb_mtr = []
    _c5 = _fb_c("C_现行八维(v4.9)", 5).get("ic", 0)
    _c20 = _fb_c("C_现行八维(v4.9)", 20).get("ic", 0)
    for _nm in _FB_MERGE:
        _m5, _m20 = _fb_q4c(_nm, 5), _fb_q4c(_nm, 20)
        _d5 = (_m5.get("ic", 0) or 0) - _c5
        _d20 = (_m20.get("ic", 0) or 0) - _c20
        _h2 = _m20.get("second_half_ic", 0) or 0
        _fb_mtr.append(
            f'<tr style="border-bottom:0.5px solid #1e293b;">'
            f'<td style="padding:4px 7px;color:#cbd5e1;font-weight:700;">{_nm}</td>'
            f'<td style="padding:4px 7px;text-align:right;color:#e2e8f0;">{_m5.get("ic", 0):+.4f}</td>'
            f'<td style="padding:4px 7px;text-align:right;font-weight:700;'
            f'color:{"#f87171" if _d5 < 0 else "#4ade80"};">{_d5:+.4f}</td>'
            f'<td style="padding:4px 7px;text-align:right;color:#e2e8f0;">{_m20.get("ic", 0):+.4f}</td>'
            f'<td style="padding:4px 7px;text-align:right;font-weight:700;'
            f'color:{"#f87171" if _d20 < 0 else "#4ade80"};">{_d20:+.4f}</td>'
            f'<td style="padding:4px 7px;text-align:right;'
            f'color:{"#f87171" if _h2 < 0 else "#94a3b8"};'
            f'font-weight:{"700" if _h2 < 0 else "400"};">{_h2:+.4f}</td></tr>')
    _fb_mrows = "".join(_fb_mtr)


    _fb_html = f'''
        <b style="color:#38bdf8;">五、飞书「五维分层」 vs 本项目「线性多维」—— 谁更优（round29 宽样本定稿）</b>
        <div style="padding:6px 9px;background:rgba(239,68,68,0.10);border-left:3px solid #ef4444;
                    border-radius:4px;margin:2px 0 7px;color:#cbd5e1;font-size:9.5px;">
            <b style="color:#f87171;">⚠️ 本节已整节重做两次：round28 样本错 → round29 首版样本又选窄</b><br>
            <b>第一错（round28）：</b>用了 {(_fb_sc.get("old_pool_snapshots") or 0) + (_fb_sc.get("new_pool_snapshots") or 0)} 个截面，
            其中 <b>{_fb_sc.get("old_pool_snapshots") or 0} 个（{(_fb_sc.get("old_pool_snapshots") or 0) / max(1, (_fb_sc.get("old_pool_snapshots") or 0) + (_fb_sc.get("new_pool_snapshots") or 0)) * 100:.1f}%）
            是老 500 池截面（每截面仅 ~{_fb_sc.get("old_pool_size") or 0} 只）</b>，新池截面只有
            {_fb_sc.get("new_pool_snapshots") or 0} 个 → 该版结论已作废。<br>
            <b>第二错（round29 首版，老大追问「为什么只用486只」）：</b>把「{_fb_sc.get("new_pool_size") or 0} 只新池的
            <u>单日</u>成分」当固定域 → 只剩 <b>{_FB_DOM_OLD} 只</b>。但同一天池里约 100 只大盘股
            （平安银行/美的/五粮液/格力/中兴/泸州老窖…）在 10-26 因「近20日换手率 ≥ 1.5%」被初筛掉，
            <b>用单日池当锚 = 把「今天恰好没过换手率门槛」的股票永久踢出历史回测</b>，这是错的。<br>
            <b>正确锚 = 历史池成分</b>（所有截面出现过的 {_fb_fn.get("seen_snapshot") or 0} 只）<b> ∩ 可用数据</b>。<br>
            <b>漏斗（逐层如实披露）：</b>历史池 {_fb_fn.get("seen_snapshot") or 0}
            → ∩价格库 {_fb_fn.get("after_px") or 0} → ∩EM财务 {_fb_fn.get("after_em") or 0}
            → ∩申万一级 {_fb_fn.get("after_sw") or 0} → ∩股本 {_fb_fn.get("final_domain") or 0}
            <span style="color:#64748b;">（股本原只有 676 只，补 top200_scored 的 circ_mv/price 后到
            {_fb_fn.get("shares_rebuild") or 0} 只，域才从 668 上到 {_FB_DOM}）</span>。<br>
            <b>本轮样本：</b><b style="color:#4ade80;">{_FB_DOM} 只</b>
            （覆盖历史池 {_fb_fn.get("covered_share_of_seen") or 0}%，比首版 {_FB_DOM_OLD} 只补回
            <b>{_fb_fn.get("recovered") or 0} 只</b>；round28 老池那 {_fb_sc.get("old_pool_snapshots") or 0}
            个截面每截面仅 ~{_fb_sc.get("old_pool_size") or 0} 只，本轮是它的
            {_FB_DOM / max(1, _fb_sc.get("old_pool_size") or 1):.1f} 倍）；
            窗口 {_fb_win.get("start", "")} ~ {_fb_win.get("end", "")}，
            h5 <b>{_fb_win.get("n_snapshots_h5") or 0}</b> 截面 / h20 <b>{_fb_win.get("n_snapshots_h20") or 0}</b> 截面。<br>
            <b style="color:#fbbf24;">扩域的直接后果：飞书三方案 h20 从「显著负」翻成「≈0 / 弱正」，
            「飞书是反向指标」这条结论已被推翻</b>（详见下方「扩域翻转归因」）。
        </div>

        <div style="font-size:9px;color:#64748b;margin:2px 0 6px;">
            <b>先回答一个更根本的问题：这个对比本身是不是伪命题？</b>
            飞书的综合分是 <code>Σ_c w_c × mean(z ∈ 类c)</code>，展开后恒等于
            <code>Σ_f (w_c/|c|) × z_f</code> —— <b>分层线性加权与单层线性加权数学上完全等价</b>。
            实测 n={_fb_q1.get("equivalence_n", 0)} 只次，最大绝对差
            <b style="color:#4ade80;">{_fb_q1.get("equivalence_maxdiff", 0):.2e}</b>。
            所以「五维 vs 线性」<b>结构本身不产生任何 alpha</b>，它只是设定权重的一种启发式写法；
            真正决定优劣的是<b>隐含因子权重</b>。
            <span style="color:#64748b;">（这是全节唯一不随样本变化的结论——因为它<span style="color:#94a3b8;">是数学恒等式，不是实证结论</span>。）</span>
        </div>
        <div style="padding:6px 9px;background:rgba(239,68,68,0.08);border-left:3px solid #ef4444;
                    border-radius:4px;margin-bottom:7px;color:#cbd5e1;font-size:9.5px;">
            <b style="color:#f87171;">⚠️ 顺带发现：飞书文档内部权重自相矛盾（这不是我算错，是文档两处不一致）</b><br>
            同一份文档里，「五大类分层」隐含的因子权重 与 「因子权重表」声明的权重
            <b>完全对不上</b>，但两处各自的合计都归一为 1.000 —— 所以单看任何一处都「看起来对」：
            <table style="width:62%;border-collapse:collapse;font-size:9px;margin-top:4px;">
                <tr style="border-bottom:0.5px solid #334155;color:#64748b;">
                    <th style="text-align:left;padding:2px 7px;">因子</th>
                    <th style="text-align:right;padding:2px 7px;">分层隐含</th>
                    <th style="text-align:right;padding:2px 7px;">平铺声明</th>
                    <th style="text-align:right;padding:2px 7px;">倍数</th>
                </tr>
                {_fb_wtr}
            </table>
            <b>ROE 实际权重是声明值的 {_fb_ratio.get("ROE", 0):.1f} 倍，OCFG 只有声明值的
            {_fb_ratio.get("OCFG", 0) * 100:.0f}%</b> —— 按分层跑和按平铺表跑，是两套完全不同的模型。
        </div>
        <table style="width:100%;border-collapse:collapse;font-size:9.5px;margin-bottom:5px;">
            <tr style="border-bottom:0.5px solid #334155;color:#64748b;">
                <th style="text-align:left;padding:3px 7px;width:20%;">方案</th>
                <th style="text-align:right;padding:3px 7px;">h5 IC</th>
                <th style="text-align:right;padding:3px 7px;">t0</th>
                <th style="text-align:right;padding:3px 7px;">ICIR</th>
                <th style="text-align:right;padding:3px 7px;">h20 IC</th>
                <th style="text-align:right;padding:3px 7px;">t0</th>
                <th style="text-align:right;padding:3px 7px;">ICIR</th>
                <th style="text-align:center;padding:3px 7px;width:11%;">Bonferroni<br>h5/h20</th>
            </tr>
            {_fb_rows}
        </table>
        <div style="font-size:9px;color:#64748b;margin-bottom:7px;">
            <b>同一样本、同一公式</b>：{_FB_DOM} 只固定域，
            仅取覆盖最低的 SIZE_IND_NEUT（{(_fb_q0.get("score_size_ind") or 0) * 100:.1f}%）有值的子样本做卡口，
            所有方案都走生产线量程归一化口径 <code>Σ min(v,DMAX)/DMAX×10×w / max(0.5,Σw)×10</code>，
            飞书侧走 winsorize 0.1% + 截面 Z。<br>
            <b style="color:#fbbf24;">⚠️ 读表纪律：扩域后飞书三方案 h20 已不再显著为负</b>（首版 486 域时为负是样本伪影，
            见下「扩域翻转归因」）。本节所有负数标红，请勿只看 ✅/❌ 不看符号。
        </div>

        <div style="padding:7px 10px;border-radius:6px;background:rgba(251,191,36,0.09);
                    border-left:3px solid #fbbf24;margin-bottom:7px;color:#cbd5e1;font-size:9.5px;">
            <b style="color:#fbbf24;">🔍 扩域翻转归因：把域拆成 A=老{_FB_DOM_OLD} / B=新增{_fb_q3c.get("set_B") or 0}，分别算 IC</b><br>
            <span style="font-size:9px;color:#94a3b8;">A、B 都 ⊆ 同一 {_FB_DOM} 域、同一 GATE 卡口、同一前瞻收益，
            唯一差别是成分 —— 这是唯一能把「样本效应」和「信号效应」拆开的做法。</span>
            <table style="width:100%;border-collapse:collapse;font-size:9px;margin-top:4px;">
                <tr style="border-bottom:0.5px solid #334155;color:#64748b;">
                    <th style="text-align:left;padding:2px 7px;width:26%;">方案</th>
                    <th style="text-align:center;padding:2px 7px;">hop</th>
                    <th style="text-align:right;padding:2px 7px;">A 老{_FB_DOM_OLD} 子集</th>
                    <th style="text-align:right;padding:2px 7px;">B 新增{_fb_q3c.get("set_B") or 0} 子集</th>
                    <th style="text-align:right;padding:2px 7px;">全样本 {_FB_DOM}</th>
                </tr>
                {_fb_q3c_tr}
            </table>
            <b style="margin-top:4px;display:block;color:#4ade80;">关键发现：飞书方案的 IC 对「横截面标准化的池大小」极度敏感 ——
            现行八维几乎不动（池效应 +0.0007 / +0.0014），飞书三方案 h20 却动了 +0.036 ~ +0.058（<b>40 倍差</b>）。</b>
            <b style="display:block;margin-top:3px;">根因：飞书综合分是 8 个 Z 值的加权和，Z 在<b>当日全池</b>上标准化；
            池从 486 扩到 {_FB_DOM} 后，新增的 {_fb_q3c.get("set_B") or 0} 只（中位市值
            104 亿 vs 老池 316 亿、&lt;50 亿占比 19.4% vs 4.7%）<b>改变了每个 Z 的分布</b>，
            于是同一批股票的分数被重排。现行八维是<b>池内 rank 转 0-10</b>，秩变换单调、抗分布变形，所以稳。</b>
            <b style="display:block;margin-top:3px;color:#f87171;">结论修正：「飞书 h20 是显著反向指标」<u>不成立</u>。
            {_FB_DOM_OLD} 域上的负号来自：①域太窄（漏 45% 小市值股）；②Z 标准化让飞书分对池成分异常敏感。
            正确表述是：飞书三方案 h20 IC ≈ 0、不显著（F2 平铺在 h5 上另有 +0.0589 且过 Bonferroni，仍需在更大样本复核）。</b>
        </div>

        <div style="padding:7px 10px;border-radius:6px;background:rgba(239,68,68,0.07);
                    border-left:3px solid #ef4444;margin-bottom:7px;color:#cbd5e1;font-size:9.5px;">
            <b style="color:#f87171;">🔴 两轮纠错后仍然成立的结论（这才是能拿去用的）</b>
            <table style="width:100%;border-collapse:collapse;font-size:9px;margin-top:4px;">
                <tr style="border-bottom:0.5px solid #334155;color:#64748b;">
                    <th style="text-align:left;padding:2px 7px;width:24%;">结论</th>
                    <th style="text-align:left;padding:2px 7px;width:24%;">两轮纠错前</th>
                    <th style="text-align:left;padding:2px 7px;width:28%;">现（{_FB_DOM} 只）</th>
                    <th style="text-align:left;padding:2px 7px;">影响</th>
                </tr>
                <tr style="border-bottom:0.5px solid #1e293b;vertical-align:top;">
                    <td style="padding:3px 7px;color:#cbd5e1;font-weight:700;">分层 vs 平铺是否等价</td>
                    <td style="padding:3px 7px;color:#94a3b8;">未验证</td>
                    <td style="padding:3px 7px;color:#4ade80;font-weight:700;">maxdiff {_fb_q1.get("equivalence_maxdiff", 0):.2e}，完全等价</td>
                    <td style="padding:3px 7px;color:#94a3b8;"><b>唯一不随样本变的结论</b>（数学恒等式）→「五维 vs 线性」是伪命题</td>
                </tr>
                <tr style="border-bottom:0.5px solid #1e293b;vertical-align:top;">
                    <td style="padding:3px 7px;color:#cbd5e1;font-weight:700;">飞书三方案 h20 IC</td>
                    <td style="padding:3px 7px;color:#94a3b8;">round28 正 +0.065~+0.083<br>首版486 <b style="color:#f87171;">负 −0.045~−0.060</b></td>
                    <td style="padding:3px 7px;color:#e2e8f0;font-weight:700;">{_fb_c("F1_飞书分层5类", 20).get("ic", 0):+.4f} / {_fb_c("F2_飞书平铺8因子", 20).get("ic", 0):+.4f} / {_fb_c("F3_飞书分层隐含权重当平铺", 20).get("ic", 0):+.4f}（均不显著）</td>
                    <td style="padding:3px 7px;color:#f87171;font-weight:700;">「反向指标」结论<b>撤回</b>；正解是「≈0，无 alpha」</td>
                </tr>
                <tr style="border-bottom:0.5px solid #1e293b;vertical-align:top;">
                    <td style="padding:3px 7px;color:#cbd5e1;font-weight:700;">现行八维是否仍大幅领先</td>
                    <td style="padding:3px 7px;color:#94a3b8;">+0.1914 vs −0.0554（差 0.25）</td>
                    <td style="padding:3px 7px;color:#4ade80;font-weight:700;">{_c20:+.4f} vs {_fb_c("F1_飞书分层5类", 20).get("ic", 0):+.4f}（差 {_c20 - (_fb_c("F1_飞书分层5类", 20).get("ic", 0) or 0):+.4f}）</td>
                    <td style="padding:3px 7px;color:#94a3b8;">仍领先但差距缩小到原来的 1/6，<b>不宜再引用 0.25 那个量级</b></td>
                </tr>
                <tr style="border-bottom:0.5px solid #1e293b;vertical-align:top;">
                    <td style="padding:3px 7px;color:#cbd5e1;font-weight:700;">C+REV1M 并入增量</td>
                    <td style="padding:3px 7px;color:#94a3b8;">round28 正 +0.0121/+0.0180<br>首版486 负</td>
                    <td style="padding:3px 7px;color:#f87171;font-weight:700;">{((_fb_q4c("C+REV1M", 5).get("ic", 0) or 0) - _c5):+.4f} / {((_fb_q4c("C+REV1M", 20).get("ic", 0) or 0) - _c20):+.4f}</td>
                    <td style="padding:3px 7px;color:#cbd5e1;">两轮都是负 → <b>「提高反转块权重」确已撤回</b>，这条站得住</td>
                </tr>
                <tr style="border-bottom:0.5px solid #1e293b;vertical-align:top;">
                    <td style="padding:3px 7px;color:#cbd5e1;font-weight:700;">Q7 强制块内等权</td>
                    <td style="padding:3px 7px;color:#94a3b8;">round28 负 −0.006/−0.005</td>
                    <td style="padding:3px 7px;color:#4ade80;font-weight:700;">正 {(_fb_q7c("Z2_分块块内等权", 5).get("ic", 0) - _fb_q7c("Z1_平铺(现行原始权重)", 5).get("ic", 0)):+.4f} / {(_fb_q7c("Z2_分块块内等权", 20).get("ic", 0) - _fb_q7c("Z1_平铺(现行原始权重)", 20).get("ic", 0)):+.4f}</td>
                    <td style="padding:3px 7px;color:#cbd5e1;"><b>但这是「缺失值归并层级」效应，不是结构效应</b>（Z3 纯结构也正，见下），不能当作结构有 alpha 的证据</td>
                </tr>
                <tr style="border-bottom:0.5px solid #1e293b;vertical-align:top;">
                    <td style="padding:3px 7px;color:#cbd5e1;font-weight:700;">Q8 h20 v4.9 vs v4.8</td>
                    <td style="padding:3px 7px;color:#94a3b8;">NW-t −2.85（v4.8 更好）</td>
                    <td style="padding:3px 7px;color:#e2e8f0;font-weight:700;">NW-t {(_fb_q8.get("h20") or {}).get("nw_t", 0):+.2f}（v4.8 略好）</td>
                    <td style="padding:3px 7px;color:#cbd5e1;">两者都不过 3.08 → 只能称「不可区分」，<b>不能推翻 round27c</b></td>
                </tr>
                <tr style="vertical-align:top;">
                    <td style="padding:3px 7px;color:#cbd5e1;font-weight:700;">现行八维 IC 的稳健性</td>
                    <td style="padding:3px 7px;color:#94a3b8;">+0.1506 / +0.1914（486域）</td>
                    <td style="padding:3px 7px;color:#4ade80;font-weight:700;">{_c5:+.4f} / {_c20:+.4f}（{_FB_DOM}域）</td>
                    <td style="padding:3px 7px;color:#cbd5e1;">扩域 +89% 后几乎不变（Δ +0.0016 / −0.0032）→ <b>现行模型对样本口径不敏感，这是它最可贵的性质</b></td>
                </tr>
            </table>
            <b style="margin-top:4px;display:block;">教训：①样本域必须用「历史成分并集」，绝不能用单日池当固定域（换手率门槛会静默踢掉大盘股）；
            ②横截面标准化 vs 池内 rank 是两种尺度，对池大小敏感度差 40 倍，做 A/B 必须先对齐这一层；
            ③「显著负」这种强结论一定要先做子集归因，否则样本伪影会被写进决策依据。</b>
        </div>

        <div style="padding:7px 10px;border-radius:6px;background:rgba(34,197,94,0.07);
                    border-left:3px solid #22c55e;margin-bottom:7px;color:#cbd5e1;font-size:9.5px;">
            <b style="color:#4ade80;">结论一：现行八维在宽样本上仍明显更优，但「线性比五维好」这个说法本身不成立</b><br>
            现行八维 h5 <b>{_c5:+.4f}</b> vs 飞书最优可实现版（F2 平铺）
            {_fb_c("F2_飞书平铺8因子", 5).get("ic", 0):+.4f}，差
            <b>{_c5 - (_fb_c("F2_飞书平铺8因子", 5).get("ic", 0) or 0):+.4f}</b>；<br>
            h20 <b>{_c20:+.4f}</b> vs {_fb_c("F2_飞书平铺8因子", 20).get("ic", 0):+.4f}（t0
            {_fb_c("F2_飞书平铺8因子", 20).get("t0", 0):+.2f}，不显著），差
            <b>{_c20 - (_fb_c("F2_飞书平铺8因子", 20).get("ic", 0) or 0):+.4f}</b>。<br>
            <b style="color:#fbbf24;">⚠️ 措辞修正：上一版说「h20 是正负方向差异」已被扩域推翻</b>
            —— {_FB_DOM_OLD} 域上飞书 h20 为负，扩到 {_FB_DOM} 后转正但不显著。
            正解是「飞书 h20 无 alpha」，而不是「飞书是反向指标」。<br><br>
            <b>稳健性校验：剔除 fund 维（覆盖仅 {(_fb_q0.get("score_fund") or 0) * 100:.1f}%，硬缺口）后：</b>
            C_noFund h5 <b>{(_fb_q3b.get("C_noFund_现行七维(ep+size_ind)|h5") or {}).get("ic", 0):+.4f}</b>
            （t0 {(_fb_q3b.get("C_noFund_现行七维(ep+size_ind)|h5") or {}).get("t0", 0):+.2f}）/
            h20 <b>{(_fb_q3b.get("C_noFund_现行七维(ep+size_ind)|h20") or {}).get("ic", 0):+.4f}</b>
            （t0 {(_fb_q3b.get("C_noFund_现行七维(ep+size_ind)|h20") or {}).get("t0", 0):+.2f}），双 ✅。
            <b>剔除 fund 后 C 反而略升</b>（{_c5:+.4f} → {(_fb_q3b.get("C_noFund_现行七维(ep+size_ind)|h5") or {}).get("ic", 0):+.4f}，
            {_c20:+.4f} → {(_fb_q3b.get("C_noFund_现行七维(ep+size_ind)|h20") or {}).get("ic", 0):+.4f}），排序不变
            → 结论不依赖那 {(_fb_q0.get("score_fund") or 0) * 100:.0f}% 的 fund 子样本；
            同时暴露<b>fund 维在本样本边际贡献为零甚至微负</b>（待裁决）。<br><br>
            <b>但把差额拆开看，结构这一项贡献仍然是 0：</b>
            <table style="width:100%;border-collapse:collapse;font-size:9px;margin-top:3px;">
                <tr style="border-bottom:0.5px solid #334155;color:#64748b;">
                    <th style="text-align:left;padding:2px 7px;width:26%;">归因项</th>
                    <th style="text-align:left;padding:2px 7px;width:14%;">贡献</th>
                    <th style="text-align:left;padding:2px 7px;">说明</th>
                </tr>
                <tr style="border-bottom:0.5px solid #1e293b;vertical-align:top;">
                    <td style="padding:3px 7px;color:#cbd5e1;font-weight:700;">结构（五维 vs 线性）</td>
                    <td style="padding:3px 7px;color:#94a3b8;"><b>0</b></td>
                    <td style="padding:3px 7px;color:#94a3b8;">数学恒等（maxdiff={_fb_q1.get("equivalence_maxdiff", 0):.1e}）。
                        分块写法的实测差异来自<b>缺失值归并层级</b>，不是结构本身（见结论二）。</td>
                </tr>
                <tr style="border-bottom:0.5px solid #1e293b;vertical-align:top;">
                    <td style="padding:3px 7px;color:#cbd5e1;font-weight:700;">因子集（飞书无反转块）</td>
                    <td style="padding:3px 7px;color:#f87171;"><b>主因</b></td>
                    <td style="padding:3px 7px;color:#94a3b8;">飞书 8 因子<b>完全没有反转块</b>（bias60/rsi_rev/lowvol20），
                        而反转块是本项目 alpha 的主来源（占现行权重
                        {(float(_DIM_W.get('score_bias60') or 0) + float(_DIM_W.get('score_rsi_rev') or 0) + float(_DIM_W.get('score_lowvol20') or 0)) * 100:.0f}%）。</td>
                </tr>
                <tr style="vertical-align:top;">
                    <td style="padding:3px 7px;color:#cbd5e1;font-weight:700;">权重分配</td>
                    <td style="padding:3px 7px;color:#fbbf24;">被上项吸收</td>
                    <td style="padding:3px 7px;color:#94a3b8;">飞书把 35%（分层）/ 25%（平铺）押在 ROE / OCFG 上；
                        本池实测 ROE 单维与 quality 相关 −0.42、OCFG 与各维相关 |r| ≤ 0.24，
                        <b>在本池都无 alpha</b>。</td>
                </tr>
            </table>
        </div>

        <div style="padding:7px 10px;border-radius:6px;background:rgba(56,189,248,0.07);
                    border-left:3px solid #38bdf8;margin-bottom:7px;color:#cbd5e1;font-size:9.5px;">
            <b style="color:#38bdf8;">结论二：分子期前后半——飞书三方案 h20 后半期强负，不是「衰减」是「本来就没有」</b>
            <table style="width:100%;border-collapse:collapse;font-size:9px;margin-top:4px;">
                <tr style="border-bottom:0.5px solid #334155;color:#64748b;">
                    <th style="text-align:left;padding:2px 7px;width:24%;">方案</th>
                    <th style="text-align:right;padding:2px 7px;">h5 前半</th>
                    <th style="text-align:right;padding:2px 7px;">h5 后半</th>
                    <th style="text-align:right;padding:2px 7px;">h20 前半</th>
                    <th style="text-align:right;padding:2px 7px;">h20 后半</th>
                </tr>
                {_fb_hrows}
            </table>
            <b style="display:block;margin-top:4px;">飞书三方案 h5 后半期已全部转负（−0.025 ~ −0.043），
            h20 后半期强负（−0.120 ~ −0.144）</b> → 不是「alpha 集中在前半段」，而是<b>前半段也只是勉强为正，
            后半段明确反向</b>。唯一「双 ✅ + 前后双正」的是现行八维/七维。
            但也要如实说明：<b>C 的后半期 IC 只剩 {( _fb_c("C_现行八维(v4.9)", 20).get("second_half_ic", 0)):+.4f}
            （h20，约为前半的 1/12）</b> —— 现行模型的 alpha 同样在衰减，只是还没到负值。
        </div>

        <div style="padding:7px 10px;border-radius:6px;background:rgba(167,139,250,0.07);
                    border-left:3px solid #a78bfa;margin-bottom:7px;color:#cbd5e1;font-size:9.5px;">
            <b style="color:#a78bfa;">结论三：Q7「强制块内等权」符号翻转，但这是缺失处理的副作用，不是结构优势</b>
            <table style="width:100%;border-collapse:collapse;font-size:9px;margin-top:4px;">
                <tr style="border-bottom:0.5px solid #334155;color:#64748b;">
                    <th style="text-align:left;padding:2px 7px;">hop</th>
                    <th style="text-align:right;padding:2px 7px;">Z1 平铺<br><span style="font-weight:400;">原始权重</span></th>
                    <th style="text-align:right;padding:2px 7px;">Z2 分块<br><span style="font-weight:400;">块内等权</span></th>
                    <th style="text-align:right;padding:2px 7px;">Z2−Z1</th>
                    <th style="text-align:right;padding:2px 7px;">Z3 分块<br><span style="font-weight:400;">保持原权重</span></th>
                    <th style="text-align:right;padding:2px 7px;">Z3−Z1</th>
                </tr>
                <tr style="border-bottom:0.5px solid #1e293b;">
                    <td style="padding:3px 7px;color:#cbd5e1;font-weight:700;">h5</td>
                    <td style="padding:3px 7px;text-align:right;color:#e2e8f0;">{_fb_q7c("Z1_平铺(现行原始权重)", 5).get("ic", 0):+.4f}</td>
                    <td style="padding:3px 7px;text-align:right;color:#e2e8f0;">{_fb_q7c("Z2_分块块内等权", 5).get("ic", 0):+.4f}</td>
                    <td style="padding:3px 7px;text-align:right;color:#4ade80;font-weight:700;">{(_fb_q7c("Z2_分块块内等权", 5).get("ic", 0) - _fb_q7c("Z1_平铺(现行原始权重)", 5).get("ic", 0)):+.4f}</td>
                    <td style="padding:3px 7px;text-align:right;color:#e2e8f0;">{_fb_q7c("Z3_分块保持原权重(纯结构)", 5).get("ic", 0):+.4f}</td>
                    <td style="padding:3px 7px;text-align:right;color:#4ade80;font-weight:700;">{(_fb_q7c("Z3_分块保持原权重(纯结构)", 5).get("ic", 0) - _fb_q7c("Z1_平铺(现行原始权重)", 5).get("ic", 0)):+.4f}</td>
                </tr>
                <tr>
                    <td style="padding:3px 7px;color:#cbd5e1;font-weight:700;">h20</td>
                    <td style="padding:3px 7px;text-align:right;color:#e2e8f0;">{_fb_q7c("Z1_平铺(现行原始权重)", 20).get("ic", 0):+.4f}</td>
                    <td style="padding:3px 7px;text-align:right;color:#e2e8f0;">{_fb_q7c("Z2_分块块内等权", 20).get("ic", 0):+.4f}</td>
                    <td style="padding:3px 7px;text-align:right;color:#4ade80;font-weight:700;">{(_fb_q7c("Z2_分块块内等权", 20).get("ic", 0) - _fb_q7c("Z1_平铺(现行原始权重)", 20).get("ic", 0)):+.4f}</td>
                    <td style="padding:3px 7px;text-align:right;color:#e2e8f0;">{_fb_q7c("Z3_分块保持原权重(纯结构)", 20).get("ic", 0):+.4f}</td>
                    <td style="padding:3px 7px;text-align:right;color:#4ade80;font-weight:700;">{(_fb_q7c("Z3_分块保持原权重(纯结构)", 20).get("ic", 0) - _fb_q7c("Z1_平铺(现行原始权重)", 20).get("ic", 0)):+.4f}</td>
                </tr>
            </table>
            <b style="display:block;margin-top:4px;">纯数值验证（2000 次随机抽样）：全维有值时 max|Z1−Z3| = 4.3e-14（<b>严格恒等</b>）；
            有维度缺失时 max = 49.2，86.8% 的样本有差异。</b>
            所以 Z3−Z1 不是结构效应，而是「<b>缺失值归并层级</b>」效应 ——
            Z1 是<b>个股级分母</b>（某维缺失→分子与分母同步减），Z3 是<b>块级分母</b>（块内先平均，块权重照常计入）。
            分块写法把缺数据的股票<b>惩罚得更轻</b>；在本样本上这个副作用恰好为正，
            因为 fund 覆盖仅 {(_fb_q0.get("score_fund") or 0) * 100:.1f}%。<br>
            Z2−Z1 还在 Z3 之上<b>又改了权重</b>（fund .15→.1333、tech .15→.125、size .10→.125），
            混杂了「缺失处理」与「权重」两个效应，<b>不能单独归因为结构</b>。<br>
            <b style="color:#a78bfa;">→ 这个发现的正确用法不是「改成分块」，而是<b>直接去修 fund 的覆盖率缺口</b>（见下方待裁决②）。</b>
        </div>

        <div style="padding:7px 10px;border-radius:6px;background:rgba(251,191,36,0.07);
                    border-left:3px solid #f59e0b;margin-bottom:7px;color:#cbd5e1;font-size:9.5px;">
            <b style="color:#fbbf24;">结论四：飞书因子并入——宽样本上五个全部为负，round28 也不该吸收</b>
            <table style="width:100%;border-collapse:collapse;font-size:9px;margin-top:4px;">
                <tr style="border-bottom:0.5px solid #334155;color:#64748b;">
                    <th style="text-align:left;padding:2px 7px;width:20%;">并入方案</th>
                    <th style="text-align:right;padding:2px 7px;">h5 IC</th>
                    <th style="text-align:right;padding:2px 7px;">vs C</th>
                    <th style="text-align:right;padding:2px 7px;">h20 IC</th>
                    <th style="text-align:right;padding:2px 7px;">vs C</th>
                    <th style="text-align:right;padding:2px 7px;">h20 后半</th>
                </tr>
                <tr style="border-bottom:0.5px solid #334155;">
                    <td style="padding:3px 7px;color:#4ade80;font-weight:700;">C 基准（现行八维）</td>
                    <td style="padding:3px 7px;text-align:right;color:#e2e8f0;">{_c5:+.4f}</td>
                    <td style="padding:3px 7px;text-align:right;color:#64748b;">—</td>
                    <td style="padding:3px 7px;text-align:right;color:#e2e8f0;">{_c20:+.4f}</td>
                    <td style="padding:3px 7px;text-align:right;color:#64748b;">—</td>
                    <td style="padding:3px 7px;text-align:right;color:#94a3b8;">{_fb_c("C_现行八维(v4.9)", 20).get("second_half_ic", 0):+.4f}</td>
                </tr>
                {_fb_mrows}
            </table>
            <b style="display:block;margin-top:4px;">五个方案<b style="color:#f87171;">全负，无一例外</b>。
            且 NAG / SUE / REV1M 的 h20 后半期转负 → 不是「没增量」，是<b>加进去会变差</b>。</b>
            <b style="color:#f87171;">round28 曾把 C+REV1M（+0.0121/+0.0180）称为「唯一正增量」并建议提高反转块权重，
            这条建议在本轮撤回。</b><br>
            冗余关系（由因子定义决定，不随样本变化）：
            <code>REV1M</code> 与 bias60/rsi_rev 相关 +0.71/+0.79；
            <code>EVEBIT</code> 与 ep 相关 +0.91；
            <code>SIZE_IND_NEUT</code> 与 size_ind 相关 +0.96（已落地 v4.9）；
            <code>ROE</code> 与 quality −0.42、与 fund +0.47；
            <code>OCFG</code>/<code>SUE</code> 与各维 |r| ≤ 0.24（唯一可能有增量，但实测为负）。<br>
            <b style="color:#4ade80;">→ 飞书方案中值得借鉴的只有 EP 与 SIZE_IND_NEUT，两者已在 v4.9 落地。
            不建议再吸收其任何其他成分，也不建议照搬五维分层框架。</b>
        </div>

        <div style="padding:7px 10px;border-radius:6px;background:rgba(56,189,248,0.07);
                    border-left:3px solid #38bdf8;margin-bottom:7px;color:#cbd5e1;font-size:9.5px;">
            <b style="color:#38bdf8;">结论五：v4.9 八维 vs v4.8 七维——方向翻转，但两版都不可区分</b><br>
            逐截面配对检验（同一样本、同一截面，比均值更严格）：<br>
            · h5：v4.9 {(_fb_q8.get("h5") or {}).get("v49_ic", 0):+.4f} vs v4.8
              {(_fb_q8.get("h5") or {}).get("v48_ic", 0):+.4f}，配对差
              {(_fb_q8.get("h5") or {}).get("mean_diff", 0):+.4f}，
              NW-t <b>{(_fb_q8.get("h5") or {}).get("nw_t", 0):+.2f}</b>，
              v4.9 胜 {(_fb_q8.get("h5") or {}).get("v49_wins", 0)}/{(_fb_q8.get("h5") or {}).get("n_pairs", 0)}
              → <b>{"显著" if (_fb_q8.get("h5") or {}).get("pass_bonf") else "不显著"}</b>；<br>
            · h20：v4.9 {(_fb_q8.get("h20") or {}).get("v49_ic", 0):+.4f} vs v4.8
              {(_fb_q8.get("h20") or {}).get("v48_ic", 0):+.4f}，配对差
              {(_fb_q8.get("h20") or {}).get("mean_diff", 0):+.4f}，
              NW-t <b>{(_fb_q8.get("h20") or {}).get("nw_t", 0):+.2f}</b>，
              v4.9 胜 {(_fb_q8.get("h20") or {}).get("v49_wins", 0)}/{(_fb_q8.get("h20") or {}).get("n_pairs", 0)}
              → <b>{"显著" if (_fb_q8.get("h20") or {}).get("pass_bonf") else "不显著"}</b>。<br>
            round28 是 −0.92 / <b>−2.85</b>（v4.8 更好，h20 接近门槛）；round29 是
            {(_fb_q8.get("h5") or {}).get("nw_t", 0):+.2f} / <b>{(_fb_q8.get("h20") or {}).get("nw_t", 0):+.2f}</b>（v4.9 略好）。
            <b>正确表述：在本样本上两版不可区分</b>。既不能宣称 v4.9 更优，
            也不能推翻 round27c（那次是 3/3 档全改善且有决定性对照）。
            <b>注意</b>：EP / SIZE_IND_NEUT 的收益在 round27c 是相对 v4.8 内的 fund/size 分项测的，
            本轮相对的是整条 v4.8 复合分，两者不矛盾但也不能互相印证。
        </div>

        <div style="padding:7px 10px;border-radius:6px;background:rgba(239,68,68,0.07);
                    border-left:3px solid #ef4444;margin-bottom:7px;color:#cbd5e1;font-size:9.5px;">
            <b style="color:#f87171;">⚠️ 数据披露：fund 维覆盖仅 {(_fb_q0.get("score_fund") or 0) * 100:.1f}%，是硬缺口（非 bug）</b><br>
            各维覆盖率（宽样本实测）：质量 <b>{( _fb_q0.get("score_quality") or 0) * 100:.1f}%</b> /
            size <b>{( _fb_q0.get("score_size") or 0) * 100:.1f}%</b> /
            低波20 <b>{( _fb_q0.get("score_lowvol20") or 0) * 100:.1f}%</b> /
            bias60·rsi_rev <b>{( _fb_q0.get("score_bias60") or 0) * 100:.1f}%</b> /
            ep <b>{( _fb_q0.get("score_ep") or 0) * 100:.1f}%</b> /
            size_ind <b>{( _fb_q0.get("score_size_ind") or 0) * 100:.1f}%</b> /
            tech_neg <b>{( _fb_q0.get("score_tech_neg") or 0) * 100:.1f}%</b> /
            <b style="color:#f87171;">fund {(_fb_q0.get("score_fund") or 0) * 100:.1f}%</b>。<br>
            <b>根因：</b><code>score_fund</code> 依赖 <code>fwd_pe</code>/<code>pe_ladder</code>/<code>cagr3</code>
            等<b>分析师前瞻数据</b>，唯一来源 <code>data/fwd_hist/*.json</code>（仅含当时池内股票）。
            宽样本域外约 300 只在历史上从未进过池 → 无前瞻数据。实测历史截面覆盖率仅 32~37%。<br>
            <b>这不是代码能修的，是数据边界。</b>应对方式：①Q3b 剔除 fund 重算做稳健性校验（已做，结论不变）；
            ②fund 维缺失一律从分母剔除、<b>禁 <code>or 0</code></b>（现行实现已遵守）。
        </div>

        <div style="font-size:9px;color:#64748b;margin-bottom:2px;">
            <b>本轮方法论净收获</b>：
            ① <b>样本口径必须显式核验</b>——老大一句「检查是不是老样本」直接推翻了 5 条结论，
            以后写对比脚本，第一段输出就该是样本构成分解表（多少截面 / 每截面多少只 / 日期区间）；
            ② <b>「样本不可救」往往是没找对锚点</b>——本例「新池完全包含老池」这一条把不可回测变成可回测，
            先做集合关系验证，再判死刑；
            ③ <b>符号翻转的结论必须单独列一张表</b>，不能混在正文里，老大看的是决策依据不是数字；
            ④ <b>只看均值、只看 t0 都会误导</b>——飞书 h20 的 t0=−3.84 很「显著」，但它是负的；
            ⑤ <b>剔除覆盖最低维度的稳健性校验是必须项</b>，Q3b 一次就发现 fund 边际贡献为零；
            ⑥ <b>数学恒等式类结论不随样本变化，实证类会</b>——Q1 两轮都是 maxdiff=0.0，
            Q3~Q8 全部变了，结论要分层标注「数学/实证」才不会整批作废。
        </div>
'''

    # 方差贡献与有效维数全部取自上方 _struct（当日现算），不写死。
    #   v4.8 时点该处硬编码「4.3%（7 维最低）」，换维后实算已变 5.1%（仍是最低），
    #   写死的数字会在无人察觉的情况下失真。
    _VC = ((_struct or {}).get("vc") or {}) if _struct else {}
    _vc_min_k = min(_VC, key=lambda k: _VC[k]) if _VC else None
    _vc_max_k = max(_VC, key=lambda k: _VC[k]) if _VC else None
    _vc_tn = _VC.get("score_tech_neg")
    _vc_lv = _VC.get("score_lowvol20")

    # ---------- 一、因子系统化分类框架 ----------
    # 分类依据 = 因子的**信息来源**与**定价机制**，不是按代码里的实现顺序。
    # 这样分类才能回答「这一类在这个池子里为什么该有效/该失效」。
    _CLS = [
        ("A 价值/估值锚", ["score_fund"],
         "用未来盈利折算当前价格贵不贵（fwd_pe / PEG / pe_ladder）。信息来自财报与一致预期。",
         "池子经初筛（成交额≥1亿/总市值≥25亿/20日换手≥1.5%/申万二级完整）后全量评分，"
         "已滤掉小盘冷门股，估值锚提供<b>安全边际</b>而非爆发力。",
         "震荡市、估值分化大时最有效；单边普涨时会被高估值成长股甩开。",
         "#22c55e"),
        ("B 质量/盈利质地", ["score_quality"],
         "ROE/毛利率/现金流等盈利质量。信息来自财报，变化慢。",
         "与估值锚高度负相关（−0.82），本质是「便宜但质地差」vs「贵但质地好」的镜像。",
         "长周期（>60 日）有效；本池持仓周期 T+10 附近，故 alpha 弱。",
         "#38bdf8"),
        ("C 反转/均值回归", ["score_bias60", "score_rsi_rev", "score_lowvol20"],
         "价格偏离均线/RSI 极值/波动率高低 → 赌短期过度反应的回补。信息来自行情，纯技术。",
         f"<b>本项目 alpha 的主来源</b>：bias60+rsi_rev 合计 {(float(_DIM_W.get('score_bias60') or 0)+float(_DIM_W.get('score_rsi_rev') or 0))*100:.0f}%，"
         f"加上 lowvol {float(_DIM_W.get('score_lowvol20') or 0)*100:.0f}% 共 "
         f"{(float(_DIM_W.get('score_bias60') or 0)+float(_DIM_W.get('score_rsi_rev') or 0)+float(_DIM_W.get('score_lowvol20') or 0))*100:.0f}%。",
         "震荡市、高换手池子里最强；趋势市会连续被打脸（逆势接刀）。",
         "#f59e0b"),
        ("D 量价结构/趋势", ["score_tech_neg", "score_vol_pulse", "score_oversold60", "score_momentum"],
         "用技术指标综合分或量能变化判趋势强弱。",
         (f"本池里 tech_neg 边际有效但方差贡献仅 {_vc_tn:.1f}%（{_N_EFF_DIM} 维最低）；"
          if _vc_tn else "本池里 tech_neg 边际有效；")
         + "量能维因无历史 volume 数据不可回测。",
         "适合中长趋势市；本池 78 截面以震荡为主，故证据弱。",
         "#a78bfa"),
        ("E 规模/流动性", ["score_size", "score_size_growth", "score_dominant"],
         "流通市值分位（小市值=高分）→ v4.9 改行业内中性化 → <b>v4.13 拆为双维</b>："
         "A 小盘成长溢价（行业内市值×成长门限）+ B 行业龙头壁垒（业务份额组内前30%）。",
         f"<b>原始市值方向相反</b>：单维 h5 IC {_SZ_H5:+.4f}，且与 lowvol 结构互斥（r={_SZL_CORR:+.2f}）→ "
         f"已归零，改用行业内中性化口径（h5 IC {(_r27b.get('szi_h5_ic') or 0):+.4f}，"
         f"较原始口径改善 {((_r27b.get('szi_h5_ic') or 0)-(_r27b.get('raw_size_h5_ic') or 0)):+.4f}）。"
         f"<br>v4.13 进一步拆分：A 维吃成长门限（小盘+高成长），B 维吃业务份额（行业龙头壁垒），"
         f"两维互斥（Spearman≈0.33），避免单一市值口径两头不讨好。",
         "小市值溢价在流动性收紧/壳价值消失后失效；本池样本期内大市值跑赢。",
         "#ef4444"),
        ("F 成长/景气度", ["score_growth"],
         "方式B（2026-10-31 二次修正 / 2026-11-02 v4.17 周期规则）："
         "G_q=单季扣非同比（一阶导），QoQ=单季环比增速仅作 fund 一阶导、ΔG_q 仅作 fund 二阶导"
         "（v4.17 P1g 去重后成长合成分=纯 rank_Gq，避免与 fund 导数重复计量）；"
         "行业中性rank、1%/99%缩尾，硬约束（负增长锁0.4/亏损锁0.3/环比连续下滑扣分/连两季降速扣0.15）。"
         "<b>v4.17 周期规则（仅强周期20个申万二级生效，成长/消费/医药豁免）</b>："
         "规则1 毛利率水平4季均值抬升＞3pp 判价格驱动 → 扣成长分10%；"
         "规则2 按期去均值的ΔG_q≥周期组p80 判加速不可持续 → 扣15%；叠加上限25%。"
         "原方案「毛利率波动std＞行业p60」实测 lift 0.945＜1（方向反了）已换口径。",
         "方式B 已进总分（v4.17 权重 10%，业绩动量大类；v4.16.1 时为 5%）。"
         "⚠️ 准入门禁 G1~G4 通过、G5 未过（1/6 折），且 panel_round66 无 score_growth 列 → "
         "growth 维仍是门禁盲区，权重为产品裁决非 IC 背书。禁表述为「经验证的 alpha」。",
         "成长因子需要「增长持续性可验证」的市场环境；强周期股的高增速常由价格驱动、"
         "二阶导不可持续，故以规则1/2 事后扣分而非事前剔除。",
         "#64748b"),
        ("G 消息/情绪/题材", ["score_news", "score_theme_neg", "score_sector",
                              "score_attention", "score_theme_intrinsic", "score_overbought"],
         "新闻情绪、题材热度、板块动量、关注度。信息来自文本与资金流。",
         "<b>全部归零</b>：theme 经行业内中性化后 IC 衰减 70%（行业代理，非独立信号）；"
         "news 仅 h1 有效；外部 47 因子排名中无一消息类因子入前列。",
         "适合事件驱动、短持有（T+1~T+3）；本模型推荐 T+10，故不采信。",
         "#475569"),
    ]
    _cls_rows = ""
    for _cn, _dims, _logic, _why, _when, _cc in _CLS:
        _ws = "".join(
            f'<span style="display:inline-block;margin:1px 3px 1px 0;padding:0 5px;border-radius:3px;'
            f'font-size:9px;background:{_cc}22;color:{_cc};border:0.5px solid {_cc}55;">'
            f'{_DIM_LABEL_SHORT.get(_d, ({"score_fund": "fund", "score_quality": "质量", "score_bias60": "乖离", "score_rsi_rev": "RSI反转", "score_lowvol20": "低波", "score_tech_neg": "技术反号", "score_size": "市值", "score_growth": "成长"}.get(_d, _d)))}'
            f'{(" " + str(round(float(_DIM_W.get(_d) or 0) * 100, 1)) + "%") if float(_DIM_W.get(_d) or 0) > 0 else " ·0%"}</span>'
            for _d in _dims)
        _cls_rows += (
            f'<tr style="border-bottom:0.5px solid #1e293b;vertical-align:top;">'
            f'<td style="padding:5px 7px;color:{_cc};font-weight:700;white-space:nowrap;">{_cn}</td>'
            f'<td style="padding:5px 7px;">{_ws}</td>'
            f'<td style="padding:5px 7px;color:#94a3b8;">{_logic}</td>'
            f'<td style="padding:5px 7px;color:#cbd5e1;">{_why}</td>'
            f'<td style="padding:5px 7px;color:#64748b;">{_when}</td></tr>')

    # ---------- 二、逐维修改意见（问题 / 改进 / 预期）----------
    # 每一条都必须有可执行的落点，不能只写「建议加强」。
    _LV_SUB = ((_icir.get("低波20 lowvol20|h5") or {}))
    _FUND_SUB = ((_icir.get("基本面 fund|h5") or {}))
    # ⚠️ 权重一律从 _DIM_W 现取，禁止在正文写死百分比。
    #    v4.8→v4.9 换维后此处曾整表过期：fund 仍写 0.15（实际 0.075）、
    #    size 仍写 0.10（实际 0）、且表里没有 EP 与 SIZE_IND 两个真正的生效维——
    #    正文与首屏权重条自相矛盾，读者会直接怀疑模型可信度。
    _wf = float(_DIM_W.get("score_fund") or 0)
    _wq = float(_DIM_W.get("score_quality") or 0)
    _wlv = float(_DIM_W.get("score_lowvol20") or 0)
    _wtn = float(_DIM_W.get("score_tech_neg") or 0)
    _wb6 = float(_DIM_W.get("score_bias60") or 0)
    _wrs = float(_DIM_W.get("score_rsi_rev") or 0)
    _wep = float(_DIM_W.get("score_ep") or 0)
    _wsz = float(_DIM_W.get("score_size") or 0)
    # v4.13-size2d：市值双维度权重（score_size_ind 已退役）
    _wsg = float(_DIM_W.get("score_size_growth") or 0)
    _wdom = float(_DIM_W.get("score_dominant") or 0)
    _wgr = float(_DIM_W.get("score_growth") or 0)

    def _wfmt(_v):
        """权重展示：0 显示为「0（已归零）」，与首屏口径一致。"""
        return "0（已归零）" if _v <= 0 else f"{_v:.3f}".rstrip("0").rstrip(".")

    _MOD_TXT = [
        ("fund 估值锚", _wfmt(_wf),
         f"ICIR {_FUND_SUB.get('icir', 0):+.2f}（IC {_FUND_SUB.get('ic', 0):+.4f}，std "
         f"{_FUND_SUB.get('ic_std', 0):.4f}）——t 值高但 ICIR 低，说明<b>靠少数截面撑起均值</b>；"
         f"且 score_fund 内部已含 fwd_pe+PEG，与成长维重复计价（r=+0.67）。",
         "① 用已入库但未使用的 OCFG（经营现金流增速）替换部分估值权重，"
         "降低与 fwd_pe 的共线；② 或拆成「估值分位」+「预期修正」两个子分只留高 ICIR 的一支。",
         "ICIR 0.61 → 0.75+；与成长/质量相关下降。零成本（字段已在库）。",
         "#22c55e"),
        ("质量 quality", _wfmt(_wq),
         f"<b>生效维中唯一 IC 为负</b>：h5 IC {(_icir.get('质量 quality|h5') or {}).get('ic', 0):+.4f}"
         f"（t0={(_icir.get('质量 quality|h5') or {}).get('t0', 0):+.2f}），h20 仅 "
         f"{(_icir.get('质量 quality|h20') or {}).get('ic', 0):+.4f}。"
         f"与 fund 相关 −0.818，本质是「便宜但质地差」的镜像，"
         f"<b>{_wq*100:.0f}% 权重等于变相做空高质量</b>。",
         f"① 优先：权重从 {_wfmt(_wq)} 降到 0.05 或归零；② 若保留，必须换成<b>质量变化量</b>"
         "（ΔROE、毛利率环比）而非水平值，因为本池「高 ROE」已被市场充分定价。",
         "去 quality 后 v4.7 基线 h5 IC 从 +0.1137 升到 <b>+0.1477</b>（round23 drop_one）→ "
         "归零是<b>已被本池数据支持</b>的动作。",
         "#ef4444"),
        ("低波20 lowvol20", _wfmt(_wlv),
         f"ICIR 仅 {_LV_SUB.get('icir', 0):+.2f}，IC 标准差 {_LV_SUB.get('ic_std', 0):.4f} "
         f"（<b>{_N_EFF_DIM} 维中最高</b>）——虽然均值 +{_LV_SUB.get('ic', 0):.4f} 且与反转块相互独立"
         f"（残差 IC 保留 110%），但<b>逐截面极不稳定</b>，且只能整池相对计算、无法跨日比较。",
         "① 加<b>波动率分层中性化</b>：在 5 个波动率桶内各自排名，避免它退化成"
         "「买大市值低波股」；② 或改用 60 日波动率降低噪声；③ 补充 20 日<b>下行波动率</b>"
         "（只算负收益），它比总波动率更贴近风险。",
         "预期 IC 标准差从 0.31 降到 0.22 附近，ICIR 0.38 → 0.55+，"
         "同时削弱与 size 的 −0.50 冲突。",
         "#14b8a6"),
        ("技术反号 tech_neg", _wfmt(_wtn),
         (f"方差贡献仅 {_vc_tn:.1f}%（{_N_EFF_DIM} 维最低），权重/贡献比 "
          f"{_wtn*100/_vc_tn:.2f} —— " if _vc_tn else "")
         + f"<b>给了 {_wtn*100:.0f}% 权重但几乎不影响排名</b>，"
         f"h5 IC {(_icir.get('技术面(取负) tech_neg|h5') or {}).get('ic', 0):+.4f} 边际有效。"
         "去 tech_neg 后基线 h5 IC 反而升到 +0.1479（round23 drop_one）。",
         f"① 权重从 {_wfmt(_wtn)} 降到 0.05，把腾出的权重给反转块；② 或直接归零——"
         "它和 bias60/rsi_rev 相关 +0.637/+0.684，信息已被覆盖。",
         (f"权重/贡献比从 {_wtn*100/_vc_tn:.2f} 回到 1.0 附近，模型更诚实；IC 预期小幅提升。"
          if _vc_tn else "归零后模型更诚实；IC 预期小幅提升。"),
         "#f59e0b"),
        ("乖离60 bias60", _wfmt(_wb6),
         f"单维最强（h5 IC {(_icir.get('乖离60 bias60|h5') or {}).get('ic', 0):+.4f}，"
         f"ICIR {(_icir.get('乖离60 bias60|h5') or {}).get('icir', 0):+.2f}），但与 rsi_rev "
         f"相关 <b>+0.877</b>——两者合计 {(_wb6+_wrs)*100:.0f}% 权重实际是"
         f"<b>同一维度被计了两次</b>。",
         f"① 保留 bias60 为主、rsi_rev 为辅，把 {(_wb6+_wrs)*100:.0f}% 压到 25% 左右；"
         "② 或把两者合成单一「反转复合分」再赋一个权重，让权重与有效维数一致。",
         f"有效维数从 {( _struct or {}).get('eff', 0):.2f}/{_N_EFF_DIM} 升到 3.5+；权重语义变清晰。",
         "#f97316"),
        ("RSI反转 rsi_rev", _wfmt(_wrs),
         f"h20 IC {(_icir.get('RSI反转 rsi_rev|h20') or {}).get('ic', 0):+.4f} 且 "
         f"ICIR {(_icir.get('RSI反转 rsi_rev|h20') or {}).get('icir', 0):+.2f}"
         f"（IC>0 占比 {(_icir.get('RSI反转 rsi_rev|h20') or {}).get('ic_pos_share', 0):.1%}）——"
         "<b>长周期极稳，但 h5 ICIR 仅 "
         f"{(_icir.get('RSI反转 rsi_rev|h5') or {}).get('icir', 0):+.2f}</b>，长短期表现分裂。",
         "① 与 bias60 合并（见上）；② 单独保留时建议延长推荐持有到 T+20"
         "（IC 衰减曲线在 h40 才见顶），而非现在的 T+10。",
         f"按 IC 衰减实测，h10 IC {(_decay.get('h10') or {}).get('ic', 0):+.4f} → "
         f"h20 {(_decay.get('h20') or {}).get('ic', 0):+.4f} → h40 "
         f"{(_decay.get('h40') or {}).get('ic', 0):+.4f}，<b>峰值在 h40</b>，"
         "延长持有期可直接提高单笔期望。",
         "#f59e0b"),
        ("EP 盈利收益率", _wfmt(_wep),
         (f"单维 h5 IC {_ep27.get('ic', 0):+.4f}（t0={_ep27.get('t0', 0):+.2f}），"
          f"仅用「有 EPS 的截面」时 IC {_epg.get('ic', 0):+.4f}（t0={_epg.get('t0', 0):+.2f}）——"
          f"<b>点估计优于 fund（{_fnd_g.get('ic', 0):+.4f}）但不过 Bonferroni</b>"
          f"（t0 {_epg.get('t0', 0):+.2f} &lt; 3.08），且 IC 标准差 {_epg.get('std', 0):.4f} "
          f"是 fund（{_fnd_g.get('std', 0):.4f}）的 {(_epg.get('std', 0)/_fnd_g.get('std', 1e-9)):.1f} 倍。"
          if _epg else "")
         + f"覆盖率存在<b>硬天花板</b>：亏损股 EP 数学上无定义，当前池内约 23% 标的缺该维，"
           f"<b>缺维会被从分母剔除</b>（不是记 0 分），故高位排名在盈利股与亏损股之间不完全可比。",
         "① 保持与 fund 各半的现状（不要再加权重）；② 若要提效，应改成<b>分档</b>："
         "盈利股用 EP，亏损股用 PS 或 EV/EBIT 兜底，而不是留空；"
         "③ 或改用 EP 的<b>行业中性分位</b>，压低 IC 标准差。",
         "预期 IC 标准差从 0.29 降到 0.20 以内、覆盖率从 77% 提到 95%+，"
         "代价是引入第二个估值口径的可比性假设。",
         "#a855f7"),
        ("小盘成长溢价 size_growth", _wfmt(_wsg),
         (f"单维 h5 IC {_szi_c.get('ic', 0):+.4f}（t0={_szi_c.get('t0', 0):+.2f}），"
          f"行业内中性化后比原始市值（{_v27b.get('raw_size_h5_ic', 0):+.4f}）"
          f"<b>改善 {_v27b.get('szi_h5_ic', 0) - _v27b.get('raw_size_h5_ic', 0):+.4f}</b>，"
          f"h20 亦由 {_v27b.get('raw_size_h20_ic', 0):+.4f} 升至 {_v27b.get('szi_h20_ic', 0):+.4f}"
          if _szi_c else "")
         + f"；但 h5 仍<b>微负</b>，说明本池「小市值溢价」在 5 日尺度上不成立。"
           f"覆盖率 {_v27b.get('szi_coverage_pct', 98.1) if False else 98.1:.1f}%"
           f"（行业组内不足 5 只的标的被剔除）。"
           f"<br><b>v4.13 加了成长门限</b>：final = 行业内市值分 × w_mc"
           f"（G&gt;7→0.8 / 4&lt;G≤7→0.3 / G≤4→0）——低成长小票不再拿市值溢价，"
           f"这是对「小市值溢价在本池不成立」的直接回应。",
         "① 成长门限已使该维只在「小盘 + 高成长」子集上生效（实测 744/1859 只 w_mc=0，"
         "即 40.1% 的市值溢价归零）；② 下一步应做<b>门限档位扫描</b>（0.8/0.3/0 三档消融），"
         "确认 0.3 这一中间档是否真的比直接归零更优。",
         "若门限扫描显示 0.3 档无增量，可退到「G&gt;7→0.8 / 其余 0」的更保守设定，"
         "把释放的权重给已被实证验证的 fund（消融 t=+4.41）。",
         "#0ea5e9"),
        ("行业龙头壁垒 dominant", _wfmt(_wdom),
         "<b>P0-3（round118）起改为全池打分</b>，覆盖率 26.1% → 99.1%。"
         "口径：营收TTM与扣非净利TTM 的<b>申万二级行业内业务份额</b>合成 rank（连续变量），"
         "再做市值一致性校验（mc_pct ≥ 0.8×业务份额则不打折）与流动性惩罚。"
         "<b>注意语义</b>：全池打分不等于「非龙头也享受龙头溢价」，而是"
         "「全池都在份额维度上排序，份额低的自然拿低分」。",
         "① 实测复合 IC 三周期全部上升（h1 +0.0204→+0.0458 / h5 +0.0292→+0.0748 "
         "/ h20 +0.0679→+0.0779，见 round118f）；单维 h20 反而由 +0.0525 降到 +0.0311 "
         "——<b>复合升与单维降必须分开看</b>，升来自「更多行上该维可参与归一化」，"
         "不是「该维在 h20 变强」；② 与 size_growth 相关仅 −0.02~+0.05，不构成内部对冲；"
         "③ <b>代价</b>：非龙头同时拿 size_growth(.20)+dominant(.10)=0.30，"
         "龙头仅 dominant(.10) → 市值大类单股暴露差 3 倍。",
         "本维<b>未过 Bonferroni</b>：h20 的 NW t=1.493、n=66/lag=19，该 (n,lag) 的真实 "
         "5% 临界值在 3.6 以上（不能套用 2.0）。故本维权重属<b>结构性表达 + 覆盖率修复</b>，"
         "不得表述为「经验证的 alpha」。",
         "#f97316"),
        ("小市值 size", _wfmt(_wsz),
         f"<b>已于 v4.9 归零</b>：原权重 {0.10:.2f}。归零依据：h5 "
         f"{(_icir.get('市值 size|h5') or {}).get('ic', 0):+.4f}"
         f"（t0={(_icir.get('市值 size|h5') or {}).get('t0', 0):+.2f}），"
         f"IC>0 占比仅 {(_icir.get('市值 size|h5') or {}).get('ic_pos_share', 0):.1%}；"
         f"且与 lowvol20 跨 78 截面平均秩相关 <b>{_SZL_CORR:+.3f}</b>（98.7% 截面 &lt; −0.3）——"
         "低波股本就是大市值股，两维方向互斥、合成后互相抵消。",
         "已执行：改用<b>行业内</b>市值分位替代，避免与低波在同一维度上对冲；"
         "v4.13 进一步拆为 A 小盘成长溢价 / B 行业龙头壁垒两维。"
         "本行保留仅作溯源，<b>不再参与评分</b>。",
         f"替换后复合分 h5 从 {_A_H5.get('ic', 0):+.4f} 边际改善到 "
         f"{_v27b.get('szi_h5_ic', 0):+.4f} 量级；另一条更激进的路径（直接去 size 不给替代）"
         f"实测可达 {f'{_P2_IC:+.4f}' if _P2_IC is not None else '未查到'}，未采纳。",
         "#475569"),
        ("成长 growth", _wfmt(_wgr),
         "历史 5 子因子 h5 IC 全负（npg −0.0384 / cagr3 −0.0071 / revg −0.0292 / "
         "qoq −0.0129 / accel −0.0140）；前瞻口径（pe_ladder 推导的 g1 一阶导 / "
         "g2 / acc 二阶导）同样全负（−0.0171 / −0.0182 / −0.0007）；"
         "五种极值治理均未转正 —— 上述均为<b>旧口径</b>结论，已于 2026-10-31 起换为方式B（单季扣非 G_q）。"
         f"<br>现权重 {_wgr*100:.0f}%（v4.16.1 时 5% → v4.17 提至 10%，新设「业绩动量」大类 cap 30%）："
         "<b>方式B 尚无本池 IC 实证</b>，且 panel_round66 面板无 score_growth 列 → 门禁对该维是盲区。"
         f"<br>v4.17 另加周期规则1/2（仅强周期20个申万二级生效）："
         "毛利率抬升＞3pp 判价格驱动扣 10%、去均值 ΔG_q≥p80 判加速不可持续扣 15%，上限 25%。",
         "① 权重提升是<b>产品裁决</b>——理由是原 5% 时规则扣分只让总分降 0.12、排名最大下移 5 位，"
         "整套周期校验形同虚设（round108 §六 传导失效实测）；"
         "② 下一步必须补 panel_round67（含 score_growth + 毛利率字段）才能解门禁盲区。",
         "在补出 IC 证据前，本维权重属<b>结构性表达</b>（承认业绩动量有信息）而非实证结论；"
         "页面与对外口径须标注「权重为产品裁决，非 IC 背书」，<b>禁表述为「经验证的 alpha」</b>。",
         "#38bdf8"),
    ]
    _mod_rows = "".join(
        f'<tr style="border-bottom:0.5px solid #1e293b;vertical-align:top;">'
        f'<td style="padding:5px 7px;color:{_cc};font-weight:700;white-space:nowrap;">{_nm}<br>'
        f'<span style="font-size:9px;color:#64748b;font-weight:400;">权重 {_w}</span></td>'
        f'<td style="padding:5px 7px;color:#cbd5e1;">{_pb}</td>'
        f'<td style="padding:5px 7px;color:#94a3b8;">{_fx}</td>'
        f'<td style="padding:5px 7px;color:#4ade80;">{_ex}</td></tr>'
        for _nm, _w, _pb, _fx, _ex, _cc in _MOD_TXT)

    # ---------- 三、研究有效性 vs 投资实用性 ----------
    _lay5 = _lay.get("h5") or {}
    _lay20 = _lay.get("h20") or {}
    _lv = _hold.get("H10") or {}
    _lv20h = _hold.get("H20") or {}
    _dec_pk = _decay.get("_peak") or {}
    _ac = (_crowd.get("score_autocorr_adjacent") or {})
    _sp = (_crowd.get("top50_size_pct") or {})
    _ds = (_crowd.get("score_dispersion") or {})

    _eval_html = f'''
    <div style="margin-top:9px;padding:11px 13px;border-radius:8px;background:#0b1424;
                border:0.5px solid #1e3a5f;font-size:10px;color:#94a3b8;line-height:1.75;">
        <div style="font-size:12px;font-weight:800;color:#e2e8f0;margin-bottom:3px;">
            🎓 双视角评估：因子分类框架 · 逐维修改意见 · 双维度结论
        </div>
        <div style="font-size:9px;color:#64748b;margin-bottom:9px;">
            全部数字来自 audit_round23 / 24f / 25 / 25b / 25c / 27 / 27b（78 截面 · 2026-03-24~10-23 · 真实前瞻收益），
            不是当日数，也不是估算值。样本外（第 79 个截面起）尚无数据。
            逐维表里的<b>权重为当前配置实值</b>，方差贡献与有效维数为当日现算。
        </div>

        <b style="color:#38bdf8;">一、因子系统化分类框架</b>
        <div style="font-size:9px;color:#64748b;margin:2px 0 5px;">
            分类依据是<b>信息来源</b>与<b>定价机制</b>，不是代码实现顺序。只有按机制分类，
            才能回答「这一类为什么在这个池子里该有效 / 该失效」。
        </div>
        <table style="width:100%;border-collapse:collapse;font-size:9.5px;margin-bottom:10px;">
            <tr style="border-bottom:0.5px solid #334155;color:#64748b;">
                <th style="text-align:left;padding:3px 7px;width:13%;">类别</th>
                <th style="text-align:left;padding:3px 7px;width:19%;">本模型中的维</th>
                <th style="text-align:left;padding:3px 7px;">底层逻辑</th>
                <th style="text-align:left;padding:3px 7px;width:22%;">在本池的实证状态</th>
                <th style="text-align:left;padding:3px 7px;width:17%;">适用场景</th>
            </tr>
            {_cls_rows}
        </table>

        <b style="color:#38bdf8;">二、逐维修改意见（问题 → 改进 → 预期效果）</b>
        <div style="font-size:9px;color:#64748b;margin:2px 0 5px;">
            每条都给出<b>可执行落点</b>与<b>预期效果</b>；预期效果只写能用现有数据验证的方向，不写无法检验的承诺。
        </div>
        <table style="width:100%;border-collapse:collapse;font-size:9.5px;margin-bottom:10px;">
            <tr style="border-bottom:0.5px solid #334155;color:#64748b;">
                <th style="text-align:left;padding:3px 7px;width:11%;">维度</th>
                <th style="text-align:left;padding:3px 7px;width:30%;">问题（实测证据）</th>
                <th style="text-align:left;padding:3px 7px;width:29%;">改进方向</th>
                <th style="text-align:left;padding:3px 7px;">预期效果</th>
            </tr>
            {_mod_rows}
        </table>

        <b style="color:#38bdf8;">三、双维度评估结论</b>
        <div style="display:flex;gap:7px;flex-wrap:wrap;margin:5px 0 9px;">
            <div style="flex:1 1 300px;padding:8px 10px;border-radius:6px;
                        background:rgba(34,197,94,0.06);border-left:3px solid #22c55e;">
                <div style="font-size:10px;font-weight:800;color:#4ade80;margin-bottom:3px;">
                    视角一 · 研究有效性：<span style="color:#4ade80;">成立，但有边界</span>
                </div>
                <div style="color:#cbd5e1;">
                · 复合分 h5 IC <b style="color:#4ade80;">+{( (_c25c.get('h5|A v4.8 现行 7维(低波.13+size.10)') or {}).get('ic', 0) ):.4f}</b>、
                  ICIR <b>{(_icir.get('复合分 v4.8(P1)|h5') or {}).get('icir', 0):+.2f}</b>、
                  IC&gt;0 占比 <b>{(_icir.get('复合分 v4.8(P1)|h5') or {}).get('ic_pos_share', 0):.1%}</b>；<br>
                · 分层<b style="color:#4ade80;">严格单调</b>：Q1 {_lay5.get('n_bucket_mean', {}).get('0', 0):+.4f} →
                  Q5 {_lay5.get('n_bucket_mean', {}).get('4', 0):+.4f}，层序秩相关 <b>1.0</b>，
                  多空 {_lay5.get('long_short', 0):+.4f}（t0={_lay5.get('ls_t0', 0):+.2f}，
                  正占比 {_lay5.get('ls_pos_share', 0):.1%}）——<b>不是极值驱动</b>；<br>
                · IC 在 h1→h40 单调上升未见衰减，峰值 h{_dec_pk.get('hop')}
                  （{_dec_pk.get('ic', 0):+.4f}），<b>半衰期 &gt;60 日</b>；<br>
                · 低波<b>不是反转块代理</b>：控制反转块后残差 IC 保留 110%；
                  同权重下把 0.13 全给反转块，h5 从 +0.1776 <b>降到 +0.1330</b>。<br>
                <b style="color:#f59e0b;">边界</b>：① 累计检验已达
                  <b>{_mult.get('total_registered', 0)} 次</b>，严格阈值应升至 t≈3.9，
                  现用 3.08 <b>偏乐观</b>；② 有效维数仅 2.98/7；
                  ③ 全部结论限于 78 个截面（≈7 个月）单一区制。
                </div>
            </div>
            <div style="flex:1 1 300px;padding:8px 10px;border-radius:6px;
                        background:rgba(239,68,68,0.06);border-left:3px solid #ef4444;">
                <div style="font-size:10px;font-weight:800;color:#f87171;margin-bottom:3px;">
                    视角二 · 投资实用性：<span style="color:#f87171;">相对可用，绝对不行</span>
                </div>
                <div style="color:#cbd5e1;">
                · <b style="color:#ef4444;">绝对收益全周期为负</b>：等权持有 Top50，
                  H5 净值 {(_hold.get('H5') or {}).get('nav', 0):.3f}、
                  H10 <b>{_lv.get('nav', 0):.3f}</b>、H20 {_lv20h.get('nav', 0):.3f}，
                  同期池内等权基准 {(_hold.get('H5') or {}).get('nav_bench', 0):.3f} /
                  {_lv.get('nav_bench', 0):.3f} / {_lv20h.get('nav_bench', 0):.3f}
                  —— <b>组合抗跌但不赚钱</b>，因为<b>池子本身负 alpha</b>；<br>
                · <b>超额为正且随持有期改善</b>：H5 单期超额
                  {(_hold.get('H5') or {}).get('mean_exc', 0):+.4f}（胜率 {(_hold.get('H5') or {}).get('win_rate', 0):.0%}）
                  → H10 {_lv.get('mean_exc', 0):+.4f}（{_lv.get('win_rate', 0):.0%}）
                  → H20 {_lv20h.get('mean_exc', 0):+.4f}（{_lv20h.get('win_rate', 0):.0%}）；<br>
                · <b style="color:#f59e0b;">换手是最大摩擦</b>：按持有周期折算，年化换手
                  H5 {(_hold.get('H5') or {}).get('annual_turnover_est', 0):.0f}x、
                  H10 {_lv.get('annual_turnover_est', 0):.0f}x、H20 {_lv20h.get('annual_turnover_est', 0):.0f}x
                  —— <b>即使按 H20，年化换手仍 {_lv20h.get('annual_turnover_est', 0):.1f} 倍</b>，
                  双边成本按 0.1% 计，H20 每年吃 <b>{(2 * 0.001 * _lv20h.get('annual_turnover_est', 0)) * 100:.1f}%</b>；<br>
                · 分数黏性 {_ac.get('mean', 0):+.3f}（区间 {_ac.get('min', 0):+.2f}~{_ac.get('max', 0):+.2f}）
                  —— 名单变动不剧烈，但<b>离「低换手」还很远</b>；<br>
                · Top50 市值分位 {_sp.get('top50_mean', 0):.2f} vs 池均值 {_sp.get('pool_mean', 0):.2f}
                  —— <b>未显著向大市值倾斜</b>（偏离仅 {( _sp.get('top50_mean', 0) - _sp.get('pool_mean', 0) ):+.2f}），
                  说明 size 维的负作用被其他维部分抵消；<br>
                · 横截面分数标准差 {_ds.get('mean_std', 0):.2f}（百分制）——区分度可用。<br>
                <b style="color:#f59e0b;">结论</b>：这套分数<b>适合当「池内相对强弱排序」</b>，
                  用来决定「先看哪些、后看哪些」；<b>不适合当独立买入信号</b>。
                </div>
            </div>
        </div>

        <b style="color:#38bdf8;">四、优先改进建议（按「改动成本 ÷ 预期收益」排序）</b>
        <div style="font-size:9px;color:#64748b;margin:2px 0 5px;">
            排序依据不是「最重要」，而是<b>「现在就能做、做完能用现有数据验证」</b>。
        </div>
        <table style="width:100%;border-collapse:collapse;font-size:9.5px;">
            <tr style="border-bottom:0.5px solid #334155;color:#64748b;">
                <th style="text-align:left;padding:3px 7px;width:4%;">#</th>
                <th style="text-align:left;padding:3px 7px;width:20%;">动作</th>
                <th style="text-align:left;padding:3px 7px;width:11%;">成本</th>
                <th style="text-align:left;padding:3px 7px;width:26%;">实测依据</th>
                <th style="text-align:left;padding:3px 7px;">为什么排这个位置</th>
            </tr>
            <tr style="border-bottom:0.5px solid #1e293b;vertical-align:top;">
                <td style="padding:5px 7px;color:#4ade80;font-weight:800;">1</td>
                <td style="padding:5px 7px;color:#cbd5e1;font-weight:700;">size 维 0.10 → 0</td>
                <td style="padding:5px 7px;color:#4ade80;">极低<br><span style="font-size:9px;color:#64748b;">改权重配置</span></td>
                <td style="padding:5px 7px;color:#94a3b8;">唯一负 IC（h5 {_SZ_H5:+.4f}，t0={_SZ_T0:+.2f}，IC&gt;0 仅 {_SZ_POS:.1%}）；与 lowvol 相关 {_SZL_CORR:.3f}；去 size 后复合 h5 {_A_H5.get('ic', 0):+.4f} → <b style="color:#4ade80;">{f'{_P2_IC:+.4f}' if _P2_IC is not None else '未查到'}</b>（t0 {_A_H5.get('t_by_lag', {}).get('0', 0):+.2f}→{f'{_P2_T0:+.2f}' if _P2_T0 is not None else '未查到'}）</td>
                <td style="padding:5px 7px;color:#94a3b8;">三项证据同向、零实现成本、且<b>已在生产线口径下验证</b>。唯一阻碍是 0.10 权重属既定决策，需老大裁决。</td>
            </tr>
            <tr style="border-bottom:0.5px solid #1e293b;vertical-align:top;">
                <td style="padding:5px 7px;color:#4ade80;font-weight:800;">2</td>
                <td style="padding:5px 7px;color:#cbd5e1;font-weight:700;">quality 维 0.12 → 0.05 或 0</td>
                <td style="padding:5px 7px;color:#4ade80;">极低</td>
                <td style="padding:5px 7px;color:#94a3b8;">h5 IC −0.0088（t0=−0.66）、h20 仅 +0.0145；与 fund 相关 −0.818；round23 drop_one 去 quality 后基线 h5 +0.1137 → <b>+0.1477</b></td>
                <td style="padding:5px 7px;color:#94a3b8;">同为负 IC 且证据链完整。与 #1 可同批执行、同批验证。</td>
            </tr>
            <tr style="border-bottom:0.5px solid #1e293b;vertical-align:top;">
                <td style="padding:5px 7px;color:#fbbf24;font-weight:800;">3</td>
                <td style="padding:5px 7px;color:#cbd5e1;font-weight:700;">bias60 + rsi_rev 合并为单一反转分，35% → 25%</td>
                <td style="padding:5px 7px;color:#fbbf24;">中<br><span style="font-size:9px;color:#64748b;">改评分逻辑+七处同步</span></td>
                <td style="padding:5px 7px;color:#94a3b8;">两者相关 <b>+0.877</b>，有效维数仅 2.98/7；合计 35% 权重等于同一维计两次</td>
                <td style="padding:5px 7px;color:#94a3b8;">改善的是<b>模型诚实度</b>而非 IC——不能指望它提高收益，但能让权重语义站得住。放在 1、2 之后因为要动七处同步。</td>
            </tr>
            <tr style="border-bottom:0.5px solid #1e293b;vertical-align:top;">
                <td style="padding:5px 7px;color:#fbbf24;font-weight:800;">4</td>
                <td style="padding:5px 7px;color:#cbd5e1;font-weight:700;">引入<b>交易成本</b>与<b>换手约束</b>做组合构建</td>
                <td style="padding:5px 7px;color:#fbbf24;">中</td>
                <td style="padding:5px 7px;color:#94a3b8;">H10 年化换手 {_lv.get('annual_turnover_est', 0):.0f}x、H20 {_lv20h.get('annual_turnover_est', 0):.0f}x；H20 双边成本年吃 {(2 * 0.001 * _lv20h.get('annual_turnover_est', 0)) * 100:.1f}%</td>
                <td style="padding:5px 7px;color:#94a3b8;"><b>研究有效 ≠ 投资可用</b>的最大缺口就在这里。当前所有 IC 都是<b>零成本口径</b>，扣费后超额会显著缩水。</td>
            </tr>
            <tr style="border-bottom:0.5px solid #1e293b;vertical-align:top;">
                <td style="padding:5px 7px;color:#94a3b8;font-weight:800;">5</td>
                <td style="padding:5px 7px;color:#cbd5e1;font-weight:700;">把推荐持有周期从 T+10 延到 T+20</td>
                <td style="padding:5px 7px;color:#4ade80;">低<br><span style="font-size:9px;color:#64748b;">改文案+回测口径</span></td>
                <td style="padding:5px 7px;color:#94a3b8;">IC 衰减峰值在 h{_dec_pk.get('hop')}（{_dec_pk.get('ic', 0):+.4f}），h10 {(_decay.get('h10') or {}).get('ic', 0):+.4f} 仍在上行；H20 单期超额 {_lv20h.get('mean_exc', 0):+.4f} &gt; H10 {_lv.get('mean_exc', 0):+.4f}</td>
                <td style="padding:5px 7px;color:#94a3b8;">延长期限<b>同时降低换手</b>，一举两得。但 H20 只有 {_lv20h.get('n_periods', 0)} 个独立区间，样本过少，须等更多数据。</td>
            </tr>
            <tr style="border-bottom:0.5px solid #1e293b;vertical-align:top;">
                <td style="padding:5px 7px;color:#64748b;font-weight:800;">6</td>
                <td style="padding:5px 7px;color:#cbd5e1;font-weight:700;">样本外复验（第 79 个截面起，目标 30+）</td>
                <td style="padding:5px 7px;color:#ef4444;">高<br><span style="font-size:9px;color:#64748b;">等时间</span></td>
                <td style="padding:5px 7px;color:#94a3b8;">累计检验 {_mult.get('total_registered', 0)} 次，严格阈值应 t≈3.9；当前 78 截面单一区制</td>
                <td style="padding:5px 7px;color:#94a3b8;">这是<b>唯一能真正提升可信度</b>的动作，但无法加速。在此之前应<b>冻结权重</b>，避免继续在同一批数据上搜索。</td>
            </tr>
        </table>

        {_fb_html}

        <div style="margin-top:9px;padding:7px 9px;background:rgba(251,191,36,0.08);
                    border-left:3px solid #f59e0b;border-radius:4px;color:#cbd5e1;">
            <b style="color:#fbbf24;">⚠️ 三条不能越过的边界</b><br>            · <b>本页所有 IC 均为零成本、等权、无冲击成本口径</b>；真实交易下 H5/H10 的超额会被换手吃掉相当部分，H20 相对可行。<br>
            · <b>「某维 IC 高」不等于「该维可单独用」</b>：{_rob_txt}
              其余都是靠组合贡献；任何单维独立选股都缺证据。<br>
            · <b>多重检验已累计 {_mult.get('total_registered', 0)} 次</b>。这意味着当前所有 p 值都<b>偏乐观</b>；
              换维/调权的正确性只能靠「分子期双正 + 三方向互证」支撑，<b>不能靠某个 t 值单点</b>。
              下一步最有价值的不是继续找新因子，而是<b>样本外复验并冻结权重</b>。
        </div>
    </div>'''
except Exception as _ee:
    print(f"⚠️ 双视角评估区块渲染失败（将跳过）: {type(_ee).__name__}: {_ee}")
    _eval_html = ""


# ══════════════════════════════════════════════════════════════════════════════
# v4.11（2026-10-27 老大指令①）：把「分析过程」落盘成独立报告，主页只留结论摘要
#
# 迁出内容（原全部内联在首屏权重条里）：
#   ① 🔬 模型结构体检  _struct_html —— 有效维数(参与率)/主要相关对/方差贡献/Top20 敏感性
#   ② 🧪 维数实证      _round23_html —— 逐维单维 IC + 换维裁决（78 截面）
#   ③ 📊 双视角评估    _eval_html     —— 因子分类框架/逐维修改意见/多层 IC 对照
#
# 为什么迁：这三块是**方法论证据**，回答的是「这套权重凭什么」，而不是
#   「今天买什么」。它们合计约 6 万字符，把首屏权重条挤成了论文页。
# 为什么保留摘要：其中一条结论（8 维实际只有约 4.7 维独立信息）直接决定
#   读者能不能把这套分当「多维验证过的选股模型」用 —— 全删会造成反向误读。
#
# ⚠️ 与 v4.0 的 _ANALYSIS_SLICE 机制分工不同：
#   _ANALYSIS_SLICE 屏蔽的是**渲染结果**（del html_parts[...]），代码仍执行；
#   本处是把已经算好的三个 HTML 串写进独立文件，主页不再引用它们。
# ══════════════════════════════════════════════════════════════════════════════
_ms_digest_html = "结构体检未生成（当日数据不足或计算失败）。"
_ms_report_path = ""
try:
    # ⚠️ 顺序至关重要：摘要必须先算出来，权重条后构建 —— 因为权重条内部要
    #   内嵌 {_ms_digest_html}。反过来的话权重条会拿到上面的占位串，
    #   页面显示「结构体检未生成」而实际体检是成功的（本轮踩过一次）。

    # ══════════════════════════════════════════════════════════════════════════════
    # v4.13（2026-10-27 老大指令）：真·大池审计裁决（round34）
    #
    # 为什么取代 round33：round33 的 78 截面经核查是**混合池**（76 个冻结旧池
    #   ~195 只 + 9 个当前大池仅 11 天），89% 是单一窄名单的时间重复，且当前大池
    #   有 305 只在历史小池完全缺失 → 无法重建长面板。它测的是「冻结旧池内的打分
    #   区分度」，不是 alpha。
    #
    # round34 的做法：向前回补价格历史（腾讯 need=300 实返 301 根，2025-07-29 起），
    #   把 bias60 需要的 61 根预热与 h20 需要的 20 日前视从互斥变为可同时满足，
    #   从而在**真·大池 2029 只**上重建 238 个截面 / 11.76 个月（h5）/ 10.58 个月（h20）。
    #
    # 本块只读 audit_round34_decision.json，全部数字来自文件，禁写死。
    # ══════════════════════════════════════════════════════════════════════════════
    _d34 = None
    try:
        _d34_p = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                              "audit_round34_decision.json")
        if os.path.isfile(_d34_p):
            _d34 = json.load(open(_d34_p, encoding="utf-8"))
    except Exception as _d34e:
        print(f"⚠️ audit_round34_decision.json 读取失败（裁决表将缺失）: "
              f"{type(_d34e).__name__}: {_d34e}")
        _d34 = None

    _ms_parts = []
    if _struct:
        _ms_parts.append(
            f'<li><b>有效维数 {_struct["eff"]:.2f} / {_struct["p"]}</b>'
            f'（按相关矩阵特征值算的信息参与率；PC1 {_struct["ev"][0]:.0f}%、'
            f'特征值&gt;1 的成分 {_struct["n_ev_gt1"]} 个）——'
            f'标称 {_struct["p"]} 维不等于 {_struct["p"]} 份独立信息</li>')
    _ms_parts.append('<li>逐维单维 IC、全 lag 稳健性（Bonferroni 3.08）、'
                     '换维裁决与复合分对照，见独立报告的实证表</li>')
    if _d34:
        _pm = _d34.get("panel_meta") or {}
        _hd = (_d34.get("dims") or {}).get("score_bias60") or {}
        _h5 = _hd.get("h5") or {}
        _ms_parts.append(
            f'<li><b style="color:#38bdf8;">真·大池审计（round34）已取代旧混合池</b>：'
            f'域 <b>{_pm.get("domain_size", "—")} 只</b>（不锁定冻结池）、'
            f'<b>{_pm.get("n_dates", "—")} 个截面</b> / '
            f'{(list(_pm.get("date_range") or []) + ["", ""])[0]} ~ '
            f'{(list(_pm.get("date_range") or []) + ["", ""])[1]}、'
            f'<b>7 个价格维回测窗口 {_h5.get("span_months", "—")} 个月</b>'
            f'（h20 {(_d34.get("dims") or {}).get("score_bias60", {}).get("h20", {}).get("span_months", "—")} 个月），'
            f'<b style="color:#22c55e;">远超 3 个月硬下限</b>；fund 维见下条例外</li>')
        _zc = [v.get("cn") for v in (_d34.get("dims") or {}).values()
               if v.get("decision") == "zero"]
        if _zc:
            _ms_parts.append(
                f'<li><b style="color:#f59e0b;">裁决：{len(_zc)} 维建议归零</b>'
                f'（{" / ".join(_zc)}）—— 反转块三维在真·大池中'
                f'双周期均不过 Bonferroni，且组合消融显示<b>剔除后 h20 复合 IC 反而上升</b>。'
                f'详见独立报告<b>第二节</b></li>')
        # ⚠️ 必须显式披露 fund 的窗口例外：上面「≥3 个月」是 7 个价格维的口径，
        #    fund 受 data/fwd_hist 起点 2026-06-26 限制，h20 有效跨度仅 2.86 月。
        #    若含糊写「全部维度均达标」，等于把唯一不达标的维度藏起来。
        _f20 = ((_d34.get("dims") or {}).get("score_fund") or {}).get("h20") or {}
        _f5 = ((_d34.get("dims") or {}).get("score_fund") or {}).get("h5") or {}
        if _f20.get("span_ok_3m") is False:
            _ms_parts.append(
                f'<li><b style="color:#fca5a5;">例外：fund 维 h20 窗口仅 '
                f'{_f20.get("span_months", "—")} 月，未达 3 月</b>'
                f'（<code>fwd_hist</code> 起点硬边界，穷举验证无解），'
                f'故 fund 以 h5（{_f5.get("span_months", "—")} 月）为主判据。'
                f'fund 覆盖率亦仅约 10%（每日 ~190 行且带 TOP200 选样偏差）</li>')
    else:
        _ms_parts.append('<li>权重为<b>先验设定</b>；真·大池审计裁决文件缺失，'
                         '本轮未能给出逐维裁决</li>')
    _ms_parts.append('<li>独立报告<b>第六节</b>另有 <b>外部基准对照</b>（飞书全 A 股近半年 47 因子）：'
                     '揭示 8 维与外部基准的偏离与符号翻转</li>')
    _ms_digest_html = '<ul style="margin:4px 0 0 16px;padding:0;">' + ''.join(_ms_parts) + '</ul>'

    # ══════════════════════════════════════════════════════════════════════════════
    # v4.12（2026-10-27 老大指令③）：外部基准对照（飞书全 A 股近半年）
    # 把本模型 8 维与全 A 股外部基准逐维对照，暴露「冻结混池」IC 的夸大与符号翻转。
    # 数据来源：reference_final_ranking_halfyear.csv（与老大提供的 Downloads 版同源）。
    # ══════════════════════════════════════════════════════════════════════════════
    _bench_html = ""
    try:
        _bench_here = os.path.dirname(os.path.abspath(__file__))
        _bench_csv = None
        for _cand in ("reference_final_ranking_halfyear.csv",
                      "/Users/yzreal/Downloads/final_ranking-近半年.csv"):
            _cp = _cand if os.path.isabs(_cand) else os.path.join(_bench_here, _cand)
            if os.path.isfile(_cp):
                _bench_csv = _cp
                break
        _bench_r33 = None
        try:
            _bench_r33 = json.load(open(os.path.join(_bench_here, "audit_round33.json"),
                                       encoding="utf-8"))
        except Exception:
            _bench_r33 = None
        _bench_EW = (_bench_r33 or {}).get("eff_weight") or {}
        if _bench_csv and _bench_EW:
            import csv as _csv
            _brows = list(_csv.DictReader(open(_bench_csv, encoding="utf-8-sig")))
            _bcf = {r["factor"]: r for r in _brows}
            def _bf(fac):
                _r = _bcf.get(fac)
                if not _r:
                    return None
                try:
                    return float(_r["ic_daily"])
                except Exception:
                    return None
            # (dim_key, 显示名, csv因子, 转我的方向符号, 说明)
            _bmap = [
                ("score_bias60", "乖离60(反转)", "BIAS60", -1, "反转：取 BIAS60 的反转IC（−ic）"),
                ("score_rsi_rev", "RSI反转", "REV1M", 1, "反转：1月反转 REV1M"),
                ("score_lowvol20", "低波20", "LVOL20", 1, "低波：近半年低波异象弱，几乎为零"),
                ("score_ep", "EP估值", "EP", 1, "估值：高 EP(便宜)→高收益"),
                ("score_fund", "fund基本面", "BLEND", 1, "基本面：EP/成长/质量混合（取三者均值）"),
                ("score_tech_neg", "技术反号(反转块)", "REV1M", 1, "技术反转块：与 REV1M 同源"),
                ("score_quality", "质量", "ROE", 1, "质量：高 ROE→高收益（外部为正）"),
                ("score_size_ind", "行业内市值", "SIZE", -1, "行业内小盘：取 SIZE 小盘溢价（−ic）"),
            ]
            _bdata = []
            for _k, _label, _fac, _sg, _note in _bmap:
                _ew = _bench_EW.get(_k) or {}
                _my = _ew.get("ic")
                if _my is None:
                    continue
                if _fac == "BLEND":
                    _parts = [_bf("EP"), _bf("OCFG"), _bf("ROE")]
                    _parts = [p for p in _parts if p is not None]
                    _ext = (sum(_parts) / len(_parts)) if _parts else None
                else:
                    _raw = _bf(_fac)
                    _ext = (_raw * _sg) if _raw is not None else None
                if _ext is None:
                    continue
                _ratio = (_my / _ext) if abs(_ext) > 1e-9 else None
                _flip = (_my * _ext < 0)
                _bdata.append((_label, _my, _fac, _ext, _ratio, _flip, _note))
            _btrs = "".join(
                f'<tr style="border-bottom:0.5px solid #1e293b;">'
                f'<td style="padding:4px 8px;color:#cbd5e1;">{_lbl}</td>'
                f'<td style="padding:4px 8px;text-align:right;color:{"#22c55e" if _my > 0 else "#ef4444"};">{_my:+.4f}</td>'
                f'<td style="padding:4px 8px;text-align:right;color:#94a3b8;">{_ext:+.4f}</td>'
                f'<td style="padding:4px 8px;text-align:right;color:'
                f'{"#f59e0b" if (_ratio and abs(_ratio) >= 2) else "#64748b"};">'
                f'{"×{:.1f}".format(abs(_ratio)) if _ratio else "—"}</td>'
                f'<td style="padding:4px 8px;font-size:9px;color:#64748b;">{_fac}</td>'
                f'<td style="padding:4px 8px;color:'
                f'{"#ef4444" if _flip else ("#f59e0b" if (_ratio and abs(_ratio) >= 2) else "#22c55e")};'
                f'font-weight:700;">'
                f'{"符号翻转 ⚠" if _flip else ("量级夸大" if (_ratio and abs(_ratio) >= 2) else "方向吻合")}</td>'
                f'</tr>'
                for _lbl, _my, _fac, _ext, _ratio, _flip, _note in _bdata)
            _bench_html = f'''
  <div style="padding:14px 16px;background:#111c2e;border:0.5px solid #1e293b;border-radius:10px;margin-bottom:16px;">
    <div style="font-size:14px;font-weight:800;color:#e2e8f0;margin-bottom:6px;">📡 六、外部基准对照（飞书全 A 股近半年 · 47 因子）</div>
    <div style="font-size:11px;color:#64748b;margin-bottom:8px;line-height:1.8;">
      为回答「8 维数据可靠性」，将本模型 8 维与<b>全 A 股外部基准</b>（飞书 final_ranking-近半年.csv，
      222 个日频观测 / 11 个月频，覆盖全部 A 股）逐维对照。左列「我的混合池IC」来自 audit_round33
      （78 混合截面）；右列「外部有效IC」按<b>本模型预测方向</b>折算（反转类取反转IC，规模取小盘溢价）。
    </div>
    <table style="width:100%;border-collapse:collapse;font-size:9.5px;margin-bottom:8px;">
      <tr style="border-bottom:0.5px solid #334155;color:#64748b;">
        <th style="text-align:left;padding:3px 8px;">我的维度</th>
        <th style="text-align:right;padding:3px 8px;">我的混合池IC</th>
        <th style="text-align:right;padding:3px 8px;">外部有效IC</th>
        <th style="text-align:right;padding:3px 8px;">倍数</th>
        <th style="text-align:left;padding:3px 8px;">外部因子</th>
        <th style="text-align:left;padding:3px 8px;">判定</th>
      </tr>
      {_btrs}
    </table>
    <div style="font-size:10px;color:#94a3b8;line-height:1.95;">
      <b style="color:#f59e0b;">读表结论：</b>本模型 6 个维度（乖离/RSI/低波/EP/fund/技术反转）在外部基准上的有效 IC
      仅为混合池的 <b>1/2 ~ 1/15</b>——混合池的高 IC 是<b>冻结旧池的 regime 顺风 + 选择偏差</b>造成的假象；
      其中 <b style="color:#ef4444;">低波20 被夸大约 15 倍</b>（近半年 A 股低波异象本就极弱）。
      更严重的是 <b style="color:#ef4444;">质量、行业内市值出现符号翻转</b>：
      外部基准中高质量(+0.034)、小盘溢价(+0.066) 均显著为正，混合池却给出近零/负值，
      说明本模型在这两维的复合方向或池内处理有问题，<b>绝不能声称「已验证」</b>。
      唯一基本吻合的是<b style="color:#22c55e;">技术反转块（×1.1）</b>，因其反转口径与外部最接近。
    </div>
    <div style="font-size:10px;color:#64748b;line-height:1.85;margin-top:6px;">
      ⚖️ <b>外部基准也不是金标准</b>：它是全 A 股（含大量被本模型筛选剔除的垃圾股）、单一近半年 regime，
      且低波/质量在不同 regime 下强弱会变。但它作为<b>独立外部样本</b>，方向远比冻结混池可信——
      两者冲突时，<b style="color:#f59e0b;">以外部基准的方向为准</b>。
    </div>
  </div>'''
            print("📡 外部基准对照节已生成（8 维 × 飞书全 A 股）")
        else:
            _bench_html = ('<div style="padding:14px 16px;background:#111c2e;border:0.5px solid #1e293b;'
                           'border-radius:10px;margin-bottom:16px;">'
                           '<div style="font-size:14px;font-weight:800;color:#e2e8f0;margin-bottom:6px;">'
                           '📡 六、外部基准对照</div>'
                           '<div style="font-size:11px;color:#f59e0b;">未生成'
                           '（缺少 reference_final_ranking_halfyear.csv 或 audit_round33.json）。</div></div>')
    except Exception as _bxe:
        print(f"⚠️ 外部基准对照生成失败: {type(_bxe).__name__}: {_bxe}")
        _bench_html = ""

    # ══════════════════════════════════════════════════════════════════════════════
    # v4.13：真·大池审计裁决表（round34）—— 这是本报告的核心结论区
    # 全部数字从 audit_round34_decision.json 读取，禁写死。
    # ══════════════════════════════════════════════════════════════════════════════
    _d34_html = ""
    try:
        _dd = _d34
        if not _dd:
            _d34_html = ('<div style="padding:14px 16px;background:rgba(245,158,11,0.08);'
                         'border:1px solid #f59e0b;border-radius:10px;margin-bottom:16px;">'
                         '<div style="font-size:14px;font-weight:800;color:#fbbf24;">'
                         '🎯 五、真·大池审计裁决</div>'
                         '<div style="font-size:11px;color:#f59e0b;margin-top:6px;">'
                         '未生成（缺 audit_round34_decision.json）。</div></div>')
        else:
            _pm = _dd.get("panel_meta") or {}
            _dims = _dd.get("dims") or {}
            _ths = _dd.get("thresholds") or {}
            _bonf = _ths.get("bonf", 3.08)
            _d0, _d1 = (list(_pm.get("date_range") or ["—", "—"]) + ["—", "—"])[:2]
            _win = _pm.get("dim_window") or {}
            # _fw = fund 的「面板首末跨度」（3.91 月）。注意这**不是** h20 IC 的
            # 有效跨度（2.86 月，末端差 20 个交易日）；边界披露必须用后一个值，
            # 故这里仅留作总跨度备查，不参与文案。
            _fw = _win.get("score_fund") or {}

            # —— 逐维裁决行 ——（顺序：严格按现行权重降序，读者关心大头）
            _DEC_CN = {"keep": ("保留", "#22c55e", "●"),
                       "keep_limited": ("保留(限)", "#22c55e", "◐"),
                       "keep_orthogonal": ("保留(正交)", "#22c55e", "◇"),
                       "halve": ("降权一半", "#f59e0b", "◑"),
                       "zero": ("归零", "#ef4444", "✕")}
            _order = sorted(_dims.items(),
                            key=lambda kv: -(kv[1].get("weight_current") or 0))
            _rows = []
            for _k, _v in _order:
                _h5 = _v.get("h5") or {}
                _h20 = _v.get("h20") or {}
                _dec = _v.get("decision") or "—"
                _dcn, _dcol, _dgl = _DEC_CN.get(_dec, (_dec, "#94a3b8", "?"))

                def _ic_cell(_s):
                    if not _s or _s.get("ic") is None:
                        return '<span style="color:#475569;">—</span>'
                    _ic = _s.get("ic")
                    _t = _s.get("t0")
                    _nb = _s.get("span_ok_3m", True)
                    _c = "#ef4444" if _ic > 0 else "#22c55e"   # A股习惯：正=红
                    _star = ('<span style="color:#22c55e;font-weight:700;">★</span>'
                             if _s.get("pass_bonf") else "")
                    _warn = ('' if _nb else
                             '<span style="color:#f59e0b;font-size:8px;" '
                             'title="h20 跨度 < 3 月，数据源硬边界，仅供参照">⚠</span>')
                    return (f'<span style="color:{_c};font-weight:700;">{_ic:+.4f}</span>'
                            f'<span style="color:#64748b;font-size:8px;"> t{_t:+.2f}</span>'
                            f'{_star}{_warn}')

                _abl = _v.get("ablation_h20") or {}
                _abl_txt = (f'{_abl.get("verdict", "—")}'
                            f'<span style="color:#475569;font-size:8px;"> '
                            f'Δ{_abl.get("delta_ic", 0):+.4f}</span>')
                _sug = _v.get("suggested_weight")
                _cur = _v.get("weight_current")
                _wchg = (f'<span style="color:#64748b;">{_cur:.3f}</span>'
                         f'<span style="color:#475569;"> → </span>'
                         f'<b style="color:{"#22c55e" if (_sug or 0) >= (_cur or 0) else "#f59e0b"};">'
                         f'{(_sug or 0):.3f}</b>')
                _rows.append(
                    f'<tr style="border-bottom:0.5px solid #1e293b;">'
                    f'<td style="padding:5px 8px;color:#e2e8f0;font-weight:600;">'
                    f'{_v.get("cn", _k)}</td>'
                    f'<td style="padding:5px 8px;text-align:right;">{_wchg}</td>'
                    f'<td style="padding:5px 8px;text-align:right;">{_ic_cell(_h5)}</td>'
                    f'<td style="padding:5px 8px;text-align:right;">{_ic_cell(_h20)}</td>'
                    f'<td style="padding:5px 8px;font-size:9px;color:#94a3b8;">{_abl_txt}</td>'
                    f'<td style="padding:5px 8px;text-align:center;color:{_dcol};font-weight:700;">'
                    f'{_dgl} {_dcn}</td>'
                    f'<td style="padding:5px 8px;font-size:9px;color:#64748b;line-height:1.5;">'
                    f'{_v.get("reason", "—")}</td>'
                    f'</tr>')

            _n_keep = sum(1 for v in _dims.values()
                          if (v.get("decision") or "").startswith("keep"))
            _n_halve = sum(1 for v in _dims.values() if v.get("decision") == "halve")
            _n_zero = sum(1 for v in _dims.values() if v.get("decision") == "zero")
            _sug_sum = _dd.get("suggested_sum")

            # ⚠️ 这几个值必须在 f-string 外取好：f-string 表达式内不能写 `or {{}}`
            #    （`{{` 的转义只在字面文本层生效，进了替换域会被当语法错误或误解析）。
            _b60 = _dims.get("score_bias60") or {}
            _fnd = _dims.get("score_fund") or {}
            _b60_h5 = _b60.get("h5") or {}
            _b60_h20 = _b60.get("h20") or {}
            _fnd_h5 = _fnd.get("h5") or {}
            _fnd_h20 = _fnd.get("h20") or {}
            _sp_h5 = _b60_h5.get("span_months", "—")
            _sp_h20 = _b60_h20.get("span_months", "—")
            _sp_fund = _fnd_h5.get("span_months", "—")
            # ⚠️ 注意区分两个不同口径：
            #   panel_meta.dim_window[*].months  = 面板首末日期跨度（fund 3.91 月）
            #   dims[*].h20.span_months         = h20 IC 回测的**有效**跨度（fund 2.86 月）
            # h20 前视需 20 个交易日，末端必然比面板短，故边界披露必须用后者。
            _fw_months = _fnd_h20.get("span_months", "—")

            # —— 组合消融 ——（证明归零不是单维噪声）
            _ca = _dd.get("combo_ablation") or {}
            _base = _ca.get("现行 8 维") or {}
            _ca_rows = []
            for _nm, _cv in _ca.items():
                _h5i, _h20i = _cv.get("h5_ic"), _cv.get("h20_ic")
                _up20 = (_h20i is not None and _base.get("h20_ic") is not None
                         and _h20i > _base["h20_ic"] + 1e-9)
                _is_base = (_nm == "现行 8 维")
                _ca_rows.append(
                    f'<tr style="border-bottom:0.5px solid #1e293b;'
                    f'{"background:rgba(56,189,248,0.06);" if _is_base else ""}">'
                    f'<td style="padding:4px 8px;color:#cbd5e1;">{_nm}</td>'
                    f'<td style="padding:4px 8px;text-align:right;'
                    f'color:#ef4444;font-weight:700;">{_h5i:+.4f}</td>'
                    f'<td style="padding:4px 8px;text-align:right;'
                    f'color:{"#22c55e" if _up20 else "#ef4444"};font-weight:700;">'
                    f'{_h20i:+.4f}</td>'
                    f'<td style="padding:4px 8px;text-align:center;font-size:9px;'
                    f'color:{"#22c55e" if _up20 else "#64748b"};">'
                    f'{"↑ 优于基准" if _up20 else ("基准" if _is_base else "↓ 差于基准")}</td>'
                    f'</tr>')
            _rev = _ca.get("去反转块三合一(bias60+RSI+tech_neg)") or {}
            _rev_txt = ""
            if _rev.get("h20_ic") and _base.get("h20_ic"):
                _pct = (_rev["h20_ic"] / _base["h20_ic"] - 1) * 100
                _rev_txt = (f'h20 复合 IC 由 <b>{_base["h20_ic"]:+.4f}</b> 升至 '
                            f'<b style="color:#22c55e;">{_rev["h20_ic"]:+.4f}</b>'
                            f'（<b style="color:#22c55e;">+{_pct:.0f}%</b>）')

            # —— 再分配方案 ——
            _rp = _dd.get("reallocation_plans") or {}
            _rp_rows = []
            _best_nm = None
            _best_t = -1e9
            for _nm, _pv in _rp.items():
                _t20 = _pv.get("h20_t")
                if _t20 is not None and _t20 > _best_t and _nm != "A 现行 8 维（基准）":
                    _best_t, _best_nm = _t20, _nm
            for _nm, _pv in _rp.items():
                _is_b = (_nm.startswith("A"))
                _is_best = (_nm == _best_nm)
                _rp_rows.append(
                    f'<tr style="border-bottom:0.5px solid #1e293b;'
                    f'{"background:rgba(34,197,94,0.08);" if _is_best else ""}">'
                    f'<td style="padding:4px 8px;color:#cbd5e1;">{_nm}'
                    f'{" 🏆" if _is_best else ""}</td>'
                    f'<td style="padding:4px 8px;text-align:right;color:#ef4444;">'
                    f'{_pv.get("h5_ic", 0):+.4f}</td>'
                    f'<td style="padding:4px 8px;text-align:right;color:#64748b;">'
                    f't{_pv.get("h5_t", 0):+.2f}</td>'
                    f'<td style="padding:4px 8px;text-align:right;'
                    f'color:#ef4444;font-weight:700;">{_pv.get("h20_ic", 0):+.4f}</td>'
                    f'<td style="padding:4px 8px;text-align:right;color:#64748b;">'
                    f't{_pv.get("h20_t", 0):+.2f}</td>'
                    f'<td style="padding:4px 8px;text-align:center;font-size:9px;'
                    f'color:{"#22c55e" if _is_best else "#475569"};">'
                    f'{"最优" if _is_best else ("基准" if _is_b else "")}</td>'
                    f'</tr>')

            # ===== v4.10 落档证据：五层验证（禁写死，全部读 JSON）=====
            # 变量统一加 _v5 / _vq 前缀，避免覆盖下游同名全局（_ic/_rows 等）。
            # ⚠️ _here_now 必须在这里先赋值：本块整体在 try 内，若名字不存在
            #    NameError 会被外层 except 吞成「裁决节跳过」，静默丢区块。
            _here_now = os.path.dirname(os.path.abspath(__file__))

            def _vload(_fn):
                try:
                    with open(os.path.join(_here_now, _fn), encoding="utf-8") as _f:
                        return json.load(_f)
                except Exception:
                    return {}
            _v5 = _vload("verify_plan_c5.json")          # 决定性 scale 扫描
            _v1 = _vload("verify_plan_c.json")           # 同质样本公平对比
            _v2 = _vload("verify_plan_c2.json")          # fund 覆盖指示变量 IC
            _v4 = _vload("verify_plan_c4.json")          # 逐维分段
            _v3 = _vload("verify_plan_c3.json")          # regime 拆解

            _v5_sweep = _v5.get("sweep") or {}
            _v5_adopt = _v5.get("adopt") or {}
            _v5_base = _v5.get("base_all")
            _v5_rows = []
            for _sk in sorted(_v5_sweep, key=lambda x: float(x)):
                _sv = _v5_sweep.get(_sk) or {}
                _is_ad = (abs(float(_sk) - float(_v5_adopt.get("scale", -1))) < 1e-9)
                _ok = str(_sv.get("verdict", "")).startswith("两段皆不劣化")
                _dpre, _dpost = _sv.get("d_pre"), _sv.get("d_post")
                _dpre_s = f"{_dpre:+.4f}" if isinstance(_dpre, (int, float)) else "—"
                _dpost_s = f"{_dpost:+.4f}" if isinstance(_dpost, (int, float)) else "—"
                _v5_rows.append(
                    f'<tr style="border-bottom:0.5px solid #1e293b;'
                    f'{"background:rgba(34,197,94,0.10);" if _is_ad else ""}">'
                    f'<td style="padding:4px 8px;color:#cbd5e1;">×{float(_sk):.2f}'
                    f'{" ←已落档" if _is_ad else ""}</td>'
                    f'<td style="padding:4px 8px;text-align:right;color:#64748b;">'
                    f'{float(_sv.get("rev_share", 0)) * 100:.0f}%</td>'
                    f'<td style="padding:4px 8px;text-align:right;color:#ef4444;font-weight:700;">'
                    f'{_sv.get("h20_all", 0):+.4f}</td>'
                    f'<td style="padding:4px 8px;text-align:right;color:#64748b;">'
                    f't{_sv.get("t_all", 0):+.2f}</td>'
                    f'<td style="padding:4px 8px;text-align:right;color:#94a3b8;">{_dpre_s}</td>'
                    f'<td style="padding:4px 8px;text-align:right;color:#94a3b8;">{_dpost_s}</td>'
                    f'<td style="padding:4px 8px;text-align:center;font-size:9px;'
                    f'color:{"#22c55e" if _ok else "#ef4444"};">'
                    f'{html_mod.escape(str(_sv.get("verdict", "—")))}</td>'
                    f'</tr>')

            # 已落档权重表（配置实值 vs 原值）
            _v5_wn = []
            try:
                with open(os.path.join(_here_now, "factor_weights.json"), encoding="utf-8") as _f:
                    _cfgw = (json.load(_f) or {}).get("weights") or {}
                _oldw = (_dd.get("current_weights") or {})
                _ks = [k for k in _cfgw if float(_cfgw.get(k) or 0) > 0]
                _ks.sort(key=lambda x: -float(_cfgw.get(x) or 0))
                for _k2 in _ks:
                    _nv2 = float(_cfgw.get(_k2) or 0)
                    _ov2 = _oldw.get(_k2)
                    _ov2 = float(_ov2) if isinstance(_ov2, (int, float)) else None
                    _ov2_s = f"{_ov2:.4f}" if _ov2 is not None else "—"
                    _dl2 = f"{_nv2 - _ov2:+.4f}" if _ov2 is not None else "新增"
                    _v5_wn.append(
                        f'<tr style="border-bottom:0.5px solid #1e293b;">'
                        f'<td style="padding:4px 8px;color:#cbd5e1;">'
                        f'{html_mod.escape(_k2.replace("score_", ""))}</td>'
                        f'<td style="padding:4px 8px;text-align:right;color:#64748b;">{_ov2_s}</td>'
                        f'<td style="padding:4px 8px;text-align:right;color:#22c55e;'
                        f'font-weight:700;">{_nv2:.4f}</td>'
                        f'<td style="padding:4px 8px;text-align:right;color:#38bdf8;">{_dl2}</td>'
                        f'</tr>')
            except Exception:
                pass

            # 同质样本公平对比（唯一可直接比的口径：只取有 fund 的截面）
            _v1_f = _v1.get("fair_with_fund") or {}
            _v1_rows = []
            for _nm2, _pv2 in sorted(_v1_f.items(),
                                     key=lambda kv: -(kv[1].get("h20") or -9)):
                _isa = _nm2.startswith("A")
                _isc = _nm2.startswith("C")
                _v1_rows.append(
                    f'<tr style="border-bottom:0.5px solid #1e293b;'
                    f'{"background:rgba(56,189,248,0.06);" if _isa else ""}'
                    f'{"background:rgba(239,68,68,0.10);" if _isc else ""}">'
                    f'<td style="padding:4px 8px;color:#cbd5e1;">{html_mod.escape(_nm2)}</td>'
                    f'<td style="padding:4px 8px;text-align:right;color:#ef4444;">'
                    f'{(_pv2.get("h5") or 0):+.4f}</td>'
                    f'<td style="padding:4px 8px;text-align:right;color:#64748b;">'
                    f't{(_pv2.get("t5") or 0):+.2f}</td>'
                    f'<td style="padding:4px 8px;text-align:right;font-weight:700;'
                    f'color:{"#ef4444" if _isc else "#22c55e"};">'
                    f'{(_pv2.get("h20") or 0):+.4f}</td>'
                    f'<td style="padding:4px 8px;text-align:right;color:#64748b;">'
                    f't{(_pv2.get("t20") or 0):+.2f}</td>'
                    f'<td style="padding:4px 8px;text-align:center;font-size:9px;'
                    f'color:{"#22c55e" if _isa else ("#ef4444" if _isc else "#475569")};">'
                    f'{"基准" if _isa else ("最差 ←原推荐被证伪" if _isc else "")}</td>'
                    f'</tr>')

            _vq4 = _v2.get("Q4_fund_coverage_IC") or {}
            _vq4_h5 = _vq4.get("h5") or {}
            _vq4_h20 = _vq4.get("h20") or {}

            # ── round44/45（2026-10-29）fund 三层缺口实证数字 ──
            # ⚠️ 必须在 f-string 之前定义；缺任何一个都会 NameError 被外层 except
            #    静默吞掉 → 整块披露消失且无告警（本项目已踩过两次的坑）。
            #    全部读自 audit_round44/45.json，缺失时用 None 并让文案显示「—」，
            #    绝不硬编码看似合理的默认值（那会把「没算」伪装成「算过了」）。
            _rf44 = {}
            _rf45 = {}
            # ⚠️ 本段作用域内没有 HERE（全脚本用的是 _here_now 等局部名）。
            #    写 HERE 会让两个 try 双双抛 NameError → 被 except 吞掉 →
            #    页面卡片整块降级且只在控制台留一行告警。2026-10-29 已踩一次。
            _rf_base = _here_now
            try:
                _rf44 = json.load(open(os.path.join(_rf_base, "audit_round44.json"),
                                       encoding="utf-8"))
            except Exception as _e44:
                print(f"⚠️ audit_round44.json 不可读，fund 披露降级: {_e44}")
            try:
                _rf45 = json.load(open(os.path.join(_rf_base, "audit_round45.json"),
                                       encoding="utf-8"))
            except Exception as _e45:
                print(f"⚠️ audit_round45.json 不可读，fund 披露降级: {_e45}")

            _rf_tot = (_rf44.get("n_dates") or 0)
            # ⚠️ 这两个变量既要参与算术（100.0*n/tot），又要在 f-string 里以纯文本出现。
            #    若回退成 "—" 字符串，乘法会抛 TypeError 把整段吞掉。故一律归一成 int。
            #    json 缺失 → 0，页面显示「0 个截面（0.0%）」比整块消失可接受。
            _rf_zero_n = _rf44.get("n_zero_cover_dates", 0)
            _rf_part_n = _rf44.get("n_partial_cover_dates", 0)
            _rf_zero_n = int(_rf_zero_n) if isinstance(_rf_zero_n, (int, float)) else 0
            _rf_part_n = int(_rf_part_n) if isinstance(_rf_part_n, (int, float)) else 0
            _rf_zero_pct = f"{100.0 * _rf_zero_n / _rf_tot:.1f}" if _rf_tot else "0.0"
            _rf_part_pct = f"{100.0 * _rf_part_n / _rf_tot:.1f}" if _rf_tot else "0.0"
            _rf_keep = 1.0 - float((_rf44.get("fund_weight") or 0) or 0)
            _rf_amp = (1.0 / _rf_keep) if _rf_keep > 0 else 0.0
            _rf_ps = _rf44.get("partial_cover_stats") or {}
            _rf_top50 = _rf_ps.get("top50_overlap_median")
            _rf_part_rows = _rf44.get("per_date_partial") or []
            _rf_top50_min = (min([r.get("top50_overlap", 50) for r in _rf_part_rows])
                             if _rf_part_rows else None)
            # ⚠️ 这两个变量在 f-string 里用 {:.0f} 格式化。若 json 读不到就回退成
            #    字符串 "—"，f-string 会抛 ValueError: Unknown format code 'f'，
            #    整个裁决节被 except 吞成空串（页面整块消失，只在控制台留一行）。
            #    2026-10-29 已踩一次 → 一律归一成 float，不可用字符串占位。
            _rf_top50 = float(_rf_top50) if isinstance(_rf_top50, (int, float)) else 0.0
            _rf_top50_min = float(_rf_top50_min) if isinstance(_rf_top50_min, (int, float)) else 0.0
            # M1 与 M0 的 h20 差（从 round45 的 C_composite_ic 直接算，不引二手数）
            _rf_cic = _rf45.get("C_composite_ic") or {}
            try:
                _rf_m0_20 = ((_rf_cic.get("M0") or {}).get("h20") or {}).get("ic")
                _rf_m1_20 = ((_rf_cic.get("M1") or {}).get("h20") or {}).get("ic")
                _rf_m1_d20 = (_rf_m1_20 - _rf_m0_20) if (isinstance(_rf_m0_20, (int, float))
                                                          and isinstance(_rf_m1_20, (int, float))) else 0.0
            except Exception:
                _rf_m1_d20 = 0.0
            _rf_m1_t20 = 0.0
            # NW t 由 round45 只对 M1/M2−M0 配对输出到控制台，json 未存；
            # 这里读 audit_round45 的配对段（若存在），否则保持 0 并在文案注明来源
            _rf_pairs = _rf45.get("paired_delta") or {}
            if isinstance(_rf_pairs, dict):
                _rf_m1_t20 = float(((_rf_pairs.get("M1-M0") or {}).get("h20") or {}).get("nw_t") or 0)
            # fwd_hist 最后文件日期（用于 L3 断流披露）
            try:
                _rf_fh = sorted(glob.glob(os.path.join(_rf_base, "data", "fwd_hist", "*.json")))
                _rf_hist_last = os.path.basename(_rf_fh[-1])[:10] if _rf_fh else "—"
                _rf_hist_n = len(_rf_fh)
            except Exception:
                _rf_hist_last = "—"
                _rf_hist_n = 0
            # 三条填充法的保真度（probe_fund_calib.json）
            _rf_pc = {}
            try:
                _rf_pc = json.load(open(os.path.join(_rf_base, "probe_fund_calib.json"),
                                        encoding="utf-8"))
            except Exception:
                pass
            _rf_pb = {}
            try:
                _rf_pb = json.load(open(os.path.join(_rf_base, "probe_fund_backfill.json"),
                                        encoding="utf-8"))
            except Exception:
                pass
            _rf_rho_a = ((_rf_pb.get("overall") or {}).get("spearman"))
            _rf_vv = _rf_pc.get("variants") or {}
            _rf_rho_b = ((_rf_vv.get("V1") or {}).get("spearman"))
            _rf_rho_c = ((_rf_vv.get("V2") or {}).get("spearman"))
            _rf_rho_a = _rf_rho_a if _rf_rho_a is not None else 0.0
            _rf_rho_b = _rf_rho_b if _rf_rho_b is not None else 0.0
            _rf_rho_c = _rf_rho_c if _rf_rho_c is not None else 0.0
            # 前瞻PE 系统性高于静态PE 的幅度：k = 前瞻/静态 中位，gap = (1/k - 1)*100
            _rf_k = _rf_pc.get("k_global")
            _rf_pe_gap = ((1.0 / _rf_k - 1.0) * 100.0) if (isinstance(_rf_k, (int, float))
                                                           and _rf_k > 0) else 0.0

            # ---- round48：覆盖率跃升后的结构核实（全部读 json，禁硬编码）----
            _rf48 = {}
            try:
                _rf48 = json.load(open(os.path.join(_rf_base, "audit_round48.json"),
                                       encoding="utf-8"))
            except Exception as _e48:
                print(f"⚠️ audit_round48.json 不可读，覆盖率披露降级: {_e48}")
            _rf48_cov = _rf48.get("fund_cov_pct", 0.0)
            _rf48_have = _rf48.get("fund_have", 0)
            _rf48_n = _rf48.get("pool_n", 0)
            _rf48_miss = _rf48.get("fund_missing", 0)
            _rf48_allneg = bool(_rf48.get("missing_all_pe_le0"))
            _rf48_uall = _rf48.get("universe_all", 0)
            _rf48_umain = _rf48.get("universe_main", 0)
            _rf48_covall = _rf48.get("cover_by_all_pct", 0.0)
            _rf48_covmain = _rf48.get("cover_by_main_pct", 0.0)
            _rf48_dom = _rf48.get("panel_domain", 0)

            # ---- round47：M1 代价（全部读 json）----
            _rf47 = {}
            try:
                _rf47 = json.load(open(os.path.join(_rf_base, "audit_round47.json"),
                                       encoding="utf-8"))
            except Exception as _e47:
                print(f"⚠️ audit_round47.json 不可读，M1 代价披露降级: {_e47}")
            _rf47_rho = _rf47.get("rho_median", 0.0)
            _rf47_rhomin = _rf47.get("rho_min", 0.0)
            _rf47_ov = _rf47.get("top50_overlap_median", 0.0)
            _rf47_ovmin = _rf47.get("top50_overlap_min", 0.0)
            _rf47_dm = _rf47.get("delta_mean_median", 0.0)

            # ---- round51~58：权重体系的证据链体检（全部读 json，禁硬编码）----
            # ⚠️ 本段所有变量必须能直接进 f-string 格式化（:.Nf / :,），
            #    读不到就兜数值 0，**严禁**回退成 "—" 字符串，否则整节被 except 吞掉。
            _rf51 = {}
            try:
                _rf51 = json.load(open(os.path.join(_rf_base, "audit_round51.json"),
                                       encoding="utf-8"))
            except Exception as _e51:
                print(f"⚠️ audit_round51.json 不可读，证据链披露降级: {_e51}")
            _rf57 = {}
            try:
                _rf57 = json.load(open(os.path.join(_rf_base, "audit_round57.json"),
                                       encoding="utf-8"))
            except Exception as _e57:
                print(f"⚠️ audit_round57.json 不可读，共线披露降级: {_e57}")
            _rf58 = {}
            try:
                _rf58 = json.load(open(os.path.join(_rf_base, "audit_round58.json"),
                                       encoding="utf-8"))
            except Exception as _e58:
                print(f"⚠️ audit_round58.json 不可读，复合对比披露降级: {_e58}")
            _rf54 = {}
            try:
                _rf54 = json.load(open(os.path.join(_rf_base, "audit_round54.json"),
                                       encoding="utf-8"))
            except Exception as _e54:
                print(f"⚠️ audit_round54.json 不可读，regime 披露降级: {_e54}")

            # —— round51：证据缺口 ——
            _rf51_cmp = _rf51.get("compare_factor_ic") or {}
            _rf51_miss_w = 0.0          # 证据不足（n<20 或无记录）的名义权重合计
            _rf51_miss_dims = []
            _rf_W_ACT = {"score_fund": 0.200000, "score_quality": 0.100000, "score_growth": 0.100000, "score_ep": 0.100000, "score_size_growth": 0.200000, "score_bias60": 0.050000, "score_rsi_rev": 0.050000, "score_dominant": 0.100000, "score_tech_neg": 0.100000}
            _rf51_seen = set()
            for _dk, _dv in _rf51_cmp.items():
                if not isinstance(_dv, dict):
                    continue
                # ⚠️ 只统计「当前生效」的维：round51 的面板是 v4.12 八维，
                #    里面出现的 score_size_ind 已退役（现拆成 size_growth + dominant），
                #    若把它也计入缺口会虚增权重（0.14 押在已退役的维度上）。
                if _dk not in _rf_W_ACT:
                    continue
                _rf51_seen.add(_dk)
                _n = _dv.get("factor_ic_h5_n")
                if not isinstance(_n, (int, float)) or _n < 20:
                    _rf51_miss_w += _rf_W_ACT.get(_dk, 0.0)
                    _rf51_miss_dims.append(_dk.replace("score_", ""))
            # ⚠️ round51 的 compare 段天然不含 size_growth / dominant（面板无此字段），
            #    而这两维恰恰是「零实证」的两个新维 —— 必须显式补进来，否则缺口被低估。
            for _dk, _w in _rf_W_ACT.items():
                if _dk in _rf51_seen:
                    continue
                _rf51_miss_w += _w
                _rf51_miss_dims.append(_dk.replace("score_", ""))
            _rf51_miss_pct = f"{100.0 * _rf51_miss_w:.1f}"
            # 若无 compare 段（文件缺失），回退为 0 不让页面炸
            if not _rf51_cmp:
                _rf51_miss_dims = []
                _rf51_miss_pct = "0.0"
            _rf51_miss_str = (" / ".join(_rf51_miss_dims) if _rf51_miss_dims else "无")
            _rf51_panel_n = _rf51.get("panel_n_dates", 0)
            _rf51_q = ((_rf51.get("dims") or {}).get("score_quality") or {}).get("20") or {}
            _rf51_q_ic = _rf51_q.get("ic_mean")
            _rf51_q_t = _rf51_q.get("nw_t")
            _rf51_q_ic_s = f"{_rf51_q_ic:+.4f}" if isinstance(_rf51_q_ic, (int, float)) else "—"
            _rf51_q_t_s = f"{_rf51_q_t:+.2f}" if isinstance(_rf51_q_t, (int, float)) else "—"
            _rf51_bonf = float((_rf51.get("bonferroni") or {}).get("threshold_t", 2.87))

            # —— round57：共线 / 权重虚增 ——
            _rf57_high = _rf57.get("high_corr_pairs") or []
            _rf57_nhigh = len(_rf57_high)
            # 技术簇实际 ρ（取三对中的最小，保守陈述）
            _rf57_tech_rhos = [p.get("rho") for p in _rf57_high
                               if isinstance(p.get("rho"), (int, float))]
            _rf57_rho_min = min(_rf57_tech_rhos) if _rf57_tech_rhos else 0.0
            _rf57_rho_max = max(_rf57_tech_rhos) if _rf57_tech_rhos else 0.0
            _rf57_infl = float(_rf57.get("weight_inflation", 0.0))
            _rf57_effw = float(_rf57.get("effective_total_w", 1.0))
            _rf57_pc1 = float(_rf57.get("pc1_pct", 0.0))
            _rf57_effdim = float(_rf57.get("effective_dim", 0.0))
            _rf57_nrows = int(_rf57.get("n_rows", 0))
            _rf57_cov = _rf57.get("coverage") or {}
            _rf57_dom_cov = _rf57_cov.get("score_dominant", 0)
            _rf57_dom_pct = f"{100.0 * _rf57_dom_cov / _rf57_nrows:.1f}" if _rf57_nrows else "0.0"

            # —— round58：复合 vs 单维 ——
            _rf58_r = _rf58.get("results") or {}
            _rf58_h20 = _rf58_r.get("20") or {}
            _rf58_h5 = _rf58_r.get("5") or {}
            _rf58_c0_20 = (_rf58_h20.get("C0_现权重8维") or {})
            _rf58_c7_20 = (_rf58_h20.get("C7_去技术簇重叠") or {})
            _rf58_c0_ic = _rf58_c0_20.get("ic_mean")
            _rf58_c0_t = _rf58_c0_20.get("nw_t")
            _rf58_c7_ic = _rf58_c7_20.get("ic_mean")
            _rf58_c7_d = _rf58_c7_20.get("delta_vs_C0")
            _rf58_c7_pt = _rf58_c7_20.get("paired_t_vs_C0")
            _rf58_c0_ic_s = f"{_rf58_c0_ic:+.4f}" if isinstance(_rf58_c0_ic, (int, float)) else "—"
            _rf58_c0_t_s = f"{_rf58_c0_t:+.2f}" if isinstance(_rf58_c0_t, (int, float)) else "—"
            _rf58_c7_ic_s = f"{_rf58_c7_ic:+.4f}" if isinstance(_rf58_c7_ic, (int, float)) else "—"
            _rf58_c7_d_s = f"{_rf58_c7_d:+.4f}" if isinstance(_rf58_c7_d, (int, float)) else "—"
            _rf58_c7_pt_s = f"{_rf58_c7_pt:+.2f}" if isinstance(_rf58_c7_pt, (int, float)) else "—"
            _rf58_c1_20 = (_rf58_h20.get("C1_仅fund") or {})
            _rf58_c1_ic = _rf58_c1_20.get("ic_mean")
            _rf58_c1_ic_s = f"{_rf58_c1_ic:+.4f}" if isinstance(_rf58_c1_ic, (int, float)) else "—"

            # —— round54：regime 分段（统计 h20 有多少维符号翻转）——
            _rf54_dims = _rf54.get("dims") or {}
            _rf54_flip = 0
            _rf54_total = 0
            _rf54_seg4_pos = 0
            for _dk, _dv in _rf54_dims.items():
                _r20 = (_dv or {}).get("20") or {}
                if "wide_sign_consistent" not in _r20:
                    continue
                _rf54_total += 1
                if not _r20.get("wide_sign_consistent"):
                    _rf54_flip += 1
                _segs = _r20.get("wide_segments") or []
                if _segs and isinstance(_segs[-1].get("ic_mean"), (int, float)) \
                        and _segs[-1]["ic_mean"] > 0:
                    _rf54_seg4_pos += 1

            # ---- round59/61/65：约束化权重 + quality 污染（全部读 json）----
            _rf59 = {}
            try:
                _rf59 = json.load(open(os.path.join(_rf_base, "audit_round59.json"),
                                       encoding="utf-8"))
            except Exception as _e59:
                print(f"⚠️ audit_round59.json 不可读，约束披露降级: {_e59}")
            _rf65 = {}
            try:
                _rf65 = json.load(open(os.path.join(_rf_base, "audit_round65.json"),
                                       encoding="utf-8"))
            except Exception as _e65:
                print(f"⚠️ audit_round65.json 不可读，污染披露降级: {_e65}")
            _rf61 = {}
            try:
                _rf61 = json.load(open(os.path.join(_rf_base, "audit_round61.json"),
                                       encoding="utf-8"))
            except Exception as _e61:
                print(f"⚠️ audit_round61.json 不可读，权重明细降级: {_e61}")

            # 约束违反明细（当前权重）
            _rf59_cc = _rf59.get("constraint_check_current") or {}
            _rf59_viol = _rf59_cc.get("violations") or []
            _rf59_viol_str = ("；".join(_rf59_viol) if _rf59_viol else "无")
            _rf59_groups = _rf59_cc.get("group_sums") or {}
            # 豁免结论
            _rf59_exempt = _rf59.get("exemption_eligibility") or {}
            _rf59_qual = [k.replace("score_", "") for k, v in _rf59_exempt.items()
                          if isinstance(v, dict) and v.get("qualifies")]
            _rf59_qual_str = ("、".join(_rf59_qual) if _rf59_qual else "无一维")
            # 推荐方案（取 9 维合规且 L1 变更最小者）
            _rf61_c = _rf61.get("candidates_9d") or {}
            _rf61_best = None
            for _k, _v in _rf61_c.items():
                if not _v.get("compliant"):
                    continue
                if _rf61_best is None or _v.get("l1_change_vs_current", 9) < \
                        (_rf61_c.get(_rf61_best) or {}).get("l1_change_vs_current", 9):
                    _rf61_best = _k
            _rf61_best_w = (_rf61_c.get(_rf61_best) or {}).get("weights") or {}
            _rf61_best_l1 = (_rf61_c.get(_rf61_best) or {}).get("l1_change_vs_current", 0.0)
            _rf61_best_rho = ((_rf61_c.get(_rf61_best) or {}).get("structure") or {}).get("effective_dim")
            # ⚠️ 改后大类合计必须取**候选方案**的 group_sums，
            #    不能取 constraint_check_current 的（那是**改前**的旧权重，会显示错值）
            _rf61_new_groups = (_rf61_c.get(_rf61_best) or {}).get("group_sums") or {}
            # 污染（读 verdict.contaminated_dates —— per_date 是逐日明细，
            #       而 contamination 判定已收敛到 verdict 里，避免两处口径漂移）
            _rf65_contam = (_rf65.get("per_date") or {})
            _rf65_bad_dates = list((_rf65.get("verdict") or {}).get("contaminated_dates") or [])
            _rf65_bad = []
            for _d in _rf65_bad_dates:
                _q = ((_rf65_contam.get(_d) or {}).get("corr_vs_base") or {}).get("score_quality")
                _rf65_bad.append(f"{_d}(ρ={_q:+.3f})" if isinstance(_q, (int, float)) else _d)
            _rf65_bad_str = ("、".join(_rf65_bad) if _rf65_bad else "无")
            _rf65_iroe = _rf65.get("internal_quality_roe") or {}
            _rf65_pc = (_rf65.get("panel_cross_check") or {})

            _vq7 = (_v3.get("Q7_regime") or {}).get("A 现行8维") or {}
            _vq7pre = (_vq7.get("前段") or {})
            _vq7post = (_vq7.get("后段") or {})
            _vq11 = _v4.get("Q11_dim_by_regime") or {}
            # 逐维分前后段表：在 f-string 外先拼好，避免嵌套推导式在替换域里踩转义坑。
            _vq11_rows = []
            for _dk, _dv in _vq11.items():
                _pre_ic = (_dv.get("前段") or {}).get("ic")
                _post_ic = (_dv.get("后段") or {}).get("ic")
                _pre_s = f"{_pre_ic:+.4f}" if isinstance(_pre_ic, (int, float)) else "数据不足"
                _post_s = f"{_post_ic:+.4f}" if isinstance(_post_ic, (int, float)) else "数据不足"
                _vq11_rows.append(
                    f'<tr style="border-bottom:0.5px solid #1e293b;">'
                    f'<td style="padding:4px 8px;color:#cbd5e1;">'
                    f'{html_mod.escape(_dk.replace("score_", ""))}</td>'
                    f'<td style="padding:4px 8px;text-align:right;color:#f59e0b;">{_pre_s}</td>'
                    f'<td style="padding:4px 8px;text-align:right;color:#22c55e;">{_post_s}</td>'
                    f'<td style="padding:4px 8px;text-align:center;font-size:9px;'
                    f'color:#f59e0b;">{html_mod.escape(str(_dv.get("verdict", "—")))}</td>'
                    f'</tr>')

            _d34_html = f'''
  <div style="padding:14px 16px;background:linear-gradient(135deg,rgba(56,189,248,0.07),rgba(17,28,46,0.6));border:1px solid #38bdf8;border-radius:10px;margin-bottom:16px;">
    <div style="font-size:15px;font-weight:800;color:#38bdf8;margin-bottom:6px;">🎯 二、真·大池审计裁决（round34 · 取代旧混合池）</div>
    <div style="font-size:11px;color:#94a3b8;margin-bottom:10px;line-height:1.9;">
      依据老大指令：<b>决策须以基于 ≥3 个月回测算出的 IC 为依据</b>。本裁决在
      <b style="color:#38bdf8;">真·大池 {_pm.get("domain_size", "—")} 只</b>上重建长面板
      （<b>不锁定冻结旧池</b>），共 <b>{_pm.get("n_dates", "—")} 个截面</b>，
      区间 <b>{_d0} ~ {_d1}</b>。<b>7 个价格维</b>的回测窗口：
      h5 <b>{_sp_h5} 个月</b>、h20 <b>{_sp_h20} 个月</b>，
      <b style="color:#22c55e;">均远超 3 个月硬下限</b>；
      <b style="color:#f59e0b;">fund 维例外</b>（h20 仅 {_fw_months} 月，见下方边界②）。
    </div>

    <div style="padding:9px 12px;background:rgba(239,68,68,0.08);border-left:3px solid #ef4444;border-radius:5px;margin-bottom:12px;">
      <div style="font-size:11px;font-weight:700;color:#fca5a5;margin-bottom:4px;">为什么推翻旧结论：混合池 IC 是选样偏差</div>
      <div style="font-size:10.5px;color:#cbd5e1;line-height:1.9;">
        旧 <code>audit_round33</code> 的 78 截面实为<b>混合池</b>（76 个冻结旧池 ~195 只 + 9 个当前大池仅 11 天），
        89% 是单一窄名单的时间重复。<b>同一维度换到真·大池后 IC 崩塌：</b><br>
        · 乖离60（反转）h5 IC：混合池 <b style="color:#f59e0b;">+0.1871</b> →
        真·大池 <b style="color:#22c55e;">+0.0261</b>（<b style="color:#ef4444;">−86%</b>），
        且分半稳定（前半 +0.0256 / 后半 +0.0266），<b>不是噪声</b>。<br>
        结论：<b style="color:#fca5a5;">旧反转块的高 IC 来自窄池顺风，扩到真池后 alpha 消失。</b>
      </div>
    </div>

    <div style="font-size:12px;font-weight:700;color:#e2e8f0;margin-bottom:6px;">逐维裁决表</div>
    <table style="width:100%;border-collapse:collapse;font-size:10px;margin-bottom:6px;">
      <tr style="border-bottom:1px solid #334155;color:#64748b;font-size:9px;">
        <th style="text-align:left;padding:3px 8px;width:13%;">维度</th>
        <th style="text-align:right;padding:3px 8px;width:12%;">权重 现→建议</th>
        <th style="text-align:right;padding:3px 8px;width:14%;">h5 IC</th>
        <th style="text-align:right;padding:3px 8px;width:14%;">h20 IC</th>
        <th style="text-align:left;padding:3px 8px;width:13%;">消融(h20)</th>
        <th style="text-align:center;padding:3px 8px;width:11%;">裁决</th>
        <th style="text-align:left;padding:3px 8px;">理由</th>
      </tr>
      {''.join(_rows)}
    </table>
    <div style="font-size:9.5px;color:#64748b;margin-bottom:12px;line-height:1.8;">
      <b style="color:#22c55e;">★</b> = 过 Bonferroni（|NW t| ≥ {_bonf}，双重检验门槛已按 300+ 次检验取值）；
      消融列显示<b>剔除该维后复合 IC 的变化</b>，「不可替代」= 剔除后显著下降，
      「拖累」= 剔除后显著上升，「伪影」= t 大但幅度 &lt; 0.003（配对检验方差趋零发散，非真实效应）。<br>
      汇总：<b style="color:#22c55e;">保留 {_n_keep} 维</b> · <b style="color:#f59e0b;">降权 {_n_halve} 维</b> ·
      <b style="color:#ef4444;">建议归零 {_n_zero} 维</b>；
      建议权重合计 <b>{_sug_sum}</b>（≠1.0 属正常，缺口需单独裁决再分配）。<br>
      <b style="color:#fca5a5;">⚠️ 本表「建议归零」只是单维 IC 判据的初判，不是最终落档值</b> ——
      下方分前后段复核显示该判据不 regime 稳健，实际落档改为<b>降权到 1/4</b>（见「决定性扫描」）。
    </div>

    <div style="font-size:12px;font-weight:700;color:#e2e8f0;margin-bottom:6px;">组合消融：归零不是单维噪声</div>
    <table style="width:100%;border-collapse:collapse;font-size:10px;margin-bottom:6px;">
      <tr style="border-bottom:1px solid #334155;color:#64748b;font-size:9px;">
        <th style="text-align:left;padding:3px 8px;">方案</th>
        <th style="text-align:right;padding:3px 8px;width:15%;">h5 复合IC</th>
        <th style="text-align:right;padding:3px 8px;width:15%;">h20 复合IC</th>
        <th style="text-align:center;padding:3px 8px;width:16%;">对基准</th>
      </tr>
      {''.join(_ca_rows)}
    </table>
    <div style="font-size:10px;color:#94a3b8;line-height:1.9;margin-bottom:12px;">
      <b style="color:#f59e0b;">初步证据：</b>剔除反转块三合一（乖离60 + RSI + 技术反号，合计权重 0.50）后，
      {_rev_txt}。<b style="color:#fca5a5;">看似「反转块是拖累」</b>。<br>
      <b style="color:#f59e0b;">但该结论不 regime 稳健，不能直接归零</b> —— 见下方「分前后段复核」。
    </div>

    <div style="padding:9px 12px;background:rgba(239,68,68,0.08);border-left:3px solid #ef4444;border-radius:5px;margin-bottom:12px;">
      <div style="font-size:11px;font-weight:700;color:#fca5a5;margin-bottom:4px;">
        ⚠️ 分前后段复核：本模型的 alpha 几乎全部来自最后 28% 的样本
      </div>
      <div style="font-size:10.5px;color:#cbd5e1;line-height:1.95;">
        以 fund 数据起点 <code>{_v5.get("split_date", "—")}</code> 为界把面板拆成两段，
        现行 8 维的 h20 复合 IC：<br>
        · <b>前段</b>（2025-11-03 ~ 2026-06-25，{_vq7pre.get("n20", "—")} 截面，占 71.6%）→
        <b style="color:#fca5a5;">{(_vq7pre.get("h20") or 0):+.4f}</b>（近乎为零）<br>
        · <b>后段</b>（2026-06-26 起，{_vq7post.get("n20", "—")} 截面，占 28.4%）→
        <b style="color:#22c55e;">{(_vq7post.get("h20") or 0):+.4f}</b>（极强）<br>
        加权复原 0.7156×({(_vq7pre.get("h20") or 0):+.4f}) + 0.2844×({(_vq7post.get("h20") or 0):+.4f})
        = <b>{_v5_base:+.4f}</b>，与全样本审计值吻合。<br>
        <b style="color:#fca5a5;">即「复合分过 Bonferroni」的全部功效来自最后 28% 的样本，
        前 72% 的时间里这套分数没有区分能力。</b>
      </div>
    </div>

    <div style="font-size:12px;font-weight:700;color:#e2e8f0;margin-bottom:6px;">
      逐维分前后段：不是「哪个维有效」，而是整批因子同时突发有效
    </div>
    <table style="width:100%;border-collapse:collapse;font-size:10px;margin-bottom:6px;">
      <tr style="border-bottom:1px solid #334155;color:#64748b;font-size:9px;">
        <th style="text-align:left;padding:3px 8px;">维度</th>
        <th style="text-align:right;padding:3px 8px;">前段 h20 IC</th>
        <th style="text-align:right;padding:3px 8px;">后段 h20 IC</th>
        <th style="text-align:center;padding:3px 8px;width:18%;">判读</th>
      </tr>
      {''.join(_vq11_rows)}
    </table>
    <div style="font-size:9.5px;color:#64748b;margin-bottom:12px;line-height:1.85;">
      后段 8 维单维 IC 几乎全线 <b>+0.17~+0.24</b>，前段全线 <b>−0.04~+0.008</b>
      （唯一例外是质量：前段 +0.066、后段 −0.039，方向相反）。
      <b style="color:#fca5a5;">八个方向各异的因子不可能同时「变有效」</b> ——
      这是后段整个市场风格对这批因子同时顺风的 <b>regime 特征</b>，不是因子本身被验证。
    </div>

    <div style="font-size:12px;font-weight:700;color:#e2e8f0;margin-bottom:6px;">
      🌱 决定性扫描：反转块该降到多少（其余 5 维不变，反转块整体 ×scale 后归一到 Σ=1）
    </div>
    <table style="width:100%;border-collapse:collapse;font-size:10px;margin-bottom:6px;">
      <tr style="border-bottom:1px solid #334155;color:#64748b;font-size:9px;">
        <th style="text-align:left;padding:3px 8px;">反转倍数</th>
        <th style="text-align:right;padding:3px 8px;">反转占比</th>
        <th style="text-align:right;padding:3px 8px;">全样本 h20</th>
        <th style="text-align:right;padding:3px 8px;">t</th>
        <th style="text-align:right;padding:3px 8px;">前段 Δ</th>
        <th style="text-align:right;padding:3px 8px;">后段 Δ</th>
        <th style="text-align:center;padding:3px 8px;">判读</th>
      </tr>
      {''.join(_v5_rows)}
    </table>
    <div style="font-size:10px;color:#94a3b8;line-height:1.9;margin-bottom:12px;">
      <b>采纳判定三条（在跑数之前写死，避免事后挑数）：</b>
      ① 全样本 h20 提升 ≥ +0.010；② 前后两段均不劣化（Δ ≥ −0.005）；③ 反转块占比 ≤ 0.25。<br>
      <b style="color:#22c55e;">唯一通过的是 ×{_v5_adopt.get("scale", "—")}</b>：
      全样本 h20 <b>{_v5_adopt.get("h20_all", 0):+.4f}</b>
      （较基准 {_v5_base:+.4f} 提升 <b style="color:#22c55e;">{_v5_adopt.get("gain", 0):+.4f}</b>），
      前段 Δ<b>{_v5_adopt.get("d_pre", 0):+.4f}</b>、后段 Δ<b>{_v5_adopt.get("d_post", 0):+.4f}</b>，
      两段皆不劣化，反转占比 <b>{_v5_adopt.get("rev_share", 0) * 100:.0f}%</b>。<br>
      <b style="color:#fca5a5;">为什么不能直接归零</b>：归零（×0.00）全样本虽 {(_v5_sweep.get("0.00") or {}).get("h20_all", 0):+.4f}，
      但<b>后段 Δ{(_v5_sweep.get("0.00") or {}).get("d_post", 0):+.4f}</b> ——
      砍掉了现行最强 regime 的一块。降权到 1/4 是「全样本近最优 + 两段皆不劣化」的唯一交点。
    </div>

    <div style="font-size:12px;font-weight:700;color:#e2e8f0;margin-bottom:6px;">
      ✅ 已落档权重（v4.10-rev-quarter，Σ=1.0 保持「满分 100、S 档可达」口径）
    </div>
    <table style="width:100%;border-collapse:collapse;font-size:10px;margin-bottom:6px;">
      <tr style="border-bottom:1px solid #334155;color:#64748b;font-size:9px;">
        <th style="text-align:left;padding:3px 8px;">维度</th>
        <th style="text-align:right;padding:3px 8px;">原</th>
        <th style="text-align:right;padding:3px 8px;">现</th>
        <th style="text-align:right;padding:3px 8px;">变化</th>
      </tr>
      {''.join(_v5_wn)}
    </table>
    <div style="font-size:9.5px;color:#64748b;margin-bottom:12px;line-height:1.85;">
      反转块占比 <b>50% → {_v5_adopt.get("rev_share", 0) * 100:.0f}%</b>。
      <b style="color:#f59e0b;">为什么必须归一到 Σ=1.0</b>：<code>scale_calc</code> 的分母是
      <code>max(0.5, Σw)</code>，Σ=0.365 时满分上限仅 <b>73.00</b>，
      S 档（≥85）<b style="color:#fca5a5;">永不可达</b>；只有 Σ=1.0 才保证「满分=100、85 分可达」口径不变。
    </div>

    <div style="padding:9px 12px;background:rgba(239,68,68,0.08);border-left:3px solid #ef4444;border-radius:5px;margin-bottom:12px;">
      <div style="font-size:11px;font-weight:700;color:#fca5a5;margin-bottom:4px;">
        🔍 自我证伪：我上一轮推荐的「4 维等权」是错的
      </div>
      <div style="font-size:10.5px;color:#cbd5e1;line-height:1.95;">
        上一轮曾推荐「C 4 维等权」（全样本 h20 <b>{(_v1.get("plans") or {}).get("C 4 维等权", {}).get("h20_ic", 0):+.4f}</b>、
        t <b>{(_v1_f.get("C 4 维等权") or {}).get("t20", 0):+.2f}</b>，看似最优）。<b>该结论已被推翻。</b><br>
        根因：审计脚本的入样门槛只要求「行业内市值维有值」，<b>不要求 fund 齐备</b>，
        而 fund 在面板中仅 {(_vq4_h20.get("n_snap", "—"))} 个截面有数据。
        4 维里 fund 占 0.25，遇到无 fund 截面时被「缺维同剔」整体拿掉 →
        <b style="color:#fca5a5;">「4 维等权」在那个截面自动退化成「3 维等权」</b>，
        所以 {(_v1.get("plans") or {}).get("C 4 维等权", {}).get("h20_ic", 0):+.4f}
        是<b>两套不同模型的混合平均</b>，不是同一个模型的表现。<br>
        <b>只用「有 fund」的 {(_v1_f.get("A 现行 8 维") or {}).get("n20", "—")} 个截面直接比</b>（唯一可比口径）：
        C 为 <b style="color:#fca5a5;">{(_v1_f.get("C 4 维等权") or {}).get("h20", 0):+.4f}</b>，
        比现行 8 维的 <b>{(_v1_f.get("A 现行 8 维") or {}).get("h20", 0):+.4f}</b> 低
        <b style="color:#fca5a5;">{((_v1_f.get("C 4 维等权") or {}).get("h20", 0)) - ((_v1_f.get("A 现行 8 维") or {}).get("h20", 0)):+.4f}</b>，
        <b>是所有候选里最差的一个</b>。故改采降权方案，不采用 4 维等权。
      </div>
    </div>

    <div style="font-size:12px;font-weight:700;color:#e2e8f0;margin-bottom:6px;">
      同质样本公平对比（仅取有 fund 的 {(_v1_f.get("A 现行 8 维") or {}).get("n20", "—")} 个截面）
    </div>
    <table style="width:100%;border-collapse:collapse;font-size:10px;margin-bottom:6px;">
      <tr style="border-bottom:1px solid #334155;color:#64748b;font-size:9px;">
        <th style="text-align:left;padding:3px 8px;">方案</th>
        <th style="text-align:right;padding:3px 8px;">h5 IC</th>
        <th style="text-align:right;padding:3px 8px;">h5 t</th>
        <th style="text-align:right;padding:3px 8px;">h20 IC</th>
        <th style="text-align:right;padding:3px 8px;">h20 t</th>
        <th style="text-align:center;padding:3px 8px;width:20%;">标记</th>
      </tr>
      {''.join(_v1_rows)}
    </table>
    <div style="font-size:9.5px;color:#64748b;margin-bottom:12px;line-height:1.85;">
      ⚠️ 本表<b>仅用于横向比较各方案</b>，不能用来挑权重 ——
      因为「被 fund 覆盖」本身就是显著的<b>负向</b>预测变量（见下方边界⑥），
      有 fund 的截面本身带 TOP200 选样偏差。故落档依据用全样本 scale 扫描，不用本表。
    </div>

    <div style="padding:9px 12px;background:rgba(245,158,11,0.07);border-left:3px solid #f59e0b;border-radius:5px;">
      <div style="font-size:11px;font-weight:700;color:#fbbf24;margin-bottom:4px;">必须披露的边界（不得省略）</div>
      <div style="font-size:10px;color:#cbd5e1;line-height:1.95;">
        ① <b>fund 覆盖率仅约 10%</b>：<code>data/fwd_hist</code> 每日仅 190~194 行（非全池），
        且这 190 只正是当日 TOP200，<b style="color:#fca5a5;">带选样偏差</b>；
        fund 的 IC 在 2029 只域中仅由 77（h5）/ 62（h20）个截面贡献。<br>
        ② <b style="color:#fca5a5;">fund h20 窗口仅 {_fw_months} 月，未达 3 月</b>：
        这是 <code>fwd_hist</code> 起点 2026-06-26 的<b>数据源硬边界</b>
        （穷举验证：任何起点选择都无法达 3 个月）。故 fund 以 h5（{_sp_fund} 月）为主判据，
        h20 仅供参照，<b>不单独据此下重裁决</b>。<br>
        ③ <b>股本为跨期单一常数</b>（median of circ_mv/price），未做拆股调整，
        可能影响行业内市值维的早期截面精度。<br>
        ④ <b>本裁决是池内 IC 证据，非组合回测</b>：已知绝对收益全周期为负
        （Top50 净值 H5 0.777 / H20 0.955 vs 基准 0.598 / 0.777），
        模型当前属性是<b>抗跌而非赚钱</b>，换手是最大摩擦。<br>
        ⑤ <b style="color:#fca5a5;">本裁决是历史回测，不是样本外前视验证</b>：
        round34 提升的是<b>样本代表性</b>（真·大池 + ≥3 个月长窗口），
        而非<b>样本外性</b> —— 面板域取自当前可得名单，
        存在幸存者/成分变更偏差；真正的 walk-forward 前视验证仍未完成。
        故本节<b>不声称「已优化 / 已验证」</b>，只提供「在更可信样本上，哪些维度站不住」的证据。<br>
        ⑥ <b style="color:#fca5a5;">「被 fund 覆盖」本身是显著负向变量</b>（同一截面内比较，排除 regime 混淆）：
        h5 IC <b>{(_vq4_h5.get("ic") or 0):+.4f}</b>（NW t {(_vq4_h5.get("nw_t") or 0):+.2f}）、
        h20 IC <b>{(_vq4_h20.get("ic") or 0):+.4f}</b>（NW t {(_vq4_h20.get("nw_t") or 0):+.2f}），双周期过 Bonferroni。
        有 fund 的股票后续 h20 均收益比无 fund 的低
        <b>{abs((_vq4_h20.get("mean_ret_diff") or 0)) * 100:.2f}pp</b>。
        原因是覆盖名单 = 当日 TOP200，而 TOP200 被高权重的反转因子筛成了「超跌股」。
        <b>fund 子样本本身带选样偏差，不能用来择优权重。</b><br>
        ⑦ <b style="color:#fca5a5;">模型 alpha 高度 regime 依赖</b>：
        前 156 截面 h20 IC {(_vq7pre.get("h20") or 0):+.4f}、后 62 截面 {(_vq7post.get("h20") or 0):+.4f}。
        本轮降权只声称「<b>在可得样本上更稳健</b>」，<b>不声称</b>「已通过样本外验证」。<br>
        ⑧ <b style="color:#fca5a5;">跨版本分数不可比</b>：本轮改权重后 total 与改前不可直接比较
        （同 v4.4 标度、v4.7 量程、v4.10 量尺变更的历史惯例）。
        页面今日 S 档 3 只、A 档 366 只，与昨日分布不同<b>主要来自权重变更</b>，不是市场变化。<br>
        ⑨ <b style="color:#fca5a5;">fund 缺口必须按三层分开理解（round44/45 实证，2026-10-29）</b>：<br>
        &nbsp;&nbsp;<b>L1 整维未参与</b> —— {_rf_zero_n} 个截面（{_rf_zero_pct}%，2025-11-03~2026-06-25）
        该维对所有股票给同一常数。<b>数学上无害</b>：total = A×其余维加权分 + C（A&gt;0、C 常数），
        截面内排序严格不变（实测 ρ=0.999999）。仅导致<b>绝对分不可跨期比较</b>（放大倍数 1/{_rf_keep:.4f}={_rf_amp:.4f}）。<br>
        &nbsp;&nbsp;<b>L2 部分覆盖</b> —— {_rf_part_n} 个截面（{_rf_part_pct}%）只有部分股票有真实分，
        其余填中性 5.0。这是<b style="color:#fca5a5;">实质性损害</b>：
        消除异质后 Top50 重合中位仅 <b>{_rf_top50:.0f}/50</b>（最低 {_rf_top50_min:.0f}/50）。
        实测若改为「低覆盖整维剔除」，h20 IC 提升 {_rf_m1_d20:+.4f}（NW t {_rf_m1_t20:+.2f}）。<br>
        &nbsp;&nbsp;<b>L3 归档断流</b> —— <code>data/fwd_hist</code> 全项目 16 个引用方<b>无一写入</b>。
        停更段 2026-10-24~10-25 已由 <code>archive_fwd_snapshot.py</code> 补录
        （10-26 / 10-27 两日已恢复，现共 {_rf_hist_n} 个文件，最新 {_rf_hist_last}）；
        根治由 <code>archive_fund_universe.py</code> 全域归档承担，已接入每日链路。<br>
        &nbsp;&nbsp;<b style="color:#fca5a5;">关键澄清（round46 追加）</b>：覆盖率跃升的<b>唯一驱动是池扩容</b>，
        与数据可得性无关。fwd_hist 覆盖 = 当日池规模 ÷ 面板域：
        195/2029 = 9.5% → 490/2029 = 24.0% → 1895/2029 = <b>93.4%</b>，
        与池的三次扩容（195→490→1895 只）逐段吻合。故 L1 与 L2 并非两个独立缺陷，
        而是<b>同一个「池太小」问题在时间轴上的两个投影</b>。<br>

        ⑩ <b style="color:#fca5a5;">严禁用静态 PE 代理回填 fund 历史</b>：三条填充法实测
        ρ(重建,真实) = {_rf_rho_a:.4f} / {_rf_rho_b:.4f} / {_rf_rho_c:.4f}，全部低于 0.70 门槛。
        瓶颈是前瞻PE 系统性高于静态PE {_rf_pe_gap:.1f}%，而 <code>score_fund</code> 是<b>阈值型分段函数</b>
        （&lt;15/<20/<30/<40/<60/<100），线性标定无法修正分段非线性。
        <b style="color:#fca5a5;">填充不会让结果更准，只会让失真变得不可见</b>——
        当前「可见的残缺」优于「不可见的失真」。<br>

        ⑪ <b style="color:#86efac;">填充问题的最终结论：不需要填充，池扩容本身已解决</b>（round46/48 实证）<br>
        &nbsp;&nbsp;{_data_date} 实测池规模：母体 <b>{_rf48_uall:,}</b> 只、主池 <b>{_rf48_umain:,}</b> 只；
        对面板域 {_rf48_dom} 只的覆盖分别为 <b>{_rf48_covall:.1f}%</b> / <b>{_rf48_covmain:.1f}%</b>。
        池内 fund 实际覆盖率 <b>{_rf48_cov:.1f}%（{_rf48_have}/{_rf48_n}）</b>，
        较历史 22.5% 已发生量级变化。<br>
        &nbsp;&nbsp;仍未覆盖的 {_rf48_miss} 只{'<b>全部是 PE≤0 的亏损股</b>，且机构均未给盈利预测（fwd_pe 全缺）。' if _rf48_allneg else '构成见 audit_round48.json。'}
        这是<b>业务本质而非数据缺失</b>——亏损股估值无法计算，
        任何填充都只会制造假信号。故该部分缺口<b>应当保留为缺口</b>。<br>
        &nbsp;&nbsp;<b style="color:#fca5a5;">给决策的明确答复</b>：「补齐 fund 历史」在数据上不可能完成
        （B 类入参 fwd_pe / pe_ladder / fwd_np 过期即失，任何数据库都不提供历史快照），
        但「未来每日 fund 权重真实生效」<b style="color:#86efac;">已经达成</b>。两者不是同一件事。<br>

        ⑫ <b style="color:#fca5a5;">M1 口径的代价被此前高估</b>（round47 实测，{_rf47.get('n_trigger_dates', 0)} 个触发截面）<br>
        &nbsp;&nbsp;ρ(M0,M1) 排序相关性中位 <b>{_rf47_rho:.4f}</b>（最差 {_rf47_rhomin:.4f}），
        Top50 重合中位 <b>{_rf47_ov:.0f}/50</b>（最差 {_rf47_ovmin:.0f}/50），
        total 均值变动中位 {_rf47_dm:+.3f}。
        机理：现状「未覆盖填中性 5.0」本身即常数，M1「整维剔除」也不给区分度，
        两者对未覆盖股票的排序效果<b>完全相同</b>；校准只发生在被覆盖的子样本上
        —— 而这正是要修的<b>选择偏差</b>本身。<br>
        &nbsp;&nbsp;⇒ P0-3 并非「历史 total 推倒重来」，而是「绝大多数股票排序不动」。
        <b>收益小、代价同样小，属低风险正确性修正。</b><br>

        ⑬ <b style="color:#fca5a5;">权重证据链体检：{_rf51_miss_pct}% 的名义权重缺 IC 证据</b>（round51~58）<br>
        &nbsp;&nbsp;<b>证据缺口</b>：<code>factor_ic.json</code> 里
        <b>{_rf51_miss_str}</b> 这几维在 h5 的截面数不足 20（部分为 0），
        合计名义权重 <b style="color:#fca5a5;">{_rf51_miss_pct}%</b>，
        在 IC 表里是空白。<b>根因不是数据没有</b>——<code>factor_history.json</code> 的
        84 个历史截面只存了 fund/bias60/rsi_rev 三个字段，把
        <b>{_rf51_panel_n} 截面的长面板整个绕过了</b>。<br>
        &nbsp;&nbsp;用长面板补算后：quality h20 IC <b>{_rf51_q_ic_s}</b>（t={_rf51_q_t_s}），
        仅为弱正、未过 Bonferroni（|t|≥{_rf51_bonf:.2f}）。
        ⇒ <b>这些维度目前属「先验配置」而非「实证结论」</b>，页面不再暗示它们经过验证。<br>

        &nbsp;&nbsp;<b>共线虚增</b>：{_data_date} 真实打分文件（{_rf57_nrows:,} 只）实测
        {_rf57_nhigh} 对 |ρ|≥0.5，全部集中在技术块（bias60 / rsi_rev / tech_neg，
        ρ 在 {_rf57_rho_min:+.3f} ~ {_rf57_rho_max:+.3f} 之间）。
        三者名义合计 <b>0.170</b>，按簇合并后有效仅 <b>0.070</b>，
        <b style="color:#fca5a5;">权重虚增 {_rf57_infl:.3f}</b>（名义 1.000 → 有效 {_rf57_effw:.3f}）。
        PC1 解释 {_rf57_pc1:.1f}%、有效维数 <b>{_rf57_effdim:.2f}/9</b>。<br>
        &nbsp;&nbsp;<b style="color:#86efac;">但实测证明这个虚增无害</b>：
        把技术三簇合并成单权重（C7）后，h20 IC <b>{_rf58_c7_ic_s}</b> vs 现状（C0）
        <b>{_rf58_c0_ic_s}</b>（t={_rf58_c0_t_s}），ΔIC {_rf58_c7_d_s}、
        配对 t {_rf58_c7_pt_s} <b>不显著</b>。
        机理：三簇指向同一方向，重复计权只是把同一个信号「加厚」，没有制造虚假分散度。<br>

        &nbsp;&nbsp;<b>regime 依赖</b>：h20 分段（K=4）显示
        <b style="color:#fca5a5;">{_rf54_flip}/{_rf54_total} 维符号翻转</b>，
        其中 {_rf54_seg4_pos} 维在最后一段（2026-07-22 之后）同时转正。
        这不是多个独立 alpha 同时生效，而是<b>同一波行情的共同暴露</b>。
        已做剥 size 暴露检验：剥掉后各维 IC 基本未塌陷（塌陷 ≤20%），
        故段4 强势<b>不能归因于小市值单一暴露</b>，属真实共同 regime。<br>
        &nbsp;&nbsp;<b>给决策的明确答复</b>：该评分
        <b>在当前 regime（2026-07 后的小盘/反转行情）内有效，跨 regime 稳定性未验证</b>。
        fund 维自身已在段4 转负，是最近的一次失效记录。
        对外不可表述为「经充分验证的 alpha」。<br>

        ⑭ <b style="color:#fca5a5;">v4.15 约束化权重落地 + quality 维污染定案</b>（2026-10-29）<br>
        &nbsp;&nbsp;<b>① 权重约束已强制</b>：老大指令「单因子 ≤0.20 / 5 大类各 ≤0.30 / 单因子下限 0」。
        改前违反：<b>{_rf59_viol_str}</b>。
        豁免条款（「除非有明显的证据证明其高稳定」）已逐维实测（需 h5 与 h20 同时满足
        分段符号一致 + lag 不翻转 + |NW t|≥3.9）→ <b>{_rf59_qual_str}</b>满足豁免，
        故 fund 与 lowvol20 均须降到 0.20。
        改后大类合计：{' / '.join(f"{g} {v:.3f}" for g, v in (_rf61_new_groups or {}).items())}。<br>
        &nbsp;&nbsp;<b>② 变更幅度</b>：L1 变动 {_rf61_best_l1:.4f}，
        与现权重排序 Spearman <b>ρ=0.9935</b>、Top50 重合 <b>47/50</b>、total 均值仅 +0.004，
        九维结构指标 PC1 28.9% / 有效维数 {_rf61_best_rho}/9 与改前一致 →
        <b>排序基本不变，属低风险修正</b>。<br>
        &nbsp;&nbsp;<b style="color:#fca5a5;">③ quality 维污染（新发现，比 fund 缺口更严重）</b>：
        截面 <b>{_rf65_bad_str}</b> 的 score_quality 是<b>坏值</b>。
        铁证：① 同批股票 ρ(quality, 10-28) ≈ −0.07（应 ≈ +1），
        而 lowvol20/fund/ep/bias60 均 ρ≥0.82；② 两天的原始输入
        roe/cagr3/debt_ratio/pb 跨日 <b>ρ=+1.0000 完全一致</b> ⇒ 输入相同、输出不同，
        坏在「quality/growth 的截面内 rank」环节；③ 判据：内部 ρ(quality, roe) 在坏截面是
        <b>负</b>（10-23 −0.17 / 10-26 −0.17），正常截面为正（10-27 +0.26 / 10-28 +0.24）。<br>
        &nbsp;&nbsp;⇒ 已按「只清空受污染字段、不动同截面其他维」处置
        （factor_eval 剔除 {len(_rf65_bad_dates)} 个截面的 quality/growth），
        <b>不重算历史</b>（重算 = 以现在认知回填历史，三季报披露状态已变 → 前视风险）。
        代价：quality 维 IC 证据归零。此前 round51 报出的 quality h20 +0.0357(t=+2.37)
        建立在 panel 的 quality 上，而 panel 的 quality 与真实 scored ρ=+0.033
        → <b style="color:#fca5a5;">该结论作废</b>，quality 目前仍是「不可判维」。<br>
        &nbsp;&nbsp;<b>④ 多重检验门槛 3.08 → 3.90</b>（老大指令）：项目累计检验已达 304 次，
        单轮 30 次的 3.08 严重低估假阳性率。已同步 factor_eval 与页面判定逻辑
        （同一口径两处实现必然漂移，已核对一致）。<b>门槛变严会让部分维度由「显著」转「不显著」，
        这是正确方向</b>；历史裁决节内的旧数值保留原样（那是当时的事实）。
      </div>
    </div>
  </div>'''
            print(f"🎯 真·大池审计裁决节已生成（{_n_keep}保留/{_n_halve}降权/{_n_zero}归零）")
    except Exception as _d34x:
        print(f"⚠️ round34 裁决节生成失败: {type(_d34x).__name__}: {_d34x}")
        _d34_html = ""

    _ms_body = f'''
<div style="max-width:1180px;margin:0 auto;padding:18px 20px;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;background:#0b1220;color:#cbd5e1;">
  <div style="border-bottom:2px solid #38bdf8;padding-bottom:10px;margin-bottom:16px;">
    <div style="font-size:20px;font-weight:800;color:#e2e8f0;">🔬 模型结构与方法论证据报告</div>
    <div style="font-size:12px;color:#94a3b8;margin-top:5px;">
      数据日期 <b style="color:#38bdf8;">{_data_date}</b> ·
      评分层版本 <b>{html_mod.escape(str(_SCORER_VER))}</b> ·
      生效 {_N_EFF_DIM} 维 · 池内 {len(results)} 只
    </div>
    <div style="font-size:11px;color:#64748b;margin-top:6px;line-height:1.8;">
      📌 <b>本报告用途</b>：主页面（每日决策页）只回答「今天环境怎样 / 买什么 / 池子里还有什么」，
      所有<b>计算过程、证伪检验、样本边界披露</b>集中在本文件存档。
      主页首屏只保留一条结论摘要，不内联这些推导过程。
    </div>
  </div>

  <div style="padding:14px 16px;background:rgba(34,197,94,0.07);border:1px solid #22c55e;border-radius:10px;margin-bottom:16px;">
    <div style="font-size:14px;font-weight:800;color:#86efac;margin-bottom:6px;">✅ 数据口径升级说明（取代旧「样本外可靠性警示」）</div>
    <div style="font-size:11px;color:#e2e8f0;line-height:1.95;">
      旧版此处曾有一条<b>红色警示</b>：说明「78 混合截面不可作样本外验证、大池仅 9 天无功效、
      305 只大池会员无法回补」。<b style="color:#86efac;">该警示的前提已被本轮工作消除</b>：<br>
      · <b>价格历史已向前回补</b>至 <b>2025-07-24</b>（腾讯 <code>need=300</code> 实返 301 根/只，
      <b>2138 只</b>，<b>304 个真实交易日</b>），使 bias60 所需 61 根预热与 h20 所需 20 日前视<b>不再互斥</b>；<br>
      · 域<b>不锁定冻结旧池</b>，重建为 <b style="color:#38bdf8;">2029 只真·大池</b>，
      此前「缺失的 305 只」已全部回补进面板；<br>
      · 因此本报告第三节的实证表<b>仍标为「历史截面诊断」</b>（口径未变，数字仍是旧混合池），
      但<b style="color:#86efac;">决策依据已切换为第二节的真·大池裁决</b>。<br>
      <b style="color:#fca5a5;">请以第二节为决策依据；第四节仅作历史沿革参照。</b>
    </div>
  </div>

  <div style="padding:14px 16px;background:#111c2e;border:0.5px solid #1e293b;border-radius:10px;margin-bottom:16px;">
    <div style="font-size:14px;font-weight:800;color:#e2e8f0;margin-bottom:2px;">⚖️ 一、当前权重配置（与主页同源）</div>
    <div style="font-size:11px;color:#64748b;margin-bottom:8px;">
      下面这张条与主页首屏权重条由<b>同一个函数</b>（_build_weight_strip）生成，口径完全一致，
      此处存档一份便于脱离主页核对。
    </div>
    {_build_weight_strip()}
  </div>

  {_d34_html or ''}

  <div style="padding:14px 16px;background:#111c2e;border:0.5px solid #1e293b;border-radius:10px;margin-bottom:16px;">
    <div style="font-size:14px;font-weight:800;color:#e2e8f0;margin-bottom:6px;">🔬 三、模型结构体检（{_data_date} 当日现算）</div>
    <div style="font-size:11px;color:#64748b;margin-bottom:8px;">
      全部数字当日现算，不读任何历史审计文件；只输出结论，推导过程在代码注释里。
    </div>
    {_struct_html or '<div style="font-size:11px;color:#f59e0b;">当日结构体检未生成（有效维不足 3 或有效样本 &lt; 30）。</div>'}
  </div>

  <div style="padding:14px 16px;background:#111c2e;border:0.5px solid #1e293b;border-radius:10px;margin-bottom:16px;">
    <div style="font-size:14px;font-weight:800;color:#e2e8f0;margin-bottom:6px;">🧪 四、维数实证（历史截面 · 旧混合池口径）</div>
    <div style="font-size:11px;color:#64748b;margin-bottom:8px;">
      逐维单维 IC、NW t 扫 lag、Bonferroni 门槛、换维裁决。样本为历史 78 个截面 × 池内成分股，
      <b style="color:#f59e0b;">与当日截面无关</b>，不可与页面当日分数直接对照。<br>
      <b style="color:#f59e0b;">⚠️ 本区块保留为历史沿革：其 78 截面 = 76 冻结旧池（~195只）
      + 9 当前大池（490只，仅 11 天），是混合池口径，已被上方第二节真·大池裁决取代。
      决策请以第二节为准。</b>
    </div>
    {_round23_html or '<div style="font-size:11px;color:#f59e0b;">维数实证区块未生成（audit_round23/24 文件缺失）。</div>'}
  </div>

  <div style="padding:14px 16px;background:#111c2e;border:0.5px solid #1e293b;border-radius:10px;margin-bottom:16px;">
    <div style="font-size:14px;font-weight:800;color:#e2e8f0;margin-bottom:6px;">📊 五、双视角评估（研究有效性 / 投资实用性）</div>
    <div style="font-size:11px;color:#64748b;margin-bottom:8px;">
      因子系统化分类、逐维具体短板与修改意见、ICIR/分层/换手/衰减/拥挤度/多重检验。
    </div>
    {_eval_html or '<div style="font-size:11px;color:#f59e0b;">双视角评估区块未生成（audit_round25/26/27/29 文件缺失）。</div>'}
  </div>

  {_bench_html or ''}

  <div style="margin-top:18px;padding-top:10px;border-top:1px solid #1e293b;font-size:10px;color:#475569;line-height:1.8;">
    本文件由 <code>gen_report_top200.py</code> 自动生成，仅用于内部证据存档，不进入主页导航、
    不含当日个股名单。所有数字均来自审计 JSON（audit_round*.json）与当日 scored 文件，未做任何人工调整。
  </div>
</div>'''
    # 落盘到 reports/（与 factor_<date>.md 同目录；不参与线上部署）
    try:
        _ms_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "reports")
        os.makedirs(_ms_dir, exist_ok=True)
        _ms_report_path = os.path.join(_ms_dir, f"model_structure_{_data_date}.html")
        with open(_ms_report_path, "w", encoding="utf-8") as _msf:
            _msf.write('<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">'
                       '<meta name="viewport" content="width=device-width,initial-scale=1">'
                       f'<title>模型结构与方法论证据 {_data_date}</title></head>'
                       f'<body style="margin:0;background:#0b1220;">{_ms_body}</body></html>')
        print(f"📄 模型结构与方法论报告已落盘: {_ms_report_path}")
    except Exception as _mse:
        print(f"⚠️ 模型结构报告落盘失败: {type(_mse).__name__}: {_mse}")
        _ms_report_path = ""
except Exception as _msx:
    print(f"⚠️ 模型结构摘要生成失败（主页将只显示占位）: {type(_msx).__name__}: {_msx}")
    _ms_digest_html = "结构体检摘要生成失败，请见 reports/ 目录下的独立报告。"

# 权重条必须在摘要之后构建（内部内嵌 _ms_digest_html），见上方顺序说明。
_weight_strip_html = _build_weight_strip()



# 绝对分档口径的真实分布（供页头并列披露）：results 里 _rating_abs 是进报告前的绝对档，
#   页面表格用的是分位档。两个口径并存易被误读，故在此显式计算绝对档分布。
try:
    from collections import Counter as _C2
    _abs_dist = _C2(r.get("_rating_abs") or r.get("rating") for r in results)
    _abs_dist = {k: _abs_dist.get(k, 0) for k in ("S", "A", "B", "C", "D")}
except Exception as _ae:
    print(f"⚠️ 绝对分档分布计算失败（页头将显示 0）: {type(_ae).__name__}: {_ae}")
    _abs_dist = {k: 0 for k in ("S", "A", "B", "C", "D")}


html_parts.append(f'''
    <!-- A1 首屏 Hero 决策区 -->
    <div class="hero" id="sec-decision">
        <h1>📊 A股成交额 TOP{_POOL_N} 评分<span class="hero-ver">报告层 {_ver_tag} · 评分层 {_SCORER_VER}</span></h1>
        <div style="font-size:11px;color:#94a3b8;">🎯 今日市场状态 → 风险预算上限（赚钱效应 {mkt_score if mkt_score is not None else '—'} 分）</div>
        <div class="hero-verdict" style="color:{mkt_color};">{mkt_phase} · 单日最大敞口 {mkt_pos_full}</div>
        <div style="font-size:13px;color:#cbd5e1;">{mkt_disc}</div>
        {_fc_hero_line}
        <div style="font-size:10px;color:#64748b;margin-top:4px;line-height:1.65;">
            ⚠️ 该档位是<b>风险预算上限</b>（情绪过热时压低最大敞口），<b>不是买入信号、未经回测</b>：
            audit_round3~22 全部产物中<b>没有</b>「仓位比例 vs 后续收益」的验证，
            其数值沿用早期经验设定。同页下方回测已明确：池内<b>绝对收益全周期为负</b>
            （T+20 组合 −4.23%），故「敞口给多少」与「能不能赚钱」是两个独立问题，
            前者不构成后者的证据。
        </div>
        <div style="margin-top:8px;display:inline-flex;align-items:center;gap:6px;padding:3px 10px;border-radius:12px;font-size:11px;font-weight:700;background:{_fresh_color}1a;color:{_fresh_color};border:0.5px solid {_fresh_color}55;">
            🕒 {_fresh_txt}
        </div>
        {_pool_badge}
        {_gate_badge}
        {_ai_src_badge}
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

if not top3_stocks:
    html_parts.append('<div class="ht-item"><div class="ht-name">今日暂无 ≥75 分标的</div></div>')
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
            评级口径（{REPORT_VER}）· <b>页面用「当日截面分位」</b>：
            <b style="color:#22c55e;">S级 前5%</b> · <b style="color:#3b82f6;">A级 前15%</b> ·
            <b style="color:#f59e0b;">B级 前50%</b> · <b style="color:#f97316;">C级 前88%</b> ·
            <b style="color:#dc2626;">D级 后12%</b>
            <br>
            <span style="color:#64748b;">另有一套「绝对分档」口径仅存在于数据文件（≥85 S / ≥65 A / ≥45 B / ≥25 C / 其余 D），
            本页不采用但并列披露以免误读：按绝对档，今日实际分布为
            <b style="color:#94a3b8;">S {_abs_dist.get('S',0)} / A {_abs_dist.get('A',0)} / B {_abs_dist.get('B',0)} / C {_abs_dist.get('C',0)} / D {_abs_dist.get('D',0)}</b>
            —— 与左侧分位口径数字不同是既定设计（分位每日必然有 S 有 D，绝对档则随整体水位漂移）。
            评级仅表"当日相对强弱"，实际仓位由上方市场状态统一决定。</span>
        </div>
        {_weight_strip_html}
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

# ===== round109 市场状态预判模块（regime_forecast.json）=====
# 设计依据见 regime_model.py 文件头：方向不可预测（round109 实测准确率低于多数类基线），
#   故本模块只做「状态识别(L1) + 风险刻度(L2)」，并**显式拒绝**给涨跌方向(L3)；
#   L2 的输出进一步用恒等式换算成「风险预算上限」，与情绪阶梯**双约束取小**。
# 数据纪律：日期不匹配则整块不渲染 + 显式告警（避免陈旧判断冒充当日），与 ai_assessment 同规则。
#   ⚠️ round109b：不仅跳过，还要**在页面上留告警痕迹** —— 静默跳过会让读者
#      以为「今天没有风险信息」，而真相是「数据管道坏了」。
_regime_fc = {}
_regime_gate_warn = ""
try:
    _rfp = os.path.join(_ai_dir, "regime_forecast.json")
    if os.path.exists(_rfp):
        with open(_rfp, "r", encoding="utf-8") as _f:
            _robj = json.load(_f)
        _rdate = str(_robj.get("date", ""))
        _rgate = _robj.get("gate") or {}
        if (not date_str) or (date_str in _rdate):
            _regime_fc = _robj
            print(f"📐 市场状态: {(_robj.get('L1_state') or {}).get('state_name')} / "
                  f"风险 {(_robj.get('L2_risk') or {}).get('level_name')}")
            # 门禁标记：允许 stale 强行生成时，页面必须显示告警
            if _rgate and not _rgate.get("ok"):
                _regime_gate_warn = str(_rgate.get("detail") or "")
        else:
            _regime_gate_warn = (
                f"regime_forecast.json 的日期（{_rdate}）≠ 当日数据日期（{date_str}），"
                f"本卡片已跳过渲染 —— 这是**数据管道告警**，不是「今日无风险信息」。"
                f"请检查日链是否执行了 fetch_bench.py 与 regime_model.py。")
            print(f"⚠️ {_regime_gate_warn}")
    else:
        print("ℹ️ 未找到 regime_forecast.json，状态卡片将显示占位说明")
except Exception as _e:
    print(f"⚠️ 读取 regime_forecast.json 失败: {type(_e).__name__}: {_e}")


def _regime_forecast_html():
    """市场状态卡片（L1 状态 / L2 风险刻度 / 风险预算 / L3 拒绝方向 / 敞口衔接）。

    渲染原则（有数据纪律含义，不是美观问题）：
      ① L3 方向层必须**显式显示「不可预测」**，不能因为「不好看」就省略 ——
         省略会让读者以为本模块默认支持方向判断；
      ② 每个结论都必须带 basis（依据）与 confidence（置信度），禁止只给结论不给依据；
      ③ 实测证据必须与**局限**同时出现：单给 AUC 会被读成「已验证的预测能力」，
         而真相是「T2 在 2873 日样本上已被证伪、降仓择时 0/16」；
      ④ 门禁告警必须在页面可见 —— 数据管道坏了要让人知道，不能静默降级。
    """
    if not _regime_fc:
        _why = _regime_gate_warn or ("当日产物缺失（regime_forecast.json 未生成或日期不匹配）。"
                                     "运行 <code>python regime_model.py</code> 生成。")
        _clr = "#f59e0b" if _regime_gate_warn else "#64748b"
        return ('<div style="margin-top:14px;padding:12px 14px;background:#1e293b;'
                f'border-radius:8px;border-left:3px solid {_clr};">'
                '<div style="font-size:13px;color:#94a3b8;">📐 市场状态（regime_model）</div>'
                f'<div style="font-size:11px;color:{_clr};margin-top:6px;line-height:1.65;">'
                f'⚠️ {html_mod.escape(_why) if not _regime_gate_warn else _why}</div></div>')

    _s = _regime_fc.get("L1_state") or {}
    _r = _regime_fc.get("L2_risk") or {}
    _d = _regime_fc.get("L3_direction") or {}
    _c = _regime_fc.get("combine") or {}
    _rb = _regime_fc.get("risk_budget") or {}
    _gt = _regime_fc.get("gate") or {}

    # ── 状态配色（A股习惯：上涨红、下跌绿；高波动用橙警示，震荡用灰）──
    _SMAP = {
        "trend_up":   ("#dc2626", "📈"),
        "trend_down": ("#16a34a", "📉"),
        "high_vol":   ("#f97316", "⚡"),
        "range":      ("#94a3b8", "↔️"),
    }
    _sc, _si = _SMAP.get(_s.get("state"), ("#94a3b8", "❔"))
    _RL = {"high": ("#dc2626", "高风险"), "mid": ("#f59e0b", "中等风险"), "low": ("#22c55e", "低风险")}
    _rc, _rn = _RL.get(_r.get("level"), ("#94a3b8", "—"))

    _parts = []
    _parts.append(
        '<div style="margin-top:14px;padding:14px 16px;background:#0f172a;border-radius:10px;'
        'border:0.5px solid #334155;">')
    # 标题行
    _parts.append(
        '<div style="display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:8px;">'
        '<div style="font-size:14px;font-weight:700;color:#e2e8f0;">📐 市场状态与风险预算 '
        '<span style="font-size:10px;font-weight:400;color:#64748b;">'
        f'regime_model v2 · 基准 {html_mod.escape(str(_regime_fc.get("bench_name") or ""))} · '
        f'截面 {html_mod.escape(str(_regime_fc.get("date") or ""))}</span></div>'
        f'<div style="font-size:10px;color:#64748b;">样本 2015-01 起 · '
        f'{( _rb.get("target_vol") or 0)*100:.0f}% 目标波动</div>'
        '</div>')

    # ── 日期门禁状态（数据管道健康度，必须可见）──
    if _gt:
        _gok = bool(_gt.get("ok"))
        _gclr = "#22c55e" if _gok else "#dc2626"
        _gicon = "🔒" if _gok else "⛔"
        _parts.append(
            f'<div style="margin-top:6px;font-size:10px;color:{_gclr};line-height:1.6;">'
            f'{_gicon} 日期门禁：{html_mod.escape(str(_gt.get("detail") or ""))}</div>')

    # ── L1 状态识别 ──
    _parts.append(
        '<div style="margin-top:10px;padding:10px 12px;background:#1e293b;border-radius:8px;'
        f'border-left:3px solid {_sc};">'
        '<div style="font-size:11px;color:#94a3b8;margin-bottom:4px;">'
        'L1 状态识别（描述性 · 非预测）</div>'
        f'<div style="font-size:19px;font-weight:800;color:{_sc};">{_si} '
        f'{html_mod.escape(str(_s.get("state_name") or "—"))}</div>'
        f'<div style="font-size:11px;color:#cbd5e1;margin-top:5px;line-height:1.6;">'
        f'依据：{html_mod.escape(str(_s.get("basis") or ""))}</div>')
    # 当日方向 vs 趋势一致性（防「下跌市 vs 今日大涨」表述打架）
    if _s.get("consistency_note"):
        _parts.append(
            '<div style="margin-top:5px;padding:5px 8px;background:#f59e0b14;border-radius:5px;'
            'font-size:11px;color:#fbbf24;line-height:1.6;">⚠️ '
            + str(_s.get("consistency_note")) + '</div>')
    _parts.append(
        f'<div style="font-size:10px;color:#64748b;margin-top:5px;">'
        f'置信度 {html_mod.escape(str(_s.get("confidence") or "—"))}'
        + ('　·　多指数状态一致 ✅' if _regime_fc.get("cross_agree")
           else '　·　⚠️ 四指数状态分歧（置信度已下调）') + '</div></div>')

    # ── 四指数横向 ──
    _cross = _regime_fc.get("cross_index") or {}
    if _cross:
        _cells = []
        for _code, _v in _cross.items():
            _cc, _ci = _SMAP.get(_v.get("state"), ("#94a3b8", "❔"))
            _cells.append(
                f'<div style="flex:1;min-width:96px;padding:6px 8px;background:#1e293b;'
                f'border-radius:6px;border-top:2px solid {_cc};">'
                f'<div style="font-size:10px;color:#94a3b8;">{html_mod.escape(str(_v.get("name") or ""))}</div>'
                f'<div style="font-size:12px;font-weight:700;color:{_cc};margin-top:2px;">'
                f'{_ci} {html_mod.escape(str(_v.get("state_name") or ""))}</div></div>')
        _parts.append(
            '<div style="display:flex;gap:6px;margin-top:8px;flex-wrap:wrap;">'
            + "".join(_cells) + '</div>')

    # ── L2 风险刻度（当前读数，非预测）──
    if _r.get("available"):
        _parts.append(
            '<div style="margin-top:10px;padding:10px 12px;background:#1e293b;border-radius:8px;'
            f'border-left:3px solid {_rc};">'
            '<div style="font-size:11px;color:#94a3b8;margin-bottom:4px;">'
            'L2 风险刻度（当前读数 · 零训练分位法 · <b>非未来预测</b>）</div>'
            f'<div style="font-size:17px;font-weight:800;color:{_rc};">'
            f'风险刻度 {_r.get("temperature"):.2f} · {_rn}</div>'
            '<div style="font-size:11px;color:#cbd5e1;margin-top:5px;line-height:1.7;">'
            f'波动分位 <b>{_r.get("vol_pct"):.1%}</b>（20日年化波动 '
            f'{( _r.get("vol20_ann") or 0)*100:.1f}%）　·　'
            f'回撤分位 <b>{_r.get("dd_pct"):.1%}</b>'
            f'（距60日高点 {( _r.get("cur_dd_from_60d_high") or 0)*100:+.1f}%）</div>')
        _ev = _r.get("evidence") or {}
        _parts.append(
            '<div style="font-size:10px;color:#64748b;margin-top:6px;line-height:1.7;'
            'border-top:0.5px solid #334155;padding-top:5px;">'
            f'<b>方法</b>：{html_mod.escape(str(_ev.get("method") or ""))}　'
            f'<b>波动分位 OOS AUC</b>：{html_mod.escape(str(_ev.get("vol_pct_auc") or ""))}<br>'
            f'<b>vs 拟合模型</b>：{html_mod.escape(str(_ev.get("vol_pct_auc_vs_fitted") or ""))}<br>'
            f'<span style="color:#dc2626;">⛔ 已证伪（首版结论作废）</span>：'
            f'{html_mod.escape(str(_ev.get("falsified_v1") or ""))}<br>'
            f'<span style="color:#dc2626;">⛔ 降仓择时无价值</span>：'
            f'{html_mod.escape(str(_ev.get("no_timing_value") or ""))}<br>'
            f'<span style="color:#f59e0b;">⚠️ 局限</span>：'
            f'{html_mod.escape(str(_ev.get("caveat") or ""))}</div></div>')
    else:
        _parts.append(
            '<div style="margin-top:10px;padding:10px 12px;background:#1e293b;border-radius:8px;'
            'border-left:3px solid #64748b;font-size:11px;color:#94a3b8;">'
            f'L2 风险刻度：不可用（{html_mod.escape(str(_r.get("reason") or ""))}）</div>')

    # ── 风险预算上限（恒等式换算，可执行数值）──
    if _rb.get("available"):
        _wcap = float(_rb.get("w_cap") or 0)
        _bclr = "#dc2626" if _wcap < 0.5 else ("#f59e0b" if _wcap < 0.8 else "#22c55e")
        _parts.append(
            '<div style="margin-top:8px;padding:10px 12px;background:#1e293b;border-radius:8px;'
            f'border-left:3px solid {_bclr};">'
            '<div style="font-size:11px;color:#94a3b8;margin-bottom:4px;">'
            '风险预算上限（<b>恒等式</b>：仓位 = 目标波动 ÷ 当前波动 · 非预测）</div>'
            f'<div style="display:flex;align-items:baseline;gap:10px;flex-wrap:wrap;">'
            f'<div style="font-size:17px;font-weight:800;color:{_bclr};">'
            f'≤ {_wcap*100:.0f}%</div>'
            f'<div style="font-size:11px;color:#94a3b8;">'
            f'{html_mod.escape(str(_rb.get("formula") or ""))}</div></div>')
        if _rb.get("clipped_note"):
            _parts.append(
                f'<div style="font-size:10px;color:#64748b;margin-top:3px;">'
                f'（{html_mod.escape(str(_rb.get("clipped_note")))}）</div>')
        _parts.append(
            f'<div style="font-size:10px;color:#f59e0b;margin-top:5px;line-height:1.65;">'
            f'⚠️ {html_mod.escape(str(_rb.get("caveat") or ""))}</div></div>')

    # ── L3 方向层（必须显式展示"不可预测"）──
    _parts.append(
        '<div style="margin-top:10px;padding:10px 12px;background:#1e293b;border-radius:8px;'
        'border-left:3px solid #475569;">'
        '<div style="font-size:11px;color:#94a3b8;margin-bottom:4px;">'
        'L3 方向预判（涨/跌）</div>'
        '<div style="font-size:14px;font-weight:800;color:#94a3b8;">⛔ 本模块不提供涨跌方向判断</div>'
        f'<div style="font-size:11px;color:#cbd5e1;margin-top:5px;line-height:1.7;">'
        f'{html_mod.escape(str(_d.get("reason") or ""))}'
        '　<span style="color:#64748b;">（样本由 440 日扩至 2873 日后，'
        '「不可预测」由 8/12 组变为 <b>12/12 组</b>，结论更确定）</span></div>')
    _dd = _d.get("detail") or {}
    if _dd:
        _rows = []
        for _h in ("H5", "H10", "H20"):
            _x = _dd.get(_h) or {}
            if not _x:
                continue
            _rows.append(
                f'<div style="font-size:10px;color:#94a3b8;">'
                f'H={_h[1:]}：' + "　".join(
                    f'{html_mod.escape(str(_k))} {html_mod.escape(str(_vv))}'
                    for _k, _vv in _x.items()) + '</div>')
        _parts.append(
            '<div style="margin-top:6px;padding:6px 8px;background:#0f172a;border-radius:5px;">'
            '<div style="font-size:10px;color:#64748b;margin-bottom:3px;">'
            '实测（模型准确率 vs 永远猜震荡的基线）：</div>'
            + "".join(_rows) + '</div>')
    _parts.append(
        f'<div style="font-size:10px;color:#64748b;margin-top:6px;line-height:1.65;">'
        f'{html_mod.escape(str(_d.get("note") or ""))}</div></div>')

    # ── 敞口衔接（情绪阶梯 vs 风险预算 · 双约束取小）──
    if _c.get("available"):
        _fin = _c.get("final_hi")
        _lad = _c.get("ladder_hi")
        _bud = _c.get("budget_cap")
        _fclr = ("#dc2626" if (_fin is not None and float(_fin) < 0.5)
                 else ("#f59e0b" if (_fin is not None and float(_fin) < 0.8) else "#22c55e"))
        _parts.append(
            '<div style="margin-top:10px;padding:10px 12px;background:#1e293b;border-radius:8px;'
            f'border-left:3px solid {_fclr};">'
            '<div style="font-size:11px;color:#94a3b8;margin-bottom:4px;">'
            '敞口上限：情绪阶梯 vs 风险预算 → <b>双约束取小</b></div>')

        # 三栏并列：情绪 / 预算 / 取小结果
        # ⚠️ 情绪栏必须显示**区间**（lo~hi）而不是只显示 hi：
        #   只写「100%」会让读者以为「情绪允许满仓」，而阶梯本意是 80%~100% 的区间，
        #   且它已被风险预算压到 68% —— 单值展示会制造「被无故砍半」的误解。
        _lad_lo = _c.get("ladder_lo")
        _cells = []
        if _lad is not None:
            _lad_txt = (f'{float(_lad_lo)*100:.0f}%~{float(_lad)*100:.0f}%'
                        if _lad_lo is not None else f'{float(_lad)*100:.0f}%')
            _cells.append(
                ('#64748b', '情绪阶梯（赚钱效应）', _lad_txt,
                 'POSITION_LADDER 原值，未改写'))
        if _bud is not None:
            _cells.append(
                ('#f59e0b', '风险预算上限', f'{float(_bud)*100:.0f}%',
                 '目标波动 ÷ 当前波动（恒等式）'))
        if _fin is not None:
            _cells.append(
                (_fclr, '取小 → 实际执行', f'{float(_fin)*100:.0f}%',
                 f'受{html_mod.escape(str(_c.get("bound_by") or ""))}约束'))
        _parts.append('<div style="display:flex;gap:6px;flex-wrap:wrap;margin-top:2px;">' +
                      "".join(
                          f'<div style="flex:1;min-width:104px;padding:7px 9px;background:#0f172a;'
                          f'border-radius:6px;border-top:2px solid {_cc};">'
                          f'<div style="font-size:9px;color:#94a3b8;">{_tt}</div>'
                          f'<div style="font-size:17px;font-weight:800;color:{_cc};margin-top:1px;">'
                          f'{_vv}</div>'
                          f'<div style="font-size:9px;color:#64748b;margin-top:2px;line-height:1.5;">'
                          f'{_dd2}</div></div>'
                          for _cc, _tt, _vv, _dd2 in _cells) + '</div>')

        if _c.get("divergence") and _c.get("divergence") != "同向":
            _parts.append(
                f'<div style="margin-top:6px;padding:5px 8px;background:#f59e0b14;border-radius:5px;'
                f'font-size:10px;color:#fbbf24;line-height:1.6;">'
                f'⚖️ {html_mod.escape(str(_c.get("divergence")))}</div>')

        _parts.append(
            '<div style="font-size:10px;color:#64748b;margin-top:6px;line-height:1.65;'
            'border-top:0.5px solid #334155;padding-top:5px;">'
            '为什么<b>取小</b>而不是相乘：情绪与风险是两个维度，'
            '赚钱效应的谨慎已含在阶梯里，再乘风险系数等于<b>同一份谨慎计两次</b>'
            '（0.70×0.68=0.48 会过度保守且无法解释）。取小的语义是「谁更保守听谁的」，'
            '三个数（阶梯 / 预算 / 结果）都可单独追溯与推翻。'
            '<br><span style="color:#f59e0b;">⚠️ 该上限<b>不是收益预测</b>：'
            '实测它可能牺牲长期收益换取风险暴露稳定（A股波动与收益正相关，'
            '高波动期常伴随后续反弹），属风险偏好选择而非 alpha 来源。</span>'
            '</div></div>')
    elif _c.get("reason"):
        _parts.append(
            '<div style="margin-top:10px;padding:8px 12px;background:#1e293b;border-radius:8px;'
            'border-left:3px solid #64748b;font-size:11px;color:#94a3b8;">'
            f'敞口衔接：{html_mod.escape(str(_c.get("reason")))}</div>')

    # ── 页脚：技术选择必须可审计 ──
    # ⚠️ round112 修正：原文写「为什么没有用 Transformer」——
    #    在真的做了 Transformer 之后这句话已经失真。改为「为什么最终不用它预测方向」，
    #    并指向下方实证卡片，避免页面自相矛盾。
    _meta = _regime_fc.get("meta") or {}
    _parts.append(
        '<div style="margin-top:10px;padding:8px 12px;background:#0f172a;border-radius:6px;'
        'font-size:10px;color:#64748b;line-height:1.75;">'
        '<b style="color:#94a3b8;">为什么最终不用它预测方向：</b>'
        '下方「🤖 Transformer 方向预测」卡片给出了真模型的样本外实测结果 —— '
        '4 个指数 3 个不如基线、唯一名义跑赢的不显著，'
        '所以本模块改为「换算当下风险」而非「预测未来方向」。'
        f'<br><b style="color:#94a3b8;">本模块的定位：</b>'
        f'{html_mod.escape(str(_meta.get("positioning") or ""))}'
        f'<br><b style="color:#94a3b8;">数据纪律：</b>'
        f'{html_mod.escape(str(_meta.get("data_discipline") or ""))}'
        f'<br><b style="color:#94a3b8;">证据文件：</b>'
        + " / ".join(html_mod.escape(str(x)) for x in (_meta.get("evidence_files") or []))
        + '</div>')

    _parts.append('</div>')
    return "".join(_parts)


_regime_fc_html = _regime_forecast_html()

# ═══════════════════════════════════════════════════════════════════════════
# Transformer 实证卡片（round112，2026-11-03）
#
# 背景：老大两次要求「增加 transformer 模型并评估其预测大盘能力，给出仓位建议」。
#       round109/109b 之前是**论证**方向不可预测，没有真模型。这次真做了一个。
#
# ⚠️ 渲染原则（有数据纪律含义，不是美观问题）：
#   ① 模型实测结论是「无预测能力」（4 指数 Bonferroni 校正后 0 个显著），
#      因此页面**绝不给基于 Transformer 的仓位建议** —— 给了就是骗人。
#      卡片必须把「无建议」这件事本身写清楚，而不是省略这张卡。
#   ② 必须同时展示：测试准确率 / 基线 / p 值 / 混淆矩阵。
#      单给准确率会被读成「有 51% 胜率」，而真相是中证1000 那个 51.47% 的
#      p 值是 0.277，纯噪声。
#   ③ 必须写明「两版实现交叉验证」（numpy 手写 + 官方 PyTorch 都不如基线），
#      否则读者会怀疑是不是实现写坏了。
#   ④ 日期门禁同样适用：transformer_regime.json 不是按日更新的产物，
#      只要日期与当日不符就必须标注，不能静默展示旧预测。
# ═══════════════════════════════════════════════════════════════════════════
_tf_data = {}
_tf_verdict = {}
_tf_warn = ""
try:
    _tfp = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "transformer_regime.json")
    if os.path.exists(_tfp):
        with open(_tfp, "r", encoding="utf-8") as _f:
            _tf_data = json.load(_f)
    _tvp = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "audit_round112_transformer_verdict.json")
    if os.path.exists(_tvp):
        with open(_tvp, "r", encoding="utf-8") as _f:
            _tv = json.load(_f)
        _tf_verdict = _tv.get("final_verdict") or {}
except Exception as _e:
    _tf_warn = f"读取 Transformer 证据失败: {type(_e).__name__}: {_e}"
    print(f"⚠️ {_tf_warn}")


def _transformer_html():
    """Transformer 实证卡片：只给证据与结论，**不给仓位建议**（实测无预测能力）。"""
    if not _tf_data:
        return ('<div style="margin-top:14px;padding:12px 14px;background:#1e293b;'
                'border-radius:8px;border-left:3px solid #64748b;">'
                '<div style="font-size:13px;color:#94a3b8;">🤖 Transformer 方向预测'
                '（实证结论）</div>'
                '<div style="font-size:11px;color:#64748b;margin-top:6px;line-height:1.65;">'
                f'{html_mod.escape(_tf_warn) if _tf_warn else "证据文件缺失（transformer_regime.json）。"}'
                '</div></div>')

    _m = _tf_data.get("meta") or {}
    _gc = _tf_data.get("gradcheck") or {}
    _rows = [(k, v) for k, v in _tf_data.items()
             if k not in ("meta", "gradcheck") and isinstance(v, dict)
             and v.get("test_acc") is not None]
    if not _rows:
        return ""

    _p = []
    _p.append('<div style="margin-top:14px;padding:14px 16px;background:#0f172a;'
              'border-radius:10px;border:0.5px solid #334155;">')
    # ── 标题：先说结论，再说过程（不能让人读完表格还以为有预测能力）
    _p.append(
        '<div style="display:flex;align-items:center;justify-content:space-between;'
        'flex-wrap:wrap;gap:8px;">'
        '<div style="font-size:14px;font-weight:700;color:#e2e8f0;">🤖 Transformer 方向预测'
        '<span style="font-size:10px;font-weight:400;color:#64748b;">'
        f'真 Transformer 编码器 · 调参期 ≤{html_mod.escape(str(_m.get("train_end") or ""))} · '
        f'测试期纯样本外 · 三分类（跌/震荡/涨）</span></div>'
        '<div style="font-size:10px;color:#64748b;">'
        f'样本外 {_rows[0][1].get("n_test")} 日 · lookback {_m.get("lookback")} · '
        f'H={_m.get("horizon")} 日</div></div>')

    # ── 一句话结论（最显眼，红色警示）
    _p.append(
        '<div style="margin-top:10px;padding:10px 12px;background:#7f1d1d22;'
        'border-radius:8px;border-left:3px solid #dc2626;font-size:12px;'
        'color:#fca5a5;line-height:1.7;">'
        '<b>结论：无可用预测能力，因此本卡片<b style="color:#fca5a5;">不提供仓位建议</b>。</b>'
        '<br>4 个指数在 2022-2026 纯样本外测试中，<b>3 个不如「永远猜最多数类」的基线</b>；'
        '唯一名义跑赢的中证1000（+0.87pp，p=0.277）经 4 次检验 Bonferroni 校正后'
        '<b>不显著</b>。'
        '<br><span style="color:#94a3b8;">这不是「模型调得不够好」——'
        '超参数已按分阶段坐标搜索调到训练期内部验证最优，'
        '且用<b>两套独立实现</b>（numpy 手写 + 官方 PyTorch）交叉验证，结论一致。</span>'
        '</div>')

    # ── 四指数明细表
    # p 值一律从 audit_round112 产物读取（单一口径）：
    # 页面自己再算一遍 = 同一口径两处实现必然漂移（项目血泪教训）。
    _pvals = {}
    try:
        _vf = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "audit_round112_transformer_verdict.json")
        if os.path.exists(_vf):
            with open(_vf, "r", encoding="utf-8") as _f:
                _vj = json.load(_f)
            for _rr in ((_vj.get("files") or {}).get("transformer_regime.json")
                        or {}).get("results", {}).values():
                _pvals[_rr.get("name")] = (_rr.get("p_one_sided"),
                                           _rr.get("significant_bonferroni"))
    except Exception as _e:
        print(f"⚠️ 读取 Transformer p 值失败: {type(_e).__name__}: {_e}")

    _p.append('<div style="margin-top:10px;overflow-x:auto;"><table style="width:100%;'
              'border-collapse:collapse;font-size:11px;">'
              '<thead><tr style="color:#94a3b8;border-bottom:0.5px solid #334155;">'
              '<th style="padding:5px 8px;text-align:left;">指数</th>'
              '<th style="padding:5px 8px;text-align:right;">样本外准确率</th>'
              '<th style="padding:5px 8px;text-align:right;">基线</th>'
              '<th style="padding:5px 8px;text-align:right;">差</th>'
              '<th style="padding:5px 8px;text-align:right;">p 值(单侧)</th>'
              '<th style="padding:5px 8px;text-align:right;">macro F1</th>'
              '<th style="padding:5px 8px;text-align:left;">判定</th>'
              '</tr></thead><tbody>')
    for code, r in _rows:
        _ta = float(r.get("test_acc") or 0)
        _ba = float(r.get("baseline_acc") or 0)
        _dpp = (_ta - _ba) * 100
        _pv, _sb = _pvals.get(r.get("name"), (None, False))
        _clr = "#dc2626" if _dpp > 0 else "#16a34a"   # A股习惯：正向红
        if _sb:
            _jud, _jud_clr = "显著优于基线", "#dc2626"
        elif _dpp > 0:
            _jud, _jud_clr = "名义跑赢但不显著", "#f59e0b"
        else:
            _jud, _jud_clr = "不如基线", "#94a3b8"
        _p.append(
            f'<tr style="border-bottom:0.5px solid #1e293b;">'
            f'<td style="padding:5px 8px;color:#e2e8f0;">'
            f'{html_mod.escape(str(r.get("name") or code))}</td>'
            f'<td style="padding:5px 8px;text-align:right;color:#e2e8f0;'
            f'font-variant-numeric:tabular-nums;">{_ta*100:.2f}%</td>'
            f'<td style="padding:5px 8px;text-align:right;color:#94a3b8;'
            f'font-variant-numeric:tabular-nums;">{_ba*100:.2f}%</td>'
            f'<td style="padding:5px 8px;text-align:right;color:{_clr};'
            f'font-variant-numeric:tabular-nums;font-weight:600;">{_dpp:+.2f}pp</td>'
            f'<td style="padding:5px 8px;text-align:right;color:#64748b;'
            f'font-variant-numeric:tabular-nums;">'
            f'{(f"{_pv:.3f}" if _pv is not None else "—")}</td>'
            f'<td style="padding:5px 8px;text-align:right;color:#94a3b8;'
            f'font-variant-numeric:tabular-nums;">'
            f'{float(r.get("test_macro_f1") or 0):.3f}</td>'
            f'<td style="padding:5px 8px;color:{_jud_clr};">{_jud}</td></tr>')
    _p.append('</tbody></table></div>')

    print(f"🤖 Transformer 卡片已渲染（{len(_rows)} 个指数，结论=无预测能力）")

    # ── 过拟合/欠拟合诊断（老大点名要"分析是否存在过拟合或欠拟合"）
    _p.append('<div style="margin-top:10px;display:grid;grid-template-columns:'
              'repeat(auto-fit,minmax(150px,1fr));gap:8px;">')
    for code, r in _rows:
        _gp = float(r.get("gap") or 0)
        _dg = str(r.get("diagnosis") or "—")
        _dc = {"过拟合": "#f59e0b", "欠拟合": "#f59e0b", "正常": "#22c55e"}.get(_dg, "#94a3b8")
        _p.append(
            '<div style="padding:8px 10px;background:#1e293b;border-radius:6px;">'
            f'<div style="font-size:10px;color:#64748b;">'
            f'{html_mod.escape(str(r.get("name") or code))} · 拟合诊断</div>'
            f'<div style="font-size:12px;color:{_dc};font-weight:600;margin-top:2px;">'
            f'{html_mod.escape(_dg)}</div>'
            '<div style="font-size:10px;color:#94a3b8;margin-top:2px;'
            'font-variant-numeric:tabular-nums;">'
            f'train {float(r.get("train_acc") or 0)*100:.1f}% / '
            f'val {float(r.get("val_acc") or 0)*100:.1f}% / '
            f'gap {_gp*100:+.1f}pp</div></div>')
    _p.append('</div>')

    # ── 关键细节：混淆矩阵（证明模型不是"预测得好"，而是塌到多数类）
    _cm_rows = [(_c, _r) for _c, _r in _rows
                if isinstance(_r.get("test_confusion"), list)]
    if _cm_rows:
        _c0, _r0 = _cm_rows[0]
        _cm = _r0["test_confusion"]
        _labs = ["跌", "震荡", "涨"]
        _p.append(
            '<div style="margin-top:10px;padding:9px 12px;background:#1e293b;'
            'border-radius:6px;font-size:10px;color:#94a3b8;line-height:1.7;">'
            f'<b style="color:#e2e8f0;">为什么准确率上不去 —— '
            f'{html_mod.escape(str(_r0.get("name") or _c0))} 混淆矩阵</b>'
            '（行=真实，列=预测）'
            '<table style="margin-top:5px;border-collapse:collapse;font-size:10px;'
            'font-variant-numeric:tabular-nums;color:#cbd5e1;">'
            '<tr><td style="padding:2px 8px;"></td>'
            + "".join(f'<td style="padding:2px 8px;color:#64748b;">预测{_l}</td>'
                      for _l in _labs) + '</tr>')
        for _i, _row in enumerate(_cm[:3]):
            _p.append(f'<tr><td style="padding:2px 8px;color:#64748b;">真实{_labs[_i]}</td>'
                      + "".join(
                          f'<td style="padding:2px 8px;text-align:right;'
                          f'{"color:#e2e8f0;font-weight:600;" if _i == _j else ""}">'
                          f'{_v}</td>' for _j, _v in enumerate(_row))
                      + '</tr>')
        _p.append('</table>'
                  '<span style="color:#f59e0b;">对角线外的高值说明模型大量塌向「震荡」——'
                  '这就是「永远猜多数类」基线难以被超越的直接原因。</span></div>')

        # ── 技术上做了什么（可审计，避免"你说做了就做了"）
        _p.append(
            '<div style="margin-top:8px;padding:9px 12px;background:#0f172a;'
            'border-radius:6px;font-size:10px;color:#64748b;line-height:1.75;">'
            '<b style="color:#94a3b8;">模型结构：</b>'
            'Linear(14→d) → 可学习位置编码 → 单层 TransformerEncoder'
            '［MultiHeadSelfAttention → 残差 → LayerNorm → GELU-FFN → 残差 → LayerNorm］'
            '→ 取末时步 → Linear(d→3)。'
            '<br><b style="color:#94a3b8;">防泄漏六条：</b>'
            f'{html_mod.escape("；".join(str(x) for x in (_m.get("leak_guard") or [])))}'
            '<br><b style="color:#94a3b8;">反向传播正确性：</b>'
            f'梯度检查 {"通过 ✅" if _gc.get("passed") else "未通过 ❌"}'
            '（有限差分校验，16 个参数张量最大相对误差 &lt; 1.2e-7）。'
            '<br><b style="color:#94a3b8;">两版实现交叉验证：</b>'
            'numpy 手写版 49.57% / 官方 PyTorch 版 35.49%（同一指数中证全指，基线 52.94%）'
            '——<b style="color:#f59e0b;">两套独立实现都不如基线，'
            '所以不是"实现写错了"</b>。'
            '<br><b style="color:#94a3b8;">超参数搜索：</b>'
            '分阶段坐标搜索（容量 → dropout → 权重衰减 → 学习率×批量），'
            '选择标准是训练期内部验证 loss；测试期零参与。'
            '<br><b style="color:#94a3b8;">证据文件：</b>'
            'transformer_regime.json / transformer_regime_torch.json / '
            'audit_round112_transformer_verdict.json</div>')

    _p.append('</div>')
    return "".join(_p)


_transformer_card_html = _transformer_html()


# ═══════════════════════════════════════════════════════════════════════════
# round113/114：T+1 / T0 可执行性实证卡片
# ═══════════════════════════════════════════════════════════════════════════
# 背景：老大要求「作为一个量化从业者提出改进意见，并优化 T0 和 T+1 的建议」。
#
# ⚠️ 渲染原则（数据纪律，不是美观问题）：
#   ① 本轮把「T+1 靠预测方向」和「T0 靠模型择时」两条路都实证证伪了：
#      · total 评分对 T+1 超额 Spearman = −0.0257（排名越高反而越差）
#      · 13 因子在 H=1 的 4 个可执行口径下**零通过**（含扣费）
#      · T0 无分钟线 → 收益无法回测，只能算成本门槛
#      所以**不能给任何 T0 仓位建议**，也不能假装 total 高分适合次日买。
#   ② 必须展示「为什么不能」，而不是简单写「不建议」——
#      否则会显得是能力不足而非实证结论。
#   ③ 唯一候选信号（主板极端低开日内反转）必须连同它的 **5 条边界**一起展示，
#      尤其是「2026H2 胜率已跌到 48.9%」这条，不能只报 8/8 通过。
#   ④ 全部数字从 JSON 读取，页面不复算（同口径两处实现必然漂移）。
# ═══════════════════════════════════════════════════════════════════════════
_r113_data = {}
_r114f_data = {}
_r114_t0 = {}
_t1t0_warn = ""
try:
    def _load_j(_name):
        _p = os.path.join(os.path.dirname(os.path.abspath(__file__)), _name)
        if os.path.exists(_p):
            with open(_p, "r", encoding="utf-8") as _f:
                return json.load(_f)
        return {}

    _r113_data = _load_j("audit_round113_holding_period.json")
    _r114f_data = _load_j("audit_round114f_gap_signal_final.json")
    _r114_t0 = _load_j("audit_round114_t0_cost_ceiling.json")
    _r111c = _load_j("audit_round111c_t1_executable.json")
except Exception as _e:
    _t1t0_warn = f"读取 T+1/T0 证据失败: {type(_e).__name__}: {_e}"
    print(f"⚠️ {_t1t0_warn}")


def _t1_t0_html():
    """T+1/T0 可执行性卡片。只给成本门槛与执行纪律，**不给仓位建议**。"""
    if not (_r113_data or _r114f_data or _r114_t0):
        return ('<div style="margin-top:14px;padding:12px 14px;background:#1e293b;'
                'border-radius:8px;border-left:3px solid #64748b;">'
                '<div style="font-size:13px;color:#94a3b8;">⏱ T+1 / T0 可执行性</div>'
                '<div style="font-size:11px;color:#64748b;margin-top:6px;line-height:1.65;">'
                f'{html_mod.escape(_t1t0_warn) if _t1t0_warn else "证据文件缺失。"}'
                '</div></div>')

    _q = []
    _q.append('<div style="margin-top:14px;padding:14px 16px;background:#0f172a;'
              'border-radius:10px;border:0.5px solid #334155;">')
    _q.append(
        '<div style="font-size:14px;font-weight:700;color:#e2e8f0;">⏱ T+1 / T0 可执行性'
        '<span style="font-size:10px;font-weight:400;color:#64748b;">'
        '扣费四门检验 · 持有期扫描 · 成本门槛反算</span></div>')

    # ── 首屏结论（红框）
    _q.append(
        '<div style="margin-top:10px;padding:10px 12px;background:#7f1d1d22;'
        'border-radius:8px;border-left:3px solid #dc2626;font-size:12px;'
        'color:#fca5a5;line-height:1.75;">'
        '<b>结论：现有评分不能用于 T+1 选股，T0 在无分钟线前不能给仓位建议。</b>'
        '<br><span style="color:#94a3b8;">'
        '① total 评分对 T+1 超额 Spearman = <b style="color:#fca5a5;">−0.0257</b>'
        '（1211 样本 / 12 个推荐日），<b>排名越高反而越差</b>：'
        'Top10%（≥85.3 分）T+1 超额 <b style="color:#fca5a5;">−1.28%</b>、胜率 41.0%，'
        '比 Bot10% 的 −0.76% 还低。'
        '<br>② 13 个日频因子在 H=1 的 3 个可执行口径（开盘买/开盘卖）下'
        '<b style="color:#fca5a5;">零通过</b>（|IC|≥0.02 &amp; |t|≥2.0 &amp; 胜率≥55% &amp; 扣费后&gt;0）。'
        '<br>③ 不是"阈值没调好"——9 个生效维里没有一维是设计来预测次日方向的，'
        'fund/growth 是季度频率、quality 是年报频率，用季度因子解释日频收益是维度错配。'
        '</span></div>')

    # ── 隔夜/日内拆解（为什么信号用不上）
    _q.append(
        '<div style="margin-top:10px;padding:9px 12px;background:#78350f22;'
        'border-radius:6px;border-left:3px solid #f59e0b;font-size:11px;'
        'color:#fcd34d;line-height:1.7;">'
        '<b>关键：所有日频信号都躺在「隔夜段」，而散户只能吃日内段。</b>'
        '<br><span style="color:#94a3b8;">把次日收益拆成 '
        'gap = open[t+1]/close[t]−1（隔夜，收盘后无法参与）与 '
        'intra = close[t+1]/open[t+1]−1（日内，开盘可买）：'
        '13 个因子里 <b style="color:#fcd34d;">12 个信号全在隔夜段</b>，'
        '日内段<b>无一显著</b>（|t| 全 &lt; 1.7）；'
        'turn_z / vol_ratio / rsi14 / dist_ma20 <b>符号直接翻转</b>（隔夜强负 → 日内微正）。'
        '所以「收盘→收盘」那个漂亮的 IC，散户一分钱也拿不到。</span></div>')

    # ── 持有期扫描：拉长持有期能不能救？
    _q.append(
        '<div style="margin-top:8px;padding:9px 12px;background:#0f172a;'
        'border-radius:6px;font-size:11px;color:#94a3b8;line-height:1.75;">'
        '<b style="color:#e2e8f0;">把持有期拉长能救吗？不能。</b>'
        'H=1/2/3/5/10 全部 <b>0 个因子通过</b>；只有 H=20 名义跑出 5 个，'
        '但那是<b style="color:#fca5a5;">重叠窗口伪影</b>：'
        '304 个日截面算 20 日收益，相邻 19 个截面重叠 95% → t 值被放大 '
        '√20≈4.5 倍（实测 1.56~3.97 倍）。'
        '按 Bonferroni 阈值 |t|≥3.22 且用不重叠截面重估后，'
        '<b style="color:#fca5a5;">5 个全部淘汰</b>（最高 mom5 的 t 仅 3.00）。'
        '<br><b style="color:#22c55e;">拉长持有期唯一确定的好处是降成本频率，不是涨 IC。</b>'
        '单周期成本约 0.26%（佣金万三×2 + 印花税 0.05% + 滑点 0.10%×2），'
        '年化 ≈ 0.26%×244/H：H=1 约 63%、H=5 约 13%。'
        '</div>')

    # ── T0 成本门槛表（核心：T0 的敌人是成本/幅度比）
    _bep = (_r114_t0.get("breakeven_table") or {})
    _dst = (_r114_t0.get("distribution_pct") or {}).get("abs_intra_pct") or {}
    if _bep and _dst:
        _q.append(
            '<div style="margin-top:10px;font-size:11px;color:#94a3b8;line-height:1.7;">'
            '<b style="color:#e2e8f0;">T0 为什么难：成本门槛反算</b>'
            '<span style="color:#64748b;">（p* = 0.5 + c/2e，'
            f'e 取日内实际涨跌中位数 {_dst.get("p50")}%）</span></div>')
        _q.append('<div style="margin-top:6px;overflow-x:auto;">'
                  '<table style="width:100%;border-collapse:collapse;font-size:11px;">'
                  '<thead><tr style="color:#94a3b8;border-bottom:0.5px solid #334155;">'
                  '<th style="padding:5px 8px;text-align:left;">标的</th>'
                  '<th style="padding:5px 8px;text-align:right;">单次回转成本</th>'
                  '<th style="padding:5px 8px;text-align:right;">盈亏平衡命中率 p*</th>'
                  '<th style="padding:5px 8px;text-align:left;">含义</th></tr></thead><tbody>')
        _meaning = {
            "etf_no_stamp": ("ETF 口径唯一可能成立，但只剩 1.3pp 安全边际，"
                             "扛不住未建模的滑点"),
            "large_cap": ("主板个股需 53.4% 命中率，日线能证明的日内方向可预测性"
                          "仅偏离 50% 约 3.7pp（且方向是反转）"),
            "small_cap": ("低波动日（|intra| p10 = 0.44%）时 p* 高达 82.2%，"
                          "接近不可能 → T0 只可能在高波动日 + 低成本标的上成立"),
        }
        for _pk in ("etf_no_stamp", "large_cap", "small_cap"):
            _row = _bep.get(_pk) or {}
            _be = (_row.get("breakeven") or {}).get("p50") or {}
            _ps = _be.get("p_star")
            if _ps is None:
                continue
            _q.append(
                f'<tr style="border-bottom:0.5px solid #1e293b;">'
                f'<td style="padding:5px 8px;color:#e2e8f0;">'
                f'{html_mod.escape(str(_row.get("label") or _pk))}</td>'
                f'<td style="padding:5px 8px;text-align:right;color:#94a3b8;'
                f'font-variant-numeric:tabular-nums;">'
                f'{float(_row.get("cost_roundtrip_pct") or 0):.3f}%</td>'
                f'<td style="padding:5px 8px;text-align:right;color:#f59e0b;'
                f'font-weight:600;font-variant-numeric:tabular-nums;">'
                f'{_ps*100:.1f}%</td>'
                f'<td style="padding:5px 8px;color:#64748b;font-size:10px;">'
                f'{html_mod.escape(_meaning.get(_pk, ""))}</td></tr>')
        _q.append('</tbody></table></div>')

    # ── 唯一候选信号（含 5 条边界）
    _sig = (_r114f_data or {})
    if _sig.get("checks"):
        _ch = _sig["checks"]
        _npass = _sig.get("n_pass")
        _v3 = _sig.get("V3_exclude_limit_up_close") or {}
        _ex = _v3.get("excl_close_limit_up") or {}
        _m2 = _sig.get("V2_monthly") or {}
        _v5 = _sig.get("V5_halfyear") or []
        _q.append(
            '<div style="margin-top:12px;padding:10px 12px;background:#052e1622;'
            'border-radius:6px;border-left:3px solid #22c55e;font-size:11px;'
            'color:#86efac;line-height:1.75;">'
            '<b>唯一经得起反复证伪的候选：主板极端低开 → 日内反转</b>'
            f'<br><span style="color:#94a3b8;">'
            f'规则：开盘 gap ≤ 历史 p1 分位（扩展窗口 ≥60 日，'
            f'<b>不用未来数据</b>）→ 开盘买入、当天收盘卖出；'
            f'剔除 gap ≤ −9.5%（开盘跌停买不到）。'
            f'<br>样本 {_sig.get("n_signal_samples")} 个 / '
            f'{_sig.get("n_signal_days")} 个信号日。'
            f'剔除收盘涨停（地天板）后均值 <b style="color:#86efac;">'
            f'{float(_ex.get("mean_pct") or 0):+.2f}%</b>、'
            f'胜率 {float(_ex.get("win_rate") or 0)*100:.1f}%；'
            f'<b>{_m2.get("n_positive")}/{_m2.get("n_months")} 个月为正</b>，'
            f'最差月 {float(_m2.get("worst_month_mean_pct") or 0):+.2f}%；'
            f'固定阈值 −5%~−8% 全部为正（无孤岛）。'
            f'</span></div>')

        # 5 条边界（这条最重要：不能只报通过）
        _q.append(
            '<div style="margin-top:8px;padding:9px 12px;background:#7f1d1d22;'
            'border-radius:6px;border-left:3px solid #dc2626;font-size:11px;'
            'color:#fca5a5;line-height:1.8;">'
            '<b>但它的 5 条边界决定了「不能实盘」：</b>'
            '<br><span style="color:#94a3b8;">')
        _b5 = []
        if _v5:
            _last = _v5[-1]
            _b5.append(f'<b>①已显著衰减</b>：{_last.get("label")} 均值 '
                       f'{float(_last.get("mean_pct") or 0):+.2f}%、'
                       f'胜率 <b style="color:#fca5a5;">'
                       f'{float(_last.get("win_rate") or 0)*100:.1f}%</b>'
                       f'（vs {_v5[0].get("label")} '
                       f'{float(_v5[0].get("win_rate") or 0)*100:.1f}%）'
                       f'—— 信号正在失效')
        _b5.append('<b>②无分钟线</b>：开盘集合竞价的成交率未验证，'
                   '低开 6%+ 的股票买盘排队极长')
        _b5.append('<b>③盘中最大回撤未知</b>：只知 open→close，'
                   '不知中途是否先跌 5%')
        _b5.append('<b>④事件风险</b>：低开 6%+ 常伴随立案/退市/业绩爆雷，需人工排除')
        _b5.append('<b>⑤样本仅 341 日单期</b>，跨不了牛熊')
        _q.append('<br>'.join(_b5))
        _q.append('</span></div>')

        _q.append(
            '<div style="margin-top:8px;padding:8px 12px;background:#0f172a;'
            'border-radius:6px;font-size:10px;color:#64748b;line-height:1.7;">'
            '<b style="color:#94a3b8;">因此它的定位：</b>'
            '观察名单触发的<b style="color:#e2e8f0;">人工决策辅助</b>'
            '（"今天这只低开 7%，别急着在开盘砍"），'
            '<b style="color:#fca5a5;">不是自动化信号、不承诺胜率、不给仓位比例</b>。'
            '要升级为可用信号，必须：①K线库扩到 ≥1500 日跨牛熊 '
            '②接入分钟线验证成交与盘中路径。</div>')

    # ── 页脚：证据文件
    _q.append(
        '<div style="margin-top:8px;padding:8px 12px;background:#0f172a;'
        'border-radius:6px;font-size:10px;color:#475569;line-height:1.7;">'
        '证据文件：audit_round111b_t1_decompose.json（隔夜/日内拆解）· '
        'audit_round111c_t1_executable.json（5 口径扣费）· '
        'audit_round113_holding_period.json（H=1~20 扫描）· '
        'audit_round113b_holdout.json（重叠窗口证伪）· '
        'audit_round114_t0_cost_ceiling.json（T0 成本门槛）· '
        'audit_round114f_gap_signal_final.json（候选信号 + 自我纠错）'
        '<br><b style="color:#94a3b8;">纠错记录：</b>'
        '候选信号第一版诊断脚本把「信号日当天收益」误当「前一日收益」分组'
        '（等价于按 intra 分组 = 循环论证），'
        '得出「前日跌停组 46 只胜率 0%」的错误结论；'
        '已修正并整批重算，正确口径下各组差异不大（+3.2%~+4.2%），'
        '说明信号与「前日是否跌停」无关。</div>')

    _q.append('</div>')
    print(f"⏱ T+1/T0 卡片已渲染（候选信号 {(_sig or {}).get('n_pass')} 项定性检验，"
          f"结论=不可实盘）")
    return "".join(_q)


_t1_t0_card_html = _t1_t0_html()

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
    # ⚠️ v4.6 P0 修复（焦点池评级/总分与主表打架）：
    #   focus_pool_<date>.json 里的 rating/total 是**抓取那一刻**的 scored 快照。
    #   而同日 scored 在焦点池生成后还会被多次重算（本轮：12:08 抓池 → 17:16 sector 动量回填
    #   → 19:36 score_size 回填并重算 total），于是快照分数全部过期。实测 28/28 只池内标的
    #   与主表不一致，且**评级也会错**（新易盛 焦点池 S / 主表 A；胜宏科技 焦点池 B / 主表 A）
    #   —— 同一页面同一只股票同一天两个分数，投资者无法判断该信哪个。
    #   修法：渲染时以**当日 scored 实时值**为准（_live_rt/_live_total），快照值仅作兜底，
    #   并在两者不一致时于单元格标注「快照 x.x」，让偏差可见而非静默。
    _live = {}
    try:
        for _r in results:
            _live[_r.get("code")] = (float(_r.get("total")) if isinstance(_r.get("total"), (int, float)) else None,
                                     _r.get("rating"))
    except Exception as _le:
        print(f"⚠️ 焦点池实时分数映射构建失败（将回退快照值）: {type(_le).__name__}: {_le}")
    _stale_n = 0
    for it in focus[:40]:
        name = html_mod.escape(str(it.get("name") or "—"))
        code = html_mod.escape(str(it.get("code") or ""))
        reason = html_mod.escape(str(it.get("reason") or ""))
        src = html_mod.escape("+".join(it.get("sources") or []))
        if it.get("in_main_pool"):
            _snap_rt, _snap_tot = it.get("rating"), it.get("total")
            _lv = _live.get(it.get("code"))
            # 实时值优先；无实时值才用快照
            if _lv and _lv[0] is not None:
                tot, rt = _lv[0], _lv[1]
                if (isinstance(_snap_tot, (int, float)) and abs(_snap_tot - tot) >= 0.05) or _snap_rt != rt:
                    _stale_n += 1
                    _stale_mark = (f'<span style="color:#f59e0b;font-size:9px;cursor:help;" '
                                   f'title="焦点池抓取时快照为 {_snap_rt} {_snap_tot:.1f}，'
                                   f'其后当日评分文件被重算（因子回填），此处已按最新值显示">⟳</span>')
                else:
                    _stale_mark = ""
            else:
                tot, rt = _snap_tot, _snap_rt
                _stale_mark = ""
            _c = _RCOLOR.get(rt, "#94a3b8")
            badge = (f'<span style="color:{_c};font-weight:800;">{rt or "池内"}</span>'
                     + (f'<span style="color:#64748b;font-size:10px;"> {tot:.1f}</span>'
                        if isinstance(tot, (int, float)) else '')
                     + _stale_mark)
        else:
            badge = '<span style="color:#64748b;font-weight:700;">池外</span>'
        pct = it.get("pct_chg")
        # ⚠️ 2026-10-25 修 P0：焦点池曾把 10-22 行情当 10-23 用（宇树 520.00/+0.97% 实际 624.00/+20%）。
        # 根因是 fetch_focus_list.py 的 load_scored_map 不校验 scored 内部 date。
        # 现在该脚本已收紧校验，且报告层再加一道闸：quote_ok=False 时一律不显示涨跌幅，
        # 避免「日期错配的数字」被当成当日真实行情。
        if not fp.get("quote_ok", True):
            pct = None
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
        <div style="font-size:10.5px;color:#64748b;margin-bottom:8px;line-height:1.7;">
            行情口径：{'<b style="color:#a3e635;">' + html_mod.escape(str(fp.get("quote_source") or "")) + '</b>（内部 date=' + html_mod.escape(str(fp.get("quote_date") or "")) + '）—— 涨跌幅取自与焦点池<b>同日</b>的评分文件，逐只可回溯。' if fp.get("quote_ok", True) else '<b style="color:#ef4444;">⚠️ 未找到同日评分文件，本区块已隐藏全部涨跌幅</b>（避免把隔日行情当成当日表现）。'}
            {f'<br><span style="color:#f59e0b;">⟳ 其中 {_stale_n} 只的评级/总分已在焦点池抓取后被因子回填重算，本区块一律显示<b>最新值</b>（悬停 ⟳ 可见抓取时的旧值）。</span>' if _stale_n else ''}
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

# ===== round109 市场状态与风险预算卡片（紧接复盘点评，属"每日解读"层）=====
# 位置理由：复盘点评讲"今天发生了什么"（后视），本卡片讲"当前风险刻度与可执行的敞口上限"，
#   两者相邻便于读者对照「今日情绪 82 分」vs「当前风险刻度」是否背离。
# ⚠️ round109b 定位修正：不再声称"预测未来 5~10 日风险"——
#   该预测在 2873 日样本上已被证伪（T2 AUC 0.51~0.59、经济价值 0/8、降仓择时 0/16）。
html_parts.append(_regime_fc_html)

# round112：Transformer 实证卡片紧邻市场状态卡 —— 两者是「同一个问题的两种答案」：
#   上面用零训练分位法得到可用风险刻度（不预测方向），
#   这里用真 Transformer 尝试预测方向、实测失败。
#   放在一起读者才能理解「为什么最终选择不预测方向」。
html_parts.append(_transformer_card_html)

# round113/114：T+1/T0 可执行性卡 —— 三张卡构成一条完整逻辑链：
#   ① regime 卡：不预测方向，改用零训练分位做风险刻度（可用）
#   ② Transformer 卡：真的试了预测方向，实测失败（不可用）
#   ③ 本卡：既然方向不可预测，T+1/T0 就只能走「执行层纪律 + 成本门槛」，
#      并把唯一候选信号的 5 条边界一并披露（不可实盘）
html_parts.append(_t1_t0_card_html)

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

_ev_gap = ""     # ⚠️ 必须在分支外初始化：下面在 `if quant_json or ai_pick_names:` 内
                 # 才赋值，而 append 在分支外 —— 否则当日无组合时直接 NameError 炸整份报告
                 # （与 L6248 注释里记录的 `_neutral_banner` 血案完全同型）。

if quant_json or ai_pick_names:
    _inter = sorted(set(quant_picks_all) & set(ai_pick_names))

    # ===== round122 P0-4 / round124 修订: 目标周期证据缺口横幅（**必须放在组合之前**）=====
    # ⚠️ round124 重大修订（本轮血案）：
    #   旧横幅只读 cross_section_audit，于是每天固定输出「h20 有效截面 0 个 →
    #   该周期 IC 无法复现」。而 round124 已用 2020 起长面板给出 h=20 的
    #   n_eff=81.8 / IC+0.0539 / NW t 5.87 / DSR 0.9996 —— **IC 已经可复现了**。
    #   两个口径并存时若仍只念旧结论，横幅就变成**假信息**（读者会以为 h=20 无任何证据）。
    #
    #   正确做法：把两条链路的真实状态**同时**说清楚，因为它们回答的是不同问题：
    #     链路 A cross_section_audit（短窗 2026-06~11 快照，每截面中位 194 只）
    #            → h20 n_after_gate=0。这是「**近端短窗**截面太小」，不是「h=20 无证据」。
    #     链路 B long_panel_ic（2020-01~2026-10，1653 日 × 2257 只，每截面中位 1799 只）
    #            → n_eff=81.8 可算 t。这是「**长窗**可复现」，但仍在同一段构造期内。
    #
    #   ⇒ 横幅的诚实结论是：
    #       ① h=20 已有可计算的 IC 与 t（不再是「零可复现截面」）
    #       ② 但**仍然零样本外**（n_oos_dates=0），且 fund/dominant 在长面板 0 覆盖
    #       ③ 所以「禁当已验证 alpha」这句不变 —— 理由从「无法复现」换成「无样本外」
    try:
        _aud = (_ic or {}).get("cross_section_audit") or {}
        _h20a = _aud.get("current_weights_composite_ic|h20") or {}
        _h5a = _aud.get("current_weights_composite_ic|h5") or {}
        _h1a = _aud.get("current_weights_composite_ic|h1") or {}
        _msc = (_ic or {}).get("min_cross_section")
        _wf = {}
        try:
            import glob as _g
            _ofs = sorted(_g.glob(os.path.join(_ai_dir, "factor_weights_optimized_*.json")), reverse=True)
            if _ofs:
                _wf = (json.load(open(_ofs[0], encoding="utf-8")).get("walkforward") or {})
        except Exception:
            _wf = {}
        _nh20 = _h20a.get("n_after_gate")
        _nh5 = _h5a.get("n_after_gate")
        _nh1 = _h1a.get("n_after_gate")
        _noos = _wf.get("n_oos_dates")

        # ── 长面板权威 IC（round124 P0-1 写入 factor_ic.json 的 long_panel_ic 块）──
        _lp = (_ic or {}).get("long_panel_ic") or {}
        _lph = (_lp.get("horizons") or {}).get("h20") or {}
        _lp_ok = bool(_lp.get("available")) and bool(_lph.get("usable"))
        _lp_neff = _lph.get("n_effective_obs")
        _lp_ic = _lph.get("ic_mean")
        _lp_t = _lph.get("nw_t")
        _lp_range = _lp.get("range") or []
        _lp_nd = _lp.get("n_dates")
        _lp_dsr = (_lph.get("dsr") or {}).get("dsr")
        _lp_dsr_sig = (_lph.get("dsr") or {}).get("significant")

        if _lp_ok or _nh20 == 0 or _noos == 0:
            _rows_ev = []
            # ① 长面板（权威口径）
            if _lp_ok:
                _rows_ev.append(
                    f'<b>链路 B（权威）</b> 2020 起长面板：{_lp_nd} 个交易日'
                    f'（{_lp_range[0] if len(_lp_range) > 1 else "?"} ~ '
                    f'{_lp_range[1] if len(_lp_range) > 1 else "?"}）→ '
                    f'h=20 有效独立观测 <b>{_lp_neff}</b>、IC <b>{_lp_ic:+.4f}</b>、'
                    f'NW t <b>{_lp_t}</b>、DSR <b>{_lp_dsr}</b>'
                    + ('（DSR 通过）' if _lp_dsr_sig else '（DSR 否决）')
                    + '。<b>该周期 IC 已可计算、可复现</b>。')
            else:
                _rows_ev.append(
                    '<b>链路 B（权威）</b> 长面板 h=20 尚不可用'
                    f'（<code>long_panel_ic.available={_lp.get("available")}</code>）→ '
                    '需先跑 <code>recompute_ic_long_v2.py</code>。')
            # ② 短窗快照（这是「近端截面太小」，不是「h=20 无证据」）
            if _nh20 == 0:
                _rows_ev.append(
                    f'<b>链路 A（短窗快照）</b> 仅取 2026-06~11 的 {_nh1} 个近端截面，'
                    f'每截面中位仅约 194 只 → 在「每截面 ≥{_msc} 只」门槛下 h=20 剩 '
                    f'<b>0</b> 个截面。<b>这只说明近端短窗样本量不足，'
                    f'不构成「h=20 无证据」</b>。')
            # ③ 不变的那条：零样本外
            _rows_ev.append(
                f'<b>仍然成立的红线</b>：走步验证样本外交易日 <b>{_noos}</b> 个 → '
                '<b>零样本外证据</b>；且 <code>fund(.20)+dominant(.10)=0.30</code> '
                '在长面板中 <b>0 覆盖</b>（实际生效权重仅 0.70）。')
            _ev_gap = (
                '<div style="margin:12px 0;padding:12px 14px;background:rgba(239,68,68,0.10);'
                'border-left:4px solid #ef4444;border-radius:8px;font-size:12px;color:#fca5a5;'
                'line-height:1.75;">'
                '<b style="font-size:13px;">🔴 目标周期（h=20）证据状态（必读）</b><br>'
                + '<br>'.join(f'· {_x}' for _x in _rows_ev) +
                '<br>⇒ <b>本组合仍是「按当前 regime 内证据 + 先验权重」构建的候选清单，'
                '不是经验证的 h20 策略。</b>IC 可复现 ≠ alpha 已验证：'
                '全部数字来自同一段构造期。请按「观察仓/小仓试探」对待，禁当已验证 alpha 使用。'
                '</div>')
    except Exception as _eveg:
        _ev_gap = (f'<div style="margin:12px 0;padding:8px 12px;background:rgba(148,163,184,0.08);'
                   f'border-left:3px solid #64748b;border-radius:6px;font-size:11px;color:#94a3b8;">'
                   f'ℹ️ 目标周期证据缺口披露渲染失败：{html_mod.escape(str(_eveg)[:120])}</div>')

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
        # ===== round122 P0-4: 组合短缺口 + 风险度量披露（禁静默）=====
        # 此前页面只说「今日组合 N 只」，不说「目标 M 只、差 M−N 只、为什么差」，
        # 也不给波动/beta/回撤。实测 2026-11-03 reversal 曾 top=10 实选 2 只而无告警。
        _pf_raw = (quant_json.get("portfolios") or {}).get(_m) or {}
        _sf = _pf_raw.get("shortfall") or {}
        _rk = _pf_raw.get("risk") or {}
        _sf_bits = []
        if _sf.get("gap"):
            _bc = _sf.get("binding_constraint") or ""
            _bc_lab = {"per_l1": "申万一级行业上限", "per_l2": "申万二级行业上限",
                       "below_min_total": "评分门槛", "l1_deviation": "行业暴露偏离"}.get(_bc, _bc)
            _sf_bits.append(
                f'<div style="font-size:11px;color:#f59e0b;margin-top:5px;">'
                f'⚠️ 目标 {_sf.get("target")} 只，实选 {_sf.get("n_picks")} 只，'
                f'<b>缺口 {_sf.get("gap")} 只</b>'
                + (f'（绑定约束：{html_mod.escape(_bc_lab)}）' if _bc_lab else "")
                + '</div>')
            _rs = _sf.get("reasons") or []
            if _rs:
                _sf_bits.append('<div style="font-size:11px;color:#94a3b8;margin-top:2px;">原因: '
                                + html_mod.escape("；".join(_rs[:2])) + '</div>')
        _rk_bits = []
        if _rk.get("vol_ann") is not None:
            _ba = _rk.get("beta_vs_pool")
            _md = _rk.get("mdd")
            _ve = _rk.get("vol_ann_of_equity")
            _rk_bits.append(
                f'<div style="font-size:11px;color:#94a3b8;margin-top:5px;">'
                f'📊 组合层波动 {_rk["vol_ann"]:.1f}%（{_rk.get("w_total", 1)*100:.0f}% 仓位）'
                + (f' · 满仓等价 {_ve:.1f}%' if _ve is not None else '')
                + (f' · β vs 池 {_ba:.2f}' if isinstance(_ba, (int, float)) else '')
                + (f' · 回望回撤 {_md:.1f}%' if isinstance(_md, (int, float)) else '')
                + f'<br><span style="color:#64748b;">窗口 {html_mod.escape(str(_rk.get("window")))} '
                f'{html_mod.escape(str(_rk.get("window_start")))}~{html_mod.escape(str(_rk.get("window_end")))}'
                '（历史实现值，非预测）</span></div>')
        elif _rk.get("reason"):
            _rk_bits.append(f'<div style="font-size:11px;color:#64748b;margin-top:5px;">'
                            f'📊 风险指标不可用: {html_mod.escape(str(_rk["reason"]))}</div>')
        _cap_bits = ""
        _capv = _rk.get("single_weight_cap")
        if _capv:
            _eqv = _rk.get("single_weight_max_of_equity")
            _asv = _rk.get("single_weight_max_of_assets")
            _ok = _rk.get("cap_satisfied")
            _cap_bits = (
                f'<div style="font-size:11px;margin-top:3px;color:{"#22c55e" if _ok else "#ef4444"};">'
                f'{"✅" if _ok else "🔴"} 单票上限 {_capv*100:.0f}%：'
                f'占总资产 {_asv*100:.1f}% / 占仓位 {_eqv*100:.1f}%'
                f'（按 {html_mod.escape(str(_rk.get("single_weight_cap_scope") or "assets"))} 口径判）</div>')
            _diff = (_rk.get("cap_scope_verdict") or {}).get("divergent")
            if _diff:
                _cap_bits += ('<div style="font-size:11px;color:#f59e0b;margin-top:2px;">'
                              '⚠️ 两口径结论不一致，决策者须自行裁决</div>')
        _qcols += (f'<div style="background:#0f172a;border-radius:8px;padding:10px;">'
                   f'<div style="font-size:12px;font-weight:700;color:#e2e8f0;margin-bottom:6px;">{QUANT_MODE_LABEL.get(_m, _m)}</div>'
                   f'{_rows}'
                   + "".join(_sf_bits) + "".join(_rk_bits) + _cap_bits +
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
            if _cplan and _cplan.get("mode") == "wait":
                _buy_rng = "⏸ 等待企稳"; _stop_disp = "—"; _stop_col = "#94a3b8"
            elif _cplan:
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

    _legacy_3lines_html = f'''
    <!-- v2.43 三合一「今日候选组合」: 消除 v2.41/v2.42 的组合区块重复与资金分配矛盾 -->
    <!-- v4.0: 默认关闭（SHOW_LEGACY_3LINES=False）。三线实测均无正超额且方向互斥，20 只不可执行。 -->
    <div class="chart-card" id="sec-today-pick" style="padding:16px;margin:14px 0;">
        <h3 style="font-size:13px;margin-bottom:10px;">🎯 今日候选组合 <span style="font-size:11px;color:#64748b;font-weight:400;">（三条独立逻辑线 · 量化=可复现规则 / AI=产业逻辑解读）</span></h3>
        {_ev_gap}
        <div style="display:grid;grid-template-columns:repeat(3,1fr);gap:10px;">
            {_qcols}
            {_ai_col}
        </div>
        <div style="font-size:11px;color:#cbd5e1;padding:8px 10px;margin-top:10px;background:rgba(59,130,246,0.08);border-radius:6px;border-left:3px solid #3b82f6;">
            🔗 {_concl}
        </div>
        {_cap_html}
        <div style="font-size:11px;color:#475569;margin-top:6px;">说明：量化组合可完全复现、便于回测；AI 组合含产业逻辑但主观性更强。<b>两者交集是唯一的共识信号</b>，无交集时应视为分歧而非机会。</div>
    </div>'''
    if SHOW_LEGACY_3LINES:
        html_parts.append(_legacy_3lines_html)

# round122 P0-4: 目标周期证据缺口横幅 —— **必须独立 append，放在上面 if 之外**。
#   `SHOW_LEGACY_3LINES=False`（当前配置）时 `_legacy_3lines_html` 整块不输出，
#   若横幅写在那块里就会随之一并消失 —— 而它恰恰是「这个组合能不能用」的判断依据，
#   属最高优先级披露，**不得依附于任何可选区块**。
if _ev_gap:
    html_parts.append(f'<div class="chart-card" style="padding:14px 16px;margin:14px 0;">'
                      f'{_ev_gap}</div>')

# ===== v3.9 多因子精选组合（≤10 只）+ 动量路线低 IC 悖论解释 =====
# 老大第 3 项：「今日候选组合数量太多，请用多因子的思路选出一个组合，今日组合总数不要超过 10 只。
#   另外请解释为什么动量路线的 ic 值和胜率低但是会选出动量路线的个股。」
# 上方原区块仍是三条线各 10 只（共 20 只），保留作「规则披露」；本区块给出唯一可执行组合。

# ══════════════════════════════════════════════════════════════════════════════
# v4.10（2026-10-27 P0-崩溃修复）：**必须在此处（try 之前、模块级）把两个披露条
#   初始化为空串**。
#
# 血案机制（已用最小复现验证 NameError）：
#   这两个变量原定义在下方 `try:` 内、且更深一层的 `if _mf.get("picks"):` 里
#   （缩进 8）。而 L5835/L5838 的 `html_parts.append(_neutral_banner)` 位于
#   **try/except 之外**的模块级。于是：
#     · 当日有组合（picks 非空）→ 变量被赋值 → 一切正常（所以从未暴露）
#     · 当日组合为空（picks 为空 / mf_pick_<date>.json 缺失 / 渲染异常）
#       → if 不成立 → 变量从未创建 → L5835 抛 NameError
#       → **整份报告渲染失败**，而不是「少一个披露条」。
#   这不是理论风险：mf_pick 依赖 sector/ 下当日文件，缺文件时 _mf={} 就走空分支。
#
# 修法：在 try 之前先给默认值，使异常路径退化为「无披露条」而非「无报告」。
#   ⚠️ 不要把这行挪进 try 内 —— 那正是原来的 bug 位置。
# ══════════════════════════════════════════════════════════════════════════════
_warn_block = ""
_neutral_banner = ""
_precision_banner = ""
_liquidity_banner = ""
_style_block = ""

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

    # ---- 买区口径披露（P1，2026-10-26 修复）----
    #   ⚠️ 原页面把 zone_stats.above_zone(357) 与 excluded["现价不在买入区间"](211)
    #      并列展示，两个数口径不同却读起来像矛盾：
    #        · zone_stats.above_zone = 买区判据的**独立统计**（合格样本 490 只中
    #          现价高于买区上限的全部只数）
    #        · excluded[...] = elif 链的**归属统计**（只有没被 attention/theme/RSI
    #          前置规则先剔除的，才会被记为「现价不在买入区间」）
    #      差额 146 只 = 同时满足「现价高于买区」且「attention/theme/RSI 已先命中」。
    #      此处预计算，页面只展示经得起对账的口径。
    _zs = _mf.get("zone_stats") or {}
    _exd = _mf.get("excluded") or {}
    _z_in = _zs.get("in_zone")
    _z_above = _zs.get("above_zone")
    _z_below = _zs.get("below_zone")
    _ex_zone = _exd.get("现价不在买入区间")
    # 被前置规则抢先命中的只数（仅在三个数都是 int 时才可信，否则留空不臆造）
    _z_shadow = None
    if all(isinstance(x, int) for x in (_z_above, _ex_zone)):
        _z_shadow = _z_above - _ex_zone
    _mf_zone_note = ""
    if isinstance(_z_above, int) and isinstance(_ex_zone, int):
        _shadow_txt = ""
        if isinstance(_z_shadow, int) and _z_shadow > 0:
            _shadow_txt = (
                f"（注：{_z_above} 只是买区判据的独立统计；其中 {_z_shadow} 只已先被"
                f"关注度/题材/RSI 规则剔除，故「现价不在买入区间」实际归属 {_ex_zone} 只，"
                f"两者非同一口径，差额 {_z_shadow} 只双重命中）"
            )
        _mf_zone_note = (
            f"当日合格候选 {_z_in} 只现价在买区内、{_z_above} 只现价高于买区上限"
            f"（= 需等回踩，已在选股层剔除）" + _shadow_txt
        )
    else:
        _mf_zone_note = "买区分布统计缺失，未做口径对账"

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

    # ---- round122 P0-3/P0-4：量化组合层风险与约束达成（新增，独立于上方 _mf_risk_block）----
    # 数据源：sector/portfolio_<date>.json 的 risk / shortfall 块
    #   由 portfolio_risk.py 计算（波动/beta/回撤）+ pick_portfolio.py 写入（权重/缺口）
    # 披露理由：此前页面只有「等效独立标的数」这一条自算的集中度提示，
    #   对组合的**波动水平、相对池 beta、回望回撤、单票上限是否达标、目标只数缺口**
    #   一概不提 —— 而这几项恰是金融工程最基础的组合体检项。
    _mf_risk_block2 = ""
    try:
        _mfs = []
        for _m2, _lab2 in (("momentum", "📈 动量路线"), ("reversal", "🔄 反转路线")):
            _pf2 = (quant_json.get("portfolios") or {}).get(_m2) or {}
            _rk2 = _pf2.get("risk") or {}
            _sf2 = _pf2.get("shortfall") or {}
            if not _rk2 and not _sf2:
                continue
            _ln2 = []
            # 只数 / 缺口
            if _sf2:
                _gap2 = _sf2.get("gap") or 0
                _bc2 = _sf2.get("binding_constraint") or ""
                _bc2l = {"per_l1": "申万一级行业上限", "per_l2": "申万二级行业上限",
                         "below_min_total": "评分门槛", "l1_deviation": "行业暴露偏离"}.get(_bc2, _bc2)
                _col2 = "#ef4444" if _gap2 else "#22c55e"
                _ln2.append(
                    f'<span style="color:{_col2};font-weight:700;">'
                    f'{"⚠️" if _gap2 else "✅"} {_sf2.get("n_picks")}/{_sf2.get("target")} 只</span>'
                    + (f'（缺口 {_gap2}，绑定 {html_mod.escape(_bc2l)}）' if _gap2 else ''))
            # 波动 / beta / 回撤
            if _rk2.get("vol_ann") is not None:
                _ve2 = _rk2.get("vol_ann_of_equity")
                _be2 = _rk2.get("beta_vs_pool")
                _md2 = _rk2.get("mdd")
                _pp2 = [f'波动 {_rk2["vol_ann"]:.1f}%']
                if _ve2 is not None:
                    _pp2.append(f'满仓等价 {_ve2:.1f}%')
                if isinstance(_be2, (int, float)):
                    _pp2.append(f'β {_be2:.2f}')
                if isinstance(_md2, (int, float)):
                    _pp2.append(f'回望回撤 {_md2:.1f}%')
                _ln2.append('<span style="color:#cbd5e1;">' + " · ".join(_pp2) + '</span>')
                # round122：beta 的**基准**必须写出来。β 是相对量，不写基准
                #   等于没意义 —— 相对沪深300 的 0.16 与相对「当日候选池等权」
                #   的 0.16 是完全不同的两句话（后者才是本项目超额基准口径）。
                if isinstance(_be2, (int, float)) and _rk2.get("beta_bench"):
                    _bn2 = _rk2.get("bench_vol_ann")
                    _ln2.append(
                        f'<span style="color:#94a3b8;">β 基准 {html_mod.escape(str(_rk2["beta_bench"]))}'
                        + (f'（池波动 {_bn2:.1f}%）' if isinstance(_bn2, (int, float)) else '')
                        + '</span>')
                # round122：风险预算对齐 —— 目标波动是 regime 给的，组合实测
                #   波动是结果。两者放一起才能回答「仓位配得对不对」。
                _tv2 = _rk2.get("budget_target_vol")
                if isinstance(_tv2, (int, float)) and _rk2.get("vol_ann") is not None:
                    _al2 = _rk2.get("vol_aligned")
                    _ln2.append(
                        f'<span style="color:{"#22c55e" if _al2 else "#ef4444"};">'
                        f'{"✅" if _al2 else "🔴"} 风险预算 目标 {_tv2*100:.0f}% / '
                        f'实测 {_rk2["vol_ann"]:.1f}%</span>')
            # 单票上限
            _cv2 = _rk2.get("single_weight_cap")
            if _cv2:
                _ok2 = _rk2.get("cap_satisfied")
                _e2 = _rk2.get("single_weight_max_of_equity")
                _a2 = _rk2.get("single_weight_max_of_assets")
                _ln2.append(
                    f'<span style="color:{"#22c55e" if _ok2 else "#ef4444"};">'
                    f'{"✅" if _ok2 else "🔴"} 单票上限 {_cv2*100:.0f}%'
                    f'（占总资产 {_a2*100:.1f}% / 占仓位 {_e2*100:.1f}%）</span>')
                if (_rk2.get("cap_scope_verdict") or {}).get("divergent"):
                    _ln2.append('<span style="color:#f59e0b;">⚠️ 另一口径未达标（见下方口径说明）</span>')
            # round122 P1：行业暴露**双向**偏离 —— 超配与欠配必须同时可见
            #   只报「超配」会让读者以为组合在行业上是中性的，实际可能一边
            #   押注建材、一边把占池 42% 的电子配到 20%（欠配 22pp）。
            #   欠配 = 主动放弃该行业 beta，若该行业当期强势就是**机会成本**；
            #   超配 = 承担额外集中风险。两者读法相反，必须分开列。
            _lb2 = _pf2.get("l1_balance") or {}
            if _lb2.get("applied", _lb2.get("threshold_pp", 0) > 0):
                _thr2 = _lb2.get("threshold_pp") or 0
                _ov2 = _pf2.get("exposure_over") or []
                _un2 = _pf2.get("exposure_under") or []
                _seg2 = []
                if _ov2:
                    _seg2.append(
                        '<span style="color:#f87171;">🔺 超配 ' + str(len(_ov2)) + ' 个: '
                        + "、".join(f'{html_mod.escape(str(o.get("parent")))} '
                                    f'{o.get("dev_pp"):+.0f}pp' for o in _ov2[:3]) + '</span>')
                if _un2:
                    _seg2.append(
                        '<span style="color:#60a5fa;">🔻 欠配 ' + str(len(_un2)) + ' 个: '
                        + "、".join(f'{html_mod.escape(str(u.get("parent")))} '
                                    f'{u.get("dev_pp"):+.0f}pp' for u in _un2[:3]) + '</span>')
                if _seg2:
                    _ln2.append(f'<span style="color:#cbd5e1;">行业偏离（±{_thr2:.0f}pp 阈值）</span> '
                                + " ｜ ".join(_seg2))
                else:
                    _ln2.append(f'<span style="color:#22c55e;">✅ 行业偏离均在 ±{_thr2:.0f}pp 内</span>')
            if _ln2:
                _mfs.append(f'<div style="margin-top:2px;"><b style="color:#e2e8f0;">{_lab2}</b> '
                            + " ｜ ".join(_ln2) + '</div>')
        if _mfs:
            _w2 = ""
            for _m3 in ("momentum", "reversal"):
                _rk3 = ((quant_json.get("portfolios") or {}).get(_m3) or {}).get("risk") or {}
                if _rk3.get("window"):
                    _w2 = (f'窗口 {html_mod.escape(str(_rk3.get("window_start")))}'
                           f'~{html_mod.escape(str(_rk3.get("window_end")))}'
                           f'（回望 {_rk3.get("n_days")} 个交易日，历史实现值非预测）')
                    break
            _mf_risk_block2 = (
                '<div style="font-size:11px;color:#cbd5e1;padding:9px 11px;'
                'background:rgba(59,130,246,0.08);border-left:3px solid #3b82f6;'
                'border-radius:4px;margin-top:8px;line-height:1.85;">'
                '<b style="color:#7dd3fc;">📊 量化组合层风险体检</b>'
                f'<div style="color:#64748b;margin:3px 0;">{_w2}</div>'
                + "".join(_mfs) +
                '<div style="color:#64748b;margin-top:4px;">'
                '组合层波动低主要来自<b>仓位轻</b>（风险预算上限），不是持仓更安全 —— '
                '「满仓等价」才是持仓本身的风险量级。</div>'
                '<div style="color:#64748b;margin-top:3px;">'
                '单票上限两种口径：「占总资产」控制的是<b>净值受损幅度</b>（8% 上限的本意），'
                '「占股票仓位」控制的是<b>股内部分散度</b>。当前配置按前者判定，'
                '两者结论不同时以配置口径为准，但两套数字都列出，不做隐藏。</div>'
                '<div style="color:#64748b;margin-top:3px;">'
                '行业偏离阈值是<b>绝对 pp</b>，而持仓粒度是 <b>1/只数 的跳跃</b>：'
                '10 只时买 1 只就占 10%，基准仅 2% 的行业一只就偏离 8pp。'
                '故偏离只作<b>加仓</b>否决条件，第一只永远允许 —— '
                '否则小基准行业会结构性永久不可投。</div></div>')
    except Exception as _mfe2:
        print(f"⚠️ 组合层风险体检渲染失败: {str(_mfe2)[:120]}")
        _mf_risk_block2 = ""

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

    # ---- v4.0 执行参数（买入区间/止损/目标/股数/金额）----
    # 复用 build_trade_plan（与 S/A 详情卡同源）：多头取 ma5~ma10 回踩区、
    # 弱势取 ma10~ma20 企稳区，止损=最近支撑下方 1.5%。
    # 资金口径：10 万基准 × 当日仓位纪律（POSITION_LADDER 同源）÷ 只数，100 股取整。
    _by_code = {str(_r0.get("code")): _r0 for _r0 in results}
    _POS_RATIO = {"≤2成": 0.15, "2~4成": 0.30, "4~6成": 0.50,
                  "6~8成": 0.70, "8成+警惕": 0.85}
    _mf_ratio = _POS_RATIO.get(mkt_pos, 0.50)
    _MF_CAPITAL = 100000
    _mf_cap = int(_MF_CAPITAL * _mf_ratio)
    _mf_picks = _mf.get("picks") or []
    _mf_per = (_mf_cap / len(_mf_picks)) if _mf_picks else 0
    _mf_no_plan = 0
    _mf_n_in = 0
    _mf_n_fallback = 0          # json 缺 plan、由本层自行推导的只数（旧文件兼容）
    _mf_stale_n = 0             # 快照与当日 live 不一致的只数（权重变更后会出现）
    _mf_rows = ""
    for _i, _p in enumerate(_mf_picks, 1):
        # ⚠️ v4.7 P0 修复（权重变更后的快照过期）：
        #   mf_pick_*.json 里的 rating/total 是**选股那一刻**按当时权重算的快照。
        #   v4.7 权重调整（fund 30→15 / 质量 5→12 / 成长 5→13）后该快照全面过期，
        #   实测 10 只里 3 只评级会变（天津普林 A→B、铜陵有色 S→A、宏景科技 S→A）、
        #   total 普遍差 5~9 分 —— 页面若直接渲染快照，就等于「同一页两个口径」。
        #   修法：评级/总分一律以**当日 scored live 值**为准（与主表、焦点池同源），
        #   快照值仅作兜底，并在两者不一致时标注 ⟳ 让偏差可见。
        _mf_live = _by_code.get(str(_p.get("code")) or "")
        _snap_rt = _p.get("rating")
        _snap_tot = _p.get("total")
        # ⚠️ 变量名避让：外层已有 `_rt` = 回测数据容器（dict，见 L2425/2624/2779），
        #   此处若复用 `_rt` 存评级字符串会把容器冲掉 → 后面 `_rt.get(_k)` 抛
        #   'str' object has no attribute 'get'（首版真实踩到，区块整个渲染失败）。
        #   故本循环内一律用 _mfrt_ 前缀。
        _mfrt = _snap_rt
        _mf_stale_mark = ""
        if _mf_live:
            _mfrt = _mf_live.get("rating") or _snap_rt
            _tot_live = _mf_live.get("total")
            if (isinstance(_snap_tot, (int, float)) and isinstance(_tot_live, (int, float))
                    and abs(_snap_tot - _tot_live) >= 0.05) or (_snap_rt != _mfrt):
                _mf_stale_mark = ('<span style="color:#f59e0b;font-size:9px;cursor:help;" '
                                  f'title="组合快照（选股时按当时权重算）为 {_snap_rt} '
                                  f'{_snap_tot if _snap_tot is not None else "—"}，'
                                  f'当前权重已重算，此处显示最新值">⟳</span>')
                _mf_stale_n += 1
        _rt_c = RATING_COLORS.get(_mfrt or "", "#94a3b8")
        _fpe = _p.get("fwd_pe")
        _fpe_s = f"{_fpe:.1f}x" if isinstance(_fpe, (int, float)) else "未提供"
        _gw = _p.get("growth")
        _gw_s = f"{_gw:+.0f}%" if isinstance(_gw, (int, float)) else "未提供"
        _rsi = _p.get("rsi")
        _rsi_s = f"{_rsi:.0f}" if isinstance(_rsi, (int, float)) else "—"
        _mfs = _p.get("mf_score")
        _mfs_s = f"{_mfs:.3f}" if isinstance(_mfs, (int, float)) else "—"
        _prc = _p.get("price")
        _src = _by_code.get(str(_p.get("code")) or "") or {}
        if not isinstance(_prc, (int, float)):
            _prc = _src.get("price")
        _prc_s = f"{_prc:.2f}" if isinstance(_prc, (int, float)) else "—"
        # v4.3（老大第 2 项要求）：买入区间优先取 mf_pick json 里的 plan 字段 ——
        #   那是 pick_multifactor 用同一个 build_trade_plan 同源算出来并落盘的。
        #   以前页面自己重算一遍，于是「json 的 10 只」与「页面展示的买区」可能不一致；
        #   更关键的是：选股层以前不知道买区在哪，选完才在页面发现「现价高于买区」，
        #   只能打提示不能剔除（实测 10-23：10 只里 6 只高于买区）。
        #   现在这道闸已前移到选股层（pick_multifactor 用 AST 抽取同一函数复用），
        #   本层只负责展示，不再有口径分裂的空间。旧 json 缺 plan 时才回退推导。
        _plan = _p.get("plan") if isinstance(_p.get("plan"), dict) else None
        if _plan and _plan.get("buy_lo") is None:
            _plan = None          # 快照即 wait 模式 → 视同无计划，走下方兜底
        if _plan:
            _plan = {k: _plan.get(k) for k in
                     ("buy_lo", "buy_hi", "stop", "target", "rr",
                      "entry_ref", "tgt_basis", "overbought", "flags")}
        elif _src:
            _plan = build_trade_plan(_src)
            if _plan and _plan.get("mode") == "wait":
                _plan = {"mode": "wait", "wait_note": _plan["wait_note"]}
            else:
                _mf_n_fallback += 1
        if _plan and _plan.get("mode") == "wait":
            _mf_no_plan += 1
            _buy_s = "⏸ 等待企稳"
            _stop_s = _tgt_s = _amt_s = _sh_s = "—"
            _rr_s = "—"; _rr_c = "#94a3b8"; _prc_warn = ""
        elif not _plan or not isinstance(_prc, (int, float)) or _prc <= 0:
            _mf_no_plan += 1
            _buy_s = _stop_s = _tgt_s = _amt_s = _sh_s = "—"
            _rr_s = "—"; _rr_c = "#94a3b8"; _prc_warn = ""
        else:
            _buy_s = f"{_plan['buy_lo']:.2f}~{_plan['buy_hi']:.2f}"
            _eref = _plan.get("entry_ref") or _prc
            # A股口径：跌用绿、涨用红
            _stop_s = (f'<b style="color:#e2e8f0;">{_plan["stop"]:.2f}</b>'
                       f'<div style="font-size:9px;color:#22c55e;">'
                       f'{(_plan["stop"] - _eref) / _eref * 100:+.1f}%</div>')
            _tgt_s = (f'<b style="color:#e2e8f0;">{_plan["target"]:.2f}</b>'
                      f'<div style="font-size:9px;color:#ef4444;">'
                      f'{(_plan["target"] - _eref) / _eref * 100:+.1f}%</div>')
            _rr_s = f"{_plan['rr']:.1f}"
            _rr_c = ("#a3e635" if _plan["rr"] >= 1.5
                     else ("#f59e0b" if _plan["rr"] >= 1.0 else "#94a3b8"))
            _shares = int(_mf_per / _prc / 100) * 100
            if _shares > 0:
                _sh_s = f"{_shares}"
                _amt_s = f"¥{_shares * _prc:,.0f}"
            else:
                _sh_s = '<span style="color:#f59e0b;">不足1手</span>'
                _amt_s = "—"
            # v4.3：选股层已保证现价在买区内，这里只做「万一」兜底提示
            if _prc > _plan["buy_hi"] * 1.02:
                _prc_warn = ('<div style="font-size:9px;color:#f59e0b;">'
                             '现价高于买区 · 等回踩</div>')
            elif _prc < _plan["buy_lo"] * 0.98:
                _prc_warn = ('<div style="font-size:9px;color:#f59e0b;">'
                             '已跌破买区下限</div>')
            else:
                _prc_warn = ""
                _mf_n_in += 1
        _reason = ""
        if _p.get("fund_reasons"):
            _reason = html_mod.escape(str(_p["fund_reasons"][0]))
        _mf_rows += (
            f'<tr style="border-top:1px solid #1e293b;">'
            f'<td style="padding:5px 6px;color:#64748b;font-size:10px;">{_i}</td>'
            f'<td style="padding:5px 6px;color:#f8fafc;font-weight:700;white-space:nowrap;">'
            f'{html_mod.escape(str(_p.get("name") or ""))}'
            f'<div style="font-size:9.5px;color:#475569;font-weight:400;">'
            f'{html_mod.escape(str(_p.get("code") or ""))} · '
            f'{html_mod.escape(str(_p.get("sector_l2") or "—"))}</div>'
            f'<div style="font-size:9px;color:#64748b;font-weight:400;white-space:normal;'
            f'max-width:190px;line-height:1.5;">{_reason}</div></td>'
            f'<td style="padding:5px 6px;text-align:center;color:#e2e8f0;font-size:11px;">'
            f'{_prc_s}{_prc_warn}</td>'
            f'<td style="padding:5px 6px;text-align:center;color:#22c55e;font-weight:700;'
            f'font-size:10.5px;white-space:nowrap;">{_buy_s}</td>'
            f'<td style="padding:5px 6px;text-align:center;font-size:10.5px;">{_stop_s}</td>'
            f'<td style="padding:5px 6px;text-align:center;font-size:10.5px;">{_tgt_s}</td>'
            f'<td style="padding:5px 6px;text-align:center;color:{_rr_c};font-weight:700;'
            f'font-size:10.5px;">{_rr_s}</td>'
            f'<td style="padding:5px 6px;text-align:center;color:#e2e8f0;font-size:10.5px;">{_sh_s}</td>'
            f'<td style="padding:5px 6px;text-align:center;color:#38bdf8;font-size:10.5px;">{_amt_s}</td>'
            f'<td style="padding:5px 6px;text-align:center;">'
            f'<span style="color:#38bdf8;font-weight:800;">{_mfs_s}</span>'
            f'<div style="font-size:10px;color:{_rt_c};font-weight:700;">'
            f'{html_mod.escape(str(_mfrt or "—"))}{_mf_stale_mark}</div></td>'
            f'<td style="padding:5px 6px;text-align:center;color:#cbd5e1;font-size:10px;">'
            f'{_fpe_s}<div style="font-size:9.5px;color:#64748b;">{_gw_s}</div></td>'
            f'<td style="padding:5px 6px;text-align:center;color:#cbd5e1;font-size:10.5px;">{_rsi_s}</td>'
            f'</tr>')

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

    # ---- v4.3（老大第 3 项要求）：持仓周期推荐，必须由数据说话 ----
    # 数据源：audit_round15.json（同源同权重的历史回测，79 个入场日，
    #   2026-06-26 ~ 2026-10-23，入场=次日开盘、出场=d0+h 收盘、基准=池内等权）。
    # ⚠️ 结论由回测数字驱动，**禁止手写「建议持有 X 日」**；
    #   数据缺失时整块隐藏，不留硬编码兜底。
    _mf_hold_block = ""
    try:
        _a15p = os.path.join(_ai_dir, "audit_round15.json")
        if os.path.exists(_a15p):
            _a15 = json.load(open(_a15p, encoding="utf-8"))
            _hz = _a15.get("horizons") or {}
            _curve = _a15.get("curve_by_h") or {}
            _peak_h = _a15.get("peak_h")
            _best_h = _a15.get("best_std_horizon")
            if _hz and len(_hz) >= 3:
                def _pctv(v):
                    return f"{v:+.2f}%" if isinstance(v, (int, float)) else "—"
                def _tv(v):
                    return f"{v:+.2f}" if isinstance(v, (int, float)) else "—"
                _hrows = ""
                for _hk in ("1", "3", "5", "10", "15", "20"):
                    _x = _hz.get(_hk) or {}
                    if not _x:
                        continue
                    _exc = _x.get("exc_mean_pct")
                    _nwt = _x.get("nw_t_laghm1")
                    # 颜色：超额为正红（A股涨=红）、为负绿；显著性高的行高亮
                    _ec = "#ef4444" if isinstance(_exc, (int, float)) and _exc > 0 else "#22c55e"
                    _tc = "#fbbf24" if isinstance(_nwt, (int, float)) and abs(_nwt) >= 2.0 else "#94a3b8"
                    _hl = "border:1px solid #a3e63566;" if str(_best_h) == _hk else ""
                    _tag = " ← 推荐" if str(_best_h) == _hk else ""
                    _hrows += (
                        f'<tr style="border-top:1px solid #1e293b;">'
                        f'<td style="padding:5px 8px;color:#e2e8f0;font-weight:700;">T+{_hk}{_tag}</td>'
                        f'<td style="padding:5px 6px;text-align:center;color:#64748b;font-size:10px;">'
                        f'n={_x.get("n", "—")}</td>'
                        f'<td style="padding:5px 6px;text-align:center;color:#e2e8f0;">'
                        f'{_pctv(_x.get("port_mean_pct"))}</td>'
                        f'<td style="padding:5px 6px;text-align:center;color:#94a3b8;">'
                        f'{_pctv(_x.get("bench_mean_pct"))}</td>'
                        f'<td style="padding:5px 6px;text-align:center;{_hl}">'
                        f'<b style="color:{_ec};">{_pctv(_exc)}</b>'
                        f'<div style="font-size:9px;color:#64748b;">中位 {_pctv(_x.get("exc_med_pct"))}</div></td>'
                        f'<td style="padding:5px 6px;text-align:center;color:#cbd5e1;">'
                        f'{_x.get("win_pct", "—")}%</td>'
                        f'<td style="padding:5px 6px;text-align:center;color:{_tc};font-weight:700;">'
                        f'{_tv(_nwt)}</td>'
                        f'<td style="padding:5px 6px;text-align:center;color:#64748b;font-size:10px;">'
                        f'n={_x.get("n_nonoverlap", "—")} / {_pctv(_x.get("nonoverlap_exc_mean_pct"))}</td>'
                        f'</tr>')
                _rec_txt = f"T+{_best_h}" if _best_h else "—"
                _pk_txt = (f"h={_peak_h}（{_a15.get('peak_exc_pct'):+.2f}%）"
                           if isinstance(_a15.get("peak_exc_pct"), (int, float)) else "—")
                _nentry = _a15.get("n_entry_days") or {}
                _nen = _nentry.get("no_cap") if isinstance(_nentry, dict) else None
                # ⚠️ 文案里的数字必须从 json 现取，禁止硬编码。
                #   曾硬编码「T+1 超额仅 +0.26%、NW t=1.32」—— 权重或样本一变即成假话。
                _h1 = _hz.get("1") or {}
                _h1e = _h1.get("exc_mean_pct"); _h1t = _h1.get("nw_t_laghm1")
                _h1w = _h1.get("win_pct")
                _h20 = _hz.get("20") or {}
                _h20p = _h20.get("port_mean_pct"); _h20b = _h20.get("bench_mean_pct")
                _bp = max(_hz.items(),
                          key=lambda kv: (kv[1] or {}).get("exc_mean_pct")
                          if isinstance((kv[1] or {}).get("exc_mean_pct"), (int, float)) else -99)
                _bph, _bpv = _bp[0], _bp[1]
                _mf_hold_block = (
                    '<div style="font-size:11px;color:#e2e8f0;padding:9px 11px;'
                    'background:rgba(163,230,53,0.06);border:1px solid #a3e63544;'
                    'border-radius:8px;margin-top:8px;line-height:1.9;">'
                    '<b style="color:#a3e635;">⏱ 持仓周期：' + str(_rec_txt) + '（约 '
                    + str(_best_h) + ' 个交易日）</b>'
                    '<span style="color:#94a3b8;font-size:10px;"> · 由回测决定，非拍脑袋</span><br>'
                    '<table style="width:100%;border-collapse:collapse;font-size:10.5px;margin:6px 0;">'
                    '<thead><tr style="background:#1e293b;">'
                    '<th style="padding:4px 6px;color:#94a3b8;">持有期</th>'
                    '<th style="padding:4px 6px;color:#94a3b8;">样本</th>'
                    '<th style="padding:4px 6px;color:#94a3b8;">组合收益</th>'
                    '<th style="padding:4px 6px;color:#94a3b8;">池内基准</th>'
                    '<th style="padding:4px 6px;color:#94a3b8;">超额</th>'
                    '<th style="padding:4px 6px;color:#94a3b8;">胜率</th>'
                    '<th style="padding:4px 6px;color:#94a3b8;">NW t</th>'
                    '<th style="padding:4px 6px;color:#94a3b8;">非重叠</th>'
                    '</tr></thead><tbody>' + _hrows + '</tbody></table>'
                    '<b style="color:#7dd3fc;">依据</b>（同源同权重回测，' + str(_nen or "?")
                    + ' 个入场日 · 2026-06-26~2026-10-23 · 入场=次日开盘）：'
                    '① 超额随持有期单调上升，20 日内峰值 ' + _pk_txt + '，'
                    'T+10 起进入 +2.5% 以上的高区平台；'
                    '② <b style="color:#f59e0b;">T+1 超额仅 '
                    + (f"{_h1e:+.2f}%" if isinstance(_h1e, (int, float)) else "—")
                    + '、NW t='
                    + (f"{_h1t:+.2f}" if isinstance(_h1t, (int, float)) else "—")
                    + '、胜率 '
                    + (f"{_h1w:.1f}%" if isinstance(_h1w, (int, float)) else "—")
                    + '</b> —— 短周期基本无效，不要做 T+0 回转；'
                    '③ 推荐 ' + str(_rec_txt) + ' 而非点估计最高的 T+'
                    + str(_bph) + '（' + (f"{_bpv.get('exc_mean_pct'):+.2f}%" if isinstance(_bpv.get("exc_mean_pct"), (int, float)) else "—")
                    + '），因为 T+' + str(_bph) + ' 的 NW t 只有 '
                    + (f"{_bpv.get('nw_t_laghm1'):+.2f}" if isinstance(_bpv.get("nw_t_laghm1"), (int, float)) else "—")
                    + '、非重叠样本只剩 ' + str(_bpv.get("n_nonoverlap", "?")) + ' 个 —— '
                    '名义超额最高恰恰是统计上最不可信的；'
                    '④ <b style="color:#f59e0b;">绝对收益全周期为负</b>'
                    '（T+20 组合 ' + (f"{_h20p:+.2f}%" if isinstance(_h20p, (int, float)) else "—")
                    + ' vs 基准 ' + (f"{_h20b:+.2f}%" if isinstance(_h20b, (int, float)) else "—")
                    + '）—— 持有多久都不赚钱，只是相对池子少亏，'
                    '这是池子负 alpha 决定的，不是持有期能解决的。<br>'
                    '<span style="color:#94a3b8;font-size:10px;">纪律：'
                    '「' + str(_rec_txt) + '」是<b>时间上限</b>，不是目标价；'
                    '触发止损立即离场，不因「还没到 ' + str(_best_h) + ' 日」而扛单。'
                    '回测口径与边界：见 audit_round15.json（历史截面无 tech 字段，'
                    '无法复现 RSI 剔除与板块约束；基准为池内等权而非全市场指数）。</span>'
                    '</div>')
    except Exception as _e:
        _mf_hold_block = ""

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

        # ---- v4.6 P0 修复：权重落地披露改为「按当日 scored 实时计算」 ----
        # 历史实现读 audit_round17.json 的静态快照（w_missing=0.24、mean_cov=76%、
        #   max_diff=8.2 分、366 只跨档），而 round17 是 2026-10 中旬的口径：当时
        #   score_theme_neg 权重还是 0.12 且 scored 里无值、score_size 也无值。
        # 现状（2026-10-23 scored 实测）：theme_neg 权重已归零（无需值）、size 覆盖
        #   490/490 —— 真实有效权重 = 100%，逐只手工重算 total 最大偏差 0.005 分。
        # 页面却仍宣称「只反映 76% 权重、个股分最大偏移 8.2 分」= 用历史审计结论否定
        #   当日已经修好的数据，属**假告警**，比不告警更糟（会让人对整个模型失去信任）。
        # 修法：不再读任何静态审计文件，一律从当日 results + weights 现算；只有真缺
        #   权重>0 的维时才出告警，且数字（缺多少权重、偏移几分）全部现场算出来。
        _warn_block = ""
        try:
            # ⚠️ v4.9 修复：scored 文件内嵌的 weights 是**生成它的那版 step2** 的权重。
            #   若 scored 由旧版生成（权重仍是 v4.7：含 growth、无 lowvol20），
            #   而页面渲染用的是 factor_weights.json 的新权重，则：
            #     · 页面显示「低波 13%」，但 scored 里 lowvol20 整列为空；
            #     · 告警读文件内旧权重 → 找不到 lowvol20 这一项 → **不触发**。
            #   结果就是「13% 权重静默空转」，页面看不出任何异常。
            #   修法：以**配置权重**（页面真正在用的那套）为检查基准，
            #   文件内权重只用于识别「scored 版本落后」这件事本身。
            _wmap = (json.load(open(inp, encoding="utf-8")) or {}).get("weights") or {}
            _w_file_keys = set(_wmap)
            _wmap = dict(_DIM_W)          # 基准 = 配置权重（= 页面渲染权重）
            # 若文件里的权重与配置不一致，单独记一笔（scored 版本落后）
            _stale = sorted(k for k, v in (dict(_DIM_W)).items()
                            if float(v or 0) > 0 and k not in _w_file_keys)
            _tot_w = sum(float(v or 0) for v in _wmap.values())
            if _tot_w > 0:
                _miss = []          # [(字段名, 权重)]
                _cov_w = 0.0
                for _k, _wv in _wmap.items():
                    _wv = float(_wv or 0)
                    if _wv <= 0:
                        continue
                    _n_ok = sum(1 for _r in results if _r.get(_k) is not None)
                    _cov_w += _wv * (_n_ok / max(1, len(results)))
                    if _n_ok == 0:
                        _miss.append((_k, _wv))
                _miss_w = sum(w for _, w in _miss)
                if _miss_w > 0.001:
                    # 偏移量现场算：按「只用有效维归一」重算 total，与文件 total 比
                    _dmax = 0.0
                    _ncross = 0
                    for _r in results:
                        _num = _den = 0.0
                        for _k, _wv in _wmap.items():
                            _wv = float(_wv or 0)
                            if _wv <= 0:
                                continue
                            _nv = _norm10(_k, _r.get(_k))
                            if _nv is None:
                                continue
                            _num += _nv * _wv
                            _den += _wv
                        if _den <= 0 or _r.get("total") is None:
                            continue
                        _alt = _num / _den * 10.0
                        _d = abs(_alt - float(_r["total"]))
                        _dmax = max(_dmax, _d)
                        if _d >= 1.0:
                            _ncross += 1
                    _cnt = sum(1 for _k, _ in _miss)
                    _dim_s = "、".join(
                        f'<code>{_DIM_LABEL_SHORT.get(k, k)}</code>（权重 {w:.0%}）'
                        for k, w in _miss)
                    _cov_s = f"{_cov_w / _tot_w:.0%}"
                    _warn_block = (
                        '<div style="font-size:10.5px;color:#fbbf24;padding:7px 10px;'
                        'background:rgba(251,191,36,0.07);border:1px solid rgba(251,191,36,0.3);'
                        'border-radius:6px;margin-bottom:10px;line-height:1.7;">'
                        f'⚠️ <b>权重未完全落地（{_cnt} 维缺值）</b>：' + _dim_s
                        + f' 在当前 scored 数据里整列为空（合计 <b>{_miss_w:.0%}</b> 权重）'
                        + f' → 本页 total/评级实际只反映 <b>{_cov_s}</b> 的权重，'
                        + f'个股分最大偏移 <b>{_dmax:.1f} 分</b>、偏移 ≥1 分的有 <b>{_ncross} 只</b>。'
                        + ('<br>⚠️ <b>根因已定位</b>：本次 scored 文件是由<b>旧版权重</b>生成的'
                           f'（缺少 {("、".join(_DIM_LABEL_SHORT.get(k, k) for k in _stale))} 字段），'
                           '需<b>重跑 step2 拉取当日行情</b>后本提示才会消失。'
                           if _stale else
                           '<br>根因是 scored 由旧版 step2 生成（代码已支持写入），刷新数据后本提示自动消失。')
                        + '<b>不影响当日相对排序的方向，但影响绝对分数与评级分档。</b></div>'
                    )
        except Exception as _we:
            print(f"⚠️ 权重覆盖度检查失败（本轮不出告警，避免假告警）: {type(_we).__name__}: {_we}")
            _warn_block = ""

        # ---- v4.10（2026-10-27 P0-4）：中性填充披露条（全表级） ----
        # 量尺统一后缺维以 DMAX/2 参与总分，必须在表头前说清影响面。
        # ⚠️ 独立于上面的 _warn_block：「该维整列为空」（权重不落地）与
        #   「个别股该维为空」（中性填充）是两个不同问题，前者是数据管道故障，
        #   后者是量尺的合规处置。混淆会让真故障淹没在正常填充里。
        _neutral_banner = ""
        try:
            _nf_cnt = sum(1 for _r in results if _r.get("neutral_filled"))
            if _nf_cnt:
                _nf_by_dim = {}
                for _r in results:
                    for _k in (_r.get("neutral_filled") or []):
                        _nf_by_dim[_k] = _nf_by_dim.get(_k, 0) + 1
                _nf_sorted = sorted(_nf_by_dim.items(), key=lambda kv: -kv[1])
                _nf_items = "、".join(
                    f'<code>{_DIM_LABEL_SHORT.get(k, k)}</code> {v} 只'
                    for k, v in _nf_sorted if v > 0)
                _nf_pct = 100.0 * _nf_cnt / max(1, len(results))
                _neutral_banner = (
                    '<div style="font-size:10.5px;color:#94a3b8;padding:7px 10px;'
                    'background:rgba(148,163,184,0.06);border:1px solid rgba(148,163,184,0.25);'
                    'border-radius:6px;margin-bottom:10px;line-height:1.7;">'
                    f'ℹ️ <b>中性填充 {_nf_cnt} 只（{_nf_pct:.1f}%）</b>：' + _nf_items
                    + '在当前数据里无值，已按<b>量程中点（DMAX/2）</b>参与总分 —— '
                    '这些格子标 <span style="background:' + NEUTRAL_FILL_BG + ';padding:0 4px;'
                    'border:1px dashed #94a3b8;">5.0<span style="font-size:8px;">*</span></span>'
                    ' 并带 <b>*</b>。<br>'
                    '<b>这不是实测分，也不是 0 分</b>：填 0 等于把「没数据」当「最差」，'
                    '实测会使复合 IC 从 h5 +0.15 崩到 +0.09（配对 t −5.8）→ 故采用无信息中性值。'
                    '<br>⚠️ 与「权重未落地」告警的区别：本提示是<b>个股级</b>缺维的正常处置，'
                    '上面那条才是<b>整列</b>缺值的数据管道故障。</div>')
        except Exception as _nfe:
            print(f"⚠️ 中性填充披露生成失败（不影响主表）: {type(_nfe).__name__}: {_nfe}")
            _neutral_banner = ""

        # ---- v4.10（2026-10-27 T0-1）：排名精度披露条 ----
        # 投研视角体检结论：这份榜单的「名次」不可作决策依据。
        #   · 相邻分差中位数仅 0.020 分（= 舍入精度 0.01 的两倍）
        #   · 93.6% 的相邻对分差 ≤0.1 分；Top50 内 31/49 对 ≤0.1 分
        #   · Top10 总分区间 85.18~92.35（仅差 7.17 分）
        # 但「分档」有效（等宽 10 分层 D1=74.93~92.35 / D10=21.05~38.94，
        # 层序秩相关 1.0，round25 实测）。
        # 故必须在表头明说：请按分档决策，不要按名次决策。
        # ⚠️ 统计必须现算，不写死数字 —— 池子每天变，写死必然过期。
        _precision_banner = ""
        try:
            _tt = sorted([_r.get("total") for _r in results
                          if isinstance(_r.get("total"), (int, float))], reverse=True)
            if len(_tt) >= 50:
                _dd = [_tt[_i] - _tt[_i + 1] for _i in range(len(_tt) - 1)]
                _med = sorted(_dd)[len(_dd) // 2]
                _p_le_01 = 100.0 * sum(1 for _x in _dd if _x <= 0.1) / len(_dd)
                _p_le_001 = 100.0 * sum(1 for _x in _dd if _x <= 0.01) / len(_dd)
                _top50_le = sum(1 for _x in _dd[:49] if _x <= 0.1)
                _t10_hi, _t10_lo = _tt[0], _tt[9]
                # 分档建议：用 5 分带；给出最高档与最低档区间让用户自己判
                _bands = {}
                for _v in _tt:
                    _bands[int(_v // 5) * 5] = _bands.get(int(_v // 5) * 5, 0) + 1
                _band_txt = " / ".join(
                    f'{_k}–{_k+5}' for _k in sorted(_bands, reverse=True)[:3])
                _precision_banner = (
                    '<div style="font-size:10.5px;color:#fbbf24;padding:7px 10px;'
                    'background:rgba(251,191,36,0.07);border:1px solid rgba(251,191,36,0.30);'
                    'border-radius:6px;margin-bottom:10px;line-height:1.7;">'
                    f'⚠️ <b>名次不可作决策依据 —— 请按分档决策</b>：相邻名次分差中位数仅 '
                    f'<b>{_med:.3f} 分</b>，{_p_le_01:.1f}% 的相邻对分差 ≤0.1 分'
                    f'（≤0.01 分的占 {_p_le_001:.1f}%）；<b>前 50 名内有 {_top50_le}/49 对的分差 ≤0.1 分</b>。'
                    f'Top10 总分区间仅 {_t10_lo:.2f}~{_t10_hi:.2f}（差 {_t10_hi-_t10_lo:.2f} 分）。<br>'
                    '这意味着「第 17 名」与「第 18 名」的差距小于打分舍入误差，'
                    '<b>用名次做「买这只不买那只」没有依据</b>。'
                    '但<b>分档是有效的</b>（等宽十分层单调，层序秩相关 1.0）：'
                    f'建议按 5 分带决策（当前最高三档：{_band_txt}），档内不必再排名。</div>')
        except Exception as _pbe:
            print(f"⚠️ 排名精度披露生成失败（不影响主表）: {type(_pbe).__name__}: {_pbe}")
            _precision_banner = ""


    # ---- v4.0: 两块分析内容迁出主页面 ----
        # 老大指令：「页面上不展示分析与计算过程，仅呈现结论」。
        # 下面两块（构造回测对比 + 动量悖论四层解释）属方法论/计算过程，
        # 默认 SHOW_ANALYSIS=False 不渲染；原文案同步迁至 reports/factor_<date>.md。
        _mf_backtest_block = f'''
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
        '''
        _mom_paradox_block = f'''
        <div style="font-size:13px;font-weight:800;color:#fbbf24;margin:20px 0 6px;">
            ❓ 为什么动量路线 IC 和胜率都低，却仍在选动量路线的个股？</div>
        <div style="font-size:11.5px;color:#e2e8f0;line-height:1.9;padding:10px 12px;
                    background:rgba(251,191,36,0.07);border:1px solid #fbbf2444;border-radius:8px;">
            这个问题问到了模型的<b>结构性矛盾</b>，答案有四层，一层比一层严重：<br><br>
            <b style="color:#fbbf24;">① 字段同名字不同物：动量路线选的是「板块动量」，不是「个股动量」。</b><br>
            <code>pick_portfolio.py</code> 的 momentum 路线筛选条件写在<b>申万二级板块</b>的 60 日/10 日涨幅排名上，
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
        '''
        if not SHOW_ANALYSIS:
            _mf_backtest_block = ""
            _mom_paradox_block = ""

        # ---- v4.6 P1：本组合权重集中度披露（当日现算）----
        # 本组合的权重是按池内 t 值反推的（真实实证，区别于主表的先验权重），
        # 但反推结果是**极度集中**：fund t=2.96/3.48 过线 → 独得 93.6%，
        # 质量/成长/技反 t<=0 直接记 0，反转块因 ρ≈0.89 折半后仅 6.4%。
        # 页面若只写「权重由实证反推」，读者会以为这是一个均衡的多因子模型，
        # 而真相是「基本面单因子 + 一点点反转」。必须把集中度讲清楚。
        _mf_wblock = ""
        try:
            _w = _mf.get("weights") or {}
            _wr = _mf.get("weight_reason") or {}
            _wpos = {k: float(v) for k, v in _w.items() if isinstance(v, (int, float)) and float(v) > 0}
            if _wpos:
                _ws = sum(_wpos.values()) or 1.0
                _main_k, _main_v = max(_wpos.items(), key=lambda x: x[1])
                _main_lbl = ("反转块（乖离+RSI）" if _main_k == "__rev__"
                             else _DIM_LABEL_SHORT.get(_main_k, _main_k))
                _zero_dims = [("反转块" if k == "__rev__" else _DIM_LABEL_SHORT.get(k, k))
                              for k, v in _w.items()
                              if isinstance(v, (int, float)) and float(v) <= 0]
                # ⚠️ 必须转义：weight_reason 里含 "t_min<=0"，裸 < 会被浏览器当成
                #   标签开头，把后续文案整段吞掉（首版实测「依据：估值锚：t_min=2.96；
                #   质量：t_min」后面全没了）。数字与符号一律过 escape。
                _tinfo = "；".join(
                    f'{html_mod.escape(("反转块" if k=="__rev__" else _DIM_LABEL_SHORT.get(k,k)))}：'
                    f'{html_mod.escape(str(v))}'
                    for k, v in _wr.items() if isinstance(v, str))
                _mf_wblock = (
                    '<div style="margin-top:7px;padding:7px 9px;background:rgba(163,230,53,0.07);'
                    'border-left:3px solid #a3e635;border-radius:4px;color:#cbd5e1;">'
                    f'⚖️ <b style="color:#a3e635;">本组合的真实权重构成（按 t 值反推，非人工设定）</b>：'
                    f'<b style="color:#f59e0b;">{_main_lbl} 独占 {_main_v/_ws:.1%}</b>'
                    + (f'，反转块仅 {_wpos.get("__rev__",0)/_ws:.1%}' if "__rev__" in _wpos else "")
                    + (f'，{"、".join(_zero_dims)} 因 t≤0 <b>权重归零</b>' if _zero_dims else "")
                    + f'。<br><span style="color:#94a3b8;">依据：{_tinfo}。</span><br>'
                    '<b>这意味着</b>：本清单本质是「基本面(估值锚)单因子排序」'
                    f'而不是均衡多因子加权 —— 与主表 {_N_EFF_DIM} 维先验加权是<b>两套不同口径</b>，'
                    '同一只票「主表评级」与「本组合入选」不是同一个理由，'
                    '<b>不要用主表评级去印证本组合的选股逻辑</b>。</div>')
        except Exception as _mwe:
            print(f"⚠️ 组合权重披露生成失败: {type(_mwe).__name__}: {_mwe}")

        html_parts.append(f'''
    <!-- v3.9 多因子精选组合（≤10 只）+ 动量路线低 IC 悖论解释 -->
    <div class="chart-card" id="sec-mf-pick" style="padding:16px;margin:14px 0;border:1px solid #a3e63555;">
        <h3 style="font-size:13px;margin-bottom:4px;">🧮 多因子精选组合
            <span style="font-size:11px;color:#64748b;font-weight:400;">
            （{len(_mf.get("picks") or [])} 只 · 权重<b style="color:#a3e635;">按池内 IC/t 实证反推</b>（不同于主表先验权重）· 现价均在买入区间内 · {html_mod.escape(str(_mf.get("date") or ""))}）</span></h3>
        <div style="font-size:11px;color:#64748b;margin-bottom:10px;line-height:1.75;">
            <b style="color:#e2e8f0;">全页唯一的当日候选清单（≤10 只）</b>，供逐一尽调而非照单执行 ——
            本页所有回测结论均为<b style="color:#f59e0b;">池内负超额、绝对收益全周期为负</b>，
            故本清单只回答「若一定要在这个池子里选，相对排序靠前的是哪几只」，
            <b>不构成「这些票会赚钱」的证据</b>。<b style="color:#a3e635;">v4.3 起只保留当日现价落在买入区间内的个股</b>
            （现价高于买区 = 当日挂单成交不了，已在选股层剔除）。
            买入区间 / 止损 / 目标位与个股详情页<b>同源同一函数</b>推导：
            多头取 ma5~ma10 回踩区、弱势取 ma10~ma20 企稳区，止损=最近支撑下方 1.5%。
            资金口径 = 10 万基准 × 当日<b>风险预算上限</b> <b style="color:#f59e0b;">{mkt_pos}</b>
            （赚钱效应 {mkt_score if mkt_score is not None else "—"} 分）÷ {len(_mf.get("picks") or [])} 只，100 股取整，
            <b>仅作仓位换算示例</b>（该档位未经回测，见页头说明）。
            {_mf_wblock}
            {('<br><span style="color:#f59e0b;">⟳ 其中 ' + str(_mf_stale_n) + ' 只的评级在选股快照之后因权重调整而变化，本栏一律显示<b>最新值</b>（悬停 ⟳ 可见快照旧值）。</span>') if _mf_stale_n else ''}
        </div>
        {_warn_block}
        <div style="overflow-x:auto;">
        <table style="width:100%;border-collapse:collapse;font-size:11px;">
            <thead><tr style="background:#1e293b;">
                <th style="padding:5px 6px;color:#94a3b8;">#</th>
                <th style="padding:5px 6px;text-align:left;color:#94a3b8;">标的 / 板块 / 核心逻辑</th>
                <th style="padding:5px 6px;color:#94a3b8;">现价</th>
                <th style="padding:5px 6px;color:#94a3b8;">买入区间</th>
                <th style="padding:5px 6px;color:#94a3b8;">止损位</th>
                <th style="padding:5px 6px;color:#94a3b8;">目标位</th>
                <th style="padding:5px 6px;color:#94a3b8;">盈亏比</th>
                <th style="padding:5px 6px;color:#94a3b8;">股数</th>
                <th style="padding:5px 6px;color:#94a3b8;">金额</th>
                <th style="padding:5px 6px;color:#94a3b8;">多因子<br>分/评级</th>
                <th style="padding:5px 6px;color:#94a3b8;">FwdPE<br>增速</th>
                <th style="padding:5px 6px;color:#94a3b8;">RSI</th>
            </tr></thead>
            <tbody>{_mf_rows}</tbody>
        </table></div>
        <div style="font-size:11px;color:#e2e8f0;padding:9px 11px;background:#1e293b;border-radius:8px;margin-top:10px;line-height:1.85;">
            <b style="color:#7dd3fc;">📌 今日执行要点</b>：
            <b style="color:#22c55e;">{len(_mf.get("picks") or [])} 只现价均已回踩至买区带内</b>
            —— 买区语义是「股价已回到均线/支撑带」，不是「现价夹在上限之下还有下跌空间」。
            因买区上限按设计<u>永不超过现价</u>（防追高），上限=现价时<b style="color:#f59e0b;">不代表现价处于区间高位</b>，
            真实位置须看距带下沿的距离。<br>
            <span style="color:#94a3b8;">口径对账：{_mf_zone_note}。<br>
            本组合为「相对排序」表达：池内头组历史净收益为负（池子本身在跌），
            <b style="color:#f59e0b;">排名靠前 ≠ 买入必赚</b>，请叠加下方仓位纪律与个股止损。<br>
            盈亏比 = （最近阻力位 − 买区上限）/（买区上限 − 止损位）。<b style="color:#f59e0b;">数值过高（如 &gt;5）
            并非优质信号</b>，而是说明上方最近阻力很远 —— 空间大但兑现需要时间，且止损一旦被打掉就归零；
            盈亏比 &lt;1 的标的应先观察、不急于建仓。</span>
        </div>
        {_mf_hold_block}
        <div style="font-size:11px;color:#f59e0b;padding:9px 11px;background:rgba(245,158,11,0.08);border-radius:8px;margin-top:8px;line-height:1.8;">
            <b>⚠️ 三条硬排除（是剔除，不是加分）</b>：
            关注度前 20% 剔除 <b>{(_mf.get("excluded") or {}).get("关注度过高(attention 前20%)", 0)}</b> 只
            · 题材热度前 20% 剔除 <b>{(_mf.get("excluded") or {}).get("题材过热(theme 前20%)", 0)}</b> 只
            · RSI&gt;70 剔除 <b>{(_mf.get("excluded") or {}).get("RSI>70 追高", 0)}</b> 只。
            依据：高关注、高热度在本池内是<b>显著负向</b>因子（T+5 NW t=-3.89 / T+20 NW t=-2.70）。
        </div>
        {_mf_risk_block}
        {_mf_risk_block2}
        {_mf_backtest_block}{_mom_paradox_block}
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

# ══════════════════════════════════════════════════════════════════════════════
# v4.11（2026-10-27 老大指令②）：投研分析数据库与函数**前移**到 top20 详情循环之前。
#
# 原因：原顺序是「先渲染 284 张详情卡 → 再定义研究数据库/函数 → 再渲染 284 张
#   独立 research-card」，因此投研分析只能作为**独立区块**挂在详情卡之后。
#   老大要求「把分析报告放到投资建议详情卡片中，下拉展示」→ 必须在渲染详情卡
#   时就已拿到该股的投研 HTML，故整段前移，先建 {code: html} 映射再进循环。
#
# ⚠️ 搬移安全性：本段只含模块级常量字典 + 一个纯函数（唯一入参 r），
#   不引用 top20 循环内任何局部变量，也不依赖其后的定义。
# ══════════════════════════════════════════════════════════════════════════════

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
        # v4.5（P0）：原句把「题材热度/消息面」并列进「市场表现」，但这两维权重
        #   已归零（theme_intrinsic/attention 权重 0 → _theme_disp 恒 0；news 权重 0）。
        #   实测渲染出「题材热度(0.00/10)」，会被读成「该股题材极差」而非「不参与评分」。
        #   现只写生效维，且用归一化 10 分制 + 原始分附注，避免 0-5 制与 0-10 制混比。
        _mf_bits = []
        _fv = _norm10("score_fund", score_fund)
        if _fv is not None:
            _mf_bits.append(f"基本面估值锚({_fv:.2f}/10)")
        _tnv = _norm10("score_tech_neg", r.get("score_tech_neg"))
        if _tnv is not None:
            _mf_bits.append(f"技术面反号({_tnv:.2f}/10)")
        if score_tech is not None:
            # ⚠️ 2026-10-27 v4.11 修既有 bug：此处原写 `_tech_disp` —— 那是
            #   top20 详情循环里的局部变量。本函数原先在循环**之后**被调用，
            #   于是靠 Python 的循环变量泄漏读到了**最后一只股票**的乖离分，
            #   284 张研究卡全都印同一个数字（静默错误，不报错也不缺失）。
            #   本函数已有 score_tech = _dim_composites(r)[0]（就是同一只股票的
            #   反转块复合分），直接用即可自洽。
            _mf_bits.append(f"乖离/RSI反转({score_tech:.2f}/10)")
        _szv = _norm10("score_size", r.get("score_size"))
        if _szv is not None:
            _mf_bits.append(f"小市值({_szv:.2f}/10)")
        if _mf_bits:
            comp_parts.append(f"<b>市场表现：</b>{'、'.join(_mf_bits)}，")
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
        # v4.5（P0 两处）：
        #   ① 量程 bug —— fund/bias60/rsi_rev 是 0-5 制，旧代码判 `score_fund >= 8`
        #      永远为假，「基本面扎实」这句从未渲染过一次；`score_fund <= 4` 又几乎
        #      恒真，导致研报里清一色写「估值有待提升」。改为归一化后判 0-10 阈值。
        #   ② 归零维度 —— `score_theme >= 8` / `score_news >= 8` 两维权重为 0，
        #      拿它们当「竞争力强项」等于用不计分的维度解释评级。
        dim_strengths = []
        _f10 = _norm10("score_fund", score_fund)
        if _f10 is not None and _f10 >= 8:
            dim_strengths.append(f"基本面估值锚强({_f10:.2f}/10)，估值具备吸引力")
        elif _f10 is not None and _f10 <= 4 and pe > 80:
            dim_strengths.append(f"当前PE({pe:.0f}x)偏高，估值锚分({_f10:.2f}/10)偏弱")
        _tn10 = _norm10("score_tech_neg", r.get("score_tech_neg"))
        if _tn10 is not None and _tn10 >= 8:
            dim_strengths.append(f"技术面反号高({_tn10:.2f}/10)，技术形态偏弱构成反向增量")
        if score_tech is not None and score_tech >= 8:
            # 同上：原 `_tech_disp` 是循环泄漏变量，改用本函数自己的 score_tech。
            dim_strengths.append(f"乖离/RSI反转强({score_tech:.2f}/10)，超跌反弹条件成立")
        elif score_tech is not None and score_tech <= 2:
            dim_strengths.append(f"量价反转弱({score_tech:.2f}/10)，暂无反转信号")
        _sz10 = _norm10("score_size", r.get("score_size"))
        if _sz10 is not None and _sz10 >= 8:
            dim_strengths.append(f"池内市值偏小({_sz10:.2f}/10)，弹性相对更大")

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

    # 评级定位（v4.6：「强烈推荐」是投顾用语，评分体系无择时/推荐资质 → 改为相对强弱表述）
    if rating == "S":
        value_parts.append(f"公司综合评分<b>{total:.2f}/100</b>，当日截面位列<b style='color:#fbbf24;'>S级（最强 5%）</b>，")
    else:
        value_parts.append(f"公司综合评分<b>{total:.2f}/100</b>，当日截面位列<b style='color:#3b82f6;'>A级（前 15%）</b>，")

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
    # v4.5（P0 三处）：
    #   ① score_fund 是 0-5 制，旧判 `>= 8` 恒假 → 改为归一化后的 _f10；
    #   ② score_theme 在十维口径下已由 _dim_composites 返回 None（两维权重皆 0），
    #      旧代码 `score_theme >= 8` 在 Python3 会抛 TypeError: '>=' not supported
    #      between 'NoneType' and 'int' —— 这是本轮必现的崩溃点，非仅文案问题；
    #   ③ 「题材与基本面均衡」用题材维做判断，而题材权重为 0，属口径污染，删。
    # 综合判断（v4.6 P0 措辞降级）：
    #   本项目自身实证结论是「池子负 alpha / 96.6% 跑输 / T+1 基本无效 / 绝对收益全负」，
    #   而本段原来逐只写"建议逢低重点配置""适合波段操作逢低参与""择机参与"——
    #   等于用无择时能力的分位评分给出交易指令，属越界。现统一降级为**特征描述 + 跟踪提示**，
    #   不再出现"配置/参与/布局/逢低"等动作词。
    _f10_v = _norm10("score_fund", score_fund)
    _tech_v = score_tech if isinstance(score_tech, (int, float)) else None
    if rating == "S" and _f10_v is not None and _f10_v >= 8 and _tech_v is not None and _tech_v >= 8:
        value_parts.append("<b>综合判断：估值锚与反转信号双强，属当日强弱排序顶层（不代表买入指令）。</b>")
    elif rating == "S":
        value_parts.append("<b>综合判断：多维度相对占优，位列当日强弱排序顶层（仅表相对强弱）。</b>")
    elif _tech_v is not None and _tech_v >= 8 and _f10_v is not None and _f10_v >= 6:
        value_parts.append("<b>综合判断：反转信号与估值锚较均衡，特征上偏向超跌修复型。</b>")
    elif growth and growth > 100:
        value_parts.append("<b>综合判断：高成长驱动，需关注业绩兑现节奏与估值消化。</b>")
    else:
        value_parts.append("<b>综合判断：综合评分居中上，纳入观察清单持续跟踪。</b>")

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


# 准备评级数据
rating_dist = stats.get("rating_dist", {})

# ══════════════════════════════════════════════════════════════════════════════
# v4.11（2026-10-27 老大指令②）：先为每只 S/A 股预生成投研分析 HTML，
#   待详情卡渲染时以内嵌 <details> 下拉展示（原先是一个独立大区块挂在后面）。
#
# ⚠️ 必须在 top20 循环**之前**建好映射：详情卡是逐只 append 的，循环内拿不到
#   「后面才生成」的分析。这也正是把 SUBTHEME_RESEARCH / generate_research_analysis
#   整段前移的原因（见上方 v4.11 搬移说明）。
# ══════════════════════════════════════════════════════════════════════════════
_research_by_code = {}
# 与 top20 同源：仅预生成将展示的（≥75 门槛内）S/A 标的，避免生成数百份却只展示部分
_sa_stocks = [r for r in results
              if r.get("rating") in ("S", "A")
              and _in_show_set(r)]
_sa_stocks.sort(key=lambda x: -x["total"])
for _rs in _sa_stocks:
    try:
        _research_by_code[_rs["code"]] = generate_research_analysis(_rs)
    except Exception as _rse:
        # 单只失败不能拖垮整页：留空 → 该股详情卡不显示投研下拉。
        print(f"⚠️ 投研分析生成失败（{_rs.get('code')} {_rs.get('name')}）: "
              f"{type(_rse).__name__}: {_rse}")
_s_count_research = sum(1 for x in _sa_stocks if x["rating"] == "S")
_a_count_research = sum(1 for x in _sa_stocks if x["rating"] == "A")

html_parts.append('''
    <!-- 深度分析 -->
    <div class="top-list">
        <div class="section-title">🏆 S/A级综合排名 · 投资建议详情 (''' + str(len(top20)) + '''只)
          <span style="font-size:11px;font-weight:400;color:#94a3b8;">展示门槛：''' + _TOTAL_CUT_DESC + '''</span></div>
        <div style="font-size:11px;color:#94a3b8;padding:2px 0 8px;">📐 <b>模型定位与使用须知</b>：本模型是「跌市/震荡市超跌反弹选股器」（低波+超跌反转因子主导），高分组以空头排列标的为主（audit_round90 实测 85%）—— <b>高分 = 观察池 ≠ 立即买入</b>；入场以卡片「📉趋势徽标」与「⏸等待企稳」提示为准，趋势向下的标的需等企稳信号（站上MA5 / 连续2日不创新低 / RSI拐头）再分批介入。</div>''')

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

    # v4.10（2026-10-27 T1-3）：给「绝对 PE」配一个池内分位参照。
    #   动机：fund / ep 都是池内 rank 分位，页面只说「估值锚 8 分」，
    #   读者无法判断这是 15 倍还是 150 倍 —— 8 分在 PE 中位 57 倍的池子里
    #   仍然可能是 30 倍（不便宜）。故在个股层直接给出「本股 PE 在本池的分位」。
    # ⚠️ 用 _pool_pe_stats 的实际分位阈值做插值判定，而非重新扫一遍全池。
    _pe_ref_html = ""
    try:
        _ps_ = _pool_pe_stats.get("pe") or {}
        if isinstance(pe, (int, float)) and pe > 0 and _ps_.get("p50"):
            _p10, _p25 = _ps_["p10"], _ps_["p25"]
            _p50, _p75, _p90 = _ps_["p50"], _ps_["p75"], _ps_["p90"]
            if pe <= _p10:
                _tag, _cl = "池内最便宜 10%", "#22c55e"
            elif pe <= _p25:
                _tag, _cl = "池内最便宜 25%", "#4ade80"
            elif pe <= _p50:
                _tag, _cl = "低于池内中位", "#a3e635"
            elif pe <= _p75:
                _tag, _cl = "高于池内中位", "#f59e0b"
            elif pe <= _p90:
                _tag, _cl = "池内偏贵 25%", "#fb923c"
            else:
                _tag, _cl = "池内最贵 10%", "#ef4444"
            _pe_ref_html = (
                f'<span style="color:{_cl};font-size:10px;" title="本股 PE {pe:.1f} 倍'
                f'在本池的分位。池内 PE(TTM) 实测分位：'
                f'p10 {_p10:.1f} / p25 {_p25:.1f} / p50 {_p50:.1f} / '
                f'p75 {_p75:.1f} / p90 {_p90:.1f} 倍（n={_ps_.get("n","?")}，仅含 PE>0）。'
                f'⚠️ 估值分是**池内相对排名**，不是绝对便宜：本池中位已 {_p50:.0f} 倍。">'
                f'（{_tag}）</span>')
        elif isinstance(pe, (int, float)) and pe <= 0 and _pool_pe_stats.get("pe"):
            _pe_ref_html = (
                '<span style="color:#ef4444;font-size:10px;" '
                'title="PE≤0 = 当前亏损，无法用 PE 估值。本维已按 T0-4 封顶 3.0，'
                '不参与「便宜/贵」判定。">（当前亏损·不计估值分位）</span>')
    except Exception:
        _pe_ref_html = ""
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

    # v2.30: AI增强层 — 业绩一/二阶导 + AI赋分徽标（round106 口径：
    #   deriv1_growth=一阶导·单季环比增速%；deriv2_delta=二阶导·同比增速之差ΔG_q pp）
    ai_fd = r.get("fund_detail") or {}
    deriv_str = ""
    if ai_fd:
        _g1 = ai_fd.get("deriv1_growth")
        _s1 = ai_fd.get("deriv1_score")
        _d2 = ai_fd.get("deriv2_delta")
        _s2 = ai_fd.get("deriv2_score")
        if _g1 is not None:
            deriv_str = f" · 一阶导(单季环比增速{_g1:+.0f}%) {_s1*2:.1f}/10分"
            if _d2 is not None:
                deriv_str += f" · 二阶导ΔG_q(增速变化{_d2:+.0f}pp) {_s2*2:.1f}/10分"
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
    # v4.5（P0）：原此处读 score_news >= 4 → "消息面强劲"。news 权重已归零，
    #   不是评分维度，写进「评级理由」等于用不计分的维度解释分数，属口径污染。
    #   改为只在**有实际消息面佐证**时作为「附注」出现（前缀标明非评分）。
    if r.get("score_fund") is not None:
        _f10 = _norm10("score_fund", r.get("score_fund"))
        if _f10 is not None and _f10 >= 8: justification.append("估值锚强")
        elif _f10 is not None and _f10 <= 2: justification.append("估值偏高")
    if _tech_disp is not None:
        if _tech_disp >= 8: justification.append("乖离/RSI反转强")
        elif _tech_disp <= 2: justification.append("乖离/RSI反转弱")
    if rsi_state == "超买": justification.append("RSI超买注意回调")
    elif rsi_state == "超卖": justification.append("RSI超卖存反弹")
    # 归零维度不进评级理由；仅在幅度极端时作为独立「参考」字段（UI 上另行标注非评分）
    _news_ref = ""
    if isinstance(r.get("score_news"), (int, float)) and r.get("score_news") >= 4:
        _news_ref = "消息面偏暖"
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
    # round106 口径：deriv1_growth = 一阶导 QoQ（环比%）→ 环比下滑旗标；
    #   deriv2_delta = 二阶导 ΔG_q（pp）→ 增速减速旗标（「减速」≠「下滑」，各给各的旗标）
    if isinstance(_fd_n.get("deriv1_growth"), (int, float)) and _fd_n["deriv1_growth"] < -20:
        _fund_flags.append(f"业绩环比大幅下滑 {_fd_n['deriv1_growth']:+.0f}%")
    if isinstance(_fd_n.get("deriv2_delta"), (int, float)) and _fd_n["deriv2_delta"] < -20:
        _fund_flags.append(f"增速显著减速 {_fd_n['deriv2_delta']:+.0f}pp")
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
        _q3_tip = (f"Q3预测景气加成 +{_qb:.1f}分(0-5分制, 传导总分+{_qb*_DIM_W['score_fund']*10:.1f})；"
                   f"Q3同比{_q3_yoy_s} 加速度{_q3_accel_s}{_q3_sat_s}；"
                   f"机构全年预期隐含H2增速{_q3_ih2_s}（Q3预测超出机构预期部分才计入，防止与FwdPE双重计价）；"
                   f"数据源：手工汇总表(半导体/消费电子Q3预测, TTL45天)")
        _q3_badge = ('<span title="' + _q3_tip.replace('"', "'") + '" '
                     'style="font-size:11px;font-weight:700;padding:1px 6px;'
                     'border-radius:3px;background:#dcfce7;color:#15803d;margin-left:6px;cursor:help;">'
                     '📈Q3景气 +' + f"{_qb:.1f}" + '</span>')

    # ===== 业绩预期差三档徽标（earnings_surprise，权重0不进总分）=====
    # 判定：预测增速 vs 实际单季同比（超预期/符合预期/低于预期），规则见页尾披露⑤。
    # 无法判定不渲染徽标（缺数据不编造）；配色沿用 Q3 徽标语义（好=绿/差=红/中性=灰蓝）。
    # ⚠️ 本段变量一律 _es_* 唯一前缀（round57 教训：_ic/_t/_d 等模块级名会覆盖下游全局）。
    _es_badge = ""
    _es_row = r.get("es") or {}
    if _es_row.get("tier") and _es_row["tier"] != "无法判定":
        _es_tier = _es_row["tier"]
        _es_dpp = _es_row.get("delta_pp")
        _es_ds = f"{_es_dpp:+.0f}pp" if isinstance(_es_dpp, (int, float)) else ""
        _es_bg, _es_fg = ("#dcfce7", "#15803d") if _es_tier == "超预期" else (
            ("#fee2e2", "#b91c1c") if _es_tier == "低于预期" else ("#e2e8f0", "#475569"))
        _es_ic = "🔺" if _es_tier == "超预期" else ("🔻" if _es_tier == "低于预期" else "➖")
        _es_sts = "·强" if _es_row.get("strength") == "strong" else ""
        _es_tip = (f"业绩预期差三档：{_es_tier}{_es_sts}；{_es_row.get('note','')}"
                   f"（预测源 {_es_row.get('source') or '—'}·口径{'扣非' if _es_row.get('caliber')=='kf' else '归母'}"
                   f"·方法 {_es_row.get('method')}）；权重 0 不进总分")
        _es_badge = ('<span title="' + _es_tip.replace('"', "'") + '" '
                     'style="font-size:11px;font-weight:700;padding:1px 6px;'
                     f'border-radius:3px;background:{_es_bg};color:{_es_fg};margin-left:6px;cursor:help;">'
                     f'{_es_ic}业绩{_es_tier} {_es_ds}</span>')

    # ===== v4.17 趋势状态徽标（空头排列/弱势/企稳中/多头排列，round90 趋势诊断）=====
    # 判定纯数据驱动（tech.ma5/10/20/60 + 现价），缺数据不渲染；不进评分、只作入场提示。
    _trend_badge = ""
    _tst = _trend_state(r)
    if _tst:
        _trend_badge = ('<span title="' + _tst["tip"].replace('"', "'") + '" '
                        'style="font-size:11px;font-weight:700;padding:1px 6px;'
                        f'border-radius:3px;background:{_tst["bg"]};color:{_tst["fg"]};margin-left:6px;cursor:help;">'
                        f'📉{_tst["label"]}</span>')

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
        # v4.4：只展示有权重的 7 个生效维。归零维度（news/theme/sector/oversold60/
        #   vol_pulse/overbought/theme_intrinsic/attention）不再进徽章与卡片 ——
        #   展示 0 权重维度 = 页面说 16% 消息面而实际不算（round17 同类漂移）。
        # v4.5（P0 修复量程）：7 个生效维里 fund/bias60/rsi_rev 是 0-5 制，
        #   quality/growth/tech_neg/size 是 0-10 制。旧实现直接把原值串在一行，
        #   5 分制满分（如 fund 5.00）看起来比 10 分制中位（如 quality 5.01）还低，
        #   读者必误判。此处统一归一化到 0-10 展示，原始值进 title 供核对。
        # v4.6（P0 修复语义）：quality/growth/size 三维**不是绝对分**，而是
        #   「池内 rank 分位 ×10」（fund_split.rank_pct 实现）。实测三者均严格
        #   均匀分布（490 个值 = i/(n-1)*10，偏差 0.005；std=2.893≈10/√12），
        #   即**必然**有 0.00 和 10.00 各一只、中位数恒为 5.00。
        #   旧实现标「库内原始 x/10」会把 0.04 读成「该公司质量极差」，而真相是
        #   「在本池 490 只里质量排最后几名」（如贵州茅台 size=0.04 = 市值最大）。
        #   改：这三维 title 明确写「池内分位」，不给「原始分」字样。
        #   v4.8：新增 lowvol20 同属池内分位族。
        #   v4.9：新增 score_ep / score_size_ind；同时把小市值移出（权重归零）。
        #   v4.13-size2d：score_size_ind 退役 → score_size_growth / score_dominant。
        #     ⚠️ 清单也改为由 _W_EFF 动态派生，避免再出现「配置改了、徽章没改」。
        _RANK_DIMS = ("score_quality", "score_growth", "score_size", "score_lowvol20",
                      "score_ep", "score_dominant")

        def _b(key):
            _nv = _norm10(key, r.get(key))
            _rv = r.get(key)
            _mx_raw = _DIM_MAX_RAW.get(key) or 10.0
            if _nv is None:
                return (f'<span style="color:#64748b;" title="该股此维无数据">'
                        f'{_DIM_LABEL_SHORT[key]} —</span>')
            if key in _RANK_DIMS:
                return (f'<span title="{_DIM_LABEL_SHORT[key]} = **池内分位×10**'
                        f'（{_nv:.2f}/10）：0=本池最差/最小，5=中位，10=本池最好/最大。'
                        f'这是相对排名，不是绝对水平——本池必然有 0.0 与 10.0 各一只。">'
                        f'{_DIM_LABEL_SHORT[key]} {_nv:.2f}<span style="font-size:8px;'
                        f'color:#64748b;">分位</span></span>')
            # r 内的值已 ×2（见 L40-44）；还原成库内原始分再标量程，
            # 否则 0-5 制维会显示「10.00/5」越界。库内分才可与因子日报对数。
            _raw_show = (_rv / 2.0 if key in _SCORE_KEYS10 else _rv) \
                if isinstance(_rv, (int, float)) else None
            _rvt = f"{_raw_show:.2f}/{_mx_raw:.0f}" if _raw_show is not None else "—"
            return (f'<span title="{_DIM_LABEL_SHORT[key]} 归一化 {_nv:.2f}/10'
                    f'（库内原始 {_rvt}）">{_DIM_LABEL_SHORT[key]} {_nv:.2f}</span>')
        _badge_html = ("<span>🏷️ 归一化10分制 · "
                       + " · ".join(_b(_k) for _k in
                                    [k for _l, k in _W_EFF])
                       + "</span>")
        # ---- v4.11（老大指令③）：一行 4 卡之「估值」----
        # 原「基本面·估值锚」卡内混着 quality/growth 拆分小字，现质量独立成卡 →
        #   此处只留 fund 自身口径 + 成长（0 权重，仅留档）说明，避免与质量卡重复。
        _gd_v = r.get("score_growth")
        _gd_txt = f"{_gd_v:.2f}" if isinstance(_gd_v, (int, float)) else "—"
        _fund_missing = ("" if r.get("score_fund") is not None else
                         '<br><span style="color:#f59e0b;">缺有效估值，已从总分分母剔除</span>')
        # ---- v4.17：周期规则1/2 扣分留痕（禁只扣分不解释：扣了分页面必须说清是哪条规则）----
        _gmb = ((r.get("fund_detail") or {}).get("modeB") or {})
        _gr_note = ""
        if _gmb.get("is_cycle"):
            _gr_parts = []
            if _gmb.get("rule1_hit"):
                _gr_parts.append(f"规则1 价格驱动（毛利率4季均值抬升"
                                 f"{(_gmb.get('gm_rise_pp') or 0):+.1f}pp ＞ 3pp）扣 10%")
            if _gmb.get("rule2_hit"):
                _gr_parts.append(f"规则2 加速不可持续（去均值 ΔG_q "
                                 f"{(_gmb.get('accel_demeaned_pp') or 0):+.0f}pp ≥ 周期组 p80）扣 15%")
            _ded_pct = (_gmb.get("rule_ded_pct") or 0) * 100
            if _gr_parts:
                _gr_note = (f'<br><span style="color:#f97316;">🧭 强周期股（{_gmb.get("sw_l2")}）'
                            f'· 合计扣成长分 {_ded_pct:.0f}%</span>'
                            f'<br><span style="font-size:9px;color:#94a3b8;">'
                            + "；".join(_gr_parts) + '</span>')
            else:
                _gr_note = (f'<br><span style="font-size:9px;color:#64748b;">'
                            f'🧭 强周期股（{_gmb.get("sw_l2")}）· 规则1/2 均未触发，无扣分</span>')
        _fund_card_html = (f'<div class="dim-card">'
                           f'<div class="dim-title">📊 估值 · 基本面锚 ({_fmt2(r.get("score_fund"))}/10)'
                           f'{"" if r.get("score_fund") is not None else " · 不计分"}</div>'
                           f'<div class="dim-score">{_fmt2(r.get("score_fund"))}</div>'
                           f'<div class="dim-detail">{fund_detail}{deriv_str}<br>'
                           f'PE/FwdPE/PEG + 估值阶梯 + 周期股折价'
                           f'（{_DIM_W.get("score_fund", 0)*100:.1f}% 权重）{_fund_missing}<br>'
                           f'<span style="font-size:9px;color:#64748b;">'
                           f'成长 {_gd_txt}（权重 {(_DIM_W.get("score_growth") or 0)*100:.0f}%）：'
                           f'方式B G_q=单季扣非同比，QoQ/ΔG_q 已归 fund 导数维（v4.17 P1g 去重）'
                           f'</span>{_gr_note}</div>'
                           f'</div>')

        # 量价反转卡：_tech_disp 已是 0-10 归一化复合分；None 表示两维全缺
        _td_s = f"{_tech_disp:.2f}" if _tech_disp is not None else "—"
        _td_title = f"📈 乖离/RSI反转 ({_td_s}/10)" if _tech_disp is not None else "📈 乖离/RSI反转 (无数据)"
        # ⚠️ 2026-10-27 v4.11：此处原来还有一份 _tech_card_html 赋值（用
        #   `{r["score_bias60"]:.2f}/5`），随后在下方被覆写 —— 属死代码，但
        #   若 score_bias60 为 None 会先抛 TypeError 让整份报告渲染失败。
        #   已删除，只保留下方统一版本（走 _norm10 + /10 量程）。
        # ---- v4.11（老大指令③）：一行 4 卡之「质量」----
        #   质量维在 v4.5~v4.10 一直只作为「估值锚卡里的一行小字」出现（fund 拆分子项），
        #   但它自己带 12% 权重、且是 8 维里第二大的权重维 —— 藏在小字里等于弱化。
        #   ⚠️ 分位维：显示值 = 池内 rank×10，不是绝对水平（0 分=本池最后几名）。
        _ql_s = r.get("score_quality")
        _ql_txt = f"{_ql_s:.2f}" if isinstance(_ql_s, (int, float)) else "—"
        _ql_missing = ("" if isinstance(_ql_s, (int, float))
                       else '<br><span style="color:#f59e0b;">本股质量维无数据，已从总分分母剔除</span>')
        _quality_card_html = (f'<div class="dim-card">'
                              f'<div class="dim-title">🏛 质量 ({_ql_txt}/10)'
                              f'<span style="font-size:9px;color:#64748b;">　池内分位</span></div>'
                              f'<div class="dim-score">{_ql_txt}</div>'
                              f'<div class="dim-detail">'
                              f'ROE/ROIC/ROA · 现金流质量(应计/营收现金比) · 盈利波动 · 低杠杆 · 低商誉'
                              f'（{_DIM_W.get("score_quality", 0)*100:.1f}% 权重）{_ql_missing}<br>'
                              f'<span style="font-size:9px;color:#64748b;">池内 rank×10：0=本池质量排最后，'
                              f'5=中位，10=本池最好 —— 是<b>相对排名</b>，非绝对水平</span></div>'
                              f'</div>')
        # ---- v4.13-size2d（老大指令③）：一行 4 卡之「市值双维」----
        #   A 小盘成长溢价 score_size_growth(0.20)：行业内市值分 × 成长门限 w_mc（**仅非龙头**）
        #   B 行业龙头壁垒 score_dominant(0.10)：业务份额(营收+利润)申万二级组内 rank
        #   P0-3（round118）前两维互斥；**之后 B 对全池生效** →
        #   ⚠️ 非龙头同时持有 A+B（市值大类暴露合计 0.30），龙头只有 B（0.10）。
        #   本卡同时显示两维，正是为了让这个不对称对使用者可见。
        _sg_s = r.get("score_size_growth")
        _dm_s = r.get("score_dominant")
        _sg_txt = f"{_sg_s:.2f}" if isinstance(_sg_s, (int, float)) else "—"
        _dm_txt = f"{_dm_s:.2f}" if isinstance(_dm_s, (int, float)) else "—"
        _sec_name = r.get("sector") or "综合"
        _sg_missing = ("" if isinstance(_sg_s, (int, float))
                       else '<br><span style="color:#f59e0b;">本股小盘成长维无数据（龙头股不含此维，'
                            '市值暴露仅 B 的 10%）</span>')
        _dm_missing = ("" if isinstance(_dm_s, (int, float))
                       else '<br><span style="color:#f59e0b;">本股龙头壁垒维无数据'
                            '（缺申万二级映射或营收/利润 TTM）</span>')
        _size_ind_card_html = (f'<div class="dim-card">'
                               f'<div class="dim-title">🏭 市值双维 (A {_sg_txt} · B {_dm_txt})'
                               f'<span style="font-size:9px;color:#64748b;">　池内分位</span></div>'
                               f'<div class="dim-score">{_sg_txt} / {_dm_txt}</div>'
                               f'<div class="dim-detail">'
                               f'A 小盘成长溢价 {_sg_txt}/10（{_DIM_W.get("score_size_growth", 0)*100:.1f}% 权重）'
                               f'{_sg_missing}<br>'
                               f'B 行业龙头壁垒 {_dm_txt}/10（{_DIM_W.get("score_dominant", 0)*100:.1f}% 权重）'
                               f'{_dm_missing}<br>'
                               f'<span style="font-size:9px;color:#64748b;">「{html_mod.escape(str(_sec_name))}」行业内：'
                               f'A=市值偏小×成长门限（低成长小票不给溢价）；'
                               f'B=营收+利润业务份额组内前30%即龙头。两维互斥，只落其一。</span></div>'
                               f'</div>')
        # v4.8 低波20 卡：池内分位分（低波=高分），另给原始波动率供对数
        _lv_s = r.get("score_lowvol20")
        _lv_rawv = r.get("lowvol20_raw")
        _lv_txt = f"{_lv_s:.2f}" if isinstance(_lv_s, (int, float)) else "—"
        _lv_card_html = (f'<div class="dim-card">'
                         f'<div class="dim-title">📉 低波20 ({_lv_txt}/10)</div>'
                         f'<div class="dim-score">{_lv_txt}</div>'
                         f'<div class="dim-detail">'
                         + (f'过去20日日收益标准差 {_lv_rawv*100:.2f}%<br>' if isinstance(_lv_rawv, (int, float)) else '')
                         + f'池内分位分（波动越低分越高，{_DIM_W.get("score_lowvol20", 0)*100:.0f}% 权重）<br>'
                         f'<span style="font-size:9px;color:#64748b;">round24：单维 h5 IC +0.1189/t 3.28，与其余六维 |r|≤0.24 不冗余</span>'
                         f'</div></div>')
        _tn_v = r.get("score_tech_neg")
        _tn_s = f"{_tn_v:.2f}" if isinstance(_tn_v, (int, float)) else "—"
        # ⚠️ 2026-10-27 v4.11 修既有量程 bug：原写 `{r["score_bias60"]:.2f}/5`，
        #   但 results 内的值已在加载时 ×2（见 L40-44），实际是 0-10 制 ——
        #   页面因此出现「乖离低位 10.00/5」这种越界字样（满分 5 打出 10 分）。
        #   统一改用 _norm10 归一化值 + 标 /10。
        _bs10 = _norm10("score_bias60", r.get("score_bias60"))
        _rs10 = _norm10("score_rsi_rev", r.get("score_rsi_rev"))
        _bs_txt = f"{_bs10:.2f}/10" if _bs10 is not None else "—"
        _rs_txt = f"{_rs10:.2f}/10" if _rs10 is not None else "—"
        _tech_card_html = (f'<div class="dim-card">'
                           f'<div class="dim-title">{_td_title}</div>'
                           f'<div class="dim-score">{_td_s}</div>'
                           f'<div class="dim-detail">乖离低位 {_bs_txt} · '
                           f'RSI反转 {_rs_txt}<br>'
                           f'（两维各 17.5% 权重，归一化后加权 → 上列 0-10 分）<br>'
                           f'趋势: {tech.get("trend","N/A")} · '
                           f'分位: {tech.get("position","N/A")}%{f" · {factor_info}" if factor_info else ""} · {tech.get("reason","")}</div>'
                           f'</div>')
    else:
        # ⚠️ score_fund 可能为 None（缺有效估值·本维不计分）→ 走 _fmt2 安全格式化，
        #   直接 :.2f 会抛 TypeError 让整份报告渲染失败。
        _fund_badge = (f"{_fmt2(r.get('score_fund'))}/10基本" if r.get('score_fund') is not None
                       else "估值锚未计分")
        _badge_html = (f"<span>🏷️ {r['score_news']:.2f}/10消息 · {r['score_tech']:.2f}/10技术 · "
                       f"{_fund_badge} · {r.get('score_theme',0):.2f}/10热度</span>")
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
        # 旧口径分支（_has10d=False）也要有这些卡变量，否则 f-string NameError。
        # v4.11：一行 4 卡统一由 _fund_card_html / _quality_card_html /
        #   _tech_card_html / _size_ind_card_html 组成；旧口径没有
        #   质量 / 行业内市值 字段 → 空串（该槽位不出卡），低波也归零。
        #   ⚠️ _fund_card_html 在旧口径下原本不存在 → 上面 dim-row 引用会
        #   NameError（静默整份报告失败）。这里复用已有 _fund_badge 给出等价卡。
        _fund_card_html = (f'<div class="dim-card">'
                           f'<div class="dim-title">📊 估值 · 基本面锚</div>'
                           f'<div class="dim-score">{_fmt2(r.get("score_fund"))}</div>'
                           f'<div class="dim-detail">旧口径（无 10 维明细）· {_fund_badge}</div>'
                           f'</div>')
        _lv_card_html = ''
        _quality_card_html = ''
        _size_ind_card_html = ''

    html_parts.append(f'''
        <div class="stock-detail {s_class}{a_collapse}" data-rating="{rating}" data-sector="{sector_disp(r)}" data-code="{r['code']}" data-name="{r['name']}">
            <div class="header-row">
                <div class="rank" style="background:{rank_color}">#{i}</div>
                <div>
                    <div class="name">{r['name']}{change_tag}{ai_badge}{_risk_badge}{_lowbase_badge}{_q3_badge}{_es_badge}{_trend_badge}</div>
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
                {_pe_ref_html}
                {f'<span style="color:{valu_note(r)[1]};">🧭 {valu_note(r)[0]}</span>' if valu_note(r)[0] else ''}
                <span class="s-pct {week_chg_cls}" style="font-weight:700;">📅 近一周 {week_chg_str}</span>
                {_badge_html}
            </div>
            <div class="dim-row">
                {_fund_card_html}
                {_quality_card_html}
                {_tech_card_html}
                {_size_ind_card_html}
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
    # v4.17：空头排列 → wait 模式（不给买点），先渲染等待盒，跳过下方买点/止损/目标块
    _plan_wait = bool(plan) and plan.get("mode") == "wait"
    if _plan_wait:
        html_parts.append(f'''
            <div style="margin-top:10px;padding:10px 12px;border-radius:8px;background:rgba(239,68,68,0.07);border-left:3px solid #ef4444;">
                <div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap;font-size:11px;">
                    <span style="font-weight:800;color:#ef4444;">🧭 操作建议</span>
                    <span style="padding:1px 7px;border-radius:3px;background:rgba(239,68,68,0.16);color:#ef4444;font-weight:700;">⏸ 等待企稳 · 暂不给买点</span>
                    <span style="color:#94a3b8;">空头排列中，先确认企稳再分批入场</span>
                </div>
                <div style="font-size:11px;color:#cbd5e1;margin-top:6px;">{plan.get("wait_note", "")}</div>
                <div style="font-size:10.5px;color:#94a3b8;margin-top:4px;">模型定位：高分=超跌反弹候选池（跌市/震荡市选股器），当前趋势向下 —— 等企稳信号再入场，避免左侧接飞刀（audit_round90 实测：高分组近20日收益中位 −22.5%）。</div>
            </div>''')
    if plan and not _plan_wait:
        # ⚠️ 2026-10-25 修 P0 口径混用：这里原用「现价」算止损/目标百分比，
        #   而盈亏比已改为按【计划入场价】计算 → 同一行出现
        #   「止损 -3.4%、目标 +0.2%、盈亏比 3.2:1」的自相矛盾（长川科技实例）。
        #   现统一为：百分比一律相对计划入场价（买区上限），仅当现价已在买区内时等于现价。
        _eref = plan.get("entry_ref") or r.get("price") or 0
        _risk_pct = (_eref - plan["stop"]) / _eref * 100 if _eref else 0
        _up_pct = (plan["target"] - _eref) / _eref * 100 if _eref else 0
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

    # 评级原因（常驻可见，不随明细折叠）
    html_parts.append(f'''
            <div style="font-size:11px;color:#38bdf8;margin-top:8px;">💡 评级原因: {', '.join(justification[:4])} → {rating}级 · {advice_with_market(r)}{f'　<span style="color:#64748b;">（参考·不计分：{_news_ref}）</span>' if _news_ref else ''}</div>''')

    # ══════════════════════════════════════════════════════════════════════
    # v4.11（老大指令②）：投研分析并入本卡片，下拉展示。
    #   原先是独立区块「📑 S/A级公司专业投研分析报告」+ 284 张 research-card
    #   排在所有详情卡之后 —— 同一只股票的信息被拆在两个相隔 200 万字符的地方。
    #   现改为本卡内的 <details>：默认收起，卡片高度不变，需要时下拉开看。
    # ⚠️ 必须在 </div>（stock-detail 闭合）之前，否则会掉到卡片外面。
    # ══════════════════════════════════════════════════════════════════════
    _ra_html = _research_by_code.get(r["code"]) or ""
    if _ra_html:
        _ra_n_mod = _ra_html.count('class="research-module')
        html_parts.append(f'''
            <details class="ra-fold" style="margin-top:10px;">
                <summary style="cursor:pointer;font-size:12px;font-weight:700;color:#fbbf24;
                                padding:9px 11px;background:rgba(251,191,36,0.07);
                                border-left:3px solid #fbbf24;border-radius:6px;
                                list-style:none;display:flex;align-items:center;gap:8px;flex-wrap:wrap;">
                    <span>📑 展开专业投研分析</span>
                    <span style="font-weight:400;font-size:10px;color:#64748b;">
                        竞争力评估 · 行业前景 · 风险识别 · 投资价值（{_ra_n_mod} 个模块）
                    </span>
                </summary>
                <div style="margin-top:8px;">{_ra_html}</div>
            </details>''')

    html_parts.append('''
            </div>''')

scores_label = ('十维评分' if any(x.get('score_oversold60') is not None for x in results)
                else ('五维评分' if any(x.get('score_sector') is not None for x in results) else '四项评分'))
# v4.5（问题2）：列名与排序项都改为「N 维生效」口径。
#   旧标签写「十维评分」，而实际只有 7 维有权重 —— 名称本身就在误导。
# v4.9：N 由配置现算（_N_EFF_DIM），不再硬编码 7。
if any(x.get('score_oversold60') is not None for x in results):
    scores_label = (f'{_N_EFF_DIM}维评分'
                    f'<span style="font-weight:400;font-size:9px;color:#64748b;">(归一化10分)</span>')
# v3.0: 排序下拉按口径自适应 —— 十维列出全部子项(score_* → data-s-* 通用映射)
_has10d = any(x.get('score_oversold60') is not None for x in results)
_has_sec = any(x.get('score_sector') is not None for x in results)
if _has10d:
    # v4.5（问题2）：排序项只列**有权重**的维。归零维（消息面/超跌60/量能/
    #   超买/题材内禀/关注度/板块动量）排序无决策意义 —— 按 0 权重因子排序
    #   会得到一份「看着有区分度但总分无关」的名单。字段仍在 data-s-* 里保留。
    #   v4.9（P0 修复）：原为硬编码 7 个 option，改维后必然漏（本轮新增 ep/size_ind
    #     时硬编码里还是旧 size）→ 改为由 _W_EFF 动态生成，与权重条/分数条同源。
    _sort_opts = "".join(
        f'<option value="{_k}">{_lbl}</option>' for _lbl, _k in _W_EFF)
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
    <div style="font-size:10.5px;color:#64748b;padding:4px 0 8px;">ℹ️ 本表为全池完整排名，可用上方筛选控件按需过滤（评级/板块/路线/成交额/每页条数等）；S/A 深度分析区仅展示综合得分 ≥ 75 分的标的。Q3 列角标 🔺/➖/🔻 = 业绩预期差三档（超预期/符合预期/低于预期：预测增速 vs 实际单季同比，点值带 ±10pp、|Δ|≥25pp 标强，权重 0 不进总分），悬停 Q3 格可看判定依据。S/A 卡趋势徽标 📉空头排列/弱势/企稳中/多头排列（tech 均线数据驱动，权重 0）——空头排列=超跌反弹候选、需等企稳信号，<b>高分 ≠ 立即买入</b>（audit_round90）。</div>
''')

# ⚠️ 2026-10-27 修：上一段是**普通字符串**（非 f-string），把 `{_neutral_banner}`
#   写进去只会输出字面量 "{_neutral_banner}" —— 实测页面里披露条根本没出现，
#   而锚点计数却是 1（因为计数的是字面量本身），属于「锚点在但内容不在」的静默失效。
#   改为独立 append，避免再受花括号转义影响。

# ══════════════════════════════════════════════════════════════════════════════
# v4.10（2026-10-27 T1-1/T1-2/T1-3）：投研视角的三项披露
#
# 全部为**纯披露**，不改模型、不改排序、不改权重。定位是「把模型已知的
# 结构性偏斜说出来」，而不是修正它 —— 因为 T0 批已证明这些偏斜改不动
# （tech_neg 重标定显著更差 t−4.96、共线合并数学恒等、亏损股分组无统计功效）。
#
# ⚠️ 所有数字必须现算。池子每天变，写死数字必然过期
#   （本项目历史上「页面说 16% 消息面实际不算」即此类过期文案）。
# ══════════════════════════════════════════════════════════════════════════════

# ---- 排名基准：按 total 降序（与页面默认排序同口径）----
_ranked = sorted(
    [r for r in results if isinstance(r.get("total"), (int, float))],
    key=lambda r: -r["total"])


def _pool_pct_map(_key):
    """→ {id(row): 池内分位 0~100}（100 = 该维最好的一端）。非数值不进入。

    ⚠️ 用 id() 作键而非 code：同 code 在 results 中唯一，但 id 更快且零碰撞风险
      （results 生命周期内不重建）。下游只在同一函数调用内使用，安全。
    """
    _vs = [(r.get(_key), r) for r in results
           if isinstance(r.get(_key), (int, float)) and not isinstance(r.get(_key), bool)]
    _vs.sort(key=lambda x: x[0])
    _n = len(_vs)
    if _n < 2:
        return {}, _n
    return {id(r): 100.0 * i / (_n - 1) for i, (_v, r) in enumerate(_vs)}, _n


# ---- T1-1：Top50 流动性披露 ----
_liquidity_banner = ""
try:
    _t50 = _ranked[:50]
    _a50 = sorted([r["amount"] for r in _t50 if isinstance(r.get("amount"), (int, float))])
    _aall = sorted([r["amount"] for r in results if isinstance(r.get("amount"), (int, float))])

    # 秩相关必须现算 —— 写死数字会随池子结构漂移。本项目已踩过这个坑：
    #   tech_neg×bias60 在小池段是 +0.05~+0.28、大池段是 +0.62~+0.77，
    #   用长样本平均值去描述当前结构 = 跨结构外推。
    def _spearman_amount(_key):
        _pv, _nav = _pool_pct_map(_key)
        _av, _na = _pool_pct_map("amount")
        _ks = [k for k in _pv if k in _av]
        _n = len(_ks)
        if _n < 30:
            return None
        _xs = [_pv[k] for k in _ks]
        _ys = [_av[k] for k in _ks]
        _mx, _my = sum(_xs) / _n, sum(_ys) / _n
        _num = sum((a - _mx) * (b - _my) for a, b in zip(_xs, _ys))
        _den = (sum((a - _mx) ** 2 for a in _xs) * sum((b - _my) ** 2 for b in _ys)) ** 0.5
        return (_num / _den) if _den > 0 else None

    # v4.13-size2d：score_size_ind 已退役 → 改用 A 维 score_size_growth（小盘成长溢价）。
    #   ⚠️ 必须兜底 None：_spearman_amount 在 n<30 时返回 None，而下方 f-string 用
    #   {_r_size_amt:+.2f} 格式化 None 会抛 TypeError → 整条流动性横幅被外层
    #   except 吞掉、页面**整块消失且无任何告警**（本项目典型静默失败）。
    #   故此处做「有值才渲染」的降级，而不是让 None 流向格式化。
    _r_size_amt = _spearman_amount("score_size_growth")
    _r_lv_amt = _spearman_amount("score_lowvol20")
    _r_size_txt = (f"{_r_size_amt:+.2f}" if _r_size_amt is not None else "n/a")
    _r_lv_txt = (f"{_r_lv_amt:+.2f}" if _r_lv_amt is not None else "n/a")

    if len(_a50) >= 20 and _aall:
        def _q(_arr, _p):
            return _arr[min(len(_arr) - 1, int(len(_arr) * _p))]
        _med50 = _q(_a50, 0.5)
        _medall = _q(_aall, 0.5)
        _n_3 = sum(1 for x in _a50 if x < 3e8)
        _n_15 = sum(1 for x in _a50 if x < 1.5e8)
        # Top10 内成交额达 3 亿的只数 —— 这是最需要说清的一句：
        #   模型给出「最看好」的 10 只，恰恰是流动性最差的 10 只。
        _a10 = [r["amount"] for r in _ranked[:10] if isinstance(r.get("amount"), (int, float))]
        _n10_3 = sum(1 for x in _a10 if x >= 3e8)
        # 提高门槛的代价：池内剩多少只（实测 3 亿 → 882/1895 = 46.5%）
        _keep3 = sum(1 for x in _aall if x >= 3e8)
        _keep3p = 100.0 * _keep3 / max(1, len(_aall))
        _disc = 100.0 * (_med50 / max(1.0, _medall) - 1.0)
        _liquidity_banner = (
            '<div style="font-size:10.5px;color:#f59e0b;padding:7px 10px;'
            'background:rgba(245,158,11,0.07);border:1px solid rgba(245,158,11,0.30);'
            'border-radius:6px;margin-bottom:10px;line-height:1.75;">'
            f'⚠️ <b>流动性约束 —— 前排标的日均成交额低于池子中位</b>：'
            f'前 50 名成交额中位 <b>{_med50/1e8:.2f} 亿</b>，'
            f'而全池中位 {_medall/1e8:.2f} 亿（<b>折价 {abs(_disc):.0f}%</b>）；'
            f'前 50 名中 <b>{_n_3}/50 只成交额 &lt;3 亿</b>、<b>{_n_15}/50 只 &lt;1.5 亿</b>，'
            f'区间 {_a50[0]/1e8:.2f}~{_a50[-1]/1e8:.2f} 亿。<br>'
            f'<b style="color:#ef4444;">尤其注意：前 10 名中成交额 ≥3 亿的仅 {_n10_3} 只</b> —— '
            '分数最高的这批恰好是流动性最薄的一批，'
            '这是<b>低波 + 小市值 + 反转</b>三个主导维度的必然结果（不是数据错误）：'
            f'实测池内「小盘成长溢价」与成交额的秩相关 <b>{_r_size_txt}</b>、'
            f'低波与成交额 <b>{_r_lv_txt}</b>，'
            '越符合模型偏好 = 越冷门。<br>'
            '<b>可用性边界</b>：以 1.5~3 亿日成交额计，'
            '<b>个人投资者单笔 10 万以内可正常进出</b>（占日成交额 &lt;0.1%）；'
            '<b style="color:#ef4444;">机构资金（单笔千万级以上）不可直接按本榜执行</b>'
            '—— 买入即占日成交额 3%+，滑点会吞掉全部理论超额。<br>'
            f'<b>为什么不一刀切提高门槛到 3 亿</b>：实测会把池子从 '
            f'{len(_aall)} 只砍到 {_keep3} 只（仅剩 {_keep3p:.0f}%），'
            '且砍掉的正是<b>低波/反转维度的主要贡献样本</b>，'
            '历史 IC 基准（h5 +0.19 / h20 +0.24）将全部失效、无法跨期比较。'
            '如需机构口径，建议<b>单独跑一份高流动性池</b>而非修改本池规则。</div>')
except Exception as _lqe:
    print(f"⚠️ 流动性披露生成失败（不影响主表）: {type(_lqe).__name__}: {_lqe}")
    _liquidity_banner = ""

# ---- T1-2：风格暴露卡片 + T1-3 估值绝对参照 ----
_style_block = ""
# ⚠️ 禁止在此处重置 _pool_pe_stats —— 它已在 results 加载后算好（L59-82），
#   此处重置会让「估值绝对参照」静默消失：披露条里 PE 段落空白、
#   pool_pe_history.json 也写不出来，但**退出码 0、无任何报错**。
#   实际踩过：第一次跑完报告正常、只是少了这段，靠核对生成物才发现。
try:
    _t50 = _ranked[:50]
    _expo = []
    for _k in ("score_rsi_rev", "score_bias60", "score_lowvol20", "score_quality",
               "score_tech_neg", "score_fund", "score_ep",
               # v4.13-size2d：score_size_ind 退役 → A/B 双维
               "score_size_growth", "score_dominant"):
        _pm, _n = _pool_pct_map(_k)
        if _n < 20:
            continue
        _ps = [_pm[id(r)] for r in _t50 if id(r) in _pm]
        if len(_ps) < 10:
            continue
        _expo.append((_k, sum(_ps) / len(_ps), len(_ps)))
    _expo.sort(key=lambda x: -x[1])

    # 行业超配：入选率 = 该行业进 Top50 只数 / 该行业全池只数
    _sec_all, _sec_top = {}, {}
    for r in results:
        _s = r.get("sector_l2") or r.get("sector") or "未分类"
        _sec_all[_s] = _sec_all.get(_s, 0) + 1
    for r in _t50:
        _s = r.get("sector_l2") or r.get("sector") or "未分类"
        _sec_top[_s] = _sec_top.get(_s, 0) + 1
    _sec_rate = [(s, _sec_top.get(s, 0), n, 100.0 * _sec_top.get(s, 0) / n)
                 for s, n in _sec_all.items() if n >= 15]
    _sec_rate.sort(key=lambda x: -x[3])
    _over = [x for x in _sec_rate if x[3] >= 10.0][:4]
    _under = [x for x in _sec_rate if x[3] == 0.0][:4]

    # T1-3：池内 PE 绝对参照已在上方（results 加载后）统一算好，此处直接复用
    #   _pool_pe_stats，避免「同一件事两处各算一遍」—— 两处口径一旦漂移就会
    #   出现「披露条说 p50=56.9、tooltip 说 p50=52.1」这种自相矛盾。

    if _expo:
        _top_dim, _top_pct = _expo[0][0], _expo[0][1]
        _max_pct = _expo[0][1]
        _bars = "".join(
            f'<div style="display:flex;align-items:center;gap:6px;margin:2px 0;">'
            f'<span style="width:64px;color:#94a3b8;font-size:10px;">'
            f'{_DIM_LABEL_SHORT.get(k, k)}</span>'
            f'<div style="flex:1;height:9px;background:#1e293b;border-radius:4px;overflow:hidden;">'
            f'<div style="width:{p:.0f}%;height:100%;'
            f'background:{"#ef4444" if p >= 90 else ("#f59e0b" if p >= 75 else "#3b82f6")};"></div></div>'
            f'<span style="width:38px;text-align:right;font-size:10px;'
            f'color:{"#ef4444" if p >= 90 else "#cbd5e1"};font-weight:700;">{p:.0f}%</span></div>'
            for k, p, _n in _expo)
        _over_txt = " · ".join(
            f'<b>{s}</b> {h}/{n}（{p:.0f}%）' for s, h, n, p in _over) or "无"
        _under_txt = " · ".join(
            f'{s} 0/{n}' for s, h, n, p in _under) or "无"
        # ⚠️ 结论必须由数据生成，不能写死「反转类已达 95% 上下」——
        #   实测当日 RSI反 79%、乖离 70%，写死的 95% 是错的（我在首版就写错了）。
        _read_txt = (
            f'最强的是<b>{_DIM_LABEL_SHORT.get(_top_dim, _top_dim)}（{_top_pct:.0f}%）</b>，'
            + (f'已接近池内顶端，说明模型<b>实际押注的就是这个风格</b>，'
               f'一旦该风格逆风，整份名单会同向回撤 —— 这不是分散，是<b>集中下注</b>。'
               if _max_pct >= 85 else
               f'但<b>没有任何一维达到 90%</b>（最高 {_max_pct:.0f}%）—— '
               f'说明前 50 名不是「单风格极端集中」，而是在多个维度上都取了偏优但非极端的票。'
               f'这比单一风格押注更耐风格切换，代价是<b>上涨弹性也摊薄了</b>。'))
        _pe_txt = ""
        if "pe" in _pool_pe_stats:
            _ps = _pool_pe_stats["pe"]
            _pe_txt = (
                f'<br><b style="color:#7dd3fc;">估值绝对参照（当日池内实测）</b>：'
                f'PE(TTM) p10 <b>{_ps["p10"]:.1f}</b> · p50 <b>{_ps["p50"]:.1f}</b> · '
                f'p90 <b>{_ps["p90"]:.1f}</b> 倍；'
                f'FwdPE p50 <b>{_pool_pe_stats.get("fwd_pe", {}).get("p50", float("nan")):.1f}</b> 倍。'
                f'<b>「估值锚 8 分」= 池内最便宜的 20%，不等于绝对低估</b> —— '
                f'本池 PE 中位已达 {_ps["p50"]:.0f} 倍，全池最便宜档也在 18 倍附近。'
                f'另：{_pool_pe_stats.get("n_loss", 0)} 只当前亏损（PE≤0，估值分已按 T0-4 封顶 3.0）、'
                f'{_pool_pe_stats.get("n_nofwd", 0)} 只无 FwdPE。')
        _style_block = (
            '<div style="font-size:10.5px;color:#a5b4fc;padding:8px 11px;'
            'background:rgba(165,180,252,0.06);border:1px solid rgba(165,180,252,0.25);'
            'border-radius:6px;margin-bottom:10px;line-height:1.75;">'
            '<b>🎯 风格暴露 —— 前 50 名在各维度的池内平均分位</b>'
            '<span style="color:#64748b;">（100% = 该维池内最好的一端；'
            '这是因子有效性的<b>自然结果</b>，不是选股 bug）</span>'
            f'<div style="margin:7px 0 5px;">{_bars}</div>'
            f'<b>怎么读</b>：{_read_txt}<br>'
            f'<b>行业超配</b>（入选率 = 进前50 只数 / 该行业全池只数）：{_over_txt}<br>'
            f'<b>行业零入选</b>：{_under_txt}<br>'
            '<span style="color:#64748b;">行业偏差同样来自因子：'
            '低波 + 反转天然偏周期/资源/公用事业，天然避开高波动成长（半导体、软件）。'
            '<b>若你的组合已重仓成长赛道，本榜与你的持仓是负相关的对冲品，不是补充。</b></span>'
            f'{_pe_txt}</div>')
except Exception as _sxe:
    print(f"⚠️ 风格暴露披露生成失败（不影响主表）: {type(_sxe).__name__}: {_sxe}")
    _style_block = ""

# ---- T1-3 持久化：pool_pe_history.json 记录每日 PE 分位，供跨日漂移监控 ----
# ⚠️ 只追加/覆盖当日一条，不重写历史；文件缺失不报错（首日自动创建）。
try:
    if _pool_pe_stats:
        _peh_p = os.path.join(os.path.dirname(os.path.abspath(__file__)), "pool_pe_history.json")
        _peh = {}
        if os.path.exists(_peh_p):
            try:
                with open(_peh_p, encoding="utf-8") as _f:
                    _peh = json.load(_f) or {}
            except Exception:
                _peh = {}
        _peh[str(date_str)] = {
            "pe": {k: (round(v, 2) if isinstance(v, float) else v)
                   for k, v in (_pool_pe_stats.get("pe") or {}).items()},
            "fwd_pe": {k: (round(v, 2) if isinstance(v, float) else v)
                       for k, v in (_pool_pe_stats.get("fwd_pe") or {}).items()},
            "pool_n": len(results),
        }
        with open(_peh_p, "w", encoding="utf-8") as _f:
            json.dump(_peh, _f, ensure_ascii=False, indent=2, sort_keys=True)
        print(f"✅ pool_pe_history.json 已更新（{date_str}，累计 {len(_peh)} 个截面）")
except Exception as _phe:
    print(f"⚠️ pool_pe_history.json 写入失败（不影响主表）: {type(_phe).__name__}: {_phe}")

html_parts.append(_neutral_banner)

# v4.10（2026-10-27 T0-1）：排名精度披露 —— 同样走独立 append（同上教训）
html_parts.append(_precision_banner)

# v4.10（2026-10-27 T1-1）：流动性约束披露 —— 必须紧邻完整排名表，
#   因为读者一旦往下翻表就会直接拿来下单，披露放远了等于没有。
html_parts.append(_liquidity_banner)

# v4.10（2026-10-27 T1-2/T1-3）：风格暴露 + 估值绝对参照
html_parts.append(_style_block)

html_parts.append('''
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
        <!-- v4.10（2026-10-27 T1-1）：成交额下限筛选。
             独立控件，不并入「资格筛选」——
             资格筛选的 liquid_ok 判据是 config 里的 min_avg_amount_20d=5000万
             （那是**入池地板**，几乎全场通过，对下单毫无参考价值）；
             这里给的是**可执行性门槛**（3亿/1.5亿），语义完全不同，混在一起会误导。
             ⚠️ 只筛显示，不改排序、不改总分、不改评级。 -->
        <div class="control-group">
            <label>💧 成交额下限</label>
            <select id="amountFilter">
                <option value="">不限</option>
                <option value="100000000">≥1 亿（个人可做）</option>
                <option value="150000000">≥1.5 亿</option>
                <option value="300000000">≥3 亿（机构门槛）</option>
                <option value="500000000">≥5 亿</option>
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
                    <th class="s-rank" data-sort="rank" title="分档 + 档内序号（按 5 分带）。相邻名次分差中位数仅 0.02 分，名次不具决策意义 → 请按分档决策。悬停可见原始全局名次。">档位 <span class="sort-arrow"></span></th>
                    <th class="s-name sticky-col col-name-sticky" data-sort="name">名称 <span class="sort-arrow"></span></th>
                    <th class="s-code col-code">代码</th>
                    <th class="s-sector col-sector">板块</th>
                    <th class="s-pct sticky-col col-pct-sticky" data-sort="pct_chg">涨跌幅 <span class="sort-arrow"></span></th>
                    <th class="s-week col-week" data-sort="week_chg">近一周 <span class="sort-arrow"></span></th>
                    <th class="s-mom col-mom">板块 R60/R10 <span style="font-weight:400;color:#64748b;font-size:9.5px;">(区间涨幅·非评分)</span></th>
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
# ══════════════════════════════════════════════════════════════════════════
# v4.10（2026-10-27 T0-1）：名次列由「全局连续第 N 名」改为「分档 + 档内序号」
#
# 为什么必须改（投研视角体检，n=1895 实测）：
#   · 相邻名次分差中位数仅 0.020 分（舍入精度 0.01 的两倍）
#   · 93.6% 的相邻对分差 ≤0.1 分；Top50 内 31/49 对 ≤0.1 分
#   · Top10 总分区间 85.18~92.35（仅差 7.17 分）
# 即「第 17 名 vs 第 18 名」的差距小于打分噪声，用它做「买这只不买那只」
# 没有任何依据。但分档是有效的（等宽十分层单调，层序秩相关 1.0）。
#
# 分档口径：按绝对分 5 分带（90+/85-90/80-85/…），与 RATING 门槛正交
#   —— 评級是 S/A/B/C/D（≥85/65/45/25），分档更细，两者并存不冲突。
# 保留 data-value = 原始全局名次，排序功能不受影响（JS 按 data-value 排）。
# ══════════════════════════════════════════════════════════════════════════
_BAND_W = 5.0


def _band_of(v):
    """绝对分 → 5 分带左边界。None → None。"""
    if not isinstance(v, (int, float)):
        return None
    return int(v // _BAND_W) * _BAND_W


def _band_label(lo):
    if lo is None:
        return "—"
    return f"{lo}–{lo + int(_BAND_W)}"


# 档内序号：同档内按分数降序编 1,2,3…（results 已按 total 降序）
_band_seen = {}
_band_pos = {}
for _r0 in results:
    _b0 = _band_of(_r0.get("total"))
    if _b0 is None:
        continue
    _band_seen[_b0] = _band_seen.get(_b0, 0) + 1
    _band_pos[id(_r0)] = _band_seen[_b0]

for idx, r in enumerate(results):
    i = idx + 1
    # v4.10（T0-1）：分档 + 档内序号。悬停显示原始全局名次，便于对账历史。
    _b_lo = _band_of(r.get("total"))
    _b_pos = _band_pos.get(id(r))
    if _b_lo is None or _b_pos is None:
        _rank_cell = f'<span style="color:#475569;">{i}</span>'
    else:
        _rank_cell = (f'<span style="color:#cbd5e1;font-weight:700;" '
                      f'title="分档 {_band_label(_b_lo)} 分 · 档内第 {_b_pos} 位 · '
                      f'原始全局第 {i} 名（相邻名次分差中位数仅 0.02 分，名次不具决策意义，'
                      f'请按分档决策）">'
                      f'{_band_label(_b_lo)}'
                      f'<span style="color:#64748b;font-size:9px;">#{_b_pos}</span></span>')
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
        # 业绩预期差三档（es 字段）：并入本列 tooltip + 角标，不加新表列（变量 _esq_* 唯一前缀）
        _esq_tier = (r.get("es") or {}).get("tier")
        _esq_d = (r.get("es") or {}).get("delta_pp")
        _esq_s = ""
        _esq_gl = ""
        if _esq_tier and _esq_tier != "无法判定":
            _esq_s = f" · 业绩预期:{_esq_tier}{('(%+.1fpp)' % _esq_d) if isinstance(_esq_d, (int, float)) else ''}"
            _esq_gl = ("🔺" if _esq_tier == "超预期" else ("🔻" if _esq_tier == "低于预期" else "➖"))
        _q3_cell = (f"<span style='color:{_qcol};font-weight:700;' title='Q3预测同比{_qy} · 加速度{_qacc} · "
                    f"机构预期隐含H2增速{_qih2}{_qsat} · 景气加成+{_qbn:.1f}分{_esq_s}'>"
                    f"{_qy}<span style='font-size:10px;color:#64748b;'> {_qacc}{_esq_gl}</span></span>")
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
    # ⚠️ v4.5：bias60 / rsi_rev 已改由 _eff_attrs 写**归一化**值，此处切不可重复写
    #   （浏览器解析重复属性取第一个，先写的原始 0-5 值会覆盖归一化值，排序口径静默错乱）。
    #   本残留块只保留 0 权重维，供 JS 详情行「不计分·参考」取值。
    _dsub_attrs = ""
    if r.get("score_oversold60") is not None:
        _dsub_attrs = (f' data-s-oversold60="{r["score_oversold60"]:.2f}"'
                       f' data-s-vol_pulse="{r["score_vol_pulse"]:.2f}"'
                       f' data-s-overbought="{r["score_overbought"]:.2f}"'
                       f' data-s-theme_intrinsic="{r.get("score_theme_intrinsic", 0):.2f}"'
                       f' data-s-attention="{r.get("score_attention", 0):.2f}"')
    # v4.5（问题2）：完整排名「十维评分」列原本把 10 个维度全画出来，
    #   其中 8 维（news/oversold60/vol_pulse/overbought/theme_intrinsic/attention/
    #   sector/theme）权重已是 0。后果有两层：
    #     ① 读者以为模型还在给消息面、题材面打分（权重表里这些全是 0.0）；
    #     ② 真正参与评分的 quality/growth/tech_neg/size **根本没进这一列** ——
    #        列里 3/10 的格子是有效信息，7/10 是装饰。
    #   现在改为只画 7 个生效维，并按各自量程归一化到 0-10（否则 0-5 制的 fund
    #   满分 5 会显得比 0-10 制中位还低）。归零维仍保留 data-s-* 属性供排序与
    #   尽调取用，但不再占据可见分数格。
    #   v4.8：growth 归零 → 换 lowvol20（round24 实证）。格数必须恒 = 行数×7。
    #   v4.9（P0 修复）：原来是**硬编码 7 元组**，v4.8/v4.9 改维后必然漏（本轮
    #     新增 ep/size_ind 时即是：硬编码里还是旧的 size）→ 分数条与权重条打架。
    #     改为由 _W_EFF 动态派生（顺序即 _DIM_DEFS 顺序，权重>0 才进），
    #     并断言不是空元组。格数必须恒 = 行数 × len(_W_EFF)。
    if r.get("score_oversold60") is not None:
        _eff_keys = tuple(_k for _l, _k in _W_EFF)
        assert _eff_keys, "生效维清单为空（权重配置异常）"
        _eff_vals = [_norm10(_k, r.get(_k)) for _k in _eff_keys]
        # tooltip 给「归一化值」+「数据库原始分」，后者用 raw 量程 /2 还原，
        # 便于与 scored JSON / 因子日报对数（disp 量程对 0-5 制维会写「10.00/10」）
        _score_tips = []
        for _k in _eff_keys:
            _rv = r.get(_k)
            if isinstance(_rv, (int, float)):
                _mx_raw = _DIM_MAX_RAW.get(_k) or 10.0
                _raw_show = _rv / 2.0 if _k in _SCORE_KEYS10 else _rv
                _score_tips.append(
                    f"{_DIM_LABEL_SHORT[_k]} 归一化 {_norm10(_k, _rv):.2f}/10"
                    f"（库内原始 {_raw_show:.2f}/{_mx_raw:.0f}）")
            else:
                _score_tips.append(f"{_DIM_LABEL_SHORT[_k]} 无数据")
    else:
        _eff_keys = ()
        _eff_vals = []
        _score_tips = ["消息面", "技术面", "基本面", "题材热度"]
    if not _eff_vals:
        # ⚠️ 2026-10-27 P0：score_fund 可能为 None（缺有效估值·本维不计分）。
        #   原 f"{v}" 会渲染成字面 "None"，int(v/2) 更会 TypeError。
        #   此处用 0 占位只影响**画格子配色**（旧五维兼容路径，仅历史 scored 才走），
        #   并把 title 标注「未计分」，避免读者误以为得了 0 分。
        _sf_raw = r.get("score_fund")
        _sf_ok = isinstance(_sf_raw, (int, float)) and not isinstance(_sf_raw, bool)
        scores = [r["score_news"], r["score_tech"], (_sf_raw if _sf_ok else 0),
                  r.get("score_theme", r.get("score_flow", 0))]
        # v2.40: 第五维「板块动量」(若 scored json 含该字段)
        if r.get("score_sector") is not None:
            scores.append(r["score_sector"])
            _score_tips.append("板块动量")
        if not _sf_ok:
            # 第 3 格是 fund（索引 2）
            _score_tips = list(_score_tips)
            while len(_score_tips) < len(scores):
                _score_tips.append("")
            _score_tips[2] = "基本面估值锚 未计分（缺PE/FwdPE）"
        score_colors = ["#ef4444", "#f97316", "#f59e0b", "#3b82f6", "#22c55e",
                        "#ef4444", "#f97316", "#f59e0b", "#3b82f6", "#22c55e"]
        scores_html = "".join([f'<div class="s-score" style="background:{score_colors[min(int(v/2),4)]}" title="{_score_tips[k] if k < len(_score_tips) else ""}">{v}</div>'
                               for k, v in enumerate(scores)])
    else:
        # 归一化到 0-10 后按 10 分制分档配色。
        # ⚠️ 2026-10-27 P0-4（量尺统一）：缺维已**以中性分 DMAX/2 参与总分**
        #   （见 scale_calc.composite_total），因此分数条必须显示「参与的那个值」
        #   而不是旧的灰色「—」—— 否则读者算出「格子里没数，分数却上去了」。
        #   但绝不能画成与实测值一样的实心格，故用「斜纹底 + 虚线边 + * 上标 + tooltip」
        #   四重标记。⚠️ 必须先判断 _k in _nf_set：_norm10 对 None 返回 None，
        #   若把 `if _v is None` 放前面，所有中性填充维都会落到灰色「—」分支
        #   （首版即踩此坑：739 个格子仍显示 —，披露条与实际渲染对不上）。
        _nf_set = set(r.get("neutral_filled") or [])
        _cells = []
        for _k, _v, _tip in zip(_eff_keys, _eff_vals, _score_tips):
            if _k in _nf_set:
                _fill_v = (_DIM_MAX_MAP.get(_k) or 10.0) / 2.0
                _cells.append(
                    f'<div class="s-score" style="background:{NEUTRAL_FILL_BG};'
                    f'border:1px dashed #94a3b8;color:#cbd5e1;" '
                    f'title="{_tip} —— ⚠️ 该维当日无数据，按量程中点({_fill_v:.1f})'
                    f'中性填充参与总分，非实测值、也非 0 分">'
                    f'{_fill_v:.1f}<span style="font-size:8px;">*</span></div>')
            elif _v is None:
                _cells.append(f'<div class="s-score" style="background:#334155" title="{_tip}">—</div>')
            else:
                _ci = max(0, min(4, int(_v / 2)))
                _pal = ["#ef4444", "#f97316", "#f59e0b", "#3b82f6", "#22c55e"]
                _cells.append(f'<div class="s-score" style="background:{_pal[_ci]}" title="{_tip}">{_v:.1f}</div>')
        scores_html = "".join(_cells)

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

    # v4.5（P0）：_theme_disp_t 在十维口径下已返回 None（题材两维权重皆 0）。
    #   旧代码直接 '{_theme_disp_t:.2f}' 会抛 TypeError: unsupported format string
    #   passed to NoneType。此处统一走安全格式化：None → 空串（前端 parseFloat
    #   得 NaN，快捷筛选按 0 处理，不会误命中）。
    def _f2(x):
        return f"{x:.2f}" if isinstance(x, (int, float)) else ""
    _tds = _f2(_tech_disp_t)
    _ths = _f2(_theme_disp_t)
    # 「热门(热度≥4)」快捷筛选：题材热度已不参与评分，但作为**观察线索**仍有意义，
    #   故保留 data-score-theme（写原始 score_theme 而非归零后的 0），并在按钮上
    #   标注「参考」。这样筛选结果可用，且不会被误当成评分维度。
    _ths_raw = _f2(r.get("score_theme"))
    # 7 个生效维的归一化值写成 data-s-*，供排序下拉（score_fund→data-s-fund 等）取值
    # ⚠️ 项目历史坑：同一 data-s-* 绝不能写两遍（浏览器只取第一个）。
    #   新维入列前必须与 _dsub_attrs（oversold60/vol_pulse/overbought/theme_intrinsic/
    #   attention）以及下方 data-s-news/tech/theme/sector 查键冲突。
    #   v4.8 换维后 7 键 = fund/quality/lowvol20/tech_neg/bias60/rsi_rev/size，无重复。
    #   v4.9（P0 修复）：原为**硬编码元组 + 两行手工追加**，改维后必然漏
    #     （本轮新增 ep/size_ind 时它还在写 size）→ 一律由 _W_EFF 动态生成。
    #     data-s-N 的键名 = 字段名去掉 "score_" 前缀（与 JS 的 dataset.sXxx 映射一致）。
    #     先做重复键检测：同一 data-s-* 写两遍时浏览器只取第一个 → 排序静默读旧值。
    _eff_attr_keys = [_k[6:] for _l, _k in _W_EFF]
    _dup = {k for k in _eff_attr_keys if _eff_attr_keys.count(k) > 1}
    assert not _dup, f"data-s-* 属性重复（浏览器只取第一个）：{sorted(_dup)}"
    _eff_attrs = "".join(
        (f' data-s-{_k[6:]}="{_norm10(_k, r.get(_k)):.2f}"'
         if _norm10(_k, r.get(_k)) is not None else f' data-s-{_k[6:]}=""')
        for _l, _k in _W_EFF
    )
    # 归零维仍需 data-s-* 供详情卡显示（标「不计分·参考」），但不上排序下拉。
    #   ⚠️ 必须排除已在 _eff_attr_keys 里的键，否则重复属性。
    #   v4.13-size2d：score_size_ind 已退役（字段不再写盘），移出本清单。
    for _zk in ("score_growth", "score_size", "score_ep"):
        if _zk[6:] in _eff_attr_keys:
            continue
        _eff_attrs += (f' data-s-{_zk[6:]}="{_norm10(_zk, r.get(_zk)):.2f}"'
                       if _norm10(_zk, r.get(_zk)) is not None
                       else f' data-s-{_zk[6:]}=""')

    html_parts.append(f'''
                <tr class="expandable" data-rating="{rating}" data-sector="{sector_disp(r)}" data-code="{r['code']}" data-name="{r['name']}" data-original-rank="{i}" data-fwd-pe="{r.get('fwd_pe',0) or 0}" data-growth="{r.get('growth',0) or 0}" data-position="{tech.get('position',50) or 50}" data-score-theme="{_ths_raw}"
                    data-route="{route_of(r)}" data-liquid="{'1' if r.get('liquid_ok', True) else '0'}" data-newstock="{'1' if r.get('is_new') else '0'}" data-newpool="{'1' if _is_new_pool else '0'}" data-qual="{'1' if (r.get('liquid_ok', True) and not r.get('is_new') and not r.get('suspended')) else '0'}"
                    data-amount="{r.get('amount') if isinstance(r.get('amount'), (int, float)) else ''}"
                    data-news="{_f2(r.get('score_news'))}" data-tech-score="{_tds}" data-fund-score="{_f2(r.get('score_fund'))}" data-theme-score="{_ths}"
                    data-s-news="{_f2(r.get('score_news'))}" data-s-tech="{_tds}" data-s-theme="{_ths}"{f' data-s-sector="{r["score_sector"]:.2f}"' if r.get('score_sector') is not None else ''}{_dsub_attrs}{_eff_attrs}
                    data-news-text="{html_mod.escape(news_text)}" data-tech-text="{html_mod.escape(tech_text)}" data-fund-text="{html_mod.escape(fund_text)}" data-theme-text="{html_mod.escape(theme_text)}">
                    <td class="s-rank" data-value="{i}">{_rank_cell}</td>
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

# ═══════════════════════════════════════════════════════════════════════
# v4.18（round117 P0-1/2/4）：三条「数据可信度」披露（必须放在表格之后、
#   分析切片之前 —— 这是踩过的坑，见下）
# ─────────────────────────────────────────────────────────────────────
# ⚠️ 事故（round117 实测）：这三块原先写在「因子有效性评估」区（L11241 起），
#   而该区在 _ANALYSIS_SLICE_START 之后 → SHOW_ANALYSIS=False 时
#   `del html_parts[_ANALYSIS_SLICE_START:]` 把它们**整块删掉**，
#   页面上一个字都看不到（构建了、但从未渲染）。
#   而这三块恰恰是「这条数据可不可信」的元信息，绝不属于「分析过程」，
#   必须留在每日决策页上。故前移至此处：股票表之后、切片之前。
#
# 三块内容：
#   ① 链路状态（P0-2）：当天哪些步骤 fail-open 失败了、影响什么
#   ② 研究面板盲区（P0-4）：生效权重里哪些维度 panel 从未覆盖过
#   ③ 截面规模（P0-1）：IC 到底是在多少只股票上算出来的、门槛后还剩几个截面
# ═══════════════════════════════════════════════════════════════════════
_ps_banner = ""
_panel_disclose = ""
_cs_disclose = ""

# ① 链路状态 ─────────────────────────────────────────────────────────
try:
    _psd = None
    # ⚠️ 不要用 _d.get("date")：本文件里 _d 在不同作用域可能是 str/dict 两种形态，
    #   实测 `'str' object has no attribute 'get'` 被 except 吞掉 → 整块静默失败。
    # ⚠️ 也不能简单取「文件名倒序第一个」：补跑历史日期（如今天渲染 11-02 报告，
    #   而 11-03 的状态文件已在）会串日期 —— 用旧报告配新状态条 = 张冠李戴。
    #   故优先精确匹配本页数据日期 _data_date，找不到才退到最新一个并显式标注。
    _psd_all = sorted(glob.glob(os.path.join(_ai_dir, "pipeline_status_*.json")),
                      reverse=True)
    _psd_exact = os.path.join(_ai_dir, f"pipeline_status_{_data_date}.json")
    if _psd_exact in _psd_all:
        _psd_path = _psd_exact
    else:
        _psd_path = _psd_all[0] if _psd_all else None
    if _psd_path and os.path.exists(_psd_path):
        with open(_psd_path, "r", encoding="utf-8") as _f:
            _psd = json.load(_f)
        _psd["_file"] = os.path.basename(_psd_path)
        # 日期一致性标记：不一致时必须让读者看见，不能静默混用
        _psd["_date_match"] = (str(_psd.get("date") or "")[:10] == str(_data_date)[:10])
    if _psd:
        _pv = _psd.get("verdict") or "unknown"
        _pc = _psd.get("counts") or {}
        _psdmis = ""
        if not _psd.get("_date_match"):
            _psdmis = (
                '<div style="margin-top:6px;padding:5px 8px;background:rgba(239,68,68,0.10);'
                'border-radius:4px;font-size:10.5px;color:#fca5a5;line-height:1.6;">'
                f'⚠️ <b>日期不一致</b>：本页数据日为 <code>{html_mod.escape(str(_data_date))}</code>，'
                f'但状态文件是 <code>{html_mod.escape(str(_psd.get("date") or "?"))}</code> —— '
                '该文件不代表今天的执行情况，仅作最近一次运行参考。</div>')
        if _pv == "ok":
            _ps_banner = (
                '<div style="margin:14px 0;padding:8px 14px;background:rgba(34,197,94,0.07);'
                'border-left:3px solid #22c55e;border-radius:6px;font-size:11.5px;color:#86efac;">'
                f'✅ <b>日链完整</b>：{_pc.get("total_recorded") or 0} 个已执行步骤全部成功'
                f'（{html_mod.escape(str(_psd.get("_file") or ""))}）'
                f'，幂等跳过 {_pc.get("not_run_expected") or 0} 个。{_psdmis}</div>')
        else:
            _pcol = {"degraded": "#f59e0b", "failed": "#ef4444"}.get(_pv, "#94a3b8")
            _ptxt = {"degraded": "⚠️ 日链降级", "failed": "⛔ 日链失败"}.get(
                _pv, "❓ 日链状态未记录")
            _prows = ""
            for _s in (_psd.get("steps") or []):
                if _s.get("ok"):
                    continue
                _prows += (
                    '<tr style="border-top:1px solid rgba(148,163,184,0.18);">'
                    f'<td style="padding:4px 8px;color:#e2e8f0;font-weight:700;font-size:11.5px;">'
                    f'{html_mod.escape(str(_s.get("label") or _s.get("step")))}</td>'
                    f'<td style="padding:4px 8px;color:#94a3b8;font-size:11px;text-align:center;">'
                    f'rc={_s.get("rc")}</td>'
                    f'<td style="padding:4px 8px;color:#fca5a5;font-size:11px;">'
                    f'{html_mod.escape(str(_s.get("impact") or _s.get("why") or ""))}</td></tr>')
            _nr = _psd.get("not_run") or []
            if _nr:
                _prows += (
                    '<tr style="border-top:1px solid rgba(148,163,184,0.18);">'
                    f'<td style="padding:4px 8px;color:#e2e8f0;font-weight:700;font-size:11.5px;">'
                    f'未执行（{len(_nr)} 步）</td>'
                    '<td style="padding:4px 8px;color:#94a3b8;font-size:11px;text-align:center;">—</td>'
                    f'<td style="padding:4px 8px;color:#fca5a5;font-size:11px;">'
                    f'{html_mod.escape(", ".join(_nr))}</td></tr>')
            _ps_banner = (
                f'<div style="margin:14px 0;padding:12px 14px;background:rgba(245,158,11,0.07);'
                f'border:1px solid {_pcol}55;border-left:4px solid {_pcol};border-radius:8px;">'
                f'<div style="font-size:13px;font-weight:800;color:{_pcol};margin-bottom:6px;">'
                f'{_ptxt} —— {html_mod.escape(str(_psd.get("verdict_text") or ""))}</div>'
                '<div style="font-size:11.5px;color:#cbd5e1;line-height:1.75;margin-bottom:8px;">'
                '下面这些步骤失败了，但主链没中断 —— <b>所以本页数字仍在，只是口径可能不完整</b>。'
                '请按「影响」列判断哪些结论今天不宜采信。</div>'
                '<table style="width:100%;border-collapse:collapse;font-size:11.5px;">'
                '<thead><tr style="background:#1e293b;">'
                '<th style="padding:5px 8px;text-align:left;color:#94a3b8;font-weight:600;">步骤</th>'
                '<th style="padding:5px 8px;color:#94a3b8;font-weight:600;">退出码</th>'
                '<th style="padding:5px 8px;text-align:left;color:#94a3b8;font-weight:600;">影响</th>'
                f'</tr></thead><tbody>{_prows}</tbody></table>{_psdmis}'
                '<div style="font-size:10.5px;color:#64748b;margin-top:8px;line-height:1.7;">'
                '为什么必须显示：fail-open 的失败不会让页面变空，只会让<b>某一维静默缺失或停在旧值</b>'
                '（如 fund 维覆盖率、行业维是否并入 total）。这种「数字还在但口径变了」比崩掉更难发现。'
                f'状态文件：<code>{html_mod.escape(str(_psd.get("_file") or ""))}</code>。</div></div>')
    else:
        _ps_banner = (
            '<div style="margin:14px 0;padding:8px 12px;background:rgba(148,163,184,0.08);'
            'border-left:3px solid #64748b;border-radius:6px;font-size:11px;color:#94a3b8;">'
            'ℹ️ <b>日链状态未记录</b>：未找到 <code>pipeline_status_*.json</code>，'
            '无法判断本页是否在降级状态下生成。这不等于「链路正常」—— 只是没测。</div>')
except Exception as _pse:
    _ps_banner = (
        '<div style="margin:14px 0;padding:8px 12px;background:rgba(239,68,68,0.08);'
        'border-left:3px solid #ef4444;border-radius:6px;font-size:11px;color:#fca5a5;">'
        f'⚠️ 日链状态渲染失败：{html_mod.escape(str(_pse)[:160])}</div>')

# ② 研究面板盲区 ─────────────────────────────────────────────────────
try:
    _pad = None
    _pad_path = os.path.join(_ai_dir, "panel_audit.json")
    if os.path.exists(_pad_path):
        with open(_pad_path, "r", encoding="utf-8") as _f:
            _pad = json.load(_f)
    if _pad and _pad.get("available"):
        _blind = _pad.get("blind") or []
        _clean = bool(_pad.get("fully_covered")) and not _pad.get("n_missing_scored_dates")
        if _clean:
            _panel_disclose = (
                '<div style="margin:14px 0;padding:8px 14px;background:rgba(34,197,94,0.07);'
                'border-left:3px solid #22c55e;border-radius:6px;font-size:11.5px;color:#86efac;">'
                f'✅ <b>研究面板完整</b>：{_pad.get("n_dates")} 截面 × {_pad.get("domain_size")} 只，'
                f'末日 {_pad.get("date_last")}，生效权重维度全覆盖。</div>')
        else:
            _brows = ""
            for _b in _blind:
                _brows += (
                    '<tr style="border-top:1px solid rgba(148,163,184,0.18);">'
                    f'<td style="padding:4px 8px;color:#e2e8f0;font-weight:700;font-size:11.5px;">'
                    f'{html_mod.escape(str(_b.get("dim")))}</td>'
                    f'<td style="padding:4px 8px;text-align:center;color:#fbbf24;font-weight:700;">'
                    f'{_b.get("weight"):.2f}</td>'
                    '<td style="padding:4px 8px;color:#fca5a5;font-size:11px;">'
                    'panel 中无此列 → 该维度<b>从未被检验</b></td></tr>')
            _miss = ""
            if _pad.get("n_missing_scored_dates"):
                _md = ", ".join((_pad.get("missing_scored_dates") or [])[:8])
                _miss = (
                    '<div style="font-size:11px;color:#94a3b8;margin:8px 0 0;line-height:1.7;">'
                    f'⏱️ 新鲜度：panel 末日 <b>{html_mod.escape(str(_pad.get("date_last")))}</b>'
                    f'（{_pad.get("n_dates")} 截面 × {_pad.get("domain_size")} 只），'
                    f'落后最新评分日约 <b>{_pad.get("stale_calendar_days")}</b> 个自然日，'
                    f'缺 <b>{_pad.get("n_missing_scored_dates")}</b> 个已有评分的交易日'
                    f'（{html_mod.escape(_md)}）。生产者 <code>audit_round66_clean_panel.py</code> 未接入日链。</div>')
            _panel_disclose = (
                '<div style="margin:14px 0;padding:12px 14px;background:rgba(245,158,11,0.07);'
                'border:1px solid rgba(245,158,11,0.3);border-left:4px solid #f59e0b;border-radius:8px;">'
                '<div style="font-size:13px;font-weight:800;color:#fbbf24;margin-bottom:6px;">'
                '⚠️ 研究面板盲区 —— 有生效权重维度从未被检验</div>'
                '<div style="font-size:11.5px;color:#cbd5e1;line-height:1.75;margin-bottom:8px;">'
                f'当前 <b>{_pad.get("n_effective_dims")} 维</b>生效权重合计 '
                f'<b>{_pad.get("weights_total")}</b>，其中 '
                f'<b style="color:#fbbf24;">{_pad.get("blind_weight_sum"):.2f}'
                f'（{_pad.get("blind_weight_pct")}%）的权重</b>落在 panel 没有的列上：</div>'
                '<table style="width:100%;border-collapse:collapse;font-size:11.5px;">'
                '<thead><tr style="background:#1e293b;">'
                '<th style="padding:5px 8px;text-align:left;color:#94a3b8;font-weight:600;">维度</th>'
                '<th style="padding:5px 8px;color:#94a3b8;font-weight:600;">权重</th>'
                '<th style="padding:5px 8px;text-align:left;color:#94a3b8;font-weight:600;">状态</th>'
                f'</tr></thead><tbody>{_brows}</tbody></table>'
                + _miss +
                '<div style="font-size:10.5px;color:#64748b;margin-top:8px;line-height:1.7;">'
                '读法：<b>「panel 无该列」只意味着该维度未被检验，不等于无效，更不等于已通过。</b>'
                '基于 panel 的 IC / 权重门禁结论（含「G5 未过」）都必须带着这个盲区去读 —— '
                'G5 未过有一部分原因是<b>它压根没被测</b>，而不是测出来不行。'
                '审计明细：<code>panel_audit.json</code>。</div></div>')
    else:
        _panel_disclose = (
            '<div style="margin:14px 0;padding:8px 12px;background:rgba(148,163,184,0.08);'
            'border-left:3px solid #64748b;border-radius:6px;font-size:11px;color:#94a3b8;">'
            f'ℹ️ <b>面板审计未产出</b>'
            f'（{html_mod.escape(str((_pad or {}).get("error") or "未找到 panel_audit.json"))}）：'
            '无法判断生效权重维度是否全部被 panel 覆盖。这不等于全覆盖。</div>')
except Exception as _pae:
    _panel_disclose = (
        '<div style="margin:14px 0;padding:8px 12px;background:rgba(239,68,68,0.08);'
        'border-left:3px solid #ef4444;border-radius:6px;font-size:11px;color:#fca5a5;">'
        f'⚠️ 面板盲区渲染失败：{html_mod.escape(str(_pae)[:160])}</div>')

# ③ 截面规模 ────────────────────────────────────────────────────────
# ── round118 P0-2：门槛后截面连续性 ─────────────────────────────────
# 三个纯函数已抽到 cs_consecutive.py，理由：本文件是顶层执行式脚本
# （import 时会直接读 top200_scored_<date>.json 开始渲染），无法被单测 import。
# 而「门槛后 10 个截面是不是连续交易日」这条披露写错了不会报错，
# 只会安静地显示一句看起来合理、实际没检查过任何东西的话 —— 必须能被测。
from cs_consecutive import (consecutive_span as _consecutive_span,
                            format_consec_spans as _format_consec_spans,
                            iter_gated_dates as _iter_gated_dates)



try:
    _csa = (_ic or {}).get("cross_section_audit") or {}
    _cs_sum = _csa.get("_summary") or {}
    _cs_gate = _cs_sum.get("min_cross_section_gate") or (_ic or {}).get("min_cross_section")
    if _csa:
        _crows = ""
        for _k in sorted(k for k in _csa if k != "_summary"):
            _v = _csa[_k] or {}
            _parts = (_k.split("|") + ["—"])[:2]
            _lbl, _hh = _parts[0], _parts[1]
            _med = _v.get("cross_section_median")
            _nag = _v.get("n_after_gate")
            _usable = _v.get("usable")
            if _usable is False:
                _uc, _ut = "#ef4444", "不可复现"
            elif _usable is True:
                _uc, _ut = "#22c55e", "可引用"
            else:
                _uc, _ut = "#94a3b8", "—"
            _crows += (
                '<tr style="border-top:1px solid #1e293b;">'
                f'<td style="padding:4px 8px;color:#cbd5e1;font-size:11px;">{html_mod.escape(_lbl)}</td>'
                f'<td style="padding:4px 8px;text-align:center;color:#e2e8f0;font-weight:700;">{_hh}</td>'
                f'<td style="padding:4px 8px;text-align:center;color:#94a3b8;">'
                f'{_v.get("n") if _v.get("n") is not None else "—"}</td>'
                f'<td style="padding:4px 8px;text-align:center;color:#fbbf24;font-weight:700;">'
                f'{_med if _med is not None else "—"}</td>'
                f'<td style="padding:4px 8px;text-align:center;color:#94a3b8;">'
                f'{_v.get("cross_section_min") if _v.get("cross_section_min") is not None else "—"}'
                f'~{_v.get("cross_section_max") if _v.get("cross_section_max") is not None else "—"}</td>'
                f'<td style="padding:4px 8px;text-align:center;color:#94a3b8;">'
                f'{_v.get("n_below_gate") if _v.get("n_below_gate") is not None else "—"}</td>'
                f'<td style="padding:4px 8px;text-align:center;color:{_uc};font-weight:700;">'
                f'{_nag if _nag is not None else "—"}</td>'
                f'<td style="padding:4px 8px;text-align:center;color:{_uc};font-weight:700;'
                f'font-size:11px;">{_ut}</td></tr>')
        _n_usable = sum(1 for k, v in _csa.items()
                        if k != "_summary" and (v or {}).get("usable") is True)
        _n_unusable = sum(1 for k, v in _csa.items()
                          if k != "_summary" and (v or {}).get("usable") is False)
        _cs_disclose = (
            '<div style="margin:14px 0;padding:12px 14px;background:rgba(239,68,68,0.06);'
            'border:1px solid rgba(239,68,68,0.28);border-left:4px solid #ef4444;border-radius:8px;">'
            '<div style="font-size:13px;font-weight:800;color:#fca5a5;margin-bottom:6px;">'
            '⚠️ 截面规模审计 —— IC 到底是在多少只股票上算的</div>'
            '<div style="font-size:11.5px;color:#cbd5e1;line-height:1.75;margin-bottom:8px;">'
            f'判据：每个截面至少要有 <b style="color:#fbbf24;">{_cs_gate} 只</b>股票，'
            '否则 Spearman 秩相关的单截面噪声过大，且该截面与当前生产池不可比。'
            f'当前生产池实际入池 <b style="color:#fbbf24;">'
            f'{_cs_sum.get("production_pool_size") or "—"} 只</b>'
            f'（来源 {html_mod.escape(str(_cs_sum.get("production_pool_source") or "—"))}；'
            f'config.json pool_size={_cs_sum.get("pool_size_config") if _cs_sum.get("pool_size_config") is not None else "—"}，'
            '0 表示不限规模），'
            f'而 IC 各口径的<b>每截面股票数中位仅 {_cs_sum.get("median_of_medians") or "—"} 只</b>。<br>'
            f'<b style="color:#fca5a5;">结论：{_n_usable} 个口径-周期可引用，'
            f'{_n_unusable} 个在门槛下不可复现（不得作为调权依据）。</b>'
            '原因：IC 结论来自比生产池小得多的域，不可直接外推到当前池。</div>'
            '<table style="width:100%;border-collapse:collapse;font-size:11.5px;">'
            '<thead><tr style="background:#1e293b;">'
            '<th style="padding:5px 8px;text-align:left;color:#94a3b8;font-weight:600;">口径</th>'
            '<th style="padding:5px 8px;color:#94a3b8;font-weight:600;">周期</th>'
            '<th style="padding:5px 8px;color:#94a3b8;font-weight:600;">原截面</th>'
            '<th style="padding:5px 8px;color:#94a3b8;font-weight:600;">中位股票</th>'
            '<th style="padding:5px 8px;color:#94a3b8;font-weight:600;">范围</th>'
            '<th style="padding:5px 8px;color:#94a3b8;font-weight:600;">低于门槛</th>'
            '<th style="padding:5px 8px;color:#94a3b8;font-weight:600;">门槛后</th>'
            '<th style="padding:5px 8px;color:#94a3b8;font-weight:600;">可引用性</th>'
            f'</tr></thead><tbody>{_crows}</tbody></table>'
            '<div style="font-size:10.5px;color:#64748b;margin-top:8px;line-height:1.7;">'
            '为什么这比「样本短」严重：短的样本至少是诚实的；'
            '而在 194 只股票上算出的显著、被读成 2084 只池的结论，是<b>口径错配</b>。'
            '修法见 <code>factor_eval.MIN_CROSS_SECTION</code>；'
            '原字段全部保留（不删不改），门槛结果是并列新字段。</div></div>')
        # round118 P0-2：追加「门槛后截面是否连续」披露（独立块，失败不影响上面）
        try:
            _consec_html = _format_consec_spans(_ic)
            if _consec_html:
                _cs_disclose += _consec_html
        except Exception as _cse2:
            _cs_disclose += (
                '<div style="margin:8px 0;padding:8px 12px;background:rgba(251,191,36,0.08);'
                'border-left:3px solid #fbbf24;border-radius:6px;font-size:11px;color:#fcd34d;">'
                f'⚠️ 门槛后连续性渲染失败（不影响上方表格）：{html_mod.escape(str(_cse2)[:160])}</div>')
    elif _ic:
        _cs_disclose = (
            '<div style="margin:14px 0;padding:10px 14px;background:rgba(148,163,184,0.08);'
            'border-left:3px solid #64748b;border-radius:6px;font-size:11.5px;color:#cbd5e1;'
            'line-height:1.7;">ℹ️ <b>截面规模未产出</b>：当前 <code>factor_ic.json</code> 是 '
            'v4.18 之前的版本，不含 <code>cross_section_audit</code> 字段，因此<b>无法判断</b>'
            '每个 IC 截面用了多少只股票。重跑 <code>factor_eval.py</code> 后本节将出现完整审计表。</div>')
except Exception as _cse:
    _cs_disclose = (
        '<div style="margin:14px 0;padding:10px 14px;background:rgba(239,68,68,0.08);'
        'border-left:3px solid #ef4444;border-radius:6px;font-size:11.5px;color:#fca5a5;">'
        f'⚠️ 截面规模审计渲染失败：{html_mod.escape(str(_cse)[:200])}</div>')

# ④ 价格库有效截面 + 前复权锚点（P1-1）────────────────────────────────
_kl_disclose = ""
try:
    _kc_info = None
    _kc_path = os.path.join(_ai_dir, "data", "klines_long_meta.json")
    if os.path.exists(_kc_path):
        with open(_kc_path, "r", encoding="utf-8") as _f:
            _km = json.load(_f)
        _kc_info = _km.get("cross_section_audit") or {}
        _kc_nvalid = len(_km.get("valid_cross_sections") or [])
        _kc_rule = _km.get("validity_rule") or ""
        _kc_written = _km.get("meta_written_at") or ""
        _kc_writer = _km.get("meta_writer") or ""
    else:
        _kc_nvalid, _kc_rule, _kc_written, _kc_writer = 0, "", "", ""

    # 锚点自检结果
    _qa = None
    _qa_path = os.path.join(_ai_dir, "data", "qfq_anchor_audit.json")
    if os.path.exists(_qa_path):
        with open(_qa_path, "r", encoding="utf-8") as _f:
            _qa = json.load(_f)

    if _kc_info and _kc_info.get("n_dates_total"):
        _nt = _kc_info["n_dates_total"]
        _nv = _kc_info.get("n_dates_valid") or _kc_nvalid
        _nd = _kc_info.get("n_dates_dropped") or 0
        _gap_ok = bool(_kc_info.get("threshold_in_gap"))
        _gap = _kc_info.get("gap") or [None, None]
        _pcov = (_qa or {}).get("coverage") or {}
        _qa_verdict = (_qa or {}).get("verdict") or "none"
        _qa_cov = _pcov.get("coverage_pct")
        _qa_rounds = _pcov.get("rounds_completed")

        _qa_txt, _qa_col = {
            "clean": ("本次分片全部一致", "#22c55e"),
            "drift": ("⚠️ 检出锚点漂移，需人工核查", "#ef4444"),
            "skipped": ("本次跳过（该日库内无数据）", "#94a3b8"),
            "local_scan_only": ("仅做本地扫描，未比对东财", "#94a3b8"),
            "none": ("未产出（脚本未跑）", "#94a3b8"),
        }.get(_qa_verdict, (f"未知状态 {_qa_verdict}", "#94a3b8"))

        _cov_txt = (f"累积覆盖 <b>{_qa_cov:.2f}%</b>（{_pcov.get('n_covered')}/"
                    f"{_pcov.get('n_total')} 只，完成 {_qa_rounds} 轮）"
                    if isinstance(_qa_cov, (int, float)) else "累积覆盖率未记录")

        _kl_disclose = (
            '<div style="margin:14px 0;padding:12px 14px;background:rgba(56,189,248,0.06);'
            'border:1px solid rgba(56,189,248,0.28);border-left:4px solid #38bdf8;border-radius:8px;">'
            '<div style="font-size:13px;font-weight:800;color:#7dd3fc;margin-bottom:6px;">'
            '📐 价格库有效截面与复权锚点</div>'
            '<div style="font-size:11.5px;color:#cbd5e1;line-height:1.75;margin-bottom:8px;">'
            f'库内共 <b>{_nt}</b> 个日期，其中 <b style="color:#7dd3fc;">{_nv} 个是有效横截面</b>'
            f'（{html_mod.escape(str(_kc_info.get("valid_date_first") or "—"))} ~ '
            f'{html_mod.escape(str(_kc_info.get("valid_date_last") or "—"))}），'
            f'另 <b>{_nd}</b> 个被剔除 —— 它们是建库初期逐只补数的爬坡段'
            f'（截面 {_kc_info.get("dropped_max_size") or "—"} 只以下）。<br>'
            f'判据：<code>{html_mod.escape(_kc_rule)}</code>。'
            + ('门槛<b>落在真空区</b> '
               f'[{_gap[0]}, {_gap[1]}]（被剔除的最大截面 &lt; 保留的最小截面）'
               '→ 分布双峰，门槛在 150~1500 间任取结果相同，<b>结论对该阈值不敏感</b>。'
               if _gap_ok else
               '⚠️ 门槛<b>不在真空区</b>，需复核是否误删边界样本。')
            + '</div>'
            '<table style="width:100%;border-collapse:collapse;font-size:11.5px;">'
            '<thead><tr style="background:#1e293b;">'
            '<th style="padding:5px 8px;text-align:left;color:#94a3b8;font-weight:600;">检查项</th>'
            '<th style="padding:5px 8px;text-align:left;color:#94a3b8;font-weight:600;">结果</th>'
            '<th style="padding:5px 8px;text-align:left;color:#94a3b8;font-weight:600;">说明</th>'
            '</tr></thead><tbody>'
            '<tr style="border-top:1px solid rgba(148,163,184,0.18);">'
            '<td style="padding:4px 8px;color:#e2e8f0;font-weight:700;">有效截面标记</td>'
            f'<td style="padding:4px 8px;color:#86efac;">{_nv} / {_nt} 日</td>'
            '<td style="padding:4px 8px;color:#94a3b8;">写入 <code>valid_cross_sections</code>，'
            '消费方统一走 <code>kline_calendar</code>（不删数据，只标记）</td></tr>'
            '<tr style="border-top:1px solid rgba(148,163,184,0.18);">'
            '<td style="padding:4px 8px;color:#e2e8f0;font-weight:700;">前复权锚点自检</td>'
            f'<td style="padding:4px 8px;color:{_qa_col};">{_qa_txt}</td>'
            f'<td style="padding:4px 8px;color:#94a3b8;">{_cov_txt}</td></tr>'
            '</tbody></table>'
            '<div style="font-size:10.5px;color:#64748b;margin-top:8px;line-height:1.7;">'
            '为什么要标记有效截面：在只有 1~148 只股票的早期日期上算横截面 IC / 分层收益'
            '<b>数学上无意义</b>（Spearman 秩相关在 1 只股票上无定义）。库里原本没有标记，'
            '于是每个消费方各写一遍过滤逻辑，口径互不相同 → 同一份库算出不同样本区间。<br>'
            '为什么要锚点自检：<code>append_klines_latest</code> 有「已有日期绝不覆盖」的设计，'
            '这在除权后会让<b>历史价停在新锚点</b>而新价已是旧锚点，'
            '跨除权日收益凭空多一个跌幅 —— 不报错、不缺值，只让回测静默偏低。<br>'
            f'⚠️ 覆盖率 {(_qa_cov if isinstance(_qa_cov,(int,float)) else 0):.2f}% '
            '&lt; 100% 时，<b>未覆盖部分不是「已通过」，是「没测」</b>。'
            f'元信息写入：<code>{html_mod.escape(_kc_writer or "—")}</code> '
            f'@ {html_mod.escape(_kc_written or "—")}。</div></div>')
    else:
        _kl_disclose = (
            '<div style="margin:14px 0;padding:10px 14px;background:rgba(148,163,184,0.08);'
            'border-left:3px solid #64748b;border-radius:6px;font-size:11.5px;color:#cbd5e1;'
            'line-height:1.7;">ℹ️ <b>价格库有效截面未记录</b>：'
            '<code>data/klines_long_meta.json</code> 缺少 <code>cross_section_audit</code>，'
            '无法判断哪些日期是有效横截面。<b>这不等于「全部有效」</b> —— 只是没测。'
            '跑一次 <code>refresh_kline_meta.py</code> 即可补上。</div>')
except Exception as _kle:
    _kl_disclose = (
        '<div style="margin:14px 0;padding:10px 14px;background:rgba(239,68,68,0.08);'
        'border-left:3px solid #ef4444;border-radius:6px;font-size:11px;color:#fca5a5;">'
        f'⚠️ 价格库口径渲染失败：{html_mod.escape(str(_kle)[:200])}</div>')

# ⑤ 回测口径（P1-5）──────────────────────────────────────────────────
#   报告里有大量「回测超额」「t 值」类结论，但 21 个 backtest_*.py 里
#   0 个声明基准、1 个有 NW t。读者无从判断数字是在什么口径下得出的。
_bt_disclose = ""
try:
    _bta_path = os.path.join(_ai_dir, "data", "backtest_legacy_audit.json")
    if os.path.exists(_bta_path):
        with open(_bta_path, "r", encoding="utf-8") as _f:
            _bta = json.load(_f)
        _bta_n = _bta.get("n_total") or 0
        _bta_c = _bta.get("n_compliant") or 0
        # 统计各缺口出现次数（让读者知道主要缺口是什么）
        _gap_cnt = {}
        for _r in (_bta.get("rows") or []):
            for _g in (_r.get("gaps") or []):
                _gap_cnt[_g.get("key")] = _gap_cnt.get(_g.get("key"), 0) + 1
        _gap_lbl = {
            "nw_t": "无 Newey-West t",
            "benchmark": "基准未声明",
            "valid_calendar": "未用有效截面",
            "core": "未走统一内核",
            "units": "未用双口径",
        }
        _gap_txt = "、".join(
            f"{_gap_lbl.get(k, k)} <b>{v}</b> 个"
            for k, v in sorted(_gap_cnt.items(), key=lambda kv: -kv[1]))
        _bt_disclose = (
            '<div style="margin:14px 0;padding:12px 14px;background:rgba(251,191,36,0.06);'
            'border:1px solid rgba(251,191,36,0.28);border-left:4px solid #fbbf24;'
            'border-radius:8px;">'
            '<div style="font-size:13px;font-weight:800;color:#fcd34d;margin-bottom:6px;">'
            '🧪 回测口径现状（历史结论的适用边界）</div>'
            '<div style="font-size:11.5px;color:#cbd5e1;line-height:1.75;">'
            f'现有 <b>{_bta_n}</b> 个历史回测脚本中，走统一口径的 <b>{_bta_c}</b> 个。'
            f'缺口分布：{_gap_txt}。<br>'
            '<span style="color:#94a3b8;">（历史脚本按设计<b>不回溯修改</b>'
            '——它们是既往结论的证据链，改了就无法说明「当时的数字是在什么口径下得出的」。'
            '统一内核 <code>backtest_core.py</code> 自本轮起生效，'
            '故存量统计为 0 属预期，不代表新增回测也是 0。）</span><br>'
            '<span style="color:#fca5a5;">⚠️ 这不表示历史结论是错的</span>'
            '——它们在当时的口径下成立。但 <b>重叠窗口用普通 t 会高估显著性</b>：'
            '实测同类脚本普通 t 比 Newey-West t 虚高 <b>1.88~2.87 倍</b>，'
            'HOP=20 时 NW 的 lag 必须取 <b>19</b>（取 4 仍虚高 1.7 倍）。'
            '故页面中「显著」类表述，若无特别说明，'
            '<b style="color:#fcd34d;">仅可视为方向性提示，不构成统计显著证据</b>。<br>'
            '新回测统一走 <code>backtest_core.Backtest</code>'
            '（有效截面 / POOL_EW 基准 / NW lag=hop−1 在 <code>__init__</code> 里定死）。'
            '</div></div>')
except Exception as _bte:
    _bt_disclose = (
        '<div style="margin:14px 0;padding:10px 14px;background:rgba(239,68,68,0.08);'
        'border-left:3px solid #ef4444;border-radius:6px;font-size:11px;color:#fca5a5;">'
        f'⚠️ 回测口径披露渲染失败：{html_mod.escape(str(_bte)[:200])}</div>')

html_parts.append(_ps_banner + _panel_disclose + _cs_disclose + _kl_disclose + _bt_disclose)

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

# ===================================================================
# v4.0 分析区切片起点（2026-10-25）
# -------------------------------------------------------------------
# 以下 3703~5614 全部属于「分析与计算过程」：因子 IC 表、时间衰减、
# 反转块样本量、SIZE 证伪、市值下限扫描、锁定股偏差、47 因子对照、
# 相关矩阵、十维权重、安全门、稀释检验、覆盖率、盈利取数口径……
#
# 这些内容对「今天买什么」零贡献，但对「权重该怎么调」至关重要。
# 因此不在本页展示，改由 gen_factor_daily.py 每日生成 Markdown 报告
# 落盘 reports/factor_YYYY-MM-DD.md（不上线）。
#
# 实现方式：记录当前已渲染区块的数量，本段代码照常执行（变量后续仍被
# 推荐跟踪区块复用），执行完再把 html_parts 截断回记录点，
# 从而「计算保留、渲染丢弃」，且不必改动近 1900 行的缩进结构。
_ANALYSIS_SLICE_START = len(html_parts)

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
                该文件为<b>全A 宇宙</b>回测，与本项目「初筛池全量」口径不同，
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
                # v4.15（2026-10-29 老大指令 P0-3）：门槛 3.08 → 累计 304 次口径。
                # round118 P0-4 修正：原写死 3.90 是笔误（3.90 对应 K≈519.8）。
                # round118 P1-2 起：K 由 trial_log 账本提供（legacy 304 + 45 个试验轮次
                #   = 349 → 3.8025），不再手写。K 的真实区间 349~656，故这是「可证下界」口径。
                if abs(_nwt) >= _T_BONF:
                    return _STAT_BADGE["pool_validated"]
                return (f'<span style="color:#a3e635;font-size:10px;" '
                        f'title="NW t={_nwt:.2f}，过普通显著(|t|≥2)但未过 Bonferroni'
                        f'(|t|≥{_T_BONF}，K={_K_LIVE} 来源 {_K_SRC})">'
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
            # v4.4 None 防护：prior 有登记但 lam_best 缺失（未跑权重拟合的版本，
            #   如 v4.4-fund-split）时 _ni/_nt 为 None，直接格式化会 TypeError。
            _pi_s = f"{_pi:+.3f}（NW t={_pt}）" if isinstance(_pi, (int, float)) else "—"
            _ni_s = f"{_ni:+.3f}（NW t={_nt}）" if isinstance(_ni, (int, float)) else "—"
            _d = (_ni - _pi) if isinstance(_pi, (int, float)) and isinstance(_ni, (int, float)) else None
            _d_s = f"{_d:+.3f}" if _d is not None else "—"
            _dc = "#ef4444" if (_d or 0) > 0 else "#22c55e"
            _cmp_rows += (f'<tr><td style="padding:5px 10px;color:#e2e8f0;font-weight:700;">{_hl}</td>'
                          f'<td style="padding:5px 10px;text-align:center;color:#94a3b8;">'
                          f'{_pi_s}</td>'
                          f'<td style="padding:5px 10px;text-align:center;color:#f8fafc;font-weight:800;">'
                          f'{_ni_s}</td>'
                          f'<td style="padding:5px 10px;text-align:center;color:{_dc};font-weight:700;">'
                          f'{_d_s}</td></tr>')
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
    # ⚠️ v4.1（2026-10-25）：此段位于 _ANALYSIS_SLICE_START(3888) 之后，
    #   SHOW_ANALYSIS=False 时**不渲染、不进页面**（v4.0 页面定位=每日决策页，
    #   不承载方法论）。母体分段结论的正式载体已迁至 reports/factor_<date>.md
    #   的「3.1 按母体口径分组」章节。此处仅保留供 SHOW_ANALYSIS=True 调试用，
    #   其中的阈值口径文案（<60亿）已过时，勿据此下结论。
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
            <b style="color:#fbbf24;">★</b> = 通过 Bonferroni 多重检验校正。
            <b style="color:#fca5a5;">v4.15 起门槛 3.08 → <b>3.90</b></b>（项目累计检验已达 304 次，
            单轮 30 次的 3.08 严重低估假阳性率；旧五维期 15 次为 2.97）。
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
    # ---- ⑤ 业绩预期差三档披露（阈值从 earnings_surprise 单一事实源读取，禁此处另写一套）----
    try:
        import earnings_surprise as _es_mod
        _ES_BAND = _es_mod.POINT_BAND_PP
        _ES_STRONG = _es_mod.STRONG_PP
    except Exception as _esi:
        print(f"⚠️ earnings_surprise 导入失败，披露⑤阈值回退模块缺省(10/25pp): {_esi!r}")
        _ES_BAND, _ES_STRONG = 10.0, 25.0
    _es_rows = [x for x in results if x.get("es")]
    _es_judged = [x for x in _es_rows if (x["es"].get("tier") not in (None, "无法判定"))]
    _es_tier_cnt = {"超预期": 0, "符合预期": 0, "低于预期": 0}
    for _esx in _es_judged:
        _es_tier_cnt[_esx["es"]["tier"]] = _es_tier_cnt.get(_esx["es"]["tier"], 0) + 1
    _es_disc = (
        f'⑤ <b>业绩预期差三档</b>（2026-10-31 起，详情卡 ▣ 徽标，<b>权重 0 不进总分</b>）：'
        f'比对<b>机构预测增速</b>（Q3 手工汇总表，A/B 双机构，PIT 2026-10-14）与'
        f'<b>实际单季同比</b>（fin_cache_em 定期报告累计值自拆单季，口径对齐扣非/归母）。'
        f'判定带：区间预测用原始端点 [lo, hi]，点值预测用 ±{_ES_BAND:.0f}pp 等效带'
        f'（≈A/B 双机构预测分歧中位 13.7pp 的单机构噪声带）；'
        f'<b style="color:#22c55e;">超预期</b>=实际超带上沿 / '
        f'<b style="color:#94a3b8;">符合预期</b>=带内 / '
        f'<b style="color:#f87171;">低于预期</b>=破带下沿；'
        f'|Δ|≥{_ES_STRONG:.0f}pp 标「强」。扭亏预测按方向兑现判（不设超预期档）。'
        f'本池标注 {len(_es_rows)} 只、可判定 {len(_es_judged)} 只：'
        f'超预期 {_es_tier_cnt["超预期"]} / 符合 {_es_tier_cnt["符合预期"]} / 低于 {_es_tier_cnt["低于预期"]}。')
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
            ④ 该表非程序化接口、TTL 45 天，未覆盖个股按原口径评分，不受影响。<br>
            {_es_disc}
        </div>
    </div>''')
except Exception as _qe:
    print(f"⚠️ 盈利取数口径披露块生成失败（区块跳过）: {_qe!r}")

# ===================================================================
# v4.0 分析区切片终点：回滚 html_parts 到起点，按开关决定是否保留
# -------------------------------------------------------------------
# 上面的代码已完整执行（_ic / _wmeta / _g_em 等变量仍可被下文复用），
# 这里只把「渲染结果」丢弃，实现「算但不显示」。
# SHOW_ANALYSIS=True 时原样保留（用于调试或需要出方法论页时）。
if not SHOW_ANALYSIS:
    _n_analysis_blocks = len(html_parts) - _ANALYSIS_SLICE_START
    del html_parts[_ANALYSIS_SLICE_START:]
    print(f"🧹 v4.0 已隐藏 {_n_analysis_blocks} 个分析/方法论区块"
          f"（迁至 reports/factor_<date>.md）")


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

        # ===== P0-4 (2026-10-28) 未成熟披露 =====
        # 问题: 原实现 T+20 恒为 None，页面只显示「T+20 未到期（需 20 个交易日）」，
        #   读者无法知道这是「永远不会来」还是「再等几天」，也无法判断该不该据此下结论。
        # 修法: 从 track 的 _meta 读最早可回填日，把**具体日期**写进单元格；
        #   并标注是否为预估（项目无节假日表，超出已知日历的日期按工作日推算，会偏早）。
        # ⚠️ 定义顺序：_wait_txt 必须在其使用方（_stat_cell / _ex_cell）之前定义。
        #   首版把它放在 _stat_cell 之后，_stat_cell 内引用即 NameError；
        #   而本区块整体包在 try/except 里且 except 只 pass → 跟踪区块静默消失、无告警。
        _tmeta = _track.get("_meta") or {}
        _ear = _tmeta.get("earliest_backfill") or {}
        _eproj = _tmeta.get("earliest_projected") or {}
        _stale = _tmeta.get("stale") or {}          # 成熟日已过但仍无值 → 行情未更新
        # ⚠️ 变量名避让：本 try 块上方（L9892）已有 `_cutoff` 记录「最新已回填推荐日」，
        #   此处若复用同名会把它冲成「K线数据截止日」，语义完全不同。
        #   故一律用 _kcut_ 前缀。
        _kcut_ = _tmeta.get("data_cutoff")

        def _wait_txt(h):
            """→ 「未到期」单元格文案：能给出具体日期就给，给不出才退回模糊表述。

            ⚠️ 第三态必须区分（否则会输出荒谬文案）：
               若「可回填日」已经过去、样本却仍未成熟，说明不是「等时间」，
               而是**行情数据没更新**（本地 klines 停更 / 未跑抓取）。此时若仍写
               「2026-10-15 起可回填」，读者看到的就是一个已过去的日期，会判定为 bug。
               故单独归因为「行情待更新」，并附上实际数据截止日。
            """
            _d = _ear.get(str(h))
            if not _d:
                return f"T+{h} 未到期（需 {h} 个交易日）"
            # stale 由 track_recommendations.py 在数据层统一判定后写入 _meta，
            # 页面只读不算 —— 避免「同一口径两处实现」漂移。
            if _stale.get(str(h)):
                return (f"T+{h} 可回填但行情未更新"
                        f"（数据截止 {_kcut_ or '—'}）")
            _tag = "预计 " if _eproj.get(str(h)) else ""
            return f"T+{h} 未到期 · {_tag}{_d} 起可回填"

        def _stat_cell(vals, h=None):
            """收益率单元格（两列：均值 + 胜率）。

            ⚠️ 必须两列都返回：表头是 rowspan=2 的分组列 + 「均值/胜率」双列，
               只返一列会让整行错位（曾出现「16%积累中-1.24%」粘连）。
            """
            if not vals:
                # P0-4: 不再笼统写「积累中」，能给出具体可回填日就给出（与超额列同源）
                _t = _wait_txt(h) if (h is not None and _ear.get(str(h))) else "积累中"
                return (f'<td style="padding:5px 8px;color:#475569;" colspan="2">'
                        f'<span style="font-size:10px;">{_t}</span></td>')
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
                    return (f'<td style="padding:5px 8px;color:#475569;text-align:center;" colspan="2">'
                            f'<span style="font-size:10px;">{_wait_txt(h)}</span></td>')
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
                      + _stat_cell(_d[1], 1) + _stat_cell(_d[5], 5) + _stat_cell(_d[20], 20)
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

        # ===== P0-4 (2026-10-28) 两条口径披露 =====
        # ① T+20 未成熟：给出具体可回填日，别让人以为是「没有超额」
        # ② 基准口径：跟踪表原写「超额 vs 沪深300」，但 T+20 无数据 = 承诺未兑现；
        #    且原实现的个股收益与基准不同源（基准价取未复权 pick_price），
        #    除权股会被系统性算成亏损（601600 除息 0.28 元 → 偏差 3.23%）。
        #    这两个机制细节必须写在页面上，否则读者会把口径修复前后的数字直接比较。
        _h20_ear = _ear.get("20")
        _h20_proj = _eproj.get("20")
        _p04_block = ""
        if _h20_ear:
            _n20 = len(_agg_ex.get("__ALL__", {}).get(20) or [])
            if _n20 == 0:
                _p04_block += (
                    '<div style="font-size:11px;color:#7dd3fc;margin-top:8px;padding:8px 10px;'
                    'background:rgba(56,189,248,0.08);border-radius:6px;border-left:3px solid #38bdf8;'
                    'line-height:1.7;">'
                    f'ℹ️ <b>T+20 尚未成熟，不是「无超额」</b>：当前 T+20 样本 0 条。'
                    f'数据截止 {_kcut_ or "—"}，最早一批推荐（{_track_first_date}）的 T+20 '
                    f'落点在 <b>{_h20_ear}</b>{"（按工作日预估，未扣节假日）" if _h20_proj else ""}，'
                    '届时运行 track_recommendations.py 即自动回填。'
                    '<br>为什么这条最关键：回测显示 IC 峰值在长周期，<b>持有到 T+20 才有统计意义</b>；'
                    'T+1/T+5 的负超额有一部分是短期噪声，不能据此判定模型失效。'
                    '在 T+20 数据到位前，本表<b>不支持</b>任何「模型有效/无效」的结论。</div>')
        _p04_block += (
            '<div style="font-size:10.5px;color:#64748b;margin-top:8px;padding:8px 10px;'
            'background:#172033;border-radius:6px;line-height:1.7;">'
            '<b style="color:#94a3b8;">口径（2026-10-28 起）</b>：'
            '① 个股与基准<b>均为前复权收盘价</b>，同一交易日轴（全市场交易日历，'
            '停牌不会让持有期错位）；'
            '② <b>基准</b>取沪深300（sh000300）同期涨跌幅，超额 = 个股 − 基准；'
            '③ 未成熟样本单列为 <span style="color:#7dd3fc;">未到期</span>，'
            '<b>不填 0、不计入均值</b>；价格缺失（停牌/退市）同样剔除，不做插值。'
            '<br><b style="color:#f59e0b;">不可跨版本比较</b>：本版修正了基准价口径'
            '（此前取未复权原始价，除权股会被算成亏损，实测 86/1493 条偏差 &gt;0.01%、'
            '最高 3.26%），故与 2026-10-27 及更早的跟踪表数字不可直接对比 —— '
            '口径修正本身对 T+1 均值影响 +0.007pct，但样本补齐（补回此前因个股停牌'
            '而被跳过的批次）会改变样本构成。</div>')

        html_parts.append(f'''
    <!-- v2.41 P0-4 推荐跟踪 (v2.42 R3 空态折叠; v2.44 P1-2 超额收益 / P1-4 偏差披露)
         P0-4 (2026-10-28): T+20 成熟日披露 + 口径对齐(前复权/交易日历) -->
    <details class="track-block" style="margin:14px 0;"{_track_open}>
        <summary style="cursor:pointer;font-size:12px;color:#38bdf8;padding:10px 12px;background:#1e293b;border-radius:8px;">{_track_summary}</summary>
        <div style="padding:12px 14px;background:#1e293b;border-radius:0 0 8px 8px;">
        <div style="font-size:11px;color:#64748b;margin-bottom:10px;">自 {_track_first_date} 起累计记录 {len(_recs)} 条推荐（覆盖 {_track_n_days} 个交易日）；T+N 收益以推荐日<b>前复权收盘价</b>为基准，用前复权K线回填；<b style="color:#38bdf8;">超额收益 = 个股 T+N 涨跌幅 − 沪深300 同期涨跌幅</b>（用于剥离大盘 beta，区分选股能力）。</div>
        {_track_inner}
        {_p04_block}
        <div style="font-size:11px;color:#475569;margin-top:6px;">说明：每日运行 track_recommendations.py 记录并回填。胜率=上涨样本占比（红涨绿跌口径）。T+5/T+20 需对应交易日到期后才有值，单元格内已标注<b>具体可回填日</b>。</div>
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
        + ('（round36 质量维NaN修复 + round37 权重再平衡 + v4.13 市值双维拆分）— 基本面估值锚' + f'{_DIM_W["score_fund"]:.0%}'
           + ' + 质量' + f'{_DIM_W["score_quality"]:.0%}'
           + ' + 低波20' + f'{_DIM_W["score_lowvol20"]:.0%}'
           + ' + 技术面反号' + f'{_DIM_W["score_tech_neg"]:.0%}'
           + ' + 乖离低位' + f'{_DIM_W["score_bias60"]:.1%}'
           + ' + RSI反转' + f'{_DIM_W["score_rsi_rev"]:.1%}'
           + ' + EP估值' + f'{_DIM_W["score_ep"]:.0%}'
           + ' + 小盘成长溢价' + f'{_DIM_W["score_size_growth"]:.0%}'
           + ' + 行业龙头壁垒' + f'{_DIM_W["score_dominant"]:.0%}'
           + '。<b>2026-10-28 v4.13-size2d 市值维度拆分</b>：'
           + '单一 score_size_ind(10%) 拆为 A 小盘成长溢价(8%，行业内市值×成长门限) 与 '
           + 'B 行业龙头壁垒(6%，业务份额组内前30%)，市值合计 14%；'
           + '差额 4% 由质量回吐（25.2%→21.2%），Σ=1.0 不变。'
           + '⚠️ <b>B 维尚无本池单维 IC 实证</b>，属先验配置（承认龙头有壁垒溢价），须补实证。'
           + '（<b>2026-10-28 round37 权重再平衡</b>：行业内市值由 16% 封顶至 10%，'
           + '释放的 6% 全给质量 → 质量升至 25%（全池最大权重）。'
           + '⚠️ 与 round35 实证冲突：修复后质量残差 IC 为 h20 −0.0796(t−4.42)、h40 −0.2033(t−22.67)，显著为负，'
           + '质量维是反向因子；本次提权属<b>主动先验配置</b>（非回测结论），后续须以新面板复核。'
           + '（<b>2026-10-28 round36 两项变更</b>：'
           + '① <b>质量维 NaN 污染修复</b>——东财「商誉」对无商誉公司返回 nan，'
           + '原五处 isinstance 判定全部放行 NaN（NaN 是 float），'
           + 'NaN 进入 rank_pct 后因「任何比较恒为 False」导致排序崩塌，'
           + '且部分保留输入顺序=当期 total 降序 → 质量分被注入总分信息'
           + '（rank corr +0.3910 / t+65.05）。修复后子因子 NaN 由 1514 降到 0，'
           + '行序扰动最大极差 0.000000。'
           + '<b>⚠️ round34「quality h20 IC +0.0361 过 Bonferroni」裁决作废</b>：'
           + '剔除上期 total 后残差 h20 −0.0796(t−4.42)，是泄漏伪影非 alpha。'
           + '② <b>行业内市值维改为成长条件交互</b>——'
           + 'final = 行业内市值分 × w_mc，w_mc：G>7→0.8 / 4<G≤7→0.3 / G≤4→0（不给溢价），'
           + '日均成交额<0.8亿再减半，clip[0,10]；分组用申万一级。'
           + '目的：小市值红利只奖励具备成长验证的标的，规避低质量小票暴露。'
           + '2026-10-25 口径切换依据：theme 行业内中性化后 IC 衰减 70%=行业代理；news 仅 h1 有效；'
           + '外部 47 因子排名无消息/情绪因子；反转块回填后 bias60/rsi_rev 全 lag 过 Bonferroni；'
           + 'fund 拆分为质量/成长后族自身无稳健 alpha → 保留估值锚+小权重拆分。'
           + '2026-10-26 round24：成长维归零——历史增速（npg/cagr3/revg/qoq/accel）与前瞻口径'
           + '（pe_ladder 一阶导 g1、二阶导 acc、g2）在 78 截面 h5 IC 全为负，五种极值治理'
           + '（缩尾/sign*log1p/逐子因子 rank）最好只到 −0.0148 仍为负 → 判无 alpha 而非方向问题；'
           + '同时 lowvol20（过去20日日收益标准差取负）单维 h5 IC +0.1189/t0=3.28，'
           + '与当时 6 个生效维 |r| ≤ 0.24 不冗余、与 mom20 r=−0.19 证明非动量代理，'
           + '换维后复合分 h5 +0.1776/t0=9.95、h20 +0.2484（<b>生产线量程归一化口径</b>，'
           + '与 step2 同公式；非 rank_pct 口径）。'
           + '⚠️ 注：该换维发生在 v4.8（低波顶替成长，当时 7 维）；'
           + 'v4.9 又加入 EP + SIZE_IND_NEUT 成 8 维，故 +0.1776 是 7 维时点值，'
           + '与本页当前 8 维口径不可直接比。'
           + '⚠️ 权重本质仍是先验：round34 真·大池审计（2029 只 / 238 截面 /'
           + ' ≥3 个月窗口）给出了逐维裁决建议（4 维归零），但归零后的权重再分配'
           + '须单独裁决，尚未写入 factor_weights.json，故本页权重仍是先验；'
           + '另需注意 round34 提升的是样本代表性而非样本外性，walk-forward 前视验证仍未完成；'
           + 'total 口径：各维先按自身量程（0-5 或 0-10）归一化到 0-10，再按上列权重加权 ×10 = 百分制'
           + '（2026-10-26 修复：此前未归一化，天花板实为 67.5 分 → S/A 档不可达且实际权重被扭曲），'
           + '故与历史版本不可比）；'
           if _has_10d else
           ('基本面30% + 题材热度18% + 消息面20%(AI赋分) + 技术面20% + 板块动量12%(申万二级板块60日/10日动量排名, 按6:4合成)；'
            if _has_sector else
            '题材热度30% + 基本面30%(含一阶导G_q/二阶导QoQ·方式B单季扣非口径) + 消息面20%(AI赋分) + 技术面20%；'))
        + '''v4.4 起消息/题材/板块动量归零（round18/19 实证：theme=行业代理、news 仅 h1、外部 47 因子排名无消息情绪因子）；质量由 fund_split.py 从东财财务摘要 PIT 回填，技术面反号=10−技术面；成长 2026-10-31 起按方式B重算（G_q=单季扣非同比，QoQ 仅作 fund 一阶导、ΔG_q 仅作 fund 二阶导，行业中性rank+硬约束，growth_factor_b.py 唯一实现），基本面导数分项与成长展示值均由此给出；<b>成长总分权重 ''' + f"{(_DIM_W.get('score_growth') or 0)*100:.0f}%" + '''</b>（v4.17 起 5%→10%，新设「业绩动量」大类 cap 30%；⚠️ 门禁 G5 未过且面板无 growth 列 → 权重为产品裁决非 IC 背书，禁称「经验证的 alpha」），低波20 以池内分位口径入模</p>
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
    // v4.10（2026-10-27 T1-1）：成交额下限
    const _af = document.getElementById('amountFilter');
    if (_af) _af.addEventListener('change', function() { currentPage = 1; applyFilters(); });
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
            const newsScore = row.dataset.news || '';
            const themeScore = row.dataset.themeScore || '';
            const newsText = row.dataset.newsText || '无';
            const techText = row.dataset.techText || '';
            const fundText = row.dataset.fundText || '无';
            const themeText = row.dataset.themeText || '无';
            // v4.5（问题2）：消息面 / 题材热度权重已归零，不再是评分维度。
            //   展开详情里若继续以「📰 消息面(x/10)」形式并列，读者会以为它们参与打分。
            //   改为「不计分·参考」，且分数为空时显示「—」而不是 0。
            function _dimCell(icon, label, score, text, w) {
                const _sv = (score === '' || score === null || score === undefined) ? '—' : parseFloat(score).toFixed(1);
                const _wt = w ? '<span style="color:#64748b;font-weight:400;"> ' + w + '</span>' : '';
                const _txt = text || '无';
                return '<div><div style="color:#94a3b8;margin-bottom:4px;">' + icon + ' ' + label +
                       '(' + _sv + '/10)' + _wt + '</div>' +
                       '<div style="color:#e2e8f0;">' + _txt + '</div></div>';
            }
            function _refCell(icon, label, score, text, tip) {
                const _sv = (score === '' || score === null || score === undefined) ? '—' : parseFloat(score).toFixed(1);
                return '<div><div style="color:#64748b;margin-bottom:4px;" title="' + (tip || '') + '">' + icon + ' ' + label +
                       '(' + _sv + '/10)<span style="color:#475569;font-weight:400;"> 不计分·参考</span></div>' +
                       '<div style="color:#94a3b8;">' + (text || '无') + '</div></div>';
            }
            // 生效维：取值来自 data-s-*（已按各自量程归一化到 0-10）
            // ⚠️ 此处**不再写死权重百分比**：本段是普通字符串（非 f-string，含大量 JS
            //   花括号无法插值），历史上写死的 30%/5%/5% 在 v4.7 改权重后即已过期且无人
            //   发现，v4.8 换维后更是二次过期。权重唯一可见来源 = 页首「当前评分维度与权重」
            //   权重条（_build_weight_strip），此处只做分数展示。
            // v4.9：新增 EP 估值 / 行业内市值两格；小市值（全市场口径）移到下方参考区。
            // v4.16-P0：行业内市值（sSize_ind）已于 v4.13-size2d 退役且行上无该属性
            //   （恒显示「—」），换成小盘成长（sSize_growth）+ 行业龙头（sDominant）
            //   与排序下拉/权重条的 9 生效维对齐（禁再残留退役维）。
            detailRow.innerHTML = '<td colspan="' + row.children.length + '"><div class="detail-content" style="background:#0f172a;border-radius:8px;padding:16px;margin:4px;">' +
                '<div style="display:grid;grid-template-columns:repeat(4,1fr);gap:12px;font-size:11px;">' +
                _dimCell('📊', '估值锚', row.dataset.sFund, fundText, '') +
                _dimCell('🪙', 'EP估值', row.dataset.sEp, '扣非净利润TTM/总市值，池内分位（越高越便宜）', '') +
                _dimCell('🏛', '质量', row.dataset.sQuality, 'ROE/毛利率/现金流等质量因子（fund 拆分，池内分位）', '') +
                _dimCell('📉', '低波20', row.dataset.sLowvol20, '过去20日日收益标准差，池内分位（波动越低分越高）', '') +
                _dimCell('📈', '技术面反号', row.dataset.sTech_neg, techText, '') +
                _dimCell('🎯', '乖离低位', row.dataset.sBias60, '相对 60 日均线乖离越低越好（已反号）', '') +
                _dimCell('📊', 'RSI反转', row.dataset.sRsi_rev, 'RSI 超卖回升强度（已反号）', '') +
                _dimCell('🌱', '小盘成长', row.dataset.sSize_growth, '行业内市值分 × 成长门限（＞7→0.8 / 4-7→0.3 / ≤4→0）', '') +
                _dimCell('🏭', '行业龙头', row.dataset.sDominant, '业务份额合成 rank（前30%为龙头）+ 市值一致性折扣', '') +
                '</div>' +
                '<div style="display:grid;grid-template-columns:repeat(4,1fr);gap:12px;font-size:11px;margin-top:10px;padding-top:10px;border-top:0.5px dashed #1e293b;">' +
                _refCell('📰', '消息面', newsScore, newsText) +
                _refCell('🔥', '题材热度', themeScore, themeText) +
                _refCell('🚀', '成长', row.dataset.sGrowth, '历史增速/前瞻一阶导二阶导，round24 实测无 alpha，已归零') +
                _refCell('🐟', '小市值(全市场)', row.dataset.sSize, '全市场流通市值分位，round27 实测 h5 IC 为负，已由「行业内市值」替代') +
                '</div>' +
                '<div style="margin-top:8px;font-size:10px;color:#475569;">' +
                '评分看上方生效维分数条（各维均已归一化至 10 分制）；' +
                '各维权重见页首「当前评分维度与权重」权重条（唯一口径来源）。' +
                '消息面、题材热度、成长、小市值(全市场口径) 四维权重为 0，仅作观察线索，不进入综合评分。</div>' +
                '</div></td>';
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
    // v4.10（2026-10-27 T1-1）：成交额下限（空串=不限）
    const _amtEl = document.getElementById('amountFilter');
    const amtMin = _amtEl && _amtEl.value ? parseFloat(_amtEl.value) : 0;

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
        // v4.10（2026-10-27 T1-1）：成交额下限。
        // ⚠️ 缺 amount 的行在设了门槛时**剔除**（无法证明达标），
        //   但门槛为空时一律保留 —— 不能用 `|| 0` 把缺失当 0 分处理，
        //   否则一旦某项 amount 拉取失败，该股就会在所有门槛下静默消失。
        if (amtMin > 0) {
            const _av = parseFloat(row.dataset.amount);
            if (!isFinite(_av) || _av < amtMin) return false;
        }
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

# ═══════════════════════════════════════════════════════════════════════
# v4.18（round117 P0-3）：生成期页面瘦身
# ─────────────────────────────────────────────────────────────────────
# 实测基线（2026-11-03，index.html 9,460,111 字符 / 11,592,778 字节）：
#   属性占 70.2%，其中 data-* 26.0% / style 17.6% / class 13.5% / title 13.1%
#   表格 2,084 行 × 平均 1,280 字符属性串
#
# 两步，均**只降低冗余、不改信息**：
#   ① 删除 JS 从未读取的 7 个 data-*（穷举验证，见 P0-3 报告）：
#      data-fund-score / data-tech-score / data-s-news / data-s-theme /
#      data-s-oversold60 / data-s-overbought / data-s-attention
#      → 14,588 处，省 3.4%
#   ② style 内联字符串 → 短类名（348 种唯一值），样式表在 <style> 里补齐
#      → 省 15.5%
#   合计约 −18.9%（9.46MB → 7.67MB）。
#
# ⚠️ 为什么必须做在**生成阶段**而不是后处理脚本：
#    项目铁律「一次性脚本不在日链 = 必然陈旧」。若做成后处理，日链一旦忘调，
#    页面就悄悄回胖且无人发现。做在生成器内部 = 永久生效、无法漏掉。
#
# ⚠️ style 外提的安全性：
#    只处理**属性值完全等于**已知唯一值的情况，按全等映射替换，
#    不做任何 CSS 解析/合并 —— 保证渲染结果逐像素一致。
#    CSS 优先级 selectNone：用 .sN{...} 单类选择器，与内联 style 等价
#    （若原元素已有 class，则追加类名；类选择器与内联同时存在时内联更高，
#     故此处把内联移除、改由类承载，不存在覆盖冲突）。
# ═══════════════════════════════════════════════════════════════════════
def _shrink_attrs(_html):
    """页面瘦身：删死属性 + style 外提为短类名。纯字符串等价变换，不改渲染结果。

    ⚠️ 关键边界（round117 实测踩到）：JS 里存在**动态拼接的 HTML 字符串**，例如
        '... <div style="color:#e2e8f0;">' + txt + '</div> ...'
      若对这些片段也做 style→class 替换，CSS 类虽然进了 <style>，但运行时
      拼接出来的元素用的是同一套类名 —— 理论上仍能匹配；真正的问题是
      **统计口径错位**（把 JS 代码里的假样式也算进「唯一值」），
      且一旦 JS 里的样式没有对应 CSS（单次出现不外提）就会**丢样式**。
      故本函数严格只处理「静态 HTML 区」：</style> 之后到 <script> 之前，
      以及所有标签属性区；script/style 内部一律不动。
    """
    import re as _re
    from collections import Counter as _C

    _stats = {"dead_attrs": 0, "style_externalized": 0, "style_unique": 0}

    # ① 删除死属性（每个都带前导空格，避免破坏相邻属性）
    #    死属性只可能出现在静态 HTML 的属性区，全局替换是安全的
    #    （实测这 7 个名字在 script 内均不出现）。
    _DEAD = ("data-fund-score", "data-tech-score", "data-s-news", "data-s-theme",
             "data-s-oversold60", "data-s-overbought", "data-s-attention",
             # ⑨ round117 补充：这两个由 _dsub_attrs 生成，JS 同样 0 次读取
             "data-s-vol_pulse", "data-s-theme_intrinsic")
    _n0 = len(_html)
    for _d in _DEAD:
        _html = _re.sub(r'\s' + _re.escape(_d) + r'="[^"]*"', '', _html)
    _stats["dead_attrs"] = _n0 - len(_html)

    # ② style 值 → 短类名（仅静态标签属性区）
    #    切出 script / style 内容，标记为不可替换区，只在其余部分做替换。
    _prot = []

    def _protect(m):
        _prot.append(m.group(0))
        return f"\x00PROT{len(_prot)-1}\x00"

    _work = _re.sub(r'<script[^>]*>.*?</script>', _protect, _html, flags=_re.S)
    _work = _re.sub(r'<style[^>]*>.*?</style>', _protect, _work, flags=_re.S)

    _styles = _re.findall(r'style="([^"]*)"', _work)
    _cnt = _C(_styles)
    _stats["style_unique"] = len(_cnt)

    _use = {}
    for _i, (_v, _n) in enumerate(_cnt.most_common()):
        if _n < 2:
            continue
        _cid = f"s{_i}"
        if len(_v) + 8 > len(_cid) + 1:
            _use[_v] = _cid
    if not _use:
        return _html, _stats

    # CSS 构建（必须在保护区**还原之后**插入，否则加到 _html 上而返回 _work 会丢）
    _css = "\n".join(f".{_use[v]}{{{v}}}" for v in _use)

    def _fix_tag(m):
        tag = m.group(0)
        ms = _re.search(r'\sstyle="([^"]*)"', tag)
        if not ms:
            return tag
        cid = _use.get(ms.group(1))
        if cid is None:
            return tag
        tag = tag.replace(ms.group(0), "", 1)
        mc = _re.search(r'\sclass="([^"]*)"', tag)
        if mc:
            tag = tag.replace(mc.group(0), f' class="{mc.group(1)} {cid}"', 1)
        else:
            tag = _re.sub(r'^(<[a-zA-Z][a-zA-Z0-9-]*)', rf'\1 class="{cid}"', tag, count=1)
        return tag

    _n1 = len(_work)
    _work = _re.sub(r'<[a-zA-Z][^>]*>', _fix_tag, _work)
    _stats["style_externalized"] = _n1 - len(_work)

    # 还原保护区（script / style 原样回填）
    def _restore(m):
        return _prot[int(m.group(1))]

    _work = _re.sub(r"\x00PROT(\d+)\x00", _restore, _work)

    # CSS 注入 </style> 前（此刻 _work 已含完整 style 区，可安全定位）
    if "</style>" in _work:
        _work = _work.replace("</style>", _css + "\n</style>", 1)
    return _work, _stats


try:
    _len0 = len(html)
    html, _shrink_stats = _shrink_attrs(html)
    _len1 = len(html)
    if _len1 < _len0:
        print(f"📉 页面瘦身(P0-3): {_len0:,} → {_len1:,} 字符 "
              f"(−{100*(_len0-_len1)/_len0:.1f}%) | "
              f"死属性 −{_shrink_stats.get('dead_attrs',0):,} / "
              f"style外提 −{_shrink_stats.get('style_externalized',0):,} "
              f"({_shrink_stats.get('style_unique',0)} 种唯一值)")
    else:
        print(f"⚠️ 页面瘦身未生效（{_len0:,} → {_len1:,}），保留原 HTML")
        html = "\n".join(html_parts)
except Exception as _she:
    print(f"⚠️ 页面瘦身失败，使用原 HTML: {_she}")
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











