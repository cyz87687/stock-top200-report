#!/usr/bin/env python3
"""TOP200 vs TOP500 样本池扩大对照回测

实验设计(控制变量: 只有每日候选池大小不同, 其余全部一致):
  对照组: 每日成交额 TOP200 池内选股
  实验组: 每日成交额 TOP500 池内选股
  同源: 同一选股逻辑(B反转路线, 复用生产 score_tech 口径) / 同一板块动量 /
        同一交易规则(T+1收盘进, 持有H个自然交易日收盘出, 等权10只)

选股逻辑(与 pick_portfolio.py B.reversal 路线同构, 全部可从K线重建, 无前视):
  1. 申万二级板块动量: 板块内个股60日/10日收益中位数 → 横截面排名(1=最强)
  2. 选弱势板块: R60 排名后40%分位(生产B路线 L2/L3 口径)
  3. 个股: RSI<30 优先, 其余按 score_tech 升序(低分=超卖=反转); 剔除 RSI>70
  4. 约束: 同二级板块≤3, 同一级行业≤2, 组合10只等权

数据纪律:
  - K线/指数全部真实接口(腾讯前复权多域名轮询+新浪兜底), 无数据则留空不编造
  - 成交额排名用典型价×成交量近似(K线无成交额字段), 与10-12真实成交额校准后披露
  - 评分信号复用 step2_stockscorer_v2.score_tech(生产口径), 保证控制变量干净

用法: python3 backtest_universe.py [--hold 20] [--top 10]
输出: data/backtest_universe_result.json + data/backtest_universe_report.md
"""
import os
import sys
import json
import argparse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
for _k in ('HTTP_PROXY', 'HTTPS_PROXY', 'http_proxy', 'https_proxy', 'ALL_PROXY', 'all_proxy'):
    os.environ.pop(_k, None)

from step2_stockscorer_v2 import score_tech, rsi as calc_rsi  # 生产口径复用

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
TENCENT_HOSTS = ["https://ifzq.gtimg.cn", "https://web.ifzq.gtimg.cn", "https://proxy.finance.qq.com/ifzqgtimg"]
KL_PATH = os.path.join(HERE, 'data', 'universe_klines.json')
SW_MAP_PATH = os.path.join(HERE, 'sector', 'sw_map.json')
FH_PATH = os.path.join(HERE, 'factor_history.json')
T1000_PATH = (os.path.join(HERE, 'top500_all_a.json')
              if os.path.exists(os.path.join(HERE, 'top500_all_a.json'))
              else os.path.join(HERE, 'top1000_all_a.json'))  # 旧文件名兜底(已停产)
BENCH_SYMBOL = 'sh000300'
ANN = 244  # A股年化交易日


# ---------------- 数据加载 ----------------

def load_klines():
    """加载并归一化: key 统一为6位code(原始数据混有 sh600519/600519 两种格式)"""
    raw = json.load(open(KL_PATH, encoding='utf-8'))
    out = {}
    for code, kd in raw.items():
        c6 = code6(code)
        if c6 and c6 not in out:
            out[c6] = kd
    return out


def load_sw_map():
    """sw_map.json: l1={code6:一级}, l2={code6:二级}, l2_parent={二级:一级} 等
    → {code6: {'sector': 二级, 'parent': 一级}}"""
    m = json.load(open(SW_MAP_PATH, encoding='utf-8'))
    l1, l2, l2p = m.get('l1', {}), m.get('l2', {}), m.get('l2_parent', {})
    out = {}
    for c6 in set(l1) | set(l2):
        sec = l2.get(c6)
        out[c6] = {'sector': sec, 'parent': l2p.get(sec) if sec else l1.get(c6)}
    return out


def fetch_bench():
    """沪深300前复权日线 → {date: close}"""
    for host in TENCENT_HOSTS:
        url = f"{host}/appstock/app/fqkline/get?param={BENCH_SYMBOL},day,,,200,qfq"
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=15) as r:
                j = json.loads(r.read().decode('utf-8'))
            k = j['data'][BENCH_SYMBOL].get('qfqday') or j['data'][BENCH_SYMBOL].get('day') or []
            return {row[0]: float(row[2]) for row in k}
        except Exception:
            continue
    return {}


def code6(code):
    s = ''.join(ch for ch in str(code) if ch.isdigit())
    return s[-6:].zfill(6) if len(s) >= 6 else None


# ---------------- 因子预计算(仅截面日, 选股用) ----------------

def precompute_factors(klines, dates):
    """factors[date][code] = {'tech','rsi','mom10','mom60','amt'} (amt=成交额近似)"""
    factors = {d: {} for d in dates}
    codes = [c for c in klines if any(d in klines[c] for d in dates)]
    n = len(codes)
    for i, code in enumerate(codes, 1):
        kd = klines[code]
        all_dates = sorted(kd.keys())
        pos = {d: j for j, d in enumerate(all_dates)}
        for d in dates:
            j = pos.get(d)
            if j is None or j < 60:
                continue  # 预热不足(MA60/mom60)
            window = all_dates[max(0, j - 64):j + 1]
            kl = [{'date': w, 'close': kd[w][1], 'vol': kd[w][4],
                   'amount': kd[w][4] * kd[w][1]} for w in window]
            price = kd[d][1]
            pct = (price / kd[all_dates[j - 1]][1] - 1) * 100
            try:
                ts, _ = score_tech(kl, price, pct)
            except Exception:
                ts = 2.0
            cls = [kd[w][1] for w in window]
            r14 = calc_rsi(cls, 14)
            mom10 = (price / kd[window[-11]][1] - 1) * 100 if len(window) >= 11 else None
            mom60 = (price / kd[window[-61]][1] - 1) * 100 if len(window) >= 61 else None
            o, c, h, l, v = kd[d]
            amt = (h + l + c) / 3 * v * 100  # 典型价×量(手)×100
            factors[d][code] = {'tech': float(ts), 'rsi': r14,
                                'mom10': mom10, 'mom60': mom60, 'amt': amt}
        if i % 200 == 0 or i == n:
            print(f"  因子预计算 [{i}/{n}]", flush=True)
    return factors


# ---------------- 板块动量与选股 ----------------

def sector_momentum(fd, sw_map):
    """申万二级板块: 个股 mom60/mom10 中位数 → 横截面排名(1=最强)。两腿共用同一套。"""
    agg = {}
    for code, f in fd.items():
        sec = (sw_map.get(code6(code)) or {}).get('sector')
        if not sec or f['mom60'] is None or f['mom10'] is None:
            continue
        a = agg.setdefault(sec, {'m60': [], 'm10': []})
        a['m60'].append(f['mom60'])
        a['m10'].append(f['mom10'])
    rows = []
    for sec, a in agg.items():
        if len(a['m60']) < 3:
            continue
        rows.append({'sector': sec,
                     'mom60': sorted(a['m60'])[len(a['m60']) // 2],
                     'mom10': sorted(a['m10'])[len(a['m10']) // 2]})
    rows.sort(key=lambda r: -r['mom60'])
    for i, r in enumerate(rows, 1):
        r['r60'] = i
    rows.sort(key=lambda r: -r['mom10'])
    for i, r in enumerate(rows, 1):
        r['r10'] = i
    return rows


def select_stocks(sm_rows, pool, sw_map, route, top, per_sector, per_parent):
    """双路线选股(与 pick_portfolio.py 同构, 全部K线可重建):
    reversal(B路线): 弱势板块(R60后40%)内 RSI<30优先, 否则 tech 升序; 剔除RSI>70
    momentum(A路线): 强势板块(R60前40%)内 tech 降序(追强势); 剔除RSI>70
    """
    if not sm_rows or not pool:
        return []
    n = len(sm_rows)
    r60 = {r['sector']: r['r60'] for r in sm_rows}
    if route == 'momentum':
        sectors = {s for s in r60 if r60[s] <= n * 0.4}   # R60 前40%
    else:
        sectors = {s for s in r60 if r60[s] > n * 0.6}    # R60 后40%
    if not sectors:
        sectors = set(r60)
    cands = []
    for code, f in pool.items():
        info = sw_map.get(code6(code)) or {}
        sec = info.get('sector') or '未分类'
        if sec not in sectors:
            continue
        r14 = f['rsi']
        if r14 is not None and r14 > 70:
            continue
        cands.append({'code': code, 'sector': sec, 'parent': info.get('parent') or sec,
                      'tech': f['tech'], 'rsi': r14})
    if route == 'momentum':
        cands.sort(key=lambda r: -r['tech'])  # 强势优先
    else:
        # RSI<30 优先组, 其余按 tech 升序(低分=超卖)
        cands.sort(key=lambda r: (0 if (r['rsi'] is not None and r['rsi'] < 30) else 1, r['tech']))
    per_cnt, par_cnt, picked = {}, {}, []
    for r in cands:
        if per_cnt.get(r['sector'], 0) >= per_sector:
            continue
        if par_cnt.get(r['parent'], 0) >= per_parent:
            continue
        per_cnt[r['sector']] = per_cnt.get(r['sector'], 0) + 1
        par_cnt[r['parent']] = par_cnt.get(r['parent'], 0) + 1
        picked.append(r)
        if len(picked) >= top:
            break
    return picked


# ---------------- 指标 ----------------

def equity_metrics(curve):
    """curve: 日收益序列 → 年化/夏普/MDD/胜率"""
    n = len(curve)
    if n < 10:
        return None
    eq, peak, mdd = 1.0, 1.0, 0.0
    for r in curve:
        eq *= (1 + r)
        peak = max(peak, eq)
        mdd = min(mdd, eq / peak - 1)
    mu = sum(curve) / n
    sd = (sum((r - mu) ** 2 for r in curve) / (n - 1)) ** 0.5 if n > 1 else 0.0
    return {'n_days': n,
            'ann_ret': (eq ** (ANN / n) - 1) * 100,
            'sharpe': mu / sd * (ANN ** 0.5) if sd > 0 else 0.0,
            'mdd': mdd * 100,
            'daily_win': sum(1 for r in curve if r > 0) / n * 100}


def alpha_metrics(curve, base_curve):
    """超额曲线 = 组合日收益 - 基准日收益(同日对齐)"""
    n = min(len(curve), len(base_curve))
    if n < 10:
        return None
    a = [curve[i] - base_curve[i] for i in range(n)]
    eq, peak, mdd = 1.0, 1.0, 0.0
    for r in a:
        eq *= (1 + r)
        peak = max(peak, eq)
        mdd = min(mdd, eq / peak - 1)
    mu = sum(a) / n
    sd = (sum((r - mu) ** 2 for r in a) / (n - 1)) ** 0.5 if n > 1 else 0.0
    return {'ann_alpha': (eq ** (ANN / n) - 1) * 100,
            'sharpe_alpha': mu / sd * (ANN ** 0.5) if sd > 0 else 0.0,
            'mdd_alpha': mdd * 100,
            'daily_win_alpha': sum(1 for r in a if r > 0) / n * 100}


def cost_adjusted_curve(curve, hold, cost_bps):
    """成本敏感性: 按持有期推算日换手, 从日收益中扣减双边成本。

    假设(保守, 等权、每日有 1/hold 的仓位到期换手):
      单个持仓一进一出 = 2 边 × cost_bps; 持有 hold 天 → 平均每日成本
      drag = 2 × cost_bps × 1e-4 / hold
    cost_bps=0 时即毛收益。此口径只扣显性成本(佣金+冲击合成值),
    未含冲击随规模递增的非线性, 作为「成本可行域」下界。
    """
    if cost_bps <= 0:
        return list(curve)
    drag = 2.0 * cost_bps * 1e-4 / max(1, hold)
    return [r - drag for r in curve]


def calibrate_amount(klines, t1000_path):
    """成交额近似校准: 用最近交易日真实成交额对比 典型价×量 近似

    兼容两代文件: 新 top500_all_a.json(键 main_pool/top500, 无 stocks)
    与旧 top1000_all_a.json(键 stocks, 已停产)。取并集以最大化校准样本。
    """
    if not os.path.exists(t1000_path):
        return None
    d = json.load(open(t1000_path, encoding='utf-8'))
    day = d.get('date')
    _rows = (d.get('stocks') or []) + (d.get('main_pool') or []) + (d.get('top500') or [])
    real = {}
    for r in _rows:
        c6 = code6(r.get('code'))
        if c6 and r.get('amount'):
            real[c6] = r['amount']
    pairs = []
    for code, amt in real.items():
        kd = klines.get(code, {})
        if day in kd:
            o, c, h, l, v = kd[day]
            pairs.append((amt, (h + l + c) / 3 * v * 100))
    if len(pairs) < 50:
        return None
    # Spearman 秩相关
    def ranks(vals):
        order = sorted(range(len(vals)), key=lambda i: vals[i])
        rk = [0.0] * len(vals)
        i = 0
        while i < len(order):
            j = i
            while j + 1 < len(order) and vals[order[j + 1]] == vals[order[i]]:
                j += 1
            avg = (i + j) / 2 + 1
            for k in range(i, j + 1):
                rk[order[k]] = avg
            i = j + 1
        return rk
    ra, rb = ranks([p[0] for p in pairs]), ranks([p[1] for p in pairs])
    n = len(pairs)
    ma, mb = sum(ra) / n, sum(rb) / n
    num = sum((ra[i] - ma) * (rb[i] - mb) for i in range(n))
    den = (sum((ra[i] - ma) ** 2 for i in range(n)) * sum((rb[i] - mb) ** 2 for i in range(n))) ** 0.5
    rho = num / den if den else 0.0
    # TOP200/TOP500 边界一致性(近似排名的名次 vs 真实名次)
    approx_rank = {code: i for i, (code, _) in enumerate(
        sorted(((c, (klines[c][day][2] + klines[c][day][3] + klines[c][day][1]) / 3 * klines[c][day][4] * 100)
                for c in real if day in klines.get(c, {})), key=lambda x: -x[1]), 1)}
    real_rank = {code: i for i, (code, _) in enumerate(sorted(real.items(), key=lambda x: -x[1]), 1)}
    both = sorted(set(approx_rank) & set(real_rank))
    t200_agree = sum(1 for c in both if (approx_rank[c] <= 200) == (real_rank[c] <= 200)) / len(both) * 100
    t500_agree = sum(1 for c in both if (approx_rank[c] <= 500) == (real_rank[c] <= 500)) / len(both) * 100
    return {'n': n, 'spearman': rho, 'top200_boundary_agree': t200_agree,
            'top500_boundary_agree': t500_agree, 'calib_date': day}


# ---------------- 主流程 ----------------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--routes', default='reversal,momentum', help='选股路线(逗号分隔)')
    ap.add_argument('--holds', default='20', help='持有自然交易日数(逗号分隔, 默认20)')
    ap.add_argument('--top', type=int, default=10, help='组合只数(默认10)')
    ap.add_argument('--per-sector', type=int, default=3)
    ap.add_argument('--per-parent', type=int, default=2)
    ap.add_argument('--cost-bps', default='0,2,4,6',
                    help='成本敏感性(单边基点, 逗号分隔; 默认 0/2/4/6bp 双边合计)')
    args = ap.parse_args()
    routes = [r.strip() for r in args.routes.split(',') if r.strip()]
    holds = [int(h) for h in args.holds.split(',') if h.strip()]
    cost_list = [float(c) for c in args.cost_bps.split(',') if c.strip()]

    print('加载数据...')
    klines = load_klines()
    sw_map = load_sw_map()
    bench = fetch_bench()
    fh = json.load(open(FH_PATH, encoding='utf-8'))
    dates = [s['date'] for s in fh['snapshots']]
    all_days = sorted({d for kd in klines.values() for d in kd})
    day_idx = {d: i for i, d in enumerate(all_days)}
    print(f'  候选池 {len(klines)} 只 | 截面日 {len(dates)} ({dates[0]}~{dates[-1]}) | '
          f'自然交易日 {len(all_days)} | 基准 {len(bench)} 日')

    calib = calibrate_amount(klines, T1000_PATH)
    if calib:
        print(f"  成交额近似校准({calib['calib_date']}, n={calib['n']}): "
              f"Spearman={calib['spearman']:.3f} | TOP200边界一致率 {calib['top200_boundary_agree']:.1f}% | "
              f"TOP500 {calib['top500_boundary_agree']:.1f}%")

    print('预计算因子(score_tech 生产口径, 仅截面日)...')
    factors = precompute_factors(klines, dates)

    # 基准曲线(对齐 all_days)
    bench_days = sorted(bench)
    b0 = 0
    for i, bd in enumerate(bench_days):
        if bd >= all_days[0]:
            b0 = i
            break
    bench_curve = [bench[bench_days[i]] / bench[bench_days[i - 1]] - 1
                   for i in range(max(1, b0), len(bench_days))]
    if len(bench_curve) > len(all_days):
        bench_curve = bench_curve[-len(all_days):]

    result = {'config': {'routes': routes, 'holds': holds, 'top': args.top,
                         'per_sector': args.per_sector, 'per_parent': args.per_parent,
                         'dates': [dates[0], dates[-1]], 'n_signal_days': len(dates),
                         'universe': len(klines), 'calibration': calib, 'ann_days': ANN},
              'groups': {}}

    for route in routes:
        for hold in holds:
            gkey = f'{route}_h{hold}'
            print(f'回放 [{gkey}] (逐自然交易日)...', flush=True)
            legs = {'top200': {'pos': [], 'trades': [], 'curve': [], 'pool_curve': []},
                    'top500': {'pos': [], 'trades': [], 'curve': [], 'pool_curve': []}}
            pending = {}
            prev_close = {}
            pools = {'top200': {}, 'top500': {}}
            n_signal_used = 0
            for d in all_days:
                # 1) 进场
                for leg, picks in pending.pop(d, {}).items():
                    for p in picks:
                        kd = klines.get(p['code'], {})
                        if d in kd:
                            legs[leg]['pos'].append({
                                'code': p['code'], 'sector': p['sector'],
                                'signal_date': p['signal_date'], 'entry_date': d,
                                'exit_day_idx': day_idx[d] + hold,
                                'entry_px': kd[d][1], 'tech': p['tech'], 'rsi': p['rsi']})
                # 2) 计当日收益 + 平仓
                for leg, L in legs.items():
                    rets, still = [], []
                    for p in L['pos']:
                        kd = klines.get(p['code'], {})
                        if d in kd and p['code'] in prev_close:
                            px = kd[d][1]
                            rets.append(px / prev_close[p['code']] - 1)
                            if day_idx[d] >= p['exit_day_idx']:
                                L['trades'].append({**p, 'ret': px / p['entry_px'] - 1, 'exit_date': d})
                            else:
                                still.append(p)
                        else:
                            still.append(p)
                    L['pos'] = still
                    L['curve'].append(sum(rets) / len(rets) if rets else 0.0)
                # 3) 池等权基准
                for leg in legs:
                    pr = []
                    for code in pools.get(leg, {}):
                        kd = klines.get(code, {})
                        if d in kd and code in prev_close:
                            pr.append(kd[d][1] / prev_close[code] - 1)
                    legs[leg]['pool_curve'].append(sum(pr) / len(pr) if pr else 0.0)
                # 4) prev_close
                for code, kd in klines.items():
                    if d in kd:
                        prev_close[code] = kd[d][1]
                # 5) 截面日选股 → 下一自然交易日进场
                if d in factors and len(factors[d]) >= 50:
                    fd = factors[d]
                    by_amt = sorted(fd.items(), key=lambda kv: -kv[1]['amt'])
                    pools = {'top200': dict(by_amt[:200]), 'top500': dict(by_amt[:500])}
                    sm = sector_momentum(fd, sw_map)
                    n_signal_used += 1
                    nxt_i = day_idx[d] + 1
                    if nxt_i < len(all_days):
                        nxt = all_days[nxt_i]
                        for leg, pool in pools.items():
                            picks = select_stocks(sm, pool, sw_map, route, args.top,
                                                  args.per_sector, args.per_parent)
                            pending.setdefault(nxt, {})[leg] = [{**p, 'signal_date': d} for p in picks]

            out = {}
            for leg, L in legs.items():
                m = equity_metrics(L['curve'])
                a_hs = alpha_metrics(L['curve'], bench_curve)
                a_pool = alpha_metrics(L['curve'], L['pool_curve'])
                ts = L['trades']
                twr = (sum(1 for t in ts if t['ret'] > 0) / len(ts) * 100) if ts else None
                avg = (sum(t['ret'] for t in ts) / len(ts) * 100) if ts else None
                twr_a = None
                if ts:
                    win_a = n_valid = 0
                    for t in ts:
                        if t['entry_date'] in bench and t['exit_date'] in bench:
                            n_valid += 1
                            if t['ret'] > bench[t['exit_date']] / bench[t['entry_date']] - 1:
                                win_a += 1
                    if n_valid:
                        twr_a = win_a / n_valid * 100
                out[leg] = {'metrics': m, 'alpha_vs_hs300': a_hs, 'alpha_vs_pool': a_pool,
                            'n_trades': len(ts), 'trade_win_rate': twr, 'avg_trade_ret': avg,
                            'trade_win_vs_hs300': twr_a, 'trades': ts,
                            'curve': L['curve'], 'pool_curve': L['pool_curve']}
            pool_metrics = {leg: equity_metrics(L['pool_curve']) for leg, L in legs.items()}
            result['groups'][gkey] = {
                'legs': {k: {kk: vv for kk, vv in v.items()
                             if kk not in ('trades', 'curve', 'pool_curve')}
                         for k, v in out.items()},
                'pool_bench': pool_metrics,
                'n_signal_days_used': n_signal_used,
                'trades': {k: v['trades'] for k, v in out.items()},
                'curves': {k: {'combo': v['curve'], 'pool': v['pool_curve']} for k, v in out.items()}}
            for leg in ('top200', 'top500'):
                m = out[leg]['metrics'] or {}
                a = out[leg]['alpha_vs_hs300'] or {}
                ap = out[leg]['alpha_vs_pool'] or {}
                print(f"  [{leg}] 年化 {m.get('ann_ret', 0):+.1f}% | 夏普 {m.get('sharpe', 0):.2f} | "
                      f"MDD {m.get('mdd', 0):.1f}% | 超额vsHS300 {a.get('ann_alpha', 0):+.1f}% | "
                      f"选股超额vs池 {ap.get('ann_alpha', 0):+.1f}% | "
                      f"单笔胜率 {out[leg]['trade_win_rate'] or 0:.0f}% | 笔数 {out[leg]['n_trades']}")

            # ---- 成本敏感性: 对每 leg 的毛曲线按 0/2/4/6bp 单边扣减 ----
            # 输出年化 + 选股超额(vs池)随成本的衰减, 给出「成本可行域」下界。
            cost_sens = {}
            for leg, L in legs.items():
                row = {}
                for cb in cost_list:
                    adj = cost_adjusted_curve(L['curve'], hold, cb)
                    mm = equity_metrics(adj) or {}
                    aa = alpha_metrics(adj, L['pool_curve']) or {}
                    row[f'{cb:g}bp'] = {'ann_ret': mm.get('ann_ret'), 'sharpe': mm.get('sharpe'),
                                        'ann_alpha_vs_pool': aa.get('ann_alpha')}
                cost_sens[leg] = row
            result['groups'][gkey]['cost_sensitivity'] = cost_sens
            for leg in ('top200', 'top500'):
                row = cost_sens[leg]
                _cells = '  '.join(
                    f"{k}:{v['ann_ret']:+.1f}%/α{v['ann_alpha_vs_pool']:+.1f}%"
                    for k, v in row.items() if v.get('ann_ret') is not None)
                print(f"  [{leg}] 成本敏感性(年化/选股超额): {_cells}")

    os.makedirs(os.path.join(HERE, 'data'), exist_ok=True)
    json.dump(result, open(os.path.join(HERE, 'data', 'backtest_universe_result.json'), 'w', encoding='utf-8'),
              ensure_ascii=False, indent=1)
    print('✅ data/backtest_universe_result.json')


if __name__ == '__main__':
    main()
