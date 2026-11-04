#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
audit_round109b_regime_alternatives.py —— round109 的补充实验：找**可预测**的那部分

═══ 背景 ═══
round109 已证明：三分类方向（震荡/单边涨/单边跌）在 440 天样本上
逻辑回归 macro-F1 仅 0.24~0.40，且三分类准确率普遍**低于多数类基线** ——
方向基本不可预测。

但「不可预测方向」≠「什么都做不了」。本脚本检验两个更可能成立的替代目标：

  T1 未来波动率水平（vol targeting）
     金融学上已有大量证据表明波动率**聚集**（GARCH 效应），可预测性远高于方向。
     若成立 → 可用于「风险预算上限」的动态缩放（波动高时压低敞口）。
     目标：预测 future_vol_H 是否进入样本前 30% 分位（高波动状态）

  T2 未来下行风险（尾部）
     目标：future_MDD_H（未来 H 日的最大回撤）是否超过阈值（深回撤状态）
     这是仓位管理**真正关心**的量，比"涨跌方向"更贴近风险预算

  T3 宽度/离散度特征的增量信息
     用 K 线库（2138 只 × 305 日）构造真实横截面特征：
       breadth = 当日上涨股票占比
       dispersion = 当日个股收益的截面标准差
       limit_ratio = 涨跌幅>9% 的股票占比（情绪极端度）
     检验它们是否比「指数自身价格特征」更能识别单边市。

判据（先写死）：
  T1  AUC ≥ 0.65 才算「高波动状态可识别」（AUC 是类别不平衡下更稳的指标）
  T2  AUC ≥ 0.63 才算「深回撤状态可识别」
  T3  加入宽度特征后，单边二分类准确率提升 ≥ 3pp 才算有增量

用法：python audit_round109b_regime_alternatives.py
输出：audit_round109b.json
"""
import os
import sys
import json
import math
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "audit_round109b.json")

sys.path.insert(0, HERE)
from audit_round109_regime_feasibility import (  # noqa
    load_bench, build_features, SoftmaxReg, zscore_fit, macro_f1,
)

# ⚠️ 与 round109 的 250 不同：这里标签需要「至少 90 日历史才能定分位阈值」，
#   若仍用 250 则首折训练样本 <120 → 直接 break → 全程无 OOS（首版实测踩到）。
TRAIN_MIN = 200
STEP = 20
HORIZONS = (5, 10, 20)
MIN_HIST_FOR_QUANTILE = 90


# ---------------------------------------------------------------- AUC
def auc_score(y_true, score):
    """秩和法 AUC；y_true 为 0/1。"""
    y = np.asarray(y_true, dtype=int)
    s = np.asarray(score, dtype=float)
    n1 = int((y == 1).sum())
    n0 = int((y == 0).sum())
    if n1 == 0 or n0 == 0:
        return float("nan")
    order = np.argsort(s, kind="mergesort")
    ranks = np.empty(len(s), dtype=float)
    ranks[order] = np.arange(1, len(s) + 1)
    # 处理并列：用平均秩
    _s_sorted = s[order]
    i = 0
    while i < len(_s_sorted):
        j = i
        while j + 1 < len(_s_sorted) and _s_sorted[j + 1] == _s_sorted[i]:
            j += 1
        if j > i:
            avg = (i + j) / 2.0 + 1
            for k in range(i, j + 1):
                ranks[order[k]] = avg
        i = j + 1
    r1 = ranks[y == 1].sum()
    return float((r1 - n1 * (n1 + 1) / 2.0) / (n1 * n0))


# ---------------------------------------------------------------- 逻辑回归（二分类）
class LogitBin:
    def __init__(self, l2=1.0, lr=0.35, iters=1500):
        self.l2, self.lr, self.iters = l2, lr, iters

    def fit(self, X, y):
        n, p = X.shape
        w = np.zeros(p)
        b = 0.0
        for _ in range(self.iters):
            z = X @ w + b
            pr = 1.0 / (1.0 + np.exp(-np.clip(z, -30, 30)))
            g = pr - y
            gw = X.T @ g / n + (self.l2 / n) * w
            gb = g.mean()
            w -= self.lr * gw
            b -= self.lr * gb
        self.w, self.b = w, b
        return self

    def score(self, X):
        return 1.0 / (1.0 + np.exp(-np.clip(X @ self.w + self.b, -30, 30)))


# ---------------------------------------------------------------- 目标构造
def fwd_vol(close, H):
    n = len(close)
    lr = np.zeros(n)
    lr[1:] = np.log(close[1:] / close[:-1])
    out = np.full(n, np.nan)
    for t in range(n - H):
        out[t] = lr[t + 1:t + 1 + H].std(ddof=1) * math.sqrt(252)
    return out


def fwd_mdd(close, H):
    """未来 H 日最大回撤（正数表示跌幅%）。"""
    n = len(close)
    out = np.full(n, np.nan)
    for t in range(n - H):
        w = close[t:t + 1 + H]
        peak = np.maximum.accumulate(w)
        out[t] = float(-(w / peak - 1.0).min())
    return out


def walk_forward_binary(F, y, idx_all, min_train=120, step=20, purge=20):
    """滚动训练二分类器，返回 OOS y 与 score 拼接。

    ⚠️ 关键设计 1：按**可用样本序号**切分，不按日历位置。
       原因（首版实测踩坑）：标签需要 ≥90 日历史定分位阈值，故 y 起点约在第 149 天，
       晚于 TRAIN_MIN=200 → 按日历切的前几折训练集为空 → 全程 n=0。

    ⚠️ 关键设计 2：purge gap。t 日标签覆盖 t→t+H，若训练集尾部与测试集头部
       相隔不足 H 日，标签会**跨折重叠**——这正是 round72 被判「前视」的同一个坑
       （round73 已明确定性：重叠 75% → 价差 +0.30 作废）。此处强制留出 purge 日。
    """
    yt_all, sc_all = [], []
    n_all = len(idx_all)
    start = min_train
    while start < n_all:
        te_pos = np.arange(start, min(start + step, n_all))
        _tr_end = max(0, start - purge)          # 训练集尾部退 purge 日
        tr = idx_all[:_tr_end]
        te = idx_all[te_pos]
        if len(te) == 0:
            break
        if len(tr) < 60 or len(np.unique(y[tr])) < 2:
            start += step
            continue
        mu, sd = zscore_fit(F[tr])
        m = LogitBin().fit((F[tr] - mu) / sd, y[tr].astype(float))
        yt_all.append(y[te])
        sc_all.append(m.score((F[te] - mu) / sd))
        start += step
    if not yt_all:
        return None, None
    return np.concatenate(yt_all), np.concatenate(sc_all)


def run_t1_t2(close, F, names, H):
    n = len(close)
    out = {}
    # ---- T1 高波动状态 ----
    fv = fwd_vol(close, H)
    valid = np.isfinite(F).all(axis=1) & np.isfinite(fv)
    idx = np.where(valid)[0]
    if len(idx) > TRAIN_MIN:
        # 阈值用「训练窗内」的 70% 分位（滚动，避免全样本前视）
        y = np.full(n, -1, dtype=int)
        for t in idx:
            hist = fv[idx[idx <= t]]
            if len(hist) < MIN_HIST_FOR_QUANTILE:
                continue
            q70 = np.quantile(hist[-250:] if len(hist) >= 250 else hist, 0.70)
            y[t] = 1 if fv[t] >= q70 else 0
        idx2 = np.where(y >= 0)[0]
        yt, sc = walk_forward_binary(F, y, idx2)
        if yt is not None:
            out["T1_high_vol"] = {
                "n_oos": int(len(yt)), "pos_rate": round(float(yt.mean()), 3),
                "auc": round(auc_score(yt, sc), 4),
                "base_acc": round(float((yt == 0).mean()), 4),
                "acc": round(float(((sc >= 0.5).astype(int) == yt).mean()), 4),
            }
    # ---- T2 深回撤状态 ----
    md = fwd_mdd(close, H)
    valid2 = np.isfinite(F).all(axis=1) & np.isfinite(md)
    idx = np.where(valid2)[0]
    if len(idx) > TRAIN_MIN:
        y = np.full(n, -1, dtype=int)
        for t in idx:
            hist = md[idx[idx <= t]]
            if len(hist) < MIN_HIST_FOR_QUANTILE:
                continue
            q70 = np.quantile(hist[-250:] if len(hist) >= 250 else hist, 0.70)
            y[t] = 1 if md[t] >= q70 else 0
        idx2 = np.where(y >= 0)[0]
        yt, sc = walk_forward_binary(F, y, idx2)
        if yt is not None:
            out["T2_deep_dd"] = {
                "n_oos": int(len(yt)), "pos_rate": round(float(yt.mean()), 3),
                "auc": round(auc_score(yt, sc), 4),
                "base_acc": round(float((yt == 0).mean()), 4),
                "acc": round(float(((sc >= 0.5).astype(int) == yt).mean()), 4),
            }
    return out


# ---------------------------------------------------------------- T3 横截面特征
def build_breadth_features():
    """从 K 线库构造真实横截面特征（严格只用当日收盘，无前视）。

    ⚠️ 已知偏差：K 线库是「当前池」的股票 → 幸存者偏差。
       breadth 的**绝对水平**会偏高，但**时序变化**仍可用；
       因此只把它作为「情绪极端度」的代理，不解读为全市场宽度。
    """
    p = os.path.join(HERE, "data", "klines_long.json")
    if not os.path.exists(p):
        return None, None
    kl = json.load(open(p, encoding="utf-8"))
    day_rets = {}
    for code, series in kl.items():
        if not isinstance(series, dict):
            continue
        ks = sorted(series.keys())
        prev = None
        for k in ks:
            row = series[k]
            if not isinstance(row, (list, tuple)) or len(row) < 2:
                continue
            c = float(row[1])
            if c <= 0:
                continue
            if prev is not None and prev > 0:
                day_rets.setdefault(k, []).append(c / prev - 1.0)
            prev = c
    dates = sorted(day_rets.keys())
    feats = {}
    for k in dates:
        arr = np.asarray(day_rets[k], dtype=float)
        if len(arr) < 200:
            continue
        feats[k] = {
            "breadth_up": float((arr > 0).mean()),
            "dispersion": float(arr.std(ddof=1)),
            "limit_ratio": float((np.abs(arr) > 0.09).mean()),
            "big_up_ratio": float((arr > 0.05).mean()),
            "skew": float(((arr - arr.mean()) ** 3).mean() / (arr.std() ** 3 + 1e-12)),
            "n_stocks": int(len(arr)),
        }
    return feats, dates


def main():
    out = {"meta": {
        "round": "109b",
        "purpose": "找可预测的那部分：波动状态/下行风险/横截面宽度",
        "train_min": TRAIN_MIN, "step": STEP,
        "criteria": {"T1_auc_min": 0.65, "T2_auc_min": 0.63, "T3_lift_pp_min": 3.0},
    }}

    # ---- T1/T2 on all indices ----
    for code, cname in (("000985", "中证全指"), ("000300", "沪深300"),
                        ("000905", "中证500"), ("000852", "中证1000")):
        dates, close = load_bench(code)
        F, names = build_features(close)
        blk = {"name": cname, "n_days": len(close)}
        for H in HORIZONS:
            r = run_t1_t2(close, F, names, H)
            blk[f"H{H}"] = r
            print(f"\n===== {cname} H={H} =====")
            for k, v in r.items():
                print(f"  {k}: n_oos={v['n_oos']} pos={v['pos_rate']:.2f} "
                      f"AUC={v['auc']:.3f} acc={v['acc']:.3f} (基线{v['base_acc']:.3f})"
                      f" {'✅过' if (k.startswith('T1') and v['auc']>=0.65) or (k.startswith('T2') and v['auc']>=0.63) else '❌未过'}")
        out[code] = blk

    # ---- T3 宽度特征 ----
    print("\n\n===== T3 横截面宽度特征 =====")
    feats, fdates = build_breadth_features()
    if feats:
        print(f"  构造成功: {len(feats)} 天, {fdates[0]} → {fdates[-1]}")
        out["T3_breadth"] = {"n_days": len(feats), "range": [fdates[0], fdates[-1]],
                             "sample": {k: feats[fdates[-1]][k] for k in
                                        ("breadth_up", "dispersion", "limit_ratio", "n_stocks")}}
        # 用中证全指对齐，测宽度特征对「单边 vs 震荡」的增量
        dates, close = load_bench("000985")
        F, names = build_features(close)
        d2i = {d: i for i, d in enumerate(dates)}
        # 逐日宽度序列
        cols = ["breadth_up", "dispersion", "limit_ratio", "big_up_ratio", "skew"]
        B = np.full((len(dates), len(cols)), np.nan)
        for k, v in feats.items():
            i = d2i.get(k)
            if i is not None:
                for j, c in enumerate(cols):
                    B[i, j] = v[c]
        # 宽度特征的 5 日均值（平滑，降低噪声）
        Bs = np.full_like(B, np.nan)
        for i in range(4, len(B)):
            w = B[i - 4:i + 1]
            if np.isfinite(w).all():
                Bs[i] = w.mean(axis=0)
        # ⚠️ 宽度序列比指数序列短（K 线库 2025-06-10 才起），
        #   若要求「全部特征都齐」会得到 n=0（首版实测踩到）。
        #   这里改为：只在**宽度可用区间**上做对比，基础集也在同一区间重算，
        #   保证 base vs +breadth 是**同一样本、同一折划分**的可比对照。
        _b_ok = np.isfinite(Bs).all(axis=1)
        if not _b_ok.any():
            print("  ⚠️ 宽度序列与指数无重叠区间，T3 跳过")
            out["T3_breadth"]["error"] = "no overlap"
        else:
            _lo, _hi = int(np.argmax(_b_ok)), int(len(_b_ok) - np.argmax(_b_ok[::-1]) - 1)
            print(f"  宽度可用区间: idx {_lo}~{_hi} ({dates[_lo]} → {dates[_hi]})")
            out["T3_breadth"]["overlap"] = [dates[_lo], dates[_hi]]
            Fx = np.column_stack([F, Bs])
            namesx = names + [f"{c}_ma5" for c in cols]
            for H in (10, 20):
                from audit_round109_regime_feasibility import build_labels
                sigma20 = F[:, names.index("vol20")]
                y, _ = build_labels(close, H, sigma20)
                # === 同区间对照 ===
                common = np.isfinite(F).all(axis=1) & (y >= 0) & _b_ok
                idxc = np.where(common)[0]
                # 基础集（仅同区间内可用列 —— 用 F 前 len(names) 列，不含宽度）
                r_base = _eval_binary(F, y, idxc, names)
                # 加宽度
                validx = common & np.isfinite(Fx).all(axis=1)
                idxx = np.where(validx)[0]
                r_ext = _eval_binary(Fx, y, idxx, namesx)
                _lift = (r_ext["acc_bi"] - r_base["acc_bi"]) * 100 if (
                    r_base["acc_bi"] == r_base["acc_bi"] and r_ext["acc_bi"] == r_ext["acc_bi"]) else float("nan")
                print(f"  H={H}: 基础集 单边acc={r_base['acc_bi']:.3f} (n={r_base['n']}) | "
                      f"+宽度集 单边acc={r_ext['acc_bi']:.3f} (n={r_ext['n']}) | lift {_lift:+.1f}pp")
                out["T3_breadth"][f"H{H}"] = {"base": r_base, "with_breadth": r_ext,
                                              "lift_pp": round(_lift, 2) if _lift == _lift else None}
    else:
        print("  ⚠️ K 线库不可用，T3 跳过")
        out["T3_breadth"] = {"error": "klines_long.json 缺失"}

    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print(f"\n✅ 已写出 {OUT}")
    return 0


def _eval_binary(F, y, idx, names):
    """「震荡(0) vs 单边(1)」二分类滚动 OOS。"""
    ybi = np.full(len(y), -1, dtype=int)
    ybi[(y == 1) | (y == 2)] = 1
    ybi[y == 0] = 0
    idxb = idx[ybi[idx] >= 0]
    if len(idxb) < 150:
        return {"n": 0, "acc_bi": float("nan")}
    yt, sc = walk_forward_binary(F, ybi, idxb)
    if yt is None:
        return {"n": 0, "acc_bi": float("nan")}
    return {"n": int(len(yt)),
            "acc_bi": round(float(((sc >= 0.5).astype(int) == yt).mean()), 4),
            "auc": round(auc_score(yt, sc), 4),
            "pos_rate": round(float(yt.mean()), 3)}


if __name__ == "__main__":
    sys.exit(main())
