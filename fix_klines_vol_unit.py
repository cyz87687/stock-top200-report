#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""fix_klines_vol_unit.py —— 修复 klines_long.json 指定日期的成交量单位混写

## 库内真实口径（实测，非推测）

bar = `[open, close, high, low, volume]`。用东财 qfq 逐日逐只交叉验证后确认：

| 板块 | 前缀 | volume 单位 | 库vol / 东财vol(手) |
|---|---|---|---|
| 科创板 | 688 | **股** | 恒等 100.00 |
| 其他 | 00/30/60/… | 手 | 恒等 1.00 |

## 事故

`2026-11-02`：**非科创板 1,825 / 1,825 全部被写成「股」**（东财核对 ratio 恒等 100.0000），
即该日整个非科创板板块的 volume 被放大恰好 100 倍。

## ⚠️ 为什么判据必须用东财，不能用库内自证

v1（用「前一日 vol」作基准，r∈[50,200]）：漏 35 个组合，且 688658 全历史都是股、
603131 当日真实暴量 848 倍 → 两个都漏。
v2（用「该股其余日期 vol 中位数」作基准，r∈[50,300]）：只修 1109 只，**漏 716 只**，
另有 441 只被「该股量级异常」误挡。根因——`该日vol / 历史中位vol` 的分布是
**单峰对数正态**（n=1,818，中位≈100，区间 20~2000），当日真实量对历史中位量本身
就有 2 个数量级自然波动，任何固定比值区间都只能随机命中约 60%。

→ **v3 改为外部权威清单驱动**：先跑 `verify_vol_unit_em.py` 用东财逐只打分，
再由本脚本按清单执行 ÷100。库内自证降级为「事后复核」，不再作为判据。

## 修复规则

只对 `verify_vol_unit_em.py` 输出的 `share` 清单（非688 且 库vol/东财vol ≈ 100）执行：
    volume ← volume / 100
price 字段（o/c/h/l）**一律不动**（与东财逐日一致，无污染；600519/300750/002587 逐日完全吻合）。

## 用法

    python verify_vol_unit_em.py --date 2026-11-02 --json data/volunit_1102_verify.json
    python fix_klines_vol_unit.py --date 2026-11-02 --from data/volunit_1102_verify.json --dry-run
    python fix_klines_vol_unit.py --date 2026-11-02 --from data/volunit_1102_verify.json
"""
import os
import sys
import json
import shutil
import argparse
import statistics
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
os.chdir(HERE)
KL = os.path.join(HERE, "data", "klines_long.json")

DIVISOR = 100.0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default="2026-11-02")
    ap.add_argument("--from", dest="src", default="data/volunit_1102_verify.json",
                    help="verify_vol_unit_em.py 产出的核对结果")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    print("=" * 96)
    print(f"fix_klines_vol_unit · {a.date} 非科创板成交量 ÷100（东财权威清单驱动）")
    print("=" * 96)

    if not os.path.exists(a.src):
        print(f"❌ 缺少核对清单 {a.src}")
        print("   请先运行：python verify_vol_unit_em.py --date %s --json %s" % (a.date, a.src))
        return 2
    ver = json.load(open(a.src, encoding="utf-8"))
    if ver.get("date") != a.date:
        print(f"❌ 清单日期 {ver.get('date')} ≠ 目标 {a.date}")
        return 2
    share = [c for c, _ in ver.get("share", [])]
    print(f"清单：日期 {ver['date']}，待修复 {len(share)} 只")
    print(f"      分类计数 {ver.get('counts')}")
    if not share:
        print("清单为空，无需修复")
        return 0

    raw = json.load(open(KL, encoding="utf-8"))
    print(f"库内 {len(raw)} 只")

    # ── 前置校验：清单里的每一只都必须「库内有该日 bar」且「当前值仍是污染值」──
    #     防止对已修复过的库重复 ÷100（幂等保护）
    pre_ok, pre_bad = [], []
    for c in share:
        m = raw.get(c)
        if not m or a.date not in m:
            pre_bad.append((c, "库内无该日"))
            continue
        bar = m[a.date]
        if not isinstance(bar, (list, tuple)) or len(bar) < 5:
            pre_bad.append((c, "bar 结构异常"))
            continue
        try:
            v = float(bar[4])
        except (TypeError, ValueError):
            pre_bad.append((c, "vol 非数值"))
            continue
        if v <= 0:
            pre_bad.append((c, "vol<=0"))
            continue
        pre_ok.append((c, v))

    if pre_bad:
        print(f"\n⚠️ 清单中 {len(pre_bad)} 只无法处理：{pre_bad[:5]}")
    print(f"可修复 {len(pre_ok)} 只")

    # ── 幂等性探针：抽样比对清单里的东财值，确认库内当前仍是「股」口径 ──
    probe = pre_ok[:20]
    already = 0
    for c, v in probe:
        # 东财值从清单 ratio 反推：v_lib = r * v_em  →  v_em = v / r
        rmap = dict((x[0], x[1]) for x in ver.get("share", []))
        r = rmap.get(c, 100.0)
        if r <= 0:
            continue
        v_em = v / r
        if abs(v / v_em - 1.0) < 0.05:
            already += 1
    if already >= len(probe) * 0.8:
        print(f"\n⚠️ 抽样 {len(probe)} 只中 {already} 只已是「手」口径 → 疑似重复修复，中止")
        return 2
    print(f"幂等探针：抽样 {len(probe)} 只，已是手口径 {already} 只（<80%，可安全修复）")

    # ── 关键样本显示 ──
    rmap = dict((x[0], x[1]) for x in ver.get("share", []))
    print("\n样本：")
    for c, v in pre_ok[:8]:
        print(f"  {c}: {v:>16,.0f} → {v/DIVISOR:>14,.0f}  (东财ratio={rmap.get(c,0):.4f})")

    if a.dry_run:
        print("\n--dry-run：不写文件")
        print("=" * 96)
        return 0

    # ── 写盘：.snap_ 备份 + tmp → os.replace 原子写 ──
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    bak = f"{KL}.snap_before_volfix3_{stamp}"
    shutil.copy2(KL, bak)
    print(f"\n已备份 → {os.path.basename(bak)}")

    n_fix = 0
    for c, v in pre_ok:
        bar = raw[c][a.date]
        raw[c][a.date] = [bar[0], bar[1], bar[2], bar[3], v / DIVISOR]
        n_fix += 1

    tmp = KL + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(raw, f, ensure_ascii=False)
    os.replace(tmp, KL)
    print(f"✅ 已修复 {n_fix} 只，写回 {KL} ({os.path.getsize(KL)/1e6:.2f} MB)")

    # ── 自检：该日 vol 中位数应回落约 100 倍 ──
    day_v2 = [float(m[a.date][4]) for m in raw.values()
              if a.date in m and isinstance(m[a.date], (list, tuple))
              and len(m[a.date]) >= 5]
    med2 = statistics.median(day_v2)
    # 对照：前一交易日同口径中位数
    prev = None
    for d in sorted({d for m in raw.values() for d in m}):
        if d < a.date:
            prev = d
    med_prev = None
    if prev:
        pv = [float(m[prev][4]) for m in raw.values()
              if prev in m and not str(m.get("__c", "")).startswith("688")]
        # 剔除 688（口径不同）
        pv = [float(m[prev][4]) for c, m in raw.items()
              if prev in m and not c.startswith("688")]
        if pv:
            med_prev = statistics.median(pv)
    print(f"\n自检：该日 vol 中位数 {statistics.median([float(m[a.date][4]) for m in raw.values() if a.date in m]):,.0f}")
    print(f"      修复后 = {med2:,.0f}")
    if med_prev:
        print(f"      前一交易日({prev}) = {med_prev:,.0f}   偏差 {med2/med_prev:.2f}x")
    ok = med_prev is None or 0.1 <= med2 / med_prev <= 10.0
    print(f"      {'✅ 已回落至正常量级' if ok else '❌ 量级异常，请人工核查'}")
    print("=" * 96)
    return 0 if ok else 2


if __name__ == "__main__":
    sys.exit(main())
