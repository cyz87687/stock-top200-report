#!/usr/bin/env python3
"""P0-1 选股池入选规则改造：池内过滤规则的可交易口径回测选型。

## 为什么这样测（方法论边界，必须先讲清）

真正的「改造入选规则」应当是全A 范围内用「成交额 + 质量/反转」重新选 500 只。
但历史限制：`factor_history.json` 只留档了**当时已入池的 490 只**，没有留全A 每日快照。
因此无法回测「换一套全A选池逻辑会选中谁」。

可验证的替代实验：**在现有池内做子集筛选**，检验「叠加某条过滤条件后的子集，
是否比不筛选的全池表现更好」。这直接回答「哪条过滤规则有真实价值」，
是落地入选规则的必要前置证据（否则又是拍阈值）。

## 评估口径
- 与 factor_eval._eval_pool_excess 同源：截面 × 持有 20 日，中位涨跌
- 子集 vs 全池（1012 只历史并集）超额
- 子集绝对中位收益（投资者真实可得）
- 跑赢全池的入场日占比（稳定性，单日幸运不算）

## 规则候选
A0 基线：不筛选（现规则）
A1 剔除 20 日涨幅前 20%（追高过滤 = 反转）
A2 剔除 60 日涨幅前 20%
A3 剔除 score_theme 前 20%（题材热度过滤）
A4 剔除 score_tech 前 20%（趋势/动量过滤）
A5 保留 score_fund 前 50%（质量过滤）
A6 剔除 score_attention 前 20%
A7 A1 ∩ A3（追高 + 热度双过滤）
A8 保留 score_fund 前 70%（温和质量过滤）
A9 剔除 20 日涨幅前 20% ∩ score_fund 后 30%（追高 + 基本面差）
"""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import factor_eval as fe

HOP = 20


def load():
    h = json.load(open(os.path.join(HERE, "factor_history.json")))
    snaps, _ = fe._drop_stale_price_snapshots(h["snapshots"])
    tdays, px = fe.px_index(h)
    return snaps, tdays, px


def med(a):
    n = len(a)
    if n == 0:
        return None
    a = sorted(a)
    return a[n // 2] if n % 2 else (a[n // 2 - 1] + a[n // 2]) / 2.0


def pctile_threshold(vals, q):
    """返回 q 分位阈值（q=0.8 → 80% 分位）。"""
    if not vals:
        return None
    a = sorted(vals)
    i = min(len(a) - 1, int(len(a) * q))
    return a[i]


def past_ret(px, code, tdays, i0, lookback):
    """t-i0 往前 lookback 日收益率；数据不足返回 None。"""
    j = i0 - lookback
    if j < 0:
        return None
    m = px.get(code) or {}
    p0, p1 = m.get(tdays[j]), m.get(tdays[i0])
    if not p0 or not p1 or p0 <= 0:
        return None
    return p1 / p0 - 1.0


def main():
    snaps, tdays, px = load()
    tidx = {d: i for i, d in enumerate(tdays)}

    # 预计算每个截面的：全池中位收益 + 每只股票的未来收益 + 历史特征
    records = []   # 每个截面一条
    for s in snaps:
        d0 = s["date"]
        if d0 not in tidx:
            continue
        i0 = tidx[d0]
        if i0 + HOP >= len(tdays):
            continue
        d1 = tdays[i0 + HOP]

        # 全池（1012 只并集）中位收益
        all_r = []
        for m in px.values():
            p0, p1 = m.get(d0), m.get(d1)
            if p0 and p1 and p0 > 0:
                all_r.append(p1 / p0 - 1.0)
        if len(all_r) < 50:
            continue
        all_med = med(all_r)

        rows = []
        for r in s["rows"]:
            code = r["code"]
            m = px.get(code) or {}
            p0, p1 = m.get(d0), m.get(d1)
            if not p0 or not p1 or p0 <= 0:
                continue
            rows.append({
                "code": code,
                "fwd": p1 / p0 - 1.0,
                "r20": past_ret(px, code, tdays, i0, 20),
                "r60": past_ret(px, code, tdays, i0, 60),
                "theme": r.get("score_theme"),
                "tech": r.get("score_tech"),
                "fund": r.get("score_fund"),
                "attn": r.get("score_attention"),
            })
        if len(rows) < 100:
            continue
        records.append({"date": d0, "all_med": all_med, "rows": rows})

    print(f"可用截面: {len(records)}  持有期: {HOP} 日\n")

    def evaluate(name, keep_fn):
        """keep_fn(rows) -> 子集 rows"""
        subs_med, exs, wins, sizes = [], [], 0, []
        for rec in records:
            sub = keep_fn(rec["rows"])
            if len(sub) < 30:
                continue
            sm = med([x["fwd"] for x in sub])
            if sm is None:
                continue
            subs_med.append(sm)
            exs.append(sm - rec["all_med"])
            sizes.append(len(sub))
            if sm > rec["all_med"]:
                wins += 1
        if not exs:
            return None
        return {
            "name": name,
            "n_days": len(exs),
            "sub_med_pct": round(med(subs_med) * 100, 2),
            "excess_med_pct": round(med(exs) * 100, 2),
            "excess_mean_pct": round(fe._mean(exs) * 100, 2),
            "beat_ratio": round(100.0 * wins / len(exs), 1),
            "avg_size": int(fe._mean(sizes)),
        }

    defs = {}

    defs["A0 基线·不筛选"] = lambda rows: rows

    def _drop_top(field, q):
        def f(rows):
            vals = [x[field] for x in rows if x.get(field) is not None]
            if len(vals) < 20:
                return rows
            thr = pctile_threshold(vals, q)
            return [x for x in rows if x.get(field) is None or x[field] < thr]
        return f

    defs["A1 剔除20日涨幅前20%"] = _drop_top("r20", 0.80)
    defs["A2 剔除60日涨幅前20%"] = _drop_top("r60", 0.80)
    defs["A3 剔除题材热度前20%"] = _drop_top("theme", 0.80)
    defs["A4 剔除趋势分前20%"] = _drop_top("tech", 0.80)
    defs["A6 剔除关注度前20%"] = _drop_top("attn", 0.80)

    def _keep_top(field, q):
        def f(rows):
            vals = [x[field] for x in rows if x.get(field) is not None]
            if len(vals) < 20:
                return rows
            thr = pctile_threshold(vals, q)
            return [x for x in rows if x.get(field) is not None and x[field] >= thr]
        return f

    defs["A5 保留基本面前50%"] = _keep_top("fund", 0.50)
    defs["A8 保留基本面前70%"] = _keep_top("fund", 0.70)

    def a7(rows):
        r1 = _drop_top("r20", 0.80)(rows)
        return _drop_top("theme", 0.80)(r1)
    defs["A7 追高∩热度双过滤"] = a7

    def a9(rows):
        v20 = [x["r20"] for x in rows if x.get("r20") is not None]
        vfd = [x["fund"] for x in rows if x.get("fund") is not None]
        if len(v20) < 20 or len(vfd) < 20:
            return rows
        t20 = pctile_threshold(v20, 0.80)
        tfd = pctile_threshold(vfd, 0.30)
        out = []
        for x in rows:
            hi_mom = x.get("r20") is not None and x["r20"] >= t20
            low_fund = x.get("fund") is not None and x["fund"] <= tfd
            if hi_mom and low_fund:
                continue
            out.append(x)
        return out
    defs["A9 追高∩基本面差"] = a9

    print(f"{'规则':<26} {'入场日':>5} {'子集中位':>9} {'超额中位':>9} "
          f"{'超额均值':>9} {'跑赢占比':>8} {'平均只数':>7}")
    print("-" * 84)
    results = []
    for name, fn in defs.items():
        r = evaluate(name, fn)
        if r is None:
            print(f"{name:<26} 无有效数据")
            continue
        results.append(r)
        print(f"{r['name']:<26} {r['n_days']:>5} {r['sub_med_pct']:>8}% "
              f"{r['excess_med_pct']:>8}pct {r['excess_mean_pct']:>8}pct "
              f"{r['beat_ratio']:>7}% {r['avg_size']:>7}")

    print()
    valid = [x for x in results if x["name"] != "A0 基线·不筛选"]
    if valid:
        valid.sort(key=lambda x: -x["excess_med_pct"])
        print("按「超额中位」排序（越大越好，0 = 与全池持平）：")
        for r in valid[:5]:
            print(f"  {r['name']:<26} 超额 {r['excess_med_pct']:+.2f}pct  "
                  f"跑赢 {r['beat_ratio']}%  剩 {r['avg_size']} 只")

    out = os.path.join(HERE, "pool_filter_backtest.json")
    json.dump({"hop": HOP, "n_days": len(records), "results": results},
              open(out, "w"), ensure_ascii=False, indent=1)
    print(f"\n写入 {out}")


if __name__ == "__main__":
    main()
