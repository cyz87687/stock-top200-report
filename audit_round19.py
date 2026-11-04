#!/usr/bin/env python3
"""Round19：以外部近半年因子排名报告（final_ranking CSV）为选型依据，
系统评估「消息面/题材热度因子的替代方案」。

动机（老大 2026-10-25）：round18 已诊断 score_news/score_theme 的构造缺陷，
并给出「theme 是行业代理、news 有短线信号但被行业噪声稀释」的实证。
老大进一步提供外部因子库近半年排名（47 因子、222 日截面、11 个月度样本），
要求评估是否有更好的因子可以替换消息面与题材热度维度。

本脚本回答三个问题：
  Q1 外部报告里，与「消息/题材」同生态位（关注度/情绪/事件/热度）的因子表现如何？
  Q2 外部报告里真正稳健的因子是什么，能否替换进我们的 12 维权重？
  Q3 在我们自己的池子（TOP500 成交额）里，这些候选因子的表现是否与外部一致？

复测范围约束（数据纪律，必须先说清楚）：
  · factor_history 的 snapshot 只入库 12 个 score_* 字段 + circ_mv/price，
    财务字段（roe/debt_ratio/pe/ocfg/turnover）从未入库 → 财务类因子
    ROE/DEBT/EP/OCFG/NAG/TAG/REVG/NPG 无历史截面，本池无法复测（Part C 论证）
  · 价格库有 px/opens（1012 只 × 143 日）→ 量价类因子可完整复测：
    LVOL20 / MOM1W / MOM1M / BIAS20 / MAX20 / DIST52H
  · scored 文件仅留 4 份 → 财务因子只能做 3~4 个截面的弱方向验证

实证纪律（沿用 round13/15/16/18）：
  · 所有 t 值为 Newey-West，lag 全档扫描（L0/L3/L6），禁只报最好 lag
  · Bonferroni 门槛 3.08（12 因子）
  · 与 CSV 口径冲突时以 CSV 公式定义为准复现，差异必须披露
  · 小样本结论必须标注 n 与「方向性」字样
"""
import csv
import json
import math
import os
import re
import sys
from collections import Counter, defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import factor_eval as fe

T_BONF = 3.08
LAGS = [0, 3, 6]
# 外部因子排名报告（老大 2026-10-25 提供）。优先读项目内留痕副本（入 git 保可复现），
# fallback 到 Downloads 原件；两者都没有则 Part A 跳过（量价复测不受影响）。
CSV_PATH = os.path.join(HERE, "reference_final_ranking_halfyear.csv")
CSV_PATH_FALLBACK = "/Users/yzreal/Downloads/final_ranking-近半年.csv"


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
    vals = [v for v in vals if isinstance(v, (int, float))]
    if not vals:
        return None
    c = Counter(vals)
    hhi = sum((n / len(vals)) ** 2 for n in c.values())
    top5 = sum(n for _, n in c.most_common(5)) / len(vals)
    return {"n": len(vals), "uniq": len(c),
            "hhi": round(hhi, 4), "top5_share": round(top5, 3),
            "zero_rate": round(c.get(0, 0) / len(vals), 3),
            "cap_rate": round(c.get(5, 0) / len(vals), 3)}


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


def ic_battery(series_by_date):
    """IC + NW t（lag 全档）+ 五分位价差。series_by_date: [{date, fv, rv}]"""
    if len(series_by_date) < 3:
        return None
    ics, q_spread = [], []
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
    rec = {"n": len(ics), "ic": round(m, 4), "t_by_lag": {}}
    for L in LAGS:
        t = nw_t(ics, L)
        if t is not None:
            rec["t_by_lag"][str(L)] = round(t, 2)
    ts = [abs(v) for v in rec["t_by_lag"].values()]
    rec["robust_bonf"] = bool(ts) and all(t >= T_BONF for t in ts)
    if q_spread:
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


def industry_neutral_series(snaps, tdays, px, field_fn, hop, sec_map, min_n=40):
    """行业内组内百分位 rank 后的截面序列（round18 C 部分同口径）。"""
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
        groups = defaultdict(list)
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
            groups[sec_map.get(r.get("code"), "?")].append((v, p1 / p0 - 1.0))
        if sum(len(g) for g in groups.values()) < min_n:
            continue
        fv, rv = [], []
        for gname, pairs in groups.items():
            if len(pairs) < 3:
                continue
            vals = sorted(p[0] for p in pairs)
            n = len(vals)
            for v, rr in pairs:
                below = sum(1 for x in vals if x < v)
                fv.append(below / max(1, n - 1))
                rv.append(rr)
        if len(fv) >= 30:
            ser.append({"date": d0, "fv": fv, "rv": rv})
    return ser


# -----------------------------------------------------------------
# 价格库自算因子（严格按 CSV 公式定义复现，差异披露）
# -----------------------------------------------------------------
def make_price_factors(tdays, px):
    """逐日逐股预计算候选因子。返回 {date: {code: {fname: val}}}"""
    tidx = {d: i for i, d in enumerate(tdays)}
    fac = defaultdict(lambda: defaultdict(dict))
    for code, closes in px.items():
        if not isinstance(closes, dict):
            continue
        for i, d in enumerate(tdays):
            if d not in closes:
                continue
            c = closes[d]
            if not c or c <= 0:
                continue
            # ---- LVOL20: STD(CLOSE,20)/CLOSE（CSV 原式：价格标准差/现价）----
            if i >= 19:
                win = [closes.get(tdays[j]) for j in range(i - 19, i + 1)]
                if all(w and w > 0 for w in win) and len(win) == 20:
                    mu = sum(win) / 20.0
                    var = sum((w - mu) ** 2 for w in win) / 19.0
                    fac[d][code]["lowvol20"] = math.sqrt(var) / c
            # ---- MOM1W: CLOSE/REF(CLOSE,5)-1 ----
            if i >= 5:
                p5 = closes.get(tdays[i - 5])
                if p5 and p5 > 0:
                    fac[d][code]["mom1w"] = c / p5 - 1.0
            # ---- MOM1M: CLOSE/REF(CLOSE,21)-1 ----
            if i >= 21:
                p21 = closes.get(tdays[i - 21])
                if p21 and p21 > 0:
                    fac[d][code]["mom1m"] = c / p21 - 1.0
            # ---- BIAS20: (CLOSE-MA(CLOSE,20))/MA(CLOSE,20) ----
            if i >= 19:
                win = [closes.get(tdays[j]) for j in range(i - 19, i + 1)]
                if all(w and w > 0 for w in win) and len(win) == 20:
                    ma = sum(win) / 20.0
                    if ma > 0:
                        fac[d][code]["bias20"] = (c - ma) / ma
            # ---- MAX20: HHV(HIGH,20)/CLOSE-1（无 HIGH，用 CLOSE 近似，披露）----
            if i >= 19:
                win = [closes.get(tdays[j]) for j in range(i - 19, i + 1)]
                if all(w and w > 0 for w in win) and len(win) == 20:
                    fac[d][code]["max20"] = max(win) / c - 1.0
            # ---- DIST52H: CLOSE/HHV(CLOSE,250)-1 ----
            if i >= 249:
                win = [closes.get(tdays[j]) for j in range(i - 249, i + 1)]
                if all(w and w > 0 for w in win) and len(win) == 250:
                    fac[d][code]["dist52h"] = c / max(win) - 1.0
    return fac


def load_csv_ranking():
    for p in (CSV_PATH, CSV_PATH_FALLBACK):
        if os.path.exists(p):
            rows = []
            with open(p, encoding="utf-8-sig") as f:
                for row in csv.DictReader(f):
                    if not row.get("factor"):
                        continue
                    rows.append(row)
            if rows:
                return rows, p
    return None, None


def fnum(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def main():
    h = json.load(open(os.path.join(HERE, "factor_history.json"), encoding="utf-8"))
    snaps, _ = fe._drop_stale_price_snapshots(h["snapshots"])
    tdays, px = fe.px_index(h)
    print(f"截面数={len(snaps)}  价格日={len(tdays)} ({tdays[0]}~{tdays[-1]})  价格股数={len(px)}")

    # code -> sector 映射（scored 文件，6 位码，与 snapshot/px 同格式）
    import glob
    sec_map = {}
    for f in glob.glob(os.path.join(HERE, "top200_scored_*.json")):
        if "_watch_" in f or ".bak" in f:
            continue
        try:
            d = json.load(open(f, encoding="utf-8"))
        except Exception:
            continue
        for r in d.get("results") or []:
            c = re.sub(r"^(sh|sz|bj)", "", str(r.get("code") or ""))
            if c and r.get("sector"):
                sec_map[c] = r["sector"]
    print(f"code->sector 映射 {len(sec_map)} 只")

    fac = make_price_factors(tdays, px)
    tidx = {d: i for i, d in enumerate(tdays)}
    out = {"n_snapshots": len(snaps), "price_days": len(tdays),
           "price_range": [tdays[0], tdays[-1]]}

    csv_rows, csv_src = load_csv_ranking()
    out["csv_loaded"] = csv_rows is not None
    if csv_src:
        out["csv_source"] = csv_src

    # ============================================================
    print()
    print("=" * 100)
    print("A. 外部因子排名报告交叉对照（final_ranking 近半年，222 日截面 / 11 个月）")
    print("=" * 100)
    if not csv_rows:
        print(f"  ⚠️ CSV 未找到: {CSV_PATH} —— Part A 跳过（量价复测不受影响）")
    else:
        print(f"  因子总数 {len(csv_rows)}，selected=TRUE {sum(1 for r in csv_rows if r.get('selected')=='TRUE')} 个")
        # A1 按 category 分组
        by_cat = defaultdict(list)
        for r in csv_rows:
            by_cat[r.get("category") or "?"].append(r)
        print("\n  A1 分类视图（selected / 落选）：")
        for cat, rs in sorted(by_cat.items(), key=lambda kv: -len(kv[1])):
            sel = [r["factor"] for r in rs if r.get("selected") == "TRUE"]
            uns = [r["factor"] for r in rs if r.get("selected") != "TRUE"]
            print(f"    {cat:<6} 入选: {','.join(sel) if sel else '—'}")
            print(f"    {'':<6} 落选: {','.join(uns)}")
        # A2 全样本强度排名（多口径）
        print("\n  A2 全样本强度排名（|IC| / ICIR / 超额t / 多空IR 四口径 Top8）：")
        def top_by(key, n=8, absval=True):
            def gv(r):
                v = fnum(r.get(key))
                return abs(v) if (v is not None and absval) else v
            rs = [r for r in csv_rows if gv(r) is not None]
            rs.sort(key=lambda r: -(gv(r) or 0))
            return [(r["factor"], r.get("category"), round(gv(r), 4) if gv(r) is not None else None) for r in rs[:n]]
        for key in ("ic_daily", "icir_daily", "exc_t", "ls_ir"):
            print(f"    {key:<10}: " + "  ".join(f"{f}({c})={v}" for f, c, v in top_by(key)))
        # A3 与消息/题材同生态位的因子（关注度/情绪/事件/热度的量价代理）
        print("\n  A3 与消息/题材同生态位的因子（关注度/情绪/热度的量价代理）：")
        eco = ["VOLR20", "TURN20", "AMT20", "MAX20", "MOM1W", "LVOL20", "DIST52H", "ILLIQ", "TRIX12", "MACDDIF"]
        out["csv_ecosystem"] = {}
        for f in eco:
            r = next((x for x in csv_rows if x["factor"] == f), None)
            if not r:
                continue
            ic = fnum(r.get("ic_daily"))
            ic_s = f"{ic:+.4f}" if ic is not None else "  无数据"
            print(f"    {f:<8} {r.get('category'):<4} IC={ic_s} ICIR={r.get('icir_daily')} "
                  f"exc_t={r.get('exc_t')} ls_ir={r.get('ls_ir')} direction={r.get('direction')} "
                  f"selected={r.get('selected')}")
            out["csv_ecosystem"][f] = {k: r.get(k) for k in
                ("category", "ic_daily", "icir_daily", "exc_t", "ls_ir", "direction", "selected", "score")}
        # A4 selected 因子明细
        print("\n  A4 selected=TRUE 因子明细：")
        out["csv_selected"] = {}
        for r in csv_rows:
            if r.get("selected") != "TRUE":
                continue
            print(f"    {r['factor']:<8} {r.get('category'):<4} IC={fnum(r.get('ic_daily')):+.4f} "
                  f"ICIR={r.get('icir_daily'):<7} 月ICt={r.get('ic_month_t'):<7} exc_t={r.get('exc_t'):<7} "
                  f"ls_ir={r.get('ls_ir'):<7} score={r.get('score')}")
            out["csv_selected"][r["factor"]] = {k: r.get(k) for k in
                ("category", "ic_daily", "icir_daily", "ic_month_t", "exc_t", "ls_ir", "score", "direction")}

    # ============================================================
    print()
    print("=" * 100)
    print("B. 池内复测：量价类候选（价格库全量可复算，78 截面）")
    print("=" * 100)
    print("  口径对照：CSV=全市场近半年；本表=TOP500 成交额池 2026-03-24~10-23")
    # B 部分候选：fname -> (CSV因子名, 说明)
    cands = [
        ("lowvol20", "LVOL20", "20日价格波动/现价（低波）"),
        ("mom1w",    "MOM1W",  "5日动量（CSV 方向: 越小越好=反转）"),
        ("mom1m",    "MOM1M",  "1个月动量（CSV 方向: 越小越好=反转）"),
        ("bias20",   "BIAS20", "20日乖离率（CSV 方向: 越小越好=反转）"),
        ("max20",    "MAX20",  "距20日高点回撤/彩票因子（CSV 方向: 越大越好）"),
        ("dist52h",  "DIST52H","距52周高点（CSV 方向: 越小越好）"),
    ]
    out["pool_retest"] = {}
    for fname, csv_name, desc in cands:
        print(f"\n  ── {fname}（{desc}）")
        fn = (lambda ff: (lambda r, s, d0: fac.get(d0, {}).get(r.get("code"), {}).get(ff)))(fname)
        # 离散度
        vals = []
        for s in snaps:
            for r in s["rows"]:
                v = fac.get(s["date"], {}).get(r.get("code"), {}).get(fname)
                if isinstance(v, (int, float)):
                    vals.append(v)
        dd = dispersion(vals) if vals else None
        if dd:
            gate = "✅过" if (dd["uniq"] >= 100 and dd["hhi"] < 0.05 and dd["cap_rate"] < 0.05) else "⚠️"
            print(f"    离散度: n={dd['n']} uniq={dd['uniq']} HHI={dd['hhi']:.3f} 零值率={dd['zero_rate']:.1%} {gate}")
        recs = {}
        for hop in (1, 5, 20):
            rec = ic_battery(cross_section_series(snaps, tdays, px, fn, hop))
            recs[str(hop)] = rec
            print(f"    h{hop}:{fmt(rec)}")
        out["pool_retest"][fname] = {"csv_name": csv_name, "desc": desc,
                                     "dispersion": dd, "ic": recs}
        # 对 h5 或 h20 有值的因子补行业内中性化（信息量最大的周期）
        for hop in (1, 20):
            ser_ind = industry_neutral_series(snaps, tdays, px, fn, hop, sec_map)
            rec_ind = ic_battery(ser_ind)
            out["pool_retest"][fname].setdefault("ic_industry_neutral", {})[str(hop)] = rec_ind
            print(f"    行业内中性化 h{hop}:{fmt(rec_ind)}")

    # ============================================================
    # B2 候选因子与现有 12 维的相关性（六条验证闸第 5 条：|ρ|>0.6 只留 IC 更高者）
    # ============================================================
    print()
    print("=" * 100)
    print("B2. 候选因子 × 现有 12 维相关性矩阵（截面 rank IC 均值，78 截面）")
    print("=" * 100)
    score_dims = ["score_fund", "score_news", "score_theme", "score_sector",
                  "score_oversold60", "score_bias60", "score_rsi_rev", "score_vol_pulse",
                  "score_overbought", "score_tech", "score_theme_intrinsic", "score_attention"]
    out["corr_with_existing"] = {}
    for fname, csv_name, desc in cands:
        row = {}
        for dim in score_dims:
            cs = []
            for s in snaps:
                d0 = s["date"]
                xs, ys = [], []
                for r in s["rows"]:
                    v = fac.get(d0, {}).get(r.get("code"), {}).get(fname)
                    dv = r.get(dim)
                    if isinstance(v, (int, float)) and isinstance(dv, (int, float)):
                        xs.append(v)
                        ys.append(dv)
                if len(xs) >= 40:
                    c = fe.rank_ic(xs, ys)
                    if c is not None:
                        cs.append(c)
            if cs:
                row[dim] = round(sum(cs) / len(cs), 3)
        out["corr_with_existing"][fname] = row
        hi = sorted(((abs(v), k, v) for k, v in row.items()), reverse=True)[:3]
        hi_s = "  ".join(f"{k}={v:+.2f}" for _, k, v in hi)
        flag = "  ⚠️与现有维度高共线" if hi and hi[0][0] > 0.6 else ""
        print(f"  {fname:<10} 最高相关: {hi_s}{flag}")
        print(f"  {'':<10} 全维: " + " ".join(f"{k.replace('score_','')}={v:+.2f}" for k, v in row.items()))

    # ============================================================
    print()
    print("=" * 100)
    print("C. 财务类候选：本池复测障碍与 4 截面弱方向验证")
    print("=" * 100)
    # C1 snapshot 入库字段审计
    snap_keys = set()
    for s in snaps:
        for r in s["rows"]:
            snap_keys |= set(r.keys())
    fin_fields = ["roe", "debt_ratio", "pe", "ocfg", "growth", "cagr3", "turnover", "turnover_ratio"]
    missing_in_snap = [f for f in fin_fields if f not in snap_keys]
    print(f"  C1 snapshot 入库字段缺: {missing_in_snap}")
    print(f"     → ROE/DEBT/EP/OCFG/NAG/TAG/REVG/NPG 无历史截面，本池无法做 IC 复测")
    print(f"     → 修复路径：扩展 factor_history 入库字段（见方案 Part 4）")
    out["snapshot_missing_fields"] = missing_in_snap

    # C2 4 份 scored 文件的弱方向验证（n=3~4 截面，禁显著性断言）
    scored_files = [f for f in sorted(glob.glob(os.path.join(HERE, "top200_scored_*.json")))
                    if "_watch_" not in f and ".bak" not in f]
    print(f"\n  C2 弱方向验证（{len(scored_files)} 个 scored 截面 × 未来5日收益 rank IC，方向性证据）：")
    out["weak_fin_validation"] = {}
    for fld, direction in (("roe", "越大越好"), ("debt_ratio", "越小越好"),
                           ("ocfg", "越大越好"), ("pe", "越小越好(EP=1/PE)")):
        ics = []
        for f in scored_files:
            try:
                d = json.load(open(f, encoding="utf-8"))
            except Exception:
                continue
            dt = d.get("date")
            if dt not in tidx:
                continue
            i0 = tidx[dt]
            if i0 + 5 >= len(tdays):
                continue
            d1 = tdays[i0 + 5]
            fv, rv = [], []
            for r in d.get("results") or []:
                code6 = re.sub(r"^(sh|sz|bj)", "", str(r.get("code") or ""))
                m = px.get(code6)
                if not m:
                    continue
                p0, p1 = m.get(dt), m.get(d1)
                if not p0 or not p1 or p0 <= 0:
                    continue
                v = r.get(fld)
                if not isinstance(v, (int, float)):
                    continue
                if fld == "pe" and v <= 0:
                    continue
                fv.append(v)
                rv.append(p1 / p0 - 1.0)
            if len(fv) >= 40:
                ic = fe.rank_ic(fv, rv)
                if ic is not None:
                    ics.append((dt, round(ic, 4)))
        if ics:
            mean_ic = sum(x[1] for x in ics) / len(ics)
            pos = sum(1 for _, v in ics if v > 0)
            print(f"    {fld:<12} ({direction}): 截面 IC = {ics}  均值={mean_ic:+.4f} 正号占比={pos}/{len(ics)}")
            out["weak_fin_validation"][fld] = {"direction": direction, "ics": ics,
                                               "mean_ic": round(mean_ic, 4),
                                               "note": "n=3~4 截面，仅方向性证据，禁显著性断言"}

    # C3 score_size 口径复测（round17 遗留：唯二可测截面 IC 为负 vs CSV SIZE 全场第一）
    print(f"\n  C3 score_size 口径复测（round17 遗留问题，n=1~2 截面仅方向性）：")
    out["size_retest"] = {}
    for fld, label in (("circ_mv", "流通市值原始值"), ("ln_circ_mv", "流通市值自然对数")):
        ics = []
        for f in scored_files:
            try:
                d = json.load(open(f, encoding="utf-8"))
            except Exception:
                continue
            dt = d.get("date")
            if dt not in tidx:
                continue
            i0 = tidx[dt]
            if i0 + 5 >= len(tdays):
                continue
            d1 = tdays[i0 + 5]
            fv, rv = [], []
            for r in d.get("results") or []:
                code6 = re.sub(r"^(sh|sz|bj)", "", str(r.get("code") or ""))
                m = px.get(code6)
                if not m:
                    continue
                p0, p1 = m.get(dt), m.get(d1)
                if not p0 or not p1 or p0 <= 0:
                    continue
                v = r.get("circ_mv")
                if not isinstance(v, (int, float)) or v <= 0:
                    continue
                if fld == "ln_circ_mv":
                    v = math.log(v)
                fv.append(v)
                rv.append(p1 / p0 - 1.0)
            if len(fv) >= 40:
                ic = fe.rank_ic(fv, rv)
                if ic is not None:
                    ics.append((dt, round(ic, 4)))
        if ics:
            print(f"    {label:<12} 截面 IC = {ics}  （负 IC=小市值跑赢，与 CSV SIZE 方向一致应为负）")
            out["size_retest"][fld] = {"ics": ics,
                                       "note": "n 极小仅方向性；CSV SIZE 为全市场口径 222 截面 exc_t=2.28/exc_win=91%，本池为池内口径，口径差异是 round17 负 IC 的候选解释"}
        else:
            print(f"    {label:<12} 无有效配对截面")

    # ============================================================
    print()
    print("=" * 100)
    print("D. 结论汇总")
    print("=" * 100)
    print("  D1 生态位结论：CSV 全部 47 因子无一个消息/情绪/舆情类因子 →")
    print("     外部专业因子库同样认为消息面不可标准化，量价代理（VOLR20/TURN20/AMT20）全部疲软")
    print("  D2 稳健结论：CSV selected 集中在 小市值(SIZE)/质量(ROE)/成长(NAG,OCFG)/偿债(DEBT)/反转(MOM1W)/估值(EP)")
    print("  D3 池内复测结论见 Part B（量价类）；财务类因复测障碍只能采信 CSV 外部证据")
    out["conclusion"] = {
        "ecosystem": "CSV 47 因子无消息/情绪/舆情类；量价关注度代理（VOLR20/TURN20/AMT20/MAX20/ILLIQ）全部 |IC|<0.04 且多不显著",
        "robust_in_csv": "SIZE(exc_t2.28,win91%) / OCFG(icir0.732 全场第一) / NAG(ls_ir1.22) / ROE / DEBT / EP / MOM1W(exc_t1.51)",
        "pool_retest": {k: {hh: (v['ic'].get(str(hh)) or {}).get('ic') for hh in (1, 5, 20)}
                        for k, v in out["pool_retest"].items()},
    }

    with open(os.path.join(HERE, "audit_round19.json"), "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print("\n✅ 已写 audit_round19.json")


if __name__ == "__main__":
    main()
