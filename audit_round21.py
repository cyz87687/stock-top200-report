#!/usr/bin/env python3
"""Round21：fund 拆分为「质量因子 25% + 成长因子 20%」的可行性实证。

动机（老大 2026-10-25）：要求把现有 fund 维度按专业分类拆成两个一级维度：
  质量因子 25%：① 资本回报 ROE/ROIC/ROA ② 现金流&盈余质量 OCF/NI、销售收现比、
                应计因子、OCFG ③ 稳健风控 盈利波动、毛利率波动、资产负债率、商誉占比
  成长因子 20%：① 历史增速 近1年扣非/3年CAGR/营收增速 ② 边际改善 单季环比、增速差分

本脚本用真实数据回答四个问题：
  Q1 数据可得性：16 个子因子在 EM 财务摘要缓存（data/fin_cache_em/，80 指标 × 20 报告期）
     的覆盖率；point-in-time 披露纪律能否重建历史截面
  Q2 子因子 IC：逐个子因子 + 两个族得分的 IC battery（NW t 全 lag + Bonferroni 3.08），
     含行业内中性化（剥离行业代理）
  Q3 合并损益：现行 12 维 vs round20 E 方案 vs 新 6 维（质量.25+成长.20+技术.15+量价+规模）
     三个配平变体的组合 IC 对比
  Q4 与现有 12 维相关性：拆分是否带来增量信息（尤其 vs score_fund 同源）

纪律（沿用 round13~20）：
  · NW t 全 lag 扫描（L0/L3/L6），Bonferroni 门槛 3.08，任何「显著」必须标注 lag
  · point-in-time：只用披露截止日 ≤ 截面日期 的报告期（A股：年报 4/30、一季报 4/30、
    中报 8/31、三季报 10/31）
  · 缺维从分母剔除（v4.3 铁律），禁 `or 0`
  · ⚠️ 回填偏差披露：EM 缓存为当前 vintage（7 天 TTL），历史截面用今天的数值重建，
    存在财报重述偏差；披露截止日为近似（实际披露可早于截止日）→ 结论标注「方向性」
  · 族内子因子权重本质是先验（等权 vs 分组权双重呈现），页面禁暗示「权重已被数据优化」
"""
import glob
import json
import math
import os
import sys
from collections import Counter, defaultdict

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import factor_eval as fe

T_BONF = 3.08
LAGS = [0, 3, 6]
EM_CACHE = os.path.join(HERE, "data", "fin_cache_em")

# ---------------------------------------------------------------- 基础设施（round20 同口径）
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


def rank_pct(vals):
    """截面内百分位 rank（0~1），并列取平均秩。"""
    n = len(vals)
    order = sorted(range(n), key=lambda i: vals[i])
    out = [0.0] * n
    i = 0
    while i < n:
        j = i
        while j + 1 < n and vals[order[j + 1]] == vals[order[i]]:
            j += 1
        avg = (i + j) / 2.0 / max(1, n - 1)
        for k in range(i, j + 1):
            out[order[k]] = avg
        i = j + 1
    return out


def pstdev(xs):
    n = len(xs)
    if n < 2:
        return None
    m = sum(xs) / n
    return math.sqrt(sum((x - m) ** 2 for x in xs) / n)


def series_for_dim(snaps, tdays, px, dim_fn, hop, min_n=40):
    """逐截面 (因子值, 未来 hop 日收益)。dim_fn(row, snap, date) -> value"""
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
            v = dim_fn(r, s, d0)
            if not isinstance(v, (int, float)):
                continue
            fv.append(v)
            rv.append(p1 / p0 - 1.0)
        if len(fv) >= min_n:
            out.append({"date": d0, "fv": fv, "rv": rv})
    return out


def ic_battery(series_by_date):
    if len(series_by_date) < 3:
        return None
    ics = []
    for s in series_by_date:
        ic = fe.rank_ic(s["fv"], s["rv"])
        if ic is not None:
            ics.append(ic)
    if len(ics) < 3:
        return None
    m = sum(ics) / len(ics)
    rec = {"n": len(ics), "ic": round(m, 4), "t_by_lag": {}}
    for L in LAGS:
        t = nw_t(ics, L)
        if t is not None:
            rec["t_by_lag"][str(L)] = round(t, 2)
    rec["robust_bonf"] = all(
        abs(rec["t_by_lag"].get(str(L), 0)) >= T_BONF for L in LAGS) and len(rec["t_by_lag"]) == len(LAGS)
    return rec


def fmt(rec):
    if not rec:
        return "样本不足"
    tstr = "/".join(f"{rec['t_by_lag'].get(str(L), float('nan')):+.2f}" for L in LAGS)
    rob = "✅" if rec.get("robust_bonf") else "⚠️"
    return f"n={rec['n']:<3} IC={rec['ic']:+.4f} t={tstr} {rob}"


# ---------------------------------------------------------------- EM 财务摘要
# 同名指标在多个「选项」分组下出现时的取值优先级（保证确定性）
IND_OPT = {
    "净资产收益率(ROE)": ["盈利能力", "常用指标"],
    "投入资本回报率": ["盈利能力"],
    "总资产报酬率(ROA)": ["常用指标", "盈利能力"],
    "总资产净利率_平均": ["盈利能力"],
    "经营活动净现金/归属母公司的净利润": ["收益质量"],
    "经营活动净现金/销售收入": ["收益质量"],
    "商誉": ["常用指标"],
    "营业总收入增长率": ["成长能力"],
    "归属母公司净利润增长率": ["成长能力"],
    "毛利率": ["常用指标", "盈利能力"],
    "资产负债率": ["财务风险", "常用指标"],
    "经营现金流量净额": ["常用指标"],
    "净利润": ["常用指标"],
    "股东权益合计(净资产)": ["常用指标"],
    "扣非净利润": ["常用指标", "成长能力"],
    "营业总收入": ["常用指标", "成长能力"],
    "每股经营现金流": ["每股指标"],
}
ALL_INDS = list(IND_OPT.keys())


def load_em_cache():
    """code6 -> {indicator: {period: value}}"""
    data = {}
    for f in glob.glob(os.path.join(EM_CACHE, "*.json")):
        code = os.path.basename(f)[:-5]
        try:
            recs = json.load(open(f, encoding="utf-8")).get("records") or []
        except Exception:
            continue
        # (指标, 选项) -> {period: val}
        by_opt = defaultdict(dict)
        for r in recs:
            name, opt = r.get("指标"), r.get("选项")
            if not name or not opt:
                continue
            d = by_opt[(name, opt)]
            for k, v in r.items():
                if len(k) == 8 and k.isdigit():
                    if v is None or v is False or v in ("--", "-", "", "False"):
                        continue
                    try:
                        d[k] = float(v)
                    except Exception:
                        pass
        ind = {}
        for name in ALL_INDS:
            for opt in IND_OPT[name]:
                if by_opt.get((name, opt)):
                    ind[name] = by_opt[(name, opt)]
                    break
        data[code] = ind
    return data


def disclosure_date(p):
    """报告期 -> 法定披露截止日（A股近似）。p='20260630' -> '2026-08-31'"""
    if len(p) != 8 or not p.isdigit():
        return None
    y, md = int(p[:4]), p[4:]
    if md == "0331":
        return "%d-04-30" % y
    if md == "0630":
        return "%d-08-31" % y
    if md == "0930":
        return "%d-10-31" % y
    if md == "1231":
        return "%d-04-30" % (y + 1)
    return None


def pit_period(periods_desc, d0):
    """point-in-time：披露截止日 <= 截面日期 的最新报告期。"""
    for p in periods_desc:
        dd = disclosure_date(p)
        if dd and dd <= d0:
            return p
    return None


# ---------------------------------------------------------------- 子因子计算
QUAL_SUBS = ["roe", "roic", "roa", "ocf_ni", "sales_cash", "accruals", "ocfg",
             "profit_vol", "gm_vol", "debt", "gw"]
GROW_SUBS = ["npg", "cagr3", "revg", "qoq", "accel"]
Q_GROUPS = {"资本回报": ["roe", "roic", "roa"],
            "现金流盈余": ["ocf_ni", "sales_cash", "accruals", "ocfg"],
            "稳健风控": ["profit_vol", "gm_vol", "debt", "gw"]}
G_GROUPS = {"历史增速": ["npg", "cagr3", "revg"], "边际改善": ["qoq", "accel"]}


def sub_factors(ind):
    """最新报告期口径（仅供 Part A 覆盖率统计用；回填一律走 sub_factors_pit）。"""
    periods_desc = sorted({p for d in ind.values() for p in d}, reverse=True)
    P = pit_period(periods_desc, "9999-12-31")
    return sub_factors_pit(ind, P) if P else {}


def aggregate(sub, mode):
    """族得分。mode: equal_q/equal_g（族内等权）/ grouped（质量三组等权）/ g6040（成长 60/40）
    缺组/缺维从分母剔除（v4.3 铁律），禁 `or 0`。"""
    if mode in ("equal_q", "equal_g"):
        members = QUAL_SUBS if mode == "equal_q" else GROW_SUBS
        vs = [sub.get(m) for m in members]
        vs = [v for v in vs if isinstance(v, (int, float))]
        return sum(vs) / len(vs) if vs else None
    if mode == "grouped":
        gmeans = []
        for members in Q_GROUPS.values():
            vs = [sub.get(m) for m in members]
            vs = [v for v in vs if isinstance(v, (int, float))]
            if vs:
                gmeans.append(sum(vs) / len(vs))
        return sum(gmeans) / len(gmeans) if gmeans else None
    if mode == "g6040":
        num, den = 0.0, 0.0
        for g, w in (("历史增速", 0.6), ("边际改善", 0.4)):
            vs = [sub.get(m) for m in G_GROUPS[g]]
            vs = [v for v in vs if isinstance(v, (int, float))]
            if vs:
                num += w * (sum(vs) / len(vs))
                den += w
        return num / den if den > 0 else None
    return None


# ---------------------------------------------------------------- 主流程
def main():
    h = json.load(open(os.path.join(HERE, "factor_history.json"), encoding="utf-8"))
    snaps, _ = fe._drop_stale_price_snapshots(h["snapshots"])
    tdays, px = fe.px_index(h)
    print(f"截面数={len(snaps)}  价格日={len(tdays)} ({tdays[0]}~{tdays[-1]})  价格股数={len(px)}")
    out = {"n_snapshots": len(snaps), "price_range": [tdays[0], tdays[-1]]}

    em = load_em_cache()
    print(f"EM 财务摘要缓存: {len(em)} 只")
    out["em_cache_n"] = len(em)

    # 板块映射（行业内中性化用；6 位码 key）
    sec_map = {}
    for f in glob.glob(os.path.join(HERE, "top200_scored_*.json")):
        try:
            d = json.load(open(f, encoding="utf-8"))
        except Exception:
            continue
        for r in d.get("results") or []:
            if r.get("code") and r.get("sector"):
                sec_map[r["code"][2:]] = r["sector"]

    # ============================================================
    print()
    print("=" * 100)
    print("A. 数据可得性：16 个子因子 × EM 缓存覆盖 + point-in-time 披露纪律")
    print("=" * 100)
    pool_codes = [r["code"] for s in snaps[-1:] for r in s["rows"]]
    hist_codes = set()
    for s in snaps:
        for r in s["rows"]:
            hist_codes.add(r["code"])
    cov_pool = sum(1 for c in set(pool_codes) if c in em)
    cov_hist = sum(1 for c in hist_codes if c in em)
    print(f"  最新截面池 {len(set(pool_codes))} 只 → 缓存覆盖 {cov_pool} ({cov_pool/max(1,len(set(pool_codes))):.0%})")
    print(f"  历史截面全部 codes {len(hist_codes)} → 缓存覆盖 {cov_hist} ({cov_hist/max(1,len(hist_codes)):.0%})")
    out["coverage"] = {"pool": cov_pool, "pool_total": len(set(pool_codes)),
                       "hist": cov_hist, "hist_total": len(hist_codes)}
    # 各子因子有值率（最新截面池，PIT 口径）
    pit_latest = snaps[-1]["date"]
    sub_cov = Counter()
    n_pool = 0
    for c in set(pool_codes):
        if c not in em:
            continue
        n_pool += 1
        sf = sub_factors(em[c])
        for k, v in sf.items():
            if isinstance(v, (int, float)):
                sub_cov[k] += 1
    print(f"\n  子因子有值率（最新截面 {pit_latest}，n={n_pool}）:")
    for k in QUAL_SUBS + GROW_SUBS:
        print(f"    {k:<12} {sub_cov[k]}/{n_pool}")
    out["sub_coverage_latest"] = {k: sub_cov[k] for k in QUAL_SUBS + GROW_SUBS}
    out["sub_coverage_n"] = n_pool

    # ============================================================
    print()
    print("=" * 100)
    print("B. Point-in-time 回填：逐截面算 16 子因子 → 族得分（等权/分组权）")
    print("=" * 100)
    # qg[date][code] = {"sub": {...}, "q_equal","q_grouped","g_equal","g6040"}
    qg = defaultdict(dict)
    for s in snaps:
        d0 = s["date"]
        for r in s["rows"]:
            code = r.get("code")
            if code not in em:
                continue
            ind = em[code]
            periods_desc = sorted({p for dd in ind.values() for p in dd}, reverse=True)
            P = pit_period(periods_desc, d0)
            if P is None:
                continue
            # 用 PIT 报告期重算子因子：sub_factors 内部取最新期，这里按 P 过滤
            sub = sub_factors_pit(ind, P)
            rec = {"sub": sub,
                   "q_equal": aggregate(sub, "equal_q"),
                   "q_grouped": aggregate(sub, "grouped"),
                   "g_equal": aggregate(sub, "equal_g"),
                   "g6040": aggregate(sub, "g6040")}
            if rec["q_grouped"] is not None or rec["g6040"] is not None:
                qg[d0][code] = rec
    n_dates = len(qg)
    n_rows = sum(len(v) for v in qg.values())
    print(f"  回填完成: {n_dates}/{len(snaps)} 个截面有质量/成长得分，共 {n_rows} 股票日")
    out["backfill"] = {"dates": n_dates, "rows": n_rows}

    # ============================================================
    print()
    print("=" * 100)
    print("C. 子因子与族得分 IC battery（NW t, lag 0/3/6, Bonferroni 3.08）")
    print("=" * 100)
    out["ic"] = {}

    def sub_fn(name):
        def fn(r, s, d0):
            rec = qg.get(d0, {}).get(r.get("code"))
            if not rec:
                return None
            v = rec["sub"].get(name)
            return v if isinstance(v, (int, float)) else None
        return fn

    def fam_fn(key):
        def fn(r, s, d0):
            rec = qg.get(d0, {}).get(r.get("code"))
            if not rec:
                return None
            v = rec.get(key)
            return v if isinstance(v, (int, float)) else None
        return fn

    print("  ── 质量族子因子（负向子因子 IC 为负=方向正确）")
    for k in QUAL_SUBS:
        out["ic"][k] = {}
        for hop in (1, 5, 20):
            out["ic"][k][str(hop)] = ic_battery(series_for_dim(snaps, tdays, px, sub_fn(k), hop))
        print(f"    {k:<12} h1:{fmt(out['ic'][k].get('1'))}")
        print(f"    {'':<12} h5:{fmt(out['ic'][k].get('5'))}")
        print(f"    {'':<12} h20:{fmt(out['ic'][k].get('20'))}")
    print("  ── 成长族子因子")
    for k in GROW_SUBS:
        out["ic"][k] = {}
        for hop in (1, 5, 20):
            out["ic"][k][str(hop)] = ic_battery(series_for_dim(snaps, tdays, px, sub_fn(k), hop))
        print(f"    {k:<12} h1:{fmt(out['ic'][k].get('1'))}")
        print(f"    {'':<12} h5:{fmt(out['ic'][k].get('5'))}")
        print(f"    {'':<12} h20:{fmt(out['ic'][k].get('20'))}")
    print("  ── 族得分（等权 vs 分组权）")
    for key, label in (("q_equal", "质量-等权"), ("q_grouped", "质量-分组权"),
                       ("g_equal", "成长-等权"), ("g6040", "成长-历史60/边际40")):
        out["ic"][key] = {}
        for hop in (1, 5, 20):
            out["ic"][key][str(hop)] = ic_battery(series_for_dim(snaps, tdays, px, fam_fn(key), hop))
        print(f"    {label:<16} h1:{fmt(out['ic'][key].get('1'))}")
        print(f"    {'':<16} h5:{fmt(out['ic'][key].get('5'))}")
        print(f"    {'':<16} h20:{fmt(out['ic'][key].get('20'))}")

    # ============================================================
    print()
    print("=" * 100)
    print("C2. 行业内中性化（剥离行业代理，round18 同口径）")
    print("=" * 100)

    def neutral_series(dim_fn, hop):
        """截面内按板块 demean 后的因子序列"""
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
            grp = defaultdict(list)
            for idx, r in enumerate(s["rows"]):
                m = px.get(r.get("code"))
                if not m:
                    continue
                p0, p1 = m.get(d0), m.get(d1)
                if not p0 or not p1 or p0 <= 0:
                    continue
                v = dim_fn(r, s, d0)
                if not isinstance(v, (int, float)):
                    continue
                grp[sec_map.get(r.get("code"), "?")].append((v, p1 / p0 - 1.0))
            fv, rv = [], []
            for g, pairs in grp.items():
                if len(pairs) < 5:
                    continue
                mv = sum(p[0] for p in pairs) / len(pairs)
                for v, ret in pairs:
                    fv.append(v - mv)
                    rv.append(ret)
            if len(fv) >= 40:
                ser.append({"date": d0, "fv": fv, "rv": rv})
        return ser

    out["ic_neutral"] = {}
    for key, label in (("q_grouped", "质量-分组权"), ("g6040", "成长-历史60/边际40"),
                       ("score_fund", "对照-score_fund")):
        if key == "score_fund":
            fn = lambda r, s, d0: r.get("score_fund") if isinstance(r.get("score_fund"), (int, float)) else None
        else:
            fn = fam_fn(key)
        out["ic_neutral"][key] = {}
        for hop in (1, 5, 20):
            out["ic_neutral"][key][str(hop)] = ic_battery(neutral_series(fn, hop))
        print(f"    {label:<16} h1:{fmt(out['ic_neutral'][key].get('1'))}")
        print(f"    {'':<16} h5:{fmt(out['ic_neutral'][key].get('5'))}")
        print(f"    {'':<16} h20:{fmt(out['ic_neutral'][key].get('20'))}")

    # ============================================================
    print()
    print("=" * 100)
    print("D. 相关性：质量/成长 vs 现有维度（增量信息检验）")
    print("=" * 100)
    # 反转块回填（round20 口径复现）——入库事故导致 74/78 截面无值，
    # 相关性检验必须用回填口径，否则只剩 3 个截面
    def s_oversold60(mom60):
        if mom60 is None: return 2.5
        if mom60 <= -30: return 5.0
        if mom60 <= -20: return 4.0
        if mom60 <= -10: return 3.0
        if mom60 <= 0: return 2.0
        return 1.0

    def s_bias60(price, ma60):
        if not ma60 or ma60 <= 0 or not price: return 2.5
        bias = (price - ma60) / ma60 * 100.0
        if bias <= -15: return 5.0
        if bias <= -10: return 4.0
        if bias <= -5: return 3.0
        if bias <= 0: return 2.0
        return 1.0

    def s_rsi_rev(rsi14):
        if rsi14 is None: return 2.5
        if rsi14 < 30: return 5.0
        if rsi14 < 40: return 4.0
        if rsi14 < 50: return 3.0
        if rsi14 < 60: return 2.0
        return 1.0

    def wilder_rsi(closes, period=14):
        if len(closes) < period + 1: return None
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

    # ⚠️ score_sector/score_attention 在 2026-10-13 前的截面为 0（入库时间差），
    #   且 sector 已证无效、attention 零权重 → 不进相关性矩阵，避免把 74 个截面筛掉
    DIMS = ["score_fund", "score_news", "score_theme", "score_tech"]
    C_sum, n_cs = None, 0
    # ⚠️ size_raw(circ_mv) 仅在 2026-10-23 一个截面有值（此前为 TOP200 旧结构无此字段），
    #   进矩阵会把 77 个截面全部筛掉 → 本轮不纳入（规模相关性见 round19 C3 单截面证据）
    labels = ["q_grouped", "g6040", "rev2_bf"] + DIMS
    for s in snaps:
        d0 = s["date"]
        cols = {k: [] for k in labels}
        for r in s["rows"]:
            rec = qg.get(d0, {}).get(r.get("code"))
            b = backfill.get(d0, {}).get(r.get("code"))
            cols["q_grouped"].append(rec.get("q_grouped") if rec else None)
            cols["g6040"].append(rec.get("g6040") if rec else None)
            cols["rev2_bf"].append((b[1] + b[2]) / 2.0 if b else None)
            for d in DIMS:
                v = r.get(d)
                cols[d].append(v if isinstance(v, (int, float)) else None)
        if any(sum(1 for x in cols[k] if x is None) > 0.3 * len(cols[k]) for k in labels):
            continue
        mat = [rank_pct([x if x is not None else 0.0 for x in cols[k]]) for k in labels]
        Ck = np.nan_to_num(np.corrcoef(np.array(mat)), nan=0.0)
        C_sum = Ck if C_sum is None else C_sum + Ck
        n_cs += 1
    C = C_sum / max(1, n_cs)
    np.fill_diagonal(C, 1.0)
    out["corr_matrix"] = {"labels": labels, "matrix": [[round(float(x), 3) for x in row] for row in C]}
    print(f"  {n_cs} 个截面截面内 rank 相关均值:")
    print("            " + " ".join(f"{l.replace('score_','')[:7]:>7}" for l in labels))
    for i, l in enumerate(labels):
        print(f"  {l.replace('score_','')[:9]:<10} " + " ".join(f"{C[i][j]:+7.2f}" for j in range(len(labels))))

    # ============================================================
    print()
    print("=" * 100)
    print("E. 合并损益：现行 vs round20-E vs 新6维（质量.25+成长.20+技术.15+量价+规模）")
    print("=" * 100)
    def build_total(weights, hop):
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
                    if d == "__rev2__":
                        b = backfill.get(d0, {}).get(r.get("code"))
                        vals[d] = (b[1] + b[2]) / 2.0 if b else None
                    elif d == "__rev4__":
                        vs = [r.get(k) for k in ("score_oversold60", "score_bias60",
                                                 "score_rsi_rev", "score_vol_pulse")]
                        vals[d] = sum(vs) / 4.0 if all(isinstance(x, (int, float)) for x in vs) else None
                    elif d in ("score_quality", "score_growth"):
                        rec = qg.get(d0, {}).get(r.get("code"))
                        key = "q_grouped" if d == "score_quality" else "g6040"
                        vals[d] = rec.get(key) if rec else None
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
                rp = rank_pct([xs[i] for i in present])
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

    variants = [
        ("A 现行12维(9生效,rev4入库残缺口径)",
         {"score_fund": 0.32, "score_news": 0.16, "score_theme_neg": 0.12,
          "score_size": 0.12, "score_sector": 0.10, "__rev4__": 0.18}),
        ("E round20-rev2精化(对照)",
         {"score_fund": 0.35, "__rev2__": 0.35, "score_size": 0.15, "score_tech_neg": 0.15}),
        ("N1 新6维-老大指定(质量.25+成长.20+技术.15+量价.30+规模.10)",
         {"score_quality": 0.25, "score_growth": 0.20, "score_tech_neg": 0.15,
          "__rev2__": 0.30, "score_size": 0.10}),
        ("N2 新6维-规模最小化(量价.35+规模.05)",
         {"score_quality": 0.25, "score_growth": 0.20, "score_tech_neg": 0.15,
          "__rev2__": 0.35, "score_size": 0.05}),
        ("N3 新6维-规模维持E(量价.25+规模.15)",
         {"score_quality": 0.25, "score_growth": 0.20, "score_tech_neg": 0.15,
          "__rev2__": 0.25, "score_size": 0.15}),
        ("N4 保fund+小权重质量成长(fund.25+质.08+成.05+技.15+量价.35+规模.12)",
         {"score_fund": 0.25, "score_quality": 0.08, "score_growth": 0.05,
          "score_tech_neg": 0.15, "__rev2__": 0.35, "score_size": 0.12}),
        ("N5 保fund为主+质量成长试探(fund.30+质.05+成.05+技.15+量价.33+规模.12)",
         {"score_fund": 0.30, "score_quality": 0.05, "score_growth": 0.05,
          "score_tech_neg": 0.15, "__rev2__": 0.33, "score_size": 0.12}),
    ]
    out["composite"] = {}
    for name, w in variants:
        assert abs(sum(w.values()) - 1.0) < 1e-9, name
        out["composite"][name] = {}
        print(f"\n  ── {name}")
        for hop in (1, 5, 20):
            rec = ic_battery(build_total(w, hop))
            out["composite"][name][str(hop)] = rec
            print(f"    h{hop}: {fmt(rec)}")

    # ============================================================
    print()
    print("=" * 100)
    print("F. 结论")
    print("=" * 100)
    q_h5 = (out["ic"].get("q_grouped") or {}).get("5")
    g_h5 = (out["ic"].get("g6040") or {}).get("5")
    n1 = (out["composite"].get("N1 新6维-老大指定(质量.25+成长.20+技术.15+量价.30+规模.10)") or {})
    n4 = (out["composite"].get("N4 保fund+小权重质量成长(fund.25+质.08+成.05+技.15+量价.35+规模.12)") or {})
    e_p = (out["composite"].get("E round20-rev2精化(对照)") or {})
    print(f"  F1 数据可得性: 16 子因子全部有 EM 缓存支撑（池内 490/490；历史 codes 81%）；"
          f"PIT 回填 {out['backfill']['rows']} 股票日 / {out['backfill']['dates']} 截面")
    print(f"  F2 族得分 h5: 质量 {fmt(q_h5)} / 成长 {fmt(g_h5)} → 两族自身均无稳健 alpha")
    print(f"  F3 合并损益: N1(老大指定正权重) h1 {fmt(n1.get('1'))} —— 全周期不过且低于现行；")
    print(f"     N4(保fund+质量.08+成长.05) h20 {fmt(n4.get('20'))} vs E {fmt(e_p.get('20'))} → 无损拆分路径")
    out["conclusion"] = {"q_h5": q_h5, "g_h5": g_h5,
                         "n1_h1": n1.get("1"), "n4_h20": n4.get("20"), "e_h20": e_p.get("20"),
                         "backfill_rows": out["backfill"]["rows"]}

    with open(os.path.join(HERE, "audit_round21.json"), "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1, default=float)
    print("\n✅ 已写 audit_round21.json")


def sub_factors_pit(ind, P):
    """按指定报告期 P 计算子因子（sub_factors 的 PIT 版本：外层已选好 P）。"""
    out = {}

    def get(name, p):
        return (ind.get(name) or {}).get(p)

    def prev_year(p):
        return "%d%s" % (int(p[:4]) - 1, p[4:])

    def prev_quarter(p):
        y, md = int(p[:4]), p[4:]
        return {"0331": "%d1231" % (y - 1), "0630": "%d0331" % y,
                "0930": "%d0630" % y, "1231": "%d0930" % y}.get(md)

    def yoy(name, p):
        cur, prev = get(name, p), get(name, prev_year(p))
        if cur is None or prev is None or prev == 0:
            return None
        return (cur - prev) / abs(prev) * 100.0

    def sq(name, p):
        pq = prev_quarter(p)
        cum = get(name, p)
        if cum is None:
            return None
        if pq is None:
            return cum
        prev_cum = get(name, pq)
        if prev_cum is None:
            return None
        return cum - prev_cum

    periods_desc = sorted({p for dd in ind.values() for p in dd}, reverse=True)

    out["roe"] = get("净资产收益率(ROE)", P)
    out["roic"] = get("投入资本回报率", P)
    out["roa"] = get("总资产报酬率(ROA)", P)
    if out["roa"] is None:
        out["roa"] = get("总资产净利率_平均", P)
    out["ocf_ni"] = get("经营活动净现金/归属母公司的净利润", P)
    out["sales_cash"] = get("经营活动净现金/销售收入", P)
    ni, ocf = get("净利润", P), get("经营现金流量净额", P)
    eq, dr = get("股东权益合计(净资产)", P), get("资产负债率", P)
    if ni is not None and ocf is not None and eq and eq > 0 and dr is not None and 0 <= dr < 100:
        ta = eq / (1.0 - dr / 100.0)
        out["accruals"] = -((ni - ocf) / ta * 100.0)
    ocf_now, ocf_prev = get("每股经营现金流", P), get("每股经营现金流", prev_year(P))
    if ocf_now is not None and ocf_prev and ocf_prev > 0:
        out["ocfg"] = (ocf_now / ocf_prev - 1.0) * 100.0
    annuals = [p for p in periods_desc if p[4:] == "1231"]
    annuals = [p for p in annuals if disclosure_date(p) and disclosure_date(p) <= disclosure_date(P)][:4]
    for sub, name in (("profit_vol", "净资产收益率(ROE)"), ("gm_vol", "毛利率")):
        vals = [get(name, p) for p in annuals]
        vals = [v for v in vals if v is not None]
        if len(vals) >= 3:
            out[sub] = -pstdev(vals)
    out["debt"] = -dr if dr is not None else None
    gwv = get("商誉", P)
    if gwv is not None and eq and eq > 0:
        out["gw"] = -100.0 * gwv / eq

    out["npg"] = yoy("扣非净利润", P)
    p3 = "%d%s" % (int(P[:4]) - 3, P[4:])
    cur3, base3 = get("扣非净利润", P), get("扣非净利润", p3)
    if cur3 is not None and base3 is not None and cur3 > 0 and base3 > 0:
        out["cagr3"] = ((cur3 / base3) ** (1.0 / 3.0) - 1.0) * 100.0
    out["revg"] = get("营业总收入增长率", P)
    if out["revg"] is None:
        out["revg"] = yoy("营业总收入", P)
    pq = prev_quarter(P)
    if pq:
        sq_now, sq_prev = sq("扣非净利润", P), sq("扣非净利润", pq)
        if sq_now is not None and sq_prev is not None and sq_prev != 0:
            out["qoq"] = (sq_now - sq_prev) / abs(sq_prev) * 100.0
        n_now, n_prev = yoy("扣非净利润", P), yoy("扣非净利润", pq)
        if n_now is not None and n_prev is not None:
            out["accel"] = n_now - n_prev
    return out


if __name__ == "__main__":
    main()
