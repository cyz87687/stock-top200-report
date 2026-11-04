#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""#284 拉取长窗口前复权日线 → data/klines_long.json

【为什么必须做这件事（硬约束推导，勿删）】
现有价格库 factor_history.prices.dates = 143 个交易日（2026-03-24 ~ 2026-10-23）。
在此基础上：
  · bias60 需 61 根收盘 → 最早可算基准日 = 2026-06-24（index 61）
  · h20   需 20 日前视 → 最后可用基准日 = 2026-09-17
  → h20 基准首末跨度最多 87 个日历天 = 2.86 个月 < 3 个月（老大硬下限）
  → 且「h20 跨度 ≥91 天」要求 WIN_START ≤ 2026-06-18，
     与「bias60 预热要求 ≥ 2026-06-24」互斥。
唯一出路：把价格库起点向前回补。实测腾讯 need=250 可回溯至 2025-12-24，
本脚本用 need=300 留余量，使起点落到 2025-10 前，彻底解除互斥。

【抓取范围】取四个集合的并集（去重后约 2200 只），而非锁定当前池：
  A. EM 财务缓存全部代码   —— 决定「能算财务维」的上限
  B. 申万行业映射全部代码   —— 决定「能算 size_ind」的上限
  C. factor_history.prices —— 已有历史，保底覆盖
  D. data/universe_klines  —— 已有历史（含 8 位键，需归一）
  域最终 = A∩B∩(价格有值) 且不锁定当前池 → 避免「只测当前池」的选择偏差。
  ⚠️ 不抓全 A 5576 只的理由：财务缓存仅 2157 只，多抓的价格永远进不了域，
     纯浪费 35 分钟网络时间。

【输出】
  data/klines_long.json = {code6: {date: [open, close, high, low, volume]}}
  与 data/universe_klines.json 同构，便于 audit 脚本直接复用既有解析逻辑。

用法
  python fetch_klines_long.py                # 增量：已有则跳过（保留旧值）
  python fetch_klines_long.py --force        # 强制全量重抓
  python fetch_klines_long.py --limit 50     # 只抓 50 只（冒烟用）
  python fetch_klines_long.py --need 300     # 覆盖窗口长度（默认 300 根）
"""
import os
import sys
import json
import time
import argparse
import urllib.request
from collections import defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import factor_eval as fe

OUT = os.path.join(HERE, "data", "klines_long.json")

TQ_HOSTS = ["https://ifzq.gtimg.cn", "https://web.ifzq.gtimg.cn",
            "https://proxy.finance.qq.com/ifzqgtimg"]


def fetch_ohlcv(sym, need=300):
    """腾讯前复权日线 → [(date, open, close, high, low, volume)]。
    注：qfqday 原始行 = [日期, 开, 收, 高, 低, 量]（与 factor_eval._fetch_qfq 同源）。
    """
    for _host in TQ_HOSTS:
        url = f"{_host}/appstock/app/fqkline/get?param={sym},day,,,{need},qfq"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                txt = r.read().decode("utf-8")
        except Exception:
            continue
        if txt.lstrip().startswith("<"):
            continue
        try:
            data = json.loads(txt)
            kdata = data.get("data", {}).get(sym, {})
            raw = kdata.get("qfqday") or kdata.get("day")
        except Exception:
            continue
        if not raw:
            continue
        out = []
        for k in raw:
            try:
                out.append((str(k[0])[:10], float(k[1]), float(k[2]),
                            float(k[3]), float(k[4]), float(k[5])))
            except Exception:
                continue
        if out:
            return out
    return None


def collect_targets():
    """四个集合取并集 → sorted(code6)，并回报每个集合的规模。"""
    def _norm(k):
        k = str(k or "").strip().lower()
        if len(k) > 6 and k[:2] in ("sh", "sz", "bj"):
            k = k[2:]
        return k if (len(k) == 6 and k.isdigit()) else None

    stat = {}

    # A. EM 财务缓存
    setA = set()
    try:
        import ep_size_ind as esi
        em = esi.load_em_all()
        setA = {_norm(c) for c in em if _norm(c)}
        stat["A_em_fin"] = len(setA)
        stat["A_keys_sample"] = sorted(list(em.keys()))[:3]
    except Exception as e:
        stat["A_em_fin_err"] = f"{type(e).__name__}: {e}"

    # B. 申万行业映射
    setB = set()
    try:
        sw = json.load(open(os.path.join(HERE, "sector", "sw_map.json"), encoding="utf-8"))
        for lvl in ("l1", "l2"):
            for k in (sw.get(lvl) or {}):
                n = _norm(k)
                if n:
                    setB.add(n)
        stat["B_sw_map"] = len(setB)
    except Exception as e:
        stat["B_sw_map_err"] = f"{type(e).__name__}: {e}"

    # C. factor_history.prices
    setC = set()
    try:
        h = json.load(open(os.path.join(HERE, "factor_history.json"), encoding="utf-8"))
        setC = {_norm(c) for c in ((h.get("prices") or {}).get("px") or {}) if _norm(c)}
        stat["C_hist_px"] = len(setC)
        stat["C_range"] = [((h.get("prices") or {}).get("dates") or [None])[0],
                           ((h.get("prices") or {}).get("dates") or [None])[-1]]
        stat["C_n_dates"] = len((h.get("prices") or {}).get("dates") or [])
    except Exception as e:
        stat["C_hist_px_err"] = f"{type(e).__name__}: {e}"

    # D. universe_klines（8 位键需归一）
    setD = set()
    try:
        kl = json.load(open(os.path.join(HERE, "data", "universe_klines.json"), encoding="utf-8"))
        setD = {_norm(c) for c in kl if _norm(c)}
        _ds = sorted({d for m in kl.values() for d in m})
        stat["D_univ_kl"] = len(setD)
        stat["D_raw_keys"] = len(kl)
        stat["D_range"] = [_ds[0], _ds[-1]] if _ds else None
    except Exception as e:
        stat["D_univ_kl_err"] = f"{type(e).__name__}: {e}"

    # E. 当前评分池（仅作参照，不作为抓取范围限制）
    setE = set()
    try:
        import glob as _g
        _fp = sorted(_g.glob(os.path.join(HERE, "top200_scored_*.json")))[-1]
        _o = json.load(open(_fp, encoding="utf-8"))
        setE = {_norm(r.get("code")) for r in (_o.get("results") or []) if _norm(r.get("code"))}
        stat["E_cur_pool"] = len(setE)
        stat["E_cur_pool_file"] = os.path.basename(_fp)
    except Exception as e:
        stat["E_cur_pool_err"] = f"{type(e).__name__}: {e}"

    # F. 有股本估计的代码（面板 size_ind 维必需）
    setF = set()
    try:
        _sh = set()
        h = json.load(open(os.path.join(HERE, "factor_history.json"), encoding="utf-8"))
        for s in h.get("snapshots", []):
            for r in s.get("rows", []):
                c = _norm(r.get("code"))
                mv, pr = r.get("circ_mv"), r.get("price")
                if c and isinstance(mv, (int, float)) and mv and isinstance(pr, (int, float)) and pr > 0:
                    _sh.add(c)
        import glob as _g2
        for _fp in sorted(_g2.glob(os.path.join(HERE, "top200_scored_*.json")))[-6:]:
            try:
                _o = json.load(open(_fp, encoding="utf-8"))
            except Exception:
                continue
            for r in (_o.get("results") or []):
                c = _norm(r.get("code"))
                mv, pr = r.get("circ_mv"), r.get("price")
                if c and isinstance(mv, (int, float)) and mv and isinstance(pr, (int, float)) and pr > 0:
                    _sh.add(c)
        setF = _sh
        stat["F_shares"] = len(setF)
    except Exception as e:
        stat["F_shares_err"] = f"{type(e).__name__}: {e}"

    # ---------------- 抓取范围（关键优化，勿随意改回全并集）
    # 域的上限 = A ∩ B ∩ F（缺财务/already行业/缺股本任一 → 该票永远进不了面板）。
    # 实测：A∪B∪C∪D∪E 全并集 = 5232 只，而 A∩B∩F 仅约 2100 只 ——
    # 抓全并集等于多抓 3000 只**永远用不上**的票，纯浪费半小时网络。
    # 故只抓 (A∩B∩F) ∪ C ∪ E：既覆盖面板全部潜在成员，又保留历史价格库与当前池。
    core = (setA & setB & setF) if (setA and setB and setF) else (setA | setB)
    union = sorted((core | setC | setE) - {None})
    stat["core_A_B_F"] = len(core)
    stat["union_all"] = len((setA | setB | setC | setD | setE) - {None})
    stat["union"] = len(union)
    stat["intersect_A_B"] = len(setA & setB)
    return union, stat


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--need", type=int, default=300)
    args = ap.parse_args()

    codes, stat = collect_targets()
    print("=" * 96)
    print("【抓取范围盘点】")
    for k in sorted(stat):
        if k == "A_keys_sample":
            continue
        print(f"  {k:22s} {stat[k]}")
    print("=" * 96)

    old = {}
    if os.path.exists(OUT) and not args.force:
        try:
            old = json.load(open(OUT, encoding="utf-8"))
            print(f"载入旧库 {len(old)} 只（增量模式）")
        except Exception:
            old = {}

    if args.limit:
        codes = codes[:args.limit]
        print(f"⚠️ 冒烟模式：只抓前 {len(codes)} 只")

    res = dict(old)
    n_ok = n_fail = n_skip = 0
    t0 = time.time()
    for i, c in enumerate(codes):
        if (i + 1) % 100 == 0:
            el = time.time() - t0
            eta = el / (i + 1) * (len(codes) - i - 1)
            print(f"   进度 {i+1}/{len(codes)}  ok={n_ok} fail={n_fail} skip={n_skip} "
                  f"已用{el/60:.1f}min ETA{eta/60:.1f}min", flush=True)
        prev = old.get(c)
        # 增量：旧库已有且日期已覆盖到足够新 → 仍需重抓一次拉齐尾部
        sym = fe._symbol(c)
        if not sym:
            n_skip += 1
            continue
        kl = fetch_ohlcv(sym, need=args.need)
        if not kl:
            if prev:
                n_skip += 1
            else:
                n_fail += 1
            continue
        m = {d: [o, cl, hi, lo, v] for d, o, cl, hi, lo, v in kl}
        # 旧值兜底合并（新数据优先）
        if prev:
            for d, v in prev.items():
                if d not in m:
                    m[d] = v
        res[c] = m
        n_ok += 1
        time.sleep(0.02)

    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(res, f, ensure_ascii=False)

    all_dates = sorted({d for m in res.values() for d in m})
    print("=" * 96)
    print(f"✅ 抓取完成：成功 {n_ok} · 失败 {n_fail} · 保留旧 {n_skip} · 库内 {len(res)} 只")
    if all_dates:
        print(f"   日期区间 {all_dates[0]} ~ {all_dates[-1]}，共 {len(all_dates)} 个交易日")
    print(f"   → {OUT}（{os.path.getsize(OUT)/1024/1024:.1f} MB）")
    print("=" * 96)

    # 关键校验：各只覆盖长度分布 + 早于 2026-03-24 的可用性
    lens = sorted(len(m) for m in res.values())
    early = sum(1 for m in res.values() if m and min(m) < "2026-03-24")

    def _q(arr, p):
        if not arr:
            return 0
        return arr[min(len(arr) - 1, int(p * (len(arr) - 1)))]
    print(f"   覆盖长度分位：min={lens[0] if lens else 0} p25={_q(lens,0.25)} "
          f"med={_q(lens,0.5)} p75={_q(lens,0.75)} max={lens[-1] if lens else 0}")
    print(f"   起点早于 2026-03-24 的股票数 = {early} / {len(res)}")
    # 交易日覆盖率：单只最多 301 根，但并集可能有异常日期 → 按月统计各日有价只数
    dcnt = defaultdict(int)
    for m in res.values():
        for d in m:
            dcnt[d] += 1
    if dcnt:
        mx = max(dcnt.values())
        thr = max(5, int(mx * 0.05))
        real_days = sorted(d for d, c2 in dcnt.items() if c2 >= thr)
        print(f"   日期并集 {len(dcnt)} 个，剔除低覆盖(<{thr}只)后真实交易日 {len(real_days)} 个"
              f"（{real_days[0] if real_days else '-'} ~ {real_days[-1] if real_days else '-'}）")

    meta = {"stat": stat, "need": args.need, "n_codes": len(res),
            "date_range": [all_dates[0], all_dates[-1]] if all_dates else None,
            "n_dates": len(all_dates), "early_codes": early,
            "fetched_at": time.strftime("%Y-%m-%d %H:%M:%S")}
    with open(os.path.join(HERE, "data", "klines_long_meta.json"), "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)
    print(f"   元信息 → data/klines_long_meta.json")


if __name__ == "__main__":
    main()
