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
]
# 可优化权重的九个维度（fund/news 池内已验证；新拆 5 维先验映射待池内积累；
#   2 先验维弱证据）；score_sector 样本不足暂固定 0.10
OPT_DIMS = ["score_fund", "score_news", "score_oversold60", "score_bias60",
            "score_rsi_rev", "score_vol_pulse", "score_overbought",
            "score_theme_intrinsic", "score_attention"]
# 十维先验（与 factor_weights.json v3.0-10dim 一致；运行时会优先读该文件 prior_weights）
PRIOR_W10 = {"score_fund": 0.18, "score_news": 0.18, "score_oversold60": 0.12,
             "score_bias60": 0.10, "score_rsi_rev": 0.06, "score_vol_pulse": 0.10,
             "score_overbought": 0.06, "score_theme_intrinsic": 0.05,
             "score_attention": 0.05}   # 合计0.90，sector 固定0.10补足
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
        if len(row) > 2:
            rows.append(row)
    # v3.0 A2: 池口径留痕(扩池前后截面不可混算 IC; 旧文件无该字段 → 200)
    return {"date": str(d.get("date") or os.path.basename(path)), "rows": rows,
            "pool_size": int(d.get("pool_size") or 200)}


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
def _fetch_qfq(sym, need=PX_NEED):
    """腾讯前复权日线 → [(date, close)]；多域名轮询，WAF 页自动换域。"""
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
    px_series = {}          # code -> {date: close}
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
        kl = _fetch_qfq(sym)
        if not kl:
            # 保留旧数据（接口偶发失败不丢历史）
            if prev:
                px_series[c] = {d: v for d, v in zip(old_dates, prev) if v is not None}
                n_skip += 1
            else:
                n_fail += 1
            continue
        px_series[c] = dict(kl)
        n_ok += 1
        time.sleep(0.03)

    all_dates = sorted({d for m in px_series.values() for d in m})
    px_compact = {c: [m.get(d) for d in all_dates] for c, m in px_series.items()}
    hist["prices"] = {
        "fetched_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "dates": all_dates,
        "px": px_compact,
        "n_codes": len(px_compact),
    }
    save_history(hist)
    print(f"✅ 价格回填完成: {len(px_compact)} 只（新抓 {n_ok}，保留旧 {n_skip}，失败 {n_fail}）"
          f"，交易日 {len(all_dates)} 个（{all_dates[0]} ~ {all_dates[-1]}）")
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
    return {
        "n": n,
        "dates": dates,
        "ic_series": [round(x, 4) for x in ics],
        "ic_mean": round(m, 4) if m is not None else None,
        "ic_std": round(sd, 4) if sd is not None else None,
        "t_stat": round(_plain_t(ics), 2) if _plain_t(ics) is not None else None,
        "nw_t": round(_nw_t(ics, h - 1), 2) if _nw_t(ics, h - 1) is not None else None,
        "bonferroni_sig": bool(_nw_t(ics, h - 1) is not None and abs(_nw_t(ics, h - 1)) >= T_BONF),
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
    """单周期有效性判定（以 Newey-West t 为主口径）。"""
    n = agg.get("n") or 0
    if n < MIN_SAMPLE:
        return "样本积累中", "#94a3b8", f"n={n}<{MIN_SAMPLE}"
    ic = agg.get("ic_mean")
    t = agg.get("nw_t")
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
    """综合判定: 取 |NW t| 最大且样本达标的周期。"""
    best, best_t = None, -1.0
    for h in HORIZONS:
        a = horizons.get(str(h)) or {}
        if (a.get("n") or 0) < MIN_SAMPLE:
            continue
        t = a.get("nw_t")
        if t is not None and abs(t) > abs(best_t):
            best, best_t = a, t
    if best is None:
        n_max = max((horizons.get(str(h)) or {}).get("n") or 0 for h in HORIZONS)
        return "样本积累中", "#94a3b8", f"各周期样本均 <{MIN_SAMPLE}（最大 {n_max}）"
    lab, col, note = _verdict_h(best)
    hh = next(h for h in HORIZONS if (horizons.get(str(h)) or {}) is best)
    return lab, col, note + f"（T+{hh}）"


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


def _composite_ic(snapshots, w4, h, tdays, px, max_base_idx=None, dates_only=None):
    """给定 4 维权重（自动归一），计算复合总分的 IC 序列与聚合指标。"""
    wn = _norm4(w4)
    fwd = _fwd_returns(snapshots, h, tdays, px, max_base_idx)
    ics, spreads, dates = [], [], []
    for d0, d1, rets in fwd:
        if dates_only is not None and d0 not in dates_only:
            continue
        base = next(s for s in snapshots if s["date"] == d0)
        fv, rv = [], []
        for r in base["rows"]:
            if r["code"] in rets and all(isinstance(r.get(f), (int, float)) for f in OPT_DIMS):
                fv.append(sum(wn[f] * r[f] for f in OPT_DIMS))
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
        "version": "v3.1-10dim-ic",
        "fitted_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "target_horizon": TARGET_H,
        "method": ("十维 IC 驱动权重: 正IC维度按比例分配+地板3%+向先验收缩(λ); "
                   "走步验证(训练≥30截面,每5截面重拟,样本外比对); "
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
    """供 step2/apply_sector_momentum 调用：读 factor_weights.json，失败回退先验。"""
    try:
        with open(WEIGHTS_PATH, encoding="utf-8") as f:
            d = json.load(f)
        w = d.get("weights") or {}
        need = set(OPT_DIMS) | {"score_sector"}
        if not need.issubset(w.keys()):
            raise ValueError("weights 字段不全")
        vals = [float(w[k]) for k in need]
        if any(v < 0 for v in vals) or abs(sum(vals) - 1.0) > 0.02:
            raise ValueError(f"weights 非法: {vals}")
        return w, d
    except Exception:
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
    if n_added:
        save_history(hist)
        print(f"📚 历史截面更新: 新增/覆盖 {n_added} 个（现有 {len(hist['snapshots'])} 个）")

    snaps = [s for s in hist.get("snapshots", []) if s.get("rows")]
    if len(snaps) < 3:
        print(f"❌ 截面数不足（{len(snaps)}），无法计算 IC；可用 --bootstrap-from-git 从 git 历史引导")
        return 1

    tdays, px = px_index(hist)
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
    elif do_opt:
        # E3: 权重复核默认只用当前池口径(最大 pool_size 组), 防新旧池混合拟合
        _cur = max(pool_sizes) if pool_sizes else 200
        _before = len(aligned)
        aligned = [s for s in aligned if _psize(s) == _cur]
        print(f"🔍 权重复核自动过滤至当前池口径 pool_size={_cur}: {_before} → {len(aligned)} 个截面"
              f"（历史口径 {pool_sizes}; --pool-filter 可指定其他口径做诊断）")

    # 因子 IC
    factors = {}
    for field, label in FACTORS:
        agg = _eval_factor(aligned, field, tdays, px)
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

    # 当前生效权重 vs 先验的复合 IC（若权重文件存在）
    w_now, w_meta = load_weights()
    current_cmp = None
    if w_now:
        w4 = {k: float(w_now[k]) for k in OPT_DIMS}
        current_cmp = {str(h): _composite_ic(aligned, w4, h, tdays, px) for h in HORIZONS}

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
        "factor_correlation": corr,
        "current_weights_composite_ic": current_cmp,
        "active_weights": w_now,
        "notes": [
            f"有效截面 {len(aligned)} 个交易日（{aligned[0]['date']} ~ {aligned[-1]['date']}），"
            f"剔除非交易日陈旧快照 {len(stale)} 个；收益对 t 日全部 TOP200 个股计算（消除留存偏差，需 --bootstrap-prices 回填）",
            "IC 衡量排序能力而非绝对收益；分层多空为未扣成本毛利口径",
            "TOP200 为高换手关注度筛选池：动量/关注型因子的负向 IC 更可能反映'关注度脉冲后的均值回复'，"
            "而非因子构造错误；解读时须结合本池的选择机制",
            "历史截面来自 git 提交记录（含早期综合行业归类版本，v2.46 已修复）",
        ],
    }

    if do_opt:
        result, w10 = optimize_weights(aligned, tdays, px)
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
