#!/usr/bin/env python3
"""Round23：7 维够不够、该不该减维 —— 直接回答老大的问题。

动机（老大 2026-10-26）：「7维用来选股是否足够还是太多了，如果不够还需要哪几维数据；
如果多了需要去掉哪个」

本脚本只做一件事：在**真实前瞻收益**上，把「维数增减」的边际价值量化出来。
不做自测、不做页面、只出数字。

四条实证线：
  Q1 维度有效性阶梯：7 维单维 IC（h1/h5/h20 + NW t lag 0/3/6 + Bonferroni）→ 谁在真干活
  Q2 维数增减曲线：从 7 维里逐维剔除 / 按有效序逐维累加，看复合 IC 怎么走 → 拐点在哪
  Q3 v4.7 当前权重落点：round21 测过 fund≥.25（过）与 fund=0/质成.45（不过），
     v4.7 = fund.15+质.12+成.13 恰在夹缝，从未被测 → 补这一格
  Q4 增量维候选：现有候选池（低波/动量/规模/量价代理）的边际 IC 增益 → 还有没有可加的

纪律：NW t 全 lag 扫描，Bonferroni 3.08；缺维从分母剔除禁 or 0；
      权重视为 rank 空间线性组合（与 step2 的 min/max 归一化在单调变换下等价）。
"""
import glob
import json
import math
import os
import sys
from collections import defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import factor_eval as fe
import audit_round21 as r21

T_BONF = 3.08
LAGS = [0, 3, 6]


# ⚠️ 以下四个函数在 audit_round21.py 里是 main() 的**局部函数**，无法 import，
#   此处按 round21 L475-513 原样复刻（口径必须逐字一致，否则回填值不同、IC 不可比）。
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


def fmt(rec):
    if not rec:
        return "样本不足"
    t = "/".join(f"{rec['t_by_lag'].get(str(L), float('nan')):+.2f}" for L in LAGS)
    return f"n={rec['n']:<3} IC={rec['ic']:+.4f} t={t} {'✅' if rec.get('robust_bonf') else '⚠️'}"


def main():
    h = json.load(open(os.path.join(HERE, "factor_history.json"), encoding="utf-8"))
    snaps, _ = fe._drop_stale_price_snapshots(h["snapshots"])
    tdays, px = fe.px_index(h)
    print(f"截面数={len(snaps)}  价格日={len(tdays)} ({tdays[0]}~{tdays[-1]})")
    out = {"n_snapshots": len(snaps), "price_range": [tdays[0], tdays[-1]]}

    # 板块映射
    sec_map = {}
    for f in glob.glob(os.path.join(HERE, "top200_scored_*.json")):
        try:
            d = json.load(open(f, encoding="utf-8"))
        except Exception:
            continue
        for r in d.get("results") or []:
            if r.get("code") and r.get("sector"):
                sec_map[r["code"]] = r["sector"]

    # ── 反转块回填（round21 同口径：oversold60 / bias60 / rsi_rev）
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
    print(f"反转块回填: {len(backfill)} 个日期")

    # ── 质量/成长 PIT 回填（round21 同口径）
    em = r21.load_em_cache()
    print(f"EM 财务缓存: {len(em)} 只")
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
            rec = {"q_grouped": r21.aggregate(sub, "grouped"),
                   "g6040": r21.aggregate(sub, "g6040")}
            if rec["q_grouped"] is not None or rec["g6040"] is not None:
                qg[d0][code] = rec
    print(f"质/成回填: {len(qg)} 个截面, {sum(len(v) for v in qg.values())} 股票日")

    # ── 7 维取值函数（rank 空间用；返回原始量，后面统一 rank_pct）
    def dim_value(d, r, d0):
        if d == "score_fund":
            v = r.get("score_fund")
            return v if isinstance(v, (int, float)) else None
        if d == "score_bias60":
            b = backfill.get(d0, {}).get(r.get("code"))
            return b[1] if b else None
        if d == "score_rsi_rev":
            b = backfill.get(d0, {}).get(r.get("code"))
            return b[2] if b else None
        if d == "score_tech_neg":
            v = r.get("score_tech")
            return (-v) if isinstance(v, (int, float)) else None
        if d == "score_size":
            mv = r.get("circ_mv")
            return (-mv) if isinstance(mv, (int, float)) else None
        if d == "score_quality":
            rec = qg.get(d0, {}).get(r.get("code"))
            return rec.get("q_grouped") if rec else None
        if d == "score_growth":
            rec = qg.get(d0, {}).get(r.get("code"))
            return rec.get("g6040") if rec else None
        return None

    ALL7 = ["score_fund", "score_bias60", "score_rsi_rev", "score_tech_neg",
            "score_size", "score_quality", "score_growth"]
    W47 = {"score_fund": 0.15, "score_bias60": 0.175, "score_rsi_rev": 0.175,
           "score_tech_neg": 0.15, "score_size": 0.10, "score_quality": 0.12,
           "score_growth": 0.13}

    # ========================================================
    print()
    print("=" * 100)
    print("Q1. 7 维单维有效性（真实前瞻收益，NW t lag 0/3/6，Bonferroni 3.08）")
    print("=" * 100)
    out["q1_dim_ic"] = {}
    for d in ALL7:
        out["q1_dim_ic"][d] = {}
        for hop in (1, 5, 20):
            ser = r21.series_for_dim(snaps, tdays, px, lambda r, s, dd, _d=d: dim_value(_d, r, dd), hop)
            out["q1_dim_ic"][d][str(hop)] = r21.ic_battery(ser)
        print(f"  {d.replace('score_',''):10s} h1:{fmt(out['q1_dim_ic'][d].get('1'))}")
        print(f"  {'':10s} h5:{fmt(out['q1_dim_ic'][d].get('5'))}")
        print(f"  {'':10s} h20:{fmt(out['q1_dim_ic'][d].get('20'))}")

    # ========================================================
    print()
    print("=" * 100)
    print("Q2. 复合 IC 的维数增减曲线（rank 空间加权，缺维从分母剔除）")
    print("=" * 100)

    def build_composite(weights, hop, extra=None):
        """weights: {dim: w}；返回逐截面 (composite, ret) 序列。
        extra: {dname: fn(r, d0)} 额外维取值（用于 Q4 候选增量维，避免 monkey patch 闭包）。
        """
        tidx = {d: i for i, d in enumerate(tdays)}
        ser = []
        for s in snaps:
            d0 = s["date"]
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
                    if weights[d] <= 0:
                        continue
                    if extra and d in extra:
                        vals[d] = extra[d](r, d0)
                    else:
                        vals[d] = dim_value(d, r, d0)
                rows.append((vals, p1 / p0 - 1.0))
            if len(rows) < 40:
                continue
            # 逐维 rank
            dim_ranks = {}
            for d in weights:
                if weights[d] <= 0:
                    continue
                xs = [rv[0].get(d) for rv in rows]
                present = [i for i, x in enumerate(xs) if isinstance(x, (int, float))]
                if len(present) < 0.5 * len(rows):
                    dim_ranks[d] = None
                    continue
                rp = r21.rank_pct([xs[i] for i in present])
                dim_ranks[d] = {i: rp[k] for k, i in enumerate(present)}
            tv, rv = [], []
            for idx, (vals, ret) in enumerate(rows):
                num = den = 0.0
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

    # Q2a: v4.7 全 7 维（基线）
    out["q2"] = {}
    print("  ── Q2a 基线：v4.7 现有 7 维权重")
    base = {}
    for hop in (1, 5, 20):
        rec = r21.ic_battery(build_composite(W47, hop))
        base[str(hop)] = rec
        print(f"     h{hop:<2d}: {fmt(rec)}")
    out["q2"]["base_v47"] = base

    # Q2b: 逐维剔除（权重等比例放大到 1.0）
    print()
    print("  ── Q2b 逐维剔除（释放的权重按比例分给其余维，保持总和 1.0）")
    print(f"     {'剔除维':12s} {'h1 IC':>9s} {'h1 t(lag0)':>11s} {'h5 IC':>9s} {'h5 t(lag0)':>11s} {'h20 IC':>9s} {'h20 t(lag0)':>11s} {'h5 ΔIC':>9s}")
    out["q2"]["drop_one"] = {}
    base_h5 = base["5"]["ic"] if base.get("5") else None
    base_h1 = base["1"]["ic"] if base.get("1") else None
    base_h20 = base["20"]["ic"] if base.get("20") else None
    for d in ALL7:
        wm = {k: v for k, v in W47.items() if k != d}
        ssum = sum(wm.values())
        wm = {k: v / ssum for k, v in wm.items()}
        recs = {}
        for hop in (1, 5, 20):
            recs[str(hop)] = r21.ic_battery(build_composite(wm, hop))
        out["q2"]["drop_one"][d] = recs
        def g(h, f="ic"):
            x = recs.get(str(h))
            return x.get(f) if x else None
        def t0(h):
            x = recs.get(str(h))
            return x["t_by_lag"].get("0") if x else None
        dh5 = (g(5, "ic") - base_h5) if (g(5, "ic") is not None and base_h5 is not None) else None
        print(f"     {d.replace('score_',''):12s} {g(1):+9.4f} {t0(1):+11.2f} {g(5):+9.4f} {t0(5):+11.2f} "
              f"{g(20):+9.4f} {t0(20):+11.2f} {dh5:+9.4f}" if dh5 is not None else "     n/a")

    # Q2c: 只留 1→7 维的累加曲线（按 Q1 的 h5 IC 从高到低排序）
    print()
    print("  ── Q2c 累加曲线（按单维 h5 IC 从高到低，逐维加入，等权）")
    rank_order = []
    for d in ALL7:
        rec = out["q1_dim_ic"][d].get("5")
        rank_order.append((rec["ic"] if rec else -9, d))
    rank_order.sort(reverse=True)
    seq = [d for _, d in rank_order]
    print(f"     加入顺序: {' → '.join(x.replace('score_','') for x in seq)}")
    print(f"     {'维数':>4s} {'构成':42s} {'h1 IC':>9s} {'h5 IC':>9s} {'h20 IC':>9s} {'h5 t0':>7s}")
    out["q2"]["cumulative"] = {}
    for k in range(1, len(seq) + 1):
        sub = seq[:k]
        wm = {d: 1.0 / k for d in sub}
        recs = {}
        for hop in (1, 5, 20):
            recs[str(hop)] = r21.ic_battery(build_composite(wm, hop))
        out["q2"]["cumulative"][str(k)] = {"dims": sub, "ic": recs}
        def g(h):
            x = recs.get(str(h))
            return x.get("ic") if x else None
        def t0(h):
            x = recs.get(str(h))
            return x["t_by_lag"].get("0") if x else None
        nm = "+".join(x.replace('score_','') for x in sub)
        print(f"     {k:>4d} {nm[:42]:42s} {g(1):+9.4f} {g(5):+9.4f} {g(20):+9.4f} {t0(5):+7.2f}")

    # ========================================================
    print()
    print("=" * 100)
    print("Q3. v4.7 权重落点检验（round21/22 已测区间的夹缝格）")
    print("=" * 100)
    variants = [
        ("v4.7 现行 fund.15+质.12+成.13+技.15+反.35+规.10", W47),
        ("R1 质/成 回到 N5 量级 fund.30+质.05+成.05+技.15+反.35+规.10",
         {"score_fund": 0.30, "score_quality": 0.05, "score_growth": 0.05,
          "score_tech_neg": 0.15, "score_bias60": 0.175, "score_rsi_rev": 0.175,
          "score_size": 0.10}),
        ("R2 fund 提到 .25 质.10 成.10",
         {"score_fund": 0.25, "score_quality": 0.10, "score_growth": 0.10,
          "score_tech_neg": 0.15, "score_bias60": 0.175, "score_rsi_rev": 0.175,
          "score_size": 0.10}),
        ("R3 去 tech_neg，其权重给反转块",
         {"score_fund": 0.15, "score_quality": 0.12, "score_growth": 0.13,
          "score_bias60": 0.25, "score_rsi_rev": 0.25, "score_size": 0.10}),
        ("R4 反转块合并为单维（.35→bias60 单 .35）",
         {"score_fund": 0.15, "score_quality": 0.12, "score_growth": 0.13,
          "score_tech_neg": 0.15, "score_bias60": 0.35, "score_size": 0.10}),
        ("R5 去 size，权重给 fund",
         {"score_fund": 0.25, "score_quality": 0.12, "score_growth": 0.13,
          "score_tech_neg": 0.15, "score_bias60": 0.175, "score_rsi_rev": 0.175}),
    ]
    out["q3_variants"] = {}
    print(f"     {'方案':56s} {'h1 IC':>9s} {'h5 IC':>9s} {'h20 IC':>9s} {'h5 t0':>7s} {'h5 rob':>7s}")
    for name, w in variants:
        recs = {}
        for hop in (1, 5, 20):
            recs[str(hop)] = r21.ic_battery(build_composite(w, hop))
        out["q3_variants"][name] = recs
        def g(hh):
            x = recs.get(str(hh))
            return x.get("ic") if x else None
        def t0(hh):
            x = recs.get(str(hh))
            return x["t_by_lag"].get("0") if x else None
        def rb(hh):
            x = recs.get(str(hh))
            return "✅" if (x and x.get("robust_bonf")) else "⚠️"
        print(f"     {name:56s} {g(1):+9.4f} {g(5):+9.4f} {g(20):+9.4f} {t0(5):+7.2f} {rb(5):>7s}")

    # ========================================================
    print()
    print("=" * 100)
    print("Q4. 增量维候选：加进 v4.7 后复合 IC 是否改善")
    print("=" * 100)
    # 候选维（均可在历史截面重建）
    def cand_value(d, r, d0, code, i0):
        """候选维：从价格历史现算"""
        closes = px.get(code)
        if not isinstance(closes, dict):
            return None
        if i0 < 20:
            return None
        cl = [closes.get(dd) for dd in tdays[:i0 + 1]]
        if len(cl) < 21 or any(x is None or x <= 0 for x in cl[-21:]):
            return None
        if d == "lowvol20":
            rets = [cl[j] / cl[j - 1] - 1.0 for j in range(len(cl) - 20, len(cl))]
            m = sum(rets) / len(rets)
            sd = math.sqrt(sum((x - m) ** 2 for x in rets) / (len(rets) - 1))
            return -sd                      # 低波 = 负波动
        if d == "mom1w":
            return -(cl[-1] / cl[-6] - 1.0)  # 反向：跌得多的好
        if d == "mom1m":
            return -(cl[-1] / cl[-21] - 1.0)
        if d == "max20":
            return max(cl[-20:]) / cl[-1]    # 距 20 日高点（越小越好→取反已含）
        return None

    cands = ["lowvol20", "mom1w", "mom1m", "max20"]
    out["q4_candidates"] = {}
    print(f"     {'候选维':12s} {'单维h5 IC':>10s} {'加入后h5 IC':>12s} {'ΔIC':>8s} {'vs现有最高|r|':>13s}")
    # 先算候选单维 IC
    for cd in cands:
        def mkfn(_cd):
            def fn(r, s, d0):
                try:
                    i0 = tdays.index(d0)
                except ValueError:
                    return None
                return cand_value(_cd, r, d0, r.get("code"), i0)
            return fn
        ser_c = r21.series_for_dim(snaps, tdays, px, mkfn(cd), 5)
        rec_c = r21.ic_battery(ser_c)
        # 加入后：候选给 0.15 权重，其余按比例缩到 0.85
        wm = {k: v * 0.85 for k, v in W47.items()}
        wm["__cand__"] = 0.15
        _cd_fixed = cd
        def _cand_fn(r, d0, _cd=_cd_fixed):
            try:
                i0 = tdays.index(d0)
            except ValueError:
                return None
            return cand_value(_cd, r, d0, r.get("code"), i0)
        rec_add = r21.ic_battery(build_composite(wm, 5, extra={"__cand__": _cand_fn}))
        # 与现有维的相关（截面 rank 相关，取最大 |r|）
        maxr = None
        for d in ALL7:
            def fn2(r, s, d0, _d=d):
                return dim_value(_d, r, d0)
            s1 = r21.series_for_dim(snaps, tdays, px, fn2, 5)
            # 按日期配对算 rank 相关
            m1 = {x["date"]: x for x in s1}
            m2 = {x["date"]: x for x in ser_c}
            rs = []
            for dd in m1:
                if dd not in m2:
                    continue
                a, b = m1[dd], m2[dd]
                if len(a["fv"]) != len(b["fv"]) or len(a["fv"]) < 20:
                    continue
                ra = r21.rank_pct(a["fv"]); rb2 = r21.rank_pct(b["fv"])
                ma = sum(ra)/len(ra); mb = sum(rb2)/len(rb2)
                num = sum((ra[i]-ma)*(rb2[i]-mb) for i in range(len(ra)))
                da = math.sqrt(sum((x-ma)**2 for x in ra)); db = math.sqrt(sum((x-mb)**2 for x in rb2))
                if da > 0 and db > 0:
                    rs.append(num/(da*db))
            if rs:
                rv = sum(rs)/len(rs)
                if maxr is None or abs(rv) > abs(maxr):
                    maxr = rv
        dic = (rec_add["ic"] - base_h5) if (rec_add and base_h5 is not None) else None
        out["q4_candidates"][cd] = {"ic_single": rec_c, "ic_added": rec_add,
                                    "delta_h5": round(dic, 4) if dic is not None else None,
                                    "max_abs_corr": round(maxr, 3) if maxr is not None else None}
        print(f"     {cd:12s} {rec_c['ic'] if rec_c else float('nan'):+10.4f} "
              f"{rec_add['ic'] if rec_add else float('nan'):+12.4f} "
              f"{dic if dic is not None else float('nan'):+8.4f} {maxr if maxr is not None else float('nan'):+13.2f}")

    json.dump(out, open(os.path.join(HERE, "audit_round23.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    print()
    print("已写出 audit_round23.json")


if __name__ == "__main__":
    main()
