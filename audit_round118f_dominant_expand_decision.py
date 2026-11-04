#!/usr/bin/env python3
"""audit_round118f_dominant_expand_decision.py —— P0-3 落地前的**决策依据**。

`audit_round118e` 已实测：全池 dominant 覆盖率 97.2%（vs 龙头 28.7%），
IC 在 h1/h5 上强于龙头版，但 **h20（生产 target_horizon）反而更弱**
（全池 +0.0311/t=0.518 vs 龙头 +0.0525/t=1.332）。所以「扩到全池更好」这句话
**不能笼统说**，必须拆成三问：

## 问 1：增量信号来自哪个子样本？
全池版包含龙头 543 只 + 非龙头 1523 只。若增量全来自非龙头，说明
「份额排名在中小市值公司里也有区分度」；若两段都正，说明该维在整池单调。
→ 分别算龙头子样本 / 非龙头子样本的 IC。

## 问 2：替换后复合 IC 是升还是降？
这是**唯一真正重要的判据**。单维 IC 上升不等于总分更好看 —— 可能被
共线维抵消。用生产 9 维权重、逐行可用维重归一化（与 `_composite_ic`
同口径），只改 `score_dominant` 的填充范围，其余全不动，比三档：
  A 现行：dominant 只填龙头（其余 None）        ← baseline
  B 全池：dominant 填全池（其余 None）
  C 全池+size_growth 也让位：dominant 填全池，size_growth 仍只填非龙头
     （⚠️ 这不是新方案，只是用来分离「谁在贡献」）

## 问 3：h20 上的劣势是否致命？
h=20 是本项目 target_horizon（T+10~T+20 持有）。若全池版在 h20 上 IC 更低，
则「覆盖率」与「目标周期有效性」存在真实冲突，**必须让老大看到这个冲突**，
不能用 h1 的显著性去掩盖 h20 的退化。

## 纪律
- 所有 IC 必带 n 与 NW t(lag=h−1)；n<5 不出结论。
- 不给「建议采纳」的结论性文字 —— 数字出来由老大裁决。
- 只读：不调用 `bootstrap_prices`（会发网络请求 + 写盘）。
"""
import collections
import json
import math
import os

import ep_size_ind as E
import factor_eval as F

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "data", "audit_round118f_dominant_expand_decision.json")

HORIZONS = (1, 5, 20)
MIN_XS = 50
# 生产 9 维权重（v4.17-cycle-rules），用于复合 IC 对照
W9 = {
    "score_fund": 0.20, "score_size_growth": 0.20, "score_tech_neg": 0.10,
    "score_quality": 0.10, "score_ep": 0.10, "score_dominant": 0.10,
    "score_growth": 0.10, "score_bias60": 0.05, "score_rsi_rev": 0.05,
}


def c6of(code):
    c = (code or "").strip().lower()
    return c[2:] if len(c) > 6 and c[:2] in ("sh", "sz", "bj") else c


def nw_t(xs, lag):
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


def _summ(ics, lag):
    if not ics:
        return {"n": 0, "note": "无有效截面"}
    t = nw_t(ics, lag)
    return {"n": len(ics), "ic_mean": round(sum(ics) / len(ics), 4),
            "nw_t": (round(t, 3) if t is not None else None), "nw_lag": lag,
            "ic_pos_share": round(sum(1 for x in ics if x > 0) / len(ics), 3)}


def build_syn(sn, em, sw):
    """→ ({c6: 全池 dominant}, {c6: syn 原始合成}) 。不含市值一致性折扣（要 circ_mv）。"""
    l2map = (sw or {}).get("l2") or {}
    sh = E.build_shares(sn["rows"], sn["date"], em, sw)
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
    full = {}
    for r in sn["rows"]:
        c6 = c6of(r.get("code"))
        v = syn.get(c6)
        if v is None:
            continue
        amt = r.get("amount")
        if isinstance(amt, (int, float)) and amt == amt and amt < E.LIQ_THRESHOLD:
            v *= E.LIQ_PENALTY
        full[c6] = max(0.0, min(10.0, v))
    return full, syn


def composite_ic(rows, rets, dims, w, dom_values, use_size_growth=True):
    """生产口径复合分与未来收益的秩相关。

    逐行按**可用维**重新归一化权重（与 factor_eval._composite_ic 同口径）。
    dom_values: {c6: dominant}；use_size_growth=False 时把 size_growth 从可用维里摘掉。
    """
    fv, rv = [], []
    for r in rows:
        c6 = c6of(r.get("code"))
        if c6 not in rets:
            continue
        vals = {}
        for d in dims:
            if d == "score_dominant":
                v = dom_values.get(c6)
            else:
                v = r.get(d)
            if isinstance(v, (int, float)):
                vals[d] = float(v)
        if not use_size_growth:
            vals.pop("score_size_growth", None)
        if "score_fund" not in vals or len(vals) < 2:
            continue
        ws = sum(w[d] for d in vals)
        if ws <= 0:
            continue
        fv.append(sum(w[d] / ws * vals[d] for d in vals))
        rv.append(rets[c6])
    if len(fv) < MIN_XS:
        return None, len(fv)
    return F.rank_ic(fv, rv), len(fv)


def main():
    print("加载…", flush=True)
    hist = F.load_history()
    snaps = hist.get("snapshots") or []
    tdays, px = F.px_index(hist)
    sw = json.load(open(os.path.join(HERE, "sector", "sw_map.json"), encoding="utf-8"))
    em = E.load_em_all()
    by_date = {s["date"]: s for s in snaps}
    print(f"  snaps={len(snaps)} tdays={len(tdays)} em={len(em)}", flush=True)

    print("逐快照算 syn…", flush=True)
    pack_by_date = {}
    for i, sn in enumerate(snaps):
        full, syn = build_syn(sn, em, sw)
        pack_by_date[sn["date"]] = {"full": full, "syn": syn}
        if (i + 1) % 20 == 0:
            print(f"  {i + 1}/{len(snaps)}", flush=True)

    dims = list(W9.keys())
    res = {"schema": "audit_round118f/1", "leader_thr": E.DOM_LEADER_THR,
           "weights_used": W9, "min_xs": MIN_XS, "horizons": {}}

    for h in HORIZONS:
        print(f"\n=== h={h} ===", flush=True)
        fwd = F._fwd_returns(snaps, h, tdays, px)
        lag = max(0, h - 1)
        # 问1：分样本 IC
        ic_pool, ic_lead, ic_non = [], [], []
        # 问2：三档复合 IC
        icA, icB, icC = [], [], []
        nxs = {"pool": [], "lead": [], "non": []}

        for d0, d1, rets in fwd:
            sn = by_date.get(d0)
            pk = pack_by_date.get(d0)
            if sn is None or not pk:
                continue
            full, syn = pk["full"], pk["syn"]
            pv, pr_, lv, lr, nv, nr = [], [], [], [], [], []
            for c6, v in full.items():
                if c6 not in rets:
                    continue
                pv.append(v)
                pr_.append(rets[c6])
                if syn.get(c6, 0) >= E.DOM_LEADER_THR:
                    lv.append(v)
                    lr.append(rets[c6])
                else:
                    nv.append(v)
                    nr.append(rets[c6])
            if len(pv) >= MIN_XS:
                ic = F.rank_ic(pv, pr_)
                if ic is not None:
                    ic_pool.append(ic)
                    nxs["pool"].append(len(pv))
            if len(lv) >= MIN_XS:
                ic = F.rank_ic(lv, lr)
                if ic is not None:
                    ic_lead.append(ic)
                    nxs["lead"].append(len(lv))
            if len(nv) >= MIN_XS:
                ic = F.rank_ic(nv, nr)
                if ic is not None:
                    ic_non.append(ic)
                    nxs["non"].append(len(nv))

            # 复合 IC 三档
            only_lead = {c: v for c, v in full.items()
                         if syn.get(c, 0) >= E.DOM_LEADER_THR}
            for bucket, domv, usg in ((icA, only_lead, True),
                                      (icB, full, True),
                                      (icC, full, False)):
                ic, nn = composite_ic(sn["rows"], rets, dims, W9, domv, usg)
                if ic is not None:
                    bucket.append(ic)

        res["horizons"][str(h)] = {
            "q1_subset_ic": {
                "poolwide": _summ(ic_pool, lag),
                "leader": _summ(ic_lead, lag),
                "non_leader": _summ(ic_non, lag),
                "mean_xs": {k: (round(sum(v) / len(v), 1) if v else None)
                            for k, v in nxs.items()},
            },
            "q2_composite_ic": {
                "A_current_partition": _summ(icA, lag),
                "B_poolwide_dominant": _summ(icB, lag),
                "C_poolwide_dom_no_size_growth": _summ(icC, lag),
                "note": ("A=现行（dominant 只填龙头）；B=dominant 填全池；"
                         "C=dominant 填全池且复合分里摘掉 size_growth"
                         "（不是候选方案，只用于分离贡献）"),
            },
        }
        print(json.dumps(res["horizons"][str(h)], ensure_ascii=False, indent=2), flush=True)

    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as fh:
        json.dump(res, fh, ensure_ascii=False, indent=2)
    print(f"\n✅ 写出 {OUT}", flush=True)


if __name__ == "__main__":
    main()
