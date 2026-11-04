#!/usr/bin/env python3
"""schedule_guard.py —— 降采样调度（老大 2026-10-26 需求③）。

规则：**股票池完整评分仅每周五收盘后跑一次；其余交易日只跑行情数据。**

## 为什么这样降采样（不是偷懒，是口径问题）
1. **财务因子在报告期内是常量**：三季报 10/31 披露完毕→11 月起全池报告期都是 0930，
   到明年 4/30 年报才变。每日重算 8 维里的 quality/fund/ep/growth 毫无新信息。
2. **技术因子黏性 0.903**（round25 实测）：今日分数与 1~3 日前的秩相关 0.90，
   日频重算的边际信息量极小。
3. **持有周期是 T+10、IC 峰值在 h40**（round25：h1 .087 → h20 .248 → h40 .280），
   策略本身不是日内，日频换分不产生 alpha，只产生换手摩擦（H20 年化 8.5x 已实测）。
4. **实测成本收益**：完整链路日跑 ≈ 8~10 分钟（异步化后），其中 90% 是抓 K 线；
   而每日真正变化的只有现价/成交额/市值 → 行情层 30 秒即可。

## 用法
    import schedule_guard as SG
    mode = SG.plan_mode()              # 'full' | 'quote_only'
    if mode == 'quote_only': ...

    python schedule_guard.py            # 打印今日应跑什么
    python schedule_guard.py --force    # 强制 full

## 环境变量
    SCORER_FORCE_FULL=1   强制完整链路（财报季临时披露、或临时要新评分）
    SCORER_FORCE_QUOTE=1  强制只跑行情（测试用）
"""
import json
import os
import sys
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG = os.path.join(HERE, "config.json")

WEEK_CN = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")


def load_cfg():
    try:
        c = json.load(open(CONFIG, encoding="utf-8"))
        return c.get("schedule") or {}
    except Exception:
        return {}


def plan_mode(now=None, cfg=None):
    """→ (mode, reason)
    mode: 'full' 跑完整评分链路 / 'quote_only' 只更新行情
    """
    cfg = cfg if cfg is not None else load_cfg()
    if os.environ.get("SCORER_FORCE_QUOTE", "") in ("1", "true", "yes"):
        return "quote_only", "SCORER_FORCE_QUOTE=1 强制只跑行情"
    if os.environ.get("SCORER_FORCE_FULL", "") in ("1", "true", "yes"):
        return "full", "SCORER_FORCE_FULL=1 强制完整链路"
    if not cfg.get("enabled", True):
        return "full", "schedule.enabled=false（降采样关闭，退回每日完整链路）"

    now = now or datetime.now()
    wd = int(cfg.get("pool_rerun_weekday", 4))     # 0=周一 … 4=周五
    # 周五 15:00 之后（收盘）才认为「本周已收盘」可跑
    is_rerun_day = (now.weekday() == wd and now.hour >= 15)
    # 周末补跑：仅在「本周五还没跑成」时触发（如 Actions 排队超时）。
    #   ⚠️ 首版无条件周末都判 full → 周五跑过之后周六周日会各再跑一次，重复且浪费。
    #   判据：本周五（或之后）是否已存在 scored 文件。
    if not is_rerun_day and now.weekday() in (5, 6) and cfg.get("weekend_catchup", True):
        friday = now.date()
        from datetime import timedelta
        friday = friday - timedelta(days=now.weekday() - wd)
        _p, _dt = latest_scored()
        if _dt and _dt >= friday.strftime("%Y-%m-%d"):
            return "quote_only", ("%s %02d:%02d 周末补跑检查：本周五(%s)已有评分(%s)，无需重跑"
                                  % (WEEK_CN[now.weekday()], now.hour, now.minute,
                                     friday.strftime("%Y-%m-%d"), _dt))
        is_rerun_day = True

    if is_rerun_day:
        return "full", ("%s %02d:%02d 是本周评分日（pool_rerun_weekday=%d，收盘后）"
                        % (WEEK_CN[now.weekday()], now.hour, now.minute, wd))
    return "quote_only", ("%s %02d:%02d 非评分日（仅周五收盘后跑完整评分；本次只更新行情）"
                          % (WEEK_CN[now.weekday()], now.hour, now.minute))


def latest_scored(date_str=None):
    """找最近的 scored 文件 → (path, its_date) 或 (None, None)。
    用于 quote_only 模式下「复用上一期评分」。"""
    import glob
    files = sorted(glob.glob(os.path.join(HERE, "top200_scored_*.json")), reverse=True)
    if date_str:
        files = [f for f in files if date_str not in f]
    for f in files:
        try:
            d = json.load(open(f, encoding="utf-8"))
            dt = d.get("date")
            rs = d.get("results") or []
            if dt and rs:
                return f, dt
        except Exception:
            continue
    return None, None


def summary(now=None):
    mode, reason = plan_mode(now)
    out = ["调度判定：**%s**  —— %s"
           % ("完整评分链路" if mode == "full" else "仅更新行情", reason)]
    p, dt = latest_scored()
    if p:
        out.append("   最近一期评分：%s（%s）"
                   % (os.path.basename(p), dt))
    if mode == "quote_only":
        out.append("   本次将复用上述评分的因子分，只刷新现价/涨跌幅/成交额/市值/板块动量")
    return out


if __name__ == "__main__":
    n = None
    if "--at" in sys.argv:
        i = sys.argv.index("--at")
        try:
            n = datetime.strptime(sys.argv[i + 1], "%Y-%m-%d %H:%M")
        except Exception:
            pass
    for ln in summary(n):
        print(ln)
    # 自检：一周七天各判定一次
    if "--week" in sys.argv:
        print("\n一周七天的判定（以 15:30 为基准）：")
        base = n or datetime.now()
        for k in range(7):
            from datetime import timedelta
            d = base - timedelta(days=base.weekday()) + timedelta(days=k)
            d = d.replace(hour=15, minute=30)
            m, r = plan_mode(d)
            print("   %s %s → %-11s %s"
                  % (d.strftime("%Y-%m-%d"), WEEK_CN[d.weekday()], m, r[:40]))
