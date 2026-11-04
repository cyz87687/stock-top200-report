#!/usr/bin/env python3
"""用 v4.10-rev-quarter 权重重算 top200_scored 的 total / rating（任务 #291）。

背景
----
2026-10-27 round34 真·大池审计后，factor_weights.json 已改为 v4.10-rev-quarter：
反转块（bias60 / rsi_rev / tech_neg）整体降至 1/4，其余 5 维按比例上调，Σ 保持 1.0。
但 top200_scored_2026-10-27.json 里的 total / rating 仍是 v4.9 旧权重的计算结果，
若直接出报告会出现「页面权重显示新值、分数却是旧值」的自相矛盾。

做法
----
不重抓行情、不重跑 step2 —— 8 个生效维的原始分（score_*）与权重无关，
只把「加权汇总」这一步按新权重重算。量尺走 scale_calc.composite_total（唯一实现来源）。

⚠️ 回归校验（本脚本每次运行都做，不通过即拒绝写入）
    先用**文件内自带的旧权重**重算 total，必须与文件内 total 逐只吻合（误差 ≤ 0.02）；
    不吻合说明公式/维度集已漂移，此时用新权重的结果同样不可信 → 直接报错退出。

⚠️ 口径纪律
    权重变更后 total 与变更前**不可比**（同 v4.4 标度、v4.7 量程、v4.10 量尺变更）。
    本脚本只在同一份面板上换权重重算，不构成任何跨版本连续序列。

用法
----
    python recalc_v410.py [--dry-run] [scored_json]
"""
import argparse
import io
import json
import os
import sys
from collections import Counter

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import scale_calc  # noqa: E402

TOL = 0.02          # 回归复现容差（分）


def load_json(p):
    with io.open(p, encoding="utf-8") as f:
        return json.load(f)


def rating_of(total):
    """与 step2 / apply_sector_momentum 完全一致的分档（禁各自另立阈值）。"""
    if total >= 85:
        return "S", "优先买入，可重仓"
    if total >= 65:
        return "A", "逢低加仓，重点关注"
    if total >= 45:
        return "B", "波段操作，轻仓参与"
    if total >= 25:
        return "C", "观望，不新开仓"
    return "D", "坚决回避，立即卖出"


def is_num(x):
    return isinstance(x, (int, float)) and not isinstance(x, bool) and x == x


def total_of(row, wmap, dim_max, sum_all):
    """按给定权重算一只股票的 total；缺维走中性填充（scale_calc 内部处理）。"""
    parts = [(k, row.get(k), w) for k, w in wmap.items() if w > 0]
    total, miss = scale_calc.composite_total(
        parts, dim_max, sum_all=sum_all)
    return total, miss


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("scored", nargs="?", default=None)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    scored = args.scored
    if scored is None:
        cand = sorted(
            [f for f in os.listdir(HERE)
             if f.startswith("top200_scored_") and f.endswith(".json")
             and "_watch_" not in f and ".bak" not in f],
            reverse=True)
        if not cand:
            print("❌ 未找到 scored json")
            return 1
        scored = os.path.join(HERE, cand[0])
    if not os.path.exists(scored):
        print(f"❌ 不存在: {scored}")
        return 1

    doc = load_json(scored)
    rows = doc.get("results") or []
    if not rows:
        print("❌ scored 内无 results")
        return 1

    fw_doc = load_json(os.path.join(HERE, "factor_weights.json"))
    dim_max = fw_doc.get("dim_max") or {}
    w_new = {k: float(v) for k, v in (fw_doc.get("weights") or {}).items()
             if is_num(v) and v > 0}
    w_old = {k: float(v) for k, v in (doc.get("weights") or {}).items()
             if is_num(v) and v > 0}

    print("=" * 78)
    print(f"scored      : {os.path.basename(scored)}")
    print(f"内嵌 date   : {doc.get('date')}   （文件名日期未必相同，历史遗留）")
    print(f"样本        : {len(rows)} 只")
    print(f"factor_weights version = {fw_doc.get('version')}")
    print("-" * 78)
    print(f"旧权重 {len(w_old)} 维  Σ={sum(w_old.values()):.6f}")
    print(f"新权重 {len(w_new)} 维  Σ={sum(w_new.values()):.6f}")
    for k in sorted(set(w_old) | set(w_new)):
        a, b = w_old.get(k, 0.0), w_new.get(k, 0.0)
        if abs(a - b) > 1e-9:
            print(f"  {k:22s} {a:.4f} → {b:.4f}  ({b - a:+.4f})")
    print("=" * 78)

    # ---- 回归校验：旧权重必须能精确复现文件内 total ----
    sa_old = scale_calc.sum_all_weights(w_old)
    maxerr, nbad, bad = 0.0, 0, []
    for r in rows:
        t, _ = total_of(r, w_old, dim_max, sa_old)
        old = r.get("total")
        if not is_num(old):
            continue
        e = abs(t - old)
        maxerr = max(maxerr, e)
        if e > TOL:
            nbad += 1
            if len(bad) < 5:
                bad.append((r.get("code"), r.get("name"), old, t))
    print(f"[回归校验] 旧权重复现最大误差 = {maxerr:.6f}，超容差 {nbad}/{len(rows)}")
    if nbad > 0:
        print("❌ 旧权重无法复现现有 total → 公式或维度集已漂移，拒绝写入。")
        for b in bad:
            print("   偏差样本:", b)
        return 2

    # ---- 新权重重算 ----
    sa_new = scale_calc.sum_all_weights(w_new)
    dist_old = Counter(r.get("rating") for r in rows)
    new_rows = []
    for r in rows:
        t, miss = total_of(r, w_new, dim_max, sa_new)
        g, adv = rating_of(t)
        new_rows.append((r, t, g, adv, miss))

    dist_new = Counter(x[2] for x in new_rows)
    print("-" * 78)
    print("评级分布变化（同一批 1895 只，仅换权重）")
    print(f"{'档':<4}{'旧':>8}{'新':>8}{'Δ':>8}")
    for g in ("S", "A", "B", "C", "D"):
        o, n = dist_old.get(g, 0), dist_new.get(g, 0)
        print(f"{g:<4}{o:>8}{n:>8}{n - o:>+8}")

    deltas = [x[1] - x[0]["total"] for x in new_rows if is_num(x[0].get("total"))]
    deltas.sort()
    if deltas:
        n = len(deltas)
        print("-" * 78)
        print("total 变化幅度（新 − 旧）")
        print(f"  均值 {sum(deltas)/n:+.3f}   中位 {deltas[n//2]:+.3f}   "
              f"最小 {deltas[0]:+.3f}   最大 {deltas[-1]:+.3f}")
        moved = sum(1 for d in deltas if abs(d) > 1.0)
        print(f"  |Δ|>1 分的占比：{moved}/{n} = {moved/n*100:.1f}%")

    # 新旧 top20 对照（检验排序是否被权重改写）
    old_top = [r.get("code") for r in sorted(
        [x for x in rows if is_num(x.get("total"))],
        key=lambda x: -x["total"])[:20]]
    new_top = [x[0].get("code") for x in sorted(
        new_rows, key=lambda x: -x[1])[:20]]
    inter = len(set(old_top) & set(new_top))
    print("-" * 78)
    print(f"Top20 重合度：{inter}/20（权重改动若显著改写选股，此值会明显下降）")
    print(f"新 Top5：")
    for x in sorted(new_rows, key=lambda x: -x[1])[:5]:
        print(f"   {x[0].get('code')} {x[0].get('name'):<6} "
              f"total {x[1]:6.2f} ({x[2]})  旧 {x[0].get('total')}")

    # 中性填充统计（新口径下缺维仍中性填充，需披露）
    nfill = sum(1 for x in new_rows if x[4])
    print(f"\n中性填充(非实测)行数：{nfill}/{len(rows)}")

    if args.dry_run:
        print("\n[--dry-run] 未写盘。")
        return 0

    # ---- 写回 ----
    out = args.out or scored
    for r, t, g, adv, miss in new_rows:
        r["total"] = t
        r["rating"] = g
        r["advice"] = adv
        r["neutral_filled"] = list(miss)
        r["weights_version"] = fw_doc.get("version")
    rows.sort(key=lambda x: (-(x.get("total") or 0), -(x.get("turnover") or 0)))
    doc["weights"] = {k: (float(v) if is_num(v) else 0.0)
                      for k, v in (fw_doc.get("weights") or {}).items()}
    doc["weights_note"] = (
        f"v4.10-rev-quarter：反转块降至 1/4（round34 真·大池审计）。"
        f"⚠️ 权重变更后 total 与变更前不可比。")
    st = doc.get("stats") or {}
    st["rating_dist"] = {g: dist_new.get(g, 0) for g in ("S", "A", "B", "C", "D")}
    doc["stats"] = st

    tmp = out + ".tmp"
    with io.open(tmp, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, separators=(",", ":"))
    os.replace(tmp, out)
    print(f"\n✅ 已写回 {os.path.basename(out)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
