#!/usr/bin/env python3
"""回测数据准备: 拉取候选池前复权日线 (TOP200 vs TOP500 池扩大对照实验)

候选池 = factor_history.json 快照并集(709只, 历史TOP200出现过)
        ∪ top1000_all_a.json stocks(1000只, 当日TOP1000, 含200~1000名)
        → 去重后约 1400 只

输出 data/universe_klines.json:
  {code: {date: [open, close, high, low, volume]}}  前复权日线(腾讯, 多域名轮询+新浪兜底)

数据纪律: 全部真实接口; 失败股票记入 data/universe_klines_failed.json 重试; 不编造。

用法: python3 fetch_universe_klines.py [--need 95]
"""
import os
import sys
import json
import time
import argparse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, 'data', 'universe_klines.json')
FAILED = os.path.join(HERE, 'data', 'universe_klines_failed.json')

for _k in ('HTTP_PROXY', 'HTTPS_PROXY', 'http_proxy', 'https_proxy', 'ALL_PROXY', 'all_proxy'):
    os.environ.pop(_k, None)

TENCENT_HOSTS = ["https://ifzq.gtimg.cn", "https://web.ifzq.gtimg.cn", "https://proxy.finance.qq.com/ifzqgtimg"]
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"


def _sym(code):
    """归一化为腾讯K线符号 (sh600519 / sz300450 / bj430047)"""
    c = str(code).strip().lower()
    if len(c) > 2 and c[:2] in ("sh", "sz", "bj") and c[2:].isdigit():
        return c
    c = str(code).strip().zfill(6)
    if c.startswith(('60', '68', '9', '51', '58')):
        return 'sh' + c
    if c.startswith(('00', '30', '20', '1', '15', '16')):
        return 'sz' + c
    if c.startswith(('8', '4', '92')):
        return 'bj' + c
    return None


def fetch_tencent(sym, need):
    """腾讯前复权日线 → {date: [o,c,h,l,v]} (多域名轮询)"""
    for host in TENCENT_HOSTS:
        url = f"{host}/appstock/app/fqkline/get?param={sym},day,,,{need},qfq"
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=15) as r:
                j = json.loads(r.read().decode('utf-8'))
            data = j.get('data', {}).get(sym, {})
            k = data.get('qfqday') or data.get('day') or []
            if not k:
                continue
            out = {}
            for row in k:
                # [date, open, close, high, low, volume, ...]
                out[row[0]] = [float(row[1]), float(row[2]), float(row[3]),
                               float(row[4]), float(row[5])]
            return out
        except Exception:
            continue
    return {}


def fetch_sina(sym, need):
    """新浪日线兜底(不复权)"""
    url = (f"https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/"
           f"CN_MarketData.getKLineData?symbol={sym}&scale=240&ma=no&datalen={need}")
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=15) as r:
            txt = r.read().decode('utf-8')
        j = json.loads(txt)
        out = {}
        for row in j:
            out[row['day']] = [float(row['open']), float(row['close']),
                               float(row['high']), float(row['low']), float(row['volume'])]
        return out
    except Exception:
        return {}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--need', type=int, default=95, help='K线根数(回测76截面+60日动量预热)')
    args = ap.parse_args()

    # --- 候选池 ---
    codes = set()
    fh = os.path.join(HERE, 'factor_history.json')
    if os.path.exists(fh):
        d = json.load(open(fh, encoding='utf-8'))
        for s in d.get('snapshots', []):
            for r in s['rows']:
                codes.add(r['code'])
    t1000 = os.path.join(HERE, 'top1000_all_a.json')
    if os.path.exists(t1000):
        d = json.load(open(t1000, encoding='utf-8'))
        for r in d.get('stocks', []):
            codes.add(r['code'])
    codes = sorted(codes)
    print(f"候选池: {len(codes)} 只 (factor_history ∪ top1000)")

    # --- 断点续拉 ---
    klines = {}
    if os.path.exists(OUT):
        klines = json.load(open(OUT, encoding='utf-8'))
        print(f"已有缓存: {len(klines)} 只, 续拉剩余")
    todo = [c for c in codes if c not in klines]
    print(f"待拉取: {len(todo)} 只 × {args.need} 根K线")

    failed = []
    t0 = time.time()
    for i, code in enumerate(todo, 1):
        sym = _sym(code)
        if not sym:
            failed.append(code)
            continue
        k = fetch_tencent(sym, args.need)
        if not k:
            k = fetch_sina(sym, args.need)
        if k:
            klines[code] = k
        else:
            failed.append(code)
        if i % 50 == 0 or i == len(todo):
            os.makedirs(os.path.dirname(OUT), exist_ok=True)
            json.dump(klines, open(OUT, 'w', encoding='utf-8'))
            json.dump(failed, open(FAILED, 'w', encoding='utf-8'))
            dt = time.time() - t0
            print(f"  [{i}/{len(todo)}] 成功 {len(klines)} 失败 {len(failed)} 耗时 {dt:.0f}s")
            time.sleep(0.5)  # 限速

    json.dump(klines, open(OUT, 'w', encoding='utf-8'))
    json.dump(failed, open(FAILED, 'w', encoding='utf-8'))
    print(f"✅ 完成: {len(klines)} 只成功, {len(failed)} 只失败 → {OUT}")


if __name__ == '__main__':
    sys.exit(main())
