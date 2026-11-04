#!/usr/bin/env python3
"""compare_q3_fix.py —— Q3 缓存修复前后 scored 对照（2026-10-31）。

背景：敦煌种业 Q3 单季亏损（扣非 −2701万、环比 −128%）但评分因 fin_cache_em
停留在 20260630（10-26 抓取、TTL 7 天未过期）而虚高至 Top1。全池强刷缓存 +
step2 重算后，本脚本输出三道对照 + 变脸名单。

用法：python compare_q3_fix.py
"""
import json

OLD = "top200_scored_2026-10-30.json.bak_pre_q3fix"
NEW = "top200_scored_2026-10-30.json"


def main():
    old = json.load(open(OLD, encoding="utf-8"))
    new = json.load(open(NEW, encoding="utf-8"))
    om = {r["code"]: r for r in old["results"]}
    nm = {r["code"]: r for r in new["results"]}

    # ---- 1) ρ(total 新旧) ----
    import math
    pairs = [(om[c]["total"], nm[c]["total"]) for c in om if c in nm
             and isinstance(om[c].get("total"), (int, float))
             and isinstance(nm[c].get("total"), (int, float))]
    n = len(pairs)
    mx = sum(p[0] for p in pairs) / n
    my = sum(p[1] for p in pairs) / n
    cov = sum((a - mx) * (b - my) for a, b in pairs) / n
    sx = math.sqrt(sum((a - mx) ** 2 for a, _ in pairs) / n)
    sy = math.sqrt(sum((b - my) ** 2 for _, b in pairs) / n)
    rho = cov / (sx * sy) if sx * sy else float("nan")
    print(f"1) ρ(total 新旧) = {rho:.4f}  n={n}")

    # ---- 2) Top50 重合 ----
    o50 = {r["code"] for r in sorted(old["results"], key=lambda x: -(x.get("total") or 0))[:50]}
    n50 = {r["code"] for r in sorted(new["results"], key=lambda x: -(x.get("total") or 0))[:50]}
    print(f"2) Top50 重合 = {len(o50 & n50)}/50")

    # ---- 3) 评级变动 ----
    ch = [c for c in om if c in nm and om[c].get("rating") != nm[c].get("rating")]
    print(f"3) 评级变动 = {len(ch)} 只")

    # ---- 4) 75 门槛 ----
    o75 = [r for r in old["results"] if (r.get("total") or 0) >= 75]
    n75 = [r for r in new["results"] if (r.get("total") or 0) >= 75]
    nmin = min((r["total"] for r in n75), default=0)
    print(f"4) ≥75分: 旧 {len(o75)} → 新 {len(n75)}（新 min={nmin:.2f}）")

    # ---- 5) growth_period 分布 ----
    from collections import Counter
    per = Counter(r.get("growth_period") for r in new["results"])
    print(f"5) growth_period: {dict(per)}")

    # ---- 6) 变脸名单：旧≥75 且 旧score_growth≥8 且 新score_growth 掉 ≥2 ----
    print("\n6) 变脸名单（旧≥75 & 成长分掉落≥2）：")
    rows = []
    for r in old["results"]:
        c = r["code"]
        if c not in nm:
            continue
        o = r.get("score_growth")
        n_ = nm[c].get("score_growth")
        if (r.get("total") or 0) >= 75 and isinstance(o, (int, float)) and isinstance(n_, (int, float)) \
                and o - n_ >= 2:
            fd = nm[c].get("fund_detail", {}) or {}
            mb = fd.get("modeB") or {}
            rows.append((o - n_, r["name"], c, o, n_, mb.get("g_q"), mb.get("qoq"),
                         r["total"], nm[c]["total"], nm[c].get("rating"),
                         ",".join(mb.get("constraints") or []) or "-"))
    rows.sort(reverse=True)
    print(f"   {'名称':<10s}{'code':<10s}{'旧sg':>6s}{'新sg':>6s}{'G_q%':>9s}{'QoQ%':>8s}"
          f"{'旧total':>8s}{'新total':>8s}{'新评级':>5s}  约束")
    for d, name, c, o, n_, gq, qq, ot, nt, nr, cons in rows:
        gq_s = f"{gq * 100:+.1f}" if isinstance(gq, (int, float)) else "n/a"
        qq_s = f"{qq * 100:+.1f}" if isinstance(qq, (int, float)) else "n/a"
        print(f"   {name:<10s}{c:<10s}{o:>6.2f}{n_:>6.2f}{gq_s:>9s}{qq_s:>8s}"
              f"{ot:>8.2f}{nt:>8.2f}{nr:>5s}  {cons}")
    print(f"   共 {len(rows)} 只")

    # ---- 7) 敦煌种业专项 ----
    print("\n7) 敦煌种业 sh600354 专项：")
    for tag, m in (("旧", om), ("新", nm)):
        r = m.get("sh600354")
        if not r:
            continue
        fd = r.get("fund_detail", {}) or {}
        mb = fd.get("modeB") or {}
        print(f"   [{tag}] total={r['total']} rating={r['rating']} sg={r.get('score_growth')} "
              f"fund={r.get('score_fund')} sgrowth={r.get('score_size_growth')} "
              f"per={r.get('growth_period')} G_q={mb.get('g_q')} qoq={mb.get('qoq')} "
              f"cons={mb.get('constraints')}")

    # ---- 8) 新 Top15 ----
    print("\n8) 新 Top15：")
    for i, r in enumerate(sorted(new["results"], key=lambda x: -(x.get("total") or 0))[:15], 1):
        print(f"   {i:>2d}. {r['total']:6.2f} {r['name']:<10s} {r['code']} "
              f"sg={r.get('score_growth')} per={r.get('growth_period')} {r.get('rating')}")


if __name__ == "__main__":
    main()
