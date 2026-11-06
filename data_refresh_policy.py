#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""data_refresh_policy.py —— 分层数据更新频率的**单一判据**（老大 2026-11-06 裁决③）。

## 裁决原文
    「股票池周五更新；财务数据每个财报日之后更新，如三季报公布完后不再更新；
      行情数据每日更新」

这不是「独立页日更 / 周更」的二选一，而是**按数据性质分层的更新频率总纲**：
不同数据有各自的自然更新周期，不该一刀切每天全量重算。

## 三类数据的日历级判定
    类别        频率            判据
    POOL        每周五          config.json → schedule.pool_rerun_weekday（默认 4 = 周五）
    FINANCIAL   财报披露窗口内  按法定披露截止日生成的窗口；窗口关闭即冻结
                按披露事件驱动  （个股级重算由 fin_factor_cache 的失效条件负责，本模块只做调度层）
    MARKET      每交易日        周末不更新；**法定节假日不在此表内**，由链路
                                check_data_fresh.py 的指数最新日期门禁兜底

## ⚠️ 口径纪律（勿违反）
1. **不重复实现报告期口径**：法定披露截止日直接复用 `growth_factor_b._PERIOD_DEADLINE`
   / `_period_deadline`（与 `fund_split.pit_period`、`build_panel_long` 同源）。
   本项目血泪铁律：同一口径两处实现必然静默漂移（round17 教训）。
2. **不编造节假日表**：A 股交易日历必须有真实数据源；本模块只做「周末」粗判，
   工作日一律由调用方注入 `is_trade_day`，或由 `check_data_fresh` 的真实指数日期门禁裁决。
   缺数据宁可显式说明，禁止 `or 0` 式兜底。
3. **本模块只判定、不执行、不写盘**。它是纯函数 + CLI，接入链路是 P1/C 组的事，
   接入前不得擅自改变 `run_daily_local.sh` 的实际调度（会改变每日产物行为）。
4. **与 schedule_guard.py 的关系**：`schedule_guard.plan_mode()` 是「整条链路 full /
   quote_only」的权威判定（含 15:00 收盘时刻、周末补跑、环境变量强制开关）；
   本模块是「按数据类别分层」的日历级判据。二者同源（都读 config.schedule），
   但职责不同：`plan()` 的 `mode` 把三类判定重新汇总成链路模式，供接线时交叉校验。

## 现存实现审计（2026-11-06 实测）
    ✅ config.json → schedule：规则已写入（2026-10-26 需求③）
    ✅ schedule_guard.py：周五 full / 其余 quote_only 判定已实现
    ✅ fin_factor_cache.py：按 (code, 报告期) 增量缓存，新财报才重算（需求①）
    ✅ step2_stockscorer_v2.py:4403：已接入上述缓存
    ❌ run_daily_local.sh：**零调用** schedule_guard，每天无条件全量跑 30+ 步
    ❌ .github/workflows/daily-refresh.yml：**零调用** run_optimized.py，同样每天全量
    ❌ run_optimized.py：唯一编排了调度的入口，但没有任何链路调用它
    ⇒ 规则「设计完备、生产零接入」。本模块把口径收敛到单一入口，接线仍需裁决后实施。

## 用法
    python data_refresh_policy.py                       # 今日计划
    python data_refresh_policy.py --at 2026-11-09       # 指定日（举例用）
    python data_refresh_policy.py --week                # 一周七天逐日判定
"""
import argparse
import json
import os
import sys
from datetime import date, datetime, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG = os.path.join(HERE, "config.json")

POOL = "POOL"
FINANCIAL = "FINANCIAL"
MARKET = "MARKET"
DATA_CLASSES = (POOL, FINANCIAL, MARKET)

# 口径唯一来源：与 fund_split.pit_period / build_panel_long 同源，勿在别处另立。
try:
    import growth_factor_b as _gfb
    _PERIOD_DEADLINE = _gfb._PERIOD_DEADLINE
    _period_deadline = _gfb._period_deadline
except Exception as _e:  # pragma: no cover - 缺模块必须硬失败，禁静默回退
    raise RuntimeError(
        "data_refresh_policy 需要 growth_factor_b 的法定披露截止日口径；"
        "禁止在本模块内另写一份（会静默漂移）。导入失败：%s" % _e
    )

PERIOD_ORDER = ("0331", "0630", "0930", "1231")
WEEK_CN = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")


# ─────────────────────────────────────────────────────────────────────────────
# 基础工具
# ─────────────────────────────────────────────────────────────────────────────
def as_date(value=None):
    """str / date / datetime / None → date。None 取本机今日。"""
    if value is None:
        return date.today()
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return datetime.strptime(str(value)[:10], "%Y-%m-%d").date()


def load_schedule(cfg=None):
    """config.json → schedule 段。缺段返回空 dict（由调用方按默认值处理）。"""
    if cfg is not None:
        return (cfg.get("schedule") or {}) if isinstance(cfg, dict) else {}
    try:
        with open(CONFIG, encoding="utf-8") as f:
            return json.load(f).get("schedule") or {}
    except (OSError, ValueError):
        return {}


def _decision(should, reason, **extra):
    d = {"should_refresh": bool(should), "reason": reason}
    d.update(extra)
    return d


# ─────────────────────────────────────────────────────────────────────────────
# 财报窗口 / 报告期（复用 growth_factor_b 口径）
# ─────────────────────────────────────────────────────────────────────────────
def disclosure_windows(year):
    """某一年的 4 个财报披露窗口 → [(start, end, period), ...]（date 类型）。

    年报窗口跨年：year 年的 1/1~4/30 是 (year-1) 年报的披露期；
    同一时段还与本年一季报窗口（4/1~4/30）重叠，故 active 可能返回 2 个。
    """
    wins = [
        (date(year, 1, 1), date(year, 4, 30), "%04d1231" % (year - 1)),  # 年报
        (date(year, 4, 1), date(year, 4, 30), "%04d0331" % year),         # 一季报
        (date(year, 7, 1), date(year, 8, 31), "%04d0630" % year),         # 中报
        (date(year, 10, 1), date(year, 10, 31), "%04d0930" % year),       # 三季报
    ]
    return wins


def active_disclosure_windows(as_of=None):
    """as_of 所处的披露窗口（可能 0~2 个）。"""
    d = as_date(as_of)
    out = []
    # 跨年窗口只需查当年与上一年即可覆盖（年报窗口 ≤ 4/30，不跨两个年界）
    for y in (d.year - 1, d.year, d.year + 1):
        for s, e, p in disclosure_windows(y):
            if s <= d <= e:
                out.append({"start": s.isoformat(), "end": e.isoformat(),
                            "period": p,
                            "deadline": _period_deadline(p)})
    # 去重（同一 period 可能被相邻年份窗口重复生成）
    seen, uniq = set(), []
    for w in out:
        if w["period"] not in seen:
            seen.add(w["period"])
            uniq.append(w)
    return sorted(uniq, key=lambda w: w["period"])


def effective_report_period(as_of=None):
    """严格 PIT：as_of 时已过法定披露截止日的**最新**报告期，如 '20260930'。

    2026-11-06 → 20260930（三季报 10/31 截止已过，年报要等 2027-04-30）
    2026-03-15 → 20250930（本年一季报/年报都还没到截止日）
    """
    d = as_date(as_of)
    cands = []
    for y in range(d.year - 2, d.year + 2):
        for md in PERIOD_ORDER:
            p = "%04d%s" % (y, md)
            dl = _period_deadline(p)
            if dl and dl <= d.isoformat():
                cands.append(p)
    return max(cands) if cands else None


def latest_closed_window(as_of=None):
    """as_of 之前最近一个已关闭的披露窗口 → dict 或 None（用于冻结期说明）。"""
    d = as_date(as_of)
    closed = []
    for y in range(d.year - 2, d.year + 2):
        for s, e, p in disclosure_windows(y):
            if e < d:
                closed.append((e, {"start": s.isoformat(), "end": e.isoformat(),
                                   "period": p, "deadline": _period_deadline(p)}))
    return max(closed, key=lambda t: t[0])[1] if closed else None


# ─────────────────────────────────────────────────────────────────────────────
# 三类数据的判定（单一入口）
# ─────────────────────────────────────────────────────────────────────────────
def pool_should_refresh(as_of=None, cfg=None):
    """POOL：每周五更新（config.schedule.pool_rerun_weekday，默认 4）。

    ⚠️ 日历级判定，不含「15:00 收盘后」与「周末补跑」——那是执行层
       schedule_guard.plan_mode() 的职责，勿在此重复实现。
    """
    d = as_date(as_of)
    sc = load_schedule(cfg)
    enabled = bool(sc.get("enabled", True))
    wd = int(sc.get("pool_rerun_weekday", 4))
    if not enabled:
        return _decision(True, "schedule.enabled=false（降采样关闭）→ 退回每日更新池",
                         weekday=WEEK_CN[d.weekday()], rerun_weekday=WEEK_CN[wd % 7])
    ok = d.weekday() == wd
    return _decision(
        ok,
        ("%s 是本周股票池更新日（pool_rerun_weekday=%d）" % (WEEK_CN[d.weekday()], wd))
        if ok else
        ("%s 非股票池更新日（仅%s更新；本次不重建池成分）"
         % (WEEK_CN[d.weekday()], WEEK_CN[wd % 7])),
        weekday=WEEK_CN[d.weekday()], rerun_weekday=WEEK_CN[wd % 7])


def financial_should_refresh(as_of=None, has_new_disclosure=None, force=False,
                             cfg=None):
    """FINANCIAL：财报披露窗口内按披露事件更新；窗口关闭即冻结。

    - 窗口外（披露完毕）：**False**，对应老大「如三季报公布完后不再更新」。
    - 窗口内且未提供 has_new_disclosure：True（保守扫描，由 fin_factor_cache
      按 (code, 报告期) 失效条件决定哪些个股真正重算）。
    - 窗口内显式 has_new_disclosure=False：本次无新披露 → False。
    - force=True：强制更新（财报更正 / 临时补数），与 SCORER_FORCE_FULL 同型开关。

    ⚠️ 个案的财报更正不属于调度层；它由 fin_factor_cache 的
       「EM 缓存 mtime 变新 / schema 版本变化」失效条件自行处理。
    """
    d = as_date(as_of)
    wins = active_disclosure_windows(d)
    if force:
        return _decision(True, "显式 force=True 强制刷新财务数据",
                         effective_period=effective_report_period(d),
                         active_windows=wins)
    if not wins:
        closed = latest_closed_window(d)
        return _decision(
            False,
            ("财报冻结期：%s 期已于 %s 披露完毕，之后不再更新；"
             "当前生效报告期 %s"
             % (closed["period"] if closed else "?",
                closed["end"] if closed else "?",
                effective_report_period(d))),
            effective_period=effective_report_period(d), active_windows=[])
    if has_new_disclosure is False:
        return _decision(False, "披露窗口内但今日无新增财报披露 → 不重算",
                         effective_period=effective_report_period(d),
                         active_windows=wins)
    return _decision(
        True,
        ("%s 处于财报披露窗口（%s），按披露事件扫描更新"
         % (d.isoformat(), ", ".join(w["period"] for w in wins))),
        effective_period=effective_report_period(d), active_windows=wins)


def market_should_refresh(as_of=None, is_trade_day=None):
    """MARKET：每个交易日更新。

    ⚠️ 本模块**不编造节假日表**。is_trade_day 显式注入（来自真实交易日历）时以其为准；
       未注入时只做周末粗判，工作日返回 True 并在 reason 中声明
       「法定节假日由 check_data_fresh 的指数最新日期门禁兜底」。
    """
    d = as_date(as_of)
    if is_trade_day is not None:
        return _decision(bool(is_trade_day),
                         "调用方注入 is_trade_day=%s（真实交易日历口径）" % bool(is_trade_day),
                         weekday=WEEK_CN[d.weekday()])
    if d.weekday() >= 5:
        return _decision(False, "%s 非交易日（周末）→ 行情不更新" % WEEK_CN[d.weekday()],
                         weekday=WEEK_CN[d.weekday()])
    return _decision(
        True,
        ("%s 工作日 → 行情每日更新；⚠️ 法定节假日不由本模块判定，"
         "以 check_data_fresh 的指数最新日期门禁为准" % WEEK_CN[d.weekday()]),
        weekday=WEEK_CN[d.weekday()])


def plan(as_of=None, cfg=None, has_new_disclosure=None, is_trade_day=None,
         force=False):
    """三类数据的完整计划 → dict（纯函数，不写盘）。

    mode：任一类需要完整评分（POOL 或 FINANCIAL）→ 'full'；
          仅 MARKET → 'quote_only'。与 schedule_guard.plan_mode 语义对齐，供接线时校验。
    """
    d = as_date(as_of)
    decisions = {
        POOL: pool_should_refresh(d, cfg),
        FINANCIAL: financial_should_refresh(d, has_new_disclosure, force, cfg),
        MARKET: market_should_refresh(d, is_trade_day),
    }
    need_full = decisions[POOL]["should_refresh"] or decisions[FINANCIAL]["should_refresh"]
    return {
        "date": d.isoformat(),
        "weekday": WEEK_CN[d.weekday()],
        "decisions": decisions,
        "effective_report_period": effective_report_period(d),
        "active_disclosure_windows": active_disclosure_windows(d),
        "latest_closed_window": latest_closed_window(d),
        "mode": "full" if need_full else "quote_only",
        "policy_source": "data_refresh_policy.py（老大 2026-11-06 裁决③）",
        "caveats": [
            "本模块只判定、不执行；生产链路接线属 P1/C 组，需裁决后实施",
            "法定节假日无本地日历表，由 check_data_fresh 指数日期门禁兜底",
            "报告期口径复用 growth_factor_b._PERIOD_DEADLINE，勿另立",
        ],
    }


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────
def format_plan(p):
    out = ["数据更新计划  %s（%s）  链路模式：%s"
           % (p["date"], p["weekday"], p["mode"]),
           "  当前生效报告期：%s" % p["effective_report_period"]]
    wins = p["active_disclosure_windows"]
    out.append("  披露窗口：%s" % (", ".join(w["period"] for w in wins) if wins else "无（冻结期）"))
    for k in DATA_CLASSES:
        d = p["decisions"][k]
        out.append("  %-10s %-3s  %s"
                   % (k, "刷新" if d["should_refresh"] else "冻结", d["reason"]))
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description="分层数据更新频率判据")
    ap.add_argument("--at", default="", help="指定日期 YYYY-MM-DD")
    ap.add_argument("--week", action="store_true", help="打印所在周七天的判定")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    a = ap.parse_args(argv)
    d = as_date(a.at or None)
    if a.week:
        monday = d - timedelta(days=d.weekday())
        plans = [plan(monday + timedelta(days=k)) for k in range(7)]
    else:
        plans = [plan(d)]
    if a.json:
        print(json.dumps(plans if a.week else plans[0], ensure_ascii=False, indent=2))
    else:
        for p in plans:
            for ln in format_plan(p):
                print(ln)
            if a.week:
                print("-" * 78)
    return 0


if __name__ == "__main__":
    sys.exit(main())
