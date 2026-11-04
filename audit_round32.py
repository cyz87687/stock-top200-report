#!/usr/bin/env python3
"""round32：T0-A/B 否决后的追因 + T0-C 亏损股 fund 分成因验证。

## 背景（round31 结论）

round31 把 tech_neg 降权/重标定、bias60×rsi_rev 合并/正交化共 9 个改法
全跑了一遍，结果 **无一能过配对检验**，且三个重标定方案显著更差：

    A3 tech_neg 改池内 rank    h5 Δ−0.0216 (配对 t −4.96)
    A4 tech_neg 线性拉伸       h5 Δ−0.0088 (配对 t −4.24)
    B2 rsi_rev 正交化          h5 Δ−0.0196 (配对 t −3.40)
    B4 只留 rsi_rev .35        h5 Δ−0.0220 (配对 t −3.80)
    A1 tech_neg 降半           h5 Δ+0.0073 (t +0.96) / h20 Δ−0.0003 → 不显著
     B1 合并均值               maxdiff 8.9e-16 = **数学恒等**（假对照，round25c 同类陷阱）

本轮回答两个「为什么」，它们决定 T0 到底该改还是该只披露：

**Q1** 为什么把 tech_neg 拉满量程反而变差？
     假设：该维窄幅分布携带的信息量本就少；它与反转块共线 +0.73，
     拉伸 = 放大共线噪音。→ 用「分位剥离」检验：把 tech_neg 对反转块回归后，
     残差是否还有 alpha。

**Q2** 亏损股（pe==0）靠 fwd_pe 拿到 fund 分（最高 4.60）是否合理？
     生产实证：421 只亏损股中 155 只有 fwd_pe → 拿到 fund（均 2.12，最高 4.60，
     15 只 ≥4）；266 只无 → 中性 2.5。而 4.60 分的典型是「当前亏损 + 卖方预测
     明年 PE 19x + growth +103%（从负转正的基数效应）→ PEG=0.19 严重低估」。
     检验：亏损股组的 fund IC 是否显著弱于盈利股组？若弱/为负 → 该维对亏损股是噪音。

## 亏损/盈利分类口径

历史面板无 pe/fwd_pe，用 EM 缓存重建：归母净利润 TTM（PIT）≤0 → 亏损。
⚠️ 必须用 pit_period 取当时可见报告期，禁拿最新财报回填历史（未来函数）。
"""
import json
import math
import os
import sys
from collections import defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import factor_eval as fe
import audit_round21 as r21
import audit_round24b as rb
import audit_round26 as r26
import scale_calc

T_BONF = 3.08
HOPS = [5, 10, 20]
LAGS = [0, 3, 6]

# round125: dim_max 改为**单一来源**（原为硬编码副本）。
#   铁律：量程只能来自 factor_weights.json → dim_cfg.load_dim_max。
from dim_cfg import load_dim_max
DIM_MAX = load_dim_max(['score_fund', 'score_quality', 'score_tech_neg', 'score_bias60', 'score_rsi_rev', 'score_lowvol20', 'score_ep', 'score_size_ind'], strict=True)
W_V49 = {"score_fund": 0.075, "score_quality": 0.12, "score_tech_neg": 0.15,
         "score_bias60": 0.175, "score_rsi_rev": 0.175, "score_lowvol20": 0.13,
         "score_ep": 0.075, "score_size_ind": 0.10}


def nw_t(xs, lag):
    n = len(xs)
    if n < 5:
        return None
    mu = sum(xs) / n
    d = [x - mu for x in xs]
    var = sum(x * x for x in d) / n
    for L in range(1, min(lag, n - 1) + 1):
        gl = sum(d[i] * d[i - L] for i in range(L, n)) / n
        var += 2 * (1 - L / (lag + 1)) * gl
    se = math.sqrt(var / n) if var > 0 else None
    return (mu / se) if se else None


def stat(ics):
    n = len(ics)
    if n < 5:
        return None
    mu = sum(ics) / n
    sd = math.sqrt(sum((x - mu) ** 2 for x in ics) / n) if n > 1 else 0.0
    k = n // 2
    h1 = sum(ics[:k]) / k
    h2 = sum(ics[k:]) / (n - k)
    ts = {L: nw_t(ics, L) for L in LAGS}
    return {"n": n, "ic": round(mu, 4),
            "icir": round(mu / sd, 3) if sd > 1e-9 else None,
            "t_by_lag": {str(L): (round(v, 2) if v is not None else None)
                         for L, v in ts.items()},
            "pass_bonf": bool(all(v is not None and abs(v) >= T_BONF
                                  for v in ts.values())),
            "ic_pos_share": round(sum(1 for x in ics if x > 0) / n, 3),
            "sub_first": round(h1, 4), "sub_second": round(h2, 4),
            "sub_both_pos": bool(h1 > 0 and h2 > 0)}


def paired_t(a, b):
    if len(a) != len(b) or len(a) < 5:
        return None, None
    d = [x - y for x, y in zip(a, b)]
    return round(sum(d) / len(d), 4), (round(nw_t(d, 0), 2) if nw_t(d, 0) is not None else None)


def main():
    h = json.load(open(os.path.join(HERE, "factor_history.json"), encoding="utf-8"))
    snaps, _ = fe._drop_stale_price_snapshots(h["snapshots"])
    tdays, px = fe.px_index(h)
    tidx = {d: i for i, d in enumerate(tdays)}
    em_f = r26.load_em_all()

    print("=" * 106)
    print("round32：T0-A/B 否决追因 + T0-C 亏损股 fund 分组验证")
    print("=" * 106)
    print(f"截面 {len(snaps)} · 交易日 {len(tdays)} · EM(full) {len(em_f)}")

    _sh = defaultdict(list)
    for s in snaps:
        for r in s["rows"]:
            mv, pr = r.get("circ_mv"), r.get("price")
            if isinstance(mv, (int, float)) and mv and isinstance(pr, (int, float)) and pr > 0:
                _sh[r["code"]].append(mv / pr)
    shares_est = {}
    for c, v in _sh.items():
        v = sorted(v)
        shares_est[c] = v[len(v) // 2]

    # ---------- 亏损标识：归母净利润 TTM（PIT） <= 0 ----------
    # 分母用总市值；EM 缓存口径下的净利润为元
    loss_by_date = defaultdict(dict)     # d0 -> code -> True(亏损) / False(盈利) / None(未知)
    mv_by_date = defaultdict(dict)
    for s in snaps:
        d0 = s["date"]
        for r in s["rows"]:
            c6 = (r.get("code") or "")[-6:]
            ind = em_f.get(c6)
            m = r.get("circ_mv")
            if not (isinstance(m, (int, float)) and m > 0):
                sh = shares_est.get(r.get("code"))
                pr = (px.get(c6) or {}).get(d0) or r.get("price")
                if sh and isinstance(pr, (int, float)) and pr > 0:
                    m = sh * pr
            if isinstance(m, (int, float)) and m > 0:
                mv_by_date[d0][c6] = float(m)
            if not ind:
                continue
            P = r21.pit_period(sorted({p for dd in ind.values() for p in dd},
                                     reverse=True), d0)
            if P is None:
                continue
            np_ttm = r26.ttm_sum(ind, "归母净利润", P)
            if np_ttm is None:
                continue
            loss_by_date[d0][c6] = bool(np_ttm <= 0)

    n_loss = sum(1 for d in loss_by_date.values() for v in d.values() if v)
    n_prof = sum(1 for d in loss_by_date.values() for v in d.values() if not v)
    print(f"亏损/盈利标识：亏损 {n_loss} 股票日 / 盈利 {n_prof} 股票日")

    # ---------- 反转块回填 ----------
    bf = rb.backfill_reversal(tdays, px)

    def dim_val(k, r, d0):
        if k == "score_fund":
            v = r.get("score_fund")
            return v if isinstance(v, (int, float)) and not isinstance(v, bool) else None
        if k == "score_tech_neg":
            v = r.get("score_tech")
            return (10.0 - float(v)) if isinstance(v, (int, float)) else None
        if k in ("score_bias60", "score_rsi_rev"):
            t = (bf.get(d0) or {}).get(r.get("code"))
            if not t:
                return None
            return t[1] if k == "score_bias60" else t[2]
        return None

    def fwd_ret(c6, d0, hop):
        m = px.get(c6)
        i0 = tidx.get(d0)
        if not m or i0 is None or i0 + hop >= len(tdays):
            return None
        p0, p1 = m.get(tdays[i0]), m.get(tdays[i0 + hop])
        if p0 is None or p1 is None or p0 <= 0:
            return None
        return p1 / p0 - 1.0

    # =====================================================================
    print()
    print("─" * 106)
    print("Q1：tech_neg 与反转块的共线有多深？剥离反转块后是否还有 alpha？")
    print("─" * 106)

    # 逐截面：tech_neg 对 (bias60, rsi_rev) 回归，取残差；同时算原始 IC
    raw_ics = {hp: [] for hp in HOPS}
    resid_ics = {hp: [] for hp in HOPS}
    corr_bt, corr_rt = [], []

    def _rank(vals):
        idx = sorted(range(len(vals)), key=lambda i: vals[i])
        rr = [0.0] * len(vals)
        i = 0
        while i < len(idx):
            j = i
            while j + 1 < len(idx) and vals[idx[j + 1]] == vals[idx[i]]:
                j += 1
            avg = (i + j) / 2.0 + 1
            for kk in range(i, j + 1):
                rr[idx[kk]] = avg
            i = j + 1
        return rr

    def _pearson(xs, ys):
        n = len(xs)
        if n < 5:
            return None
        mx, my = sum(xs) / n, sum(ys) / n
        sxy = sum((xs[i] - mx) * (ys[i] - my) for i in range(n))
        sxx = sum((xs[i] - mx) ** 2 for i in range(n)) ** 0.5
        syy = sum((ys[i] - my) ** 2 for i in range(n)) ** 0.5
        return sxy / (sxx * syy) if sxx > 0 and syy > 0 else None

    for s in snaps:
        d0 = s["date"]
        recs = []
        for r in s["rows"]:
            t = dim_val("score_tech_neg", r, d0)
            b = dim_val("score_bias60", r, d0)
            v = dim_val("score_rsi_rev", r, d0)
            if t is None or b is None or v is None:
                continue
            recs.append((r["code"], t, b, v,
                         {hp: fwd_ret((r.get("code") or "")[-6:], d0, hp) for hp in HOPS}))
        if len(recs) < 40:
            continue
        c1 = _pearson(_rank([x[1] for x in recs]), _rank([x[2] for x in recs]))
        c2 = _pearson(_rank([x[1] for x in recs]), _rank([x[3] for x in recs]))
        if c1 is not None:
            corr_bt.append(c1)
        if c2 is not None:
            corr_rt.append(c2)

        # 二元 OLS: tech_neg ~ bias60 + rsi_rev
        xs1 = [x[2] for x in recs]
        xs2 = [x[3] for x in recs]
        ys = [x[1] for x in recs]
        n = len(recs)
        m1, m2, my = sum(xs1) / n, sum(xs2) / n, sum(ys) / n
        s11 = sum((v - m1) ** 2 for v in xs1)
        s22 = sum((v - m2) ** 2 for v in xs2)
        s12 = sum((xs1[i] - m1) * (xs2[i] - m2) for i in range(n))
        s1y = sum((xs1[i] - m1) * (ys[i] - my) for i in range(n))
        s2y = sum((xs2[i] - m2) * (ys[i] - my) for i in range(n))
        det = s11 * s22 - s12 * s12
        if abs(det) < 1e-12:
            continue
        b1 = (s22 * s1y - s12 * s2y) / det
        b2 = (s11 * s2y - s12 * s1y) / det
        a0 = my - b1 * m1 - b2 * m2
        resid = [(c, ys[i] - (a0 + b1 * xs1[i] + b2 * xs2[i]), None, fw)
                 for i, (c, _t, _b, _v, fw) in enumerate(recs)]

        for hp in HOPS:
            pr = [(x[1], x[4][hp]) for x in recs if x[4][hp] is not None]
            if len(pr) >= 40:
                ic = fe.rank_ic([p[0] for p in pr], [p[1] for p in pr])
                if ic is not None:
                    raw_ics[hp].append(ic)
            pr2 = [(x[1], x[3][hp]) for x in resid if x[3][hp] is not None]
            if len(pr2) >= 40:
                ic2 = fe.rank_ic([p[0] for p in pr2], [p[1] for p in pr2])
                if ic2 is not None:
                    resid_ics[hp].append(ic2)

    print(f"  tech_neg × bias60  平均秩相关 = {sum(corr_bt)/len(corr_bt):+.3f} (n={len(corr_bt)} 截面)")
    print(f"  tech_neg × rsi_rev 平均秩相关 = {sum(corr_rt)/len(corr_rt):+.3f}")
    print()
    print(f"  {'口径':<34} {'hop':>4} {'IC':>9} {'t0':>7} {'Bonf':>6} {'n':>4}")
    for hp in HOPS:
        for nm, arr in (("tech_neg 原始（生产线口径）", raw_ics[hp]),
                        ("tech_neg 剥离反转块后残差", resid_ics[hp])):
            st = stat(arr)
            if not st:
                continue
            print(f"  {nm:<34} h{hp:<3} {st['ic']:>+9.4f} {st['t_by_lag']['0']:>+7.2f} "
                  f"{'✅' if st['pass_bonf'] else '⚠️':>6} {st['n']:>4}")
    print()
    print("  → 若残差 IC 大幅衰减（<原始一半）说明 tech_neg 的 alpha 主要是反转块的影子，")
    print("    强行拉满量程只会放大共线噪音 —— 这解释了 round31 里 A3/A4 显著变差。")

    # =====================================================================
    print()
    print("─" * 106)
    print("Q2：亏损股（归母净利润 TTM ≤ 0）的 fund 分是否仍有 alpha？")
    print("─" * 106)

    grp = {"loss": {hp: [] for hp in HOPS}, "profit": {hp: [] for hp in HOPS}}
    cnt = {"loss": 0, "profit": 0}
    exc = {"loss": {hp: [] for hp in HOPS}, "profit": {hp: [] for hp in HOPS}}

    for s in snaps:
        d0 = s["date"]
        buckets = {"loss": [], "profit": []}
        for r in s["rows"]:
            c6 = (r.get("code") or "")[-6:]
            f = dim_val("score_fund", r, d0)
            if f is None:
                continue
            lg = (loss_by_date.get(d0) or {}).get(c6)
            if lg is None:
                continue
            key = "loss" if lg else "profit"
            fw = {hp: fwd_ret(c6, d0, hp) for hp in HOPS}
            buckets[key].append((f, fw))
            cnt[key] += 1
        for key, arr in buckets.items():
            for hp in HOPS:
                pr = [(x[0], x[1][hp]) for x in arr if x[1][hp] is not None]
                if len(pr) >= 25:
                    ic = fe.rank_ic([p[0] for p in pr], [p[1] for p in pr])
                    if ic is not None:
                        grp[key][hp].append(ic)

    print(f"  样本：亏损 {cnt['loss']} 股票日 / 盈利 {cnt['profit']} 股票日")
    print()
    print(f"  {'分组':<12} {'hop':>4} {'IC':>9} {'ICIR':>7} {'t0':>7} {'t3':>7} {'t6':>7} {'Bonf':>6} {'n':>4}")
    for key, label in (("profit", "盈利股"), ("loss", "亏损股")):
        for hp in HOPS:
            st = stat(grp[key][hp])
            if not st:
                print(f"  {label:<12} h{hp:<3} 样本不足 (n={len(grp[key][hp])})")
                continue
            t = st["t_by_lag"]
            print(f"  {label:<12} h{hp:<3} {st['ic']:>+9.4f} {st['icir'] or 0:>+7.3f} "
                  f"{t['0']:>+7.2f} {t['3']:>+7.2f} {t['6']:>+7.2f} "
                  f"{'✅' if st['pass_bonf'] else '⚠️':>6} {st['n']:>4}")

    print()
    print("  判定：若亏损股 fund IC 不显著（或为负）→ 该维对亏损股是噪音，")
    print("        「靠 fwd_pe 拿 4.60 分」属无依据加分，应统一不计分。")

    out = {"round": "round32",
           "q1": {"corr_tech_bias60": round(sum(corr_bt) / len(corr_bt), 4),
                  "corr_tech_rsi": round(sum(corr_rt) / len(corr_rt), 4),
                  "raw": {f"h{hp}": stat(raw_ics[hp]) for hp in HOPS},
                  "resid": {f"h{hp}": stat(resid_ics[hp]) for hp in HOPS}},
           "q2": {"n_days": cnt,
                  "profit": {f"h{hp}": stat(grp['profit'][hp]) for hp in HOPS},
                  "loss": {f"h{hp}": stat(grp['loss'][hp]) for hp in HOPS}}}
    with open(os.path.join(HERE, "audit_round32.json"), "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print()
    print("=" * 106)
    print("已写出 audit_round32.json")
    print("=" * 106)
    return 0


if __name__ == "__main__":
    sys.exit(main())
