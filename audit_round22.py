#!/usr/bin/env python3
"""Round22：对 round21 落地方案的两个未闭合缺口补测（落地前最后一道闸）。

背景（老大 2026-10-25 要求「评估和修改该方案，更新到本地 index，先在本地验证」）。
round21 给出 N4/N5（保 fund + 小权重质量/成长）是唯一全 lag 过关的拆分形态，但存在两个
未闭合缺口，直接落地等于把「全样本扫描」当结论用：

  缺口 1 —— 选择性偏差：N4/N5 的权重（fund .25/质 .08/成 .05）与 E 方案一样是全样本
           扫描产物，没做样本内外分层。本脚本按六条验证闸第 1 条补：
           构造期 ≤2026-09-30 / 样本外 ≥2026-10-08（避开国庆非交易日）。
           ⚠️ 硬约束：价格库止于 2026-10-23，样本外窗口仅 ~12 个交易日 →
              h20 样本外不可验（入场日 + 20 日超库），只有 h1/h5 可验 → h20 结论必须
              依赖构造期 + 独立来源互证，禁宣称「样本外稳健」。
  缺口 2 —— 成长反号未测：成长 h20 IC −0.0429 仅 L0 过（t −2.89），round21 以
           「纪律不可主张」结案。但 theme_neg 的先例说明反号维度可能有价值，
           本脚本实测 q_neg / g_neg 两个反号变体（族得分取负），而不是回避。

另补：fund 权重敏感性（0.25/0.30/0.35），确认方案对 fund 单点权重不过敏。

判据（缺一不可，全部满足才允许落地）：
  ① 构造期 h1/h5/h20 全 lag 过 Bonferroni 3.08；
  ② 样本外 h1/h5 全 lag 过 3.08（n 不足则如实标「不可验」）；
  ③ 反号变体若无稳健证据，明确排除并留痕（避免后续重复踩坑）。

纪律：NW t 全 lag（L0/L3/L6）；缺维分母剔除禁 or 0；族内权重本质是先验；
EM 缓存 vintage 偏差沿用 round21 披露。
"""
import glob
import json
import os
import sys
from collections import defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

# 复用 round21 的全部基础设施（顶层仅常量+函数，import 无副作用）
import audit_round21 as r21

SPLIT_IS = "2026-09-30"   # 构造期截止
SPLIT_OOS = "2026-10-08"  # 样本外起始（10-01~10-07 国庆非交易日）


def load_all():
    h = json.load(open(os.path.join(HERE, "factor_history.json"), encoding="utf-8"))
    snaps, _ = r21.fe._drop_stale_price_snapshots(h["snapshots"])
    tdays, px = r21.fe.px_index(h)
    em = r21.load_em_cache()
    return snaps, tdays, px, em


def build_qg(snaps, em):
    """PIT 回填质量/成长族得分（round21 Part B 同口径）。"""
    qg = defaultdict(dict)
    for s in snaps:
        d0 = s["date"]
        for r in s["rows"]:
            code = r.get("code")
            if code not in em:
                continue
            ind = em[code]
            periods_desc = sorted({p for dd in ind.values() for p in dd}, reverse=True)
            P = r21.pit_period(periods_desc, d0)
            if P is None:
                continue
            sub = r21.sub_factors_pit(ind, P)
            rec = {"sub": sub,
                   "q_equal": r21.aggregate(sub, "equal_q"),
                   "q_grouped": r21.aggregate(sub, "grouped"),
                   "g_equal": r21.aggregate(sub, "equal_g"),
                   "g6040": r21.aggregate(sub, "g6040")}
            if rec["q_grouped"] is not None or rec["g6040"] is not None:
                qg[d0][code] = rec
    return qg


def build_backfill(tdays, px):
    """反转块回填（round20 口径：oversold60/bias60/rsi_rev 三兄弟）。"""
    def s_oversold60(mom60):
        if mom60 is None:
            return 2.5
        if mom60 <= -30:
            return 5.0
        if mom60 <= -20:
            return 4.0
        if mom60 <= -10:
            return 3.0
        if mom60 <= 0:
            return 2.0
        return 1.0

    def s_bias60(price, ma60):
        if not ma60 or ma60 <= 0 or not price:
            return 2.5
        bias = (price - ma60) / ma60 * 100.0
        if bias <= -15:
            return 5.0
        if bias <= -10:
            return 4.0
        if bias <= -5:
            return 3.0
        if bias <= 0:
            return 2.0
        return 1.0

    def s_rsi_rev(rsi14):
        if rsi14 is None:
            return 2.5
        if rsi14 < 30:
            return 5.0
        if rsi14 < 40:
            return 4.0
        if rsi14 < 50:
            return 3.0
        if rsi14 < 60:
            return 2.0
        return 1.0

    def wilder_rsi(closes, period=14):
        if len(closes) < period + 1:
            return None
        deltas = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
        gains = [max(d, 0.0) for d in deltas]
        losses = [max(-d, 0.0) for d in deltas]
        ag = sum(gains[:period]) / period
        al = sum(losses[:period]) / period
        for i in range(period, len(deltas)):
            ag = (ag * (period - 1) + gains[i]) / period
            al = (al * (period - 1) + losses[i]) / period
        if al == 0:
            return 100.0 if ag > 0 else 50.0
        rs = ag / al
        return 100 - 100 / (1 + rs)

    backfill = defaultdict(dict)
    for code, closes_d in px.items():
        if not isinstance(closes_d, dict):
            continue
        cl = [closes_d.get(d) for d in tdays]
        for i, d in enumerate(tdays):
            if i < 60 or not cl[i] or cl[i] <= 0:
                continue
            p60 = cl[i - 60]
            if not p60 or p60 <= 0:
                continue
            mom60 = (cl[i] / p60 - 1.0) * 100.0
            win = cl[i - 59:i + 1]
            if any(w is None or w <= 0 for w in win):
                continue
            ma60 = sum(win) / 60.0
            hist = [cl[j] for j in range(0, i + 1) if cl[j] and cl[j] > 0]
            r14 = wilder_rsi(hist, 14) if len(hist) >= 15 else None
            backfill[d][code] = (s_oversold60(mom60), s_bias60(cl[i], ma60), s_rsi_rev(r14))
    return backfill


def build_total(weights, hop, snaps, tdays, px, qg, backfill, date_lo=None, date_hi=None):
    """组合总分截面序列。date_lo/date_hi：只保留入场日在 [lo,hi] 的截面（分层用）。

    与 round21 build_total 的差异：支持反号维度（*_neg）+ 日期分层过滤。
    权重口径：逐截面内 rank_pct 后按权重加权，缺维从分母剔除（v4.3 铁律）。
    """
    tidx = {d: i for i, d in enumerate(tdays)}
    ser = []
    for s in snaps:
        d0 = s["date"]
        if date_lo and d0 < date_lo:
            continue
        if date_hi and d0 > date_hi:
            continue
        if d0 not in tidx:
            continue
        i0 = tidx[d0]
        if i0 + hop >= len(tdays):
            continue
        d1 = tdays[i0 + hop]
        rows = []
        for r in s["rows"]:
            m = px.get(r.get("code"))
            if not m:
                continue
            p0, p1 = m.get(d0), m.get(d1)
            if not p0 or not p1 or p0 <= 0:
                continue
            vals = {}
            for d in weights:
                if d == "__rev2__":
                    b = backfill.get(d0, {}).get(r.get("code"))
                    vals[d] = (b[1] + b[2]) / 2.0 if b else None
                elif d == "__rev4__":
                    vs = [r.get(k) for k in ("score_oversold60", "score_bias60",
                                             "score_rsi_rev", "score_vol_pulse")]
                    vals[d] = sum(vs) / 4.0 if all(isinstance(x, (int, float)) for x in vs) else None
                elif d == "score_quality":
                    rec = qg.get(d0, {}).get(r.get("code"))
                    vals[d] = rec.get("q_grouped") if rec else None
                elif d == "score_growth":
                    rec = qg.get(d0, {}).get(r.get("code"))
                    vals[d] = rec.get("g6040") if rec else None
                elif d == "score_growth_neg":
                    rec = qg.get(d0, {}).get(r.get("code"))
                    v = rec.get("g6040") if rec else None
                    vals[d] = (-v) if isinstance(v, (int, float)) else None
                elif d == "score_quality_neg":
                    rec = qg.get(d0, {}).get(r.get("code"))
                    v = rec.get("q_grouped") if rec else None
                    vals[d] = (-v) if isinstance(v, (int, float)) else None
                elif d == "score_tech_neg":
                    v = r.get("score_tech")
                    vals[d] = (-v) if isinstance(v, (int, float)) else None
                elif d == "score_size":
                    mv = r.get("circ_mv")
                    vals[d] = -mv if isinstance(mv, (int, float)) else None
                elif d == "score_theme_neg":
                    v = r.get("score_theme")
                    vals[d] = (10.0 - v) if isinstance(v, (int, float)) else None
                else:
                    vals[d] = r.get(d)
            if all(v is None or isinstance(v, (int, float)) for v in vals.values()):
                rows.append((vals, p1 / p0 - 1.0))
        if len(rows) < 40:
            continue
        dim_ranks = {}
        for d in weights:
            xs = [rv[0].get(d) for rv in rows]
            present = [i for i, x in enumerate(xs) if isinstance(x, (int, float))]
            if len(present) < 0.5 * len(rows):
                dim_ranks[d] = None
                continue
            rp = r21.rank_pct([xs[i] for i in present])
            dim_ranks[d] = {i: rp[k] for k, i in enumerate(present)}
        tv, rv = [], []
        for idx, (vals, ret) in enumerate(rows):
            num, den = 0.0, 0.0
            for d, w in weights.items():
                if w <= 0:
                    continue
                m = dim_ranks.get(d)
                if m is None or idx not in m:
                    continue
                num += w * m[idx]
                den += w
            if den >= 0.5:
                tv.append(num / den)
                rv.append(ret)
        if len(tv) >= 40:
            ser.append({"date": d0, "fv": tv, "rv": rv})
    return ser


VARIANTS = [
    ("A 现行12维(9生效)",
     {"score_fund": 0.32, "score_news": 0.16, "score_theme_neg": 0.12,
      "score_size": 0.12, "score_sector": 0.10, "__rev4__": 0.18}),
    ("E round20-rev2精化",
     {"score_fund": 0.35, "__rev2__": 0.35, "score_size": 0.15, "score_tech_neg": 0.15}),
    ("N4 保fund+小权重质/成",
     {"score_fund": 0.25, "score_quality": 0.08, "score_growth": 0.05,
      "score_tech_neg": 0.15, "__rev2__": 0.35, "score_size": 0.12}),
    ("N5 保fund.30+小权重质/成",
     {"score_fund": 0.30, "score_quality": 0.05, "score_growth": 0.05,
      "score_tech_neg": 0.15, "__rev2__": 0.35, "score_size": 0.10}),
    ("N6 N4-成长反号",
     {"score_fund": 0.25, "score_quality": 0.08, "score_growth_neg": 0.05,
      "score_tech_neg": 0.15, "__rev2__": 0.35, "score_size": 0.12}),
]

FUND_SENS = [
    ("F25 fund.25", {"score_fund": 0.25, "__rev2__": 0.42, "score_size": 0.18, "score_tech_neg": 0.15}),
    ("F30 fund.30", {"score_fund": 0.30, "__rev2__": 0.40, "score_size": 0.15, "score_tech_neg": 0.15}),
    ("F35 fund.35", {"score_fund": 0.35, "__rev2__": 0.35, "score_size": 0.15, "score_tech_neg": 0.15}),
    ("F40 fund.40", {"score_fund": 0.40, "__rev2__": 0.30, "score_size": 0.15, "score_tech_neg": 0.15}),
]


def main():
    snaps, tdays, px, em = load_all()
    print(f"截面数={len(snaps)} ({snaps[0]['date']}~{snaps[-1]['date']})  价格日={len(tdays)} ({tdays[0]}~{tdays[-1]})")
    n_is = sum(1 for s in snaps if s["date"] <= SPLIT_IS)
    n_oos = sum(1 for s in snaps if s["date"] >= SPLIT_OOS)
    print(f"分层：构造期 ≤{SPLIT_IS} = {n_is} 截面；样本外 ≥{SPLIT_OOS} = {n_oos} 截面")
    out = {"n_snapshots": len(snaps), "n_is": n_is, "n_oos": n_oos,
           "split_is": SPLIT_IS, "split_oos": SPLIT_OOS}

    qg = build_qg(snaps, em)
    backfill = build_backfill(tdays, px)
    n_rows = sum(len(v) for v in qg.values())
    print(f"PIT 回填：{len(qg)}/{len(snaps)} 截面，{n_rows} 股票日；反转块回填 {len(backfill)} 个截面日")

    ctx = dict(snaps=snaps, tdays=tdays, px=px, qg=qg, backfill=backfill)

    # ============================================================
    print()
    print("=" * 100)
    print("A. 缺口1：样本内外分层（构造期 ≤09-30 / 样本外 ≥10-08）")
    print("=" * 100)
    print("  ⚠️ 硬约束：价格库止于 %s，样本外 h20 入场日+20 日超库 → h20 样本外预期 n≈0" % tdays[-1])
    out["is_oos"] = {}
    for name, w in VARIANTS:
        out["is_oos"][name] = {}
        for tag, lo, hi in (("IS", None, SPLIT_IS), ("OOS", SPLIT_OOS, None)):
            for hop in (1, 5, 20):
                ser = build_total(w, hop, date_lo=lo, date_hi=hi, **ctx)
                rec = r21.ic_battery(ser) or {"n": 0, "ic": 0.0, "t_by_lag": {},
                                              "robust_bonf": False}
                out["is_oos"][name][f"{tag}_h{hop}"] = rec
                n = rec.get("n", 0)
                ts = "/".join(f"{rec['t_by_lag'].get(str(L), float('nan')):+.2f}"
                              for L in r21.LAGS)
                mark = "✅" if rec.get("robust_bonf") else ("—" if n < 5 else "⚠️")
                print(f"  {name:<24} {tag} h{hop:<2} n={n:<3} IC={rec.get('ic', 0):+.4f} "
                      f"t={ts} {mark}")

    # ============================================================
    print()
    print("=" * 100)
    print("B. 缺口2：反号变体（族得分取负）单独 IC battery（全样本，构造期）")
    print("=" * 100)
    out["neg_variants"] = {}

    def fam_neg(key):
        def fn(r, s, d0):
            rec = qg.get(d0, {}).get(r.get("code"))
            if not rec:
                return None
            v = rec.get(key)
            return (-v) if isinstance(v, (int, float)) else None
        return fn

    for label, key in (("quality_neg(=-q_grouped)", "q_grouped"),
                       ("growth_neg(=-g6040)", "g6040")):
        out["neg_variants"][label] = {}
        for hop in (1, 5, 20):
            ser = r21.series_for_dim(snaps, tdays, px, fam_neg(key), hop)
            rec = r21.ic_battery(ser)
            out["neg_variants"][label][str(hop)] = rec
            print(f"  {label:<26} h{hop:<2} {r21.fmt(rec)}")

    # ============================================================
    print()
    print("=" * 100)
    print("C. fund 权重敏感性（其余维度按 E 比例归一，全样本）")
    print("=" * 100)
    out["fund_sens"] = {}
    for name, w in FUND_SENS:
        out["fund_sens"][name] = {}
        for hop in (1, 5, 20):
            ser = build_total(w, hop, **ctx)
            rec = r21.ic_battery(ser)
            out["fund_sens"][name][str(hop)] = rec
            print(f"  {name:<12} h{hop:<2} {r21.fmt(rec)}")

    # ============================================================
    print()
    print("=" * 100)
    print("D. 落地判据汇总")
    print("=" * 100)
    verdict = {}
    for name, _ in VARIANTS:
        d = out["is_oos"][name]
        is_ok = all((d.get(f"IS_h{h}") or {}).get("robust_bonf") for h in (1, 5, 20))
        oos_h1h5 = [d.get(f"OOS_h{h}") or {} for h in (1, 5)]
        oos_ok = all(r.get("robust_bonf") for r in oos_h1h5 if r.get("n", 0) >= 5)
        oos_n = {h: (d.get(f"OOS_h{h}") or {}).get("n", 0) for h in (1, 5, 20)}
        verdict[name] = {"is_all_lag_pass": bool(is_ok), "oos_h1h5_pass": bool(oos_ok),
                         "oos_n": oos_n,
                         "ic": {f"{t}_h{h}": (d.get(f"{t}_h{h}") or {}).get("ic")
                                for t in ("IS", "OOS") for h in (1, 5, 20)}}
        print(f"  {name:<24} 构造期全lag过={is_ok}  样本外h1/h5过={oos_ok}  样本外n={oos_n}")
    out["verdict"] = verdict
    neg_ok = {k: all((v.get(str(h)) or {}).get("robust_bonf") for h in (1, 5, 20))
              for k, v in out["neg_variants"].items()}
    out["neg_robust"] = neg_ok
    print(f"  反号变体全lag过: {neg_ok}")

    with open(os.path.join(HERE, "audit_round22.json"), "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print("\n  → audit_round22.json")


if __name__ == "__main__":
    main()
