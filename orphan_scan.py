#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""orphan_scan.py —— 根目录 json 孤儿检测（P1-3）

## 为什么不能靠「grep 文件名」

首版实测：只做字符串匹配，202 个 json 里报出 **95 个孤儿（11.22MB）**。
但逐个核查后发现**大量误判**，因为真实读取有三种机制：

| 机制 | 例子 | 静态 grep 能否发现 |
|---|---|---|
| ① 字面量路径 | `open("factor_ic.json")` | ✅ 能 |
| ② f-string 拼接 | `f"focus_pool_{date_str}.json"` | ❌ **不能** → 误判为孤儿 |
| ③ glob 通配 | `glob("pipeline_status_*.json")` | ❌ **不能** → 误判为孤儿 |

实测误判样本：
- `focus_pool_2026-11-02.json` —— 被 `gen_report_top200.py:5668` 用 f-string 读取
- `pipeline_status_*.json` —— 被页面渲染段用 `glob` 读取
- `top200_scored_2026-10-29.json` —— 9.93MB，虽无脚本写死名字，
  但 `prune_keep` 的保留窗口按后缀匹配，且它是历史存档

→ 故本脚本用 **三档判定**，而不是二值：

    referenced  : 字面量命中，或有动态机制能生成该名 → **保留**
    guarded     : 落在某个 glob / 前缀规则的保护域内 → **保留（标注）**
    orphan      : 三档都不命中 → **可归档**

## 「孤儿」不等于「可删」

本脚本只**移动**到 `archive/orphan_json/`，不删除。理由：
归档类 json（`audit_roundXX.json`）是历史研究的**证据**，
删掉等于让 `reports/roundXXX.md` 里的数字失去可复核来源。

## 用法

    python orphan_scan.py                 # 只报告，不动文件
    python orphan_scan.py --apply         # 真正移动到 archive/orphan_json/
    python orphan_scan.py --json out.json # 落盘审计结果
"""
import os
import re
import sys
import glob
import json
import shutil
import argparse
import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
os.chdir(HERE)
ARCHIVE = os.path.join(HERE, "archive", "orphan_json")

# ── 保护域：这些前缀/模式名下的 json 一律不判孤儿 ──────────────────────
GUARDED_PATTERNS = [
    # 日链产物：按日期生成，消费方用 f-string 或 glob
    (r"^top200_scored_\d{4}-\d{2}-\d{2}\.json$", "日评分产物（prune_keep 保留窗口管理）"),
    (r"^top200_report_\d{4}-\d{2}-\d{2}\.html$", "日报告产物"),
    (r"^focus_pool_\d{4}-\d{2}-\d{2}\.json$", "AI 焦点池（f-string 拼接读取）"),
    (r"^focus_pool_\d{4}-\d{2}-\d{2}\.search\.json$", "焦点池检索中间件"),
    (r"^pipeline_status_\d{4}-\d{2}-\d{2}\.json$", "日链状态（glob 读取）"),
    (r"^qfq_anchor.*\.json$", "锚点自检产物"),
    (r"^panel_audit\.json$", "面板审计产物"),
    # 状态/游标：可能暂未被读，但属运行期状态，不该被归档
    (r"^pool_rebuild_state\.json$", "重建游标"),
    (r"^factor_weights\.json$", "权重配置本体"),
    (r"^factor_weights_optimized_.*\.json$", "权重重估留痕"),
    (r"^config\.json$", "配置"),
    (r"^weight_gate.*\.json$", "权重门禁产物"),
]

# ── 源码文本（引用源）────────────────────────────────────────────────
def _source_blob():
    files = []
    for pat in ("*.py", "*.sh", "*.md", "*.yml", "*.yaml"):
        files += glob.glob(pat)
    files += glob.glob(".github/workflows/*")
    blob = {}
    for f in files:
        if not os.path.isfile(f):
            continue
        if ".snap" in f or f.endswith(".bak"):
            continue
        try:
            if os.path.getsize(f) > 6_000_000:      # 超大 md 跳过
                continue
            blob[f] = open(f, encoding="utf-8", errors="ignore").read()
        except Exception:
            continue
    return blob


def classify(jsons, blob):
    """返回 {name: (verdict, reason)}"""
    # 收集 f-string / glob 的「可生成模式」
    # ⚠️ 首版把 f"xxx_{var}.json" 的约束放松成 ^前缀.*后缀$，结果几乎匹配一切
    #    → 202/202 全判「已引用」，假阴性 100%。必须按 【前缀 + 占位约束 + 后缀】 严判。
    dyn_rules = []      # (compiled_regex, desc)
    for src in blob.values():
        # f"focus_pool_{date_str}.json" → 前缀=focus_pool_ 后缀=.json 占位=变量
        # ⚠️ 变量约束必须排除格式化说明符：实测 `f'{g(r5,\'x\',\'{:.1f}\')}'`
        #    被误当成文件模板，{ 里塞了引号/冒号/括号 → 正则变成匹配一切
        #    → 202/202 全判「动态生成」。故要求变量名只含 [A-Za-z0-9_.]。
        for m in re.finditer(
                r'f["\']([A-Za-z0-9_\.\-/]*?)\{([A-Za-z_][A-Za-z0-9_\.\[\]\'"]{0,30})\}'
                r'([A-Za-z0-9_\.\-/{}\[\]]*)["\']', src):
            pre, var, suf = m.group(1), m.group(2), m.group(3)
            # 变量里不允许出现格式化说明符语法
            if re.search(r'[():,]\s*$', var) or ':' in var or '(' in var:
                continue
            pre = pre.split("/")[-1]
            if not pre:
                continue
            # 占位变量若是「日期」类，约束为 10 位日期；否则只允许非空紧凑串
            if re.search(r'date|day|_d\b|dt|today', var, re.I):
                rx = "^" + re.escape(pre) + r"\d{4}-\d{2}-\d{2}" + re.escape(suf) + "$"
            else:
                rx = "^" + re.escape(pre) + r"[A-Za-z0-9_\.\-]+" + re.escape(suf) + "$"
            dyn_rules.append((re.compile(rx), f"f-string {pre}{{{var}}}{suf}"))
        # glob("prefix_*.json") —— 星号只允许替换一段
        # ⚠️ 必须排除 `glob("*.json")` 这类笼统模式：实测有脚本遍历目录统计文件数，
        #    它的 glob("*.json") 会把 202 个文件全判成「被引用」（又一次假阴性 100%）。
        #    判据：星号前至少要有一个非空字面前缀（如 "pipeline_status_"）。
        for m in re.finditer(
                r'glob[^(]*\(\s*[^)]*?["\']([A-Za-z0-9_\.\-/]*\*[A-Za-z0-9_\.\-\*/]*)["\']', src):
            g = m.group(1).split("/")[-1]
            parts = g.split("*")
            if len(parts) == 2:
                head = parts[0]
                # 前缀必须足够具体：≥6 字符且含下划线，否则视为笼统遍历
                if len(head) < 6 or "_" not in head:
                    continue
                rx = "^" + re.escape(head) + r"[A-Za-z0-9_\.\-]*" + re.escape(parts[1]) + "$"
                dyn_rules.append((re.compile(rx), f"glob {g}"))

    out = {}
    for j in jsons:
        base = os.path.basename(j)
        # ① 字面量命中（排除自身文件内的自引用）
        lit = sum(s.count(base) for f, s in blob.items() if f != j)
        if lit > 0:
            out[base] = ("referenced", f"字面量引用 {lit} 处")
            continue
        # ② 动态模式能生成该名（严格约束）
        hit_dyn = None
        for rx, desc in dyn_rules:
            if rx.match(base):
                hit_dyn = desc
                break
        if hit_dyn:
            out[base] = ("referenced", f"动态生成（{hit_dyn}）")
            continue
        # ③ 保护域
        hit_g = None
        for rx, why in GUARDED_PATTERNS:
            if re.match(rx, base):
                hit_g = why
                break
        if hit_g:
            out[base] = ("guarded", hit_g)
            continue
        out[base] = ("orphan", "三档均未命中")
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="移动到 archive/orphan_json/")
    ap.add_argument("--json", default="", help="落盘审计结果")
    a = ap.parse_args()

    print("=" * 92)
    print("orphan_scan · 根目录 json 孤儿检测")
    print("=" * 92)

    jsons = sorted(glob.glob("*.json"))
    blob = _source_blob()
    print(f"根目录 json {len(jsons)} 个；引用源文件 {len(blob)} 个")

    res = classify(jsons, blob)
    by = {"referenced": [], "guarded": [], "orphan": []}
    for name, (v, why) in res.items():
        by[v].append((name, why, os.path.getsize(name)))

    print()
    for v, label in (("referenced", "已被引用"), ("guarded", "保护域内"), ("orphan", "孤儿")):
        items = by[v]
        sz = sum(s for _, _, s in items) / 1048576
        print(f"{label:<10} {len(items):>4} 个   {sz:>7.2f} MB")

    if by["orphan"]:
        print(f"\n孤儿清单（{len(by['orphan'])} 个）：")
        for name, why, s in sorted(by["orphan"], key=lambda x: -x[2]):
            print(f"  {s/1048576:8.3f} MB  {name}")

    if a.json:
        json.dump({
            "scanned_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "n_total": len(jsons),
            "counts": {k: len(v) for k, v in by.items()},
            "referenced": [[n, w] for n, w, _ in by["referenced"]],
            "guarded": [[n, w] for n, w, _ in by["guarded"]],
            "orphan": [[n, w, s] for n, w, s in by["orphan"]],
            "note": "孤儿仅归档不删除：audit_roundXX.json 是 reports/*.md 数字的可复核来源",
        }, open(a.json, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
        print(f"\n已写 {a.json}")

    if a.apply and by["orphan"]:
        os.makedirs(ARCHIVE, exist_ok=True)
        moved = 0
        for name, why, s in by["orphan"]:
            dst = os.path.join(ARCHIVE, name)
            if os.path.exists(dst):
                dst = dst + "." + datetime.datetime.now().strftime("%H%M%S")
            shutil.move(name, dst)
            moved += 1
        tot = sum(s for _, _, s in by["orphan"]) / 1048576
        print(f"\n✅ 已移动 {moved} 个孤儿 json（{tot:.2f} MB）→ {ARCHIVE}")
        print("   ⚠️ 未删除。需要时可原样移回根目录。")
    elif by["orphan"]:
        print(f"\n（未加 --apply，只报告不动文件）")

    print("=" * 92)
    return 0


if __name__ == "__main__":
    sys.exit(main())
