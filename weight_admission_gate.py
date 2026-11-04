#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
weight_admission_gate.py —— 权重准入裁决器（P0-3 单调性一票否决 + P1-2 过拟合门禁）

═══ 为什么需要这个文件 ═══

round98 双视角评估指出两条结构性缺陷：
  · **P0-3 分层单调性被当"参考指标"而非硬约束** ——
    round95/96 里出现过「IC 与 ICIR 都很好，但 Q1..Q5 不单调」的方案被当作可选项。
    业务上这意味着「分数越高、未来收益不一定越高」，即打分不可用于排序选股。
  · **P1-2 缺少过拟合门禁** ——
    round97 一次枚举 16807 组权重只在 19 点验证集上比较，选择自由度远超信息量。
    没有门禁时，任何"最优权重"都可能是噪声拟合。

本文件把这两条从「审计脚本里的临时逻辑」升级为**独立、可复用、可阻断的裁决器**：
任何权重候选（factor_weights.json 的替代品、优化器输出、人工指定）
都必须通过 `evaluate()` 才能上线。

═══ 五道门禁（任一 FAIL 即不准入）═══

  G1 monotonic          分层严格单调 Q1<Q2<Q3<Q4<Q5          ← P0-3 一票否决
  G2 spread_positive    Q5−Q1 > 0                            ← 与 G1 互补（防"单调但递减"）
  G3 beat_ew            组合 ICIR > 等权同因子池 ICIR         ← 优化必须优于零参数
  G4 beat_single        组合 ICIR > 最强单因子 ICIR           ← 合成必须优于单打
  G5 wf_pass_rate       walk-forward 折通过率 ≥ 阈值          ← P1-2 过拟合门禁

  ⚠️ G5 的通过率阈值默认 0.5（6 折需 ≥3 折通过）。
     round99 实测现行因子池 0 折通过 → 结论是「不具备上线条件」。
     这是**结论而非失败**：门禁的价值就在于把不合格方案挡住。

═══ 域口径（关键）═══

评估域默认用 round91 的逐截面重建池（data/pit_pool.json），
而非固定的 2029 只事后筛选域 —— 后者含池前视偏差
（记忆实测：全池等权 +29.34% vs HS300 −11.46%，差 40.8pp）。
可用 --domain full 切回全池做对照，但**准入判定只认 rebuild 域**。

═══ 用法 ═══

  # 裁决现行线上权重
  python weight_admission_gate.py

  # 裁决候选权重文件（{"score_fund": 0.3, ...} 或 {"weights": {...}}）
  python weight_admission_gate.py --weights cand.json

  # 只跑门禁不跑 walk-forward（快，用于迭代）
  python weight_admission_gate.py --weights cand.json --skip-wf

  # 输出机器可读结果
  python weight_admission_gate.py --weights cand.json --json out.json

退出码：0 = 通过；1 = 未通过；2 = 无法评估（数据不足）
"""
import os
import sys
import json
import math
import argparse
import itertools
import time

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import factor_validation_core as F

# ── round124 P0-1/P1-2：面板与缓存改为「按可用性择优」，不再硬编码 round66 ──
# 血案：原 `PANEL = panel_round66.json`（2026-10-29 建，无 score_growth 列），
#   而 `panel_round123.json` 的 growth 是 238/238 满覆盖。
#   → 闸门基于 round66 得出的「growth 是盲区」结论，
#     在 round123 上**已经不成立**，但报告仍在按旧结论披露。
# 现在按优先级取第一个存在的面板，并把实际用了哪个写进输出。
_PANEL_CANDIDATES = [
    "data/panel_round124_long.json",   # 2020 起长面板（round124 新建）
    "data/panel_round123.json",
    "data/panel_round66.json",
    "data/panel_round34.json",
]


def _pick_panel():
    for rel in _PANEL_CANDIDATES:
        p = os.path.join(HERE, rel)
        if os.path.exists(p):
            return p, rel
    return os.path.join(HERE, "data", "panel_round66.json"), "data/panel_round66.json（fallback）"


PANEL, PANEL_REL = _pick_panel()
NPZ = os.path.join(HERE, "data", "val_arrays_h20.npz")
PIT_POOL = os.path.join(HERE, "data", "pit_pool.json")
WEIGHTS = os.path.join(HERE, "factor_weights.json")


def _resolve_dims(weights_path=None):
    """从权重文件动态取「权重大于 0」的维度作为评估集（round118 P0-1）。

    ═══ 为什么必须动态取 ═══
    原实现 `DIMS = F.DIMS`，而 `factor_validation_core.DIMS` 是**硬编码的 9 维旧列表**。
    实读 factor_weights.json 后发现两处偏差，方向相反、后果都不轻：
        多算的：score_lowvol20  权重 0.0  ← 停用维度却被纳入评估，白占分母
        漏算的：score_growth    权重 0.1  ← **0.10 权重从未过一次闸门**
    漏算的后果：评估集投影成 8 维并按 `w/sum(w)` 重归一化
    （score_size_growth 0.20/0.90=0.2222、score_fund 0.20/0.90=0.2222、
      score_growth 的 0.10 被整个丢掉）→ 「权重已过五门」这句话在旧实现下
    **只覆盖 90% 的名义权重，且那 90% 还是错配的**。

    ═══ 修法与不变量 ═══
    1. 维度集 = 权重文件里 value>0 的键，顺序按权重降序（保证输出稳定可 diff）。
    2. 三条断言把「静默漂移」变成「启动即报错」：
       · 归一化后权重和为 1（防漏维导致分母 ≠ 1）
       · 每个维度的权重 > 0（防引入 0 权重维度稀释）
       · 不得与 factor_weights.json 的键集合出现差集（防未来新增维度又被漏掉）
    3. 若权重文件缺失/不可读 → 回退到 `F.DIMS` 并**打印显式警告**，
       而不是静默用旧列表继续跑（宁可吵，不可错）。

    返回 (dims_list, weight_dict, warnings)
    """
    warnings = []
    path = weights_path or WEIGHTS
    raw = None
    try:
        with open(path, encoding="utf-8") as f:
            blob = json.load(f)
        raw = blob.get("weights") if isinstance(blob, dict) else None
        if raw is None and isinstance(blob, dict):
            # 兼容「裸 dict」形式的候选权重文件
            raw = {k: v for k, v in blob.items()
                   if isinstance(v, (int, float)) and not isinstance(v, bool)}
    except Exception as e:
        warnings.append(f"权重文件不可读（{path}: {e}）→ 回退 F.DIMS，本次准入结论不可用于声明合规")
        return list(F.DIMS), {d: 1.0 / len(F.DIMS) for d in F.DIMS}, warnings

    if not isinstance(raw, dict) or not raw:
        warnings.append(f"权重文件无 weights 字段（{path}）→ 回退 F.DIMS")
        return list(F.DIMS), {d: 1.0 / len(F.DIMS) for d in F.DIMS}, warnings

    pos = {k: float(v) for k, v in raw.items()
           if isinstance(v, (int, float)) and not isinstance(v, bool) and float(v) > 0}
    if not pos:
        warnings.append(f"权重文件全部 ≤0（{path}）→ 回退 F.DIMS")
        return list(F.DIMS), {d: 1.0 / len(F.DIMS) for d in F.DIMS}, warnings

    tot = sum(pos.values())
    dims = [k for k, _ in sorted(pos.items(), key=lambda kv: (-kv[1], kv[0]))]

    # ── 三条不变量 ──
    if abs(tot - 1.0) > 1e-3:
        warnings.append(f"权重和 = {tot:.6f}（≠1），评估时会按 w/sum(w) 重归一化")
    assert all(pos[d] > 0 for d in dims), "动态维度集里出现了非正权重"
    _missing = set(pos) - set(dims)
    assert not _missing, f"动态维度集遗漏了 {_missing} —— 归一化会静默放大其余维度"

    # 与旧硬编码列表的差异必须显式说出来（这是本轮要修的核心事实）
    _old = set(F.DIMS)
    _added = [d for d in dims if d not in _old]
    _dropped = [d for d in F.DIMS if d not in pos]
    if _added:
        warnings.append(f"F.DIMS 未覆盖但权重大于 0 的维度：{_added}（旧实现曾漏算这些权重）")
    if _dropped:
        warnings.append(f"F.DIMS 含但权重大于 0 未包含：{_dropped}（旧实现曾白占分母）")
    return dims, pos, warnings


DIMS, _GATE_W, DIMS_WARNINGS = _resolve_dims()

# ═══════════════════════════════════════════════════════════════════════════
# 可评估集 = 名义集 ∩ **面板真实存在的列**
# ═══════════════════════════════════════════════════════════════════════════
# ⚠️⚠️ round125 P0 修复（本轮实测，前序 bug）：
#   这里原写成 `DIMS_EVAL = [d for d in DIMS if d in F.DIMS]`，
#   即用 `factor_validation_core.DIMS` 当作「面板能提供的列」的白名单。
#   该写法在 round124 **被改坏了**：round124 P0-2 把 `F.DIMS` 从「硬编码 9 维旧表」
#   改成了「从 factor_weights.json 动态解析」，于是 `F.DIMS` 与 `DIMS` 变成
#   **同一个集合** → `d in F.DIMS` 恒真 → DIMS_EVAL 恒等于 DIMS、DIMS_BLIND 恒空。
#
#   后果（本轮实读证据，不是推测）：
#     闸门面板 = data/panel_round124_long.json，真实列只有 12 个，
#     **不含 score_fund 与 score_dominant**（长面板是 2020 起回补，
#     主力净流入/龙头判定无历史快照）。而这两个维度合计权重 **0.30**。
#     DIMS_BLIND=[] + evaluable_ratio=1.0 ⇒ 闸门在宣称「9 维全部通过五门」，
#     实际有 30% 名义权重**一列数据都没有**。
#     这正是 test_round117_p0_p1.py 里 4 条 FAIL 的共同根因
#     （test_eval_dims_are_panel_columns / test_growth_is_declared_blind_not_fake_verified
#      / test_coverage_ratio_is_disclosed_and_less_than_one / 连带 env_lock）。
#
#   正确判据：**实读面板**（_panel_coverage），问「面板里到底有没有这一列」，
#   而不是问「另一个模块的 DIMS 常量里有没有这个名字」。
#   名字对不对（打错字）由 _resolve_dims 的三条不变量守，不归这里管。
#
#   为什么延迟到调用时算：`_panel_coverage` 定义在本段之后（L~200），
#   且读面板要 IO —— 模块导入时读一次 1GB 面板是不可接受的。
DIM_MAX = {d: 10.0 for d in DIMS}

# 行级覆盖率门槛：低于此值视为「列存在但实际不可评估」
# 依据：score_fund 在历史面板前 158 个截面完全为空、行级覆盖仅 3.5%。
#   若仍算「已评估」，就等于用一个 96.5% 为空的维度去给 20% 权重背书。
_GATE_MIN_COVERAGE = 0.50
_SPARSE_CACHE = {"path": None, "mtime": None, "data": None}
_EVAL_CACHE = {"path": None, "mtime": None, "eval": None, "blind": None}


def resolve_eval_dims(panel_path=None):
    """返回 (DIMS_EVAL, DIMS_BLIND, cov, ndate) —— 基于**面板真实列**。

    DIMS_EVAL：面板里真有这一列（不管稀疏）的维度
    DIMS_BLIND：面板里根本没有这一列的维度 → **其权重从未被任何检验背书**
    cov/ndate：见 _panel_coverage
    """
    p = panel_path or PANEL
    try:
        mt = os.path.getmtime(p)
    except OSError:
        mt = None
    if (_EVAL_CACHE["path"] == p and _EVAL_CACHE["mtime"] == mt
            and _EVAL_CACHE["eval"] is not None):
        return _EVAL_CACHE["eval"], _EVAL_CACHE["blind"], *_panel_coverage(p)
    cov, ndate = _panel_coverage(p)
    if not cov:
        # 面板读不出来时**不能假装全可评估** —— 空列表=全部盲区，
        # 让下游 coverage=0、结论直接判不可用，而不是静默满分。
        ev, bl = [], list(DIMS)
    else:
        ev = [d for d in DIMS if d in cov]
        bl = [d for d in DIMS if d not in cov]
    _EVAL_CACHE.update(path=p, mtime=mt, eval=ev, blind=bl)
    return ev, bl, cov, ndate


# 模块级快照在 `_panel_coverage` 定义之后才求值（见文件下方 ANCHOR 处），
# 因为 resolve_eval_dims 依赖它。此处先声明名字，避免 import 顺序炸掉。
DIMS_EVAL, DIMS_BLIND = [], list(DIMS)
_PANEL_COV0, _PANEL_NDATE0 = {}, {}
_DIM_MAX_EVAL = {}

# ⚠️⚠️ round118 P0-1 顺带查到的更严重事实（独立于 DIMS 缺维）：
#   闸门用的 npz 缓存 `data/val_arrays_h20.npz` 覆盖 **2025-11-03 ~ 2026-09-22（219 截面）**，
#   而 `data/panel_round66.json` 覆盖 **2025-11-03 ~ 2026-10-27（238 截面）**。
#   两者差 19 个截面，看似小事，叠加 score_fund 的时间分布后变成大事：
#       score_fund 只在 2026-06-26 之后的 80 个截面有值
#       → 闸门评估区间里「有 fund」的截面 ≈ 61 个，占 219 的 28%
#       → 其余 158 个截面 composite 直接把 fund 从分母剔除
#   所以「fund 0.20 的权重过了闸门」这句话，实际含义是
#   「在 28% 的截面里、fund 参与合成时，整体 IC 表现可接受」。
#   这个限定条件以前从未被写出来。此处只做披露，不擅自刷新 npz
#   （刷新会改变全部历史准入结论，属于需要单独排期的动作）。


def _panel_coverage(panel_path=None):
    """实读面板，返回 (每维行级覆盖率, 每维有数据的截面数)。

    round118 P0-1 新增。为什么必须实读而不是依赖声明：面板是派生文件，
    「F.DIMS 里有这个键」与「面板真的有这一列」是两件不同的事
    —— score_growth 就是只有前者（键在列表里，列一条都没有）。
    不实读就会把盲区当已验证。
    """
    path = panel_path or PANEL
    try:
        mt = os.path.getmtime(path)
    except OSError:
        return {}, {}
    if _SPARSE_CACHE["path"] == path and _SPARSE_CACHE["mtime"] == mt:
        return _SPARSE_CACHE["data"]
    try:
        with open(path, encoding="utf-8") as f:
            panel = json.load(f)
    except Exception:
        return {}, {}
    tot, cnt, ndate = 0, {}, {}
    for d in panel.get("dates") or []:
        rows = (panel.get("rows") or {}).get(d) or {}
        if not rows:
            continue
        present = set()
        for _c, r in rows.items():
            tot += 1
            for k, v in r.items():
                if isinstance(v, (int, float)) and not isinstance(v, bool):
                    cnt[k] = cnt.get(k, 0) + 1
                    present.add(k)
        for k in present:
            ndate[k] = ndate.get(k, 0) + 1
    cov = {k: round(v / tot, 4) if tot else 0.0 for k, v in cnt.items()}
    data = (cov, ndate)
    _SPARSE_CACHE.update(path=path, mtime=mt, data=data)
    return data


# ═══════════════════════════════════════════════════════════════════════════
# ANCHOR：模块级求值（必须在 _panel_coverage 定义之后）
# ═══════════════════════════════════════════════════════════════════════════
# 求值一次并缓存，供 `from weight_admission_gate import DIMS_EVAL` 的老代码使用。
# 若面板缺失：DIMS_EVAL=[] / DIMS_BLIND=全部 → coverage=0、结论判不可用。
# 这是**保守但诚实**的降级：宁可说「什么都没验证」，不可假装验证过。
DIMS_EVAL, DIMS_BLIND, _PANEL_COV0, _PANEL_NDATE0 = resolve_eval_dims()
_DIM_MAX_EVAL = {d: 10.0 for d in DIMS_EVAL}


def _dim_coverage_report(dims=None, wmap=None):
    """逐维覆盖率 + 稀疏告警（round118 P0-1）。

    与 `_gate_coverage()` 的分工：
        _gate_coverage       → 名义权重「有多少进得了闸门」（集合层面）
        _dim_coverage_report → 各维在面板上「实有多少数据」（样本层面）
    两个数字都必须披露，因为它们回答不同问题：
        「growth 完全没进评估」vs「fund 进了评估但 96.5% 的行是空的」
    """
    cov, ndate = _panel_coverage()
    dims = dims or DIMS_EVAL
    w = wmap if wmap is not None else _GATE_W
    out, sparse = {}, {}
    for d in dims:
        c = cov.get(d, 0.0)
        out[d] = {"row_coverage": c, "n_dates_with_data": ndate.get(d, 0)}
        if c < _GATE_MIN_COVERAGE:
            sparse[d] = {"row_coverage": c,
                         "weight": round(w.get(d, 0.0), 4),
                         "n_dates_with_data": ndate.get(d, 0)}
    return {
        "min_coverage_threshold": _GATE_MIN_COVERAGE,
        "by_dim": out,
        "sparse_dims": sparse,
        "npz_vs_panel_gap": _npz_panel_gap(),
        "note": ("；".join(
            f"{d} 行级覆盖仅 {v['row_coverage'] * 100:.1f}%"
            f"（{v['n_dates_with_data']} 个截面有值）却占权重 {v['weight']:.2f}"
            for d, v in sparse.items()) or "各维行级覆盖率均达标"),
    }


def _npz_panel_gap():
    """npz 缓存（闸门真正读的数据）与面板文件的时间覆盖差。

    round118 P0-1 新增。两者不齐会让「闸门评估区间」短于「面板区间」，
    而覆盖率是按**面板**算的 —— 若不多披露这一层，读者会以为闸门看到了
    面板的全部 238 个截面，实际只有 219 个。
    """
    try:
        z = np.load(NPZ, allow_pickle=False)
        zd = [str(x) for x in z["dates"]]
        with open(PANEL, encoding="utf-8") as f:
            pd = [str(x) for x in (json.load(f).get("dates") or [])]
    except Exception as e:
        return {"available": False, "error": str(e)}
    zn, pn = set(zd), set(pd)
    return {
        "available": True,
        "npz": {"first": zd[0] if zd else None, "last": zd[-1] if zd else None,
                "n": len(zd)},
        "panel": {"first": pd[0] if pd else None, "last": pd[-1] if pd else None,
                  "n": len(pd)},
        "n_panel_not_in_npz": len(pn - zn),
        "n_npz_not_in_panel": len(zn - pn),
        "note": (f"闸门实际只看到 {len(zd)} 个截面（{zd[0]}~{zd[-1]}），"
                 f"面板有 {len(pd)} 个（至 {pd[-1]}）；"
                 f"差 {len(pn - zn)} 个截面未进入闸门评估 —— "
                 f"覆盖率按面板算，闸门区间更短，二者不可混用"
                 if pn - zn else "npz 与面板截面完全一致"),
    }


def _gate_coverage(wmap=None):
    """统计「本次闸门实际覆盖了名义权重的百分之几」。

    round118 P0-1 之前，这个数字从未被算过、也从未被披露。没有它，
    「权重已过五门」会被读成「全部权重已验证」，而实际上：
        score_growth 0.10 → 面板无此列，永远无法评估
        score_lowvol20 0.0 → 已停用，不该出现在评估集
    """
    w = wmap if wmap is not None else _GATE_W
    tot = sum(w.values()) or 1.0
    cov = sum(v for k, v in w.items() if k in DIMS_EVAL)
    return {
        "nominal_weight_total": round(tot, 6),
        "evaluable_weight": round(cov, 6),
        "evaluable_ratio": round(cov / tot, 4) if tot else None,
        "evaluable_dims": list(DIMS_EVAL),
        "blind_dims": {d: round(w.get(d, 0.0), 4) for d in DIMS_BLIND},
        "note": (
            f"闸门实际覆盖 {cov / tot * 100:.1f}% 的名义权重；"
            + (f"以下维度不在面板中、**从未经过任何闸门验证**："
               + "、".join(f"{d}={w.get(d, 0):.2f}" for d in DIMS_BLIND)
               if DIMS_BLIND else "全部名义维度均可评估")
        ),
    }

# 现行 v4.15 的结构性分组（round94 实测符号翻转）
FLIP_DIMS = ["score_ep", "score_lowvol20", "score_rsi_rev", "score_tech_neg"]
STABLE_DIMS = ["score_fund", "score_quality", "score_size_growth"]

# G5 默认阈值
DEFAULT_WF_PASS_RATE = 0.5


# ═══════════════ 数值/序列化工具（numpy 标量陷阱）═══════════════

def _fnum(v):
    """数值判定，涵盖 numpy 标量。

    ⚠️ 2026-10-29 血案：`isinstance(np.float32(x), (int, float))` 是 **False**，
       导致 fwd_hist 读出的 fund 值全被判为缺失 → 「同域 IC 全空但无报错」。
       必须用 numbers.Real 才能涵盖 numpy 标量。
    """
    import numbers
    if isinstance(v, bool) or v is None:
        return None
    if isinstance(v, numbers.Real):
        try:
            f = float(v)
        except (TypeError, ValueError):
            return None
        return None if (math.isnan(f) or math.isinf(f)) else f
    return None


def _jsonable(o):
    if isinstance(o, dict):
        return {k: _jsonable(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_jsonable(v) for v in o]
    if isinstance(o, np.bool_):
        return bool(o)
    if isinstance(o, np.integer):
        return int(o)
    if isinstance(o, np.floating):
        return float(o)
    return o


# ═══════════════ 权重加载与规范化 ═══════════════

def load_weights(path=None, dims=None):
    """读取权重。支持 {"weights": {...}} 或扁平 {...} 两种格式。

    只保留 `dims` 内的维度，其余（已归零维）丢弃；随后归一到 Σ=1。

    ⚠️ round118 P0-1：`dims` 默认从**该权重文件自身**动态解析，而不是用模块级
       的 `DIMS`。原因：裁决候选权重（`--weights cand.json`）时，若候选引入了
       新维度（如某个新因子），用旧 `DIMS` 过滤会把它**静默丢掉**，然后归一化
       把剩余维放大 —— 你会得到一个「通过了闸门」的结论，但它评估的根本不是
       候选文件里的那一组权重。这是比 P0-1 本身更隐蔽的同类错误。
    """
    if path is None:
        path = WEIGHTS
    if dims is None:
        # 候选权重裁决：取「候选自己的正权重」∩「面板可提供的维度」。
        #   取交集而不是直接用候选全集的理由：面板 K 轴只到 F.DIMS，
        #   候选里的新维度在这里没有数据，无法评估 —— 但必须报出来，
        #   不能假装评估过（这是本函数第二段警告的职责）。
        dims, _w, _warn = _resolve_dims(path)
        for msg in _warn:
            print(f"⚠️ 权重维度解析：{msg}")
        dims = set(dims) & set(F.DIMS)
    else:
        dims = set(dims)
    with open(path, encoding="utf-8") as fh:
        d = json.load(fh)
    w = d.get("weights") if isinstance(d, dict) and "weights" in d else d
    if not isinstance(w, dict):
        raise ValueError(f"权重文件格式不支持: {path}")
    out = {}
    _dropped = {}
    for k, v in w.items():
        f = _fnum(v)
        if f is None or f <= 0:
            continue
        if k not in dims:
            _dropped[k] = f
            continue
        out[k] = f
    if _dropped:
        # 不再静默：候选权重里有本次评估集之外的维度，必须让人看见
        print(f"⚠️ {path}: 以下维度有正权重但不在评估集内，已被排除 —— {_dropped}；"
              f"评估结论**不代表**该候选权重的完整形态")
    tot = sum(out.values())
    if tot <= 0:
        raise ValueError("权重全为零")
    return {k: v / tot for k, v in out.items()}, d


def load_v415():
    w, _ = load_weights(WEIGHTS)
    return w


# ═══════════════ 域掩码 ═══════════════

def make_pool_mask(dates, codes, min_n=50):
    """把 pit_pool.json 的逐截面成分转成 (nD, nC) 掩码。"""
    if not os.path.exists(PIT_POOL):
        return None, []
    pit = json.load(open(PIT_POOL, encoding="utf-8"))
    pools = pit.get("pools") or {}
    code_idx = {c: j for j, c in enumerate(codes)}
    nD, nC = len(dates), len(codes)
    mask = np.zeros((nD, nC), dtype=bool)
    used = []
    for di, dt in enumerate(dates):
        members = pools.get(dt)
        if not members:
            continue
        js = [code_idx[c] for c in members if c in code_idx]
        if len(js) < min_n:
            continue
        mask[di, js] = True
        used.append(di)
    return mask, used


# ═══════════════ 评估核心 ═══════════════

def ic_series(X, M, Y, RY, wmap, pool_mask=None, idxs=None):
    """在指定域与截面集合上的组合 IC 序列。"""
    if idxs is None:
        idxs = list(range(X.shape[0]))
    idxs = list(idxs)
    if not idxs:
        return []
    Xs, Ms, Ys, RYs = X[idxs], M[idxs], Y[idxs], RY[idxs]
    if pool_mask is not None:
        pm = pool_mask[idxs]
        Ms = Ms & pm[:, :, None]          # ⚠️ M 是 (nD,nC,nK) 三维，须广播
        Xs = np.where(Ms, Xs, np.nan)
    S = F.composite(Xs, Ms, Ys, wmap, DIM_MAX)
    return F.row_pearson_ranks(F.rank_rows(S), RYs, min_n=30 if pool_mask is not None else 100)


def layer_matrix(X, M, Y, wmap, pool_mask=None, idxs=None, q=5):
    """逐截面各档收益矩阵 (nDate, q)。

    与直接取均值的区别：本函数保留**每一期**的档位收益，
    从而可以算相邻档差值的标准误 —— 单调性是否显著必须靠它判断，
    只看均值会误把噪声当结构（round101 实测：所有相邻档差 |t|≤1.33）。
    """
    if idxs is None:
        idxs = list(range(X.shape[0]))
    idxs = list(idxs)
    Xs, Ms, Ys = X[idxs], M[idxs], Y[idxs]
    pm = None
    if pool_mask is not None:
        pm = pool_mask[idxs]
        Ms = Ms & pm[:, :, None]
        Xs = np.where(Ms, Xs, np.nan)
    S = F.composite(Xs, Ms, Ys, wmap, DIM_MAX)
    out = np.full((len(idxs), q), np.nan)
    for i in range(len(idxs)):
        s, y = S[i], Ys[i]
        ok = np.isfinite(s) & np.isfinite(y)
        if pm is not None:
            ok &= pm[i]
        if ok.sum() < q * 10:
            continue
        ss, yy = s[ok], y[ok]
        yy_s = yy[np.argsort(ss)]
        n = len(yy_s)
        for b in range(q):
            lo, hi = int(n * b / q), int(n * (b + 1) / q)
            if hi > lo:
                out[i, b] = float(np.mean(yy_s[lo:hi]))
    return out


def layer_stats(X, M, Y, wmap, pool_mask=None, idxs=None, q=5):
    """分层统计：各档均值/标准误、相邻档差值及其 t、Q5−Q1 及其 t。

    返回 dict:
      means     各档均值
      se        各档标准误
      diffs     相邻档差值（长度 q-1，diff[i] = means[i+1] - means[i]）
      diff_t    相邻档差值 t 值
      spread    Q5 − Q1
      spread_t  Q5 − Q1 的 t 值（判断尾部有效性是否显著）
      n_eff     有效截面数
    """
    L = layer_matrix(X, M, Y, wmap, pool_mask, idxs, q)
    n = np.sum(~np.isnan(L), axis=0)
    with np.errstate(invalid="ignore"):
        mu = np.nanmean(L, axis=0)
        sd = np.nanstd(L, axis=0, ddof=1)
    se = sd / np.sqrt(np.maximum(n, 1))
    diffs, diff_t = [], []
    for b in range(q - 1):
        d = mu[b + 1] - mu[b]
        dse = math.sqrt(se[b + 1] ** 2 + se[b] ** 2)
        diffs.append(float(d))
        diff_t.append(float(d / dse) if dse > 1e-12 else None)
    spread = float(mu[-1] - mu[0])
    sse = math.sqrt(se[-1] ** 2 + se[0] ** 2)
    spread_t = float(spread / sse) if sse > 1e-12 else None
    return {"means": [float(v) for v in mu], "se": [float(v) for v in se],
            "diffs": diffs, "diff_t": diff_t,
            "spread": spread, "spread_t": spread_t,
            "n_eff": int(np.max(~np.isnan(L), axis=0).max()) if len(L) else 0,
            "matrix": L}


def layer_means(X, M, Y, wmap, pool_mask=None, idxs=None, q=5):
    """兼容旧接口：返回 (各档均值, Q5−Q1)。"""
    st = layer_stats(X, M, Y, wmap, pool_mask, idxs, q)
    if any(m is None for m in st["means"]):
        return st["means"], None
    return st["means"], st["spread"]


def is_monotonic(means):
    """分层**严格**单调递增（Q1<Q2<Q3<Q4<Q5）—— 理想判据。

    ⚠️ 本判据在当前样本量下几乎不可达：round101 标定显示
       相邻档差值的标准误约 0.010~0.015，而档间真实差异只有 0.003~0.018，
       全部 |t| ≤ 1.33。**6 折、3 个方案、12 个相邻差值，无一显著。**
       ⇒ 严格单调在本数据上等同于「要求噪声按指定顺序排列」，
         不能单独当一票否决，必须配合噪声容忍判据（见 is_monotonic_robust）。
    """
    if not means or any(m is None for m in means):
        return False
    return all(means[i] < means[i + 1] for i in range(len(means) - 1))


def is_monotonic_robust(stats, t_tol=1.5):
    """**噪声容忍**单调：只把「统计显著的下降」当真实断裂。

    规则：相邻档差值 diff[i] < 0 且 |t_i| ≥ t_tol 才算真断点。
          diff[i]≥0 或 |t_i|<t_tol 的下降视为噪声平台，不影响判定。
    并要求 Q5 > Q1（尾部方向正确）。

    依据：round101 标定 —— 12 个相邻差值的标准误约 0.012，
    真实档差只有 0.003~0.018，|t|≤1.33。用严格不等号会把
    纯噪声判成结构断裂，门禁失去区分度（3 个方案全部 FAIL）。
    """
    if not stats or not stats.get("means"):
        return False
    if any(m is None for m in stats["means"]):
        return False
    if stats["spread"] <= 0:
        return False
    for d, t in zip(stats["diffs"], stats["diff_t"]):
        if d < 0 and t is not None and abs(t) >= t_tol:
            return False
    return True


def diagnose_break(means, diff_t=None, t_tol=1.5):
    """定位单调性断点，返回 (断点描述, 断裂档差值, 是否显著)。

    为什么需要：round99 实测 6 折**全部**是 Q4→Q5 断裂，
    说明"最高分档系统性弱于 Q4"是惩罚维度过度集中的指纹，
    而不是随机噪声。只报 True/False 无法定位病因。
    但 round101 追加标定：这些断点**统计上都不显著**，
    故必须同时给出 t，否则会把噪声误当成结构缺陷。
    """
    if not means or any(m is None for m in means):
        return None, None, None
    for i in range(len(means) - 1):
        if means[i] >= means[i + 1]:
            t = diff_t[i] if diff_t and i < len(diff_t) else None
            sig = bool(t is not None and abs(t) >= t_tol)
            return f"Q{i+1}→Q{i+2}", float(means[i + 1] - means[i]), sig
    return None, None, None


# ═══════════════ P1-2 过拟合门禁：walk-forward ═══════════════

def walk_forward(X, M, Y, RY, dates, pool, pool_mask, used_idx,
                 folds=6, train=60, val=20, step=0.25, w_cap=1.0,
                 pass_rate_thr=DEFAULT_WF_PASS_RATE, verbose=True):
    """滚动前推验证 + 五道门禁。

    pool: 参与枚举的因子池（默认剔除 fund —— 其数据源 fwd_hist 不可回溯）
    used_idx: 参与评估的截面索引（重建池域下为 pit_pool 覆盖到的截面）

    ⚠️ 关键设计：**选择自由度要报出来**。
       一次枚举 N 组权重、验证集只有 V 个点，则每次"最优"的信息量
       约等于 V/N。N≫V 时最优解几乎必然是噪声。本函数把
       n_enumerated / n_val 一并输出，供人判断结论可信度。
    """
    if verbose:
        print("\n" + "=" * 78)
        print("G5  walk-forward 过拟合门禁")
        print("=" * 78)

    idxs_all = list(used_idx)
    nD = len(idxs_all)
    win = train + val
    if nD < win:
        return {"error": f"截面不足（{nD} < train+val={win}）", "pass": False}

    stride = val
    starts = list(range(0, nD - win + 1, stride))
    if len(starts) > folds:
        starts = starts[-folds:]

    grid = [round(i * step, 4) for i in range(int(round(1 / step)) + 1)]
    combos = []
    for c in itertools.product(grid, repeat=len(pool)):
        if abs(sum(c) - 1.0) > 1e-9:
            continue
        if max(c) > w_cap + 1e-9:
            continue
        combos.append(c)

    if verbose:
        print(f"  截面 {nD}  train={train} val={val} stride={stride} "
              f"→ {len(starts)} 折")
        print(f"  候选权重 {len(combos)} 组 × 验证集 {val} 点 "
              f"⇒ 选择自由度 {len(combos)/max(val,1):.0f}:1 "
              f"{'⚠️ 严重过拟合风险' if len(combos) > 10 * val else ''}")

    base_ew = tuple([round(1.0 / len(pool), 6)] * len(pool))

    def to_wmap(combo):
        return {d: w for d, w in zip(pool, combo) if w > 0}

    recs = []
    for fi, s in enumerate(starts, 1):
        tr = idxs_all[s:s + train]
        va = idxs_all[s + train:s + win]
        if len(tr) < 20 or len(va) < 5:
            continue

        # 训练段选 ICIR 最优（不看验证段）
        best, best_icir = None, None
        for combo in combos:
            st = F.stat(ic_series(X, M, Y, RY, to_wmap(combo), pool_mask, tr))
            if not st or st.get("n", 0) < 15 or st.get("icir") is None:
                continue
            if best_icir is None or st["icir"] > best_icir:
                best, best_icir = combo, st["icir"]
        if best is None:
            continue

        st_opt = F.stat(ic_series(X, M, Y, RY, to_wmap(best), pool_mask, va))
        st_ew = F.stat(ic_series(X, M, Y, RY, to_wmap(base_ew), pool_mask, va))
        lst = layer_stats(X, M, Y, to_wmap(best), pool_mask, va)
        means, spread = lst["means"], lst["spread"]

        # 最强单因子（训练段选，验证段评）
        bs, bs_icir = None, None
        for d in pool:
            st = F.stat(ic_series(X, M, Y, RY, {d: 1.0}, pool_mask, tr))
            if st and st.get("icir") is not None and (bs_icir is None or st["icir"] > bs_icir):
                bs, bs_icir = d, st["icir"]
        st_single = (F.stat(ic_series(X, M, Y, RY, {bs: 1.0}, pool_mask, va))
                     if bs else None)

        gates = {
            "g1_monotonic_strict": bool(is_monotonic(means)),
            "g1_monotonic_robust": bool(is_monotonic_robust(lst)),
            "g2_spread_positive": bool(spread is not None and spread > 0),
            "g3_beat_ew": bool(st_opt.get("icir") is not None and st_ew.get("icir") is not None
                               and st_opt["icir"] > st_ew["icir"]),
            "g4_beat_single": bool(st_opt.get("icir") is not None
                                   and st_single and st_single.get("icir") is not None
                                   and st_opt["icir"] > st_single["icir"]),
        }
        # 符号一致性（训练/验证同号）：不作为独立门禁，但记录 ——
        # 符号翻转说明该组合在不同时段方向相反，是最强的"不可复现"信号。
        sign_ok = bool(best_icir is not None and st_opt.get("icir") is not None
                       and np.sign(best_icir) == np.sign(st_opt["icir"]))
        # 折通过以**噪声容忍单调**为准（严格单调另记，供诊断）
        gates["pass"] = bool(all(gates[k] for k in
                                 ("g1_monotonic_robust", "g2_spread_positive",
                                  "g3_beat_ew", "g4_beat_single")))

        brk, gap, brk_sig = diagnose_break(means, lst["diff_t"])
        recs.append({
            "fold": fi,
            "train_range": [dates[tr[0]], dates[tr[-1]]],
            "val_range": [dates[va[0]], dates[va[-1]]],
            "best_combo": {d: w for d, w in zip(pool, best) if w > 0},
            "train_icir": best_icir,
            "val_icir": st_opt.get("icir"),
            "val_ic": st_opt.get("ic"),
            "val_t": st_opt.get("t"),
            "val_n": st_opt.get("n"),
            "ew_icir": st_ew.get("icir"),
            "single_best": bs,
            "single_icir": st_single.get("icir") if st_single else None,
            "layer_means": means,
            "layer_se": lst["se"],
            "layer_diff_t": lst["diff_t"],
            "spread": spread,
            "spread_t": lst["spread_t"],
            "monotonic_break_at": brk,
            "break_gap": gap,
            "break_significant": brk_sig,
            "sign_consistent": sign_ok,
            "gates": gates,
        })
        if verbose:
            print(f"  折{fi} train {dates[tr[0]]}~{dates[tr[-1]]} "
                  f"val {dates[va[0]]}~{dates[va[-1]]}")
            print(f"      ICIR {best_icir:>7.4f}(训) → "
                  f"{(st_opt.get('icir') if st_opt.get('icir') is not None else float('nan')):>7.4f}(验)  "
                  f"EW={st_ew.get('icir') if st_ew.get('icir') is not None else float('nan'):>7.4f}  "
                  f"spread={(spread if spread is not None else float('nan')):>8.5f}"
                  f"(t={lst['spread_t'] if lst['spread_t'] is not None else float('nan'):+.2f})  "
                  f"{'PASS' if gates['pass'] else 'FAIL'}"
                  f"{'  断@' + brk + ('(显著)' if brk_sig else '(噪声)') if brk else ''}")

    n = len(recs)
    n_pass = sum(1 for r in recs if r["gates"]["pass"])
    rate = n_pass / max(n, 1)
    vics = [r["val_icir"] for r in recs if r["val_icir"] is not None]
    tics = [r["train_icir"] for r in recs if r["train_icir"] is not None]

    # 断点统计（病因定位）
    brk_cnt, brk_sig_cnt = {}, {}
    for r in recs:
        if r["monotonic_break_at"]:
            brk_cnt[r["monotonic_break_at"]] = brk_cnt.get(r["monotonic_break_at"], 0) + 1
            if r.get("break_significant"):
                brk_sig_cnt[r["monotonic_break_at"]] = \
                    brk_sig_cnt.get(r["monotonic_break_at"], 0) + 1

    summary = {
        "n_folds": n,
        "n_pass": n_pass,
        "pass_rate": round(rate, 4),
        "pass_rate_threshold": pass_rate_thr,
        "g5_pass": bool(rate >= pass_rate_thr),
        "n_enumerated": len(combos),
        "n_val_points": val,
        "selection_dof_ratio": round(len(combos) / max(val, 1), 1),
        "overfit_warning": bool(len(combos) > 10 * val),
        "train_icir_mean": round(float(np.mean(tics)), 4) if tics else None,
        "val_icir_mean": round(float(np.mean(vics)), 4) if vics else None,
        "val_icir_std": round(float(np.std(vics)), 4) if vics else None,
        "train_to_val_decay": (round(float(np.mean(tics)) - float(np.mean(vics)), 4)
                               if tics and vics else None),
        "n_g1_strict": sum(1 for r in recs if r["gates"]["g1_monotonic_strict"]),
        "n_g1_robust": sum(1 for r in recs if r["gates"]["g1_monotonic_robust"]),
        "n_g2_spread": sum(1 for r in recs if r["gates"]["g2_spread_positive"]),
        "n_g3_beat_ew": sum(1 for r in recs if r["gates"]["g3_beat_ew"]),
        "n_g4_beat_single": sum(1 for r in recs if r["gates"]["g4_beat_single"]),
        "monotonic_break_histogram": brk_cnt,
        "monotonic_break_significant_histogram": brk_sig_cnt,
        "n_break_significant": sum(brk_sig_cnt.values()),
    }

    if verbose:
        print("\n  " + "-" * 74)
        print(f"  折数 {n}  全门禁通过 {n_pass}（{rate*100:.1f}%，"
              f"阈值 {pass_rate_thr*100:.0f}%）")
        print(f"  G1 严格单调 {summary['n_g1_strict']}/{n}   "
              f"G1 噪声容忍 {summary['n_g1_robust']}/{n}   "
              f"G2 spread>0 {summary['n_g2_spread']}/{n}")
        print(f"  G3 胜等权 {summary['n_g3_beat_ew']}/{n}   "
              f"G4 胜单因子 {summary['n_g4_beat_single']}/{n}")
        if brk_cnt:
            print(f"  单调性断点分布: " +
                  ", ".join(f"{k}×{v}" for k, v in sorted(brk_cnt.items())) +
                  f"  （其中统计显著 {summary['n_break_significant']} 个）")
            if summary["n_break_significant"] == 0:
                print("      ⇒ 断点全部落在噪声区间，不构成结构性缺陷")
        if summary["overfit_warning"]:
            print(f"  ⚠️ 选择自由度 {summary['selection_dof_ratio']}:1 "
                  f"（{len(combos)} 组 / {val} 验证点）→ 最优解可信度低")

    return {"summary": summary, "folds": recs}


# ═══════════════ 主裁决 ═══════════════

def evaluate(wmap, X, M, Y, RY, dates, pool_mask, used_idx,
             pool=None, skip_wf=False, wf_kwargs=None, verbose=True):
    """对给定权重做完整裁决，返回 dict（含 verdict: PASS/FAIL/UNKNOWN）。"""
    pool = pool or STABLE_DIMS + ["score_ep", "score_lowvol20",
                                  "score_rsi_rev", "score_tech_neg"]
    wf_kwargs = wf_kwargs or {}

    if verbose:
        print("\n" + "=" * 78)
        print("待裁决权重")
        print("=" * 78)
        for k, v in sorted(wmap.items(), key=lambda kv: -kv[1]):
            grp = ("翻转" if k in FLIP_DIMS else
                   "稳定" if k in STABLE_DIMS else "其他")
            print(f"  {k:<20s} {v:>8.6f}   [{grp}]")
        tot = sum(wmap.values())
        print(f"  {'Σ':<20s} {tot:>8.6f}")
        flip_w = sum(wmap.get(d, 0.0) for d in FLIP_DIMS)
        print(f"  翻转维合计 {flip_w:.4f}  稳定维合计 "
              f"{sum(wmap.get(d, 0.0) for d in STABLE_DIMS):.4f}")

    # ── 域内全样本表现 ──
    ics = ic_series(X, M, Y, RY, wmap, pool_mask, used_idx)
    st = F.stat(ics)
    lst = layer_stats(X, M, Y, wmap, pool_mask, used_idx)
    means, spread = lst["means"], lst["spread"]
    mono_strict = is_monotonic(means)
    mono_robust = is_monotonic_robust(lst)
    brk, gap, brk_sig = diagnose_break(means, lst["diff_t"])

    if verbose:
        print(f"\n  评估域: {'重建池' if pool_mask is not None else '全池'}  "
              f"截面 {st['n']}  IC={st['ic']:.4f}  ICIR={st['icir']:.4f}  "
              f"t={st['t']:.2f}")
        if means:
            print("  分层: " + "  ".join(
                f"Q{i+1}={m:+.4f}" for i, m in enumerate(means)))
            print("  相邻档差: " + "  ".join(
                f"Q{i+1}→Q{i+2}={d:+.5f}(t={t:+.2f})"
                for i, (d, t) in enumerate(zip(lst["diffs"], lst["diff_t"]))))
            print(f"  spread={spread:+.5f}(t={lst['spread_t']:+.2f})  "
                  f"严格单调={'Y' if mono_strict else 'N'}  "
                  f"噪声容忍单调={'Y' if mono_robust else 'N'}"
                  f"{'  断点 ' + brk + f'({gap:+.5f}, ' + ('显著' if brk_sig else '噪声') + ')' if brk else ''}")

    # ── G1/G2（一票否决，先判，省 walk-forward 开销）──
    # G1 用**噪声容忍**判据（见 is_monotonic_robust 的标定说明）；
    # 严格单调另记一个诊断字段，不作为门禁，否则会否决一切方案。
    g1 = bool(mono_robust)
    g2 = bool(spread is not None and spread > 0)
    gates = {"g1_monotonic_robust": g1, "g2_spread_positive": g2}

    wf = None
    if not skip_wf and g1 and g2:
        wf = walk_forward(X, M, Y, RY, dates, pool, pool_mask, used_idx,
                          verbose=verbose, **wf_kwargs)
        gates["g5_wf_pass_rate"] = bool(wf.get("summary", {}).get("g5_pass"))
        # G3/G4 用 walk-forward 的验证段统计（避免全样本自评的乐观偏差）
        s = wf.get("summary", {})
        n = s.get("n_folds", 0)
        gates["g3_beat_ew"] = bool(n > 0 and s.get("n_g3_beat_ew", 0) / n >= 0.5)
        gates["g4_beat_single"] = bool(n > 0 and s.get("n_g4_beat_single", 0) / n >= 0.5)
    elif not skip_wf:
        gates["g5_wf_pass_rate"] = False
        gates["g3_beat_ew"] = False
        gates["g4_beat_single"] = False
        if verbose:
            print("\n  ⚠️ G1/G2 未通过 → 跳过 walk-forward（一票否决已成立）")

    # ── 裁决 ──
    if skip_wf:
        verdict = "PARTIAL" if (g1 and g2) else "FAIL"
        reason = ("仅跑了 G1/G2，walk-forward 未评估——"
                  "**不可据此上线**，需补 --skip-wf 之外的完整裁决"
                  if g1 and g2 else "G1/G2 未通过")
    elif all(gates.values()):
        verdict = "PASS"
        reason = "五道门禁全部通过"
    else:
        verdict = "FAIL"
        failed = [k for k, v in gates.items() if not v]
        reason = "未通过门禁: " + ", ".join(failed)

    if verbose:
        print("\n  " + "=" * 78)
        print(f"  裁决: {verdict}  —— {reason}")
        for k in ("g1_monotonic_robust", "g2_spread_positive", "g3_beat_ew",
                  "g4_beat_single", "g5_wf_pass_rate"):
            if k in gates:
                print(f"    {k:<22s} {'Y' if gates[k] else 'N'}")
        print(f"    {'(诊断) 严格单调':<22s} {'Y' if mono_strict else 'N'}"
              f"   ← 仅记录，不作门禁（当前样本量下统计上不可达）")
        print("  " + "=" * 78)

    return {
        "verdict": verdict,
        "reason": reason,
        "gates": gates,
        "weights": {k: round(v, 6) for k, v in wmap.items()},
        "flip_total": round(sum(wmap.get(d, 0.0) for d in FLIP_DIMS), 6),
        "stable_total": round(sum(wmap.get(d, 0.0) for d in STABLE_DIMS), 6),
        "domain": "rebuild_pool" if pool_mask is not None else "full_2029",
        "n_dates": st.get("n"),
        "all": st,
        "layer_means": means,
        "layer_se": lst["se"],
        "layer_diffs": lst["diffs"],
        "layer_diff_t": lst["diff_t"],
        "spread": spread,
        "spread_t": lst["spread_t"],
        "monotonic_strict": mono_strict,
        "monotonic_robust": mono_robust,
        "monotonic_break_at": brk,
        "break_gap": gap,
        "break_significant": brk_sig,
        "walkforward": wf,
        "caveat": (
            "⚠️ 本条裁决的边界："
            "(1) 域为 round91 逐截面重建池，股本用跨期常数（无拆股调整）→ 历史市值有近似误差；"
            "(2) regime 划分为事后（用未来收益），仅作分层诊断，记忆 R2 已证不可事前利用；"
            "(3) G5 的 walk-forward 是唯一防过拟合手段，但它本身也在同一段历史上滚动，"
            "不能替代真正的未来样本外；"
            "(4) G1 用噪声容忍判据：round101 标定 12 个相邻档差 |t|≤1.33，"
            "严格单调等于要求噪声按指定顺序排列，故只把统计显著的下降当断裂；"
            "(5) 即便 PASS，也只代表『在这 219 个截面上站得住』，"
            "不等于『未来有效』—— 禁止表述为『经验证的 alpha』。"
        ),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--weights", default=None, help="候选权重 json（默认裁决线上 factor_weights.json）")
    ap.add_argument("--domain", default="rebuild", choices=["rebuild", "full"],
                    help="评估域：rebuild=重建池（准入判定只认此域）/ full=全池对照")
    ap.add_argument("--skip-wf", action="store_true", help="跳过 walk-forward（快，但结论不可上线）")
    ap.add_argument("--folds", type=int, default=6)
    ap.add_argument("--train", type=int, default=60)
    ap.add_argument("--val", type=int, default=20)
    ap.add_argument("--step", type=float, default=0.25)
    ap.add_argument("--pass-rate", type=float, default=DEFAULT_WF_PASS_RATE,
                    help="G5 折通过率阈值（默认 0.5）")
    ap.add_argument("--pool", default="score_quality,score_ep,score_lowvol20,score_size_growth",
                    help="walk-forward 枚举的因子池（默认剔除 fund：数据不可回溯）")
    ap.add_argument("--json", default=None, help="输出 json 路径")
    args = ap.parse_args()

    t0 = time.time()
    dates, X, M, Y, codes = F.load_panel_arrays(PANEL, NPZ)
    dates = [str(d) for d in dates]
    codes = [str(c) for c in codes]
    RY = F.rank_rows(Y)
    print(f"面板 {X.shape}  {dates[0]}~{dates[-1]}")

    pool_mask, used_idx = None, list(range(len(dates)))
    if args.domain == "rebuild":
        pool_mask, used_idx = make_pool_mask(dates, codes)
        if not used_idx:
            print("⚠️ pit_pool 无可比截面 → 回退全池")
            pool_mask, used_idx = None, list(range(len(dates)))
        else:
            sizes = [int(pool_mask[i].sum()) for i in used_idx]
            print(f"重建池: {len(used_idx)} 截面  "
                  f"规模 median={int(np.median(sizes))} "
                  f"min={min(sizes)} max={max(sizes)}")

    wmap, raw = load_weights(args.weights)
    print(f"权重来源: {args.weights or WEIGHTS}")

    pool = [x.strip() for x in args.pool.split(",") if x.strip()]
    res = evaluate(wmap, X, M, Y, RY, dates, pool_mask, used_idx,
                   pool=pool, skip_wf=args.skip_wf,
                   wf_kwargs={"folds": args.folds, "train": args.train,
                              "val": args.val, "step": args.step,
                              "pass_rate_thr": args.pass_rate})

    res["meta"] = {
        "weights_source": args.weights or WEIGHTS,
        "weights_version": (raw.get("version") if isinstance(raw, dict) else None),
        "domain": args.domain,
        "npz": os.path.relpath(NPZ, HERE),
        "skip_wf": args.skip_wf,
        "wf_config": {"folds": args.folds, "train": args.train,
                      "val": args.val, "step": args.step, "pool": pool,
                      "pass_rate_thr": args.pass_rate},
        "built_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "elapsed_s": round(time.time() - t0, 1),
        # ── round118 P0-1：两层覆盖率披露 ──
        # 缺它则「过五门」会被读成「全部权重已验证」——实测并非如此
        "dim_coverage": _gate_coverage(_GATE_W),          # 集合层面：多少权重进得了闸门
        "row_coverage": _dim_coverage_report(),            # 样本层面：各维在面板上多稀疏
        "dims_nominal": list(DIMS),
        "dims_evaluated": list(DIMS_EVAL),
        "dims_blind": list(DIMS_BLIND),
        "dims_warnings": list(DIMS_WARNINGS),
    }
    print("\n" + "─" * 78)
    print("闸门维度覆盖：" + res["meta"]["dim_coverage"]["note"])
    print("行级覆盖率：" + res["meta"]["row_coverage"]["note"])

    out_path = args.json or os.path.join(HERE, "weight_gate_result.json")
    json.dump(_jsonable(res), open(out_path, "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    print(f"\n→ {out_path}  耗时 {res['meta']['elapsed_s']}s")

    return 0 if res["verdict"] == "PASS" else (1 if res["verdict"] == "FAIL" else 2)


if __name__ == "__main__":
    sys.exit(main())
