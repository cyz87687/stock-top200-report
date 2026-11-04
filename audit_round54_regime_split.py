#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
audit_round54_regime_split.py —— 时间分段稳定性：判断负 IC 是「regime」还是「结构性失效」

【要回答的问题】
  round51 宽域 quality h20 = +0.0357 (t=+2.37, n=219, 2025-11~2026-09)
  round53 配对 quality h20 = -0.0393 (t=-1.33, n=63,  2026-06~2026-09)
  同一维度、同一份数据，符号相反 —— 必须判断这是：
    (a) regime 变化（某段行情里因子失效，属正常）
    (b) 结构性失效（因子本身没信息）
  判据：把全区间均匀切段，看 IC 符号是否稳定、分段均值是否单调漂移。

【做法】
  在宽域（2029 只，238 截面）上，把截面按时间等分成 K=4 段，
  每段内算 IC 均值与 NW t。若某段符号与其他段相反 → regime；
  若各段都近零且符号随机跳 → 结构性无效。

  同时对「池内域」做同样分段（只有 78~82 个截面，K=2）。
"""
import json
import os
import math
import glob

HERE = os.path.dirname(os.path.abspath(__file__))
PANEL = os.path.join(HERE, 'data', 'panel_round34.json')
KL = os.path.join(HERE, 'data', 'klines_long.json')
FWD = os.path.join(HERE, 'data', 'fwd_hist')
OUT = os.path.join(HERE, 'audit_round54.json')

MIN_XS = 20
NW_LAG = 4
DIMS = ['score_quality', 'score_ep', 'score_size_ind', 'score_lowvol20',
        'score_bias60', 'score_rsi_rev', 'score_tech_neg', 'score_fund']
HOPS = [5, 20]
K_WIDE = 4


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
    dts = sorted(rows.keys())

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

    def ic_one(dim, dt, fwd, domain):
        snap = rows[dt]
        mem = pool_members.get(dt)
        fv, rv = [], []
        for code, vals in snap.items():
            v = vals.get(dim)
            if not isinstance(v, (int, float)):
                continue
            if domain == 'pool' and (not mem or code not in mem):
                continue
            c0 = close(code, dt)
            c1 = close(code, fwd)
            if not c0 or not c1 or c0 <= 0:
                continue
            fv.append(float(v))
            rv.append(c1 / c0 - 1.0)
        if len(fv) < MIN_XS or max(fv) - min(fv) < 1e-12:
            return None
        return spearman(fv, rv)

    def series(dim, h, domain):
        out = []
        for dt in dts:
            i = pos.get(dt)
            if i is None or i + h >= len(cal):
                continue
            ic = ic_one(dim, dt, cal[i + h], domain)
            if ic is not None:
                out.append((dt, ic))
        return out

    out = {
        'round': 54,
        'purpose': '时间分段稳定性：区分 regime 变化与结构性失效',
        'k_wide': K_WIDE,
        'caveat': '收盘/收盘收益、NW lag=4；分段内截面数较少，|t| 仅供量级参考',
        'dims': {},
    }

    for dim in DIMS:
        out['dims'][dim] = {}
        for h in HOPS:
            rec = {}
            # --- 宽域 4 段 ---
            sw = series(dim, h, 'all')
            if len(sw) >= K_WIDE * 5:
                k = len(sw) // K_WIDE
                segs = []
                for gi in range(K_WIDE):
                    lo = gi * k
                    hi = len(sw) if gi == K_WIDE - 1 else (gi + 1) * k
                    part = sw[lo:hi]
                    vals = [v for _, v in part]
                    m = sum(vals) / len(vals)
                    t = nw_t(vals)
                    segs.append({
                        'seg': gi + 1,
                        'date_range': [part[0][0], part[-1][0]],
                        'n': len(vals),
                        'ic_mean': round(m, 4),
                        'nw_t': round(t, 2) if t is not None else None,
                    })
                signs = [1 if s['ic_mean'] > 0 else -1 for s in segs]
                rec['wide_segments'] = segs
                rec['wide_sign_consistent'] = (len(set(signs)) == 1)
                rec['wide_signs'] = signs
                allv = [v for _, v in sw]
                rec['wide_full'] = {'n': len(allv), 'ic_mean': round(sum(allv) / len(allv), 4),
                                    'nw_t': round(nw_t(allv), 2) if nw_t(allv) is not None else None,
                                    'date_range': [sw[0][0], sw[-1][0]]}
            # --- 池内 2 段 ---
            sp = series(dim, h, 'pool')
            if len(sp) >= 8:
                k2 = len(sp) // 2
                segs = []
                for gi, part in enumerate([sp[:k2], sp[k2:]]):
                    if not part:
                        continue
                    vals = [v for _, v in part]
                    m = sum(vals) / len(vals)
                    t = nw_t(vals)
                    segs.append({'seg': gi + 1, 'date_range': [part[0][0], part[-1][0]],
                                 'n': len(vals), 'ic_mean': round(m, 4),
                                 'nw_t': round(t, 2) if t is not None else None})
                signs = [1 if s['ic_mean'] > 0 else -1 for s in segs]
                rec['pool_segments'] = segs
                rec['pool_sign_consistent'] = (len(set(signs)) == 1)
                allv = [v for _, v in sp]
                rec['pool_full'] = {'n': len(allv), 'ic_mean': round(sum(allv) / len(allv), 4),
                                    'nw_t': round(nw_t(allv), 2) if nw_t(allv) is not None else None,
                                    'date_range': [sp[0][0], sp[-1][0]]}
            out['dims'][dim][str(h)] = rec

    json.dump(out, open(OUT, 'w', encoding='utf-8'), ensure_ascii=False, indent=1)

    # ---- 打印 ----
    for h in HOPS:
        print(f"\n{'='*100}")
        print(f"  h = {h} 日   宽域分段 K={K_WIDE}")
        print(f"{'='*100}")
        for dim in DIMS:
            r = out['dims'][dim][str(h)]
            if 'wide_segments' not in r:
                print(f"{dim:20s} 样本不足")
                continue
            full = r['wide_full']
            icon = '✓稳定' if r['wide_sign_consistent'] else '✗翻转'
            print(f"{dim:20s} 全区间 IC={full['ic_mean']:+.4f} t={full['nw_t']:+.2f} n={full['n']:>3d} "
                  f"[{full['date_range'][0]}~{full['date_range'][1]}]  {icon}")
            for s in r['wide_segments']:
                bar = '█' * min(20, int(abs(s['ic_mean']) * 200))
                sign = '+' if s['ic_mean'] > 0 else '-'
                print(f"    段{s['seg']} {s['date_range'][0]}~{s['date_range'][1]} "
                      f"n={s['n']:>3d} IC={s['ic_mean']:+.4f} t={s['nw_t']:+.2f} {sign}{bar}")
            print()

    print(f"✅ 写出 {os.path.basename(OUT)}")


if __name__ == '__main__':
    main()
