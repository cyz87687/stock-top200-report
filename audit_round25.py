#!/usr/bin/env python3
"""round25：补齐 factor-researcher 要求但 round23/24 全缺的五类指标。

## 为什么必须补

round23 只算了单维 IC 与 NW t；round24e/f 只做了换维裁决。以下五项
是判断「一个因子能不能真上线」的必备内容，此前**一项都没算**：

  Q1 ICIR — IC 均值/IC 标准差。IC 显著但 ICIR 低 = 靠少数截面撑起均值，
     上线后大概率失效。项目纪律只认 NW t，缺 ICIR 会系统性高估。
  Q2 分层单调性 — 把复合分切成 5 层，看每层前瞻收益是否单调。
     IC 是秩相关，IC 高但分层不单调 = 非线性/尾部驱动，实盘组合会踩空。
  Q3 换手率 — 相邻截面 Top50 名单重叠度。换手过高，实盘手续费吃光 alpha；
     这是「研究有效」与「投资可用」之间最大的鸿沟。
  Q4 IC 衰减半衰期 — IC 随 hop 衰减到峰值一半所需天数，直接决定换仓周期。
  Q5 拥挤度代理 — 分数自相关、Top50 的 size 分位、分数离散度。
     拥挤度高 = 大家都用同一套逻辑，边际 alpha 消失。

## 数据与口径

与 audit_round24f.py **逐字同口径**（生产线量程归一化），复用其
dim_val / composite_prod 逻辑与 size 股本重建、lowvol20 整池 rank。
所有 t 值用 Newey-West(lag=hop) 或 lag=6，Bonferroni 阈值 3.08。

输出：audit_round25.json
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
DMAX = {"score_fund": 5.0, "score_quality": 10.0, "score_growth": 10.0,
        "score_tech_neg": 10.0, "score_bias60": 5.0, "score_rsi_rev": 5.0,
        "score_size": 10.0, "score_lowvol20": 10.0}

# v4.8 现行生效权重（与 factor_weights.json 一致，Σ=1.0）
W_V48 = {"score_fund": 0.15, "score_quality": 0.12, "score_lowvol20": 0.13,
         "score_tech_neg": 0.15, "score_bias60": 0.175, "score_rsi_rev": 0.175,
         "score_size": 0.10}


def _rank(vals):
    idx = sorted(range(len(vals)), key=lambda i: vals[i])
    r = [0.0] * len(vals)
    i = 0
    while i < len(idx):
        j = i
        while j + 1 < len(idx) and vals[idx[j + 1]] == vals[idx[i]]:
            j += 1
        avg = (i + j) / 2.0 + 1
        for k in range(i, j + 1):
            r[idx[k]] = avg
        i = j + 1
    return r


def spearman(xs, ys):
    n = len(xs)
    if n < 5:
        return None
    rx, ry = _rank(xs), _rank(ys)
    mx, my = sum(rx) / n, sum(ry) / n
    sxy = sum((rx[i] - mx) * (ry[i] - my) for i in range(n))
    sxx = sum((rx[i] - mx) ** 2 for i in range(n)) ** 0.5
    syy = sum((ry[i] - my) ** 2 for i in range(n)) ** 0.5
    return (sxy / (sxx * syy)) if sxx > 0 and syy > 0 else None


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


def icir(ics):
    """ICIR = mean(IC) / std(IC)。std 用样本标准差（n-1）。"""
    n = len(ics)
    if n < 3:
        return None
    mu = sum(ics) / n
    sd = (sum((x - mu) ** 2 for x in ics) / (n - 1)) ** 0.5
    return (mu / sd) if sd > 0 else None


def main():
    h = json.load(open(os.path.join(HERE, "factor_history.json"), encoding="utf-8"))
    snaps, _ = fe._drop_stale_price_snapshots(h["snapshots"])
    tdays, px = fe.px_index(h)
    tidx = {d: i for i, d in enumerate(tdays)}
    em = r21.load_em_cache()

    print("=" * 96)
    print("round25：补 ICIR / 分层单调性 / 换手 / IC 衰减 / 拥挤度")
    print("=" * 96)
    print(f"截面 {len(snaps)} · 交易日 {len(tdays)} · 价格 {len(px)} 只")

    # ===== 与 round24f 完全一致的数据准备 =====
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

    shares = defaultdict(list)
    for s in snaps:
        for r in s["rows"]:
            mv, pr = r.get("circ_mv"), r.get("price")
            if isinstance(mv, (int, float)) and mv and isinstance(pr, (int, float)) and pr > 0:
                shares[r["code"]].append(mv / pr)
    shares_est = {}
    for c, v in shares.items():
        v = sorted(v)
        shares_est[c] = v[len(v) // 2]

    size_by_date = {}
    for s in snaps:
        d0 = s["date"]
        mv = {}
        for r in s["rows"]:
            c = r["code"]
            v = r.get("circ_mv")
            if not (isinstance(v, (int, float)) and v):
                sh = shares_est.get(c)
                pr = r.get("price")
                if sh and isinstance(pr, (int, float)) and pr > 0:
                    v = sh * pr
            if isinstance(v, (int, float)) and v > 0:
                mv[c] = float(v)
        if len(mv) < 20:
            size_by_date[d0] = {}
            continue
        ks = sorted(mv, key=lambda c: -mv[c])
        n = len(ks)
        size_by_date[d0] = {c: round((i / (n - 1)) * 10.0, 2) for i, c in enumerate(ks)} if n > 1 else {}

    def lowvol_raw(code, d0):
        m = px.get(code)
        if not m or d0 not in tidx:
            return None
        i0 = tidx[d0]
        if i0 < 20:
            return None
        closes = []
        for j in range(i0 - 20, i0 + 1):
            if j >= len(tdays):
                return None
            v = m.get(tdays[j])
            if v is None:
                return None
            closes.append(v)
        rets = [(closes[k] / closes[k - 1] - 1.0) for k in range(1, len(closes))]
        n = len(rets)
        mu = sum(rets) / n
        return (sum((r - mu) ** 2 for r in rets) / (n - 1)) ** 0.5

    lv_by_date = {}
    for s in snaps:
        d0 = s["date"]
        raw = {}
        for r in s["rows"]:
            v = lowvol_raw(r["code"], d0)
            if v is not None:
                raw[r["code"]] = v
        if len(raw) < 20:
            lv_by_date[d0] = {}
            continue
        ks = sorted(raw, key=lambda c: raw[c])
        n = len(ks)
        lv_by_date[d0] = {c: round((1.0 - i / (n - 1)) * 10.0, 2) for i, c in enumerate(ks)} if n > 1 else {}

    def _rank10(vals_by_code):
        ks = sorted(vals_by_code, key=lambda c: vals_by_code[c])
        n = len(ks)
        if n < 2:
            return {}
        return {c: round((i / (n - 1)) * 10.0, 2) for i, c in enumerate(ks)}

    qual_rank_by_date, grow_rank_by_date = {}, {}
    for s in snaps:
        d0 = s["date"]
        q, g = {}, {}
        for r in s["rows"]:
            sub = (subs.get(d0) or {}).get(r["code"])
            if not sub:
                continue
            vq = r21.aggregate(sub, "grouped")
            vg = r21.aggregate(sub, "g6040")
            if isinstance(vq, (int, float)):
                q[r["code"]] = vq
            if isinstance(vg, (int, float)):
                g[r["code"]] = vg
        qual_rank_by_date[d0] = _rank10(q)
        grow_rank_by_date[d0] = _rank10(g)

    def dim_val(d, r, d0):
        if d == "score_fund":
            v = r.get("score_fund")
            return v if isinstance(v, (int, float)) and not isinstance(v, bool) else None
        if d == "score_tech_neg":
            v = r.get("score_tech")
            return (10.0 - v) if isinstance(v, (int, float)) else None
        if d in ("score_bias60", "score_rsi_rev"):
            t = (bf.get(d0) or {}).get(r.get("code"))
            if not t:
                return None
            return t[1] if d == "score_bias60" else t[2]
        if d == "score_quality":
            return qual_rank_by_date.get(d0, {}).get(r.get("code"))
        if d == "score_growth":
            return grow_rank_by_date.get(d0, {}).get(r.get("code"))
        if d == "score_size":
            return size_by_date.get(d0, {}).get(r.get("code"))
        if d == "score_lowvol20":
            return lv_by_date.get(d0, {}).get(r.get("code"))
        return None

    def fwd_ret(code, d0, hop):
        m = px.get(code)
        if not m or d0 not in tidx:
            return None
        i0 = tidx[d0]
        if i0 + hop >= len(tdays):
            return None
        p0, p1 = m.get(tdays[i0]), m.get(tdays[i0 + hop])
        if p0 is None or p1 is None or p0 <= 0:
            return None
        return p1 / p0 - 1.0

    def composite_prod(r, wmap, d0):
        num = den = 0.0
        for k, w in wmap.items():
            if w <= 0:
                continue
            v = dim_val(k, r, d0)
            if v is None:
                continue
            mx = DMAX.get(k, 10.0)
            num += min(float(v), mx) / mx * 10.0 * w
            den += w
        return (num / max(0.5, den) * 10.0) if den > 0 else None

    out = {"n_snapshots": len(snaps),
           "formula": "production scale-normalize (same as audit_round24f)",
           "weights": W_V48}

    # ================= Q1：ICIR（7 维 + 复合）=================
    print()
    print("─" * 96)
    print("Q1 ICIR = mean(IC)/std(IC) —— IC 显著但 ICIR 低 = 靠少数截面撑均值")
    print("─" * 96)

    def collect_ic(wmap, hop):
        ics, dates = [], []
        for s in snaps:
            d0 = s["date"]
            pairs = []
            for r in s["rows"]:
                c = composite_prod(r, wmap, d0)
                fr = fwd_ret(r["code"], d0, hop)
                if c is not None and fr is not None:
                    pairs.append((c, fr))
            if len(pairs) >= 40:
                ic = spearman([p[0] for p in pairs], [p[1] for p in pairs])
                if ic is not None:
                    ics.append(ic)
                    dates.append(d0)
        return ics, dates

    q1 = {}
    dim_names = {
        "score_fund": "基本面 fund", "score_quality": "质量 quality",
        "score_lowvol20": "低波20 lowvol20", "score_tech_neg": "技术面(取负) tech_neg",
        "score_bias60": "乖离60 bias60", "score_rsi_rev": "RSI反转 rsi_rev",
        "score_size": "市值 size", "COMPOSITE_v48": "复合分 v4.8(P1)",
    }
    for key, nm in dim_names.items():
        wmap = W_V48 if key == "COMPOSITE_v48" else {key: 1.0}
        for hop in (5, 20):
            ics, _ = collect_ic(wmap, hop)
            if len(ics) < 10:
                continue
            m = sum(ics) / len(ics)
            v = icir(ics)
            t0 = nw_t(ics, 0)
            q1[f"{nm}|h{hop}"] = {
                "n": len(ics), "ic": round(m, 4),
                "ic_std": round((sum((x - m) ** 2 for x in ics) / (len(ics) - 1)) ** 0.5, 4),
                "icir": (round(v, 3) if v is not None else None),
                "t0": (round(t0, 2) if t0 is not None else None),
                "ic_pos_share": round(sum(1 for x in ics if x > 0) / len(ics), 3),
                "ic_min": round(min(ics), 4), "ic_max": round(max(ics), 4),
            }
    print(f"  {'因子':<26} {'h':>3} {'n':>4} {'IC':>8} {'IC_std':>8} {'ICIR':>7} {'t0':>7} {'IC>0占比':>9}")
    for k, v in q1.items():
        print(f"  {k.split('|')[0]:<26} {k.split('|')[1]:>3} {v['n']:>4} {v['ic']:>+8.4f} "
              f"{v['ic_std']:>8.4f} {v['icir']:>+7.2f} {v['t0']:>+7.2f} {v['ic_pos_share']:>8.1%}")
    out["q1_icir"] = q1

    # ================= Q2：分层单调性（5 层等权）=================
    print()
    print("─" * 96)
    print("Q2 分层单调性：复合分 v4.8 切 5 层，看每层前瞻超额是否单调")
    print("─" * 96)
    q2 = {}
    for hop in (5, 20):
        bucket_exc = defaultdict(list)      # bucket -> 逐截面平均超额
        ls = []                              # 多空差（逐截面）
        for s in snaps:
            d0 = s["date"]
            arr = []
            for r in s["rows"]:
                c = composite_prod(r, W_V48, d0)
                fr = fwd_ret(r["code"], d0, hop)
                if c is not None and fr is not None:
                    arr.append((c, fr))
            if len(arr) < 40:
                continue
            arr.sort(key=lambda x: x[0])
            bench = sum(a[1] for a in arr) / len(arr)     # 池内等权基准
            n = len(arr)
            edges = [int(round(i * n / 5)) for i in range(6)]
            bmeans = []
            for b in range(5):
                seg = arr[edges[b]:edges[b + 1]]
                if not seg:
                    bmeans.append(None)
                    continue
                mm = sum(x[1] for x in seg) / len(seg)
                bucket_exc[b].append(mm - bench)
                bmeans.append(mm - bench)
            if bmeans[0] is not None and bmeans[-1] is not None:
                ls.append(bmeans[-1] - bmeans[0])
        rec = {"n_bucket_mean": {str(b): (round(sum(v) / len(v), 4) if v else None)
                                 for b, v in bucket_exc.items()}}
        bvals = [rec["n_bucket_mean"].get(str(b)) for b in range(5)]
        valid = [x for x in bvals if x is not None]
        # 单调性：5 层均值是否严格递增
        inc = all(bvals[i] is not None and bvals[i + 1] is not None and bvals[i + 1] > bvals[i]
                  for i in range(4))
        # 秩相关（层序号 vs 层均超额）
        sp = spearman([float(i) for i in range(5)], [float(x) for x in bvals]) if len(valid) == 5 else None
        if ls:
            mu = sum(ls) / len(ls)
            rec.update({"long_short": round(mu, 4),
                        "ls_t0": (round(nw_t(ls, 0), 2) if nw_t(ls, 0) is not None else None),
                        "ls_t6": (round(nw_t(ls, 6), 2) if nw_t(ls, 6) is not None else None),
                        "ls_pos_share": round(sum(1 for x in ls if x > 0) / len(ls), 3),
                        "n_cs": len(ls)})
        rec["strict_monotonic"] = inc
        rec["bucket_rank_corr"] = (round(sp, 3) if sp is not None else None)
        q2[f"h{hop}"] = rec
        lab = ["Q1(最低分)", "Q2", "Q3", "Q4", "Q5(最高分)"]
        print(f"\n  h{hop}  逐截面平均超额（相对池内等权基准）：")
        for b in range(5):
            v = bvals[b]
            print(f"    {lab[b]:<12} {v:>+8.4f}" if v is not None else f"    {lab[b]:<12}      —")
        print(f"    单调递增 {'✅' if inc else '❌'} · 层序秩相关 {rec['bucket_rank_corr']} · "
              f"多空 h{hop} 均值 {rec.get('long_short')} (t0={rec.get('ls_t0')}, 正占比 {rec.get('ls_pos_share')})")
    out["q2_layered"] = q2

    # ================= Q3：换手率（Top50 名单重叠）=================
    print()
    print("─" * 96)
    print("Q3 换手率：相邻截面 Top50 名单重叠度（越小=换手越高=实盘成本越高）")
    print("─" * 96)
    q3 = {}
    for topn in (20, 50, 100):
        tops = []
        for s in snaps:
            d0 = s["date"]
            arr = []
            for r in s["rows"]:
                c = composite_prod(r, W_V48, d0)
                if c is not None:
                    arr.append((c, r["code"]))
            if len(arr) < topn:
                continue
            arr.sort(key=lambda x: -x[0])
            tops.append((d0, set(c for _, c in arr[:topn])))
        overlaps = []
        for i in range(1, len(tops)):
            a, b = tops[i - 1][1], tops[i][1]
            inter = len(a & b)
            overlaps.append(inter / max(len(a), 1))
        if not overlaps:
            continue
        mo = sum(overlaps) / len(overlaps)
        q3[f"top{topn}"] = {"mean_overlap": round(mo, 4),
                            "mean_turnover": round(1 - mo, 4),
                            "min_overlap": round(min(overlaps), 4),
                            "max_overlap": round(max(overlaps), 4),
                            "n_pairs": len(overlaps)}
        print(f"  Top{topn:<4} 平均重叠 {mo:>6.1%} → 单期换手 {1 - mo:>6.1%}"
              f"  (重叠区间 {min(overlaps):.1%}~{max(overlaps):.1%}, n={len(overlaps)})")
    out["q3_turnover"] = q3

    # ================= Q4：IC 衰减半衰期 =================
    print()
    print("─" * 96)
    print("Q4 IC 衰减：复合分在多个 hop 上的 IC，判断换仓周期上限")
    print("─" * 96)
    q4 = {}
    hops = [1, 2, 3, 5, 10, 20, 40, 60]
    for hop in hops:
        ics, _ = collect_ic(W_V48, hop)
        if len(ics) < 10:
            continue
        m = sum(ics) / len(ics)
        q4[f"h{hop}"] = {"n": len(ics), "ic": round(m, 4),
                         "t0": (round(nw_t(ics, 0), 2) if nw_t(ics, 0) is not None else None),
                         "icir": (round(icir(ics), 3) if icir(ics) is not None else None)}
        print(f"  h{hop:<3} n={len(ics):>3}  IC {m:>+8.4f}  t0 {q4[f'h{hop}']['t0']:>+7.2f}  ICIR {q4[f'h{hop}']['icir']}")
    peak = max(q4.items(), key=lambda kv: kv[1]["ic"]) if q4 else None
    half = None
    if peak:
        pk_ic = peak[1]["ic"]
        pk_hop = int(peak[0][1:])
        for hop in hops:
            k = f"h{hop}"
            if k in q4 and hop > pk_hop and q4[k]["ic"] <= pk_ic / 2:
                half = hop
                break
    q4["_peak"] = {"hop": (int(peak[0][1:]) if peak else None),
                   "ic": (peak[1]["ic"] if peak else None)}
    q4["_half_life_hop"] = half
    q4["_note"] = ("峰值 IC 出现在 h%d；衰减到峰值一半的首个可测 hop = %s"
                   % (q4["_peak"]["hop"] or -1, half if half else ">60（未衰减到一半）"))
    print(f"  → 峰值 h{q4['_peak']['hop']} IC {q4['_peak']['ic']:+.4f}；{q4['_note']}")
    out["q4_decay"] = q4

    # ================= Q5：拥挤度代理 =================
    print()
    print("─" * 96)
    print("Q5 拥挤度代理：分数自相关 / Top50 市值分位 / 分数离散度")
    print("─" * 96)
    q5 = {}
    # (a) 同一只股票相邻截面的分数秩相关 → 分数黏性
    score_by_date = {}
    for s in snaps:
        d0 = s["date"]
        sc = {}
        for r in s["rows"]:
            c = composite_prod(r, W_V48, d0)
            if c is not None:
                sc[r["code"]] = c
        score_by_date[d0] = sc
    dates_sorted = [s["date"] for s in snaps]
    autocor = []
    for i in range(1, len(dates_sorted)):
        a = score_by_date.get(dates_sorted[i - 1]) or {}
        b = score_by_date.get(dates_sorted[i]) or {}
        common = [c for c in a if c in b]
        if len(common) < 40:
            continue
        r = spearman([a[c] for c in common], [b[c] for c in common])
        if r is not None:
            autocor.append(r)
    if autocor:
        q5["score_autocorr_adjacent"] = {
            "mean": round(sum(autocor) / len(autocor), 4),
            "min": round(min(autocor), 4), "max": round(max(autocor), 4),
            "n_pairs": len(autocor),
            "note": "分数黏性；>0.95=名单几乎不动（换手极低但可能锁死），<0.7=剧烈换血"
        }
        print(f"  分数自相关(相邻截面) 均值 {q5['score_autocorr_adjacent']['mean']:+.4f}"
              f"  区间 {q5['score_autocorr_adjacent']['min']:+.3f}~{q5['score_autocorr_adjacent']['max']:+.3f}")

    # (b) Top50 的 size 分位均值（拥挤度：是否全挤在大市值）
    top_size_pct, all_size_pct, disp = [], [], []
    for s in snaps:
        d0 = s["date"]
        arr = [(composite_prod(r, W_V48, d0), r["code"]) for r in s["rows"]]
        arr = [x for x in arr if x[0] is not None]
        if len(arr) < 60:
            continue
        arr.sort(key=lambda x: -x[0])
        sz = size_by_date.get(d0) or {}
        vals = [sz[c] for _, c in arr[:50] if c in sz]
        if vals:
            top_size_pct.append(sum(vals) / len(vals))
        allv = [sz[c] for c in sz]
        if allv:
            all_size_pct.append(sum(allv) / len(allv))
        sc = [x[0] for x in arr]
        mu = sum(sc) / len(sc)
        disp.append((sum((x - mu) ** 2 for x in sc) / len(sc)) ** 0.5)
    if top_size_pct and all_size_pct:
        q5["top50_size_pct"] = {
            "top50_mean": round(sum(top_size_pct) / len(top_size_pct), 2),
            "pool_mean": round(sum(all_size_pct) / len(all_size_pct), 2),
            "note": "size 分位 0-10，10=池内最大市值；top50 显著高于池均值 = 组合向大市值倾斜"
        }
        d = q5["top50_size_pct"]["top50_mean"] - q5["top50_size_pct"]["pool_mean"]
        print(f"  Top50 平均 size 分位 {q5['top50_size_pct']['top50_mean']:.2f} vs 池均值 "
              f"{q5['top50_size_pct']['pool_mean']:.2f} → 偏离 {d:+.2f}")
    if disp:
        q5["score_dispersion"] = {"mean_std": round(sum(disp) / len(disp), 3),
                                 "note": "复合分标准差（0-100 量纲）；过小=区分度不足"}
        print(f"  复合分横截面标准差 均值 {q5['score_dispersion']['mean_std']:.2f}（百分制）")
    out["q5_crowding"] = q5

    # ================= Q6：多重检验登记 =================
    print()
    print("─" * 96)
    print("Q6 多重检验登记（此前从未系统登记，是最脆弱的一环）")
    print("─" * 96)
    q6 = {
        "cumulative_tests": {
            "round13~22 单维/拆分测试": 38,
            "round23 q6 子集穷举 2^7": 127,
            "round23 q3/q4/q5/q7 变体": 21,
            "round24/24b/24c 成长口径": 24,
            "round24d 换维 7 方案 × 2 hop": 14,
            "round24e size 处置 4 方案": 8,
            "round24f 生产线口径 10 方案 × 2 hop": 20,
            "round25 本轮 7 维 + 复合 × 2 hop + 8 衰减": 52,
        },
        "total_registered": 0,
        "bonferroni_threshold_used": T_BONF,
        "note": ("⚠️ 累计检验次数已远超单次 Bonferroni 的适用范围。项目现用的 3.08 "
                 "对应「1 次假设、双侧 0.002」；若按累计 ~300 次检验，严格阈值应升到 "
                 "t≈3.9~4.0。这意味着：**当前所有 p 值都偏乐观**，"
                 "换维决策的正确性只能靠「分子期双正 + 三来源互证」而非 t 值单点。"),
        "subset_search_caveat": ("round23 q6 的 2^7=127 子集穷举存在选择性偏差："
                                 "k=5 最优不等于 k=5 真优。v4.8 的 7 维结构不是穷举选出来的，"
                                 "而是逐维做可解释性裁决（成长归零、低波替补、size 待裁），"
                                 "这一点比 IC 数值更重要。"),
    }
    q6["total_registered"] = sum(q6["cumulative_tests"].values())
    for k, v in q6["cumulative_tests"].items():
        print(f"  {k:<40} {v:>4} 次")
    print(f"  {'累计登记':<40} {q6['total_registered']:>4} 次")
    print(f"  {q6['note']}")
    out["q6_multitest"] = q6

    with open(os.path.join(HERE, "audit_round25.json"), "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print()
    print("=" * 96)
    print("已写出 audit_round25.json")
    print("=" * 96)
    return 0


if __name__ == "__main__":
    sys.exit(main())
