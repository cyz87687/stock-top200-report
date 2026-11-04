#!/usr/bin/env python3
"""round31：投研视角 T0 两问的 IC 前置验证（tech_neg 权重失真 / bias60×rsi_rev 共线）。

## 为什么必须先验证再落地

诊断已确认两个「页面看不出、但实质影响决策」的问题：

  ① score_tech_neg 名义 15%、有效仅 5.3%（std=0.902，极差仅占量程 33.9%）
     根因：s_tech_neg = 10 − ts_adj，而 ts_adj ∈ [0,5] → 该维值域恒为 [5,10]，
     只用了量程上半段。DMAX=10 的分母没用满 → 权重被静默稀释。
  ② bias60 × rsi_rev 秩相关 +0.854，合计 35% 权重实为 1 个因子
     → 标称 8 维，有效独立维数仅 3.04。

但「诊断出失真」≠「改了就更好」。本脚本用**生产线同构量尺**（scale_calc，
分母恒定 Σ全部生效权重 + 缺维填 DMAX/2）在 85 个截面上对比各修法，
判定标准沿用项目纪律（不放宽）：
  · NW t 在 lag 0/3/6 **全部** ≥ 3.08（Bonferroni）
  · 分子期前半/后半必须同号
  · 与现状必须做**逐截面配对 t**（不是比两个点估计的均值）
  · ⚠️ 本轮同时比较 10 个方案 = 多重检验（项目累计已 300+ 次），
     任何单点「过」都不足以上线；必须 ΔIC>0 且配对 t 同向。

## 方案

T0-A（tech_neg）
  A0 现状        tech_neg .15
  A1 降半        tech_neg .075，让给 lowvol20（.13→.205）
  A2 归零        tech_neg 0，全让给 lowvol20（.28）
  A3 重标定      tech_neg 改池内 rank 分位 0-10（权重仍 .15）← 唯一「不改先验只改刻度」
  A4 线性拉伸    tech_neg 把 [5,10] 线性映到 [0,10]（等价于 DMAX 改 5 后乘 2）

T0-B（共线）
  B0 现状        bias60 .175 / rsi_rev .175
  B1 合并均值    新维 rev_m=(bias60+rsi_rev)/2 占 .35
  B2 正交化      rsi_rev 对 bias60 逐截面 OLS 取残差 → rank10，权重仍 .175 各半
  B3 只留 bias60 .35
  B4 只留 rsi_rev .35

## 口径声明（血泪：round24d 栽在这里）

量尺**必须**是生产线公式 sum(min(v,DMAX)/DMAX*10*w)/Σ全部生效权重*10，
且缺维填 NEUTRAL_FILL_RATIO*10（=5.0）。**不得**用「逐截面 rank_pct 再加权」，
那样绝对数值不可作上线依据。
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
import scale_calc
from dim_cfg import load_dim_max

T_BONF = 3.08
HOPS = [5, 10, 20]
LAGS = [0, 3, 6]

# round125: dim_max 改为**单一来源**（原为硬编码副本）。
#   铁律：量程只能来自 factor_weights.json → dim_cfg.load_dim_max。
#   ⚠️ 本文件里的 score_size 是历史维度（v4.9 用），当前配置已无该键 →
#      故显式给兜底值 10.0，避免 dim_cfg strict 报错；它不是本次改动引入的。
_DIM_KEYS = ["score_fund", "score_quality", "score_tech_neg", "score_bias60",
             "score_rsi_rev", "score_lowvol20", "score_ep", "score_size_ind"]
DIM_MAX = load_dim_max(_DIM_KEYS, strict=True)
DIM_MAX.setdefault("score_size", 10.0)   # 历史键，仅本脚本内部使用

# 现行 v4.9 八维（Σ=1.0）
W_V49 = {"score_fund": 0.075, "score_quality": 0.12, "score_tech_neg": 0.15,
         "score_bias60": 0.175, "score_rsi_rev": 0.175, "score_lowvol20": 0.13,
         "score_ep": 0.075, "score_size_ind": 0.10}


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
    k = n // 2
    h1 = sum(ics[:k]) / k
    h2 = sum(ics[k:]) / (n - k)
    ts = {L: nw_t(ics, L) for L in LAGS}
    return {"n": n, "ic": round(mu, 4),
            "icir": round(mu / sd, 3) if sd > 1e-9 else None,
            "t_by_lag": {str(L): (round(v, 2) if v is not None else None)
                         for L, v in ts.items()},
            "pass_bonf": bool(all(v is not None and abs(v) >= T_BONF
                                  for v in ts.values())),
            "ic_pos_share": round(sum(1 for x in ics if x > 0) / n, 3),
            "sub_first": round(h1, 4), "sub_second": round(h2, 4),
            "sub_both_pos": bool(h1 > 0 and h2 > 0)}


def paired_t(a, b):
    """逐截面配对差 a−b 的 NW t（lag0）。两列必须同长同截面顺序。"""
    if len(a) != len(b) or len(a) < 5:
        return None, None
    d = [x - y for x, y in zip(a, b)]
    return round(sum(d) / len(d), 4), (round(nw_t(d, 0), 2) if nw_t(d, 0) is not None else None)


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

    print("=" * 104)
    print("round31：T0 两问 IC 前置验证（生产线同构量尺 scale_calc / uniform_v1）")
    print("=" * 104)
    print(f"截面 {len(snaps)} · 交易日 {len(tdays)} · EM(full) {len(em_f)} / EM(basic) {len(em_b)}")

    # ---------- 股本重建（size 与 size_ind 用）----------
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

    def rank10_asc(d):
        ks = sorted(d, key=lambda c: d[c])
        n = len(ks)
        if n < 2:
            return {}
        return {c: round((i / (n - 1)) * 10.0, 2) for i, c in enumerate(ks)}

    ep_score = {d: rank10_asc(v) for d, v in ep_raw.items()}
    szi_score = {}
    for d0, mvd in mv_by_date.items():
        pairs = [(c, sector_map.get(c), v) for c, v in mvd.items()]
        z, _ = r26.group_winsorize_zneg(pairs, min_grp=5)
        szi_score[d0] = rank10_asc(z) if len(z) >= 20 else {}

    # ---------- 现行维重建 ----------
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
        qual_rank[d0] = rank10_asc(q)

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
        # 低波 = 高分 → 按 raw 升序排（最小波动拿 10 分）
        ks = sorted(raw, key=lambda c: raw[c])
        n = len(ks)
        lv_by_date[d0] = {c: round((1.0 - i / (n - 1)) * 10.0, 2)
                          for i, c in enumerate(ks)} if n > 1 else {}

    # ---------- tech_neg 重标定所需：逐截面 rank 与线性拉伸 ----------
    tech_raw = defaultdict(dict)          # d0 -> code -> s_tech_neg (原值 5~10)
    for s in snaps:
        d0 = s["date"]
        for r in s["rows"]:
            v = r.get("score_tech")
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                tech_raw[d0][r["code"]] = 10.0 - float(v)

    tech_rank = {d: rank10_asc(v) for d, v in tech_raw.items()}

    def tech_stretch(v):
        """[5,10] → [0,10] 线性拉伸。等价于把该维 DMAX 当 5 用再乘 2。"""
        return max(0.0, min(10.0, (float(v) - 5.0) * 2.0))

    tech_stretched = {d: {c: tech_stretch(v) for c, v in m.items()}
                      for d, m in tech_raw.items()}

    # ---------- bias60 / rsi_rev 及正交化 ----------
    def dim_val(d, r, d0):
        c6 = (r.get("code") or "")[-6:]
        if d == "score_fund":
            v = r.get("score_fund")
            return v if isinstance(v, (int, float)) and not isinstance(v, bool) else None
        if d == "score_tech_neg":
            return tech_raw.get(d0, {}).get(r["code"])
        if d in ("score_bias60", "score_rsi_rev"):
            t = (bf.get(d0) or {}).get(r.get("code"))
            if not t:
                return None
            return t[1] if d == "score_bias60" else t[2]
        if d == "score_quality":
            return qual_rank.get(d0, {}).get(r["code"])
        if d == "score_lowvol20":
            return lv_by_date.get(d0, {}).get(r["code"])
        if d == "score_ep":
            return ep_score.get(d0, {}).get(c6)
        if d == "score_size_ind":
            return szi_score.get(d0, {}).get(c6)
        return None

    # 正交化：rsi_rev 对 bias60 逐截面 OLS 取残差 → rank10
    rev_resid_rank = {}
    for s in snaps:
        d0 = s["date"]
        xs, ys = [], []
        for r in s["rows"]:
            b = dim_val("score_bias60", r, d0)
            v = dim_val("score_rsi_rev", r, d0)
            if b is not None and v is not None:
                xs.append(float(b))
                ys.append(float(v))
        if len(xs) < 40:
            rev_resid_rank[d0] = {}
            continue
        mx = sum(xs) / len(xs)
        my = sum(ys) / len(ys)
        sxy = sum((xs[i] - mx) * (ys[i] - my) for i in range(len(xs)))
        sxx = sum((xs[i] - mx) ** 2 for i in range(len(xs)))
        beta = sxy / sxx if sxx > 1e-12 else 0.0
        resid = {}
        for r in s["rows"]:
            b = dim_val("score_bias60", r, d0)
            v = dim_val("score_rsi_rev", r, d0)
            if b is not None and v is not None:
                resid[r["code"]] = float(v) - (my + beta * (float(b) - mx))
        rev_resid_rank[d0] = rank10_asc(resid) if len(resid) >= 40 else {}

    # 合并维：等权均值（先把两者归一到 0-10 再平均）
    rev_merge = {}
    for s in snaps:
        d0 = s["date"]
        m = {}
        for r in s["rows"]:
            b = dim_val("score_bias60", r, d0)
            v = dim_val("score_rsi_rev", r, d0)
            if b is not None and v is not None:
                m[r["code"]] = (float(b) / 5.0 * 10.0 + float(v) / 5.0 * 10.0) / 2.0
            elif b is not None:
                m[r["code"]] = float(b) / 5.0 * 10.0
            elif v is not None:
                m[r["code"]] = float(v) / 5.0 * 10.0
        rev_merge[d0] = m

    print(f"子因子 {sum(len(v) for v in subs.values())} 股票日 · 反转回填 {len(bf)} 日 "
          f"· tech_neg 覆盖 {sum(len(v) for v in tech_raw.values())}")

    def fwd_ret(c6, d0, hop):
        m = px.get(c6)
        i0 = tidx.get(d0)
        if not m or i0 is None or i0 + hop >= len(tdays):
            return None
        p0, p1 = m.get(tdays[i0]), m.get(tdays[i0 + hop])
        if p0 is None or p1 is None or p0 <= 0:
            return None
        return p1 / p0 - 1.0

    # ---------- 截面预取：把每行每维值算一次，方案只做加权 ----------
    print("预计算截面维度矩阵 ...")
    panel = []          # [(d0, [(code, {dim:val}, fwd{hop:ret})])]
    for s in snaps:
        d0 = s["date"]
        rows = []
        for r in s["rows"]:
            c6 = (r.get("code") or "")[-6:]
            dv = {}
            for k in ("score_fund", "score_quality", "score_tech_neg",
                      "score_bias60", "score_rsi_rev", "score_lowvol20",
                      "score_ep", "score_size_ind"):
                dv[k] = dim_val(k, r, d0)
            dv["_tech_rank"] = tech_rank.get(d0, {}).get(r["code"])
            dv["_tech_stretch"] = tech_stretched.get(d0, {}).get(r["code"])
            dv["_rev_resid"] = rev_resid_rank.get(d0, {}).get(r["code"])
            dv["_rev_merge"] = rev_merge.get(d0, {}).get(r["code"])
            fw = {hp: fwd_ret(c6, d0, hp) for hp in HOPS}
            rows.append((c6, dv, fw))
        panel.append((d0, rows))
    print(f"预计算完成：{len(panel)} 截面 / {sum(len(x[1]) for x in panel)} 股票日")

    # ---------- 方案定义 ----------
    def base(**over):
        w = dict(W_V49)
        w.update(over)
        return w

    CASES = {
        "A0 现状 tech_neg .15": base(),
        "A1 tech_neg .075→lowvol .205": base(score_tech_neg=0.075, score_lowvol20=0.205),
        "A2 tech_neg 0→lowvol .28": base(score_tech_neg=0.0, score_lowvol20=0.28),
        "A3 tech_neg 改池内rank .15": dict(base(), **{"__sub_tech": "rank"}),
        "A4 tech_neg 线性拉伸 .15": dict(base(), **{"__sub_tech": "stretch"}),
        "B0 现状 bias.175/rsi.175": base(),
        "B1 合并均值 rev_m .35": {"score_fund": 0.075, "score_quality": 0.12,
                                  "score_tech_neg": 0.15, "score_lowvol20": 0.13,
                                  "score_ep": 0.075, "score_size_ind": 0.10,
                                  "__rev_merge": 0.35},
        "B2 正交化 rsi 残差 .175": dict(base(), **{"__sub_rev": "resid"}),
        "B3 只留 bias60 .35": {"score_fund": 0.075, "score_quality": 0.12,
                               "score_tech_neg": 0.15, "score_lowvol20": 0.13,
                               "score_ep": 0.075, "score_size_ind": 0.10,
                               "score_bias60": 0.35},
        "B4 只留 rsi_rev .35": {"score_fund": 0.075, "score_quality": 0.12,
                                "score_tech_neg": 0.15, "score_lowvol20": 0.13,
                                "score_ep": 0.075, "score_size_ind": 0.10,
                                "score_rsi_rev": 0.35},
    }

    def build_parts(spec, dv):
        """把带 __ 前缀的伪维展开成真实 (dim, value, weight) 三元组 + dim_max。

        ⚠️ 缺维（value=None）的项**必须留在 parts 里**，因为 composite_total
           要求「分母恒定 = Σ全部生效权重、缺维填中性」。若在此处过滤 None，
           会退化成 P0-4 修复前的混合量尺。"""
        parts, dmax = [], {}
        sub_tech = spec.get("__sub_tech")
        sub_rev = spec.get("__sub_rev")
        for k, w in spec.items():
            if k.startswith("__") or not scale_calc.is_num(w) or w <= 0:
                continue
            v = dv.get(k)
            if k == "score_tech_neg" and sub_tech == "rank":
                v = dv.get("_tech_rank")
            elif k == "score_tech_neg" and sub_tech == "stretch":
                v = dv.get("_tech_stretch")
            elif k == "score_rsi_rev" and sub_rev == "resid":
                v = dv.get("_rev_resid")
            parts.append((k, v, w))
            dmax[k] = DIM_MAX.get(k, 10.0)
        _rw = spec.get("__rev_merge")
        if scale_calc.is_num(_rw) and _rw > 0:
            parts.append(("__rev_merge", dv.get("_rev_merge"), _rw))
            dmax["__rev_merge"] = 10.0
        return parts, dmax

    def run(spec, hop):
        ics, dates = [], []
        for d0, rows in panel:
            fv, rv = [], []
            for c6, dv, fw in rows:
                fr = fw.get(hop)
                if fr is None:
                    continue
                parts, dmax = build_parts(spec, dv)
                tot, _miss = scale_calc.composite_total(parts, dmax)
                if tot is None:
                    continue
                fv.append(tot)
                rv.append(fr)
            if len(fv) >= 40:
                ic = fe.rank_ic(fv, rv)
                if ic is not None:
                    ics.append(ic)
                    dates.append(d0)
        return ics, dates

    out = {"round": "round31", "scale": "uniform_v1 (scale_calc)",
           "n_snapshots": len(snaps), "cases": {}}
    series = {}

    for hop in HOPS:
        print()
        print("─" * 104)
        print(f"【h{hop}】生产线同构量尺 · 10 方案")
        print("─" * 104)
        print(f"  {'方案':<32} {'n':>4} {'IC':>8} {'ICIR':>7} {'t0':>7} {'t3':>7} {'t6':>7} "
              f"{'Bonf':>5} {'IC>0':>6} {'前半':>8} {'后半':>8}")
        for nm, spec in CASES.items():
            ics, dates = run(spec, hop)
            st = stat(ics)
            if not st:
                print(f"  {nm:<32} 样本不足")
                continue
            t = st["t_by_lag"]
            print(f"  {nm:<32} {st['n']:>4} {st['ic']:>+8.4f} {st['icir'] or 0:>+7.3f} "
                  f"{t.get('0') or 0:>+7.2f} {t.get('3') or 0:>+7.2f} {t.get('6') or 0:>+7.2f}  "
                  f"{'✅' if st['pass_bonf'] else '⚠️':>5} {st['ic_pos_share']:>6.3f} "
                  f"{st['sub_first']:>+8.4f} {st['sub_second']:>+8.4f}")
            out["cases"][f"h{hop}|{nm}"] = st
            series[(hop, nm)] = (ics, dates)

    # ---------- 配对检验 ----------
    print()
    print("=" * 104)
    print("配对检验（逐截面配对差，同截面才可比）——  基准 = A0/B0 现状")
    print("=" * 104)
    print(f"  {'对比':<40} {'hop':>4} {'ΔIC':>9} {'配对t':>8} {'结论':>16}")
    PAIRS = [
        ("A0 现状 tech_neg .15", "A1 tech_neg .075→lowvol .205"),
        ("A0 现状 tech_neg .15", "A2 tech_neg 0→lowvol .28"),
        ("A0 现状 tech_neg .15", "A3 tech_neg 改池内rank .15"),
        ("A0 现状 tech_neg .15", "A4 tech_neg 线性拉伸 .15"),
        ("B0 现状 bias.175/rsi.175", "B1 合并均值 rev_m .35"),
        ("B0 现状 bias.175/rsi.175", "B2 正交化 rsi 残差 .175"),
        ("B0 现状 bias.175/rsi.175", "B3 只留 bias60 .35"),
        ("B0 现状 bias.175/rsi.175", "B4 只留 rsi_rev .35"),
        ("B0 现状 bias.175/rsi.175", "A1 tech_neg .075→lowvol .205"),
    ]
    for a, b in PAIRS:
        for hop in HOPS:
            sa = series.get((hop, a))
            sb = series.get((hop, b))
            if not sa or not sb:
                continue
            # 按日期对齐
            da = dict(zip(sa[1], sa[0]))
            db = dict(zip(sb[1], sb[0]))
            common = sorted(set(da) & set(db))
            if len(common) < 10:
                continue
            va = [da[d] for d in common]
            vb = [db[d] for d in common]
            dm, tt = paired_t(vb, va)
            if dm is None:
                continue
            verdict = "候选通过" if (dm > 0 and tt is not None and tt >= T_BONF) else (
                "同向不显著" if dm > 0 else "更差·否决")
            print(f"  {b[:38]:<40} h{hop:<3} {dm:>+9.4f} {tt:>+8.2f} {verdict:>16}")
            out["cases"][f"pair|h{hop}|{b}|vs|{a}"] = {"d_ic": dm, "t": tt,
                                                       "n": len(common)}

    # ---------- tech_neg 分布诊断（确认重标定的必要性）----------
    print()
    print("─" * 104)
    print("tech_neg 值域诊断（为何名义 15% 只发挥 5.3%）")
    print("─" * 104)
    allv = [v for d, m in tech_raw.items() for v in m.values()]
    allv.sort()
    n = len(allv)
    print(f"  s_tech_neg 全历史 n={n}  min={allv[0]:.2f} p25={allv[n//4]:.2f} "
          f"p50={allv[n//2]:.2f} p75={allv[3*n//4]:.2f} max={allv[-1]:.2f}")
    print(f"  值域占用 = {(allv[-1]-allv[0])/10.0*100:.1f}%  → DMAX=10 的分母只用了上半段")
    print(f"  → 结论：不是「该因子没用」，是「刻度浪费了半个量程」，权重被静默稀释")

    with open(os.path.join(HERE, "audit_round31.json"), "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print()
    print("=" * 104)
    print("已写出 audit_round31.json")
    print("=" * 104)
    return 0


if __name__ == "__main__":
    sys.exit(main())
