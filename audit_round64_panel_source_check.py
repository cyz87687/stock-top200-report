#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
audit_round64_panel_source_check.py —— 长面板接入前的等值性交叉验证

【要回答的问题】
  执行顺序表第 1 项「factor_ic 数据源切长面板」之前必须证明：
  **同一日期、同一批股票、同一价格源下，panel_round34 的因子值算出的 IC
   与 factor_history 算出的 IC 一致**。
  若不一致，接入长面板就等于换了口径，IC 序列不可比 —— 这正是「同一口径两处实现必然漂移」。

【做法】
  取 factor_history 与 panel 的**重叠日期**（有 ep 的那几天：10-23 / 10-26 / 10-27），
  对每个共有因子：
    - 用 factor_history 的因子值 + klines_long 价格 → IC_hist
    - 用 panel 的因子值       + klines_long 价格 → IC_panel
  比较：Spearman 因子值相关（应≈1）、IC 绝对差、IC 序列相关

【口径注意】
  panel 的 size 维是 score_size_ind（v4.12），factor_history 是 score_size_growth（v4.14），
  二者**不是同一因子**，不参与等值性比较（只比较共有且同名的维）。

【输出】audit_round64.json
"""
import json
import os
import math

HERE = os.path.dirname(os.path.abspath(__file__))
HIST = os.path.join(HERE, 'factor_history.json')
PANEL = os.path.join(HERE, 'data', 'panel_round34.json')
KL = os.path.join(HERE, 'data', 'klines_long.json')
OUT = os.path.join(HERE, 'audit_round64.json')

MIN_XS = 20


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


def pearson(x, y):
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
    return pearson(_rank(x), _rank(y))


def main():
    hist = json.load(open(HIST, encoding='utf-8'))
    panel = json.load(open(PANEL, encoding='utf-8'))
    kl = json.load(open(KL, encoding='utf-8'))

    hs = {s['date']: s for s in hist.get('snapshots', [])}
    ps = panel['rows']

    all_dates = set()
    for c, d in kl.items():
        all_dates |= set(d.keys())
    cal = sorted(all_dates)
    pos = {d: i for i, d in enumerate(cal)}

    def close(code, dt):
        d = kl.get(code)
        if not d:
            return None
        row = d.get(dt)
        if not row or not isinstance(row, list) or len(row) < 2:
            return None
        return row[1]

    overlap = sorted(set(hs) & set(ps))
    out = {
        'round': 64,
        'purpose': '长面板接入 factor_ic 前的等值性交叉验证',
        'overlap_dates': overlap,
        'n_overlap': len(overlap),
        'note': ('panel 的 size 维是 score_size_ind（v4.12），factor_history 是 score_size_growth'
                 '（v4.14），不是同一因子，不参与等值性比较。'
                 'factor_history 的 code 为 6 位，panel 同为 6 位，可直接对齐。'),
        'dims': {},
    }

    # 共有同名维
    common_dims = set()
    for d in overlap:
        rows = hs[d]['rows']
        if rows:
            common_dims |= set(rows[0].keys()) & set(list(ps[d].values())[0].keys())
    common_dims = sorted(x for x in common_dims if x.startswith('score_'))

    for dim in common_dims:
        rec = {'per_date': [], 'factor_value_corr': [], 'ic_diff': []}
        for d in overlap:
            hrows = {r['code']: r for r in hs[d]['rows'] if isinstance(r.get('code'), str)}
            prows = ps[d]
            # ⚠️ factor_history 早期截面（06-26~10-12）只存了 fund/news，
            #    其余字段不存在 → 该维在此截面「两源都有值」的 code 数为 0，
            #    无法比较。必须先筛出「两源该维都有值且 ≥MIN_XS」的截面才可比。
            #    否则会把「字段不存在」误读成「口径不一致」（2026-10-29 踩过）。
            xs, ys = [], []
            for code, pv in prows.items():
                hv = hrows.get(code, {}).get(dim)
                if isinstance(pv.get(dim), (int, float)) and isinstance(hv, (int, float)):
                    xs.append(float(hv))
                    ys.append(float(pv[dim]))
            n_common = len(xs)
            if n_common < MIN_XS:
                # 该维在此截面不可比 → 跳过，不计入统计
                continue
            vcorr = spearman(xs, ys)

            # IC：各自算 h1/h5（用 klines_long 收盘价）
            ics = {}
            for tag, src in (('hist', hrows), ('panel', prows)):
                for h in (1, 5):
                    i = pos.get(d)
                    if i is None or i + h >= len(cal):
                        ics[(tag, h)] = None
                        continue
                    fwd = cal[i + h]
                    fv, rv = [], []
                    for code, vals in src.items():
                        v = vals.get(dim)
                        if not isinstance(v, (int, float)):
                            continue
                        c0 = close(code, d)
                        c1 = close(code, fwd)
                        if not c0 or not c1 or c0 <= 0:
                            continue
                        fv.append(float(v))
                        rv.append(c1 / c0 - 1.0)
                    if len(fv) < MIN_XS or max(fv) - min(fv) < 1e-12:
                        ics[(tag, h)] = None
                    else:
                        ics[(tag, h)] = spearman(fv, rv)
            if vcorr is not None:
                rec['factor_value_corr'].append(round(vcorr, 6))
            for h in (1, 5):
                a, b = ics[('hist', h)], ics[('panel', h)]
                if a is not None and b is not None:
                    rec['ic_diff'].append(round(abs(a - b), 6))
            rec['per_date'].append({
                'date': d,
                'n_common_codes': n_common,
                'factor_value_spearman': round(vcorr, 6) if vcorr is not None else None,
                'ic_hist_h1': ics[('hist', 1)], 'ic_panel_h1': ics[('panel', 1)],
                'ic_hist_h5': ics[('hist', 5)], 'ic_panel_h5': ics[('panel', 5)],
            })
        if rec['factor_value_corr']:
            rec['factor_value_corr_mean'] = round(
                sum(rec['factor_value_corr']) / len(rec['factor_value_corr']), 6)
            rec['factor_value_corr_min'] = round(min(rec['factor_value_corr']), 6)
        if rec['ic_diff']:
            rec['ic_abs_diff_mean'] = round(sum(rec['ic_diff']) / len(rec['ic_diff']), 6)
            rec['ic_abs_diff_max'] = round(max(rec['ic_diff']), 6)
        out['dims'][dim] = rec

    json.dump(out, open(OUT, 'w', encoding='utf-8'), ensure_ascii=False, indent=1)

    # ---- 打印 ----
    print('═' * 104)
    print('  长面板接入前：等值性交叉验证')
    print('═' * 104)
    print(f"  重叠日期：{overlap}  （共 {len(overlap)} 天）")
    print()
    print(f"{'维度':20s} {'因子值ρ(均值)':>14s} {'因子值ρ(最低)':>14s} {'IC差(均值)':>12s} {'IC差(最大)':>12s}")
    print('-' * 104)
    for dim, r in out['dims'].items():
        vc = r.get('factor_value_corr_mean')
        vmin = r.get('factor_value_corr_min')
        im = r.get('ic_abs_diff_mean')
        imx = r.get('ic_abs_diff_max')
        print(f"{dim:20s} "
              f"{(f'{vc:.6f}' if vc is not None else '—'):>14s} "
              f"{(f'{vmin:.6f}' if vmin is not None else '—'):>14s} "
              f"{(f'{im:.6f}' if im is not None else '—'):>12s} "
              f"{(f'{imx:.6f}' if imx is not None else '—'):>12s}")
    print('-' * 104)
    print('  判据：因子值 ρ ≥ 0.999（口径同源）且 IC 绝对差 ≤ 0.01（数值可比）→ 可安全接入')
    print()
    for dim, r in out['dims'].items():
        if not r['per_date']:
            continue
        print(f"  【{dim}】")
        for pd_ in r['per_date']:
            print(f"    {pd_['date']} n={pd_['n_common_codes']:5d} "
                  f"ρ因子={pd_['factor_value_spearman']}  "
                  f"IC h1: hist={pd_['ic_hist_h1']} panel={pd_['ic_panel_h1']}  "
                  f"IC h5: hist={pd_['ic_hist_h5']} panel={pd_['ic_panel_h5']}")

    print(f"\n✅ 写出 {os.path.basename(OUT)}")


if __name__ == '__main__':
    main()
