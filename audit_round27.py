#!/usr/bin/env python3
"""round27：落地前验证 —— EP 替换 score_fund、SIZE_IND_NEUT 替换 score_size。

## 老大的裁决（2026-10-26）
  · EP（扣非净利TTM/总市值）→ **替换** score_fund
  · SIZE_IND_NEUT（行业内小市值）→ **替换** 现有 score_size

## 为什么必须先验证再动代码
项目铁律：任何权重/维度变更必须先有量化依据，且复合 IC 必须用**生产线同口径**
（量程归一化 `sum(min(v,DMAX)/DMAX*10*w)/max(0.5,sum(w))*10`）计算。
round24d 就是踩了「用 rank_pct 加权冒充生产线公式」的坑，绝对数值全部作废。

## 关键实现问题：EP / SIZE_IND_NEUT 没有既定 DMAX
生产线里 fund 是 0-5 关注制、quality/size/lowvol20 是 0-10 分位分。
EP / SIZE_IND_NEUT 原值是 Z-score（有正负、无固定量程）→ 无法直接进量程归一化公式。
**必须先按池内 rank 转成 0-10 分位分**（这是本项目已踩 3 次的「两层量程陷阱」）。

## 样本卡口
EP 覆盖 89.5%、SIZE_IND_NEUT 覆盖 75.2%，而 fund/size 接近 100%。
替换会缩小样本 → **所有方案必须在「EP 与 SIZE_IND_NEUT 同时有值」的子样本上比较**，
否则 IC 差异里混着样本变更效应。同时另给全池基线做参照。

## 基线一致性自检（强制）
本脚本重建的 v4.8 必须精确复现 round25 的 h5 +0.1776 / h20 +0.2484，偏差 > 0.02 则中止。

## 输出
  audit_round27.json
    baseline_check  基线自检结果（不通过则整个结论作废）
    cases           各方案的 h5/h20/h10/h40 IC + ICIR + NW t(0/3/6)
    ablation        单项替换 vs 双项替换 vs 权重再分配
    verdict         逐方案判定
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
HOPS = [5, 10, 20, 40]

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


def ic_stats(ics):
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

    # 两套 EM 加载器（round26 已验证的坑）：
    #   em_feishu（全 70 指标）→ 算 EP / SIZE_IND_NEUT
    #   em_base（round21 的 17 指标白名单）→ 复现 quality 维，保证基线可复现
    em_feishu = r26.load_em_all()
    em_base = r21.load_em_cache()

    scored = json.load(open(os.path.join(HERE, "top200_scored_2026-10-23.json"), encoding="utf-8"))
    sector_map = {(r.get("code") or "")[-6:]: r.get("sector_parent") for r in scored["results"]}

    print("=" * 100)
    print("round27：落地前验证 —— EP 替换 fund、SIZE_IND_NEUT 替换 size")
    print("=" * 100)
    print(f"截面 {len(snaps)} · 交易日 {len(tdays)} · 价格 {len(px)} 只")

    # ---------------- 股本重建（circ_mv 仅在最近 2 截面有值）
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

    # ---------------- 逐截面算 EP / SIZE_IND_NEUT 原始值（Z 之前）
    ep_raw = defaultdict(dict)      # date -> code -> EP 原值
    mv_by_date = defaultdict(dict)  # date -> code -> 流通市值（真实或重建）
    for s in snaps:
        d0 = s["date"]
        for r in s["rows"]:
            c6 = (r.get("code") or "")[-6:]
            ind = em_feishu.get(c6)
            if not ind:
                continue
            P = r21.pit_period(sorted({p for dd in ind.values() for p in dd}, reverse=True), d0)
            if P is None:
                continue
            f = r26.build_factors(ind, P, d0, r.get("price"), None, sector_map.get(c6), r)
            v = f.get("EP")
            if isinstance(v, (int, float)):
                ep_raw[d0][c6] = v
            mv = r.get("circ_mv")
            if not (isinstance(mv, (int, float)) and mv > 0):
                sh = shares_est.get(r.get("code"))
                pr = (px.get(c6) or {}).get(d0) or r.get("price")
                if sh and isinstance(pr, (int, float)) and pr > 0:
                    mv = sh * pr
            if isinstance(mv, (int, float)) and mv > 0:
                mv_by_date[d0][c6] = float(mv)

    # ⚠️ EP 与 SIZE_IND_NEUT 都是 Z/连续值 → 必须先转 0-10 池内分位分，
    #    否则进量程归一化公式会因量纲不同把复合分带偏（两层量程陷阱）
    def rank10(d):
        ks = sorted(d, key=lambda c: d[c])
        n = len(ks)
        if n < 2:
            return {}
        return {c: round((i / (n - 1)) * 10.0, 2) for i, c in enumerate(ks)}

    ep_score = {d: rank10(v) for d, v in ep_raw.items()}

    size_ind_score = {}
    for d0, mvd in mv_by_date.items():
        pairs = [(c, sector_map.get(c), v) for c, v in mvd.items()]
        szn, _ = r26.group_winsorize_zneg(pairs)
        # szn 是「−行业内 Z」，低市值高分 → 再转 0-10 分位保持与生产线分位族一致
        size_ind_score[d0] = rank10(szn) if len(szn) >= 20 else {}

    print(f"EP 覆盖截面 {len(ep_score)} · SIZE_IND_NEUT 覆盖截面 {len(size_ind_score)}")
    print(f"EP 末截面 {len(ep_score.get(snaps[-1]['date'], {}))} 只 · "
          f"SIZE_IND_NEUT 末截面 {len(size_ind_score.get(snaps[-1]['date'], {}))} 只")

    # ---------------- 复现现行 7 维（round26 已验证的路径）
    subs = defaultdict(dict)
    for s in snaps:
        d0 = s["date"]
        for r in s["rows"]:
            ind = em_base.get((r.get("code") or "")[-6:])
            if not ind:
                continue
            P = r21.pit_period(sorted({p for dd in ind.values() for p in dd}, reverse=True), d0)
            if P is None:
                continue
            subs[d0][r["code"]] = r21.sub_factors_pit(ind, P)

    bf = rb.backfill_reversal(tdays, px)

    def _rank10_asc(vals_by_code):
        ks = sorted(vals_by_code, key=lambda c: vals_by_code[c])
        n = len(ks)
        if n < 2:
            return {}
        return {c: round((i / (n - 1)) * 10.0, 2) for i, c in enumerate(ks)}

    qual_rank_by_date = {}
    for s in snaps:
        d0 = s["date"]
        q = {}
        for r in s["rows"]:
            sub = (subs.get(d0) or {}).get(r["code"])
            if sub:
                vq = r21.aggregate(sub, "grouped")
                if isinstance(vq, (int, float)):
                    q[r["code"]] = vq
        qual_rank_by_date[d0] = _rank10_asc(q)

    # size（小市值=高分，必须降序 —— round26 踩过的坑）
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
            return qual_rank_by_date.get(d0, {}).get(r.get("code"))
        if d == "score_size":
            return size_by_date.get(d0, {}).get(c6)
        if d == "score_lowvol20":
            return lv_by_date.get(d0, {}).get(r.get("code"))
        if d == "score_ep":
            return ep_score.get(d0, {}).get(c6)
        if d == "score_size_ind":
            return size_ind_score.get(d0, {}).get(c6)
        return None

    def fwd_ret(code6, d0, hop):
        m = px.get(code6)
        i0 = tidx.get(d0)
        if not m or i0 is None or i0 + hop >= len(tdays):
            return None
        p0, p1 = m.get(tdays[i0]), m.get(tdays[i0 + hop])
        if p0 is None or p1 is None or p0 <= 0:
            return None
        return p1 / p0 - 1.0

    def composite(r, wmap, d0):
        """与 step2 L4632-4652 逐字同口径（量程归一化）。"""
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

    # ============ 方案
    CASES = {
        "A v4.8 现行（fund.15 + size.10）": dict(W_V48),
        "B EP 替换 fund（EP.15）": {**W_V48, "score_fund": 0, "score_ep": 0.15},
        "C SIZE_IND_NEUT 替换 size（SZI.10）": {**W_V48, "score_size": 0, "score_size_ind": 0.10},
        "D 双替换（EP.15 + SZI.10）": {**W_V48, "score_fund": 0, "score_ep": 0.15,
                                        "score_size": 0, "score_size_ind": 0.10},
        "E EP 与 fund 各半（.075+.075）": {**W_V48, "score_fund": 0.075, "score_ep": 0.075},
        "F 双替换 + size 权重给 EP（EP.25 无size）": {**W_V48, "score_fund": 0, "score_ep": 0.25,
                                                    "score_size": 0},
        "G 双替换 + size 权重给低波（EP.15 低波.23）": {**W_V48, "score_fund": 0, "score_ep": 0.15,
                                                     "score_size": 0, "score_lowvol20": 0.23},
        "H 仅 fund 单维（对照）": {"score_fund": 1.0},
        "I 仅 EP 单维（对照）": {"score_ep": 1.0},
        "J 仅 size 单维（对照）": {"score_size": 1.0},
        "K 仅 SIZE_IND_NEUT 单维（对照）": {"score_size_ind": 1.0},
    }

    # 样本卡口：EP 与 SIZE_IND_NEUT 同时有值（覆盖最低的两项）
    def run_case(wmap, hop, gate=False):
        ics = []
        for s in snaps:
            d0 = s["date"]
            fv, rv = [], []
            for r in s["rows"]:
                c6 = (r.get("code") or "")[-6:]
                if gate:
                    if ep_score.get(d0, {}).get(c6) is None:
                        continue
                    if size_ind_score.get(d0, {}).get(c6) is None:
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
        return ic_stats(ics)

    out = {"round": "round27", "n_snapshots": len(snaps),
           "formula": "production scale-normalize", "cases": {}, "cases_fullpool": {}}

    # ---- 基线自检（全池，必须复现 round25）
    print("\n【自检】基线一致性（A 方案全池，须复现 round25 的 +0.1776 / +0.2484）")
    base_ok = {}
    for hop in (5, 20):
        st = run_case(CASES["A v4.8 现行（fund.15 + size.10）"], hop, gate=False)
        out["cases_fullpool"]["A|h%d" % hop] = st
        ref = {5: 0.1776, 20: 0.2484}[hop]
        got = (st or {}).get("ic")
        ok = got is not None and abs(got - ref) <= 0.02
        base_ok["h%d" % hop] = {"got": got, "ref": ref, "ok": ok}
        print("  h%-3d 本脚本=%s  round25=%.4f  偏差=%s  %s" % (
            hop, f"{got:+.4f}" if got is not None else "—", ref,
            f"{got - ref:+.4f}" if got is not None else "—", "OK" if ok else "⚠️ 超差"))
    out["baseline_check"] = {"detail": base_ok,
                             "passed": all(v["ok"] for v in base_ok.values())}
    if not out["baseline_check"]["passed"]:
        print("\n❌ 基线自检未过 → 中止，后续结论不可信")
        json.dump(out, open(os.path.join(HERE, "audit_round27.json"), "w", encoding="utf-8"),
                  ensure_ascii=False, indent=1)
        return

    # ---- 全池参照（不卡口）
    print("\n【全池】不卡口（各方最大样本，仅作参照，不可跨方案比差额）")
    print("%-40s %5s %8s %8s %7s %7s %7s %6s" %
          ("方案", "hop", "IC", "ICIR", "NWt0", "NWt3", "NWt6", "过Bonf"))
    for name in ("A v4.8 现行（fund.15 + size.10）", "B EP 替换 fund（EP.15）",
                 "E EP 与 fund 各半（.075+.075）",
                 "H 仅 fund 单维（对照）", "I 仅 EP 单维（对照）"):
        for hop in (5, 20):
            st = run_case(CASES[name], hop, gate=False)
            out["cases_fullpool"]["%s|h%d" % (name, hop)] = st
            if not st:
                continue
            print("%-40s %5d %8.4f %8.3f %7s %7s %7s %6s" % (
                name, hop, st["ic"], st["icir"] or 0, st["t0"], st["t3"], st["t6"],
                "YES" if st["pass_bonf"] else "-"))

    # ---- 统一卡口对比（核心结论）
    print("\n【核心】统一卡口（EP 与 SIZE_IND_NEUT 同时有值）—— 所有方案可在同一批股票上比")
    print("%-40s %5s %8s %8s %7s %7s %7s %8s" %
          ("方案", "hop", "IC", "ICIR", "NWt0", "NWt3", "NWt6", "IC>0"))
    baseA = {}
    for name in CASES:
        for hop in HOPS:
            st = run_case(CASES[name], hop, gate=True)
            out["cases"]["%s|h%d" % (name, hop)] = st
            if name.startswith("A ") and st:
                baseA[hop] = st["ic"]
    for name in CASES:
        for hop in HOPS:
            st = out["cases"].get("%s|h%d" % (name, hop))
            if not st:
                continue
            delta = ""
            if hop in baseA and baseA[hop] is not None:
                delta = "%+.4f" % (st["ic"] - baseA[hop])
            print("%-40s %5d %8.4f %8.3f %7s %7s %7s %6.1f%% %8s" % (
                name, hop, st["ic"], st["icir"] or 0, st["t0"], st["t3"], st["t6"],
                st["ic_pos_share"] * 100, delta))

    # ---- 判定
    print("\n【判定】相对基线 A 的 ΔIC（卡口口径）")
    verdict = {}
    for name in CASES:
        if name.startswith("A "):
            continue
        row = {}
        for hop in HOPS:
            st = out["cases"].get("%s|h%d" % (name, hop))
            b = baseA.get(hop)
            if st and b is not None:
                row["h%d" % hop] = round(st["ic"] - b, 4)
        verdict[name] = row
        print("  %-40s %s" % (name, "  ".join("%s=%+.4f" % (k, v) for k, v in row.items())))
    out["verdict_delta_vs_A"] = verdict

    json.dump(out, open(os.path.join(HERE, "audit_round27.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    print(f"\n→ 已写出 {os.path.join(HERE, 'audit_round27.json')}")


if __name__ == "__main__":
    main()
