#!/usr/bin/env python3
"""
round108d｜规则2 干净口径标定 + 扣分档反推
============================================
round108b 已证：按期去均值后，周期股 corr(ΔG_q_t, ΔG_q_{t+1}) = −0.5528，
非周期仅 −0.0211 → 「周期股高加速必回落」成立，且是周期独有结构。

本脚本在**干净口径（去均值 accel）**上标定规则2 的扣分档：
  1. 去均值 accel 的周期/非周期分位（定阈值：p80/p90）
  2. 周期股 去均值 accel 分桶 → 次期 accel 中位 / 为负概率 / 回落幅度
  3. 反推扣分档：把「次期回落幅度」映射到 growth 合成分的扣分（0-1 量程）
     扣分原则（不发明）：扣分 ≈ 该档位「次期 accel 中位」占全组 accel 标准差的比例，
     再按 growth 权重 0.05 → 总分影响实测量化

输出：data/round108d_rule2_calibration.json
"""
import json
import os
from collections import defaultdict

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
CACHE = os.path.join(HERE, "data", "fin_cache_em")
SW_MAP = os.path.join(HERE, "sector", "sw_map.json")
SCORED = os.path.join(HERE, "top200_scored_2026-11-02.json")
OUT = os.path.join(HERE, "data", "round108d_rule2_calibration.json")

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

    store = {}   # code -> {p: accel}, l2 map
    for fn in files:
        code = fn[:-5]
        try:
            d = json.load(open(os.path.join(CACHE, fn), encoding="utf-8"))
        except Exception:
            continue
        recs = d.get("records") or []
        kf = get_series(recs, "扣非净利润")
        if not kf:
            continue
        l2 = code2l2.get(code)
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
        if len(ser) >= 4:
            store[code] = (ser, l2, bool(l2 and l2 in CYCLE_L2))

    print(f"[load] {len(store)} 只", flush=True)

    # 缩尾 1/99（对齐 growth_factor_b 清洗口径）
    allacc = [a for ser, _, _ in store.values() for a in ser.values()]
    lo, hi = np.percentile(allacc, 1), np.percentile(allacc, 99)
    print(f"[winsor] accel 1%={lo:.1f} 99%={hi:.1f}", flush=True)

    # 按期去均值
    by_p = defaultdict(list)
    for code, (ser, l2, cyc) in store.items():
        for p, a in ser.items():
            by_p[p].append(float(np.clip(a, lo, hi)))
    mu = {p: float(np.mean(v)) for p, v in by_p.items()}
    sd_all = float(np.std([np.clip(a, lo, hi) for v in by_p.values() for a in v]))
    print(f"[demean] 全组 accel std = {sd_all:.2f}", flush=True)

    # 去均值序列 + 分位（周期/非周期各自）
    cyc_dm, ncy_dm = [], []
    for code, (ser, l2, cyc) in store.items():
        for p, a in ser.items():
            d_ = float(np.clip(a, lo, hi)) - mu.get(p, 0.0)
            (cyc_dm if cyc else ncy_dm).append(d_)
    q_cyc = {f"p{int(x*100)}": round(float(np.percentile(cyc_dm, x * 100)), 2)
             for x in (0.5, 0.6, 0.7, 0.8, 0.9, 0.95)}
    q_ncy = {f"p{int(x*100)}": round(float(np.percentile(ncy_dm, x * 100)), 2)
             for x in (0.5, 0.6, 0.7, 0.8, 0.9, 0.95)}
    print("[q] 周期去均值分位:", json.dumps(q_cyc, ensure_ascii=False))
    print("[q] 非周期去均值分位:", json.dumps(q_ncy, ensure_ascii=False))

    # ---- 分桶：去均值 accel → 次期 去均值 accel ----
    def bucket(only_cycle, edges):
        rows = []
        for code, (ser, l2, cyc) in store.items():
            if cyc != only_cycle:
                continue
            ps = sorted(ser.keys())
            for i in range(len(ps) - 1):
                if prev_q(ps[i + 1]) != ps[i]:
                    continue
                p, p1 = ps[i], ps[i + 1]
                a = float(np.clip(ser[p], lo, hi)) - mu.get(p, 0.0)
                b = float(np.clip(ser[p1], lo, hi)) - mu.get(p1, 0.0)
                rows.append((a, b))
        if len(rows) < 100:
            return {}
        arr = np.array(rows)
        out = {}
        for lo_, hi_ in edges:
            m = (arr[:, 0] >= lo_) & (arr[:, 0] < hi_)
            if m.sum() >= 30:
                nb = arr[m, 1]
                out[f"[{lo_},{hi_})"] = {
                    "n": int(m.sum()),
                    "next_dm_median": round(float(np.median(nb)), 2),
                    "next_dm_mean": round(float(np.mean(nb)), 2),
                    "pct_next_neg": round(float((nb < 0).mean() * 100), 1),
                    "pct_revert_half": round(float((nb < -abs(arr[m, 0]).mean() * 0.5).mean() * 100), 1),
                }
        return out

    cyc_p80 = float(np.percentile(cyc_dm, 80))
    cyc_p90 = float(np.percentile(cyc_dm, 90))
    edges_cyc = [(-9999, cyc_p80), (cyc_p80, cyc_p90), (cyc_p90, 9999),
                 (cyc_p90, 9999), (50, 9999), (100, 9999)]
    edges_cyc = [( -9999, 0), (0, cyc_p80), (cyc_p80, cyc_p90), (cyc_p90, 9999), (100, 9999)]
    b_cyc = bucket(True, edges_cyc)
    ncy_p80 = float(np.percentile(ncy_dm, 80))
    ncy_p90 = float(np.percentile(ncy_dm, 90))
    b_ncy = bucket(False, [(-9999, 0), (0, ncy_p80), (ncy_p80, ncy_p90), (ncy_p90, 9999), (100, 9999)])

    # ---- 当期池内：周期组 accel 触发情况 ----
    sc = json.load(open(SCORED, encoding="utf-8"))
    def norm(c):
        for p in ("sz", "sh", "bj"):
            if str(c).startswith(p):
                return str(c)[2:]
        return str(c)
    cur = "20260930"
    trig = []
    for r in sc["results"]:
        code = norm(r["code"])
        if code not in store:
            continue
        ser, l2, cyc = store[code]
        if not cyc or cur not in ser:
            continue
        a = float(np.clip(ser[cur], lo, hi)) - mu.get(cur, 0.0)
        trig.append((r["name"], code, l2, round(ser[cur], 1), round(a, 1),
                     r.get("score_growth"), r.get("total")))
    trig.sort(key=lambda x: -x[4])
    n_trig80 = sum(1 for t in trig if t[4] >= cyc_p80)
    n_trig90 = sum(1 for t in trig if t[4] >= cyc_p90)

    result = {
        "date": "2026-11-02",
        "winsor": {"p1": round(float(lo), 2), "p99": round(float(hi), 2)},
        "accel_std_pool": round(sd_all, 2),
        "quantile_cycle_demeaned": q_cyc,
        "quantile_nocycle_demeaned": q_ncy,
        "bucket_cycle": b_cyc,
        "bucket_nocycle": b_ncy,
        "current_pool_cycle_trigger": {
            "n_cycle_in_pool": len(trig),
            "thr_p80_cycle": round(cyc_p80, 2),
            "thr_p90_cycle": round(cyc_p90, 2),
            "n_ge_p80": n_trig80,
            "n_ge_p90": n_trig90,
            "top15": [dict(zip(("name", "code", "l2", "accel_raw_pp",
                                "accel_demeaned_pp", "score_growth", "total"), t))
                      for t in trig[:15]],
        },
    }
    json.dump(result, open(OUT, "w", encoding="utf-8"), ensure_ascii=False, indent=1)

    print("\n===== 规则2 分桶：周期（去均值）=====")
    for k, v in b_cyc.items():
        print(f"  {k:16s}", json.dumps(v, ensure_ascii=False))
    print("\n===== 对照：非周期（去均值）=====")
    for k, v in b_ncy.items():
        print(f"  {k:16s}", json.dumps(v, ensure_ascii=False))
    print(f"\n===== 当期池内周期触发（阈值 p80={cyc_p80:.1f} / p90={cyc_p90:.1f}）=====")
    print(f"  周期池内 {len(trig)} 只；≥p80 {n_trig80} 只；≥p90 {n_trig90} 只")
    for t in trig[:12]:
        print(f"  {t[0]:8s} {t[2] or '':8s} accel={t[3]:8.1f} dm={t[4]:8.1f} "
              f"grow={t[5]} total={t[6]}")
    print(f"[done] → {OUT}")


if __name__ == "__main__":
    main()
