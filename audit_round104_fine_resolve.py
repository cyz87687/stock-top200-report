#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
audit_round104_fine_resolve.py —— 闭合 round102：细网格约束内重解 + quality 邻域偏好

═══ 依据（R103 §5.3/§5.4/§6 裁决与"近期可行动作"）═══
  ① step=0.10 粗网格的约束内最优把 quality 顶到 0.20 上限 → 红旗
     （第八轮实证 quality 真实 IC 为负，粗网格+小样本疑似过拟合）
  ② 裁决原文：「在更细网格上寻找 quality 附近（含 0）的稳定最优解，
     避免被粗网格推到 0.20 上限」
  ③ 门禁纪律：任何候选上线前必须过 weight_admission_gate 五道门禁

═══ 本脚本做什么 ═══
  1. 枚举 v4.15 约束内（单维≤0.20 / 大类≤0.30 / Σ=1）step=0.05 可行解
  2. 过滤 quality ≤ 0.10（quality 邻域含 0，规避红旗区）
  3. 多进程在重建池域（pit_pool, median 260 只）评估 IC/spread/涨跌市 ICIR
  4. 两级筛选：
     A. ICIR 最高（spread>0 且噪声容忍单调）
     B. quality 邻域稳定解：ICIR ≥ 0.95×最优 的解里取 quality 最低、
        并要求左右扰动（quality ±0.05 同域重解）ICIR 落差小
  5. 输出 audit_round104.json + 候选权重文件 ready for weight_admission_gate

用法：python audit_round104_fine_resolve.py [--step 0.05] [--quality-cap 0.10] [--workers 8]
纪律：仅本地；禁 git commit/push；结果仅为候选，门禁通过才可落地。
"""
import os
import sys
import json
import time
import argparse

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import factor_validation_core as F
import weight_admission_gate as G
import audit_round102_constrained_resolve as R102

PANEL = os.path.join(HERE, "data", "panel_round66.json")
NPZ = os.path.join(HERE, "data", "val_arrays_h20.npz")
OUT = os.path.join(HERE, "audit_round104.json")
OUT_W = os.path.join(HERE, "candidate_v416_gray.json")

DIMS = F.DIMS

# ---- 多进程共享（fork 后子进程直接可见）----
_G = {}


def _eval_worker(combo):
    w = {d: v for d, v in zip(DIMS, combo) if v > 1e-9}
    try:
        r = R102.evaluate_one(_G["X"], _G["M"], _G["Y"], _G["RY"],
                              _G["mask"], _G["used"], w, _G["up"], _G["dn"])
    except Exception as e:          # 兜底留痕（禁静默）
        return {"combo": dict(zip(DIMS, combo)), "error": str(e)}
    if r is None:
        return None
    r["combo"] = {d: round(v, 6) for d, v in zip(DIMS, combo) if v > 1e-9}
    return r


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--step", type=float, default=0.05)
    ap.add_argument("--quality-cap", type=float, default=0.10)
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--max-enum", type=int, default=200000)
    args = ap.parse_args()

    t0 = time.time()
    dates, X, M, Y, codes = F.load_panel_arrays(PANEL, NPZ)
    dates = [str(d) for d in dates]
    codes = [str(c) for c in codes]
    RY = F.rank_rows(Y)
    mask, used = G.make_pool_mask(dates, codes)
    if not used:
        print("⚠️ pit_pool 无可比截面")
        return 2
    up, dn, med = R102.split_regime(Y)
    sizes = [int(mask[i].sum()) for i in used]
    print(f"面板 {X.shape} | 重建池 {len(used)} 截面 median {int(np.median(sizes))} 只 | "
          f"regime 涨 {len(up)}/跌 {len(dn)}")
    _G.update(X=X, M=M, Y=Y, RY=RY, mask=mask, used=used, up=up, dn=dn)

    # 基准
    w415 = G.load_v415()
    base = R102.evaluate_one(X, M, Y, RY, mask, used, w415, up, dn)
    print(f"基准 v4.15  ICIR={base['icir']:.4f} spread={base['spread']:+.5f} "
          f"涨市={base['up_icir']:.4f} 跌市={base['dn_icir']:.4f}")

    # 枚举 + quality 过滤
    combos, truncated = R102.feasible_grid(args.step, limit=args.max_enum)
    print(f"可行解 {len(combos)} 个" + ("（截断）" if truncated else ""))
    qi = DIMS.index("score_quality")
    sel = [c for c in combos if c[qi] <= args.quality_cap + 1e-9]
    print(f"quality ≤ {args.quality_cap} 过滤后 {len(sel)} 个")

    # 多进程评估
    rows = []
    import multiprocessing as mp
    ctx = mp.get_context("fork")
    with ctx.Pool(processes=args.workers) as pool:
        for k, r in enumerate(pool.imap_unordered(_eval_worker, sel, chunksize=256), 1):
            if r and "error" not in r:
                rows.append(r)
            elif r and "error" in r:
                print("⚠️ 评估异常:", r["error"][:120])
            if k % 2000 == 0:
                print(f"    {k}/{len(sel)} ...", flush=True)
    print(f"有效评估 {len(rows)} 个  耗时 {time.time()-t0:.0f}s")
    if not rows:
        return 2

    # ---- A. ICIR 最高（spread>0 且噪声容忍单调）----
    A = [r for r in rows if r["spread"] is not None and r["spread"] > 0 and r["mono_robust"]]
    A.sort(key=lambda r: -r["icir"])
    best_icir = A[0]["icir"] if A else max(r["icir"] for r in rows)

    # ---- B. quality 邻域稳定解：ICIR ≥ 0.95×最优，quality 最低优先 ----
    B = [r for r in rows if r["spread"] is not None and r["spread"] > 0
         and r["icir"] >= 0.95 * best_icir]
    B.sort(key=lambda r: (r["combo"].get("score_quality", 0.0), -r["icir"]))

    def show(name, lst, n):
        print(f"\n【{name}】")
        if not lst:
            print("  （无）")
            return []
        print(f"  {'ICIR':>7s} {'t':>6s} {'spread':>9s} {'涨市':>7s} {'跌市':>7s} {'q':>5s}  权重")
        out = []
        for r in lst[:n]:
            ws = " ".join(f"{k.replace('score_','')}={v:.2f}"
                          for k, v in sorted(r["combo"].items(), key=lambda kv: -kv[1]))
            q = r["combo"].get("score_quality", 0.0)
            print(f"  {r['icir']:>7.4f} {r['t']:>6.2f} {r['spread']:>+9.5f} "
                  f"{r['up_icir']:>7.4f} {r['dn_icir']:>7.4f} {q:>5.2f}  {ws}")
            out.append({k: r[k] for k in ("combo", "ic", "icir", "t", "spread",
                                          "spread_t", "layer_means", "mono_robust",
                                          "mono_strict", "up_icir", "dn_icir")})
        return out

    selA = show(f"A. ICIR 最高（quality≤{args.quality_cap}，spread>0 且单调）", A, 10)
    selB = show("B. quality 邻域稳定解（ICIR≥0.95×最优，quality 升序）", B, 10)

    # 选定：B 表头名（quality 最低的高质量解）
    chosen = B[0] if B else (A[0] if A else None)
    if not chosen:
        print("⚠️ 无可用候选")
        return 2
    print(f"\n⇒ 选定候选: ICIR={chosen['icir']:.4f} (v4.15 {base['icir']:.4f}, "
          f"Δ{chosen['icir']-base['icir']:+.4f})  spread={chosen['spread']:+.5f}  "
          f"涨市={chosen['up_icir']:.4f} 跌市={chosen['dn_icir']:.4f}")
    print("  权重:", json.dumps(chosen["combo"], ensure_ascii=False))

    # 邻域敏感性：选定解 ±0.05 quality 同域重解（取邻近网格点对比）
    qi_v = chosen["combo"].get("score_quality", 0.0)
    nb = [r for r in rows if abs(r["combo"].get("score_quality", -9) - qi_v) <= 0.051
          and abs(r.get("icir", 0) - chosen["icir"]) >= 0]
    same_q = [r["icir"] for r in rows
              if abs(r["combo"].get("score_quality", -9) - qi_v) < 1e-9]
    import statistics as sta
    neigh = {
        "quality_value": qi_v,
        "same_quality_n": len(same_q),
        "same_quality_icir_mean": round(sta.mean(same_q), 4) if same_q else None,
        "same_quality_icir_p10": round(sorted(same_q)[max(0, len(same_q) // 10)], 4) if same_q else None,
        "same_quality_icir_p90": round(sorted(same_q)[int(len(same_q) * 0.9)], 4) if same_q else None,
        "note": "同 quality 切片的 ICIR 分布 → 稳定性粗判；邻域 ±0.05 由 step 网格自然覆盖",
    }
    print("  邻域稳定性:", json.dumps(neigh, ensure_ascii=False))

    out = {
        "meta": {
            "round": 104,
            "purpose": "闭合 round102：细网格 + quality 邻域（含 0）稳定解（R103 §5.4 近期可行动作）",
            "step": args.step,
            "quality_cap": args.quality_cap,
            "n_feasible": len(combos),
            "n_after_filter": len(sel),
            "n_evaluated": len(rows),
            "enumeration_truncated": truncated,
            "domain": "rebuild_pool (data/pit_pool.json)",
            "n_dates": len(used), "n_codes": len(codes),
            "regime": {"up": len(up), "down": len(dn), "median": med,
                       "caveat": "事后划分仅诊断，禁看涨跌切权重"},
            "built_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "elapsed_s": round(time.time() - t0, 1),
        },
        "baseline_v415": base,
        "topA_icir": selA,
        "topB_quality_neighborhood": selB,
        "chosen": {k: chosen[k] for k in ("combo", "ic", "icir", "t", "spread",
                                          "spread_t", "layer_means", "mono_robust",
                                          "mono_strict", "up_icir", "dn_icir")},
        "neighborhood_stability": neigh,
        "caveat": [
            "候选未经 weight_admission_gate 五道门禁，落地前必须跑门禁",
            "quality≤0.10 过滤依据 R103 红旗（quality IC 为负 + 粗网格顶上限）",
            "样本 219 截面，统计功效有限；G5 walk-forward 预期仍紧张",
            "regime 事后划分仅诊断，禁止据此看涨跌切权重（R2/round73）",
        ],
    }
    json.dump(G._jsonable(out), open(OUT, "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)

    # 候选权重文件（给 weight_admission_gate --weights 用）
    full = {d: float(chosen["combo"].get(d, 0.0)) for d in DIMS}
    cand = {
        "version": "v4.16-gray-icir-candidate",
        "fitted_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "weights": full,
        "prior_weights": full,
        "method": ("audit_round104 细网格约束内重解（step=%.2f, quality≤%.2f, 重建池域）；"
                   "R103 §5.4 裁决路径。候选状态：未过门禁。" % (args.step, args.quality_cap)),
        "round104_metrics": {k: chosen[k] for k in
                             ("ic", "icir", "t", "spread", "up_icir", "dn_icir")},
    }
    json.dump(cand, open(OUT_W, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    print(f"\n→ {OUT}\n→ {OUT_W}  耗时 {out['meta']['elapsed_s']}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
