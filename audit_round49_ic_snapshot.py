"""
round49：fund 覆盖率跃升后，其 IC 是否从「无实证」变为「显著」？

背景：
  - 此前所有 fund IC 结论建立在「覆盖 9%~22%」的样本上，且被选择偏差污染
  - 2026-10-28 覆盖率 89.6%（2415/2696），偏差大幅缓解
  - factor_eval 已用新截面重算（80 截面，2026-06-26~2026-10-28）

本脚本从 factor_ic.json 提取事实，并与历史结论对照。
⚠️ 严格边界：80 个截面里绝大多数仍是「覆盖 9%~22%」的旧截面，
   只有最后 3 个（10-26/10-27/10-28）是高覆盖。所以本次 IC 提升
   **不能**直接归因于覆盖率改善，只能作为「当前态势快照」。
"""
import json, os

HERE = os.path.dirname(os.path.abspath(__file__))
j = json.load(open(os.path.join(HERE, 'factor_ic.json'), encoding='utf-8'))
F = j['factors']


def get(name, h, key, default=None):
    v = (F.get(name) or {}).get('horizons', {}).get(str(h)) or {}
    return v.get(key, default)


print('=' * 104)
print('round49 · fund 与新维的当前 IC 快照（factor_ic.json，date=%s）' % j.get('date'))
print('=' * 104)
print(f'截面数 {j.get("n_snapshots")}，区间 {j.get("snapshot_range")}，'
      f'剔除陈旧 {len(j.get("stale_snapshots_dropped") or [])} 个')

print(f'\n{"因子":14s} {"w":>6s} {"h1_ic":>8s} {"h1_nwT":>7s} {"h5_ic":>8s} {"h5_nwT":>7s} '
      f'{"h20_ic":>8s} {"h20_nwT":>7s} {"Bonf":>5s} {"flips":>5s} {"胜率":>6s}')
print('-' * 104)

W = json.load(open(os.path.join(HERE, 'factor_weights.json'), encoding='utf-8'))
wd = W.get('weights', W)
WACT = {k: float(v) for k, v in wd.items() if isinstance(v, (int, float)) and v > 0}

ROWS = ['score_fund', 'score_lowvol20', 'score_quality', 'score_ep',
        'score_size_growth', 'score_dominant', 'score_bias60', 'score_rsi_rev',
        'score_tech_neg', 'total']
for name in ROWS:
    if name not in F:
        continue
    w = WACT.get(name, float('nan'))
    cells = []
    bonf = flips = wr = None
    for h in (1, 5, 20):
        ic = get(name, h, 'ic_mean')
        t = get(name, h, 'nw_t')
        cells.append(f'{ic:+8.4f}' if isinstance(ic, (int, float)) else '   —    ')
        cells.append(f'{t:+7.2f}' if isinstance(t, (int, float)) else '   —   ')
        if h == 5:
            bonf = get(name, h, 'robust_bonferroni')
            ns = get(name, h, 'nw_sensitivity') or {}
            flips = ns.get('flips')
            wr = get(name, h, 'win_rate')
    w_s = f'{w:6.3f}' if w == w else '   —'
    b_s = ('✓' if bonf else '✗') if bonf is not None else '—'
    f_s = ('是' if flips else '否') if flips is not None else '—'
    wr_s = f'{wr:5.1f}%' if isinstance(wr, (int, float)) else '   —'
    print(f'{name:14s} {w_s} ' + ' '.join(cells) + f' {b_s:>5s} {f_s:>5s} {wr_s:>6s}')

print('\n' + '=' * 104)
print('结论')
print('=' * 104)

fund_t5 = get('score_fund', 5, 'nw_t')
fund_ic5 = get('score_fund', 5, 'ic_mean')
fund_bf = get('score_fund', 5, 'robust_bonferroni')
fund_fl = (get('score_fund', 5, 'nw_sensitivity') or {}).get('flips')
print(f'fund  h5: IC={fund_ic5:+.4f}  NW t={fund_t5:+.2f}  Bonferroni={"过" if fund_bf else "未过"}  '
      f'敏感度翻转={"有" if fund_fl else "无"}')

low_t5 = get('score_lowvol20', 5, 'nw_t')
low_ic5 = get('score_lowvol20', 5, 'ic_mean')
low_bf = get('score_lowvol20', 5, 'robust_bonferroni')
low_fl = (get('score_lowvol20', 5, 'nw_sensitivity') or {}).get('flips')
print(f'lowvol20 h5: IC={low_ic5:+.4f}  NW t={low_t5:+.2f}  Bonferroni={"过" if low_bf else "未过"}  '
      f'敏感度翻转={"有" if low_fl else "无"}')

print()
if fund_bf and not low_bf:
    print('⚠️ 重大变化：fund 过 Bonferroni 且不翻转，而 lowvol20（w=0.208，第二大权重）')
    print('   未过 Bonferroni 且在 lag 敏感性上翻转（flips=True）→ 其显著性依赖 lag 选择。')
    print('   ⇒ 这与此前「lowvol20 是稳健主力维」的认知相反，需老大注意。')
