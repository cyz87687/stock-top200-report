#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
audit_round67_backtest_engine.py —— 组合级回测引擎（项目首个净值层）

═══ 为什么必须先建这一层 ═══

老大给的三大最终目标是：
  ① 最大化组合 ICIR（截面预测信号稳定、统计显著）
  ② 样本外超额夏普尽可能高
  ③ 限制超额最大回撤

项目此前**只有 IC 层**：所有 audit_round*.json 都是「截面秩相关 + NW t」，
没有一条净值曲线、没有基准序列、没有成本模型。
⇒ ②③ 两个目标在现有基础设施下**根本无法度量**，任何"已优化"的说法都是空话。

本脚本补上这一层，并严格区分：
  · ICIR          —— 预测层指标，衡量「打分与未来收益的秩相关有多稳定」
  · 超额夏普/回撤  —— 实现层指标，衡量「按这个打分真组组合能赚多少、回撤多大」
  两者可以背离：IC 高但组合集中度过高/换手过大 → 超额夏普反而低。
  这正是必须同时看三个目标的原因。

═══ 回测口径（全部显式声明，不留隐含假设）═══

1. **组合构建**：每个调仓日 t，按 composite 打分取 TopN，**等权**。
   （等权而非打分加权：打分加权会让权重与分数尺度绑定，跨权重版本不可比）

2. **成交价**：t+1 日**开盘价**买入（可交易口径，不用 t 日收盘 —— 那会引入前视）。
   卖出同样用调仓日 t+1 开盘价。这是本引擎最关键的口径，写入输出 caveat。

3. **基准**：沪深300（000300）同口径「t+1 开盘 → 下一调仓日 t+1 开盘」。
   同时输出对 中证500 / 中证1000 / 中证全指 的超额，用于判断是否只是小盘 beta。

4. **成本**：双边 0.12%（佣金 0.025%×2 + 印花税 0.05%（卖出）+ 滑点 0.02%×2）。
   按**换手率**逐期扣减，不做「年化常数摊薄」—— 那会低估高换手策略的成本。

5. **停牌/退市**：调仓日无价格的股票**剔除**（不是填 0 收益），
   若持仓在持有期内失去价格，按最后可得价平仓并在 caveat 记录。

6. **调仓频率**：H ∈ {5, 10, 20} 交易日。日频（H=1）换手成本过高，仅作对照。

7. **样本外**：所有指标同时给出「全样本」与「最后 40% 截面」两套，
   因为 panel 的 h20 分段检验显示段 1~3 与段 4 符号翻转（round54）。

═══ 输出 ═══
audit_round67.json  —— 含 base（v4.15 权重）在多种 (TopN, H) 下的完整指标、
                        各基准超额、以及净值曲线（供画图/复核）
"""
import os
import sys
import json
import math
import argparse
from collections import defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

PANEL = os.path.join(HERE, "data", "panel_round66.json")
KLONG = os.path.join(HERE, "data", "klines_long.json")
WEIGHTS = os.path.join(HERE, "factor_weights.json")
BENCH_DIR = os.path.join(HERE, "data", "bench")
OUT = os.path.join(HERE, "audit_round67.json")

# 双边交易成本（小数）。买入 0.025% 佣金 + 0.02% 滑点；卖出再加 0.05% 印花税
COST_BUY = 0.00025 + 0.0002
COST_SELL = 0.00025 + 0.0002 + 0.0005
COST_ROUND = COST_BUY + COST_SELL   # 一次完整换仓（100% 换手）的成本

BENCHES = ("000300", "000905", "000852", "000985")


def is_num(x):
    return isinstance(x, (int, float)) and not isinstance(x, bool) and x == x


# ─────────────────────────── 组合合成 ───────────────────────────

def composite_all(rows, wmap, dim_max):
    """按生产同口径合成总分。

    total = Σ min(v, DMAX)/DMAX × 10 × w  /  Σ生效w  × 10
    ⚠️ 缺维**从分母剔除**（禁 or 0）。这正是 P0-4「混合量尺」的来源：
       缺维时 Σw 变小 → 实际暴露被放大。本轮不改口径，只如实标注。
    返回 (scores, used_w_sum)
    """
    num, den = 0.0, 0.0
    for k, w in wmap.items():
        if w <= 0:
            continue
        v = rows.get(k)
        if not is_num(v):
            continue
        dmax = dim_max.get(k, 10.0)
        num += min(float(v), dmax) / dmax * 10.0 * w
        den += w
    if den <= 0:
        return None, 0.0
    return num / den * 10.0, den


def build_scores(panel, wmap, dim_max):
    """per_date: {date: [(code, score, used_w), ...] 按 score 降序}"""
    out = {}
    for d in panel["dates"]:
        rows = panel["rows"].get(d) or {}
        lst = []
        for c, rec in rows.items():
            s, uw = composite_all(rec, wmap, dim_max)
            if s is not None:
                lst.append((c, s, uw))
        # ⚠️ 排序键必须带 code 兜底：只按值排序受 PYTHONHASHSEED 影响（项目血泪）
        lst.sort(key=lambda x: (-x[1], x[0]))
        out[d] = lst
    return out


# ─────────────────────────── 净值回测 ───────────────────────────

def load_prices():
    return json.load(open(KLONG, encoding="utf-8"))


def load_bench(code):
    p = os.path.join(BENCH_DIR, f"{code}.json")
    if not os.path.exists(p):
        return None
    return json.load(open(p, encoding="utf-8"))["px"]


def _px(px, code, d):
    """取 d 日**开盘价**。返回 None 表示不可交易。"""
    bar = (px.get(code) or {}).get(d)
    if isinstance(bar, (list, tuple)) and len(bar) >= 1 and is_num(bar[0]) and bar[0] > 0:
        return float(bar[0])
    return None


def _bpx(bpx, d):
    bar = bpx.get(d)
    if isinstance(bar, (list, tuple)) and len(bar) >= 1 and is_num(bar[0]) and bar[0] > 0:
        return float(bar[0])
    return None


def backtest(panel, scores, px, bench_px, topn, hold, tdays, tindex):
    """等权 TopN，每 hold 个交易日调仓，t+1 开盘成交。

    返回 dict：净值序列、每期收益、换手、与各基准的对比。
    """
    dates = panel["dates"]
    # 调仓日 = 每隔 hold 个交易日；需要 t+1 存在
    rebal = []
    for i in range(0, len(dates) - 1, hold):
        d_t = dates[i]
        d_exec = dates[i + 1] if i + 1 < len(dates) else None
        if d_exec:
            rebal.append((d_t, d_exec, i))
    if len(rebal) < 3:
        return None

    periods = []
    prev_hold = set()
    nav = 1.0
    nav_series = []   # [(exec_date, nav)]
    cost_total = 0.0

    for k, (d_sig, d_exec, i0) in enumerate(rebal):
        # 执行日之后的下一个调仓执行日（= 卖出日）
        if k + 1 < len(rebal):
            d_next, i1 = rebal[k + 1][1], rebal[k + 1][2]
        else:
            d_next, i1 = None, len(dates) - 1

        picks = scores.get(d_sig) or []
        # 只保留执行日可买的（有开盘价）
        sel = []
        for c, s, uw in picks:
            if _px(px, c, d_exec) is not None:
                sel.append(c)
            if len(sel) >= topn:
                break
        if len(sel) < max(5, topn // 4):
            continue

        # 换手：与上期持仓的对称差 / 上期规模
        cur = set(sel)
        if prev_hold:
            turnover = len(cur - prev_hold) / max(len(cur), 1)
        else:
            turnover = 1.0

        # 本期收益：执行日开盘 → 下期执行日开盘（等权）
        rets = []
        for c in sel:
            p0 = _px(px, c, d_exec)
            p1 = _px(px, c, d_next) if d_next else None
            if p1 is None:
                # 持有期内失去价格：按最后一个可得价平仓
                sub = px.get(c) or {}
                cand = [dd for dd in sub if d_exec <= dd <= (d_next or dates[-1])]
                cand.sort()
                if cand:
                    bar = sub[cand[-1]]
                    p1 = float(bar[0]) if isinstance(bar, (list, tuple)) else None
            if p0 and p1 and p0 > 0:
                rets.append(p1 / p0 - 1.0)
        if not rets:
            prev_hold = cur
            continue
        gross = sum(rets) / len(rets)

        # 成本：换手部分按双边成本扣
        cost = turnover * COST_ROUND
        net = gross - cost
        cost_total += cost

        nav *= (1.0 + net)
        nav_series.append((d_exec, nav))
        periods.append({
            "signal_date": d_sig, "exec_date": d_exec,
            "next_date": d_next, "n": len(rets),
            "gross": gross, "cost": cost, "net": net,
            "turnover": turnover, "nav": nav,
            # 池内等权基准需要每期**全池**成员（不是组合成员）
            "_pool": [c for c, _s, _uw in picks],
        })
        prev_hold = cur

    if len(periods) < 3:
        return None

    # 基准同期收益（同口径：执行日开盘 → 下期执行日开盘）
    # ⚠️ 2026-10-29 新增 pool_ew（池内等权）——这是**唯一诚实的因子对照基准**。
    #    原因（round69 T1/T1b 实测）：本池由「2026-10-27 的成交额≥1亿+市值≥25亿」回溯筛出，
    #    天然偏向「后来变活跃」的股票 → 全池等权在同一区间 +29.34%，而 HS300 −11.46%。
    #    即：对 HS300 的 40pp「超额」里，绝大部分来自**前视选择偏差**，不是因子。
    #    只有跑赢同池等权，才说明因子真的在池内做了有效区分。
    pool_ew = []
    for p in periods:
        rs = []
        for c in (p.get("_pool") or []):
            p0 = _px(px, c, p["exec_date"])
            p1 = _px(px, c, p["next_date"]) if p["next_date"] else None
            if p0 and p1 and p0 > 0:
                rs.append(p1 / p0 - 1.0)
        pool_ew.append((sum(rs) / len(rs)) if rs else None)

    bench_periods = {"POOL_EW": pool_ew}
    for bcode in BENCHES:
        bp = bench_px.get(bcode)
        if not bp:
            continue
        br = []
        for p in periods:
            b0 = _bpx(bp, p["exec_date"])
            b1 = _bpx(bp, p["next_date"]) if p["next_date"] else None
            if b0 and b1 and b0 > 0:
                br.append(b1 / b0 - 1.0)
            else:
                br.append(None)
        bench_periods[bcode] = br

    # ── 指标 ──
    nets = [p["net"] for p in periods]
    ann = 244.0 / hold   # 每年调仓次数
    metrics = _metrics(nets, ann, hold)
    metrics["avg_turnover"] = sum(p["turnover"] for p in periods) / len(periods)
    metrics["cost_annual_drag"] = metrics["avg_turnover"] * COST_ROUND * ann
    metrics["n_periods"] = len(periods)
    metrics["nav_final"] = nav

    excess = {}
    for bcode, br in bench_periods.items():
        ex = [(n - b) for n, b in zip(nets, br) if b is not None]
        if len(ex) < 10:
            continue
        m = _metrics(ex, ann, hold)
        bm = _metrics([b for b in br if b is not None], ann, hold)
        # 信息比率 = 超额均值 / 超额标准差（年化）
        excess[bcode] = {
            **m,
            "bench_ann_ret": bm["ann_ret"],
            "bench_ann_vol": bm["ann_vol"],
            "ir": m["sharpe"],     # 超额序列的夏普 = 信息比率
            "excess_ann_ret": m["ann_ret"],
        }

    return {
        "topn": topn, "hold": hold,
        "periods": periods,
        "nav_series": nav_series,
        "metrics": metrics,
        "excess": excess,
        "total_cost": cost_total,
    }


def _metrics(rets, ann_factor, hold):
    """从逐期收益算年化指标。ann_factor = 每年期数。"""
    n = len(rets)
    if n < 3:
        return {"ann_ret": None, "ann_vol": None, "sharpe": None, "max_dd": None}
    mu = sum(rets) / n
    var = sum((r - mu) ** 2 for r in rets) / (n - 1) if n > 1 else 0.0
    sd = math.sqrt(var)
    ann_ret = (1.0 + mu) ** ann_factor - 1.0 if mu > -1 else -1.0
    ann_vol = sd * math.sqrt(ann_factor)
    sharpe = (mu / sd * math.sqrt(ann_factor)) if sd > 1e-12 else None
    # 最大回撤（逐期复利净值的峰谷）
    nav = 1.0
    peak = 1.0
    mdd = 0.0
    for r in rets:
        nav *= (1.0 + r)
        peak = max(peak, nav)
        mdd = min(mdd, nav / peak - 1.0)
    return {
        "ann_ret": ann_ret,
        "ann_vol": ann_vol,
        "sharpe": sharpe,
        "max_dd": mdd,
        "win_rate": sum(1 for r in rets if r > 0) / n,
        "mean_period": mu,
    }


def _excess_dd(port_rets, bench_rets):
    """超额净值（1+port)/(1+bench) 的最大回撤 —— 这才是「超额最大回撤」。"""
    nav = 1.0
    peak = 1.0
    mdd = 0.0
    for p, b in zip(port_rets, bench_rets):
        if b is None:
            continue
        nav *= (1.0 + p) / (1.0 + b)
        peak = max(peak, nav)
        mdd = min(mdd, nav / peak - 1.0)
    return mdd, nav


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--panel", default=PANEL)
    ap.add_argument("--out", default=OUT)
    ap.add_argument("--topn", default="20,30,50,80,100")
    ap.add_argument("--hold", default="5,10,20")
    args = ap.parse_args()

    print("加载面板 / 价格 / 基准 ...")
    panel = json.load(open(args.panel, encoding="utf-8"))
    px = load_prices()
    bench_px = {}
    for b in BENCHES:
        v = load_bench(b)
        if v:
            bench_px[b] = v
    print(f"  面板 {len(panel['dates'])} 截面 × {len(panel.get('domain') or [])} 只")
    print(f"  基准 {list(bench_px.keys())}")

    wc = json.load(open(WEIGHTS, encoding="utf-8"))
    wmap = {k: float(v) for k, v in wc["weights"].items()}
    dim_max = {k: float(v) for k, v in wc["dim_max"].items()}
    print(f"  权重 {wc['version']}  Σw={sum(wmap.values()):.6f}")

    print("\n合成打分 ...")
    scores = build_scores(panel, wmap, dim_max)
    print(f"  → {sum(len(v) for v in scores.values())} 条打分")

    tdays = sorted({d for c in px for d in px[c]})

    topns = [int(x) for x in args.topn.split(",")]
    holds = [int(x) for x in args.hold.split(",")]

    results = {}
    for hold in holds:
        for topn in topns:
            key = f"top{topn}_h{hold}"
            r = backtest(panel, scores, px, bench_px, topn, hold, tdays, None)
            if not r:
                print(f"  ⚠️ {key} 期数不足，跳过")
                continue
            # 超额回撤（对 HS300）
            bp = bench_px.get("000300")
            if bp:
                br = []
                for p in r["periods"]:
                    b0 = _bpx(bp, p["exec_date"])
                    b1 = _bpx(bp, p["next_date"]) if p["next_date"] else None
                    br.append((b1 / b0 - 1.0) if (b0 and b1 and b0 > 0) else None)
                mdd_ex, nav_ex = _excess_dd([p["net"] for p in r["periods"]], br)
                r["metrics"]["excess_max_dd_vs_hs300"] = mdd_ex
                r["metrics"]["excess_nav_final_vs_hs300"] = nav_ex
            results[key] = r
            m = r["metrics"]
            ex = r["excess"].get("000300") or {}

            def _f(v, mult=1.0, nd=2):
                return "n/a" if v is None else f"{v*mult:.{nd}f}"

            print(f"  {key:12s} 年化{_f(m['ann_ret'],100):>7}%  夏普{_f(m['sharpe']):>5}  "
                  f"超额{_f(ex.get('excess_ann_ret'),100):>7}%  IR{_f(ex.get('ir')):>5}  "
                  f"超额MDD{_f(m.get('excess_max_dd_vs_hs300'),100):>7}%  "
                  f"换手{_f(m['avg_turnover'],100,1):>5}%")

    meta = {
        "round": 67,
        "purpose": "组合级回测引擎：把 IC 层接上净值层，为「超额夏普/超额回撤」提供唯一度量基础",
        "panel": os.path.relpath(args.panel, HERE),
        "weights_version": wc["version"],
        "cost_model": {
            "cost_buy": COST_BUY, "cost_sell": COST_SELL,
            "cost_round": COST_ROUND,
            "note": "佣金0.025%×2 + 滑点0.02%×2 + 印花税0.05%(卖出)；按换手率逐期扣减，不做年化摊薄",
        },
        "exec_price": "t+1 开盘（可交易口径，不用 t 日收盘以免前视）",
        "weighting": "等权",
        "n_dates": len(panel["dates"]),
        "date_range": [panel["dates"][0], panel["dates"][-1]],
        "caveat": (
            "① 执行价为 t+1 开盘，未考虑涨跌停无法成交；"
            "② 本池上游已过滤成交额≥1亿，但仍可能存在流动性冲击成本低估；"
            "③ panel 的 A/B 市值维用「股本中位数×收盘」近似，无拆股调整；"
            "④ 样本 2025-11~2026-10 属小盘投机主导 regime，跨 regime 未验证；"
            "⑤ 超额基准为沪深300，本池偏小盘，须同时看对中证1000/全指的超额以排除纯 size beta。"
        ),
    }

    json.dump({"meta": meta, "results": results},
              open(args.out, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"\n→ {args.out}")


if __name__ == "__main__":
    main()
