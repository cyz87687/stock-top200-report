#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""audit_round70b_seg_diag.py —— 分段单维 IC 诊断

round70 发现段B（2026-03~06）组合 ICIR = −0.655，即该期模型**系统性反向**。
本脚本逐段拆单维，定位是「全部维度同向翻转」还是「个别维度拖累」。

同时输出各维在「段内 IC 符号一致性」（pos_rate），判断是 regime 切换还是噪声。
"""
import os
import sys
import json
import math
import numpy as np
from scipy.stats import rankdata

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from audit_round70_icir_fast import DIMS, nw_t, stat, WEIGHTS, _f  # noqa

NPZ = os.path.join(HERE, "data", "icir_arrays_h20.npz")
OUT = os.path.join(HERE, "audit_round70b.json")


def main():
    z = np.load(NPZ, allow_pickle=False)
    dates = [str(x) for x in z["dates"]]
    X, M, Y = z["X"], z["M"], z["Y"]
    RY = rankdata(Y, axis=1, nan_policy="omit")
    n = len(dates)

    segs = {"A": (0, int(n * .40)), "B": (int(n * .40), int(n * .70)),
            "C": (int(n * .70), n), "ALL": (0, n)}

    print(f"面板 {n} 截面 {dates[0]} ~ {dates[-1]}\n")
    for k, (a, b) in segs.items():
        print(f"  段{k:3s} {b-a:3d} 截面  {dates[a]} ~ {dates[b-1]}")
    print()

    # ── 逐维逐段 IC ──
    out = {}
    hdr = f"{'维度':14s}" + "".join(f"{'段'+k:>16s}" for k in ("A", "B", "C", "ALL"))
    print(hdr)
    print("-" * len(hdr))
    for j, d in enumerate(DIMS):
        row = {}
        cells = []
        for k, (a, b) in segs.items():
            xs = X[a:b, :, j]
            ys = RY[a:b]
            ics = []
            for t in range(xs.shape[0]):
                x = xs[t]
                y = ys[t]
                ok = (~np.isnan(x)) & (~np.isnan(y))
                if ok.sum() < 100:
                    continue
                xv, yv = x[ok], y[ok]
                xv = xv - xv.mean()
                yv = yv - yv.mean()
                sx = math.sqrt((xv * xv).sum())
                sy = math.sqrt((yv * yv).sum())
                if sx > 0 and sy > 0:
                    ics.append(float((xv * yv).sum() / (sx * sy)))
            st = stat(ics)
            row[k] = st
            cells.append(f"{_f(st['ic'],3)}/{_f(st['t'],1):>5}")
        out[d] = row
        print(f"{d.replace('score_',''):14s}" + "".join(f"{c:>16s}" for c in cells))

    print("\n（格式：IC / NW-t）")

    # ── 段B 内部按 10 截面滑窗，看负 IC 集中在哪 ──
    print("\n段B 内 10 截面滑窗组合 IC（v4.15 原权重）：")
    wc = json.load(open(WEIGHTS, encoding="utf-8"))
    dm = np.array([float(wc["dim_max"].get(k, 10.0)) for k in DIMS])
    w = np.array([float(wc["weights"].get(k, 0.0)) for k in DIMS])
    V = np.where(M, np.minimum(np.where(np.isnan(X), 0, X), dm) / dm * 10.0, 0.0)
    den = M.astype(float) @ w
    num = V @ w
    with np.errstate(invalid="ignore", divide="ignore"):
        S = np.where(den > 1e-12, num / den * 10.0, np.nan)
    RS = rankdata(S, axis=1, nan_policy="omit")
    ics_all = []
    for t in range(n):
        ok = (~np.isnan(RS[t])) & (~np.isnan(RY[t]))
        if ok.sum() < 100:
            ics_all.append(None)
            continue
        xv = RS[t][ok] - RS[t][ok].mean()
        yv = RY[t][ok] - RY[t][ok].mean()
        sx = math.sqrt((xv * xv).sum())
        sy = math.sqrt((yv * yv).sum())
        ics_all.append(float((xv * yv).sum() / (sx * sy)) if sx > 0 and sy > 0 else None)

    a, b = segs["B"]
    win = []
    for s in range(a, b - 9, 5):
        sub = [v for v in ics_all[s:s + 10] if v is not None]
        if len(sub) >= 5:
            win.append({"from": dates[s], "to": dates[s + 9],
                        "ic": sum(sub) / len(sub), "n": len(sub)})
    for wd in win:
        flag = "← 反向" if wd["ic"] < -0.05 else ""
        print(f"  {wd['from']} ~ {wd['to']}  n={wd['n']:2d}  IC={wd['ic']:+.4f} {flag}")

    neg = sum(1 for v in ics_all[a:b] if v is not None and v < 0)
    tot = sum(1 for v in ics_all[a:b] if v is not None)
    print(f"\n段B 逐截面：负 IC {neg}/{tot} = {neg/tot*100:.1f}%")

    # ── 全样本各段正 IC 占比 ──
    print("\n各段正 IC 比例（组合，原权重）：")
    for k, (a2, b2) in segs.items():
        sub = [v for v in ics_all[a2:b2] if v is not None]
        pr = sum(1 for v in sub if v > 0) / len(sub)
        mu = sum(sub) / len(sub)
        print(f"  段{k:3s} n={len(sub):3d} 均值IC={mu:+.4f} 正IC占比={pr*100:5.1f}%")

    json.dump({"segments": {k: [dates[v[0]], dates[v[1]-1]] for k, v in segs.items()},
               "single_dim_by_seg": out,
               "segB_windows": win,
               "segB_neg_rate": neg / tot if tot else None,
               "composite_ic_series": ics_all},
              open(OUT, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"\n→ {OUT}")


if __name__ == "__main__":
    main()
