#!/usr/bin/env python3
"""每日选股: 以板块动量为主轴, 从 TOP200 评分结果中构建组合

选股流程(两条并行路线):
  A. momentum 路线(用户原始思路)
     板块: 60日动量第1 ∩ 10日动量前3  → 无则放宽 R60<=3 ∩ R10<=5 → R60<=5 ∩ R10<=10 → R60<=10
     个股: 板块内按综合评分 total 降序, 剔除 RSI>70
  B. reversal 路线(回测证据)
     板块: 60日动量末3 ∩ 10日动量末10 → 放宽 R60>70 ∩ R10>70 → R60>40 ∩ R10>40 → R60>40
     个股: 板块内 RSI<30 优先, 其次按 total 降序; 剔除 RSI>70

共同约束: 每板块最多 --per-sector 只, 组合总规模 --top 只, 等权。
回测依据见 sector/backtest_ref.json(申万二级 124 个板块, 2021-01-04~2026-09-18, 1386 交易日)。

用法:
  python3 pick_portfolio.py [scored_json] [--top 10] [--per-sector 3] [--out sector/portfolio_<date>.html]
"""
import os
import sys
import json
import glob
import argparse
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
SECTOR_DIR = os.path.join(HERE, 'sector')
MAP_PATH = os.path.join(SECTOR_DIR, 'sw_map.json')
MOM_PATH = os.path.join(SECTOR_DIR, 'sector_momentum_latest.json')
BT_REF = os.path.join(SECTOR_DIR, 'backtest_ref.json')


def code6(code):
    s = ''.join(ch for ch in str(code) if ch.isdigit())
    return s[-6:].zfill(6) if len(s) >= 6 else None


def pick_sectors(rows, mode):
    """返回层级列表 [(名称, 判定函数)]，按严格→放宽排序"""
    n = len(rows)
    r60 = {r['sector']: r['r60'] for r in rows}
    r10 = {r['sector']: r['r10'] for r in rows}
    if mode == 'momentum':
        return [
            ('L0 严格: 60日动量第1 ∩ 10日动量前3', lambda s: r60[s] == 1 and r10[s] <= 3),
            ('L1 放宽: 60日动量前3 ∩ 10日前5', lambda s: r60[s] <= 3 and r10[s] <= 5),
            ('L2 再放宽: 60日前5 ∩ 10日前10', lambda s: r60[s] <= 5 and r10[s] <= 10),
            ('L3 兜底: 60日动量前10', lambda s: r60[s] <= 10),
            ('L4 最终兜底: 60日动量前20', lambda s: r60[s] <= 20),
        ]
    return [
        ('L0 严格: 60日动量末3 ∩ 10日动量末10', lambda s: r60[s] >= n - 2 and r10[s] >= n - 9),
        ('L1 放宽: 60日排名>70 ∩ 10日排名>70', lambda s: r60[s] > 70 and r10[s] > 70),
        ('L2 再放宽: 60日排名>40 ∩ 10日排名>40', lambda s: r60[s] > 40 and r10[s] > 40),
        ('L3 兜底: 60日排名>40', lambda s: r60[s] > 40),
        ('L4 最终兜底: 60日排名>20', lambda s: r60[s] > 20),
    ]


def select(rows, results, mode, min_cands):
    """逐级放宽直到「选中板块 ∩ TOP200 候选池」达到 min_cands 只"""
    r60 = {r['sector']: r['r60'] for r in rows}
    levels = pick_sectors(rows, mode)
    trace = []
    chosen = (set(), levels[-1][0], [], [])
    for name, fn in levels:
        sel = set(s for s in r60 if fn(s))
        cands = [r for r in results
                 if r.get('sector_l2') in sel
                 and not ((r.get('tech') or {}).get('rsi') or 0) > 70]
        trace.append({'level': name, 'n_sectors': len(sel), 'n_cands': len(cands),
                      'sectors': sorted(sel)[:12]})
        if len(cands) >= min_cands:
            chosen = (sel, name, cands, trace)
            break
        if chosen[0] == set() or len(cands) > len(chosen[2]):
            chosen = (sel, name, cands, trace)
    return chosen


def build(cands, mode, per_sector, top):
    if mode == 'reversal':
        # 回测支持: 弱势板块内 RSI<30 优先
        cands.sort(key=lambda r: (0 if (r.get('tech') or {}).get('rsi') is not None and (r.get('tech') or {}).get('rsi') < 30 else 1,
                                  -float(r.get('total') or 0)))
    else:
        cands.sort(key=lambda r: -float(r.get('total') or 0))
    per_cnt, picked = {}, []
    for r in cands:
        s = r.get('sector_l2')
        if per_cnt.get(s, 0) >= per_sector:
            continue
        per_cnt[s] = per_cnt.get(s, 0) + 1
        picked.append(r)
        if len(picked) >= top:
            break
    return picked


def fmt_pct(x):
    if x is None:
        return '—'
    return f"{x:+.2f}%"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('scored', nargs='?', default=None)
    ap.add_argument('--top', type=int, default=10)
    ap.add_argument('--per-sector', type=int, default=3)
    ap.add_argument('--out', default=None)
    args = ap.parse_args()

    scored = args.scored
    if scored is None:
        fs = sorted(glob.glob(os.path.join(HERE, 'top200_scored_*.json')), reverse=True)
        scored = fs[0] if fs else None
    if not scored or not os.path.exists(scored):
        print("❌ 未找到 scored json")
        return 1
    mom = json.load(open(MOM_PATH, encoding='utf-8'))
    data = json.load(open(scored, encoding='utf-8'))
    results = data.get('results', [])
    rows = mom['sectors']
    mdate = mom.get('date')
    sdate = data.get('date')
    n = len(rows)
    print(f"scored 日期 {sdate} | 板块动量日期 {mdate} | 板块数 {n} | 候选池 {len(results)} 只")

    out = {'date': sdate, 'momentum_date': mdate, 'top': args.top, 'per_sector': args.per_sector, 'portfolios': {}}
    min_cands = max(5, args.top // 2)
    for mode in ('momentum', 'reversal'):
        levels = pick_sectors(rows, mode)
        secs, level, cands, trace = select(rows, results, mode, min_cands)
        picked = build(cands, mode, args.per_sector, args.top)
        out['portfolios'][mode] = {
            'level_used': level, 'target_rule': levels[0][0], 'level_trace': trace,
            'sectors': sorted(secs), 'n_candidates': len(cands),
            'picks': [{'name': r['name'], 'code': r['code'], 'sector_l2': r.get('sector_l2'),
                       'parent': r.get('sector_parent', ''), 'total': r['total'], 'rating': r['rating'],
                       'price': r.get('price'), 'pct_chg': r.get('pct_chg'),
                       'rsi': (r.get('tech') or {}).get('rsi'), 'trend': (r.get('tech') or {}).get('trend'),
                       'sector_r60': r.get('sector_r60'), 'sector_r10': r.get('sector_r10'),
                       'sector_mom60': r.get('sector_mom60'), 'sector_mom10': r.get('sector_mom10'),
                       'score_sector': r.get('score_sector'),
                       'score_news': r.get('score_news'), 'score_tech': r.get('score_tech'),
                       'score_fund': r.get('score_fund'), 'score_theme': r.get('score_theme')}
                      for r in picked],
        }
        print(f"\n=== {mode} 路线 | {level} ===")
        for t in trace:
            print(f"   [{t['level']:<34}] 板块{t['n_sectors']:>3} 候选{t['n_cands']:>3}")
        print(f"  选中板块({len(secs)}): {', '.join(sorted(secs)[:12])}{'...' if len(secs) > 12 else ''}  候选 {len(cands)} 只")
        for i, r in enumerate(picked, 1):
            t = r.get('tech') or {}
            print(f"  {i:>2}. {r['name']:<6}{r['code']} {str(r.get('sector_l2')):<8} "
                  f"总分{r['total']:>5.1f}({r['rating']}) RSI{t.get('rsi') if t.get('rsi') is not None else 0:>5.1f} "
                  f"{str(t.get('trend')):<6} 板块R60={r.get('sector_r60')} R10={r.get('sector_r10')}")

    op = args.out or os.path.join(SECTOR_DIR, f"portfolio_{sdate}.json")
    with open(op, 'w', encoding='utf-8') as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print(f"\n✅ 组合已写入 {op}")
    return 0


if __name__ == '__main__':
    sys.exit(main())
