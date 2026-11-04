#!/usr/bin/env python3
"""Round18：消息面(score_news)与题材热度(score_theme)因子的系统性诊断与替代方案实证。

动机（老大 2026-10-25）：两个因子「评分区分度不足、多数标的得分趋同、
且仅科技类个股能获得高分」。本脚本用真实数据定位成因，并实证候选替代因子。

诊断假设（按可伪设计）：
  H1 趋同：离散度不足 —— 唯一值少、桶集中、封顶率高（对标 score_attention）
  H2 科技倾斜：高分通路依赖硬编码先验（THEME_DB/SUB_THEME/KNOWN_CATALYSTS），
     而硬编码条目本身科技占比高
  H3 价格污染：两个因子都内嵌「当日涨跌幅」项（news 的 price_mod /
     theme 的 day_mod 与情绪确认 C），而当日动量在本池是负 alpha
     → 因子的负 IC 可能来自污染而非「消息/题材本身」
  H4 行业代理：因子的 IC 在行业内中性化后可能消失 → 它只是行业 beta

实证纪律（沿用 round13/15/16）：
  · 所有 t 值为 Newey-West，lag 全档扫描（L0/L3/L6），禁只报最好 lag
  · Bonferroni 门槛 3.08（12 因子）
  · 小样本结论必须标注 n 与「方向性」字样
  · 候选因子必须过离散度门槛：uniq>=100 且 HHI<0.05 且封顶率<5%
"""
import json
import math
import os
import sys
from collections import Counter, defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import factor_eval as fe

T_BONF = 3.08
LAGS = [0, 3, 6]


def nw_t(xs, lag):
    n = len(xs)
    if n < 3:
        return None
    m = sum(xs) / n
    e = [x - m for x in xs]
    s = sum(v * v for v in e)
    for k in range(1, min(lag, n - 1) + 1):
        gk = sum(e[i] * e[i - k] for i in range(k, n))
        s += 2.0 * (1.0 - k / (lag + 1.0)) * gk
    if s <= 0:
        return None
    return m / math.sqrt((s / n) / n)


def dispersion(vals):
    """离散度诊断：唯一值/集中度/零值率/封顶率。"""
    vals = [v for v in vals if isinstance(v, (int, float))]
    if not vals:
        return None
    c = Counter(vals)
    hhi = sum((n / len(vals)) ** 2 for n in c.values())
    top5 = sum(n for _, n in c.most_common(5)) / len(vals)
    return {"n": len(vals), "uniq": len(c),
            "hhi": round(hhi, 4), "top5_share": round(top5, 3),
            "zero_rate": round(c.get(0, 0) / len(vals), 3),
            "cap_rate": round(c.get(5, 0) / len(vals), 3),
            "top_buckets": [[k, n] for k, n in c.most_common(6)]}


def cross_section_series(snaps, tdays, px, field_fn, hop, min_n=40):
    """逐截面算 (因子值, 未来 hop 日收益)，返回 [{date, fv, rv}]。"""
    tidx = {d: i for i, d in enumerate(tdays)}
    out = []
    for s in snaps:
        d0 = s["date"]
        if d0 not in tidx:
            continue
        i0 = tidx[d0]
        if i0 + hop >= len(tdays):
            continue
        d1 = tdays[i0 + hop]
        fv, rv = [], []
        for r in s["rows"]:
            m = px.get(r.get("code"))
            if not m:
                continue
            p0, p1 = m.get(d0), m.get(d1)
            if not p0 or not p1 or p0 <= 0:
                continue
            v = field_fn(r, s, d0)
            if not isinstance(v, (int, float)):
                continue
            fv.append(v)
            rv.append(p1 / p0 - 1.0)
        if len(fv) < min_n:
            continue
        out.append({"date": d0, "fv": fv, "rv": rv})
    return out


def ic_battery(series_by_date, label, print_fn=print):
    """IC + NW t（lag 全档）+ 五分位价差。series_by_date: [{date, fv, rv}]"""
    if len(series_by_date) < 3:
        return None
    ics = []
    q_spread = []
    for s in series_by_date:
        ic = fe.rank_ic(s["fv"], s["rv"])
        if ic is not None:
            ics.append(ic)
        sp = fe._quantile_spread(s["fv"], s["rv"], q=5)
        if sp:
            q_spread.append(sp)
    if len(ics) < 3:
        return None
    m = sum(ics) / len(ics)
    rec = {"n": len(ics), "ic": round(m, 4),
           "t_by_lag": {}, "q_spread": None}
    for L in LAGS:
        t = nw_t(ics, L)
        if t is not None:
            rec["t_by_lag"][str(L)] = round(t, 2)
    ts = [abs(v) for v in rec["t_by_lag"].values()]
    rec["robust_bonf"] = bool(ts) and all(t >= T_BONF for t in ts)
    if q_spread:
        # _quantile_spread 返回 float：按因子降序分5组，头部组均值收益 − 尾部组均值收益
        rec["q_spread_top_minus_bot"] = round(sum(q_spread) / len(q_spread), 4)
    return rec


def fmt(rec):
    if not rec:
        return "  样本不足"
    ts = rec.get("t_by_lag") or {}
    tstr = " ".join(f"L{k}:{v:+.2f}" for k, v in sorted(ts.items()))
    rob = "✅" if rec.get("robust_bonf") else "⚠️"
    qs = f" 头-尾={rec['q_spread_top_minus_bot']:+.2%}" if rec.get("q_spread_top_minus_bot") is not None else ""
    return f"  n={rec['n']:<3} IC={rec['ic']:+.4f} {tstr} {rob}{qs}"


# -----------------------------------------------------------------
# 价格库自算因子（全部 1012 只 × 143 日可复算）
# -----------------------------------------------------------------
def make_px_factors(tdays, px):
    """预计算逐日逐股的 ret1d / gap1 / rvol20。返回 {date: {code: val}}"""
    tidx = {d: i for i, d in enumerate(tdays)}
    ret1, gap1, rvol20 = defaultdict(dict), defaultdict(dict), defaultdict(dict)
    for code, series in px.items():
        # series: {date: close} 或 list？factor_eval 的 px 是 dict date->close
        n = len(series) if isinstance(series, dict) else 0
        closes = series if isinstance(series, dict) else {}
        for i, d in enumerate(tdays):
            if d not in closes:
                continue
            if i >= 1:
                dprev = tdays[i - 1]
                p0 = closes.get(dprev)
                p1 = closes[d]
                if p0 and p1 and p0 > 0:
                    ret1[d][code] = p1 / p0 - 1.0
            if i >= 20:
                win = []
                ok = True
                for j in range(i - 19, i + 1):
                    dj = tdays[j]
                    if dj not in closes:
                        ok = False
                        break
                    win.append(closes[dj])
                if ok and len(win) == 20:
                    rs = []
                    for j in range(1, 20):
                        if win[j - 1] > 0:
                            rs.append(math.log(win[j] / win[j - 1]))
                    if len(rs) >= 10:
                        mu = sum(rs) / len(rs)
                        var = sum((x - mu) ** 2 for x in rs) / (len(rs) - 1)
                        rvol20[d][code] = math.sqrt(var) * math.sqrt(244)
    return ret1, gap1, rvol20


def main():
    h = json.load(open(os.path.join(HERE, "factor_history.json"), encoding="utf-8"))
    snaps, _ = fe._drop_stale_price_snapshots(h["snapshots"])
    tdays, px = fe.px_index(h)
    opens = fe.open_index(h)
    print(f"截面数={len(snaps)}  价格日={len(tdays)} ({tdays[0]}~{tdays[-1]})  价格股数={len(px)}")

    # code -> sector 映射（来自 scored 文件，6 位码）
    import glob
    import re as _re
    sec_map = {}
    for f in glob.glob(os.path.join(HERE, "top200_scored_*.json")):
        if "_watch_" in f or ".bak" in f:
            continue
        try:
            d = json.load(open(f, encoding="utf-8"))
        except Exception:
            continue
        for r in d.get("results") or []:
            c = _re.sub(r"^(sh|sz|bj)", "", str(r.get("code") or ""))
            if c and r.get("sector"):
                sec_map[c] = r["sector"]

    ret1, _gap1, rvol20 = make_px_factors(tdays, px)
    # gap1 需要 opens
    gap1 = defaultdict(dict)
    for code, oser in opens.items():
        for d, o in oser.items():
            i = None
            # 找 d 在 tdays 的位置
            pass
    tidx = {d: i for i, d in enumerate(tdays)}
    for code, oser in opens.items():
        closes = px.get(code) or {}
        for d, o in oser.items():
            if d not in tidx or o is None:
                continue
            i = tidx[d]
            if i < 1:
                continue
            p0 = closes.get(tdays[i - 1])
            if p0 and p0 > 0:
                gap1[d][code] = o / p0 - 1.0

    out = {"generated_for": "news/theme factor diagnosis", "n_snapshots": len(snaps)}

    # ============================================================
    print()
    print("=" * 100)
    print("A. 离散度诊断（85 截面聚合，n=股票日）——H1 趋同")
    print("=" * 100)
    agg = defaultdict(list)
    for s in snaps:
        for r in s["rows"]:
            for f in ("score_news", "score_theme", "score_theme_intrinsic",
                      "score_attention", "score_sector", "score_fund"):
                v = r.get(f)
                if isinstance(v, (int, float)):
                    agg[f].append(v)
    out["dispersion"] = {}
    for f, vs in agg.items():
        dd = dispersion(vs)
        out["dispersion"][f] = dd
        print(f"  {f:<24} n={dd['n']:<6} uniq={dd['uniq']:<5} HHI={dd['hhi']:.3f} "
              f"top5桶占比={dd['top5_share']:.1%} 零值率={dd['zero_rate']:.1%} 封顶(5.0)率={dd['cap_rate']:.1%}")
    print("  参照：score_attention（成交额百分位）为满离散基准")
    print(f"  → score_theme_intrinsic 仅 {out['dispersion']['score_theme_intrinsic']['uniq']} 个取值，"
          f"是硬编码先验 THEME_DB/SUB_THEME 的直接后果")

    # ============================================================
    print()
    print("=" * 100)
    print("B. 价格污染实证 —— H3：当日动量注入 + 本池动量负 alpha")
    print("=" * 100)
    # B1 截面相关 corr(score_*, ret1d)
    for f in ("score_news", "score_theme", "score_theme_intrinsic"):
        cs = []
        for s in snaps:
            xs, ys = [], []
            for r in s["rows"]:
                v = r.get(f)
                r1 = ret1.get(s["date"], {}).get(r.get("code"))
                if isinstance(v, (int, float)) and r1 is not None:
                    xs.append(v)
                    ys.append(r1)
            if len(xs) >= 40:
                c = fe._pearson(xs, ys)
                if c is not None:
                    cs.append(c)
        if cs:
            m = sum(cs) / len(cs)
            pos = sum(1 for c in cs if c > 0) / len(cs)
            out.setdefault("contamination_corr", {})[f] = {
                "mean_corr_with_ret1d": round(m, 3), "share_positive": round(pos, 3), "n": len(cs)}
            print(f"  corr({f}, ret1d) 截面均值={m:+.3f}  正相关截面占比={pos:.0%} (n={len(cs)})")

    # B2 ret1d 自身作为因子（污染源本体）
    f_ret1 = lambda r, s, d0: ret1.get(d0, {}).get(r.get("code"))
    for hop in (1, 5, 20):
        rec = ic_battery(cross_section_series(snaps, tdays, px, f_ret1, hop), "ret1d")
        out.setdefault("ic_ret1d", {})[str(hop)] = rec
        print(f"  ret1d（当日收益，污染源本体） h{hop}:{fmt(rec)}")

    # B3 基线复现：score_news / score_theme
    for f in ("score_news", "score_theme"):
        out.setdefault("ic_baseline", {})[f] = {}
        for hop in (1, 5, 20):
            fn = (lambda ff: (lambda r, s, d0: r.get(ff)))(f)
            rec = ic_battery(cross_section_series(snaps, tdays, px, fn, hop), f)
            out["ic_baseline"][f][str(hop)] = rec
            print(f"  {f} h{hop}:{fmt(rec)}")

    # B4 正交化残差：控制 ret1d 后的因子（剥离污染的标准做法）
    print("  --- 控制 ret1d 后的残差因子（截面内 OLS 正交化）---")
    for f in ("score_news", "score_theme"):
        out.setdefault("ic_resid", {})[f] = {}
        for hop in (1, 5, 20):
            tidx2 = {d: i for i, d in enumerate(tdays)}
            ser2 = []
            for s in snaps:
                d0 = s["date"]
                if d0 not in tidx2:
                    continue
                i0 = tidx2[d0]
                if i0 + hop >= len(tdays):
                    continue
                d1 = tdays[i0 + hop]
                xs, ys, rr = [], [], []
                for r in s["rows"]:
                    m = px.get(r.get("code"))
                    if not m:
                        continue
                    p0, p1 = m.get(d0), m.get(d1)
                    if not p0 or not p1 or p0 <= 0:
                        continue
                    v = r.get(f)
                    r1 = ret1.get(d0, {}).get(r.get("code"))
                    if not isinstance(v, (int, float)) or r1 is None:
                        continue
                    xs.append(v)
                    ys.append(r1)
                    rr.append(p1 / p0 - 1.0)
                if len(xs) < 40:
                    continue
                mx, my = sum(xs) / len(xs), sum(ys) / len(ys)
                sxx = sum((x - mx) ** 2 for x in xs)
                if sxx <= 0:
                    continue
                beta = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / sxx
                resid = [x - beta * (y - my) for x, y in zip(xs, ys)]
                ser2.append({"date": d0, "fv": resid, "rv": rr})
            rec = ic_battery(ser2, f)
            out["ic_resid"][f][str(hop)] = rec
            print(f"  {f} | ret1d 正交化残差 h{hop}:{fmt(rec)}")

    # ============================================================
    print()
    print("=" * 100)
    print("C. 行业内中性化 IC —— H4：因子是否只是行业代理")
    print("=" * 100)
    cov = len(sec_map)
    print(f"  code->sector 映射 {cov} 只（来自 scored 文件）")
    for f in ("score_news", "score_theme", "score_fund"):
        ser_all = []
        for hop in (1, 20):
            ser = cross_section_series(snaps, tdays, px,
                                       (lambda ff: (lambda r, s, d0: r.get(ff)))(f), hop)
            # 行业内 rank（按 sector 分组内百分位）
            ser_ind = []
            for s in ser:
                groups = defaultdict(list)
                idx_by_code = {}
                for i, r in enumerate(s["_rows"] if "_rows" in s else []):
                    pass
                ser_ind.append(s)
            # 简化：用 sec_map 对截面行分组需要 code，cross_section_series 没带回 code
            # → 这里重建带 code 的序列
            ser2 = []
            tidx2 = {d: i for i, d in enumerate(tdays)}
            for s in snaps:
                d0 = s["date"]
                if d0 not in tidx2:
                    continue
                i0 = tidx2[d0]
                if i0 + hop >= len(tdays):
                    continue
                d1 = tdays[i0 + hop]
                groups = defaultdict(list)
                for r in s["rows"]:
                    m = px.get(r.get("code"))
                    if not m:
                        continue
                    p0, p1 = m.get(d0), m.get(d1)
                    if not p0 or not p1 or p0 <= 0:
                        continue
                    v = r.get(f)
                    if not isinstance(v, (int, float)):
                        continue
                    groups[sec_map.get(r.get("code"), "?")].append((v, p1 / p0 - 1.0))
                if sum(len(g) for g in groups.values()) < 40:
                    continue
                fv, rv = [], []
                for gname, pairs in groups.items():
                    if len(pairs) < 3:
                        continue
                    vals = sorted(p[0] for p in pairs)
                    n = len(vals)
                    for v, rr in pairs:
                        # 组内百分位 rank
                        below = sum(1 for x in vals if x < v)
                        fv.append(below / max(1, n - 1))
                        rv.append(rr)
                if len(fv) >= 30:
                    ser2.append({"date": d0, "fv": fv, "rv": rv})
            rec = ic_battery(ser2, f)
            out.setdefault("ic_industry_neutral", {})[f] = out.get("ic_industry_neutral", {}).get(f, {}) or {}
            out["ic_industry_neutral"][f][str(hop)] = rec
            print(f"  {f} 行业内中性化 h{hop}:{fmt(rec)}")

    # ============================================================
    print()
    print("=" * 100)
    print("D. 候选替代因子实证（全部可由现有数据复算）")
    print("=" * 100)
    cands = {}
    # D1 gap1 隔夜跳空（消息在非交易时段的价格印记）
    f_gap = lambda r, s, d0: gap1.get(d0, {}).get(r.get("code"))
    cands["gap1（隔夜跳开）"] = f_gap
    # D2 rvol20 已实现波动
    f_rvol = lambda r, s, d0: rvol20.get(d0, {}).get(r.get("code"))
    cands["rvol20（20日实现波动）"] = f_rvol
    # D3 theme_delta5：题材热度 5 日变化（拥挤度边际）
    def f_theme_delta(r, s, d0):
        i = tidx.get(d0)
        if i is None or i < 5:
            return None
        dprev = tdays[i - 5]
        snap_prev = None
        for ss in snaps:
            if ss["date"] == dprev:
                snap_prev = ss
                break
        if snap_prev is None:
            return None
        cur = r.get("score_theme")
        prev = None
        for rr in snap_prev["rows"]:
            if rr.get("code") == r.get("code"):
                prev = rr.get("score_theme")
                break
        if isinstance(cur, (int, float)) and isinstance(prev, (int, float)):
            return cur - prev
        return None
    cands["theme_delta5（题材热度5日变化）"] = f_theme_delta
    # D4 attn_delta5：关注度 5 日变化
    def f_attn_delta(r, s, d0):
        i = tidx.get(d0)
        if i is None or i < 5:
            return None
        dprev = tdays[i - 5]
        snap_prev = None
        for ss in snaps:
            if ss["date"] == dprev:
                snap_prev = ss
                break
        if snap_prev is None:
            return None
        cur = r.get("score_attention")
        prev = None
        for rr in snap_prev["rows"]:
            if rr.get("code") == r.get("code"):
                prev = rr.get("score_attention")
                break
        if isinstance(cur, (int, float)) and isinstance(prev, (int, float)):
            return cur - prev
        return None
    cands["attn_delta5（关注度5日变化）"] = f_attn_delta
    # D5 theme_ind_neutral：行业内中性化后的 theme 值（水平）
    def f_theme_ind(r, s, d0):
        v = r.get("score_theme")
        if not isinstance(v, (int, float)):
            return None
        g = sec_map.get(r.get("code"))
        return (v, g)
    # 行业内中性 theme 需要截面级处理
    ser_ind_theme = []
    for hop in (1, 20):
        ser2 = []
        for s in snaps:
            d0 = s["date"]
            if d0 not in tidx:
                continue
            i0 = tidx[d0]
            if i0 + hop >= len(tdays):
                continue
            d1 = tdays[i0 + hop]
            groups = defaultdict(list)
            for r in s["rows"]:
                m = px.get(r.get("code"))
                if not m:
                    continue
                p0, p1 = m.get(d0), m.get(d1)
                if not p0 or not p1 or p0 <= 0:
                    continue
                v = r.get("score_theme")
                if not isinstance(v, (int, float)):
                    continue
                groups[sec_map.get(r.get("code"), "?")].append((v, p1 / p0 - 1.0))
            if sum(len(g) for g in groups.values()) < 40:
                continue
            fv, rv = [], []
            for gname, pairs in groups.items():
                if len(pairs) < 3:
                    continue
                mu = sum(p[0] for p in pairs) / len(pairs)
                for v, rr in pairs:
                    fv.append(v - mu)
                    rv.append(rr)
            if len(fv) >= 30:
                ser2.append({"date": d0, "fv": fv, "rv": rv})
        rec = ic_battery(ser2, "theme_ind_neutral")
        out.setdefault("candidates", {})["theme_ind_neutral"] = out.get("candidates", {}).get("theme_ind_neutral", {}) or {}
        out["candidates"]["theme_ind_neutral"][str(hop)] = rec
        print(f"  theme_ind_neutral（行业内中性 theme 水平） h{hop}:{fmt(rec)}")

    for name, fn in cands.items():
        out["candidates"][name] = {}
        for hop in (1, 5, 20):
            ser = cross_section_series(snaps, tdays, px, fn, hop)
            rec = ic_battery(ser, name)
            out["candidates"][name][str(hop)] = rec
            print(f"  {name} h{hop}:{fmt(rec)}")
        # 离散度门槛
        vals = []
        for s in snaps:
            for r in s["rows"]:
                v = fn(r, s, s["date"])
                if isinstance(v, (int, float)):
                    vals.append(v)
        if vals:
            dd = dispersion(vals)
            out.setdefault("candidate_dispersion", {})[name] = dd
            gate = "✅过" if (dd["uniq"] >= 100 and dd["hhi"] < 0.05 and dd["cap_rate"] < 0.05) else "❌不过"
            print(f"    离散度门槛: uniq={dd['uniq']} HHI={dd['hhi']:.3f} 封顶率={dd['cap_rate']:.1%} → {gate}")

    # ============================================================
    print()
    print("=" * 100)
    print("E. 硬编码先验的行业构成 —— H2 科技倾斜的结构性证据")
    print("=" * 100)
    # E1 KNOWN_CATALYSTS 条数与层级分布
    src = open(os.path.join(HERE, "step2_stockscorer_v2.py"), encoding="utf-8").read()
    import re as _re2
    m = _re2.search(r"KNOWN_CATALYSTS = \{(.*?)\n\}\n", src, _re2.S)
    n_cat = len(_re2.findall(r'"(sh\d+|sz\d+|bj\d+)":', m.group(1))) if m else 0
    levels = Counter(int(x) for x in _re2.findall(r'"level":\s*(\d)', m.group(1))) if m else Counter()
    print(f"  KNOWN_CATALYSTS 硬编码 {n_cat} 条（覆盖 10-23 截面 490 只中的一部分），level 分布={dict(sorted(levels.items()))}")
    out["known_catalysts"] = {"n": n_cat, "level_dist": {str(k): v for k, v in sorted(levels.items())}}

    # E2 THEME_DB 先验分的行业分布
    m2 = _re2.search(r"THEME_DB = \{(.*?)\n\}\n", src, _re2.S)
    td_scores = _re2.findall(r'"([^"]+)":\s*\{\s*\n\s*"score":\s*(\d)', m2.group(1)) if m2 else []
    hi_themes = [k for k, v in td_scores if int(v) >= 4]
    lo_themes = [k for k, v in td_scores if int(v) <= 2]
    print(f"  THEME_DB 先验>=4 的板块: {hi_themes}")
    print(f"  THEME_DB 先验<=2 的板块: {lo_themes}")
    out["theme_db"] = {"ge4": hi_themes, "le2": lo_themes}

    # E3 AI 覆盖层的行业构成（ai_assessment.json）
    ai_p = os.path.join(HERE, "ai_assessment.json")
    if os.path.exists(ai_p):
        ai = json.load(open(ai_p, encoding="utf-8"))
        ths = ai.get("themes") or {}
        tot_stocks = sum(len(t.get("stocks") or []) for t in ths.values())
        print(f"  AI 题材层: {len(ths)} 个主题覆盖 {tot_stocks} 只，news/heat 取值:")
        for k, t in ths.items():
            print(f"    {k:<16} news={t.get('news')} heat={t.get('heat')} stocks={len(t.get('stocks') or [])}")
        out["ai_themes"] = {k: {"news": t.get("news"), "heat": t.get("heat"),
                                "n_stocks": len(t.get("stocks") or [])} for k, t in ths.items()}

    # E4 最新截面：高分组的行业构成
    d23 = json.load(open(os.path.join(HERE, "top200_scored_2026-10-23.json"), encoding="utf-8"))
    rs23 = d23["results"]
    hi_news = [r for r in rs23 if (r.get("score_news") or 0) >= 4.5]
    lo_news = [r for r in rs23 if (r.get("score_news") or 0) <= 1.0]
    sec_count = Counter(r.get("sector") for r in rs23)
    def sec_profile(grp, name):
        c = Counter(r.get("sector") for r in grp)
        top = ", ".join(f"{k}:{v}" for k, v in c.most_common(5))
        print(f"  {name} (n={len(grp)}): {top}")
        return {"n": len(grp), "top_sectors": [[k, v] for k, v in c.most_common(8)]}
    out["latest_xsec"] = {
        "news_hi": sec_profile(hi_news, "score_news>=4.5"),
        "news_lo": sec_profile(lo_news, "score_news<=1.0"),
        "pool_sec_dist": [[k, v] for k, v in sec_count.most_common()],
    }
    ai_cov = sum(1 for r in rs23 if r.get("ai_applied"))
    out["latest_xsec"]["ai_coverage"] = {"applied": ai_cov, "total": len(rs23)}
    print(f"  AI 覆盖层实际赋权个股: {ai_cov}/{len(rs23)} = {ai_cov/len(rs23):.0%}（其余保持 step2 基础分）")

    # E5 消息面覆盖失败量化：近7日无消息 ≠ 无事件
    rec = Counter(r.get("news_recency") for r in rs23)
    n_no_news = rec.get("近7日无消息", 0)
    n_zero_items = sum(1 for r in rs23 if not (r.get("news_items") or []))
    print(f"  「近7日无消息」{n_no_news} 只、news_items 为 0 条 {n_zero_items} 只 —— "
          f"这些得 0 分，占 0.16 权重被系统性扣分")
    out["latest_xsec"]["news_coverage_failure"] = {
        "no_news_7d": n_no_news, "zero_items": n_zero_items, "total": len(rs23)}

    # E6 AI 层混合毁粒度：主题均值 0.7 权重把个股分拉到主题水平
    ai_rows = [r for r in rs23 if r.get("ai_applied")]
    if ai_rows:
        # 反推 base = (ai_score - 0.7*t_score)/0.3 不可行（t_score 未逐股留痕），
        # 改用可观测证据：同一主题内个股分的标准差 vs 未覆盖组
        by_label = defaultdict(list)
        for r in ai_rows:
            by_label[r.get("ai_theme_label")].append(r["score_news"])
        spreads = {k: (len(v), round(sum(v) / len(v), 2),
                       round((sum((x - sum(v) / len(v)) ** 2 for x in v) / len(v)) ** 0.5, 2))
                   for k, v in by_label.items() if len(v) >= 5}
        print("  AI 主题内 score_news 的组内标准差（(n, 均值, sd)）：")
        for k, v in spreads.items():
            print(f"    {k:<16} n={v[0]:<3} mean={v[1]:<6} sd={v[2]}")
        out["latest_xsec"]["ai_within_theme_sd"] = {k: v for k, v in spreads.items()}

    # ============================================================
    print()
    print("=" * 100)
    print("F. 预期差方向性检验（4 个截面的结构化新闻数据，小样本→只作方向性证据）")
    print("=" * 100)
    # 用 news_items 的 sentiment 计利好/利空条数，与当日涨跌幅交叉
    scored_files = [f for f in sorted(glob.glob(os.path.join(HERE, "top200_scored_*.json")))
                    if "_watch_" not in f and ".bak" not in f]
    pooled = []
    for f in scored_files:
        d = json.load(open(f, encoding="utf-8"))
        dt = d.get("date")
        if dt not in tidx:
            continue
        i0 = tidx[dt]
        if i0 + 5 >= len(tdays):
            continue
        d1 = tdays[i0 + 5]
        for r in d.get("results") or []:
            code6 = _re.sub(r"^(sh|sz|bj)", "", str(r.get("code") or ""))
            m = px.get(code6)
            if not m:
                continue
            p0, p1 = m.get(dt), m.get(d1)
            if not p0 or not p1 or p0 <= 0:
                continue
            items = r.get("news_items") or []
            n_bull = sum(1 for x in items if "利好" in str(x.get("sentiment", "")) or "机构关注" in str(x.get("sentiment", "")))
            n_bear = sum(1 for x in items if "利空" in str(x.get("sentiment", "")))
            pct = r.get("pct_chg")
            if not isinstance(pct, (int, float)):
                continue
            pooled.append({"date": dt, "code": code6, "n_bull": n_bull, "n_bear": n_bear,
                           "pct": pct, "fwd5": (p1 / p0 - 1.0) * 100,
                           "score_news": r.get("score_news")})
    print(f"  可配对样本: {len(pooled)} 股票日（来自 {len(scored_files)} 个 scored 截面）")
    if pooled:
        # 分组1: 有利好事件的股票，按当日涨幅分三档，看未来5日收益
        bull = [p for p in pooled if p["n_bull"] >= 1]
        grp_hi = [p for p in bull if p["pct"] >= 5]
        grp_mid = [p for p in bull if -3 <= p["pct"] < 5]
        grp_lo = [p for p in bull if p["pct"] < -3]
        def _m(g):
            return (len(g), round(sum(x["fwd5"] for x in g) / len(g), 2)) if g else (0, None)
        print("  有>=1条利好事件的股票，按当日涨跌幅分组（预期差检验）：")
        print(f"    当日大涨(>=+5%): n={_m(grp_hi)[0]:<4} 未来5日均值={_m(grp_hi)[1]}%  ← 已反应")
        print(f"    当日平盘(-3~+5%): n={_m(grp_mid)[0]:<4} 未来5日均值={_m(grp_mid)[1]}%  ← 未反应")
        print(f"    当日下跌(<-3%):  n={_m(grp_lo)[0]:<4} 未来5日均值={_m(grp_lo)[1]}%")
        nob = [p for p in pooled if p["n_bull"] == 0]
        print(f"    无利好事件:      n={_m(nob)[0]:<4} 未来5日均值={_m(nob)[1]}%")
        out["expectation_gap"] = {
            "n_pooled": len(pooled), "n_files": len(scored_files),
            "bull_hi": _m(grp_hi), "bull_mid": _m(grp_mid), "bull_lo": _m(grp_lo),
            "no_bull": _m(nob),
            "note": "小样本方向性证据，n 为股票日而非独立截面，不能做显著性断言"}

    with open(os.path.join(HERE, "audit_round18.json"), "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print("\n✅ 已写 audit_round18.json")


if __name__ == "__main__":
    main()
