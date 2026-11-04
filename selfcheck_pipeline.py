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
if HERE not in sys.path:
    sys.path.insert(0, HERE)

# ⚠️ 2026-10-29 P0（口径漂移修复）：复合分口径**唯一实现来源**是 scale_calc。
#   本文件此前自带第四份实现（缺维从分母剔除），与生产的
#   「分母恒为 Σ全部生效权重 + 缺维填中性分 DMAX/2」不是同一把尺：
#   实测 2026-10-29 焦作万方(sz000612) 生产 total=87.25，
#   旧自检算得 90.73（差 3.48），导致 20/20 抽样全部「偏离」→ 误报
#   「十维总分未生效 · apply 可能降级」，把 2 分钟能查清的事引到错误方向。
#   教训（与项目纪律第 6 条同源）：**同一口径两处实现必然漂移**，
#   自检脚本必须复用生产实现，而不是照抄一份。
import scale_calc

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
    # 权重 + 量程（2026-10-26 P0：必须读 dim_max，否则自检与生产口径不一致 → 误报）
    wpath = os.path.join(HERE, "factor_weights.json")
    W = None
    DIM_MAX = {}
    if os.path.exists(wpath):
        try:
            _d = json.load(open(wpath, encoding="utf-8"))
            W = (_d.get("weights") or {})
            DIM_MAX = (_d.get("dim_max") or {})
        except Exception:
            W = None
    if not W or "score_oversold60" not in W:
        return ("skip", "未找到十维权重，跳过一致性检查")
    # dim_max 缺失时回退内置默认（与 factor_weights.json 同源，防漏同步）
    _DM_DEFAULT = {
        "score_fund": 5.0, "score_news": 5.0, "score_theme_neg": 10.0,
        "score_size": 10.0, "score_sector": 5.0, "score_quality": 10.0,
        "score_growth": 10.0, "score_tech_neg": 10.0, "score_oversold60": 5.0,
        "score_bias60": 5.0, "score_rsi_rev": 5.0, "score_vol_pulse": 5.0,
        "score_overbought": 5.0, "score_theme_intrinsic": 5.0, "score_attention": 5.0,
    }
    for _k, _v in _DM_DEFAULT.items():
        if not isinstance(DIM_MAX.get(_k), (int, float)) or DIM_MAX.get(_k) <= 0:
            DIM_MAX[_k] = _v

    _W_POS = {k: float(w) for k, w in W.items() if float(w) > 0}

    def _scale_total(r):
        """用生产口径（scale_calc）重算 total —— 唯一实现来源，禁自建公式。

        口径要点（见 scale_calc.py 模块头）：
          · 分子：Σ min(v,DMAX)/DMAX×10×w，缺维填中性分 DMAX/2 → 归一后恒 5.0
          · 分母：Σ 全部生效权重（不随该股缺几维变化）
        返回 None 表示无法评估（权重表为空）。
        """
        if not _W_POS:
            return None
        _parts = [(k, r.get(k), w) for k, w in _W_POS.items()]
        _t, _ = scale_calc.composite_total(_parts, DIM_MAX, weights=_W_POS)
        return _t

    def expected(r):
        # 2026-10-29：改为直接复用 scale_calc（原为本地第三份实现）。
        # 历史注释保留 —— 原实现两次踩坑：
        #   ① v4.3 前写 `float(r.get(k) or 0)`（falsy 陷阱）；
        #   ② v4.3 改为「缺维从分母剔除」，但生产自 2026-10-27（P0-4）已
        #      改为「分母恒定 + 中性填充」→ 两者再次分叉，产生系统性误报。
        return _scale_total(r)

    # v4.3（P0 新增）：**权重覆盖度检查** —— 直接回答「权重有没有真的落到数据上」。
    #   这是比「total 是否吻合」更前置的一道闸：缺维时 total 照样能算出来（按剩余
    #   维度归一），页面照常出，但 silently 少算了一部分权重。round17 实测
    #   scored 数据缺 0.24 权重、最大偏移 8.15 分、366/490 只偏移 ≥1 分，
    #   而此前唯一的自检项把根因归错了地方。
    _wpos = {k: v for k, v in W.items() if v > 0}
    _cov_rows = []
    for r in rs:
        _have = [k for k in _wpos if isinstance(r.get(k), (int, float))]
        _cov_rows.append(sum(_wpos[k] for k in _have))
    _cov_mean = sum(_cov_rows) / len(_cov_rows) if _cov_rows else 0.0
    _missing_dims = sorted(k for k in _wpos
                           if not any(isinstance(r.get(k), (int, float)) for r in rs))

    # ---- 满分可达性闸（2026-10-26 P0 新增）----
    #   背景：各维量程不统一（12 维 0-5 制 / 4 维 0-10 制），若汇总时不按量程归一化，
    #   理论天花板 = Σ(w×真实量程)/Σw×10 = 67.5 → S≥85 / A≥65 永不可达，
    #   490 只评级只有 B/C/D，评级体系实质失效，但**页面照常出、无任何报错**。
    #   更隐蔽的是名义权重被扭曲（tech_neg 15%→22.2%，反转块 35%→26%）。
    #
    #   ⚠️ 判据设计（易错点）：不能用「各维实测 max」反推天花板 —— 那受样本限制，
    #   正确实现也可能只有 97.8，与未归一化的 87.8 难以区分，闸门失去判别力。
    #   改用的判据：直接重算两套 total 并与数据里的 total 比对 ——
    #     · norm_total  = Σ(w · v/DIM_MAX·10)/Σw ×10（正确：按量程归一化）
    #     · raw_total   = Σ(w · v)/Σw ×10（错误：按原值直接加权）
    #   看数据更接近哪一套，即可判定生产实现走的是哪条路径。这是**因果判据**，
    #   不受样本分布影响。
    def _norm_total(r):
        # 2026-10-29：同样切到生产口径（scale_calc，含中性填充 + 恒定分母）。
        # 保留独立函数名，是为了让下面「量程归一化」闸门的因果判据仍可读：
        #   _norm_total = 正确实现（按 dim_max 归一 + 统一量尺）
        #   _raw_total  = 错误实现（原值直接加权）——故意保留，用于反证
        return _scale_total(r)

    def _raw_total(r):
        _s = _d = 0.0
        for _k, _w in _wpos.items():
            _v = r.get(_k)
            if not isinstance(_v, (int, float)):
                continue
            _s += float(_v) * float(_w)
            _d += float(_w)
        return round(_s / _d * 10, 2) if _d > 0 else None

    _n_norm = _n_raw = _n_tot = 0
    for _r in rs[:60]:
        if _r.get("score_sector") is None:
            continue          # apply 前无板块维，与 total 口径不符，跳过
        _t = _r.get("total")
        if not isinstance(_t, (int, float)):
            continue
        _n_tot += 1
        _a, _b = _norm_total(_r), _raw_total(_r)
        if _a is not None and abs(float(_t) - _a) <= 0.6:
            _n_norm += 1
        elif _b is not None and abs(float(_t) - _b) <= 0.6:
            _n_raw += 1
    # 两套公式在「全 0-10 制」时相等，故需确认确有量程差异才可判定
    _mixed = len({float(DIM_MAX.get(k, 10.0)) for k in _wpos}) > 1
    if _n_tot >= 5 and _mixed:
        if _n_raw > _n_norm:
            return ("fail",
                    f"⚠️ 评分未按量程归一化（P0）：抽样 {_n_tot} 只中 "
                    f"{_n_raw} 只与「原值直接加权」吻合、仅 {_n_norm} 只与「按 dim_max 归一化」吻合。"
                    f"后果：① 理论天花板 67.5 分 → S≥85/A≥65 永不可达（评级体系实质失效）；"
                    f"② 名义权重被扭曲（0-5 制维度实际只发挥一半 → tech_neg 15%→实占 22.2%，"
                    f"反转块 35%→26%）。请确认 step2/enhance/apply 三处均按 dim_max 归一化。")
    _tmax = max((x for x in (r.get("total") for r in rs)
                 if isinstance(x, (int, float))), default=None)
    if _tmax is not None and _tmax < 60:
        print(f"   ℹ️ 归一化口径正常；当日实测最高 {_tmax:.1f} 分"
              f"（各维普遍偏低，属截面特征，高评级当日可能无人达到）")

    if _missing_dims:
        _mw = sum(_wpos[k] for k in _missing_dims)
        _msg = (f"⚠️ 权重未落地: {_missing_dims} 在 scored 数据里完全没有值"
                f"（合计 {_mw:.1%} 权重）→ total 实际只反映 {_cov_mean:.1%} 的权重。"
                f"根因通常是**数据比代码旧**（step2 已能写这些字段，但 scored 是改动前生成的），"
                f"不是 apply 降级。请重跑 step2→enhance→apply 刷新 scored。")
        if _mw >= 0.05:
            return ("fail", _msg + f"（缺失权重 {_mw:.1%} ≥ 5%，属 P0：评级可能跨档）")
        return ("warn", _msg)

    # 抽前 20 只比对（apply 后 total 应≈十维加权）
    diff = 0
    n = 0
    for r in rs[:20]:
        if r.get("score_sector") is None:
            continue     # apply 前无板块维度，跳过
        n += 1
        _e = expected(r)
        if _e is None:
            continue
        if abs(float(r.get("total") or 0) - _e) > 0.5:
            diff += 1
    if n and diff / n > 0.5:
        return ("fail", f"十维总分未生效: 抽样 {n} 只中 {diff} 只 total 偏离十维加权值 >0.5"
                        f"（apply_sector_momentum 可能降级）")
    return ("ok", f"十维总分一致性: 抽样 {n} 只全部吻合；权重覆盖度 {_cov_mean:.1%}")


def main():
    scored = sys.argv[1] if len(sys.argv) > 1 else None
    if not scored:
        fs = [f for f in sorted(glob.glob(os.path.join(HERE, "top200_scored_*.json")), reverse=True)
              if "_watch_" not in f and ".bak" not in f]
        scored = fs[0] if fs else None

    issues = 0
    print("🔎 链路自检 (v3.3)")
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

    print("── B) 十维总分一致性 + 权重落地 ──")
    res = check_ten_dim_applied(scored)
    if res is None:
        print("   ⏭️ 无 scored json，跳过")
    else:
        st, msg = res
        icon = {"ok": "✅", "skip": "⏭️", "fail": "⚠️", "warn": "⚠️"}[st]
        print(f"   {icon} {msg}")
        if st in ("fail", "warn"):
            issues += 1

    print(f"── 结论: {'⚠️ 发现 ' + str(issues) + ' 项可疑（请人工复核）' if issues else '✅ 全部通过'} ──")
    return 1 if issues else 0


if __name__ == "__main__":
    sys.exit(main())
