#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
round107d 确认层 V2 修复预览（2026-11-02）
============================================
背景：老大核实指出 RA·G2 前三（西藏珠峰/永兴材料/方盛制药）都在创新低/贴地阴跌，
并无企稳迹象，但 V1 量价确认给了 0.8「价稳量增」——算法缺陷实锤。

V1 三处病根（已核实）：
  ①「价稳」用 5 日净涨幅 |mom5|<=3 判定，看不见路径（方盛 10-29 单日 -6% 被净额抹平）
  ②没有「创新低/贴地」否决（收盘趴在 20 日区间底部 19%/22% 也给 0.8）
  ③边界悬崖（mom5=-3.0 进「价稳」格、-3.1 进「阴跌」档）+ 放量下跌误读为「资金回流」

V2 修复（确认层单点改造，权重/耦合不动）：
  硬否决（V 直接 0.10，一票否决「未企稳」）：
    a. 今日最低 = 20 日最低（今天还在创低）
    b. 近 5 日 >=2 天刷新此前 20 日低（连创低）
    c. 5 日内创 60 日新低 且 收盘仍在 MA5 之下（破位未收复）
    d. pos20 < 25%（贴地：收盘趴在 20 日区间底部）
  软否决（V <= 0.40）：
    e. 收盘 < MA5（短线未转强）或 近 3 日收盘连降（路径向下）
  通过后才进量价阶梯（修边界：上下档用严格不等号连续化，中档 0.5 起步不再白给 0.6/0.8）
  K 线缺失票：V = 0.25（无法核实企稳 → 不给确认，禁臆造）

数据口径（本轮钉死，禁再读反）：
  data/klines_long.json = {code6: {date: [open, close, high, low, volume]}}
  （来源 fetch_klines_long.py L249: m = {d: [o, cl, hi, lo, v] ...}）

⚠️ 反事实预览，不写回生产文件。结果存 data/round107d_confirm_v2_preview.json
"""
import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))
SCORED = os.path.join(HERE, "top200_scored_2026-10-30.json")
KLONG = os.path.join(HERE, "data", "klines_long.json")
OUT = os.path.join(HERE, "data", "round107d_confirm_v2_preview.json")

REV_DIMS = ["score_tech_neg", "score_rsi_rev", "score_bias60"]
ALL_DIMS = ["score_fund", "score_quality", "score_ep", "score_size_growth",
            "score_dominant", "score_tech_neg", "score_rsi_rev", "score_bias60",
            "score_growth"]
W10 = {"score_fund": 0.20, "score_quality": 0.10, "score_ep": 0.10,
       "score_size_growth": 0.20, "score_dominant": 0.10,
       "score_tech_neg": 0.10, "score_rsi_rev": 0.10,
       "score_bias60": 0.05, "score_growth": 0.05}


def norm_of(raw, dmax):
    if isinstance(raw, (int, float)) and raw == raw:
        return min(raw, dmax) / dmax * 10.0
    return 5.0


def ma_confirm(tech):
    """M 均线确认 0~1：与 round107b 完全同口径（单一来源原则下调用）。"""
    bl = tech.get("bull_level")
    trend = tech.get("trend") or ""
    price = tech.get("price") or tech.get("_price")
    ma60 = tech.get("ma60")
    if bl == 3:
        m = 1.0
    elif trend == "金叉":
        m = 0.75
    elif bl == 2:
        m = 0.85
    elif bl == 1:
        m = 0.7
    elif trend == "偏多":
        m = 0.5
    elif trend == "缠绕":
        m = 0.3
    elif trend in ("偏空", "死叉"):
        m = 0.15
    elif trend in ("日线空头", "周线空头", "双线空头"):
        m = 0.0
    else:
        m = 0.3
    if isinstance(ma60, (int, float)) and isinstance(price, (int, float)) and ma60 > 0:
        if price > ma60:
            m = min(1.0, m + 0.1)
    return m


def load_kline_ctx():
    """code6 -> dict(closes, lows, highs, vols)，升序。缺失返回 None。"""
    kl = json.load(open(KLONG, encoding="utf-8"))
    out = {}
    for code, m in kl.items():
        ds = sorted(m.keys())
        if len(ds) < 25:
            continue
        out[code] = {
            "closes": [m[d][1] for d in ds],
            "highs": [m[d][2] for d in ds],
            "lows": [m[d][3] for d in ds],
            "vols": [m[d][4] for d in ds],
        }
    return out


def vol_confirm_v2(tech, kx):
    """V2 量价确认 0~1：创新低/贴地/破 MA5 否决 + 路径感知。
    返回 (V, tag)。"""
    if not kx:
        # 缺K线：无法核实企稳 → 禁给确认；量价方向仍从 tech 字段保守判断
        m5x = tech.get("mom5"); amtx = tech.get("amount_ratio")
        if isinstance(m5x, (int, float)) and isinstance(amtx, (int, float)):
            if m5x <= -3.0 and amtx >= 1.2:
                return 0.0, "缺K线:放量下跌"
            if m5x < 0:
                return 0.15, "缺K线:回落"
        return 0.25, "缺K线保守"
    closes, lows, highs, vols = kx["closes"], kx["lows"], kx["highs"], kx["vols"]
    c = closes[-1]
    low20 = min(lows[-20:]); high20 = max(highs[-20:])
    pos20 = (c - low20) / max(1e-9, high20 - low20)
    ma5 = sum(closes[-5:]) / 5
    # a. 今日最低 = 20 日最低
    new_low_today = lows[-1] <= min(lows[-21:-1]) + 1e-9
    # b. 近 5 日刷新此前 20 日低的天数
    nl = 0
    for i in range(len(closes) - 5, len(closes)):
        prior = min(lows[max(0, i - 20):i]) if i >= 1 else lows[i]
        if lows[i] <= prior + 1e-9:
            nl += 1
    # c. 5 日内创 60 日新低
    prior60 = min(lows[-65:-5]) if len(lows) >= 65 else min(lows[:-5])
    new_low_5d = min(lows[-5:]) <= prior60 + 1e-9
    # e. 路径：近 3 日收盘连降
    path_down = len(closes) >= 3 and closes[-1] < closes[-2] < closes[-3]
    m5 = tech.get("mom5")
    amt = tech.get("amount_ratio")

    # ---- 硬否决 ----
    if new_low_today:
        return 0.10, "硬否决:今日创20日新低"
    if nl >= 2:
        return 0.10, f"硬否决:近5日{nl}天刷新低"
    if new_low_5d and c < ma5:
        return 0.10, "硬否决:5日内创60日新低且破MA5"
    if pos20 < 0.25:
        return 0.10, f"硬否决:贴地(pos20={pos20:.0%})"

    # ---- 软否决：封顶 0.40 ----
    soft = c < ma5 or path_down

    if not isinstance(m5, (int, float)) or not isinstance(amt, (int, float)):
        v = 0.35
        return (min(v, 0.40) if soft else v), "缺参中性"
    # ---- 量价阶梯（修边界：严格不等号，中档 0.5 起步）----
    if m5 >= 3.0 and amt >= 1.2:
        v = 1.0
    elif m5 >= 3.0 and amt >= 0.85:
        v = 0.8
    elif m5 > 0 and amt >= 1.0 and pos20 >= 0.5:
        v = 0.8
    elif abs(m5) < 3.0 and amt >= 1.0:
        v = 0.5
    elif abs(m5) < 3.0 and amt < 0.85:
        v = 0.45
    elif m5 <= -3.0 and amt >= 1.2:
        v = 0.0
    elif m5 < 0:
        v = 0.2
    else:
        v = 0.3
    tag = f"m5={m5},amt={amt},pos20={pos20:.0%}"
    if soft:
        return min(v, 0.40), f"软否决:{tag}"
    return v, tag


def gate(raw, mode, c, dmax):
    if mode == "G1":
        return raw * (0.4 + 0.6 * c)
    if mode == "G2":
        if c < 0.35:
            return min(raw, 0.5 * dmax)
        if c < 0.55:
            return min(raw, 0.8 * dmax)
        return raw
    return raw


def main():
    d = json.load(open(SCORED, encoding="utf-8"))
    rows = d["results"]
    dmax = d.get("dim_max") or {"score_fund": 5.0, "score_quality": 10.0,
                                "score_ep": 10.0, "score_size_growth": 10.0,
                                "score_dominant": 10.0, "score_tech_neg": 10.0,
                                "score_rsi_rev": 5.0, "score_bias60": 5.0,
                                "score_growth": 10.0}
    klctx = load_kline_ctx()

    recs = []
    for r in rows:
        code6 = (r.get("code") or "")[2:]
        tech = dict(r.get("tech") or {})
        tech["price"] = r.get("price")
        kx = klctx.get(code6)
        v2, tag = vol_confirm_v2(tech, kx)
        m = ma_confirm(tech)
        c2 = 0.5 * v2 + 0.5 * m
        # 堵 M-rescue 洞：硬否决（未企稳）时均线分不得救场，C 封顶 0.25
        if tag.startswith("硬否决"):
            c2 = min(c2, 0.25)
        recs.append({
            "name": r.get("name"), "code": r.get("code"),
            "vals": {k: r.get(k) for k in ALL_DIMS},
            "V2": round(v2, 3), "M": round(m, 3), "C2": round(c2, 3),
            "tag": tag,
            "total_now": r.get("total") or 0.0,
            "has_kl": bool(kx),
        })

    cur_order = sorted(range(len(recs)), key=lambda i: -recs[i]["total_now"])
    cur_rank = [0] * len(recs)
    for pos, i in enumerate(cur_order):
        cur_rank[i] = pos + 1

    named = ["西藏珠峰", "永兴材料", "方盛制药", "鼎胜新材", "盛弘股份",
             "生益科技", "中际旭创", "巨人网络", "敦煌种业", "圣晖集成"]

    out = {"note": "V2=创新低/贴地/破MA5否决+路径感知；口径klines=[o,c,h,l,v]",
           "named_v2": {}}
    for nm in named:
        for i, r in enumerate(recs):
            if r["name"] == nm:
                out["named_v2"][nm] = {"V2": r["V2"], "M": r["M"], "C2": r["C2"],
                                       "tag": r["tag"], "has_kl": r["has_kl"]}
                break

    print("===== 前三嫌疑票 V2 判定 =====")
    for nm in ["西藏珠峰", "永兴材料", "方盛制药"]:
        for i, r in enumerate(recs):
            if r["name"] == nm:
                print(f"{nm}: V2={r['V2']:.2f} M={r['M']:.2f} C2={r['C2']:.2f}  [{r['tag']}]")
                break

    for scope in ["RA", "RB"]:
        rev = REV_DIMS if scope == "RA" else ["score_rsi_rev", "score_bias60"]
        for mode in ["G2", "G1"]:
            totals = []
            for rec in recs:
                t = 0.0
                for k in ALL_DIMS:
                    ww = W10.get(k, 0.0)
                    if ww <= 0:
                        continue
                    raw = rec["vals"].get(k)
                    if k in rev and isinstance(raw, (int, float)) and raw == raw:
                        raw = max(0.0, min(dmax.get(k, 10.0),
                                           gate(raw, mode, rec["C2"], dmax.get(k, 10.0))))
                    t += norm_of(raw, dmax.get(k, 10.0)) * ww
                totals.append(round(t * 10.0, 2))
            order = sorted(range(len(totals)), key=lambda i: -totals[i])
            rank = [0] * len(totals)
            for pos, i in enumerate(order):
                rank[i] = pos + 1
            key = f"{scope}·{mode}·V2"
            n75 = sum(1 for t in totals if t >= 75)
            out[key] = {"n75": n75, "max": max(totals),
                        "top10": [{"rank": p + 1, "code": recs[i]["code"],
                                   "name": recs[i]["name"], "total": totals[i],
                                   "C2": recs[i]["C2"], "V2": recs[i]["V2"],
                                   "cur_rank": cur_rank[i]} for p, i in enumerate(order[:10])],
                        "named": {nm: {"now": recs[next(j for j in range(len(recs)) if recs[j]["name"] == nm)]["total_now"],
                                       "now_rank": cur_rank[next(j for j in range(len(recs)) if recs[j]["name"] == nm)],
                                       "new": totals[next(j for j in range(len(recs)) if recs[j]["name"] == nm)],
                                       "new_rank": rank[next(j for j in range(len(recs)) if recs[j]["name"] == nm)]}
                                  for nm in named}}
            print(f"\n===== {key}  >=75={n75}  max={max(totals):.2f} =====")
            print(f"{'#':>2} {'代码':<10} {'名称':<8} {'新t':>6} {'V2':>5} {'C2':>5} {'现行#':>5} {'位移':>5}")
            for t in out[key]["top10"]:
                mv = t["cur_rank"] - t["rank"]
                print(f"{t['rank']:>2} {t['code']:<10} {t['name']:<8} {t['total']:>6.2f} "
                      f"{t['V2']:>5.2f} {t['C2']:>5.2f} {t['cur_rank']:>5} {mv:>+5d}")
            if scope == "RA":
                print(f"-- {key} 关键案例 --")
                for nm in named:
                    v = out[key]["named"][nm]
                    print(f"  {nm}: 现行 {v['now']:.2f}(#{v['now_rank']}) -> {v['new']:.2f}(#{v['new_rank']})")

    # V2 判定分布（透明度）
    n_kl = sum(1 for r in recs if r["has_kl"])
    tags_hard = sum(1 for r in recs if r["tag"].startswith("硬否决"))
    tags_soft = sum(1 for r in recs if r["tag"].startswith("软否决"))
    print(f"\nV2 分布: 有K线 {n_kl}/{len(recs)}  硬否决 {tags_hard}  软否决 {tags_soft}  "
          f"缺K线保守 {sum(1 for r in recs if not r['has_kl'])}")
    out["v2_dist"] = {"has_kl": n_kl, "hard_veto": tags_hard,
                      "soft_veto": tags_soft, "no_kl": len(recs) - n_kl}

    json.dump(out, open(OUT, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"\nsaved {OUT}")


if __name__ == "__main__":
    main()
