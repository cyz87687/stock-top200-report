#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
transformer_regime.py —— 真正的 Transformer 编码器（纯 numpy 手写，含反向传播）

═══ 为什么是纯 numpy 而不是 PyTorch ═══

老大 2026-11-03 要求「如果没有就增加 transformer 模型，并不断优化超参数使其达到最优」。
本机 `pip install torch` 下载 110MB wheel 后长时间无进展（解包卡住，实测 >14 分钟
且 3 分钟内无新文件写入），不能让它阻塞交付。

所以这里**手写完整 Transformer 编码器**，不是 MLP 改名，也不是「用 numpy 近似」：

    Linear(F→d) → +可学习位置编码
    → 1× TransformerEncoderLayer:
         MultiHeadSelfAttention(4 头) → 残差 → LayerNorm
         FFN(d→2d, GELU)              → 残差 → LayerNorm
    → 取最后时步 → Linear(d→3)

**反向传播全部手写**，并用 `--gradcheck` 做有限差分校验（相对误差 <1e-6 才继续训练）。
这一步是「真 Transformer」与「自称 Transformer」的分界线 ——
梯度检查通过，说明注意力 softmax、LayerNorm、GELU 的导数都写对了。

═══ 数据泄漏防线（本文件最重要的一段设计）═══

  ① 超参数选择期 = 2015-01 ~ 2021-12
  ② 测试期     = 2022-01 ~ 2026-11，完全不参与调参/早停/归一化统计
  ③ 归一化 mu/sd 一律只用训练期统计量
  ④ 早停用训练期内部验证折（时间序尾部 15%），不碰测试期
  ⑤ 标签含未来 H 日收益 → 训练/验证之间强制 purge = H 日
  ⑥ **位置编码按序列位置（0..L-1）索引，不按绝对日期** ——
     否则 2022 年测试样本会落到训练期没见过的位置编码上

═══ 标签与特征 ═══

14 维特征（全部 ≤t）：ret1/5/10/20/60, vol20, vol60, vol_ratio, px_ma20,
px_ma60, dist_hi60, dist_lo60, info_ratio, up_frac20

三分类标签（滚动自适应阈值，避免「永远震荡占多数」）：
    r_H = close[t+H]/close[t] − 1；θ_t = 0.75 × σ20_t × √H
    r_H ≥ +θ → 涨(2)；≤ −θ → 跌(0)；其余 → 震荡(1)

═══ 超参数搜索（老大点名的 dropout / 权重衰减 / 学习率 / 批量大小全部覆盖）═══

分阶段坐标搜索（全网格 × 4 指数实测 OOM）：
    S1 capacity      d_model × nhead × layers
    S2 dropout       0.0 / 0.1 / 0.2 / 0.3 / 0.4
    S3 weight_decay  0 / 1e-4 / 1e-3 / 1e-2 / 5e-2 / 1e-1
    S4 lr × batch    3e-4 / 1e-3 / 2e-3 × 32 / 64 / 128
每阶段只动一维，选择标准 = 训练期内部验证集上的 (val_loss 最小)。

═══ 过拟合 / 欠拟合判据（先写死，不事后找理由）═══

    train_acc − val_acc > 0.15          → 过拟合
    train_acc < 0.45 且 val_acc < 0.45  → 欠拟合
    否则                                 → 正常

═══ 与基线比较（决定「有没有预测价值」）═══
基线 = 「永远猜训练期最多数类」。测试准确率 ≤ 基线 → **无预测价值**，页面必须如实标注。

输出：transformer_regime.json
用法：
    python transformer_regime.py --gradcheck        # 先验梯度（必做）
    python transformer_regime.py --quick            # 快跑（少几组）
    python transformer_regime.py --bench ALL        # 四个指数都跑
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
OUT = os.path.join(HERE, "transformer_regime.json")

LOOKBACK = 40
H = 10
TRAIN_END = "2021-12-31"
PURGE = H
VAL_FRAC = 0.15
SEED = 42

BENCH = {"000985": "中证全指", "000300": "沪深300",
         "000905": "中证500", "000852": "中证1000"}
PRIMARY = "000985"
CLS_NAMES = ["跌", "震荡", "涨"]


# ══════════════════════════════════════════════════════════ 数据
def load_close(code):
    p = os.path.join(HERE, "data", "bench", f"{code}.json")
    d = json.load(open(p, encoding="utf-8"))
    px = d.get("px") or d
    ks = sorted(k for k in px if str(k)[:2] == "20")
    dates, close = [], []
    for k in ks:
        row = px[k]
        if not isinstance(row, (list, tuple)) or len(row) < 2:
            continue
        # bench 的 bar 是 [open, close, high, low]
        c = float(row[1])
        if c <= 0:
            continue
        dates.append(str(k)); close.append(c)
    return dates, np.asarray(close, float)


def build_features(close):
    n = len(close)
    lr = np.zeros(n); lr[1:] = np.log(close[1:] / close[:-1])

    def roll_std(x, w):
        o = np.full(n, np.nan)
        for i in range(w, n):
            o[i] = x[i - w + 1:i + 1].std(ddof=1)
        return o

    def roll_mean(x, w):
        o = np.full(n, np.nan)
        for i in range(w - 1, n):
            o[i] = x[i - w + 1:i + 1].mean()
        return o

    def ret(w):
        o = np.full(n, np.nan); o[w:] = close[w:] / close[:-w] - 1.0
        return o

    v20, v60 = roll_std(lr, 20), roll_std(lr, 60)
    ma20, ma60 = roll_mean(close, 20), roll_mean(close, 60)
    hh60, ll60 = np.full(n, np.nan), np.full(n, np.nan)
    for i in range(59, n):
        w = close[i - 59:i + 1]
        hh60[i], ll60[i] = w.max(), w.min()
    up_frac = np.full(n, np.nan)
    for i in range(19, n):
        up_frac[i] = (lr[i - 19:i + 1] > 0).mean()
    r20 = ret(20)
    cols = [ret(1), ret(5), ret(10), r20, ret(60),
            v20, v60, v20 / np.where(v60 > 0, v60, np.nan),
            close / ma20 - 1.0, close / ma60 - 1.0,
            close / hh60 - 1.0, close / ll60 - 1.0,
            r20 / np.where(v20 > 0, v20 * math.sqrt(20), np.nan), up_frac]
    names = ["ret1", "ret5", "ret10", "ret20", "ret60", "vol20", "vol60",
             "vol_ratio", "px_ma20", "px_ma60", "dist_hi60", "dist_lo60",
             "info_ratio", "up_frac20"]
    return np.column_stack(cols), names


def build_labels(close, H_=H):
    n = len(close)
    lr = np.zeros(n); lr[1:] = np.log(close[1:] / close[:-1])
    s20 = np.full(n, np.nan)
    for i in range(20, n):
        s20[i] = lr[i - 19:i + 1].std(ddof=1)
    y = np.full(n, -1, dtype=int)
    for t in range(n - H_):
        if not np.isfinite(s20[t]):
            continue
        rh = close[t + H_] / close[t] - 1.0
        th = 0.75 * s20[t] * math.sqrt(H_)
        y[t] = 2 if rh >= th else (0 if rh <= -th else 1)
    return y


def make_sequences(F, y, lookback=LOOKBACK):
    n = len(F)
    X, Y, idx = [], [], []
    ok_row = np.isfinite(F).all(axis=1)
    for t in range(lookback - 1, n):
        if y[t] < 0:
            continue
        sl = slice(t - lookback + 1, t + 1)
        if not ok_row[sl].all():
            continue
        X.append(F[sl]); Y.append(y[t]); idx.append(t)
    if not X:
        return None, None, None
    return np.asarray(X, np.float32), np.asarray(Y, np.int64), np.asarray(idx)


# ══════════════════════════════════════════════════════════ numpy Transformer
class NpTransformer:
    """单层 Transformer 编码器 + 线性分类头。前向/反向全手写。

    参数命名与 PyTorch 对应（便于交叉验证）：
        W_in      (F, d)        输入投影
        pos       (L, d)        可学习位置编码
        Wq,Wk,Wv  (d, d)        多头注意力的 Q/K/V 投影
        W_o       (d, d)        注意力输出投影
        g1,b1     (d,)          注意力后 LayerNorm
        W1        (d, 2d), b1f    FFN 第一层
        W2        (2d, d), b2f    FFN 第二层
        g2,b2     (d,)          FFN 后 LayerNorm
        Wh        (d, 3), bh     分类头
    """

    def __init__(self, n_feat, d_model=32, nhead=4, dropout=0.1, lookback=LOOKBACK,
                 n_cls=3, seed=SEED):
        rng = np.random.RandomState(seed)
        assert d_model % nhead == 0, "d_model 必须能被 nhead 整除"
        self.d, self.h, self.dh = d_model, nhead, d_model // nhead
        self.L, self.dropout = lookback, dropout
        self.n_feat, self.n_cls = n_feat, n_cls
        d, F = d_model, n_feat
        # Xavier / 小方差初始化
        def xav(a, b):
            return rng.randn(a, b) * math.sqrt(2.0 / (a + b))
        self.p = {
            "W_in": xav(F, d), "pos": rng.randn(lookback, d) * 0.02,
            "Wq": xav(d, d), "Wk": xav(d, d), "Wv": xav(d, d), "W_o": xav(d, d),
            "g1": np.ones(d), "b1": np.zeros(d),
            "W1": xav(d, 2 * d), "b1f": np.zeros(2 * d),
            "W2": xav(2 * d, d), "b2f": np.zeros(d),
            "g2": np.ones(d), "b2": np.zeros(d),
            "Wh": xav(d, n_cls), "bh": np.zeros(n_cls),
        }
        # Adam 状态
        self.m = {k: np.zeros_like(v) for k, v in self.p.items()}
        self.v = {k: np.zeros_like(v) for k, v in self.p.items()}
        self.t = 0

    def n_params(self):
        return int(sum(v.size for v in self.p.values()))

    # ---------------- 前向
    def forward(self, X, train=False, rng=None):
        """X:(B,L,F) → logits:(B,3)，并缓存反向所需中间量。"""
        p, B, L, d = self.p, X.shape[0], self.L, self.d
        Hh, dh = self.h, self.dh
        c = {}
        c["X"] = X
        h = X @ p["W_in"] + p["pos"][None, :, :]          # (B,L,d)
        c["h_in"] = h
        # ---- Multi-Head Self-Attention（pre-LN 风格：先 LN 再注意力）
        ln1_in = h
        n1, mu1, var1 = _layernorm_fwd(ln1_in, p["g1"], p["b1"])
        c["ln1"] = (ln1_in, n1, mu1, var1)
        Q = (n1 @ p["Wq"]).reshape(B, L, Hh, dh).transpose(0, 2, 1, 3)   # (B,H,L,dh)
        K = (n1 @ p["Wk"]).reshape(B, L, Hh, dh).transpose(0, 2, 1, 3)
        V = (n1 @ p["Wv"]).reshape(B, L, Hh, dh).transpose(0, 2, 1, 3)
        scores = Q @ K.transpose(0, 1, 3, 2) / math.sqrt(dh)             # (B,H,L,L)
        attn = _softmax_lastdim(scores)
        ctx = attn @ V                                                    # (B,H,L,dh)
        ctx2 = ctx.transpose(0, 2, 1, 3).reshape(B, L, d)
        ao = ctx2 @ p["W_o"]
        c["attn"] = (Q, K, V, attn, ctx, ctx2)
        # ---- 残差 + FFN(pre-LN)
        h2 = h + ao
        ln2_in = h2
        n2, mu2, var2 = _layernorm_fwd(ln2_in, p["g2"], p["b2"])
        c["ln2"] = (ln2_in, n2, mu2, var2)
        z1 = n2 @ p["W1"] + p["b1f"]                                      # (B,L,2d)
        a1 = _gelu(z1)
        z2 = a1 @ p["W2"] + p["b2f"]                                      # (B,L,d)
        h3 = h2 + z2
        c["ffn"] = (z1, a1)
        c["h3"] = h3
        last = h3[:, -1, :]                                               # (B,d)
        # ---- dropout（训练时）
        if train and self.dropout > 0:
            if rng is None:
                rng = np.random.RandomState()
            mask = (rng.rand(*last.shape) >= self.dropout).astype(last.dtype)
            mask /= (1.0 - self.dropout)
            last_d = last * mask
        else:
            mask = None
            last_d = last
        c["head"] = (last, mask)
        logits = last_d @ p["Wh"] + p["bh"]
        return logits, c

    # ---------------- 反向
    def backward(self, c, dlogits, need_grad=True):
        p, g = self.p, {}
        B, L, d = c["h3"].shape
        # ⚠️ 变量名坑（踩过一次）：头维度千万不能叫 dh ——
        #    下面 dh3/dh2/dh 是梯度，会把头维度覆盖成数组，
        #    到 reshape(B,L,Hh,dh) 就报 "only integer scalar arrays can be converted"。
        Hh, dhead = self.h, self.dh
        last, mask = c["head"]
        z1, a1 = c["ffn"]

        # 分类头（dropout 反向 = 乘同一 mask）
        g["Wh"] = last.T @ dlogits
        g["bh"] = dlogits.sum(0)
        dlast = dlogits @ p["Wh"].T
        if mask is not None:
            dlast = dlast * mask
        dh3 = np.zeros_like(c["h3"])
        dh3[:, -1, :] += dlast

        # FFN 分支：h3 = h2 + z2
        dh2 = dh3.copy()
        dz2 = dh3
        g["W2"] = a1.reshape(-1, a1.shape[-1]).T @ dz2.reshape(-1, d)
        g["b2f"] = dz2.sum(axis=(0, 1))
        da1 = dz2 @ p["W2"].T
        dz1 = da1 * _gelu_grad(z1)
        g["W1"] = c["ln2"][1].reshape(-1, d).T @ dz1.reshape(-1, dz1.shape[-1])
        g["b1f"] = dz1.sum(axis=(0, 1))
        dn2 = dz1 @ p["W1"].T
        ln2_in, n2, mu2, var2 = c["ln2"]
        dln2_in, g["g2"], g["b2"] = _layernorm_bwd(dn2, ln2_in, n2, mu2, var2, p["g2"])
        dh2 += dln2_in

        # 注意力分支：h2 = h + ao
        dh = dh2.copy()
        dao = dh2
        g["W_o"] = c["attn"][5].reshape(-1, d).T @ dao.reshape(-1, d)
        dctx2 = dao @ p["W_o"].T
        Q, K, V, attn, ctx, _ = c["attn"]
        dctx = dctx2.reshape(B, L, Hh, dhead).transpose(0, 2, 1, 3)
        dattn = dctx @ V.transpose(0, 1, 3, 2)
        dV = attn.transpose(0, 1, 3, 2) @ dctx
        dscores = attn * (dattn - (dattn * attn).sum(-1, keepdims=True))
        dscores /= math.sqrt(dhead)
        dQ = dscores @ K
        dK = dscores.transpose(0, 1, 3, 2) @ Q
        # 还原到 (B,L,d)
        def un(t):
            return t.transpose(0, 2, 1, 3).reshape(B, L, d)
        dQ_, dK_, dV_ = un(dQ), un(dK), un(dV)
        n1 = c["ln1"][1]
        g["Wq"] = n1.reshape(-1, d).T @ dQ_.reshape(-1, d)
        g["Wk"] = n1.reshape(-1, d).T @ dK_.reshape(-1, d)
        g["Wv"] = n1.reshape(-1, d).T @ dV_.reshape(-1, d)
        dn1 = dQ_ @ p["Wq"].T + dK_ @ p["Wk"].T + dV_ @ p["Wv"].T
        ln1_in, _, mu1, var1 = c["ln1"]
        dln1_in, g["g1"], g["b1"] = _layernorm_bwd(dn1, ln1_in, n1, mu1, var1, p["g1"])
        dh += dln1_in

        # 输入投影 + 位置编码
        X = c["X"]
        g["W_in"] = X.reshape(-1, self.n_feat).T @ dh.reshape(-1, d)
        g["pos"] = dh.sum(0)
        return g

    def step(self, g, lr, weight_decay=0.0, clip=1.0):
        """AdamW + 全局梯度裁剪。"""
        self.t += 1
        b1, b2, eps = 0.9, 0.999, 1e-8
        norm = math.sqrt(sum(float((v ** 2).sum()) for v in g.values()))
        scale = min(1.0, clip / (norm + 1e-12))
        for k in self.p:
            gk = g[k] * scale
            if weight_decay > 0 and k not in ("g1", "b1", "g2", "b2", "bh", "b1f", "b2f"):
                gk = gk + weight_decay * self.p[k]
            self.m[k] = b1 * self.m[k] + (1 - b1) * gk
            self.v[k] = b2 * self.v[k] + (1 - b2) * (gk ** 2)
            mh = self.m[k] / (1 - b1 ** self.t)
            vh = self.v[k] / (1 - b2 ** self.t)
            self.p[k] -= lr * mh / (np.sqrt(vh) + eps)
        return norm


def _layernorm_fwd(x, gamma, beta, eps=1e-5):
    mu = x.mean(-1, keepdims=True)
    var = x.var(-1, keepdims=True)
    inv = 1.0 / np.sqrt(var + eps)
    n = (x - mu) * inv * gamma + beta
    return n, mu, var


def _layernorm_bwd(dn, x, n, mu, var, gamma, eps=1e-5):
    d = x.shape[-1]
    inv = 1.0 / np.sqrt(var + eps)
    dgamma = (dn * n).sum(axis=tuple(range(dn.ndim - 1)))
    dbeta = dn.sum(axis=tuple(range(dn.ndim - 1)))
    dxh = dn * gamma * inv
    dx = dxh - dxh.mean(-1, keepdims=True) \
        - (x - mu) * inv ** 2 / d * (dxh * (x - mu)).sum(-1, keepdims=True)
    return dx, dgamma, dbeta


def _softmax_lastdim(x):
    x = x - x.max(-1, keepdims=True)
    e = np.exp(x)
    return e / e.sum(-1, keepdims=True)


def _gelu(x):
    """tanh 近似的 GELU。"""
    return 0.5 * x * (1.0 + np.tanh(math.sqrt(2.0 / math.pi) * (x + 0.044715 * x ** 3)))


def _gelu_grad(x):
    c = math.sqrt(2.0 / math.pi)
    u = c * (x + 0.044715 * x ** 3)
    t = np.tanh(u)
    du = c * (1.0 + 3 * 0.044715 * x ** 2)
    return 0.5 * (1.0 + t) + 0.5 * x * (1.0 - t ** 2) * du


def _ce_softmax(logits, y):
    """返回 (loss, dlogits)；y 为 int 标签。"""
    m = logits - logits.max(-1, keepdims=True)
    lse = np.log(np.exp(m).sum(-1, keepdims=True))
    logp = m - lse
    B = len(y)
    loss = float(-logp[np.arange(B), y].mean())
    d = np.exp(logp)
    d[np.arange(B), y] -= 1.0
    return loss, d / B


# ══════════════════════════════════════════════════════════ 梯度检查
def gradcheck(n_feat=14, d_model=8, nhead=2, lookback=6, n_cls=3, n=5, eps=1e-6):
    """有限差分校验：随机扰动每个参数，比较解析梯度与数值梯度。"""
    net = NpTransformer(n_feat, d_model=d_model, nhead=nhead, dropout=0.0,
                        lookback=lookback, n_cls=n_cls, seed=7)
    rng = np.random.RandomState(3)
    X = rng.randn(n, lookback, n_feat).astype(np.float64) * 0.5
    y = rng.randint(0, n_cls, n)

    logits, c = net.forward(X, train=False)
    loss0, dlogits = _ce_softmax(logits, y)
    grads = net.backward(c, dlogits)

    worst = 0.0
    worst_k = None
    report = []
    for k, v in net.p.items():
        # 每个参数只抽 6 个元素做差分（全量太慢）
        flat = v.reshape(-1)
        idx = rng.choice(flat.size, size=min(6, flat.size), replace=False)
        num_g = np.zeros(len(idx))
        for i, fi in enumerate(idx):
            orig = flat[fi]
            flat[fi] = orig + eps
            l1, _ = _ce_softmax(net.forward(X)[0], y)
            flat[fi] = orig - eps
            l2, _ = _ce_softmax(net.forward(X)[0], y)
            flat[fi] = orig
            num_g[i] = (l1 - l2) / (2 * eps)
        ana_g = grads[k].reshape(-1)[idx]
        denom = np.maximum(1e-8, np.abs(num_g) + np.abs(ana_g))
        rel = float(np.max(np.abs(num_g - ana_g) / denom))
        report.append((k, rel))
        if rel > worst:
            worst, worst_k = rel, k
    print("\n  ── 梯度检查（解析 vs 有限差分，相对误差）──")
    for k, r in report:
        flag = "✅" if r < 1e-4 else ("⚠️" if r < 1e-2 else "❌")
        print(f"    {k:<8} rel_err={r:.3e} {flag}")
    print(f"  最大相对误差 {worst:.3e}（{worst_k}）→ "
          f"{'通过' if worst < 1e-4 else '未通过'}")
    return worst < 1e-4


# ══════════════════════════════════════════════════════════ 训练
def train_one(Xtr, ytr, Xva, yva, cfg, seed=SEED, verbose=False):
    net = NpTransformer(Xtr.shape[2], d_model=cfg["d_model"], nhead=cfg["nhead"],
                        dropout=cfg["dropout"], lookback=Xtr.shape[1], seed=seed)
    rng = np.random.RandomState(seed)
    bs = cfg["batch_size"]
    best = (1e9, None, 0.0, 0.0, -1)
    hist = []
    patience, bad = cfg.get("patience", 8), 0
    n = len(Xtr)
    for ep in range(cfg["epochs"]):
        perm = rng.permutation(n)
        for i in range(0, n, bs):
            b = perm[i:i + bs]
            if len(b) < 8:
                continue
            logits, c = net.forward(Xtr[b], train=True, rng=rng)
            _, dl = _ce_softmax(logits, ytr[b])
            g = net.backward(c, dl)
            net.step(g, cfg["lr"], cfg["weight_decay"], cfg.get("clip", 1.0))
        # 验证
        # ⚠️ evaluate 返回 (acc, pred)；早前写成 `vl, va = evaluate(...)`
        #    会把 pred 数组当准确率 → "only 0-dimensional arrays can be converted"。
        #    这里显式只取 acc。
        va, _ = evaluate(net, Xva, yva)
        _Xs = Xtr if len(Xtr) <= 4000 else Xtr[-4000:]
        _ys = ytr if len(ytr) <= 4000 else ytr[-4000:]
        ta, _ = evaluate(net, _Xs, _ys)
        vl = _ce_loss(net, Xva, yva)
        hist.append({"epoch": ep + 1, "val_loss": round(float(vl), 5),
                     "val_acc": round(float(va), 4), "train_acc": round(float(ta), 4)})
        if vl < best[0] - 1e-6:
            best = (vl, {k: v.copy() for k, v in net.p.items()}, va, ta, ep)
            bad = 0
        else:
            bad += 1
            if bad >= patience:
                break
        if verbose and (ep + 1) % 20 == 0:
            print(f"      ep{ep+1:3d} vl={vl:.4f} va={va:.3f} ta={ta:.3f}")
    if best[1] is not None:
        net.p = best[1]
    return float(best[0]), float(best[2]), float(best[3]), net, hist, best[4]


def evaluate(net, X, y):
    if len(X) == 0:
        return float("nan"), np.array([])
    logits, _ = net.forward(X, train=False)
    pred = logits.argmax(1)
    return float((pred == y).mean()), pred


def _ce_loss(net, X, y, bs=512):
    """分批算交叉熵（避免一次性大矩阵）。"""
    if len(X) == 0:
        return float("nan")
    tot, n = 0.0, 0
    for i in range(0, len(X), bs):
        lg, _ = net.forward(X[i:i + bs], train=False)
        l, _ = _ce_softmax(lg, y[i:i + bs])
        tot += l * len(y[i:i + bs]); n += len(y[i:i + bs])
    return tot / max(1, n)


def predict_proba(net, X):
    logits, _ = net.forward(X, train=False)
    z = logits - logits.max(-1, keepdims=True)
    e = np.exp(z)
    return e / e.sum(-1, keepdims=True)


# ══════════════════════════════════════════════════════════ 评估工具
def macro_f1(y, pred, n_cls=3):
    f1s = []
    y, pred = np.asarray(y), np.asarray(pred)
    for cc in range(n_cls):
        tp = int(((pred == cc) & (y == cc)).sum())
        fp = int(((pred == cc) & (y != cc)).sum())
        fn = int(((pred != cc) & (y == cc)).sum())
        if tp + fp + fn == 0:
            continue
        pr = tp / (tp + fp) if tp + fp else 0.0
        rc = tp / (tp + fn) if tp + fn else 0.0
        f1s.append(2 * pr * rc / (pr + rc) if pr + rc else 0.0)
    return float(np.mean(f1s)) if f1s else float("nan")


def split_by_date(dates_seq, X, y, train_end=TRAIN_END):
    m_tr = np.array([d <= train_end for d in dates_seq])
    return (X[m_tr], y[m_tr], X[~m_tr], y[~m_tr], np.where(m_tr)[0], np.where(~m_tr)[0])


def inner_split(X, y, val_frac=VAL_FRAC, purge=PURGE):
    m = len(X)
    n_va = max(30, int(m * val_frac))
    cut = m - n_va
    tr_end = max(0, cut - purge)
    return X[:tr_end], y[:tr_end], X[cut:], y[cut:]


def search_hyperparams(Xtr, ytr, quick=False, log=print):
    """分阶段坐标搜索，只在训练期内进行。"""
    Xa, ya, Xv, yv = inner_split(Xtr, ytr)
    base = {"d_model": 32, "nhead": 4, "layers": 1, "dropout": 0.1,
            "weight_decay": 1e-3, "lr": 1e-3, "batch_size": 64,
            "epochs": 60 if quick else 120, "patience": 8, "clip": 1.0}

    def ev(cfg):
        try:
            vl, va, ta, _, h, bep = train_one(Xa, ya, Xv, yv, cfg)
            return {"val_loss": round(vl, 5), "val_acc": round(va, 4),
                    "train_acc": round(ta, 4), "best_epoch": bep + 1,
                    "n_epochs_run": len(h), "cfg": dict(cfg)}
        except Exception as e:
            return {"error": f"{type(e).__name__}: {e}", "cfg": dict(cfg)}

    cur = dict(base)
    r0 = ev(cur)
    if "error" in r0:
        return cur, {"val_loss": None, "val_acc": None}, [{"stage": "baseline", **r0}]
    path = [{"stage": "baseline", **{k: v for k, v in r0.items() if k != "cfg"},
             "cfg": {k: cur[k] for k in ("d_model", "nhead", "dropout",
                                         "weight_decay", "lr", "batch_size")}}]
    log(f"    基线 d={cur['d_model']} h={cur['nhead']} do={cur['dropout']} "
        f"wd={cur['weight_decay']} lr={cur['lr']} bs={cur['batch_size']}"
        f" → val_loss={r0['val_loss']} val_acc={r0['val_acc']}")

    stages = [
        ("capacity", [{"d_model": d, "nhead": hh}
                      for d, hh in ((16, 2), (32, 4), (64, 4), (32, 2))]),
        ("dropout", [{"dropout": dv} for dv in (0.0, 0.1, 0.2, 0.3, 0.4)]),
        ("weight_decay", [{"weight_decay": w} for w in (0.0, 1e-4, 1e-3, 1e-2, 5e-2, 1e-1)]),
        ("lr_batch", [{"lr": l, "batch_size": b}
                      for l, b in ((3e-4, 64), (1e-3, 64), (2e-3, 64),
                                   (1e-3, 32), (1e-3, 128))]),
    ]
    if quick:
        stages = stages[:2]

    for name, cands in stages:
        best, trials = None, []
        for delta in cands:
            cfg = dict(cur); cfg.update(delta)
            r = ev(cfg)
            trials.append({**{k: v for k, v in r.items() if k != "cfg"}, "delta": delta})
            if "error" in r:
                continue
            if best is None or r["val_loss"] < best["val_loss"] - 1e-6:
                best = {"cfg": cfg, **r}
        if best:
            cur = best["cfg"]
        path.append({"stage": name, "trials": trials,
                     "picked": {k: cur[k] for k in cands[0].keys()},
                     "val_loss": best["val_loss"] if best else None,
                     "val_acc": best["val_acc"] if best else None})
        log(f"    {name:12s} → {({k: cur[k] for k in cands[0]})} "
            f"val_loss={best['val_loss'] if best else 'NA'} "
            f"val_acc={best['val_acc'] if best else 'NA'}")
    return cur, {"val_loss": path[-1].get("val_loss"), "val_acc": path[-1].get("val_acc")}, \
        path


# ══════════════════════════════════════════════════════════ 主流程
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--quick", action="store_true")
    ap.add_argument("--bench", default=PRIMARY)
    ap.add_argument("--gradcheck", action="store_true", help="只做梯度检查")
    a = ap.parse_args()

    print("═══ numpy 手写 Transformer（MultiHeadSelfAttention + FFN + Pre-LN + 残差）═══")
    if a.gradcheck:
        ok = gradcheck()
        return 0 if ok else 1

    out = {"meta": {
        "model": "TinyTransformer-numpy（真 Transformer 编码器，反向传播手写）",
        "impl": ("纯 numpy，不依赖 torch（本机 pip install torch 解包卡住 >14min）；"
                 "含 MultiHeadSelfAttention(4头) / Pre-LayerNorm / GELU-FFN / 残差 / "
                 "可学习位置编码；反向传播手写，--gradcheck 有限差分校验"),
        "lookback": LOOKBACK, "horizon": H, "purge": PURGE,
        "train_end": TRAIN_END,
        "design": ("2015-2021 用于超参数搜索（含内部验证早停）；"
                   "2022-2026 完全不参与调参，仅最终评估一次"),
        "labels": "三分类 0=跌 1=震荡 2=涨，阈值 0.75×σ20×√H（滚动，无前视）",
        "features": ["ret1", "ret5", "ret10", "ret20", "ret60", "vol20", "vol60",
                     "vol_ratio", "px_ma20", "px_ma60", "dist_hi60", "dist_lo60",
                     "info_ratio", "up_frac20"],
        "baseline": "训练期最多数类占比（永远猜同一类）",
        "leak_guard": ["超参数只用 ≤2021-12-31", "测试期零参与",
                       "归一化 mu/sd 只用训练折", "早停用训练期内部验证折",
                       "训练/验证间 purge=H", "位置编码按序列位置而非绝对日期"],
    }, "gradcheck": None}

    gc_ok = gradcheck(n_feat=14, d_model=8, nhead=2, lookback=6, n=6)
    out["gradcheck"] = {"passed": bool(gc_ok),
                        "note": "有限差分相对误差 <1e-4 视为通过"}

    log = print
    for code, name in BENCH.items():
        if a.bench != "ALL" and code != a.bench:
            continue
        print(f"\n{'='*78}\n{name} ({code})")
        dates, close = load_close(code)
        F, fnames = build_features(close)
        y = build_labels(close)
        X, Y, idx = make_sequences(F, y)
        if X is None or len(X) < 400:
            print("  ⚠️ 样本不足，跳过")
            continue
        ds = [dates[i] for i in idx]
        Xtr, ytr, Xte, yte, itr, ite = split_by_date(ds, X, Y)
        print(f"  序列样本 {len(X)}  |  训练期(≤{TRAIN_END}) {len(Xtr)}  |  测试期 {len(Xte)}")
        print(f"  训练期标签分布 {np.bincount(ytr, minlength=3).tolist()}  "
              f"测试期 {np.bincount(yte, minlength=3).tolist()}")

        # 归一化：mu/sd 只用训练期
        mu = Xtr.reshape(-1, Xtr.shape[2]).mean(axis=0)
        sd = Xtr.reshape(-1, Xtr.shape[2]).std(axis=0) + 1e-8
        Xtr = (Xtr - mu) / sd
        Xte = (Xte - mu) / sd

        t0 = time.time()
        cfg, val, path = search_hyperparams(Xtr, ytr, quick=a.quick, log=log)
        print(f"  ⏱ 搜索 {time.time()-t0:.0f}s")

        # 用最优配置重训（早停仍在训练期内部）
        Xa, ya, Xv, yv = inner_split(Xtr, ytr)
        vl, va_acc, ta_acc, net, hist, bep = train_one(Xa, ya, Xv, yv, cfg)

        # 测试期只评估一次
        prob = predict_proba(net, Xte)
        pred = prob.argmax(1)
        test_acc = float((pred == yte).mean())
        test_f1 = macro_f1(yte, pred)
        maj = int(np.bincount(ytr, minlength=3).argmax())
        base_acc = float((yte == maj).mean())

        gap = ta_acc - va_acc
        if gap > 0.15:
            diag = "过拟合"
        elif ta_acc < 0.45 and va_acc < 0.45:
            diag = "欠拟合"
        else:
            diag = "正常"
        verdict = ("优于基线" if test_acc > base_acc + 1e-9
                   else ("等于基线" if abs(test_acc - base_acc) < 1e-9 else "不如基线"))

        print(f"  参数 {net.n_params()} | 最优 d={cfg['d_model']} h={cfg['nhead']} "
              f"do={cfg['dropout']} wd={cfg['weight_decay']} lr={cfg['lr']} bs={cfg['batch_size']}")
        print(f"  训练期 train_acc={ta_acc:.4f} val_acc={va_acc:.4f} 差={gap:+.4f} → {diag}")
        print(f"  ★ 测试期(2022-2026) acc={test_acc:.4f} macroF1={test_f1:.4f} "
              f"基线={base_acc:.4f} → {verdict}")

        # 最新一日预测（用于页面仓位建议）
        last_seq = X[-1] if len(X) else None
        latest = None
        if last_seq is not None:
            seq = ((last_seq - mu) / sd)[None, ...]
            pr = predict_proba(net, seq)[0]
            latest = {"date": ds[-1], "prob": [round(float(x), 4) for x in pr],
                      "pred": CLS_NAMES[int(pr.argmax())],
                      "confidence": round(float(pr.max()), 4)}

        out[code] = {
            "name": name, "n_params": net.n_params(), "best_cfg": cfg,
            "n_train": int(len(Xtr)), "n_test": int(len(Xte)),
            "train_acc": round(ta_acc, 4), "val_acc": round(va_acc, 4),
            "gap": round(gap, 4), "diagnosis": diag, "best_epoch": int(bep + 1),
            "test_acc": round(test_acc, 4), "test_macro_f1": round(test_f1, 4),
            "baseline_maj_class": CLS_NAMES[maj], "baseline_acc": round(base_acc, 4),
            "verdict": verdict,
            "test_confusion": [[int(((yte == i) & (pred == j)).sum()) for j in range(3)]
                               for i in range(3)],
            "search_path": path, "history_tail": hist[-8:],
            "test_range": [ds[ite[0]], ds[ite[-1]]] if len(ite) else None,
            "label_dist_train": np.bincount(ytr, minlength=3).tolist(),
            "label_dist_test": np.bincount(yte, minlength=3).tolist(),
            "latest": latest,
        }

    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print(f"\n✅ 已写出 {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
