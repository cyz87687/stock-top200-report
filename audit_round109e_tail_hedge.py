#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
audit_round109e_tail_hedge.py —— 离散「尾部保险」规则 vs 连续 vol targeting vs 无脑降仓

═══ 为什么要做这一步 ═══

round109d 结果（2873 日样本）：
    连续 vol targeting ：1/4 通过
    相对「固定 70%」更优：仅 沪深300 1/4

→ 连续缩放（w = target_vol / realized_vol）在 A 股**没有超越简单降杠杆的能力**。
   原因：A 股波动率与未来收益**正相关**（高风险溢价期），连续缩仓在波动抬升时减仓，
   恰好错失波动率回归后的反弹（中证1000 CAGR 满仓 −0.28% → vol target −2.25%，
   而固定 70% 反而 +0.62%）。

═══ 但连续缩放失败 ≠ 风险刻度无用 ═══

A 股的真实特征是「低频暴涨 + 高频阴跌 + 偶发崩塌」。连续缩放会被日常噪声反复触发；
更贴合这种分布的规则是**尾部保险**：平时满仓吃 beta，只在极端风险读数出现时降仓。

本脚本检验 5 种离散规则，全部**只用 t 日及以前的读数**（无前视），
每日决策、次日生效：

  R0  满仓（基准）
  R1  固定 70%（无脑降仓对照，**必须跑赢它才算有价值**）
  R2  vol_pct ≥ 0.90 → 仓位 0.30，否则 1.00
  R3  vol_pct ≥ 0.80 → 仓位 0.50，否则 1.00
  R4  dd_pct  ≥ 0.90 → 仓位 0.50，否则 1.00   （距 60 日高点跌幅的历史分位）
  R5  vol_pct ≥ 0.90 或 dd_pct ≥ 0.90 → 0.30，否则 1.00

判据（先写死，不许事后改）——四条**同时**满足：
  ① 已实现波动降幅 ≥ 15%（相对满仓）
  ② ΔSharpe ≥ 0（相对满仓）
  ③ MDD 改善 ≥ 5%（相对满仓）
  ④ **ΔSharpe > 0 相对固定 70%** ← 关键：必须证明「择时」优于「无脑降仓」

若 R2~R5 全部跑不赢 R1，则结论是：**A 股指数层面没有可用的降仓择时信号，
仓位建议只能作为风险披露，不能作为 alpha 来源。** 这个结论本身也是交付物。

输出：audit_round109e.json
"""
import os
import sys
import json
import math
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "audit_round109e.json")
sys.path.insert(0, HERE)

from audit_round109_regime_feasibility import load_bench  # noqa

PCT_LOOKBACK = 250     # 分位回看窗口
VOL_WIN = 20           # 已实现波动窗口
DD_WIN = 60            # 距高点回撤窗口
MIN_HIST = 120         # 分位就绪最少历史


def _vol_ann(close, win=VOL_WIN):
    c = np.asarray(close, float)
    n = len(c)
    r = np.full(n, np.nan)
    r[1:] = c[1:] / c[:-1] - 1.0
    out = np.full(n, np.nan)
    for t in range(win, n):
        seg = r[t - win + 1:t + 1]
        if np.all(np.isfinite(seg)):
            out[t] = float(seg.std(ddof=1)) * math.sqrt(252)
    return out


def _dd60(close, win=DD_WIN):
    """距过去 win 日最高收盘的回撤（负值）。"""
    c = np.asarray(close, float)
    n = len(c)
    out = np.full(n, np.nan)
    for t in range(win, n):
        hi = c[t - win + 1:t + 1].max()
        if hi > 0:
            out[t] = c[t] / hi - 1.0
    return out


def _pct_rank(series, lookback=PCT_LOOKBACK, min_hist=MIN_HIST):
    """t 日读数在「t-lookback..t」历史中的分位；只用 ≤t 数据。"""
    s = np.asarray(series, float)
    n = len(s)
    out = np.full(n, np.nan)
    for t in range(n):
        if not np.isfinite(s[t]):
            continue
        lo = max(0, t - lookback + 1)
        hist = s[lo:t + 1]
        hist = hist[np.isfinite(hist)]
        if len(hist) < min_hist:
            continue
        out[t] = float((hist <= s[t]).sum()) / len(hist)
    return out


def perf(returns, ann=252):
    r = np.asarray(returns, float)
    if len(r) < 20:
        return None
    eq = np.cumprod(1 + r)
    n = len(r)
    cagr = eq[-1] ** (ann / n) - 1 if eq[-1] > 0 else float("nan")
    vol = r.std(ddof=1) * math.sqrt(ann)
    peak = np.maximum.accumulate(eq)
    mdd = float((eq / peak - 1).min())
    sharpe = (r.mean() * ann) / vol if vol > 0 else float("nan")
    return {"CAGR": round(float(cagr), 4), "vol": round(float(vol), 4),
            "MDD": round(float(mdd), 4), "Sharpe": round(float(sharpe), 3)}


def rules(vp, dp, n):
    """→ dict[rule_name] = weight array（t 日决策、t+1 生效）"""
    w = {}
    w["R0_满仓"] = np.ones(n)
    w["R1_固定70%"] = np.full(n, 0.70)
    w["R2_volp90_降0.3"] = np.where(vp >= 0.90, 0.30, 1.00)
    w["R3_volp80_降0.5"] = np.where(vp >= 0.80, 0.50, 1.00)
    w["R4_ddp90_降0.5"] = np.where(dp >= 0.90, 0.50, 1.00)
    w["R5_双极端_降0.3"] = np.where((vp >= 0.90) | (dp >= 0.90), 0.30, 1.00)
    return w


def run_index(code, name):
    dates, close = load_bench(code)
    n = len(close)
    dret = np.zeros(n)
    dret[1:] = close[1:] / close[:-1] - 1.0
    fwd1 = np.full(n, np.nan)
    fwd1[:-1] = dret[1:]

    v20 = _vol_ann(close)
    dd = _dd60(close)
    vp = _pct_rank(v20)
    dp = _pct_rank(dd)          # 回撤越深 → 分位越高

    W = rules(vp, dp, n)

    # 只在「分位已就绪」的区间比对（保证所有规则同区间，可比）
    ok = np.isfinite(fwd1) & np.isfinite(v20) & np.isfinite(vp) & np.isfinite(dp)
    idx = np.where(ok)[0]
    if len(idx) < 500:
        return None
    r_mkt = fwd1[idx]

    res = {}
    for k, warr in W.items():
        res[k] = perf(r_mkt * warr[idx])
    base = res["R0_满仓"]
    fix = res["R1_固定70%"]
    if not base or not fix:
        return None

    out = {"n_days": int(len(idx)), "range": [dates[int(idx[0])], dates[int(idx[-1])]],
           "vol_pct_p50": round(float(np.nanpercentile(vp[idx], 50)), 3),
           "vol_pct_p90": round(float(np.nanpercentile(vp[idx], 90)), 3),
           "perf": res, "verdict": {}}

    for k in ("R2_volp90_降0.3", "R3_volp80_降0.5", "R4_ddp90_降0.5", "R5_双极端_降0.3"):
        p = res[k]
        if not p:
            continue
        vol_cut = (base["vol"] - p["vol"]) / base["vol"] if base["vol"] > 0 else 0.0
        mdd_impr = ((abs(base["MDD"]) - abs(p["MDD"])) / abs(base["MDD"])
                    if base["MDD"] < 0 else 0.0)
        d_sh = p["Sharpe"] - base["Sharpe"]
        d_sh_fix = p["Sharpe"] - fix["Sharpe"]
        trig = float(np.mean(W[k][idx] < 1.0)) * 100
        passed = (vol_cut >= 0.15) and (d_sh >= 0.0) and (mdd_impr >= 0.05) and (d_sh_fix > 0.0)
        out["verdict"][k] = {
            "vol_reduction_pct": round(vol_cut * 100, 2),
            "mdd_improve_pct": round(mdd_impr * 100, 2),
            "sharpe_delta_vs_buy": round(d_sh, 3),
            "sharpe_delta_vs_fixed": round(d_sh_fix, 3),
            "trigger_days_pct": round(trig, 1),
            "pass": bool(passed),
        }
    return out


def main():
    out = {"meta": {
        "round": "109e",
        "purpose": "离散尾部保险规则 vs 连续 vol targeting vs 无脑降仓（同期对照）",
        "params": {"pct_lookback": PCT_LOOKBACK, "vol_win": VOL_WIN,
                   "dd_win": DD_WIN, "min_hist": MIN_HIST},
        "criteria": {"vol_reduction_min_pct": 15, "sharpe_delta_vs_buy_min": 0.0,
                     "mdd_improve_min_pct": 5, "sharpe_delta_vs_fixed_strictly_positive": True},
        "why": "round109d 连续 vol targeting 相对固定70% 仅 1/4 更优 → "
               "检验更贴合 A 股尾部特征的离散规则是否更有效",
    }}
    npass = 0
    ntot = 0
    for code, name in (("000985", "中证全指"), ("000300", "沪深300"),
                       ("000905", "中证500"), ("000852", "中证1000")):
        r = run_index(code, name)
        out[code] = {"name": name, "result": r}
        print(f"\n{'='*78}\n{name} ({code})")
        if not r:
            print("  ⚠️ 有效样本不足")
            continue
        print(f"  区间 {r['range'][0]} ~ {r['range'][1]}  n={r['n_days']}")
        b = r["perf"]["R0_满仓"]
        print(f"  R0 满仓      CAGR={b['CAGR']:+.2%} vol={b['vol']:.2%} "
              f"MDD={b['MDD']:.2%} Sharpe={b['Sharpe']:.2f}")
        for k in ("R1_固定70%", "R2_volp90_降0.3", "R3_volp80_降0.5",
                  "R4_ddp90_降0.5", "R5_双极端_降0.3"):
            p = r["perf"][k]
            v = r["verdict"].get(k)
            tag = ""
            if v:
                tag = (f" | vol↓{v['vol_reduction_pct']:+.1f}% MDD↑{v['mdd_improve_pct']:+.1f}% "
                       f"ΔSh(vs买){v['sharpe_delta_vs_buy']:+.2f} "
                       f"ΔSh(vs70){v['sharpe_delta_vs_fixed']:+.2f} "
                       f"触发{v['trigger_days_pct']:.0f}% "
                       f"{'✅过' if v['pass'] else '❌'}")
            print(f"  {k:<18} CAGR={p['CAGR']:+.2%} vol={p['vol']:.2%} "
                  f"MDD={p['MDD']:.2%} Sharpe={p['Sharpe']:.2f}{tag}")
        for k, v in r["verdict"].items():
            ntot += 1
            npass += bool(v["pass"])

    out["summary"] = {"pass": int(npass), "total": int(ntot)}
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print(f"\n{'='*78}\n通过 {npass}/{ntot}（规则×指数）")
    print(f"✅ 已写出 {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
