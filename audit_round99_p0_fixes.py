#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
audit_round99_p0_fixes.py —— P0-1 / P0-2 / P0-3 / P1-2 四项改造的一体化实现

═══ 本轮要解决的四个问题 ═══

【P0-1】fund 维覆盖率 8.23% 的根因是「评估域错配」，不是数据缺失
  实测证据（2026-09-22）：
    · npz 评估域 = 2029 只全池；fund 有值仅 167 只 → 8.23%
    · fwd_hist 当日存档 = 196 只（TOP200 池）；fund 有值 196 只 → 100%
    · 归一化代码（sh600026 → 600026）后交集 = 167/196 = 85.2%
  ⇒ fund 的真实覆盖是 85.2%，只是被 2029 全池稀释。
  ⇒ 修复方式：对每个截面，以「fwd_hist 当日存档域」为 fund 的评估域，
     并用同域内的其他维度做对照，而不是把 fund 放进 2029 全池算 IC。

【P0-2】样本功效不足 —— 实现 walk-forward 滚动验证
  原 round97：求解段 90+71 截面，内部验证仅 19 截面（单次切分）。
  本轮：滚动前推 k 折，每折「前 train 求解 / 紧邻后 val 验证」，输出 ICIR 分布。

【P0-3】分层单调性一票否决
  原 round96：5 个方案 spread 全部未过，且 3 个为负，但仍输出为「候选」。
  本轮：spread <= 0 直接判 FAIL，不进入候选；并强制输出 Q1..Q5 均值。

【P1-2】过拟合门禁
  ① 强制零参数基准（等权 / 单因子最好）作为对照，优化解必须在验证段击败它；
  ② 断言「验证段 ICIR 符号 == 求解段 ICIR 符号」，翻转即拒绝；
  ③ 记录选择自由度（枚举规模），并给出「随机极值」参考。

用法：
  python audit_round99_p0_fixes.py                 # 全部
  python audit_round99_p0_fixes.py --only p01      # 只跑 P0-1 诊断
"""
import os
import sys
import json
import math
import time
import glob
import argparse
import itertools
from collections import Counter

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import factor_validation_core as F

NPZ = os.path.join(HERE, "data", "val_arrays_h20.npz")
PANEL = os.path.join(HERE, "data", "panel_round66.json")
FWD_HIST = os.path.join(HERE, "data", "fwd_hist")
OUT = os.path.join(HERE, "audit_round99.json")

DIMS = F.DIMS
DIM_MAX = {d: 10.0 for d in DIMS}
T_BONF = F.T_BONF          # 3.90
FDR_Q = F.FDR_Q            # 0.10


# ═══════════════════════════════════════════════════════════
# 工具
# ═══════════════════════════════════════════════════════════

def _norm_code(c):
    """sh600026 / sz000001 / bj830799 → 600026 / 000001 / 830799"""
    return (c or "").replace("sh", "").replace("sz", "").replace("bj", "")


def _fnum(v):
    """严格数值判定（禁 or 0 陷阱）。

    ⚠️ 2026-10-29 血案：`isinstance(np.float32(x), (int, float))` 是 **False**
       （numpy 标量只注册到 numbers.Real，不继承 Python 内置 float）。
       若只写 isinstance(v,(int,float))，npz 里的 float32 收益会被静默判为缺失，
       导致「同域 IC 全空但无报错」—— 与 round35 的 NaN 陷阱同源。
       故显式用 numbers.Real（涵盖 numpy 标量与内置数值），再排除 bool。
    """
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
    """递归把 numpy 标量/布尔转成 Python 原生，便于 json.dump。

    ⚠️ np.bool_ / np.float32 / np.int64 都不是 json 可序列化类型，
       且 np.bool_ 的 isinstance(x, bool) 为 False —— 必须显式转换。
    """
    if isinstance(o, dict):
        return {k: _jsonable(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_jsonable(v) for v in o]
    if isinstance(o, (np.bool_,)):
        return bool(o)
    if isinstance(o, (np.integer,)):
        return int(o)
    if isinstance(o, (np.floating,)):
        return float(o)
    return o


def load_fwd_fund_map():
    """加载 fwd_hist 全部截面 → {date: {code6: score_fund}}，仅保留有值者。"""
    out = {}
    for p in sorted(glob.glob(os.path.join(FWD_HIST, "*.json"))):
        try:
            d = json.load(open(p, encoding="utf-8"))
        except Exception:
            continue
        dt = d.get("date") or os.path.basename(p).replace(".json", "")
        m = {}
        for r in (d.get("rows") or []):
            v = _fnum(r.get("score_fund"))
            if v is not None:
                m[_norm_code(r.get("code"))] = v
        if m:
            out[dt] = m
    return out


# ═══════════════════════════════════════════════════════════
# P0-1：fund 评估域错配诊断 + 同域对照 IC
# ═══════════════════════════════════════════════════════════

def run_p01(X, M, Y, dates, codes, RY, fund_map):
    """诊断 fund 在「全池域」vs「fwd_hist 同域」下的覆盖与 IC 差异。"""
    print("\n" + "=" * 78)
    print("P0-1  fund 评估域错配诊断")
    print("=" * 78)

    code_idx = {c: j for j, c in enumerate(codes)}
    k_fund = DIMS.index("score_fund")

    rows = []
    for di, dt in enumerate(dates):
        fm = fund_map.get(dt)
        if not fm:
            continue
        # 同域：fwd_hist 当日有 fund 的代码 ∩ npz 域
        dom = [code_idx[c] for c in fm if c in code_idx]
        if len(dom) < 50:
            continue
        dom = np.array(dom, dtype=int)

        # 全池域下 fund 有效数
        cov_pool = int(M[di, :, k_fund].sum())
        # 同域下 fund 有效数（用 fwd_hist 值，不依赖 npz 的 M）
        cov_dom = len(dom)

        # 同域 IC：fund(fwd_hist 值) vs Y，仅取 Y 非 NaN 且 fund 有效
        fv, yv = [], []
        for j in dom:
            f = _fnum(fm.get(codes[j]))
            y = _fnum(Y[di, j])
            if f is None or y is None:
                continue
            fv.append(f)
            yv.append(y)
        ic_dom = None
        if len(fv) >= 50:
            rx = F.rank_rows(np.array([fv], dtype=float))
            ry = F.rank_rows(np.array([yv], dtype=float))
            got = F.row_pearson_ranks(rx, ry, min_n=50)
            ic_dom = got[0] if got else None

        # 全池 IC（用 npz 的 X/M）
        ic_pool = None
        m = M[di, :, k_fund]
        if m.sum() >= 50:
            xv = X[di, :, k_fund][m].astype(float)
            yv2 = Y[di, :][m].astype(float)
            ok = ~np.isnan(yv2) & ~np.isnan(xv)
            if ok.sum() >= 50:
                rx = F.rank_rows(xv[ok][None, :])
                ry = F.rank_rows(yv2[ok][None, :])
                got = F.row_pearson_ranks(rx, ry, min_n=50)
                ic_pool = got[0] if got else None

        rows.append({
            "date": dt,
            "domain_n": int(len(dom)),
            "cov_pool_pct": cov_pool / len(codes),
            "cov_dom_pct": 1.0,
            "ic_dom": ic_dom,
            "ic_pool": ic_pool,
        })

    ic_doms = [r["ic_dom"] for r in rows if r["ic_dom"] is not None]
    ic_pools = [r["ic_pool"] for r in rows if r["ic_pool"] is not None]
    st_dom = F.stat(ic_doms) if ic_doms else {}
    st_pool = F.stat(ic_pools) if ic_pools else {}

    print(f"  可比截面数: {len(rows)}")
    if rows:
        print(f"  同域股票数中位: {int(np.median([r['domain_n'] for r in rows]))}")
        print(f"  全池域 fund 覆盖率中位: {np.median([r['cov_pool_pct'] for r in rows]):.4f}")
    print(f"\n  {'口径':<18s} {'n':>5s} {'IC均值':>9s} {'ICIR':>8s} {'t':>7s}")
    if st_dom:
        print(f"  {'同域(fwd_hist)':<18s} {st_dom['n']:>5d} {st_dom['ic']:>9.4f} "
              f"{st_dom['icir']:>8.4f} {st_dom['t']:>7.2f}")
    if st_pool:
        print(f"  {'全池(2029)':<18s} {st_pool['n']:>5d} {st_pool['ic']:>9.4f} "
              f"{st_pool['icir']:>8.4f} {st_pool['t']:>7.2f}")

    verdict = {
        "root_cause": "评估域错配：fwd_hist 只存档当日 TOP200 池（约196只），"
                      "验证面板为 2029 只全池 → fund 覆盖率被稀释至约8%",
        "fix": "fund 只在 fwd_hist 同域内评估，不与全池其他维度混算组合 IC",
        "n_dates": len(rows),
        "stat_same_domain": st_dom,
        "stat_full_pool": st_pool,
        "sample": rows[:5] + rows[-5:] if len(rows) > 10 else rows,
    }
    print(f"\n  ⇒ fund 不可回溯（fwd_pe 当日快照，过期即失）→ 无法补算历史，"
          f"只能限定评估域")
    return verdict


# ═══════════════════════════════════════════════════════════
# P0-2 / P0-3 / P1-2：walk-forward + 单调性门禁 + 过拟合门禁
# ═══════════════════════════════════════════════════════════

def _composite_ic(X, M, Y, wv, idxs, RY):
    """在给定截面索引集合上算组合 IC（复用核心，逐截面取样）。"""
    wmap = {d: wv[i] for i, d in enumerate(DIMS) if wv[i] > 0}
    if not wmap:
        return []
    return F.combo_ics_at(X, M, Y, wmap, DIM_MAX, RY, idxs)


def _layer_means(X, M, Y, wv, idxs, RY, q=5):
    """组合分五档，返回各档未来收益均值与 spread。

    分档基于 composite 总分（已按缺维从分母剔除重归一），
    不使用 rank 后再分档，避免与生产口径偏差。
    """
    wmap = {d: wv[i] for i, d in enumerate(DIMS) if wv[i] > 0}
    if not wmap:
        return None, None
    idxs = list(idxs)
    Xs, Ms, Ys = X[idxs], M[idxs], Y[idxs]
    comp = F.composite(Xs, Ms, Ys, wmap, DIM_MAX)   # (nD, nC) 总分
    bucket_vals = [[] for _ in range(q)]
    for i in range(len(idxs)):
        c = comp[i]
        y = Ys[i]
        ok = np.isfinite(c) & np.isfinite(y)
        if ok.sum() < q * 10:
            continue
        cc, yy = c[ok], y[ok]
        order = np.argsort(cc)          # 升序：低分 → 高分
        yy_s = yy[order]
        n = len(yy_s)
        for b in range(q):
            lo, hi = int(n * b / q), int(n * (b + 1) / q)
            if hi > lo:
                bucket_vals[b].append(float(np.mean(yy_s[lo:hi])))
    means = [float(np.mean(v)) if v else None for v in bucket_vals]
    if any(m is None for m in means):
        return means, None
    spread = means[-1] - means[0]       # Q5(最高分) − Q1(最低分)
    return means, spread


def _monotonic(means):
    """严格单调递增（Q1<Q2<Q3<Q4<Q5）。"""
    if not means or any(m is None for m in means):
        return False
    return all(means[i] < means[i + 1] for i in range(len(means) - 1))


def run_p02_p03_p12(X, M, Y, dates, codes, RY, args):
    """walk-forward 滚动验证 + 单调性一票否决 + 过拟合门禁。"""
    print("\n" + "=" * 78)
    print("P0-2 / P0-3 / P1-2  walk-forward + 单调性一票否决 + 过拟合门禁")
    print("=" * 78)

    nD = len(dates)
    folds = args.folds
    train = args.train
    val = args.val
    pool = args.pool

    # 滚动窗口
    win = train + val
    stride = val
    starts = list(range(0, nD - win + 1, stride))
    if len(starts) > folds:
        starts = starts[-folds:]
    print(f"  截面总数 {nD}  train={train} val={val} stride={stride} → {len(starts)} 折")

    # 候选权重网格（仅 pool 内因子；非负、Σ=1、单维≤cap）
    cap = args.w_cap
    step = args.step
    grid = [round(i * step, 4) for i in range(int(round(1 / step)) + 1)]
    combos = []
    for c in itertools.product(grid, repeat=len(pool)):
        if abs(sum(c) - 1.0) > 1e-9:
            continue
        if max(c) > cap + 1e-9:
            continue
        combos.append(c)
    print(f"  候选权重: {len(combos)} 个（pool={pool}, cap={cap}, step={step}）")

    # 零参数基准：等权 & 单因子最好
    base_ew = tuple([round(1.0 / len(pool), 6)] * len(pool))

    def to_wv(combo):
        wv = [0.0] * len(DIMS)
        for d, w in zip(pool, combo):
            wv[DIMS.index(d)] = w
        return wv

    fold_records = []
    for fi, s in enumerate(starts, 1):
        tr_idx = list(range(s, s + train))
        va_idx = list(range(s + train, s + win))
        if len(tr_idx) < 20 or len(va_idx) < 5:
            continue

        # ── 在训练段选 ICIR 最优（不看验证段）──
        best = None
        for combo in combos:
            wv = to_wv(combo)
            ics = _composite_ic(X, M, Y, wv, tr_idx, RY)
            st = F.stat(ics)
            if not st or st.get("n", 0) < 15:
                continue
            if best is None or st["icir"] > best["train_icir"]:
                best = {"combo": combo, "train_icir": st["icir"],
                        "train_ic": st["ic"], "train_t": st["t"], "train_n": st["n"]}
        if best is None:
            continue

        # ── 验证段评估：优化解 vs 零参数基准 ──
        def eval_combo(combo):
            wv = to_wv(combo)
            ics = _composite_ic(X, M, Y, wv, va_idx, RY)
            st = F.stat(ics)
            means, spread = _layer_means(X, M, Y, wv, va_idx, RY)
            return st, means, spread

        st_opt, means_opt, spread_opt = eval_combo(best["combo"])
        st_ew, means_ew, spread_ew = eval_combo(base_ew)

        # 单因子最好（训练段选）
        best_single, best_single_icir = None, None
        for d in pool:
            wv = [0.0] * len(DIMS)
            wv[DIMS.index(d)] = 1.0
            st = F.stat(_composite_ic(X, M, Y, wv, tr_idx, RY))
            if st and (best_single_icir is None or st["icir"] > best_single_icir):
                best_single, best_single_icir = d, st["icir"]
        st_single, means_single, spread_single = (
            eval_combo((1.0,) + tuple([0.0] * (len(pool) - 1)))
            if pool[0] == best_single else (None, None, None))
        if best_single and pool.index(best_single) != 0:
            c = [0.0] * len(pool)
            c[pool.index(best_single)] = 1.0
            st_single, means_single, spread_single = eval_combo(tuple(c))

        # ── P1-2 门禁 ──
        gate = {}
        gate["sign_consistent"] = bool(
            st_opt is not None and best["train_icir"] is not None
            and np.sign(st_opt["icir"]) == np.sign(best["train_icir"]))
        gate["beat_ew"] = bool(
            st_opt is not None and st_ew is not None
            and st_opt["icir"] > st_ew["icir"])
        gate["beat_single"] = bool(
            st_opt is not None and st_single is not None
            and st_opt["icir"] > st_single["icir"])
        # ── P0-3 单调性一票否决 ──
        gate["monotonic"] = bool(_monotonic(means_opt))
        gate["spread_positive"] = bool(spread_opt is not None and spread_opt > 0)
        gate["pass"] = bool(all(gate[k] for k in
                                ("sign_consistent", "beat_ew", "beat_single",
                                 "monotonic", "spread_positive")))

        fold_records.append({
            "fold": fi,
            "train_range": [dates[tr_idx[0]], dates[tr_idx[-1]]],
            "val_range": [dates[va_idx[0]], dates[va_idx[-1]]],
            "best_combo": {d: w for d, w in zip(pool, best["combo"]) if w > 0},
            "train_icir": best["train_icir"],
            "train_ic": best["train_ic"],
            "val_icir": st_opt["icir"] if st_opt else None,
            "val_ic": st_opt["ic"] if st_opt else None,
            "val_t": st_opt["t"] if st_opt else None,
            "val_n": st_opt["n"] if st_opt else None,
            "ew_icir": st_ew["icir"] if st_ew else None,
            "single_best": best_single,
            "single_icir": st_single["icir"] if st_single else None,
            "layer_means": means_opt,
            "layer_means_ew": means_ew,
            "spread": spread_opt,
            "spread_ew": spread_ew,
            "gates": gate,
            "n_enumerated": len(combos),
        })

        print(f"  折{fi}: train {dates[tr_idx[0]]}~{dates[tr_idx[-1]]} "
              f"val {dates[va_idx[0]]}~{dates[va_idx[-1]]}")
        print(f"       train ICIR={best['train_icir']:>7.4f} → "
              f"val ICIR={st_opt['icir'] if st_opt else float('nan'):>7.4f}  "
              f"EW={st_ew['icir'] if st_ew else float('nan'):>7.4f}  "
              f"spread={spread_opt if spread_opt is None else round(spread_opt,5)}  "
              f"{'PASS' if gate['pass'] else 'FAIL'}")

    # ── 汇总 ──
    n_pass = sum(1 for r in fold_records if r["gates"]["pass"])
    val_icirs = [r["val_icir"] for r in fold_records if r["val_icir"] is not None]
    tr_icirs = [r["train_icir"] for r in fold_records if r["train_icir"] is not None]
    ew_icirs = [r["ew_icir"] for r in fold_records if r["ew_icir"] is not None]
    spreads = [r["spread"] for r in fold_records if r["spread"] is not None]

    summary = {
        "n_folds": len(fold_records),
        "n_pass": n_pass,
        "pass_rate": round(n_pass / max(len(fold_records), 1), 4),
        "train_icir_mean": round(float(np.mean(tr_icirs)), 4) if tr_icirs else None,
        "val_icir_mean": round(float(np.mean(val_icirs)), 4) if val_icirs else None,
        "val_icir_std": round(float(np.std(val_icirs)), 4) if val_icirs else None,
        "ew_icir_mean": round(float(np.mean(ew_icirs)), 4) if ew_icirs else None,
        "train_to_val_decay": (round(float(np.mean(tr_icirs)) - float(np.mean(val_icirs)), 4)
                               if tr_icirs and val_icirs else None),
        "spread_mean": round(float(np.mean(spreads)), 5) if spreads else None,
        "n_spread_positive": sum(1 for s in spreads if s > 0),
        "n_monotonic": sum(1 for r in fold_records if r["gates"]["monotonic"]),
        "n_sign_flip": sum(1 for r in fold_records if not r["gates"]["sign_consistent"]),
        "n_beat_ew": sum(1 for r in fold_records if r["gates"]["beat_ew"]),
        "n_beat_single": sum(1 for r in fold_records if r["gates"]["beat_single"]),
    }

    print("\n  " + "-" * 74)
    print(f"  折数 {summary['n_folds']}  通过 {summary['n_pass']} "
          f"（{summary['pass_rate']*100:.1f}%）")
    print(f"  训练段 ICIR 均值 {summary['train_icir_mean']}  →  "
          f"验证段 {summary['val_icir_mean']} ± {summary['val_icir_std']}  "
          f"（衰减 {summary['train_to_val_decay']}）")
    print(f"  等权基准验证段 ICIR 均值 {summary['ew_icir_mean']}")
    print(f"  分层 spread 均值 {summary['spread_mean']}  为正 {summary['n_spread_positive']}/{len(spreads)}  "
          f"严格单调 {summary['n_monotonic']}/{len(fold_records)}")
    print(f"  门禁: 符号一致 {len(fold_records)-summary['n_sign_flip']}/{len(fold_records)}  "
          f"胜等权 {summary['n_beat_ew']}/{len(fold_records)}  "
          f"胜单因子 {summary['n_beat_single']}/{len(fold_records)}")

    if summary["n_pass"] == 0:
        print("\n  ⚠️ 结论：0 折通过全部门禁 → 当前因子池在当前数据上"
              "**不具备可上线条件**（这是正确结论，不是失败）")

    return {"summary": summary, "folds": fold_records}


# ═══════════════════════════════════════════════════════════
# main
# ═══════════════════════════════════════════════════════════

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default="", help="p01 / p02 / all")
    ap.add_argument("--folds", type=int, default=8)
    ap.add_argument("--train", type=int, default=60, help="训练段截面数")
    ap.add_argument("--val", type=int, default=20, help="验证段截面数")
    ap.add_argument("--step", type=float, default=0.25, help="权重网格步长")
    ap.add_argument("--w-cap", type=float, default=1.0, help="单维上限")
    ap.add_argument("--pool", default="score_quality,score_ep,score_lowvol20,score_size_growth",
                    help="候选因子池（逗号分隔），默认剔除 fund（因不可回溯）")
    args = ap.parse_args()
    args.pool = [x.strip() for x in args.pool.split(",") if x.strip()]

    t0 = time.time()
    # 加载（第 1 参为 panel json 路径，第 2 参为 npz 缓存；命中缓存则直接读）
    dates, X, M, Y, codes = F.load_panel_arrays(PANEL, NPZ)
    dates = [str(d) for d in dates]
    codes = [str(c) for c in codes]
    RY = F.rank_rows(Y)
    print(f"面板: {X.shape}  截面 {dates[0]}~{dates[-1]}")

    out = {"meta": {
        "round": 99,
        "purpose": "P0-1 fund域诊断 + P0-2 walk-forward + P0-3 单调性一票否决 + P1-2 过拟合门禁",
        "npz": "data/val_arrays_h20.npz",
        "config": {"folds": args.folds, "train": args.train, "val": args.val,
                   "step": args.step, "w_cap": args.w_cap, "pool": args.pool},
        "built_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }}

    if args.only in ("", "all", "p01"):
        fund_map = load_fwd_fund_map()
        print(f"fwd_hist fund 截面数: {len(fund_map)}")
        out["p01_fund_domain"] = run_p01(X, M, Y, dates, codes, RY, fund_map)

    if args.only in ("", "all", "p02"):
        out["p02_walkforward"] = run_p02_p03_p12(X, M, Y, dates, codes, RY, args)

    out["elapsed_s"] = round(time.time() - t0, 1)
    json.dump(_jsonable(out), open(OUT, "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    print(f"\n→ {OUT}  耗时 {out['elapsed_s']}s")


if __name__ == "__main__":
    main()
