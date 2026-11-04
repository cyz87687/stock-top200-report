#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""verify_env_lock.py —— 运行环境与 requirements.txt 锁定一致性校验（P1-4）

## 为什么需要

`requirements.txt` 里写的版本只是**声明**。声明与实际环境是否一致，
原本没有任何机制检查 —— 也就是说「钉版本」这件事在跑之前是**不可验证的**。
`pip freeze` 能列出环境，但没人会每天手动 diff 一遍。

本脚本做三件事：
1. 逐包比对 `requirements.txt` 声明版本 vs 实际安装版本
2. 报告 Python 版本（本项目按 3.13.x 开发；大版本变化需重验）
3. 检出「代码在用但 requirements 未声明」的包（补声明缺口）

## 退出码

    0 = 全部一致
    1 = 有版本不符 / 缺失 / 未声明依赖   （**不阻断日链**，只告警）

## 用法

    python verify_env_lock.py
    python verify_env_lock.py --json env_lock_audit.json
    python verify_env_lock.py --fix-hint      # 打印应改成什么
"""
import os
import re
import sys
import json
import glob
import argparse
import datetime
import importlib.metadata as md

HERE = os.path.dirname(os.path.abspath(__file__))
os.chdir(HERE)
REQ = os.path.join(HERE, "requirements.txt")

# 代码里 import 但可能在 requirements 漏声明的包（import 名 → pip 名）
IMPORT_ALIAS = {
    "bs4": "beautifulsoup4",
    "sklearn": "scikit-learn",
    "PIL": "pillow",
    "yaml": "pyyaml",
    "cv2": "opencv-python",
}

# 不要求声明的（标准库 / 项目内部模块 / 已知不需要）
SKIP_IMPORTS = {
    "json", "os", "sys", "re", "math", "time", "glob", "datetime", "argparse",
    "hashlib", "threading", "shutil", "subprocess", "statistics", "collections",
    "importlib", "concurrent", "urllib", "csv", "itertools", "functools",
    "random", "string", "copy", "warnings", "traceback", "typing", "io",
    "pathlib", "decimal", "pickle", "gzip", "zipfile", "tempfile", "textwrap",
    "unicodedata", "base64", "hmac", "secrets", "socket", "ssl", "queue",
    "heapq", "bisect", "array", "struct", "calendar", "locale", "platform",
    "uuid", "zlib", "html", "ast", "inspect", "signal", "atexit", "shlex",
    "difflib", "pprint", "sqlite3", "xml", "email", "http", "ftplib",
    # ⚠️ 以下曾漏收导致误报（实测第一版把它们当成「未声明第三方包」）：
    "asyncio", "builtins", "multiprocessing", "numbers", "symtable",
    # unittest 是标准库；test_round117_p0_p1.py 引用它但不应要求 pip 声明
    "unittest", "doctest", "pdb", "cgitb",
    "operator", "contextlib", "dataclasses", "enum", "typing_extensions",
    "abc", "weakref", "gc", "sysconfig", "site", "token", "tokenize",
    "dis", "codecs", "unicodedata", "wave", "colorsys", "graphlib",
    "concurrent.futures", "logging", "getpass", "shlex", "stat",
    "timeit", "cProfile", "pstats", "tracemalloc", "resource",
    # 项目内部模块（同目录 .py）
    "kline_units", "kline_calendar", "pipeline_status", "panel_audit",
    # round125：标准库，曾被误报（admin_server.py 用它起本地 HTTP 服务）。
    # 现由 _is_stdlib() 动态兜底，这里同步补上以兼容 3.9 回退路径。
    "socketserver",
    "factor_eval", "growth_factor_b", "earnings_surprise", "fund_split",
    "fin_factor_cache", "ep_size_ind", "portfolio_metrics", "orphan_scan",
    "verify_env_lock", "verify_qfq_anchor", "refresh_kline_meta",
    # P1-5 回测统一口径内核与其守卫（内部模块，非 pip 包）
    "backtest_core", "backtest_legacy_guard", "factor_validation_core",
}


def parse_requirements(path):
    """返回 (active, commented)
    active    = {pip_name: version_or_None}  生效依赖
    commented = {pip_name: version_or_None}  被注释掉的「已显式声明的可选依赖」
    """
    active, commented = {}, {}
    if not os.path.exists(path):
        return active, commented
    for line in open(path, encoding="utf-8"):
        raw = line.rstrip("\n")
        # 先处理「被注释掉的依赖」：形如 `# torch==2.15.0`
        m_c = re.match(r"^#\s*([A-Za-z0-9_\.\-]+)\s*==\s*([A-Za-z0-9_\.\-\+]+)\s*$", raw)
        if m_c:
            commented[m_c.group(1).lower()] = m_c.group(2)
            continue
        line = raw.split("#")[0].strip()
        if not line:
            continue
        m = re.match(r"^([A-Za-z0-9_\.\-]+)\s*(?:==\s*([A-Za-z0-9_\.\-\+]+))?", line)
        if m:
            active[m.group(1).lower()] = m.group(2)
    return active, commented


def _is_stdlib(mod):
    """是否为**标准库**模块（含内置）。

    ⚠️ round125 修：原实现只靠 SKIP_IMPORTS 手写白名单，漏一个就误报一次。
    实测被误报的是 `socketserver`（admin_server.py 用它起 HTTP 服务）——
    它是货真价实的标准库，却因为不在手工名单里被判成「代码在用但 requirements
    未声明」→ verify_env_lock 退出码 1 → test_env_lock_verifier_passes FAIL。

    手写白名单的结构性问题：标准库有 200+ 个模块，枚举必然跟不上。
    Python 3.10+ 提供官方权威答案 `sys.stdlib_module_names`，直接用它。
    （3.9 及以下没有该属性 → 回退到白名单，行为不变。）
    """
    if not mod:
        return False
    if mod in getattr(sys, "builtin_module_names", ()):
        return True
    names = getattr(sys, "stdlib_module_names", None)
    if names is not None:
        return mod.split(".")[0] in names
    return False


def scan_imports():
    """扫所有 .py 的顶层 import，返回 {pip_name: [文件列表]}"""
    found = {}
    for f in glob.glob("*.py"):
        if ".snap" in f:
            continue
        try:
            s = open(f, encoding="utf-8", errors="ignore").read()
        except Exception:
            continue
        for m in re.finditer(r"^\s*(?:import|from)\s+([A-Za-z_][A-Za-z0-9_]*)",
                             s, re.M):
            mod = m.group(1)
            if mod in SKIP_IMPORTS or _is_stdlib(mod):
                continue
            pip = IMPORT_ALIAS.get(mod, mod).lower()
            found.setdefault(pip, set()).add(f)
    return {k: sorted(v) for k, v in found.items()}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", default="")
    ap.add_argument("--fix-hint", action="store_true")
    a = ap.parse_args()

    print("=" * 90)
    print("verify_env_lock · 依赖锁定一致性")
    print("=" * 90)

    req, req_opt = parse_requirements(REQ)
    if not req and not req_opt:
        print(f"⚠️ 未读到 {REQ}")
        return 1

    py_ver = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    print(f"Python {py_ver}   锁定文件声明 {len(req)} 个生效包"
          + (f" + {len(req_opt)} 个可选包（已注释声明）" if req_opt else ""))
    print(f"{'包名':<22}{'声明':<14}{'实际':<14}状态")
    print("-" * 90)

    rows = []
    n_bad = 0
    for name in sorted(req):
        want = req[name]
        try:
            have = md.version(name)
        except md.PackageNotFoundError:
            have = None
        if have is None:
            st, ok = "❌ 未安装", False
        elif want is None:
            st, ok = "⚠️ 未钉版本", False
        elif have == want:
            st, ok = "✅", True
        else:
            st, ok = f"❌ 不符", False
        if not ok:
            n_bad += 1
        print(f"{name:<22}{(want or '—'):<14}{(have or '—'):<14}{st}")
        rows.append({"pkg": name, "declared": want, "installed": have, "ok": ok})

    # 补声明缺口
    imports = scan_imports()
    # ⚠️ 可选依赖（requirements 里被注释声明）不算缺口 —— 它们已被显式记录，
    #   只是故意不装进最小生产环境。见 requirements.txt 的「可选依赖」段。
    missing = {k: v for k, v in imports.items() if k not in req and k not in req_opt}
    optional_used = {k: v for k, v in imports.items() if k in req_opt}
    # 过滤掉明显是本地模块的（同目录有同名 .py）
    local_mods = {f[:-3].lower() for f in glob.glob("*.py")}
    missing = {k: v for k, v in missing.items() if k not in local_mods}

    print()
    if optional_used:
        print(f"ℹ️ 已声明的可选依赖（非日链，装了才可跑对应历史脚本）：")
        for k, files in sorted(optional_used.items()):
            try:
                v = md.version(k)
            except Exception:
                v = "未安装"
            print(f"   {k:<22} 实际 {v:<12} 引用 {len(files)} 处（如 {files[0]}）")
        print()
    if missing:
        print(f"⚠️ 代码在用但 requirements 未声明 {len(missing)} 个：")
        for k, files in sorted(missing.items()):
            try:
                v = md.version(k)
            except Exception:
                v = "未安装"
            print(f"   {k:<22} 实际 {v:<12} 引用 {len(files)} 处（如 {files[0]}）")
    else:
        print("✅ 无未声明依赖")

    py_ok = sys.version_info[:2] == (3, 13)
    if not py_ok:
        print(f"\n⚠️ Python 版本 {py_ver} 与开发环境 3.13.x 不同，"
              f"行为差异需重验（本项不判失败）")

    print()
    if n_bad or missing:
        print(f"❌ {n_bad} 个包版本不符/未钉 + {len(missing)} 个未声明")
        if a.fix_hint:
            print("\n建议写入 requirements.txt：")
            for r in rows:
                if not r["ok"] and r["installed"]:
                    print(f"  {r['pkg']}=={r['installed']}")
            for k in sorted(missing):
                try:
                    print(f"  {k}=={md.version(k)}")
                except Exception:
                    pass
    else:
        print("✅ 环境与锁定完全一致")

    if a.json:
        json.dump({
            "checked_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "python": py_ver,
            "n_declared": len(req),
            "n_mismatch": n_bad,
            "packages": rows,
            "undeclared": {k: {"installed": (md.version(k) if _has(k) else None),
                               "files": v} for k, v in missing.items()},
            "verdict": "ok" if (not n_bad and not missing) else "drift",
        }, open(a.json, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
        print(f"\n已写 {a.json}")

    print("=" * 90)
    return 0 if (not n_bad and not missing) else 1


def _has(name):
    try:
        md.version(name)
        return True
    except Exception:
        return False


if __name__ == "__main__":
    sys.exit(main())
