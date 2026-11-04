#!/usr/bin/env python3
"""bench_async_qps.py —— 标定 QPS 上限：在「不触发 501/429」前提下找最快档。

对每档 QPS 用同一批股票实测，统计：耗时、成功率、状态码分布、黑名单。
纪律：任何一档出现 501/429 即判定该档越界，取上一档为安全上限。
"""
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import async_fetch as AF

d = json.load(open(os.path.join(HERE, "top500_all_a.json"), encoding="utf-8"))
codes = [r["code"] for r in d.get("main_pool") or []]
N = int(sys.argv[1]) if len(sys.argv) > 1 else 120
sample = codes[:N]
print("样本 %d 只 | aiohttp=%s" % (len(sample), AF.HAS_AIOHTTP))
print("=" * 74)
print("%-8s %-8s %-8s %-8s %-10s %s" % ("QPS", "并发", "耗时", "成功率", "速率", "状态码"))
print("-" * 74)

rows = []
for qps, conc in [(6, 10), (12, 10), (20, 12), (40, 16), (80, 20)]:
    # 预热一次，排除 DNS/TLS 首次握手
    AF.fetch_many_klines(sample[:2], concurrency=conc, qps=qps, progress_every=0)
    res, st = AF.fetch_many_klines(sample, concurrency=conc, qps=qps, progress_every=0)
    s = st.as_dict()
    bad = sum(v for k, v in s["by_status"].items() if str(k) in ("501", "429", "503", "500"))
    print("%-8d %-8d %-8.1f %-8.1f %-10.1f %s"
          % (qps, conc, s["secs"], 100.0 * s["ok"] / max(1, s["n"]), s["rate"],
             " ".join("%s×%d" % kv for kv in sorted(s["by_status"].items(), key=lambda x: str(x[0])))))
    rows.append({"qps": qps, "conc": conc, "secs": s["secs"], "ok": s["ok"],
                 "n": s["n"], "bad": bad, "rate": s["rate"],
                 "extrap490": round(s["secs"] / max(1, s["n"]) * 490, 1),
                 "extrap5377": round(s["secs"] / max(1, s["n"]) * 5377, 1)})
    time.sleep(2)

print("-" * 74)
print("%-8s %-8s %-10s %-12s %s" % ("QPS", "并发", "外推490只", "外推5377只", "判据"))
for r in rows:
    verdict = "❌ 越界（出现5xx/429）" if r["bad"] else "✅ 安全"
    print("%-8d %-8d %-10.1fs %-12.0fs %s" % (r["qps"], r["conc"], r["extrap490"],
                                              r["extrap5377"], verdict))
safe = [r for r in rows if r["bad"] == 0]
if safe:
    best = max(safe, key=lambda r: r["qps"])
    print("\n→ 安全档最高 QPS = %d（外推 490 只 %.1fs / 5377 只 %.0fs）"
          % (best["qps"], best["extrap490"], best["extrap5377"]))
json.dump(rows, open("/tmp/_qps_bench.json", "w"), ensure_ascii=False, indent=1)
