#!/usr/bin/env python3
"""Round20：评估「12 维 → 5 维（基本面/质量、成长、技术面、量价、规模）」架构重构的可行性。

动机（老大 2026-10-25）：在 round18/19 诊断消息面/题材因子之后，进一步要求评估
将现有因子体系收敛到五个一级维度的可行性。本脚本用真实数据回答四个问题：

  Q1 冗余度：现有 9 个生效维度（+score_tech）彼此相关多高？有效自由度几个？
     → 相关矩阵 + PCA 方差解释率（前 k 主成分累计解释>=85% 的 k 即等效维度数）
  Q2 各维池内 IC：统一口径复算（含从未入权重的 score_tech 首次实测）
  Q3 合并损益：现行 12 维加权 vs 5 维族结构，total 的 IC 对比
  Q4 数据可得性：5 维每个维度需要的数据、现有来源、历史入库状态、缺口

纪律（沿用 round13~19）：
  · NW t 全 lag 扫描（L0/L3/L6），Bonferroni 门槛 3.08
  · 5 维映射中「成长」维度因财务字段无历史入库，用 fund 兼任并显式披露
  · 小样本结论标注 n 与「方向性」
"""
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

# 现行 12 维权重（3 个 0 权重的展示维度不计入生效集合）
W_CURRENT = {"score_fund": 0.32, "score_news": 0.16, "score_theme_neg": 0.12,
             "score_size": 0.12, "score_sector": 0.10, "score_oversold60": 0.045,
             "score_bias60": 0.045, "score_rsi_rev": 0.045, "score_vol_pulse": 0.045}

# round19 建议的 5 维族间权重（成长维度暂无独立数据，fund 兼任，显式披露）
W_5DIM = {"基本面质量成长(fund兼任)": 0.45, "技术面(score_tech)": 0.15,
          "量价(反转块等权)": 0.25, "规模(-池内市值分位)": 0.15}


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
    ts = [abs(v) for v in rec["t_by_lag"].values()]
    rec["robust_bonf"] = bool(ts) and all(t >= T_BONF for t in ts)
    return rec


def fmt(rec):
    if not rec:
        return "样本不足"
    ts = rec.get("t_by_lag") or {}
    tstr = " ".join(f"L{k}:{v:+.2f}" for k, v in sorted(ts.items()))
    rob = "✅" if rec.get("robust_bonf") else "⚠️"
    return f"n={rec['n']:<3} IC={rec['ic']:+.4f} {tstr} {rob}"


def main():
    h = json.load(open(os.path.join(HERE, "factor_history.json"), encoding="utf-8"))
    snaps, _ = fe._drop_stale_price_snapshots(h["snapshots"])
    tdays, px = fe.px_index(h)
    print(f"截面数={len(snaps)}  价格日={len(tdays)} ({tdays[0]}~{tdays[-1]})  价格股数={len(px)}")

    out = {"n_snapshots": len(snaps), "price_range": [tdays[0], tdays[-1]]}

    # 9 个生效维度 + score_tech（技术面素材，从未入权重）+ circ_mv（规模）
    DIMS = ["score_fund", "score_news", "score_theme", "score_sector",
            "score_oversold60", "score_bias60", "score_rsi_rev", "score_vol_pulse",
            "score_tech", "score_attention"]

    # ============================================================
    print()
    print("=" * 100)
    print("A. 维度冗余度：相关矩阵 + PCA 等效维度数")
    print("=" * 100)
    # 逐截面 rank 相关矩阵，再对「截面间」取平均（更稳健：先截面内 rank 化）
    acc = None
    n_cs = 0
    C_sum = None
    for s in snaps:
        cols = {d: [] for d in DIMS}
        cols["size_raw"] = []
        for r in s["rows"]:
            mv = r.get("circ_mv")
            cols["size_raw"].append(mv if isinstance(mv, (int, float)) else None)
            for d in DIMS:
                v = r.get(d)
                cols[d].append(v if isinstance(v, (int, float)) else None)
        # 任一维度缺失率 >30% 的截面跳过
        if any(sum(1 for x in cols[d] if x is None) > 0.3 * len(cols[d]) for d in DIMS):
            continue
        mat = []
        for d in DIMS:
            xs = [x if x is not None else 0.0 for x in cols[d]]
            mat.append(rank_pct(xs))
        mat.append(rank_pct([x if x is not None else 0.0 for x in cols["size_raw"]]))
        M = np.array(mat)
        Ck = np.corrcoef(M)
        Ck = np.nan_to_num(Ck, nan=0.0)
        if C_sum is None:
            C_sum = Ck
        else:
            C_sum = C_sum + Ck
        n_cs += 1
    C = C_sum / max(1, n_cs)
    np.fill_diagonal(C, 1.0)
    labels = DIMS + ["circ_mv(规模)"]
    out["corr_matrix"] = {"labels": labels, "matrix": [[round(float(x), 3) for x in row] for row in C]}
    print(f"  {n_cs} 个截面的截面内 rank 相关均值：")
    print("            " + " ".join(f"{l.replace('score_','')[:7]:>7}" for l in labels))
    for i, l in enumerate(labels):
        row = " ".join(f"{C[i][j]:+7.2f}" for j in range(len(labels)))
        print(f"  {l.replace('score_','')[:9]:<10} {row}")
    # PCA 等效维度数
    evals = np.linalg.eigvalsh(C)[::-1]
    evals = np.maximum(evals, 0)
    tot = evals.sum()
    cum = np.cumsum(evals) / tot
    k85 = int(np.searchsorted(cum, 0.85) + 1)
    out["pca"] = {"eigenvalues": [round(float(x), 3) for x in evals],
                  "cum_var_ratio": [round(float(x), 3) for x in cum],
                  "k_for_85pct": k85}
    print(f"\n  特征值: {[round(float(x),2) for x in evals]}")
    print(f"  累计方差解释率: {[round(float(x),3) for x in cum]}")
    print(f"  → 85% 方差只需 {k85} 个主成分（名义 {len(labels)} 维）")

    # ============================================================
    print()
    print("=" * 100)
    print("B. 各维度池内 IC 复算（统一口径，含 score_tech 首次实测）")
    print("=" * 100)
    out["dim_ic"] = {}
    dim_fns = {d: (lambda dd: (lambda r, s, d0: r.get(dd)))(d) for d in DIMS}
    for d in DIMS:
        out["dim_ic"][d] = {}
        for hop in (1, 5, 20):
            rec = ic_battery(series_for_dim(snaps, tdays, px, dim_fns[d], hop))
            out["dim_ic"][d][str(hop)] = rec
        r1, r5, r20 = (out["dim_ic"][d].get(str(x)) for x in (1, 5, 20))
        cur_w = W_CURRENT.get(d, 0.0)
        if d == "score_theme":
            cur_w = W_CURRENT.get("score_theme_neg", 0.0)
        print(f"  {d:<20} (现权重{cur_w:.3f}) h1:{fmt(r1)}")
        print(f"  {'':<20}            h5:{fmt(r5)}")
        print(f"  {'':<20}            h20:{fmt(r20)}")

    # ============================================================
    # B2 反转块回填重测：三兄弟（oversold60/bias60/rsi_rev）是纯价格函数，
    #   价格库 143 天全量可自算 → 修复「10-13 才入库、历史截面全 0」事故
    #   ⚠️ vol_pulse（额比 amt5/amt20）与 attention（成交额排名）依赖成交量，
    #      价格库无 volume → 不可回填，只呈现三兄弟结果
    # ============================================================
    print()
    print("=" * 100)
    print("B2. 反转块回填重测（px 库自算 143 天，修复入库事故）")
    print("=" * 100)
    print("  口径复现自 step2_stockscorer_v2.py：score_oversold60(mom60 分档) /")
    print("  score_bias60((price-ma60)/ma60 分档) / score_rsi_rev(Wilder RSI14 分档)")

    def s_oversold60(mom60):
        if mom60 is None:
            return 2.5
        if mom60 <= -30: return 5.0
        if mom60 <= -20: return 4.0
        if mom60 <= -10: return 3.0
        if mom60 <= 0:   return 2.0
        return 1.0

    def s_bias60(price, ma60):
        if not ma60 or ma60 <= 0 or not price:
            return 2.5
        bias = (price - ma60) / ma60 * 100.0
        if bias <= -15: return 5.0
        if bias <= -10: return 4.0
        if bias <= -5:  return 3.0
        if bias <= 0:   return 2.0
        return 1.0

    def s_rsi_rev(rsi14):
        if rsi14 is None:
            return 2.5
        if rsi14 < 30: return 5.0
        if rsi14 < 40: return 4.0
        if rsi14 < 50: return 3.0
        if rsi14 < 60: return 2.0
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

    # 自算全量：{date: {code: (oversold60, bias60, rsi_rev)}}
    backfill = defaultdict(dict)
    for code, closes_d in px.items():
        if not isinstance(closes_d, dict):
            continue
        cl = [closes_d.get(d) for d in tdays]
        for i, d in enumerate(tdays):
            if i < 60:
                continue
            if not cl[i] or cl[i] <= 0:
                continue
            # mom60
            p60 = cl[i - 60]
            if not p60 or p60 <= 0:
                continue
            mom60 = (cl[i] / p60 - 1.0) * 100.0
            # ma60
            win = cl[i - 59:i + 1]
            if any(w is None or w <= 0 for w in win):
                continue
            ma60 = sum(win) / 60.0
            # rsi14：Wilder 递推，传入越长历史越收敛（EMA 式对初值不敏感）
            hist = [cl[j] for j in range(0, i + 1) if cl[j] and cl[j] > 0]
            r14 = wilder_rsi(hist, 14) if len(hist) >= 15 else None
            backfill[d][code] = (s_oversold60(mom60), s_bias60(cl[i], ma60), s_rsi_rev(r14))

    # 一致性校验：与 10-13~10-23 已入库的 9 个截面对照
    stored = [s for s in snaps if any(isinstance(r.get("score_bias60"), (int, float)) for r in s["rows"])]
    print(f"\n  一致性校验（自算 vs 已入库 {len(stored)} 个截面，完全相等比例）：")
    out["backfill_consistency"] = {}
    for fname, idx in (("score_oversold60", 0), ("score_bias60", 1), ("score_rsi_rev", 2)):
        eq, tot = 0, 0
        for s in stored:
            d0 = s["date"]
            if d0 not in backfill:
                continue
            for r in s["rows"]:
                v = r.get(fname)
                b = backfill[d0].get(r.get("code"))
                if isinstance(v, (int, float)) and b is not None:
                    tot += 1
                    if abs(v - b[idx]) < 1e-9:
                        eq += 1
        rate = eq / max(1, tot)
        out["backfill_consistency"][fname] = {"match": eq, "total": tot, "rate": round(rate, 4)}
        flag = "✅" if rate >= 0.95 else ("⚠️" if rate >= 0.85 else "❌")
        print(f"    {fname:<20} {eq}/{tot} = {rate:.1%} {flag}")

    # 全量 IC 重测（回填口径）
    print(f"\n  全量 IC 重测（回填后样本量：h1/h5/h20 应为 77/73/58 量级）：")
    out["backfill_ic"] = {}
    for fname, idx in (("score_oversold60", 0), ("score_bias60", 1), ("score_rsi_rev", 2)):
        fn = (lambda ii: (lambda r, s, d0: backfill.get(d0, {}).get(r.get("code"), (None, None, None))[ii]))(idx)
        out["backfill_ic"][fname] = {}
        for hop in (1, 5, 20):
            rec = ic_battery(series_for_dim(snaps, tdays, px, fn, hop))
            out["backfill_ic"][fname][str(hop)] = rec
        r1, r5, r20 = (out["backfill_ic"][fname].get(str(x)) for x in (1, 5, 20))
        print(f"    {fname:<20} h1:{fmt(r1)}")
        print(f"    {'':<20} h5:{fmt(r5)}")
        print(f"    {'':<20} h20:{fmt(r20)}")
    # 对照：回填前的旧口径（入库 9 截面）n 值
    old = out["dim_ic"].get("score_bias60") or {}
    print(f"\n  对照 round4/5/13 旧结论：入库截面 n(h1)={ (old.get('1') or {}).get('n') }、"
          f"n(h5)={ (old.get('5') or {}).get('n') } → 本次回填后见上（样本量放大约 10 倍）")

    # ============================================================
    print()
    print("=" * 100)
    print("C. 5 维映射与数据可得性审计")
    print("=" * 100)
    audit = {
        "基本面/质量": {
            "需要": ["ROE/ROA/ROIC", "毛利率/净利率", "资产负债率/产权比率", "估值(EP/BP/PE)"],
            "现有来源": "scored 单截面: roe(490/490), debt_ratio(490/490), pe, fund_detail",
            "历史入库": "❌ 无（snapshot 只有 score_fund 聚合分，fund 本身混合了估值+成长，无法拆分）",
            "缺口": "财务原始字段未入库 → 质量维度无法独立成族、无法历史复测"},
        "成长": {
            "需要": ["净利润/营收/扣非增长", "3年CAGR", "经营现金流增长(OCFG)"],
            "现有来源": "scored 单截面: growth(490/490), cagr3(362/490), ocfg(400/490)",
            "历史入库": "❌ 无（同上，成长信息被打包进 score_fund）",
            "缺口": "同质量维度；OCFG 已采未用（round19），其余需先扩展入库"},
        "技术面": {
            "需要": ["均线体系", "MACD", "RSI", "KDJ", "乖离"],
            "现有来源": "scored 单截面 tech dict 36 子字段(ma5/60/macd/rsi/mom5-60/vol_ratio)",
            "历史入库": "⚠️ 部分（score_tech 聚合分 490/490 入库可复算；tech dict 原始指标只有单截面）",
            "缺口": "score_tech 从未入权重、无 IC 证据；原始指标需入库才能细化"},
        "量价": {
            "需要": ["短期/中期反转", "动量", "波动", "成交额/换手/量比"],
            "现有来源": "反转块四兄弟(历史✓) + px 自算动量/波动 + 单截面 turnover/turnover_ratio/vol_ratio",
            "历史入库": "⚠️ 部分（反转块四兄弟历史✓；成交额/换手只有单截面，价格库无 volume）",
            "缺口": "量比/换手/成交额历史缺 → VOLR20/TURN20/AMT20 类因子不可复测（round19 已证）"},
        "规模": {
            "需要": ["流通市值/总市值"],
            "现有来源": "scored + snapshot: circ_mv(490/490)",
            "历史入库": "✅ 有（circ_mv 全量入库）",
            "缺口": "池内口径与全市场口径相反（round19 C3），需明确用哪个口径"},
    }
    for k, v in audit.items():
        print(f"\n  【{k}】")
        for kk, vv in v.items():
            print(f"    {kk}: {vv}")
    out["data_audit"] = audit

    # ============================================================
    print()
    print("=" * 100)
    print("D. 合并损益：现行 12 维加权 vs 5 维族结构（total 的 IC 对比）")
    print("=" * 100)
    # 现行口径：theme_neg = 10 - score_theme；size = -池内 circ_mv 分位（高分=小市值）
    def dim_value(r, s, d0, d):
        if d == "score_theme_neg":
            v = r.get("score_theme")
            return (10.0 - v) if isinstance(v, (int, float)) else None
        if d == "score_size":
            mv = r.get("circ_mv")
            return mv if isinstance(mv, (int, float)) else None
        return r.get(d)

    # 注意：现行口径里 size 加分方向是「小市值」，circ_mv 原始值越大=市值越大 → 加权时取负
    # 为保持与现行 total 方向一致，这里 size 维度用 -circ_mv
    def build_total(weights, hop, rev_equal=False):
        """按权重体系逐截面合成 total，返回 IC 序列。weights: {dim: w}"""
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
            # 先收集各行各维值
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
                    if d == "__rev4__":
                        vs = [r.get(k) for k in ("score_oversold60", "score_bias60",
                                                 "score_rsi_rev", "score_vol_pulse")]
                        vals["__rev4__"] = sum(vs) / 4.0 if all(isinstance(x, (int, float)) for x in vs) else None
                    elif d == "__rev3__":
                        # 回填口径三兄弟等权（vol_pulse 无成交额历史不可回填，剔除）
                        b = backfill.get(d0, {}).get(r.get("code"))
                        vals["__rev3__"] = sum(b) / 3.0 if b else None
                    elif d == "__rev2__":
                        # 回填口径两兄弟等权（oversold60 单独不显著，剔除）
                        b = backfill.get(d0, {}).get(r.get("code"))
                        vals["__rev2__"] = (b[1] + b[2]) / 2.0 if b else None
                    elif d == "score_size_rev":
                        # 池内大市值加分（round19 C3：池内 circ_mv IC +0.2108 方向）
                        mv = r.get("circ_mv")
                        vals[d] = mv if isinstance(mv, (int, float)) else None
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
            # 逐维截面内百分位（rank 化消除量纲），缺维从分母剔除
            dim_ranks = {}
            for d in weights:
                xs = [rv[0].get(d) for rv in rows]
                present = [i for i, x in enumerate(xs) if isinstance(x, (int, float))]
                if len(present) < 0.5 * len(rows):
                    dim_ranks[d] = None
                    continue
                rp = rank_pct([xs[i] for i in present])
                m = {i: rp[k] for k, i in enumerate(present)}
                dim_ranks[d] = m
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

    # A：现行 12 维（9 生效）——注意 rev4 入库口径在 77 个截面中仅 8 个有值，
    #    大部分截面 __rev4__ 缺失→分母缩小→量价维度实际未充分参与（如实呈现）
    w_curr = {"score_fund": 0.32, "score_news": 0.16, "score_theme_neg": 0.12,
              "score_size": 0.12, "score_sector": 0.10, "__rev4__": 0.18}
    # B：5 维证据版——量价用回填口径三兄弟等权；技术面反号（tech 低分=弱势=反转标的）
    w_5d_evi = {"score_fund": 0.35, "__rev3__": 0.35, "score_size": 0.15, "score_tech_neg": 0.15}
    # C：5 维保守版——技术面正权（反面对照：预期其拖累组合）
    w_5d_techpos = {"score_fund": 0.35, "__rev3__": 0.30, "score_size": 0.15, "score_tech": 0.20}
    # D：5 维无技术面版——fund/量价/规模三族 + 消息面修复版占位
    w_5d_notech = {"score_fund": 0.40, "__rev3__": 0.35, "score_size": 0.15, "score_news": 0.10}
    # E：rev2 精化版（剔除不显著的 oversold60）
    w_5d_rev2 = {"score_fund": 0.35, "__rev2__": 0.35, "score_size": 0.15, "score_tech_neg": 0.15}
    # F：rev2 + size 反号（池内大市值加分，round19 C3 方向）
    w_5d_rev2_bigmv = {"score_fund": 0.35, "__rev2__": 0.35, "score_size_rev": 0.15, "score_tech_neg": 0.15}

    for name, w in (("A 现行12维(9生效,rev为入库残缺口径)", w_curr),
                    ("D 5维-无技术面", w_5d_notech),
                    ("B 5维-证据版(tech反号入技术面)", w_5d_evi),
                    ("C 5维-对照(tech正权,预期拖累)", w_5d_techpos),
                    ("E 5维-rev2精化(剔除oversold60)", w_5d_rev2),
                    ("F 5维-rev2+大市值(size反号)", w_5d_rev2_bigmv)):
        out.setdefault("composite", {})[name] = {}
        print(f"\n  ── {name}  权重={ {k.replace('score_','').replace('__rev4__','量价rev4'): round(v,3) for k,v in w.items()} }")
        for hop in (1, 5, 20):
            rec = ic_battery(build_total(w, hop))
            out["composite"][name][str(hop)] = rec
            print(f"    h{hop}: {fmt(rec)}")

    # ============================================================
    print()
    print("=" * 100)
    print("E. 结论")
    print("=" * 100)
    tech_h5 = (out["dim_ic"].get("score_tech") or {}).get("5")
    fund_h5 = (out["dim_ic"].get("score_fund") or {}).get("5")
    print(f"  E1 等效维度数: PCA 85% 方差需 {out['pca']['k_for_85pct']} 个主成分（名义 11 维）")
    print(f"  E2 score_tech 首次实测: h5 {fmt(tech_h5)}（从未入权重，本次为其第一个 IC 证据）")
    print(f"  E3 合并损益: 见 Part D 三个口径对比——若 B 与 A 的 |IC| 差距在噪声内，")
    print(f"     则 5 维化是纯架构收益（可解释/可扩展/与外部证据对齐），非 alpha 提升")
    out["conclusion"] = {
        "k85": out["pca"]["k_for_85pct"],
        "tech_h5": tech_h5, "fund_h5": fund_h5,
    }

    with open(os.path.join(HERE, "audit_round20.json"), "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print("\n✅ 已写 audit_round20.json")


if __name__ == "__main__":
    main()
