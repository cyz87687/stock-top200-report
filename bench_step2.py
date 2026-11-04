#!/usr/bin/env python3
"""bench_step2.py —— 定位 step2 耗时来源（基准测试，不改数据）。

测四段：
  B1 EM 财务缓存 I/O：load_em_cache / load_em_all 各读多少文件、多久
  B2 财务子因子纯计算：fund_split 490 只 family_scores 多久
  B3 行情因子：低波20 / rank_pct / 行业内 Z 多久
  B4 K线抓取：串行 vs 并发（腾讯 501 后走新浪）
"""
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

scored = sys.argv[1] if len(sys.argv) > 1 else os.path.join(HERE, "top200_scored_2026-10-23.json")
d = json.load(open(scored, encoding="utf-8"))
rows = d.get("results") or []
codes = [str(r.get("code") or "")[-6:] for r in rows]
as_of = d.get("date") or "2026-10-23"
print("样本：%d 只 | as_of=%s" % (len(codes), as_of))

print("\n=== B1: EM 财务缓存 I/O ===")
import fund_split as FS
t0 = time.time()
em17 = FS.load_em_cache()
t1 = time.time()
print("  fund_split.load_em_cache (17指标白名单): %.2fs → %d 只" % (t1 - t0, len(em17)))

import ep_size_ind as ESI
t0 = time.time()
em_all = ESI.load_em_all()
t1 = time.time()
print("  ep_size_ind.load_em_all  (全70指标):     %.2fs → %d 只" % (t1 - t0, len(em_all)))

import glob
nfiles = len(glob.glob(os.path.join(HERE, "data", "fin_cache_em", "*.json")))
print("  缓存文件数: %d | 合计 %.1f MB"
      % (nfiles, sum(os.path.getsize(f) for f in glob.glob(os.path.join(HERE, "data", "fin_cache_em", "*.json"))) / 1e6))

print("\n=== B2: 财务子因子纯计算（不含 I/O）===")
t0 = time.time()
nq, ng = FS.score_rows([{"code": c} for c in codes], as_of, em=em17)
t1 = time.time()
print("  fund_split.score_rows 490 只: %.2fs（quality %d / growth %d）" % (t1 - t0, nq, ng))
print("  → 单只 %.1f ms  | 外推 5000 只 %.1fs" % ((t1 - t0) / max(1, len(codes)) * 1000,
                                                (t1 - t0) / max(1, len(codes)) * 5000))

t0 = time.time()
esi_rows = [{"code": c, "price": (rows[i].get("price") or 10.0),
             "circ_mv": (rows[i].get("circ_mv") or 1e9)} for i, c in enumerate(codes)]
n_ep, n_szi, drop = ESI.score_rows(esi_rows, as_of, em=em_all)
t1 = time.time()
print("  ep_size_ind.score_rows 490 只: %.2fs（EP %d / SZI %d）" % (t1 - t0, n_ep, n_szi))

print("\n=== B3: 行情因子 ===")
import step2_stockscorer_v2 as S2
# 造 490 只 × 60 根 K 线的合成数据（纯 CPU 部分）
import random
random.seed(42)
fake = {}
for c in codes[:490]:
    px = 10.0
    kl = []
    for i in range(70):
        px *= (1 + random.gauss(0, 0.02))
        kl.append({"date": "2026-%02d-%02d" % (1 + i // 28, 1 + i % 28),
                   "open": px, "close": px, "high": px * 1.01, "low": px * 0.99,
                   "vol": 1e6, "amount": px * 1e6})
    fake[c] = kl

t0 = time.time()
lv = {c: S2.raw_lowvol20(fake[c]) for c in fake}
lv = {k: v for k, v in lv.items() if v is not None}
t1 = time.time()
print("  raw_lowvol20 490 只（Python 循环）: %.3fs" % (t1 - t0))

t0 = time.time()
rk = S2.score_lowvol20_rank(lv)
t1 = time.time()
print("  score_lowvol20_rank 490 只: %.3fs" % (t1 - t0))

t0 = time.time()
for c in list(fake)[:100]:
    S2.rsi([k["close"] for k in fake[c]], 14)
t1 = time.time()
print("  rsi 100 只（Python）: %.3fs → 490 只约 %.2fs" % (t1 - t0, (t1 - t0) * 4.9))

print("\n=== B4: polars 对照 ===")
try:
    import polars as pl
    t0 = time.time()
    df = pl.DataFrame({"code": list(fake.keys()),
                       "close": [fake[c][-1]["close"] for c in fake]})
    # 低波20 向量化：需要宽表
    closes = {}
    for c, kl in fake.items():
        closes[c] = [k["close"] for k in kl[-21:]]
    rows_ = []
    for c, cs in closes.items():
        for i, v in enumerate(cs):
            rows_.append({"code": c, "i": i, "close": v})
    wide = pl.DataFrame(rows_)
    vol = (wide.sort(["code", "i"])
               .with_columns((pl.col("close") / pl.col("close").shift(1).over("code") - 1).alias("ret"))
               .group_by("code")
               .agg(pl.col("ret").std().alias("sd")))
    t1 = time.time()
    print("  polars 低波20 全池: %.3fs（%d 只，vs Python %.3fs）" % (t1 - t0, vol.height, 0))
except ImportError:
    print("  polars 未安装")
