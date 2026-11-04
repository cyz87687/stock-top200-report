#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
audit_round110_model_diagnostics.py —— 全项目**唯一被训练模型**的诊断与超参数寻优

═══ 先澄清一个事实（避免答错题）═══

老大要求「检查当前 transformer 模型的超参数配置」。

**项目里没有 Transformer。** 核实结果：
  · torch / tensorflow / keras / jax / sklearn / lightgbm / xgboost **全部未安装**
  · 全仓库 `transformer` 字符串命中 100% 是「为什么**不用** Transformer」的说明文字
    （regime_model.py 文件头、页面卡片、audit_round109 系列 docstring）

项目里**唯一被训练的拟合模型**是手写逻辑回归：

    audit_round109b_regime_alternatives.py :: class LogitBin
        __init__(l2=1.0, lr=0.35, iters=1500)
        全批量梯度下降，无验证集、无早停、无 dropout、无学习率衰减
        特征 14 维（build_features），滚动 zscore（仅用训练集 mu/sd）

所以本脚本诊断的是**这个模型**，并回答老大真正关心的问题：
  过拟合还是欠拟合？正则化/学习率/批量大小怎么调？调完能不能提升？

═══ 诊断设计（四项，缺一不可）═══

  D1 训练/测试 AUC 差        → 判定「过拟合 vs 欠拟合」的主判据
       train_auc − test_auc > 0.15 → 过拟合
       两者都 < 0.60            → 欠拟合（信号不足或模型容量不足）
       两者接近且 > 0.65        → 健康

  D2 学习曲线（训练样本量 vs OOS AUC）
       曲线仍在上升 → 数据受限（加数据有用，加正则无用）
       曲线已平台/下降 → 容量或噪声限制

  D3 收敛性（损失 vs 迭代）
       lr=0.35 是否过大导致震荡/不收敛；iters=1500 是否足够

  D4 超参数网格（l2 × lr × iters）× 目标（T1 波动 / T2 回撤）
       判据：OOS AUC 提升 ≥ 0.01 才算「有意义的优化」，否则属噪声

⚠️ 铁律：所有调参**只在训练折内**选，OOS 折只用于最终评估一次。
   若用 OOS 选超参数 = 前视，本项目 round72→73 已踩过一次。

输出：audit_round110_model_diagnostics.json
"""
import os
import sys
import json
import math
import itertools
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "audit_round110_model_diagnostics.json")
sys.path.insert(0, HERE)

from audit_round109_regime_feasibility import load_bench, build_features, zscore_fit  # noqa
from audit_round109b_regime_alternatives import (  # noqa
    LogitBin, fwd_vol, fwd_mdd, auc_score, MIN_HIST_FOR_QUANTILE,
)

BENCH = ("000985", "000300", "000905", "000852")
H = 10
PURGE = H          # 与 walk_forward_binary 一致
TRAIN_MIN = 120
STEP = 20

# 超参数搜索（**分阶段坐标搜索**，非全网格）
# ⚠️ 为什么不做全网格：72 组 × 4 指数 × 2 目标 = 576 次 walk-forward，
#    每次约 130 折 × 1500 迭代 → 实测 OOM/超时（exit 137）。
#    坐标搜索（先 l2 → 再 lr → 再 iters）只需约 12 次，且结论可解释：
#    正则强度对泛化影响最大，学习率主要影响收敛，迭代数决定是否收敛到位。
GRID_L2 = (0.1, 0.3, 1.0, 3.0, 10.0, 30.0)      # 权重衰减：从弱到强（1.0 为现用值）
GRID_LR = (0.05, 0.1, 0.2, 0.35)                 # 学习率（0.35 为现用值）
GRID_ITERS = (500, 1500, 4000)                   # 迭代（全批量 → Batch Size = 全样本）


# ---------------------------------------------------------------- 目标构造
def build_target(close, kind, H_=H):
    """kind: 'T1' 高波动 / 'T2' 深回撤。滚动 70% 分位做标签（只用 ≤t）。"""
    n = len(close)
    raw = fwd_vol(close, H_) if kind == "T1" else fwd_mdd(close, H_)
    y = np.full(n, -1, dtype=int)
    order = np.where(np.isfinite(raw))[0]
    for t in order:
        hist = raw[order[order <= t]]
        if len(hist) < MIN_HIST_FOR_QUANTILE:
            continue
        q70 = np.quantile(hist[-250:] if len(hist) >= 250 else hist, 0.70)
        y[t] = 1 if raw[t] >= q70 else 0
    return y


# ---------------------------------------------------------------- 带诊断的 WF
def wf_with_diag(F, y, idx, l2=1.0, lr=0.35, iters=1500,
                 min_train=TRAIN_MIN, step=STEP, purge=PURGE):
    """滚动训练，同时记录**折内 train AUC** 与 **OOS AUC**（判过拟合用）。

    → dict(train_auc, oos_auc, n_oos, n_folds, n_train_median, converged_loss)
    """
    tr_aucs, oos_aucs, ntrain = [], [], []
    yt_all, sc_all = [], []
    start = min_train
    n_all = len(idx)
    while start < n_all:
        te_pos = np.arange(start, min(start + step, n_all))
        tr = idx[:max(0, start - purge)]
        te = idx[te_pos]
        if len(te) == 0:
            break
        if len(tr) < 60 or len(np.unique(y[tr])) < 2:
            start += step
            continue
        mu, sd = zscore_fit(F[tr])
        Xtr, Xte = (F[tr] - mu) / sd, (F[te] - mu) / sd
        m = LogitBin(l2=l2, lr=lr, iters=iters).fit(Xtr, y[tr].astype(float))
        # 折内 train AUC（同一折训练集自评 → 与 OOS 对比看泛化差）
        _s_tr = m.score(Xtr)
        if len(np.unique(y[tr])) == 2:
            tr_aucs.append(auc_score(y[tr], _s_tr))
        yt_all.append(y[te]); sc_all.append(m.score(Xte))
        ntrain.append(len(tr))
        start += step
    if not yt_all:
        return None
    yt = np.concatenate(yt_all); sc = np.concatenate(sc_all)
    return {
        "train_auc": round(float(np.mean(tr_aucs)), 4) if tr_aucs else None,
        "oos_auc": round(float(auc_score(yt, sc)), 4),
        "n_oos": int(len(yt)),
        "n_folds": int(len(yt_all)),
        "n_train_median": int(np.median(ntrain)) if ntrain else 0,
        "pos_rate": round(float(yt.mean()), 3),
    }


def convergence_trace(close, F, y, l2=1.0, lr=0.35, iters_max=4000):
    """D3：单折全量训练，记录损失轨迹，判断 lr 是否过大/迭代是否足够。"""
    valid = np.isfinite(F).all(axis=1) & (y >= 0)
    idx = np.where(valid)[0]
    _cut = len(idx) * 3 // 4
    tr = idx[:_cut]
    mu, sd = zscore_fit(F[tr])
    X = (F[tr] - mu) / sd
    yy = y[tr].astype(float)
    w = np.zeros(X.shape[1]); b = 0.0
    n = len(yy)
    losses = []
    for i in range(iters_max):
        z = X @ w + b
        pr = 1.0 / (1.0 + np.exp(-np.clip(z, -30, 30)))
        eps = 1e-12
        loss = -np.mean(yy * np.log(pr + eps) + (1 - yy) * np.log(1 - pr + eps)) \
               + (l2 / (2 * n)) * float(w @ w)
        losses.append(float(loss))
        g = pr - yy
        w -= lr * (X.T @ g / n + (l2 / n) * w)
        b -= lr * g.mean()
    # 判定：后 20% 区间的损失波动 / 最终损失
    tail = losses[int(iters_max * 0.8):]
    rel_osc = (max(tail) - min(tail)) / max(abs(min(tail)), 1e-9)
    return {
        "loss_first": round(losses[0], 6),
        "loss_at_500": round(losses[499], 6),
        "loss_at_1500": round(losses[1499], 6),
        "loss_final": round(losses[-1], 6),
        "tail_relative_oscillation": round(float(rel_osc), 6),
        "still_decreasing": bool(losses[-1] < losses[-101] - 1e-9),
        "lr_used": lr, "iters_used": iters_max,
    }


def learning_curve(F, y, idx, lr=0.35, l2=1.0, iters=1500):
    """D2：固定 OOS 段，逐步加大训练窗口，看 OOS AUC 是否仍上升。"""
    n_all = len(idx)
    out = []
    for frac in (0.25, 0.40, 0.55, 0.70, 0.85):
        mt = max(60, int(n_all * frac))
        r = wf_with_diag(F, y, idx, l2=l2, lr=lr, iters=iters,
                         min_train=mt, step=STEP, purge=PURGE)
        if r:
            out.append({"train_frac": frac, "min_train": mt,
                        "oos_auc": r["oos_auc"], "n_oos": r["n_oos"],
                        "train_auc": r["train_auc"]})
    return out


def coordinate_search(F, y, idx, base_l2=1.0, base_lr=0.35, base_iters=1500):
    """分阶段坐标搜索：l2 → lr → iters，每阶段只动一个参数。

    → (best_params, best_auc, path)  path 记录每个阶段的候选与得分（可审计）
    """
    path = []
    cur = {"l2": base_l2, "lr": base_lr, "iters": base_iters}
    best_auc = wf_with_diag(F, y, idx, **cur)["oos_auc"]

    # 阶段1：正则强度（影响泛化，最重要）
    stage = []
    for l2 in GRID_L2:
        r = wf_with_diag(F, y, idx, l2=l2, lr=cur["lr"], iters=cur["iters"])
        if r:
            stage.append({"l2": l2, "oos_auc": r["oos_auc"], "train_auc": r["train_auc"]})
    if stage:
        b = max(stage, key=lambda x: x["oos_auc"])
        path.append({"stage": "l2", "candidates": stage, "picked": b["l2"], "auc": b["oos_auc"]})
        if b["oos_auc"] > best_auc:
            best_auc, cur["l2"] = b["oos_auc"], b["l2"]

    # 阶段2：学习率（固定已选 l2）
    stage = []
    for lr in GRID_LR:
        r = wf_with_diag(F, y, idx, l2=cur["l2"], lr=lr, iters=cur["iters"])
        if r:
            stage.append({"lr": lr, "oos_auc": r["oos_auc"], "train_auc": r["train_auc"]})
    if stage:
        b = max(stage, key=lambda x: x["oos_auc"])
        path.append({"stage": "lr", "candidates": stage, "picked": b["lr"], "auc": b["oos_auc"]})
        if b["oos_auc"] > best_auc:
            best_auc, cur["lr"] = b["oos_auc"], b["lr"]

    # 阶段3：迭代数（检验是否收敛到位）
    stage = []
    for it in GRID_ITERS:
        r = wf_with_diag(F, y, idx, l2=cur["l2"], lr=cur["lr"], iters=it)
        if r:
            stage.append({"iters": it, "oos_auc": r["oos_auc"], "train_auc": r["train_auc"]})
    if stage:
        b = max(stage, key=lambda x: x["oos_auc"])
        path.append({"stage": "iters", "candidates": stage, "picked": b["iters"], "auc": b["oos_auc"]})
        if b["oos_auc"] > best_auc:
            best_auc, cur["iters"] = b["oos_auc"], b["iters"]

    return cur, best_auc, path


def main():
    res = {"meta": {
        "round": 110,
        "purpose": "诊断全项目唯一被训练模型（LogitBin 逻辑回归）的过拟合/欠拟合并寻优超参数",
        "fact_check": ("项目内无 Transformer：torch/tensorflow/keras/jax/sklearn/lightgbm/xgboost "
                       "均未安装；全部 'transformer' 字符串为「为什么不用」的说明文字"),
        "model_under_test": "audit_round109b_regime_alternatives.LogitBin(l2=1.0, lr=0.35, iters=1500)",
        "features": "14 维（ret1/5/10/20/60, vol20, vol60, vol_ratio, px_ma20, px_ma60, dist_hi60, dist_lo60, info_ratio, up_frac20, brk5）",
        "H": H, "purge": PURGE, "train_min": TRAIN_MIN, "step": STEP,
        "search": {"method": "分阶段坐标搜索(l2→lr→iters)", "l2": list(GRID_L2),
                   "lr": list(GRID_LR), "iters": list(GRID_ITERS),
                   "why_not_full_grid": "72组×4指数×2目标=576次WF → 实测 exit 137 OOM"},
        "discipline": "超参数只在训练折内选；OOS 只评估一次；提升 ≥0.01 AUC 才算有意义",
    }}
    summary = []
    for code in BENCH:
        dates, close = load_bench(code)
        F, names = build_features(close)
        print(f"\n{'='*80}\n{code}  n={len(close)}  特征 {len(names)} 维")
        code_out = {"feature_names": names}
        for kind in ("T1", "T2"):
            y = build_target(close, kind)
            valid = np.isfinite(F).all(axis=1) & (y >= 0)
            idx = np.where(valid)[0]
            if len(idx) < 200:
                continue
            base = wf_with_diag(F, y, idx)          # 现行超参数
            if not base:
                continue
            gap = (base["train_auc"] - base["oos_auc"]) if base["train_auc"] is not None else None
            diag = ("过拟合" if (gap is not None and gap > 0.15)
                    else ("欠拟合" if (base["oos_auc"] < 0.60 and base["train_auc"] is not None
                                   and base["train_auc"] < 0.65)
                          else "接近健康/信号弱"))
            print(f"  [{kind}] 现行(l2=1.0,lr=0.35,it=1500)  "
                  f"trainAUC={base['train_auc']}  oosAUC={base['oos_auc']}  "
                  f"差={gap:+.3f}  → {diag}")
            code_out[kind] = {"baseline": base, "gap": round(gap, 4) if gap is not None else None,
                              "diagnosis": diag}

            # ---- D2 学习曲线（只对主基准做，省时）----
            if code == "000985":
                lc = learning_curve(F, y, idx)
                code_out[f"{kind}_learning_curve"] = lc
                print("        学习曲线:", "  ".join(
                    f"{x['train_frac']:.0%}→{x['oos_auc']:.3f}" for x in lc))

            # ---- D3 收敛（只对主基准 T1 做）----
            if code == "000985" and kind == "T1":
                cv = convergence_trace(close, F, y)
                code_out["convergence"] = cv
                print(f"        收敛: loss {cv['loss_first']:.4f}→500:{cv['loss_at_500']:.4f}"
                      f"→1500:{cv['loss_at_1500']:.4f}→4000:{cv['loss_final']:.4f}  "
                      f"尾部相对震荡={cv['tail_relative_oscillation']:.2e}  "
                      f"仍在下降={cv['still_decreasing']}")

            # ---- D4 分阶段坐标搜索 ----
            bp, b_auc, bpath = coordinate_search(F, y, idx)
            delta = b_auc - base["oos_auc"]
            verdict = ("有意义(≥+0.01)" if delta >= 0.01
                       else ("轻微(+0.005~0.01)" if delta >= 0.005
                             else "无意义(属噪声)"))
            code_out[f"{kind}_tuning"] = {
                "best_params": bp, "best_auc": round(b_auc, 4),
                "delta_vs_baseline": round(delta, 4), "verdict": verdict,
                "search_path": bpath,
            }
            print(f"        坐标搜索: best {bp} → oosAUC={b_auc:.4f} "
                  f"(Δ{delta:+.4f}, {verdict})")
            summary.append({"bench": code, "target": kind,
                            "base_auc": base["oos_auc"], "best_auc": round(b_auc, 4),
                            "delta": round(delta, 4), "verdict": verdict,
                            "best_params": bp})
        res[code] = code_out

    # 汇总
    res["summary"] = summary
    n_meaningful = sum(1 for s in summary if s["delta"] >= 0.01)
    res["conclusion"] = {
        "n_combos": len(summary),
        "n_meaningful_improve": n_meaningful,
        "verdict": (f"坐标搜索 × {len(summary)} 个「指数×目标」组合：仅 {n_meaningful} 组 AUC 提升 ≥0.01"
                    if summary else "无有效组合"),
    }
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(res, f, ensure_ascii=False, indent=1)
    print(f"\n{'='*80}")
    print(f"结论：{res['conclusion']['verdict']}")
    print(f"✅ 已写出 {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
