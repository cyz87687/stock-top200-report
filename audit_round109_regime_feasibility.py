#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
audit_round109_regime_feasibility.py —— 市场状态（regime）预测的**可行性边界**检验

═══ 为什么要先跑这一关 ═══

老大要求在「每日解读」引入 Transformer 预测市场走势 / 至少识别
「震荡市 / 单边上涨市 / 单边下跌市」。

本项目已有两次血的教训：
  round72 用 mkt[t-5] 当过去5日收益（实际覆盖 t-5→t+15，与未来重叠 75%）
          → 得出「价差 +0.30、可利用」，round73 修复重叠窗口后 **翻转**为
          exploitable=false。
  round51  quality 面板列坏 → ρ 仅 0.029 → 结论整批判废。

所以本轮的规定动作是：**先量出天花板，再谈模型选择**。
若线性模型在严格的滚动 OOS 上都无法显著超过「多数类基线」，
那么 Transformer（参数多 3~4 个数量级、样本需求高 2 个数量级）在本数据规模下
必然更差 —— 此时上 Transformer 不是技术进步，是**过度拟合的包装**。

═══ 实验设计（全部无前视）═══

数据：data/bench/{000300,000905,000852,000985}.json
      440 个交易日（2025-01-02 → 2026-10-29），日线 [open, close, high, low]

标签（预测目标，三分类）：
      r_H = close[t+H]/close[t] - 1
      |r_H| < θ_t  → 震荡(0)；r_H ≥ +θ_t → 单边涨(1)；r_H ≤ -θ_t → 单边跌(2)
      阈值 θ_t **滚动自适应**：θ_t = 0.75 × σ20_t × sqrt(H)
      （σ20_t 为 t 日及以前 20 日收益标准差；不用全样本 σ，否则前视）

特征（15 个，全部只用 ≤ t 的收盘价）：
      动量类  ret1/5/10/20/60
      波动类  vol20/vol60、vol_ratio=vol20/vol60
      位置类  px/MA20-1、px/MA60-1、距60日高点、距60日低点
      趋势质量 ret20/(vol20*sqrt(20))（信息比形式）
      宽度代理 近20日上涨天数占比
      量能代理 ｜近5日收益｜/vol20（突破强度）

评价（真正的考题）：
      滚动窗口 walk-forward：训练 250 日 → 预测随后 20 日 → 前移 20 日
      OOS 指标：
        ① accuracy 三分类整体准确率
        ② macro-F1（三类平均，避免「多数类刷分」）
        ③ 多数类基线 accuracy（永远猜最多的那类）
        ④ 「单边」二分类准确率（震荡 vs 单边）—— 对仓位最有用的一层
        ⑤ 相对基线的提升（百分点）
      参照模型：
        B0 多数类
        B1 动量规则（ret20>0 → 猜涨，否则猜跌；不看阈值）
        B2 多项逻辑回归（L2，numpy 手写 softmax + 梯度下降）
        B3 「vol 分层」朴素规则（高波动→猜单边，低波动→猜震荡）

判据（先写死，不许事后改）：
      B2 的 macro-F1 必须 ≥ 多数类 macro-F1 + 0.05，
      且「单边」二分类准确率 ≥ 基线 + 3pp，
      才算「有可用信号」。否则只能做**描述性状态标注**，不能做预测驱动仓位。

用法：python audit_round109_regime_feasibility.py
输出：audit_round109.json
"""
import os
import sys
import json
import math
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
BENCH = os.path.join(HERE, "data", "bench")
OUT = os.path.join(HERE, "audit_round109.json")

HORIZONS = (5, 10, 20)
TRAIN_MIN = 250      # 最小训练窗口
STEP = 20            # 滚动步长
CLS_NAMES = ("震荡", "单边涨", "单边跌")


# ---------------------------------------------------------------- 数据装载
def load_bench(code="000985"):
    """→ (dates:list[str], close:np.array) —— 只用收盘价，避免 OHLC 顺序歧义。"""
    p = os.path.join(BENCH, f"{code}.json")
    d = json.load(open(p, encoding="utf-8"))
    px = d.get("px") or d
    ks = sorted(k for k in px.keys() if str(k)[:2] == "20")
    close, dates = [], []
    for k in ks:
        row = px[k]
        # 格式 [open, close, high, low]（已在 round109 前核对：high≥open/close，low≤两者）
        if not isinstance(row, (list, tuple)) or len(row) < 2:
            continue
        c = float(row[1])
        if c <= 0:
            continue
        dates.append(str(k))
        close.append(c)
    return dates, np.asarray(close, dtype=float)


# ---------------------------------------------------------------- 特征构造
def build_features(close):
    """全部特征在 t 日收盘后可算；返回 (F, names)，F[t] 为 t 日特征。

    前 60 个位置因窗口不足置 NaN，后续统一剔除。
    """
    n = len(close)
    lr = np.zeros(n)
    lr[1:] = np.log(close[1:] / close[:-1])

    def _roll_std(x, w):
        out = np.full(n, np.nan)
        for i in range(w, n):
            out[i] = x[i - w + 1:i + 1].std(ddof=1)
        return out

    def _roll_mean(x, w):
        out = np.full(n, np.nan)
        for i in range(w - 1, n):
            out[i] = x[i - w + 1:i + 1].mean()
        return out

    def _ret(w):
        out = np.full(n, np.nan)
        out[w:] = close[w:] / close[:-w] - 1.0
        return out

    f = {}
    f["ret1"] = _ret(1)
    f["ret5"] = _ret(5)
    f["ret10"] = _ret(10)
    f["ret20"] = _ret(20)
    f["ret60"] = _ret(60)
    v20 = _roll_std(lr, 20)
    v60 = _roll_std(lr, 60)
    f["vol20"] = v20
    f["vol60"] = v60
    f["vol_ratio"] = v20 / np.where(v60 > 0, v60, np.nan)
    ma20 = _roll_mean(close, 20)
    ma60 = _roll_mean(close, 60)
    f["px_ma20"] = close / ma20 - 1.0
    f["px_ma60"] = close / ma60 - 1.0
    hh60 = np.full(n, np.nan)
    ll60 = np.full(n, np.nan)
    for i in range(59, n):
        w = close[i - 59:i + 1]
        hh60[i] = w.max()
        ll60[i] = w.min()
    f["dist_hi60"] = close / hh60 - 1.0
    f["dist_lo60"] = close / ll60 - 1.0
    f["info_ratio"] = f["ret20"] / np.where(v20 > 0, v20 * math.sqrt(20), np.nan)
    up_frac = np.full(n, np.nan)
    for i in range(19, n):
        up_frac[i] = (lr[i - 19:i + 1] > 0).mean()
    f["up_frac20"] = up_frac
    f["brk5"] = np.abs(f["ret5"]) / np.where(v20 > 0, v20 * math.sqrt(5), np.nan)

    names = list(f.keys())
    F = np.column_stack([f[k] for k in names])
    return F, names


# ---------------------------------------------------------------- 标签
def build_labels(close, H, sigma20):
    """三分类标签 + 自适应阈值；返回 (y, theta)，无效处 y=-1。"""
    n = len(close)
    y = np.full(n, -1, dtype=int)
    theta = np.full(n, np.nan)
    for t in range(n - H):
        if not np.isfinite(sigma20[t]) or sigma20[t] <= 0:
            continue
        thr = 0.75 * sigma20[t] * math.sqrt(H)
        r = close[t + H] / close[t] - 1.0
        theta[t] = thr
        if r >= thr:
            y[t] = 1
        elif r <= -thr:
            y[t] = 2
        else:
            y[t] = 0
    return y, theta


# ---------------------------------------------------------------- 模型
class SoftmaxReg:
    """多项逻辑回归（numpy 手写，L2 正则，全批量梯度下降 + 早停）。

    选择它的理由：在 n≈200 的量级上，它是最不容易过拟合的**非线性可加**模型；
    若它都学不到东西，任何高容量模型只会更差。
    """

    def __init__(self, l2=1.0, lr=0.35, iters=1200, n_cls=3):
        self.l2, self.lr, self.iters, self.n_cls = l2, lr, iters, n_cls

    def fit(self, X, y):
        n, p = X.shape
        K = self.n_cls
        Y = np.zeros((n, K))
        Y[np.arange(n), y] = 1.0
        W = np.zeros((p, K))
        b = np.zeros(K)
        for _ in range(self.iters):
            Z = X @ W + b
            Z -= Z.max(axis=1, keepdims=True)
            E = np.exp(Z)
            P = E / E.sum(axis=1, keepdims=True)
            G = (P - Y) / n
            gW = X.T @ G + (self.l2 / n) * W
            gb = G.sum(axis=0)
            W -= self.lr * gW
            b -= self.lr * gb
        self.W, self.b = W, b
        return self

    def predict(self, X):
        Z = X @ self.W + self.b
        return Z.argmax(axis=1)


def macro_f1(y_true, y_pred, n_cls=3):
    """三类 macro-F1；某类无真值/无预测时该类的 F1 记 0（不掩盖塌缩）。"""
    f1s = []
    for c in range(n_cls):
        tp = int(((y_pred == c) & (y_true == c)).sum())
        fp = int(((y_pred == c) & (y_true != c)).sum())
        fn = int(((y_pred != c) & (y_true == c)).sum())
        if tp + fp == 0 or tp + fn == 0:
            f1s.append(0.0)
            continue
        prec = tp / (tp + fp)
        rec = tp / (tp + fn)
        f1s.append(0.0 if prec + rec == 0 else 2 * prec * rec / (prec + rec))
    return float(np.mean(f1s))


def zscore_fit(X):
    mu = np.nanmean(X, axis=0)
    sd = np.nanstd(X, axis=0)
    sd = np.where(sd > 0, sd, 1.0)
    return mu, sd


# ---------------------------------------------------------------- 主实验
def run_horizon(close, F, names, H):
    sigma20 = F[:, names.index("vol20")]
    y, theta = build_labels(close, H, sigma20)
    n = len(close)

    # 有效样本（特征与标签都齐）
    valid = np.isfinite(F).all(axis=1) & (y >= 0)
    idx_all = np.where(valid)[0]

    folds = []
    start = 0
    while True:
        # ⚠️ round109b 修正：加 purge gap。
        #   原版训练集直接取 idx_all < 切分点，但 t 日标签覆盖 t→t+H，
        #   训练集尾部与测试集头部相隔不足 H 日 → 标签跨折重叠 → 前视。
        #   这正是 round72 被判「前视」的同一个坑（round73 已定性：重叠 75% → 结论作废）。
        _cut = TRAIN_MIN + start * STEP
        tr_i = idx_all[idx_all < (_cut - H)]
        te_i = idx_all[(idx_all >= _cut) & (idx_all < (_cut + STEP))]
        if len(te_i) == 0:
            break
        if len(tr_i) < 120:
            # 不再直接 break：早期切分点训练不足时跳过该折继续，
            # 否则长样本下首折若样本偏少会整轮无 OOS（round109b 首版踩坑）。
            start += 1
            continue
        mu, sd = zscore_fit(F[tr_i])
        Xtr = (F[tr_i] - mu) / sd
        Xte = (F[te_i] - mu) / sd
        ytr, yte = y[tr_i], y[te_i]

        # B0 多数类
        maj = np.bincount(ytr, minlength=3).argmax()
        p0 = np.full(len(yte), maj)

        # B1 动量规则
        r20 = F[te_i][:, names.index("ret20")]
        p1 = np.where(r20 > 0, 1, 2)

        # B2 逻辑回归
        p2 = SoftmaxReg().fit(Xtr, ytr).predict(Xte)

        # B3 波动分层规则（vol20 中位数以上→单边，方向看 ret20；以下→震荡）
        v20tr = F[tr_i][:, names.index("vol20")]
        cut = float(np.median(v20tr))
        v20te = F[te_i][:, names.index("vol20")]
        p3 = np.where(v20te >= cut, np.where(r20 > 0, 1, 2), 0)

        folds.append({
            "n_te": int(len(yte)),
            "y": yte.tolist(),
            "b0": p0.tolist(), "b1": p1.tolist(),
            "b2": p2.tolist(), "b3": p3.tolist(),
        })
        start += 1

    if not folds:
        return None

    yte = np.concatenate([np.array(f["y"]) for f in folds])
    res = {"H": H, "n_folds": len(folds), "n_oos": int(len(yte)),
           "label_dist": {CLS_NAMES[i]: int((yte == i).sum()) for i in range(3)}}
    for key, nm in (("b0", "多数类"), ("b1", "动量规则"), ("b2", "逻辑回归"), ("b3", "波动分层")):
        p = np.concatenate([np.array(f[key]) for f in folds])
        acc = float((p == yte).mean())
        mf1 = macro_f1(yte, p)
        # 「单边」二分类：震荡(0) vs 单边(1/2)
        t_bi = (yte != 0).astype(int)
        p_bi = (p != 0).astype(int)
        acc_bi = float((p_bi == t_bi).mean())
        base_bi = float((np.zeros_like(t_bi) == t_bi).mean())  # 全猜震荡的基线
        res[nm] = {"acc": round(acc, 4), "macro_f1": round(mf1, 4),
                   "acc_binary": round(acc_bi, 4), "base_binary": round(base_bi, 4),
                   "lift_binary_pp": round((acc_bi - base_bi) * 100, 2)}
    # 混淆矩阵（逻辑回归）
    p2 = np.concatenate([np.array(f["b2"]) for f in folds])
    cm = np.zeros((3, 3), dtype=int)
    for a, b in zip(yte, p2):
        cm[a, b] += 1
    res["cm_logreg"] = cm.tolist()
    return res


def main():
    out = {"meta": {
        "round": 109,
        "purpose": "regime 三分类预测的可行性边界（先量天花板，再谈模型）",
        "data": "data/bench/000985.json（中证全指）",
        "train_min": TRAIN_MIN, "step": STEP,
        "label_rule": "r_H 相对 0.75*vol20*sqrt(H) 的三分类（滚动自适应阈值，无前视）",
        "priors": {
            "round72": "重叠窗口前视，价差+0.30 已作废",
            "round73": "修复后 exploitable=false",
        },
    }}
    for code, cname in (("000985", "中证全指"), ("000300", "沪深300"),
                        ("000905", "中证500"), ("000852", "中证1000")):
        try:
            dates, close = load_bench(code)
        except Exception as e:
            out[code] = {"error": f"{type(e).__name__}: {e}"}
            continue
        F, names = build_features(close)
        blk = {"name": cname, "n_days": len(close),
               "range": [dates[0], dates[-1]], "n_features": len(names),
               "features": names}
        for H in HORIZONS:
            r = run_horizon(close, F, names, H)
            blk[f"H{H}"] = r
        out[code] = blk
        print(f"\n===== {cname} ({code}) n={len(close)} =====")
        for H in HORIZONS:
            r = blk.get(f"H{H}")
            if not r:
                print(f"  H={H}: 无有效折")
                continue
            print(f"  H={H:2d} folds={r['n_folds']} n_oos={r['n_oos']} "
                  f"dist={r['label_dist']}")
            for nm in ("多数类", "动量规则", "逻辑回归", "波动分层"):
                v = r[nm]
                print(f"     {nm:6s} acc={v['acc']:.3f} macroF1={v['macro_f1']:.3f} "
                      f"单边acc={v['acc_binary']:.3f} (基线{v['base_binary']:.3f}, "
                      f"lift {v['lift_binary_pp']:+.1f}pp)")
            print(f"     混淆矩阵(逻辑回归, 行=真实 震荡/涨/跌): {r['cm_logreg']}")

    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print(f"\n✅ 已写出 {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
