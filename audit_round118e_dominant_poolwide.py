#!/usr/bin/env python3
"""audit_round118e_dominant_poolwide.py —— P0-3 决策依据：把 score_dominant 从
「组内前 30% 龙头」扩到**全池**，到底会注入正 alpha 还是负 alpha。

## 为什么必须先做这个诊断，不能直接改代码

`reports/round118_project_assessment_2026-11-03.md` 1.3 节把 dominant 覆盖率
26.6% 记为 P0 缺陷，理由是「9 维权重声明与实测不符」。这个事实本身成立，但
**成因不是数据缺失**（实测：申万二级映射 100%、EM 财务缓存 100%、营收+利润
TTM 齐全 99.7%、全池 syn 可算 99.1%），而是 `ep_size_ind.score_rows` 里一行
互斥分区：

    is_leader = syn[c6] >= DOM_LEADER_THR      # 组内前 30%
    if is_leader:  r["score_dominant"] = ...   # 只有龙头写 dominant
    else:          r["score_size_growth"] = ...# 其余写给 size_growth

所以「扩到全池」= **拆掉 A/B 互斥分区**，是模型结构变更，不是补数据。拆之前
必须回答一个问题：**全池 dominant 的历史 IC 是正还是负。** 为负则扩展等于往
账户里注入负 alpha，覆盖率从 26.6% 涨到 99% 反而是变得更糟。

## 三个必须分开量的口径

1. **全池 dominant（本脚本主口径）**：syn 按申万二级组内 rank 合成，含流动性
   惩罚；**不含**市值一致性折扣 —— 因为折扣要 `mc_pct`，而 `mc_pct` 要
   `circ_mv`，90 个快照里只有 8 个有 circ_mv。历史 IC 必须用能贯穿全历史的
   口径，否则测的是 8 个截面而不是 79 个。
2. **现生产分区版**：同一快照只取 syn>=7.0 的那 543 只，看这 543 只内部 IC。
   ⚠️ 这个数**不能**直接和口径 1 比大小 —— 样本从 2066 只缩到 543 只，
   IC 的抽样误差完全不同（记忆第 ⑲ 条：临界值随 n 变化，不是常数 2.0）。
3. **方向一致性**：dominant 是「份额越大越高分」，size_growth 是「市值越小
   越高分」，两者方向相反。拆分区后同一只股票会**同时**拿到两个方向的分数，
   市值大类（合计 0.30）可能内部对冲。故必须报两者的相关性。

## 纪律

- 所有 IC 必须带**截面数 n** 与 NW t；n<5 的口径直接标「不可用」不出结论。
- 不写任何数据文件以外的结论进代码；结论由 scripts 输出后人工判读。
"""
import collections
import json
import math
import os

import ep_size_ind as E
import factor_eval as F

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "data", "audit_round118e_dominant_poolwide.json")

HORIZONS = (1, 5, 20)
MIN_XS = 50          # 单截面最少股票数，低于此不算 IC


def c6of(code):
    c = (code or "").strip().lower()
    return c[2:] if len(c) > 6 and c[:2] in ("sh", "sz", "bj") else c


def build_syn(sn, em, sw):
    """单个快照 → {code6: 全池 dominant 分}。

    syn = 0.5×组内营收份额rank + 0.5×组内利润份额rank（申万二级组内）。
    再叠流动性惩罚（不依赖 circ_mv，可贯穿全历史）。
    ⚠️ **不含**市值一致性折扣 —— 见模块 docstring 口径 1。
    """
    rows = sn["rows"]
    as_of = sn["date"]
    l2map = (sw or {}).get("l2") or {}
    sh = E.build_shares(rows, as_of, em, sw)
    rg = collections.defaultdict(list)
    pg = collections.defaultdict(list)
    for c6, s in sh.items():
        l2 = l2map.get(c6)
        if not l2:
            continue
        if isinstance(s["rev_share"], (int, float)):
            rg[l2].append((c6, s["rev_share"]))
        if isinstance(s["profit_share"], (int, float)):
            pg[l2].append((c6, s["profit_share"]))
    rr = E.group_rank10(rg)
    pr = E.group_rank10(pg)
    syn = {c: E.DOM_REV_W * rr[c] + E.DOM_PROFIT_W * pr[c]
           for c in sh if c in rr and c in pr}
    out = {}
    for r in rows:
        c6 = c6of(r.get("code"))
        v = syn.get(c6)
        if v is None:
            continue
        amt = r.get("amount")
        if isinstance(amt, (int, float)) and amt == amt and amt < E.LIQ_THRESHOLD:
            v *= E.LIQ_PENALTY
        out[c6] = max(0.0, min(10.0, v))
    return out, syn


def nw_t(xs, lag):
    """Newey-West t（lag=hop−1，不是拍 4 —— 记忆第 ⑮ 条）。"""
    n = len(xs)
    if n < 3:
        return None
    m = sum(xs) / n
    d = [x - m for x in xs]
    g0 = sum(x * x for x in d) / n
    var = g0
    for L in range(1, min(lag, n - 1) + 1):
        gl = sum(d[i] * d[i - L] for i in range(L, n)) / n
        var += 2.0 * (1.0 - L / (lag + 1.0)) * gl
    if var <= 0:
        return None
    return m / math.sqrt(var / n)


def main():
    print("加载数据…", flush=True)
    hist = F.load_history()
    snaps = hist.get("snapshots") or []
    # ⚠️ 必须用 px_index()，**不是** bootstrap_prices()。
    #   后者名字像"取价格"，实际会逐只发起网络请求（~709 次）并 save_history() 写盘。
    #   本审计是只读诊断，调用它等于在诊断过程中重刷数据 —— 我第一次踩了这个坑，
    #   把 factor_history.json 的 prices 段整体重抓了一遍（结果方向是改善，但过程不可控：
    #   905 只抓取失败）。**"看起来像 getter 的名字"必须读函数体再调。**
    tdays, px = F.px_index(hist)
    sw = json.load(open(os.path.join(HERE, "sector", "sw_map.json"), encoding="utf-8"))
    em = E.load_em_all()
    print(f"  snaps={len(snaps)} tdays={len(tdays)} px={len(px)} em={len(em)}", flush=True)

    by_date = {s["date"]: s for s in snaps}
    res = {"schema": "audit_round118e/1", "horizons": {}, "coverage": {},
           "leader_thr": E.DOM_LEADER_THR, "min_xs": MIN_XS}

    # 先一次性算好每个快照的 syn（避免 3 个 horizon 重复算）
    print("逐快照计算全池 dominant…", flush=True)
    syn_by_date = {}
    cov_rows = {}
    for i, sn in enumerate(snaps):
        full, syn = build_syn(sn, em, sw)
        syn_by_date[sn["date"]] = {"full": full, "syn": syn}
        cov_rows[sn["date"]] = {
            "n_rows": len(sn["rows"]),
            "n_full": len(full),
            "n_leader": sum(1 for v in syn.values() if v >= E.DOM_LEADER_THR),
        }
        if (i + 1) % 10 == 0:
            print(f"  {i + 1}/{len(snaps)}", flush=True)

    ns_full = [v["n_full"] / max(1, v["n_rows"]) for v in cov_rows.values()]
    ns_lead = [v["n_leader"] / max(1, v["n_rows"]) for v in cov_rows.values()]
    res["coverage"] = {
        "poolwide_mean_pct": round(100 * sum(ns_full) / len(ns_full), 2),
        "leader_mean_pct": round(100 * sum(ns_lead) / len(ns_lead), 2),
        "poolwide_min_pct": round(100 * min(ns_full), 2),
        "note": "全池口径覆盖率 vs 现生产龙头口径覆盖率（同 90 个快照逐日均值）",
    }
    print(f"覆盖率：全池 {res['coverage']['poolwide_mean_pct']}% / "
          f"龙头 {res['coverage']['leader_mean_pct']}%", flush=True)

    for h in HORIZONS:
        print(f"\n=== h={h} ===", flush=True)
        fwd = F._fwd_returns(snaps, h, tdays, px)
        ic_all, ic_lead, ic_sg, n_rows_used = [], [], [], []

        # 相关性：同一截面上 全池dominant(在非龙头上) vs size_growth
        corr_pairs = []

        for d0, d1, rets in fwd:
            sn = by_date.get(d0)
            if sn is None:
                continue
            pack = syn_by_date.get(d0)
            if not pack:
                continue
            full = pack["full"]
            syn = pack["syn"]
            fv, rv = [], []
            lv, lr = [], []
            for c6, v in full.items():
                if c6 not in rets:
                    continue
                fv.append(v)
                rv.append(rets[c6])
                if syn.get(c6, 0) >= E.DOM_LEADER_THR:
                    lv.append(v)
                    lr.append(rets[c6])
            if len(fv) >= MIN_XS:
                ic = F.rank_ic(fv, rv)
                if ic is not None:
                    ic_all.append(ic)
                    n_rows_used.append(len(fv))
            if len(lv) >= MIN_XS:
                ic = F.rank_ic(lv, lr)
                if ic is not None:
                    ic_lead.append(ic)

            # 相关性（同一截面上全池 dominant vs size_growth）
            sg = {c6of(r.get("code")): r.get("score_size_growth")
                  for r in sn["rows"]
                  if isinstance(r.get("score_size_growth"), (int, float))}
            xs, ys = [], []
            for c6 in full:
                if c6 in sg and sg[c6] is not None:
                    xs.append(full[c6])
                    ys.append(sg[c6])
            if len(xs) >= MIN_XS:
                corr_pairs.append((xs, ys))

        def _summ(ics, lag):
            if not ics:
                return {"n": 0, "note": "无有效截面"}
            m = sum(ics) / len(ics)
            return {"n": len(ics), "ic_mean": round(m, 4),
                    "nw_t": (round(nw_t(ics, lag), 3) if nw_t(ics, lag) is not None else None),
                    "nw_lag": lag,
                    "ic_pos_share": round(sum(1 for x in ics if x > 0) / len(ics), 3),
                    "ic_min": round(min(ics), 4), "ic_max": round(max(ics), 4)}

        lag = max(0, h - 1)
        res["horizons"][str(h)] = {
            "poolwide": _summ(ic_all, lag),
            "leader_only": _summ(ic_lead, lag),
            "mean_xs_size_poolwide": round(sum(n_rows_used) / len(n_rows_used), 1)
            if n_rows_used else None,
            "leader_note": ("⚠️ leader_only 每截面只有约 543 只（现生产分区），"
                            "与 poolwide 的 2066 只**不可直接比大小** —— "
                            "临界值随 n 变化（记忆第 ⑲ 条）"),
        }

        # 平均截面相关
        if corr_pairs:
            cs = []
            for xs, ys in corr_pairs:
                mx = sum(xs) / len(xs)
                my = sum(ys) / len(ys)
                cov = sum((a - mx) * (b - my) for a, b in zip(xs, ys))
                sx = math.sqrt(sum((a - mx) ** 2 for a in xs))
                sy = math.sqrt(sum((b - my) ** 2 for b in ys))
                if sx > 0 and sy > 0:
                    cs.append(cov / (sx * sy))
            if cs:
                res["horizons"][str(h)]["corr_vs_size_growth"] = {
                    "n_xs": len(cs), "mean": round(sum(cs) / len(cs), 4),
                    "min": round(min(cs), 4), "max": round(max(cs), 4),
                }

        print(json.dumps(res["horizons"][str(h)], ensure_ascii=False, indent=2), flush=True)

    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as fh:
        json.dump(res, fh, ensure_ascii=False, indent=2)
    print(f"\n✅ 写出 {OUT}", flush=True)


if __name__ == "__main__":
    main()
