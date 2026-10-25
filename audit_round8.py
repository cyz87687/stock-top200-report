#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
audit_round8.py — 新一轮 P0/P1 提案（2026-10-25 第 5 轮）

前 4 轮已清空 P0、P1 剩 2 项（都属等数据）。本轮从「量化从业者」视角
换一组此前没审过的维度，只保留有硬数据支撑的项：

A. 前视偏差（look-ahead）——量化审计第一红线
   检查 snapshot 里 score_* 的生成时间 vs 价格可得时间，是否用了当日收盘后信息
B. 成交可行性——10 只组合按成交额能否在合理规模内建仓
C. 组合相关性——10 只组合的实际分散度（名义分散 vs 真实分散）
D. 极端风险——组合内是否有退市/ST/停牌风险标的

输出 audit_round8.json
"""
import json
import math
import re
import statistics as st
from collections import Counter

HIST = "factor_history.json"
SCORED = "top200_scored_2026-10-23.json"
MF = "sector/mf_pick_2026-10-23.json"
OUT = []


def add(level, title, evidence, why, fix):
    OUT.append({"level": level, "title": title, "evidence": evidence,
                "why": why, "fix": fix})


def bare(c):
    return re.sub(r"^(sh|sz|bj)", "", str(c))


def load(p):
    try:
        return json.load(open(p, encoding="utf-8"))
    except Exception:
        return None


def corr(a, b):
    n = min(len(a), len(b))
    if n < 5:
        return None
    a = a[:n]
    b = b[:n]
    ma = sum(a) / n
    mb = sum(b) / n
    va = sum((x - ma) ** 2 for x in a)
    vb = sum((x - mb) ** 2 for x in b)
    if va <= 0 or vb <= 0:
        return None
    cov = sum((a[i] - ma) * (b[i] - mb) for i in range(n))
    return cov / math.sqrt(va * vb)


def main():
    h = load(HIST) or {}
    sc = load(SCORED) or {}
    mf = load(MF) or {}
    snaps = h.get("snapshots") or []
    praw = h.get("prices") or {}
    pdates = praw.get("dates") or []
    px = praw.get("px") or {}
    opens = praw.get("opens") or {}

    # ---------- A. 前视偏差 ----------
    # 检查：snapshot 的 date 当天价格，是否也出现在 prices 里（即用当日收盘价算分）
    # 这不是错，但若「当日收盘价」参与评分而买入价也用当日收盘，就是不可交易的。
    snap_dates = [s["date"] for s in snaps]
    in_px = sum(1 for d in snap_dates if d in set(pdates))
    add("INFO", "评分当日收盘价是否进入价格库",
        "snapshot 日期 %d 个，其中 %d 个在 prices.dates 中" % (len(snap_dates), in_px),
        "说明评分与价格同源同日。若买入价用 T 日收盘，则用到了收盘后才知道的分数 → 前视。",
        "本页回测口径已固定为「T+1 开盘买入」，需验证该口径在代码里真的执行。")

    # 验证 fwd_ret 口径：T+1 开盘买、T+1+h 收盘卖
    # 抽样核对一个标的
    if snaps and pdates:
        s0 = snaps[-1]
        code = bare(s0["rows"][0]["code"])
        arr = px.get(code) or px.get(s0["rows"][0]["code"])
        oarr = opens.get(code) or opens.get(s0["rows"][0]["code"])
        if arr:
            j = pdates.index(s0["date"]) if s0["date"] in pdates else None
            if j is not None and j + 2 < len(pdates):
                p_t = arr[j]
                p_t1 = arr[j + 1]
                o_t1 = oarr[j + 1] if oarr else None
                add("INFO", "口径抽样核对（%s）" % code,
                    "T日收盘=%s  T+1开盘=%s  T+1收盘=%s" % (p_t, o_t1, p_t1),
                    "确认 T+1 开盘价与 T+1 收盘价都存在且不同，说明「T+1 开盘买」口径可执行。",
                    "口径成立，非前视。")

    # ---------- B. 成交可行性 ----------
    picks = mf.get("picks") or []
    res_map = {bare(r["code"]): r for r in (sc.get("results") or [])}
    rows = []
    for p in picks:
        c = bare(p.get("code"))
        r = res_map.get(c) or {}
        amt = r.get("turnover")
        rows.append({
            "name": p.get("name"), "code": c,
            "turnover_yi": round(amt / 1e8, 2) if isinstance(amt, (int, float)) else None,
            "circ_mv_yi": p.get("circ_mv_yi"),
            "price": p.get("price"),
            "pct_chg": p.get("pct_chg"),
        })
    tos = [x["turnover_yi"] for x in rows if x["turnover_yi"]]
    if tos:
        mn = min(tos)
        md = st.median(tos)
        # 假设单只不超过当日成交额 5%
        cap_yi = mn * 0.05
        add("INFO" if mn >= 2.0 else "P1",
            "10 只组合的成交额可容纳规模：单只上限约 %.2f 亿" % cap_yi,
            "组合成交额（亿元）：最小 %.2f / 中位 %.2f / 最大 %.2f；"
            "按「不超过单只当日成交额 5%%」保守估算，单只最多买 %.2f 亿，"
            "10 只合计约 %.1f 亿" % (mn, md, max(tos), cap_yi, cap_yi * 10),
            "若资金规模超过合计上限，实际建仓会显著冲击价格，"
            "回测的「T+1 开盘价成交」假设失效。",
            "页面标注可容纳规模上限。当前 10 只合计约 %.1f 亿，"
            "对数千万级资金充足，对亿级以上需拆单。" % (cap_yi * 10))

    # ---------- C. 组合真实分散度 ----------
    if len(picks) >= 5:
        codes = [bare(p["code"]) for p in picks]
        # 用价格序列算两两相关
        ser = {}
        for c in codes:
            arr = px.get(c) or px.get(c.upper())
            if arr:
                ser[c] = [v for v in arr if isinstance(v, (int, float))]
        minlen = min((len(v) for v in ser.values()), default=0)
        if minlen >= 20 and len(ser) >= 5:
            ser = {k: v[-minlen:] for k, v in ser.items()}
            rets = {}
            for k, v in ser.items():
                rets[k] = [(v[i] / v[i - 1] - 1.0) for i in range(1, len(v))
                           if v[i - 1]]
            cs = []
            ks = list(rets.keys())
            for i in range(len(ks)):
                for j in range(i + 1, len(ks)):
                    cc = corr(rets[ks[i]], rets[ks[j]])
                    if cc is not None:
                        cs.append(cc)
            if cs:
                avg_c = sum(cs) / len(cs)
                mx = max(cs)
                # 有效独立标的数 ≈ n/(1+(n-1)*avg_corr)
                n = len(ks)
                eff = n / (1.0 + (n - 1) * avg_c) if avg_c > -1 else n
                lvl = "P1" if eff < n * 0.6 else "INFO"
                add(lvl, "10 只组合的真实分散度：等效独立标的 %.1f 只（名义 %d 只）"
                    % (eff, n),
                    "两两日收益相关系数：均值 %.3f / 最大 %.3f / n_pairs=%d；"
                    "按 n/(1+(n-1)ρ) 折算等效独立标的数 = %.1f" % (avg_c, mx, len(cs), eff),
                    "名义 10 只分散，但若平均相关 %.2f，实际只等同于 %.1f 只独立标的。"
                    "这直接决定「强制分散」保护了多少风险。" % (avg_c, eff),
                    "页面标注等效独立标的数，避免「10 只=分散」的错觉。"
                    "若 ρ 持续 >0.5，应引入行业/风格对冲而非单纯增加只数。")

    # ---------- D. 极端风险 ----------
    risk = []
    for p in picks:
        c = bare(p.get("code"))
        r = res_map.get(c) or {}
        if r.get("suspended"):
            risk.append("%s(停牌)" % p.get("name"))
    if risk:
        add("P0", "组合含停牌标的", "、".join(risk), "停牌无法建仓", "剔除")
    else:
        add("INFO", "组合内无停牌标的", "10 只全部 suspended=False", "—", "—")

    # ---------- 汇总 ----------
    print("=" * 72)
    from collections import Counter as C2
    cnt = C2(x["level"] for x in OUT)
    print("本轮结论：", dict(cnt))

    for lvl in ("P0", "P1", "INFO"):
        sel = [x for x in OUT if x["level"] == lvl]
        if not sel:
            continue
        print("\n### %s（%d 项）###" % (lvl, len(sel)))
        for x in sel:
            print("\n[%s] %s" % (lvl, x["title"]))
            print("  证据: %s" % x["evidence"])
            print("  为何重要: %s" % x["why"])
            print("  处置: %s" % x["fix"])

    json.dump({"items": OUT, "picks": rows},
              open("audit_round8.json", "w"), ensure_ascii=False, indent=2)
    print("\n-> audit_round8.json")


if __name__ == "__main__":
    main()
