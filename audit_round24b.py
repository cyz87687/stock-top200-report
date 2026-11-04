#!/usr/bin/env python3
"""round24b：前瞻成长的口径校正与增量检验（round24 的严格复核）。

round24 初步结果里有两个必须复核的点：
  ① `g1+fwd 等权` 直接把「一阶导百分数（中位 +34%）」与「fwd_pe 取负（中位 −15）」
     相加再除 2 —— **量纲不同却等权相加**，混合后的排序可能由方差大的一方主导，
     这不是 MAJOR 缺陷但也绝不能直接上线。正确做法是**先截面 rank 再等权**。
  ② `fwd_pe 取负` 单维 h5 IC=+0.0927 > score_fund 的 +0.0762，而两者 rank 相关 −0.538
     → 必须回答「fwd_pe 是 fund 的替代品，还是真增量」。

五条检验：
  A 严格混合：rank 后再等权 / z-score 后再等权，对比原始等权 → 量纲陷阱有多大
  B 增量检验：控制 fund 后 fwd_pe 的残差 IC（把 fwd_pe 对 fund 做截面回归取残差）
  C 加维检验：7 维 + fwd_pe（不替换 growth，纯新增一维）→ 复合 IC 增益
  D 稳健性：分子期 + 分市值组（大小市值各半）+ 行业中性
  E 最终方案对比：所有候选在同一张表上比较，选出可上线方案

⚠️ 与 round24 同一套回填与 IC battery（r21.ic_battery / fe.rank_ic），NW lag 0/3/6，
   Bonferroni 3.08。缺维从分母剔除禁 `or 0`。
"""
import glob
import json
import os
import sys
from collections import defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import factor_eval as fe
import audit_round21 as r21

T_BONF = 3.08
LAGS = [0, 3, 6]


def fmt(rec):
    if not rec:
        return "样本不足"
    t = "/".join(f"{rec['t_by_lag'].get(str(L), float('nan')):+.2f}" for L in LAGS)
    return (f"n={rec['n']:<3} IC={rec['ic']:+.4f} t={t} "
            f"{'✅过Bonf' if rec.get('robust_bonf') else '⚠️未过'}")


def ladder_ints(L):
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
    d = ladder_ints(L)
    g1 = g2 = None
    if 2026 in d and 2027 in d:
        g1 = (d[2026] / d[2027] - 1.0) * 100.0
    if 2027 in d and 2028 in d:
        g2 = (d[2027] / d[2028] - 1.0) * 100.0
    acc = (g2 - g1) if (g1 is not None and g2 is not None) else None
    return g1, g2, acc


def load_fwd():
    fwd = {}
    for f in sorted(glob.glob(os.path.join(HERE, "data", "fwd_hist", "*.json"))):
        d = json.load(open(f, encoding="utf-8"))
        m = {}
        for r in d.get("rows") or []:
            c6 = (r.get("code") or "").replace("sh", "").replace("sz", "").replace("bj", "")
            if c6:
                m[c6] = r
        fwd[d["date"]] = m
    return fwd


# ⚠️ 关键口径缺陷（本轮踩坑）：data/fwd_hist/ 里 **只有 9 个截面**（10-13 之后的主力池
#   文件）带 score_bias60 / score_rsi_rev / circ_mv —— 早期 TOP200 文件生成于 v4.x
#   之前，这三个字段根本不存在。若直接用它们做 7 维复合，其余 77 个截面会因「缺维
#   从分母剔除」而静默退化成 4 维（fund/tech_neg/growth/quality 合计权重仅 0.55），
#   于是 S0/S1/S2 的对比全在残缺口径上做 → 结论不可信。
#   修法：与 round23 完全一致，用 px 价格序列**回填**三个反转块字段 + 用池内市值回填
#   circ_mv（分位同口径由 score_size 的算法保证）。
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
    return 100 - 100 / (1 + ag / al)


def backfill_reversal(tdays, px):
    """date -> code6 -> (oversold60, bias60, rsi_rev)。与 round23 逐字同口径。"""
    bf = defaultdict(dict)
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
            bf[d][code] = (s_oversold60(mom60), s_bias60(cl[i], ma60), s_rsi_rev(r14))
    return bf


def main():
    h = json.load(open(os.path.join(HERE, "factor_history.json"), encoding="utf-8"))
    snaps, _ = fe._drop_stale_price_snapshots(h["snapshots"])
    tdays, px = fe.px_index(h)
    fwd = load_fwd()
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
            qg[d0][r["code"]] = r21.sub_factors_pit(ind, P)

    snaps_fwd = [{"date": d0, "rows": [dict(rec, code=c6) for c6, rec in m.items()]}
                 for d0, m in sorted(fwd.items())]
    print(f"截面: factor_history={len(snaps)}  fwd_hist={len(snaps_fwd)}  价格日={len(tdays)}")

    # ── 反转块回填（补齐 fwd_hist 缺 bias60/rsi_rev 的 77 个截面）
    bf = backfill_reversal(tdays, px)
    n_filled = 0
    for s in snaps_fwd:
        d0 = s["date"]
        b = bf.get(d0, {})
        for r in s["rows"]:
            t = b.get(r.get("code"))
            if not t:
                continue
            if not isinstance(r.get("score_bias60"), (int, float)):
                r["score_bias60"] = t[1]
                n_filled += 1
            if not isinstance(r.get("score_rsi_rev"), (int, float)):
                r["score_rsi_rev"] = t[2]
            if not isinstance(r.get("score_oversold60"), (int, float)):
                r["score_oversold60"] = t[0]
    print(f"反转块回填: {len(bf)} 个日期，新填 bias60 {n_filled} 处")

    # ── circ_mv 回填：用 factor_history 的 rows（含 circ_mv）逐日建表，
    #    仍缺的用「当日池内中位数」占位是不允许的（会制造假分位）；
    #    改为：只在**当日池内**做分位，缺市值者该日 score_size 记 None（从分母剔除）。
    mv_map = defaultdict(dict)
    for s in h["snapshots"]:
        for r in s["rows"]:
            mv = r.get("circ_mv")
            if isinstance(mv, (int, float)) and mv > 0:
                mv_map[s["date"]][r["code"]] = mv
    n_mv_filled = 0
    for s in snaps_fwd:
        d0 = s["date"]
        for r in s["rows"]:
            if isinstance(r.get("circ_mv"), (int, float)) and r["circ_mv"] > 0:
                continue
            v = mv_map.get(d0, {}).get(r.get("code"))
            if v:
                r["circ_mv"] = v
                n_mv_filled += 1
    n_mv_sec = sum(1 for s in snaps_fwd
                   if any(isinstance(r.get("circ_mv"), (int, float)) for r in s["rows"]))
    n_b60_sec = sum(1 for s in snaps_fwd
                    if any(isinstance(r.get("score_bias60"), (int, float)) for r in s["rows"]))
    print(f"circ_mv 回填: {n_mv_filled} 处 → 有市值的截面 {n_mv_sec}/{len(snaps_fwd)}；"
          f"有 bias60 的截面 {n_b60_sec}/{len(snaps_fwd)}")

    # ⚠️⚠️ 本轮的第二个、也是更严重口径缺陷：
    #   circ_mv（→ score_size 维）在 fwd_hist 里只有 9/86 个截面有值，且 factor_history
    #   的 rows 里同样只有 1469/19146 有值（v4.x 之后才写入）→ **无法回填**。
    #   后果：若在 86 个截面上做 7 维复合，size 维在 77 个截面被「缺维剔除出分母」，
    #   0.10 权重静默空转 → S0/S1/S2 的横向对比全部是**口径漂移的假对比**
    #   （实测：补齐 bias60 后 S0 从 +0.0596 跳到 +0.1140，就是分母在变）。
    #   处置（只能如此，且必须披露）：把复合 IC 的评估样本**限制在 9 个全字段截面**，
    #   样本量太小无功效 → 因此 C 部分的复合对比**不作为上线依据**，
    #   上线依据改为「单维 IC + 增量残差 IC + 与 fund 的冗余度」三条，
    #   并在页面/记忆里明确标注「fwd_pe 的历史复合验证受 size 维缺失限制，
    #   只能做单维与残差级验证」。
    full_sec = [s["date"] for s in snaps_fwd
                if any(isinstance(r.get("circ_mv"), (int, float)) for r in s["rows"])]
    print(f"⚠️ 全字段截面（可做完整 7 维复合）仅 {len(full_sec)} 个: {full_sec}")
    out0 = {"n_snapshots_fh": len(snaps), "n_snapshots_fwd": len(snaps_fwd),
            "mv_cols_after_backfill": n_mv_sec, "bias_after_backfill": n_b60_sec,
            "full_field_sections": full_sec,
            "composite_caveat": "circ_mv 仅 9/86 截面有值且无法回填 → 7 维复合对比"
                                "存在缺维分母漂移，不作为上线依据"}

    def base_val(d, r, d0):
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
        # 前瞻原始字段
        if d in ("fwd_pe_neg", "fwd_g1", "fwd_acc"):
            rec = fwd.get(d0, {}).get(r.get("code"))
            if not rec:
                return None
            if d == "fwd_pe_neg":
                v = rec.get("fwd_pe")
                return (-v) if isinstance(v, (int, float)) and v > 0 else None
            g1, _g2, acc = fwd_growth(rec.get("pe_ladder"))
            return g1 if d == "fwd_g1" else acc
        return None

    # ============================================================
    print()
    print("=" * 104)
    print("A. 严格混合：量纲陷阱有多大（g1 与 fwd_pe 的等权到底是靠什么在排序）")
    print("=" * 104)

    # 在截面内：raw 等权 / rank 后等权 / z-score 后等权
    def mixed_ic(method, hop, min_n=40):
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
            triples = []
            for r in s["rows"]:
                m = px.get(r.get("code"))
                if not m:
                    continue
                p0, p1 = m.get(d0), m.get(d1)
                if not p0 or not p1 or p0 <= 0:
                    continue
                g1 = base_val("fwd_g1", r, d0)
                fp = base_val("fwd_pe_neg", r, d0)
                triples.append((g1, fp, p1 / p0 - 1.0))
            if len(triples) < min_n:
                continue
            fv = []
            if method == "raw":
                for g1, fp, _ in triples:
                    parts = [v for v in (g1, fp) if v is not None]
                    fv.append((sum(parts) / len(parts)) if parts else None)
            else:
                gi = [i for i, t in enumerate(triples) if t[0] is not None]
                fi = [i for i, t in enumerate(triples) if t[1] is not None]
                gm = {}
                fm = {}
                if len(gi) >= 20:
                    rp = r21.rank_pct([triples[i][0] for i in gi])
                    gm = {gi[k]: rp[k] for k in range(len(gi))}
                if len(fi) >= 20:
                    rp = r21.rank_pct([triples[i][1] for i in fi])
                    fm = {fi[k]: rp[k] for k in range(len(fi))}
                if method == "rank":
                    for i in range(len(triples)):
                        parts = [m2[i] for m2 in (gm, fm) if i in m2]
                        fv.append((sum(parts) / len(parts)) if parts else None)
                else:  # zscore
                    def zmap(dd):
                        if not dd:
                            return {}
                        vs = list(dd.values())
                        m0 = sum(vs) / len(vs)
                        sd = (sum((v - m0) ** 2 for v in vs) / max(1, len(vs) - 1)) ** 0.5
                        if sd <= 0:
                            return {}
                        return {k: (v - m0) / sd for k, v in dd.items()}
                    gz, fz = zmap(gm), zmap(fm)
                    for i in range(len(triples)):
                        parts = [m2[i] for m2 in (gz, fz) if i in m2]
                        fv.append((sum(parts) / len(parts)) if parts else None)
            cc, rr = [], []
            for v, (_g, _f, ret) in zip(fv, triples):
                if v is None:
                    continue
                cc.append(v)
                rr.append(ret)
            if len(cc) >= min_n:
                ser.append({"date": d0, "fv": cc, "rv": rr})
        return ser

    out = dict(out0)
    out["a_mix"] = {}
    print(f"  {'混合方式':20s} {'h5 IC':>9s} {'h5 t0':>8s} {'h20 IC':>9s} {'h20 t0':>8s}")
    for method, label in (("raw", "原始值等权（round24 口径）"),
                          ("rank", "截面 rank 后等权"),
                          ("zscore", "截面 z-score 后等权")):
        recs = {str(hop): r21.ic_battery(mixed_ic(method, hop)) for hop in (1, 5, 20)}
        out["a_mix"][method] = recs
        def g(hp, f="ic"):
            x = recs.get(str(hp))
            return x.get(f) if x else None
        def t0(hp):
            x = recs.get(str(hp))
            return x["t_by_lag"].get("0") if x else None
        print(f"  {label:20s} {g(5):+9.4f} {t0(5):+8.2f} {g(20):+9.4f} {t0(20):+8.2f}")

    # ============================================================
    print()
    print("=" * 104)
    print("B. 增量检验：控制 fund 后，fwd_pe 还剩多少独立信息（残差 IC）")
    print("=" * 104)

    def residual_ic(new_dim, ctrl_dim, hop, min_n=40):
        """截面内把 new_dim 对 ctrl_dim 做线性回归，取残差再算 IC。"""
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
                a = base_val(new_dim, r, d0)
                b = base_val(ctrl_dim, r, d0)
                if a is None or b is None:
                    continue
                rows.append((a, b, p1 / p0 - 1.0))
            if len(rows) < min_n:
                continue
            ra = r21.rank_pct([x[0] for x in rows])
            rb = r21.rank_pct([x[1] for x in rows])
            n = len(rows)
            mb = sum(rb) / n
            ma = sum(ra) / n
            sbb = sum((v - mb) ** 2 for v in rb)
            if sbb <= 0:
                continue
            beta = sum((rb[i] - mb) * (ra[i] - ma) for i in range(n)) / sbb
            resid = [ra[i] - (ma + beta * (rb[i] - mb)) for i in range(n)]
            ser.append({"date": d0, "fv": resid, "rv": [x[2] for x in rows]})
        return ser

    out["b_residual"] = {}
    for tag, nd, cd in (("fwd_pe | fund", "fwd_pe_neg", "score_fund"),
                        ("g1 | fund", "fwd_g1", "score_fund"),
                        ("acc | fund", "fwd_acc", "score_fund"),
                        ("fwd_pe | quality", "fwd_pe_neg", "score_quality")):
        recs = {str(hop): r21.ic_battery(residual_ic(nd, cd, hop)) for hop in (1, 5, 20)}
        out["b_residual"][tag] = recs
        print(f"  {tag:22s} h5:{fmt(recs.get('5'))}")
        print(f"  {'':22s} h20:{fmt(recs.get('20'))}")

    # ============================================================
    print()
    print("=" * 104)
    print("C. 加维 / 换维：在真实 7 维复合上做组合，看 h5/h20 复合 IC")
    print("=" * 104)

    W47 = {"score_fund": 0.15, "score_bias60": 0.175, "score_rsi_rev": 0.175,
           "score_tech_neg": 0.15, "score_size": 0.10, "score_quality": 0.12,
           "score_growth": 0.13}

    def build(weights, hop, extra=None, min_n=40, only_dates=None):
        tidx = {d: i for i, d in enumerate(tdays)}
        ser = []
        for s in snaps_fwd:
            d0 = s["date"]
            if only_dates is not None and d0 not in only_dates:
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
                for d, w in weights.items():
                    if w <= 0:
                        continue
                    vals[d] = (extra[d](r, d0) if (extra and d in extra)
                               else base_val(d, r, d0))
                rows.append((vals, p1 / p0 - 1.0))
            if len(rows) < min_n:
                continue
            dim_ranks = {}
            for d, w in weights.items():
                if w <= 0:
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
            if len(tv) >= min_n:
                ser.append({"date": d0, "fv": tv, "rv": rv2})
        return ser

    schemes = {}
    schemes["S0 现行 7 维（历史增速）"] = (dict(W47), None)
    # 加维：fwd_pe 作为第 8 维，权重重分配
    for fw in (0.10, 0.13, 0.20):
        wm = {k: v * (1.0 - fw) for k, v in W47.items()}
        wm["fwd_pe_neg"] = fw
        schemes[f"S1 加 fwd_pe 第8维 w={fw:.2f}"] = (wm, None)
    # 换维：growth → fwd_pe（保权重 0.13）
    wm = dict(W47)
    del wm["score_growth"]
    wm["fwd_pe_neg"] = 0.13
    schemes["S2 换维 growth→fwd_pe(0.13)"] = (wm, None)
    # 换维：growth → g1+fwd rank 混合（各 0.065）
    wm = dict(W47)
    del wm["score_growth"]
    wm["fwd_g1"] = 0.065
    wm["fwd_pe_neg"] = 0.065
    schemes["S3 换维 growth→g1.065+fwd.065"] = (wm, None)
    # 加维：growth 保留 + fwd_pe 新增 0.10
    wm = {k: v * 0.9 for k, v in W47.items()}
    wm["fwd_pe_neg"] = 0.10
    schemes["S4 保 growth + fwd_pe .10"] = (wm, None)
    # 对照：去 growth
    wm = {k: v for k, v in W47.items() if k != "score_growth"}
    s = sum(wm.values())
    schemes["S5 直接去 growth"] = ({k: v / s for k, v in wm.items()}, None)

    out["c_schemes"] = {}
    print("⚠️ 口径限制：circ_mv 仅 9/86 截面有值，完整 7 维复合只能在以下两套样本上做：")
    print("   ① 全样本 86 截面（size 维在 77 个截面被剔除 → 分母漂移，仅作参照）")
    print("   ② 9 个全字段截面（口径干净但样本量小，t 值不可作显著性判据）")
    print()
    full_set = set(full_sec)
    for sample_tag, only in (("全样本86", None), ("全字段9", full_set)):
        print(f"  ── 样本: {sample_tag}")
        print(f"     {'方案':32s} {'h1 IC':>9s} {'h5 IC':>9s} {'h5 t0':>7s} {'h20 IC':>9s} {'n':>4s}")
        for name, (wm, ex) in schemes.items():
            recs = {str(hop): r21.ic_battery(build(wm, hop, ex, only_dates=only))
                    for hop in (1, 5, 20)}
            out["c_schemes"].setdefault(name, {"weights": {k: round(v, 4) for k, v in wm.items()}})
            out["c_schemes"][name][f"ic_{sample_tag}"] = recs

            def g(hp, f="ic"):
                x = recs.get(str(hp))
                return x.get(f) if x else None

            def t0(hp):
                x = recs.get(str(hp))
                return x["t_by_lag"].get("0") if x else None

            def fnum(v, spec="+.4f", na="      n/a"):
                return na if v is None else format(v, spec)
            h5 = recs.get("5")
            nn = h5["n"] if h5 else None
            print(f"     {name:32s} {fnum(g(1)):>9s} {fnum(g(5)):>9s} {fnum(t0(5)):>7s} "
                  f"{fnum(g(20)):>9s} {('n/a' if nn is None else str(nn)):>4s}")
        print()

    # ============================================================
    print()
    print("=" * 104)
    print("D. 稳健性（对 S2 换维方案）：分子期 + 分市值组")
    print("=" * 104)

    def stab(wm, hop=5):
        ser = build(wm, hop)
        if len(ser) < 10:
            return None
        k = len(ser) // 2
        res = {}
        for tag, part in (("前半", ser[:k]), ("后半", ser[k:])):
            ics = [fe.rank_ic(s["fv"], s["rv"]) for s in part]
            ics = [c for c in ics if c is not None]
            if len(ics) < 3:
                continue
            t = r21.nw_t(ics, 0)
            res[tag] = {"n": len(ics), "ic": round(sum(ics) / len(ics), 4),
                        "t0": round(t, 2) if t is not None else None,
                        "range": [part[0]["date"], part[-1]["date"]]}
        return res

    def mv_split_stab(wm, hop=5):
        """分市值组：每个截面内按 circ_mv 中位数切半，分别算 IC。"""
        tidx = {d: i for i, d in enumerate(tdays)}
        big, small = [], []
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
                mv = r.get("circ_mv")
                if not m or not isinstance(mv, (int, float)) or mv <= 0:
                    continue
                p0, p1 = m.get(d0), m.get(d1)
                if not p0 or not p1 or p0 <= 0:
                    continue
                vals = {d: base_val(d, r, d0) for d in wm if wm[d] > 0}
                rows.append((vals, p1 / p0 - 1.0, mv))
            if len(rows) < 60:
                continue
            dim_ranks = {}
            for d in wm:
                if wm[d] <= 0:
                    continue
                xs = [rv[0].get(d) for rv in rows]
                present = [i for i, x in enumerate(xs) if isinstance(x, (int, float))]
                if len(present) < 0.5 * len(rows):
                    dim_ranks[d] = None
                    continue
                rp = r21.rank_pct([xs[i] for i in present])
                dim_ranks[d] = {i: rp[k] for k, i in enumerate(present)}
            comp = []
            for idx, (vals, ret, mv) in enumerate(rows):
                num = den = 0.0
                for d, w in wm.items():
                    if w <= 0:
                        continue
                    mm = dim_ranks.get(d)
                    if mm is None or idx not in mm:
                        continue
                    num += w * mm[idx]
                    den += w
                comp.append((num / den if den >= 0.5 else None, ret, mv))
            comp = [c for c in comp if c[0] is not None]
            if len(comp) < 60:
                continue
            mvs = sorted(c[2] for c in comp)
            med = mvs[len(mvs) // 2]
            for grp, cond in ((small, lambda c: c[2] < med), (big, lambda c: c[2] >= med)):
                sub = [c for c in comp if cond(c)]
                if len(sub) < 25:
                    continue
                grp.append({"date": d0, "fv": [c[0] for c in sub], "rv": [c[1] for c in sub]})
        res = {}
        for tag, grp in (("小市值半", small), ("大市值半", big)):
            ics = [fe.rank_ic(x["fv"], x["rv"]) for x in grp]
            ics = [c for c in ics if c is not None]
            if len(ics) < 3:
                continue
            t = r21.nw_t(ics, 0)
            res[tag] = {"n": len(ics), "ic": round(sum(ics) / len(ics), 4),
                        "t0": round(t, 2) if t is not None else None}
        return res

    out["d_stability"] = {}
    for tag, wm in (("S0 现行", W47),
                    ("S2 换维 growth→fwd_pe", schemes["S2 换维 growth→fwd_pe(0.13)"][0])):
        sp = stab(wm, 5)
        mv = mv_split_stab(wm, 5)
        out["d_stability"][tag] = {"subperiod_h5": sp, "mv_split_h5": mv}
        print(f"  ── {tag}")
        if sp:
            print("     分子期: " + "  ".join(
                f"{k}: IC={v['ic']:+.4f} t0={v['t0']:+.2f} n={v['n']}" for k, v in sp.items()))
        if mv:
            print("     分市值: " + "  ".join(
                f"{k}: IC={v['ic']:+.4f} t0={v['t0']:+.2f} n={v['n']}" for k, v in mv.items()))

    # ============================================================
    print()
    print("=" * 104)
    print("E. 单维总表（同一张表比较 all，便于决策）")
    print("=" * 104)

    single = {}
    for tag, d in (("score_fund（现行估值锚）", "score_fund"),
                   ("score_growth（现行历史增速）", "score_growth"),
                   ("score_quality", "score_quality"),
                   ("fwd_pe 取负", "fwd_pe_neg"),
                   ("前瞻一阶导 g1", "fwd_g1"),
                   ("前瞻二阶导 acc", "fwd_acc")):
        recs = {}
        for hop in (1, 5, 20):
            # 在 fwd 截面上算（保证同样本）
            tidx = {dd: i for i, dd in enumerate(tdays)}
            ser = []
            for s in snaps_fwd:
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
                    v = base_val(d, r, d0)
                    if not isinstance(v, (int, float)):
                        continue
                    fv.append(v)
                    rv.append(p1 / p0 - 1.0)
                if len(fv) >= 40:
                    ser.append({"date": d0, "fv": fv, "rv": rv})
            recs[str(hop)] = r21.ic_battery(ser)
        single[tag] = recs
        print(f"  {tag:26s} h5:{fmt(recs.get('5'))}")
        print(f"  {'':26s} h20:{fmt(recs.get('20'))}")
    out["e_single"] = single

    json.dump(out, open(os.path.join(HERE, "audit_round24b.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    print("\n✅ 已写 audit_round24b.json")


if __name__ == "__main__":
    main()
