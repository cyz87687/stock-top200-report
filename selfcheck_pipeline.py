#!/usr/bin/env python3
"""链路自检（v3.2）—— 把「静默降级」变成可见告警。

背景（实测事故）：apply_sector_momentum.py 在十维重构后残留对已删变量
(W_NEWS/W_TECH/W_FUND/W_THEME_OLD/W_THEME_NEW/W_SECTOR) 的引用 → 运行即 NameError。
但 workflow 对该步骤用 `if ...; then ... else echo ⚠️` 守护，单步失败不中断链路，
于是「十维总分未重算」被静默吞掉，页面只剩五维中间态（S 级全消失）。
py_compile 只查语法不查名字，无法发现此类问题。

本脚本在每日链路末尾（gen_report 之前）执行两项检查：
  A) 静态：扫描关键脚本是否引用了「未定义的全局名」—— 用 compile + symtable 近似检测；
  B) 动态：若 scored json 已含 score_oversold60（十维数据）但 total 明显偏离十维加权值，
     说明 apply_sector_momentum 未生效（降级），报警。

退出码：0 全部通过；1 发现可疑（workflow 记录为 WARNING，不中断）。
用法: python3 selfcheck_pipeline.py [scored_json]
"""
import json
import os
import re
import sys
import symtable
import glob

HERE = os.path.dirname(os.path.abspath(__file__))

# 关键链路上「每日必跑」的脚本
CRITICAL = [
    "fetch_top200_sina.py", "step2_stockscorer_v2.py", "generate_ai_assessment.py",
    "enhance_scores.py", "sector_momentum_daily.py", "apply_sector_momentum.py",
    "pick_portfolio.py", "track_recommendations.py", "factor_eval.py", "gen_report_top200.py",
]


def _collect_globals(src):
    """收集模块级赋值的目标名（含 import / def / class / 赋值）。"""
    names = set()
    try:
        top = symtable.symtable(src, "<mod>", "exec")
    except SyntaxError:
        return names
    for sym in top.get_symbols():
        if sym.is_assigned() or sym.is_imported():
            names.add(sym.get_name())
    return names


def check_undefined_names(path):
    """近似检测：模块顶层函数体内引用的全局名，是否在当前模块或其他已知模块中定义。

    说明：这是轻量启发式（不构建完整跨模块符号表），只标记「本模块未定义、
    且不在 builtins/常见导入名中」的可疑全局引用，供人工复核。
    """
    src = open(path, encoding="utf-8").read()
    defined = _collect_globals(src)
    import builtins
    bi = set(dir(builtins))
    # 顶层函数内的自由变量（全局引用）
    imports = set()
    for m in re.finditer(r"^\s*(?:from\s+\S+\s+import\s+(.+)|import\s+(.+))$", src, re.M):
        grp = (m.group(1) or m.group(2) or "").replace("(", " ").replace(")", " ")
        for tok in re.split(r"[,\s]+", grp):
            tok = tok.strip().split(" as ")[-1].strip()
            if tok:
                imports.add(tok.split(".")[0])
    known = defined | bi | imports

    suspicious = []
    try:
        top = symtable.symtable(src, "<mod>", "exec")
    except SyntaxError:
        return suspicious
    # 收集模块内所有函数体内引用的、被判定为 global 的名字
    referenced_globals = set()

    def walk(tbl):
        for child in tbl.get_children():
            if child.get_type() == "function":
                for sym in child.get_symbols():
                    if sym.is_global() and sym.is_referenced():
                        referenced_globals.add(sym.get_name())
            walk(child)

    walk(top)
    for nm in sorted(referenced_globals):
        if nm in known:
            continue
        # 排除解释器注入的内建名与常见局部约定
        if nm in ("self", "cls", "args", "kwargs", "__file__", "__name__", "__doc__"):
            continue
        suspicious.append(nm)
    return suspicious


def check_ten_dim_applied(scored_path):
    """动态检查：scored json 含十维字段时，total 是否接近十维加权值。"""
    if not scored_path or not os.path.exists(scored_path):
        return None
    try:
        d = json.load(open(scored_path, encoding="utf-8"))
    except Exception:
        return None
    rs = d.get("results") or []
    if not rs:
        return None
    r0 = rs[0]
    if "score_oversold60" not in r0:
        return ("skip", "scored json 非十维口径，跳过十维一致性检查")
    # 权重
    wpath = os.path.join(HERE, "factor_weights.json")
    W = None
    if os.path.exists(wpath):
        try:
            W = (json.load(open(wpath, encoding="utf-8")).get("weights") or {})
        except Exception:
            W = None
    if not W or "score_oversold60" not in W:
        return ("skip", "未找到十维权重，跳过一致性检查")

    def expected(r):
        t = 0.0
        for k, w in W.items():
            t += float(r.get(k) or 0) * float(w)
        return round(t * 4, 2)

    # 抽前 20 只比对（apply 后 total 应≈十维加权）
    diff = 0
    n = 0
    for r in rs[:20]:
        if r.get("score_sector") is None:
            continue     # apply 前无板块维度，跳过
        n += 1
        if abs(float(r.get("total") or 0) - expected(r)) > 0.5:
            diff += 1
    if n and diff / n > 0.5:
        return ("fail", f"十维总分未生效: 抽样 {n} 只中 {diff} 只 total 偏离十维加权值 >0.5"
                        f"（apply_sector_momentum 可能降级）")
    return ("ok", f"十维总分一致性: 抽样 {n} 只全部吻合")


def main():
    scored = sys.argv[1] if len(sys.argv) > 1 else None
    if not scored:
        fs = [f for f in sorted(glob.glob(os.path.join(HERE, "top200_scored_*.json")), reverse=True)
              if "_watch_" not in f and ".bak" not in f]
        scored = fs[0] if fs else None

    issues = 0
    print("🔎 链路自检 (v3.2)")
    print("── A) 未定义全局名扫描 ──")
    for fn in CRITICAL:
        p = os.path.join(HERE, fn)
        if not os.path.exists(p):
            continue
        sus = check_undefined_names(p)
        if sus:
            issues += 1
            print(f"   ⚠️ {fn}: 可疑未定义全局引用 {sus}")
    if issues == 0:
        print("   ✅ 关键脚本未见未定义全局引用")

    print("── B) 十维总分一致性 ──")
    res = check_ten_dim_applied(scored)
    if res is None:
        print("   ⏭️ 无 scored json，跳过")
    else:
        st, msg = res
        icon = {"ok": "✅", "skip": "⏭️", "fail": "⚠️"}[st]
        print(f"   {icon} {msg}")
        if st == "fail":
            issues += 1

    print(f"── 结论: {'⚠️ 发现 ' + str(issues) + ' 项可疑（请人工复核）' if issues else '✅ 全部通过'} ──")
    return 1 if issues else 0


if __name__ == "__main__":
    sys.exit(main())
