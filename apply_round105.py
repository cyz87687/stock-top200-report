#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
apply_round105.py —— 落地 v4.16.1-gray-growth 权重（round105 P0-2，2026-10-31）

【变更】score_growth 0→0.05（方式B合成分=业绩动量通道），score_tech_neg 0.15→0.10
  （「近乎常数」注记维让渡）。Σ=1.0；单维≤0.20；五大类≤0.30 全部保持。
  score_growth 不属于任何大类（factor_weights.json groups 现状）→ 不破大类上限。

【证据链如实留痕】
  门禁 weight_gate_result_v4161.json：G1✓ G2✓ G3✓ G4✓ G5✗(1/6) → verdict FAIL。
  ⚠️ panel_round66 面板无 score_growth 列（9 维：fund/bias60/rsi_rev/tech_neg/
  lowvol20/ep/size_ind/quality/size_growth+dominant）→ 门禁对该维是盲区，
  其裁决实际评估的是 tech_neg 变化后的 8 维归一复合（日志可见 0.2→0.2105 归一）。
  growth .05 是**产品裁决权重**（老大指令「业绩动量进总分」），非 IC 证据背书。
  灰度性质同 v4.16：禁宣称『经验证的 alpha』。

【纪律】与 apply_v416_gray 同构：factor_weights.json 权威源 + 七处硬编码同步 +
  verify_weights_round36.py PASS 才出图 + 禁 git commit/push。
"""
import json
import os
import re
import shutil
import argparse
import time

HERE = os.path.dirname(os.path.abspath(__file__))
WJ = os.path.join(HERE, "factor_weights.json")
BACKUP_SUFFIX = ".bak_v4161_before"

DIM_CAP = 0.20
GROUP_CAP = 0.30
GROUPS = {
    "基本面": ["score_fund", "score_quality"],
    "估值": ["score_ep"],
    "低波": ["score_lowvol20"],
    "技术反转": ["score_bias60", "score_rsi_rev", "score_tech_neg"],
    "市值": ["score_size_growth", "score_dominant"],
}
KEY_ORDER = [
    "score_fund", "score_news", "score_theme_neg", "score_size", "score_sector",
    "score_quality", "score_growth", "score_tech_neg", "score_oversold60",
    "score_bias60", "score_rsi_rev", "score_vol_pulse", "score_overbought",
    "score_theme_intrinsic", "score_attention", "score_lowvol20", "score_ep",
    "score_size_ind", "score_size_growth", "score_dominant",
]
# 披露⑬ 当前实际权重：9 生效维（lowvol20 归零退役，growth 首次进总分）
RF_ACT_KEYS = ["score_fund", "score_quality", "score_growth", "score_ep",
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


def build_literal(w, varname):
    parts = [f'"{k}": {w.get(k, 0.0):.6f}' for k in KEY_ORDER]
    return f"{varname} = {{{', '.join(parts)}}}"


def sync_hardcoded(full):
    print("\n  同步兜底硬编码（7 处）：")
    targets = [("enhance_scores.py", "_W_PRIOR_10D"),
               ("factor_eval.py", "PRIOR_W10"),
               ("apply_sector_momentum.py", "_W_PRIOR_10D"),
               ("step2_stockscorer_v2.py", "_W_PRIOR_10D")]
    for fn, varname in targets:
        p = os.path.join(HERE, fn)
        src = open(p, encoding="utf-8").read()
        m = re.search(rf"^{varname}\s*=\s*\{{.*?\}}", src, re.S | re.M)
        if not m:
            print(f"     ⚠️ {fn}: 未定位到 {varname}")
            continue
        src2 = src.replace(m.group(0), build_literal(full, varname), 1)
        if src2 == src:
            print(f"     ⚠️ {fn}: 替换未生效")
            continue
        shutil.copy2(p, p + BACKUP_SUFFIX)
        open(p, "w", encoding="utf-8").write(src2)
        print(f"     ✅ {fn} 的 {varname}")

    gp = os.path.join(HERE, "gen_report_top200.py")
    src = open(gp, encoding="utf-8").read()
    m = re.search(r"_fb = \{\"score_fund\":.*?\}", src, re.S)
    if m:
        new_blk = ("_fb = " + "{" + ", ".join(f'"{k}": {full[k]:.6f}' for k in KEY_ORDER) + "}")
        src = src.replace(m.group(0), new_blk, 1)
        print("     ✅ gen_report_top200.py 的 _fb")
    else:
        print("     ⚠️ gen_report_top200.py: 未定位到 _fb")
    m = re.search(r"_rf_W_ACT = \{.*?\}", src, re.S)
    if m:
        new_blk = ("_rf_W_ACT = {" + ", ".join(
            f'"{k}": {full[k]:.6f}' for k in RF_ACT_KEYS) + "}")
        src = src.replace(m.group(0), new_blk, 1)
        print("     ✅ gen_report_top200.py 的 _rf_W_ACT（披露⑬，9 生效维含 growth）")
    else:
        print("     ⚠️ gen_report_top200.py: 未定位到 _rf_W_ACT")
    shutil.copy2(gp, gp + BACKUP_SUFFIX)
    open(gp, "w", encoding="utf-8").write(src)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--weights", default=os.path.join(HERE, "candidate_v4161_growth.json"))
    ap.add_argument("--gate-json", default=os.path.join(HERE, "weight_gate_result_v4161.json"))
    ap.add_argument("--scored", default=os.path.join(HERE, "top200_scored_2026-10-30.json"))
    ap.add_argument("--version", default="v4.16.1-gray-growth")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    cand = json.load(open(args.weights, encoding="utf-8"))
    all_w = {k: float(v) for k, v in (cand.get("weights") or {}).items()}
    new9 = {k: v for k, v in all_w.items() if v > 0}
    print("═" * 92)
    print(f"  {args.version} 权重落地 + 评分重算（round105 P0-2）")
    print("═" * 92)
    print("  生效维:", json.dumps({k.replace('score_', ''): v for k, v in
                                sorted(new9.items(), key=lambda kv: -kv[1])},
                               ensure_ascii=False))

    viol = check(all_w)
    if viol:
        print("❌ 权重不合规，中止：")
        for v in viol:
            print("   ", v)
        return 1
    print(f"  ✅ 约束合规：Σ={sum(all_w.values()):.6f} 单维≤{DIM_CAP} 大类≤{GROUP_CAP}")
    for g, ms in GROUPS.items():
        print(f"     {g:8s} {sum(new9.get(m, 0.0) for m in ms):.4f}")

    gate_note = "门禁未提供"
    if args.gate_json and os.path.exists(args.gate_json):
        gj = json.load(open(args.gate_json, encoding="utf-8"))
        gv = gj.get("verdict")
        gates = gj.get("gates") or {}
        gate_note = (f"门禁裁决={gv}（G1={gates.get('g1_monotonic_robust')}/"
                     f"G2={gates.get('g2_spread_positive')}/G3={gates.get('g3_beat_ew')}/"
                     f"G4={gates.get('g4_beat_single')}/G5={gates.get('g5_wf_pass_rate')}）")
        print(f"  门禁: {gate_note}")
        print("  ⚠️ panel 无 score_growth 列 → 门禁对该维盲区，growth .05 为产品裁决权重")

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
    d["fitted_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    d["weights"] = {k: all_w.get(k, 0.0) for k in all_keys}
    d["prior_weights"] = {k: all_w.get(k, 0.0) for k in (d.get("prior_weights") or all_keys)}
    d["method"] = (
        f"{args.version}：2026-10-31 round105 P0-2「业绩动量进总分」。"
        f"score_growth 0→0.05（方式B·单季扣非 G_q/QoQ 合成分，产品裁决=业绩变脸响应通道；"
        f"round105 实锤旧 total 对业绩变脸零响应），score_tech_neg 0.15→0.10（近乎常数注记维让渡）。"
        f"{gate_note}。⚠️ 面板 panel_round66 无 score_growth 列——门禁对该维是盲区，"
        f"其判定仅覆盖 tech_neg 变化后的 8 维归一复合；growth .05 非 IC 证据背书。"
        f"灰度性质同 v4.16（G5 样本量缺口未闭合，P0 池前视重建仍开放）——"
        f"禁止表述为『经验证的 alpha』。v4.15 约束沿用：单因子≤0.20/5 大类各≤0.30/Σ=1。")
    d.setdefault("notes", [])
    d["notes"] = [n for n in d["notes"] if args.version not in str(n)]
    d["notes"].insert(0, f"【{args.version}】" + d["method"])
    with open(WJ, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=2)
    print(f"\n  ✅ factor_weights.json → {args.version}")
    for k in sorted(set(list(old_w) + list(new9)), key=lambda k: -new9.get(k, 0)):
        o, n = old_w.get(k, 0.0), new9.get(k, 0.0)
        if abs(o - n) > 1e-9:
            print(f"       {k.replace('score_',''):16s} {o:.4f} → {n:.4f}  ({n-o:+.4f})")

    # ── 2. 七处硬编码同步 ──
    sync_hardcoded({k: all_w.get(k, 0.0) for k in KEY_ORDER})

    # ── 3. 重算评分预览（复用 enhance 单一口径）──
    print("\n  重算评分（enhance_scores._calc_total_10d + _rate_100）：")
    import sys
    sys.path.insert(0, HERE)
    import enhance_scores as ES
    import statistics as sta

    data = json.load(open(args.scored, encoding="utf-8"))
    results = data["results"]
    old_tot = {r["code"]: r.get("total") for r in results}
    old_rate = {r["code"]: r.get("rating") for r in results}
    for r in results:
        t = ES._calc_total_10d(r)
        r["total"] = t
        r["rating"], r["advice"] = ES._rate_100(t)
    results.sort(key=lambda x: (-x["total"], -(x.get("turnover") or 0)))
    from collections import Counter
    rc = Counter(r["rating"] for r in results)
    data["results"] = results
    data["model"] = (f"stock-scorer {args.version} (9-dim + AI + sector-momentum[evidence], "
                     f"gray-release: {gate_note})")
    data["weights"] = d["weights"]
    data["weights_note"] = (
        f"权重版本 {args.version}（{d['fitted_at']} 重算）；total 跨权重版本**不可比**"
        f"（scale_calc 统一量尺未变、权重刻度变了）。")
    data.setdefault("stats", {})
    data["stats"]["rating_dist"] = {k: rc.get(k, 0) for k in "SABCD"}
    with open(args.scored, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

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
    rate_chg = sum(1 for r in results if old_rate.get(r["code"]) != r["rating"])
    new_tot = [r["total"] for r in results]
    print(f"  ρ(total 新旧) = {rho:.4f}   n={len(pairs)}")
    print(f"  Top50 重合 = {len(top50_old & top50_new)}/50")
    print(f"  评级变动 = {rate_chg} 只 | 新分布 S:{rc.get('S',0)} A:{rc.get('A',0)} "
          f"B:{rc.get('B',0)} C:{rc.get('C',0)} D:{rc.get('D',0)}")
    print(f"  total: min {min(new_tot):.2f} / 中位 {sta.median(new_tot):.2f} / "
          f"max {max(new_tot):.2f} | ≥75: {sum(1 for t in new_tot if t >= 75)} 只")
    print("\n  ✅ 落地完成。必须再跑 verify_weights_round36.py（PASS 才出图）。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
