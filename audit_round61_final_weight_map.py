#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
audit_round61_final_weight_map.py —— 通过验收的方案 → 生产 9 维口径的映射与终选

【背景】
  round59/60 在**面板 8 维口径**上筛出 4 个全通过方案（A≡E、C、D）。
  但生产是 **9 维**（市值维已拆成 score_size_growth + score_dominant 两维）。
  长面板无这两字段 → 面板上只能用旧 score_size_ind 代理（合计市值权重）。
  ⇒ 落地前必须：
    1. 把面板方案的「市值合计权重」拆回 A/B 两维（保持合计不变、保持互斥分区）
    2. 在**生产 9 维口径**下校验全部约束（单因子≤0.20、大类≤0.30、Σ=1）
    3. 用**今日真实打分文件**算九维相关矩阵 → PC1、有效维数、权重虚增
       （新权重下这些结构指标是否改善）

【关键设计约束（不可违反）】
  - size_growth 与 dominant 是**互斥分区**（一只股票只落一维），故二者不是独立因子，
    而是「市值维的两种表达」。约束按「单因子」计，但要同时说明：任一股票
    实际只被其中一个计权。
  - dominant 当日覆盖率仅 26.7% → 其权重实际上只作用在 1/4 股票上，
    这一点必须在终选报告里明示，避免「0.06 权重」的错觉。

【终选判据】
  ① 9 维口径约束全合规
  ② 面板三周期验收全通过（继承 round60）
  ③ 九维结构指标（PC1 / 有效维数）相对当前权重不劣化
  ④ 变更幅度最小原则：偏离当前权重越少越好（降低「换权重 = 换历史」的不可比代价）
"""
import json
import os
import math

HERE = os.path.dirname(os.path.abspath(__file__))
SCORED = os.path.join(HERE, 'top200_scored_2026-10-28.json')
R59 = os.path.join(HERE, 'audit_round59.json')
R60 = os.path.join(HERE, 'audit_round60.json')
OUT = os.path.join(HERE, 'audit_round61.json')

DIM_CAP = 0.20
GROUP_CAP = 0.30

GROUPS = {
    '基本面': ['score_fund', 'score_quality'],
    '估值': ['score_ep'],
    '低波': ['score_lowvol20'],
    '技术反转': ['score_bias60', 'score_rsi_rev', 'score_tech_neg'],
    '市值': ['score_size_growth', 'score_dominant'],
}

W_CUR_9D = {
    'score_fund': 0.242, 'score_lowvol20': 0.208, 'score_quality': 0.120,
    'score_ep': 0.120, 'score_size_growth': 0.080, 'score_bias60': 0.070,
    'score_rsi_rev': 0.070, 'score_dominant': 0.060, 'score_tech_neg': 0.030,
}


def check(wmap):
    viol = []
    for k, v in wmap.items():
        if v > DIM_CAP + 1e-9:
            viol.append(f"单因子超限 {k.replace('score_','')}={v:.4f}>{DIM_CAP}")
        if v < -1e-9:
            viol.append(f"负权重 {k.replace('score_','')}={v:.4f}")
    for g, ms in GROUPS.items():
        s = sum(wmap.get(m, 0.0) for m in ms)
        if s > GROUP_CAP + 1e-9:
            viol.append(f"大类超限 {g}={s:.4f}>{GROUP_CAP}")
    tot = sum(wmap.values())
    if abs(tot - 1.0) > 1e-6:
        viol.append(f"Σ={tot:.8f}≠1")
    return viol


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


def structure(wmap, data, dims):
    """算九维相关矩阵 → PC1、有效维数、权重虚增"""
    n = len(next(iter(data.values())))
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
    eff = (tot ** 2) / sum(v * v for v in eig) if any(eig) else None

    # 权重虚增：|ρ|≥0.5 簇合并
    parent = {k: k for k in dims}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    corr = {}
    for i, a in enumerate(dims):
        for b in dims[i + 1:]:
            c = spearman(filled[a], filled[b])
            corr[(a, b)] = c
    for (a, b), c in corr.items():
        if c is not None and abs(c) >= 0.5:
            ra, rb = find(a), find(b)
            if ra != rb:
                parent[rb] = ra
    clusters = {}
    for k in dims:
        clusters.setdefault(find(k), []).append(k)
    infl = 0.0
    cl_detail = []
    for root, ms in clusters.items():
        if len(ms) > 1:
            nom = sum(wmap.get(k, 0.0) for k in ms)
            eff_w = max(wmap.get(k, 0.0) for k in ms)
            infl += nom - eff_w
            cl_detail.append({'members': ms, 'nominal_w': round(nom, 4),
                              'effective_w': round(eff_w, 4)})
    return {'pc1_pct': round(pc1, 1) if pc1 else None,
            'effective_dim': round(eff, 2) if eff else None,
            'eigenvalues': [round(v, 3) for v in eig],
            'weight_inflation': round(infl, 4),
            'clusters': cl_detail,
            'corr': {f'{a}|{b}': (round(c, 3) if c is not None else None)
                     for (a, b), c in corr.items()}}


def main():
    r59 = json.load(open(R59, encoding='utf-8'))
    r60 = json.load(open(R60, encoding='utf-8'))
    scored = json.load(open(SCORED, encoding='utf-8'))
    results = scored.get('results', [])

    dims = list(W_CUR_9D.keys())
    data = {k: [] for k in dims}
    for r in results:
        for k in dims:
            v = r.get(k)
            data[k].append(float(v) if isinstance(v, (int, float)) and not (v != v) else None)

    # ── 面板通过验收的方案 → 9 维映射 ──
    # 市值合计权重拆回 A/B：保持与当前一致的相对比例（size_growth:dominant = 0.08:0.06 = 4:3）
    passing = []
    for h in ('1', '5', '20'):
        for name, r in (r60['results'].get(h) or {}).items():
            if r['verdict'] == '全通过':
                passing.append((name, h))
    names = sorted(set(n for n, _ in passing))

    cand9 = {}
    for name in names:
        w8 = r59['candidates'][name]['weights']
        size_total = w8['score_size_ind']
        sg = size_total * 0.08 / 0.14     # 沿用当前 4:3 比例
        dm = size_total * 0.06 / 0.14
        w9 = {
            'score_fund': w8['score_fund'],
            'score_quality': w8['score_quality'],
            'score_ep': w8['score_ep'],
            'score_lowvol20': w8['score_lowvol20'],
            'score_bias60': w8['score_bias60'],
            'score_rsi_rev': w8['score_rsi_rev'],
            'score_tech_neg': w8['score_tech_neg'],
            'score_size_growth': round(sg, 6),
            'score_dominant': round(dm, 6),
        }
        s = sum(w9.values())
        w9 = {k: round(v / s, 8) for k, v in w9.items()}
        cand9[name] = w9

    out = {
        'round': 61,
        'purpose': '通过验收方案 → 生产 9 维口径的映射、约束校验与结构指标终选',
        'mapping_rule': '市值合计权重按当前 size_growth:dominant = 4:3 拆回两维（互斥分区，合计不变）',
        'current_9d': {'weights': W_CUR_9D, 'sum': round(sum(W_CUR_9D.values()), 8),
                       'violations': check(W_CUR_9D)},
        'structure_current': structure(W_CUR_9D, data, dims),
        'candidates_9d': {},
        'n_scored': len(results),
    }

    for name, w9 in cand9.items():
        viol = check(w9)
        # 变更幅度（相对当前）
        l1 = sum(abs(w9[k] - W_CUR_9D[k]) for k in dims)
        rec = {
            'weights': w9,
            'sum': round(sum(w9.values()), 8),
            'violations': viol,
            'compliant': len(viol) == 0,
            'group_sums': {g: round(sum(w9.get(m, 0.0) for m in ms), 4) for g, ms in GROUPS.items()},
            'l1_change_vs_current': round(l1, 4),
            'max_abs_change': round(max(abs(w9[k] - W_CUR_9D[k]) for k in dims), 4),
            'structure': structure(w9, data, dims),
            'panel_verdict': {h: (r60['results'][h].get(name) or {}).get('verdict')
                              for h in ('1', '5', '20')},
            'panel_ic': {h: (r60['results'][h].get(name) or {}).get('all_ic')
                         for h in ('1', '5', '20')},
            'panel_t': {h: (r60['results'][h].get(name) or {}).get('all_t')
                        for h in ('1', '5', '20')},
        }
        out['candidates_9d'][name] = rec

    json.dump(out, open(OUT, 'w', encoding='utf-8'), ensure_ascii=False, indent=1)

    # ---- 打印 ----
    print('═' * 110)
    print('  终选：面板验收通过方案 → 生产 9 维口径映射')
    print('═' * 110)
    cs = out['structure_current']
    print(f"  当前 v4.14 9维：Σ={out['current_9d']['sum']:.6f}  违规：{out['current_9d']['violations']}")
    print(f"    结构：PC1={cs['pc1_pct']}%  有效维数={cs['effective_dim']}/9  权重虚增={cs['weight_inflation']}")
    print()
    hdr = f"{'方案':20s} {'合规':>5s} {'Σ':>8s} {'L1变更':>7s} {'最大变更':>8s} {'PC1':>6s} {'有效维':>7s} {'虚增':>6s} {'h1/h5/h20 IC':>26s}"
    print(hdr)
    print('-' * 110)
    for name, rec in out['candidates_9d'].items():
        st = rec['structure']
        ics = ' / '.join(f"{rec['panel_ic'][h]:+.4f}" if rec['panel_ic'][h] is not None else '—'
                         for h in ('1', '5', '20'))
        print(f"{name:20s} {('✓' if rec['compliant'] else '✗'):>5s} {rec['sum']:8.6f} "
              f"{rec['l1_change_vs_current']:7.4f} {rec['max_abs_change']:8.4f} "
              f"{st['pc1_pct']:5.1f}% {st['effective_dim']:6.2f} {st['weight_inflation']:6.3f} {ics:>26s}")
    print('-' * 110)

    print()
    print('  各候选的 9 维权重明细：')
    for name, rec in out['candidates_9d'].items():
        print(f"\n  【{name}】合规={rec['compliant']}  L1变更={rec['l1_change_vs_current']}")
        for k in dims:
            old = W_CUR_9D[k]
            new = rec['weights'][k]
            flag = '' if abs(new - old) < 1e-9 else f'  ← 原 {old:.3f}（{new-old:+.3f}）'
            print(f"    {k.replace('score_',''):16s} {new:.4f}{flag}")
        print(f"    大类：{rec['group_sums']}")
        st = rec['structure']
        print(f"    结构：PC1 {st['pc1_pct']}%（原 {cs['pc1_pct']}%）  "
              f"有效维数 {st['effective_dim']}（原 {cs['effective_dim']}）  "
              f"虚增 {st['weight_inflation']}（原 {cs['weight_inflation']}）")
        if st['clusters']:
            for c in st['clusters']:
                print(f"      共线簇：{'+'.join(x.replace('score_','') for x in c['members'])} "
                      f"名义 {c['nominal_w']} → 有效 {c['effective_w']}")

    print(f"\n✅ 写出 {os.path.basename(OUT)}")


if __name__ == '__main__':
    main()
