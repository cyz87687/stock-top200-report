#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
audit_round111b_t1_decompose.py —— 把 T+1 收益拆成「隔夜 + 日内」两段，定位信号到底在哪一段

═══ 为什么要拆 ═══

audit_round111 的结果出现一个刺眼模式：12 个因子里有 6 个在「收盘→收盘」口径显著
（amp t=−3.98、vol_ratio t=−3.54、turn_z t=−3.50），但一切到「t+1 开盘买 → t+1 收盘卖」
这个**唯一可执行**的口径，t 值全部塌到 1.6 以下，IC 衰减 76%~87%，
且 turn_z / rsi14 / dist_ma20 / vol_ratio 的**符号直接翻转**（负→正）。

符号翻转不是噪声，它说明：
   「收盘→收盘」这段收益里，**隔夜跳空的方向**与**日内的方向**是相反的。
   而散户只能参与日内那一段（因为信号是收盘后才算出来的）。

所以必须把 r_{t→t+1} 拆成：
    gap   = open[t+1] / close[t] − 1      （隔夜，收盘后无法交易）
    intra = close[t+1] / open[t+1] − 1    （日内，开盘可买入）

分别算每个因子对 gap 和 intra 的 IC。这一步决定 T+1 建议能不能成立：
   · 若 |IC_gap| 显著 > |IC_intra|  → 信号在隔夜段 = **不可执行**，不得写进 T+1 建议
   · 若 |IC_intra| 显著且方向稳定     → 可以做「次日开盘买入、收盘卖出」
   · 若两者都显著但反向              → 可以做「次日开盘反向操作」

═══ 额外做一件事：T0 的「当日尾盘」可行性 ═══

老大要「优化 T0」。T0（当日回转）在 A 股只能用「已有底仓的 T+0」，本质是
在同一交易日内低买高卖。可用信号只有**日内**（分钟级），而本项目的 K 线库
只有日线 → **T0 无法用现有数据回测**。这一点必须在报告里说清，
不能拿日线数据假装验证了 T0。

但有一件日线能验证的事：**尾盘（14:50~15:00）买入 vs 收盘买入**是否有差异？
日线数据只有 close，无法区分。所以 T0 的结论只能是「数据不足，需接入分钟线」，
并给出接入方案。这是诚实的做法。

输出：audit_round111b_t1_decompose.json
"""
import os
import sys
import json
import math
import argparse
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from kline_units import vol_coef           # noqa: E402  成交量双口径唯一来源

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

KL = os.path.join(HERE, "data", "klines_long.json")
OUT = os.path.join(HERE, "audit_round111b_t1_decompose.json")

FEATS = ["mom1", "mom5", "rev1", "rsi14", "dist_ma5", "dist_ma20",
         "vol_ratio", "vol20", "amp", "close_pos", "turn_z", "gap", "amt_z"]


def rank_avg(a):
    """平均秩（处理并列）。"""
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
    """单列 Spearman，含并列处理与 NaN 中位数填充。"""
    m = np.isfinite(col) & np.isfinite(y)
    if m.sum() < 50:
        return np.nan
    c = col[m].copy(); yy = y[m].copy()
    rx, ry = rank_avg(c), rank_avg(yy)
    rx -= rx.mean(); ry -= ry.mean()
    sx = math.sqrt((rx ** 2).sum()); sy = math.sqrt((ry ** 2).sum())
    if sx == 0 or sy == 0:
        return np.nan
    return float((rx * ry).sum() / (sx * sy))


def rsi_wilder(close, win=14):
    n = len(close)
    out = np.full(n, np.nan)
    if n <= win:
        return out
    d = np.diff(close)
    up = np.where(d > 0, d, 0.0)
    dn = np.where(d < 0, -d, 0.0)
    au = up[:win].mean(); ad = dn[:win].mean()
    for i in range(win, n):
        if i > win:
            au = (au * (win - 1) + up[i - 1]) / win
            ad = (ad * (win - 1) + dn[i - 1]) / win
        out[i] = 100.0 if ad == 0 else 100.0 - 100.0 / (1.0 + au / ad)
    return out


def roll_mean(a, w):
    n = len(a)
    o = np.full(n, np.nan)
    for i in range(w - 1, n):
        seg = a[i - w + 1:i + 1]
        if np.isfinite(seg).sum() >= w * 0.8:
            o[i] = np.nanmean(seg)
    return o


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--min-amt", type=float, default=3e7)
    a = ap.parse_args()

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

    F = {k: np.full((T, N), np.nan, dtype=np.float32) for k in FEATS}
    for j in range(N):
        c = C[:, j]
        if np.isfinite(c).sum() < 80:
            continue
        prev = np.concatenate([[np.nan], c[:-1]])
        with np.errstate(invalid="ignore", divide="ignore"):
            F["mom1"][:, j] = c / prev - 1.0
            F["mom5"][:, j] = c / np.concatenate([np.full(5, np.nan), c[:-5]]) - 1.0
        F["rev1"][:, j] = -F["mom1"][:, j]
        F["rsi14"][:, j] = rsi_wilder(c, 14)
        with np.errstate(invalid="ignore", divide="ignore"):
            F["dist_ma5"][:, j] = c / roll_mean(c, 5) - 1.0
            F["dist_ma20"][:, j] = c / roll_mean(c, 20) - 1.0
        amt = P["amt"][:, j]
        with np.errstate(invalid="ignore", divide="ignore"):
            F["vol_ratio"][:, j] = amt / roll_mean(amt, 20)
        lr = np.full(T, np.nan)
        with np.errstate(invalid="ignore", divide="ignore"):
            lr[1:] = np.log(c[1:] / c[:-1])
        v = np.full(T, np.nan)
        for i in range(20, T):
            seg = lr[i - 19:i + 1]
            if np.isfinite(seg).all():
                v[i] = seg.std(ddof=1)
        F["vol20"][:, j] = v
        with np.errstate(invalid="ignore", divide="ignore"):
            F["amp"][:, j] = (H[:, j] - L[:, j]) / prev
            rng = H[:, j] - L[:, j]
            F["close_pos"][:, j] = np.where(rng > 0, (c - L[:, j]) / rng, 0.5)
            F["gap"][:, j] = O[:, j] / prev - 1.0
        lamt = np.log(np.where(amt > 0, amt, np.nan))
        mu, sd = roll_mean(lamt, 60), np.full(T, np.nan)
        for i in range(59, T):
            seg = lamt[i - 59:i + 1]
            if np.isfinite(seg).all():
                sd[i] = seg.std(ddof=1)
        with np.errstate(invalid="ignore", divide="ignore"):
            F["turn_z"][:, j] = (lamt - mu) / np.where(sd > 0, sd, np.nan)
            # amt_z：当日成交额横截面 z（跨股可比，去掉 turn_z 的时序成分）
            F["amt_z"][:, j] = lamt

    # 三段收益
    gap = np.full((T, N), np.nan)      # open[t+1]/close[t] − 1
    intra = np.full((T, N), np.nan)    # close[t+1]/open[t+1] − 1
    tot = np.full((T, N), np.nan)      # close[t+1]/close[t] − 1
    with np.errstate(invalid="ignore", divide="ignore"):
        gap[:-1] = O[1:] / C[:-1] - 1.0
        intra[:-1] = C[1:] / O[1:] - 1.0
        tot[:-1] = C[1:] / C[:-1] - 1.0

    ic = {k: {"gap": [], "intra": [], "tot": []} for k in FEATS}
    n_day = 0
    for t in range(25, T - 1):
        ok = (np.isfinite(C[t]) & np.isfinite(gap[t]) & np.isfinite(intra[t])
              & np.isfinite(P["amt"][t]) & (P["amt"][t] >= a.min_amt))
        if ok.sum() < 100:
            continue
        n_day += 1
        y_g, y_i, y_t = gap[t][ok], intra[t][ok], tot[t][ok]
        Xf = np.column_stack([F[k][t][ok] for k in FEATS]).astype(float)
        for ci, k in enumerate(FEATS):
            col = Xf[:, ci]
            m = np.isfinite(col)
            if m.sum() >= 30:
                lo, hi = np.nanpercentile(col[m], [1, 99])
                col = np.clip(col, lo, hi)
            ic[k]["gap"].append(spearman_col(col, y_g))
            ic[k]["intra"].append(spearman_col(col, y_i))
            ic[k]["tot"].append(spearman_col(col, y_t))

    print(f"  有效截面 {n_day} 个\n")

    def s(v):
        v = np.asarray(v, float); v = v[np.isfinite(v)]
        if len(v) < 10:
            return None
        mu, sd = float(v.mean()), float(v.std(ddof=1))
        return {"ic": round(mu, 5),
                "t": round(mu / (sd / math.sqrt(len(v))), 3) if sd > 0 else None,
                "pos_rate": round(float((v > 0).mean()), 4)}

    res = {}
    for k in FEATS:
        r = {h: s(ic[k][h]) for h in ("gap", "intra", "tot")}
        if not r["tot"]:
            continue
        # 判定：信号主要在哪一段
        tg = abs(r["gap"]["t"]) if r["gap"] and r["gap"]["t"] else 0
        ti = abs(r["intra"]["t"]) if r["intra"] and r["intra"]["t"] else 0
        if ti >= 2.0 and ti > tg:
            where = "日内（可执行）"
        elif tg >= 2.0 and tg > ti:
            where = "隔夜（不可执行）"
        elif tg >= 2.0 and ti >= 2.0:
            where = "两段都有"
        else:
            where = "无显著信号"
        # 方向一致性：gap 与 intra 的 IC 符号
        flip = bool(r["gap"] and r["intra"]
                    and np.sign(r["gap"]["ic"]) != np.sign(r["intra"]["ic"]))
        res[k] = {"gap": r["gap"], "intra": r["intra"], "tot": r["tot"],
                  "where": where, "sign_flip": flip}

    print(f"{'因子':<11}{'隔夜IC':>9}{'t':>7}{'日内IC':>9}{'t':>7}"
          f"{'合计IC':>9}{'t':>7}{'符号翻转':>9}  信号位置")
    for k, v in sorted(res.items(), key=lambda kv: -abs(kv[1]["tot"]["ic"])):
        g, i_, t_ = v["gap"], v["intra"], v["tot"]
        print(f"{k:<11}{g['ic'] if g else 0:>+9.4f}{g['t'] if g else 0:>7.2f}"
              f"{i_['ic'] if i_ else 0:>+9.4f}{i_['t'] if i_ else 0:>7.2f}"
              f"{t_['ic']:>+9.4f}{t_['t']:>7.2f}"
              f"{'是' if v['sign_flip'] else '否':>9}  {v['where']}")

    exe = [k for k, v in res.items() if v["where"] in ("日内（可执行）", "两段都有")]
    print(f"\n可执行口径（日内）显著的因子：{exe if exe else '无'}")

    json.dump({
        "meta": {
            "purpose": "T+1 收益拆解：定位信号在隔夜段还是日内段（决定 T+1 建议是否可执行）",
            "n_days": n_day, "date_range": [alld[0], alld[-1]],
            "segments": {
                "gap": "open[t+1]/close[t] − 1（隔夜跳空，收盘后信号无法参与）",
                "intra": "close[t+1]/open[t+1] − 1（日内，次日开盘可买入 → **可执行**）",
                "tot": "close[t+1]/close[t] − 1（合计）",
            },
            "verdict_rule": ("日内 |t|>=2.0 且 > 隔夜 |t| → 可执行；"
                             "隔夜 |t|>=2.0 且 > 日内 → 不可执行"),
            "t0_note": ("T0（日内回转）需要分钟线，本项目 K 线库只有日线 → "
                        "**T0 无法用现有数据回测**，任何 T0 结论只能作为"
                        "「需接入分钟线后验证」的方案，不能声称已验证。"),
        },
        "n_days": n_day,
        "results": res,
        "executable_factors": exe,
    }, open(OUT, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"\n✅ 已写出 {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
