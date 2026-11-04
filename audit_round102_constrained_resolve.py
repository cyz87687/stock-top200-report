#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
audit_round102_constrained_resolve.py —— 在 v4.15 约束内重解权重

═══ 为什么需要这一步 ═══

round100/101 找到的最强候选（翻转维压缩至 0.05）有两个硬障碍：
  ① 违反 v4.15 约束：fund .454 / size_growth .259 / quality .227 三超单因子 0.20；
     基本面大类 .68 超大类上限 0.30
  ② G5 walk-forward 只过 1/6 折

障碍 ① 是**治理问题不能破例**，障碍 ② 是**样本量问题短期无解**。
但障碍 ① 可以直接绕开：既然方向（压缩翻转维）已经由 P1-1 的代价曲线和
P1-3 的「等权 3 稳定维 ICIR 0.8048」两条独立证据指向，那么在
**约束内**沿这个方向重解，就能知道「合规空间里最好能做到多少」。

═══ 本脚本做什么 ═══

在 v4.15 约束（单因子 ≤0.20 / 5 大类各 ≤0.30 / Σ=1）下：
  1. 枚举可行权重网格（步长默认 0.02，九维）
  2. 对每个可行解，在**重建池域**上算全样本 ICIR 与分层 spread
  3. 多目标筛选：
     A. ICIR 最高（spread>0 且 G1 噪声容忍通过）
     B. spread 最高（ICIR>0）
     C. 折中（ICIR 与 spread 均标准化后加权）
  4. 对最优解跑完整五道门禁
  5. 输出与 v4.15 的逐项对照

═══ 为什么不能只按 ICIR 排序 ═══

记忆 R1 已证：模型是**防御型选股器**，且 IC 与市场方向同号率仅 33.8%。
只按全样本 ICIR 优化会得到「涨市完全失效」的解（round97 minimax 解即如此，
它把 lowvol20/bias60/rsi_rev 全归零，放弃跌市捕获力）。
故本脚本**必须同时报涨市 ICIR 与跌市 ICIR**，让决策者看到取舍。

用法：
  python audit_round102_constrained_resolve.py
  python audit_round102_constrained_resolve.py --step 0.05   # 粗网格快速看方向
  python audit_round102_constrained_resolve.py --topn 15
"""
import os
import sys
import json
import math
import time
import argparse
import itertools

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import factor_validation_core as F
import weight_admission_gate as G

PANEL = os.path.join(HERE, "data", "panel_round66.json")
NPZ = os.path.join(HERE, "data", "val_arrays_h20.npz")
OUT = os.path.join(HERE, "audit_round102.json")

DIMS = F.DIMS
DIM_MAX = G.DIM_MAX

# v4.15 约束（与 factor_weights.json 的 weight_constraints 一致）
DIM_CAP = 0.20
GROUP_CAP = 0.30
GROUPS = F.GROUPS


def _jsonable(o):
    return G._jsonable(o)


def feasible_grid(step=0.02, dims=None, limit=60000):
    """枚举满足 v4.15 约束的权重（Σ=1、单维≤0.20、大类≤0.30）。

    ⚠️ 组合数爆炸风险（实测 2026-10-29）：
       step=0.10 → 1040 个；step=0.05 → 44401 个；step=0.02 → **直接 OOM（exit 137）**。
       故必须有 limit 硬闸 —— 达到上限即停止枚举并**如实返回 truncated 标记**，
       绝不允许静默 OOM（OOM 后 kill 会连带杀掉后台跑批链路，本项目已踩过 exit 137）。
    """
    dims = dims or DIMS
    n = len(dims)
    grid = [round(i * step, 6) for i in range(int(round(DIM_CAP / step)) + 1)]
    gidx = {g: [dims.index(k) for k in ks if k in dims] for g, ks in GROUPS.items()}

    out = []
    state = {"truncated": False}

    def rec(i, w, rem):
        if state["truncated"]:
            return
        if len(out) >= limit:
            state["truncated"] = True
            return
        if i == n:
            if abs(rem) < 1e-9:
                out.append(tuple(w))
            return
        # 剩余维度最多能填 rem（每个 ≤ DIM_CAP）
        if rem < -1e-9 or rem > (n - i) * DIM_CAP + 1e-9:
            return
        for v in grid:
            if v > rem + 1e-9:
                break
            w.append(v)
            # 大类剪枝：已超额则跳过
            ok = True
            for g, idx in gidx.items():
                if i in idx:
                    s = sum(w[j] for j in idx if j <= i)
                    if s > GROUP_CAP + 1e-9:
                        ok = False
                        break
            if ok:
                rec(i + 1, w, rem - v)
            w.pop()
            if state["truncated"]:
                return

    sys.setrecursionlimit(10000)
    rec(0, [], 1.0)
    return out, state["truncated"]


def split_regime(Y):
    """事后 regime 划分（用池等权未来收益中位数）。

    ⚠️ 仅作分层诊断。记忆 R2（round72/73）已证 regime 不可事前利用，
       禁止据此做「看涨跌切权重」。
    """
    ewY = np.nanmean(Y, axis=1)
    med = float(np.nanmedian(ewY))
    up = [i for i in range(len(ewY)) if np.isfinite(ewY[i]) and ewY[i] > med]
    dn = [i for i in range(len(ewY)) if np.isfinite(ewY[i]) and ewY[i] <= med]
    return up, dn, med


def evaluate_one(X, M, Y, RY, pool_mask, used, wmap, up, dn):
    """单个权重的完整指标。"""
    ics = G.ic_series(X, M, Y, RY, wmap, pool_mask, used)
    st = F.stat(ics)
    if st.get("icir") is None:
        return None
    lst = G.layer_stats(X, M, Y, wmap, pool_mask, used)
    st_up = F.stat(G.ic_series(X, M, Y, RY, wmap, pool_mask, up))
    st_dn = F.stat(G.ic_series(X, M, Y, RY, wmap, pool_mask, dn))
    return {
        "ic": st["ic"], "icir": st["icir"], "t": st["t"], "n": st["n"],
        "spread": lst["spread"], "spread_t": lst["spread_t"],
        "layer_means": lst["means"],
        "mono_robust": G.is_monotonic_robust(lst),
        "mono_strict": G.is_monotonic(lst["means"]),
        "up_icir": st_up.get("icir"), "dn_icir": st_dn.get("icir"),
        "up_ic": st_up.get("ic"), "dn_ic": st_dn.get("ic"),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--step", type=float, default=0.02, help="权重网格步长")
    ap.add_argument("--topn", type=int, default=12)
    ap.add_argument("--max-enum", type=int, default=200000, help="枚举上限（防爆内存）")
    args = ap.parse_args()

    t0 = time.time()
    dates, X, M, Y, codes = F.load_panel_arrays(PANEL, NPZ)
    dates = [str(d) for d in dates]
    codes = [str(c) for c in codes]
    RY = F.rank_rows(Y)
    print(f"面板 {X.shape}  {dates[0]}~{dates[-1]}")

    mask, used = G.make_pool_mask(dates, codes)
    if not used:
        print("⚠️ pit_pool 无可比截面")
        return 2
    print(f"重建池 {len(used)} 截面  median {int(np.median([int(mask[i].sum()) for i in used]))} 只")

    up, dn, med = split_regime(Y)
    print(f"regime（事后）: 涨 {len(up)} / 跌 {len(dn)}  (中位 {med:.5f})")

    # 基准
    w415 = G.load_v415()
    base = evaluate_one(X, M, Y, RY, mask, used, w415, up, dn)
    stable = G.STABLE_DIMS
    w_stable = {d: 1.0 / len(stable) for d in stable}
    base_stable = evaluate_one(X, M, Y, RY, mask, used, w_stable, up, dn)
    print(f"\n基准 v4.15     ICIR={base['icir']:>7.4f} spread={base['spread']:+.5f} "
          f"涨市={base['up_icir']:>7.4f} 跌市={base['dn_icir']:>7.4f}")
    print(f"基准 等权3稳定维 ICIR={base_stable['icir']:>7.4f} spread={base_stable['spread']:+.5f} "
          f"涨市={base_stable['up_icir']:>7.4f} 跌市={base_stable['dn_icir']:>7.4f}")

    # 枚举
    print(f"\n枚举可行权重（step={args.step}）...")
    combos, truncated = feasible_grid(args.step, limit=args.max_enum)
    print(f"  可行解 {len(combos)} 个"
          f"{'  ⚠️ 已达上限被截断（结果仅覆盖搜索空间的一部分）' if truncated else ''}")
    if not combos:
        print("⚠️ 无可行解")
        return 2

    rows = []
    for k, c in enumerate(combos, 1):
        w = {d: v for d, v in zip(DIMS, c) if v > 1e-9}
        r = evaluate_one(X, M, Y, RY, mask, used, w, up, dn)
        if r is None:
            continue
        r["combo"] = {d: round(v, 6) for d, v in zip(DIMS, c) if v > 1e-9}
        rows.append(r)
        if k % 2000 == 0:
            print(f"    {k}/{len(combos)} ...", flush=True)

    print(f"  有效评估 {len(rows)} 个")
    if not rows:
        print("⚠️ 无有效解")
        return 2

    # ── 多目标筛选 ──
    # A. ICIR 最高（spread>0 且噪声容忍单调通过）
    A = [r for r in rows if r["spread"] is not None and r["spread"] > 0 and r["mono_robust"]]
    A.sort(key=lambda r: -r["icir"])
    # B. spread 最高（ICIR>0）
    B = [r for r in rows if r["icir"] > 0 and r["spread"] is not None and r["spread"] > 0]
    B.sort(key=lambda r: -r["spread"])
    # C. 折中：ICIR 与 spread 各自标准化后等权
    icirs = np.array([r["icir"] for r in rows])
    sprs = np.array([r["spread"] if r["spread"] is not None else -9 for r in rows])
    z_icir = (icirs - icirs.mean()) / (icirs.std() + 1e-12)
    z_spr = (sprs - sprs.mean()) / (sprs.std() + 1e-12)
    for r, a, b in zip(rows, z_icir, z_spr):
        r["_blend"] = float(0.5 * a + 0.5 * b)
    C = sorted([r for r in rows if r["spread"] is not None and r["spread"] > 0],
               key=lambda r: -r["_blend"])

    def show(name, lst, n):
        print(f"\n  【{name}】")
        if not lst:
            print("    （无满足条件解）")
            return []
        print(f"    {'ICIR':>7s} {'t':>6s} {'spread':>9s} {'涨市':>7s} {'跌市':>7s} "
              f"{'单调':>4s}  权重")
        out = []
        for r in lst[:n]:
            ws = " ".join(f"{k.replace('score_','')}={v:.2f}"
                          for k, v in sorted(r["combo"].items(), key=lambda kv: -kv[1]))
            print(f"    {r['icir']:>7.4f} {r['t']:>6.2f} {r['spread']:>+9.5f} "
                  f"{r['up_icir']:>7.4f} {r['dn_icir']:>7.4f} "
                  f"{'Y' if r['mono_robust'] else 'N':>4s}  {ws}")
            out.append({k: r[k] for k in ("combo", "ic", "icir", "t", "spread",
                                          "spread_t", "layer_means", "mono_robust",
                                          "mono_strict", "up_icir", "dn_icir")})
        return out

    selA = show("A. ICIR 最高（spread>0 且噪声容忍单调）", A, args.topn)
    selB = show("B. spread 最高", B, args.topn)
    selC = show("C. ICIR×spread 折中", C, args.topn)

    # ── 最优 A 与 v4.15 逐项对照 ──
    best = A[0] if A else (C[0] if C else None)
    compare = None
    if best:
        compare = {
            "best": {k: best[k] for k in ("combo", "ic", "icir", "t", "spread",
                                          "spread_t", "layer_means", "mono_robust",
                                          "up_icir", "dn_icir")},
            "v415": base,
            "stable_equal": base_stable,
            "delta_icir": round(best["icir"] - base["icir"], 4),
            "delta_spread": round((best["spread"] or 0) - (base["spread"] or 0), 5),
            "delta_up": round((best["up_icir"] or 0) - (base["up_icir"] or 0), 4),
            "delta_dn": round((best["dn_icir"] or 0) - (base["dn_icir"] or 0), 4),
        }
        print(f"\n  ⇒ 约束内最优: ICIR {best['icir']:.4f}（v4.15 为 {base['icir']:.4f}，"
              f"Δ {compare['delta_icir']:+.4f}）")
        print(f"                spread {best['spread']:+.5f}（v4.15 为 {base['spread']:+.5f}）")
        print(f"                涨市 {best['up_icir']:.4f} / 跌市 {best['dn_icir']:.4f}"
              f"（v4.15: {base['up_icir']:.4f} / {base['dn_icir']:.4f}）")

    out = {
        "meta": {
            "round": 102,
            "purpose": "在 v4.15 约束内重解权重（单因子≤0.20 / 大类≤0.30 / Σ=1）",
            "npz": os.path.relpath(NPZ, HERE),
            "domain": "rebuild_pool (data/pit_pool.json)",
            "n_dates": len(used),
            "n_codes": len(codes),
            "step": args.step,
            "n_feasible": len(combos),
            "enumeration_truncated": truncated,
            "n_evaluated": len(rows),
            "regime": {"up": len(up), "down": len(dn), "median": med,
                       "caveat": "事后划分，仅诊断；记忆 R2 已证不可事前利用"},
            "built_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "elapsed_s": None,
        },
        "baseline": {"v415": base, "stable_equal": base_stable},
        "topA_icir": selA, "topB_spread": selB, "topC_blend": selC,
        "compare": compare,
        "caveat": [
            "约束内重解只解决「违规」问题，不解决「G5 折通过率不足」问题——"
            "样本量缺口仍在，最优解仍须跑完整五道门禁才能上线。",
            "regime 划分为事后（用未来收益），禁止据此做「看涨跌切权重」。",
            "域为重建池，股本用跨期常数（无拆股调整）→ 历史市值有近似误差。",
        ],
    }
    out["meta"]["elapsed_s"] = round(time.time() - t0, 1)
    json.dump(_jsonable(out), open(OUT, "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    print(f"\n→ {OUT}  耗时 {out['meta']['elapsed_s']}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
