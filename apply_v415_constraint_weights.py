#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
apply_v415_constraint_weights.py —— 落地 v4.15 约束化权重（老大 2026-10-29 指令）

【权重变更】
  方案 A「证据优先」（round59 候选 → round60 三周期四道验收全通过 → round61 九维合规
  → round62/63 排序与结构验证）：
      fund       0.242 → 0.200   （原超单因子 0.20 上限）
      lowvol20   0.208 → 0.200   （原超单因子 0.20 上限）
      quality    0.120 → 0.100
      ep         0.120 → 0.120
      size_growth 0.080 → 0.114286
      dominant   0.060 → 0.085714
      bias60     0.070 → 0.0756
      rsi_rev    0.070 → 0.0756
      tech_neg   0.030 → 0.0288
  大类（5 类）：基本面 0.300 / 估值 0.120 / 低波 0.200 / 技术反转 0.180 / 市值 0.200
  Σ = 1.000000

【豁免条款的实测结论】
  老大原话「除非有明显的证据证明其高稳定，单一因子权重不超过 20%」。
  round59 对 8 个维度逐一检验（需 h5 与 h20 同时满足「分段符号一致 + lag 不翻转 +
  |NW t| ≥ 3.9」）→ **无一维通过** ⇒ fund 与 lowvol20 必须降到 0.20，无豁免可用。

【变更纪律】
  ① 主路径 = factor_weights.json（唯一权威源）
  ② 兜底硬编码 = 5 处（enhance_scores / factor_eval / apply_sector_momentum /
     step2_stockscorer_v2 / gen_report 的 _fb），必须同步，否则文件缺失时会静默回退旧权重
  ③ 改完必跑 verify_weights_round36.py 做六方一致性校验
  ④ 禁 git commit / push（未经老大同意）
"""
import json
import os
import re
import shutil

HERE = os.path.dirname(os.path.abspath(__file__))
WJ = os.path.join(HERE, 'factor_weights.json')
BACKUP_SUFFIX = '.bak_v414_before'

# ── 新权重（方案 A，9 维）──
NEW9 = {
    'score_fund': 0.200000,
    'score_lowvol20': 0.200000,
    'score_quality': 0.100000,
    'score_ep': 0.120000,
    'score_size_growth': 0.114286,
    'score_bias60': 0.075600,
    'score_rsi_rev': 0.075600,
    'score_dominant': 0.085714,
    'score_tech_neg': 0.028800,
}

GROUPS = {
    '基本面': ['score_fund', 'score_quality'],
    '估值': ['score_ep'],
    '低波': ['score_lowvol20'],
    '技术反转': ['score_bias60', 'score_rsi_rev', 'score_tech_neg'],
    '市值': ['score_size_growth', 'score_dominant'],
}
DIM_CAP = 0.20
GROUP_CAP = 0.30

COMMENT = ("v4.15-constraint-cap：老大 2026-10-29 指令「单因子≤0.20 + 5大类各≤0.30」。"
           "原 fund 0.242 / lowvol20 0.208 双双破单因子上限，基本面大类 0.362 破大类上限；"
           "round59 逐维检验「高稳定」豁免条款（h5&h20 同时满足 分段符号一致+lag不翻转+|NW t|≥3.9）"
           "→ **无一维通过**，故无豁免可用。方案A 经 round60 三周期四道验收全通过、"
           "round61 九维约束合规、round62/63 排序(ρ=0.9935)与结构(PC1 28.9%/有效维数 6.01)均不劣化。"
           "⚠️ 候选筛选与验证同在 238 截面上，属探索性，唯一最终裁决仍是前向 walk-forward。")


def check(w):
    v = []
    for k, x in w.items():
        if x > DIM_CAP + 1e-9:
            v.append(f'单因子超限 {k}={x}')
        if x < -1e-9:
            v.append(f'负权重 {k}={x}')
    for g, ms in GROUPS.items():
        s = sum(w.get(m, 0.0) for m in ms)
        if s > GROUP_CAP + 1e-9:
            v.append(f'大类超限 {g}={s:.4f}')
    t = sum(w.values())
    if abs(t - 1.0) > 1e-6:
        v.append(f'Σ={t:.8f}≠1')
    return v


def main():
    print('═' * 92)
    print('  v4.15 约束化权重落地')
    print('═' * 92)

    # ── 0. 落地前校验 ──
    viol = check(NEW9)
    if viol:
        print('❌ 新权重不合规，中止：')
        for v in viol:
            print('   ', v)
        return 1
    print(f"  ✅ 新权重合规：Σ={sum(NEW9.values()):.6f}")
    for g, ms in GROUPS.items():
        print(f"     {g:8s} {sum(NEW9.get(m,0) for m in ms):.4f}  ({'+'.join(x.replace('score_','') for x in ms)})")

    # ── 1. factor_weights.json（权威源）──
    d = json.load(open(WJ, encoding='utf-8'))
    old_w = {k: v for k, v in (d.get('weights') or {}).items()
             if isinstance(v, (int, float)) and v > 0}
    shutil.copy2(WJ, WJ + BACKUP_SUFFIX)

    d['version'] = 'v4.15-constraint-cap'
    d['fitted_at'] = '2026-10-29 10:30:00'
    d['weights'] = {k: NEW9.get(k, 0.0) for k in d['weights'].keys()}
    d['prior_weights'] = {k: NEW9.get(k, 0.0) for k in (d.get('prior_weights') or {}).keys()}
    d['method'] = COMMENT
    d.setdefault('notes', [])
    d['notes'] = [n for n in d['notes'] if 'v4.15-constraint-cap' not in str(n)]
    d['notes'].insert(0, '【v4.15-constraint-cap】' + COMMENT)
    d['notes'].insert(1, ('【v4.15 约束】单因子上限 0.20 / 5 大类各 ≤0.30 / 单因子下限 0；'
                          '豁免条款（高稳定性）实测无一维通过。变更依据：'
                          'audit_round59（约束校验+豁免检验）、round60（三周期四道验收）、'
                          'round61（九维映射+结构）、round62（排序影响+权重敏感度）、'
                          'round63（真实9维终选验证）。'))
    # 记录约束元信息，便于后续自动校验
    d['weight_constraints'] = {
        'dim_cap': DIM_CAP, 'group_cap': GROUP_CAP, 'dim_floor': 0.0,
        'groups': GROUPS,
        'enforcement': 'verify_weights_round36.py 必须逐项校验，FAIL 则不出图',
    }
    with open(WJ, 'w', encoding='utf-8') as f:
        json.dump(d, f, ensure_ascii=False, indent=2)
    print(f"\n  ✅ factor_weights.json 已更新 → version={d['version']}")
    print(f"     备份：{os.path.basename(WJ + BACKUP_SUFFIX)}")
    print('     变更明细：')
    for k in NEW9:
        o = old_w.get(k, 0.0)
        if abs(o - NEW9[k]) > 1e-9:
            print(f"       {k.replace('score_',''):16s} {o:.4f} → {NEW9[k]:.4f}  ({NEW9[k]-o:+.4f})")

    # ── 2. 五处兜底硬编码同步 ──
    # 这些是「读 JSON 失败时的回退先验」，必须与 JSON 一致，否则静默回退旧权重
    targets = [
        ('enhance_scores.py', '_W_PRIOR_10D'),
        ('factor_eval.py', 'PRIOR_W10'),
        ('apply_sector_momentum.py', '_W_PRIOR_10D'),
        ('step2_stockscorer_v2.py', '_W_PRIOR_10D'),
    ]
    # gen_report 的 _fb 是 dict 字面量、多行，单独处理
    NEW_DICT_STR = ('{"score_fund": 0.2, "score_news": 0, "score_theme_neg": 0, '
                     '"score_size": 0, "score_sector": 0, "score_quality": 0.1, '
                     '"score_growth": 0, "score_tech_neg": 0.0288, "score_oversold60": 0, '
                     '"score_bias60": 0.0756, "score_rsi_rev": 0.0756, "score_vol_pulse": 0, '
                     '"score_overbought": 0, "score_theme_intrinsic": 0, "score_attention": 0, '
                     '"score_lowvol20": 0.2, "score_ep": 0.12, "score_size_ind": 0.0, '
                     '"score_size_growth": 0.114286, "score_dominant": 0.085714}')

    print('\n  同步兜底硬编码：')
    for fn, varname in targets:
        p = os.path.join(HERE, fn)
        if not os.path.exists(p):
            print(f'     ⚠️ {fn} 不存在，跳过')
            continue
        src = open(p, encoding='utf-8').read()
        # 定位该变量的定义行（含跨行时取到行尾闭合）
        m = re.search(rf'^{varname}\s*=\s*\{{.*?\}}', src, re.S | re.M)
        if not m:
            print(f'     ⚠️ {fn}: 未定位到 {varname}，跳过')
            continue
        old_block = m.group(0)
        new_block = re.sub(r'(\{.*\})',
                           lambda mm: NEW_DICT_STR,
                           old_block, count=1, flags=re.S)
        # 保留行尾注释
        tail = ''
        cm = re.search(r'\}\s*(#.*)$', old_block, re.S)
        if cm:
            tail = '  # ' + COMMENT.split('。')[0]
        new_block = new_block.rstrip()
        if tail and not new_block.endswith('#'):
            new_block += tail
        src2 = src.replace(old_block, new_block, 1)
        if src2 == src:
            print(f'     ⚠️ {fn}: 替换未生效')
            continue
        shutil.copy2(p, p + BACKUP_SUFFIX)
        with open(p, 'w', encoding='utf-8') as f:
            f.write(src2)
        print(f'     ✅ {fn} 的 {varname} 已同步')

    # gen_report 的 _fb（多行 dict 字面量）
    gp = os.path.join(HERE, 'gen_report_top200.py')
    if os.path.exists(gp):
        src = open(gp, encoding='utf-8').read()
        m = re.search(r'_fb = \{"score_fund":.*?\}', src, re.S)
        if m:
            new_blk = ('_fb = {"score_fund": 0.2, "score_news": 0.0, "score_theme_neg": 0.0,\n'
                       '            "score_size": 0.0, "score_sector": 0.0, "score_quality": 0.1,\n'
                       '            "score_growth": 0.0, "score_tech_neg": 0.0288, "score_oversold60": 0.0,\n'
                       '            "score_bias60": 0.0756, "score_rsi_rev": 0.0756, "score_vol_pulse": 0.0,\n'
                       '            "score_overbought": 0.0, "score_theme_intrinsic": 0.0, "score_attention": 0.0,\n'
                       '            "score_lowvol20": 0.2, "score_ep": 0.12, "score_size_ind": 0.0,\n'
                       '            "score_size_growth": 0.114286, "score_dominant": 0.085714}')
            src2 = src.replace(m.group(0), new_blk, 1)
            if src2 != src:
                shutil.copy2(gp, gp + BACKUP_SUFFIX)
                with open(gp, 'w', encoding='utf-8') as f:
                    f.write(src2)
                print('     ✅ gen_report_top200.py 的 _fb 已同步')
            else:
                print('     ⚠️ gen_report_top200.py: _fb 替换未生效')
        else:
            print('     ⚠️ gen_report_top200.py: 未定位到 _fb')

    print('\n  ✅ 落地完成。下一步必须跑 verify_weights_round36.py 做六方一致性校验。')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
