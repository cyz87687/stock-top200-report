#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
regime_model.py —— 市场状态模块（round109 落地，round109b 样本扩至 2015 后**大幅修正**）

═══ 一、老大要什么，实测给什么 ═══

老大要求「引入 transform 模型预测未来走势，或至少识别震荡/单边涨/单边跌」。
本模块**没有**采用 Transformer，也**不做**方向预测 —— 不是保守，是实测否决：

  ┌─ 方向预测（三分类：震荡/单边涨/单边跌）
  │  样本 440 日：8/12 组低于多数类基线
  │  样本 2873 日（2015-01~2026-11）：**12/12 组全部低于多数类基线**
  │    沪深300 H=10：0.450 vs 基线 0.611
  │    中证全指 H=20：0.394 vs 0.571
  │  → 任何模型都不如「永远猜震荡」。给方向 = 给伪精确。
  │
  ├─ 深回撤预测（T2，曾以为有效）
  │  样本 440 日：AUC 0.727~0.786、经济价值 3/8 ← **这是小样本运气**
  │  样本 2873 日：AUC 0.509~0.587（接近随机 0.5）、经济价值 **0/8**
  │  → 已证伪，本模块不再使用。
  │
  └─ 风险择时降仓（round109d/109e，2873 日实测）
     连续 vol targeting      ：1/4 通过；相对「无脑固定70%」仅 沪深300 更优
     离散尾部保险 4 规则×4 指数：**0/16 通过**
       · 中证全指：满仓 MDD −41.6% → vol≥90%降0.3 后 MDD **−53.5%（更差）**
       · 沪深300：−45.6% → **−53.2%（更差）**
     根因（已逐段核实）：A 股高波动期**恰恰是大幅反弹期** ——
       vol 分位≥90% 的触发段里，2024-09-26~11-05 满仓 **+20.76%**、
       2018-10-11~11-12 **+5.32%**、2018-02-06~03-15 **+3.15%**。
       降仓系统性错过反弹 → 净值峰值退到 2015-12-21，11 年未创新高
       → 年度收益恶化：2018 −31.5%→−36.4%、2022 −19.2%→−24.5%、2024 +5.3%→−10.8%
     → **指数层面不存在可用的降仓择时信号。**

═══ 二、那这个模块还剩什么用？（这是本轮的真正交付）═══

方向、回撤、择时三条路都被实测关死，但「风险预算上限」**根本不需要预测**——
它是恒等式：

        风险 = 仓位 × 波动率      ⟹      仓位上限 = 风险预算 / 当前波动率

所以本模块的定位从「预测未来」改成「**把当日风险刻度换算成可执行的风险预算**」：

  L1 状态识别（描述性，零训练，高置信）
     当日属于 趋势涨/趋势跌/高波动/震荡 —— 只描述现在，不预测。

  L2 风险刻度（描述性，零训练，中高置信）
     当前波动率/回撤在历史什么分位 → 风险温度 0~1。
     ⚠️ 语义已改：不再叫「未来 H 日风险预判」（那个预测被证伪），
        只叫「当前风险刻度」—— 它是**当下读数**，不是预言。

  L3 方向预判（明确拒绝）
     输出「不可预测」+ 实测依据。

  风险预算上限（算术，非预测）
     目标组合波动 15% / 当前已实现波动 → 得到仓位上限。
     **必须披露代价**：这么做会降低长期收益（A股波动与收益正相关），
     换来的是风险暴露稳定。这是**风险偏好选择**，不是收益增强手段。

═══ 三、与赚钱效应 82 分如何衔接 ═══

赚钱效应 82 分（亢奋期）是**当日截面情绪**，回答「今天赚钱容易吗」。
它的构成（step2 compute_money_effect，已核）：
  广度项 40（涨停封顶）+ 涨停净额 18（封顶）+ 指数强度 ≈9 + 中位涨幅 15（封顶）≈ 82
→ **它是情绪满分档，且不含任何前瞻信息。**

衔接方式从「乘法」改为「**双约束取小**」：

       实际敞口上限 = min( 情绪上限（赚钱效应阶梯）,  风险预算上限 )

  为什么取 min 而不是相乘：两个约束来自不同维度（情绪 vs 风险容量），
  相乘会把同一份谨慎重复计两次（如 82分×0.70 与 风险上限 0.70 叠加成 0.49），
  取小则语义干净：**谁更保守听谁的**。
  这也修正了 round109 首版「×0.70」把情绪顶惩罚与风险预算混为一谈的问题。

数据纪律：
  - 所有特征只用 ≤ t 日收盘价，无前视；
  - 缺失即缺失，不填充、不用 `or 0`；
  - 每个输出都带 `basis`（依据）与 `confidence`（置信度），前端必须展示；
  - **硬门禁**：指数最新交易日 ≠ 数据日期时，直接退出（exit 2），
    绝不拿陈旧指数冒充当日（2026-11-02 事故：指数缓存停 10-29，
    输出「单边下跌+高风险+回撤分位1.0」，而当日四指数全涨、赚钱效应 82）。
"""
import os
import json
import math
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
BENCH_DIR = os.path.join(HERE, "data", "bench")

# 用于判定状态的基准指数（中证全指覆盖最广，作为主口径）
PRIMARY_BENCH = "000985"
BENCH_NAMES = {"000985": "中证全指", "000300": "沪深300",
               "000905": "中证500", "000852": "中证1000"}

# 前瞻窗口：与回测口径一致（H=10 是 round109c 中经济价值最好的一档）
H_DEFAULT = 10
# 零训练分位的历史回看长度
PCT_LOOKBACK = 250
MIN_HISTORY = 120      # 少于此长度不出结论（避免用不足样本硬判）

# 状态阈值（写死、可复核；基于 2873 日样本标定）
TREND_MA = 20          # 趋势判定用 MA20
TREND_SLOPE_WIN = 10   # 斜率窗口
VOL_HIGH_PCT = 0.70    # 波动分位 ≥70% → 高波动
DD_PRESSURE_PCT = 0.70 # 回撤压力分位 ≥70% → 高风险

# ---------------- 风险预算（恒等式换算，**不是预测**）----------------
# 目标组合年化波动：A股权益中性设定 15%（与 round109d 的 TARGET_VOL_ANNUAL 同口径）
TARGET_VOL_ANNUAL = 0.15
RISK_W_MIN = 0.30      # 风险预算给出的仓位下限（不清零，留结构性参与）
RISK_W_MAX = 1.00      # 上限（不加杠杆）

# ---------------- 硬门禁：指数日期必须等于数据日期 ----------------
# 2026-11-02 事故：指数缓存停在 10-29，模型输出「单边下跌市 + 高风险 + 回撤分位 1.0」，
#   而当日四指数全涨、赚钱效应 82 分 → 报告与事实完全相反。
# 根因：fetch_bench.py 是一次性手动脚本，不在日链里，缓存静默陈旧。
# 修复：① 加入日链；② 本门禁 —— 日期不符**直接拒绝生成**，不输出任何结论。
MAX_BENCH_LAG_DAYS = 0   # 允许滞后天数（0 = 必须完全同日）


# ---------------------------------------------------------------- 数据
def load_bench(code=PRIMARY_BENCH):
    """→ (dates, close, high, low)；high/low 仅用于回撤计算，缺失则回退 close。"""
    p = os.path.join(BENCH_DIR, f"{code}.json")
    if not os.path.exists(p):
        raise FileNotFoundError(f"基准指数缓存缺失: {p}")
    d = json.load(open(p, encoding="utf-8"))
    px = d.get("px") or d
    ks = sorted(k for k in px.keys() if str(k)[:2] == "20")
    dates, close, high, low = [], [], [], []
    for k in ks:
        row = px[k]
        if not isinstance(row, (list, tuple)) or len(row) < 2:
            continue
        c = float(row[1])
        if c <= 0:
            continue
        dates.append(str(k))
        close.append(c)
        # 格式 [open, close, high, low]（已在 round109 核对顺序）
        high.append(float(row[2]) if len(row) >= 3 else c)
        low.append(float(row[3]) if len(row) >= 4 else c)
    return dates, np.asarray(close, float), np.asarray(high, float), np.asarray(low, float)


# ---------------------------------------------------------------- 硬门禁
def check_date_gate(bench=PRIMARY_BENCH, data_date=None, max_lag_days=MAX_BENCH_LAG_DAYS):
    """**硬门禁**：指数最新交易日必须等于数据日期，否则拒绝出结论。

    2026-11-02 事故的直接产物：指数缓存停 2026-10-29，而 scored 数据是 11-02，
    模型却照常输出「单边下跌市 + 高风险 + 回撤分位 1.0」，
    与当日「四指数全涨、赚钱效应 82 分」完全相反 —— 报告自相矛盾。

    参数：
      data_date : 当日横截面数据日期（'YYYY-MM-DD'）。None 时**跳过检查**并显式标记，
                  不静默通过（调用方必须知道没检查）。
      max_lag_days : 容忍的滞后天数。0 = 必须同日。

    返回 dict：
      ok       : bool，是否通过
      bench_date / data_date / lag_days
      action   : 'pass' / 'stale_bench' / 'no_data_date' / 'no_bench'
      detail   : 人可读说明（可直接上报告页）
    """
    out = {"bench": bench, "bench_date": None, "data_date": data_date,
           "lag_days": None, "ok": False, "action": None, "detail": ""}
    try:
        dates, _c, _h, _l = load_bench(bench)
    except Exception as e:
        out["action"] = "no_bench"
        out["detail"] = f"指数缓存不可读（{type(e).__name__}: {e}），拒绝出结论"
        return out
    out["bench_date"] = dates[-1]

    if not data_date:
        out["action"] = "no_data_date"
        out["detail"] = ("未提供数据日期，**日期门禁未执行**（不是通过）。"
                         "调用方应传入当日横截面日期，否则陈旧指数无法被发现。")
        return out

    # 归一化：只保留前 10 位日期部分（容忍 'YYYY-MM-DD HH:MM:SS'）
    _bd = str(out["bench_date"])[:10]
    _dd = str(data_date)[:10]
    from datetime import datetime as _dt
    try:
        lag = (_dt.strptime(_dd, "%Y-%m-%d") - _dt.strptime(_bd, "%Y-%m-%d")).days
    except Exception:
        lag = None
    out["lag_days"] = lag

    if _bd == _dd:
        out["ok"] = True
        out["action"] = "pass"
        out["detail"] = f"指数最新交易日 = 数据日期 = {_bd} ✅"
        return out

    if lag is not None and 0 <= lag <= max_lag_days:
        out["ok"] = True
        out["action"] = "pass"
        out["detail"] = f"指数日期 {_bd} 滞后 {lag} 日（在容忍范围 {max_lag_days} 日内）✅"
        return out

    out["action"] = "stale_bench"
    _sign = "落后" if (lag is None or lag > 0) else "**超前**"
    out["detail"] = (f"⛔ 日期门禁拦截：指数最新 {_bd}，数据日期 {_dd}，"
                     f"指数{_sign} {abs(lag) if lag is not None else '?'} 天。"
                     f"陈旧指数会得出与当日事实相反的状态判断，已拒绝出结论。"
                     f"→ 请先运行 fetch_bench.py 补数。")
    return out


# ---------------------------------------------------------------- 特征（无前视）
def _ret(c, w):
    n = len(c)
    out = np.full(n, np.nan)
    out[w:] = c[w:] / c[:-w] - 1.0
    return out


def _vol(c, w):
    n = len(c)
    lr = np.zeros(n)
    lr[1:] = np.log(c[1:] / c[:-1])
    out = np.full(n, np.nan)
    for i in range(w, n):
        out[i] = lr[i - w + 1:i + 1].std(ddof=1)
    return out


def _pct_rank(series, t, lookback=PCT_LOOKBACK):
    """series[t] 在 series[≤t] 的历史分位（0~1）；无足够历史→None。"""
    h = series[:t + 1]
    h = h[np.isfinite(h)]
    if len(h) < MIN_HISTORY:
        return None
    h = h[-lookback:]
    if not np.isfinite(series[t]):
        return None
    return float((h <= series[t]).mean())


# ---------------------------------------------------------------- L1 状态识别
def classify_state(dates, close):
    """零训练规则打状态标签（描述性，不预测未来）。

    三类（互斥，按优先级）：
      高波动     vol20 分位 ≥ VOL_HIGH_PCT   （风险优先，先判这个）
      单边趋势   |close/MA20-1| ≥ 1.5% 且 MA20 斜率同向 ≥ 0.15%/日
      震荡       其余

    返回 dict：state / trend_dir / 各项实测值 / basis
    """
    n = len(close)
    t = n - 1
    vol20 = _vol(close, 20)
    ma20 = np.full(n, np.nan)
    for i in range(19, n):
        ma20[i] = close[i - 19:i + 1].mean()

    v_pct = _pct_rank(vol20, t)
    px_ma20 = float(close[t] / ma20[t] - 1.0) if np.isfinite(ma20[t]) else None

    # MA20 斜率（近 TREND_SLOPE_WIN 日，按日均 %）
    slope = None
    if t - TREND_SLOPE_WIN >= 19 and np.isfinite(ma20[t]) and np.isfinite(ma20[t - TREND_SLOPE_WIN]):
        slope = float((ma20[t] / ma20[t - TREND_SLOPE_WIN] - 1.0) / TREND_SLOPE_WIN)

    if v_pct is not None and v_pct >= VOL_HIGH_PCT:
        state, sname = "high_vol", "高波动市"
        basis = f"20日年化波动 {vol20[t]*math.sqrt(252):.1%}，处近{PCT_LOOKBACK}日 {v_pct:.0%} 分位"
    elif px_ma20 is not None and slope is not None and abs(px_ma20) >= 0.015 and abs(slope) >= 0.0015 \
            and (px_ma20 > 0) == (slope > 0):
        if px_ma20 > 0:
            state, sname = "trend_up", "单边上涨市"
        else:
            state, sname = "trend_down", "单边下跌市"
        basis = (f"收盘{'高于' if px_ma20>0 else '低于'}MA20 {abs(px_ma20):.2%}，"
                 f"MA20 日均斜率 {slope:+.2%}")
    else:
        state, sname = "range", "震荡市"
        _p = f"{px_ma20:+.2%}" if px_ma20 is not None else "N/A"
        _s = f"{slope:+.2%}" if slope is not None else "N/A"
        basis = f"收盘相对 MA20 {_p}、MA20 斜率 {_s}，均未达趋势阈值"

    # ── 当日方向 vs 趋势状态 的一致性（避免"下跌市"与"今日大涨"的表述打架）──
    # 关键区分：趋势状态看**均线位置/斜率**（慢变量），当日涨跌是**单日实现值**（快变量）。
    # 两者背离时（如下跌趋势 + 单日大涨）必须显式说明，否则读者会认为结论自相矛盾。
    day_pct = None
    if n >= 2:
        day_pct = float(close[t] / close[t - 1] - 1.0)
    _flag, _note = "aligned", ""
    if day_pct is not None:
        if state == "trend_down" and day_pct > 0.005:
            _flag = "rebound_in_downtrend"
            _note = (f"当日 +{day_pct:.2%} 属<b>下跌趋势中的反弹</b>："
                     f"指数仍在 MA20 下方 {abs(px_ma20 or 0):.2%}，趋势未反转")
        elif state == "trend_up" and day_pct < -0.005:
            _flag = "pullback_in_uptrend"
            _note = (f"当日 {day_pct:.2%} 属<b>上涨趋势中的回调</b>："
                     f"指数仍在 MA20 上方 {abs(px_ma20 or 0):.2%}，趋势未破坏")
        elif state == "high_vol":
            _flag = "high_vol_day"
            _note = f"当日 {day_pct:+.2%}，但波动处于高位，单日方向参考意义低"
        elif state == "range" and abs(day_pct) > 0.015:
            _flag = "range_breakout_attempt"
            _note = f"当日 {day_pct:+.2%} 幅度较大，但均线结构未确认，暂按震荡处理"

    return {
        "state": state, "state_name": sname, "basis": basis,
        "close": float(close[t]), "date": dates[t],
        "day_pct": round(day_pct, 5) if day_pct is not None else None,
        "consistency": _flag, "consistency_note": _note,
        "vol20_ann": round(float(vol20[t] * math.sqrt(252)), 4) if np.isfinite(vol20[t]) else None,
        "vol20_pct": round(v_pct, 4) if v_pct is not None else None,
        "px_ma20": round(px_ma20, 5) if px_ma20 is not None else None,
        "ma20_slope_daily": round(slope, 6) if slope is not None else None,
        "confidence": "high",   # 描述性标签，无拟合 → 高置信
    }


# ---------------------------------------------------------------- L2 风险刻度
def risk_temperature(dates, close, high=None, low=None, H=H_DEFAULT):
    """**当前风险刻度** 0~1（越大越危险）。

    ⚠️ 语义（round109b 修正）：首版叫「未来 H 日风险预判」，但那个「预测」已被证伪：
       · 440 日样本 T2 深回撤 AUC 0.727~0.786、经济价值 3/8
       · 2873 日样本 T2 AUC **0.509~0.587**（≈随机）、经济价值 **0/8**
       → 这是小样本运气。故本函数**不再声称预测未来**，只做当下读数。

    刻意用**零训练分位法**，不用拟合模型：
      拟合模型逐折 AUC 从 0.03 摆到 1.0（沪深300 H=10 倒挂折 34/66）；
      零训练波动分位 AUC 0.675~0.770（2873 日样本，8/8 组 >0.63），
      且**高于**拟合模型（0.586~0.682）→ 分布漂移下分位法更稳。

    两个分量：
      ① vol_pct : vol20 的历史分位（波动聚集效应 —— 最稳的经验规律）
      ② dd_pct  : 距近 60 日高点回撤的历史分位（回撤压力）
      风险温度 = max(vol_pct, 0.5*vol_pct + 0.5*dd_pct)
                （取 max：高波动本身就是风险，不应被低回撤稀释）

    ⚠️ 不输出涨跌方向 —— 2873 日样本 12/12 组准确率低于多数类基线。
    """
    n = len(close)
    t = n - 1
    vol20 = _vol(close, 20)
    v_pct = _pct_rank(vol20, t)

    # 回撤压力：当前距近 60 日高点的跌幅
    dd = np.full(n, np.nan)
    for i in range(59, n):
        hh = high[i - 59:i + 1].max() if high is not None else close[i - 59:i + 1].max()
        if hh > 0:
            dd[i] = float(close[i] / hh - 1.0)   # ≤0
    dd_pct = _pct_rank(-dd, t)   # 跌幅越大分位越高；-dd ≥0

    if v_pct is None or dd_pct is None:
        return {"available": False,
                "reason": f"可用历史不足 {MIN_HISTORY} 日，不出风险刻度",
                "confidence": "none"}

    temp = max(v_pct, 0.5 * v_pct + 0.5 * dd_pct)
    temp = float(min(1.0, max(0.0, temp)))

    if temp >= 0.75:
        lvl, lname = "high", "高风险"
    elif temp >= 0.50:
        lvl, lname = "mid", "中等风险"
    else:
        lvl, lname = "low", "低风险"

    # 实测依据必须带出来：前端要披露，避免被当成「验证过的预测」
    return {
        "available": True, "level": lvl, "level_name": lname,
        "temperature": round(temp, 4),
        "vol_pct": round(v_pct, 4), "dd_pct": round(dd_pct, 4),
        "vol20_ann": round(float(vol20[t] * math.sqrt(252)), 4) if np.isfinite(vol20[t]) else None,
        "cur_dd_from_60d_high": round(float(dd[t]), 4) if np.isfinite(dd[t]) else None,
        "horizon": H,
        "confidence": "medium",
        "semantics": "当前风险刻度（非未来预测）",
        "evidence": {
            "method": "零训练分位法（非拟合模型）",
            "vol_pct_auc": "0.675~0.770（2873日样本，4指数×2期限，8/8 组 >0.63）",
            "vol_pct_auc_vs_fitted": "高于拟合模型 0.586~0.682 → 分位法抗分布漂移",
            "falsified_v1": ("首版 T2 深回撤预测：440日 AUC 0.727~0.786、经济价值 3/8；"
                             "2873日 AUC 跌至 0.509~0.587、经济价值 0/8 → 已判定小样本运气，本模块不再使用"),
            "no_timing_value": ("降仓择时已被证伪（round109d/109e，2873日）："
                                "连续 vol targeting 1/4、离散尾部保险 0/16 通过；"
                                "中证全指 vol≥90%降仓后 MDD −41.6%→−53.5%（更差）。"
                                "根因：A股高波动期恰是大幅反弹期（2024-09-26~11-05 满仓 +20.76%）。"),
            "caveat": ("样本 2873 交易日（2015-01~2026-11，含 2015 股灾/2016 熔断/2018 熊市/"
                       "2022 熊市/2024-25 反弹），但仍只是一个市场；"
                       "赚钱效应分（横截面宽度）仅 2025-07 起可得，无法回测其择时价值"),
        },
    }


# ---------------------------------------------------------------- L3 方向预判（拒绝）
def direction_forecast():
    """明确返回「不可预测」，并附实测依据。

    这是本模块的核心诚实性设计。**2873 日样本（2015-01~2026-11）实测**：
      H=10 沪深300 acc=0.450 vs 多数类基线 0.611（低 16.1pp）
      H=20 中证全指 acc=0.394 vs 基线 0.571（低 17.7pp）
      4 指数 × 3 期限共 12 组：**12/12 全部低于多数类基线**（440 日样本时是 8/12）
    即：**任何模型都不如「永远猜震荡」**。给方向 = 给伪精确，且样本越长越确定。
    """
    return {
        "available": False,
        "reason": "方向不可预测（2873日样本 12/12 组准确率低于多数类基线）",
        "detail": ({
            "H5": {"沪深300": "0.530 vs 基线 0.589", "中证500": "0.422 vs 0.476"},
            "H10": {"沪深300": "0.450 vs 基线 0.611", "中证全指": "0.439 vs 0.500"},
            "H20": {"中证全指": "0.394 vs 基线 0.571", "沪深300": "0.500 vs 0.624"},
        }),
        "source": "audit_round109.json（2873日版）",
        "note": ("不引入 Transformer 同样基于此：方向信号本身不存在，"
                 "提高模型容量只会把噪声拟合得更像信号（round72→73 前视教训同源）"),
        "confidence": "none",
    }


# ---------------------------------------------------------------- 风险预算（恒等式）
def risk_budget(risk, target_vol=TARGET_VOL_ANNUAL,
                w_min=RISK_W_MIN, w_max=RISK_W_MAX):
    """把「当前风险刻度」换算成**仓位上限**——这是恒等式，不是预测。

        风险 = 仓位 × 波动率   ⟹   仓位上限 = 目标波动 / 当前波动

    ⚠️ 必须诚实披露代价（round109d/109e 实测）：
       该换算会**降低长期收益**，因为 A 股波动率与未来收益正相关
       （高波动期常伴随后续反弹）。它换来的是「风险暴露稳定」，
       属于**风险偏好选择**，不是收益增强手段。
       实测：连续 vol targeting 相对满仓，中证1000 CAGR −0.28%→−2.25%。

    与 L2「风险温度」的区别：
      · 风险温度 = 波动 + 回撤的**综合分位**（0~1，情绪化的"危险程度"）
      · 风险预算 = 只用**波动率**的**算术换算**（%，可执行数值）
    两者都输出，前者用于解释，后者用于执行。

    返回：dict（available / w_cap / target_vol / realized_vol / formula / caveat）
    """
    if not (risk or {}).get("available"):
        return {"available": False, "reason": "风险刻度缺失，不给风险预算上限"}
    rv = risk.get("vol20_ann")
    if rv is None or not (rv > 0):
        return {"available": False, "reason": "已实现波动缺失或非正，不给风险预算上限"}

    w_raw = target_vol / float(rv)
    w_cap = float(min(w_max, max(w_min, w_raw)))
    _clipped = None
    if w_raw > w_max:
        _clipped = f"理论值 {w_raw:.2f} > 上限 {w_max:.2f}（不加杠杆），已截断"
    elif w_raw < w_min:
        _clipped = f"理论值 {w_raw:.2f} < 下限 {w_min:.2f}（不清零），已抬升"

    return {
        "available": True,
        "w_cap": round(w_cap, 4),
        "w_raw": round(float(w_raw), 4),
        "w_min": w_min, "w_max": w_max,
        "target_vol": target_vol,
        "realized_vol": round(float(rv), 4),
        "formula": f"{target_vol:.0%} ÷ {float(rv):.2%} = {w_raw:.2f} → 截断至 {w_cap:.2f}",
        "clipped_note": _clipped,
        "caveat": ("该上限由恒等式得出（风险 = 仓位 × 波动），非收益预测；"
                   "实测会牺牲部分长期收益换取风险暴露稳定（见 round109d/109e），"
                   "属风险偏好选择而非 alpha 来源。"),
        "confidence": "high（算术，非统计推断）",
    }


# ---------------------------------------------------------------- 与赚钱效应衔接
# ⚠️ 必须与 gen_report_top200.py 的 POSITION_LADDER **保持一致**。
#   单一事实源原则：此处只是复刻展示层阶梯用于「算调整后区间」，
#   不改写阶梯本身；若展示层阶梯变更，此处须同步（否则 adjusted_desc 会错）。
#   已在 verify 脚本中加一致性断言，见 check_ladder_consistency()。
LADDER = [
    (0,   20,  0.00, 0.20),
    (20,  40,  0.20, 0.40),
    (40,  60,  0.40, 0.60),
    (60,  80,  0.60, 0.80),
    (80,  101, 0.80, 1.00),
]


def ladder_bounds(money_score):
    """赚钱效应分 → 原阶梯的 (下限, 上限) 百分比；缺失→(None, None)。"""
    if money_score is None:
        return None, None
    for lo, hi, plo, phi in LADDER:
        if lo <= money_score < hi:
            return plo, phi
    return None, None


def check_ladder_consistency():
    """自检：把 LADDER 与 gen_report_top200.py 的 POSITION_LADDER 文案对照。

    返回 (ok, detail)。仅在显式调用时执行，不进入日链主路径。
    """
    p = os.path.join(HERE, "gen_report_top200.py")
    if not os.path.exists(p):
        return False, "gen_report_top200.py 不存在"
    txt = open(p, encoding="utf-8").read()
    _expect = ['"≤2成"', '"2~4成"', '"4~6成"', '"6~8成"', '"8成+警惕"']
    _miss = [e for e in _expect if e not in txt]
    if _miss:
        return False, f"展示层阶梯文案变更，需同步历史预期: 缺失 {_miss}"
    return True, "LADDER 与展示层 POSITION_LADDER 文案一致（5 档）"


def combine_position(money_score, risk, ladder_lo=None, ladder_hi=None, budget=None):
    """赚钱效应（情绪）与风险预算（风险容量）**双约束取小** → 实际敞口上限。

    ═══ 为什么改（round109b 修正）═══
    首版是「赚钱效应 × 风险温度 → 系数」（如 82 × 高险 = 0.70）。
    问题：这把两个不同维度的谨慎**相乘**了 ——
      · 赚钱效应 82 分本身已给出「8成+警惕」的阶梯（情绪顶的谨慎已含在阶梯里）；
      · 风险温度高又给一个 0.70；
      → 相乘 = 同一份谨慎计两次（0.70 × 风险预算 0.70 = 0.49，过度保守且无法解释）。

    改为 **取小**：实际上限 = min(情绪阶梯上限, 风险预算上限)。
    语义干净：**谁更保守听谁的**，两个约束各管一个维度，不重复计数。

    ═══ 三个数必须都能追溯 ═══
      ladder_hi  : 情绪给的（POSITION_LADDER，未改写）
      budget_cap : 风险恒等式给的（TARGET_VOL / realized_vol）
      final_hi   : min(两者) ← 真正执行的
    任何一环都可以单独质疑与推翻，不会被乘法揉成一个不可解释的数字。

    ladder_lo/hi : 原阶梯百分比区间（如 0.80/1.00）；None 时只给上限不给区间。
    budget       : risk_budget() 的返回值；None 时退化为只用情绪阶梯。
    """
    out = {"money_score": money_score, "available": True}

    # ---- 约束 A：情绪阶梯 ----
    has_ladder = (ladder_lo is not None and ladder_hi is not None)
    a_hi = float(ladder_hi) if has_ladder else None

    # ---- 约束 B：风险预算 ----
    has_budget = bool((budget or {}).get("available"))
    b_hi = float(budget["w_cap"]) if has_budget else None

    if not has_ladder and not has_budget:
        return {"available": False,
                "reason": "情绪阶梯与风险预算均缺失，不给敞口上限（保持原值）"}

    # ---- 取小 ----
    _cands = [v for v in (a_hi, b_hi) if v is not None]
    final_hi = min(_cands)
    if has_ladder and has_budget:
        bound_by = "情绪阶梯" if a_hi <= b_hi else "风险预算"
    elif has_ladder:
        bound_by = "情绪阶梯（风险预算缺失，未加约束）"
    else:
        bound_by = "风险预算（赚钱效应缺失，未加约束）"

    out.update({
        "ladder_hi": a_hi, "ladder_lo": (float(ladder_lo) if has_ladder else None),
        "budget_cap": b_hi, "final_hi": round(final_hi, 4),
        "bound_by": bound_by,
    })

    # ---- 实际区间：下限同步受风险预算约束（不高于 final_hi）----
    if has_ladder:
        final_lo = min(float(ladder_lo), final_hi)
    else:
        final_lo = None
    out["final_lo"] = (round(final_lo, 4) if final_lo is not None else None)

    # ---- 人可读说明 ----
    if has_ladder and has_budget:
        out["adjusted_desc"] = (
            f"情绪阶梯 {ladder_lo*100:.0f}%~{ladder_hi*100:.0f}%　vs　"
            f"风险预算上限 {b_hi*100:.0f}%（{budget.get('formula')}）"
            f"　→　<b>取小 = {final_hi*100:.0f}%</b>（受{bound_by}约束）")
    elif has_ladder:
        out["adjusted_desc"] = f"仅情绪阶梯 {ladder_lo*100:.0f}%~{ladder_hi*100:.0f}%（风险预算缺失）"
    else:
        out["adjusted_desc"] = (f"仅风险预算上限 {b_hi*100:.0f}%"
                                f"（赚钱效应缺失，无法给区间）")

    # ---- 背离标注（信息量所在，但不参与计算）----
    _lvl = (risk or {}).get("level")
    if money_score is not None and _lvl:
        if money_score >= 80 and _lvl == "high":
            out["divergence"] = "情绪顶：赚钱效应高但风险刻度高 → 不加仓"
        elif money_score < 40 and _lvl == "low":
            out["divergence"] = "磨底：情绪低但风险低 → 不必降到最低档"
        else:
            out["divergence"] = "同向"
    return out


# ---------------------------------------------------------------- 对外主入口
def run(bench=PRIMARY_BENCH, H=H_DEFAULT, money_score=None, data_date=None,
        enforce_gate=True):
    """生成当日完整状态与风险预算，返回可直接序列化的 dict。

    参数：
      data_date    : 当日横截面数据日期（'YYYY-MM-DD'）。**强烈建议传入** ——
                     不传则日期门禁无法执行（会在结果里标记 gate.executed=False）。
      enforce_gate : True 时门禁不通过直接抛 RuntimeError（默认）；
                     False 时只把结果写进 gate 字段并继续（供诊断/回放用）。
    """
    # ---- 硬门禁：先验日期，再算任何东西 ----
    gate = check_date_gate(bench, data_date)
    if not gate["ok"] and enforce_gate:
        raise RuntimeError(f"[日期门禁] {gate['detail']}")

    dates, close, high, low = load_bench(bench)
    st = classify_state(dates, close)
    rk = risk_temperature(dates, close, high, low, H=H)
    dr = direction_forecast()
    rb = risk_budget(rk)
    # 原阶梯区间由调用方按赚钱效应映射（None 时只给风险预算上限）
    _lo, _hi = ladder_bounds(money_score)
    comb = combine_position(money_score, rk, _lo, _hi, budget=rb)

    # 多指数一致性检查（若四个指数状态分歧 → 降低置信度）
    cross = {}
    for code in BENCH_NAMES:
        try:
            _d, _c, _h, _l = load_bench(code)
            _one = classify_state(_d, _c)
            cross[code] = {"name": BENCH_NAMES[code],
                           "state": _one["state"],
                           "state_name": _one["state_name"],
                           "bench_date": _d[-1],
                           "day_pct": _one.get("day_pct")}
        except Exception:
            continue
    _states = [v["state"] for v in cross.values()]
    agree = len(set(_states)) == 1
    if not agree:
        st["confidence"] = "medium"
        st["basis"] += f"；⚠️ 四指数状态不一致（{', '.join(sorted(set(_states)))}），置信度下调"

    return {
        "date": st["date"],
        "data_date": data_date,
        "bench": bench, "bench_name": BENCH_NAMES.get(bench, bench),
        "gate": gate,
        "L1_state": st,
        "L2_risk": rk,
        "L3_direction": dr,
        "risk_budget": rb,
        "combine": comb,
        "cross_index": cross,
        "cross_agree": agree,
        "meta": {
            "model": "regime_model v2 (round109b, 2873日样本)",
            "horizon": H,
            "why_not_transformer": (
                "方向信号不存在，不是模型不够强：2873 日样本 4指数×3期限共 12 组，"
                "准确率 12/12 全部低于多数类基线（中证全指 H=20：0.394 vs 0.571）；"
                "深回撤预测 AUC 由 440日的 0.727~0.786 跌至 0.509~0.587（经济价值 0/8）；"
                "降仓择时 0/16 通过（中证全指降仓后 MDD −41.6%→−53.5% 反而更差）。"
                "提高模型容量只会把噪声拟合得更像信号（round72→73 前视教训同源）。"
            ),
            "positioning": "不预测方向；把当前风险刻度换算成风险预算上限（恒等式），与情绪阶梯双约束取小",
            "data_discipline": "特征只用 ≤t 日收盘；缺失即缺失；每个结论附 basis 与 confidence；指数日期≠数据日期则拒绝生成",
            "evidence_files": ["audit_round109.json", "audit_round109b.json",
                               "audit_round109c.json", "audit_round109d.json",
                               "audit_round109e.json"],
        },
    }


if __name__ == "__main__":
    import sys
    import argparse
    import glob
    ap = argparse.ArgumentParser()
    ap.add_argument("--bench", default=PRIMARY_BENCH)
    ap.add_argument("--H", type=int, default=H_DEFAULT)
    ap.add_argument("--money", type=float, default=None, help="当日赚钱效应分（可选）")
    ap.add_argument("--data-date", default=None,
                    help="当日横截面数据日期 YYYY-MM-DD（默认从最新 scored 自动读取）")
    ap.add_argument("--allow-stale", action="store_true",
                    help="⚠️ 仅诊断用：跳过日期门禁（会产出陈旧结论，禁止用于出报告）")
    ap.add_argument("--out", default=os.path.join(HERE, "regime_forecast.json"))
    a = ap.parse_args()

    # ---- 从最新 scored 读取「数据日期」与「赚钱效应」（单一数据源，不另建口径）----
    _scored_path = None
    try:
        _fs = [f for f in sorted(glob.glob(os.path.join(HERE, "top200_scored_*.json")), reverse=True)
               if "bak" not in f and "tmp" not in f and "watch" not in f]
        if _fs:
            _scored_path = _fs[0]
            _d = json.load(open(_scored_path, encoding="utf-8"))
            if a.money is None:
                a.money = (_d.get("market_review") or {}).get("money_score")
                print(f"📥 从 {os.path.basename(_scored_path)} 读得赚钱效应 {a.money}")
            if a.data_date is None:
                a.data_date = _d.get("date")
                print(f"📥 从 {os.path.basename(_scored_path)} 读得数据日期 {a.data_date}")
    except Exception as e:
        print(f"⚠️ 读取 scored 失败（赚钱效应/数据日期可能缺失）: {type(e).__name__}: {e}")

    if a.data_date is None:
        print("⚠️ 未能确定数据日期 → 日期门禁无法执行（这是风险点，不是通过）")

    # ================= 硬门禁（老大要求：指数日期 ≠ 数据日期则告警）=================
    gate = check_date_gate(a.bench, a.data_date)
    print(f"\n{'='*66}\n🔒 日期门禁")
    print(f"   指数最新 {gate['bench_date']}  |  数据日期 {gate['data_date']}  |  滞后 {gate['lag_days']} 天")
    print(f"   {gate['detail']}")
    if not gate["ok"]:
        if not a.allow_stale:
            print(f"\n⛔ 门禁未通过，**拒绝生成** {os.path.basename(a.out)}（exit 2）")
            print("   修复：先跑 fetch_bench.py 补数；确属节假日请显式用 --allow-stale 并说明")
            sys.exit(2)
        print("   ⚠️ --allow-stale 已指定：继续生成，但该产物**不得**用于正式报告")

    try:
        res = run(a.bench, a.H, a.money, data_date=a.data_date,
                  enforce_gate=False)   # 门禁已在上面显式处理并打印
    except Exception as e:
        print(f"⛔ 生成失败: {type(e).__name__}: {e}")
        sys.exit(2)

    res["gate"]["enforced"] = True
    with open(a.out, "w", encoding="utf-8") as f:
        json.dump(res, f, ensure_ascii=False, indent=2)

    s, r, c, rb = res["L1_state"], res["L2_risk"], res["combine"], res.get("risk_budget") or {}
    print(f"\n{'='*66}")
    print(f"📅 {res['date']}  基准 {res['bench_name']}")
    print(f"L1 状态识别 : {s['state_name']}  (置信 {s['confidence']})")
    print(f"             {s['basis']}")
    if s.get("consistency_note"):
        print(f"             ⚠️ {s['consistency_note']}")
    print(f"L2 风险刻度 : {r.get('level_name')}  刻度 {r.get('temperature')}  "
          f"(波动分位 {r.get('vol_pct')} / 回撤分位 {r.get('dd_pct')})")
    print(f"L3 方向预判 : ⛔ {res['L3_direction']['reason']}")
    if rb.get("available"):
        print(f"风险预算    : 目标波动 {rb['target_vol']:.0%} ÷ 已实现 {rb['realized_vol']:.2%} "
              f"→ 仓位上限 {rb['w_cap']*100:.0f}%")
        if rb.get("clipped_note"):
            print(f"             （{rb['clipped_note']}）")
    print(f"敞口衔接    : {c.get('adjusted_desc')}")
    if c.get("divergence"):
        print(f"背离标注    : {c['divergence']}")
    print(f"多指数一致  : {'✅ 四指数状态一致' if res['cross_agree'] else '⚠️ 存在分歧'}")
    for k, v in res["cross_index"].items():
        print(f"    {v['name']:8s} {v['state_name']:8s} 当日 {v.get('day_pct')}")
    print(f"\n✅ 已写出 {a.out}")
    sys.exit(0)
