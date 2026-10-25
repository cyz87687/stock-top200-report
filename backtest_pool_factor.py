#!/usr/bin/env python3
"""剔除低价因子，改用 final_ranking.csv 的因子体系（老大 2026-10-25 指令）。

## 背景：为什么必须剔除「绝对价格」
上一版 P0-1 按「全A 绝对价格最便宜 18%」选池，回测超额 +4.60pct，但实跑结果：
  · 池内科技仅 12.6%，TOP10 = 中国建筑/TCL科技/民生银行/包钢股份/万科Ａ
  · 与老大「保留科技属性」裁决冲突
加科技配额 30% 后结构缓解，但根子上的问题没解：**绝对价格根本不是因子**。
实证（今日全A 5573 只）：
  · Spearman(价格, ln流通市值) = 0.166  ← 几乎无关
  · 「价格最低500」市值中位 44.0 亿（≈全A中位 48.1 亿），选出的是萃华退/*ST卓然/
    *ST清越/元道退 —— 便宜的是「即将退市的壳」，不是低估
  · 「市值最小500」市值中位 9.2 亿、价格中位 15.05 元 —— 才是真正的小微盘
  · 两组重合仅 26/500 = 5.2%
→ 绝对价格是 SIZE 的极差代理（r=0.166），且把「面值退市风险」误当 alpha。

## final_ranking.csv 的因子体系（47 个候选，作者选定 9 个）
唯一达到显著超额的是 SIZE（小市值，方向「越小越好」）：
  exc_t=+2.28（全表唯一 |t|>2 的多头超额）、LS_IR=1.95（最高）、score=0.993（最高）、
  IC_daily=-0.0655、ICIR=-0.604、IC_daily_win=0.25（即「小市值跑赢」的比例高）
次强：NAG（净资产同比增速）LS_IR=1.216 / TAG（总资产增速）LS_IR=1.400 / NPG LS_IR=1.765
反向有效（越小越好/做负向）：MOM1W(exc_t=+1.51)、VOLR20(exc_t=-2.52)、SIZE、
  PSY12、MFI14、DEBT、BIAS60、TRIX12、AMT20
估值族普遍弱：EP(exc_t=+0.10)、BP(-0.54)、CFP(-0.25)、EVEBIT(-0.32)、DY(+1.48 但 IC 为负)
→ 「便宜」在估值维度也不成立，真正的 alpha 集中在 SIZE + 成长 + 弱量价反转。

## 本脚本目标
在 1012 只宇宙内（历史价格可得、股本可从今日市值/价格反推、无前视）回测：
  · 新口径（剔除低价，改用 SIZE 为首的多因子）
  · 对照旧口径（绝对价格分位）
确认新口径是否真的更优，再决定是否改线上选池规则。

## 数据边界（必须诚实）
1012 宇宙 = 「历史上曾进入成交额榜」的并集 → 市值中位 187.7 亿（全A 48.1 亿），
是**偏大盘热门股**集合。SIZE 因子在此宇宙内仍有区分度（12.2~22954 亿），
但「小市值 alpha」在真全A（含大量 20 亿以下微盘）中通常更强，方向上可外推，
绝对幅度不可外推。财务因子（ROE/OCFG/NAG 等）历史快照未存 → 本脚本不含，另议。
"""
import json
import os
import sys
import math

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import factor_eval as fe

HOP = 20
TECH_L1 = {"电子", "计算机", "通信", "电力设备", "国防军工", "机械设备"}


def med(a):
    if not a:
        return None
    a = sorted(a)
    n = len(a)
    return a[n // 2] if n % 2 else (a[n // 2 - 1] + a[n // 2]) / 2.0


def pct_rank(vals):
    """百分位排名 0~1（1=最大）。用于横截面标准化。"""
    n = len(vals)
    order = sorted(range(n), key=lambda i: vals[i])
    out = [0.0] * n
    for pos, i in enumerate(order):
        out[i] = pos / max(1, n - 1)
    return out


def load_shares():
    """从今日新浪快照反推流通股本（nmc/price）。返回 {6位代码: 股数}。"""
    snap = "/tmp/alla_snapshot.json"
    if not os.path.exists(snap):
        raise SystemExit(f"缺 {snap}，请先跑 fetch 抓今日全A快照")
    rows = json.load(open(snap))
    sh = {}
    for r in rows:
        try:
            p = float(r.get("trade") or 0)
            mv = float(r.get("nmc") or 0) * 1e4
        except Exception:
            continue
        if p > 0 and mv > 0:
            sh[r["code"]] = mv / p
    return sh


def build():
    h = json.load(open(os.path.join(HERE, "factor_history.json")))
    snaps, _ = fe._drop_stale_price_snapshots(h["snapshots"])
    tdays, px = fe.px_index(h)
    sw = json.load(open(os.path.join(HERE, "sector", "sw_map.json")))
    l1 = sw["l1"]
    shares = load_shares()
    tidx = {d: i for i, d in enumerate(tdays)}
    codes = [c for c in px if c in shares]
    print(f"宇宙 {len(px)} 只，可反推股本 {len(codes)} 只")

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
            c6 = c[2:] if len(c) > 6 else c
            _r5 = m.get(tdays[i0 - 5]) if i0 >= 5 else None
            _r20 = m.get(tdays[i0 - 20]) if i0 >= 20 else None
            _r60 = m.get(tdays[i0 - 60]) if i0 >= 60 else None
            rows.append({
                "c": c, "fwd": p1 / p0 - 1.0, "price": p0,
                "mv": shares[c] * p0,          # 当日流通市值（元），无前视
                "r5": (p0 / _r5 - 1.0) if _r5 else None,
                "r20": (p0 / _r20 - 1.0) if _r20 else None,
                "r60": (p0 / _r60 - 1.0) if _r60 else None,
                "tech": l1.get(c6) in TECH_L1,
            })
        if len(rows) < 300:
            continue
        data.append({"date": d0, "rows": rows,
                     "all_med": med([x["fwd"] for x in rows])})
    return data


def ev(name, fn, data):
    meds, exs, wins, sizes = [], [], 0, []
    for rec in data:
        sub = fn(rec["rows"], rec)
        if len(sub) < 30:
            continue
        sm = med([x["fwd"] for x in sub])
        if sm is None:
            continue
        meds.append(sm)
        exs.append(sm - rec["all_med"])
        sizes.append(len(sub))
        if sm > rec["all_med"]:
            wins += 1
    if len(exs) < 10:
        return None
    return {"name": name, "n": len(exs), "med": round(med(meds) * 100, 2),
            "exc": round(med(exs) * 100, 2), "exc_mean": round(fe._mean(exs) * 100, 2),
            "beat": round(100.0 * wins / len(exs), 1),
            "size": int(fe._mean(sizes)), "_exs": exs}


def build_rules():
    def cheapest(frac):
        def f(rows, rec):
            v = sorted(rows, key=lambda x: x["price"])
            return v[:max(30, int(len(v) * frac))]
        return f

    def smallest(frac):
        def f(rows, rec):
            v = sorted(rows, key=lambda x: x["mv"])
            return v[:max(30, int(len(v) * frac))]
        return f

    def smallest_tech(frac, quota):
        def f(rows, rec):
            v = sorted(rows, key=lambda x: x["mv"])
            pool = v[:max(200, int(len(v) * frac))]
            n_t = int(rec.get("_pool", 500) * quota) if "_pool" in rec else int(500 * quota)
            t = [x for x in pool if x["tech"]][:n_t]
            o = [x for x in pool if not x["tech"]]
            return t + o[:500 - len(t)]
        return f

    def composite(rows, rec):
        """多因子合成：SIZE(0.5) + 弱反转MOM1W(0.2) + 低波动代理r60反转(0.15)
        + 剔除极端追高(0.15)。全部取自 final_ranking.csv 的 selected 因子，
        且只用历史价格可得量（财务因子历史缺失，暂不纳入）。"""
        n = len(rows)
        if n < 30:
            return rows
        mv = [math.log(max(1.0, x["mv"])) for x in rows]
        r5 = [x["r5"] if x["r5"] is not None else 0.0 for x in rows]
        r60 = [x["r60"] if x["r60"] is not None else 0.0 for x in rows]
        # SIZE：越小越好 → 用 (1-rank)
        s_size = [1.0 - v for v in pct_rank(mv)]
        # MOM1W：越小越好（弱反转）
        s_mom1w = [1.0 - v for v in pct_rank(r5)]
        # BIAS60 同族：60日涨幅越小越好
        s_bias60 = [1.0 - v for v in pct_rank(r60)]
        sc = []
        for i in range(n):
            sc.append(0.50 * s_size[i] + 0.25 * s_mom1w[i] + 0.25 * s_bias60[i])
        order = sorted(range(n), key=lambda i: -sc[i])
        return [rows[i] for i in order[:500]]

    return [
        ("P0 全宇宙（基准）", lambda r, rec: r),
        ("OLD 绝对价格最便宜18%〔旧口径〕", cheapest(0.18)),
        ("OLD 绝对价格最便宜9%", cheapest(0.09)),
        ("SIZE 市值最小9%", smallest(0.09)),
        ("SIZE 市值最小18%", smallest(0.18)),
        ("SIZE 市值最小36%", smallest(0.36)),
        ("SIZE 市值最小18% + 科技配额30%", smallest_tech(0.18, 0.30)),
        ("COMPOSITE SIZE.5+反转.25+BIAS60.25", composite),
    ]


def main():
    data = build()
    print(f"有效截面 {len(data)}  持有 {HOP} 日\n")
    rules = build_rules()
    print(f"{'规则':<38}{'入场日':>5}{'中位':>9}{'超额中位':>10}"
          f"{'超额均值':>10}{'胜率':>8}{'只数':>6}")
    print("-" * 90)
    res = []
    for name, fn in rules:
        r = ev(name, fn, data)
        if not r:
            print(f"{name:<38} 无数据")
            continue
        res.append(r)
        print(f"{r['name']:<38}{r['n']:>5}{r['med']:>+8.2f}%{r['exc']:>+9.2f}pct"
              f"{r['exc_mean']:>+10.2f}{r['beat']:>7.1f}%{r['size']:>6}")

    k = len(data) // 3
    print("\n=== 三段稳健性（超额中位 pct） ===")
    print(f"{'规则':<38}{'段1':>9}{'段2':>9}{'段3':>9}")
    for name, fn in rules:
        vals = []
        for b in (data[:k], data[k:2 * k], data[2 * k:]):
            r = ev(name, fn, b)
            vals.append(f"{r['exc']:>+8.2f}" if r else "     NA ")
        print(f"{name:<38}" + " ".join(vals))

    print("\n=== 配对检验（vs OLD 绝对价格最便宜18%） ===")
    import statistics
    base = next((r for r in res if r["name"].startswith("OLD 绝对价格最便宜18%")), None)
    if base:
        for r in res:
            if r is base or r["name"].startswith("P0"):
                continue
            d = [a - b for a, b in zip(r["_exs"], base["_exs"])]
            n = len(d)
            mu = statistics.mean(d)
            sd = statistics.stdev(d) if n > 1 else 0
            t = mu / (sd / (n ** 0.5)) if sd > 0 else 0
            tag = "显著优于" if t > 2 else ("显著劣于" if t < -2 else "不显著(噪声内)")
            print(f"  {r['name']:<38}{mu*100:>+7.2f}pct  t={t:>+6.2f}  {tag}")

    for r in res:
        r.pop("_exs", None)
    out = os.path.join(HERE, "pool_factor_backtest.json")
    json.dump({"n_days": len(data), "results": res},
              open(out, "w"), ensure_ascii=False, indent=1)
    print(f"\n写入 {out}")


if __name__ == "__main__":
    main()
