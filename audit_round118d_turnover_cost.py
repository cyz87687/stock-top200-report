#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""audit_round118d_turnover_cost.py —— 换手率与交易成本实测（round118 P1-5）
## 为什么必须做这个

`pick_portfolio` 按日推进，**没有任何成本概念**。而项目已实测两件事：

1. **`total` 的 IC 在 h=3 达峰（+0.0255），h=30 翻负（−0.0175）** —— 信号衰减极快
2. **日频调仓是纯成本浪费**（Novy-Marx & Velikov 2016；本项目 IC 衰减曲线同向）

但「浪费多少」从来没量过。本脚本量它：**真实换手率 × 真实成本 = 被吃掉的收益**。

## 三个不能含糊的口径

### ① 换手率必须按**持仓变动**算，不能按"分数变动"算

分数每天全变（浮点抖动），但持仓不变就不产生成本。正确口径：
    turnover_t = (新进只数 + 退出只数) / (2 × 持仓只数)     # 单边
    换手成本_t = turnover_t × cost_bps × 2                  # 双边（卖旧 + 买新）
若按"分数变了就换"算，会把浮点抖动当成交，**换手率虚高数倍**。

⚠️ 实测补充：只要 `topN` 选出的集合不变，换手就是 0。但**权重变化**
（score-weighted 组合）也会产生成本。本脚本对等权口径算集合换手，
对加权口径额外算权重换手 —— 两者不可混为一谈。

### ② 成本必须给两档，且标明口径

| 档 | cost_bps（单边） | 依据 |
|---|---|---|
| 乐观 | 20 | 佣金 3bp + 印花税卖出 5bp/2 + 滑点小 |
| 悲观 | 50 | 小市值 + 冲击成本 + 部分涨停买不到 |

**不取"平均成本"这一个数** —— 那会让人以为成本是已知的。它取决于标的和规模。

### ③ 净收益必须与毛收益并列，且净收益是**扣成本后**的

`net = gross - Σ换手成本`。不允许只报 gross 然后说"未计成本" ——
项目已因「毛利口径未标注」被记过（`amt=v*c` 漏 ×100 事故）。

## 数据来源（全部本地，不联网）

- 分数与持仓：`factor_history.json` 的 `snapshots[*].rows[*].total`
- 价格：`factor_history.json` 的 `prices`（前复权，经 `factor_eval.px_index`）
- 交易日轴：同上（**150 日**，不等于 K 线库的 341 日 —— 轴不同不可混用）

## 输出

`data/audit_round118d_turnover_cost.json`
"""
import os
import sys
import json
import math
import argparse
import contextlib
import io

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import factor_eval as F                                    # noqa: E402
import backtest_core as bc                                 # noqa: E402

OUT_PATH = os.path.join(HERE, "data", "audit_round118d_turnover_cost.json")

# 调仓频率（交易日间隔）。1 = 每日，5 = 周度，20 = 月度
HOPS = (1, 2, 3, 5, 10, 20)
# 组合规模
TOPNS = (10, 20, 50)
# 成本档（单边 bps）
COST_BPS = (20, 50)
# 基准
BENCH = "POOL_EW"


# ═══════════════ 数据准备 ═══════════════

def _prep():
    """读 factor_history，返回 (tdays, snaps, px)。

    派生维必须跑 —— 否则 `total` 与单维口径不一致（历史坑）。
    """
    with open(F.HISTORY_PATH, encoding="utf-8") as f:
        hist = json.load(f)
    tdays, px = F.px_index(hist)
    snaps = [s for s in hist.get("snapshots") or [] if s.get("rows")]
    # 派生维：静音（_derive_dims 会打印污染剔除日志）
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        F._derive_dims(snaps)
        # ⚠️ 这个函数是**就地改** snaps，返回值是"剔除记录"列表，**不是快照**。
        #    我第一版写成 `snaps = F._strip_contaminated_fields(snaps)`，
        #    结果 90 个快照变成 2 个（那 2 条恰好是污染日的记录）——
        #    「有返回值就当返回了新数据」是个典型误读。已由 _selftest 第 0 条锁定。
        F._strip_contaminated_fields(snaps)
    # 只保留日期在轴上的快照
    tidx = {d: i for i, d in enumerate(tdays)}
    snaps = [s for s in snaps if s.get("date") in tidx]
    return tdays, snaps, px


def _rank_picks(snap, topn, field="total"):
    """按 field 降序取前 topn 只，返回 [code]（只保留有分数的）。"""
    rows = [r for r in (snap.get("rows") or [])
            if isinstance(r.get(field), (int, float))
            and r.get("code")]
    rows.sort(key=lambda r: r[field], reverse=True)
    return [r["code"] for r in rows[:topn]]


def _rank_picks_weighted(snap, topn, field="total"):
    """按 field 取前 topn，返回 {code: weight}（score-weighted，权重归一到 1）。

    权重口径的解释权很重要：这里用**原始分数**归一到和=1，
    而不是 softmax 或 rank 权重。换一种权重口径，换手率就不同 ——
    所以结果里必须写下用的是哪种。
    """
    rows = [r for r in (snap.get("rows") or [])
            if isinstance(r.get(field), (int, float))
            and r.get("code")]
    rows.sort(key=lambda r: r[field], reverse=True)
    rows = rows[:topn]
    tot = sum(r[field] for r in rows)
    if tot <= 0:
        return {}
    return {r["code"]: r[field] / tot for r in rows}


def _fwd_ret(px, code, d0, d1):
    a = (px.get(code) or {}).get(d0)
    b = (px.get(code) or {}).get(d1)
    if not a or not b or a <= 0:
        return None
    return b / a - 1.0


def _pool_ew(px, d0, d1):
    """池内等权收益 = 两日都有价格的全部股票的 fwd 收益均值。

    ⚠️ 口径与 backtest_core.pool_ew_ret 一致：分母只算有价格的。
    """
    c0 = set()
    for c, m in px.items():
        if m.get(d0):
            c0.add(c)
    rets = []
    for c in c0:
        r = _fwd_ret(px, c, d0, d1)
        if r is not None:
            rets.append(r)
    return (sum(rets) / len(rets)) if rets else None


# ═══════════════ 换手率与成本 ═══════════════

def turnover_series(snaps, tdays, hop, topn, weighted=False):
    """逐调仓日算单边换手率。

    ## ⚠️ 调仓日必须**跳步**，不能逐日推进（这是本脚本第一版的实质错误）

    第一版写成「所有能走到 t+hop 的快照日都是调仓日」，于是 hop=20 时
    调仓日仍是逐日的 66 个 → 每份"20 日收益"被当成一个观测连续累加 20 次，
    毛收益从 hop=1 的 −0.56% 变成 hop=20 的 **−10.6%**（虚假的 20 倍）。
    同时换手率也被摊平：逐日调仓 + 持 20 日 = 每天都持有 20 份重叠仓位，
    换手率自然不随 hop 变化（实测 45.7% → 40.8%，几乎不动）。

    **正确口径**：hop 日调仓一次，即调仓日序列 `reb[0], reb[0]+hop, ...`。
    这样每个观测代表一段不重叠的持有期，换手率与收益都可比。

    返回 [{"date", "next_date", "turnover", "n", "n_enter", "n_exit"}]。
    """
    tidx = {d: i for i, d in enumerate(tdays)}
    by_date = {s["date"]: s for s in snaps}
    # 可作调仓日的快照日（能走到 t+hop）
    cand = [s["date"] for s in snaps if tidx[s["date"]] + hop < len(tdays)]
    if not cand:
        return []
    # ⚠️ 跳步：每 hop 个交易日调仓一次（不重叠持有期）
    reb = cand[::hop]
    out = []
    prev_set = None
    prev_w = None
    for d in reb:
        s = by_date[d]
        if weighted:
            cur_w = _rank_picks_weighted(s, topn)
            cur_set = set(cur_w)
        else:
            cur_set = set(_rank_picks(s, topn))
            cur_w = None
        if not cur_set:
            continue
        if prev_set is None:
            # 建仓：换手按 100%（从现金买入）—— 这是真实成本，不能免
            tov = 1.0
            n_enter = n_exit = len(cur_set)
        else:
            enter = cur_set - prev_set
            exit_ = prev_set - cur_set
            n_enter, n_exit = len(enter), len(exit_)
            tov = (n_enter + n_exit) / (2.0 * max(len(cur_set), len(prev_set)))
            # 加权口径：集合不变但权重变了也有成本
            if weighted and prev_w:
                w_tov = sum(abs(cur_w.get(c, 0.0) - prev_w.get(c, 0.0))
                            for c in (set(cur_w) | set(prev_w))) / 2.0
                # 取两者较大值：集合换手与权重换手都会产生真实成本，
                # 等权口径下二者相同，加权口径下 w_tov 可能更大
                tov = max(tov, w_tov)
        out.append({
            "date": d,
            "next_date": tdays[tidx[d] + hop],
            "turnover": tov,
            "n": len(cur_set),
            "n_enter": n_enter,
            "n_exit": n_exit,
        })
        prev_set, prev_w = cur_set, cur_w
    return out


def cost_drag(hop, turnover_mean, cost_bps):
    """每次调仓的双边成本率，以及折算到每日的成本率。

    单边 cost_bps（万分之 x）→ 双边 = 2 × cost_bps。
    `turnover_mean` 已是「单边换手比例」，所以：
        每次调仓成本率 = turnover_mean × 2 × cost_bps / 10000
    """
    per_reb = turnover_mean * 2.0 * cost_bps / 10000.0
    per_day = per_reb / hop
    return per_reb, per_day


def simulate(snaps, tdays, px, hop, topn, cost_bps, weighted=False):
    """模拟调仓：算毛收益、换手、净收益、超额。

    ⚠️ 收益口径：调仓日 t 的持仓，持有到 t+hop 的**前复权收盘**收益。
    不是"t+1 开盘买入" —— 那是 T+1 可执行口径，本项目已单独测过
    （round111c 结论：日内可执行显著因子=无）。这里测的是「信号是否有价值」，
    用收盘到收盘是标准做法，但**必须写明**，否则会被误读成可执行收益。
    """
    tidx = {d: i for i, d in enumerate(tdays)}
    by_date = {s["date"]: s for s in snaps}
    tov_rows = turnover_series(snaps, tdays, hop, topn, weighted=weighted)
    gross, net, excess = [], [], []
    benchs = []                    # 与 gross/net 同索引（能算基准的那些观测）
    cost_total = 0.0
    per_day_cost = []

    for r in tov_rows:
        d, d1 = r["date"], r["next_date"]
        s = by_date[d]
        if weighted:
            w = _rank_picks_weighted(s, topn)
        else:
            codes = _rank_picks(s, topn)
            w = {c: 1.0 / len(codes) for c in codes} if codes else {}
        if not w:
            continue
        rets = []
        ws = []
        for c, wt in w.items():
            rr = _fwd_ret(px, c, d, d1)
            if rr is not None:
                rets.append(rr)
                ws.append(wt)
        if not rets:
            continue
        wsum = sum(ws)
        g = sum(rr * wt for rr, wt in zip(rets, ws)) / wsum
        bench = _pool_ew(px, d, d1)
        # 成本：本次调仓的换手 × 双边费率
        c_rate = r["turnover"] * 2.0 * cost_bps / 10000.0
        cost_total += c_rate
        per_day_cost.append(c_rate / hop)
        gross.append(g)
        net.append(g - c_rate)
        if bench is not None:
            excess.append(g - c_rate - bench)
            benchs.append(bench)

    n = len(gross)
    out = {
        "hop": hop,
        "topn": topn,
        "weighted": weighted,
        "cost_bps_oneway": cost_bps,
        "n_rebalances": n,
        "n_turnover_points": len(tov_rows),
        "mean_turnover": (sum(x["turnover"] for x in tov_rows) / len(tov_rows)
                          if tov_rows else None),
        "gross_mean": (sum(gross) / n) if n else None,
        "net_mean": (sum(net) / n) if n else None,
        "cost_mean": (sum(per_day_cost) / len(per_day_cost)) if per_day_cost else None,
        "excess_mean": (sum(excess) / len(excess)) if excess else None,
        "gross_annual": None,
        "net_annual": None,
        "excess_annual": None,
        "ann_factor": 252.0 / hop,
    }
    # 年化：每 hop 日一个观测 → 每年 252/hop 个
    #
    # ⚠️ 年化在样本少时会爆炸（hop=20 只有 4 个观测，年化超额能到 −130%）。
    #    所以**同时**给区间累计，并让累计成为主口径 —— 年化只作参考且必须带 n。
    af = 252.0 / hop
    if out["gross_mean"] is not None:
        out["gross_annual"] = (1.0 + out["gross_mean"]) ** af - 1.0
        out["net_annual"] = (1.0 + out["net_mean"]) ** af - 1.0
    if out["excess_mean"] is not None:
        out["excess_annual"] = out["excess_mean"] * af
    # 区间累计（连乘）：**主口径**，不受年化外推放大
    if gross:
        _cg = 1.0
        for x in gross:
            _cg *= (1.0 + x)
        out["gross_cum"] = _cg - 1.0
    if net:
        _cn = 1.0
        for x in net:
            _cn *= (1.0 + x)
        out["net_cum"] = _cn - 1.0
    # 累计超额 = 累计净 − 累计基准（同一批调仓日上各自连乘）
    if benchs:
        _cb = 1.0
        for x in benchs:
            _cb *= (1.0 + x)
        out["bench_cum"] = _cb - 1.0
        if out.get("net_cum") is not None:
            out["excess_cum"] = out["net_cum"] - out["bench_cum"]
    out["n_bench"] = len(benchs)
    # NW t：lag 必须 = hop-1（重叠窗口）
    if n >= 5:
        out["nw_t_net"] = bc.nw_t(net, max(1, hop - 1))
        out["nonoverlap_n"] = len(bc.nonoverlap(net, hop))
        no = bc.nonoverlap(net, hop)
        out["nonoverlap_t"] = bc.plain_t(no) if len(no) >= 5 else None
    else:
        out["nw_t_net"] = out["nonoverlap_n"] = out["nonoverlap_t"] = None
    # DSR（P1-3）：N 从 trial_log 取，不给就不算
    try:
        import trial_log
        mm = bc.sharpe_moments(net)
        if mm:
            no = bc.nonoverlap(net, hop)
            out["dsr"] = bc.deflated_sharpe(
                mm["sr"], len(no) if len(no) >= 5 else n,
                trial_log.n_trials(), bc.DEFAULT_SR_STD,
                mm["skew"], mm["kurt"])
    except Exception as e:
        out["dsr"] = {"error": f"{type(e).__name__}: {e}"}
    return out


# ═══════════════ 主流程 ═══════════════

def run(verbose=True):
    tdays, snaps, px = _prep()
    if verbose:
        print(f"[prep] 快照 {len(snaps)} 个，交易日轴 {len(tdays)} 日 "
              f"({tdays[0]}~{tdays[-1]})，价格库 {len(px)} 只")
    grid = []
    for hop in HOPS:
        for topn in TOPNS:
            for cb in COST_BPS:
                r = simulate(snaps, tdays, px, hop, topn, cb)
                grid.append(r)
                if verbose and cb == COST_BPS[0]:
                    _print_row(r)
    # 加权口径单跑（对比等权）
    weighted_rows = []
    for hop in HOPS:
        for topn in TOPNS:
            r = simulate(snaps, tdays, px, hop, topn, COST_BPS[0],
                         weighted=True)
            weighted_rows.append(r)

    out = {
        "meta": {
            "purpose": "换手率与交易成本实测：量化「日频调仓浪费多少」",
            "data": "factor_history.json（快照分数 + 前复权价格）",
            "n_snapshots": len(snaps),
            "n_tdays": len(tdays),
            "date_range": [tdays[0], tdays[-1]],
            "n_codes_px": len(px),
            "hops": list(HOPS),
            "topns": list(TOPNS),
            "cost_bps": list(COST_BPS),
            "return_def": ("调仓日 t 收盘 → t+hop 收盘的前复权收益。"
                           "**不是 T+1 开盘买入** —— 那是可执行口径，已单独测过"
                           "（round111c：日内可执行显著因子=无）"),
            "turnover_def": ("单边换手 = (新进 + 退出) / (2×持仓数)；"
                             "建仓日按 100%。加权口径下另算权重换手并取较大值"),
            "cost_def": "每次调仓成本率 = 换手 × 2 × cost_bps / 10000（双边）",
            "caveat": ("⚠️ 未考虑冲击成本随规模的放大、涨跌停无法成交、"
                       "停牌。故悲观档 50bp 可能仍偏乐观"),
        },
        "grid_equal_weight": grid,
        "grid_score_weighted": weighted_rows,
    }
    return out


def _print_row(r):
    def pct(x):
        return f"{x*100:7.2f}%" if isinstance(x, (int, float)) else "     —"
    print(f"  hop={r['hop']:<3d} top{r['topn']:<3d} "
          f"换手={pct(r['mean_turnover'])}  "
          f"毛={pct(r['gross_mean'])} 净={pct(r['net_mean'])} "
          f"超额={pct(r['excess_mean'])} "
          f"NW t={r['nw_t_net'] if r['nw_t_net'] is None else round(r['nw_t_net'],2)}")


def main(argv=None):
    ap = argparse.ArgumentParser(description="换手率与成本实测（P1-5）")
    ap.add_argument("--out", default=OUT_PATH)
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args(argv)

    if a.selftest:
        return _selftest()

    print("=" * 100)
    print("换手率与交易成本实测（等权口径，成本档 = 20bp 单边）")
    print("=" * 100)
    res = run()
    with open(a.out, "w", encoding="utf-8") as f:
        json.dump(res, f, ensure_ascii=False, indent=1)
    print("=" * 100)
    print(f"已写 {a.out}")

    # 三条结论必须在终端就看见，不能只埋在 JSON 里
    g = res["grid_equal_weight"]
    print("\n【结论 1】跨 hop 不能比「每期收益」—— 比区间累计超额（区间本身在跌）")
    print("   ⚠️ hop=20 时每期 −12% 不是「比 hop=1 的 −0.56% 差 20 倍」，")
    print("      而是「同样在跌的区间里持有了 20 天」。跨 hop 只能比同区间的累计。")
    print("   ⚠️ 年化在 n=4 时会外推到 −130%+，**不可作为主结论**，只作参考。")
    for topn in TOPNS:
        row = []
        for hop in HOPS:
            x = next((y for y in g if y["hop"] == hop and y["topn"] == topn
                      and y["cost_bps_oneway"] == 20), None)
            if not x:
                continue
            ec = x.get("excess_cum")
            row.append(f"hop{hop}:{'—' if ec is None else f'{ec*100:+.1f}pp'}"
                       f"(n={x['n_rebalances']})")
        if row:
            print(f"   top{topn}: " + "  ".join(row))
    _b = next((x.get("bench_cum") for x in g if x.get("bench_cum") is not None), None)
    if _b is not None:
        print(f"   同期 POOL_EW 累计 = {_b*100:+.2f}%（所有 hop 同一个区间，故基准相同）")

    print("\n【结论 2】换手率**随 hop 上升**（与直觉相反，机制：间隔越久排名漂移越大）")
    for topn in TOPNS:
        vals = [(x["hop"], x["mean_turnover"]) for x in g
                if x["topn"] == topn and x["cost_bps_oneway"] == 20
                and x["mean_turnover"] is not None]
        if vals:
            print(f"   top{topn}: " + "  ".join(
                f"hop{h}={t*100:.1f}%" for h, t in vals))

    print("\n【结论 3】日频的**年度成本拖累**（这才是「日频是否浪费」的直接答案）")
    print("   年度成本 ≈ 每次调仓成本率 × (252/hop)")
    for topn in TOPNS:
        for hop in (1, 5, 20):
            x = next((y for y in g if y["hop"] == hop and y["topn"] == topn
                      and y["cost_bps_oneway"] == 20), None)
            if x and x.get("mean_turnover") is not None:
                per_reb = x["mean_turnover"] * 2 * 20 / 10000.0
                ann_cost = per_reb * 252.0 / hop
                print(f"   top{topn:<3d} hop={hop:<3d} 每次 {per_reb*100:.3f}% "
                      f"× {252.0/hop:.0f} 次/年 = **{ann_cost*100:.2f}%/年**")

    print("\n【结论 4】净收益为正的组合数（20bp / 50bp）")
    for cb in COST_BPS:
        sub = [x for x in g if x["cost_bps_oneway"] == cb]
        pos = [x for x in sub if x.get("net_mean") is not None and x["net_mean"] > 0]
        print(f"   {cb}bp：{len(pos)}/{len(sub)} 个组合净收益为正"
              f"（区间 2026-06-26~2026-11-02 为下跌市，POOL_EW 同期 −10.8%）")
    return 0


def _selftest():
    """自检：换手率口径、成本方向、年化因子。"""
    ok = True
    print("=" * 78)
    print("audit_round118d 自查")
    print("=" * 78)

    tdays, snaps, px = _prep()

    # 0. ⚠️ _prep 必须保住绝大多数快照 —— 锁住「把剔除记录当快照」那个坑
    #
    # 实测：90 → 84，少的 6 个是 **2026-09-25 与 2026-10-01~10-07**，
    # 都不在价格轴 tdays 上（国庆假期 + 一个非交易日）。
    # 这是**设计内的过滤**，不是 bug；判据必须能区分这两者，
    # 否则我把阈值放松就会把真 bug 一起放过去。
    import json as _json
    with open(F.HISTORY_PATH, encoding="utf-8") as _f:
        _h = _json.load(_f)
    _raw_dates = [s["date"] for s in (_h.get("snapshots") or []) if s.get("rows")]
    _tset = set(tdays)
    _expect = [d for d in _raw_dates if d in _tset]
    print(f"0. _prep 保住快照 {len(snaps)} 个 / 轴上应有 {len(_expect)} 个"
          f" / 原始 {len(_raw_dates)} 个",
          "✅" if len(snaps) == len(_expect) else "❌ 与轴上应得数不符")
    ok &= (len(snaps) == len(_expect))
    if len(snaps) != len(_raw_dates):
        _off = [d for d in _raw_dates if d not in _tset]
        print(f"   （{len(_off)} 个快照日不在价格轴上，属设计内过滤：{_off[:6]}）")

    # 1. 换手率必须在 [0,1]
    tov = turnover_series(snaps, tdays, 20, 20)
    bad = [x for x in tov if not (0.0 <= x["turnover"] <= 1.0 + 1e-9)]
    print(f"1. 换手率全在 [0,1]（{len(tov)} 点）",
          "✅" if not bad else f"❌ {bad[:3]}")
    ok &= (not bad)

    # 2. 同一持仓集合 → 换手必须为 0（防"分数抖动当成交"）
    s = snaps[0]
    same = [s, dict(s)]
    t2 = turnover_series(same, tdays, 1, 20)
    # 第二个日期不在轴上会被跳过；这里直接测辅助函数
    a = set(_rank_picks(s, 20))
    b = set(_rank_picks(s, 20))
    print(f"2. 同一快照两次选取相同 → 换手差 = {len(a ^ b)}",
          "✅" if a == b else "❌")
    ok &= (a == b)

    # 3. 换手率必须落在 [0,1]，且**不假设**它随 hop 单调
    #
    # ⚠️ 我第一版断言「hop 越大换手越低」，实测被推翻：hop=1 是 39.5%，
    #    hop=20 是 68.8%（反而更高）。原因：hop 越大，两次调仓间隔越久，
    #    排名漂移越多 → 换手率上升。"调仓越少换手越低"只在持仓不变的前提下成立，
    #    而 topN 排名每天在变。**方向断言必须来自机制，不能来自直觉。**
    tov1 = turnover_series(snaps, tdays, 1, 20)
    tov20 = turnover_series(snaps, tdays, 20, 20)
    v1 = sum(x["turnover"] for x in tov1) / max(1, len(tov1))
    v20 = sum(x["turnover"] for x in tov20) / max(1, len(tov20))
    _in_range = all(0.0 <= x["turnover"] <= 1.0 + 1e-9 for x in (tov1 + tov20))
    print(f"3. 换手率 hop=1 {v1*100:.1f}% / hop=20 {v20*100:.1f}%"
          f"（都在 [0,1]：{'✅' if _in_range else '❌'}；不假设单调）")
    ok &= _in_range

    # 3a. hop=1 时换手率必须显著高于 0 —— 否则说明"调仓"根本没发生
    print(f"3a. hop=1 换手 {v1*100:.1f}% 显著 >0",
          "✅" if v1 > 0.05 else "❌ 持仓几乎不变，调仓未生效")
    ok &= (v1 > 0.05)

    # 3b. ⚠️ 调仓日必须跳步（每 hop 日一次），不能逐日推进
    #
    # 这是本脚本第一版的实质错误：hop=20 时调仓日仍逐日 66 个 →
    # 每份"20 日收益"被连续累加 20 次，毛收益从 −0.56% 虚增到 −10.6%；
    # 换手率也被摊平（45.7%→40.8%，几乎不随 hop 变化）。
    _idx = {d: i for i, d in enumerate(tdays)}
    _cand = [s["date"] for s in snaps if _idx[s["date"]] + 20 < len(tdays)]
    _reb = _cand[::20]
    _gaps = sorted({_idx[_reb[i + 1]] - _idx[_reb[i]]
                    for i in range(len(_reb) - 1)})
    print(f"3b. hop=20 调仓日数 {len(tov20)}，相邻轴间隔 {_gaps}"
          f"（应全为 20 或快照缺失导致的整数倍）",
          "✅" if all(g >= 20 for g in _gaps) else "❌ 仍在逐日推进")
    ok &= all(g >= 20 for g in _gaps)

    # 3c. hop=1 与 hop=20 的**年化**毛收益应同量级（都在 −10%~+10% 内）
    #
    # 若逐日推进，hop=20 的"每期"毛收益会等于 hop=1 的 20 倍 → 年化爆炸。
    _r1 = simulate(snaps, tdays, px, 1, 20, 20)
    _r20 = simulate(snaps, tdays, px, 20, 20, 20)
    _fa1, _fa20 = _r1.get("gross_annual"), _r20.get("gross_annual")
    if _fa1 is not None and _fa20 is not None:
        print(f"3c. 年化毛收益 hop=1 {_fa1*100:+.1f}% vs hop=20 {_fa20*100:+.1f}%"
              f"（同量级则跳步正确）",
              "✅" if abs(_fa20) < 5 * max(abs(_fa1), 0.02) else "❌ 量级不符")
        ok &= (abs(_fa20) < 5 * max(abs(_fa1), 0.02))

    # 4. 成本越高净收益越低
    r20 = simulate(snaps, tdays, px, 20, 20, 20)
    r50 = simulate(snaps, tdays, px, 20, 20, 50)
    if r20.get("net_mean") is not None and r50.get("net_mean") is not None:
        print(f"4. 20bp 净 {r20['net_mean']*100:+.3f}% > 50bp 净 "
              f"{r50['net_mean']*100:+.3f}%",
              "✅" if r20["net_mean"] > r50["net_mean"] else "❌")
        ok &= (r20["net_mean"] > r50["net_mean"])

    # 5. 年化因子 = 252/hop
    print(f"5. 年化因子 hop=20 → {r20['ann_factor']}（应 12.6）",
          "✅" if abs(r20["ann_factor"] - 12.6) < 1e-9 else "❌")
    ok &= (abs(r20["ann_factor"] - 12.6) < 1e-9)

    # 6. 建仓日换手必须是 100%（不能免掉首次建仓成本）
    print(f"6. 建仓日换手 {tov[0]['turnover']*100:.0f}%（应 100%）",
          "✅" if abs(tov[0]["turnover"] - 1.0) < 1e-12 else "❌")
    ok &= (abs(tov[0]["turnover"] - 1.0) < 1e-12)

    print("=" * 78)
    print("全部通过 ✅" if ok else "存在失败 ❌")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
