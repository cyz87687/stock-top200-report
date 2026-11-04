# -*- coding: utf-8 -*-
"""
audit_round62_weight_impact.py —— 新权重对「实际排序」的影响评估（落地前最后一道）

【为什么必须做】
  纸面 IC 好 ≠ 落地安全。换权重会改所有 total、所有排名、所有评级。
  若 Top50 重合度低，说明「换一个权重就换一批股票」= 不可比风险极高，
  且老大手册明确记着「改权重后 S/A 档分布跳变」这个已发生问题。

【评估内容】
  ① 用今日真实打分文件，按各候选权重重算 composite（缺维从分母剔除）
  ② 与当前权重排序对比：Spearman ρ、Top20/50/100 重合数
  ③ 评级分布变化（分位口径 (i+0.5)/n）
  ④ total 均值/分位变化
  ⑤ **权重敏感度**：当前权重下 total 对各维的弹性（权重±0.02 时 total 变动）
     → 回答「权重精度到多少才有意义」，避免假精确

【注意】
  本脚本只读 scored 文件，不写任何生产文件。
"""
import json
import os
import math
import bisect

HERE = os.path.dirname(os.path.abspath(__file__))
SCORED = os.path.join(HERE, 'top200_scored_2026-10-28.json')
R61 = os.path.join(HERE, 'audit_round61.json')
OUT = os.path.join(HERE, 'audit_round62.json')

CUTS = [(0.05, 'S'), (0.15, 'A'), (0.50, 'B'), (0.88, 'C'), (1.01, 'D')]


def composite(row, wmap):
    num = 0.0
    den = 0.0
    for k, w in wmap.items():
        v = row.get(k)
        if isinstance(v, (int, float)) and not (v != v):
            num += float(v) * w
            den += w
    return num / den if den > 0 else None


def rank_rating(vals):
    """按分位给级：q=(i+0.5)/n，**按行索引**给级（total 并列不得互相覆盖）"""
    n = len(vals)
    order = sorted(range(n), key=lambda i: vals[i])
    out = {}
    for rank, i in enumerate(order):
        q = (rank + 0.5) / n
        for c, name in CUTS:
            if q < c:
                out[i] = name
                break
        else:
            out[i] = 'D'
    return out


def main():
    scored = json.load(open(SCORED, encoding='utf-8'))
    results = scored.get('results', [])
    r61 = json.load(open(R61, encoding='utf-8'))

    dims = list(r61['current_9d']['weights'].keys())
    w_cur = r61['current_9d']['weights']

    # 当前权重的 composite 与排名
    def build(wmap):
        pairs = []
        for i, r in enumerate(results):
            c = composite(r, wmap)
            if c is not None:
                pairs.append((i, c))
        return pairs

    base_pairs = build(w_cur)
    base_order = [i for i, _ in sorted(base_pairs, key=lambda t: -t[1])]
    base_top20 = set(base_order[:20])
    base_top50 = set(base_order[:50])
    base_top100 = set(base_order[:100])
    base_vals = [c for _, c in base_pairs]

    def dist(vals):
        n = len(vals)
        order = sorted(range(n), key=lambda i: vals[i])
        d = {k: 0 for k in ('S', 'A', 'B', 'C', 'D')}
        for rank, i in enumerate(order):
            q = (rank + 0.5) / n
            for c, name in CUTS:
                if q < c:
                    d[name] += 1
                    break
            else:
                d['D'] += 1
        return d

    base_dist = dist(base_vals)

    out = {
        'round': 62,
        'purpose': '新权重对实际排序/评级/分布的影响 + 权重敏感度（落地前最后一道）',
        'scored_file': os.path.basename(SCORED),
        'n_rows': len(results),
        'n_with_composite': len(base_pairs),
        'current': {
            'weights': w_cur,
            'rating_dist': base_dist,
            'total_mean': round(sum(base_vals) / len(base_vals), 4),
            'total_max': round(max(base_vals), 4),
            'total_min': round(min(base_vals), 4),
        },
        'candidates': {},
    }

    for name, rec in r61['candidates_9d'].items():
        w9 = rec['weights']
        pairs = build(w9)
        vals = [c for _, c in pairs]
        order = sorted(range(len(pairs)), key=lambda i: -pairs[i][1])
        top50 = set(pairs[i][0] for i in order[:50])
        top20 = set(pairs[i][0] for i in order[:20])
        top100 = set(pairs[i][0] for i in order[:100])
        # Spearman：两个 composite 序列按 code 对齐
        m = {i: c for i, c in pairs}
        bm = {i: c for i, c in base_pairs}
        common = sorted(set(m) & set(bm))
        xs = [bm[i] for i in common]
        ys = [m[i] for i in common]

        def rk(v):
            o = sorted(range(len(v)), key=lambda i: v[i])
            rr = [0.0] * len(v)
            k = 0
            while k < len(v):
                j = k
                while j + 1 < len(v) and v[o[j + 1]] == v[o[k]]:
                    j += 1
                avg = (k + j) / 2.0 + 1.0
                for t in range(k, j + 1):
                    rr[o[t]] = avg
                k = j + 1
            return rr

        def pr(x, y):
            n = len(x)
            mx = sum(x) / n
            my = sum(y) / n
            sxy = sum((a - mx) * (b - my) for a, b in zip(x, y))
            sxx = sum((a - mx) ** 2 for a in x)
            syy = sum((b - my) ** 2 for b in y)
            return sxy / math.sqrt(sxx * syy) if sxx > 0 and syy > 0 else None

        rho = pr(rk(xs), rk(ys)) if len(common) > 2 else None
        d = dist(vals)
        out['candidates'][name] = {
            'weights': w9,
            'rating_dist': d,
            'rating_shift_vs_current': {k: d[k] - base_dist[k] for k in d},
            'total_mean': round(sum(vals) / len(vals), 4),
            'total_mean_delta': round(sum(vals) / len(vals) - sum(base_vals) / len(base_vals), 4),
            'spearman_vs_current': round(rho, 4) if rho is not None else None,
            'top20_overlap': len(top20 & base_top20),
            'top50_overlap': len(top50 & base_top50),
            'top100_overlap': len(top100 & base_top100),
            'l1_weight_change': rec['l1_change_vs_current'],
        }

    # ── 权重敏感度：当前权重下各维 ±0.02 的影响 ──
    sens = {}
    for d in dims:
        for delta in (0.02, 0.05):
            w2 = dict(w_cur)
            w2[d] = round(w2[d] + delta, 6)
            tot2 = sum(w2.values())
            w2 = {k: v / tot2 for k, v in w2.items()}
            p2 = build(w2)
            # 与基线排序相关
            m2 = {i: c for i, c in p2}
            common = sorted(set(m2) & set(bm))
            xs = [bm[i] for i in common]
            ys = [m2[i] for i in common]
            rho = pr(rk(xs), rk(ys)) if len(common) > 2 else None
            o2 = sorted(range(len(p2)), key=lambda i: -p2[i][1])
            t50 = set(p2[i][0] for i in o2[:50])
            sens[f'{d.replace("score_","")}_{delta:+.2f}'] = {
                'spearman_vs_current': round(rho, 5) if rho is not None else None,
                'top50_overlap': len(t50 & base_top50),
            }
    out['weight_sensitivity_current'] = sens

    json.dump(out, open(OUT, 'w', encoding='utf-8'), ensure_ascii=False, indent=1)

    # ---- 打印 ----
    print('═' * 108)
    print('  新权重对实际排序的影响')
    print('═' * 108)
    print(f"  当前权重评级分布：{base_dist}   total 均值 {out['current']['total_mean']}")
    print()
    print(f"{'方案':20s} {'ρ(排序)':>9s} {'Top20':>7s} {'Top50':>7s} {'Top100':>7s} "
          f"{'S':>5s} {'A':>5s} {'B':>6s} {'C':>6s} {'D':>5s} {'均值Δ':>8s}")
    print('-' * 108)
    for name, c in out['candidates'].items():
        d = c['rating_dist']
        print(f"{name:20s} {c['spearman_vs_current']:+9.4f} "
              f"{c['top20_overlap']:>3d}/20 {c['top50_overlap']:>3d}/50 {c['top100_overlap']:>4d}/100 "
              f"{d['S']:5d} {d['A']:5d} {d['B']:6d} {d['C']:6d} {d['D']:5d} "
              f"{c['total_mean_delta']:+8.3f}")
    print('-' * 108)
    print(f"  当前评级分布参考：S={base_dist['S']} A={base_dist['A']} B={base_dist['B']} "
          f"C={base_dist['C']} D={base_dist['D']}")
    print()
    print('═' * 108)
    print('  权重敏感度（当前权重下，单维调整后的排序变化）')
    print('═' * 108)
    print(f"{'调整':28s} {'ρ(排序)':>10s} {'Top50重合':>10s}")
    print('-' * 108)
    for k, v in sens.items():
        print(f"  {k:26s} {v['spearman_vs_current']:+10.5f} {v['top50_overlap']:>6d}/50")
    print('-' * 108)
    print('  → ρ 接近 1 且 Top50 重合高 = 权重精度要求宽松；反之说明权重需精细标定')

    print(f"\n✅ 写出 {os.path.basename(OUT)}")


if __name__ == '__main__':
    main()
