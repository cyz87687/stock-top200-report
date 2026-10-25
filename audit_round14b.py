#!/usr/bin/env python3
"""Round14b：全候选池的「现价 vs 买入区间」分布 —— 回答「只选当日可成交的，能剩下几只」。

Round14 的结论（10 只里只有 4 只现价真在区间内）只是症状。本脚本查根因：

⚠️ 关键发现：`build_trade_plan` 里有一句 `buy_hi = min(ref_hi, price)`，
   即**买区上限被强制截到不高于现价**。推论：
     · price == buy_hi  ⇔ 现价正好压在买区上限（股价在均线下方，退化到"支撑~现价"区间）
     · price >  buy_hi  ⇔ 股价已涨到均线带上方，需回踩才进入买区
     · price <  buy_lo  ⇔ 已跌破买区下限（破位）
   也就是说「现价在买入区间内」这件事，在既有实现下**天然退化为「现价 == 买区上限」**，
   永远不会出现"现价低于买区上限且高于下限"的中间态。
   → 因此「只在买入区间内选股」的正确语义是「现价不高于买区上限」，
     而不是「现价夹在 lo 和 hi 之间」。本轮按前者实现，并把这一点写进披露。

本脚本对 pick_multifactor 的**全部合格候选**（不是只对 top10）算买区，输出：
  · 各状态（区间内/高于买区/低于买区）只数与占比
  · 若只保留「现价 ≤ 买区上限」，按多因子分排序能凑出几只、板块约束下能凑出几只
"""
import ast
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))


def load_trade_plan_fn():
    src = open(os.path.join(HERE, "gen_report_top200.py"), encoding="utf-8").read()
    tree = ast.parse(src)
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == "build_trade_plan":
            ns = {}
            exec(compile(ast.get_source_segment(src, node),
                         "<build_trade_plan>", "exec"), ns)
            return ns["build_trade_plan"]
    raise RuntimeError("未找到 build_trade_plan")


def _latest_scored():
    """自动探测最新的 top200_scored_<date>.json（排除 watch/bak）。

    ⚠️ v4.3：原先默认写死 "2026-10-23"，接进每日 workflow 后会永远算旧日期的分布。
    """
    import glob as _g
    fs = sorted(_g.glob(os.path.join(HERE, "top200_scored_*.json")))
    fs = [f for f in fs if "_watch_" not in f and ".bak" not in f]
    if not fs:
        return None
    return os.path.basename(fs[-1])[len("top200_scored_"):-len(".json")]


def main():
    date_str = sys.argv[1] if len(sys.argv) > 1 else None
    if not date_str:
        date_str = _latest_scored()
    if not date_str:
        print("❌ 未找到 top200_scored_<date>.json")
        return 1
    btp = load_trade_plan_fn()

    scp = os.path.join(HERE, f"top200_scored_{date_str}.json")
    if not os.path.exists(scp):
        print(f"❌ 缺少 {scp}")
        return 1
    sc = json.load(open(scp, encoding="utf-8"))
    rows = [r for r in (sc.get("results") or []) if r]

    # 与 pick_multifactor 同口径的资格过滤
    elig = []
    for r in rows:
        if r.get("is_new") or r.get("suspended") or not r.get("liquid_ok", True):
            continue
        elig.append(r)

    st_cnt = {"in": 0, "above": 0, "below": 0, "no_plan": 0}
    above_gaps = []
    detail = []
    for r in elig:
        price = r.get("price")
        plan = btp(r)
        if not plan or not isinstance(price, (int, float)) or price <= 0:
            st_cnt["no_plan"] += 1
            continue
        lo, hi = plan["buy_lo"], plan["buy_hi"]
        if price > hi:
            st = "above"
            above_gaps.append((price / hi - 1) * 100)
        elif price < lo:
            st = "below"
        else:
            st = "in"
        st_cnt[st] += 1
        detail.append({"code": str(r.get("code")), "name": r.get("name"),
                       "price": price, "buy_lo": lo, "buy_hi": hi,
                       "state": st,
                       "prem_pct": round((price / hi - 1) * 100, 2)})

    n = len(elig)
    print(f"=== {date_str} 合格候选 {n} 只的买区分布 ===\n")
    for k, lbl in (("in", "现价 == 买区上限（可挂单）"),
                   ("above", "现价 > 买区上限（需回踩）"),
                   ("below", "现价 < 买区下限（已破位）"),
                   ("no_plan", "无交易计划")):
        v = st_cnt[k]
        print(f"  {lbl:<24}{v:>4} 只  {v / max(n, 1) * 100:>5.1f}%")
    if above_gaps:
        g = sorted(above_gaps)
        print(f"\n高于买区的溢价幅度：中位 {g[len(g)//2]:.2f}% · "
              f"最小 {g[0]:.2f}% · 最大 {g[-1]:.2f}% · "
              f"≤2% 的 {sum(1 for x in g if x <= 2)} 只")

    # 若只保留「现价 ≤ 买区上限」，按板块约束能凑几只
    ok = [d for d in detail if d["state"] == "in"]
    print(f"\n可挂单候选合计：{len(ok)} 只")
    by_l2, by_l1 = {}, {}
    l2map = {}
    for r in elig:
        l2map[str(r.get("code"))] = (r.get("sector_l2") or r.get("sector") or "未分类",
                                     r.get("sector_parent") or r.get("sector_l2") or "未分类")
    cap_ok = []
    for d in ok:
        l2, l1 = l2map.get(d["code"], ("未分类", "未分类"))
        d["sector_l2"], d["sector_parent"] = l2, l1
        if by_l2.get(l2, 0) >= 2 or by_l1.get(l1, 0) >= 2:
            continue
        by_l2[l2] = by_l2.get(l2, 0) + 1
        by_l1[l1] = by_l1.get(l1, 0) + 1
        cap_ok.append(d)
    print(f"施加板块约束（L2≤2 / L1≤2）后：{len(cap_ok)} 只")
    print("\n明细（可挂单 + 板块约束通过）：")
    for d in cap_ok:
        print(f"  {d['name']} {d['code']} {d['sector_l2']:<10} "
              f"现价 {d['price']:.2f} 买区 {d['buy_lo']:.2f}~{d['buy_hi']:.2f}")

    out = {"date": date_str, "n_eligible": n, "state_counts": st_cnt,
           "above_gap_stats": ({"median": round(sorted(above_gaps)[len(above_gaps)//2], 2),
                                "min": round(min(above_gaps), 2),
                                "max": round(max(above_gaps), 2),
                                "le2pct": sum(1 for x in above_gaps if x <= 2)}
                              if above_gaps else None),
           "n_tradable_raw": len(ok), "n_tradable_capped": len(cap_ok),
           "tradable_capped": cap_ok,
           "structural_note": ("build_trade_plan 的 buy_hi = min(ref_hi, price) 使买区上限"
                               "永不高于现价 → 「现价在区间内」退化为「现价 == 买区上限」；"
                               "故筛选语义为「现价不高于买区上限」")}
    op = os.path.join(HERE, "audit_round14b.json")
    json.dump(out, open(op, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"\n✅ 写入 {op}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
