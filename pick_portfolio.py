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
CONFIG_PATH = os.path.join(HERE, 'config.json')

# config.json 缺省值（与 fetch_top200_sina.py 保持同一套约定）
_DEF_CFG = {
    'route_pool_map': {'reversal': 500, 'momentum': 200},
    'momentum_route_weight': 0.5,
    'liquidity_filter': {'enabled': True, 'min_avg_amount_20d': 5e7, 'min_avg_turnover_rate': 0.01},
    'industry_limits': {'max_single_stock': 0.08, 'max_l2_sector': 0.25,
                        'max_l1_parent': 0.35, 'max_l1_deviation_pp': 8},
}


def load_config():
    """读 config.json；缺失/损坏则回退内置缺省（保证向后可用）。"""
    cfg = json.loads(json.dumps(_DEF_CFG))   # deep copy
    if os.path.exists(CONFIG_PATH):
        try:
            user = json.load(open(CONFIG_PATH, encoding='utf-8'))
            for k in cfg:
                if isinstance(cfg[k], dict) and isinstance(user.get(k), dict):
                    cfg[k].update(user[k])
                elif k in user:
                    cfg[k] = user[k]
        except Exception as e:
            print(f"  ⚠️ config.json 解析失败({str(e)[:40]}), 使用缺省")
    return cfg


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


def build(cands, mode, per_sector, per_parent, top, min_total=12.0, cfg=None,
          no_l1_cap=False):
    """构建组合。约束：
      - min_total  : 综合评分低于该值的个股不入选(默认12分, 只推 B 级及以上)
      - per_sector : 同一申万二级板块最多 N 只
      - per_parent : 同一申万一级行业(parent)最多 N 只 —— 防行业集中(如多只银行股)
      - cfg.industry_limits: 以「持仓占比」表达的更细上限(单票/二级/一级),
        与 per_* 的「只数上限」叠加取更严者(等权下占比≈只数/top)
      - v3.2 P0-2: 申万一级行业**暴露偏离**上限(相对等权基准 ≤ max_l1_deviation_pp 个百分点)。
        只数约束 ≠ 暴露约束 —— TOP500 行业结构偏中小盘后, 固定只数在不同行业
        不再等价; 反转信号具板块级聚集性, 易退化为行业轮动豪赌。此处按真实
        「已选权重占比 - 等权基准占比」检查, 超限行业按评分顺延替补。
        no_l1_cap=True 时关闭(对照用)。
    """
    cfg = cfg or _DEF_CFG
    lim = cfg.get('industry_limits', {})
    # 等权假设下的「只数上限」换算: 以占比上限反推每板块/每行业允许的只数
    eff_per_sector = per_sector
    eff_per_parent = per_parent
    if top > 0:
        if lim.get('max_l2_sector'):
            eff_per_sector = min(per_sector, max(2, int(top * float(lim['max_l2_sector']) + 0.999)))
        if lim.get('max_l1_parent'):
            eff_per_parent = min(per_parent, max(2, int(top * float(lim['max_l1_parent']) + 0.999)))
    dev_pp = float(lim.get('max_l1_deviation_pp', 0) or 0)

    cands = [r for r in cands if float(r.get('total') or 0) >= min_total]
    if mode == 'reversal':
        # 回测支持: 弱势板块内 RSI<30 优先
        cands.sort(key=lambda r: (0 if (r.get('tech') or {}).get('rsi') is not None and (r.get('tech') or {}).get('rsi') < 30 else 1,
                                  -float(r.get('total') or 0)))
    else:
        cands.sort(key=lambda r: -float(r.get('total') or 0))

    # v3.2 P0-2: 等权基准的申万一级行业占比（候选池内分布，代表「行业中性」参照）
    _par_total = len(cands) or 1
    _base_share = {}
    for r in cands:
        _p = r.get('sector_parent') or r.get('sector_l2') or '未分类'
        _base_share[_p] = _base_share.get(_p, 0) + 1.0
    _base_share = {k: v / _par_total for k, v in _base_share.items()}

    per_cnt, par_cnt, picked = {}, {}, []
    _capped_log = []          # 触顶替补记录（审计用）
    for r in cands:
        s = r.get('sector_l2') or '未分类'
        p = r.get('sector_parent') or s
        if per_cnt.get(s, 0) >= eff_per_sector:
            continue
        if eff_per_parent and par_cnt.get(p, 0) >= eff_per_parent:
            continue
        # P0-2: 行业暴露偏离检查（等权口径：加入后该行业占比 vs 基准占比）
        if dev_pp > 0 and not no_l1_cap and top > 0:
            _would = (par_cnt.get(p, 0) + 1) / float(top)
            if (_would - _base_share.get(p, 0)) * 100.0 > dev_pp:
                _capped_log.append({'name': r.get('name'), 'code': r.get('code'),
                                    'parent': p, 'reason': 'l1_deviation',
                                    'would_share': round(_would, 3),
                                    'base_share': round(_base_share.get(p, 0), 3)})
                continue
        per_cnt[s] = per_cnt.get(s, 0) + 1
        par_cnt[p] = par_cnt.get(p, 0) + 1
        picked.append(r)
        if len(picked) >= top:
            break
    return picked, _capped_log


def fmt_pct(x):
    if x is None:
        return '—'
    return f"{x:+.2f}%"


def _base_share_of(cands, parent):
    """候选池内某申万一级行业的等权基准占比（P0-2 暴露偏离的参照）"""
    n = len(cands) or 1
    return sum(1 for r in cands
               if (r.get('sector_parent') or r.get('sector_l2') or '未分类') == parent) / float(n)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('scored', nargs='?', default=None)
    ap.add_argument('--top', type=int, default=10)
    ap.add_argument('--per-sector', type=int, default=3)
    ap.add_argument('--per-parent', type=int, default=2, help='同一申万一级行业最多N只(防行业集中)')
    ap.add_argument('--min-total', type=float, default=12.0, help='最低综合评分(默认12, 只推B级及以上)')
    ap.add_argument('--no-route-pool', action='store_true',
                    help='关闭路线差异化池(两路线都用全池), 用于对照')
    ap.add_argument('--no-eligibility', action='store_true',
                    help='关闭资格过滤(次新/停牌/流动性不达标不剔除), 用于对照')
    ap.add_argument('--no-l1-cap', action='store_true',
                    help='关闭申万一级行业暴露偏离上限, 用于对照')
    ap.add_argument('--out', default=None)
    args = ap.parse_args()

    cfg = load_config()
    route_pool = cfg.get('route_pool_map', {})
    mom_w = float(cfg.get('momentum_route_weight', 0.5) or 0.5)

    scored = args.scored
    if scored is None:
        fs = [f for f in sorted(glob.glob(os.path.join(HERE, 'top200_scored_*.json')), reverse=True)
              if '_watch_' not in f and '.bak' not in f]
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

    # ---- 路线差异化池：按成交额(turnover)排名切分 ----
    # 依据: TOP200 vs TOP500 对照回测 —— B 反转路线在 TOP500 全胜(选股超额 +6.3pp),
    #       A 动量路线在 TOP500 全败(反转市)。故 reversal 用更大池(默认500),
    #       momentum 维持小池(默认200), 兼顾证据与集中度。
    ranked = sorted(results, key=lambda r: -float(r.get('turnover') or 0))
    for i, r in enumerate(ranked, 1):
        r['_amt_rank'] = i
    pool_stat = {}
    for mode in ('momentum', 'reversal'):
        ps = int(route_pool.get(mode, len(ranked)) or len(ranked))
        pool_stat[mode] = min(ps, len(ranked))
    print(f"scored 日期 {sdate} | 板块动量日期 {mdate} | 板块数 {n} | 全池 {len(results)} 只")
    if not args.no_route_pool and len(ranked) > min(pool_stat.values()):
        print(f"  路线差异化池: momentum→前{pool_stat['momentum']} / reversal→前{pool_stat['reversal']}"
              f"（动量路线降权系数 {mom_w}）")

    out = {'date': sdate, 'momentum_date': mdate, 'top': args.top, 'per_sector': args.per_sector,
           'per_parent': args.per_parent,
           'route_pool_map': ({'momentum': pool_stat['momentum'], 'reversal': pool_stat['reversal']}
                              if not args.no_route_pool else {'momentum': len(ranked), 'reversal': len(ranked)}),
           'momentum_route_weight': mom_w,
           'portfolios': {}}
    min_cands = max(args.top, 10)   # 候选不足 top 数量时继续放宽板块层级 → 组合更分散
    elig_stats = {}
    for mode in ('momentum', 'reversal'):
        # 路线池过滤（--no-route-pool 或池本身就好时用全池）
        if args.no_route_pool:
            route_results = results
        else:
            cap = pool_stat[mode]
            route_results = [r for r in results if r.get('_amt_rank', 1) <= cap]
        # v3.2 P0-1/P0-3: 资格过滤（次新/停牌/流动性不达标剔除，--no-eligibility 关闭）
        _before = len(route_results)
        if not args.no_eligibility:
            route_results = [r for r in route_results
                             if not r.get('is_new') and not r.get('suspended')
                             and r.get('liquid_ok', True)]
            elig_stats[mode] = {'before': _before, 'after': len(route_results),
                                'dropped': _before - len(route_results)}
        levels = pick_sectors(rows, mode)
        secs, level, cands, trace = select(rows, route_results, mode, min_cands)
        picked, capped_log = build(cands, mode, args.per_sector, args.per_parent,
                                   args.top, args.min_total, cfg, no_l1_cap=args.no_l1_cap)
        # v3.2 P0-2: 行业暴露表（等权口径，已选权重 vs 候选池基准）
        _par_share = {}
        for r in picked:
            _p = r.get('sector_parent') or r.get('sector_l2') or '未分类'
            _par_share[_p] = _par_share.get(_p, 0) + 1
        _n_pick = len(picked) or 1
        _exposure = [{'parent': k, 'n': v, 'share': round(v / _n_pick, 3),
                      'base_share': round(_base_share_of(cands, k), 3),
                      'dev_pp': round((v / _n_pick - _base_share_of(cands, k)) * 100, 1)}
                     for k, v in sorted(_par_share.items(), key=lambda x: -x[1])]
        out['portfolios'][mode] = {
            'level_used': level, 'target_rule': levels[0][0], 'level_trace': trace,
            'sectors': sorted(secs), 'n_candidates': len(cands),
            'pool_used': len(route_results),
            'eligibility': elig_stats.get(mode),
            'exposure': _exposure,
            'l1_capped': capped_log,
            'route_weight': (mom_w if mode == 'momentum' else 1.0),
            'picks': [{'name': r['name'], 'code': r['code'], 'sector_l2': r.get('sector_l2'),
                       'parent': r.get('sector_parent', ''), 'total': r['total'], 'rating': r['rating'],
                       'adj_total': round(float(r['total']) * (mom_w if mode == 'momentum' else 1.0), 2),
                       'amt_rank': r.get('_amt_rank'),
                       'price': r.get('price'), 'pct_chg': r.get('pct_chg'),
                       'turnover_ratio': r.get('turnover_ratio'), 'circ_mv': r.get('circ_mv'),
                       'rsi': (r.get('tech') or {}).get('rsi'), 'trend': (r.get('tech') or {}).get('trend'),
                       'sector_r60': r.get('sector_r60'), 'sector_r10': r.get('sector_r10'),
                       'sector_mom60': r.get('sector_mom60'), 'sector_mom10': r.get('sector_mom10'),
                       'score_sector': r.get('score_sector'),
                       'score_news': r.get('score_news'), 'score_tech': r.get('score_tech'),
                       'score_fund': r.get('score_fund'), 'score_theme': r.get('score_theme')}
                      for r in picked],
        }
        print(f"\n=== {mode} 路线 | {level} | 路线池 {len(route_results)} 只 ===")
        if elig_stats.get(mode):
            _e = elig_stats[mode]
            print(f"   资格过滤: {_e['before']} → {_e['after']} (剔除次新/停牌/流动性不达标 {_e['dropped']} 只)")
        for t in trace:
            print(f"   [{t['level']:<34}] 板块{t['n_sectors']:>3} 候选{t['n_cands']:>3}")
        print(f"  选中板块({len(secs)}): {', '.join(sorted(secs)[:12])}{'...' if len(secs) > 12 else ''}  候选 {len(cands)} 只")
        if capped_log:
            print(f"  一级行业暴露触顶替补 {len(capped_log)} 只: "
                  f"{', '.join(str(c['name']) for c in capped_log[:5])}{'...' if len(capped_log) > 5 else ''}")
        print(f"  行业暴露: " + " | ".join(
            f"{e['parent']} {e['n']}只({e['share']:.0%}, 偏离{e['dev_pp']:+.1f}pp)" for e in _exposure[:6]))
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
