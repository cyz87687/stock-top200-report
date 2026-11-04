#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
audit_round86_size_artifact.py —— size_growth α 的伪影排查（决定性）

═══ 为什么必须查 ═══

round85 的结论「仅 size_growth 的 α = +52.85%（t +4.29），段C +93.97%（t +3.94）」
若成立，是重大发现。但样本数据显示 **size_growth 有大量 0 分**：
    000001 → 0.0    000002 → 0.0    000011 → 5.79    000017 → 0.0    000021 → 1.18

若一个维度有大量并列 0 分，且组合按「分数降序 + 代码升序」排序，
则**得分相同的一大批股票会按股票代码顺序被选中** ——
这不是选股能力，而是**代码序的偶然性**（小市值股往往集中在某些代码段）。

⇒ 必须验证的三个伪影：

P1 **并列退化**：Top20 里有多少是 0 分？这些 0 分的选中是否由代码序决定？
    检验：把 0 分股票**随机打散**（同分内随机排序），若 α 崩塌 → 是伪影。
    这是最关键的一检。

P2 **覆盖偏误**：size_growth 覆盖 72.9%，缺失的 27.1% 是谁？
    若有值的恰好是特定子集（如仅主板/仅成长股），则「选中有值的」本身
    就是一种筛选，α 可能来自覆盖机制而非维度。

P3 **单期驱动**：α 是否全靠一两期贡献？
    检验：逐期 α 序列；剔除贡献最大的一期后 α 剩多少；
    以及「留一期（leave-one-period-out）」的最小值。

═══ 判决 ═══

若 P1（同分随机打散后 α 崩塌到接近 0）→ **round85 结论作废**，
   size_growth 的 α 是代码序伪影。
若 P1 通过 + P3 稳健 → 是真发现。

用法：python audit_round86_size_artifact.py
"""
import os
import sys
import json
import math
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from audit_round67_backtest_engine import (  # noqa
    composite_all, load_prices, _px, COST_ROUND)

PANEL = os.path.join(HERE, "data", "panel_round66.json")
WEIGHTS = os.path.join(HERE, "factor_weights.json")
OUT = os.path.join(HERE, "audit_round86.json")
DIM = "score_size_growth"


def build_scores(panel, dim_max, dim_only=None, tie_random=None):
    """tie_random: 若给出 rng，则同分内随机打散（用于 P1 检验）。"""
    out = {}
    for d, rows in panel["rows"].items():
        arr = []
        for c, rec in rows.items():
            if dim_only is not None:
                v = rec.get(dim_only)
                if not isinstance(v, (int, float)) or v != v:
                    continue
                arr.append((c, float(v)))
            else:
                s, uw = composite_all(rec, wmap, dim_max)
                if s is not None:
                    arr.append((c, s))
        if tie_random is not None:
            # 按分数分组，组内随机打散，再按分数降序拼回
            groups = {}
            for c, s in arr:
                groups.setdefault(round(s, 10), []).append(c)
            newarr = []
            for sv, cs in groups.items():
                idx = tie_random.permutation(len(cs))
                for j in idx:
                    newarr.append((cs[j], sv))
            newarr.sort(key=lambda x: -x[1])
            arr = newarr
        else:
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


def run(panel, px, scores, topn, hold):
    dates = panel["dates"]
    rebal = [(dates[i], dates[i + 1], i) for i in range(0, len(dates) - 1, hold)]
    pers, prev_w = [], {}
    for idx, (d_sig, d_exec, i0) in enumerate(rebal):
        d_next = rebal[idx + 1][1] if idx + 1 < len(rebal) else None
        picks = scores.get(d_sig) or []
        cands = [(c, s) for c, s in picks if _px(px, c, d_exec) is not None]
        if not cands:
            prev_w = {}
            continue
        sel = cands[:topn]
        if len(sel) < max(5, topn // 4):
            prev_w = {}
            continue
        raw = np.array([max(1e-6, s) for _, s in sel])
        w = _cap_weight(raw, 0.08)
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
        prs = []
        for c, _s in cands:
            p0 = _px(px, c, d_exec)
            p1 = _px(px, c, d_next) if d_next else None
            if p0 and p1 and p0 > 0:
                prs.append(p1 / p0 - 1.0)
        pers.append({"net": net, "port": port,
                     "pool": (sum(prs) / len(prs)) if prs else None,
                     "n_zero_in_sel": sum(1 for _c, s in sel if s == 0),
                     "codes": [c for c, _s in sel]})
        prev_w = wmap
    return pers


def decomp(pers, hold):
    if len(pers) < 4:
        return None
    ann = 244.0 / hold
    net = np.array([p["net"] for p in pers])
    pool = np.array([p["pool"] if p["pool"] is not None else np.nan for p in pers])
    ok = ~np.isnan(pool)
    net, pool = net[ok], pool[ok]
    if len(net) < 4 or pool.var(ddof=1) < 1e-12:
        return None
    b = float(np.cov(net, pool, ddof=1)[0, 1] / pool.var(ddof=1))
    a = float(net.mean() - b * pool.mean())
    resid = net - (a + b * pool)
    s2 = float((resid ** 2).sum() / (len(net) - 2))
    se_a = math.sqrt(s2 / len(net)) if len(net) > 2 else None
    return {"n": len(net), "alpha_ann": float(a * ann),
            "alpha_t": (a / se_a) if se_a and se_a > 1e-12 else None,
            "beta": b,
            "pool_ann": float((1 + pool.mean()) ** ann - 1),
            "excess_ann": float((1 + (net - pool).mean()) ** ann - 1)}


def main():
    print("加载 ...")
    panel = json.load(open(PANEL, encoding="utf-8"))
    px = load_prices()
    wc = json.load(open(WEIGHTS, encoding="utf-8"))
    dim_max = {k: float(v) for k, v in wc["dim_max"].items()}
    wmap = {k: float(v) for k, v in wc["weights"].items()}
    hold, topn = 10, 20

    # ═══ P1a 分布画像 ═══
    print("\n" + "═" * 100)
    print("P1a  size_growth 的分布与并列情况")
    print("═" * 100)
    zeros = tot = 0
    nz_hist = {}
    for d in panel["dates"]:
        for c, rec in (panel["rows"].get(d) or {}).items():
            v = rec.get(DIM)
            if isinstance(v, (int, float)) and v == v:
                tot += 1
                if v == 0:
                    zeros += 1
                nz_hist[round(v, 2)] = nz_hist.get(round(v, 2), 0) + 1
    print(f"  全样本有效单元格 {tot}，其中 0 分 {zeros}（{zeros/max(tot,1)*100:.1f}%）")
    print(f"  唯一值个数 {len(nz_hist)}，最高频前 8: "
          f"{sorted(nz_hist.items(), key=lambda kv:-kv[1])[:8]}")
    print(f"  ⚠️ 0 分占比 {zeros/max(tot,1)*100:.1f}% → 同分并列严重")

    # ═══ P1b 同分随机打散 ═══
    print("\n" + "═" * 100)
    print("P1b  决定性检验：同分内随机打散后 α 是否保持")
    print("═" * 100)
    sc_code = build_scores(panel, dim_max, dim_only=DIM)          # 代码序（round85 的做法）
    base = decomp(run(panel, px, sc_code, topn, hold), hold)
    print(f"  代码序（同分按股票代码升序）  α年化 {base['alpha_ann']*100:+.2f}%  "
          f"t {base['alpha_t']:+.2f}  β {base['beta']:.3f}")
    print()
    print("  10 次同分随机打散：")
    als = []
    for si in range(10):
        rng = np.random.default_rng(70000 + si)
        sc = build_scores(panel, dim_max, dim_only=DIM, tie_random=rng)
        m = decomp(run(panel, px, sc, topn, hold), hold)
        if m:
            als.append(m["alpha_ann"])
            print(f"    第{si+1:2d}次  α年化 {m['alpha_ann']*100:+8.2f}%  "
                  f"t {m['alpha_t']:+6.2f}  β {m['beta']:.3f}")
    if als:
        print(f"\n  随机打散后 α: 中位 {np.median(als)*100:+.2f}%  "
              f"区间 [{min(als)*100:+.2f}%, {max(als)*100:+.2f}%]")
        collapse = abs(np.median(als)) < abs(base["alpha_ann"]) * 0.5
        if collapse:
            print("  ⇒ ⚠️ **α 崩塌** → round85 的 α 主要来自**同分按代码序**的伪影，")
            print("     size_growth 单维结论**作废**。")
        else:
            print("  ⇒ ✅ α 未崩塌 → α 不依赖代码序，是真实的维度能力。")

    # ═══ P2 覆盖偏误 ═══
    print("\n" + "═" * 100)
    print("P2  覆盖偏误：有值 vs 无值样本的差异")
    print("═" * 100)
    d0 = panel["dates"][-1]
    rows = panel["rows"][d0]
    has = [c for c, r in rows.items() if isinstance(r.get(DIM), (int, float))]
    non = [c for c, r in rows.items() if not isinstance(r.get(DIM), (int, float))]
    print(f"  截面 {d0}：有值 {len(has)}，无值 {len(non)}")
    for nm, grp in (("有值", has), ("无值", non)):
        ep = [rows[c].get("score_ep") for c in grp
              if isinstance(rows[c].get("score_ep"), (int, float))]
        lv = [rows[c].get("score_lowvol20") for c in grp
              if isinstance(rows[c].get("score_lowvol20"), (int, float))]
        fv = [rows[c].get("score_fund") for c in grp
              if isinstance(rows[c].get("score_fund"), (int, float))]
        print(f"    {nm}: n={len(grp):5d}  ep均值 {np.mean(ep):6.3f}  "
              f"lowvol20均值 {np.mean(lv):6.3f}  fund均值 {np.mean(fv):6.3f}")

    # ═══ P3 单期驱动 ═══
    print("\n" + "═" * 100)
    print("P3  α 是否被单期驱动（leave-one-period-out）")
    print("═" * 100)
    pers = run(panel, px, sc_code, topn, hold)
    full = decomp(pers, hold)
    print(f"  全样本 α年化 {full['alpha_ann']*100:+.2f}%")
    loo = []
    for i in range(len(pers)):
        sub = pers[:i] + pers[i + 1:]
        m = decomp(sub, hold)
        if m:
            loo.append((pers[i]["exec"], m["alpha_ann"]))
    loo_sorted = sorted(loo, key=lambda kv: kv[1])
    print(f"  剔除任一期后的 α 最小值: {loo_sorted[0][0]} → {loo_sorted[0][1]*100:+.2f}%")
    print(f"                            最大值: {loo_sorted[-1][0]} → {loo_sorted[-1][1]*100:+.2f}%")
    n_pos = sum(1 for _, v in loo if v > 0)
    print(f"  {n_pos}/{len(loo)} 次剔除后 α 仍为正 → "
          f"{'✅ 不依赖单期' if n_pos >= len(loo)*0.8 else '⚠️ 依赖单期'}")

    # 逐期 α
    print("\n  逐期贡献（组合收益 − β×池收益 − α）：")
    ann = 244.0 / hold
    pool = np.array([p["pool"] for p in pers])
    net = np.array([p["net"] for p in pers])
    b = full["beta"]
    a = full["alpha_per_period"] if "alpha_per_period" in full else (
        net.mean() - b * pool.mean())
    resid = net - (a + b * pool)
    contrib = np.sort(np.abs(resid))[::-1]
    tot_abs = contrib.sum()
    print(f"    最大一期占总残差绝对值 {contrib[0]/tot_abs*100:.1f}%，"
          f"前 3 期占 {contrib[:3].sum()/tot_abs*100:.1f}%")
    if contrib[0] / tot_abs > 0.5:
        print("    ⚠️ **单期主导** → α 不稳健")
    else:
        print("    ✅ 贡献分散，非单期主导")

    json.dump({
        "meta": {"round": 86,
                 "purpose": "size_growth α 的伪影排查：并列退化 / 覆盖偏误 / 单期驱动",
                 "panel": os.path.relpath(PANEL, HERE),
                 "config": {"topn": topn, "hold": hold}},
        "distribution": {"total_cells": tot, "zero_cells": zeros,
                         "zero_pct": zeros / max(tot, 1),
                         "n_unique": len(nz_hist)},
        "code_order": base,
        "tie_random_alphas": [float(x) for x in als],
        "tie_random_median": float(np.median(als)) if als else None,
        "collapsed": bool(als and abs(np.median(als)) < abs(base["alpha_ann"]) * 0.5),
        "loo_alpha": {k: v for k, v in loo},
        "loo_n_positive": n_pos, "loo_total": len(loo),
        "max_period_share": float(contrib[0] / tot_abs),
        "top3_share": float(contrib[:3].sum() / tot_abs),
    }, open(OUT, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"\n→ {OUT}")


if __name__ == "__main__":
    main()
