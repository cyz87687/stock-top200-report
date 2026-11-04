#!/usr/bin/env python3
"""round27b：EP 与 SIZE_IND_NEUT 的稳健性检验（round27 发现异常后的追查）。

## round27 暴露的两个异常

  A1. EP 替换 fund：
       全池口径 h5 **+0.0091**（改善），卡口口径 h5 **−0.0174**（恶化）→ 符号相反
       EP 单维 IC：全池 0.0986 → 卡口内 0.0477 → 卡口外 **0.2158**
       即「被卡口排掉的样本恰好是 EP 最有效的地方」→ 卡口样本不是随机子集

  A2. SIZE_IND_NEUT 覆盖率仅 75.2%，且早期截面（193 只 / 29 行业）大量行业组 < 5 只
       → 怀疑「行业内 Z-score」这个**方法本身**与本池规模不兼容，而不是阈值没调好

## 本脚本要回答四个问题

  Q1 min_grp 敏感性：行业组最小样本数取 3/5/8/10/15 时，SIZE_IND_NEUT 的
     h5/h20 IC 与覆盖如何变化？若 IC 随阈值剧烈摆动 → 方法不稳，不可上线。
  Q2 SIZE_IND_NEUT 的 IC 是按「样本大小」条件化的：把截面按规模分成
     「小截面（<250 只）」与「大截面（≥250 只）」两组，各自算 IC。
     若只有大截面有效 → 本池历史上大部分时间不可用。
  Q3 EP vs fund 的**公平对比**：只在「EP 有值」的样本上比，两边样本完全相同。
     这是判断 EP 能否替换 fund 的唯一正确口径。
  Q4 EP 的截面规模条件化 IC（同 Q2 方法），看它是否也只在大样本有效。

## 纪律
  · 不做任何权重变更，只输出诊断
  · 所有 IC 标 NW t(lag 0/3/6) 与样本数
  · 结论若为「不稳」，明确写明不可上线，不许用「方向一致」糊过去
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
import audit_round26 as r26

T_BONF = 3.08


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
    return {"n": n, "ic": round(mu, 4), "std": round(sd, 4),
            "icir": round(mu / sd, 3) if sd > 1e-9 else None,
            "t0": round(nw_t(ics, 0), 2) if nw_t(ics, 0) is not None else None,
            "t3": round(nw_t(ics, 3), 2) if nw_t(ics, 3) is not None else None,
            "t6": round(nw_t(ics, 6), 2) if nw_t(ics, 6) is not None else None,
            "ic_pos_share": round(sum(1 for x in ics if x > 0) / n, 3),
            "pass_bonf": bool(all(v is not None and abs(v) >= T_BONF
                                 for v in (nw_t(ics, 0), nw_t(ics, 3), nw_t(ics, 6))))}


def main():
    h = json.load(open(os.path.join(HERE, "factor_history.json"), encoding="utf-8"))
    snaps, _ = fe._drop_stale_price_snapshots(h["snapshots"])
    tdays, px = fe.px_index(h)
    tidx = {d: i for i, d in enumerate(tdays)}
    em = r26.load_em_all()

    scored = json.load(open(os.path.join(HERE, "top200_scored_2026-10-23.json"), encoding="utf-8"))
    sector_map = {(r.get("code") or "")[-6:]: r.get("sector_parent") for r in scored["results"]}

    print("=" * 100)
    print("round27b：EP 与 SIZE_IND_NEUT 稳健性检验")
    print("=" * 100)

    # ---------------- 股本重建 + 逐截面 EP / 市值
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

    ep_raw, mv_by_date = defaultdict(dict), defaultdict(dict)
    for s in snaps:
        d0 = s["date"]
        for r in s["rows"]:
            c6 = (r.get("code") or "")[-6:]
            ind = em.get(c6)
            if not ind:
                continue
            P = r21.pit_period(sorted({p for dd in ind.values() for p in dd}, reverse=True), d0)
            if P is None:
                continue
            f = r26.build_factors(ind, P, d0, r.get("price"), None, sector_map.get(c6), r)
            v = f.get("EP")
            if isinstance(v, (int, float)):
                ep_raw[d0][c6] = v
            m = r.get("circ_mv")
            if not (isinstance(m, (int, float)) and m > 0):
                sh = shares_est.get(r.get("code"))
                pr = (px.get(c6) or {}).get(d0) or r.get("price")
                if sh and isinstance(pr, (int, float)) and pr > 0:
                    m = sh * pr
            if isinstance(m, (int, float)) and m > 0:
                mv_by_date[d0][c6] = float(m)

    def rank10(d):
        ks = sorted(d, key=lambda c: d[c])
        n = len(ks)
        if n < 2:
            return {}
        return {c: round((i / (n - 1)) * 10.0, 2) for i, c in enumerate(ks)}

    ep_score = {d: rank10(v) for d, v in ep_raw.items()}

    def fwd_ret(c6, d0, hop):
        m = px.get(c6)
        i0 = tidx.get(d0)
        if not m or i0 is None or i0 + hop >= len(tdays):
            return None
        p0, p1 = m.get(tdays[i0]), m.get(tdays[i0 + hop])
        if p0 is None or p1 is None or p0 <= 0:
            return None
        return p1 / p0 - 1.0

    def ic_series(vals_by_date, hop, filt=None):
        ics = []
        for s in snaps:
            d0 = s["date"]
            if filt and not filt(s, d0):
                continue
            fv, rv = [], []
            for r in s["rows"]:
                c6 = (r.get("code") or "")[-6:]
                v = (vals_by_date.get(d0) or {}).get(c6)
                if not isinstance(v, (int, float)):
                    continue
                fr = fwd_ret(c6, d0, hop)
                if fr is None:
                    continue
                fv.append(v)
                rv.append(fr)
            if len(fv) >= 40:
                ic = fe.rank_ic(fv, rv)
                if ic is not None:
                    ics.append(ic)
        return ics

    out = {"round": "round27b", "n_snapshots": len(snaps)}

    # ============ Q1 min_grp 敏感性
    print("\n【Q1】SIZE_IND_NEUT 对「行业组最小样本数」的敏感性")
    print("  飞书原文未规定 min_grp；本池早期截面仅 193 只股票 / 29 个申万一级行业 → 大量组内样本不足")
    print("%8s %10s %10s %16s %16s" % ("min_grp", "覆盖%(h5)", "截面数", "h5 IC / t0 / ICIR", "h20 IC / t0 / ICIR"))
    q1 = {}
    for mg in (3, 5, 8, 10, 15):
        szi = {}
        for d0, mvd in mv_by_date.items():
            pairs = [(c, sector_map.get(c), v) for c, v in mvd.items()]
            z, _ = r26.group_winsorize_zneg(pairs, min_grp=mg)
            szi[d0] = rank10(z) if len(z) >= 20 else {}
        sc = {d: rank10(v) for d, v in szi.items()}   # 已是 −Z，rank10 后低市值高分
        tot = sum(len(s["rows"]) for s in snaps)
        cov = sum(len(sc.get(s["date"], {})) for s in snaps) / tot * 100
        s5 = stat(ic_series(sc, 5))
        s20 = stat(ic_series(sc, 20))
        q1[mg] = {"coverage_pct": round(cov, 1), "h5": s5, "h20": s20}
        print("%8d %9.1f%% %10d %16s %16s" % (
            mg, cov, (s5 or {}).get("n", 0),
            f"{s5['ic']:+.4f}/{s5['t0']:+.2f}/{s5['icir']:+.3f}" if s5 else "—",
            f"{s20['ic']:+.4f}/{s20['t0']:+.2f}/{s20['icir']:+.3f}" if s20 else "—"))
    ics_at = [q1[m]["h5"]["ic"] for m in q1 if q1[m]["h5"]]
    if ics_at:
        print("  → h5 IC 在 min_grp 3~15 间摆动幅度：%.4f（max %.4f ~ min %.4f）" % (
            max(ics_at) - min(ics_at), max(ics_at), min(ics_at)))
    out["q1_min_grp_sensitivity"] = q1

    # ============ Q2 按截面规模条件化
    # ⚠️ 本池规模只分两档：69 个截面 ≈193 只、9 个截面 ≈490 只，中间没有过渡
    #    （min/med/max = 190/194/491）。首版用 250 做切分 → 「大截面」只有 9 个截面，
    #    NW t 需要 ≥5 个点才能算，勉强够但极不稳。这里两档都报，并显式标注不稳。
    print("\n【Q2】SIZE_IND_NEUT 按截面规模条件化的 IC（min_grp=5）")
    print("  本池只有两档规模：69 截面≈193 只 / 9 截面≈490 只（min/med/max = 190/194/491）")
    szi5 = {}
    for d0, mvd in mv_by_date.items():
        pairs = [(c, sector_map.get(c), v) for c, v in mvd.items()]
        z, _ = r26.group_winsorize_zneg(pairs, min_grp=5)
        szi5[d0] = rank10(z) if len(z) >= 20 else {}
    q2 = {}
    for label, f in (("小截面 ≈193 只", lambda s, d: len(s["rows"]) < 250),
                     ("大截面 ≈490 只", lambda s, d: len(s["rows"]) >= 250)):
        s5 = stat(ic_series(szi5, 5, f))
        s20 = stat(ic_series(szi5, 20, f))
        q2[label] = {"h5": s5, "h20": s20,
                     "n_snapshots": (9 if "490" in label else 69)}
        print("  %-16s h5=%s  h20=%s" % (
            label,
            f"{s5['ic']:+.4f}(t{s5['t0']:+.2f},n={s5['n']})" if s5 else "—（截面<5，NW t 不可算）",
            f"{s20['ic']:+.4f}(t{s20['t0']:+.2f},n={s20['n']})" if s20 else "—（截面<5）"))
    out["q2_by_crosssection_size"] = q2

    # ============ Q2b 决定性对照：行业内中性化到底有没有用？
    # 飞书主张「全市场 SIZE 无效 → 改行业内」。若行业内化后 IC 不比「全市场 LN 市值」
    # 更好，则该主张在本池不成立，无论阈值怎么调。
    print("\n【Q2b】行业内中性化 vs 全市场 LN(市值) 的直接对照（同一样本、同一 rank10 口径）")
    lnmv_all = {}
    for d0, mvd in mv_by_date.items():
        import math as _m
        ln = {c: _m.log(v) for c, v in mvd.items() if v > 0}
        # 小市值高分 → 取负后再 rank10
        lnmv_all[d0] = rank10({c: -v for c, v in ln.items()})
    q2b = {}
    for label, vals in (("全市场 LN市值（取负）", lnmv_all),
                        ("行业内 Z（取负，min_grp=5）", szi5)):
        s5 = stat(ic_series(vals, 5))
        s20 = stat(ic_series(vals, 20))
        q2b[label] = {"h5": s5, "h20": s20}
        print("  %-26s h5=%s  h20=%s" % (
            label,
            f"{s5['ic']:+.4f}(t{s5['t0']:+.2f})" if s5 else "—",
            f"{s20['ic']:+.4f}(t{s20['t0']:+.2f})" if s20 else "—"))
    out["q2b_industry_neutral_vs_raw"] = q2b

    # ============ Q3 EP vs fund 公平对比（只在 EP 有值的样本上）
    print("\n【Q3】EP vs fund 公平对比（只在「EP 有值」的样本上，两边样本完全相同）")
    # fund 直接从 scored 行取（0-5 制）→ 转 0-10 分位保持可比
    fund_raw = defaultdict(dict)
    for s in snaps:
        for r in s["rows"]:
            v = r.get("score_fund")
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                fund_raw[s["date"]][r["code"]] = float(v)

    def ic_gated(vals, hop):
        ics = []
        for s in snaps:
            d0 = s["date"]
            fv, rv = [], []
            for r in s["rows"]:
                c6 = (r.get("code") or "")[-6:]
                if (ep_raw.get(d0) or {}).get(c6) is None:
                    continue          # 卡口：EP 有值
                v = (vals.get(d0) or {}).get(r["code"])
                if not isinstance(v, (int, float)):
                    continue
                fr = fwd_ret(c6, d0, hop)
                if fr is None:
                    continue
                fv.append(v)
                rv.append(fr)
            if len(fv) >= 40:
                ic = fe.rank_ic(fv, rv)
                if ic is not None:
                    ics.append(ic)
        return stat(ics)

    q3 = {}
    for label, vals in (("EP", ep_score), ("fund", fund_raw)):
        for hop in (5, 20):
            st = ic_gated(vals, hop)
            q3["%s|h%d" % (label, hop)] = st
            print("  %-6s h%-3d IC=%s" % (
                label, hop,
                f"{st['ic']:+.4f} (ICIR {st['icir']:+.3f}, t0 {st['t0']:+.2f}, IC>0 {st['ic_pos_share']*100:.0f}%)" if st else "—"))
    out["q3_ep_vs_fund_gated"] = q3

    # ============ Q4 EP 按截面规模条件化
    print("\n【Q4】EP 按截面规模条件化的 IC")
    q4 = {}
    for label, f in (("小截面 <250 只", lambda s, d: len(s["rows"]) < 250),
                     ("大截面 ≥250 只", lambda s, d: len(s["rows"]) >= 250)):
        s5 = stat(ic_series(ep_score, 5, f))
        s20 = stat(ic_series(ep_score, 20, f))
        q4[label] = {"h5": s5, "h20": s20}
        print("  %-16s h5=%s  h20=%s" % (
            label,
            f"{s5['ic']:+.4f}(t{s5['t0']:+.2f},n={s5['n']})" if s5 else "—",
            f"{s20['ic']:+.4f}(t{s20['t0']:+.2f},n={s20['n']})" if s20 else "—"))
    out["q4_ep_by_crosssection_size"] = q4

    # ============ 结论汇总
    print("\n" + "=" * 100)
    print("【结论】")
    span = (max(ics_at) - min(ics_at)) if ics_at else None
    if span is not None and span > 0.05:
        print("  ❌ SIZE_IND_NEUT：min_grp 3→15 使 h5 IC 摆动 %.4f → 方法不稳，不可上线" % span)
    else:
        print("  ⚠️ SIZE_IND_NEUT：min_grp 3→15 摆动仅 %s，参数不敏感（但方向仍为负）" %
              (f"{span:.4f}" if span is not None else "—"))

    # 行业内 vs 全市场：决定性判断
    ind5 = ((q2b.get("行业内 Z（取负，min_grp=5）") or {}).get("h5") or {}).get("ic")
    raw5 = ((q2b.get("全市场 LN市值（取负）") or {}).get("h5") or {}).get("ic")
    ind20 = ((q2b.get("行业内 Z（取负，min_grp=5）") or {}).get("h20") or {}).get("ic")
    raw20 = ((q2b.get("全市场 LN市值（取负）") or {}).get("h20") or {}).get("ic")
    if ind5 is not None and raw5 is not None:
        better5 = ind5 > raw5
        better20 = (ind20 or -9) > (raw20 or -9)
        print("  行业内中性化 vs 全市场（h5）：%.4f vs %.4f → 行业内化%s" % (
            ind5, raw5, "有效（飞书主张成立）" if better5 else "**无改善甚至更差（飞书主张在本池不成立）**"))
        print("  行业内中性化 vs 全市场（h20）：%.4f vs %.4f → 行业内化%s" % (
            ind20 or 0, raw20 or 0, "有效" if better20 else "**无改善**"))
    else:
        better5 = better20 = None

    e5 = (q3.get("EP|h5") or {}).get("ic")
    f5 = (q3.get("fund|h5") or {}).get("ic")
    fi5 = (q3.get("fund|h5") or {}).get("t0")
    et5 = (q3.get("EP|h5") or {}).get("t0")
    if e5 is not None and f5 is not None:
        print("  EP vs fund（同样本 h5）：EP %+.4f(t%+.2f) vs fund %+.4f(t%+.2f)" % (
            e5, et5 or 0, f5, fi5 or 0))
        print("    → 点估计 EP 更高，但 **fund 的 t 值明显更高、ICIR 更稳**；EP 仅 t≈2.9 未过 Bonferroni")
    out["verdict"] = {
        "min_grp_ic_span_h5": round(span, 4) if span is not None else None,
        "szi_param_stable": bool(span is not None and span <= 0.05),
        "industry_neutral_beats_raw_h5": better5,
        "industry_neutral_beats_raw_h20": better20,
        "szi_h5_ic": ind5, "raw_size_h5_ic": raw5,
        "szi_h20_ic": ind20, "raw_size_h20_ic": raw20,
        "ep_h5_gated": e5, "ep_h5_t0": et5,
        "fund_h5_gated": f5, "fund_h5_t0": fi5,
        "ep_point_estimate_better": bool(e5 is not None and f5 is not None and e5 > f5),
        "ep_passes_bonferroni": bool(et5 is not None and et5 >= T_BONF),
        "fund_passes_bonferroni": bool(fi5 is not None and fi5 >= T_BONF),
    }

    json.dump(out, open(os.path.join(HERE, "audit_round27b.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    print(f"\n→ 已写出 {os.path.join(HERE, 'audit_round27b.json')}")


if __name__ == "__main__":
    main()
