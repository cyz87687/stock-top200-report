#!/usr/bin/env python3
"""P0-4 量尺修复的 A/B 验证脚本（2026-10-27）。

不重跑 step2（那需要重新拉全部行情），而是在既有 scored 上按两条公式
分别重算 total，逐只对比，验证：
  ① 新版（统一量尺 B_neutral）与旧版（混合量尺 C_cur）的分差范围
  ② 评级迁移矩阵
  ③ Top50 成分变化
  ④ 数据完整性：生效 8 维的覆盖度、伪分/None 分布、值域越界检查

输出 p0_4_ab_verify.json，供页面与报告引用。

用法: python3 verify_p0_4_ab.py [scored_json]
"""
import json
import os
import sys
from collections import Counter, defaultdict

import scale_calc

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "p0_4_ab_verify.json")


def rating_of(t):
    if t >= 85:
        return "S"
    if t >= 65:
        return "A"
    if t >= 45:
        return "B"
    if t >= 25:
        return "C"
    return "D"


def score_cur(r, eff, DMAX):
    """旧口径：分母 = 该股可用维权重之和（混合量尺）。"""
    num = den = 0.0
    for k, w in eff.items():
        v = r.get(k)
        if scale_calc.is_num(v):
            mx = DMAX.get(k, 10.0)
            num += min(float(v), mx) / mx * 10.0 * w
            den += w
    return round(num / max(0.5, den) * 10, 2) if den > 0 else None


def main():
    scored_path = (sys.argv[1] if len(sys.argv) > 1
                   else os.path.join(HERE, "top200_scored_2026-10-27.json"))
    scored = json.load(open(scored_path, encoding="utf-8"))
    rows = scored["results"]
    date = scored.get("date")

    W, DMAX, meta = scale_calc.load_weights_and_dim_max(HERE)
    eff = {k: v for k, v in W.items() if v > 0}
    SUM_ALL = scale_calc.sum_all_weights(eff)

    print(f"scored = {os.path.basename(scored_path)}  date={date}  n={len(rows)}")
    print(f"权重版本 = {meta.get('version')}  生效 {len(eff)} 维  Σw={SUM_ALL:.4f}")

    # ---------- 一、数据完整性 ----------
    print("\n=== 一、数据完整性 / 指标质量 ===")
    print(f"{'维度':22s} {'权重':>6s} {'DMAX':>5s} {'有值':>6s} {'覆盖':>7s} "
          f"{'min':>8s} {'p50':>8s} {'max':>8s} {'越界':>5s}")
    integrity = {}
    for k, w in eff.items():
        vals = [float(r[k]) for r in rows if scale_calc.is_num(r.get(k))]
        mx = DMAX.get(k, 10.0)
        vals_sorted = sorted(vals)
        p50 = vals_sorted[len(vals_sorted) // 2] if vals_sorted else None
        over = sum(1 for v in vals if v > mx + 1e-9 or v < -1e-9)
        print(f"{k:22s} {w:6.3f} {mx:5.1f} {len(vals):6d} "
              f"{100*len(vals)/len(rows):6.1f}% "
              f"{min(vals):8.2f} {p50:8.2f} {max(vals):8.2f} {over:5d}"
              if vals else f"{k:22s} {w:6.3f} {mx:5.1f} {0:6d}   0.0%  (全空)")
        integrity[k] = {
            "weight": round(w, 4), "dim_max": mx, "n_ok": len(vals),
            "coverage_pct": round(100.0 * len(vals) / len(rows), 2),
            "min": round(min(vals), 4) if vals else None,
            "p50": round(p50, 4) if p50 is not None else None,
            "max": round(max(vals), 4) if vals else None,
            "n_out_of_range": over,
        }

    # 权重一致性：配置Σ vs 文件内Σ
    fw_file = (scored.get("weights") or {})
    mism = [k for k in eff if abs(float(fw_file.get(k) or 0) - eff[k]) > 1e-6]
    print(f"\n文件内权重与 factor_weights.json 不一致的维: {mism or '无'}")

    # ---------- 二、A/B 逐只对比 ----------
    print("\n=== 二、A/B 逐只对比（旧混合量尺 vs 新统一量尺） ===")
    recs = []
    miss_hist = Counter()
    miss_by_dim = Counter()
    for r in rows:
        c = score_cur(r, eff, DMAX)
        n, miss = scale_calc.composite_total(
            [(k, r.get(k), w) for k, w in eff.items()], DMAX, weights=eff)
        if c is None:
            continue
        miss_hist[len(miss)] += 1
        for k in miss:
            miss_by_dim[k] += 1
        recs.append({
            "code": r.get("code"), "name": r.get("name"),
            "sector": r.get("sector_l2") or r.get("sector"),
            "price": r.get("price"), "pct": r.get("pct_chg"),
            "old": c, "new": n, "d": round(n - c, 2),
            "old_rating": rating_of(c), "new_rating": rating_of(n),
            "missing": miss, "n_missing": len(miss),
            "file_total": r.get("total"),
        })
    print(f"可比样本 n={len(recs)}")
    print("\n缺维个数分布：")
    for k in sorted(miss_hist):
        print(f"  缺 {k} 维: {miss_hist[k]:5d} 只 ({100*miss_hist[k]/len(recs):5.1f}%)")
    print("\n按维度统计缺值只数：")
    for k, v in miss_by_dim.most_common():
        print(f"  {k:22s} {v:5d} 只 ({100*v/len(recs):5.1f}%)")

    ds = [x["d"] for x in recs]
    ds_sorted = sorted(ds)
    print(f"\n分差 new−old: min={min(ds):+.2f} "
          f"p50={ds_sorted[len(ds_sorted)//2]:+.2f} max={max(ds):+.2f}")
    print(f"  上升>0.01: {sum(1 for x in ds if x > 0.01):5d} 只")
    print(f"  下降<−0.01: {sum(1 for x in ds if x < -0.01):5d} 只")
    print(f"  基本不变: {sum(1 for x in ds if abs(x) <= 0.01):5d} 只")

    # 旧口径复算 vs 文件 total（确认我的复算与生产一致）
    bad = [x for x in recs
           if scale_calc.is_num(x["file_total"]) and abs(x["file_total"] - x["old"]) > 0.2]
    print(f"\n旧口径复算 vs 文件 total 不符(>0.2 分): {len(bad)} 只 "
          f"→ {'✅ 复算与生产一致' if not bad else '❌ 需排查'}")

    # ---------- 三、评级迁移 ----------
    print("\n=== 三、评级迁移矩阵（行=旧，列=新） ===")
    mig = defaultdict(int)
    order = ["S", "A", "B", "C", "D"]
    for x in recs:
        mig[(x["old_rating"], x["new_rating"])] += 1
    print("      " + "".join(f"{c:>7s}" for c in order) + f"{'合计':>7s}")
    for a in order:
        row_sum = sum(mig[(a, b)] for b in order)
        print(f"  {a}   " + "".join(f"{mig[(a,b)]:7d}" for b in order) + f"{row_sum:7d}")
    n_chg = sum(v for (a, b), v in mig.items() if a != b)
    print(f"\n评级变化: {n_chg} 只（{100*n_chg/len(recs):.1f}%）")
    up = sum(v for (a, b), v in mig.items()
             if order.index(b) < order.index(a))
    dn = sum(v for (a, b), v in mig.items()
             if order.index(b) > order.index(a))
    print(f"  上调(评级变好): {up} 只 / 下调: {dn} 只")

    # ---------- 四、Top50 成分变化 ----------
    print("\n=== 四、Top50 成分变化 ===")
    rk_old = [x["code"] for x in sorted(recs, key=lambda z: (-z["old"], z["code"]))[:50]]
    rk_new = [x["code"] for x in sorted(recs, key=lambda z: (-z["new"], z["code"]))[:50]]
    keep = len(set(rk_old) & set(rk_new))
    print(f"旧 Top50 中保留到新 Top50 的: {keep}/50")
    name_of = {x["code"]: x["name"] for x in recs}
    out = [name_of[c] for c in rk_old if c not in set(rk_new)]
    inn = [name_of[c] for c in rk_new if c not in set(rk_old)]
    print(f"  调出（旧新Top50）: {len(out)} 只 {out[:10]}")
    print(f"  调入: {len(inn)} 只 {inn[:10]}")

    # ---------- 五、被纠正的「虚高」个案 ----------
    print("\n=== 五、虚高纠正最显著的 10 只（旧分被混合量尺抬高） ===")
    for x in sorted(recs, key=lambda z: z["d"])[:10]:
        print(f"  {x['name']:8s} {x['code']:10s} {x['old']:6.2f} → {x['new']:6.2f} "
              f"({x['d']:+6.2f}) 缺{x['n_missing']}维 {x['missing']} "
              f"评级 {x['old_rating']}→{x['new_rating']}")

    # ---------- 六、与对比页预期核验 ----------
    print("\n=== 六、与 p0_4_scale_compare.html 的预期核对 ===")
    print(f"  对比页 B_neutral 区间 [−5.47, +3.88]；本次实测 "
          f"[{min(ds):+.2f}, {max(ds):+.2f}] → "
          f"{'✅ 一致' if abs(min(ds)+5.47) < 0.02 and abs(max(ds)-3.89) < 0.02 else '⚠️ 有出入'}")

    result = {
        "date": date, "scored": os.path.basename(scored_path),
        "n": len(recs), "weights_version": meta.get("version"),
        "sum_all": round(SUM_ALL, 6), "n_eff_dims": len(eff),
        "integrity": integrity,
        "weight_mismatch_vs_file": mism,
        "old_total_recompute_mismatch": len(bad),
        "missing_hist": {str(k): v for k, v in sorted(miss_hist.items())},
        "missing_by_dim": dict(miss_by_dim),
        "delta": {"min": round(min(ds), 2),
                  "p50": round(ds_sorted[len(ds_sorted) // 2], 2),
                  "max": round(max(ds), 2),
                  "n_up": sum(1 for x in ds if x > 0.01),
                  "n_down": sum(1 for x in ds if x < -0.01),
                  "n_same": sum(1 for x in ds if abs(x) <= 0.01)},
        "rating_migration": {f"{a}->{b}": v for (a, b), v in mig.items() if a != b},
        "n_rating_changed": n_chg,
        "n_rating_up": up, "n_rating_down": dn,
        "top50_keep": keep,
        "top50_out": out, "top50_in": inn,
        "top_down": sorted(recs, key=lambda z: z["d"])[:20],
        "top_up": sorted(recs, key=lambda z: -z["d"])[:10],
    }
    json.dump(result, open(OUT, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"\n✅ 已写出 {OUT}")


if __name__ == "__main__":
    main()
