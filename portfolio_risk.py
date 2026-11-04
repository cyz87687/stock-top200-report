#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""portfolio_risk.py —— 组合层风险度量（round122 / P0-3）

## 为什么必须有这个模块

`pick_portfolio.py` 此前**完全没有风控层**：全文件 grep `vol|mdd|drawdown|stop|var`
零命中。它只回答「选哪几只」，不回答「这个组合有多危险」。而下游
`regime_model.risk_budget()` 明明算出了仓位上限（今日 final_hi=0.2），却
**没有任何代码消费它** —— 风险预算算给空气。

金融工程视角看，缺这三样等于没有组合管理：
1. **波动率**：没有它就无法做风险预算（仓位 × 波动 = 风险敞口恒等式）
2. **相对基准的 beta**：不知道组合是在承担 beta 还是真在选股
3. **集中度 / 有效只数**：等权 6 只的"分散"和等权 13 只完全不同

## 做法

读 `sector/portfolio_<date>.json`（含上一步写入的 `weights`），用
`data/klines_long.json` 的前复权收盘价计算：

- **组合年化波动** `σ_p = std(日收益) × √244`（按 weights 加权，含现金 0 收益）
- **相对池等权 beta** `β = cov(r_p, r_pool) / var(r_pool)`
  （基准 = 当日候选池等权，与本项目「超额以 POOL_EW 为基准」的口径一致）
- **区间 MDD / 区间收益**：从推荐日到数据截止日
- **集中度**：HHI、有效只数、单票最大权重、行业最大权重
- **风险预算对齐**：`w_total × σ_p` 是否为合理风险敞口，与 regime `target_vol` 对比

## ⚠️ 五个必须披露的口径限制（写进输出 caveats，禁删）

1. **样本极短**：路径长度通常 <15 个交易日，年化波动/夏普的置信度很低，
   只能横向比较不同组合，**不可外推**为"这个组合年化波动 25%"。
2. **无前视但无成本**：权重按等权假设，不含冲击成本/佣金/印花税；
   实测日频调仓成本 32~46%/年（`audit_round118d`）→ 此处的"收益"是毛收益。
3. **beta 基准是池等权**，不是沪深300等宽基指数。池等权本身波动约 25%，
   与组合（约 12%）差异大，beta 会系统性偏离 1。
4. **生存者偏差**：只用当日仍在库内的标的，退市/长期停牌者已不在 K 线库。
5. **波动是历史实现的**，不是预测。`regime_model` 已实证方向不可预测
   （2873 日 12/12 组低于多数类基线）→ 本模块只度量，**不预测**。

## 用法

    python3 portfolio_risk.py                      # 处理最新组合
    python3 portfolio_risk.py --date 2026-11-03    # 指定日期
    python3 portfolio_risk.py --inplace            # 回填进 portfolio json
"""
import json
import os
import sys
import math
import argparse
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
SECTOR_DIR = os.path.join(HERE, 'sector')
KLONG = os.path.join(HERE, 'data', 'klines_long.json')
OUT = os.path.join(HERE, 'portfolio_risk.json')

TRADING_DAYS = 244
MIN_DAYS_FOR_VOL = 8        # 与 portfolio_metrics.MIN_DR 同口径，防小样本伪值
MIN_DAYS_FOR_BETA = 20      # beta 需要更多点，否则 cov/var 比值极不稳定

CAVEATS = [
    "窗口=回望60个交易日（组合日==数据截止日时无未来数据）：度量的是**持仓结构的固有风险**，不是未来收益预测",
    "毛收益：不含冲击成本/佣金/印花税；实测日频调仓成本32~46%/年（audit_round118d）",
    "beta基准=当日候选池等权（POOL_EW，非宽基指数）；池等权波动约25%，beta会系统性偏离1",
    "生存者偏差：退市/长停标的已不在K线库，回撤被系统性低估",
    "波动为历史实现值，不是预测；regime已实证方向不可预测，本模块只度量不预测",
    "回望窗口内假设「今天这批持仓一直在手里」，与实际换手路径不符，仅作风险量级参照",
]


def _c6(code):
    s = str(code or '').strip().lower()
    for p in ('sh', 'sz', 'bj'):
        if s.startswith(p):
            return s[len(p):]
    return s


def _close(px, c6, d):
    """取前复权收盘价。bar = [open, close, high, low, volume] → index 1"""
    b = (px.get(c6) or {}).get(d)
    if isinstance(b, (list, tuple)) and len(b) >= 2:
        v = b[1]
        if isinstance(v, (int, float)) and v == v:
            return float(v)
    if isinstance(b, (int, float)) and b == b:
        return float(b)
    return None


def _calendar(px):
    """交易日历委托 kline_calendar（唯一口径来源）"""
    import kline_calendar as kc
    return kc.build_valid(px)


def _std(xs):
    n = len(xs)
    if n < 2:
        return 0.0
    m = sum(xs) / n
    return (sum((x - m) ** 2 for x in xs) / (n - 1)) ** 0.5


def _cov(xs, ys):
    n = min(len(xs), len(ys))
    if n < 2:
        return 0.0
    mx, my = sum(xs[:n]) / n, sum(ys[:n]) / n
    return sum((xs[i] - mx) * (ys[i] - my) for i in range(n)) / (n - 1)


def _mdd(nav):
    """返回 (mdd, mdd_at)。nav = [(date, value)]"""
    if not nav:
        return 0.0, None
    peak = nav[0][1]
    mdd, at = 0.0, None
    for d, v in nav:
        if v > peak:
            peak = v
        dd = (v - peak) / peak if peak > 0 else 0.0
        if dd < mdd:
            mdd, at = dd, d
    return mdd, at


def compute_one(pf, px, cal, bench_codes=None):
    """算单个路线组合的风险指标。

    pf: portfolio json 里 `portfolios[mode]` 那一块（含 picks / weights / risk）
    返回 risk dict（含所有指标 + 不可计算时的原因）
    """
    picks = pf.get('picks') or []
    weights = pf.get('weights') or {}
    rk = pf.get('risk') or {}
    w_total = float(rk.get('w_total') or 1.0)
    cash = float(rk.get('cash_weight') or 0.0)
    out = {'n_picks': len(picks), 'w_total': w_total, 'cash_weight': cash,
           'vol_ann': None, 'vol_ann_of_equity': None, 'beta_vs_pool': None,
           'mdd': None, 'mdd_at': None, 'period_ret': None,
           'n_days': 0, 'computable': False, 'reason': None,
           'hhi': rk.get('hhi'), 'effective_n': rk.get('effective_n'),
           'single_weight_max_of_equity': rk.get('single_weight_max_of_equity'),
           'single_weight_max_of_assets': rk.get('single_weight_max_of_assets'),
           'sector_max_share': None, 'caveats': CAVEATS}

    if not picks:
        out['reason'] = '无持仓，无法计算'
        return out

    # ── 持仓行业集中度（按占股票仓位口径）──
    sect = {}
    for r in picks:
        k = r.get('parent') or r.get('sector_l2') or '未分类'
        sect[k] = sect.get(k, 0) + 1
    out['sector_max_share'] = round(max(sect.values()) / len(picks), 4) if sect else None
    out['sector_max_name'] = max(sect, key=sect.get) if sect else None
    out['n_sectors_l1'] = len(sect)

    # ── 定位起点日期：用 portfolio json 的 date ──
    d0 = pf.get('_date') or pf.get('date')
    if not d0:
        out['reason'] = '组合缺少 date 字段'
        return out
    cidx = {d: i for i, d in enumerate(cal)}
    if d0 not in cidx:
        out['reason'] = f'组合日 {d0} 不在交易日历内（非有效截面）'
        return out
    i0 = cidx[d0]

    # ⚠️⚠️ 关键设计决定（round122）：组合日 == 数据截止日时，**未来数据不存在**，
    #   用「d0 → 截止日」的路径算波动/回撤会得到 0 天 → 永远不可计算。
    #   而「组合建立后能赚多少」本来就是不可知的，硬算等于偷看未来。
    #   金融工程的正确做法：用**回望窗口**度量该组合「若在过去持有会承受多大风险」
    #   —— 即把**今天的持仓权重**套到**过去 N 个交易日**的收益上，得到
    #   「历史回溯波动 / 回溯回撤」。这衡量的是**持仓结构的固有风险**，
    #   与「未来收益」无关，也不含前视（用的是历史价格，不是未来价格）。
    #   窗口取 LOOKBACK=60 个交易日（约 3 个月），覆盖本项目 K 线库 306 日的 1/5。
    #   ⚠️ 必须显式标注 window='lookback'，不能与 'forward'（跟踪期实测）混用。
    LOOKBACK = 60
    i_start = max(0, i0 - LOOKBACK)
    fwd_days_avail = len(cal) - 1 - i0
    mode_w = 'lookback' if fwd_days_avail < MIN_DAYS_FOR_VOL else 'forward'
    if mode_w == 'forward':
        i_start = i0
    out['window'] = mode_w
    out['window_start'] = cal[i_start]
    out['window_end'] = cal[-1] if mode_w == 'forward' else cal[i0]
    out['fwd_days_available'] = fwd_days_avail

    # ── 每只票的权重（占**股票仓位**，用于算组合收益形状；现金单独处理）──
    # 注意：组合收益必须按「占总资产」权重算，否则 w_total 信息被抹掉。
    w_asset = {_c6(c): float(w) for c, w in weights.items()}
    codes = [_c6(r.get('code')) for r in picks]

    # ── 逐日组合收益（占总资产口径，现金 0 收益）──
    days = []
    prev = {}
    for i in range(i_start, len(cal) if mode_w == 'forward' else i0 + 1):
        d = cal[i]
        cur = {}
        for c in codes:
            v = _close(px, c, d)
            if v is not None:
                cur[c] = v
        if not prev:
            prev = cur
            days.append((d, 0.0, len(cur)))
            continue
        ret = 0.0
        wsum = 0.0
        for c in codes:
            if c in cur and c in prev and prev[c] > 0:
                w = w_asset.get(c, 0.0)
                ret += w * (cur[c] - prev[c]) / prev[c]
                wsum += w
        days.append((d, ret, len(cur)))
        prev = cur

    n_days = len(days) - 1
    out['n_days'] = n_days
    if n_days < 2:
        out['reason'] = f'路径仅 {n_days} 个交易日，不足 2 天'
        return out

    # 净值（从 1.0 起，含现金的 0 收益 → 组合净值 = 1 + 累计加权收益）
    nav = [(days[0][0], 1.0)]
    for d, r, _ in days[1:]:
        nav.append((d, nav[-1][1] * (1.0 + r)))
    mdd, mdd_at = _mdd(nav)
    out['mdd'] = round(mdd * 100, 2)
    out['mdd_at'] = mdd_at
    out['period_ret'] = round((nav[-1][1] - 1.0) * 100, 2)

    drs = [r for _, r, _ in days[1:]]
    sd = _std(drs)
    ann_vol = sd * math.sqrt(TRADING_DAYS)
    # 占**总资产**口径的波动（w_total 已含在权重里 → 这就是组合层真实波动）
    if n_days >= MIN_DAYS_FOR_VOL:
        out['vol_ann'] = round(ann_vol * 100, 2)
        # 换算到「若满仓」的等价波动（去掉现金稀释），便于与其他组合横比
        out['vol_ann_of_equity'] = (round(ann_vol / w_total * 100, 2)
                                    if w_total > 1e-6 else None)
    else:
        out['reason'] = f'路径 {n_days} 天 < {MIN_DAYS_FOR_VOL} 天，波动/夏普不给值（防小样本伪值）'

    # ── beta vs **候选池等权**（本项目超额基准口径 POOL_EW）──
    # ⚠️ round122 踩坑：起初想用「持仓自身等权」当基准，但这与组合权重完全共线
    #   → 分子 cov 与分母 var 同源 → beta 恒等于 w_total（实测恒为 0.20），
    #   **纯属数学巧合，零信息量**。正确基准必须是**当日候选池**（与 pick_portfolio
    #   的筛选范围一致），代表「同池随机等权持有」的机会成本。
    #   候选池名单从 portfolio json 的 `candidate_codes` 取（pick_portfolio 写入）；
    #   缺失则退化为「K线库抽样等权」，并显式标注口径。
    if n_days >= MIN_DAYS_FOR_BETA:
        bench = [_c6(c) for c in (pf.get('candidate_codes') or [])]
        bench_src = '候选池等权(POOL_EW)'
        if len(bench) < 5:
            bench = [_c6(c) for c in list(px.keys())[:800]]
            bench_src = 'K线库抽样800只等权（候选池名单缺失，退化口径）'
        b_rets, prevb = [], {}
        _end = len(cal) if mode_w == 'forward' else i0 + 1
        for i in range(i_start, _end):
            d = cal[i]
            cur = {}
            for c in bench:
                v = _close(px, c, d)
                if v is not None:
                    cur[c] = v
            if not prevb:
                prevb = cur
                b_rets.append(0.0)
                continue
            rs = [(cur[c] - prevb[c]) / prevb[c]
                  for c in cur if c in prevb and prevb[c] > 0]
            b_rets.append(sum(rs) / len(rs) if len(rs) >= 3 else 0.0)
            prevb = cur
        p_rets = [r for _, r, _ in days]
        n = min(len(p_rets), len(b_rets))
        var_b = _cov(b_rets[:n], b_rets[:n])
        out['beta_bench'] = bench_src
        out['bench_n'] = len(bench)
        if var_b > 1e-12:
            out['beta_vs_pool'] = round(_cov(p_rets[:n], b_rets[:n]) / var_b, 3)
            out['bench_vol_ann'] = round(_std(b_rets[:n]) * math.sqrt(TRADING_DAYS) * 100, 2)
        else:
            out['beta_vs_pool'] = None
            out['beta_bench'] = f'{bench_src}；基准方差≈0，不可计算'
    else:
        out['beta_bench'] = f'路径 {n_days} 天 < {MIN_DAYS_FOR_BETA} 天，beta 不给值'

    # ── 风险预算对齐 ──
    rp = os.path.join(HERE, 'regime_forecast.json')
    if os.path.exists(rp):
        try:
            rf = json.load(open(rp, encoding='utf-8'))
            rb = rf.get('risk_budget') or {}
            tv = rb.get('target_vol')
            out['budget_target_vol'] = tv
            if tv and out.get('vol_ann') is not None:
                # 组合实际风险敞口 = 波动（已含仓位稀释）；与 target_vol 直接比
                out['vol_vs_target'] = round(out['vol_ann'] / 100.0 / float(tv), 3)
                out['vol_aligned'] = bool(out['vol_ann'] / 100.0 <= float(tv) + 1e-9)
        except Exception as e:
            out['budget_note'] = f'regime_forecast 读取失败: {str(e)[:40]}'

    out['computable'] = out['vol_ann'] is not None
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--date', default=None, help='组合日期，缺省取最新 portfolio_*.json')
    ap.add_argument('--inplace', action='store_true',
                    help='把 risk 块回填进 sector/portfolio_<date>.json')
    ap.add_argument('--out', default=OUT)
    args = ap.parse_args()

    if not os.path.exists(KLONG):
        print(f"❌ 未找到 {KLONG}")
        return 1

    if args.date:
        pf_path = os.path.join(SECTOR_DIR, f'portfolio_{args.date}.json')
    else:
        fs = sorted([f for f in os.listdir(SECTOR_DIR)
                     if f.startswith('portfolio_') and f.endswith('.json')
                     and 'latest' not in f], reverse=True)
        pf_path = os.path.join(SECTOR_DIR, fs[0]) if fs else None
    if not pf_path or not os.path.exists(pf_path):
        print(f"❌ 未找到组合文件: {pf_path}")
        return 1

    print(f"读取组合: {os.path.basename(pf_path)}")
    pj = json.load(open(pf_path, encoding='utf-8'))
    print("加载 K 线库...")
    px = json.load(open(KLONG, encoding='utf-8'))
    cal = _calendar(px)
    print(f"  交易日历 {len(cal)} 天（{cal[0]} ~ {cal[-1]}），K线 {len(px)} 只")

    date = pj.get('date')
    result = {'date': date, 'source': os.path.basename(pf_path),
              'generated_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
              'data_cutoff': cal[-1], 'cal_days': len(cal),
              'portfolios': {}, 'caveats': CAVEATS}

    print(f"\n{'路线':<10}{'只数':>5}{'仓位':>7}{'天数':>5}{'年化波动':>10}{'区间收益':>10}{'MDD':>9}{'beta':>8}{'有效N':>7}")
    for mode in ('momentum', 'reversal'):
        pf = (pj.get('portfolios') or {}).get(mode)
        if not pf:
            continue
        pf['_date'] = date
        r = compute_one(pf, px, cal)
        result['portfolios'][mode] = r
        vs = f"{r['vol_ann']:>9.1f}%" if r['vol_ann'] is not None else f"{'n/a':>10}"
        rs = f"{r['period_ret']:>9.2f}%" if r['period_ret'] is not None else f"{'n/a':>10}"
        md = f"{r['mdd']:>8.2f}%" if r['mdd'] is not None else f"{'n/a':>9}"
        be = f"{r['beta_vs_pool']:>8.2f}" if r['beta_vs_pool'] is not None else f"{'n/a':>8}"
        ef = f"{r['effective_n']:>7.1f}" if r.get('effective_n') is not None else f"{'n/a':>7}"
        print(f"{mode:<10}{r['n_picks']:>5}{r['w_total']*100:>6.0f}%{r['n_days']:>5}{vs}{rs}{md}{be}{ef}")
        print(f"   窗口: {r.get('window')} {r.get('window_start')} ~ {r.get('window_end')}"
              f"（未来可用 {r.get('fwd_days_available')} 天）")
        if r.get('reason'):
            print(f"   ⚠️ {r['reason']}")
        if r.get('beta_bench'):
            print(f"   beta 基准: {r['beta_bench']}")

    # ── 风险预算对齐检查 ──
    print("\n风险预算对齐（regime target_vol vs 组合实际波动）:")
    for mode, r in result['portfolios'].items():
        tv = r.get('budget_target_vol')
        if tv and r.get('vol_ann') is not None:
            ok = r.get('vol_aligned')
            print(f"  {mode}: 组合波动 {r['vol_ann']:.1f}% vs 目标 {float(tv)*100:.1f}%"
                  f" → {'✅ 在预算内' if ok else '🔴 超预算'}")
        elif tv:
            print(f"  {mode}: 目标 {float(tv)*100:.1f}%，组合波动不可计算（样本不足）")
        else:
            print(f"  {mode}: regime 无 target_vol，无法对齐")

    print("\n波动解剖（为什么组合波动低 ≠ 持仓安全）:")
    for mode, r in result['portfolios'].items():
        ve = r.get('vol_ann_of_equity')
        bv = r.get('bench_vol_ann')
        if ve is not None and bv is not None:
            print(f"  {mode}: 组合层 {r['vol_ann']:.1f}%（含 {r['w_total']*100:.0f}% 仓位）"
                  f" / 满仓等价 {ve:.1f}% / 候选池 {bv:.1f}%"
                  f" → 满仓口径比池{'高' if ve > bv else '低'} {abs(ve-bv):.1f}pp")
        elif ve is not None:
            print(f"  {mode}: 组合层 {r['vol_ann']:.1f}% / 满仓等价 {ve:.1f}%（候选池基准不可用）")
    print("  ⚠️ 组合层波动低主要来自**仓位轻**（w_total），不是持仓更安全——满仓口径才是持仓本身的风险")

    print("\n⚠️ 口径限制（必须与上面数字一起读）:")
    for i, c in enumerate(CAVEATS, 1):
        print(f"  {i}. {c}")

    json.dump(result, open(args.out, 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
    print(f"\n📄 已写入 {args.out}")

    # ── 回填进 portfolio json ──
    if args.inplace:
        for mode, r in result['portfolios'].items():
            pf = (pj.get('portfolios') or {}).get(mode)
            if pf is not None:
                old = pf.get('risk') or {}
                merged = dict(old)
                merged.update({k: r[k] for k in
                               ('vol_ann', 'vol_ann_of_equity', 'beta_vs_pool',
                                'mdd', 'mdd_at', 'period_ret', 'n_days',
                                # round122 修复：回填白名单**漏了窗口三字段**，
                                #   导致 gen_report 的「窗口 xxx~xxx（回望 N 个
                                #   交易日）」整句静默不渲染 —— 而这句话是判断
                                #   「这些波动数字是什么口径」的唯一依据（回望
                                #   还是前向、多少天）。字段在白名单缺失时
                                #   下游只能拿到 None 然后走 `if rk.get("window")`
                                #   的假分支，页面看着正常、其实少了一句关键限定。
                                #   与 round122 的 `_ev_gap` 被可选区块吞掉完全同型：
                                #   **数据层有、回填层丢、渲染层静默**。
                                'window', 'window_start', 'window_end',
                                'fwd_days_available',
                                'beta_bench', 'bench_n', 'bench_vol_ann',
                                'budget_target_vol', 'vol_aligned',
                                'sector_max_share', 'sector_max_name', 'n_sectors_l1',
                                'computable', 'reason', 'caveats')})
                pf['risk'] = merged
        json.dump(pj, open(pf_path, 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
        print(f"✅ risk 块已回填 {os.path.basename(pf_path)}")
    return 0


if __name__ == '__main__':
    sys.exit(main())
