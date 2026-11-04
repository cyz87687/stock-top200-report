#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""round36/37→v4.13：按当前口径重算 scored 的 quality / 市值双维 / total / rating。

任务 #297-299（老大 2026-10-28 指令）+ v4.13-size2d（市值双维拆分）
--------------------------------------------------------------------
1. 授权修复 fund_split.py 的 NaN 污染（已在模块内完成，本脚本负责验证）
2. quality 权重维持现状不动，但重算后的数据必须正确
3. 重跑全池（本脚本：同数据、只换算法，不动行情源）
4. score_size_ind 改为「行业市值-成长条件交互打分」（严格按老大原文：
   final = raw × w_mc，w_mc ∈ {0, 0.3, 0.8}，clip[0,10]，**不做二次重标定**）
5. **v4.13-size2d（2026-10-28 老大裁决）**：单一 score_size_ind(0.10) 拆成两个
   互斥维度各自加权进总分 ——
     A score_size_growth(0.08) 小盘成长溢价：原 v4.12 成长条件交互分，对非龙头生效；
     B score_dominant(0.06) 行业龙头壁垒：业务份额(营收+利润TTM，申万二级行业内占比)
       合成 rank 前 30%（syn≥7.0）判为龙头，做市值一致性校验 + 流动性惩罚。
   ⚠️ A/B 分区后，同一只股票**只会落其中一维**；两者皆缺才是「市值维缺失」。

关键口径（勿改）
----------------
① quality 走严格 PIT；growth 走 pit_mode="actual_ok"（grace=5 天，让三季报在
   10-26 生效）。理由见 fund_split._pit_period_actual 文档。
② size_growth 成长门槛用 G（= score_growth，0-10）；G 缺失 → w_mc=0（保守方向）。
③ 流动性扣减用「当日 amount 近似 20 日均额」（老大选定），阈值 0.8 亿。
   本池上游已过滤成交额≥1亿 → 全池无一只触发，属设计冗余，非缺陷。
④ 分组统一用**申万一级**（A 维市值分）与**申万二级**（B 维业务份额）。

⚠️ 回归校验（本脚本每次运行都做，不通过即拒绝写入）
   先用文件内自带的旧 score_quality / score_size_ind 复现文件内 total，
   必须逐只吻合（容差 0.02）；不吻合说明公式或维度集已漂移，拒绝写入。
   ⚠️ 权重变更（v4.13 把 quality 0.252→0.212、市值 0.10→0.08+0.06）必然导致
     total 偏移，此时加 --force 绕过（属预期，非掩盖缺陷）。

⚠️ 口径纪律
   total 与 v4.10 及更早**不可比**（size_ind 语义变更 + quality 修复 + v4.13 权重）。
   跨版本必须用同一把尺重算，禁直接引历史 total。

用法
----
    python recalc_v412.py [--dry-run] [scored_json]
"""
import argparse
import io
import json
import os
import sys
from collections import Counter, defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import scale_calc          # noqa: E402
import fund_split as fs_mod  # noqa: E402
import ep_size_ind as esi    # noqa: E402
import cycle_sustain as cs   # noqa: E402  round37 任务1：周期行业估值锚封顶

TOL = 0.02
RATING_CUTS = [(0.05, "S"), (0.15, "A"), (0.50, "B"), (0.88, "C"), (1.01, "D")]
ADVICE = {"S": "优先买入，可重仓", "A": "逢低加仓，重点关注",
          "B": "波段操作，轻仓参与", "C": "观望，不新开仓",
          "D": "坚决回避，立即卖出"}


def load_json(p):
    with io.open(p, encoding="utf-8") as f:
        return json.load(f)


def is_num(x):
    return isinstance(x, (int, float)) and not isinstance(x, bool) and x == x


def total_of(row, wmap, dim_max, sum_all):
    parts = [(k, row.get(k), w) for k, w in wmap.items() if w > 0]
    return scale_calc.composite_total(parts, dim_max, sum_all=sum_all)


def rating_pct(vals):
    """分位相对评级 —— ⚠️ 必须与 gen_report_top200._quantile_ratings 逐字同构。

    页面的最终评级由 gen_report 的 _quantile_ratings 重算（用 (i+0.5)/n 分位），
    若本脚本用 (i+1)/n，边界上会差几只（实测 S 94 vs 95、D 228 vs 227），
    变成「scored 说 94 只、页面说 95 只」的两套口径。故此处严格对齐：
        q = (i + 0.5) / n ；if q < hi → 该档

    ⚠️ 必须**按行索引**返回，不能按 total 值查表 —— total 会有并列（实测同分最多
    同分多只），用 {value: rating} 字典会让并列行全部塌到同一个评级，
    与页面逐行给级的结果在档位边界处必然不一致。
    """
    n = len(vals)
    order = sorted(range(n), key=lambda i: -vals[i])
    out = [None] * n
    for rank, i in enumerate(order):
        q = (rank + 0.5) / n
        rt = "D"
        for hi, g in RATING_CUTS:
            if q < hi:
                rt = g
                break
        out[i] = rt
    return out


def rating_abs(t):
    """绝对阈值评级（与 recalc_v410.rating_of 一致），仅备查。"""
    if t >= 85:
        return "S"
    if t >= 65:
        return "A"
    if t >= 45:
        return "B"
    if t >= 25:
        return "C"
    return "D"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("scored", nargs="?", default=None)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--force", action="store_true",
                    help="绕过回归守卫（仅用于已验证公式保真后的权重变更重算）")
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    scored = args.scored
    if scored is None:
        cand = sorted([f for f in os.listdir(HERE)
                       if f.startswith("top200_scored_") and f.endswith(".json")
                       and "_watch_" not in f and ".bak" not in f], reverse=True)
        if not cand:
            print("❌ 未找到 scored json")
            return 1
        scored = os.path.join(HERE, cand[0])
    if not os.path.exists(scored):
        print(f"❌ 不存在: {scored}")
        return 1

    doc = load_json(scored)
    rows = doc.get("results") or []
    if not rows:
        print("❌ scored 内无 results")
        return 1

    fw = load_json(os.path.join(HERE, "factor_weights.json"))
    dim_max = fw.get("dim_max") or {}
    w = {k: float(v) for k, v in (fw.get("weights") or {}).items()
         if is_num(v) and v > 0}
    sum_all = scale_calc.sum_all_weights(w)
    as_of = doc.get("date") or "2026-10-26"

    print("=" * 78)
    print(f"scored      : {os.path.basename(scored)}")
    print(f"内嵌 date   : {doc.get('date')}   （文件名日期未必相同，历史遗留）")
    print(f"样本        : {len(rows)} 只")
    print(f"factor_weights version = {fw.get('version')}   Σw = {sum_all:.6f}")
    print(f"PIT 截面日  : {as_of}")
    print("=" * 78)

    # ---------- 回归校验：旧字段必须精确复现文件内 total ----------
    maxerr, nbad, bad = 0.0, 0, []
    for r in rows:
        t, _ = total_of(r, w, dim_max, sum_all)
        old = r.get("total")
        if not is_num(old):
            continue
        e = abs(t - old)
        maxerr = max(maxerr, e)
        if e > TOL:
            nbad += 1
            if len(bad) < 5:
                bad.append((r.get("code"), r.get("name"), old, t))
    print(f"[回归校验] 旧字段复现 total 最大误差 = {maxerr:.6f}，"
          f"超容差 {nbad}/{len(rows)}")
    if nbad > 0 and not args.force:
        print("❌ 旧字段无法复现现有 total → 公式或维度集已漂移，拒绝写入。")
        for b in bad:
            print("   偏差样本:", b)
        print("   若为【已验证公式保真后的权重变更】，加 --force 可绕过本守卫。")
        return 2
    if nbad > 0 and args.force:
        print("⚠️ --force：跳过回归守卫（权重变更导致 total 必然偏移，属预期）。")

    # ---------- 1) quality / growth 重算 ----------
    print("-" * 78)
    qg = [{"code": r["code"]} for r in rows]
    nq, ng = fs_mod.score_rows(qg, as_of, pit_mode="actual_ok")
    NQ = {r["code"]: r.get("score_quality") for r in qg}
    NG = {r["code"]: r.get("score_growth") for r in qg}
    print(f"[1] fund_split  quality {nq}/{len(rows)}   growth {ng}/{len(rows)}")

    # ---------- 2) 市值双维度重算（v4.13-size2d：A 小盘成长 / B 行业龙头） ----------
    er = [{"code": r["code"], "price": r.get("price"),
           "circ_mv": r.get("circ_mv"),
           "score_growth": NG.get(r["code"]),
           "avg_amt_20d": r.get("amount")} for r in rows]
    n_ep, n_sg, drop = esi.score_rows(er, as_of)
    NE = {r["code"]: r.get("score_ep") for r in er}
    # ⚠️ v4.13：score_size_ind 已退役，改为两张互斥的表 ——
    #   NSG = A 维 score_size_growth（非龙头：行业内市值分×成长门限）
    #   NDM = B 维 score_dominant（龙头：业务份额组内前30%）
    #   一只股票只会落其中一维；两者皆 None = 该股两维都缺失（丢组/无市值）。
    NSG = {r["code"]: r.get("score_size_growth") for r in er}
    NDM = {r["code"]: r.get("score_dominant") for r in er}
    RAW = {r["code"]: r.get("score_mc_raw") for r in er}
    WMC = {r["code"]: r.get("w_mc") for r in er}
    print(f"[2] ep_size_ind  ep {n_ep}/{len(rows)}   "
          f"size_growth(A) {n_sg}/{len(rows)}   "
          f"dominant(B) {sum(1 for v in NDM.values() if is_num(v))}/{len(rows)}")
    print(f"    丢弃行业 {len(drop)} 个共 {sum(drop.values())} 只：{drop}")

    # ---------- 3) 缺成长分 / 流动性扣减留痕 ----------
    # ⚠️ 必须区分三种「拿不到市值维」的成因，否则审计会误判：
    #   ① 行业组成员 < MIN_GRP 被整组丢弃（与成长分无关，circ_mv 有但组太小）
    #   ② 成长分缺失 → w_mc=0 → A 维得 0（不是缺失，是设计上的 0 分）
    #   ③ circ_mv 缺失（本池为 0 只）
    #   原实现把 ①②混算成「缺成长分」，虚报 36 只。
    # v4.13：A/B 分区后，「缺维」判据必须是 **两维皆 None**（不能只看 A 维，
    #   否则 B 维龙头股会被误报成「市值维缺失」）。
    in_grp = {k for k, v in NSG.items() if v is not None}
    in_grp |= {k for k, v in NDM.items() if v is not None}
    n_leader = sum(1 for v in NDM.values() if is_num(v))
    # ⚠️ 判据必须是「值 is None」，不能是「key not in NS」—— er 覆盖全部行，
    #   NSG/NDM 的 key 恒存在，只是被丢组/未落维的行值为 None。用 key 判会恒得 0。
    dropped_codes = [r["code"] for r in rows
                     if NSG.get(r["code"]) is None and NDM.get(r["code"]) is None]
    no_g = sum(1 for r in rows
               if NSG.get(r["code"]) is not None and not is_num(NG.get(r["code"])))
    liq = sum(1 for r in rows
              if is_num(r.get("amount")) and r["amount"] < esi.LIQ_THRESHOLD)
    wdist = Counter(WMC.get(r["code"]) for r in rows)
    print(f"[3] 市值双维覆盖 {len(in_grp)} / {len(rows)}"
          f"（A 维 {n_sg} 只 + B 维龙头 {n_leader} 只，互斥）")
    print(f"    ① 行业内成员<{esi.MIN_GRP} 被丢组：{len(dropped_codes)} 只"
          f"（该维按缺失处理，中性填充）")
    print(f"    ② 组内但缺成长分 → w_mc=0 得 0 分：{no_g} 只")
    print(f"    ③ 日均额<{esi.LIQ_THRESHOLD/1e8:.1f}亿 被溢价减半：{liq} 只"
          f"（当前池上游已过滤≥1亿 → 恒为 0，属设计冗余）")
    print(f"    w_mc 分布：{dict((k, v) for k, v in wdist.items() if k is not None)}")

    # ---------- 4) total 重算 ----------
    old_total = {r["code"]: r.get("total") for r in rows}
    new_total = {}
    new_rows = []
    for r in rows:
        rec = dict(r)
        # ⚠️ v4.13：清退已退役维度的字段。
        #   score_size_ind 是 v4.12 的字段，其值由**旧算法**（单一成长门限市值分）算出，
        #   与新 A/B 双维不是同一口径。若留在文件里，后续任何代码/人工读到它都会
        #   误以为是当前模型的值 —— 这正是 round35 NaN 事故的同类风险
        #   （「有字段≠有信息」）。旧值保留在 .bak_v413_before 备份里可溯源。
        rec.pop("score_size_ind", None)
        if NQ.get(r["code"]) is not None:
            rec["score_quality"] = round(float(NQ[r["code"]]), 2)
        if NSG.get(r["code"]) is not None:
            rec["score_size_growth"] = round(float(NSG[r["code"]]), 2)
        if NDM.get(r["code"]) is not None:
            rec["score_dominant"] = round(float(NDM[r["code"]]), 2)
        if NE.get(r["code"]) is not None:
            rec["score_ep"] = round(float(NE[r["code"]]), 2)
        # ---- 周期行业估值锚封顶（round37 任务1，2026-10-28 老大指令①）----
        # 周期股 G_sustain_final < 7.0 → score_ep 封顶 8.5（解决福达合金类满分失真）。
        # 仅用真实已有字段(is_cyclic + score_sustain + score_ep)，不编造；
        # 降级模式下 G_sustain_final == score_sustain（Step1-4 输入缺口全 SKIP）。
        if rec.get("score_ep") is not None and cs.is_cycle_sector(rec)[0]:
            _gf, _ = cs.cycle_sector_sustain_correction(rec)
            _cap_ep, _capped = cs.val_anchor_limit(rec, _gf)
            if _capped:
                rec["score_ep_raw"] = round(float(rec["score_ep"]), 2)
                rec["score_ep"] = _cap_ep
                rec["ep_capped"] = True
            else:
                rec["ep_capped"] = False
        else:
            rec["ep_capped"] = False
        if WMC.get(r["code"]) is not None:
            rec["w_mc"] = WMC[r["code"]]
            rec["score_mc_raw"] = RAW[r["code"]]
        if NG.get(r["code"]) is not None:
            rec["score_growth"] = round(float(NG[r["code"]]), 2)
        t, miss = total_of(rec, w, dim_max, sum_all)
        rec["total"] = t
        rec["neutral_filled"] = list(miss)
        new_rows.append(rec)
        new_total[r["code"]] = t

    # 评级：分位相对口径（页面用），绝对口径存 _rating_abs 备查
    rs = rating_pct([x["total"] for x in new_rows])
    for x, g in zip(new_rows, rs):
        x["rating"] = g
        x["advice"] = ADVICE[g]
        x["_rating_abs"] = rating_abs(x["total"])
        x["weights_version"] = fw.get("version")
        x["score_model"] = "v4.13-size2d"

    # ---------- 5) 报告 ----------
    dv = sorted(new_total[k] - old_total[k] for k in old_total
                if is_num(old_total[k]))
    n = len(dv)
    print("-" * 78)
    print("[4] total 变化（新 − 旧）")
    print(f"    均值 {sum(dv)/n:+.2f}  中位 {dv[n//2]:+.2f}  "
          f"p5 {dv[n//20]:+.2f}  p95 {dv[19*n//20]:+.2f}  "
          f"最小 {dv[0]:+.2f}  最大 {dv[-1]:+.2f}")
    print(f"    下降 {sum(1 for x in dv if x < -0.005)} 只 / "
          f"上升 {sum(1 for x in dv if x > 0.005)} 只")
    n_ep_capped = sum(1 for x in new_rows if x.get("ep_capped"))
    print(f"    周期估值锚封顶(ep>8.5→8.5)：{n_ep_capped} 只"
          f"（仅用 is_cyclic+score_sustain+score_ep，降级模式）")

    # ⚠️ 评级对比必须**同一口径**：旧文件的 rating 是绝对阈值（S≥85），
    #   分位口径是 gen_report 渲染时才用（RATING_CUTS）。若直接拿旧绝对 vs
    #   新分位对比，会把「口径切换」误读成「评级恶化 176 只 A 降级」。
    #   故此处两条线都算：
    #     (a) 绝对口径：旧文件值 vs 新绝对阈值 → 反映分数变动
    #     (b) 分位口径：对旧 total 也套 RATING_CUTS vs 新分位 → 反映排序变动
    old_abs = [x.get("rating") for x in rows]
    new_abs = [rating_abs(x["total"]) for x in new_rows]
    old_pct = rating_pct([old_total[r["code"]] for r in rows
                          if is_num(old_total[r["code"]])])
    dist_o_abs = Counter(old_abs)
    dist_n_abs = Counter(new_abs)
    dist_o_pct = Counter(old_pct)
    dist_n_pct = Counter(x["rating"] for x in new_rows)

    print("-" * 78)
    print("[5] 评级分布变化")
    print(f"    (a) 绝对阈值口径（S≥85/A≥65/B≥45/C≥25）—— 反映分数水平变动")
    print(f"        {'档':<4}{'旧':>8}{'新':>8}{'Δ':>8}")
    for g in ("S", "A", "B", "C", "D"):
        o, nn = dist_o_abs.get(g, 0), dist_n_abs.get(g, 0)
        print(f"        {g:<4}{o:>8}{nn:>8}{nn - o:>+8}")
    print(f"    (b) 分位相对口径（页面实际使用）—— 反映排序变动，分布恒定")
    print(f"        {'档':<4}{'旧':>8}{'新':>8}{'Δ':>8}")
    for g in ("S", "A", "B", "C", "D"):
        o, nn = dist_o_pct.get(g, 0), dist_n_pct.get(g, 0)
        print(f"        {g:<4}{o:>8}{nn:>8}{nn - o:>+8}")
    dist_new = dist_n_pct

    def topk(d, k=50):
        return set(sorted(d, key=lambda x: -d[x])[:k])

    inter = len(topk(old_total) & topk(new_total))
    print("-" * 78)
    print(f"[6] Top50 重合度（旧 vs 新）：{inter}/50")

    # 维度分布对照
    print("-" * 78)
    print("[7] 维度分布对照")
    # v4.13：score_size_ind 已退役 → 改为 A/B 双维。
    # ⚠️ 旧文件里没有 score_size_growth / score_dominant（v4.12 只有单一 size_ind），
    #   故「旧」一栏可能为空 → st_ 必须对空列表安全返回，否则 ZeroDivisionError
    #   会在无告警的情况下打断整个报告输出（print 之前就已算完，前面的结论看不到）。
    for k in ("score_quality", "score_size_growth", "score_dominant"):
        ov = sorted(r[k] for r in rows if is_num(r.get(k)))
        nv = sorted(x[k] for x in new_rows if is_num(x.get(k)))
        def st_(v):
            if not v:
                return "n=0（本维在旧口径中不存在）"
            return (f"n={len(v)} mean {sum(v)/len(v):.2f} "
                    f"p10 {v[len(v)//10]:.2f} p50 {v[len(v)//2]:.2f} "
                    f"p90 {v[9*len(v)//10]:.2f}")
        print(f"    {k}")
        print(f"      旧 {st_(ov)}")
        print(f"      新 {st_(nv)}")

    # 威海广泰单点核验（v4.13：市值已拆 A/B 双维，同一只股票只会落其中一维）
    wg = [x for x in new_rows if x.get("code") == "sz002111"]
    if wg:
        x = wg[0]
        _is_leader = is_num(x.get("score_dominant"))
        print("-" * 78)
        print("[8] 威海广泰核验")
        print(f"    G={x.get('score_growth')}  w_mc={x.get('w_mc')}  "
              f"score_mc_raw={x.get('score_mc_raw')}")
        print(f"    分区={'B 行业龙头(业务份额前30%)' if _is_leader else 'A 小盘成长'}"
              f"  size_growth={x.get('score_size_growth')}  "
              f"dominant={x.get('score_dominant')}")
        print(f"    quality={x.get('score_quality')}  "
              f"total {old_total.get('sz002111')} → {x['total']} ({x['rating']})")

    if args.dry_run:
        print("\n[--dry-run] 未写盘。")
        return 0

    # ---------- 写回 ----------
    out = args.out or scored
    new_rows.sort(key=lambda x: (-(x.get("total") or 0),
                                 -(x.get("turnover") or 0)))
    doc["results"] = new_rows
    doc["weights"] = {k: (float(v) if is_num(v) else 0.0)
                      for k, v in (fw.get("weights") or {}).items()}
    doc["weights_note"] = (
        "v4.13-size2d（2026-10-28）：① fund_split NaN 污染已修复，quality 为"
        "确定性可复现分；② score_size_ind 改为成长条件交互分 "
        "final=raw×w_mc（w_mc: G>7→0.8 / 4<G≤7→0.3 / G≤4→0），clip[0,10]；"
        "③ **市值维度拆为两个互斥维度** —— A score_size_growth(8%) 小盘成长溢价"
        "（上述成长条件交互分，对非龙头生效）；B score_dominant(6%) 行业龙头壁垒"
        "（业务份额=营收TTM+扣非净利TTM ÷ 申万二级行业内本池合计，合成 rank 前30%"
        "即龙头 syn≥7.0，再做市值一致性校验 mc_pct≥0.8×业务份额 与流动性惩罚）；"
        "市值合计 10%→14%，差额 4% 由 quality(25.2%→21.2%) 回吐，Σw=1.0。"
        "⚠️ total 与 v4.10 及更早不可比。")
    doc["model"] = "v4.13-size2d-q212"
    doc["cycle_anchor_cap"] = {
        "applied": True, "n_capped": n_ep_capped, "cap": 8.5,
        "as_of": "2026-10-28",
        "note": "round37 任务1：周期股 G_sustain_final<7.0 → score_ep 封顶 8.5；"
                "降级模式（Step1-4 输入缺口全 SKIP），仅用真实已有字段。",
    }
    st = doc.get("stats") or {}
    st["rating_dist"] = {g: dist_new.get(g, 0) for g in ("S", "A", "B", "C", "D")}
    st["size_gate"] = {
        "G_HIGH": esi.G_HIGH, "G_LOW": esi.G_LOW,
        "W_HIGH": esi.W_HIGH, "W_MID": esi.W_MID, "W_LOW": esi.W_LOW,
        "LIQ_THRESHOLD_yi": esi.LIQ_THRESHOLD / 1e8,
        "w_mc_dist": {str(k): v for k, v in wdist.items()},
        "no_growth": no_g, "liq_cut": liq,
    }
    # v4.13-size2d：市值双维度分区留痕（A/B 互斥，覆盖率与龙头数可复现核对）
    st["size_dual_dim"] = {
        "model": "v4.13-size2d",
        "A_size_growth_weight": 0.08,
        "B_dominant_weight": 0.06,
        "DOM_LEADER_THR": esi.DOM_LEADER_THR,
        "DOM_REV_W": esi.DOM_REV_W,
        "DOM_PROFIT_W": esi.DOM_PROFIT_W,
        "DOM_MC_CONSIST": esi.DOM_MC_CONSIST,
        "DOM_DISCOUNT": esi.DOM_DISCOUNT,
        "n_A": n_sg,
        "n_B_leaders": n_leader,
        "n_both_missing": len(dropped_codes),
        "n_total": len(rows),
        "note": "A/B 互斥分区：A=非龙头（行业内市值分×成长门限）；"
                "B=业务份额组内前30%的行业龙头。两维皆 None 才是市值维缺失。",
    }
    doc["stats"] = st

    tmp = out + ".tmp"
    with io.open(tmp, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, separators=(",", ":"))
    os.replace(tmp, out)
    print(f"\n✅ 已写回 {os.path.basename(out)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
