#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
audit_round111c_t1_executable.py —— 检验「次日开盘买 → 再次日开盘卖」这个**真可执行**口径

═══ 为什么还要这一步 ═══

audit_round111b 把 T+1 收益拆成隔夜/日内两段，结论是：
    13 个因子里 12 个的信号全部落在**隔夜段**（amp 隔夜 t=−8.95、turn_z −12.19、
    vol_ratio −12.67），日内段**无一显著**（|t| 全部 <1.7）。
    且 8 个因子出现「隔夜负、日内正」的符号翻转。

这意味着：用 t 日收盘算出的信号，预测的是**t→t+1 的隔夜跳空**，
而散户在 t 日收盘后已经无法参与那段行情。

═══ 但还有一个可能的救法 ═══

「次日开盘买 → 再次日开盘卖」（持有 1 个完整交易日）能**完整拿到一次隔夜**。
它的收益 = open[t+2]/open[t+1] − 1，包含：
    · t+1 日内（close[t+1]/open[t+1]）
    · t+1→t+2 隔夜（open[t+2]/close[t+1]）
如果信号主要来自「隔夜漂移的持续性」，那么这个口径应该有效。

本脚本对比 5 个可执行口径：
    E1  open[t+1] → close[t+1]      （当日回转，T0 代理）
    E2  open[t+1] → open[t+2]        （持 1 日，抓到一次隔夜）
    E3  close[t+1] → close[t+2]      （T+1 收盘买，隔日收盘卖）
    E4  close[t]   → close[t+1]      （含隔夜，不可执行，作基准对照）
    E5  close[t]   → close[t+2]      （2 日，含一次隔夜）

对每个口径算 IC 与「顶10%−底10%」多空收益，并**扣 0.2% 双边成本**给净收益。

═══ 判据 ═══
可执行口径（E1/E2/E3）必须同时满足：
    |IC 均值| ≥ 0.02、|t| ≥ 2.0、IC 胜率 ≥55% 或 ≤45%、扣费后多空仍然 >0
只有一个口径过 → 只能用于该口径，不得外推。

输出：audit_round111c_t1_executable.json
"""
import os
import sys
import json
import math
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from kline_units import vol_coef           # noqa: E402  成交量双口径唯一来源

HERE = os.path.dirname(os.path.abspath(__file__))
KL = os.path.join(HERE, "data", "klines_long.json")
OUT = os.path.join(HERE, "audit_round111c_t1_executable.json")

COST = 0.002          # 双边成本 0.2%（佣金+印花税+滑点，A股散户偏保守估计）
MIN_AMT = 3e7


def rank_avg(a):
    n = len(a)
    o = np.argsort(a, kind="mergesort")
    r = np.empty(n, float)
    r[o] = np.arange(1, n + 1, dtype=float)
    sa = a[o]
    i = 0
    while i < n:
        j = i
        while j + 1 < n and sa[j + 1] == sa[i]:
            j += 1
        if j > i:
            r[o[i:j + 1]] = (i + j) / 2.0 + 1
        i = j + 1
    return r


def spearman_col(col, y):
    m = np.isfinite(col) & np.isfinite(y)
    if m.sum() < 50:
        return np.nan
    rx, ry = rank_avg(col[m]), rank_avg(y[m])
    rx -= rx.mean(); ry -= ry.mean()
    sx, sy = math.sqrt((rx ** 2).sum()), math.sqrt((ry ** 2).sum())
    return float((rx * ry).sum() / (sx * sy)) if sx > 0 and sy > 0 else np.nan


def rsi_wilder(close, win=14):
    n = len(close)
    out = np.full(n, np.nan)
    if n <= win:
        return out
    d = np.diff(close)
    up = np.where(d > 0, d, 0.0); dn = np.where(d < 0, -d, 0.0)
    au, ad = up[:win].mean(), dn[:win].mean()
    for i in range(win, n):
        if i > win:
            au = (au * (win - 1) + up[i - 1]) / win
            ad = (ad * (win - 1) + dn[i - 1]) / win
        out[i] = 100.0 if ad == 0 else 100.0 - 100.0 / (1.0 + au / ad)
    return out


def roll_mean_1d(a, w):
    n = len(a)
    o = np.full(n, np.nan)
    for i in range(w - 1, n):
        seg = a[i - w + 1:i + 1]
        if np.isfinite(seg).sum() >= w * 0.8:
            o[i] = np.nanmean(seg)
    return o


FEATS = ["amp", "vol_ratio", "turn_z", "rsi14", "vol20", "dist_ma20",
         "mom1", "rev1", "close_pos", "gap", "mom5"]


def main():
    print(f"读取 {KL} ...")
    kl = json.load(open(KL, encoding="utf-8"))
    codes = sorted(kl.keys())
    alld = sorted({d for c in codes for d in kl[c]})
    di = {d: i for i, d in enumerate(alld)}
    T, N = len(alld), len(codes)

    P = {k: np.full((T, N), np.nan) for k in ("c", "o", "h", "l", "vol", "amt")}
    for j, cd in enumerate(codes):
        for dt, v in kl[cd].items():
            i = di.get(dt)
            if i is None or not isinstance(v, (list, tuple)) or len(v) < 5:
                continue
            try:
                o_, c_, h_, l_, v_ = (float(v[0]), float(v[1]), float(v[2]),
                                      float(v[3]), float(v[4]))
            except (TypeError, ValueError):
                continue
            if not (c_ > 0 and o_ > 0):
                continue
            P["o"][i, j] = o_; P["c"][i, j] = c_; P["h"][i, j] = h_
            P["l"][i, j] = l_; P["vol"][i, j] = v_; P["amt"][i, j] = v_ * vol_coef(cd) * c_
    del kl
    C, O, H, L = P["c"], P["o"], P["h"], P["l"]
    print(f"  {N} 只 × {T} 日，有效 {np.isfinite(C).sum():,}")

    # 特征面板
    F = {k: np.full((T, N), np.nan, dtype=np.float32) for k in FEATS}
    PC = np.vstack([np.full((1, N), np.nan), C[:-1]])       # prev close (2D!)
    for j in range(N):
        c = C[:, j]
        if np.isfinite(c).sum() < 80:
            continue
        prev = PC[:, j]
        with np.errstate(invalid="ignore", divide="ignore"):
            F["mom1"][:, j] = c / prev - 1.0
            F["mom5"][:, j] = c / np.concatenate([np.full(5, np.nan), c[:-5]]) - 1.0
            F["amp"][:, j] = (H[:, j] - L[:, j]) / prev
            rng = H[:, j] - L[:, j]
            F["close_pos"][:, j] = np.where(rng > 0, (c - L[:, j]) / rng, 0.5)
            F["gap"][:, j] = O[:, j] / prev - 1.0
            F["dist_ma20"][:, j] = c / roll_mean_1d(c, 20) - 1.0
        F["rev1"][:, j] = -F["mom1"][:, j]
        F["rsi14"][:, j] = rsi_wilder(c, 14)
        amt = P["amt"][:, j]
        with np.errstate(invalid="ignore", divide="ignore"):
            F["vol_ratio"][:, j] = amt / roll_mean_1d(amt, 20)
        lr = np.full(T, np.nan)
        with np.errstate(invalid="ignore", divide="ignore"):
            lr[1:] = np.log(c[1:] / c[:-1])
        v20 = np.full(T, np.nan)
        for i in range(20, T):
            seg = lr[i - 19:i + 1]
            if np.isfinite(seg).all():
                v20[i] = seg.std(ddof=1)
        F["vol20"][:, j] = v20
        lamt = np.log(np.where(amt > 0, amt, np.nan))
        mu, sd = roll_mean_1d(lamt, 60), np.full(T, np.nan)
        for i in range(59, T):
            seg = lamt[i - 59:i + 1]
            if np.isfinite(seg).all():
                sd[i] = seg.std(ddof=1)
        with np.errstate(invalid="ignore", divide="ignore"):
            F["turn_z"][:, j] = (lamt - mu) / np.where(sd > 0, sd, np.nan)

    # 5 个口径的收益
    R = {}
    with np.errstate(invalid="ignore", divide="ignore"):
        e1 = np.full((T, N), np.nan); e1[:-1] = C[1:] / O[1:] - 1.0
        e2 = np.full((T, N), np.nan); e2[:-2] = O[2:] / O[1:-1] - 1.0
        e3 = np.full((T, N), np.nan); e3[:-2] = C[2:] / C[1:-1] - 1.0
        e4 = np.full((T, N), np.nan); e4[:-1] = C[1:] / C[:-1] - 1.0
        e5 = np.full((T, N), np.nan); e5[:-2] = C[2:] / C[:-2] - 1.0
    R["E1_open_to_close"] = (e1, "open[t+1]→close[t+1]", True, "当日回转（T0 代理）")
    R["E2_open_to_open"] = (e2, "open[t+1]→open[t+2]", True, "持 1 日，抓一次隔夜")
    R["E3_close_next_to_close"] = (e3, "close[t+1]→close[t+2]", True, "T+1 收盘买")
    R["E4_close_to_close"] = (e4, "close[t]→close[t+1]", False, "含隔夜，不可执行（对照）")
    R["E5_close_to_close_2d"] = (e5, "close[t]→close[t+2]", False, "2 日（对照）")

    out = {}
    for tag, (mat, label, executable, desc) in R.items():
        ics = {k: [] for k in FEATS}
        lss = {k: [] for k in FEATS}
        nd = 0
        for t in range(25, T - 2):
            ok = (np.isfinite(C[t]) & np.isfinite(mat[t])
                  & np.isfinite(P["amt"][t]) & (P["amt"][t] >= MIN_AMT))
            if ok.sum() < 100:
                continue
            nd += 1
            y = mat[t][ok]
            Xf = np.column_stack([F[k][t][ok] for k in FEATS]).astype(float)
            for ci, k in enumerate(FEATS):
                col = Xf[:, ci]
                m = np.isfinite(col)
                if m.sum() >= 30:
                    lo, hi = np.nanpercentile(col[m], [1, 99])
                    col = np.clip(col, lo, hi)
                ics[k].append(spearman_col(col, y))
                mm = np.isfinite(col)
                if mm.sum() >= 100:
                    lo_t, hi_t = np.nanpercentile(col[mm], [10, 90])
                    up = (col >= hi_t) & mm; dn = (col <= lo_t) & mm
                    if up.sum() >= 10 and dn.sum() >= 10:
                        lss[k].append(float(y[up].mean() - y[dn].mean()))
        res = {}
        for k in FEATS:
            v = np.asarray([x for x in ics[k] if np.isfinite(x)], float)
            if len(v) < 10:
                continue
            mu, sd = v.mean(), v.std(ddof=1)
            tt = mu / (sd / math.sqrt(len(v)))
            ls = np.asarray([x for x in lss[k] if np.isfinite(x)], float)
            lsmu = float(ls.mean()) if len(ls) else float("nan")
            net = lsmu - COST
            gates = [abs(mu) >= 0.02, abs(tt) >= 2.0,
                     (v > 0).mean() >= 0.55 or (v > 0).mean() <= 0.45,
                     net > 0]
            res[k] = {"ic": round(float(mu), 5), "t": round(float(tt), 3),
                      "pos_rate": round(float((v > 0).mean()), 4),
                      "ls_gross": round(lsmu * 100, 3),
                      "ls_net": round(net * 100, 3),
                      "n_gate": int(sum(gates)),
                      "pass": bool(all(gates))}
        out[tag] = {"label": label, "executable": executable, "desc": desc,
                    "n_days": nd, "factors": res}

    # 打印：每个口径下最好的因子
    print()
    for tag, (mat, label, executable, desc) in R.items():
        o = out[tag]
        rows = sorted(o["factors"].items(), key=lambda kv: -abs(kv[1]["ic"]))
        print(f"── {tag}  {label}  {'【可执行】' if executable else '【不可执行·对照】'}  "
              f"{o['n_days']} 个截面")
        print(f"   {'因子':<10}{'IC':>9}{'t':>7}{'胜率':>7}{'多空毛%':>9}{'扣费净%':>9}{'门禁':>5}{'':>4}")
        for k, v in rows[:5]:
            print(f"   {k:<10}{v['ic']:>+9.4f}{v['t']:>7.2f}{v['pos_rate']*100:>6.1f}%"
                  f"{v['ls_gross']:>+9.2f}{v['ls_net']:>+9.2f}{v['n_gate']:>5d}"
                  f"{'   ✅' if v['pass'] else ''}")
        allpass = [k for k, v in o["factors"].items() if v["pass"]]
        print(f"   → 四门全过：{allpass if allpass else '无'}\n")

    winners = {tag: [k for k, v in out[tag]["factors"].items() if v["pass"]]
               for tag in R if R[tag][2]}
    json.dump({
        "meta": {
            "purpose": "找出 T+1 真正可执行的口径（用 5 个口径对照，含成本）",
            "cost_per_roundtrip": COST,
            "min_amt": MIN_AMT,
            "gates": "|IC|>=0.02 且 |t|>=2.0 且 IC胜率>=55%或<=45% 且 扣费后多空>0",
            "verdict": ("可执行口径下的 winners 为空 → 说明书里所有「次日方向预测」"
                        "能力在扣费后可执行层面均不成立"),
        },
        "by_vehicle": out,
        "executable_winners": winners,
    }, open(OUT, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"✅ 已写出 {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
