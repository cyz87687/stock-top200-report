#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""portfolio_metrics.py —— 跟踪组合的日频风险指标（#306 / P1-5）

## 为什么需要

现有跟踪表只有「各期均值 + 胜率」，无法回答：
- 组合层面到底能不能用？（均值 +0%/胜率 50% 的组合，回撤可能是 −30%）
- 波动有多大？（夏普把收益和风险放在一起）
- 每日推荐的重合度多高？（重合度高 ⇒ 名义分散但实质集中，且换手成本被低估）

## 做法

对每个推荐日 d0，把当日名单视为**等权组合**，用 `data/klines_long.json` 的前复权
收盘价逐日估值，得到净值路径，再算：
- **区间收益**：d0 → 数据截止日
- **最大回撤 MDD**：沿路径的峰谷最大跌幅
- **年化波动 / 夏普**：日收益标准差 × √244；夏普 =（日均收益 × 244）/ 年化波动
  （无风险利率取 0，本项目口径统一；不引入外部利率数据避免口径漂移）
- **卡玛比率**：区间收益 / |MDD|
- **持仓重叠度**：相邻推荐日的 Jaccard = |交集| / |并集|

## ⚠️ 三个必须披露的口径限制

1. **生存者偏差**：只跟踪当日仍在名单内的标的，跌出榜单者不再追踪 → 回撤被系统性低估。
2. **天数太短**：12 个推荐日、路径最长约 13 个交易日 → 夏普年化的置信度极低，
   仅作横向比较，不可当"年化夏普 2.0"这类结论使用。
3. **等权假设**：实际建议有权重分配（不同评级仓位不同），此处统一等权以便可比。

## 输出
portfolio_metrics.json
"""
import json
import os
import math

HERE = os.path.dirname(os.path.abspath(__file__))
TRACK = os.path.join(HERE, "recommendation_track.json")
KLONG = os.path.join(HERE, "data", "klines_long.json")
OUT = os.path.join(HERE, "portfolio_metrics.json")

TRADING_DAYS = 244


def _c6(code):
    s = str(code or "").strip().lower()
    for p in ("sh", "sz", "bj"):
        if s.startswith(p):
            return s[len(p):]
    return s


def _close(px, c6, d):
    b = (px.get(c6) or {}).get(d)
    if isinstance(b, (list, tuple)) and len(b) >= 2:
        v = b[1]
        if isinstance(v, (int, float)) and v == v:
            return float(v)
    if isinstance(b, (int, float)) and b == b:
        return float(b)
    return None


def build_calendar(px, min_ratio=0.5):
    """有效交易日历 —— **委托给 kline_calendar（唯一口径来源，P1-1）**

    ⚠️ 2026-11-03 起不再自行实现过滤逻辑。
    原实现的 `c >= len(px) * min_ratio`（= 库内代码数 × 0.5 = 1069）与
    `kline_calendar.build_valid`（= max(1000, n×0.5)）在本库当前分布下**结果相同**
    （均 305 日），但两处独立实现必然漂移 —— 项目记忆「同一口径两处实现必然漂移
    → 单一来源」。故此处保留函数签名（兼容调用点），内部改为委托。

    min_ratio 参数仅为向后兼容保留，实际门槛由 kline_calendar 的
    MIN_ABS=1000 / MIN_RATIO=0.5 决定。
    """
    import kline_calendar as kc
    return kc.build_valid(px, min_ratio=min_ratio)

def main():
    track = json.load(open(TRACK, encoding="utf-8"))
    px = json.load(open(KLONG, encoding="utf-8"))
    cal = build_calendar(px)
    cidx = {d: i for i, d in enumerate(cal)}
    cutoff = cal[-1]

    recs = track.get("records") or []
    by_date = {}
    for r in recs:
        by_date.setdefault(r["date"], []).append(r)

    print(f"推荐日 {len(by_date)} 个 | 交易日历 {len(cal)} 天 ({cal[0]} ~ {cutoff})")

    rows = []
    for d0 in sorted(by_date):
        items = by_date[d0]
        if d0 not in cidx:
            print(f"  ⚠️ {d0} 不在交易日历内，跳过")
            continue
        i0 = cidx[d0]
        # 逐日等权净值：每只股票相对 d0 的涨跌幅取平均
        path = []
        for i in range(i0, len(cal)):
            d = cal[i]
            rets = []
            for r in items:
                c0 = _close(px, _c6(r["code"]), d0)
                c1 = _close(px, _c6(r["code"]), d)
                if c0 and c1 and c0 > 0:
                    rets.append((c1 - c0) / c0)
            if len(rets) >= max(3, len(items) * 0.3):
                path.append((d, sum(rets) / len(rets)))
        if len(path) < 2:
            print(f"  ⚠️ {d0} 路径不足 2 天，跳过")
            continue

        nav = [(d, 1.0 + v) for d, v in path]
        # 最大回撤
        peak = nav[0][1]
        mdd = 0.0
        mdd_at = None
        for d, v in nav:
            if v > peak:
                peak = v
            dd = (v - peak) / peak if peak > 0 else 0.0
            if dd < mdd:
                mdd = dd
                mdd_at = d
        # 日收益序列
        dr = []
        for i in range(1, len(nav)):
            p0, p1 = nav[i - 1][1], nav[i][1]
            if p0 > 0:
                dr.append((p1 - p0) / p0)
        sd = (sum((x - sum(dr) / len(dr)) ** 2 for x in dr) / (len(dr) - 1)) ** 0.5 if len(dr) > 1 else 0.0
        ann_vol = sd * math.sqrt(TRADING_DAYS)
        ann_ret = (nav[-1][1] - 1.0) / len(dr) * TRADING_DAYS if dr else 0.0
        total = nav[-1][1] - 1.0

        # ⚠️ 夏普必须要求足够样本：日收益 <8 个时 sd 由 1-2 个点算，年化后被除爆
        #    （实测 10-23 只有 1 个日收益 → 年化波动 1.0% → 夏普 −886.50，纯伪值）。
        MIN_DR = 8
        sharpe = (ann_ret / ann_vol) if (ann_vol > 1e-6 and len(dr) >= MIN_DR) else None

        # ⚠️ 卡玛 = 区间收益 / |MDD|。若路径单调下跌（起点即峰值），两者数值相等 → 恒为 −1.00，
        #    数学正确但零信息量，容易让读者误以为"每期都一样差"。标记出来。
        monotonic_down = (abs(total - mdd) < 1e-9 and total < 0)

        rows.append({
            "date": d0, "n": len(items),
            "days": len(nav) - 1,
            "total_ret_pct": total * 100,
            "mdd_pct": mdd * 100, "mdd_at": mdd_at,
            "ann_vol_pct": ann_vol * 100 if len(dr) >= MIN_DR else None,
            "sharpe_naive": sharpe,
            "calmar": (total / abs(mdd)) if abs(mdd) > 1e-9 else None,
            "monotonic_down": monotonic_down,
            "n_daily_rets": len(dr),
        })

    print(f"\n{'推荐日':<12}{'只数':>5}{'天数':>5}{'区间收益':>10}{'最大回撤':>10}{'年化波动':>10}{'夏普':>8}{'卡玛':>8}")
    for r in rows:
        vol_s = f"{r['ann_vol_pct']:>9.1f}%" if r["ann_vol_pct"] is not None else f"{'n/a':>10}"
        sh_s = f"{r['sharpe_naive']:>8.2f}" if r["sharpe_naive"] is not None else f"{'n/a':>8}"
        cm_s = f"{r['calmar']:>8.2f}" if r["calmar"] is not None else f"{'n/a':>8}"
        flag = " *" if r["monotonic_down"] else ""
        print(f"{r['date']:<12}{r['n']:>5}{r['days']:>5}"
              f"{r['total_ret_pct']:>9.2f}%{r['mdd_pct']:>9.2f}%{vol_s}{sh_s}{cm_s}{flag}")
    print("  * 单调下跌（起点即峰值）：卡玛 = 区间收益/|MDD| 必为 −1.00，数学正确但零信息量")
    print(f"  夏普要求日收益 ≥8 个才计算（否则 sd 由 1-2 点估出、年化后被除爆，产生如 −886 的伪值）")

    # ── 汇总 ──
    if rows:
        n = len(rows)
        avg_ret = sum(r["total_ret_pct"] for r in rows) / n
        avg_mdd = sum(r["mdd_pct"] for r in rows) / n
        worst = min(rows, key=lambda r: r["mdd_pct"])
        print(f"\n等权平均：区间收益 {avg_ret:+.2f}% | 平均最大回撤 {avg_mdd:.2f}%")
        print(f"最差回撤：{worst['date']}  {worst['mdd_pct']:.2f}%（谷底 {worst['mdd_at']}）")

    # ── 持仓重叠度（相邻推荐日 Jaccard）──
    print("\n持仓重叠度（相邻推荐日，Jaccard = |交|/|并|）:")
    ds = sorted(by_date)
    ov = []
    for a, b in zip(ds, ds[1:]):
        sa = {r["code"] for r in by_date[a]}
        sb = {r["code"] for r in by_date[b]}
        j = len(sa & sb) / len(sa | sb) if (sa | sb) else None
        ov.append({"from": a, "to": b, "jaccard": j,
                   "n_from": len(sa), "n_to": len(sb), "overlap": len(sa & sb)})
        print(f"  {a} → {b}: 交 {len(sa&sb):>3} / 并 {len(sa|sb):>3} = {j*100:>5.1f}%"
              if j is not None else f"  {a} → {b}: —")
    if ov:
        js = [x["jaccard"] for x in ov if x["jaccard"] is not None]
        if js:
            print(f"  平均重叠度 {sum(js)/len(js)*100:.1f}%（越高 → 换手越低但实质越集中）")

    # ── 三态与偏差披露 ──
    print("\n⚠️ 口径限制（必须与上面数字一起读）：")
    print("  ① 生存者偏差：只跟踪当日仍在名单内的标的，跌出榜单者停止追踪 → 回撤被系统性**低估**；")
    print(f"  ② 样本太短：{len(ds)} 个推荐日、路径最长 {max((r['days'] for r in rows), default=0)} 个交易日，")
    print("     夏普/卡玛的年化置信度极低，仅可横向比较，**不可当『年化夏普 X』结论**；")
    print("  ③ 等权假设：实际建议按评级分仓，此处统一等权以求口径一致；")
    print("  ④ 无风险利率取 0：不引入外部利率数据，避免跨源口径漂移。")

    json.dump({
        "per_date": rows,
        "overlap": ov,
        "summary": {
            "n_dates": len(rows),
            "avg_total_ret_pct": sum(r["total_ret_pct"] for r in rows) / len(rows) if rows else None,
            "avg_mdd_pct": sum(r["mdd_pct"] for r in rows) / len(rows) if rows else None,
            "worst_mdd": min(rows, key=lambda r: r["mdd_pct"]) if rows else None,
            "avg_jaccard": (sum(x["jaccard"] for x in ov if x["jaccard"] is not None)
                            / max(len([x for x in ov if x["jaccard"] is not None]), 1)) if ov else None,
        },
        "caveats": ["生存者偏差致回撤低估", "样本期短，夏普年化不可外推",
                    "等权假设", "无风险利率取0"],
        "data_cutoff": cutoff,
    }, open(OUT, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"\n📄 结果已落盘：{OUT}")


if __name__ == "__main__":
    main()
