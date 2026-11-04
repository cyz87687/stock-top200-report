#!/usr/bin/env python3
"""ep_size_ind.py —— EP（估值）+ SIZE_IND_NEUT（行业内中性化市值）两维打分
（v4.9 新增，单一实现来源）。

背景（老大 2026-10-26 指令）：
  参考飞书《完整的量化选股评分模型》改造现模型，要求「先看现有指标能否复用」。
  round26 按飞书口径在本池 78 截面复测 8 因子，结论（见 reports/feishu_model_2026-10-26.md）：
    · 8 个因子全部可用现有数据算出，零新增数据源；
    · 只有 EP（本池 h5 IC +0.0986, t0=2.86）与 SIZE_IND_NEUT（行业内中性化，
      h20 IC +0.0883, t3.03）值得吸收；
    · 飞书 5 大类框架整体远弱于现行（h5 +0.0551 vs +0.1437），故**不照搬框架**，
      只做两处单维替换。

老大裁决（round27c 实跑后两轮确认）：
  ① EP 与 fund **各半**：score_fund 0.15 → 0.075，新增 score_ep 0.075
     （EP 全量替换 fund 在 h20 变差 −0.0033，2/3 档；各半方案 3/3 档全改善）
  ② **SIZE_IND_NEUT 替换 size**：score_size 0.10 → 0，新增 score_size_ind 0.10
     （round27b Q2b 决定性对照：全市场 LN市值取负 h5 −0.0638/t−2.71；
      行业内 Z 取负 h5 −0.0148、h20 +0.0883/t3.03 → 行业内改造确实有效）

⚠️ 本模块是审计（audit_round26/27/27b/27c）与生产（step2）的**唯一实现来源**。
   改这里必须同步重跑 audit_round27c.py 验证 IC 不漂移——round17 的教训是
   「同一逻辑两处实现必然漂移，且不报错」。

口径（与 audit_round26.build_factors / group_winsorize_zneg 逐字一致，勿单方面修改）：
  · EP = 扣非净利润TTM / 总市值；总股本 = 归母净利润 / 基本每股收益（同报告期，量纲自洽）
  · SIZE_IND_NEUT 四步（飞书原文）：申万一级分组 → 组内 LN(流通市值) winsorize 0.1%/99.9%
    → 组内 Z-score → 取负；**不再做二次截面标准化**（飞书明确）
  · 两者最后都转成池内 rank 分位 ×10 → 0-10 分制（与 score_size/quality 同口径）
  · 缺维从分母剔除（v4.3 铁律），禁 `or 0`

⚠️ 三个已踩过的坑（改代码前必读）：
  1. **不能复用 fund_split.load_em_cache()**：它的指标白名单只有 17 项，会把
     「基本每股收益」「归母净利润」直接过滤 → EP 覆盖率实测 0.0%。
     故本模块自带 load_em_all()，加载 EM 缓存里全部 70 个指标。
  2. **EM 原始数据含字符串 'nan'**（745/818 只的「每股息税前利润」含 nan）：
     float('nan') 能通过 isinstance 检查、污染截面均值/方差 → 整列 Z 变 nan；
     且 NaN 比较恒 False 会静默串位。必须在入口显式剔除。
  3. **min_grp 不能设 10**：本池 29 个申万一级行业里大量只有 3~8 只，
     min_grp=10 时覆盖率实测只有 7.6%；设 5 后 71.3%。round27b Q1 已验证
     min_grp 3~10 结果完全相同（本池行业组大小集中在 1~4 只或 ≥11 只，中间空档），
     15 时降到 59.1% → 参数不敏感，取 5。
"""
import glob
import json
import math
import os
import time
from collections import defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
EM_CACHE = os.path.join(HERE, "data", "fin_cache_em")
# v4.11：EP/SIZE_IND 专用 pkl（与 fund_split.EM_PKL 分开建——两者加载的指标集不同：
#   fund_split 只留 17 项白名单，本模块要全部 70 项，共用一份会互相污染）
EM_ALL_PKL = os.path.join(EM_CACHE, "_all_cache.pkl")

# 同名指标在多个「选项」分组下出现时的取值优先级（与 audit_round26.load_em_all 一致）
_OPT_PRI = {"常用指标": 0, "每股指标": 1, "成长能力": 2, "盈利能力": 3, "财务风险": 4}

MIN_GRP = 5          # SIZE_IND_NEUT 行业内最小组样本数（见文件头坑 3）

# ---------------------------------------------------------------- 成长条件交互（v4.12）
# 老大 2026-10-28 指令：小市值溢价必须有成长门槛，低成长小票不享受市值溢价。
# 三步：① 行业内市值原始分 score_mc_raw（越小越高）
#       ② 按成长综合分 G 分三档给动态权重 w_mc
#       ③ 流动性不足再打对折
# 目的：规避「单纯小市值 → 低质量小票暴露」，让市值红利只奖励成长已验证的标的。
G_HIGH = 7.0         # 高成长阈值：G > 7.0 → 充分释放小市值溢价
G_LOW = 4.0          # 低成长阈值：G <= 4.0 → 关闭小市值溢价
W_HIGH = 0.80        # 高成长档权重
W_MID = 0.30         # 中性成长档权重
W_LOW = 0.00         # 低成长档权重
LIQ_THRESHOLD = 8e7  # 近20日日均成交额阈值（0.8 亿）→ 不足则溢价减半
LIQ_PENALTY = 0.5    # 流动性惩罚系数

# ---------------------------------------------------------------- 方案3 双因子拆分（2026-10-28 老大指令）
# 将单一 score_size_ind 拆成两个独立打分维度，各自加权进入总分：
#   ① score_size_growth（小盘成长溢价）：= 原 v4.12 score_size_ind（行业内市值分 × 成长门限 × 流动性惩罚）
#      额外加一道「小票门槛」mc_pct ≤ MC_SMALL_CAP（个股市值占申万二级行业比 ≤ 30%）→ 仅真小票享受溢价；
#      中大盘（mc_pct > MC_SMALL_CAP）走模块B。
#   ② score_dominant（龙头壁垒溢价）：基于业务份额（rev_share / profit_share = 个股营收/扣非净利 ÷ 申万二级
#      行业本池合计）组内 rank 合成龙头分；再做「市值一致性校验」与流动性惩罚。
# ⚠️ 以下两处为本次重构的**解读性参数**，已在最终汇报中明确标注待老大确认：
#   · MC_SMALL_CAP：模块A 小票门槛（明确，按 spec）。
#   · DOM_MC_CONSIST / DOM_DISCOUNT：模块B「市值校验 mc_pct<0.8 打折」的语义解读——
#      我的实现：业务份额未被市值充分确认（mc_pct < 0.8 × max(rev_share, profit_share)）则龙头分打折。
#      （spec 原文「mc_pct<0.8 打折」未明确定义 mc_pct 的比较基准，故此为我的可辩护重构，待裁。）
MC_SMALL_CAP = 0.30   # 模块A 小票门槛：个股市值占申万二级行业比 ≤ 0.30 → 走小盘成长溢价
DOM_REV_W = 0.5       # 模块B 业务份额合成：营收份额权重
DOM_PROFIT_W = 0.5    # 模块B 业务份额合成：利润份额权重
DOM_MC_CONSIST = 0.80 # 模块B 市值一致性：mc_pct < 0.80 × 业务份额 → 龙头分打折（解读参数，待确认）
DOM_DISCOUNT = 0.70   # 市值一致性不达标时的龙头分折扣
DOM_NEUTRAL = 5.0     # 申万二级行业组内 <2 只（无法 rank）时给中性分，避免微行业误评
DOM_LEADER_THR = 7.0  # 模块B 龙头门槛：业务份额合成 rank ≥ 7（组内前 30%）即为行业龙头
                      # ⚠️ 老大 2026-10-28 裁决：原文 mc_pct>0.3 门槛在真实分布下仅覆盖 4.1%（77只）形同虚设，
                      #   改用「业务份额龙头」分区（两维 Spearman 仅 0.33，真正互补）。mc_pct 仅保留为留痕/一致性校验。
                      # ⚠️ P0-3（round118）：本阈值**不再决定 dominant 是否落分**（改为全池落分），
                      #   只用于「留痕标注谁是龙头」与 n_dom_leader 计数。分区仍决定 size_growth 给谁。

# ---------------------------------------------------------------- P0-3 全池打分（round118）
# reports/round118_project_assessment_2026-11-03.md §1.3 把 dominant 覆盖率 26.6% 记为缺陷。
# 实测成因不是数据缺失（申万二级映射 100%、EM 财务 100%、营收+利润TTM齐全 99.7%），
# 而是上面这行阈值造成的**互斥分区**：只有组内前 30% 写 dominant，其余写 size_growth。
# 故「扩到全池」= 拆分区，是结构变更。裁决前先实测（audit_round118e/118f）：
#   覆盖率  26.6% → 97.2%
#   单维 IC  h1 +0.0218(t1.03)→+0.0426(t3.15) / h5 +0.0203(t0.73)→+0.0610(t1.83)
#            / h20 +0.0525(t1.33)→+0.0311(t0.52)  ← **h20 单维反而变弱**
#   复合 IC  h1 +0.0204→+0.0458 / h5 +0.0292→+0.0748 / h20 +0.0679→+0.0779  ← 三周期全升
# 复合升而 h20 单维降，说明升来自「其余 8 维在更多行上有 dominant 可参与归一化」，
# 不是「dominant 在 h20 变强」。**这两句话必须分开写**，混起来就成了虚假背书。
# 与 size_growth 相关仅 −0.02~+0.05 → 不构成对冲（这是扩张前最担心的事）。
# ⚠️ 未过 Bonferroni：h20 的 NW t=1.493、n=66/lag=19，该 (n,lag) 的真实 5% 临界值
#    在 3.6 以上（记忆第 ⑲ 条，禁套 2.0）。故本条**不得表述为「经验证的 alpha」**。


def growth_weight(g):
    """成长综合分 G（0-10）→ 市值溢价动态权重 w_mc ∈ {0.0, 0.3, 0.8}。

    G 缺失（None）时返回 0.0 —— **不猜**。缺成长数据就拿不到市值溢价，
    这是保守方向（宁可少加分，不凭空送分）。
    """
    if not (isinstance(g, (int, float)) and not isinstance(g, bool) and g == g):
        return 0.0
    if g > G_HIGH:
        return W_HIGH
    if g > G_LOW:
        return W_MID
    return W_LOW


def interact_size_growth(score_mc_raw, g, avg_amt, liq_threshold=LIQ_THRESHOLD):
    """市值原始分 × 成长门限权重 × 流动性惩罚 → 最终 0-10。

    score_mc_raw: 行业内市值分（0-10，市值越小越高）
    g:            成长综合分（0-10）；None → 不给溢价
    avg_amt:      近20日日均成交额（元）；None → 视为「无法判定流动性」，
                  **不惩罚**（缺数据不等于低流动性，禁 or 0 铁律）
    """
    if not (isinstance(score_mc_raw, (int, float)) and not isinstance(score_mc_raw, bool)
            and score_mc_raw == score_mc_raw):
        return None
    s = float(score_mc_raw) * growth_weight(g)
    if avg_amt is not None and isinstance(avg_amt, (int, float)) and avg_amt == avg_amt:
        if avg_amt < liq_threshold:
            s *= LIQ_PENALTY
    return max(0.0, min(10.0, s))


def group_winsorize_zneg_l1(pairs, min_grp=MIN_GRP):
    """同 group_winsorize_zneg，但按**申万一级**行业分组（老大 2026-10-28 指定）。

    pairs: [(code, industry_l1, circ_mv)] → ({code: -z}, {被丢弃行业: 组大小})
    """
    return group_winsorize_zneg(pairs, min_grp=min_grp)


# ============================================================ EM 财务（PIT）

def _fnum(v):
    """EM 原始值 → float 或 None。NaN/inf/占位符一律 None（坑 2）。"""
    if v is None or v is False:
        return None
    if isinstance(v, str) and v.strip() in ("--", "-", "", "False", "nan", "None"):
        return None
    try:
        f = float(v)
    except Exception:
        return None
    if f != f or f in (float("inf"), float("-inf")):
        return None
    return f


def load_em_all(cache_dir=None, use_pkl=True):
    """加载 EM 缓存**全部**指标 → {code6: {indicator: {period: value}}}。

    ⚠️ 不能复用 fund_split.load_em_cache()（白名单只有 17 项）——见文件头坑 1。

    v4.11（2026-10-26 性能改造，老大需求①「缓存机制」）：
      原实现每次全量遍历 1348 个 JSON（约 127MB）逐文件 json.load + 解析，
      实测 **46.4s**（500 只池时该项占 step2 总时长 5.6%），是继三处串行网络段
      之后的第四大头；池放开到 1518 只后仍为固定成本，白白重复烧。
      财报数据一天最多变一次 → 加 pickle 快路径，实测读到 **0.2~0.3s**。
      · 失效判据**复用 fund_split._em_sig 的 (文件数, 总 mtime, 总 size)**，
        刻意不另写一份：P0 血案（quality 覆盖静默跌半）就是两处指纹口径不一致
        导致一侧命中半成品 pkl。同一判据、同一目录，才不会一边认一边不认。
      · 与 fund_split 一样：**任一个 JSON 解析失败（并发写入中间态）就不落 pkl**，
        否则会把「缺几只」的残缺结果固化一整天。
    """
    cache_dir = cache_dir or EM_CACHE

    if use_pkl:
        try:
            import fund_split as _fs
            _sig = _fs._em_sig(cache_dir)
            # ⚠️ v4.18（2026-11-04 实测事故）：_em_sig 内部 os.stat 失败会**静默返回 None**。
            #   原逻辑 `if _sig and ...` 在 _sig=None 时跳过 pkl，直接掉进下面 3357 文件的全量
            #   遍历；而一旦 I/O 通道本身坏了（当日实测 sitecustomize 报
            #   `_BrokerProtocolError: execution attempt 不存在或已关闭`），全量遍历会
            #   **每个文件都失败** → data={} → EP 评分表 0/2174 → score_ep 0.10 权重整段空转，
            #   日志却只留下「3357 个文件解析失败（并发写入中间态）」这句**完全误导**的归因
            #   （并发写入不可能让 3357 个文件同时坏）。
            #   ⇒ _sig 拿不到时，pkl 是**唯一可信来源**：宁可吃一份已验签过的过期快照，
            #     也不要返回空表把 0.10 权重打成静默盲区。此处显式告警，不静默。
            if _sig is None and os.path.exists(EM_ALL_PKL):
                print("   ⚠️ [ep_size_ind] EM 目录指纹取不到（I/O 异常）→ 降级直读 pkl 快照")
            if (_sig is None or _sig) and os.path.exists(EM_ALL_PKL):
                meta = json.load(open(EM_ALL_PKL + ".meta", encoding="utf-8"))
                if _sig is None or tuple(meta.get("sig") or ()) == _sig:
                    import pickle
                    with open(EM_ALL_PKL, "rb") as fh:
                        cached = pickle.load(fh)
                    if cached:
                        if _sig is None:
                            print("      ⚠️ 快照未验签（%d 只），请确认是否当日数据" % len(cached))
                        return cached
        except Exception as _e:
            print("   ⚠️ [ep_size_ind] pkl 快路径异常(%s) → 回退全量遍历" % type(_e).__name__)

    data = {}
    _n_fail = 0
    _all_files = glob.glob(os.path.join(cache_dir, "*.json"))
    for f in _all_files:
        code = os.path.basename(f)[:-5]
        try:
            recs = json.load(open(f, encoding="utf-8")).get("records") or []
        except Exception:
            _n_fail += 1     # 并发写入中间态，不能当「这公司没财务数据」
            continue
        by_name = {}
        for r in recs:
            name, opt = r.get("指标"), r.get("选项")
            if not name or not opt:
                continue
            pr = _OPT_PRI.get(opt, 9)
            d = by_name.setdefault(name, {})
            # 同一 指标+选项 只保留优先级最高的一条
            if opt in d and d[opt][0] <= pr:
                continue
            ser = {}
            for k, v in r.items():
                if len(k) == 8 and k.isdigit():
                    fv = _fnum(v)
                    if fv is not None:
                        ser[k] = fv
            if ser:
                d[opt] = (pr, ser)
        ind = {}
        for name, opts in by_name.items():
            # opts 可能为空（该指标所有选项行都没有任何有效数值期）→ 跳过，
            #   否则 min() 空序列直接抛 ValueError，整个缓存加载失败。
            if not opts:
                continue
            best = min(opts.values(), key=lambda t: t[0])
            ind[name] = best[1]
        if ind:
            data[code] = ind

    if _n_fail:
        # ⚠️ 归因必须分层（2026-11-04 事故）：并发写入中间态只会影响「当时正在被覆写」的
        #   少数文件；若失败占比接近全量，那不是并发，是 **I/O 通道坏了**。
        #   原实现把 3357/3357 全失败也报成「并发写入中间态」→ 引导人去查写入进程，
        #   而真因是沙箱 broker 断连。误归因比不报更坏。
        _tot = len(_all_files) or 1
        _ratio = _n_fail / _tot
        if _ratio >= 0.5:
            print("   🚨 [ep_size_ind] EM 缓存 %d/%d (%.0f%%) 文件读取失败 —— "
                  "**疑似 I/O 通道故障，非并发写入**" % (_n_fail, _tot, _ratio * 100))
            print("      ⇒ 这些维将整段缺失（score_ep 权重空转）；"
                  "若已知 pkl 快照可用，请检查是否被上一条降级逻辑跳过")
        else:
            print("   ⚠️ [ep_size_ind] EM 缓存 %d 个文件解析失败（并发写入中间态）"
                  "→ 本轮不落 pkl，下次重试" % _n_fail)

    if use_pkl and data and not _n_fail:
        try:
            import fund_split as _fs
            import pickle
            _sig = _fs._em_sig(cache_dir)
            tmp = EM_ALL_PKL + ".tmp"
            with open(tmp, "wb") as fh:
                pickle.dump(data, fh, protocol=5)
            os.replace(tmp, EM_ALL_PKL)
            json.dump({"sig": list(_sig), "n": len(data),
                       "built": time.strftime("%Y-%m-%d %H:%M:%S")},
                      open(EM_ALL_PKL + ".meta", "w", encoding="utf-8"),
                      ensure_ascii=False)
        except Exception:
            pass
    return data


def disclosure_date(p):
    """报告期 → 法定披露截止日（年报/一季报 4/30、中报 8/31、三季报 10/31）。"""
    if not p or len(p) != 8:
        return None
    y, md = p[:4], p[4:]
    return {"0331": "%s-04-30" % y, "0630": "%s-08-31" % y,
            "0930": "%s-10-31" % y, "1231": "%s-04-30" % str(int(y) + 1)}.get(md)


def pit_period(periods_desc, d0):
    """point-in-time：披露截止日 ≤ 截面日期 的最新报告期。"""
    for p in periods_desc:
        dd = disclosure_date(p)
        if dd and dd <= d0:
            return p
    return None


def ttm_sum(ind, name, P):
    """累计值 → TTM。P 非年报时 = P + 上年年报 − 上年同期累计。"""
    def get(n, p):
        return (ind.get(n) or {}).get(p)

    cur = get(name, P)
    if cur is None:
        return None
    md = P[4:]
    if md == "1231":
        return cur
    y = int(P[:4])
    prev_annual = get(name, "%d1231" % (y - 1))
    prev_same = get(name, "%d%s" % (y - 1, md))
    if prev_annual is None or prev_same is None:
        return None
    return cur + prev_annual - prev_same


# ============================================================ 原始因子

def raw_ep(ind, P, price):
    """EP = 扣非净利润TTM / 总市值。返回 float 或 None。

    总股本 = 归母净利润 / 基本每股收益（同一报告期，量纲自洽，避免用错股本单位）。
    ⚠️ 与 audit_round26.build_factors 的 EP 段逐字同构，改这里必须重跑 round27c。
    """
    if not ind or not P or not (isinstance(price, (int, float)) and price > 0):
        return None
    ni_attr = (ind.get("归母净利润") or {}).get(P)
    eps = (ind.get("基本每股收益") or {}).get(P)
    ttm_np = ttm_sum(ind, "扣非净利润", P)
    if ttm_np is None or not ni_attr or not eps or eps <= 0.001:
        return None
    shares = ni_attr / eps
    mcap = price * shares
    if mcap <= 0:
        return None
    v = ttm_np / mcap
    if v != v or v in (float("inf"), float("-inf")):
        return None
    return v


def winsorize_z(vals, lo=0.001, hi=0.999):
    """飞书流水线 1-2 步：截面 winsorize → Z-score。→ (z_list, n_clipped) 或 (None, 0)。"""
    xs = [v for v in vals if isinstance(v, (int, float)) and v == v
          and v not in (float("inf"), float("-inf"))]
    n = len(xs)
    if n < 10:
        return None, 0
    s = sorted(xs)
    k_lo = max(0, int(math.floor(lo * (n - 1))))
    k_hi = min(n - 1, int(math.ceil(hi * (n - 1))))
    a, b = s[k_lo], s[k_hi]
    clipped = sum(1 for v in xs if v < a or v > b)
    w = [min(max(v, a), b) for v in xs]
    m = sum(w) / n
    sd = math.sqrt(sum((v - m) ** 2 for v in w) / n)
    if sd <= 1e-12:
        return None, clipped
    return [(v - m) / sd for v in w], clipped


def group_winsorize_zneg(pairs, min_grp=MIN_GRP):
    """飞书 SIZE_IND_NEUT 四步：行业分组 → 组内 winsorize → 组内 Z → 取负。

    pairs: [(code, industry, circ_mv)] → ({code: -z}, {被丢弃行业: 组大小})
    ⚠️ 被丢弃的行业显式返回，不静默（见文件头坑 3）。
    """
    by = defaultdict(list)
    for c, ind, v in pairs:
        if not (isinstance(v, (int, float)) and v > 0):
            continue
        by[ind or "未分类"].append((c, math.log(v)))
    out, dropped = {}, {}
    for ind, arr in by.items():
        if len(arr) < min_grp:
            dropped[ind] = len(arr)
            continue
        zs, _ = winsorize_z([v for _, v in arr])
        if zs is None:
            dropped[ind] = len(arr)
            continue
        for (c, _), z in zip(arr, zs):
            out[c] = -z          # 取负：组内低市值 → 高分
    return out, dropped


# ============================================================ 池内分位

def rank10(d):
    """{code: 原始值} → {code: 0-10 分位分}（升序：值越大分越高；并列取平均秩）。"""
    items = [(c, v) for c, v in (d or {}).items()
             if isinstance(v, (int, float)) and v == v]
    n = len(items)
    if n < 2:
        return {}
    items.sort(key=lambda t: t[1])
    out = {}
    i = 0
    while i < n:
        j = i
        while j + 1 < n and items[j + 1][1] == items[i][1]:
            j += 1
        avg = (i + j) / 2.0 / max(1, n - 1)
        for k in range(i, j + 1):
            out[items[k][0]] = round(avg * 10.0, 2)
        i = j + 1
    return out


def sector_l1_of(code, sw_map=None):
    """code6 → 申万一级行业名（无映射返回 None）。

    生产端 step2 里 sector_parent 要到 apply_sector_momentum 才写入，
    故此处直接用 sw_map 的 l2 → l2_parent 反查。
    """
    if sw_map is None:
        try:
            p = os.path.join(HERE, "sector", "sw_map.json")
            sw_map = json.load(open(p, encoding="utf-8"))
        except Exception:
            return None
    c6 = str(code or "").strip().lower()
    if len(c6) > 6 and c6[:2] in ("sh", "sz", "bj"):
        c6 = c6[2:]
    l2 = (sw_map.get("l2") or {}).get(c6)
    if not l2:
        return None
    return (sw_map.get("l2_parent") or {}).get(l2)


def group_rank10(groups):
    """组内升序 rank → {code6: 0-10}（组内值越大分越高；并列取平均秩）。

    groups: {l2: [(code6, value), ...]}。组内 <2 只无法 rank → 给 DOM_NEUTRAL（中性）。
    与 rank10 的区别：rank10 是整池，本函数是**组内**，用于「行业内龙头地位」。
    """
    out = {}
    for l2, arr in groups.items():
        vals = [(c, v) for c, v in arr
                if isinstance(v, (int, float)) and v == v]
        if len(vals) < 2:
            for c, _ in vals:
                out[c] = DOM_NEUTRAL
            continue
        out.update(rank10({c: v for c, v in vals}))
    return out


def pit_ttm_pair(ind, P):
    """EM 指标字典 → (营业总收入TTM, 扣非净利润TTM)，缺失返回 (None, None)。"""
    if not ind or not P:
        return None, None
    rev = ttm_sum(ind, "营业总收入", P)
    prof = ttm_sum(ind, "扣非净利润", P)
    return rev, prof


def build_shares(rows, as_of, em, sw_map, mv_key="circ_mv"):
    """计算个股市占率 → {code6: {"rev_share", "profit_share", "mc_pct"}}。

    口径（诚实标注）：
      · rev_share / profit_share：个股 PIT TTM 营业总收入 / 扣非净利润 ÷ 申万二级行业**本池**合计
        （EM 仅财务数据，市值须取自 scored 池；故分母用本池同行业合计，而非全市场）。
      · mc_pct：个股市值（circ_mv）÷ 申万二级行业**本池**合计 —— 即「个股市值占行业内占比」。
    业务份额分母为组内正数求和（利润份额允许为负 → 亏损股份额为负，rank 自然垫底）。
    ⚠️ spec 代理方案「无份额时用 mc_pct 替代」：本池 rev_share/profit_share 100% 可得（EM 2157 覆盖），
      代理路径不触发；仍保留 None 透传由下游兜底。
    """
    l2map = (sw_map or {}).get("l2") or {}
    info = {}
    for r in rows:
        code = (r.get("code") or "").strip().lower()
        c6 = code[2:] if len(code) > 6 and code[:2] in ("sh", "sz", "bj") else code
        l2 = l2map.get(c6)
        if not l2:
            continue
        rev = prof = None
        ind = em.get(c6)
        if ind:
            periods_desc = sorted({p for dd in ind.values() for p in dd}, reverse=True)
            P = pit_period(periods_desc, as_of)
            if P:
                rev, prof = pit_ttm_pair(ind, P)
        mv = r.get(mv_key)
        mv = mv if (isinstance(mv, (int, float)) and mv > 0) else None
        info[c6] = (l2, rev, prof, mv)
    # 组内正数求和（分母）
    rev_sum = defaultdict(float)
    prof_sum = defaultdict(float)
    mv_sum = defaultdict(float)
    for c6, (l2, rev, prof, mv) in info.items():
        if rev is not None and rev > 0:
            rev_sum[l2] += rev
        if prof is not None and prof > 0:
            prof_sum[l2] += prof
        if mv is not None:
            mv_sum[l2] += mv
    out = {}
    for c6, (l2, rev, prof, mv) in info.items():
        rs = (rev / rev_sum[l2]) if (rev is not None and rev_sum[l2] > 0) else None
        ps = (prof / prof_sum[l2]) if (prof is not None and prof_sum[l2] > 0) else None
        mp = (mv / mv_sum[l2]) if (mv is not None and mv_sum[l2] > 0) else None
        out[c6] = {"rev_share": rs, "profit_share": ps, "mc_pct": mp}
    return out


# ============================================================ 生产入口

def score_rows(rows, as_of, em=None, price_key="price", mv_key="circ_mv",
               sw_map=None, min_present=10, growth_key="score_growth",
               amt_key="avg_amt_20d", use_growth_gate=True, recalib_a=True,
               dominant_scope="pool"):
    """给一批行补 score_ep / score_size_growth / score_dominant（0-10），原地写入。

    rows: [{"code": "sz000001"/"000001", "price": 12.3, "circ_mv": 1.2e10,
            "score_growth": 6.2, "avg_amt_20d": 1.5e8}, ...]
    as_of: 截面日期 "YYYY-MM-DD"（PIT 用）
    use_growth_gate: True（默认）→ 市值维走「成长条件交互」（v4.12）；
                     False → 退回旧口径（纯行业内市值分位），仅用于 A/B 对照
    recalib_a: True（默认，v4.14）→ A 维原始交互分再做池内 rank 重标定为 0-10；
               False → 直接用原始交互分（与 v4.13 逐字一致），仅供消融对照
    dominant_scope: **P0-3（round118）新增，默认 "pool"**
               "pool"   → score_dominant 全池打分（覆盖率 26.6% → 97.2%）
               "leader" → 旧口径，只给业务份额组内前 30%（仅消融对照/回滚用）
               依据 audit_round118e/118f：全池版复合 IC 三周期全部上升
               （h1 +0.0204→+0.0458、h5 +0.0292→+0.0748、h20 +0.0679→+0.0779），
               与 size_growth 相关仅 −0.02~+0.05 → 不构成内部对冲。
               ⚠️ 代价：非龙头同时拿 size_growth(.20)+dominant(.10)=0.30，
               龙头只拿 dominant(.10) → 市值大类单股暴露差 3 倍，须向使用者披露。
    返回 (n_ep, n_size_growth, dropped)。

    ⚠️ 池内分位必须**整池一起算**（与 score_size/quality 同口径）：
       池逐日变化，禁止把历史截面拼起来一次性分位。
    ⚠️ 缺维不写字段（保持 None → 下游从分母剔除），禁 or 0。

    方案3（2026-10-28 老大指令）：将单一 score_size_ind 拆成两个独立维度 ——
      · 分区（老大裁决）：个股业务份额合成 rank（0.5×营收份额rank + 0.5×利润份额rank，
        申万二级行业内）≥ DOM_LEADER_THR(7.0，组内前30%) → 行业龙头，进 score_dominant(B)；
        其余进 score_size_growth(A)。
      · score_size_growth（小盘成长溢价）= 原 v4.12 成长条件交互分（行业内市值分×成长门限×
        流动性惩罚），对非龙头股票生效（**分区不变**）。
      · score_dominant（龙头壁垒溢价）= 业务份额合成 rank，再做市值一致性
        校验（DOM_MC_CONSIST，解读参数）与流动性惩罚，clip[0,10]。
        **P0-3 起默认对全池生效**；"leader" 模式下仅组内前 30% 生效（旧口径）。
    ⚠️ 原文 mc_pct>0.3 门槛在真实分布下仅覆盖 4.1%（77只）形同虚设，已弃用；mc_pct 仅作
      留痕与模块B 一致性校验输入。
    留痕字段：mc_pct / rev_share / profit_share / score_mc_raw / w_mc / dominant_scope。
    """
    if dominant_scope not in ("pool", "leader"):
        raise ValueError(f"dominant_scope 只能是 'pool' / 'leader'，收到 {dominant_scope!r}")
    if em is None:
        em = load_em_all()
    if sw_map is None:
        try:
            sw_map = json.load(open(os.path.join(HERE, "sector", "sw_map.json"),
                                    encoding="utf-8"))
        except Exception:
            sw_map = {}

    ep_raw, szi_pairs = {}, []
    for r in rows:
        code = (r.get("code") or "").strip().lower()
        c6 = code[2:] if len(code) > 6 and code[:2] in ("sh", "sz", "bj") else code
        ind = em.get(c6)
        if ind:
            periods_desc = sorted({p for dd in ind.values() for p in dd}, reverse=True)
            P = pit_period(periods_desc, as_of)
            if P:
                v = raw_ep(ind, P, r.get(price_key))
                if v is not None:
                    ep_raw[c6] = v
        mv = r.get(mv_key)
        if isinstance(mv, (int, float)) and mv > 0:
            szi_pairs.append((c6, sector_l1_of(c6, sw_map), mv))

    ep_score = rank10(ep_raw) if len(ep_raw) >= min_present else {}
    szi_z, dropped = group_winsorize_zneg(szi_pairs)
    mc_raw_score = rank10(szi_z) if len(szi_z) >= min_present else {}

    # ---- 方案3：市占率（申万二级行业内占比，本池为分母）----
    shares = build_shares(rows, as_of, em, sw_map, mv_key)

    # ---- 模块B 业务份额组内 rank ----
    rev_grp, prof_grp = defaultdict(list), defaultdict(list)
    l2map = (sw_map or {}).get("l2") or {}
    for c6, s in shares.items():
        l2 = l2map.get(c6)
        if not l2:
            continue
        if isinstance(s["rev_share"], (int, float)):
            rev_grp[l2].append((c6, s["rev_share"]))
        if isinstance(s["profit_share"], (int, float)):
            prof_grp[l2].append((c6, s["profit_share"]))
    rev_rank = group_rank10(rev_grp)
    prof_rank = group_rank10(prof_grp)

    # ---- 业务份额合成 rank（用于龙头分区）----
    syn = {}
    for c6 in shares:
        rv = rev_rank.get(c6)
        pv = prof_rank.get(c6)
        if rv is not None and pv is not None:
            syn[c6] = DOM_REV_W * rv + DOM_PROFIT_W * pv

    # ---- 统一分区：龙头进 B，其余进 A（v4.12 成长门限）----
    # v4.14：A 维两段式 —— 先算原始交互分（行业内市值分 × 成长门限 × 流动性惩罚），
    #   整池算完后再做**池内 rank 重标定**到 0-10。原因（v4.13 双视角评估实测）：
    #   w_mc 是 {0, 0.3, 0.8} 三档离散乘数 → 原始分值域塌缩到 0~8、中位≈0、
    #   均值仅 1.62/10（应≈5.0）、48% 得 0 分、排序贡献 σ 只占 6.7%（名义 8%）。
    #   rank 重标定只做单调变换，**不改变任何两只股票的相对次序**，
    #   故「低成长股票拿不到市值溢价」这一语义完全保留（w_mc=0 仍排在最末档），
    #   但把刻度拉回满量程、中位数归位 5.0、消除系统性 −0.27 分的标定拖累。
    n_no_g = n_liq_cut = 0
    n_sg = n_dom = n_dom_leader = 0
    a_raw = {}          # c6 → 重标定前的原始交互分（留痕 + 消融用）
    a_rows = {}         # c6 → 该行 dict 引用（重标定后回写）
    for r in rows:
        code = (r.get("code") or "").strip().lower()
        c6 = code[2:] if len(code) > 6 and code[:2] in ("sh", "sz", "bj") else code
        s = shares.get(c6, {})
        mc_pct = s.get("mc_pct")
        rev_share = s.get("rev_share")
        profit_share = s.get("profit_share")
        # 留痕（所有有市值/财务的行都写）
        if isinstance(mc_pct, (int, float)):
            r["mc_pct"] = round(mc_pct, 4)
        if isinstance(rev_share, (int, float)):
            r["rev_share"] = round(rev_share, 4)
        if isinstance(profit_share, (int, float)):
            r["profit_share"] = round(profit_share, 4)
        _syn_v = syn.get(c6)
        is_leader = _syn_v is not None and _syn_v >= DOM_LEADER_THR

        # ---- 模块B 龙头壁垒溢价 ----
        # P0-3（round118）起默认 **全池打分**，不再只给组内前 30%。
        # 依据 audit_round118e/118f 实测：全池覆盖率 97.2%（原 28.7%），
        # 复合 IC 三周期全部上升（h1 +0.0204→+0.0458、h5 +0.0292→+0.0748、
        # h20 +0.0679→+0.0779），且与 size_growth 相关仅 −0.02~+0.05（不构成对冲）。
        # ⚠️ 语义变化必须说清：原口径「只有龙头拿龙头溢价」，新口径是
        #   「全池都在龙头维度上排序，非龙头按份额排名拿低分」。后者更符合
        #   「份额排名是一个连续变量」的事实，但**不是**「非龙头也享受龙头溢价」。
        if dominant_scope == "pool" or is_leader:
            if _syn_v is not None:
                dom = _syn_v
                biz = max(rev_share if isinstance(rev_share, (int, float)) else 0.0,
                          profit_share if isinstance(profit_share, (int, float)) else 0.0)
                if biz > 0 and isinstance(mc_pct, (int, float)) and mc_pct < DOM_MC_CONSIST * biz:
                    dom *= DOM_DISCOUNT   # 市值一致性校验（解读参数，待确认）
                amt = r.get(amt_key)
                if isinstance(amt, (int, float)) and amt == amt and amt < LIQ_THRESHOLD:
                    dom *= LIQ_PENALTY
                r["score_dominant"] = round(max(0.0, min(10.0, dom)), 2)
                n_dom += 1
                if is_leader:
                    n_dom_leader += 1

        # ---- 模块A 小盘成长溢价（v4.12 成长条件交互）----
        # ⚠️ 分区条件**保持不变**：size_growth 仍只给非龙头。
        #   理由：该维语义是「小盘成长溢价」，龙头按定义不是小盘；
        #   若对龙头也展开，等于把「越小越高分」套到大市值股上，方向错。
        #   于是非龙头同时拿到 size_growth(.20) + dominant(.10) = 市值大类 0.30，
        #   而龙头只有 dominant(.10)。这个 3 倍暴露差是扩张的直接后果，
        #   **必须披露**（页面图例 + 报告），不能当成「覆盖率修好了就没事了」。
        if not is_leader:
            raw = mc_raw_score.get(c6)
            if raw is None:
                continue
            g = r.get(growth_key)
            if not (isinstance(g, (int, float)) and not isinstance(g, bool) and g == g):
                n_no_g += 1
            amt = r.get(amt_key)
            val = interact_size_growth(raw, g, amt)
            if val is None:
                continue
            if isinstance(amt, (int, float)) and amt == amt and amt < LIQ_THRESHOLD:
                n_liq_cut += 1
            a_raw[c6] = val
            a_rows[c6] = r
            r["score_mc_raw"] = raw           # 留痕：未打折的行业内市值分
            r["w_mc"] = growth_weight(g)      # 留痕：本次生效的溢价权重

    # ---- A 维重标定（v4.14）----
    # ⚠️ 关键设计约束（第一版实现踩坑后修正）：
    #   不能对**全部** a_raw 做 rank 重标定。a_raw 里 w_mc=0 的低成长股票（655 只，
    #   占 A 维 47.9%）原始分全是并列的 0.0；rank10 对并列值取平均秩，会把这一大坨
    #   抬到分布中位（实测 p50 从 0 → 5.00）—— 成长门限的「低成长不给溢价」语义
    #   被完全抹掉，反而变成给低成长股送 5 分。这是比原标定偏倚更严重的语义错误。
    #   正确做法：**只对拿到溢价的股票（w_mc>0）在 0-10 上展开**，w_mc=0 的明确给 0。
    #   这样：① 低成长仍是 0 分（语义保留）② 有溢价的部分不再被 0.8 上限压到 8.0
    #   （恢复结构性上限到 10）③ 不制造任何新的加分。
    #   ⚠️ 相应地，「均值应≈5.0」这个判据本身是错的：A 维是**条件性维度**
    #   （只有成长达标的才给分），本就不该均匀分布。中位数仍会偏低，这是设计而非缺陷。
    if recalib_a and len(a_raw) >= min_present:
        pos = {c: v for c, v in a_raw.items()
               if isinstance(v, (int, float)) and v > 0}
        a_score = rank10(pos) if len(pos) >= min_present else {}
        for c6, val in a_raw.items():
            rr = a_rows.get(c6)
            if rr is None:
                continue
            rr["score_size_growth_raw"] = round(val, 2)   # 留痕：重标定前
            if c6 in a_score:
                rr["score_size_growth"] = a_score[c6]
            else:
                # w_mc=0 或流动性打对折后仍为 0 → 明确 0 分，不参与 rank
                rr["score_size_growth"] = 0.0
            n_sg += 1
    else:
        # 关闭重标定（仅消融对照）→ 直接用原始交互分，与 v4.13 逐字一致
        for c6, val in a_raw.items():
            rr = a_rows.get(c6)
            if rr is None:
                continue
            rr["score_size_growth"] = round(val, 2)
            n_sg += 1
    if n_no_g or n_liq_cut:
        print(f"   🧮 [模块A 小盘成长] {n_no_g} 只缺成长分（溢价按 0），"
              f"{n_liq_cut} 只日均额<{LIQ_THRESHOLD/1e8:.1f}亿（减半）；实际落分 {n_sg} 只")
    if n_dom:
        if dominant_scope == "pool":
            print(f"   🏛️ [模块B 龙头壁垒] 落分 {n_dom} 只（**全池**口径；"
                  f"其中组内前 30% 龙头 {n_dom_leader} 只，其余 {n_dom - n_dom_leader} 只是"
                  f"「按份额排名拿低分」而非「享受龙头溢价」）")
        else:
            print(f"   🏛️ [模块B 龙头壁垒] 落分 {n_dom} 只（业务份额组内前 30%，旧口径）")
    if dominant_scope == "pool" and n_dom:
        print(f"      ⚠️ 暴露不对称：非龙头同时拿 size_growth(.20)+dominant(.10)=0.30，"
              f"龙头仅 dominant(.10) → 市值大类单股暴露差 3 倍（须在页面/报告披露）")

    n_ep = 0
    for r in rows:
        code = (r.get("code") or "").strip().lower()
        c6 = code[2:] if len(code) > 6 and code[:2] in ("sh", "sz", "bj") else code
        if c6 in ep_score:
            r["score_ep"] = ep_score[c6]
            n_ep += 1
    return n_ep, n_sg, dropped


if __name__ == "__main__":
    # 冒烟：用最新 scored 跑一遍，打印覆盖率（不写盘）
    import sys
    fs = sorted(glob.glob(os.path.join(HERE, "top200_scored_*.json")), reverse=True)
    fs = [f for f in fs if ".bak" not in f]
    if not fs:
        print("无 scored 文件")
        sys.exit(1)
    d = json.load(open(fs[0], encoding="utf-8"))
    rows_o = d.get("results") or []
    as_of = d.get("date") or "2026-10-23"
    print(f"{os.path.basename(fs[0])} | n={len(rows_o)} | as_of={as_of}\n")

    for scope in ("leader", "pool"):
        # 深拷贝行，避免两轮互相污染（score_rows 是**原地写**）
        rows = [dict(r) for r in rows_o]
        for k in ("score_ep", "score_size_growth", "score_dominant", "mc_pct",
                  "rev_share", "profit_share", "score_mc_raw", "w_mc",
                  "score_size_growth_raw"):
            for r in rows:
                r.pop(k, None)
        ne, ns, dr = score_rows(rows, as_of, dominant_scope=scope)
        n_dom = sum(1 for r in rows if isinstance(r.get("score_dominant"), (int, float)))
        n_leader = sum(1 for r in rows if isinstance(r.get("score_dominant"), (int, float))
                       and isinstance(r.get("score_size_growth"), (int, float)))
        print(f"  ── scope={scope} ──")
        print(f"  score_ep          {ne}/{len(rows)} = {ne / max(1, len(rows)) * 100:.1f}%")
        print(f"  score_size_growth {ns}/{len(rows)} = {ns / max(1, len(rows)) * 100:.1f}%")
        print(f"  score_dominant    {n_dom}/{len(rows)} = {n_dom / max(1, len(rows)) * 100:.1f}%")
        for k in ("score_ep", "score_size_growth", "score_dominant"):
            vs = sorted(r[k] for r in rows if isinstance(r.get(k), (int, float)))
            if vs:
                print(f"    {k}: min {vs[0]:.2f} / p50 {vs[len(vs) // 2]:.2f} / max {vs[-1]:.2f}")
        if dr:
            print(f"  行业内 Z 丢弃行业 {len(dr)} 个（共 {sum(dr.values())} 只）：{dr}")
        # 互斥性核查：leader 模式下应为 0；pool 模式下属设计内允许 >0
        both = sum(1 for r in rows
                   if isinstance(r.get("score_size_growth"), (int, float))
                   and isinstance(r.get("score_dominant"), (int, float)))
        print(f"  同时落 A+B 的只数：{both}"
              f"（leader 模式应为 0；pool 模式=非龙头数，属设计内）\n")

