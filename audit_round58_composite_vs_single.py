#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
audit_round58_composite_vs_single.py —— 决定性检验：现权重复合分是否比单维更差？

【为什么这是本轮最重要的检验】
  factor_ic.json 实测（h1）：
      total（现权重复合）  IC=+0.0215  t=+1.50   不显著
      score_fund（单维）   IC=+0.0411  t=+2.87   显著
      score_lowvol20       IC=+0.0544  t=+1.66
  ⇒ 如果「九维加权后的总分」预测力低于「单用一个 fund 维」，
    那这个 24.2% 权重的讨论本身就是次要问题 —— 真正的问题是**加权方式在稀释信息**。
    必须先证实/证伪，否则所有关于单维权重的讨论都是在优化一个更差的目标。

【做法】
  在**同一批截面、同一份因子值、同一份价格**上，构造多个复合方案并比较：
    C0  现权重 v4.14（Σ=1，缺维从分母剔除）
    C1  仅 fund（单维）
    C2  仅 lowvol20
    C3  fund + lowvol20 各 0.5
    C4  九维等权
    C5  仅显著维（fund + lowvol20 + tech_neg，按其权重重新归一）
    C6  三大独立簇等权（fund / lowvol20 / 技术簇代表 bias60）
  全部用**同一口径**：Spearman(复合分_t, 收益 t→t+h)，NW lag=4。

  数据源：panel_round34（宽域 2029 只，238 截面）—— 唯一能覆盖多维度长时间的选择。
  ⚠️ 面板缺 size_growth/dominant（v4.12 八维），故 C0 用「八维版 v4.12 权重」而非 v4.14 九维，
     但这对「复合 vs 单维」的相对结论无影响，因为所有方案都缺同样两维。
"""
import json
import os
import math

HERE = os.path.dirname(os.path.abspath(__file__))
PANEL = os.path.join(HERE, 'data', 'panel_round34.json')
KL = os.path.join(HERE, 'data', 'klines_long.json')
OUT = os.path.join(HERE, 'audit_round58.json')

MIN_XS = 30
NW_LAG = 4
HOPS = [1, 5, 20]

# 面板可用的 8 维（size_ind 代表市值）
PANEL_DIMS = ['score_fund', 'score_quality', 'score_ep', 'score_lowvol20',
              'score_bias60', 'score_rsi_rev', 'score_tech_neg', 'score_size_ind']

# v4.12 的 8 维权重（面板对应口径），Σ=1
W_V412 = {
    'score_fund': 0.242,
    'score_quality': 0.120,
    'score_ep': 0.120,
    'score_lowvol20': 0.208,
    'score_bias60': 0.070,
    'score_rsi_rev': 0.070,
    'score_tech_neg': 0.030,
    'score_size_ind': 0.140,   # = size_growth 0.08 + dominant 0.06
}

SCHEMES = {
    'C0_现权重8维': W_V412,
    'C1_仅fund': {'score_fund': 1.0},
    'C2_仅lowvol20': {'score_lowvol20': 1.0},
    'C3_fund+lowvol各半': {'score_fund': 0.5, 'score_lowvol20': 0.5},
    'C4_八维等权': {k: 1.0 / len(PANEL_DIMS) for k in PANEL_DIMS},
    'C5_仅显著三维': {'score_fund': 0.242 / 0.51, 'score_lowvol20': 0.208 / 0.51,
                      'score_tech_neg': 0.030 / 0.51},
    'C6_三独立簇等权': {'score_fund': 1 / 3, 'score_lowvol20': 1 / 3,
                        'score_bias60': 1 / 3},
    'C7_去技术簇重叠': {'score_fund': 0.242, 'score_lowvol20': 0.208,
                        'score_quality': 0.120, 'score_ep': 0.120,
                        'score_size_ind': 0.140, 'score_bias60': 0.170},
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


def composite(row, wmap):
    """缺维从分母剔除的加权平均（与生产口径一致，非 or 0）"""
    num = 0.0
    den = 0.0
    for k, w in wmap.items():
        v = row.get(k)
        if isinstance(v, (int, float)) and not (v != v):
            num += float(v) * w
            den += w
    if den <= 0:
        return None
    return num / den


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

    out = {
        'round': 58,
        'purpose': '检验现权重复合分是否劣于单维（即加权是否在稀释信息）',
        'panel': os.path.basename(PANEL),
        'date_range': [dts[0], dts[-1]],
        'n_dates': len(dts),
        'caveat': ('面板为 v4.12 八维（缺 size_growth/dominant），C0 用八维权重代入；'
                   '各方案口径统一为「缺维从分母剔除」，故横向可比。'
                   '收盘/收盘收益，与 factor_ic 的 t+1 开盘扣20bp 不可直接比数值。'),
        'schemes': {k: {kk: round(vv, 4) for kk, vv in v.items()} for k, v in SCHEMES.items()},
        'results': {},
    }

    for h in HOPS:
        res = {name: [] for name in SCHEMES}
        for dt in dts:
            i = pos.get(dt)
            if i is None or i + h >= len(cal):
                continue
            fwd = cal[i + h]
            snap = rows[dt]
            per_scheme = {name: ([], []) for name in SCHEMES}
            for code, vals in snap.items():
                c0 = close(code, dt)
                c1 = close(code, fwd)
                if not c0 or not c1 or c0 <= 0:
                    continue
                ret = c1 / c0 - 1.0
                for name, wmap in SCHEMES.items():
                    s = composite(vals, wmap)
                    if s is None:
                        continue
                    per_scheme[name][0].append(s)
                    per_scheme[name][1].append(ret)
            for name in SCHEMES:
                sv, rv = per_scheme[name]
                if len(sv) < MIN_XS or max(sv) - min(sv) < 1e-12:
                    continue
                ic = spearman(sv, rv)
                if ic is not None:
                    res[name].append(ic)

        rec = {}
        for name, xs in res.items():
            if len(xs) < MIN_XS:
                rec[name] = {'n': len(xs), 'note': '样本不足'}
                continue
            m = sum(xs) / len(xs)
            t = nw_t(xs)
            rec[name] = {
                'n': len(xs),
                'ic_mean': round(m, 4),
                'nw_t': round(t, 2) if t is not None else None,
                'win_rate': round(sum(1 for v in xs if v > 0) / len(xs) * 100.0, 1),
                'ic_std': round(math.sqrt(sum((v - m) ** 2 for v in xs) / len(xs)), 4),
            }
        # 与 C0 配对比较
        base = res['C0_现权重8维']
        for name, xs in res.items():
            if name == 'C0_现权重8维' or len(xs) != len(base) or len(xs) < 3:
                continue
            diffs = [a - b for a, b in zip(xs, base)]
            md = sum(diffs) / len(diffs)
            sd = math.sqrt(sum((v - md) ** 2 for v in diffs) / (len(diffs) - 1)) if len(diffs) > 1 else 0
            pt = md / (sd / math.sqrt(len(diffs))) if sd > 0 else None
            rec[name]['delta_vs_C0'] = round(md, 4)
            rec[name]['paired_t_vs_C0'] = round(pt, 2) if pt is not None else None
        out['results'][str(h)] = rec

    json.dump(out, open(OUT, 'w', encoding='utf-8'), ensure_ascii=False, indent=1)

    # ---- 打印 ----
    for h in HOPS:
        r = out['results'][str(h)]
        print(f"\n{'='*104}")
        print(f"  h = {h} 日   复合方案对比（面板 {out['date_range'][0]}~{out['date_range'][1]}）")
        print(f"{'='*104}")
        print(f"{'scheme':22s} {'n':>4s} {'IC':>9s} {'NWt':>7s} {'胜率%':>7s} {'ICIR':>7s} "
              f"{'ΔvsC0':>9s} {'配对t':>7s}")
        print('-' * 104)
        rank_list = []
        for name in SCHEMES:
            d = r[name]
            if d.get('note'):
                print(f"{name:22s} {d['n']:4d}  {d['note']}")
                continue
            icir = d['ic_mean'] / d['ic_std'] * math.sqrt(len(range(0, 252, h))) if d['ic_std'] > 0 else None
            dv = d.get('delta_vs_C0')
            pt = d.get('paired_t_vs_C0')
            print(f"{name:22s} {d['n']:4d} {d['ic_mean']:+9.4f} {d['nw_t']:+7.2f} "
                  f"{d['win_rate']:6.1f}% {(icir if icir else 0):7.2f} "
                  f"{(f'{dv:+.4f}' if dv is not None else '   —'):>9s} "
                  f"{(f'{pt:+.2f}' if pt is not None else '  —'):>7s}")
            rank_list.append((d['ic_mean'], d['nw_t'], name))
        print('-' * 104)
        rank_list.sort(reverse=True)
        print("  按 IC 排序：")
        for ic, t, name in rank_list:
            bar = '█' * min(30, int(abs(ic) * 250))
            print(f"    {name:22s} IC={ic:+.4f} t={t:+.2f} {bar}")

    print(f"\n✅ 写出 {os.path.basename(OUT)}")


if __name__ == '__main__':
    main()
