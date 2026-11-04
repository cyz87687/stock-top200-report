#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
round107 P0-2 权重方案反事实模拟（2026-11-02）
==============================================
在不动原始因子分的前提下，用候选权重向量重算 total，量化：
  · 业绩减速案例（鼎胜/盛弘）排名位移
  · 全池 ρ / Top50 重合 / ≥75 门槛数
  · 关键案例（生益/中际/巨人/敦煌）位移
同时模拟 fund 内部结构（0.6 base + 0.2 d1 + 0.2 d2 → 0.4/0.3/0.3）。
⚠️ 这是方案书的量化锚，不是正式重算——正式落地必须走门禁+七处同步+全链。
"""
import json, math

SCORED = "top200_scored_2026-10-30.json"
DIMS = ["score_fund", "score_quality", "score_ep", "score_size_growth",
        "score_dominant", "score_tech_neg", "score_rsi_rev", "score_bias60",
        "score_growth"]

def norm_of(raw, dmax):
    if isinstance(raw, (int, float)) and raw == raw:
        return min(raw, dmax) / dmax * 10.0
    return 5.0  # 中性填充

def fund_remix(score_fund, d1, d2, mix):
    """按 mix=(base,d1,d2) 重构 fund 分。base 由现口径反解。"""
    w0, w1, w2 = 0.6, 0.2, 0.2  # 现口径
    if not (isinstance(score_fund, (int, float)) and isinstance(d1, (int, float))
            and isinstance(d2, (int, float))):
        return score_fund  # 无法重构则保留
    base = (score_fund - w1 * d1 - w2 * d2) / w0
    base = max(0.0, min(5.0, base))
    m0, m1, m2 = mix
    return m0 * base + m1 * d1 + m2 * d2

def spearman(a, b):
    n = len(a)
    def rank(x):
        order = sorted(range(n), key=lambda i: x[i])
        r = [0.0] * n
        i = 0
        while i < n:
            j = i
            while j + 1 < n and x[order[j + 1]] == x[order[i]]:
                j += 1
            avg = (i + j) / 2.0 + 1.0
            for k in range(i, j + 1):
                r[order[k]] = avg
            i = j + 1
        return r
    ra, rb = rank(a), rank(b)
    ma, mb = sum(ra) / n, sum(rb) / n
    cov = sum((ra[i] - ma) * (rb[i] - mb) for i in range(n))
    va = math.sqrt(sum((ra[i] - ma) ** 2 for i in range(n)))
    vb = math.sqrt(sum((rb[i] - mb) ** 2 for i in range(n)))
    return cov / (va * vb) if va > 0 and vb > 0 else 0.0

def main():
    d = json.load(open(SCORED))
    rows = d["results"]
    dmax = d.get("dim_max") or {}
    if not dmax:
        dmax = {"score_fund": 5.0, "score_quality": 10.0, "score_ep": 10.0,
                "score_size_growth": 10.0, "score_dominant": 10.0,
                "score_tech_neg": 10.0, "score_rsi_rev": 5.0,
                "score_bias60": 5.0, "score_growth": 10.0}

    # ---- 约束校验 ----
    GROUPS = {"基本面": ["score_fund", "score_quality"],
              "估值": ["score_ep"], "低波": ["score_lowvol20"],
              "技术反转": ["score_bias60", "score_rsi_rev", "score_tech_neg"],
              "市值": ["score_size_growth", "score_dominant"]}
    def check(w, label):
        errs = []
        if abs(sum(w.values()) - 1.0) > 1e-9:
            errs.append(f"Σ={sum(w.values())}")
        for k, v in w.items():
            if v > 0.20 + 1e-9:
                errs.append(f"{k}={v}>0.20")
            if v < -1e-9:
                errs.append(f"{k}<0")
        for gn, dims in GROUPS.items():
            tot = sum(w.get(x, 0) for x in dims)
            if tot > 0.30 + 1e-9:
                errs.append(f"组[{gn}]={tot}>0.30")
        print(f"约束[{label}]: {'PASS' if not errs else 'FAIL ' + '; '.join(errs)}")
        return not errs

    # ---- 候选方案 ----
    base_w = {"score_fund": 0.20, "score_quality": 0.10, "score_ep": 0.10,
              "score_size_growth": 0.20, "score_dominant": 0.10,
              "score_tech_neg": 0.10, "score_rsi_rev": 0.10,
              "score_bias60": 0.05, "score_growth": 0.05}
    schemes = {
        "S0 现行 v4.16.1": (dict(base_w), (0.6, 0.2, 0.2)),
        "S1 稳妥 growth.10": (dict(base_w, score_growth=0.10, score_tech_neg=0.05), (0.6, 0.2, 0.2)),
        "S2 证据导向 growth.15": (dict(base_w, score_growth=0.15, score_tech_neg=0.05, score_rsi_rev=0.05), (0.6, 0.2, 0.2)),
        "S3 =S1+fund内部 0.4/0.3/0.3": (dict(base_w, score_growth=0.10, score_tech_neg=0.05), (0.4, 0.3, 0.3)),
        "S4 =S2+fund内部 0.4/0.3/0.3": (dict(base_w, score_growth=0.15, score_tech_neg=0.05, score_rsi_rev=0.05), (0.4, 0.3, 0.3)),
    }

    named = ["鼎胜新材", "盛弘股份", "生益科技", "中际旭创", "巨人网络", "敦煌种业"]

    # 预取各行原始分
    recs = []
    for r in rows:
        vals = {k: r.get(k) for k in DIMS}
        fd = r.get("fund_detail") or {}
        recs.append({
            "name": r.get("name"), "code": r.get("code"),
            "vals": vals,
            "d1": fd.get("deriv1_score"), "d2": fd.get("deriv2_score"),
            "total_now": r.get("total") or 0.0,
        })

    results = {}
    for label, (w, mix) in schemes.items():
        check(w, label)
        totals = []
        for rec in recs:
            t = 0.0
            for k in DIMS:
                ww = w.get(k, 0.0)
                if ww <= 0:
                    continue
                raw = rec["vals"].get(k)
                if k == "score_fund":
                    raw = fund_remix(raw, rec["d1"], rec["d2"], mix)
                t += norm_of(raw, dmax.get(k, 10.0)) * ww
            totals.append(round(t * 10.0, 2))   # ×10 对齐实际 total 0-100 口径
        order = sorted(range(len(totals)), key=lambda i: -totals[i])
        rank = [0] * len(totals)
        for pos, i in enumerate(order):
            rank[i] = pos + 1
        rho = spearman([r["total_now"] for r in recs], totals)
        top50_new = set(order[:50])
        top50_old = set(sorted(range(len(recs)), key=lambda i: -recs[i]["total_now"])[:50])
        overlap = len(top50_new & top50_old)
        n75 = sum(1 for t in totals if t >= 75)
        results[label] = {"totals": totals, "rank": rank, "rho": rho,
                          "overlap": overlap, "n75": n75}
        print(f"\n=== {label} ===")
        print(f"ρ(vs 现行)={rho:.4f}  Top50重合={overlap}/50  ≥75分={n75}  "
              f"total范围={min(totals):.1f}~{max(totals):.1f}")
        for nm in named:
            for i, rec in enumerate(recs):
                if rec["name"] == nm:
                    r_old = sorted(range(len(recs)), key=lambda j: -recs[j]["total_now"]).index(i) + 1
                    print(f"  {nm}: {rec['total_now']:.2f}(#{r_old}) -> {totals[i]:.2f}(#{rank[i]})")
                    break

    # 方案间对比表
    print("\n" + "=" * 70)
    print("方案对比汇总")
    print(f"{'方案':<34s} {'ρ':>6s} {'Top50':>6s} {'≥75':>5s}")
    for label, v in results.items():
        print(f"{label:<34s} {v['rho']:>6.3f} {v['overlap']:>4d}/50 {v['n75']:>5d}")

    json.dump(
        {"schemes": {k: {"rho": v["rho"], "overlap": v["overlap"], "n75": v["n75"]}
                     for k, v in results.items()}},
        open("data/round107_weight_schemes_preview.json", "w"),
        ensure_ascii=False, indent=1)
    print("\nsaved data/round107_weight_schemes_preview.json")

if __name__ == "__main__":
    main()
