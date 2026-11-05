#!/usr/bin/env python3
"""exec_backtest.py —— 可执行净值回测模拟器（round130 / P1-3）。

审计 §三.4：现有 TopN 曲线是「重叠 20 日**毛**收益」的预测诊断，没有次日
成交、资金占用、换手、税费、滑点、涨跌停、停牌与退市规则，不能当可实现净值。
本模块另建一条**独立**模拟器，与诊断链分离：

  * 信号日 T → **T+1 开盘**成交（可用 next_close 对照），不使用 T 日收盘价成交；
  * 双边佣金 + 卖出印花税 + 滑点，逐笔计入现金流；
  * 涨停不可买 / 跌停不可卖 / 当日无 bar（停牌）→ 拒单并留痕，不假设成交；
  * 退市/长期停牌：按最后有效价终止并显式标注，不静默归零也不静默忽略；
  * 现金 0 收益，换手按 Σ|Δw|/2 计，净值与毛值**分别**输出；
  * 覆盖率不足时拒绝给出收益结论。

⚠️ 边界（必须随结果一起披露）
   1. 本模拟器是**执行层工具**，不修复因子/数据/过拟合问题；
   2. 前复权价已含分红送转调整，但未建模真实分红现金流与融券；
   3. 涨跌停用「T 日相对前收的近似阈值」，不是交易所逐笔撮合；
   4. 组合权重序列来自既有 portfolio 产物；若历史组合本身是同一段数据
      上选出的，回测仍是**样本内**，不得称样本外验证。
   5. 不连接券商、不自动交易，仅用于研究。

用法：
    python exec_backtest.py --selfcheck
    python exec_backtest.py --from-portfolios --out data/exec_backtest_round130.json
    python exec_backtest.py --rebalances my_rebalances.json --out /tmp/exec.json
"""
import argparse
import glob
import json
import os
import sys
from collections import defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
KLONG = os.path.join(HERE, "data", "klines_long.json")
SECTOR_DIR = os.path.join(HERE, "sector")

DEFAULT_CONFIG = {
    "exec_price": "next_open",       # next_open | next_close
    "commission_bps": 3.0,           # 双边，各 3bp（万三）
    "stamp_duty_bps": 5.0,           # 仅卖出，5bp（0.05%）
    "slippage_bps": 5.0,             # 买卖各 5bp
    "limit_up_pct": 0.098,           # 主板近似；创/科/北另有阈值
    "limit_special_pct": 0.198,
    "delist_policy": "last_price",   # 最后有效价终止（并标注）
    "min_price_coverage": 0.80,
}
SPECIAL_PREFIX = ("300", "301", "688", "689", "8", "4")


def _c6(code):
    s = str(code or "").strip().lower()
    for p in ("sh", "sz", "bj"):
        if s.startswith(p):
            return s[len(p):]
    return s


def _bar(px, c, d):
    b = (px.get(c) or {}).get(d)
    if isinstance(b, (list, tuple)) and len(b) >= 2:
        return b
    if isinstance(b, (int, float)) and b == b:
        return [float(b), float(b)]
    return None


def _close(px, c, d):
    b = _bar(px, c, d)
    if b and isinstance(b[1], (int, float)) and b[1] == b[1]:
        return float(b[1])
    return None


def _open(px, c, d, mode):
    b = _bar(px, c, d)
    if not b:
        return None
    idx = 0 if mode == "next_open" else 1
    v = b[idx] if len(b) > idx else b[1]
    return float(v) if isinstance(v, (int, float)) and v == v and v > 0 else None


def calendar_of(px):
    try:
        import kline_calendar as kc
        return kc.build_valid(px)
    except Exception:
        ds = sorted({d for by in px.values() for d in by})
        return [d for d in ds if isinstance(d, str) and len(d) == 10]


def _limit_for(code):
    return (DEFAULT_CONFIG["limit_special_pct"]
            if str(code).startswith(SPECIAL_PREFIX)
            else DEFAULT_CONFIG["limit_up_pct"])


def from_portfolios(limit=None):
    """把历史 sector/portfolio_<date>.json 转成再平衡序列。"""
    fs = sorted(f for f in glob.glob(os.path.join(SECTOR_DIR, "portfolio_*.json"))
                if "latest" not in os.path.basename(f))
    if limit:
        fs = fs[-limit:]
    out = []
    for p in fs:
        try:
            with open(p, encoding="utf-8") as f:
                d = json.load(f)
        except Exception:
            continue
        date = d.get("date") or os.path.basename(p)[10:20]
        for mode, pf in (d.get("portfolios") or {}).items():
            w = pf.get("weights") or {}
            if not w:
                continue
            out.append({"date": date, "mode": mode,
                        "weights": {_c6(k): float(v) for k, v in w.items()},
                        "cash": float((pf.get("risk") or {}).get("cash_weight") or 0.0),
                        "source": os.path.basename(p)})
    return out


def from_long_panel(px, cal, top_n=20, every=20, w_total=0.8):
    """用长面板统一内核的合成分构造多期 TopN 等权再平衡序列。

    ⚠️ 这是**执行层演示**：信号来自代理面板（factor_parity=False），
       样本内、无 purge、无样本外；净值只能用来检验执行/成本口径，
       不得作为策略有效性证据。假设全部写入返回的 meta。
    """
    import numpy as np
    from dim_cfg import load_dim_max
    from scoring_core import (artifact_meta, classify_artifact,
                              composite_total_matrix)
    mat = os.path.join(HERE, "data", "panel_r124_mat.npz")
    with open(os.path.join(HERE, "factor_weights.json"), encoding="utf-8") as f:
        _cfg = json.load(f)
    W = {k: float(v) for k, v in (_cfg.get("weights") or {}).items()
         if float(v or 0) > 0}
    DM = load_dim_max(["score_bias60", "score_rsi_rev"], strict=True)
    z = np.load(mat, allow_pickle=False)
    pdates = [str(d) for d in z["dates"]]
    codes = [str(c) for c in z["codes"]]
    dims = [str(d) for d in z["dims"]]
    cidx = {d: i for i, d in enumerate(cal)}
    cand = [(i, d) for i, d in enumerate(pdates)
            if d in cidx and cidx[d] + 1 < len(cal)]
    sel = cand[::max(1, every)]
    if not sel:
        return [], {"error": "长面板与行情日历无可执行交集"}
    S = z["S"][[i for i, _ in sel]]
    comp, cmeta = composite_total_matrix(S, dims, W, DM)
    reb = []
    for k, (_, d) in enumerate(sel):
        row = comp[k]
        order = np.argsort(-np.nan_to_num(row, nan=-1e9))
        picked = [codes[j] for j in order[:top_n] if np.isfinite(row[j])]
        if not picked:
            continue
        w = w_total / len(picked)
        reb.append({"date": d, "weights": {c: w for c in picked},
                    "cash": round(1.0 - w_total, 6),
                    "source": "long_panel_topn_proxy", "n_picks": len(picked)})
    _am = artifact_meta(kernel_mode="production", factor_parity=False)
    meta = {
        "signal_source": "data/panel_r124_mat.npz 合成分 TopN",
        "composite": cmeta,
        "kernel_class": classify_artifact(_am),
        "assumptions": {"top_n": top_n, "rebalance_every_trading_days": every,
                        "w_total": w_total, "weights": "等权",
                        "signal_to_execution": "T 日信号 → T+1 开盘成交"},
        "disclosure": ("代理面板因子、样本内、无 purge/embargo、无样本外；"
                       "仅用于检验执行与成本口径，禁止当作策略收益或 alpha 证据。"),
    }
    return reb, meta


def simulate(rebalances, px, cal, config=None, charge_cost=True):
    """逐日盯市的可执行净值模拟。返回 dict（含净值、成本、拒单、覆盖率）。"""
    cfg = dict(DEFAULT_CONFIG)
    cfg.update(config or {})
    cal = list(cal)
    cidx = {d: i for i, d in enumerate(cal)}
    # 信号日 → T+1 执行日
    plan = defaultdict(list)
    for r in sorted(rebalances, key=lambda x: x["date"]):
        d = r["date"]
        if d not in cidx or cidx[d] + 1 >= len(cal):
            continue
        plan[cal[cidx[d] + 1]].append(r)
    if not plan:
        return {"error": "无可执行再平衡（信号日不在日历内或已是最后一日）"}

    start, end = min(cidx[d] for d in plan), len(cal) - 1
    holdings = {}          # code6 -> shares
    cash = 1.0
    nav = []
    rejected = []
    n_rebal = 0
    turnover_sum = 0.0
    cost_sum = 0.0
    last_price = {}
    stale = set()
    cov_list = []

    def _nav_at(d):
        v = cash
        for c, sh in holdings.items():
            p = _close(px, c, d)
            if p is None:
                p = last_price.get(c)
                if p is not None:
                    stale.add(c)
            else:
                last_price[c] = p
            if p is not None:
                v += sh * p
        return v

    for i in range(start, end + 1):
        d = cal[i]
        # ── 执行再平衡（T+1 开盘）──
        for r in plan.get(d, []):
            n_rebal += 1
            total = _nav_at(d)
            if total <= 0:
                continue
            target = {c: float(w) * total for c, w in r["weights"].items()}
            w_prev = {}
            for c, sh in holdings.items():
                p = _close(px, c, d) or last_price.get(c)
                if p:
                    w_prev[c] = sh * p / total
            all_codes = set(target) | set(holdings)
            for c in sorted(all_codes):
                cur_val = w_prev.get(c, 0.0) * total
                tgt_val = target.get(c, 0.0)
                delta = tgt_val - cur_val
                if abs(delta) < total * 1e-6:
                    continue
                p = _open(px, c, d, cfg["exec_price"])
                if p is None:
                    rejected.append({"date": d, "code": c, "reason": "无行情(停牌/退市)"})
                    continue
                prev_close = _close(px, c, cal[i - 1]) if i > 0 else None
                lim = _limit_for(c)
                if delta > 0 and prev_close and p >= prev_close * (1 + lim) - 1e-9:
                    rejected.append({"date": d, "code": c, "reason": "涨停不可买"})
                    continue
                if delta < 0 and prev_close and p <= prev_close * (1 - lim) + 1e-9:
                    rejected.append({"date": d, "code": c, "reason": "跌停不可卖"})
                    continue
                slip = cfg["slippage_bps"] / 1e4
                fill = p * (1 + slip) if delta > 0 else p * (1 - slip)
                shares_delta = delta / fill
                fee = abs(delta) * (cfg["commission_bps"] +
                                    (cfg["stamp_duty_bps"] if delta < 0 else 0)) / 1e4
                if charge_cost:
                    cash -= fee
                    cost_sum += fee
                cash -= delta
                holdings[c] = holdings.get(c, 0.0) + shares_delta
                if abs(holdings[c]) < 1e-12:
                    holdings.pop(c, None)
            turnover_sum += sum(abs(target.get(c, 0.0) - w_prev.get(c, 0.0) * total)
                                for c in all_codes) / (2 * total)

        # ── 盯市 ──
        # ⚠️ 覆盖率分母必须是「持仓市值」而非总资产：现金不需要行情，
        #    否则半仓组合的覆盖率上限只有 0.5，会被永久误判为数据不足。
        v = cash
        covered = 0.0
        held = 0.0
        for c, sh in holdings.items():
            p = _close(px, c, d)
            has_quote = p is not None
            if p is None:
                p = last_price.get(c)
                if p is not None:
                    stale.add(c)
            else:
                last_price[c] = p
            if p is not None:
                pv = abs(sh * p)
                v += sh * p
                held += pv
                if has_quote:
                    covered += pv
        cov_list.append(covered / held if held > 1e-12 else 1.0)
        nav.append([d, round(v, 6)])

    mdd, at = 0.0, None
    peak = nav[0][1] if nav else 1.0
    for d, v in nav:
        peak = max(peak, v)
        dd = (v - peak) / peak if peak > 0 else 0.0
        if dd < mdd:
            mdd, at = dd, d
    cov_mean = sum(cov_list) / len(cov_list) if cov_list else 0.0
    out = {
        "n_days": len(nav),
        "n_rebalances": n_rebal,
        "nav": nav,
        "net_return_pct": round((nav[-1][1] - 1.0) * 100, 4) if nav else None,
        "mdd_pct": round(mdd * 100, 4),
        "mdd_at": at,
        "total_cost_pct": round(cost_sum * 100, 4),
        "turnover": round(turnover_sum, 4),
        "n_rejected_trades": len(rejected),
        "rejected_trades": rejected[:50],
        "delisted_or_stale_positions": sorted(stale),
        "price_coverage_mean": round(cov_mean, 4),
        "coverage_gate_ok": bool(cov_mean >= cfg["min_price_coverage"]),
        "config": cfg,
        "delist_policy": cfg["delist_policy"],
        "disclosure": ("净值为 T+1 开盘成交、含佣金/印花税/滑点、拒单留痕的执行模拟；"
                       "毛值不含成本。样本内、前复权、涨跌停近似、不建模分红现金流；"
                       "不构成投资建议，不证明策略有效性。"),
    }
    if not out["coverage_gate_ok"]:
        out["net_return_pct"] = None
        out["mdd_pct"] = None
        out["reason"] = "行情覆盖率不足，拒绝给出收益/回撤结论"
    return out


def run(rebalances, px, cal, config=None):
    """同时给净值与毛值（毛值 = 关闭费用，其余执行规则完全相同）。"""
    gross = simulate(rebalances, px, cal, config, charge_cost=False)
    net = simulate(rebalances, px, cal, config, charge_cost=True)
    if "error" in net:
        return net
    net["gross_return_pct"] = gross.get("net_return_pct")
    net["gross_mdd_pct"] = gross.get("mdd_pct")
    net["cost_drag_pp"] = (round(gross["net_return_pct"] - net["net_return_pct"], 4)
                           if net.get("net_return_pct") is not None
                           and gross.get("net_return_pct") is not None else None)
    return net


def selfcheck():
    """构造数据验证：T+1 成交、成本、拒单、覆盖率（不依赖真实行情）。"""
    cal = [f"2026-01-{i:02d}" for i in range(5, 15)]
    px = {
        # 平缓上涨（每日 +0.1%），不触发涨停；用于验证成交与成本
        "000001": {d: [10.0 + 0.01 * i, 10.0 + 0.01 * i,
                       10.0 + 0.01 * i, 10.0 + 0.01 * i, 100]
                   for i, d in enumerate(cal)},
        # 涨停不可买：T+1 开盘较前收 +9.8%（正好触及近似阈值）
        "000002": {cal[0]: [10.0, 10.0, 10.0, 10.0, 100],
                   cal[1]: [10.98, 10.98, 10.98, 10.98, 100]},
    }
    reb = [{"date": cal[0], "weights": {"000001": 0.5, "000002": 0.5}, "cash": 0.0}]
    r = run(reb, px, cal)
    assert r["n_rebalances"] == 1, r
    assert r["n_rejected_trades"] == 1, r
    assert r["rejected_trades"][0]["reason"] == "涨停不可买", r
    assert r["total_cost_pct"] > 0, r
    assert r["net_return_pct"] < r["gross_return_pct"], r
    assert r["cost_drag_pp"] > 0, r
    return {"passed": True, "n_rejected": r["n_rejected_trades"],
            "net_pct": r["net_return_pct"], "gross_pct": r["gross_return_pct"],
            "cost_drag_pp": r["cost_drag_pp"]}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--selfcheck", action="store_true")
    ap.add_argument("--from-portfolios", action="store_true")
    ap.add_argument("--from-long-panel", action="store_true",
                    help="用长面板统一内核合成分构造多期 TopN 再平衡（执行层演示）")
    ap.add_argument("--top-n", type=int, default=20)
    ap.add_argument("--every", type=int, default=20, help="每隔 N 个交易日调仓")
    ap.add_argument("--w-total", type=float, default=0.8)
    ap.add_argument("--portfolio-limit", type=int, default=None)
    ap.add_argument("--rebalances", default=None)
    ap.add_argument("--out", default=os.path.join(HERE, "data", "exec_backtest_round130.json"))
    args = ap.parse_args()
    if args.selfcheck:
        print(json.dumps(selfcheck(), ensure_ascii=False, indent=2))
        return 0
    if not os.path.exists(KLONG):
        print(f"❌ 未找到 K 线 {KLONG}")
        return 1
    with open(KLONG, encoding="utf-8") as f:
        px = json.load(f)
    cal = calendar_of(px)
    meta = {}
    if args.rebalances:
        with open(args.rebalances, encoding="utf-8") as f:
            reb = json.load(f)
        src = args.rebalances
    elif args.from_long_panel:
        reb, meta = from_long_panel(px, cal, args.top_n, args.every, args.w_total)
        src = "data/panel_r124_mat.npz (proxy TopN)"
    elif args.from_portfolios:
        reb = from_portfolios(args.portfolio_limit)
        src = "sector/portfolio_*.json"
    else:
        ap.print_help()
        return 1
    out = run(reb, px, cal)
    out.update(meta)
    out["n_rebalance_records"] = len(reb)
    out["source"] = src
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print(json.dumps({k: v for k, v in out.items()
                      if k not in ("nav", "rejected_trades", "config")},
                     ensure_ascii=False, indent=2))
    print(f"\n写出 {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
