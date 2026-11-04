#!/usr/bin/env python3
"""
round108c｜规则1 口径有效性对决（用次期真实 G_q 回落做效标）
==============================================================
背景：round108b 发现「毛利率波动大 → 更不可持续」不成立（HiVol −50.29 vs LoVol −77.41）。
      故必须检验：到底哪个口径能识别「价格驱动 → 后续业绩回落」。

三个候选口径（均在强周期组内评估，因为规则只对强周期生效）：
  A. 波动口径（老大原方案简化版）：近4季单季毛利率 std > 行业历史 p60
  B. 毛利率抬升口径：近4季毛利率均值 − 前4季均值 > 阈值（涨价直接抬毛利）
  C. 量价背离口径（业界标准代理）：毛利率同比抬升 >0 且单季营收同比 < 0
     （涨价但没多卖 = 纯价格驱动）

效标（label）：t+1 期 G_q 相对 t 期**下降**
  · primary  = 次期 G_q < 当期 G_q（增速退坡）
  · hard     = 次期 G_q < 当期 G_q − 20pp（显著退坡）

评估：各口径命中组的「退坡率」 vs 未命中组，算 lift = 命中组退坡率 / 全组退坡率。

输出：data/round108c_rule1_bench.json
"""
import json
import os
from collections import defaultdict

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
CACHE = os.path.join(HERE, "data", "fin_cache_em")
SW_MAP = os.path.join(HERE, "sector", "sw_map.json")
OUT = os.path.join(HERE, "data", "round108c_rule1_bench.json")

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

    # code -> {period: dict(gm, gm_prev4, gq, rev_yoy, sq_rev)}
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
        if not l2 or l2 not in CYCLE_L2:
            continue                      # 只评估强周期组（规则生效域）

        sq_rev, sq_cost, sq_kf = single_quarter(rev), single_quarter(cost), single_quarter(kf)
        gm = {p: (sq_rev[p] - sq_cost[p]) / sq_rev[p] * 100.0
              for p in sq_rev if p in sq_cost and sq_rev[p] > 1e4}
        gps = sorted(gm.keys())
        per = {}
        for j, p in enumerate(gps):
            rec = {"gm": gm[p]}
            if j >= 3:
                w4 = [gm[q] for q in gps[j - 3:j + 1]]
                rec["gm_std4"] = float(np.std(w4, ddof=1))
                rec["gm_mean4"] = float(np.mean(w4))
            if j >= 7:
                prev4 = [gm[q] for q in gps[j - 7:j - 3]]
                rec["gm_rise"] = rec["gm_mean4"] - float(np.mean(prev4))
            # 单季营收同比
            ply = f"{int(p[:4])-1}{p[4:]}"
            if p in sq_rev and ply in sq_rev and sq_rev[ply] > 1e4:
                rec["rev_yoy"] = sq_rev[p] / sq_rev[ply] - 1.0
            # G_q
            if p in sq_kf and ply in sq_kf and sq_kf[ply] > 0:
                rec["gq"] = sq_kf[p] / sq_kf[ply] - 1.0
            if rec:
                per[p] = rec
        if per:
            store[code] = (per, l2)

    print(f"[load] 强周期组可用 {len(store)} 只", flush=True)

    # ---- 效标配对：t 期特征 → t+1 期 G_q 变化 ----
    pairs = []      # (code, l2, p, feat, gq_t, gq_t1)
    for code, (per, l2) in store.items():
        ps = sorted(per.keys())
        for i in range(len(ps) - 1):
            p, p1 = ps[i], ps[i + 1]
            if prev_q(p1) != p:
                continue
            f, f1 = per[p], per[p1]
            if "gq" not in f or "gq" not in f1:
                continue
            pairs.append((code, l2, p, f, f["gq"], f1["gq"]))
    print(f"[pairs] 可用 t→t+1 配对 {len(pairs)} 条", flush=True)

    base_decline = np.mean([1 if b < a else 0 for _, _, _, _, a, b in pairs])
    base_hard = np.mean([1 if b < a - 0.20 else 0 for _, _, _, _, a, b in pairs])
    print(f"[base] 退坡率 {base_decline*100:.1f}% / 显著退坡(>20pp) {base_hard*100:.1f}%", flush=True)

    # ---- 行业历史 p60（波动口径阈值）----
    by_l2_std = defaultdict(list)
    for code, (per, l2) in store.items():
        for p, f in per.items():
            if "gm_std4" in f:
                by_l2_std[l2].append(f["gm_std4"])
    ind_p60 = {}
    for l2, arr in by_l2_std.items():
        if len(arr) >= 30:
            ind_p60[l2] = float(np.percentile(arr, 60))
    all_std = [v for a in by_l2_std.values() for v in a]
    pool_p60 = float(np.percentile(all_std, 60))
    print(f"[thr] 行业历史 p60 中位 {np.median(list(ind_p60.values())):.2f}, 兜底 {pool_p60:.2f}", flush=True)

    # ---- 三口径评估 ----
    def evaluate(name, pred):
        """pred(f) -> True/False/None"""
        hit_idx, miss_idx = [], []
        for _, _, _, f, a, b in pairs:
            v = pred(f)
            if v is None:
                continue
            (hit_idx if v else miss_idx).append((a, b))
        def rate(arr, hard=False):
            if len(arr) < 20:
                return None
            if hard:
                return float(np.mean([1 if b < a - 0.20 else 0 for a, b in arr]))
            return float(np.mean([1 if b < a else 0 for a, b in arr]))
        r_hit, r_miss = rate(hit_idx), rate(miss_idx)
        rh_hit, rh_miss = rate(hit_idx, True), rate(miss_idx, True)
        return {
            "name": name,
            "n_hit": len(hit_idx), "n_miss": len(miss_idx),
            "decline_hit": round(r_hit * 100, 1) if r_hit is not None else None,
            "decline_miss": round(r_miss * 100, 1) if r_miss is not None else None,
            "lift": round(r_hit / base_decline, 3) if r_hit is not None else None,
            "hard_hit": round(rh_hit * 100, 1) if rh_hit is not None else None,
            "hard_miss": round(rh_miss * 100, 1) if rh_miss is not None else None,
            "hard_lift": round(rh_hit / base_hard, 3) if rh_hit is not None else None,
        }

    def th_std(f):
        if "gm_std4" not in f:
            return None
        thr = ind_p60.get(f.get("_l2"), pool_p60)
        return f["gm_std4"] > thr

    # 需要 l2 传入 → 包一层
    pairs2 = [(c, l2, p, {**f, "_l2": l2}, a, b) for c, l2, p, f, a, b in pairs]
    pairs = pairs2

    results = []
    results.append(evaluate("A 波动>行业p60（老大简化方案）", th_std))
    for th in (2.0, 3.0, 5.0):
        results.append(evaluate(f"B 毛利率抬升>{th}pp", lambda f, t=th: (f["gm_rise"] > t) if "gm_rise" in f else None))
    results.append(evaluate("C 量价背离(毛利抬升>0且营收同比<0)",
                            lambda f: (f["gm_rise"] > 0 and f.get("rev_yoy", 9) < 0)
                            if ("gm_rise" in f and "rev_yoy" in f) else None))
    results.append(evaluate("C2 量价背离(毛利抬升>1且营收同比<0)",
                            lambda f: (f["gm_rise"] > 1 and f.get("rev_yoy", 9) < 0)
                            if ("gm_rise" in f and "rev_yoy" in f) else None))
    results.append(evaluate("D 纯高毛利抬升>3pp 且 营收同比<10%",
                            lambda f: (f["gm_rise"] > 3 and f.get("rev_yoy", 9) < 0.10)
                            if ("gm_rise" in f and "rev_yoy" in f) else None))
    # 对照：什么都不判（全组）
    results.append({
        "name": "对照 全组基线", "n_hit": len(pairs), "n_miss": 0,
        "decline_hit": round(base_decline * 100, 1), "decline_miss": None, "lift": 1.0,
        "hard_hit": round(base_hard * 100, 1), "hard_miss": None, "hard_lift": 1.0,
    })

    json.dump({"date": "2026-11-02", "n_pairs": len(pairs),
               "base_decline_pct": round(base_decline * 100, 1),
               "base_hard_pct": round(base_hard * 100, 1),
               "industry_p60_median": round(float(np.median(list(ind_p60.values()))), 2),
               "pool_p60_fallback": round(pool_p60, 2),
               "results": results},
              open(OUT, "w", encoding="utf-8"), ensure_ascii=False, indent=1)

    print("\n===== 规则1 口径对决（效标=次期 G_q 退坡）=====")
    print(f"{'口径':40s} {'命中n':>7s} {'退坡%':>7s} {'未命中%':>8s} {'lift':>6s} {'硬退坡%':>8s} {'hlift':>6s}")
    for r in results:
        print(f"{r['name']:40s} {r['n_hit']:>7d} {str(r['decline_hit']):>7s} "
              f"{str(r['decline_miss']):>8s} {str(r['lift']):>6s} {str(r['hard_hit']):>8s} {str(r['hard_lift']):>6s}")
    print(f"\n[done] → {OUT}")


if __name__ == "__main__":
    main()
