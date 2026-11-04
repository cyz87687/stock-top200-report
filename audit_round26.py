#!/usr/bin/env python3
"""round26：按飞书《量化选股评分完整方案》口径，在本池复测 8 个目标因子。

## 为什么要单独做这一轮

老大给了两份材料：
  ① 飞书文档「量化选股评分完整方案」——5 大类权重 + 8 因子公式 + 统一预处理流水线
  ② `final_ranking-近半年.csv`——47 因子 IC 排名（外部全市场、近半年样本）

两者都不是本项目口径。本项目是「成交额 TOP500 池 × 78 截面（2026-06-26~10-23）」，
而 CSV 是**外部全市场**、飞书公式基于**全市场截面 Z-score**。
已发现反证：CSV 的 BIAS60 ic_daily = **−0.0640**，本项目 score_bias60 的 h5 IC = **+0.1648**，
符号完全相反 → 外部 IC 不能直接搬。

所以本轮把 8 个因子**按飞书公式在本池重算**，逐项对比 CSV，判断：
  · 哪些结论可迁移（同号同量级）
  · 哪些必须放弃（异号或塌陷）
  · 照搬飞书 5 大类会丢掉多少本项目已有的 alpha（反转块 bias60+rsi_rev 占 35% 权重、IC +0.16/+0.21）

## 飞书口径落地说明（逐条对齐，差异显式标注）

  · Winsorize 0.1%/99.9%：本池单截面 ~190-490 只 → 0.1% 分位≈0.5 只，
    实际效果等同 min/max 截断，几乎不生效。照实现，但要记录被截断的样本数。
  · Z-score：飞书要求**全市场截面**。本项目无全市场面板，只能用**池内截面** Z-score。
    ⚠️ 这是口径差异，不是等价替代：池子本身就是「成交额前 500」筛选后的，
       池内 Z-score 剔除了「大盘/小盘」的绝对位置信息。所有结论标注「池内 Z」。
  · SIZE_IND_NEUT：飞书四步 = 行业内分组 → 组内 LN(流通市值) 去极值 → 组内 Z → 取负。
    行业内已 Z，**不再二次全市场标准化**（照做）。
    行业用 sector_parent（申万一级，scored 100% 覆盖，取最近截面映射近似历史）。
  · 未来函数规避：财务类用 disclosure_date(P) <= 截面日期；行情类用 T 日收盘当日因子
    （本脚本算的是「当日收盘因子 → 未来 hop 日收益」，与项目既有 78 截面口径一致，
     不含隔夜开盘执行价差 → 结论偏乐观，已在 round25 记录）。

## 输出

  audit_round26.json
    q0_coverage   8 因子逐截面样本覆盖率
    q1_ic         8 因子（飞书口径）× hop(1/5/10/20) 的 IC/ICIR/NW t(0,3,6)/IC>0 占比
    q2_vs_csv     与外部 CSV 逐因子对比（同号？量级？）
    q3_vs_v48     8 因子与现行 7 维的截面秩相关（找重叠 / 找增量）
    q4_fbscore    飞书简化公式综合评分在本池的 IC，与 v4.8 复合分对比
    q5_ablation   逐个删掉飞书 5 大类，看复合分掉多少（各类边际贡献）
    q6_reversal   照搬飞书（去掉反转块）vs 现行（保留反转块）的 alpha 差额
"""
import csv
import glob
import json
import math
import os
import sys
from collections import defaultdict

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import factor_eval as fe
import audit_round21 as r21
import audit_round24b as rb

T_BONF = 3.08
LAGS = [0, 3, 6]
HOPS = [1, 5, 10, 20]
EM_CACHE = os.path.join(HERE, "data", "fin_cache_em")
CSV_REF = "/Users/yzreal/Downloads/final_ranking-近半年.csv"

# ============================================================ 基础工具（沿用项目口径）


def nw_t(xs, lag):
    n = len(xs)
    if n < 5:
        return None
    mu = sum(xs) / n
    d = [x - mu for x in xs]
    var = sum(x * x for x in d) / n
    for L in range(1, min(lag, n - 1) + 1):
        gl = sum(d[i] * d[i - L] for i in range(L, n)) / n
        var += 2 * (1 - L / (lag + 1)) * gl
    se = math.sqrt(var / n) if var > 0 else None
    return (mu / se) if se else None


def winsorize_z(vals, lo=0.001, hi=0.999):
    """飞书流水线第 1-2 步：截面 winsorize 0.1%/99.9% → Z-score。
    → (z_list, n_clipped)。缺失值不参与（调用方已过滤）。"""
    vals = [v for v in vals if isinstance(v, (int, float)) and v == v
            and v not in (float("inf"), float("-inf"))]
    n = len(vals)
    if n < 10:
        return None, 0
    s = sorted(vals)
    k_lo = max(0, int(math.floor(lo * (n - 1))))
    k_hi = min(n - 1, int(math.ceil(hi * (n - 1))))
    a, b = s[k_lo], s[k_hi]
    clipped = sum(1 for v in vals if v < a or v > b)
    w = [min(max(v, a), b) for v in vals]
    m = sum(w) / n
    sd = math.sqrt(sum((v - m) ** 2 for v in w) / n)
    if sd <= 1e-12:
        return None, clipped
    return [(v - m) / sd for v in w], clipped


def group_winsorize_zneg(pairs, min_grp=5):
    """飞书 SIZE_IND_NEUT 四步：行业分组 → 组内 winsorize → 组内 Z → 取负。
    pairs: [(code, industry, value)] → {code: -z}
    ⚠️ min_grp 首版设 10，实测覆盖率只有 7.6%：本池 29 个申万一级行业里，
       电子 153 只、机械设备 47 只，但大量行业只有 3~8 只股票 → 全被丢弃。
       飞书原文没说最小样本数；样本 <5 时 Z-score 无统计意义，故设为 5，
       并把被丢弃的组在输出中留痕，不静默。"""
    by = defaultdict(list)
    for c, ind, v in pairs:
        if v is None or v <= 0:
            continue
        by[ind].append((c, math.log(v)))
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
            out[c] = -z          # 取负：低市值（组内）得高分
    return out, dropped


def spearman(xs, ys):
    n = len(xs)
    if n < 5:
        return None
    rx, ry = fe._ranks(xs), fe._ranks(ys)
    mx, my = sum(rx) / n, sum(ry) / n
    sxy = sum((rx[i] - mx) * (ry[i] - my) for i in range(n))
    sxx = sum((rx[i] - mx) ** 2 for i in range(n)) ** 0.5
    syy = sum((ry[i] - my) ** 2 for i in range(n)) ** 0.5
    return (sxy / (sxx * syy)) if sxx > 0 and syy > 0 else None


def ic_stats(ics):
    n = len(ics)
    if n < 5:
        return None
    mu = sum(ics) / n
    sd = math.sqrt(sum((x - mu) ** 2 for x in ics) / n) if n > 1 else 0.0
    return {
        "n": n,
        "ic": round(mu, 4),
        "std": round(sd, 4),
        "icir": round(mu / sd, 3) if sd > 1e-9 else None,
        "t0": round(nw_t(ics, 0), 2) if nw_t(ics, 0) is not None else None,
        "t3": round(nw_t(ics, 3), 2) if nw_t(ics, 3) is not None else None,
        "t6": round(nw_t(ics, 6), 2) if nw_t(ics, 6) is not None else None,
        "ic_pos_share": round(sum(1 for x in ics if x > 0) / n, 3),
        "pass_bonf": bool(all(v is not None and abs(v) >= T_BONF
                              for v in (nw_t(ics, 0), nw_t(ics, 3), nw_t(ics, 6)))),
    }


# ============================================================ EM 财务（PIT）

def load_em_all():
    """⚠️ 不能复用 r21.load_em_cache()：它的 ALL_INDS 只有 17 个指标白名单，
       会把「基本每股收益」「每股息税前利润」「归母净利润」直接过滤掉
       → EP / EVEBIT / SUE 三项全空（首版实测覆盖率 0.0%，正是此因）。
       这里加载 EM 缓存里的**全部** 70 个指标，只做数值清洗与 指标+选项 去重。"""
    data = {}
    for f in glob.glob(os.path.join(EM_CACHE, "*.json")):
        code = os.path.basename(f)[:-5]
        try:
            recs = json.load(open(f, encoding="utf-8")).get("records") or []
        except Exception:
            continue
        # 选项优先级：常用指标 > 每股指标 > 成长能力 > 盈利能力 > 其他（同 round21 语义）
        PRI = {"常用指标": 0, "每股指标": 1, "成长能力": 2, "盈利能力": 3, "财务风险": 4}
        by_name = {}
        for r in recs:
            name, opt = r.get("指标"), r.get("选项")
            if not name or not opt:
                continue
            d = by_name.setdefault(name, {})
            pr = PRI.get(opt, 9)
            if opt in d and d[opt][0] <= pr:
                continue
            ser = {}
            for k, v in r.items():
                if len(k) == 8 and k.isdigit():
                    if v is None or v is False or v in ("--", "-", "", "False"):
                        continue
                    try:
                        fv = float(v)
                    except Exception:
                        continue
                    # ⚠️ EM 原始数据里有字符串 'nan'（745/818 只股票的「每股息税前利润」含 nan），
                    #    float('nan') 能通过 isinstance 检查但会污染截面均值/标准差、
                    #    进而让 Z-score 全变 nan，且下游 NaN 比较恒为 False → 静默串位。
                    #    必须在此显式剔除，不能靠下游兜。
                    if fv != fv or fv in (float("inf"), float("-inf")):
                        continue
                    ser[k] = fv
            if ser:
                d[opt] = (pr, ser)
        ind = {}
        for name, byopt in by_name.items():
            best = sorted(byopt.items(), key=lambda kv: kv[1][0])
            if best:
                ind[name] = best[0][1][1]
        data[code] = ind
    return data


def ttm_sum(ind, name, P):
    """累计值 → TTM：最近 4 个季度单季之和。
    P 若为 Q1/中报/三季报，需 P + 去年同期年报 − 去年同期同季累计。"""
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


def single_q(ind, name, P):
    """累计值 → 单季值。"""
    def get(n, p):
        return (ind.get(n) or {}).get(p)

    cur = get(name, P)
    if cur is None:
        return None
    md = P[4:]
    if md == "0331":
        return cur
    y = int(P[:4])
    prevq = {"0630": "%d0331" % y, "0930": "%d0630" % y, "1231": "%d0930" % y}.get(md)
    if prevq is None:
        return None
    pc = get(name, prevq)
    if pc is None:
        return None
    return cur - pc


def prev_periods(P, k):
    """P 之前的 k 个同季报告期（用于 SUE 的 EPS 序列）。"""
    out = []
    y, md = int(P[:4]), P[4:]
    order = ["0331", "0630", "0930", "1231"]
    i = order.index(md)
    for _ in range(k):
        i -= 1
        if i < 0:
            i = 3
            y -= 1
        out.append("%d%s" % (y, order[i]))
    return out


def build_factors(ind, P, d0, price, circ_mv, sector, snap_row):
    """按飞书公式算 8 因子（原始值，Z-score 在截面层统一做）。
    → {factor: raw_value or None}"""
    def get(name, p):
        return (ind.get(name) or {}).get(p)

    out = {}

    # ---- OCFG 经营现金流同比增长率（飞书：OPERATING_CASH_FLOW_GROWTH_RATE）
    #      用「经营现金流量净额」同报告期 YoY（累计值 YoY 已消季节性）
    cur_ocf, prev_ocf = get("经营现金流量净额", P), get("经营现金流量净额", "%d%s" % (int(P[:4]) - 1, P[4:]))
    if cur_ocf is not None and prev_ocf is not None and abs(prev_ocf) > 1e6:
        out["OCFG"] = (cur_ocf - prev_ocf) / abs(prev_ocf) * 100.0

    # ---- ROE 净资产收益率（飞书：ROE()）直接用 EM 披露口径（PIT）
    v = get("净资产收益率(ROE)", P)
    if v is not None:
        out["ROE"] = v

    # ---- EP = 扣非净利润TTM / 总市值
    #      总股本 = 归母净利润 / 基本每股收益（同一报告期，量纲自洽）
    ni_attr, eps = get("归母净利润", P), get("基本每股收益", P)
    ttm_np = ttm_sum(ind, "扣非净利润", P)
    if ttm_np is not None and price and price > 0 and ni_attr and eps and eps > 0.001:
        shares = ni_attr / eps                       # 股
        mcap = price * shares                        # 元
        if mcap > 0:
            out["EP"] = ttm_np / mcap
            # ---- EVEBIT：飞书要 1/EV_TO_EBIT。EV = 市值 + 有息负债 − 现金，
            #      EM 缓存无「有息负债」拆分 → 退化为 EBIT_TTM/总市值，仅少一个负债加项。
            eps_ebit = get("每股息税前利润", P)
            if eps_ebit is not None:
                # 每股息税前利润是累计口径 → 先转 TTM
                e_ttm = ttm_sum(ind, "每股息税前利润", P)
                if e_ttm is not None:
                    out["EVEBIT"] = (e_ttm * shares) / mcap
                    out["_ev_degraded"] = 1

    # ---- NAG 净资产同比增长率（飞书：NET_ASSET_GROWTH_RATE）
    eq, eq_py = get("股东权益合计(净资产)", P), get("股东权益合计(净资产)", "%d%s" % (int(P[:4]) - 1, P[4:]))
    if eq is not None and eq_py is not None and eq_py > 0:
        out["NAG"] = (eq - eq_py) / eq_py * 100.0

    # ---- SUE 标准化未预期盈余（飞书：一致预期口径）
    #      ⚠️ 本项目无分析师预期库 → 用「财报单季扣非EPS」对自身历史 4 季做标准化：
    #         SUE = (Q_t − mean(前4季)) / std(前4季)。这是 time-series SUE，
    #         不是飞书要的 analyst-based SUE，方向一致但噪声更大。
    q_eps = single_q(ind, "基本每股收益", P)
    if q_eps is not None:
        hist = [single_q(ind, "基本每股收益", p) for p in prev_periods(P, 4)]
        hist = [x for x in hist if x is not None]
        if len(hist) >= 3:
            m = sum(hist) / len(hist)
            sd = math.sqrt(sum((x - m) ** 2 for x in hist) / len(hist))
            if sd > 1e-6:
                out["SUE"] = (q_eps - m) / sd
                out["_sue_proxy"] = 1

    # ---- SIZE_IND_NEUT：原始值交给截面层做行业内 Z（这里只透传市值与行业）
    if circ_mv and circ_mv > 0:
        out["_mv"] = circ_mv
        out["_ind"] = sector or "未分类"
    # ⚠️ 兜底：任何 nan/inf 一律当缺失（下游 NaN 比较恒 False 会静默跳过，
    #    但若混进 Z-score 的均值/方差计算就会整列变 nan）
    for k in list(out.keys()):
        v = out[k]
        if isinstance(v, float) and (v != v or v in (float("inf"), float("-inf"))):
            out.pop(k)
    return out


# ============================================================ 主流程

def main():
    h = json.load(open(os.path.join(HERE, "factor_history.json"), encoding="utf-8"))
    snaps, _ = fe._drop_stale_price_snapshots(h["snapshots"])
    tdays, px = fe.px_index(h)
    tidx = {d: i for i, d in enumerate(tdays)}
    em = load_em_all()

    # 行业映射（最近截面近似全历史；申万一级半年内极少调整，标注近似）
    scored = json.load(open(os.path.join(HERE, "top200_scored_2026-10-23.json"), encoding="utf-8"))
    sector_map = {}
    for r in scored["results"]:
        c = r.get("code") or ""
        c6 = c[-6:] if len(c) >= 6 else c
        sector_map[c6] = r.get("sector_parent")

    print("=" * 96)
    print("round26：按飞书《量化选股评分完整方案》口径，在本池复测 8 因子")
    print("=" * 96)
    print(f"截面 {len(snaps)} · 交易日 {len(tdays)} · 价格 {len(px)} 只 · EM 缓存 {len(em)} 只")

    # ---------------- ⚠️ 股本重建：circ_mv 只在最近 2 个截面有值
    #   实测 2026-06-26 起 76 个截面 circ_mv 全为 0，仅 10-22/10-23 有值
    #   → SIZE_IND_NEUT 覆盖率首版只有 7.6%。沿用 round24e 股本重建法：
    #     用 circ_mv/price 的中位数反推每股股本，再乘各截面当日价。
    #   ⚠️ 代价：重建市值 = 固定股本 × 当日价 → 市值变动几乎完全由价格驱动，
    #     而 SIZE_IND_NEUT 是行业内 LN(市值) 的 Z 取负 → 退化为「行业内涨幅的负向」，
    #     与 REV1M（1 月反转）可能高度共线。Q3 会显式检验这一点，不掩盖。
    shares_est = {}
    _sh = defaultdict(list)
    for s in snaps:
        for r in s["rows"]:
            mv, pr = r.get("circ_mv"), r.get("price")
            if isinstance(mv, (int, float)) and mv and isinstance(pr, (int, float)) and pr > 0:
                _sh[r["code"]].append(mv / pr)
    for c, v in _sh.items():
        v = sorted(v)
        shares_est[c] = v[len(v) // 2]
    print(f"股本重建（round24e 法）：{len(shares_est)} 只（真实 circ_mv 仅覆盖最近 2 截面）")

    def mcap_of(code6, d0, row):
        """→ 当日流通市值。优先真实 circ_mv；缺失时用 重建股本 × 当日价。"""
        v = row.get("circ_mv")
        if isinstance(v, (int, float)) and v > 0:
            return float(v)
        sh = shares_est.get(row.get("code"))
        pr = (px.get(code6) or {}).get(d0) or row.get("price")
        if sh and isinstance(pr, (int, float)) and pr > 0:
            return sh * pr
        return None

    # ---------------- 逐截面：算原始因子 → 截面 Z（含 SIZE_IND_NEUT 行业内 Z）
    FACTORS = ["OCFG", "ROE", "EP", "EVEBIT", "NAG", "REV1M", "SIZE_IND_NEUT", "SUE"]
    raw_by_date = {}       # date -> code -> {factor: raw}
    cov_by_date = {}       # date -> factor -> n
    clip_note = []
    ind_drop_note = []

    for s in snaps:
        d0 = s["date"]
        raw, cov = {}, defaultdict(int)
        sue_proxy = 0
        ev_deg = 0
        for r in s["rows"]:
            c6 = (r.get("code") or "")[-6:]
            ind = em.get(c6)
            if not ind:
                continue
            P = r21.pit_period(sorted({p for dd in ind.values() for p in dd}, reverse=True), d0)
            if P is None:
                continue
            f = build_factors(ind, P, d0, r.get("price"), mcap_of(c6, d0, r),
                              sector_map.get(c6), r)
            # REV1M = −(过去 21 交易日收益)，行情类不需 PIT
            p0 = (px.get(c6) or {}).get(d0)
            i0 = tidx.get(d0)
            if p0 and i0 is not None and i0 >= 21:
                pb = (px.get(c6) or {}).get(tdays[i0 - 21])
                if pb and pb > 0:
                    f["REV1M"] = -(p0 / pb - 1.0) * 100.0
            sue_proxy += f.pop("_sue_proxy", 0)
            ev_deg += f.pop("_ev_degraded", 0)
            mv, sind = f.pop("_mv", None), f.pop("_ind", None)
            raw[c6] = f
            for k in f:
                cov[k] += 1
            raw.setdefault(c6, {})["_mv"] = mv
            raw[c6]["_ind"] = sind

        # SIZE_IND_NEUT：行业内 winsorize + Z + 取负
        pairs = [(c, v.get("_ind"), v.get("_mv")) for c, v in raw.items() if v.get("_mv")]
        szn, dropped = group_winsorize_zneg(pairs)
        for c, z in szn.items():
            raw[c]["SIZE_IND_NEUT"] = z
            cov["SIZE_IND_NEUT"] += 1
        if dropped:
            ind_drop_note.append((d0, len(dropped), sum(dropped.values())))

        for c in raw:
            raw[c].pop("_mv", None)
            raw[c].pop("_ind", None)

        # 截面 Z-score（SIZE_IND_NEUT 跳过，飞书明确不二次标准化）
        for k in FACTORS:
            if k == "SIZE_IND_NEUT":
                continue
            codes = [c for c, v in raw.items() if isinstance(v.get(k), (int, float))]
            if len(codes) < 10:
                continue
            zs, clipped = winsorize_z([raw[c][k] for c in codes])
            if zs is None:
                for c in codes:
                    raw[c][k] = None
                continue
            for c, z in zip(codes, zs):
                raw[c][k] = z
            if clipped:
                clip_note.append((d0, k, clipped))

        raw_by_date[d0] = raw
        cov_by_date[d0] = dict(cov)

    # 覆盖统计
    cov_summary = {}
    for k in FACTORS:
        ns = [cov_by_date[d].get(k, 0) for d in raw_by_date]
        tot = [len(raw_by_date[d]) for d in raw_by_date]
        cover = sum(ns) / sum(tot) if sum(tot) else 0.0
        cov_summary[k] = {
            "cover_pct": round(cover * 100, 1),
            "n_min": min(ns), "n_med": sorted(ns)[len(ns) // 2], "n_max": max(ns),
            "pool_med": sorted(tot)[len(tot) // 2],
        }
    print("\n【Q0】8 因子本池覆盖率（飞书口径计算，78 截面）")
    print("%-16s %8s %8s %8s %8s" % ("因子", "覆盖%", "最小n", "中位n", "池中位"))
    for k in FACTORS:
        v = cov_summary[k]
        print("%-16s %7.1f%% %8d %8d %8d" % (k, v["cover_pct"], v["n_min"], v["n_med"], v["pool_med"]))

    # ---------------- Q1：IC battery
    def fwd_ret(code6, d0, hop):
        m = px.get(code6)
        i0 = tidx.get(d0)
        if not m or i0 is None or i0 + hop >= len(tdays):
            return None
        p0, p1 = m.get(tdays[i0]), m.get(tdays[i0 + hop])
        if p0 is None or p1 is None or p0 <= 0:
            return None
        return p1 / p0 - 1.0

    q1 = {}
    for k in FACTORS:
        for hop in HOPS:
            ics, dates = [], []
            for d0, raw in raw_by_date.items():
                fv, rv = [], []
                for c, v in raw.items():
                    x = v.get(k)
                    if not isinstance(x, (int, float)):
                        continue
                    fr = fwd_ret(c, d0, hop)
                    if fr is None:
                        continue
                    fv.append(x)
                    rv.append(fr)
                if len(fv) >= 40:
                    ic = fe.rank_ic(fv, rv)
                    if ic is not None:
                        ics.append(ic)
                        dates.append(d0)
            st = ic_stats(ics)
            if st:
                q1["%s|h%d" % (k, hop)] = st

    print("\n【Q1】8 因子本池 IC（飞书口径：winsorize 0.1% + 池内截面 Z-score）")
    print("%-16s %6s %8s %8s %7s %7s %7s %7s %6s" %
          ("因子", "hop", "IC", "ICIR", "NWt0", "NWt3", "NWt6", "IC>0", "过Bonf"))
    for k in FACTORS:
        for hop in HOPS:
            s2 = q1.get("%s|h%d" % (k, hop))
            if not s2:
                continue
            print("%-16s %6d %8.4f %8.3f %7s %7s %7s %6.1f%% %6s" % (
                k, hop, s2["ic"], s2["icir"] or 0, s2["t0"], s2["t3"], s2["t6"],
                s2["ic_pos_share"] * 100, "YES" if s2["pass_bonf"] else "-"))

    # ---------------- Q2：与外部 CSV 对比
    csv_ref = {}
    try:
        for row in csv.DictReader(open(CSV_REF, encoding="utf-8-sig")):
            def fnum(x):
                try:
                    return float(x)
                except Exception:
                    return None
            csv_ref[row["factor"]] = {
                "ic": fnum(row.get("ic_daily")),
                "icir": fnum(row.get("icir_daily")),
                "exc_ann": fnum(row.get("exc_ann")),
                "coverage": fnum(row.get("coverage")),
                "selected": row.get("selected"),
            }
    except Exception as e:
        print("CSV 读取失败:", e)

    # CSV 的 SIZE 是「全市场 LN(流通市值)」，对应本项目 SIZE_IND_NEUT 的母体
    q2 = {}
    for k, src in (("OCFG", "OCFG"), ("ROE", "ROE"), ("EP", "EP"), ("EVEBIT", "EVEBIT"),
                   ("NAG", "NAG"), ("REV1M", "REV1M"), ("SIZE_IND_NEUT", "SIZE"),
                   ("SUE", None)):
        c = csv_ref.get(src) if src else None
        mine = q1.get("%s|h5" % k) or {}
        same = None
        if c and c.get("ic") is not None and mine.get("ic") is not None:
            same = (c["ic"] > 0) == (mine["ic"] > 0)
        q2[k] = {
            "csv_factor": src,
            "csv_ic": c.get("ic") if c else None,
            "csv_icir": c.get("icir") if c else None,
            "csv_coverage": c.get("coverage") if c else None,
            "csv_selected": c.get("selected") if c else None,
            "pool_ic_h5": mine.get("ic"),
            "pool_icir_h5": mine.get("icir"),
            "pool_t0_h5": mine.get("t0"),
            "same_sign": same,
        }

    print("\n【Q2】外部 CSV 与本池同因子对比（h5）")
    print("%-16s %10s %8s %8s   %8s %8s %8s  %6s" %
          ("因子", "CSV对应", "CSV_IC", "CSV_ICIR", "池内_IC", "池内ICIR", "池内t0", "同号"))
    for k in FACTORS:
        v = q2[k]
        print("%-16s %10s %8s %8s   %8s %8s %8s  %6s" % (
            k, v["csv_factor"] or "—",
            f"{v['csv_ic']:+.4f}" if v["csv_ic"] is not None else "—",
            f"{v['csv_icir']:+.3f}" if v["csv_icir"] is not None else "—",
            f"{v['pool_ic_h5']:+.4f}" if v["pool_ic_h5"] is not None else "—",
            f"{v['pool_icir_h5']:+.3f}" if v["pool_icir_h5"] is not None else "—",
            f"{v['pool_t0_h5']:+.2f}" if v["pool_t0_h5"] is not None else "—",
            ("Y" if v["same_sign"] else "N") if v["same_sign"] is not None else "—"))

    # ---------------- 现行 7 维（用于相关与复合对比）
    # ⚠️ 基线复现必须用 r21.load_em_cache()（17 指标白名单），
    #   不能用本脚本的 load_em_all()（全 70 指标）：
    #   两者对「同一指标取哪个选项」的优先级规则不同（round21 按 ALL_INDS×IND_OPT 顺序取首个，
    #   本脚本按常用指标>每股指标>成长能力 优先级取）→ quality 维重建值会不一致
    #   → v4.8 基线复现不了 round25 的已落地值，后续所有差额都失去参照。
    em_base = r21.load_em_cache()
    subs = defaultdict(dict)
    for s in snaps:
        d0 = s["date"]
        for r in s["rows"]:
            ind = em_base.get((r.get("code") or "")[-6:])
            if not ind:
                continue
            P = r21.pit_period(sorted({p for dd in ind.values() for p in dd}, reverse=True), d0)
            if P is None:
                continue
            subs[d0][r["code"]] = r21.sub_factors_pit(ind, P)

    bf = rb.backfill_reversal(tdays, px)

    def _rank10(vals_by_code):
        ks = sorted(vals_by_code, key=lambda c: vals_by_code[c])
        n = len(ks)
        if n < 2:
            return {}
        return {c: round((i / (n - 1)) * 10.0, 2) for i, c in enumerate(ks)}

    qual_rank_by_date = {}
    for s in snaps:
        d0 = s["date"]
        q = {}
        for r in s["rows"]:
            sub = (subs.get(d0) or {}).get(r["code"])
            if sub:
                vq = r21.aggregate(sub, "grouped")
                if isinstance(vq, (int, float)):
                    q[r["code"]] = vq
        qual_rank_by_date[d0] = _rank10(q)

    shares = defaultdict(list)
    for s in snaps:
        for r in s["rows"]:
            mv, pr = r.get("circ_mv"), r.get("price")
            if isinstance(mv, (int, float)) and mv and isinstance(pr, (int, float)) and pr > 0:
                shares[r["code"]].append(mv / pr)
    shares_est = {}
    for c, v in shares.items():
        v = sorted(v)
        shares_est[c] = v[len(v) // 2]

    size_by_date = {}
    for s in snaps:
        d0 = s["date"]
        mv = {}
        for r in s["rows"]:
            c = r["code"]
            v = r.get("circ_mv")
            if not (isinstance(v, (int, float)) and v):
                sh = shares_est.get(c)
                pr = r.get("price")
                if sh and isinstance(pr, (int, float)) and pr > 0:
                    v = sh * pr
            if isinstance(v, (int, float)) and v > 0:
                mv[c] = float(v)
        if len(mv) < 20:
            size_by_date[d0] = {}
            continue
        # ⚠️ 方向必须是「小市值=高分」（与 round24f / 生产线一致）：
        #   降序排序后 i=0 是最大市值 → 得 0 分；最小市值 → 得 10 分。
        #   首版误用 _rank10（升序，大市值=高分）导致 v4.8 基线 h20 IC 从
        #   0.2484 掉到 0.1915（偏差 −0.057，超自检容差）→ 已修正。
        ks = sorted(mv, key=lambda c: -mv[c])
        n = len(ks)
        size_by_date[d0] = {c: round((i / (n - 1)) * 10.0, 2) for i, c in enumerate(ks)} if n > 1 else {}

    def lowvol_raw(code, d0):
        m = px.get(code)
        i0 = tidx.get(d0)
        if not m or i0 is None or i0 < 20:
            return None
        closes = []
        for j in range(i0 - 20, i0 + 1):
            v = m.get(tdays[j])
            if v is None:
                return None
            closes.append(v)
        rets = [(closes[k] / closes[k - 1] - 1.0) for k in range(1, len(closes))]
        n = len(rets)
        mu = sum(rets) / n
        return (sum((r - mu) ** 2 for r in rets) / (n - 1)) ** 0.5

    lv_by_date = {}
    for s in snaps:
        d0 = s["date"]
        raw = {}
        for r in s["rows"]:
            v = lowvol_raw(r["code"], d0)
            if v is not None:
                raw[r["code"]] = v
        if len(raw) < 20:
            lv_by_date[d0] = {}
            continue
        # 低波是「波动越低越好」→ 降序后 i=0 是最高波动得 0 分，最低波得 10 分
        n = len(raw)
        ks = sorted(raw, key=lambda c: -raw[c])
        lv_by_date[d0] = {c: round((i / (n - 1)) * 10.0, 2) for i, c in enumerate(ks)} if n > 1 else {}

    def v48_val(code6, r, d0):
        """现行 7 维的**原始分**（0-5 或 0-10），键用 6 位 code。"""
        out = {}
        v = r.get("score_fund")
        out["score_fund"] = v if isinstance(v, (int, float)) else None
        v = r.get("score_tech")
        out["score_tech_neg"] = (10.0 - v) if isinstance(v, (int, float)) else None
        t = (bf.get(d0) or {}).get(code6)
        out["score_bias60"] = t[1] if t else None
        out["score_rsi_rev"] = t[2] if t else None
        out["score_quality"] = qual_rank_by_date.get(d0, {}).get(code6)
        out["score_size"] = size_by_date.get(d0, {}).get(code6)
        out["score_lowvol20"] = lv_by_date.get(d0, {}).get(code6)
        return out

    # ---------------- Q3：新因子 vs 现行维的截面秩相关
    print("\n【Q3】8 个新因子与现行 7 维的截面秩相关（78 截面均值）")
    cur_dims = ["score_fund", "score_quality", "score_lowvol20", "score_tech_neg",
                "score_bias60", "score_rsi_rev", "score_size"]
    q3 = {}
    for k in FACTORS:
        row = {}
        for d in cur_dims:
            cors = []
            for s in snaps:
                d0 = s["date"]
                raw = raw_by_date.get(d0) or {}
                xs, ys = [], []
                for r in s["rows"]:
                    c6 = (r.get("code") or "")[-6:]
                    x = (raw.get(c6) or {}).get(k)
                    if not isinstance(x, (int, float)):
                        continue
                    y = v48_val(c6, r, d0).get(d)
                    if not isinstance(y, (int, float)):
                        continue
                    xs.append(x)
                    ys.append(y)
                if len(xs) >= 40:
                    c2 = spearman(xs, ys)
                    if c2 is not None:
                        cors.append(c2)
            row[d] = round(sum(cors) / len(cors), 3) if cors else None
        q3[k] = row
    print("%-16s %9s %9s %9s %9s %9s %9s %9s" %
          ("新因子\\现行", "fund", "质量", "低波20", "tech_neg", "bias60", "rsi_rev", "size"))
    for k in FACTORS:
        row = q3[k]
        print("%-16s %9s %9s %9s %9s %9s %9s %9s" % (
            k,
            *[f"{row[d]:+.2f}" if row[d] is not None else "—" for d in cur_dims]))

    # ---------------- Q4/Q6：飞书公式综合评分 vs v4.8
    # 飞书简化公式：0.25 OCFG + 0.10 ROE + 0.15 EP + 0.05 EVEBIT
    #               + 0.20 NAG + 0.10 REV1M + 0.05 SIZE_IND_NEUT + 0.10 SUE
    FB_W = {"OCFG": 0.25, "ROE": 0.10, "EP": 0.15, "EVEBIT": 0.05,
            "NAG": 0.20, "REV1M": 0.10, "SIZE_IND_NEUT": 0.05, "SUE": 0.10}

    def fb_score(raw, code6):
        num = den = 0.0
        for k, w in FB_W.items():
            v = (raw.get(code6) or {}).get(k)
            if not isinstance(v, (int, float)):
                continue
            num += v * w
            den += w
        return (num / den) if den > 0 else None

    def _fb_score_nosz(raw, code6):
        """去掉 SIZE_IND_NEUT（5% 权重）的飞书简化公式 —— 用于在近全样本上验证，
        因为 SIZE_IND_NEUT 受股本重建限制只有 75.2% 覆盖。"""
        num = den = 0.0
        for k, w in FB_W.items():
            if k == "SIZE_IND_NEUT":
                continue
            v = (raw.get(code6) or {}).get(k)
            if not isinstance(v, (int, float)):
                continue
            num += v * w
            den += w
        return (num / den) if den > 0 else None

    # v4.8 生产线公式（量程归一化）
    DMAX = {"score_fund": 5.0, "score_quality": 10.0, "score_tech_neg": 10.0,
            "score_bias60": 5.0, "score_rsi_rev": 5.0, "score_size": 10.0,
            "score_lowvol20": 10.0}
    W48 = {"score_fund": 0.15, "score_quality": 0.12, "score_lowvol20": 0.13,
           "score_tech_neg": 0.15, "score_bias60": 0.175, "score_rsi_rev": 0.175,
           "score_size": 0.10}

    def v48_score(r, d0):
        num = den = 0.0
        dvals = v48_val((r.get("code") or "")[-6:], r, d0)
        for k, w in W48.items():
            v = dvals.get(k)
            if not isinstance(v, (int, float)):
                continue
            mx = DMAX[k]
            num += min(float(v), mx) / mx * 10.0 * w
            den += w
        return (num / max(0.5, den) * 10.0) if den > 0 else None

    # 飞书 5 大类组合（按大类权重，类内等权）
    FB_CLS = {"质量": (0.35, ["ROE"]), "估值": (0.20, ["EP", "EVEBIT"]),
              "成长": (0.20, ["NAG", "OCFG"]), "量价": (0.15, ["REV1M", "SIZE_IND_NEUT"]),
              "一致预期": (0.10, ["SUE"])}

    def fb_cls_score(raw, code6):
        num = den = 0.0
        for cls, (w, mem) in FB_CLS.items():
            vs = [raw.get(code6, {}).get(m) for m in mem]
            vs = [v for v in vs if isinstance(v, (int, float))]
            if vs:
                num += w * (sum(vs) / len(vs))
                den += w
        return (num / den) if den > 0 else None

    def run_composite(fn, hop, require=None):
        """require: 若给出，则只保留该因子有值的股票（保证对比在同一样本上）。
        ⚠️ 这是本轮最容易出错的地方：飞书因子覆盖 86%~99%，v4.8 覆盖近全池。
           若各算各的样本，两边的 IC 差里混着「样本不同」的效应，差额不可解释。
           所以所有「飞书 vs v4.8」的对比都必须过 require 对齐样本。"""
        ics = []
        for s in snaps:
            d0 = s["date"]
            raw = raw_by_date.get(d0) or {}
            fv, rv = [], []
            for r in s["rows"]:
                c6 = (r.get("code") or "")[-6:]
                if require is not None:
                    rq = (raw.get(c6) or {}).get(require)
                    if not isinstance(rq, (int, float)):
                        continue
                v = fn(r, c6, d0, raw)
                if v is None:
                    continue
                fr = fwd_ret(c6, d0, hop)
                if fr is None:
                    continue
                fv.append(v)
                rv.append(fr)
            if len(fv) >= 40:
                ic = fe.rank_ic(fv, rv)
                if ic is not None:
                    ics.append(ic)
        return ic_stats(ics)

    # ===== 样本来齐口径：飞书综合分 / 5 大类 / v4.8 全部只在「8 因子全有值」的子样本上算
    #   SUE 覆盖最低（98.7%）→ 用它做卡口最严；但 SIZE_IND_NEUT 只有 75.2%，
    #   同时要求 8 因子全有会把样本砍到 75% → 那样 v4.8 基线也就被砍到 75%，
    #   它的 IC 会和全池口径不同。
    #   折中：主轴对比用「SIZE_IND_NEUT 有值」做卡口（覆盖最低的那个），
    #   另给一份「不卡口」的 v4.8 全池基线做参照，两者并列披露。
    q4 = {
        # 卡口口径（同一样本）：SIZE_IND_NEUT 有值才纳入
        "fbsimple_h5_gated": run_composite(lambda r, c, d, raw: fb_score(raw, c), 5, "SIZE_IND_NEUT"),
        "fbclass_h5_gated": run_composite(lambda r, c, d, raw: fb_cls_score(raw, c), 5, "SIZE_IND_NEUT"),
        "v48_h5_gated": run_composite(lambda r, c, d, raw: v48_score(r, d), 5, "SIZE_IND_NEUT"),
        "fbsimple_h20_gated": run_composite(lambda r, c, d, raw: fb_score(raw, c), 20, "SIZE_IND_NEUT"),
        "fbclass_h20_gated": run_composite(lambda r, c, d, raw: fb_cls_score(raw, c), 20, "SIZE_IND_NEUT"),
        "v48_h20_gated": run_composite(lambda r, c, d, raw: v48_score(r, d), 20, "SIZE_IND_NEUT"),
        # 全池口径（各自最大样本）：v4.8 的真实基线，用于核对 round25
        "v48_h5_full": run_composite(lambda r, c, d, raw: v48_score(r, d), 5),
        "v48_h20_full": run_composite(lambda r, c, d, raw: v48_score(r, d), 20),
        # 不计 size 维的飞书公式（SIZE_IND_NEUT 仅 5% 权重，去掉后可覆盖全样本）
        "fbsimple_nosz_h5": run_composite(lambda r, c, d, raw: _fb_score_nosz(raw, c), 5),
        "fbsimple_nosz_h20": run_composite(lambda r, c, d, raw: _fb_score_nosz(raw, c), 20),
    }

    # ⚠️ 基线一致性自检：本脚本的 v4.8 复合分必须在**全池**上复现已落地值
    #   round25 记录 v4.8 h5 IC = +0.1776 / h20 = +0.2484。
    #   若这里算出的数字不同，则说明数据重建路径有偏，后面所有差额都不能直接比。
    print("\n【Q4-a】基线一致性自检（v4.8 全池，本脚本 vs round25 已落地值）")
    _ref = {"h5": 0.1776, "h20": 0.2484}
    baseline_ok = {}
    for hop in (5, 20):
        got = (q4.get("v48_h%d_full" % hop) or {}).get("ic")
        ref = _ref["h%d" % hop]
        ok = got is not None and abs(got - ref) <= 0.02
        baseline_ok["h%d" % hop] = {"got": got, "ref_round25": ref, "within_0.02": ok}
        print("  h%-3d 本脚本=%s  已落地(round25)=%.4f  偏差=%s  容差0.02 %s" % (
            hop, f"{got:+.4f}" if got is not None else "—", ref,
            f"{got - ref:+.4f}" if got is not None else "—", "OK" if ok else "⚠️ 超差"))

    print("\n【Q4】飞书综合评分 vs 现行 v4.8（同一 78 截面、同一样本口径）")
    print("%-34s %6s %8s %8s %7s %7s %7s %7s" % ("方案", "hop", "IC", "ICIR", "NWt0", "NWt3", "NWt6", "IC>0"))
    rows4 = (
        ("[卡口] 飞书简化公式(8因子)", "fbsimple", "gated"),
        ("[卡口] 飞书5大类分", "fbclass", "gated"),
        ("[卡口] v4.8 现行7维", "v48", "gated"),
        ("[全池] 飞书简化公式(去size)", "fbsimple_nosz", ""),
        ("[全池] v4.8 现行7维", "v48", "full"),
    )
    for label, key, suffix in rows4:
        for hop in (5, 20):
            kk = "%s_h%d" % (key, hop) + ("_" + suffix if suffix else "")
            s2 = q4.get(kk)
            if not s2:
                continue
            print("%-34s %6d %8.4f %8.3f %7s %7s %7s %6.1f%%" % (
                label, hop, s2["ic"], s2["icir"] or 0, s2["t0"], s2["t3"], s2["t6"],
                s2["ic_pos_share"] * 100))

    # ---------------- Q5：飞书 5 大类逐类消融（看边际贡献）
    q5 = {}
    for drop in list(FB_CLS) + [None]:
        def mk(drop=drop):
            def fn(raw, code6):
                num = den = 0.0
                for cls, (w, mem) in FB_CLS.items():
                    if cls == drop:
                        continue
                    vs = [raw.get(code6, {}).get(m) for m in mem]
                    vs = [v for v in vs if isinstance(v, (int, float))]
                    if vs:
                        num += w * (sum(vs) / len(vs))
                        den += w
                return (num / den) if den > 0 else None
            return fn
        f = mk()
        # ⚠️ 必须与 Q4 一样做样本卡口，否则「去掉某类后 IC 变化」里混着样本扩大效应：
        #   去掉量价 → SIZE_IND_NEUT 不再参与 → 样本从 75% 覆盖扩到近全池 → IC 变化被高估。
        q5["drop_" + (drop or "none")] = run_composite(
            lambda r, c, d, raw: f(raw, c), 5, "SIZE_IND_NEUT")

    print("\n【Q5】飞书 5 大类消融（h5，样本卡口=SIZE_IND_NEUT 有值，与 Q4 同一样本）")
    base_ic = (q5.get("drop_none") or {}).get("ic") or 0.0
    for k, v in q5.items():
        if not v:
            continue
        print("%-14s IC=%+.4f  ICIR=%+.3f  t0=%+.2f  ΔIC(相对完整)=%+.4f" % (
            k.replace("drop_", "去掉 "), v["ic"], v["icir"] or 0, v["t0"] or 0, v["ic"] - base_ic))

    # ---------------- Q6：反转块的价值（飞书完全没有 vs 现行占 35%）
    q6 = {}
    rev_w = {"score_bias60": 0.175, "score_rsi_rev": 0.175}
    # 现行 v4.8 去掉反转块（0.35 权重按比例分给其余 5 维）
    rest = {k: v for k, v in W48.items() if k not in rev_w}
    rest_sum = sum(rest.values())
    W_no_rev = {k: v / rest_sum for k, v in rest.items()}

    def v48_norev_score(r, d0):
        num = den = 0.0
        dvals = v48_val((r.get("code") or "")[-6:], r, d0)
        for k, w in W_no_rev.items():
            v = dvals.get(k)
            if not isinstance(v, (int, float)):
                continue
            mx = DMAX[k]
            num += min(float(v), mx) / mx * 10.0 * w
            den += w
        return (num / max(0.5, den) * 10.0) if den > 0 else None

    for hop in (5, 20):
        q6["v48_full_h%d" % hop] = run_composite(lambda r, c, d, raw: v48_score(r, d), hop)
        q6["v48_norev_h%d" % hop] = run_composite(lambda r, c, d, raw: v48_norev_score(r, d), hop)
    print("\n【Q6】反转块（bias60+rsi_rev，现行 35% 权重）的价值 —— 飞书 5 大类里没有它们的独立位置")
    print("%-30s %6s %8s %8s %7s %7s" % ("方案", "hop", "IC", "ICIR", "NWt0", "IC>0"))
    for hop in (5, 20):
        for label, key in (("v4.8 含反转块(35%)", "v48_full"), ("v4.8 去反转块", "v48_norev")):
            s2 = q6.get("%s_h%d" % (key, hop))
            if not s2:
                continue
            print("%-30s %6d %8.4f %8.3f %7s %6.1f%%" % (
                label, hop, s2["ic"], s2["icir"] or 0, s2["t0"], s2["ic_pos_share"] * 100))
        a = (q6.get("v48_full_h%d" % hop) or {}).get("ic")
        b = (q6.get("v48_norev_h%d" % hop) or {}).get("ic")
        if a is not None and b is not None:
            print("   → 反转块贡献 ΔIC = %+.4f（含 − 不含）" % (a - b))

    # ---------------- 输出
    out = {
        "round": "round26",
        "purpose": "按飞书《量化选股评分完整方案》口径在本池复测 8 因子",
        "caveats": [
            "飞书要求全市场截面 Z-score；本项目无全市场面板，只能用**池内截面** Z-score —— 池子已被成交额TOP500筛选，剔除了绝对规模位置信息",
            "Winsorize 0.1%/99.9% 在本池（190-490 只/截面）几乎等同 min/max 截断，实际不生效",
            "EVEBIT 缺有息负债拆分 → 退化为 EBIT_TTM/总市值",
            "SUE 无分析师预期库 → 用财报单季扣非EPS对自身前4季标准化（time-series SUE，非 analyst SUE）",
            "SIZE_IND_NEUT 的行业用最近截面（2026-10-23）sector_parent 近似全历史",
            "行情类因子用 T 日收盘价，未计 T+1 开盘执行滑点 → 结论偏乐观",
        ],
        "n_snapshots": len(snaps),
        "date_range": [snaps[0]["date"], snaps[-1]["date"]],
        "q0_coverage": cov_summary,
        "q1_ic": q1,
        "q2_vs_csv": q2,
        "q3_corr_vs_v48": q3,
        "q4_composite": q4,
        "baseline_check": {
            "note": "本脚本 v4.8 全池复现 vs round25 已落地值。偏差必须≈0，否则本轮所有差额失效",
            "detail": baseline_ok,
            "passed": all(v.get("within_0.02") for v in baseline_ok.values()),
        },
        "q5_ablation": q5,
        "q6_reversal_value": q6,
        "clip_events": len(clip_note),
        "industry_group_dropped": {
            "n_snapshots_affected": len(ind_drop_note),
            "mean_dropped_groups": round(sum(x[1] for x in ind_drop_note) / max(1, len(ind_drop_note)), 1),
            "mean_dropped_stocks": round(sum(x[2] for x in ind_drop_note) / max(1, len(ind_drop_note)), 1),
            "note": "行业内样本<5 的申万一级行业被整体丢弃（Z-score 无统计意义），不静默",
        },
    }
    p = os.path.join(HERE, "audit_round26.json")
    json.dump(out, open(p, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"\n→ 已写出 {p}")


if __name__ == "__main__":
    main()
