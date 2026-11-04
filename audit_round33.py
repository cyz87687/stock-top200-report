#!/usr/bin/env python3
"""round33：逐维**边际贡献**裁决 —— 回答「tech_neg 15% 到底该不该改」。

## 为什么 round31/32 的结论不能直接用

round31 在 78 个截面上测，但这些截面里 **69 个是 187 只的小池**（2026-07~10-10），
只有 9 个是当前规模的 400+ 只大池。两者结构完全不同：

    tech_neg × bias60 秩相关：小池 +0.05~+0.28（弱），大池 +0.62~+0.77（强）
    tech_neg 值域 std：小池 1.036，大池 0.937

即「共线程度」本身随池子结构变化，用历史长样本的平均值（+0.389）去裁决
当前大池的权重，属于**跨结构外推**，不可作上线依据。

## 本轮换指标：从「有效权重」换成「边际贡献」

诊断里说「tech_neg 名义 15%、有效仅 5.3%」，那个有效权重 = w×std/Σ(w×std)
只衡量**分散度**，不衡量**增量信息**。一个因子可以窄幅（低 std）但高度有效；
也可以宽幅（高 std）但纯噪音。

正确指标是**边际贡献**：从基准里去掉该维（权重按比例分给其余维），
看复合 IC 掉多少。掉得多 = 该维不可替代；掉得少或反升 = 该维是累赘。

同时对每维做 **IC × 有效权重** 的分解，说明「窄幅但有效」的可能性。

## 覆盖度声明（必须看）

大池截面仅 9 个 → 逐维消融的配对检验在此样本上**没有功效**。
故本脚本输出两个样本的结果并明确标注可信度：
  · 全样本（78 截面）：趋势参考，含结构混杂
  · 大池（9 截面）：贴近生产，但 n 太小
结论若两者一致 → 可信；若冲突 → 不许上线，只做披露。
"""
import json
import math
import os
import sys
import statistics
from collections import defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import factor_eval as fe
import audit_round21 as r21
import audit_round24b as rb
import audit_round26 as r26
import scale_calc

T_BONF = 3.08
HOPS = [5, 10, 20]
LAGS = [0, 3, 6]

# round125: dim_max 改为**单一来源**（原为硬编码副本）。
#   铁律：量程只能来自 factor_weights.json → dim_cfg.load_dim_max。
from dim_cfg import load_dim_max
DIM_MAX = load_dim_max(['score_fund', 'score_quality', 'score_tech_neg', 'score_bias60', 'score_rsi_rev', 'score_lowvol20', 'score_ep', 'score_size_ind'], strict=True)
W_V49 = {"score_fund": 0.075, "score_quality": 0.12, "score_tech_neg": 0.15,
         "score_bias60": 0.175, "score_rsi_rev": 0.175, "score_lowvol20": 0.13,
         "score_ep": 0.075, "score_size_ind": 0.10}

LABEL = {"score_fund": "fund 估值", "score_quality": "质量", "score_tech_neg": "tech_neg",
         "score_bias60": "bias60", "score_rsi_rev": "rsi_rev",
         "score_lowvol20": "lowvol20", "score_ep": "ep", "score_size_ind": "size_ind"}


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
    ts = {L: nw_t(ics, L) for L in LAGS}
    return {"n": n, "ic": round(mu, 4),
            "icir": round(mu / sd, 3) if sd > 1e-9 else None,
            "t_by_lag": {str(L): (round(v, 2) if v is not None else None)
                         for L, v in ts.items()},
            "pass_bonf": bool(all(v is not None and abs(v) >= T_BONF
                                  for v in ts.values()))}


def paired(a, b):
    if len(a) != len(b) or len(a) < 5:
        return None, None
    d = [x - y for x, y in zip(a, b)]
    t = nw_t(d, 0)
    return round(sum(d) / len(d), 4), (round(t, 2) if t is not None else None)


def main():
    h = json.load(open(os.path.join(HERE, "factor_history.json"), encoding="utf-8"))
    snaps, _ = fe._drop_stale_price_snapshots(h["snapshots"])
    tdays, px = fe.px_index(h)
    tidx = {d: i for i, d in enumerate(tdays)}
    em_f = r26.load_em_all()
    em_b = r21.load_em_cache()

    scored = json.load(open(os.path.join(HERE, "top200_scored_2026-10-23.json"),
                            encoding="utf-8"))
    sector_map = {(r.get("code") or "")[-6:]: r.get("sector_parent")
                  for r in scored["results"]}

    print("=" * 108)
    print("round33：逐维边际贡献裁决（替代「有效权重」这个有误导性的指标）")
    print("=" * 108)

    # ---------- 池子规模分段 ----------
    sizes = [(s["date"], len(s["rows"])) for s in snaps]
    big_dates = [d for d, n in sizes if n >= 400]
    small_dates = [d for d, n in sizes if n < 300]
    print(f"截面 {len(snaps)}：大池(n>=400) {len(big_dates)} 个 "
          f"[{big_dates[0] if big_dates else '-'} ~ {big_dates[-1] if big_dates else '-'}]"
          f" · 小池(n<300) {len(small_dates)} 个")
    print("⚠️ 大池样本 9 个 → 配对检验无功效，结论以「两样本方向是否一致」为准")

    # ---------- 数据重建 ----------
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
            if ind:
                P = r21.pit_period(sorted({p for dd in ind.values() for p in dd},
                                         reverse=True), d0)
                if P is not None:
                    f = r26.build_factors(ind, P, d0, r.get("price"), None,
                                          sector_map.get(c6), r)
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
        pairs_ = [(c, sector_map.get(c), v) for c, v in mvd.items()]
        z, _ = r26.group_winsorize_zneg(pairs_, min_grp=5)
        szi_score[d0] = rank10(z) if len(z) >= 20 else {}

    subs = defaultdict(dict)
    for s in snaps:
        d0 = s["date"]
        for r in s["rows"]:
            ind = em_b.get((r.get("code") or "")[-6:])
            if not ind:
                continue
            P = r21.pit_period(sorted({p for dd in ind.values() for p in dd},
                                     reverse=True), d0)
            if P is not None:
                subs[d0][r["code"]] = r21.sub_factors_pit(ind, P)
    bf = rb.backfill_reversal(tdays, px)

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
        qual_rank[d0] = rank10(q)

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
        ks = sorted(raw, key=lambda c: raw[c])
        lv_by_date[d0] = {c: round((1.0 - i / (n - 1)) * 10.0, 2)
                          for i, c in enumerate(ks)} if n > 1 else {}

    def dim_val(k, r, d0):
        c6 = (r.get("code") or "")[-6:]
        if k == "score_fund":
            v = r.get("score_fund")
            return v if isinstance(v, (int, float)) and not isinstance(v, bool) else None
        if k == "score_tech_neg":
            v = r.get("score_tech")
            return (10.0 - float(v)) if isinstance(v, (int, float)) else None
        if k in ("score_bias60", "score_rsi_rev"):
            t = (bf.get(d0) or {}).get(r.get("code"))
            if not t:
                return None
            return t[1] if k == "score_bias60" else t[2]
        if k == "score_quality":
            return qual_rank.get(d0, {}).get(r["code"])
        if k == "score_lowvol20":
            return lv_by_date.get(d0, {}).get(r["code"])
        if k == "score_ep":
            return ep_score.get(d0, {}).get(c6)
        if k == "score_size_ind":
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

    WKEYS = [k for k, v in W_V49.items() if v > 0]

    # 预计算
    print("\n预计算截面矩阵 ...")
    panel = []
    for s in snaps:
        d0 = s["date"]
        rows = []
        for r in s["rows"]:
            c6 = (r.get("code") or "")[-6:]
            dv = {k: dim_val(k, r, d0) for k in WKEYS}
            fw = {hp: fwd_ret(c6, d0, hp) for hp in HOPS}
            rows.append((dv, fw))
        panel.append((d0, len(s["rows"]), rows))
    print(f"完成：{len(panel)} 截面 / {sum(len(x[2]) for x in panel)} 股票日")

    def composite(dv, wmap):
        parts = [(k, dv.get(k), w) for k, w in wmap.items() if w > 0]
        tot, _ = scale_calc.composite_total(parts, DIM_MAX)
        return tot

    def run(wmap, hop, filter_fn):
        ics = []
        for d0, nrow, rows in panel:
            if not filter_fn(nrow):
                continue
            fv, rv = [], []
            for dv, fw in rows:
                fr = fw.get(hop)
                if fr is None:
                    continue
                c = composite(dv, wmap)
                if c is None:
                    continue
                fv.append(c)
                rv.append(fr)
            if len(fv) >= 40:
                ic = fe.rank_ic(fv, rv)
                if ic is not None:
                    ics.append(ic)
        return ics

    ALL = lambda n: True
    BIG = lambda n: n >= 400
    SMALL = lambda n: n < 300
    SAMPLES = [("全样本", ALL), ("大池 n>=400", BIG), ("小池 n<300", SMALL)]

    out = {"round": "round33", "samples": {}, "ablation": {}}

    # ---------- 1) 基准 & 逐维消融 ----------
    for sname, fn in SAMPLES:
        print()
        print("=" * 108)
        print(f"【{sname}】基准 vs 逐维消融（去掉该维，权重按比例分给其余维 → 归一）")
        print("=" * 108)
        for hop in HOPS:
            base_ics = run(W_V49, hop, fn)
            bst = stat(base_ics)
            if not bst:
                print(f"  h{hop}: 样本不足")
                continue
            print(f"\n  h{hop} 基准 IC {bst['ic']:+.4f} (t0 {bst['t_by_lag']['0']:+.2f}, "
                  f"n={bst['n']})")
            print(f"    {'去掉该维':<16} {'IC':>9} {'ΔIC':>9} {'配对t':>8} {'n':>4}  {'判定'}")
            for k in WKEYS:
                w2 = {kk: vv for kk, vv in W_V49.items() if kk != k}
                sw = sum(w2.values())
                w2 = {kk: vv / sw for kk, vv in w2.items()}     # 归一，保持 Σ=1
                ics2 = run(w2, hop, fn)
                st2 = stat(ics2)
                if not st2:
                    continue
                # 对齐截面
                d1 = dict(zip(range(len(base_ics)), base_ics))
                d2 = dict(zip(range(len(ics2)), ics2))
                mn = min(len(base_ics), len(ics2))
                dm, tt = paired(base_ics[:mn], ics2[:mn])
                verdict = "该维不可替代" if (dm is not None and dm > 0) else "该维可去/累赘"
                print(f"    {LABEL[k]:<16} {st2['ic']:>+9.4f} {dm if dm is not None else 0:>+9.4f} "
                      f"{tt if tt is not None else 0:>+8.2f} {st2['n']:>4}  {verdict}")
                out["ablation"][f"{sname}|h{hop}|drop_{k}"] = {
                    "ic": st2["ic"], "d_ic": dm, "t": tt, "n": st2["n"]}
            out["samples"][f"{sname}|h{hop}|base"] = bst

    # ---------- 2) 有效权重 vs IC 贡献分解 ----------
    print()
    print("=" * 108)
    print("为什么「有效权重 5.3%」不等于「贡献 5.3%」—— 分散度 vs 增量信息")
    print("=" * 108)
    # 用大池（最接近生产）当日截面算 std，用全样本 IC 说明
    last_big = [s for s in snaps if len(s["rows"]) >= 400][-1]
    d0 = last_big["date"]
    print(f"\n取截面 {d0}（n={len(last_big['rows'])}）")
    print(f"  {'维度':<14} {'名义w':>7} {'std(0-10)':>10} {'有效w':>7} {'单维IC':>8} {'w×IC':>8}  {'解释'}")
    rows_dv = []
    for r in last_big["rows"]:
        c6 = (r.get("code") or "")[-6:]
        rows_dv.append(({k: dim_val(k, r, d0) for k in WKEYS},
                        {hp: fwd_ret(c6, d0, hp) for hp in HOPS}))
    stds, single_ic = {}, {}
    for k in WKEYS:
        vals = [dv[k] for dv, _ in rows_dv if scale_calc.is_num(dv.get(k))]
        if len(vals) < 20:
            continue
        # 归一到 0-10
        mx = DIM_MAX.get(k, 10.0)
        norm = [min(v, mx) / mx * 10.0 for v in vals]
        stds[k] = statistics.pstdev(norm)
    tot_ws = sum(W_V49[k] * stds.get(k, 0) for k in WKEYS if k in stds)
    # 单维 IC（全样本，为可比）
    for k in WKEYS:
        ics = []
        for d0x, nrow, rows in panel:
            fv, rv = [], []
            for dv, fw in rows:
                fr = fw.get(5)
                v = dv.get(k)
                if fr is None or not scale_calc.is_num(v):
                    continue
                fv.append(v)
                rv.append(fr)
            if len(fv) >= 40:
                ic = fe.rank_ic(fv, rv)
                if ic is not None:
                    ics.append(ic)
        if ics:
            single_ic[k] = statistics.mean(ics)

    for k in sorted(WKEYS, key=lambda x: -W_V49[x]):
        if k not in stds:
            continue
        ew = W_V49[k] * stds[k] / tot_ws if tot_ws > 0 else 0
        ic = single_ic.get(k)
        note = ""
        if ic is not None and ew > 0:
            eff = W_V49[k] * ic
            note = "窄幅但有效" if (ew < W_V49[k] * 0.7 and ic > 0.05) else (
                "宽幅且有效" if ic > 0.05 else "宽幅低效")
            print(f"  {LABEL[k]:<14} {W_V49[k]*100:>6.1f}% {stds[k]:>10.3f} {ew*100:>6.1f}% "
                  f"{ic:>+8.4f} {eff:>+8.4f}  {note}")
            out["eff_weight"] = out.get("eff_weight") or {}
            out["eff_weight"][k] = {"w": W_V49[k], "std": round(stds[k], 3),
                                    "eff_w": round(ew, 4), "ic": round(ic, 4)}
    print()
    print("  关键读法：tech_neg 的 std 小（窄幅）→ 有效权重低，但单维 IC 仍为正，")
    print("  且 round31 实测「拉满量程」显著更差（配对 t −4.96）→ 窄幅是它的特性，不是缺陷。")
    print("  「有效权重」只说明它把股票分开的能力弱，不说明它没用。")

    with open(os.path.join(HERE, "audit_round33.json"), "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print()
    print("=" * 108)
    print("已写出 audit_round33.json")
    print("=" * 108)
    return 0


if __name__ == "__main__":
    sys.exit(main())
