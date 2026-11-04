# -*- coding: utf-8 -*-
"""audit_round90_trend_diag.py — 高分组「空头排列 + 区间收益差」归因审计（2026-10-31）

用户观察：首页推荐的高分标的均线呈空头排列，区间收益率不理想。
本脚本只做只读诊断，用真实日线（data/klines_long.json，[o,c,h,l,vol]）实算：
  1) 高分组(total≥75) vs 全池：均线排列占比 / 5/20/60日收益 / MA60乖离
  2) score_bias60 分桶 × 近20日收益（量化"接飞刀"效应）
  3) total 分数段 × 近20日收益（分数越高是否越跌）
  4) 趋势门槛压力测试：高分组里还剩几只 收盘>MA20 / >MA10

运行：python audit_round90_trend_diag.py
"""
import json
import os
import statistics as sta

HERE = os.path.dirname(os.path.abspath(__file__))
os.chdir(HERE)

kl = json.load(open("data/klines_long.json"))
sc = json.load(open("top200_scored_2026-10-30.json"))
rows = sc["results"]


def c6(code):
    return code.replace("sh", "").replace("sz", "").replace("bj", "")


def closes(code6):
    kk = kl.get(code6)
    if not kk:
        return []
    return [kk[d][1] for d in sorted(kk)]   # idx1 = close（[o,c,h,l,vol]）


def ma_stats(r):
    c = closes(c6(r["code"])) if r.get("code") else []
    if len(c) < 62:
        return None
    def ma(n):
        return sum(c[-n:]) / n
    m5, m10, m20, m60 = ma(5), ma(10), ma(20), ma(60)
    close = c[-1]
    return dict(
        bear_full=close < m5 < m10 < m20 < m60,
        bear_m=m5 < m10 < m20,
        below60=close < m60,
        below20=close < m20,
        above20=close > m20,
        above10=close > m10,
        ret5=(c[-1] / c[-6] - 1) * 100,
        ret20=(c[-1] / c[-21] - 1) * 100,
        ret60=(c[-1] / c[-61] - 1) * 100,
        bias60=(close / m60 - 1) * 100,
    )


def summarize(group, label):
    st = [s for s in (ma_stats(r) for r in group) if s]
    n = len(st)
    if not n:
        print(label, "无数据")
        return st
    print(f"--- {label} n={n}")
    for k, txt in [("bear_full", "完全空头 close<MA5<MA10<MA20<MA60"),
                   ("bear_m", "均线空头 MA5<MA10<MA20"),
                   ("below60", "收盘<MA60"),
                   ("below20", "收盘<MA20")]:
        c = sum(s[k] for s in st)
        print(f"  {txt:38s}: {c} ({c / n * 100:.0f}%)")
    print(f"  5日收益中位 {sta.median(s['ret5'] for s in st):+.2f}% | "
          f"20日 {sta.median(s['ret20'] for s in st):+.2f}% | "
          f"60日 {sta.median(s['ret60'] for s in st):+.2f}%")
    print(f"  乖离(close/MA60-1) 中位 {sta.median(s['bias60'] for s in st):+.2f}%")
    return st


hi = [r for r in rows if isinstance(r.get("total"), (int, float)) and r["total"] >= 75]
pool = [r for r in rows if r.get("code")]
print("=" * 60)
summarize(hi, "高分组 total≥75")
summarize(pool, "全池")

print("=" * 60)
print("score_bias60 桶 → 近20日收益中位（bias60 越高=越超跌）:")
bb = {}
for r in pool:
    c = closes(c6(r["code"]))
    if len(c) < 21 or r.get("score_bias60") is None:
        continue
    bb.setdefault(r["score_bias60"], []).append((c[-1] / c[-21] - 1) * 100)
for b in sorted(bb):
    xs = bb[b]
    print(f"  bias60={b}: n={len(xs):4d}  20日中位 {sta.median(xs):+.2f}%")

print("score_rsi_rev 桶 → 近20日收益中位:")
rb = {}
for r in pool:
    c = closes(c6(r["code"]))
    if len(c) < 21 or r.get("score_rsi_rev") is None:
        continue
    rb.setdefault(r["score_rsi_rev"], []).append((c[-1] / c[-21] - 1) * 100)
for b in sorted(rb):
    xs = rb[b]
    print(f"  rsi_rev={b}: n={len(xs):4d}  20日中位 {sta.median(xs):+.2f}%")

print("=" * 60)
print("total 分数段 → 近20日收益中位:")
tb = {}
for r in pool:
    c = closes(c6(r["code"]))
    if len(c) < 21 or not isinstance(r.get("total"), (int, float)):
        continue
    t = r["total"]
    seg = ">=75" if t >= 75 else ("70-75" if t >= 70 else ("60-70" if t >= 60 else ("50-60" if t >= 50 else "<50")))
    tb.setdefault(seg, []).append((c[-1] / c[-21] - 1) * 100)
for seg in [">=75", "70-75", "60-70", "50-60", "<50"]:
    xs = tb.get(seg, [])
    if xs:
        print(f"  {seg:6s}: n={len(xs):4d}  20日中位 {sta.median(xs):+.2f}%")

print("=" * 60)
st_hi = [s for s in (ma_stats(r) for r in hi) if s]
print(f"高分组趋势门槛压力测试 n={len(st_hi)}:")
print(f"  收盘>MA20: {sum(s['above20'] for s in st_hi)} 只")
print(f"  收盘>MA10: {sum(s['above10'] for s in st_hi)} 只")
print(f"  未完全空头排列(非 close<MA5<MA10<MA20<MA60): {sum(not s['bear_full'] for s in st_hi)} 只")
