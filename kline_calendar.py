#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""kline_calendar.py —— klines_long.json **有效横截面**的唯一口径来源

## 为什么必须有这个模块

`data/klines_long.json` 共 341 个日期，但**并非每日都是有效横截面**（实测）：

| 截面股票数 | 日数 | 说明 |
|---|---|---|
| 1 ~ 149 | **36** | 2025-06-09 ~ 2025-07-28 建库初期，逐只补数据的爬坡段 |
| ≥1500 | **305** | 2025-07-29 起完整横截面 |

在这 36 天里做「横截面 IC / 分层回测 / 组合净值」是**无意义**的：
1 只股票的截面对任何排序指标都只有 1 个值，Spearman 无定义。

**但库里没有任何标记** —— 于是每个消费方各自写一遍过滤逻辑：

    # portfolio_metrics.py（旧）
    def build_calendar(px, min_ratio=0.5):
        return sorted(d for d, c in cnt.items() if c >= len(px) * min_ratio)

    # 其他 audit 脚本：有的写 c >= 1000，有的写 c >= n*0.8，有的干脆不过滤

口径不统一 → **同一份库在不同脚本里算出不同的样本区间** → 结论不可比。
这正是项目记忆里「同一口径两处实现必然漂移 → 单一来源」的又一实例。

## 门槛为什么选 1000 而不是别的数

实测**在本库的实际分布下，门槛取 1000~2000 之间任意值，结果完全相同（305/36）**：
因为分布是**双峰**的，36 个小截面全部 <150 只，305 个大截面全部 ≥1500 只，
中间**不存在任何日期**。所以门槛在 [150, 1500] 区间内是**不敏感**的。

选 `min_abs=1000` 的理由：它同时把「相对口径」的两种极端都覆盖住 ——
- `n_codes × 0.5` = 1069（库内 2138 只），比 1000 稍严 → 1000 更宽松，不会误删
- `n_codes × 0.8` = 1710，比 1000 严 → 但实测两者结果一致，说明当前库干净

⚠️ **不要把 1000 理解成「有 1000 只就够了」**。它只是本库当前分布下的安全切点；
   若未来库扩到 5000 只，`min_ratio` 会生效并自动收紧（见 `build_valid` 的 dual 逻辑）。

## 用法

    from kline_calendar import valid_dates, build_valid, is_valid

    # 方式一：直接拿列表（读 meta，缺失则现算）
    ds = valid_dates()                      # 305 个日期，升序

    # 方式二：用已有 px 现算（不读 meta，回测脚本内部循环时用）
    ds = build_valid(px)

    # 方式三：判断某日是否有效
    if is_valid("2025-07-10"): ...

    # 方式四：过滤 px 字典（返回只含有效日期的新字典）
    px_ok = filter_px(px)

## 附带职责：写回 meta

`data/klines_long_meta.json` 原本**只有 `fetch_klines_long.py` 会写**，
而该脚本是 35 分钟全量重抓、**从未接入日链** → meta 永远停在最后一次全量跑
（实测停在 `2026-10-27`，而库文件已到 `2026-11-02`，差 6 天）。

→ 本模块的 `write_meta()` 由 `refresh_kline_meta.py` 每日调用，
  保证 meta 与库文件同步。这是「一次性脚本不在日链=必然陈旧」的修复。
"""
import os
import json
import statistics
from collections import Counter

HERE = os.path.dirname(os.path.abspath(__file__))
KLONG = os.path.join(HERE, "data", "klines_long.json")
KMETA = os.path.join(HERE, "data", "klines_long_meta.json")

# ── 有效截面判据（唯一来源，勿在别处重复定义）────────────────────────────
MIN_ABS = 1000        # 绝对下限：截面股票数 ≥ 1000
MIN_RATIO = 0.5       # 相对下限：≥ 库内代码数 × 0.5（库扩容后自动收紧）
# 实际门槛 = max(MIN_ABS, n_codes * MIN_RATIO)


def _load_px(px=None):
    if px is not None:
        return px
    with open(KLONG, encoding="utf-8") as f:
        return json.load(f)


def cross_section_sizes(px=None):
    """返回 (dates_asc, sizes_aligned, n_codes)"""
    px = _load_px(px)
    cnt = Counter()
    for bars in px.values():
        for d in bars:
            cnt[d] += 1
    dates = sorted(cnt)
    return dates, [cnt[d] for d in dates], len(px)


def build_valid(px=None, min_abs=None, min_ratio=None):
    """现算有效截面列表（升序）。不读 meta，保证与当前库一致"""
    _abs = MIN_ABS if min_abs is None else int(min_abs)
    _rat = MIN_RATIO if min_ratio is None else float(min_ratio)
    dates, sizes, n = cross_section_sizes(px)
    thr = max(_abs, n * _rat)
    return [d for d, s in zip(dates, sizes) if s >= thr]


def threshold_info(px=None):
    """披露门槛是怎么定的、卡掉了什么 —— 供报告层直接展示"""
    dates, sizes, n = cross_section_sizes(px)
    thr = max(MIN_ABS, n * MIN_RATIO)
    ok = [d for d, s in zip(dates, sizes) if s >= thr]
    drop = [(d, s) for d, s in zip(dates, sizes) if s < thr]
    # 双峰性检验：小截面最大 vs 有效截面最小，中间有没有「灰色地带」
    small_max = max((s for _, s in drop), default=0)
    valid_min = min((s for d, s in zip(dates, sizes) if s >= thr), default=0)
    return {
        "n_dates_total": len(dates),
        "n_dates_valid": len(ok),
        "n_dates_dropped": len(drop),
        "min_abs": MIN_ABS,
        "min_ratio": MIN_RATIO,
        "n_codes": n,
        "threshold": thr,
        "date_first": dates[0] if dates else None,
        "date_last": dates[-1] if dates else None,
        "valid_date_first": ok[0] if ok else None,
        "valid_date_last": ok[-1] if ok else None,
        "median_cross_section": statistics.median(sizes) if sizes else 0,
        "dropped_max_size": small_max,
        "valid_min_size": valid_min,
        # 双峰：被剔除的最大截面 < 保留的最小截面 → 门槛落在真空区，不敏感
        "threshold_in_gap": bool(small_max and valid_min and small_max < valid_min),
        "gap": [small_max, valid_min],
        "dropped_sample": drop[:5],
    }


def valid_dates(px=None, prefer_meta=True):
    """读 meta 优先（快），缺失/过期则现算并回写"""
    if prefer_meta:
        try:
            with open(KMETA, encoding="utf-8") as f:
                m = json.load(f)
            vd = m.get("valid_cross_sections")
            px_ = _load_px(px)
            # 校验 meta 未过期：库的最新日期必须出现在 meta 里
            latest = max((d for bars in px_.values() for d in bars), default=None)
            if isinstance(vd, list) and vd and latest in vd:
                return vd
        except Exception:
            pass
    return build_valid(px)


def is_valid(date, px=None):
    return str(date)[:10] in set(valid_dates(px))


def filter_px(px=None):
    """返回只含有效截面日期的新 px（不修改入参）"""
    px = _load_px(px)
    keep = set(build_valid(px))
    return {c: {d: b for d, b in bars.items() if d in keep}
            for c, bars in px.items()}


def write_meta(extra=None):
    """把 valid_cross_sections + 审计信息写回 meta（保留原有其它字段）"""
    px = _load_px()
    info = threshold_info(px)
    vd = build_valid(px)
    meta = {}
    if os.path.exists(KMETA):
        try:
            with open(KMETA, encoding="utf-8") as f:
                meta = json.load(f)
        except Exception:
            meta = {}
    # 更新顶层统计（原 fetch_klines_long.py 写的字段）
    meta["n_codes"] = info["n_codes"]
    meta["n_dates"] = info["n_dates_total"]
    meta["date_range"] = [info["date_first"], info["date_last"]]
    # P1-1 新增字段
    meta["valid_cross_sections"] = vd
    meta["cross_section_audit"] = info
    meta["validity_rule"] = (
        f"截面股票数 >= max({MIN_ABS}, n_codes x {MIN_RATIO}) = {info['threshold']:.0f}"
    )
    meta["meta_written_at"] = __import__("datetime").datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    meta["meta_writer"] = "kline_calendar.write_meta"
    if extra:
        meta.update(extra)
    with open(KMETA, "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=1)
    return meta


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="有效横截面口径（唯一来源）")
    ap.add_argument("--write", action="store_true", help="写回 data/klines_long_meta.json")
    ap.add_argument("--show", action="store_true", help="打印审计信息")
    a = ap.parse_args()

    px = _load_px()
    info = threshold_info(px)
    print("=" * 88)
    print("kline_calendar · 有效横截面口径")
    print("=" * 88)
    print(f"库内：{info['n_codes']} 只 × {info['n_dates_total']} 个日期 "
          f"（{info['date_first']} ~ {info['date_last']}）")
    print(f"判据：截面股票数 ≥ max({info['min_abs']}, {info['n_codes']}×{info['min_ratio']}) "
          f"= {info['threshold']:.0f} 只")
    print(f"有效：{info['n_dates_valid']} 日（{info['valid_date_first']} ~ {info['valid_date_last']}）")
    print(f"剔除：{info['n_dates_dropped']} 日")
    if info["n_dates_dropped"]:
        print(f"       被剔除的最大截面 {info['dropped_max_size']} 只；"
              f"保留的最小截面 {info['valid_min_size']} 只")
        if info["threshold_in_gap"]:
            print(f"       ✅ 门槛落在真空区 [{info['dropped_max_size']}, {info['valid_min_size']}] "
                  f"→ 双峰分布，门槛在 150~1500 间任取结果相同（不敏感）")
        else:
            print(f"       ⚠️ 门槛不在真空区 → 需复核是否误删边界样本")
        for d, s in info["dropped_sample"]:
            print(f"       - {d}  {s} 只")
    else:
        print("       （无剔除）")

    if a.write:
        m = write_meta()
        print(f"\n✅ 已写回 {KMETA}（{len(m['valid_cross_sections'])} 个有效日期）")
        print(f"   {os.path.getsize(KMETA)/1024:.1f} KB")
    print("=" * 88)
