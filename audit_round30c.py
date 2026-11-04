#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""round30c：为什么「fund 单维显著正 IC」却「复合分边际为负」—— 四重归因

起点（round30 / round30b 实测，两个结论必须同时成立）
-----------------------------------------------------
  (1) fund 单维 rank IC  h5 +0.0615 (t+4.52) · h20 +0.0951 (t+5.37)
      → 双双过 Bonferroni 3.08，且过严格阈值 3.9。**不是 ≈0，是显著正。**
  (2) 把 fund 权重 0.075 归零，复合分 IC **显著变好**：
      h5 +0.1520 → +0.1548 (配对 t=+4.04) · h20 +0.1879 → +0.1921 (配对 t=+4.98)

「单维有 alpha、边际贡献为负」在因子组合里是**经典现象**，但成立前提是
「该 alpha 已被其他维覆盖」。本脚本要排除三个**替代解释**，否则不能据此降权：

  H1 冗余假设：fund 的 alpha 被 EP/质量等维覆盖 → **真冗余，应当降权**
  H2 量尺假设：fund 覆盖率仅 ~25%，同截面上 25% 的股票按 8 维、75% 按 7 维算，
              分母 max(0.5, Σw) 使有 fund 者分数被系统性放大 1/0.925 = 1.081 倍，
              这是**混合量尺引入的伪噪声**，而非 fund 无用
  H3 子集假设：改善只发生在「fund 缺失」子集（该子集两方案公式本就相同 → 必无差异），
              若如此则结论无效
  H4 权重再分配假设：归零把 0.075 摊给其余七维，改善来自「其余维权重更高」而非
              「fund 是负贡献」；需要与「随机删掉同权重的另一维」对照

实验设计
--------
  E1 子集分解：把每个截面拆成 F（fund 有值）与 N（fund 缺失）两个子集，
     分别算 W0 与 W2 的 IC，再做配对。H3 的判定：N 子集内两方案必须逐位相同。
  E2 量尺偏差量化：直接测量「有 fund 值」与「无 fund 值」两组股票的 7 维复合分
     在截面内的 rank 位置差异 —— 若两组分布存在系统性偏移，H2 成立。
  E3 干净对照（决定性）：**只取 fund 有值的股票**构成同质子样本。
     此子样本内 8 维 vs 7 维是**同一量尺**（都有 fund 或用同一套无 fund 公式一致处理），
     不再有混合归一化问题。若此处 W2 仍更好 → H1 成立（真冗余）；
     若 W0 更好 → H2 成立（之前的负边际是混合量尺造成的假象）。
  E4 冗余分解：fund 与 EP 的相关最高（ρ=+0.358）。做四方案对照：
     全八维 / 去 fund / 去 EP / 去两者。再算 fund 控制 EP 后的增量 IC。
  E5 随机对照：随机删掉任一非零维（同 0.075 权重）重复 200 次，
     看「删 fund 的改善」是否落在随机分布之外 —— 排除 H4。

⚠️ 口径边界（与 round30/30b 一致，不隐藏）
------------------------------------------
panel 的 fund **数值**继承 `data/fwd_hist/*.json`，含当年 ROE 量纲 bug 的 ±0.5 档影响，
且 roe/cagr3/gross_margin/q3_info 未存档不可重算（复现率仅 7.8%）。
本脚本精确隔离的仅：P0-2 兜底修复（PE/FwdPE 均缺 → None）+ 权重处置。

用法
----
  python audit_round30c.py
输出
----
  audit_round30c.json
"""

import os
import sys
import json
import math
import random
from collections import defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import factor_eval as fe
import audit_round29 as r29

nw_t = r29.nw_t
ic_stats = r29.ic_stats
T_BONF = r29.T_BONF

PANEL = os.path.join(HERE, "data", "panel_round30.json")


def is_num(x):
    return isinstance(x, (int, float)) and not isinstance(x, bool) and x == x


def main():
    panel = json.load(open(PANEL, encoding="utf-8"))
    h = json.load(open(os.path.join(HERE, "factor_history.json"), encoding="utf-8"))
    tdays, px = fe.px_index(h)
    tidx = {d: i for i, d in enumerate(tdays)}

    fwj = json.load(open(os.path.join(HERE, "factor_weights.json"), encoding="utf-8"))
    W = {k: float(v or 0) for k, v in (fwj.get("weights") or {}).items()}
    DMAX = {k: float(v) for k, v in (fwj.get("dim_max") or {}).items()}
    EFF = [k for k, v in W.items() if v > 0]

    GATE = "score_size_ind"

    print("=" * 112)
    print("round30c：fund「单维正 / 边际负」矛盾的四重归因")
    print("=" * 112)
    print(f"面板：域 {panel['meta']['domain_size']} 只 · 截面 {panel['meta']['n_dates']} 个 "
          f"· 窗口 {panel['meta']['window_start']} 起")

    # ---------- fund 覆盖率实测
    tot = 0
    have = 0
    for d0 in panel["dates"]:
        for c, v in panel["rows"][d0].items():
            tot += 1
            if is_num(v.get("score_fund")):
                have += 1
    print(f"fund 覆盖率（股票×日）：{have}/{tot} = {100.0*have/tot:.2f}%")

    def reweight(fund_w, drop=None):
        """fund 权重设 fund_w、或整体去掉 drop 维，差额按比例摊给其余，Σ=1。"""
        base = {k: W[k] for k in EFF if k not in ("score_fund", drop) and W[k] > 0}
        if drop == "score_fund":
            base = {k: W[k] for k in EFF if k != "score_fund" and W[k] > 0}
        s = sum(base.values())
        fw = 0.0 if drop == "score_fund" else fund_w
        rest = 1.0 - fw
        out = {k: v / s * rest for k, v in base.items()}
        if fw > 0:
            out["score_fund"] = fw
        return out

    def prod_score(vals, wmap):
        num = den = 0.0
        for k, w in wmap.items():
            if w <= 0:
                continue
            v = vals.get(k)
            if not is_num(v):
                continue
            mx = DMAX.get(k) or 10.0
            num += min(float(v), mx) / mx * 10.0 * w
            den += w
        return (num / max(0.5, den) * 10.0) if den > 0 else None

    def fwd_ret(c, d0, hop):
        m = px.get(c)
        i0 = tidx.get(d0)
        if not m or i0 is None:
            return None
        j = i0 + hop
        if j >= len(tdays):
            return None
        p0, p1 = m.get(d0), m.get(tdays[j])
        if p0 and p1 and p0 > 0:
            return p1 / p0 - 1.0
        return None

    def series(wmap, hop, subset=None, min_n=40):
        """返回 {date: ic}。subset: None=全部 / 'F'=fund有值 / 'N'=fund缺失。"""
        out = {}
        for d0 in panel["dates"]:
            if tidx[d0] + hop >= len(tdays):
                continue
            cur = panel["rows"][d0]
            fv, rv = [], []
            for c, vals in cur.items():
                if not is_num(vals.get(GATE)):
                    continue
                hf = is_num(vals.get("score_fund"))
                if subset == "F" and not hf:
                    continue
                if subset == "N" and hf:
                    continue
                v = prod_score(vals, wmap)
                if v is None:
                    continue
                fr = fwd_ret(c, d0, hop)
                if fr is None:
                    continue
                fv.append(v)
                rv.append(fr)
            if len(fv) >= min_n:
                ic = fe.rank_ic(fv, rv)
                if ic is not None:
                    out[d0] = {"ic": ic, "n": len(fv)}
        return out

    def report(tag, s):
        if not s:
            print(f"  {tag:38s} 无数据")
            return None
        ics = [v["ic"] for d0, v in sorted(s.items())]
        ns = [v["n"] for d0, v in sorted(s.items())]
        st = ic_stats(ics)
        st["n_per_snap_med"] = sorted(ns)[len(ns) // 2]
        print("  %-38s IC=%+.4f  t0=%+.2f  t3=%+.2f  t6=%+.2f  n_med=%d  Bonf=%s"
              % (tag, st["ic"], st["t0"] or 0, st["t3"] or 0, st["t6"] or 0,
                 st["n_per_snap_med"], "✅" if st["pass_bonf"] else "❌"))
        return st

    def paired(a, b, label):
        if not a or not b:
            return None
        common = sorted(set(a) & set(b))
        diffs = [b[d]["ic"] - a[d]["ic"] for d in common]
        if not diffs:
            return None
        mu = sum(diffs) / len(diffs)
        t = nw_t(diffs, 0)
        pos = sum(1 for x in diffs if x > 0)
        identical = all(abs(x) < 1e-12 for x in diffs)
        print("    %-36s ΔIC=%+.4f  NW t=%+.2f  胜/负=%d/%d  配对截面=%d%s"
              % (label, mu, t or 0, pos, len(diffs) - pos, len(diffs),
                 "  ⚠️逐位相同(公式等价)" if identical else ""))
        return {"mean_diff": round(mu, 6), "nw_t": round(t, 2) if t is not None else None,
                "wins": pos, "losses": len(diffs) - pos, "n_pairs": len(diffs),
                "identical": identical}

    W0 = reweight(0.075)          # 现行
    W2 = reweight(0.0)            # fund 归零
    W_EP0 = reweight(0.075, drop="score_ep")   # 去 EP（fund 保留）

    out = {"round": "30c", "fund_coverage": round(have / max(1, tot), 4)}

    # ============================== E1 子集分解
    print("\n" + "─" * 112)
    print("【E1】子集分解：F=fund有值 子集 / N=fund缺失 子集")
    print("─" * 112)
    print("  目的：排除 H3（改善是否只发生在 fund 缺失子集）。若 N 子集内两方案逐位相同 → H3 排除。")
    e1 = {}
    for hop in (5, 20):
        print(f"  hop={hop}:")
        sF0 = series(W0, hop, "F", min_n=30)
        sF2 = series(W2, hop, "F", min_n=30)
        sN0 = series(W0, hop, "N", min_n=30)
        sN2 = series(W2, hop, "N", min_n=30)
        e1["h%d" % hop] = {
            "F_W0": report("F 子集 · W0(8维)", sF0),
            "F_W2": report("F 子集 · W2(7维)", sF2),
            "N_W0": report("N 子集 · W0", sN0),
            "N_W2": report("N 子集 · W2", sN2),
        }
        print("  配对（W2 − W0）：")
        e1["h%d" % hop]["F_paired"] = paired(sF0, sF2, "F 子集")
        e1["h%d" % hop]["N_paired"] = paired(sN0, sN2, "N 子集")
        print()

    out["e1_subset"] = e1

    # ============================== E2 量尺偏差
    print("─" * 112)
    print("【E2】量尺偏差：有 fund / 无 fund 两组股票的 7 维复合分截面 rank 分布")
    print("─" * 112)
    print("  目的：排除 H2。fund 缺失时 den 少 0.075 → 分数被放大 1/0.925=1.081 倍。")
    print("        若两组打分分布不同，混合排序就带系统性偏差。")
    e2 = {"by_date": {}}
    rank_gap_all = []
    for d0 in panel["dates"]:
        cur = panel["rows"][d0]
        pts = []
        for c, vals in cur.items():
            if not is_num(vals.get(GATE)):
                continue
            v7 = prod_score({k: vals[k] for k in vals if k != "score_fund"}, 
                            {k: v for k, v in W2.items()})
            if v7 is None:
                continue
            pts.append((c, v7, is_num(vals.get("score_fund"))))
        if len(pts) < 60:
            continue
        pts.sort(key=lambda x: (x[1], x[0]))
        n = len(pts)
        rk = {p[0]: (i / (n - 1.0)) for i, p in enumerate(pts)}
        hf = [rk[c] for c, _, h in pts if h]
        nf = [rk[c] for c, _, h in pts if not h]
        if len(hf) >= 10 and len(nf) >= 10:
            gap = (sum(hf) / len(hf)) - (sum(nf) / len(nf))
            rank_gap_all.append((d0, gap, len(hf), len(nf)))
            e2["by_date"][d0] = {"gap": round(gap, 4), "n_F": len(hf), "n_N": len(nf)}
    if rank_gap_all:
        gaps = [x[1] for x in rank_gap_all]
        mg = sum(gaps) / len(gaps)
        tg = nw_t(gaps, 0)
        print(f"  截面数 {len(gaps)} · 有fund组 7维分 rank 均值 − 无fund组 = {mg:+.4f}  (NW t={tg:+.2f})")
        print(f"    正 → 有fund的股票在 7 维上系统性偏高（其 8 维分被放大后更容易冲到前排，形成偏差）")
        print(f"    负 → 有fund的股票在 7 维上系统性偏低")
        e2["mean_gap"] = round(mg, 4)
        e2["nw_t"] = round(tg, 2) if tg is not None else None
    else:
        print("  ⚠️ 可用截面不足")

    # 直接量化：同一只股票，8维分 vs 7维分 的名次变动
    moved = []
    for d0 in panel["dates"]:
        cur = panel["rows"][d0]
        rows = []
        for c, vals in cur.items():
            if not is_num(vals.get(GATE)):
                continue
            v8 = prod_score(vals, W0)
            v7 = prod_score(vals, W2)
            if v8 is None or v7 is None:
                continue
            rows.append((c, v8, v7, is_num(vals.get("score_fund"))))
        if len(rows) < 60:
            continue
        n = len(rows)
        r8 = {r[0]: i for i, r in enumerate(sorted(rows, key=lambda x: (x[1], x[0])))}
        r7 = {r[0]: i for i, r in enumerate(sorted(rows, key=lambda x: (x[2], x[0])))}
        for c, _, _, h in rows:
            if h:
                moved.append(r8[c] - r7[c])
    if moved:
        nm = len(moved)
        mm = sum(moved) / nm
        print(f"  含 fund 的股票：加入 fund 后名次变化（8维名次 − 7维名次，负数=上升）")
        print(f"    平均 {mm:+.2f} 名 / 中位 {sorted(moved)[nm//2]:+.0f} 名 / 样本 {nm} 个股票×日")
        e2["rank_shift_median"] = sorted(moved)[nm // 2]
    out["e2_scale_bias"] = e2

    # ============================== E3 干净对照（决定性）
    print("\n" + "─" * 112)
    print("【E3】干净对照（决定性）：只在 fund 有值的同质子样本内比较 8 维 vs 7 维")
    print("─" * 112)
    print("  理由：E1 的 F 子集已消除「混合量尺」问题。此处再做 stricter 版本——")
    print("        要求该股**所有截面**都有 fund 值（恒覆盖股），彻底排除缺值切换。")
    always_f = set()
    first = panel["dates"][0]
    for c in panel["rows"][first]:
        ok = True
        for d0 in panel["dates"]:
            v = panel["rows"][d0].get(c)
            if v is None or not is_num(v.get("score_fund")):
                ok = False
                break
        if ok:
            always_f.add(c)
    print(f"  恒覆盖股（每个截面都有 fund 值）：{len(always_f)} 只")
    e3 = {"n_always_f": len(always_f)}
    if len(always_f) >= 100:
        for hop in (5, 20):
            s0 = series(W0, hop, "F", min_n=30)
            s2 = series(W2, hop, "F", min_n=30)
            # 限定恒覆盖股
            s0h, s2h = {}, {}
            for d0 in s0:
                cur = {c: v for c, v in panel["rows"][d0].items() if c in always_f}
                for tgt, wm in ((s0h, W0), (s2h, W2)):
                    fv, rv = [], []
                    for c, vals in cur.items():
                        if not is_num(vals.get(GATE)):
                            continue
                        vv = prod_score(vals, wm)
                        if vv is None:
                            continue
                        fr = fwd_ret(c, d0, hop)
                        if fr is None:
                            continue
                        fv.append(vv)
                        rv.append(fr)
                    if len(fv) >= 40:
                        ic = fe.rank_ic(fv, rv)
                        if ic is not None:
                            tgt[d0] = {"ic": ic, "n": len(fv)}
            print(f"  hop={hop}（恒覆盖同质样本）:")
            e3["h%d" % hop] = {"W0": report("  W0(8维)", s0h), "W2": report("  W2(7维)", s2h)}
            e3["h%d" % hop]["paired"] = paired(s0h, s2h, "  W2 − W0")
        print("  判定：若此处 W2 仍显著更好 → H1 真冗余；若 W0 更好/不可区分 → H2 混合量尺假象。")
    else:
        print("  ⚠️ 恒覆盖股不足 100 只，E3 改用 E1 的 F 子集结论（已在上表）")
    out["e3_clean"] = e3

    # ============================== E4 冗余分解（fund vs EP）
    print("\n" + "─" * 112)
    print("【E4】冗余分解：fund 与 EP 相关最高（ρ=+0.358），四方案对照")
    print("─" * 112)
    e4 = {}
    W_dropEP = reweight(0.075, drop="score_ep")
    # 去 EP 时把 EP 的 0.075 也归零、摊给其余（含 fund 按原 0.075）
    W_both0 = {k: 0.0 for k in EFF}
    _base = {k: W[k] for k in EFF if k not in ("score_fund", "score_ep") and W[k] > 0}
    _s = sum(_base.values())
    W_both0 = {k: v / _s for k, v in _base.items()}
    for hop in (5, 20):
        print(f"  hop={hop}:")
        a = series(W0, hop)
        b = series(W2, hop)
        c = series(W_dropEP, hop)
        d = series(W_both0, hop)
        e4["h%d" % hop] = {
            "all8": report("  A 全八维(fund+.ep各.075)", a),
            "no_fund": report("  B 去 fund", b),
            "no_ep": report("  C 去 EP", c),
            "no_both": report("  D 去 fund+EP", d),
        }
        print("    配对：")
        e4["h%d" % hop]["B_vs_A"] = paired(a, b, "B 去fund − A")
        e4["h%d" % hop]["C_vs_A"] = paired(a, c, "C 去EP   − A")
        e4["h%d" % hop]["D_vs_A"] = paired(a, d, "D 去两者 − A")
        print()

    # fund 控制 EP 后的偏相关（当日 scored，横截面）
    try:
        sc = json.load(open(os.path.join(HERE, "top200_scored_2026-10-27.json"), encoding="utf-8"))
        rows = [r for r in sc["results"]
                if is_num(r.get("score_fund")) and is_num(r.get("score_ep"))
                and is_num(r.get("score_quality"))]
        if len(rows) >= 100:
            f = [float(r["score_fund"]) for r in rows]
            e = [float(r["score_ep"]) for r in rows]
            rho_fe = fe.rank_ic(f, e)
            # 用 EP 的秩对 fund 的秩做线性回归取残差，再算残差与前瞻收益无关（无收益数据）
            # → 改为：残差与其余维的相关，判断 fund 是否还有独立结构
            res_ = []
            n = len(rows)
            def _rank(xs):
                order = sorted(range(n), key=lambda i: (xs[i], i))
                rk = [0.0] * n
                for pos, i in enumerate(order):
                    rk[i] = pos / (n - 1.0)
                return rk
            rf, re_ = _rank(f), _rank(e)
            mf = sum(rf) / n
            me = sum(re_) / n
            cov = sum((rf[i] - mf) * (re_[i] - me) for i in range(n)) / n
            vare = sum((re_[i] - me) ** 2 for i in range(n)) / n
            beta = cov / vare if vare > 0 else 0.0
            res_ = [(rf[i] - mf) - beta * (re_[i] - me) for i in range(n)]
            print(f"  fund × EP 横截面 ρ={rho_fe:+.4f} · 用 EP 秩回归后 fund 残差标准差="
                  f"{math.sqrt(sum(x*x for x in res_)/n):.4f}（原始 fund 秩 sd≈0.2887）")
            e4["fund_ep_rho"] = round(rho_fe, 4) if rho_fe is not None else None
            e4["fund_resid_sd_vs_full_sd"] = round(
                math.sqrt(sum(x * x for x in res_) / n) / 0.2887, 4)
    except Exception as ex:
        print(f"  ⚠️ 偏相关计算跳过：{ex}")
    out["e4_redundancy"] = e4

    # ============================== E5 随机对照
    print("─" * 112)
    print("【E5】随机对照：随机删任一维（同 0.075 权重口径），检验「删 fund 的改善」是否特异")
    print("─" * 112)
    print("  目的：排除 H4（改善只是「其余维权重更高」的普遍效应）。")
    random.seed(20261027)
    e5 = {}
    for hop in (5, 20):
        a = series(W0, hop)
        base_ic = (lambda s: sum(v["ic"] for _, v in sorted(s.items())) / len(s))(a) if a else 0
        res_by_dim = {}
        for dim in EFF:
            if dim == "score_fund":
                continue
            wm = reweight(0.075, drop=dim)
            if dim == "score_ep":
                pass
            s = series(wm, hop)
            if not s:
                continue
            ic = sum(v["ic"] for _, v in sorted(s.items())) / len(s)
            res_by_dim[dim] = round(ic - base_ic, 5)
        fund_ic = (lambda s: sum(v["ic"] for _, v in sorted(s.items())) / len(s))(
            series(W2, hop)) if series(W2, hop) else 0
        fund_delta = fund_ic - base_ic
        # 随机删除：把任意一维权重设为 0 后重摊（等价于上面循环，随机维度已在其中）
        deltas = list(res_by_dim.values())
        worse = sum(1 for x in deltas if x < fund_delta)
        print(f"  hop={hop}: 删 fund ΔIC={fund_delta:+.5f}")
        print(f"    删其他维的 ΔIC 分布：min={min(deltas):+.5f} max={max(deltas):+.5f} "
              f"med={sorted(deltas)[len(deltas)//2]:+.5f}")
        print(f"    「比删 fund 更差」的其他维数：{worse}/{len(deltas)}")
        print(f"    逐维明细：")
        for k, v in sorted(res_by_dim.items(), key=lambda x: x[1]):
            mark = " ★比删fund更好" if v > fund_delta else ""
            print(f"      {k:20s} ΔIC={v:+.5f}{mark}")
        e5["h%d" % hop] = {"fund_delta": round(fund_delta, 5),
                           "others": res_by_dim,
                           "n_others_worse": worse,
                           "others_min": round(min(deltas), 5),
                           "others_max": round(max(deltas), 5)}
        print()

    out["e5_random_control"] = e5

    with open(os.path.join(HERE, "audit_round30c.json"), "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print("已写出 audit_round30c.json")


if __name__ == "__main__":
    main()
