#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
audit_round109d_vol_target.py —— 样本扩至 2015 后重定模型：**波动率目标法**（vol targeting）

═══ 为什么必须重做（本轮最重要的修正）═══

round109c 在 440 日样本上得出「T2 深回撤 AUC 0.72~0.79、Sharpe 改善 7/8」，
据此设计了「深回撤概率 → 三档降仓」的仓位逻辑。

**样本扩至 2873 日（2015-01 ~ 2026-11）后，该结论被推翻**：

  T2 AUC      0.727~0.786  →  0.509~0.587   （接近随机 0.5）
  经济价值       3/8 通过   →  0/8 通过      （MDD 改善甚至为负）

即：round109c 的 T2 是**小样本运气**，不是稳定信号。若继续用它调仓位，是拿噪声当信号。

═══ 但 T1（波动率）反而更稳 ═══

  T1 零训练 vol 分位 AUC：0.675~0.770（8/8 组全部 >0.63，且高于拟合模型 0.586~0.682）
  → 波动率聚集（volatility clustering）是金融学中最稳健的经验规律之一，
    11 年样本下依然成立，且**零训练分位法优于拟合模型**（无分布漂移风险）。

═══ 因此改用「波动率目标法」（vol targeting，业界标准做法）═══

不做「预测涨跌」也不做「预测回撤」，而是：
      w_t = min(w_max, target_vol / realized_vol_t)
即**按已实现波动率反向缩放仓位**：波动高 → 自动降杠杆；波动低 → 恢复。

这与「预测未来回撤」的本质区别：
  · 它不需要预测**方向**，也不需要预测**回撤点**；
  · 只用已经发生的波动率（无前视），作用是**稳定组合的风险暴露**，
    使"单位时间承担的风险"近似恒定 —— 这是风险预算管理的正统目标。

判据（先写死，不许事后改）：
  ① 波动率目标的**已实现波动**必须显著低于满仓（降幅 ≥15%）
  ② Sharpe 必须**不劣于**满仓（ΔSharpe ≥ 0）
  ③ MDD 必须改善（≥5%）
  → 三条同时满足才算「有风险管理价值」。

⚠️ 样本局限：2015-2026 含 2015 股灾、2016 熔断、2018 熊市、2022 熊市、2024-2025 反弹，
   覆盖多个完整周期，比 round109 的单一 regime 强得多；但仍只是**一个市场（A股）**，
   不能外推到其他市场。

用法：python audit_round109d_vol_target.py
输出：audit_round109d.json
"""
import os
import sys
import json
import math
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "audit_round109d.json")
sys.path.insert(0, HERE)

from audit_round109_regime_feasibility import load_bench  # noqa
# ⚠️ 不 import _ret / _vol：可行性脚本只导出 load_bench（返回 (dates, close)）。
#    跨脚本复用未声明的私有函数 = 静默 ImportError 风险，故本脚本自带实现。


def _vol(close, win):
    """rolling 20 日收益率标准差（**未年化**）；前 win 位置为 nan。"""
    c = np.asarray(close, float)
    n = len(c)
    r = np.full(n, np.nan)
    r[1:] = c[1:] / c[:-1] - 1.0
    out = np.full(n, np.nan)
    for t in range(win, n):
        seg = r[t - win + 1:t + 1]
        if np.all(np.isfinite(seg)):
            out[t] = float(seg.std(ddof=1))
    return out


TARGET_VOL_ANNUAL = 0.15   # 目标年化波动 15%（A股中性设定）
W_MAX = 1.0                # 仓位上限（不加杠杆）
W_MIN = 0.30               # 仓位下限（不清零，留结构性参与）
REBAL = 5                  # 每 5 交易日调整一次（降低换手）


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
    calmar = (cagr / abs(mdd)) if mdd < 0 else float("nan")
    return {"CAGR": round(float(cagr), 4), "vol": round(float(vol), 4),
            "MDD": round(mdd, 4), "Sharpe": round(float(sharpe), 3),
            "Calmar": round(float(calmar), 3)}


def vol_target_weights(close, target=TARGET_VOL_ANNUAL, w_max=W_MAX, w_min=W_MIN, rebal=REBAL):
    """波动率目标仓位（t 日决策、t+1 生效 → 无前视）。

    σ_t 用 **t 日及以前** 20 日已实现波动（年化）。
    w_t = clip(target/σ_t, w_min, w_max)；每 rebal 日更新一次，其余日沿用。
    """
    n = len(close)
    v20 = _vol(close, 20) * math.sqrt(252)          # 年化
    w = np.full(n, np.nan)
    last = 1.0
    for t in range(n):
        if t % rebal == 0 and np.isfinite(v20[t]) and v20[t] > 0:
            last = float(np.clip(target / v20[t], w_min, w_max))
        w[t] = last
    return w, v20


def run_index(code, name):
    dates, close = load_bench(code)
    n = len(close)
    dret = np.zeros(n)
    dret[1:] = close[1:] / close[:-1] - 1.0
    fwd1 = np.full(n, np.nan)
    fwd1[:-1] = dret[1:]        # t 日决策 → t+1 日收益（无前视）

    w, v20 = vol_target_weights(close)
    # 决策日 t 用 w[t]，赚 fwd1[t]；首 20 日波动未就绪，跳过
    ok = np.isfinite(fwd1) & np.isfinite(w) & np.isfinite(v20)
    idx = np.where(ok)[0]
    if len(idx) < 250:
        return None, dates

    r_mkt = fwd1[idx]
    w_use = w[idx]
    r_vt = r_mkt * w_use

    # 参照：固定 70% 仓位（简单降杠杆对照，检验「vol target 是否优于无脑降仓」）
    r_fix = r_mkt * 0.70

    p_buy = perf(r_mkt)
    p_vt = perf(r_vt)
    p_fix = perf(r_fix)
    if not (p_buy and p_vt and p_fix):
        return None, dates

    vol_cut = (p_buy["vol"] - p_vt["vol"]) / p_buy["vol"]
    mdd_impr = (abs(p_buy["MDD"]) - abs(p_vt["MDD"])) / abs(p_buy["MDD"]) if p_buy["MDD"] < 0 else 0.0
    d_sharpe = p_vt["Sharpe"] - p_buy["Sharpe"]
    passed = (vol_cut >= 0.15) and (d_sharpe >= 0.0) and (mdd_impr >= 0.05)

    return {
        "n_days": int(len(idx)),
        "range": [dates[int(idx[0])], dates[int(idx[-1])]],
        "avg_w": round(float(w_use.mean()), 3),
        "w_p10": round(float(np.percentile(w_use, 10)), 3),
        "w_p50": round(float(np.percentile(w_use, 50)), 3),
        "w_p90": round(float(np.percentile(w_use, 90)), 3),
        "buy_hold": p_buy, "vol_target": p_vt, "fixed_70": p_fix,
        "vol_reduction_pct": round(vol_cut * 100, 2),
        "mdd_improve_pct": round(mdd_impr * 100, 2),
        "sharpe_delta": round(d_sharpe, 3),
        "sharpe_delta_vs_fixed": round(p_vt["Sharpe"] - p_fix["Sharpe"], 3),
        "pass": bool(passed),
    }, dates


def main():
    out = {"meta": {
        "round": "109d",
        "purpose": "样本扩至 2015 后改用波动率目标法（T2 深回撤已被证伪）",
        "target_vol_annual": TARGET_VOL_ANNUAL,
        "w_max": W_MAX, "w_min": W_MIN, "rebal": REBAL,
        "criteria": {"vol_reduction_min_pct": 15, "sharpe_delta_min": 0.0, "mdd_improve_min_pct": 5},
        "why": "round109c 的 T2 深回撤在 2873 日样本上 AUC 由 0.73~0.79 跌至 0.51~0.59、"
               "经济价值 0/8 → 属小样本运气；T1 波动率 AUC 0.675~0.770 稳定 → 改用 vol targeting",
    }}
    npass = 0
    ntot = 0
    for code, name in (("000985", "中证全指"), ("000300", "沪深300"),
                       ("000905", "中证500"), ("000852", "中证1000")):
        r, _ = run_index(code, name)
        out[code] = {"name": name, "result": r}
        print(f"\n{'='*72}\n{name} ({code})")
        if not r:
            print("  ⚠️ 有效样本不足")
            continue
        b, v, f7 = r["buy_hold"], r["vol_target"], r["fixed_70"]
        print(f"  区间 {r['range'][0]} ~ {r['range'][1]}  n={r['n_days']}")
        print(f"  仓位分布：均 {r['avg_w']:.2f} | p10={r['w_p10']:.2f} p50={r['w_p50']:.2f} p90={r['w_p90']:.2f}")
        print(f"  满仓      CAGR={b['CAGR']:+.2%} vol={b['vol']:.2%} MDD={b['MDD']:.2%} Sharpe={b['Sharpe']:.2f}")
        print(f"  波动目标  CAGR={v['CAGR']:+.2%} vol={v['vol']:.2%} MDD={v['MDD']:.2%} Sharpe={v['Sharpe']:.2f}")
        print(f"  固定70%   CAGR={f7['CAGR']:+.2%} vol={f7['vol']:.2%} MDD={f7['MDD']:.2%} Sharpe={f7['Sharpe']:.2f}")
        print(f"  → 波动降 {r['vol_reduction_pct']:+.1f}% | MDD改善 {r['mdd_improve_pct']:+.1f}% | "
              f"ΔSharpe {r['sharpe_delta']:+.2f}")
        print(f"  → 相对固定70% 的 ΔSharpe {r['sharpe_delta_vs_fixed']:+.2f}"
              f"{'（vol target 更优 ✅）' if r['sharpe_delta_vs_fixed'] > 0 else '（不如无脑降仓 ❌）'}")
        print(f"  判定：{'✅ 过' if r['pass'] else '❌ 未过'}")
        ntot += 1
        npass += bool(r["pass"])

    out["summary"] = {"pass": int(npass), "total": int(ntot)}
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print(f"\n{'='*72}\n通过 {npass}/{ntot}")
    print(f"✅ 已写出 {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
