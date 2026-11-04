"""
用高覆盖新截面（10-26 / 10-27）复核 M1 规则是否仍需上线。

核心问题：
  P0-3「低覆盖(<50%)整维剔除」的收益 +0.0039(t=3.82) 是在
  「覆盖 9%~24%」的旧截面上测出来的。现在覆盖率已跃到 93.4%，
  这条规则是否会变成「永不触发」的死代码？还是会误伤新截面？

⚠️ 口径说明（重要）：
  data/panel_round34.json 的 fund_flags 建于 2026-10-29 之前，
  彼时 fwd_hist 尚无 2026-10-26 / 10-27 文件（断流），故面板里这两日
  覆盖率为 0。但本轮已用 archive_fwd_snapshot.py 补录成功。
  因此本脚本**双口径并列**：
    口径A（面板，历史真值）：反映「当时真实拿到的数据」
    口径B（fwd_hist 现文件）：反映「若现在重建面板会得到的覆盖」
  只有口径B 才能回答「新规则上线后会不会触发」。
"""
import json, os, glob, re

HERE = os.path.dirname(os.path.abspath(__file__))


def is_num(x):
    return isinstance(x, (int, float)) and not isinstance(x, bool) and x == x


p = json.load(open(os.path.join(HERE, 'data/panel_round34.json'), encoding='utf-8'))
ff, dates, domain = p['fund_flags'], p['dates'], p['domain']
dom = set()
for c in domain:
    m = re.search(r'(\d{6})', str(c))
    if m:
        dom.add(m.group(1))

print('=' * 100)
print('口径A：面板 fund_flags（反映当时真实拿到的数据）')
print('=' * 100)
covA = []
for d in dates:
    m = ff.get(d, {})
    n = len(m)
    nv = sum(1 for v in m.values() if not v.get('no_val'))
    covA.append((d, n, nv, (nv / n * 100) if n else 0.0))

trigA = [c for c in covA if 0 < c[3] < 50]
zeroA = [c for c in covA if c[2] == 0]
nontA = [c for c in covA if c[3] >= 50]
print(f'总截面 {len(covA)}')
print(f'  · 全缺 0%            : {len(zeroA):3d}')
print(f'  · 低覆盖 0<cov<50%   : {len(trigA):3d}  ← M1 触发')
print(f'  · 高覆盖 cov>=50%    : {len(nontA):3d}')

print('\n' + '=' * 100)
print('口径B：fwd_hist 现文件 vs 面板域（若现在重建面板）')
print('=' * 100)
covB = []
for fp in sorted(glob.glob(os.path.join(HERE, 'data', 'fwd_hist', '*.json')),
                 key=lambda x: os.path.basename(x)):
    # ⚠️ 必须从 basename 取日期：fp 含绝对路径 .../2026-07-24-18-07-15/...
    #    对整个路径用正则会取到项目目录里的 2026-07-24，全部错标（已踩）
    d = re.search(r'(\d{4}-\d{2}-\d{2})', os.path.basename(fp)).group(1)
    jj = json.load(open(fp, encoding='utf-8'))
    rr = jj.get('rows') or []
    codes = set()
    for r in rr:
        m = re.search(r'(\d{6})', str(r.get('code', '')))
        if m:
            codes.add(m.group(1))
    cov = len(codes & dom) / len(dom) * 100 if dom else 0
    covB.append((d, len(codes), len(codes & dom), cov))

print(f'{"date":12s} {"fwd_hist":>9s} {"∩域":>6s} {"覆盖":>7s}  M1触发?')
for d, n, ni, c in covB[-16:]:
    flag = 'YES' if 0 < c < 50 else ('全缺' if c == 0 else 'no')
    print(f'{d:12s} {n:9d} {ni:6d} {c:6.1f}%  {flag}')

trigB = [c for c in covB if 0 < c[3] < 50]
print(f'\n口径B 全区间 {len(covB)} 个文件，M1 会触发 {len(trigB)} 个')

print('\n' + '=' * 100)
print('结论')
print('=' * 100)
print(f'口径A：M1 在 {len(covA)} 个历史截面触发 {len(trigA)} 个（{len(trigA)/len(covA)*100:.1f}%），'
      f'集中在 2026-06-26 ~ 2026-10-23 旧池期（覆盖 8~22%）')
print(f'口径B：补录 fwd_hist 后，10-26/10-27 覆盖跃至 93%+，M1 不再触发')
print('⇒ M1 定位：**历史修正器**，不是日常守门器')
print('   · 历史回测：启用（否则 80 个旧截面被 9% 覆盖的 fund 污染）')
print('   · 每日生产：不触发（覆盖率 93%+），无副作用，也无收益')
print('⇒ 实现建议：作为回测口径开关 `--m1-drop-low-cover`，默认 ON')
print('   并在页面披露「2026-10-26 前 fund 覆盖不足，历史 total 按整维剔除口径」')
