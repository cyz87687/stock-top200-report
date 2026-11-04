#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""#293 质量维 NaN 污染审计 —— 修复前后 quality 的真实 IC 对比。

背景（老大 2026-10-27 质询）：
  排名第 2 的威海广泰（sz002111）质量分 8.74 / 估值分 8.79 接近满分，
  但 ROE 仅 4.25%、单季归母净利同比 −92.90%。
  追查发现 fund_split 有 NaN 击穿，导致 score_quality 分数依赖**输入行序**。

本脚本回答三个问题：
  Q1 污染有多严重？（行序扰动下同股分数极差）
  Q2 NaN 从哪条链路进来？（子因子 / 族分 / 分位）
  Q3 修复后 quality 的 IC 是变好还是变差？→ 决定 8 维 vs 4 维怎么选

⚠️ 只读 + 只在临时文件写；不改 fund_split.py，不动线上 scored。
"""
import argparse
import copy
import json
import math
import os
import random
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import fund_split as FS          # noqa: E402
import factor_eval as fe         # noqa: E402


def is_num(x):
    return isinstance(x, (int, float)) and not isinstance(x, bool) and x == x and not math.isinf(x)


# ---------------------------------------------------------------- Q1 行序扰动
def q1_order_dependence(rows, em, n_seed=8):
    codes = [r["code"] for r in rows]
    seqs = [codes]
    for s in range(n_seed):
        c = codes[:]
        random.Random(s).shuffle(c)
        seqs.append(c)

    mats = []
    for seq in seqs:
        buf = [{"code": c} for c in seq]
        FS.score_rows(buf, "2026-10-26", em=em)
        mats.append({x["code"]: x.get("score_quality") for x in buf})

    rng, share2, share5 = [], 0, 0
    for c in codes:
        vs = [m.get(c) for m in mats]
        vs = [v for v in vs if is_num(v)]
        if len(vs) < 2:
            continue
        d = max(vs) - min(vs)
        rng.append(d)
        if d > 2:
            share2 += 1
        if d > 5:
            share5 += 1
    rng.sort()
    tot = len(rng)
    wg = [m.get("sz002111") for m in mats]
    return {
        "n_compared": tot,
        "range_min": rng[0] if rng else None,
        "range_median": rng[tot // 2] if rng else None,
        "range_p90": rng[int(tot * 0.9)] if rng else None,
        "range_max": rng[-1] if rng else None,
        "share_gt2": share2 / tot if tot else None,
        "share_gt5": share5 / tot if tot else None,
        "weihai_samples": wg,
    }


# ---------------------------------------------------------------- Q2 NaN 链路
def q2_nan_trace(em):
    n_rows = n_sub_nan = n_qf_nan = n_gf_nan = 0
    kinds = {}
    for code, ind in em.items():
        periods = sorted({p for dd in ind.values() for p in dd}, reverse=True)
        P = FS.pit_period(periods, "2026-10-26")
        if not P:
            continue
        n_rows += 1
        sub = FS.sub_factors_pit(ind, P)
        for k, v in sub.items():
            if isinstance(v, float) and (math.isnan(v) or math.isinf(v)):
                n_sub_nan += 1
                kinds[k] = kinds.get(k, 0) + 1
        qf, gf = FS.family_scores(sub)
        if isinstance(qf, float) and math.isnan(qf):
            n_qf_nan += 1
        if isinstance(gf, float) and math.isnan(gf):
            n_gf_nan += 1
    return {"n_companies": n_rows, "n_sub_nan": n_sub_nan,
            "n_qf_nan": n_qf_nan, "n_gf_nan": n_gf_nan,
            "nan_by_sub": dict(sorted(kinds.items(), key=lambda kv: -kv[1]))}


# ---------------------------------------------------------------- 修复版打分
def patched_sub(ind, P, _orig=FS.sub_factors_pit):
    s = _orig(ind, P)
    return {k: (v if is_num(v) else None) for k, v in s.items()}


def score_all(rows, em, clean):
    """返回 {code6: score_quality}，确定性（不依赖行序）。"""
    fn = patched_sub if clean else None
    out = {}
    for r in rows:
        c = (r.get("code") or "")
        c6 = c[2:] if c[:2] in ("sh", "sz", "bj") else c
        ind = em.get(c6)
        if not ind:
            continue
        periods = sorted({p for dd in ind.values() for p in dd}, reverse=True)
        P = FS.pit_period(periods, "2026-10-26")
        if not P:
            continue
        sub = fn(ind, P) if fn else FS.sub_factors_pit(ind, P)
        qf, _ = FS.family_scores(sub)
        out[c6] = qf if is_num(qf) else None
    # 整池分位（与 score_rows 同口径）
    present = [(c, v) for c, v in out.items() if is_num(v)]
    present.sort(key=lambda kv: kv[1])
    n = len(present)
    res = {c: None for c in out}
    i = 0
    while i < n:
        j = i
        while j + 1 < n and present[j + 1][1] == present[i][1]:
            j += 1
        avg = (i + j) / 2.0
        for k in range(i, j + 1):
            res[present[k][0]] = round(avg / max(1, n - 1) * 10.0, 2)
        i = j + 1
    return res


# ---------------------------------------------------------------- Q3 真实 IC
def q3_ic(panel, px, tdays, tidx, qmap_old, qmap_new, hop):
    def fwd_ret(c, d0):
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

    dates = [d for d in panel["dates"]
             if tidx.get(d) is not None and tidx[d] + hop < len(tdays)]
    out = {}
    for tag, qmap in (("old", qmap_old), ("new", qmap_new)):
        ics = []
        for d0 in dates:
            cur = panel["rows"].get(d0) or {}
            xs, ys = [], []
            for c, v in cur.items():
                q = qmap.get(c)
                if not is_num(q):
                    continue
                fr = fwd_ret(c, d0)
                if fr is None:
                    continue
                xs.append(q)
                ys.append(fr)
            if len(xs) >= 40:
                ic = fe.rank_ic(xs, ys)
                if ic is not None:
                    ics.append(ic)
        if len(ics) >= 5:
            mu = sum(ics) / len(ics)
            sd = (sum((x - mu) ** 2 for x in ics) / (len(ics) - 1)) ** 0.5
            out[tag] = {"ic": mu, "n": len(ics), "t": mu / (sd / len(ics) ** 0.5) if sd > 0 else None}
        else:
            out[tag] = {"ic": None, "n": len(ics), "t": None}
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--panel", default=os.path.join(HERE, "data", "panel_round30.json"))
    ap.add_argument("--scored", default=os.path.join(HERE, "top200_scored_2026-10-27.json"))
    ap.add_argument("--out", default=os.path.join(HERE, "audit_round35.json"))
    args = ap.parse_args()

    d = json.load(open(args.scored, encoding="utf-8"))
    rows = d["results"]
    em = FS.load_em_cache()

    print("=" * 100)
    print("【Q1】行序扰动：同一数据、同一日期，仅打乱输入顺序")
    print("=" * 100)
    r1 = q1_order_dependence(rows, em)
    print(f"  可比股票 {r1['n_compared']} 只")
    print(f"  分数极差  中位 {r1['range_median']:.2f} 分 · p90 {r1['range_p90']:.2f} · "
          f"max {r1['range_max']:.2f}")
    print(f"  极差>2 分 {r1['share_gt2']*100:.1f}% · 极差>5 分 {r1['share_gt5']*100:.1f}%")
    print(f"  威海广泰 9 次不同行序的质量分: {r1['weihai_samples']}")

    print()
    print("=" * 100)
    print("【Q2】NaN 来源链路")
    print("=" * 100)
    r2 = q2_nan_trace(em)
    print(f"  有 PIT 报告期公司 {r2['n_companies']}")
    print(f"  子因子 NaN/inf 共 {r2['n_sub_nan']} 个；族分为 NaN：quality {r2['n_qf_nan']} "
          f"/ growth {r2['n_gf_nan']}")
    print(f"  NaN 分布（按子因子）: {r2['nan_by_sub']}")

    print()
    print("=" * 100)
    print("【Q3】修复前后 quality 的真实 IC（历史面板）")
    print("=" * 100)
    panel = json.load(open(args.panel, encoding="utf-8"))
    import audit_round34 as A34
    px = A34.load_px()
    tdays = A34.build_tdays(px)
    tidx = {x: i for i, x in enumerate(tdays)}
    print(f"  面板 {len(panel['dates'])} 截面 · 交易日 {len(tdays)} 个 "
          f"({tdays[0]} ~ {tdays[-1]})")

    codes_hist = set()
    for d0 in panel["dates"]:
        codes_hist.update((panel["rows"].get(d0) or {}).keys())
    hist_rows = [{"code": "sh" + c if c[0] == "6" else "sz" + c} for c in codes_hist]

    q_new_hist = score_all(hist_rows, em, clean=True)
    q_old_hist = score_all(hist_rows, em, clean=False)

    r3 = {}
    for hop in (5, 20):
        r3[f"hop{hop}"] = q3_ic(panel, px, tdays, tidx, q_old_hist, q_new_hist, hop)
        s = r3[f"hop{hop}"]
        print(f"  h{hop:<3d} 修复前 IC {s['old']['ic'] if s['old']['ic'] is not None else float('nan'):+.4f} "
              f"(t {s['old']['t'] if s['old']['t'] is not None else float('nan'):+.2f}, n={s['old']['n']})"
              f"   →   修复后 IC {s['new']['ic'] if s['new']['ic'] is not None else float('nan'):+.4f} "
              f"(t {s['new']['t'] if s['new']['t'] is not None else float('nan'):+.2f}, n={s['new']['n']})")

    out = {"q1_order_dependence": r1, "q2_nan_trace": r2, "q3_ic": r3}
    json.dump(out, open(args.out, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"\n写入 {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
