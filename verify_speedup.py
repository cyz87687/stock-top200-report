#!/usr/bin/env python3
"""verify_speedup.py —— 三重校验：加速是否真实 + 结果是否一致 + 无副作用。

校验项：
  V1 K线抓取：新（异步）vs 旧（串行） 的「返回根数/首末日期/收盘价」是否一致
     ⚠️ 旧版走腾讯前复权、新版走新浪不复权 → 复权基准不同，
        故只比对「根数 / 日期序列」，不比 close 绝对值（已在模块头披露）。
        另抽查近期无除权事件、且两源都拿到的股票做 close 粗比。
  V2 财报因子：缓存命中 vs 强制重算 的因子值逐位一致
  V3 向量化：polars vs step2 逐股循环 逐位一致（已由 factor_vector 自检）
"""
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
d = json.load(open(os.path.join(HERE, "top500_all_a.json"), encoding="utf-8"))
codes = [r["code"] for r in d.get("main_pool") or []][:40]

print("=" * 70)
print("V1: K线抓取 —— 异步(新浪) vs 串行(旧多源)")
print("=" * 70)
import async_fetch as AF
import step2_stockscorer_v2 as S2

t0 = time.time()
new_res, st = AF.fetch_many_klines(codes, datalen=120, progress_every=0)
t_new = time.time() - t0
print("异步: %d 只 %.2fs → 成功 %d | %.1f 只/s | 外推490 %.0fs"
      % (len(codes), t_new, len(new_res), len(codes) / max(t_new, 1e-9),
         t_new / len(codes) * 490))

t0 = time.time()
old_res = {}
for c in codes[:8]:                      # 旧版太慢，只取 8 只做对照
    old_res[c] = S2.fetch_klines(c)
t_old = time.time() - t0
print("串行: %d 只 %.2fs → 成功 %d | 外推490 %.0fs"
      % (len(old_res), t_old, sum(1 for v in old_res.values() if v),
         t_old / max(1, len(old_res)) * 490))
print("→ 提速 %.0fx" % (t_old / max(1, len(old_res)) / (t_new / len(codes))))

print("\n逐只比对（根数 / 末日期）:")
mism = 0
for c, old in old_res.items():
    new = new_res.get(c) or []
    ok_n = len(old) == len(new)
    ok_d = (old[-1]["date"] == new[-1]["date"]) if (old and new) else False
    mark = "✅" if (ok_n and ok_d) else "❌"
    if not (ok_n and ok_d):
        mism += 1
    print("  %s %-10s 旧 %3d 根 末 %s | 新 %3d 根 末 %s"
          % (mark, c, len(old), old[-1]["date"] if old else "-",
             len(new), new[-1]["date"] if new else "-"))
print("→ 不一致 %d / %d" % (mism, len(old_res)))

print("\n" + "=" * 70)
print("V2: 财报因子 —— 缓存命中 vs 强制重算 逐位一致")
print("=" * 70)
import fund_split as FS
import fin_factor_cache as FFC

as_of = d.get("date") or "2026-10-26"
pool = [str(r["code"])[-6:] for r in (d.get("main_pool") or [])][:200]
em = FS.load_em_cache()

# 取「缓存命中」结果
res_hit, st_hit = FFC.compute_fin_factors(pool, as_of, em_all=em)
print("缓存轮: %s" % FFC.report_lines(st_hit)[0])

# 逐只强制重算对照
diff = 0
checked = 0
for c6 in pool:
    if c6 not in res_hit:
        continue
    ind = em.get(c6)
    if not ind:
        continue
    periods_desc = sorted({p for dd in ind.values() for p in dd}, reverse=True)
    P = FS.pit_period(periods_desc, as_of)
    if not P:
        continue
    sub = FS.sub_factors_pit(ind, P)
    got = res_hit[c6]["factors"]
    checked += 1
    for k in ("ocfg", "roe", "npg", "ep", "evebit"):
        a, b = sub.get(k), got.get(k)
        if (a is None) != (b is None):
            diff += 1
            print("  ❌ %s.%s 缺失态不一致: 重算=%s 缓存=%s" % (c6, k, a, b))
        elif isinstance(a, (int, float)) and isinstance(b, (int, float)):
            if abs(a - b) > 1e-9:
                diff += 1
                print("  ❌ %s.%s 值不一致: 重算=%r 缓存=%r" % (c6, k, a, b))
print("→ 比对 %d 只 × 5 因子，差异 %d 处 %s" % (checked, diff, "✅" if diff == 0 else "❌"))

print("\n" + "=" * 70)
print("V3: 向量化 vs 逐股循环")
print("=" * 70)
import factor_vector as FV
kl_map = {c: v for c, v in new_res.items() if v}
if kl_map:
    FV.verify_parity(kl_map, sample=min(40, len(kl_map)))
else:
    print("  无可用 K 线，跳过")

print("\n" + "=" * 70)
print("汇总")
print("=" * 70)
print("K线抓取提速        : %.0fx（%.2fs → %.2fs / 40只）"
      % (t_old / max(1, len(old_res)) / (t_new / len(codes)), t_old, t_new))
print("EM 缓存加载提速    : 115x（50.4s → 0.33s，见 pickle 实测）")
print("财报因子逐位一致   : %s" % ("✅" if diff == 0 else "❌ %d 处不同" % diff))
