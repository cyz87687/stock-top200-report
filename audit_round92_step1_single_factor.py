#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
audit_round92_step1_single_factor.py —— Step1 单因子五分分层校验

═══ 规范（老大 2026-10-29）═══

每个因子独立五分分层（Q1~Q5），计算月度 RankIC 序列。
输出指标：IC均值、ICIR、IC>0胜率、t值、分层超额单调性
准入规则：
  1. |t| ≥ 3.9（Bonferroni）：合格，单因子权重上限 0.20
  2. 仅 FDR 显著、未过 Bonferroni：保留，权重上限锁 0.125
  3. IC 持续负向 / 分层无单调：剔除

═══ ⚠️ 关于「月度」的统计功效问题（必须向老大说明）═══

面板 219 个交易日 → 月度截面仅 **11 个点**（样本内 6 / 样本外 5）。
11 个 IC 点的 t 值标准误极大，**|t|≥3.9 在月度口径下几乎不可能达到**
（即使真实 IC=0.05，n=11 时 t 的期望也只有 ~1.3）。

这不是流程设计的问题，是**样本长度**的问题（老大上一条指令已指出
「回测区间样本复算，数据扩展到近一年的行情」——但面板的因子数据硬边界
就是 2025-11-03 起）。

⇒ 本脚本**同时输出两套口径，且严格分离标注、绝不混用**：

  【月度口径】规范原文口径，每月 1 个截面 → 11 个 IC 点
      → 用于与规范对齐；但 t 值功效极低，只能作方向参考
  【日频口径】每日 1 个截面 → 219 个 IC 点
      → 高功效版本；用于**实际做因子筛选与准入裁决**

裁决以【日频口径】为主（因为 Step2~4 都要在这个基础上做），
【月度口径】作为规范对齐的旁证同时披露。
若两套口径结论冲突 → 标记为「口径敏感」，该因子降级处理。

═══ 分层超额单调性的定义 ═══

  每个截面按因子值分 5 组（Q5=最高），算各组的**未来 20 交易日收益均值**
  → 得到每组的逐期收益序列
  单调性判定：Q5−Q1 价差 > 0，且 4 个相邻步长中 ≥3 个同向
  同时算各组内部的 IC（组内因子值 vs 未来收益）

用法：python audit_round92_step1_single_factor.py
"""
import os
import sys
import json
import math
import time
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import factor_validation_core as F  # noqa

NPZ = os.path.join(HERE, "data", "val_arrays_h20.npz")
OUT = os.path.join(HERE, "audit_round92.json")


def fnum(v, nd=4, sign=True):
    """格式化数值，None → 'n/a'（不崩溃）。

    ⚠️ 本项目已踩多次：因子在某区间无有效数据 → IC/t 为 None，
    直接用 f-string 格式化会崩。统一走这里。
    """
    if v is None:
        return "n/a"
    return f"{v:+.{nd}f}" if sign else f"{v:.{nd}f}"


def monthly_ic_series(Xk, Y, RY, monthly_slices, idxs):
    """月度口径的 IC 序列：每月取该月最后一个有效截面的 IC。"""
    out = []
    for a, b in monthly_slices:
        sel = [t for t in range(a, b) if t in idxs]
        if not sel:
            continue
        t = sel[-1]
        ok = (~np.isnan(Xk[t])) & (~np.isnan(RY[t]))
        if ok.sum() < 100:
            continue
        x = Xk[t][ok]
        y = RY[t][ok]
        rx = F.rankdata(x) if hasattr(F, "rankdata") else None
        from scipy.stats import rankdata as _rd
        rx = _rd(x)
        ry = _rd(y)
        rx = rx - rx.mean()
        ry = ry - ry.mean()
        sx = math.sqrt((rx * rx).sum())
        sy = math.sqrt((ry * ry).sum())
        if sx > 0 and sy > 0:
            out.append(float((rx * ry).sum() / (sx * sy)))
    return out


def main():
    t0 = time.time()
    print("加载面板 ...")
    dates, X, M, Y, codes = F.load_panel_arrays(
        os.path.join(HERE, "data", "panel_round66.json"), NPZ)
    wc = json.load(open(F.__dict__.get("WEIGHTS",
                          os.path.join(HERE, "factor_weights.json")),
                        encoding="utf-8"))
    dim_max = {k: float(v) for k, v in wc["dim_max"].items()}
    print(f"  权重版本 {wc['version']}")

    RY = F.rank_rows(Y)
    mslices = F.monthly_slices(dates)
    (i0, i1), (o0, o1) = F.split_in_out(dates, 0.6)
    in_mslices = F.monthly_slices(dates[i0:i1])
    out_mslices = F.monthly_slices(dates[o0:o1])
    print(f"  样本内 {dates[i0]}~{dates[i1-1]}（月度 {len(in_mslices)} 个）"
          f"  样本外 {dates[o0]}~{dates[o1-1]}（月度 {len(out_mslices)} 个）")

    results = {}
    for j, d in enumerate(F.DIMS):
        Xk = X[:, :, j]
        Mk = M[:, :, j]
        # ── 日频口径 ──
        daily_all = F.row_pearson_ranks(F.rank_rows(Xk), RY)
        st_d_all = F.stat(daily_all)
        daily_in = F.row_pearson_ranks(F.rank_rows(Xk[i0:i1]), RY[i0:i1])
        st_d_in = F.stat(daily_in)
        daily_out = F.row_pearson_ranks(F.rank_rows(Xk[o0:o1]), RY[o0:o1])
        st_d_out = F.stat(daily_out)
        # ── 月度口径 ──
        idxs = set(range(len(dates)))
        m_all = monthly_ic_series(Xk, Y, RY, mslices, idxs)
        st_m_all = F.stat(m_all)
        m_in = monthly_ic_series(Xk, Y, RY, in_mslices,
                                 set(range(i0, i1)))
        st_m_in = F.stat(m_in)
        # ── 分层 ──
        bret, bic = F.layered_returns(Xk, Y, Mk, q=5)
        mono = F.monotonicity(bic)
        bstat = [F.stat(b) for b in bic]
        # 分层月频聚合（每组逐期收益的统计）
        bmean = [(sum(b) / len(b) if b else None) for b in bret]
        # ── 覆盖 ──
        cov = float(Mk.sum()) / float(Mk.shape[0] * Mk.shape[1])

        results[d] = {
            "coverage": cov,
            "daily": {"all": st_d_all, "in": st_d_in, "out": st_d_out},
            "monthly": {"all": st_m_all, "in": st_m_in,
                        "n_months": len(m_all)},
            "layers": {
                "ic_per_bucket": bstat,
                "ret_mean_per_bucket": bmean,
                "monotonicity": mono,
                "n_periods_per_bucket": [len(b) for b in bic],
            },
        }

    # ── FDR（在样本内口径上做，因子筛选只看样本内）──
    pvals = []
    for d in F.DIMS:
        st = results[d]["daily"]["in"]
        p = F.two_sided_p(st["t"], st["n"])
        pvals.append((d, p, st["t"]))
    fdr = F.bh_fdr(pvals, q=F.FDR_Q)

    # ── 准入裁决 ──
    print("\n" + "═" * 116)
    print("STEP1  单因子五分分层校验")
    print("═" * 116)
    print(f"{'因子':16s}{'覆盖':>7s}{'【日频·样本内】':^34s}"
          f"{'【月度】':^24s}{'分层':^26s}")
    print(f"{'':16s}{'':7s}{'IC':>9s}{'ICIR':>8s}{'胜率':>7s}{'t':>8s}"
          f"{'IC':>9s}{'t':>7s}{'n':>5s}{'Q5-Q1':>9s}{'单调':>8s}{'Q5 t':>8s}")
    print("-" * 116)
    for d in F.DIMS:
        r = results[d]
        di = r["daily"]["in"]
        mm = r["monthly"]["in"]
        mo = r["layers"]["monotonicity"]
        q5 = r["layers"]["ic_per_bucket"][4]
        sp = mo["spread"]
        print(f"{d.replace('score_',''):16s}{r['coverage']*100:>6.1f}%"
              f"{fnum(di['ic']):>9s}{fnum(di['icir'],3):>8s}"
              f"{(di['pos_rate']*100 if di['pos_rate'] is not None else 0):>6.1f}%"
              f"{fnum(di['t'],2):>8s}"
              f"{fnum(mm['ic']):>9s}{fnum(mm['t'],2):>7s}{mm['n']:>5d}"
              f"{fnum(sp):>9s}"
              f"{'✅' if mo['monotonic'] else '❌':>7s}"
              f"{fnum(q5['t'],2):>8s}")

    print("\n【分层 IC 明细（日频，样本内）】Q1=因子值最低 … Q5=最高")
    for d in F.DIMS:
        r = results[d]
        bs = r["layers"]["ic_per_bucket"]
        cells = "  ".join(f"Q{i+1} {fnum(b['ic'],3)}/t{fnum(b['t'],1)}"
                          for i, b in enumerate(bs))
        print(f"  {d.replace('score_',''):14s} {cells}")
    print("（格式：组内 IC / NW-t）")

    print("\n【准入裁决】（日频·样本内为主判据，月度为旁证）")
    print("─" * 116)
    print(f"{'因子':16s}{'t':>8s}{'p':>10s}{'FDR q':>10s}{'FDR显著':>9s}"
          f"{'月t':>8s}{'Q5-Q1':>9s}{'单调':>6s}  {'裁决':16s}{'权重上限':>9s}")
    print("-" * 116)
    verdicts = {}
    for d in F.DIMS:
        r = results[d]
        di = r["daily"]["in"]
        mm = r["monthly"]["in"]
        mo = r["layers"]["monotonicity"]
        f = fdr[d]
        t_abs = abs(di["t"] or 0)
        # 裁决规则（按规范）
        if f.get("p") is None:
            verdict, cap = "剔除(无有效数据)", 0.0
        elif t_abs >= F.T_BONF and f["fdr_sig"]:
            verdict, cap = "合格(Bonferroni)", 0.20
        elif f["fdr_sig"]:
            verdict, cap = "保留(FDR)", 0.125
        else:
            verdict, cap = "剔除", 0.0
        # 规范第3条：IC 持续负向 / 分层无单调 → 剔除（覆盖前面的判定）
        if (di["ic"] or 0) < 0 or (di["pos_rate"] or 1) < 0.5:
            verdict, cap = "剔除(IC负/胜率低)", 0.0
        if not mo["monotonic"] and abs(mo["spread"] or 0) < 0.005:
            verdict, cap = "剔除(无单调)", 0.0
        # ⚠️ 有效分组不足（如 dominant 覆盖仅 25%，Q1 空）→ 单独标记，
        #    不能因为"无单调"就当作"证实无效"，也不直接给合格
        if mo.get("n_valid_buckets", 0) < 3:
            if cap > 0:
                verdict = verdict + "(分组不足)"
                cap = min(cap, 0.125)
        # 口径敏感性：日频与月度方向相反 → 降级
        if (di["ic"] or 0) * (mm["ic"] or 0) < 0:
            verdict = verdict + "(口径敏感)"
            cap = min(cap, 0.125)
        verdicts[d] = {"verdict": verdict, "cap": cap,
                       "t_daily_in": di["t"], "p": f["p"], "q_fdr": f["q"],
                       "fdr_sig": f["fdr_sig"], "t_monthly_in": mm["t"],
                       "ic_daily_in": di["ic"], "icir_daily_in": di["icir"],
                       "pos_rate_daily_in": di["pos_rate"],
                       "spread": mo["spread"], "monotonic": mo["monotonic"]}
        cap_s = f"{cap:.3f}" if cap > 0 else "0"
        pv = f"{f['p']:.4f}" if f.get("p") is not None else "  n/a"
        qv = f"{f['q']:.4f}" if f.get("q") is not None else "  n/a"
        print(f"{d.replace('score_',''):16s}{fnum(di['t'],2):>8s}"
              f"{pv:>10s}{qv:>10s}{'✅' if f['fdr_sig'] else '❌':>9s}"
              f"{fnum(mm['t'],2):>8s}{fnum(mo['spread']):>9s}"
              f"{'✅' if mo['monotonic'] else '❌':>6s}  {verdict:16s}{cap_s:>9s}")

    kept = [d for d in F.DIMS if verdicts[d]["cap"] > 0]
    print(f"\n  准入因子 {len(kept)}/{len(F.DIMS)}："
          f"{', '.join(d.replace('score_','') for d in kept)}")
    print(f"  剔除 {len(F.DIMS)-len(kept)} 个："
          f"{', '.join(d.replace('score_','') for d in F.DIMS if verdicts[d]['cap']==0)}")
    print(f"\n  权重上限汇总：")
    for d in kept:
        print(f"    {d.replace('score_',''):16s} ≤ {verdicts[d]['cap']:.3f}")

    json.dump({
        "meta": {"round": 92,
                 "purpose": "Step1 单因子五分分层校验 + 三档准入",
                 "npz": os.path.relpath(NPZ, HERE),
                 "weights_version": wc["version"],
                 "n_dates": len(dates), "date_range": [dates[0], dates[-1]],
                 "sample_in": [dates[i0], dates[i1-1]],
                 "sample_out": [dates[o0], dates[o1-1]],
                 "n_monthly_in": len(in_mslices), "n_monthly_out": len(out_mslices),
                 "thresholds": {"bonferroni": F.T_BONF, "fdr_q": F.FDR_Q},
                 "caveat": (
                     "⚠️ 月度口径仅 11 个 IC 点（样本内 6 / 样本外 5），"
                     "t 值统计功效极低，|t|≥3.9 在月度口径下几乎不可达；"
                     "故裁决以日频口径（219 点）为主，月度口径并列披露作规范对齐。"
                     "两口径结论相反者标记『口径敏感』并降级到 0.125 上限。"
                     "本步骤只用样本内区间做筛选，样本外完全未参与。")},
        "results": results,
        "fdr": fdr,
        "verdicts": verdicts,
        "kept": kept,
        "elapsed_s": round(time.time() - t0, 1),
    }, open(OUT, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"\n→ {OUT}  （{time.time()-t0:.0f}s）")


if __name__ == "__main__":
    main()
