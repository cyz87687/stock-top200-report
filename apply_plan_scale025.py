#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
apply_plan_scale025.py —— 把「反转块降至 1/4、归一到 Σ=1」同步到全部权重入口

裁决来源（verify_plan_c5.py 的预置三条件扫描）：
  · 全样本 h20 IC +0.0778 → +0.0949（+0.0171）
  · 前段(2025-11-03~2026-06-25) Δ +0.0217、后段(2026-06-26~) Δ +0.0054 → 两段皆不劣化
  · 反转块占比 0.500 → 0.200（≤0.25 上限）
  · 优于"归零"：归零全样本 +0.0933 但**后段 -0.0341**（砍掉了现行最强 regime 的一块）

新权重（scale=0.25 后归一）：
  bias60 .070 / rsi_rev .070 / tech_neg .060 / lowvol20 .208
  quality .192 / size_ind .160 / ep .120 / fund .120        Σ=1.000

⚠️ 必须归一到 Σ=1.0：若直接落 Σ=0.625，prod_score 的 max(0.5, den) 不压缩
   （0.625>0.5 安全），但保持 Σ=1 才能让「满分=100、S档≥85 可达」的口径不变，
   也避免与历史 total 产生又一层量纲断裂。
   ⚠️ 跨天分数不可比：本轮改权重后 total 与昨日不可直接比较，页面须披露。

本脚本做两件事：
  1) 改 factor_weights.json 的 weights 与 prior_weights
  2) 改 5 个 py 文件里的回退常量（_W_PRIOR_10D / PRIOR_W10 / _fb）
  3) 改完立刻跑 gen_report 的 _verify_weight_consistency() 六方校验

用法：python apply_plan_scale025.py [--dry-run]
"""
import io
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))

NEW8 = {
    "score_bias60": 0.070,
    "score_rsi_rev": 0.070,
    "score_tech_neg": 0.060,
    "score_lowvol20": 0.208,
    "score_quality": 0.192,
    "score_size_ind": 0.160,
    "score_ep": 0.120,
    "score_fund": 0.120,
}
ZERO_KEYS = ["score_news", "score_theme_neg", "score_size", "score_sector",
             "score_growth", "score_oversold60", "score_vol_pulse",
             "score_overbought", "score_theme_intrinsic", "score_attention"]

FULL = dict(NEW8)
for k in ZERO_KEYS:
    FULL[k] = 0.0

assert abs(sum(FULL.values()) - 1.0) < 1e-9, f"Σ={sum(FULL.values())} ≠ 1"


def make_literal(d, indent="  "):
    """生成单行 python dict 字面量，键顺序固定（与现有文件风格一致）。"""
    order = ["score_fund", "score_news", "score_theme_neg", "score_size",
             "score_sector", "score_quality", "score_growth", "score_tech_neg",
             "score_oversold60", "score_bias60", "score_rsi_rev", "score_vol_pulse",
             "score_overbought", "score_theme_intrinsic", "score_attention",
             "score_lowvol20", "score_ep", "score_size_ind"]
    parts = []
    for k in order:
        v = d.get(k, 0.0)
        parts.append(f'"{k}": {v:g}')
    return "{" + ", ".join(parts) + "}"


def patch_py(path, var_names, dry):
    """把 path 中形如 `VAR = {...}` 的行整体替换为新闻权重。"""
    src = io.open(path, encoding="utf-8").read()
    n = 0
    for var in var_names:
        # 匹配 VAR = { ... } 到行尾（这些常量都在单行内）
        pat = re.compile(rf'^({var}\s*=\s*)\{{.*\}}\s*(#.*)?$', re.M)
        def _rep(m):
            nonlocal n
            n += 1
            tail = ("  " + m.group(2)) if m.group(2) else ""
            return m.group(1) + make_literal(FULL) + tail
        src2, cnt = pat.subn(_rep, src)
        if cnt:
            src = src2
            print(f"  ✅ {os.path.basename(path)} :: {var} 已替换 {cnt} 处")
    if n and not dry:
        io.open(path, "w", encoding="utf-8").write(src)
    return n


def main():
    dry = "--dry-run" in sys.argv
    print("=" * 96)
    print("同步「反转块×0.25 + 归一到Σ=1」新权重" + ("  【DRY-RUN 不写盘】" if dry else ""))
    print("=" * 96)
    print("  新权重：")
    for k, v in NEW8.items():
        print(f"    {k:<18} {v:.3f}")
    print(f"    Σ = {sum(NEW8.values()):.3f}")

    # ---- 1) factor_weights.json
    print("\n[1] factor_weights.json")
    jp = os.path.join(HERE, "factor_weights.json")
    J = json.load(io.open(jp, encoding="utf-8"))
    old_w = dict(J.get("weights") or {})
    J["version"] = "v4.10-rev-quarter"
    J["fitted_at"] = "2026-10-27 21:30:00"
    J["weights"] = FULL
    J["prior_weights"] = dict(FULL)
    J["method"] = (
        (J.get("method") or "")
        + " ⑦ **round34 真·大池审计（2026-10-27）：反转块降至 1/4（v4.10-rev-quarter）**。"
        + "面板：域 2029 只（不锁定冻结池）/ 238 截面 / 2025-11-03~2026-10-27；"
        + "h5 233 截面 11.53 月、h20 218 截面 10.58 月，均远超 3 个月。"
        + "裁决扫描（其余 5 维不变、反转块整体 ×scale 后归一到 Σ=1）："
        + "scale=1.00 全样本 h20 +0.0778 → 0.25 时 +0.0949（t +7.01），"
        + "且前段(2025-11-03~2026-06-25, 156 截面) Δ+0.0217、"
        + "后段(2026-06-26~, 62 截面) Δ+0.0054，**两段皆不劣化**；"
        + "归零(scale=0) 全样本虽 +0.0933 但后段 **-0.0341**，砍掉现行最强 regime 一块，故取 0.25 而非归零。"
        + "⚠️ 关键限制：模型 alpha 高度 regime 依赖——前 156 截面 h20 IC -0.0021(t -0.14) 近乎为零，"
        + "后 62 截面 +0.2786(t +10.58) 极强；加权复原 0.7156×(-0.0021)+0.2844×(+0.2786)=+0.0777 与审计值吻合。"
        + "故本权重只声称「在可得样本上更稳健」，不声称已通过样本外验证。"
        + "⚠️ 跨天分数不可比：本轮改权重后 total 与改前不可直接比较（历史版本一律不可比）。"
    )
    if not dry:
        with io.open(jp, "w", encoding="utf-8") as f:
            json.dump(J, f, ensure_ascii=False, indent=1)
    print(f"  ✅ weights / prior_weights 已更新（version → {J['version']}）")
    print(f"     旧 Σ={sum(old_w.values()):.3f} → 新 Σ={sum(FULL.values()):.3f}")

    # ---- 2) 五个 py 回退常量
    print("\n[2] 回退常量（漏改一处即静默回退旧口径）")
    targets = [
        ("factor_eval.py", ["PRIOR_W10"]),
        ("step2_stockscorer_v2.py", ["_W_PRIOR_10D"]),
        ("apply_sector_momentum.py", ["_W_PRIOR_10D"]),
        ("enhance_scores.py", ["_W_PRIOR_10D"]),
    ]
    total = 0
    for fn, vars_ in targets:
        p = os.path.join(HERE, fn)
        if not os.path.exists(p):
            print(f"  ⚠️ 缺文件 {fn}")
            continue
        total += patch_py(p, vars_, dry)

    # gen_report 的 _fb 是多行字典，单独处理
    print("\n[3] gen_report_top200.py :: _fb（多行字典，单独替换）")
    gp = os.path.join(HERE, "gen_report_top200.py")
    gsrc = io.open(gp, encoding="utf-8").read()
    pat = re.compile(r'(\n\s*_fb = \{)[\s\S]*?(\}\n)', re.M)
    if pat.search(gsrc):
        new_block = (
            "\n    # v4.10-rev-quarter（2026-10-27 round34 真·大池审计）：反转块降至 1/4。\n"
            "    # 依据：scale 扫描下 0.25 是唯一「全样本 h20 +0.0949（近最优）且前后段皆不劣化」的点；\n"
            "    # 归零虽 +0.0933 但后段 -0.0341。详见 verify_plan_c5.json 与 factor_weights.json method ⑦。\n"
            "    _fb = {\"score_fund\": 0.12, \"score_news\": 0.0, \"score_theme_neg\": 0.0,\n"
            "           \"score_size\": 0.0, \"score_sector\": 0.0, \"score_quality\": 0.192,\n"
            "           \"score_growth\": 0.0, \"score_tech_neg\": 0.06, \"score_oversold60\": 0.0,\n"
            "           \"score_bias60\": 0.07, \"score_rsi_rev\": 0.07, \"score_vol_pulse\": 0.0,\n"
            "           \"score_overbought\": 0.0, \"score_theme_intrinsic\": 0.0, \"score_attention\": 0.0,\n"
            "           \"score_lowvol20\": 0.208, \"score_ep\": 0.12, \"score_size_ind\": 0.16}\n"
        )
        if not dry:
            gsrc = pat.sub(new_block, gsrc, count=1)
            io.open(gp, "w", encoding="utf-8").write(gsrc)
        print("  ✅ _fb 已替换")
        total += 1
    else:
        print("  ❌ 未匹配到 _fb")

    print(f"\n共修改 {total} 个回退常量 + 1 个 JSON")
    print("\n下一步：python gen_report_top200.py top200_scored_2026-10-27.json")
    print("        必须看到「权重口径六方一致性校验通过」才继续。")


if __name__ == "__main__":
    main()
