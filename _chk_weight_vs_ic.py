#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""第4项核查：实际权重 vs 单维IC vs 消融边际贡献 三方对照。
数据全部实取，禁用记忆值。缺值跳过而非 or 0。
"""
import json, os

D = os.path.dirname(os.path.abspath(__file__))

def load(p):
    with open(os.path.join(D, p), "r", encoding="utf-8") as f:
        return json.load(f)

def fx(v, sign=False, nd=4, pct=False):
    if v is None:
        return "—"
    if pct:
        return f"{v*100:.1f}%"
    return f"{v:+.{nd}f}" if sign else f"{v:.{nd}f}"

fw = load("factor_weights.json")
r33 = load("audit_round33.json")
r23 = load("audit_round23.json")

W = fw["weights"]
DMAX = fw.get("dim_max", {})

DIMS = ["score_ep", "score_quality", "score_size_ind", "score_tech_neg",
        "score_bias60", "score_rsi_rev", "score_lowvol20", "score_fund"]
NAME = {"score_ep": "EP估值", "score_quality": "质量", "score_size_ind": "行业内市值",
        "score_tech_neg": "技术面反号", "score_bias60": "乖离60", "score_rsi_rev": "RSI反转",
        "score_lowvol20": "低波20", "score_fund": "fund基本面"}

effw = r33.get("eff_weight", {})
IC = {}
for d in DIMS:
    v = (effw.get(d) or {}).get("ic")
    if v is not None:
        IC[d] = float(v)

q1 = r23.get("q1_dim_ic", {})

print("=" * 100)
print("【A】单维 IC 双源交叉校验（h5）")
print("-" * 100)
print(f"{'维度':<12}{'round33 IC':>12}{'round23 IC':>12}{'round23 n':>10}{'round23 Bonf':>14}")
for d in DIMS:
    a = IC.get(d)
    b = ((q1.get(d) or {}).get("5") or {})
    bi = b.get("ic")
    bn = b.get("n")
    bb = b.get("robust_bonf")
    line = f"{NAME[d]:<12}{fx(a, True):>12}{fx(float(bi) if bi is not None else None, True):>12}"
    line += f"{(str(bn) if bn is not None else '—'):>10}"
    line += f"{(str(bb) if bb is not None else '—'):>14}"
    print(line)

abl = r33.get("ablation", {})
AB = {}
for k, v in abl.items():
    if not k.startswith("全样本|h5|drop_"):
        continue
    AB[k.split("drop_")[-1]] = v
BASE = (next(iter(AB.values()))["ic"] + next(iter(AB.values()))["d_ic"]) if AB else None

print()
print("=" * 100)
print(f"【B】三方对照表（复合 IC 基线 = {fx(BASE, True)}，h5 全样本消融）")
print("-" * 100)
print(f"{'维度':<12}{'名义w':>8}{'有效w':>8}{'单维IC':>10}"
      f"{'w×IC':>10}{'IC隐含w':>10}{'消融ΔIC':>10}{'消融t':>9}{'IC名次':>8}{'t名次':>8}")

rows = []
for d in DIMS:
    w = W.get(d, 0.0) or 0.0
    ew = (effw.get(d) or {}).get("eff_w")
    ic = IC.get(d)
    ab = AB.get(d) or {}
    rows.append(dict(d=d, w=w, ew=ew, ic=ic,
                     wic=(w * ic if ic is not None else None),
                     dic=ab.get("d_ic"), t=ab.get("t")))

rk_ic = {x["d"]: i + 1 for i, x in enumerate(
    sorted([x for x in rows if x["ic"] is not None], key=lambda z: -z["ic"]))}
rk_t = {x["d"]: i + 1 for i, x in enumerate(
    sorted([x for x in rows if x["t"] is not None], key=lambda z: -z["t"]))}
rk_w = {x["d"]: i + 1 for i, x in enumerate(
    sorted(rows, key=lambda z: -z["w"]))}

pos = {x["d"]: max(0.0, x["ic"]) for x in rows if x["ic"] is not None}
spos = sum(pos.values())

for x in sorted(rows, key=lambda z: (-(z["t"] if z["t"] is not None else -99))):
    d = x["d"]
    iw = (pos.get(d, 0.0) / spos) if spos else None
    line = f"{NAME[d]:<12}{x['w']*100:>7.1f}%{fx(x['ew'], pct=True):>8}"
    line += f"{fx(x['ic'], True):>10}{fx(x['wic'], True):>10}{fx(iw, pct=True):>10}"
    line += f"{fx(x['dic'], True):>10}{fx(x['t'], True, 2):>9}"
    line += f"{str(rk_ic.get(d, '—')):>8}{str(rk_t.get(d, '—')):>8}"
    print(line)

print()
print("=" * 100)
print("【C】fund 逐项名次（1=最高）")
print("-" * 100)
f = next(x for x in rows if x["d"] == "score_fund")
w_list = sorted([x["w"] for x in rows], reverse=True)
wic_list = sorted([x["wic"] for x in rows if x["wic"] is not None], reverse=True)
_top_ic = min(rk_ic, key=rk_ic.get)
_top_t = min(rk_t, key=rk_t.get)
print(f"  名义权重 {f['w']*100:.1f}%  → 名次 {rk_w['score_fund']}/8"
      f"（并列：{', '.join(NAME[x['d']] for x in rows if abs(x['w']-f['w'])<1e-9)} 同为 {f['w']*100:.1f}%）")
print(f"  单维 IC  {fx(f['ic'], True)} → 名次 {rk_ic['score_fund']}/8"
      f"   （最高为 {NAME[_top_ic]} {fx(IC[_top_ic], True)}）")
print(f"  w×IC     {fx(f['wic'], True)} → 名次 {wic_list.index(f['wic'])+1}/8")
print(f"  消融 ΔIC {fx(f['dic'], True)}")
print(f"  消融 t   {fx(f['t'], True, 2)} → 名次 {rk_t['score_fund']}/8"
      f"   （最高为 {NAME[_top_t]} {fx([x for x in rows if x['d']==_top_t][0]['t'], True, 2)}）")
print()
print("  单维IC名次 vs 权重名次 :", rk_ic['score_fund'], "vs", rk_w['score_fund'],
      "→", "一致" if rk_ic['score_fund'] == rk_w['score_fund'] else "不一致")
print("  消融t名次 vs 权重名次  :", rk_t['score_fund'], "vs", rk_w['score_fund'],
      "→", "一致" if rk_t['score_fund'] == rk_w['score_fund'] else "不一致")

print()
print("=" * 100)
print("【D】Bonferroni 稳健维（round23 q1_dim_ic h5, robust_bonf=True）")
print("-" * 100)
cnt = 0
for d, per in q1.items():
    h5 = per.get("5") or {}
    if h5.get("robust_bonf"):
        cnt += 1
        print(f"  ✓ {d:<20} n={h5.get('n')}  ic={fx(float(h5.get('ic')), True)}")
print(f"  共 {cnt} 个维度通过 Bonferroni。")
print()
print("=" * 100)
print("【E】dim_max 配置（页面量程归一化用）")
print("-" * 100)
for d in DIMS:
    print(f"  {NAME[d]:<12} {DMAX.get(d)}")
