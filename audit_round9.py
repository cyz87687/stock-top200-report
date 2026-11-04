#!/usr/bin/env python3
"""Round9 审计：拆穿「换母体 = +7.61pct / t=+6.45」的可信度。

三个致命疑点，逐一实证：
  Q1 重叠窗口：58 个日频截面、HOP=20 → 独立样本仅 58//20 = 2~3 个。
     原 t=+6.45 按 58 个独立观测算，必然高估。用非重叠子样本重算。
  Q2 基准错配：原回测基准 all_med = 「曾进过成交额榜」993 只并集的中位，
     这批股市值中位 ~187 亿（偏大盘）。而线上池是「全A 流通市值最小 18%」
     （中位 31 亿）。基准选谁，决定超额多少。
  Q3 beta 而非 alpha：小市值组在该宇宙内的超额，可能整段来自
     「宇宙越偏大盘 → 挑最小的越偏离 → 超额越大」的机械关系。
     用「组内对数市值差」做对照检验。
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


def nw_t(xs, lag=None):
    """Newey-West HAC t 值（对重叠窗口做部分修正）。"""
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


def ols_t(xs):
    n = len(xs)
    if n < 2:
        return None
    m = mean(xs)
    s = sd(xs)
    if not s:
        return None
    return m / (s / math.sqrt(n))


def main():
    h = json.load(open(os.path.join(HERE, "factor_history.json")))
    snaps, _ = fe._drop_stale_price_snapshots(h["snapshots"])
    tdays, px = fe.px_index(h)
    tidx = {d: i for i, d in enumerate(tdays)}

    # 与 backtest_pool_factor.py 同口径：股本由今日市值/价格反推
    snap = "/tmp/alla_snapshot.json"
    shares = {}
    if os.path.exists(snap):
        for r in json.load(open(snap)):
            try:
                p = float(r.get("trade") or 0)
                mv = float(r.get("nmc") or 0) * 1e4
            except Exception:
                continue
            if p > 0 and mv > 0:
                c = str(r.get("code") or "")
                if c:
                    shares[c] = mv / p
    print(f"股本可反推 {len(shares)} 只（快照 {os.path.exists(snap)}）")

    codes = [c for c in px if c in shares]
    print(f"价格宇宙 {len(px)} 只，其中可反推股本 {len(codes)} 只")

    data = []
    for s in snaps:
        d0 = s["date"]
        if d0 not in tidx:
            continue
        i0 = tidx[d0]
        if i0 + HOP >= len(tdays) or i0 < 60:
            continue
        i1 = i0 + HOP
        rows = []
        for c in codes:
            m = px.get(c) or {}
            p0, p1 = m.get(tdays[i0]), m.get(tdays[i1])
            if not p0 or not p1 or p0 <= 0:
                continue
            rows.append({"c": c, "fwd": p1 / p0 - 1.0, "price": p0,
                         "mv": shares[c] * p0})
        if len(rows) < 300:
            continue
        data.append({"date": d0, "rows": rows,
                     "all_med": med([x["fwd"] for x in rows]),
                     "mv_med_yi": med([x["mv"] for x in rows]) / 1e8})
    print(f"\n可用截面 {len(data)} 个：{data[0]['date']} ~ {data[-1]['date']}")
    print(f"宇宙流通市值中位 {med([r['mv_med_yi'] for r in data]):.1f} 亿 "
          f"（这是「偏大盘热门股并集」，不是全A）")

    # ---------- 基线：SIZE 最小 18% ----------
    exs, exs9, exs36, dates = [], [], [], []
    mv_gap = []
    for rec in data:
        v = sorted(rec["rows"], key=lambda x: x["mv"])
        for frac, box in ((0.18, exs), (0.09, exs9), (0.36, exs36)):
            sub = v[:max(30, int(len(v) * frac))]
            sm = med([x["fwd"] for x in sub])
            box.append(sm - rec["all_med"])
        s18 = v[:max(30, int(len(v) * 0.18))]
        mv_gap.append(math.log(max(1.0, rec["mv_med_yi"] * 1e8))
                      - math.log(max(1.0, med([x["mv"] for x in s18]))))
        dates.append(rec["date"])

    def report(label, xs):
        print(f"\n【{label}】n={len(xs)}")
        print(f"  中位超额 {med(xs)*100:+.2f} pct | 均值 {mean(xs)*100:+.2f} pct "
              f"| 胜率 {100.0*sum(1 for v in xs if v>0)/len(xs):.1f}%")
        print(f"  OLS t（假设 n 个独立观测）  = {ols_t(xs):+.2f}   ← 原引用的算法")
        print(f"  NW  t（lag={min(HOP-1, max(1,int(len(xs)**(1/3))*2))}, 部分修正）= {nw_t(xs):+.2f}")

    print("\n" + "=" * 72)
    print("Q1  重叠窗口问题：58 个日频截面是否被当成了 58 个独立观测")
    print("=" * 72)
    report("SIZE 最小18%（全部 58 截面，重叠）", exs)
    report("SIZE 最小9% （全部 58 截面，重叠）", exs9)
    report("SIZE 最小36%（全部 58 截面，重叠）", exs36)

    # 非重叠：每 HOP 个交易日取 1 个
    print("\n" + "-" * 72)
    print("非重叠子样本（间隔 ≥20 个交易日，独立观测）")
    print("-" * 72)
    sel, last_i = [], -10 ** 9
    for k, d in enumerate(dates):
        i = tidx[d]
        if i - last_i >= HOP:
            sel.append(k)
            last_i = i
    print(f"  非重叠样本数 = {len(sel)}  （对应日期 {[dates[k] for k in sel]}）")
    if len(sel) >= 3:
        report("SIZE 最小18%（非重叠）", [exs[k] for k in sel])
        report("SIZE 最小9% （非重叠）", [exs9[k] for k in sel])

    # ---------- Q2/Q3：beta 还是 alpha ----------
    print("\n" + "=" * 72)
    print("Q3  超额是 alpha 还是「宇宙偏大盘 × 挑最小」的机械 beta")
    print("=" * 72)
    # 组内市值偏离度 vs 超额 的相关
    pairs = list(zip(mv_gap, exs))
    n = len(pairs)
    if n >= 10:
        ax = [p[0] for p in pairs]
        ay = [p[1] for p in pairs]
        mx, my = mean(ax), mean(ay)
        cov = sum((ax[i] - mx) * (ay[i] - my) for i in range(n)) / (n - 1)
        sx, sy = sd(ax), sd(ay)
        r = cov / (sx * sy) if sx and sy else None
        print(f"  corr(组内市值偏离度, 组超额) = {r:+.3f}")
        print(f"  偏离度中位 = {med(ax):.2f}（log 单位，≈ {math.exp(med(ax)):.1f}x）")
        print(f"  → {'⚠️ 超额主要随「挑得比宇宙小多少」变化 = beta，非选股 alpha' if r and r > 0.5 else '相关性不构成 beta 解释'}")

    # 组内绝对收益 vs 宇宙绝对收益（剥离相对基准的伪影）
    print("\n" + "-" * 72)
    print("绝对收益口径（不用相对基准，直接看组内 T+20 中位收益）")
    print("-" * 72)
    for frac, label in ((0.18, "SIZE 最小18%"), (0.09, "SIZE 最小9%"), (1.0, "整个宇宙")):
        absv = []
        for rec in data:
            v = sorted(rec["rows"], key=lambda x: x["mv"])
            sub = v[:max(30, int(len(v) * frac))] if frac < 1 else v
            absv.append(med([x["fwd"] for x in sub]))
        print(f"  {label:14s} 中位 T+20 收益 {med(absv)*100:+7.2f}% | "
              f"均值 {mean(absv)*100:+7.2f}% | 正收益占比 "
              f"{100.0*sum(1 for v in absv if v>0)/len(absv):5.1f}%")

    print("\n" + "=" * 72)
    print("Q2  基准错配影响：换基准后超额变多少")
    print("=" * 72)
    print("  · 回测基准 = 上述 993 只偏大盘并集的中位（市值中位约 "
          f"{med([r['mv_med_yi'] for r in data]):.0f} 亿）")
    print("  · 线上池   = 全A 流通市值最小 18%（今日实测中位 31.1 亿）")
    print("  · 两者母体不同 → 回测相对超额不可直接外推为线上收益")
    # 用「宇宙内最小18%」的绝对收益 减去「宇宙整体」绝对收益，看是否只是基准造成的
    s18_abs, uni_abs = [], []
    for rec in data:
        v = sorted(rec["rows"], key=lambda x: x["mv"])
        s18_abs.append(med([x["fwd"] for x in v[:max(30, int(len(v) * 0.18))]]))
        uni_abs.append(med([x["fwd"] for x in v]))
    diff = [s18_abs[i] - uni_abs[i] for i in range(len(s18_abs))]
    print(f"\n  组内绝对 − 宇宙绝对：中位 {med(diff)*100:+.2f} pct  "
          f"（应与 exc 中位 {med(exs)*100:+.2f} 一致，验证口径）")

    out = {
        "n_snapshots": len(data),
        "universe_mv_med_yi": round(med([r["mv_med_yi"] for r in data]), 1),
        "overlap": {
            "size18": {"n": len(exs), "med_pct": round(med(exs) * 100, 2),
                       "mean_pct": round(mean(exs) * 100, 2),
                       "ols_t": round(ols_t(exs), 2), "nw_t": round(nw_t(exs), 2)},
            "size9": {"n": len(exs9), "med_pct": round(med(exs9) * 100, 2),
                      "ols_t": round(ols_t(exs9), 2), "nw_t": round(nw_t(exs9), 2)},
            "size36": {"n": len(exs36), "med_pct": round(med(exs36) * 100, 2),
                       "ols_t": round(ols_t(exs36), 2), "nw_t": round(nw_t(exs36), 2)},
        },
        "nonoverlap": {
            "n": len(sel), "dates": [dates[k] for k in sel],
            "size18": ({"n": len(sel),
                        "med_pct": round(med([exs[k] for k in sel]) * 100, 2),
                        "ols_t": round(ols_t([exs[k] for k in sel]), 2) if len(sel) >= 3 else None,
                        "nw_t": round(nw_t([exs[k] for k in sel]), 2) if len(sel) >= 3 else None}
                       if len(sel) >= 3 else None),
        },
        "mv_gap_corr": round(pairs and r, 3) if n >= 10 and r is not None else None,
    }
    with open(os.path.join(HERE, "audit_round9.json"), "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print(f"\n✅ 已写 audit_round9.json")


if __name__ == "__main__":
    main()
