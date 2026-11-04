# -*- coding: utf-8 -*-
"""audit_round91_trend_confirm_ic.py — 趋势确认维 0 权重旁挂 IC 测试（2026-10-31）

背景：audit_round90 实测高分组 85% 空头排列、近20日收益 −22.5%。改进方法 B 层：
新增「趋势确认」维（与现有 9 维负相关，正是 round77~89 指出的缺口方向），
先以 **0 权重旁挂** 测历史 IC，过 Bonferroni(3.90) 再谈 weight_admission_gate。

因子定义（三分量等权 rank 均值，全部只用 ≤t 数据，无前视）：
  above20 = close_t / MA20_t − 1          （价格在 20 日线上方程度）
  slope20 = MA20_t / MA20_{t-5} − 1       （20 日线 5 日斜率）
  mom20   = close_t / close_{t-20} − 1    （20 日动量）
  trend_confirm = mean(rank_pct(above20), rank_pct(slope20), rank_pct(mom20))

方法：
  · 截面：每 5 个交易日一个，需 ≥60 日历史与 ≥21 日未来
  · 池：当前 scored 2042 只 —— ⚠️ 池前视（探索性旁挂，非上线依据）
  · Rank IC（Spearman）per 截面 → mean / t / ICIR / 胜率；分段 = 池等权 f20 正负
  · 企稳条件对照：深度超跌组（close/MA60−1 ≤ −15%）中 站上MA5 vs 未站上 的前瞻收益差
  · 阈值：|t| ≥ 3.90（项目 Bonferroni，304 次累计检验）

用法：python audit_round91_trend_confirm_ic.py [--attach scored.json]
"""
import json
import os
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
os.chdir(HERE)

T_BONF = 3.90


def load_close_matrix():
    kl = json.load(open("data/klines_long.json"))
    dates = sorted({d for v in kl.values() for d in v})
    d_idx = {d: i for i, d in enumerate(dates)}
    codes = sorted(kl)
    # 行=交易日、列=个股 —— 全脚本统一 axis0=时间
    mat = np.full((len(dates), len(codes)), np.nan, dtype=np.float64)
    for ci, c in enumerate(codes):
        for d, arr in kl[c].items():
            mat[d_idx[d], ci] = arr[1]        # [o,c,h,l,vol] → close
    return codes, dates, mat


def rank_pct(a):
    """列向量横截面 rank 分位 (0~1)，NaN 保持 NaN。"""
    n = a.shape[0]
    out = np.full(n, np.nan)
    m = ~np.isnan(a)
    if m.sum() < 30:
        return out
    order = a[m].argsort().argsort()
    out[m] = (order + 0.5) / m.sum()
    return out


def main():
    codes, dates, mat = load_close_matrix()
    sc = json.load(open("top200_scored_2026-10-30.json"))
    pool_codes = {r["code"].replace("sh", "").replace("sz", "").replace("bj", "")
                  for r in sc["results"] if r.get("code")}
    pool_rows = np.array([c in pool_codes for c in codes])
    print(f"日线 {mat.shape[0]} 只 × {mat.shape[1]} 日（{dates[0]}~{dates[-1]}），池内 {pool_rows.sum()} 只")

    ma20 = np.full_like(mat, np.nan)
    ma60 = np.full_like(mat, np.nan)
    for w, out in ((20, ma20), (60, ma60)):
        # ⚠️ axis=0 必须显式：nancumsum 默认 axis=None 会把矩阵拍平成 1 维（round91 首跑踩坑）
        csum = np.nancumsum(np.nan_to_num(mat, nan=0.0), axis=0)
        valid = ~np.isnan(mat)
        vcs = np.cumsum(valid, axis=0)
        for i in range(w, mat.shape[0]):
            s = csum[i] - csum[i - w]
            n = vcs[i] - vcs[i - w]
            with np.errstate(invalid="ignore"):
                out[i] = np.where(n == w, s / w, np.nan)

    mom_mat = np.full_like(mat, np.nan)
    with np.errstate(divide="ignore", invalid="ignore"):
        mom_mat[20:] = mat[20:] / mat[:-20] - 1.0
        slope_mat = np.full_like(mat, np.nan)
        slope_mat[5:] = ma20[5:] / ma20[:-5] - 1.0
        above_mat = mat / ma20 - 1.0
        bias60_mat = mat / ma60 - 1.0
        f5_mat = np.full_like(mat, np.nan)
        f5_mat[:-5] = mat[5:] / mat[:-5] - 1.0
        f20_mat = np.full_like(mat, np.nan)
        f20_mat[:-20] = mat[20:] / mat[:-20] - 1.0
    mom_mat[~np.isfinite(mom_mat)] = np.nan
    slope_mat[~np.isfinite(slope_mat)] = np.nan
    above_mat[~np.isfinite(above_mat)] = np.nan
    bias60_mat[~np.isfinite(bias60_mat)] = np.nan

    sections = list(range(60, mat.shape[0] - 20, 5))
    ics5, ics20, seg = [], [], {"up": [], "down": []}
    stab_diff5, stab_diff20 = [], []
    for t in sections:
        pm = pool_rows & ~np.isnan(f20_mat[t]) & ~np.isnan(above_mat[t]) \
             & ~np.isnan(slope_mat[t]) & ~np.isnan(mom_mat[t])
        if pm.sum() < 200:
            continue
        comp = (rank_pct(above_mat[t, pm]) * 0.5
                + rank_pct(slope_mat[t, pm]) * 0.3
                + rank_pct(mom_mat[t, pm]) * 0.2)
        f5v, f20v = f5_mat[t, pm], f20_mat[t, pm]
        for arr, store in ((f5v, ics5), (f20v, ics20)):
            m = ~np.isnan(arr)
            r1, r2 = rank_pct(comp[m]), rank_pct(arr[m])
            ok = ~np.isnan(r1) & ~np.isnan(r2)
            if ok.sum() >= 200:
                store.append(float(np.corrcoef(r1[ok], r2[ok])[0, 1]))
        # 分段：池等权 f20 均值正/负
        ew = np.nanmean(f20v)
        seg["up" if ew > 0 else "down"].append(ics20[-1] if ics20 else np.nan)
        # 企稳条件对照：深度超跌（bias60≤−15%）内 站上MA5 vs 未站上
        dm = pm & ~np.isnan(bias60_mat[t]) & (bias60_mat[t] <= -0.15) & ~np.isnan(ma20[t])
        if dm.sum() >= 30:
            stab = dm & (mat[t] > ma20[t])       # 用 MA20 近似企稳(站上MA5样本过少时更稳)
            nos = dm & ~stab
            for arr, store in ((f5_mat[t], stab_diff5), (f20_mat[t], stab_diff20)):
                a1, a2 = arr[stab & ~np.isnan(arr)], arr[nos & ~np.isnan(arr)]
                if len(a1) >= 8 and len(a2) >= 8:
                    store.append(float(np.median(a1) - np.median(a2)))

    def tstat(xs):
        xs = [x for x in xs if not np.isnan(x)]
        if len(xs) < 10:
            return None, len(xs)
        a = np.array(xs)
        t = a.mean() / a.std(ddof=1) * np.sqrt(len(a))
        return float(t), len(xs)

    out = {"sections": len(sections), "t_bonf": T_BONF,
           "caveat": "池前视（用当前 scored 池回溯历史），探索性旁挂，非上线依据"}
    for name, xs in (("trend_confirm_vs_f5", ics5), ("trend_confirm_vs_f20", ics20)):
        t, n = tstat(xs)
        a = np.array([x for x in xs if not np.isnan(x)])
        out[name] = {"mean_ic": round(float(a.mean()), 4) if n else None,
                     "t": round(t, 2) if t is not None else None,
                     "icir": round(float(a.mean() / a.std(ddof=1)), 3) if n > 1 else None,
                     "hit": round(float((a > 0).mean() * 100), 1) if n else None,
                     "n_sections": n}
    for k in ("up", "down"):
        t, n = tstat(seg[k])
        a = np.array([x for x in seg[k] if not np.isnan(x)])
        out[f"seg_{k}"] = {"mean_ic": round(float(a.mean()), 4) if n else None,
                           "t": round(t, 2) if t is not None else None, "n": n}
    for name, xs in (("stab_f5_pp", stab_diff5), ("stab_f20_pp", stab_diff20)):
        t, n = tstat(xs)
        a = np.array([x for x in xs if not np.isnan(x)])
        out[name] = {"median_diff_pp": round(float(np.median(a)), 2) if n else None,
                     "mean_diff_pp": round(float(a.mean()), 2) if n else None,
                     "t": round(t, 2) if t is not None else None, "n_sections": n}

    os.makedirs("reports", exist_ok=True)
    with open("reports/round91_trend_confirm_ic.json", "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print(json.dumps(out, ensure_ascii=False, indent=1))

    ok5 = out["trend_confirm_vs_f5"]["t"] and abs(out["trend_confirm_vs_f5"]["t"]) >= T_BONF
    ok20 = out["trend_confirm_vs_f20"]["t"] and abs(out["trend_confirm_vs_f20"]["t"]) >= T_BONF
    print("\n判定：", end="")
    if ok5 or ok20:
        print(f"趋势确认维 IC 过 Bonferroni({T_BONF}) → 可进入 weight_admission_gate 流程")
    else:
        print(f"IC 未过 Bonferroni({T_BONF}) → 继续旁挂观察，不给权重")
    return out


def attach():
    """把当日 trend_confirm（0~1 分位）落库 scored json（0 权重，不改 total/rating）。"""
    codes, dates, mat = load_close_matrix()
    sc_path = sys.argv[2]
    sc = json.load(open(sc_path))
    c2i = {c: i for i, c in enumerate(codes)}
    t = mat.shape[0] - 1
    ma20 = np.nanmean(mat[t - 19:t + 1], axis=0)
    ma20p5 = np.nanmean(mat[t - 24:t - 4], axis=0)
    n_att = 0
    vals_above, vals_slope, vals_mom = {}, {}, {}
    for r in sc["results"]:
        c6 = (r.get("code") or "").replace("sh", "").replace("sz", "").replace("bj", "")
        ci = c2i.get(c6)
        if ci is None or np.isnan(mat[t, ci]):
            r["trend_confirm"] = None
            continue
        px = mat[t, ci]
        vals_above[c6] = px / ma20[ci] - 1 if ma20[ci] == ma20[ci] else None
        vals_slope[c6] = ma20[ci] / ma20p5[ci] - 1 if ma20p5[ci] == ma20p5[ci] else None
        vals_mom[c6] = px / mat[t - 20, ci] - 1 if not np.isnan(mat[t - 20, ci]) else None

    def ranks(d):
        ks = [k for k, v in d.items() if v is not None]
        vs = np.array([d[k] for k in ks])
        order = vs.argsort().argsort()
        return {k: float((o + 0.5) / len(ks)) for k, o in zip(ks, order)}

    ra, rs, rm = ranks(vals_above), ranks(vals_slope), ranks(vals_mom)
    for r in sc["results"]:
        c6 = (r.get("code") or "").replace("sh", "").replace("sz", "").replace("bj", "")
        if c6 in ra and c6 in rs and c6 in rm:
            r["trend_confirm"] = round(ra[c6] * 0.5 + rs[c6] * 0.3 + rm[c6] * 0.2, 4)
            n_att += 1
        else:
            r["trend_confirm"] = None
    with open(sc_path, "w", encoding="utf-8") as f:
        json.dump(sc, f, ensure_ascii=False)
    print(f"attach 完成：{n_att}/{len(sc['results'])} 只落 trend_confirm（0权重，仅数据层）")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--attach":
        attach()
    else:
        main()
