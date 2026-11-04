#!/usr/bin/env python3
"""Round16：把「现价必须在买入区间」这个新过滤器放进历史回测，看它对组合收益的影响。

老大 2026-10-25 第 2 问：
  「多因子组合有多只现价不在买入区间，只选当日在买入区间的个股」

Round14/14b 已确认问题真实存在（10 只里 6 只高于买区；490 只候选里仅 26.7% 已回踩到位）。
但在改选股逻辑之前，必须先回答一个更基础的问题：
    **这个过滤会让组合变好还是变差？**
如果变差，那就是「执行纪律」与「收益」的权衡，必须如实披露，不能只讲纪律好听。

数据与复现口径（诚实边界）：
  历史截面（factor_history.snapshots）只有 16 个评分字段，没有 tech。
  但价格库有 143 个交易日全历史（1012 只）→ 可自行计算：
    · ma5 / ma10 / ma20（对 d0 收盘价）
    · 支撑位 = 近 20 个交易日最低价（近似 tech.support_levels 的「最近支撑」）
    · 阻力位 = 近 20 个交易日最高价
  由此**完整复现** gen_report_top200.build_trade_plan 的买区推导：
    多头(ma5>ma10 且 ma10<=price)：买区 = [ma10, min(ma5, price)]
    弱势：买区 = [支撑*0.995, min(支撑*1.02, price)]
    兜底：[price*0.95, price*0.98]
    buy_hi 永不超过 price
  ⚠️ 与线上的差异（必须披露）：
    ① 无 RSI → 无法复现 RSI>70 剔除；
    ② 无 sector_l2 → 无法复现板块约束（主口径不带）；
    ③ 支撑/阻力用近 20 日高低点近似，线上用 tech.support_levels
       （更复杂的结构位），买区边界会有差异，但「price > buy_hi 即需回踩」
       这个判定的方向一致；
    ④ 样本区间 2026-06-26 ~ 2026-10-23（79 个可用入场日），恰逢池子负 alpha 期。

对照设计（同源、同时刻、同权重，只差「过不过滤」）：
  变体 A no_filter：当前线上口径，按 mf 分直接取前 10
  变体 B in_zone  ：只保留 price <= buy_hi 的候选，再取前 10（老大要求的口径）
  两者用完全相同的入场/出场/基准/统计口径，唯一差别就是这一道闸。
"""
import json
import math
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "audit_round16.json")

REV_BLOCK = ("score_oversold60", "score_bias60", "score_rsi_rev",
             "score_vol_pulse", "score_overbought")
DROP_HIGH = {"score_attention": 0.80, "score_theme": 0.80}
TOP_N = 10
MIN_PICKS = 5
MAXH = 20
MA_WIN = 20          # 支撑/阻力近似窗口


def F(x, d=None):
    try:
        v = float(x)
        return v if v == v else d
    except (TypeError, ValueError):
        return d


def pct_rank(vals):
    idx = [(i, v) for i, v in enumerate(vals) if v is not None]
    idx.sort(key=lambda x: x[1])
    n = len(idx)
    out = {i: (0.0 if n <= 1 else k / (n - 1.0)) for k, (i, _) in enumerate(idx)}
    for i, v in enumerate(vals):
        if v is None:
            out.setdefault(i, 0.0)
    return out


def nw_t(xs, lag):
    n = len(xs)
    if n < 3:
        return None
    m = sum(xs) / n
    e = [x - m for x in xs]
    g0 = sum(v * v for v in e) / n
    if g0 <= 0:
        return None
    var = g0
    for L in range(1, min(lag, n - 1) + 1):
        w = 1.0 - L / (lag + 1.0)
        gl = sum(e[i] * e[i - L] for i in range(L, n)) / n
        var += 2.0 * w * gl
    if var <= 0:
        return None
    return m / math.sqrt(var / n)


def med(xs):
    xs = sorted(xs)
    n = len(xs)
    if not n:
        return None
    return xs[n // 2] if n % 2 else (xs[n // 2 - 1] + xs[n // 2]) / 2.0


def ma_at(series, i, w):
    if i - w + 1 < 0:
        return None
    seg = series[i - w + 1:i + 1]
    if any(v is None for v in seg):
        return None
    return sum(seg) / w


def buy_zone(price, ma5, ma10, ma20, sup):
    """复现 gen_report_top200.build_trade_plan 的买区推导（无 RSI/无 position）。

    → (buy_lo, buy_hi)
    """
    if not price or price <= 0:
        return None, None
    bullish = (ma5 is not None and ma10 is not None and ma5 > ma10)
    ref_hi = ma5 if bullish else ma10
    ref_lo = ma10 if bullish else ma20
    lo = hi = None
    if ref_hi and ref_lo:
        if ref_hi < ref_lo:
            ref_lo, ref_hi = ref_hi, ref_lo
        if ref_lo <= price:
            lo, hi = ref_lo, min(ref_hi, price)
    if lo is None or hi is None or (hi - lo) / max(price, 1e-9) < 0.005:
        if sup and sup < price:
            lo = sup * 0.995
            hi = min(sup * 1.02, price)
        else:
            lo, hi = price * 0.95, price * 0.98
    if hi > price:
        hi = price
    if lo > hi:
        lo, hi = hi * 0.99, hi
    return round(lo, 2), round(hi, 2)


def load_weights():
    prior = {"score_fund": 0.32, "score_news": 0.16,
             "score_oversold60": 0.045, "score_bias60": 0.045,
             "score_rsi_rev": 0.045, "score_vol_pulse": 0.045,
             "score_overbought": 0.0}
    try:
        d = json.load(open(os.path.join(HERE, "factor_weights.json"), encoding="utf-8"))
        w = d.get("weights") or {}
        if w and abs(sum(float(w.get(k) or 0) for k in prior) - 0.70) < 0.06:
            return w, d.get("version")
    except Exception:
        pass
    return prior, "prior-v4.2"


def main():
    hist = json.load(open(os.path.join(HERE, "factor_history.json"), encoding="utf-8"))
    snaps = hist["snapshots"]
    p = hist["prices"]
    tdays, px, op = p["dates"], p["px"], p.get("opens") or {}
    W, wver = load_weights()
    print(f"权重版本: {wver}\n")

    # 预计算：每个 (截面, 票) 的均线/支撑/买区
    prep = []
    for snap in snaps:
        d0 = snap["date"]
        if d0 not in tdays:
            continue
        i0 = tdays.index(d0)
        if i0 - MA_WIN + 1 < 0:
            continue
        recs = []
        for r in snap["rows"]:
            c = str(r.get("code") or "")
            s = px.get(c)
            if not s or i0 >= len(s):
                continue
            price = r.get("price")
            if not isinstance(price, (int, float)) or price <= 0:
                price = s[i0]
            if not isinstance(price, (int, float)) or price <= 0:
                continue
            ma5 = ma_at(s, i0, 5)
            ma10 = ma_at(s, i0, 10)
            ma20 = ma_at(s, i0, 20)
            seg = [v for v in s[i0 - MA_WIN + 1:i0 + 1]
                   if isinstance(v, (int, float)) and v > 0]
            if not seg:
                continue
            lo, hi = buy_zone(price, ma5, ma10, ma20, min(seg))
            if lo is None:
                continue
            recs.append({"code": c, "price": price, "buy_lo": lo, "buy_hi": hi,
                         "row": r})
        if len(recs) < 20:
            continue
        prep.append({"date": d0, "i0": i0, "recs": recs})
    print(f"可计算买区的截面：{len(prep)} / {len(snaps)}")

    # ---- 构造组合（两个变体）----
    def make_picks(items, need_zone):
        n = len(items)
        cols = {k: [F(x["row"].get(k)) for x in items]
                for k in ("score_fund", "score_news", "score_attention", "score_theme")}

        def rev(i):
            vs = [F(items[i]["row"].get(d), 0.0) or 0.0 for d in REV_BLOCK]
            return sum(vs) / len(vs) if vs else 0.0

        pr = {k: pct_rank(cols[k]) for k in ("score_fund", "score_news")}
        prv = pct_rank([rev(i) for i in range(n)])
        pr_att = pct_rank(cols["score_attention"])
        pr_thm = pct_rank(cols["score_theme"])
        wf = float(W.get("score_fund") or 0)
        wn = float(W.get("score_news") or 0)
        wr = sum(float(W.get(k) or 0) for k in REV_BLOCK)
        tot = (wf + wn + wr) or 1.0
        wf, wn, wr = wf / tot, wn / tot, wr / tot

        cand, n_zone, n_drop = [], 0, 0
        for i, x in enumerate(items):
            if pr_att.get(i, 0) >= DROP_HIGH["score_attention"]:
                continue
            if pr_thm.get(i, 0) >= DROP_HIGH["score_theme"]:
                continue
            if need_zone:
                if x["price"] <= x["buy_hi"]:
                    n_zone += 1
                else:
                    n_drop += 1
                    continue
            s = wf * pr["score_fund"].get(i, 0) + wn * pr["score_news"].get(i, 0) \
                + wr * prv.get(i, 0)
            cand.append((s, i))
        cand.sort(key=lambda x: -x[0])
        codes = [items[i]["code"] for _, i in cand[:TOP_N]]
        return codes, n_zone, n_drop

    variants = {"no_filter": False, "in_zone": True}
    picks_by_v = {k: [] for k in variants}
    zone_stats = []
    for rec in prep:
        items = rec["recs"]
        for vk, nz in variants.items():
            codes, n_zone, n_drop = make_picks(items, nz)
            if len(codes) >= MIN_PICKS:
                picks_by_v[vk].append({"date": rec["date"], "i0": rec["i0"],
                                       "codes": codes})
        _, n_zone, n_drop = make_picks(items, True)
        zone_stats.append({"date": rec["date"], "n_eligible": len(items),
                           "n_in_zone": n_zone, "n_above": n_drop,
                           "in_zone_pct": round(n_zone / len(items) * 100, 1)})
    for vk in variants:
        print(f"可用入场日 [{vk}]：{len(picks_by_v[vk])}")
    _z = [z["in_zone_pct"] for z in zone_stats]
    print(f"「现价≤买区上限」的当日占比：中位 {med(_z):.1f}% · "
          f"最小 {min(_z):.1f}% · 最大 {max(_z):.1f}%")
    _cnt = [len(x["codes"]) for x in picks_by_v["in_zone"]]
    if _cnt:
        print(f"in_zone 组合只数：最小 {min(_cnt)} · 中位 {med(_cnt):.0f} · 最大 {max(_cnt)}")

    # ---- 逐 h 计算 ----
    rows_by_h = {}
    for vk in variants:
        rows_by_h[vk] = {}
        for h in range(1, MAXH + 1):
            port_rets, bench_rets, used = [], [], []
            for rc in picks_by_v[vk]:
                i0 = rc["i0"]
                if i0 + h >= len(tdays):
                    continue
                ie = i0 + 1
                if ie >= len(tdays):
                    continue
                rs = []
                for c in rc["codes"]:
                    o = op.get(c) or []
                    e = px.get(c) or []
                    if ie >= len(o) or i0 + h >= len(e):
                        continue
                    po, pc = o[ie], e[i0 + h]
                    if not isinstance(po, (int, float)) or po <= 0:
                        continue
                    if not isinstance(pc, (int, float)) or pc <= 0:
                        continue
                    rs.append(pc / po - 1.0)
                if len(rs) < 5:
                    continue
                port_rets.append(sum(rs) / len(rs))
                # 池内基准：同截面全部有价格的票、同窗口、同口径
                brs = []
                for x in prep:
                    if x["date"] == rc["date"]:
                        for it in x["recs"]:
                            o2 = op.get(it["code"]) or []
                            e2 = px.get(it["code"]) or []
                            if ie >= len(o2) or i0 + h >= len(e2):
                                continue
                            po2, pc2 = o2[ie], e2[i0 + h]
                            if isinstance(po2, (int, float)) and po2 > 0 and \
                               isinstance(pc2, (int, float)) and pc2 > 0:
                                brs.append(pc2 / po2 - 1.0)
                        break
                if len(brs) < 20:
                    port_rets.pop()
                    continue
                bench_rets.append(sum(brs) / len(brs))
                used.append(rc["date"])
            n = len(port_rets)
            if n < 3:
                continue
            exc = [port_rets[i] - bench_rets[i] for i in range(n)]
            rows_by_h[vk][h] = {
                "h": h, "n": n, "dates": used,
                "port_mean_pct": round(sum(port_rets) / n * 100, 3),
                "bench_mean_pct": round(sum(bench_rets) / n * 100, 3),
                "exc_mean_pct": round(sum(exc) / n * 100, 3),
                "exc_med_pct": round(med(exc) * 100, 3),
                "win_pct": round(sum(1 for x in exc if x > 0) / n * 100, 1),
                "nw_t_laghm1": (round(nw_t(exc, h - 1), 2) if nw_t(exc, h - 1) is not None else None),
                "exc_series_pct": [round(x * 100, 3) for x in exc],
            }

    # ---- 对比输出 ----
    STDS = [1, 3, 5, 10, 15, 20]
    print("\n=== A/B 对比：不过滤 vs 只选现价在买区内 ===")
    print(f"{'周期':>7} | {'A超额':>8} {'A胜率':>7} {'A NWt':>7} | "
          f"{'B超额':>8} {'B胜率':>7} {'B NWt':>7} | {'B-A':>8}")
    print("-" * 76)
    cmp_out = {}
    for h in STDS:
        a = rows_by_h["no_filter"].get(h)
        b = rows_by_h["in_zone"].get(h)
        if not a or not b:
            continue
        d = b["exc_mean_pct"] - a["exc_mean_pct"]
        cmp_out[str(h)] = {
            "A_exc_pct": a["exc_mean_pct"], "A_win_pct": a["win_pct"],
            "A_nw_t": a["nw_t_laghm1"], "A_n": a["n"],
            "B_exc_pct": b["exc_mean_pct"], "B_win_pct": b["win_pct"],
            "B_nw_t": b["nw_t_laghm1"], "B_n": b["n"],
            "diff_pct": round(d, 3),
        }
        print(f"  T+{h:<4} | {a['exc_mean_pct']:>+7.2f}% {a['win_pct']:>6.1f}% "
              f"{(a['nw_t_laghm1'] or 0):>+7.2f} | {b['exc_mean_pct']:>+7.2f}% "
              f"{b['win_pct']:>6.1f}% {(b['nw_t_laghm1'] or 0):>+7.2f} | {d:>+7.2f}%")

    def peak(vk):
        c = [(h, rows_by_h[vk][h]["exc_mean_pct"]) for h in range(1, MAXH + 1)
             if rows_by_h[vk].get(h)]
        return max(c, key=lambda x: x[1]) if c else (None, None)
    pa, pb = peak("no_filter"), peak("in_zone")
    print(f"\n超额峰值：A h={pa[0]}（{pa[1]:+.2f}%） · B h={pb[0]}（{pb[1]:+.2f}%）")

    res = {
        "date_run": "latest",
        "weights_version": wver,
        "note": "A/B 唯一差别 = 是否要求现价≤买区上限；权重/入场/出场/基准口径完全一致",
        "reconstruction": {
            "ma": "用价格库对 d0 收盘价算 ma5/ma10/ma20",
            "support": "近 20 交易日最低价（近似 tech.support_levels）",
            "buy_zone_rule": "与 gen_report_top200.build_trade_plan 同构："
                             "多头[ma10, min(ma5,price)]；弱势[支撑*0.995, min(支撑*1.02,price)]；"
                             "兜底[price*0.95, price*0.98]；buy_hi 永不超过 price",
        },
        "n_snapshots": len(prep),
        "zone_stats": {"median_in_zone_pct": round(med(_z), 1),
                       "min_in_zone_pct": round(min(_z), 1),
                       "max_in_zone_pct": round(max(_z), 1),
                       "by_date": zone_stats},
        "n_entry_days": {k: len(v) for k, v in picks_by_v.items()},
        "comparison": cmp_out,
        "curve_A_no_filter": {str(h): rows_by_h["no_filter"][h] for h in range(1, MAXH + 1)
                              if rows_by_h["no_filter"].get(h)},
        "curve_B_in_zone": {str(h): rows_by_h["in_zone"][h] for h in range(1, MAXH + 1)
                            if rows_by_h["in_zone"].get(h)},
        "peak": {"A_h": pa[0], "A_exc_pct": pa[1], "B_h": pb[0], "B_exc_pct": pb[1]},
        "boundaries": [
            "无 RSI → 无法复现 RSI>70 剔除；无 sector_l2 → 无法复现板块约束",
            "支撑/阻力用近 20 日高低点近似，线上用 tech.support_levels，买区边界会有差异",
            "样本区间 2026-06-26 ~ 2026-10-23，恰逢池子负 alpha 期，绝对收益全为负",
            "入场=次日开盘，出场=d0+h 收盘，未计手续费与冲击成本",
            "日频重叠观测：非重叠样本量远小于 n",
        ],
    }
    json.dump(res, open(OUT, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"\n✅ 写入 {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
