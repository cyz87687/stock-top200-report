"""
量化 M1 口径对历史 total 的排序影响幅度（供裁决参考）。

⚠️ 口径边界（必须先读）：
  data/panel_round34.json 是 v4.12 时代产物，row 只有 8 个旧维：
    score_fund / score_tech_neg / score_bias60 / score_rsi_rev /
    score_quality / score_lowvol20 / score_size_ind / score_ep
  缺 v4.14 新增的 score_size_growth / score_dominant，也无 total 字段。
  因此本脚本**只能用面板实有维做近似重算**，结论仅用于回答一个相对问题：
    「M1 把 fund 整维剔除后，截面内排序会变多少？」
  这个问题主要由「fund 权重占分母的比重」决定，用旧维集合近似是合理的，
  但**绝对不能**把重算出的绝对分当成 v4.14 的 total。

  权重口径：面板每维权重按 v4.14 中同名维取值；size_ind 因已拆分为
  size_growth(0.08)+dominant(0.06)，这里合并为 0.14 挂在 score_size_ind 上，
  并在分母中同步处理，保证 Σw 与 v4.14 一致。
"""
import json, os
import statistics as st

HERE = os.path.dirname(os.path.abspath(__file__))
W = json.load(open(os.path.join(HERE, 'factor_weights.json'), encoding='utf-8'))
wd = W.get('weights', W)
WACT = {k: float(v) for k, v in wd.items() if isinstance(v, (int, float)) and v > 0}

# 面板维 → v4.14 权重（size_ind 合并 size_growth+dominant）
PMAP = {
    'score_fund': WACT.get('score_fund', 0.0),
    'score_tech_neg': WACT.get('score_tech_neg', 0.0),
    'score_bias60': WACT.get('score_bias60', 0.0),
    'score_rsi_rev': WACT.get('score_rsi_rev', 0.0),
    'score_quality': WACT.get('score_quality', 0.0),
    'score_lowvol20': WACT.get('score_lowvol20', 0.0),
    'score_size_ind': WACT.get('score_size_growth', 0.0) + WACT.get('score_dominant', 0.0),
    'score_ep': WACT.get('score_ep', 0.0),
}
SUM_P = sum(PMAP.values())

p = json.load(open(os.path.join(HERE, 'data/panel_round34.json'), encoding='utf-8'))
dates, rows, ff = p['dates'], p['rows'], p['fund_flags']
DMAX = 10.0

print('=' * 100)
print('M1 口径对历史排序的影响幅度（8 维近似，见文件头口径边界）')
print('=' * 100)
print(f'v4.14 Σw = {sum(WACT.values()):.6f}')
print(f'面板可映射维 Σw = {SUM_P:.6f}（size_ind 合并 size_growth+dominant）')
print(f'未映射维（面板无数据）: score_size_growth / score_dominant 已合并; 其余全部覆盖')


def rank(xs):
    order = sorted(range(len(xs)), key=lambda i: xs[i])
    rk = [0.0] * len(xs)
    for pos, i in enumerate(order):
        rk[i] = pos + 1.0
    return rk


def spear(a, b):
    ra, rb = rank(a), rank(b)
    ma, mb = st.mean(ra), st.mean(rb)
    nu = sum((x - ma) * (y - mb) for x, y in zip(ra, rb))
    de = (sum((x - ma) ** 2 for x in ra) ** 0.5) * (sum((y - mb) ** 2 for y in rb) ** 0.5)
    return nu / de if de else None


def calc(row, drop_fund):
    num = 0.0
    den = 0.0
    for k, w in PMAP.items():
        if drop_fund and k == 'score_fund':
            continue
        v = row.get(k)
        if isinstance(v, (int, float)) and not isinstance(v, bool) and v == v:
            num += min(float(v), DMAX) / DMAX * 10.0 * w
        else:
            num += (DMAX / 2.0) / DMAX * 10.0 * w   # 缺维填 DMAX/2，分母照计
        den += w
    return (num / den * 10.0) if den > 0 else None


res = []
for d in dates:
    r = rows.get(d) or {}
    fl = ff.get(d) or {}
    if not r or not fl:
        continue
    n = len(r)
    nv = sum(1 for v in fl.values() if not v.get('no_val'))
    cov = nv / n if n else 0.0
    if not (0 < cov < 0.5):
        continue
    codes, a, b = [], [], []
    for code, row in r.items():
        v0 = calc(row, drop_fund=False)
        v1 = calc(row, drop_fund=True)
        if v0 is None or v1 is None:
            continue
        codes.append(code)
        a.append(v0)
        b.append(v1)
    if len(codes) < 50:
        continue
    rho = spear(a, b)
    # Top50 重合
    top_a = set(codes[i] for i in sorted(range(len(a)), key=lambda i: -a[i])[:50])
    top_b = set(codes[i] for i in sorted(range(len(b)), key=lambda i: -b[i])[:50])
    res.append((d, len(codes), cov, rho, st.mean(b) - st.mean(a),
                st.mean(a), st.mean(b), len(top_a & top_b)))

if res:
    print(f'\n触发 M1 的截面：{len(res)} 个')
    print(f'{"date":12s} {"n":>5s} {"cov":>6s} {"rho":>8s} {"Top50∩":>7s} {"Δmean":>8s} {"M0":>6s} {"M1":>6s}')
    for d, n, cov, rho, dm, m0, m1, ov in res[:6]:
        print(f'{d:12s} {n:5d} {cov*100:5.1f}% {rho:8.4f} {ov:5d}/50 {dm:+8.3f} {m0:6.2f} {m1:6.2f}')
    print('   ...')
    for d, n, cov, rho, dm, m0, m1, ov in res[-4:]:
        print(f'{d:12s} {n:5d} {cov*100:5.1f}% {rho:8.4f} {ov:5d}/50 {dm:+8.3f} {m0:6.2f} {m1:6.2f}')

    rhos = [x[3] for x in res if x[3] is not None]
    dms = [x[4] for x in res]
    ovs = [x[7] for x in res]
    print(f'\nρ(M0,M1)  中位 {st.median(rhos):.4f}  最小 {min(rhos):.4f}')
    print(f'Top50 重合 中位 {st.median(ovs):.0f}/50  最小 {min(ovs)}/50')
    print(f'Δ均值     中位 {st.median(dms):+.3f}  区间 [{min(dms):+.3f}, {max(dms):+.3f}]')

    out = {
        'round': 47,
        'purpose': 'M1（低覆盖整维剔除）对历史排序的影响幅度',
        'caveat': ('面板为 v4.12 的 8 维产物，无 total 字段，缺 size_growth/dominant；'
                   '本重算按 v4.14 同名权重近似，size_ind 合并为 size_growth+dominant=0.14。'
                   '结论只回答「排序变动幅度」，不得当作 v4.14 绝对分。'),
        'weights_version': W.get('version'),
        'n_trigger_dates': len(res),
        'rho_median': st.median(rhos), 'rho_min': min(rhos),
        'top50_overlap_median': st.median(ovs), 'top50_overlap_min': min(ovs),
        'delta_mean_median': st.median(dms),
        'delta_mean_range': [min(dms), max(dms)],
        'per_date': [{'date': d, 'n': n, 'cov': c, 'rho': r, 'top50_overlap': ov,
                      'mean_m0': m0, 'mean_m1': m1}
                     for d, n, c, r, _, m0, m1, ov in res],
    }
    json.dump(out, open(os.path.join(HERE, 'audit_round47.json'), 'w', encoding='utf-8'),
              ensure_ascii=False, indent=1)
    print('\n✅ 已写 audit_round47.json')
else:
    print('无可重算截面')
