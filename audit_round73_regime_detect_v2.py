#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
audit_round73_regime_detect_v2.py —— regime 事前可检测性（修复前视版）

═══ round72 的致命 bug（必须记录，这是本轮最危险的自欺）═══

round72 用 `mkt[t-5]` 当"过去 5 日市场收益"，但
    Y[t] = t → t+20 的 h20 前瞻收益
    Y[t-5] = t-5 → t+15 的收益  ← 其中 t+1 .. t+15 **全在未来**
故 `past5_mkt` 与"未来市场收益"的 ρ 高达 **+0.6862**，那不是预测能力，
而是**它本身就是未来收益的一部分**（重叠 15/20 = 75% 的窗口）。

同理 `vol20` 用 `mkt[t-20..t-1]`，最后一个 Y[t-1] 覆盖 t-1→t+19，
也有 19/20 的重叠前视。

round72 因此得出"段C 价差 +0.3004 → regime 切换可行"的**伪结论**，作废。

═══ 正确做法 ═══

必须用**真正的日频已实现收益**构造信号。本脚本从 `data/klines_long.json`
（2138 只 × 302 日收盘价）重建「池内等权日频指数」，然后：

  事前信号（t 日收盘后已知）：
    P5   过去 5 个交易日池内等权累计收益
    P20  过去 20 个交易日
    P60  过去 60 个交易日
    V20  过去 20 日池内等权日收益年化波动
    MA   收盘 / 20 日均线 − 1（位置）
    DD   距 60 日高点的回撤

  目标（未来，不参与信号构造）：
    F20  未来 20 个交易日池内等权收益
    IC   组合打分与 Y 的截面秩相关

  检验：事前信号 与 F20 / IC 的秩相关 + bootstrap CI。

⚠️ 这里的关键是**时间轴对齐**：npz 的 dates 是信号日，
   klines 的日期是交易日，必须用「≤ signal_date 的最后交易日」取信号，
   用「signal_date + 20 个交易日」取未来收益。

用法：python audit_round73_regime_detect_v2.py
"""
import os
import sys
import json
import math
import bisect
import numpy as np
from scipy.stats import rankdata

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from audit_round70_icir_fast import DIMS, WEIGHTS, nw_t, stat, _f  # noqa
from audit_round71_regime import pearson, spearman, boot_ci  # noqa

NPZ = os.path.join(HERE, "data", "icir_arrays_h20.npz")
KLONG = os.path.join(HERE, "data", "klines_long.json")
OUT = os.path.join(HERE, "audit_round73.json")


def build_pool_index(klong_path):
    """从个股日线重建池内等权日频指数（等权、日频、无前视）。"""
    raw = json.load(open(klong_path, encoding="utf-8"))
    # 收集所有交易日
    days = sorted({d for c in raw for d in raw[c]})
    di = {d: i for i, d in enumerate(days)}
    n = len(days)

    # 每只股票的收盘价序列（缺失为 NaN）
    closes = []
    for c, bars in raw.items():
        arr = np.full(n, np.nan)
        for d, bar in bars.items():
            j = di.get(d)
            if j is None:
                continue
            v = None
            if isinstance(bar, dict):
                v = bar.get("close")
                if v is None:
                    v = bar.get("c")
            elif isinstance(bar, (list, tuple)) and len(bar) > 1:
                v = bar[1]
            if isinstance(v, (int, float)) and v == v and v > 0:
                arr[j] = float(v)
        closes.append(arr)
    C = np.vstack(closes)  # (nStock, nDay)

    # 日收益
    R = np.full_like(C, np.nan)
    R[:, 1:] = C[:, 1:] / C[:, :-1] - 1.0
    # 极端值治理（拆股/复权错误会导致 >200% 或 <-80%）
    R = np.where((R > 2.0) | (R < -0.8), np.nan, R)

    # 池内等权日收益 = 每日横截面均值
    with np.errstate(invalid="ignore"):
        cnt = (~np.isnan(R)).sum(axis=0)
        idx_ret = np.where(cnt > 50, np.nanmean(R, axis=0), np.nan)
    # 累计指数
    lvl = np.full(n, np.nan)
    lvl[0] = 1.0
    for j in range(1, n):
        r = idx_ret[j]
        lvl[j] = lvl[j - 1] * (1.0 + r) if (r == r and lvl[j - 1] == lvl[j - 1]) else lvl[j - 1]
    return days, di, idx_ret, lvl


def main():
    z = np.load(NPZ, allow_pickle=False)
    sig_dates = [str(x) for x in z["dates"]]
    X, M, Y = z["X"], z["M"], z["Y"]
    nD, nC, nK = X.shape

    wc = json.load(open(WEIGHTS, encoding="utf-8"))
    dm = np.array([float(wc["dim_max"].get(k, 10.0)) for k in DIMS])
    w = np.array([float(wc["weights"].get(k, 0.0)) for k in DIMS])
    V = np.where(M, np.minimum(np.where(np.isnan(X), 0.0, X), dm) / dm * 10.0, 0.0)
    den = M.astype(np.float64) @ w
    num = V @ w
    with np.errstate(invalid="ignore", divide="ignore"):
        S = np.where(den > 1e-12, num / den * 10.0, np.nan)
    RY = rankdata(Y, axis=1, nan_policy="omit")
    RS = rankdata(S, axis=1, nan_policy="omit")

    ic = []
    for t in range(nD):
        ok = (~np.isnan(RS[t])) & (~np.isnan(RY[t]))
        if ok.sum() < 100:
            ic.append(None); continue
        a = RS[t][ok] - RS[t][ok].mean()
        b = RY[t][ok] - RY[t][ok].mean()
        sx = math.sqrt((a * a).sum()); sy = math.sqrt((b * b).sum())
        ic.append(float((a * b).sum() / (sx * sy)) if sx > 0 and sy > 0 else None)

    print("重建池内等权日频指数 ...")
    days, di, idx_ret, lvl = build_pool_index(KLONG)
    print(f"  {len(days)} 交易日  {days[0]} ~ {days[-1]}")
    print(f"  指数终值 {lvl[-1]:.4f}  日均收益 {np.nanmean(idx_ret)*100:+.4f}%  "
          f"年化波动 {np.nanstd(idx_ret)*math.sqrt(244)*100:.2f}%")

    # ═══ 逐信号日取事前信号 ═══
    def sig_at(d_iso, kind):
        """用 ≤d_iso 的最后交易日构造信号。"""
        j = bisect.bisect_right(days, d_iso) - 1
        if j < 1:
            return None
        if kind in ("P5", "P20", "P60"):
            L = {"P5": 5, "P20": 20, "P60": 60}[kind]
            if j - L < 0:
                return None
            a, b = lvl[j - L], lvl[j]
            if not (a == a and b == b) or a <= 0:
                return None
            return float(b / a - 1.0)
        if kind == "V20":
            if j - 20 < 0:
                return None
            sub = idx_ret[j - 19:j + 1]
            sub = sub[~np.isnan(sub)]
            if len(sub) < 10:
                return None
            return float(np.std(sub, ddof=1) * math.sqrt(244))
        if kind == "MA":
            if j - 20 < 0:
                return None
            sub = lvl[j - 19:j + 1]
            if np.isnan(sub).any():
                return None
            return float(lvl[j] / sub.mean() - 1.0)
        if kind == "DD":
            if j - 60 < 0:
                return None
            sub = lvl[j - 60:j + 1]
            if np.isnan(sub).any() or sub.max() <= 0:
                return None
            return float(lvl[j] / sub.max() - 1.0)
        return None

    # 未来 20 交易日池内等权收益（用于验证信号的信息量，不参与信号构造）
    fwd20 = []
    for d in sig_dates:
        j = bisect.bisect_right(days, d) - 1
        if j < 0 or j + 20 >= len(days):
            fwd20.append(None); continue
        a, b = lvl[j], lvl[j + 20]
        fwd20.append(float(b / a - 1.0) if (a == a and b == b and a > 0) else None)

    KINDS = ("P5", "P20", "P60", "V20", "MA", "DD")
    sigs = {k: [sig_at(d, k) for d in sig_dates] for k in KINDS}

    print("\n" + "═" * 92)
    print("事前信号（仅 ≤t 日线）→ 未来 20 日池内等权收益 / 未来 IC")
    print("═" * 92)
    print(f"{'信号':8s}{'n':>5s}{'ρ(→未来市场)':>16s}{'ρ(→未来IC)':>15s}"
          f"{'CI(→IC)':>24s}{'判定':>10s}")
    print("-" * 92)
    res = {}
    for k in KINDS:
        sv = sigs[k]
        pairs = [(sv[t], fwd20[t], ic[t]) for t in range(nD)
                 if sv[t] is not None and fwd20[t] is not None and ic[t] is not None]
        if len(pairs) < 30:
            print(f"{k:8s}{len(pairs):>5d}  n 不足")
            continue
        s_ = [p[0] for p in pairs]
        f_ = [p[1] for p in pairs]
        i_ = [p[2] for p in pairs]
        r_f = spearman(s_, f_)
        r_i = spearman(s_, i_)
        lo, hi = boot_ci(s_, i_)
        sig = (lo < 0 and hi < 0) or (lo > 0 and hi > 0)
        res[k] = {"n": len(pairs), "rho_fwd_market": r_f, "rho_ic": r_i,
                  "ci_ic": [lo, hi], "significant": bool(sig)}
        print(f"{k:8s}{len(pairs):>5d}{r_f:>+16.4f}{r_i:>+15.4f}"
              f"  [{lo:+.3f}, {hi:+.3f}]{'★显著' if sig else '不显著':>10s}")

    # ═══ 段C 样本外检验 ═══
    print("\n" + "═" * 92)
    print("样本外检验：切点用段A中位数冻结，段C 只测一次")
    print("═" * 92)
    nA, nB = int(nD * 0.40), int(nD * 0.70)
    segs = {"A": (0, nA), "B": (nA, nB), "C": (nB, nD), "ALL": (0, nD)}
    rows = []
    for k in KINDS:
        sv = sigs[k]
        av = [sv[t] for t in range(nA) if sv[t] is not None]
        if len(av) < 20 or k not in res or not res[k]["significant"]:
            continue
        cut = float(np.median(av))
        row = {"signal": k, "cut": cut, "seg": {}}
        for sk, (a, b) in segs.items():
            low = [ic[t] for t in range(a, b) if sv[t] is not None and ic[t] is not None and sv[t] <= cut]
            high = [ic[t] for t in range(a, b) if sv[t] is not None and ic[t] is not None and sv[t] > cut]
            lm = sum(low) / len(low) if low else None
            hm = sum(high) / len(high) if high else None
            row["seg"][sk] = {"low_n": len(low), "low_ic": lm,
                              "high_n": len(high), "high_ic": hm,
                              "spread": (lm - hm) if (lm is not None and hm is not None) else None}
        rows.append(row)
        c = row["seg"]["C"]
        print(f"  {k:6s} 切点{cut:+.4f}  段A价差{_f(row['seg']['A']['spread'],3)}  "
              f"段B{_f(row['seg']['B']['spread'],3)}  段C{_f(c['spread'],3)}  "
              f"(段C 低侧n={c['low_n']} IC={_f(c['low_ic'],3)} / 高侧n={c['high_n']} IC={_f(c['high_ic'],3)})")

    print("\n" + "═" * 92)
    print("裁决")
    print("═" * 92)
    good = [r for r in rows if r["seg"]["C"]["spread"] is not None
            and r["seg"]["C"]["spread"] > 0.05
            and r["seg"]["C"]["low_n"] >= 15 and r["seg"]["C"]["high_n"] >= 15]
    if good:
        print(f"  ✅ {len(good)} 个信号在段C 样本外仍给出 >0.05 的 IC 价差：")
        for r in good:
            c = r["seg"]["C"]
            print(f"     {r['signal']}: 价差 {c['spread']:+.4f} "
                  f"（低侧 {c['low_n']} 期 IC {c['low_ic']:+.4f} / "
                  f"高侧 {c['high_n']} 期 IC {c['high_ic']:+.4f}）")
        verdict = True
    else:
        print("  ❌ 无任何事前信号在段C 给出 >0.05 的价差 → regime 关系**不可事前利用**")
        verdict = False

    if not verdict:
        print("\n  ⇒ round71 的 r=−0.67 是**真实存在但不可交易**的关系：")
        print("     它说明模型的 IC 高度依赖市场环境，因此")
        print("     ① 全样本 ICIR 最大化必然在两类 regime 间取平均 → 样本外不稳定；")
        print("     ② 任何『按 regime 调权重』的方案都缺乏事前可用信号；")
        print("     ③ 正确做法不是调权重，而是**承认模型只在跌市/震荡市有效**，")
        print("        在涨市里降低仓位或不使用，这属于仓位管理而非因子优化。")

    json.dump({
        "meta": {"round": 73,
                 "purpose": "regime 事前可检测性（修复 round72 的重叠窗口前视）",
                 "npz": os.path.relpath(NPZ, HERE),
                 "klong": os.path.relpath(KLONG, HERE),
                 "weights_version": wc["version"],
                 "n_dates": nD, "date_range": [sig_dates[0], sig_dates[-1]],
                 "signal_timing": "仅用 ≤signal_date 的日线收盘（无重叠窗口）",
                 "round72_deprecated": (
                     "round72 用 mkt[t-5]=Y[t-5] 当过去5日收益，但 Y[t-5] 覆盖 "
                     "t-5→t+15，与未来重叠 75% → 前视。其『段C 价差+0.30』作废。")},
        "pool_index": {"n_days": len(days), "final_lvl": float(lvl[-1]),
                       "ann_vol": float(np.nanstd(idx_ret) * math.sqrt(244))},
        "signals": res, "seg_rows": rows,
        "exploitable": bool(verdict),
    }, open(OUT, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"\n→ {OUT}")


if __name__ == "__main__":
    main()
