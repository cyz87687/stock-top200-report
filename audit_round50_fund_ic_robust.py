"""
核实 round49 结论的可靠性：fund 的 t=3.21 是真的，还是有口径问题？

要检查的点：
 1. fund 在 80 个截面里的覆盖率分布 —— 绝大多数仍是 9~22% 低覆盖，
    那 t=3.21 究竟反映什么？
 2. ic_series 的分段表现：前段 vs 后段（高覆盖段）
 3. 若剔除高覆盖 3 个截面后 t 是否还成立？
"""
import json, os
import statistics as st

HERE = os.path.dirname(os.path.abspath(__file__))
j = json.load(open(os.path.join(HERE, 'factor_ic.json'), encoding='utf-8'))
f = j['factors']['score_fund']
print('=' * 100)
print('fund IC 可靠性核实')
print('=' * 100)

for h in ('1', '5', '20'):
    d = f['horizons'].get(h) or {}
    dates, ics = d.get('dates') or [], d.get('ic_series') or []
    if not dates:
        continue
    print(f'\n--- h{h}: n={len(ics)} ---')
    # 按覆盖率分界：2026-10-26 起为高覆盖
    lo, hi = [], []
    for dt, ic in zip(dates, ics):
        (hi if dt >= '2026-10-26' else lo).append((dt, ic))
    print(f'  低覆盖段(<=10-23): n={len(lo)}  mean={st.mean([x[1] for x in lo]):+.4f}')
    if hi:
        print(f'  高覆盖段(>=10-26): n={len(hi)}  mean={st.mean([x[1] for x in hi]):+.4f}  '
              f'值={[round(x[1],4) for x in hi]}')
    print(f'  全段 mean={st.mean(ics):+.4f}  std={st.pstdev(ics):.4f}')
    neg = sum(1 for x in ics if x < 0)
    print(f'  负 IC 截面 {neg}/{len(ics)} = {neg/len(ics)*100:.0f}%')

# 剔除高覆盖段后重算 NW t（lag=4）
def nw_t(xs, lag=4):
    n = len(xs)
    if n < lag + 3:
        return None
    m = st.mean(xs)
    dev = [x - m for x in xs]
    g0 = sum(d * d for d in dev) / n
    var = g0
    for L in range(1, lag + 1):
        gl = sum(dev[i] * dev[i - L] for i in range(L, n)) / n
        var += 2 * (1 - L / (lag + 1)) * gl
    if var <= 0:
        return None
    return m / (var / n) ** 0.5


print('\n' + '=' * 100)
print('剔除高覆盖 3 个截面后，fund 的 NW t 是否仍成立？')
print('=' * 100)
for h in ('1', '5', '20'):
    d = f['horizons'].get(h) or {}
    dates, ics = d.get('dates') or [], d.get('ic_series') or []
    if not dates:
        continue
    lo = [ic for dt, ic in zip(dates, ics) if dt < '2026-10-26']
    t_all = nw_t(ics)
    t_lo = nw_t(lo)
    print(f'h{h}: 全段 n={len(ics)} t={t_all:+.2f}  |  低覆盖段 n={len(lo)} t={t_lo:+.2f}')

print("""
解读要点：
  · 若「低覆盖段」t 也很高 → t=3.21 不是新覆盖带来的，而是历史段本就有，
    此前「fund 无实证」的判断可能是被「含 NaN 污染 / 口径错」掩盖，非真实的弱。
  · 若「低覆盖段」t 很低、只有高覆盖段才高 → 说明 t=3.21 由 3 个截面撑起，不可信。
""")
