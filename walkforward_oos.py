#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
walkforward_oos.py —— G5 缺口补齐：**时间稳定性检验 + 前瞻 OOS 登记**
=========================================================================
背景
----
`factor_weights_optimized_*.json` 里 `walkforward.n_oos_dates = 0` 已挂了很久，
说明是「十维池内截面 1 < MIN_TRAIN(30)」——日重估路径只在**当日截图**上找样本，
永远凑不满 30 个截面。而长面板（1653 日 × 2257 只）明明有足够日期。

⭐ 但这里有个**不能糊弄过去的方法论事实**：
    v4.18 的权重是用 2020-01 ~ 2026-10 这**整段**数据反复对照调出来的。
    在同一段上做任何滚动切分、任何"训练/验证"划分，都仍是 in-sample ——
    验证段的标签早已参与过权重的挑选。**把它叫「样本外」是自欺**。
    PBO 也做不了（不能编一批策略去填矩阵，见项目铁律）。

所以本脚本诚实地区分两件事，各自给出可核查的产物：

  ① time_stability（时间稳定性检验，IN-SAMPLE）
     固定权重（**不做任何逐折重拟**），按时间等分 K 折，逐折算
     IC / NW t / TopN 超额。回答的是「信号是靠某一段撑起来的吗」，
     不是「未来能不能赚钱」。折边界做 h 日 embargo，避免 label 跨折。
     ⚠️ 无选择自由度：权重固定 ⇒ 不存在"N 组候选 × V 个验证点"的过拟合，
        这与 weight_admission_gate.walk_forward()（逐折枚举权重）是两回事。

  ② forward_register（前瞻 OOS 登记，真·样本外）
     锁定当前权重指纹，从 oos_start_date 起只做**前向记录**：
     当日 TopN 名单 → h 日后回填真实收益 → 累积独立观测。
     满 N_MIN 个有效独立观测（默认 30，且 h=20 重叠须按 h 折算）
     之前，`n_oos_dates` 如实保持 0，G5 不得改判 PASS。
     这是唯一能真正闭合 G5 的路径。

目标函数对齐 C1 裁决
--------------------
生产持仓只有 6~13 只，故除 Top10% 外**额外报 N6/N10/N13/N30/N50 超额**。
IC 只作辅助：实测 IC 与头部超额方向相反（lowvol20 IC+0.0828 但 TopN 全程负），
绝不能用 IC 排名去裁决权重（见 guard_ic_driven_weights.py）。

用法：
    python3 walkforward_oos.py                 # 产出 data/walkforward_oos.json
    python3 walkforward_oos.py --folds 8 -h 20
"""
import argparse
import hashlib
import json
import os
import sys
import time

import numpy as np
from scipy.stats import rankdata

HERE = os.path.dirname(os.path.abspath(__file__))
MAT = os.path.join(HERE, "data", "panel_r124_mat.npz")
KL = os.path.join(HERE, "data", "klines_2020.npz")
FW = os.path.join(HERE, "factor_weights.json")
OUT = os.path.join(HERE, "data", "walkforward_oos.json")

MIN_CS = 200
MIN_AMOUNT = 2000.0      # 万元
TOPNS = (6, 10, 13, 30, 50, 0)   # 0 = Top10%
N_MIN_OOS = 30           # 真·前瞻独立观测达标线
PASS_RATE_THR = 0.5      # 与 weight_admission_gate.DEFAULT_WF_PASS_RATE 一致

sys.path.insert(0, HERE)
from dim_cfg import load_dim_max  # noqa: E402


def spearman(a, b):
    if len(a) < MIN_CS:
        return None
    ra, rb = rankdata(a), rankdata(b)
    ra -= ra.mean()
    rb -= rb.mean()
    den = float(np.sqrt((ra * ra).sum() * (rb * rb).sum()))
    return float((ra * rb).sum() / den) if den > 0 else None


def nw_t(xs, lag):
    """Newey-West HAC t（Bartlett），与 recompute_ic_long_v2.nw_t 同口径。"""
    x = np.asarray(xs, dtype=np.float64)
    n = len(x)
    if n < 3:
        return None
    mu = x.mean()
    e = x - mu
    g0 = float((e * e).mean())
    var = g0
    for k in range(1, min(lag, n - 1) + 1):
        gk = float((e[k:] * e[:-k]).mean())
        var += 2.0 * (1.0 - k / (lag + 1.0)) * gk
    if var <= 0:
        return None
    return float(mu / np.sqrt(var / n))


def load_weights():
    with open(FW, encoding="utf-8") as f:
        cfg = json.load(f)
    w = {k: float(v) for k, v in (cfg.get("weights") or {}).items()
         if float(v or 0) > 0}
    if abs(sum(w.values()) - 1.0) > 1e-9:
        raise RuntimeError(f"Σw={sum(w.values())} ≠ 1.0")
    sha = hashlib.sha1(open(FW, "rb").read()).hexdigest()
    return w, cfg.get("version"), int(cfg.get("target_horizon") or 0), sha


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--folds", type=int, default=6)
    ap.add_argument("-H", "--horizon", type=int, default=20)
    ap.add_argument("--mat", default=MAT)
    ap.add_argument("--out", default=OUT)
    args = ap.parse_args()
    h, K = args.horizon, args.folds
    t0 = time.time()

    W, VER, TH, SHA = load_weights()
    print(f"权重 {VER} / h={TH} / sha1={SHA[:12]} / 生效 {len(W)} 维")

    z = np.load(args.mat, allow_pickle=False)
    pdates = [str(d) for d in z["dates"]]
    codes = [str(c) for c in z["codes"]]
    dims = [str(d) for d in z["dims"]]
    S, P = z["S"], z["P"]
    DIM_MAX = load_dim_max(["score_bias60", "score_rsi_rev"], strict=True)
    wv = np.array([W.get(d, 0.0) / DIM_MAX.get(d, 10.0) for d in dims],
                  dtype=np.float64)
    eff = sum(W.get(d, 0.0) for d in dims)
    blind = sum(W.get(d, 0.0) for d in W if d not in dims)
    print(f"面板 {len(pdates)} 日 × {len(codes)} 只 × {len(dims)} 维  "
          f"effective Σ={eff:.2f} / 盲区 Σ={blind:.2f} ({[d for d in W if d not in dims]})")

    k = np.load(KL, allow_pickle=False)
    close = k["X"][:, :, 1]
    kdates = [str(d) for d in k["dates"]]
    kcodes = [str(c) for c in k["codes"]]
    if kcodes != codes:
        kmap = {c: i for i, c in enumerate(kcodes)}
        close = close[[kmap[c] for c in codes]]
    kdmap = {d: i for i, d in enumerate(kdates)}

    mask = ~np.isnan(S)
    Sf = np.where(mask, S, 0.0).astype(np.float64)
    num = Sf @ wv
    den = mask.astype(np.float64) @ wv
    with np.errstate(invalid="ignore", divide="ignore"):
        comp = np.where(den > 1e-9, num / np.maximum(den, 1e-12), np.nan)

    amt = P[:, :, 2]
    liq_ok = np.isfinite(amt) & (amt >= MIN_AMOUNT)
    with np.errstate(invalid="ignore", divide="ignore"):
        r1 = np.full_like(close, np.nan, dtype=np.float64)
        r1[:, 1:] = close[:, 1:] / np.where(close[:, :-1] > 0,
                                            close[:, :-1], np.nan) - 1.0
    lim = np.where(np.isin([c[:3] for c in codes],
                           ["300", "301", "688", "689"]), 0.198, 0.098)[:, None]
    limit_up = np.isfinite(r1) & (r1 >= lim)

    # ── 逐日算 IC + 各档 TopN 超额（一次性，供所有折复用）──
    daily = []
    for n, d in enumerate(pdates):
        di = kdmap.get(d)
        if di is None or di + h >= close.shape[1]:
            continue
        srow = comp[n]
        p0 = close[:, di].astype(np.float64)
        p1 = close[:, di + h].astype(np.float64)
        with np.errstate(invalid="ignore", divide="ignore"):
            ret = p1 / np.where(p0 > 0, p0, np.nan) - 1.0
        sel = np.isfinite(srow) & np.isfinite(ret) & liq_ok[n] & ~limit_up[:, di]
        if int(sel.sum()) < MIN_CS:
            continue
        fv, rv = srow[sel], ret[sel]
        ic = spearman(fv, rv)
        if ic is None:
            continue
        o = np.argsort(-fv)
        rs = rv[o]
        exc = {}
        for N in TOPNS:
            kk = N if N > 0 else max(1, len(rs) // 10)
            if kk >= len(rs):
                continue
            exc[f"N{kk}" if N > 0 else "top10pct"] = float(rs[:kk].mean() - rv.mean())
        daily.append({"n": n, "date": d, "ic": ic, "exc": exc,
                      "cs": int(sel.sum())})
    print(f"有效交易日 {len(daily)} / {len(pdates)}（liq+非涨停，cs≥{MIN_CS}）")

    if not daily:
        raise SystemExit("❌ 无有效截面，拒绝产出空结论")

    # ── ① 时间稳定性：等分 K 折 + 折边界 h 日 embargo ──
    #    embargo 去掉每折最后 h 日（其 h 日前瞻 label 会跨到下一折），
    #    以及最前面 h 日（面板 warm-up，反转/低波维尚未有值）。
    idx = np.array([x["n"] for x in daily])
    bounds = np.linspace(0, len(daily), K + 1).astype(int)
    folds = []
    for fi in range(K):
        a, b = bounds[fi], bounds[fi + 1]
        seg = [x for x in daily[a:b]
               if x["n"] >= h and (fi == K - 1 or x["n"] < daily[b - 1]["n"] - h)]
        if len(seg) < 20:
            folds.append({"fold": fi + 1, "n_dates": len(seg),
                          "verdict": "样本不足", "pass": False})
            continue
        ics = [x["ic"] for x in seg]
        excs = {}
        for key in seg[0]["exc"]:
            vs = [x["exc"][key] for x in seg if key in x["exc"]]
            if vs:
                excs[key] = round(float(np.mean(vs)) * 100, 4)
        t = nw_t(ics, h - 1)
        wr = sum(1 for v in ics if v > 0) / len(ics)
        folds.append({
            "fold": fi + 1,
            "date_range": [seg[0]["date"], seg[-1]["date"]],
            "n_dates": len(seg),
            "n_effective_obs": round(len(seg) / h, 1),
            "ic_mean": round(float(np.mean(ics)), 4),
            "nw_t": round(t, 2) if t is not None else None,
            "nw_lag": h - 1,
            "ic_win_rate": round(wr, 3),
            "excess_pct": excs,
            "median_cs": int(np.median([x["cs"] for x in seg])),
            "pass": bool(np.mean(ics) > 0 and wr >= 0.5),
            "verdict": "正 IC 且胜率≥50%" if (np.mean(ics) > 0 and wr >= 0.5)
                       else "未达标",
        })
    n_pass = sum(1 for f in folds if f.get("pass"))
    pass_rate = n_pass / K
    all_ics = [x["ic"] for x in daily]
    overall_exc = {}
    for key in daily[0]["exc"]:
        vs = [x["exc"][key] for x in daily if key in x["exc"]]
        if vs:
            overall_exc[key] = round(float(np.mean(vs)) * 100, 4)

    stability = {
        "label": "时间稳定性检验（IN-SAMPLE，不是样本外）",
        "folds": K,
        "horizon_days": h,
        "embargo_days": h,
        "weight_fitting": "固定权重，逐折不重拟 ⇒ 无选择自由度",
        "n_stability_dates": len(daily),
        "pass_rate": round(pass_rate, 3),
        "pass_rate_threshold": PASS_RATE_THR,
        "g5_pass": bool(pass_rate >= PASS_RATE_THR),
        "overall": {
            "ic_mean": round(float(np.mean(all_ics)), 4),
            "nw_t": round(nw_t(all_ics, h - 1), 2),
            "nw_lag": h - 1,
            "n_effective_obs": round(len(daily) / h, 1),
            "win_rate": round(sum(1 for v in all_ics if v > 0) / len(all_ics), 3),
            "excess_pct": overall_exc,
        },
        "per_fold": folds,
        "caveat": ("⚠️ 权重是在 2020-01~2026-10 全样本上反复对照调出来的，"
                   "本检验的每一折标签都参与过权重挑选 ⇒ **仍是 in-sample**。"
                   "通过只说明「信号不靠单一年份撑起」，不说明「未来有效」。"
                   "折间 IC 符号若翻转，本身即 regime 依赖的直接证据。"),
    }

    # ── ② 前瞻 OOS 登记（真·样本外，当前必然为 0）──
    reg_path = os.path.join(HERE, "data", "oos_forward_register.json")
    if os.path.exists(reg_path):
        with open(reg_path, encoding="utf-8") as f:
            reg = json.load(f)
        if reg.get("weight_sha1") != SHA:
            # 权重变了 → 旧登记作废，重新起算（不能把不同权重的观测混在一起）
            reg = {"reset_history": reg.get("reset_history", []) + [{
                "at": time.strftime("%Y-%m-%d %H:%M:%S"),
                "reason": "权重指纹变更，旧前瞻观测不适用于新权重",
                "old_sha1": reg.get("weight_sha1"),
                "old_n": len(reg.get("observations") or []),
            }], "observations": []}
    else:
        reg = {"observations": []}
    obs = reg.get("observations") or []
    reg.update({
        "weight_sha1": SHA,
        "weight_version": VER,
        "target_horizon": TH,
        "oos_start_date": reg.get("oos_start_date") or time.strftime("%Y-%m-%d"),
        "n_oos_dates": len(obs),
        "n_independent_effective": round(len(obs) / h, 1),
        "n_min_required": N_MIN_OOS,
        "overlap_adjustment": f"h={h} 重叠窗口，n_eff = n/{h}",
        "status": ("PASS·可声明经验证的 alpha" if len(obs) / h >= N_MIN_OOS
                   else "PENDING·样本外观测不足"),
        "rule": ("每日写入当日 TopN 名单，h 日后回填真实收益；"
                 "权重指纹变更即整段作废重来。禁止用历史面板回填充数。"),
        "observations": obs,
    })
    with open(reg_path, "w", encoding="utf-8") as f:
        json.dump(reg, f, ensure_ascii=False, indent=1)

    out = {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "src": {"panel_matrix": os.path.basename(args.mat),
                "klines": os.path.basename(KL),
                "weights": "factor_weights.json"},
        "weight_version": VER,
        "weight_sha1": SHA,
        "target_horizon": TH,
        "effective_weight_sum": round(eff, 4),
        "blind_weight_sum": round(blind, 4),
        "blind_dims": [d for d in W if d not in dims],
        # ⚠️ 这个字段故意保持 0：真·前瞻样本外还没开始积累。
        #    历史面板的时间稳定性检验日期数另记在 time_stability.n_stability_dates。
        "n_oos_dates": len(obs),
        "time_stability": stability,
        "g5_verdict": {
            "pass": bool(pass_rate >= PASS_RATE_THR and len(obs) / h >= N_MIN_OOS),
            "stability_pass": bool(pass_rate >= PASS_RATE_THR),
            "forward_oos_pass": bool(len(obs) / h >= N_MIN_OOS),
            "note": ("G5 需同时满足：时间稳定性通过 AND 前瞻样本外独立观测≥30。"
                     "当前仅前者可测，后者为 0 ⇒ **G5 仍 FAIL**，"
                     "禁宣称『经验证的 alpha』。"),
        },
        "methodology": {
            "why_not_pbo": "PBO 需要真实的多策略收益矩阵；本项目没有可核查的"
                           "策略族，编造策略填矩阵违反数据纪律 → 不做。",
            "why_not_walk_forward_enum": (
                "weight_admission_gate.walk_forward() 逐折枚举权重组合选最优，"
                "其脚本自报选择自由度 N:V 常达数百比一；那是策略搜索，"
                "不是对固定生产权重的样本外检验。本脚本不枚举、不重拟。"),
            "target_function": ("TopN 头部超额（N6/N10/N13/N30/N50）—— 与生产"
                                "持仓 6~13 只及 C1 裁决口径一致；IC 仅辅助。"),
            "filters": {"min_cross_section": MIN_CS,
                        "min_amount_wan": MIN_AMOUNT,
                        "exclude_limit_up": True},
        },
    }
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)

    print("\n=== 时间稳定性（IN-SAMPLE，非 OOS）===")
    for f in folds:
        print(f"  折{f['fold']}  {f.get('date_range', ['-', '-'])[0]}~"
              f"{f.get('date_range', ['-', '-'])[-1]}  n={f.get('n_dates')}  "
              f"IC={f.get('ic_mean')}  NWt={f.get('nw_t')}  "
              f"胜率={f.get('ic_win_rate')}  {'✅' if f.get('pass') else '❌'}")
    print(f"  通过率 {n_pass}/{K} = {pass_rate:.1%}（门槛 {PASS_RATE_THR:.0%}）")
    print(f"  全域 IC={stability['overall']['ic_mean']}  "
          f"NWt={stability['overall']['nw_t']}  "
          f"n_eff={stability['overall']['n_effective_obs']}")
    print(f"  TopN 超额(%): {overall_exc}")
    print(f"\n=== G5 裁决 ===")
    print(f"  time_stability={'PASS' if stability['g5_pass'] else 'FAIL'} / "
          f"forward_oos={'PASS' if out['g5_verdict']['forward_oos_pass'] else 'PENDING(0)'} "
          f"⇒ G5 {'PASS' if out['g5_verdict']['pass'] else 'FAIL'}")
    print(f"  n_oos_dates = {out['n_oos_dates']}（真·前瞻；历史稳定性段 "
          f"{stability['n_stability_dates']} 日不可冒充）")
    print(f"\n写出 {args.out} + {os.path.basename(reg_path)}  耗时 {time.time()-t0:.0f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
