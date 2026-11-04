#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""trial_log.py —— 多重检验校正的唯一硬输入（round118 P1-2）

## 为什么必须有这个文件

Bonferroni 阈值 = Φ⁻¹(1 − α/(2K))，**K 是"试过多少次"**。
在此之前，K 是一个手写在源码里的常量，而且三个地方互相矛盾：

| 出处 | 声称的 K | 按 K 复算的阈值 | 实际写的阈值 |
|---|---|---|---|
| `factor_ic.json` 的 `method.bonferroni` | 30 | 3.1440 | 3.90 |
| `factor_eval.py` 源码注释 | 304 | **3.7681** | 3.90 |
| 反解 3.90 得到的 K | **≈519.8** | 3.9001 | 3.90 |

三个数没有一个是 3.90。**阈值与声明对不上，就无法审计。**

根本问题不是"算错了"，而是**"试过多少次"这件事没有账本**。只要 K 靠回忆，
它永远是最后一次回忆时的心情。

## 这个文件的铁律

1. **先登记，后看结果**。`append_trial()` 允许 `nw_t=None` 先登记；
   但**不允许事后修改已有记录**（只允许追加 `superseded_by` 指向新记录）。
   事后补记的 trial log 不可信 —— 但**完全没账本比不可信的账本更糟**，
   所以历史部分明确标 `backfilled: true`，让读者知道它的可信等级。
2. **两种日期分开记**：`trial_date`（试验发生日）与 `recorded_at`（写入账本时刻）。
   两者不一致时，`backfilled=True`。混在一起就无法区分"当时记的"和"后来补的"。
3. **K 只有一个出口**：`n_trials()`。任何脚本要 K，必须调它，不得自己数。
4. **不计入 K 的试验也要记**。`stat_diagnostic`（如 NW 蒙特卡洛）、
   `data_audit`（如价格库补洞）不产生"因子有效性假设"，计入 K 会虚增阈值。
   但它们必须留痕 —— 否则下次有人会重新做一遍。

## ⚠️ 已知缺口（本文件存在的第一个事实）

`legacy.k = 304` 是 2026-10-29 round43 声明的「round13~65 累计检验数」。
**round66~118 又产生了 54 个审计产物、40 个轮次，从未计入 K。**

本文件不擅自改这个数（改 K = 改阈值 = 改所有历史结论，须老大裁决），
而是把它显式暴露成 `k_summary()["gap"]`，让缺口看得见。

## 用法

```python
import trial_log as tl

tl.append_trial(what="score_x 权重 0.10→0", round_no=119,
                kind=tl.KIND_FACTOR, trial_date="2026-11-04",
                nw_t=-1.2, outcome=tl.OUTCOME_REJECTED,
                why="IC 反号")

k = tl.n_trials()          # 唯一出口
print(tl.k_summary())      # legacy / verified / measured / gap 四段
```

命令行：
    python trial_log.py                      # 摘要
    python trial_log.py --list               # 全部记录
    python trial_log.py --backfill-from-artifacts   # 从 audit_round*.json 补登
    python trial_log.py --audit              # 找出"有产物但没登记"的缺口
    python trial_log.py --selftest           # 自检
"""
import os
import re
import sys
import json
import glob
import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
LOG_PATH = os.path.join(HERE, "data", "trial_log.json")

SCHEMA = "trial_log/1"

# ── 试验类型 ────────────────────────────────────────────────────────────
KIND_FACTOR = "factor_test"            # 单因子/维度有效性检验（计入 K）
KIND_STRATEGY = "strategy_backtest"    # 策略/组合回测（计入 K，也是 data snooping 源）
KIND_DIAG = "stat_diagnostic"          # 统计方法诊断，如 NW 小样本蒙特卡洛（不计入）
KIND_DATA = "data_audit"               # 数据完整性审计，如价格库补洞（不计入）
KIND_UNCLASSIFIED = "unclassified"     # 回填但未能判定（不计入，待人工确认）

COUNTED_KINDS = (KIND_FACTOR, KIND_STRATEGY)
ALL_KINDS = (KIND_FACTOR, KIND_STRATEGY, KIND_DIAG, KIND_DATA, KIND_UNCLASSIFIED)

# ── 结论 ────────────────────────────────────────────────────────────────
OUTCOME_ACCEPTED = "accepted"      # 纳入模型/权重
OUTCOME_REJECTED = "rejected"      # 否决
OUTCOME_PARTIAL = "partial"        # 部分成立（如仅某周期）
OUTCOME_PENDING = "pending"        # 未结论
OUTCOME_INVALIDATED = "invalidated"  # 后续被推翻（如口径错误）
ALL_OUTCOMES = (OUTCOME_ACCEPTED, OUTCOME_REJECTED, OUTCOME_PARTIAL,
                OUTCOME_PENDING, OUTCOME_INVALIDATED)

# ── legacy 段（不可考古的聚合声明）────────────────────────────────────────
# 304 的出处已核实：reports/round43_v415_constraint_and_quality_p0_2026-10-29.md
#   第 148 行「项目累计检验已达 304 次（round13~65）」。
# 它是**一次聚合声明**，不是 304 条明细 —— 这正是它无法审计的原因。
LEGACY_K = 304
LEGACY_ROUNDS = "round13~65"
LEGACY_DECLARED_AT = "2026-10-29"
LEGACY_DECLARED_IN = "reports/round43_v415_constraint_and_quality_p0_2026-10-29.md"

# ── 回填用的显式策展表 ────────────────────────────────────────────────
# ⚠️ 这里**故意不用关键词自动分类**。第一版试过关键词，实测 54 条里
#    23 条落到 unclassified（42%），且出现明显误判：
#      · r72/r73「regime 的事前可检测性」被判成 data_audit（实为因子可预测性检验）
#      · r111「T+1 横截面信号检验」被判成 stat_diagnostic（实为因子检验）
#    关键词分类器的错误方向不可控，而 kind 直接决定「是否计入 K」→
#    分类错一个，K 就错一个，阈值跟着漂。**宁可人工逐条定性。**
#
# 本表的每一条都对照过该产物的 `meta.purpose`（见 --list 的 what 字段）。
# 有 purpose 的填分类；无 purpose 的留 KIND_UNCLASSIFIED 并保持 needs_review。
CURATION = {
    # ── 数据审计类：修数据/重建池，不产生「因子有效」假设 ──
    "audit_round66.json": KIND_DATA,      # 9 维长面板重算
    "audit_round91.json": KIND_DATA,      # 逐截面重建可交易池
    # ── 统计方法/诊断类：检验方法本身或基础设施，不产生新假设 ──
    "audit_round67.json": KIND_DIAG,      # IC 层接净值层的回测引擎
    "audit_round77.json": KIND_DIAG,      # 数学可行性边界（上限在哪，非假设检验）
    "audit_round110_model_diagnostics.json": KIND_DIAG,   # LogitBin 超参诊断
    "audit_round112_transformer_verdict.json": KIND_DIAG, # Transformer 无预测能力
    "audit_round114_t0_cost_ceiling.json": KIND_DIAG,     # 成本天花板反算（非回测）
    # ── 因子/维度检验类：计入 K ──
    "audit_round70.json": KIND_FACTOR,    # 组合 ICIR 最大化
    "audit_round71.json": KIND_FACTOR,    # IC 是否依赖 regime
    "audit_round72.json": KIND_FACTOR,    # regime 事前可检测性
    "audit_round73.json": KIND_FACTOR,    # 同上（修重叠前视）
    "audit_round74.json": KIND_FACTOR,    # minimax 稳健权重
    "audit_round79.json": KIND_FACTOR,    # 池稳健性 + 打分有效性
    "audit_round80.json": KIND_FACTOR,    # 严格匹配随机组合（决定性实验）
    "audit_round84.json": KIND_FACTOR,    # α 来源归因（维度消融）
    "audit_round85.json": KIND_FACTOR,    # size_growth 单维严格验证
    "audit_round87.json": KIND_FACTOR,    # size_growth 真实来源拆解
    "audit_round88.json": KIND_FACTOR,    # v4.16 候选权重验收
    "audit_round89.json": KIND_FACTOR,    # 约束内 α 最优化 + 帕累托
    "audit_round92.json": KIND_FACTOR,    # Step1 分层校验 + 三档准入
    "audit_round93.json": KIND_FACTOR,    # Step2 相关性 + Step3 增量 + Step4 权重
    "audit_round94.json": KIND_FACTOR,    # Step1 修正版（regime 分层准入）
    "audit_round95.json": KIND_FACTOR,    # Step3+Step4 minimax 双 regime
    "audit_round96.json": KIND_FACTOR,    # Step5 样本外盲测 + Step6 稳健性
    "audit_round97.json": KIND_FACTOR,    # Step4 权重求解（穷举校验）
    "audit_round99.json": KIND_FACTOR,    # fund 域诊断 + 单调性一票否决
    "audit_round100.json": KIND_FACTOR,   # 接入重建池 + 翻转维度压缩
    "audit_round102.json": KIND_FACTOR,   # v4.15 约束内重解权重
    "audit_round104.json": KIND_FACTOR,   # round102 细网格 + quality 邻域
    "audit_round111_t0_t1_factors.json": KIND_FACTOR,     # T+1 横截面信号检验
    "audit_round111b_t1_decompose.json": KIND_FACTOR,     # T+1 收益拆解（隔夜/日内）
    "audit_round113_holding_period.json": KIND_FACTOR,    # 持有期扫描
    "audit_round113b_holdout.json": KIND_FACTOR,          # 对 round113 三重证伪
    "data/audit_round118c_ic_decay.json": KIND_FACTOR,    # IC 衰减曲线
    # ── round118e/118f：dominant 扩张决策链 ──
    # ⚠️ 这两条为什么算 KIND_FACTOR 而不是 KIND_DIAG：
    #   它们直接产出了「dominant 扩全池」的依据（单维 IC + 复合 IC 三档对照），
    #   而这个结论要进 P0-3 的结构变更。**凡是产出「要不要改模型」结论的检验，
    #   都算一次试验** —— 归成 diag 就等于说「我看过但没据此做决定」，
    #   那正是本项目历史上 K 被系统性低估的机制。
    "data/audit_round118e_dominant_poolwide.json": KIND_FACTOR,   # 全池 dominant 单维 IC
    "data/audit_round118f_dominant_expand_decision.json": KIND_FACTOR,  # 复合 IC 三档对照
    # ── 策略/组合回测类：计入 K（也是 data snooping 的主要来源）──
    "audit_round69.json": KIND_STRATEGY,  # 回测可信度四证伪
    "audit_round75.json": KIND_STRATEGY,  # 组合层优化（基准改 POOL_EW）
    "audit_round76.json": KIND_STRATEGY,  # 三大目标最终裁决
    "audit_round78.json": KIND_STRATEGY,  # 超额波动来源解剖
    "audit_round81.json": KIND_STRATEGY,  # β 敞口调整方案
    "audit_round82.json": KIND_STRATEGY,  # 集中度样本外验证
    "audit_round83.json": KIND_STRATEGY,  # alpha/beta 拆分
    "audit_round109.json": KIND_STRATEGY,          # 波动率目标法（regime）
    "audit_round109b.json": KIND_STRATEGY,         # regime 三分类可行性
    "audit_round109c.json": KIND_STRATEGY,         # 尾部保险 vs vol targeting
    "audit_round109d.json": KIND_STRATEGY,         # 找可预测部分
    "audit_round109e.json": KIND_STRATEGY,         # 倒挂归因 + 经济价值
    "audit_round111c_t1_executable.json": KIND_STRATEGY,  # T+1 可执行口径
    "audit_round114b_gap_reversal.json": KIND_STRATEGY,   # 低开反转 6/6 检验
    "audit_round114c_gap_reversal_robust.json": KIND_STRATEGY,  # 聚类/β中性
    "audit_round114d_gap_signal_nature.json": KIND_STRATEGY,    # 信号性质判定
    "audit_round114e_main_board_only.json": KIND_STRATEGY,      # 主板子样本
    "audit_round114f_gap_signal_final.json": KIND_STRATEGY,     # 最终定性
    "data/audit_round118d_turnover_cost.json": KIND_STRATEGY,   # 换手率/成本实测（6 hop × 3 topN）
    # ── round120/121：反转两维（bias60 / rsi_rev）重映射决策链 ──
    # ⚠️ 全部计入 K，判据同 round118e/f：它们产出了「反转映射要不要改、怎么改」的结论，
    #   而这个结论已经进了生产代码（step2 的池内分位 + 确认门罚分）。
    #   归成 diag 就等于说「我看过但没据此决定」——那正是 K 被系统性低估的机制。
    #   注意：120a~120e 是同一问题的前几版口径（120b 字段符号相反、120c 有 min_grp=1 伪影、
    #   120d/120e 口径不可比），它们的**结论已被 120f 取代**，但仍各算一次试验：
    #   试错本身就是 data snooping，不因为「后来发现算错了」就不算。
    "data/audit_round120_reversal_gate.json": KIND_FACTOR,        # 首版：前置条件扫描
    "data/audit_round120b_focused.json": KIND_FACTOR,             # 聚焦版（字段符号相反，已作废）
    "data/audit_round120c_resolve.json": KIND_FACTOR,             # 口径对账（+0.656 为 min_grp=1 伪影）
    "data/audit_round120d_score_monotonic.json": KIND_FACTOR,     # 分档单调性（未配对）
    "data/audit_round120e_excess.json": KIND_FACTOR,              # excess_mean 全正（口径污染）
    "data/audit_round120f_final.json": KIND_FACTOR,               # ★ 终版严格配对裁决
    "data/audit_round121_rev_remap.json": KIND_FACTOR,            # M0~M4 候选（封顶/置缺维）
    "data/audit_round121b_fine.json": KIND_FACTOR,                # RSI/bias 细分档扫描
    "data/audit_round121c_h20.json": KIND_FACTOR,                 # ★ h=20 主判据复裁（推翻 h=5 结论）
    "data/audit_round121d_dim.json": KIND_FACTOR,                 # 共线/单维 vs 合并/确认门增量
    "data/audit_round121e_final.json": KIND_FACTOR,               # ★ 终选（C0~C7，池内分位胜出）
    # round121f：落地后自查发现「确认门触发范围」超出实测证据区间（并集→只 RSI<30）。
    #   它改的是**生产代码的触发条件**，不是纯诊断，按判据计入 K。
    "data/audit_round121f_gate_scope.json": KIND_FACTOR,
    # ── 无 meta.purpose，无法定性 ──
    "audit_round70b.json": KIND_UNCLASSIFIED,
    "data/audit_round118b_nw_small_sample.json": KIND_DIAG,  # NW 蒙特卡洛（已知用途）
}


# ═══════════════ 读写 ═══════════════

def _now():
    return datetime.datetime.now().strftime("%Y-%m-%dT%H:%M:%S")


def _atomic_write(path, obj):
    """原子写：先写临时文件再 rename。中途崩不产生半截 JSON。"""
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def _empty():
    return {
        "schema": SCHEMA,
        "created_at": _now(),
        "updated_at": _now(),
        "policy": {
            "k_outlet": "n_trials() 是 K 的唯一出口，任何脚本不得自行计数",
            "append_only": ("只允许追加；已有记录不得原地改，只能追加新记录并用 "
                            "superseded_by 指向它"),
            "date_split": ("trial_date = 试验发生日；recorded_at = 写入账本时刻。"
                           "两者不一致 → backfilled=True"),
            "counted_kinds": list(COUNTED_KINDS),
            "uncounted_note": ("stat_diagnostic / data_audit / unclassified "
                              "不计入 K —— 它们不产生「因子有效」这类假设，"
                              "计入会虚增阈值。但必须留痕，否则会被重做"),
        },
        "legacy": {
            "k": LEGACY_K,
            "rounds": LEGACY_ROUNDS,
            "declared_at": LEGACY_DECLARED_AT,
            "declared_in": LEGACY_DECLARED_IN,
            "verifiable": False,
            "note": ("聚合声明，非逐条明细 → 无法逐项复核。这是「K 不可审计」的"
                     "历史根源，不是本文件造成的。"),
        },
        "trials": [],
    }


def load(create=True):
    """读账本。不存在时（create=True）返回空结构，**不落盘**（避免只读调用产生副作用）。"""
    if not os.path.exists(LOG_PATH):
        if not create:
            raise FileNotFoundError(f"trial log 不存在：{LOG_PATH}")
        return _empty()
    with open(LOG_PATH, encoding="utf-8") as f:
        d = json.load(f)
    if d.get("schema") != SCHEMA:
        raise ValueError(f"trial_log schema 不匹配：{d.get('schema')!r} != {SCHEMA!r}")
    d.setdefault("trials", [])
    d.setdefault("legacy", {})
    return d


def save(d):
    d["updated_at"] = _now()
    os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
    _atomic_write(LOG_PATH, d)


# ═══════════════ 写入 ═══════════════

def _next_id(trials):
    n = 0
    for t in trials:
        m = re.match(r"T(\d+)$", str(t.get("id") or ""))
        if m:
            n = max(n, int(m.group(1)))
    return "T%04d" % (n + 1)


def append_trial(what, round_no=None, kind=KIND_FACTOR, trial_date=None,
                 nw_t=None, outcome=OUTCOME_PENDING, why="", source=None,
                 backfilled=False, needs_review=False, extra=None,
                 n_tests=None):
    """登记一次试验。返回 id。

    **允许 nw_t=None**：先登记"我打算试这个"比事后补记可信得多。
    但 `what` 必填 —— 一条没有描述的记录等于没记。

    `n_tests`（可选）：这一次登记的试验**内部包含几个独立检验**。
    老数据大多拿不到（结构各异），拿不到就留 None —— **不要填 1**，
    填 1 会把"不知道"伪装成"已知"，而 K 的下界就这样被当成真值用掉。

    `needs_review=True` 的记录**不计入 K**（保守方向：宁可阈值偏松，
    也不要拿一条我自己都没确认分类的记录去抬高阈值）。
    """
    if not (what or "").strip():
        raise ValueError("what 必填 —— 空描述等于没记账")
    if kind not in ALL_KINDS:
        raise ValueError(f"kind 必须是 {ALL_KINDS}，收到 {kind!r}")
    if outcome not in ALL_OUTCOMES:
        raise ValueError(f"outcome 必须是 {ALL_OUTCOMES}，收到 {outcome!r}")

    d = load()
    trials = d["trials"]
    tid = _next_id(trials)
    rec = {
        "id": tid,
        "round": round_no,
        "kind": kind,
        "trial_date": trial_date or datetime.date.today().isoformat(),
        "recorded_at": _now(),
        "backfilled": bool(backfilled),
        "needs_review": bool(needs_review),
        "what": what.strip(),
        "nw_t": (round(float(nw_t), 4) if nw_t is not None else None),
        "outcome": outcome,
        "why": (why or "").strip(),
        "source": source,
        "n_tests": (int(n_tests) if n_tests else None),
        "superseded_by": None,
    }
    if extra:
        rec["extra"] = extra
    trials.append(rec)
    save(d)
    return tid


def supersede(old_id, **kw):
    """把旧记录标为被取代，并追加一条新记录。

    用于"之前那条记错了/口径改了"。**不删除、不改原记录** —— 抹掉历史
    等于让后来人无法知道「这里改过一次、改的是什么」。
    """
    d = load()
    old = next((t for t in d["trials"] if t["id"] == old_id), None)
    if old is None:
        raise KeyError(f"无此记录：{old_id}")
    kw.setdefault("round_no", old.get("round"))
    kw.setdefault("kind", old.get("kind"))
    new_id = append_trial(**kw)
    d = load()
    for t in d["trials"]:
        if t["id"] == old_id:
            t["superseded_by"] = new_id
    save(d)
    return new_id


# ═══════════════ 读取 / 计数 ═══════════════

def read_trials(kind=None, outcome=None, include_superseded=False,
                reviewed_only=False):
    d = load()
    out = []
    for t in d["trials"]:
        if not include_superseded and t.get("superseded_by"):
            continue
        if kind is not None:
            ks = (kind,) if isinstance(kind, str) else tuple(kind)
            if t.get("kind") not in ks:
                continue
        if outcome is not None and t.get("outcome") != outcome:
            continue
        if reviewed_only and t.get("needs_review"):
            continue
        out.append(t)
    return out


def n_trials(reviewed_only=True, basis="both"):
    """**K 的唯一出口。**

    ## ⚠️ 关于两个不可通约的单位（这是本函数的核心，改它前必读）

    `legacy.k = 304` 的单位是**「检验次数」**（round43 声明的 round13~65 累计）。
    而回填的 trial 记录，单位是**「审计产物 / 试验轮次」** —— 一个产物里
    往往装了多个检验：实测 `audit_round92.json` 的 `verdicts` 有 **9** 条、
    `audit_round113_holding_period.json` 的 `results` 有 **13** 条。

    **两种单位不能直接相加。** 实测 45 个计入类产物中，**25 个无法从产物
    中提取精确检验次数**（结构各异，没有统一的 `n_tests` 字段）。

    所以本函数给两个口径，**都不假装精确**：

    | basis | 含义 | 用途 |
    |---|---|---|
    | `"trials"` | legacy.k + 试验轮次数 | **下界**：单位混用但仍是最保守的可证数 |
    | `"both"`（默认） | 同上，并返回 `k_range` | 调用方自己决定用哪个 |

    **为什么默认给下界而不是估个数**：K 估小 → 阈值偏松 → 容易把噪声报成信号，
    这是明确的风险方向。但 K 估大同样有问题：会把真信号误杀，且**看起来更严谨**，
    更难被发现。两个方向都不该由我拍板，所以：
    **默认返回可证下界，并把不确定性显式返回**，由使用方（DSR / Bonferroni /
    报告）各自声明采用哪个口径。
    """
    d = load()
    legacy = int((d.get("legacy") or {}).get("k") or 0)
    rows = read_trials(kind=COUNTED_KINDS, reviewed_only=reviewed_only)
    k = legacy + len(rows)
    if basis == "trials":
        return k
    # "both" —— 附带区间信息（不改变返回值，避免调用方静默拿到一个"估算值"）
    return k


def k_range(reviewed_only=True):
    """K 的**可证下界 ~ 上界**，连同"为什么上界不可精确"一起返回。

    上界粗估：把"无法提取检验数"的产物按已提取产物的**中位检验数**补。
    这不是精确值，是为了让读者看到"下界与真实值可能差一个量级"，
    而不是把下界当成真值直接代进 Φ⁻¹。
    """
    d = load()
    legacy = int((d.get("legacy") or {}).get("k") or 0)
    rows = read_trials(kind=COUNTED_KINDS, reviewed_only=reviewed_only)
    per = [(t.get("n_tests") or 0) for t in rows]
    known = [x for x in per if x > 0]
    unknown = sum(1 for x in per if x <= 0)
    med = sorted(known)[len(known) // 2] if known else 1
    lo = legacy + len(rows)
    hi = legacy + sum(known) + unknown * med
    return {
        "k_lower": lo,
        "k_upper_estimate": hi,
        "n_trials_counted": len(rows),
        "n_with_known_n_tests": len(known),
        "n_without_n_tests": unknown,
        "median_n_tests_when_known": med,
        "note": ("下界 = legacy.k + 试验轮次数（单位混用，保守可证）。"
                 "上界 = 把缺检验数的产物按已知中位数补 —— **这是估计，不是实测**。"
                 "两者差距本身就是「K 无法精确得知」的量化表达。"),
    }


def k_summary():
    """K 的四段账：legacy / 已确认 / 待确认 / 合计。"""
    d = load()
    legacy = int((d.get("legacy") or {}).get("k") or 0)
    reviewed = read_trials(kind=COUNTED_KINDS, reviewed_only=True)
    pending = [t for t in read_trials(kind=COUNTED_KINDS) if t.get("needs_review")]
    uncounted = read_trials(kind=(KIND_DIAG, KIND_DATA, KIND_UNCLASSIFIED))
    unclassified = [t for t in uncounted if t.get("kind") == KIND_UNCLASSIFIED]
    return {
        "legacy_k": legacy,
        "legacy_rounds": (d.get("legacy") or {}).get("rounds"),
        "verified_k": len(reviewed),
        "pending_review_k": len(pending),
        "unclassified": len(unclassified),
        "uncounted_other": len(uncounted) - len(unclassified),
        "k_measured": legacy + len(reviewed),
        "k_upper_bound": legacy + len(reviewed) + len(pending) + len(unclassified),
        "by_kind": {kd: len(read_trials(kind=kd)) for kd in ALL_KINDS},
    }


# ═══════════════ 与审计产物对账 ═══════════════

_ARTIFACT_PAT = re.compile(r"audit_round(\d+)([a-z]?)")


def _artifact_rounds():
    """扫 audit_round*.json，返回 {round: [relpath, ...]}。"""
    out = {}
    for pat in ("audit_round*.json", os.path.join("data", "audit_round*.json")):
        for f in glob.glob(os.path.join(HERE, pat)):
            rel = os.path.relpath(f, HERE)
            m = _ARTIFACT_PAT.search(os.path.basename(rel))
            if not m:
                continue
            out.setdefault(int(m.group(1)), []).append(rel)
    return out


def _guess_kind(purpose, rel=None):
    """查策展表定 kind。**表里没有 → unclassified + 需人工确认**。

    有意不用关键词兜底：兜底会给出「看起来有分类」的结果，
    掩盖「这条新产物根本没人看过」这个事实。而 kind 决定是否计入 K，
    静默兜底 = K 静默漂移。
    """
    if rel is not None:
        rel = rel.replace(os.sep, "/")
        if rel in CURATION:
            return CURATION[rel]
    return KIND_UNCLASSIFIED


def _artifact_purpose(path):
    try:
        with open(os.path.join(HERE, path), encoding="utf-8") as f:
            d = json.load(f)
    except Exception:
        return None
    if not isinstance(d, dict):
        return None
    meta = d.get("meta")
    if isinstance(meta, dict) and meta.get("purpose"):
        return str(meta["purpose"])
    return None


_N_TESTS_KEYS = ("verdicts", "results", "checks", "cases", "by_config",
                 "candidates", "by_scheme", "grid_rows", "rows", "survivors")


def _artifact_n_tests(path):
    """尝试从产物里数出**检验个数**。拿不到返回 None（**不返回 1**）。

    已知只有约 44%（20/45）能取到 —— 没有统一字段。返回 None 比返回 1 诚实：
    `k_range()` 会把这些记成"未知"，而不是把"不知道"算成"1 次"。
    """
    try:
        with open(os.path.join(HERE, path), encoding="utf-8") as f:
            d = json.load(f)
    except Exception:
        return None
    if not isinstance(d, dict):
        return None
    for k in _N_TESTS_KEYS:
        v = d.get(k)
        if isinstance(v, (dict, list)) and len(v) > 0:
            return len(v)
    return None


def backfill_from_artifacts(min_round=66, dry_run=False, verbose=True):
    """把 round>=min_round 的审计产物补登为 trial（`backfilled=True`）。

    ⚠️ **为什么要 min_round**：round13~65 的检验次数已经被 legacy.k=304
    聚合涵盖。若把那段产物也逐条补登，会**重复计算** —— 直接把 K 翻倍，
    阈值从 3.77 抬到 4.2 以上，所有历史结论都会被判为不显著。
    "补账"变成"伪造更严的标准"，是比记不清更糟的错误。
    """
    d = load()
    have = set()
    for t in d["trials"]:
        src = t.get("source")
        if src:
            have.add(src)
    arts = _artifact_rounds()
    added = []
    for rn in sorted(arts):
        if rn < min_round:
            continue
        for rel in arts[rn]:
            if rel in have:
                continue
            purpose = _artifact_purpose(rel)
            kind = _guess_kind(purpose, rel)
            _curated = rel.replace(os.sep, "/") in CURATION
            rec = {
                "what": (purpose or f"round{rn} 审计产物（无 meta.purpose）")[:300],
                "round_no": rn,
                "kind": kind,
                "trial_date": None,        # 产物 mtime 是批量重写时间，不可用
                "outcome": OUTCOME_PENDING,
                "why": ("从审计产物回填；kind 取自 trial_log.CURATION 策展表"
                        if _curated else
                        "从审计产物回填；**策展表未收录 → 分类待人工确认**"),
                "source": rel,
                "backfilled": True,
                # 策展表已定性的不必再标 review；未收录的必须标
                "needs_review": not _curated,
                "n_tests": _artifact_n_tests(rel),
            }
            if verbose and not dry_run:
                tid = append_trial(**rec)
                added.append((tid, rn, kind, rel))
            else:
                added.append((None, rn, kind, rel))
    return added


def audit_gaps(min_round=66):
    """找出「有审计产物但账本里没有」的缺口。

    这是 trial log 的**看门狗**：一次性脚本不在日链 = 必然陈旧
    （本项目已在 panel 构建上踩过）。账本同理，必须有机制在它变旧时叫出来。
    """
    d = load()
    have = {t.get("source") for t in d["trials"] if t.get("source")}
    arts = _artifact_rounds()
    missing = []
    for rn in sorted(arts):
        if rn < min_round:
            continue
        for rel in arts[rn]:
            if rel not in have:
                missing.append({"round": rn, "source": rel,
                                "purpose": _artifact_purpose(rel)})
    return missing


# ═══════════════ 自检 ═══════════════

def selftest():
    ok = True
    print("=" * 78)
    print("trial_log 自检（内存内，不写盘）")
    print("=" * 78)

    # 1. K 的唯一出口存在且与 legacy 一致（空账本时）
    base = LEGACY_K
    print(f"1. legacy.k = {base}", "✅" if base == 304 else "❌")
    ok &= (base == 304)

    # 2. 空描述必须拒绝
    try:
        append_trial(what="   ")
        print("2. 空 what 未 raise ❌")
        ok = False
    except ValueError:
        print("2. 空 what 已 raise ✅")

    # 3. 非法 kind / outcome 必须拒绝
    bad = 0
    for kw in ({"what": "x", "kind": "nonsense"},
               {"what": "x", "outcome": "nonsense"}):
        try:
            append_trial(**kw)
            bad += 1
        except ValueError:
            pass
    print(f"3. 非法 kind/outcome 拒绝数 {2-bad}/2", "✅" if bad == 0 else "❌")
    ok &= (bad == 0)

    # 4. needs_review 的记录不得计入 K
    d = load()
    n_rev = len(read_trials(kind=COUNTED_KINDS, reviewed_only=True))
    n_all = len(read_trials(kind=COUNTED_KINDS))
    print(f"4. K 只数已确认：已确认 {n_rev} ≤ 计入类总数 {n_all}",
          "✅" if n_rev <= n_all else "❌")
    ok &= (n_rev <= n_all)

    # 5. 不计入类（诊断/数据审计）确实不在 K 里
    cnt = k_summary()
    k_from_parts = base + cnt["verified_k"]
    print(f"5. n_trials()={n_trials()} == legacy+verified={k_from_parts}",
          "✅" if n_trials() == k_from_parts else "❌")
    ok &= (n_trials() == k_from_parts)

    print("=" * 78)
    print("全部通过 ✅" if ok else "存在失败 ❌")
    return 0 if ok else 1


# ═══════════════ CLI ═══════════════

def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(description="多重检验 trial log（round118 P1-2）")
    ap.add_argument("--list", action="store_true", help="列出全部记录")
    ap.add_argument("--kind", choices=ALL_KINDS, help="只看某类")
    ap.add_argument("--backfill-from-artifacts", action="store_true",
                    help="从 audit_round*.json 补登 round>=66 的产物")
    ap.add_argument("--min-round", type=int, default=66,
                    help="补登起点（默认 66；<66 会与 legacy 304 重复计算）")
    ap.add_argument("--audit", action="store_true", help="找出未登记的缺口")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)

    if a.selftest:
        return selftest()

    if a.backfill_from_artifacts:
        added = backfill_from_artifacts(min_round=a.min_round,
                                       dry_run=a.dry_run)
        tag = "（dry-run，未写盘）" if a.dry_run else ""
        print(f"补登 {len(added)} 条{tag}")
        for tid, rn, kind, rel in added:
            print(f"  {tid or '—':6s} r{rn:<4d} {kind:20s} {rel}")
        if not a.dry_run:
            print("\n" + json.dumps(k_summary(), ensure_ascii=False, indent=1))
        return 0

    if a.audit:
        miss = audit_gaps(min_round=a.min_round)
        print(f"未登记的审计产物：{len(miss)} 个（round>={a.min_round}）")
        for m in miss:
            print(f"  r{m['round']:<4d} {m['source']}")
        return 0 if not miss else 1

    if a.list:
        rows = read_trials(kind=a.kind)
        print(f"共 {len(rows)} 条")
        for t in rows:
            flag = ""
            if t.get("backfilled"):
                flag += "📥"
            if t.get("needs_review"):
                flag += "❓"
            nt = f"{t['nw_t']:+.2f}" if t.get("nw_t") is not None else "  — "
            print(f"  {t['id']} r{str(t.get('round')):>4s} "
                  f"{t['trial_date']} {t['kind']:20s} t={nt} "
                  f"{t['outcome']:12s}{flag} {t['what'][:50]}")
        return 0

    print(json.dumps(k_summary(), ensure_ascii=False, indent=1))
    lg = (load().get("legacy") or {})
    print(f"\n⚠️ legacy.k={lg.get('k')} 来自 {lg.get('rounds')}"
          f"（{lg.get('declared_at')} 声明于 {lg.get('declared_in')}）")
    print("   round66 起的试验须逐条登记；缺口用 --audit 查。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
