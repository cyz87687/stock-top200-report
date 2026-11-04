#!/usr/bin/env python3
"""
round108i｜v4.17-cycle-rules 权重落地（七处同步）
====================================================
老大 2026-11-02 裁决 D1~D8 全采纳：
  D1 规则1 换口径 → 毛利率抬升 > 3pp（已落 growth_factor_b）
  D2「余量给 fund」撞双约束 → 余量全给 growth（.05→.10），fund 保持 .20
  D3 P1g 去重 → growth 合成分纯 rank_Gq（已落 growth_factor_b）
  D4 规则2 扣 15%（已落）
  D5 叠加上限 25%（已落）
  D6 光伏算周期（已落 cycle_industry.json）
  D7 联动提权（本脚本）
  D8 周期标签单一来源（已落 cycle_industry.json）

权重变动：
  score_growth   0.05 → 0.10   （业绩动量通道加倍，规则1/2 才有传导效力）
  score_rsi_rev  0.10 → 0.05   （反转簇让出，round92 实证单维 IC 不显著）
  Σ = 1.0 不变，其余七维一律不动

新增大类 C1：
  weight_constraints.groups 新增「业绩动量」: ["score_growth"]，cap 0.30
  → 补 round105 治理缺口（growth 此前不在任何大类，无组约束）

合规校验（v4.15 约束）：
  单因子 ≤ 0.20：growth .10 ✅ / rsi_rev .05 ✅
  大类 ≤ 0.30：基本面 .30 ✅ / 估值 .10 ✅ / 技术反转 .20 ✅ / 市值 .30 ✅ / 业绩动量 .10 ✅

用法：
  python3 apply_round108.py --dry-run   # 只打印变更
  python3 apply_round108.py             # 实际写入
"""
import argparse
import json
import os
import re

HERE = os.path.dirname(os.path.abspath(__file__))

NEW_W = {
    "score_growth": 0.10,        # 0.05 → 0.10
    "score_rsi_rev": 0.05,       # 0.10 → 0.05
}
OLD_W = {
    "score_growth": 0.05,
    "score_rsi_rev": 0.10,
}
NEW_VERSION = "v4.17-cycle-rules"
NEW_GROUP = ("业绩动量", ["score_growth"])
GROUPS_AFTER = {
    "基本面": ["score_fund", "score_quality"],
    "估值": ["score_ep"],
    "低波": ["score_lowvol20"],
    "技术反转": ["score_bias60", "score_rsi_rev", "score_tech_neg"],
    "市值": ["score_size_growth", "score_dominant"],
    "业绩动量": ["score_growth"],
}

CHANGES = []


def set_dict_values(src, pat, updates, label):
    """在源码里把 `pat = {...}` 内的指定键改成新值，保持格式不动。"""
    m = re.search(pat + r"\s*=\s*\{", src)
    if not m:
        raise RuntimeError(f"{label}: 未找到 {pat} 定义")
    start = m.start()
    # 找到匹配的右花括号
    i = src.index("{", start)
    depth = 0
    for j in range(i, len(src)):
        if src[j] == "{":
            depth += 1
        elif src[j] == "}":
            depth -= 1
            if depth == 0:
                end = j
                break
    block = src[i:end + 1]
    new_block = block
    for k, newv in updates.items():
        # 匹配 "score_x": 0.05  或  "score_x": 0.05,
        pk = re.compile(r'("' + re.escape(k) + r'"\s*:\s*)([0-9.]+)')
        found = pk.search(new_block)
        if not found:
            raise RuntimeError(f"{label}: {pat} 内未找到键 {k}")
        oldv = float(found.group(2))
        new_block = pk.sub(lambda mm: f'{mm.group(1)}{newv:.6f}', new_block, count=1)
        CHANGES.append(f"{label}:{pat}[{k}] {oldv} → {newv}")
    return src[:i] + new_block + src[end + 1:]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    # ---------- 1) factor_weights.json ----------
    fw_path = os.path.join(HERE, "factor_weights.json")
    cfg = json.load(open(fw_path, encoding="utf-8"))
    old_total = sum(float(v or 0) for v in (cfg.get("weights") or {}).values())
    for k, v in NEW_W.items():
        cfg["weights"][k] = v
        cfg["prior_weights"][k] = v
        CHANGES.append(f"factor_weights.json: weights/prior_weights[{k}] "
                       f"{OLD_W[k]} → {v}")
    new_total = sum(float(v or 0) for v in cfg["weights"].values())
    assert abs(new_total - 1.0) < 1e-9, f"Σw={new_total} ≠ 1.0"

    cfg["version"] = NEW_VERSION
    cfg["fitted_at"] = "2026-11-02 20:25:00"
    cfg["method"] = (
        "v4.17-cycle-rules：2026-11-02 round108「指令D 落地」——老大三条指令全采纳。"
        "①P0/P1 修订：C1 growth 纳入「业绩动量」大类（cap .30，补 round105 治理缺口）；"
        "C2 加 accel 与 P1g 去重互斥，采纳 P1g（D3 路A，growth 合成分改纯 rank_Gq，"
        "QoQ 一阶导不再重复计量）；C3 防呆改数据驱动（规则1/2 取代拍脑袋阈值）；C4 panel 待补 score_growth。"
        "②周期标签：新建 sector/cycle_industry.json 单一来源（20 个申万二级，含光伏），"
        "正面清单 gate，成长/消费/医药天然豁免。"
        "③两条规则（仅强周期生效）：规则1 盈利持续性=毛利率水平抬升>3pp 扣成长分 10%"
        "（原方案「毛利率波动 std>行业p60」实测 lift 0.945<1 方向反了，已换口径）；"
        "规则2 二阶导可持续性=按期去均值 ΔG_q≥周期组 p80 扣 15%；叠加上限 25%。"
        "权重变动：growth .05→.10（业绩动量通道加倍，否则规则扣分只传导 −0.12 分形同虚设）、"
        "rsi_rev .10→.05（反转簇让出）；Σ=1.0 不变。"
        "门禁：G1~G4 沿用 v4.16.1 结论、G5 仍未过（1/6 折），panel_round66 无 score_growth 列 → "
        "growth 维仍是门禁盲区，权重为产品裁决非 IC 背书。灰度性质存续——禁止表述为『经验证的 alpha』。"
    )
    if "notes" not in cfg:
        cfg["notes"] = []
    cfg["notes"].insert(0,
        "【v4.17-cycle-rules】2026-11-02 round108 指令D 落地：growth .05→.10 / rsi_rev .10→.05；"
        "新增「业绩动量」大类（cap .30）；growth 合成分 P1g 去重（纯 rank_Gq）；"
        "新增周期标签单一来源 sector/cycle_industry.json（20 个申万二级，含光伏）；"
        "新增规则1（毛利率抬升>3pp 扣 10%）+规则2（去均值 ΔG_q≥p80 扣 15%，上限 25%），"
        "仅强周期股生效。⚠️ 规则1 原方案「毛利率波动 std>行业p60」实测 lift 0.945 方向反了已换口径；"
        "⚠️ 相同阈值下周期/非周期次期回落幅度几乎相同 → 门控合法性来自 autocorr 结构差异"
        "（去均值后周期 −0.55 vs 非周期 −0.02），禁宣称『只有周期股会回落』。"
    )
    # C1：新增业绩动量大类
    wc = cfg.get("weight_constraints") or {}
    groups = wc.get("groups") or {}
    if "业绩动量" not in groups:
        groups[NEW_GROUP[0]] = NEW_GROUP[1]
        wc["groups"] = groups
        cfg["weight_constraints"] = wc
        CHANGES.append("factor_weights.json: weight_constraints.groups 新增 "
                       "「业绩动量」: [score_growth]（cap 0.30）")

    if not args.dry_run:
        json.dump(cfg, open(fw_path, "w", encoding="utf-8"),
                  ensure_ascii=False, indent=2)

    # ---------- 2) 五个 py 的硬编码先验 ----------
    TARGETS = [("factor_eval.py", "PRIOR_W10"),
               ("step2_stockscorer_v2.py", "_W_PRIOR_10D"),
               ("apply_sector_momentum.py", "_W_PRIOR_10D"),
               ("enhance_scores.py", "_W_PRIOR_10D"),
               ("gen_report_top200.py", "_fb"),
               # round108 追加：_rf_W_ACT 与 _fb 同文件但**是两个独立入口**
               #   _fb=权重条展示源；_rf_W_ACT=证据缺口名义权重合计计算源。
               #   漏改它会静默把「无 IC 证据的名义权重合计」算成旧口径（实测漏过一轮）。
               ("gen_report_top200.py", "_rf_W_ACT")]
    for fn, pat in TARGETS:
        p = os.path.join(HERE, fn)
        src = open(p, encoding="utf-8").read()
        src2 = set_dict_values(src, pat, NEW_W, fn)
        if src2 != src and not args.dry_run:
            open(p, "w", encoding="utf-8").write(src2)

    print("=" * 76)
    for c in CHANGES:
        print("  " + c)
    print("=" * 76)
    print(f"  Σw: {old_total:.4f} → {new_total:.4f}  (目标 1.0)")
    print(f"  version → {NEW_VERSION}")
    if args.dry_run:
        print("\n[DRY-RUN] 未写盘")
    else:
        print(f"\n[OK] 已写入 {len(TARGETS)+2} 处（json 两入口 + {len(TARGETS)} 处 py 硬编码）")


if __name__ == "__main__":
    main()
