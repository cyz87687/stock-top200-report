#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""backtest_legacy_guard.py —— 旧回测脚本的「未走统一口径」守卫（P1-5）

## 为什么需要这层

round117 P1-5 的要求是：「新回测必须调 `backtest_core`；**旧脚本不改**，
但加一层 wrapper 打印『本脚本未走统一口径』警告。」

不改旧脚本是对的 —— 那 21 个 `backtest_*.py` 是历史结论的证据链，
改了就说不清「当时的结论是在什么口径下得出的」。但**不改**不等于**放任**：
如果没人知道自己正在看一个口径不明的数字，那等于默许误读。

## 两种用法

### 1. 审计现有脚本（本文件主用途）

    python backtest_legacy_guard.py

输出每个 `backtest_*.py` 的口径缺口清单。**不改任何脚本**。

### 2. 包住旧脚本运行（出结论时用）

    python backtest_legacy_guard.py --wrap backtest_pool_factor.py

先打印横幅警告，再原样执行该脚本（`subprocess`，退出码透传）。
```
================================================================
⚠️  本脚本未走统一回测口径（backtest_core.py）
----------------------------------------------------------------
文件：backtest_pool_factor.py
缺口：未使用有效截面日历 / 基准未声明 / 0 个 NW t
后果：本脚本输出的 t 值对 HOP=20 重叠窗口**可能虚高**。
      实测同类脚本普通 t 比 NW t 虚高 1.88~2.87 倍。
      引用其结论时必须同时标注这一点，不得单独当作显著证据。
================================================================
```
"""
import os
import re
import sys
import glob
import json
import argparse
import subprocess

HERE = os.path.dirname(os.path.abspath(__file__))

# 口径缺口判定（每条都对应一个实测过的坑，不是凭空要求）
CHECKS = [
    ("valid_calendar", r"kline_calendar|valid_cross",
     "未使用有效截面日历 → 建库初期 36 个爬坡日（截面 1~148 只）会被当成有效样本"),
    ("benchmark", r"POOL_EW|pool_ew|hs300|HS300",
     "基准未声明 → 项目纪律「超额以 POOL_EW 为基准」未被代码强制"),
    ("nw_t", r"nw_t|newey|Newey",
     "无 Newey-West t → 对 HOP=N 重叠窗口用普通 t，t 值虚高（实测 1.88~2.87 倍）"),
    ("core", r"backtest_core",
     "未调用 backtest_core → 收益口径/基准/区间/显著性各自实现，可能互相矛盾"),
    ("units", r"kline_units",
     "未用 kline_units → 若涉及成交额，volume 双口径（688=股/其余=手）可能差 100 倍"),
]


def audit(path):
    with open(path, encoding="utf-8", errors="ignore") as f:
        src = f.read()
    uses_core = bool(re.search(r"backtest_core", src))
    gaps = []
    for key, pat, why in CHECKS:
        # ⚠️ 「走了 core」= 已具备 core 提供的全部口径，不能再按文本找 nw_t。
        #    第一版没做这个豁免，导致「正确使用 core 的脚本」被判为缺 nw_t
        #    （正向验证直接抓出来的：合规脚本报红 → 指标无信号价值）。
        if key == "nw_t" and uses_core:
            continue
        if key in ("valid_calendar", "benchmark", "units") and uses_core:
            # core 在 __init__ 里定死有效日历 / 基准 / 双口径，脚本自身无需再引用
            continue
        if key == "core" and uses_core:
            continue
        if not re.search(pat, src):
            gaps.append({"key": key, "why": why})
    hop = re.search(r"^HOP\s*=\s*(\d+)", src, re.M)
    return {
        "file": os.path.basename(path),
        "lines": len(src.split("\n")),
        "hop": int(hop.group(1)) if hop else None,
        "uses_core": uses_core,
        "gaps": gaps,
        "n_gaps": len(gaps),
        "compliant": len(gaps) == 0,
    }


def banner(r):
    lines = [
        "=" * 72,
        "⚠️  本脚本未走统一回测口径（backtest_core.py）",
        "-" * 72,
        f"文件：{r['file']}" + (f"    HOP={r['hop']}" if r["hop"] else ""),
    ]
    if r["gaps"]:
        lines.append("缺口：")
        for g in r["gaps"]:
            lines.append(f"  · [{g['key']}] {g['why']}")
    if any(g["key"] == "nw_t" for g in r["gaps"]):
        lines += [
            "后果：本脚本输出的 t 值对重叠窗口**可能虚高**。",
            "      实测同类脚本普通 t 比 NW t 虚高 1.88~2.87 倍。",
            "      引用其结论时必须同时标注这一点，不得单独当作显著证据。",
        ]
    lines.append("=" * 72)
    return "\n".join(lines)


def wrap(target, argv):
    path = os.path.join(HERE, target)
    if not os.path.exists(path):
        sys.exit(f"找不到 {target}")
    r = audit(path)
    print(banner(r), file=sys.stderr)
    # 原样执行，退出码透传 —— 不改脚本、不拦执行（历史证据链要能重跑）
    p = subprocess.run([sys.executable, path] + argv, cwd=HERE)
    return p.returncode


def main():
    ap = argparse.ArgumentParser(
        description="旧回测脚本口径守卫（P1-5）。默认模式 = 只审计不改动。")
    ap.add_argument("--wrap", metavar="SCRIPT",
                    help="包住指定脚本运行（打印警告后原样执行）")
    ap.add_argument("--json", metavar="OUT", help="审计结果写入 JSON")
    ap.add_argument("--only-noncompliant", action="store_true",
                    help="只列不合格的")
    a = ap.parse_args()

    if a.wrap:
        # 余下参数透传给目标脚本
        i = sys.argv.index("--wrap")
        rest = [x for x in sys.argv[i + 2:] if x != "--"]
        return wrap(a.wrap, rest)

    files = sorted(glob.glob(os.path.join(HERE, "backtest_*.py")))
    files = [f for f in files
             if os.path.basename(f) not in ("backtest_legacy_guard.py",
                                            "backtest_core.py")]
    rows = [audit(f) for f in files]
    rows.sort(key=lambda r: (-r["n_gaps"], r["file"]))

    print("=" * 96)
    print(f"旧回测脚本口径审计  ——  共 {len(rows)} 个 backtest_*.py")
    print("=" * 96)
    print(f"{'脚本':38s}{'HOP':>5s}{'缺口':>5s}  缺什么")
    print("-" * 96)
    for r in rows:
        if a.only_noncompliant and r["compliant"]:
            continue
        keys = ",".join(g["key"] for g in r["gaps"]) or "✅ 全部合规"
        print(f"{r['file']:38s}{(r['hop'] or '?'):>5}{r['n_gaps']:>5}  {keys}")

    n_c = sum(1 for r in rows if r["compliant"])
    print("-" * 96)
    print(f"合规 {n_c} / {len(rows)}")
    print()
    print("⚠️ 这不是「这些脚本是错的」。它们的结论在**当时声明的口径**下有效。")
    print("   问题在于：口径没有写进代码，所以无法自动检查，也无法阻止被误读。")
    print("   新回测请用 backtest_core.Backtest，口径在 __init__ 里定死、不可改。")

    if a.json:
        with open(a.json, "w", encoding="utf-8") as f:
            json.dump({"n_total": len(rows), "n_compliant": n_c,
                       "rows": rows}, f, ensure_ascii=False, indent=1)
        print(f"\n写入 {a.json}")

    # 审计本身不阻断（旧脚本是历史证据，不该因口径问题让 CI 红）
    return 0


if __name__ == "__main__":
    sys.exit(main())
