#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""按当前 factor_weights.json 重算 scored 文件的 total / rating / rating_dist。

⚠️ 为什么需要这个脚本（v4.7）：
   权重一改，scored 里的 total 就过期了 —— 而页面直接显示 total 与评级。
   若只改权重不改数据，页面会显示「权重条写 15/12/13」但「分数还是老口径算的」，
   属于最典型的静默口径漂移（页面与数据各说各话）。
   本脚本严格复刻 step2_stockscorer_v2.py 的公式：
       total = Σ( min(v, DIM_MAX[k]) / DIM_MAX[k] * 10 * w ) / Σw * 10
   其中缺维从分母剔除（禁 or 0），DIM_MAX 取自 factor_weights.json 的 dim_max。

用法：
    python reweight_scored.py top200_scored_2026-10-23.json            # 就地重算（先备份）
    python reweight_scored.py xxx.json --dry-run                       # 只打印对比
"""
import json
import os
import shutil
import sys
from collections import Counter

_HERE = os.path.dirname(os.path.abspath(__file__))


def load_cfg():
    p = os.path.join(_HERE, "factor_weights.json")
    with open(p, encoding="utf-8") as f:
        cfg = json.load(f) or {}
    w = {k: float(v) for k, v in (cfg.get("weights") or {}).items()}
    dm = {k: float(v) for k, v in (cfg.get("dim_max") or {}).items()}
    return cfg, w, dm


def recompute_total(r, w, dm):
    """复刻 step2 的 total 公式；缺维从分母剔除。→ (total, 参与维数, 缺失维列表)"""
    num = 0.0
    den = 0.0
    miss = []
    used = 0
    for k, wv in w.items():
        if wv <= 0:
            continue
        v = r.get(k)
        if not isinstance(v, (int, float)) or isinstance(v, bool):
            miss.append(k)
            continue
        mx = max(dm.get(k, 10.0), 1e-9)
        num += min(v, mx) / mx * 10.0 * wv
        den += wv
        used += 1
    if den <= 0:
        return None, used, miss
    return round(num / den * 10.0, 2), used, miss


def abs_rating(total):
    if total is None:
        return None
    if total >= 85:
        return "S"
    if total >= 65:
        return "A"
    if total >= 45:
        return "B"
    if total >= 25:
        return "C"
    return "D"


ABS_ADVICE = {
    "S": "优先买入，可重仓", "A": "逢低加仓，重点关注", "B": "波段操作，轻仓参与",
    "C": "观望，不新开仓", "D": "坚决回避，立即卖出",
}


def quantile_ratings(items):
    """与 gen_report_top200._quantile_ratings 同源：按 total 降序分位给级。"""
    rows = [x for x in items if x.get("total") is not None]
    if not rows:
        return {}
    srt = sorted(rows, key=lambda x: -float(x.get("total") or 0))
    n = len(srt)
    out = {}
    for i, it in enumerate(srt):
        q = (i + 0.5) / n
        rt = "D"
        for hi, r in (("0.05", "S"), ("0.15", "A"), ("0.50", "B"), ("0.88", "C"), ("1.01", "D")):
            if q < float(hi):
                rt = r
                break
        out[it["code"]] = rt
    return out


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    dry = "--dry-run" in sys.argv
    if not args:
        print(__doc__)
        return 1
    path = args[0]
    cfg, w, dm = load_cfg()
    wsum = sum(w.values())
    print(f"配置版本: {cfg.get('version')}  |  权重合计 {wsum:.4f}")
    if abs(wsum - 1.0) > 0.02:
        print(f"❌ 权重合计 {wsum:.4f} ≠ 1.0，中止")
        return 2
    print("  " + ", ".join(f"{k.replace('score_','')}={v}" for k, v in sorted(w.items(), key=lambda x: -x[1]) if v > 0))

    with open(path, encoding="utf-8") as f:
        d = json.load(f)
    rs = d.get("results") or []
    if not rs:
        print("❌ 无 results")
        return 2

    old_w = d.get("weights") or {}
    print(f"\n文件内旧权重合计 {sum(float(v) for v in old_w.values()):.4f}  →  新 {wsum:.4f}")
    changed_w = [k for k in set(list(old_w.keys()) + list(w.keys()))
                 if abs(float(old_w.get(k, 0)) - float(w.get(k, 0))) > 1e-9]
    print("权重变化维度:", changed_w or "无")

    # 备份
    bak = path + ".bak_reweight"
    if not dry:
        shutil.copy2(path, bak)
        print(f"✅ 已备份 → {bak}")

    diffs = []
    miss_all = []
    for r in rs:
        t_new, used, miss = recompute_total(r, w, dm)
        if miss:
            miss_all.append((r.get("name"), miss))
        t_old = r.get("total")
        if t_new is None:
            continue
        r["total"] = t_new
        r["rating"] = abs_rating(t_new)          # 文件内 rating = 绝对档（与 gen_report 的 _rating_abs 同口径）
        r["advice"] = ABS_ADVICE.get(r["rating"], "")
        if isinstance(t_old, (int, float)):
            diffs.append(abs(t_new - t_old))
    n = len(diffs)
    print(f"\n重算 {len(rs)} 只；total 变化 |Δ|: max={max(diffs):.2f} "
          f"mean={sum(diffs)/n:.2f} 中位={sorted(diffs)[n//2]:.2f} | 变化>5 分的: {sum(1 for x in diffs if x>5)} 只")
    if miss_all:
        print(f"⚠️ 有缺维的记录 {len(miss_all)} 只（缺维从分母剔除）：{miss_all[:5]}")
    else:
        print("✅ 无缺维记录（7 个生效维全部有值）")

    # 评级分布（绝对档）
    dist = Counter(r.get("rating") for r in rs)
    print("绝对档分布:", {k: dist.get(k, 0) for k in "SABCD"})

    # 分位档（页面展示口径）预览
    q = quantile_ratings(rs)
    qd = Counter(q.values())
    print("分位档分布:", {k: qd.get(k, 0) for k in "SABCD"})

    if dry:
        print("\n[dry-run] 未写入")
        return 0

    d["weights"] = w
    d["model"] = f"stock-scorer {cfg.get('version')} (15-dim + AI + sector-momentum[evidence], prior-pending (全市场回测先验, 池内 IC 积累中))"
    st = d.get("stats") or {}
    st["rating_dist"] = {k: dist.get(k, 0) for k in "SABCD"}
    st["rating_dist_note"] = (
        "本字段=文件内 results[].rating 的分布（绝对分档 ≥85S/≥65A/≥45B/≥25C）。"
        "页面展示用的是当日截面分位口径（S前5%/A前15%/B前50%/C前88%/D后12%），"
        f"两者不同属既定设计。{cfg.get('version')} 权重调整后已全量重算。")
    d["stats"] = st
    with open(path, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=2)
    print(f"\n✅ 已写回 {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
