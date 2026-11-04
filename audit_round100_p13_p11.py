#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
audit_round100_p13_p11.py —— P1-3（接入重建池）+ P1-1（翻转维度权重取舍）

═══ P1-3：为什么必须接入重建池 ═══

round91 已逐截面重建「当日真实可交易池」（data/pit_pool.json），
池规模 min 175 / median 266 / max 312，与原固定 2029 只域重叠率约 98.5%。
但 round92~99 全部仍在 2029 固定域上评估 —— 该域是「事后筛选过的」，
含池前视偏差（记忆实测：全池等权 +29.34% vs HS300 −11.46%，差 40.8pp）。

本轮把评估域换成 pit_pool 的当日成分，再看：
  · 各因子 IC / ICIR 是否还成立
  · 组合分层单调性是否改善
  · v4.15 权重 vs 等权 vs 单因子 的相对优劣是否反转

═══ P1-1：符号翻转维度的权重取舍 ═══

round94 实测 4 维在涨/跌市符号翻转（ep / lowvol20 / rsi_rev / tech_neg），
名义权重合计 0.4244（v4.15）。本轮做「压缩扫描」：
  把翻转维度总权重从 0.4244 逐步压到 0，把释放的权重按比例分给稳定维
  （fund / quality / size_growth），逐档观察：
    · 全样本 ICIR
    · 涨市段 ICIR / 跌市段 ICIR（看防御力的代价）
    · 分层 spread
输出「代价曲线」，把取舍量化给决策者，而不是凭感觉二选一。

用法：
  python audit_round100_p13_p11.py --task p13
  python audit_round100_p13_p11.py --task p11
"""
import os
import sys
import json
import math
import time
import argparse

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import factor_validation_core as F

PANEL = os.path.join(HERE, "data", "panel_round66.json")
NPZ = os.path.join(HERE, "data", "val_arrays_h20.npz")
PIT_POOL = os.path.join(HERE, "data", "pit_pool.json")
WEIGHTS = os.path.join(HERE, "factor_weights.json")
OUT = os.path.join(HERE, "audit_round100.json")

DIMS = F.DIMS
DIM_MAX = {d: 10.0 for d in DIMS}


def _fnum(v):
    """数值判定（涵盖 numpy 标量；np.float32 不是 float 实例，必须用 numbers.Real）。"""
    import numbers
    if isinstance(v, bool) or v is None:
        return None
    if isinstance(v, numbers.Real):
        try:
            f = float(v)
        except (TypeError, ValueError):
            return None
        return None if (math.isnan(f) or math.isinf(f)) else f
    return None


def _jsonable(o):
    if isinstance(o, dict):
        return {k: _jsonable(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_jsonable(v) for v in o]
    if isinstance(o, np.bool_):
        return bool(o)
    if isinstance(o, np.integer):
        return int(o)
    if isinstance(o, np.floating):
        return float(o)
    return o


def load_w_v415():
    d = json.load(open(WEIGHTS, encoding="utf-8"))
    w = d.get("weights") or {}
    return {k: float(v) for k, v in w.items() if isinstance(v, (int, float))}


def build_custom_composite(X, M, Y, wmap, dim_max=DIM_MAX):
    """按权重合成总分（缺维从分母剔除），复用核心实现。"""
    return F.composite(X, M, Y, wmap, dim_max)


def ic_on_domain(X, M, Y, wmap, RY, pool_mask):
    """在自定义域掩码上算组合 IC 序列。

    pool_mask: (nD, nC) bool，True = 该截面该股票在池内。
    ⚠️ M 为 (nD, nC, nK) 三维，pool_mask 必须广播到维轴上再取交集，
       直接 `M & pool_mask` 会因形状不匹配报 broadcast 错误。
    返回 list[float]
    """
    Mc = M & pool_mask[:, :, None]
    Xc = np.where(Mc, X, np.nan)
    S = build_custom_composite(Xc, Mc, Y, wmap)
    RS = F.rank_rows(S)
    return F.row_pearson_ranks(RS, RY, min_n=30)


def make_pool_mask(dates, codes, pit_pool, min_n=50):
    """把 pit_pool 的逐截面成分转成 (nD, nC) 掩码。"""
    code_idx = {c: j for j, c in enumerate(codes)}
    nD, nC = len(dates), len(codes)
    mask = np.zeros((nD, nC), dtype=bool)
    used = []
    for di, dt in enumerate(dates):
        members = pit_pool.get(dt)
        if not members:
            continue
        js = [code_idx[c] for c in members if c in code_idx]
        if len(js) < min_n:
            continue
        mask[di, js] = True
        used.append(di)
    return mask, used


def layer_means_domain(X, M, Y, wmap, pool_mask, idxs, q=5):
    """域内分五档（按 composite 总分升序），返回各档 Y 均值与 spread。"""
    Mc = M & pool_mask[:, :, None]
    Xc = np.where(Mc, X, np.nan)
    S = build_custom_composite(Xc, Mc, Y, wmap)
    bucket = [[] for _ in range(q)]
    for di in idxs:
        s = S[di]
        y = Y[di]
        ok = np.isfinite(s) & np.isfinite(y) & pool_mask[di]
        if ok.sum() < q * 10:
            continue
        ss, yy = s[ok], y[ok]
        order = np.argsort(ss)
        yy_s = yy[order]
        n = len(yy_s)
        for b in range(q):
            lo, hi = int(n * b / q), int(n * (b + 1) / q)
            if hi > lo:
                bucket[b].append(float(np.mean(yy_s[lo:hi])))
    means = [float(np.mean(v)) if v else None for v in bucket]
    if any(m is None for m in means):
        return means, None
    return means, means[-1] - means[0]


def run_p13(X, M, Y, dates, codes, RY):
    print("\n" + "=" * 78)
    print("P1-3  接入 round91 逐截面重建池，重算因子与组合表现")
    print("=" * 78)

    pit = json.load(open(PIT_POOL, encoding="utf-8"))
    pools = pit.get("pools") or {}
    print(f"  pit_pool 截面数: {len(pools)}")

    mask, used = make_pool_mask(dates, codes, pools)
    if not used:
        print("  ⚠️ 无可比截面，跳过")
        return {"error": "no comparable dates"}
    sizes = [int(mask[di].sum()) for di in used]
    print(f"  可比截面: {len(used)}  池规模 median={int(np.median(sizes))} "
          f"min={min(sizes)} max={max(sizes)}")

    pit_dates = [dates[i] for i in used]
    print(f"  区间: {pit_dates[0]} ~ {pit_dates[-1]}")

    w415 = {k: v for k, v in load_w_v415().items() if k in DIMS}
    # 归一化（factor_weights.json 里 Σ 可能含已归零维）
    tot = sum(w415.values())
    w415 = {k: v / tot for k, v in w415.items()} if tot > 0 else w415

    # 对照方案
    stable = ["score_fund", "score_quality", "score_size_growth"]
    schemes = {
        "v4.15": w415,
        "等权9维": {d: 1.0 / len(DIMS) for d in DIMS},
        "等权3稳定维": {d: 1.0 / len(stable) for d in stable},
        "仅size_growth": {"score_size_growth": 1.0},
        "仅quality": {"score_quality": 1.0},
        "仅fund": {"score_fund": 1.0},
        "仅ep": {"score_ep": 1.0},
        "仅lowvol20": {"score_lowvol20": 1.0},
    }

    rows = []
    print(f"\n  {'方案':<16s} {'n':>4s} {'IC均值':>9s} {'ICIR':>8s} {'t':>7s} "
          f"{'Q5-Q1':>9s} {'单调':>5s}")
    for name, w in schemes.items():
        ics = ic_on_domain(X, M, Y, w, RY, mask)
        st = F.stat(ics)
        means, spread = layer_means_domain(X, M, Y, w, mask, used)
        mono = (means is not None and all(
            means[i] < means[i + 1] for i in range(len(means) - 1)))
        iс = st.get("ic")
        print(f"  {name:<16s} {st['n']:>4d} "
              f"{(iс if iс is not None else float('nan')):>9.4f} "
              f"{(st['icir'] if st['icir'] is not None else float('nan')):>8.4f} "
              f"{(st['t'] if st['t'] is not None else float('nan')):>7.2f} "
              f"{(spread if spread is not None else float('nan')):>9.5f} "
              f"{'Y' if mono else 'N':>5s}")
        rows.append({"scheme": name, "n": st["n"], "ic": iс,
                     "icir": st["icir"], "t": st["t"],
                     "layer_means": means, "spread": spread,
                     "monotonic": bool(mono),
                     "weights": {k: round(v, 6) for k, v in w.items() if v > 0}})

    # 域内 vs 原全池 对照（v4.15）
    ics_dom = ic_on_domain(X, M, Y, w415, RY, mask)
    ics_all = F.combo_ics(X, M, Y, w415, DIM_MAX, RY)
    st_dom, st_all = F.stat(ics_dom), F.stat(ics_all)
    print(f"\n  域效应（v4.15）：")
    print(f"    重建池域  IC={st_dom['ic']:>8.4f} ICIR={st_dom['icir']:>7.4f} t={st_dom['t']:>6.2f}")
    print(f"    原2029全池 IC={st_all['ic']:>8.4f} ICIR={st_all['icir']:>7.4f} t={st_all['t']:>6.2f}")

    return {
        "n_dates": len(used),
        "date_range": [pit_dates[0], pit_dates[-1]],
        "pool_size": {"median": int(np.median(sizes)),
                      "min": int(min(sizes)), "max": int(max(sizes))},
        "schemes": rows,
        "domain_effect_v415": {"rebuild_pool": st_dom, "full_2029": st_all},
        "caveat": "重建池用当日量价重算（价格≥2/额≥1亿/市值≥25亿/剔动量前20%/上市≥60日）；"
                  "股本为跨期常数（无拆股调整）→ 历史市值有近似误差",
    }


def run_p11(X, M, Y, dates, codes, RY):
    print("\n" + "=" * 78)
    print("P1-1  符号翻转维度的权重压缩代价曲线")
    print("=" * 78)

    # regime 划分（事后，仅用于分层诊断；与记忆 R2 一致：不可事前利用）
    # 用池等权未来 h20 收益中位数划分
    ewY = np.nanmean(Y, axis=1)
    med = float(np.nanmedian(ewY))
    up_idx = [i for i in range(len(dates)) if np.isfinite(ewY[i]) and ewY[i] > med]
    dn_idx = [i for i in range(len(dates)) if np.isfinite(ewY[i]) and ewY[i] <= med]
    print(f"  regime（事后）：涨市 {len(up_idx)} 截面 / 跌市 {len(dn_idx)} 截面 "
          f"（中位 {med:.5f}）")

    flip = ["score_ep", "score_lowvol20", "score_rsi_rev", "score_tech_neg"]
    stable = ["score_fund", "score_quality", "score_size_growth"]
    w415 = load_w_v415()
    tot = sum(w415.values())
    w415 = {k: v / tot for k, v in w415.items()}

    w_flip_415 = sum(w415.get(d, 0.0) for d in flip)
    w_stab_415 = sum(w415.get(d, 0.0) for d in stable)
    print(f"  v4.15 结构：翻转维合计 {w_flip_415:.4f} / 稳定维合计 {w_stab_415:.4f}")

    # 其他维（bias60/dominant）在 v4.15 中占比很小，一并按压缩逻辑处理为「其他」
    other = [d for d in DIMS if d not in flip and d not in stable]
    w_other_415 = sum(w415.get(d, 0.0) for d in other)
    print(f"  其他维（bias60/rsi_rev 之外）合计 {w_other_415:.4f}")

    rows = []
    print(f"\n  {'翻转维权重':>10s} {'全样本ICIR':>11s} {'涨市ICIR':>9s} "
          f"{'跌市ICIR':>9s} {'spread':>9s} {'单调':>5s}")
    for target in [0.4244, 0.35, 0.30, 0.25, 0.20, 0.15, 0.10, 0.05, 0.0]:
        # 构造权重：翻转维按 v4.15 内部比例缩放到 target；
        # 释放的权重按 v4.15 内部比例分给稳定维；其他维保持 0（v4.15 中已很小）
        if w_flip_415 > 0:
            scale = target / w_flip_415
            w = {d: w415.get(d, 0.0) * scale for d in flip}
        else:
            w = {d: 0.0 for d in flip}
        freed = w_flip_415 - target
        if w_stab_415 > 0:
            add = freed / w_stab_415
            for d in stable:
                w[d] = w415.get(d, 0.0) * (1.0 + add) if (1.0 + add) > 0 else 0.0
        # 保 Σ=1
        s = sum(w.values())
        if s > 0:
            w = {k: v / s for k, v in w.items()}

        ics_all = F.combo_ics(X, M, Y, w, DIM_MAX, RY)
        st_all = F.stat(ics_all)
        ics_up = F.combo_ics_at(X, M, Y, w, DIM_MAX, RY, up_idx)
        ics_dn = F.combo_ics_at(X, M, Y, w, DIM_MAX, RY, dn_idx)
        st_up, st_dn = F.stat(ics_up), F.stat(ics_dn)
        means, spread = layer_means_domain(
            X, M, Y, w, np.ones((M.shape[0], M.shape[1]), dtype=bool),
            list(range(len(dates))))
        mono = (means is not None and all(
            means[i] < means[i + 1] for i in range(len(means) - 1)))

        print(f"  {target:>10.4f} "
              f"{(st_all['icir'] if st_all['icir'] is not None else float('nan')):>11.4f} "
              f"{(st_up['icir'] if st_up['icir'] is not None else float('nan')):>9.4f} "
              f"{(st_dn['icir'] if st_dn['icir'] is not None else float('nan')):>9.4f} "
              f"{(spread if spread is not None else float('nan')):>9.5f} "
              f"{'Y' if mono else 'N':>5s}")
        rows.append({
            "w_flip_total": target,
            "weights": {k: round(v, 6) for k, v in w.items() if v > 1e-9},
            "all": st_all, "up": st_up, "down": st_dn,
            "layer_means": means, "spread": spread, "monotonic": bool(mono),
        })

    # 找最优点（全样本 ICIR 最大 且 spread>0）
    best = None
    for r in rows:
        if r["spread"] is None or r["spread"] <= 0:
            continue
        if r["all"]["icir"] is None:
            continue
        if best is None or r["all"]["icir"] > best["all"]["icir"]:
            best = r
    if best:
        print(f"\n  ⇒ 最优（spread>0 约束下全样本 ICIR 最高）："
              f"翻转维权重 {best['w_flip_total']:.4f}, "
              f"ICIR={best['all']['icir']:.4f}, spread={best['spread']:.5f}")

    return {
        "regime_split": {"up": len(up_idx), "down": len(dn_idx), "median": med,
                         "caveat": "事后划分（用未来收益），仅诊断用；"
                                   "记忆 R2 已证 regime 不可事前利用"},
        "flip_dims": flip, "stable_dims": stable,
        "v415_structure": {"flip": w_flip_415, "stable": w_stab_415, "other": w_other_415},
        "scan": rows,
        "best": best,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--task", default="all", choices=["all", "p13", "p11"])
    args = ap.parse_args()

    t0 = time.time()
    dates, X, M, Y, codes = F.load_panel_arrays(PANEL, NPZ)
    dates = [str(d) for d in dates]
    codes = [str(c) for c in codes]
    RY = F.rank_rows(Y)
    print(f"面板 {X.shape}  {dates[0]}~{dates[-1]}")

    out = {"meta": {"round": 100,
                    "purpose": "P1-3 接入重建池 + P1-1 翻转维度压缩代价曲线",
                    "npz": "data/val_arrays_h20.npz",
                    "pit_pool": "data/pit_pool.json",
                    "built_at": time.strftime("%Y-%m-%d %H:%M:%S")}}

    if args.task in ("all", "p13"):
        out["p13_rebuild_pool"] = run_p13(X, M, Y, dates, codes, RY)
    if args.task in ("all", "p11"):
        out["p11_flip_compression"] = run_p11(X, M, Y, dates, codes, RY)

    out["elapsed_s"] = round(time.time() - t0, 1)
    json.dump(_jsonable(out), open(OUT, "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    print(f"\n→ {OUT}  耗时 {out['elapsed_s']}s")


if __name__ == "__main__":
    main()
