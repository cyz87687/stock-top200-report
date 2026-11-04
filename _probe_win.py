# -*- coding: utf-8 -*-
"""只读探测：交易日/预热边界、fwd_hist 覆盖、网络、缺价统计、panel_round30 复核。"""
import json, os, sys, datetime as dt

HERE = os.path.dirname(os.path.abspath(__file__))
os.chdir(HERE)
sys.path.insert(0, '.')

OUT = {}
D = lambda s: dt.date.fromisoformat(s)


def norm(c):
    c = str(c).strip().lower()
    for p in ('sh', 'sz', 'bj'):
        if c.startswith(p):
            c = c[2:]
    return c


# ---------------- 1. 交易日与预热边界 ----------------
fh = json.load(open('factor_history.json'))
tdays = fh['prices']['dates']
n = len(tdays)

first_ge61 = next(d for i, d in enumerate(tdays) if i >= 61)
sec1 = {
    'tdays_first': tdays[0],
    'tdays_last': tdays[-1],
    'n_tdays': n,
    'first_index_ge_61': first_ge61,
    'idx_60_63': {str(i): tdays[i] for i in (60, 61, 62, 63)},
}

CANDS = ['2026-06-19', '2026-06-22', '2026-06-23', '2026-06-26',
         '2026-07-01', '2026-07-10']
rows1 = []
for d0 in CANDS:
    if d0 not in tdays:
        rows1.append({'win_start': d0, 'in_tdays': False})
        continue
    idx = tdays.index(d0)
    rest = tdays[idx:]
    for h in (5, 20):
        pass
    def base_win(h):
        """i+h <= len-1 的基准日集合"""
        bs = [rest[i] for i in range(len(rest)) if i + h <= len(rest) - 1]
        return bs
    b5, b20 = base_win(5), base_win(20)
    def span(bs):
        if not bs:
            return None
        days = (D(bs[-1]) - D(bs[0])).days
        return {'first': bs[0], 'last': bs[-1], 'n': len(bs),
                'cal_days': days, 'cal_months': round(days / 30.44, 2)}
    rows1.append({
        'win_start': d0, 'in_tdays': True, 'idx': idx,
        'remaining_tdays': len(rest),
        'h5': {'n_base': len(b5), 'approx': len([d for d in tdays if d >= d0]) - 5,
               'span': span(b5)},
        'h20': {'n_base': len(b20), 'approx': len([d for d in tdays if d >= d0]) - 20,
                'span': span(b20)},
    })
sec1['win_start_scan'] = rows1

# (a) 反解：h20 跨度 >= 91 天 的最晚 WIN_START
H20_NEED = 91
latest_ok = None
for d0 in tdays:
    idx = tdays.index(d0)
    rest = tdays[idx:]
    bs = [rest[i] for i in range(len(rest)) if i + 20 <= len(rest) - 1]
    if len(bs) < 2:
        continue
    days = (D(bs[-1]) - D(bs[0])).days
    if days >= H20_NEED:
        latest_ok = {'win_start': d0, 'idx': idx, 'h20_span_days': days,
                     'h20_span_months': round(days / 30.44, 2),
                     'h20_first': bs[0], 'h20_last': bs[-1], 'h20_n_base': len(bs)}
sec1['latest_win_start_for_h20_ge_91d'] = latest_ok
OUT['sec1_tdays'] = sec1

# ---------------- 2. fwd_hist 覆盖 ----------------
files = sorted(os.listdir('data/fwd_hist'))
fdates = sorted(f[:-5] for f in files if f.endswith('.json'))
want = []
d = D('2026-06-19')
while d <= D('2026-10-23'):
    want.append(d.isoformat())
    d += dt.timedelta(days=1)
have = set(fdates)
missing = [x for x in want if x not in have]
OUT['sec2_fwd_hist'] = {
    'n_files': len(files),
    'n_dated': len(fdates),
    'files_not_json': [f for f in files if not f.endswith('.json')],
    'earliest': fdates[0], 'latest': fdates[-1],
    'n_unique_dates': len(have),
    'dups': len(fdates) - len(have),
    'range_20260619_20261023_days': len(want),
    'covered_days_in_range': len([x for x in want if x in have]),
    'missing_dates': missing,
    'missing_count': len(missing),
    'sample_dates_head': fdates[:5], 'sample_dates_tail': fdates[-5:],
}

# ---------------- 3. 网络可用性 ----------------
import traceback
net = {'results': [], 'ok': False, 'error': None}
try:
    import factor_eval as fe
    for sym in ["sh600519", "sz000001", "sz300750"]:
        try:
            r = fe._fetch_qfq(sym, need=150)
            net['results'].append({
                'sym': sym, 'n': len(r) if r else 0,
                'first': r[0] if r else None, 'last': r[-1] if r else None,
                'first_date': r[0][0] if r else None,
                'last_date': r[-1][0] if r else None,
            })
        except Exception as e:
            net['results'].append({'sym': sym, 'n': 0, 'error': repr(e)[:300]})
    net['ok'] = any(x.get('n', 0) > 0 for x in net['results'])
except Exception as e:
    net['error'] = repr(e)[:500] + '\n' + traceback.format_exc()[:1500]
OUT['sec3_network'] = net

# ---------------- 4. 缺价统计 ----------------
scored_path = 'top200_scored_2026-10-26.json'
if not os.path.exists(scored_path):
    scored_path = 'top200_scored_2026-10-27.json'
sc = json.load(open(scored_path))
P = {norm(x['code']) for x in sc['results'] if x.get('code')}
K1 = {norm(c) for c in fh['prices']['px'].keys()}
uk = json.load(open('data/universe_klines.json'))
K2 = {norm(c) for c in uk.keys()}
fin = {f[:-5] for f in os.listdir('data/fin_cache_em') if f.endswith('.json')}
fin = {norm(c) for c in fin}

gap = sorted(P - (K1 | K2))
sec4 = {
    'scored_file': scored_path, 'scored_date': sc.get('date'),
    'n_P': len(P), 'n_K1': len(K1), 'n_K2': len(K2),
    'n_K1_cap_P': len(K1 & P), 'n_K2_cap_P': len(K2 & P),
    'n_union_cap_P': len((K1 | K2) & P),
    'n_k2_outside_P': len(K2 - P),
    'gap': len(P) - len((K1 | K2) & P),
    'n_fin_cache_em': len(fin),
    'gap_with_fin_cache': len([c for c in gap if c in fin]),
    'gap_without_fin_cache': len([c for c in gap if c not in fin]),
    'gap_sample': gap[:30],
    'gap_all': gap,
    'fin_covers_P': len(P & fin),
    'fin_missing_in_P': len(P - fin),
}
OUT['sec4_gap'] = sec4

# ---------------- 5. panel_round30 复核 ----------------
p30 = json.load(open('data/panel_round30.json'))
pd_ = p30['dates']
L = len(pd_)
def pwin(h):
    bs = [pd_[i] for i in range(L) if i + h <= L - 1]
    days = (D(bs[-1]) - D(bs[0])).days if bs else None
    return {'n_base': len(bs), 'first': bs[0], 'last': bs[-1],
            'cal_days': days, 'cal_months': round(days / 30.44, 2) if days is not None else None,
            'n_future_days_needed': h}
OUT['sec5_panel'] = {
    'panel_dates_first': pd_[0], 'panel_dates_last': pd_[-1], 'n_dates': L,
    'domain': len(p30['domain']),
    'rows_key_count': len(p30['rows']),
    'h5': pwin(5), 'h20': pwin(20),
    'h20_head_dates': [pd_[i] for i in range(3)],
    'h20_tail_dates': [pd_[i] for i in (L - 22, L - 21, L - 20, L - 1)],
    'meta': p30.get('meta'),
}

# ---------------- 附加：缺口可补性 & 延长交易日场景 ----------------
gap_sample_fetch = []
try:
    import factor_eval as fe
    for sym in ['sh600026', 'sz000048', 'sh603045']:
        r = fe._fetch_qfq(sym, need=200)
        gap_sample_fetch.append({'sym': sym, 'n': len(r) if r else 0,
                                 'first': r[0][0] if r else None,
                                 'last': r[-1][0] if r else None})
except Exception as e:
    gap_sample_fetch = [{'error': repr(e)[:200]}]
OUT['sec4_gap']['gap_fetch_sample'] = gap_sample_fetch

def latest_start_ge(seq, h, need_days):
    ok = None
    for d0 in seq:
        rest = seq[seq.index(d0):]
        b = [rest[j] for j in range(len(rest)) if j + h <= len(rest) - 1]
        if len(b) < 2:
            continue
        days = (D(b[-1]) - D(b[0])).days
        if days >= need_days:
            ok = {'win_start': d0, 'h20_span_days': days,
                  'h20_span_months': round(days / 30.44, 2),
                  'h20_first': b[0], 'h20_last': b[-1], 'h20_n_base': len(b)}
    return ok

ext = tdays + ['2026-10-26', '2026-10-27']
OUT['sec6_extension'] = {
    'note': '把 prices 由 143 日延伸到最新交易日（2026-10-27）后的边界',
    'now': {'n_tdays': len(tdays), 'h20_last_base': None,
            'latest_win_start_ge91': latest_start_ge(tdays, 20, 91)},
    'ext_plus2': {'n_tdays': len(ext),
                  'latest_win_start_ge91': latest_start_ge(ext, 20, 91)},
}
for k, seq in (('now', tdays), ('ext_plus2', ext)):
    L = len(seq)
    b20 = [seq[i] for i in range(L) if i + 20 <= L - 1]
    b5 = [seq[i] for i in range(L) if i + 5 <= L - 1]
    OUT['sec6_extension'][k].update({
        'h5_last_base': b5[-1], 'h20_last_base': b20[-1],
        'h5_n': len(b5), 'h20_n': len(b20)})

# ---------------- (c) 补价后域估算 ----------------
union_P = (K1 | K2) & P
sec_c = {
    'domain_now_panel30': len(p30['domain']),
    'domain_if_union_P': len(union_P),
    'if_union_all_K2': len(K1 | K2),
    'if_union_all_K2_and_P': len((K1 | K2) | P),
    'delta_vs_917': len(union_P) - len(p30['domain']),
    'domain_def_note': 'panel_round30.domain ⊂ K1(1012)，为「全窗口无缺价」子集；补价后可扩到 (K1∪K2)∩P',
}
# 面板域的真实定义核对
idx_all = {d: i for i, d in enumerate(tdays)}
dom = set(p30['domain'])
full_cover = {c for c in K1 if all(fh['prices']['px'][c][idx_all[d]] is not None
                                   for d in p30['dates'])}
sec_c['domain_excluded_from_K1'] = len(K1 - dom)
sec_c['excluded_reason'] = ('域内 917 只 = 窗口内价格全不为 None；'
                            f'K1 内另有 {len(K1-dom)} 只在窗口早期有缺口被剔除，'
                            f'其中 {len([c for c in (K1-dom) if c in fin])} 只有 EM 财务缓存')
sec_c['full_cover_subset_size'] = len(full_cover)
OUT['secC_domain_projection'] = sec_c

with open('_probe_win.json', 'w', encoding='utf-8') as f:
    json.dump(OUT, f, ensure_ascii=False, indent=2)

# ---------------- 打印 ----------------
print('=== 1 交易日 ===')
print('first', tdays[0], 'last', tdays[-1], 'n', n)
print('first index>=61:', first_ge61)
for i in (60, 61, 62, 63):
    print(f'  idx {i}: {tdays[i]}')
print()
hdr = f"{'WIN_START':<12}{'idx':>5}{'h5_n':>6}{'h5首':>13}{'h5末':>13}{'h5天':>7}{'h5月':>7}{'h20_n':>7}{'h20首':>13}{'h20末':>13}{'h20天':>7}{'h20月':>7}"
print(hdr)
for r in rows1:
    if not r['in_tdays']:
        print(f"{r['win_start']:<12}  (不在交易日序列中)"); continue
    a, b = r['h5'], r['h20']
    print(f"{r['win_start']:<12}{r['idx']:>5}{a['n_base']:>6}{a['span']['first']:>13}{a['span']['last']:>13}"
          f"{a['span']['cal_days']:>7}{a['span']['cal_months']:>7}"
          f"{b['n_base']:>7}{b['span']['first']:>13}{b['span']['last']:>13}"
          f"{b['span']['cal_days']:>7}{b['span']['cal_months']:>7}")
print()
print('(a) h20 跨度>=91 天的最晚 WIN_START:', latest_ok)
print()
print('=== 2 fwd_hist ===')
print(json.dumps(OUT['sec2_fwd_hist'], ensure_ascii=False, indent=1)[:2000])
print()
print('=== 3 网络 ===')
for x in net['results']:
    print(x)
print('ok=', net['ok'], 'error=', net['error'])
print()
print('=== 4 缺口 ===')
s4 = dict(sec4); s4.pop('gap_all'); s4.pop('gap_sample')
print(json.dumps(s4, ensure_ascii=False, indent=1))
print('gap sample:', sec4['gap_sample'])
print()
print('=== 5 panel ===')
print(json.dumps(OUT['sec5_panel'], ensure_ascii=False, indent=1))
print()
print('=== C 域投影 ===')
print(json.dumps(sec_c, ensure_ascii=False, indent=1))
