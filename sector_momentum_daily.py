#!/usr/bin/env python3
"""每日板块动量计算(申万二级行业, 自建等权指数)

流程:
  1) 读取 sector/sw_map.json (个股 → 申万二级行业映射, 由 fetch_sw_map.py 更新)
  2) 并发抓取全市场近 NEED_BARS 根前复权日线(腾讯 fqkline)
  3) 按行业等权合成板块指数, 计算 mom60 / mom10 及横截面排名 r60 / r10
  4) 输出 sector/sector_momentum_<date>.json

用法: python3 sector_momentum_daily.py [--date YYYY-MM-DD] [--refresh-map]
前置: 代理环境变量会被清除(腾讯接口需直连)
"""
import os
import sys
import json
import time
import argparse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

for k in ['HTTP_PROXY', 'HTTPS_PROXY', 'http_proxy', 'https_proxy', 'ALL_PROXY', 'all_proxy']:
    os.environ.pop(k, None)

HERE = os.path.dirname(os.path.abspath(__file__))
SECTOR_DIR = os.path.join(HERE, 'sector')
MAP_PATH = os.path.join(SECTOR_DIR, 'sw_map.json')
MOM_LONG, MOM_SHORT = 60, 10
NEED_BARS = 90          # 抓取根数(>61 以覆盖停牌)
MIN_MEMBERS = 5
MIN_VALID_ABS = 5
MIN_VALID_RATIO = 0.3
MIN_SECTORS = 20
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"


def to_symbol(code6):
    c = str(code6).zfill(6)
    if c.startswith(('60', '68', '9', '51', '58')):
        return 'sh' + c
    if c.startswith(('00', '30', '20', '1', '15', '16')):
        return 'sz' + c
    if c.startswith(('8', '4', '92')):
        return 'bj' + c
    return None


def fetch_one(code6):
    sym = to_symbol(code6)
    if not sym:
        return code6, None
    url = (f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={sym},day,,,{NEED_BARS},qfq")
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    for _ in range(3):
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                j = json.loads(r.read().decode('utf-8'))
            d = j.get('data', {}).get(sym, {})
            kl = d.get('qfqday') or d.get('day') or []
            if not kl:
                return code6, None
            return code6, [(x[0], float(x[2])) for x in kl]   # (date, close)
        except Exception:
            time.sleep(0.6)
    return code6, None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--date', default=None, help='数据日期(默认取行情最新日期)')
    ap.add_argument('--workers', type=int, default=16)
    args = ap.parse_args()

    if not os.path.exists(MAP_PATH):
        print(f"❌ 缺少行业映射 {MAP_PATH}，请先运行 fetch_sw_map.py")
        return 1
    sw = json.load(open(MAP_PATH, encoding='utf-8'))
    members = sw['l2_members']
    code2l2 = sw['l2']
    codes = sorted(set(code2l2.keys()))
    print(f"映射行业 {len(members)} 个 / 个股 {len(codes)} 只, 抓取近 {NEED_BARS} 根日线...", flush=True)

    t0 = time.time()
    data = {}
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = {ex.submit(fetch_one, c): c for c in codes}
        done = 0
        for f in as_completed(futs):
            c, kl = f.result()
            if kl and len(kl) >= MOM_LONG + 2:
                data[c] = kl
            done += 1
            if done % 500 == 0:
                print(f"  {done}/{len(codes)}  {time.time()-t0:.0f}s", flush=True)
    print(f"  抓取完成: {len(data)}/{len(codes)} 只有效, 耗时 {time.time()-t0:.0f}s", flush=True)

    # 日期轴: 用出现频次最高的日期序列
    from collections import Counter
    cnt = Counter()
    for kl in data.values():
        for d, _ in kl:
            cnt[d] += 1
    all_dates = sorted([d for d, n in cnt.items() if n >= max(20, len(data) * 0.2)])
    if len(all_dates) < MOM_LONG + 2:
        print("❌ 交易日序列不足")
        return 1
    print(f"  交易日轴: {all_dates[0]} ~ {all_dates[-1]} ({len(all_dates)} 天)", flush=True)

    # 收益率矩阵: code → {date: ret}
    ret = {}
    for c, kl in data.items():
        ser = dict(kl)
        vals = [ser.get(all_dates[i]) for i in range(len(all_dates))]
        rr = {}
        for i in range(1, len(all_dates)):
            a, b = vals[i - 1], vals[i]
            if a and b and a > 0 and b > 0:
                r = b / a - 1
                rr[all_dates[i]] = r if abs(r) < 0.5 else None
            else:
                rr[all_dates[i]] = None
        ret[c] = rr

    # 板块等权指数(累计净值)
    idx = {}
    for b, codes_b in members.items():
        cs = [c for c in codes_b if c in ret]
        if len(cs) < MIN_MEMBERS:
            continue
        nav, cur = {}, 1.0
        for i, d in enumerate(all_dates):
            if i == 0:
                nav[d] = 1.0
                continue
            rs = [ret[c].get(d) for c in cs]
            rs = [x for x in rs if x is not None]
            if len(rs) < MIN_VALID_ABS or len(rs) < MIN_VALID_RATIO * len(cs):
                nav[d] = cur
                continue
            cur *= (1 + sum(rs) / len(rs))
            nav[d] = cur
        idx[b] = (nav, len(cs))

    last = args.date or all_dates[-1]
    if last not in all_dates:
        last = all_dates[-1]
    li = all_dates.index(last)
    if li < MOM_LONG:
        print(f"❌ 日期 {last} 历史不足 {MOM_LONG} 日")
        return 1

    rows = []
    for b, (nav, n) in idx.items():
        v_now, v_60 = nav.get(last), nav.get(all_dates[li - MOM_LONG])
        v_10 = nav.get(all_dates[li - MOM_SHORT])
        if not v_now or not v_60 or not v_10:
            continue
        rows.append({'sector': b, 'parent': sw.get('l2_parent', {}).get(b, ''),
                     'mom60': round((v_now / v_60 - 1) * 100, 2),
                     'mom10': round((v_now / v_10 - 1) * 100, 2),
                     'n_members': n})
    if len(rows) < MIN_SECTORS:
        print(f"❌ 有效板块不足 ({len(rows)})")
        return 1
    rows.sort(key=lambda x: -x['mom60'])
    for i, r in enumerate(rows, 1):
        r['r60'] = i
    for i, r in enumerate(sorted(rows, key=lambda x: -x['mom10']), 1):
        r['r10'] = i

    stock_sector = {}
    for c in data:
        s = code2l2.get(c)
        if s:
            stock_sector[c] = s

    out = {'date': last, 'mom_long_days': MOM_LONG, 'mom_short_days': MOM_SHORT,
           'n_sectors': len(rows), 'n_stocks': len(data),
           'index_type': 'equal_weight_sector_index(self-built, qfq)',
           'sectors': rows, 'stock_sector': stock_sector}
    op = os.path.join(SECTOR_DIR, f"sector_momentum_{last}.json")
    with open(op, 'w', encoding='utf-8') as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    latest = os.path.join(SECTOR_DIR, "sector_momentum_latest.json")
    with open(latest, 'w', encoding='utf-8') as f:
        json.dump(out, f, ensure_ascii=False, indent=1)

    print(f"\n✅ 写入 {op}")
    print(f"=== {last} 板块60日动量 Top6 / Bottom3 ===")
    for r in rows[:6]:
        print(f"  {r['sector']:<10}({r['parent']:<6}) 60日{r['mom60']:+7.2f}% 10日{r['mom10']:+7.2f}% R60={r['r60']} R10={r['r10']}")
    print("  ...")
    for r in rows[-3:]:
        print(f"  {r['sector']:<10}({r['parent']:<6}) 60日{r['mom60']:+7.2f}% 10日{r['mom10']:+7.2f}% R60={r['r60']} R10={r['r10']}")
    return 0


if __name__ == '__main__':
    sys.exit(main())
