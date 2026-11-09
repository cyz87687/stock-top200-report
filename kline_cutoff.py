#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""kline_cutoff.py —— 行情抓取的「日期硬上限」单一实现（round136）。

## 为什么需要这个模块

腾讯 `fqkline` 接口在**盘中**就返回当日行，且价与量都是**未收盘的瞬时值**。
实测 2026-11-09 10:50：茅台当日 vol=12532 手，而 11-06 整日 42848 手（约 29%）。

本项目所有增量行情落库都遵循同一条铁律 —— **已有日期绝不覆盖**
（保历史可复现，防前复权因子漂移静默改写面板）。两条规则叠加的后果是：

> 盘中缺陷跑一次 → 那条半日 K 线被永久写死 → 收盘后也不会被修正 → 污染不可逆

受影响的不止一个脚本，因此判据必须**单一实现**，禁止各处各写一份
（项目已有教训：日期判据散落 → 各处口径不一致 → 陈旧数据静默上线）。

## 用法

    import kline_cutoff as kc
    cutoff, reason = kc.resolve_cutoff()          # 自动：交易时段内排除当日
    cutoff, reason = kc.resolve_cutoff("2026-11-06")   # 显式：只补到该日

    if kc.bar_allowed(d, cutoff):                 # 落库前逐 bar 判定
        ...

## 判据（刻意粗，不编节假日表）

- 显式 `--until` 优先，格式非法**直接抛错**（禁止静默降级成「不限制」）
- 未显式给：周一~周五 09:30~15:05 视为交易时段 → 上限 = 前一日
- 周末 / 盘后：不限制（保持原行为）
- 节假日不判：「今天是不是真开市」由 `check_data_fresh` 的指数日期门禁兜底；
  本模块只负责「不把未收盘的当日价写进库」这一件事
"""
import datetime

# 交易时段（含收盘后 5 分钟冗余 —— 15:00 收盘瞬间的接口快照仍可能是残缺的）
SESSION_START_HM = 930
SESSION_END_HM = 1505


def now_cst():
    """当前北京时间（不依赖本机时区设置）。"""
    utc = datetime.datetime.now(datetime.timezone.utc)
    return (utc + datetime.timedelta(hours=8)).replace(tzinfo=None)


def in_session(now=None):
    """是否处于「当日行情可能未定稿」的时段（工作日交易时段内）。"""
    now = now or now_cst()
    if now.weekday() >= 5:
        return False
    hm = now.hour * 100 + now.minute
    return SESSION_START_HM <= hm <= SESSION_END_HM


def resolve_cutoff(until=None, now=None):
    """解析日期硬上限，返回 (cutoff_iso | None, reason)。

    cutoff_iso 为 'YYYY-MM-DD'，语义是「允许落库的最大日期」；None = 不限制。
    """
    now = now or now_cst()
    if until:
        d = str(until).strip()[:10]
        datetime.date.fromisoformat(d)        # 格式不对直接抛错，不静默
        return d, "显式指定上限"
    if in_session(now):
        prev = now.date() - datetime.timedelta(days=1)
        return prev.isoformat(), (
            f"未指定上限且当前 {now:%H:%M} 处交易时段，自动排除当日 {now:%Y-%m-%d}")
    return None, "未限制（非交易时段）"


def bar_allowed(date_str, cutoff):
    """该日期的 bar 是否允许落库。cutoff 为 None 时全部允许。"""
    if not cutoff:
        return True
    return str(date_str)[:10] <= cutoff
