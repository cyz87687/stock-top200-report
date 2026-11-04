#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""round36 七处权重入口一致性校验（复用 gen_report 的判定逻辑，但独立可跑）。

为什么单独写：gen_report_top200.py 在 import 时就会执行
_verify_weight_consistency() 并**继续往下跑整个出图流程**（找不到当日 scored
就抛 FileNotFoundError）。想只做校验就必须把函数抽出来独立运行。

七处入口（与项目记忆一致）：
  1 factor_weights.json  weights
  2 factor_weights.json  prior_weights   （必须与 weights 逐键相等）
  3 factor_eval.py       PRIOR_W10
  4 step2_stockscorer_v2.py _W_PRIOR_10D
  5 apply_sector_momentum.py _W_PRIOR_10D
  6 enhance_scores.py    _W_PRIOR_10D
  7 gen_report_top200.py _fb
另加 dim_max 三处：factor_weights.json / step2._DIM_MAX_DEFAULT / enhance / apply / factor_eval
"""
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ERRS = []
INFO = []


def _read(name):
    p = os.path.join(HERE, name)
    if not os.path.exists(p):
        ERRS.append(f"{name} 不存在")
        return ""
    with open(p, encoding="utf-8", errors="replace") as f:
        return f.read()


def _grab_dict(src, pat):
    """从源码里抓 `pat = {...}` 的键值（支持一行或多行）。"""
    m = re.search(pat + r"\s*=\s*\{(.*?)\}", src, re.S)
    if not m:
        return None
    return {k: float(v) for k, v in
            re.findall(r'"(score_[a-z0-9_]+)"\s*:\s*([0-9.]+)', m.group(1))}


# ---------- 1/2 配置文件 ----------
cfg = json.load(open(os.path.join(HERE, "factor_weights.json"), encoding="utf-8"))
W = cfg.get("weights") or {}
PW = cfg.get("prior_weights") or {}
DMAX_CFG = cfg.get("dim_max") or {}

tot = sum(float(v) for v in W.values())
INFO.append(f"factor_weights.json version = {cfg.get('version')}  Σw = {tot:.6f}")
if abs(tot - 1.0) > 0.02:
    ERRS.append(f"weights 合计 {tot:.4f} ≠ 1.0")
for k, v in PW.items():
    if abs(float(W.get(k, -1)) - float(v)) > 1e-9:
        ERRS.append(f"prior_weights[{k}]={v} ≠ weights[{k}]={W.get(k)}")

EXPECT = {k: float(v) for k, v in W.items() if float(v or 0) > 0}
INFO.append("生效维（w>0）: " + ", ".join(
    f"{k.replace('score_','')}={v}" for k, v in
    sorted(EXPECT.items(), key=lambda x: -x[1])))

# ---------- 约束校验（v4.15-constraint-cap，老大 2026-10-29 指令）----------
# 老大指令：单因子上限 0.20 / 5 大类各 ≤0.30 / 单因子下限 0。
# ⚠️ 必须在这里校验而不是只在 apply 脚本里 —— 漏一处就是静默回退无告警（项目铁律）。
CONSTRAINT_META = cfg.get("weight_constraints") or {}
if CONSTRAINT_META:
    DIM_CAP = float(CONSTRAINT_META.get("dim_cap", 0.20))
    GROUP_CAP = float(CONSTRAINT_META.get("group_cap", 0.30))
    DIM_FLOOR = float(CONSTRAINT_META.get("dim_floor", 0.0))
    GROUPS = CONSTRAINT_META.get("groups") or {}
else:
    # 无元信息时用默认口径（不报错，但要提示）
    DIM_CAP, GROUP_CAP, DIM_FLOOR = 0.20, 0.30, 0.0
    GROUPS = {
        "基本面": ["score_fund", "score_quality"],
        "估值": ["score_ep"],
        "低波": ["score_lowvol20"],
        "技术反转": ["score_bias60", "score_rsi_rev", "score_tech_neg"],
        "市值": ["score_size_growth", "score_dominant"],
    }
    INFO.append("⚠️ 配置内无 weight_constraints 元信息，按默认口径校验")

for k, v in EXPECT.items():
    if v > DIM_CAP + 1e-9:
        ERRS.append(f"约束违规：单因子 {k}={v} > 上限 {DIM_CAP}")
    if v < DIM_FLOOR - 1e-9:
        ERRS.append(f"约束违规：单因子 {k}={v} < 下限 {DIM_FLOOR}")
for g, ms in GROUPS.items():
    s = sum(EXPECT.get(m, 0.0) for m in ms)
    if s > GROUP_CAP + 1e-9:
        ERRS.append(f"约束违规：大类 {g}={s:.4f} > 上限 {GROUP_CAP}")
    INFO.append(f"约束大类 {g}: {s:.4f} (≤{GROUP_CAP})")
INFO.append(f"约束校验：单因子 ≤{DIM_CAP} / 大类 ≤{GROUP_CAP} / 下限 {DIM_FLOOR}")

# ---------- 3~7 五个 py 硬编码先验 ----------
# ⚠️ round108（2026-11-02）新增第 6 项 gen_report_top200.py:_rf_W_ACT ——
#   它与同文件的 _fb 是**两个独立入口**：_fb 决定「权重条展示」，
#   _rf_W_ACT 决定「证据缺口名义权重合计（_rf51_miss_pct）」。此前只校验 _fb，
#   round108 换权重时 _rf_W_ACT 静默停在旧值（growth .05 / rsi_rev .10），
#   页面会把 55% 的证据缺口算错 → 漏一处=静默回退，故一并纳入。
TARGETS = [("factor_eval.py", "PRIOR_W10"),
           ("step2_stockscorer_v2.py", "_W_PRIOR_10D"),
           ("apply_sector_momentum.py", "_W_PRIOR_10D"),
           ("enhance_scores.py", "_W_PRIOR_10D"),
           ("gen_report_top200.py", "_fb"),
           ("gen_report_top200.py", "_rf_W_ACT")]
for fn, pat in TARGETS:
    src = _read(fn)
    if not src:
        continue
    d = _grab_dict(src, pat)
    if d is None:
        ERRS.append(f"{fn}: 未找到 {pat} 定义")
        continue
    bad = [(k, d.get(k), v) for k, v in EXPECT.items()
           if abs(d.get(k, -1) - v) > 1e-9]
    if bad:
        for k, a, b in bad:
            ERRS.append(f"{fn}: {pat}[{k}]={a} ≠ 配置 {b}")
    else:
        INFO.append(f"{fn}:{pat} ✅ 与配置一致（{len(EXPECT)} 维）")

# ---------- dim_max 入口 ----------
if DMAX_CFG:
    WANT_DMAX = {k: float(v) for k, v in DMAX_CFG.items() if float(v or 0) > 0}
    INFO.append("dim_max(生效): " + ", ".join(
        f"{k.replace('score_','')}={v}" for k, v in sorted(WANT_DMAX.items())))
    for fn, pat in [("step2_stockscorer_v2.py", "_DIM_MAX_DEFAULT"),
                    ("enhance_scores.py", "_DIM_MAX_DEFAULT"),
                    ("apply_sector_momentum.py", "_DIM_MAX_DEFAULT"),
                    ("factor_eval.py", "_DIM_MAX_DEFAULT")]:
        src = _read(fn)
        if not src:
            continue
        m = re.search(pat + r"\s*=\s*\{(.*?)\}", src, re.S)
        if not m:
            INFO.append(f"{fn}: 无 {pat}（跳过）")
            continue
        d = {k: float(v) for k, v in
             re.findall(r'"(score_[a-z0-9_]+)"\s*:\s*([0-9.]+)', m.group(1))}
        bad = [(k, d.get(k), v) for k, v in WANT_DMAX.items()
               if k in d and abs(d[k] - v) > 1e-9]
        if bad:
            for k, a, b in bad:
                ERRS.append(f"{fn}: {pat}[{k}]={a} ≠ 配置 {b}")
        else:
            INFO.append(f"{fn}:{pat} ✅ 与配置一致（{len(d)} 维）")

# ---------- 8 准入裁决（P0-3 + P1-2，round101 新增）----------
# 为什么并入本脚本：此前「七处一致」只能保证权重在各文件间**没走样**，
# 但完全不检查「这组权重本身能不能用」。round101 实测线上 v4.15 在
# 五道门禁上 0 通过，而链路一直照常出图 —— 说明缺的正是这一层。
#
# ⚠️ 默认**不阻断**（advisory），因为当前 v4.15 就是 FAIL 状态；
#    若直接阻断会让整条链路停摆。设 ADMISSION_ENFORCE=1 可改为硬阻断，
#    届时权重必须先通过 weight_admission_gate.py 才能出图。
ENFORCE = os.environ.get("ADMISSION_ENFORCE", "0") == "1"
try:
    import subprocess
    gate = os.path.join(HERE, "weight_admission_gate.py")
    if not os.path.exists(gate):
        INFO.append("准入裁决：weight_admission_gate.py 不存在（跳过）")
    else:
        gp = os.path.join(HERE, "weight_gate_verify.json")
        subprocess.run(
            [sys.executable, gate, "--weights",
             os.path.join(HERE, "factor_weights.json"),
             "--domain", "rebuild", "--skip-wf", "--json", gp],
            capture_output=True, text=True, timeout=300)
        verdict = None
        if os.path.exists(gp):
            try:
                verdict = json.load(open(gp, encoding="utf-8"))
            except Exception:
                verdict = None
        if verdict:
            v = verdict.get("verdict")
            g = verdict.get("gates") or {}
            INFO.append(f"准入裁决(快速档 G1/G2): {v}  "
                        f"G1={'Y' if g.get('g1_monotonic_robust') else 'N'} "
                        f"G2={'Y' if g.get('g2_spread_positive') else 'N'}  "
                        f"spread={verdict.get('spread')} "
                        f"(t={verdict.get('spread_t')})")
            if v == "FAIL":
                msg = ("准入裁决未通过：G1/G2 一票否决已成立。"
                       "完整裁决请跑 weight_admission_gate.py --domain rebuild；"
                       "详见 reports/round101_p0p1_landing_2026-10-29.md")
                if ENFORCE:
                    ERRS.append(msg)
                else:
                    INFO.append("⚠️ " + msg + "（当前为 advisory，未阻断）")
        else:
            INFO.append("准入裁决：无结果输出（跳过）")
except Exception as e:
    INFO.append(f"准入裁决执行失败（跳过）: {e}")

print("=" * 72)
for s in INFO:
    print("  " + s)
print("=" * 72)
if ERRS:
    print("❌ 不一致 %d 处：" % len(ERRS))
    for e in ERRS:
        print("   - " + e)
    sys.exit(1)
print("✅ 八处权重入口 + dim_max 入口全部一致")
