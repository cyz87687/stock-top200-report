#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
audit_round112_transformer_verdict.py —— 对 Transformer 四指数结果做显著性检验并出最终结论

═══ 为什么必须有这一步 ═══

transformer_regime.py 四指数结果：
    中证全指 0.4957 < 0.5294   → 不如基线
    沪深300  0.5423 < 0.5466   → 不如基线
    中证500  0.4836 < 0.5363   → 不如基线
    中证1000 **0.5147 > 0.5060 → 优于基线**  ← 唯一「赢」的一个

如果只看最后一行，很容易写成「Transformer 在中证1000 上有效」。
但这必须回答两个问题：

  Q1 「+0.87pp」在 1158 个样本上显著吗？
     二项检验：H0 为 p = baseline_acc，观测到 p̂ = test_acc
     若 p 值 > 0.05 → **不能拒绝「等于基线」**，该「优于基线」是抽样噪声

  Q2 四个指数里挑出唯一赢的那个，是不是多重比较？
     4 次检验，至少一次假阳性的概率 ≈ 1 − 0.95⁴ = 18.5%
     没有任何先验理由只看中证1000 → 必须按 4 次检验做 Bonferroni 校正
     （α = 0.05/4 = 0.0125）

═══ 判据（先写死）═══
  · 单指数：二项检验 p < 0.05 且 test_acc > baseline  → 该指数「显著优于基线」
  · 全体  ：需至少 1 个指数通过 Bonferroni 校正后仍显著，才可称「模型有预测能力」
  · 否则  ：结论为「无预测能力」，页面必须如实标注，不得写「部分指数有效」

输出：audit_round112_transformer_verdict.json
"""
import os
import sys
import json
import math
import argparse

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

FILES = ["transformer_regime.json", "transformer_regime_torch.json"]


def norm_cdf(z):
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def binom_test_normal(k, n, p0):
    """正态近似的单侧二项检验（H1: p > p0）。返回 (z, p_one_sided)。

    n≈1158 时正态近似足够精确（np0=586 >> 10）。
    """
    if n <= 0:
        return None, None
    ph = k / n
    se = math.sqrt(p0 * (1 - p0) / n)
    if se == 0:
        return None, None
    z = (ph - p0) / se
    return z, 1.0 - norm_cdf(z)


def wilson_ci(k, n, z=1.96):
    """Wilson 置信区间（比 Wald 在小样本/极端比例下稳）。"""
    if n <= 0:
        return None, None
    ph = k / n
    d = 1 + z * z / n
    c = ph + z * z / (2 * n)
    s = z * math.sqrt(ph * (1 - ph) / n + z * z / (4 * n * n))
    return (c - s) / d, (c + s) / d


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--alpha", type=float, default=0.05)
    a = ap.parse_args()

    out = {"meta": {
        "purpose": "对 Transformer 测试期准确率做显著性检验，防止把抽样噪声当预测能力",
        "test": "单侧二项检验（正态近似），H0: p = baseline_acc，H1: p > baseline_acc",
        "multiple_comparison": (f"4 个指数各检验一次，Bonferroni 校正 α = "
                                f"{a.alpha}/4 = {a.alpha/4:.4f}"),
        "decision_rule": ("全部指数均未通过 Bonferroni 校正 → 结论「无预测能力」；"
                          "不得表述为「部分指数有效」"),
    }, "files": {}}

    for fn in FILES:
        p = os.path.join(HERE, fn)
        if not os.path.exists(p):
            print(f"⚠️ 跳过 {fn}（不存在）")
            continue
        d = json.load(open(p, encoding="utf-8"))
        rows = {}
        n_idx = 0
        n_beat = 0
        n_sig = 0
        print(f"\n{'='*74}\n{fn}")
        print(f"  {'指数':<10}{'test_acc':>9}{'baseline':>9}{'差(pp)':>8}"
              f"{'z':>7}{'p单侧':>9}{'95%CI下界':>10}  判定")
        for code, r in d.items():
            if code == "meta" or not isinstance(r, dict):
                continue
            if "test_acc" not in r:
                continue
            n_idx += 1
            n = int(r.get("n_test") or 0)
            ta = float(r["test_acc"]); ba = float(r["baseline_acc"])
            k = int(round(ta * n))
            z, pv = binom_test_normal(k, n, ba)
            lo, hi = wilson_ci(k, n)
            beat = ta > ba + 1e-9
            sig = bool(pv is not None and beat and pv < a.alpha)
            sig_bonf = bool(pv is not None and beat and pv < a.alpha / 4)
            if beat:
                n_beat += 1
            if sig:
                n_sig += 1
            verdict = ("显著优于基线" if sig_bonf
                       else ("名义优于但**不显著**" if beat else "不如基线"))
            print(f"  {r.get('name', code):<10}{ta:>9.4f}{ba:>9.4f}"
                  f"{(ta-ba)*100:>+8.2f}{(z if z is not None else 0):>7.2f}"
                  f"{(pv if pv is not None else 1):>9.3f}"
                  f"{(lo if lo else 0):>10.4f}  {verdict}")
            rows[code] = {
                "name": r.get("name"), "n_test": n,
                "test_acc": ta, "baseline_acc": ba,
                "diff_pp": round((ta - ba) * 100, 2),
                "z": round(z, 3) if z is not None else None,
                "p_one_sided": round(pv, 4) if pv is not None else None,
                "ci95_wilson": [round(lo, 4), round(hi, 4)] if lo else None,
                "nominal_beat": beat,
                "significant": sig,
                "significant_bonferroni": sig_bonf,
                "verdict": verdict,
            }

        out["files"][fn] = {
            "n_indices": n_idx, "n_nominal_beat": n_beat,
            "n_significant": n_sig, "results": rows,
        }
        print(f"  → {n_idx} 个指数中，名义上跑赢基线 {n_beat} 个，"
              f"统计显著 {n_sig} 个")

    # ---- 总判定 ----
    any_sig = any(v["n_significant"] > 0 for v in out["files"].values())
    # 两版实现是否结论一致（交叉验证）
    f1 = out["files"].get("transformer_regime.json", {}).get("results", {})
    f2 = out["files"].get("transformer_regime_torch.json", {}).get("results", {})
    cross = {}
    for code in set(f1) & set(f2):
        cross[code] = {
            "numpy_test_acc": f1[code]["test_acc"],
            "torch_test_acc": f2[code]["test_acc"],
            "both_below_baseline": (not f1[code]["nominal_beat"]
                                    and not f2[code]["nominal_beat"]),
        }

    out["final_verdict"] = {
        "any_significant": bool(any_sig),
        "statement": (
            "四指数测试期（2022-2026，1158 个样本）中，Transformer 在 3 个指数上"
            "不如「永远猜训练期最多数类」的基线；中证1000 名义上高 0.87pp，"
            "但二项检验 p 远大于 0.05，经 4 次检验 Bonferroni 校正后**不显著**。"
            "**结论：该 Transformer 对指数方向无可用预测能力**，"
            "与 round109/109b「方向不可预测」的结论一致，且这次是有模型实证的。"
            if not any_sig else
            "至少一个指数通过 Bonferroni 校正后显著优于基线，需进一步扩展样本复核"
            "（方法论⑫：小样本的通过必须扩样本复核再落地）。"),
        "cross_validation": (
            "numpy 手写版与官方 PyTorch 版在 000985 上均「不如基线」"
            f"（numpy {f1.get('000985', {}).get('test_acc')} / "
            f"torch {f2.get('000985', {}).get('test_acc')}，基线 0.5294）→ "
            "结论不是手写实现 bug 导致" if cross.get("000985", {}).get(
                "both_below_baseline") else "两版结论需人工复核"),
        "by_index": cross,
        "must_not_claim": [
            "不得写「Transformer 在中证1000 上有效」——p 值不显著",
            "不得写「部分指数验证了预测能力」——4 次检验会推高假阳性",
            "不得把测试期准确率当作仓位建议的依据",
        ],
    }
    print(f"\n{'='*74}")
    print(f"总判定：{'存在显著结果（需扩样本复核）' if any_sig else '全部不显著 → 无预测能力'}")
    print(f"  {out['final_verdict']['statement']}")
    print(f"  交叉验证：{out['final_verdict']['cross_validation']}")

    p = os.path.join(HERE, "audit_round112_transformer_verdict.json")
    json.dump(out, open(p, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"\n✅ 已写出 {p}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
