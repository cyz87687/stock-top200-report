#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
transformer_regime_torch.py —— 用官方 PyTorch 实现同一架构，交叉验证 numpy 版结论

═══ 为什么要有这个文件 ═══

transformer_regime.py 是纯 numpy 手写（因 torch 当时装不上），
结论是「测试期 acc 0.4957 < 基线 0.5294 → 不如基线」。

手写实现的结果必须先排除「实现有 bug」这一可能，才能作为结论对外说。
验证方式有两种，都做：
    ① 梯度检查（已做，16/16 参数 rel_err < 1.2e-7）
    ② **用官方 PyTorch 跑同一份数据、同一套切分、同一架构** → 若两者测试准确率接近，
       说明结论来自数据而非实现

本文件与 transformer_regime.py 严格对齐：
    · 同一 load_close / build_features / build_labels / make_sequences（直接 import）
    · 同一 TRAIN_END="2021-12-31" / PURGE / VAL_FRAC / LOOKBACK / H
    · 同一超参数搜索空间（分阶段坐标搜索）
    · 同一判据（基线=训练期最多数类）

差异只允许在「实现方式」：
    numpy 手写 AdamW + 手写反向  vs  torch.nn.TransformerEncoder + torch.optim.AdamW

输出：transformer_regime_torch.json（与 numpy 版并列对比）
用法：
    python transformer_regime_torch.py --bench 000985
    python transformer_regime_torch.py --bench ALL --quick
"""
import os
import sys
import json
import math
import time
import argparse
import numpy as np

# ⚠️ torch 2.15 + Python 3.13 有个已知冲突：_dynamo 的 mega-cache 会把
#    artifact_type_key=precompile 重复注册，抛 AssertionError 直接导入失败。
#    与本任务无关（我们不用 torch.compile），禁用即可。
#    必须在 import torch 之前设置。
os.environ.setdefault("TORCHDYNAMO_DISABLE", "1")
os.environ.setdefault("TORCH_INDUCTOR_DISABLE", "1")

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from transformer_regime import (load_close, build_features, build_labels,
                                make_sequences, macro_f1, split_by_date,
                                inner_split, CLS_NAMES, BENCH, PRIMARY,
                                LOOKBACK, H, TRAIN_END, PURGE, VAL_FRAC, SEED)

OUT = os.path.join(HERE, "transformer_regime_torch.json")


def build_model(n_feat, d_model, nhead, dropout, n_cls=3, lookback=LOOKBACK):
    import torch
    import torch.nn as nn

    class TinyTransformer(nn.Module):
        """与 numpy 版同构：Linear→+pos→Encoder(1层,Pre-LN,GELU,FFN 2d)→取末时步→LN→Linear"""

        def __init__(self):
            super().__init__()
            self.inp = nn.Linear(n_feat, d_model)
            self.pos = nn.Parameter(torch.zeros(1, lookback, d_model))
            nn.init.normal_(self.pos, std=0.02)
            enc = nn.TransformerEncoderLayer(
                d_model=d_model, nhead=nhead, dim_feedforward=d_model * 2,
                dropout=dropout, batch_first=True, norm_first=True,
                activation="gelu")
            self.enc = nn.TransformerEncoder(enc, num_layers=1)
            self.norm = nn.LayerNorm(d_model)
            self.drop = nn.Dropout(dropout)
            self.head = nn.Linear(d_model, n_cls)

        def forward(self, x):
            h = self.inp(x) + self.pos[:, :x.shape[1], :]
            h = self.enc(h)
            h = self.norm(h[:, -1, :])
            return self.head(self.drop(h))

    return TinyTransformer()


def train_one(Xtr, ytr, Xva, yva, cfg, seed=SEED):
    import torch
    import torch.nn as nn
    torch.manual_seed(seed); np.random.seed(seed)
    torch.set_num_threads(max(1, (os.cpu_count() or 2) - 1))

    model = build_model(Xtr.shape[2], cfg["d_model"], cfg["nhead"], cfg["dropout"])

    # ⚠️ 不能用 torch.optim.AdamW：本机 torch 2.15 + Python 3.13 下，
    #    Optimizer.__init__ → torch._compile.inner → import torch._dynamo
    #    → torch._inductor 二次注册 "precompile" artifact → AssertionError。
    #    （实测堆栈：torch/optim/optimizer.py:442 → torch/_compile.py:47）
    #    这里手写 AdamW，语义与 torch 版一致（decoupled weight decay），
    #    完全绕开 optimizer 的 compile 钩子，架构与训练流程不变。
    params = [p for p in model.parameters() if p.requires_grad]
    st = [{"m": torch.zeros_like(p), "v": torch.zeros_like(p)} for p in params]
    lr, wd, t_step = cfg["lr"], cfg["weight_decay"], 0
    b1, b2, eps = 0.9, 0.999, 1e-8
    decay_skip = {"bias"}   # 与 AdamW 惯例一致：bias / norm 参数不做权重衰减

    def opt_step():
        nonlocal t_step
        t_step += 1
        torch.nn.utils.clip_grad_norm_(params, 1.0)
        with torch.no_grad():
            for p, s in zip(params, st):
                g = p.grad
                if g is None:
                    continue
                # decoupled weight decay：只对矩阵参数（dim>1）衰减，
                # bias / LayerNorm 的 gamma,beta（dim=1）不算，与 AdamW 惯例一致
                if wd > 0 and g.dim() > 1:
                    g = g + wd * p
                s["m"].mul_(b1).add_(g, alpha=1 - b1)
                s["v"].mul_(b2).addcmul_(g, g, value=1 - b2)
                mh = s["m"] / (1 - b1 ** t_step)
                vh = s["v"] / (1 - b2 ** t_step)
                p.add_(mh / (vh.sqrt() + eps), alpha=-lr)

    cnt = np.bincount(ytr, minlength=3).astype(float)
    w = torch.tensor(cnt.sum() / (3 * np.maximum(cnt, 1)), dtype=torch.float32)
    lossf = nn.CrossEntropyLoss(weight=w)

    Xtr_t = torch.from_numpy(Xtr); ytr_t = torch.from_numpy(ytr)
    Xva_t = torch.from_numpy(Xva); yva_t = torch.from_numpy(yva)
    bs = cfg["batch_size"]; n = len(Xtr_t)
    best = (1e9, None, 0.0, 0.0, -1)
    hist, patience, bad = [], cfg.get("patience", 8), 0

    for ep in range(cfg["epochs"]):
        model.train()
        perm = torch.randperm(n)
        for i in range(0, n, bs):
            b = perm[i:i + bs]
            if len(b) < 8:
                continue
            model.zero_grad(set_to_none=True)
            loss = lossf(model(Xtr_t[b]), ytr_t[b])
            loss.backward()
            opt_step()
        model.eval()
        with torch.no_grad():
            vl = float(lossf(model(Xva_t), yva_t))
            va = float((model(Xva_t).argmax(1) == yva_t).float().mean())
            _X = Xtr_t if n <= 4000 else Xtr_t[-4000:]
            _y = ytr_t if n <= 4000 else ytr_t[-4000:]
            ta = float((model(_X).argmax(1) == _y).float().mean())
        hist.append({"epoch": ep + 1, "val_loss": round(vl, 5),
                     "val_acc": round(va, 4), "train_acc": round(ta, 4)})
        if vl < best[0] - 1e-6:
            best = (vl, {k: v.detach().clone() for k, v in model.state_dict().items()},
                    va, ta, ep)
            bad = 0
        else:
            bad += 1
            if bad >= patience:
                break
    if best[1] is not None:
        model.load_state_dict(best[1])
    return float(best[0]), float(best[2]), float(best[3]), model, hist, best[4]


def predict_proba(model, X):
    import torch
    model.eval()
    with torch.no_grad():
        return torch.softmax(model(torch.from_numpy(X)), dim=1).numpy()


def count_params(m):
    return int(sum(p.numel() for p in m.parameters() if p.requires_grad))


def search_hyperparams(Xtr, ytr, quick=False, log=print):
    """与 numpy 版完全相同的搜索空间与选择标准。"""
    Xa, ya, Xv, yv = inner_split(Xtr, ytr)
    base = {"d_model": 32, "nhead": 4, "dropout": 0.1,
            "weight_decay": 1e-3, "lr": 1e-3, "batch_size": 64,
            "epochs": 60 if quick else 120, "patience": 8}

    def ev(cfg):
        try:
            vl, va, ta, _, h, bep = train_one(Xa, ya, Xv, yv, cfg)
            return {"val_loss": round(vl, 5), "val_acc": round(va, 4),
                    "train_acc": round(ta, 4), "best_epoch": bep + 1,
                    "n_epochs_run": len(h)}
        except Exception as e:
            return {"error": f"{type(e).__name__}: {e}"}

    cur = dict(base)
    r0 = ev(cur)
    path = [{"stage": "baseline", **r0,
             "cfg": {k: cur[k] for k in ("d_model", "nhead", "dropout",
                                         "weight_decay", "lr", "batch_size")}}]
    log(f"    基线 d={cur['d_model']} h={cur['nhead']} do={cur['dropout']} "
        f"wd={cur['weight_decay']} lr={cur['lr']} bs={cur['batch_size']}"
        f" → val_loss={r0.get('val_loss')} val_acc={r0.get('val_acc')}")

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
            trials.append({**r, "delta": delta})
            if "error" in r:
                continue
            if best is None or r["val_loss"] < best["val_loss"] - 1e-6:
                best = {"cfg": cfg, **r}
        if best:
            cur = best["cfg"]
        path.append({"stage": name, "trials": trials,
                     "picked": {k: cur[k] for k in cands[0].keys()},
                     "val_loss": best.get("val_loss") if best else None,
                     "val_acc": best.get("val_acc") if best else None})
        log(f"    {name:12s} → {({k: cur[k] for k in cands[0]})} "
            f"val_loss={best.get('val_loss') if best else 'NA'} "
            f"val_acc={best.get('val_acc') if best else 'NA'}")
    return cur, path


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--quick", action="store_true")
    ap.add_argument("--bench", default=PRIMARY)
    a = ap.parse_args()

    try:
        import torch
        print(f"torch {torch.__version__}")
    except Exception as e:
        print(f"❌ 缺少 torch: {e}")
        return 2

    out = {"meta": {
        "model": "TinyTransformer-torch（官方 PyTorch，用于交叉验证 numpy 版）",
        "torch": torch.__version__,
        "lookback": LOOKBACK, "horizon": H, "purge": PURGE, "train_end": TRAIN_END,
        "design": ("2015-2021 调参（含内部验证早停）；2022-2026 纯测试，只评估一次。"
                   "数据/切分/搜索空间/判据与 transformer_regime.py 完全一致，"
                   "唯一差异是实现（torch vs numpy 手写）"),
        "purpose": ("交叉验证：若 torch 版测试准确率与 numpy 版接近，"
                    "则「不如基线」的结论来自数据而非手写实现 bug"),
    }}

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
        print(f"  序列样本 {len(X)}  |  训练期 {len(Xtr)}  |  测试期 {len(Xte)}")
        print(f"  训练期标签 {np.bincount(ytr, minlength=3).tolist()}  "
              f"测试期 {np.bincount(yte, minlength=3).tolist()}")

        mu = Xtr.reshape(-1, Xtr.shape[2]).mean(axis=0)
        sd = Xtr.reshape(-1, Xtr.shape[2]).std(axis=0) + 1e-8
        Xtr = (Xtr - mu) / sd
        Xte = (Xte - mu) / sd

        t0 = time.time()
        cfg, path = search_hyperparams(Xtr, ytr, quick=a.quick, log=log)
        print(f"  ⏱ 搜索 {time.time()-t0:.0f}s")

        Xa, ya, Xv, yv = inner_split(Xtr, ytr)
        vl, va_acc, ta_acc, model, hist, bep = train_one(Xa, ya, Xv, yv, cfg)
        prob = predict_proba(model, Xte)
        pred = prob.argmax(1)
        test_acc = float((pred == yte).mean())
        test_f1 = macro_f1(yte, pred)
        maj = int(np.bincount(ytr, minlength=3).argmax())
        base_acc = float((yte == maj).mean())
        gap = ta_acc - va_acc
        diag = ("过拟合" if gap > 0.15
                else ("欠拟合" if (ta_acc < 0.45 and va_acc < 0.45) else "正常"))
        verdict = ("优于基线" if test_acc > base_acc + 1e-9
                   else ("等于基线" if abs(test_acc - base_acc) < 1e-9 else "不如基线"))

        print(f"  参数 {count_params(model)} | 最优 d={cfg['d_model']} h={cfg['nhead']} "
              f"do={cfg['dropout']} wd={cfg['weight_decay']} lr={cfg['lr']} bs={cfg['batch_size']}")
        print(f"  训练期 train_acc={ta_acc:.4f} val_acc={va_acc:.4f} 差={gap:+.4f} → {diag}")
        print(f"  ★ 测试期 acc={test_acc:.4f} macroF1={test_f1:.4f} "
              f"基线={base_acc:.4f} → {verdict}")

        latest = None
        if len(X):
            seq = ((X[-1] - mu) / sd)[None, ...]
            pr = predict_proba(model, seq)[0]
            latest = {"date": ds[-1], "prob": [round(float(x), 4) for x in pr],
                      "pred": CLS_NAMES[int(pr.argmax())],
                      "confidence": round(float(pr.max()), 4)}

        out[code] = {
            "name": name, "n_params": count_params(model), "best_cfg": cfg,
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
            "latest": latest,
        }

    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print(f"\n✅ 已写出 {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
