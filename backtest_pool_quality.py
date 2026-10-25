#!/usr/bin/env python3
"""P0-1 终审：质量门槛 × 价格倾斜的二维权衡。

## 为什么必须做这一步
上一轮发现「池子越小、价格越便宜，超额越高」（价格前60 → +3.64pct，前100 → +2.71pct）。
但直接落地等于把系统改成**纯低价股策略**，与使用者的实际投研方向
（PCB/光模块/存储/半导体等科技成长）完全脱节——那就不再是"改进"而是"换了个产品"。

## 核心问题
能否在保持「成长/质量属性」的前提下享受反转红利？

## 方案
在滚动20日母体上，先按 score_fund 施加质量门槛（fund 是唯一全周期稳健维度，
h5 NW t=3.50），再做价格倾斜。网格：
  quality_q ∈ {不过滤, fund≥p30, fund≥p50, fund≥p70}
  size      ∈ {60, 100, 150, 300, 全部}
输出超额中位 / 胜率 / 三段稳定性，找帕累托前沿。

## 判据
若「fund≥p50 + 价格前150」仍有显著正超额，说明两者可兼得，优先推荐该口径
（既保留成长属性，又获得反转 alpha）。
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
    if not a:
        return None
    a = sorted(a)
    n = len(a)
    return a[n // 2] if n % 2 else (a[n // 2 - 1] + a[n // 2]) / 2.0


def ret(px, code, tdays, i0, i1):
    m = px.get(code) or {}
    p0, p1 = m.get(tdays[i0]), m.get(tdays[i1])
    if not p0 or not p1 or p0 <= 0:
        return None
    return p1 / p0 - 1.0


def build():
    snaps, tdays, px = load()
    tidx = {d: i for i, d in enumerate(tdays)}
    ordered = sorted([s for s in snaps if s["date"] in tidx],
                     key=lambda s: tidx[s["date"]])
    data = []
    for pos, s in enumerate(ordered):
        d0 = s["date"]
        i0 = tidx[d0]
        if i0 + HOP >= len(tdays) or i0 < 20:
            continue
        i1 = i0 + HOP
        cand = {}
        for r in s["rows"]:
            if r.get("price"):
                cand[r["code"]] = {"price": r["price"],
                                   "fund": r.get("score_fund"),
                                   "theme": r.get("score_theme"),
                                   "amt": r.get("amount") or r.get("turnover") or 0}
        for j in range(max(0, pos - 20), pos):
            for r in ordered[j]["rows"]:
                c = r["code"]
                if c in cand:
                    continue
                pr = (px.get(c) or {}).get(d0)
                if pr:
                    # 召回股取最近一次已知的 fund 分（历史已知，无前视）
                    cand[c] = {"price": pr, "fund": r.get("score_fund"),
                               "theme": r.get("score_theme"), "amt": 0}
        rows = []
        for c, v in cand.items():
            fwd = ret(px, c, tdays, i0, i1)
            if fwd is None:
                continue
            rows.append({"c": c, "fwd": fwd, **v})
        base_fwd = [ret(px, r["code"], tdays, i0, i1) for r in s["rows"]]
        base_fwd = [x for x in base_fwd if x is not None]
        if len(rows) < 100 or len(base_fwd) < 50:
            continue
        data.append({"date": d0, "rows": rows, "base_med": med(base_fwd)})
    return data


def apply(rows, q_thr, n):
    """q_thr: None 或 fund 分位阈值(0~1)；n: 最终只数上限。"""
    v = rows
    if q_thr is not None:
        fv = [x["fund"] for x in v if x.get("fund") is not None]
        if len(fv) >= 30:
            thr = sorted(fv)[min(len(fv) - 1, int(len(fv) * q_thr))]
            v = [x for x in v if x.get("fund") is not None and x["fund"] >= thr]
    v = sorted(v, key=lambda x: x["price"])
    return v[:min(n, len(v))]


def ev(fn, data):
    meds, exs, wins, sizes, worst = [], [], 0, [], []
    for rec in data:
        sub = fn(rec["rows"])
        if len(sub) < 20:
            continue
        sm = med([x["fwd"] for x in sub])
        if sm is None:
            continue
        meds.append(sm)
        exs.append(sm - rec["base_med"])
        sizes.append(len(sub))
        worst.append(min(x["fwd"] for x in sub))
        if sm > rec["base_med"]:
            wins += 1
    if len(exs) < 10:
        return None
    return {"med": round(med(meds) * 100, 2), "exc": round(med(exs) * 100, 2),
            "exc_mean": round(fe._mean(exs) * 100, 2),
            "beat": round(100.0 * wins / len(exs), 1),
            "size": int(fe._mean(sizes)), "worst": round(med(worst) * 100, 2),
            "n": len(exs)}


def main():
    data = build()
    print(f"有效截面 {len(data)}（{data[0]['date']} ~ {data[-1]['date']}）")
    print("母体 = 今日成交额榜 ∪ 滚动20日池；基线 = 当日成交额榜全取\n")

    qs = [None, 0.30, 0.50, 0.70]
    ns = [60, 100, 150, 300, 10 ** 6]
    qlabel = {None: "不过滤", 0.30: "fund≥p30", 0.50: "fund≥p50", 0.70: "fund≥p70"}

    print(f"{'质量门槛':<12} {'最终只数':<10} {'入场日':>5} {'超额中位':>9} "
          f"{'超额均值':>9} {'胜率':>7} {'实际只数':>8} {'最差单票':>9}")
    print("-" * 88)
    grid = {}
    for q in qs:
        for n in ns:
            r = ev(lambda rows, _q=q, _n=n: apply(rows, _q, _n), data)
            if not r:
                continue
            lbl = f"前{n}" if n < 10 ** 6 else "全部"
            grid[(q, n)] = r
            mark = ""
            if r["exc"] > 0 and r["beat"] >= 70:
                mark = " ★"
            print(f"{qlabel[q]:<12} {lbl:<10} {r['n']:>5} {r['exc']:>+8.2f}pct "
                  f"{r['exc_mean']:>+8.2f} {r['beat']:>6.1f}% {r['size']:>8} "
                  f"{r['worst']:>+8.2f}%{mark}")

    print("\n=== 三段稳健性（只列 fund≥p50 与 不过滤 两组对照）===")
    k = len(data) // 3
    blocks = (data[:k], data[k:2 * k], data[2 * k:])
    print(f"{'质量门槛':<12} {'最终只数':<10} {'段1':>9} {'段2':>9} {'段3':>9}")
    for q in (None, 0.50):
        for n in ns:
            vals = []
            for b in blocks:
                r = ev(lambda rows, _q=q, _n=n: apply(rows, _q, _n), b)
                vals.append(f"{r['exc']:>+8.2f}" if r else "     NA ")
            lbl = f"前{n}" if n < 10 ** 6 else "全部"
            print(f"{qlabel[q]:<12} {lbl:<10} " + " ".join(vals))

    out = os.path.join(HERE, "pool_quality_backtest.json")
    json.dump({"n_days": len(data),
               "grid": {f"{qlabel[q]}|{n}": v for (q, n), v in grid.items()}},
              open(out, "w"), ensure_ascii=False, indent=1)
    print(f"\n写入 {out}")


if __name__ == "__main__":
    main()
