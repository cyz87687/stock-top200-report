#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
audit_round113_holding_period.py —— T+1 优化方向定案：持有期扫描 + 成本天花板

═══ 为什么必须做这一步 ═══

round111c 已证明：H=1（次日）在全部 5 个可执行口径下，13 因子 **零通过**。
但「H=1 不行」不等于「T+1 建议无救」。存在两种可能，必须分开验：

  可能性 A：信号本身弱 → 拉长持有期也救不回来（成本摊薄但 IC 不涨）
  可能性 B：信号在短周期被噪声/成本吃掉 → 拉长到 H=3/5/10 后扣费转正

**这两者的处置完全不同**：
  · 若是 A → T+1 建议只能改「不预测方向」，走执行层优化（成本、时机中性化）
  · 若是 B → T+1 建议应改成「持有 H 天」（本质是 T+N 而非 T+1）

═══ 口径设计（严格可执行，不打折）═══

持有期 H 的可执行口径唯一选法是「次日开盘买 → 第 H 日收盘卖」：
    r_H = close[t+H] / open[t+1] − 1
理由：
  · 信号在 t 日收盘后算出 → 最早只能 t+1 开盘成交（不能用 close[t]，那是内幕）
  · 卖出放在 close[t+H] 也略乐观（收盘集合竞价可能滑点），但方向不至于反
    → 报告里必须写「卖出端滑点未建模」，不能声称这是完整净值

⚠️ 与 round111c 的 E1/E2/E3 口径对照（避免同口径两处实现漂移）：
  · H=1 时本脚本 r_1 = close[t+1]/open[t+1]−1 == round111c 的 E1（T0 代理）
  · H=2 时 r_2 = close[t+2]/open[t+1]−1，**不是** E3（E3 是 close[t+1]→close[t+2]）
    → 两者差一个隔夜段，数值不应相同，这是设计差异不是 bug

═══ 成本模型（三档，全部披露）═══

  · 佣金+过户费：单边 0.03%（万三，含最低 5 元约束的近似）
  · 印花税：卖出 0.05%（2023-08 后减半）
  · 滑点：单边 0.10%（小市值池，保守）
  → 单边合计 ≈ 0.13%，双边 ≈ 0.26%
但资金周转 H 天一次 → **每 H 天的成本率是 0.26%，年化成本 ≈ 0.26% × 244/H**
这一点是「拉长持有期」唯一确定的正向作用：**降成本频率**，不是涨 IC。

═══ 判据（与 round111c 完全一致，禁放宽）═══

  |IC| ≥ 0.02 且 |t| ≥ 2.0 且 IC胜率 ≥ 55% 且 扣费后多空 > 0
额外记录「净超额 / H」= 每日摊薄的净多空，用于横向比较不同 H 的效率。

输出：audit_round113_holding_period.json
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
KL = os.path.join(HERE, "data", "klines_long.json")
OUT = os.path.join(HERE, "audit_round113_holding_period.json")

FEATS = ["mom1", "mom5", "rev1", "rsi14", "dist_ma5", "dist_ma20",
         "vol_ratio", "vol20", "amp", "close_pos", "turn_z", "gap", "amt_z"]

HORIZONS = [1, 2, 3, 5, 10, 20]

# 成本三档（单边），全部落进 JSON 供审计
COST_ONE_WAY = {
    "commission": 0.0003,     # 佣金万三（单边）
    "stamp_on_sell": 0.0005,  # 印花税卖出 0.05%
    "slippage": 0.0010,       # 滑点单边 0.10%（小市值保守）
}
COST_RT = (COST_ONE_WAY["commission"] * 2
           + COST_ONE_WAY["stamp_on_sell"]
           + COST_ONE_WAY["slippage"] * 2)   # ≈ 0.0026


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
    ap.add_argument("--quantile", type=float, default=0.2,
                    help="多空分位（0.2 = 多前20%% 空后20%%）")
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
                # ⚠️ klines_long 的 bar 是 [open, close, high, low, volume]，
                #   不是 OHLCV。这个坑 round111 踩过一次（导致 2084 只剩 11 只）。
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
            F["amt_z"][:, j] = lamt

    # ⚠️ 持有期收益：r_H[t] = close[t+H] / open[t+1] − 1
    #    起点用 open[t+1]（信号 t 日收盘后才算出 → 最早次日开盘成交）。
    #    绝不能用 close[t] 作起点，那是用信号当天的收盘价买入 = 前视。
    RH = {}
    for h in HORIZONS:
        r = np.full((T, N), np.nan)
        if h < T - 1:
            with np.errstate(invalid="ignore", divide="ignore"):
                r[:T - h] = C[h:] / O[1:T - h + 1] - 1.0
        RH[h] = r

    ic = {k: {h: [] for h in HORIZONS} for k in FEATS}
    ls = {k: {h: [] for h in HORIZONS} for k in FEATS}
    n_day = 0
    for t in range(25, T - 1):
        # 截面有效性：当日有成交额 + 当日开盘可交易 + 最短持有期收益已跑完
        base_ok = (np.isfinite(C[t]) & np.isfinite(O[t + 1])
                   & np.isfinite(P["amt"][t]) & (P["amt"][t] >= a.min_amt))
        if base_ok.sum() < 100:
            continue
        n_day += 1
        Xf = np.column_stack([F[k][t] for k in FEATS]).astype(float)
        for ci, k in enumerate(FEATS):
            col = Xf[:, ci]
            m0 = np.isfinite(col)
            if m0.sum() >= 30:
                lo, hi = np.nanpercentile(col[m0], [1, 99])
                col = np.clip(col, lo, hi)
            for h in HORIZONS:
                ok = base_ok & np.isfinite(col) & np.isfinite(RH[h][t])
                if ok.sum() < 100:
                    continue
                y = RH[h][t][ok]
                ic[k][h].append(spearman_col(col[ok], y))
                # 多空：按因子分位切多空组
                x = col[ok]
                lo_q = np.nanquantile(x, a.quantile)
                hi_q = np.nanquantile(x, 1 - a.quantile)
                top = y[x <= lo_q]        # 因子值低组
                bot = y[x >= hi_q]        # 因子值高组
                if len(top) >= 10 and len(bot) >= 10:
                    ls[k][h].append(float(np.nanmean(top) - np.nanmean(bot)))

    print(f"  有效截面 {n_day} 个")

    def stat(v):
        v = np.asarray(v, float); v = v[np.isfinite(v)]
        if len(v) < 10:
            return None
        mu, sd = float(v.mean()), float(v.std(ddof=1))
        return {"ic": round(mu, 5),
                "t": round(mu / (sd / math.sqrt(len(v))), 3) if sd > 0 else None,
                "pos_rate": round(float((v > 0).mean()), 4)}

    # 成本摊薄：持有 h 天，一个「买-卖」周期成本 0.26%，摊到每天 = /h
    # 但多空的「毛收益」是 h 天的累计收益 → 净 = 毛 − 单周期成本
    res = {}
    for k in FEATS:
        per_h = {}
        for h in HORIZONS:
            s = stat(ic[k][h])
            if not s:
                continue
            g = np.asarray(ls[k][h], float); g = g[np.isfinite(g)]
            gross = float(g.mean()) * 100 if len(g) else None      # %
            net = (gross - COST_RT * 100) if gross is not None else None
            per_h[str(h)] = {
                "ic": s["ic"], "t": s["t"], "pos_rate": s["pos_rate"],
                "ls_gross_pct": round(gross, 4) if gross is not None else None,
                "ls_net_pct": round(net, 4) if net is not None else None,
                "net_per_day_pct": (round(net / h, 5)
                                    if net is not None else None),
                "n_ic": len([x for x in ic[k][h] if np.isfinite(x)]),
                # 判据与 round111c 完全一致，禁放宽
                "pass": bool(s["t"] is not None and abs(s["ic"]) >= 0.02
                             and abs(s["t"]) >= 2.0
                             and (s["pos_rate"] >= 0.55 or s["pos_rate"] <= 0.45)
                             and net is not None and net > 0),
            }
        if per_h:
            res[k] = per_h

    # 汇总：每个持有期有几个因子通过
    by_h = {}
    for h in HORIZONS:
        winners = [k for k in res if res[k].get(str(h), {}).get("pass")]
        best_net = max((res[k][str(h)]["net_per_day_pct"]
                        for k in res if str(h) in res[k]
                        and res[k][str(h)]["net_per_day_pct"] is not None),
                       default=None)
        by_h[str(h)] = {"n_pass": len(winners), "winners": winners,
                        "best_net_per_day_pct": best_net}

    # 打印矩阵
    print(f"\n{'因子':<11}" + "".join(f"{'H=' + str(h):>16}" for h in HORIZONS))
    print(f"{'':<11}" + "".join(f"{'IC / 净多空':>16}" for h in HORIZONS))
    for k in sorted(res, key=lambda x: -abs(res[x].get("1", {}).get("ic", 0)
                                           if res[x].get("1") else 0)):
        line = f"{k:<11}"
        for h in HORIZONS:
            r = res[k].get(str(h))
            if not r:
                line += f"{'—':>16}"
            else:
                mark = "✓" if r["pass"] else " "
                line += f"{r['ic']:>+7.4f}/{r['ls_net_pct']:>+6.2f}{mark}  "
        print(line)

    print("\n各持有期通过数：")
    for h in HORIZONS:
        b = by_h[str(h)]
        print(f"  H={h:<3} 通过 {b['n_pass']:>2} 个  "
              f"{b['winners']}  最佳日均净 {b['best_net_per_day_pct']}")

    # 结论判定：区分「信号弱」与「成本吃掉」
    any_pass = sum(by_h[str(h)]["n_pass"] for h in HORIZONS)
    best_h = max(HORIZONS, key=lambda h: (by_h[str(h)]["n_pass"],
                                          by_h[str(h)]["best_net_per_day_pct"] or -9))
    # 检查 IC 是否随 H 单调增强（若有 = 信号在长周期，属可能性 B）
    ic_trend = {}
    for k in res:
        seq = [res[k].get(str(h), {}).get("ic") for h in HORIZONS]
        seq = [x for x in seq if x is not None]
        if len(seq) >= 3:
            ic_trend[k] = round(float(seq[-1] - seq[0]), 5)

    if any_pass == 0:
        verdict = ("全部持有期（H=1/2/3/5/10/20）扣费后**零因子通过** → "
                   "不是「H=1 太短」，是**日频因子在本池扣费后没有可利用的方向信息**。"
                   "T+1 建议只能走「不预测方向」的执行层优化，不能靠拉长持有期救。")
    elif best_h > 1:
        verdict = (f"H=1 不通过但 H={best_h} 有 {by_h[str(best_h)]['n_pass']} 个因子通过 → "
                   f"信号属于「长周期有效」，T+1 建议应改为「T+{best_h}」持有。")
    else:
        verdict = f"仅 H=1 有 {by_h['1']['n_pass']} 个因子通过 → 可保留 T+1 口径。"

    print(f"\n{'='*66}\n结论：{verdict}\n{'='*66}")

    json.dump({
        "meta": {
            "purpose": "持有期扫描：判定「H=1 不通过」是信号弱还是成本吃掉",
            "n_days": n_day, "date_range": [alld[0], alld[-1]],
            "n_codes": N,
            "horizons": HORIZONS,
            "return_def": "r_H[t] = close[t+H] / open[t+1] − 1（信号 t 日收盘算出，t+1 开盘买入）",
            "cost_one_way": COST_ONE_WAY,
            "cost_roundtrip": round(COST_RT, 6),
            "cost_note": ("单周期（一次买+一次卖）成本 "
                          f"{COST_RT*100:.2f}%；持有 H 天摊到每天 = "
                          f"{COST_RT*100:.2f}%/H。拉长持有期唯一确定的正向作用"
                          "是**降低交易频率**，不是提升 IC。"),
            "gates": "|IC|>=0.02 且 |t|>=2.0 且 IC胜率>=55%(或<=45%) 且 扣费后多空>0",
            "sell_slippage_note": ("卖出端用 close[t+H]，未建模收盘集合竞价滑点；"
                                   "实际净值应再打折，故「通过」也只是上界估计。"),
            "executability": ("r_H 起点 open[t+1] 是**可执行**的（信号 t 日收盘后才有）；"
                              "对照：若用 close[t] 作起点则含隔夜跳空，不可执行。"),
        },
        "n_days": n_day,
        "results": res,
        "by_horizon": by_h,
        "ic_trend_h_last_minus_first": ic_trend,
        "verdict": verdict,
        "any_pass_total": any_pass,
        "best_horizon": best_h,
    }, open(OUT, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"\n✅ 已写出 {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
