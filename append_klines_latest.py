#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""append_klines_latest.py —— 给长价格库补最新交易日（增量，不重抓历史）

背景：data/klines_long.json 由 fetch_klines_long.py 维护，但那是**全量 35 分钟**
的重抓。日常只需把最新 1~N 个交易日追加进去，历史不动。

本脚本只做增量合并：
  1. 读现有 data/klines_long.json
  2. 对每个代码只拉最近 need 根（默认 8），**仅追加库中不存在的日期**
  3. 已有日期一律不覆盖（防止前复权因子变化导致历史被静默改写）
     ⚠️ 前复权价的固有性质：除权后历史价格会整体平移。本脚本**故意不重写历史**
        以保住已归档面板的可复现性；如需重算，显式用 --rebuild。

用法
  python append_klines_latest.py                 # 追加最新交易日
  python append_klines_latest.py --dry-run       # 只看将追加多少
  python append_klines_latest.py --need 30       # 多拉几根（补几天缺口）
"""
import os
import sys
import json
import time
import argparse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
os.chdir(HERE)

KLONG = os.path.join(HERE, "data", "klines_long.json")
TQ_HOSTS = ["https://ifzq.gtimg.cn", "https://web.ifzq.gtimg.cn",
            "https://proxy.finance.qq.com/ifzqgtimg"]


def fetch_ohlcv(sym, need=8):
    for _host in TQ_HOSTS:
        url = f"{_host}/appstock/app/fqkline/get?param={sym},day,,,{need},qfq"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        try:
            with urllib.request.urlopen(req, timeout=12) as r:
                txt = r.read().decode("utf-8")
        except Exception:
            continue
        if txt.lstrip().startswith("<"):
            continue
        try:
            data = json.loads(txt)
            kdata = data.get("data", {}).get(sym, {}) or {}
            raw = kdata.get("qfqday") or kdata.get("day")
        except Exception:
            continue
        if not raw:
            continue
        out = []
        for k in raw:
            try:
                out.append((str(k[0])[:10], float(k[1]), float(k[2]),
                            float(k[3]), float(k[4]), float(k[5])))
            except Exception:
                continue
        if out:
            return out
    return None


def sym_of(code6):
    if code6.startswith(("60", "68", "51", "58", "11", "50")):
        return "sh" + code6
    if code6.startswith(("00", "30", "12", "15", "16")):
        return "sz" + code6
    if code6.startswith(("8", "4", "92")):
        return "bj" + code6
    return "sh" + code6


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--need", type=int, default=8)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()

    print("=" * 96)
    print("append_klines_latest · 增量补最新交易日")
    print("=" * 96)

    raw = json.load(open(KLONG, encoding="utf-8"))
    codes = sorted({str(c)[-6:] for c in raw if str(c)[-6:].isdigit()})
    if args.limit:
        codes = codes[:args.limit]
    print(f"价格库：{len(raw)} 键 → {len(codes)} 个 6 位代码")

    # 现有最新日期
    alld = set()
    for c, m in raw.items():
        alld.update(m.keys())
    dmax = max(alld) if alld else None
    print(f"库内最新日期：{dmax}，总日期数 {len(alld)}")

    added_dates = set()
    n_new = 0
    n_fail = 0
    n_codes_touched = 0
    t0 = time.time()
    for i, c6 in enumerate(codes):
        sym = sym_of(c6)
        bars = fetch_ohlcv(sym, args.need)
        if not bars:
            n_fail += 1
            continue
        # 找对应键（可能是 6 位或带前缀）
        key = None
        if c6 in raw:
            key = c6
        else:
            for k in raw:
                if str(k)[-6:] == c6:
                    key = k
                    break
        if key is None:
            raw[c6] = {}
            key = c6
        m = raw[key]
        touched = False
        for bar in bars:
            d, o, cl, hi, lo, vol = bar
            if d in m:
                continue                     # 已有日期绝不覆盖
            m[d] = [o, cl, hi, lo, vol]
            added_dates.add(d)
            n_new += 1
            touched = True
        if touched:
            n_codes_touched += 1
        if (i + 1) % 200 == 0:
            el = time.time() - t0
            print(f"   … {i+1}/{len(codes)}  新增 {n_new} 点  失败 {n_fail}  "
                  f"用时 {el:.0f}s", flush=True)

    print()
    print(f"完成：{n_codes_touched} 只被追加，新增 {n_new} 个价格点，失败 {n_fail} 只")
    if added_dates:
        print(f"新增日期：{sorted(added_dates)}")
    else:
        print("无新增日期（库已是最新）")

    if args.dry_run:
        print("--dry-run：不写文件")
        return
    if n_new == 0:
        print("无变化，跳过写盘")
        return
    with open(KLONG, "w", encoding="utf-8") as f:
        json.dump(raw, f, ensure_ascii=False)
    print(f"✅ 已写回 {KLONG} ({os.path.getsize(KLONG)/1e6:.2f} MB)")
    print("=" * 96)


if __name__ == "__main__":
    main()
