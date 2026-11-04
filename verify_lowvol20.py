#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""v4.8 lowvol20 落地验证（离线，不联网、不改生产文件）。

目的：确认新维在真实数据上「能算出来、分布正确、与既有维不冗余」。
⚠️ K 线缓存末端 = 2026-10-13，scored 文件 = 2026-10-23 → **两者不同日**，
   本脚本只用于验证计算逻辑与分布性质，**其绝对数值不得当成 10-23 的评分**。

四条检查：
  A 覆盖率：490 只里有多少只能算出 lowvol20（要求 100%）
  B 分位性质：池内分位分应严格均匀 → min 0 / max 10 / 中位 5 / 均值 5
  C 冗余度：与既有 6 个生效维的秩相关（要求 |r| ≤ 0.30）
  D 换维效果：用新权重重算 total，对比旧 total 的排序变化
"""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SCORED = os.path.join(HERE, "top200_scored_2026-10-23.json")
KLINES = os.path.join(HERE, "data", "universe_klines.json")
UNIVERSE = os.path.join(HERE, "data", "backtest_universe_result.json")


# ===== 与 step2.raw_lowvol20 逐字一致的实现（改一处必须两处同步）=====
def raw_lowvol20(kl):
    """过去 20 个交易日**日收益率标准差**（原始值）。返回 float 或 None。"""
    if not kl or len(kl) < 21:
        return None
    closes = [k["close"] for k in kl[-21:]]
    rets = [(closes[i] / closes[i - 1] - 1.0) for i in range(1, len(closes))]
    n = len(rets)
    mu = sum(rets) / n
    return (sum((r - mu) ** 2 for r in rets) / (n - 1)) ** 0.5


def score_lowvol20_rank(raw_vals):
    """原始波动率 → 池内 rank 分位分（0-10，低波=高分）。与 step2 同实现。"""
    if not raw_vals:
        return {}
    ks = sorted(raw_vals, key=lambda c: raw_vals[c])
    n = len(ks)
    out = {}
    for i, k in enumerate(ks):
        pct = (i / (n - 1)) if n > 1 else 0.5
        out[k] = round((1.0 - pct) * 10.0, 2)
    return out


def norm_code(c):
    """scored 用 'sz300438'，universe_klines 用 '300438' → 统一取后 6 位数字。
    ⚠️ 这是**验证脚本的桥接**，step2 内部 code 与 K 线同源自洽，无需此转换。"""
    s = "".join(ch for ch in str(c) if ch.isdigit())
    return s[-6:] if len(s) >= 6 else s


def kl_list_from_dict(d):
    """universe_klines.json 结构：{code: {date: [open,high,low,close,vol]}} →
    转成 step2 的 [{date, open, high, low, close, vol}] 升序列表。"""
    if not isinstance(d, dict) or not d:
        return []
    out = []
    for dt in sorted(d.keys()):
        v = d[dt]
        if not isinstance(v, (list, tuple)) or len(v) < 4:
            continue
        try:
            out.append({"date": dt, "open": float(v[0]), "high": float(v[1]),
                        "low": float(v[2]), "close": float(v[3]),
                        "vol": float(v[4]) if len(v) > 4 else 0.0})
        except (TypeError, ValueError):
            continue
    return out


def rank_corr(xs, ys):
    """Spearman 秩相关（平均秩处理并列）。"""
    def avg_rank(vals):
        idx = sorted(range(len(vals)), key=lambda i: vals[i])
        r = [0.0] * len(vals)
        i = 0
        while i < len(idx):
            j = i
            while j + 1 < len(idx) and vals[idx[j + 1]] == vals[idx[i]]:
                j += 1
            avg = (i + j) / 2.0 + 1
            for k in range(i, j + 1):
                r[idx[k]] = avg
            i = j + 1
        return r
    n = len(xs)
    if n < 3:
        return None
    rx, ry = avg_rank(xs), avg_rank(ys)
    mx, my = sum(rx) / n, sum(ry) / n
    sxy = sum((rx[i] - mx) * (ry[i] - my) for i in range(n))
    sxx = sum((rx[i] - mx) ** 2 for i in range(n)) ** 0.5
    syy = sum((ry[i] - my) ** 2 for i in range(n)) ** 0.5
    return (sxy / (sxx * syy)) if sxx > 0 and syy > 0 else None


def main():
    print("=" * 78)
    print("v4.8 lowvol20 落地验证（离线）")
    print("=" * 78)

    with open(SCORED, encoding="utf-8") as f:
        scored = json.load(f)
    rows = scored["results"]
    print(f"scored 文件：{os.path.basename(SCORED)}  date={scored.get('date')}  n={len(rows)}")

    with open(KLINES, encoding="utf-8") as f:
        klines = json.load(f)
    # code 桥接：scored 'sz300438' → klines '300438'
    kl_by_num = {norm_code(k): v for k, v in klines.items() if isinstance(v, dict) and v}
    print(f"code 桥接：scored 前缀式 → klines 纯数字，映射表 {len(kl_by_num)} 条")

    # 末端日期（判新鲜度）
    ends = {}
    for code, dd in klines.items():
        if isinstance(dd, dict) and dd:
            ends[code] = max(dd.keys())
    from collections import Counter
    ec = Counter(ends.values())
    top_end = ec.most_common(1)[0]
    print(f"K 线缓存：{len(klines)} 只，末端日期 {top_end[0]}（{top_end[1]} 只）")
    print(f"⚠️  K线末端 {top_end[0]} ≠ scored date {scored.get('date')} → 仅验证逻辑，数值不代表该日")
    print()

    # ===== A 覆盖率 =====
    print("─" * 78)
    print("A 覆盖率")
    raw = {}
    missing = []
    for r in rows:
        code = r["code"]
        kl = kl_list_from_dict(kl_by_num.get(norm_code(code)) or {})
        v = raw_lowvol20(kl)
        if v is None:
            missing.append((code, len(kl)))
        else:
            raw[code] = v
    cov = len(raw) / len(rows) if rows else 0
    print(f"  可算 {len(raw)}/{len(rows)} = {cov:.2%}")
    if missing:
        print(f"  ❌ 缺失 {len(missing)} 只（K线不足 21 根）：{missing[:8]}")
    else:
        print("  ✅ 全池 100% 覆盖，无缺失")

    # 原始波动率分布
    vals = sorted(raw.values())
    n = len(vals)
    def pct(p):
        return vals[min(n - 1, max(0, int(p * (n - 1))))]
    print(f"  原始波动率（日收益标准差）：min {vals[0]*100:.2f}%  "
          f"p25 {pct(.25)*100:.2f}%  中位 {pct(.5)*100:.2f}%  "
          f"p75 {pct(.75)*100:.2f}%  max {vals[-1]*100:.2f}%")

    # ===== B 分位性质 =====
    print()
    print("─" * 78)
    print("B 池内分位性质（应严格均匀）")
    rank = score_lowvol20_rank(raw)
    rv = sorted(rank.values())
    m = len(rv)
    mean = sum(rv) / m
    med = rv[m // 2] if m % 2 else (rv[m // 2 - 1] + rv[m // 2]) / 2
    print(f"  n={m}  min {rv[0]:.2f}  max {rv[-1]:.2f}  中位 {med:.2f}  均值 {mean:.3f}")
    ok_b = (abs(rv[0]) < 0.01 and abs(rv[-1] - 10.0) < 0.01
            and abs(med - 5.0) < 0.05 and abs(mean - 5.0) < 0.05)
    print(f"  {'✅ 均匀分布，性质符合池内分位定义' if ok_b else '❌ 分布异常'}")

    # ===== C 冗余度 =====
    print()
    print("─" * 78)
    print("C 与既有生效维的秩相关（要求 |r| ≤ 0.30 才算独立）")
    DIMS = ["score_fund", "score_quality", "score_tech_neg",
            "score_bias60", "score_rsi_rev", "score_size"]
    codes = [c for c in rank if any(r["code"] == c for r in rows)]
    by_code = {r["code"]: r for r in rows}
    corrs = {}
    for k in DIMS:
        xs, ys = [], []
        for c in codes:
            v = by_code[c].get(k)
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                xs.append(rank[c])
                ys.append(float(v))
        rr = rank_corr(xs, ys) if len(xs) >= 30 else None
        corrs[k] = rr
        flag = "✅" if rr is not None and abs(rr) <= 0.30 else ("⚠️" if rr is not None else "—")
        print(f"  {flag} {k:<20} r = {rr:+.3f}" if rr is not None
              else f"  — {k:<20} n={len(xs)} 不足")
    worst = max((abs(v) for v in corrs.values() if v is not None), default=0)
    print(f"  最冗余 |r| = {worst:.3f} → {'✅ 不冗余' if worst <= 0.30 else '❌ 存在冗余'}")

    # ===== D 换维效果 =====
    print()
    print("─" * 78)
    print("D 换维后的 total 变化（同池同维度，仅权重不同）")
    W_NEW = {"score_fund": 0.15, "score_quality": 0.12, "score_lowvol20": 0.13,
             "score_tech_neg": 0.15, "score_bias60": 0.175, "score_rsi_rev": 0.175,
             "score_size": 0.10}
    W_OLD = {"score_fund": 0.15, "score_quality": 0.12, "score_growth": 0.13,
             "score_tech_neg": 0.15, "score_bias60": 0.175, "score_rsi_rev": 0.175,
             "score_size": 0.10}
    # 量程：scored 文件里 fund/bias60/rsi_rev 是 0-5 制，其余 0-10
    RAW5 = {"score_fund", "score_bias60", "score_rsi_rev"}
    DMAX = {"score_fund": 5.0, "score_quality": 10.0, "score_growth": 10.0,
            "score_lowvol20": 10.0, "score_tech_neg": 10.0, "score_bias60": 5.0,
            "score_rsi_rev": 5.0, "score_size": 10.0}

    def total_of(r, wmap):
        num = 0.0
        den = 0.0
        for k, w in wmap.items():
            if w <= 0:
                continue
            v = r.get(k)
            if k == "score_lowvol20":
                v = rank.get(r["code"])
            if not isinstance(v, (int, float)) or isinstance(v, bool):
                continue          # ⚠️ 缺失从分母剔除，禁 or 0
            num += (float(v) / DMAX[k] * 10.0) * w
            den += w
        return (num / den * 10.0) if den > 0 else None

    olds, news = [], []
    for r in rows:
        a, b = total_of(r, W_OLD), total_of(r, W_NEW)
        if a is not None and b is not None:
            olds.append((r["code"], r["name"], a))
            news.append((r["code"], r["name"], b))
    for lbl, arr in (("旧 v4.7（含成长 0.13）", olds), ("新 v4.8（含低波 0.13）", news)):
        vs = sorted(x[2] for x in arr)
        k = len(vs)
        print(f"  {lbl}: n={k}  min {vs[0]:.2f}  p25 {vs[k//4]:.2f}  "
              f"中位 {vs[k//2]:.2f}  p75 {vs[3*k//4]:.2f}  max {vs[-1]:.2f}  均值 {sum(vs)/k:.2f}")

    # Top20 名单变化
    old_top = [x[0] for x in sorted(olds, key=lambda t: -t[2])[:20]]
    new_top = [x[0] for x in sorted(news, key=lambda t: -t[2])[:20]]
    keep = len(set(old_top) & set(new_top))
    print(f"  Top20 重合 {keep}/20（换维后名单变动 {20-keep} 只）")
    nm = {c: n for c, n, _ in olds}
    added = [c for c in new_top if c not in set(old_top)]
    if added:
        print(f"  新进 Top20：{'、'.join(nm.get(c, c) for c in added[:8])}")
    dropped = [c for c in old_top if c not in set(new_top)]
    if dropped:
        print(f"  掉出 Top20：{'、'.join(nm.get(c, c) for c in dropped[:8])}")

    print()
    print("=" * 78)
    allok = (cov >= 0.999) and ok_b and (worst <= 0.30)
    print("结论：" + ("✅ A/B/C 三项全过，lowvol20 代码路径有效、分布正确、不冗余"
                  if allok else "❌ 存在未通过项，见上"))
    print("⚠️ 提醒：本验证用的是 K 线末端 " + top_end[0] + "，与 scored "
          + str(scored.get("date")) + " 不同日；正式评分须重跑 step2 拉当日数据。")
    print("=" * 78)
    return 0 if allok else 1


if __name__ == "__main__":
    sys.exit(main())
