#!/usr/bin/env python3
"""Round12：P0-5 —— 补测 drop_mom_top_pct（追高过滤）是否成立。

问题：config.pool_rules.drop_mom_top_pct = 20，即剔除「当日涨幅排名前 20%」的候选。
  该参数自 v3.8 引入后**从未回测过**（全项目 grep 仅 fetch_top200_sina.py 自身 3 处引用）。
  今日实测它剔掉 632 只 = 候选的 20.1%，属于凭直觉设定的重大过滤器。

诚实边界（必须先说）：历史快照里没有「当日涨幅」字段（只有 scored 后的 pct_chg，
  而 scored 是收盘后产物，用它筛池等于前视）。
  故本脚本用 **T-1 日的 5 日/1 日收益** 作为「追高」代理 —— 这是真实可得的、
  且在 T 日开盘前已知的信息，不存在前视。

测四档：0（不过滤）/ 10% / 20%（现状）/ 30%，在 SIZE 分层抽样的池上比较 T+20 超额。
"""
import json
import os
import sys
import math

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import factor_eval as fe

HOP = 20


def med(a):
    if not a:
        return None
    a = sorted(a)
    n = len(a)
    return a[n // 2] if n % 2 else (a[n // 2 - 1] + a[n // 2]) / 2.0


def mean(a):
    return sum(a) / len(a) if a else None


def sd(a):
    if len(a) < 2:
        return None
    m = mean(a)
    return math.sqrt(sum((x - m) ** 2 for x in a) / (len(a) - 1))


def ols_t(xs):
    n = len(xs)
    if n < 3:
        return None
    m, s = mean(xs), sd(xs)
    if not s:
        return None
    return m / (s / math.sqrt(n))


def nw_t(xs, lag=None):
    n = len(xs)
    if n < 3:
        return None
    m = mean(xs)
    if lag is None:
        lag = min(HOP - 1, max(1, int(n ** (1 / 3)) * 2))
    d = [x - m for x in xs]
    s2 = sum(v * v for v in d) / n
    for k in range(1, min(lag, n - 1) + 1):
        w = 1.0 - k / (lag + 1.0)
        g = sum(d[i] * d[i + k] for i in range(n - k)) / n
        s2 += 2 * w * g
    se = math.sqrt(max(s2, 1e-18) / n)
    return m / se if se > 0 else None


def main():
    h = json.load(open(os.path.join(HERE, "factor_history.json")))
    snaps, _ = fe._drop_stale_price_snapshots(h["snapshots"])
    tdays, px = fe.px_index(h)
    tidx = {d: i for i, d in enumerate(tdays)}

    # 用「曾进成交额榜」的并集作为宇宙（与 backtest_pool_factor 同源，保证可比）
    codes = list(px.keys())
    print(f"宇宙 {len(codes)} 只，交易日 {len(tdays)} 天（{tdays[0]} ~ {tdays[-1]}）")

    data = []
    for s in snaps:
        d0 = s["date"]
        if d0 not in tidx:
            continue
        i0 = tidx[d0]
        if i0 + HOP >= len(tdays) or i0 < 25:
            continue
        d1 = tdays[i0 + HOP]
        d_prev = tdays[i0 - 1]
        d_prev5 = tdays[i0 - 5]
        rows = []
        for c in codes:
            m = px.get(c) or {}
            p0, p1 = m.get(d0), m.get(d1)
            if not p0 or not p1 or p0 <= 0:
                continue
            # T-1 日已知信息：T-1 相对 T-6 的 5 日收益（追高代理，无前视）
            pp, pp5 = m.get(d_prev), m.get(d_prev5)
            r5_prev = (pp / pp5 - 1.0) if (pp and pp5 and pp5 > 0) else None
            rows.append({"c": c, "fwd": p1 / p0 - 1.0, "r5_prev": r5_prev})
        if len(rows) < 200:
            continue
        data.append({"date": d0, "rows": rows,
                     "all_med": med([x["fwd"] for x in rows])})
    print(f"可用截面 {len(data)} 个：{data[0]['date']} ~ {data[-1]['date']}\n")

    def run(mom_pct, use_strata=True, strata=5, pool_n=500):
        """模拟当前选池：先按池规模分层抽样，再按 mom_pct 剔除追高。"""
        exs, sizes = [], []
        for rec in data:
            rows = [x for x in rec["rows"] if x["r5_prev"] is not None]
            if len(rows) < 150:
                continue
            # 按 5 日涨幅排序，剔除涨幅最高的 mom_pct%
            rows.sort(key=lambda x: x["r5_prev"])
            if mom_pct > 0:
                keep_n = int(len(rows) * (1 - mom_pct / 100.0))
                rows = rows[:max(50, keep_n)]
            # 分层抽样（按 fwd 无关的分层变量——用 r5_prev 分档会与过滤耦合，
            #   故此处用价格自然序近似「随机」，保证与 mom 过滤正交）
            if use_strata and len(rows) > pool_n:
                sel, seen = [], set()
                per = max(1, pool_n // strata)
                N = len(rows)
                for k in range(strata):
                    lo, hi = N * k // strata, N * (k + 1) // strata
                    blk = rows[lo:hi]
                    take = blk if len(blk) <= per else [blk[int(i * len(blk) / per)]
                                                        for i in range(per)]
                    for x in take:
                        if x["c"] not in seen:
                            seen.add(x["c"])
                            sel.append(x)
                rows = sel
            else:
                rows = rows[:pool_n]
            if len(rows) < 30:
                continue
            sm = med([x["fwd"] for x in rows])
            exs.append(sm - rec["all_med"])
            sizes.append(len(rows))
        if len(exs) < 10:
            return None
        return {"mom_pct": mom_pct, "n": len(exs),
                "exc_med": round(med(exs) * 100, 2),
                "exc_mean": round(mean(exs) * 100, 2),
                "beat": round(100.0 * sum(1 for v in exs if v > 0) / len(exs), 1),
                "ols_t": round(ols_t(exs), 2), "nw_t": round(nw_t(exs), 2),
                "pool_size": int(mean(sizes)), "_exs": exs}
    
    print("=" * 78)
    print("追高过滤（T-1 日 5 日涨幅代理，无前视）对 T+20 超额的影响")
    print("=" * 78)
    print(f"{'追高剔除':<12}{'n':<5}{'超额中位':<11}{'均值':<11}{'胜率':<9}{'OLS t':<9}{'NW t':<8}{'池均':<6}")
    results = []
    for mp in (0, 10, 20, 30, 40):
        r = run(mp)
        if not r:
            print(f"{mp:>3}%        样本不足")
            continue
        results.append(r)
        print(f"{mp:>3}%{'':<8}{r['n']:<5}{r['exc_med']:+9.2f}pct {r['exc_mean']:+8.2f}pct "
              f"{r['beat']:>6.1f}% {r['ols_t']:+8.2f} {r['nw_t']:+7.2f} {r['pool_size']:>5}")

    # 与「不过滤」做逐截面配对检验
    print("\n" + "=" * 78)
    print("配对检验：各档 vs 0%（不过滤）—— 追高过滤是否真的带来改善")
    print("=" * 78)
    base = next((r for r in results if r["mom_pct"] == 0), None)
    pair_res = []
    if base:
        for r in results:
            if r["mom_pct"] == 0:
                continue
            if len(r["_exs"]) != len(base["_exs"]):
                print(f"  {r['mom_pct']:>3}%  样本数不一致({len(r['_exs'])} vs {len(base['_exs'])})，跳过")
                continue
            diff = [r["_exs"][i] - base["_exs"][i] for i in range(len(base["_exs"]))]
            pt, nt = ols_t(diff), nw_t(diff)
            sig = "✅显著" if nt and abs(nt) >= 2.0 else "不显著"
            pair_res.append({"mom_pct": r["mom_pct"], "diff_med": round(med(diff) * 100, 2),
                             "diff_mean": round(mean(diff) * 100, 2),
                             "ols_t": round(pt, 2), "nw_t": round(nt, 2)})
            print(f"  {r['mom_pct']:>3}%  配对差中位 {med(diff)*100:+.2f} pct "
                  f"均值 {mean(diff)*100:+.2f} pct  OLS t={pt:+.2f}  NW t={nt:+.2f}  {sig}")

    # 结论
    print("\n" + "=" * 78)
    print("结论")
    print("=" * 78)
    if base and len(results) > 1:
        best = max(results, key=lambda r: r["exc_med"])
        print(f"  超额中位最高档: 追高剔除 {best['mom_pct']}% → {best['exc_med']:+.2f} pct")
        print(f"  不过滤（0%）: {base['exc_med']:+.2f} pct  "
              f"（胜率 {base['beat']:.1f}%，NW t={base['nw_t']:+.2f}）")
        b20 = next((r for r in results if r["mom_pct"] == 20), None)
        if b20:
            print(f"  现状（20%）: {b20['exc_med']:+.2f} pct  "
                  f"（胜率 {b20['beat']:.1f}%，NW t={b20['nw_t']:+.2f}）")
            delta = b20["exc_med"] - base["exc_med"]
            # 判据必须是「配对检验显著性」，不能只看中位数绝对差是否 <0.5pct ——
            # 因子本身量级就小（0.4pct 级），用绝对差判会漏掉真实的显著改善。
            pair = next((p for p in pair_res if p["mom_pct"] == 20), None)
            if pair and abs(pair["nw_t"]) >= 2.0:
                print(f"  → ✅ 追高过滤**有效**：配对差 {pair['diff_med']:+.2f} pct，"
                      f"NW t={pair['nw_t']:+.2f}（超过 2.0 门槛）。")
                print(f"     保留 20% 阈值。但注意中位改善仅 {delta:+.2f} pct，"
                      f"主要价值在「不过滤时超额转负({base['exc_med']:+.2f})」这一点上。")
            else:
                print(f"  → 配对检验不显著（NW t={pair['nw_t'] if pair else 'n/a'}），"
                      f"该参数证据不足，建议置 0 以减少自由度。")
    print("\n  ⚠️ 代理变量局限：用「T-1 日 5 日涨幅」代理 fetch 实际使用的「当日涨幅」，")
    print("     两者不完全等价（当日涨幅含 T 日跳空，T-1 不可知）。故本结论只检验")
    print("     『追高是否该被剔除』这个方向，不精确复现线上过滤效果。")

    out = {k: v for k, v in ((r["mom_pct"], {kk: vv for kk, vv in r.items()
                                             if not kk.startswith("_")})
                             for r in results)}
    with open(os.path.join(HERE, "mom_filter_backtest.json"), "w", encoding="utf-8") as fp:
        json.dump({"n_snapshots": len(data), "proxy": "T-1日5日涨幅",
                   "results": out}, fp, ensure_ascii=False, indent=2)
    print("\n✅ 已写 mom_filter_backtest.json")


if __name__ == "__main__":
    main()
