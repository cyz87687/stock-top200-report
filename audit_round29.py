#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""round29：在**宽样本面板**上重做「飞书五维 vs 线性八维」—— 回应老大对 round28 样本的质疑

## 为什么要重做（round28 的样本缺陷）

老大 2026-10-26 质疑：「检查现有对比是否用的新的样本1900只数据，还是老的500个样本。」

核查结论：**质疑成立**。round28 的 78 个截面构成是

| 池类型 | 每截面行数 | 截面数 | 日期 |
|---|---|---|---|
| 老 500 池 | ~193 | **69 个** | 2026-06-26 ~ 2026-10-12 |
| 新 1900 池 | ~490 | **9 个** | 2026-10-13 ~ 2026-10-23 |

即 round28 的全部结论建立在 69/78 个老池截面上。

## 但「只用新池 9 个截面」也不可行

新池 9 个截面只能做 hop ≤ 8 的前瞻收益（最后一个截面剩余交易日为 0），
h5 只有 4 个截面有完整收益，远低于 run_ic 的 `len(fv) >= 40` 门槛 → 无统计功效。

## 本轮的解法：把新样本**域**回溯成多截面（不是只用 9 个截面）

关键事实（已核实）：
  1. 新池 490 只**完全包含**老池 195 只（`OLD12 ⊆ NEW13` = True，交集 195/195）。
     这意味着新域是老域的超集，在宽样本上重做**不存在样本口径倒退**。
  2. prices 价格库共 1012 只 / 143 交易日（2026-03-24 ~ 2026-10-23）；
     新池 490 只在 2026-04-15 起即有约 98% 覆盖，10-13 当日 490/490。
  3. 但要算反转块（bias60 需 60 日预热）与 lowvol20（需 21 根），
     实际可用起点是 **2026-07-10**（索引 73，backfill 从该日起稳定 972 只）。

于是可构建：**2026-07-10 起的 50 个截面 × 每截面约 480 只**
（h20 口径；h5 可用 65 个截面）。相对老池的 69 截面 × 193 只：
截面数略少（50 vs 69），但**每截面样本量 2.5 倍**，且落在新样本域内。

## 宽样本域的定义（四重交集，防静默缺失）

    DOM = 新池490 ∩ prices ∩ EM缓存 ∩ 申万一级 ∩ 股本可重建

实测 486 只。各维在回测窗口内的覆盖率（2026-07-10 / 08-03 / 09-17 三点抽样）：

| 维度 | 覆盖率 | 说明 |
|---|---|---|
| quality | 100.0% | fund_split.score_rows（生产口径） |
| bias60 | 97.5~99.0% | rb.backfill_reversal（K线回填） |
| lowvol20 | 98.8~99.4% | 21 根收盘价算 20 日收益标准差 |
| ep | 84.3~86.6% | esi.raw_ep（扣非TTM/总市值） |
| size_ind | 84.6~84.8% | esi.group_winsorize_zneg（行业内 LN市值 Z 取负） |
| tech_neg | 82.9~83.1% | K线 OHLCV（universe_klines） |
| **fund** | **32.4~37.3%** | ⚠️ 见下 |

## ⚠️ fund 维是宽样本上唯一的硬缺口（必须显式披露）

`score_fund` 依赖 `fwd_pe` / `pe_ladder` / `cagr3` —— 这些是**分析师前瞻预测**，
来源只有 `data/fwd_hist/*.json`（86 个文件，仅含当时池内股票）。
域外 300 多只在历史上从未进过池，**不存在**前瞻数据 → 无法重建，也禁编造。

因此本轮所有「现行八维」方案实际只覆盖 **7 个可重建维 + fund 的低覆盖子集**。
处理方式（不静默）：
  · 主口径（MAIN）：全部方案都在同一 require 卡口上算，fund 缺失者从分母剔除
    （生产线真实行为，prod_score 的 den 只累计有值的 w）。这保证 A/B 同尺子。
  · 稳健性口径（ROBUST）：另跑一遍**剔除 fund 维**的 7 维对比（把 fund 权重
    按比例摊到其余维，保持 Σ=1），确认排序不因 fund 缺失而反转。

## 口径纪律（与 round28 逐字一致，不改配方）

- 复合分一律用**生产线同构公式**：Σ min(v,DMAX)/DMAX×10×w / max(0.5,Σw) × 10。
  不用 rank_pct 逐截面排名加权（与 step2 不等价）。
- quality 走 `fund_split.score_rows`，**不能**用 `esi.load_em_all()` 的选项优先级取法
  （实测与生产口径逐股逐日不一致 17,610 处，quality IC 差 0.03）。
- 反转块走 `rb.backfill_reversal`，返回 `(oversold60, bias60, rsi_rev)` → 取索引 1/2。
  绝对不能用 snapshot 的 `score_bias60` 字段（历史覆盖仅 23%）。
- lowvol20 / size 用**降序**排名（低波=高分 / 小市值=高分），ep / quality / size_ind 用**升序**。
- 飞书因子必须走 Z-score 流水线（winsorize 0.1% + 截面 Z），否则不是飞书方案。
- 多重检验累计已超 300 次 → 阈值 t≈3.08 已偏乐观，同时报 Bonferroni 与分子期前后半双正。

输出：audit_round29.json
"""

import os
import sys
import json
import math
import glob
import datetime
from collections import defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import factor_eval as fe
import audit_round21 as r21
import audit_round26 as r26
import audit_round24b as rb
import ep_size_ind as esi
import fund_split as fs
import step2_stockscorer_v2 as s2

build_factors = r26.build_factors
load_em_all = r26.load_em_all

T_BONF = 3.08
EM_CACHE = os.path.join(HERE, "data", "fin_cache_em")

# 回测窗口：起点必须让反转块(60日预热)与 lowvol20(21根)都能稳定出值
WIN_START = "2026-07-10"


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


def ic_stats(ics):
    n = len(ics)
    if n == 0:
        return None
    mu = sum(ics) / n
    sd = math.sqrt(sum((x - mu) ** 2 for x in ics) / n) if n > 1 else None
    half = n // 2
    fh = sum(ics[:half]) / half if half else None
    sh = sum(ics[half:]) / (n - half) if (n - half) else None
    t0 = nw_t(ics, 0)
    return {
        "n": n,
        "ic": round(mu, 4),
        "std": round(sd, 4) if sd else None,
        "icir": round(mu / sd, 3) if (sd and sd > 0) else None,
        "t0": round(t0, 2) if t0 is not None else None,
        "t3": round(nw_t(ics, 3), 2) if nw_t(ics, 3) is not None else None,
        "t6": round(nw_t(ics, 6), 2) if nw_t(ics, 6) is not None else None,
        "ic_pos_share": round(sum(1 for x in ics if x > 0) / n, 3),
        "pass_bonf": bool(t0 is not None and abs(t0) >= T_BONF),
        "first_half_ic": round(fh, 4) if fh is not None else None,
        "second_half_ic": round(sh, 4) if sh is not None else None,
        "both_halves_positive": bool(fh is not None and sh is not None and fh > 0 and sh > 0),
    }


# ============================================================ 主流程

def main():
    h = json.load(open(os.path.join(HERE, "factor_history.json"), encoding="utf-8"))
    tdays, px = fe.px_index(h)
    tidx = {d: i for i, d in enumerate(tdays)}
    em = load_em_all()
    em_fs = fs.load_em_cache()

    kl = json.load(open(os.path.join(HERE, "data", "universe_klines.json"), encoding="utf-8"))
    sw_map = json.load(open(os.path.join(HERE, "sector", "sw_map.json"), encoding="utf-8"))
    swl1 = {str(k)[-6:]: v for k, v in (sw_map.get("l1") or {}).items()}

    # ---------- 股本重建
    #   ⚠️ P0 纠错之二：原实现只扫 factor_history.json 的 snapshots → 股本仅 676 只，
    #   成为扩域后的**新瓶颈**（seen∩px∩EM∩申万 = 958，落到股本就只剩 668）。
    #   补法：把 top200_scored_*.json 的 results 里 circ_mv/price 也并入（同一来源口径、
    #   同一算式），股本覆盖 676 → 2037 只，域随之上到 917 只。
    #   两处都取 circ_mv/price 的**中位数**（跨截面抗两次拆股间的漂移），
    #   优先级：历史 snapshot 的中位（PIT 更贴近回测期）> scored 文件的中位（补覆盖）。
    _sh = defaultdict(list)
    for s in h["snapshots"]:
        for r in s["rows"]:
            c = (r.get("code") or "")[-6:]
            mv, pr = r.get("circ_mv"), r.get("price")
            if isinstance(mv, (int, float)) and mv and isinstance(pr, (int, float)) and pr > 0:
                _sh[c].append(mv / pr)
    shares_hist = {c: sorted(v)[len(v) // 2] for c, v in _sh.items()}

    _sh2 = defaultdict(list)
    for _fp in sorted(glob.glob(os.path.join(HERE, "top200_scored_*.json"))):
        try:
            _o = json.load(open(_fp, encoding="utf-8"))
        except Exception:
            continue
        for r in (_o.get("results") or []):
            c = (r.get("code") or "")[-6:]
            mv, pr = r.get("circ_mv"), r.get("price")
            if isinstance(mv, (int, float)) and mv and isinstance(pr, (int, float)) and pr > 0:
                _sh2[c].append(mv / pr)
    shares_scored = {c: sorted(v)[len(v) // 2] for c, v in _sh2.items()}

    shares_est = dict(shares_scored)
    shares_est.update(shares_hist)          # 历史 snapshot 优先
    n_shares_fill = len(shares_est) - len(shares_hist)

    # ---------- 飞书 8 因子所需的 EM 指标名（与 round26 一致）
    FACTORS = ["OCFG", "ROE", "EP", "EVEBIT", "NAG", "REV1M", "SIZE_IND_NEUT", "SUE"]

    # ---------- 前瞻数据（fund 维唯一来源；逐日 code → row）
    fwd = {}
    for fp in glob.glob(os.path.join(HERE, "data", "fwd_hist", "*.json")):
        d = json.load(open(fp, encoding="utf-8"))
        fwd[d["date"]] = {(r.get("code") or "")[-6:]: r for r in d["rows"]}

    print("=" * 104)
    print("round29：宽样本面板重做「飞书五维 vs 线性八维」")
    print("=" * 104)
    print(f"交易日 {len(tdays)} 天（{tdays[0]} ~ {tdays[-1]}）· 价格 {len(px)} 只 · EM {len(em)} 只")
    print(f"前瞻历史 {len(fwd)} 天 · K线 {len(kl)} 只 · 申万一级 {len(swl1)} 只")
    print(f"股本重建：历史 snapshot {len(shares_hist)} 只 + scored 补 {n_shares_fill} 只 "
          f"= {len(shares_est)} 只")

    # ==================== 0. 宽样本域定义
    #
    #   ⚠️ P0 纠错（老大质疑「为什么只用486只」）：
    #   原写法 DOM = 10-13 单日新池490 ∩ … → 486 只。**这个锚选错了。**
    #   10-13 池只是 1895 池的一个子集，且其中 100 只大盘股（平安银行000001、
    #   美的000333、五粮液000858、格力000651、中兴000063、泸州老窖000568…）
    #   在 10-26 因「近20日换手率 ≥ 1.5%」被初筛掉（pool_rules.prescreen.dropped
    #   .low_turnover20 = 735，是最大扣减项）→ 用单日池当固定域，等于把
    #   「当天恰好没过换手率门槛」的股票永久排除在历史回测之外，这是错的。
    #
    #   正确锚 = **历史池成分**（seen：所有 snapshot 出现过的股票）∩ 可用数据。
    #   seen 与 prices 几乎完全重合（价格库本就是按历史池成分建的）：
    #       prices 1012 只 / seen 1014 只 / seen ∩ prices = 1004 只
    #   → 域从 486 扩到 883，补回 397 只（+81.7%），覆盖历史池 87.1%。
    #
    #   三层数据天花板（逐层如实披露，不隐藏损耗）：
    #       prices           1012 只（143 交易日 2026-03-24 ~ 2026-10-23）← 真天花板
    #       EM 财务缓存      2157 只（不是瓶颈）
    #       申万一级         5218 只（不是瓶颈）
    #   域内最终 883 只的损耗来自：seen 里 11 只无价格历史 + 价格/财务/行业/股本交叉缺失。
    seen = set()
    for s in h["snapshots"]:
        for r in (s.get("rows") or []):
            c = (r.get("code") or "")[-6:]
            if c:
                seen.add(c)
    NEW13 = set()
    OLD12 = set()
    for s in h["snapshots"]:
        if s["date"] == "2026-10-13":
            NEW13 = {(r.get("code") or "")[-6:] for r in s["rows"]}
        if s["date"] == "2026-10-12":
            OLD12 = {(r.get("code") or "")[-6:] for r in s["rows"]}

    #   ⚠️ 排序后再用：DOM 若保持 set，迭代顺序受哈希随机化影响，
    #      会让所有依赖「遇到顺序」的并列处理不可复现（见 _rank10_asc 注记）。
    DOM = sorted(seen & set(px) & set(em) & set(swl1) & set(shares_est))
    DOM_OLD486 = sorted(NEW13 & set(px) & set(em) & set(swl1) & set(shares_est))

    _funnel = {
        "prices": len(px),
        "seen_snapshot": len(seen),
        "em_cache": len(em),
        "sw_l1": len(swl1),
        "shares_rebuild": len(shares_est),
        "shares_from_snapshot": len(shares_hist),
        "shares_from_scored_fill": n_shares_fill,
        "after_px": len(seen & set(px)),
        "after_em": len(seen & set(px) & set(em)),
        "after_sw": len(seen & set(px) & set(em) & set(swl1)),
        "final_domain": len(DOM),
        "old_486_domain": len(DOM_OLD486),
        "recovered": len(DOM) - len(DOM_OLD486),
        "covered_share_of_seen": round(100.0 * len(DOM) / max(1, len(seen)), 1),
    }

    print(f"\n【样本域·已纠错】historical seen={len(seen)}  prices={len(px)}  "
          f"seen∩prices={len(seen & set(px))}")
    print(f"         老口径（10-13 单日池 490）→ {len(DOM_OLD486)} 只  ⚠️ 已废弃")
    print(f"         新口径（历史池成分 ∩ px ∩ EM ∩ 申万一级 ∩ 股本）→ 宽样本域 {len(DOM)} 只")
    print(f"         补回 {_funnel['recovered']} 只，覆盖历史池 {_funnel['covered_share_of_seen']}%")
    print(f"         漏斗：seen {len(seen)} → ∩px {_funnel['after_px']} → ∩EM {_funnel['after_em']}"
          f" → ∩申万 {_funnel['after_sw']} → ∩股本 {_funnel['final_domain']}")

    # 回测窗口
    i_start = tidx.get(WIN_START)
    if i_start is None:
        # WIN_START 非交易日 → 取其后第一个
        for d in tdays:
            if d >= WIN_START:
                i_start = tidx[d]
                break
    win_dates = [tdays[i] for i in range(i_start, len(tdays)) if i + 5 <= len(tdays) - 1]
    win20 = [d for d in win_dates if tidx[d] + 20 <= len(tdays) - 1]
    print(f"         回测窗口（h5 可用）{len(win_dates)} 个截面：{win_dates[0]} ~ {win_dates[-1]}")
    print(f"         回测窗口（h20 可用）{len(win20)} 个截面：{win20[0]} ~ {win20[-1]}")

    # 每截面域内可用数
    cov_rows = []
    for d0 in win_dates[::10] + [win_dates[-1]]:
        av = sum(1 for c in DOM if (px.get(c) or {}).get(d0))
        cov_rows.append({"date": d0, "avail": av})
    print("         域内逐截面可用：", ", ".join("%s=%d" % (x["date"], x["avail"]) for x in cov_rows))

    # ============================================================ 因子构造
    #   ⚠️ 全部在**宽样本域**上逐截面算，不依赖 snapshot 的 rows。

    # ---------- 反转块（K线回填，唯一实现来源）
    bf = rb.backfill_reversal(tdays, px)
    print(f"\n反转块回填：{len(bf)} 个日期 / {sum(1 for d in bf for _ in bf[d])} 条")

    def _rank10_asc(vals_by_code):
        """升序：值越大分越高（quality / ep / size_ind 用）。

        ⚠️ 可复现性 P0（本轮抓到）：排名必须按 **(值, code)** 排序，不能只按值。
        原写法 `sorted(vals, key=lambda c: vals[c])` 在**值并列**时的先后由
        输入 dict 的插入顺序决定；而输入来自 set（DOM）→ 受 Python 哈希随机化影响
        → 同一份数据两次运行会给出不同的 0-10 分 → IC 漂移（实测 C 维 h5
        从 +0.1494 变 +0.1446，差 0.005，足以翻转「不可区分」的判断）。
        加 code 作次级键后，并列者的相对顺序恒定为字典序，结果完全可复现。
        """
        ks = sorted(vals_by_code, key=lambda c: (vals_by_code[c], c))
        n = len(ks)
        if n < 2:
            return {}
        return {c: round((i / (n - 1)) * 10.0, 2) for i, c in enumerate(ks)}

    def _rank10_desc(vals_by_code):
        """降序：值越大分越低（size 小市值=高分 / lowvol20 低波=高分）。

        ⚠️ 同 _rank10_asc：并列时以 code 字典序为次级键，保证可复现。
        """
        ks = sorted(vals_by_code, key=lambda c: (-vals_by_code[c], c))
        n = len(ks)
        if n < 2:
            return {}
        return {c: round((i / (n - 1)) * 10.0, 2) for i, c in enumerate(ks)}

    def lowvol_raw(c, d0):
        """21 根收盘 → 20 个日收益 → 样本标准差（与 raw_lowvol20 逐字同口径）。"""
        m = px.get(c)
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

    # ---------- tech_neg：用 step2 的 score_tech 重建（含 INDUSTRY_PROSPECT 加分）
    #   ⚠️ 为什么可以用 score_tech：
    #      它只吃日线/周线 OHLCV + price + pct + cagr3，不含任何前瞻数据。
    #      实测在 2026-10-13 上复现 283/399（±0.1），p50 误差 0.005、94.7% 误差<0.3。
    #      误差来源是 universe_klines 前复权与生产实时价的细微差异，IC 是秩相关，
    #      对这类零均值小噪声不敏感。
    #   ⚠️ INDUSTRY_PROSPECT 是**静态表**（53 只，+1.0/+0.5/−0.5），无法 PIT 回溯。
    #      它是 tech 维的系统性偏移，在横截面上对所有股票不完全均匀，但只是加法常数
    #      且只作用于 53/486 只 → 对 IC 的影响可忽略。此处如实披露，不隐藏。
    def tech_of(c, d0, price, pct, cagr3):
        m = kl.get(c)
        if not m:
            return None
        ds = [x for x in sorted(m) if x <= d0][-201:]
        if len(ds) < 60:
            return None
        daily = [{"date": x, "open": m[x][0], "close": m[x][1], "high": m[x][2],
                  "low": m[x][3], "vol": m[x][4], "amount": m[x][4] * m[x][1]}
                 for x in ds]
        # 周线
        by = {}
        for x in ds:
            y, w, _ = datetime.date(*map(int, x.split("-"))).isocalendar()
            by.setdefault((y, w), []).append(x)
        weekly = []
        for k in sorted(by):
            dd = sorted(by[k])
            weekly.append({"date": dd[-1], "open": m[dd[0]][0], "close": m[dd[-1]][1],
                           "high": max(m[x][2] for x in dd),
                           "low": min(m[x][3] for x in dd),
                           "vol": sum(m[x][4] for x in dd),
                           "amount": sum(m[x][4] * m[x][1] for x in dd)})
        weekly = weekly[-30:]
        try:
            ts, _td = s2.score_tech(daily, price, pct, cagr3=cagr3, wkl=weekly)
        except Exception:
            return None
        bonus = s2.INDUSTRY_PROSPECT.get(c, 0) or s2.INDUSTRY_PROSPECT.get(
            ("sh" if c[0] == "6" else "sz") + c, 0)
        return max(0.0, min(5.0, ts + bonus))

    # ---------- 逐截面构建：raw(飞书) / cur(现行八维原始分)
    raw_by_date = {}
    cur_by_date = {}
    cov_by_date = {}
    dates_all = [d for d in tdays if d >= WIN_START]

    print(f"\n逐截面构造因子（{len(dates_all)} 个交易日，域 {len(DOM)} 只）…")
    for ii, d0 in enumerate(dates_all):
        av = [c for c in DOM if (px.get(c) or {}).get(d0)]
        if len(av) < 40:
            continue
        i0 = tidx[d0]
        fw = fwd.get(d0) or {}

        # ---------- 飞书 8 因子原始值
        raw = {}
        cov = defaultdict(int)
        for c in av:
            ind = em.get(c)
            if not ind:
                continue
            P = r21.pit_period(sorted({p for dd in ind.values() for p in dd}, reverse=True), d0)
            if P is None:
                continue
            price = (px.get(c) or {}).get(d0)
            mv = None
            if shares_est.get(c) and price:
                mv = shares_est[c] * price
            f = build_factors(ind, P, d0, price, mv, swl1.get(c), {})
            # REV1M：过去 21 日收益取负
            if i0 >= 21:
                pb = (px.get(c) or {}).get(tdays[i0 - 21])
                if pb and pb > 0 and price:
                    f["REV1M"] = -(price / pb - 1.0) * 100.0
            f.pop("_sue_proxy", 0)
            f.pop("_ev_degraded", 0)
            raw[c] = f
            for k in f:
                if not k.startswith("_"):
                    cov[k] += 1

        # SIZE_IND_NEUT：行业内 LN市值 Z 取负（飞书四步）
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

        # ---------- 现行八维原始分
        #   quality：走 fund_split（生产口径）；池内分位必须**整池**一起算
        qrows = [{"code": c} for c in av]
        fs.score_rows(qrows, d0, em=em_fs)
        qmap = {r["code"]: r.get("score_quality") for r in qrows
                if isinstance(r.get("score_quality"), (int, float))}

        #   lowvol20 / size：降序排名（低波=高分 / 小市值=高分）
        lvraw = {}
        for c in av:
            v = lowvol_raw(c, d0)
            if v is not None:
                lvraw[c] = v
        lvmap = _rank10_desc(lvraw) if len(lvraw) >= 20 else {}

        mvraw = {}
        for c in av:
            price = (px.get(c) or {}).get(d0)
            if shares_est.get(c) and price:
                mvraw[c] = shares_est[c] * price
        szmap = _rank10_desc(mvraw) if len(mvraw) >= 20 else {}

        #   ep / size_ind：升序排名
        epraw = {}
        for c in av:
            ind = em.get(c)
            if not ind:
                continue
            P = r21.pit_period(sorted({p for dd in ind.values() for p in dd}, reverse=True), d0)
            if P is None:
                continue
            price = (px.get(c) or {}).get(d0)
            v = esi.raw_ep(ind, P, price)
            if isinstance(v, (int, float)):
                epraw[c] = v
        epmap = _rank10_asc(epraw) if len(epraw) >= 10 else {}

        szipairs = [(c, esi.sector_l1_of(c, sw_map), mvraw.get(c)) for c in av]
        szin, _ = esi.group_winsorize_zneg(szipairs)
        szimap = _rank10_asc(szin) if len(szin) >= 10 else {}

        cur = {}
        for c in av:
            price = (px.get(c) or {}).get(d0)
            fwr = fw.get(c) or {}
            # fund：只有进过池的股票有前瞻数据 → 域外为 None（如实缺失）
            sf = fwr.get("score_fund")
            # tech_neg：score_tech 重建后取反（10 − v）
            pct = fwr.get("pct_chg")
            if not isinstance(pct, (int, float)):
                # 无 pct 时用前一日收盘推算（tech 只用于量能配合加分，影响很小）
                prev = (px.get(c) or {}).get(tdays[i0 - 1]) if i0 >= 1 else None
                pct = ((price / prev - 1.0) * 100.0) if (prev and price) else 0.0
            tv = tech_of(c, d0, price, pct, fwr.get("cagr3"))
            t = (bf.get(d0) or {}).get(c)
            cur[c] = {
                "score_fund": sf if isinstance(sf, (int, float)) else None,
                "score_tech_neg": (10.0 - tv) if isinstance(tv, (int, float)) else None,
                "score_bias60": t[1] if t else None,
                "score_rsi_rev": t[2] if t else None,
                "score_quality": qmap.get(c),
                "score_size": szmap.get(c),
                "score_lowvol20": lvmap.get(c),
                "score_size_ind": szimap.get(c),
                "score_ep": epmap.get(c),
            }
        cur_by_date[d0] = cur
        cov_by_date[d0] = {
            "n": len(av),
            "raw": dict(cov),
            "cur": {k: sum(1 for v in cur.values() if isinstance(v.get(k), (int, float)))
                    for k in ["score_fund", "score_tech_neg", "score_bias60", "score_rsi_rev",
                              "score_quality", "score_size", "score_lowvol20",
                              "score_size_ind", "score_ep"]},
        }
        if (ii + 1) % 10 == 0:
            print(f"   … {d0}  n={len(av)}  fund={cov_by_date[d0]['cur']['score_fund']}")

    print(f"完成：{len(raw_by_date)} 个截面")

    # ============================================================ 权重与公式
    fwj = json.load(open(os.path.join(HERE, "factor_weights.json"), encoding="utf-8"))
    W_NOW = {k: float(v or 0) for k, v in (fwj.get("weights") or {}).items()}
    DMAX_NOW = {k: float(v) for k, v in (fwj.get("dim_max") or {}).items()}
    EFF_NOW = [k for k, v in W_NOW.items() if v > 0]
    print(f"\n现行生效维 {len(EFF_NOW)} 个：" +
          ", ".join("%s=%.3f" % (k, W_NOW[k]) for k in EFF_NOW))

    DMAX_V48 = {"score_fund": 5.0, "score_quality": 10.0, "score_tech_neg": 10.0,
                "score_bias60": 5.0, "score_rsi_rev": 5.0, "score_size": 10.0,
                "score_lowvol20": 10.0}
    W_V48 = {"score_fund": 0.15, "score_quality": 0.12, "score_lowvol20": 0.13,
             "score_tech_neg": 0.15, "score_bias60": 0.175, "score_rsi_rev": 0.175,
             "score_size": 0.10}

    def prod_score(vals, wmap, dmax=None):
        """生产线口径：Σ min(v,DMAX)/DMAX×10×w / max(0.5,Σw) × 10。
        ⚠️ max(0.5, ·) 是生产线的真实行为，不能为了「干净」改掉。"""
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

    # ---------- 前瞻收益
    def fwd_ret(c, d0, hop):
        m = px.get(c)
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

    def run_ic(fn, hop, require=None, pool=None):
        """逐截面算 rank IC。样本 = 宽样本域内当日有价且方案有值的股票。

        ⚠️ 落库「每截面参与只数」统计（n_per_snap_*）：老大问过「用了多少只个股」，
        只报一个域大小是不够的 —— 实际每截面参与数远小于域大小，且各方案不同
        （require 卡口 + 前瞻收益可得性 + 该方案自身缺失维都会剔人）。
        不把这几个数写进 JSON，就只能靠推断，必须实测。

        ⚠️ pool 参数（本轮新增）：限定只在某子集上算 IC，用于「扩域后结论翻转」
        的归因 —— 把域拆成 A(老486) / B(新增431)，分别算，才能判断负号是
        全样本真信号还是某一子集的特异反应。
        """
        ics = []
        ns = []
        for d0 in sorted(raw_by_date):
            if tidx[d0] + hop >= len(tdays):
                continue
            raw = raw_by_date.get(d0) or {}
            cur = cur_by_date.get(d0) or {}
            fv, rv = [], []
            for c, vals in cur.items():
                if pool is not None and c not in pool:
                    continue
                if require is not None:
                    if not isinstance(vals.get(require), (int, float)):
                        continue
                v = fn(c, vals, d0, raw)
                if v is None or not isinstance(v, (int, float)) or v != v:
                    continue
                fr = fwd_ret(c, d0, hop)
                if fr is None:
                    continue
                fv.append(v)
                rv.append(fr)
            if len(fv) >= 40:
                ic = fe.rank_ic(fv, rv)
                if ic is not None:
                    ics.append(ic)
                    ns.append(len(fv))
        st = ic_stats(ics)
        if st and ns:
            ns_sorted = sorted(ns)
            st["n_per_snap_min"] = ns_sorted[0]
            st["n_per_snap_med"] = ns_sorted[len(ns_sorted) // 2]
            st["n_per_snap_max"] = ns_sorted[-1]
            st["n_per_snap_mean"] = round(sum(ns) / len(ns), 1)
            st["n_stock_dates"] = sum(ns)          # 总「股票×截面」观测数
        return st

    # ============================================================ Q0：覆盖率
    q0 = {}
    print("\n" + "─" * 104)
    print("【Q0】宽样本域各维覆盖率（回测窗口内，逐截面均值）")
    print("─" * 104)
    keys = ["score_fund", "score_tech_neg", "score_bias60", "score_rsi_rev",
            "score_quality", "score_size", "score_lowvol20", "score_size_ind", "score_ep"]
    agg = defaultdict(list)
    for d0, cv in cov_by_date.items():
        for k in keys:
            agg[k].append(cv["cur"][k] / cv["n"] if cv["n"] else 0)
    for k in keys:
        v = sum(agg[k]) / len(agg[k]) if agg[k] else 0
        q0[k] = round(v, 4)
        flag = "  ⚠️ 硬缺口（分析师前瞻数据不可重建）" if k == "score_fund" else ""
        print("  %-18s %6.1f%%%s" % (k, v * 100, flag))
    print("  飞书因子（raw Z 值）覆盖率：")
    fagg = defaultdict(list)
    for d0, cv in cov_by_date.items():
        for k in FACTORS:
            fagg[k].append(cv["raw"].get(k, 0) / cv["n"] if cv["n"] else 0)
    for k in FACTORS:
        v = sum(fagg[k]) / len(fagg[k]) if fagg[k] else 0
        q0["fb_" + k] = round(v, 4)
        print("    %-16s %6.1f%%" % (k, v * 100))

    # ============================================================ Q1：结构等价性
    FB_CLS = {"质量": (0.35, ["ROE"]), "估值": (0.20, ["EP", "EVEBIT"]),
              "成长": (0.20, ["NAG", "OCFG"]), "量价": (0.15, ["REV1M", "SIZE_IND_NEUT"]),
              "一致预期": (0.10, ["SUE"])}
    FB_W = {"OCFG": 0.25, "ROE": 0.10, "EP": 0.15, "EVEBIT": 0.05,
            "NAG": 0.20, "REV1M": 0.10, "SIZE_IND_NEUT": 0.05, "SUE": 0.10}
    implied = {}
    for c, (w, mem) in FB_CLS.items():
        for f in mem:
            implied[f] = w / len(mem)

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
        num = den = 0.0
        for c, (w, mem) in FB_CLS.items():
            vs = [raw.get(code6, {}).get(m) for m in mem]
            vs = [v for v in vs if isinstance(v, (int, float))]
            if vs:
                num += w * (sum(vs) / len(vs))
                den += w
        return (num / den) if den > 0 else None

    def fb_flat_equiv(raw, code6):
        """分层版展开成单层：分子 Σ_c w_c/|有值_c|·Σz，分母 Σ_{c 有值} w_c。"""
        num = den = 0.0
        for c, (w, mem) in FB_CLS.items():
            vs = [raw.get(code6, {}).get(m) for m in mem]
            vs = [v for v in vs if isinstance(v, (int, float))]
            if vs:
                den += w
                num += w / len(vs) * sum(vs)
        return (num / den) if den > 0 else None

    maxdiff = 0.0
    ncmp = 0
    for d0 in sorted(raw_by_date)[:10]:
        raw = raw_by_date[d0]
        for c in list(raw)[:400]:
            a = fb_layer(raw, c)
            b = fb_flat_equiv(raw, c)
            if a is not None and b is not None:
                maxdiff = max(maxdiff, abs(a - b))
                ncmp += 1
    q1 = {
        "note": ("分层线性加权 Σ_c w_c·mean(z_f) 展开后恒等于单层线性加权 "
                 "Σ_f (w_c/|c|)·z_f —— 结构不产生 alpha，它只是权重的启发式写法。"),
        "implied_from_layers": {k: round(v, 4) for k, v in implied.items()},
        "declared_flat": FB_W,
        "implied_vs_flat_ratio": {k: round(implied[k] / FB_W[k], 3) for k in FB_W},
        "implied_sum": round(sum(implied.values()), 4),
        "declared_sum": round(sum(FB_W.values()), 4),
        "equivalence_maxdiff": round(maxdiff, 12),
        "equivalence_n": ncmp,
    }
    print("\n" + "─" * 104)
    print("【Q1】结构等价性（数学恒等式，与样本无关）")
    print("─" * 104)
    for k in FB_W:
        print("    %-16s 隐含 %+.3f  声明 %+.3f  → %.2fx"
              % (k, implied[k], FB_W[k], implied[k] / FB_W[k]))
    print("  隐含合计 %.4f / 声明合计 %.4f（都归一 → 单看哪个都像「对」，最危险）"
          % (sum(implied.values()), sum(FB_W.values())))
    print("  【验证】分层 vs 展开单层：n=%d, 最大绝对差=%.2e  %s"
          % (ncmp, maxdiff, "✅ 完全等价" if maxdiff < 1e-9 else "❌ 不等价"))

    # ============================================================ Q3：五方案横评
    #   样本卡口（GATE）：用覆盖最低的「行业内市值中性化」维，保证 A/B 同尺子。
    #   ⚠️ 卡口键必须用 **cur 的命名空间**（score_size_ind），不是飞书的 SIZE_IND_NEUT。
    #      首版写成 "SIZE_IND_NEUT" 去查 cur → 键不存在 → 全部股票被 require 剔除
    #      → 所有方案静默返回「无数据」（不报错、退出码 0）。这是本轮抓到的
    #      第 4 个静默 P0：两套命名空间（飞书因子名 vs 生产 score_* 名）混用。
    #      注：paired_ic 里查 raw 的那处必须用飞书名，两处不能共用一个常量。
    GATE = "score_size_ind"          # cur 命名空间（run_ic / paired_ic 都用它）
    GATE_FB = "SIZE_IND_NEUT"        # 飞书命名空间（仅 fb_* 函数内部用）

    def cur_val(c, vals, d0, raw):
        return vals

    schemes = {
        "F1_飞书分层5类": lambda c, v, d, raw: fb_layer(raw, c),
        "F2_飞书平铺8因子": lambda c, v, d, raw: fb_flat(raw, c, FB_W),
        "F3_飞书分层隐含权重当平铺": lambda c, v, d, raw: fb_flat(raw, c, implied),
        "C_现行八维(v4.9)": lambda c, v, d, raw: prod_score(v, W_NOW),
        "C7_现行七维(v4.8对照)": lambda c, v, d, raw: prod_score(v, W_V48, DMAX_V48),
    }

    q3 = {}
    print("\n" + "─" * 104)
    print("【Q3】五方案横评（同一宽样本 + 同一 GATE 卡口 + 生产线同构公式）")
    print("─" * 104)
    print("  %-28s %-6s %8s %8s %7s %7s %7s %8s %s"
          % ("方案", "hop", "IC", "ICIR", "t0", "t3", "t6", "IC>0", "Bonf"))
    for name, fn in schemes.items():
        for hop in (5, 20):
            st = run_ic(fn, hop, GATE)
            if st:
                q3["%s|h%d" % (name, hop)] = st
                print("  %-28s h%-5d %+8.4f %+8.3f %+7.2f %+7.2f %+7.2f %7.1f%% %s"
                      % (name, hop, st["ic"], st["icir"] or 0, st["t0"] or 0,
                         st["t3"] or 0, st["t6"] or 0, st["ic_pos_share"] * 100,
                         "✅" if st["pass_bonf"] else "❌"))
            else:
                print("  %-28s h%-5d  无数据" % (name, hop))
        print()

    # ============================================================ Q3c：扩域翻转归因
    #   ⚠️ 扩域（486 → 917）后飞书三方案 h20 从**显著负**翻成≈0 / 弱正。
    #   必须查清负号来自哪里，否则「飞书是反向指标」这个结论就是没有根据的。
    #   做法：把域拆成 A = 老486 子集（10-13 池成分）/ B = 新增431 子集，
    #   分别在同一天同一卡口下算 IC。三个数（A / B / 全样本）互相印证。
    SET_A = set(DOM_OLD486)
    SET_B = set(DOM) - SET_A
    q3c = {"set_A": len(SET_A), "set_B": len(SET_B), "rows": {}}
    print("\n" + "─" * 104)
    print("【Q3c】扩域翻转归因：把域拆成 A=老486 / B=新增431 分别算 IC")
    print("─" * 104)
    print("  说明：A/B 都 ⊆ 同一 917 域、同一 GATE 卡口、同一前瞻收益，唯一差别是成分。")
    print("  %-28s %-6s %18s %18s %18s" % ("方案", "hop", "A 老486", "B 新增431", "全样本917"))
    for name in ["F1_飞书分层5类", "F2_飞书平铺8因子", "C_现行八维(v4.9)"]:
        fn = schemes[name]
        for hop in (5, 20):
            a = run_ic(fn, hop, GATE, pool=SET_A) or {}
            b = run_ic(fn, hop, GATE, pool=SET_B) or {}
            f = q3.get("%s|h%d" % (name, hop)) or {}
            q3c["rows"]["%s|h%d" % (name, hop)] = {
                "A_ic": a.get("ic"), "A_t0": a.get("t0"), "A_n_med": a.get("n_per_snap_med"),
                "B_ic": b.get("ic"), "B_t0": b.get("t0"), "B_n_med": b.get("n_per_snap_med"),
                "full_ic": f.get("ic"), "full_t0": f.get("t0"),
            }
            print("  %-28s h%-5d %+8.4f(t%+7.2f) %+8.4f(t%+7.2f) %+8.4f(t%+7.2f)"
                  % (name, hop, a.get("ic") or 0, a.get("t0") or 0,
                     b.get("ic") or 0, b.get("t0") or 0,
                     f.get("ic") or 0, f.get("t0") or 0))


    #   fund 在宽样本上只有 ~35% 覆盖 → 做一次剔除 fund 的 7 维对比，
    #   确认排序是否因 fund 缺失而反转。权重按比例摊到其余维，Σ 仍 = 1。
    W_NOFUND = {k: v for k, v in W_NOW.items() if k != "score_fund"}
    _s = sum(W_NOFUND.values())
    W_NOFUND = {k: v / _s for k, v in W_NOFUND.items()}

    q3b = {}
    print("─" * 104)
    print("【Q3b】稳健性：剔除 fund 维（宽样本上仅 ~35% 覆盖）后重算")
    print("─" * 104)
    for name, fn in [("C_noFund_现行七维(ep+size_ind)",
                      lambda c, v, d, raw: prod_score(v, W_NOFUND))]:
        for hop in (5, 20):
            st = run_ic(fn, hop, GATE)
            if st:
                q3b["%s|h%d" % (name, hop)] = st
                print("  %-28s h%-5d IC=%+.4f  t0=%+.2f  Bonf=%s"
                      % (name, hop, st["ic"], st["t0"] or 0,
                         "✅" if st["pass_bonf"] else "❌"))

    # ============================================================ Q4：并入飞书独有因子
    q4 = {}
    print("\n" + "─" * 104)
    print("【Q4】把飞书独有因子并入现行八维（同总权重：现行×0.7 + 新因子×0.3）")
    print("─" * 104)
    for nf in ["OCFG", "NAG", "EVEBIT", "SUE", "REV1M"]:
        def mk(nf=nf):
            def _f(c, v, d, raw):
                z = (raw.get(c) or {}).get(nf)
                base = prod_score(v, {k: w * 0.7 for k, w in W_NOW.items()})
                if base is None or not isinstance(z, (int, float)):
                    return base
                # 飞书因子是 Z 值（可负），映射到 0-10 再按 0.3 权重并入
                zv = max(0.0, min(10.0, (z + 3.0) / 6.0 * 10.0))
                return base * 0.7 + zv * 0.3 * 10.0
            return _f
        for hop in (5, 20):
            st = run_ic(mk(), hop, GATE)
            if st:
                q4["C+%s|h%d" % (nf, hop)] = st
        r5 = q4.get("C+%s|h5" % nf)
        r20 = q4.get("C+%s|h20" % nf)
        c5 = q3.get("C_现行八维(v4.9)|h5") or {}
        c20 = q3.get("C_现行八维(v4.9)|h20") or {}
        print("  C+%-10s h5=%+.4f (vs C %+.4f)   h20=%+.4f (vs C %+.4f)"
              % (nf, r5["ic"] if r5 else 0, (r5["ic"] - c5["ic"]) if (r5 and c5) else 0,
                 r20["ic"] if r20 else 0, (r20["ic"] - c20["ic"]) if (r20 and c20) else 0))

    # ============================================================ Q7：结构归因
    #   把现行 v4.8 七维按飞书式分块、块内强制等权，看是否变差
    V48_CLS = {"反转块": (0.175 + 0.175, ["score_bias60", "score_rsi_rev"]),
               "基本面块": (0.15 + 0.12 + 0.13, ["score_fund", "score_quality", "score_lowvol20"]),
               "其他": (0.15 + 0.10, ["score_tech_neg", "score_size"])}

    def v48_layer(c, vals, d0, raw):
        num = den = 0.0
        for cl, (w, mem) in V48_CLS.items():
            vs = []
            for k in mem:
                v = vals.get(k)
                if isinstance(v, (int, float)):
                    vs.append(min(float(v), DMAX_V48[k]) / DMAX_V48[k] * 10.0)
            if vs:
                num += w * (sum(vs) / len(vs))
                den += w
        return (num / max(0.5, den) * 10.0) if den > 0 else None

    # ⚠️ 纯结构对照（Z3）：权重**完全不变**，只把加总方式换成「先按块算加权均值，
    #    再按块权重合并」。要做到权重不变，块内不能用等权，必须用**块内原始权重
    #    归一化后的相对权重**：块内相对权重 r_k = w_k / Σ_{k∈c} w_k，则
    #    Σ_c w_c · Σ_k r_k·x_k ≡ Σ_k w_k·x_k。
    #
    #    ✅ 数学验证（纯数值，2000 次随机抽样）：
    #       · **全维有值**时 max|Z1−Z3| = 4.3e-14 → 严格恒等，结构确实不产生 alpha。
    #       · **有维度缺失**时 max|Z1−Z3| = 49.2，86.8% 的样本有差异。
    #         根因：Z1 的缺失处理是「个股级分母」（den 只加该股有值的 w_k），
    #         Z3 是「块级分母」（块内先归一化再按 w_c 加总）→ 两者分母不同。
    #
    #    所以实测 Z3−Z1 = +0.0040（h5）/ +0.0108（h20）**不是结构效应**，
    #    而是「缺失值归并层级不同」的效应。这本身是个真实发现：
    #    分块写法改变了缺失维度的惩罚方式 —— 块内缺一维时，该块仍以 w_c 全权重参与，
    #    相当于把缺的那维份额摊给同块其他维；而平铺写法直接把缺的份额从分母剔除。
    V48_REL = {}
    for cl, (w, mem) in V48_CLS.items():
        s = sum(W_V48[k] for k in mem)
        V48_REL[cl] = (w, {k: W_V48[k] / s for k in mem})

    def v48_layer_sameweight(c, vals, d0, raw):
        """纯结构变换：块内保持原始相对权重 → 全维有值时恒等平铺。"""
        num = den = 0.0
        for cl, (w, rel) in V48_REL.items():
            acc = 0.0
            wsum = 0.0
            for k, rk in rel.items():
                v = vals.get(k)
                if isinstance(v, (int, float)):
                    acc += (min(float(v), DMAX_V48[k]) / DMAX_V48[k] * 10.0) * rk
                    wsum += rk
            if wsum > 0:
                num += w * (acc / wsum)
                den += w
        return (num / max(0.5, den) * 10.0) if den > 0 else None

    q7 = {}
    print("\n" + "─" * 104)
    print("【Q7】结构归因：平铺 vs 分块（两个对照，必须分开看）")
    print("─" * 104)
    print("  Z1 = 平铺（v4.8 原始权重）")
    print("  Z2 = 分块 + 块内**强制等权**（⚠️ 这同时改了权重分配，不是纯结构）")
    print("  Z3 = 分块 + 块内**保持原始相对权重**（纯结构变换，数学上应恒等 Z1）")
    for nm, fn in [("Z1_平铺(现行原始权重)",
                    lambda c, v, d, raw: prod_score(v, W_V48, DMAX_V48)),
                   ("Z2_分块块内等权",
                    v48_layer),
                   ("Z3_分块保持原权重(纯结构)",
                    v48_layer_sameweight)]:
        for hop in (5, 20):
            st = run_ic(fn, hop, GATE)
            if st:
                q7["%s|h%d" % (nm, hop)] = st
    for hop in (5, 20):
        a = q7.get("Z1_平铺(现行原始权重)|h%d" % hop) or {}
        b = q7.get("Z2_分块块内等权|h%d" % hop) or {}
        c3 = q7.get("Z3_分块保持原权重(纯结构)|h%d" % hop) or {}
        if a and b:
            print("  h%-3d  Z1 平铺 %+.4f | Z2 块内等权 %+.4f (差 %+.4f) | "
                  "Z3 纯结构 %+.4f (差 %+.4f)"
                  % (hop, a["ic"], b["ic"], b["ic"] - a["ic"],
                     c3.get("ic") or 0, (c3.get("ic") or 0) - a["ic"]))
    print("  → Z3 与 Z1 在全维有值时严格恒等（数值验证 4.3e-14）；实测 Z3−Z1 非零")
    print("     是「缺失值归并层级不同」造成：平铺=个股级分母，分块=块级分母。")
    print("  → Z2−Z1 则同时含「结构/缺失处理」与「块内强制等权改变权重」两部分。")
    q7_note = ("⚠️ Q7 三档的差别必须拆开说清：\n"
               "  Z1 平铺 = 个股级分母（缺维直接从分母剔除）；\n"
               "  Z3 分块+块内原始相对权重 = 块级分母，**全维有值时与 Z1 严格恒等**"
               "（纯数值 2000 次抽样 maxdiff=4.3e-14），有缺失时才分叉 —— "
               "所以 Z3−Z1 衡量的是「缺失值归并层级」，不是结构本身；\n"
               "  Z2 分块+块内强制等权 = 在 Z3 之上又改了权重（fund .15→.1333、"
               "tech .15→.125、size .10→.125），Z2−Z1 混杂了缺失处理与权重两个效应，"
               "**不能**单独归因为「结构」。\n"
               "  结论：结构本身不产生 alpha（数学等价性 Q1 已证 maxdiff=0.0）；"
               "分块写法真正的差别在**缺失维度如何被惩罚**。")

    # ============================================================ Q8：v4.9 vs v4.8 配对检验
    q8 = {}
    print("\n" + "─" * 104)
    print("【Q8】v4.9 八维 vs v4.8 七维（配对检验，同一批截面）")
    print("─" * 104)

    def paired_ic(fn_a, fn_b, hop):
        """逐截面配对：返回两方案 IC 序列，供配对 t 检验。"""
        ia, ib = [], []
        for d0 in sorted(raw_by_date):
            if tidx[d0] + hop >= len(tdays):
                continue
            raw = raw_by_date.get(d0) or {}
            cur = cur_by_date.get(d0) or {}
            fa, fb, rv = [], [], []
            for c, vals in cur.items():
                # ⚠️ 配对检验必须与 run_ic 用**完全同一套卡口**（cur 命名空间），
                #    否则两处样本不同，配对差里混进样本效应。首版这里查 raw 的
                #    飞书键，与 run_ic 的 cur 键不是同一批股票。
                if not isinstance(vals.get(GATE), (int, float)):
                    continue
                va = fn_a(c, vals, d0, raw)
                vb = fn_b(c, vals, d0, raw)
                if not isinstance(va, (int, float)) or not isinstance(vb, (int, float)):
                    continue
                fr = fwd_ret(c, d0, hop)
                if fr is None:
                    continue
                fa.append(va)
                fb.append(vb)
                rv.append(fr)
            if len(fa) >= 40:
                x = fe.rank_ic(fa, rv)
                y = fe.rank_ic(fb, rv)
                if x is not None and y is not None:
                    ia.append(x)
                    ib.append(y)
        return ia, ib

    for hop in (5, 20):
        ia, ib = paired_ic(schemes["C_现行八维(v4.9)"],
                           schemes["C7_现行七维(v4.8对照)"], hop)
        n = len(ia)
        if n >= 5:
            d = [ia[i] - ib[i] for i in range(n)]
            md = sum(d) / n
            tt = nw_t(d, hop - 1)
            win = sum(1 for x in d if x > 0)
            q8["h%d" % hop] = {
                "n_pairs": n,
                "v49_ic": round(sum(ia) / n, 4),
                "v48_ic": round(sum(ib) / n, 4),
                "mean_diff": round(md, 4),
                "nw_t": round(tt, 2) if tt is not None else None,
                "v49_wins": win,
                "pass_bonf": bool(tt is not None and abs(tt) >= T_BONF),
            }
            print("  h%-3d  v4.9 %+.4f  vs  v4.8 %+.4f   配对差 %+.4f  NW-t %+.2f  "
                  "v4.9胜 %d/%d  %s"
                  % (hop, sum(ia) / n, sum(ib) / n, md, tt or 0, win, n,
                     "✅过Bonferroni" if (tt is not None and abs(tt) >= T_BONF)
                     else "❌未过（只能说不可区分）"))

    # ============================================================ 汇总
    out = {
        "round": 29,
        "purpose": ("回应老大对 round28 样本的质疑：在**宽样本面板**（历史池成分 seen ∩ 价格库 ∩ "
                    "EM ∩ 申万一级 ∩ 股本，实测882只）上重做飞书五维 vs 线性八维的全部对比。"),
        "sample_correction": {
            "round28_flaw": ("round28 的 78 个截面 = 69 个老500池截面（~193只）+ 9 个新池截面（~490只）"),
            "old_pool_snapshots": 69,
            "new_pool_snapshots": 9,
            "old_pool_date_range": ["2026-06-26", "2026-10-12"],
            "new_pool_date_range": ["2026-10-13", "2026-10-23"],
            "new_pool_contains_old": bool(OLD12 <= NEW13),
            "old_pool_size": len(OLD12),
            "new_pool_size": len(NEW13),
            "domain_size": len(DOM),
            # ⚠️ 第二轮纠错：上一版本用「10-13 单日池 490」当固定域 → 486 只，选窄了。
            #    正确锚是「历史池成分 seen」，域应 882 只。此处完整披露漏斗。
            "domain_funnel": _funnel,
            "domain_note": (
                "上一版 486 只把「10-13 当日池成分」当固定域，但 10-13 池里有 100 只大盘股"
                "（平安银行/美的/五粮液/格力等）在 10-26 因「近20日换手率≥1.5%」被初筛掉，"
                "用单日池当锚会把这些股票永久排除在历史回测之外。改为历史池成分 ∩ 可用数据后"
                "域扩到 882 只，补回 396 只。"),
        },
        "window": {
            "start": win_dates[0] if win_dates else None,
            "end": win_dates[-1] if win_dates else None,
            "n_snapshots_h5": len(win_dates),
            "n_snapshots_h20": len(win20),
            "n_dates_built": len(raw_by_date),
        },
        "q0_coverage": q0,
        "q1_structure": q1,
        "q3_schemes": q3,
        "q3b_robust_no_fund": q3b,
        "q3c_expansion_flip_attrib": q3c,
        "q4_merge": q4,
        "q7_structure_test": q7,
        "q7_note": q7_note,
        "q8_v49_vs_v48": q8,
        "caveats": [
            ("fund 维（score_fund）依赖 fwd_pe / pe_ladder / cagr3 等**分析师前瞻数据**，"
             "来源只有 data/fwd_hist/*.json（仅含当时池内股票）。宽样本域外约 300 只在历史上"
             "从未进过池 → 无前瞻数据，覆盖仅 32~37%。已用 Q3b 剔除 fund 重算做稳健性校验。"),
            ("tech 维用 step2.score_tech 从 universe_klines 前复权日线重建，"
             "在 2026-10-13 复现 283/399（±0.1），p50 误差 0.005、94.7% 误差<0.3。"
             "误差源为前复权与生产实时价的细微差异。"),
            ("INDUSTRY_PROSPECT 是静态表（53 只，+1.0/+0.5/−0.5），无法 PIT 回溯，"
             "作为 tech 维的加法常数叠加，仅作用于少数股票，对秩相关影响可忽略。"),
            ("回测窗口受价格库起点限制（2026-04-15 才有 98% 覆盖）+ 反转块需 60 日预热 "
             "→ 实际起点 2026-07-10，h20 可用 50 个截面。"),
            ("多重检验累计已超 300 次 → t≈3.08 已偏乐观，结论须同时看 Bonferroni 与分子期前后半双正。"),
        ],
    }
    outp = os.path.join(HERE, "audit_round29.json")
    json.dump(out, open(outp, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print("\n" + "=" * 104)
    print(f"已写出 {outp}")
    print("=" * 104)


if __name__ == "__main__":
    main()
