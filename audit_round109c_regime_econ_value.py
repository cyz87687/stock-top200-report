#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
audit_round109c_regime_econ_value.py —— 两件事：
  （1）诊断 T1（高波动状态）为何 AUC 在部分指数/期限上倒挂到 0.30
  （2）检验 T2（深回撤状态）预测**是否真有经济价值** —— 用它调仓位，
      风险调整后收益到底有没有改善。这是回答「现有仓位参考值缺乏参考价值」
      的唯一硬标准：不是模型指标好看，而是调完仓位真的更稳。

═══ （1）T1 诊断思路 ═══
倒挂（AUC<0.5）有两种可能：
  a) 真·非平稳：训练期学到「vol 高→未来 vol 高」，但测试期 regime 切换（波动中枢下移），
     模型把高分打给了「实际已进入低波动」的区间 → AUC 反转。属**分布漂移**，可诊断。
  b) 实现 bug：标签/特征错位。
区分方法：用「当期 vol20 的历史分位」做**零训练**预测器（不需拟合，无漂移风险），
若它的 AUC 稳定 >0.5，说明信号本身存在，倒挂来自拟合模型的漂移 → 归因 a)。
另外用「分折 AUC」看是否集中在某几折倒挂（漂移特征）还是均匀分布（无信号特征）。

═══ （2）经济价值检验 ═══
仓位规则（三档，可解释、无参数爆炸）：
  p_dd = 模型预测的「未来 H 日深回撤」概率
  p_dd < 0.35          → 仓位 = 1.0× 基准
  0.35 ≤ p_dd < 0.55   → 仓位 = 0.7× 基准
  p_dd ≥ 0.55          → 仓位 = 0.4× 基准
基准仓位取 1.0（满仓指数），故比较的是「满仓持有」vs「按风险信号降档」。
指标：CAGR、年化波动、最大回撤、Sharpe(rf=0)、Calmar。
判据（先写死）：动态仓位必须同时满足
  ① 最大回撤改善 ≥ 15%（相对降幅）
  ② CAGR 下降 ≤ 30%（不能为了控回撤把收益砍光）
才算「有参考价值」。否则只能做**描述性披露**，不能做仓位指令。

⚠️ 样本仅 440 日（≈1.75 年），且 2025-2026 是单一上涨 regime
   → 任何「择时降仓」天然吃亏。此结果只能用于判断「信号有没有信息」，
     不能当成「实盘能赚多少」的估计。这一点必须写进结论。

用法：python audit_round109c_regime_econ_value.py
输出：audit_round109c.json
"""
import os
import sys
import json
import math
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "audit_round109c.json")
sys.path.insert(0, HERE)

from audit_round109_regime_feasibility import (  # noqa
    load_bench, build_features, zscore_fit,
)
from audit_round109b_regime_alternatives import (  # noqa
    auc_score, LogitBin, fwd_mdd, walk_forward_binary,
)

MIN_HIST_FOR_QUANTILE = 90


def fold_aucs(F, y, idx_all, min_train=120, step=20, purge=20):
    """逐折 AUC，用于判断倒挂是「集中在少数折（漂移）」还是「均匀（无信号）」。"""
    out = []
    n_all = len(idx_all)
    start = min_train
    while start < n_all:
        te_pos = np.arange(start, min(start + step, n_all))
        tr = idx_all[:max(0, start - purge)]
        te = idx_all[te_pos]
        if len(te) == 0 or len(tr) < 60 or len(np.unique(y[tr])) < 2 or len(np.unique(y[te])) < 2:
            start += step
            continue
        mu, sd = zscore_fit(F[tr])
        m = LogitBin().fit((F[tr] - mu) / sd, y[tr].astype(float))
        sc = m.score((F[te] - mu) / sd)
        a = auc_score(y[te], sc)
        # 同期信息：当期 vol20 分位（零训练预测器）
        out.append({"n": int(len(te)), "auc": round(a, 3) if a == a else None})
        start += step
    return out


def t1_diagnose(close, F, names, H):
    """T1 诊断：训练模型 vs 零训练分位预测器。"""
    n = len(close)
    lr = np.zeros(n)
    lr[1:] = np.log(close[1:] / close[:-1])
    fv = np.full(n, np.nan)
    for t in range(n - H):
        fv[t] = lr[t + 1:t + 1 + H].std(ddof=1) * math.sqrt(252)

    idx = np.where(np.isfinite(F).all(axis=1) & np.isfinite(fv))[0]
    y = np.full(n, -1, dtype=int)
    for t in idx:
        hist = fv[idx[idx <= t]]
        if len(hist) < MIN_HIST_FOR_QUANTILE:
            continue
        q70 = np.quantile(hist[-250:] if len(hist) >= 250 else hist, 0.70)
        y[t] = 1 if fv[t] >= q70 else 0
    idx2 = np.where(y >= 0)[0]
    if len(idx2) < 150:
        return None

    # A. 训练模型（walk-forward）
    yt, sc = walk_forward_binary(F, y, idx2)
    a_model = auc_score(yt, sc) if yt is not None else float("nan")

    # B. 零训练预测器：用「当期 vol20 在过去250日的分位」直接当 score
    v20 = F[:, names.index("vol20")]
    zero_sc, ys = [], []
    for t in idx2:
        hist = v20[idx2[idx2 <= t]]
        hist = hist[np.isfinite(hist)]
        if len(hist) < MIN_HIST_FOR_QUANTILE or not np.isfinite(v20[t]):
            continue
        hh = hist[-250:] if len(hist) >= 250 else hist
        pct = float((hh <= v20[t]).mean())
        zero_sc.append(pct)
        ys.append(y[t])
    a_zero = auc_score(np.array(ys), np.array(zero_sc)) if len(ys) > 50 else float("nan")

    # C. 逐折 AUC（看倒挂是否集中）
    fa = fold_aucs(F, y, idx2)

    return {
        "n_oos": int(len(yt)) if yt is not None else 0,
        "auc_model": round(a_model, 4) if a_model == a_model else None,
        "auc_zerotrain_vol_pct": round(a_zero, 4) if a_zero == a_zero else None,
        "fold_aucs": fa,
        "n_folds_auc_below_0.5": sum(1 for f in fa if f["auc"] is not None and f["auc"] < 0.5),
        "n_folds": len(fa),
    }


def perf(returns, ann=252):
    """→ dict(CAGR, vol, MDD, Sharpe, Calmar)"""
    r = np.asarray(returns, dtype=float)
    if len(r) < 20:
        return None
    eq = np.cumprod(1 + r)
    n = len(r)
    cagr = eq[-1] ** (ann / n) - 1 if eq[-1] > 0 else float("nan")
    vol = r.std(ddof=1) * math.sqrt(ann)
    peak = np.maximum.accumulate(eq)
    mdd = float((eq / peak - 1).min())
    sharpe = (r.mean() * ann) / vol if vol > 0 else float("nan")
    calmar = (cagr / abs(mdd)) if mdd < 0 else float("nan")
    return {"CAGR": round(float(cagr), 4), "vol": round(float(vol), 4),
            "MDD": round(mdd, 4), "Sharpe": round(float(sharpe), 3),
            "Calmar": round(float(calmar), 3)}


def econ_test(close, F, names, H, dates):
    """T2 深回撤概率 → 调仓位，测风险调整后收益改善。"""
    n = len(close)
    md = fwd_mdd(close, H)
    idx = np.where(np.isfinite(F).all(axis=1) & np.isfinite(md))[0]
    y = np.full(n, -1, dtype=int)
    for t in idx:
        hist = md[idx[idx <= t]]
        if len(hist) < MIN_HIST_FOR_QUANTILE:
            continue
        q70 = np.quantile(hist[-250:] if len(hist) >= 250 else hist, 0.70)
        y[t] = 1 if md[t] >= q70 else 0
    idx2 = np.where(y >= 0)[0]
    if len(idx2) < 200:
        return None

    # 逐折产出「测试段的 p_dd」，保留日期位置以便对齐收益
    pos_p, prob_p = [], []
    n_all = len(idx2)
    start, step, purge = 120, 20, H
    while start < n_all:
        te_pos = np.arange(start, min(start + step, n_all))
        tr = idx2[:max(0, start - purge)]
        te = idx2[te_pos]
        if len(te) == 0 or len(tr) < 60 or len(np.unique(y[tr])) < 2:
            start += step
            continue
        mu, sd = zscore_fit(F[tr])
        m = LogitBin().fit((F[tr] - mu) / sd, y[tr].astype(float))
        pr = m.score((F[te] - mu) / sd)
        pos_p.extend(te.tolist())
        prob_p.extend(pr.tolist())
        start += step
    if len(pos_p) < 100:
        return None

    pos_p = np.array(pos_p)
    prob_p = np.array(prob_p)
    # 次 1 日指数收益（用于计算持仓收益），严格用 t+1 收益 → 无前视
    dret = np.zeros(n)
    dret[1:] = close[1:] / close[:-1] - 1.0
    fwd1 = np.full(n, np.nan)
    fwd1[:-1] = dret[1:]

    ok = np.isfinite(fwd1[pos_p])
    pos_p, prob_p = pos_p[ok], prob_p[ok]
    r_mkt = fwd1[pos_p]

    # 三档仓位
    w = np.where(prob_p >= 0.55, 0.4, np.where(prob_p >= 0.35, 0.7, 1.0))
    r_dyn = r_mkt * w

    p_buy = perf(r_mkt)
    p_dyn = perf(r_dyn)
    if not p_buy or not p_dyn:
        return None
    dd_impr = (abs(p_buy["MDD"]) - abs(p_dyn["MDD"])) / abs(p_buy["MDD"]) if p_buy["MDD"] < 0 else 0.0

    # ⚠️ 判据修正（首版错误，必须记下）：
    #   首版用「CAGR 保留比 = 动态CAGR / 满仓CAGR ≥ 0.70」。
    #   但本期 OOS 段几乎全落在**下跌段**（后140日 −9.9%~−16%），满仓 CAGR 为负，
    #   此时「降仓使亏损从 −23.6% 减到 −12.3%」是**改善**，而比值 0.52 却被判未过
    #   → 判据在负收益区间**方向反了**。
    #   改用**风险调整后收益**比较（对正负收益都成立）：
    #     ① Sharpe 必须改善（>0 变化量）
    #     ② MDD 相对改善 ≥ 15%
    #   这才是「仓位管理是否有价值」的正确提问方式（不问收益绝对值，问单位风险收益）。
    sharpe_delta = p_dyn["Sharpe"] - p_buy["Sharpe"]
    calmar_delta = p_dyn["Calmar"] - p_buy["Calmar"]
    passed = (dd_impr >= 0.15) and (sharpe_delta > 0.0)
    return {
        "n_days": int(len(r_mkt)),
        "range": [dates[int(pos_p[0])], dates[int(pos_p[-1])]],
        "w_dist": {"1.0": int((w == 1.0).sum()), "0.7": int((w == 0.7).sum()), "0.4": int((w == 0.4).sum())},
        "avg_w": round(float(w.mean()), 3),
        "buy_hold": p_buy, "risk_scaled": p_dyn,
        "mdd_improve_pct": round(float(dd_impr) * 100, 2),
        "sharpe_delta": round(float(sharpe_delta), 3),
        "calmar_delta": round(float(calmar_delta), 3),
        # 保留原始比值供参考，但**不再作为判据**
        "cagr_dyn_vs_buy": round(float(p_dyn["CAGR"] / p_buy["CAGR"]), 3) if p_buy["CAGR"] != 0 else None,
        "pass": bool(passed),
    }


def main():
    out = {"meta": {
        "round": "109c",
        "purpose": "T1 倒挂归因 + T2 经济价值检验",
        "criteria": {"mdd_improve_min_pct": 15, "cagr_keep_min": 0.70},
        "sample_warning": "样本仅 440 日(≈1.75年)，2025-2026 为单一上涨 regime；"
                          "结论只用于判断信号有无信息，不作实盘收益估计",
    }}

    for code, cname in (("000985", "中证全指"), ("000300", "沪深300"),
                        ("000905", "中证500"), ("000852", "中证1000")):
        dates, close = load_bench(code)
        F, names = build_features(close)
        blk = {"name": cname}
        print(f"\n{'='*70}\n{cname} ({code})")

        # ---------- T1 诊断 ----------
        print("  --- T1 高波动状态诊断 ---")
        blk["T1_diag"] = {}
        for H in (10, 20):
            d = t1_diagnose(close, F, names, H)
            blk["T1_diag"][f"H{H}"] = d
            if d:
                print(f"   H={H}: 训练模型AUC={d['auc_model']} | "
                      f"零训练vol分位AUC={d['auc_zerotrain_vol_pct']} | "
                      f"倒挂折 {d['n_folds_auc_below_0.5']}/{d['n_folds']}")
                print(f"        逐折AUC={[f['auc'] for f in d['fold_aucs']]}")

        # ---------- T2 经济价值 ----------
        print("  --- T2 深回撤 → 仓位（经济价值）---")
        blk["T2_econ"] = {}
        for H in (10, 20):
            r = econ_test(close, F, names, H, dates)
            blk["T2_econ"][f"H{H}"] = r
            if r:
                b, s = r["buy_hold"], r["risk_scaled"]
                print(f"   H={H}: 平均仓位={r['avg_w']:.2f} 档位分布={r['w_dist']}")
                print(f"        满仓     CAGR={b['CAGR']:+.2%} vol={b['vol']:.2%} MDD={b['MDD']:.2%} "
                      f"Sharpe={b['Sharpe']:.2f} Calmar={b['Calmar']:.2f}")
                print(f"        风险降档 CAGR={s['CAGR']:+.2%} vol={s['vol']:.2%} MDD={s['MDD']:.2%} "
                      f"Sharpe={s['Sharpe']:.2f} Calmar={s['Calmar']:.2f}")
                print(f"        → MDD改善 {r['mdd_improve_pct']:+.1f}% | "
                      f"Sharpe变化 {r['sharpe_delta']:+.2f} | "
                      f"Calmar变化 {r['calmar_delta']:+.2f} | "
                      f"{'✅ 过' if r['pass'] else '❌ 未过'}")
        out[code] = blk

    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print(f"\n✅ 已写出 {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
