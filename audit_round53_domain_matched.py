#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
audit_round53_domain_matched.py —— round52 的严格版：**同日期配对**域宽对照

【为什么必须再做一次】
  round52 里「宽域 n=234 / 池内 n=78」——两者**截面日期范围不同**：
      宽域 2025-11-03~2026-10-21（234 个）
      池内 2026-06-26~2026-10-21（78 个，受 fwd_hist 起点限制）
  因此 round52 的「池内 IC 是宽域 4~7 倍」可能是**时间段的差异**（10 月是反弹/反转行情），
  而非域宽度的差异。不控制日期就下结论 = 混淆变量，结论不可用。

【本脚本】
  只取「池内样本足够的那些截面日期」作为公共日期集 D*，
  然后在 D* 上同时算宽域 IC 与池内 IC —— **两个数字来自完全相同的日期、相同的因子值、
  相同的价格、相同的收益口径，唯一变量是「域」**。
  这才是干净的 A/B。

  同时输出：
    - 配对差 ΔIC = 池内 − 宽域 的均值与 t 值（逐截面配对，配对 t 检验）
    - 池内 IC 在 D* 上是否仍显著（NW t）
"""
import json
import os
import math
import glob

HERE = os.path.dirname(os.path.abspath(__file__))
PANEL = os.path.join(HERE, 'data', 'panel_round34.json')
KL = os.path.join(HERE, 'data', 'klines_long.json')
FWD = os.path.join(HERE, 'data', 'fwd_hist')
OUT = os.path.join(HERE, 'audit_round53.json')

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


def paired_t(diffs):
    """配对 t：mean / (sd/sqrt(n))"""
    n = len(diffs)
    if n < 3:
        return None
    m = sum(diffs) / n
    sd = math.sqrt(sum((v - m) ** 2 for v in diffs) / (n - 1))
    if sd == 0:
        return None
    return m / (sd / math.sqrt(n))


def main():
    panel = json.load(open(PANEL, encoding='utf-8'))
    kl = json.load(open(KL, encoding='utf-8'))
    rows = panel['rows']
    all_dates_panel = sorted(rows.keys())

    all_dates = set()
    for c, d in kl.items():
        all_dates |= set(d.keys())
    cal = sorted(all_dates)
    pos = {d: i for i, d in enumerate(cal)}

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

    def ic_on(dim, dt, fwd, domain):
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
            return None, 0
        ic = spearman(fv, rv)
        return (ic, len(fv)) if ic is not None else (None, len(fv))

    out = {
        'round': 53,
        'purpose': '同日期配对的域宽对照（消除 round52 的时间段混淆）',
        'method': ('在「池内样本足够 + 宽域样本足够」的公共日期集 D* 上，同时算池内 IC 与宽域 IC，'
                   '唯一变量为域；另做逐截面配对 t 检验（ΔIC = 池内 − 宽域）'),
        'caveat': '收盘/收盘收益、NW lag=4、min_xs=20；与 factor_ic.json 的 t+1 开盘扣20bp 口径不同，不可直接比数值',
        'nw_lag': NW_LAG,
        'min_xs': MIN_XS,
        'dims': {},
    }

    for dim in DIMS:
        out['dims'][dim] = {}
        for h in HOPS:
            pool_ics, all_ics, off_ics, diffs, dates_used = [], [], [], [], []
            for dt in all_dates_panel:
                i = pos.get(dt)
                if i is None or i + h >= len(cal):
                    continue
                fwd = cal[i + h]
                icp, npool = ic_on(dim, dt, fwd, 'pool')
                if icp is None or npool < MIN_XS:
                    continue          # 必须有足够的池内样本
                ica, nall = ic_on(dim, dt, fwd, 'all')
                if ica is None or nall < MIN_XS:
                    continue
                ico, _ = ic_on(dim, dt, fwd, 'offpool')
                pool_ics.append(icp)
                all_ics.append(ica)
                off_ics.append(ico)
                diffs.append(icp - ica)
                dates_used.append(dt)

            if len(pool_ics) < MIN_XS:
                out['dims'][dim][str(h)] = {'n_matched': len(pool_ics),
                                            'note': f'公共截面仅 {len(pool_ics)} < {MIN_XS}，无法做配对检验'}
                continue

            mp = sum(pool_ics) / len(pool_ics)
            ma = sum(all_ics) / len(all_ics)
            md = sum(diffs) / len(diffs)
            valid_off = [v for v in off_ics if v is not None]
            mo = sum(valid_off) / len(valid_off) if valid_off else None
            out['dims'][dim][str(h)] = {
                'n_matched': len(pool_ics),
                'date_range': [dates_used[0], dates_used[-1]],
                'pool_ic': round(mp, 4),
                'pool_nw_t': round(nw_t(pool_ics), 2) if nw_t(pool_ics) is not None else None,
                'all_ic': round(ma, 4),
                'all_nw_t': round(nw_t(all_ics), 2) if nw_t(all_ics) is not None else None,
                'offpool_ic': round(mo, 4) if mo is not None else None,
                'offpool_nw_t': round(nw_t(valid_off), 2) if valid_off and nw_t(valid_off) is not None else None,
                'delta_pool_minus_all': round(md, 4),
                'paired_t': round(paired_t(diffs), 2) if paired_t(diffs) is not None else None,
                'ratio_pool_over_all': round(mp / ma, 2) if abs(ma) > 1e-9 else None,
            }

    json.dump(out, open(OUT, 'w', encoding='utf-8'), ensure_ascii=False, indent=1)

    # ---- 打印 ----
    for h in HOPS:
        print(f"\n{'='*104}")
        print(f"  h = {h} 日   （同日期配对，唯一变量 = 域）")
        print(f"{'='*104}")
        print(f"{'dim':20s} {'n配对':>5s} {'池内IC':>8s} {'池内t':>7s} {'宽域IC':>8s} {'宽域t':>7s} "
              f"{'域外IC':>8s} {'Δ(池-宽)':>9s} {'配对t':>7s} {'倍数':>6s}")
        print('-' * 104)
        for dim in DIMS:
            r = out['dims'][dim][str(h)]
            if 'note' in r:
                print(f"{dim:20s} {r['n_matched']:5d}  {r['note']}")
                continue

            def f2(v):
                return f"{v:+.4f}" if isinstance(v, (int, float)) else '   —'

            def ft(v):
                return f"{v:+.2f}" if isinstance(v, (int, float)) else '  —'

            ratio = r.get('ratio_pool_over_all')
            print(f"{dim:20s} {r['n_matched']:5d} {f2(r['pool_ic']):>8s} {ft(r['pool_nw_t']):>7s} "
                  f"{f2(r['all_ic']):>8s} {ft(r['all_nw_t']):>7s} {f2(r['offpool_ic']):>8s} "
                  f"{f2(r['delta_pool_minus_all']):>9s} {ft(r['paired_t']):>7s} "
                  f"{(f'{ratio:.1f}x' if ratio else '—'):>6s}")
        print('-' * 104)
        print(f"  公共日期区间：{out['dims'][DIMS[0]][str(h)].get('date_range')}")

    print(f"\n✅ 写出 {os.path.basename(OUT)}")


if __name__ == '__main__':
    main()
