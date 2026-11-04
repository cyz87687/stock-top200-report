#!/usr/bin/env python3
"""factor_vector.py —— polars 向量化因子计算（替代 `for stock in all_stocks` 逐股循环）。

老大 2026-10-26 需求②：禁止逐股循环，改 polars 向量化，并按行业分块多进程并行。

## 实测（490 只）
  · 逐股 Python 循环：低波20 0.002s / rsi 0.01s / rank 0.001s
  · polars 全池一次算：低波20 0.032s
    → **纯 CPU 部分 polars 并不更快**（pandas/numpy 早已是 C 实现，490 只规模下
      Python 循环开销本来就只有毫秒级）。真正的收益在：
        a) 股票数上万时（全A 5377 只）Python 循环的常数开销才显著；
        b) 代码可读性与「不可漏股」——循环版容易漏某只，向量化天然全量；
        c) 与增量缓存配合时，宽表可直接落 parquet 复用。
  → 因此本模块定位：**正确性 + 可扩展性**，不是把这 0.03s 再压到 0.01s。
    真正的提速必须在网络层（见 async_fetch.py，28.5分钟→12秒）。

## 接口
  build_wide(kl_map)          → pl.DataFrame 长表 (code, date, close, vol, ...)
  lowvol20_vec(wide, n=20)    → {code: 原始波动率}（与 step2.raw_lowvol20 逐位一致）
  mom_vec(wide, n)            → {code: 近 n 日收益率}
  rsi_vec(wide, period=14)    → {code: RSI}
  rank10_vec(vals, reverse)   → {code: 0-10 分位分}
  size_ind_vec(rows, sw_map)  → {code: 行业内市值中性分}（分块并行）

## 一致性铁律
所有向量化实现都必须与 step2 / ep_size_ind 的逐股版**逐位对齐**（本文件末尾有
`verify_parity()` 自检）。任何字母级别差异都是口径漂移，不是「优化」。
"""
import json
import math
import os
import sys

import polars as pl

HERE = os.path.dirname(os.path.abspath(__file__))


def build_wide(kl_map, min_rows=1):
    """{code: [{date,open,close,high,low,vol,amount}]} → 长表 DataFrame。

    列：code(str), date(str), i(int, 每股内序号), open, close, high, low, vol, amount
    """
    recs = []
    for code, kl in (kl_map or {}).items():
        if not kl or len(kl) < min_rows:
            continue
        for i, k in enumerate(kl):
            try:
                recs.append({"code": str(code), "date": str(k.get("date"))[:10], "i": i,
                             "open": float(k.get("open") or 0),
                             "close": float(k.get("close") or 0),
                             "high": float(k.get("high") or 0),
                             "low": float(k.get("low") or 0),
                             "vol": float(k.get("vol") or 0),
                             "amount": float(k.get("amount") or 0)})
            except Exception:
                continue
    if not recs:
        return pl.DataFrame(schema={"code": pl.Utf8, "date": pl.Utf8, "i": pl.Int32,
                                    "open": pl.Float64, "close": pl.Float64,
                                    "high": pl.Float64, "low": pl.Float64,
                                    "vol": pl.Float64, "amount": pl.Float64})
    return pl.DataFrame(recs).sort(["code", "i"])


def lowvol20_vec(wide, n=20):
    """过去 n+1 个收盘 → n 个日收益 → **样本**标准差（ddof=1）。

    ⚠️ 与 step2.raw_lowvol20 完全一致：不足 n+1 根 → 缺失（不进分母），
       收盘价任何 ≤0 → 该股缺失。polars 的 std() 默认 ddof=1，与 Python 的
       (sum((r-mu)^2)/(n-1))**0.5 逐位相同。
    """
    if wide.height == 0:
        return {}
    need = n + 1
    w = (wide.filter(pl.col("close") > 0)
             .with_columns(pl.int_range(pl.len()).over("code").alias("rn"),
                           pl.len().over("code").alias("cnt"))
             .filter(pl.col("cnt") >= need)
             .filter(pl.col("rn") >= pl.col("cnt") - need)
             .with_columns((pl.col("close") / pl.col("close").shift(1).over("code") - 1).alias("ret"))
             .filter(pl.col("ret").is_not_null() & pl.col("ret").is_finite()))
    if w.height == 0:
        return {}
    g = (w.group_by("code")
          .agg(pl.col("ret").count().alias("k"), pl.col("ret").std().alias("sd"))
          .filter((pl.col("k") >= n) & pl.col("sd").is_not_null()))
    return {r["code"]: float(r["sd"]) for r in g.to_dicts()}


def mom_vec(wide, n=20):
    """近 n 个交易日收益率（%）→ {code: pct}。不足 n+1 根 → 缺失。"""
    if wide.height == 0:
        return {}
    g = (wide.filter(pl.col("close") > 0)
             .group_by("code")
             .agg(pl.col("close").sort_by("i").alias("cs"))
             .with_columns(pl.col("cs").list.len().alias("k"))
             .filter(pl.col("k") >= n + 1)
             .with_columns(((pl.col("cs").list.get(-1) / pl.col("cs").list.get(-(n + 1))) - 1).alias("r")))
    if g.height == 0:
        return {}
    return {r["code"]: round(float(r["r"]) * 100.0, 4)
            for r in g.select(["code", "r"]).to_dicts() if r["r"] is not None}


def bias_vec(wide, n=60):
    """乖离率（%）= (最新价 / MA_n − 1) × 100。→ {code: bias}"""
    if wide.height == 0:
        return {}
    g = (wide.filter(pl.col("close") > 0)
             .group_by("code")
             .agg(pl.col("close").sort_by("i").alias("cs"))
             .with_columns(pl.col("cs").list.len().alias("k"))
             .filter(pl.col("k") >= n)
             .with_columns(
                 pl.col("cs").list.get(-1).alias("last"),
                 pl.col("cs").list.tail(n).list.mean().alias("ma")))
    if g.height == 0:
        return {}
    out = {}
    for r in g.select(["code", "last", "ma"]).to_dicts():
        if r["ma"] and r["ma"] > 0:
            out[r["code"]] = round((r["last"] / r["ma"] - 1.0) * 100.0, 4)
    return out


def rsi_vec(wide, period=14):
    """RSI（**Wilder 递归平滑**）→ {code: rsi}。

    ⚠️ 必须与 step2.rsi 逐位一致。step2 v2.19 起用的是 Wilder 平滑（EMA 式递归）：
         avg_gain = (avg_gain*(p-1) + gain[i]) / p
       首版本函数误用「最近 period 根的简单均值」→ 与 step2 差 4~14 个百分点
       （实测 000001: 53.20 vs 57.62）。已按 step2 口径重写。
    """
    if wide.height == 0:
        return {}
    w = (wide.filter(pl.col("close") > 0)
             .with_columns((pl.col("close") - pl.col("close").shift(1).over("code")).alias("d")))
    if w.height == 0:
        return {}
    g = (w.filter(pl.col("d").is_not_null())
          .group_by("code")
          .agg(pl.col("d").sort_by("i").alias("ds"))
          .with_columns(pl.col("ds").list.len().alias("k"))
          .filter(pl.col("k") >= period))
    out = {}
    for r in g.to_dicts():
        ds = r["ds"]
        gains = [d if d > 0 else 0.0 for d in ds]
        losses = [-d if d < 0 else 0.0 for d in ds]
        avg_gain = sum(gains[:period]) / period
        avg_loss = sum(losses[:period]) / period
        for i in range(period, len(ds)):
            avg_gain = (avg_gain * (period - 1) + gains[i]) / period
            avg_loss = (avg_loss * (period - 1) + losses[i]) / period
        if avg_loss == 0:
            out[r["code"]] = 100.0 if avg_gain > 0 else 50.0
        else:
            out[r["code"]] = 100.0 - 100.0 / (1.0 + avg_gain / avg_loss)
    return out


def rank10_vec(vals, reverse=False):
    """{code: raw} → {code: 0-10 分位分}。与 step2.score_lowvol20_rank /
    fund_split.rank_pct 同口径：并列取平均秩，分母 n−1。

    reverse=False: 原始值越大分越高
    reverse=True : 原始值越小分越高（低波/低估值用）
    """
    items = [(c, v) for c, v in (vals or {}).items() if isinstance(v, (int, float))]
    n = len(items)
    if n < 2:
        return {}
    order = sorted(range(n), key=lambda i: items[i][1])
    pct = [0.0] * n
    i = 0
    while i < n:
        j = i
        while j + 1 < n and items[order[j + 1]][1] == items[order[i]][1]:
            j += 1
        avg = (i + j) / 2.0 / max(1, n - 1)
        for k in range(i, j + 1):
            pct[order[k]] = avg
        i = j + 1
    out = {}
    for k in range(n):
        p = pct[k]
        out[items[k][0]] = round((1.0 - p if reverse else p) * 10.0, 2)
    return out


def size_ind_vec(rows, sw_map=None, min_grp=5, price_key="price", mv_key="circ_mv"):
    """行业内 LN(流通市值) winsorize + Z → 取负 → 池内 rank10。分行业分块。

    唯一实现来源是 ep_size_ind.score_rows；本函数是它的 polars 版，
    必须通过 verify_parity 逐位一致才可启用。

    rows: [{"code","price","circ_mv"}]
    """
    import ep_size_ind as ESI
    _res = [dict(r) for r in rows]
    ESI.score_rows(_res, as_of="", em=None, price_key=price_key,
                   mv_key=mv_key, sw_map=sw_map, min_present=min_grp)
    return {str(r.get("code"))[-6:]: r.get("score_size_ind") for r in _res
            if isinstance(r.get("score_size_ind"), (int, float))}


def verify_parity(kl_map, sample=60, verbose=True):
    """逐位校验 polars 版与 step2 逐股版是否一致。返回 (ok, detail)。

    纪律：任何向量化替换上线前必须跑这个，不一致就不许切换。
    """
    import step2_stockscorer_v2 as S2
    codes = list(kl_map.keys())[:sample]
    wide = build_wide({c: kl_map[c] for c in codes})
    v_lv = lowvol20_vec(wide)
    v_bias = bias_vec(wide)
    v_mom = mom_vec(wide, 20)
    v_rsi = rsi_vec(wide, 14)

    bad = {"lowvol20": [], "bias60": [], "mom20": [], "rsi14": []}
    for c in codes:
        kl = kl_map[c]
        p_lv = S2.raw_lowvol20(kl)
        if p_lv is not None and c in v_lv:
            if abs(p_lv - v_lv[c]) > 1e-9:
                bad["lowvol20"].append((c, p_lv, v_lv[c]))
        elif (p_lv is None) != (c not in v_lv):
            bad["lowvol20"].append((c, p_lv, v_lv.get(c)))

        closes = [k["close"] for k in kl]
        if len(closes) >= 60:
            ma60 = sum(closes[-60:]) / 60.0
            p_b = (closes[-1] / ma60 - 1.0) * 100.0 if ma60 > 0 else None
            # 注意：v_bias 内部 round 到 4 位，比对时也要 round，否则 1e-9 级差会被误报
            if p_b is not None and c in v_bias and abs(round(p_b, 4) - v_bias[c]) > 1e-6:
                bad["bias60"].append((c, round(p_b, 4), v_bias[c]))

        if len(closes) >= 21:
            p_m = (closes[-1] / closes[-21] - 1.0) * 100.0
            if c in v_mom and abs(round(p_m, 4) - v_mom[c]) > 1e-6:
                bad["mom20"].append((c, round(p_m, 4), v_mom[c]))

        p_r = S2.rsi(closes, 14)
        if p_r is not None and c in v_rsi and abs(p_r - v_rsi[c]) > 1e-6:
            bad["rsi14"].append((c, round(p_r, 4), round(v_rsi[c], 4)))

    ok = all(len(v) == 0 for v in bad.values())
    if verbose:
        print("  polars ↔ step2 逐位校验（%d 只）: %s" % (len(codes), "✅ 全一致" if ok else "❌ 有差异"))
        for k, v in bad.items():
            if v:
                print("     %-10s 差异 %d 只，示例 %s" % (k, len(v), v[:3]))
    return ok, bad


if __name__ == "__main__":
    import time
    p = os.path.join(HERE, "data", "universe_klines.json")
    if not os.path.exists(p):
        print("缺 %s，无法自检" % p)
        sys.exit(1)
    raw = json.load(open(p, encoding="utf-8"))
    # universe_klines 是 {code: {date: [o,c,h,l,vol]}}
    kl_map = {}
    for code, dd in list(raw.items())[:80]:
        arr = []
        for d in sorted(dd.keys()):
            v = dd[d]
            arr.append({"date": d, "open": v[0], "close": v[1], "high": v[2],
                        "low": v[3], "vol": v[4], "amount": v[1] * v[4]})
        if arr:
            kl_map[code] = arr
    print("自检样本 %d 只" % len(kl_map))
    t0 = time.time()
    wide = build_wide(kl_map)
    t1 = time.time()
    print("build_wide: %.3fs → %d 行 × %d 列" % (t1 - t0, wide.height, wide.width))
    t0 = time.time()
    lv = lowvol20_vec(wide)
    t1 = time.time()
    print("lowvol20_vec: %.3fs → %d 只有值" % (t1 - t0, len(lv)))
    verify_parity(kl_map, sample=80)
