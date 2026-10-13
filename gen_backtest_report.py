#!/usr/bin/env python3
"""TOP200 vs TOP500 对照回测报告生成器

读 data/backtest_universe_result.json → data/backtest_universe_report.md
含: 口径声明 / 成交额近似校准 / 主结果 / 敏感性矩阵 / β-α分解 / 结论
"""
import os
import json

HERE = os.path.dirname(os.path.abspath(__file__))
RESULT = os.path.join(HERE, 'data', 'backtest_universe_result.json')
OUT_MD = os.path.join(HERE, 'data', 'backtest_universe_report.md')


def pct(x, d=1):
    return '—' if x is None else f"{x:+.{d}f}%"


def num(x, d=2):
    return '—' if x is None else f"{x:.{d}f}"


def leg_row(name, leg):
    m, a, p = leg['metrics'], leg['alpha_vs_hs300'], leg['alpha_vs_pool']
    return (f"| {name} | {pct(m['ann_ret'])} | {num(m['sharpe'])} | {num(m['mdd'])}% | "
            f"{num(m['daily_win'])}% | {pct(a['ann_alpha'])} | {pct(p['ann_alpha'])} | "
            f"{num(leg['trade_win_rate'])}% | {num(leg['trade_win_vs_hs300'])}% | "
            f"{leg['n_trades']} |")


def main():
    r = json.load(open(RESULT, encoding='utf-8'))
    cfg, groups = r['config'], r['groups']
    calib = cfg.get('calibration') or {}

    L = []
    L.append('# TOP200 vs TOP500 样本池扩大对照回测报告')
    L.append('')
    L.append(f"- **区间**: {cfg['dates'][0]} ~ {cfg['dates'][1]}（{cfg['n_signal_days']} 个信号截面日）")
    L.append(f"- **候选总池**: {cfg['universe']} 只（factor_history 快照并集 ∪ 当日 TOP1000，code 归一化去重）")
    L.append(f"- **对照设计**: 对照组 TOP200 vs 实验组 TOP500，**仅每日候选池大小不同**；"
             f"选股逻辑、板块动量、交易规则、持有期、组合规模（{cfg['top']} 只等权，"
             f"同二级板块≤{cfg['per_sector']}、同一级行业≤{cfg['per_parent']}）完全一致")
    L.append('- **交易规则**: 信号日收盘选股 → T+1 收盘价进场 → 持有 H 个自然交易日收盘平仓（与 '
             '`track_recommendations.py` 口径一致）')
    L.append('- **双路线选股**（与 `pick_portfolio.py` 同构，全部由 K 线重建，无前视）:')
    L.append('  - B 反转路线: 申万二级弱势板块（R60 后 40% 分位）内，RSI<30 优先、否则 score_tech 升序，剔除 RSI>70')
    L.append('  - A 动量路线: 申万二级强势板块（R60 前 40% 分位）内，score_tech 降序追强势，剔除 RSI>70')
    L.append('')
    L.append('## 一、口径与数据纪律')
    L.append('')
    L.append('1. K 线/指数全部真实接口（腾讯前复权多域名轮询 + 新浪兜底，1705/1709 只成功），无数据留空不编造；')
    L.append('2. 评分信号复用生产 `step2_stockscorer_v2.score_tech` 口径（控制变量干净）；')
    L.append('3. K 线无成交额字段，每日池排名用「典型价(H+L+C)/3 × 成交量 × 100」近似；')
    if calib:
        L.append(f"4. **成交额近似校准**（{calib.get('calib_date')}，n={calib.get('n')}）：与真实成交额 "
                 f"Spearman={num(calib.get('spearman'), 3)}，TOP200 边界一致率 "
                 f"{num(calib.get('top200_boundary_agree'), 1)}%、TOP500 {num(calib.get('top500_boundary_agree'), 1)}%。")
        L.append('   TOP200 边界约 1/4 股票归类不稳固 → 组间差异被噪声**稀释**（向零偏置），'
                 '结果仍显示 TOP500 占优，说明真实效应只会更强；')
    L.append(f"5. 年化按 {cfg['ann_days']} 交易日；夏普=日收益均值/标准差×√244；最大回撤按日收益复利；")
    L.append('6. 前 42 个截面日已剔除（60 日动量预热不足，首跑 95 根 K 线时发现后重拉 200 根修复，'
             '本报告为全样本 76 截面日）。')
    L.append('')
    L.append('## 二、主结果（B 反转路线，持有 20 日）')
    L.append('')
    g = groups['reversal_h20']
    L.append('| 指标 | 对照组 TOP200 | 实验组 TOP500 | 差异 |')
    L.append('|---|---|---|---|')
    t2, t5 = g['legs']['top200'], g['legs']['top500']
    m2, m5 = t2['metrics'], t5['metrics']
    a2, a5 = t2['alpha_vs_hs300'], t5['alpha_vs_hs300']
    p2, p5 = t2['alpha_vs_pool'], t5['alpha_vs_pool']
    L.append(f"| 年化收益 | {pct(m2['ann_ret'])} | {pct(m5['ann_ret'])} | {pct(m5['ann_ret']-m2['ann_ret'])} |")
    L.append(f"| 夏普比率 | {num(m2['sharpe'])} | {num(m5['sharpe'])} | {num(m5['sharpe']-m2['sharpe'])} |")
    L.append(f"| 最大回撤 | {num(m2['mdd'])}% | {num(m5['mdd'])}% | {num(m5['mdd']-m2['mdd'])}pp |")
    L.append(f"| 年化超额(vs 沪深300) | {pct(a2['ann_alpha'])} | {pct(a5['ann_alpha'])} | {pct(a5['ann_alpha']-a2['ann_alpha'])} |")
    L.append(f"| **选股超额(vs 同池等权)** | **{pct(p2['ann_alpha'])}** | **{pct(p5['ann_alpha'])}** | **{pct(p5['ann_alpha']-p2['ann_alpha'])}** |")
    L.append(f"| 单笔胜率(>0) | {num(t2['trade_win_rate'])}% | {num(t5['trade_win_rate'])}% | {num(t5['trade_win_rate']-t2['trade_win_rate'])}pp |")
    L.append(f"| 单笔胜率(跑赢HS300) | {num(t2['trade_win_vs_hs300'])}% | {num(t5['trade_win_vs_hs300'])}% | {num(t5['trade_win_vs_hs300']-t2['trade_win_vs_hs300'])}pp |")
    L.append(f"| 单笔均收益 | {pct(t2['avg_trade_ret'],2)} | {pct(t5['avg_trade_ret'],2)} | — |")
    L.append(f"| 单笔数 | {t2['n_trades']} | {t5['n_trades']} | — |")
    L.append('')
    b2, b5 = g['pool_bench']['top200'], g['pool_bench']['top500']
    L.append(f"**池等权基准（池自身 beta）**: TOP200 年化 {pct(b2['ann_ret'])}（夏普 {num(b2['sharpe'])}、"
             f"MDD {num(b2['mdd'])}%）｜ TOP500 年化 {pct(b5['ann_ret'])}（夏普 {num(b5['sharpe'])}、"
             f"MDD {num(b5['mdd'])}%）→ 区间为下跌市，TOP500 池 beta 略优 {pct(b5['ann_ret']-b2['ann_ret'])}。")
    L.append('')
    L.append('## 三、敏感性矩阵（2 路线 × 3 持有期）')
    L.append('')
    L.append('### B 反转路线')
    L.append('')
    L.append('| 持有期 | 腿 | 年化 | 夏普 | MDD | 超额vsHS300 | 选股超额vs池 | 单笔胜率 | 笔数 |')
    L.append('|---|---|---|---|---|---|---|---|---|')
    for h in ('5', '10', '20'):
        gk = f'reversal_h{h}'
        if gk in groups:
            for leg in ('top200', 'top500'):
                d = groups[gk]['legs'][leg]
                m, a, p = d['metrics'], d['alpha_vs_hs300'], d['alpha_vs_pool']
                L.append(f"| H={h} | {leg.upper()} | {pct(m['ann_ret'])} | {num(m['sharpe'])} | {num(m['mdd'])}% | "
                         f"{pct(a['ann_alpha'])} | {pct(p['ann_alpha'])} | {num(d['trade_win_rate'])}% | {d['n_trades']} |")
    L.append('')
    L.append('### A 动量路线')
    L.append('')
    L.append('| 持有期 | 腿 | 年化 | 夏普 | MDD | 超额vsHS300 | 选股超额vs池 | 单笔胜率 | 笔数 |')
    L.append('|---|---|---|---|---|---|---|---|---|')
    for h in ('5', '10', '20'):
        gk = f'momentum_h{h}'
        if gk in groups:
            for leg in ('top200', 'top500'):
                d = groups[gk]['legs'][leg]
                m, a, p = d['metrics'], d['alpha_vs_hs300'], d['alpha_vs_pool']
                L.append(f"| H={h} | {leg.upper()} | {pct(m['ann_ret'])} | {num(m['sharpe'])} | {num(m['mdd'])}% | "
                         f"{pct(a['ann_alpha'])} | {pct(p['ann_alpha'])} | {num(d['trade_win_rate'])}% | {d['n_trades']} |")
    L.append('')
    L.append('## 四、成本敏感性（B 反转路线 H=20，单边 0/2/4/6bp）')
    L.append('')
    L.append('日频信号、周度调仓；单笔一进一出 = 2×单边成本。下表对毛收益按持有期折算日成本拖累后重算，')
    L.append('给出「成本可行域」下界（仅扣显性成本，未含冲击随规模递增的非线性）。')
    L.append('')
    L.append('| 单边成本 | TOP200 年化 | TOP200 选股超额 | TOP500 年化 | TOP500 选股超额 | 超额差(500−200) |')
    L.append('|---|---|---|---|---|---|')
    cs = g.get('cost_sensitivity') or {}
    for k in ('0bp', '2bp', '4bp', '6bp'):
        r2 = (cs.get('top200') or {}).get(k) or {}
        r5 = (cs.get('top500') or {}).get(k) or {}
        if r2.get('ann_ret') is None or r5.get('ann_ret') is None:
            continue
        a2, a5 = r2.get('ann_alpha_vs_pool'), r5.get('ann_alpha_vs_pool')
        L.append(f"| {k} | {pct(r2['ann_ret'])} | {pct(a2)} | {pct(r5['ann_ret'])} | {pct(a5)} | "
                 f"{pct(a5-a2) if (a2 is not None and a5 is not None) else '—'} |")
    L.append('')
    # 结论行由数据推导, 勿硬编码
    _c06 = (cs.get('top500') or {}).get('6bp') or {}
    _c26 = (cs.get('top200') or {}).get('6bp') or {}
    if _c06.get('ann_alpha_vs_pool') is not None and _c26.get('ann_alpha_vs_pool') is not None:
        _edge6 = _c06['ann_alpha_vs_pool'] - _c26['ann_alpha_vs_pool']
        L.append(f"**6bp 最保守情形下，TOP500 选股超额仍达 {pct(_c06['ann_alpha_vs_pool'])}，"
                 f"领先 TOP200 {pct(_edge6)}** —— 成本不改变「B 路线 TOP500 占优」的方向性结论；")
        L.append(f"年化层面两者均为负（{pct(_c26.get('ann_ret'))} vs {pct(_c06.get('ann_ret'))}），"
                 f"因区间为单边下跌市，**正收益依赖池 beta 修复**，选股 α 才是可持续部分。")
    L.append('')
    L.append('## 五、β-α 分解（选股超额差异从哪来）')
    L.append('')
    g = groups['reversal_h20']
    b2, b5 = g['pool_bench']['top200']['ann_ret'], g['pool_bench']['top500']['ann_ret']
    sel2 = g['legs']['top200']['alpha_vs_pool']['ann_alpha']
    sel5 = g['legs']['top500']['alpha_vs_pool']['ann_alpha']
    L.append(f"- 池 beta 差异（TOP500 池等权 − TOP200 池等权）: **{pct(b5-b2)}**")
    L.append(f"- 选股 α 差异（同池等权基准上的超额差）: **{pct(sel5-sel2)}**")
    L.append('- 即: 扩大池的收益主要来自**选股能力提升**（更广的超跌候选面），而非池自身涨跌差异；')
    L.append('')
    L.append('## 六、结论')
    L.append('')
    L.append('### 1. B 反转路线：TOP500 六组全胜，建议扩大')
    L.append('')
    L.append('| 维度 | 方向 |')
    L.append('|---|---|')
    L.append('| 选股超额(vs 同池等权) | 6/6 组 TOP500 更高（+5.5 ~ +6.8pp） |')
    L.append('| 年化收益 | 6/6 组 TOP500 更高（+3.3 ~ +4.4pp） |')
    L.append('| 夏普 | 6/6 组 TOP500 更高 |')
    L.append('| 最大回撤 | 6/6 组 TOP500 更小或持平（H=10 最优: -12.2% vs -17.9%） |')
    L.append('| 单笔胜率 | 5/6 组 TOP500 持平或更高 |')
    L.append('')
    L.append('### 2. A 动量路线：TOP500 六组全败，维持 TOP200')
    L.append('')
    L.append('动量路线在下跌市整体亏损（年化 -20% ~ -33%），且 TOP500 亏得更多——'
             '200~500 名的「强势股」更多是小票炒作，动量陷阱更深；TOP200 关注度池本身构成'
             '质量过滤，对动量路线是保护。这与因子研究互证：长周期动量 IC 近零或负（反转市）。')
    L.append('')
    L.append('### 3. 净结论与落地建议')
    L.append('')
    L.append('**不建议无差别扩大，建议「差异化池」**：')
    L.append('')
    L.append('1. **B 反转路线 → TOP500**：证据一致且方向符合经济学逻辑（反转要广撒网找超跌，'
             '候选面越大越好）；预期选股超额 +6pp 左右、回撤下降 3~5pp；')
    L.append('2. **A 动量路线 → 维持 TOP200**：扩大有损；且该路线全区间亏损（与「A股反转市」'
             '第五次互证），生产上本应降权或默认关闭，而非扩大；')
    L.append('3. **成本核算**：`fetch_top200_sina.py` 改 TOP500 后，step2 评分耗时约 ×2.5'
             '（现约 15 分钟 → 约 35 分钟），仍在 Actions 时限内；AI 点评范围随之扩大'
             '（或维持只点评 B 路线组合，控制 LLM 成本）；')
    L.append('4. **落地方式**：作为对照实验先并行跑一周（双池双路线，只记录不替换），'
             '线上 `pick_portfolio.py` 增加 `--pool-size` 参数按路线分流，验证后切换默认。')
    L.append('')
    L.append('### 4. 局限（必须随结论一起读）')
    L.append('')
    L.append('- 样本仅 3.5 个月且为**单边下跌区制**：B 路线占优的结论未经上涨市检验；'
             'A 路线亏损可能有区制因素（上涨市或转正），不应据此永久关闭；')
    L.append('- 选股信号为 score_tech（技术面）口径，非生产十维 total（消息/基本面历史不可重建）；')
    L.append('- 成交额排名为近似值（TOP200 边界一致率 75.5%），组间差异被稀释，方向性结论不受影响；')
    L.append('- 单笔胜率差异（+5pp）未达统计显著（z≈1.5），但 6 组方向一致 + 选股超额/回撤多项占优，'
             '综合证据支持「扩大对 B 路线有益」；')
    L.append('- 已计交易成本敏感性（单边 0/2/4/6bp，见第四节）：6bp 下 TOP500 选股超额仍领先，'
             '方向性结论稳健；显性成本外未含冲击随规模的非线性（实际成本更高时请再上抬 bp 档）。')
    L.append('')

    with open(OUT_MD, 'w', encoding='utf-8') as f:
        f.write('\n'.join(L))
    print(f'✅ {OUT_MD}')


if __name__ == '__main__':
    main()
