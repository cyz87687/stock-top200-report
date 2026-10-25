#!/usr/bin/env python3
"""验证多因子组合（fund + news + 剔除高 theme）在历史截面上是否真的优于 fund 单维

问题来源：老大第 3 项要求「用多因子思路选组合」。多因子听起来比单因子好，但
  「多因子一定更好」是迷信——本项目已有反例（score_theme 加进去是负 alpha）。
  所以在把多因子组合推给老大之前，必须先证明「加 news 是加分不是减分」。

方法（与池体检同口径，可复现）：
  - 快照：factor_history.json 的 78 个有效截面（2026-06-26 ~ 2026-10-23）
  - 价格：prices.dates / px / opens
  - 口径：T+1 开盘买入，扣 20bp 往返成本（与页面 tradable_ic 一致）
  - 基准：同日池内全部个股等权（池内基准，不是指数）
  - 对比四组：
      A  fund 单维 TOP10
      B  fund+news 按 0.76/0.20 加权 TOP10
      C  fund+news 加权 + 剔除 theme 前20% TOP10
      D  fund+news 加权 + 剔除 theme 前20% + 剔除 attention 前20%
    （D 是 pick_multifactor.py 的可回测近似；板块约束无法回测，因快照无 sector 字段）

输出：各组的 T+1/T+5/T+20 超额中位、跑赢率、配对 t。
"""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
COST_BPS = 20.0
TOP_N = 10

# 与 pick_multifactor.py 保持一致（权重取其实证推导结果）
W_FUND = 0.762
W_NEWS = 0.201


def _f(x, d=None):
    try:
        v = float(x)
        return v if v == v else d
    except (TypeError, ValueError):
        return d


def load():
    with open(os.path.join(HERE, "factor_history.json"), encoding="utf-8") as f:
        h = json.load(f)
    snaps = h.get("snapshots") or []
    pr = h.get("prices") or {}
    dates = pr.get("dates") or []
    px = pr.get("px") or {}
    opens = pr.get("opens") or {}
    return snaps, dates, px, opens


def _norm(c):
    s = "".join(ch for ch in str(c) if ch.isdigit())
    return s[-6:].zfill(6) if len(s) >= 6 else None


def build_px_index(dates, px):
    """{code: {date: close}}，与 factor_eval.px_index 同构"""
    out = {}
    for code, arr in px.items():
        c6 = _norm(code)
        if not c6:
            continue
        if not isinstance(arr, list):
            continue
        m = {}
        for i, v in enumerate(arr):
            if i < len(dates) and isinstance(v, (int, float)) and v > 0:
                m[dates[i]] = v
        out[c6] = m
    return out


def pct_rank(vals):
    idx = [(i, v) for i, v in enumerate(vals) if v is not None]
    idx.sort(key=lambda x: x[1])
    n = len(idx)
    out = {}
    for k, (i, _) in enumerate(idx):
        out[i] = 0.0 if n <= 1 else k / (n - 1.0)
    for i in range(len(vals)):
        out.setdefault(i, 0.0)
    return out


def ret_of(code6, d0, hop, dates, pxi, opens):
    """T+1 开盘买入 → T+1+hop 开盘卖出，扣往返成本（%）；不可得返回 None"""
    try:
        i0 = dates.index(d0)
    except ValueError:
        return None
    if i0 + 1 + hop >= len(dates):
        return None
    d_buy, d_sell = dates[i0 + 1], dates[i0 + 1 + hop]
    o = pxi.get(code6) or {}
    p0 = _f(o.get(d_buy))
    p1 = _f(o.get(d_sell))
    if not p0 or not p1 or p0 <= 0:
        # 退化用收盘
        p0 = _f((pxi.get(code6) or {}).get(d_buy))
        p1 = _f((pxi.get(code6) or {}).get(d_sell))
    if not p0 or not p1 or p0 <= 0:
        return None
    return (p1 / p0 - 1.0) * 100.0 - COST_BPS / 100.0


def main():
    global TOP_N
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--hop", type=int, default=5)
    ap.add_argument("--top", type=int, default=10)
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()
    hop = args.hop
    TOP_N = args.top

    snaps, dates, px, opens = load()
    pxi = build_px_index(dates, px)
    snaps = [s for s in snaps if (s.get("rows") or [])]

    rows_out = {k: [] for k in ("A", "B", "C", "D", "POOL")}

    for s in snaps:
        d0 = s["date"]
        rows = []
        for r in s["rows"]:
            c6 = _norm(r.get("code"))
            if not c6:
                continue
            rows.append({
                "code": c6,
                "fund": _f(r.get("score_fund")),
                "news": _f(r.get("score_news")),
                "theme": _f(r.get("score_theme")),
                "att": _f(r.get("score_attention")),
            })
        if len(rows) < 30:
            continue

        # 池内基准
        pool = [ret_of(r["code"], d0, hop, dates, pxi, opens) for r in rows]
        pool_ok = [v for v in pool if v is not None]
        if len(pool_ok) < 20:
            continue
        base = sum(pool_ok) / len(pool_ok)

        pr_f = pct_rank([r["fund"] for r in rows])
        pr_n = pct_rank([r["news"] for r in rows])
        pr_t = pct_rank([r["theme"] for r in rows])
        pr_a = pct_rank([r["att"] for r in rows])

        for i, r in enumerate(rows):
            r["mix"] = W_FUND * pr_f.get(i, 0) + W_NEWS * pr_n.get(i, 0)
            r["_i"] = i

        def topk(pool_rows):
            cand = sorted(pool_rows, key=lambda r: -r["mix"])[:TOP_N]
            rs = [ret_of(r["code"], d0, hop, dates, pxi, opens) for r in cand]
            rs = [v for v in rs if v is not None]
            return (sum(rs) / len(rs)) if rs else None

        has_f = [r for r in rows if r["fund"] is not None]
        has_fn = [r for r in rows if r["fund"] is not None and r["news"] is not None]
        # A: fund 单维
        a_c = sorted(has_f, key=lambda r: -(r["fund"] or 0))[:TOP_N]
        a_r = [ret_of(r["code"], d0, hop, dates, pxi, opens) for r in a_c]
        a_r = [v for v in a_r if v is not None]
        if a_r:
            rows_out["A"].append(sum(a_r) / len(a_r) - base)
        rows_out["POOL"].append(base)

        b = topk(has_fn)
        if b is not None:
            rows_out["B"].append(b - base)

        c_pool = [r for r in has_fn if pr_t.get(r["_i"], 0) < 0.80]
        c = topk(c_pool)
        if c is not None:
            rows_out["C"].append(c - base)

        d_pool = [r for r in c_pool if pr_a.get(r["_i"], 0) < 0.80]
        d = topk(d_pool)
        if d is not None:
            rows_out["D"].append(d - base)

    def summarize(name):
        v = rows_out[name]
        if len(v) < 5:
            print(f"  {name:6} 样本不足 n={len(v)}")
            return None
        n = len(v)
        mean = sum(v) / n
        med = sorted(v)[n // 2]
        win = sum(1 for x in v if x > 0) / n * 100
        sd = (sum((x - mean) ** 2 for x in v) / (n - 1)) ** 0.5 if n > 1 else 0
        t = mean / (sd / n ** 0.5) if sd > 0 else 0
        print(f"  {name:6} n={n:>3}  超额均值{mean:+6.2f}pct  中位{med:+6.2f}pct  "
              f"跑赢率{win:5.1f}%  t={t:+.2f}")
        return mean

    if not args.quiet:
        print(f"=== 多因子组合历史验证（T+{hop}，t+1开盘·扣{COST_BPS:.0f}bp，TOP{TOP_N}）===")
        print(f"  A = fund 单维 | B = fund+news 加权 | C = B+剔theme前20% | D = C+剔attention前20%")
    summarize("A")
    summarize("B")
    summarize("C")
    summarize("D")

    # 配对检验
    def paired(x, y, label):
        xs, ys = rows_out[x], rows_out[y]
        m = min(len(xs), len(ys))
        if m < 5:
            return
        diff = [ys[i] - xs[i] for i in range(m)]
        dm = sum(diff) / m
        sd = (sum((q - dm) ** 2 for q in diff) / (m - 1)) ** 0.5 if m > 1 else 0
        t = dm / (sd / m ** 0.5) if sd > 0 else 0
        print(f"  配对 {label}: 均值差 {dm:+.2f}pct, t={t:+.2f}, n={m}"
              + ("  → 显著" if abs(t) >= 2 else "  → 不显著"))

    if not args.quiet:
        print()
        paired("A", "B", "B−A（加 news）")
        paired("A", "D", "D−A（多因子全量）")
        paired("B", "C", "C−B（再剔 theme）")
        paired("C", "D", "D−C（再剔 attention）")


if __name__ == "__main__":
    sys.exit(main())
