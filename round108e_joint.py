#!/usr/bin/env python3
"""
round108e｜规则1+2 联合效应检验（决定性）
==========================================
round108b/c/d 三个单规则结论互相矛盾，本脚本回答唯一重要的问题：
  「周期标签 × 价格驱动 × 高加速」这个**联合条件**是否才真正识别不可持续？

设计（全部限强周期组，规则生效域内）：
  因子：
    P = 毛利率抬升 > 阈值（价格驱动代理，round108c 已证 lift 1.26~1.38）
    A = 去均值 accel ≥ 组内 p80（高加速）
    C = 周期标签（本组全为 True，故与组外对照）
  效标：t+1 期 G_q 相对 t 期回落；以及 t+1 期 accel 去均值 < 0

  六格交叉 P×A，输出退坡率 / lift，看哪一格才真正有区分度。
  再算「组合得分」：把 lift 排序，判断哪套条件能给出足够样本量（≥100）且 lift 最大。

输出：data/round108e_joint.json
"""
import json
import os
from collections import defaultdict

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
CACHE = os.path.join(HERE, "data", "fin_cache_em")
SW_MAP = os.path.join(HERE, "sector", "sw_map.json")
OUT = os.path.join(HERE, "data", "round108e_joint.json")

Q_END = ("0331", "0630", "0930", "1231")
CYCLE_L2 = {
    "工业金属", "小金属", "金属新材料", "航运港口", "煤炭开采", "焦炭Ⅱ", "焦炭",
    "化学原料", "化学纤维", "造纸", "养殖业", "冶钢原料", "普钢", "特钢Ⅱ", "特钢",
    "油气开采Ⅱ", "油气开采", "油服工程", "炼化及贸易", "农化制品", "贵金属", "能源金属",
}


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
    code2l2 = (json.load(open(SW_MAP, encoding="utf-8")).get("l2") or {})
    files = [f for f in os.listdir(CACHE) if f.endswith(".json")]

    store = {}
    for fn in files:
        code = fn[:-5]
        try:
            d = json.load(open(os.path.join(CACHE, fn), encoding="utf-8"))
        except Exception:
            continue
        recs = d.get("records") or []
        if not recs:
            continue
        rev = get_series(recs, "营业总收入")
        cost = get_series(recs, "营业成本")
        kf = get_series(recs, "扣非净利润")
        if not (rev and cost and kf):
            continue
        l2 = code2l2.get(code)
        sq_rev, sq_cost, sq_kf = single_quarter(rev), single_quarter(cost), single_quarter(kf)
        gm = {p: (sq_rev[p] - sq_cost[p]) / sq_rev[p] * 100.0
              for p in sq_rev if p in sq_cost and sq_rev[p] > 1e4}
        gps = sorted(gm.keys())
        per = {}
        for j, p in enumerate(gps):
            r = {}
            if j >= 7:
                r["gm_rise"] = float(np.mean([gm[q] for q in gps[j-3:j+1]])) - \
                               float(np.mean([gm[q] for q in gps[j-7:j-3]]))
            ply = f"{int(p[:4])-1}{p[4:]}"
            if p in sq_rev and ply in sq_rev and sq_rev[ply] > 1e4:
                r["rev_yoy"] = sq_rev[p] / sq_rev[ply] - 1.0
            p1 = prev_q(p)
            ply1 = f"{int(p1[:4])-1}{p1[4:]}"
            if (p in sq_kf and ply in sq_kf and sq_kf[ply] > 0
                    and p1 in sq_kf and ply1 in sq_kf and sq_kf[ply1] > 0):
                gq = sq_kf[p] / sq_kf[ply] - 1.0
                gq1 = sq_kf[p1] / sq_kf[ply1] - 1.0
                r["gq"] = gq
                r["accel"] = (gq - gq1) * 100.0
            per[p] = r
        if per:
            store[code] = (per, l2, bool(l2 and l2 in CYCLE_L2))
    print(f"[load] {len(store)} 只（周期 {sum(1 for v in store.values() if v[2])}）", flush=True)

    # 缩尾 + 按期去均值
    allacc = [f["accel"] for per, _, _ in store.values() for f in per.values() if "accel" in f]
    lo, hi = np.percentile(allacc, 1), np.percentile(allacc, 99)
    by_p = defaultdict(list)
    for per, _, _ in store.values():
        for p, f in per.items():
            if "accel" in f:
                by_p[p].append(float(np.clip(f["accel"], lo, hi)))
    mu = {p: float(np.mean(v)) for p, v in by_p.items()}

    # 配对（仅周期组，规则生效域）
    pairs = []
    for code, (per, l2, cyc) in store.items():
        if not cyc:
            continue
        ps = sorted(per.keys())
        for i in range(len(ps) - 1):
            p, p1 = ps[i], ps[i + 1]
            if prev_q(p1) != p:
                continue
            f, f1 = per[p], per[p1]
            if "gq" not in f or "gq" not in f1 or "accel" not in f:
                continue
            dm = float(np.clip(f["accel"], lo, hi)) - mu.get(p, 0.0)
            dm1 = (float(np.clip(f1["accel"], lo, hi)) - mu.get(p1, 0.0)) if "accel" in f1 else None
            pairs.append({"code": code, "l2": l2, "p": p,
                          "gm_rise": f.get("gm_rise"), "rev_yoy": f.get("rev_yoy"),
                          "accel": f["accel"], "dm": dm, "dm1": dm1,
                          "gq": f["gq"], "gq1": f1["gq"]})
    print(f"[pairs] 周期组配对 {len(pairs)} 条", flush=True)

    base_dec = np.mean([1 if r["gq1"] < r["gq"] else 0 for r in pairs])
    base_hard = np.mean([1 if r["gq1"] < r["gq"] - 0.20 else 0 for r in pairs])
    base_acc_neg = np.mean([1 if (r["dm1"] is not None and r["dm1"] < 0) else 0
                            for r in pairs if r["dm1"] is not None])
    thr_acc = float(np.percentile([r["dm"] for r in pairs], 80))
    print(f"[base] 退坡 {base_dec*100:.1f}% 硬退坡 {base_hard*100:.1f}% "
          f"次期accel<0 {base_acc_neg*100:.1f}% | accel p80={thr_acc:.1f}", flush=True)

    def cell(filt, label):
        a = [r for r in pairs if filt(r)]
        if len(a) < 25:
            return {"cell": label, "n": len(a), "insufficient": True}
        dec = np.mean([1 if r["gq1"] < r["gq"] else 0 for r in a])
        hard = np.mean([1 if r["gq1"] < r["gq"] - 0.20 else 0 for r in a])
        an = [r for r in a if r["dm1"] is not None]
        accn = np.mean([1 if r["dm1"] < 0 else 0 for r in an]) if an else None
        return {"cell": label, "n": len(a),
                "decline_pct": round(float(dec) * 100, 1),
                "lift": round(float(dec / base_dec), 3),
                "hard_pct": round(float(hard) * 100, 1),
                "hard_lift": round(float(hard / base_hard), 3),
                "next_accel_neg_pct": round(float(accn) * 100, 1) if accn is not None else None,
                "next_accel_neg_lift": round(float(accn / base_acc_neg), 3) if accn is not None else None}

    P = lambda r, t=3.0: (r["gm_rise"] is not None and r["gm_rise"] > t)
    A = lambda r: r["dm"] >= thr_acc
    R = lambda r, t=0.0: (r["rev_yoy"] is not None and r["rev_yoy"] < t)

    grid = []
    grid.append(cell(lambda r: True, "全组基线（周期）"))
    grid.append(cell(lambda r: P(r) and A(r), "P&A  毛利抬升>3 且 高加速"))
    grid.append(cell(lambda r: P(r), "P    仅毛利抬升>3"))
    grid.append(cell(lambda r: A(r), "A    仅高加速"))
    grid.append(cell(lambda r: (not P(r)) and A(r), "¬P&A 高加速但非价格驱动"))
    grid.append(cell(lambda r: P(r) and (not A(r)), "P&¬A 价格驱动但未加速"))
    grid.append(cell(lambda r: P(r) and A(r) and R(r), "P&A&R 三项全中"))
    grid.append(cell(lambda r: P(r) and R(r), "P&R 价格驱动+营收负"))
    grid.append(cell(lambda r: P(r, 5.0) and A(r), "P(>5)&A 强价格驱动+高加速"))

    json.dump({"date": "2026-11-02", "n_pairs_cycle": len(pairs),
               "base": {"decline_pct": round(float(base_dec) * 100, 1),
                        "hard_pct": round(float(base_hard) * 100, 1),
                        "next_accel_neg_pct": round(float(base_acc_neg) * 100, 1)},
               "thr_accel_p80": round(thr_acc, 2),
               "winsor": [round(float(lo), 2), round(float(hi), 2)],
               "grid": grid},
              open(OUT, "w", encoding="utf-8"), ensure_ascii=False, indent=1)

    print("\n===== 规则1+2 联合交叉（周期组）=====")
    print(f"{'格':28s} {'n':>6s} {'退坡%':>7s} {'lift':>6s} {'硬退坡%':>8s} {'hlift':>6s} {'次期acc<0%':>10s} {'alift':>6s}")
    for g in grid:
        if g.get("insufficient"):
            print(f"{g['cell']:28s} {g['n']:>6d}  (样本不足)")
            continue
        print(f"{g['cell']:28s} {g['n']:>6d} {g['decline_pct']:>7} {g['lift']:>6} "
              f"{g['hard_pct']:>8} {g['hard_lift']:>6} {str(g['next_accel_neg_pct']):>10} "
              f"{str(g['next_accel_neg_lift']):>6}")
    print(f"\n[done] → {OUT}")


if __name__ == "__main__":
    main()
