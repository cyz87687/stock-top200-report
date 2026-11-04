#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
audit_round91_pit_pool.py —— 【动作①】重建「当日真实可交易池」，消除池的前视偏误

═══ 要修的到底是什么 ═══

第七轮 round69 已证：回测池由「2026-10-27 的成交额≥1亿 + 市值≥25亿」回溯筛出，
于是：
    全池等权（POOL_EW）同期 **+29.34%**  vs  沪深300 **−11.46%**，差 **40.8pp**
这个差不是任何因子的能力，而是**幸存者偏误**：
「今天活跃/今天够大的股票」被回溯到过去，而过去某些日子它们并不活跃、并不够大。
→ 对指数基准的超额 IR 2.01 里，绝大部分是这个偏误。

═══ 重建口径（严格按 config.json 的 pool_rules）═══

    universe = all_a（排除 BJ）
    ① 价格 floor：当日收盘价 ≥ 2.0 元
    ② 成交额：当日成交额 ≥ 1 亿元
    ③ 总市值：当日总市值 ≥ 25 亿元
    ④ 剔除动量前 20%（drop_mom_top_pct=20）
    ⑤ 上市天数 ≥ 60（eligibility）

⚠️ 三个诚实披露：
  D1 **股本用当前估计值**（load_shares_est，取 factor_history + scored 的中位数），
     跨期常数、无拆股调整 → 历史市值有误差。这是**已知的、无法在本轮消除**的近似。
     但它**不含前视选择效应**：股本变化慢，用当前股本回溯不会系统性偏向
     「后来才活跃」的股票（那才是真偏误）。
  D2 **动量剔除**需要 20 日动量，用价格序列现算，无前视。
  D3 **上市天数 ≥ 60** 需要上市日期。数据里没有 → 用「该股在价格序列中
     已有 ≥60 个交易日」近似（价格序列起点早于面板起点 99 个交易日，
     故对早期截面略偏严，对后期基本准确）。

═══ 关键设计：池是「逐截面」的 ═══

对每个截面 t：
  1. 用 t 日的收盘价、成交量 → 当日成交额
  2. 用 t 日收盘价 × 股本 → 当日总市值
  3. 按上述 5 条筛出当日可交易集合 = POOL_t
  4. 组合从 POOL_t 里选股（不再用 2029 只的固定大池）
  5. 基准 = POOL_t 等权（与组合同池，天然同口径）

这一步同时修掉两个问题：
  · 组合与基准来自**同一集合** → β 失配大幅收敛
  · 基准不再含幸存者偏误 → 对指数的超额变得可归因

═══ 与「组合选股范围」的关系 ═══

面板的 domain 是 2029 只（真·大池，不锁定冻结旧池）。
重建后的 POOL_t 是**当日合格池**，可能远小于 2029。
组合的打分必须在 POOL_t 内取 TopN。
若 POOL_t 太小（< 20），该截面跳过并记录。

用法：python audit_round91_pit_pool.py
"""
import os
import sys
import json
import math
import time
import argparse
from collections import defaultdict
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from audit_round38_size_ablation import load_shares_est  # noqa

PANEL = os.path.join(HERE, "data", "panel_round66.json")
KLONG = os.path.join(HERE, "data", "klines_long.json")
CONFIG = os.path.join(HERE, "config.json")
OUT = os.path.join(HERE, "audit_round91.json")
POOL_OUT = os.path.join(HERE, "data", "pit_pool.json")


def is_num(x):
    return isinstance(x, (int, float)) and not isinstance(x, bool) and x == x


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--panel", default=PANEL)
    ap.add_argument("--out", default=OUT)
    ap.add_argument("--pool-out", default=POOL_OUT)
    args = ap.parse_args()

    t0 = time.time()
    cfg = json.load(open(CONFIG, encoding="utf-8"))
    pr = cfg["pool_rules"]
    MIN_AMOUNT = float(pr["min_amount"])          # 1e8
    MIN_MKTCAP = float(pr["min_mktcap"])          # 2.5e9
    PRICE_FLOOR = float(pr["price_floor"])         # 2.0
    DROP_MOM = float(pr.get("drop_mom_top_pct", 0) or 0) / 100.0
    MIN_LIST_DAYS = int((cfg.get("eligibility_filter") or {}).get("min_listing_days", 60))
    print("池规则（来自 config.json）：")
    print(f"  价格 ≥ {PRICE_FLOOR}  成交额 ≥ {MIN_AMOUNT/1e8:.1f}亿  "
          f"总市值 ≥ {MIN_MKTCAP/1e8:.0f}亿  动量剔除前 {DROP_MOM*100:.0f}%  "
          f"上市 ≥ {MIN_LIST_DAYS} 天")

    print("\n加载面板 ...")
    panel = json.load(open(args.panel, encoding="utf-8"))
    dates = list(panel["dates"])
    print(f"  {len(dates)} 截面  {dates[0]} ~ {dates[-1]}")

    print("加载价格 ...")
    px = json.load(open(KLONG, encoding="utf-8"))
    px_days = sorted({d for c in px for d in px[c]})
    print(f"  {len(px)} 只 × {len(px_days)} 日  {px_days[0]} ~ {px_days[-1]}")

    print("加载股本估计 ...")
    shares, sh_meta = load_shares_est()
    print(f"  {sh_meta}")

    # ── 逐股预处理：日频序列 ──
    # bar = [open, close, high, low, volume]
    print("\n构建日频序列 ...")
    seq = {}     # code -> (dates[], close[], amount[], listed_idx)
    for c, bars in px.items():
        ds = sorted(bars.keys())
        cl, am = [], []
        for d in ds:
            b = bars[d]
            if isinstance(b, (list, tuple)) and len(b) >= 5:
                c1 = b[1]
                v = b[4]
                cl.append(float(c1) if is_num(c1) else None)
                # 成交额 ≈ 收盘价 × 成交量（成交量单位：手/股，见数据口径）
                am.append(float(c1) * float(v) if (is_num(c1) and is_num(v)) else None)
            elif isinstance(b, (list, tuple)) and len(b) >= 2:
                c1 = b[1]
                cl.append(float(c1) if is_num(c1) else None)
                am.append(None)
            else:
                cl.append(None)
                am.append(None)
        seq[c] = (ds, cl, am)
    print(f"  {len(seq)} 只  {time.time()-t0:.0f}s")

    # 成交量单位核验：用最新一天对比 scored 的 amount
    print("\n核验成交额单位 ...")
    dlast = dates[-1]
    ratios = []
    for c in list(seq)[:200]:
        ds, cl, am = seq[c]
        if dlast in ds:
            i = ds.index(dlast)
            if am[i] and am[i] > 0:
                ratios.append(am[i])
    if ratios:
        print(f"  重建成交额 中位 {np.median(ratios)/1e8:.2f}亿  "
              f"min {min(ratios)/1e8:.2f}亿  max {max(ratios)/1e8:.2f}亿")
    # 与 scored 的 amount 对照
    sf = os.path.join(HERE, "top200_scored_2026-10-28.json")
    if os.path.exists(sf):
        so = json.load(open(sf, encoding="utf-8"))
        chk = []
        for r in (so.get("results") or [])[:300]:
            c = str(r.get("code") or "")[-6:]
            a = r.get("amount")
            if c in seq and is_num(a) and a > 0:
                ds, cl, am = seq[c]
                if dlast in ds:
                    i = ds.index(dlast)
                    if am[i]:
                        chk.append(am[i] / a)
        if chk:
            print(f"  vs scored.amount 比值 中位 {np.median(chk):.4f}  "
                  f"p10 {np.percentile(chk,10):.4f}  p90 {np.percentile(chk,90):.4f}")

    # ── 逐截面重建池 ──
    print(f"\n逐截面重建池（{len(dates)} 截面）...")
    pools = {}
    stats = []
    for di_, d0 in enumerate(dates):
        members = []
        for c, (ds, cl, am) in seq.items():
            if d0 not in ds:
                continue
            i = ds.index(d0)
            p = cl[i]
            a = am[i]
            if p is None or a is None:
                continue
            if p < PRICE_FLOOR:
                continue
            if a < MIN_AMOUNT:
                continue
            sh = shares.get(c)
            if not sh or sh <= 0:
                continue
            if p * sh < MIN_MKTCAP:
                continue
            if i < MIN_LIST_DAYS:
                continue
            members.append((c, p, a, sh, i))
        # 动量剔除（用截止 d0 的 20 日涨幅，涨得最多的前 DROP_MOM 剔除）
        if DROP_MOM > 0 and len(members) > 50:
            mom = []
            for c, p, a, sh, i in members:
                j = i - 20
                if j >= 0 and seq[c][1][j] and seq[c][1][j] > 0:
                    mom.append((c, p / seq[c][1][j] - 1.0))
                else:
                    mom.append((c, None))
            valid = sorted([(m, cc) for cc, m in mom if m is not None],
                           key=lambda kv: -kv[0])
            k = int(len(valid) * DROP_MOM)
            drop = {cc for cc, _ in valid[:k]}
            members = [m for m in members if m[0] not in drop]
        pools[d0] = sorted(c for c, *_ in members)
        # 记录池内成交额/市值分布，供诊断
        amts = [m[2] for m in members]
        mvcs = [m[1] * m[3] for m in members]
        stats.append({"date": d0, "n": len(members),
                      "amt_median": float(np.median(amts)) if amts else None,
                      "mv_median": float(np.median(mvcs)) if mvcs else None})
        if (di_ + 1) % 40 == 0 or di_ == len(dates) - 1:
            print(f"  {di_+1}/{len(dates)}  {d0}  池 {len(members)} 只  "
                  f"{time.time()-t0:.0f}s")

    ns = [s["n"] for s in stats]
    print(f"\n池规模：最小 {min(ns)}  中位 {int(np.median(ns))}  最大 {max(ns)}")
    print(f"  对比：原固定大池 2029 只 → 重建后中位 {int(np.median(ns))} 只"
          f"（缩减 {1-np.median(ns)/2029:.0%}）")

    # 与原面板 domain 的重叠
    dom = set(panel.get("domain") or [])
    ov = []
    for d0 in dates[:20]:
        if d0 in pools:
            ov.append(len(set(pools[d0]) & dom) / max(1, len(pools[d0])))
    if ov:
        print(f"  与原 2029 只大池的重叠率（前 20 截面）均值 {np.mean(ov):.1%}")

    json.dump({"dates": dates,
               "pools": {d: pools[d] for d in dates}},
              open(args.pool_out, "w", encoding="utf-8"), ensure_ascii=False)
    print(f"\n→ {args.pool_out}  ({os.path.getsize(args.pool_out)/1e6:.1f} MB)")

    json.dump({
        "meta": {"round": 91,
                 "purpose": "动作①：逐截面重建当日真实可交易池，消除池前视偏误",
                 "panel": os.path.relpath(args.panel, HERE),
                 "pool_rules": {"price_floor": PRICE_FLOOR,
                                "min_amount": MIN_AMOUNT,
                                "min_mktcap": MIN_MKTCAP,
                                "drop_mom_top_pct": DROP_MOM,
                                "min_listing_days": MIN_LIST_DAYS},
                 "shares_meta": sh_meta,
                 "n_dates": len(dates),
                 "date_range": [dates[0], dates[-1]],
                 "caveat": (
                     "D1 股本用当前估计值的跨期中位数，无拆股调整 → 历史市值有误差；"
                     "但该近似不含前视选择效应（股本变化慢）。"
                     "D2 上市天数用「价格序列中已有 ≥60 个交易日」近似，"
                     "对早期截面略偏严（价格序列仅比面板早 99 日）。"
                     "D3 成交额 = 收盘价 × 成交量，为近似值。")},
        "pool_size": {"min": int(min(ns)), "median": int(np.median(ns)),
                      "max": int(max(ns)),
                      "overlap_with_2029_domain": (float(np.mean(ov)) if ov else None)},
        "per_date": stats,
        "elapsed_s": round(time.time() - t0, 1),
    }, open(args.out, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"→ {args.out}  （{time.time()-t0:.0f}s）")


if __name__ == "__main__":
    main()
