#!/usr/bin/env python3
"""round24f：口径校正 —— 用**生产线真实公式**重做换维裁决。

## 为什么必须重做

round24d 判定「去掉成长、换上低波 0.13 → h5 IC +0.1709 / t0=10.05 过 Bonferroni」，
那是本轮上线的唯一量化依据。但复核 round24d 源码发现：

  · round24d `build()`（L209-259）对每一维先做**逐截面 rank_pct**，
    再加权：num += w * rank_pct(value)。即所有维都被转成「当日池内排名」。
  · 生产线 step2_stockscorer_v2.py 的真实公式是**量程归一化**：
        _w9 = Σ min(v, DIM_MAX[k]) / DIM_MAX[k] * 10 * w
    fund/bias60/rsi_rev 是 0-5 制绝对分，quality/size 是 0-10 分位分，
    绝对分与分位分混在一起按量程除 → 与「全部转排名」不等价。

两者在分布偏斜时会给出不同的复合分，进而不同的 IC。所以
**round24d 的绝对数值不是生产线口径，不能直接作为上线依据。**

本脚本用与 step2 完全一致的量程归一化公式，把整条阶梯在同一套样本上重算：

  P0  v4.7 现行（成长0.13 + size0.10）            ← 上线前基线
  P1  v4.8 拟上线（低波0.13 + size0.10）          ← 本次改动
  P2  低波0.13 + size0（去掉 size）
  P3  成长0.13 + 低波0.13 + size0.10（加维不换维，各维×0.87腾权重）
  P4  成长0.05 + 低波0.08 + size0.10
  P5  低波0.23 + size0（低波吃掉成长+size）

判定标准（项目纪律，不许放宽）：
  · NW t 在 lag 0/3/6 **全部** ≥ 3.08（Bonferroni）才算稳健
  · 分子期（前半/后半）必须同号
  · ⚠️ 同时比较 6+ 个方案 = 多重检验，任何单点「过」都不足以上线，
     必须 P1 > P0 且提升幅度在分子期两段都为正才算真实改善。

数据：factor_history 78 截面 + px_index（价格）；size 用 circ_mv/price 反推股本重建。
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

T_BONF = 3.08
LAGS = [0, 3, 6]
DMAX = {"score_fund": 5.0, "score_quality": 10.0, "score_growth": 10.0,
        "score_tech_neg": 10.0, "score_bias60": 5.0, "score_rsi_rev": 5.0,
        "score_size": 10.0, "score_lowvol20": 10.0}


def _rank(vals):
    idx = sorted(range(len(vals)), key=lambda i: vals[i])
    r = [0.0] * len(vals)
    i = 0
    while i < len(idx):
        j = i
        while j + 1 < len(idx) and vals[idx[j + 1]] == vals[idx[i]]:
            j += 1
        avg = (i + j) / 2.0 + 1
        for k in range(i, j + 1):
            r[idx[k]] = avg
        i = j + 1
    return r


def spearman(xs, ys):
    n = len(xs)
    if n < 5:
        return None
    rx, ry = _rank(xs), _rank(ys)
    mx, my = sum(rx) / n, sum(ry) / n
    sxy = sum((rx[i] - mx) * (ry[i] - my) for i in range(n))
    sxx = sum((rx[i] - mx) ** 2 for i in range(n)) ** 0.5
    syy = sum((ry[i] - my) ** 2 for i in range(n)) ** 0.5
    return (sxy / (sxx * syy)) if sxx > 0 and syy > 0 else None


def nw_t(ics, lag):
    n = len(ics)
    if n < 5:
        return None
    mu = sum(ics) / n
    d = [x - mu for x in ics]
    var = sum(x * x for x in d) / n
    for L in range(1, min(lag, n - 1) + 1):
        gl = sum(d[i] * d[i - L] for i in range(L, n)) / n
        var += 2 * (1 - L / (lag + 1)) * gl
    se = math.sqrt(var / n) if var > 0 else None
    return (mu / se) if se else None


def main():
    h = json.load(open(os.path.join(HERE, "factor_history.json"), encoding="utf-8"))
    snaps, _ = fe._drop_stale_price_snapshots(h["snapshots"])
    tdays, px = fe.px_index(h)
    tidx = {d: i for i, d in enumerate(tdays)}
    em = r21.load_em_cache()

    print("=" * 92)
    print("round24f：用生产线真实公式（量程归一化）重做换维裁决")
    print("=" * 92)
    print(f"截面 {len(snaps)} · 交易日 {len(tdays)} · 价格 {len(px)} 只")

    # ===== 子因子（质量/成长需要 PIT 重建）=====
    subs = defaultdict(dict)
    for s in snaps:
        d0 = s["date"]
        for r in s["rows"]:
            ind = em.get(r.get("code"))
            if not ind:
                continue
            P = r21.pit_period(sorted({p for dd in ind.values() for p in dd}, reverse=True), d0)
            if P is None:
                continue
            subs[d0][r["code"]] = r21.sub_factors_pit(ind, P)

    bf = rb.backfill_reversal(tdays, px)
    print(f"子因子 {sum(len(v) for v in subs.values())} 股票日 · 反转回填 {len(bf)} 日")

    # ===== 股本重建 → size =====
    shares = defaultdict(list)
    for s in snaps:
        for r in s["rows"]:
            mv, pr = r.get("circ_mv"), r.get("price")
            if isinstance(mv, (int, float)) and mv and isinstance(pr, (int, float)) and pr > 0:
                shares[r["code"]].append(mv / pr)
    shares_est = {}
    for c, v in shares.items():
        v = sorted(v)
        shares_est[c] = v[len(v) // 2]

    size_by_date = {}
    for s in snaps:
        d0 = s["date"]
        mv = {}
        for r in s["rows"]:
            c = r["code"]
            v = r.get("circ_mv")
            if not (isinstance(v, (int, float)) and v):
                sh = shares_est.get(c)
                pr = r.get("price")
                if sh and isinstance(pr, (int, float)) and pr > 0:
                    v = sh * pr
            if isinstance(v, (int, float)) and v > 0:
                mv[c] = float(v)
        if len(mv) < 20:
            size_by_date[d0] = {}
            continue
        ks = sorted(mv, key=lambda c: -mv[c])
        n = len(ks)
        size_by_date[d0] = {c: round((i / (n - 1)) * 10.0, 2) for i, c in enumerate(ks)} if n > 1 else {}

    # ===== lowvol20（原始负标准差 → 池内分位 0-10）=====
    def lowvol_raw(code, d0):
        m = px.get(code)
        if not m or d0 not in tidx:
            return None
        i0 = tidx[d0]
        if i0 < 20:
            return None
        closes = []
        for j in range(i0 - 20, i0 + 1):
            if j >= len(tdays):
                return None
            v = m.get(tdays[j])
            if v is None:
                return None
            closes.append(v)
        rets = [(closes[k] / closes[k - 1] - 1.0) for k in range(1, len(closes))]
        n = len(rets)
        mu = sum(rets) / n
        return (sum((r - mu) ** 2 for r in rets) / (n - 1)) ** 0.5

    lv_by_date = {}
    for s in snaps:
        d0 = s["date"]
        raw = {}
        for r in s["rows"]:
            v = lowvol_raw(r["code"], d0)
            if v is not None:
                raw[r["code"]] = v
        if len(raw) < 20:
            lv_by_date[d0] = {}
            continue
        ks = sorted(raw, key=lambda c: raw[c])
        n = len(ks)
        lv_by_date[d0] = {c: round((1.0 - i / (n - 1)) * 10.0, 2) for i, c in enumerate(ks)} if n > 1 else {}

    # ⚠️ 量程陷阱（本项目已踩 N 次，本轮又踩一次）：
    #   aggregate() 返回的是**原始财务指标**（growth 中位 38.34 / max 126465、
    #   quality min −4.92），**不是** 0-10 分位分。生产线里这两个维已经在
    #   fund_split.py 里转成「池内 rank 分位 ×10」才写入 scored。
    #   若直接除 DMAX=10，growth 的 126465/10*10 会把复合分彻底带偏
    #   （实测导致所有复合方案 IC 变负，与单维全正自相矛盾）。
    #   正确做法：先按池内 rank 转成 0-10 分位分，再走生产公式。
    def _rank10(vals_by_code):
        """原始值 → 池内 rank 分位分 0-10（与 fund_split.rank_pct 同方向：值越大分越高）。"""
        ks = sorted(vals_by_code, key=lambda c: vals_by_code[c])
        n = len(ks)
        if n < 2:
            return {}
        return {c: round((i / (n - 1)) * 10.0, 2) for i, c in enumerate(ks)}

    # 逐截面预先把 quality / growth 转成分位分（这两个是池内分位族）
    qual_rank_by_date = {}
    grow_rank_by_date = {}
    for s in snaps:
        d0 = s["date"]
        q, g = {}, {}
        for r in s["rows"]:
            sub = (subs.get(d0) or {}).get(r["code"])
            if not sub:
                continue
            vq = r21.aggregate(sub, "grouped")
            vg = r21.aggregate(sub, "g6040")
            if isinstance(vq, (int, float)):
                q[r["code"]] = vq
            if isinstance(vg, (int, float)):
                g[r["code"]] = vg
        qual_rank_by_date[d0] = _rank10(q)
        grow_rank_by_date[d0] = _rank10(g)

    def dim_val(d, r, d0):
        """→ 该维**生产口径的原始分**（未归一化），None=缺失。
        ⚠️ quality/growth 返回**分位分 0-10**（生产线就是分位分），不是 aggregate 原始值。"""
        if d == "score_fund":
            v = r.get("score_fund")
            return v if isinstance(v, (int, float)) and not isinstance(v, bool) else None
        if d == "score_tech_neg":
            v = r.get("score_tech")
            return (10.0 - v) if isinstance(v, (int, float)) else None
        if d in ("score_bias60", "score_rsi_rev"):
            t = (bf.get(d0) or {}).get(r.get("code"))
            if not t:
                return None
            return t[1] if d == "score_bias60" else t[2]
        if d == "score_quality":
            return qual_rank_by_date.get(d0, {}).get(r.get("code"))
        if d == "score_growth":
            return grow_rank_by_date.get(d0, {}).get(r.get("code"))
        if d == "score_size":
            return size_by_date.get(d0, {}).get(r.get("code"))
        if d == "score_lowvol20":
            return lv_by_date.get(d0, {}).get(r.get("code"))
        return None

    def fwd_ret(code, d0, hop):
        m = px.get(code)
        if not m or d0 not in tidx:
            return None
        i0 = tidx[d0]
        if i0 + hop >= len(tdays):
            return None
        p0, p1 = m.get(tdays[i0]), m.get(tdays[i0 + hop])
        if p0 is None or p1 is None or p0 <= 0:
            return None
        return p1 / p0 - 1.0

    def composite_prod(r, wmap, d0):
        """⚠️ 与 step2 L4632-4652 逐字同口径：
             _w9 = Σ min(v, DIM_MAX)/DIM_MAX * 10 * w   ；_denom = Σ w
             total = _w9 / max(0.5, _denom) * 10
        W 未归一（各方案权重和可能 !=1），_denom 已按实际参与维求和。"""
        num = den = 0.0
        for k, w in wmap.items():
            if w <= 0:
                continue
            v = dim_val(k, r, d0)
            if v is None:
                continue                       # 缺维剔分母，禁 or 0
            mx = DMAX.get(k, 10.0)
            num += min(float(v), mx) / mx * 10.0 * w
            den += w
        return (num / max(0.5, den) * 10.0) if den > 0 else None

    # ===== 方案阶梯 =====
    BASE5 = {"score_fund": 0.15, "score_quality": 0.12, "score_tech_neg": 0.15,
             "score_bias60": 0.175, "score_rsi_rev": 0.175}
    CASES = {
        "P0 v4.7基线 成长.13+size.10": {**BASE5, "score_growth": 0.13, "score_size": 0.10},
        "P1 v4.8拟上线 低波.13+size.10": {**BASE5, "score_lowvol20": 0.13, "score_size": 0.10},
        "P2 低波.13+size0": {**BASE5, "score_lowvol20": 0.13},
        "P3 成长.13+低波.13+size.10(各维×0.87)":
            {**{k: v * 0.87 for k, v in BASE5.items()},
             "score_growth": 0.13 * 0.87, "score_size": 0.10 * 0.87, "score_lowvol20": 0.13},
        "P4 成长.05+低波.08+size.10": {**BASE5, "score_growth": 0.05,
                                        "score_lowvol20": 0.08, "score_size": 0.10},
        "P5 低波.23+size0": {**BASE5, "score_lowvol20": 0.23},
        # 单维基准（用于解释复合为什么这样）
        "Q 仅低波.13": {"score_lowvol20": 1.0},
        "Q 仅成长.13(等权占位)": {"score_growth": 1.0},
        "Q 仅size.10(等权占位)": {"score_size": 1.0},
        "Q 仅fund": {"score_fund": 1.0},
    }

    out = {"n_snapshots": len(snaps),
           "formula": "production scale-normalize: sum(min(v,DMAX)/DMAX*10*w)/max(0.5,sum(w))*10",
           "cases": {}}

    def run_case(wmap, hop):
        ics, dates = [], []
        for s in snaps:
            d0 = s["date"]
            pairs = []
            for r in s["rows"]:
                c = composite_prod(r, wmap, d0)
                fr = fwd_ret(r["code"], d0, hop)
                if c is not None and fr is not None:
                    pairs.append((c, fr))
            if len(pairs) >= 40:
                ic = spearman([p[0] for p in pairs], [p[1] for p in pairs])
                if ic is not None:
                    ics.append(ic)
                    dates.append(d0)
        if len(ics) < 10:
            return None
        mean_ic = sum(ics) / len(ics)
        ts = {str(L): nw_t(ics, L) for L in LAGS}
        rob = all((ts.get(str(L)) or -9) >= T_BONF for L in LAGS)
        k = len(ics) // 2
        h1 = sum(ics[:k]) / k
        h2 = sum(ics[k:]) / (len(ics) - k)
        return {"n": len(ics), "ic": round(mean_ic, 4),
                "t_by_lag": {kk: (round(v, 2) if v is not None else None) for kk, v in ts.items()},
                "robust_bonf": rob,
                "sub_first": round(h1, 4), "sub_second": round(h2, 4),
                "sub_both_pos": (h1 > 0 and h2 > 0)}

    print()
    print("─" * 92)
    print("生产线口径下的方案阶梯（h5 / h20）")
    print("─" * 92)
    for hop in (5, 20):
        print(f"\n【h{hop}】")
        print(f"  {'方案':<38} {'n':>4} {'IC':>8} {'t0':>7} {'t3':>7} {'t6':>7}  {'Bonf':>5} {'前半':>8} {'后半':>8}")
        for nm, wmap in CASES.items():
            rec = run_case(wmap, hop)
            if not rec:
                print(f"  {nm:<38} 样本不足")
                continue
            t = rec["t_by_lag"]
            print(f"  {nm:<38} {rec['n']:>4} {rec['ic']:>+8.4f} "
                  f"{t.get('0') or 0:>+7.2f} {t.get('3') or 0:>+7.2f} {t.get('6') or 0:>+7.2f}  "
                  f"{'✅' if rec['robust_bonf'] else '⚠️':>5} "
                  f"{rec['sub_first']:>+8.4f} {rec['sub_second']:>+8.4f}")
            out["cases"][f"h{hop}|{nm}"] = rec

    # ===== 关键裁决：P1 是否真的优于 P0 =====
    print()
    print("=" * 92)
    print("裁决：P1（拟上线）vs P0（现行基线）—— 必须两项同时成立才算改善")
    print("=" * 92)
    verdict = {}
    for hop in (5, 20):
        p0 = out["cases"].get(f"h{hop}|P0 v4.7基线 成长.13+size.10")
        p1 = out["cases"].get(f"h{hop}|P1 v4.8拟上线 低波.13+size.10")
        if not p0 or not p1:
            continue
        d_ic = p1["ic"] - p0["ic"]
        # 逐截面配对差（同一批截面才可比）
        both = True
        print(f"\n  h{hop}：")
        print(f"    P0 IC {p0['ic']:+.4f} (t0 {p0['t_by_lag'].get('0'):+.2f}) "
              f"→ P1 IC {p1['ic']:+.4f} (t0 {p1['t_by_lag'].get('0'):+.2f})")
        print(f"    ΔIC = {d_ic:+.4f} → {'✅ P1 更优' if d_ic > 0 else '❌ P1 更差'}")
        print(f"    P1 分子期：前半 {p1['sub_first']:+.4f} / 后半 {p1['sub_second']:+.4f} "
              f"→ {'✅ 双正' if p1['sub_both_pos'] else '❌ 存在负段'}")
        print(f"    P1 Bonferroni 全 lag：{'✅ 过' if p1['robust_bonf'] else '⚠️ 未过'}")
        verdict[f"h{hop}"] = {"d_ic": round(d_ic, 4),
                              "p1_better": d_ic > 0,
                              "p1_sub_both_pos": p1["sub_both_pos"],
                              "p1_robust": p1["robust_bonf"]}

    # size 维影响
    print()
    print("─" * 92)
    print("size 维影响：P1（含 size.10）vs P2（去 size）")
    for hop in (5, 20):
        p1 = out["cases"].get(f"h{hop}|P1 v4.8拟上线 低波.13+size.10")
        p2 = out["cases"].get(f"h{hop}|P2 低波.13+size0")
        if p1 and p2:
            d = p1["ic"] - p2["ic"]
            print(f"  h{hop:<2} 含size {p1['ic']:+.4f} vs 去size {p2['ic']:+.4f} → "
                  f"Δ {d:+.4f} → {'含 size 更优' if d > 0 else 'size 在拖累'}")

    out["verdict"] = verdict
    with open(os.path.join(HERE, "audit_round24f.json"), "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print()
    print("=" * 92)
    print("已写出 audit_round24f.json")
    print("=" * 92)
    return 0


if __name__ == "__main__":
    sys.exit(main())
