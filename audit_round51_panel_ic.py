#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
audit_round51_panel_ic.py —— 用 238 截面长面板补算「factor_ic.json 证据不足」的维度

【为什么必须做】
  factor_ic.json（2026-10-29 01:55 生成）里：
    score_quality     n=2     w=0.120
    score_size_growth n=2     w=0.080
    score_tech_neg    n=2     w=0.030
    score_ep          n=0     w=0.120
  合计权重 0.35 —— 超过三分之一的总权重，IC 证据几乎为零（n<min_sample=20 只能显示 None）。
  但 data/panel_round34.json 内有 238 截面 × 2029 只的长面板，这几维覆盖率中位数 ~2000。
  ⇒ 不是数据没有，是 factor_ic.json 的证据链只读 factor_history.json（87 截面，
     且其中 84 个只存了 fund/bias60/rsi_rev 三字段），把长面板彻底绕过了。

【口径说明（必须诚实标注）】
  1. 面板来自 panel_round34.json（v4.12 的 8 维产物），**不含** v4.14 的 score_size_growth /
     score_dominant，只有一个 score_size_ind。故：
       - size_growth / dominant **无法**从本面板补算 → 本脚本明确输出 not_available
       - 但 size_ind（原始 0.16 权重口径）可算，作为「市值维」的代理证据，必须标注为旧维
  2. score_ep 在面板里 null 极多（cover_med=1557），只对有值截面计算。
  3. 收益口径：t 日因子值 → t+h 个交易日后的**收盘价/收盘价**收益（面板 price_source=klines_long）。
     与 factor_ic.json 的「t+1 开盘买入」可交易口径**不同**，故数值不可直接对比，
     只能看符号与显著性量级。此点写入输出 json 的 caveat。
  4. NW t：Bartlett 核，lag=4（与 factor_eval 一致）。
  5. min_sample=20：截面内有效样本 <20 只的截面丢弃（避免小样本噪声）。
"""
import json
import os
import math

HERE = os.path.dirname(os.path.abspath(__file__))
PANEL = os.path.join(HERE, 'data', 'panel_round34.json')
KL = os.path.join(HERE, 'data', 'klines_long.json')
OUT = os.path.join(HERE, 'audit_round51.json')

MIN_XS = 20         # 单截面最少标的数
NW_LAG = 4


def _rank(vals):
    """平均秩（处理并列）"""
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
    """Newey-West HAC t（Bartlett 核）"""
    n = len(xs)
    if n < 3:
        return None
    mean = sum(xs) / n
    d = [v - mean for v in xs]
    var = sum(v * v for v in d) / n
    for L in range(1, min(lag, n - 1) + 1):
        w = 1.0 - L / (lag + 1.0)
        cov = sum(d[t] * d[t - L] for t in range(L, n)) / n
        var += 2.0 * w * cov
    if var <= 0:
        return None
    se = math.sqrt(var / n)
    if se == 0:
        return None
    return mean / se


def main():
    panel = json.load(open(PANEL, encoding='utf-8'))
    kl = json.load(open(KL, encoding='utf-8'))
    rows = panel['rows']            # {date: {code: {dim: val}}}
    dates = sorted(rows.keys())

    # 交易日序列（用价格库的真实交易日，避免面板日期与价格日期错位）
    all_dates = set()
    for c, d in kl.items():
        all_dates |= set(d.keys())
    cal = sorted(all_dates)
    pos = {d: i for i, d in enumerate(cal)}

    # 收价查询
    def close(code, dt):
        d = kl.get(code)
        if not d:
            return None
        row = d.get(dt)
        if not row or not isinstance(row, list) or len(row) < 2:
            return None
        return row[1]

    DIMS = ['score_fund', 'score_quality', 'score_ep', 'score_lowvol20',
            'score_bias60', 'score_rsi_rev', 'score_tech_neg', 'score_size_ind']
    HOPS = [1, 5, 20]

    result = {
        'round': 51,
        'purpose': '用 238 截面长面板补算 factor_ic.json 中证据不足的维度（quality/ep/tech_neg/size_ind）',
        'panel': os.path.basename(PANEL),
        'panel_dates': [dates[0], dates[-1]],
        'panel_n_dates': len(dates),
        'price_source': 'klines_long（收盘价对收盘价）',
        'min_xs': MIN_XS,
        'nw_lag': NW_LAG,
        'caveat': ('收益为收盘/收盘口径，与 factor_ic.json 的「t+1 开盘买入、扣20bp」可交易口径不同，'
                   '数值不可直接对比，只看符号与显著性量级；且本面板为 v4.12 八维产物，'
                   '无 score_size_growth / score_dominant，市值维只有旧 score_size_ind 可算。'),
        'dims': {},
        'not_available': {
            'score_size_growth': 'panel_round34 无该字段（v4.12 时尚未拆分 A/B 维）',
            'score_dominant': 'panel_round34 无该字段（v4.12 时尚未拆分 A/B 维）',
        },
    }

    for dim in DIMS:
        per_hop = {}
        for h in HOPS:
            xs = []
            used_dates = []
            n_codes_list = []
            for dt in dates:
                i = pos.get(dt)
                if i is None or i + h >= len(cal):
                    continue
                fwd = cal[i + h]
                snap = rows[dt]
                fv, rv = [], []
                for code, vals in snap.items():
                    v = vals.get(dim)
                    if not isinstance(v, (int, float)):
                        continue
                    c0 = close(code, dt)
                    c1 = close(code, fwd)
                    if not c0 or not c1 or c0 <= 0:
                        continue
                    fv.append(float(v))
                    rv.append(c1 / c0 - 1.0)
                if len(fv) < MIN_XS:
                    continue
                # 因子值全同 → Spearman 无定义
                if max(fv) - min(fv) < 1e-12:
                    continue
                ic = spearman(fv, rv)
                if ic is None:
                    continue
                xs.append(ic)
                used_dates.append(dt)
                n_codes_list.append(len(fv))
            if len(xs) < MIN_XS:
                per_hop[str(h)] = {'n': len(xs), 'ic_mean': None, 'nw_t': None,
                                   'note': f'有效截面 {len(xs)} < {MIN_XS}，样本不足'}
                continue
            mean = sum(xs) / len(xs)
            t = nw_t(xs)
            win = sum(1 for v in xs if v > 0) / len(xs) * 100.0
            std = math.sqrt(sum((v - mean) ** 2 for v in xs) / len(xs))
            per_hop[str(h)] = {
                'n': len(xs),
                'ic_mean': round(mean, 4),
                'ic_std': round(std, 4),
                'nw_t': round(t, 2) if t is not None else None,
                'win_rate': round(win, 1),
                'med_codes_per_xs': sorted(n_codes_list)[len(n_codes_list) // 2],
                'first_date': used_dates[0],
                'last_date': used_dates[-1],
            }
        result['dims'][dim] = per_hop

    # Bonferroni：4 维 × 3 周期 = 12 次检验，双尾 0.05 → |t| ≥ 2.87
    result['bonferroni'] = {
        'n_tests': 12,
        'threshold_t': 2.87,
        'note': '4 维 × 3 周期 = 12 次检验，Bonferroni 校正后双尾 0.05 对应 |t| ≥ 2.87',
    }

    # 与 factor_ic.json 对照
    try:
        fic = json.load(open(os.path.join(HERE, 'factor_ic.json'), encoding='utf-8'))
        cmp = {}
        for dim in DIMS:
            f = (fic.get('factors') or {}).get(dim)
            if not f:
                cmp[dim] = {'in_factor_ic': False}
                continue
            hh = (f.get('horizons') or {}).get('5') or {}
            cmp[dim] = {
                'in_factor_ic': True,
                'factor_ic_h5_n': hh.get('n'),
                'factor_ic_h5_ic': hh.get('ic_mean'),
                'panel_h5_n': (result['dims'][dim].get('5') or {}).get('n'),
                'panel_h5_ic': (result['dims'][dim].get('5') or {}).get('ic_mean'),
            }
        result['compare_factor_ic'] = cmp
    except Exception as e:
        result['compare_factor_ic'] = {'error': str(e)}

    with open(OUT, 'w', encoding='utf-8') as f:
        json.dump(result, f, ensure_ascii=False, indent=1)

    # ---- 打印 ----
    print(f"面板 {result['panel_dates'][0]} ~ {result['panel_dates'][1]}  {result['panel_n_dates']} 截面")
    print(f"Bonferroni: {result['bonferroni']['n_tests']} 次检验 → |t| ≥ {result['bonferroni']['threshold_t']}")
    print()
    hdr = f"{'dim':20s} {'h':>3s} {'n':>4s} {'IC':>8s} {'NWt':>7s} {'win%':>6s} {'medN':>5s} {'区间':>24s}"
    print(hdr)
    print('-' * len(hdr))
    for dim in DIMS:
        for h in HOPS:
            d = result['dims'][dim][str(h)]
            ic = f"{d['ic_mean']:+.4f}" if d.get('ic_mean') is not None else '—'
            t = f"{d['nw_t']:+.2f}" if d.get('nw_t') is not None else '—'
            wr = f"{d['win_rate']:.1f}" if d.get('win_rate') is not None else '—'
            mn = str(d.get('med_codes_per_xs', '—'))
            rg = f"{d.get('first_date','—')}~{d.get('last_date','—')}" if d.get('first_date') else '—'
            print(f"{dim:20s} {h:3d} {d['n']:4d} {ic:>8s} {t:>7s} {wr:>6s} {mn:>5s} {rg:>24s}")
        print('-' * len(hdr))
    print()
    print('== 与 factor_ic.json 的 h5 对照 ==')
    for dim, v in result.get('compare_factor_ic', {}).items():
        if not isinstance(v, dict) or not v.get('in_factor_ic', True):
            print(f"{dim:20s} 不在 factor_ic.json")
            continue
        print(f"{dim:20s} factor_ic n={v.get('factor_ic_h5_n')} IC={v.get('factor_ic_h5_ic')}   |   "
              f"长面板 n={v.get('panel_h5_n')} IC={v.get('panel_h5_ic')}")
    print()
    print(f"✅ 写出 {os.path.basename(OUT)}")


if __name__ == '__main__':
    main()
