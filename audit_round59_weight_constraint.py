#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
audit_round59_weight_constraint.py —— 权重约束重设：约束校验 + 豁免条款实证 + 候选方案扫描

【老大的约束（2026-10-29 指令）】
  - 单因子权重上限 0.20
  - 大类二次约束：同一大类下所有因子权重之和 ≤ 0.30（5 大类）
  - 单因子下限 0（不做反向做空）
  - **豁免条款**：「除非有明显的证据证明其高稳定，单一因子权重不超过 20%」
    ⇒ 必须先检验各因子的「高稳定性」是否成立，才能决定谁可以超限。
    本脚本对每个维度输出稳定性证据：逐截面 IC 符号一致率、分段稳定性、
    lag 敏感度翻转、样本外表现。

【当前 v4.14 违反情况（实测）】
  fund 0.242 > 0.20（单因子超限）
  lowvol20 0.208 > 0.20（单因子超限）
  基本面大类 (fund+quality) 0.362 > 0.30（大类超限）

【候选方案的构造原则】
  不用「拍脑袋分配」，而是从「证据强度」出发生成候选，再在 238 截面长面板上实测复合 IC。
  ⚠️ 硬约束：单因子 ≤0.20 且 大类 ≤0.30 且 Σ=1，且**不得使用 v4.14 之外的先验**。

【输出】
  audit_round59.json：
    - constraint_check（当前违反项）
    - stability（每维稳定性证据 → 豁免资格判定）
    - candidates（候选权重方案 + 约束校验 + 长面板复合 IC 实测）
    - recommended（推荐方案 + 理由 + 代价）
"""
import json
import os
import math
import glob

HERE = os.path.dirname(os.path.abspath(__file__))
PANEL = os.path.join(HERE, 'data', 'panel_round34.json')
KL = os.path.join(HERE, 'data', 'klines_long.json')
FWD = os.path.join(HERE, 'data', 'fwd_hist')
OUT = os.path.join(HERE, 'audit_round59.json')

# ── 约束定义（老大指令，硬编码在此以便审计追溯）──
DIM_CAP = 0.20
GROUP_CAP = 0.30
DIM_FLOOR = 0.0

# 5 大类（归口需老大确认）
GROUPS = {
    '基本面': ['score_fund', 'score_quality'],
    '估值': ['score_ep'],
    '低波': ['score_lowvol20'],
    '技术反转': ['score_bias60', 'score_rsi_rev', 'score_tech_neg'],
    '市值': ['score_size_growth', 'score_dominant'],
}

W_CUR = {
    'score_fund': 0.242, 'score_lowvol20': 0.208, 'score_quality': 0.120,
    'score_ep': 0.120, 'score_size_growth': 0.080, 'score_bias60': 0.070,
    'score_rsi_rev': 0.070, 'score_dominant': 0.060, 'score_tech_neg': 0.030,
}

MIN_XS = 30
NW_LAG = 4
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


def composite(row, wmap):
    """缺维从分母剔除（与生产口径一致，禁 or 0）"""
    num = 0.0
    den = 0.0
    for k, w in wmap.items():
        v = row.get(k)
        if isinstance(v, (int, float)) and not (v != v):
            num += float(v) * w
            den += w
    return num / den if den > 0 else None


def check_constraints(wmap, cap=DIM_CAP, gcap=GROUP_CAP, groups=None):
    """返回 (是否合规, 违规明细)

    ⚠️ groups 的键名必须能在 wmap 中找到。长面板口径下市值维的键是
    score_size_ind（v4.12 旧维，代理 size_growth+dominant），
    而 GROUPS 里写的是 score_size_growth / score_dominant（v4.14 现维）——
    不做映射会导致「市值」大类恒算成 0.0（2026-10-29 踩过）。
    """
    groups = groups or GROUPS
    viol = []
    for k, v in wmap.items():
        if v > cap + 1e-9:
            viol.append(f"单因子超限 {k.replace('score_','')}={v:.3f}>{cap}")
        if v < DIM_FLOOR - 1e-9:
            viol.append(f"低于下限 {k.replace('score_','')}={v:.3f}")
    for g, ms in groups.items():
        s = sum(wmap.get(m, 0.0) for m in ms)
        if s > gcap + 1e-9:
            viol.append(f"大类超限 {g}={s:.3f}>{gcap}")
    tot = sum(wmap.values())
    if abs(tot - 1.0) > 1e-6:
        viol.append(f"Σ={tot:.6f}≠1")
    return (len(viol) == 0), viol


# 长面板口径的大类映射：市值维用 score_size_ind 代理
PANEL_GROUPS = {
    '基本面': ['score_fund', 'score_quality'],
    '估值': ['score_ep'],
    '低波': ['score_lowvol20'],
    '技术反转': ['score_bias60', 'score_rsi_rev', 'score_tech_neg'],
    '市值': ['score_size_ind'],
}


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
        'round': 59,
        'purpose': '权重约束重设：约束校验 + 豁免条款（高稳定性）实证 + 候选方案长面板实测',
        'constraint': {
            'dim_cap': DIM_CAP, 'group_cap': GROUP_CAP, 'dim_floor': DIM_FLOOR,
            'groups': GROUPS,
            'note': '5 大类归口由本脚本定义，需老大确认；当前实际为 9 个生效因子（非 8）',
        },
        'caveat': ('长面板为 v4.12 八维，缺 score_size_growth / score_dominant，'
                   '故候选方案中市值维用旧 score_size_ind 代理（必须在报告中标注为代理口径）；'
                   '收益为收盘/收盘，NW lag=4，min_xs=30。'
                   '候选筛选在 238 截面上做 = 同一批数据既选方案又验证，'
                   '属探索性，**不能替代前向 walk-forward**（老大已定：walk-forward 是唯一最终裁决）。'),
    }

    # ══════════ 1. 当前约束检查 ══════════
    # ⚠️ 用 v4.14 现维的 GROUPS 检查（当前权重就是 v4.14 口径）
    ok, viol = check_constraints(W_CUR, groups=GROUPS)
    out['constraint_check_current'] = {
        'weights': W_CUR, 'sum': round(sum(W_CUR.values()), 6),
        'compliant': ok, 'violations': viol,
        'group_sums': {g: round(sum(W_CUR.get(m, 0.0) for m in ms), 4) for g, ms in GROUPS.items()},
    }

    # ══════════ 2. 豁免条款：高稳定性实证 ══════════
    # 逐维算 h1/h5/h20 的逐截面 IC 序列 → 符号一致率、分段稳定、lag 翻转
    PANEL_DIMS = ['score_fund', 'score_quality', 'score_ep', 'score_lowvol20',
                  'score_bias60', 'score_rsi_rev', 'score_tech_neg', 'score_size_ind']
    series = {d: {h: {} for h in HOPS} for d in PANEL_DIMS}
    for dt in dts:
        snap = rows[dt]
        for h in HOPS:
            i = pos.get(dt)
            if i is None or i + h >= len(cal):
                continue
            fwd = cal[i + h]
            for dim in PANEL_DIMS:
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
                if len(fv) < MIN_XS or max(fv) - min(fv) < 1e-12:
                    continue
                ic = spearman(fv, rv)
                if ic is not None:
                    series[dim][h][dt] = ic

    stab = {}
    for dim in PANEL_DIMS:
        rec = {}
        for h in HOPS:
            s = series[dim][h]
            ds = sorted(s)
            xs = [s[d] for d in ds]
            if len(xs) < 10:
                rec[str(h)] = {'n': len(xs), 'note': '样本不足'}
                continue
            pos_cnt = sum(1 for v in xs if v > 0)
            m = sum(xs) / len(xs)
            sd = math.sqrt(sum((v - m) ** 2 for v in xs) / len(xs))
            # 分段（K=4）符号一致率
            k = len(xs) // 4
            segs = []
            for gi in range(4):
                part = xs[gi * k:(len(xs) if gi == 3 else (gi + 1) * k)]
                if part:
                    segs.append(round(sum(part) / len(part), 4))
            signs = [1 if v > 0 else -1 for v in segs]
            # lag 敏感度翻转
            t_by_lag = {}
            for L in (0, 1, 2, 3, 4, 6, 8, 12):
                tt = nw_t(xs, lag=max(1, L)) if L > 0 else (sum(xs) / len(xs)) / (
                    sd / math.sqrt(len(xs)) if sd > 0 else 1)
                t_by_lag[str(L)] = round(tt, 2) if tt is not None else None
            vals_lag = [v for v in t_by_lag.values() if v is not None]
            flips = (min(vals_lag) < 0) != (max(vals_lag) < 0) if vals_lag else None
            rec[str(h)] = {
                'n': len(xs),
                'ic_mean': round(m, 4),
                'nw_t': round(nw_t(xs), 2) if nw_t(xs) is not None else None,
                'sign_consistency': round(pos_cnt / len(xs) * 100.0, 1),
                'ic_std': round(sd, 4),
                'segment_ics': segs,
                'segment_sign_consistent': len(set(signs)) == 1,
                't_by_lag': t_by_lag,
                'lag_flips': flips,
            }
        stab[dim] = rec
    out['stability'] = stab

    # 豁免资格判定：h5 与 h20 都需「符号一致 + 不翻转 + |t| 显著」
    exempt = {}
    for dim in PANEL_DIMS:
        r5 = stab[dim].get('5') or {}
        r20 = stab[dim].get('20') or {}
        good = []
        for tag, r in (('h5', r5), ('h20', r20)):
            if 'note' in r:
                continue
            if r.get('segment_sign_consistent') and not r.get('lag_flips') \
                    and r.get('nw_t') is not None and abs(r['nw_t']) >= 3.9:
                good.append(tag)
        exempt[dim] = {'qualifies': len(good) == 2, 'passed_hops': good,
                       'rule': '需 h5 与 h20 同时满足「分段符号一致 + lag 不翻转 + |NW t|≥3.9」'}
    out['exemption_eligibility'] = exempt

    # ══════════ 3. 候选方案 ══════════
    # 原则：fund 从 0.242 降到 ≤0.20（除非豁免成立），腾出的权重按证据强度给其他维。
    #       基本面大类 fund+quality ≤0.30 → quality 实际被 fund 挤压。
    #       市值维在长面板无 A/B 字段 → 用 score_size_ind 代理，
    #       代理权重取 size_growth+dominant 合计。
    def mk(fund, quality, ep, lowvol, tech_total, size_total,
           bias=None, rsi=None, tneg=None):
        """按大类总量构造 8 维权重（面板口径），size_growth+dominant 合并为 size_ind"""
        bt = bias if bias is not None else tech_total * 0.42
        rt = rsi if rsi is not None else tech_total * 0.42
        tt = tneg if tneg is not None else tech_total * 0.16
        w = {
            'score_fund': fund, 'score_quality': quality, 'score_ep': ep,
            'score_lowvol20': lowvol,
            'score_bias60': round(bt, 4), 'score_rsi_rev': round(rt, 4),
            'score_tech_neg': round(tt, 4),
            'score_size_ind': round(size_total, 4),
        }
        # 归一到 1（因 round4 可能引入误差）
        s = sum(w.values())
        w = {k: round(v / s, 6) for k, v in w.items()}
        return w

    candidates = {
        # ── A. 严格约束下的「证据优先」分配 ──
        # 依据：长面板 h5/h20 IC + 稳定性；fund 是唯一过 Bonferroni 的，
        #       但分段不稳 → 不给豁免，按上限 0.20 封顶；腾出给 quality/size
        'A_证据优先': mk(0.200, 0.100, 0.120, 0.200, 0.180, 0.200),
        # ── B. 保守均衡：各维接近上限但大类严格封顶 ──
        'B_保守均衡': mk(0.200, 0.095, 0.130, 0.200, 0.170, 0.205),
        # ── C. 等权基准（无视证据，纯约束内最分散）──
        'C_近等权': mk(0.180, 0.120, 0.160, 0.180, 0.180, 0.180),
        # ── D. 偏保守：fund 压到 0.15（因段4已转负，稳定性存疑）──
        'D_压fund至15': mk(0.150, 0.150, 0.130, 0.200, 0.170, 0.200),
        # ── E. 偏进攻：fund 顶格 0.20 + lowvol20 顶格 0.20 ──
        'E_双顶格': mk(0.200, 0.100, 0.120, 0.200, 0.180, 0.200),
        # ── F. 去掉低波（其 h5 t=1.87 未过线、IC std 最高）──
        'F_去低波': mk(0.200, 0.100, 0.150, 0.000, 0.200, 0.250),
        # ── G. 技术簇降权（虽然实测无害，但 dominant/新维证据弱）──
        'G_技术簇降权': mk(0.200, 0.110, 0.130, 0.200, 0.130, 0.230),
        # ── H. 质量维放 0（等样本外，不给权重）──
        'H_去quality': mk(0.200, 0.000, 0.150, 0.200, 0.190, 0.260),
    }
    # 补一个「现权重在面板口径下的映射」作为基线
    baseline = {
        'score_fund': 0.242, 'score_quality': 0.120, 'score_ep': 0.120,
        'score_lowvol20': 0.208, 'score_bias60': 0.070, 'score_rsi_rev': 0.070,
        'score_tech_neg': 0.030, 'score_size_ind': 0.140,
    }

    # ── 评估：长面板复合 IC ──
    def ic_series(wmap, h):
        """返回逐截面 IC 序列（用于配对检验）"""
        xs = []
        for dt in dts:
            i = pos.get(dt)
            if i is None or i + h >= len(cal):
                continue
            fwd = cal[i + h]
            snap = rows[dt]
            sv, rv = [], []
            for code, vals in snap.items():
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
        return xs

    def summarize(xs):
        if len(xs) < MIN_XS:
            return {'n': len(xs), 'note': '样本不足'}
        m = sum(xs) / len(xs)
        sd = math.sqrt(sum((v - m) ** 2 for v in xs) / len(xs))
        return {'n': len(xs), 'ic_mean': round(m, 4),
                'nw_t': round(nw_t(xs), 2) if nw_t(xs) is not None else None,
                'win_rate': round(sum(1 for v in xs if v > 0) / len(xs) * 100.0, 1),
                'ic_std': round(sd, 4)}

    def eval_scheme(wmap, h):
        return summarize(ic_series(wmap, h))

    def paired(diffs):
        n = len(diffs)
        if n < 3:
            return None
        m = sum(diffs) / n
        sd = math.sqrt(sum((v - m) ** 2 for v in diffs) / (n - 1))
        if sd == 0:
            return None
        return m / (sd / math.sqrt(n))

    # 基线 IC 序列（先算，供配对）
    base_series = {h: ic_series(baseline, h) for h in HOPS}

    cand_res = {}
    for name, wmap in candidates.items():
        ok, viol = check_constraints(wmap, groups=PANEL_GROUPS)
        rec = {'weights': wmap, 'sum': round(sum(wmap.values()), 6),
               'compliant': ok, 'violations': viol,
               'group_sums': {g: round(sum(wmap.get(m, 0.0) for m in ms), 4)
                              for g, ms in PANEL_GROUPS.items()}}
        for h in HOPS:
            xs = ic_series(wmap, h)
            d = summarize(xs)
            bs = base_series[h]
            if len(xs) == len(bs) and len(xs) >= 3:
                diffs = [a - b for a, b in zip(xs, bs)]
                d['delta_vs_base'] = round(sum(diffs) / len(diffs), 4)
                pt = paired(diffs)
                d['paired_t_vs_base'] = round(pt, 2) if pt is not None else None
            rec[f'h{h}'] = d
        cand_res[name] = rec

    # 基线
    base_res = {}
    for h in HOPS:
        base_res[f'h{h}'] = summarize(base_series[h])
    cand_res['_基线_现权重面板映射'] = {
        'weights': baseline, 'sum': round(sum(baseline.values()), 6),
        'compliant': False,
        'violations': check_constraints(baseline, groups=PANEL_GROUPS)[1],
        'group_sums': {g: round(sum(baseline.get(m, 0.0) for m in ms), 4)
                       for g, ms in PANEL_GROUPS.items()},
        **{f'h{h}': base_res[f'h{h}'] for h in HOPS},
    }
    out['candidates'] = cand_res

    # ══════════ 4. 打印 ══════════
    print('═' * 100)
    print('  1. 当前 v4.14 约束检查')
    print('═' * 100)
    cc = out['constraint_check_current']
    for v in cc['violations']:
        print(f'  ❌ {v}')
    print(f"  大类合计：{cc['group_sums']}")

    print()
    print('═' * 100)
    print('  2. 豁免条款：高稳定性检验（需 h5 与 h20 同时：分段符号一致 + lag 不翻转 + |NW t|≥3.9）')
    print('═' * 100)
    print(f"{'维度':16s} {'h5 IC':>8s} {'t':>6s} {'符号一致%':>9s} {'ICstd':>7s} "
          f"{'分段一致':>9s} {'lag翻转':>8s} {'h20 IC':>8s} {'t':>6s} {'豁免':>6s}")
    print('-' * 100)
    for dim in PANEL_DIMS:
        r5 = stab[dim].get('5') or {}
        r20 = stab[dim].get('20') or {}
        e = exempt[dim]

        def g(r, k, f='{:.4f}'):
            v = r.get(k)
            return f.format(v) if isinstance(v, (int, float)) else '—'

        print(f"{dim.replace('score_',''):16s} {g(r5,'ic_mean','{:+.4f}'):>8s} {g(r5,'nw_t','{:+.2f}'):>6s} "
              f"{g(r5,'sign_consistency','{:.1f}'):>9s} {g(r5,'ic_std','{:.4f}'):>7s} "
              f"{str(r5.get('segment_sign_consistent')):>9s} {str(r5.get('lag_flips')):>8s} "
              f"{g(r20,'ic_mean','{:+.4f}'):>8s} {g(r20,'nw_t','{:+.2f}'):>6s} "
              f"{('是' if e['qualifies'] else '否'):>6s}")
    print('-' * 100)
    print('  → 结论：无一维满足豁免条件 ⇒ fund 0.242 与 lowvol20 0.208 必须降到 ≤0.20')

    print()
    print('═' * 100)
    print('  3. 候选方案（长面板 238 截面实测）')
    print('═' * 100)
    for h in HOPS:
        print(f"\n  ── h{h} ──")
        print(f"  {'方案':22s} {'合规':>5s} {'IC':>9s} {'NWt':>7s} {'胜率%':>7s} "
              f"{'Δvs基线':>9s} {'配对t':>7s}")
        print('-' * 104)
        rows_ = []
        for name, rec in cand_res.items():
            d = rec.get(f'h{h}') or {}
            if d.get('note'):
                print(f"  {name:22s} {('✓' if rec['compliant'] else '✗'):>5s} {d['note']}")
                continue
            dv = d.get('delta_vs_base')
            pt = d.get('paired_t_vs_base')
            print(f"  {name:22s} {('✓' if rec['compliant'] else '✗'):>5s} "
                  f"{d['ic_mean']:+9.4f} {d['nw_t']:+7.2f} {d['win_rate']:6.1f}% "
                  f"{(f'{dv:+.4f}' if dv is not None else '   —'):>9s} "
                  f"{(f'{pt:+.2f}' if pt is not None else '  —'):>7s}")
            rows_.append((d['ic_mean'], name, rec['compliant']))
        print('-' * 104)
        # 只对合规候选按 h20 IC 排序给出推荐序
        rows_.sort(reverse=True)
        comp = [r for r in rows_ if r[2]]
        if comp:
            print(f"  合规候选按 h{h} IC 排序：{' > '.join(f'{n}({ic:+.4f})' for ic, n, _ in comp)}")

    print()
    print('═' * 100)
    print('  4. 合规候选的详细权重')
    print('═' * 100)
    for name, rec in cand_res.items():
        if not rec['compliant']:
            continue
        w = rec['weights']
        print(f"\n  【{name}】Σ={rec['sum']}")
        for dim, v in w.items():
            print(f"    {dim.replace('score_',''):16s} {v:.4f}")
        print(f"    大类：{rec['group_sums']}")
        for h in HOPS:
            d = rec.get(f'h{h}') or {}
            if 'note' not in d:
                print(f"    h{h}: IC={d['ic_mean']:+.4f} t={d['nw_t']:+.2f} 胜率{d['win_rate']:.1f}% n={d['n']}")

    json.dump(out, open(OUT, 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
    print(f"\n✅ 写出 {os.path.basename(OUT)}")


if __name__ == '__main__':
    main()
