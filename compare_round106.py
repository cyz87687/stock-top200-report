#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""round106 收尾对照：增长口径改造（扣非+ratio单季同比+一阶导QoQ+二阶导ΔG_q）后 vs bak_pre_round106。"""
import json
import math

OLD = "top200_scored_2026-10-30.json.bak_pre_round106"
NEW = "top200_scored_2026-10-30.json"

old = json.load(open(OLD))["results"]
new = json.load(open(NEW))["results"]
old_by = {r["code"]: r for r in old}
new_by = {r["code"]: r for r in new}

pairs = [(old_by[c]["total"], new_by[c]["total"]) for c in new_by if c in old_by]
n = len(pairs)
mx = sum(a for a, _ in pairs) / n
my = sum(b for _, b in pairs) / n
num = sum((a - mx) * (b - my) for a, b in pairs)
den = math.sqrt(sum((a - mx) ** 2 for a, _ in pairs) *
                sum((b - my) ** 2 for _, b in pairs))
print(f"ρ(total 新旧) = {num/den:.4f}  n={n}")

old50 = set(sorted(old_by, key=lambda c: -old_by[c]["total"])[:50])
new50 = set(sorted(new_by, key=lambda c: -new_by[c]["total"])[:50])
print(f"Top50 重合 = {len(old50 & new50)}/50")

chg = sum(1 for c in new_by if old_by.get(c, {}).get("rating") != new_by[c]["rating"])
from collections import Counter
rc = Counter(r["rating"] for r in new)
print(f"评级变动 = {chg} 只 | 新分布 S:{rc.get('S',0)} A:{rc.get('A',0)} "
      f"B:{rc.get('B',0)} C:{rc.get('C',0)} D:{rc.get('D',0)}")
tot = [r["total"] for r in new]
print(f"total: min {min(tot):.2f} / max {max(tot):.2f} | ≥75: {sum(1 for t in tot if t >= 75)} 只")

# ---- round106 专项：负基数/二阶导留痕分布 ----
n_neg = n_turn = n_decel = 0
for r in new:
    mb = (r.get("fund_detail") or {}).get("modeB") or {}
    if mb.get("neg_base"):
        n_neg += 1
        if mb.get("turnaround"):
            n_turn += 1
    cons = mb.get("constraints") or []
    if "consec_decel" in cons:
        n_decel += 1
print(f"负基数中性通道 = {n_neg} 只（其中扭亏 {n_turn}）| 连两季降速触发 = {n_decel} 只")

print(f"\n{'个股':<10s} {'旧total':>8s} {'旧#':>4s} {'新total':>8s} {'新#':>4s}  "
      f"{'fund':>5s} {'growth':>6s} {'G_q':>7s} {'QoQ':>7s} {'ΔG_q':>8s}")
old_rank = {c: i + 1 for i, c in enumerate(sorted(old_by, key=lambda c: -old_by[c]["total"]))}
new_rank = {c: i + 1 for i, c in enumerate(sorted(new_by, key=lambda c: -new_by[c]["total"]))}
for name in ["生益科技", "中际旭创", "巨人网络", "敦煌种业"]:
    for c, r in new_by.items():
        if r["name"] == name:
            o = old_by.get(c, {})
            mb = (r.get("fund_detail") or {}).get("modeB") or {}
            gq = mb.get("g_q"); qq = mb.get("qoq"); ac = mb.get("accel_pp")
            fmt = lambda x, s="": ("n/a" if x is None else f"{x*100:+.0f}%" if s == "%" else (f"{x:+.0f}pp" if s == "pp" else f"{x:.2f}"))
            print(f"{name:<10s} {o.get('total', 0):>8.2f} {old_rank.get(c, 0):>4d} "
                  f"{r['total']:>8.2f} {new_rank[c]:>4d}  "
                  f"{r.get('score_fund') or 0:>5.2f} {r.get('score_growth') or 0:>6.2f} "
                  f"{fmt(gq, '%'):>7s} {fmt(qq, '%'):>7s} {fmt(ac, 'pp'):>8s}")
            break

print("\n新 Top10:")
for c in sorted(new_by, key=lambda c: -new_by[c]["total"])[:10]:
    r = new_by[c]
    mb = (r.get("fund_detail") or {}).get("modeB") or {}
    ac = mb.get("accel_pp")
    print(f"  {r['total']:6.2f} {r['name']:<10s} {c}  sg={r.get('score_growth')} "
          f"ΔG_q={'n/a' if ac is None else f'{ac:+.0f}pp'} "
          f"neg_base={mb.get('neg_base')} es={(r.get('es') or {}).get('tier')}")
