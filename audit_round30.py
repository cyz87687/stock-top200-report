#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""round30：fund 维兜底修复后的 IC 重测（老大裁决第二步）

背景（老大裁决原文）
--------------------
「先修兜底再降权（推荐）：①无估值时 fund 返回 None（从分母剔除，不伪造分数）；
  ②修完重测 IC，若仍 ≈0 再把 0.075 降下来。分两步、每步可验证」

本脚本只回答一个问题：**兜底修复后，fund 维的单维 IC 是否仍 ≈0？**

为什么不能「直接拿修复后的代码重算历史 fund 分」
------------------------------------------------
`data/fwd_hist/*.json` 是当年从 git 历史恢复的精简 scored，只保留了
`pe / fwd_pe / growth / pe_ladder / score_fund / score_tech` 等字段，
**没有保存 `roe / roe_unit / gross_margin / cagr3 / q3_info / fwd_np / hist_loss_free`**
——而这 7 个参数全部参与 score_fund 打分。

实测（2026-10-23 截面 490 只）用现存字段重算 score_fund：same=38 / diff=452
→ 复现率 7.8%。**即「用现在的代码重算历史分」不可行，会引入比要修的问题更大的偏误。**

精确剥离法（本脚本采用）
------------------------
兜底修复的作用域是**精确可界定的**：
  · 修复前：`effective_pe is None or <= 0` → 用增速伪造 0/1.2/2/3/3.4 分；
  · 修复后：同一条件 → 返回 None，该股该日退出本维。
对 `PE 与 FwdPE 均有值` 的样本，兜底分支**根本没有进入**，分数逐字不变。
→ 因此「剔除 PE/FwdPE 全缺样本后的 fund IC」= 修复后 fund IC 的**精确等价**（就兜底这一步而言）。

四个口径（隔离实验）
--------------------
  X_old     : 全样本，沿用旧 score_fund（含伪造分）      ← round29 口径，基线
  Y_nofake  : 剔除 PE/FwdPE 均缺样本                    ← **修复后的等价口径（主结论）**
  Z_fillmed : 伪造分替换为当期池内中位数（对照：填充是否比剔除好）
  W_dropfake_low : 只剔除伪分中 < 1 的部分（对照：污染是否只来自低分段）

⚠️ 残余不可复现项（如实披露，不隐藏）
--------------------------------------
1. ROE 量纲修复（P0-1）改动的是 ±0.5 分的加减档，fwd_hist 未存 roe/roe_unit
   → 历史面板无法复现该效应。本脚本主结论**只覆盖兜底修复（P0-2）**，
     ROE 修复的效应改由当日 scored 文件（修复前后各一版）做横截面验证。
2. `cagr3 / gross_margin / q3_info / fwd_np / hist_loss_free` 缺失
   → 重算路径不可用（见上），本脚本因此**不做**「用新代码重算历史」这条线。

统计口径与 round29 完全一致（NW lag 0/3/6、Bonferroni t=3.08、分子期双正）。

用法
----
  python audit_round30.py
输出
----
  audit_round30.json
"""

import os
import sys
import json
import math
import glob
from collections import defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import factor_eval as fe

T_BONF = 3.08          # 项目中沿用的 Bonferroni 阈值
T_BONF_STRICT = 3.9    # 累计 304+ 次多重检验后的严格阈值（round25 记载）


# ============================================================ 统计工具（与 round29 逐字同口径）

def nw_t(xs, lag):
    n = len(xs)
    if n < 5:
        return None
    mu = sum(xs) / n
    d = [x - mu for x in xs]
    var = sum(x * x for x in d) / n
    for L in range(1, min(lag, n - 1) + 1):
        gl = sum(d[i] * d[i - L] for i in range(L, n)) / n
        var += 2 * (1 - L / (lag + 1)) * gl
    se = math.sqrt(var / n) if var > 0 else None
    return (mu / se) if se else None


def ic_stats(ics):
    n = len(ics)
    if n == 0:
        return None
    mu = sum(ics) / n
    sd = math.sqrt(sum((x - mu) ** 2 for x in ics) / n) if n > 1 else None
    half = n // 2
    fh = sum(ics[:half]) / half if half else None
    sh = sum(ics[half:]) / (n - half) if (n - half) else None
    t0 = nw_t(ics, 0)
    return {
        "n": n,
        "ic": round(mu, 4),
        "std": round(sd, 4) if sd else None,
        "icir": round(mu / sd, 3) if (sd and sd > 0) else None,
        "t0": round(t0, 2) if t0 is not None else None,
        "t3": round(nw_t(ics, 3), 2) if nw_t(ics, 3) is not None else None,
        "t6": round(nw_t(ics, 6), 2) if nw_t(ics, 6) is not None else None,
        "ic_pos_share": round(sum(1 for x in ics if x > 0) / n, 3),
        "pass_bonf": bool(t0 is not None and abs(t0) >= T_BONF),
        "pass_bonf_strict": bool(t0 is not None and abs(t0) >= T_BONF_STRICT),
        "first_half_ic": round(fh, 4) if fh is not None else None,
        "second_half_ic": round(sh, 4) if sh is not None else None,
        "both_halves_positive": bool(fh is not None and sh is not None and fh > 0 and sh > 0),
    }


def is_pos_num(x):
    return isinstance(x, (int, float)) and not isinstance(x, bool) and x == x and x > 0


# ============================================================ 主流程

def main():
    h = json.load(open(os.path.join(HERE, "factor_history.json"), encoding="utf-8"))
    tdays, px = fe.px_index(h)
    tidx = {d: i for i, d in enumerate(tdays)}

    # ---------- 前瞻历史（fund 的唯一来源）
    fwd = {}
    for fp in glob.glob(os.path.join(HERE, "data", "fwd_hist", "*.json")):
        d = json.load(open(fp, encoding="utf-8"))
        fwd[d["date"]] = {(r.get("code") or "")[-6:]: r for r in d["rows"]}

    # ---------- 历史池成分（域锚，与 round29 一致的做法）
    seen = set()
    for s in h["snapshots"]:
        for r in (s.get("rows") or []):
            c = (r.get("code") or "")[-6:]
            if c:
                seen.add(c)

    dom = sorted(seen & set(px))

    print("=" * 108)
    print("round30：fund 维兜底修复后的 IC 重测")
    print("=" * 108)
    print(f"交易日 {len(tdays)} 天（{tdays[0]} ~ {tdays[-1]}）· 价格 {len(px)} 只")
    print(f"前瞻历史 {len(fwd)} 个截面 · 历史池成分 {len(seen)} 只 · 域 {len(dom)} 只")

    # ---------- 伪分判定
    _audit = {
        "total_stock_days_with_fund": 0,
        "fake_no_pe": 0,
        "fake_dist": defaultdict(int),
        "real_zero_kept": 0,
        "has_pe_or_fwdpe": 0,
    }
    for d0, rows in fwd.items():
        for c, r in rows.items():
            sf = r.get("score_fund")
            if sf is None:
                continue
            _audit["total_stock_days_with_fund"] += 1
            if is_pos_num(r.get("pe")) or is_pos_num(r.get("fwd_pe")):
                _audit["has_pe_or_fwdpe"] += 1
                if sf == 0:
                    _audit["real_zero_kept"] += 1
            else:
                _audit["fake_no_pe"] += 1
                _audit["fake_dist"][round(float(sf), 2)] += 1

    _fake_pct = 100.0 * _audit["fake_no_pe"] / max(1, _audit["total_stock_days_with_fund"])
    print(f"\n【A】伪分规模（全 86 个截面合并统计）")
    print(f"  有 score_fund 的 股票×日 : {_audit['total_stock_days_with_fund']}")
    print(f"  其中 PE 与 FwdPE 均有值  : {_audit['has_pe_or_fwdpe']}")
    print(f"  其中 PE 与 FwdPE 均缺    : {_audit['fake_no_pe']}  （{_fake_pct:.1f}%，修复后 → None）")
    print(f"  真 0 分（有 PE 但极差）  : {_audit['real_zero_kept']}  ← 修复后**保留**，不可一起清零")
    print(f"  伪分取值分布 top12：")
    for v, c in sorted(_audit["fake_dist"].items(), key=lambda x: -x[1])[:12]:
        print(f"     {v:>5.2f} → {c} 条")

    # ---------- 前瞻收益
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

    # ---------- 四个口径的值提取器
    def _fake(r):
        """该行是否为「无估值伪分」。"""
        return not (is_pos_num(r.get("pe")) or is_pos_num(r.get("fwd_pe")))

    def build_values(d0, rows, mode):
        """返回 {code: value}。mode 见文件头四口径说明。"""
        out = {}
        if mode == "Z_fillmed":
            # 先算「有效样本」的中位数（作为填充值）
            valid = [float(r["score_fund"]) for r in rows.values()
                     if r.get("score_fund") is not None and not _fake(r)]
            med = sorted(valid)[len(valid) // 2] if valid else None
        for c, r in rows.items():
            sf = r.get("score_fund")
            if sf is None:
                continue
            fake = _fake(r)
            if mode == "X_old":
                out[c] = float(sf)
            elif mode == "Y_nofake":
                if fake:
                    continue
                out[c] = float(sf)
            elif mode == "Z_fillmed":
                out[c] = (med if (fake and med is not None) else float(sf))
            elif mode == "W_dropfake_low":
                if fake and float(sf) < 1.0:
                    continue
                out[c] = float(sf)
        return out

    MODES = ["X_old", "Y_nofake", "Z_fillmed", "W_dropfake_low"]
    MODE_LABEL = {
        "X_old": "X_旧口径(含伪造分)·round29基线",
        "Y_nofake": "Y_修复后(剔除无估值)★主结论",
        "Z_fillmed": "Z_伪分填池内中位(对照:填充)",
        "W_dropfake_low": "W_只剔伪分<1(对照:部分剔)",
    }

    # ---------- 逐 hop 算 IC
    res = {}
    print(f"\n【B】fund 单维 rank IC（四个口径 × h5/h20）")
    print("─" * 108)
    print("  %-34s %-4s %8s %8s %7s %7s %7s %7s %8s %s"
          % ("口径", "hop", "IC", "ICIR", "t0", "t3", "t6", "IC>0", "n_med", "Bonf(3.08/3.9)"))
    for hop in (1, 5, 10, 20):
        if hop not in (5, 20):
            continue
        for mode in MODES:
            ics, ns = [], []
            for d0 in sorted(fwd):
                if tidx.get(d0) is None:
                    continue
                if tidx[d0] + hop >= len(tdays):
                    continue
                rows = fwd[d0]
                vals = build_values(d0, rows, mode)
                fv, rv = [], []
                for c, v in vals.items():
                    if c not in px:
                        continue
                    fr = fwd_ret(c, d0, hop)
                    if fr is None:
                        continue
                    fv.append(v)
                    rv.append(fr)
                if len(fv) >= 40:
                    ic = fe.rank_ic(fv, rv)
                    if ic is not None:
                        ics.append(ic)
                        ns.append(len(fv))
            st = ic_stats(ics)
            if st and ns:
                ns_sorted = sorted(ns)
                st["n_per_snap_min"] = ns_sorted[0]
                st["n_per_snap_med"] = ns_sorted[len(ns_sorted) // 2]
                st["n_per_snap_max"] = ns_sorted[-1]
                st["n_per_snap_mean"] = round(sum(ns) / len(ns), 1)
                st["n_stock_dates"] = sum(ns)
            res["%s|h%d" % (mode, hop)] = st
            if st:
                flag = ("✅✅" if st["pass_bonf_strict"] else ("✅" if st["pass_bonf"] else "❌"))
                print("  %-34s h%-3d %+8.4f %+8.3f %+7.2f %+7.2f %+7.2f %6.1f%% %8d %s"
                      % (MODE_LABEL[mode], hop, st["ic"], st["icir"] or 0, st["t0"] or 0,
                         st["t3"] or 0, st["t6"] or 0, st["ic_pos_share"] * 100,
                         st["n_per_snap_med"], flag))
            else:
                print("  %-34s h%-3d  无数据" % (MODE_LABEL[mode], hop))
        print()

    # ---------- 配对检验：修复是否显著改变了 IC
    print("【C】配对检验：Y_修复后 vs X_旧口径（逐截面配对，判断是否系统性改变）")
    print("─" * 108)
    pair = {}
    for hop in (5, 20):
        # 重算逐截面 IC 序列（成对）
        seq = {}
        for mode in ("X_old", "Y_nofake"):
            arr = {}
            for d0 in sorted(fwd):
                if tidx.get(d0) is None or tidx[d0] + hop >= len(tdays):
                    continue
                vals = build_values(d0, fwd[d0], mode)
                fv, rv = [], []
                for c, v in vals.items():
                    if c not in px:
                        continue
                    fr = fwd_ret(c, d0, hop)
                    if fr is None:
                        continue
                    fv.append(v)
                    rv.append(fr)
                if len(fv) >= 40:
                    ic = fe.rank_ic(fv, rv)
                    if ic is not None:
                        arr[d0] = ic
            seq[mode] = arr
        common = sorted(set(seq["X_old"]) & set(seq["Y_nofake"]))
        diffs = [seq["Y_nofake"][d] - seq["X_old"][d] for d in common]
        if diffs:
            mu = sum(diffs) / len(diffs)
            t = nw_t(diffs, 0)
            pos = sum(1 for x in diffs if x > 0)
            pair["h%d" % hop] = {
                "n_pairs": len(diffs), "mean_diff": round(mu, 4),
                "nw_t": round(t, 2) if t is not None else None,
                "y_wins": pos, "x_wins": len(diffs) - pos,
                "pass_bonf": bool(t is not None and abs(t) >= T_BONF),
            }
            print("  h%-3d  配对截面 %d 个 · 均值差 %+.4f · NW t=%+.2f · "
                  "修复后更优 %d 次 / 更差 %d 次 · Bonf=%s"
                  % (hop, len(diffs), mu, t or 0, pos, len(diffs) - pos,
                     "✅" if (t is not None and abs(t) >= T_BONF) else "❌"))

    # ---------- fund 的真实独立信息量：与其它维的相关（复用当日 scored 文件）
    print("\n【D】fund 与其余七维的截面秩相关（2026-10-27 scored，修复后实证）")
    print("─" * 108)
    corr = {}
    try:
        sc = json.load(open(os.path.join(HERE, "top200_scored_2026-10-27.json"), encoding="utf-8"))
        rows = sc["results"]
        dims = ["score_fund", "score_quality", "score_tech_neg", "score_bias60",
                "score_rsi_rev", "score_lowvol20", "score_ep", "score_size_ind"]
        pairs = {}
        for k in dims:
            if k == "score_fund":
                continue
            fv, rv = [], []
            for r in rows:
                a, b = r.get("score_fund"), r.get(k)
                if isinstance(a, (int, float)) and not isinstance(a, bool) \
                        and isinstance(b, (int, float)) and not isinstance(b, bool):
                    fv.append(a)
                    rv.append(b)
            if len(fv) >= 100:
                c = fe.rank_ic(fv, rv)
                corr[k] = {"rho": round(c, 4) if c is not None else None, "n": len(fv)}
                print("  fund × %-18s ρ=%+.4f  (n=%d)" % (k, c or 0, len(fv)))
        res["fund_corr_2026_10_27"] = corr
    except Exception as e:
        print(f"  ⚠️ 无法读取 scored 文件：{e}")

    # ---------- 落库
    out = {
        "round": 30,
        "purpose": "fund 维兜底修复（P0-2）后的 IC 重测 —— 老大裁决第二步",
        "verdict_scope": {
            "covered": "兜底修复（无估值 → None）的精确效应",
            "not_covered": ["ROE 量纲修复（fwd_hist 未存 roe/roe_unit）",
                            "cagr3/gross_margin/q3_info/fwd_np/hist_loss_free（历史未存）"],
        },
        "fake_audit": {
            "total_stock_days_with_fund": _audit["total_stock_days_with_fund"],
            "fake_no_pe": _audit["fake_no_pe"],
            "fake_pct": round(_fake_pct, 2),
            "real_zero_kept": _audit["real_zero_kept"],
            "fake_dist": dict(sorted(_audit["fake_dist"].items(), key=lambda x: -x[1])),
        },
        "modes": {k: res.get(k) for k in res if "|h" in k},
        "paired_fix_vs_old": pair,
        "thresholds": {"bonf": T_BONF, "bonf_strict_after_304_tests": T_BONF_STRICT},
    }
    if "fund_corr_2026_10_27" in res:
        out["fund_corr_2026_10_27"] = res["fund_corr_2026_10_27"]

    with open(os.path.join(HERE, "audit_round30.json"), "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print(f"\n已写出 audit_round30.json")

    # ---------- 结论
    print("\n" + "=" * 108)
    print("【结论】")
    print("=" * 108)
    for hop in (5, 20):
        x = res.get("X_old|h%d" % hop) or {}
        y = res.get("Y_nofake|h%d" % hop) or {}
        z = res.get("Z_fillmed|h%d" % hop) or {}
        print(f"  h{hop}:")
        print(f"    旧口径(含伪分)  IC={x.get('ic'):+.4f}  t0={x.get('t0'):+.2f}  Bonf={'✅' if x.get('pass_bonf') else '❌'}")
        print(f"    修复后(剔除)    IC={y.get('ic'):+.4f}  t0={y.get('t0'):+.2f}  Bonf={'✅' if y.get('pass_bonf') else '❌'}")
        print(f"    填充中位数对照  IC={z.get('ic'):+.4f}  t0={z.get('t0'):+.2f}  Bonf={'✅' if z.get('pass_bonf') else '❌'}")
        d = (y.get("ic") or 0) - (x.get("ic") or 0)
        print(f"    修复效应 ΔIC={d:+.4f}")
    print()


if __name__ == "__main__":
    main()
