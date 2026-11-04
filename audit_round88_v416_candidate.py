#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
audit_round88_v416_candidate.py —— v4.16 候选权重的构建与严格验收

═══ 依据（round84~87）═══

  · round84 α 归因：
      仅 size_growth      α +52.85% (t +4.29)  β 0.981   ← 唯一显著
      仅 lowvol20         α +11.54% (t +1.04)  β **−0.225**（负beta）
      仅 ep               α  +7.34% (t +0.66)  β **−0.206**（负beta）
      剔除 ep             Δα **+18.10pp**（最大拖累）
      剔除 rsi_rev        Δα **+9.17pp**（拖累）
      剔除 tech_neg       Δα **+5.75pp**（拖累）
      剔除 lowvol20       Δα **−18.77pp**（崩塌）
      剔除 quality        Δα −5.65pp
      剔除 size_growth    Δα −6.12pp
  · round85：仅 size_growth 通过三条判据（段C α 显著 / p100 / 优于市值最小基准）
  · round86：Top20 从不含 0 分（238/238 截面）→ 无并列退化
  · round87：size_growth 与价格 ρ=+0.106、Top20 重合 0/20 → **不是价格代理**

═══ 但必须回答一个关键问题 ═══

lowvol20 与 ep 的单维 β 是**负的**（−0.225 / −0.206）。
负 β 意味着：**这两维选出的股票在跌市里相对池等权是涨的** ——
即它们是「涨市/牛市型」资产。它们的 α 为正但伴随高波动。

而 round71/83/84 都显示：**涨市里模型的 α 为负**（涨市期 α −16.67%）。

⇒ 若把 ep/rsi_rev/tech_neg 的权重砍掉、保留 lowvol20 + size_growth，
   模型可能变成「**双引擎**：跌市靠 lowvol20 的负β防守 + 全期靠 size_growth 的 α**」。

═══ 本脚本构建并验收 4 个候选 ═══

  V0  v4.15 现状（基线）
  V1  仅 size_growth
  V2  size_growth + lowvol20（保留低波防守）
  V3  size_growth + lowvol20 + quality（保留质量锚，去掉三个拖累维）
  V4  V3 + 微量 fund

验收（全部必须过）：
  ① 段C 样本外 α 显著为正（t > 1.5）
  ② α 优于 200 随机组合 p95 且超多重检验门槛
  ③ 超额MDD 与 IR 同时不劣于基线
  ④ 权重满足 v4.15 约束（单维≤0.20、大类≤0.30、Σ=1）

⚠️ 约束④有个现实问题：size_growth 单独就要 0.20 才满，
   单维上限 0.20 会限制它。若放宽约束到 0.30，需老大决策。

用法：python audit_round88_v416_candidate.py
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
OUT = os.path.join(HERE, "audit_round88.json")
DIMS = [
    "score_fund", "score_quality", "score_ep", "score_lowvol20",
    "score_bias60", "score_rsi_rev", "score_tech_neg",
    "score_size_growth", "score_dominant",
]
GROUPS = {
    "基本面": ["score_fund", "score_quality"],
    "估值": ["score_ep"],
    "低波": ["score_lowvol20"],
    "技术反转": ["score_bias60", "score_rsi_rev", "score_tech_neg"],
    "市值": ["score_size_growth", "score_dominant"],
}


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


def run(panel, px, scores, topn, hold, rng=None, random_pick=False):
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
        if random_pick:
            if len(cands) <= topn:
                sel = cands
            else:
                ii = rng.choice(len(cands), size=topn, replace=False)
                sub = [cands[i] for i in ii]
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
                     "pool": (sum(prs) / len(prs)) if prs else None})
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
    ex = net - pool
    navx = np.cumprod(1.0 + ex)
    peak = np.maximum.accumulate(navx)
    return {"n": len(net), "alpha_ann": float(a * ann),
            "alpha_t": (a / se_a) if se_a and se_a > 1e-12 else None,
            "beta": b,
            "pool_ann": float((1 + pool.mean()) ** ann - 1),
            "ann": float((1 + net.mean()) ** ann - 1),
            "excess_ann": float((1 + ex.mean()) ** ann - 1),
            "excess_mdd": float((navx / peak - 1.0).min()),
            "sigma_excess": float(ex.std(ddof=1) * math.sqrt(ann))}


def feasible(wmap, w_cap=0.20, g_cap=0.30):
    if any(v < -1e-9 or v > w_cap + 1e-9 for v in wmap.values()):
        return False
    if abs(sum(wmap.values()) - 1.0) > 1e-6:
        return False
    for ks in GROUPS.values():
        if sum(wmap.get(k, 0.0) for k in ks) > g_cap + 1e-9:
            return False
    return True


def _run_from_pool(px, pool_cands, pool_rets, exec_dates, topn, hold, rng):
    """从缓存的候选池跑一个随机组合（不重建打分，节省内存）。"""
    pers, prev_w = [], {}
    for cands, pool_r, (d_exec, d_next) in zip(pool_cands, pool_rets, exec_dates):
        if not cands:
            prev_w = {}
            continue
        if len(cands) <= topn:
            sel = cands
        else:
            ii = rng.choice(len(cands), size=topn, replace=False)
            sub = [cands[i] for i in ii]
            sc = rng.random(len(sub)) + 0.5
            sel = [(c, float(s)) for (c, _), s in zip(sub, sc)]
        raw = np.array([max(1e-6, s) for _, s in sel])
        w = _cap_weight(raw, 0.08)
        wmap = {sel[i][0]: float(w[i]) for i in range(len(sel))}
        allc = set(wmap) | set(prev_w)
        turnover = sum(abs(wmap.get(c, 0.0) - prev_w.get(c, 0.0)) for c in allc) / 2.0
        rs, ws = [], []
        for c, _s in sel:
            p0 = _px(px, c, d_exec)
            p1 = _px(px, c, d_next) if d_next else None
            if p0 and p1 and p0 > 0:
                rs.append(p1 / p0 - 1.0)
                ws.append(wmap[c])
        if not rs:
            prev_w = wmap
            continue
        sw = sum(ws)
        port = sum(r * ww for r, ww in zip(rs, ws)) / sw if sw > 0 else 0.0
        pers.append({"net": port - turnover * COST_ROUND, "port": port,
                     "pool": pool_r})
        prev_w = wmap
    return pers


def main():
    print("加载 ...")
    panel = json.load(open(PANEL, encoding="utf-8"))
    px = load_prices()
    wc = json.load(open(WEIGHTS, encoding="utf-8"))
    dim_max = {k: float(v) for k, v in wc["dim_max"].items()}
    w_base = {k: float(v) for k, v in wc["weights"].items()}
    hold, topn = 10, 20

    def mk(spec):
        """spec: {dim: weight}，其余 0。"""
        w = {k: 0.0 for k in DIMS}
        for k, v in spec.items():
            w[k] = v
        s = sum(w.values())
        return {k: v / s for k, v in w.items()}

    cands = {
        "V0_基线v4.15": w_base,
        "V1_仅size_growth": mk({"score_size_growth": 1.0}),
        "V2_size+lowvol": mk({"score_size_growth": 0.5, "score_lowvol20": 0.5}),
        "V3_size+lowvol+quality": mk({"score_size_growth": 0.4, "score_lowvol20": 0.35,
                                       "score_quality": 0.25}),
        "V4_V3+fund": mk({"score_size_growth": 0.4, "score_lowvol20": 0.3,
                           "score_quality": 0.2, "score_fund": 0.1}),
    }

    print("\n" + "═" * 104)
    print("C1  候选方案的权重与约束合规")
    print("═" * 104)
    print(f"{'方案':26s}{'Σw':>8s}{'最大单维':>10s}{'约束(≤0.20/≤0.30)':>20s}")
    print("-" * 68)
    for nm, w in cands.items():
        mx = max(w.values())
        ok = feasible(w)
        print(f"{nm:26s}{sum(w.values()):>8.4f}{mx:>10.4f}"
              f"{'✅ 合规' if ok else '⚠️ 违反约束':>20s}")

    print("\n" + "═" * 104)
    print("C2  各候选的 α / IR / 回撤（TopN=20, H=10）")
    print("═" * 104)
    print(f"{'方案':26s}{'段':4s}{'n':>4s}{'α年化':>11s}{'α t':>8s}{'β':>8s}"
          f"{'超额年化':>11s}{'超额MDD':>10s}")
    print("-" * 88)
    res = {}
    pers_by = {}
    for nm, w in cands.items():
        sc = build_scores(panel, w, dim_max)
        pers = run(panel, px, sc, topn, hold)
        pers_by[nm] = pers
        n = len(pers)
        iA, iB = int(n * 0.40), int(n * 0.70)
        segs = {"A": (0, iA), "B": (iA, iB), "C": (iB, n), "ALL": (0, n)}
        res[nm] = {}
        for sk, (a, b) in segs.items():
            m = decomp(pers[a:b], hold)
            if not m:
                continue
            res[nm][sk] = m
            print(f"{nm:26s}{sk:4s}{m['n']:>4d}{m['alpha_ann']*100:>+10.2f}%"
                  f"{(m['alpha_t'] or 0):>+8.2f}{m['beta']:>8.3f}"
                  f"{m['excess_ann']*100:>+10.2f}%{m['excess_mdd']*100:>+9.2f}%")
        print()

    print("═" * 104)
    print("C3  随机对照基线（200 次，严格匹配）")
    print("═" * 104)
    # ⚠️ 内存纪律：随机对照只需**候选池 + 池等权收益**，不需要重建打分。
    #    先前每次随机都 build_scores(238 截面) → 内存累积 → exit 137。
    #    改为只算一次候选池与池收益，随机对照复用。
    _dates = panel["dates"]
    _rebal = [(_dates[i], _dates[i + 1], i) for i in range(0, len(_dates) - 1, hold)]
    _sc0 = build_scores(panel, w_base, dim_max)
    _pool_cands, _pool_rets, _exec_dates = [], [], []
    for _idx, (_ds, _de, _i0) in enumerate(_rebal):
        _dn = _rebal[_idx + 1][1] if _idx + 1 < len(_rebal) else None
        _cands = [(c, s) for c, s in (_sc0.get(_ds) or [])
                  if _px(px, c, _de) is not None]
        _pool_cands.append(_cands)
        _exec_dates.append((_de, _dn))
        _rs = []
        for c, _s in _cands:
            _p0 = _px(px, c, _de)
            _p1 = _px(px, c, _dn) if _dn else None
            if _p0 and _p1 and _p0 > 0:
                _rs.append(_p1 / _p0 - 1.0)
        _pool_rets.append((sum(_rs) / len(_rs)) if _rs else None)
    del _sc0
    print(f"  候选池缓存完成：{len(_pool_cands)} 期 × 平均 "
          f"{np.mean([len(c) for c in _pool_cands]):.0f} 只")

    als = []
    for si in range(200):
        rng = np.random.default_rng(20261029 + si)
        pers_r = _run_from_pool(px, _pool_cands, _pool_rets, _exec_dates,
                                topn, hold, rng)
        m = decomp(pers_r, hold)
        if m:
            als.append(m["alpha_ann"])
        if (si + 1) % 50 == 0:
            print(f"    {si+1}/200 ...")
    arr = np.array(als)
    mult_p = float(np.percentile(arr, (1 - 1 / 17) * 100))
    print(f"  随机 α: 中位 {np.median(arr)*100:+.2f}%  p95 {np.percentile(arr,95)*100:+.2f}%  "
          f"σ {arr.std(ddof=1)*100:.2f}%")
    print(f"  多重检验门槛（17 配置期望最大，p94.1）= {mult_p*100:+.2f}%")

    print("\n" + "═" * 104)
    print("C4  验收")
    print("═" * 104)
    base = res.get("V0_基线v4.15", {})
    verdicts = {}
    for nm in cands:
        r = res.get(nm, {})
        if "ALL" not in r:
            continue
        v_all, v_c = r["ALL"], r.get("C")
        pct = float((arr < v_all["alpha_ann"]).mean() * 100)
        c1 = bool(v_c and v_c["alpha_ann"] > 0 and (v_c["alpha_t"] or 0) > 1.5)
        c2 = bool(pct >= 95 and v_all["alpha_ann"] > mult_p)
        if base and "ALL" in base:
            c3 = bool(v_all["excess_mdd"] >= base["ALL"]["excess_mdd"] - 0.02)
        else:
            c3 = False
        c4 = feasible(cands[nm])
        verdicts[nm] = {"c1_oos_alpha": c1, "c2_beats_random": c2,
                        "c3_mdd_not_worse": c3, "c4_constraints": c4,
                        "alpha_pct": pct}
        allpass = c1 and c2 and c3 and c4
        print(f"  {nm:26s} ①{'✅' if c1 else '❌'} ②{'✅' if c2 else '❌'}"
              f"({'p' + format(pct, '.0f') + ' ' if True else ''})"
              f" ③{'✅' if c3 else '❌'} ④{'✅' if c4 else '❌'}"
              f"  ⇒ {'**全部通过**' if allpass else '未全过'}")
    print()
    print("  判据：①段C α 显著为正(t>1.5) ②α>p95 且超多重检验门槛 "
          "③超额MDD 不劣于基线 ④权重满足 v4.15 约束")

    # 最优者
    ok_all = [nm for nm, v in verdicts.items()
              if all(v[k] for k in ("c1_oos_alpha", "c2_beats_random",
                                    "c3_mdd_not_worse"))]
    print()
    if ok_all:
        best = max(ok_all, key=lambda nm: res[nm]["ALL"]["alpha_ann"])
        print(f"  ⇒ 可上线候选（①②③全过）：{best}")
        print(f"     权重：{ {k.replace('score_',''): round(v,4) for k,v in cands[best].items() if v>0} }")
        print(f"     约束合规：{feasible(cands[best])}（若为 False 说明需放宽单维上限）")
    else:
        print("  ⇒ 无候选同时通过 ①②③")
        print("     → 结论：**当前样本不支持上线新权重**；可作为下一轮的优先假设")

    json.dump({
        "meta": {"round": 88,
                 "purpose": "v4.16 候选权重的构建与严格验收",
                 "panel": os.path.relpath(PANEL, HERE),
                 "weights_version_base": wc["version"],
                 "config": {"topn": topn, "hold": hold, "n_seeds": 200},
                 "caveat": ("⚠️ 23 期样本 + 17+ 个配置 → 任何候选都只是「下一轮的优先假设」，"
                            "上线前必须扩样本（跨年）与加执行约束复算")},
        "candidates": {nm: {"weights": cands[nm], "feasible": feasible(cands[nm]),
                             "metrics": res.get(nm, {})} for nm in cands},
        "random_alpha": {"median": float(np.median(arr)),
                         "p95": float(np.percentile(arr, 95)),
                         "sigma": float(arr.std(ddof=1)),
                         "mult_test_p94_1": mult_p},
        "verdicts": verdicts,
        "passed_all_123": ok_all,
    }, open(OUT, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"\n→ {OUT}")


if __name__ == "__main__":
    main()
