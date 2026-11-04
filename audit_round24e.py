#!/usr/bin/env python3
"""round24e：补齐 round24d 的缺口 —— 七维**含 size** 口径的多截面前瞻验证。

为什么必须补这一轮：
  round24d 的复合对比（K0/K1/K2/K3/K2+LV）全部**排除 size 维**，理由是
  circ_mv 只在 9/86 截面有值、带 size 会因「缺维剔分母」静默漂移。
  但 v4.8 实际上线的是**七维含 size 0.10**。也就是说：
    · 已验证的：六维（不含 size）+ 低波 0.13 → h5 IC +0.1709 / t0=10.05 ✅
    · 要上线的：七维（含 size 0.10）+ 低波 0.13 → **从未验证**
  而单截面已发现 lowvol20 与 size 秩相关 −0.617（控制其他五维后 −0.632），
  两维方向互斥（低波股=大市值股），合成后可能互相抵消。

本脚本做三件事：
  E1 重建历史 size 维：用 circ_mv（有值截面）+ 当日收盘价反推股本，
     再用「股本 × 当日价 = 当日流通市值」逐截面重建 → 覆盖率从 8% 提到接近 100%。
  E2 在同一套 78 截面上，把「六维口径」与「七维含 size 口径」逐项对照，
     确认 size 维到底是加分还是抵消。
  E3 若确认抵消，测三个处置：size 保持 0.10 / size 降到 0.05 / size 归零（权重让给低波）。

⚠️ 口径纪律：NW t lag 0/3/6，Bonferroni 3.08；缺维从分母剔除（禁 or 0）；
   价格覆盖与 hop 索引严格 > d0（无前视）。
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
# 量程（库里原始量程）：fund/bias60/rsi_rev 为 0-5，其余 0-10
DMAX = {"score_fund": 5.0, "score_quality": 10.0, "score_growth": 10.0,
        "score_tech_neg": 10.0, "score_bias60": 5.0, "score_rsi_rev": 5.0,
        "score_size": 10.0, "score_lowvol20": 10.0}


# ===== 统计工具（与 round24b 同口径，逐字一致避免跨轮不可比）=====
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


def nw_t(ics, lag):
    """Newey-West t（lag=h−1 口径，与 round23/24 一致）。"""
    n = len(ics)
    if n < 5:
        return None
    mu = sum(ics) / n
    d = [x - mu for x in ics]
    g0 = sum(x * x for x in d) / n
    var = g0
    for L in range(1, min(lag, n - 1) + 1):
        gl = sum(d[i] * d[i - L] for i in range(L, n)) / n
        var += 2 * (1 - L / (lag + 1)) * gl
    se = math.sqrt(var / n) if var > 0 else None
    return (mu / se) if se else None


def ic_rec(pairs):
    """pairs = [(feature, fwd)] → {ic, n, t_by_lag, robust_bonf}"""
    if len(pairs) < 20:
        return None
    xs = [p[0] for p in pairs]
    ys = [p[1] for p in pairs]
    ic = spearman(xs, ys)
    if ic is None:
        return None
    t = {str(L): nw_t([ic], L) for L in LAGS}   # 单值序列无法算 NW → 用聚合序列
    return {"n": len(pairs), "ic": ic, "t_by_lag": t}


def fmt(rec):
    if not rec:
        return "样本不足"
    t = "/".join(f"{rec['t_by_lag'].get(str(L)) or float('nan'):+.2f}" for L in LAGS)
    return (f"n={rec['n']:<3} IC={rec['ic']:+.4f} t={t} "
            f"{'✅过Bonf' if rec.get('robust_bonf') else '⚠️未过'}")


def main():
    h = json.load(open(os.path.join(HERE, "factor_history.json"), encoding="utf-8"))
    snaps, _ = fe._drop_stale_price_snapshots(h["snapshots"])
    tdays, px = fe.px_index(h)
    tidx = {d: i for i, d in enumerate(tdays)}
    em = r21.load_em_cache()

    print("=" * 84)
    print("round24e：七维含 size 口径的多截面验证（补 round24d 缺口）")
    print("=" * 84)
    print(f"截面 {len(snaps)} · 交易日 {len(tdays)} · 价格序列 {len(px)} 只")

    out = {"n_snapshots": len(snaps)}

    # ===== E0：重建股本表（shares = circ_mv / price）=====
    # 同一只股票在不同截面若都有 circ_mv，股本应基本恒定（除非送转）；
    # 取中位数作为该股股本估计，再乘以各截面当日价 → 当日流通市值。
    shares = defaultdict(list)
    for s in snaps:
        for r in s["rows"]:
            mv, pr = r.get("circ_mv"), r.get("price")
            if isinstance(mv, (int, float)) and mv and isinstance(pr, (int, float)) and pr > 0:
                shares[r["code"]].append(mv / pr)
    shares_est = {}
    for c, v in shares.items():
        v = sorted(v)
        shares_est[c] = v[len(v) // 2]         # 中位数抗送转跳变
    print(f"E0 股本估计：{len(shares_est)} 只（有 circ_mv 的股票）；"
          f"平均每股有 {sum(len(v) for v in shares.values())/max(1,len(shares)):.1f} 个观测")

    # ===== E1：逐截面重建 size 维 =====
    def size_score_map(d0, rows):
        """当日全池 → score_size（0-10 池内分位，市值越小越高分）。
        重建口径与 step2 一致：池内 rank 分位 ×10，导数为高。
        返回 {code: score} 与覆盖率。"""
        mv = {}
        for r in rows:
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
            return {}, 0.0
        ks = sorted(mv, key=lambda c: -mv[c])      # 市值从大到小
        n = len(ks)
        out_map = {}
        for i, c in enumerate(ks):
            # 市值最大 → 分位 0 分；最小 → 10 分
            out_map[c] = round((i / (n - 1)) * 10.0, 2) if n > 1 else 5.0
        return out_map, len(mv) / len(rows) if rows else 0.0

    cov_list = []
    size_by_date = {}
    for s in snaps:
        d0 = s["date"]
        mp, cov = size_score_map(d0, s["rows"])
        size_by_date[d0] = mp
        cov_list.append((d0, cov, len(mp), len(s["rows"])))
    cv = [c for _, c, _, _ in cov_list if c > 0]
    print(f"E1 size 维重建覆盖率：均值 {sum(cv)/len(cv):.1%}  "
          f"最低 {min(cv):.1%}  最高 {max(cv):.1%}  （原 circ_mv 直采仅 8.26%）")
    out["e1_size_coverage"] = {
        "mean": round(sum(cv)/len(cv), 4), "min": round(min(cv), 4),
        "max": round(max(cv), 4), "n_dates": len(cv)}

    # ===== lowvol20（与 round24d 同实现）=====
    def lowvol20(code, d0):
        m = px.get(code)
        if not m or d0 not in tidx:
            return None
        i0 = tidx[d0]
        if i0 < 20:
            return None
        closes = []
        for j in range(i0 - 20, i0 + 1):        # 含 d0，无前视
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

    # 逐截面 lowvol 分位分
    lv_rank_by_date = {}
    for s in snaps:
        d0 = s["date"]
        raw = {}
        for r in s["rows"]:
            v = lowvol20(r["code"], d0)
            if v is not None:
                raw[r["code"]] = v
        if len(raw) < 20:
            lv_rank_by_date[d0] = {}
            continue
        ks = sorted(raw, key=lambda c: raw[c])   # 波动从低到高
        n = len(ks)
        lv_rank_by_date[d0] = {c: round((1.0 - i / (n - 1)) * 10.0, 2) for i, c in enumerate(ks)} if n > 1 else {}
    lvc = [len(v) / max(1, len(next(s["rows"] for s in snaps if s["date"] == d))) if False else 0 for d, v in lv_rank_by_date.items()]
    n_lv = sum(len(v) for v in lv_rank_by_date.values())
    print(f"E1 lowvol20 分位分：{n_lv} 股票日 · 非空截面 {sum(1 for v in lv_rank_by_date.values() if v)}/{len(snaps)}")

    # ===== 反转块回填（round23/24 同实现，否则 bias/rsi 只在 9 截面有值）=====
    bf = rb.backfill_reversal(tdays, px)
    print(f"E1 反转块回填：{len(bf)} 日")

    # ===== 前瞻收益（hop 5/20，严格 > d0）=====
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

    # ===== E2：六维 vs 七维含 size =====
    print()
    print("─" * 84)
    print("E2  同一套截面，六维口径 vs 七维含 size 口径")
    W6 = {"score_fund": 0.15, "score_quality": 0.12, "score_tech_neg": 0.15,
          "score_bias60": 0.175, "score_rsi_rev": 0.175, "score_lowvol20": 0.13}
    W7 = dict(W6)
    W7["score_size"] = 0.10
    W7n = {k: v / sum(W7.values()) for k, v in W7.items()}   # 归一
    W6n = {k: v / sum(W6.values()) for k, v in W6.items()}

    def get_dim(r, k, d0):
        if k == "score_lowvol20":
            return lv_rank_by_date.get(d0, {}).get(r["code"])
        if k == "score_size":
            return size_by_date.get(d0, {}).get(r["code"])
        v = r.get(k)
        if k in ("score_bias60", "score_rsi_rev"):
            # 优先用回填值
            b = bf.get(d0, {}).get(r["code"])
            if isinstance(b, dict):
                v = b.get(k.split("_")[1]) if False else b.get(k, v)
        return v if isinstance(v, (int, float)) and not isinstance(v, bool) else None

    def composite(r, wmap, d0):
        num = den = 0.0
        for k, w in wmap.items():
            v = get_dim(r, k, d0)
            if v is None:
                continue                        # 缺维剔分母
            num += (float(v) / DMAX[k] * 10.0) * w
            den += w
        return (num / den * 10.0) if den > 0 else None

    results = {}
    for hop in (5, 20):
        for lbl, wmap in (("六维（不含size，低波0.13）", W6n),
                          ("七维（含size0.10，低波0.13）", W7n)):
            ics = []
            npairs = 0
            for s in snaps:
                d0 = s["date"]
                pairs = []
                for r in s["rows"]:
                    c = composite(r, wmap, d0)
                    fr = fwd_ret(r["code"], d0, hop)
                    if c is not None and fr is not None:
                        pairs.append((c, fr))
                if len(pairs) >= 20:
                    ic = spearman([p[0] for p in pairs], [p[1] for p in pairs])
                    if ic is not None:
                        ics.append(ic)
                        npairs += len(pairs)
            if len(ics) >= 10:
                mean_ic = sum(ics) / len(ics)
                ts = {str(L): nw_t(ics, L) for L in LAGS}
                rob = all((ts.get(str(L)) or 0) >= T_BONF for L in LAGS)
                rec = {"n": len(ics), "ic": round(mean_ic, 4),
                       "t_by_lag": {k: (round(v, 2) if v is not None else None) for k, v in ts.items()},
                       "robust_bonf": rob, "n_obs": npairs}
                results[f"h{hop}|{lbl}"] = rec
                print(f"  h{hop:<2} {lbl:<30} {fmt(rec)}")

    # ===== E3：size 处置三方案 =====
    print()
    print("─" * 84)
    print("E3  size 维处置对比（低波固定 0.13，其余五维不动）")
    BASE5 = {"score_fund": 0.15, "score_quality": 0.12, "score_tech_neg": 0.15,
             "score_bias60": 0.175, "score_rsi_rev": 0.175}
    CAND = {
        "A size 0.10（=要上线方案）": {**BASE5, "score_lowvol20": 0.13, "score_size": 0.10},
        "B size 0.05（降权）": {**BASE5, "score_lowvol20": 0.13, "score_size": 0.05},
        "C size 0（归零，让给低波0.23）": {**BASE5, "score_lowvol20": 0.23},
        "D size 0.10 + 低波0.08（双低权）": {**BASE5, "score_lowvol20": 0.08, "score_size": 0.10},
    }
    for nm, wmap in CAND.items():
        wn = {k: v / sum(wmap.values()) for k, v in wmap.items()}
        for hop in (5, 20):
            ics = []
            for s in snaps:
                d0 = s["date"]
                pairs = []
                for r in s["rows"]:
                    c = composite(r, wn, d0)
                    fr = fwd_ret(r["code"], d0, hop)
                    if c is not None and fr is not None:
                        pairs.append((c, fr))
                if len(pairs) >= 20:
                    ic = spearman([p[0] for p in pairs], [p[1] for p in pairs])
                    if ic is not None:
                        ics.append(ic)
            if len(ics) >= 10:
                mean_ic = sum(ics) / len(ics)
                ts = {str(L): nw_t(ics, L) for L in LAGS}
                rob = all((ts.get(str(L)) or 0) >= T_BONF for L in LAGS)
                rec = {"n": len(ics), "ic": round(mean_ic, 4),
                       "t_by_lag": {k: (round(v, 2) if v is not None else None) for k, v in ts.items()},
                       "robust_bonf": rob}
                results[f"AM|h{hop}|{nm}"] = rec
                print(f"  h{hop:<2} {nm:<32} {fmt(rec)}")

    # ===== E4：size 与 lowvol 的截面相关性（多截面统计）=====
    print()
    print("─" * 84)
    print("E4  size × lowvol20 跨截面相关性（单截面 −0.617 是否稳定）")
    cs = []
    for s in snaps:
        d0 = s["date"]
        sv = size_by_date.get(d0, {})
        lv = lv_rank_by_date.get(d0, {})
        common = [c for c in sv if c in lv]
        if len(common) < 40:
            continue
        r = spearman([lv[c] for c in common], [sv[c] for c in common])
        if r is not None:
            cs.append((d0, r, len(common)))
    if cs:
        rs = [x[1] for x in cs]
        mu = sum(rs) / len(rs)
        sd = (sum((x - mu) ** 2 for x in rs) / (len(rs) - 1)) ** 0.5
        neg = sum(1 for x in rs if x < -0.3)
        print(f"  截面数 {len(cs)}  均值 r = {mu:+.3f}  标准差 {sd:.3f}  "
              f"min {min(rs):+.3f}  max {max(rs):+.3f}")
        print(f"  r < −0.3 的截面：{neg}/{len(rs)} = {neg/len(rs):.1%}")
        print(f"  → {'✅ 跨截面稳定负相关，两维方向互斥' if neg/len(rs) > 0.7 else '⚠️ 稳定性不足'}")
        out["e4_size_lowvol_corr"] = {"n": len(cs), "mean": round(mu, 4),
                                      "sd": round(sd, 4), "neg30_share": round(neg/len(rs), 4),
                                      "min": round(min(rs), 4), "max": round(max(rs), 4)}

    # ===== E5：size 单维 IC（用重建后的全覆盖 size）=====
    print()
    print("─" * 84)
    print("E5  size 单维 IC（重建后全覆盖，对比 round23 的 9 截面旧结论）")
    for hop in (5, 20):
        ics = []
        for s in snaps:
            d0 = s["date"]
            sv = size_by_date.get(d0, {})
            pairs = []
            for r in s["rows"]:
                v = sv.get(r["code"])
                fr = fwd_ret(r["code"], d0, hop)
                if v is not None and fr is not None:
                    pairs.append((v, fr))
            if len(pairs) >= 20:
                ic = spearman([p[0] for p in pairs], [p[1] for p in pairs])
                if ic is not None:
                    ics.append(ic)
        if len(ics) >= 10:
            mean_ic = sum(ics) / len(ics)
            ts = {str(L): nw_t(ics, L) for L in LAGS}
            rob = all((ts.get(str(L)) or 0) >= T_BONF for L in LAGS)
            rec = {"n": len(ics), "ic": round(mean_ic, 4),
                   "t_by_lag": {k: (round(v, 2) if v is not None else None) for k, v in ts.items()},
                   "robust_bonf": rob}
            results[f"SIZE|h{hop}"] = rec
            print(f"  h{hop:<2} score_size（重建，覆盖≈全池）    {fmt(rec)}")

    out["results"] = results
    out["note"] = ("round24e 补齐 round24d 缺测的 size 维。"
                   "size 用 circ_mv/price 反推股本后逐截面重建，覆盖率见 e1。"
                   "所有 IC 为截面 Spearman 均值的 NW t（lag 0/3/6），Bonferroni 门槛 3.08。")
    with open(os.path.join(HERE, "audit_round24e.json"), "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print()
    print("=" * 84)
    print("已写出 audit_round24e.json")
    print("=" * 84)
    return 0


if __name__ == "__main__":
    sys.exit(main())
