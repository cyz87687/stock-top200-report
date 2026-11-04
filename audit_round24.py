#!/usr/bin/env python3
"""Round24：成长因子前瞻化重构 —— 老大的判断对不对，改完 IC 会不会转正。

动机（老大 2026-10-26）：
  「1、正常成长因子不应该负向，检查成长因子的逻辑，是不是应该换成更能代表未来的，
    比如业绩一阶导和二阶导、pefwd来决定成长因子，而不是用历史的roe增长率」

前置诊断（round23 已出）：
  · score_growth 全池 h5 IC = −0.0218（t0=−1.73）、h20 IC = −0.0429（t0=−2.89）
  · corr(score_growth, growth 历史增速) = +0.3045   ← 它确实在测历史增速
  · corr(score_growth, fwd_pe)        = −0.1287   ← 与前瞻估值几乎无关
  · corr(score_growth, score_fund)    = +0.6707   ← 与估值锚高度同源
  ⇒ 现有成长维是「估值锚的衰减复制品」，负 IC 的根因是它不含任何前瞻信息。

⚠️ 前置结论修正（本轮推翻前一轮判断）：
  此前认为「fwd_pe/pe_ladder 不在 factor_history.json ⇒ 前瞻成长无法回测」。
  实测 git 历史 **86 个正式 scored 文件全部带 fwd_pe，83 个截面带完整三年 PE 阶梯**
  （16,149 股票日三年齐全 / 19,338）→ 前瞻口径**可以回测**，本脚本就是那条路。

四条实证线：
  Q1 现有成长维的病灶定位：子因子逐项 IC（历史增速 5 项 vs 前瞻 3 项），
     并检查「成长维 vs 估值锚」的相关结构是否真的在重复计数。
  Q2 前瞻成长候选构造 + 单维 IC：
     g1   = PE26/PE27 − 1        预测净利一阶导（未来一年的预期增速）
     g2   = PE27/PE28 − 1        二阶导前段
     acc  = g2 − g1             预测增速的加速度（二阶导）
     fwd  = fwd_pe 取负          前瞻估值水平（越便宜越高分）
     以及它们的组合与「g1+acc 加权」。
  Q3 与现有维的冗余度 + 增量 IC：前瞻成长 vs fund/quality/bias60/rsi_rev 的相关，
     以及「把 score_growth 换成前瞻口径」后 7 维复合 IC 的变化。
  Q4 生产口径复刻：在**真实 total 公式**上验证换维前后（与 round23 q9 同一套复刻代码）。

纪律：
  · NW t 全 lag 扫描（0/3/6），Bonferroni 3.08；缺维从分母剔除禁 `or 0`
  · pe_ladder 的年份键是**字符串**（'2026'），必须 int() 化后才可取
  · 前瞻 PE 阶梯是「当日快照」，含分析师预测随时被上修/下修 → 分层披露
  · 回测用真实前瞻收益（factor_history 的 px），非当日截面自测
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
FWD_HIST = os.path.join(HERE, "data", "fwd_hist")


def fmt(rec):
    if not rec:
        return "样本不足"
    t = "/".join(f"{rec['t_by_lag'].get(str(L), float('nan')):+.2f}" for L in LAGS)
    return (f"n={rec['n']:<3} IC={rec['ic']:+.4f} t={t} "
            f"{'✅过Bonf' if rec.get('robust_bonf') else '⚠️未过'}")


def ladder_ints(L):
    """pe_ladder 的键是字符串 '2026'，统一转 int 并过滤无效值。"""
    if not isinstance(L, dict):
        return {}
    out = {}
    for k, v in L.items():
        try:
            y = int(k)
        except (TypeError, ValueError):
            continue
        if isinstance(v, (int, float)) and v > 0:
            out[y] = float(v)
    return out


def fwd_growth(L):
    """由 PE 阶梯反推预测净利增速。

    PE = 市值 / 预测净利 ⇒ 净利 ∝ 1/PE
    g1  = 净利27/净利26 − 1 = PE26/PE27 − 1   （未来一年的预期增速，一阶导）
    g2  = PE27/PE28 − 1
    acc = g2 − g1                              （增速的变化，二阶导）
    """
    d = ladder_ints(L)
    g1 = g2 = None
    if 2026 in d and 2027 in d:
        g1 = (d[2026] / d[2027] - 1.0) * 100.0
    if 2027 in d and 2028 in d:
        g2 = (d[2027] / d[2028] - 1.0) * 100.0
    acc = (g2 - g1) if (g1 is not None and g2 is not None) else None
    return g1, g2, acc


def main():
    h = json.load(open(os.path.join(HERE, "factor_history.json"), encoding="utf-8"))
    snaps, _ = fe._drop_stale_price_snapshots(h["snapshots"])
    tdays, px = fe.px_index(h)
    print(f"factor_history 截面={len(snaps)}  价格日={len(tdays)} ({tdays[0]}~{tdays[-1]})")

    # ── 载入从 git 恢复的历史前瞻数据（date -> code6 -> row）
    fwd = {}
    for f in sorted(glob.glob(os.path.join(FWD_HIST, "*.json"))):
        d = json.load(open(f, encoding="utf-8"))
        m = {}
        for r in d.get("rows") or []:
            c = (r.get("code") or "")
            c6 = c.replace("sh", "").replace("sz", "").replace("bj", "")
            if c6:
                m[c6] = r
        fwd[d["date"]] = m
    n_ladder = sum(1 for m in fwd.values() for r in m.values() if r.get("pe_ladder"))
    print(f"前瞻数据: {len(fwd)} 个截面, {sum(len(m) for m in fwd.values())} 股票日, "
          f"其中 pe_ladder 有值 {n_ladder}")

    # ── 基本面/质量成长回填（round21 同口径，用于对比「历史增速」 vs 「前瞻」）
    em = r21.load_em_cache()
    qg = defaultdict(dict)
    for s in snaps:
        d0 = s["date"]
        for r in s["rows"]:
            ind = em.get(r.get("code"))
            if not ind:
                continue
            P = r21.pit_period(sorted({p for dd in ind.values() for p in dd}, reverse=True), d0)
            if P is None:
                continue
            sub = r21.sub_factors_pit(ind, P)
            qg[d0][r["code"]] = sub
    print(f"子因子回填: {len(qg)} 截面, {sum(len(v) for v in qg.values())} 股票日")

    out = {"n_snapshots": len(snaps), "fwd_snapshots": len(fwd),
           "price_range": [tdays[0], tdays[-1]]}

    # ══════════════════════════════════════════════════════════════════
    print()
    print("=" * 104)
    print("Q1. 现有成长维的病灶定位：子因子逐项 IC（真实前瞻收益）")
    print("=" * 104)

    # 每个子因子单独做 dim_fn
    G_SUBS = ["npg", "cagr3", "revg", "qoq", "accel"]
    Q_SUBS = ["roe", "roic", "roa", "ocf_ni", "sales_cash", "accruals", "ocfg",
              "profit_vol", "gm_vol", "debt", "gw"]

    def sub_fn(name):
        def f(r, s, d0):
            rec = qg.get(d0, {}).get(r.get("code"))
            if not rec:
                return None
            v = rec.get(name)
            return v if isinstance(v, (int, float)) else None
        return f

    out["q1_growth_subs"] = {}
    print("── 成长族 5 项（全部是**历史已实现**增速）")
    for name in G_SUBS:
        recs = {}
        for hop in (1, 5, 20):
            recs[str(hop)] = r21.ic_battery(r21.series_for_dim(snaps, tdays, px, sub_fn(name), hop))
        out["q1_growth_subs"][name] = recs
        print(f"  {name:8s}  h1:{fmt(recs.get('1'))}")
        print(f"  {'':8s}  h5:{fmt(recs.get('5'))}")
        print(f"  {'':8s}  h20:{fmt(recs.get('20'))}")

    out["q1_quality_subs"] = {}
    print("\n── 质量族 11 项（对照：这些本就不该有强 IC，属防守因子）")
    for name in Q_SUBS:
        recs = {str(hop): r21.ic_battery(r21.series_for_dim(snaps, tdays, px, sub_fn(name), hop))
                for hop in (1, 5, 20)}
        out["q1_quality_subs"][name] = recs
        h5 = recs.get("5")
        print(f"  {name:12s} h5:{fmt(h5)}")

    # ══════════════════════════════════════════════════════════════════
    print()
    print("=" * 104)
    print("Q2. 前瞻成长候选构造 + 单维 IC（pe_ladder 反推一阶导/二阶导）")
    print("=" * 104)

    # 覆盖率与分布
    dist_g1, dist_acc = [], []
    n_cov = {"g1": 0, "g2": 0, "acc": 0, "fwd": 0, "tot": 0}
    for d0, m in fwd.items():
        for c6, r in m.items():
            n_cov["tot"] += 1
            g1, g2, acc = fwd_growth(r.get("pe_ladder"))
            if g1 is not None:
                n_cov["g1"] += 1
                dist_g1.append(g1)
            if g2 is not None:
                n_cov["g2"] += 1
            if acc is not None:
                n_cov["acc"] += 1
                dist_acc.append(acc)
            if isinstance(r.get("fwd_pe"), (int, float)) and r["fwd_pe"] > 0:
                n_cov["fwd"] += 1

    def pctl(xs, q):
        if not xs:
            return None
        ys = sorted(xs)
        i = min(len(ys) - 1, max(0, int(q * (len(ys) - 1))))
        return ys[i]

    print(f"覆盖率（{n_cov['tot']} 股票日）: g1={n_cov['g1']} ({n_cov['g1']/max(1,n_cov['tot']):.1%}) "
          f"g2={n_cov['g2']} acc={n_cov['acc']} fwd_pe={n_cov['fwd']}")
    print(f"一阶导 g1: p10={pctl(dist_g1,.1):+.1f}%  p25={pctl(dist_g1,.25):+.1f}%  "
          f"中位={pctl(dist_g1,.5):+.1f}%  p75={pctl(dist_g1,.75):+.1f}%  p90={pctl(dist_g1,.9):+.1f}%")
    print(f"二阶导 acc: p10={pctl(dist_acc,.1):+.1f}%  p25={pctl(dist_acc,.25):+.1f}%  "
          f"中位={pctl(dist_acc,.5):+.1f}%  p75={pctl(dist_acc,.75):+.1f}%  p90={pctl(dist_acc,.9):+.1f}%")
    out["q2_coverage"] = {"n": n_cov,
                          "g1_pct": {"p10": pctl(dist_g1, .1), "p50": pctl(dist_g1, .5),
                                     "p90": pctl(dist_g1, .9)},
                          "acc_pct": {"p10": pctl(dist_acc, .1), "p50": pctl(dist_acc, .5),
                                      "p90": pctl(dist_acc, .9)}}

    def fwd_fn(which):
        def f(r, s, d0):
            c6 = (r.get("code") or "")
            rec = fwd.get(d0, {}).get(c6)
            if not rec:
                return None
            g1, g2, acc = fwd_growth(rec.get("pe_ladder"))
            if which == "g1":
                return g1
            if which == "g2":
                return g2
            if which == "acc":
                return acc
            if which == "fwd":
                v = rec.get("fwd_pe")
                return (-v) if isinstance(v, (int, float)) and v > 0 else None
            if which == "g1_acc_50_30":
                if g1 is None and acc is None:
                    return None
                return 0.5 * g1 if acc is None else (0.3 * acc if g1 is None else 0.5 * g1 + 0.3 * acc)
            if which == "g1_acc_eq":   # 等权，缺项按可用项
                vs = [v for v in (g1, acc) if v is not None]
                return sum(vs) / len(vs) if vs else None
            if which == "g1_fwd_eq":
                fv = rec.get("fwd_pe")
                parts = []
                if g1 is not None:
                    parts.append(g1)
                if isinstance(fv, (int, float)) and fv > 0:
                    parts.append(-fv)
                return sum(parts) / len(parts) if parts else None
            return None
        return f

    CANDS = ["g1", "g2", "acc", "fwd", "g1_acc_eq", "g1_fwd_eq"]
    CN = {"g1": "一阶导 g1", "g2": "g2", "acc": "二阶导 acc", "fwd": "fwd_pe取负",
          "g1_acc_eq": "g1+acc 等权", "g1_fwd_eq": "g1+fwd 等权"}
    out["q2_single_ic"] = {}
    print()
    for c in CANDS:
        recs = {str(hop): r21.ic_battery(r21.series_for_dim(snaps, tdays, px, fwd_fn(c), hop))
                for hop in (1, 5, 20)}
        out["q2_single_ic"][c] = recs
        print(f"  {CN[c]:14s} h1:{fmt(recs.get('1'))}")
        print(f"  {'':14s} h5:{fmt(recs.get('5'))}")
        print(f"  {'':14s} h20:{fmt(recs.get('20'))}")

    # ══════════════════════════════════════════════════════════════════
    print()
    print("=" * 104)
    print("Q3. 冗余度：前瞻成长 vs 现有 7 维（截面 rank 相关）")
    print("=" * 104)

    def dim_value(d, r, d0):
        if d == "score_fund":
            v = r.get("score_fund")
            return v if isinstance(v, (int, float)) else None
        if d == "score_bias60":
            v = r.get("score_bias60")
            return v if isinstance(v, (int, float)) else None
        if d == "score_rsi_rev":
            v = r.get("score_rsi_rev")
            return v if isinstance(v, (int, float)) else None
        if d == "score_tech_neg":
            v = r.get("score_tech")
            return (-v) if isinstance(v, (int, float)) else None
        if d == "score_size":
            mv = r.get("circ_mv")
            return (-mv) if isinstance(mv, (int, float)) else None
        if d == "score_quality":
            rec = qg.get(d0, {}).get(r.get("code"))
            if not rec:
                return None
            return r21.aggregate(rec, "grouped")
        if d == "score_growth":
            rec = qg.get(d0, {}).get(r.get("code"))
            if not rec:
                return None
            return r21.aggregate(rec, "g6040")
        if d == "fwd_g1":
            return fwd_fn("g1")(r, None, d0)
        if d == "fwd_acc":
            return fwd_fn("acc")(r, None, d0)
        if d == "fwd_g1acc":
            return fwd_fn("g1_acc_eq")(r, None, d0)
        return None

    DIMS = ["score_fund", "score_bias60", "score_rsi_rev", "score_tech_neg",
            "score_size", "score_quality", "score_growth"]
    NEW = ["fwd_g1", "fwd_acc", "fwd_g1acc"]

    # 用 dimension 层（scored 里的当日截面）算相关 —— 与 round23 structural_diag 同法
    corr_pairs = defaultdict(list)
    for d0, m in fwd.items():
        rows = []
        for c6, rec in m.items():
            rows.append(dict(rec, code=c6))
        if len(rows) < 40:
            continue
        for nd in NEW:
            xs, ys = [], []
            for r in rows:
                a = dim_value(nd, r, d0)
                if a is None:
                    continue
                xs.append((a, r))
            if len(xs) < 40:
                continue
            av = [a for a, _ in xs]
            ra = r21.rank_pct(av)
            amap = {id(x[1]): ra[i] for i, x in enumerate(xs)}
            for od in DIMS:
                bv, idx = [], []
                for i, (a, r) in enumerate(xs):
                    b = dim_value(od, r, d0)
                    if isinstance(b, (int, float)):
                        bv.append(b)
                        idx.append(i)
                if len(bv) < 30:
                    continue
                rb = r21.rank_pct(bv)
                bmap = {idx[k]: rb[k] for k in range(len(idx))}
                common = [i for i in range(len(xs)) if i in bmap]
                if len(common) < 30:
                    continue
                xa = [ra[i] for i in common]
                xb = [bmap[i] for i in common]
                c = fe._pearson(xa, xb)
                if c is not None:
                    corr_pairs[(nd, od)].append(c)

    out["q3_corr"] = {}
    print(f"{'新维':12s} {'原维':16s} {'截面平均 r':>10s} {'截面数':>7s}")
    for nd in NEW:
        for od in DIMS:
            vs = corr_pairs.get((nd, od)) or []
            if not vs:
                continue
            m = sum(vs) / len(vs)
            out["q3_corr"][f"{nd}~{od}"] = round(m, 3)
            flag = " ⚠️高冗余" if abs(m) >= 0.6 else ""
            print(f"{CN.get(nd.replace('fwd_',''), nd):12s} {od.replace('score_',''):16s} "
                  f"{m:+10.3f} {len(vs):7d}{flag}")
        print()

    # ── Q3b: 换维后的复合 IC（rank 空间，与 round23 同法）
    print("── Q3b 换维检验：把 score_growth 换成前瞻口径后，7 维复合 IC 怎么走")
    W47 = {"score_fund": 0.15, "score_bias60": 0.175, "score_rsi_rev": 0.175,
           "score_tech_neg": 0.15, "score_size": 0.10, "score_quality": 0.12,
           "score_growth": 0.13}

    # 用 fwd_hist 的截面重建（比 factor_history 覆盖更全：86 vs 85，且都带前瞻）
    snaps_fwd = []
    for d0, m in sorted(fwd.items()):
        snaps_fwd.append({"date": d0, "rows": [dict(rec, code=c6) for c6, rec in m.items()]})

    def build_composite(weights, hop, extra_map=None):
        tidx = {d: i for i, d in enumerate(tdays)}
        ser = []
        for s in snaps_fwd:
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
                    if extra_map and d in extra_map:
                        vals[d] = extra_map[d](r, s, d0)
                    else:
                        vals[d] = dim_value(d, r, d0)
                rows.append((vals, p1 / p0 - 1.0))
            if len(rows) < 40:
                continue
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
            tv, rv2 = [], []
            for idx, (vals, ret) in enumerate(rows):
                num = den = 0.0
                for d, w in weights.items():
                    if w <= 0:
                        continue
                    mm = dim_ranks.get(d)
                    if mm is None or idx not in mm:
                        continue
                    num += w * mm[idx]
                    den += w
                if den >= 0.5:
                    tv.append(num / den)
                    rv2.append(ret)
            if len(tv) >= 40:
                ser.append({"date": d0, "fv": tv, "rv": rv2})
        return ser

    variants = {}
    variants["V0 现行（历史增速）"] = (W47, None)
    for tag, rep in (("g1", "g1"), ("acc", "acc"), ("g1_acc_eq", "g1acc")):
        wm = dict(W47)
        variants[f"V-{tag} 换前瞻 {CN[tag]}"] = (
            wm, {"score_growth": (lambda r, s, d0, _t=rep: fwd_fn(
                "g1" if _t == "g1" else ("acc" if _t == "acc" else "g1_acc_eq"))(r, s, d0))})
    # V-拆：growth 拆成前瞻一阶导 + 二阶导 两维（各 0.065）
    variants["V-split 拆成 g1/acc 两维"] = (
        {"score_fund": 0.15, "score_bias60": 0.175, "score_rsi_rev": 0.175,
         "score_tech_neg": 0.15, "score_size": 0.10, "score_quality": 0.12,
         "score_growth": 0.065, "fwd_acc": 0.065},
        {"fwd_acc": lambda r, s, d0: fwd_fn("acc")(r, s, d0)})

    out["q3b_variants"] = {}
    print(f"  {'方案':28s} {'h1 IC':>9s} {'h5 IC':>9s} {'h5 t(lag0)':>11s} {'h20 IC':>9s} {'h20 t(lag0)':>11s}")
    for name, (wm, em2) in variants.items():
        recs = {}
        for hop in (1, 5, 20):
            recs[str(hop)] = r21.ic_battery(build_composite(wm, hop, em2))
        out["q3b_variants"][name] = recs

        def g(hop, f="ic"):
            x = recs.get(str(hop))
            return x.get(f) if x else None

        def t0(hop):
            x = recs.get(str(hop))
            return x["t_by_lag"].get("0") if x else None
        print(f"  {name:28s} {g(1):+9.4f} {g(5):+9.4f} {t0(5):+11.2f} "
              f"{g(20):+9.4f} {t0(20):+11.2f}")

    # ══════════════════════════════════════════════════════════════════
    print()
    print("=" * 104)
    print("Q4. 时间稳定性：前瞻成长 IC 的分子期（前半/后半）+ 逐月")
    print("=" * 104)

    def subperiod_by(ser, key="date"):
        if len(ser) < 10:
            return None
        k = len(ser) // 2
        a, b = ser[:k], ser[k:]
        res = {}
        for tag, part in (("前半", a), ("后半", b)):
            ics = [fe.rank_ic(s["fv"], s["rv"]) for s in part]
            ics = [c for c in ics if c is not None]
            if len(ics) < 3:
                continue
            res[tag] = {"n": len(ics), "ic": round(sum(ics) / len(ics), 4),
                        "t0": round(r21.nw_t(ics, 0), 2) if r21.nw_t(ics, 0) is not None else None,
                        "range": [part[0][key], part[-1][key]]}
        return res

    out["q4_subperiod"] = {}
    for c in ["g1", "acc", "g1_acc_eq"]:
        ser5 = r21.series_for_dim(snaps, tdays, px, fwd_fn(c), 5)
        sp = subperiod_by(ser5)
        out["q4_subperiod"][c] = sp
        if sp:
            print(f"  {CN[c]:14s} " + "  ".join(
                f"{tag}: IC={v['ic']:+.4f} t0={v['t0']:+.2f} n={v['n']}" for tag, v in sp.items()))

    # ══════════════════════════════════════════════════════════════════
    print()
    print("=" * 104)
    print("Q5. 结论汇总")
    print("=" * 104)

    def best(c, hop="5"):
        r = out["q2_single_ic"].get(c, {}).get(hop)
        return r

    verdict = []
    for c in ["g1", "acc", "fwd", "g1_acc_eq"]:
        r1 = best(c, "1")
        r5 = best(c, "5")
        r20 = best(c, "20")
        parts = []
        for tag, r in (("h1", r1), ("h5", r5), ("h20", r20)):
            if r:
                parts.append(f"{tag} IC={r['ic']:+.4f}/t={r['t_by_lag'].get('0', 0):+.2f}"
                             f"{'✅' if r.get('robust_bonf') else ''}")
        verdict.append(f"  {CN[c]}: " + "  ".join(parts))
    print("\n".join(verdict))
    out["verdict_lines"] = verdict

    json.dump(out, open(os.path.join(HERE, "audit_round24.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    print(f"\n✅ 已写 audit_round24.json")


if __name__ == "__main__":
    main()
