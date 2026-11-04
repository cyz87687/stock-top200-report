#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
audit_round52_domain_split.py —— 决定性对照：同一因子、同一区间，只改「域宽度」

【要回答的问题】
  round51 发现：同一因子在同一区间，宽域（2029只）与窄池（TOP池 490/1895只）的 IC 差 2 倍以上：
      score_lowvol20 h5：宽域 +0.0565  vs  factor_ic(窄池) +0.1248
      score_bias60   h5：宽域 +0.0258  vs  factor_ic(窄池) +0.0748
  必须确认这是「域宽度/选择偏差」造成，而不是我的计算实现与 factor_eval 有差异。

【设计】
  三个域，同一批截面日期、同一份因子值（panel_round34）、同一份价格（klines_long）：
    D1 全宽域     : 面板当日全部 2029 只
    D2 TOP池域    : 仅当日 fwd_hist 快照内的标的（490 / 1895）—— 复刻 factor_eval 的域
    D3 域外       : D1 但剔除 D2（= 从未进过 TOP 池的标的）
  若 D2 ≈ factor_ic 数值，则证明差异纯由域宽度解释，计算实现无偏。

【口径】
  收盘/收盘收益，NW lag=4，min_xs=20。与 factor_ic 的 t+1 开盘口径仍有差异，
  故不追求数值完全相等，看「D2 是否显著高于 D1、D2 是否更接近 factor_ic」。
"""
import json
import os
import math
import glob

HERE = os.path.dirname(os.path.abspath(__file__))
PANEL = os.path.join(HERE, 'data', 'panel_round34.json')
KL = os.path.join(HERE, 'data', 'klines_long.json')
FWD = os.path.join(HERE, 'data', 'fwd_hist')
OUT = os.path.join(HERE, 'audit_round52.json')

MIN_XS = 20
NW_LAG = 4
DIMS = ['score_lowvol20', 'score_bias60', 'score_rsi_rev', 'score_tech_neg',
        'score_quality', 'score_ep', 'score_size_ind', 'score_fund']
HOPS = [1, 5, 20]


def _rank(vals):
    n = len(vals)
    idx = sorted(range(n), key=lambda i: vals[i])
    r = [0.0] * n
    i = 0
    while i < n:
        j = i
        while j + 1 < n and vals[idx[j + 1]] == vals[idx[i]]:
            j += 1
        avg = (i + j) / 2.0 + 1.0
        for t in range(i, j + 1):
            r[idx[t]] = avg
        i = j + 1
    return r


def _pearson(x, y):
    n = len(x)
    if n < 3:
        return None
    mx = sum(x) / n
    my = sum(y) / n
    sxy = sum((a - mx) * (b - my) for a, b in zip(x, y))
    sxx = sum((a - mx) ** 2 for a in x)
    syy = sum((b - my) ** 2 for b in y)
    if sxx <= 0 or syy <= 0:
        return None
    return sxy / math.sqrt(sxx * syy)


def spearman(x, y):
    if len(x) < 3:
        return None
    return _pearson(_rank(x), _rank(y))


def nw_t(xs, lag=NW_LAG):
    n = len(xs)
    if n < 3:
        return None
    mean = sum(xs) / n
    d = [v - mean for v in xs]
    var = sum(v * v for v in d) / n
    for L in range(1, min(lag, n - 1) + 1):
        w = 1.0 - L / (lag + 1.0)
        var += 2.0 * w * (sum(d[t] * d[t - L] for t in range(L, n)) / n)
    if var <= 0:
        return None
    se = math.sqrt(var / n)
    return mean / se if se > 0 else None


def main():
    panel = json.load(open(PANEL, encoding='utf-8'))
    kl = json.load(open(KL, encoding='utf-8'))
    rows = panel['rows']
    dates = sorted(rows.keys())

    all_dates = set()
    for c, d in kl.items():
        all_dates |= set(d.keys())
    cal = sorted(all_dates)
    pos = {d: i for i, d in enumerate(cal)}

    # 载入每个日期的 TOP 池成员
    pool_members = {}
    for fp in glob.glob(os.path.join(FWD, '*.json')):
        dt = os.path.basename(fp)[:10]
        try:
            j = json.load(open(fp, encoding='utf-8'))
        except Exception:
            continue
        rs = j.get('rows') if isinstance(j, dict) else j
        s = set()
        for r in (rs or []):
            c = r.get('code') if isinstance(r, dict) else None
            if c:
                s.add(str(c)[-6:])
        if s:
            pool_members[dt] = s

    def close(code, dt):
        d = kl.get(code)
        if not d:
            return None
        row = d.get(dt)
        if not row or not isinstance(row, list) or len(row) < 2:
            return None
        return row[1]

    def calc(dim, h, domain):
        xs = []
        for dt in dates:
            i = pos.get(dt)
            if i is None or i + h >= len(cal):
                continue
            fwd = cal[i + h]
            snap = rows[dt]
            mem = pool_members.get(dt)
            fv, rv = [], []
            for code, vals in snap.items():
                v = vals.get(dim)
                if not isinstance(v, (int, float)):
                    continue
                if domain == 'pool':
                    if not mem or code not in mem:
                        continue
                elif domain == 'offpool':
                    if mem and code in mem:
                        continue
                c0 = close(code, dt)
                c1 = close(code, fwd)
                if not c0 or not c1 or c0 <= 0:
                    continue
                fv.append(float(v))
                rv.append(c1 / c0 - 1.0)
            if len(fv) < MIN_XS or max(fv) - min(fv) < 1e-12:
                continue
            ic = spearman(fv, rv)
            if ic is not None:
                xs.append(ic)
        if len(xs) < MIN_XS:
            return {'n': len(xs), 'ic_mean': None, 'nw_t': None}
        mean = sum(xs) / len(xs)
        t = nw_t(xs)
        return {'n': len(xs), 'ic_mean': round(mean, 4),
                'nw_t': round(t, 2) if t is not None else None,
                'win_rate': round(sum(1 for v in xs if v > 0) / len(xs) * 100.0, 1)}

    # factor_ic 参考值
    try:
        fic = json.load(open(os.path.join(HERE, 'factor_ic.json'), encoding='utf-8'))
        ficf = fic.get('factors') or {}
    except Exception:
        ficf = {}

    out = {
        'round': 52,
        'purpose': '同一因子同一区间只改域宽度，验证 IC 差异是否纯由域宽/选择偏差解释',
        'domains': {
            'all': '面板当日全部标的（≈2029 只宽域）',
            'pool': '仅当日 fwd_hist 快照成员（≈490/1895 只，复刻 factor_eval 域）',
            'offpool': '宽域剔除池内成员（从未入 TOP 池的标的）',
        },
        'caveat': '收盘/收盘收益，与 factor_ic 的 t+1 开盘扣20bp 口径不同，数值不求相等，只看相对关系',
        'nw_lag': NW_LAG,
        'min_xs': MIN_XS,
        'dims': {},
    }

    for dim in DIMS:
        out['dims'][dim] = {}
        for h in HOPS:
            rec = {}
            for dom in ('all', 'pool', 'offpool'):
                rec[dom] = calc(dim, h, dom)
            # factor_ic 参照
            hh = ((ficf.get(dim) or {}).get('horizons') or {}).get(str(h)) or {}
            rec['factor_ic'] = {'n': hh.get('n'), 'ic_mean': hh.get('ic_mean'),
                                'nw_t': hh.get('nw_t')}
            out['dims'][dim][str(h)] = rec

    json.dump(out, open(OUT, 'w', encoding='utf-8'), ensure_ascii=False, indent=1)

    # ---- 打印 ----
    for h in HOPS:
        print(f"\n{'='*94}\n  h = {h} 日\n{'='*94}")
        print(f"{'dim':20s} | {'宽域all':>16s} | {'池内pool':>16s} | {'域外off':>16s} | {'factor_ic':>14s}")
        print(f"{'':20s} | {'n':>4s}{'IC':>8s}{'t':>4s} | {'n':>4s}{'IC':>8s}{'t':>4s} | {'n':>4s}{'IC':>8s}{'t':>4s} | {'n':>4s}{'IC':>8s}")
        print('-' * 94)
        for dim in DIMS:
            r = out['dims'][dim][str(h)]

            def fmt(k):
                d = r[k]
                ic = f"{d['ic_mean']:+.4f}" if d.get('ic_mean') is not None else '   —  '
                t = f"{d['nw_t']:+.2f}" if d.get('nw_t') is not None else '  — '
                return f"{d['n']:>4d}{ic:>8s}{t:>4s}"

            fi = r['factor_ic']
            fic_ic = f"{fi['ic_mean']:.4f}" if isinstance(fi.get('ic_mean'), (int, float)) else '—'
            fic_n = str(fi.get('n')) if fi.get('n') is not None else '—'
            print(f"{dim:20s} | {fmt('all')} | {fmt('pool')} | {fmt('offpool')} | {fic_n:>4s}{fic_ic:>8s}")
        print('-' * 94)

    print(f"\n✅ 写出 {os.path.basename(OUT)}")


if __name__ == '__main__':
    main()
