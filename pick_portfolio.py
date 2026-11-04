#!/usr/bin/env python3
"""每日选股: 以板块动量为主轴, 从 TOP200 评分结果中构建组合

选股流程(两条并行路线):
  A. momentum 路线(用户原始思路)
     板块: 60日动量第1 ∩ 10日动量前3  → 无则放宽 R60<=3 ∩ R10<=5 → R60<=5 ∩ R10<=10 → R60<=10
     个股: 板块内按综合评分 total 降序, 剔除 RSI>70
  B. reversal 路线(回测证据)
     板块: 60日动量末3 ∩ 10日动量末10 → 放宽 R60>70 ∩ R10>70 → R60>40 ∩ R10>40 → R60>40
     个股: 板块内 RSI<30 优先, 其次按 total 降序; 剔除 RSI>70

共同约束: 每板块最多 --per-sector 只, 组合总规模 --top 只, 等权。
回测依据见 sector/backtest_ref.json(申万二级 124 个板块, 2021-01-04~2026-09-18, 1386 交易日)。

v4.18（round122，金融工程师视角 P0/P1 落地）:
  · P0-1 `max_single_stock` 从死参数改为真约束 —— 翻译成 min_holdings,
    实际目标只数 = max(--top, ceil(1/max_single_stock)) = max(10, 13) = 13
  · P0-2 板块层级搜索的停止条件从「候选数」改为「组合约束下可达只数」
    (旧版 L0 命中 60 只候选就 break, 但全在同一申万一级行业, per_parent=2 砍到 2 只)
  · P0-3 接入 regime 风险预算 final_hi 作为 w_total, 输出真实 weights(含现金)
  · P1   行业暴露偏离由「只查超配」改为**双向**, 低配也披露

用法:
  python3 pick_portfolio.py [scored_json] [--top 10] [--per-sector 3] [--out sector/portfolio_<date>.html]
  对照开关: --no-reach-check(旧停止条件) --no-risk(强制满仓) --no-l1-cap(关行业偏离)
"""
import os
import sys
import json
import glob
import math as _math
import argparse
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
SECTOR_DIR = os.path.join(HERE, 'sector')
MAP_PATH = os.path.join(SECTOR_DIR, 'sw_map.json')
MOM_PATH = os.path.join(SECTOR_DIR, 'sector_momentum_latest.json')
BT_REF = os.path.join(SECTOR_DIR, 'backtest_ref.json')
CONFIG_PATH = os.path.join(HERE, 'config.json')

# config.json 缺省值（与 fetch_top200_sina.py 保持同一套约定）
_DEF_CFG = {
    'route_pool_map': {'reversal': 500, 'momentum': 200},
    'momentum_route_weight': 0.5,
    'liquidity_filter': {'enabled': True, 'min_avg_amount_20d': 5e7, 'min_avg_turnover_rate': 0.01},
    'industry_limits': {'max_single_stock': 0.08, 'max_l2_sector': 0.25,
                        'max_l1_parent': 0.35, 'max_l1_deviation_pp': 8},
}


def load_config():
    """读 config.json；缺失/损坏则回退内置缺省（保证向后可用）。"""
    cfg = json.loads(json.dumps(_DEF_CFG))   # deep copy
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, encoding='utf-8') as _f:
                user = json.load(_f)
            for k in cfg:
                if isinstance(cfg[k], dict) and isinstance(user.get(k), dict):
                    cfg[k].update(user[k])
                elif k in user:
                    cfg[k] = user[k]
        except Exception as e:
            print(f"  ⚠️ config.json 解析失败({str(e)[:40]}), 使用缺省")
    return cfg


def code6(code):
    s = ''.join(ch for ch in str(code) if ch.isdigit())
    return s[-6:].zfill(6) if len(s) >= 6 else None


def pick_sectors(rows, mode):
    """返回层级列表 [(名称, 判定函数)]，按严格→放宽排序"""
    n = len(rows)
    r60 = {r['sector']: r['r60'] for r in rows}
    r10 = {r['sector']: r['r10'] for r in rows}
    if mode == 'momentum':
        return [
            ('L0 严格: 60日动量第1 ∩ 10日动量前3', lambda s: r60[s] == 1 and r10[s] <= 3),
            ('L1 放宽: 60日动量前3 ∩ 10日前5', lambda s: r60[s] <= 3 and r10[s] <= 5),
            ('L2 再放宽: 60日前5 ∩ 10日前10', lambda s: r60[s] <= 5 and r10[s] <= 10),
            ('L3 兜底: 60日动量前10', lambda s: r60[s] <= 10),
            ('L4 最终兜底: 60日动量前20', lambda s: r60[s] <= 20),
        ]
    return [
        ('L0 严格: 60日动量末3 ∩ 10日动量末10', lambda s: r60[s] >= n - 2 and r10[s] >= n - 9),
        ('L1 放宽: 60日排名>70 ∩ 10日排名>70', lambda s: r60[s] > 70 and r10[s] > 70),
        ('L2 再放宽: 60日排名>40 ∩ 10日排名>40', lambda s: r60[s] > 40 and r10[s] > 40),
        ('L3 兜底: 60日排名>40', lambda s: r60[s] > 40),
        ('L4 最终兜底: 60日排名>20', lambda s: r60[s] > 20),
    ]


def select(rows, results, mode, min_cands, reach_fn=None):
    """逐级放宽直到「选中板块 ∩ 候选池」在**组合约束下实际可达只数** >= min_cands

    v4.18 P0-2（round122）：
      旧版判据是 `len(cands) >= min_cands`（候选**数量**），然后在 L0 就 break。
      但候选数 ≠ 能选出的只数：`build()` 随后还会过 min_total / per_l2 / per_l1 /
      l1_deviation 四道闸。实测 2026-11-03 reversal 路线 —— L0 选中「半导体」1 个
      板块、60 只候选（远超 10）→ 立即 break，可这 60 只**全在「电子」一个申万一级
      行业**，`per_parent=2` 直接砍到 2 只，最终 top=10 只选出 2 只，且全程无告警。
      ⇒ 搜索的停止条件错了，不是约束太紧。修复方式**不是放宽风控**，而是把
        「可达只数」交给 reach_fn（内部 dry-run 一次 build）来判断是否够格停止。

    reach_fn: 可选回调 `cands -> int`，返回该候选集在组合约束下实际能选出的只数。
              ⚠️ 不传时退化为旧行为（只看候选数），仅供对照/回滚。
    返回 (sel, level_name, cands, trace)
    """
    r60 = {r['sector']: r['r60'] for r in rows}
    levels = pick_sectors(rows, mode)
    trace = []
    best = None
    for name, fn in levels:
        sel = set(s for s in r60 if fn(s))
        cands = [r for r in results
                 if r.get('sector_l2') in sel
                 and not ((r.get('tech') or {}).get('rsi') or 0) > 70]
        reach = len(cands) if reach_fn is None else int(reach_fn(cands))
        trace.append({'level': name, 'n_sectors': len(sel), 'n_cands': len(cands),
                      'n_reachable': reach, 'sectors': sorted(sel)[:12]})
        if best is None or reach > best[4]:
            best = (sel, name, cands, list(trace), reach)
        if reach >= min_cands:
            return sel, name, cands, list(trace)
    if best is None:
        return set(), levels[-1][0], [], trace
    return best[0], best[1], best[2], best[3]


def build(cands, mode, per_sector, per_parent, top, min_total=12.0, cfg=None,
          no_l1_cap=False, w_total=1.0, want_diag=False):
    """构建组合。约束：
      - min_total  : 综合评分低于该值的个股不入选(默认12分, 只推 B 级及以上)
      - per_sector : 同一申万二级板块最多 N 只
      - per_parent : 同一申万一级行业(parent)最多 N 只 —— 防行业集中(如多只银行股)
      - cfg.industry_limits: 以「持仓占比」表达的更细上限(单票/二级/一级),
        与 per_* 的「只数上限」叠加取更严者(等权下占比≈只数/top)
      - v3.2 P0-2: 申万一级行业**暴露偏离**上限(相对等权基准 ≤ max_l1_deviation_pp 个百分点)。
        只数约束 ≠ 暴露约束 —— TOP500 行业结构偏中小盘后, 固定只数在不同行业
        不再等价; 反转信号具板块级聚集性, 易退化为行业轮动豪赌。此处按真实
        「已选权重占比 - 等权基准占比」检查, 超限行业按评分顺延替补。
        no_l1_cap=True 时关闭(对照用)。

    ---- v4.18 P0-1（round122）：把 max_single_stock 从死参数改成真约束 ----
    `config.json:industry_limits.max_single_stock` 此前**全项目无人读取**（只有定义
    和缺省值），实测当日 momentum 6 只 → 单票 16.7%、reversal 2 只 → 单票 50%，
    是 8% 上限的 2~6 倍，且无任何告警。
    翻译口径（与 per_sector/per_parent 的既有语义**保持一致**，都按「占股票仓位
    的比例」而非「占总资产」，否则同一 dict 里三个键会用两套单位）：
        min_holdings = ceil(1.0 / max_single_stock)      # 等权下单票 ≤ 8% ⇒ ≥13 只
    语义裁决（**必须显式，不能让两个约束互相静默压制**）：
        · `top`            = 名义目标只数，**上限**语义（不许超）
        · `min_holdings`   = 单票上限反推的**下限**（不许少）
        · 实际目标 `target = max(top, min_holdings)`
        · per_sector / per_parent 的只数换算一律按 **target** 推导，而不是原 top
          —— 否则「为了不超 8% 而必须持 13 只」时，行业上限还卡在按 10 只算的
          4 只，两约束直接打架，静默压制回原状。
        · 若 target 仍凑不满，**不报错、不静默**：交给 shortfall 结构显式披露。

    w_total: 组合股票仓位占总资产比例（来自 regime 风险预算 final_hi）。仅用于
             输出「占总资产」口径的真实单票权重，**不参与约束推导**（见上：三个
             limit 键统一按仓位口径）。缺省 1.0 = 满仓。

    返回:
      want_diag=False → (picked, _capped_log)                     # 向后兼容旧签名
      want_diag=True  → (picked, _capped_log, diag)               # diag 含 shortfall
    """
    cfg = cfg or _DEF_CFG
    lim = cfg.get('industry_limits', {})

    # ---- P0-1: 单票上限 → 最小持仓只数 ----
    # ⚠️ 口径裁决（round122，金融工程视角，**这是本轮最重要的设计决定**）：
    #   `max_single_stock` 该按「占股票仓位」还是「占总资产」解释，决定 min_holdings
    #   是 13 只还是 3 只，量级差 4 倍。两种都自洽，但对应不同风控哲学：
    #
    #   · scope="assets"（**默认**）：单票敞口占总资产 ≤ 8%
    #       min_holdings = ceil(w_total / 0.08)，w_total=0.2 时仅需 3 只。
    #       金融含义：8% 单票上限的本意是控制**组合层的单票风险敞口**，
    #       而敞口是相对总资产而言的。2 成仓时 6 只票各占 3.3% 总资产，
    #       单票暴雷对净值的最大伤害 = 3.3%，风控上完全达标。
    #       若强行要求 13 只，等于用「仓位轻」去换「持仓散」，反而稀释 alpha。
    #
    #   · scope="equity"（严格）：单票占**股票部分** ≤ 8%
    #       min_holdings = ceil(1 / 0.08) = 13 只，与 w_total 无关。
    #       金融含义：把股票仓位当成一个独立的钱袋子做内部等权分散。
    #       ⚠️ 实测今日 momentum 路线在此口径下**结构性不可行**：
    #       候选只覆盖 4 个申万一级行业，`per_parent=2` 封顶 8 只，
    #       即使把池扩到全市场 1000 只，可达上限仍是 7 只（已实测）。
    #       此时必须显式报 infeasible，而不是偷偷放宽行业约束。
    #
    #   裁决：默认 assets（有金融意义、可行），同时把 equity 口径**一并输出**，
    #   两者不一致时在 shortfall 里明确写「口径分歧」，让决策者看见而不是被隐藏。
    _scope = str(lim.get('max_single_stock_scope') or 'assets').lower()
    if _scope not in ('assets', 'equity'):
        _scope = 'assets'
    _mss = lim.get('max_single_stock')
    min_holdings = 1
    mss_note = None
    _mss_v = None
    if _mss:
        try:
            _mss_v = float(_mss)
            if not (0 < _mss_v <= 1):
                mss_note = f"max_single_stock={_mss_v} 超出 (0,1]，已忽略"
                _mss_v = None
        except (TypeError, ValueError):
            mss_note = f"max_single_stock={lim.get('max_single_stock')!r} 非数值，已忽略"
            _mss_v = None
    else:
        mss_note = "config 未配置 max_single_stock，单票上限不生效"
    if _mss_v:
        # 上限反推的基准权重：assets 口径看股票仓位占总资产，equity 口径恒为 1.0
        _base_w = float(w_total) if _scope == 'assets' else 1.0
        # 用整数算术做 ceil，避免 0.2/0.08 = 2.4999999999999996 之类的浮点误差
        min_holdings = max(1, int(_math.ceil(round(_base_w / _mss_v, 9))))
    target = max(int(top), min_holdings)

    # 等权假设下的「只数上限」换算: 以占比上限反推每板块/每行业允许的只数
    # ⚠️ 一律按 target 推导（见 docstring 语义裁决），不再按 top
    #
    # round122 观察（**只披露，不改参数**）：
    #   `--per-parent 2` 与 `max_l1_parent=0.35` 语义不同却用 min() 叠加，
    #   实际永远是 2 赢（target=10 时 0.35×10≈4 > 2）→ 百分比配置在当前
    #   target 下不构成约束。实测今日 candidate 池「电子」占 41.9%，按百分比
    #   允许 4 只，被 per_parent=2 卡到 2 只，这是 momentum 路线只能选 6 只的
    #   直接原因。**是否放宽属风控偏好裁决，不由代码擅自决定** —— 此处只在
    #   shortfall 里标出「绑定约束是谁」，把决策权留在配置里。
    eff_per_sector = per_sector
    eff_per_parent = per_parent
    _pct_per_parent = None
    if target > 0:
        if lim.get('max_l2_sector'):
            eff_per_sector = min(per_sector, max(2, int(target * float(lim['max_l2_sector']) + 0.999)))
        if lim.get('max_l1_parent'):
            _pct_per_parent = max(2, int(target * float(lim['max_l1_parent']) + 0.999))
            eff_per_parent = min(per_parent, _pct_per_parent)
    dev_pp = float(lim.get('max_l1_deviation_pp', 0) or 0)

    # P0-2: 五因拒绝账本（禁静默）
    rej = {'below_min_total': 0, 'per_l2': 0, 'per_l1': 0,
           'l1_deviation': 0, 'exhausted': 0}
    n_in = len(cands)
    cands = [r for r in cands if float(r.get('total') or 0) >= min_total]
    rej['below_min_total'] = n_in - len(cands)
    if mode == 'reversal':
        # 回测支持: 弱势板块内 RSI<30 优先
        cands.sort(key=lambda r: (0 if (r.get('tech') or {}).get('rsi') is not None and (r.get('tech') or {}).get('rsi') < 30 else 1,
                                  -float(r.get('total') or 0)))
    else:
        cands.sort(key=lambda r: -float(r.get('total') or 0))

    # v3.2 P0-2: 等权基准的申万一级行业占比（候选池内分布，代表「行业中性」参照）
    _par_total = len(cands) or 1
    _base_share = {}
    for r in cands:
        _p = r.get('sector_parent') or r.get('sector_l2') or '未分类'
        _base_share[_p] = _base_share.get(_p, 0) + 1.0
    _base_share = {k: v / _par_total for k, v in _base_share.items()}

    per_cnt, par_cnt, picked = {}, {}, []
    _capped_log = []          # 触顶替补记录（审计用）
    for r in cands:
        s = r.get('sector_l2') or '未分类'
        p = r.get('sector_parent') or s
        if per_cnt.get(s, 0) >= eff_per_sector:
            rej['per_l2'] += 1
            continue
        if eff_per_parent and par_cnt.get(p, 0) >= eff_per_parent:
            rej['per_l1'] += 1
            continue
        # P0-2: 行业暴露偏离检查（等权口径：加入后该行业占比 vs 基准占比）
        # ⚠️ round122 实测出的结构缺陷：偏离上限是**绝对 pp**，而持仓粒度是
        #   1/target 的**跳跃**。target=10 时买 1 只就占 10%，若某行业基准占比
        #   仅 0.9%，1 只的偏离 = 10% - 0.9% = 9.1pp > 8pp 阈值 → 该行业
        #   **连 1 只都永远买不了**（今日 12 个行业里 5 个被这样静默封死）。
        #   下限保护：只要还没到这个行业的上限，就允许买入第一只 —— 否则
        #   「行业中性」会退化成「只投高基准占比的大行业」，与初衷相反。
        #   偏离仅在**已持有 ≥1 只之后**才作为加仓的否决条件。
        if dev_pp > 0 and not no_l1_cap and target > 0:
            _would = (par_cnt.get(p, 0) + 1) / float(target)
            _has = par_cnt.get(p, 0)
            if _has > 0 and (_would - _base_share.get(p, 0)) * 100.0 > dev_pp:
                _capped_log.append({'name': r.get('name'), 'code': r.get('code'),
                                    'parent': p, 'reason': 'l1_deviation',
                                    'would_share': round(_would, 3),
                                    'base_share': round(_base_share.get(p, 0), 3)})
                rej['l1_deviation'] += 1
                continue
        per_cnt[s] = per_cnt.get(s, 0) + 1
        par_cnt[p] = par_cnt.get(p, 0) + 1
        picked.append(r)
        if len(picked) >= target:
            break

    if not want_diag:
        return picked, _capped_log

    n_pick = len(picked)
    shortfall = max(0, target - n_pick)
    # 候选是否已**全部用完**（选中 + 各闸拒绝 == 进池数）。旧判据写成 `< n_in`
    # 正好反了：momentum 侧 11 只候选、5 只被 per_l1 拦、6 只选中 = 全部耗尽，
    # 却被记成「还有剩余」→ 多报一条误导性的「候选耗尽 7 只」。
    _consumed = n_pick + rej['below_min_total'] + rej['per_l2'] + rej['per_l1'] + rej['l1_deviation']
    _all_used = _consumed >= n_in
    if shortfall and _all_used:
        rej['exhausted'] = shortfall
    reasons = []
    binding = None
    if shortfall:
        # 判定**绑定约束**：谁吃掉的候选最多，谁就是真正的瓶颈。
        # 这是金融工程里「约束归因」的最小实现 —— 只报「没选够」没有意义，
        # 必须回答「是哪条风控把它拦住的」。
        _led = {k: v for k, v in rej.items() if k != 'exhausted' and v}
        if _led:
            binding = max(_led, key=lambda k: _led[k])
        if rej['below_min_total'] and rej['below_min_total'] >= n_in * 0.5:
            reasons.append(f"评分门槛: {rej['below_min_total']} 只低于 min_total={min_total} 被剔除")
        if rej['per_l1']:
            reasons.append(f"申万一级上限: {rej['per_l1']} 只触顶 eff_per_parent={eff_per_parent}"
                           f"（候选集中于少数行业）")
        if rej['per_l2']:
            reasons.append(f"申万二级上限: {rej['per_l2']} 只触顶 eff_per_sector={eff_per_sector}")
        if rej['l1_deviation']:
            reasons.append(f"行业暴露偏离: {rej['l1_deviation']} 只超 max_l1_deviation_pp={dev_pp}")
        if rej['exhausted']:
            reasons.append(f"候选耗尽: 进池 {n_in} 只全部用完，最终可用 {n_pick} 只")
        # % 上限是否因 per_parent 硬顶而失效（只披露，不改）
        if _pct_per_parent and _pct_per_parent > eff_per_parent:
            reasons.append(
                f"⚠️ max_l1_parent={lim.get('max_l1_parent')} 换算为 {_pct_per_parent} 只，"
                f"但 --per-parent={per_parent} 更严（生效 {eff_per_parent} 只）→ "
                f"百分比配置在当前 target 下不构成约束")
    # 单票权重两种口径**都算**。合规判定按 `max_single_stock_scope` 选择，
    # 但两套数字都进 diag —— 免得「占总资产 3.3% 合规 / 占仓位 16.7% 超标」
    # 只露一半，让读者以为风控过了。
    _eq_of_equity = (1.0 / n_pick) if n_pick else None
    _eq_of_assets = (_eq_of_equity * float(w_total)) if _eq_of_equity else None
    _cap_satisfied = None
    _cap_scope_verdict = None
    if _mss_v:
        _chk = _eq_of_assets if _scope == 'assets' else _eq_of_equity
        _cap_satisfied = bool(_chk is not None and _chk <= _mss_v + 1e-9)
        # 另一口径单独裁决，两边不一致就是「口径分歧」，必须披露
        _other_scope = 'equity' if _scope == 'assets' else 'assets'
        _other = _eq_of_equity if _other_scope == 'equity' else _eq_of_assets
        _cap_scope_verdict = {
            'scope': _scope, 'checked': _chk, 'cap': _mss_v, 'pass': _cap_satisfied,
            'other_scope': _other_scope, 'other_value': _other,
            'other_pass': bool(_other is not None and _other <= _mss_v + 1e-9),
            'divergent': bool(_cap_satisfied is not None and _other is not None
                              and _cap_satisfied != (_other <= _mss_v + 1e-9)),
        }
    diag = {
        'target': target, 'top_nominal': int(top), 'min_holdings': min_holdings,
        'max_single_stock': _mss_v, 'max_single_stock_scope': _scope,
        'mss_note': mss_note,
        'eff_per_sector': eff_per_sector, 'eff_per_parent': eff_per_parent,
        'n_candidates_after_min_total': len(cands),
        'n_picks': n_pick, 'shortfall': shortfall,
        'shortfall_reasons': reasons,
        'binding_constraint': binding,
        'per_parent_pct_equivalent': _pct_per_parent,
        'reject_ledger': rej,
        # 占比口径（与 limit 三键一致）：单票占**股票仓位**
        'single_weight_max_of_equity': round(_eq_of_equity, 4) if _eq_of_equity else None,
        # 占总资产口径（w_total 来自 regime final_hi），供风险预算对齐
        'w_total': round(float(w_total), 4),
        'single_weight_max_of_assets': round(_eq_of_assets, 4) if _eq_of_assets else None,
        'cap_satisfied': _cap_satisfied,
        'cap_scope_verdict': _cap_scope_verdict,
    }
    return picked, _capped_log, diag


def fmt_pct(x):
    if x is None:
        return '—'
    return f"{x:+.2f}%"


def eff_per_sector_of(diag):
    return diag.get('eff_per_sector') if diag else '—'


def eff_per_parent_of(diag):
    return diag.get('eff_per_parent') if diag else '—'


def _base_share_of(cands, parent):
    """候选池内某申万一级行业的等权基准占比（P0-2 暴露偏离的参照）"""
    n = len(cands) or 1
    return sum(1 for r in cands
               if (r.get('sector_parent') or r.get('sector_l2') or '未分类') == parent) / float(n)


def _base_shares_of(cands):
    """候选池内**全部**申万一级行业的等权基准占比 {parent: share}。

    与 `build()` 内部的 `_base_share` 同口径（同一份候选、同一个分母），
    单独抽出来是因为双向偏离检查需要在 main() 里遍历全部行业。
    """
    n = len(cands) or 1
    acc = {}
    for r in cands:
        p = r.get('sector_parent') or r.get('sector_l2') or '未分类'
        acc[p] = acc.get(p, 0) + 1.0
    return {k: v / n for k, v in acc.items()}


def load_w_total(regime_path=None):
    """从 regime_forecast.json 读「股票仓位上限」作为 w_total。

    v4.18 P0-3（round122）：此前 `regime_model.risk_budget()` 算出 final_hi=0.2
    （今日只该 2 成仓），但 pick_portfolio **完全不读它**，输出里也没有 weights
    字段 —— 风险预算算给空气。此处把它接成真实权重来源。

    返回 (w_total, info)。w_total ∈ (0,1]；读不到则 1.0 并**显式标注降级**，
    绝不静默按满仓处理。
    """
    p = regime_path or os.path.join(HERE, 'regime_forecast.json')
    info = {'source': None, 'available': False, 'date': None,
            'gate_ok': None, 'final_hi': None, 'final_lo': None,
            'bound_by': None, 'fallback': False, 'note': None}
    if not os.path.exists(p):
        info['note'] = f"未找到 {os.path.basename(p)}，w_total 降级为满仓 1.0"
        info['fallback'] = True
        return 1.0, info
    try:
        # ⚠️ 必须用 with 关句柄：裸 `json.load(open(...))` 在 CPython 里靠
        #   引用计数立即回收，看起来「没事」，但一旦抛异常或换实现就会泄漏。
        #   round122 单测（ResourceWarning）暴露了这一点。
        with open(p, encoding='utf-8') as _f:
            d = json.load(_f)
    except Exception as e:
        info['note'] = f"regime_forecast 解析失败({str(e)[:40]})，w_total 降级为满仓 1.0"
        info['fallback'] = True
        return 1.0, info
    info['source'] = os.path.basename(p)
    info['date'] = d.get('date') or d.get('data_date')
    gate = d.get('gate') or {}
    info['gate_ok'] = gate.get('ok')
    cb = d.get('combine') or {}
    info['final_hi'] = cb.get('final_hi')
    info['final_lo'] = cb.get('final_lo')
    info['bound_by'] = cb.get('bound_by')
    info['available'] = bool(cb.get('available'))
    # 🔒 日期门禁：指数最新交易日必须等于数据日，否则仓位建议不可用（沿用 v1 铁律）
    if gate and gate.get('ok') is False:
        info['note'] = f"风险预算日期门禁未通过（{gate.get('detail')}），w_total 降级为满仓 1.0"
        info['fallback'] = True
        return 1.0, info
    _fh = cb.get('final_hi')
    if _fh is None:
        info['note'] = "combine.final_hi 缺失，w_total 降级为满仓 1.0"
        info['fallback'] = True
        return 1.0, info
    try:
        w = float(_fh)
    except (TypeError, ValueError):
        info['note'] = f"combine.final_hi={_fh!r} 非数值，w_total 降级为满仓 1.0"
        info['fallback'] = True
        return 1.0, info
    if not (0.0 < w <= 1.0):
        info['note'] = f"combine.final_hi={w} 超出 (0,1]，w_total 降级为满仓 1.0"
        info['fallback'] = True
        return 1.0, info
    return w, info


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('scored', nargs='?', default=None)
    ap.add_argument('--top', type=int, default=10)
    ap.add_argument('--per-sector', type=int, default=3)
    ap.add_argument('--per-parent', type=int, default=2, help='同一申万一级行业最多N只(防行业集中)')
    ap.add_argument('--min-total', type=float, default=12.0, help='最低综合评分(默认12, 只推B级及以上)')
    ap.add_argument('--no-route-pool', action='store_true',
                    help='关闭路线差异化池(两路线都用全池), 用于对照')
    ap.add_argument('--no-eligibility', action='store_true',
                    help='关闭资格过滤(次新/停牌/流动性不达标不剔除), 用于对照')
    ap.add_argument('--no-l1-cap', action='store_true',
                    help='关闭申万一级行业暴露偏离上限, 用于对照')
    ap.add_argument('--no-reach-check', action='store_true',
                    help='回滚开关: 板块层级仍按「候选数」判停止(旧行为), 用于对照')
    ap.add_argument('--no-risk', action='store_true',
                    help='不读 regime 风险预算, w_total 强制 1.0(满仓), 用于对照')
    ap.add_argument('--out', default=None)
    args = ap.parse_args()

    cfg = load_config()
    route_pool = cfg.get('route_pool_map', {})
    mom_w = float(cfg.get('momentum_route_weight', 0.5) or 0.5)

    # P0-3: 接入 regime 风险预算作为真实仓位（--no-risk 强制满仓，用于对照）
    if args.no_risk:
        w_total, rinfo = 1.0, {'source': None, 'available': False, 'fallback': True,
                               'note': '--no-risk 指定满仓 1.0（对照模式）'}
    else:
        w_total, rinfo = load_w_total()

    scored = args.scored
    if scored is None:
        fs = [f for f in sorted(glob.glob(os.path.join(HERE, 'top200_scored_*.json')), reverse=True)
              if '_watch_' not in f and '.bak' not in f]
        scored = fs[0] if fs else None
    if not scored or not os.path.exists(scored):
        print("❌ 未找到 scored json")
        return 1
    mom = json.load(open(MOM_PATH, encoding='utf-8'))
    data = json.load(open(scored, encoding='utf-8'))
    results = data.get('results', [])
    rows = mom['sectors']
    mdate = mom.get('date')
    sdate = data.get('date')
    n = len(rows)

    # ---- 路线差异化池：按成交额(turnover)排名切分 ----
    # 依据: TOP200 vs TOP500 对照回测 —— B 反转路线在 TOP500 全胜(选股超额 +6.3pp),
    #       A 动量路线在 TOP500 全败(反转市)。故 reversal 用更大池(默认500),
    #       momentum 维持小池(默认200), 兼顾证据与集中度。
    ranked = sorted(results, key=lambda r: -float(r.get('turnover') or 0))
    for i, r in enumerate(ranked, 1):
        r['_amt_rank'] = i
    pool_stat = {}
    for mode in ('momentum', 'reversal'):
        ps = int(route_pool.get(mode, len(ranked)) or len(ranked))
        pool_stat[mode] = min(ps, len(ranked))
    print(f"scored 日期 {sdate} | 板块动量日期 {mdate} | 板块数 {n} | 全池 {len(results)} 只")
    if not args.no_route_pool and len(ranked) > min(pool_stat.values()):
        print(f"  路线差异化池: momentum→前{pool_stat['momentum']} / reversal→前{pool_stat['reversal']}"
              f"（动量路线降权系数 {mom_w}）")

    out = {'date': sdate, 'momentum_date': mdate, 'top': args.top, 'per_sector': args.per_sector,
           'per_parent': args.per_parent,
           'route_pool_map': ({'momentum': pool_stat['momentum'], 'reversal': pool_stat['reversal']}
                              if not args.no_route_pool else {'momentum': len(ranked), 'reversal': len(ranked)}),
           'momentum_route_weight': mom_w,
           'portfolios': {}}
    min_cands = max(args.top, 10)   # 候选不足 top 数量时继续放宽板块层级 → 组合更分散
    elig_stats = {}
    for mode in ('momentum', 'reversal'):
        # 路线池过滤（--no-route-pool 或池本身就好时用全池）
        if args.no_route_pool:
            route_results = results
        else:
            cap = pool_stat[mode]
            route_results = [r for r in results if r.get('_amt_rank', 1) <= cap]
        # v3.2 P0-1/P0-3: 资格过滤（次新/停牌/流动性不达标剔除，--no-eligibility 关闭）
        _before = len(route_results)
        if not args.no_eligibility:
            route_results = [r for r in route_results
                             if not r.get('is_new') and not r.get('suspended')
                             and r.get('liquid_ok', True)]
            elig_stats[mode] = {'before': _before, 'after': len(route_results),
                                'dropped': _before - len(route_results)}
        # v4.11（2026-10-26）：追高拦截迁移到此层。
        #   fetch 原先把「当日涨幅前 20%」直接从池里删掉，与老大「初筛池每只都要评分」
        #   冲突（被删的 380 只永远拿不到分数，页面无法解释「它几分、为何不能买」）。
        #   现在 fetch 只打 chase_hi 标记不删，拦截责任下移到**选组合**这一层——
        #   语义更准（评分是度量、追高是交易约束），且对选股结果**完全等价**：
        #   删除集合相同，只是删除发生的位置变了。
        #   ⚠️ 若移除本段，等于废除已回测有效的追高规则（配对 t+2.66），务必保留。
        if not args.no_eligibility:
            _n_chase = sum(1 for r in route_results if r.get('chase_hi'))
            if _n_chase:
                route_results = [r for r in route_results if not r.get('chase_hi')]
                _es = elig_stats.setdefault(mode, {'before': _before, 'after': 0, 'dropped': 0})
                _es['chase_dropped'] = _n_chase
                _es['after'] = len(route_results)
                _es['dropped'] = _es.get('dropped', 0) + _n_chase
        levels = pick_sectors(rows, mode)

        # ---- P0-2: 搜索停止条件改为「组合约束下的可达只数」 ----
        # 用 build() 自身 dry-run 当 reach_fn，保证「判据」与「实际执行」同一套代码，
        # 不会出现 select 认为够 10 只、build 实际只给 2 只的口径分裂。
        def _reach(_c):
            _p, _, _d = build(_c, mode, args.per_sector, args.per_parent, args.top,
                              args.min_total, cfg, no_l1_cap=args.no_l1_cap,
                              w_total=w_total, want_diag=True)
            return _d['n_picks']

        secs, level, cands, trace = select(rows, route_results, mode, min_cands,
                                           reach_fn=None if args.no_reach_check else _reach)
        picked, capped_log, bdiag = build(cands, mode, args.per_sector, args.per_parent,
                                          args.top, args.min_total, cfg,
                                          no_l1_cap=args.no_l1_cap,
                                          w_total=w_total, want_diag=True)
        # v3.2 P0-2: 行业暴露表（等权口径，已选权重 vs 候选池基准）
        _par_share = {}
        for r in picked:
            _p = r.get('sector_parent') or r.get('sector_l2') or '未分类'
            _par_share[_p] = _par_share.get(_p, 0) + 1
        _n_pick = len(picked) or 1
        _exposure = [{'parent': k, 'n': v, 'share': round(v / _n_pick, 3),
                      'base_share': round(_base_share_of(cands, k), 3),
                      'dev_pp': round((v / _n_pick - _base_share_of(cands, k)) * 100, 1)}
                     for k, v in sorted(_par_share.items(), key=lambda x: -x[1])]
        # v4.18 P1（round122）：暴露偏离改**双向**，且**覆盖全部行业**。
        #   旧版两个缺陷：
        #     ① 只查「超配」（build() 里 `_would - base > dev_pp` 才否决），
        #        行业低配（基准 20% 实配 0）**永不触发任何提示** → 行业中性只做了一半；
        #     ② `_under` 虽名叫 under，却只收录「一股未配」的行业，
        #        已配 1 只但基准 30% 的行业（实配 10%，低配 20pp）反而被漏掉。
        #   金融工程上「欠配」与「超配」是同一枚硬币的两面：欠配意味着主动
        #   放弃了该行业的 beta（若该行业恰好是当期强势行业，就是**机会成本**），
        #   超配意味着承担了额外集中风险。两者都必须可见，读者才能判断这是
        #   「有意的行业选择」还是「约束失效的副产品」。
        _thr = 0.0
        if not args.no_l1_cap:
            _thr = float((cfg.get('industry_limits') or {}).get('max_l1_deviation_pp', 0) or 0)
        _base_all = _base_shares_of(cands)
        _over, _under = [], []
        if _thr > 0:
            for _p, _bs in _base_all.items():
                _n_here = _par_share.get(_p, 0)
                _sh_here = _n_here / _n_pick
                _d = (_sh_here - _bs) * 100.0
                if _d > _thr:
                    _over.append({'parent': _p, 'n': _n_here, 'share': round(_sh_here, 3),
                                  'base_share': round(_bs, 3), 'dev_pp': round(_d, 1),
                                  # 相对偏离：绝对 pp 阈值对小基准行业不公平（基准 2%、
                                  # 实配 12% 时相对偏离 6 倍，绝对只有 10pp）。两种口径
                                  # 都给出，判定仍按配置的绝对 pp（不改风控语义）。
                                  'dev_rel': round((_sh_here / _bs - 1.0), 2) if _bs > 0 else None})
                elif _d < -_thr:
                    _under.append({'parent': _p, 'n': _n_here, 'share': round(_sh_here, 3),
                                   'base_share': round(_bs, 3), 'dev_pp': round(_d, 1),
                                   'zero_weight': _n_here == 0,
                                   'dev_rel': round((_sh_here / _bs - 1.0), 2) if _bs > 0 else None})
            _over.sort(key=lambda x: -x['dev_pp'])
            _under.sort(key=lambda x: x['dev_pp'])
        _l1_balance = {
            'threshold_pp': _thr,
            'n_over': len(_over), 'n_under': len(_under),
            'n_under_zero_weight': sum(1 for u in _under if u['zero_weight']),
            # 欠配行业的**基准占比合计**（占候选池）。注意这不是「被完全放弃的
            # 池子」——欠配含两种情形：一股未配（zero_weight）与配了但远低于基准。
            # 这个数越大，说明组合与「池等权」这条基准的偏离越系统性，读者据此
            # 判断「超额是选股带来的，还是行业权重错配带来的」。
            'under_base_share_sum': round(sum(u['base_share'] for u in _under), 3),
            # 超配行业的基准占比合计，与上者配对看：两边同时很大 = 组合在
            # 行业层面做了显著的方向性押注（不是行业中性）。
            'over_base_share_sum': round(sum(o['base_share'] for o in _over), 3),
            'note': ('偏离仅在 build() 中作为**加仓**否决条件，第一只永远允许'
                     '（否则绝对 pp 阈值会让小基准行业永久不可投）'
                     if _thr > 0 else 'max_l1_deviation_pp 未配置或已关闭 → 行业中性不生效'),
        }

        # P0-3: 等权真实权重（含现金）。w_total 来自 regime 风险预算。
        #   ⚠️ 两个口径必须分开命名，否则「3.3% vs 上限 8%」看着合规、
        #   实际股票仓位里单票已 16.7%（momentum 6 只今天正是如此）。
        _eq_w = (float(w_total) / _n_pick) if _n_pick else 0.0     # 占总资产
        _eq_of_equity = (1.0 / _n_pick) if _n_pick else None        # 占股票仓位
        _weights = {r['code']: round(_eq_w, 4) for r in picked}
        # 现金必须用**舍入后**的权重反推，否则 6×0.0333=0.1998 与 cash=0.8000
        # 之和 = 0.9998，页面上资金分配表会对不上账。
        _cash = round(1.0 - sum(_weights.values()), 4) if _weights else 1.0
        # HHI / 有效只数必须在**股票仓位内部**归一化（等权下 = 1/n）。
        # ⚠️ 直接对「占总资产权重」求 HHI 会得到 1/(n×(w/n)²) = n/w² ≫ n，
        #    momentum 6 只曾因此报出 effective_n=150.3，纯属口径错误。
        _hhi = (sum((1.0 / _n_pick) ** 2 for _ in picked)) if _n_pick else None
        _risk = {
            'w_total': round(float(w_total), 4),
            'w_total_source': rinfo,
            # 两个口径都输出；合规判定按 configured scope
            'single_weight_max_of_equity': round(_eq_of_equity, 4) if _eq_of_equity else None,
            'single_weight_max_of_assets': round(_eq_w, 4),
            'single_weight_cap': bdiag.get('max_single_stock'),
            'single_weight_cap_scope': bdiag.get('max_single_stock_scope'),
            'cap_satisfied': bdiag.get('cap_satisfied'),
            'cap_scope_verdict': bdiag.get('cap_scope_verdict'),
            'n_picks': _n_pick,
            'target': bdiag.get('target'),
            'cash_weight': _cash,
            # HHI / 有效只数：**股票仓位内部**口径（等权 = 1/n，二者互逆）
            'hhi': round(_hhi, 4) if _hhi else None,
            'effective_n': round(1.0 / _hhi, 2) if _hhi else None,
            # ⚠️ 波动/beta/MDD 需 K 线库，由 portfolio_risk.py 另算并回填
            'vol_ann': None, 'beta_vs_pool': None, 'mdd': None,
            'risk_metrics_note': '组合波动/beta/回撤由 portfolio_risk.py 单独计算，此处留空占位',
        }
        out['portfolios'][mode] = {
            'level_used': level, 'target_rule': levels[0][0], 'level_trace': trace,
            'sectors': sorted(secs), 'n_candidates': len(cands),
            # P0-3: 候选池代码名单 —— 供 portfolio_risk.py 算 beta 的 POOL_EW 基准。
            #   此前组合 json 只存入选股，下游想算「相对同池等权的 beta」无从下手，
            #   只能退化成「持仓自身等权」→ beta 恒等于 w_total，纯数学巧合。
            'candidate_codes': [r.get('code') for r in cands if r.get('code')],
            'pool_used': len(route_results),
            'eligibility': elig_stats.get(mode),
            'exposure': _exposure,
            'exposure_over': _over,
            'exposure_under': _under,
            'l1_balance': _l1_balance,
            'l1_capped': capped_log,
            'route_weight': (mom_w if mode == 'momentum' else 1.0),
            'weights': _weights,
            'risk': _risk,
            'shortfall': {'target': bdiag.get('target'),
                          'top_nominal': bdiag.get('top_nominal'),
                          'min_holdings': bdiag.get('min_holdings'),
                          'n_picks': _n_pick,
                          'gap': bdiag.get('shortfall'),
                          'reasons': bdiag.get('shortfall_reasons') or [],
                          'binding_constraint': bdiag.get('binding_constraint'),
                          'reject_ledger': bdiag.get('reject_ledger'),
                          'eff_per_sector': bdiag.get('eff_per_sector'),
                          'eff_per_parent': bdiag.get('eff_per_parent'),
                          'per_parent_pct_equivalent': bdiag.get('per_parent_pct_equivalent')},
            'picks': [{'name': r['name'], 'code': r['code'], 'sector_l2': r.get('sector_l2'),
                       'parent': r.get('sector_parent', ''), 'total': r['total'], 'rating': r['rating'],
                       'adj_total': round(float(r['total']) * (mom_w if mode == 'momentum' else 1.0), 2),
                       'amt_rank': r.get('_amt_rank'),
                       'price': r.get('price'), 'pct_chg': r.get('pct_chg'),
                       'turnover_ratio': r.get('turnover_ratio'), 'circ_mv': r.get('circ_mv'),
                       'rsi': (r.get('tech') or {}).get('rsi'), 'trend': (r.get('tech') or {}).get('trend'),
                       'sector_r60': r.get('sector_r60'), 'sector_r10': r.get('sector_r10'),
                       'sector_mom60': r.get('sector_mom60'), 'sector_mom10': r.get('sector_mom10'),
                       'score_sector': r.get('score_sector'),
                       'score_news': r.get('score_news'), 'score_tech': r.get('score_tech'),
                       'score_fund': r.get('score_fund'), 'score_theme': r.get('score_theme')}
                      for r in picked],
        }
        print(f"\n=== {mode} 路线 | {level} | 路线池 {len(route_results)} 只 ===")
        if elig_stats.get(mode):
            _e = elig_stats[mode]
            print(f"   资格过滤: {_e['before']} → {_e['after']} (剔除次新/停牌/流动性不达标 {_e['dropped']} 只)")
        for t in trace:
            _r = t.get('n_reachable')
            print(f"   [{t['level']:<34}] 板块{t['n_sectors']:>3} 候选{t['n_cands']:>3}"
                  + (f" 可达{_r:>3}" if _r is not None else ""))
        print(f"  选中板块({len(secs)}): {', '.join(sorted(secs)[:12])}{'...' if len(secs) > 12 else ''}  候选 {len(cands)} 只")
        if capped_log:
            print(f"  一级行业暴露触顶替补 {len(capped_log)} 只: "
                  f"{', '.join(str(c['name']) for c in capped_log[:5])}{'...' if len(capped_log) > 5 else ''}")
        print(f"  行业暴露: " + " | ".join(
            f"{e['parent']} {e['n']}只({e['share']:.0%}, 偏离{e['dev_pp']:+.1f}pp)" for e in _exposure[:6]))
        if _over:
            print(f"  🔺 行业**超配**超阈值 {len(_over)} 个: " + " | ".join(
                f"{o['parent']} {o['n']}只(基准{o['base_share']:.0%}, 偏离{o['dev_pp']:+.1f}pp)"
                for o in _over[:5]))
        if _under:
            print(f"  🔻 行业**低配**超阈值 {len(_under)} 个"
                  f"（其中 {_l1_balance['n_under_zero_weight']} 个一股未配；"
                  f"低配行业占候选池 {_l1_balance['under_base_share_sum']:.0%}）: " + " | ".join(
                f"{u['parent']} {u['n']}只(基准{u['base_share']:.0%}, 偏离{u['dev_pp']:+.1f}pp)"
                for u in _under[:5]))
        if not _over and not _under:
            print(f"  ✅ 行业双向偏离均在 ±{_l1_balance['threshold_pp']:.0f}pp 内")
        elif _thr <= 0:
            print("  ⚠️ 行业中性未生效（max_l1_deviation_pp 未配置或 --no-l1-cap）")
        # ---- P0-1/P0-2 约束达成状态（禁静默）----
        _sp = bdiag['shortfall']
        print(f"  约束: 名义 top={bdiag['top_nominal']} · 单票上限反推 min_holdings={bdiag['min_holdings']}"
              f"(max_single_stock={bdiag['max_single_stock']}) → 目标 {bdiag['target']} 只 · 实选 {_n_pick} 只")
        print(f"        行业只数上限: 二级{eff_per_sector_of(bdiag)} / 一级{eff_per_parent_of(bdiag)}")
        if _sp:
            print(f"  🔴 未达目标 {_sp} 只 —— 原因: " + ("; ".join(bdiag['shortfall_reasons'])
                                                    if bdiag['shortfall_reasons'] else "未分类"))
            print(f"     拒绝账本: {bdiag['reject_ledger']}")
        _sw_eq = _risk['single_weight_max_of_equity']
        _sw_as = _risk['single_weight_max_of_assets']
        _cap = _risk['single_weight_cap']
        _sc = _risk['single_weight_cap_scope'] or 'assets'
        if _cap:
            _ok = _risk['cap_satisfied']
            print(f"  单票权重: 占总资产 {_sw_as:.1%} / 占股票仓位 {_sw_eq:.1%}"
                  f"（上限 {_cap:.1%}，按 {_sc} 口径判 → {'✅ 合规' if _ok else '🔴 超限'}）")
            _bv = _risk.get('cap_scope_verdict') or {}
            if _bv.get('divergent'):
                print(f"     ⚠️ 口径分歧: {_bv['scope']}口径{'过' if _bv['pass'] else '不过'}"
                      f"({_bv['checked']:.1%}) 但 {_bv['other_scope']}口径"
                      f"{'过' if _bv['other_pass'] else '不过'}({_bv['other_value']:.1%})"
                      f" —— 两口径不同源，决策者须自行裁决")
        else:
            print(f"  ⚠️ 单票上限未生效（{bdiag['mss_note']}），当前单票占总资产 {_sw_as:.1%}")
        print(f"  仓位: w_total={float(w_total):.0%}"
              f"（{'regime 风险预算' if not rinfo.get('fallback') else '降级: ' + str(rinfo.get('note'))}）"
              f" 现金 {_cash:.1%}")
        # 单票权重栏（等权，但显式把百分比打出来，避免"6只"看不出单票16.7%）
        for i, r in enumerate(picked, 1):
            t = r.get('tech') or {}
            print(f"  {i:>2}. {r['name']:<6}{r['code']} {str(r.get('sector_l2')):<8} "
                  f"总分{r['total']:>5.1f}({r['rating']}) RSI{t.get('rsi') if t.get('rsi') is not None else 0:>5.1f} "
                  f"{str(t.get('trend')):<6} 板块R60={r.get('sector_r60')} R10={r.get('sector_r10')} "
                  f"权重{_eq_w:.1%}")

    op = args.out or os.path.join(SECTOR_DIR, f"portfolio_{sdate}.json")
    with open(op, 'w', encoding='utf-8') as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print(f"\n✅ 组合已写入 {op}")
    return 0


if __name__ == '__main__':
    sys.exit(main())
