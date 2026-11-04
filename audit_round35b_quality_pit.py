#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""#293b 按截面日正确重算 quality → 修复前后真实 IC。

⚠️ audit_round35.py 的 Q3 有方法错误：用固定 as_of="2026-10-26" 给所有历史截面打分，
   导致每只股票的 quality 是一个跨期常数，IC 失去时序含义。本脚本按**每个截面日**
   独立做 PIT（as_of=d0），与生产 score_rows 口径一致。

回答：NaN 修复后，quality 这一维还有没有 alpha？
  · 若修复后 IC 显著为正 → 保留该维（8 维）
  · 若修复后 IC 仍为零/负 → 该维无 alpha，4 维（等权放大 25%）不可取，应降权或归零
"""
import json
import math
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import fund_split as FS      # noqa: E402
import factor_eval as fe     # noqa: E402
import audit_round34 as A34  # noqa: E402


def is_num(x):
    return isinstance(x, (int, float)) and not isinstance(x, bool) and x == x and not math.isinf(x)


def _orig_sub(ind, P):
    return FS.sub_factors_pit(ind, P)


def _clean_sub(ind, P):
    s = FS.sub_factors_pit(ind, P)
    return {k: (v if is_num(v) else None) for k, v in s.items()}


def rank10_map(raw):
    """整池 rank 分位 → 0-10（与 fund_split.score_rows 同口径，但输入是 dict 故确定）。"""
    present = [(c, v) for c, v in raw.items() if is_num(v)]
    present.sort(key=lambda kv: kv[1])
    n = len(present)
    out = {c: None for c in raw}
    i = 0
    while i < n:
        j = i
        while j + 1 < n and present[j + 1][1] == present[i][1]:
            j += 1
        avg = (i + j) / 2.0
        for k in range(i, j + 1):
            out[present[k][0]] = round(avg / max(1, n - 1) * 10.0, 2)
        i = j + 1
    return out


def build_qmap(codes, em, dates, subfn):
    """{date: {code6: quality 0-10}}，按每个截面日独立 PIT。"""
    out = {}
    for d0 in dates:
        raw = {}
        for c6 in codes:
            ind = em.get(c6)
            if not ind:
                continue
            periods = sorted({p for dd in ind.values() for p in dd}, reverse=True)
            P = FS.pit_period(periods, d0)
            if not P:
                continue
            qf, _ = FS.family_scores(subfn(ind, P))
            raw[c6] = qf if is_num(qf) else None
        out[d0] = rank10_map(raw)
    return out


def ic_series(panel, px, tdays, tidx, qmaps, hop):
    def fwd(c, d0):
        m = px.get(c)
        i0 = tidx.get(d0)
        if not m or i0 is None:
            return None
        j = i0 + hop
        if j >= len(tdays):
            return None
        p0, p1 = m.get(d0), m.get(tdays[j])
        if p0 and p1 and p0 > 0:
            return p1 / p0 - 1.0
        return None

    ics = []
    for d0, qm in qmaps.items():
        if tidx.get(d0) is None or tidx[d0] + hop >= len(tdays):
            continue
        cur = panel["rows"].get(d0) or {}
        xs, ys = [], []
        for c in cur:
            q = qm.get(c)
            if not is_num(q):
                continue
            r = fwd(c, d0)
            if r is None:
                continue
            xs.append(q)
            ys.append(r)
        if len(xs) >= 40:
            ic = fe.rank_ic(xs, ys)
            if ic is not None:
                ics.append(ic)
    if len(ics) < 5:
        return None
    mu = sum(ics) / len(ics)
    sd = (sum((x - mu) ** 2 for x in ics) / (len(ics) - 1)) ** 0.5
    return {"ic": mu, "n": len(ics), "t": mu / (sd / len(ics) ** 0.5) if sd > 0 else None}


def main():
    scored = json.load(open(os.path.join(HERE, "top200_scored_2026-10-27.json"), encoding="utf-8"))
    panel = json.load(open(os.path.join(HERE, "data", "panel_round30.json"), encoding="utf-8"))
    em = FS.load_em_cache()
    px = A34.load_px()
    tdays = A34.build_tdays(px)
    tidx = {x: i for i, x in enumerate(tdays)}

    codes = set()
    for d0 in panel["dates"]:
        codes.update((panel["rows"].get(d0) or {}).keys())
    codes = sorted(codes)
    print(f"面板域 {len(codes)} 只 · {len(panel['dates'])} 截面 · 交易日 {len(tdays)}")

    print("\n按截面日重算 quality（修复前后各一遍）…")
    q_old = build_qmap(codes, em, panel["dates"], _orig_sub)
    q_new = build_qmap(codes, em, panel["dates"], _clean_sub)

    print("=" * 92)
    print("【quality 真实 IC —— 每截面日独立 PIT】")
    print("=" * 92)
    print(f"  {'hop':>4s} {'修复前IC':>10s} {'t':>7s} {'n':>4s} {'修复后IC':>10s} {'t':>7s} {'n':>4s}")
    res = {}
    for hop in (5, 10, 20, 40):
        a = ic_series(panel, px, tdays, tidx, q_old, hop)
        b = ic_series(panel, px, tdays, tidx, q_new, hop)
        res[f"h{hop}"] = {"old": a, "new": b}
        fa = f"{a['ic']:+.4f}" if a else "n/a"
        ta = f"{a['t']:+.2f}" if a and a["t"] is not None else "n/a"
        na = a["n"] if a else 0
        fb = f"{b['ic']:+.4f}" if b else "n/a"
        tb = f"{b['t']:+.2f}" if b and b["t"] is not None else "n/a"
        nb = b["n"] if b else 0
        print(f"  {hop:>4d} {fa:>10s} {ta:>7s} {na:>4d} {fb:>10s} {tb:>7s} {nb:>4d}")

    # 用当日真实 scored（已含污染的 8.74 等）做对照：面板自带 quality
    print("\n对照：面板内已存 quality（即当前线上口径，含 NaN 污染）")
    for hop in (5, 20):
        ics = []
        for d0 in panel["dates"]:
            if tidx.get(d0) is None or tidx[d0] + hop >= len(tdays):
                continue
            cur = panel["rows"].get(d0) or {}
            xs, ys = [], []
            for c, v in cur.items():
                q = v.get("score_quality")
                if not is_num(q):
                    continue
                m = px.get(c)
                i0 = tidx.get(d0)
                if not m or i0 is None or i0 + hop >= len(tdays):
                    continue
                p0, p1 = m.get(d0), m.get(tdays[i0 + hop])
                if p0 and p1 and p0 > 0:
                    xs.append(q)
                    ys.append(p1 / p0 - 1.0)
            if len(xs) >= 40:
                ic = fe.rank_ic(xs, ys)
                if ic is not None:
                    ics.append(ic)
        if len(ics) >= 5:
            mu = sum(ics) / len(ics)
            sd = (sum((x - mu) ** 2 for x in ics) / (len(ics) - 1)) ** 0.5
            t = mu / (sd / len(ics) ** 0.5) if sd > 0 else None
            print(f"  h{hop:<3d} IC {mu:+.4f}  t {t:+.2f}  n={len(ics)}")
            res.setdefault(f"panel_h{hop}", {"ic": mu, "t": t, "n": len(ics)})

    json.dump(res, open(os.path.join(HERE, "audit_round35b.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    print("\n写入 audit_round35b.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
