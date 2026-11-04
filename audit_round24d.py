#!/usr/bin/env python3
"""round24d：上线方案最终裁决 —— 「成长怎么办 + 低波要不要加」联合测试。

round24/24b/24c 的全部证据已指向同一个结论，本脚本做最后的三件事：

  ① 把「成长维」的四种处置放在同一张表上裁决：
     K0 原样保留（0.13）  K1 降权（0.13→0.05）  K2 归零不计分  K3 反号
  ② 把 lowvol20（老大批准加入）在**同一套样本**上复测，并与现有 6 维做冗余核对
  ③ 输出可落地的最终权重表 + 必须同步的六处清单

⚠️ 关键口径纪律（本轮血的教训，务必遵守）：
   · circ_mv（score_size 维）只在 9/86 个历史截面有值，**无法回填** →
     任何跨 86 截面的「7 维复合对比」都会被「缺维剔除分母」污染，
     S0 从 +0.0596 跳到 +0.1140 就是证据。因此本脚本的复合对比
     **一律排除 size 维**（分母只用 6 维，权重在 6 维内归一），
     size 维的价值单独用单维 IC 评估，不混入复合时点对比。
   · bias60 / rsi_rev 用 px 回填（83 个截面），与 round23 逐字同口径。
   · NW lag 0/3/6，Bonferroni 3.08；缺维从分母剔除，禁 `or 0`。
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
GROW = ["npg", "cagr3", "revg", "qoq", "accel"]
HIST_G = ["npg", "cagr3", "revg"]
MARG_G = ["qoq", "accel"]


def fmt(rec):
    if not rec:
        return "样本不足"
    t = "/".join(f"{rec['t_by_lag'].get(str(L), float('nan')):+.2f}" for L in LAGS)
    return (f"n={rec['n']:<3} IC={rec['ic']:+.4f} t={t} "
            f"{'✅过Bonf' if rec.get('robust_bonf') else '⚠️未过'}")


def main():
    h = json.load(open(os.path.join(HERE, "factor_history.json"), encoding="utf-8"))
    snaps, _ = fe._drop_stale_price_snapshots(h["snapshots"])
    tdays, px = fe.px_index(h)
    em = r21.load_em_cache()

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
    print(f"截面={len(snaps)}  反转块回填={len(bf)} 日  "
          f"子因子={sum(len(v) for v in subs.values())} 股票日")

    out = {"n_snapshots": len(snaps)}

    # ── lowvol20：过去 20 日收益率标准差取负（round23 Q4 唯一不冗余候选）
    def lowvol20(code, d0):
        m = px.get(code)
        if not m:
            return None
        tidx = {d: i for i, d in enumerate(tdays)}
        if d0 not in tidx:
            return None
        i0 = tidx[d0]
        if i0 < 20:
            return None
        cl = [m.get(tdays[i]) for i in range(i0 - 20, i0 + 1)]
        if any(c is None or c <= 0 for c in cl):
            return None
        rets = [(cl[i] / cl[i - 1] - 1.0) for i in range(1, len(cl))]
        n = len(rets)
        mu = sum(rets) / n
        sd = (sum((r - mu) ** 2 for r in rets) / (n - 1)) ** 0.5
        return -sd      # 取负：低波=高分

    # ── 基础维取值（不含 size）
    def dim_val(d, r, d0):
        if d == "score_fund":
            v = r.get("score_fund")
            return v if isinstance(v, (int, float)) else None
        if d == "score_tech_neg":
            v = r.get("score_tech")
            return (-v) if isinstance(v, (int, float)) else None
        if d in ("score_bias60", "score_rsi_rev"):
            t = (bf.get(d0) or {}).get(r.get("code"))
            if not t:
                return None
            return t[1] if d == "score_bias60" else t[2]
        if d == "score_quality" or d == "score_growth":
            sub = (subs.get(d0) or {}).get(r.get("code"))
            if not sub:
                return None
            if d == "score_quality":
                return r21.aggregate(sub, "grouped")
            return r21.aggregate(sub, "g6040")
        if d == "lowvol20":
            return lowvol20(r.get("code"), d0)
        if d == "score_growth_neg":
            sub = (subs.get(d0) or {}).get(r.get("code"))
            if not sub:
                return None
            v = r21.aggregate(sub, "g6040")
            return (-v) if v is not None else None
        return None

    # ══════════════════════════════════════════════════════════════
    print()
    print("=" * 104)
    print("Q1. lowvol20 单维复测（与 round23 Q4 交叉验证）+ 冗余度")
    print("=" * 104)

    def ll_fn(r, s, d0):
        return lowvol20(r.get("code"), d0)

    recs_lv = {str(hop): r21.ic_battery(r21.series_for_dim(snaps, tdays, px, ll_fn, hop))
               for hop in (1, 5, 20)}
    out["q1_lowvol20"] = recs_lv
    for hop in ("1", "5", "20"):
        print(f"  lowvol20 h{hop:<2s}: {fmt(recs_lv.get(hop))}")

    # 分子期
    ser5 = r21.series_for_dim(snaps, tdays, px, ll_fn, 5)
    k = len(ser5) // 2
    out["q1_lowvol20_sub"] = {}
    print("  分子期 h5:")
    for tag, part in (("前半", ser5[:k]), ("后半", ser5[k:])):
        ics = [fe.rank_ic(x["fv"], x["rv"]) for x in part]
        ics = [c for c in ics if c is not None]
        if len(ics) < 3:
            continue
        t = r21.nw_t(ics, 0)
        out["q1_lowvol20_sub"][tag] = {"n": len(ics), "ic": round(sum(ics) / len(ics), 4),
                                       "t0": round(t, 2) if t is not None else None}
        print(f"    {tag}: IC={sum(ics)/len(ics):+.4f} t0={t:+.2f} n={len(ics)}")

    # 冗余度：与现有 6 维的截面 rank 相关
    EX6 = ["score_fund", "score_bias60", "score_rsi_rev", "score_tech_neg",
           "score_quality", "score_growth"]
    red = defaultdict(list)
    for s in snaps:
        d0 = s["date"]
        rows = s["rows"]
        lv, idx = [], []
        for i, r in enumerate(rows):
            v = lowvol20(r.get("code"), d0)
            if v is not None:
                lv.append(v)
                idx.append(i)
        if len(lv) < 40:
            continue
        rl = r21.rank_pct(lv)
        lmap = {idx[j]: rl[j] for j in range(len(idx))}
        for od in EX6:
            bv = []
            bidx = []
            for i in idx:
                v = dim_val(od, rows[i], d0)
                if isinstance(v, (int, float)):
                    bv.append(v)
                    bidx.append(i)
            if len(bv) < 30:
                continue
            rbp = r21.rank_pct(bv)
            bmap = {bidx[j]: rbp[j] for j in range(len(bidx))}
            common = [i for i in idx if i in bmap]
            if len(common) < 30:
                continue
            c = fe._pearson([lmap[i] for i in common], [bmap[i] for i in common])
            if c is not None:
                red[od].append(c)

    out["q1_redundancy"] = {k2: round(sum(v) / len(v), 3) for k2, v in red.items() if v}
    print("  与现有 6 维的截面 rank 相关（|r|<0.35 视为不冗余）:")
    for k2, v in out["q1_redundancy"].items():
        flag = "✅不冗余" if abs(v) < 0.35 else ("⚠️中度共线" if abs(v) < 0.6 else "❌高度共线")
        print(f"    lowvol20 ~ {k2.replace('score_',''):10s} r={v:+.3f}  {flag}")

    # ══════════════════════════════════════════════════════════════
    print()
    print("=" * 104)
    print("Q2. 复合裁决（6 维，不含 size）：成长维四种处置 × 是否加低波")
    print("=" * 104)

    # 6 维权重（从 v4.7 的 7 维里剔除 size 0.10 后归一）
    W7 = {"score_fund": 0.15, "score_bias60": 0.175, "score_rsi_rev": 0.175,
          "score_tech_neg": 0.15, "score_size": 0.10, "score_quality": 0.12,
          "score_growth": 0.13}
    base6 = {k: v for k, v in W7.items() if k != "score_size"}
    s6 = sum(base6.values())
    base6 = {k: v / s6 for k, v in base6.items()}
    print(f"  基准 6 维（v4.7 剔除 size 后归一，Σ={sum(base6.values()):.4f}）: "
          + " ".join(f"{k.replace('score_','')}={v:.3f}" for k, v in base6.items()))

    def build(w, hop, min_n=40):
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
                vals = {d: dim_val(d, r, d0) for d in w if w[d] > 0}
                rows.append((vals, p1 / p0 - 1.0))
            if len(rows) < min_n:
                continue
            dim_ranks = {}
            for d in w:
                if w[d] <= 0:
                    continue
                xs = [rv[0].get(d) for rv in rows]
                present = [i for i, x in enumerate(xs) if isinstance(x, (int, float))]
                if len(present) < 0.5 * len(rows):
                    dim_ranks[d] = None
                    continue
                rp = r21.rank_pct([xs[i] for i in present])
                dim_ranks[d] = {i: rp[k] for k, i in enumerate(present)}
            tv, rv2 = [], []
            for i, (vals, ret) in enumerate(rows):
                num = den = 0.0
                for d, ww in w.items():
                    if ww <= 0:
                        continue
                    mm = dim_ranks.get(d)
                    if mm is None or i not in mm:
                        continue
                    num += ww * mm[i]
                    den += ww
                if den >= 0.5:
                    tv.append(num / den)
                    rv2.append(ret)
            if len(tv) >= min_n:
                ser.append({"date": d0, "fv": tv, "rv": rv2})
        return ser

    def renorm(w):
        s = sum(w.values())
        return {k: v / s for k, v in w.items() if v > 0}

    cases = {}
    # K0 原样
    cases["K0 成长原样 0.13"] = base6
    # K1 降权到 0.05
    w = dict(base6)
    freed = w["score_growth"] - 0.05
    w["score_growth"] = 0.05
    for kk in ("score_fund", "score_bias60", "score_rsi_rev", "score_tech_neg", "score_quality"):
        w[kk] += freed / 5.0
    cases["K1 成长降权 0.05"] = renorm(w)
    # K2 归零
    w = {k: v for k, v in base6.items() if k != "score_growth"}
    cases["K2 成长去掉"] = renorm(w)
    # K3 反号
    w = {k: v for k, v in base6.items() if k != "score_growth"}
    w["score_growth_neg"] = 0.13
    cases["K3 成长反号 0.13"] = renorm(w)
    # 加低波：成长去掉 + lowvol20
    w = {k: v for k, v in base6.items() if k != "score_growth"}
    w["lowvol20"] = 0.13
    cases["K2+LV 去成长 换 低波0.13"] = renorm(w)
    # 加低波：成长降权 + 低波
    w = dict(base6)
    freed = w["score_growth"] - 0.05
    w["score_growth"] = 0.05
    w["lowvol20"] = freed
    cases["K1+LV 成长0.05 + 低波0.08"] = renorm(w)
    # 加低波：成长原样 + 低波（从各维均摊）
    w = {k: v * 0.87 for k, v in base6.items()}
    w["lowvol20"] = 0.13
    cases["K0+LV 成长0.13 + 低波0.13"] = renorm(w)

    out["q2_cases"] = {}
    print(f"  {'方案':30s} {'h1 IC':>9s} {'h5 IC':>9s} {'h5 t0':>7s} {'h20 IC':>9s} {'h20 t0':>7s} {'h5 bonf':>7s}")
    for name, w in cases.items():
        recs = {str(hop): r21.ic_battery(build(w, hop)) for hop in (1, 5, 20)}
        out["q2_cases"][name] = {"weights": {k: round(v, 4) for k, v in w.items()}, "ic": recs}

        def g(hp, f="ic"):
            x = recs.get(str(hp))
            return x.get(f) if x else None

        def t0(hp):
            x = recs.get(str(hp))
            return x["t_by_lag"].get("0") if x else None

        def fn2(v, spec="+.4f"):
            return "    n/a" if v is None else format(v, spec)
        b = recs.get("5")
        print(f"  {name:30s} {fn2(g(1)):>9s} {fn2(g(5)):>9s} {fn2(t0(5)):>7s} "
              f"{fn2(g(20)):>9s} {fn2(t0(20)):>7s} "
              f"{('✅' if (b and b.get('robust_bonf')) else '⚠️'):>7s}")

    # ══════════════════════════════════════════════════════════════
    print()
    print("=" * 104)
    print("Q3. 最优方案分子期稳健性")
    print("=" * 104)

    def stab(w, hop=5):
        ser = build(w, hop)
        k2 = len(ser) // 2
        res = {}
        for tag, part in (("前半", ser[:k2]), ("后半", ser[k2:])):
            ics = [fe.rank_ic(x["fv"], x["rv"]) for x in part]
            ics = [c for c in ics if c is not None]
            if len(ics) < 3:
                continue
            t = r21.nw_t(ics, 0)
            res[tag] = {"n": len(ics), "ic": round(sum(ics) / len(ics), 4),
                        "t0": round(t, 2) if t is not None else None}
        return res

    out["q3_stability"] = {}
    for name in ("K0 成长原样 0.13", "K2 成长去掉", "K2+LV 去成长 换 低波0.13"):
        st = stab(cases[name], 5)
        out["q3_stability"][name] = st
        print(f"  ── {name}")
        for tag, v in st.items():
            print(f"     {tag}: IC={v['ic']:+.4f} t0={v['t0']:+.2f} n={v['n']}")

    json.dump(out, open(os.path.join(HERE, "audit_round24d.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    print("\n✅ 已写 audit_round24d.json")


if __name__ == "__main__":
    main()
