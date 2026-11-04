#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""refresh_kline_meta.py —— 每日刷新 klines_long_meta.json（P1-1 配套）

## 为什么需要这个独立脚本

`data/klines_long_meta.json` 原本**只有 `fetch_klines_long.py` 会写**，
而该脚本是 35 分钟的全量重抓、**从未接入日链**。
日链用的是 `append_klines_latest.py`（增量追加），它**根本不碰 meta**。

后果（实测）：库文件更新到 `2026-11-02`，meta 却停在 `2026-10-27`，差 6 个自然日。
所有读 meta 的下游都拿到了过期信息 —— 又一个「一次性脚本不在日链=必然陈旧」。

本脚本只做一件事：读当前库 → 重算有效截面与统计 → 写回 meta。
不抓网络、不改价格、约 2 秒。

## 为什么单独一个脚本而不是塞进 kline_calendar

`kline_calendar.py` 是**被 import 的库**（58 个消费方），不应带副作用；
本脚本是**日链里的执行单元**。职责分离，也便于单独在 status 里打点。
"""
import os
import sys
import json

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
os.chdir(HERE)

import kline_calendar as kc


def main():
    print("=" * 88)
    print("refresh_kline_meta · 刷新价格库元信息")
    print("=" * 88)

    if not os.path.exists(kc.KLONG):
        print(f"⚠️ 库文件不存在：{kc.KLONG}")
        return 1

    old = {}
    if os.path.exists(kc.KMETA):
        try:
            with open(kc.KMETA, encoding="utf-8") as f:
                old = json.load(f)
        except Exception:
            old = {}

    old_range = old.get("date_range")
    old_n_valid = len(old.get("valid_cross_sections") or [])

    m = kc.write_meta()
    info = m["cross_section_audit"]
    n_valid = len(m["valid_cross_sections"])

    print(f"库：{info['n_codes']} 只 × {info['n_dates_total']} 日  "
          f"（{info['date_first']} ~ {info['date_last']}）")
    print(f"有效截面：{n_valid} 日  "
          f"（{info['valid_date_first']} ~ {info['valid_date_last']}）"
          f"  剔除 {info['n_dates_dropped']} 日")
    print(f"门槛：{m['validity_rule']}"
          f"  真空区={info['threshold_in_gap']}  gap={info['gap']}")

    # 披露变化（让日志自己说话，不靠人记）
    if old_range and old_range != m["date_range"]:
        print(f"📅 date_range 更新：{old_range} → {m['date_range']}")
    if old_n_valid and old_n_valid != n_valid:
        print(f"🔢 有效截面数变化：{old_n_valid} → {n_valid}")
    if not old_range:
        print("🆕 首次写入 meta（此前无 date_range）")

    print(f"✅ 已写回 {kc.KMETA}（{os.path.getsize(kc.KMETA)/1024:.1f} KB）")
    print("=" * 88)
    return 0


if __name__ == "__main__":
    sys.exit(main())
