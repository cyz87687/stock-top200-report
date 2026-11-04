"""audit_p04_coverage_bias.py —— P0-4 关键诊断：fund/dominant 的低覆盖**是不是随机的**。

为什么这一步决定 P0-4 该怎么改：
    生产面板 panel_round123 实测覆盖率：
        score_fund       3.53%   ← 96.5% 的股票**没有**这一维
        score_dominant  25.48%
    而现行 v4.17 给它们 0.20 / 0.10，合计 0.30。

    如果缺失是**随机**的 → 只是「信息少」，重归一化后分数仍可比，属可接受的降级。
    如果缺失**与市值/行业/流动性相关** → 那么有 fund 数据的股票和没有的股票，
        其 total 由**不同维度的组合**决定，跨股票不可比。
        这不是「精度下降」，是「口径分裂」。

同一型血案在本项目已有先例：
    「跨权重版本禁比 total」「缺维从中性填 DMAX/2」都是为了防这一件事。

本脚本检验：
    Q1 fund 有值 vs 无值 两组的 circ_mv / amount / 行业 分布差异（KS 与卡方式对比）
    Q2 dominant 同
    Q3 覆盖率的时间稳定性（是否只在某几个月有数据）
    Q4 「有 fund」的股票在 total 排序中的分位分布 —— 若系统性偏头部/尾部即为选择效应
"""
import json
import os
import time

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
PANEL = os.path.join(HERE, "data", "panel_round123.json")
SCORED = os.path.join(HERE, "top200_scored_2026-11-03.json")
OUT = os.path.join(HERE, "data", "audit_p04_coverage_bias.json")

# ⚠️ 血案（本轮）：panel_round123 的 rows 里**只有 score_* 分数列，没有市值/流动性**。
# 第一版直接用 v.get("circ_mv") → 全部 None → 所有维度样本数 0、静默输出空表。
# 面板字段实测只有 11 列：score_fund/bias60/rsi_rev/tech_neg/lowvol20/ep/
# size_ind/quality/size_growth/dominant/growth + total。
# ⇒ 市值必须从 top200_scored_*.json 的 circ_mv 旁取，按 code 关联。


def ks_2samp(a, b):
    """两样本 KS 统计量与近似 p 值（不用 scipy 的重实现，便于自证）。"""
    a = np.sort(np.asarray(a, dtype=np.float64))
    b = np.sort(np.asarray(b, dtype=np.float64))
    if len(a) < 5 or len(b) < 5:
        return None, None
    allv = np.concatenate([a, b])
    ca = np.searchsorted(a, allv, side="right") / len(a)
    cb = np.searchsorted(b, allv, side="right") / len(b)
    d = float(np.max(np.abs(ca - cb)))
    en = (len(a) * len(b) / (len(a) + len(b))) ** 0.5
    lam = (en + 0.12 + 0.11 / en) * d
    # Kolmogorov 分布的渐近尾概率
    p = 2.0 * sum((-1) ** (k - 1) * np.exp(-2.0 * k * k * lam * lam)
                  for k in range(1, 101))
    return round(d, 4), round(float(min(max(p, 0.0), 1.0)), 6)


def main():
    t0 = time.time()
    print("加载生产面板 ...", flush=True)
    d = json.load(open(PANEL, encoding="utf-8"))
    rows, dates = d["rows"], d["dates"]
    print(f"  {len(dates)} 日 × {len(d['domain'])} 只  {dates[0]} ~ {dates[-1]}",
          flush=True)

    # 逐维统计：覆盖率 + 缺失与市值的关联
    res = {"panel": PANEL, "n_dates": len(dates),
           "range": [dates[0], dates[-1]],
           "mv_source": "top200_scored_2026-11-03.json:circ_mv（按 code 关联，每股一个静态值）",
           "dim_coverage": {}, "missing_vs_mv": {}, "time_pattern": {},
           "rank_position": {}}

    # 市值表：面板无市值列，从评分文件取（circ_mv 单位亿元）
    mv_map = {}
    if os.path.exists(SCORED):
        sc = json.load(open(SCORED, encoding="utf-8"))
        for r in sc.get("results") or []:
            c6 = str(r.get("code") or "")[-6:]
            v = r.get("circ_mv")
            if c6 and isinstance(v, (int, float)) and v > 0:
                mv_map[c6] = float(v)
    print(f"  市值表 {len(mv_map)} 只（源 {os.path.basename(SCORED)}:circ_mv）", flush=True)
    res["n_mv"] = len(mv_map)

    DIMS = ["score_fund", "score_dominant", "score_ep", "score_quality",
            "score_growth", "score_size_growth", "score_lowvol20",
            "score_bias60", "score_rsi_rev", "score_tech_neg"]

    per_dim = {k: {"have": [], "miss": [], "by_date": {}, "rank_pct": []}
               for k in DIMS}

    # ⚠️ 血案 2（本轮）：面板 rows 里**也没有 total 列**（只有 11 个 score_* 列）。
    #    第二版用 v.get("total") 取分位 → tot 恒为空 → len(tot)<20 全被 continue
    #    → 又一次静默输出空表。⇒ total 必须自己按现行权重算（逐行重归一化）。
    W_REF = {"score_fund": 0.20, "score_size_growth": 0.20, "score_tech_neg": 0.10,
             "score_quality": 0.10, "score_ep": 0.10, "score_dominant": 0.10,
             "score_growth": 0.10, "score_bias60": 0.05, "score_rsi_rev": 0.05}
    DMAX = {"score_bias60": 5.0, "score_rsi_rev": 5.0}

    def _total(v):
        num = den = 0.0
        for k, w in W_REF.items():
            val = v.get(k)
            if val is None:
                continue
            num += w * (val / DMAX.get(k, 10.0))
            den += w
        return (num / den) if den > 1e-9 else None

    # 只用最近 60 个交易日（近端口径，与生产决策相关）
    use_dates = dates[-60:]
    used = 0
    for dt in use_dates:
        r = rows.get(dt) or {}
        if not r:
            continue
        tot = {}
        for c, v in r.items():
            t = _total(v)
            if t is not None:
                tot[c] = t
        if len(tot) < 20:
            continue
        tot_sorted = sorted(tot.values())
        ntot = len(tot_sorted)
        used += 1
        for c, v in r.items():
            mv = mv_map.get(c)
            if mv is None:
                continue
            for k in DIMS:
                val = v.get(k)
                rec = per_dim[k]
                if val is None:
                    rec["miss"].append(mv)
                    continue
                rec["have"].append(mv)
                rec["by_date"][dt] = rec["by_date"].get(dt, 0) + 1
                if c in tot:
                    import bisect
                    rec["rank_pct"].append(
                        bisect.bisect_left(tot_sorted, tot[c]) / max(1, ntot - 1))
    res["n_used_dates"] = used

    print("\n=== 覆盖率与「缺失是否随机」===", flush=True)
    print(f"{'dim':<22}{'覆盖率':>9}{'有值n':>9}{'缺失n':>9}"
          f"{'有值MV中位':>12}{'缺失MV中位':>12}{'KS D':>8}{'p':>10}"
          f"{'有值total分位':>14}", flush=True)
    for k in DIMS:
        rec = per_dim[k]
        nh, nm = len(rec["have"]), len(rec["miss"])
        if nh + nm == 0:
            continue
        cov = nh / (nh + nm)
        mvh = float(np.median(rec["have"])) if nh else None
        mvm = float(np.median(rec["miss"])) if nm else None
        dd, pp = ks_2samp(rec["have"], rec["miss"]) if (nh >= 5 and nm >= 5) else (None, None)
        rp = float(np.mean(rec["rank_pct"])) if rec["rank_pct"] else None
        res["dim_coverage"][k] = {"coverage": round(cov, 4), "n_have": nh, "n_miss": nm}
        res["missing_vs_mv"][k] = {"mv_median_have": round(mvh, 2) if mvh else None,
                                   "mv_median_miss": round(mvm, 2) if mvm else None,
                                   "ks_d": dd, "ks_p": pp,
                                   "random_if_p_gt_0.05": (pp is not None and pp > 0.05)}
        res["rank_position"][k] = round(rp, 4) if rp is not None else None
        print(f"{k:<22}{cov:>8.2%}{nh:>9}{nm:>9}"
              f"{(f'{mvh:.1f}' if mvh else '-'):>12}{(f'{mvm:.1f}' if mvm else '-'):>12}"
              f"{(f'{dd:.3f}' if dd else '-'):>8}{(f'{pp:.2e}' if pp is not None else '-'):>10}"
              f"{(f'{rp:.3f}' if rp is not None else '-'):>14}", flush=True)

    # Q3 时间稳定性：fund 的覆盖是否只在某些月份出现
    print("\n=== 时间稳定性（有值股票数的逐月分布）===", flush=True)
    for k in ("score_fund", "score_dominant"):
        bd = per_dim[k]["by_date"]
        months = {}
        for dt, c in bd.items():
            months.setdefault(dt[:7], 0)
            months[dt[:7]] += c
        res["time_pattern"][k] = {"n_months_with_data": len(months),
                                  "months": sorted(months)}
        allm = sorted({dt[:7] for dt in use_dates})
        miss_m = [m for m in allm if m not in months]
        print(f"  {k:<20} 近60日覆盖 {len(months)}/{len(allm)} 个月；"
              f"无数据月份={miss_m or '无'}", flush=True)

    # 结论
    print("\n=== 裁决 ===", flush=True)
    for k in ("score_fund", "score_dominant"):
        m = res["missing_vs_mv"].get(k) or {}
        cov = (res["dim_coverage"].get(k) or {}).get("coverage")
        if m.get("ks_p") is None:
            print(f"  {k}: 样本不足，无法判定", flush=True)
            continue
        verdict = ("缺失**随机**（p>0.05）→ 属可接受的精度下降"
                   if m["ks_p"] > 0.05 else
                   "缺失**非随机**（p≤0.05）→ 有/无该维的股票由不同维度组合决定 ⇒ 跨股票不可比")
        print(f"  {k}: 覆盖 {cov:.2%}；KS p={m['ks_p']:.2e} → {verdict}", flush=True)
        rp = res["rank_position"].get(k)
        if rp is not None:
            print(f"      有值股票的 total 分位均值 = {rp:.3f}"
                  f"（0.5=无偏；显著偏离 0.5 说明选择效应）", flush=True)

    with open(OUT, "w", encoding="utf-8") as f:
        json.dump({**res, "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                   "window": "近 60 个交易日",
                   "caveat": ("KS 检验基于 circ_mv 单变量；缺失机制可能与未观测变量相关，"
                              "本检验只能证伪「与市值无关」，不能证明「完全随机」。"
                              "rank_position 用当日 total 分位，total 本身含该维 → 存在循环，"
                              "故只作方向参考。")}, f, ensure_ascii=False, indent=1)
    print(f"\n写出 {OUT}  耗时 {time.time()-t0:.0f}s", flush=True)


if __name__ == "__main__":
    raise SystemExit(main())
