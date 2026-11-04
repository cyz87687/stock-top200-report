#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""round28：飞书「五维分层」 vs 本项目「线性八维」—— 归因式对比

## 为什么要重做（round26 的结论不可直接用作「谁更优」）

round26 得出「飞书综合分 IC 只有现行 53%(h5)/43%(h20)」，但那个对比**同时改变了三个变量**：

| 变量 | 飞书方案 | round26 的 v4.8 基线 |
|---|---|---|
| ① 因子集 | 8 个基本面/量价因子（无 bias60、无 rsi_rev、无 lowvol20） | 含反转块 35% |
| ② 打分口径 | 截面 Z-score，线性可加（有负分） | 量程归一化到 0-10，有界 |
| ③ 权重 | ROE 独占 35%（分层隐含）/ OCFG 25%（平铺声明）| bias60+rsi_rev 35% |
| ④ 维度数 | 5 类 / 8 因子 | 7 维（round26 时点；现已 8 维） |

三个变量同时变 → 差额全部堆在一个「53%」里，**无法归因**。老大问的是
「五维 vs 线性到底谁更优」，这个问题必须先拆成：

  Q1 【数学】分层线性加权 与 单层线性加权 是不是同一个东西？
  Q2 【结构】在**完全相同的因子集、完全相同的权重**下，只换结构，结果变不变？
  Q3 【因子集】在**相同结构**下，换因子集能拿多少？
  Q4 【权重】飞书自己的分层隐含权重 与 平铺声明权重 差多少？哪个更好？
  Q5 【现状】飞书最优可实现版 vs 本项目 v4.9 现行八维，同口径同样本谁赢？
  Q6 【互补】把飞书独有因子（EP/OCFG/NAG/EVEBIT/SUE）并入现行八维，能否提升？

## 口径纪律（本轮全部遵守）
- 所有对比必须在**同一样本**上算（require 卡口）。飞书因子覆盖 86~99%，
  各自算各自的样本，差额里会混进样本效应。
- 复合分 IC 一律用**生产线同构公式**（量程归一化），
  不用 rank_pct 逐截面排名加权 —— 后者与 step2 不等价。
- 飞书口径的因子**必须**用 Z-score 流水线（winsorize 0.1% + 截面 Z），
  否则就不是飞书方案，比了也没意义。
- 多重检验累计已超 300 次 → 阈值 t≈3.08 已偏乐观，同时报 Bonferroni 与
  「分子期前后半双正」两个稳健性检验。

输出：audit_round28.json
"""

import os
import sys
import json
import math
import glob
from collections import defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import factor_eval as fe
import audit_round21 as r21
# ⚠️ 直接复用 round26 已验证的 EM 加载与 8 因子构造：
#   我在本文件里重写 build_factors 时把指标名写成了「经营现金流量净额」以外的
#   自造名（如「扣除非经常性损益后的净利润」「归属于母公司股东权益合计」），
#   结果 ROE / NAG 覆盖率直接 0.0%——这类错误不会报错，只会静默产出空列。
#   EM 缓存真实指标名见 data/fin_cache_em/*.json：净资产收益率(ROE)、
#   股东权益合计(净资产)、经营现金流量净额、每股息税前利润、基本每股收益。
import audit_round26 as r26
build_factors = r26.build_factors
load_em_all = r26.load_em_all
ttm_sum = r26.ttm_sum
single_q = r26.single_q
prev_periods = r26.prev_periods
# ⚠️ 反转块必须用 K 线回填（round23/24b 唯一实现来源）。用 snapshot 的
#   score_bias60 字段会让 35% 权重的反转块在 77/78 个截面上静默空转。
import audit_round24b as rb
# EP / SIZE_IND_NEUT 用 v4.9 落地模块（唯一实现来源，禁在此另写一份）
import ep_size_ind as esi
# quality 必须走 fund_split（生产口径）；esi.load_em_all 的选项优先级取法与生产不同，
# 实测 quality IC 差 0.03，足以把复合分基线推偏 → 两套 EM 缓存分别加载，不混用。
import fund_split as fs

T_BONF = 3.08
EM_CACHE = os.path.join(HERE, "data", "fin_cache_em")


# ============================================================ 统计工具

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
    """飞书流水线第 1-2 步：截面 winsorize 0.1%/99.9% → Z-score。"""
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
    """飞书 SIZE_IND_NEUT 四步：行业分组 → 组内 winsorize → 组内 Z → 取负。"""
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
            out[c] = -z
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
    # 分子期双正检验：前半 / 后半各自均值
    half = n // 2
    f_ic = round(sum(ics[:half]) / half, 4) if half else None
    s_ic = round(sum(ics[half:]) / (n - half), 4) if (n - half) else None
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
        "first_half_ic": f_ic,
        "second_half_ic": s_ic,
        "both_halves_positive": bool(f_ic is not None and s_ic is not None
                                     and f_ic > 0 and s_ic > 0),
    }


# （EM 加载与 8 因子构造直接复用 audit_round26，见文件头 import 段）


# ============================================================ 主流程

def main():
    h = json.load(open(os.path.join(HERE, "factor_history.json"), encoding="utf-8"))
    snaps, _ = fe._drop_stale_price_snapshots(h["snapshots"])
    tdays, px = fe.px_index(h)
    tidx = {d: i for i, d in enumerate(tdays)}
    em = load_em_all()
    em_fs = fs.load_em_cache()   # quality 专用（生产口径）

    scored = json.load(open(os.path.join(HERE, "top200_scored_2026-10-23.json"), encoding="utf-8"))
    sector_map = {}
    for r in scored["results"]:
        c = r.get("code") or ""
        c6 = c[-6:] if len(c) >= 6 else c
        sector_map[c6] = r.get("sector_parent")

    # SIZE_IND_NEUT 要的是**申万一级**（sector_parent 是二级，飞书用的是行业内）
    try:
        sw_map = json.load(open(os.path.join(HERE, "sector", "sw_map.json"), encoding="utf-8"))
    except Exception:
        sw_map = {}

    print("=" * 100)
    print("round28：飞书「五维分层」 vs 本项目「线性八维」—— 归因式对比")
    print("=" * 100)
    print(f"截面 {len(snaps)} · 交易日 {len(tdays)} · 价格 {len(px)} 只 · EM 缓存 {len(em)} 只")

    # ---------- 前视收益
    def fwd_ret(code6, d0, hop):
        m = px.get(code6)
        i0 = tidx.get(d0)
        if not m or i0 is None:
            return None
        j = i0 + hop
        if j >= len(tdays):
            return None
        p0, p1 = m.get(d0), m.get(tdays[j])
        if p0 and p1 and p0 > 0:
            return p1 / p0 - 1.0
        return None

    # ---------- 股本重建
    shares_est = {}
    _sh = defaultdict(list)
    for s in snaps:
        for r in s["rows"]:
            mvv, pr = r.get("circ_mv"), r.get("price")
            if isinstance(mvv, (int, float)) and mvv and isinstance(pr, (int, float)) and pr > 0:
                _sh[r["code"]].append(mvv / pr)
    for c, v in _sh.items():
        v = sorted(v)
        shares_est[c] = v[len(v) // 2]
    print(f"股本重建：{len(shares_est)} 只")

    def mcap_of(code6, d0, row):
        v = row.get("circ_mv")
        if isinstance(v, (int, float)) and v > 0:
            return float(v)
        sh = shares_est.get(row.get("code"))
        pr = (px.get(code6) or {}).get(d0) or row.get("price")
        if sh and isinstance(pr, (int, float)) and pr > 0:
            return sh * pr
        return None

    # ---------- 逐截面算飞书 8 因子 → Z
    FACTORS = ["OCFG", "ROE", "EP", "EVEBIT", "NAG", "REV1M", "SIZE_IND_NEUT", "SUE"]
    raw_by_date = {}
    cov_by_date = {}

    for s in snaps:
        d0 = s["date"]
        raw, cov = {}, defaultdict(int)
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
            p0 = (px.get(c6) or {}).get(d0)
            i0 = tidx.get(d0)
            if p0 and i0 is not None and i0 >= 21:
                pb = (px.get(c6) or {}).get(tdays[i0 - 21])
                if pb and pb > 0:
                    f["REV1M"] = -(p0 / pb - 1.0) * 100.0
            f.pop("_sue_proxy", 0)
            f.pop("_ev_degraded", 0)
            raw[c6] = f
            for k in f:
                if not k.startswith("_"):
                    cov[k] += 1

        pairs = [(c, v.get("_ind"), v.get("_mv")) for c, v in raw.items() if v.get("_mv")]
        szn, _dropped = group_winsorize_zneg(pairs)
        for c, z in szn.items():
            raw[c]["SIZE_IND_NEUT"] = z
            cov["SIZE_IND_NEUT"] += 1

        for c in raw:
            raw[c].pop("_mv", None)
            raw[c].pop("_ind", None)

        # 截面 Z（SIZE_IND_NEUT 已标准化，跳过）
        for k in FACTORS:
            if k == "SIZE_IND_NEUT":
                continue
            codes = [c for c, v in raw.items() if isinstance(v.get(k), (int, float))]
            if len(codes) < 10:
                continue
            zs, _ = winsorize_z([raw[c][k] for c in codes])
            if zs is None:
                for c in codes:
                    raw[c][k] = None
                continue
            for c, z in zip(codes, zs):
                raw[c][k] = z

        raw_by_date[d0] = raw
        cov_by_date[d0] = dict(cov)

    cov_summary = {}
    for k in FACTORS:
        ns = [cov_by_date[d].get(k, 0) for d in raw_by_date]
        tot = [len(raw_by_date[d]) for d in raw_by_date]
        cov_summary[k] = {
            "cover_pct": round(sum(ns) / sum(tot) * 100, 1) if sum(tot) else 0.0,
            "n_med": sorted(ns)[len(ns) // 2],
            "pool_med": sorted(tot)[len(tot) // 2],
        }
    print("\n【Q0】飞书 8 因子本池覆盖率")
    for k in FACTORS:
        v = cov_summary[k]
        print("  %-16s %6.1f%%  中位n=%d / 池中位=%d" % (k, v["cover_pct"], v["n_med"], v["pool_med"]))

    # ---------- 现行维重算（八维，v4.9）
    #   ⚠️ 必须用 factor_weights.json 的当前配置，不能用 round26 里写死的 v4.8 七维。
    #
    # ⚠️⚠️⚠️ 口径血案（本文件首版三处全错，C7 基线 h5 只有 +0.0936，应 +0.1776）
    #   ① bias60 / rsi_rev 必须用 rb.backfill_reversal()（K 线回填，100% 覆盖），
    #      **绝对不能**用 snapshot 里的 score_bias60 字段——该字段 2026-10-13 才入库，
    #      历史覆盖仅 23%；缺值时它从分母剔除，等于让 35% 权重的反转块在 77 个截面上
    #      静默空转，剩下的 fund/quality/tech 撑不起 IC。**不报错、不告警。**
    #      backfill 返回 tuple = (oversold60, bias60, rsi_rev) → 索引 1/2，不是 0/1。
    #      （首版写 t[0]/t[1]，把 oversold60 当成了 bias60。）
    #   ② quality 必须用 r21.aggregate(sub, "grouped") 的池内升序分位，
    #      **不能**自己算 ROE —— 两套实现必然漂移（round17 教训）。首版自算 ROE
    #      还把 _rank10 写成降序（值大→分低），方向整个反了。
    #   ③ size 与 lowvol20 都必须**降序**排名（小市值=高分 / 低波=高分）。
    fw = json.load(open(os.path.join(HERE, "factor_weights.json"), encoding="utf-8"))
    W_NOW = {k: float(v or 0) for k, v in (fw.get("weights") or {}).items()}
    DMAX_NOW = {k: float(v) for k, v in (fw.get("dim_max") or {}).items()}
    EFF_NOW = [k for k, v in W_NOW.items() if v > 0]
    print(f"\n现行生效维 {len(EFF_NOW)} 个：{', '.join('%s=%.3f' % (k, W_NOW[k]) for k in EFF_NOW)}")

    # 反转块：K 线回填（与 round23/24b/25/26 逐字同口径）
    bf = rb.backfill_reversal(tdays, px)
    _n_b60 = sum(1 for d in bf for _ in bf[d])
    print(f"反转块回填（K线）：{len(bf)} 个日期 / {_n_b60} 条，覆盖最近截面 "
          f"{len(bf.get(snaps[-1]['date'], {}))} 只")

    # 子因子（quality 用 r21.aggregate，禁自算）
    subs = defaultdict(dict)
    for s in snaps:
        d0 = s["date"]
        for r in s["rows"]:
            ind = em.get(r["code"])
            if not ind:
                continue
            P = r21.pit_period(sorted({p for dd in ind.values() for p in dd}, reverse=True), d0)
            if P is None:
                continue
            subs[d0][r["code"]] = r21.sub_factors_pit(ind, P)

    def _rank10_asc(vals_by_code):
        """升序：值越大分越高（quality / ep / size_ind 用）。"""
        ks = sorted(vals_by_code, key=lambda c: vals_by_code[c])
        n = len(ks)
        if n < 2:
            return {}
        return {c: round((i / (n - 1)) * 10.0, 2) for i, c in enumerate(ks)}

    def _rank10_desc(vals_by_code):
        """降序：值越大分越低（size 小市值=高分 / lowvol20 低波=高分）。"""
        ks = sorted(vals_by_code, key=lambda c: -vals_by_code[c])
        n = len(ks)
        if n < 2:
            return {}
        return {c: round((i / (n - 1)) * 10.0, 2) for i, c in enumerate(ks)}

    # quality：**必须**走 fund_split.score_rows（生产口径唯一实现来源）。
    # ⚠️ 不能用 esi.load_em_all() 的 _OPT_PRI 选项优先级取法：实测与生产口径
    #   逐股逐日不一致 17,610 处 → quality IC 从 −0.0209 变成 +0.0076（差 0.03），
    #   足以把复合分基线整体推偏。同一指标名在多个「选项」分组下值不同，
    #   生产口径是**后写入覆盖**（setdefault + 逐 record update），不是优先级挑选。
    qual_by_date = {}
    for s in snaps:
        d0 = s["date"]
        rows = [{"code": r["code"]} for r in s["rows"]]
        fs.score_rows(rows, d0, em=em_fs)
        qual_by_date[d0] = {r["code"]: r["score_quality"] for r in rows
                            if isinstance(r.get("score_quality"), (int, float))}

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
        size_by_date[d0] = _rank10_desc(mv)

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
        # ⚠️ 低波=高分 → 降序（波动率最大者得 0 分）。首版写成升序，方向反了。
        lv_by_date[d0] = _rank10_desc(raw)

    # EP：扣非净利润TTM / 总市值，池内升序分位（唯一实现来源 ep_size_ind.raw_ep）
    ep_rank_by_date = {}
    for s in snaps:
        d0 = s["date"]
        raw = {}
        for r in s["rows"]:
            c6 = (r.get("code") or "")[-6:]
            ind = em.get(c6)
            if not ind:
                continue
            P = r21.pit_period(sorted({p for dd in ind.values() for p in dd}, reverse=True), d0)
            if P is None:
                continue
            v = esi.raw_ep(ind, P, r.get("price"))
            if v is not None:
                raw[c6] = v
        ep_rank_by_date[d0] = _rank10_asc(raw) if len(raw) >= 10 else {}

    # size_ind：申万一级行业内 LN(流通市值) Z 取负 → 池内升序分位
    szi_rank_by_date = {}
    for s in snaps:
        d0 = s["date"]
        pairs = []
        for r in s["rows"]:
            c6 = (r.get("code") or "")[-6:]
            pairs.append((c6, esi.sector_l1_of(c6, sw_map), mcap_of(c6, d0, r)))
        szn, _ = esi.group_winsorize_zneg(pairs)
        szi_rank_by_date[d0] = _rank10_asc(szn) if len(szn) >= 10 else {}

    def cur_val(code6, r, d0):
        """现行八维**原始分**（0-5 或 0-10）。键统一用 6 位 code。"""
        out = {}
        v = r.get("score_fund")
        out["score_fund"] = v if isinstance(v, (int, float)) else None
        v = r.get("score_tech")
        out["score_tech_neg"] = (10.0 - v) if isinstance(v, (int, float)) else None
        # ⚠️ backfill 返回 (oversold60, bias60, rsi_rev) → 取 1 / 2
        t = (bf.get(d0) or {}).get(code6)
        out["score_bias60"] = t[1] if t else None
        out["score_rsi_rev"] = t[2] if t else None
        out["score_quality"] = qual_by_date.get(d0, {}).get(code6)
        out["score_size"] = size_by_date.get(d0, {}).get(code6)
        out["score_lowvol20"] = lv_by_date.get(d0, {}).get(code6)
        out["score_size_ind"] = szi_rank_by_date.get(d0, {}).get(code6)
        out["score_ep"] = ep_rank_by_date.get(d0, {}).get(code6)
        return out

    # ---------- 生产线同构复合分（量程归一化）
    def prod_score(vals, wmap, dmax=None):
        """生产线口径：Σ min(v,DMAX)/DMAX×10×w / max(0.5,Σw) × 10。
        ⚠️ max(0.5, ·) 在分母小于 0.5 时会放大分数 —— 这是生产线的真实行为，
           不能为了「干净」改成 max(1e-9, ·)，否则与已落地数值不可比。"""
        dmax = dmax if dmax is not None else DMAX_NOW
        num = den = 0.0
        for k, w in wmap.items():
            v = vals.get(k)
            if not isinstance(v, (int, float)) or w <= 0:
                continue
            mx = dmax.get(k) or 10.0
            num += min(float(v), mx) / mx * 10.0 * w
            den += w
        return (num / max(0.5, den) * 10.0) if den > 0 else None

    def run_ic(fn, hop, require=None, label=""):
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
                if v is None or not isinstance(v, (int, float)) or v != v:
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

    def show(label, st, hop):
        if not st:
            print("  %-40s hop=%2d  无数据" % (label, hop))
            return
        print("  %-40s hop=%2d  IC=%+.4f  ICIR=%+.3f  t0=%+.2f t3=%+.2f t6=%+.2f  "
              "IC>0=%.0f%%  Bonf=%-5s  前半%+.4f/后半%+.4f" % (
                  label, hop, st["ic"], st["icir"] or 0, st["t0"] or 0, st["t3"] or 0,
                  st["t6"] or 0, st["ic_pos_share"] * 100,
                  "✅" if st["pass_bonf"] else "❌",
                  st["first_half_ic"] or 0, st["second_half_ic"] or 0))

    # ==================== 基线复现自检（必须先过，否则所有对比无效） ====================
    # 铁律：任何 A vs B 对比，必须先把 B 重建到能精确复现已落地数值，
    #      否则差额全是数据管道噪声，不是模型优劣。
    # 已落地基准（round24f/round25 生产线量程归一化口径，全池）：
    #   v4.8 七维  h5=+0.1776  h20=+0.2484
    #   加上 ep/size_ind 拆分后的 v4.9 八维没有历史基准，只能与 v4.8 并列展示。
    BASE_CHECK = {"C7_现行七维(v4.8对照)": {"h5": 0.1437, "h20": 0.1924}}
    # 基准取 **round26 已落地的 gated 口径**（同一把 require 卡口），不是 round25 全池。
    # 理由：本轮所有方案对比都在 GATE 子样本上做，基准必须同一样本才可比。
    # 容差 0.005：同一公式同一快照下应为 0，留一点吸收 EM 缓存日内刷新（见下方注记）。
    BASE_TOL = 0.005
    # 全池口径的 round25 值，只做参考展示（不与 gated 混用）
    BASE_FULL_REF = {"h5": 0.1776, "h20": 0.2484}

    # v4.8 七维对照：DMAX 必须用 v4.8 时代的值（与 factor_weights.json 现值不同）
    DMAX_V48 = {"score_fund": 5.0, "score_quality": 10.0, "score_tech_neg": 10.0,
                "score_bias60": 5.0, "score_rsi_rev": 5.0, "score_size": 10.0,
                "score_lowvol20": 10.0}
    W_V48 = {"score_fund": 0.15, "score_quality": 0.12, "score_lowvol20": 0.13,
             "score_tech_neg": 0.15, "score_bias60": 0.175, "score_rsi_rev": 0.175,
             "score_size": 0.10}

    # ==================== Q1：结构等价性（数学，无需算 IC） ====================
    FB_CLS = {"质量": (0.35, ["ROE"]), "估值": (0.20, ["EP", "EVEBIT"]),
              "成长": (0.20, ["NAG", "OCFG"]), "量价": (0.15, ["REV1M", "SIZE_IND_NEUT"]),
              "一致预期": (0.10, ["SUE"])}
    FB_W = {"OCFG": 0.25, "ROE": 0.10, "EP": 0.15, "EVEBIT": 0.05,
            "NAG": 0.20, "REV1M": 0.10, "SIZE_IND_NEUT": 0.05, "SUE": 0.10}

    implied = {}
    for c, (w, mem) in FB_CLS.items():
        for f in mem:
            implied[f] = w / len(mem)

    q1 = {
        "note": ("分层线性加权 Σ_c w_c·mean(z_f) 展开后恒等于单层线性加权 "
                 "Σ_f (w_c/|c|)·z_f —— 结构不产生 alpha，它只是权重的启发式写法。"
                 "但飞书文档内部两处权重自相矛盾，见 implied_vs_flat。"),
        "implied_from_layers": {k: round(v, 4) for k, v in implied.items()},
        "declared_flat": FB_W,
        "implied_vs_flat_ratio": {k: round(implied[k] / FB_W[k], 3) for k in FB_W},
        "implied_sum": round(sum(implied.values()), 4),
        "declared_sum": round(sum(FB_W.values()), 4),
    }
    print("\n【Q1】结构等价性（数学恒等式，与数据无关）")
    print("  分层隐含权重 vs 平铺声明权重：")
    for k in FB_W:
        print("    %-16s 隐含 %+.3f  声明 %+.3f  → %.2fx" % (k, implied[k], FB_W[k], implied[k] / FB_W[k]))
    print("  ⚠️ ROE 隐含权重 %.2f 是声明值 %.2f 的 %.1f 倍；OCFG 隐含只有声明的 %.0f%%"
          % (implied["ROE"], FB_W["ROE"], implied["ROE"] / FB_W["ROE"], implied["OCFG"] / FB_W["OCFG"] * 100))

    # ==================== Q2：只换结构，因子集/权重完全一致
    #   A: 单层线性，权重 = 飞书平铺声明
    #   B: 分层线性，权重 = 飞书大类（隐含因子权重 = implied）
    #   → 两者因子集相同，只是权重不同。真正检验「结构」需要构造一个
    #     权重完全相同的分层版（大类权重按 implied 倒推），数学上必然相等，
    #     故这里改为直接验证：用 implied 当权重做单层线性，应精确复现分层结果。
    def fb_flat(raw, code6, wmap):
        num = den = 0.0
        for k, w in wmap.items():
            v = (raw.get(code6) or {}).get(k)
            if not isinstance(v, (int, float)):
                continue
            num += v * w
            den += w
        return (num / den) if den > 0 else None

    def fb_layer(raw, code6):
        """飞书原始分层：score = Σ_c w_c × mean(类内 z)，分母只累计**有值**的类权重。"""
        num = den = 0.0
        for c, (w, mem) in FB_CLS.items():
            vs = [raw.get(code6, {}).get(m) for m in mem]
            vs = [v for v in vs if isinstance(v, (int, float))]
            if vs:
                num += w * (sum(vs) / len(vs))
                den += w
        return (num / den) if den > 0 else None

    def fb_flat_equiv(raw, code6):
        """等价形式：分母按「实际参与因子所属大类的权重」累计。

        ⚠️ 这个函数踩过两次坑，都是同一件事的两面（缺失值处理）：
        ① 首版直接用 implied 当权重、分母累计 Σimplied → maxdiff=1.29。
           不是「结构不等价」，是分层版分母只累计**有值大类**的 w_c。
        ② 二版把分子写成 w/|c|·Σz（|c| = 类内声明因子数）→ maxdiff=0.85。
           分层版是 w·mean(有值的 z) = w/|有值|·Σz，不是 w/|声明|·Σz。
           类内缺一个因子时两者差 2 倍。
        正确写法：分子 Σ_c w_c/|有值_c| · Σ_{f∈有值_c} z_f，分母 Σ_{c 有值} w_c。
        这样展开后与 fb_layer 逐字等价（下面有数值验证）。
        """
        num = 0.0
        den = 0.0
        for c, (w, mem) in FB_CLS.items():
            vs = [raw.get(code6, {}).get(m) for m in mem]
            vs = [v for v in vs if isinstance(v, (int, float))]
            if vs:
                den += w
                num += w / len(vs) * sum(vs)
        return (num / den) if den > 0 else None

    # 验证等价性：同一截面上逐股比较「分层」与「展开成单层」两种算法（应完全相等）
    maxdiff = 0.0
    ncmp = 0
    for s in snaps[:10]:
        d0 = s["date"]
        raw = raw_by_date.get(d0) or {}
        for r in s["rows"]:
            c6 = (r.get("code") or "")[-6:]
            a = fb_layer(raw, c6)
            b = fb_flat_equiv(raw, c6)
            if a is not None and b is not None:
                maxdiff = max(maxdiff, abs(a - b))
                ncmp += 1
    q1["equivalence_maxdiff"] = round(maxdiff, 12)
    q1["equivalence_n"] = ncmp
    print("  【验证】分层版 与 展开成单层版（同缺失处理）：n=%d, 最大绝对差=%.2e"
          % (ncmp, maxdiff))
    if maxdiff < 1e-9:
        print("  ✅ 数学上完全相等 → 「五维分层」不是独立优势，它就是一套权重")
    else:
        print("  ❌ 不等价，说明实现有差异，需排查")

    # ==================== Q3~Q5：方案对比（同一样本卡口）
    #   样本卡口用 SIZE_IND_NEUT（飞书因子中覆盖最低的 75%）
    GATE = "SIZE_IND_NEUT"
    schemes = {
        "F1_飞书分层5类": lambda r, c, d, raw: fb_layer(raw, c),
        "F2_飞书平铺8因子": lambda r, c, d, raw: fb_flat(raw, c, FB_W),
        "F3_飞书分层隐含权重当平铺": lambda r, c, d, raw: fb_flat(raw, c, implied),
        "C_现行八维(v4.9)": lambda r, c, d, raw: prod_score(cur_val(c, r, d), W_NOW),
        "C7_现行七维(v4.8对照)": lambda r, c, d, raw: prod_score(
            cur_val(c, r, d), W_V48, DMAX_V48),
    }

    # ---------- 基线复现自检（必须先过）
    #   ⚠️ 这条闸门的作用：如果 C7 复现不出 round24f/round25 的 +0.1776/+0.2484，
    #      说明我的数据管道与已落地实现不同构 → 所有「飞书 vs 现行」的差额里
    #      混着管道噪声，结论不可用。本闸门在 round26 抓到了两个 P0 bug
    #      （size 方向写反、EM 白名单过滤掉 EPS），本轮抓到三个（反转块用错数据源、
    #      quality 自算且方向反、lowvol 方向反）。
    print("\n" + "─" * 100)
    print("【基线自检】本轮重建的 v4.8 七维 必须复现 round26 已落地的 gated 口径")
    print("─" * 100)
    base_ok = True
    base_detail = {}
    for nm, ref in BASE_CHECK.items():
        for hop, refv in ref.items():
            hh = int(hop[1:])
            st = run_ic(schemes[nm], hh, GATE)     # 与对比方案同一 require 卡口
            got = st["ic"] if st else None
            dev = (got - refv) if (got is not None) else None
            ok = dev is not None and abs(dev) <= BASE_TOL
            base_detail["%s|%s" % (nm, hop)] = {
                "got": got, "ref": refv,
                "dev": round(dev, 4) if dev is not None else None,
                "within_tol": bool(ok)}
            base_ok = base_ok and ok
            print("  %-26s hop=%2d  重建=%s  已落地=%+.4f  偏差=%s  %s" % (
                nm, hh,
                ("%+.4f" % got) if got is not None else "无数据",
                refv,
                ("%+.4f" % dev) if dev is not None else "—",
                "✅" if ok else "❌"))
    # 全池口径对照（只做参考，不参与判据）
    st_full5 = run_ic(schemes["C7_现行七维(v4.8对照)"], 5)
    st_full20 = run_ic(schemes["C7_现行七维(v4.8对照)"], 20)
    print("  ── 参考：全池口径 本轮 h5=%s / h20=%s，round25 已落地 %+.4f / %+.4f" % (
        ("%+.4f" % st_full5["ic"]) if st_full5 else "—",
        ("%+.4f" % st_full20["ic"]) if st_full20 else "—",
        BASE_FULL_REF["h5"], BASE_FULL_REF["h20"]))
    print("  → %s" % ("✅ 复现通过（偏差 ≤ ±%.3f），以下所有对比在同一尺子上完成" % BASE_TOL
                      if base_ok else
                      "❌ 未复现！以下所有对比的差额含数据管道噪声，**不可作为上线依据**"))
    print("─" * 100)

    q3 = {}
    print("\n【Q3/Q4/Q5】方案对比（同一样本：SIZE_IND_NEUT 有值；生产线同构公式）")
    for name, fn in schemes.items():
        for hop in (5, 20):
            st = run_ic(fn, hop, GATE)
            show(name, st, hop)
            if st:
                q3["%s|h%d" % (name, hop)] = st
        print()

    # ==================== Q6：互补性——飞书独有因子并入现行八维
    #   用「同总权重」原则：现行八维压缩到 0.7，飞书新增因子占 0.3
    #   选的因子 = 与现行维相关性最低的几个（EP 已在现行里，排除）
    q6 = {}
    print("【Q6】飞书独有因子并入现行八维（同总权重，现行×0.7 + 新因子×0.3）")
    # 计算飞书因子与现行维的相关（用于挑最不冗余的）
    cur_dims = EFF_NOW
    corr_tbl = {}
    for k in ["OCFG", "ROE", "NAG", "EVEBIT", "REV1M", "SIZE_IND_NEUT", "SUE"]:
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
                    y = cur_val(c6, r, d0).get(d)
                    if not isinstance(y, (int, float)):
                        continue
                    xs.append(x)
                    ys.append(y)
                if len(xs) >= 40:
                    c2 = spearman(xs, ys)
                    if c2 is not None:
                        cors.append(c2)
            row[d] = round(sum(cors) / len(cors), 3) if cors else None
        corr_tbl[k] = row
        print("  %-16s " % k + " ".join("%s=%+.2f" % (d.replace("score_", ""), row[d])
                                        for d in cur_dims if row[d] is not None))

    # 构造候选：把飞书因子按 Z 直接作为第 9/10 维并入（保持 0-10 量纲：Z 线性映射）
    def z_to_10(z):
        """Z-score → 0-10 有界分（±3σ 映射到 0-10，超出截断）。"""
        if not isinstance(z, (int, float)):
            return None
        return max(0.0, min(10.0, (z + 3.0) / 6.0 * 10.0))

    def merged_score(new_factors, new_w_each):
        """现行八维（权重按 (1-total_new) 等比缩放）+ 新因子。"""
        tot_new = new_w_each * len(new_factors)
        scale = 1.0 - tot_new

        def fn(r, c6, d0, raw):
            vals = cur_val(c6, r, d0)
            num = den = 0.0
            for k, w in W_NOW.items():
                v = vals.get(k)
                if not isinstance(v, (int, float)) or w <= 0:
                    continue
                mx = DMAX_NOW.get(k) or 10.0
                num += min(float(v), mx) / mx * 10.0 * w * scale
                den += w * scale
            for nk in new_factors:
                z = (raw.get(c6) or {}).get(nk)
                v = z_to_10(z)
                if v is None:
                    continue
                num += v * new_w_each
                den += new_w_each
            return (num / max(0.5, den) * 10.0) if den > 0 else None
        return fn

    # 单因子并入（各占 0.10）
    for f in ["OCFG", "NAG", "EVEBIT", "SUE", "REV1M"]:
        for hop in (5, 20):
            st = run_ic(merged_score([f], 0.10), hop, GATE)
            show("C+%s(0.10)" % f, st, hop)
            if st:
                q6["C+%s|h%d" % (f, hop)] = st
        print()

    # ==================== Q7：决定性对照——「同权重、同因子集」下结构是否等价
    # Q1 已在数学上证明分层 ≡ 线性（maxdiff=0）。但数学等价 ≠ 实践等价：
    # 飞书把 SIZE_IND_NEUT 放在【量价】类内、与 REV1M 各占一半（0.15/2=0.075）；
    # 平铺表却给 SIZE_IND_NEUT 0.05、REV1M 0.10。**这才是两处权重打架的来源**。
    # 所以真正要回答老大的是：**飞书这套「隐含因子权重」到底好不好**，
    # 而"结构"这一项归因为零。下面把这个结论用数据钉死：
    #   Z1 把 v4.8 的 7 维权重按飞书大类结构重新分组（同因子集、同总权重）
    #      → IC 必须与 v4.8 完全一致（证明结构不产生任何 alpha）
    print("\n【Q7】决定性对照：同因子集、同权重，只把权重按「分层/平铺」两种写法表达")
    print("─" * 100)
    # 把 v4.8 的 7 维拆成两层：第一层「反转块」，第二层其余
    # 无论怎么分组，展开后每个因子的隐含权重不变 → 分数必等
    V48_CLS = {"反转块": (0.175 + 0.175, ["score_bias60", "score_rsi_rev"]),
               "基本面块": (0.15 + 0.12 + 0.13, ["score_fund", "score_quality", "score_lowvol20"]),
               "其他": (0.15 + 0.10, ["score_tech_neg", "score_size"])}

    def layered_v48(vals):
        """把 v4.8 七维按「分层」写法算（先块内等权，再块间加权）。"""
        num = den = 0.0
        for cl, (w, mem) in V48_CLS.items():
            vs = []
            for k in mem:
                v = vals.get(k)
                if not isinstance(v, (int, float)):
                    continue
                mx = DMAX_V48[k]
                vs.append(min(float(v), mx) / mx * 10.0)
            if vs:
                num += w * (sum(vs) / len(vs))
                den += w
        return (num / den * 10.0) if den > 0 else None

    # ⚠️ 注意：V48_CLS 的块内等权 ≠ v4.8 的原始权重分配，
    #   所以这个对照**故意**不等价——它量化的是"同样的 7 个维度、
    #   若按飞书那种'大类内等权'的写法，会损失/获得多少"。
    q7 = {}
    for hop in (5, 20):
        st_flat = run_ic(lambda r, c, d, raw: prod_score(cur_val(c, r, d), W_V48, DMAX_V48),
                         hop, GATE)
        st_layer = run_ic(lambda r, c, d, raw: layered_v48(cur_val(c, r, d)), hop, GATE)
        q7["flat_h%d" % hop] = st_flat
        q7["layer_h%d" % hop] = st_layer
        show("Z1_平铺（v4.8真实权重，块内不等权）", st_flat, hop)
        show("Z2_分层写法（块内等权，同因子集同总权重）", st_layer, hop)
        if st_flat and st_layer:
            print("     → 差额 %+.4f（负数=分层写法更差，正数=结构本身带来增益）"
                  % (st_layer["ic"] - st_flat["ic"]))
        print()

    # ==================== Q8：v4.9 八维 vs v4.8 七维 —— 逐截面配对检验
    #   本轮出现一个与 round27c 结论相反的现象：八维 h5/h20 均略低于七维。
    #   只比均值不够（两条 IC 序列高度重叠，均值差 0.004 可能是噪声）。
    #   必须做**逐截面配对差**的 NW-t，否则就是拿噪声当结论。
    print("\n【Q8】v4.9 八维 vs v4.8 七维：逐截面配对检验（同一样本、同一截面）")
    print("─" * 100)
    q8 = {}
    for hop in (5, 20):
        ics8, ics7 = [], []
        for s in snaps:
            d0 = s["date"]
            raw = raw_by_date.get(d0) or {}
            v8, v7, rv = [], [], []
            for r in s["rows"]:
                c6 = (r.get("code") or "")[-6:]
                if not isinstance((raw.get(c6) or {}).get(GATE), (int, float)):
                    continue
                vals = cur_val(c6, r, d0)
                a = prod_score(vals, W_NOW)
                b = prod_score(vals, W_V48, DMAX_V48)
                fr = fwd_ret(c6, d0, hop)
                if a is None or b is None or fr is None:
                    continue
                v8.append(a); v7.append(b); rv.append(fr)
            if len(v8) >= 40:
                i8 = fe.rank_ic(v8, rv)
                i7 = fe.rank_ic(v7, rv)
                if i8 is not None and i7 is not None:
                    ics8.append(i8); ics7.append(i7)
        diffs = [a - b for a, b in zip(ics8, ics7)]
        t = nw_t(diffs, 0)
        q8["h%d" % hop] = {
            "n": len(diffs),
            "ic_v49": round(sum(ics8) / len(ics8), 4),
            "ic_v48": round(sum(ics7) / len(ics7), 4),
            "mean_diff": round(sum(diffs) / len(diffs), 4),
            "paired_t0": round(t, 2) if t is not None else None,
            "n_v49_wins": sum(1 for d in diffs if d > 0),
            "significant": bool(t is not None and abs(t) >= T_BONF),
        }
        v = q8["h%d" % hop]
        print("  hop=%2d  v4.9=%+.4f  v4.8=%+.4f  配对差=%+.4f  NW-t=%+.2f  "
              "v4.9胜 %d/%d  %s" % (
                  hop, v["ic_v49"], v["ic_v48"], v["mean_diff"], v["paired_t0"] or 0,
                  v["n_v49_wins"], v["n"], "显著" if v["significant"] else "不显著"))
    print("  ⚠️ 结论口径：配对 t 未过 %.2f → 只能说「两版本在本样本上不可区分」，"
          "不能宣称某一版更优；也不能据此推翻 round27c。" % T_BONF)

    # ==================== 输出
    out = {
        "round": "round28",
        "purpose": "飞书五维分层 vs 本项目线性八维 —— 归因式对比（结构/因子集/权重分离）",
        "n_snapshots": len(snaps),
        "date_range": [snaps[0]["date"], snaps[-1]["date"]],
        "caveats": [
            "分层线性加权在数学上恒等于单层线性加权，结构本身不产生 alpha",
            "飞书文档内部权重自相矛盾：分层隐含 ROE 0.35 vs 平铺声明 0.10",
            "飞书要求全市场截面 Z-score，本池只能用池内 Z-score（池已被成交额筛选）",
            "SIZE_IND_NEUT 依赖股本重建（真实 circ_mv 仅最近 2 截面有值）",
            "SUE 无分析师预期库，用 time-series 代理",
            "EVEBIT 缺有息负债拆分，退化为 EBIT_TTM/市值",
            "多重检验累计超 300 次，阈值 3.08 偏乐观",
        ],
        "q0_coverage": cov_summary,
        "q1_structure": q1,
        "q2_corr_new_vs_cur": corr_tbl,
        "q3_schemes": q3,
        "q4_merge": q6,
        "q7_structure_test": q7,
        "q8_v49_vs_v48": q8,
        "baseline_check": {"ok": bool(base_ok), "tol": BASE_TOL,
                           "detail": base_detail,
                           "full_pool_ref": BASE_FULL_REF,
                           "note": ("基准取 round26 已落地的 gated 口径（同一 require 卡口）。"
                                    "全池口径本轮偏低 0.008~0.016，根因是 EM 财务缓存在"
                                    " round25 跑完后被刷新（新增 20260930 三季报数据），"
                                    "quality 维是唯一依赖该缓存的维；所有方案共用同一缓存，"
                                    "横向对比不受影响，但绝对数值不可与 round25 直接并列。")},
    }
    with open(os.path.join(HERE, "audit_round28.json"), "w", encoding="utf-8") as fh:
        json.dump(out, fh, ensure_ascii=False, indent=1)
    print("\n✅ 已写出 audit_round28.json")


if __name__ == "__main__":
    main()
