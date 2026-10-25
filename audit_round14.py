#!/usr/bin/env python3
"""Round14：核查多因子组合「现价是否落在买入区间内」—— 逐只实测，不许估算。

动机（老大 2026-10-25）：
  「多因子组合有多只现价不在买入区间，只选当日在买入区间的个股。」

问题结构（本次核查才看清）：
  `sector/mf_pick_<date>.json` 的 picks 里**没有 buy_lo / buy_hi 字段** ——
  买入区间是报告层 gen_report_top200.build_trade_plan() 在**渲染时**用
  ma5/ma10/ma20 + 支撑阻力位现推的。于是产生两个天然缺陷：
    ① 选股层（pick_multifactor.py）完全不知道买区在哪，选出来必然混入「已高于买区」的票；
    ② 报告层只能事后打个提示（`现价高于买区 · 等回踩`），无法剔除，
       导致「json 10 只 / 页面 10 只但只有 N 只可执行」的口径分裂。

本脚本做的事：
  用 AST 从 gen_report_top200.py 里**抽取 build_trade_plan 函数的源码**，
  在隔离命名空间 exec 出来复用 —— 不 import 整个报告模块（那会跑一遍全量渲染），
  也不重写一份（重写必然与页面口径漂移）。
  然后逐只算 buy_lo/buy_hi，判定 price 是否落在 [buy_lo, buy_hi]。

判定口径（与页面 `_mf_n_in` 一致，故意放宽 2%）：
  price ∈ [buy_lo*0.98, buy_hi*1.02] → 记为「在区间内」
  页面阈值用 `_prc > buy_hi*1.02` 判「高于买区」，`_prc >= buy_lo*0.98` 计「已进入」。
  本脚本同时输出**严格口径**（buy_lo <= price <= buy_hi）与**页面口径**，
  因为两者结论可能不同 —— 必须让老大看到差异，而不是挑一个好看的数。

输出：audit_round14.json + 控制台表格。
"""
import ast
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "audit_round14.json")


def load_trade_plan_fn():
    """从 gen_report_top200.py 抽取 build_trade_plan 源码并 exec 出来。

    为什么不用 import：gen_report_top200.py 是顶层脚本，import 即触发整份报告
    渲染（读 json、拼 2.7MB HTML），既慢又有副作用。
    为什么不用复制源码：两处副本必然漂移，而「买入区间」是本轮判定的唯一依据，
    口径漂移会让核查结论失效。
    """
    p = os.path.join(HERE, "gen_report_top200.py")
    src = open(p, encoding="utf-8").read()
    tree = ast.parse(src)
    fn = None
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == "build_trade_plan":
            fn = node
            break
    if fn is None:
        raise RuntimeError("未在 gen_report_top200.py 找到 build_trade_plan")
    seg = ast.get_source_segment(src, fn)
    ns = {}
    exec(compile(seg, "<build_trade_plan>", "exec"), ns)
    return ns["build_trade_plan"]


def _latest_mf_date():
    """自动探测最新的 mf_pick_<date>.json，返回其日期串。

    ⚠️ v4.3：原先默认写死 "2026-10-23"，接进每日 workflow 后会永远核查旧日期，
       让新日期的「现价是否在买区内」永远无人校验。改为按文件探测。
    """
    import glob as _g
    fs = sorted(_g.glob(os.path.join(HERE, "sector", "mf_pick_*.json")))
    if not fs:
        return None
    return os.path.basename(fs[-1])[len("mf_pick_"):-len(".json")]


def main():
    date_str = sys.argv[1] if len(sys.argv) > 1 else None
    if not date_str:
        date_str = _latest_mf_date()
    if not date_str:
        print("❌ 未找到 sector/mf_pick_<date>.json")
        return 1
    mfp = os.path.join(HERE, "sector", f"mf_pick_{date_str}.json")
    scp = os.path.join(HERE, f"top200_scored_{date_str}.json")
    for p in (mfp, scp):
        if not os.path.exists(p):
            print(f"❌ 缺少 {p}")
            return 1

    with open(mfp, encoding="utf-8") as f:
        mf = json.load(f)
    with open(scp, encoding="utf-8") as f:
        sc = json.load(f)
    rows = {str(r.get("code")): r for r in (sc.get("results") or []) if r}
    picks = mf.get("picks") or []

    btp = load_trade_plan_fn()

    items, n_strict, n_page = [], 0, 0
    for i, p in enumerate(picks, 1):
        code = str(p.get("code") or "")
        src = rows.get(code) or {}
        price = p.get("price")
        if not isinstance(price, (int, float)) or price <= 0:
            price = src.get("price")
        plan = btp(src) if src else None
        it = {
            "idx": i, "name": p.get("name"), "code": code,
            "sector_l2": p.get("sector_l2"),
            "price": price,
            "mf_score": p.get("mf_score"), "total": p.get("total"),
            "rating": p.get("rating"),
            "rsi": p.get("rsi"), "trend": p.get("trend"),
        }
        if not plan or not isinstance(price, (int, float)):
            it.update({"plan_ok": False, "verdict": "无交易计划（缺技术数据）"})
            items.append(it)
            continue
        lo, hi = plan["buy_lo"], plan["buy_hi"]
        # 严格口径：现价必须真的落在 [lo, hi] 内
        strict = (lo <= price <= hi)
        # 页面口径：放宽 2%（页面用 price > hi*1.02 判「高于买区」）
        page = (lo * 0.98 <= price <= hi * 1.02)
        n_strict += 1 if strict else 0
        n_page += 1 if page else 0
        if price > hi:
            gap = (price / hi - 1) * 100
            verdict = f"高于买区上限 {gap:+.2f}% → 需回踩 {price - hi:.2f} 元"
            state = "above"
        elif price < lo:
            gap = (price / lo - 1) * 100
            verdict = f"低于买区下限 {gap:+.2f}% → 已破位，非回踩"
            state = "below"
        else:
            verdict = f"区间内（距上限 {(hi - price) / price * -100:+.2f}%）"
            state = "in"
        it.update({
            "plan_ok": True, "buy_lo": lo, "buy_hi": hi,
            "stop": plan["stop"], "target": plan["target"],
            "rr": round(plan["rr"], 3), "entry_ref": plan["entry_ref"],
            "tgt_basis": plan.get("tgt_basis"),
            "state": state, "in_strict": strict, "in_page": page,
            "verdict": verdict,
            "overbought": plan.get("overbought"),
            "flags": plan.get("flags"),
        })
        items.append(it)

    n = len(items)
    n_ok = sum(1 for x in items if x.get("plan_ok"))
    res = {
        "date": date_str,
        "source_mf": os.path.basename(mfp),
        "source_scored": os.path.basename(scp),
        "n_picks": n,
        "n_plan_ok": n_ok,
        "n_in_strict": n_strict,
        "n_in_page": n_page,
        "n_above": sum(1 for x in items if x.get("state") == "above"),
        "n_below": sum(1 for x in items if x.get("state") == "below"),
        "ruler": {
            "strict": "buy_lo <= price <= buy_hi（现价真的落在区间内）",
            "page": "buy_lo*0.98 <= price <= buy_hi*1.02（报告层 _mf_n_in 的容差口径）",
        },
        "items": items,
    }
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(res, f, ensure_ascii=False, indent=1)

    print(f"=== {date_str} 多因子组合买入区间核查（{n} 只，有交易计划 {n_ok} 只）===\n")
    print(f"{'#':>2} {'名称':<8}{'板块':<10}{'现价':>7}{'买区':>15}{'状态':>7}  判定")
    print("-" * 96)
    for x in items:
        if not x.get("plan_ok"):
            print(f"{x['idx']:>2} {str(x.get('name')):<8}{str(x.get('sector_l2')):<10}"
                  f"{x.get('price') or 0:>7.2f}{'—':>15}{'—':>7}  {x['verdict']}")
            continue
        tag = {"in": "✅区间内", "above": "⚠️高于", "below": "⚠️低于"}[x["state"]]
        print(f"{x['idx']:>2} {str(x.get('name')):<8}{str(x.get('sector_l2')):<10}"
              f"{x['price']:>7.2f}{x['buy_lo']:>7.2f}~{x['buy_hi']:<7.2f}"
              f"{'':>3}{tag:<7} {x['verdict']}")
    print("-" * 96)
    print(f"严格口径（真在区间内）：{n_strict}/{n} 只")
    print(f"页面口径（±2% 容差）  ：{n_page}/{n} 只")
    print(f"高于买区 {res['n_above']} 只 · 低于买区 {res['n_below']} 只")
    print(f"\n✅ 写入 {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
