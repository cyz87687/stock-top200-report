#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
audit_round5.py — P1-2 反转块权重敏感性 + P1-5 分市场状态检验（2026-10-25）

起因：audit_round4 发现反转块 T+1 IC 为负（-0.0254, t=-0.63）、
T+5 转正（+0.0791, t=1.84），而现权重给反转块合计 0.24。
本脚本回答两个问题：

Q1（P1-2）反转块权重从 0.24 降到多少，组合 5 日表现最优？是否显著？
Q2（P1-5）把窗口切成前后两段，小市值/fund 的有效性是否只在某一段成立？

输出 audit_round5.json
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

# 现权重（v3.6-fund-led）
W_NOW = {
    "fund": 0.34, "news": 0.22, "sector": 0.10,
    "oversold60": 0.055, "bias60": 0.055, "rsi_rev": 0.055,
    "vol_pulse": 0.055, "overbought": 0.02,
    "theme_intrinsic": 0.08, "attention": 0.02,
}


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
        }
    days = {}
    for s in h["snapshots"]:
        days[s["date"]] = {bare(r["code"]): r for r in s["rows"]}
    dates = sorted(days)
    praw = h.get("prices") or {}
    pdates = praw.get("dates") or []
    prices = {"idx": {x: k for k, x in enumerate(pdates)},
              "px": praw.get("px") or {}, "opens": praw.get("opens") or {}}
    return days, dates, prices, smap


def px(prices, code, date, use_open=False):
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
    if i + 1 + hop >= len(dates):
        return None
    p_in = px(prices, code, dates[i + 1], use_open=True)
    p_out = px(prices, code, dates[i + 1 + hop], use_open=False)
    if not p_in or not p_out or p_in <= 0:
        return None
    return (p_out / p_in - 1.0) * 100.0 - COST_BPS / 100.0


def nw_t(xs):
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


def rev_score(row, mode):
    """反转块 0-5 分。mode: weighted(现权重) / mean(等权均值) / zero"""
    if mode == "zero":
        return None
    vals = []
    for f in REV:
        x = row.get(f)
        if isinstance(x, (int, float)):
            vals.append((5.0 - x) if f == "score_overbought" else x)
    if not vals:
        return None
    if mode == "mean":
        return sum(vals) / len(vals)
    # weighted
    w = {"score_oversold60": 0.055, "score_bias60": 0.055,
         "score_rsi_rev": 0.055, "score_vol_pulse": 0.055,
         "score_overbought": 0.02}
    num = den = 0.0
    for f in REV:
        x = row.get(f)
        if isinstance(x, (int, float)):
            xx = (5.0 - x) if f == "score_overbought" else x
            num += w[f] * xx
            den += w[f]
    return num / den if den > 0 else None


def build_score(row, rev_w, rev_mode="weighted"):
    """按给定反转块总权重构造总分（其余维度按现权重归一）"""
    f = row.get("score_fund")
    if not isinstance(f, (int, float)):
        return None
    n = row.get("score_news")
    n = n if isinstance(n, (int, float)) else 0.0
    sec = row.get("score_sector")
    sec = sec if isinstance(sec, (int, float)) else 0.0
    th = row.get("score_theme_intrinsic")
    th = th if isinstance(th, (int, float)) else 0.0
    at = row.get("score_attention")
    at = at if isinstance(at, (int, float)) else 0.0

    now_rev = 0.24
    scale = (1.0 - rev_w) / (1.0 - now_rev) if rev_w < 1.0 else 0.0
    s = (W_NOW["fund"] * f + W_NOW["news"] * n + W_NOW["sector"] * sec
         + W_NOW["theme_intrinsic"] * th + W_NOW["attention"] * at) * scale
    if rev_w > 0:
        rv = rev_score(row, rev_mode)
        if rv is not None:
            s += rev_w * rv
        else:
            # 缺失时把这块权重均摊给 fund
            s += rev_w * f
    return s


def run_weight_scan(days, dates, prices, smap):
    """P1-2：反转块总权重扫描"""
    grid = [0.0, 0.08, 0.12, 0.18, 0.24]
    modes = ["weighted", "mean"]
    out = {}
    for mode in modes:
        for rw in grid:
            key = "%s_%.2f" % (mode, rw)
            agg = {1: [], 5: [], 20: []}
            for i, d in enumerate(dates):
                rows = list(days[d].values())
                cand = []
                for r in rows:
                    s = build_score(r, rw, mode)
                    if s is not None:
                        cand.append((s, r))
                if len(cand) < 20:
                    continue
                cand.sort(key=lambda x: -x[0])
                picks = [c[1] for c in cand[:10]]
                for h in (1, 5, 20):
                    rs = [fwd_ret(prices, bare(r["code"]), dates, i, h)
                          for r in picks]
                    rs = [x for x in rs if x is not None]
                    if rs:
                        agg[h].append(sum(rs) / len(rs))
            row = {}
            for h in (1, 5, 20):
                if agg[h]:
                    row["h%d" % h] = {
                        "mean": round(sum(agg[h]) / len(agg[h]), 2),
                        "n": len(agg[h]),
                    }
            out[key] = row
    # 配对检验：每个变体 vs 现权重 0.24
    base_key = "weighted_0.24"
    base_series = {}
    for i, d in enumerate(dates):
        rows = list(days[d].values())
        cand = [(build_score(r, 0.24, "weighted"), r) for r in rows]
        cand = [c for c in cand if c[0] is not None]
        if len(cand) < 20:
            continue
        cand.sort(key=lambda x: -x[0])
        picks = [c[1] for c in cand[:10]]
        for h in (1, 5, 20):
            rs = [fwd_ret(prices, bare(r["code"]), dates, i, h) for r in picks]
            rs = [x for x in rs if x is not None]
            if rs:
                base_series.setdefault(h, []).append(sum(rs) / len(rs))
    tests = {}
    for mode in modes:
        for rw in grid:
            key = "%s_%.2f" % (mode, rw)
            if key == base_key:
                continue
            trow = {}
            for h in (1, 5, 20):
                ser = []
                for i, d in enumerate(dates):
                    rows = list(days[d].values())
                    cand = [(build_score(r, rw, mode), r) for r in rows]
                    cand = [c for c in cand if c[0] is not None]
                    if len(cand) < 20:
                        continue
                    cand.sort(key=lambda x: -x[0])
                    picks = [c[1] for c in cand[:10]]
                    rs = [fwd_ret(prices, bare(r["code"]), dates, i, h)
                          for r in picks]
                    rs = [x for x in rs if x is not None]
                    if rs:
                        ser.append(sum(rs) / len(rs))
                b = base_series.get(h) or []
                m = min(len(ser), len(b))
                if m < 8:
                    continue
                dif = [ser[k] - b[k] for k in range(m)]
                t = nw_t(dif)
                trow["h%d" % h] = {
                    "delta": round(sum(dif) / m, 2),
                    "t": (round(t, 2) if t else None),
                    "sig": bool(t and abs(t) >= BONF_T),
                    "win": round(sum(1 for x in dif if x > 0) * 100.0 / m, 1),
                }
            tests[key] = trow
    return {"means": out, "vs_base_0.24": tests}


def run_regime_split(days, dates, prices, smap):
    """P1-5：前后两段分别检验 fund 头组表现，判断是否单一市场状态"""
    mid = len(dates) // 2
    segs = {"first_half": dates[:mid], "second_half": dates[mid:]}
    out = {"split_date": dates[mid], "segments": {}}
    for name, seg in segs.items():
        if len(seg) < 10:
            continue
        ics = {1: [], 5: [], 20: []}
        tops = {1: [], 5: [], 20: []}
        pools = {1: [], 5: [], 20: []}
        for i, d in enumerate(dates):
            if d not in seg:
                continue
            rows = list(days[d].values())
            cand = [(r.get("score_fund"), r) for r in rows
                    if isinstance(r.get("score_fund"), (int, float))]
            if len(cand) < 30:
                continue
            cand.sort(key=lambda x: -x[0])
            # 头 20%
            k = max(3, int(round(len(cand) * 0.2)))
            top = [c[1] for c in cand[:k]]
            for h in (1, 5, 20):
                trs = [fwd_ret(prices, bare(r["code"]), dates, i, h) for r in top]
                trs = [x for x in trs if x is not None]
                prs = [fwd_ret(prices, bare(r["code"]), dates, i, h)
                       for r in rows]
                prs = [x for x in prs if x is not None]
                if trs and prs:
                    tops[h].append(sum(trs) / len(trs))
                    pools[h].append(sum(prs) / len(prs))
        row = {}
        for h in (1, 5, 20):
            if tops[h] and pools[h]:
                m = min(len(tops[h]), len(pools[h]))
                dif = [tops[h][j] - pools[h][j] for j in range(m)]
                t = nw_t(dif)
                row["h%d" % h] = {
                    "top": round(sum(tops[h]) / len(tops[h]), 2),
                    "pool": round(sum(pools[h]) / len(pools[h]), 2),
                    "delta": round(sum(dif) / m, 2),
                    "t": (round(t, 2) if t else None),
                    "win": round(sum(1 for x in dif if x > 0) * 100.0 / m, 1),
                    "n": m,
                }
        out["segments"][name] = row
    return out


def main():
    days, dates, prices, smap = load()
    print("snapshots=%d  %s..%s" % (len(dates), dates[0], dates[-1]))
    res = {"range": [dates[0], dates[-1]], "n": len(dates)}

    print("\n=== Q1 反转块权重扫描（组合均值，%）===")
    q1 = run_weight_scan(days, dates, prices, smap)
    res["q1_weight_scan"] = q1
    print("  %-16s %8s %8s %8s" % ("配置", "T+1", "T+5", "T+20"))
    for key in sorted(q1["means"]):
        r = q1["means"][key]
        vals = [r.get("h%d" % h, {}).get("mean") for h in (1, 5, 20)]
        print("  %-16s %8s %8s %8s" % (key, *[("%+.2f" % v) if v is not None
                                               else "-" for v in vals]))
    print("\n  -- 相对现权重(0.24)的配对检验 --")
    for key in sorted(q1["vs_base_0.24"]):
        r = q1["vs_base_0.24"][key]
        parts = []
        for h in (1, 5, 20):
            c = r.get("h%d" % h)
            if c:
                parts.append("T+%d %+.2f(t=%s,%s)" % (h, c["delta"], c["t"],
                                                      "SIG" if c["sig"] else "ns"))
        print("  %-16s %s" % (key, " | ".join(parts)))

    print("\n=== Q2 分市场状态（fund 头20% 超额）===")
    q2 = run_regime_split(days, dates, prices, smap)
    res["q2_regime"] = q2
    print("  切分日: %s" % q2["split_date"])
    for name, row in q2["segments"].items():
        for h in ("h1", "h5", "h20"):
            c = row.get(h)
            if c:
                print("  %-12s %s  头组 %+.2f%% / 池内 %+.2f%%  超额 %+.2f%% "
                      "t=%s win=%.0f%% n=%d"
                      % (name, h, c["top"], c["pool"], c["delta"], c["t"],
                         c["win"], c["n"]))

    json.dump(res, open("audit_round5.json", "w"), ensure_ascii=False, indent=2)
    print("\n-> audit_round5.json")


if __name__ == "__main__":
    main()
