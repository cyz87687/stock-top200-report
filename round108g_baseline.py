#!/usr/bin/env python3
"""
round108g｜生成规则2 去均值基线（生产可复现）
================================================
问题：规则2 用「按期去均值后的 ΔG_q ≥ p80」判高加速。日更时只有当期一个截面，
     无法在当期算出「该报告期的全池均值 mu」——必须预生成历史基线。

口径（与 round108d 标定严格一致）：
  mu[period] = 该报告期**全池**（所有有 ΔG_q 的股票，不分周期）ΔG_q 缩尾后均值
  dm          = clip(accel, p1, p99) − mu[period]
  p80 阈值    = 周期组 dm 分布的 p80（标定值，全局常数）

生产调用：
  base = json.load('data/cycle_accel_baseline.json')
  mu_t = base['mu_by_period'].get(period) or base['global_mu_fallback']
  dm   = clip(accel, base['winsor'][0], base['winsor'][1]) - mu_t
  if dm >= base['p80_demeaned_cycle']: 触发规则2

输出：data/cycle_accel_baseline.json
"""
import json
import os
from collections import defaultdict

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
CACHE = os.path.join(HERE, "data", "fin_cache_em")
SW_MAP = os.path.join(HERE, "sector", "sw_map.json")
CYCLE_CFG = os.path.join(HERE, "sector", "cycle_industry.json")
OUT = os.path.join(HERE, "data", "cycle_accel_baseline.json")

Q_END = ("0331", "0630", "0930", "1231")
PERIOD = (Q_END)


def get_series(recs, name):
    for r in recs:
        if r.get("指标") == name:
            out = {}
            for k, v in r.items():
                if k in ("选项", "指标"):
                    continue
                if len(k) == 8 and k.isdigit() and k[4:] in Q_END:
                    if isinstance(v, (int, float)) and np.isfinite(v):
                        out[k] = float(v)
            if out:
                return out
    return {}


def single_quarter(cum):
    by_year = defaultdict(dict)
    for p, v in cum.items():
        by_year[p[:4]][p[4:]] = v
    sq = {}
    for y, qs in by_year.items():
        prev = 0.0
        for q in Q_END:
            if q not in qs:
                continue
            sq[y + q] = qs[q] - prev
            prev = qs[q]
    return sq


def prev_q(p):
    y, q = int(p[:4]), p[4:]
    i = Q_END.index(q)
    return f"{y-1}1231" if i == 0 else f"{y}{Q_END[i-1]}"


def main():
    ci = json.load(open(CYCLE_CFG, encoding="utf-8"))
    cycle_l2 = set(ci["l2_list"])
    code2l2 = (json.load(open(SW_MAP, encoding="utf-8")).get("l2") or {})

    accel_by_code = {}
    for fn in [f for f in os.listdir(CACHE) if f.endswith(".json")]:
        code = fn[:-5]
        try:
            d = json.load(open(os.path.join(CACHE, fn), encoding="utf-8"))
        except Exception:
            continue
        recs = d.get("records") or []
        kf = get_series(recs, "扣非净利润") if recs else {}
        if not kf:
            continue
        sq = single_quarter(kf)
        ser = {}
        for p in sorted(sq.keys()):
            ply = f"{int(p[:4])-1}{p[4:]}"
            p1 = prev_q(p)
            ply1 = f"{int(p1[:4])-1}{p1[4:]}"
            if ply not in sq or sq[ply] <= 0:
                continue
            if p1 not in sq or ply1 not in sq or sq[ply1] <= 0:
                continue
            gq = sq[p] / sq[ply] - 1.0
            gq1 = sq[p1] / sq[ply1] - 1.0
            ser[p] = (gq - gq1) * 100.0
        if ser:
            accel_by_code[code] = ser
    print(f"[load] {len(accel_by_code)} 只", flush=True)

    allv = [a for ser in accel_by_code.values() for a in ser.values()]
    lo, hi = float(np.percentile(allv, 1)), float(np.percentile(allv, 99))
    print(f"[winsor] p1={lo:.2f} p99={hi:.2f}", flush=True)

    by_p = defaultdict(list)
    for ser in accel_by_code.values():
        for p, a in ser.items():
            by_p[p].append(float(np.clip(a, lo, hi)))
    mu_by_period = {p: round(float(np.mean(v)), 4) for p, v in sorted(by_p.items())}
    n_by_period = {p: len(v) for p, v in sorted(by_p.items())}
    print(f"[mu] {len(mu_by_period)} 个报告期", flush=True)
    for p in sorted(mu_by_period)[-6:]:
        print(f"  {p} mu={mu_by_period[p]:8.2f} n={n_by_period[p]}")

    # 周期组 dm 分布 → p80
    cyc_dm = []
    for code, ser in accel_by_code.items():
        l2 = code2l2.get(code)
        if not l2 or l2 not in cycle_l2:
            continue
        for p, a in ser.items():
            cyc_dm.append(float(np.clip(a, lo, hi)) - mu_by_period[p])
    p80 = float(np.percentile(cyc_dm, 80))
    print(f"\n[cycle] dm 样本 {len(cyc_dm)}  p80={p80:.2f}", flush=True)
    for q in (50, 60, 70, 80, 90, 95):
        print(f"  p{q} = {np.percentile(cyc_dm, q):8.2f}")

    result = {
        "version": "round108-2026-11-02",
        "note": "规则2 去均值基线。mu[period]=该报告期全池 ΔG_q 缩尾后均值（不分周期）；"
                "dm=clip(accel,winsor)-mu；触发条件 dm>=p80_demeaned_cycle。"
                "标定来源 round108d_rule2_calib.py（强周期组 277 只 / 11189 配对）。",
        "winsor": [round(lo, 2), round(hi, 2)],
        "mu_by_period": mu_by_period,
        "n_by_period": n_by_period,
        "n_codes": len(accel_by_code),
        "p80_demeaned_cycle": round(p80, 2),
        "cycle_dm_dist": {f"p{q}": round(float(np.percentile(cyc_dm, q)), 2)
                          for q in (10, 25, 50, 60, 70, 80, 90, 95, 99)},
        "global_mu_fallback": round(float(np.mean([float(np.clip(a, lo, hi))
                                                   for ser in accel_by_code.values()
                                                   for a in ser.values()])), 4),
        "periods_covered": sorted(mu_by_period.keys()),
    }
    json.dump(result, open(OUT, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"\n[global_mu_fallback] {result['global_mu_fallback']}")
    print(f"[done] → {OUT}")


if __name__ == "__main__":
    main()
