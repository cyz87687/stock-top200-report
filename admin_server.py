#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""round125｜权重/规则配置后台 —— 维护页与展示页分离。

为什么需要它（老大 2026-11-04 指令）：
    「权重好像一直在变，但是没有定论。研究是否可以把现有的权重列出来，
      把一些现有的规则和配置项放到一个后台，后台前端改为可手动配置权重，
      配置完点击按钮现在的 index 页面生效，等于把维护页面和展示页面分开」

职责边界（**只做配置管理，不做评分计算**）：
    1. 列出当前全部权重 / 约束 / 量程 / 选池规则 / 周期规则
    2. 手动改权重，**实时**校验 v4.15 约束（单因子≤0.20 / 大类≤0.30 / Σ=1.0）
    3. 点「生效」→ 自动完成**八处同步** + `verify_weights_round36.py` + 重生成 index.html
    4. 每次变更落审计日志 `data/admin_audit.jsonl`（含快照路径，可回滚）

安全铁律（每条都踩过坑）：
    · **禁跨行正则**改源码 —— round125 我用 `\\{.*?\\}` 吞掉过 audit_round31.py 292 行。
      这里一律用**括号配平**定位字典块（apply_round108.py 已验证的写法）。
    · 写盘前先快照全部受影响文件到 `.snap_admin_<ts>/`，任一步失败**自动回滚**。
    · 改完 py 必须 `ast.parse` 过一遍语法，再跑 verify_weights_round36.py，
      FAIL 一律回滚，绝不带病出图。
    · `weights` 与 `prior_weights` 必须逐键同步 —— verify 会查，漏了就是静默回退。
    · 只监听 127.0.0.1，不对外。**禁 git commit/push**（本脚本不碰 git）。

用法：
    python3 admin_server.py                # 默认 127.0.0.1:8788
    python3 admin_server.py --port 9000
    python3 admin_server.py --once          # 只做一次自检不起服务
"""
import argparse
import ast
import copy
import datetime
import glob
import http.server
import json
import os
import re
import shutil
import socketserver
import subprocess
import sys
import threading
import traceback
import urllib.parse

HERE = os.path.dirname(os.path.abspath(__file__))
PY = sys.executable

FW = os.path.join(HERE, "factor_weights.json")
CFG = os.path.join(HERE, "config.json")
CYCLE = os.path.join(HERE, "sector", "cycle_industry.json")
AUDIT = os.path.join(HERE, "data", "admin_audit.jsonl")
VERIFY = os.path.join(HERE, "verify_weights_round36.py")

# 八处同步落点 —— 与 verify_weights_round36.py 的 TARGETS 逐项一致。
# ⚠️ gen_report_top200.py 有两个独立入口（_fb 与 _rf_W_ACT），漏一个就是静默回退。
WEIGHT_TARGETS = [
    ("factor_eval.py", "PRIOR_W10"),
    ("step2_stockscorer_v2.py", "_W_PRIOR_10D"),
    ("apply_sector_momentum.py", "_W_PRIOR_10D"),
    ("enhance_scores.py", "_W_PRIOR_10D"),
    ("gen_report_top200.py", "_fb"),
    ("gen_report_top200.py", "_rf_W_ACT"),
]

# 写盘前要快照的文件（含 json 两入口 + 6 个 py）
SNAPSHOT_FILES = ["factor_weights.json"] + sorted({f for f, _ in WEIGHT_TARGETS})


# ─────────────────────────────────────────────────────────────────────────────
# 通用工具
# ─────────────────────────────────────────────────────────────────────────────
def _now():
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _load_json(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _dump_json(path, obj):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)


# ─────────────────────────────────────────────────────────────────────────────
# 局部保真写盘（cycle_industry.json 专用）
# ─────────────────────────────────────────────────────────────────────────────
# ⚠️ 为什么不能整文件 json.dumps(indent=2) 重写 cycle_industry.json：
#   该文件是**手写风格**，字符串数组按视觉宽度贪心折行（多元素同行），
#   实测 l2_list 折在 17 列、theme_list 折在 69 列、sw_cyclic_list 折在 78 列
#   ——每个块的折行宽度由作者当时手写决定，**没有统一阈值，无法自动复刻**。
#   整文件重写会把 7827 字节变成 8397 字节（+570），git diff 从「改 2 个行业」
#   变成「整个文件重写」，等于把可 review 的改动埋掉。
#   所以这里只**就地替换数组块内的元素**，块外一个字节都不动。
#
# 复现算法（已实测四份清单全部字节级一致）：
#   以该块**原文自身的最大行宽**为上限做贪心折行（两元素之间放 ", "，
#   除末元素外行尾补 ","），缩进 = key 行的缩进 + 2 空格。
def _disp_width(s):
    """终端显示宽度：CJK 全角算 2 列。"""
    import unicodedata
    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in s)


def _pack_str_array(items, ind, width):
    """把字符串列表按 width 显示列贪心折行，返回行列表（不含 key 行/"]"行）。"""
    out, cur = [], None
    for k, it in enumerate(items):
        tok = '"' + str(it) + '"' + ("," if k < len(items) - 1 else "")
        if cur is None:
            cur = ind + "  " + tok
        elif _disp_width(cur + " " + tok) > width:
            out.append(cur)
            cur = ind + "  " + tok
        else:
            cur = cur + " " + tok
    if cur is not None:
        out.append(cur)
    return out


_ARR_BLOCK_RE = None


def _find_array_block(lines, key):
    """定位 `"key": [` ... `]` 块，返回 (i, j, indent)。i 是 key 行，j 是 "]" 行。

    ⚠️ 只认**独占一行的** `"key": [` ——本文件所有清单都是这个形态。
    若某 key 写成单行内联数组（`"key": ["a","b"]`），返回 None，
    调用方据此走「整文件 dumps」兜底，并把这个事实写进 changes 里披露。
    """
    import re
    pat = re.compile(r'^(\s*)"' + re.escape(key) + r'"\s*:\s*\[$')
    for i, l in enumerate(lines):
        m = pat.match(l)
        if m:
            ind = m.group(1)
            j = i + 1
            while j < len(lines) and lines[j].rstrip() not in (ind + "]", ind + "],"):
                j += 1
            if j >= len(lines):
                return None
            return i, j, ind
    return None


def patch_str_arrays(relpath, updates):
    """**就地**替换 json 里若干字符串数组块，块外不动。返回 (新文本, 已应用keys, 跳过keys)。

    updates: {key: [新列表]}，key 支持末级名（theme_list / l2_list …）。
    值与原文件完全相同的 key 会被跳过（保证**空操作 = 零字节改动**）。
    """
    path = os.path.join(HERE, relpath)
    with open(path, encoding="utf-8") as f:
        txt = f.read()
    lines = txt.split("\n")
    applied, skipped = [], []

    for key, newv in updates.items():
        newv = [str(x) for x in (newv or [])]
        loc = _find_array_block(lines, key)
        if not loc:
            skipped.append(key)
            continue
        i, j, ind = loc
        try:
            oldv = json.loads("[" + "\n".join(
                lines[i + 1:j]).strip().rstrip(",") + "]")
        except Exception:
            skipped.append(key)
            continue
        if oldv == newv:
            skipped.append(key)          # 无变化 → 不碰这一块
            continue
        data_lines = lines[i + 1:j]
        width = max((_disp_width(x) for x in data_lines), default=0) or 80
        new_body = _pack_str_array(newv, ind, width)
        # ⚠️ 末尾元素**绝不能**补逗号：块尾 `],` 里那个逗号属于 "]" 之后的
        #   兄弟键分隔符，不属于数组内最后一个元素。曾经按 tail.endswith("],")
        #   补逗号 → 生成 `"晚元素",\n  ],` → Illegal trailing comma，
        #   整个局部写盘失败并静默回退成整文件重写（diff 158 行）。
        tail = lines[j]
        lines = lines[:i + 1] + new_body + [tail] + lines[j + 1:]
        applied.append(key)

    return "\n".join(lines), applied, skipped


def _audit(entry):
    """追加审计日志（append-only，禁覆盖）。"""
    os.makedirs(os.path.dirname(AUDIT), exist_ok=True)
    entry.setdefault("ts", _now())
    with open(AUDIT, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def weights_fingerprint(weights):
    """权重指纹 —— 二次确认用。

    必须是「权重的函数」，别人没法拿旧指纹凑合过去：
      固定按 key 排序 + 三位小数 + 空格连接，再取 sha256 前 12 位。
    前端 admin.html 里有一份**完全相同**的实现（JS 版），
    两边算法必须一致，否则前端算出来的 confirm 后端不认。
    """
    import hashlib
    items = sorted((str(k), f"{float(v or 0):.3f}")
                   for k, v in (weights or {}).items())
    raw = "|".join(f"{k}={v}" for k, v in items)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]


# ─────────────────────────────────────────────────────────────────────────────
# 源码安全改写：括号配平（**禁跨行正则**）
# ─────────────────────────────────────────────────────────────────────────────
def set_dict_values(src, pat, updates, label, changes):
    """把源码里 `pat = {...}` 字典内的指定键改成新值，保持格式不动。

    用**括号配平**定位右花括号，不用 `re.S` 跨行匹配 ——
    round125 血案：`re.search(r'^DIM_MAX = \\{.*?\\}', src, re.S|re.M)` 的
    `.*?` 会一路吞到文件末尾最后一个 `}`，audit_round31.py 被吃掉 292 行。
    """
    m = re.search(pat + r"\s*=\s*\{", src)
    if not m:
        raise RuntimeError(f"{label}: 未找到 {pat} 定义")
    start = m.start()
    i = src.index("{", start)
    depth = 0
    end = None
    for j in range(i, len(src)):
        if src[j] == "{":
            depth += 1
        elif src[j] == "}":
            depth -= 1
            if depth == 0:
                end = j
                break
    if end is None:
        raise RuntimeError(f"{label}: {pat} 花括号未配平（文件可能已损坏）")
    block = src[i:end + 1]
    new_block = block
    for k, newv in updates.items():
        pk = re.compile(r'("' + re.escape(k) + r'"\s*:\s*)([0-9.]+)')
        found = pk.search(new_block)
        if not found:
            raise RuntimeError(f"{label}: {pat} 内未找到键 {k}")
        oldv = float(found.group(2))
        if abs(oldv - float(newv)) < 1e-12:
            continue
        new_block = pk.sub(lambda mm: f'{mm.group(1)}{float(newv):.6f}',
                           new_block, count=1)
        changes.append(f"{label}:{pat}[{k}] {oldv} → {float(newv)}")
    return src[:i] + new_block + src[end + 1:]


# ─────────────────────────────────────────────────────────────────────────────
# 约束校验（v4.15-constraint-cap，与 verify_weights_round36.py 同口径）
# ─────────────────────────────────────────────────────────────────────────────
def validate_weights(weights, cfg):
    """校验权重是否满足约束。返回 (ok, errors, info)。"""
    errs, info = [], []
    wc = cfg.get("weight_constraints") or {}
    DIM_CAP = float(wc.get("dim_cap", 0.20))
    GROUP_CAP = float(wc.get("group_cap", 0.30))
    DIM_FLOOR = float(wc.get("dim_floor", 0.0))
    GROUPS = wc.get("groups") or {}

    active = {k: float(v) for k, v in weights.items() if float(v or 0) > 0}
    total = sum(float(v or 0) for v in weights.values())
    info.append(f"Σw = {total:.6f}（目标 1.0）")
    if abs(total - 1.0) > 1e-6:
        errs.append(f"Σw = {total:.6f} ≠ 1.0（当前容差 1e-6，比 verify 的 0.02 严）")

    for k, v in active.items():
        if v > DIM_CAP + 1e-9:
            errs.append(f"单因子超限：{k}={v} > {DIM_CAP}")
        if v < DIM_FLOOR - 1e-9:
            errs.append(f"单因子低于下限：{k}={v} < {DIM_FLOOR}")

    group_sum = {}
    for g, members in GROUPS.items():
        s = sum(active.get(m, 0.0) for m in members)
        group_sum[g] = s
        flag = "✅" if s <= GROUP_CAP + 1e-9 else "❌"
        info.append(f"{flag} 大类 {g} = {s:.4f}（≤{GROUP_CAP}）")
        if s > GROUP_CAP + 1e-9:
            errs.append(f"大类超限：{g}={s:.4f} > {GROUP_CAP}")

    # 未归入任何大类的生效维 —— 会绕过组约束，必须暴露
    grouped = {m for ms in GROUPS.values() for m in ms}
    orphan = [k for k in active if k not in grouped]
    if orphan:
        errs.append(f"以下生效维不在任何大类内，绕过组约束：{orphan}")

    # 未声明的键（防打错字静默进不了分母）
    known = set(cfg.get("weights") or {})
    unknown = [k for k in weights if k not in known]
    if unknown:
        errs.append(f"未声明的权重键（不在 factor_weights.json.weights）：{unknown}")

    return (len(errs) == 0), errs, info, group_sum


# ─────────────────────────────────────────────────────────────────────────────
# 快照 / 回滚
# ─────────────────────────────────────────────────────────────────────────────
def make_snapshot():
    """把将被改写的文件全部复制到 .snap_admin_<ts>/，返回快照目录。"""
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    snap = os.path.join(HERE, f".snap_admin_{ts}")
    os.makedirs(snap, exist_ok=True)
    for fn in SNAPSHOT_FILES:
        src = os.path.join(HERE, fn)
        if os.path.exists(src):
            shutil.copy2(src, os.path.join(snap, os.path.basename(fn) + ".bak"))
    # 记录清单，回滚时按它还原
    _dump_json(os.path.join(snap, "_manifest.json"),
               {"ts": ts, "files": SNAPSHOT_FILES})
    return snap


def rollback(snap):
    """从快照还原。返回还原的文件列表。

    ⚠️ round125 修：原实现遍历 `SNAPSHOT_FILES`（权重那 7 个文件），
    导致「改选池/周期规则后回滚」什么都不还原 —— 快照里明明有 config.json，
    但回滚函数根本不看它。现改为**按快照自己的 _manifest.json 还原**，
    快照里有什么就还什么，不依赖写快照那一刻的全局常量。
    备份文件名规则：相对路径里的 `/` 换成 `_`（见 make_snapshot_extra）。
    """
    restored = []
    man_p = os.path.join(snap, "_manifest.json")
    files = None
    if os.path.exists(man_p):
        try:
            files = json.load(open(man_p, encoding="utf-8")).get("files")
        except Exception:
            files = None
    if not files:
        files = SNAPSHOT_FILES

    for rp in files:
        rp = str(rp).replace(os.sep, "/")
        for cand in (rp.replace("/", "_") + ".bak",
                     os.path.basename(rp) + ".bak"):
            bak = os.path.join(snap, cand)
            if os.path.exists(bak):
                dst = os.path.join(HERE, rp)
                os.makedirs(os.path.dirname(dst) or HERE, exist_ok=True)
                shutil.copy2(bak, dst)
                restored.append(rp)
                break
    return restored


# ─────────────────────────────────────────────────────────────────────────────
# 八处同步
# ─────────────────────────────────────────────────────────────────────────────
def sync_eight_places(new_weights, dry_run=False):
    """把 new_weights 写进 factor_weights.json（两入口）+ 6 处 py 硬编码。

    返回 (changes, error)。error 非 None 时**未写盘**或已由调用方回滚。
    """
    changes = []

    # 1) factor_weights.json：weights 与 prior_weights 必须逐键同步
    cfg = _load_json(FW)
    for k, v in new_weights.items():
        old_w = float((cfg.get("weights") or {}).get(k, 0) or 0)
        old_p = float((cfg.get("prior_weights") or {}).get(k, 0) or 0)
        if abs(old_w - float(v)) > 1e-12 or abs(old_p - float(v)) > 1e-12:
            changes.append(f"factor_weights.json: weights/prior_weights[{k}] "
                           f"{old_w} → {float(v)}")
        cfg.setdefault("weights", {})[k] = float(v)
        cfg.setdefault("prior_weights", {})[k] = float(v)

    new_total = sum(float(v or 0) for v in cfg["weights"].values())
    if abs(new_total - 1.0) > 1e-9:
        return changes, f"Σw={new_total} ≠ 1.0，拒绝写入"

    # ⚠️ 变更留痕**只写 data/admin_audit.jsonl**，不写进 factor_weights.json。
    #   round125 血案：我曾在 cfg 里追加 admin_change_log，结果每次「生效」都会
    #   让 shasum factor_weights.json 变化，哪怕权重一个字没动 ——
    #   而项目铁律正是「跑前 shasum 留指纹」，指纹被审计日志扰动等于
    #   指纹失效。审计归审计，配置归配置，两份文件各司其职。

    if not dry_run:
        _dump_json(FW, cfg)

    # 2) 六处 py 硬编码（括号配平，禁跨行正则）
    for fn, pat in WEIGHT_TARGETS:
        p = os.path.join(HERE, fn)
        if not os.path.exists(p):
            return changes, f"{fn} 不存在，无法同步 {pat}"
        src = open(p, encoding="utf-8").read()
        try:
            src2 = set_dict_values(src, pat, new_weights, fn, changes)
        except RuntimeError as e:
            return changes, str(e)
        if src2 != src:
            # 语法自检 —— 被改坏的 py 会让整条日链崩，必须挡在这里
            try:
                ast.parse(src2)
            except SyntaxError as e:
                return changes, f"{fn} 改写后语法错误：{e}"
            if not dry_run:
                with open(p, "w", encoding="utf-8") as f:
                    f.write(src2)

    return changes, None


def run_verify():
    """跑 verify_weights_round36.py，返回 (ok, output)。"""
    try:
        r = subprocess.run([PY, VERIFY], capture_output=True, text=True,
                           cwd=HERE, timeout=300)
        out = (r.stdout or "") + (r.stderr or "")
        return r.returncode == 0, out
    except Exception as e:
        return False, f"verify 执行异常：{e}"


# ─────────────────────────────────────────────────────────────────────────────
# 重生成 index.html
# ─────────────────────────────────────────────────────────────────────────────
def regenerate_index(scored_path=None):
    """重跑 gen_report_top200.py 并 cp 到 index.html。返回 (ok, msg, detail)。"""
    if not scored_path:
        cands = sorted(glob.glob(os.path.join(HERE, "top200_scored_*.json")))
        if not cands:
            return False, "无 top200_scored_*.json，无法重生成", ""
        scored_path = cands[-1]

    # 先备份当前 index.html
    idx = os.path.join(HERE, "index.html")
    bak = os.path.join(HERE, ".snap_index_before_admin.html")
    if os.path.exists(idx):
        shutil.copy2(idx, bak)

    r = subprocess.run([PY, "gen_report_top200.py", scored_path],
                       capture_output=True, text=True, cwd=HERE, timeout=900)
    detail = (r.stdout or "")[-4000:] + (r.stderr or "")[-2000:]
    if r.returncode != 0:
        # 报告生成失败 → 还原 index.html，别留半成品
        if os.path.exists(bak):
            shutil.copy2(bak, idx)
        return False, "gen_report_top200.py 失败，index.html 已还原", detail

    # 找到刚生成的 html 并 cp 成 index.html
    htmls = sorted(glob.glob(os.path.join(HERE, "top200_report_*.html")),
                   key=os.path.getmtime)
    if not htmls:
        return False, "gen_report 未产出 top200_report_*.html", detail
    shutil.copy2(htmls[-1], idx)
    return True, f"已重生成 index.html（源 {os.path.basename(htmls[-1])}）", detail


# ─────────────────────────────────────────────────────────────────────────────
# 配置读取（给前端）
# ─────────────────────────────────────────────────────────────────────────────
def _sw_names():
    """申万二级/一级全名单（来自 sector/sw_map.json）。

    前端「+ 添加行业」下拉用它，避免手打行业名 —— 打错一个字
    （比如写成「航运港口业」）就会永远命中不了，而页面看起来一切正常。
    """
    p = os.path.join(HERE, "sector", "sw_map.json")
    if not os.path.exists(p):
        return {"l2": [], "l1": []}
    try:
        sw = _load_json(p)
        return {"l2": sorted((sw.get("l2_members") or {}).keys()),
                "l1": sorted((sw.get("l1_members") or {}).keys())}
    except Exception:
        return {"l2": [], "l1": []}


def read_config_snapshot():
    cfg = _load_json(FW)
    pool = _load_json(CFG) if os.path.exists(CFG) else {}
    cyc = _load_json(CYCLE) if os.path.exists(CYCLE) else {}

    # 当前 index.html / 数据状态
    idx = os.path.join(HERE, "index.html")
    scored = sorted(glob.glob(os.path.join(HERE, "top200_scored_*.json")))

    def _fstat(p):
        if not os.path.exists(p):
            return None
        st = os.stat(p)
        return {"path": os.path.basename(p), "bytes": st.st_size,
                "mtime": datetime.datetime.fromtimestamp(st.st_mtime)
                .strftime("%Y-%m-%d %H:%M:%S")}

    return {
        "ts": _now(),
        "weights": cfg.get("weights") or {},
        "prior_weights": cfg.get("prior_weights") or {},
        "dim_max": cfg.get("dim_max") or {},
        "weight_constraints": cfg.get("weight_constraints") or {},
        "pool_editable": POOL_EDITABLE,
        "version": cfg.get("version"),
        "target_horizon": cfg.get("target_horizon"),
        "fitted_at": cfg.get("fitted_at"),
        "method_head": (cfg.get("method") or "")[:300],
        "pool": {
            "pool_size": pool.get("pool_size"),
            "pool_rules": pool.get("pool_rules") or {},
            "liquidity_filter": pool.get("liquidity_filter") or {},
            "industry_limits": pool.get("industry_limits") or {},
            "eligibility_filter": pool.get("eligibility_filter") or {},
            "route_pool_map": pool.get("route_pool_map") or {},
            "version": pool.get("version"),
        },
        "cycle": {
            "version": cyc.get("version"),
            "gate_semantics": cyc.get("gate_semantics") or {},
            # ⚠️ round125 修复：**真实生效字段是 l2_list / l1_list**。
            #   此前写成 cyc.get("l2") or cyc.get("items") or boss_declared.items
            #   → 页面显示的是老大口语清单 12 项（'稀土','化纤','航运','化肥'…），
            #   而 growth_factor_b 真正消费的是 l2_list 20 项申万二级标准名
            #   （'金属新材料','航运港口','焦炭Ⅱ','炼化及贸易'…）。两边差 17/9 项。
            #   把 boss_declared 单独作为「原文留档」返回，**不参与判定**。
            "l2": cyc.get("l2_list") or [],
            "l1": cyc.get("l1_list") or [],
            # ⚠️ 同时保留 **JSON 原名键**：前端 CYC_SLOTS 直接按文件名里的键编辑，
            #   若只给 l2/l1 别名，前端 getPath(cycle,'l2_list') 恒 undefined
            #   → 四份清单全渲染成空，且「删一项」变成「把空列表写回去」。
            #   （round125 CDP 实测踩过：nChip=0，估算恒 163→163）
            "l2_list": cyc.get("l2_list") or [],
            "l1_list": cyc.get("l1_list") or [],
            "step2_cycle_lists": cyc.get("step2_cycle_lists") or {},
            "boss_declared": (cyc.get("boss_declared_2026_11_02") or {}).get("items") or [],
            "boss_declared_note": ((cyc.get("boss_declared_2026_11_02") or {})
                                   .get("note") or ""),
            "mapping": cyc.get("mapping") or {},
            "mapping_notes": cyc.get("mapping_notes") or {},
            "excluded_deliberately": cyc.get("excluded_deliberately") or {},
            "l1_note": cyc.get("l1_note") or "",
            "diff_vs_step2_SW_CYCLICAL": cyc.get("diff_vs_step2_SW_CYCLICAL") or {},
            "rules": cyc.get("rules") or {},
            # 四份清单的消费方/效果说明（前端用）
            "cycle_list_meta": {
                k: {"label": v["label"], "consumer": v["consumer"],
                    "effect": v["effect"]}
                for k, v in CYCLE_LIST_FIELDS.items()
            },
        },
        "sw_names": _sw_names(),
        "files": {
            "index_html": _fstat(idx),
            "scored": _fstat(scored[-1]) if scored else None,
            "factor_weights": _fstat(FW),
        },
    }


def read_audit(limit=50):
    if not os.path.exists(AUDIT):
        return []
    out = []
    with open(AUDIT, encoding="utf-8") as f:
        for ln in f:
            ln = ln.strip()
            if not ln:
                continue
            try:
                out.append(json.loads(ln))
            except Exception:
                pass
    return out[-limit:][::-1]


# ─────────────────────────────────────────────────────────────────────────────
# 配置写盘：保留原文件的序列化格式（禁 json.dump 统一 indent 造成全文件 diff）
# ─────────────────────────────────────────────────────────────────────────────
# 三个 json 的原始缩进各不相同（实测）：
#   factor_weights.json  indent=2 · 无尾换行
#   config.json          indent=1 · 无尾换行
#   cycle_industry.json  indent=2 · 有尾换行
# 用统一 indent 重写会把整份文件变成一行行 diff，git diff 里看不出真正改了哪几个数
# → 违反「改配置必须一眼可审」的纪律。
_JSON_FMT = {
    "factor_weights.json": (2, False),
    "config.json": (1, False),
    "sector/cycle_industry.json": (2, True),
}


def _dump_json_keep_fmt(relpath, obj):
    """按该文件**原有**缩进/尾换行写回，保持 diff 最小。"""
    ind, nl = _JSON_FMT.get(relpath.replace(os.sep, "/"), (2, False))
    path = os.path.join(HERE, relpath)
    txt = json.dumps(obj, ensure_ascii=False, indent=ind)
    if nl:
        txt += "\n"
    with open(path, "w", encoding="utf-8") as f:
        f.write(txt)


# ─────────────────────────────────────────────────────────────────────────────
# ② 大类约束（weight_constraints）—— 改完**立即**经 verify 生效
# ─────────────────────────────────────────────────────────────────────────────
def validate_constraints(wc, weights):
    """校验一组 weight_constraints 是否自洽。返回 (ok, errors, info)。

    ⚠️ 与 validate_weights 的分工：
      validate_weights 用**已生效的约束**去校验一组权重（「这批权重合不合规」）；
      本函数校验**约束本身**（「这条约束写得对不对」）。
    后者不查，会出现「把 dim_cap 调到 0.5 → 所有单因子都合规 → 约束形同废除」
    这种「改了约束让权重自动过关」的绕过路径。
    """
    errs, info = [], []
    try:
        dim_cap = float(wc.get("dim_cap"))
        group_cap = float(wc.get("group_cap"))
        dim_floor = float(wc.get("dim_floor", 0.0))
    except Exception:
        return False, ["dim_cap / group_cap / dim_floor 必须是数字"], info
    groups = wc.get("groups") or {}

    # —— 数值区间 ——
    if not (0.0 < dim_cap <= 1.0):
        errs.append(f"dim_cap={dim_cap} 必须在 (0, 1] 内")
    if not (0.0 < group_cap <= 1.0):
        errs.append(f"group_cap={group_cap} 必须在 (0, 1] 内")
    if not (0.0 <= dim_floor <= 1.0):
        errs.append(f"dim_floor={dim_floor} 必须在 [0, 1] 内")
    if dim_floor > dim_cap + 1e-12:
        errs.append(f"dim_floor({dim_floor}) > dim_cap({dim_cap})，下限不能高于上限")
    if dim_cap > group_cap + 1e-12:
        errs.append(f"dim_cap({dim_cap}) > group_cap({group_cap})："
                    "单因子允许值超过大类允许值，等于大类约束失效")

    # —— 「放宽到形同虚设」也要拦 ——
    # 上面只拦「自相矛盾」。但 group_cap=0.9 在 6 大类下同样等于废除组约束：
    # 一个类吃掉 90% 权重，其余 5 类分 10%。这不是「配置得更宽」，
    # 是「以为还在约束其实没有」—— 二者在页面上长得一样，所以必须在这里说破。
    #
    # 判据刻意选**客观的「单类不得过半」**，而不是「1/n×常数」这类主观阈值：
    #   实测 1/n×2.4 会把 group_cap=0.40 也拒掉 —— 而 0.40 是合理的放宽区间
    #   （现行 0.30），误伤合法配置等于把「可配置」做成「只能调小」。
    #   过半则是硬道理：单类 >50% 时组约束不再是分散化工具。
    _n_groups = len(groups) if isinstance(groups, dict) else 0
    if group_cap > 0.5 + 1e-12:
        errs.append(
            f"group_cap={group_cap} > 0.5：单个大类可吃掉过半权重，"
            "组约束在语义上已失效（不再是分散化工具）。"
            "若确实需要单类过半，请直接改 factor_weights.json 并说明意图。")
    elif _n_groups >= 3 and group_cap > 0.4 + 1e-12:
        info.append(f"⚠️ group_cap={group_cap} 已接近单类过半上限 0.5，"
                    f"当前有 {_n_groups} 个大类，实际分散度会明显下降")
    if group_cap < 1.0 / max(_n_groups, 1) - 1e-12:
        errs.append(f"group_cap={group_cap} < 1/{_n_groups}={1.0/max(_n_groups,1):.3f}："
                    f"Σ=1.0 时所有大类合计上限 {group_cap*_n_groups:.2f} < 1，权重无解")

    # —— 成员表结构 ——
    if not isinstance(groups, dict) or not groups:
        errs.append("groups 必须是非空 dict（大类名 → 维度列表）")
        return False, errs, info
    seen = {}
    for g, ms in groups.items():
        if not isinstance(ms, list) or not ms:
            errs.append(f"大类 {g} 的成员必须是非空列表")
            continue
        for m in ms:
            if not str(m).startswith("score_"):
                errs.append(f"大类 {g} 的成员 {m} 不是 score_ 开头的维度名")
            if m in seen:
                errs.append(f"维度 {m} 同时属于「{seen[m]}」和「{g}」"
                            "（重复归类会让两个大类都吃到同一份权重）")
            seen[m] = g

    # —— 与当前权重的关系（这才是真正会打死链路的地方）——
    known = set(weights or {})
    unknown = sorted(set(seen) - known)
    if unknown:
        errs.append(f"这些成员不在 factor_weights.json.weights 里（打错字？）：{unknown}")

    active = {k: float(v) for k, v in (weights or {}).items() if float(v or 0) > 0}
    orphan = sorted(k for k in active if k not in seen)
    if orphan:
        # 不阻断写盘，但必须暴露：这是「掩盖无法归类的维」的标准做法
        errs.append(f"以下生效维将不在任何大类内（绕过组约束）：{orphan}")

    gsum = {}
    for g, ms in groups.items():
        s = sum(active.get(m, 0.0) for m in ms)
        gsum[g] = round(s, 6)
        flag = "✅" if s <= group_cap + 1e-9 else "❌"
        info.append(f"{flag} {g} = {s:.4f}（≤{group_cap}）")
        if s > group_cap + 1e-9:
            errs.append(f"在当前权重下大类 {g}={s:.4f} > group_cap={group_cap}")

    over = [(k, v) for k, v in active.items() if v > dim_cap + 1e-9]
    for k, v in over:
        errs.append(f"在当前权重下单因子 {k}={v} > dim_cap={dim_cap}")

    tp = sum(float(v or 0) for v in (weights or {}).values())
    if abs(tp - 1.0) > 1e-6:
        errs.append(f"注意：当前权重 Σ={tp:.6f} ≠ 1.0，组约束的百分比口径会失真")

    info.append(f"约束校验：dim_cap≤{dim_cap} / group_cap≤{group_cap} / "
                f"dim_floor≥{dim_floor} / {len(groups)} 大类 {len(seen)} 维")
    return (len(errs) == 0), errs, info


def apply_constraints(wc, reason=""):
    """写 weight_constraints 到 factor_weights.json，然后跑 verify + 重生成 index。

    为什么必须跟 verify：约束是**出图前的最后一道闸**，
    改窄了要立刻知道现有权重会不会因此不合规 —— 那正是「改约束」的风险所在。
    """
    with _APPLY_LOCK:
        cfg = _load_json(FW)
        weights = cfg.get("weights") or {}
        ok, errs, info = validate_constraints(wc, weights)
        if not ok:
            _audit({"action": "constraints_rejected", "reason": reason,
                    "errors": errs, "constraints": wc})
            return {"ok": False, "stage": "validate", "errors": errs, "info": info}

        old = copy.deepcopy(cfg.get("weight_constraints") or {})
        # ⚠️ 空操作必须**零字节改动**：约束一字未改时不该写盘、不该动 verify。
        #   旧实现无条件 deepcopy → 写盘 → verify → 重生成 index，
        #   「点一下没改任何东西的 apply」也会跑 15 分钟的出图链路。
        if old == wc:
            _audit({"action": "constraints_noop", "reason": reason,
                    "note": "无实际改动，未写盘、未重生成 index"})
            return {"ok": True, "noop": True, "diff": [],
                    "info": info + ["未检测到实际改动 → 未写盘"],
                    "regenerate": "（无改动，未重生成 index）"}

        snap = make_snapshot()
        cfg["weight_constraints"] = wc
        _dump_json_keep_fmt("factor_weights.json", cfg)

        vok, vout = run_verify()
        if not vok:
            cfg2 = _load_json(FW)
            cfg2["weight_constraints"] = old
            _dump_json_keep_fmt("factor_weights.json", cfg2)
            _audit({"action": "constraints_rollback", "stage": "verify",
                    "verify_tail": vout[-1500:], "snap": os.path.basename(snap),
                    "reason": reason})
            return {"ok": False, "stage": "verify",
                    "error": "改完约束后 verify 未过（现有权重违反新约束），已还原",
                    "verify_tail": vout[-1500:],
                    "snap": os.path.basename(snap)}

        rok, rmsg, _ = regenerate_index()
        if not rok:
            rmsg = "⚠️ " + rmsg

        _audit({"action": "constraints_ok", "reason": reason,
                "constraints": wc, "diff": _diff_obj(old, wc),
                "snap": os.path.basename(snap), "regenerate": rmsg,
                "verify_tail": vout[-800:]})
        return {"ok": True, "info": info, "snap": os.path.basename(snap),
                "regenerate": rmsg, "verify_tail": vout[-1500:],
                "diff": _diff_obj(old, wc)}


def _diff_obj(a, b):
    """浅层 dict diff 描述（只用于审计留痕，不做深层结构比对）。"""
    out = []
    for k in sorted(set(a) | set(b)):
        va, vb = a.get(k, "<缺>"), b.get(k, "<缺>")
        if va != vb:
            out.append(f"{k}: {json.dumps(va, ensure_ascii=False)[:200]} → "
                       f"{json.dumps(vb, ensure_ascii=False)[:200]}")
    return out


# ─────────────────────────────────────────────────────────────────────────────
# ③ 选池规则（config.json）—— 改完**次日日链**才生效，无法用手头数据预览
# ─────────────────────────────────────────────────────────────────────────────
# 后台开放编辑的白名单。**故意不是「整个 config.json 都能改」**：
# 里面还有 workflow/schedule/monitoring/fetch 等运维字段，
# 在线改它们的后果（比如改坏 schedule 让日链不跑）远超「配置管理」的职责边界。
POOL_EDITABLE = {
    "pool_size": {"type": "int", "min": 0, "max": 6000,
                  "semantics": "0 = 不限规模（初筛池全量评分）"},
    # ⚠️ 金额类字段用 int，不用 float：
    #   原文件里 min_amount=100000000 / min_mktcap=2500000000 都是**整数**字面量。
    #   声明成 float 会让「原值回传的空操作」把它们写成 100000000.0，
    #   语义等价但 config.json 字节变脏（diff 显示 3 行改动，实际什么都没改）。
    #   保留 int 后空操作 = 零字节改动。
    "pool_rules.min_amount": {"type": "int", "min": 0, "max": 1e10,
                              "semantics": "当日成交额下限（硬约束·筛选）"},
    "pool_rules.min_mktcap": {"type": "int", "min": 0, "max": 1e13,
                              "semantics": "总市值下限（硬约束·筛选，非流通市值）"},
    "pool_rules.price_floor": {"type": "float", "min": 0, "max": 100,
                               "semantics": "股价下限（避面值退市）"},
    "pool_rules.drop_mom_top_pct": {"type": "float", "min": 0, "max": 50,
                                    "semantics": "当日涨幅前 N% 标记为追高（chase_hi，"
                                                 "仅标记不剔除，由组合层二次拦截）"},
    "pool_rules.exclude_bj": {"type": "bool", "semantics": "是否排除北交所"},
    "liquidity_filter.enabled": {"type": "bool", "semantics": "流动性过滤总开关"},
    "liquidity_filter.min_avg_amount_20d": {"type": "int", "min": 0, "max": 1e10,
                                           "semantics": "20 日均成交额下限"},
    "liquidity_filter.min_avg_turnover_rate": {"type": "float", "min": 0, "max": 1,
                                               "semantics": "20 日均换手率下限（小数，0.01=1%）"},
    "industry_limits.max_single_stock": {"type": "float", "min": 0, "max": 1,
                                         "semantics": "单票敞口上限"},
    "industry_limits.max_single_stock_scope": {"type": "enum",
                                               "options": ["assets", "equity"],
                                               "semantics": "单票口径：assets=占总资产 / equity=占股票仓位"},
    "industry_limits.max_l2_sector": {"type": "float", "min": 0, "max": 1,
                                      "semantics": "申万二级行业上限"},
    "industry_limits.max_l1_parent": {"type": "float", "min": 0, "max": 1,
                                      "semantics": "申万一级父类上限"},
    "industry_limits.max_l1_deviation_pp": {"type": "float", "min": 0, "max": 100,
                                            "semantics": "一级行业偏离上限（pp，首只永远允许）"},
    "eligibility_filter.exclude_st": {"type": "bool", "semantics": "排除 ST"},
    "eligibility_filter.exclude_delisting": {"type": "bool", "semantics": "排除退市整理"},
    "eligibility_filter.exclude_suspended": {"type": "bool", "semantics": "排除停牌"},
    "eligibility_filter.min_listing_days": {"type": "int", "min": 0, "max": 3650,
                                            "semantics": "上市天数下限"},
    "route_pool_map.reversal": {"type": "int", "min": 0, "max": 6000,
                                "semantics": "反转路线候选池只数"},
    "route_pool_map.momentum": {"type": "int", "min": 0, "max": 6000,
                                "semantics": "动量路线候选池只数"},
}

# 当前生产数据里**真的存在**的字段 —— 用来判断「改了能不能预估影响」
_POOL_ESTIMABLE = {"pool_rules.min_amount", "pool_rules.min_mktcap",
                   "pool_rules.price_floor", "pool_rules.drop_mom_top_pct",
                   "liquidity_filter.min_avg_amount_20d",
                   "liquidity_filter.min_avg_turnover_rate"}


def _get_path(obj, dotted, default=None):
    cur = obj
    for seg in dotted.split("."):
        if not isinstance(cur, dict) or seg not in cur:
            return default
        cur = cur[seg]
    return cur


def _set_path(obj, dotted, val):
    segs = dotted.split(".")
    cur = obj
    for seg in segs[:-1]:
        if not isinstance(cur.get(seg), dict):
            cur[seg] = {}
        cur = cur[seg]
    old = cur.get(segs[-1], "<缺>")
    cur[segs[-1]] = val
    return old


def _coerce(v, spec):
    """按 spec 把前端传来的值转成正确类型；不合法抛 ValueError。"""
    t = spec.get("type")
    if t == "bool":
        if isinstance(v, bool):
            return v
        s = str(v).strip().lower()
        if s in ("true", "1", "yes", "on"):
            return True
        if s in ("false", "0", "no", "off"):
            return False
        raise ValueError(f"布尔值无法解析：{v!r}")
    if t == "enum":
        if v not in spec["options"]:
            raise ValueError(f"取值必须是 {spec['options']} 之一，收到 {v!r}")
        return v
    if t == "int":
        f = float(v)
        if abs(f - round(f)) > 1e-9:
            raise ValueError(f"要求整数，收到 {v!r}")
        f = int(round(f))
    else:
        f = float(v)
    if "min" in spec and f < spec["min"] - 1e-12:
        raise ValueError(f"{f} < 下限 {spec['min']}")
    if "max" in spec and f > spec["max"] + 1e-12:
        raise ValueError(f"{f} > 上限 {spec['max']}")
    return f


def validate_pool(edits, cfg_json=None):
    """校验一批选池规则改动。返回 (ok, errors, info, normalized)。

    edits 形如 {"pool_rules.min_amount": 1.2e8, ...}，只允许 POOL_EDITABLE 里的键。
    """
    errs, info = [], []
    pool = cfg_json if cfg_json is not None else _load_json(CFG)
    norm = {}
    for k, v in (edits or {}).items():
        spec = POOL_EDITABLE.get(k)
        if not spec:
            errs.append(f"字段 {k} 不在后台可编辑白名单内（防止误改运维字段）")
            continue
        try:
            norm[k] = _coerce(v, spec)
        except Exception as e:
            errs.append(f"{k}: {e}")

    if errs:
        return False, errs, info, norm

    # 交叉一致性：这些是代码里真实存在的「A 必须先于 B」关系
    def eff(key):
        return norm.get(key, _get_path(pool, key))

    if eff("pool_rules.price_floor") is not None and \
       float(eff("pool_rules.min_mktcap") or 0) <= 0 and \
       float(eff("pool_rules.min_amount") or 0) <= 0:
        info.append("⚠️ 市值/成交额下限都为 0，池子会退化为全A（含微盘），"
                    "流动性风险显著上升")

    lf_on = eff("liquidity_filter.enabled")
    if lf_on and float(eff("liquidity_filter.min_avg_amount_20d") or 0) <= 0:
        info.append("⚠️ 流动性过滤已启用但均额下限为 0 → 等于未过滤")

    sc = eff("industry_limits.max_single_stock_scope")
    mss = float(eff("industry_limits.max_single_stock") or 0)
    if mss > 0:
        need = int(-(-1 // mss)) if mss < 1 else 1
        info.append(f"单票上限 {mss:.0%}（scope={sc}）→ 至少需 {need} 只持仓"
                    + ("（占股票仓位，实测 momentum 路线结构上不可行）"
                       if sc == "equity" else "（占总资产）"))
    if mss > 0.2:
        info.append(f"⚠️ 单票上限 {mss:.0%} 超过 20%，集中度风险偏高（历史上曾是 8%）")

    p2 = float(eff("industry_limits.max_l2_sector") or 0)
    p1 = float(eff("industry_limits.max_l1_parent") or 0)
    if p1 and p2 and p2 > p1:
        errs.append(f"二级上限({p2}) > 一级上限({p1})，二级约束永远不生效")

    if "pool_size" in norm and int(norm["pool_size"]) not in (0,) and \
       int(norm["pool_size"]) < 100:
        info.append(f"⚠️ pool_size={norm['pool_size']} 会强制分层抽样，"
                    "与「全量评分」口径冲突（0 = 不限规模）")

    return (len(errs) == 0), errs, info, norm


def estimate_pool_impact(norm, old_cfg=None):
    """用**当前 scored 文件**估算选池规则改动的直接影响。返回统计 dict。

    ⚠️ old_cfg：**改动前的** config 快照。必须由调用方在写盘前传进来。
      不传的话会读当前 config.json 当「旧值」—— 而 apply_pool 是
      「先写盘再估算」的顺序，那样 old 与 new 恒相等，页面上每一行都显示
      changed=False，等于把「改了什么」这件事整个吞掉（实测踩过）。

    ⚠️ 这只是**近似**，必须如实标注边界：
      1) scored 已是「旧规则筛完之后」的池子 → 若新规则更**宽**（门槛降低），
         本该进池但旧规则已经剔掉的股票**根本不在数据里**，估不出来。
         所以本函数对「放宽」方向只能给下界（n_added=0 不代表真的没新增）。
      2) 换手率/市值/股价都有真实值，可以精确算「谁会因此被剔掉」。
      3) 这里只算**硬约束筛选**，不重算评分与排名（评分没变，变的是谁在池里）。
    """
    d, path = _load_scored()
    if d is None:
        return {"ok": False, "error": "无 scored 文件，无法估算"}
    rows = d.get("results") or []
    if not rows:
        return {"ok": False, "error": "scored 无 results"}

    cfg_cur = old_cfg if old_cfg is not None else _load_json(CFG)

    def cur(k):
        return _get_path(cfg_cur, k)

    def pick(k, conv=float, default=0.0):
        v = norm.get(k, cur(k))
        try:
            return conv(v) if v is not None else default
        except Exception:
            return default

    # (字段名, 标签, 取值函数, 格式化函数)
    RULES = [
        ("pool_rules.min_amount", "当日成交额 < min_amount",
         lambda r: r.get("amount"), lambda v: f"{v/1e8:.2f} 亿" if v else "—"),
        ("pool_rules.min_mktcap", "总市值 < min_mktcap",
         lambda r: r.get("total_mv"), lambda v: f"{v/1e8:.1f} 亿" if v else "—"),
        ("pool_rules.price_floor", "股价 < price_floor",
         lambda r: r.get("price"), lambda v: f"{v:.2f} 元" if v else "—"),
    ]
    if pick("liquidity_filter.enabled", bool, False):
        RULES.append(
            ("liquidity_filter.min_avg_turnover_rate",
             "换手率 < min_avg_turnover_rate（⚠️ 字段是当日换手率，非 20 日均）",
             lambda r: r.get("turnover_ratio"),
             lambda v: f"{v:.3f}%" if v is not None else "—"))

    checks = []
    for key, label, getter, fmt in RULES:
        thr = pick(key)
        if not thr:
            continue
        old = cur(key)
        hit, examples = 0, []
        for r in rows:
            v = getter(r)
            if v is None:
                continue
            if float(v) < thr:
                hit += 1
                if len(examples) < 6:
                    examples.append({"code": r.get("code"), "name": r.get("name"),
                                     "value": fmt(float(v))})
        checks.append({
            "field": key, "rule": label,
            "old": old, "new": thr,
            "changed": (abs(float(old or 0) - float(thr)) > 1e-12),
            "n_dropped": hit,
            "pct_dropped": round(hit / len(rows) * 100, 2),
            "examples": examples,
        })

    return {
        "ok": True,
        "source": os.path.basename(path),
        "data_date": d.get("date"),
        "pool_n": len(rows),
        "checks": checks,
        "note": ("⚠️ 这是**近似估算**，不是预览：scored 文件是「旧规则筛完之后」的池子，"
                 "新规则若更**宽**（门槛降低），本该新进池的股票根本不在数据里，估不出来。"
                 "能精确算的只有「谁会因此被剔掉」。放宽方向请以次日日链的实际池子数为准。"),
    }


def apply_pool(edits, reason=""):
    """写选池规则到 config.json。**不重生成 index**（今日 index 与选池无关）。"""
    with _APPLY_LOCK:
        cur = _load_json(CFG)
        ok, errs, info, norm = validate_pool(edits, cur)
        if not ok:
            _audit({"action": "pool_rejected", "reason": reason,
                    "errors": errs, "edits": edits})
            return {"ok": False, "stage": "validate", "errors": errs, "info": info}

        snap = make_snapshot_extra(["config.json"])
        # ⚠️ old_cfg 必须在写盘**前**拍下来：estimate_pool_impact 需要「旧值」
        #   去算 changed / 旧门槛。写盘后它读到的 config.json 已是新值，
        #   会得出 old==new、每行 changed=False（实测踩过）。
        old_cfg = copy.deepcopy(cur)
        changes = []
        real_change = False
        for k, v in norm.items():
            old = _get_path(cur, k)
            if old != v:
                real_change = True
                changes.append(f"{k}: {old} → {v}")
            _set_path(cur, k, v)

        # ⚠️ 空操作必须**零字节改动**。原实现无条件 _dump_json_keep_fmt →
        #   「什么都没改也点一次 apply」会把整文件重写一遍（mtime 变、类型可能漂移），
        #   而选池规则本来就不重生成 index，改了没改页面上看不出区别。
        if not real_change:
            _audit({"action": "pool_noop", "reason": reason,
                    "note": "无实际改动，未写盘"})
            return {"ok": True, "noop": True, "changes": [],
                    "info": info + ["未检测到实际改动 → 未写盘"],
                    "impact": estimate_pool_impact(norm, old_cfg),
                    "effective": "未写盘，规则保持原样"}

        _dump_json_keep_fmt("config.json", cur)

        # 写后自检：能被 json 读回、值确实是新的
        chk = _load_json(CFG)
        bad = [k for k, v in norm.items()
               if _get_path(chk, k) != v]
        if bad:
            _audit({"action": "pool_write_failed", "bad": bad,
                    "snap": os.path.basename(snap)})
            return {"ok": False, "stage": "verify",
                    "error": f"写盘后回读不一致：{bad}", "changes": changes}

        preview = estimate_pool_impact(norm, old_cfg)
        _audit({"action": "pool_ok", "reason": reason, "changes": changes,
                "snap": os.path.basename(snap),
                "impact_checks": [{k: c[k] for k in
                                   ("rule", "old", "new", "n_dropped")}
                                  for c in (preview.get("checks") or [])]})
        return {"ok": True, "changes": changes, "info": info,
                "snap": os.path.basename(snap), "impact": preview,
                "effective": "次日日链（fetch_top200_sina → step2 重跑）才生效；"
                             "本次**未**重生成 index.html，因为选池不影响今日已算好的分数。"}


# ─────────────────────────────────────────────────────────────────────────────
# ④ 周期规则（cycle_industry.json）
# ─────────────────────────────────────────────────────────────────────────────
# 可编辑字段：四份清单 + 规则阈值
CYCLE_LIST_FIELDS = {
    "l2_list": {"label": "规则1/2 gate 正面清单（申万二级）",
                "consumer": "growth_factor_b 规则1/规则2 gate",
                "effect": "命中才扣成长分（规则1 −10% / 规则2 −15%）"},
    "l1_list": {"label": "一级兼容清单",
                "consumer": "growth_factor_b 历史合成分权重",
                "effect": "仅历史兼容，**不影响**规则1/2 gate"},
    "step2_cycle_lists.theme_list": {
        "label": "step2 主题粗分类（低PE可持续性保护）",
        "consumer": "step2_stockscorer_v2.is_cyclic",
        "effect": "命中就要论证低PE是否盈利高峰陷阱 → 影响 score_sustain"},
    "step2_cycle_lists.sw_cyclic_list": {
        "label": "step2 申万二级（低PE可持续性保护）",
        "consumer": "step2_stockscorer_v2.is_cyclic",
        "effect": "同上（主题粗分类漏掉的行业靠本清单兜底）"},
}
CYCLE_RULE_FIELDS = {
    "cycle_rules.rule1_gm_rise_pp": {"type": "float", "min": 0, "max": 100,
                                     "semantics": "规则1：近4季毛利率均值−前4季均值 > 本值(pp) → 触发"},
    "cycle_rules.rule1_ded": {"type": "float", "min": 0, "max": 1,
                              "semantics": "规则1 扣成长合成分比例"},
    "cycle_rules.rule2_ded": {"type": "float", "min": 0, "max": 1,
                              "semantics": "规则2 扣成长合成分比例"},
    "cycle_rules.rule_ded_cap": {"type": "float", "min": 0, "max": 1,
                                 "semantics": "两规则叠加扣减上限"},
}


def validate_cycle(payload):
    """校验周期规则改动。返回 (ok, errors, info, normalized)。"""
    errs, info = [], []
    norm = {}

    # 已知的申万二级/一级名单 —— 用来抓错别字（写错一个字的行业名永远命中不了）
    sw = _load_json(os.path.join(HERE, "sector", "sw_map.json")) \
        if os.path.exists(os.path.join(HERE, "sector", "sw_map.json")) else {}
    l2_names = set((sw.get("l2_members") or {}).keys())
    l1_names = set((sw.get("l1_members") or {}).keys())

    for k, spec in CYCLE_LIST_FIELDS.items():
        if k not in payload:
            continue
        v = payload[k]
        if not isinstance(v, list):
            errs.append(f"{k} 必须是列表")
            continue
        clean, seen = [], set()
        for x in v:
            s = str(x).strip()
            if not s:
                continue
            if s in seen:
                info.append(f"{k}: 重复项 {s} 已去重")
                continue
            seen.add(s)
            clean.append(s)
        # 错别字检查：申万二级名对不上就一定是打错了
        if k.endswith("l2_list") or k.endswith("sw_cyclic_list"):
            unk = [s for s in clean if l2_names and s not in l2_names]
            if unk:
                errs.append(f"{k} 里这些不是有效的申万二级行业名（打错字？）：{unk}")
        if k.endswith("l1_list"):
            unk = [s for s in clean if l1_names and s not in l1_names]
            if unk:
                errs.append(f"{k} 里这些不是有效的申万一级行业名：{unk}")
        if not clean:
            errs.append(f"{k} 不能为空（清空会让周期判定全部失效）")
        norm[k] = clean
        info.append(f"{k}: {len(clean)} 项")

    # 规则阈值
    rules = {}
    for k, spec in CYCLE_RULE_FIELDS.items():
        if k not in payload:
            continue
        try:
            rules[k.split(".", 1)[1]] = _coerce(payload[k], spec)
        except Exception as e:
            errs.append(f"{k}: {e}")
    if rules:
        r1, r2, cap = rules.get("rule1_ded"), rules.get("rule2_ded"), rules.get("rule_ded_cap")
        if r1 is not None and r2 is not None and cap is not None:
            if cap > 1.0:
                errs.append("rule_ded_cap > 1 会把成长分扣成负数")
            if r1 + r2 > 0 and cap > r1 + r2 + 1e-12:
                info.append(f"rule_ded_cap={cap} > rule1+rule2={r1+r2:.2f}，"
                            "上限实际不起作用（两条规则不会同时触发到顶）")
        norm["cycle_rules"] = rules

    # 语义提醒：这两套清单不能互相替换
    l2 = norm.get("l2_list")
    swl = norm.get("step2_cycle_lists.sw_cyclic_list")
    if l2 is not None and swl is not None:
        gate_only = sorted(set(swl) - set(l2))
        cyc_only = sorted(set(l2) - set(swl))
        info.append(f"两套清单差异：step2 独有 {len(gate_only)} 项 / "
                    f"gate 独有 {len(cyc_only)} 项 —— "
                    "语义不同（低PE保护 vs 规则gate），**互不替换**")
        if set(l2) == set(swl):
            info.append("⚠️ 你把两套清单改成完全一样了："
                        "step2 的低PE保护范围会扩大到规则 gate 口径，"
                        "历史上 step2 故意多保护 15 个行业，请确认这是有意的")

    return (len(errs) == 0), errs, info, norm


def estimate_cycle_impact(payload):
    """用当前 scored 的 sector_l2 / is_cyclic 估算周期清单改动的命中数变化。

    ⚠️ 边界：只能算「清单命中只数」的变化，**算不出**分数变化 ——
       规则1/2 扣分依赖毛利率环比（gm_rise_pp）与去均值 ΔG_q，
       这两项不在 scored 文件里，得重跑 growth_factor_b。
    """
    d, path = _load_scored()
    if d is None:
        return {"ok": False, "error": "无 scored 文件"}
    rows = d.get("results") or []
    cyc_json = _load_json(CYCLE)

    def counts(l2set, swset):
        n_gate = sum(1 for r in rows if r.get("sector_l2") in l2set)
        n_s2 = sum(1 for r in rows
                   if r.get("sector") in swset or r.get("sector_l2") in swset)
        return n_gate, n_s2

    cur_l2 = set(cyc_json.get("l2_list") or [])
    s2 = cyc_json.get("step2_cycle_lists") or {}
    cur_theme = set(s2.get("theme_list") or [])
    cur_sw = set(s2.get("sw_cyclic_list") or [])

    new_l2 = set(payload.get("l2_list") or cur_l2)
    new_theme = set(payload.get("step2_cycle_lists.theme_list") or cur_theme)
    new_sw = set(payload.get("step2_cycle_lists.sw_cyclic_list") or cur_sw)

    a_gate, a_s2 = counts(cur_l2, cur_theme | cur_sw)
    b_gate, b_s2 = counts(new_l2, new_theme | new_sw)

    def flips(newset, oldset, key):
        add = [r for r in rows if r.get(key) in (newset - oldset)]
        rem = [r for r in rows if r.get(key) in (oldset - newset)]
        return add, rem

    add_g, rem_g = flips(new_l2, cur_l2, "sector_l2")
    add_sw, rem_sw = flips(new_theme | new_sw, cur_theme | cur_sw, "sector_l2")

    return {
        "ok": True, "source": os.path.basename(path), "data_date": d.get("date"),
        "pool_n": len(rows),
        "gate": {"cur": a_gate, "new": b_gate, "delta": b_gate - a_gate,
                 "n_added": len(add_g), "n_removed": len(rem_g),
                 "added": [{"name": r.get("name"), "l2": r.get("sector_l2")}
                           for r in add_g[:10]],
                 "removed": [{"name": r.get("name"), "l2": r.get("sector_l2")}
                             for r in rem_g[:10]]},
        "step2": {"cur": a_s2, "new": b_s2, "delta": b_s2 - a_s2,
                  "n_added": len(add_sw), "n_removed": len(rem_sw),
                  "added": [{"name": r.get("name"), "l2": r.get("sector_l2")}
                            for r in add_sw[:10]],
                  "removed": [{"name": r.get("name"), "l2": r.get("sector_l2")}
                              for r in rem_sw[:10]]},
        "note": ("命中**只数**可精确算；分数变化算不出 —— 规则1/2 扣分依赖"
                 "毛利率环比与去均值 ΔG_q，这两项不在 scored 文件里，"
                 "需重跑 growth_factor_b（即次日日链）。"),
    }


def apply_cycle(payload, reason="", regenerate=True):
    """写周期规则。改 l2_list 会改变成长分 → 必须重生成 index 才能一致。

    为什么这几项要重生成，而选池规则不用：
      周期清单 → 规则1/2 gate → 扣 score_growth 合成分 → score_growth 变
      → total 变。而选池规则只决定「谁在池里」，不改已算好的分数。
    """
    with _APPLY_LOCK:
        ok, errs, info, norm = validate_cycle(payload)
        if not ok:
            _audit({"action": "cycle_rejected", "reason": reason,
                    "errors": errs, "payload": payload})
            return {"ok": False, "stage": "validate", "errors": errs, "info": info}

        cur = _load_json(CYCLE)
        old = copy.deepcopy(cur)
        snap = make_snapshot_extra(["sector/cycle_industry.json"])
        changes = []
        real_change = False

        for k, v in norm.items():
            if k == "cycle_rules":
                # 阈值在代码里是常量，不在 json 里（历史原因）——
                # 写进 json 只会变成「写了但没人读」的假配置，所以直接拒绝。
                changes.append(
                    "⚠️ 规则阈值不在本 json 里：growth_factor_b.py 的 "
                    "RULE1_GM_RISE_PP/RULE1_DED/RULE2_DED/RULE_DED_CAP 是**代码常量**，"
                    "写进 cycle_industry.json 不会有任何效果。本轮**未写入**阈值，"
                    "如需可配需先改造 growth_factor_b 读取入口。")
                continue
            segs = k.split(".")
            tgt = cur
            for s in segs[:-1]:
                tgt = tgt.setdefault(s, {})
            oldv = tgt.get(segs[-1])
            if oldv != v:
                real_change = True
                add = sorted(set(v) - set(oldv or []))
                rem = sorted(set(oldv or []) - set(v))
                changes.append(
                    f"{k}: {len(oldv or [])} 项 → {len(v)} 项"
                    + (f"（+{add}）" if add else "")
                    + (f"（−{rem}）" if rem else ""))
            tgt[segs[-1]] = v

        # ⚠️ 空操作必须**零字节改动**：一项都没变时不写盘、不动 version/fitted_at。
        #   旧实现无条件刷 version + 整文件 dumps → 「点一下 apply 但什么都没改」
        #   也会让文件变脏（version 变成 admin-xxx、数组折行被重写），
        #   老大看到 diff 却找不出语义差异。这里直接提前返回。
        if not real_change:
            _audit({"action": "cycle_noop", "reason": reason,
                    "note": "无实际改动，未写盘（version/fitted_at 保持不变）"})
            return {"ok": True, "noop": True, "changes": changes,
                    "info": info + ["未检测到实际改动 → 未写盘"],
                    "regenerate": "（无改动，未重生成 index）",
                    "impact": estimate_cycle_impact(payload)}

        cur["version"] = f"admin-{datetime.datetime.now().strftime('%Y%m%d-%H%M%S')}"
        cur["fitted_at"] = _now()

        # 清单类字段走**局部保真替换**（只动数组块内元素，块外零改动）；
        # 其余标量字段（version/fitted_at）用文本级定点替换，同样不重排全文件。
        list_updates = {k.split(".")[-1]: v for k, v in norm.items()
                        if isinstance(v, list)}
        txt, applied, skipped = patch_str_arrays(
            "sector/cycle_industry.json", list_updates)
        for k2 in list_updates:
            if k2 in skipped and any(k.endswith("." + k2) or k == k2
                                     for k in norm):
                changes.append(
                    f"⚠️ {k2} 未做局部替换（该键不是独占一行的数组块），"
                    "已回退整文件写盘 → 该块折行会被重排")
        # 标量字段定点替换：只在原文里改这两个字面量，不碰其它字节
        txt = re.sub(r'("version"\s*:\s*")[^"]*(")',
                     lambda m: m.group(1) + cur["version"] + m.group(2),
                     txt, count=1)
        txt = re.sub(r'("fitted_at"\s*:\s*")[^"]*(")',
                     lambda m: m.group(1) + cur["fitted_at"] + m.group(2),
                     txt, count=1)
        with open(os.path.join(HERE, "sector/cycle_industry.json"),
                  "w", encoding="utf-8") as f:
            f.write(txt)
        # 写回后必须能解析，否则宁可整文件重写（不能留半坏文件）
        try:
            _chk = _load_json(CYCLE)
            if _chk.get("version") != cur["version"]:
                raise ValueError("version 字段未生效")
        except Exception as e:
            _dump_json_keep_fmt("sector/cycle_industry.json", cur)
            changes.append(f"⚠️ 局部写盘校验失败({e}) → 已回退整文件写盘")

        impact = estimate_cycle_impact(payload)

        regen_msg = ""
        if regenerate:
            rok, rmsg, _ = regenerate_index()
            regen_msg = rmsg if rok else ("⚠️ " + rmsg)
        else:
            regen_msg = "（按要求未重生成 index）"

        _audit({"action": "cycle_ok", "reason": reason, "changes": changes,
                "snap": os.path.basename(snap), "regenerate": regen_msg,
                "impact_gate": (impact.get("gate") or {}).get("delta"),
                "impact_step2": (impact.get("step2") or {}).get("delta")})
        return {"ok": True, "changes": changes, "info": info,
                "snap": os.path.basename(snap), "impact": impact,
                "regenerate": regen_msg,
                "warning": ("⚠️ 改了 l2_list/step2 清单的一瞬间，index.html 里的 wind 成长分"
                            "就与配置不一致了 —— 必须重跑日链（含 growth_factor_b 与 step2）"
                            "才算真正生效。本次只做了「配置落盘 + 重生成现有数据」。" 
                            if "l2_list" in norm or any(
                                k.startswith("step2_cycle_lists") for k in norm)
                            else "")}


def make_snapshot_extra(relpaths):
    """快照额外文件（config.json / cycle_industry.json）。"""
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    snap = os.path.join(HERE, f".snap_admin_{ts}")
    os.makedirs(snap, exist_ok=True)
    for rp in SNAPSHOT_FILES + list(relpaths):
        src = os.path.join(HERE, rp)
        if os.path.exists(src):
            dst = os.path.join(snap, rp.replace(os.sep, "_") + ".bak")
            shutil.copy2(src, dst)
    _dump_json(os.path.join(snap, "_manifest.json"),
               {"ts": ts, "files": SNAPSHOT_FILES + list(relpaths)})
    return snap


# ─────────────────────────────────────────────────────────────────────────────
# TOP-N 预览（用「编辑中」的权重重算，不写盘）
# ─────────────────────────────────────────────────────────────────────────────
_SCORED_CACHE = {"path": None, "mtime": None, "data": None}
_DIM_CACHE = {"mtime": None, "dim_max": None}


def _load_dim_max():
    """dim_max 单一来源：factor_weights.json（带 mtime 缓存）。"""
    mt = os.path.getmtime(FW)
    if _DIM_CACHE["mtime"] == mt and _DIM_CACHE["dim_max"] is not None:
        return _DIM_CACHE["dim_max"]
    cfg = _load_json(FW)
    dm = {k: float(v) for k, v in (cfg.get("dim_max") or {}).items()}
    _DIM_CACHE["mtime"] = mt
    _DIM_CACHE["dim_max"] = dm
    return dm


def _load_scored():
    """加载最新 top200_scored_*.json（带 mtime 缓存，文件 12MB 不能每次读）。"""
    cands = sorted(glob.glob(os.path.join(HERE, "top200_scored_*.json")))
    if not cands:
        return None, None
    p = cands[-1]
    mt = os.path.getmtime(p)
    if _SCORED_CACHE["path"] == p and _SCORED_CACHE["mtime"] == mt:
        return _SCORED_CACHE["data"], p
    d = _load_json(p)
    _SCORED_CACHE.update({"path": p, "mtime": mt, "data": d})
    return d, p


def composite_one(rec, weights, dim_max):
    """按**生产口径**重算单只 total（百分制）。

    口径必须与 scale_calc.composite_total 逐字一致：
      缺维 → 中性填充 DIM_MAX/2（归一后恒 5.0）；
      分母恒为 Σ全部生效权重（**不随缺失变化**）；
      末了 ×10 得百分制。
    ⚠️ round125 我曾在别处用「缺维从分母剔除」，与生产差 65 分 —— 那是另一套口径，
       不能混用。这里只走生产口径，并用调用方的自证来兜底。
    """
    num = 0.0
    den = 0.0
    missing = []
    for k, w in weights.items():
        w = float(w or 0)
        if w <= 0:
            continue
        den += w
        mx = float(dim_max.get(k) or 10.0)
        if mx <= 0:
            mx = 10.0
        v = rec.get(k)
        if isinstance(v, (int, float)):
            num += min(float(v), mx) / mx * 10.0 * w
        else:
            num += 0.5 * 10.0 * w      # 中性填充 DMAX/2 → 归一后恒 5.0
            missing.append(k)
    if den <= 0:
        return None, missing
    return round(num / den * 10.0, 2), missing


def preview_topn(weights, top_n=10, verify_caliber=True):
    """用给定权重重算全池并返回 TOP-N。**不写盘、不改任何文件**。

    `verify_caliber`：先用**已生效权重**重算，与文件里的 total 比对。
    差 > 0.005 说明口径走偏了，此时**拒绝**返回预览 —— 宁可报错也不给
    一份「看起来像但其实不是 index 口径」的榜单。
    """
    d, path = _load_scored()
    if d is None:
        return {"ok": False, "error": "无 top200_scored_*.json，无法预览"}

    rows = d.get("results") or []
    if not rows:
        return {"ok": False, "error": "scored 文件里无 results"}

    dim_max = _load_dim_max()

    # ── 口径自证 ──
    caliber = {"checked": False}
    if verify_caliber:
        active_now = {k: float(v) for k, v in
                      (_load_json(FW).get("weights") or {}).items() if float(v or 0) > 0}
        diffs = []
        for r in rows:
            s, _ = composite_one(r, active_now, dim_max)
            if s is not None and isinstance(r.get("total"), (int, float)):
                diffs.append(abs(s - float(r["total"])))
        if diffs:
            mx = max(diffs)
            caliber = {"checked": True, "max_abs_diff": round(mx, 6),
                       "n": len(diffs),
                       "passed": mx <= 0.005}
            if mx > 0.005:
                # 列出偏差最大的几只，便于定位是哪一维口径不同
                worst = []
                for r in rows[:400]:
                    s, _ = composite_one(r, active_now, dim_max)
                    if s is not None and isinstance(r.get("total"), (int, float)):
                        worst.append((abs(s - float(r["total"])), r.get("code"),
                                      r.get("name"), float(r["total"]), s))
                worst.sort(reverse=True)
                return {
                    "ok": False, "stage": "caliber",
                    "error": (f"口径自证失败：用已生效权重重算与文件 total 最大差 "
                              f"{mx:.4f} 分（阈值 0.005）。预览不可信，已拒绝。"),
                    "caliber": caliber,
                    "worst": [{"code": c, "name": n, "file_total": t,
                               "recomputed": s, "diff": round(dd, 4)}
                              for dd, c, n, t, s in worst[:5]],
                }

    # ── 用目标权重重算 ──
    scored = []
    for r in rows:
        s, miss = composite_one(r, weights, dim_max)
        if s is not None:
            scored.append((s, miss, r))
    scored.sort(key=lambda x: (-x[0], x[2].get("code") or ""))

    # ── 生效权重下的排名（用于对比名次变化）──
    active_now = {k: float(v) for k, v in
                  (_load_json(FW).get("weights") or {}).items() if float(v or 0) > 0}
    base_sorted = sorted(
        [(composite_one(r, active_now, dim_max)[0], r.get("code"))
         for r in rows],
        key=lambda x: (-(x[0] if x[0] is not None else -1), x[1] or ""))
    base_rank = {c: i + 1 for i, (_, c) in enumerate(base_sorted)}

    out = []
    for i, (s, miss, r) in enumerate(scored[:top_n], 1):
        code = r.get("code")
        prev = base_rank.get(code)
        row = {
            "rank": i,
            "prev_rank": prev,
            "rank_change": (prev - i) if prev else None,
            "code": code,
            "name": r.get("name"),
            "total": s,
            "base_total": (composite_one(r, active_now, dim_max)[0]),
            "sector": r.get("sector"),
            "sector_src": r.get("sector_src"),
            "circ_mv_yi": round((r.get("circ_mv") or 0) / 1e8, 2),
            "amount_yi": round((r.get("amount") or 0) / 1e8, 2),
            "pe": r.get("pe"),
            "pb": r.get("pb"),
            "rating": r.get("rating"),
            "price": r.get("price"),
            "pct_chg": r.get("pct_chg"),
            "neutral_missing": [m.replace("score_", "") for m in miss],
            "scores": {k.replace("score_", ""): r.get(k)
                       for k in weights if float(weights[k] or 0) > 0},
        }
        out.append(row)

    return {
        "ok": True, "ts": _now(),
        "source": os.path.basename(path),
        "data_date": d.get("date"),
        "pool_n": len(rows),
        "caliber": caliber,
        "weights": {k: float(v) for k, v in weights.items() if float(v or 0) > 0},
        "summary": {
            "top_total": out[0]["total"] if out else None,
            "top10_mean": round(sum(x["total"] for x in out) / len(out), 2) if out else None,
            "n_new_entries": sum(1 for x in out if (x["prev_rank"] or 999) > top_n),
            "n_entered": [x["name"] for x in out if (x["prev_rank"] or 999) > top_n],
        },
        "rows": out,
        "note": ("预览用「生产口径」逐股重算：缺维填中性 DIM_MAX/2、分母恒 Σ生效权重。"
                 "本操作**不写盘**，只读 scored 文件。真正生效需点「生效并重生成 index」。"),
    }


# ─────────────────────────────────────────────────────────────────────────────
# 生效主流程
# ─────────────────────────────────────────────────────────────────────────────
_APPLY_LOCK = threading.Lock()


def apply_weights(new_weights, regenerate=True, reason=""):
    """完整生效链路：校验 → 快照 → 八处同步 → verify → 重生成 → 审计。"""
    with _APPLY_LOCK:
        cfg = _load_json(FW)
        ok, errs, info, gsum = validate_weights(new_weights, cfg)
        if not ok:
            _audit({"action": "apply_rejected", "reason": reason,
                    "errors": errs, "weights": new_weights})
            return {"ok": False, "stage": "validate", "errors": errs, "info": info}

        snap = make_snapshot()
        changes, err = sync_eight_places(new_weights, dry_run=False)
        if err:
            restored = rollback(snap)
            _audit({"action": "apply_rollback", "stage": "sync",
                    "error": err, "restored": restored, "snap": snap,
                    "reason": reason})
            return {"ok": False, "stage": "sync", "error": err,
                    "restored": restored, "snap": os.path.basename(snap)}

        vok, vout = run_verify()
        if not vok:
            restored = rollback(snap)
            _audit({"action": "apply_rollback", "stage": "verify",
                    "verify_tail": vout[-1500:], "restored": restored,
                    "snap": snap, "reason": reason})
            return {"ok": False, "stage": "verify",
                    "error": "verify_weights_round36.py 未通过，已回滚",
                    "verify_tail": vout[-1500:], "restored": restored,
                    "snap": os.path.basename(snap)}

        regen_msg, regen_detail = "", ""
        if regenerate:
            rok, regen_msg, regen_detail = regenerate_index()
            if not rok:
                # 报告失败不影响权重本身（权重已通过 verify），但要如实报告
                regen_msg = "⚠️ " + regen_msg

        _audit({"action": "apply_ok", "reason": reason, "weights": new_weights,
                "changes": changes, "group_sum": gsum,
                "snap": os.path.basename(snap),
                "regenerate": regen_msg, "verify_tail": vout[-800:]})

        return {"ok": True, "changes": changes, "info": info,
                "group_sum": gsum, "snap": os.path.basename(snap),
                "regenerate": regen_msg, "regenerate_detail": regen_detail,
                "verify_tail": vout[-1500:]}


def rollback_latest():
    """回滚到最近一次快照，并**重生成 index.html**。

    ⚠️ round125 补：只回滚权重不重生成 = 展示页还挂着旧权重的分数，
    维护页/展示页立刻又不一致了 —— 这正是要消灭的病。所以回滚必须跟一次重生成。
    """
    snaps = sorted(glob.glob(os.path.join(HERE, ".snap_admin_*")))
    if not snaps:
        return {"ok": False, "error": "无可用快照"}
    snap = snaps[-1]
    restored = rollback(snap)
    vok, vout = run_verify()

    regen_msg = ""
    if vok:
        rok, regen_msg, _ = regenerate_index()
        if not rok:
            regen_msg = "⚠️ " + regen_msg
    else:
        regen_msg = "⚠️ verify 未过，跳过重生成（避免带病出图）"

    _audit({"action": "manual_rollback", "snap": os.path.basename(snap),
            "restored": restored, "verify_ok": vok,
            "regenerate": regen_msg})
    return {"ok": True, "snap": os.path.basename(snap), "restored": restored,
            "verify_ok": vok, "regenerate": regen_msg,
            "verify_tail": vout[-1200:]}


# ─────────────────────────────────────────────────────────────────────────────
# HTTP 服务
# ─────────────────────────────────────────────────────────────────────────────
class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):  # 安静
        pass

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        data = body if isinstance(body, bytes) else \
            json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n <= 0:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode("utf-8"))
        except Exception:
            return {}

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        path = u.path
        try:
            if path in ("/", "/index", "/admin"):
                p = os.path.join(HERE, "admin.html")
                if not os.path.exists(p):
                    self._send(404, {"error": "admin.html 不存在"})
                    return
                with open(p, "rb") as f:
                    self._send(200, f.read(), "text/html; charset=utf-8")
            elif path == "/api/config":
                self._send(200, read_config_snapshot())
            elif path == "/api/status":
                vok, vout = run_verify()
                self._send(200, {"verify_ok": vok, "verify_tail": vout[-2500:],
                                 "ts": _now()})
            elif path == "/api/audit":
                self._send(200, read_audit())
            elif path == "/api/snapshots":
                snaps = sorted(glob.glob(os.path.join(HERE, ".snap_admin_*")))
                self._send(200, [os.path.basename(s) for s in snaps])
            else:
                self._send(404, {"error": f"unknown path {path}"})
        except Exception as e:
            self._send(500, {"error": str(e),
                             "trace": traceback.format_exc()[-800:]})

    def do_POST(self):
        u = urllib.parse.urlparse(self.path)
        path = u.path
        try:
            body = self._body()
            if path == "/api/validate":
                cfg = _load_json(FW)
                ok, errs, info, gsum = validate_weights(
                    body.get("weights") or {}, cfg)
                self._send(200, {"ok": ok, "errors": errs, "info": info,
                                 "group_sum": gsum})
            elif path == "/api/preview":
                w = body.get("weights") or {}
                if not w:
                    self._send(400, {"ok": False, "error": "缺 weights"})
                    return
                try:
                    n = int(body.get("top_n") or 10)
                except Exception:
                    n = 10
                n = max(1, min(n, 50))
                self._send(200, preview_topn(
                    w, top_n=n,
                    verify_caliber=bool(body.get("verify_caliber", True))))
            elif path == "/api/apply":
                w = body.get("weights") or {}
                # 后端闸：必须带与本次权重匹配的 confirm 指纹。
                # 这不是重复前端 —— 它防的是「直接 curl 调接口绕过二次确认」。
                want = weights_fingerprint(w)
                got = (body.get("confirm") or "").strip()
                if got != want:
                    _audit({"action": "apply_blocked_no_confirm",
                            "weights": w,
                            "reason": "缺少或错误的二次确认指纹"})
                    self._send(400, {
                        "ok": False, "stage": "confirm",
                        "error": (f"未通过二次确认：请求需带 confirm={want}"
                                  "（本次权重指纹）。前端确认弹窗会自动带上。"),
                        "expected_confirm": want,
                    })
                    return
                res = apply_weights(w,
                                    regenerate=bool(body.get("regenerate", True)),
                                    reason=body.get("reason") or "")
                self._send(200 if res.get("ok") else 400, res)
            elif path == "/api/rollback":
                self._send(200, rollback_latest())
            # ── ② 大类约束 ──
            elif path == "/api/constraints/validate":
                cfg = _load_json(FW)
                wc = body.get("constraints") or {}
                ok, errs, info = validate_constraints(wc, cfg.get("weights") or {})
                self._send(200, {"ok": ok, "errors": errs, "info": info})
            elif path == "/api/constraints/apply":
                wc = body.get("constraints") or {}
                res = apply_constraints(wc, reason=body.get("reason") or "")
                self._send(200 if res.get("ok") else 400, res)
            # ── ③ 选池规则 ──
            elif path == "/api/pool/validate":
                ok, errs, info, norm = validate_pool(body.get("edits") or {})
                self._send(200, {"ok": ok, "errors": errs, "info": info,
                                 "normalized": norm})
            elif path == "/api/pool/estimate":
                ok, errs, info, norm = validate_pool(body.get("edits") or {})
                if not ok:
                    self._send(400, {"ok": False, "errors": errs})
                    return
                self._send(200, estimate_pool_impact(norm))
            elif path == "/api/pool/apply":
                res = apply_pool(body.get("edits") or {},
                                 reason=body.get("reason") or "")
                self._send(200 if res.get("ok") else 400, res)
            # ── ④ 周期规则 ──
            elif path == "/api/cycle/validate":
                ok, errs, info, norm = validate_cycle(body.get("payload") or {})
                self._send(200, {"ok": ok, "errors": errs, "info": info,
                                 "normalized": norm})
            elif path == "/api/cycle/estimate":
                self._send(200, estimate_cycle_impact(body.get("payload") or {}))
            elif path == "/api/cycle/apply":
                res = apply_cycle(body.get("payload") or {},
                                  reason=body.get("reason") or "",
                                  regenerate=bool(body.get("regenerate", True)))
                self._send(200 if res.get("ok") else 400, res)
            else:
                self._send(404, {"error": f"unknown path {path}"})
        except Exception as e:
            self._send(500, {"error": str(e),
                             "trace": traceback.format_exc()[-800:]})


class Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def selfcheck():
    """--once 自检：配置可读、约束可校验、八处落点存在、verify 能跑。"""
    print("=" * 72)
    print(f"admin_server 自检  { _now() }")
    print("=" * 72)
    cfg = _load_json(FW)
    w = cfg.get("weights") or {}
    active = {k: v for k, v in w.items() if float(v or 0) > 0}
    print(f"  version        : {cfg.get('version')}")
    print(f"  target_horizon : {cfg.get('target_horizon')}")
    print(f"  生效维          : {len(active)} 个")
    for k, v in sorted(active.items(), key=lambda x: -float(x[1])):
        print(f"      {k.replace('score_',''):<16} {float(v):.3f}")
    print(f"  Σw = {sum(float(v or 0) for v in w.values()):.6f}")

    ok, errs, info, gsum = validate_weights(w, cfg)
    print(f"\n  约束校验        : {'PASS' if ok else 'FAIL'}")
    for s in info:
        print(f"      {s}")
    for e in errs:
        print(f"      ❌ {e}")

    print("\n  八处同步落点    :")
    for fn, pat in WEIGHT_TARGETS:
        p = os.path.join(HERE, fn)
        ex = "✅" if os.path.exists(p) else "❌ 缺失"
        print(f"      {ex} {fn} : {pat}")

    vok, vout = run_verify()
    print(f"\n  verify_weights_round36 : {'PASS' if vok else 'FAIL'}")
    tail = [ln for ln in vout.splitlines() if ln.strip()][-12:]
    for ln in tail:
        print(f"      {ln}")
    print("=" * 72)
    return 0 if (ok and vok) else 1


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8788)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--once", action="store_true", help="只自检不起服务")
    args = ap.parse_args()

    if args.once:
        sys.exit(selfcheck())

    os.chdir(HERE)
    srv = Server((args.host, args.port), Handler)
    print(f"权重配置后台已启动: http://{args.host}:{args.port}/")
    print(f"  展示页 index.html 与维护页分离；改完点「生效」才会重生成 index.html")
    print(f"  Ctrl+C 退出")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n已退出")


if __name__ == "__main__":
    main()
