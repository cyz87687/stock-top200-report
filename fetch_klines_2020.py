"""fetch_klines_2020.py —— 轻量拉取器：直接从代码清单拉 2020 起 qfq 日线。

与 build_long_history_2020.py 的分工：
    build_long_history_2020.py  负责 universe 发现 + npz 组装（重、少跑）
    本脚本                       只负责按给定清单拉取写缓存（轻、可分批/可循环）

为什么单独写一个：
    实测本机对长任务的资源限制很紧（--limit 50 即 Exit 137）。
    build 脚本每次启动都要先拉 universe（9s）+ import akshare（3s），
    而单批只能跑 20 只（12s）→ 有效工作时间不足 1/3，且极易被回收。
    本脚本直接读清单，无 universe 开销，单批吞吐高得多。

用法：
    python fetch_klines_2020.py --codes data/domain_2029.json
    python fetch_klines_2020.py --codes data/domain_2029.json --limit 100
    python fetch_klines_2020.py --codes data/domain_2029.json --skip-cached
"""
import argparse
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")
CACHE_DIR = os.path.join(DATA, "klines_2020_cache")
START = "20200101"
END = "20261104"


def fetch_one(code):
    """拉单只 qfq 日线，写缓存。返回 (code, status) status∈{ok,empty,fail,cached}。"""
    os.makedirs(CACHE_DIR, exist_ok=True)
    cp = os.path.join(CACHE_DIR, f"{code}.json")
    if os.path.exists(cp):
        try:
            with open(cp, encoding="utf-8") as f:
                blob = json.load(f)
            if blob.get("start") == START and blob.get("end") == END:
                return code, "cached"
        except Exception:
            pass
    try:
        import akshare as ak
        df = ak.stock_zh_a_hist(symbol=code, period="daily",
                                start_date=START, end_date=END, adjust="qfq")
        if df is None or not len(df):
            return code, "empty"
        rows = []
        for _, r in df.iterrows():
            try:
                rows.append([str(r["日期"])[:10],
                             float(r["开盘"]), float(r["收盘"]),
                             float(r["最高"]), float(r["最低"]),
                             float(r["成交量"]), float(r["成交额"]),
                             float(r["换手率"])])
            except (TypeError, ValueError):
                continue
        if not rows:
            return code, "empty"
        blob = {"code": code, "start": START, "end": END,
                "fetched_at": time.strftime("%Y-%m-%d %H:%M:%S"), "rows": rows}
        tmp = cp + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(blob, f, ensure_ascii=False)
        os.replace(tmp, cp)
        del blob, rows, df
        return code, "ok"
    except Exception:
        return code, "fail"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--codes", required=True, help="json 数组文件，或每行一个代码")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--offset", type=int, default=0)
    ap.add_argument("--workers", type=int, default=3)
    args = ap.parse_args()

    with open(args.codes, encoding="utf-8") as f:
        raw = f.read().strip()
    if raw.startswith("["):
        codes = json.loads(raw)
    else:
        codes = [x.strip() for x in raw.splitlines() if x.strip()]
    codes = [str(c).zfill(6) for c in codes]
    if args.offset:
        codes = codes[args.offset:]
    if args.limit:
        codes = codes[:args.limit]

    t0 = time.time()
    stat = {"ok": 0, "empty": 0, "fail": 0, "cached": 0}
    done = 0
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = [ex.submit(fetch_one, c) for c in codes]
        for fu in as_completed(futs):
            _c, st = fu.result()
            stat[st] = stat.get(st, 0) + 1
            done += 1

    el = time.time() - t0
    print(f"批次完成 {done} 只  耗时 {el:.0f}s ({el/max(1,done):.2f}s/只)")
    print(f"  ok={stat['ok']} cached={stat['cached']} "
          f"empty={stat['empty']} fail={stat['fail']}")
    n = len([x for x in os.listdir(CACHE_DIR) if x.endswith('.json')])
    print(f"  缓存累计 {n} 只")
    return 0


if __name__ == "__main__":
    sys.exit(main())
