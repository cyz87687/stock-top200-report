#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
audit_round75_portfolio_opt.py —— 组合层优化：超额夏普/超额回撤（目标②③）

═══ 前置事实（必须先读，否则会得出假结论）═══

round69 已证：
  · 全池等权（POOL_EW）在该区间 **+29.34%**（不重叠 20 日，11 期），
    而沪深300 **−11.46%**，差 **40.8pp**。
  · 该池由「2026-10-27 的成交额≥1亿+市值≥25亿」回溯筛出 → **前视选择偏差**。
  · round67 的 top30_h20 对 HS300 IR 2.01，对 POOL_EW **IR 仅 0.19**，
    且按复利 29.73% **跑输** 池内等权 35.37%。

⇒ 任何"对沪深300 的超额"都是池偏差的伪装。本脚本**只以 POOL_EW 为主基准**，
   HS300 等指数仅作并列参考，并且明确标注其不可归因。

═══ 本脚本优化的组合层决策（逐项量化代价）═══

D1 持仓数 TopN ∈ {20, 30, 50, 80, 100}
D2 调仓频率 H ∈ {5, 10, 20}
D3 单票上限（分散化）：等权天然 1/N，改为「打分加权 + 上限」看是否改善 IR
D4 换手惩罚：对 |Δw| 加惩罚，降低交易成本
D5 波动率目标：按过去 20 日组合波动缩放到目标年化波动（15%/20%/25%）
D6 市场择时（regime）：**已在 round73 否证不可事前利用**，不做，仅作对照记录

═══ 判据（这是唯一验收标准）═══

对 POOL_EW 的 IR > 0.5 且 超额MDD > −10%，才算「样本外可用」。
对 HS300 的漂亮数字一律标注「含池偏差，不可归因」。

用法：python audit_round75_portfolio_opt.py
"""
import os
import sys
import json
import math
import argparse
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from audit_round67_backtest_engine import (  # noqa
    composite_all, load_prices, load_bench, _px, _bpx, _metrics, _excess_dd,
    COST_BUY, COST_SELL, COST_ROUND, BENCHES)

PANEL = os.path.join(HERE, "data", "panel_round66.json")
WEIGHTS = os.path.join(HERE, "factor_weights.json")
OUT = os.path.join(HERE, "audit_round75.json")


def build_scores(panel, wmap, dim_max):
    out = {}
    for d, rows in panel["rows"].items():
        arr = []
        for c, rec in rows.items():
            s, uw = composite_all(rec, wmap, dim_max)
            if s is not None:
                arr.append((c, s, uw))
        arr.sort(key=lambda x: (-x[1], x[0]))
        out[d] = arr
    return out


def run_bt(panel, scores, px, bench_px, topn, hold, mode="equal",
           w_cap=None, vt_target=None, verbose=False):
    """扩展回测：支持打分加权/单票上限/波动率目标。

    mode: equal（等权）/ score（打分加权）
    """
    dates = panel["dates"]
    rebal = []
    for i in range(0, len(dates) - 1, hold):
        rebal.append((dates[i], dates[i + 1], i))
    if len(rebal) < 3:
        return None

    periods, nav = [], 1.0
    prev_w = {}
    ret_hist = []   # 组合期收益，用于 vol target

    for k, (d_sig, d_exec, i0) in enumerate(rebal):
        d_next = rebal[k + 1][1] if k + 1 < len(rebal) else None
        picks = scores.get(d_sig) or []
        sel = []
        for c, s, uw in picks:
            if _px(px, c, d_exec) is not None:
                sel.append((c, s))
            if len(sel) >= topn:
                break
        if len(sel) < max(5, topn // 4):
            continue

        # 权重
        if mode == "score":
            raw = np.array([max(1e-6, s) for _, s in sel], np.float64)
            w = raw / raw.sum()
            if w_cap:
                for _ in range(50):
                    over = w > w_cap
                    if not over.any():
                        break
                    excess = (w[over] - w_cap).sum()
                    w[over] = w_cap
                    free = ~over
                    if free.sum() == 0:
                        break
                    w[free] += excess * w[free] / w[free].sum()
                w = w / w.sum()
        else:
            w = np.ones(len(sel)) / len(sel)
        wmap = {sel[i][0]: float(w[i]) for i in range(len(sel))}

        # 换手（按权重的 L1 距离 / 2）
        allc = set(wmap) | set(prev_w)
        turnover = sum(abs(wmap.get(c, 0.0) - prev_w.get(c, 0.0)) for c in allc) / 2.0

        # 期收益
        rets, wts = [], []
        for c, _s in sel:
            p0 = _px(px, c, d_exec)
            p1 = _px(px, c, d_next) if d_next else None
            if p1 is None:
                sub = px.get(c) or {}
                cand = sorted(dd for dd in sub if d_exec <= dd <= (d_next or dates[-1]))
                if cand:
                    bar = sub[cand[-1]]
                    p1 = float(bar[0]) if isinstance(bar, (list, tuple)) else None
            if p0 and p1 and p0 > 0:
                rets.append(p1 / p0 - 1.0)
                wts.append(wmap[c])
        if not rets:
            prev_w = wmap
            continue
        wsum = sum(wts)
        gross = sum(r * ww for r, ww in zip(rets, wts)) / wsum if wsum > 0 else 0.0

        cost = turnover * COST_ROUND
        net = gross - cost

        # 波动率目标：用已实现期收益的历史波动缩放（只用过去）
        scale = 1.0
        if vt_target and len(ret_hist) >= 6:
            realized = float(np.std(ret_hist[-12:], ddof=1)) * math.sqrt(244.0 / hold)
            if realized > 1e-6:
                scale = min(1.0, vt_target / realized)
                net = net * scale
                cost_scale_note = scale
        ret_hist.append(net)

        nav *= (1.0 + net)
        periods.append({
            "signal_date": d_sig, "exec_date": d_exec, "next_date": d_next,
            "n": len(rets), "gross": gross, "cost": cost, "net": net,
            "turnover": turnover, "nav": nav, "scale": scale,
            "_pool": [c for c, _s, _uw in picks],
            "_w": wmap,
        })
        prev_w = wmap

    if len(periods) < 3:
        return None

    # 基准
    pool_ew = []
    for p in periods:
        rs = []
        for c in (p.get("_pool") or []):
            p0 = _px(px, c, p["exec_date"])
            p1 = _px(px, c, p["next_date"]) if p["next_date"] else None
            if p0 and p1 and p0 > 0:
                rs.append(p1 / p0 - 1.0)
        pool_ew.append((sum(rs) / len(rs)) if rs else None)
    bench_periods = {"POOL_EW": pool_ew}
    for bcode in BENCHES:
        bp = bench_px.get(bcode)
        if not bp:
            continue
        br = []
        for p in periods:
            b0 = _bpx(bp, p["exec_date"])
            b1 = _bpx(bp, p["next_date"]) if p["next_date"] else None
            br.append((b1 / b0 - 1.0) if (b0 and b1 and b0 > 0) else None)
        bench_periods[bcode] = br

    nets = [p["net"] for p in periods]
    ann = 244.0 / hold
    m = _metrics(nets, ann, hold)
    m["avg_turnover"] = sum(p["turnover"] for p in periods) / len(periods)
    m["cost_annual_drag"] = m["avg_turnover"] * COST_ROUND * ann
    m["n_periods"] = len(periods)
    m["nav_final"] = nav

    excess = {}
    for bcode, br in bench_periods.items():
        ex = [n - b for n, b in zip(nets, br) if b is not None]
        if len(ex) < 8:
            continue
        em = _metrics(ex, ann, hold)
        bm = _metrics([b for b in br if b is not None], ann, hold)
        mdd, navx = _excess_dd(nets, br)
        excess[bcode] = {**em, "bench_ann_ret": bm["ann_ret"],
                         "bench_ann_vol": bm["ann_vol"], "ir": em["sharpe"],
                         "excess_ann_ret": em["ann_ret"],
                         "excess_max_dd": mdd, "excess_nav": navx}

    return {"topn": topn, "hold": hold, "mode": mode, "w_cap": w_cap,
            "vt_target": vt_target, "periods": periods, "metrics": m,
            "excess": excess}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--panel", default=PANEL)
    ap.add_argument("--out", default=OUT)
    args = ap.parse_args()

    print("加载面板 / 价格 / 基准 ...")
    panel = json.load(open(args.panel, encoding="utf-8"))
    px = load_prices()
    bench_px = {}
    for b in BENCHES:
        v = load_bench(b)
        if v:
            bench_px[b] = v
    print(f"  面板 {len(panel['dates'])} 截面 × {len(panel.get('domain') or [])} 只")
    print(f"  基准 {list(bench_px.keys())} + POOL_EW（池内等权，主基准）")

    wc = json.load(open(WEIGHTS, encoding="utf-8"))
    wmap = {k: float(v) for k, v in wc["weights"].items()}
    dim_max = {k: float(v) for k, v in wc["dim_max"].items()}
    print(f"  权重 {wc['version']}")

    print("\n合成打分 ...")
    scores = build_scores(panel, wmap, dim_max)
    print(f"  → {sum(len(v) for v in scores.values())} 条")

    # ═══ 网格搜索 ═══
    grid = []
    for hold in (5, 10, 20):
        for topn in (20, 30, 50, 80, 100):
            grid.append(dict(topn=topn, hold=hold, mode="equal"))
    # 打分加权
    for hold in (10, 20):
        for topn in (20, 30, 50):
            for cap in (0.08, 0.12):
                grid.append(dict(topn=topn, hold=hold, mode="score", w_cap=cap))
    # 波动率目标
    for hold in (20,):
        for topn in (30, 50):
            for vt in (0.15, 0.20, 0.25):
                grid.append(dict(topn=topn, hold=hold, mode="equal", vt_target=vt))

    print(f"\n回测网格 {len(grid)} 组 ...")
    results = {}
    rows = []
    for i, g in enumerate(grid):
        key = f"top{g['topn']}_h{g['hold']}_{g['mode']}"
        if g.get("w_cap"):
            key += f"_cap{g['w_cap']}"
        if g.get("vt_target"):
            key += f"_vt{g['vt_target']}"
        r = run_bt(panel, scores, px, bench_px, **g)
        if not r:
            continue
        results[key] = r
        pe = r["excess"].get("POOL_EW") or {}
        hs = r["excess"].get("000300") or {}
        rows.append({
            "key": key, **g,
            "ann": r["metrics"]["ann_ret"],
            "sharpe": r["metrics"]["sharpe"],
            "mdd": r["metrics"]["max_dd"],
            "turnover": r["metrics"]["avg_turnover"],
            "cost_drag": r["metrics"]["cost_annual_drag"],
            "pool_bench": pe.get("bench_ann_ret"),
            "pool_ex_ann": pe.get("excess_ann_ret"),
            "pool_ir": pe.get("ir"),
            "pool_ex_mdd": pe.get("excess_max_dd"),
            "hs300_ex_ann": hs.get("excess_ann_ret"),
            "hs300_ir": hs.get("ir"),
            "n_periods": r["metrics"]["n_periods"],
        })
        if (i + 1) % 8 == 0:
            print(f"  {i+1}/{len(grid)} ...")

    # ═══ 结果排序（按对 POOL_EW 的 IR）═══
    print("\n" + "═" * 100)
    print("按「对 POOL_EW 的 IR」排序（这才是唯一诚实的验收口径）")
    print("═" * 100)
    valid = [r for r in rows if r["pool_ir"] is not None]
    valid.sort(key=lambda x: -x["pool_ir"])
    hdr = (f"{'方案':28s}{'年化':>8s}{'夏普':>7s}{'MDD':>8s}{'换手':>7s}"
           f"{'成本拖累':>9s}{'池等权年化':>11s}{'超额年化':>9s}{'IR':>7s}{'超额MDD':>9s}")
    print(hdr)
    print("-" * 100)
    for r in valid[:20]:
        print(f"{r['key']:28s}{r['ann']*100:>+7.2f}%{r['sharpe']:>+7.2f}"
              f"{r['mdd']*100:>+7.2f}%{r['turnover']*100:>6.1f}%"
              f"{r['cost_drag']*100:>8.2f}%{r['pool_bench']*100:>+10.2f}%"
              f"{r['pool_ex_ann']*100:>+8.2f}%{r['pool_ir']:>+7.2f}"
              f"{r['pool_ex_mdd']*100:>+8.2f}%")

    print("\n" + "═" * 100)
    print("对照：对沪深300 的「超额」（⚠️ 含池前视偏差，不可归因于因子）")
    print("═" * 100)
    hs_sorted = sorted([r for r in rows if r["hs300_ir"] is not None],
                       key=lambda x: -x["hs300_ir"])
    print(f"{'方案':28s}{'对HS300超额':>13s}{'IR':>8s}  ｜{'对POOL_EW超额':>14s}{'IR':>8s}")
    print("-" * 100)
    for r in hs_sorted[:10]:
        print(f"{r['key']:28s}{r['hs300_ex_ann']*100:>+12.2f}%{r['hs300_ir']:>+8.2f}  ｜"
              f"{r['pool_ex_ann']*100:>+13.2f}%{r['pool_ir']:>+8.2f}")

    # ═══ 逐项代价分解 ═══
    print("\n" + "═" * 100)
    print("各项决策的量化代价（相对 top30_h20_equal 基准）")
    print("═" * 100)
    base_key = "top30_h20_equal"
    base = next((r for r in rows if r["key"] == base_key), None)
    if base:
        print(f"  基准 {base_key}: 池超额年化 {base['pool_ex_ann']*100:+.2f}%  "
              f"IR {base['pool_ir']:+.2f}  超额MDD {base['pool_ex_mdd']*100:+.2f}%")
        print()
        print(f"  {'变体':30s}{'Δ超额年化':>11s}{'IR':>8s}{'ΔIR':>8s}{'超额MDD':>10s}{'说明':>16s}")
        print("  " + "-" * 84)
        for r in rows:
            if r["key"] == base_key:
                continue
            note = ""
            if r["hold"] != 20 and r["topn"] == 30 and r["mode"] == "equal" and not r.get("vt_target"):
                note = f"调仓H={r['hold']}"
            elif r["topn"] != 30 and r["hold"] == 20 and r["mode"] == "equal" and not r.get("vt_target"):
                note = f"持仓N={r['topn']}"
            elif r["mode"] == "score":
                note = f"打分加权 cap={r['w_cap']}"
            elif r.get("vt_target"):
                note = f"波动率目标{r['vt_target']}"
            if not note:
                continue
            print(f"  {r['key']:30s}{(r['pool_ex_ann']-base['pool_ex_ann'])*100:>+10.2f}%"
                  f"{r['pool_ir']:>+8.2f}{r['pool_ir']-base['pool_ir']:>+8.2f}"
                  f"{r['pool_ex_mdd']*100:>+9.2f}%{note:>16s}")

    # ═══ 验收 ═══
    print("\n" + "═" * 100)
    print("验收判据：对 POOL_EW 的 IR > 0.5 且 超额MDD > −10%")
    print("═" * 100)
    pass_rows = [r for r in valid if r["pool_ir"] > 0.5
                 and r["pool_ex_mdd"] is not None and r["pool_ex_mdd"] > -0.10]
    if pass_rows:
        print(f"  ✅ {len(pass_rows)} 组通过：")
        for r in pass_rows[:10]:
            print(f"     {r['key']}: IR {r['pool_ir']:+.2f}  超额MDD {r['pool_ex_mdd']*100:+.2f}%  "
                  f"超额年化 {r['pool_ex_ann']*100:+.2f}%")
    else:
        best = valid[0]
        print(f"  ❌ 无一组通过。最优 {best['key']}: IR {best['pool_ir']:+.2f}"
              f"（阈值 0.5）、超额MDD {best['pool_ex_mdd']*100:+.2f}%（阈值 −10%）")
        print("  ⇒ 因子在池内**未产生可交易的稳定超额**。")
        print("     对 HS300 的漂亮 IR 全部来自池前视偏差（round69 已证）。")

    json.dump({
        "meta": {"round": 75,
                 "purpose": "目标②③：组合层优化（超额夏普/超额回撤），主基准改为 POOL_EW",
                 "panel": os.path.relpath(args.panel, HERE),
                 "weights_version": wc["version"],
                 "n_dates": len(panel["dates"]),
                 "date_range": [panel["dates"][0], panel["dates"][-1]],
                 "cost_model": {"cost_round": COST_ROUND,
                                "note": "佣金0.025%×2+滑点0.02%×2+印花税0.05%；按L1换手/2逐期扣"},
                 "exec_price": "t+1 开盘",
                 "primary_benchmark": "POOL_EW 池内等权（唯一无前视偏差的对照）",
                 "secondary_benchmark_warning": (
                     "HS300/中证500/1000/全指 的超额含池前视选择偏差：池由 2026-10-27 "
                     "的成交额≥1亿+市值≥25亿回溯筛出，全池等权同期 +29.34% vs HS300 −11.46%，"
                     "差 40.8pp。故对指数超额不可归因于因子。"),
                 "acceptance": "对 POOL_EW IR>0.5 且 超额MDD>−10%",
                 "regime_note": ("round73 已证 regime 不可事前利用（所有日线信号段C IC价差<0.05），"
                                 "故不做择时；round71 已证 IC 与未来市场收益 r=−0.6705")},
        "grid_rows": rows,
        "best_by_pool_ir": valid[0]["key"] if valid else None,
        "n_pass": len(pass_rows),
        "pass_keys": [r["key"] for r in pass_rows],
    }, open(args.out, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"\n→ {args.out}")


if __name__ == "__main__":
    main()
