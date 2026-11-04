#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
audit_round69_backtest_bias.py —— 回测可信度的四个证伪测试

═══ 为什么必须做，而且必须先做 ═══

round67 首跑结果：
  top30_h20  年化 29.73%  夏普 1.25  超额(HS300) 49.10%  IR 2.01  超额MDD −8.39%
  top30_h10  年化 23.94%  夏普 1.26  超额 39.29%  IR 2.23

**这个数字好得不合常理。** A股任何公开多因子多头策略，样本外 IR>2 都是极强的信号；
在 238 个截面（不足一年）上拿到 IR 2.0+ 且超额回撤仅 −8%，第一反应必须是「有偏」，
而不是「alpha 找到了」。在把任何优化建立在这条净值曲线上之前，必须先证伪。

═══ 四个测试 ═══

T1 随机组合基线（最关键）
   从**同一个池**里随机抽 TopN 只，跑同样的调仓规则与成本。
   若随机组合也能拿到 20%+ 的超额，说明超额主要来自**池本身**（域偏差），
   因子只贡献了增量部分 —— 那么所有「因子有效」的结论都要打掉相应折扣。

T2 域生存者偏差
   统计每个截面在池中的股票数 vs 当期真实有价格的股票数。
   DOM = EM财务 ∩ 申万 ∩ 长价格库 ∩ 有股本，是**用 2026-10-27 的数据可得性**定的，
   再回溯套到 2025-11 —— 期间退市/被并购的股票**根本不在域内**。
   若首末截面数量几乎不变而市场实际退市数十家 → 生存者偏差成立。

T3 分时段归因
   把 238 截面按季度切开，看超额是否集中在某一两个季度。
   round54 已证 h20 IC 分段 7~8/8 维符号翻转 → 净值层也必须分段看，
   否则「全样本 IR 2.0」可能只是某一段的极端值。

T4 基准敏感性
   对 HS300 / 中证500 / 中证1000 / 中证全指 分别算超额。
   本池偏小盘，若对中证1000 的超额远小于对 HS300，说明「超额」主要是 size beta，
   而 size beta 在大小盘风格反转时会整段失效（这是超额回撤的真正来源）。

═══ 输出 ═══
audit_round69.json
"""
import os
import sys
import json
import math
import random
import argparse

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from audit_round67_backtest_engine import (
    build_scores, load_prices, load_bench, backtest, _bpx, _px,
    _metrics, _excess_dd, BENCHES, COST_ROUND,
)

PANEL = os.path.join(HERE, "data", "panel_round66.json")
WEIGHTS = os.path.join(HERE, "factor_weights.json")
OUT = os.path.join(HERE, "audit_round69.json")


def is_num(x):
    return isinstance(x, (int, float)) and not isinstance(x, bool) and x == x


def random_scores(panel, seed, universe_by_date=None):
    """同池随机打分（每个截面独立打乱）。"""
    rng = random.Random(seed)
    out = {}
    for d in panel["dates"]:
        rows = panel["rows"].get(d) or {}
        codes = list(rows.keys())
        rng.shuffle(codes)
        out[d] = [(c, 0.0, 1.0) for c in codes]
    return out


def bench_ret_periods(bp, periods):
    br = []
    for p in periods:
        b0 = _bpx(bp, p["exec_date"])
        b1 = _bpx(bp, p["next_date"]) if p["next_date"] else None
        br.append((b1 / b0 - 1.0) if (b0 and b1 and b0 > 0) else None)
    return br


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--panel", default=PANEL)
    ap.add_argument("--topn", type=int, default=30)
    ap.add_argument("--hold", type=int, default=20)
    ap.add_argument("--n-random", type=int, default=30)
    args = ap.parse_args()

    panel = json.load(open(args.panel, encoding="utf-8"))
    px = load_prices()
    bench_px = {b: load_bench(b) for b in BENCHES}
    bench_px = {k: v for k, v in bench_px.items() if v}

    wc = json.load(open(WEIGHTS, encoding="utf-8"))
    wmap = {k: float(v) for k, v in wc["weights"].items()}
    dim_max = {k: float(v) for k, v in wc["dim_max"].items()}

    dates = panel["dates"]
    print(f"面板 {len(dates)} 截面  {dates[0]} ~ {dates[-1]}")
    print(f"策略 top{args.topn} / H{args.hold} / 权重 {wc['version']}\n")

    out = {"meta": {
        "round": 69,
        "purpose": "回测可信度四证伪：随机基线 / 域生存者偏差 / 分时段 / 基准敏感性",
        "panel": os.path.relpath(args.panel, HERE),
        "topn": args.topn, "hold": args.hold,
        "n_random": args.n_random,
    }}

    # ───────── T1 随机组合基线 ─────────
    print("=" * 78)
    print("T1 随机组合基线（同池、同调仓、同成本）")
    print("=" * 78)
    scores = build_scores(panel, wmap, dim_max)
    base = backtest(panel, scores, px, bench_px, args.topn, args.hold, None, None)
    if not base:
        print("策略回测失败"); return
    bm = base["metrics"]
    print(f"  策略   年化 {bm['ann_ret']*100:7.2f}%  夏普 {_p(bm['sharpe'])}  "
          f"MDD {bm['max_dd']*100:7.2f}%")

    rand_res = []
    for s in range(args.n_random):
        rs = random_scores(panel, 10000 + s)
        r = backtest(panel, rs, px, bench_px, args.topn, args.hold, None, None)
        if r:
            rand_res.append(r["metrics"])
    if rand_res:
        ar = [m["ann_ret"] for m in rand_res if m["ann_ret"] is not None]
        ash = [m["sharpe"] for m in rand_res if m["sharpe"] is not None]
        amd = [m["max_dd"] for m in rand_res if m["max_dd"] is not None]
        ar.sort(); ash.sort(); amd.sort()
        print(f"  随机×{len(rand_res)}  年化 中位 {ar[len(ar)//2]*100:7.2f}%  "
              f"区间 [{ar[0]*100:.2f}%, {ar[-1]*100:.2f}%]")
        print(f"            夏普 中位 {ash[len(ash)//2]:.3f}  "
              f"区间 [{ash[0]:.2f}, {ash[-1]:.2f}]")
        # 随机组合相对策略的差距
        beat = sum(1 for a in ar if a >= bm["ann_ret"])
        rank_pct = 1.0 - beat / len(ar)
        print(f"  → 策略在随机分布中的分位：{rank_pct*100:.1f}%  "
              f"（{beat}/{len(ar)} 个随机组合年化不低于策略）")
        out["T1_random_baseline"] = {
            "strategy_ann_ret": bm["ann_ret"], "strategy_sharpe": bm["sharpe"],
            "random_median_ann": ar[len(ar)//2], "random_min_ann": ar[0], "random_max_ann": ar[-1],
            "random_median_sharpe": ash[len(ash)//2],
            "strategy_percentile": rank_pct,
            "n_random": len(rand_res),
            "note": ("若随机中位年化已接近策略，则超额主要来自池，不是因子。"
                     "策略分位越接近 100% 才说明因子有增量。"),
        }

    # ───────── T2 域生存者偏差 ─────────
    print("\n" + "=" * 78)
    print("T2 域生存者偏差（池是用 2026-10-27 的数据可得性定的，再回溯套用）")
    print("=" * 78)
    sizes = []
    for d in dates:
        rows = panel["rows"].get(d) or {}
        sizes.append(len(rows))
    print(f"  池内截面规模：首个 {sizes[0]}  中位 {sorted(sizes)[len(sizes)//2]}  末个 {sizes[-1]}")
    # 域内每只股票有价格的交易日数
    dom = panel.get("domain") or []
    cover = []
    for c in dom:
        n = len(px.get(c) or {})
        cover.append(n)
    cover.sort()
    full = sum(1 for n in cover if n >= 280)
    print(f"  域 {len(dom)} 只：有 ≥280 个交易日价格的 {full} 只 ({full/max(len(dom),1)*100:.1f}%)")
    print(f"  价格交易日数分布：p5={cover[len(cover)//20]}  p50={cover[len(cover)//2]}  "
          f"p95={cover[len(cover)*19//20]}")
    # 中途「消失」的股票（价格序列在样本期末之前就断了）
    last_day = dates[-1]
    still = sum(1 for c in dom if (px.get(c) or {}).get(last_day))
    print(f"  末个截面仍有价格的 {still} 只 / 域 {len(dom)} → "
          f"{len(dom)-still} 只中途失去价格（退市/停牌/被并购）")
    out["T2_universe_bias"] = {
        "domain_size": len(dom),
        "n_dates": len(dates),
        "pool_size_first": sizes[0], "pool_size_last": sizes[-1],
        "n_full_history": full,
        "n_lost_before_end": len(dom) - still,
        "price_days_p5": cover[len(cover)//20], "price_days_p50": cover[len(cover)//2],
        "caveat": ("DOM 由「当前可得的 EM 财务 + 申万行业 + 长价格库 + 股本」交集确定，"
                   "本质是**当前时点的数据可得性快照**。已退市/长期停牌股若 EM 缓存在 2026-10-27 "
                   "已被清理，则它们从 2025-11 的截面里也不存在 → 生存者偏差。"
                   "本池规模约 2000 只，占 A 股约 5400 只的 37%，缺的 63% 中包含全部已退市股。"),
    }

    # ───────── T3 分时段 ─────────
    print("\n" + "=" * 78)
    print("T3 分时段归因（超额是否集中在某一两个季度）")
    print("=" * 78)
    periods = base["periods"]
    bp300 = bench_px.get("000300")
    br = bench_ret_periods(bp300, periods)
    rows3 = []
    for i, p in enumerate(periods):
        rows3.append({
            "exec": p["exec_date"], "net": p["net"],
            "bench": br[i], "excess": (p["net"] - br[i]) if br[i] is not None else None,
        })
    # 按季度聚合（复利）
    from collections import OrderedDict
    q = OrderedDict()
    for r in rows3:
        key = r["exec"][:7]
        q.setdefault(key, []).append(r)
    print(f"  {'月份':8s} {'期数':>4s} {'策略':>9s} {'基准':>9s} {'超额':>9s}")
    qstats = {}
    for k, lst in q.items():
        pn = 1.0; bn = 1.0; en = 1.0
        for r in lst:
            pn *= (1 + r["net"])
            if r["bench"] is not None:
                bn *= (1 + r["bench"])
                en *= (1 + r["net"]) / (1 + r["bench"])
        qstats[k] = {"n": len(lst), "port": pn - 1, "bench": bn - 1, "excess": en - 1}
        print(f"  {k:8s} {len(lst):4d} {(pn-1)*100:8.2f}% {(bn-1)*100:8.2f}% {(en-1)*100:8.2f}%")
    ex_list = [v["excess"] for v in qstats.values()]
    pos_q = sum(1 for x in ex_list if x > 0)
    print(f"  → {pos_q}/{len(ex_list)} 个月份超额为正；"
          f"最好 {max(ex_list)*100:.2f}%  最差 {min(ex_list)*100:.2f}%")
    tot = 1.0
    for v in qstats.values():
        tot *= (1 + v["excess"])
    best_q = max(qstats.items(), key=lambda kv: kv[1]["excess"])
    print(f"  累计超额 {(tot-1)*100:.2f}%；最高单月 {best_q[0]} "
          f"贡献 {best_q[1]['excess']*100:.2f}%")
    out["T3_time_attribution"] = {"by_month": qstats, "n_pos_months": pos_q,
                                  "n_months": len(ex_list),
                                  "cum_excess": tot - 1,
                                  "best_month": best_q[0],
                                  "best_month_excess": best_q[1]["excess"]}

    # ───────── T4 基准敏感性 ─────────
    print("\n" + "=" * 78)
    print("T4 基准敏感性（超额是 alpha 还是 size beta）")
    print("=" * 78)
    names = {"000300": "沪深300", "000905": "中证500", "000852": "中证1000", "000985": "中证全指"}
    t4 = {}
    print(f"  {'基准':10s} {'基准年化':>10s} {'超额年化':>10s} {'IR':>7s} {'超额MDD':>10s}")
    for b, bp in bench_px.items():
        brs = bench_ret_periods(bp, periods)
        ex = [(p["net"] - x) for p, x in zip(periods, brs) if x is not None]
        if len(ex) < 10:
            continue
        ann = 244.0 / args.hold
        m = _metrics(ex, ann, args.hold)
        bm2 = _metrics([x for x in brs if x is not None], ann, args.hold)
        mdd_ex, nav_ex = _excess_dd([p["net"] for p in periods], brs)
        t4[b] = {"name": names.get(b, b), "bench_ann": bm2["ann_ret"],
                 "excess_ann": m["ann_ret"], "ir": m["sharpe"],
                 "excess_mdd": mdd_ex, "excess_nav": nav_ex}
        print(f"  {names.get(b,b):10s} {bm2['ann_ret']*100:9.2f}% "
              f"{m['ann_ret']*100:9.2f}% {_p(m['sharpe']):>7} {mdd_ex*100:9.2f}%")
    out["T4_bench_sensitivity"] = t4
    if "000300" in t4 and "000852" in t4:
        gap = t4["000300"]["excess_ann"] - t4["000852"]["excess_ann"]
        print(f"\n  → 对 HS300 超额 {t4['000300']['excess_ann']*100:.2f}% vs "
              f"对中证1000 超额 {t4['000852']['excess_ann']*100:.2f}%  "
              f"差额 {gap*100:.2f}pp")
        print(f"    差额越大，说明越多「超额」其实只是小盘暴露。")
        out["T4_size_beta_share"] = {
            "excess_vs_hs300": t4["000300"]["excess_ann"],
            "excess_vs_csi1000": t4["000852"]["excess_ann"],
            "gap": gap,
        }

    # ───────── 策略 vs 随机：对每个基准都算 ─────────
    print("\n" + "=" * 78)
    print("T1b 随机基线在各基准下的超额（判断池偏差的大小）")
    print("=" * 78)
    if rand_res:
        # 重新跑一次保存 periods 的随机组合（取 5 个代表）
        t1b = {}
        for b, bp in bench_px.items():
            b_list = [m.get("excess_ann_%s" % b) for m in rand_res]
        # 直接算：随机组合的 ann_ret 中位 − 基准 ann_ret
        for b, bp in bench_px.items():
            brs = bench_ret_periods(bp, periods)
            bm3 = _metrics([x for x in brs if x is not None], 244.0/args.hold, args.hold)
            rand_med = ar[len(ar)//2]
            t1b[b] = {"bench_ann": bm3["ann_ret"],
                      "random_median_ann": rand_med,
                      "random_excess_median": rand_med - bm3["ann_ret"]}
            print(f"  vs {names.get(b,b):10s} 基准年化 {bm3['ann_ret']*100:7.2f}%  "
                  f"随机组合年化中位 {rand_med*100:7.2f}%  "
                  f"→ 池偏差约 {(rand_med - bm3['ann_ret'])*100:7.2f}pp")
        out["T1b_random_excess"] = t1b

    json.dump(out, open(OUT, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"\n→ {OUT}")


def _p(v, nd=2):
    return "  n/a" if v is None else f"{v:.{nd}f}"


if __name__ == "__main__":
    main()
