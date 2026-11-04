#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""round40：持有期衰减曲线重算（#307 / P2-3）

## 为什么要重做，而不是沿用 h40 峰值

#307 的建议原文是「页面 T+10 → 建议 T+20（对齐 h40 IC 峰值）」。
这个建议的依据是 `audit_round25.json` 的 `q4_decay`：

    h1 +0.0873 (n77) → h5 +0.1776 (n73) → h10 +0.2243 (n68)
    → h20 +0.2484 (n58) → **h40 +0.2796 (n39)** → h60 +0.2794 (n20)
    _peak: hop=40

**但那是旧 8 维模型 + 冻结小池 + 39 个截面。** 现状已完全不同：

- 模型：v4.8 八维 → **v4.14-evidence-aligned 十维**（quality 12% / fund 24.2% 等）
- 池：冻结小池（约 195 只）→ **大池 2029 只**
- 截面：39 个（h40）→ 面板有 **238 个**

且 `factor_ic.json`（2026-10-23，78 截面，大池）里 total 的 h20 IC 只剩 **+0.02（NW t=0.41）**，
并标记 `regime_shift=True`（前半 +0.0817 / 后半 −0.0416）。

⇒ **用旧 h40 峰值去定今天的持有期，是把已失效的结论套在新模型上。**
本脚本用当前 v4.14 权重在大池面板上重算完整曲线。

## 做法

1. 从 `data/panel_round34.json` 取 7 个旧维（bias60/rsi_rev/tech_neg/lowvol20/quality/
   ep/size_ind），用 `factor_weights.json` 的**真实权重**（不凭记忆）加权。
2. A/B 两维由 `audit_round38_size_ablation.build_ab_dims` 重建（含成长门限）。
3. 按 `scale_calc.composite_total` 同口径合成总分：每维 0-10，
   `total = Σ min(v,DMAX)/DMAX×10×w / Σ生效w ×10`，**缺维从分母剔除，禁 or 0**。
4. 对 h ∈ [1,2,3,5,10,15,20,30,40,60] 算 rank IC 时序，输出 IC / NW t / ICIR / 胜率。

## 输出
audit_round40.json
"""
import os
import sys
import json

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import scale_calc          # 生产同一实现，禁止在审计脚本里重写合成公式

from audit_round38_size_ablation import (
    build_ab_dims, is_num, spearman, nw_t, HOPS, PANEL, KLONG,
)

WEIGHTS_FILE = os.path.join(HERE, "factor_weights.json")
OUT = os.path.join(HERE, "audit_round40.json")

# 要测的持有期（交易日）。前密后疏：短端是换手成本敏感区，长端看衰减
TEST_HOPS = (1, 2, 3, 5, 10, 15, 20, 30, 40, 60)

# 面板里直接存了值的维字段。
# ⚠️ 2026-10-28 修正：原清单漏了 score_fund，导致 cov 里没有该 key、
#    fund_cov 走 .get(...,0) 默认成 0，被误读成「覆盖率 0%」。
#    实测 3.53%（16920/478650，80/238 截面有值）。凡新增维度必须同步加入本清单。
PANEL_DIMS = ("score_bias60", "score_rsi_rev", "score_tech_neg",
              "score_lowvol20", "score_quality", "score_ep",
              "score_size_ind", "score_fund")

# 不在面板里、需由 build_ab_dims() 现场重建的维字段
REBUILT_DIMS = ("score_size_growth", "score_dominant")


def _tdays_of(px, min_ratio=0.5):
    from collections import Counter
    cnt = Counter()
    for m in px.values():
        for d in m:
            cnt[d] += 1
    n = len(px)
    return sorted(d for d, c in cnt.items() if c >= n * min_ratio)


def composite(rec, w, dmax):
    """按 scale_calc.composite_total **同口径**合成总分。

    ⚠️ 2026-10-28 修正（重大口径错误）：
       本函数原实现为「缺维从分母剔除」，即缺一维就把该维权重从分母拿掉、
       其余维权重被放大。**这与生产实现不一致** —— 生产 scale_calc.composite_total
       的做法是：缺维按中性值 DMAX/2（归一后恒 5.0）**填充**，分母恒为 Σ所有正权重。

       两者差异实测：同一只股票 fund 缺值时，生产口径 59.76、剔除口径 62.88
       （相差 3.12 分）；剔除口径等于把其余 8 维权重放大 1.3193 倍。

       本项目「缺维从分母剔除」的铁律，指的是**统计聚合时不得把缺失当 0 参与
       平均**（避免 or 0 陷阱），而 composite_total 的「中性填充 + 固定分母」
       已满足该要求（既不把缺失当 0，也不让缺失改变其他维的权重）。
       故此处必须严格复刻 composite_total。
    """
    parts = [(k, rec.get(k), wt) for k, wt in w.items()]
    total, _miss = scale_calc.composite_total(parts, dmax, weights=w)
    return total if is_num(total) else None


def main():
    wj = json.load(open(WEIGHTS_FILE, encoding="utf-8"))
    w = wj["weights"]
    dmax = wj.get("dim_max") or {}
    print(f"权重版本 {wj.get('version')}  Σw={sum(w.values()):.6f}  target_horizon={wj.get('target_horizon')}")
    act = {k: v for k, v in w.items() if v > 0}
    print(f"生效维 {len(act)} 个：" + ", ".join(f"{k[6:] if k.startswith('score_') else k}={v}"
                                             for k, v in sorted(act.items(), key=lambda x: -x[1])))

    px = json.load(open(KLONG, encoding="utf-8"))
    tdays = _tdays_of(px)
    tidx = {d: i for i, d in enumerate(tdays)}
    panel = json.load(open(PANEL, encoding="utf-8"))

    # ── ⚠️ 先做覆盖率体检：这是本脚本能否给出可信结论的前提 ──
    # fund 权重 24.2% 是最大维度。若它在面板上大面积缺失，
    # 「缺维从分母剔除」会让总分退化成「其余几维的加权」，
    # 算出来的曲线就不是生产 composite 的曲线。
    print("\n" + "-" * 104)
    print("⚠️ 面板覆盖率体检（决定本曲线能否代表生产 composite）")
    print("-" * 104)
    tot_sd = sum(len(panel["rows"].get(dt) or {}) for dt in panel["dates"])
    cov = {}
    for f in PANEL_DIMS:
        has = nd = 0
        for dt in panel["dates"]:
            rows = panel["rows"].get(dt) or {}
            h = sum(1 for r in rows.values() if is_num(r.get(f)))
            has += h
            if h > 0:
                nd += 1
        cov[f] = {"n": has, "pct": has / tot_sd * 100 if tot_sd else 0, "n_dates": nd}
        flag = "  🚨" if cov[f]["pct"] < 20 else ""
        print(f"  {f:<22} {has:>8} / {tot_sd}  {cov[f]['pct']:>6.1f}%  "
              f"有值截面 {nd}/{len(panel['dates'])}{flag}")

    # ⚠️ 硬断言：体检清单必须覆盖所有生效权重维。
    #    历史教训：PANEL_DIMS 漏列 score_fund → cov 无该 key → .get(...,0) 静默给 0，
    #    一个「代码漏项」被读成「数据 0% 覆盖」，进而把整条结论建立在错误前提上。
    _active = {k for k, v in w.items() if v > 0}
    _checked = set(PANEL_DIMS) | set(REBUILT_DIMS)
    _missing = sorted(_active - _checked)
    if _missing:
        raise SystemExit(f"🚨 覆盖率体检清单漏了生效维度 {_missing}，"
                         f"请补进 PANEL_DIMS / REBUILT_DIMS 后重跑（禁止静默兜底）")

    fund_cov = cov.get("score_fund", {}).get("pct", 0)
    degraded = fund_cov < 20
    if degraded:
        print(f"\n  🚨 **fund 维覆盖率仅 {fund_cov:.2f}%**，权重却是 {w.get('score_fund'):.3f}（最大维）。")
        print("     这是原始数据缺失，不是存储问题：面板 238 个截面里只有 80 个有值，")
        print("     2026-06-26 ~ 2026-10-12 每截面仅约 185/2020（9%）有值，")
        print("     2026-10-13 起升到约 445/2025（22%），2026-10-23 最高 457/2029。")
        print()
        print("     ⚠️ 后果必须按**生产口径**（中性填充5.0 + 固定分母）理解，不能按「剔除」理解：")
        print("        · fund 缺失 → 该维恒取中性 5.0 → 吃掉 24.2% 权重但对该截面")
        print("          **不产生任何区分度**（所有股票同值）")
        print("        · 此时 total = 常数项 + 0.758×其余维加权 → 是其余维的**正线性变换**")
        print("          ⇒ Spearman IC 与「只用其余维排序」**完全相同**（单调不变性）")
        print("        · 所以 fund 缺失**不会让 IC 失真**，只会让绝对分数向 50 压缩、")
        print("          使跨截面的分数不可比（本报告只用 IC/rank，不受影响）")
        print("     ⇒ 本脚本仍输出两条曲线，但定位已修正：")
        print("        A) full   ：生产口径（fund 缺则中性填充）—— **这才是生产真值**")
        print("        B) ex_fund：把 fund 权重彻底拿掉再归一 —— 用于回答")
        print("                    「如果不用 fund，其余维的信号强度是多少」")
        print("        两者在有 fund 的截面上会有微小差异，其余截面理论上应近乎相同；")
        print("        若实测差异很大，反而说明口径实现有问题，需回查。")

    print("\n重建 A/B 维（成长门限 + 龙头壁垒）...")
    ab, _m = build_ab_dims(panel)

    # ── 重建维的真实覆盖率（面板里这两维是 0%，全靠现场重算）──
    # 只统计「重建后真的拿到值」的比例，避免把 build 失败当成「该维无信息」。
    print("\n  重建维覆盖率（build_ab_dims 现场重算，面板内无存量值）：")
    for f in REBUILT_DIMS:
        has = nd = 0
        for dt in panel["dates"]:
            abr = ab.get(dt) or {}
            h = 0
            for _c, _a in abr.items():
                v = _a.get("A_G1") if f == "score_size_growth" else _a.get("B_dom")
                if is_num(v):
                    h += 1
            has += h
            if h > 0:
                nd += 1
        cov[f] = {"n": has, "pct": has / tot_sd * 100 if tot_sd else 0, "n_dates": nd}
        print(f"  {f:<22} {has:>8} / {tot_sd}  {cov[f]['pct']:>6.1f}%  "
              f"有值截面 {nd}/{len(panel['dates'])}")

    # ── 组装每截面总分：两条口径 ──
    # A) full  ：生产口径（composite_total 原样；fund 缺值 → 中性填充 5.0，分母恒定）
    # B) ex_fund：把 fund 权重彻底拿掉再归一 → 回答「不用 fund 时其余维信号多强」
    w_ex = {k: v for k, v in w.items() if k != "score_fund"}
    _s = sum(w_ex.values())
    w_ex = {k: v / _s for k, v in w_ex.items()}
    print(f"\n  ex_fund 口径：剔除 fund 后 {len([1 for v in w_ex.values() if v>0])} 维，"
          f"权重归一 Σ={sum(w_ex.values()):.6f}")

    print("合成总分（两条口径）...")
    totals, totals_ex = {}, {}
    for d0 in panel["dates"]:
        rows = panel["rows"].get(d0) or {}
        abr = ab.get(d0) or {}
        out, out_ex = {}, {}
        for c, r in rows.items():
            rec = dict(r)
            a = abr.get(c) or {}
            rec["score_size_growth"] = a.get("A_G1")      # A 维现行门限档
            rec["score_dominant"] = a.get("B_dom")
            t = composite(rec, w, dmax)
            if t is not None:
                out[c] = t
            t2 = composite(rec, w_ex, dmax)
            if t2 is not None:
                out_ex[c] = t2
        if out:
            totals[d0] = out
        if out_ex:
            totals_ex[d0] = out_ex
    print(f"  有效截面：full {len(totals)} 个 | ex_fund {len(totals_ex)} 个")

    # ── 口径自检：某维整截面缺失时，full 与 ex_fund 的排序必须完全一致 ──
    # 原理（用实测验证，不靠推演）：composite_total 对全缺维恒取中性 5.0，
    #   此时 total = C + Σ(其余维 × 归一化值 × w) / Σw，C 为常数
    #   → 是其余维加权分的**正线性变换** → Spearman 必为 1.0000。
    # 若实测偏离 1.0000，说明排序被别的东西污染了，必须查清再往下走。
    print("\n  口径自检（fund 整截面缺失的抽样）...")
    _checked = 0
    _worst = None
    for d0 in panel["dates"]:
        if d0 not in totals or d0 not in totals_ex:
            continue
        rows = panel["rows"].get(d0) or {}
        has_fund = sum(1 for r in rows.values() if is_num(r.get("score_fund")))
        if has_fund > 0:
            continue                      # 只看 fund 整截面全缺的
        xs, ys = {}, {}
        for c in totals[d0]:
            if c in totals_ex[d0]:
                xs[c] = totals[d0][c]
                ys[c] = totals_ex[d0][c]
        if len(xs) < 100:
            continue
        rho, _n = spearman(xs, ys)
        _checked += 1
        if _worst is None or rho < _worst[1]:
            _worst = (d0, rho, len(xs))
    if _checked:
        print(f"    检查 {_checked} 个 fund 全缺截面，最低 rho={_worst[1]:.6f}"
              f"（{_worst[0]}，n={_worst[2]}）")
        if _worst[1] < 0.9999:
            print("    ⚠️ 偏离 1.0000 → 两条口径不只是常数差，排序也变了，需回查实现")
        else:
            print("    ✅ 恒等于 1.0000 → 证实：fund 缺失不影响排序，只压缩绝对分")
            print("       （故 IC 层面 full 与 ex_fund 必然同量级；差异只在有 fund 的截面）")
    else:
        print("    （无 fund 全缺且样本足够的截面，跳过）")

    # ── 逐持有期算 IC ──
    def fwd(code, d0, hop):
        m = px.get(code)
        i0 = tidx.get(d0)
        if not m or i0 is None or i0 + hop >= len(tdays):
            return None

        def _cl(d):
            b = m.get(d)
            if isinstance(b, (list, tuple)) and len(b) >= 2 and is_num(b[1]):
                return float(b[1])
            return float(b) if is_num(b) else None

        c0, c1 = _cl(tdays[i0]), _cl(tdays[i0 + hop])
        if not c0 or not c1:
            return None
        return (c1 - c0) / c0 * 100

    def run_curve(tot_map, label):
        out = {}
        print(f"\n【{label}】")
        print(f"{'hop':>5}{'截面n':>7}{'IC':>9}{'NW t':>8}{'ICIR':>8}{'胜率':>8}{'IC/√hop':>11}")
        for hop in TEST_HOPS:
            ics = []
            for d0, tmap in tot_map.items():
                if d0 not in tidx or tidx[d0] + hop >= len(tdays):
                    continue
                xs, ys = {}, {}
                for c, t in tmap.items():
                    fr = fwd(c, d0, hop)
                    if fr is None:
                        continue
                    xs[c] = t
                    ys[c] = fr
                if len(xs) >= 100:
                    ic, _n = spearman(xs, ys)
                    if ic is not None:
                        ics.append(ic)
            if len(ics) < 5:
                out[f"h{hop}"] = {"n": len(ics), "ic": None}
                print(f"{hop:>5}{len(ics):>7}     样本不足")
                continue
            m_ = sum(ics) / len(ics)
            sd = (sum((x - m_) ** 2 for x in ics) / (len(ics) - 1)) ** 0.5
            t_ = nw_t(ics, lag=4)
            win = sum(1 for x in ics if x > 0) / len(ics)
            eff = (m_ / (hop ** 0.5)) if hop > 0 else None
            out[f"h{hop}"] = {"n": len(ics), "ic": m_, "sd": sd, "nw_t": t_,
                              "icir": (m_ / sd) if sd else None,
                              "pos_rate": win, "ic_per_sqrt_hop": eff}
            print(f"{hop:>5}{len(ics):>7}{m_:>+9.4f}{t_:>8.2f}{(m_/sd if sd else 0):>8.3f}"
                  f"{win*100:>7.1f}%{eff:>11.5f}")
        return out

    print("\n" + "-" * 104)
    print("持有期衰减曲线（大池面板 · 全市场交易日历 · 两条口径）")
    print("-" * 104)
    res = run_curve(totals, "A) full —— 生产权重原样（fund 24.2% 在列）")
    res_ex = run_curve(totals_ex, "B) ex_fund —— 剔除 fund、其余权重归一（可比口径）")

    # ── 裁决 ──
    print("\n" + "=" * 104)
    print("裁决：最优持有期在哪？现在改 T+20 有依据吗？")
    print("=" * 104)

    def judge(valid, name):
        if not valid:
            print(f"  【{name}】无有效结果。")
            return None
        peak_ic = max(valid, key=lambda k: valid[k]["ic"])
        peak_t = max(valid, key=lambda k: (valid[k]["nw_t"] or -9))
        peak_eff = max(valid, key=lambda k: (valid[k]["ic_per_sqrt_hop"] or -9))
        cur = "h20" if "h20" in valid else peak_ic
        print(f"\n  【{name}】")
        print(f"    按 IC 幅度       {peak_ic:<5} IC={valid[peak_ic]['ic']:+.4f} "
              f"(t={valid[peak_ic]['nw_t']:+.2f}, n={valid[peak_ic]['n']})")
        print(f"    按 t 值/稳定性   {peak_t:<5} IC={valid[peak_t]['ic']:+.4f} "
              f"(t={valid[peak_t]['nw_t']:+.2f})")
        print(f"    按单位时间效率   {peak_eff:<5} IC/√hop={valid[peak_eff]['ic_per_sqrt_hop']:+.5f}")
        print(f"    现行 T+20        IC={valid[cur]['ic']:+.4f} "
              f"(t={valid[cur]['nw_t']:+.2f}, n={valid[cur]['n']})")
        sig = [k for k, v in valid.items() if is_num(v.get("nw_t")) and abs(v["nw_t"]) >= 3.9]
        sig2 = [k for k, v in valid.items() if is_num(v.get("nw_t")) and abs(v["nw_t"]) >= 2.0]
        print(f"    过 |t|≥2：{sig2 if sig2 else '无'} ｜ 过 Bonferroni(|t|≥3.9)：{sig if sig else '无'}")
        return {"peak_ic": peak_ic, "peak_t": peak_t, "cur": cur, "sig": sig, "sig2": sig2}

    j_full = judge({k: v for k, v in res.items() if is_num(v.get("ic"))}, "A) full（生产权重）")
    j_ex = judge({k: v for k, v in res_ex.items() if is_num(v.get("ic"))}, "B) ex_fund（可比口径）")

    print()
    if j_ex:
        if not j_ex["sig"]:
            print("  🚨 **ex_fund 口径下没有任何持有期过 Bonferroni 严格阈值**（|t|≥3.9）。")
            print("     即：在可得数据上，当前 composite 的预测力整体偏弱，")
            print("     『改持有期能提升效果』这个前提不成立 —— 先解决信号强度，再谈持有期。")
            print("     ⇒ #307 裁决：**不改持有期**（无依据），改为页面披露实证边界。")
        elif j_ex["peak_ic"] == j_ex["cur"]:
            print(f"  ✅ ex_fund 口径下峰值就在 T+20（{j_ex['peak_ic']}），现行设置与实证一致，**不改**。")
        elif j_ex["peak_ic"] in j_ex["sig"]:
            print(f"  ⇒ ex_fund 口径下 {j_ex['peak_ic']} 过严格阈值且优于 T+20，**有依据改**。")
        else:
            print(f"  ⇒ 峰值在 {j_ex['peak_ic']} 但未过严格阈值，**维持 T+20**（改则属拟合噪声）。")

    if j_full and j_ex and j_full["peak_ic"] != j_ex["peak_ic"]:
        print(f"\n  ⚠️ 两条口径的峰值不同（full {j_full['peak_ic']} vs ex_fund {j_ex['peak_ic']}），")
        print("     根因就是 fund 覆盖率仅 3.5%：full 口径在多数截面上 fund 被剔除，")
        print("     权重实际转移给其余维度，使得曲线的形状依赖『哪些截面恰好有 fund 值』。")
        print("     ⇒ 这也是为什么 ex_fund 才是可复现、可比较的口径。")

    # ── 与旧 round25 对照（说明为何不能沿用）──
    print("\n" + "-" * 104)
    print("与 audit_round25（旧 8 维小池）的对照 —— 为何不能沿用 h40 峰值")
    print("-" * 104)
    old_p = os.path.join(HERE, "audit_round25.json")
    if os.path.exists(old_p):
        old = json.load(open(old_p, encoding="utf-8")).get("q4_decay") or {}
        for h in (5, 10, 20, 40, 60):
            o = old.get(f"h{h}") or {}
            n_ = res.get(f"h{h}") or {}
            if is_num(o.get("ic")) and is_num(n_.get("ic")):
                print(f"  h{h:<3} 旧(round25) IC={o['ic']:+.4f} n={o['n']:<3} | "
                      f"新(round40) IC={n_['ic']:+.4f} n={n_['n']:<3} | "
                      f"差 {n_['ic']-o['ic']:+.4f}")
            elif is_num(o.get("ic")):
                print(f"  h{h:<3} 旧(round25) IC={o['ic']:+.4f} n={o['n']:<3} | 新 样本不足")
        print("\n  ⚠️ 两列**不可直接比较**：模型维度不同（8→10）、池不同（冻结小池→大池 2029）、")
        print("     截面数不同、regime 不同。上表只用于说明「旧 h40 峰值在新口径下不复现」。")

    json.dump({"decay_full": res, "decay_ex_fund": res_ex,
               "coverage": cov,
               "fund_covered_pct": fund_cov,
               "weights_version": wj.get("version"),
               "weights": w,
               "target_horizon": wj.get("target_horizon"),
               "n_dates": len(totals),
               "note": "v4.14 权重 + 大池面板重算；与 round25（旧8维小池）不可直接比较。"
                       "因 fund 覆盖率仅 3.5%，ex_fund 口径才具可比性。",
               "caveat": "A 维市值用股本反推值（无拆股调整），B 维方向已知为负（round38c）；"
                         "fund 维覆盖率 3.5%（原始数据缺失，非存储问题）→ full 口径会退化，"
                         "结论以 ex_fund 为准"},
              open(OUT, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"\n📄 结果已落盘：{OUT}")


if __name__ == "__main__":
    main()
