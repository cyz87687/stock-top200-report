#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
audit_round6.py — fund 因子滚动窗口衰减检验（2026-10-25）

起因：audit_round5 分段发现 fund 头20% 超额
  前半段(06-26~08-26) T+20 +2.53% (t=2.15, 胜率74%)
  后半段(08-26~10-23) T+20 -0.69% (t=-0.83, 胜率33%)
两段方向相反。本脚本用 30 日滚动窗口确认：是趋势性衰减，还是二次切分偶然。

输出 audit_round6.json
"""
import json
import math
import re

HIST = "factor_history.json"
COST_BPS = 20.0
WIN = 30


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


def nw_t(xs):
    n = len(xs)
    if n < 3:
        return None
    m = sum(xs) / n
    e = [x - m for x in xs]
    s = sum(v * v for v in e) / n
    for L in range(1, min(5, n)):
        s += 2.0 * (1.0 - L / 5.0) * sum(e[i] * e[i - L]
                                        for i in range(L, n)) / n
    if s <= 0:
        return None
    se = math.sqrt(s / n)
    return m / se if se > 0 else None


def main():
    days, dates, prices = load()
    # 逐日算 fund 头20% 超额（三个周期）
    daily = {h: [] for h in (1, 5, 20)}
    for i, d in enumerate(dates):
        rows = list(days[d].values())
        cand = [(r.get("score_fund"), r) for r in rows
                if isinstance(r.get("score_fund"), (int, float))]
        if len(cand) < 30:
            for h in (1, 5, 20):
                daily[h].append(None)
            continue
        cand.sort(key=lambda x: -x[0])
        k = max(3, int(round(len(cand) * 0.2)))
        top = [c[1] for c in cand[:k]]
        for h in (1, 5, 20):
            trs = [fwd_ret(prices, bare(r["code"]), dates, i, h) for r in top]
            trs = [x for x in trs if x is not None]
            prs = [fwd_ret(prices, bare(r["code"]), dates, i, h) for r in rows]
            prs = [x for x in prs if x is not None]
            daily[h].append((sum(trs) / len(trs) - sum(prs) / len(prs))
                            if trs and prs else None)

    res = {"range": [dates[0], dates[-1]], "window": WIN, "roll": {}}
    for h in (1, 5, 20):
        ser = daily[h]
        pts = []
        for j in range(len(ser) - WIN + 1):
            chunk = [x for x in ser[j:j + WIN] if x is not None]
            if len(chunk) < max(8, WIN // 3):
                continue
            m = sum(chunk) / len(chunk)
            t = nw_t(chunk)
            pts.append({"start": dates[j], "end": dates[j + WIN - 1],
                        "exc": round(m, 2), "t": round(t, 2) if t else None,
                        "n": len(chunk),
                        "win": round(sum(1 for x in chunk if x > 0)
                                     * 100.0 / len(chunk), 1)})
        res["roll"]["h%d" % h] = pts

    print("=== fund 头20% 超额 · 30日滚动窗口 ===")
    print("T+20（最关键的持有期）")
    print("  %-12s %-12s %8s %7s %7s %5s"
          % ("窗口起", "窗口止", "超额%", "t", "胜率%", "n"))
    sel = res["roll"]["h20"]
    step = max(1, len(sel) // 14)
    for p in sel[::step]:
        print("  %-12s %-12s %+8.2f %7s %7.0f %5d"
              % (p["start"], p["end"], p["exc"], p["t"], p["win"], p["n"]))
    for h in (1, 5):
        print("\nT+%d" % h)
        sel2 = res["roll"]["h%d" % h]
        step2 = max(1, len(sel2) // 8)
        for p in sel2[::step2]:
            print("  %-12s %-12s %+8.2f %7s %7.0f %5d"
                  % (p["start"], p["end"], p["exc"], p["t"], p["win"], p["n"]))

    # 衰减趋势：最后 5 个窗口 vs 前 5 个窗口
    print("\n=== 衰减趋势判定 ===")
    summary = {}
    for h in (1, 5, 20):
        sel3 = res["roll"]["h%d" % h]
        if len(sel3) < 10:
            continue
        first = sel3[:5]
        last = sel3[-5:]
        mf = sum(p["exc"] for p in first) / 5
        ml = sum(p["exc"] for p in last) / 5
        summary["h%d" % h] = {"early5": round(mf, 2), "late5": round(ml, 2),
                              "decay": round(ml - mf, 2)}
        print("  T+%-3d 早5窗均值 %+.2f%%  晚5窗均值 %+.2f%%  变化 %+.2f pct"
              % (h, mf, ml, ml - mf))
    res["decay_summary"] = summary

    # 正超额窗口占比
    print("\n=== 正超额窗口占比 ===")
    for h in (1, 5, 20):
        sel4 = res["roll"]["h%d" % h]
        if sel4:
            pos = sum(1 for p in sel4 if p["exc"] > 0)
            print("  T+%-3d %d/%d = %.0f%%"
                  % (h, pos, len(sel4), pos * 100.0 / len(sel4)))

    json.dump(res, open("audit_round6.json", "w"), ensure_ascii=False, indent=2)
    print("\n-> audit_round6.json")


if __name__ == "__main__":
    main()
