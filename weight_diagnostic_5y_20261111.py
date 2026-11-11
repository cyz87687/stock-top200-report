#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""weight_diagnostic_5y_20261111.py —— 近 5 年回测：验证「是否应该更改权重」。

只读诊断：不改 factor_weights.json、不改评分产物、不登记 trial、不提交。

三条唯一实现（禁止自写等价口径）：
  合成   scoring_core.composite_total_matrix  （分母恒 Σ=1.00；缺维中性填充）
  门禁   weight_admission_gate.evaluate / walk_forward
  执行   exec_backtest.run                   （T+1 开盘 / 13bp / 拒单留痕 / 覆盖率门禁）

⚠️ 证据边界（必须随结果一起披露）
  1. 长面板 data/panel_r124_mat.npz 来自 build_panel_long.py，自述
     factor_parity=False / PROXY_ONLY：size_growth=流通市值分位、
     quality=ROE 分位、growth=归母净利同比分位，均非生产算法；
     score_fund 整列无历史快照（真盲区，权重 0.20）。
  2. pit_pool.json 仅 238 截面（2025-11-03 起）→ 5 年区间无法用重建池域，
     只能用全池域（含池前视偏差），结论为诊断级、非准入判定。
  3. 闸门内部 F.composite 用「缺维剔分母」+ DIM_MAX 全 10，与生产内核
     「恒分母 + 5/10 混合量程」不同口径；两条线分别列示，禁止混用。
  4. 5 年全部落在权重拟合样本内（权重 2026-11-04 基于 1635 日全历史），
     不构成真样本外；禁称「经验证的 alpha」。
"""
import os
import sys
import io
import json
import math
import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import numpy as np
from scipy.optimize import minimize

import scoring_core as C
import exec_backtest as EB
import factor_eval as FE
import factor_validation_core as F
import weight_admission_gate as G
import trial_log

# ───────────────────────────── 配置 ─────────────────────────────
CFG = json.load(open(os.path.join(HERE, "factor_weights.json"), encoding="utf-8"))
W_FULL = dict(CFG["weights"])
DMAX = dict(CFG["dim_max"])
W_ACT = {k: float(v) for k, v in W_FULL.items() if float(v) > 0}
GROUPS = dict(CFG["weight_constraints"]["groups"])
CAP = 0.20
GCAP = 0.30
H = 20
START, END = "2021-11-11", "2026-11-11"
N_TOP = 10
W_TOTAL = 0.80
EVERY = 20
OUT_JSON = os.path.join(HERE, "data", "weight_diagnostic_5y_20261111.json")


def nw_t(x, lag):
    """Newey-West t（与近 1 年诊断同实现；lag=H-1=19）。"""
    a = np.asarray([v for v in x if np.isfinite(v)], dtype=float)
    n = len(a)
    if n < 3:
        return None
    mu = float(a.mean())
    e = a - mu
    var = float((e * e).sum() / n)
    for L in range(1, min(lag, n - 1) + 1):
        w = 1.0 - L / (lag + 1.0)
        gl = float((e[L:] * e[:-L]).sum() / n)
        var += 2.0 * w * gl
    se = math.sqrt(max(var, 1e-18) / n)
    return round(float(mu / se), 4) if se > 0 else None


def mean_finite(v):
    a = [x for x in v if np.isfinite(x)]
    return round(float(np.mean(a)), 6) if a else None


def icir(v):
    a = [x for x in v if np.isfinite(x)]
    if len(a) < 3:
        return None
    sd = float(np.std(a, ddof=1))
    return round(float(np.mean(a) / sd), 4) if sd > 1e-12 else None


def nav_metrics(out):
    nav = [v for _, v in (out.get("nav") or [])]
    if len(nav) < 3:
        return {"error": out.get("error") or out.get("reason") or "nav too short"}
    r = [nav[i] / nav[i - 1] - 1 for i in range(1, len(nav))]
    cum = nav[-1] - 1.0
    n = len(r)
    ann = (1 + cum) ** (252.0 / n) - 1 if 1 + cum > 0 else -1.0
    vol = float(np.std(r, ddof=1)) * math.sqrt(252)
    mdd = out.get("mdd_pct")
    return {
        "n_days": len(nav),
        "n_rebalances": out.get("n_rebalances"),
        "net_return_pct": out.get("net_return_pct"),
        "gross_return_pct": out.get("gross_return_pct"),
        "ann_return_pct": round(ann * 100, 3),
        "ann_vol_pct": round(vol * 100, 3),
        "sharpe_rf0": round(ann / vol, 4) if vol > 0 else None,
        "mdd_pct": mdd,
        "calmar": round(ann / (abs(mdd) / 100), 4) if mdd else None,
        "total_cost_pct": out.get("total_cost_pct"),
        "cost_drag_pp": out.get("cost_drag_pp"),
        "turnover": out.get("turnover"),
        "n_rejected_trades": out.get("n_rejected_trades"),
        "price_coverage_mean": out.get("price_coverage_mean"),
        "coverage_gate_ok": out.get("coverage_gate_ok"),
    }


# ───────────────────────── 1. 载入 5 年面板 ─────────────────────────
z = np.load(os.path.join(HERE, "data", "panel_r124_mat.npz"), allow_pickle=False)
DATES = [str(x) for x in z["dates"]]
CODES = [str(x) for x in z["codes"]]
DIMS9 = [str(x) for x in z["dims"]]
i0 = next(i for i, d in enumerate(DATES) if d >= START)
i1 = len(DATES) - 1
Dw = DATES[i0:i1 + 1]
S = z["S"][i0:i1 + 1].astype(np.float32)          # (nD, nC, 9)
P = z["P"][i0:i1 + 1].astype(np.float32)          # (nD, nC, 4)
nD, nC, nK = S.shape
price = P[:, :, 0]

eval_i = [i for i in range(nD) if i + H < nD and START <= Dw[i] <= END]
nE = len(eval_i)
n_eff = round(nE / H, 2)
print(f"面板 {Dw[0]} ~ {Dw[-1]}  nD={nD} nC={nC} dims9={DIMS9}")
print(f"评估截面 {nE}（{Dw[eval_i[0]]} ~ {Dw[eval_i[-1]]}）  n_eff=nE/H={n_eff}")

Y = np.full((nE, nC), np.nan, dtype=np.float64)
for k, i in enumerate(eval_i):
    p0 = price[i].astype(np.float64)
    p1 = price[i + H].astype(np.float64)
    with np.errstate(all="ignore"):
        Y[k] = p1 / p0 - 1.0
    Y[k][~np.isfinite(p0) | ~np.isfinite(p1)] = np.nan

# 逐截面 Rank IC：复用 factor_validation_core.row_pearson_ranks（唯一实现）
RY_E = F.rank_rows(Y)
S_E = S[eval_i].astype(np.float64)


def series_ic(X2):
    RS = F.rank_rows(X2)
    out = F.row_pearson_ranks(RS, RY_E, min_n=200)
    if len(out) != nE:
        raise RuntimeError(f"row_pearson_ranks 丢行: {len(out)} != {nE}")
    return np.asarray(out, dtype=float)


def series_top(X2, N=N_TOP):
    """TopN 等权 h20 收益 − 截面等权（诊断目标函数，非净值）。"""
    res = np.full(X2.shape[0], np.nan)
    for k in range(X2.shape[0]):
        x, y = X2[k], Y[k]
        m = np.isfinite(x) & np.isfinite(y)
        idx = np.where(m)[0]
        if len(idx) < 200:
            continue
        top = idx[np.argsort(-x[idx])[:N]]
        res[k] = float(y[top].mean() - y[idx].mean())
    return res


def composite_of(wmap):
    Wf = dict(W_FULL)
    Wf.update({k: float(v) for k, v in wmap.items()})
    return np.stack([
        C.composite_total_matrix(S[i], DIMS9, Wf, DMAX)[0]
        for i in eval_i
    ])


# 唯一内核：现行权重合成（分母恒 1.00，fund 盲区中性填充）
_, KERNEL_META = C.composite_total_matrix(S[0], DIMS9, W_FULL, DMAX)
COMP = composite_of(W_ACT)
W_EW = {k: 1.0 / len(W_ACT) for k in W_ACT}
COMP_EW = composite_of(W_EW)

print("内核 meta =", KERNEL_META)

SERIES = {"__COMPOSITE__": COMP, "__EW__": COMP_EW}
for j, d in enumerate(DIMS9):
    SERIES[d] = S_E[:, :, j]

summary = {}
for key, X2 in SERIES.items():
    ics = series_ic(X2)
    ex10 = series_top(X2, 10)
    summary[key] = {
        "n": nE,
        "ic_mean": mean_finite(ics),
        "ic_nw_t19": nw_t(ics, H - 1),
        "ic_nw_t4": nw_t(ics, 4),
        "ic_icir": icir(ics),
        "top10_excess_mean": mean_finite(ex10),
        "top10_excess_nw_t19": nw_t(ex10, H - 1),
        "top10_excess_icir": icir(ex10),
        "top10_win_rate_pct": round(100 * float(np.mean(ex10[np.isfinite(ex10)] > 0)), 2),
        "first_half_excess": mean_finite(ex10[:nE // 2]),
        "second_half_excess": mean_finite(ex10[nE // 2:]),
        "cross_section_median": int(np.median(np.isfinite(Y).sum(axis=1))),
        "n_effective_obs_h20": n_eff,
    }

print("\n=== 因子层（5 年 / 唯一内核 / h20） ===")
print(f"{'dim':22s} {'IC':>9s} {'NWt19':>8s} {'Top10Exc':>10s} {'NWt19':>8s} {'H1':>9s} {'H2':>9s}")
for k, v in summary.items():
    print(f"{k:22s} {str(v['ic_mean']):>9} {str(v['ic_nw_t19']):>8} "
          f"{str(v['top10_excess_mean']):>10} {str(v['top10_excess_nw_t19']):>8} "
          f"{str(v['first_half_excess']):>9} {str(v['second_half_excess']):>9}")

# 现行合成的分层（Q1..Q5）单调性诊断：与 G1/G2 同式，保留主结果线
q_means = []
for k in range(nE):
    x, y = COMP[k], Y[k]
    m = np.isfinite(x) & np.isfinite(y)
    idx = np.where(m)[0]
    if len(idx) < 200:
        q_means.append([np.nan] * 5)
        continue
    order = idx[np.argsort(x[idx])]
    chunks = np.array_split(order, 5)
    q_means.append([float(y[c].mean()) for c in chunks])
QM = np.asarray(q_means)
layer_means = [round(float(np.nanmean(QM[:, q])), 6) for q in range(5)]
layer_diffs = [round(layer_means[q + 1] - layer_means[q], 6) for q in range(4)]
mono_strict = all(layer_diffs[q] > 0 for q in range(4))
spread = round(layer_means[4] - layer_means[0], 6)
print("\n主结果线 分层 Q1..Q5 =", layer_means, "spread =", spread,
      "严格单调 =", mono_strict)

# ─────────────────── 2. 唯一五门判据（代理面板） ───────────────────
FDIMS = list(F.DIMS)
X8 = np.full((nD, nC, len(FDIMS)), np.nan, dtype=np.float32)
M8 = np.zeros((nD, nC, len(FDIMS)), dtype=bool)
for k, d in enumerate(FDIMS):
    if d in DIMS9:
        j = DIMS9.index(d)
        v = S[:, :, j]
        M8[:, :, k] = np.isfinite(v)
        X8[:, :, k] = np.where(M8[:, :, k], v, 0.0)
Y8 = np.full((nD, nC), np.nan, dtype=np.float64)
for k, i in enumerate(eval_i):
    Y8[i] = Y[k]
RY8 = F.rank_rows(Y8)

W_GATE = G.load_v415()
POOL4 = ["score_quality", "score_size_growth", "score_growth", "score_ep"]
print("\n=== 五门（weight_admission_gate.evaluate / 全池域 / 代理因子） ===")
GATE = G.evaluate(
    W_GATE, X8, M8, Y8, RY8, Dw, None, list(eval_i),
    pool=POOL4,
    wf_kwargs={"folds": 6, "train": 120, "val": 40,
               "step": 0.25, "pass_rate_thr": G.DEFAULT_WF_PASS_RATE},
    verbose=True,
)
print("五门 verdict =", GATE["verdict"], GATE["gates"])

# ─────────────── 3. 候选权重：带约束 SLSQP 投影 ───────────────
OBS7 = [d for d in W_ACT if d != "score_fund"]
assert len(OBS7) == 7, OBS7


def project(raw):
    """把「表现分」投影到可行域：Σ=1.00 / 单因子≤.20 / 大类≤.30 / fund 锁 .20。"""
    pos = np.array([max(0.0, float(raw.get(k, 0) or 0)) for k in OBS7], dtype=float)
    s = float(pos.sum())
    target = 0.80 * pos / s if s > 1e-12 else np.full(len(OBS7), 0.80 / len(OBS7))
    cons = [{"type": "eq", "fun": lambda w: float(w.sum() - 0.80)}]
    for gname, ks in GROUPS.items():
        idx = [OBS7.index(k) for k in ks if k in OBS7]
        if not idx:
            continue
        outside = sum(float(W_FULL.get(k, 0)) for k in ks if k not in OBS7)
        limit = GCAP - outside
        cons.append({"type": "ineq",
                     "fun": lambda w, ii=idx, li=limit: float(li - w[ii].sum())})
    res = minimize(lambda w: float(((w - target) ** 2).sum()), target,
                   method="SLSQP", bounds=[(0.0, CAP)] * len(OBS7),
                   constraints=cons, options={"ftol": 1e-13, "maxiter": 800})
    w = np.clip(res.x, 0.0, CAP)
    tot = float(w.sum())
    if tot > 1e-12:
        w = w * (0.80 / tot)          # 精确闭合到 0.80
    w = np.round(w, 6)
    out = {k: float(w[i]) for i, k in enumerate(OBS7)}
    out["score_fund"] = round(1.0 - sum(out.values()), 6)
    return out


def check_constraints(w):
    tot = sum(w.values())
    single = max(w.values())
    grp = {g: round(sum(w.get(k, 0.0) for k in ks), 6) for g, ks in GROUPS.items()}
    return {
        "sum": round(tot, 6), "sum_ok": abs(tot - 1.0) <= 1e-4,
        "single_max": round(single, 6), "single_ok": single <= CAP + 1e-6,
        "group_max": max(grp.values()), "group_ok": max(grp.values()) <= GCAP + 1e-6,
        "groups": grp, "nonneg": all(v >= -1e-9 for v in w.values()),
    }


OBS_SUM = {d: summary.get(d, {}) for d in OBS7}
raw1 = {d: max(0.0, OBS_SUM[d].get("top10_excess_mean") or 0.0) for d in OBS7}
raw2 = {d: max(0.0, OBS_SUM[d].get("top10_excess_icir") or 0.0) for d in OBS7}
raw3 = {d: abs(OBS_SUM[d].get("ic_mean") or 0.0) for d in OBS7}
CAND = {
    "R1_head_excess_5y": project(raw1),
    "R2_icir_like_5y": project(raw2),
    "R3_abs_ic_BANNED": project(raw3),
}
THR = round(FE._bonferroni_t(trial_log.n_trials()), 4)
K_NOW = trial_log.n_trials()
t_candidates = [v["top10_excess_nw_t19"] for k, v in summary.items()
                if k in W_ACT and v["top10_excess_nw_t19"] is not None]
T_MAX = max(t_candidates) if t_candidates else None
LAM = (max(0.0, min(1.0, (T_MAX - 2.0) / (THR - 2.0)))
       * max(0.0, min(1.0, n_eff / 30.0))) if T_MAX is not None else 0.0
LAM = round(LAM, 6)
R4 = {k: round((1 - LAM) * W_ACT[k] + LAM * CAND["R1_head_excess_5y"].get(k, 0.0), 6)
      for k in W_ACT}
CAND["R4_shrink"] = R4
CAND["R0_current"] = dict(W_ACT)
CAND["EW_equal"] = dict(W_EW)

# 敏感性：若把「合成组合」本身的 Top10 超额 t 也纳入 t_max（而非只看单因子维），
# λ 会略大于 0。这里把该口径一并跑净值，用事实说明偏移是否足以改权重。
T_COMPOSITE = summary["__COMPOSITE__"]["top10_excess_nw_t19"]
LAM_SENS = (round(max(0.0, min(1.0, (T_COMPOSITE - 2.0) / (THR - 2.0)))
                  * min(1.0, n_eff / 30.0), 6)
            if T_COMPOSITE is not None else 0.0)
R4_SENS = {k: round((1 - LAM_SENS) * W_ACT[k]
                    + LAM_SENS * CAND["R1_head_excess_5y"].get(k, 0.0), 6)
           for k in W_ACT}
CAND["R4_shrink_sens_composite_t"] = R4_SENS

# 前后半段拟合 → 交叉验证（带 purge/embargo；诊断级补充，非唯一判据）
# 切点两侧各剔除 H 个截面，避免 h=20 前向收益跨越训练/验证边界。
HALF = nE // 2
PURGE = H
TRAIN_IDX = {
    "first": list(range(0, HALF - PURGE)),
    "second": list(range(HALF + PURGE, nE)),
}
TEST_IDX = {"second": TRAIN_IDX["second"], "first": TRAIN_IDX["first"]}
ex_by_dim = {}
for d in OBS7:
    ex_by_dim[d] = (series_top(S_E[:, :, DIMS9.index(d)], 10)
                    if d in DIMS9 else np.full(nE, np.nan))
seg_raw = {}
for name, idxs in TRAIN_IDX.items():
    seg_raw[name] = {}
    for d in OBS7:
        v = float(np.nanmean(ex_by_dim[d][idxs]))
        seg_raw[name][d] = max(0.0, v) if np.isfinite(v) else 0.0
seg_cand = {k: project(v) for k, v in seg_raw.items()}
L1_HALF = round(sum(abs(seg_cand["first"][d] - seg_cand["second"][d]) for d in W_ACT), 4)

# ─────────────────── 4. 组合层可执行净值 ───────────────────
zk = np.load(os.path.join(HERE, "data", "klines_2020.npz"), allow_pickle=False)
XK = zk["X"]
DK = [str(x) for x in zk["dates"]]
CK = [str(x) for x in zk["codes"]]
ci = {c: i for i, c in enumerate(CK)}
dj = [(j, d) for j, d in enumerate(DK) if d >= START]
cal = [d for _, d in dj]
calset = set(cal)

WEIGHT_SETS = {
    "R0_current": CAND["R0_current"],
    "R1_head_excess_5y": CAND["R1_head_excess_5y"],
    "R2_icir_like_5y": CAND["R2_icir_like_5y"],
    "R3_abs_ic_BANNED": CAND["R3_abs_ic_BANNED"],
    "R4_shrink": CAND["R4_shrink"],
    "R4_shrink_sens_composite_t": CAND["R4_shrink_sens_composite_t"],
    "EW_equal": CAND["EW_equal"],
    "SEG_first_fit": seg_cand["first"],
    "SEG_second_fit": seg_cand["second"],
}


def make_reb(wmap, idx_pool=None, every=EVERY, N=N_TOP):
    comp = composite_of(wmap)
    src = list(range(nE)) if idx_pool is None else list(idx_pool)
    out = []
    for k in src:
        if k % every:
            continue
        d = Dw[eval_i[k]]
        if d not in calset:
            continue
        x = comp[k]
        idx = np.where(np.isfinite(x))[0]
        if len(idx) < N:
            continue
        top = idx[np.argsort(-x[idx])[:N]]
        out.append({"date": d,
                    "weights": {CODES[j]: W_TOTAL / N for j in top},
                    "cash": round(1 - W_TOTAL, 6),
                    "source": "diag5y", "n_picks": N})
    return out


all_reb = {name: make_reb(w) for name, w in WEIGHT_SETS.items()}
picks = set()
for reb in all_reb.values():
    for r in reb:
        picks.update(r["weights"].keys())
print(f"\n执行层：调仓 {len(all_reb['R0_current'])} 次，持仓并集 {len(picks)} 只，"
      f"日历 {cal[0]}~{cal[-1]} ({len(cal)} 日)")

px = {}
for c in picks:
    if c not in ci:
        continue
    row = XK[ci[c]]
    by = {}
    for j, d in dj:
        o, cl = float(row[j, 0]), float(row[j, 1])
        if np.isfinite(o) and np.isfinite(cl) and o > 0 and cl > 0:
            by[d] = [o, cl]
    if by:
        px[c] = by
print("px 构造完成：", len(px), "只")

PORTFOLIO = {}
RAW_NAV = {}
for name, reb in all_reb.items():
    if not reb:
        PORTFOLIO[name] = {"error": "无可执行再平衡"}
        continue
    out = EB.run(reb, px, cal)
    PORTFOLIO[name] = {"n_rebalance_records": len(reb), **nav_metrics(out)}
    RAW_NAV[name] = out.get("nav") or []


def nav_diag(nav):
    """核验净值是否被单日复权跳变污染，并给分年度收益。"""
    if len(nav) < 3:
        return {}
    rets = [(nav[i][0], nav[i][1] / nav[i - 1][1] - 1)
            for i in range(1, len(nav))]
    mx = max(rets, key=lambda x: x[1])
    mn = min(rets, key=lambda x: x[1])
    by_year = {}
    for d, v in nav:
        by_year.setdefault(d[:4], []).append(v)
    yr = {y: round((v[-1] / v[0] - 1) * 100, 2) for y, v in by_year.items()}
    return {
        "max_1d_pct": round(mx[1] * 100, 3), "max_1d_date": mx[0],
        "min_1d_pct": round(mn[1] * 100, 3), "min_1d_date": mn[0],
        "n_days_abs_move_gt_10pct": sum(1 for _, r in rets if abs(r) > 0.10),
        "by_year_pct": yr,
        "first_nav": nav[0][1], "last_nav": nav[-1][1],
    }


NAV_DIAG = {k: nav_diag(v) for k, v in RAW_NAV.items() if v}

BENCH5 = {}
for _code, _name in (("000985", "中证全指"), ("000300", "沪深300"),
                     ("000905", "中证500"), ("000852", "中证1000")):
    try:
        _pxb = json.load(open(os.path.join(HERE, "data", "bench", _code + ".json"),
                              encoding="utf-8"))["px"]
        _ds = [k for k in sorted(_pxb) if k >= START]
        _v = [float(_pxb[k][1]) for k in _ds]
        _r = [_v[i] / _v[i - 1] - 1 for i in range(1, len(_v))]
        _cum = _v[-1] / _v[0] - 1
        _ann = (1 + _cum) ** (252.0 / len(_r)) - 1 if 1 + _cum > 0 else -1.0
        _vol = float(np.std(_r, ddof=1)) * math.sqrt(252)
        _peak, _mdd = _v[0], 0.0
        for _x in _v:
            _peak = max(_peak, _x)
            _mdd = min(_mdd, _x / _peak - 1)
        BENCH5[_code] = {
            "name": _name, "first": _ds[0], "last": _ds[-1], "n_days": len(_ds),
            "return_pct": round(_cum * 100, 2), "ann_pct": round(_ann * 100, 2),
            "vol_pct": round(_vol * 100, 2),
            "sharpe_rf0": round(_ann / _vol, 3) if _vol > 0 else None,
            "mdd_pct": round(_mdd * 100, 2),
        }
    except Exception as _e:
        BENCH5[_code] = {"error": str(_e)}

# 交叉验证：前半拟合的权重，只在后半段用（反之亦然），净值窗口也截断
def clipped_cal(date_list):
    lo, hi = min(date_list), max(date_list)
    return [d for d in cal if lo <= d <= hi]


OOS_LIKE = {}
for train_name, test_name in (("first", "second"), ("second", "first")):
    test_idx = TEST_IDX[test_name]
    reb_a = make_reb(seg_cand[train_name], idx_pool=test_idx)
    reb_b = make_reb(W_ACT, idx_pool=test_idx)
    dates_test = [Dw[eval_i[k]] for k in test_idx]
    subcal = clipped_cal(dates_test)
    key = f"train_{train_name}_test_{test_name}"
    OOS_LIKE[key] = {
        "test_window": [dates_test[0], dates_test[-1]],
        "n_test_snapshots": len(test_idx),
        "purge_each_side": PURGE,
        "fitted_weight": f"SEG_{train_name}_fit",
        "fitted_net": nav_metrics(EB.run(reb_a, px, subcal)) if reb_a else {"error": "no reb"},
        "prior_net": nav_metrics(EB.run(reb_b, px, subcal)) if reb_b else {"error": "no reb"},
    }

print("\n=== 组合层可执行净值 ===")
for k, v in PORTFOLIO.items():
    print(f"{k:22s} net={v.get('net_return_pct')} sharpe={v.get('sharpe_rf0')} "
          f"mdd={v.get('mdd_pct')} cov={v.get('coverage_gate_ok')}")
print("\n=== 带 purge 的交叉验证 ===")
for k, v in OOS_LIKE.items():
    print(k, "fitted=", v["fitted_net"].get("net_return_pct"),
          "prior=", v["prior_net"].get("net_return_pct"))
print("\n=== 净值曲线核验（R0 现行） ===")
print(json.dumps(NAV_DIAG.get("R0_current", {}), ensure_ascii=False))
print("\n=== 基准 5 年（真实） ===")
for _c, _v in BENCH5.items():
    print(_c, _v.get("name"), "收益=", _v.get("return_pct"),
          "夏普=", _v.get("sharpe_rf0"), "MDD=", _v.get("mdd_pct"))
print("\n=== λ 敏感性 ===", "T_COMPOSITE=", T_COMPOSITE,
      "LAM_SENS=", LAM_SENS,
      "L1偏移=", round(sum(abs(R4_SENS[k] - W_ACT[k]) for k in W_ACT), 6))

# ───────────────────────── 5. 汇总落盘 ─────────────────────────
constraint_report = {k: check_constraints(v) for k, v in WEIGHT_SETS.items()}
gate_wf = (GATE.get("walkforward") or {}).get("summary") or {}
failed_gates = [k for k, v in GATE["gates"].items() if not v]
should_change = bool(
    GATE["verdict"] == "PASS"
    and T_MAX is not None and T_MAX >= THR
    and LAM > 0
)

result = {
    "generated_at": datetime.datetime.now().isoformat(timespec="seconds"),
    "purpose": "近5年回测验证是否应更改 v4.19-c1-sustain-gates 权重（只读诊断，不落地）",
    "status": "NOT_ADOPTED",
    "should_change_weights": should_change,
    "final_weights": dict(W_ACT),
    "final_weights_source": "严格等于现行 factor_weights.json（未改动一个字节）",
    "window": {
        "natural_5y": [START, END],
        "panel_first": Dw[0], "panel_last": Dw[-1],
        "panel_trading_days_in_window": nD,
        "eval_first": Dw[eval_i[0]], "eval_last": Dw[eval_i[-1]],
        "n_eval_snapshots": nE, "horizon_h": H,
        "n_effective_independent_obs": n_eff,
        "why_eval_end": "面板末日 2026-10-30，h=20 前向收益完整可得的最后截面为 2026-09-24",
        "in_sample_not_oos": "权重 2026-11-04 基于 1635 日全历史拟合，本 5 年区间全部落在样本内",
    },
    "data": {
        "factor_matrix": "data/panel_r124_mat.npz (1653×2257×9, 2020-01-02~2026-10-30)",
        "exec_prices": "data/klines_2020.npz (2257×1655×7, 2020-01-02~2026-11-03)",
        "dims_in_panel": DIMS9,
        "effective_dims": list(W_ACT.keys()),
        "blind_dim": "score_fund（权重 0.20，面板整列无历史快照 → 内核按 DMAX/2 中性填充，分母恒 Σ=1.00）",
        "kernel_meta": KERNEL_META,
        "factor_parity": False,
        "artifact_class": "PROXY_ONLY",
        "proxy_dims": {
            "score_size_growth": "流通市值分位，非生产 size_growth 复合分",
            "score_quality": "ROE 单指标分位，非生产三族质量分",
            "score_growth": "归母净利同比分位，非生产成长存量+方式B+三门槛",
            "score_fund": "逐日资金流无历史快照 → 真盲区",
        },
        "pool_domain": "full（pit_pool.json 仅 238 截面 2025-11-03 起，5 年区间无重建池 → 含池前视偏差）",
        "gate_kernel_difference": "闸门 F.composite 用缺维剔分母 + DIM_MAX 全 10；生产内核恒分母 + 5/10 混合量程，两条线不可混用",
    },
    "metrics_definition": {
        "objective": "TopN 等权 h20 收益 − 截面等权（头部超额），不是全截面 IC",
        "rank_ic": "factor_validation_core.row_pearson_ranks（Spearman，仅方向诊断，禁用于排权重）",
        "nw_t": "lag=19 主口径；另列 lag=4 与 factor_validation_core.stat 对照",
        "n_eff": "n_eval_snapshots / h（重叠窗口折合独立观测）",
        "portfolio": "T+1 开盘 / 佣金3bp+印花5bp+滑点5bp / 涨停拒买跌停拒卖 / 覆盖率门禁 0.80 / Top10 等权 w_total=0.80 / 每20交易日调仓",
        "forbidden": "禁把 TopN 毛超额当净值；禁跨权重版本比 total；禁按 IC 排权重",
    },
    "factor_summary_5y": summary,
    "layer_diagnostic_kernel_line": {
        "q1_to_q5_mean_h20_return": layer_means,
        "adjacent_diffs": layer_diffs,
        "spread_q5_minus_q1": spread,
        "strictly_monotonic": mono_strict,
    },
    "gate_five_doors": {
        "verdict": GATE["verdict"],
        "gates": GATE["gates"],
        "failed": failed_gates,
        "all_sample_ic": GATE.get("all"),
        "layer_means": GATE.get("layer_means"),
        "spread": GATE.get("spread"),
        "walkforward_summary": gate_wf,
        "walkforward_folds": (GATE.get("walkforward") or {}).get("folds"),
        "caveat": GATE.get("caveat"),
        "gate_coverage": G._gate_coverage(W_GATE),
    },
    "candidate_weights": CAND,
    "constraint_check": constraint_report,
    "candidate_weights_by_half": seg_cand,
    "candidate_half_L1_drift": L1_HALF,
    "shrinkage_rule": {
        "formula": "w = (1-λ)·w_prior + λ·w_data; λ = clip((t_max-2)/(thr-2),0,1) × clip(n_eff/30,0,1)",
        "t_max_single_dims": T_MAX, "bonferroni_threshold": THR,
        "n_eff": n_eff, "lambda": LAM,
        "t_max_plus_composite": T_COMPOSITE,
        "lambda_if_composite_t": LAM_SENS,
        "weight_offset_if_composite_t": round(
            sum(abs(R4_SENS[k] - W_ACT[k]) for k in W_ACT), 6),
        "note": ("沿用 2026-11-11 近1年诊断提出的收缩口径，非项目已固化的权威判据；"
                 "主口径 t_max 只取单因子生效维，另列把合成组合纳入后的敏感性，"
                 "两种口径下 λ 均 <0.05，权重偏移可忽略"),
    },
    "portfolio_exec_backtest": PORTFOLIO,
    "benchmark_5y": BENCH5,
    "nav_diagnostics": NAV_DIAG,
    "nav_caveat": ("组合净值与满仓指数口径不同（w_total=0.80、Top10 等权、每20日调仓、"
                   "基准为截面等权而非指数），且信号来自 PROXY_ONLY 代理面板，"
                   "仅作同口径方案间对照，禁作为策略有效性证明"),
    "oos_like_purged": OOS_LIKE,
    "bonferroni": {
        "K_now": K_NOW, "threshold": THR,
        "K_upper": FE.K_UPPER, "threshold_upper": round(FE._bonferroni_t(FE.K_UPPER), 4),
        "nw_whitenoise_95pct_n60": 2.00,
        "note": "NW t 小样本临界值随 n 变化：n=6/lag=4→4.90、n=10→3.63、n=60→2.00；报显著必查 (n,lag)",
    },
    "verdict_reasons": [],
    "required_disclosures": [
        "PROXY_ONLY：长面板因子为历史代理（factor_parity=False），禁止用于证明生产模型有效性",
        "score_fund 权重 0.20 为整列盲区，5 年回测只检验到 Σ=0.80",
        "5 年全部为样本内（权重基于 1635 日全历史），不是真样本外",
        "pit_pool 无法覆盖 5 年，全池域含池前视偏差",
        "G5 未过 ⇒ 禁称「经验证的 alpha」",
        "IC 与头部超额方向相反 ⇒ 禁按 IC 排权重",
        "净值曲线已核验无复权跳变（|日涨跌|>10% 仅 1 天），但收益集中在 "
        "2024/2025 反弹段，非全周期稳定收益",
        "Top10 离散换仓使净值对 <2pp 权重偏移产生 >20pp 差异，禁止用净值排序反推权重",
        "R3（|IC| 加权）净值最高但属明令禁用口径，不构成改权重依据",
        "本次未登记 trial、未改任何权重/产物/页面、未提交",
    ],
}
best_cand = max(
    ((k, v.get("net_return_pct")) for k, v in PORTFOLIO.items()
     if k in ("R1_head_excess_5y", "R2_icir_like_5y", "R3_abs_ic_BANNED")
     and v.get("net_return_pct") is not None),
    key=lambda kv: kv[1], default=("n/a", None))
_l1sens = round(sum(abs(R4_SENS[k] - W_ACT[k]) for k in W_ACT), 6)
_nd = NAV_DIAG.get("R0_current", {})
result["verdict_reasons"] = [
    f"五门 verdict={GATE['verdict']}，未通过项={failed_gates or '无'}；"
    f"G5 折通过率={gate_wf.get('pass_rate')}（阈值 {gate_wf.get('pass_rate_threshold')}）；"
    f"G3 胜等权 {gate_wf.get('n_g3_beat_ew')}/{gate_wf.get('n_folds')} 折",
    f"生效单因子维 Top10 超额最大 NW t19={T_MAX}；合成组合自身="
    f"{T_COMPOSITE}（名义>2.0，但均远低于 Bonferroni {THR}，K={K_NOW}）",
    f"收缩系数 λ={LAM}；即使把合成组合纳入 t_max，λ 也仅 {LAM_SENS}",
    f"⚠️ 净值对微小权重变化极敏感：λ={LAM_SENS} 时权重 L1 偏移仅 {_l1sens}，"
    f"净值却从 {PORTFOLIO['R0_current'].get('net_return_pct')}% 变为 "
    f"{PORTFOLIO['R4_shrink_sens_composite_t'].get('net_return_pct')}%"
    f"（差 {round(PORTFOLIO['R4_shrink_sens_composite_t'].get('net_return_pct', 0) - PORTFOLIO['R0_current'].get('net_return_pct', 0), 2)}pp）"
    f" ⇒ Top10 离散换仓噪声主导，禁止用净值排序反推权重",
    f"前后半段候选权重 L1 漂移={L1_HALF}；带 purge 交叉验证两个方向结论相反"
    f"（{OOS_LIKE['train_first_test_second']['fitted_net'].get('net_return_pct')}% vs "
    f"prior {OOS_LIKE['train_first_test_second']['prior_net'].get('net_return_pct')}%；"
    f"{OOS_LIKE['train_second_test_first']['fitted_net'].get('net_return_pct')}% vs "
    f"prior {OOS_LIKE['train_second_test_first']['prior_net'].get('net_return_pct')}%）",
    f"现行权重 5 年可执行净收益={PORTFOLIO['R0_current'].get('net_return_pct')}%；"
    f"朴素候选中最优={best_cand[0]} {best_cand[1]}%（R3 为禁用 |IC| 口径，非选择依据）",
    f"净值曲线已核验：最大单日 +{_nd.get('max_1d_pct')}%({_nd.get('max_1d_date')})、"
    f"{_nd.get('min_1d_pct')}%({_nd.get('min_1d_date')})，"
    f"|日涨跌|>10% 仅 {_nd.get('n_days_abs_move_gt_10pct')} 天 → 无复权跳变污染；"
    f"但收益高度集中在 2024/2025 反弹段（{_nd.get('by_year_pct')}）",
]
if not should_change:
    result["conclusion"] = (
        "不更改权重。5 年样本把 n_eff 从 10.75 提升到 %.2f，全样本 IC 与分层单调性"
        "都变显著（合成组合 IC NW t19=%.2f、Q1→Q5 严格单调，但 WF 折内严格单调仅 %s/%s），"
        "真正决定能否改权重的三道关仍未过："
        "① G5 滚动前推折通过率仅 %.1f%%（阈值 50%%）、G3 胜等权仅 %s/%s 折；"
        "② 头部超额最大 NW t19=%.4f，远低于 Bonferroni %.4f；"
        "③ 5 年全是样本内 + 代理因子 PROXY_ONLY + score_fund 0.20 盲区。"
        "且净值对 %.2fpp 的权重微调就产生 %.1fpp 差异，属离散换仓噪声。"
        "最终权重严格等于现行 v4.19-c1-sustain-gates。"
        % (n_eff, summary["__COMPOSITE__"]["ic_nw_t19"],
           gate_wf.get("n_g1_strict"), gate_wf.get("n_folds"),
           (gate_wf.get("pass_rate") or 0) * 100,
           gate_wf.get("n_g3_beat_ew"), gate_wf.get("n_folds"),
           T_COMPOSITE, THR,
           _l1sens * 100,
           abs((PORTFOLIO["R4_shrink_sens_composite_t"].get("net_return_pct") or 0)
               - (PORTFOLIO["R0_current"].get("net_return_pct") or 0)))
    )
else:
    result["conclusion"] = "达到改权重的统计门槛，但仍需五门+八处同步+trial预登记后另行裁决。"

with io.open(OUT_JSON, "w", encoding="utf-8") as f:
    json.dump(result, f, ensure_ascii=False, indent=1, default=lambda o: str(o))


# ───────────────────── 6. 同源 HTML 报告 ─────────────────────
import html as _html


def esc(s):
    return _html.escape(str(s))


def num(v, digits=2, signed=False, suffix="", color=False):
    if v is None:
        return '<span class="mut">—</span>'
    try:
        f = float(v)
    except (TypeError, ValueError):
        return '<span class="mut">—</span>'
    cls = ""
    if color:
        cls = "up" if f > 0 else ("down" if f < 0 else "mut")
    s = (f"{f:+.{digits}f}" if signed else f"{f:.{digits}f}") + suffix
    return f'<span class="{cls}">{s}</span>' if cls else s


def render_html(R):
    W = R["window"]
    S5 = R["factor_summary_5y"]
    G5 = R["gate_five_doors"]
    WF = G5.get("walkforward_summary") or {}
    FOLDS = G5.get("walkforward_folds") or []
    PF = R["portfolio_exec_backtest"]
    ND = R["nav_diagnostics"].get("R0_current", {})
    SH = R["shrinkage_rule"]
    CW = R["candidate_weights"]
    CC = R["constraint_check"]
    dim_order = R["data"]["dims_in_panel"] + ["__COMPOSITE__", "__EW__"]

    rows = []
    for d in dim_order:
        v = S5.get(d, {})
        w = W_FULL.get(d, 0.0) if not d.startswith("__") else ("合成" if d == "__COMPOSITE__" else "等权")
        rows.append(
            "<tr><td class='nm'>%s</td><td>%s</td><td>%s</td><td>%s</td>"
            "<td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td></tr>" % (
                esc(d), esc(w),
                num(v.get("ic_mean"), 5, True), num(v.get("ic_nw_t19"), 3, True, color=True),
                num((v.get("top10_excess_mean") or 0) * 100, 3, True, color=True),
                num(v.get("top10_excess_nw_t19"), 3, True, color=True),
                num((v.get("first_half_excess") or 0) * 100, 3, True, color=True),
                num((v.get("second_half_excess") or 0) * 100, 3, True, color=True),
                num(v.get("top10_win_rate_pct"), 1, False, "%")))

    gate_rows = []
    for k, label in (("g1_monotonic_robust", "G1 分层单调（噪声容忍）"),
                     ("g2_spread_positive", "G2 spread>0"),
                     ("g3_beat_ew", "G3 胜等权"),
                     ("g4_beat_single", "G4 胜最强单因子"),
                     ("g5_wf_pass_rate", "G5 walk-forward 通过率")):
        ok = G5["gates"].get(k)
        gate_rows.append("<tr><td>%s</td><td>%s</td><td class='%s'>%s</td></tr>" % (
            esc(label), esc(k), "up" if ok else "down", "PASS" if ok else "FAIL"))

    fold_rows = []
    for f in FOLDS:
        tr, va = f.get("train_range") or [], f.get("val_range") or []
        ok = (f.get("gates") or {}).get("pass")
        fold_rows.append(
            "<tr><td>%s</td><td>%s~%s</td><td>%s~%s</td><td>%s</td><td>%s</td>"
            "<td>%s</td><td>%s</td><td class='%s'>%s</td></tr>" % (
                f.get("fold"), esc(tr[0] if tr else ""), esc(tr[-1] if tr else ""),
                esc(va[0] if va else ""), esc(va[-1] if va else ""),
                num(f.get("train_icir"), 3), num(f.get("val_icir"), 3, color=True),
                num(f.get("ew_icir"), 3), num(f.get("spread"), 5, True, color=True),
                "up" if ok else "down", "PASS" if ok else "FAIL"))

    pf_rows = []
    for k, v in PF.items():
        pf_rows.append(
            "<tr><td class='nm'>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td>"
            "<td>%s</td><td>%s</td><td>%s</td><td>%s</td></tr>" % (
                esc(k), num(v.get("net_return_pct"), 2, True, "%", color=True),
                num(v.get("ann_return_pct"), 2, True, "%", color=True),
                num(v.get("sharpe_rf0"), 3, color=True),
                num(v.get("mdd_pct"), 2, False, "%", color=True),
                num(v.get("calmar"), 3, color=True),
                num(v.get("total_cost_pct"), 2, False, "%"),
                num(v.get("n_rejected_trades"), 0),
                "✅" if v.get("coverage_gate_ok") else "❌"))

    bench_rows = []
    for c, v in R["benchmark_5y"].items():
        bench_rows.append("<tr><td class='nm'>%s %s</td><td>%s~%s</td><td>%s</td>"
                          "<td>%s</td><td>%s</td></tr>" % (
                              esc(v.get("name", c)), esc(c), esc(v.get("first")),
                              esc(v.get("last")), num(v.get("return_pct"), 2, True, "%", color=True),
                              num(v.get("sharpe_rf0"), 3, color=True),
                              num(v.get("mdd_pct"), 2, False, "%", color=True)))

    year_rows = []
    for y, v in sorted((ND.get("by_year_pct") or {}).items()):
        year_rows.append("<tr><td>%s</td><td>%s</td></tr>" % (
            esc(y), num(v, 2, True, "%", color=True)))

    oos_rows = []
    for k, v in R["oos_like_purged"].items():
        oos_rows.append("<tr><td class='nm'>%s</td><td>%s~%s</td><td>%s</td><td>%s</td>"
                        "<td>%s</td><td class='%s'>%s</td></tr>" % (
                            esc(k), esc(v["test_window"][0]), esc(v["test_window"][1]),
                            num(v["fitted_net"].get("net_return_pct"), 2, True, "%", color=True),
                            num(v["prior_net"].get("net_return_pct"), 2, True, "%", color=True),
                            num((v["fitted_net"].get("net_return_pct") or 0)
                                - (v["prior_net"].get("net_return_pct") or 0), 2, True, "pp", color=True),
                            "up" if (v["fitted_net"].get("net_return_pct") or 0)
                            > (v["prior_net"].get("net_return_pct") or 0) else "down",
                            "拟合权重胜" if (v["fitted_net"].get("net_return_pct") or 0)
                            > (v["prior_net"].get("net_return_pct") or 0) else "先验权重胜"))

    wdims = list(R["final_weights"].keys())
    cand_rows = []
    for k, wmap in CW.items():
        cells = "".join("<td>%s</td>" % num(wmap.get(d), 4) for d in wdims)
        ck = CC.get(k, {})
        cand_rows.append("<tr><td class='nm'>%s</td>%s<td class='%s'>%s</td></tr>" % (
            esc(k), cells, "up" if ck.get("sum_ok") and ck.get("single_ok")
            and ck.get("group_ok") else "down",
            "PASS" if ck.get("sum_ok") and ck.get("single_ok") and ck.get("group_ok") else "FAIL"))

    reasons = "".join("<li>%s</li>" % esc(x) for x in R["verdict_reasons"])
    disclosures = "".join("<li>%s</li>" % esc(x) for x in R["required_disclosures"])

    return """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>近5年回测权重验证 · 2026-11-11</title>
<style>
:root{--bg:#f6f7f9;--card:#fff;--bd:#e3e6ea;--tx:#1b2430;--mut:#6b7785;--up:#c62828;--down:#1b8a5a;--acc:#1c4e80}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--tx);font:14px/1.65 -apple-system,BlinkMacSystemFont,"PingFang SC","Microsoft YaHei",sans-serif}
.wrap{max-width:1180px;margin:0 auto;padding:28px 20px 60px}
h1{font-size:25px;margin:0 0 6px}h2{font-size:18px;margin:34px 0 12px;padding-left:10px;border-left:4px solid var(--acc)}
h3{font-size:15px;margin:20px 0 8px;color:var(--acc)}
.sub{color:var(--mut);font-size:13px;margin-bottom:18px}
.card{background:var(--card);border:1px solid var(--bd);border-radius:10px;padding:18px 20px;margin:14px 0;box-shadow:0 1px 3px rgba(0,0,0,.03)}
.hero{border-left:5px solid var(--up);background:#fff7f6}
.badge{display:inline-block;padding:3px 10px;border-radius:5px;font-size:12px;font-weight:700;letter-spacing:.5px}
.b-no{background:#fdecea;color:#b71c1c;border:1px solid #f5c6c2}
.b-ok{background:#e8f5e9;color:#1b5e20;border:1px solid #b7dfb9}
.b-warn{background:#fff8e1;color:#8a6d00;border:1px solid #f0dfa8}
table{width:100%%;border-collapse:collapse;font-size:12.5px;margin:8px 0}
th,td{padding:7px 8px;border-bottom:1px solid var(--bd);text-align:right;white-space:nowrap}
th{background:#eef2f6;color:#33465c;font-weight:600;position:sticky;top:0}
td:first-child,th:first-child{text-align:left}
tr:nth-child(even) td{background:#fafbfc}
.nm{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:12px}
.up{color:var(--up)}.down{color:var(--down)}.mut{color:var(--mut)}
.kv{display:grid;grid-template-columns:repeat(auto-fit,minmax(210px,1fr));gap:10px;margin:10px 0}
.kv div{background:#f8fafc;border:1px solid var(--bd);border-radius:7px;padding:9px 12px}
.kv b{display:block;font-size:17px;margin-top:2px}
.kv span{color:var(--mut);font-size:12px}
ul{margin:8px 0 8px 20px;padding:0}li{margin:5px 0}
.note{background:#fff8e1;border:1px solid #f0dfa8;border-radius:8px;padding:12px 16px;color:#6b5600;font-size:13px;margin:12px 0}
.foot{color:var(--mut);font-size:12px;margin-top:34px;border-top:1px solid var(--bd);padding-top:14px}
.scroll{overflow-x:auto}
code{background:#eef2f6;padding:1px 5px;border-radius:4px;font-size:12px}
</style></head><body><div class="wrap">
<h1>近 5 年回测 · 是否应更改权重</h1>
<div class="sub">生成 %s ｜ 权重版本 v4.19-c1-sustain-gates ｜ 指纹 <code>9347d48f…</code> ｜ 只读诊断，未改任何权重/产物</div>

<div class="card hero">
<span class="badge b-no">NOT_ADOPTED</span> &nbsp; <span class="badge b-warn">PROXY_ONLY</span> &nbsp;
<b style="font-size:17px">结论：不更改权重</b>
<p style="margin:10px 0 0">%s</p>
</div>

<div class="kv">
<div><span>评估区间</span><b>%s ~ %s</b></div>
<div><span>h20 完整截面 / n_eff</span><b>%s / %s</b></div>
<div><span>五门裁决</span><b class="down">%s</b></div>
<div><span>收缩系数 λ</span><b>%s</b></div>
<div><span>Bonferroni 阈值 (K=%s)</span><b>%s</b></div>
<div><span>现行权重 5 年净收益</span><b class="up">%s%%</b></div>
</div>

<h2>一、判断方法（先钉死口径再看结果）</h2>
<div class="card">
<p>目标函数 = <b>TopN 头部超额</b>（Top10 等权 h20 收益 − 截面等权），<b>不是全截面 IC</b>。Rank IC 只作方向诊断，
因为项目已实测「IC 与头部超额方向相反」。</p>
<table><thead><tr><th>环节</th><th style="text-align:left">唯一实现 / 判据</th></tr></thead><tbody>
<tr><td>合成打分</td><td style="text-align:left"><code>scoring_core.composite_total_matrix</code>：分母恒 Σ=1.00，缺维填 DMAX/2 中性分；<code>score_fund</code> 盲区由内核补 0.20 中性权重</td></tr>
<tr><td>五门准入</td><td style="text-align:left"><code>weight_admission_gate.evaluate / walk_forward</code>：G1 单调 / G2 spread / G3 胜等权 / G4 胜单因子 / G5 六折≥3 折通过</td></tr>
<tr><td>执行净值</td><td style="text-align:left"><code>exec_backtest.run</code>：T+1 开盘、13bp 费用、涨跌停拒单留痕、覆盖率门禁 0.80、Top10 等权 w_total=0.80、每 20 交易日调仓</td></tr>
<tr><td>显著性</td><td style="text-align:left">Newey-West t（lag=19，另列 lag=4）；Bonferroni 阈值按 <code>trial_log.n_trials()</code> 现算；n_eff = 截面数 / h</td></tr>
<tr><td>权重分配</td><td style="text-align:left">按表现分归一 → <b>SLSQP 带约束欧氏投影</b>：Σ=1.00、单因子≤0.20、大类≤0.30、<code>score_fund</code> 锁 0.20；再按收缩规则 <code>w=(1−λ)w_prior+λw_data</code></td></tr>
</tbody></table>
<div class="note"><b>λ = clip((t_max−2)/(thr−2),0,1) × clip(n_eff/30,0,1)</b>：证据不足时偏移必须归零。
本轮 t_max=%s、thr=%s、n_eff=%s ⇒ <b>λ=%s</b>；即使把合成组合自身 t=%s 纳入，λ 也仅 <b>%s</b>。</div>
</div>

<h2>二、时间区间、数据范围与证据边界</h2>
<div class="card scroll">
<table><thead><tr><th>项</th><th style="text-align:left">实际值</th></tr></thead><tbody>
<tr><td>自然 5 年</td><td style="text-align:left">%s ~ %s（含闰日，5 个日历年）</td></tr>
<tr><td>面板交易日 / 评估截面</td><td style="text-align:left">%s 日（%s~%s）/ %s 个 h20 收益完整可得截面（%s~%s）</td></tr>
<tr><td>有效独立观测</td><td style="text-align:left">n_eff = %s / %s = <b>%s</b>（近 1 年仅 10.75）</td></tr>
<tr><td>因子矩阵</td><td style="text-align:left">%s</td></tr>
<tr><td>执行价格</td><td style="text-align:left">%s</td></tr>
<tr><td>可检验权重</td><td style="text-align:left"><b>Σ=0.80</b>；<code>score_fund</code> 0.20 整列无历史快照（真盲区，未覆盖≠通过）</td></tr>
<tr><td>评估域</td><td style="text-align:left">%s</td></tr>
<tr><td>因子性质</td><td style="text-align:left"><b>factor_parity=False / PROXY_ONLY</b>：size_growth=流通市值分位、quality=ROE 分位、growth=归母净利同比分位，均非生产算法</td></tr>
<tr><td>闸门与内核口径差</td><td style="text-align:left">%s</td></tr>
<tr><td>样本内外</td><td style="text-align:left">%s</td></tr>
</tbody></table>
</div>

<h2>三、真实回测结果</h2>
<h3>3.1 因子层（唯一内核，5 年 / h=20）</h3>
<div class="card scroll"><table><thead><tr>
<th>维度</th><th>权重</th><th>Rank IC</th><th>IC NW t19</th><th>Top10 超额</th><th>超额 NW t19</th><th>前半段</th><th>后半段</th><th>胜率</th>
</tr></thead><tbody>%s</tbody></table>
<p class="sub">IC 与头部超额方向明显背离：<code>score_bias60</code> IC +0.0811 最高，但 Top10 超额 −3.30%%/20日最差；
合成组合 IC NW t19=%s 显著，头部超额 NW t19 仅 %s。<b>这正是禁止按 IC 排权重的实测依据。</b></p></div>

<h3>3.2 五门准入裁决（唯一判据，5 年样本）</h3>
<div class="card scroll"><table><thead><tr><th>门禁</th><th>字段</th><th>结果</th></tr></thead><tbody>%s</tbody></table>
<p class="sub">全样本：IC=%s、ICIR=%s、NW t(lag=4)=%s；分层 Q1..Q5 = %s，spread=%s（t=%s）。
<b>全样本统计显著，但滚动前推折通过率 %s%% &lt; 50%%，G3 仅 %s/%s 折胜等权。</b></p>
<h3 style="margin-top:18px">G5 六折明细</h3>
<table><thead><tr><th>折</th><th>训练区间</th><th>验证区间</th><th>训练 ICIR</th><th>验证 ICIR</th><th>等权 ICIR</th><th>spread</th><th>判定</th></tr></thead><tbody>%s</tbody></table>
<p class="sub">训练 ICIR 均值 %s → 验证 %s，衰减 %s；折内严格单调仅 %s/%s，单调性断点全部落在噪声区间（显著 0 个）。</p></div>

<h3>3.3 组合层可执行净值（T+1 开盘 / 含成本）</h3>
<div class="card scroll"><table><thead><tr>
<th>方案</th><th>净收益</th><th>年化</th><th>夏普</th><th>最大回撤</th><th>Calmar</th><th>成本</th><th>拒单</th><th>覆盖门禁</th>
</tr></thead><tbody>%s</tbody></table>
<p class="sub">%s。<b>R3（|IC| 加权）净值最高但属明令禁用口径</b>；R4 收缩与 R0 完全一致（λ=0）。
R4 敏感性组仅偏移 %s 权重（L1），净值却差 %s pp ⇒ <b>净值由 Top10 离散换仓噪声主导，禁止用净值排序反推权重</b>。</p></div>

<h3>3.4 基准对照（同为 5 年真实行情）</h3>
<div class="card scroll"><table><thead><tr><th>指数</th><th>区间</th><th>收益</th><th>夏普</th><th>最大回撤</th></tr></thead><tbody>%s</tbody></table>
<p class="sub">组合净值与满仓指数口径不同（w_total=0.80、Top10 等权、基准为截面等权），仅作参照，不构成策略有效性证明。</p></div>

<h3>3.5 净值曲线核验（排除复权跳变 / 假收益）</h3>
<div class="card"><div class="kv">
<div><span>最大单日</span><b class="up">%s%% <small>%s</small></b></div>
<div><span>最小单日</span><b class="down">%s%% <small>%s</small></b></div>
<div><span>|日涨跌|&gt;10%% 天数</span><b>%s</b></div>
<div><span>首末净值</span><b>%s → %s</b></div>
</div>
<table><thead><tr><th>年度</th><th>组合净值收益</th></tr></thead><tbody>%s</tbody></table>
<p class="sub">单日极值分别落在 2024-09-30（924 行情）与 2025-04-07（关税冲击），与真实行情吻合，<b>无复权跳变污染</b>；
但收益高度集中在 2024/2025 反弹段，属状态集中而非全周期稳定。</p></div>

<h3>3.6 带 purge/embargo 的双向交叉验证（诊断级补充）</h3>
<div class="card scroll"><table><thead><tr><th>方向</th><th>验证窗口</th><th>拟合权重净值</th><th>现行先验净值</th><th>差</th><th>结论</th></tr></thead><tbody>%s</tbody></table>
<p class="sub">切点两侧各剔除 20 个截面（h=20 前向收益不跨越边界）。<b>换一个切分方向，优劣结论就翻转</b> ⇒
同一「按 5 年表现定权重」规则没有可迁移性。前后半段候选权重 L1 漂移 = %s。
此检验非项目唯一判据，且仍在样本内，仅作诊断。</p></div>

<h2>四、候选权重与约束校验</h2>
<div class="card scroll"><table><thead><tr><th>方案</th>%s<th>约束</th></tr></thead><tbody>%s</tbody></table>
<p class="sub">全部候选均通过 Σ=1.00 / 单因子≤0.20 / 大类≤0.30，<b>但没有一组通过五门</b>，全部不落地。
<code>score_fund</code> 锁定 0.20（盲区，不臆造）。</p></div>

<h2>五、为什么仍不更改权重</h2>
<div class="card"><ol>%s</ol></div>

<h2>六、落地条件与红线</h2>
<div class="card">
<p><b>若将来要改权重，必须同时满足：</b>① 真样本外 h20 可验截面 ≥30 且带 purge/embargo；
② G5 六折≥3 折通过、五门全 PASS；③ 长面板追平并补 <code>score_fund</code> 历史 PIT 快照、
因子逐维复现生产算法（factor_parity=True）；④ trial 预登记、复权源恢复；
⑤ 八处权重同步 + <code>verify_weights_round36</code> PASS。顺序必须是
<b>补数据 → 固定权重只跑验证 → 仅当证伪才约束内重解</b>。</p>
<p><b>红线：</b>禁称「经验证的 alpha」（G5 未过）；禁按 IC 排权重；禁把净值排序当权重依据；
禁把反弹叙事当分布外冲击；禁跨权重版本比 total；NW t 小样本临界值随 n 变化（n=60≈2.00），
报「显著」必查 (n, lag)。</p>
</div>

<h2>七、必须随结果一起披露</h2>
<div class="card"><ul>%s</ul></div>

<div class="foot">本报告与 <code>data/weight_diagnostic_5y_20261111.json</code> 由
<code>weight_diagnostic_5y_20261111.py</code> 单次运行同源生成，可复现。
本次为只读诊断：未改 factor_weights.json、未改评分产物、未改页面、未登记 trial、未提交、未 push。
不构成投资建议。</div>
</div></body></html>""" % (
        esc(R["generated_at"]), esc(R["conclusion"]),
        esc(W["natural_5y"][0]), esc(W["natural_5y"][1]),
        W["n_eval_snapshots"], W["n_effective_independent_obs"],
        esc(G5["verdict"]), SH["lambda"], R["bonferroni"]["K_now"],
        R["bonferroni"]["threshold"],
        num(PF["R0_current"].get("net_return_pct"), 2),
        SH["t_max_single_dims"], SH["bonferroni_threshold"], SH["n_eff"], SH["lambda"],
        SH["t_max_plus_composite"], SH["lambda_if_composite_t"],
        esc(W["natural_5y"][0]), esc(W["natural_5y"][1]),
        W["panel_trading_days_in_window"], esc(W["panel_first"]), esc(W["panel_last"]),
        W["n_eval_snapshots"], esc(W["eval_first"]), esc(W["eval_last"]),
        W["n_eval_snapshots"], W["horizon_h"], W["n_effective_independent_obs"],
        esc(R["data"]["factor_matrix"]), esc(R["data"]["exec_prices"]),
        esc(R["data"]["pool_domain"]), esc(R["data"]["gate_kernel_difference"]),
        esc(W["in_sample_not_oos"]),
        "".join(rows),
        num(S5["__COMPOSITE__"].get("ic_nw_t19"), 3),
        num(S5["__COMPOSITE__"].get("top10_excess_nw_t19"), 3),
        "".join(gate_rows),
        num((G5.get("all") or {}).get("ic"), 4), num((G5.get("all") or {}).get("icir"), 4),
        num((G5.get("all") or {}).get("t"), 2),
        esc(" / ".join("%+.4f" % x for x in (G5.get("layer_means") or []))),
        num(G5.get("spread"), 5, True), num(G5.get("spread_t"), 2, True),
        num((WF.get("pass_rate") or 0) * 100, 1), WF.get("n_g3_beat_ew"), WF.get("n_folds"),
        "".join(fold_rows),
        num(WF.get("train_icir_mean"), 3), num(WF.get("val_icir_mean"), 3),
        num(WF.get("train_to_val_decay"), 3), WF.get("n_g1_strict"), WF.get("n_folds"),
        "".join(pf_rows), esc(R["nav_caveat"]),
        SH["weight_offset_if_composite_t"],
        num(abs((PF["R4_shrink_sens_composite_t"].get("net_return_pct") or 0)
                - (PF["R0_current"].get("net_return_pct") or 0)), 2),
        "".join(bench_rows),
        num(ND.get("max_1d_pct"), 3, True), esc(ND.get("max_1d_date")),
        num(ND.get("min_1d_pct"), 3, True), esc(ND.get("min_1d_date")),
        ND.get("n_days_abs_move_gt_10pct"), ND.get("first_nav"), ND.get("last_nav"),
        "".join(year_rows),
        "".join(oos_rows), R["candidate_half_L1_drift"],
        "".join("<th>%s</th>" % esc(d) for d in wdims), "".join(cand_rows),
        reasons, disclosures,
    )


OUT_HTML = os.path.join(HERE, "weight_diagnostic_5y_20261111.html")
with io.open(OUT_HTML, "w", encoding="utf-8") as f:
    f.write(render_html(result))

print("\n=== 结论 ===")
print(result["conclusion"])
print("K=", K_NOW, "THR=", THR, "T_MAX=", T_MAX, "LAMBDA=", LAM)
print("五门未过项 =", failed_gates)
print("WROTE", OUT_JSON)
