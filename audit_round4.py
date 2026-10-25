#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
audit_round4.py — 第 4 轮 P1 实证审计（2026-10-25）

针对剩余 P1 逐项做真实回测，用数据决定「修 / 披露 / 关闭」：

P1-2 反转块共线：5 维两两 |corr| 0.74~0.89，合计权重 0.24 → 是否重复计权？
     → 对比：(a) 5 维按现权重  (b) 5 维取等权均值合成 1 维  (c) 只留 RSI_REV 单维
P1-3 板块约束 L2≤2/L1≤2 从未回测 → 有约束 vs 无约束的组合表现
P1-4 组合层用板块动量、评分层关个股动量 → 口径互斥，量化其影响

输出 audit_round4.json
"""
import json
import math
import re
import statistics as st
from collections import defaultdict

HIST = "factor_history.json"
SCORED = "top200_scored_2026-10-23.json"
COST_BPS = 20.0
BONF_T = 3.08

REV = ("score_oversold60", "score_bias60", "score_rsi_rev",
       "score_vol_pulse", "score_overbought")


def bare(c):
    return re.sub(r"^(sh|sz|bj)", "", str(c))


def load():
    h = json.load(open(HIST))
    d = json.load(open(SCORED))
    smap = {}
    for r in d["results"]:
        smap[bare(r["code"])] = {
            "s1": r.get("sector_parent") or r.get("sector"),
            "s2": r.get("sector_l2"),
            "mom10": r.get("sector_mom10"),
            "mom60": r.get("sector_mom60"),
        }
    days = {}
    for s in h["snapshots"]:
        days[s["date"]] = {bare(r["code"]): r for r in s["rows"]}
    dates = sorted(days)
    # prices 结构: {"dates":[...], "px":{code:[...]}, "opens":{code:[...]}}
    praw = h.get("prices") or {}
    pdates = praw.get("dates") or []
    pidx = {d0: k for k, d0 in enumerate(pdates)}
    px_map = praw.get("px") or {}
    op_map = praw.get("opens") or {}
    prices = {"dates": pdates, "idx": pidx, "px": px_map, "opens": op_map}
    return days, dates, prices, smap


def px(prices, code, date, use_open=False):
    """取某日价格；use_open=True 优先用开盘价（可交易口径买入）"""
    j = prices["idx"].get(date)
    if j is None:
        return None
    src = prices["opens"] if use_open else prices["px"]
    arr = src.get(code) or src.get(bare(code))
    if not arr or j >= len(arr):
        return None
    v = arr[j]
    if v is None and use_open:
        arr2 = prices["px"].get(code) or prices["px"].get(bare(code))
        if arr2 and j < len(arr2):
            v = arr2[j]
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def fwd_ret(prices, code, dates, i, hop):
    """t+1 开盘买入、t+1+hop 收盘卖出，扣 20bp 往返成本"""
    if i + 1 + hop >= len(dates):
        return None
    p_in = px(prices, code, dates[i + 1], use_open=True)
    p_out = px(prices, code, dates[i + 1 + hop], use_open=False)
    if not p_in or not p_out or p_in <= 0:
        return None
    return (p_out / p_in - 1.0) * 100.0 - COST_BPS / 100.0


def nw_t(xs):
    """Newey-West 调整 t（lag = 4）"""
    n = len(xs)
    if n < 3:
        return None
    m = sum(xs) / n
    e = [x - m for x in xs]
    g0 = sum(v * v for v in e) / n
    s = g0
    for L in range(1, min(5, n)):
        gl = sum(e[i] * e[i - L] for i in range(L, n)) / n
        s += 2.0 * (1.0 - L / 5.0) * gl
    if s <= 0:
        return None
    se = math.sqrt(s / n)
    return m / se if se > 0 else None


def zs(vals):
    """截面 z-score（忽略 None）"""
    good = [v for v in vals if v is not None]
    if len(good) < 5:
        return None
    m = sum(good) / len(good)
    sd = st.pstdev(good)
    if sd <= 0:
        return None
    return m, sd


def rev_composite(row, mode):
    """反转块的三种合成方式，返回 0-1 归一化信号（越高越反转）"""
    v = [row.get(f) for f in REV]
    have = [x for x in v if isinstance(x, (int, float))]
    if not have:
        return None
    if mode == "weighted":
        # 现权重（去掉 overbought 的负向：overbought 越高越差，需反号）
        w = {"score_oversold60": 0.055, "score_bias60": 0.055,
             "score_rsi_rev": 0.055, "score_vol_pulse": 0.055,
             "score_overbought": 0.02}
        num = 0.0
        den = 0.0
        for f in REV:
            x = row.get(f)
            if isinstance(x, (int, float)):
                ww = w[f]
                xx = (5.0 - x) if f == "score_overbought" else x
                num += ww * xx
                den += ww
        return num / den if den > 0 else None
    if mode == "mean":
        # 等权均值合成 1 维（去冗余）
        vals2 = []
        for f in REV:
            x = row.get(f)
            if isinstance(x, (int, float)):
                vals2.append((5.0 - x) if f == "score_overbought" else x)
        return sum(vals2) / len(vals2) if vals2 else None
    if mode == "rsi_only":
        x = row.get("score_rsi_rev")
        return x if isinstance(x, (int, float)) else None
    return None


def run_rev_block(days, dates, prices):
    """P1-2：反转块三种合成方式的 IC 与头组超额"""
    out = {}
    for mode in ("weighted", "mean", "rsi_only"):
        ics = {1: [], 5: [], 20: []}
        excs = {1: [], 5: [], 20: []}
        for i, d in enumerate(dates):
            rows = list(days[d].values())
            if len(rows) < 30:
                continue
            sig = []
            for r in rows:
                s = rev_composite(r, mode)
                rr = {h: fwd_ret(prices, bare(r["code"]), dates, i, h)
                      for h in (1, 5, 20)}
                if s is not None:
                    sig.append((s, rr, r))
            if len(sig) < 30:
                continue
            z = zs([x[0] for x in sig])
            if not z:
                continue
            mu, sd = z
            for h in (1, 5, 20):
                pairs = [((x[0] - mu) / sd, x[1][h]) for x in sig
                         if x[1][h] is not None]
                if len(pairs) < 25:
                    continue
                n = len(pairs)
                xs = [p[0] for p in pairs]
                ys = [p[1] for p in pairs]
                mx = sum(xs) / n
                my = sum(ys) / n
                cov = sum((xs[k] - mx) * (ys[k] - my) for k in range(n))
                vx = sum((xs[k] - mx) ** 2 for k in range(n))
                vy = sum((ys[k] - my) ** 2 for k in range(n))
                if vx <= 0 or vy <= 0:
                    continue
                ic = cov / math.sqrt(vx * vy)
                ics[h].append(ic)
                srt = sorted(pairs, key=lambda p: -p[0])
                k = max(3, int(round(n * 0.2)))
                top = sum(p[1] for p in srt[:k]) / k
                bot = sum(p[1] for p in srt[-k:]) / k
                excs[h].append(round(top, 4))
        row = {}
        for h in (1, 5, 20):
            if ics[h]:
                row["h%d" % h] = {
                    "ic": round(sum(ics[h]) / len(ics[h]), 4),
                    "t": (round(nw_t(ics[h]), 2) if nw_t(ics[h]) else None),
                    "ic_win": round(sum(1 for x in ics[h] if x > 0)
                                    * 100.0 / len(ics[h]), 1),
                    "n": len(ics[h]),
                    "top_exc": (round(sum(excs[h]) / len(excs[h]), 2)
                                if excs[h] else None),
                }
        out[mode] = row
    return out


def portfolio(days, dates, prices, smap, i, cap_l2, cap_l1, n_pick=10):
    """构造当日组合：fund 主导 + news，可叠加板块约束"""
    d = dates[i]
    rows = list(days[d].values())
    cand = []
    for r in rows:
        f = r.get("score_fund")
        n = r.get("score_news")
        if not isinstance(f, (int, float)):
            continue
        nn = n if isinstance(n, (int, float)) else 0.0
        # 复刻 pick_multifactor 的权重：fund .762 news .201
        sc = 0.762 * f + 0.201 * nn
        info = smap.get(bare(r["code"])) or {}
        cand.append((sc, r, info.get("s2"), info.get("s1")))
    if not cand:
        return []
    cand.sort(key=lambda x: -x[0])
    picked = []
    c2 = defaultdict(int)
    c1 = defaultdict(int)
    for sc, r, s2, s1 in cand:
        if cap_l2 and s2 and c2[s2] >= cap_l2:
            continue
        if cap_l1 and s1 and c1[s1] >= cap_l1:
            continue
        picked.append(r)
        if s2:
            c2[s2] += 1
        if s1:
            c1[s1] += 1
        if len(picked) >= n_pick:
            break
    return picked


def run_portfolio(days, dates, prices, smap):
    """P1-3：有/无板块约束的组合表现对比（逐日配对）"""
    variants = {
        "no_cap": (0, 0),
        "cap2_2": (2, 2),
        "cap1_1": (1, 1),
        "cap2_1": (2, 1),
    }
    series = {k: {1: [], 5: [], 20: []} for k in variants}
    for i in range(len(dates)):
        pk = {}
        for name, (a, b) in variants.items():
            pk[name] = portfolio(days, dates, prices, smap, i, a, b)
        if not pk.get("no_cap"):
            continue
        for h in (1, 5, 20):
            for name, picks in pk.items():
                rs = []
                for r in picks:
                    rr = fwd_ret(prices, bare(r["code"]), dates, i, h)
                    if rr is not None:
                        rs.append(rr)
                if rs:
                    series[name][h].append(sum(rs) / len(rs))
    res = {}
    for h in (1, 5, 20):
        base = series["no_cap"][h]
        if len(base) < 5:
            continue
        bm = sum(base) / len(base)
        row = {"no_cap_mean": round(bm, 2), "n": len(base)}
        for name in variants:
            if name == "no_cap":
                continue
            v = series[name][h]
            if len(v) < 5:
                continue
            m = len(v) if len(v) == len(base) else min(len(v), len(base))
            dif = [v[k] - base[k] for k in range(m)]
            t = nw_t(dif)
            row[name] = {
                "mean": round(sum(v) / len(v), 2),
                "delta": round(sum(dif) / m, 2),
                "t": (round(t, 2) if t else None),
                "sig": bool(t and abs(t) >= BONF_T),
                "win": round(sum(1 for x in dif if x > 0) * 100.0 / m, 1),
            }
        res["h%d" % h] = row
    return res


def run_momentum_conflict(days, dates, prices, smap):
    """P1-4：组合层用 sector_mom 排序 vs 不用，差多少"""
    out = {}
    for use_mom in (False, True):
        agg = {1: [], 5: [], 20: []}
        for i, d in enumerate(dates):
            rows = list(days[d].values())
            cand = []
            for r in rows:
                f = r.get("score_fund")
                if not isinstance(f, (int, float)):
                    continue
                nn = r.get("score_news")
                nn = nn if isinstance(nn, (int, float)) else 0.0
                info = smap.get(bare(r["code"])) or {}
                m10 = info.get("mom10")
                mom_adj = 0.0
                if use_mom and isinstance(m10, (int, float)):
                    mom_adj = max(-1.0, min(1.0, m10 / 10.0)) * 0.5
                cand.append((0.762 * f + 0.201 * nn + mom_adj, r))
            if len(cand) < 10:
                continue
            cand.sort(key=lambda x: -x[0])
            picks = [c[1] for c in cand[:10]]
            for h in (1, 5, 20):
                rs = [fwd_ret(prices, bare(r["code"]), dates, i, h) for r in picks]
                rs = [x for x in rs if x is not None]
                if rs:
                    agg[h].append(sum(rs) / len(rs))
        key = "with_sector_mom" if use_mom else "fund_news_only"
        out[key] = {("h%d" % h): (round(sum(agg[h]) / len(agg[h]), 2)
                                  if agg[h] else None) for h in (1, 5, 20)}
        out[key]["n"] = len(agg[1])
    # 配对差
    diffs = {}
    for h in (1, 5, 20):
        a = out["fund_news_only"].get("h%d" % h)
        b = out["with_sector_mom"].get("h%d" % h)
        if a is not None and b is not None:
            diffs["h%d" % h] = round(b - a, 2)
    out["delta_mom_minus_plain"] = diffs
    return out


def main():
    days, dates, prices, smap = load()
    print("snapshots=%d  dates=%s..%s  codes_with_sector=%d"
          % (len(dates), dates[0], dates[-1], len(smap)))
    res = {"date_range": [dates[0], dates[-1]], "n_snapshots": len(dates)}

    print("\n=== P1-2 反转块合成方式对比 ===")
    r2 = run_rev_block(days, dates, prices)
    res["p1_2_reversal_block"] = r2
    for mode, row in r2.items():
        line = []
        for h in (1, 5, 20):
            c = row.get("h%d" % h)
            if c:
                line.append("T+%d ic=%+.4f t=%s win=%.0f%% top=%+.2f%%"
                            % (h, c["ic"], c["t"], c["ic_win"], c["top_exc"]))
        print("  %-10s %s" % (mode, " | ".join(line)))

    print("\n=== P1-3 板块约束对比（vs 无约束）===")
    r3 = run_portfolio(days, dates, prices, smap)
    res["p1_3_sector_cap"] = r3
    for h in ("h1", "h5", "h20"):
        row = r3.get(h)
        if not row:
            continue
        print("  %s  无约束均值 %+.2f%% (n=%d)"
              % (h, row["no_cap_mean"], row["n"]))
        for name in ("cap2_2", "cap1_1", "cap2_1"):
            c = row.get(name)
            if c:
                print("       %-7s %+.2f%%  delta=%+.2f  t=%s sig=%s win=%.0f%%"
                      % (name, c["mean"], c["delta"], c["t"],
                         "YES" if c["sig"] else "no", c["win"]))

    print("\n=== P1-4 板块动量叠加影响 ===")
    r4 = run_momentum_conflict(days, dates, prices, smap)
    res["p1_4_sector_mom"] = r4
    for k in ("fund_news_only", "with_sector_mom"):
        print("  %-18s %s" % (k, {h: r4[k][h] for h in ("h1", "h5", "h20")}))
    print("  差值(叠加动量-纯fund+news): %s" % r4["delta_mom_minus_plain"])

    json.dump(res, open("audit_round4.json", "w"), ensure_ascii=False, indent=2)
    print("\n-> audit_round4.json")


if __name__ == "__main__":
    main()
