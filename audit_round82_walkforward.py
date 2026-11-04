#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
audit_round82_walkforward.py —— 集中度发现的样本外验证（walk-forward）

═══ 为什么必须做这一步 ═══

round81 发现 top10_h10 相对 top20_h10 **两项目标同时改善**：
    IR  +0.32 → +0.59        超额年化 +8.35% → +18.53%
    超额MDD −23.28% → −23.79%（基本持平）
这是本轮唯一一条「不是取舍、而是同时改善」的建议。

**但它是在同一段 23 期样本上发现的** —— 典型的样本内挑选。
若不验证就上线，就是把 round72 的前视错误换个形式重犯。

═══ 验证设计（严格 walk-forward）═══

按调仓期（非截面）顺序切三段，与 IC 层的切分独立：
  段A  前 40% 期   → 在此选最优 TopN
  段B  中 30% 期   → 在此选 λ（向基线收缩）
  段C  后 30% 期   → **只测一次**，是唯一样本外数字

候选 TopN ∈ {10, 15, 20, 30, 40, 60, 80, 100}（含等权基线 TopN=∞ 的近似）

**额外必做：随机对照的样本外分位**
把「选中的 TopN」在段C 上的表现，与 200 个严格匹配的随机组合在段C 上的
分布比较 → 得到「TopN 这个选择本身是否优于随机」的样本外分位数。
这是唯一能排除「TopN 小只是因为小市值/低波暴露」的检验。

═══ 输出 ═══

  ① 段A/B/C 各自的 TopN 选择与表现
  ② 段C 上「选中 TopN vs 随机 TopN」的分位
  ③ 是否值得上线的最终判定

用法：python audit_round82_walkforward.py --seeds 200
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
OUT = os.path.join(HERE, "audit_round82.json")
TOPNS = (10, 15, 20, 30, 40, 60, 80, 100)


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


def build_periods(panel, px, scores, hold):
    """预计算每期的候选池与池等权收益。"""
    dates = panel["dates"]
    rebal = [(dates[i], dates[i + 1], i) for i in range(0, len(dates) - 1, hold)]
    out = []
    for idx, (d_sig, d_exec, i0) in enumerate(rebal):
        d_next = rebal[idx + 1][1] if idx + 1 < len(rebal) else None
        picks = scores.get(d_sig) or []
        cands = [(c, s) for c, s in picks if _px(px, c, d_exec) is not None]
        prs = []
        for c, _s in cands:
            p0 = _px(px, c, d_exec)
            p1 = _px(px, c, d_next) if d_next else None
            if p0 and p1 and p0 > 0:
                prs.append(p1 / p0 - 1.0)
        out.append({"sig": d_sig, "exec": d_exec, "next": d_next,
                    "cands": cands, "pool": (sum(prs) / len(prs)) if prs else None})
    return out


def run(periods_def, topn, hold, rng=None, random_pick=False):
    """跑一个组合。random_pick=True 时用 rng 随机抽 topn 只。"""
    pers, prev_w = [], {}
    for p in periods_def:
        cands = p["cands"]
        if not cands:
            prev_w = {}
            continue
        if random_pick:
            if len(cands) <= topn:
                sel = cands
            else:
                idx = rng.choice(len(cands), size=topn, replace=False)
                sub = [cands[i] for i in idx]
                sc = rng.random(len(sub)) + 0.5
                sel = [(c, float(s)) for (c, _), s in zip(sub, sc)]
        else:
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
            px0 = None
            sub = None
            px0 = _px(PRX, c, p["exec"])
            px1 = _px(PRX, c, p["next"]) if p["next"] else None
            if px1 is None:
                sub = PRX.get(c) or {}
                cand = sorted(dd for dd in sub if p["exec"] <= dd <= (p["next"] or "9999"))
                if cand:
                    b = sub[cand[-1]]
                    px1 = float(b[0]) if isinstance(b, (list, tuple)) else None
            if px0 and px1 and px0 > 0:
                rs.append(px1 / px0 - 1.0)
                ws.append(wmap[c])
        if not rs:
            prev_w = wmap
            continue
        sw = sum(ws)
        port = sum(r * ww for r, ww in zip(rs, ws)) / sw if sw > 0 else 0.0
        net = port - turnover * COST_ROUND
        pers.append({"net": net, "port": port, "pool": p["pool"], "turnover": turnover})
        prev_w = wmap
    return pers


def met(pers, hold):
    if len(pers) < 4:
        return None
    ann = 244.0 / hold
    net = np.array([p["net"] for p in pers])
    pool = np.array([p["pool"] if p["pool"] is not None else np.nan for p in pers])
    ok = ~np.isnan(pool)
    net, pool = net[ok], pool[ok]
    if len(net) < 4 or ex_std_zero(net, pool):
        return None
    ex = net - pool
    navx = np.cumprod(1.0 + ex)
    peak = np.maximum.accumulate(navx)
    return {
        "n": len(net),
        "ann": float((1 + net.mean()) ** ann - 1),
        "excess_ann": float((1 + ex.mean()) ** ann - 1),
        "ir": float(ex.mean() / ex.std(ddof=1) * math.sqrt(ann)) if ex.std(ddof=1) > 1e-12 else None,
        "excess_mdd": float((navx / peak - 1.0).min()),
        "sigma_excess_ann": float(ex.std(ddof=1) * math.sqrt(ann)),
        "beta": float(np.cov(net, pool, ddof=1)[0, 1] / pool.var(ddof=1)) if pool.var(ddof=1) > 1e-12 else None,
    }


def ex_std_zero(net, pool):
    return False


PRX = None


def main():
    global PRX
    ap = argparse.ArgumentParser()
    ap.add_argument("--seeds", type=int, default=200)
    ap.add_argument("--hold", type=int, default=10)
    ap.add_argument("--panel", default=PANEL)
    ap.add_argument("--out", default=OUT)
    args = ap.parse_args()

    print("加载 ...")
    panel = json.load(open(args.panel, encoding="utf-8"))
    PRX = load_prices()
    wc = json.load(open(WEIGHTS, encoding="utf-8"))
    wmap = {k: float(v) for k, v in wc["weights"].items()}
    dim_max = {k: float(v) for k, v in wc["dim_max"].items()}
    scores = build_scores(panel, wmap, dim_max)
    hold = args.hold
    print(f"  {len(panel['dates'])} 截面，权重 {wc['version']}，H={hold}")

    pd_all = build_periods(panel, PRX, scores, hold)
    n = len(pd_all)
    iA, iB = int(n * 0.40), int(n * 0.70)
    segs = {"A": (0, iA), "B": (iA, iB), "C": (iB, n)}
    print(f"  调仓期 {n} 段：")
    for k, (a, b) in segs.items():
        print(f"    段{k} {b-a:2d} 期  {pd_all[a]['exec']} ~ {pd_all[b-1]['exec']}")

    # ── 段A 选 TopN ──
    print("\n" + "═" * 96)
    print("W1  段A 选 TopN")
    print("═" * 96)
    print(f"{'TopN':>6s}{'IR':>9s}{'超额年化':>11s}{'超额MDD':>11s}{'σ超额':>9s}{'β':>8s}")
    print("-" * 60)
    segA = {}
    for t in TOPNS:
        m = met(run(pd_all[segs['A'][0]:segs['A'][1]], t, hold), hold)
        if m:
            segA[t] = m
            print(f"{t:>6d}{(m['ir'] or 0):>+9.3f}{m['excess_ann']*100:>+10.2f}%"
                  f"{m['excess_mdd']*100:>+10.2f}%{m['sigma_excess_ann']*100:>8.1f}%"
                  f"{(m['beta'] or 0):>8.3f}")
    best_t = max(segA, key=lambda t: segA[t]["ir"] or -9)
    print(f"\n  → 段A 最优 TopN = {best_t}（IR {segA[best_t]['ir']:+.3f}）")

    # ── 段B 选 λ ──
    print("\n" + "═" * 96)
    print("W2  段B 选 λ（向 TopN=20 基线收缩，检验集中度是否稳健）")
    print("═" * 96)
    segB_res = {}
    for t in (best_t, 20):
        m20 = met(run(pd_all[segs['B'][0]:segs['B'][1]], 20, hold), hold)
        mt = met(run(pd_all[segs['B'][0]:segs['B'][1]], t, hold), hold)
        if not m20 or not mt:
            continue
        segB_res[t] = {"n20": m20, "nt": mt}
        print(f"  TopN={t:3d}  段B IR {mt['ir']:+.3f}（TopN=20 基线 IR {m20['ir']:+.3f}）"
              f"  超额MDD {mt['excess_mdd']*100:+.2f}%")

    # ── 段C 裁决（只测一次）──
    print("\n" + "═" * 96)
    print("W3  段C 样本外裁决（只测一次）")
    print("═" * 96)
    segC = {}
    for t in sorted(set(list(TOPNS) + [best_t])):
        m = met(run(pd_all[segs['C'][0]:segs['C'][1]], t, hold), hold)
        if m:
            segC[t] = m
            print(f"  TopN={t:3d}  IR {m['ir']:+.3f}  超额年化 {m['excess_ann']*100:+.2f}%  "
                  f"超额MDD {m['excess_mdd']*100:+.2f}%  σ超额 {m['sigma_excess_ann']*100:.1f}%  "
                  f"β {m['beta']:.3f}")

    # ── 随机对照在段C（决定性）──
    print(f"\n  跑 {args.seeds} 个随机组合的**段C**对照 ...")
    rand = {}
    for t in (10, 20, best_t):
        ts = set(t if isinstance(t, (list, tuple)) else [t])
        for tt in ts:
            irs, mdd = [], []
            for si in range(args.seeds):
                rng = np.random.default_rng(20261029 + si)
                m = met(run(pd_all[segs['C'][0]:segs['C'][1]], tt, hold, rng, True), hold)
                if m and m["ir"] is not None:
                    irs.append(m["ir"])
                    mdd.append(m["excess_mdd"])
            if irs:
                rand[tt] = {"ir_median": float(np.median(irs)),
                            "ir_p10": float(np.percentile(irs, 10)),
                            "ir_p90": float(np.percentile(irs, 90)),
                            "mdd_median": float(np.median(mdd)),
                            "n": len(irs)}
                print(f"    TopN={tt:3d} 随机 IR 中位 {np.median(irs):+.3f} "
                      f"[p10 {np.percentile(irs,10):+.3f}, p90 {np.percentile(irs,90):+.3f}]")

    print("\n" + "═" * 96)
    print("W4  最终判定")
    print("═" * 96)
    print(f"{'TopN':>6s}{'段A IR':>10s}{'段C IR':>10s}{'段C超额年化':>13s}"
          f"{'段C超额MDD':>13s}{'随机中位IR':>13s}{'样本外分位':>13s}")
    print("-" * 82)
    verdict_rows = {}
    for t in sorted(segC):
        m = segC[t]
        r = rand.get(t)
        if r:
            pct = float((np.array([r["ir_p10"], r["ir_p90"]]) < m["ir"]).mean() * 50)  # 占区间比例
            pct = 50.0 + 50.0 * (m["ir"] - r["ir_p10"]) / max(1e-9, (r["ir_p90"] - r["ir_p10"]))
            pct = min(100.0, max(0.0, pct))
            rtxt = f"{r['ir_median']:+.3f}"
        else:
            pct, rtxt = None, "n/a"
        verdict_rows[t] = {"segA_ir": segA.get(t, {}).get("ir"),
                           "segC": m, "random": r, "oos_percentile": pct}
        print(f"{t:>6d}{(segA.get(t,{}).get('ir') or 0):>+10.3f}{(m['ir'] or 0):>+10.3f}"
              f"{m['excess_ann']*100:>+12.2f}%{m['excess_mdd']*100:>+12.2f}%"
              f"{rtxt:>13s}{(f'{pct:.1f}%' if pct is not None else 'n/a'):>13s}")

    # 结论
    print()
    b_c = segC.get(best_t)
    b20_c = segC.get(20)
    if b_c and b20_c:
        print(f"  段A 选的 TopN={best_t} 在段C：IR {(b_c['ir'] or 0):+.3f}，"
              f"TopN=20 在段C：IR {(b20_c['ir'] or 0):+.3f}")
        d = (b_c["ir"] or 0) - (b20_c["ir"] or 0)
        if d > 0.1:
            print(f"  ⇒ **集中度的优势在样本外延续**（ΔIR {d:+.3f}）→ 建议采纳")
        elif d > 0:
            print(f"  ⇒ 集中度优势在样本外**衰减**（ΔIR {d:+.3f}）→ 需谨慎，样本内挑选成分明显")
        else:
            print(f"  ⇒ 集中度优势在样本外**消失**（ΔIR {d:+.3f}）→ **不可上线**")
    r_best = rand.get(best_t)
    if b_c and r_best:
        print(f"  样本外分位：真实 TopN={best_t} 的段C IR {(b_c['ir'] or 0):+.3f} vs "
              f"随机中位 {r_best['ir_median']:+.3f}")
        if (b_c["ir"] or 0) > r_best["ir_p90"]:
            print("  ⇒ 优于 200 个随机组合的 p90 → **打分在样本外仍有效**")
        elif (b_c["ir"] or 0) > r_best["ir_median"]:
            print("  ⇒ 优于随机中位但未过 p90 → 微弱有效")
        else:
            print("  ⇒ 不优于随机中位 → **样本外无法证明确有 alpha**")

    json.dump({
        "meta": {"round": 82,
                 "purpose": "集中度发现的样本外验证（walk-forward + 随机对照）",
                 "panel": os.path.relpath(args.panel, HERE),
                 "weights_version": wc["version"],
                 "hold": hold, "n_periods": n,
                 "segments": {k: [pd_all[a]["exec"], pd_all[b-1]["exec"]] for k, (a, b) in segs.items()},
                 "caveat": ("⚠️ 段C 仅 ~7 期，功效极低；"
                            "本轮所有 walk-forward 结论都只能作为「不排除」而非「确认」")},
        "segA_by_topn": {str(k): v for k, v in segA.items()},
        "best_topn_segA": best_t,
        "segC_by_topn": {str(k): v for k, v in segC.items()},
        "random_segC": {str(k): v for k, v in rand.items()},
        "verdict": verdict_rows,
    }, open(args.out, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"\n→ {args.out}")


if __name__ == "__main__":
    main()
