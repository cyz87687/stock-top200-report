#!/usr/bin/env python3
"""
round108f｜规则1/2 落地预览（当期池影响 + 扣分传导量化）
==========================================================
用 round108e 证实的联合条件，在**今日池**上模拟落地效果，量化：
  1. 命中名单（周期 × 价格驱动 × 高加速）
  2. 扣分在 score_growth(0-10) 上的实际幅度
  3. 传导到 total 的**真实影响**（关键：growth w=0.05 时规则是否形同虚设）
  4. 同步对比 growth 权重提到 .10/.15 后的影响（round107 S1/S2 联动必要性）

输出：data/round108f_rule_preview.json
"""
import json
import os
from collections import defaultdict

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
CACHE = os.path.join(HERE, "data", "fin_cache_em")
SW_MAP = os.path.join(HERE, "sector", "sw_map.json")
SCORED = os.path.join(HERE, "top200_scored_2026-11-02.json")
CALIB = os.path.join(HERE, "data", "round108e_joint.json")
OUT = os.path.join(HERE, "data", "round108f_rule_preview.json")

Q_END = ("0331", "0630", "0930", "1231")
CYCLE_L2 = {
    "工业金属", "小金属", "金属新材料", "航运港口", "煤炭开采", "焦炭Ⅱ", "焦炭",
    "化学原料", "化学纤维", "造纸", "养殖业", "冶钢原料", "普钢", "特钢Ⅱ", "特钢",
    "油气开采Ⅱ", "油气开采", "油服工程", "炼化及贸易", "农化制品", "贵金属", "能源金属",
}

# 现行权重（v4.16.1-gray-growth）
W_NOW = {"fund": .20, "size_growth": .20, "tech_neg": .10, "quality": .10,
         "ep": .10, "rsi_rev": .10, "dominant": .10, "bias60": .05, "growth": .05}
# ⚠️ round125 修正：原 `"fund": 10` 是**错的** —— 生产 dim_max 里 score_fund 是 **5.0**。
#   实测该 key 在本脚本里**从未被读取**（只用到 DIM_MAX["growth"]），所以历史输出未被污染；
#   但它是个埋着的雷：谁哪天照着这里取 fund 量程，就会把 fund 分数压半。
#   本脚本用无 `score_` 前缀的短键名体系（历史遗留），与 dim_cfg 的键名不通用，
#   故不接统一加载器，只修正错值；已登记于 test_dim_cfg_single_source._ALLOWLIST。
DIM_MAX = {"fund": 5.0, "size_growth": 10, "tech_neg": 10, "quality": 10, "ep": 10,
           "rsi_rev": 10, "dominant": 10, "bias60": 5, "growth": 10}


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


def norm(c):
    c = str(c)
    for p in ("sz", "sh", "bj"):
        if c.startswith(p):
            return c[2:]
    return c


def main():
    code2l2 = (json.load(open(SW_MAP, encoding="utf-8")).get("l2") or {})
    calib = json.load(open(CALIB, encoding="utf-8"))
    lo, hi = calib["winsor"]
    thr_acc = calib["thr_accel_p80"]
    print(f"[calib] winsor={lo}..{hi}  accel p80={thr_acc}", flush=True)

    sc = json.load(open(SCORED, encoding="utf-8"))
    rows = sc["results"]
    print(f"[pool] {len(rows)} 只", flush=True)

    # 全池 accel 按期均值（口径与 108e 一致：用缓存全池，非仅周期）
    store = {}
    for fn in [f for f in os.listdir(CACHE) if f.endswith(".json")]:
        code = fn[:-5]
        try:
            d = json.load(open(os.path.join(CACHE, fn), encoding="utf-8"))
        except Exception:
            continue
        recs = d.get("records") or []
        if not recs:
            continue
        rev, cost, kf = (get_series(recs, "营业总收入"), get_series(recs, "营业成本"),
                         get_series(recs, "扣非净利润"))
        if not (rev and cost and kf):
            continue
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
        store[code] = per

    by_p = defaultdict(list)
    for per in store.values():
        for p, f in per.items():
            if "accel" in f:
                by_p[p].append(float(np.clip(f["accel"], lo, hi)))
    mu = {p: float(np.mean(v)) for p, v in by_p.items()}

    cur = "20260930"
    # 扣分方案：规则1 10%（老大指定），规则2 15%（按 lift 比例反推：24.7/16 × 10%），叠加上限 25%
    DED_R1, DED_R2, CAP = 0.10, 0.15, 0.25

    hits = []
    for r in rows:
        code = norm(r["code"])
        l2 = code2l2.get(code)
        if not l2 or l2 not in CYCLE_L2:
            continue                                    # 周期标签 gate：非强周期直接跳过
        per = store.get(code) or {}
        f = per.get(cur)
        if not f or "accel" not in f:
            continue
        dm = float(np.clip(f["accel"], lo, hi)) - mu.get(cur, 0.0)
        P = (f.get("gm_rise") is not None and f["gm_rise"] > 3.0)
        A = dm >= thr_acc
        ded = 0.0
        tags = []
        if P:
            ded += DED_R1
            tags.append(f"R1价格驱动(gm_rise={f['gm_rise']:.1f}pp)")
        if A:
            ded += DED_R2
            tags.append(f"R2高加速(dm={dm:.0f}pp)")
        if ded <= 0:
            continue
        ded = min(ded, CAP)
        sg = r.get("score_growth")
        hits.append({
            "name": r["name"], "code": r["code"], "l2": l2,
            "gm_rise": round(f["gm_rise"], 2) if f.get("gm_rise") is not None else None,
            "rev_yoy": round(f["rev_yoy"] * 100, 1) if f.get("rev_yoy") is not None else None,
            "accel": round(f["accel"], 1), "accel_dm": round(dm, 1),
            "gq": round(f["gq"] * 100, 1) if f.get("gq") is not None else None,
            "score_growth": sg, "total": r.get("total"),
            "ded_pct": ded, "tags": tags,
        })
    hits.sort(key=lambda x: -x["ded_pct"])
    print(f"[hits] 命中 {len(hits)} 只（周期组 {sum(1 for r in rows if code2l2.get(norm(r['code'])) in CYCLE_L2)} 只中）", flush=True)

    # ---- 传导量化：扣 score_growth 的 x% → total 变化 ----
    def total_delta(rec, ded_pct, w_growth):
        sg = rec["score_growth"]
        if sg is None:
            return 0.0
        old = sg / DIM_MAX["growth"] * w_growth * 10     # score_growth 是 0-10 → 归一 × 权重 × 10
        new = sg * (1 - ded_pct) / DIM_MAX["growth"] * w_growth * 10
        return new - old

    for h in hits:
        h["d_total_w05"] = round(total_delta(h, h["ded_pct"], W_NOW["growth"]), 3)
        h["d_total_w10"] = round(total_delta(h, h["ded_pct"], .10), 3)
        h["d_total_w15"] = round(total_delta(h, h["ded_pct"], .15), 3)

    print("\n===== 命中明细（前 20）=====")
    print(f"{'名称':10s}{'二级':10s}{'毛利抬升':>9s}{'加速dm':>8s}{'成长分':>7s}{'总分':>7s}"
          f"{'扣%':>6s}{'Δ@.05':>7s}{'Δ@.10':>7s}{'Δ@.15':>7s}")
    for h in hits[:20]:
        print(f"{h['name']:10s}{(h['l2'] or ''):10s}{str(h['gm_rise']):>9s}{h['accel_dm']:>8.0f}"
              f"{str(h['score_growth']):>7s}{str(h['total']):>7s}{h['ded_pct']*100:>5.0f}%"
              f"{h['d_total_w05']:>7.2f}{h['d_total_w10']:>7.2f}{h['d_total_w15']:>7.2f}")

    # 汇总：规则对 total 的最大影响
    max05 = min(h["d_total_w05"] for h in hits) if hits else 0
    max10 = min(h["d_total_w10"] for h in hits) if hits else 0
    max15 = min(h["d_total_w15"] for h in hits) if hits else 0
    print(f"\n[传导] 最大总分影响：w=.05 → {max05:.2f} 分 | w=.10 → {max10:.2f} | w=.15 → {max15:.2f}")

    # 排名位移模拟（仅演示，不改文件）
    base_sorted = sorted(rows, key=lambda r: -(r.get("total") or 0))
    rank_now = {r["code"]: i + 1 for i, r in enumerate(base_sorted)}
    sim = []
    for r in rows:
        h = next((x for x in hits if x["code"] == r["code"]), None)
        d = h["d_total_w05"] if h else 0.0
        sim.append({"code": r["code"], "name": r["name"],
                    "total": round((r.get("total") or 0) + d, 2)})
    sim.sort(key=lambda x: -x["total"])
    rank_new = {s["code"]: i + 1 for i, s in enumerate(sim)}
    print("\n===== 位移（w=.05 口径，仅规则扣分）=====")
    moved = [(s["name"], rank_now[s["code"]], rank_new[s["code"]]) for s in sim
             if rank_now[s["code"]] != rank_new[s["code"]]]
    moved.sort(key=lambda x: -(x[2] - x[1]))
    print(f"  排名变动 {len(moved)} 只；最大下移 {moved[0] if moved else None}")
    for n, o, nw in moved[:8]:
        print(f"  {n:10s} #{o} → #{nw} ({nw-o:+d})")

    json.dump({"date": "2026-11-02",
               "scheme": {"ded_rule1": DED_R1, "ded_rule2": DED_R2, "cap": CAP,
                          "thr_gm_rise": 3.0, "thr_accel_p80": thr_acc},
               "n_hits": len(hits),
               "n_cycle_in_pool": sum(1 for r in rows if code2l2.get(norm(r["code"])) in CYCLE_L2),
               "max_total_impact": {"w05": round(max05, 2), "w10": round(max10, 2),
                                    "w15": round(max15, 2)},
               "rank_moved": len(moved),
               "hits": hits},
              open(OUT, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"[done] → {OUT}")


if __name__ == "__main__":
    main()
