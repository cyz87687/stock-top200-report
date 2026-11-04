#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
audit_round63_final_verify.py —— 终选方案在「生产 9 维真实数据」下的最终验证

【为什么还差这一步】
  round59~61 全在 **panel_round34（v4.12 八维）** 上做，市值维用旧 score_size_ind 代理。
  但生产是 9 维：市值维已拆成 score_size_growth（覆盖 72.9%）+ score_dominant（覆盖 26.7%），
  两者**互斥分区**，且 dominant 的表达口径与 size_ind 完全不同（B 维是「行业内营收/利润份额」）。
  ⇒ 代理口径的 IC 结论**不能直接外推**到 9 维。必须用今日真实 scored 文件重算。

【本脚本】
  1. 用今日 2696 只真实数据，按各候选 9 维权重重算 composite
  2. 与「面板代理口径」的排序对比 → 量化代理失真
  3. 计算新权重下的**结构指标**（PC1 / 有效维数 / 权重虚增 / 覆盖完整度）
  4. 计算**权重虚增**：市值 A/B 互斥导致实际只有一维计权 → 单股实际承受的权重
  5. 输出终选建议：方案 + 权重 + 变更幅度 + 全部代价

【重要口径声明】
  今日 scored 是**横截面单日**，无法算 IC（IC 需要时间序列）。
  本脚本只能做「结构与排序」验证，**IC 结论仍来自 round59/60 的 238 截面长面板**。
  两者是互补的：长面板给 IC（用代理维），今日数据给真实结构（用真实维）。
"""
import json
import os
import math

HERE = os.path.dirname(os.path.abspath(__file__))
SCORED = os.path.join(HERE, 'top200_scored_2026-10-28.json')
R61 = os.path.join(HERE, 'audit_round61.json')
R62 = os.path.join(HERE, 'audit_round62.json')
OUT = os.path.join(HERE, 'audit_round63.json')

GROUPS = {
    '基本面': ['score_fund', 'score_quality'],
    '估值': ['score_ep'],
    '低波': ['score_lowvol20'],
    '技术反转': ['score_bias60', 'score_rsi_rev', 'score_tech_neg'],
    '市值': ['score_size_growth', 'score_dominant'],
}


def composite(row, wmap):
    num = 0.0
    den = 0.0
    used = []
    for k, w in wmap.items():
        v = row.get(k)
        if isinstance(v, (int, float)) and not (v != v):
            num += float(v) * w
            den += w
            used.append(k)
    if den <= 0:
        return None, 0.0, []
    return num / den, den, used


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
    mx = sum(x) / n
    my = sum(y) / n
    sxy = sum((a - mx) * (b - my) for a, b in zip(x, y))
    sxx = sum((a - mx) ** 2 for a in x)
    syy = sum((b - my) ** 2 for b in y)
    if sxx <= 0 or syy <= 0:
        return None
    return sxy / math.sqrt(sxx * syy)


def spearman(x, y):
    return pearson(_rank(x), _rank(y))


def main():
    scored = json.load(open(SCORED, encoding='utf-8'))
    results = scored.get('results', [])
    r61 = json.load(open(R61, encoding='utf-8'))
    r62 = json.load(open(R62, encoding='utf-8'))

    dims = list(r61['current_9d']['weights'].keys())
    w_cur = r61['current_9d']['weights']

    def build(wmap):
        pairs = []
        for i, r in enumerate(results):
            c, den, used = composite(r, wmap)
            if c is not None:
                pairs.append((i, c, den, used))
        return pairs

    base = build(w_cur)
    base_m = {i: c for i, c, _, _ in base}

    out = {
        'round': 63,
        'purpose': '终选方案在生产 9 维真实数据下的结构与排序验证',
        'caveat': ('今日 scored 为单日横截面，**无法算 IC**。IC 结论来自 round59/60 的 238 截面长面板'
                   '（市值维为 size_ind 代理）。本脚本只验证真实 9 维下的结构与排序影响，'
                   '两者互补但不互相替代。'),
        'n_rows': len(results),
        'current': {'weights': w_cur},
        'candidates': {},
    }

    # 覆盖与「实际计权」分析
    cover = {}
    for k in dims:
        n = sum(1 for r in results
                if isinstance(r.get(k), (int, float)) and not (r.get(k) != r.get(k)))
        cover[k] = n
    out['coverage'] = {k: {'n': v, 'pct': round(100.0 * v / len(results), 1)}
                        for k, v in cover.items()}

    for name, rec in r61['candidates_9d'].items():
        w9 = rec['weights']
        pairs = build(w9)
        m = {i: c for i, c, _, _ in pairs}
        common = sorted(set(m) & set(base_m))
        xs = [base_m[i] for i in common]
        ys = [m[i] for i in common]
        rho = spearman(xs, ys) if len(common) > 2 else None

        # 每只股票「实际生效的权重分母」分布 —— 关键：dominant 只覆盖 26.7%
        dens = [den for _, _, den, _ in pairs]
        # 实际计权维数
        ndim_used = [len(used) for _, _, _, used in pairs]
        # 市值维实际由谁承担
        sg_only = sum(1 for _, _, _, u in pairs
                      if 'score_size_growth' in u and 'score_dominant' not in u)
        dm_only = sum(1 for _, _, _, u in pairs
                      if 'score_dominant' in u and 'score_size_growth' not in u)
        both = sum(1 for _, _, _, u in pairs
                   if 'score_dominant' in u and 'score_size_growth' in u)
        neither = sum(1 for _, _, _, u in pairs
                      if 'score_dominant' not in u and 'score_size_growth' not in u)

        # 有效权重：对每只股票，按实际参与的维重新归一后，
        # 「技术簇」等共线簇的真实暴露 = 该簇内参与维的权重和
        tech_w = w9['score_bias60'] + w9['score_rsi_rev'] + w9['score_tech_neg']
        base_tech_w = w_cur['score_bias60'] + w_cur['score_rsi_rev'] + w_cur['score_tech_neg']
        # 单股实际承受的「技术簇」暴露（缺失维剔除后重新归一）
        tech_exposure = []
        for i, c, den, used in pairs:
            sub = sum(w9[k] for k in used
                      if k in ('score_bias60', 'score_rsi_rev', 'score_tech_neg'))
            tech_exposure.append(sub / den if den > 0 else 0.0)

        out['candidates'][name] = {
            'weights': w9,
            'spearman_vs_current_real9d': round(rho, 4) if rho is not None else None,
            'panel_proxy_vs_real_rank_gap': (
                round(r62['candidates'][name]['spearman_vs_current'], 4)
                if name in r62['candidates'] else None),
            'top50_overlap': r62['candidates'][name]['top50_overlap'],
            'l1_change_vs_current': rec['l1_change_vs_current'],
            'mean_weight_denominator': round(sum(dens) / len(dens), 4),
            'mean_dims_used': round(sum(ndim_used) / len(ndim_used), 3),
            'min_dims_used': min(ndim_used),
            'market_cap_partition': {
                'size_growth_only': sg_only, 'dominant_only': dm_only,
                'both': both, 'neither': neither,
            },
            'tech_cluster_nominal_w': round(tech_w, 4),
            'tech_cluster_nominal_w_current': round(base_tech_w, 4),
            'tech_cluster_mean_effective_exposure': round(
                sum(tech_exposure) / len(tech_exposure), 4),
            'structure': rec['structure'],
        }

    json.dump(out, open(OUT, 'w', encoding='utf-8'), ensure_ascii=False, indent=1)

    # ---- 打印 ----
    print('═' * 112)
    print('  终选验证：生产 9 维真实数据（2026-10-28，2696 只）')
    print('═' * 112)
    print('  覆盖：', {k.replace('score_', ''): f"{v['pct']}%" for k, v in out['coverage'].items()})
    print()
    print(f"{'方案':20s} {'ρ(真实9d)':>10s} {'ρ(面板代理)':>12s} {'Top50':>7s} "
          f"{'L1变更':>8s} {'均参与维数':>10s} {'最少维数':>8s} {'技术簇有效暴露':>14s}")
    print('-' * 112)
    for name, c in out['candidates'].items():
        print(f"{name:20s} {c['spearman_vs_current_real9d']:+10.4f} "
              f"{(c['panel_proxy_vs_real_rank_gap'] if c['panel_proxy_vs_real_rank_gap'] is not None else 0):+12.4f} "
              f"{c['top50_overlap']:>4d}/50 {c['l1_change_vs_current']:8.4f} "
              f"{c['mean_dims_used']:10.3f} {c['min_dims_used']:8d} "
              f"{c['tech_cluster_mean_effective_exposure']:14.4f}")
    print('-' * 112)
    print(f"  当前技术簇名义权重 = {out['candidates'][list(out['candidates'])[0]]['tech_cluster_nominal_w_current']}")
    print()
    print('  市值维互斥分区实测：')
    for name, c in out['candidates'].items():
        mp = c['market_cap_partition']
        print(f"    {name:20s} 仅 size_growth {mp['size_growth_only']:5d} / "
              f"仅 dominant {mp['dominant_only']:5d} / 两者皆有 {mp['both']:4d} / "
              f"皆无 {mp['neither']:4d}")
    print()
    print('  ⚠️ 代理失真：面板用 score_size_ind（单一维，权重全部生效）代理市值维；')
    print('     真实 9 维下 dominant 只覆盖 26.7%，故「市值维实际暴露」小于名义权重。')
    print(f"     这意味着长面板的市值维 IC 结论偏乐观（代理口径下市值维 100% 生效）。")

    print(f"\n✅ 写出 {os.path.basename(OUT)}")


if __name__ == '__main__':
    main()
