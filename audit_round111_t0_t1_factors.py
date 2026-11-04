#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
audit_round111_t0_t1_factors.py —— T+1（次日）信号的真实横截面检验

═══ 为什么必须先做这件事，而不是直接给建议 ═══

老大要求「优化 T0 和 T+1 的建议」。给建议之前必须先回答一个问题：
**现有评分（total）对次日收益有区分力吗？**

用 recommendation_track.json 现有实测（12 个推荐日、1158 条同口径样本）先算：

    按 total 十等分 → T+1 超额（vs 沪深300）
        Q1(65.0~66.5)  −0.85%   胜率 39.1%
        Q10(84.8~93.4) −0.90%   胜率 41.4%
        Spearman(total, T+1超额) = −0.0120
        Top10% 超额 −0.92%  **反而差于** Bot10% 的 −0.85%

结论：**total 对次日收益零区分力**。它不是次日交易信号，也不该被当成次日信号用。
所以「优化 T+1 建议」不能靠调总分阈值，必须另找**专门针对 1 日横截面**的特征。

═══ 本脚本测什么 ═══

在 data/klines_long.json（2138 只 × 337 个交易日，2025-07-29~2026-11-02）上，
对每个交易日做**横截面**排序，计算每个「当日可观测」特征与**次日收益**的
Spearman IC，然后看：

    IC 均值 / IC t 值 / IC 胜率（IC>0 的交易日占比）/ 多空分位收益差

═══ 特征清单（全部只用 ≤t 的收盘数据，无前视）═══

  mom1       当日涨跌幅           
  mom5       5 日动量           
  rev1       1 日反转 = −mom1     
  rsi14      Wilder RSI(14)      
  dist_ma5   收盘/MA5 − 1        
  dist_ma20  收盘/MA20 − 1       
  vol_ratio  当日量 / 20 日均量   
  vol20      20 日收益波动率（低波溢价） 
  amp        当日振幅 (hi−lo)/prev_close
  close_pos  (close−low)/(high−low) 收盘位置
  turn_z     换手代理（成交额/20日均额）的 z 分
  gap        隔夜跳空 open/prev_close − 1
  amt_rank   当日成交额横截面分位（流动性/关注度）

═══ 判据（先写死，避免事后找理由）═══

  ① |IC 均值| ≥ 0.02  且  |IC t 值| ≥ 2.0   → 该因子在样本内显著
  ② IC 胜率 ≥ 55% 或 ≤ 45%                    → 方向稳定（不是靠极端日撑起）
  ③ 多空(顶10% − 底10%)次日收益差 均值 > 0      → 有经济价值（扣费前）
  ④ 子样本分段（按时间前后两半）IC 同号         → 不是单段运气

四条全过才标「可用」；只过 1~3 条标「弱」；全不过标「无效」。

⚠️ 本检验用的是 T+1 收益，**存在买卖时点可执行性问题**：
   信号用 t 日收盘算，t+1 日什么价成交？本脚本假设「t+1 开盘买入、t+1 收盘卖出」
   （即 ret_next = close[t+1]/open[t+1] − 1），同时给出「收盘到收盘」口径做对照。
   若只有收盘-收盘口径有效、开盘-收盘口径失效，说明信号是**隔夜跳空**驱动的，
   实盘抓不到（A 股开盘集合竞价滑点），必须标为不可用。

输出：audit_round111_t0_t1_factors.json
用法：python audit_round111_t0_t1_factors.py [--topn 214] [--min-amt 3e7]
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
OUT = os.path.join(HERE, "audit_round111_t0_t1_factors.json")

FEATS = ["mom1", "mom5", "rev1", "rsi14", "dist_ma5", "dist_ma20",
         "vol_ratio", "vol20", "amp", "close_pos", "turn_z", "gap"]


# ------------------------------------------------------------------ 工具
def spearman_vec(X, y):
    """X:(n,p) 每列与 y 的 Spearman 相关（向量化，处理并列用平均秩）。

    单日横截面 n≈2000，p=12，用 argsort 双排序算秩即可。
    """
    n = len(y)
    if n < 30:
        return np.full(X.shape[1], np.nan)

    def rank(a):
        o = np.argsort(a, kind="mergesort")
        r = np.empty(n, float)
        r[o] = np.arange(1, n + 1, dtype=float)
        # 并列取平均秩（价格/涨幅常有并列）
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

    ry = rank(y)
    ry = ry - ry.mean()
    sy = math.sqrt((ry ** 2).sum())
    out = np.full(X.shape[1], np.nan)
    if sy == 0:
        return out
    for c in range(X.shape[1]):
        col = X[:, c]
        m = np.isfinite(col)
        if m.sum() < 30:
            continue
        cc = col.copy()
        cc[~m] = np.nanmedian(col[m])
        rx = rank(cc)
        rx = rx - rx.mean()
        sx = math.sqrt((rx ** 2).sum())
        out[c] = float((rx * ry).sum() / (sx * sy)) if sx > 0 else np.nan
    return out


def rsi_wilder(close, win=14):
    """Wilder RSI，逐列就绪（不整体前向填充，避免前期假值）。"""
    n = len(close)
    out = np.full(n, np.nan)
    if n <= win:
        return out
    d = np.diff(close)
    up = np.where(d > 0, d, 0.0)
    dn = np.where(d < 0, -d, 0.0)
    au = up[:win].mean()
    ad = dn[:win].mean()
    for i in range(win, n):
        if i > win:
            au = (au * (win - 1) + up[i - 1]) / win
            ad = (ad * (win - 1) + dn[i - 1]) / win
        out[i] = 100.0 if ad == 0 else 100.0 - 100.0 / (1.0 + au / ad)
    return out


def roll_mean(a, w):
    n = len(a)
    o = np.full(n, np.nan)
    cs = np.nancumsum(np.insert(np.where(np.isfinite(a), a, 0.0), 0, 0.0))
    cnt = np.concatenate([[0], np.cumsum(np.isfinite(a).astype(float))])
    for i in range(w - 1, n):
        s = cs[i + 1] - cs[i + 1 - w]
        c = cnt[i + 1] - cnt[i + 1 - w]
        if c >= w * 0.8:
            o[i] = s / c
    return o


def roll_std_ret(lr, w):
    n = len(lr)
    o = np.full(n, np.nan)
    for i in range(w, n):
        seg = lr[i - w + 1:i + 1]
        if np.isfinite(seg).all():
            o[i] = seg.std(ddof=1)
    return o


# ------------------------------------------------------------------ 主流程
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--min-amt", type=float, default=3e7, help="当日最小成交额（元）")
    ap.add_argument("--topn", type=int, default=0,
                    help="每日取成交额前 N 只（0=不过滤）")
    ap.add_argument("--lag", type=int, default=1, help="持有天数（默认 1=次日）")
    a = ap.parse_args()

    print(f"读取 {KL} ...")
    kl = json.load(open(KL, encoding="utf-8"))
    codes = sorted(kl.keys())
    alld = sorted({d for c in codes for d in kl[c]})
    di = {d: i for i, d in enumerate(alld)}
    T, N = len(alld), len(codes)
    print(f"  股票 {N} 只 × 交易日 {T} 个（{alld[0]} ~ {alld[-1]}）")

    # ⚠️ 字段顺序坑（踩过一次）：klines_long 的 bar 是 [open, close, high, low, volume]，
    #   不是 OHLCV。写成 v[0]=close 会让 open/close 互换、量价全部错位，
    #   实测后果是「成交额过滤后 2084 只只剩 11 只」——不是数据稀疏，是读错了列。
    #   本文件与 append_klines_sina_fallback.py::parse_sina 的落盘顺序一致：
    #       m[d] = [o, cl, hi, lo, vol]
    P = {k: np.full((T, N), np.nan) for k in ("c", "o", "h", "l", "vol", "amt")}
    for j, cd in enumerate(codes):
        rows = kl[cd]
        for dt, v in rows.items():
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
            P["o"][i, j] = o_; P["c"][i, j] = c_
            P["h"][i, j] = h_; P["l"][i, j] = l_
            P["vol"][i, j] = v_
            # 成交额代理 = 成交量 × 收盘价（新浪 volume 单位为股，近似元）
            P["amt"][i, j] = v_ * vol_coef(cd) * c_
    del kl
    C = P["c"]
    print(f"  收盘价面板 {C.shape}，有效单元 {np.isfinite(C).sum():,}")

    # 预计算特征面板（按列/按行都行，这里按股票逐个算再写入，省内存）
    F = {k: np.full((T, N), np.nan, dtype=np.float32) for k in FEATS}
    for j in range(N):
        c = C[:, j]
        if np.isfinite(c).sum() < 60:
            continue
        lr = np.full(T, np.nan)
        with np.errstate(invalid="ignore", divide="ignore"):
            lr[1:] = np.log(c[1:] / c[:-1])
        F["mom1"][:, j] = c / np.concatenate([[np.nan], c[:-1]]) - 1.0
        F["rev1"][:, j] = -F["mom1"][:, j]
        with np.errstate(invalid="ignore"):
            F["mom5"][:, j] = c / np.concatenate([np.full(5, np.nan), c[:-5]]) - 1.0
        F["rsi14"][:, j] = rsi_wilder(c, 14)
        ma5, ma20 = roll_mean(c, 5), roll_mean(c, 20)
        with np.errstate(invalid="ignore"):
            F["dist_ma5"][:, j] = c / ma5 - 1.0
            F["dist_ma20"][:, j] = c / ma20 - 1.0
        amt = P["amt"][:, j]
        amt20 = roll_mean(amt, 20)
        with np.errstate(invalid="ignore", divide="ignore"):
            F["vol_ratio"][:, j] = amt / amt20
        F["vol20"][:, j] = roll_std_ret(lr, 20)
        h, l, o = P["h"][:, j], P["l"][:, j], P["o"][:, j]
        prev = np.concatenate([[np.nan], c[:-1]])
        with np.errstate(invalid="ignore", divide="ignore"):
            F["amp"][:, j] = (h - l) / prev
            rng = h - l
            F["close_pos"][:, j] = np.where(rng > 0, (c - l) / rng, 0.5)
            F["gap"][:, j] = o / prev - 1.0
        # turn_z：成交额相对自身 60 日的 z 分（跨期可比，消除个股规模差异）
        amt60 = roll_mean(amt, 60)
        # 用对数成交额的滚动波动做标准化
        lamt = np.log(np.where(amt > 0, amt, np.nan))
        mu60 = roll_mean(lamt, 60)
        sd60 = np.full(T, np.nan)
        for i in range(59, T):
            seg = lamt[i - 59:i + 1]
            if np.isfinite(seg).all():
                sd60[i] = seg.std(ddof=1)
        with np.errstate(invalid="ignore", divide="ignore"):
            F["turn_z"][:, j] = (lamt - mu60) / np.where(sd60 > 0, sd60, np.nan)

    # 次日收益两种口径
    Hh = a.lag
    with np.errstate(invalid="ignore", divide="ignore"):
        R_cc = np.full((T, N), np.nan)      # t+1..t+H 收盘→收盘
        R_oc = np.full((T, N), np.nan)      # t+1 开盘 → t+H 收盘（可执行口径）
        R_co = np.full((T, N), np.nan)      # t 收盘 → t+1 收盘（含隔夜）
        R_cc[:-Hh] = C[Hh:] / C[:-Hh] - 1.0
        R_oc[:-Hh] = C[Hh:] / P["o"][1:(T - Hh + 1)] - 1.0
        if Hh == 1:
            R_co[:-1] = C[1:] / C[:-1] - 1.0

    # ---- 逐日横截面 IC ----
    ic_cc = {k: [] for k in FEATS}
    ic_oc = {k: [] for k in FEATS}
    ls_cc = {k: [] for k in FEATS}     # 多空（收盘口径）
    ls_oc = {k: [] for k in FEATS}
    used_days, n_used = [], []

    for t in range(25, T - Hh):
        amt_t = P["amt"][t]
        ok = np.isfinite(C[t]) & np.isfinite(R_cc[t]) & np.isfinite(R_oc[t])
        ok &= np.isfinite(amt_t) & (amt_t >= a.min_amt)
        if a.topn > 0:
            idx = np.where(ok)[0]
            if len(idx) > a.topn:
                keep = idx[np.argsort(-amt_t[idx])[:a.topn]]
                ok = np.zeros(N, bool); ok[keep] = True
        if ok.sum() < 100:
            continue
        y_cc, y_oc = R_cc[t][ok], R_oc[t][ok]
        if y_cc.std() == 0:
            continue
        Xf = np.column_stack([F[k][t][ok] for k in FEATS]).astype(float)
        # 横截面去极值（winsor 1%/99%）防妖股主导秩相关
        for c_ in range(Xf.shape[1]):
            col = Xf[:, c_]
            m = np.isfinite(col)
            if m.sum() >= 30:
                lo, hi = np.nanpercentile(col[m], [1, 99])
                Xf[:, c_] = np.clip(col, lo, hi)
        icc = spearman_vec(Xf, y_cc)
        ico = spearman_vec(Xf, y_oc)
        for ci, k in enumerate(FEATS):
            if np.isfinite(icc[ci]):
                ic_cc[k].append(float(icc[ci]))
                # 多空：按该因子分位取顶/底 10%
                col = Xf[:, ci]
                m = np.isfinite(col)
                if m.sum() >= 100:
                    lo_th, hi_th = np.nanpercentile(col[m], [10, 90])
                    up = (col >= hi_th) & m
                    dn = (col <= lo_th) & m
                    if up.sum() >= 10 and dn.sum() >= 10:
                        ls_cc[k].append(float(y_cc[up].mean() - y_cc[dn].mean()))
                        ls_oc[k].append(float(y_oc[up].mean() - y_oc[dn].mean()))
            if np.isfinite(ico[ci]):
                ic_oc[k].append(float(ico[ci]))
        used_days.append(alld[t]); n_used.append(int(ok.sum()))

    print(f"  有效截面 {len(used_days)} 个，日均样本 {np.mean(n_used):.0f} 只\n")

    # ---- 汇总 ----
    def summ(v):
        v = np.asarray(v, float)
        v = v[np.isfinite(v)]
        if len(v) < 10:
            return None
        mu = float(v.mean())
        sd = float(v.std(ddof=1))
        t = mu / (sd / math.sqrt(len(v))) if sd > 0 else float("nan")
        return {"n_days": int(len(v)), "ic_mean": round(mu, 5),
                "ic_std": round(sd, 5), "ic_t": round(t, 3),
                "ic_pos_rate": round(float((v > 0).mean()), 4),
                "ic_ir": round(mu / sd, 4) if sd > 0 else None}

    def split_half(v, tag):
        v = np.asarray(v, float)
        m = np.isfinite(v)
        v = v[m]
        if len(v) < 40:
            return None
        h = len(v) // 2
        return {f"{tag}_h1": round(float(v[:h].mean()), 5),
                f"{tag}_h2": round(float(v[h:].mean()), 5),
                f"{tag}_same_sign": bool(np.sign(v[:h].mean()) == np.sign(v[h:].mean()))}

    res = {}
    for k in FEATS:
        s_cc, s_oc = summ(ic_cc[k]), summ(ic_oc[k])
        if not s_cc:
            continue
        lsc = summ(ls_cc[k]); lso = summ(ls_oc[k])
        sh = split_half(ic_cc[k], "ic") or {}
        # 四道判据
        g = []
        g.append(abs(s_cc["ic_mean"]) >= 0.02 and abs(s_cc["ic_t"]) >= 2.0)
        g.append(s_cc["ic_pos_rate"] >= 0.55 or s_cc["ic_pos_rate"] <= 0.45)
        g.append(bool(lsc and lsc["ic_mean"] > 0))
        g.append(bool(sh.get("ic_same_sign")))
        npass = int(sum(g))
        # 可执行性检查：开盘-收盘口径是否也显著
        exe_ok = bool(s_oc and abs(s_oc["ic_mean"]) >= 0.015
                      and abs(s_oc["ic_t"]) >= 1.8
                      and np.sign(s_oc["ic_mean"]) == np.sign(s_cc["ic_mean"]))
        verdict = ("可用" if (npass == 4 and exe_ok)
                   else ("弱" if npass >= 2 else "无效"))
        res[k] = {
            "ic_close_close": s_cc, "ic_open_close_exec": s_oc,
            "long_short_cc": lsc, "long_short_oc": lso,
            "gates": {"G1_signif": g[0], "G2_stable": g[1],
                      "G3_econ": g[2], "G4_subsample": g[3]},
            "n_gate_pass": npass, "executable": exe_ok,
            "verdict": verdict, **(sh or {}),
        }

    order = sorted(res.items(), key=lambda kv: -abs(kv[1]["ic_close_close"]["ic_mean"]))
    print(f"{'因子':<11}{'IC均值':>9}{'t值':>8}{'胜率':>7}{'多空%':>8}"
          f"{'开盘口径t':>10}{'门禁':>6}{'结论':>7}")
    for k, v in order:
        s = v["ic_close_close"]; l = v["long_short_cc"] or {}
        so = v["ic_open_close_exec"] or {}
        print(f"{k:<11}{s['ic_mean']:>+9.4f}{s['ic_t']:>8.2f}{s['ic_pos_rate']*100:>6.1f}%"
              f"{l.get('ic_mean',0)*100:>+8.2f}{so.get('ic_t',0):>10.2f}"
              f"{v['n_gate_pass']:>6d}{v['verdict']:>7}")

    json.dump({
        "meta": {
            "purpose": "T+1 次日横截面信号检验（给 T0/T+1 建议提供实证基础）",
            "sample": f"{alld[0]} ~ {alld[-1]}，{len(used_days)} 个有效截面，"
                      f"{N} 只股票",
            "horizon": Hh, "min_amt": a.min_amt, "topn": a.topn,
            "ret_cc": "t+H 收盘 / t 收盘 − 1（含隔夜，不可直接执行）",
            "ret_oc": "t+H 收盘 / t+1 开盘 − 1（**可执行口径**）",
            "gates": {
                "G1_signif": "|IC|>=0.02 且 |t|>=2.0",
                "G2_stable": "IC 胜率 >=55% 或 <=45%",
                "G3_econ": "多空顶10%−底10% 次日收益差 >0",
                "G4_subsample": "时间前后两半 IC 同号",
                "executable": "开盘→收盘口径 |IC|>=0.015 且 |t|>=1.8 且与收盘口径同号",
            },
            "known_bias": ("① 池内前视：股票池取当前 klines_long 全库（2138 只），"
                           "含少量已退市/新上市，未做严格 PIT 重建；"
                           "② 未扣交易成本（约 0.15%~0.25% 双边含冲击）；"
                           "③ 样本仅 2025-07 起 337 日，跨越单一牛市+回调段，"
                           "regime 依赖未检验；④ 未剔除涨跌停/停牌不可成交样本。"),
        },
        "n_days": len(used_days),
        "date_range": [alld[0], alld[-1]],
        "results": res,
        "ranked": [k for k, _ in order],
    }, open(OUT, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"\n✅ 已写出 {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
