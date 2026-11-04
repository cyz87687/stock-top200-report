#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
audit_round60_candidate_robust.py —— 候选权重方案的稳健性检验（选方案不能只看全区间）

【为什么必须做】
  round59 在 238 截面上既选方案又验证 → **探索性**，属多重检验（8 候选 × 3 周期）。
  且 round54 已证明：IC 集中在段4（2026-07-22 后），全区间 IC 高 ≠ 跨 regime 稳。
  ⇒ 候选方案必须做「分段不劣化」验收，这是老大定的强制流程之一：
     「分子期双正 + 三方向互证 + 前后段均不劣化」。

【四道检验】
  T1 分段不劣化：K=4 分段，方案在后段不得比基线显著更差（配对 t > -2）
  T2 前后段双正：前段 IC 与后段 IC 同号（不能一段正一段负）
  T3 三方向互证：宽域 / 池内 / 池外三个域同号
  T4 约束合规：单因子 ≤0.20、大类 ≤0.30、Σ=1

【输出】
  audit_round60.json + 每候选的验收结论（PASS/FAIL 及失败原因）
"""
import json
import os
import math
import glob

HERE = os.path.dirname(os.path.abspath(__file__))
PANEL = os.path.join(HERE, 'data', 'panel_round34.json')
KL = os.path.join(HERE, 'data', 'klines_long.json')
FWD = os.path.join(HERE, 'data', 'fwd_hist')
R59 = os.path.join(HERE, 'audit_round59.json')
OUT = os.path.join(HERE, 'audit_round60.json')

MIN_XS = 30
NW_LAG = 4
HOPS = [1, 5, 20]
K = 4

PANEL_GROUPS = {
    '基本面': ['score_fund', 'score_quality'],
    '估值': ['score_ep'],
    '低波': ['score_lowvol20'],
    '技术反转': ['score_bias60', 'score_rsi_rev', 'score_tech_neg'],
    '市值': ['score_size_ind'],
}


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


def paired(diffs):
    n = len(diffs)
    if n < 3:
        return None
    m = sum(diffs) / n
    sd = math.sqrt(sum((v - m) ** 2 for v in diffs) / (n - 1))
    if sd == 0:
        return None
    return m / (sd / math.sqrt(n))


def composite(row, wmap):
    num = 0.0
    den = 0.0
    for k, w in wmap.items():
        v = row.get(k)
        if isinstance(v, (int, float)) and not (v != v):
            num += float(v) * w
            den += w
    return num / den if den > 0 else None


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

    r59 = json.load(open(R59, encoding='utf-8'))
    cands = {k: v for k, v in r59['candidates'].items() if not k.startswith('_基线')}
    baseline = r59['candidates']['_基线_现权重面板映射']['weights']

    def ic_series(wmap, h, domain='all'):
        """逐截面 IC；domain: all / pool / offpool"""
        xs, ds = [], []
        for dt in dts:
            i = pos.get(dt)
            if i is None or i + h >= len(cal):
                continue
            fwd = cal[i + h]
            snap = rows[dt]
            mem = pool_members.get(dt)
            sv, rv = [], []
            for code, vals in snap.items():
                if domain == 'pool' and (not mem or code not in mem):
                    continue
                if domain == 'offpool' and mem and code in mem:
                    continue
                s = composite(vals, wmap)
                if s is None:
                    continue
                c0 = close(code, dt)
                c1 = close(code, fwd)
                if not c0 or not c1 or c0 <= 0:
                    continue
                sv.append(s)
                rv.append(c1 / c0 - 1.0)
            if len(sv) < MIN_XS or max(sv) - min(sv) < 1e-12:
                continue
            ic = spearman(sv, rv)
            if ic is not None:
                xs.append(ic)
                ds.append(dt)
        return xs, ds

    def seg_split(xs, ds, k=K):
        """按时间等分 k 段，返回每段 (ic_mean, dates)"""
        n = len(xs)
        if n < k:
            return []
        step = n // k
        segs = []
        for gi in range(k):
            lo = gi * step
            hi = n if gi == k - 1 else (gi + 1) * step
            part = xs[lo:hi]
            if not part:
                continue
            segs.append({'seg': gi + 1, 'n': len(part),
                         'ic_mean': round(sum(part) / len(part), 4),
                         'dates': [ds[lo], ds[hi - 1]],
                         't': round(nw_t(part), 2) if nw_t(part) is not None else None})
        return segs

    out = {
        'round': 60,
        'purpose': '候选权重方案的稳健性验收（分段不劣化 / 前后段双正 / 三方向互证 / 约束合规）',
        'acceptance_rules': {
            'T1_分段不劣化': '后段（K=4 的第4段）配对 t（候选−基线）不得 < -2.0',
            'T2_前后段双正': '前段(1-3段合并)与后段(第4段) IC 同号',
            'T3_三方向互证': '宽域/池内/池外三域 IC 同号',
            'T4_约束合规': '单因子≤0.20 且 大类≤0.30 且 Σ=1',
        },
        'caveat': ('候选在 238 截面上筛选 = 同一批数据既选又验，属探索性；'
                   'T1~T4 只是**入围门槛**，不能替代前向 walk-forward（老大已定其为唯一最终裁决）。'
                   '市值维为 score_size_ind 代理（面板无 size_growth/dominant 字段）。'),
        'results': {},
    }

    for h in HOPS:
        b_all, b_dates = ic_series(baseline, h, 'all')
        rec_all = {}
        for name, crec in cands.items():
            wmap = crec['weights']
            x_all, d_all = ic_series(wmap, h, 'all')
            x_pool, d_pool = ic_series(wmap, h, 'pool')
            x_off, _ = ic_series(wmap, h, 'offpool')

            segs_c = seg_split(x_all, d_all)
            segs_b = seg_split(b_all, b_dates)
            # T1：后段配对
            t1 = None
            t1_fail = ''
            if len(segs_c) == K and len(segs_b) == K:
                sc = x_all[-len(segs_c[-1]['dates']):] if False else None
                # 精确取后段区间
                lo = len(x_all) - segs_c[-1]['n']
                lob = len(b_all) - segs_b[-1]['n']
                m = min(len(x_all) - lo, len(b_all) - lob)
                if m >= 5:
                    diffs = [x_all[lo + i] - b_all[lob + i] for i in range(m)]
                    t1 = round(paired(diffs), 2) if paired(diffs) is not None else None
                    if t1 is not None and t1 < -2.0:
                        t1_fail = f'后段配对 t={t1} < -2.0'

            # T2：前后段双正
            pre_vals = [s['ic_mean'] for s in segs_c[:-1]]
            pre_mean = round(sum(pre_vals) / len(pre_vals), 4) if pre_vals else None
            post_mean = segs_c[-1]['ic_mean'] if segs_c else None
            t2_ok = (pre_mean is not None and post_mean is not None
                     and (pre_mean > 0) == (post_mean > 0))
            t2_fail = '' if t2_ok else f'前段 {pre_mean} 与后段 {post_mean} 异号'

            # T3：三方向互证
            m_pool = sum(x_pool) / len(x_pool) if x_pool else None
            m_off = sum(x_off) / len(x_off) if x_off else None
            m_all = sum(x_all) / len(x_all) if x_all else None
            signs = [1 if (v is not None and v > 0) else (-1 if v is not None else 0)
                     for v in (m_all, m_pool, m_off)]
            t3_ok = all(s != 0 for s in signs) and len(set(signs)) == 1
            t3_fail = '' if t3_ok else f'三域异号 all={m_all} pool={m_pool} off={m_off}'

            # T4：约束
            t4_ok = crec['compliant']
            t4_fail = '' if t4_ok else '; '.join(crec['violations'])

            passes = [t1_fail == '', t2_ok, t3_ok, t4_ok]
            rec_all[name] = {
                'weights': wmap,
                'segments': segs_c,
                'pre_seg_mean': pre_mean, 'post_seg_mean': post_mean,
                'all_ic': round(m_all, 4) if m_all is not None else None,
                'all_t': round(nw_t(x_all), 2) if nw_t(x_all) is not None else None,
                'pool_ic': round(m_pool, 4) if m_pool is not None else None,
                'offpool_ic': round(m_off, 4) if m_off is not None else None,
                'delta_post_seg_paired_t': t1,
                'T1_分段不劣化': 'PASS' if t1_fail == '' else f'FAIL({t1_fail})',
                'T2_前后段双正': 'PASS' if t2_ok else f'FAIL({t2_fail})',
                'T3_三方向互证': 'PASS' if t3_ok else f'FAIL({t3_fail})',
                'T4_约束合规': 'PASS' if t4_ok else f'FAIL({t4_fail})',
                'n_pass': sum(passes),
                'verdict': '全通过' if all(passes) else f'{sum(passes)}/4 通过',
            }
        out['results'][str(h)] = rec_all

    json.dump(out, open(OUT, 'w', encoding='utf-8'), ensure_ascii=False, indent=1)

    # ---- 打印 ----
    for h in HOPS:
        print(f"\n{'='*118}")
        print(f"  h = {h} 日   候选方案稳健性验收")
        print(f"{'='*118}")
        print(f"{'方案':20s} {'全区间IC':>9s} {'t':>6s} {'前段':>9s} {'后段':>9s} "
              f"{'后段Δt':>8s} {'池内':>9s} {'T1':>6s} {'T2':>6s} {'T3':>6s} {'T4':>6s} {'结论':>8s}")
        print('-' * 118)
        for name, r in out['results'][str(h)].items():
            t1 = r['T1_分段不劣化'][:4]
            t2 = r['T2_前后段双正'][:4]
            t3 = r['T3_三方向互证'][:4]
            t4 = r['T4_约束合规'][:4]
            print(f"{name:20s} {r['all_ic']:+9.4f} {r['all_t']:+6.2f} "
                  f"{(r['pre_seg_mean'] if r['pre_seg_mean'] is not None else 0):+9.4f} "
                  f"{(r['post_seg_mean'] if r['post_seg_mean'] is not None else 0):+9.4f} "
                  f"{(r['delta_post_seg_paired_t'] if r['delta_post_seg_paired_t'] is not None else 0):+8.2f} "
                  f"{(r['pool_ic'] if r['pool_ic'] is not None else 0):+9.4f} "
                  f"{t1:>6s} {t2:>6s} {t3:>6s} {t4:>6s} {r['verdict']:>8s}")
        print('-' * 118)
        print('  失败明细：')
        for name, r in out['results'][str(h)].items():
            fails = [f"{k}={v}" for k, v in
                     (('T1', r['T1_分段不劣化']), ('T2', r['T2_前后段双正']),
                      ('T3', r['T3_三方向互证']), ('T4', r['T4_约束合规']))
                     if not v.startswith('PASS')]
            if fails:
                print(f"    {name:20s} {'; '.join(fails)}")

    print(f"\n✅ 写出 {os.path.basename(OUT)}")


if __name__ == '__main__':
    main()
