#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
apply_v416_gray.py —— 落地 v4.16 灰度权重并重算评分（2026-10-31 老大指令「按方案改造，重新计算评分」）

【方案依据】
  R103 §5.4/§6 裁决路径：闭合 round102（细网格 + quality 邻域含 0 稳定解）
  → 过 weight_admission_gate 五道门禁 → 灰度上线 v4.16 → 重算评分。
  候选来源：audit_round104_fine_resolve.py（step=0.05、quality≤0.10、重建池域）。

【变更纪律（与 apply_v415 同构）】
  ① 主路径 = factor_weights.json（唯一权威源）
  ② 兜底硬编码 7 处同步：enhance_scores / factor_eval / apply_sector_momentum /
     step2_stockscorer_v2 / gen_report._fb / gen_report._rf_W_ACT（披露⑬当前实际权重）
  ③ 重算评分复用 enhance_scores._calc_total_10d + _rate_100（单一口径，禁再实现）
  ④ 改完必跑 verify_weights_round36.py（六方一致性 + 约束校验）
  ⑤ 禁 git commit / push

用法：
  python apply_v416_gray.py --weights candidate_v416_gray.json \\
      --gate-json weight_gate_result_v416.json --scored top200_scored_2026-10-30.json
  python apply_v416_gray.py ... --dry-run    # 只校验不落盘
"""
import json
import os
import re
import shutil
import argparse

HERE = os.path.dirname(os.path.abspath(__file__))
WJ = os.path.join(HERE, "factor_weights.json")
BACKUP_SUFFIX = ".bak_v415_before"

# 与 factor_weights.json 的 weight_constraints 一致（v4.15 治理约束，v4.16 继续遵守）
DIM_CAP = 0.20
GROUP_CAP = 0.30
GROUPS = {
    "基本面": ["score_fund", "score_quality"],
    "估值": ["score_ep"],
    "低波": ["score_lowvol20"],
    "技术反转": ["score_bias60", "score_rsi_rev", "score_tech_neg"],
    "市值": ["score_size_growth", "score_dominant"],
}
# 兜底硬编码 dict 的完整键序（与既有 5 处字面量一致）
KEY_ORDER = [
    "score_fund", "score_news", "score_theme_neg", "score_size", "score_sector",
    "score_quality", "score_growth", "score_tech_neg", "score_oversold60",
    "score_bias60", "score_rsi_rev", "score_vol_pulse", "score_overbought",
    "score_theme_intrinsic", "score_attention", "score_lowvol20", "score_ep",
    "score_size_ind", "score_size_growth", "score_dominant",
]
# gen_report._rf_W_ACT（披露⑬ 当前实际权重，9 生效维）
RF_ACT_KEYS = ["score_fund", "score_lowvol20", "score_quality", "score_ep",
               "score_size_growth", "score_bias60", "score_rsi_rev",
               "score_dominant", "score_tech_neg"]


def check(w):
    v = []
    for k, x in w.items():
        if x > DIM_CAP + 1e-9:
            v.append(f"单因子超限 {k}={x}")
        if x < -1e-9:
            v.append(f"负权重 {k}={x}")
    for g, ms in GROUPS.items():
        s = sum(w.get(m, 0.0) for m in ms)
        if s > GROUP_CAP + 1e-9:
            v.append(f"大类超限 {g}={s:.4f}")
    t = sum(w.values())
    if abs(t - 1.0) > 1e-6:
        v.append(f"Σ={t:.8f}≠1")
    return v


def build_literal(w, varname, indent="    "):
    """重建 20 键 dict 字面量（数值 %.6f 保证 round-trip 与 json 精确一致）。"""
    parts = [f'"{k}": {w.get(k, 0.0):.6f}' for k in KEY_ORDER]
    body = ", ".join(parts)
    return f"{varname} = {{{body}}}"


def sync_hardcoded(new9, gate_json):
    print("\n  同步兜底硬编码（7 处）：")
    # ① 四处单行 dict：enhance / factor_eval / apply_sector_momentum / step2
    targets = [("enhance_scores.py", "_W_PRIOR_10D"),
               ("factor_eval.py", "PRIOR_W10"),
               ("apply_sector_momentum.py", "_W_PRIOR_10D"),
               ("step2_stockscorer_v2.py", "_W_PRIOR_10D")]
    full = {k: float(new9.get(k, 0.0)) for k in KEY_ORDER}
    for fn, varname in targets:
        p = os.path.join(HERE, fn)
        src = open(p, encoding="utf-8").read()
        m = re.search(rf"^{varname}\s*=\s*\{{.*?\}}", src, re.S | re.M)
        if not m:
            print(f"     ⚠️ {fn}: 未定位到 {varname}")
            continue
        new_block = build_literal(full, varname)
        src2 = src.replace(m.group(0), new_block, 1)
        if src2 == src:
            print(f"     ⚠️ {fn}: 替换未生效")
            continue
        shutil.copy2(p, p + BACKUP_SUFFIX)
        open(p, "w", encoding="utf-8").write(src2)
        print(f"     ✅ {fn} 的 {varname}")

    # ② gen_report._fb（多行）
    gp = os.path.join(HERE, "gen_report_top200.py")
    src = open(gp, encoding="utf-8").read()
    m = re.search(r"_fb = \{\"score_fund\":.*?\}", src, re.S)
    if m:
        new_blk = ("_fb = " + "{" + ", ".join(f'"{k}": {full[k]:.6f}' for k in KEY_ORDER) + "}")
        src = src.replace(m.group(0), new_blk, 1)
        print("     ✅ gen_report_top200.py 的 _fb")
    else:
        print("     ⚠️ gen_report_top200.py: 未定位到 _fb")

    # ③ gen_report._rf_W_ACT（披露⑬ 当前实际权重 —— 预存在漂移一并修复）
    m = re.search(r"_rf_W_ACT = \{.*?\}", src, re.S)
    if m:
        new_blk = ("_rf_W_ACT = {" + ", ".join(
            f'"{k}": {full[k]:.6f}' for k in RF_ACT_KEYS) + "}")
        src = src.replace(m.group(0), new_blk, 1)
        print("     ✅ gen_report_top200.py 的 _rf_W_ACT（披露⑬，顺带修复 v4.14 残留漂移）")
    else:
        print("     ⚠️ gen_report_top200.py: 未定位到 _rf_W_ACT")
    shutil.copy2(gp, gp + BACKUP_SUFFIX)
    open(gp, "w", encoding="utf-8").write(src)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--weights", default=os.path.join(HERE, "candidate_v416_gray.json"))
    ap.add_argument("--gate-json", default=None, help="weight_admission_gate 输出（嵌入 method 留痕）")
    ap.add_argument("--scored", default=os.path.join(HERE, "top200_scored_2026-10-30.json"))
    ap.add_argument("--version", default="v4.16-gray-icir")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    cand = json.load(open(args.weights, encoding="utf-8"))
    new9 = {k: float(v) for k, v in (cand.get("weights") or {}).items() if float(v) > 0}
    print("═" * 92)
    print("  v4.16 灰度权重落地 + 评分重算")
    print("═" * 92)
    print("  候选:", json.dumps({k.replace('score_', ''): v for k, v in
                              sorted(new9.items(), key=lambda kv: -kv[1])},
                             ensure_ascii=False))

    viol = check({k: float(v) for k, v in cand.get("weights", {}).items()})
    if viol:
        print("❌ 权重不合规，中止：")
        for v in viol:
            print("   ", v)
        return 1
    print(f"  ✅ 约束合规：Σ={sum(cand.get('weights', {}).values()):.6f}  "
          f"单维≤{DIM_CAP}  大类≤{GROUP_CAP}")
    for g, ms in GROUPS.items():
        s = sum(new9.get(m, 0.0) for m in ms)
        print(f"     {g:8s} {s:.4f}")

    gate_note = ""
    if args.gate_json and os.path.exists(args.gate_json):
        gj = json.load(open(args.gate_json, encoding="utf-8"))
        gv = gj.get("verdict")
        gates = gj.get("gates") or {}
        gate_note = (f"门禁裁决={gv}（G1={gates.get('g1_monotonic_robust')}/"
                     f"G2={gates.get('g2_spread_positive')}/G3={gates.get('g3_beat_ew')}/"
                     f"G4={gates.get('g4_beat_single')}/G5={gates.get('g5_wf_pass_rate')}）")
        print(f"  门禁: {gate_note}")
    else:
        gate_note = "门禁未跑/未提供（⚠️ 候选状态灰度）"
        print(f"  ⚠️ {gate_note}")

    if args.dry_run:
        print("  --dry-run：不落盘")
        return 0

    # ── 1. factor_weights.json（权威源）──
    d = json.load(open(WJ, encoding="utf-8"))
    old_w = {k: v for k, v in (d.get("weights") or {}).items()
             if isinstance(v, (int, float)) and v > 0}
    shutil.copy2(WJ, WJ + BACKUP_SUFFIX)
    all_keys = list((d.get("weights") or {}).keys())
    d["version"] = args.version
    d["fitted_at"] = __import__("time").strftime("%Y-%m-%d %H:%M:%S")
    d["weights"] = {k: float(new9.get(k, 0.0)) for k in all_keys}
    d["prior_weights"] = {k: float(new9.get(k, 0.0)) for k in (d.get("prior_weights") or all_keys)}
    d["method"] = (
        f"{args.version}：2026-10-31 老大指令「按方案改造，重新计算评分」。"
        f"依据 R103 §5.4/§6 裁决路径——audit_round104 细网格约束内重解"
        f"（step=0.05、quality≤0.10 邻域含 0、重建池域 pit_pool median 260 只）+ "
        f"round92 RSI/乖离有效性核查（rsi_rev 近窗 IC 为负、bias60 弱而不显著）"
        f"选取 quality 邻域稳定解。{gate_note}。"
        f"v4.15 约束沿用：单因子≤0.20 / 5 大类各≤0.30 / Σ=1。"
        f"⚠️ 灰度状态：G5 样本量缺口未闭合（仅 219 截面），P0（池前视重建严格重算 α）"
        f"仍开放——禁止表述为『经验证的 alpha』。")
    d.setdefault("notes", [])
    d["notes"] = [n for n in d["notes"] if args.version not in str(n)]
    d["notes"].insert(0, f"【{args.version}】" + d["method"])
    d["weight_constraints"] = {
        "dim_cap": DIM_CAP, "group_cap": GROUP_CAP, "dim_floor": 0.0,
        "groups": GROUPS,
        "enforcement": "verify_weights_round36.py 必须逐项校验，FAIL 则不出图",
    }
    with open(WJ, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=2)
    print(f"\n  ✅ factor_weights.json → {args.version}")
    print("     变更明细（生效维）：")
    for k in sorted(set(list(old_w) + list(new9)), key=lambda k: -new9.get(k, 0)):
        o, n = old_w.get(k, 0.0), new9.get(k, 0.0)
        if abs(o - n) > 1e-9:
            print(f"       {k.replace('score_',''):16s} {o:.4f} → {n:.4f}  ({n-o:+.4f})")

    # ── 2. 七处硬编码同步 ──
    sync_hardcoded(new9, args.gate_json)

    # ── 3. 重算评分（复用 enhance 单一口径）──
    print("\n  重算评分（enhance_scores._calc_total_10d + _rate_100）：")
    import sys
    sys.path.insert(0, HERE)
    import enhance_scores as ES
    import statistics as sta

    data = json.load(open(args.scored, encoding="utf-8"))
    results = data["results"]
    old_tot = {r["code"]: r.get("total") for r in results}
    old_rate = {}
    for r in results:
        old_rate[r["code"]] = r.get("rating")

    for r in results:
        t = ES._calc_total_10d(r)
        r["total"] = t
        r["rating"], r["advice"] = ES._rate_100(t)

    results.sort(key=lambda x: (-x["total"], -(x.get("turnover") or 0)))
    from collections import Counter
    rc = Counter(r["rating"] for r in results)
    sec_avg = {}
    for r in results:
        sec_avg.setdefault(r["sector"], []).append(r["total"])
    data["results"] = results
    data["model"] = (f"stock-scorer {args.version} (9-dim + AI + sector-momentum[evidence], "
                     f"gray-release: {gate_note})")
    data["weights"] = d["weights"]
    data["weights_note"] = (
        f"权重版本 {args.version}（{d['fitted_at']} 重算）；"
        f"total 与 v4.15 及更早版本**不可比**（scale_calc 统一量尺未变、权重刻度变了），"
        f"跨版本比较必须用同一把尺重算（scale_calc.py 模块头纪律）。")
    data["stats"]["rating_dist"] = {k: rc.get(k, 0) for k in "SABCD"}
    data["stats"]["sector_avg"] = {
        s: {"count": len(v), "avg": round(sum(v) / len(v), 1)}
        for s, v in sorted(sec_avg.items(), key=lambda x: -sum(x[1]) / len(x[1]))
    }
    with open(args.scored, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    # ── 4. 三道对照 ──
    new_tot = [r["total"] for r in results]
    pairs = [(old_tot[r["code"]], r["total"]) for r in results
             if isinstance(old_tot.get(r["code"]), (int, float))]
    import math
    def _rank(xs):
        order = sorted(range(len(xs)), key=lambda i: xs[i])
        rk = [0.0] * len(xs)
        for pos, i in enumerate(order):
            rk[i] = pos
        return rk
    rx, ry = _rank([p[0] for p in pairs]), _rank([p[1] for p in pairs])
    mx, my = sum(rx) / len(rx), sum(ry) / len(ry)
    num = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    den = math.sqrt(sum((a - mx) ** 2 for a in rx) * sum((b - my) ** 2 for b in ry))
    rho = num / den if den else None
    top50_old = set(sorted((c for c, t in old_tot.items() if isinstance(t, (int, float))),
                          key=lambda c: -old_tot[c])[:50])
    top50_new = set(r["code"] for r in results[:50])
    overlap = len(top50_old & top50_new)
    rate_chg = sum(1 for r in results if old_rate.get(r["code"]) != r["rating"])
    print(f"  ρ(total 新旧) = {rho:.4f}   n={len(pairs)}")
    print(f"  Top50 重合 = {overlap}/50")
    print(f"  评级变动 = {rate_chg} 只 | 新分布 S:{rc.get('S',0)} A:{rc.get('A',0)} "
          f"B:{rc.get('B',0)} C:{rc.get('C',0)} D:{rc.get('D',0)}")
    print(f"  total 分布: min {min(new_tot):.2f} / 中位 {sta.median(new_tot):.2f} / "
          f"max {max(new_tot):.2f} | ≥75: {sum(1 for t in new_tot if t >= 75)} 只")
    print("\n  ✅ 落地完成。下一步必须跑 verify_weights_round36.py（六方一致性+约束）。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
