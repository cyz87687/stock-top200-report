#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
audit_round7.py — fund 因子近期失效的严格复验（2026-10-25）

起因：audit_round6 滚动窗口显示最近 30 日 fund 头20% 超额
  T+1 t=-2.96、T+5 t=-2.98（显著为负）。
但滚动窗口高度重叠 → t 值可能被高估。本轮用三种方法交叉验证：

M1 非重叠采样：T+5 用 hop=5 跳采（每 5 个交易日取 1 个），消除重叠
M2 只用最近 20 个交易日、非重叠
M3 我们把「头20%」换成「头10 只」再看，排除排序尾部干扰

同时检验：换成等权全池基准（market-neutral）结论是否一致。

输出 audit_round7.json
"""
import json
import math
import re

HIST = "factor_history.json"
COST_BPS = 20.0


def bare(c):
    return re.sub(r"^(sh|sz|bj)", "", str(c))


def load():
    h = json.load(open(HIST))
    days = {}
    for s in h["snapshots"]:
        days[s["date"]] = {bare(r["code"]): r for r in s["rows"]}
    dates = sorted(days)
    praw = h.get("prices") or {}
    pdates = praw.get("dates") or []
    prices = {"idx": {x: k for k, x in enumerate(pdates)},
              "px": praw.get("px") or {}, "opens": praw.get("opens") or {}}
    return days, dates, prices


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


def nw_t(xs, lag=4):
    n = len(xs)
    if n < 4:
        return None
    m = sum(xs) / n
    e = [x - m for x in xs]
    s = sum(v * v for v in e) / n
    for L in range(1, min(lag + 1, n)):
        s += 2.0 * (1.0 - L / (lag + 1.0)) * sum(
            e[i] * e[i - L] for i in range(L, n)) / n
    if s <= 0:
        return None
    se = math.sqrt(s / n)
    return m / se if se > 0 else None


def plain_t(xs):
    n = len(xs)
    if n < 3:
        return None
    m = sum(xs) / n
    sd = math.sqrt(sum((x - m) ** 2 for x in xs) / (n - 1))
    if sd <= 0:
        return None
    return m / (sd / math.sqrt(n))


def excess_series(days, dates, prices, i, hop, k_mode):
    """返回当日 fund 头组相对全池的超额（百分点）"""
    d = dates[i]
    rows = list(days[d].values())
    cand = [(r.get("score_fund"), r) for r in rows
            if isinstance(r.get("score_fund"), (int, float))]
    if len(cand) < 30:
        return None
    cand.sort(key=lambda x: -x[0])
    if k_mode == "pct20":
        k = max(3, int(round(len(cand) * 0.2)))
    else:
        k = 10
    top = [c[1] for c in cand[:k]]
    trs = [fwd_ret(prices, bare(r["code"]), dates, i, hop) for r in top]
    trs = [x for x in trs if x is not None]
    prs = [fwd_ret(prices, bare(r["code"]), dates, i, hop) for r in rows]
    prs = [x for x in prs if x is not None]
    if not trs or not prs:
        return None
    return (sum(trs) / len(trs), sum(prs) / len(prs),
            sum(trs) / len(trs) - sum(prs) / len(prs))


def main():
    days, dates, prices = load()
    res = {"range": [dates[0], dates[-1]]}
    print("=== 方法 M1：非重叠采样（T+5 用 hop=5 跳采）===")
    print("  %-10s %6s %9s %9s %8s %6s %6s"
          % ("区间", "n", "头组%", "池内%", "超额%", "t_NW", "t_OLS"))
    m1 = {}
    for label, tail in (("全样本", None), ("近60日", 60), ("近40日", 40),
                        ("近20日", 20)):
        obs = []
        for i in range(0, len(dates), 5):  # 跳采
            if i + 1 + 5 >= len(dates):
                continue
            d = dates[i]
            if tail is not None and d < dates[-tail]:
                continue
            r = excess_series(days, dates, prices, i, 5, "pct20")
            if r:
                obs.append(r)
        if len(obs) < 4:
            print("  %-10s 样本不足" % label)
            continue
        tops = [o[0] for o in obs]
        pools = [o[1] for o in obs]
        excs = [o[2] for o in obs]
        tn = nw_t(excs)
        to = plain_t(excs)
        m1[label] = {"n": len(obs),
                     "top": round(sum(tops) / len(tops), 2),
                     "pool": round(sum(pools) / len(pools), 2),
                     "exc": round(sum(excs) / len(excs), 2),
                     "t_nw": round(tn, 2) if tn else None,
                     "t_ols": round(to, 2) if to else None,
                     "win": round(sum(1 for x in excs if x > 0)
                                  * 100.0 / len(excs), 1)}
        print("  %-10s %6d %+9.2f %+9.2f %+8.2f %6s %6s"
              % (label, len(obs), m1[label]["top"], m1[label]["pool"],
                 m1[label]["exc"], m1[label]["t_nw"], m1[label]["t_ols"]))
    res["m1_nonoverlap_h5"] = m1

    print("\n=== 方法 M2：T+1 逐日（近 N 日，日频无跳采但超额本身不重叠）===")
    m2 = {}
    for label, tail in (("全样本", None), ("近40日", 40), ("近20日", 20),
                        ("近10日", 10)):
        excs = []
        for i, d in enumerate(dates):
            if tail is not None and d < dates[-tail]:
                continue
            r = excess_series(days, dates, prices, i, 1, "pct20")
            if r:
                excs.append(r[2])
        if len(excs) < 4:
            continue
        tn = nw_t(excs, lag=4)
        to = plain_t(excs)
        m2[label] = {"n": len(excs), "exc": round(sum(excs) / len(excs), 2),
                     "t_nw": round(tn, 2) if tn else None,
                     "t_ols": round(to, 2) if to else None,
                     "win": round(sum(1 for x in excs if x > 0)
                                  * 100.0 / len(excs), 1)}
        print("  %-10s n=%-4d 超额 %+.2f%%  t_NW=%s  t_OLS=%s  胜率=%.0f%%"
              % (label, len(excs), m2[label]["exc"], m2[label]["t_nw"],
                 m2[label]["t_ols"], m2[label]["win"]))
    res["m2_daily_h1"] = m2

    print("\n=== 方法 M3：头 10 只（与实盘组合一致）· T+5 非重叠 ===")
    m3 = {}
    for label, tail in (("全样本", None), ("近60日", 60), ("近30日", 30)):
        obs = []
        for i in range(0, len(dates), 5):
            if i + 1 + 5 >= len(dates):
                continue
            if tail is not None and dates[i] < dates[-tail]:
                continue
            r = excess_series(days, dates, prices, i, 5, "top10")
            if r:
                obs.append(r[2])
        if len(obs) < 4:
            continue
        tn = nw_t(obs)
        m3[label] = {"n": len(obs), "exc": round(sum(obs) / len(obs), 2),
                     "t_nw": round(tn, 2) if tn else None,
                     "win": round(sum(1 for x in obs if x > 0)
                                  * 100.0 / len(obs), 1)}
        print("  %-10s n=%-3d 超额 %+.2f%%  t_NW=%s  胜率=%.0f%%"
              % (label, len(obs), m3[label]["exc"], m3[label]["t_nw"],
                 m3[label]["win"]))
    res["m3_top10_h5"] = m3

    json.dump(res, open("audit_round7.json", "w"), ensure_ascii=False, indent=2)
    print("\n-> audit_round7.json")


if __name__ == "__main__":
    main()
