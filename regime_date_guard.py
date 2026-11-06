#!/usr/bin/env python3
"""产物日期一致性守卫（round133 / 2026-11-06 裁决 2：日期不一致数据不要展示）。

背景（2026-11-05 实锤）：
  `fetch_bench` 失效 → `regime_model` 正确地拒绝生成新预算 → 盘上仍是
  `regime_forecast.json` 的 2026-11-04 版本；`pick_portfolio.load_w_total`
  只校验文件**内部**的 `gate.ok`（11-04 自检时确实为 True），于是
  11-05 组合照样消费 11-04 的 `final_hi=0.5837`，并标 `budget_status=OK`。
  下游读者完全看不出这是过期预算。

裁决（老大 2026-11-06）：
  「日期不一致数据不要展示」——既不沿用旧预算、也不回退成 0 / 0.2 / 1.0
  之后再给一个「可执行」的组合清单；凡是依据产物日期 ≠ 数据日期的数据，
  一律**不产出、不展示**，只在页面留一句「未更新」的披露（页面必须说实话）。

⭐ 本模块是**单一实现**：pick_portfolio / portfolio_risk / gen_report_top200
   都必须调用这里，禁止各自再写一遍 `date_str in xxx` 的弱判断。

判据：
  · 只取字符串中的前 10 位 `YYYY-MM-DD`（容忍 `2026-11-05 15:00:00`）
  · 产物日期缺失 = 无法证明一致 → 判 `NO_ARTIFACT_DATE`，同样不展示
  · `data_date` 缺失 = 离线/单测上下文 → 由调用方决定是否跳过（本函数判
    `UNVERIFIED_NO_DATA_DATE`，`ok=False`）；生产链必须传数据日期
"""
import re

_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")

STATUS_OK = "OK"
STATUS_STALE = "STALE_DATE"
STATUS_NO_ARTIFACT_DATE = "NO_ARTIFACT_DATE"
STATUS_UNVERIFIED = "UNVERIFIED_NO_DATA_DATE"


def normalize_date(value):
    """→ 'YYYY-MM-DD' 或 None。容忍带时间戳、带后缀的日期字符串。"""
    if value is None:
        return None
    m = _DATE_RE.search(str(value))
    return m.group(0) if m else None


def check_artifact_date(artifact_date, data_date):
    """严格比对产物日期与数据日期。

    → {'ok', 'status', 'artifact_date', 'data_date', 'reason'}
    `ok=False` 时调用方**必须**不消费、不展示该产物里的任何数值。
    """
    art = normalize_date(artifact_date)
    dat = normalize_date(data_date)
    base = {'artifact_date': art, 'data_date': dat}
    if dat is None:
        return dict(base, ok=False, status=STATUS_UNVERIFIED,
                    reason="缺少数据日期，无法验证产物时效")
    if art is None:
        return dict(base, ok=False, status=STATUS_NO_ARTIFACT_DATE,
                    reason=f"产物无有效日期，无法证明与数据日期 {dat} 一致")
    if art != dat:
        return dict(base, ok=False, status=STATUS_STALE,
                    reason=f"产物日期 {art} ≠ 数据日期 {dat}")
    return dict(base, ok=True, status=STATUS_OK, reason="日期一致")


def check_payload(payload, data_date, date_keys=("date", "data_date")):
    """对 dict 产物做校验，自动取第一个非空日期字段。"""
    if not isinstance(payload, dict):
        return check_artifact_date(None, data_date)
    art = next((payload.get(k) for k in date_keys if payload.get(k)), None)
    return check_artifact_date(art, data_date)


def hidden_notice(check, subject):
    """统一的「不展示」披露文案（纯文本，调用方自行 escape）。"""
    return (f"{subject}依据的产物日期 {check.get('artifact_date') or '缺失'} "
            f"≠ 数据日期 {check.get('data_date') or '缺失'}，"
            f"按「日期不一致数据不展示」裁决，本期相关数值不产出、不展示；"
            f"这属于数据管道告警，不是「今日无数据」。")


def budget_source_stale(source, data_date):
    """组合产物 `risk.w_total_source` 的时效检查（页面层独立复核用）。

    历史产物可能仍标 `budget_status=OK`（因为写它时还没装守卫），所以
    页面**不能只信 status**，必须自己比 `source.date` 与数据日期。
    """
    if not isinstance(source, dict):
        return None
    chk = check_artifact_date(source.get("date"), data_date)
    if chk["ok"] and str(source.get("budget_status") or "OK").startswith("STALE"):
        chk = dict(chk, ok=False, status=STATUS_STALE,
                   reason=f"产物自带 budget_status={source.get('budget_status')}")
    return chk
