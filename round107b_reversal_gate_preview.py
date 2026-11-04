#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
round107b 反转簇「触发×确认」门控反事实预览（2026-11-02）
========================================================
老大裁决方向：不砍技术面因子权重，改造反转簇因子本身——
反转信号（超跌触发）必须与确定性信号（量价配合 + 多头排列）确认后才给高分。

结构（每只股票）：
  触发层 T：沿用原反转逻辑强度（由现分数反推触发档位，不改历史口径）
  确认层 C = 0.5·V + 0.5·M
    V 量价确认：amt_ratio(5日额/20日额) + mom5(5日涨幅) 联合判定
    M 均线确认：bull_level/trend 多头排列 + 价格与 MA60 关系
  耦合（三选一，供老大裁决）：
    G1 乘法门控：new = old × (0.4 + 0.6·C)
    G2 封顶门槛：C<0.35 封 2.5 / C<0.55 封 4.0 / 其余不变
    G3 加法重构：new = 0.6×old + 0.4×10C

作用对象：score_tech_neg / score_rsi_rev / score_bias60（反转簇三维）。
⚠️ 权重结构不动（维持 v4.16.1）——这是因子层改造，不是权重层。
⚠️ 反事实预览，不写回任何文件（结果存 data/round107b_reversal_gate_preview.json）。
"""
import json

SCORED = "top200_scored_2026-10-30.json"
OUT = "data/round107b_reversal_gate_preview.json"
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


def vol_confirm(tech):
    """V 量价确认 0~1：量能配合 + 价格行为联合判定。"""
    amt = tech.get("amount_ratio")
    m5 = tech.get("mom5")
    if not isinstance(amt, (int, float)) or not isinstance(m5, (int, float)):
        return 0.35  # 缺失 → 中性偏低（禁臆造满分）
    if m5 >= 3.0 and amt >= 1.2:
        return 1.0    # 放量反弹
    if m5 >= 3.0 and amt >= 0.85:
        return 0.8    # 温和放量回升
    if abs(m5) <= 3.0 and amt >= 1.0:
        return 0.8    # 价稳量增（资金回流）
    if abs(m5) <= 3.0 and amt < 0.85:
        return 0.6    # 缩量企稳（抛压衰竭）
    if 0 < m5 < 3.0:
        return 0.65   # 温和回升
    if m5 < -3.0 and amt >= 1.2:
        return 0.0    # 放量下跌 = 出货，不给确认
    if m5 < -3.0 and amt < 1.0:
        return 0.1    # 缩量阴跌 = 未企稳
    return 0.3


def ma_confirm(tech):
    """M 均线确认 0~1：多头排列等级 + MA60 位置。"""
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
    # MA60 确认加项（价格站上 MA60 = 中期趋势转强）
    if isinstance(ma60, (int, float)) and isinstance(price, (int, float)) and ma60 > 0:
        if price > ma60:
            m = min(1.0, m + 0.1)
    return m


def gate(new, mode, c, dmax=10.0):
    """耦合方式。dmax=该维量程（tech_neg 0-10 / rsi_rev·bias60 0-5），阈值按量程缩放。"""
    if mode == "G1":   # 乘法门控
        return new * (0.4 + 0.6 * c)
    if mode == "G2":   # 封顶门槛（半程/八成量程封顶）
        if c < 0.35:
            return min(new, 0.5 * dmax)
        if c < 0.55:
            return min(new, 0.8 * dmax)
        return new
    if mode == "G3":   # 加法重构：反转 60% + 确认 40%（确认分=满量程×C）
        return 0.6 * new + 0.4 * (dmax * c)
    return new


def main():
    d = json.load(open(SCORED))
    rows = d["results"]
    dmax = d.get("dim_max") or {"score_fund": 5.0, "score_quality": 10.0, "score_ep": 10.0,
                                "score_size_growth": 10.0, "score_dominant": 10.0,
                                "score_tech_neg": 10.0, "score_rsi_rev": 5.0,
                                "score_bias60": 5.0, "score_growth": 10.0}
    # 反转簇量程（0-10 制在 scored 中如何存放？现分数 tech_neg 8.21 → 0-10 制）
    # 与 round107 模拟器同口径：raw 分直接归一。

    recs = []
    for r in rows:
        tech = dict(r.get("tech") or {})
        tech["price"] = r.get("price")
        v = vol_confirm(tech)
        m = ma_confirm(tech)
        c = 0.5 * v + 0.5 * m
        recs.append({
            "name": r.get("name"), "code": r.get("code"),
            "vals": {k: r.get(k) for k in ALL_DIMS},
            "V": round(v, 3), "M": round(m, 3), "C": round(c, 3),
            "trend": tech.get("trend"), "bull_level": tech.get("bull_level"),
            "amount_ratio": tech.get("amount_ratio"), "mom5": tech.get("mom5"),
            "total_now": r.get("total") or 0.0,
        })

    cur_order = sorted(range(len(recs)), key=lambda i: -recs[i]["total_now"])
    cur_rank = [0] * len(recs)
    for pos, i in enumerate(cur_order):
        cur_rank[i] = pos + 1

    named = ["鼎胜新材", "盛弘股份", "生益科技", "中际旭创", "巨人网络", "敦煌种业", "圣晖集成"]

    def score_mode(mode):
        totals, contribs = [], []
        for rec in recs:
            t = 0.0
            cc = {}
            for k in ALL_DIMS:
                ww = W10.get(k, 0.0)
                if ww <= 0:
                    continue
                raw = rec["vals"].get(k)
                if k in REV_DIMS and isinstance(raw, (int, float)) and raw == raw:
                    raw = gate(raw, mode, rec["C"], dmax.get(k, 10.0))
                    raw = max(0.0, min(dmax.get(k, 10.0), raw))
                v = norm_of(raw, dmax.get(k, 10.0)) * ww
                cc[k] = round(v * 10.0, 2)
                t += v
            totals.append(round(t * 10.0, 2))
            contribs.append(cc)
        order = sorted(range(len(totals)), key=lambda i: -totals[i])
        rank = [0] * len(totals)
        for pos, i in enumerate(order):
            rank[i] = pos + 1
        return totals, rank, order, contribs

    # C 分布
    cs = [r["C"] for r in recs]
    cs_sorted = sorted(cs)
    print(f"C 分布: min={min(cs):.2f} p25={cs_sorted[len(cs)//4]:.2f} "
          f"median={cs_sorted[len(cs)//2]:.2f} p75={cs_sorted[3*len(cs)//4]:.2f} max={max(cs):.2f}")
    n_low = sum(1 for c in cs if c < 0.35)
    n_mid = sum(1 for c in cs if 0.35 <= c < 0.55)
    n_hi = sum(1 for c in cs if c >= 0.55)
    print(f"C<0.35(弱确认)={n_low}  0.35~0.55={n_mid}  ≥0.55(强确认)={n_hi}")

    results = {}
    for mode in ["G1", "G2", "G3"]:
        totals, rank, order, contribs = score_mode(mode)
        results[mode] = {"totals": totals, "rank": rank, "order": order, "contribs": contribs}
        print(f"\n===== {mode} 前 10 =====")
        print(f"{'#':>3} {'代码':<10} {'名称':<8} {'新total':>7} {'C':>5} {'V':>5} {'M':>5} {'现行#':>5} {'位移':>5}")
        for pos, i in enumerate(order[:10]):
            r = recs[i]
            mv = cur_rank[i] - (pos + 1)
            print(f"{pos+1:>3} {r['code']:<10} {r['name']:<8} {totals[i]:>7.2f} "
                  f"{r['C']:>5.2f} {r['V']:>5.2f} {r['M']:>5.2f} {cur_rank[i]:>5} {mv:>+5d}")
        print(f"-- {mode} 关键案例 --")
        for nm in named:
            for i, r in enumerate(recs):
                if r["name"] == nm:
                    print(f"  {nm}: 现行 {r['total_now']:.2f}(#{cur_rank[i]}) -> {totals[i]:.2f}(#{rank[i]})  "
                          f"C={r['C']:.2f}(V{r['V']:.2f}/M{r['M']:.2f}) {r['trend']}")
                    break

    # G1 前十逐维贡献
    o = results["G1"]
    short = {"score_fund": "fund", "score_quality": "qual", "score_ep": "ep",
             "score_size_growth": "szg", "score_dominant": "dom", "score_tech_neg": "tech",
             "score_rsi_rev": "rsi", "score_bias60": "bias", "score_growth": "grow"}
    print("\n===== G1 前十 逐维贡献（0-100 口径，反转簇已门控） =====")
    cols = [k for k in ALL_DIMS if W10.get(k, 0) > 0]
    hdr = " ".join(f"{short[k]:>5}" for k in cols)
    print(f"{'名称':<8} {hdr} {'total':>7}")
    for pos, i in enumerate(o["order"][:10]):
        cc = o["contribs"][i]
        vals = " ".join(f"{cc.get(k, 0.0):>5.2f}" for k in cols)
        print(f"{recs[i]['name']:<8} {vals} {o['totals'][i]:>7.2f}")

    # 鼎胜/盛弘在三种模式下的反转簇得分变化
    print("\n===== 鼎胜/盛弘 反转簇门控前后（原始分 → 门控后） =====")
    for nm in ["鼎胜新材", "盛弘股份"]:
        for i, r in enumerate(recs):
            if r["name"] == nm:
                old = {k: r["vals"].get(k) for k in REV_DIMS}
                print(f"{nm} C={r['C']:.2f}:")
                for mode in ["G1", "G2", "G3"]:
                    new = {k: round(gate(old[k], mode, r["C"], dmax.get(k, 10.0)), 2) for k in REV_DIMS}
                    print(f"  {mode}: {new}")
                break

    json.dump({"C_dist": {"min": min(cs), "median": cs_sorted[len(cs)//2], "max": max(cs),
                          "n_low": n_low, "n_mid": n_mid, "n_hi": n_hi},
               "modes": {m: {"top10": [{"rank": p + 1, "code": recs[i]["code"], "name": recs[i]["name"],
                                        "total": results[m]["totals"][i], "C": recs[i]["C"],
                                        "cur_rank": cur_rank[i]} for p, i in enumerate(results[m]["order"][:10])],
                             "named": {nm: {"now": next(recs[j]["total_now"] for j in range(len(recs)) if recs[j]["name"] == nm),
                                            "now_rank": cur_rank[next(j for j in range(len(recs)) if recs[j]["name"] == nm)],
                                            "new": next(results[m]["totals"][j] for j in range(len(recs)) if recs[j]["name"] == nm),
                                            "new_rank": results[m]["rank"][next(j for j in range(len(recs)) if recs[j]["name"] == nm)]}
                                       for nm in named}}
                          for m in results}},
              open(OUT, "w"), ensure_ascii=False, indent=1)
    print(f"\nsaved {OUT}")


if __name__ == "__main__":
    main()
