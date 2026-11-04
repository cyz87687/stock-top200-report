"""assert_coverage_consistent.py —— 校验面板 meta.coverage 与实算一致。

round124 P1-4 新增。血案：`fix_panel_reversal_dims.py` 重派生了
score_bias60/score_rsi_rev 的取值，但没有回写 `meta.coverage`，导致
metadata 报 rsi_rev 98.0%、实算 99.4%。

为什么这是硬问题：面板的 metadata 是**所有披露的依据**（闸门、报告、
审计脚本都读它）。元数据落后于数据 = 用一个旧尺子量新东西，
而且量出来的数字还会被当成事实。

用法：
    python assert_coverage_consistent.py                 # 检查默认面板
    python assert_coverage_consistent.py --panel X.json  # 指定
    python assert_coverage_consistent.py --fix           # 就地修正 metadata
退出码：0 一致 / 1 不一致（--fix 后为 0）
"""
import argparse
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_PANEL = os.path.join(HERE, "data", "panel_round123.json")
TOL_PP = 0.1          # 容忍 0.1 个百分点（四舍五入误差）


def compute(panel):
    """实算每维行级覆盖率。"""
    dates = panel.get("dates") or []
    rows_all = panel.get("rows") or {}
    total = 0
    cnt = {}
    for d in dates:
        rows = rows_all.get(d) or {}
        total += len(rows)
        for _c, rec in rows.items():
            for k, v in rec.items():
                if isinstance(v, (int, float)) and not isinstance(v, bool) and v == v:
                    cnt[k] = cnt.get(k, 0) + 1
    return {k: {"n": v, "total": total,
                "pct": round(v / total * 100, 1) if total else 0.0}
            for k, v in cnt.items()}, total


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--panel", default=DEFAULT_PANEL)
    ap.add_argument("--fix", action="store_true")
    args = ap.parse_args()

    if not os.path.exists(args.panel):
        print(f"❌ 面板不存在：{args.panel}")
        return 1
    with open(args.panel, encoding="utf-8") as f:
        panel = json.load(f)

    actual, total = compute(panel)
    declared = (panel.get("meta") or {}).get("coverage") or {}

    print(f"面板 {os.path.basename(args.panel)}  总行数 {total:,}")
    print(f"{'维度':<22}{'metadata':>12}{'实算':>12}{'差(pp)':>10}  判定")
    print("-" * 70)

    bad = []
    for k in sorted(set(actual) | set(declared)):
        a = actual.get(k, {}).get("pct")
        d = (declared.get(k) or {}).get("pct") if isinstance(declared.get(k), dict) else None
        if a is None:
            print(f"{k:<22}{str(d):>12}{'—':>12}{'—':>10}  ⚠️ metadata 有、数据无")
            bad.append((k, d, None))
            continue
        if d is None:
            print(f"{k:<22}{'—':>12}{a:>12}{'—':>10}  ⚠️ 数据有、metadata 缺")
            bad.append((k, None, a))
            continue
        diff = round(a - d, 2)
        flag = "✅" if abs(diff) <= TOL_PP else "❌ 不一致"
        print(f"{k:<22}{d:>12.1f}{a:>12.1f}{diff:>+10.2f}  {flag}")
        if abs(diff) > TOL_PP:
            bad.append((k, d, a))

    print("-" * 70)
    if not bad:
        print("✅ 全部一致（容忍 %.1fpp）" % TOL_PP)
        return 0

    print(f"❌ {len(bad)} 个维度不一致")
    if args.fix:
        panel.setdefault("meta", {})["coverage"] = actual
        panel["meta"].setdefault("audit_fixes", []).append({
            "round": "124",
            "what": "meta.coverage 按实算回写",
            "why": ("fix_panel_reversal_dims.py 重派生了 score_bias60/"
                    "score_rsi_rev 取值但未回写 coverage，导致元数据落后于数据；"
                    "元数据是所有披露的依据，不能落后"),
            "fixed_dims": [b[0] for b in bad],
        })
        tmp = args.panel + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(panel, f, ensure_ascii=False)
        os.replace(tmp, args.panel)
        print(f"✅ 已回写 {args.panel}")
        return 0
    print("   加 --fix 就地修正")
    return 1


if __name__ == "__main__":
    sys.exit(main())
