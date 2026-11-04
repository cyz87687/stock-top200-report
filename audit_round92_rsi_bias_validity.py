#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
audit_round92_rsi_bias_validity.py — RSI/乖离（bias60/rsi_rev）因子有效性核查
============================================================================
问题（老大 2026-10-31）：「核查 rsi 和乖离有效果吗，目前高分全是空头排列，
最近3个月区间收益为负」

三路检验（全部用真实数据，禁幻觉）：
  A. 重建原始因子长窗口前瞻 IC（klines_long 2025-06→2026-10，~68 个周截面）
     - bias60_raw = close/MA60 − 1（模型里取负打分：越低分越高）
     - RSI14（Wilder；模型里 <30 满分，越低分越高）
     → 因子方向按模型口径（值越小越好）算 IC 与五档前瞻收益
  B. 实际模型打分前瞻收益（factor_history 89 快照，2026-06-26→10-30）
     - score_bias60 / score_rsi_rev / total vs 前瞻 h5/h20/h60
  C. 最近 3 个月静态核验（10-30 截面高分组 trailing 60 交易日收益）

产出：reports/round92_rsi_bias_validity.json + stdout 表格
纪律：不动任何生产文件/权重；nancumsum 必须显式 axis=0（round91 教训）；
     变量名避开 sc（Bash 安全策略误杀）。
"""
import json
import numpy as np
from collections import defaultdict

HERE = __file__.rsplit("/", 1)[0] or "."
OUT_J = f"{HERE}/reports/round92_rsi_bias_validity.json"


def load_close_matrix():
    kl = json.load(open(f"{HERE}/data/klines_long.json"))
    dates = sorted({d for v in kl.values() for d in v})
    d_idx = {d: i for i, d in enumerate(dates)}
    codes = sorted(kl)
    mat = np.full((len(dates), len(codes)), np.nan, dtype=np.float64)
    for ci, c in enumerate(codes):
        for d, arr in kl[c].items():
            mat[d_idx[d], ci] = arr[1]        # [o,c,h,l,?] → close
    return codes, dates, mat


def rolling_ma(mat, w):
    """窗口 w 简单均值；要求窗内 w 个值全部有效（与 round91 同口径）。"""
    out = np.full_like(mat, np.nan)
    csum = np.nancumsum(np.nan_to_num(mat, nan=0.0), axis=0)   # ⚠️ axis=0 必须显式
    valid = ~np.isnan(mat)
    vcs = np.cumsum(valid, axis=0)
    for i in range(w, mat.shape[0]):
        s = csum[i] - csum[i - w]
        n = vcs[i] - vcs[i - w]
        with np.errstate(invalid="ignore"):
            out[i] = np.where(n == w, s / w, np.nan)
    return out


def rsi14_mat(mat):
    """Wilder RSI14（逐日递推，按列向量化）。

    ⚠️ 初始化必须按列逐个就绪（每个股票在首个"14 个连续有效 delta"窗口
    处用简单均值启动），不能全局只在 t=15 初始化——否则晚上市/停牌复牌
    的列永远 NaN（round92 首跑教训）。
    """
    n_t, n_c = mat.shape
    delta = np.full_like(mat, np.nan)
    delta[1:] = mat[1:] - mat[:-1]
    gain = np.where(delta > 0, delta, 0.0)
    loss = np.where(delta < 0, -delta, 0.0)
    gain[~np.isfinite(delta)] = np.nan
    loss[~np.isfinite(delta)] = np.nan
    fin = np.isfinite(delta)
    # 14 连续有效 delta 的窗口就绪掩码（逐列）
    dcount = np.cumsum(fin, axis=0)
    ready = np.zeros_like(mat, dtype=bool)
    for i in range(14, n_t):
        prev = dcount[i - 14] if i >= 15 else 0
        ready[i] = (dcount[i] - prev) == 14
    ag = np.full_like(mat, np.nan)   # 平均涨
    al = np.full_like(mat, np.nan)   # 平均跌
    with np.errstate(invalid="ignore"):
        for i in range(14, n_t):
            if i > 14:
                ag[i] = (ag[i - 1] * 13 + gain[i]) / 14.0
                al[i] = (al[i - 1] * 13 + loss[i]) / 14.0
            init = ready[i] & ~np.isfinite(ag[i]) if i > 14 else ready[i]
            if init.any():
                lo = max(1, i - 13)
                ag[i] = np.where(init, np.nanmean(gain[lo:i + 1], axis=0), ag[i])
                al[i] = np.where(init, np.nanmean(loss[lo:i + 1], axis=0), al[i])
    with np.errstate(divide="ignore", invalid="ignore"):
        rs = ag / al
        rsi = 100.0 - 100.0 / (1.0 + rs)
        rsi = np.where(al == 0, 100.0, rsi)   # 无跌→100
    rsi[~np.isfinite(rsi)] = np.nan
    return rsi


def rank_ic(fac, fwd):
    """Spearman（秩相关，NaN 成对剔除）。"""
    m = np.isfinite(fac) & np.isfinite(fwd)
    if m.sum() < 30:
        return None, int(m.sum())
    from scipy.stats import spearmanr
    r = spearmanr(fac[m], fwd[m]).correlation
    return (float(r) if r == r else None), int(m.sum())


def seg_t(xs):
    if not xs or len(xs) < 2:
        return None, len(xs)
    import statistics as sta
    mu = sta.mean(xs)
    sd = sta.stdev(xs)
    if sd == 0:
        return None, len(xs)
    return mu / (sd / len(xs) ** 0.5), len(xs)


# ============================================================
def part_a():
    """重建原始因子（bias60_raw / RSI14）长窗口前瞻 IC + 五档收益。"""
    codes, dates, mat = load_close_matrix()
    n_t = len(dates)
    ma60 = rolling_ma(mat, 60)
    with np.errstate(divide="ignore", invalid="ignore"):
        bias_raw = mat / ma60 - 1.0     # 越低 = 越超跌 = 模型分越高
    bias_raw[~np.isfinite(bias_raw)] = np.nan
    rsi = rsi14_mat(mat)                # 越低 = 越超卖 = 模型分越高

    fwd = {}
    for h in (5, 20, 60):
        f = np.full_like(mat, np.nan)
        with np.errstate(divide="ignore", invalid="ignore"):
            f[:-h] = mat[h:] / mat[:-h] - 1.0
        f[~np.isfinite(f)] = np.nan
        fwd[h] = f

    # 每 5 日一个截面（需 MA60 就绪 → 从 t=60 起）
    xs_ics = defaultdict(list)      # (factor, h) -> [ic]
    xs_ics_seg = defaultdict(lambda: defaultdict(list))
    quint = defaultdict(lambda: defaultdict(list))  # factor -> q -> [fwd20]
    hit = defaultdict(lambda: defaultdict(list))
    n_xs = 0
    for t in range(60, n_t - 5, 5):
        pool = np.isfinite(mat[t])
        # 跌市/涨市段：池内等权过去20日收益中位数
        p20 = np.full_like(mat[t], np.nan)
        if t >= 20:
            with np.errstate(divide="ignore", invalid="ignore"):
                p20 = mat[t] / mat[t - 20] - 1.0
        seg = "up" if np.nanmedian(p20[pool]) > 0 else "down"
        for fname, fac_t in (("bias60_raw", bias_raw[t]), ("rsi14_raw", rsi[t])):
            for h in (5, 20, 60):
                m = pool & np.isfinite(fac_t) & np.isfinite(fwd[h][t])
                if m.sum() < 50:
                    continue
                # 模型口径方向：因子值越小越好 → IC 用 −fac 与 fwd
                ic, nn = rank_ic(-fac_t[m], fwd[h][t][m])
                if ic is not None:
                    xs_ics[(fname, h)].append(ic)
                    xs_ics_seg[(fname, h)][seg].append(ic)
                if h == 20:
                    # 五档（按 fac 升序：Q1=最超跌=模型最高分档）
                    vals = fac_t[m]
                    rets = fwd[20][t][m]
                    order = np.argsort(vals)
                    qs = np.array_split(order, 5)
                    for qi, idx in enumerate(qs, 1):
                        quint[fname][qi].extend(rets[idx].tolist())
                        hit[fname][qi].extend((rets[idx] > 0).astype(float).tolist())
                    # 交叉验证：截面内 pooled spearman（对照逐截面 IC 均值）
                    ic_pool, _ = rank_ic(-vals, rets)
                    if ic_pool is not None:
                        xs_ics[(fname, "20_pool")].append(ic_pool)
        n_xs += 1

    out = {"n_cross_sections": n_xs, "window": [dates[0], dates[-1]], "ic": {}, "quintile_fwd20": {}}
    print("==== A. 重建原始因子 · 长窗口前瞻 IC（klines 2025-06→2026-10）====")
    print(f"截面数 {n_xs}（每 5 日）| 池=当日全市场 2138 只口径 | IC 方向：模型口径（值越小越好→正IC=有效）")
    for fname in ("bias60_raw", "rsi14_raw"):
        for h in (5, 20, 60):
            ics = xs_ics[(fname, h)]
            if not ics:
                continue
            import statistics as sta
            tstat, nn = seg_t(ics)
            up = xs_ics_seg[(fname, h)].get("up", [])
            dn = xs_ics_seg[(fname, h)].get("down", [])
            key = f"{fname}_h{h}"
            out["ic"][key] = {
                "mean_ic": round(sta.mean(ics), 4), "n": nn,
                "t": round(tstat, 2) if tstat else None,
                "pct_positive": round(sum(1 for x in ics if x > 0) / len(ics), 3),
                "up_seg_ic": round(sta.mean(up), 4) if up else None, "up_n": len(up),
                "down_seg_ic": round(sta.mean(dn), 4) if dn else None, "down_n": len(dn),
            }
            r = out["ic"][key]
            print(f"  {fname:11s} h{h:<2d}: IC {r['mean_ic']:+.4f} (t={r['t']}, 正占比 {r['pct_positive']:.0%}, n={nn})"
                  f" | 涨市段 {r['up_seg_ic']} (n={r['up_n']}) 跌市段 {r['down_seg_ic']} (n={r['down_n']})")
    print(f"  五档前瞻 20 日收益（Q1=最超跌=模型最高分档 → Q5=最不超跌；均值+中位双口径防离群扭曲）:")
    import statistics as sta
    for fname in ("bias60_raw", "rsi14_raw"):
        row = {}
        for qi in range(1, 6):
            xs = quint[fname][qi]
            hk = hit[fname][qi]
            row[qi] = {"n": len(xs),
                       "mean_fwd20_pct": round(sta.mean(xs) * 100, 2) if xs else None,
                       "median_fwd20_pct": round(sta.median(xs) * 100, 2) if xs else None,
                       "hit_rate": round(sta.mean(hk), 3) if hk else None}
        out["quintile_fwd20"][fname] = row
        qs_str = " | ".join(
            f"Q{qi} 均值{row[qi]['mean_fwd20_pct']:+.2f}%/中位{row[qi]['median_fwd20_pct']:+.2f}%"
            for qi in range(1, 6))
        print(f"  {fname:11s}: {qs_str}")
        # 离群诊断：Q1/Q5 的 p99
        for qi in (1, 5):
            xs = sorted(quint[fname][qi])
            if xs:
                p99 = xs[int(len(xs) * 0.99)] * 100
                print(f"             Q{qi} p99={p99:.1f}% max={xs[-1]*100:.1f}%")
    pool_ics = xs_ics.get(("bias60_raw", "20_pool"), [])
    if pool_ics:
        out["ic"]["bias60_raw_h20_pooled_mean_ic"] = round(sta.mean(pool_ics), 4)
        print(f"  [交叉验证] bias60 逐截面 pooled spearman 均值 IC = {sta.mean(pool_ics):+.4f} (n={len(pool_ics)})")
    pool_ics_r = xs_ics.get(("rsi14_raw", "20_pool"), [])
    if pool_ics_r:
        out["ic"]["rsi14_raw_h20_pooled_mean_ic"] = round(sta.mean(pool_ics_r), 4)
        print(f"  [交叉验证] rsi14 逐截面 pooled spearman 均值 IC = {sta.mean(pool_ics_r):+.4f} (n={len(pool_ics_r)})")
    return out


# ============================================================
def part_b():
    """实际模型打分（factor_history 快照）前瞻收益。"""
    fh = json.load(open(f"{HERE}/factor_history.json"))
    snaps = fh["snapshots"]
    codes, dates, mat = load_close_matrix()
    d_idx = {d: i for i, d in enumerate(dates)}
    c_idx = {c: i for i, c in enumerate(codes)}

    fwd_cache = {}
    for h in (5, 20, 60):
        f = np.full_like(mat, np.nan)
        with np.errstate(divide="ignore", invalid="ignore"):
            f[:-h] = mat[h:] / mat[:-h] - 1.0
        f[~np.isfinite(f)] = np.nan
        fwd_cache[h] = f

    rows_out = defaultdict(list)                    # (score, h) -> [ic]
    seg_ics = defaultdict(lambda: defaultdict(list))
    quint = defaultdict(lambda: defaultdict(list))
    n_used = 0
    snap_dates_used = []
    for snap in snaps:
        d = snap["date"]
        if d not in d_idx:
            continue
        t = d_idx[d]
        fac_vals = defaultdict(dict)    # score -> {ci: val}
        fwd_vals = defaultdict(dict)
        for r in snap["rows"]:
            c6 = r["code"]
            ci = c_idx.get(c6)
            if ci is None:
                continue
            for sname in ("score_bias60", "score_rsi_rev", "total"):
                v = r.get(sname)
                if isinstance(v, (int, float)):
                    fac_vals[sname][ci] = v
        for h in (5, 20, 60):
            for ci in list(fac_vals["total"].keys()) or list(fac_vals["score_bias60"].keys()):
                fv = fwd_cache[h][t, ci]
                if np.isfinite(fv):
                    fwd_vals[h][ci] = fv
        if not any(fwd_vals.values()):
            continue
        # 段：过去20日池内中位收益
        ci_all = [c_idx[r["code"]] for r in snap["rows"] if r["code"] in c_idx and np.isfinite(mat[t, c_idx[r["code"]]])]
        p20s = []
        if t >= 20:
            for ci in ci_all:
                if np.isfinite(mat[t - 20, ci]) and mat[t - 20, ci] > 0:
                    p20s.append(mat[t, ci] / mat[t - 20, ci] - 1.0)
        seg = "up" if (p20s and np.median(p20s) > 0) else "down"
        n_used += 1
        snap_dates_used.append(d)
        for sname in ("score_bias60", "score_rsi_rev", "total"):
            for h in (5, 20, 60):
                common = [ci for ci in fac_vals[sname] if ci in fwd_vals[h]]
                if len(common) < 50:
                    continue
                fac = np.array([fac_vals[sname][ci] for ci in common])
                fw = np.array([fwd_vals[h][ci] for ci in common])
                ic, nn = rank_ic(fac, fw)      # 打分已按"越高越好"方向
                if ic is not None:
                    rows_out[(sname, h)].append(ic)
                    seg_ics[(sname, h)][seg].append(ic)
                if h == 20:
                    order = np.argsort(-fac)    # 高分在前
                    for qi, idx in enumerate(np.array_split(order, 5), 1):
                        quint[sname][qi].extend(fw[idx].tolist())

    import statistics as sta
    out = {"n_snapshots_used": n_used, "window": [snap_dates_used[0], snap_dates_used[-1]] if snap_dates_used else None,
           "ic": {}, "quintile_fwd20": {}}
    print("\n==== B. 实际模型打分前瞻 IC（factor_history 89 快照）====")
    print(f"可用快照 {n_used} 个 | {out['window']} | IC 方向：分越高越好→正IC=有效")
    for sname in ("score_bias60", "score_rsi_rev", "total"):
        for h in (5, 20, 60):
            ics = rows_out[(sname, h)]
            if not ics:
                continue
            tstat, nn = seg_t(ics)
            up = seg_ics[(sname, h)].get("up", [])
            dn = seg_ics[(sname, h)].get("down", [])
            key = f"{sname}_h{h}"
            out["ic"][key] = {
                "mean_ic": round(sta.mean(ics), 4), "n": nn,
                "t": round(tstat, 2) if tstat else None,
                "pct_positive": round(sum(1 for x in ics if x > 0) / len(ics), 3),
                "up_seg_ic": round(sta.mean(up), 4) if up else None, "up_n": len(up),
                "down_seg_ic": round(sta.mean(dn), 4) if dn else None, "down_n": len(dn),
            }
            r = out["ic"][key]
            print(f"  {sname:15s} h{h:<2d}: IC {r['mean_ic']:+.4f} (t={r['t']}, 正占比 {r['pct_positive']:.0%}, n={nn})"
                  f" | 涨市段 {r['up_seg_ic']} (n={r['up_n']}) 跌市段 {r['down_seg_ic']} (n={r['down_n']})")
    for sname in ("score_bias60", "score_rsi_rev", "total"):
        row = {}
        for qi in range(1, 6):
            xs = quint[sname][qi]
            row[qi] = {"n": len(xs),
                       "mean_fwd20_pct": round(sta.mean(xs) * 100, 2) if xs else None,
                       "median_fwd20_pct": round(sta.median(xs) * 100, 2) if xs else None,
                       "hit_rate": round(sum(1 for x in xs if x > 0) / len(xs), 3) if xs else None}
        out["quintile_fwd20"][sname] = row
        qs_str = " | ".join(
            (f"Q{qi} {row[qi]['mean_fwd20_pct']:+.2f}%/{row[qi]['median_fwd20_pct']:+.2f}%"
             if row[qi]["mean_fwd20_pct"] is not None else f"Q{qi} n/a")
            for qi in range(1, 6))
        print(f"  五档 fwd20 {sname:15s} (均值/中位): {qs_str}")

    # ---- 字段覆盖年代（快照 schema 演进：旧模型无 score_bias60/score_rsi_rev）----
    cov = {}
    for s in ("score_bias60", "score_rsi_rev", "total"):
        ds = [snap["date"] for snap in snaps
              if any(isinstance(r.get(s), (int, float)) for r in snap["rows"])]
        cov[s] = {"n_snaps": len(ds), "first": ds[0] if ds else None, "last": ds[-1] if ds else None}
        print(f"  [覆盖] {s:15s}: {len(ds)}/{len(snaps)} 个快照有值（{cov[s]['first']} → {cov[s]['last']}）")
    out["field_coverage"] = cov

    # ---- B2: 最近 3 个月子窗（2026-08-01 起）单独看 ----
    print("\n==== B2. 最近 3 个月子窗（快照日 ≥ 2026-08-01）====")
    out["recent3m_ic"] = {}
    for sname in ("score_bias60", "score_rsi_rev", "total"):
        # 重扫一遍只取子窗
        ics_h = defaultdict(list)
        for snap in snaps:
            d = snap["date"]
            if d < "2026-08-01" or d not in d_idx:
                continue
            t = d_idx[d]
            for h in (5, 20):
                fac_l, fw_l = [], []
                for r in snap["rows"]:
                    ci = c_idx.get(r["code"])
                    if ci is None:
                        continue
                    v = r.get(sname)
                    fv = fwd_cache[h][t, ci] if np.isfinite(mat[t, ci]) else np.nan
                    if isinstance(v, (int, float)) and np.isfinite(fv):
                        fac_l.append(v)
                        fw_l.append(fv)
                if len(fac_l) >= 50:
                    ic, _ = rank_ic(np.array(fac_l), np.array(fw_l))
                    if ic is not None:
                        ics_h[h].append(ic)
        for h in (5, 20):
            ics = ics_h[h]
            if not ics:
                continue
            tstat, nn = seg_t(ics)
            out["recent3m_ic"][f"{sname}_h{h}"] = {
                "mean_ic": round(sta.mean(ics), 4), "n": nn,
                "t": round(tstat, 2) if tstat else None,
                "pct_positive": round(sum(1 for x in ics if x > 0) / len(ics), 3)}
            r = out["recent3m_ic"][f"{sname}_h{h}"]
            print(f"  {sname:15s} h{h:<2d}: IC {r['mean_ic']:+.4f} (t={r['t']}, 正占比 {r['pct_positive']:.0%}, n={nn})")
    return out


# ============================================================
def part_c():
    """最近 3 个月静态：10-30 高分组 trailing 60 交易日收益。"""
    codes, dates, mat = load_close_matrix()
    d_idx = {d: i for i, d in enumerate(dates)}
    c_idx = {c: i for i, c in enumerate(codes)}
    t = d_idx.get("2026-10-30", mat.shape[0] - 1)
    scored = json.load(open(f"{HERE}/top200_scored_2026-10-30.json"))
    rows = scored["results"]

    def trail_ret(c6, h):
        ci = c_idx.get(c6)
        if ci is None or t - h < 0:
            return None
        p0, p1 = mat[t - h, ci], mat[t, ci]
        if not (np.isfinite(p0) and np.isfinite(p1)) or p0 <= 0:
            return None
        return (p1 / p0 - 1.0) * 100.0

    groups = {
        "total≥75": [r for r in rows if isinstance(r.get("total"), (int, float)) and r["total"] >= 75],
        "score_bias60=5": [r for r in rows if r.get("score_bias60") == 5],
        "score_rsi_rev=5": [r for r in rows if r.get("score_rsi_rev") == 5],
        "全池": [r for r in rows if r.get("code")],
    }
    import statistics as sta
    out = {"asof": dates[t], "groups": {}}
    print("\n==== C. 最近 3 个月静态（trailing 60 交易日，截至 %s）====" % dates[t])
    for gname, grp in groups.items():
        for h in (20, 60):
            xs = [x for x in (trail_ret(r["code"].replace("sh", "").replace("sz", "").replace("bj", ""), h) for r in grp) if x is not None]
            key = f"{gname}_trail{h}"
            out["groups"][key] = {
                "n": len(xs),
                "median_pct": round(sta.median(xs), 2) if xs else None,
                "mean_pct": round(sta.mean(xs), 2) if xs else None,
                "pct_positive": round(sum(1 for x in xs if x > 0) / len(xs), 3) if xs else None}
            r = out["groups"][key]
            print(f"  {gname:16s} 近{h}日: 中位 {r['median_pct']:+.2f}% | 均值 {r['mean_pct']:+.2f}% | 正收益占比 {r['pct_positive']:.0%} (n={r['n']})")
    return out


def main():
    res = {"a_raw_factor_long_window": part_a(),
           "b_model_score_forward": part_b(),
           "c_trailing_3m": part_c()}
    with open(OUT_J, "w", encoding="utf-8") as f:
        json.dump(res, f, ensure_ascii=False, indent=1)
    print(f"\n[OK] 报告已存 {OUT_J}")


if __name__ == "__main__":
    main()
