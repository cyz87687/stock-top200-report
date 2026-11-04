#!/usr/bin/env python3
"""prefetch_em_fin.py —— 为新池预抓 EM 财务摘要缓存（data/fin_cache_em/）。

背景（2026-10-26）：选池改为分层抽样后，池内主体是微小盘股，而 fin_cache_em
只有历史遗留的 818 只（大盘为主），命中率骤降到 10.4% → quality 维覆盖塌到
52/500。step2 里虽有 on-demand 抓取，但 490 只串行抓会把 step2 拖到超时
（Actions job timeout 120min）。故独立 prefetch，可断点续跑。

用法：
  python prefetch_em_fin.py                     # 用 top500_all_a.json 的主池
  python prefetch_em_fin.py --pool <path>       # 指定池文件
  python prefetch_em_fin.py --workers 4         # 并发（默认 4，别开太大）
  python prefetch_em_fin.py --force             # 忽略 TTL，全量重抓

判据：缓存文件存在且 mtime 在 TTL 内 → 跳过（与 step2 的 fetch_em_growth_cached
完全同源，不另立口径）。
"""
import argparse
import glob
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import step2_stockscorer_v2 as S2


def _c6(x):
    s = str(x or "").strip().lower()
    for p in ("sh", "sz", "bj"):
        if s.startswith(p):
            s = s[len(p):]
    return s


def load_pool_codes(path):
    d = json.load(open(path, encoding="utf-8"))
    rows = d.get("main_pool") or d.get("top500") or []
    out, seen = [], set()
    for r in rows:
        c = _c6(r.get("code") or r.get("symbol") or r.get("ts_code") or "")
        if len(c) == 6 and c.isdigit() and c not in seen:
            seen.add(c)
            out.append(c)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pool", default=os.path.join(HERE, "top500_all_a.json"))
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--ttl", type=int, default=S2.EM_FIN_CACHE_TTL_DAYS)
    args = ap.parse_args()

    codes = load_pool_codes(args.pool)
    if not codes:
        print("❌ 池文件无有效代码：%s" % args.pool)
        return 2
    cache_dir = S2.EM_FIN_CACHE_DIR
    os.makedirs(cache_dir, exist_ok=True)

    todo, fresh = [], 0
    for c in codes:
        p = os.path.join(cache_dir, "%s.json" % c)
        if (not args.force) and os.path.exists(p):
            try:
                age = (time.time() - os.path.getmtime(p)) / 86400.0
                if age < args.ttl:
                    fresh += 1
                    continue
            except Exception:
                pass
        todo.append(c)

    print("📦 池 %d 只 | 缓存已新鲜 %d | 待抓 %d | TTL %d 天 | 并发 %d"
          % (len(codes), fresh, len(todo), args.ttl, args.workers))
    if not todo:
        print("✅ 无需抓取")
        return 0

    ok = fail = 0
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as ex:
        futs = {ex.submit(S2.fetch_em_growth_cached, c, 0 if args.force else args.ttl): c
                for c in todo}
        for i, fu in enumerate(as_completed(futs), 1):
            c = futs[fu]
            try:
                r = fu.result()
                flag = (r or {}).get("flag")
                if flag and flag != "no_data":
                    ok += 1
                else:
                    fail += 1
                    print("   ⚠️ %s 无数据(flag=%s)" % (c, flag))
            except Exception as e:
                fail += 1
                print("   ❌ %s 异常: %s" % (c, e))
            if i % 20 == 0 or i == len(todo):
                el = time.time() - t0
                print("   [%d/%d] ok=%d fail=%d  %.1fs (%.2f 只/s)"
                      % (i, len(todo), ok, fail, el, i / max(el, 1e-9)))

    # 复核
    have = 0
    for c in codes:
        p = os.path.join(cache_dir, "%s.json" % c)
        if os.path.exists(p) and os.path.getsize(p) > 2000:
            have += 1
    print("✅ 完成：池 %d 只中 %d 只有缓存（%.1f%%）"
          % (len(codes), have, 100.0 * have / len(codes)))
    return 0 if have >= len(codes) * 0.85 else 1


if __name__ == "__main__":
    sys.exit(main())
