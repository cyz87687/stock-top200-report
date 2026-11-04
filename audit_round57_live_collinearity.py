#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
audit_round57_live_collinearity.py —— 用今日真实打分文件核实「权重虚增」

【为什么必须用真实文件】
  round55 用的是 panel_round34（v4.12 八维、rank 百分位化后的历史值）。
  但当前生效的是 v4.14-evidence-aligned 九维。要判断「权重是否虚增」，
  必须用今日（2026-10-28）真实产出的 scored 文件，而不是历史面板。
  且 panel 缺 score_size_growth / score_dominant，无法评估这两个新维。

【检验内容】
  1. 今日 2696 只上，九个生效维两两 Spearman 相关矩阵
  2. 按权重把「同一簇」的权重合并，给出「有效独立权重」 vs 「名义权重」
     例：若 bias60/rsi_rev/tech_neg 三者 ρ≈0.7，则名义合计 0.17 的权重
         实际只相当于 1 个维度在起作用
  3. 相关矩阵的 PC1 解释比例、有效维数（= (Σλ)² / Σλ²，参与维度指标的常用口径）
     与记忆里的「有效维数 6.88/9」交叉核对
"""
import json
import os
import math

HERE = os.path.dirname(os.path.abspath(__file__))
SCORED = os.path.join(HERE, 'top200_scored_2026-10-28.json')
OUT = os.path.join(HERE, 'audit_round57.json')

W = {
    'score_fund': 0.242,
    'score_lowvol20': 0.208,
    'score_quality': 0.120,
    'score_ep': 0.120,
    'score_size_growth': 0.080,
    'score_bias60': 0.070,
    'score_rsi_rev': 0.070,
    'score_dominant': 0.060,
    'score_tech_neg': 0.030,
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


def jacobi_eig(mat, iters=200):
    """对称矩阵 Jacobi 特征值（返回降序列表）"""
    n = len(mat)
    a = [row[:] for row in mat]
    for _ in range(iters):
        off = 0.0
        p = q = 0
        for i in range(n):
            for j in range(i + 1, n):
                if abs(a[i][j]) > off:
                    off = abs(a[i][j])
                    p, q = i, j
        if off < 1e-10:
            break
        app, aqq, apq = a[p][p], a[q][q], a[p][q]
        theta = 0.5 * math.atan2(2 * apq, aqq - app) if abs(aqq - app) > 1e-15 else math.pi / 4
        c, s = math.cos(theta), math.sin(theta)
        for k in range(n):
            akp, akq = a[k][p], a[k][q]
            a[k][p] = c * akp - s * akq
            a[k][q] = s * akp + c * akq
        for k in range(n):
            apk, aqk = a[p][k], a[q][k]
            a[p][k] = c * apk - s * aqk
            a[q][k] = s * apk + c * aqk
    return sorted([a[i][i] for i in range(n)], reverse=True)


def main():
    d = json.load(open(SCORED, encoding='utf-8'))
    results = d.get('results', [])
    print(f"打分文件 {os.path.basename(SCORED)}  日期 {d.get('date')}  记录 {len(results)} 条")

    dims = list(W.keys())
    # 提取各维值（isinstance 严格判定，禁 NaN）
    data = {k: [] for k in dims}
    for r in results:
        for k in dims:
            v = r.get(k)
            data[k].append(float(v) if isinstance(v, (int, float)) and not (v != v) else None)

    n = len(results)
    # 覆盖统计
    print(f"\n{'dim':20s} {'w':>6s} {'有值':>6s} {'覆盖率':>7s} {'唯一值':>6s}")
    cover = {}
    for k in dims:
        cnt = sum(1 for v in data[k] if v is not None)
        uniq = len(set(v for v in data[k] if v is not None))
        cover[k] = cnt
        print(f"{k:20s} {W[k]:6.3f} {cnt:6d} {100.0*cnt/n:6.1f}% {uniq:6d}")

    # 两两相关：只用两维都有值的样本（pairwise）
    print(f"\n{'='*100}")
    print("  九维两两 Spearman 相关（pairwise 有效样本；|ρ|≥0.5 标 ★，≥0.7 标 ★★）")
    print(f"{'='*100}")
    corr = {}
    for i, a in enumerate(dims):
        for b in dims[i + 1:]:
            xs, ys = [], []
            for idx in range(n):
                va, vb = data[a][idx], data[b][idx]
                if va is not None and vb is not None:
                    xs.append(va)
                    ys.append(vb)
            if len(xs) < 50:
                corr[(a, b)] = None
                continue
            c = spearman(xs, ys)
            corr[(a, b)] = (round(c, 3), len(xs)) if c is not None else None

    # 打印矩阵
    short = {k: k.replace('score_', '') for k in dims}
    hdr = f"{'':14s}" + ''.join(f"{short[b][:8]:>9s}" for b in dims)
    print(hdr)
    for i, a in enumerate(dims):
        line = f"{short[a][:13]:14s}"
        for j, b in enumerate(dims):
            if i == j:
                line += f"{'1.000':>9s}"
            elif j > i:
                cc = corr.get((a, b))
                line += f"{cc[0]:9.3f}" if cc else f"{'—':>9s}"
            else:
                cc = corr.get((b, a))
                line += f"{cc[0]:9.3f}" if cc else f"{'—':>9s}"
        print(line)

    # 高相关对
    high = [(k, v) for k, v in corr.items() if v and abs(v[0]) >= 0.5]
    high.sort(key=lambda kv: -abs(kv[1][0]))
    print(f"\n  高相关对（|ρ|≥0.5）共 {len(high)} 对：")
    for (a, b), (c, ns) in high:
        star = '★★' if abs(c) >= 0.7 else '★'
        print(f"    {star} {a:22s} vs {b:22s} ρ={c:+.3f}  n={ns}")

    # PC1 / 有效维数：用完整（两两都有的）子样本，缺失填该维中位数以保证矩阵正定
    print(f"\n{'='*100}")
    print("  特征结构（缺失填样本中位数的完整样本口径）")
    print(f"{'='*100}")
    med = {}
    for k in dims:
        vals = sorted(v for v in data[k] if v is not None)
        med[k] = vals[len(vals) // 2] if vals else 5.0
    filled = {k: [v if v is not None else med[k] for v in data[k]] for k in dims}

    m = len(dims)
    C = [[0.0] * m for _ in range(m)]
    for i in range(m):
        for j in range(m):
            C[i][j] = 1.0 if i == j else (spearman(filled[dims[i]], filled[dims[j]]) or 0.0)
    eig = jacobi_eig(C)
    tot = sum(eig)
    pc1 = eig[0] / tot * 100 if tot > 0 else None
    # 有效维数 = (Σλ)²/Σλ²
    eff = (tot ** 2) / sum(v * v for v in eig) if any(eig) else None
    print(f"  特征值（降序）：{[round(v,3) for v in eig]}")
    print(f"  PC1 解释比例：{pc1:.1f}%")
    print(f"  有效维数（(Σλ)²/Σλ²）：{eff:.2f} / {m}")

    # 权重簇合并
    print(f"\n{'='*100}")
    print("  权重虚增测算：把 |ρ|≥0.5 的簇内权重合并为「有效独立权重」")
    print(f"{'='*100}")
    # 用并查集找簇
    parent = {k: k for k in dims}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    for (a, b), v in corr.items():
        if v and abs(v[0]) >= 0.5:
            union(a, b)

    clusters = {}
    for k in dims:
        clusters.setdefault(find(k), []).append(k)

    nominal_total = sum(W.values())
    eff_total = 0.0
    cluster_rows = []
    # ⚠️ 变量名冲突血泪史（2026-10-29 踩）：此前这里用 `eff` 存簇的有效权重，
    #    直接把上面算好的「有效维数 eff=6.01」覆盖成 0.06，json 里 effective_dim 就错了。
    #    故簇内一律用 _cl_ 前缀，绝不碰 eff / pc1 / eig。
    for root, members in clusters.items():
        wsum = sum(W[k] for k in members)
        # 簇的有效权重取「成员中权重最大者」（保守：假设最强代表其他）
        _cl_eff = max(W[k] for k in members)
        eff_total += _cl_eff
        cluster_rows.append({'members': members, 'nominal_w': round(wsum, 3),
                             'effective_w': round(_cl_eff, 3),
                             'inflate': round(wsum - _cl_eff, 3)})
    cluster_rows.sort(key=lambda r: -r['inflate'])

    for r in cluster_rows:
        tag = '簇' if len(r['members']) > 1 else '独立'
        print(f"  [{tag}] {' + '.join(r['members'])}")
        print(f"        名义权重 {r['nominal_w']:.3f} → 有效 {r['effective_w']:.3f}  "
              f"{'虚增 ' + format(r['inflate'], '.3f') if r['inflate'] > 0 else '（无虚增）'}")
    print(f"\n  名义总权重 {nominal_total:.3f}  →  簇合并后有效总权重 {eff_total:.3f}  "
          f"（虚增 {nominal_total - eff_total:.3f}，{100*(nominal_total-eff_total)/nominal_total:.1f}%）")

    out = {
        'round': 57,
        'purpose': '用今日真实打分文件核实九维共线性与权重虚增',
        'scored_file': os.path.basename(SCORED),
        'date': d.get('date'),
        'n_rows': n,
        'weights': W,
        'coverage': cover,
        'corr_pairs': {f'{a}|{b}': (v[0] if v else None) for (a, b), v in corr.items()},
        'high_corr_pairs': [{'pair': f'{a}|{b}', 'rho': v[0], 'n': v[1]}
                            for (a, b), v in high],
        'pc1_pct': round(pc1, 1) if pc1 else None,
        'effective_dim': round(eff, 2) if eff else None,
        'eigenvalues': [round(v, 3) for v in eig],
        'clusters': cluster_rows,
        'nominal_total_w': round(nominal_total, 3),
        'effective_total_w': round(eff_total, 3),
        'weight_inflation': round(nominal_total - eff_total, 3),
    }
    json.dump(out, open(OUT, 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
    print(f"\n✅ 写出 {os.path.basename(OUT)}")


if __name__ == '__main__':
    main()
