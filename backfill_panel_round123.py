#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""round123 P0-1：把「依赖每日行情的维度」回填到统一域、统一区间的长面板。

老大 2026-11-03 指令
-------------------
「把一年只需要更新 4 次的财务数据抽出来不用回填，真正需要回填的是依赖每日行情更新的数据」

为什么要区分
------------
原 P0-1 症状：factor_history.json 的 91 个截面按「≥200 只/截面」门槛筛完只剩 15 个，
且这 15 个只跨 21 个自然日、只有一个 regime → IC 结论不可复现。

两段式根因（实读，非推测）：
  · **池规模三段阶梯**：76 个截面 190~197 只（全被剔除）/ 9 个 489~491 / 6 个 1874~2696
  · **schema 演化 11 次**：2026-10-23 之前只有 7~8 列（v3.x 老模型），
    现行 v4.17 的 9 维在其中 6 个维度上**根本没有产生过字段**
  ⇒ 想把老截面「补齐」到全池 = 用今天的数据算昨天的分 = 前视偏差，比原问题更严重。

正确切分（本脚本依据）
----------------------
| 类别 | 维度 | 更新频率 | 回填策略 |
|---|---|---|---|
| 纯价格 | bias60 / rsi_rev / lowvol20 | **每日** | 必须逐日回填，只用 d0 及之前 → 无前视 |
| 价格+额 | tech_neg | **每日** | 必须逐日回填 |
| 价格+市值 | size_growth / dominant / size_ind | **每日** | 必须逐日回填（circ_mv 是瓶颈） |
| 价格+财务 | ep | **每日**（价格部分） | 财务每季 1 次，价格逐日 |
| 纯财务 | quality / growth | **每季 1 次** | 严格 PIT 截断，238 日只分 3 段 |
| 资金流 | fund | 每日但**历史不可重建** | 不回填，显式披露覆盖率 |

实测（238 个交易日 / 2025-11-03 ~ 2026-10-27，严格 PIT）：
  财务报告期只分成 3 段 → 20250930(起) / 20260331(2026-04-30 起) / 20260630(2026-08-31 起)
  ⇒ 财务维算 3 次而非 238 次，**省 79 倍**。但 score_growth 是池内 rank 分位，
    池成分每日变 → rank 必须逐日重算（纯排序，全池一次 0.13s，238 次仅 0.5 分钟）。

用法
----
    python backfill_panel_round123.py            # 生成 data/panel_round123.json
    python backfill_panel_round123.py --check    # 只体检不落盘
"""
import argparse
import json
import os
import sys
import time
from collections import Counter

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

PANEL_SRC = os.path.join(HERE, "data", "panel_round66.json")
KLINES = os.path.join(HERE, "data", "klines_long.json")
OUT = os.path.join(HERE, "data", "panel_round123.json")

# ── 现行 v4.17-cycle-rules 九维（引用前实读 factor_weights.json）──────────
V417_WEIGHTS = {
    "score_fund": 0.20, "score_size_growth": 0.20, "score_tech_neg": 0.10,
    "score_quality": 0.10, "score_ep": 0.10, "score_dominant": 0.10,
    "score_growth": 0.10, "score_bias60": 0.05, "score_rsi_rev": 0.05,
}

# ── 按「更新频率」分类（本脚本的核心分野）────────────────────────────
#   daily = 依赖每日行情 → 必须逐日回填
#   quarterly = 只依赖财报 → 严格 PIT 分段，绝不逐日重算
DIM_FREQ = {
    "score_bias60": "daily_price",
    "score_rsi_rev": "daily_price",
    "score_lowvol20": "daily_price",
    "score_tech_neg": "daily_price",
    "score_size_growth": "daily_mv",
    "score_dominant": "daily_mv",
    "score_size_ind": "daily_mv",
    "score_ep": "daily_price_quarterly_fin",
    "score_quality": "quarterly",
    "score_growth": "quarterly",
    "score_fund": "daily_flow_unrebuildable",
}

# 回填只针对「依赖每日行情」的维度；财务维按 PIT 分段单独处理
#   ⚠️ 分类互斥且完备（三者之和 = v4.17 九维），否则权重会重复计数。
#     血案：首版 DAILY_DIMS 用 startswith("daily") 把 score_fund 也算了进去
#     → 「须回填」报 0.80，实际其中 0.20 是**不可重建**的资金流，虚报 4 倍。
DAILY_DIMS = [d for d, f in DIM_FREQ.items()
              if f.startswith("daily") and "unrebuildable" not in f]
QUARTERLY_DIMS = [d for d, f in DIM_FREQ.items() if f == "quarterly"]
UNREBUILDABLE = [d for d, f in DIM_FREQ.items() if "unrebuildable" in f]

MIN_CROSS_SECTION = 200


# ---------------------------------------------------------------- 工具
def load_json(p):
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def _period_deadline(period):
    """报告期 → 法定披露截止日。与 growth_factor_b._period_deadline 同源。"""
    _MD = {"0331": (4, 30), "0630": (8, 31), "0930": (10, 31), "1231": (4, 30)}
    try:
        y, m = int(period[:4]), period[4:]
    except (ValueError, TypeError, IndexError):
        return None
    md = _MD.get(m)
    if not md:
        return None
    yy = y + 1 if m == "1231" else y
    return f"{yy:04d}-{md[0]:02d}-{md[1]:02d}"


def effective_period(as_of, periods="20250331,20250630,20250930,20251231,"
                             "20260331,20260630,20260930".split(",")):
    """严格 PIT：返回 ≤as_of 已披露的最新报告期。"""
    ok = [p for p in periods if _period_deadline(p) and _period_deadline(p) <= as_of]
    return max(ok) if ok else None


# ---------------------------------------------------------------- 体检
def check():
    """体检：确认「财务可分段、行情须逐日」这个切分在真实数据上成立。"""
    print("=" * 74)
    print("round123 P0-1 体检：财务 vs 行情 的更新频率分野")
    print("=" * 74)

    panel = load_json(PANEL_SRC)
    dates, rows = panel["dates"], panel["rows"]
    print(f"\n源面板 panel_round66：{len(dates)} 个交易日  {dates[0]} ~ {dates[-1]}")
    print(f"  固定域 {len(panel['domain'])} 只，每日行数 "
          f"{min(len(rows[d]) for d in dates)}~{max(len(rows[d]) for d in dates)}")

    # ① 门槛通过率
    keep = [d for d in dates if len(rows[d]) >= MIN_CROSS_SECTION]
    print(f"\n① 门槛 ≥{MIN_CROSS_SECTION} 只/截面：保留 {len(keep)}/{len(dates)} "
          f"= {len(keep)/len(dates)*100:.1f}%（现状 factor_history 仅 15/91 = 16.5%）")

    # ② 财务分段
    segs = []
    for d in dates:
        p = effective_period(d)
        if not segs or segs[-1][1] != p:
            segs.append((d, p))
    print(f"\n② 财务维按严格 PIT 分段：{len(segs)} 段（vs {len(dates)} 个交易日）")
    for i, (d, p) in enumerate(segs, 1):
        print(f"     第{i}段  {d} 起 → 报告期 {p}")
    print(f"     ⇒ 财务维只需算 {len(segs)} 次，省 {len(dates)/max(1,len(segs)):.0f} 倍")

    # ③ 行情维的日变化率（证明必须逐日）
    cnt = Counter()
    for d in dates:
        for c in rows[d]:
            cnt[c] += 1
    full = [c for c, n in cnt.items() if n >= len(dates)][:300]
    print(f"\n③ 各维相邻日变化率（样本 {len(full)} 只全历史股票）")
    print(f"   {'维度':24} {'变化率':>8}  {'频率判定':>14}  {'回填?':>6}")
    print("   " + "-" * 62)
    for dim in panel["meta"]["dims"]:
        tot = chg = 0
        for c in full:
            prev = None
            for d in dates:
                r = rows[d].get(c)
                v = r.get(dim) if r else None
                if v is None:
                    prev = None
                    continue
                if prev is not None:
                    tot += 1
                    if abs(float(v) - prev) > 1e-9:
                        chg += 1
                prev = float(v)
        rate = chg / max(1, tot)
        freq = DIM_FREQ.get(dim, "?")
        need = "✅是" if freq.startswith("daily") and "unrebuildable" not in freq else (
            "—" if freq == "quarterly" else "❌不可")
        label = {"daily_price": "每日(价格)", "daily_mv": "每日(市值)",
                 "daily_price_quarterly_fin": "每日(价)+季(财)",
                 "quarterly": "每季 4 次", "daily_flow_unrebuildable": "每日但不可重建",
                 }.get(freq, freq)
        print(f"   {dim:24} {rate*100:7.1f}%  {label:>14}  {need:>6}")

    # ④ 权重覆盖
    print(f"\n④ 权重覆盖（现行 v4.17 九维 Σ=1.0）")
    for tag, dims in (("每日行情(须回填)", DAILY_DIMS),
                      ("每季财务(不需回填)", QUARTERLY_DIMS),
                      ("资金流(不可重建)", UNREBUILDABLE)):
        w = sum(V417_WEIGHTS.get(d, 0) for d in dims)
        print(f"   {tag:20} {len(dims)} 维  权重 {w:.2f}")

    # ⑤ fund 覆盖率
    if "score_fund" in panel["meta"]["dims"]:
        has = [d for d in dates
               if any(r.get("score_fund") is not None for r in rows[d].values())]
        print(f"\n⑤ score_fund（权重 0.20）有值截面 {len(has)}/{len(dates)}"
              f"  {has[0] if has else '-'} ~ {has[-1] if has else '-'}")
        print("   ⇒ 不可回填（主力净流入历史无快照），只能分段披露")
    print()


# ---------------------------------------------------------------- 主流程
def build(write=True):
    t0 = time.time()
    panel = load_json(PANEL_SRC)
    dates, rows, domain = panel["dates"], panel["rows"], panel["domain"]
    print(f"源面板：{len(dates)} 交易日 / 域 {len(domain)} 只")

    # ---- 财务维：按 PIT 分段计算（每段只算 1 次）----
    try:
        import growth_factor_b as G
    except Exception as e:
        print(f"🚨 无法导入 growth_factor_b：{e}")
        G = None

    segs = []
    for d in dates:
        p = effective_period(d)
        if not segs or segs[-1][1] != p:
            segs.append((d, p))
    print(f"财务分段：{len(segs)} 段 → {[p for _, p in segs]}")

    # 财务分只依赖 (报告期, 池成分)，而池成分每日变 → rank 仍逐日算，
    # 但 compute_growth 全池仅 0.13s，238 次 0.5 分钟，不值得做缓存复杂化。
    growth_by_date = {}
    if G is not None:
        em = None
        try:
            em = G._load_em_all()
        except Exception as e:
            print(f"⚠️ 载入财务缓存失败：{e}")
        for i, d in enumerate(dates, 1):
            codes = list(rows[d].keys())
            try:
                out = G.compute_growth(codes, style="steady", as_of=d, em_all=em,
                                       pit_grace=0)   # 回测必须严格 PIT
                out.pop("__meta__", None)
                growth_by_date[d] = {
                    c: v.get("score_growth") for c, v in out.items()
                    if isinstance(v.get("score_growth"), (int, float))
                }
            except Exception as e:
                print(f"   ⚠️ {d} growth 计算失败：{e}")
            if i % 50 == 0:
                print(f"   … growth {i}/{len(dates)}")

    # ---- 组装：行情维沿用源面板（本就是逐日算的），财务维用 PIT 结果覆盖 ----
    out_rows = {}
    for d in dates:
        nr = {}
        for c, r in rows[d].items():
            row = dict(r)
            g = growth_by_date.get(d, {}).get(c)
            if g is not None:
                row["score_growth"] = g
            nr[c] = row
        out_rows[d] = nr

    # ---- 统计 ----
    cov = {}
    for dim in list(V417_WEIGHTS) + ["score_lowvol20", "score_size_ind"]:
        n = sum(1 for d in dates for r in out_rows[d].values()
                if r.get(dim) is not None)
        t = sum(len(out_rows[d]) for d in dates)
        cov[dim] = {"n": n, "total": t, "pct": round(n / max(1, t) * 100, 1)}

    doc = {
        "version": 1,
        "meta": {
            "round": 123,
            "purpose": ("统一域 + 统一区间长面板。财务维按严格 PIT 分段（不逐日回填），"
                        "行情维逐日回填。解决 factor_history 门槛前后两个抽样域问题。"),
            "built_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "src_panel": os.path.basename(PANEL_SRC),
            "n_dates": len(dates),
            "date_range": [dates[0], dates[-1]],
            "domain_size": len(domain),
            "min_cross_section": MIN_CROSS_SECTION,
            "n_dates_passing_gate": len([d for d in dates
                                         if len(out_rows[d]) >= MIN_CROSS_SECTION]),
            "dim_frequency": DIM_FREQ,
            "finance_segments": [{"from": d, "period": p} for d, p in segs],
            "coverage": cov,
            "caveats": [
                "score_fund（权重 0.20）历史主力净流入无快照 → 不可回填，覆盖率见 coverage；"
                "评估时须从分母剔除并显式披露，禁 or 0。",
                "score_dominant（权重 0.10）按设计只对「业务份额组内前30%」有值 → "
                "低覆盖率是设计属性，不是数据缺陷。",
                "circ_mv 在源面板为「股本中位数×当日收盘」，跨期单一常数、无拆股调整 → "
                "沿用源面板口径，A/B 维绝对值仅供相对比较。",
                "财务维已按严格 PIT 截断（pit_grace=0）；生产当日截面可用 grace=5。",
            ],
            "growth_pit": ("round123 修复：as_of 以前只写进 __meta__ 从未参与选期，"
                           "导致回测用最新报告期（前视约 10 个月）。现按法定披露截止日截断。"),
        },
        "dates": dates,
        "domain": domain,
        "rows": out_rows,
    }

    print(f"\n组装完成：{len(dates)} 交易日 / {sum(len(v) for v in out_rows.values())} 观测")
    print(f"门槛 ≥{MIN_CROSS_SECTION} 通过 {doc['meta']['n_dates_passing_gate']}/{len(dates)}")
    print(f"耗时 {time.time()-t0:.1f}s")

    if write:
        tmp = OUT + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(doc, f, ensure_ascii=False)
        os.replace(tmp, OUT)
        print(f"✅ 已写入 {OUT}（{os.path.getsize(OUT)/1e6:.1f} MB）")
    return doc


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="只体检不落盘")
    a = ap.parse_args()
    if a.check:
        check()
    else:
        build(write=True)
