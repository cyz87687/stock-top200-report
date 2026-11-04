#!/usr/bin/env python3
"""round27c：最终裁决 —— 每项替换用各自正确的样本卡口。

## 为什么还要再做一轮

round27 用「EP 与 SIZE_IND_NEUT 同时有值」做统一卡口，结果发现：
    EP 在卡口内 h5 IC = 0.0477，卡口外 = 0.2158
即卡口样本**不是随机子集**，它系统性排掉了 EP 最有效的部分 → 用这个卡口评判 EP 是错的。

round27b 已确立两件事：
  ① EP 在「EP 有值」的同样本上确实优于 fund（0.0986 vs 0.0746），但 t 值远低（2.86 vs 4.97）
  ② 行业内中性化在本池有效（h5 −0.0148 vs 全市场 −0.0638；h20 +0.0883 vs +0.0417）

## 本轮规则

**每项替换只在它自己的可用样本上评判，不与另一项交叉卡口。**

  对比 1（EP vs fund）：卡口 = EP 有值
  对比 2（SIZE_IND_NEUT vs size）：卡口 = SIZE_IND_NEUT 有值
  对比 3（双替换）：卡口 = EP 与 SZI 都有值（这才是保守下限，单独标注）

## 持有期重点

项目推荐持有 T+10。SIZE_IND_NEUT 的 alpha 集中在 h20（h5 为负）→
必须同时看 h10。
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
import audit_round26 as r26

T_BONF = 3.08
HOPS = [5, 10, 20]

DMAX = {"score_fund": 5.0, "score_quality": 10.0, "score_tech_neg": 10.0,
        "score_bias60": 5.0, "score_rsi_rev": 5.0, "score_size": 10.0,
        "score_lowvol20": 10.0, "score_ep": 10.0, "score_size_ind": 10.0}

W_V48 = {"score_fund": 0.15, "score_quality": 0.12, "score_lowvol20": 0.13,
         "score_tech_neg": 0.15, "score_bias60": 0.175, "score_rsi_rev": 0.175,
         "score_size": 0.10}


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
    em_f = r26.load_em_all()
    em_b = r21.load_em_cache()

    scored = json.load(open(os.path.join(HERE, "top200_scored_2026-10-23.json"), encoding="utf-8"))
    sector_map = {(r.get("code") or "")[-6:]: r.get("sector_parent") for r in scored["results"]}

    print("=" * 100)
    print("round27c：最终裁决（每项替换用各自正确的卡口）")
    print("=" * 100)

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
            ind = em_f.get(c6)
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
    szi_score = {}
    for d0, mvd in mv_by_date.items():
        pairs = [(c, sector_map.get(c), v) for c, v in mvd.items()]
        z, _ = r26.group_winsorize_zneg(pairs, min_grp=5)
        szi_score[d0] = rank10(z) if len(z) >= 20 else {}

    # 现行维重建
    subs = defaultdict(dict)
    for s in snaps:
        d0 = s["date"]
        for r in s["rows"]:
            ind = em_b.get((r.get("code") or "")[-6:])
            if not ind:
                continue
            P = r21.pit_period(sorted({p for dd in ind.values() for p in dd}, reverse=True), d0)
            if P is None:
                continue
            subs[d0][r["code"]] = r21.sub_factors_pit(ind, P)
    bf = rb.backfill_reversal(tdays, px)

    def _asc(d):
        ks = sorted(d, key=lambda c: d[c])
        n = len(ks)
        return {c: round((i / (n - 1)) * 10.0, 2) for i, c in enumerate(ks)} if n > 1 else {}

    qual_rank = {}
    for s in snaps:
        d0 = s["date"]
        q = {}
        for r in s["rows"]:
            sub = (subs.get(d0) or {}).get(r["code"])
            if sub:
                vq = r21.aggregate(sub, "grouped")
                if isinstance(vq, (int, float)):
                    q[r["code"]] = vq
        qual_rank[d0] = _asc(q)

    size_by_date = {}
    for s in snaps:
        d0 = s["date"]
        mvd = mv_by_date.get(d0) or {}
        if len(mvd) < 20:
            size_by_date[d0] = {}
            continue
        ks = sorted(mvd, key=lambda c: -mvd[c])
        n = len(ks)
        size_by_date[d0] = {c: round((i / (n - 1)) * 10.0, 2) for i, c in enumerate(ks)} if n > 1 else {}

    def lowvol_raw(code, d0):
        m = px.get(code)
        i0 = tidx.get(d0)
        if not m or i0 is None or i0 < 20:
            return None
        closes = []
        for j in range(i0 - 20, i0 + 1):
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
        n = len(raw)
        ks = sorted(raw, key=lambda c: -raw[c])
        lv_by_date[d0] = {c: round((i / (n - 1)) * 10.0, 2) for i, c in enumerate(ks)} if n > 1 else {}

    def dim_val(d, r, d0):
        c6 = (r.get("code") or "")[-6:]
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
            return qual_rank.get(d0, {}).get(r.get("code"))
        if d == "score_size":
            return size_by_date.get(d0, {}).get(c6)
        if d == "score_lowvol20":
            return lv_by_date.get(d0, {}).get(r.get("code"))
        if d == "score_ep":
            return ep_score.get(d0, {}).get(c6)
        if d == "score_size_ind":
            return szi_score.get(d0, {}).get(c6)
        return None

    def fwd_ret(c6, d0, hop):
        m = px.get(c6)
        i0 = tidx.get(d0)
        if not m or i0 is None or i0 + hop >= len(tdays):
            return None
        p0, p1 = m.get(tdays[i0]), m.get(tdays[i0 + hop])
        if p0 is None or p1 is None or p0 <= 0:
            return None
        return p1 / p0 - 1.0

    def composite(r, wmap, d0):
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

    def run(wmap, hop, gate):
        ics = []
        for s in snaps:
            d0 = s["date"]
            fv, rv = [], []
            for r in s["rows"]:
                c6 = (r.get("code") or "")[-6:]
                if gate and gate(d0, c6) is not True:
                    continue
                v = composite(r, wmap, d0)
                if v is None:
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

    out = {"round": "round27c", "n_snapshots": len(snaps), "results": {}}

    def has_ep(d, c):
        return (ep_raw.get(d) or {}).get(c) is not None

    def has_szi(d, c):
        return (szi_score.get(d) or {}).get(c) is not None

    def has_both(d, c):
        return has_ep(d, c) and has_szi(d, c)

    # ============ 对比 1：EP vs fund（卡口 = EP 有值）
    print("\n【对比 1】EP 替换 fund —— 卡口 = EP 有值（两边同一样本，这是评判 EP 的正确口径）")
    print("%-42s %5s %8s %8s %7s %7s %7s %8s" % ("方案", "hop", "IC", "ICIR", "NWt0", "NWt3", "NWt6", "过Bonf"))
    c1 = {
        "A v4.8 现行 fund.15": dict(W_V48),
        "B EP 替换 fund EP.15": {**W_V48, "score_fund": 0, "score_ep": 0.15},
        "E EP+fund 各半 .075/.075": {**W_V48, "score_fund": 0.075, "score_ep": 0.075},
    }
    base1 = {}
    for name, w in c1.items():
        for hop in HOPS:
            st = run(w, hop, has_ep)
            out["results"]["C1|%s|h%d" % (name, hop)] = st
            if name.startswith("A ") and st:
                base1[hop] = st["ic"]
    for name in c1:
        for hop in HOPS:
            st = out["results"].get("C1|%s|h%d" % (name, hop))
            if not st:
                continue
            dlt = "%+.4f" % (st["ic"] - base1[hop]) if hop in base1 else ""
            print("%-42s %5d %8.4f %8.3f %7s %7s %7s %6s %8s" % (
                name, hop, st["ic"], st["icir"] or 0, st["t0"], st["t3"], st["t6"],
                "YES" if st["pass_bonf"] else "-", dlt))

    # ============ 对比 2：SIZE_IND_NEUT vs size（卡口 = SZI 有值）
    print("\n【对比 2】SIZE_IND_NEUT 替换 size —— 卡口 = SIZE_IND_NEUT 有值")
    print("%-42s %5s %8s %8s %7s %7s %7s %8s" % ("方案", "hop", "IC", "ICIR", "NWt0", "NWt3", "NWt6", "过Bonf"))
    c2 = {
        "A v4.8 现行 size.10": dict(W_V48),
        "C SZI 替换 size SZI.10": {**W_V48, "score_size": 0, "score_size_ind": 0.10},
        "C2 去 size 维（不补 SZI）": {**W_V48, "score_size": 0},
    }
    base2 = {}
    for name, w in c2.items():
        for hop in HOPS:
            st = run(w, hop, has_szi)
            out["results"]["C2|%s|h%d" % (name, hop)] = st
            if name.startswith("A ") and st:
                base2[hop] = st["ic"]
    for name in c2:
        for hop in HOPS:
            st = out["results"].get("C2|%s|h%d" % (name, hop))
            if not st:
                continue
            dlt = "%+.4f" % (st["ic"] - base2[hop]) if hop in base2 else ""
            print("%-42s %5d %8.4f %8.3f %7s %7s %7s %6s %8s" % (
                name, hop, st["ic"], st["icir"] or 0, st["t0"], st["t3"], st["t6"],
                "YES" if st["pass_bonf"] else "-", dlt))

    # ============ 对比 3：双替换（卡口 = both，保守下限）
    print("\n【对比 3】双替换 —— 卡口 = EP 与 SZI 都有值（最保守，样本最小）")
    print("%-42s %5s %8s %8s %7s %7s %7s %8s" % ("方案", "hop", "IC", "ICIR", "NWt0", "NWt3", "NWt6", "过Bonf"))
    c3 = {
        "A v4.8 现行（双卡口）": dict(W_V48),
        "D 双替换 EP.15+SZI.10": {**W_V48, "score_fund": 0, "score_ep": 0.15,
                                   "score_size": 0, "score_size_ind": 0.10},
    }
    base3 = {}
    for name, w in c3.items():
        for hop in HOPS:
            st = run(w, hop, has_both)
            out["results"]["C3|%s|h%d" % (name, hop)] = st
            if name.startswith("A ") and st:
                base3[hop] = st["ic"]
    for name in c3:
        for hop in HOPS:
            st = out["results"].get("C3|%s|h%d" % (name, hop))
            if not st:
                continue
            dlt = "%+.4f" % (st["ic"] - base3[hop]) if hop in base3 else ""
            print("%-42s %5d %8.4f %8.3f %7s %7s %7s %6s %8s" % (
                name, hop, st["ic"], st["icir"] or 0, st["t0"], st["t3"], st["t6"],
                "YES" if st["pass_bonf"] else "-", dlt))

    # ============ 判定汇总
    print("\n" + "=" * 100)
    print("【判定汇总】")
    verdict = {}
    for tag, base, label in (("C1", base1, "EP 替换 fund"), ("C2", base2, "SZI 替换 size")):
        for hop in HOPS:
            k = "%s|B " % tag if False else None
        names = [n for n in (c1 if tag == "C1" else c2) if not n.startswith("A ")]
        for n in names:
            row = {}
            for hop in HOPS:
                st = out["results"].get("%s|%s|h%d" % (tag, n, hop))
                if st and hop in base:
                    row["h%d" % hop] = round(st["ic"] - base[hop], 4)
            verdict["%s / %s" % (label, n)] = row
    for k, row in verdict.items():
        improves = sum(1 for v in row.values() if v > 0)
        print("  %-46s %s  → %d/%d 档改善 %s" % (
            k, "  ".join("%s=%+.4f" % (kk, vv) for kk, vv in row.items()),
            improves, len(row), "✅" if improves == len(row) else ("⚠️" if improves else "❌")))
    out["verdict_delta"] = verdict

    json.dump(out, open(os.path.join(HERE, "audit_round27c.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    print(f"\n→ 已写出 {os.path.join(HERE, 'audit_round27c.json')}")


if __name__ == "__main__":
    main()
