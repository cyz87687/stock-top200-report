#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
round128｜C1 权重方案落地（八处同步 + 约束校验 + 测试锁同步）
================================================================
老大 2026-11-04「全部改动」裁决：采纳 P0-4 实证里**唯一全档改善**的 C1 方案。

依据（data/weight_p04_constrained.json，round124 实测；
     注意：不是 weight_scheme_curve.json，后者是 IC/TopN 方向性诊断）：
    tech_neg .10 → 0、growth .10 → .20
    N6 / N10 / N13 / N30 / N50 全档优于现行（+0.14 ~ +1.13pp）
    代价：复合 IC −10%（+0.0539 → +0.0485）
    ⇒ **裁决目标函数是 TopN 头部超额，不是全截面 IC**。
      生产持仓只有 6~13 只，IC 衡量的是全池排序，两者方向本就相反：
      lowvol20 IC +0.0828 但 TopN 全程负；size_growth IC +0.0337 但 TopN 全程正。
      禁把 IC 排名映射成权重（见 guard_ic_driven_weights.py）。

合规校验（v4.15 约束，逐项实测而非只写注释）：
    单因子 ≤ 0.20 : growth .20 = 上限 ✅ 其余最大 .20 ✅
    大类   ≤ 0.30 : 业绩动量 .20 ✅ / 技术反转 .10（原 .20）✅ / 市值 .30 ✅
    Σ = 1.00      : 0.20+0.20+0+0.10+0.10+0.10+0.20+0.05+0.05 = 1.00 ✅
    target_horizon = 20 不变 ✅

⚠️ 与 round108 脚本的差别（本轮新增三件事）：
  1) 写盘前把 8 个目标文件快照到 .snap_round128_pre_c1/（留证用 .snap_ 不用 .bak）
  2) 约束校验器真的执行，任一不过即 SystemExit，不写盘
  3) 测试里的权重指纹锁 / 版本锁同步更新，避免"改了权重但测试仍锁旧指纹"

用法：
    python3 apply_round128_c1.py --dry-run    # 只打印变更，不写盘
    python3 apply_round128_c1.py              # 实际写入
"""
import argparse
import hashlib
import json
import os
import re
import shutil
import time

HERE = os.path.dirname(os.path.abspath(__file__))
FW = os.path.join(HERE, "factor_weights.json")
SNAP = os.path.join(HERE, ".snap_round128_pre_c1")

NEW_W = {"score_tech_neg": 0.00, "score_growth": 0.20}
OLD_W = {"score_tech_neg": 0.10, "score_growth": 0.10}
NEW_VERSION = "v4.18-c1-head-excess"
OLD_VERSION = "v4.17-cycle-rules"

# 生产链路的六处 py 硬编码（与 round108 完全一致；_fb 与 _rf_W_ACT 是同文件
# 两个独立入口，漏后者会静默把「无 IC 证据的名义权重合计」算成旧口径）
TARGETS = [("factor_eval.py", "PRIOR_W10"),
           ("step2_stockscorer_v2.py", "_W_PRIOR_10D"),
           ("apply_sector_momentum.py", "_W_PRIOR_10D"),
           ("enhance_scores.py", "_W_PRIOR_10D"),
           ("gen_report_top200.py", "_fb"),
           ("gen_report_top200.py", "_rf_W_ACT")]

# 测试里锁死权重指纹 / 版本的两处，落地时必须一并更新
TEST_LOCKS = ["test_round117_p0_p1.py", "test_round124_p0_p4.py"]

CHANGES = []


def set_dict_values(src, pat, updates, label):
    """在源码里把 `pat = {...}` 内的指定键改成新值，保持格式不动。"""
    m = re.search(pat + r"\s*=\s*\{", src)
    if not m:
        raise RuntimeError(f"{label}: 未找到 {pat} 定义")
    i = src.index("{", m.start())
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
        raise RuntimeError(f"{label}: {pat} 花括号未闭合")
    block = src[i:end + 1]
    new_block = block
    for k, newv in updates.items():
        pk = re.compile(r'("' + re.escape(k) + r'"\s*:\s*)([0-9.]+)')
        found = pk.search(new_block)
        if not found:
            raise RuntimeError(f"{label}: {pat} 内未找到键 {k}")
        oldv = float(found.group(2))
        if abs(oldv - OLD_W[k]) > 1e-9:
            raise RuntimeError(
                f"{label}:{pat}[{k}] 现值 {oldv} ≠ 预期旧值 {OLD_W[k]} "
                f"—— 说明有人改过，拒绝盲改")
        new_block = pk.sub(lambda mm: f'{mm.group(1)}{newv:.6f}',
                           new_block, count=1)
        CHANGES.append(f"{label}:{pat}[{k}] {oldv:.2f} → {newv:.2f}")
    return src[:i] + new_block + src[end + 1:]


def check_constraints(w):
    """v4.15 三条硬约束，真的执行。"""
    errs = []
    tot = sum(float(v or 0) for v in w.values())
    if abs(tot - 1.0) > 1e-9:
        errs.append(f"Σw = {tot:.6f} ≠ 1.0")
    cap = 0.20
    for k, v in w.items():
        if float(v or 0) > cap + 1e-9:
            errs.append(f"单因子 {k}={v} > {cap}")
        if float(v or 0) < 0:
            errs.append(f"单因子 {k}={v} < 0")
    with open(FW, encoding="utf-8") as f:
        cfg = json.load(f)
    groups = ((cfg.get("weight_constraints") or {}).get("groups") or {})
    gcap = float((cfg.get("weight_constraints") or {}).get("group_cap") or 0.30)
    for g, dims in groups.items():
        s = sum(float(w.get(d, 0) or 0) for d in dims)
        if s > gcap + 1e-9:
            errs.append(f"大类 {g}={s:.3f} > {gcap}")
    covered = {d for dims in groups.values() for d in dims}
    for k, v in w.items():
        if float(v or 0) > 0 and k not in covered:
            errs.append(f"{k} 权重>0 但不在任何大类（治理缺口）")
    return errs, tot


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    cfg = json.load(open(FW, encoding="utf-8"))
    if cfg.get("version") != OLD_VERSION:
        raise SystemExit(f"❌ 当前 version={cfg.get('version')}，"
                         f"预期 {OLD_VERSION}（防重复执行/防在别的版本上盲改）")
    if int(cfg.get("target_horizon") or 0) != 20:
        raise SystemExit("❌ target_horizon ≠ 20 —— 改因子前先确认持有周期")

    # ---------- 1) factor_weights.json ----------
    for k, v in NEW_W.items():
        old = float(cfg["weights"].get(k, -1))
        if abs(old - OLD_W[k]) > 1e-9:
            raise SystemExit(f"❌ weights[{k}]={old} ≠ 预期 {OLD_W[k]}")
        cfg["weights"][k] = v
        cfg["prior_weights"][k] = v
        CHANGES.append(f"factor_weights.json: weights/prior_weights[{k}] "
                       f"{old:.2f} → {v:.2f}")

    errs, tot = check_constraints(cfg["weights"])
    if errs:
        raise SystemExit("❌ 约束校验 FAIL，拒绝写盘：\n   - " + "\n   - ".join(errs))
    print(f"✅ 约束校验 PASS：Σ={tot:.2f} / 单因子≤0.20 / 大类≤0.30 / 无治理缺口")

    cfg["version"] = NEW_VERSION
    cfg["fitted_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    cfg["method"] = (
        "v4.18-c1-head-excess：2026-11-04 round128「全部改动」裁决，采纳 P0-4 实证里"
        "唯一全档改善的 C1 方案。tech_neg .10→0（IC −0.0069 / NW t −0.67，"
        "与 0 无法区分，属机会成本而非负 alpha）、growth .10→.20。"
        "依据 data/weight_scheme_curve.json：N6/N10/N13/N30/N50 全档优于现行"
        "（+0.14~+1.13pp），代价复合 IC −10%（+0.0539→+0.0485）。"
        "⭐ 目标函数是 **TopN 头部超额**（生产持仓 6~13 只），不是全截面 IC —— "
        "实测两者方向相反：lowvol20 IC +0.0828 但 TopN 全区间负，"
        "size_growth IC +0.0337 但 TopN 全程正。**禁按 IC 排权重**。"
        "⚠️ 仍是产品裁决、非 IC 背书：G5 样本外未闭合、fund(0.20) 长面板无历史快照，"
        "禁止表述为『经验证的 alpha』。target_horizon=20 不变。"
    )
    cfg.setdefault("notes", []).insert(0, (
        "【v4.18-c1-head-excess】2026-11-04 round128 落地 C1：tech_neg .10→0 / "
        "growth .10→.20。八处同步（本 json 的 weights+prior_weights、"
        "factor_eval.PRIOR_W10、step2/enhance/apply 的 _W_PRIOR_10D、"
        "gen_report._fb 与 _rf_W_ACT）+ 测试指纹锁同步。约束校验 PASS："
        "Σ=1.00 / growth .20 恰为单因子上限 / 业绩动量组 .20≤.30 / "
        "技术反转组 .20→.10。代价 IC −10%，收益是头部超额全档改善。"
        "⚠️ 本轮同时修了长面板 dominant 回填的 em 转置 bug，"
        "长面板有效权重 Σ 由 0.70 提升到 0.80（fund .20 仍是真盲区）。"
    ))
    cfg["weight_scheme_decision"] = {
        "adopted": "C1",
        "target_function": "TopN_head_excess",
        "not_target_function": "cross_section_IC",
        "evidence": "data/weight_p04_constrained.json",
        "ic_cost_pct": -10.0,
        "head_excess_gain_pp": [0.14, 1.13],
        "note": ("生产持仓 6~13 只，IC 是全池排序指标；两者方向相反已被实测，"
                 "见 guard_ic_driven_weights.py"),
    }

    # ---------- 2) 六处 py 硬编码 ----------
    rendered = {}
    for fn, pat in TARGETS:
        p = os.path.join(HERE, fn)
        src = open(p, encoding="utf-8").read()
        if fn in rendered:
            src = rendered[fn]
        rendered[fn] = set_dict_values(src, pat, NEW_W, fn)

    # ---------- 3) 测试锁同步 ----------
    new_sha = hashlib.sha1(
        json.dumps(cfg, ensure_ascii=False, indent=2).encode("utf-8")
    ).hexdigest()
    old_sha = "c98df667b5f9353060bedb3d35866edcc06dc1de"
    CHANGES.append(f"权重文件 sha1: {old_sha} → {new_sha}")
    for fn in TEST_LOCKS:
        p = os.path.join(HERE, fn)
        src = open(p, encoding="utf-8").read()
        src = src.replace(old_sha, new_sha)
        src = src.replace(f'"{OLD_VERSION}"', f'"{NEW_VERSION}"')
        src = src.replace(f"（{OLD_VERSION}）", f"（{NEW_VERSION}）")
        rendered[fn] = src
        CHANGES.append(f"{fn}: 指纹锁 + 版本锁 → {NEW_VERSION} / {new_sha[:12]}")

    # ---------- 4) 写盘 ----------
    print("=" * 78)
    for c in CHANGES:
        print("  " + c)
    print("=" * 78)
    if args.dry_run:
        print("\n[DRY-RUN] 未写盘")
        return

    os.makedirs(SNAP, exist_ok=True)
    for fn in [os.path.basename(FW)] + sorted(set(
            [t[0] for t in TARGETS] + TEST_LOCKS)):
        shutil.copy2(os.path.join(HERE, fn), os.path.join(SNAP, fn))
    print(f"留证快照 → {SNAP}/ ({len(os.listdir(SNAP))} 个文件)")

    with open(FW, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    for fn, src in rendered.items():
        with open(os.path.join(HERE, fn), "w", encoding="utf-8") as f:
            f.write(src)
    real_sha = hashlib.sha1(open(FW, "rb").read()).hexdigest()
    if real_sha != new_sha:
        raise SystemExit(f"❌ 落盘 sha1 {real_sha} ≠ 测试锁 {new_sha} —— "
                         f"文件序列化与预期不一致，请检查是否有 BOM/换行差异")
    print(f"\n[OK] 已写入 {len(rendered)+1} 个文件；实盘 sha1 = {real_sha}")


if __name__ == "__main__":
    main()
