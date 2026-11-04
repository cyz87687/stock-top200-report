#!/usr/bin/env python3
"""
成长因子算法｜方式B（单季度扣非口径）—— 唯一实现来源
================================================================
round106 口径定稿（老大 2026-11-01 指令）：

增速口径一律使用**扣非净利润**（剔除政府补助/资产处置/公允价值变动等噪音）。
三个度量的精确定义：

  单季同比 G_q = Q_Profit / Q_Profit_ly − 1
      成长速度——衡量「增长还是下滑」
      · Q_Profit    = 本期单季度扣非归母净利润（累计自拆）
      · Q_Profit_ly = 去年同季度扣非归母净利润
      · ⚠️ 负基数（Q_Profit_ly ≤ 0）增速**无定义**（比值符号失真，敦煌 Q3 案例：
        −2701/−1872−1 = +44% 实为亏损扩大；Q2 案例 9569/−1207−1 = −893% 实为扭亏）
        → G_q = None + neg_base 留痕，方向用 turnaround(扭亏) / 双亏旗标表达，
        合成取行业中性 0.5（「无法判定」≠「最差」，对齐 ES 层负基数规则）
  一阶导 QoQ = Q_Profit / Q_Profit_prev − 1
      单季环比增速——衡量「利润环比还在增加」（level 动量）
      · Q_Profit_prev = 上一个报告期单季扣非净利（Q1 的环比 = 上年 Q4）
      · 负基数（Q_Profit_prev ≤ 0）同上：无定义 + 中性 0.5 + 留痕
  二阶导 ΔG_q = G_q(t) − G_q(t−1)（单位：百分点 pp）
      增速变化——衡量「增速在加速还是减速」，**而非**「增长还是下滑」
      · G_q(t−1) 无定义（含负基数）→ 二阶导无定义，deriv2 中性 3.0 留痕

  Q_Profit 自累计口径自拆（东财财务摘要「扣非净利润」为年内累计）：
      Q1 = 累计(Q1)                     Q2 = 累计(H1) − 累计(Q1)
      Q3 = 累计(Q1~3) − 累计(H1)        Q4 = 累计(FY) − 累计(Q1~3)

前置清洗（执行顺序固定）
  1. 基础剔除：单季推不出来（缺累计档）→ 因子得分 = 0（不参与缩尾/rank）；
     负基数 → 不剔除，走「无定义 + 中性 + 留痕」通道（见上）
  2. 异常缩尾：G_q、QoQ、ΔG_q 分别 1%/99% Winsorize
  3. 行业中性 Rank：申万一级行业内百分位 rank → rank_Gq、rank_qoq、rank_accel ∈ [0,1]
  4. 周期标记：is_cycle 走 sector/cycle_industry.json 的 l2_list 正面清单
     （老大 2026-11-02 20 个申万二级，含光伏设备）；命中者额外触发规则1/规则2 扣分
  5. 一次性收益校正：Q_Profit 本身即扣非口径（已剔除非经常性损益），天然满足

合成打分（round108 D3 路A：P1g 去重）
  Score = w1 · rank_Gq                 （QoQ/ΔG_q 只走 fund deriv1/deriv2，不再重复计量）
  STYLE_W 两档同值 (1.0, 0.0)；CYCLE_W (1.0, 0.0)
  周期股差异**不再靠权重**，改由规则1（毛利率抬升>3pp 扣 10%）与
  规则2（按期去均值 ΔG_q ≥ p80 扣 15%）承担，合计上限 25%

规则 gate（老大 2026-11-02）
  仅命中强周期标签的个股才进入校验；成长/消费/医药稳定成长标的
  完全不计算（gm_reason="not_cycle_skip"、rule_gate="non_cycle_skip"）
  · 负基数票 rank_Gq 位由中性 0.5 占位（旗标 neg_base/turnaround 留痕）
  · 二阶导不进合成分，进 deriv2 槽位（fund 0.6/0.2/0.2）+ 连两季降速约束

硬约束扣分（作用于 0-1 合成分，顺序：扣分 → 上限 → 下限）
  ① G_q < 0            → 得分上限锁定 0.4（负增长=「下滑」，无论环比/加速多高；G_q 无定义不触发）
  ② QoQ < 0            → 连续扣分：每环比 −10% 扣 0.05，上限 0.2（round105 P1-3）
  ③ 连两季降速         → 扣 0.15：连续两季 ΔG_q<0（增速连续减速，round106 按二阶导
                         精确定义实现；旧「连续两季 QoQ<0」实为环比下滑，已由②覆盖，
                         降级为审计字段 consec_qoq_neg 不再扣分）
  ④ Q_Profit < 0       → 得分上限锁定 0.3（本季亏损）
  ⑤ 得分下限 0，不出现负分

归一化输出
  约束后合成分在全股票池做百分位 rank → [0,100] = score_growth_100
  系统 0-10 量程：score_growth = score_growth_100 / 10（与 DIM_MAX=10 对齐）

⚠️ 术语（round106 定稿，取代 2026-10-31 二次修正）：
   单季同比 G_q = 同比增速（增长/下滑维度）；一阶导 = 单季环比增速（利润环比还在
   增加）；二阶导 = 同比增速之差 ΔG_q（增速加速/减速维度）。旧口径「一阶导=G_q、
   二阶导=QoQ」废除；更旧口径（累计同比、growth−cagr3 冒充加速度、ΔG_q 一度废除）
   均废除。

用法：
  python3 growth_factor_b.py --scored top200_scored_2026-10-30.json \
      [--out data/growth_b_2026-10-30.json] [--style steady|attack]
"""
import argparse
import json
import os
import sys
from datetime import datetime

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
EM_CACHE = os.path.join(HERE, "data", "fin_cache_em")
SW_MAP_PATH = os.path.join(HERE, "sector", "sw_map.json")

# 报告期顺序（年内累计序列推单季用）
_PERIOD_ORDER = ("0331", "0630", "0930", "1231")

# ---------------------------------------------------------------- 周期行业（round108 单一来源）
# ⚠️ 2026-11-02 round108：周期口径统一到 sector/cycle_industry.json，禁在本文件硬编码。
#    三个消费方（本文件合成分 / step2.is_cyclic / 规则1·2 gate）必须读同一份。
#    老口径 CYCLE_L1={"有色金属","基础化工","煤炭","石油石化","钢铁"} + CYCLE_L2={"光伏设备"}
#    已废除：整类基础化工含 172 只化学制品成长股，会把非大宗价格驱动的标的误判周期。
CYCLE_CFG_PATH = os.path.join(HERE, "sector", "cycle_industry.json")
BASELINE_PATH = os.path.join(HERE, "data", "cycle_accel_baseline.json")

_CYCLE_CFG_CACHE = None


def _load_cycle_cfg():
    """读 cycle_industry.json → (l2_set, l1_set)。缺失时退化为空集（非周期，保守不误杀）。"""
    global _CYCLE_CFG_CACHE
    if _CYCLE_CFG_CACHE is not None:
        return _CYCLE_CFG_CACHE
    try:
        cfg = json.load(open(CYCLE_CFG_PATH, encoding="utf-8"))
        l2 = set(cfg.get("l2_list") or [])
        l1 = set(cfg.get("l1_list") or [])
    except Exception as e:
        print(f"⚠️ growth_factor_b: cycle_industry.json 读取失败({e}) → 周期标记全 False")
        l2, l1 = set(), set()
    _CYCLE_CFG_CACHE = (l2, l1)
    return _CYCLE_CFG_CACHE


_BASELINE_CACHE = None


def _load_baseline():
    """读规则2 去均值基线 data/cycle_accel_baseline.json。缺失 → None（规则2 不生效并留痕）。"""
    global _BASELINE_CACHE
    if _BASELINE_CACHE is not None:
        return _BASELINE_CACHE
    try:
        _BASELINE_CACHE = json.load(open(BASELINE_PATH, encoding="utf-8"))
    except Exception as e:
        print(f"⚠️ growth_factor_b: cycle_accel_baseline.json 读取失败({e}) → 规则2 停用")
        _BASELINE_CACHE = {}
    return _BASELINE_CACHE


# 合成权重模板（算法第 3 节）
# ⚠️ round108 D3 路A（老大裁决）：P1g 去重 —— growth 合成分改**纯 rank_Gq**，
#    QoQ 不再进合成分（旧 0.3·rank_QoQ 与 enhance deriv1=rank_QoQ×5 重复计量同一 rank）。
#    一阶导话语权全部走 fund deriv1 槽位，二阶导全部走 fund deriv2 槽位。
STYLE_W = {"steady": (1.0, 0.0), "attack": (1.0, 0.0)}   # round108：两档同值（去重后单维）
CYCLE_W = (1.0, 0.0)  # round108：周期同样单维；周期差异改由规则1/2 的扣分承担

# ---- round108 规则1/2 参数（老大 2026-11-02 裁决 D1/D4/D5）----
RULE1_GM_RISE_PP = 3.0      # 规则1：近4季毛利率均值 − 前4季均值 > 3.0pp → 价格驱动
RULE1_DED = 0.10            # 规则1 扣成长合成分 10%
RULE2_DED = 0.15            # 规则2 扣 15%（round108e 单独 lift 1.59 > 规则1 的 1.38）
RULE_DED_CAP = 0.25         # 叠加上限 25%（P&A 联合 lift 1.82，但不清零）


# ---------------------------------------------------------------- PIT 截断
# ⚠️ round123（2026-11-03）P0-1 数据层改造：修复 **假 PIT**
#   实际测得：compute_growth(..., as_of=D) 在 D=2025-12-01 / 2026-04-30 /
#   2026-08-31 / 2026-10-27 四个日期上返回**完全相同**的 score_growth
#   （300 只样本差异 0 只 = 0.0%）。根因是 as_of 以前**只写进 __meta__，
#   从未参与报告期选择**：选期逻辑恒为 `sorted(cum.keys(), reverse=True)`
#   → 永远取全库最新报告期（实测 em_all 里 2026Q3 有 2682 只）。
#   ⇒ 回测 2025-12 截面时会用到 2026Q3 财报，**前视偏差约 10 个月**，
#     且这种前视会让「成长维有效」的结论系统性偏高（用了未来才知道的业绩）。
#   修复：按法定披露截止日截断（标准 PIT 口径，与 fund_split.pit_period 同源）。
#     年报/一季报 → 4/30；中报 → 8/31；三季报 → 10/31。
#   ⚠️ 必须与 fund_split.pit_period 保持一致，两处口径分叉 = 静默漂移（round17 教训）。
_PERIOD_DEADLINE = {"0331": (4, 30), "0630": (8, 31),
                    "0930": (10, 31), "1231": (4, 30)}


def _period_deadline(period):
    """报告期 → 法定披露截止日 'YYYY-MM-DD'。1231 算作**次年** 4/30。"""
    try:
        y, m = int(period[:4]), period[4:]
    except (ValueError, TypeError, IndexError):
        return None
    md = _PERIOD_DEADLINE.get(m)
    if not md:
        return None
    yy = y + 1 if m == "1231" else y
    return f"{yy:04d}-{md[0]:02d}-{md[1]:02d}"


def _pit_filter(cum, as_of, grace_days=0):
    """只保留「法定披露截止日 − grace_days ≤ as_of」的报告期。

    grace_days=0 → 严格 PIT（默认，回测必须用这个）。
    grace_days>0 → 仅用于**生产当日截面**的成长维（见 fund_split 说明：
      A 股三季报多在 10/20~10/30 实际已发布，法定截止 10/31 会白白退回中报）。
      回测**禁止**用 grace>0，否则在历史截面上重新引入前视。
    """
    if not as_of:
        return cum
    from datetime import date, timedelta
    try:
        d0 = date.fromisoformat(str(as_of)[:10])
    except (ValueError, TypeError):
        return cum
    if grace_days:
        d0 = d0 + timedelta(days=int(grace_days))
    out = {}
    for p, v in cum.items():
        dd = _period_deadline(p)
        if dd is None:
            continue
        try:
            if date.fromisoformat(dd) <= d0:
                out[p] = v
        except ValueError:
            continue
    return out


# ---------------------------------------------------------------- 单季拆分
def _prev_period(p):
    """上一个报告期（同一年内倒退一档，Q1 → 上年 Q4）。"""
    y, m = p[:4], p[4:]
    i = _PERIOD_ORDER.index(m)
    return f"{int(y) - 1}1231" if i == 0 else f"{y}{_PERIOD_ORDER[i - 1]}"


def _single_quarter(cum, period):
    """累计序列 → 单季扣非净利。cum: {period: 累计值}。缺失返回 None。"""
    v = cum.get(period)
    if not isinstance(v, (int, float)) or v != v:
        return None
    m = period[4:]
    if m == "0331":
        return float(v)
    prev = _prev_period(period)
    pv = cum.get(prev)
    if not isinstance(pv, (int, float)) or pv != pv:
        return None
    return float(v) - float(pv)


def _gq_at(cum, period):
    """单季同比增速 G_q = Q/Q_ly − 1（小数，0.11 = 11%）——round106 ratio 口径。

    返回 (g_q, q_profit, q_profit_ly, reason)；不可算时 g_q=None 并给 reason:
      no_data   —— 本期或去年同季单季推不出来（累计缺档）
      neg_base  —— 去年同季扣非净利 ≤ 0：比值增速无定义（含旧 zero_base），
                   负基数失真案例见模块 docstring；方向旗标由调用方按 q/q_ly 符号打
    """
    q = _single_quarter(cum, period)
    ly_p = f"{int(period[:4]) - 1}{period[4:]}"
    q_ly = _single_quarter(cum, ly_p)
    if q is None or q_ly is None:
        return None, q, q_ly, "no_data"
    if q_ly <= 0:
        return None, q, q_ly, "neg_base"
    return q / q_ly - 1.0, q, q_ly, None


def _qoq_at(cum, period):
    """一阶导·单季环比增速 QoQ = Q/Q_prev − 1（小数）——round106 ratio 口径。

    「利润环比还在增加」= QoQ > 0。Q1 的环比 = 上年 Q4。
    返回 (qoq, q_cur, q_prev, reason)；不可算时 qoq=None:
      no_data   —— 本期或上一报告期单季推不出来（累计缺档）
      neg_base  —— 上一报告期单季扣非净利 ≤ 0：比值环比无定义（含旧 zero_base）
    """
    q = _single_quarter(cum, period)
    q_prev = _single_quarter(cum, _prev_period(period))
    if q is None or q_prev is None:
        return None, q, q_prev, "no_data"
    if q_prev <= 0:
        return None, q, q_prev, "neg_base"
    return q / q_prev - 1.0, q, q_prev, None


def _single_quarter_metric(cum, period):
    """通用：累计值序列 → 单季值（用于营收/成本）。缺档返回 None。"""
    return _single_quarter(cum, period)


def _gm_rise_at(cum_rev, cum_cost, period, lookback=4):
    """规则1 指标：近 4 季单季毛利率均值 − 前 4 季均值（单位 pp）。

    为什么不用「毛利率波动 std」（老大原方案的替代简化口径）：
      round108c 实证 lift=0.945 < 1（命中组次期退坡 49.8% < 未命中 54.2%），方向反了。
      根因=波动混合「价格驱动」（涨价，利润虚高不可持续）与「成本扰动」
      （原料涨/停产/结构变，常是业绩低点后续会修复）→ 信号互相抵消。
    改用「毛利率水平抬升」：涨价最直接的可测证据，实测 lift 1.26 / 硬退坡 lift 1.38，
    且随阈值 2→3→5pp 单调（1.245→1.261→1.309）＝真信号。

    返回 (gm_rise_pp, gm_mean4, gm_mean4_prev, reason)
      insufficient_history —— 累计档位不足 2×lookback（上市不足两年 / 缓存不全）
                             → 规则1 对其**不生效**，不是「通过校验」（审计必须能区分）
      no_data —— 档位够但近 4 季或前 4 季有季推不出单季毛利率
                 （毛利率分母营收需 >1e4 元，防迷你营收失真）
    """
    sq_rev = {p: _single_quarter(cum_rev, p) for p in cum_rev}
    sq_cost = {p: _single_quarter(cum_cost, p) for p in cum_cost}
    gm = {}
    for p, rv in sq_rev.items():
        cs = sq_cost.get(p)
        if rv is None or cs is None or rv <= 1e4:
            continue
        gm[p] = (rv - cs) / rv * 100.0
    seq = sorted(gm.keys())
    if period not in gm:
        return None, None, None, "no_data"
    if seq.index(period) < lookback * 2 - 1:
        # 档位不够 ≠ 数据坏：这是「窗口不足以下判定」，与「毛利率推不出」必须分开留痕，
        #   否则事后审「为什么这只周期股没触发规则1」会误判成数据缺失。
        return None, None, None, "insufficient_history"
    i = seq.index(period)
    cur4 = [gm[q] for q in seq[i - lookback + 1:i + 1]]
    prev4 = [gm[q] for q in seq[i - 2 * lookback + 1:i - lookback + 1]]
    if len(cur4) < lookback or len(prev4) < lookback:
        return None, None, None, "no_data"
    m_cur = float(np.mean(cur4))
    m_prev = float(np.mean(prev4))
    return m_cur - m_prev, m_cur, m_prev, None


def _accel_at(cum, period):
    """二阶导 ΔG_q = G_q(period) − G_q(prev_period)，单位百分点 pp。

    「增速在加速还是减速」：ΔG_q > 0 = 加速（哪怕 G_q 仍为负，负收窄也是加速），
    ΔG_q < 0 = 减速（哪怕 G_q 仍为正，正增长放缓也是减速）。
    任一期 G_q 无定义（含负基数）→ 返回 (None, None, reason)。
    """
    g1, _, _, why1 = _gq_at(cum, period)
    g0, _, _, why0 = _gq_at(cum, _prev_period(period))
    if g1 is None or g0 is None:
        return None, g0, (why1 or why0 or "no_data")
    return (g1 - g0) * 100.0, g0 * 100.0, None


# ---------------------------------------------------------------- 截面计算
def _mid_rank_pct(values):
    """升序 mid-rank 百分位 → {key: [0,1]}（并列取平均秩；n=1 → 0.5）。"""
    n = len(values)
    if n == 0:
        return {}
    if n == 1:
        return {values[0][0]: 0.5}
    order = sorted(values, key=lambda t: t[1])
    out = {}
    i = 0
    while i < n:
        j = i
        while j + 1 < n and order[j + 1][1] == order[i][1]:
            j += 1
        avg = (i + j) / 2.0 / (n - 1)
        for k in range(i, j + 1):
            out[order[k][0]] = avg
        i = j + 1
    return out


def _winsorize_bounds(vals, lo=1.0, hi=99.0):
    a = np.asarray([v for v in vals if v is not None and v == v], dtype=float)
    if a.size == 0:
        return None, None
    return float(np.percentile(a, lo)), float(np.percentile(a, hi))


def _load_em_all():
    """code6 → {指标: {报告期: 值}}。优先 fund_split 的 pickle 快路径。"""
    try:
        import fund_split
        return fund_split.load_em_cache()
    except Exception as e:
        print(f"⚠️ growth_factor_b: fund_split.load_em_cache 失败（{e}），逐文件直读")
        out = {}
        if os.path.isdir(EM_CACHE):
            for fn in os.listdir(EM_CACHE):
                if not fn.endswith(".json"):
                    continue
                code = fn[:-5]
                try:
                    d = json.load(open(os.path.join(EM_CACHE, fn), encoding="utf-8"))
                except Exception:
                    continue
                ind = {}
                for r in d.get("records") or []:
                    name = r.get("指标")
                    if not name:
                        continue
                    ind[name] = {k: v for k, v in r.items()
                                 if k not in ("选项", "指标") and isinstance(v, (int, float)) and v == v}
                out[code] = ind
        return out


def _load_sw():
    """code6 → (申万一级, 申万二级)。"""
    try:
        sw = json.load(open(SW_MAP_PATH, encoding="utf-8"))
        return sw.get("l1") or {}, sw.get("l2") or {}, sw.get("l2_parent") or {}
    except Exception:
        return {}, {}, {}


def compute_growth(rows, style="steady", em_all=None, as_of=None, pit_grace=0):
    """方式B 成长因子截面计算（round106 口径）。

    rows: [{code, name?, sector_parent?}] 或 ['000001', ...]（code 可带 sh/sz/bj 前缀）
    返回 {code6: {g_q, qoq, accel_pp, neg_base, turnaround, qoq_prev, qoq_chg,
                  rank_gq, rank_qoq, rank_accel, w1, w2, composite,
                  score_growth_100, score_growth,
                  deriv1_growth_pct(=QoQ %), deriv1_score,
                  deriv2_delta_pt(=ΔG_q pp), deriv2_score, ...审计字段}}
    度量定义：G_q=单季同比(Q/Q_ly−1)；deriv1=一阶导 QoQ(Q/Q_prev−1)；
    deriv2=二阶导 ΔG_q=G_q(t)−G_q(t−1)（pp）。负基数无定义+中性留痕。

    as_of:   截面日 'YYYY-MM-DD'。**传了才做 PIT 截断**（round123 起真正生效）。
    pit_grace: 0=严格 PIT（回测必须用 0）；>0 仅用于生产当日截面（见 _pit_filter）。
    """
    if style not in STYLE_W:
        raise ValueError(f"style 须为 {list(STYLE_W)}，收到 {style!r}")
    _pit_grace = int(pit_grace or 0)


    # ---- 0) 规整输入 ----
    pool = []
    for r in rows:
        if isinstance(r, str):
            r = {"code": r}
        code = str(r.get("code") or "").strip().lower()
        if len(code) > 6 and code[:2] in ("sh", "sz", "bj"):
            code = code[2:]
        if not code:
            continue
        pool.append((code, r))
    if not pool:
        return {}

    if em_all is None:
        em_all = _load_em_all()
    sw_l1, sw_l2, sw_l2p = _load_sw()
    cycle_l2, _cycle_l1 = _load_cycle_cfg()   # round108：周期清单单一来源

    # ---- 1) 逐股：自拆单季 → G_q（单季同比）/ QoQ（一阶导）/ ΔG_q（二阶导）----
    recs = {}   # code6 -> 事实字段
    for code, row in pool:
        ind = em_all.get(code) or {}
        cum = ind.get("扣非净利润") or {}
        # 只留真实数值
        cum = {k: v for k, v in cum.items()
               if isinstance(v, (int, float)) and v == v}
        # ⚠️ round123 P0-1：**在此处做 PIT 截断**，而非选期处。
        #   原因：下游 _single_quarter / _gq_at / _qoq_at / _accel_at 全部依赖
        #   cum 这个「累计序列」，若只在选期处换 period 而 cum 仍含未来报告期，
        #   则「去年同季」(period−1年) 与「上季」取值仍可能来自未披露期
        #   → 前视从侧门漏回来。故唯一正确做法是**先把未来期整段砍掉**。
        #   实测收益：238 个交易日按严格 PIT 只分成 3 段（20250930/20260331/
        #   20260630），即财务维一年只需算 3 次 —— 印证「财务数据不需逐日回填」。
        cum = _pit_filter(cum, as_of, grace_days=_pit_grace)
        missing = None
        g_q = qoq = accel_pp = accel_prev_pp = None
        q = q_ly = q_prev_q = None
        qoq_prev = qoq_chg = None
        neg_base = turnaround = False
        consec_decel = False   # 真·连两季降速（ΔG_q<0 两季，round106 约束③）
        consec_qoq_neg = False  # 旧条件（连续两季环比下滑）仅审计留痕
        period = period_prev = None
        if not cum:
            missing = "no_data"
        else:
            # 最新「可比期」：本期与去年同季单季均可推（**含负基数**；round106 修正
            # 旧逻辑在负基数时回退旧报告期 → 敦煌 Q3 负基数逃回 Q2 的 +892% 失真值）
            for p in sorted(cum.keys(), reverse=True):
                _q, _qly = _single_quarter(cum, p), None
                _ly_p = f"{int(p[:4]) - 1}{p[4:]}"
                _qly = _single_quarter(cum, _ly_p)
                if _q is not None and _qly is not None:
                    q, q_ly, period = _q, _qly, p
                    break
            if period is None:
                missing = "no_data"
            else:
                g_q, _, _, _why = _gq_at(cum, period)
                if g_q is None and _why == "neg_base":
                    neg_base = True
                    turnaround = (q > 0)      # 扭亏：方向向好，幅度无定义
                elif g_q is None:
                    missing = _why            # no_data（理论上已排除）
                period_prev = _prev_period(period)
                qoq, _qc, q_prev_q, _wq = _qoq_at(cum, period)
                if qoq is None and _wq == "neg_base":
                    pass  # 环比无定义（上季≤0），rank 中性留痕（见合成段）
                qoq_prev, _, _, _ = _qoq_at(cum, period_prev)
                if qoq is not None and qoq_prev is not None:
                    qoq_chg = qoq - qoq_prev   # 审计留痕：环比增速的变化（不入模）
                consec_qoq_neg = (qoq is not None and qoq < 0
                                  and qoq_prev is not None and qoq_prev < 0)
                # 二阶导 ΔG_q（pp）+ 上一季 ΔG_q（连两季降速判定用）
                accel_pp, _, _ = _accel_at(cum, period)
                accel_prev_pp, _, _ = _accel_at(cum, period_prev)
                consec_decel = (accel_pp is not None and accel_pp < 0
                                and accel_prev_pp is not None and accel_prev_pp < 0)

        # 行业（申万一级优先取 sw_map，行内 sector_parent 兜底）
        l1 = sw_l1.get(code) or row.get("sector_parent") or None
        l2 = sw_l2.get(code) or None
        if not l1 and l2:
            l1 = sw_l2p.get(l2)
        # round108：周期判定统一走 cycle_industry.json 的 l2_list（光伏已纳入，D6 裁决）
        is_cycle = bool(l2 and l2 in cycle_l2)

        # ---- round108 规则1/2 原料：毛利率抬升（周期股才计算）----
        gm_rise = gm_mean4 = gm_mean4_prev = None
        gm_reason = "not_cycle_skip"      # 非周期股**完全不计算**（老大原话：直接跳过）
        if is_cycle and period:
            cum_rev = (ind.get("营业总收入") or {})
            cum_cost = (ind.get("营业成本") or {})
            cum_rev = {k: v for k, v in cum_rev.items()
                       if isinstance(v, (int, float)) and v == v}
            cum_cost = {k: v for k, v in cum_cost.items()
                        if isinstance(v, (int, float)) and v == v}
            if cum_rev and cum_cost:
                gm_rise, gm_mean4, gm_mean4_prev, gm_reason = _gm_rise_at(
                    cum_rev, cum_cost, period)
            else:
                gm_reason = "no_revenue_cost_data"

        recs[code] = {
            "code": code, "name": row.get("name"),
            "period": period, "period_prev": period_prev,
            "g_q": g_q, "qoq": qoq, "qoq_prev": qoq_prev, "qoq_chg": qoq_chg,
            "accel_pp": accel_pp, "accel_prev_pp": accel_prev_pp,
            "neg_base": neg_base, "turnaround": turnaround,
            "q_profit": q, "q_profit_ly": q_ly, "q_profit_prev_q": q_prev_q,
            "consec_decel": consec_decel, "consec_qoq_neg": consec_qoq_neg,
            "missing_reason": missing,
            "sw_l1": l1, "sw_l2": l2, "is_cycle": is_cycle,
            "gm_rise_pp": gm_rise, "gm_mean4": gm_mean4,
            "gm_mean4_prev": gm_mean4_prev, "gm_reason": gm_reason,
        }

    # ---- 2) 异常缩尾 1%/99%（只在有效样本上；G_q / QoQ / ΔG_q 分别）----
    g_vals = [v["g_q"] for v in recs.values() if v["g_q"] is not None]
    q_vals = [v["qoq"] for v in recs.values() if v["qoq"] is not None]
    a_vals = [v["accel_pp"] for v in recs.values() if v["accel_pp"] is not None]
    g_lo, g_hi = _winsorize_bounds(g_vals)
    q_lo, q_hi = _winsorize_bounds(q_vals)
    a_lo, a_hi = _winsorize_bounds(a_vals)
    for v in recs.values():
        v["g_q_w"] = v["g_q"]
        v["qoq_w"] = v["qoq"]
        v["accel_w"] = v["accel_pp"]
        v["winsorized"] = False
        if v["g_q"] is not None and g_lo is not None:
            cw = min(max(v["g_q"], g_lo), g_hi)
            if cw != v["g_q"]:
                v["winsorized"] = True
            v["g_q_w"] = cw
        if v["qoq"] is not None and q_lo is not None:
            cw = min(max(v["qoq"], q_lo), q_hi)
            if cw != v["qoq"]:
                v["winsorized"] = True
            v["qoq_w"] = cw
        if v["accel_pp"] is not None and a_lo is not None:
            cw = min(max(v["accel_pp"], a_lo), a_hi)
            if cw != v["accel_pp"]:
                v["winsorized"] = True
            v["accel_w"] = cw

    # ---- 3) 行业中性 Rank（申万一级组内百分位；缺行业 → 全池组）----
    grp_g, grp_q, grp_a = {}, {}, {}
    for code, v in recs.items():
        if v["g_q_w"] is not None:
            grp_g.setdefault(v["sw_l1"] or "__POOL__", []).append((code, v["g_q_w"]))
        if v["qoq_w"] is not None:
            grp_q.setdefault(v["sw_l1"] or "__POOL__", []).append((code, v["qoq_w"]))
        if v["accel_w"] is not None:
            grp_a.setdefault(v["sw_l1"] or "__POOL__", []).append((code, v["accel_w"]))
    rank_g, rank_q, rank_a = {}, {}, {}
    for gname, arr in grp_g.items():
        rank_g.update(_mid_rank_pct(arr))
    for gname, arr in grp_q.items():
        rank_q.update(_mid_rank_pct(arr))
    for gname, arr in grp_a.items():
        rank_a.update(_mid_rank_pct(arr))

    # ---- 4) 合成 + 硬约束（round108 D3 路A：Score = w1·rank_Gq，P1g 去重后单维）----
    # ⚠️ 旧口径 Score = 0.7·rank_Gq + 0.3·rank_QoQ，与 enhance deriv1=rank_QoQ×5 重复计量
    #    同一 rank_QoQ（round107-3 实证）。去重后 QoQ 只走 fund deriv1、ΔG_q 只走 fund deriv2。
    w_def = STYLE_W[style]
    _BASELINE = _load_baseline()      # 规则2 去均值基线
    for code, v in recs.items():
        w1, w2 = CYCLE_W if v["is_cycle"] else w_def
        v["w1"], v["w2"] = w1, w2
        v["qoq_missing_neutral"] = (v["qoq"] is None)
        if v["g_q"] is None and not v["neg_base"]:
            # 基础剔除（no_data：单季推不出累计缺档）：因子得分 = 0，不进 rank 池
            v.update({
                "in_rank_pool": False,
                "rank_gq": None, "rank_qoq": None, "rank_accel": None,
                "composite_raw": None, "composite": 0.0,
                "score_growth_100": 0.0, "score_growth": 0.0,
                "deriv1_growth_pct": None, "deriv1_score": 3.0,
                "deriv2_delta_pt": None, "deriv2_score": 3.0,
                "constraints_hit": ["missing_zero"],
            })
            continue
        # G_q 位：正常 = 行业 rank；负基数（同比增速无定义）= 中性 0.5
        #   「无法判定」≠「最差」（对齐 ES 层负基数规则）；方向用 turnaround 旗标表达
        rg = 0.5 if v["neg_base"] else rank_g.get(code, 0.5)   # 单只行业组 → 中性 0.5
        rq = rank_q.get(code, 0.5)          # QoQ 缺失（含负基数）→ 中性 0.5
        ra = rank_a.get(code)               # ΔG_q rank；无定义 → None（deriv2 中性）
        raw = w1 * rg + w2 * rq
        score = raw
        hit = []
        if v["neg_base"]:
            hit.append("neg_base_neutral_turnaround" if v["turnaround"]
                       else "neg_base_neutral")
        # ② QoQ 连续扣分（round105 P1-3，2026-10-31 取代旧悬崖「<−0.3 才扣 0.2」）：
        #    每环比 −10% 扣 0.05、上限 0.2（≈−40% 封顶）；作用于原始 QoQ，非缩尾值。
        #    旧悬崖实测逃逸案例：巨人 qoq=−0.295 差 0.5pp 零惩罚（round105）。
        if v["qoq"] is not None and v["qoq"] < 0:
            _qded = min(0.2, (-v["qoq"]) * 0.5)
            score -= _qded
            hit.append(f"qoq_cont_ded_{_qded:.2f}")
            if v["qoq"] < -0.3:
                hit.append("qoq_lt_-0.3")   # 旧标记保留供历史对齐/审计
        # ③ 连两季降速（round106 二阶导精确定义）：连续两季 ΔG_q<0（增速连续减速）→ 扣 0.15。
        #    旧「连续两季 QoQ<0」实为环比下滑（由②覆盖），降级审计字段 consec_qoq_neg。
        if v["consec_decel"]:
            score -= 0.15
            hit.append("consec_decel")
        if v["consec_qoq_neg"]:
            hit.append("consec_qoq_neg_audit")
        # ① G_q < 0 → 上限 0.4（「下滑」维度；负基数无定义不触发，双亏票由④锁）
        if v["g_q"] is not None and v["g_q"] < 0:
            score = min(score, 0.4)
            hit.append("gq_neg_cap_0.4")
        # ④ 本季亏损 → 上限 0.3
        if v["q_profit"] is not None and v["q_profit"] < 0:
            score = min(score, 0.3)
            hit.append("q_loss_cap_0.3")

        # ===== round108 规则1/2：仅强周期股生效（正面清单 gate，非周期直接跳过）=====
        # 老大 2026-11-02：「只要个股命中强周期标签，下面整套校验逻辑才生效；
        #   成长赛道、消费、医药稳定成长标的直接跳过本惩罚模块」
        r1_hit = r2_hit = False
        r_ded = 0.0
        dm = None
        if v["is_cycle"]:
            # ---- 规则1 盈利持续性校验（价格驱动 → 扣 10%）----
            # 口径=毛利率水平抬升（非波动 std；round108c 实测 std 口径 lift 0.945 方向反了）
            if v["gm_rise_pp"] is not None and v["gm_rise_pp"] > RULE1_GM_RISE_PP:
                r1_hit = True
                r_ded += RULE1_DED
                hit.append(f"r1_price_driven_gm+{v['gm_rise_pp']:.1f}pp")
            # ---- 规则2 二阶导可持续性惩罚（高加速 → 扣 15%）----
            # 口径=按期去均值后的 ΔG_q ≥ 周期组 p80（基线 data/cycle_accel_baseline.json）
            # 实证依据 round108b：去均值后 corr(ΔG_q_t,ΔG_q_t+1) 周期 −0.5528 vs 非周期 −0.0211
            if v["accel_pp"] is not None:
                _wz = _BASELINE.get("winsor") or [None, None]
                if _wz[0] is not None:
                    _a = float(np.clip(v["accel_pp"], _wz[0], _wz[1]))
                else:
                    _a = v["accel_pp"]
                _mu = (_BASELINE.get("mu_by_period") or {}).get(v["period"])
                if _mu is None:
                    _mu = _BASELINE.get("global_mu_fallback")
                if _mu is not None:
                    dm = _a - float(_mu)
            _p80 = _BASELINE.get("p80_demeaned_cycle")
            if dm is not None and _p80 is not None and dm >= _p80:
                r2_hit = True
                r_ded += RULE2_DED
                hit.append(f"r2_high_accel_dm{dm:.0f}pp")
            if r_ded > RULE_DED_CAP:
                hit.append(f"rule_ded_capped_{RULE_DED_CAP:.2f}")
                r_ded = RULE_DED_CAP
            if r_ded > 0:
                score = score * (1.0 - r_ded)
        # ⑤ 下限 0
        score = max(score, 0.0)
        v.update({
            "in_rank_pool": True,
            "rank_gq": rg, "rank_qoq": rq, "rank_accel": ra,
            "composite_raw": raw, "composite": score,
            "constraints_hit": hit,
            # deriv1 = 一阶导（单季环比增速，%）；deriv2 = 二阶导（ΔG_q = 同比增速之差，pp）
            "deriv1_growth_pct": (v["qoq"] * 100.0) if v["qoq"] is not None else None,
            "deriv1_score": round(rq * 5.0, 2),          # 0-5，对齐 new_fund 量程
            "deriv2_delta_pt": v["accel_pp"],
            "deriv2_score": round(ra * 5.0, 2) if ra is not None else 3.0,
            # ---- round108 规则留痕 ----
            "rule1_hit": r1_hit, "rule2_hit": r2_hit,
            "rule_ded_pct": round(r_ded, 4),
            "accel_demeaned_pp": round(dm, 2) if dm is not None else None,
            "rule_gate": ("cycle_on" if v["is_cycle"] else "non_cycle_skip"),
        })

    # ---- 5) 全池百分位 rank → [0,100]（no_data 股已固定 0，不进 rank 池）----
    pool_vals = [(c, v["composite"]) for c, v in recs.items()
                 if v.get("in_rank_pool")]
    fin = _mid_rank_pct(pool_vals)
    for code, v in recs.items():
        if not v.get("in_rank_pool"):
            continue
        s100 = fin.get(code, 0.0) * 100.0
        v["score_growth_100"] = round(s100, 2)
        v["score_growth"] = round(s100 / 10.0, 2)   # 0-10 量程（DIM_MAX=10）

    # ---- 元信息 ----
    n_valid = sum(1 for v in recs.values() if v.get("in_rank_pool"))
    # ⚠️ round123 修复：原实现硬编码 `v.get("period") == "20260930"`，
    #   回测历史截面时该计数恒为 0（历史截面按 PIT 不可能取到 2026Q3），
    #   会让「三季报口径 N 只」这条披露在回测里静默失真。
    #   改为「本期实际生效报告期的分布」，与 as_of 同步。
    _pc = {}
    for v in recs.values():
        p = v.get("period")
        if p:
            _pc[p] = _pc.get(p, 0) + 1
    _latest_p = max(_pc) if _pc else None
    n_q3 = _pc.get(_latest_p, 0) if _latest_p else 0
    n_neg = sum(1 for v in recs.values() if v.get("neg_base"))
    n_turn = sum(1 for v in recs.values() if v.get("turnaround"))
    meta = {
        "algo": "growth_factor_b (方式B·单季度扣非口径, round106)",
        "style": style, "w_default": w_def, "w_cycle": CYCLE_W,
        "as_of": as_of or datetime.now().strftime("%Y-%m-%d"),
        "n_pool": len(recs), "n_valid": n_valid,
        "n_q3_period": n_q3,
        "n_neg_base": n_neg, "n_turnaround": n_turn,
        # round123 P0-1：PIT 执行情况必须落盘，否则「有没有前视」无法事后复核。
        "pit": {
            "enabled": bool(as_of),
            "grace_days": _pit_grace,
            "mode": ("strict" if _pit_grace == 0 else "actual_ok"),
            "note": ("报告期按法定披露截止日截断（年报/一季报→4/30，中报→8/31，"
                     "三季报→10/31）；grace>0 仅限生产当日截面，回测必须 0"),
            "period_dist": _pc,
            "effective_latest_period": _latest_p,
        },
        "winsorize": {"g_q": [g_lo, g_hi], "qoq": [q_lo, q_hi], "accel_pp": [a_lo, a_hi]},
        "g_q_def": "Q/Q_ly - 1 (单季同比; 负基数无定义+中性, round106 ratio 口径)",
        "deriv1_def": "qoq = Q/Q_prev - 1 (一阶导·单季环比增速)",
        "deriv2_def": "accel = G_q(t) - G_q(t-1) (二阶导·同比增速之差, pp)",
    }
    out = dict(recs)
    out["__meta__"] = meta
    return out


# ---------------------------------------------------------------- CLI
def main():
    ap = argparse.ArgumentParser(description="成长因子·方式B（单季度扣非口径）")
    ap.add_argument("--scored", required=True, help="scored json（取池内 code/name/sector_parent）")
    ap.add_argument("--out", default=None, help="审计输出 json 路径")
    ap.add_argument("--style", default="steady", choices=["steady", "attack"])
    ap.add_argument("--top", type=int, default=15, help="打印 Top N")
    args = ap.parse_args()

    data = json.load(open(args.scored, encoding="utf-8"))
    results = data.get("results") or []
    rows = [{"code": r.get("code"), "name": r.get("name"),
             "sector_parent": r.get("sector_parent")} for r in results]

    t0 = datetime.now()
    gf = compute_growth(rows, style=args.style, as_of=data.get("date"))
    meta = gf.pop("__meta__")
    print(f"✅ 方式B 成长因子完成: {meta['n_valid']}/{meta['n_pool']} 只有效"
          f"（三季报口径 {meta['n_q3_period']} 只）耗时 {(datetime.now()-t0).total_seconds():.1f}s")

    top = sorted((v for v in gf.values() if v.get("score_growth") is not None),
                 key=lambda x: -x["score_growth_100"])[:args.top]
    print(f"\nTop{args.top}（score_growth_100）：")
    for v in top:
        gq = v.get("g_q")
        d1 = v.get("deriv1_growth_pct")
        d2 = v.get("deriv2_delta_pt")
        nb = " 负基数" if v.get("neg_base") else ""
        print(f"  {v.get('name') or v['code']:8s} {v['code']}  "
              f"score={v['score_growth_100']:6.1f}  "
              f"G_q={'n/a' if gq is None else f'{gq*100:+.0f}%'}  "
              f"QoQ(一阶)={'n/a' if d1 is None else f'{d1:+.0f}%'}  "
              f"ΔG_q(二阶)={'n/a' if d2 is None else f'{d2:+.0f}pp'}  "
              f"({v.get('period')}, {'周期' if v['is_cycle'] else '非周期'}{nb})")

    if args.out:
        os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
        json.dump({"meta": meta, "rows": gf},
                  open(args.out, "w", encoding="utf-8"),
                  ensure_ascii=False, indent=1)
        print(f"\n📄 审计文件: {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
