#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
audit_round56_discrete_bias.py —— 离散化/并列对 IC 的低估幅度量化

【发现的缺陷】
  panel_round34 里 score_bias60 / score_rsi_rev **只有 5 个整数档（1,2,3,4,5）**：
      2026-10-27: bias60 唯一值=5, rsi_rev 唯一值=5（n≈2000）
      而 quality/ep/lowvol20/size_ind 唯一值=1001（rank 百分位化，连续）
  2000 只股票挤在 5 个档里 → 每档约 400 只并列。Spearman 用平均秩处理并列，
  但**信息已经损失**：档内的排序差异被抹平。这会系统性**低估**这两维的真实 IC，
  而不是高估。故 round51~55 里 bias60/rsi_rev 的 IC 数值应视为**下界**。

【量化方法】
  既然我们只有离散值，无法「恢复」丢失的信息，但可以下界反向估计：
    用同一份数据，把连续维（如 lowvol20）人为离散成 5 档，看 IC 掉多少。
    设连续维真 IC = ρ_c，离散成 5 档后 IC = ρ_d，则衰减率 k = ρ_d / ρ_c。
    对同分辨率因子，用 1/k 可粗略还原「若为连续值」的 IC 下界。

  再做「档内并列比例」统计，说明损失程度。
"""
import json
import os
import math

HERE = os.path.dirname(os.path.abspath(__file__))
PANEL = os.path.join(HERE, 'data', 'panel_round34.json')
KL = os.path.join(HERE, 'data', 'klines_long.json')
OUT = os.path.join(HERE, 'audit_round56.json')

MIN_XS = 20
NW_LAG = 4
HOPS = [5, 20]
# 用连续维做「离散化损失」的标定实验
CALIB_DIMS = ['score_lowvol20', 'score_quality', 'score_ep', 'score_size_ind']
TARGET_DIMS = ['score_bias60', 'score_rsi_rev']


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


def discretize_quantile(vals, nbins):
    """按分位把连续值压成 nbins 个整数档（复刻 bias60/rsi_rev 的 1..5 形态）"""
    n = len(vals)
    order = sorted(range(n), key=lambda i: vals[i])
    out = [0] * n
    for pos, idx in enumerate(order):
        b = min(nbins - 1, int(pos * nbins / n))
        out[idx] = b + 1
    return out


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

    # 收集每个截面的 (factor→val, ret)
    out = {
        'round': 56,
        'purpose': '量化 bias60/rsi_rev 仅 5 整数档造成的 IC 低估幅度',
        'method': ('把连续维按分位压成 5 档，测量 IC 衰减率 k=ρ_离散/ρ_连续；'
                   '对同为 5 档的 bias60/rsi_rev，其真实 IC 下界可视为 ρ_观测/k'),
        'caveat': 'k 用 4 个连续维标定取均值，为量级估计非精确还原；不同因子离散化损失不完全相同',
        'hops': {},
    }

    for h in HOPS:
        # 每截面：连续原值 IC、5档后 IC
        per_dim = {d: {'cont': [], 'disc': [], 'tie': []} for d in CALIB_DIMS + TARGET_DIMS}

        for dt in dts:
            i = pos.get(dt)
            if i is None or i + h >= len(cal):
                continue
            fwd = cal[i + h]
            snap = rows[dt]
            rec = {}
            for code, vals in snap.items():
                c0 = close(code, dt)
                c1 = close(code, fwd)
                if not c0 or not c1 or c0 <= 0:
                    continue
                rec[code] = (vals, c1 / c0 - 1.0)
            if len(rec) < MIN_XS:
                continue
            codes = list(rec.keys())

            for dim in CALIB_DIMS + TARGET_DIMS:
                fv, rv, cv = [], [], []
                for c in codes:
                    v = rec[c][0].get(dim)
                    if not isinstance(v, (int, float)):
                        continue
                    fv.append(float(v))
                    rv.append(rec[c][1])
                if len(fv) < MIN_XS or max(fv) - min(fv) < 1e-12:
                    continue
                ic_cont = spearman(fv, rv)
                dv = discretize_quantile(fv, 5)
                ic_disc = spearman(dv, rv)
                uniq = len(set(fv))
                # 并列比例：1 - 唯一值/样本数
                tie_rate = 1.0 - uniq / len(fv)
                if ic_cont is not None and ic_disc is not None:
                    per_dim[dim]['cont'].append(ic_cont)
                    per_dim[dim]['disc'].append(ic_disc)
                    per_dim[dim]['tie'].append(tie_rate)

        rec_out = {}
        ks = []
        for dim in CALIB_DIMS + TARGET_DIMS:
            c = per_dim[dim]
            if len(c['cont']) < 10:
                rec_out[dim] = {'note': '样本不足'}
                continue
            mc = sum(c['cont']) / len(c['cont'])
            md = sum(c['disc']) / len(c['disc'])
            k = md / mc if abs(mc) > 1e-9 else None
            rec_out[dim] = {
                'n': len(c['cont']),
                'ic_continuous': round(mc, 4),
                't_continuous': round(nw_t(c['cont']), 2) if nw_t(c['cont']) is not None else None,
                'ic_binned5': round(md, 4),
                't_binned5': round(nw_t(c['disc']), 2) if nw_t(c['disc']) is not None else None,
                'decay_k': round(k, 3) if k is not None else None,
                'observed_tie_rate': round(sum(c['tie']) / len(c['tie']), 3),
                'role': 'calibration' if dim in CALIB_DIMS else 'target',
            }
            if dim in CALIB_DIMS and k is not None and k > 0.05:
                ks.append(k)

        k_mean = sum(ks) / len(ks) if ks else None
        # 对 target 维做还原
        for dim in TARGET_DIMS:
            r = rec_out.get(dim) or {}
            if r.get('ic_continuous') is not None and k_mean:
                # target 维的「原值」已经是离散的，故用 ic_binned5 / k 估算连续情形
                r['estimated_if_continuous'] = round(r['ic_binned5'] / k_mean, 4)
                r['uplift_pct'] = round((1 / k_mean - 1) * 100, 1)

        out['hops'][str(h)] = {
            'calibration_dims': CALIB_DIMS,
            'target_dims': TARGET_DIMS,
            'k_mean_calibration': round(k_mean, 3) if k_mean else None,
            'implied_uplift_pct': round((1 / k_mean - 1) * 100, 1) if k_mean else None,
            'dims': rec_out,
        }

    json.dump(out, open(OUT, 'w', encoding='utf-8'), ensure_ascii=False, indent=1)

    # ---- 打印 ----
    for h in HOPS:
        r = out['hops'][str(h)]
        print(f"\n{'='*100}")
        print(f"  h = {h} 日   离散化损失标定")
        print(f"{'='*100}")
        print(f"{'dim':20s} {'角色':>6s} {'连续IC':>9s} {'5档后IC':>9s} {'衰减k':>7s} {'档内并列率':>10s}")
        print('-' * 100)
        for dim in CALIB_DIMS + TARGET_DIMS:
            d = r['dims'].get(dim) or {}
            if 'note' in d:
                print(f"{dim:20s} {d['note']}")
                continue
            role = '标定' if d['role'] == 'calibration' else '目标'
            print(f"{dim:20s} {role:>6s} {d['ic_continuous']:+9.4f} {d['ic_binned5']:+9.4f} "
                  f"{(d['decay_k'] if d['decay_k'] is not None else float('nan')):7.3f} "
                  f"{d['observed_tie_rate']*100:9.1f}%")
        print('-' * 100)
        print(f"  标定 k 均值 = {r['k_mean_calibration']}  →  "
              f"若 5 档维改为连续，IC 可上修约 {r['implied_uplift_pct']}%")
        for dim in TARGET_DIMS:
            d = r['dims'].get(dim) or {}
            if d.get('estimated_if_continuous') is not None:
                print(f"    {dim:18s} 观测 IC={d['ic_binned5']:+.4f} → 连续情形估计 {d['estimated_if_continuous']:+.4f}")

    print(f"\n✅ 写出 {os.path.basename(OUT)}")


if __name__ == '__main__':
    main()
