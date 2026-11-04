#!/usr/bin/env python3
"""
round108b｜规则1/规则2 交叉验证（对抗性检验）
==============================================
round108 首轮标定出现两个反直觉结果，本脚本做对抗性复核：

A. 规则2：「周期股二阶导更不可持续」——首轮 corr 周期 −0.5565 / 非周期 −0.4819，
   但周期股当期 mean ΔG_q = −84pp（整体深度减速），autocorr 被时间共性污染。
   → 做**按期去均值**（period fixed effect）后重算，才算干净。

B. 控制 accel 水平后，非周期股次季回归反而更猛（−133.99 vs −131.44）。
   → 做 is_cycle × 毛利率波动 × accel 三维交叉，检验「价格驱动 × 高加速」才是
     真正不可持续的组合（老大规则1+规则2 的联合假设）。

C. 规则1：行业历史 p60 口径下周期股命中率 37.2% < 非周期 43.5%。
   → 输出周期行业 p60 明细，检验阈值是否真的贴合周期股。

输出：data/round108b_crosscheck.json
"""
import json
import os
from collections import defaultdict

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
CACHE = os.path.join(HERE, "data", "fin_cache_em")
SW_MAP = os.path.join(HERE, "sector", "sw_map.json")
OUT = os.path.join(HERE, "data", "round108b_crosscheck.json")

Q_END = ("0331", "0630", "0930", "1231")
CYCLE_L2 = {
    "工业金属", "小金属", "金属新材料", "航运港口", "煤炭开采", "焦炭Ⅱ", "焦炭",
    "化学原料", "化学纤维", "造纸", "养殖业", "冶钢原料", "普钢", "特钢Ⅱ", "特钢",
    "油气开采Ⅱ", "油气开采", "油服工程", "炼化及贸易", "农化制品", "贵金属", "能源金属",
}


def load_sw():
    d = json.load(open(SW_MAP, encoding="utf-8"))
    return d.get("l2") or {}, d.get("l2_parent") or {}


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
    code2l2, l2_parent = load_sw()
    files = [f for f in os.listdir(CACHE) if f.endswith(".json")]

    gm_vol = {}      # code -> {period: std4}
    gm_lvl = {}      # code -> {period: mean4 毛利率水平}
    recs_by = {}     # code -> (gq/accel 序列, l2, is_cycle)
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
        is_cyc = bool(l2 and l2 in CYCLE_L2)

        sq_rev, sq_cost = single_quarter(rev), single_quarter(cost)
        gm = {p: (sq_rev[p] - sq_cost[p]) / sq_rev[p] * 100.0
              for p in sq_rev if p in sq_cost and sq_rev[p] > 1e4}
        ps = sorted(gm.keys())
        for j in range(3, len(ps)):
            win = [gm[p] for p in ps[j - 3:j + 1]]
            if len(win) == 4:
                gm_vol.setdefault(code, {})[ps[j]] = float(np.std(win, ddof=1))
                gm_lvl.setdefault(code, {})[ps[j]] = float(np.mean(win))

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
            ser[p] = (gq, (gq - gq1) * 100.0)
        if ser:
            recs_by[code] = (ser, l2, is_cyc)

    print(f"[load] {len(recs_by)} 只有 ΔG_q 序列", flush=True)

    # ---------- A. 按期去均值后的 autocorr ----------
    # 先算每个 period 的组内均值（分 cycle/nocycle），再减掉
    def demean_autocorr(only_cycle):
        raw = []
        for code, (ser, l2, cyc) in recs_by.items():
            if cyc != only_cycle:
                continue
            for p, (gq, acc) in ser.items():
                raw.append((p, acc))
        if len(raw) < 200:
            return {"n": 0}
        by_p = defaultdict(list)
        for p, a in raw:
            by_p[p].append(a)
        mu = {p: float(np.mean(v)) for p, v in by_p.items()}
        xs, ys = [], []
        for code, (ser, l2, cyc) in recs_by.items():
            if cyc != only_cycle:
                continue
            ps = sorted(ser.keys())
            for i in range(len(ps) - 1):
                if prev_q(ps[i + 1]) != ps[i]:
                    continue
                a1 = ser[ps[i]][1] - mu.get(ps[i], 0.0)
                a2 = ser[ps[i + 1]][1] - mu.get(ps[i + 1], 0.0)
                xs.append(a1); ys.append(a2)
        if len(xs) < 50:
            return {"n": len(xs)}
        return {"n": len(xs),
                "corr": round(float(np.corrcoef(xs, ys)[0, 1]), 4),
                "raw_corr": None}

    # ---------- B. is_cycle × 毛利率波动 × accel 三交叉 ----------
    # 用当期截面 p60 阈值切「高波动 / 低波动」（各行业历史口径太脏，改用当期周期组内）
    cur = "20260930"
    cyc_vol = [gm_vol[c][cur] for c in gm_vol
               if c in recs_by and recs_by[c][2] and cur in gm_vol[c]]
    ncyc_vol = [gm_vol[c][cur] for c in gm_vol
                if c in recs_by and not recs_by[c][2] and cur in gm_vol[c]]
    thr_cyc = float(np.percentile(cyc_vol, 60)) if cyc_vol else 4.3
    thr_ncyc = float(np.percentile(ncyc_vol, 60)) if ncyc_vol else 5.9

    def cross(only_cycle):
        thr = thr_cyc if only_cycle else thr_ncyc
        cells = defaultdict(list)
        for code, (ser, l2, cyc) in recs_by.items():
            if cyc != only_cycle:
                continue
            ps = sorted(ser.keys())
            for i in range(len(ps) - 1):
                if prev_q(ps[i + 1]) != ps[i]:
                    continue
                p = ps[i]
                acc = ser[p][1]
                vol = gm_vol.get(code, {}).get(p)
                if vol is None:
                    continue
                hivol = "HiVol" if vol > thr else "LoVol"
                ab = "acc>=60" if acc >= 60 else ("acc<0" if acc < 0 else "acc0_60")
                cells[(hivol, ab)].append(ser[ps[i + 1]][1])
        out = {}
        for (hv, ab), arr in cells.items():
            if len(arr) >= 30:
                a = np.array(arr)
                out[f"{hv}|{ab}"] = {
                    "n": int(len(a)),
                    "next_gq_median": round(float(np.median(a)), 2),
                    "next_gq_mean": round(float(np.mean(a)), 2),
                    "pct_next_neg": round(float((a < 0).mean() * 100), 1),
                }
        return out

    # ---------- C. 周期行业 p60 明细 ----------
    by_l2 = defaultdict(list)
    for code, wins in gm_vol.items():
        l2 = code2l2.get(code)
        if not l2:
            continue
        for s in wins.values():
            by_l2[l2].append(s)
    cyc_p60 = {}
    for l2, arr in by_l2.items():
        if len(arr) >= 30:
            cyc_p60[l2] = {"n": len(arr),
                           "p50": round(float(np.percentile(arr, 50)), 2),
                           "p60": round(float(np.percentile(arr, 60)), 2)}

    ils = sorted([(l2, v) for l2, v in cyc_p60.items() if l2 in CYCLE_L2],
                 key=lambda kv: kv[1]["p60"])
    ncy = sorted([(l2, v) for l2, v in cyc_p60.items() if l2 not in CYCLE_L2],
                 key=lambda kv: kv[1]["p60"])
    cyc_vals = [v["p60"] for _, v in ils]
    ncy_vals = [v["p60"] for _, v in ncy]

    result = {
        "date": "2026-11-02",
        "A_demeaned_autocorr": {
            "note": "按期去均值（period fixed effect）后的 corr(ΔG_q_t, ΔG_q_{t+1})",
            "cycle": demean_autocorr(True),
            "nocycle": demean_autocorr(False),
        },
        "B_cross": {
            "note": "Is_cycle × 毛利率波动(当期组内p60) × accel 三交叉 → 次季 ΔG_q",
            "thr_cycle_p60": round(thr_cyc, 2),
            "thr_nocycle_p60": round(thr_ncyc, 2),
            "cycle": cross(True),
            "nocycle": cross(False),
        },
        "C_industry_p60": {
            "cycle_industries": dict(ils),
            "cycle_p60_median": round(float(np.median(cyc_vals)), 2) if cyc_vals else None,
            "nocycle_p60_median": round(float(np.median(ncy_vals)), 2) if ncy_vals else None,
            "cycle_p60_range": [round(min(cyc_vals), 2), round(max(cyc_vals), 2)] if cyc_vals else None,
        },
    }
    json.dump(result, open(OUT, "w", encoding="utf-8"), ensure_ascii=False, indent=1)

    print("\n===== A. 按期去均值 autocorr =====")
    print("周期  :", json.dumps(result["A_demeaned_autocorr"]["cycle"], ensure_ascii=False))
    print("非周期:", json.dumps(result["A_demeaned_autocorr"]["nocycle"], ensure_ascii=False))

    print("\n===== B. 三交叉（周期组，阈值 p60=%.2f）=====" % thr_cyc)
    for k, v in sorted(result["B_cross"]["cycle"].items()):
        print(f"  {k:22s}", json.dumps(v, ensure_ascii=False))
    print("\n===== B. 三交叉（非周期组，阈值 p60=%.2f）=====" % thr_ncyc)
    for k, v in sorted(result["B_cross"]["nocycle"].items()):
        print(f"  {k:22s}", json.dumps(v, ensure_ascii=False))

    print("\n===== C. 周期行业历史 p60 =====")
    for l2, v in ils:
        print(f"  {l2:10s} n={v['n']:5d} p50={v['p50']:6.2f} p60={v['p60']:6.2f}")
    print(f"\n周期行业 p60 中位 = {result['C_industry_p60']['cycle_p60_median']}  "
          f"非周期 = {result['C_industry_p60']['nocycle_p60_median']}")
    print(f"[done] → {OUT}")


if __name__ == "__main__":
    main()
