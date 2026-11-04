#!/usr/bin/env python3
"""round24c：成长维 IC 为负的**机械根因**定位与修复验证。

round24/24b 的结论是「一阶导/二阶导没 alpha，fwd_pe 有 alpha 但与 fund 重复」。
但那是在**含极值污染的成长分**上对比的。本脚本回答一个更前置的问题：

  成长分 IC 为负，究竟是「用了历史数据」的错，还是「子因子极值没处理」的错？

已实测的极值证据（抽样 16 个截面 / 3349 股票日）：
  npg    p1=−1504.9   p50=+41.9    p99=+5040.9   max=+318,054.9
  qoq    p1=−3426.1   p50=+114.9   p99=+3718.6   max=+53,082.1
  accel  p1=−3648.7   p50=+5.8     p99=+5210.5   max=+314,067.7
  g6040  p1=−750.1    p50=+11.6    p95=+596.1    max=+126,465.3

⇒ 组内「等权均值」后仍被单个极端值主导；再经截面 rank，**一半样本被压到几乎相同的秩**
   → IC 测的其实是「谁不是异常值」，不是「谁成长得快」。这是机械性失真，与前瞻/历史无关。

四条检验（全部只在成长维内部做，不涉及权重调整）：
  A 极值治理梯度：raw → 缩尾(winsor 1/5%) → 对数(sign*log1p) → 双重
     每种都重算 IC，看「治理本身」能不能把负 IC 拉正
  B 子因子分组：只用稳健子因子（cagr3 / revg，天然有界）vs 只用极端子因子（npg/qoq/accel）
  C 分位化后再合成：先逐子因子截面 rank（天然免疫极值）再按 60/40 合成 → 与 raw 均值对比
  D 前瞻口径在同一治理框架下复测（g1 / acc / fwd_pe），确认相对优劣是否改变

纪律：NW lag 0/3/6，Bonferroni 3.08；缺维从分母剔除；缩尾阈值按**当日池内**分位。
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


def winsor_vals(vals, lo=0.01, hi=0.99):
    """按当日截面分位缩尾（在池内做，禁跨日拼）。"""
    if len(vals) < 10:
        return vals
    s = sorted(vals)
    a = s[max(0, int(lo * (len(s) - 1)))]
    b = s[min(len(s) - 1, int(hi * (len(s) - 1)))]
    return [min(max(v, a), b) for v in vals]


def slog(v):
    return math.copysign(math.log1p(abs(v)), v)


def main():
    from collections import Counter
    h = json.load(open(os.path.join(HERE, "factor_history.json"), encoding="utf-8"))
    snaps, _ = fe._drop_stale_price_snapshots(h["snapshots"])
    tdays, px = fe.px_index(h)
    em = r21.load_em_cache()
    fwd = rb.load_fwd()

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
    print(f"截面={len(snaps)}  子因子回填={sum(len(v) for v in subs.values())} 股票日")

    out = {"n_snapshots": len(snaps)}

    # ══════════════════════════════════════════════════════════
    print()
    print("=" * 104)
    print("A. 极值治理梯度：同一个成长维，换四种治理方式看 IC")
    print("=" * 104)

    def g_raw(sub):
        """现行生产口径：组内 raw 等权均值 → 组间 60/40。
        缺组整组剔除（与 fund_split.family_scores 同口径）。"""
        num = den = 0.0
        for grp, members, w in (("hist", HIST_G, 0.6), ("marg", MARG_G, 0.4)):
            vs = [sub.get(m) for m in members]
            vs = [v for v in vs if isinstance(v, (int, float))]
            if not vs:
                continue
            num += w * (sum(vs) / len(vs))
            den += w
        return num / den if den > 0 else None

    def make_scorer(mode):
        """mode: raw / winsor / log / both / rank_sub"""
        def fn(r, s, d0):
            code = r.get("code")
            day = subs.get(d0) or {}
            if not day:
                return None
            if mode == "rank_sub":
                # 逐子因子截面 rank（免疫极值）再按 60/40 合成
                num = den = 0.0
                for grp, members, w in (("hist", HIST_G, 0.6), ("marg", MARG_G, 0.4)):
                    parts = []
                    for m in members:
                        vals, idx = [], []
                        for c, sb in day.items():
                            v = sb.get(m)
                            if isinstance(v, (int, float)):
                                vals.append(v)
                                idx.append(c)
                        if len(vals) < 20:
                            continue
                        rp = r21.rank_pct(vals)
                        d = {idx[k]: rp[k] for k in range(len(idx))}
                        if code in d:
                            parts.append(d[code])
                    if not parts:
                        continue
                    num += w * (sum(parts) / len(parts))
                    den += w
                return num / den if den > 0 else None

            sub = day.get(code)
            if not sub:
                return None
            if mode in ("winsor", "both", "log", "log_winsor"):
                # 需要当日截面全部同子因子值来定缩尾阈值
                newsub = dict(sub)
                for m in GROW:
                    vals, idx = [], []
                    for c, sb in day.items():
                        v = sb.get(m)
                        if isinstance(v, (int, float)):
                            vals.append(v)
                            idx.append(c)
                    if len(vals) < 20 or code not in idx:
                        continue
                    if mode in ("winsor", "both", "log_winsor"):
                        cur = sub.get(m)
                        if not isinstance(cur, (int, float)):
                            continue
                        s2 = sorted(vals)
                        a = s2[max(0, int(0.01 * (len(s2) - 1)))]
                        b = s2[min(len(s2) - 1, int(0.99 * (len(s2) - 1)))]
                        newsub[m] = min(max(cur, a), b)
                    if mode == "log":
                        cur = sub.get(m)
                        if isinstance(cur, (int, float)):
                            newsub[m] = slog(cur)
                if mode == "log_winsor":
                    for m in GROW:
                        v = newsub.get(m)
                        if isinstance(v, (int, float)):
                            newsub[m] = slog(v)
                return g_raw(newsub)
            return g_raw(sub)
        return fn

    for mode, label in (("raw", "① raw（现行生产口径）"),
                        ("winsor", "② 缩尾 1%/99%"),
                        ("log", "③ sign*log1p"),
                        ("log_winsor", "④ 缩尾+对数"),
                        ("rank_sub", "⑤ 逐子因子截面 rank 后合成")):
        recs = {}
        for hop in (1, 5, 20):
            recs[str(hop)] = r21.ic_battery(
                r21.series_for_dim(snaps, tdays, px, make_scorer(mode), hop))
        out[f"a_{mode}"] = recs
        print(f"  {label:26s} h5:{fmt(recs.get('5'))}")
        print(f"  {'':26s} h20:{fmt(recs.get('20'))}")

    # ══════════════════════════════════════════════════════════
    print()
    print("=" * 104)
    print("B. 子因子分组：稳健型（cagr3/revg）vs 极值型（npg/qoq/accel）")
    print("=" * 104)

    def group_fn(members):
        def fn(r, s, d0):
            sub = (subs.get(d0) or {}).get(r.get("code"))
            if not sub:
                return None
            vs = [sub.get(m) for m in members]
            vs = [v for v in vs if isinstance(v, (int, float))]
            return sum(vs) / len(vs) if vs else None
        return fn

    out["b_groups"] = {}
    for tag, mem in (("仅历史增速 npg+cagr3+revg", HIST_G),
                     ("仅边际改善 qoq+accel", MARG_G),
                     ("仅稳健型 cagr3+revg", ["cagr3", "revg"]),
                     ("仅极值型 npg+qoq+accel", ["npg", "qoq", "accel"])):
        recs = {str(hop): r21.ic_battery(r21.series_for_dim(snaps, tdays, px, group_fn(mem), hop))
                for hop in (1, 5, 20)}
        out["b_groups"][tag] = recs
        print(f"  {tag:26s} h5:{fmt(recs.get('5'))}")
        print(f"  {'':26s} h20:{fmt(recs.get('20'))}")

    # ══════════════════════════════════════════════════════════
    print()
    print("=" * 104)
    print("C. 生产可用性评估：增长治理后的成长维，是否值得保留 0.13 权重")
    print("=" * 104)

    # rank_sub 口径最稳健（免疫极值），看它与 fund/quality 的相关
    def dim_rank_sub(codes, day):
        num = den = 0.0
        for grp, members, w in (("hist", HIST_G, 0.6), ("marg", MARG_G, 0.4)):
            parts = []
            for m in members:
                vals, idx = [], []
                for c in codes:
                    v = (day.get(c) or {}).get(m)
                    if isinstance(v, (int, float)):
                        vals.append(v)
                        idx.append(c)
                if len(vals) < 20:
                    continue
                rp = r21.rank_pct(vals)
                parts.append({idx[k]: rp[k] for k in range(len(idx))})
            if parts:
                num += w
                den += w
        return num, den

    corr_acc = defaultdict(list)
    for d0, day in subs.items():
        codes = list(day.keys())
        if len(codes) < 40:
            continue
        comp = {}
        for grp, members, w in (("hist", HIST_G, 0.6), ("marg", MARG_G, 0.4)):
            parts = []
            for m in members:
                vals, idx = [], []
                for c in codes:
                    v = (day.get(c) or {}).get(m)
                    if isinstance(v, (int, float)):
                        vals.append(v)
                        idx.append(c)
                if len(vals) < 20:
                    continue
                rp = r21.rank_pct(vals)
                parts.append({idx[k]: rp[k] for k in range(len(idx))})
            if not parts:
                continue
            for c in codes:
                ps = [p[c] for p in parts if c in p]
                if ps:
                    comp.setdefault(c, []).append((w, sum(ps) / len(ps)))
        final = {}
        for c, ws in comp.items():
            num = sum(w * v for w, v in ws)
            den = sum(w for w, _ in ws)
            if den > 0:
                final[c] = num / den
        if len(final) < 40:
            continue
        cc = list(final.keys())
        fv = [final[c] for c in cc]
        rf = r21.rank_pct(fv)
        fmap = {cc[i]: rf[i] for i in range(len(cc))}
        # 与同截面的 fund 对比（用 scored 的 score_fund）
        sf = {}
        for r in (next((s for s in snaps if s["date"] == d0), {}) or {}).get("rows") or []:
            v = r.get("score_fund")
            if isinstance(v, (int, float)):
                sf[r["code"]] = v
        common = [c for c in cc if c in sf]
        if len(common) < 30:
            continue
        rs = r21.rank_pct([sf[c] for c in common])
        smap = {common[i]: rs[i] for i in range(len(common))}
        cval = fe._pearson([fmap[c] for c in common], [smap[c] for c in common])
        if cval is not None:
            corr_acc["corr(成长rank合成, fund)"].append(cval)

    out["c_corr"] = {k: round(sum(v) / len(v), 3) for k, v in corr_acc.items() if v}
    print("  与估值锚的相关（治理后成长维）:")
    for k, v in out["c_corr"].items():
        print(f"    {k} = {v:+.3f}   （治理前 +0.6707，若明显下降说明极值治理解开了同源）")

    # ══════════════════════════════════════════════════════════
    print()
    print("=" * 104)
    print("D. 前瞻口径在同一治理框架下复测（fwd_hist 截面）")
    print("=" * 104)

    def fwd_once(which):
        def fn(r, s, d0):
            rec = fwd.get(d0, {}).get(r.get("code"))
            if not rec:
                return None
            if which == "fwd_pe_neg":
                v = rec.get("fwd_pe")
                return (-v) if isinstance(v, (int, float)) and v > 0 else None
            g1, g2, acc = rb.fwd_growth(rec.get("pe_ladder"))
            return {"g1": g1, "g2": g2, "acc": acc}.get(which)
        return fn

    snaps_fwd = [{"date": d0, "rows": [dict(rec, code=c6) for c6, rec in m.items()]}
                 for d0, m in sorted(fwd.items())]
    out["d_fwd"] = {}
    for which, label in (("g1", "前瞻一阶导 g1"), ("acc", "前瞻二阶导 acc"),
                         ("fwd_pe_neg", "fwd_pe 取负")):
        recs = {str(hop): r21.ic_battery(r21.series_for_dim(snaps_fwd, tdays, px,
                                                            fwd_once(which), hop))
                for hop in (1, 5, 20)}
        out["d_fwd"][which] = recs
        print(f"  {label:18s} h5:{fmt(recs.get('5'))}")
        print(f"  {'':18s} h20:{fmt(recs.get('20'))}")

    # ══════════════════════════════════════════════════════════
    print()
    print("=" * 104)
    print("E. 结论")
    print("=" * 104)
    a_raw = out.get("a_raw", {}).get("5") or {}
    a_best = None
    best_mode = None
    for m in ("winsor", "log", "log_winsor", "rank_sub"):
        rec = out.get(f"a_{m}", {}).get("5") or {}
        if rec.get("ic") is not None:
            if a_best is None or rec["ic"] > a_best:
                a_best = rec["ic"]
                best_mode = m
    print(f"  现行 raw 口径 h5 IC = {a_raw.get('ic')}")
    print(f"  治理后最好 h5 IC = {a_best}（{best_mode}）")
    if a_raw.get("ic") is not None and a_best is not None:
        print(f"  增益 = {a_best - a_raw['ic']:+.4f}")
    out["e_summary"] = {"raw_h5_ic": a_raw.get("ic"), "best_mode": best_mode,
                        "best_h5_ic": a_best,
                        "gain": (a_best - a_raw["ic"]) if (a_raw.get("ic") is not None
                                                           and a_best is not None) else None}

    json.dump(out, open(os.path.join(HERE, "audit_round24c.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    print("\n✅ 已写 audit_round24c.json")


if __name__ == "__main__":
    main()
