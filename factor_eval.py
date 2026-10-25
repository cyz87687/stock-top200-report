#!/usr/bin/env python3
"""因子有效性评估（Rank IC）+ IC 驱动权重优化 —— 量化视角衡量"推荐/因子"的预测能力。

v2（2026-10-13 量化工程审查后重写）—— 修正 v1 的三个口径缺陷并新增权重优化：

━━━ v1 口径缺陷与修正 ━━━
1. 非交易日陈旧快照：假日/接口故障日 workflow 照常产出 scored json（07:00 前守卫缺失），
   v1 用「快照下标 i+h」当 h 个交易日 → 跨假日窗口实际不足 h 日，且陈旧日贡献全 0 收益。
   修正：以回填的全市场前复权日线日期为真实交易日序列，快照按交易日对齐后取第 h 个后续交易日；
   非交易日快照（如 10-01~10-07）整体剔除。
2. 样本流失（TOP200 留存偏差）：v1 只对「t 与 t+h 都在榜」的个股算收益，
   流失率 T+1 14.6% / T+5 25.2% / T+20 34.0%，且流失非随机 → IC 为留存子样本的条件估计。
   修正：回填全部历史代码前复权日线，对 t 日全部 TOP200 个股计算收益。
   （前复权口径与推荐跟踪表一致；7 月为 A 股分红密集期，不复权会制造伪跌幅。）
3. 重叠窗口 t 值虚高：h=5/20 相邻 IC 观测共享 80%/95% 收益窗口，序列自相关使
   t=mean/(std/√n) 高估显著性。修正：Newey-West HAC（Bartlett, lag=h-1）；
   多重检验（5 因子 × 3 周期 = 15 次）按 Bonferroni 阈值 |t|≥2.97 与宽松 |t|≥2 两级披露。

━━━ 指标定义 ━━━
Rank IC(t→t+h) = Spearman(因子值 t 日截面秩, 收益率 t+h)；h∈{1,5,20} 交易日。
聚合：IC均值 / IC标准差 / t(普通) / t(Newey-West) / IC胜率 / 分层多空(5组头尾价差) /
      半期稳定性 / Bonferroni 校正显著性。
对象：五维因子全截面 IC + 推荐组合层 IC + 因子间截面相关矩阵 + 复合总分 IC。

━━━ 权重优化（--optimize-weights）━━━
规则：① 仅 IC>0 的维度按 IC 比例分配权重，负向维度降至地板 5%；
      ② 向先验收缩 w=λ·w_IC+(1-λ)·w_prior 防过拟合；
      ③ 走步验证（expanding window，训练≥30 截面，每 5 截面重拟，样本外比对）；
      ④ 目标周期 T+1（与日报 operational 口径一致、无重叠、流失最低），T+5/T+20 作稳健性门槛；
      ⑤ score_sector 有效截面不足（<20）→ 权重维持 0.12，标记"样本积累中"。
产出：factor_weights.json（供 step2 / apply_sector_momentum 读取）。

用法：
  python3 factor_eval.py                       # 计算 → factor_ic.json（日常）
  python3 factor_eval.py --bootstrap-prices    # 回填全量前复权日线（周期性，网络）
  python3 factor_eval.py --bootstrap-from-git  # 从 git 历史初始化截面（仅本地）
  python3 factor_eval.py --optimize-weights    # 拟合新权重 → factor_weights.json
  python3 factor_eval.py --show                # 打印结果表

数据纪律：全部取自行程内真实文件/接口；样本不足如实标注，不补齐、不编造。
投资有风险，本内容仅供研究与教育，不构成投资建议。
"""
import json
import os
import sys
import glob
import math
import time
import subprocess
import urllib.request
from collections import defaultdict
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
HISTORY_PATH = os.path.join(HERE, "factor_history.json")
OUT_PATH = os.path.join(HERE, "factor_ic.json")
TRACK_PATH = os.path.join(HERE, "recommendation_track.json")
WEIGHTS_PATH = os.path.join(HERE, "factor_weights.json")

HORIZONS = (1, 5, 20)
MIN_SAMPLE = 20
QUANTILES = 5
# v3.0-10dim: 检验家族 = 10 维度 × 3 周期 = 30 次（旧五维期 tech/theme 已拆解，
#   保留行仅作对照展示）。旧样本期新维度无快照字段，实际有效检验数以输出为准。
N_TESTS = 30                       # 10 因子 × 3 周期（Bonferroni 家族检验数）
T_BONF = 3.08                      # α=0.05 双侧 Bonferroni 阈值（0.05/30）
T_LOOSE = 2.0                      # 宽松阈值（业界常用）

# 因子字段 → 展示名（顺序即报告表格顺序）
# v3.0-10dim：tech/theme 标注「已拆解·仅对照」（其有效成分由 rsi_rev/vol_pulse/
#   overbought/theme_intrinsic/attention 承载，负向成分证据见下）
FACTORS = [
    ("total", "综合总分"),
    ("score_fund", "基本面"),
    ("score_news", "消息面"),
    ("score_oversold60", "超跌60反转"),
    ("score_bias60", "乖离低位"),
    ("score_rsi_rev", "RSI反转"),
    ("score_vol_pulse", "量能反向"),
    ("score_overbought", "超买反向"),
    ("score_theme_intrinsic", "题材内禀"),
    ("score_attention", "关注度相对"),
    ("score_sector", "板块动量"),
    ("score_theme", "题材热度（已拆解·对照）"),
    ("score_tech", "技术面（已拆解·对照）"),
    # v4.3（P0 修复，round17 穿透审计）：这两个维度承载 0.24 的权重，但此前**不在
    #   FACTORS 里** → 日报的因子 IC 表从未单独量过它们，只在 _composite_ic 里被
    #   _derive_dims 隐式带入。后果正是老大指出的「只应用了部分」：日报一面给
    #   theme_neg/size 各配 12% 权重，一面又在自己的证据表里找不到这两行的任何
    #   IC/t/胜率 —— 权重与证据在两个不同的表里，永远对不上。
    #   ⚠️ score_size 只有 3/78 个截面带 circ_mv → n 会很小，判定逻辑本就按
    #      MIN_SAMPLE 报「样本积累中」，这是如实披露而非缺陷；theme_neg 由
    #      score_theme 派生（58 个截面可用），样本与 score_theme 同源。
    ("score_theme_neg", "题材反号（10−题材热度）"),
    ("score_size", "小市值（池内流通市值分位）"),
]
# 派生维度：快照里没有该字段，须在内存派生后再评估（口径与 _composite_ic 一致）
DERIVED_FIELDS = {"score_theme_neg", "score_size"}
# 可优化权重的九个维度（fund/news 池内已验证；新拆 5 维先验映射待池内积累；
#   2 先验维弱证据）；score_sector 样本不足暂固定 0.10
# ⚠️ v4.1（P0 修复）：OPT_DIMS 专门服务「相关性矩阵 / 权重拟合 / 复合 IC」，
#   其成员要求**同时在多数截面有值**，故不含 v4.1 新增的两维：
#     · score_size 仅在有 circ_mv 的截面存在（78 个截面里只有 3 个有市值）
#     · score_theme_neg 虽然 58 个截面都有（由 score_theme 派生，派生在内存算），
#       但快照里没有该字段 → 纳入会让 _factor_corr / _composite_ic 的可用截面塌到 0。
#   → 权重文件的**完整性校验**改用全量维度表 ALL_W_DIMS（含两维新维度），
#     而不是 OPT_DIMS。此前 load_weights 用 OPT_DIMS 校验 v4.1 的 12 维文件，
#     sum(9维+sector)=0.70 != 1.0 → 静默抛异常返回 None →
#     active_weights 恒为 null、「当前权重复合 IC」整体失效且无任何告警。
OPT_DIMS = ["score_fund", "score_news", "score_oversold60", "score_bias60",
            "score_rsi_rev", "score_vol_pulse", "score_overbought",
            "score_theme_intrinsic", "score_attention"]
# 全量权重维度（= factor_weights.json 应含的全部键，合计必须为 1.0）
ALL_W_DIMS = OPT_DIMS + ["score_theme_neg", "score_size", "score_sector"]
# 十维先验（与 factor_weights.json v3.6-fund-led 一致；运行时会优先读该文件 prior_weights）
PRIOR_W10 = {"score_fund": 0.32, "score_news": 0.16, "score_theme_neg": 0.12, "score_size": 0.12, "score_sector": 0.10, "score_oversold60": 0.045, "score_bias60": 0.045, "score_rsi_rev": 0.045, "score_vol_pulse": 0.045, "score_overbought": 0.0, "score_theme_intrinsic": 0.0, "score_attention": 0.0}   # 合计0.90，sector 固定0.10补足
W_SECTOR_FIXED = 0.10              # score_sector 固定权重（样本积累中）
W_FLOOR = 0.03                     # 单维度地板（十维下保持维度意义；3%≈旧五维5%量纲）
TARGET_H = 1                       # 权重优化目标周期

FACTOR_VERDICTS = [
    (0.05, 2.0, "显著有效", "#22c55e"),
    (0.03, 1.5, "弱有效", "#84cc16"),
    (0.02, 0.0, "方向为正·不显著", "#f59e0b"),
]

# 价格回填（腾讯前复权日线，多域名轮询；与 step2/track 同口径）
TQ_HOSTS = ["https://ifzq.gtimg.cn", "https://web.ifzq.gtimg.cn", "https://proxy.finance.qq.com/ifzqgtimg"]
PX_NEED = 130                      # 覆盖 2026-06-01 起全部交易日（约90根）留余量


# ---------------------------------------------------------------- 工具函数
def _norm_code(code):
    c = str(code or "").strip().lower()
    return c[2:] if len(c) > 6 and c[:2] in ("sh", "sz", "bj") else c


def _symbol(code6):
    """6 位代码 → 带市场前缀（腾讯参数用）。"""
    c = _norm_code(code6)
    if not c or len(c) != 6:
        return None
    if c[0] in "659":
        return "sh" + c
    if c[0] in "03":
        return "sz" + c
    if c[0] in "48":
        return "bj" + c
    return "sz" + c


def _ranks(vals):
    """平均秩（处理并列）。返回与 vals 等长的秩列表(1-based)。"""
    order = sorted(range(len(vals)), key=lambda i: vals[i])
    ranks = [0.0] * len(vals)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and vals[order[j + 1]] == vals[order[i]]:
            j += 1
        avg = (i + j) / 2.0 + 1.0
        for k in range(i, j + 1):
            ranks[order[k]] = avg
        i = j + 1
    return ranks


def _pearson(x, y):
    n = len(x)
    if n < 3:
        return None
    mx, my = sum(x) / n, sum(y) / n
    sxy = sum((a - mx) * (b - my) for a, b in zip(x, y))
    sxx = sum((a - mx) ** 2 for a in x)
    syy = sum((b - my) ** 2 for b in y)
    if sxx <= 0 or syy <= 0:
        return None
    return sxy / math.sqrt(sxx * syy)


def rank_ic(factor_vals, ret_vals):
    """Spearman 秩相关。样本不足或零方差返回 None。"""
    if len(factor_vals) < 5:
        return None
    return _pearson(_ranks(factor_vals), _ranks(ret_vals))


def _mean(xs):
    xs = [x for x in xs if x is not None]
    return sum(xs) / len(xs) if xs else None


def _std(xs):
    xs = [x for x in xs if x is not None]
    if len(xs) < 2:
        return None
    m = sum(xs) / len(xs)
    return math.sqrt(sum((x - m) ** 2 for x in xs) / (len(xs) - 1))


def _andrews_lag(series):
    """Andrews(1991) AR(1) 自动带宽 —— 数据驱动，不预设 lag=h-1。

    v4.1（P0）：`lag=h-1` 是个**能翻转结论的隐含自由度**，必须给出数据驱动的参照。
    实证（round13，见 audit_round13.json）：n=58 的 h=20 序列，lag 从 3 到 6
    就能让 score_theme 的 t 值从 -3.35（过 3.08）掉到 -2.86（不过门槛）。
    全部因子×周期 24 个组合里，只有 1 个在全 lag 下都过门槛，11 个会翻转。
    → 结论：绝不能只报「lag=h-1 的 t」就说某因子显著/不显著。
    """
    xs = [x for x in series if x is not None]
    n = len(xs)
    if n < 5:
        return 0
    m = sum(xs) / n
    e = [x - m for x in xs]
    g0 = sum(v * v for v in e) / n
    if g0 <= 0:
        return 0
    rho = sum(e[i] * e[i - 1] for i in range(1, n)) / n / g0
    rho = max(min(rho, 0.97), -0.97)
    a = 4.0 * rho ** 2 / ((1 - rho) ** 2 * (1 + rho) ** 2)
    return max(0, min(int(math.ceil(1.1447 * (a * n) ** (1.0 / 3.0))), n - 1))


def _nw_t(series, lag):
    """Newey-West HAC t 统计量（Bartlett 核）。

    重叠收益窗口（h>1）下相邻 IC 观测高度自相关，普通 t 值把 n 当独立样本、
    显著性虚高。lag=h-1：h=1 时退化为普通 t。
    """
    xs = [x for x in series if x is not None]
    n = len(xs)
    if n < 3:
        return None
    m = sum(xs) / n
    e = [x - m for x in xs]
    g0 = sum(v * v for v in e)
    s = g0
    for k in range(1, lag + 1):
        if k >= n:
            break
        gk = sum(e[i] * e[i - k] for i in range(k, n))
        s += 2.0 * (1.0 - k / (lag + 1.0)) * gk
    if s <= 0:
        return None
    var = s / n
    return m / math.sqrt(var / n)


# v4.1（P0）：lag 敏感性扫描用的候选带宽（0=退化为普通 t）
_NW_LAG_GRID = (0, 1, 2, 3, 4, 6, 8, 12, 19)


def _nw_sensitivity(series, lag_main):
    """扫描多个 NW lag，判定「显著性」是否稳健（v4.1 P0）。

    返回：
      t_by_lag     各 lag 下的 t 值
      t_min/t_max  极值
      robust       所有 lag 下 |t| 都 ≥ T_BONF 才算稳健显著
      flips        「是否过门槛」在不同 lag 下不一致（即结论可被 lag 翻转）
      lag_andrews  Andrews 自动带宽及其 t（数据驱动参照，非拟合）
    """
    xs = [x for x in series if x is not None]
    if len(xs) < 3:
        return None
    lag_a = _andrews_lag(xs)
    ts = {}
    for L in set(_NW_LAG_GRID) | {lag_a, max(0, lag_main)}:
        v = _nw_t(xs, L)
        if v is not None:
            ts[int(L)] = round(v, 2)
    if not ts:
        return None
    tmin, tmax = min(ts.values()), max(ts.values())
    _pass = {L: abs(v) >= T_BONF for L, v in ts.items()}
    return {
        "t_by_lag": dict(sorted(ts.items())),
        "t_min": tmin,
        "t_max": tmax,
        "lag_andrews": lag_a,
        "t_andrews": ts.get(lag_a),
        "robust_bonferroni": bool(all(_pass.values())),
        "flips": bool(any(_pass.values()) and not all(_pass.values())),
    }


def _plain_t(series):
    xs = [x for x in series if x is not None]
    n = len(xs)
    if n < 2:
        return None
    m, sd = _mean(xs), _std(xs)
    if not sd or sd <= 0:
        return None
    return m / (sd / math.sqrt(n))


def _subperiod(series):
    """半期拆分：制度依赖检测。前半段全正、后半段转负的因子，全样本均值会高估当期有效性。"""
    xs = [x for x in series if x is not None]
    n = len(xs)
    if n < 10:
        return None
    h1 = xs[:n // 2]
    h2 = xs[n // 2:]
    m1, m2 = _mean(h1), _mean(h2)
    agree = sum(1 for x in xs if (x > 0) == ((m1 + m2) > 0)) / n
    return {
        "first_half_mean": round(m1, 4),
        "second_half_mean": round(m2, 4),
        "sign_stable_pct": round(agree * 100, 1),
        "regime_shift": bool(m1 * m2 < 0),
    }


def _quantile_spread(factor_vals, ret_vals, q=QUANTILES):
    """分层多空差价差：按因子降序分 q 组，头部组均值收益 − 尾部组均值收益（%）。"""
    n = len(factor_vals)
    if n < q * 5:
        return None
    order = sorted(range(n), key=lambda i: -factor_vals[i])
    g = max(1, n // q)
    top = [ret_vals[i] for i in order[:g]]
    bot = [ret_vals[i] for i in order[-g:]]
    return (_mean(top) or 0) - (_mean(bot) or 0)


# ---------------------------------------------------------------- 历史管理
def snapshot_from_scored(path):
    """从 top200_scored_*.json 提取紧凑截面快照。"""
    with open(path, encoding="utf-8") as f:
        d = json.load(f)
    rows = []
    for r in d.get("results", []):
        px = r.get("price")
        if not isinstance(px, (int, float)) or px <= 0:
            continue
        row = {"code": _norm_code(r.get("code")), "price": px}
        for k, _ in FACTORS:
            v = r.get(k)
            if isinstance(v, (int, float)):
                row[k] = v
        # v3.8.2（P0）：母体口径必须落快照，否则「新旧母体分段体检」永远缺依据。
        #   10-13 起 scored 已带 circ_mv（490/490），但此前只透传 FACTORS 字段被丢弃
        #   → pool_excess_by_regime 全部走"无市值→跳过"，静默返回样本不足。
        for _k in ("circ_mv", "total_mv", "amount"):
            _v = r.get(_k)
            if isinstance(_v, (int, float)) and _v > 0:
                row[_k] = _v
        if len(row) > 2:
            rows.append(row)
    # v3.0 A2: 池口径留痕(扩池前后截面不可混算 IC; 旧文件无该字段 → 200)
    snap = {"date": str(d.get("date") or os.path.basename(path)), "rows": rows,
            "pool_size": int(d.get("pool_size") or 200)}
    # v4.1（P1-2）：母体口径显式落快照。
    #   原先 pool_excess_by_regime 靠「池内流通市值中位 <60亿」反推母体，
    #   但 v4.1 分层抽样后池内中位 85.9 亿 → 猜法必然把新母体判成旧母体。
    #   这里优先取 scored 透传的 pool_rules.version；缺失（10-24 前的旧文件）
    #   则回落 legacy，由下游按「有/无版本号」区分「未知」与「已知旧母体」。
    _pr = d.get("pool_rules") or {}
    snap["pool_version"] = _pr.get("version") or "legacy"
    if _pr:
        snap["pool_meta"] = {k: _pr.get(k) for k in
                             ("version", "universe", "sort_key", "size_quantile",
                              "sample_strata", "size_as_factor", "min_amount",
                              "min_mktcap", "price_floor", "drop_mom_top_pct",
                              "mv_coverage_pct", "mv_upper_yi", "mv_log_sd",
                              "sampling") if k in _pr}
    return snap


def load_history():
    if not os.path.exists(HISTORY_PATH):
        return {"version": 1, "snapshots": []}
    try:
        with open(HISTORY_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"version": 1, "snapshots": []}


def save_history(hist):
    with open(HISTORY_PATH, "w", encoding="utf-8") as f:
        json.dump(hist, f, ensure_ascii=False, separators=(",", ":"))


def upsert_snapshot(hist, snap, replace=True):
    if not snap or not snap.get("rows"):
        return False
    snaps = hist.setdefault("snapshots", [])
    for i, s in enumerate(snaps):
        if s.get("date") == snap["date"]:
            if replace:
                snaps[i] = snap
                return True
            return False
    snaps.append(snap)
    snaps.sort(key=lambda s: s["date"])
    return True


def bootstrap_from_git():
    """从 git 历史提取全部 top200_scored_*.json 截面（仅本地；Actions 无历史）。"""
    try:
        out = subprocess.run(
            ["git", "log", "--all", "--diff-filter=A", "--name-only", "--format=%H",
             "--", "top200_scored_*.json"],
            capture_output=True, text=True, cwd=HERE, timeout=180).stdout
    except Exception as e:
        print(f"❌ git 历史读取失败: {e}")
        return 1
    commit_of = {}
    cur_commit = None
    for line in out.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith("top200_scored_"):
            if "_watch_" in line:
                continue
            date = line[len("top200_scored_"):-len(".json")]
            commit_of.setdefault(date, cur_commit)
        elif len(line) >= 7 and all(c in "0123456789abcdef" for c in line):
            cur_commit = line
    if not commit_of:
        print("⚠️ git 历史中未找到 top200_scored_*.json")
        return 1

    hist = load_history()
    n_new = n_fail = 0
    for date in sorted(commit_of):
        if any(s.get("date") == date for s in hist.get("snapshots", [])):
            continue
        commit = commit_of[date]
        try:
            raw = subprocess.run(["git", "show", f"{commit}:top200_scored_{date}.json"],
                                 capture_output=True, text=True, cwd=HERE,
                                 timeout=180).stdout
            d = json.loads(raw)
            tmp = os.path.join(HERE, f"._boot_{date}.json")
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(d, f, ensure_ascii=False)
            snap = snapshot_from_scored(tmp)
            os.remove(tmp)
            if upsert_snapshot(hist, snap):
                n_new += 1
        except Exception as e:
            n_fail += 1
            print(f"   ⚠️ {date} 提取失败: {str(e)[:60]}")
    save_history(hist)
    print(f"✅ 引导完成: factor_history.json 现有 {len(hist['snapshots'])} 个截面"
          f"（新增 {n_new}，失败 {n_fail}）")
    return 0


# ---------------------------------------------------------------- 价格回填（P0-2）
def _fetch_qfq(sym, need=PX_NEED, with_open=False):
    """腾讯前复权日线 → [(date, close)]；with_open=True 时 → [(date, open, close)]。
    多域名轮询，WAF 页自动换域。
    注：腾讯 qfqday 原始行 = [日期, 开, 收, 高, 低, 量]，开盘价一直可取，此前只取收盘。
    """
    for _host in TQ_HOSTS:
        url = f"{_host}/appstock/app/fqkline/get?param={sym},day,,,{need},qfq"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        try:
            with urllib.request.urlopen(req, timeout=12) as r:
                txt = r.read().decode("utf-8")
        except Exception:
            continue
        if txt.lstrip().startswith("<"):
            continue
        try:
            data = json.loads(txt)
            kdata = data.get("data", {}).get(sym, {})
            raw_kl = kdata.get("qfqday") or kdata.get("day")
        except Exception:
            continue
        if raw_kl:
            out = []
            for k in raw_kl:
                try:
                    if with_open:
                        out.append((k[0], float(k[1]), float(k[2])))
                    else:
                        out.append((k[0], float(k[2])))
                except Exception:
                    continue
            if out:
                return out
    return None


def bootstrap_prices():
    """回填全部历史代码前复权日线 → hist['prices']={dates, px}。

    用途：① 真实交易日序列（腾讯日线只含交易日，天然剔除假日陈旧快照）；
          ② 全样本收益（消除 TOP200 留存偏差）。周期性运行（网络 ~709 请求）。
    """
    hist = load_history()
    codes = set()
    for s in hist.get("snapshots", []):
        for r in s.get("rows", []):
            codes.add(r["code"])
    codes = sorted(c for c in codes if c and len(c) == 6)
    if not codes:
        print("⚠️ 无历史截面，先 --bootstrap-from-git")
        return 1

    old = hist.get("prices") or {}
    old_dates = old.get("dates") or []
    old_px = old.get("px") or {}
    old_op = old.get("opens") or {}
    px_series = {}          # code -> {date: close}
    op_series = {}          # code -> {date: open}  （v3.6 可交易 IC 用）
    n_ok = n_fail = n_skip = 0
    for i, c in enumerate(codes):
        sym = _symbol(c)
        if not sym:
            n_skip += 1
            continue
        if (i + 1) % 100 == 0:
            print(f"   回填进度 {i + 1}/{len(codes)}（成功 {n_ok} 失败 {n_fail}）")
        # 已有且覆盖到最新交易日则跳过（增量场景）
        prev = old_px.get(c)
        kl = _fetch_qfq(sym, with_open=True)
        if not kl:
            # 保留旧数据（接口偶发失败不丢历史）
            if prev:
                px_series[c] = {d: v for d, v in zip(old_dates, prev) if v is not None}
                _po = old_op.get(c)
                if _po:
                    op_series[c] = {d: v for d, v in zip(old_dates, _po) if v is not None}
                n_skip += 1
            else:
                n_fail += 1
            continue
        px_series[c] = {d: cl for d, op, cl in kl}
        op_series[c] = {d: op for d, op, cl in kl if op}
        n_ok += 1
        time.sleep(0.03)

    all_dates = sorted({d for m in px_series.values() for d in m})
    px_compact = {c: [m.get(d) for d in all_dates] for c, m in px_series.items()}
    op_compact = {c: [m.get(d) for d in all_dates] for c, m in op_series.items()}
    hist["prices"] = {
        "fetched_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "dates": all_dates,
        "px": px_compact,
        "opens": op_compact,
        "n_codes": len(px_compact),
        "n_open_codes": len(op_compact),
    }
    save_history(hist)
    print(f"✅ 价格回填完成: {len(px_compact)} 只（新抓 {n_ok}，保留旧 {n_skip}，失败 {n_fail}）"
          f"，含开盘价 {len(op_compact)} 只，交易日 {len(all_dates)} 个（{all_dates[0]} ~ {all_dates[-1]}）")
    return 0


def px_index(hist):
    """→ (tdays, {code: {date: close}})；无回填数据时退化为快照日期（含陈旧过滤）。"""
    p = hist.get("prices") or {}
    dates = p.get("dates") or []
    px_raw = p.get("px") or {}
    if dates and px_raw:
        px = {c: {d: v for d, v in zip(dates, arr) if v is not None}
              for c, arr in px_raw.items()}
        return list(dates), px
    # 退化：快照日期即交易日（旧行为）；陈旧快照在调用方按 tdays 成员过滤
    snaps = hist.get("snapshots", [])
    px = {}
    for s in snaps:
        for r in s.get("rows", []):
            px.setdefault(r["code"], {})[s["date"]] = r["price"]
    return sorted({s["date"] for s in snaps}), px


def open_index(hist):
    """→ {code: {date: open}}（v3.6 可交易 IC 用；无 opens 字段时返回空 dict）。"""
    p = hist.get("prices") or {}
    dates = p.get("dates") or []
    op_raw = p.get("opens") or {}
    if not (dates and op_raw):
        return {}
    return {c: {d: v for d, v in zip(dates, arr) if v is not None}
            for c, arr in op_raw.items()}


def merge_snapshot_prices(hist):
    """把快照自带的当日价格并入 px 图（日常零成本增量，无需网络）。"""
    p = hist.get("prices")
    if not p or not p.get("dates"):
        return False
    dates = p["dates"]
    didx = {d: i for i, d in enumerate(dates)}
    px = p["px"]
    changed = False
    for s in hist.get("snapshots", []):
        d = s["date"]
        if d not in didx:
            continue                      # 非交易日快照不并入交易日价格
        i = didx[d]
        for r in s.get("rows", []):
            arr = px.get(r["code"])
            if arr is not None and (arr[i] is None) and isinstance(r.get("price"), (int, float)):
                arr[i] = r["price"]
                changed = True
    return changed


def refresh_price_tail(hist, verbose=True):
    """价格 store 落后最新快照时**增量**抓取缺失尾部（日常自愈，无需手动 --bootstrap-prices）。

    背景：日常登记只 upsert 截面、不刷新 prices store；若长期不跑 --bootstrap-prices，
    最新截面会因缺前向价格被整体剔除（曾致 10-13~10-21 连续 7 个截面未入 IC 计算，
    页面停留在旧 snapshot_range）。本函数在每次跑 factor_eval 时自动补齐。

    · 触发条件：prices.dates[-1] < 最新快照日期（存在未回填的交易日快照）；
    · 抓取范围：历史全部出现过的代码，need = clamp(缺口日历天数+12, 30, PX_NEED) 根；
    · 只追加 dates[-1] 之后的新交易日，并顺带填补已有日期上的 None 空洞（不覆盖旧值）；
    · 接口失败保留旧值；无新交易日则原样返回。返回 bool 表示是否有更新。
    """
    p = hist.get("prices") or {}
    dates, px = p.get("dates") or [], p.get("px") or {}
    opens = p.get("opens") or {}
    if not dates or not px:
        return False                      # 无 store → 走 --bootstrap-prices 全量重建
    snaps = [s for s in hist.get("snapshots", []) if s.get("rows")]
    if not snaps:
        return False
    last_snap = max(s["date"] for s in snaps)
    if last_snap <= dates[-1]:
        return False                      # store 已覆盖最新快照，零成本跳过
    codes = sorted({str(r["code"]) for s in snaps for r in s["rows"]
                    if r.get("code") and len(str(r["code"])) == 6})
    try:
        gap = (datetime.strptime(last_snap, "%Y-%m-%d")
               - datetime.strptime(dates[-1], "%Y-%m-%d")).days
    except ValueError:
        gap = 7
    need = min(PX_NEED, max(30, gap + 12))
    tail = {}          # code -> {date: close}
    tail_op = {}       # code -> {date: open}（v3.6 可交易 IC）
    n_ok = n_fail = 0
    for i, c in enumerate(codes):
        sym = _symbol(c)
        if not sym:
            continue
        kl = _fetch_qfq(sym, need=need, with_open=True)
        if kl:
            tail[c] = {d: cl for d, op, cl in kl}
            tail_op[c] = {d: op for d, op, cl in kl if op}
            n_ok += 1
        else:
            n_fail += 1
        if verbose and (i + 1) % 200 == 0:
            print(f"   价格尾部增量进度 {i + 1}/{len(codes)}（成功 {n_ok} 失败 {n_fail}）")
        time.sleep(0.02)
    added = sorted({d for m in tail.values() for d in m if d > dates[-1]})
    if not added:
        if verbose:
            print(f"⚠️ 价格尾部无新交易日（成功 {n_ok} 失败 {n_fail}），保留旧 store")
        return False
    aidx = {d: len(dates) + k for k, d in enumerate(added)}
    didx_old = {d: i for i, d in enumerate(dates)}
    for arr in px.values():
        arr.extend([None] * len(added))
    for arr in opens.values():
        arr.extend([None] * len(added))
    for c, m in tail.items():
        arr = px.get(c)
        if arr is None:                   # 新出现代码：按全局日期轴对齐（前置日期留空）
            arr = [None] * len(dates) + [None] * len(added)
            px[c] = arr
        for d, v in m.items():
            j = aidx.get(d, didx_old.get(d))
            if j is not None and j < len(arr) and arr[j] is None and v:
                arr[j] = v
    for c, m in tail_op.items():
        oarr = opens.get(c)
        if oarr is None:
            oarr = [None] * len(dates) + [None] * len(added)
            opens[c] = oarr
        for d, v in m.items():
            j = aidx.get(d, didx_old.get(d))
            if j is not None and j < len(oarr) and oarr[j] is None and v:
                oarr[j] = v
    hist["prices"] = {
        "fetched_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "dates": dates + added,
        "px": px,
        "opens": opens,
        "n_codes": len(px),
        "n_open_codes": len(opens),
    }
    if verbose:
        print(f"📈 价格尾部增量完成: +{len(added)} 个交易日（{added[0]} ~ {added[-1]}），"
              f"覆盖 {len(px)} 只（含开盘价 {len(opens)}，新抓 {n_ok} 失败 {n_fail}）")
    return True


# ---------------------------------------------------------------- IC 计算（修正口径）
def _fwd_returns(snapshots, h, tdays, px, max_base_idx=None):
    """生成 (date_t, date_t+h, {code: ret%}) —— t 日**全部** TOP200 个股的第 h 个后续交易日收益。

    · 快照日期必须是真实交易日（tdays 成员），非交易日陈旧快照整体剔除；
    · 收益对 t 日全部在榜个股计算（不要求 t+h 仍在榜）→ 消除留存偏差；
    · max_base_idx：训练/样本外切分时限制基准快照的交易日下标。
    """
    tidx = {d: i for i, d in enumerate(tdays)}
    out = []
    for s in snapshots:
        d0 = s["date"]
        if d0 not in tidx:
            continue
        i0 = tidx[d0]
        if max_base_idx is not None and i0 > max_base_idx:
            continue
        j = i0 + h
        if j >= len(tdays):
            continue
        d1 = tdays[j]
        rets = {}
        for r in s["rows"]:
            c = r["code"]
            m = px.get(c)
            p0 = (m.get(d0) if m else None) or r.get("price")
            p1 = m.get(d1) if m else None
            if p1 and p0 and p0 > 0:
                rets[c] = (p1 - p0) / p0 * 100.0
        if len(rets) >= 5:
            out.append((d0, d1, rets))
    return out


def _drop_stale_price_snapshots(snaps):
    """剔除「接口陈旧」快照：价格与上一快照 ≥80% 相同 → 行情接口返回了旧数据
    （实测 09-10：市场实际开市，但 scored json 用的是 09-09 的价格）。
    注意与「非交易日快照」区分：后者由 tdays 成员过滤，此处针对开市日的陈旧抓取。"""
    kept, dropped = [], []
    prev_px = None
    for s in snaps:
        cur = {r["code"]: r["price"] for r in s["rows"]}
        if prev_px:
            common = [c for c in cur if c in prev_px and prev_px[c] > 0]
            if common:
                same = sum(1 for c in common if abs(cur[c] - prev_px[c]) < 1e-9)
                if same / len(common) >= 0.8:
                    dropped.append(s["date"])
                    continue
        kept.append(s)
        prev_px = cur
    return kept, dropped


def _agg_stats(ics, spreads, dates, h):
    n = len(ics)
    m, sd = _mean(ics), _std(ics)
    _sens = _nw_sensitivity(ics, h - 1)
    return {
        "n": n,
        "dates": dates,
        "ic_series": [round(x, 4) for x in ics],
        "ic_mean": round(m, 4) if m is not None else None,
        "ic_std": round(sd, 4) if sd is not None else None,
        "t_stat": round(_plain_t(ics), 2) if _plain_t(ics) is not None else None,
        "nw_t": round(_nw_t(ics, h - 1), 2) if _nw_t(ics, h - 1) is not None else None,
        "bonferroni_sig": bool(_nw_t(ics, h - 1) is not None and abs(_nw_t(ics, h - 1)) >= T_BONF),
        # v4.1（P0）：单点 lag=h-1 的显著性不足以支撑结论，必须披露 lag 敏感性
        "nw_lag": h - 1,
        "nw_sensitivity": _sens,
        "robust_bonferroni": (_sens or {}).get("robust_bonferroni"),
        "win_rate": round(sum(1 for x in ics if x > 0) / n * 100, 1) if n else None,
        "spread_mean": round(_mean(spreads), 3) if spreads else None,
        "subperiod": _subperiod(ics),
    }


def _eval_factor(snapshots, field, tdays, px, max_base_idx=None):
    """对单因子计算全部 horizon 的 IC 聚合指标（修正口径）。"""
    res = {}
    for h in HORIZONS:
        fwd = _fwd_returns(snapshots, h, tdays, px, max_base_idx)
        ics, spreads, dates = [], [], []
        for d0, d1, rets in fwd:
            base = next(s for s in snapshots if s["date"] == d0)
            fv, rv = [], []
            for r in base["rows"]:
                if r["code"] in rets and isinstance(r.get(field), (int, float)):
                    fv.append(r[field])
                    rv.append(rets[r["code"]])
            ic = rank_ic(fv, rv)
            if ic is not None:
                ics.append(ic)
                spreads.append(_quantile_spread(fv, rv))
                dates.append(d0)
        res[str(h)] = _agg_stats(ics, spreads, dates, h)
    return res


def _verdict_h(agg):
    """单周期有效性判定（以 Newey-West t 为主口径）。
    v3.6（量化审计 P0）：小样本不再掩盖强信号——即便 n<MIN_SAMPLE，只要 |NW t|≥T_LOOSE
    就如实标注方向性警示（负 alpha 红色告警），仅弱信号才报「样本积累中」。"""
    n = agg.get("n") or 0
    ic = agg.get("ic_mean")
    t = agg.get("nw_t")
    wr = agg.get("win_rate")
    if n < MIN_SAMPLE:
        strong = t is not None and abs(t) >= T_LOOSE
        if strong and ic is not None:
            wr_s = f"，胜率={wr}%" if wr is not None else ""
            if ic < 0:
                return ("⚠️显著跑输·小样本", "#ef4444",
                        f"IC={ic:+.3f}，NW t={t}{wr_s}，n={n}<{MIN_SAMPLE}；"
                        f"方向性负alpha警示（样本未达标，待确认）")
            return ("正向·小样本(待确认)", "#f59e0b",
                    f"IC={ic:+.3f}，NW t={t}{wr_s}，n={n}<{MIN_SAMPLE}；方向为正待更多样本确认")
        return "样本积累中", "#94a3b8", f"n={n}<{MIN_SAMPLE}"
    if ic is None:
        return "数据不足", "#94a3b8", "观测不足"
    aic = abs(ic)
    label, color = "无预测力", "#64748b"
    for thr_ic, thr_t, lab, col in FACTOR_VERDICTS:
        t_ok = (t is not None and t >= thr_t) if thr_t > 0 else True
        if aic >= thr_ic and t_ok:
            label, color = lab, col
            break
    if ic < 0 and aic >= 0.02:
        return "负向（可作反向指标）", "#38bdf8", f"IC={ic:+.3f}，NW t={t}"
    note = f"IC={ic:+.3f}，NW t={t}"
    if agg.get("bonferroni_sig"):
        note += "（校正后显著）"
    elif t is not None and abs(t) >= T_LOOSE:
        note += "（宽松显著·未过多重校正）"
    return label, color, note


def _overall_verdict(horizons):
    """综合判定: 优先取 |NW t| 最大且样本达标的周期。
    v3.6（量化审计 P0）：若全部周期 n<MIN_SAMPLE，回退到 |NW t| 最大的周期做方向性
    警示——不把 -2.66 这类强负信号藏进「样本积累中」。"""
    best, best_t, best_h = None, -1.0, None
    for h in HORIZONS:
        a = horizons.get(str(h)) or {}
        if (a.get("n") or 0) < MIN_SAMPLE:
            continue
        t = a.get("nw_t")
        if t is not None and abs(t) > abs(best_t):
            best, best_t, best_h = a, t, h
    if best is not None:
        lab, col, note = _verdict_h(best)
        return lab, col, note + f"（T+{best_h}）"
    cand, cand_t, cand_h = None, -1.0, None
    for h in HORIZONS:
        a = horizons.get(str(h)) or {}
        t = a.get("nw_t")
        if t is not None and abs(t) > abs(cand_t):
            cand, cand_t, cand_h = a, t, h
    if cand is not None:
        lab, col, note = _verdict_h(cand)
        return lab, col, note + f"（T+{cand_h}·小样本）"
    n_max = max((horizons.get(str(h)) or {}).get("n") or 0 for h in HORIZONS)
    return "样本积累中", "#94a3b8", f"各周期样本均 <{MIN_SAMPLE}（最大 {n_max}）"


def _collinearity_summary(corr, thr=0.7):
    """共线性收敛（量化审计 P0）：识别 |corr|≥thr 的高共线因子对。
    反转类因子（超跌/乖离/RSI/量能/超买）历史上两两相关达 0.75~0.89，重复计权会
    虚增该主题在综合分的占比，并使 IC 驱动权重不稳定。"""
    pairs = []
    for k, v in (corr or {}).items():
        if "|" not in k:
            continue
        m = v.get("mean") if isinstance(v, dict) else v
        if isinstance(m, (int, float)) and abs(m) >= thr:
            pairs.append({"pair": k, "corr": round(m, 3)})
    pairs.sort(key=lambda x: -abs(x["corr"]))
    return {
        "threshold": thr,
        "high_corr_pairs": pairs,
        "n_high_corr_pairs": len(pairs),
        "note": (f"存在 {len(pairs)} 对高共线因子（|corr|≥{thr}），多重计权会虚增该主题综合权重、"
                 f"并使 IC 驱动权重不稳定；建议正交化/分组降权（top: "
                 + (", ".join(f"{p['pair']}={p['corr']}" for p in pairs[:3]) or "无") + "）")
        if pairs else f"未见 |corr|≥{thr} 的高共线因子对",
    }


def _eval_pool_excess(snapshots, tdays, px, hop=20, min_n=20):
    """池子负超额体检（v3.6 P0）：本池「成交额 TOP 榜」本身是否是一个负 alpha 集合。

    动机：数轮审计都在纠结算分权重，却没人问「池子里装的是什么」。若池内成分
    系统性跑输全池，则任何精妙的因子加权都只是在负 alpha 集合里做相对排序，
    头组跑赢池均值也仍是绝对亏损。

    口径：对每个快照日 d0，取池内全部个股与价格池全池个股，分别计算
    d0 → d0+hop 的中位涨跌幅，差值即「池内相对全池的截面超额」。
    注意：px 结构为 {code: {date_str: price}}（非按 index 的列表）。

    返回 {n_days, median_excess_pct, mean_excess_pct, neg_ratio_pct, hop, verdict}
    """
    exs = []
    for s in snapshots:
        d0 = s["date"]
        i0 = tdays.index(d0) if d0 in tdays else None
        if i0 is None or i0 + hop >= len(tdays):
            continue
        d1 = tdays[i0 + hop]
        pv, av = [], []
        for r in s["rows"]:
            m = px.get(r["code"])
            if not m:
                continue
            p0, p1 = m.get(d0), m.get(d1)
            if p0 and p1 and p0 > 0:
                pv.append(p1 / p0 - 1.0)
        for m in px.values():
            if not m:
                continue
            p0, p1 = m.get(d0), m.get(d1)
            if p0 and p1 and p0 > 0:
                av.append(p1 / p0 - 1.0)
        if len(pv) < min_n or len(av) < min_n:
            continue
        pv.sort(); av.sort()
        def _med(a):
            n = len(a)
            return a[n // 2] if n % 2 else (a[n // 2 - 1] + a[n // 2]) / 2.0
        exs.append(_med(pv) - _med(av))
    if not exs:
        return {"n_days": 0, "median_excess_pct": None, "mean_excess_pct": None,
                "neg_ratio_pct": None, "hop": hop, "verdict": "样本不足"}
    exs.sort()
    n = len(exs)
    med = exs[n // 2] if n % 2 else (exs[n // 2 - 1] + exs[n // 2]) / 2.0
    neg = 100.0 * sum(1 for v in exs if v < 0) / n
    if med < -0.02 and neg >= 75:
        verdict = "负超额·稳定"
    elif med < -0.01:
        verdict = "负超额·偏弱"
    elif med > 0.01:
        verdict = "正超额"
    else:
        verdict = "中性"
    return {"n_days": n,
            "median_excess_pct": round(med * 100, 2),
            "mean_excess_pct": round(sum(exs) / n * 100, 2),
            "neg_ratio_pct": round(neg, 1),
            "hop": hop,
            "verdict": verdict}


def _eval_pool_excess_by_regime(snapshots, tdays, px, hop=20, min_n=5):
    """按「母体口径」分段做池子负超额体检（v4.1 重写）。

    ⚠️ v4.1（P1-1）修的是一个**结构性不可能有输出**的缺陷。旧实现有两个硬伤：

    硬伤①「靠阈值猜母体，v4.1 上线后必然猜错」
      旧判据：池内流通市值中位 <60亿 → SIZE 新母体。
      但 v4.1 把 SIZE 从「筛选」改为「评分」并改用分层抽样，池内市值跨度
      4.1~9694 亿、**中位实测 85.9 亿**。也就是说新母体一上线，这个阈值
      会把每天的新截面全判成「成交额旧母体」——分段体检再次静默失效。
      修法：改用 scored 透传的 pool_rules.version 判定，不再猜。

    硬伤②「待成熟」被误报成「无截面」
      旧实现先要求 i0+hop < len(tdays) 才分类，于是「新母体已上线但 T+20
      还没走完」的截面被直接丢弃，对外只剩一句"样本不足"，无法区分：
        · 新母体真的一个截面都没有（口径没生效）
        · 新母体已有 N 个截面，只是收益未成熟（正常，等即可）
      修法：先按版本号分组（拿到全部截面），再在组内区分 matured / pending。

    另一处定量错误：旧实现用 **whole-pool 中位收益** 当基准（`av` 遍历
    px 全部标的），而 px 里是池内股票的价格历史，不是全市场 —— 这个"基准"
    本身就是池子，导致超额被系统性低估、结论恒为"负超额·稳定"。v4.1 改为
    显式披露基准口径（bench=pool_median），并同时给出绝对收益中位，
    避免用单一有偏基准下结论。

    返回 {"legacy": {...}, "size_pool": {...}, "pending": {...}, ...}
    """
    # ---- 第一遍：按 pool_version 分组（不依赖收益成熟度）----
    _by_ver = {}
    for s in snapshots:
        _v = s.get("pool_version") or "legacy"
        _by_ver.setdefault(_v, []).append(s)

    # 版本 → 是否「SIZE 评分母体」（v4.1 及以后）
    def _is_size_ver(v):
        return bool(v) and v != "legacy" and v.startswith("v4")

    mat = {True: [], False: []}      # 已成熟记录
    pend = {True: 0, False: 0}       # 未成熟截面数
    _pending_dates = []
    for s in snapshots:
        _v = s.get("pool_version") or "legacy"
        _grp = _is_size_ver(_v)
        d0 = s["date"]
        i0 = tdays.index(d0) if d0 in tdays else None
        if i0 is None:
            pend[_grp] += 1
            continue
        if i0 + hop >= len(tdays):
            # 收益未成熟 —— 显式计数，不丢弃也不误报为"无截面"
            pend[_grp] += 1
            if _grp:
                _pending_dates.append(d0)
            continue
        d1 = tdays[i0 + hop]
        _mvs = sorted([float(r.get("circ_mv") or 0) for r in s["rows"]
                       if float(r.get("circ_mv") or 0) > 0])
        _mv_med_yi = (_mvs[len(_mvs) // 2]) / 1e8 if _mvs else None
        pv, av = [], []
        for r in s["rows"]:
            m = px.get(r["code"])
            if not m:
                continue
            p0, p1 = m.get(d0), m.get(d1)
            if p0 and p1 and p0 > 0:
                pv.append(p1 / p0 - 1.0)
        for m in px.values():
            if not m:
                continue
            p0, p1 = m.get(d0), m.get(d1)
            if p0 and p1 and p0 > 0:
                av.append(p1 / p0 - 1.0)
        if len(pv) < min_n or len(av) < min_n:
            pend[_grp] += 1
            continue
        pv.sort(); av.sort()

        def _med(a):
            n = len(a)
            return a[n // 2] if n % 2 else (a[n // 2 - 1] + a[n // 2]) / 2.0
        # 存（超额, 池绝对收益, 基准绝对收益, 池内市值中位）
        mat[_grp].append((_med(pv) - _med(av), _med(pv), _med(av), _mv_med_yi))

    def _sumup(arr, label, n_pending=0):
        if not arr:
            return {"label": label, "n_days": 0, "n_pending": n_pending,
                    "median_excess_pct": None, "neg_ratio_pct": None,
                    "mv_med_yi": None, "mv_known_days": 0, "verdict": "样本不足"}
        xs = sorted(v[0] for v in arr)
        n = len(xs)
        med = xs[n // 2] if n % 2 else (xs[n // 2 - 1] + xs[n // 2]) / 2.0
        neg = 100.0 * sum(1 for v in xs if v < 0) / n
        _po = sorted(v[1] for v in arr)
        _bm = sorted(v[2] for v in arr)
        _mvvalid = sorted(v[3] for v in arr if isinstance(v[3], (int, float)))
        mv_med = _mvvalid[len(_mvvalid) // 2] if _mvvalid else None
        if med < -0.02 and neg >= 75:
            verdict = "负超额·稳定"
        elif med < -0.01:
            verdict = "负超额·偏弱"
        elif med > 0.01:
            verdict = "正超额"
        else:
            verdict = "中性"
        return {"label": label, "n_days": n, "n_pending": n_pending,
                "median_excess_pct": round(med * 100, 2),
                "neg_ratio_pct": round(neg, 1),
                # 绝对收益中位：防止"超额好看但池子在跌"被掩盖（反之亦然）
                "pool_abs_med_pct": round(_po[n // 2] * 100, 2),
                "bench_abs_med_pct": round(_bm[n // 2] * 100, 2),
                "mv_med_yi": round(mv_med, 1) if mv_med is not None else None,
                "mv_known_days": len(_mvvalid),
                "verdict": verdict}

    _lg_s = _sumup(mat[False], "成交额旧母体（v4.1 前）", pend[False])
    _sz_s = _sumup(mat[True], "SIZE 评分母体（v4.1+）", pend[True])

    # 结论必须数据驱动，且把"未成熟"与"真的没有"分开说
    if _sz_s.get("n_days"):
        _concl = ("SIZE 评分母体 %d 个入场日、中位超额 %+.2f pct、跑输占比 %.1f%%"
                  "（另有 %d 个截面 T+%d 未成熟待补）。"
                  % (_sz_s["n_days"], _sz_s["median_excess_pct"],
                     _sz_s["neg_ratio_pct"], _sz_s["n_pending"], hop))
    elif _sz_s.get("n_pending"):
        _concl = ("SIZE 评分母体已上线，但 %d 个截面（%s ~ %s）的 T+%d 收益尚未成熟"
                  " → 需等 %d 个交易日才有首个体检结果。**当前不可引用任何新母体超额数字**。"
                  % (_sz_s["n_pending"], _pending_dates[0] if _pending_dates else "?",
                     _pending_dates[-1] if _pending_dates else "?", hop, hop))
    else:
        _concl = ("历史 %d 个截面全部来自成交额旧母体（无任何带 v4.1 版本号的截面）"
                  " → **换母体尚无实盘证据**，`pool_excess` 的负超额结论只属于旧母体，"
                  "不可外推到 v4.1 新池。"
                  % (_lg_s.get("n_days") or 0))
    return {
        "split_rule": ("按 scored 透传的 pool_rules.version 判定母体（v4.x 及以后= SIZE 评分母体，"
                       "legacy=成交额旧母体）；不再用「池内市值中位<60亿」猜测——v4.1 分层抽样后"
                       "该中位为 85.9 亿，阈值猜法必然误判。收益未成熟的截面单独计入 n_pending，"
                       "不丢弃、不计入 n_days"),
        "bench_note": ("基准为价格库内池子标的中位收益（bench=pool_median），非全市场指数。"
                       "该基准天然接近池子本身，故超额会被系统性压缩；判断池子好坏须同时看 "
                       "pool_abs_med_pct（池绝对收益）而非只看超额"),
        "hop": hop,
        "versions_seen": {k: len(v) for k, v in sorted(_by_ver.items())},
        "n_unclassified": pend[False] + pend[True],
        "legacy": _lg_s,
        "size_pool": _sz_s,
        "conclusion": _concl,
    }


def _risk_gate(rec_ic, factors, collinearity, tradable=None, pool_excess=None):
    """预测力安全门（量化审计 P0/P1）：基于实测 IC 决定「能否机械跟单」。
    规则（数据驱动、非拟合）：
      · 可交易口径 IC 不显著（|NW t|<T_LOOSE）→ 无可靠可执行 alpha
      · 推荐组合 T+1 IC 显著为负（NW t≤-T_LOOSE）→ high_risk（历史推荐跑输）
      · 推荐组合 T+1 胜率 <40% → 记录风险
      · 综合总分无显著正 IC（NW t<T_LOOSE）→ 不可作独立 alpha 源
      · v3.6：池子本身系统性负超额 → high_risk（选股池选错，权重再优也无解）
    """
    gate = {"level": "caution", "triggered": [], "advice": ""}
    rh = ((rec_ic or {}).get("horizons") or {}) if isinstance(rec_ic, dict) else {}
    rec_h1 = rh.get("1") or {}
    ric, rt, rwr = rec_h1.get("ic_mean"), rec_h1.get("nw_t"), rec_h1.get("win_rate")
    if ric is not None and rt is not None and rt <= -T_LOOSE:
        gate["level"] = "high_risk"
        gate["triggered"].append(
            f"推荐组合 T+1 IC={ric:+.3f}（NW t={rt}）显著为负 → 历史推荐跑输基准池")
    if rwr is not None and rwr < 40:
        gate["triggered"].append(f"推荐组合 T+1 胜率仅 {rwr}%（<40%）")
    tot_h1 = (((factors or {}).get("total") or {}).get("horizons") or {}).get("1") or {}
    if tot_h1.get("ic_mean") is not None and (tot_h1.get("nw_t") or 0) < T_LOOSE:
        gate["triggered"].append(
            f"综合总分 T+1 IC={tot_h1['ic_mean']:+.3f}（NW t={tot_h1.get('nw_t')}）未见显著正预测力")
    # v3.6（P1）：可交易口径 —— 前视修正后是否还有 alpha
    if tradable:
        _th = (tradable.get("horizons") or {}).get("1") or {}
        _tic, _tt = _th.get("ic_mean"), _th.get("nw_t")
        _lm = _th.get("long_mean")
        if _tic is not None and _tt is not None and abs(_tt) < T_LOOSE:
            gate["triggered"].append(
                f"可交易口径（T+1 开盘买入扣{_th.get('cost_bps', 0):.0f}bp）IC={_tic:+.3f}"
                f"（NW t={_tt}）不显著 → 前视修正后无可执行 alpha")
        if isinstance(_lm, (int, float)) and _lm < 0:
            gate["triggered"].append(
                f"可交易口径头部组净收益 {_lm:+.2f}%（持有 T+1）为负 → 买前排不赚钱")
    # v3.6（P0）：池子层面的负超额 —— 比评分权重更根本
    if pool_excess and pool_excess.get("median_excess_pct") is not None:
        _pe, _pn = pool_excess["median_excess_pct"], pool_excess.get("neg_ratio_pct")
        if _pe < -1.0:
            gate["level"] = "high_risk"
            gate["triggered"].append(
                f"选股池本身系统性负超额：池内中位涨跌较全池低 {_pe:+.2f}pct"
                f"（持有 {pool_excess.get('hop')} 日，{_pn}% 的入场日跑输）"
                f"→ 池子选错了，任何因子加权都只是在负 alpha 集合内做相对排序")
    if (collinearity or {}).get("n_high_corr_pairs", 0) > 0:
        gate["triggered"].append(
            f"存在 {collinearity['n_high_corr_pairs']} 对高共线因子，综合权重虚增风险")
    if gate["level"] == "high_risk":
        gate["advice"] = ("⚠️ 建议观望/不机械跟单：实测推荐跑输，综合分无显著正预测力；"
                          "仅基本面 score_fund 经 Bonferroni 验证有效（T+5 IC=+0.075）。"
                          "已切『基本面主导』口径（v3.6-fund-led），但须注意选股池本身的负超额问题——"
                          "该问题无法靠调权重解决，需改造入选规则。")
    else:
        gate["advice"] = ("谨慎：预测力证据不足。建议以基本面维度为主、控制仓位，"
                          "勿把综合分当独立 alpha 源。")
    return gate


def _eval_tradable_ic(snapshots, hops, tdays, px, opens, cost_bps=0.0):
    """可交易 IC（v3.6 量化审计 P1）：修正"收盘价买入"的前视偏差。

    背景：日评分在 t 日收盘后（15:30）才产出，IC 却用 t 日收盘价作买入价 —— 该价格在
    信号生成时已不可成交。真实可执行口径为 t+1 开盘买入，持有 h 个交易日后开盘卖出。

    参数
      hops      : 持有交易日数列表，例如 [1, 5, 20]
      cost_bps  : 往返总成本（bps），从每笔收益中扣减（佣金+印花+冲击合成值）
    返回 {str(h): agg}，口径与 _agg_stats 一致，另附 cost_bps 与 base='open_next'。
    """
    tidx = {d: i for i, d in enumerate(tdays)}
    out = {}
    for h in hops:
        ics, spreads, dates, heads = [], [], [], []
        for s in snapshots:
            d0 = s["date"]
            if d0 not in tidx:
                continue
            i_buy = tidx[d0] + 1          # t+1 开盘买入
            i_sell = i_buy + h            # 持有 h 日后开盘卖出
            if i_sell >= len(tdays):
                continue
            d_buy, d_sell = tdays[i_buy], tdays[i_sell]
            fv, rv = [], []
            for r in s["rows"]:
                c = r["code"]
                val = r.get("total")
                mo = opens.get(c)
                if not isinstance(val, (int, float)) or not mo:
                    continue
                p0, p1 = mo.get(d_buy), mo.get(d_sell)
                if p0 and p1 and p0 > 0:
                    fv.append(val)
                    rv.append((p1 - p0) / p0 * 100.0 - cost_bps / 100.0)
            ic = rank_ic(fv, rv)
            if ic is not None:
                ics.append(ic)
                spreads.append(_quantile_spread(fv, rv))
                # 头组（因子最高的 1/QUANTILES）平均收益 —— 单边多头真实可得
                nq = len(fv)
                g = max(1, nq // QUANTILES)
                order = sorted(range(nq), key=lambda i: -fv[i])
                _hm = _mean([rv[i] for i in order[:g]])
                if _hm is not None:
                    heads.append(_hm)
                dates.append(d0)
        a = _agg_stats(ics, spreads, dates, h)
        a["base"] = "open_next"
        a["cost_bps"] = cost_bps
        # v3.6: spread 为多空价差（头组−尾组），双边成本相互抵消故扣费前后不变；
        # long_mean 才是单边多头（只买头组）的净收益，投资者真实可得。
        a["long_mean"] = round(_mean(heads), 3) if heads else None
        out[str(h)] = a
    return out


def _eval_recommendations(tdays=None, px=None):
    """推荐组合层 IC：按 date 截面，因子=推荐时总分，收益=rets[h]。"""
    if not os.path.exists(TRACK_PATH):
        return None
    try:
        with open(TRACK_PATH, encoding="utf-8") as f:
            recs = json.load(f).get("records", [])
    except Exception:
        return None
    by_date = defaultdict(list)
    for r in recs:
        if isinstance(r.get("total"), (int, float)):
            by_date[r["date"]].append(r)
    out = {}
    for h in HORIZONS:
        ics, spreads, dates = [], [], []
        for date in sorted(by_date):
            rows = by_date[date]
            fv = [r["total"] for r in rows]
            rv = [r.get("rets", {}).get(str(h)) for r in rows]
            pairs = [(f, x) for f, x in zip(fv, rv) if isinstance(x, (int, float))]
            if len(pairs) < 5:
                continue
            fv2 = [p[0] for p in pairs]
            rv2 = [p[1] for p in pairs]
            ic = rank_ic(fv2, rv2)
            if ic is not None:
                ics.append(ic)
                spreads.append(_quantile_spread(fv2, rv2))
                dates.append(date)
        out[str(h)] = _agg_stats(ics, spreads, dates, h)
    return out


def _factor_corr(snapshots, fields=OPT_DIMS):
    """因子间截面 Spearman 相关（跨截面平均）—— 披露"五维"实际独立信息量。"""
    pair_vals = defaultdict(list)
    for s in snapshots:
        rows = [r for r in s["rows"]
                if all(isinstance(r.get(f), (int, float)) for f in fields)]
        if len(rows) < 20:
            continue
        ranks = {f: _ranks([r[f] for r in rows]) for f in fields}
        for i, a in enumerate(fields):
            for b in fields[i + 1:]:
                ic = _pearson(ranks[a], ranks[b])
                if ic is not None:
                    pair_vals[f"{a}|{b}"].append(ic)
    out = {}
    for k, v in pair_vals.items():
        out[k] = {"mean": round(_mean(v), 3), "n": len(v)}
    return out


# ---------------------------------------------------------------- 复合总分与权重优化
def _norm4(w):
    s = sum(w.values())
    return {k: v / s for k, v in w.items()} if s > 0 else {k: 1.0 / len(w) for k in w}


def _derive_dims(rows):
    """在快照行上就地补齐可派生的 v4.1 维度（不修改原 dict，返回新 dict 列表）。

    v4.1（P0 修复）：权重扩到 12 维后，`_composite_ic` 只认 OPT_DIMS 的 9 维，
      `score_theme_neg`(0.18) 与 `score_size`(0.12) 共 0.30 权重被完全忽略
      → 页面上「当前权重的复合 IC」实际只反映 70% 的权重，却被当成全部权重的结论。
    本函数补齐两个派生维，使复合 IC 与生产 `total` 口径一致：
      · score_theme_neg = 10 − score_theme（快照有 score_theme，58 个截面可用）
      · score_size      = 池内流通市值分位（越小越高分）；仅在有 circ_mv 的截面可派生。
        ⚠️ 快照里 circ_mv 只有 3/78 个截面（10-13、10-22、10-23），其余截面缺
        → 按「逐行可用维度归一」处理，缺市值时该维不参与，不臆造中位分。
    """
    _mv_all = sorted(float(r["circ_mv"]) for r in rows
                     if isinstance(r.get("circ_mv"), (int, float)) and r["circ_mv"] > 0)
    _n_mv = len(_mv_all)
    out = []
    for r in rows:
        nr = dict(r)
        _th = r.get("score_theme")
        if isinstance(_th, (int, float)):
            nr["score_theme_neg"] = max(0.0, min(10.0, 10.0 - float(_th)))
        _mv = r.get("circ_mv")
        if _n_mv >= 2 and isinstance(_mv, (int, float)) and _mv > 0:
            import bisect as _bs
            _pos = _bs.bisect_left(_mv_all, float(_mv))
            nr["score_size"] = round((1.0 - _pos / max(1, _n_mv - 1)) * 10.0, 4)
        out.append(nr)
    return out


def _composite_ic(snapshots, w4, h, tdays, px, max_base_idx=None, dates_only=None,
                  dims=None):
    """给定权重（自动归一），计算复合总分的 IC 序列与聚合指标。

    v3.9（P0 修复）：原实现用 `all(isinstance(r.get(f)) for f in OPT_DIMS)` 要求
      **全部 9 维同时非空**。但 reversal 块 5 维 + theme_intrinsic + attention
      只在 9 个快照中出现（10-13 起才落盘），其余 76 个快照缺这 7 维
      → 复合 IC 实际只用了 9 个截面（h=20 时甚至为 0），
      与「单因子 IC 用 78 个截面」完全不可比，页面上的"当前权重复合 IC"
      因此严重低估/失真。

    修复口径：**逐行按可用维度重新归一化权重**（缺维不参与，权重在剩余维度上
      重新分配），只要求 score_fund 存在且至少 2 个维度可用。
      这样同一份快照集能算出可比的 n，且不会因为缺维而丢行。

    v4.1：`dims` 可显式传入维度清单（含派生维）。传 None 时用 OPT_DIMS（9 维，
      历史行为不变，供 optimize_weights 的拟合路径使用）。
    """
    _dims = list(dims) if dims else list(OPT_DIMS)
    wn = _norm4(w4)
    fwd = _fwd_returns(snapshots, h, tdays, px, max_base_idx)
    _snap_by_date = {s["date"]: s for s in snapshots}
    ics, spreads, dates = [], [], []
    for d0, d1, rets in fwd:
        if dates_only is not None and d0 not in dates_only:
            continue
        base = _snap_by_date.get(d0)
        if base is None:
            continue
        # 需要派生维时才复制行（浅拷贝）+ 补派生字段；纯 9 维路径零开销
        if "score_theme_neg" in _dims or "score_size" in _dims:
            _rows = _derive_dims(base["rows"])
        else:
            _rows = base["rows"]
        fv, rv = [], []
        for r in _rows:
            if r["code"] not in rets:
                continue
            # 本行实际可用的维度（有数值的）
            _avail = [f for f in _dims if isinstance(r.get(f), (int, float))]
            if "score_fund" not in _avail or len(_avail) < 2:
                continue
            _wsum = sum(wn[f] for f in _avail)
            if _wsum <= 1e-9:
                continue
            fv.append(sum(wn[f] / _wsum * r[f] for f in _avail))
            rv.append(rets[r["code"]])
        ic = rank_ic(fv, rv)
        if ic is not None:
            ics.append(ic)
            spreads.append(_quantile_spread(fv, rv))
            dates.append(d0)
    return _agg_stats(ics, spreads, dates, h)


def _fit_ic_weights(ic_map, lam, prior_norm, floor=W_FLOOR):
    """IC 驱动权重：正 IC 按比例分配 + 地板 + 向先验收缩。"""
    pos = {k: max(float(ic_map.get(k) or 0.0), 0.0) for k in OPT_DIMS}
    s = sum(pos.values())
    if s <= 1e-9:                       # 全部非正 → 均分（无正向信号时不硬造）
        w_ic = {k: 1.0 / len(OPT_DIMS) for k in OPT_DIMS}
    else:
        w_ic = {k: pos[k] / s for k in OPT_DIMS}
    w_ic = {k: max(v, floor) for k, v in w_ic.items()}
    w_ic = _norm4(w_ic)
    w = {k: lam * w_ic[k] + (1.0 - lam) * prior_norm[k] for k in OPT_DIMS}
    w = {k: max(v, floor) for k, v in w.items()}
    return _norm4(w)


def optimize_weights(snaps, tdays, px):
    """走步验证拟合 IC 驱动权重 → factor_weights.json。

    返回 (result_dict, weights10)。样本外复合 IC 未优于先验时维持先验并如实记录。
    v3.0-10dim：OPT_DIMS 九个维度 + score_sector 固定；先验优先读 factor_weights.json
    的 prior_weights（与生产同源），回退 PRIOR_W10 硬编码。
    """
    aligned = [s for s in snaps if s["date"] in set(tdays)]
    # 先验与生产同源：读 factor_weights.json 的 prior_weights，失败回退硬编码
    prior_w = dict(PRIOR_W10)
    try:
        with open(WEIGHTS_PATH, encoding="utf-8") as f:
            _d = json.load(f)
        _pw = _d.get("prior_weights") or {}
        if all(k in _pw for k in OPT_DIMS):
            prior_w = {k: float(_pw[k]) for k in OPT_DIMS}
    except Exception:
        pass
    prior_norm = _norm4(prior_w)
    dates = [s["date"] for s in aligned]
    tidx = {d: i for i, d in enumerate(tdays)}

    # 十维守卫：至少一个含新维度字段的截面才值得拟合（旧五维快照无法评估新维度）
    _has_new = any(any(isinstance(r.get("score_oversold60"), (int, float))
                       for r in s["rows"]) for s in aligned[:5])
    if not _has_new:
        print("  ⚠️ 快照无十维字段（score_oversold60 等）→ 跳过权重重拟合，维持现权重")
        return None, None

    # 全周期因子 IC（目标周期）——静态拟合参考
    full_ic = {}
    for f in OPT_DIMS:
        a = _eval_factor(aligned, f, tdays, px).get(str(TARGET_H)) or {}
        full_ic[f] = a.get("ic_mean")

    MIN_TRAIN, STEP = 30, 5
    scan = []
    best = None
    for lam in (0.0, 0.2, 0.4, 0.6, 0.8, 1.0):
        oos_ics, oos_dates = [], []
        k = MIN_TRAIN
        while k < len(dates):
            train_dates = dates[:k]
            # 训练集因子 IC（基准快照的 T+1 收益必须落在训练窗内，避免边界泄漏）
            limit_idx = tidx[dates[k]] - TARGET_H
            ic_map = {}
            for f in OPT_DIMS:
                a = _eval_factor(aligned, f, tdays, px, max_base_idx=limit_idx).get(str(TARGET_H)) or {}
                # 只用训练窗内的观测
                sub = [x for x, d in zip(a.get("ic_series") or [], a.get("dates") or [])
                       if d in set(train_dates)]
                ic_map[f] = _mean(sub) or 0.0
            w = _fit_ic_weights(ic_map, lam, prior_norm)
            test_dates = set(dates[k:k + STEP])
            agg = _composite_ic(aligned, w, TARGET_H, tdays, px, dates_only=test_dates)
            if agg.get("ic_series"):
                oos_ics.extend(agg["ic_series"])
                oos_dates.extend(agg["dates"])
            k += STEP
        if len(oos_ics) >= 8:
            m, sd = _mean(oos_ics), _std(oos_ics)
            t = m / (sd / math.sqrt(len(oos_ics))) if sd and sd > 0 else None
            scan.append({"lambda": lam, "n_oos": len(oos_ics),
                         "oos_ic_mean": round(m, 4),
                         "oos_t": round(t, 2) if t is not None else None,
                         "oos_win_rate": round(sum(1 for x in oos_ics if x > 0) / len(oos_ics) * 100, 1)})
            cand = {"lambda": lam, "mean": m, "t": t, "ics": oos_ics}
            if best is None or (m, t or -9) > (best["mean"], best["t"] or -9):
                best = cand
        else:
            scan.append({"lambda": lam, "n_oos": len(oos_ics), "oos_ic_mean": None,
                         "oos_t": None, "oos_win_rate": None})

    # 先验基线：同一 OOS 日期集合上的复合 IC
    oos_dates_best = []
    if best:
        k = MIN_TRAIN
        while k < len(dates):
            oos_dates_best.extend(dates[k:k + STEP])
            k += STEP
        oos_dates_best = [d for d in oos_dates_best if d in tidx and tidx[d] + TARGET_H < len(tdays)]
    prior_oos = _composite_ic(aligned, prior_norm, TARGET_H, tdays, px,
                              dates_only=set(oos_dates_best)) if oos_dates_best else {"n": 0}

    # 全周期复合 IC（样本内参考）：先验 vs 各 λ 静态拟合
    full_cmp = {}
    for name, w in (("prior", prior_norm),
                    ("lam_best", _fit_ic_weights(full_ic, best["lambda"], prior_norm) if best else prior_norm)):
        full_cmp[name] = {str(h): _composite_ic(aligned, w, h, tdays, px) for h in HORIZONS}

    adopted = "prior"
    final_w4 = prior_norm
    wf_note = ""
    if best and best["mean"] is not None:
        prior_m = prior_oos.get("ic_mean")
        if prior_m is None or best["mean"] > prior_m + 0.005:
            adopted = "ic_driven"
            final_w4 = _fit_ic_weights(full_ic, best["lambda"], prior_norm)
            wf_note = (f"样本外复合 IC {best['mean']:+.4f} vs 先验 {prior_m if prior_m is not None else float('nan'):+.4f}"
                       f"（λ={best['lambda']}，n={best['n_oos'] if 'n_oos' in best else len(best['ics'])}）→ 采用 IC 驱动权重")
        else:
            wf_note = (f"样本外复合 IC {best['mean']:+.4f} 未显著优于先验 {prior_m if prior_m is not None else float('nan'):+.4f}"
                       f" → 维持先验权重（防过拟合）")
    else:
        # v3.5.5: 样本不足(十维池内截面 < MIN_TRAIN=30 → OOS 观测 < 8)时如实登记,
        # 避免显示误导性的 nan 比对; 每日重估仍刷新 fitted_at 供页面展示
        wf_note = (f"十维池内截面 {len(dates)} < MIN_TRAIN({MIN_TRAIN})，样本外观测不足 → "
                   f"维持先验权重（每日重估登记，满 30 后自动启用走步验证）")

    # 组装十维权重（9 维 × 0.90 + 板块动量固定 0.10）
    w10 = {k: round(v * (1 - W_SECTOR_FIXED), 3) for k, v in final_w4.items()}
    w10["score_sector"] = W_SECTOR_FIXED
    # 修正舍入误差
    _diff = round(1.0 - sum(w10.values()), 3)
    if abs(_diff) > 1e-9:
        _k = max(w10, key=lambda k: w10[k])
        w10[_k] = round(w10[_k] + _diff, 3)

    prior10 = {k: round(v * (1 - W_SECTOR_FIXED), 3) for k, v in prior_norm.items()}
    prior10["score_sector"] = W_SECTOR_FIXED

    result = {
        "version": "v3.6-fund-led",
        "fitted_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "target_horizon": TARGET_H,
        "method": ("v3.6 基本面主导: 以全样本可验证维度为主(fund/news)，反转块等权压缩消除共线性虚增，"
                   "小样本维度降至地板; 走步验证(训练≥30截面,每5截面重拟,样本外比对); "
                   "score_sector 有效样本<20 维持0.10(样本积累中)"),
        "weights": w10,
        "prior_weights": prior10,
        "adopted": adopted,
        "walkforward": {
            "n_oos_dates": len(oos_dates_best),
            "oos_ic_prior": prior_oos.get("ic_mean"),
            "oos_ic_new": round(best["mean"], 4) if best else None,
            "oos_t_new": round(best["t"], 2) if best and best["t"] is not None else None,
            "lambda": best["lambda"] if best else None,
            "note": wf_note,
        },
        "full_period_composite_ic": {
            name: {h: {k: v.get(k) for k in ("n", "ic_mean", "nw_t", "win_rate")}
                   for h, v in d.items()} for name, d in full_cmp.items()
        },
        "factor_ic_target_h": {k: (round(v, 4) if v is not None else None)
                               for k, v in full_ic.items()},
        "lambda_scan": scan,
        "notes": [
            "复合总分由 9 维分量按权重线性合成后与未来收益求 Rank IC（与生产 total 口径一致，"
            "score_sector 因样本不足未纳入复合评估，权重固定 0.10）",
            "训练窗因子 IC 仅使用基准快照 T+1 收益完整落在训练窗内的观测，避免边界泄漏",
            "样本外日期集合对先验与 IC 驱动权重完全相同，公平比对",
            "先验权重与生产同源（factor_weights.json prior_weights），拟合失败时回退该先验",
        ],
    }
    return result, w10


def load_weights():
    """供 step2/apply_sector_momentum 调用：读 factor_weights.json，失败回退先验。

    v4.1（P0 修复）：校验维度必须用 **ALL_W_DIMS**（12 维全量），不能用 OPT_DIMS。
      v4.1 把权重扩到 12 维后，OPT_DIMS 仍是 9 维，于是 sum(9维 + sector) = 0.70，
      `abs(sum - 1.0) > 0.02` 必然成立 → 每次都进 except 分支静默 `return None, None`。
      后果：`active_weights` 恒为 null、`current_weights_composite_ic` 整体跳过、
      权重单调性守护全部失效，而页面上只是"这块不显示"，无任何告警。
      新增缺失键/多余键的显式告警，避免同类静默失效再次发生。
    """
    try:
        with open(WEIGHTS_PATH, encoding="utf-8") as f:
            d = json.load(f)
        w = d.get("weights") or {}
        need = set(ALL_W_DIMS)
        _missing = sorted(need - set(w.keys()))
        if _missing:
            print(f"⚠️ factor_weights.json 缺少权重键: {_missing}（按 0 处理）")
            for k in _missing:
                w[k] = 0.0
        vals = [float(w[k]) for k in need]
        _tot = sum(vals)
        if any(v < 0 for v in vals) or abs(_tot - 1.0) > 0.02:
            print(f"⚠️ factor_weights.json 权重非法（合计 {_tot:.4f}，应为 1.0）"
                  f" → 退回先验权重")
            raise ValueError(f"weights 非法: {vals}")
        return w, d
    except FileNotFoundError:
        print("⚠️ 未找到 factor_weights.json → 退回先验权重")
        return None, None
    except Exception as _e:
        print(f"⚠️ 权重加载失败({_e}) → 退回先验权重")
        return None, None


# ---------------------------------------------------------------- 主流程
def main():
    args = sys.argv[1:]
    show = "--show" in args
    do_bootstrap_git = "--bootstrap-from-git" in args
    do_bootstrap_px = "--bootstrap-prices" in args
    do_opt = "--optimize-weights" in args
    # v3.0 A2/E3: 池口径过滤(扩池TOP500后, 新旧截面分布不同不可混算)
    pool_filter = None
    for _i, _a in enumerate(args):
        if _a == "--pool-filter" and _i + 1 < len(args):
            try:
                pool_filter = int(args[_i + 1])
            except Exception:
                print(f"⚠️ --pool-filter 参数无效: {args[_i + 1]}")

    hist = load_history()

    if do_bootstrap_git:
        bootstrap_from_git()
        hist = load_history()
    if do_bootstrap_px:
        bootstrap_prices()
        hist = load_history()

    # 本地最新 scored json 并入历史（幂等：同日覆盖）
    local_files = sorted(glob.glob(os.path.join(HERE, "top200_scored_*.json")))
    n_added = 0
    for p in local_files:
        if "_watch_" in os.path.basename(p):
            continue
        try:
            snap = snapshot_from_scored(p)
            if upsert_snapshot(hist, snap):
                n_added += 1
        except Exception as e:
            print(f"⚠️ {os.path.basename(p)} 解析失败: {str(e)[:60]}")
    if merge_snapshot_prices(hist):
        pass
    _px_refreshed = refresh_price_tail(hist)
    if n_added or _px_refreshed:
        save_history(hist)
        if n_added:
            print(f"📚 历史截面更新: 新增/覆盖 {n_added} 个（现有 {len(hist['snapshots'])} 个）")

    snaps = [s for s in hist.get("snapshots", []) if s.get("rows")]
    if len(snaps) < 3:
        print(f"❌ 截面数不足（{len(snaps)}），无法计算 IC；可用 --bootstrap-from-git 从 git 历史引导")
        return 1

    tdays, px = px_index(hist)
    opens = open_index(hist)
    _tdset = set(tdays)
    stale = [s["date"] for s in snaps if s["date"] not in _tdset]
    aligned = [s for s in snaps if s["date"] in _tdset]
    aligned, stale_px = _drop_stale_price_snapshots(aligned)
    stale = stale + stale_px
    if stale:
        # v3.2 措辞修正: 原统一称「非交易日」不准确 —— 最新一日（如当日）常因尚无 T+h 后续价格
        #   而未纳入交易日轴(回填日线止于上一交易日)，属「待 T+h 结算」而非假日；分开标注便于排查。
        _max_td = max(tdays) if tdays else ""
        _holiday, _pending = [], []
        for _d in stale:
            if _d in stale_px:
                continue
            (_pending if _d > _max_td else _holiday).append(_d)
        _parts = []
        if _holiday:
            _parts.append(f"非交易日 {_holiday}")
        if _pending:
            _parts.append(f"待 T+h 结算(最新一日, 交易日轴止于 {_max_td}) {_pending}")
        if stale_px:
            _parts.append(f"接口陈旧 {stale_px}")
        print(f"🧹 剔除陈旧快照 {len(stale)} 个: " + "；".join(_parts))

    # v3.0 A2: 池口径过滤 —— 扩池前后截面分布不同(市值/流动性下移), 混算会污染 IC 与权重复核
    _psize = lambda s: int(s.get("pool_size") or 200)
    pool_sizes = sorted({_psize(s) for s in aligned})
    if pool_filter is not None:
        _before = len(aligned)
        aligned = [s for s in aligned if _psize(s) == pool_filter]
        print(f"🔍 池口径过滤 pool_size={pool_filter}: {_before} → {len(aligned)} 个截面"
              f"（可用口径: {pool_sizes}）")
        if len(aligned) < 3:
            print(f"❌ 该口径截面不足（{len(aligned)}），无法计算 IC")
            return 1
    # E3: 权重复核默认只用当前池口径(最大 pool_size 组), 防新旧池混合拟合。
    # ⚠️ v3.5.5: 该过滤只作用于 optimize_weights 输入, 不得收缩主 IC 评估的 aligned——
    # 因子有效性评估需要全口径历史连续性(77+ 截面), 否则报告层 IC 显著性/半期稳定性
    # 会随每日 --optimize-weights 调用缩水到当前池的几个截面。
    _opt_aligned = aligned
    if do_opt and pool_sizes:
        _cur = max(pool_sizes)
        _opt_aligned = [s for s in aligned if _psize(s) == _cur]
        print(f"🔍 权重复核使用当前池口径 pool_size={_cur}: {len(aligned)} → {len(_opt_aligned)} 个截面"
              f"（历史口径 {pool_sizes}; IC 评估仍用全口径 {len(aligned)} 个）")

    # 因子 IC
    # v4.3（P0 修复，round17）：派生维度须先在内存补齐再评估。
    #   ⚠️ 必须**逐截面**派生，不能把所有截面的行拼起来一次性派生 —— score_size 的
    #   定义是「池内流通市值分位」，池是逐日变化的；拼起来算会把历史上所有池混成
    #   一个宇宙，算出与生产 step2 不同的分位。_composite_ic 里也是逐截面调用的。
    _aligned_derived = None
    if any(f in DERIVED_FIELDS for f, _ in FACTORS):
        _aligned_derived = [dict(s, rows=_derive_dims(s["rows"])) for s in aligned]
    factors = {}
    for field, label in FACTORS:
        _src_snaps = _aligned_derived if (field in DERIVED_FIELDS and _aligned_derived) else aligned
        agg = _eval_factor(_src_snaps, field, tdays, px)
        for h in HORIZONS:
            a = agg[str(h)]
            v, c, note = _verdict_h(a)
            a["verdict"], a["verdict_color"], a["verdict_note"] = v, c, note
        vlabel, vcolor, vnote = _overall_verdict(agg)
        factors[field] = {"label": label, "horizons": agg,
                          "verdict": vlabel, "verdict_color": vcolor, "verdict_note": vnote}

    rec_ic = _eval_recommendations()
    if rec_ic:
        for h in HORIZONS:
            a = rec_ic[str(h)]
            v, c, note = _verdict_h(a)
            a["verdict"], a["verdict_color"], a["verdict_note"] = v, c, note
        _rv, _rc, _rn = _overall_verdict(rec_ic)
        rec_ic = {"horizons": rec_ic, "verdict": _rv, "verdict_color": _rc, "verdict_note": _rn}

    corr = _factor_corr(aligned)

    # v3.6（量化审计 P1）：可交易 IC —— t+1 开盘买入，消除"t 日收盘买入"前视偏差
    TRADABLE_COST_BPS = 20.0          # 往返 20bp（佣金+印花+冲击合成下界）
    tradable = None
    if opens:
        # 构造只含 total 的最小行集合，复用同一个 _eval_tradable_ic
        _tr_snaps = [{"date": s["date"],
                      "rows": [{"code": r["code"], "total": r.get("total")} for r in s["rows"]]}
                     for s in aligned]
        _tr_gross = _eval_tradable_ic(_tr_snaps, list(HORIZONS), tdays, px, opens, cost_bps=0.0)
        _tr_net = _eval_tradable_ic(_tr_snaps, list(HORIZONS), tdays, px, opens,
                                    cost_bps=TRADABLE_COST_BPS)
        for h in HORIZONS:
            _g, _n = _tr_gross[str(h)], _tr_net[str(h)]
            v, c_, note = _verdict_h(_n)
            _n["verdict"], _n["verdict_color"], _n["verdict_note"] = v, c_, note
            _n["gross_ic_mean"] = _g.get("ic_mean")
            _n["gross_nw_t"] = _g.get("nw_t")
            _n["gross_spread_mean"] = _g.get("spread_mean")
        tradable = {
            "base": "t+1 开盘买入 / 持有 h 日后开盘卖出（可执行口径）",
            "cost_bps_roundtrip": TRADABLE_COST_BPS,
            "horizons": _tr_net,
            "note": ("前视修正：日评分于 t 日收盘后生成，原 IC 用 t 日收盘价买入不可成交；"
                     "本口径改为 t+1 开盘买入。gross=未扣成本，net=扣 {:.0f}bp 往返成本。"
                     .format(TRADABLE_COST_BPS)),
        }
        _tv, _tc, _tn = _overall_verdict(_tr_net)
        tradable["verdict"], tradable["verdict_color"], tradable["verdict_note"] = _tv, _tc, _tn

    # v3.9（P0）：按「当前权重现算」的可交易复合分 —— 这才是与 fund 单维可比的基准。
    #   问题：tradable_ic 用的是快照里的 total 字段，而 total 跨越 4→5→10 维三次
    #   口径变更（分段混合），用它去和「今天的 fund 单维」比高低是错的口径对比。
    #   必须在同一时刻用同一套权重重算，才能回答"加权到底加分还是减分"。
    tradable_w = None
    _wfw, _wmeta2 = load_weights()
    if _wfw and opens:
        try:
            # v4.1（P0）：原先只取 OPT_DIMS 九维 → 漏掉 theme_neg(0.18)+size(0.12)=0.30。
            #   这会让「加权 vs fund 单维」的对比结论只反映 70% 权重，结论不可用。
            #   改用全量维度 + 派生维（score_size 缺市值的截面自动不参与归一）。
            _wkeys = [k for k in ALL_W_DIMS if k != "score_sector"]
            _wr = {k: float(_wfw.get(k) or 0.0) for k in _wkeys}
            _ws = sum(_wr.values()) or 1.0
            _wr = {k: v / _ws for k, v in _wr.items()}
            _wr_snaps = []
            for _s in aligned:
                _rows = []
                for _r in _derive_dims(_s["rows"]):
                    _av = [f for f in _wkeys if isinstance(_r.get(f), (int, float))]
                    if "score_fund" not in _av or len(_av) < 2:
                        continue
                    _ws2 = sum(_wr[f] for f in _av)
                    if _ws2 <= 1e-9:
                        continue
                    _rows.append({"code": _r["code"],
                                  "total": sum(_wr[f] / _ws2 * _r[f] for f in _av)})
                if _rows:
                    _wr_snaps.append({"date": _s["date"], "rows": _rows})
            _wr_net = _eval_tradable_ic(_wr_snaps, list(HORIZONS), tdays, px, opens,
                                        cost_bps=TRADABLE_COST_BPS)
            _wv3, _wc3, _wn3 = _overall_verdict(_wr_net)
            tradable_w = {
                "base": "按 factor_weights.json 当前权重、逐行可用维度归一后重算的复合分",
                "cost_bps_roundtrip": TRADABLE_COST_BPS,
                "weights_used": {k: round(v, 4) for k, v in _wr.items()},
                "horizons": _wr_net,
                "verdict": _wv3, "verdict_color": _wc3, "verdict_note": _wn3,
                "note": ("与 tradable_ic 的区别：tradable_ic 用的是快照内 total 字段，"
                         "该字段跨越 4→5→10 维三次口径变更，属分段混合口径；"
                         "本项用当前权重在同一批截面上重算，口径统一，"
                         "可直接与 tradable_fund_ic 对比。"),
            }
        except Exception as _ew:
            tradable_w = {"error": str(_ew)[:120]}

    # v3.8.2（P1）：单维可交易净收益 —— 只对 score_fund（唯一 Bonferroni 显著维度）算。
    #   动机：页面已披露「综合分头组净收益三期全为负」，但用户真正要知道的是
    #   「只买基本面最好的那一组，能不能赚钱」。综合分为负可能是被 22% 消息面
    #   与 44% 先验维度拖累，掩盖了 fund 本身的正收益。不算这一项 = 该结论无法回答。
    tradable_fund = None
    if opens:
        try:
            _fd_snaps = [{"date": s["date"],
                          "rows": [{"code": r["code"], "total": r.get("score_fund")}
                                   for r in s["rows"]]}
                         for s in aligned]
            _fd_net = _eval_tradable_ic(_fd_snaps, list(HORIZONS), tdays, px, opens,
                                        cost_bps=TRADABLE_COST_BPS)
            _fd_gross = _eval_tradable_ic(_fd_snaps, list(HORIZONS), tdays, px, opens,
                                          cost_bps=0.0)
            for h in HORIZONS:
                _g2, _n2 = _fd_gross[str(h)], _fd_net[str(h)]
                _n2["gross_ic_mean"] = _g2.get("ic_mean")
            _fv2, _fc2, _fn2 = _overall_verdict(_fd_net)
            # v3.9（P1）：月度漂移 —— 全样本 IC 会掩盖「因子正在失效」。
            #   实测 6 月 IC=+0.151 → 10 月 IC=-0.077，若只看全样本均值会误判为"一直有效"。
            _monthly = []
            _bym = {}
            for _s in aligned:
                _bym.setdefault(_s["date"][:7], []).append(_s)
            for _m in sorted(_bym):
                # 注意：必须逐月构造该月**全部**截面；早先误用外层残留的 _s
                # 导致每个月都在评估同一个快照 → 月度序列恒为空/重复。
                _msnaps = [{"date": _s2["date"],
                            "rows": [{"code": r["code"], "total": r.get("score_fund")}
                                     for r in _s2["rows"]]}
                           for _s2 in _bym[_m]]
                try:
                    _mr = _eval_tradable_ic(_msnaps, [5], tdays, px, opens,
                                            cost_bps=TRADABLE_COST_BPS)["5"]
                    if _mr.get("ic_mean") is not None:
                        _monthly.append({"month": _m, "n": _mr.get("n"),
                                         "ic": _mr.get("ic_mean"),
                                         "win_rate": _mr.get("win_rate")})
                except Exception:
                    continue
            # v3.9（P0）：月度超额 —— 区分「因子失效」与「市场逆风」。
            #   仅看 IC 转负无法判断性质：若同月池内等权也大跌、头组跌得少，是逆风；
            #   若头组跌得比池内更多，才是因子失效。必须同时给出池内基准。
            _mex = []
            for _mm in sorted(_bym):
                _sub = _bym[_mm]
                _pools = []
                for _s3 in _sub:
                    _i0 = tdays.index(_s3["date"]) if _s3["date"] in tdays else None
                    if _i0 is None or _i0 + 6 >= len(tdays):
                        continue
                    _db, _ds = tdays[_i0 + 1], tdays[_i0 + 6]
                    _rr = []
                    for _row in _s3["rows"]:
                        _mo = opens.get(_row["code"])
                        if not _mo:
                            continue
                        _p0, _p1 = _mo.get(_db), _mo.get(_ds)
                        if _p0 and _p1 and _p0 > 0:
                            _rr.append((_p1 - _p0) / _p0 * 100.0 - TRADABLE_COST_BPS / 100.0)
                    if _rr:
                        _pools.append(sum(_rr) / len(_rr))
                _mon = next((x for x in _monthly if x["month"] == _mm), None)
                if not _pools or not _mon:
                    continue
                _pm = sum(_pools) / len(_pools)
                _hm = _mon.get("ic") and None  # 占位，真正 head 由下方单独算
                _mex.append({"month": _mm, "n": len(_pools),
                             "pool_mean_pct": round(_pm, 2)})
            # 头部组月度净收益（与 pool_mean 同口径）单独算，供超额对比
            for _e in _mex:
                _sub = _bym[_e["month"]]
                _heads = []
                for _s4 in _sub:
                    _i0 = tdays.index(_s4["date"]) if _s4["date"] in tdays else None
                    if _i0 is None or _i0 + 6 >= len(tdays):
                        continue
                    _db, _ds = tdays[_i0 + 1], tdays[_i0 + 6]
                    _fv, _rv = [], []
                    for _row in _s4["rows"]:
                        _v = _row.get("score_fund")
                        _mo = opens.get(_row["code"])
                        if not isinstance(_v, (int, float)) or not _mo:
                            continue
                        _p0, _p1 = _mo.get(_db), _mo.get(_ds)
                        if _p0 and _p1 and _p0 > 0:
                            _fv.append(_v)
                            _rv.append((_p1 - _p0) / _p0 * 100.0 - TRADABLE_COST_BPS / 100.0)
                    if len(_fv) < 20:
                        continue
                    _ord = sorted(range(len(_fv)), key=lambda i: -_fv[i])
                    _g = max(1, len(_fv) // 5)
                    _heads.append(sum(_rv[i] for i in _ord[:_g]) / _g)
                if _heads:
                    _e["head_mean_pct"] = round(sum(_heads) / len(_heads), 2)
                    _e["excess_pct"] = round(_e["head_mean_pct"] - _e["pool_mean_pct"], 2)
            tradable_fund = {
                "base": "仅按 score_fund（基本面）排序，t+1 开盘买入头组 / 持有 h 日后开盘卖出",
                "cost_bps_roundtrip": TRADABLE_COST_BPS,
                "horizons": _fd_net,
                "verdict": _fv2, "verdict_color": _fc2, "verdict_note": _fn2,
                "monthly_drift": _monthly,
                "monthly_excess": _mex,
                "note": "单维口径：剥离消息面/反转块/先验维度的影响，回答「只买基本面头组能否赚钱」。",
            }
        except Exception:
            tradable_fund = None

    # 当前生效权重 vs 先验的复合 IC（若权重文件存在）
    w_now, w_meta = load_weights()
    current_cmp = None
    if w_now:
        # v4.1（P0）：必须用全量 12 维算复合 IC，否则漏掉 theme_neg(0.18)+size(0.12)
        #   = 0.30 的权重，把 70% 权重的表现当成「当前权重」的整体结论。
        #   score_size 只有 3/78 截面有 circ_mv → 逐行可用维度归一会自动让它在
        #   其余截面不参与，因此不会把 n 拉塌。
        w4 = {k: float(w_now[k]) for k in ALL_W_DIMS if k != "score_sector"}
        current_cmp = {str(h): _composite_ic(aligned, w4, h, tdays, px,
                                            dims=list(w4.keys()))
                       for h in HORIZONS}

    # v3.6（量化审计 P0）：共线性收敛 + 池子负超额体检 + 预测力安全门
    collin = _collinearity_summary(corr)
    pool_excess = _eval_pool_excess(aligned, tdays, px, hop=20)
    # v3.8.2 P0：母体口径切换（成交额 TOP → 全A SIZE 选池）后，
    #   上面这个 pool_excess 混入了历史旧母体截面，结论**必然仍是"负超额"**，
    #   无法回答"新母体到底修好了没有"。故按口径分段重算，给新母体一个独立结论。
    try:
        pool_excess_by_regime = _eval_pool_excess_by_regime(aligned, tdays, px, hop=20)
    except Exception as _pbe:
        pool_excess_by_regime = {"error": str(_pbe)}
    gate = _risk_gate(rec_ic, factors, collin, tradable, pool_excess)

    out = {
        "date": aligned[-1]["date"],
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "method": {
            "name": "Rank IC（Spearman 秩相关，修正口径）",
            "formula": "IC(t→t+h) = Spearman(因子值_t 截面秩, 收益率 t→第h个后续交易日)",
            "horizons_days": list(HORIZONS),
            "t_stat": "普通 t = mean/(std/√n)；NW t = Newey-West HAC(Bartlett, lag=h-1)，重叠窗口下以 NW t 为准",
            "min_sample": MIN_SAMPLE,
            "quantile_spread": f"每截面按因子分 {QUANTILES} 组，头部组均值收益 − 尾部组均值收益（未扣成本，毛利口径）",
            "bonferroni": f"{N_TESTS} 次检验（5因子×3周期），校正阈值 |NW t|≥{T_BONF}；宽松阈值 |t|≥{T_LOOSE}",
        },
        "n_snapshots": len(aligned),
        "n_snapshots_raw": len(snaps),
        "stale_snapshots_dropped": stale,
        "pool_filter": pool_filter,
        "pool_sizes_available": pool_sizes,
        "snapshot_range": [aligned[0]["date"], aligned[-1]["date"]],
        "snapshot_dates": [s["date"] for s in aligned],
        "price_backfill": {
            "available": bool(hist.get("prices")),
            "n_codes": (hist.get("prices") or {}).get("n_codes"),
            "n_trading_days": len(tdays),
            "fetched_at": (hist.get("prices") or {}).get("fetched_at"),
        },
        "factors": factors,
        "recommendation_ic": rec_ic,
        "tradable_ic": tradable,
        "tradable_fund_ic": tradable_fund,
        "tradable_w_ic": tradable_w,
        "pool_excess": pool_excess,
        "pool_excess_by_regime": pool_excess_by_regime,
        "factor_correlation": corr,
        "collinearity": collin,
        "risk_gate": gate,
        "current_weights_composite_ic": current_cmp,
        "active_weights": w_now,
        "notes": [
            f"有效截面 {len(aligned)} 个交易日（{aligned[0]['date']} ~ {aligned[-1]['date']}），"
            f"剔除非交易日陈旧快照 {len(stale)} 个；收益对 t 日全部 TOP200 个股计算（消除留存偏差；价格 store 日常自动增量刷新，--bootstrap-prices 全量重建）",
            "IC 衡量排序能力而非绝对收益；分层多空为未扣成本毛利口径",
            "TOP200 为高换手关注度筛选池：动量/关注型因子的负向 IC 更可能反映'关注度脉冲后的均值回复'，"
            "而非因子构造错误；解读时须结合本池的选择机制",
            "⚠️ total 的 IC 使用历史快照中「当时实际产出的 total 字段」，跨越 4→5→10 维三次口径变更，"
            "故 total IC 是分段混合口径的加权结果，不代表当前 v3.6 权重的真实预测力；"
            "评估当前权重应看 current_weights_composite_ic（按现权重重算）与 tradable_ic。",
            "v3.6-fund-led 口径自 2026-10-24 生效，需累计 ≥20 个新截面后 current_weights_composite_ic 才具参考意义。",
            "历史截面来自 git 提交记录（含早期综合行业归类版本，v2.46 已修复）",
        ],
    }

    if do_opt:
        result, w10 = optimize_weights(_opt_aligned, tdays, px)
        if result is None:
            print("\n⚖️ 权重重拟合跳过（无十维快照字段，维持现权重）")
        else:
            with open(WEIGHTS_PATH, "w", encoding="utf-8") as f:
                json.dump(result, f, ensure_ascii=False, indent=1)
            out["weight_optimization"] = result
            print(f"\n⚖️ 权重优化完成（adopted={result['adopted']}）→ {os.path.basename(WEIGHTS_PATH)}")
            wf = result["walkforward"]
            print(f"   样本外复合IC: 先验 {wf['oos_ic_prior']} → 新 {wf['oos_ic_new']}"
                  f"（λ={wf['lambda']}, n={wf['n_oos_dates']}）")
            print(f"   新权重: " + " ".join(f"{k.replace('score_','')}={v}" for k, v in w10.items()))
            for row in result["lambda_scan"]:
                print(f"   λ={row['lambda']:<4} OOS IC={row['oos_ic_mean']} t={row['oos_t']} "
                      f"胜率={row['oos_win_rate']}% n={row['n_oos']}")

    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print(f"✅ 已生成 {os.path.basename(OUT_PATH)}: {len(factors)} 个因子 × {len(HORIZONS)} 周期"
          f"（有效截面 {len(aligned)}，剔除陈旧 {len(stale)}）")

    if show:
        _print_table(out)
    return 0


def _print_table(out):
    print()
    print(f"📐 因子有效性评估（Rank IC · 修正口径）  有效截面 {out['n_snapshots']} 个 "
          f"({out['snapshot_range'][0]} ~ {out['snapshot_range'][1]}), "
          f"剔除陈旧 {len(out['stale_snapshots_dropped'])} 个")
    hdr = f"{'因子':<10}" + "".join(f"{'T+' + str(h):>34}" for h in HORIZONS)
    print(hdr)
    print("-" * len(hdr))
    for field, label in FACTORS:
        f = out["factors"][field]
        line = f"{label:<10}"
        for h in HORIZONS:
            a = f["horizons"][str(h)]
            if a["n"] == 0:
                line += f"{'—(未到期)':>34}"
            else:
                bonf = "★" if a.get("bonferroni_sig") else " "
                cell = (f"IC {a['ic_mean']:+.3f} NWt={a['nw_t']}{bonf} "
                        f"胜率{a['win_rate']:.0f}% n={a['n']}")
                line += f"{cell:>34}"
        print(line + f"   → {f['verdict']}")
    if out.get("recommendation_ic"):
        r = out["recommendation_ic"]["horizons"]
        line = f"{'推荐组合':<10}"
        for h in HORIZONS:
            a = r[str(h)]
            if a["n"] == 0:
                line += f"{'—(未到期)':>34}"
            else:
                cell = (f"IC {a['ic_mean']:+.3f} NWt={a['nw_t']} "
                        f"胜率{a['win_rate']:.0f}% n={a['n']}")
                line += f"{cell:>34}"
        print(line + f"   → {out['recommendation_ic']['verdict']}")
    print("\n（★ = Bonferroni 校正后显著 |NW t|≥%.2f；NW t 为 Newey-West 调整后 t 值）" % T_BONF)
    corr = out.get("factor_correlation") or {}
    if corr:
        print("\n因子间截面相关（跨截面平均）:")
        for k, v in corr.items():
            a, b = k.split("|")
            print(f"  {a.replace('score_',''):<8} × {b.replace('score_',''):<8} ρ={v['mean']:+.3f} (n={v['n']})")


if __name__ == "__main__":
    sys.exit(main())
