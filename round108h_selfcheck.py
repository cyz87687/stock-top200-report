#!/usr/bin/env python3
"""
round108h｜growth_factor_b 改造自查（改造前后同池对比）
========================================================
验证三件事：
  A. 周期清单切换生效：is_cycle 从 l1 口径 → cycle_industry.json l2 口径，
     对比命中差异（应排除基础化工整类、保留光伏）
  B. P1g 去重生效：合成分 = 纯 rank_Gq，rank_QoQ 不再进合成分
  C. 规则1/2 生效：周期股命中数与扣分幅度；非周期股 rule_gate=non_cycle_skip

输出：data/round108h_selfcheck.json
"""
import json
import os
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import growth_factor_b as gfb


def main():
    sc = json.load(open(os.path.join(HERE, "top200_scored_2026-11-02.json"),
                        encoding="utf-8"))
    rows = sc["results"]
    print(f"[pool] {len(rows)} 只", flush=True)

    out = gfb.compute_growth(rows, style="steady")
    out.pop("__meta__", None)     # 元信息键，非股票
    print(f"[compute] {len(out)} 只（已剔除 __meta__ 元信息键）", flush=True)

    n_valid = sum(1 for v in out.values() if v.get("in_rank_pool"))
    n_cycle = sum(1 for v in out.values() if v.get("is_cycle"))
    n_r1 = sum(1 for v in out.values() if v.get("rule1_hit"))
    n_r2 = sum(1 for v in out.values() if v.get("rule2_hit"))
    n_both = sum(1 for v in out.values() if v.get("rule1_hit") and v.get("rule2_hit"))
    n_ded = sum(1 for v in out.values() if (v.get("rule_ded_pct") or 0) > 0)

    print("\n===== A. 周期清单 =====")
    print(f"  周期命中 {n_cycle} 只（l2 口径，含光伏）")
    from collections import Counter
    c = Counter(v["sw_l2"] for v in out.values() if v.get("is_cycle"))
    for k, v in c.most_common():
        print(f"    {k:12s} {v}")
    # 与非周期对照：原 l1 口径会多抓哪些
    l1_cycle = {"有色金属", "基础化工", "煤炭", "石油石化", "钢铁"}
    old_hit = [k for k, v in out.items() if (v.get("sw_l1") in l1_cycle)
               or v.get("sw_l2") == "光伏设备"]
    new_hit = [k for k, v in out.items() if v.get("is_cycle")]
    print(f"\n  旧口径(l1整类+光伏) 命中 {len(old_hit)}；新口径 {len(new_hit)}")
    dropped = set(old_hit) - set(new_hit)
    print(f"  新口径剔除 {len(dropped)} 只（原基础化工里的成长型）")
    dc = Counter(out[k].get("sw_l2") for k in dropped)
    for k, v in dc.most_common(8):
        print(f"    剔除 {k:14s} {v}")
    added = set(new_hit) - set(old_hit)
    print(f"  新口径新增 {len(added)} 只")
    if added:
        ac = Counter(out[k]["sw_l2"] for k in added)
        print("   ", dict(ac))

    print(f"\n===== B. P1g 去重 =====")
    # 验证合成分 = w1*rg（w2=0）
    bad = []
    for k, v in out.items():
        if not v.get("in_rank_pool"):
            continue
        if v.get("w2") != 0.0:
            bad.append((k, v.get("w2")))
    print(f"  w2 非零的样本: {len(bad)} （应为 0）")
    # rank_qoq 仍然保留（供 fund deriv1）
    n_rq = sum(1 for v in out.values() if v.get("rank_qoq") is not None)
    print(f"  rank_qoq 仍保留 {n_rq} 只（供 fund deriv1 用 ✅）")

    print(f"\n===== C. 规则1/2 =====")
    print(f"  有效样本 {n_valid}；周期 {n_cycle}")
    print(f"  规则1 命中 {n_r1}；规则2 命中 {n_r2}；双命中 {n_both}；有扣分 {n_ded}")
    # 非周期股必须 rule_gate=non_cycle_skip 且无扣分
    viol = [k for k, v in out.items()
            if not v.get("is_cycle") and ((v.get("rule_ded_pct") or 0) > 0
                                          or v.get("rule_gate") != "non_cycle_skip")]
    print(f"  ⚠️ 非周期股误扣/误标: {len(viol)} （应为 0）")

    hits = sorted([v for v in out.values() if (v.get("rule_ded_pct") or 0) > 0],
                  key=lambda x: -(x["rule_ded_pct"] or 0))
    print(f"\n  命中明细（前 20，按扣分降序）:")
    print(f"  {'名称':10s}{'二级':12s}{'毛利抬升':>9s}{'dm':>9s}{'扣%':>6s}{'合成分':>8s}{'留痕'}")
    for v in hits[:20]:
        print(f"  {(v.get('name') or ''):10s}{(v.get('sw_l2') or ''):12s}"
              f"{str(round(v['gm_rise_pp'],1) if v.get('gm_rise_pp') is not None else None):>9s}"
              f"{str(v.get('accel_demeaned_pp')):>9s}"
              f"{v['rule_ded_pct']*100:>5.0f}%{v['composite']:>8.3f}  "
              f"{[h for h in v['constraints_hit'] if h.startswith('r')]}")

    # 与旧 growth 分对比（旧=未改前 scored 里的 score_growth）
    old_sg = {r["code"][2:] if r["code"][:2] in ("sh", "sz", "bj") else r["code"]:
              r.get("score_growth") for r in rows}
    deltas = []
    for k, v in out.items():
        if not v.get("in_rank_pool"):
            continue
        o = old_sg.get(k)
        if o is None or v.get("score_growth") is None:
            continue
        deltas.append(v["score_growth"] - o)
    if deltas:
        print(f"\n  score_growth 变化: n={len(deltas)} 中位={np.median(deltas):+.3f} "
              f"均值={np.mean(deltas):+.3f} min={min(deltas):+.2f} max={max(deltas):+.2f}")

    json.dump({"date": "2026-11-02",
               "n_pool": len(rows), "n_valid": n_valid,
               "n_cycle": n_cycle, "n_rule1": n_r1, "n_rule2": n_r2,
               "n_both": n_both, "n_ded": n_ded,
               "n_violation_noncycle": len(viol),
               "cycle_l2_breakdown": dict(c),
               "old_l1_hit": len(old_hit), "new_l2_hit": len(new_hit),
               "dropped_n": len(dropped),
               "dropped_industries": dict(dc),
               "hits": [{k: v.get(k) for k in
                         ("code", "name", "sw_l2", "gm_rise_pp", "accel_demeaned_pp",
                          "rule_ded_pct", "rule1_hit", "rule2_hit", "composite",
                          "score_growth", "constraints_hit")} for v in hits]},
              open(os.path.join(HERE, "data", "round108h_selfcheck.json"), "w",
                   encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"\n[done] → data/round108h_selfcheck.json")


if __name__ == "__main__":
    main()
