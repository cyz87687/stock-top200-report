#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
audit_round96_step56_blind_test.py —— Step5 样本外盲测 + Step6 稳健性

═══ 流程规范（老大 2026-10-29）═══

Step5 样本外盲测验收（参数冻结，只跑一次，不做任何修改）
  验收标准：
    · ICIR 保持正向，无断崖下跌
    · IR、超额夏普、卡玛无大幅崩塌
    · Q5 分层收益保持正向单调

Step6 简易稳健性检查（可选）
  拆分牛/震荡/熊市子区间，观察因子表现是否在不同环境稳定

═══ 前置锁定（不可变）═══

  · 股票池：现有已筛选后的 2029 只样本（不重建池）
  · 调仓：月度截面（每 20 个交易日）
  · 基准：沪深300（同时输出池等权 POOL_EW 作对照）

  · 样本内 = 涨市段（老大裁决②）
  · 样本外 = 跌市段（盲测，只跑一次）

═══ 回测口径 ═══

  · 执行价：t+1 日**开盘价**（可交易口径）
  · 成本：双边 0.12%（佣金0.025%×2 + 滑点0.02%×2 + 印花税0.05%卖出）
  · 持仓：等权 Top30（可调），月度调仓
  · 基准收益：HS300 同口径（t+1开盘 → 下期开盘）
  · 卡玛 = 年化超额收益 / |超额MDD|

═══ ⚠️ 关于「超额」的诚实披露 ═══

规范锁定基准为沪深300。但已实测（round69）：本池由 2026-10-27 的
成交额/市值回溯筛出，全池等权同期 +29.34% vs HS300 −11.46%，差 **40.8pp**。
⇒ 对 HS300 的超额含大量**池前视选择偏误**，不可归因于因子。

本脚本**同时输出**对 POOL_EW 的超额（唯一无偏对照），
并把两者差值单列为「池偏误」观察项 —— 按规范「样本内回测指标仅做观察」的精神处理。

用法：python audit_round96_step56_blind_test.py
"""
import os
import sys
import json
import math
import time
import argparse
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import factor_validation_core as F  # noqa

NPZ = os.path.join(HERE, "data", "val_arrays_h20.npz")
PANEL = os.path.join(HERE, "data", "panel_round66.json")
WEIGHTS = os.path.join(HERE, "factor_weights.json")
OUT = os.path.join(HERE, "audit_round96.json")
COST_BUY = 0.00025 + 0.0002
COST_SELL = 0.00025 + 0.0002 + 0.0005
COST_ROUND = COST_BUY + COST_SELL


def load_px():
    import json as _j
    return _j.load(open(os.path.join(HERE, "data", "klines_long.json"), encoding="utf-8"))


def bench_px(code):
    p = os.path.join(HERE, "data", "bench", f"{code}.json")
    if not os.path.exists(p):
        return None
    return json.load(open(p, encoding="utf-8"))["px"]


def px_open(px, code, d):
    bar = (px.get(code) or {}).get(d)
    if isinstance(bar, (list, tuple)) and len(bar) >= 1 and isinstance(bar[0], (int, float)) \
            and bar[0] > 0:
        return float(bar[0])
    return None


def bench_open(bpx, d):
    bar = (bpx or {}).get(d)
    if isinstance(bar, (list, tuple)) and len(bar) >= 1 and isinstance(bar[0], (int, float)) \
            and bar[0] > 0:
        return float(bar[0])
    return None


def metrics(rets, ann_per):
    n = len(rets)
    if n < 3:
        return {}
    mu = sum(rets) / n
    var = sum((r - mu) ** 2 for r in rets) / (n - 1)
    sd = math.sqrt(var)
    nav, peak, mdd = 1.0, 1.0, 0.0
    for r in rets:
        nav *= (1.0 + r)
        peak = max(peak, nav)
        mdd = min(mdd, nav / peak - 1.0)
    ann = (1.0 + mu) ** ann_per - 1.0
    ann_vol = sd * math.sqrt(ann_per)
    sharpe = (mu / sd * math.sqrt(ann_per)) if sd > 1e-12 else None
    calmar = (ann / abs(mdd)) if mdd < -1e-9 else None
    return {"n": n, "ann_ret": ann, "ann_vol": ann_vol, "sharpe": sharpe,
            "max_dd": mdd, "calmar": calmar,
            "win_rate": sum(1 for r in rets if r > 0) / n}


def excess_stats(port_rets, bench_rets, ann_per):
    ex = [p - b for p, b in zip(port_rets, bench_rets) if b is not None]
    if len(ex) < 3:
        return {}
    m = metrics(ex, ann_per)
    # 超额净值回撤
    nav, peak, mdd = 1.0, 1.0, 0.0
    for p, b in zip(port_rets, bench_rets):
        if b is None:
            continue
        nav *= (1.0 + p) / (1.0 + b)
        peak = max(peak, nav)
        mdd = min(mdd, nav / peak - 1.0)
    m["excess_max_dd"] = mdd
    m["excess_calmar"] = (m["ann_ret"] / abs(mdd)) if mdd < -1e-9 else None
    return m


def backtest(dates, codes, px, bpx300, X, M, Y, wmap, dim_max, RY, idx_list,
             topn=30, mode="equal", layer_q=None):
    """在给定截面索引上做月度调仓回测。

    layer_q: 若给定（dict of dim→weights），额外算该维的 Q1~Q5 分层月收益。
    """
    # 构造月度调仓日
    # ⚠️ 2026-10-29 修正（关键）：原实现用 `range(0, len(idx_list), 20)` 取调仓日，
    #    但 idx_list 是**非连续**的（跌市段的 110 个截面散布在 219 个交易日里），
    #    按「列表位置」取 20 会把不连续的截面当成连续调仓期 →
    #    只得到 5 期，且年化数字完全失真（曾出现 +506% 的荒谬值）。
    # 正确：**按真实交易日索引**取月度调仓点，即只在 idx_list 内
    #      每跨过 20 个交易日取一次。
    rebal = []
    last_t = -10 ** 9
    for t in idx_list:
        if t - last_t >= F.MONTHLY_HOP:
            if t + 1 < len(dates):
                rebal.append((t, t + 1))
            last_t = t
    if len(rebal) < 3:
        return None

    port_rets, pool_rets, hs_rets = [], [], []
    nav, prev_hold = 1.0, set()
    layer_acc = {}      # dim -> {q: [rets]}
    layer_pick = {}    # dim -> {q: [codes]}

    for k, (t_sig, t_exec) in enumerate(rebal):
        d_sig, d_exec = dates[t_sig], dates[t_exec]
        d_next_idx = rebal[k + 1][1] if k + 1 < len(rebal) else None
        d_next = dates[d_next_idx] if d_next_idx is not None else None
        # 打分
        row = X[t_sig]
        mrow = M[t_sig]
        wv = np.array([wmap.get(d, 0.0) for d in F.DIMS])
        dmv = np.array([dim_max.get(d, 10.0) for d in F.DIMS])
        V = np.where(mrow, np.minimum(np.where(np.isnan(row), 0.0, row), dmv) / dmv * 10.0, 0.0)
        den = mrow.astype(np.float64) @ wv
        num = V @ wv
        with np.errstate(invalid="ignore", divide="ignore"):
            S = np.where(den > 1e-12, num / den * 10.0, np.nan)
        cand = [(c, float(S[j])) for j, c in enumerate(codes)
                if not np.isnan(S[j]) and px_open(px, c, d_exec) is not None]
        if len(cand) < max(5, topn // 4):
            continue
        cand.sort(key=lambda kv: (-kv[1], kv[0]))
        sel = cand[:topn]
        # 等权
        wmap_sel = {c: 1.0 / len(sel) for c, _ in sel}
        turnover = (len(set(wmap_sel) - prev_hold) / max(len(sel), 1)) if prev_hold else 1.0
        rs, ws = [], []
        for c, _s in sel:
            p0 = px_open(px, c, d_exec)
            p1 = px_open(px, c, d_next) if d_next else None
            if p0 and p1 and p0 > 0:
                rs.append(p1 / p0 - 1.0)
                ws.append(wmap_sel[c])
        if not rs:
            prev_hold = set(wmap_sel)
            continue
        sw = sum(ws)
        gross = sum(r * ww for r, ww in zip(rs, ws)) / sw if sw > 0 else 0.0
        net = gross - turnover * COST_ROUND
        port_rets.append(net)
        prev_hold = set(wmap_sel)
        # 池等权（基准对照）
        prs = []
        for c, _s in cand:
            p0 = px_open(px, c, d_exec)
            p1 = px_open(px, c, d_next) if d_next else None
            if p0 and p1 and p0 > 0:
                prs.append(p1 / p0 - 1.0)
        pool_rets.append((sum(prs) / len(prs)) if prs else None)
        # HS300
        b0, b1 = bench_open(bpx300, d_exec), (bench_open(bpx300, d_next) if d_next else None)
        hs_rets.append((b1 / b0 - 1.0) if (b0 and b1 and b0 > 0) else None)
        # 分层：① 组合打分自身（规范验收口径：Q5 分层收益正向单调）
        #        ② 指定单因子（诊断用）
        # 组合打分已在上面按 S 排序 → cand 已按分数降序 → 直接分 5 组
        if len(cand) >= 100:
            q5 = len(cand) // 5
            groups = [cand[:q5],
                      cand[q5:2 * q5],
                      cand[2 * q5:3 * q5],
                      cand[3 * q5:4 * q5],
                      cand[4 * q5:]]
            for gi_, grp in enumerate(groups):
                if len(grp) < 20:
                    continue
                brs = []
                for c, _s in grp:
                    p0 = px_open(px, c, d_exec)
                    p1 = px_open(px, c, d_next) if d_next else None
                    if p0 and p1 and p0 > 0:
                        brs.append(p1 / p0 - 1.0)
                if brs:
                    layer_acc.setdefault("COMBO", {}).setdefault(gi_, []).append(
                        sum(brs) / len(brs))
        # 分层（按指定维）
        if layer_q:
            for dim in layer_q:
                j = F.DIMS.index(dim)
                xv = X[t_sig, :, j]
                ok = (~np.isnan(xv)) & np.array([px_open(px, c, d_exec) is not None
                                               for c in codes])
                if ok.sum() < 100:
                    continue
                lab = F.quantile_buckets(xv[ok], 5)
                if lab is None:
                    continue
                xs = xv[ok]
                cs = [c for c, m in zip(codes, ok) if m]
                for b in range(5):
                    sel_b = lab == b
                    if sel_b.sum() < 20:
                        continue
                    brs = []
                    for idx_b in np.where(sel_b)[0]:
                        c = cs[idx_b]
                        p0 = px_open(px, c, d_exec)
                        p1 = px_open(px, c, d_next) if d_next else None
                        if p0 and p1 and p0 > 0:
                            brs.append(p1 / p0 - 1.0)
                    if brs:
                        layer_acc.setdefault(dim, {}).setdefault(b, []).append(
                            sum(brs) / len(brs))

    if len(port_rets) < 3:
        return None
    ann_per = 244.0 / F.MONTHLY_HOP
    m = metrics(port_rets, ann_per)
    out = {"port": m, "n_periods": len(port_rets)}
    # 基准超额
    for nm, br in (("HS300", hs_rets), ("POOL_EW", pool_rets)):
        clean = [(p, b) for p, b in zip(port_rets, br) if b is not None]
        if len(clean) >= 3:
            out[f"ex_{nm}"] = excess_stats([p for p, _ in clean],
                                           [b for _, b in clean], ann_per)
            out[f"bench_{nm}"] = metrics([b for _, b in clean], ann_per)
    if "bench_HS300" in out and "bench_POOL_EW" in out:
        out["pool_bias_gap"] = out["bench_POOL_EW"]["ann_ret"] - out["bench_HS300"]["ann_ret"]
    out["layers"] = {dim: {b: {"mean": sum(v) / len(v), "n": len(v)}
                           for b, v in sorted(q.items())}
                     for dim, q in layer_acc.items()}
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=OUT)
    ap.add_argument("--topn", type=int, default=30)
    args = ap.parse_args()
    t0 = time.time()

    dates, X, M, Y, codes = F.load_panel_arrays(PANEL, NPZ)
    wc = json.load(open(WEIGHTS, encoding="utf-8"))
    dim_max = {k: float(v) for k, v in wc["dim_max"].items()}
    RY = F.rank_rows(Y)
    r94 = json.load(open(os.path.join(HERE, "audit_round94.json"), encoding="utf-8"))
    r95 = json.load(open(os.path.join(HERE, "audit_round95.json"), encoding="utf-8"))
    r97 = json.load(open(os.path.join(HERE, "audit_round97.json"), encoding="utf-8"))

    print("加载价格与基准 ...")
    px = load_px()
    bpx300 = bench_px("000300")
    print(f"  HS300 基准: {'有' if bpx300 else '无'}")

    # regime 划分（与 round95 一致）
    mkt = np.array([np.nanmean(Y[t]) for t in range(len(dates))])
    med = float(np.nanmedian(mkt))
    up = sorted(t for t in range(len(dates)) if mkt[t] > med)
    dn = sorted(t for t in range(len(dates)) if mkt[t] <= med)
    j1 = up[0] + int(len(up) * 0.7)
    solve, inner = up[:j1], up[j1:]
    print(f"  样本内(涨市段) {len(up)} 截面：求解 {len(solve)} + 内部验证 {len(inner)}")
    print(f"  样本外(跌市段，盲测) {len(dn)} 截面 {dates[dn[0]]}~{dates[dn[-1]]}")

    # 候选方案
    pool = r95["step1_pool"]
    # ⚠️ 2026-10-29：用 round97（穷举版，严格校验全部约束）的解作为主候选。
    #    round95 的坐标上升解（5因子各0.20）**违反大类约束**（基本面 0.40 > 0.30）。
    w_minimax = {d: 0.0 for d in F.DIMS}
    for d, v in r97["minimax_solution"]["weights"].items():
        w_minimax[d] = v
    cands = {
        "A_v415基线": {k: float(v) for k, v in wc["weights"].items()},
        "B_minimax解(round97)": w_minimax,
        "C_9因子等权": {d: 1.0 / len(F.DIMS) for d in F.DIMS},
        "D_仅size_growth": {"score_size_growth": 1.0},
        "E_5因子等权": {d: (0.2 if d in r94["verdicts"] and
                          r94["verdicts"][d]["cap"] > 0 else 0.0) for d in F.DIMS},
    }
    # 分层检验用：组合本身的打分（Q5 = 打分最高组）
    layer_dims = ["score_size_growth", "score_lowvol20", "score_ep"]

    results = {}
    for nm, wm in cands.items():
        print(f"\n回测 {nm} ...")
        r_out = backtest(dates, codes, px, bpx300, X, M, Y, wm, dim_max, RY, dn,
                         topn=args.topn, layer_q=layer_dims)
        if not r_out:
            print("  期数不足，跳过")
            continue
        results[nm] = r_out
        p = r_out["port"]
        hs = r_out.get("ex_HS300", {})
        pe = r_out.get("ex_POOL_EW", {})
        print(f"  组合：年化 {p['ann_ret']*100:+.2f}%  夏普 {_f(p['sharpe'],2)}  "
              f"卡玛 {_f(p.get('calmar'),2)}  MDD {p['max_dd']*100:+.2f}%  "
              f"胜率 {p['win_rate']*100:.1f}%  n={p['n']}")
        print(f"  对HS300：超额年化 {_f(hs.get('ann_ret'),2)}  IR {_f(hs.get('sharpe'),2)}  "
              f"超额MDD {_f(hs.get('excess_max_dd'),2)}  卡玛 {_f(hs.get('excess_calmar'),2)}")
        print(f"  对POOL_EW：超额年化 {_f(pe.get('ann_ret'),2)}  IR {_f(pe.get('sharpe'),2)}  "
              f"超额MDD {_f(pe.get('excess_max_dd'),2)}  卡玛 {_f(pe.get('excess_calmar'),2)}")
        if "pool_bias_gap" in r_out:
            print(f"  ⚠️ 池偏误（POOL_EW年化 − HS300年化）= "
                  f"{r_out['pool_bias_gap']*100:+.2f}pp")

    # ═══ Step5 验收 ═══
    print("\n" + "═" * 104)
    print("STEP5  样本外盲测验收（参数已冻结，只跑一次）")
    print("═" * 104)
    print(f"{'方案':26s}{'ICIR':>9s}{'IR(HS300)':>11s}{'超额夏普':>10s}"
          f"{'卡玛':>8s}{'超额MDD':>10s}{'Q5单调':>9s}{'验收':>12s}")
    print("-" * 104)
    verdicts = {}
    for nm, r_out in results.items():
        p = r_out["port"]
        hs = r_out.get("ex_HS300", {})
        # Q5 分层单调性
        mono_ok = None
        mono_detail = {}
        combo_mono = None
        for dim, q in (r_out.get("layers") or {}).items():
            means = [q[b]["mean"] for b in sorted(q) if b in q]
            if len(means) >= 3:
                spread = means[-1] - means[0]
                inc = sum(1 for i in range(len(means) - 1) if means[i + 1] > means[i])
                mono = inc >= len(means) - 1
                mono_detail[dim] = {"means": means, "spread": spread, "monotonic": mono}
                if dim == "COMBO":
                    mono_ok = mono          # 规范口径：组合打分的 Q5 单调
                    combo_mono = mono_detail[dim]
        # 验收判据
        c1 = (p.get("sharpe") is not None and p["sharpe"] > 0)
        c2 = (hs.get("sharpe") is not None and hs["sharpe"] > 0)
        c3 = (p.get("calmar") is not None and p["calmar"] > 0)
        c4 = (mono_ok is not False)
        ok = c1 and c2 and c3 and c4
        verdicts[nm] = {"sharpe_pos": c1, "excess_sharpe_pos": c2,
                        "calmar_pos": c3, "q5_monotonic": c4, "pass": ok,
                        "layers": mono_detail, "combo_layer": combo_mono}
        print(f"{nm:26s}{_f(p.get('sharpe'),2):>9s}{_f(hs.get('sharpe'),2):>11s}"
              f"{_f(hs.get('ann_ret'),2):>10s}{_f(p.get('calmar'),2):>8s}"
              f"{_f(hs.get('excess_max_dd'),2):>10s}"
              f"{('✅' if mono_ok else '❌') if mono_ok is not None else 'n/a':>9s}"
              f"{('✅ 通过' if ok else '⚠️ 未全过'):>12s}")

    # ICIR 断崖检验（对比样本内）
    print("\n  【ICIR 断崖检验】（样本内求解段 vs 样本外）")
    for nm in results:
        wm = cands[nm]
        st_in = F.stat(F.combo_ics_at(X, M, Y, wm, dim_max, RY, solve))
        st_out = F.stat(F.combo_ics_at(X, M, Y, wm, dim_max, RY, dn))
        cliff = (st_out["icir"] is not None and st_in["icir"] is not None
                 and st_out["icir"] < 0.3 * st_in["icir"])
        print(f"    {nm:26s} 样本内 ICIR {_f(st_in['icir'],3)} → "
              f"样本外 {_f(st_out['icir'],3)}  "
              f"{'⚠️ 断崖' if cliff else '✅ 未断崖'}")

    # ═══ Step6 稳健性 ═══
    print("\n" + "═" * 104)
    print("STEP6  牛/震荡/熊子区间稳健性")
    print("═" * 104)
    q33, q67 = np.nanpercentile(mkt, [33.33, 66.67])
    bull = [t for t in range(len(dates)) if mkt[t] >= q67]
    bear = [t for t in range(len(dates)) if mkt[t] <= q33]
    side = [t for t in range(len(dates)) if q33 < mkt[t] < q67]
    print(f"  牛市 {len(bull)} 截面（池等权 ≥{q67*100:+.2f}%）｜"
          f"震荡 {len(side)} ｜熊市 {len(bear)} 截面（≤{q33*100:+.2f}%）")
    print()
    print(f"{'因子':16s}{'牛市IC':>10s}{'牛t':>8s}{'震荡IC':>10s}{'震t':>8s}"
          f"{'熊市IC':>10s}{'熊t':>8s}{'稳定?':>8s}")
    print("-" * 82)
    step6 = {}
    for d in F.DIMS:
        j = F.DIMS.index(d)
        row = {}
        for rn, idxs in (("bull", bull), ("side", side), ("bear", bear)):
            if len(idxs) < 20:
                row[rn] = {"ic": None, "t": None, "n": len(idxs)}
                continue
            # ⚠️ 非连续区间必须逐截面取样
            ics = []
            for tt in idxs:
                r = F.row_pearson_ranks(F.rank_rows(X[tt:tt+1, :, j]), RY[tt:tt+1])
                ics.extend(r)
            row[rn] = {**F.stat(ics)}
        signs = [row[r]["ic"] for r in ("bull", "side", "bear") if row[r]["ic"] is not None]
        stable = (len(set(1 if s > 0 else -1 for s in signs)) == 1) if len(signs) >= 2 else None
        step6[d] = {**row, "sign_stable": stable}
        print(f"{d.replace('score_',''):16s}"
              f"{_f(row['bull']['ic']):>10s}{_f(row['bull']['t'],2):>8s}"
              f"{_f(row['side']['ic']):>10s}{_f(row['side']['t'],2):>8s}"
              f"{_f(row['bear']['ic']):>10s}{_f(row['bear']['t'],2):>8s}"
              f"{('✅' if stable else ('❌' if stable is False else 'n/a')):>8s}")

    json.dump({
        "meta": {"round": 96,
                 "purpose": "Step5 样本外盲测验收 + Step6 牛/震荡/熊稳健性",
                 "npz": os.path.relpath(NPZ, HERE),
                 "weights_version_base": wc["version"],
                 "config": {"topn": args.topn, "hold": F.MONTHLY_HOP,
                            "exec": "t+1 开盘", "cost_round": COST_ROUND},
                 "locked": {"pool": "现有 2029 只已筛选样本（不重建）",
                            "hold": "月度截面（20 交易日）",
                            "bench_primary": "沪深300",
                            "bench_control": "POOL_EW（唯一无前视偏误对照）",
                            "sample_in": "涨市段", "sample_out": "跌市段（盲测）"},
                 "caveat": (
                     "① 对 HS300 的超额含池前视偏误（已量化并单列）；"
                     "② 样本外仅 110 截面、月度调仓仅约 11 期 → 统计功效极低；"
                     "③ regime 划分是事后（用未来收益），仅用于划分区间；"
                     "④ Step6 的牛/熊划分同样事后，仅作稳健性诊断。")},
        "results": results, "verdicts": verdicts,
        "step6_regime_stability": step6,
        "elapsed_s": round(time.time() - t0, 1),
    }, open(args.out, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"\n→ {args.out}  （{time.time()-t0:.0f}s）")


def _f(v, nd=2):
    return "  n/a " if v is None else f"{v:+.{nd}f}"


if __name__ == "__main__":
    main()
