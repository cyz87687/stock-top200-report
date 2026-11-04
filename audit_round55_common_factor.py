#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
audit_round55_common_factor.py —— 决定性诊断：段4「全体维度同时有效」是各自 alpha 还是同一波行情？

【为什么必须查】
  round54 显示：2026-07-22~2026-10-21（段4）几乎所有维度 IC 同时转强且同号：
      size_ind  +0.2479 (t=+21.38)
      lowvol20  +0.1864 (t=+ 2.42)
      rsi_rev   +0.1627 (t=+ 3.15)
      tech_neg  +0.1398 (t=+ 2.65)
      bias60    +0.1170 (t=+ 1.58)
  5 个方向完全不同的因子在同一段同时高正 IC，且 size_ind 的 t=21 高到异常
  ⇒ 高度可疑：不是 5 个独立 alpha，而是**它们共享一个暴露**（小市值/流动性），
     该暴露在这一段被市场单边奖励。若为真，则「IC 高」≠「因子有效」，
     权重实证基础需要重写。

【检验】
  1. 逐截面算各维 IC 的时间序列，计算「各维 IC 序列 与 size_ind IC 序列 的相关系数」
     → 若段4 内相关普遍 >0.6，说明同源。
  2. 直接算各维**因子值本身**与 size_ind 因子值的截面 Spearman 相关
     → 若 size_ind 与 lowvol20/rsi_rev 本身高度相关，则暴露重叠是结构性的。
  3. 「同质化检验」：把 size_ind 从收益里剥掉（对 size_ind 做截面回归取残差收益），
     重算各维 IC → 若 IC 大幅塌陷，则证明原 IC 主要来自 size 暴露。
     这是最干净的一刀。
"""
import json
import os
import math
import glob

HERE = os.path.dirname(os.path.abspath(__file__))
PANEL = os.path.join(HERE, 'data', 'panel_round34.json')
KL = os.path.join(HERE, 'data', 'klines_long.json')
OUT = os.path.join(HERE, 'audit_round55.json')

MIN_XS = 20
NW_LAG = 4
DIMS = ['score_quality', 'score_ep', 'score_lowvol20', 'score_bias60',
        'score_rsi_rev', 'score_tech_neg', 'score_size_ind']
HOPS = [5, 20]


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


def residualize(y, x):
    """对 y 关于 x 做简单线性回归，返回残差（截面内）"""
    n = len(y)
    if n < 3:
        return y
    mx = sum(x) / n
    my = sum(y) / n
    sxx = sum((v - mx) ** 2 for v in x)
    if sxx <= 0:
        return y
    sxy = sum((a - mx) * (b - my) for a, b in zip(x, y))
    b = sxy / sxx
    a = my - b * mx
    return [v - (a + b * xv) for v, xv in zip(y, x)]


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

    def close(code, dt):
        d = kl.get(code)
        if not d:
            return None
        row = d.get(dt)
        if not row or not isinstance(row, list) or len(row) < 2:
            return None
        return row[1]

    # 段4 区间
    SEG4 = ('2026-07-22', '2026-10-21')

    out = {
        'round': 55,
        'purpose': '检验段4「全体维度同时有效」是否为共享 size 暴露的单一行情所致',
        'seg4': list(SEG4),
        'caveat': ('收盘/收盘收益；残差化用简单 OLS 对 size_ind 因子值回归（非多因子正交）。'
                   '⚠️ score_size_ind 自身的「剥size后IC」是恒等式陷阱：把 y 对 x 回归取残差后'
                   '再与 x 求相关，数学上必然得到负值，无任何信息量，禁止引用该行做结论。'),
        'identity_trap': 'score_size_ind 行不可解读（对自身残差化→恒为负）',
        'panel_value_note': ('panel_round34 中 quality/ep/lowvol20/size_ind 为 rank 百分位化后的值'
                             '（唯一值 1001、均值恰 5.000、min 0 / max 10），Spearman 对单调变换不变，'
                             '故 IC 口径有效；bias60/rsi_rev 仅 5 个整数档（1~5），存在大量并列，'
                             '以平均秩处理，档位分辨率粗会低估 IC 绝对值。'),
        'dims': {},
    }

    # ---------- 检验1 & 3：每日 IC 序列 + size 残差化后的 IC ----------
    per_hop = {}
    for h in HOPS:
        ic_series = {d: {} for d in DIMS}     # dim -> {date: ic}
        ric_series = {d: {} for d in DIMS}    # 残差化后
        for dt in dts:
            i = pos.get(dt)
            if i is None or i + h >= len(cal):
                continue
            fwd = cal[i + h]
            snap = rows[dt]
            # 收集统一样本（所有维度都有值 + size_ind 有值）
            rec = {}
            for code, vals in snap.items():
                v_si = vals.get('score_size_ind')
                if not isinstance(v_si, (int, float)):
                    continue
                c0 = close(code, dt)
                c1 = close(code, fwd)
                if not c0 or not c1 or c0 <= 0:
                    continue
                rec[code] = (vals, c1 / c0 - 1.0)
            if len(rec) < MIN_XS:
                continue
            codes = list(rec.keys())
            si_vals = [float(rec[c][0]['score_size_ind']) for c in codes]
            if max(si_vals) - min(si_vals) < 1e-12:
                continue
            for dim in DIMS:
                fv, rv = [], []
                for c in codes:
                    v = rec[c][0].get(dim)
                    if not isinstance(v, (int, float)):
                        continue
                    fv.append(float(v))
                    rv.append(rec[c][1])
                if len(fv) < MIN_XS or max(fv) - min(fv) < 1e-12:
                    continue
                ic = spearman(fv, rv)
                if ic is not None:
                    ic_series[dim][dt] = ic
                # 残差化：收益对 size_ind 回归取残差，再与因子做 Spearman
                fv2, rv2, si2 = [], [], []
                for c in codes:
                    v = rec[c][0].get(dim)
                    if not isinstance(v, (int, float)):
                        continue
                    fv2.append(float(v))
                    rv2.append(rec[c][1])
                    si2.append(float(rec[c][0]['score_size_ind']))
                if len(fv2) >= MIN_XS:
                    res = residualize(rv2, si2)
                    ric = spearman(fv2, res)
                    if ric is not None:
                        ric_series[dim][dt] = ric

        rec_out = {}
        for dim in DIMS:
            s = ic_series[dim]
            rs = ric_series[dim]
            # 全区间
            allv = [s[dt] for dt in sorted(s)]
            # 段4
            s4 = [s[dt] for dt in sorted(s) if SEG4[0] <= dt <= SEG4[1]]
            r4 = [rs[dt] for dt in sorted(rs) if SEG4[0] <= dt <= SEG4[1]]
            # 与 size_ind IC 序列的相关（段4）
            si4 = [ic_series['score_size_ind'][dt] for dt in sorted(ic_series['score_size_ind'])
                   if SEG4[0] <= dt <= SEG4[1]]
            common = [dt for dt in sorted(s) if SEG4[0] <= dt <= SEG4[1]
                      and dt in ic_series['score_size_ind']]
            if dim != 'score_size_ind' and len(common) >= 5:
                a = [s[dt] for dt in common]
                b = [ic_series['score_size_ind'][dt] for dt in common]
                corr_with_size = _pearson(a, b)
            else:
                corr_with_size = None

            rec_out[dim] = {
                'full': {'n': len(allv), 'ic': round(sum(allv) / len(allv), 4) if allv else None,
                         't': round(nw_t(allv), 2) if allv and nw_t(allv) is not None else None},
                'seg4_raw': {'n': len(s4), 'ic': round(sum(s4) / len(s4), 4) if s4 else None,
                             't': round(nw_t(s4), 2) if s4 and nw_t(s4) is not None else None},
                'seg4_resid': {'n': len(r4), 'ic': round(sum(r4) / len(r4), 4) if r4 else None,
                               't': round(nw_t(r4), 2) if r4 and nw_t(r4) is not None else None},
                'seg4_ic_corr_with_size': round(corr_with_size, 3) if corr_with_size is not None else None,
            }
            if r4 and s4:
                base = sum(s4) / len(s4)
                resd = sum(r4) / len(r4)
                rec_out[dim]['seg4_shrink_pct'] = round((1 - resd / base) * 100, 1) if abs(base) > 1e-9 else None
        per_hop[str(h)] = rec_out
    out['dims'] = per_hop

    # ---------- 检验2：因子值本身的截面相关（段4 首日 + 全区间均值） ----------
    pair_corr = {}
    accum = {}
    for dt in dts:
        if not (SEG4[0] <= dt <= SEG4[1]):
            continue
        snap = rows[dt]
        for i, d1 in enumerate(DIMS):
            for d2 in DIMS[i + 1:]:
                xs, ys = [], []
                for code, vals in snap.items():
                    v1 = vals.get(d1)
                    v2 = vals.get(d2)
                    if isinstance(v1, (int, float)) and isinstance(v2, (int, float)):
                        xs.append(float(v1))
                        ys.append(float(v2))
                if len(xs) < 50:
                    continue
                c = spearman(xs, ys)
                if c is not None:
                    accum.setdefault(f'{d1}|{d2}', []).append(c)
    for k, v in accum.items():
        pair_corr[k] = {'mean_corr': round(sum(v) / len(v), 3), 'n': len(v)}
    out['seg4_factor_value_corr'] = dict(sorted(pair_corr.items(),
                                                key=lambda kv: -abs(kv[1]['mean_corr'])))

    json.dump(out, open(OUT, 'w', encoding='utf-8'), ensure_ascii=False, indent=1)

    # ---- 打印 ----
    for h in HOPS:
        print(f"\n{'='*104}")
        print(f"  h = {h} 日   段4（{SEG4[0]}~{SEG4[1]}）原始 IC vs 剥掉 size 暴露后的 IC")
        print(f"{'='*104}")
        print(f"{'dim':20s} {'段4原始IC':>10s} {'t':>7s} {'剥size后IC':>11s} {'t':>7s} "
              f"{'塌陷%':>7s} {'IC序列与size相关':>16s}")
        print('-' * 104)
        for dim in DIMS:
            r = per_hop[str(h)][dim]
            raw = r['seg4_raw']
            res = r['seg4_resid']
            sh = r.get('seg4_shrink_pct')
            cs = r.get('seg4_ic_corr_with_size')

            def f4(d):
                return f"{d['ic']:+.4f}" if d['ic'] is not None else '   —'

            def ft(d):
                return f"{d['t']:+.2f}" if d['t'] is not None else '  —'

            print(f"{dim:20s} {f4(raw):>10s} {ft(raw):>7s} {f4(res):>11s} {ft(res):>7s} "
                  f"{(f'{sh:.1f}%' if sh is not None else '—'):>7s} "
                  f"{(f'{cs:+.3f}' if cs is not None else '—'):>16s}")
        print('-' * 104)

    print(f"\n{'='*104}")
    print("  段4 因子值之间的截面 Spearman 相关（暴露重叠度，|ρ|>0.5 即高度同源）")
    print(f"{'='*104}")
    for k, v in list(out['seg4_factor_value_corr'].items())[:14]:
        bar = '█' * min(24, int(abs(v['mean_corr']) * 30))
        print(f"  {k:44s} ρ={v['mean_corr']:+.3f}  n={v['n']:>3d}  {bar}")

    print(f"\n✅ 写出 {os.path.basename(OUT)}")


if __name__ == '__main__':
    main()
