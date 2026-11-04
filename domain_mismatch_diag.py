#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
domain_mismatch_diag.py —— 因子评估域口径诊断（P0-1 固化）

═══ 为什么需要这个文件 ═══

round97 曾判「fund 维零 alpha」，依据是它在验证面板（2029 只全池）上的
IC ≈ 0 且 t 值不显著。round99 复查后发现**这个结论是错的**，根因不是数据缺陷
而是**评估域错配**：

  · 唯一数据源 data/fwd_hist/*.json 每日只存档「当日在 TOP 池内」的
    190~490 只（fwd_pe 是当日快照，过期即失，**不可回溯**）
  · 验证面板 data/val_arrays_h20.npz 却是 2029 只固定域
  → fund 在面板上的覆盖率被稀释到 8.23%（实测），但 fwd_hist **内部覆盖 100%**
  → 在「同域」上重算：IC = +0.0897（t=2.74）；全池：+0.0941（t=2.86）
  → **两口径都显著为正 → round97 的 fund=0 结论被推翻**

═══ 本文件的职责 ═══

把「同域 vs 全池」的覆盖与 IC 差异做成一个**可重复运行的标准检查**，
以后任何维度只要数据源有域限制（只覆盖子集），都必须先跑它，
禁止直接在全池面板上判该维的 alpha。

═══ 诊断项 ═══

  D1 coverage      每个维度在两个域上的覆盖率（分母不同 → 不可直接比 IC）
  D2 domain_ic     同一维度在「同域」与「全池」上的 IC / ICIR / t 对照
  D3 dilution      稀释倍数 = 全池覆盖率 / 同域覆盖率
  D4 verdict       若同域显著而全池不显著 → 判定为「域错配」而非「因子无效」

═══ 已知边界（必须随结论一起披露）═══

  · data/fwd_hist 最早 2026-06-26，共 89 个文件 → fund 只能覆盖 63/219 截面
  · fwd_pe 当日快照不可回溯 → 历史无法补算，只能限定评估域
  · 同域样本约 167~196 只，远小于全池 2029 只 → 单截面 IC 噪声更大，
    但截面数足够（63）时统计功效仍可用

═══ 用法 ═══

  python domain_mismatch_diag.py                 # 诊断全部有域限制的维度
  python domain_mismatch_diag.py --dim score_fund
  python domain_mismatch_diag.py --json out.json
"""
import os
import sys
import json
import glob
import math
import argparse
import time

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import factor_validation_core as F

PANEL = os.path.join(HERE, "data", "panel_round66.json")
NPZ = os.path.join(HERE, "data", "val_arrays_h20.npz")
FWD_HIST = os.path.join(HERE, "data", "fwd_hist")

DIMS = F.DIMS

# 已知「数据源只覆盖子集」的维度 → 对应快照目录
DOMAIN_LIMITED = {
    "score_fund": FWD_HIST,       # fwd_pe 当日快照，只在 TOP 池内存档
}


def _fnum(v):
    """数值判定，涵盖 numpy 标量。

    ⚠️ 2026-10-29 血案：`isinstance(np.float32(x), (int, float))` 是 **False**。
       本脚本首版即因此让「同域 IC 全空但无报错」—— 必须用 numbers.Real。
    """
    import numbers
    if isinstance(v, bool) or v is None:
        return None
    if isinstance(v, numbers.Real):
        try:
            f = float(v)
        except (TypeError, ValueError):
            return None
        return None if (math.isnan(f) or math.isinf(f)) else f
    return None


def _norm_code(c):
    """代码归一：去掉 sh/sz/bj 前缀，保留 6 位数字。"""
    s = str(c or "").strip().lower()
    for p in ("sh", "sz", "bj"):
        if s.startswith(p):
            s = s[len(p):]
    return s.zfill(6) if s.isdigit() else s


def _jsonable(o):
    if isinstance(o, dict):
        return {k: _jsonable(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_jsonable(v) for v in o]
    if isinstance(o, np.bool_):
        return bool(o)
    if isinstance(o, np.integer):
        return int(o)
    if isinstance(o, np.floating):
        return float(o)
    return o


def load_snapshot_map(dim, snap_dir):
    """加载快照目录全部截面 → {date: {code6: value}}，仅保留有值者。"""
    out = {}
    for p in sorted(glob.glob(os.path.join(snap_dir, "*.json"))):
        try:
            d = json.load(open(p, encoding="utf-8"))
        except Exception:
            continue
        dt = d.get("date") or os.path.basename(p).replace(".json", "")
        m = {}
        for r in (d.get("rows") or []):
            v = _fnum(r.get(dim))
            if v is not None:
                m[_norm_code(r.get("code"))] = v
        if m:
            out[str(dt)] = m
    return out


def single_cross_ic(vals, ys):
    """单截面 Spearman（秩相关）。"""
    if len(vals) < 30:
        return None
    rx = F.rank_rows(np.asarray([vals], dtype=float))
    ry = F.rank_rows(np.asarray([ys], dtype=float))
    got = F.row_pearson_ranks(rx, ry, min_n=30)
    return got[0] if got else None


def diag_dim(X, M, Y, dates, codes, RY, dim, snap_map):
    """单个维度的域错配诊断。"""
    if dim not in DIMS:
        return {"error": f"{dim} 不在 DIMS 内"}
    k = DIMS.index(dim)
    code_idx = {c: j for j, c in enumerate(codes)}

    rows = []
    for di, dt in enumerate(dates):
        sm = snap_map.get(dt)
        if not sm:
            continue
        idxs = [code_idx[c] for c in sm if c in code_idx]
        if len(idxs) < 50:
            continue
        vals, ys = [], []
        for j in idxs:
            v = _fnum(sm.get(codes[j]))
            y = _fnum(Y[di, j])
            if v is None or y is None:
                continue
            vals.append(v)
            ys.append(y)
        ic_dom = single_cross_ic(vals, ys)

        # 面板口径：用面板 X/M 里**该维有值**的股票。
        # ⚠️ 注意：这不是"全池 2029 只" —— M 为 True 只表示该维有值。
        #    实测这两个域高度重叠（Jaccard 约 0.85），故两个 IC 几乎相同。
        #    真正要报的是「该维在面板上的覆盖率只有 9%」这件事本身：
        #    剩下 91% 的股票在组合打分时是**缺维剔除**的，fund 对总分
        #    的实际影响力远小于其名义权重。
        ic_pool = None
        pool_js = None
        m = M[di, :, k]
        if int(m.sum()) >= 50:
            xv = X[di, :, k][m].astype(float)
            yv = Y[di, :][m].astype(float)
            ok = ~np.isnan(xv) & ~np.isnan(yv)
            if ok.sum() >= 50:
                ic_pool = single_cross_ic(list(xv[ok]), list(yv[ok]))
            # 域重叠度
            set_snap = set(idxs)
            set_panel = set(np.where(m)[0].tolist())
            inter = len(set_snap & set_panel)
            uni = len(set_snap | set_panel)
            pool_js = (inter / uni) if uni else None

        rows.append({
            "date": dt,
            "domain_n": len(idxs),
            "panel_n": int(m.sum()),
            "n_used": len(vals),
            "pool_cov": float(m.sum()) / len(codes),
            "jaccard": pool_js,
            "ic_dom": ic_dom,
            "ic_panel": ic_pool,
        })

    if not rows:
        return {"error": f"{dim}: 无可比截面", "n_dates": 0}

    st_dom = F.stat([r["ic_dom"] for r in rows if r["ic_dom"] is not None])
    st_pool = F.stat([r["ic_panel"] for r in rows if r["ic_panel"] is not None])

    jss = [r["jaccard"] for r in rows if r["jaccard"] is not None]
    jac_med = round(float(np.median(jss)), 4) if jss else None

    dom_sig = bool(st_dom.get("t") is not None and abs(st_dom["t"]) >= 2.0)
    pool_sig = bool(st_pool.get("t") is not None and abs(st_pool["t"]) >= 2.0)
    # 域错配判定：同域显著、面板不显著，且方向一致
    mismatch = bool(
        dom_sig and not pool_sig
        and st_dom.get("ic") is not None and st_pool.get("ic") is not None
        and np.sign(st_dom["ic"]) == np.sign(st_pool["ic"]))
    both_sig = bool(dom_sig and pool_sig)
    # 两域高度重叠（Jaccard>0.8）时，两口径 IC 本就应接近 →
    # 此时"对照"无信息量，真正的问题是覆盖率不足导致的影响力度偏差
    overlapping = bool(jac_med is not None and jac_med > 0.8)

    if both_sig and overlapping:
        verdict = ("两口径均显著且**评估域高度重叠**（Jaccard≈%.2f）→ "
                   "此对照只能确认「该维在它覆盖到的那 9%% 股票上确实有 alpha」，"
                   "**不能**证明「在全池上同样有效」—— 因为另外 91%% 的股票该维是缺维剔除的。"
                   "真实风险是**影响力度偏差**：名义权重与对被评分股票的实际暴露严重不符。"
                   % (jac_med or 0))
    elif both_sig:
        verdict = ("两口径均显著 → **该维有效**，此前判无效的结论应推翻；"
                   "覆盖率低只影响组合打分的稳定性，不影响该维的单维 alpha")
    elif mismatch:
        verdict = ("域错配：同域显著、面板不显著 → 是**评估域选错**，"
                   "不是因子无效。禁止在面板全域上判该维 alpha")
    elif not dom_sig and not pool_sig:
        verdict = "两口径均不显著 → 该维在本样本内无 alpha（结论成立）"
    else:
        verdict = "同域不显著、面板显著 → 反向错配，需查快照值是否被污染"

    return {
        "dim": dim,
        "source": os.path.relpath(DOMAIN_LIMITED.get(dim, ""), HERE),
        "n_dates": len(rows),
        "date_range": [rows[0]["date"], rows[-1]["date"]],
        "domain_size_median": int(np.median([r["domain_n"] for r in rows])),
        "pool_cov_median": round(float(np.median([r["pool_cov"] for r in rows])), 4),
        "jaccard_median": jac_med,
        "domains_overlapping": overlapping,
        "dilution_x": round(
            (len(codes) / max(float(np.median([r["domain_n"] for r in rows])), 1)), 1),
        "exposure_bias": (
            f"该维名义权重 w 只作用于覆盖率 {np.median([r['pool_cov'] for r in rows]):.1%} 的股票，"
            f"其余股票按「缺维从分母剔除」重归一 → 对被评分股票的实际暴露约为 "
            f"{1.0/max(float(np.median([r['pool_cov'] for r in rows])), 1e-6):.1f}x 名义值"
            if overlapping else None),
        "stat_same_domain": st_dom,
        "stat_full_pool": st_pool,
        "same_domain_significant": dom_sig,
        "full_pool_significant": pool_sig,
        "mismatch_confirmed": mismatch,
        "both_significant": both_sig,
        "verdict": verdict,
        "sample": rows[:3] + rows[-3:] if len(rows) > 6 else rows,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dim", default=None, help="只诊断指定维度（默认全部有域限制的维）")
    ap.add_argument("--json", default=None)
    args = ap.parse_args()

    t0 = time.time()
    dates, X, M, Y, codes = F.load_panel_arrays(PANEL, NPZ)
    dates = [str(d) for d in dates]
    codes = [str(c) for c in codes]
    RY = F.rank_rows(Y)
    print(f"面板 {X.shape}  {dates[0]}~{dates[-1]}")

    dims = [args.dim] if args.dim else list(DOMAIN_LIMITED.keys())
    out = {"meta": {
        "purpose": "P0-1 因子评估域口径诊断（同域 vs 全池）",
        "npz": os.path.relpath(NPZ, HERE),
        "n_dates_total": len(dates),
        "n_codes": len(codes),
        "built_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }, "dims": {}}

    for dim in dims:
        snap_dir = DOMAIN_LIMITED.get(dim)
        if not snap_dir or not os.path.isdir(snap_dir):
            print(f"\n⚠️ {dim}: 无快照目录，跳过")
            continue
        snap_map = load_snapshot_map(dim, snap_dir)
        print(f"\n" + "=" * 78)
        print(f"{dim}  快照截面 {len(snap_map)} 个  ({os.path.relpath(snap_dir, HERE)})")
        print("=" * 78)
        r = diag_dim(X, M, Y, dates, codes, RY, dim, snap_map)
        if r.get("error"):
            print(f"  ⚠️ {r['error']}")
            out["dims"][dim] = r
            continue
        print(f"  可比截面 {r['n_dates']}  ({r['date_range'][0]} ~ {r['date_range'][1]})")
        print(f"  同域规模中位 {r['domain_size_median']} 只  "
              f"面板覆盖率中位 {r['pool_cov_median']:.4f}  "
              f"Jaccard 重叠 {r['jaccard_median']}  "
              f"稀释 {r['dilution_x']}x")
        sd, sp = r["stat_same_domain"], r["stat_full_pool"]
        print(f"\n  {'口径':<16s} {'n':>5s} {'IC均值':>9s} {'ICIR':>8s} {'t':>7s} {'显著':>5s}")
        for nm, st, sig in (("同域(快照)", sd, r["same_domain_significant"]),
                            ("面板(该维有值)", sp, r["full_pool_significant"])):
            if st and st.get("n"):
                print(f"  {nm:<16s} {st['n']:>5d} "
                      f"{(st['ic'] if st['ic'] is not None else float('nan')):>9.4f} "
                      f"{(st['icir'] if st['icir'] is not None else float('nan')):>8.4f} "
                      f"{(st['t'] if st['t'] is not None else float('nan')):>7.2f} "
                      f"{'Y' if sig else 'N':>5s}")
        if r.get("exposure_bias"):
            print(f"\n  ⚠️ 影响力度偏差: {r['exposure_bias']}")
        print(f"\n  ⇒ {r['verdict']}")
        out["dims"][dim] = r

    out["meta"]["elapsed_s"] = round(time.time() - t0, 1)
    p = args.json or os.path.join(HERE, "domain_mismatch_diag.json")
    json.dump(_jsonable(out), open(p, "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    print(f"\n→ {p}  耗时 {out['meta']['elapsed_s']}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
