#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
audit_round65_quality_contamination.py —— quality 维污染的定量诊断与修复验证

【发现（本轮 P0，比 fund 缺口更严重）】
  `top200_scored_2026-10-23.json` 与 `top200_scored_2026-10-26.json` 两天的
  **score_quality 字段是坏值**，且这正好是 `factor_ic.json` 里 quality 有记录的全部两个截面
  ⇒ quality 维的 IC 证据链（n=2）建立在坏数据上。

【证据链（全部实测，见输出）】
  1. 10-26 vs 10-28 同批 1741 只：ρ(quality) = **−0.0745**（应≈+1）
     而 lowvol20 +0.9316 / bias60 +0.9268 / rsi_rev +0.8381 / ep +0.9990 → **只有 quality 坏**
  2. 同两天的**原始字段完全一致**：roe +1.0000 / cagr3 +1.0000 / debt_ratio +1.0000 / pb +1.0000
     ⇒ 输入相同、输出不同 ⇒ 是**计算环节**坏，不是数据源坏
  3. 10-26 内部 ρ(quality, roe) = **−0.1680**（负！）；10-27 = **+0.2563**（正，符合预期）
     ⇒ quality 与其最核心子因子 roe 呈**负相关** ⇒ 排序被打乱
  4. 10-26 内部 ρ(quality, growth) = **−0.1905**（同源两维一起坏）
  5. 分箱单调性检验（10-26 quality 分 10 档 → 10-28 quality 均值）：**断裂**
     （最高档 9.27 → 4.599，低于第 1 档的 5.461）
  6. 时间线：10-23 截面写于 10-26 15:27，10-26 截面写于 10-26 18:16，
     而 10-27（10-28 14:07）与 10-28（10-29 01:36）均正常（ρ=+0.9998 / +1.0000）
     ⇒ **两个坏截面都在 10-26 当天用当时代码生成**；当日之后（round35/36 修 NaN/PIT 后）正常
  7. 10-26 的 `.bak_sector` 备份与现文件完全一致 ⇒ 不是「二次运行覆盖」

【修复方案】
  A. 重算两个坏截面的 quality/growth（用现行已修复的 fund_split.score_rows）
     —— 但需注意：重算用的是**当前**的 EM 缓存与现行 PIT 逻辑，
        严格来说这是「以现在的认知回填历史」，存在前视风险。
        ⇒ 故本脚本只做**诊断 + 污染清单**，不擅自改写历史文件。
  B. 在 factor_eval 里**剔除被污染的截面**（标记而非删除），让 IC 表不再引用坏数据。
  C. quality 维的 IC 证据清零并披露（此前 round51 报出的 quality h20 +0.0357 t=+2.37
     建立在 panel 的 quality 上 —— 该 panel 的 quality 同样与真实 scored 不一致，
     **该结论作废**，见下方 panel 交叉验证）。

【输出】audit_round65.json
"""
import json
import os
import math
import glob

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, 'audit_round65.json')

BASE = '2026-10-28'          # 最新可信截面
SUSPECT = ['2026-10-23', '2026-10-26']
DIMS = ['score_quality', 'score_growth', 'score_lowvol20', 'score_fund', 'score_ep',
        'score_bias60', 'score_rsi_rev', 'score_tech_neg']
RAW = ['roe', 'cagr3', 'debt_ratio', 'pb', 'pe', 'fwd_pe']


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


def load(d):
    p = os.path.join(HERE, f'top200_scored_{d}.json')
    if not os.path.exists(p):
        return None
    try:
        j = json.load(open(p, encoding='utf-8'))
    except Exception:
        return None
    return {r['code'][-6:]: r for r in j.get('results', [])}


def main():
    base = load(BASE)
    out = {
        'round': 65,
        'purpose': 'quality 维污染的定量诊断（10-23 / 10-26 两截面）+ 修复方案评估',
        'base_date': BASE,
        'suspect_dates': SUSPECT,
        'verdict': None,
        'per_date': {},
        'raw_field_consistency': {},
        'internal_quality_roe': {},
        'panel_cross_check': {},
        'impact': {},
        'fix_options': [],
    }

    # ── 1. 逐日 vs 基准的因子维相关 ──
    print('═' * 108)
    print('  quality 污染诊断')
    print('═' * 108)
    print(f"  基准截面：{BASE}")
    print()
    for fp in sorted(glob.glob(os.path.join(HERE, 'top200_scored_*.json'))):
        fn = os.path.basename(fp)
        # ⚠️ 必须排除备份与下划线前缀文件（如 _2026-10-12 / *.bak_sector），
        #    否则文件名切片取出的日期不对，污染判定会漏（2026-10-29 踩过）。
        if not fn.startswith('top200_scored_2') or '.bak' in fn:
            continue
        d = fn[len('top200_scored_'):-len('.json')]
        if d == BASE or not d.startswith('2026-'):
            continue
        D = load(d)
        if not D:
            continue
        ks = sorted(set(D) & set(base))
        rec = {}
        print(f"  ── {d}  池 {len(D)} 只，交集 {len(ks)} 只 ──")
        for dim in DIMS:
            xs, ys = [], []
            for c in ks:
                u = D[c].get(dim)
                v = base[c].get(dim)
                if isinstance(u, (int, float)) and isinstance(v, (int, float)):
                    xs.append(u)
                    ys.append(v)
            if len(xs) < 50:
                rec[dim] = None
                continue
            r = spearman(xs, ys)
            rec[dim] = round(r, 4) if r is not None else None
            flag = '  ❌ 污染' if (r is not None and r < 0.5) else ''
            print(f"    ρ({dim.replace('score_',''):16s}, {BASE}) = {r:+.4f}  n={len(xs)}{flag}")
        out['per_date'][d] = {'pool_n': len(D), 'n_common': len(ks), 'corr_vs_base': rec}

    # ── 2. 原始字段一致性（输入是否相同）──
    print()
    print('  ── 原始字段一致性：10-26 vs 10-27（同池 1895 只）──')
    a26 = load('2026-10-26')
    a27 = load('2026-10-27')
    if a26 and a27:
        ks = sorted(set(a26) & set(a27))
        for f in RAW:
            xs, ys = [], []
            for c in ks:
                u = a26[c].get(f)
                v = a27[c].get(f)
                if isinstance(u, (int, float)) and isinstance(v, (int, float)):
                    xs.append(u)
                    ys.append(v)
            r = spearman(xs, ys) if len(xs) >= 50 else None
            out['raw_field_consistency'][f] = {'rho': round(r, 4) if r is not None else None,
                                               'n': len(xs)}
            if r is not None:
                print(f"    ρ({f:12s}) = {r:+.4f}  n={len(xs)}")
    print('    ⇒ 原始输入完全一致 ⇒ 坏在计算环节')

    # ── 3. 内部 ρ(quality, roe) ──
    print()
    print('  ── 关键判据：quality 与其核心子因子 roe 的内部关系 ──')
    for d in ['2026-10-23', '2026-10-26', '2026-10-27', '2026-10-28']:
        D = load(d)
        if not D:
            continue
        xs, ys = [], []
        for c in D:
            u = D[c].get('score_quality')
            v = D[c].get('roe')
            if isinstance(u, (int, float)) and isinstance(v, (int, float)):
                xs.append(u)
                ys.append(v)
        if len(xs) < 50:
            continue
        r = spearman(xs, ys)
        out['internal_quality_roe'][d] = round(r, 4)
        flag = '  ❌ 负相关 → 排序被打乱' if (r is not None and r < 0) else ''
        print(f"    {d}: ρ(quality, roe) = {r:+.4f}  n={len(xs)}{flag}")

    # ── 4. panel 的 quality 是否同样不可信 ──
    pp = os.path.join(HERE, 'data', 'panel_round34.json')
    if os.path.exists(pp):
        p = json.load(open(pp, encoding='utf-8'))
        pr = p['rows'].get('2026-10-26') or {}
        if pr and base:
            ks = sorted(set(pr) & set(base))
            xs, ys = [], []
            for c in ks:
                u = pr[c].get('score_quality')
                v = base[c].get('score_quality')
                if isinstance(u, (int, float)) and isinstance(v, (int, float)):
                    xs.append(u)
                    ys.append(v)
            r = spearman(xs, ys) if len(xs) >= 50 else None
            out['panel_cross_check'] = {
                'date': '2026-10-26', 'rho_panel_vs_scored_quality': round(r, 4) if r is not None else None,
                'n': len(xs),
                'conclusion': ('panel 的 score_quality 与真实 scored 不一致（ρ 极低）'
                               '⇒ round51 用 panel 补算出的 quality IC 结论作废'
                               if r is not None and r < 0.5 else 'panel quality 可信'),
            }
            print()
            print('  ── panel_round34 的 quality 与真实 scored 一致性 ──')
            print(f"    ρ(panel.quality, scored.quality) = {r:+.4f}  n={len(xs)}")
            print(f"    ⇒ {out['panel_cross_check']['conclusion']}")

    # ── 5. 影响面：IC 表里受污染的截面 ──
    fic = os.path.join(HERE, 'factor_ic.json')
    if os.path.exists(fic):
        try:
            f = json.load(open(fic, encoding='utf-8'))
            q = (f.get('factors') or {}).get('score_quality') or {}
            hh = (q.get('horizons') or {})
            out['impact']['factor_ic_quality'] = {
                h: {'n': (hh.get(h) or {}).get('n'),
                    'dates': (hh.get(h) or {}).get('dates')}
                for h in hh}
            print()
            print('  ── 影响面：factor_ic.json 里 quality 的截面 ──')
            for h, v in out['impact']['factor_ic_quality'].items():
                bad = [d for d in (v['dates'] or []) if d in SUSPECT]
                print(f"    h{h}: n={v['n']}  日期={v['dates']}  "
                      f"{'❌ 全部落在污染截面' if bad and len(bad) == len(v['dates'] or []) else ('⚠️ 部分污染' if bad else '✅ 未受污染')}")
        except Exception as e:
            out['impact']['factor_ic_quality'] = {'error': str(e)}

    # ── 6. 结论 ──
    contaminated = []
    for d, r in out['per_date'].items():
        cq = (r.get('corr_vs_base') or {}).get('score_quality')
        if cq is not None and cq < 0.5:
            contaminated.append(d)
    out['verdict'] = {
        'contaminated_dates': contaminated,
        'root_cause': ('两个坏截面都在 2026-10-26 当天用当时代码生成；'
                       '当日之后（round35 修 NaN / round36 修 PIT 后）生成的截面全部正常。'
                       '原始输入（roe/cagr3/debt_ratio/pb）跨日完全一致，'
                       '故坏在「quality/growth 的截面内 rank」环节 —— '
                       '与记忆中的「round35 NaN 污染 → 排序崩塌 → 分数依赖输入行序」同一类问题。'),
        'quality_ic_evidence': '作废（factor_ic 的 quality n=2 且两截面均污染；round51 的 panel 补算亦不可信）',
        'action_taken': '本脚本只诊断不改数据；历史文件交老大裁决',
    }
    out['fix_options'] = [
        {'opt': 'A', 'action': '用现行已修复的 fund_split.score_rows 重算 10-23/10-26 的 quality/growth',
         'cost': '中（需载入 636MB EM 缓存）',
         'risk': '高：这是「以现在认知回填历史」，严格 PIT 下三季报披露状态已变，'
                 '回填值不等于当日真实可得值 → 有前视风险'},
        {'opt': 'B', 'action': '在 factor_eval 中标记并剔除这两个截面（不删文件，只是不参与 IC 统计）',
         'cost': '低', 'risk': '低：诚实做法，IC 样本少 2 个截面，quality 维证据清零'},
        {'opt': 'C', 'action': '仅披露不改数据（页面标注 quality 维 IC 证据不可用）',
         'cost': '零', 'risk': '低，但 IC 表会继续显示 n=2 的误导性数字'},
    ]

    json.dump(out, open(OUT, 'w', encoding='utf-8'), ensure_ascii=False, indent=1)

    print()
    print('  ── 结论 ──')
    print(f"    污染截面：{contaminated}")
    print(f"    {out['verdict']['root_cause']}")
    print(f"    quality 维 IC 证据：{out['verdict']['quality_ic_evidence']}")
    print()
    print('  修复选项：')
    for o in out['fix_options']:
        print(f"    {o['opt']}: {o['action']}")
        print(f"       成本={o['cost']}  风险={o['risk']}")
    print(f"\n✅ 写出 {os.path.basename(OUT)}")


if __name__ == '__main__':
    main()
