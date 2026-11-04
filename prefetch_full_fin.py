#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""prefetch_full_fin.py —— 抓全市场（sw_map 5218 只）财务摘要到独立缓存 data/fin_cache_full/

## 用途

#305（B 维分母升级）需要全市场同行业营收合计。本脚本补齐池外 3073 只的财务数据。

## 为什么不复用生产缓存

`data/fin_cache_em/` 是**生产缓存**（step2 的 quality/fund 维在读，有指纹机制 `fs._em_sig`）。
往里写全市场数据会改变指纹 → 触发 pkl 重建 → 可能影响生产路径的覆盖统计。
故独立目录 `data/fin_cache_full/`，只读消费，不参与任何生产链路。

## 用法
  python prefetch_full_fin.py                  # 抓全部池外股票
  python prefetch_full_fin.py --workers 6      # 并发数（默认 6）
  python prefetch_full_fin.py --limit 100      # 只抓前 N 只（调试用）

## 幂等
缓存文件存在且 mtime < TTL(7天) → 跳过。可断点续跑。
"""
import argparse
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import fund_split as fs

CACHE_FULL = os.path.join(HERE, "data", "fin_cache_full")
SW_MAP = os.path.join(HERE, "sector", "sw_map.json")
TTL_DAYS = 7


def fetch_one(code):
    os.makedirs(CACHE_FULL, exist_ok=True)
    p = os.path.join(CACHE_FULL, "%s.json" % code)
    if os.path.exists(p):
        try:
            if (time.time() - os.path.getmtime(p)) / 86400 < TTL_DAYS:
                return code, "cached"
        except Exception:
            pass
    for attempt in range(3):
        try:
            import akshare as ak
            df = ak.stock_financial_abstract(symbol=code)
            if df is not None and len(df) > 0:
                recs = []
                for r in df.to_dict("records"):
                    row = {}
                    for k, v in r.items():
                        key = str(k)
                        if len(key) == 8 and key.isdigit():
                            try:
                                row[key] = float(v)
                            except Exception:
                                row[key] = None
                        else:
                            row[key] = str(v) if v is not None else ""
                    recs.append(row)
                raw = {"sym": code,
                       "cached_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                       "records": recs}
                tmp = p + ".tmp"
                json.dump(raw, open(tmp, "w", encoding="utf-8"), ensure_ascii=False)
                os.replace(tmp, p)          # 原子写，避免半成品被读到
                return code, "ok"
            return code, "empty"
        except Exception as e:
            if attempt == 2:
                return code, "fail:%s" % type(e).__name__
            time.sleep(1.2 * (attempt + 1))
    return code, "fail"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()

    sw = json.load(open(SW_MAP, encoding="utf-8"))
    allc = set(sw["l2"].keys())
    em = fs.load_em_cache()
    pool = set(em.keys()) & allc
    out_pool = sorted(allc - pool)
    if args.limit:
        out_pool = out_pool[: args.limit]

    print(f"全市场 {len(allc)} | 池内(已有 EM) {len(pool)} | 待抓池外 {len(out_pool)} | 并发 {args.workers}")
    t0 = time.time()
    stat = {"ok": 0, "cached": 0, "empty": 0, "fail": 0}
    fails = []
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = {ex.submit(fetch_one, c): c for c in out_pool}
        done = 0
        for f in as_completed(futs):
            code, st = f.result()
            done += 1
            key = st.split(":")[0]
            stat[key] = stat.get(key, 0) + 1
            if key == "fail":
                fails.append((code, st))
            if done % 200 == 0 or done == len(out_pool):
                el = time.time() - t0
                eta = el / done * (len(out_pool) - done)
                print(f"   ... {done}/{len(out_pool)}  {el:.0f}s  剩约 {eta:.0f}s  {stat}")

    el = time.time() - t0
    print(f"\n完成：{el:.1f}s  {stat}")
    rate = stat["ok"] + stat["cached"]
    print(f"有效率 {(rate/len(out_pool)*100) if out_pool else 0:.1f}%")
    if fails[:10]:
        print(f"失败样例（前10）：{fails[:10]}")

    # 落一份清单，供 B 维分母重建时核对覆盖
    json.dump({"total": len(out_pool), "stat": stat, "seconds": round(el, 1),
               "fails": fails, "cache_dir": CACHE_FULL},
              open(os.path.join(HERE, "data", "fin_cache_full_manifest.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    print("📄 清单：data/fin_cache_full_manifest.json")


if __name__ == "__main__":
    main()
