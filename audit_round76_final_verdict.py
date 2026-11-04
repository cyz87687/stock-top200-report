#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
audit_round76_final_verdict.py —— 三大目标的最终裁决与分段稳健性检验

═══ 为什么还需要这一步 ═══

round75 的 33 组网格全部未过验收（最优 top20_h10_score_cap0.08：IR 0.32、超额MDD −21.56%）。
但「全样本 IR 0.32」这个数字本身可能仍是一个假象：
若它完全由段C（2026-06~09 跌市）贡献，那模型依然只在单一 regime 有效，
与 round71 的发现一致，不能算"样本外可用"。

故本脚本对 round75 里排名靠前的方案做**分段裁决**：
  · 把 NAV 曲线按三段切开，逐段算对 POOL_EW 的超额年化 / IR / 超额MDD
  · 判据：三段中至少 2 段 IR > 0，且最差段 IR > −0.5

同时给出三大目标的**最终达成状态表**，逐项如实回答：
  目标①  最大化组合 ICIR
  目标②  样本外超额夏普
  目标③  限制超额最大回撤

用法：python audit_round76_final_verdict.py
"""
import os
import sys
import json
import math
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from audit_round67_backtest_engine import (  # noqa
    composite_all, load_prices, load_bench, _px, _bpx, _metrics, _excess_dd,
    COST_ROUND, BENCHES)

PANEL = os.path.join(HERE, "data", "panel_round66.json")
WEIGHTS = os.path.join(HERE, "factor_weights.json")
OUT = os.path.join(HERE, "audit_round76.json")


def build_scores(panel, wmap, dim_max):
    out = {}
    for d, rows in panel["rows"].items():
        arr = []
        for c, rec in rows.items():
            s, _uw = composite_all(rec, wmap, dim_max)
            if s is not None:
                arr.append((c, s))
        arr.sort(key=lambda x: (-x[1], x[0]))
        out[d] = arr
    return out


def bt_seg(panel, scores, px, topn, hold, mode="score", w_cap=0.08):
    """跑一次回测并保留足够信息做分段。"""
    dates = panel["dates"]
    rebal = [(dates[i], dates[i + 1], i) for i in range(0, len(dates) - 1, hold)]
    periods, nav, prev_w = [], 1.0, {}
    for k, (d_sig, d_exec, i0) in enumerate(rebal):
        d_next = rebal[k + 1][1] if k + 1 < len(rebal) else None
        picks = scores.get(d_sig) or []
        sel = [(c, s) for c, s in picks if _px(px, c, d_exec) is not None][:topn]
        if len(sel) < max(5, topn // 4):
            continue
        if mode == "score":
            raw = np.array([max(1e-6, s) for _, s in sel])
            w = raw / raw.sum()
            for _ in range(50):
                over = w > w_cap
                if not over.any():
                    break
                ex = (w[over] - w_cap).sum()
                w[over] = w_cap
                free = ~over
                if free.sum() == 0:
                    break
                w[free] += ex * w[free] / w[free].sum()
            w = w / w.sum()
        else:
            w = np.ones(len(sel)) / len(sel)
        wmap = {sel[i][0]: float(w[i]) for i in range(len(sel))}
        allc = set(wmap) | set(prev_w)
        turnover = sum(abs(wmap.get(c, 0.0) - prev_w.get(c, 0.0)) for c in allc) / 2.0
        rets, wts = [], []
        for c, _s in sel:
            p0 = _px(px, c, d_exec)
            p1 = _px(px, c, d_next) if d_next else None
            if p1 is None:
                sub = px.get(c) or {}
                cand = sorted(dd for dd in sub if d_exec <= dd <= (d_next or dates[-1]))
                if cand:
                    b = sub[cand[-1]]
                    p1 = float(b[0]) if isinstance(b, (list, tuple)) else None
            if p0 and p1 and p0 > 0:
                rets.append(p1 / p0 - 1.0)
                wts.append(wmap[c])
        if not rets:
            prev_w = wmap
            continue
        ws = sum(wts)
        gross = sum(r * ww for r, ww in zip(rets, wts)) / ws if ws > 0 else 0.0
        net = gross - turnover * COST_ROUND
        nav *= (1.0 + net)
        pool = [c for c, _s in picks]
        prs = []
        for c in pool:
            p0 = _px(px, c, d_exec)
            p1 = _px(px, c, d_next) if d_next else None
            if p0 and p1 and p0 > 0:
                prs.append(p1 / p0 - 1.0)
        periods.append({"signal_date": d_sig, "exec_date": d_exec, "net": net,
                        "turnover": turnover, "nav": nav,
                        "pool_ret": (sum(prs) / len(prs)) if prs else None,
                        "n": len(rets)})
        prev_w = wmap
    return periods


def seg_stats(periods, hold):
    """按调仓期序切三段。"""
    n = len(periods)
    cuts = [(0, int(n * .40)), (int(n * .40), int(n * .70)), (int(n * .70), n)]
    ann = 244.0 / hold
    out = []
    for a, b in cuts:
        sub = periods[a:b]
        if len(sub) < 3:
            out.append(None)
            continue
        nets = [p["net"] for p in sub]
        pool = [p["pool_ret"] for p in sub]
        ex = [n_ - p_ for n_, p_ in zip(nets, pool) if p_ is not None]
        if len(ex) < 3:
            out.append(None)
            continue
        m = _metrics(nets, ann, hold)
        em = _metrics(ex, ann, hold)
        br = [0.0 if p_ is None else p_ for p_ in pool[:len(nets)]]
        mdd, navx = _excess_dd(nets, br)
        out.append({"from": sub[0]["signal_date"], "to": sub[-1]["signal_date"],
                    "n": len(sub), "pool_ann": m["ann_ret"], "pool_bench_ann": None,
                    "ex_ann": em["ann_ret"], "ir": em["sharpe"],
                    "ex_mdd": mdd, "pos_rate": em.get("win_rate")})
    return out


def main():
    print("加载 ...")
    panel = json.load(open(PANEL, encoding="utf-8"))
    px = load_prices()
    wc = json.load(open(WEIGHTS, encoding="utf-8"))
    wmap = {k: float(v) for k, v in wc["weights"].items()}
    dim_max = {k: float(v) for k, v in wc["dim_max"].items()}
    scores = build_scores(panel, wmap, dim_max)
    print(f"  {len(panel['dates'])} 截面，权重 {wc['version']}")

    cases = [
        ("top20_h10_score_cap0.08", 20, 10, "score", 0.08),
        ("top20_h10_equal", 20, 10, "equal", None),
        ("top20_h20_score_cap0.08", 20, 20, "score", 0.08),
        ("top30_h20_equal", 30, 20, "equal", None),
        ("top30_h10_score_cap0.08", 30, 10, "score", 0.08),
    ]

    print("\n" + "═" * 104)
    print("分段稳健性裁决（对 POOL_EW 的超额，三段各自独立计算）")
    print("═" * 104)
    res = {}
    for nm, topn, hold, mode, cap in cases:
        pers = bt_seg(panel, scores, px, topn, hold, mode, cap or 0.08)
        segs = seg_stats(pers, hold)
        res[nm] = {"topn": topn, "hold": hold, "mode": mode, "w_cap": cap,
                   "segments": segs, "n_periods": len(pers)}
        print(f"\n  【{nm}】{len(pers)} 期")
        print(f"    {'段':4s}{'期数':>5s}{'区间':>26s}{'组合年化':>10s}{'超额年化':>10s}"
              f"{'IR':>8s}{'超额MDD':>10s}{'胜率':>8s}")
        names = ("A", "B", "C")
        for i, s in enumerate(segs):
            if not s:
                print(f"    {names[i]:4s}  期数不足")
                continue
            print(f"    {names[i]:4s}{s['n']:>5d}{s['from']+'~'+s['to']:>26s}"
                  f"{s['pool_ann']*100:>+9.2f}%{s['ex_ann']*100:>+9.2f}%"
                  f"{s['ir']:>+8.2f}{(s['ex_mdd'] or 0)*100:>+9.2f}%"
                  f"{s['pos_rate']*100:>7.1f}%")
        ok = [s for s in segs if s and s["ir"] is not None]
        n_pos = sum(1 for s in ok if s["ir"] > 0)
        worst = min((s["ir"] for s in ok), default=None)
        verdict = ("✅ 通过" if (len(ok) == 3 and n_pos >= 2 and worst > -0.5)
                   else "❌ 未通过")
        print(f"    → 正IR段 {n_pos}/{len(ok)}  最差段IR {worst:+.2f}  {verdict}")
        res[nm]["n_pos_seg"] = n_pos
        res[nm]["worst_ir"] = worst
        res[nm]["pass"] = bool(len(ok) == 3 and n_pos >= 2 and worst > -0.5)

    # ═══ 三大目标最终状态 ═══
    r70 = json.load(open(os.path.join(HERE, "audit_round70.json"), encoding="utf-8"))
    r71 = json.load(open(os.path.join(HERE, "audit_round71.json"), encoding="utf-8"))
    r73 = json.load(open(os.path.join(HERE, "audit_round73.json"), encoding="utf-8"))
    r74 = json.load(open(os.path.join(HERE, "audit_round74.json"), encoding="utf-8"))
    r75 = json.load(open(os.path.join(HERE, "audit_round75.json"), encoding="utf-8"))

    print("\n" + "═" * 104)
    print("三大目标最终达成状态")
    print("═" * 104)

    full = r70["cases"]["FULL"]
    base_all = full["baseline"]["ALL"]
    opt_all = full["candidates"]["opt_noShrink"]["stat"]["ALL"]
    eq_all = full["candidates"]["equal"]["stat"]["ALL"]
    rob = r74["candidates"]["robust_minimax"]

    print("\n① 最大化组合 ICIR（截面信号稳定、统计显著）")
    print(f"   原权重 v4.15        全样本 ICIR {base_all['icir']:+.4f}  IC {base_all['ic']:+.4f}  "
          f"NW-t {base_all['t']:+.2f}  n={base_all['n']}")
    print(f"   全样本ICIR最大化    全样本 ICIR {opt_all['icir']:+.4f}  IC {opt_all['ic']:+.4f}  "
          f"NW-t {opt_all['t']:+.2f}   ← 段A优化")
    print(f"   等权                全样本 ICIR {eq_all['icir']:+.4f}  IC {eq_all['ic']:+.4f}  "
          f"NW-t {eq_all['t']:+.2f}")
    print(f"   稳健minimax         全样本 ICIR {rob['stat']['ALL']['icir']:+.4f}  "
          f"IC {rob['stat']['ALL']['ic']:+.4f}  NW-t {rob['stat']['ALL']['t']:+.2f}")
    print(f"\n   样本外裁决（段C，只测一次）：")
    print(f"     原权重            ICIR {full['candidates']['base']['stat']['C']['icir']:+.4f}")
    print(f"     全样本ICIR最大化  ICIR {full['candidates']['opt_noShrink']['stat']['C']['icir']:+.4f}"
          f"   ← 不如原权重")
    print(f"     等权              ICIR {full['candidates']['equal']['stat']['C']['icir']:+.4f}")
    print(f"     稳健minimax       ICIR {rob['stat']['C']['icir']:+.4f}")
    print(f"   ⇒ 目标①：全样本 ICIR 从 {base_all['icir']:+.3f} 提到 {opt_all['icir']:+.3f}（+{(opt_all['icir']-base_all['icir']):.3f}），"
          f"但**样本外反而下降** {(full['candidates']['opt_noShrink']['stat']['C']['icir'] - full['candidates']['base']['stat']['C']['icir']):+.3f}。")
    print(f"     可行解是稳健 minimax：全样本 {rob['stat']['ALL']['icir']:+.3f}、段C {rob['stat']['C']['icir']:+.3f}、"
          f"最差段从 −0.655 提到 −0.166。**无法做到全 regime 显著为正。**")

    print("\n② 样本外超额夏普（对 POOL_EW，唯一诚实基准）")
    # ⚠️ round75 的 grid_rows 是按网格顺序存的，**不是**按 IR 排序。
    #    直接取 [0] 会拿到 top20_h5 之类的尾部方案 → 必须先排序再取。
    _valid = [r for r in r75["grid_rows"] if r.get("pool_ir") is not None]
    _valid.sort(key=lambda x: -x["pool_ir"])
    best = _valid[0]
    print(f"   最优方案 {best['key']}：超额年化 {best['pool_ex_ann']*100:+.2f}%  "
          f"IR {best['pool_ir']:+.2f}")
    print(f"   验收判据 IR > 0.5 → **未达成**（最优仅 {best['pool_ir']:+.2f}）")
    print(f"   对照：对沪深300 的 IR {best['hs300_ir']:+.2f} —— 含池前视偏差，不可归因")
    print(f"   ⇒ 目标②：**未达成**。池内等权同期年化 {best['pool_bench']*100:+.2f}%，"
          f"策略 {best['ann']*100:+.2f}%，边际仅 {(best['ann']-best['pool_bench'])*100:+.2f}pp。")

    print("\n③ 限制超额最大回撤（持续跑输风险）")
    mdd_ok = [r for r in r75["grid_rows"]
              if r["pool_ex_mdd"] is not None and r["pool_ex_mdd"] > -0.10]
    mdds = [r["pool_ex_mdd"] for r in r75["grid_rows"] if r["pool_ex_mdd"] is not None]
    best_mdd_row = max(r75["grid_rows"],
                       key=lambda r: r["pool_ex_mdd"] if r["pool_ex_mdd"] is not None else -9)
    worst_mdd = min(mdds)          # 最深的回撤
    best_mdd = max(mdds)           # 最浅的回撤（最接近 0）
    print(f"   全部 33 组网格的超额MDD 区间：")
    print(f"     最深 {worst_mdd*100:+.2f}%  最浅 {best_mdd*100:+.2f}%  "
          f"中位 {np.median(mdds)*100:+.2f}%")
    print(f"   验收判据 超额MDD > −10% → 通过 {len(mdd_ok)}/33 组 → **未达成**")
    print(f"   回撤最浅方案 {best_mdd_row['key']}：{best_mdd*100:+.2f}%"
          f"（该方案池超额 {best_mdd_row['pool_ex_ann']*100:+.2f}%、IR {best_mdd_row['pool_ir']:+.2f}）")
    print(f"   ⇒ 目标③：**未达成**。降回撤的代价是超额几近归零，二者在本样本内不可兼得。")

    print("\n" + "═" * 104)
    print("根因（三条硬证据，全部可复算）")
    print("═" * 104)
    print(f"  R1  模型 IC 与市场环境强负相关：r = {r71['H1_ic_vs_market']['pearson']:+.4f}  "
          f"CI [{r71['H1_ic_vs_market']['boot_ci'][0]:+.4f}, {r71['H1_ic_vs_market']['boot_ci'][1]:+.4f}]  "
          f"R² {r71['H1_ic_vs_market']['r2']:.3f}")
    print(f"      五档单调：跌市 IC {r71['quintile'][0]['ic_mean']:+.4f} → "
          f"涨市 IC {r71['quintile'][4]['ic_mean']:+.4f}（价差 {r71['quintile'][0]['ic_mean']-r71['quintile'][4]['ic_mean']:+.4f}）")
    print(f"      {r71['H2_by_regime']['n_flip']}/9 维在涨跌市 IC 符号翻转")
    print(f"  R2  regime **不可事前利用**：修复前视后（round73），所有日线信号段C IC 价差 < 0.05")
    for k, v in r73["signals"].items():
        row = next((x for x in r73["seg_rows"] if x["signal"] == k), None)
        sp = row["seg"]["C"]["spread"] if row else None
        print(f"      {k:4s} ρ(→IC)={v['rho_ic']:+.4f}  段C价差 "
              f"{('%.4f'%sp) if sp is not None else 'n/a'}")
    print("      ⇒ 不能「看市场涨跌切权重」，只能被动承受 regime 风险")
    print(f"  R3  池前视选择偏差：全池等权同期 +29.34%，HS300 −11.46%，差 40.8pp")
    print(f"      故对指数超额 IR 2.0 在池内口径下塌到 0.2~0.3")

    print("\n" + "═" * 104)
    print("最终结论")
    print("═" * 104)
    print("  三大目标中：")
    print("   ① 组合 ICIR —— **部分达成**：全样本可提至 0.414（t 2.87），")
    print("      但这是「跨 regime 平均」意义上的稳定，样本外单段不稳定，最差段仍为负。")
    print("   ② 样本外超额夏普 —— **未达成**：对诚实基准 POOL_EW 最优 IR 0.32 < 0.5。")
    print("   ③ 超额最大回撤 —— **未达成**：33 组全部深于 −10%，最优 −15.97%。")
    print()
    print("  ⇒ 这不是优化做得不够，而是**三项之间的结构性矛盾**：")
    print("     模型在跌市有真实 alpha（IC +0.308），在涨市系统性反向（IC −0.098），")
    print("     且 regime 不可预测 ⇒ 任何固定权重都只能二选一。")
    print("     真正的改善路径在**权重层之外**：")
    print("       · 把模型定位为「跌市/震荡市选股器」，涨市降仓位（需外部择时或人工判断）")
    print("       · 或引入与现有 9 维**负相关**的新维（当前 9 维 PC1 占 28.9%，有效维数仅 6.01）")

    json.dump({
        "meta": {"round": 76,
                 "purpose": "三大目标最终裁决 + 分段稳健性",
                 "panel": os.path.relpath(PANEL, HERE),
                 "weights_version": wc["version"],
                 "n_dates": len(panel["dates"])},
        "segment_verdict": res,
        "goal_status": {
            "goal1_icir": {"status": "部分达成",
                           "base_all_icir": base_all["icir"],
                           "opt_all_icir": opt_all["icir"],
                           "robust_all_icir": rob["stat"]["ALL"]["icir"],
                           "robust_t": rob["stat"]["ALL"]["t"],
                           "oos_base_icir": full["candidates"]["base"]["stat"]["C"]["icir"],
                           "oos_opt_icir": full["candidates"]["opt_noShrink"]["stat"]["C"]["icir"],
                           "robust_worst_seg_icir": rob["min_icir"]},
            "goal2_excess_sharpe": {"status": "未达成",
                                    "best_key": best["key"], "best_ir": best["pool_ir"],
                                    "threshold": 0.5,
                                    "best_ex_ann": best["pool_ex_ann"],
                                    "pool_bench_ann": best["pool_bench"]},
            "goal3_excess_mdd": {"status": "未达成",
                                 "n_pass": len(mdd_ok), "n_total": len(r75["grid_rows"]),
                                 "threshold": -0.10,
                                 "deepest_mdd": worst_mdd,
                                 "shallowest_mdd": best_mdd,
                                 "shallowest_key": best_mdd_row["key"],
                                 "median_mdd": float(np.median(mdds))},
        },
        "root_causes": {
            "R1_regime_dependence": {
                "pearson_ic_vs_future_market": r71["H1_ic_vs_market"]["pearson"],
                "boot_ci": r71["H1_ic_vs_market"]["boot_ci"],
                "r2": r71["H1_ic_vs_market"]["r2"],
                "ic_down_market": r71["quintile"][0]["ic_mean"],
                "ic_up_market": r71["quintile"][4]["ic_mean"],
                "n_dim_flip": r71["H2_by_regime"]["n_flip"],
            },
            "R2_regime_not_forecastable": {
                "note": "round73 修复 round72 重叠窗口前视后，所有事前日线信号段C IC 价差 < 0.05",
                "signals": {k: {"rho_ic": v["rho_ic"], "ci": v["ci_ic"]}
                            for k, v in r73["signals"].items()},
            },
            "R3_pool_lookahead_bias": {
                "pool_ew_ann": 0.2934, "hs300_ann": -0.1146, "spread_pp": 40.8,
                "note": "池由 2026-10-27 的成交额/市值回溯筛出",
            },
        },
        "next_steps": [
            "定位模型为跌市/震荡市选股器，涨市降仓位（需外部择时）",
            "引入与现有 9 维负相关的新维（PC1 28.9%、有效维数 6.01/9）",
            "放弃『单一权重全 regime 最优』这一不成立的假设",
        ],
    }, open(OUT, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"\n→ {OUT}")


if __name__ == "__main__":
    main()
