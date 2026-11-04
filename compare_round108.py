#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""round108 收尾三道对照：v4.17-cycle-rules（含规则1/2 + P1g 去重）vs v4.16.1 基线。

对照口径固定三道（项目惯例，禁自造指标）：
  ① ρ(total 新旧)  —— 整体改动幅度
  ② Top50 重合      —— 头部是否被规则打散
  ③ 评级变动只数    —— 跨档位移规模
再叠加本轮的专属核对：
  ④ 68 只周期规则命中票的位移专项（老大点名：华锡有色/东方锆业/新疆众和/中远海能/广汇能源）
  ⑤ 光伏 28 只新增周期票的位移（D6 裁决的直接影响面）
  ⑥ 扣分传导是否闭合：score_growth 降幅 → total 降幅（round108 §六 的失效风险验收）
"""
import json
import math
from collections import Counter

OLD = "top200_scored_2026-11-02.json.snap_r108_before"
NEW = "top200_scored_2026-11-02.json"

old = json.load(open(OLD))["results"]
new = json.load(open(NEW))["results"]
old_by = {r["code"]: r for r in old}
new_by = {r["code"]: r for r in new}

W = {"fund": .20, "size_growth": .20, "tech_neg": .10, "quality": .10,
     "ep": .10, "dominant": .10, "growth": .10, "bias60": .05, "rsi_rev": .05}
W_OLD = dict(W, growth=.05, rsi_rev=.10)


def _rho(pairs):
    n = len(pairs)
    if n < 3:
        return float("nan")
    mx = sum(a for a, _ in pairs) / n
    my = sum(b for _, b in pairs) / n
    num = sum((a - mx) * (b - my) for a, b in pairs)
    den = math.sqrt(sum((a - mx) ** 2 for a, _ in pairs) *
                    sum((b - my) ** 2 for _, b in pairs))
    return num / den if den else float("nan")


pairs = [(old_by[c]["total"], new_by[c]["total"]) for c in new_by
         if c in old_by and isinstance(old_by[c].get("total"), (int, float))]
print(f"① ρ(total 新旧) = {_rho(pairs):.4f}   n={len(pairs)}")

old_rk = {c: i + 1 for i, c in enumerate(sorted(
    [c for c in old_by if isinstance(old_by[c].get("total"), (int, float))],
    key=lambda c: -old_by[c]["total"]))}
new_rk = {c: i + 1 for i, c in enumerate(sorted(
    [c for c in new_by if isinstance(new_by[c].get("total"), (int, float))],
    key=lambda c: -new_by[c]["total"]))}

old50 = set(sorted(old_rk, key=lambda c: old_rk[c])[:50])
new50 = set(sorted(new_rk, key=lambda c: new_rk[c])[:50])
print(f"② Top50 重合 = {len(old50 & new50)}/50   掉出: "
      f"{[old_by[c]['name'] for c in sorted(old50 - new50, key=lambda c: old_rk[c])]}")
print(f"   新进: {[new_by[c]['name'] for c in sorted(new50 - old50, key=lambda c: new_rk[c])]}")

chg = [c for c in new_by if old_by.get(c, {}).get("rating") != new_by[c].get("rating")]
rc = Counter(r.get("rating") for r in new)
print(f"③ 评级变动 = {len(chg)} 只 | 新分布 "
      f"S:{rc.get('S',0)} A:{rc.get('A',0)} B:{rc.get('B',0)} "
      f"C:{rc.get('C',0)} D:{rc.get('D',0)}")
tot = [r["total"] for r in new if isinstance(r.get("total"), (int, float))]
print(f"   total: min {min(tot):.2f} / max {max(tot):.2f} | "
      f"≥75: {sum(1 for t in tot if t >= 75)} 只（首页卡片门槛）")

# ---- ④ 周期规则命中票专项 ----
print("\n④ 规则命中票位移（按扣分降序，含老大点名 5 只）")
_rows = []
for c, r in new_by.items():
    mb = ((r.get("fund_detail") or {}).get("modeB") or {})
    if mb.get("rule_ded_pct"):
        _rows.append((mb.get("rule_ded_pct"), c, r, mb))
_rows.sort(key=lambda x: -x[0])
print(f"{'名称':<10s}{'二级':<12s}{'扣%':>5s}{'sg_old':>7s}{'sg_new':>7s}"
      f"{'旧#':>5s}{'新#':>5s}{'Δ位':>6s}{'旧total':>8s}{'新total':>8s}{'Δtotal':>7s}  规则")
FOCUS = {"华锡有色", "东方锆业", "新疆众和", "中远海能", "广汇能源",
         "奥特维", "帝科股份", "聚和材料"}
for ded, c, r, mb in _rows:
    o = old_by.get(c, {})
    sg_o, sg_n = o.get("score_growth"), r.get("score_growth")
    d_rk = old_rk.get(c, 0) - new_rk.get(c, 0)
    tags = []
    if mb.get("rule1_hit"):
        tags.append(f"R1(gm+{mb.get('gm_rise_pp')}pp)")
    if mb.get("rule2_hit"):
        tags.append(f"R2(dm{mb.get('accel_demeaned_pp')}pp)")
    mark = "★" if r.get("name") in FOCUS else " "
    print(f"{mark}{str(r.get('name'))[:8]:<9s}{str(mb.get('sw_l2'))[:11]:<12s}"
          f"{ded*100:>4.0f}%{(sg_o if sg_o is not None else 0):>7.2f}"
          f"{(sg_n if sg_n is not None else 0):>7.2f}"
          f"{old_rk.get(c,0):>5d}{new_rk.get(c,0):>5d}{d_rk:>+6d}"
          f"{(o.get('total') or 0):>8.2f}{r['total']:>8.2f}"
          f"{r['total']-(o.get('total') or 0):>+7.2f}  {'+'.join(tags)}")

# ---- ⑤ 光伏新增 28 只 ----
print("\n⑤ 光伏设备（D6 新增周期）全量位移")
_ph = [(c, r) for c, r in new_by.items()
       if ((r.get("fund_detail") or {}).get("modeB") or {}).get("sw_l2") == "光伏设备"]
_ph.sort(key=lambda x: new_rk.get(x[0], 9999))
print(f"   命中 {len(_ph)} 只（池内）")
print(f"{'名称':<10s}{'旧#':>5s}{'新#':>5s}{'Δ位':>6s}{'扣%':>5s}{'sg_old':>7s}{'sg_new':>7s}{'Δtotal':>7s}")
for c, r in _ph:
    o = old_by.get(c, {})
    mb = ((r.get("fund_detail") or {}).get("modeB") or {})
    print(f"{str(r.get('name'))[:8]:<10s}{old_rk.get(c,0):>5d}{new_rk.get(c,0):>5d}"
          f"{old_rk.get(c,0)-new_rk.get(c,0):>+6d}"
          f"{(mb.get('rule_ded_pct') or 0)*100:>4.0f}%"
          f"{(o.get('score_growth') or 0):>7.2f}{(r.get('score_growth') or 0):>7.2f}"
          f"{r['total']-(o.get('total') or 0):>+7.2f}")

# ---- ⑥ 扣分传导闭合验收 ----
# ⚠️ 2026-11-02 修正：原版直接把「规则命中票的 Δtotal」当纯规则效应，
#   实测扣 10% 组传导比算出 7.97（理论应≈0.20）——**口径错误**，已重写为反事实分解。
#   真相：规则只作用于 score_growth 一维（权重 0.10），扣 10~25% 合成分
#   → 总分至多变动 ≈0.10×2.0×扣幅×分位量级 ≈ −0.2~−0.5 分。
#   而那些 −17 分的大位移来自另一条独立链路：P1g 去重改变了 score_growth 分布
#   → ep_size_ind.growth_weight() 的 G 档位翻转（G>7 / G>4 / ≤4）
#   → score_size_growth（**20% 权重**）从 6.93 直接归零 → 总分 −13.9。
#   故此处必须把「规则效应」与「门限连带效应」拆开，否则归因完全错误。
print("\n⑥ 规则纯效应 vs 门限连带效应（反事实分解）")
import sys as _sys
_sys.path.insert(0, ".")
try:
    import enhance_scores as _E
    import scale_calc as _SC
    _Wn = _E._load_w10()
    _DM = _E._load_dim_max()

    def _tot(rr, W):
        _p = [(k, rr.get(k), v) for k, v in W.items() if v > 0]
        t, _ = _SC.composite_total(_p, _DM,
                                   weights={k: v for k, v in W.items() if v > 0})
        return t

    # 反事实：保留新权重、新 size_growth/ep 等一切，只把 score_growth 还原成「未扣规则」的值
    #   未扣规则值 = 扣后分 / (1 - r_ded)
    _rule_only = []
    _gate_only = []
    for ded, c, r, mb in _rows:
        o = old_by.get(c, {})
        if not isinstance(o.get("total"), (int, float)):
            continue
        _sg_new = r.get("score_growth")
        if isinstance(_sg_new, (int, float)) and ded > 0:
            _sg_norule = _sg_new / (1.0 - ded)      # 反事实：规则未触发
        else:
            _sg_norule = _sg_new
        _t_norule = _tot(dict(r, score_growth=_sg_norule), _Wn)
        _rule_only.append(_tot(r, _Wn) - _t_norule)
        # 门限连带：新口径下 size_growth 用旧行值 vs 新行值
        _gate_only.append(_tot(dict(r, score_size_growth=o.get("score_size_growth")), _Wn)
                          - _tot(r, _Wn))
    import statistics as _st
    print(f"   n={len(_rule_only)} 只规则命中票")
    print(f"   ① 规则纯效应（仅 score_growth 扣分） 均 {_st.mean(_rule_only):+.3f}  "
          f"中位 {_st.median(_rule_only):+.3f}  min {min(_rule_only):+.3f}")
    print(f"   ② size_growth 门限连带效应（新口径 vs 沿用旧 size_growth）"
          f" 均 {_st.mean(_gate_only):+.3f}  中位 {_st.median(_gate_only):+.3f}")
    print(f"   → 规则纯效应均值 {_st.mean(_rule_only):+.2f} 分（w=.10 × 量程2 × 扣幅×分位，量级合理）；"
          f"规则票里出现的 −17/-15 分大位移由门限连带主导，与规则无关")
except Exception as _e:
    print(f"   ⚠️ 反事实分解失败（跳过）: {_e!r}")

# 全池三层分解（权重 / 维度分 / 门限连带）
try:
    import statistics as _st2
    _W_old = dict(_Wn, score_growth=0.05, score_rsi_rev=0.10)
    _d_w, _d_g, _d_sz = [], [], []
    for c, r in new_by.items():
        o = old_by.get(c)
        if not o or not isinstance(o.get("total"), (int, float)):
            continue
        _t_old = _tot(o, _W_old)
        _t_w = _tot(o, _Wn)
        _t_g = _tot(dict(r, score_size_growth=o.get("score_size_growth")), _Wn)
        _t_new = _tot(r, _Wn)
        _d_w.append(_t_w - _t_old)
        _d_g.append(_t_g - _t_w)
        _d_sz.append(_t_new - _t_g)
    print(f"\n   全池 {len(_d_w)} 只三层分解：")
    print(f"     ① 纯权重效应（分数不变，只换 .05/.10）  均 {_st2.mean(_d_w):+.3f}  "
          f"中位 {_st2.median(_d_w):+.3f}")
    print(f"     ② 维度分效应（growth 去重 + 规则 + ep） 均 {_st2.mean(_d_g):+.3f}  "
          f"中位 {_st2.median(_d_g):+.3f}")
    print(f"     ③ size_growth 门限连带                  均 {_st2.mean(_d_sz):+.3f}  "
          f"中位 {_st2.median(_d_sz):+.3f}")
    print(f"     ⚠️ ①为负的系统性原因：rsi_rev 让出 5pp 给 growth，而 growth 分位均值"
          f"低于 rsi_rev 归一值 → 等权替换整体降分，属预期内")
except Exception as _e:
    print(f"   ⚠️ 三层分解失败（跳过）: {_e!r}")

# ---- 全池 score_growth 变化分布 ----
_sgc = [((new_by[c].get("score_growth") or 0) - (old_by[c].get("score_growth") or 0))
        for c in new_by if c in old_by]
_sgc.sort()
print(f"\n   score_growth 全池变化: n={len(_sgc)} 中位 {_sgc[len(_sgc)//2]:+.3f} "
      f"min {_sgc[0]:+.2f} max {_sgc[-1]:+.2f} "
      f"| 变化>0.5 只数 {sum(1 for x in _sgc if abs(x) > 0.5)}")

print("\n新 Top15（v4.17）：")
for c in sorted(new_rk, key=lambda c: new_rk[c])[:15]:
    r = new_by[c]
    mb = ((r.get("fund_detail") or {}).get("modeB") or {})
    print(f"  {new_rk[c]:>2d} {r['total']:6.2f} {str(r.get('name'))[:10]:<10s} {c}  "
          f"sg={(r.get('score_growth') or 0):5.2f}  "
          f"cyc={'Y' if mb.get('is_cycle') else '-'}  ded={mb.get('rule_ded_pct')}")
