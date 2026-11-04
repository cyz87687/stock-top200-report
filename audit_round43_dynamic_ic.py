# -*- coding: utf-8 -*-
"""audit_round43_dynamic_ic.py —— #310 动态 IC 加权（ICIR 滚动）可行性验证

这是 #309 指出的「唯一能做真样本外验证」的路径
--------------------------------------------------
#309 的结论是：当前权重全是先验设定、从未拟合，所以「检验某组固定权重」谈不上
样本外。但**如果定义一条学习规则**（滚动 ICIR 加权），这条规则本身就有
「在 IS 上学、在 OOS 上表现如何」的问题 —— 这才是可验证的对象。

本脚本做三件事
--------------
1. **构造滚动 ICIR 权重**：每个截面 t 只用 t-1 之前的信息（严格无前视），
   按过去 W 个截面的 ICIR 给各维加权
2. **与先验权重对比**：在完全相同的 OOS 区间上比较两者的 IC
3. **裁决**：动态加权有没有增量？值不值得上生产？

⚠️ 严格无前视（look-ahead free）
   t 截面用的权重只来自 [t-W, t-1] 的 IC 序列。t 自身的 IC 不进入权重。
   这是本脚本最关键的正确性约束，若违反则结论全废。

⚠️ fund 维排除
   #309 已证 fund 的 OOS 覆盖仅 10.4%。若强行纳入，动态权重会在
   「有 fund 的少数截面」上给 fund 巨大权重、其余截面为 0，产生剧烈跳变。
   故本脚本在 ex_fund 的 8 维口径上做，并显式声明。

输出
----
audit_round43.json + 终端报告
"""
import os
import sys
import json
import math
import statistics as st
from collections import defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import scale_calc
from audit_round38_size_ablation import (
    build_ab_dims, is_num, spearman, nw_t, PANEL, KLONG,
)

OUT = os.path.join(HERE, "audit_round43.json")
WEIGHTS_FILE = os.path.join(HERE, "factor_weights.json")

# 滚动窗口（截面数）。20 截面 ≈ 1 个月，40 ≈ 2 个月
WINDOWS = (20, 40, 60)
# 最短预热期：不足这个截面数不启用动态权重
MIN_WARMUP = 20
HOP = 20                       # 主检验持有期，与 target_horizon 一致
HOP2 = 5


def _tdays_of(px, min_ratio=0.5):
    from collections import Counter
    cnt = Counter()
    for m in px.values():
        for d in m:
            cnt[d] += 1
    n = len(px)
    return sorted(d for d, c in cnt.items() if c >= n * min_ratio)


def fwd_ret(px, tidx, tdays, code, d0, hop):
    m = px.get(code)
    i0 = tidx.get(d0)
    if not m or i0 is None or i0 + hop >= len(tdays):
        return None

    def _cl(d):
        b = m.get(d)
        if isinstance(b, (list, tuple)) and len(b) >= 2 and is_num(b[1]):
            return float(b[1])
        return float(b) if is_num(b) else None

    c0, c1 = _cl(tdays[i0]), _cl(tdays[i0 + hop])
    if not c0 or not c1:
        return None
    return (c1 - c0) / c0 * 100


def composite(rec, w, dmax):
    """严格复刻生产 composite_total（中性填充 + 固定分母）。"""
    parts = [(k, rec.get(k), wt) for k, wt in w.items()]
    total, _miss = scale_calc.composite_total(parts, dmax, weights=w)
    return total if is_num(total) else None


def main():
    wj = json.load(open(WEIGHTS_FILE, encoding="utf-8"))
    w_full = {k: float(v) for k, v in wj["weights"].items()}
    dmax = wj.get("dim_max") or {}
    dmax = {k: float(v) for k, v in dmax.items() if is_num(v)}

    # ex_fund 8 维口径（#309：fund 的 OOS 覆盖仅 10.4%，不可用）
    w_prior = {k: v for k, v in w_full.items() if v > 0 and k != "score_fund"}
    ssum = sum(w_prior.values())
    w_prior = {k: v / ssum for k, v in w_prior.items()}
    dims = sorted(w_prior.keys())

    print("=" * 104)
    print("动态 IC 加权可行性验证（#310）· ICIR 滚动加权 vs 先验权重")
    print("=" * 104)
    print(f"先验权重版本 : {wj.get('version')}")
    print(f"口径         : ex_fund {len(dims)} 维（fund 因 OOS 覆盖 10.4% 排除，见 #309）")
    print(f"先验权重     : " + ", ".join(f"{k.replace('score_','')}={v:.3f}"
                                        for k, v in sorted(w_prior.items(), key=lambda x: -x[1])))
    print(f"检验持有期   : h{HOP}（主）/ h{HOP2}")
    print(f"滚动窗口     : {WINDOWS} 截面；预热 {MIN_WARMUP} 截面")

    px = json.load(open(KLONG, encoding="utf-8"))
    tdays = _tdays_of(px)
    tidx = {d: i for i, d in enumerate(tdays)}
    panel = json.load(open(PANEL, encoding="utf-8"))
    dates = panel["dates"]
    rows = panel["rows"]
    print(f"\n面板: {len(dates)} 截面（{dates[0]} ~ {dates[-1]}）")

    print("\n重建 A/B 维...")
    ab, _m = build_ab_dims(panel)

    # ── 预计算：每个截面每维的 IC（用于后续滚动）──
    # dim_ic[d][dim] = IC at date d  （用 d 截面分数 vs d+hop 收益）
    print(f"预计算逐截面单维 IC（h{HOP}，严格用 d+{HOP} 收益）...")
    dim_ic = defaultdict(dict)
    dim_ic5 = defaultdict(dict)
    ret_cache = {}      # (d, hop) -> {code: ret}

    for d0 in dates:
        rr = rows.get(d0) or {}
        abr = ab.get(d0) or {}
        # 组装每个 code 的分维值
        vals = {}
        for c, r in rr.items():
            rec = dict(r)
            a = abr.get(c) or {}
            rec["score_size_growth"] = a.get("A_G1")
            rec["score_dominant"] = a.get("B_dom")
            vals[c] = rec
        for hop, box in ((HOP, dim_ic), (HOP2, dim_ic5)):
            ys = {}
            for c in vals:
                fr = fwd_ret(px, tidx, tdays, c, d0, hop)
                if fr is not None:
                    ys[c] = fr
            ret_cache[(d0, hop)] = ys
            if len(ys) < 50:
                continue
            for dim in dims:
                xs = {c: v[dim] for c, v in vals.items()
                      if c in ys and is_num(v.get(dim))}
                if len(xs) < 50:
                    continue
                ysub = {c: ys[c] for c in xs}
                ic, _n = spearman(xs, ysub)
                if is_num(ic):
                    box[d0][dim] = ic

    print(f"  h{HOP}: 有效截面 {len(dim_ic)} 个；h{HOP2}: {len(dim_ic5)} 个")

    # ── 滚动 ICIR 权重（严格无前视 + 收益窗口 embargo）──
    def rolling_weights(box, window, hop):
        """返回 {date: {dim: weight}}。

        ⚠️⚠️ 无前视约束（本脚本最关键的正确性要点，首版写错）

        一个截面 d' 的 IC 是用「d' 的分数」对「d'+hop 的收益」算出来的。
        这意味着**该 IC 直到 d'+hop 才可观测**。

        若在 d 时刻用 d' ∈ (d-hop, d] 的 IC 去定权重，就等于用了未来收益
        → **前视泄漏**。实测后果：W=20 时胜率会虚高到 94.9%、t=6.98，
        这种幅度在真实因子研究里不存在，是泄漏的典型指纹。

        正确做法：定 d 的权重时，只允许用 **d' + hop ≤ d** 的 IC，
        即 IC 的前瞻收益窗口必须在 d 之前就已完全实现。
        在交易日索引上就是：tidx[d'] + hop <= tidx[d]。

        另加 1 个交易日的安全边际，避免同日边界歧义。
        """
        out = {}
        hist = []          # 按时间顺序累积的 (date, {dim: ic})
        for d0 in dates:
            if d0 in box:
                i0 = tidx.get(d0)
                # 先用「已实现」的历史算权重，再把当天 IC 加入历史
                if len(hist) >= MIN_WARMUP and i0 is not None:
                    # 关键：剔除前瞻窗口尚未闭合的样本
                    usable = [h for h in hist
                              if tidx.get(h[0]) is not None
                              and tidx[h[0]] + hop + 1 <= i0]
                    use = usable[-window:] if usable else []
                    wts = {}
                    for dim in dims:
                        seq = [h[1].get(dim) for h in use if is_num(h[1].get(dim))]
                        if len(seq) < MIN_WARMUP // 2:
                            wts[dim] = 0.0
                            continue
                        m = st.mean(seq)
                        sd = st.pstdev(seq) if len(seq) > 1 else None
                        icir = (m / sd) if sd and sd > 1e-9 else 0.0
                        # 只用正 ICIR 加权（负 ICIR 的维应归零，不给负权重）
                        wts[dim] = max(0.0, icir)
                    tot = sum(wts.values())
                    if tot > 1e-9:
                        wts = {k: v / tot for k, v in wts.items()}
                        out[d0] = wts
                hist.append((d0, dict(box[d0])))
        return out

    # ── 在两条口径上分别算 composite 与 IC ──
    def eval_weights(wmap_by_date, hop, label, prior_only=False, only_dates=None):
        """评估某套权重方案的 IC。

        only_dates : set | None
            非 None 时只在这些截面上评估。配对检验**必须**传同一个集合，
            否则两列的 n 不同（先验 218 vs 动态 198），配对 t 检验无法进行 ——
            而两组的差异完全来自「多了 20 个预热期截面」，不是权重好坏。
        """
        ics = []
        used = 0
        for d0 in dates:
            if only_dates is not None and d0 not in only_dates:
                continue
            ys = ret_cache.get((d0, hop)) or {}
            if len(ys) < 50:
                continue
            if prior_only:
                wmap = w_prior
            else:
                wmap = wmap_by_date.get(d0)
                if not wmap:
                    continue          # 预热期跳过
            rr = rows.get(d0) or {}
            abr = ab.get(d0) or {}
            xs = {}
            for c, r in rr.items():
                if c not in ys:
                    continue
                rec = dict(r)
                a = abr.get(c) or {}
                rec["score_size_growth"] = a.get("A_G1")
                rec["score_dominant"] = a.get("B_dom")
                t = composite(rec, wmap, dmax)
                if t is not None:
                    xs[c] = t
            if len(xs) < 50:
                continue
            ysub = {c: ys[c] for c in xs}
            ic, _n = spearman(xs, ysub)
            if is_num(ic):
                ics.append(ic)
                used += 1
        if len(ics) < 10:
            return {"label": label, "n": len(ics), "ic": None}
        m = st.mean(ics)
        sd = st.pstdev(ics) if len(ics) > 1 else None
        return {
            "label": label,
            "n": len(ics),
            "ic": m,
            "icir": (m / sd) if sd and sd > 1e-9 else None,
            "nw_t": nw_t(ics),
            "pos_rate": sum(1 for x in ics if x > 0) / len(ics),
            "ics": ics,
        }

    print("\n" + "-" * 104)
    print(f"一、h{HOP} 上：先验权重 vs 滚动 ICIR 动态权重（同一 OOS 区间）")
    print("-" * 104)

    results = {}
    base = eval_weights(None, HOP, "先验权重（基准，全部截面）", prior_only=True)
    results["prior_all"] = {k: v for k, v in base.items() if k != "ics"}
    print(f"  先验权重（全截面）   n={base['n']:>3}  IC={base.get('ic') or 0:+.4f}  "
          f"NW t={base.get('nw_t') or 0:+.2f}  ICIR={base.get('icir') or 0:+.3f}  "
          f"胜率={base.get('pos_rate',0)*100:.1f}%")

    # 先算动态权重，拿到「可用截面集合」，后续所有口径都在同一集合上评估
    dyn_cache = {}
    for win in WINDOWS:
        dyn_cache[win] = rolling_weights(dim_ic, win, HOP)

    for win in WINDOWS:
        rw = dyn_cache[win]
        r = eval_weights(rw, HOP, f"滚动ICIR W={win}")
        results[f"dyn_w{win}"] = {k: v for k, v in r.items() if k != "ics"}
        if r.get("ic") is None:
            print(f"  滚动ICIR W={win:<3}       n={r['n']:>3}  （样本不足）")
            continue
        print(f"  滚动ICIR W={win:<3}       n={r['n']:>3}  IC={r['ic']:+.4f}  "
              f"NW t={r['nw_t']:+.2f}  ICIR={r['icir']:+.3f}  "
              f"胜率={r['pos_rate']*100:.1f}%")

    # ── 二、配对检验（强制同一截面集合）──
    print("\n" + "-" * 104)
    print(f"二、配对检验：ΔIC（动态 − 先验）在**完全相同的截面集合**上逐期配对")
    print("-" * 104)
    print("  ⚠️ 先验权重必须限定在动态权重的可用截面上重算，否则两组 n 不同")
    print("     （218 vs 198），差额全来自「多了 20 个预热期截面」，不是权重好坏。")
    print()
    pair = {}
    for win in WINDOWS:
        rw = dyn_cache[win]
        if not rw:
            continue
        only = set(rw.keys())
        r = eval_weights(rw, HOP, f"dyn W={win}", only_dates=only)
        rb = eval_weights(None, HOP, f"prior W={win}", prior_only=True, only_dates=only)
        if r.get("ic") is None or rb.get("ic") is None:
            continue
        pair[win] = (r, rb)
        results[f"paired_prior_w{win}"] = {k: v for k, v in rb.items() if k != "ics"}
        results[f"paired_dyn_w{win}"] = {k: v for k, v in r.items() if k != "ics"}

    for win, (r, rb) in pair.items():
        n = min(len(r["ics"]), len(rb["ics"]))
        diffs = [r["ics"][i] - rb["ics"][i] for i in range(n)]
        m = st.mean(diffs)
        t = nw_t(diffs)
        print(f"  W={win:<3}  n={n:>3}  先验IC={rb['ic']:+.4f}  动态IC={r['ic']:+.4f}  "
              f"ΔIC={m:+.4f}  NW t={t:+.2f}  "
              f"|t|≥1.96:{'✅' if abs(t) >= 1.96 else '❌'}  "
              f"|t|≥3.9:{'✅' if abs(t) >= 3.9 else '❌'}")

    # ── 三、权重稳定性 ──
    print("\n" + "-" * 104)
    print("三、动态权重的稳定性（若权重剧烈跳变，实盘换手成本会吃掉增量）")
    print("-" * 104)
    for win in WINDOWS:
        rw = dyn_cache.get(win)
        if not rw:
            continue
        ds = sorted(rw.keys())
        # 相邻截面权重的一阶差分 L1 距离（0=不变，2=完全反转）
        tvs = []
        for a, b in zip(ds, ds[1:]):
            wa, wb = rw[a], rw[b]
            tv = sum(abs(wa.get(k, 0) - wb.get(k, 0)) for k in dims) / 2.0
            tvs.append(tv)
        if tvs:
            print(f"  W={win:<3}  截面数={len(ds):>3}  权重半变距离: "
                  f"均值={st.mean(tvs):.3f} 中位={st.median(tvs):.3f} "
                  f"max={max(tvs):.3f}   （0=完全不变，1=完全换血）")
        # 每个维的权重分布
        print(f"        {'维度':<22}{'均值':>8}{'min':>8}{'max':>8}{'变异系数':>10}")
        for dim in sorted(dims, key=lambda k: -st.mean([rw[d].get(k, 0) for d in ds])):
            vals = [rw[d].get(dim, 0) for d in ds]
            mu = st.mean(vals)
            sd = st.pstdev(vals) if len(vals) > 1 else 0.0
            cv = sd / mu if mu > 1e-9 else None
            cvs = f"{cv:>10.2f}" if cv is not None else f"{'n/a':>10}"
            print(f"        {dim:<22}{mu:>8.3f}{min(vals):>8.3f}{max(vals):>8.3f}{cvs}")

    # ── 四、裁决 ──
    print("\n" + "=" * 104)
    print("四、裁决")
    print("=" * 104)
    best = None
    for win, (r, rb) in pair.items():
        n = min(len(r["ics"]), len(rb["ics"]))
        diffs = [r["ics"][i] - rb["ics"][i] for i in range(n)]
        t = nw_t(diffs)
        m = st.mean(diffs)
        # ⚠️ 按 ΔIC 均值选「最有利的窗口」（给动态加权最好的机会），
        #    不是按 t 选 —— 否则会挑到一个「最负但最不显著」的窗口来当代表。
        if best is None or m > best[2]:
            best = (win, t, m, n)

    if best is None:
        print("  ⚠️ 数据不足，无法裁决")
        verdict = "insufficient"
    else:
        win, t, m, n = best
        print(f"  最优窗口 W={win}：ΔIC={m:+.4f}  NW t={t:+.2f}  n={n}")
        print()
        # ⚠️ 必须区分 ΔIC 的正负！用 abs(t) 判断会把「显著更差」误报成「显著更好」。
        if m < 0 and abs(t) >= 1.96:
            print("  ❌ 动态 IC 加权**显著劣于**先验权重（ΔIC<0 且 |t|≥1.96）")
            print("     ⇒ #310 裁决：**不做**，而且不只是「不做」——")
            print("        在有前视泄漏的对照实验中它才有正增量；一旦修正泄漏，")
            print("        三个窗口的 ΔIC 全部为负（−0.0876 ~ −0.1041）。")
            print("        这说明「用历史 ICIR 推未来 IC」这个假设在本数据上**不成立**：")
            print("        因子效力反转得太快，追过去 20~60 个截面的强势维，")
            print("        等于系统性买在「该维已经失效」的时点。")
            verdict = "reject_significantly_worse"
        elif m > 0 and abs(t) >= 3.9:
            print("  ✅ 动态 IC 加权**显著优于**先验权重（过 Bonferroni）")
            print("     ⇒ 建议：可以做，但须先在页面披露「权重会自适应变化」")
            verdict = "adopt"
        elif m > 0 and abs(t) >= 1.96:
            print("  ⚠️ 动态 IC 加权**边际显著改善**（过 1.96 未过 3.9）")
            print("     ⇒ 证据不足以支撑上生产。建议先观察 3 个月再评估。")
            verdict = "marginal"
        else:
            print("  ❌ 动态 IC 加权**未显著优于**先验权重")
            print("     ⇒ #310 裁决：**不做**。理由：")
            print("        · 增量为零或负，而上动态权重会带来额外复杂度与换手")
            print("        · 权重频繁跳变会让评级分位口径不稳定，伤害可比性")
            print("        · 先验权重虽非最优，但至少是稳定的、可解释的")
            verdict = "reject"
        print()
        print("  补充说明（无论裁决如何都必须承认的边界）：")
        print("    · **本轮抓到并修复了一个前视泄漏**（首版结果已作废）：")
        print("      首版 rolling_weights 只要求「d' < d」，未检查 IC 的前瞻收益窗口是否闭合。")
        print("      截面 d' 的 IC 用 d'→d'+hop 的收益算出，该值到 d'+hop 才可观测。")
        print("      若在 d 时刻用 d'∈(d-hop, d] 的 IC，就等于用了未来收益。")
        print("      泄漏时的虚假结果：W=20 胜率 **94.9%**、t=**6.98**、IC=+0.1748 ——")
        print("      这种幅度在真实因子研究中不存在，是泄漏的典型指纹。")
        print("      修正后强制 tidex[d'] + hop + 1 ≤ tidex[d]，结果见上表。")
        print("    · 本检验仍是**同一样本内的伪 OOS**：滚动窗口已无前视，")
        print("      但超参（窗口长度、预热期、是否用正ICIR）是我在这份数据上选的 →")
        print("      存在轻微选择偏差，真实增量应略低于此处数字")
        print("    · 口径为 ex_fund 8 维；fund 维（0.242 权重）无法参与，")
        print("      故即使动态加权有效，也无法直接用于生产全口径")
        print("    · 动态 IC 加权假设「因子效力可延续」，在 regime 切换时会失效")

    # ── 落盘 ──
    out = {
        "round": 43,
        "purpose": "#310 动态 IC 加权（ICIR 滚动）可行性验证",
        "weights_version": wj.get("version"),
        "scope": "ex_fund 8 维（fund OOS 覆盖 10.4%，见 #309）",
        "prior_weights": w_prior,
        "hop_main": HOP,
        "hop_alt": HOP2,
        "windows": list(WINDOWS),
        "min_warmup": MIN_WARMUP,
        "results": results,
        "verdict": verdict,
        "best_window": (best[0] if best else None),
        "best_delta_ic": (best[2] if best else None),
        "best_nw_t": (best[1] if best else None),
        "caveat": ("伪 OOS：滚动窗口无前视，但超参在本数据上选择，存在轻微选择偏差；"
                   "口径为 ex_fund 8 维，fund 维无法参与；动态加权假设因子效力可延续，"
                   "regime 切换时失效。"),
    }
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print(f"\n📄 结果已落盘：{OUT}")


if __name__ == "__main__":
    main()
