#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
audit_round80_matched_random.py —— 严格匹配的随机组合检验（打分有效性的决定性实验）

═══ 为什么必须重做 ═══

round79 的 B3 基准有缺陷：随机抽样抽了 `max(topn*3, 60)` = 60 只，
而组合只有 20 只 —— **数量不匹配**，持仓数差异会改变组合波动与回撤，
结论不可用。B1（IR −28.59）更是无意义：σ_超额 只有 0.1%，分母塌缩导致 IR 爆炸。

本脚本做**严格匹配**的版本，这是整个项目最该早做但一直没做的实验。

═══ 实验设计 ═══

对同一 (TopN=20, H=10, 打分加权 cap0.08) 配置，跑 R=200 次随机对照：
  · 每期在**同一候选池**内随机抽 **恰好 TopN 只**，用**同样的加权规则**（随机分）
  · 其余口径（执行价、成本、持有期、基准）与真实策略完全一致
  · 记录每次的：超额年化、IR、超额 MDD、组合年化

然后问一个**唯一**问题：
  **真实策略在 200 个随机组合的分布里排第几百分位？**

  · 若 ≥ 90 百分位 → 打分有实质价值，之前"超额小"是市场环境问题，不是模型问题
  · 若 50~90 → 打分有微弱价值
  · 若 < 50 → **打分的排序能力无法在组合层兑现**（即使 IC 显著为正）

═══ 附带检验：β 到底是模型的低波属性还是池口径失真 ═══

  · 组合 vs 池等权 β
  · 组合 vs 随机组合 β（同口径）
  · 若两者都低 → 低 beta 是**模型选出来的**（真实、可交易）
  · 若只有 vs 池等权低 → 是池口径失真（不可归因）

  这直接决定「beta 中性化」这条修复路径是否成立。

用法：python audit_round80_matched_random.py --seeds 200
"""
import os
import sys
import json
import math
import argparse
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from audit_round67_backtest_engine import (  # noqa
    composite_all, load_prices, _px, COST_ROUND)

PANEL = os.path.join(HERE, "data", "panel_round66.json")
WEIGHTS = os.path.join(HERE, "factor_weights.json")
OUT = os.path.join(HERE, "audit_round80.json")


def build_scores(panel, wmap, dim_max):
    out = {}
    for d, rows in panel["rows"].items():
        arr = []
        for c, rec in rows.items():
            s, uw = composite_all(rec, wmap, dim_max)
            if s is not None:
                arr.append((c, s))
        arr.sort(key=lambda x: (-x[1], x[0]))
        out[d] = arr
    return out


def _cap_weight(w, cap=0.08):
    w = w / w.sum()
    for _ in range(50):
        over = w > cap
        if not over.any():
            break
        ex = (w[over] - cap).sum()
        w[over] = cap
        free = ~over
        if free.sum() == 0:
            break
        w[free] += ex * w[free] / w[free].sum()
    return w / w.sum()


def run_one(panel, px, per_period_pick, topn, hold, cap=0.08):
    """per_period_pick(d_sig, d_exec, cands) -> 选中的 (code, score) 列表。"""
    dates = panel["dates"]
    rebal = [(dates[i], dates[i + 1], i) for i in range(0, len(dates) - 1, hold)]
    periods, nav, prev_w = [], 1.0, {}
    for k, (d_sig, d_exec, i0) in enumerate(rebal):
        d_next = rebal[k + 1][1] if k + 1 < len(rebal) else None
        cands = per_period_pick(d_sig, d_exec)
        if not cands:
            continue
        sel = cands[:topn]
        if len(sel) < max(5, topn // 4):
            continue
        raw = np.array([max(1e-6, s) for _, s in sel])
        w = _cap_weight(raw, cap)
        wmap = {sel[i][0]: float(w[i]) for i in range(len(sel))}
        allc = set(wmap) | set(prev_w)
        turnover = sum(abs(wmap.get(c, 0.0) - prev_w.get(c, 0.0)) for c in allc) / 2.0
        rs, ws = [], []
        for c, _s in sel:
            p0 = _px(px, c, d_exec)
            p1 = _px(px, c, d_next) if d_next else None
            if p1 is None:
                sub = px.get(c) or {}
                cand = sorted(dd for dd in sub if d_exec <= dd <= (d_next or dates[-1]))
                if cand:
                    b = sub[cand[-1]]
                    p1 = float(b[0]) if isinstance(b, (list, tuple)) else None
            if p0 and p1 and p0 > 0:
                rs.append(p1 / p0 - 1.0)
                ws.append(wmap[c])
        if not rs:
            prev_w = wmap
            continue
        sw = sum(ws)
        port = sum(r * ww for r, ww in zip(rs, ws)) / sw if sw > 0 else 0.0
        net = port - turnover * COST_ROUND
        nav *= (1.0 + net)
        periods.append({"net": net, "port": port, "turnover": turnover, "nav": nav})
        prev_w = wmap
    return periods


def metrics(periods, pool_rets, hold):
    if len(periods) < 6:
        return None
    ann = 244.0 / hold
    net = np.array([p["net"] for p in periods])
    pool = np.array([r if r is not None else np.nan for r in pool_rets[:len(periods)]])
    ok = ~np.isnan(pool)
    net, pool = net[ok], pool[ok]
    if len(net) < 6:
        return None
    ex = net - pool
    if ex.std(ddof=1) < 1e-12:
        return {"n": len(net), "excess_ann": float((1 + ex.mean()) ** ann - 1),
                "ir": None, "excess_mdd": 0.0, "sigma_excess": 0.0,
                "ann": float((1 + net.mean()) ** ann - 1), "beta": None}
    navx = np.cumprod(1.0 + ex)
    peak = np.maximum.accumulate(navx)
    b = float(np.cov(net, pool, ddof=1)[0, 1] / pool.var(ddof=1)) if pool.var(ddof=1) > 1e-12 else None
    return {
        "n": len(net),
        "ann": float((1 + net.mean()) ** ann - 1),
        "excess_ann": float((1 + ex.mean()) ** ann - 1),
        "ir": float(ex.mean() / ex.std(ddof=1) * math.sqrt(ann)),
        "excess_mdd": float((navx / peak - 1.0).min()),
        "sigma_excess": float(ex.std(ddof=1) * math.sqrt(ann)),
        "beta": b,
        "avg_turnover": float(np.mean([p["turnover"] for p in periods])),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seeds", type=int, default=200)
    ap.add_argument("--topn", type=int, default=20)
    ap.add_argument("--hold", type=int, default=10)
    ap.add_argument("--panel", default=PANEL)
    ap.add_argument("--out", default=OUT)
    args = ap.parse_args()

    print("加载 ...")
    panel = json.load(open(args.panel, encoding="utf-8"))
    px = load_prices()
    wc = json.load(open(WEIGHTS, encoding="utf-8"))
    wmap = {k: float(v) for k, v in wc["weights"].items()}
    dim_max = {k: float(v) for k, v in wc["dim_max"].items()}
    scores = build_scores(panel, wmap, dim_max)
    print(f"  {len(panel['dates'])} 截面，权重 {wc['version']}")
    print(f"  配置 TopN={args.topn}  H={args.hold}  cap0.08  打分加权")

    dates = panel["dates"]
    hold = args.hold
    topn = args.topn

    # 预计算每期的候选池（有开盘价）与池等权收益
    rebal = [(dates[i], dates[i + 1], i) for i in range(0, len(dates) - 1, hold)]
    cand_map, pool_rets = {}, []
    for (d_sig, d_exec, i0) in rebal:
        d_next = rebal[k][1] if False else None
        pass
    for idx, (d_sig, d_exec, i0) in enumerate(rebal):
        d_next = rebal[idx + 1][1] if idx + 1 < len(rebal) else None
        picks = scores.get(d_sig) or []
        cands = [(c, s) for c, s in picks if _px(px, c, d_exec) is not None]
        cand_map[(d_sig, d_exec)] = cands
        rs = []
        for c, _s in cands:
            p0 = _px(px, c, d_exec)
            p1 = _px(px, c, d_next) if d_next else None
            if p0 and p1 and p0 > 0:
                rs.append(p1 / p0 - 1.0)
        pool_rets.append((sum(rs) / len(rs)) if rs else None)

    # ── 真实策略 ──
    def real_pick(d_sig, d_exec):
        return cand_map.get((d_sig, d_exec)) or []

    real = run_one(panel, px, real_pick, topn, hold)
    rm = metrics(real, pool_rets, hold)
    print(f"\n真实策略：{rm}")

    # ── 随机对照 ──
    print(f"\n跑 {args.seeds} 个严格匹配的随机组合 ...")
    rand_rows = []
    for si in range(args.seeds):
        rng = np.random.default_rng(20261029 + si)

        def rnd_pick(d_sig, d_exec, _rng=rng):
            cands = cand_map.get((d_sig, d_exec)) or []
            if len(cands) <= topn:
                return cands
            idx = _rng.choice(len(cands), size=topn, replace=False)
            # 随机"分数"：模拟同分布的打分（用随机秩）→ 同样的加权效果
            sub = [cands[i] for i in idx]
            sc = _rng.random(topn) + 0.5      # 与真实分数同量级
            return [(c, float(s)) for (c, _), s in zip(sub, sc)]

        pers = run_one(panel, px, rnd_pick, topn, hold)
        m = metrics(pers, pool_rets, hold)
        if m:
            rand_rows.append(m)
        if (si + 1) % 25 == 0:
            irs = [r["ir"] for r in rand_rows if r["ir"] is not None]
            print(f"  {si+1}/{args.seeds}  随机IR中位 {np.median(irs):+.3f}" if irs else f"  {si+1}/{args.seeds}")

    irs = np.array([r["ir"] for r in rand_rows if r["ir"] is not None])
    exs = np.array([r["excess_ann"] for r in rand_rows])
    mdds = np.array([r["excess_mdd"] for r in rand_rows])
    anns = np.array([r["ann"] for r in rand_rows])
    betas = np.array([r["beta"] for r in rand_rows if r["beta"] is not None])

    print("\n" + "═" * 96)
    print("严格匹配随机对照（200 次，TopN/H/加权/成本全部相同）")
    print("═" * 96)
    print(f"{'指标':16s}{'真实策略':>12s}{'随机中位':>12s}{'随机均值':>12s}"
          f"{'随机p10':>12s}{'随机p90':>12s}{'真实分位':>12s}")
    print("-" * 96)

    def row(name, rv, arr, higher_better=True):
        if arr is None or len(arr) == 0:
            return
        pct = float((arr < rv).mean() * 100) if higher_better else float((arr > rv).mean() * 100)
        print(f"{name:16s}{rv:>+12.4f}{np.median(arr):>+12.4f}{np.mean(arr):>+12.4f}"
              f"{np.percentile(arr,10):>+12.4f}{np.percentile(arr,90):>+12.4f}{pct:>11.1f}%")

    row("超额年化", rm["excess_ann"], exs)
    row("IR", rm["ir"], irs)
    row("超额MDD", rm["excess_mdd"], mdds, higher_better=False)
    row("组合年化", rm["ann"], anns)
    row("β(vs池)", rm["beta"], betas)

    print()
    ir_pct = float((irs < rm["ir"]).mean() * 100) if len(irs) else None
    ex_pct = float((exs < rm["excess_ann"]).mean() * 100)
    print(f"  ⇒ 真实策略 IR 分位 = {ir_pct:.1f}%，超额年化分位 = {ex_pct:.1f}%")
    print(f"  ⇒ 随机组合 IR 中位 {np.median(irs):+.3f}、β 中位 {np.median(betas):.3f}"
          f"；真实策略 β = {rm['beta']:.3f}")
    if ir_pct >= 90:
        verdict = "✅ 打分有实质价值（≥p90）"
    elif ir_pct >= 50:
        verdict = "⚠️ 打分有微弱价值（p50~p90）"
    else:
        verdict = "❌ 打分在组合层不优于随机（<p50）"
    print(f"  ⇒ {verdict}")

    print("\n【β 的来源判定】")
    print(f"  真实组合 vs 池等权 β      = {rm['beta']:.3f}")
    print(f"  随机组合 vs 池等权 β 中位 = {np.median(betas):.3f}")
    if abs(rm["beta"] - np.median(betas)) < 0.10:
        print("  ⇒ 两者接近 → **低 β 是池口径失真**（不可归因于模型）")
        print("     ⇒ 用池等权当基准时，超额被系统性扭曲，需改用同口径基准")
    else:
        print("  ⇒ 两者有差距 → **低 β 是模型选股的结果**（真实属性，可交易）")
        print("     ⇒ 组合确实系统性持有抗跌/小盘股，这正是 R1 发现的风格暴露")

    json.dump({
        "meta": {"round": 80,
                 "purpose": "严格匹配随机组合检验：打分有效性的决定性实验 + β 来源判定",
                 "panel": os.path.relpath(args.panel, HERE),
                 "weights_version": wc["version"],
                 "config": {"topn": topn, "hold": hold, "cap": 0.08,
                            "weighting": "score-weighted", "n_seeds": args.seeds},
                 "caveat": ("随机对照与真实策略共享**同一候选池**，故只检验"
                            "「在同一池内按打分排序 vs 随机选股」的差异，"
                            "不检验池本身的构建是否有前视（后者需另做重建池实验）")},
        "real": rm,
        "random": {"n": len(rand_rows),
                   "ir_median": float(np.median(irs)), "ir_mean": float(np.mean(irs)),
                   "ir_p10": float(np.percentile(irs, 10)), "ir_p90": float(np.percentile(irs, 90)),
                   "excess_ann_median": float(np.median(exs)),
                   "excess_ann_p10": float(np.percentile(exs, 10)),
                   "excess_ann_p90": float(np.percentile(exs, 90)),
                   "mdd_median": float(np.median(mdds)),
                   "beta_median": float(np.median(betas)),
                   "ann_median": float(np.median(anns))},
        "percentile": {"ir": ir_pct, "excess_ann": ex_pct},
        "verdict": verdict,
    }, open(args.out, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"\n→ {args.out}")


if __name__ == "__main__":
    main()
