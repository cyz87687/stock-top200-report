#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""#285 重建真·大池长面板（回测窗口 ≥3 个月）→ data/panel_round34.json

【与 round30b 的三个本质区别（这是本脚本存在的理由）】
1. 域不锁定历史小池。round30b 的 DOM = seen & ...，其中 seen 来自 factor_history
   的快照代码 —— 而该快照 76/85 是冻结旧池（~195 只）。本脚本改为
   DOM = EM财务 ∩ 申万行业 ∩ 长价格库 ∩ 有股本，实测目标 ~2000 只量级。
2. 价格源换成 data/klines_long.json（need≈300，起点 2025-07）。
   round30b 用 factor_history.prices（143 日，起点 2026-03-24），
   导致 bias60 预热(需61根→最早06-24)与 h20 前视(需20日→最晚09-17)互斥，
   h20 跨度最多 2.86 月 < 3 月。长价格库彻底解除该互斥。
3. 窗口由 --win-start 显式指定，且**脚本自身校验**每维实际跨度 ≥3 个月；
   不达标即在该维输出 insufficient_window 标记，绝不静默当合格。

【8 维数据链路（历史可重算性已逐个核过）】
  quality   : fund_split.score_rows(财务 PIT)              —— 全量可算
  ep        : esi.raw_ep(财务 PIT + 当日价)                 —— 缺价票算不出
  size_ind  : esi.group_winsorize_zneg(价×股本, 申万分组)   —— 同上
  tech_neg  : 10 − step2.score_tech(OHLCV≥60日)            —— 长价格库可算
  bias60    : audit_round24b.backfill_reversal              —— 61 收盘
  rsi_rev   : 同上                                          —— ≥15 收盘
  lowvol20  : 20 日收益样本标准差 → 整池 rank                —— 21 收盘
  fund      : ⚠️ 只能继承 data/fwd_hist/*.json（2026-06-26 起）
              —— 永久不可重算（缺 roe/gross_margin/cagr3/fwd_np 等 7 个入参，
                 round30 实测复现率仅 7.8%）。故 fund 维的有效窗口
                 独立于其它维，跨度单独统计并在输出里显式标注。

用法
  python build_panel_round34.py --probe                 # 只探窗口边界，不建面板
  python build_panel_round34.py --win-start 2026-05-06  # 建面板
  python build_panel_round34.py --win-start 2026-05-06 --max-dates 40  # 小样本冒烟
"""
import os
import sys
import json
import math
import glob
import argparse
import datetime as dt
from collections import defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import factor_eval as fe
import audit_round21 as r21
import audit_round26 as r26
import audit_round24b as rb
import ep_size_ind as esi
import fund_split as fs
import step2_stockscorer_v2 as s2
import audit_round29 as r29

load_em_all = r26.load_em_all

OUT = os.path.join(HERE, "data", "panel_round34.json")
KLONG = os.path.join(HERE, "data", "klines_long.json")
KHIST = os.path.join(HERE, "data", "universe_klines.json")


def is_num(x):
    return isinstance(x, (int, float)) and not isinstance(x, bool) and x == x


def is_pos_num(x):
    return is_num(x) and x > 0


def month_span(d0, d1):
    """两个 YYYY-MM-DD 之间的日历跨度（月，按 30.44 天/月折算）。"""
    a = dt.date(*map(int, d0.split("-")))
    b = dt.date(*map(int, d1.split("-")))
    days = (b - a).days
    return days, days / 30.4375


# ============================================================ 价格库装配

def load_price_source():
    """长价格库优先，回退 universe_klines，再回退 factor_history.prices。
    返回 (tdays, px, src_note)。px = {code6: {date: close}}。

    ⚠️ P0（2026-10-27 实测踩坑）：返回的 tdays 必须是**过滤后的真实交易日**，
    不能直接用「各股日期并集」。klines_long 的并集有 337 个日期，其中 33 个是
    2025-06~07 的低覆盖异常日期（当时只有少数股票在库）。
    若不过滤，bias60/rsi_rev 回溯 61 根时会取到 None →
    实测三维覆盖率中位从 ~1900 直接塌到 **1**（整维静默失效）。
    """
    notes = []
    msg = {}

    def _finish(px, src):
        """统一出口：并集 → 覆盖率过滤 → 真实交易日序列。"""
        tdays = market_tdays(px)
        msg[src]["dates_raw"] = len({d for m in px.values() for d in m})
        msg[src]["dates_clean"] = len(tdays)
        msg[src]["range"] = [tdays[0], tdays[-1]] if tdays else None
        return tdays, px, "+".join(notes), msg

    # 主源：klines_long（本脚本期望的源）
    if os.path.exists(KLONG):
        try:
            raw = json.load(open(KLONG, encoding="utf-8"))
            px = {}
            for c, m in raw.items():
                c6 = str(c)[-6:]
                if len(c6) != 6 or not c6.isdigit():
                    continue
                px[c6] = {d: v[1] for d, v in m.items() if v and len(v) >= 2 and is_pos_num(v[1])}
            msg["klines_long"] = {"codes": len(px)}
            notes.append("klines_long")
            if px:
                return _finish(px, "klines_long")
        except Exception as e:
            msg["klines_long_err"] = f"{type(e).__name__}: {e}"

    # 回退 1：universe_klines（8 位键需归一）
    if os.path.exists(KHIST):
        try:
            raw = json.load(open(KHIST, encoding="utf-8"))
            px = {}
            for c, m in raw.items():
                k = str(c).lower()
                if len(k) > 6 and k[:2] in ("sh", "sz", "bj"):
                    k = k[2:]
                if len(k) != 6 or not k.isdigit():
                    continue
                d = px.setdefault(k, {})
                for dd, v in m.items():
                    if v and len(v) >= 2 and is_pos_num(v[1]):
                        d[dd] = v[1]
            msg["universe_klines"] = {"codes": len(px)}
            notes.append("universe_klines")
            if px:
                return _finish(px, "universe_klines")
        except Exception as e:
            msg["universe_klines_err"] = f"{type(e).__name__}: {e}"

    # 回退 2：factor_history.prices
    h = json.load(open(os.path.join(HERE, "factor_history.json"), encoding="utf-8"))
    tdays, px = fe.px_index(h)
    msg["factor_history"] = {"codes": len(px), "dates": len(tdays)}
    notes.append("factor_history")
    return list(tdays), px, "+".join(notes), msg


def market_tdays(px, min_cover_ratio=0.05):
    """交易日序列 = 各股日期并集中，当日有价的股票数 ≥ 域规模×min_cover_ratio。
    用于剔除个别股票的异常日期（如错录的非交易日）。
    """
    cnt = defaultdict(int)
    for m in px.values():
        for d in m:
            cnt[d] += 1
    if not cnt:
        return []
    mx = max(cnt.values())
    thr = max(5, int(mx * min_cover_ratio))
    return sorted(d for d, n in cnt.items() if n >= thr)


def rebuild_shares():
    """流通股本估计 = median(circ_mv / price)。
    来源与 round29/30b 同法（历史 snapshot 中位优先，scored 文件补覆盖）。
    ⚠️ 长价格库无 circ_mv，此处**只能**从既有快照反推；这是本面板的已知边界：
       股本为跨期单一常数，不做拆股调整。对 size_ind（行业内相对大小）影响有限，
       因为行业分组内的相对排序在拆股当年基本保持。输出 meta 里显式披露。
    """
    _sh = defaultdict(list)
    h = json.load(open(os.path.join(HERE, "factor_history.json"), encoding="utf-8"))
    for s in h.get("snapshots", []):
        for r in s.get("rows", []):
            c = str(r.get("code") or "")[-6:]
            mv, pr = r.get("circ_mv"), r.get("price")
            if is_pos_num(mv) and is_pos_num(pr):
                _sh[c].append(mv / pr)
    hist = {c: sorted(v)[len(v) // 2] for c, v in _sh.items() if v}

    _sh2 = defaultdict(list)
    for fp in sorted(glob.glob(os.path.join(HERE, "top200_scored_*.json"))):
        try:
            o = json.load(open(fp, encoding="utf-8"))
        except Exception:
            continue
        for r in (o.get("results") or []):
            c = str(r.get("code") or "")[-6:]
            mv, pr = r.get("circ_mv"), r.get("price")
            if is_pos_num(mv) and is_pos_num(pr):
                _sh2[c].append(mv / pr)
    scored = {c: sorted(v)[len(v) // 2] for c, v in _sh2.items() if v}

    out = dict(scored)
    out.update(hist)          # 历史中位优先（PIT 更贴近回测期）
    return out, {"from_hist": len(hist), "from_scored": len(scored), "merged": len(out)}


def load_fwd():
    fwd = {}
    for fp in glob.glob(os.path.join(HERE, "data", "fwd_hist", "*.json")):
        try:
            d = json.load(open(fp, encoding="utf-8"))
        except Exception:
            continue
        fwd[d["date"]] = {str(r.get("code") or "")[-6:]: r for r in d.get("rows", [])}
    return fwd


# ============================================================ 窗口探测

def probe(tdays, px, fwd):
    """报告各候选 WIN_START 下 h5/h20 的基准截面数与跨度。"""
    tidx = {d: i for i, d in enumerate(tdays)}
    n = len(tdays)
    print("=" * 108)
    print(f"【窗口探测】交易日 {n} 个：{tdays[0]} ~ {tdays[-1]}")
    print("=" * 108)
    print(f"  {'WIN_START':12s} {'idx':>4s} {'h5基准':>6s} {'h5末':>11s} {'h5跨度':>14s} "
          f"{'h20基准':>7s} {'h20末':>11s} {'h20跨度':>14s}")
    out = []
    # ⚠️ P0（2026-10-27 踩坑）：下界必须 ≥61。
    #   audit_round24b.backfill_reversal 内部 `if i < 60: continue`，
    #   step2.score_tech 也要求 ≥60 根日线。
    #   原实现 lo=min(70, n//4) 在某些数据形状下会给出 idx<60 的"推荐"，
    #   实测把 WIN_START 定到 2025-09-15（idx≈37）→ bias60/rsi_rev/tech_neg
    #   三维覆盖率直接为 0，整维静默失效。故这里硬编码安全下界 61。
    WARMUP_MIN_IDX = 61
    lo = WARMUP_MIN_IDX
    cands = []
    for i in list(range(lo, min(lo + 400, n - 25), 5)) + [n - 130, n - 110, n - 95]:
        if 0 <= i < n:
            cands.append(i)
    for i in sorted(set(cands)):
        if i < WARMUP_MIN_IDX:
            continue
        d0 = tdays[i]
        # h5
        d5 = [tdays[j] for j in range(i, n - 5)]
        d20 = [tdays[j] for j in range(i, n - 20)]
        if not d5 or not d20:
            continue
        s5 = month_span(d5[0], d5[-1])
        s20 = month_span(d20[0], d20[-1])
        # 预热覆盖：该截面能真正算出 bias60 的股票数
        # 条件 = 当日有收盘价（cl[i]）且 60 根前有收盘价（cl[i-60]）
        warm = 0
        for c, m in px.items():
            if m.get(tdays[i]) and m.get(tdays[i - 60]):
                warm += 1
        warm_ok = warm >= 500          # 域规模约 2000，500 已是可运算下限
        out.append({"win_start": d0, "idx": i,
                    "h5_n": len(d5), "h5_last": d5[-1], "h5_days": s5[0], "h5_months": round(s5[1], 2),
                    "h20_n": len(d20), "h20_last": d20[-1], "h20_days": s20[0],
                    "h20_months": round(s20[1], 2),
                    "h20_ok": s20[1] >= 3.0,
                    "warm_cover": warm, "warm_cover_ok": warm_ok})
        print(f"  {d0:12s} {i:4d} {len(d5):6d} {d5[-1]:>11s} "
              f"{s5[0]:5d}天/{s5[1]:5.2f}月 {len(d20):7d} {d20[-1]:>11s} "
              f"{s20[0]:5d}天/{s20[1]:5.2f}月 {'✅' if s20[1] >= 3.0 else '❌'} "
              f"预热{warm:5d}{'✅' if warm_ok else '❌'}")

    # 推荐：h20 达标中最早的**同时预热覆盖达标**的那一个
    # ⚠️ P0（2026-10-27）：只看 h20 月份跨度会把 WIN_START 推到 idx=61，
    #   但 idx 61~62 时多数票才刚有 60 根历史（长库最早索引中位=3），
    #   bias60 可算数仅 118/143（应为 2000+）→ 整维静默塌缩。
    #   故必须叠加「预热覆盖」条件。
    ok = [x for x in out if x["h20_ok"]]
    rec = None
    for x in ok:
        if x.get("warm_cover_ok"):
            rec = x
            break
    if rec is None and ok:
        rec = ok[0]
    print("-" * 108)
    if rec:
        print(f"  ✅ 推荐 WIN_START = {rec['win_start']}：h20 {rec['h20_n']} 截面 / "
              f"{rec['h20_days']}天 / {rec['h20_months']} 月；"
              f"h5 {rec['h5_n']} 截面 / {rec['h5_days']}天 / {rec['h5_months']} 月")
        print(f"     预热覆盖：该截面可算 bias60 的股票 {rec['warm_cover']} 只 "
              f"{'✅' if rec.get('warm_cover_ok') else '❌ 不足500'}")
    else:
        print("  ❌ 无任何候选满足 h20 跨度 ≥3 个月 —— 需继续向前回补价格")
    # fund 窗口
    if fwd:
        fd = sorted(fwd)
        inter = [d for d in fd if rec and d >= rec["win_start"]] if rec else fd
        if inter:
            sp = month_span(inter[0], inter[-1])
            print(f"  ⚠️ fund 可用窗口 = {inter[0]} ~ {inter[-1]}（{len(inter)} 个日期 / "
                  f"{sp[0]}天 / {sp[1]:.2f} 月）{'✅' if sp[1] >= 3.0 else '❌ 不足3个月'}")
    print("=" * 108)
    return out, rec


# ============================================================ 面板构建

def build(win_start, max_dates=0, verbose=True):
    tdays, px, src, srcmsg = load_price_source()
    if verbose:
        print(f"【价格源】{src} · {srcmsg}")
    if not tdays:
        raise SystemExit("❌ 价格源为空")

    em = load_em_all()
    em_fs = fs.load_em_cache()
    sw_map = json.load(open(os.path.join(HERE, "sector", "sw_map.json"), encoding="utf-8"))
    swl1 = {str(k)[-6:]: v for k, v in (sw_map.get("l1") or {}).items()}
    swl2 = {str(k)[-6:]: v for k, v in (sw_map.get("l2") or {}).items()}
    shares_est, shstat = rebuild_shares()
    fwd = load_fwd()

    # 域：EM财务 ∩ 申万 ∩ 有价格 ∩ 有股本 —— 刻意不锁定当前池
    DOM = sorted(set(em) & set(swl1) & set(px) & set(shares_est))
    if verbose:
        print(f"【域构建】EM财务 {len(em)} ∩ 申万l1 {len(swl1)} ∩ 有价格 {len(px)} "
              f"∩ 有股本 {len(shares_est)} → DOM = {len(DOM)} 只")
        print(f"   股本来源 {shstat}")

    tidx = {d: i for i, d in enumerate(tdays)}
    n = len(tdays)

    # ---- 排名工具（并列以 code 为次级键，保证 PYTHONHASHSEED 无关可复现）
    def _rank10_asc(d):
        ks = sorted(d, key=lambda c: (d[c], c))
        m = len(ks)
        if m < 2:
            return {}
        return {c: round((i / (m - 1)) * 10.0, 2) for i, c in enumerate(ks)}

    def _rank10_desc(d):
        ks = sorted(d, key=lambda c: (-d[c], c))
        m = len(ks)
        if m < 2:
            return {}
        return {c: round((i / (m - 1)) * 10.0, 2) for i, c in enumerate(ks)}

    def lowvol_raw(c, d0):
        m = px.get(c)
        i0 = tidx.get(d0)
        if not m or i0 is None or i0 < 20:
            return None
        closes = []
        for j in range(i0 - 20, i0 + 1):
            v = m.get(tdays[j])
            if v is None:
                return None
            closes.append(v)
        rets = [(closes[k] / closes[k - 1] - 1.0) for k in range(1, len(closes))]
        nn = len(rets)
        if nn < 2:
            return None
        mu = sum(rets) / nn
        var = sum((r - mu) ** 2 for r in rets) / (nn - 1)
        return math.sqrt(var) if var >= 0 else None

    def tech_of(c, d0, price, pct, cagr3):
        """score_tech 需完整个 OHLCV + weekly 聚合。
        长价格库与 universe_klines 同构（[open, close, high, low, volume]），
        故直接用长库；缺则回退 KHIST。
        """
        m = None
        if os.path.exists(KLONG):
            m = _KL_CACHE.get(c)
        if not m:
            m = _KH_CACHE.get(c)
        if not m:
            return None
        ds = [x for x in sorted(m) if x <= d0][-201:]
        if len(ds) < 60:
            return None
        daily = []
        for x in ds:
            o, cl, hi, lo, vv = m[x][0], m[x][1], m[x][2], m[x][3], m[x][4]
            daily.append({"date": x, "open": o, "close": cl, "high": hi, "low": lo,
                          "vol": vv, "amount": vv * cl})
        by = {}
        for x in ds:
            y, w, _ = dt.date(*map(int, x.split("-"))).isocalendar()
            by.setdefault((y, w), []).append(x)
        weekly = []
        for k in sorted(by):
            dd = sorted(by[k])
            weekly.append({"date": dd[-1], "open": m[dd[0]][0], "close": m[dd[-1]][1],
                           "high": max(m[x][2] for x in dd),
                           "low": min(m[x][3] for x in dd),
                           "vol": sum(m[x][4] for x in dd),
                           "amount": sum(m[x][4] * m[x][1] for x in dd)})
        weekly = weekly[-30:]
        try:
            ts, _td = s2.score_tech(daily, price, pct, cagr3=cagr3, wkl=weekly)
        except Exception:
            return None
        bonus = s2.INDUSTRY_PROSPECT.get(c, 0) or s2.INDUSTRY_PROSPECT.get(
            ("sh" if c[0] == "6" else "sz") + c, 0)
        return max(0.0, min(5.0, ts + bonus))

    # OHLCV 缓存（tech_of 内部高频访问，避免逐次从 JSON 取）
    _KL_CACHE = {}
    _KH_CACHE = {}
    if os.path.exists(KLONG):
        try:
            _r = json.load(open(KLONG, encoding="utf-8"))
            for c, m in _r.items():
                c6 = str(c)[-6:]
                if len(c6) == 6 and c6.isdigit():
                    _KL_CACHE[c6] = m
        except Exception:
            pass
    if os.path.exists(KHIST):
        try:
            _r = json.load(open(KHIST, encoding="utf-8"))
            for c, m in _r.items():
                k = str(c).lower()
                if len(k) > 6 and k[:2] in ("sh", "sz", "bj"):
                    k = k[2:]
                if len(k) == 6 and k.isdigit():
                    _KH_CACHE[k] = m
        except Exception:
            pass
    if verbose:
        print(f"【OHLCV 缓存】klines_long {len(_KL_CACHE)} 只 · universe_klines {len(_KH_CACHE)} 只")

    bf = rb.backfill_reversal(tdays, px)
    dates_all = [d for d in tdays if d >= win_start]
    if max_dates:
        dates_all = dates_all[:max_dates]

    panel = {"domain": DOM, "dates": [], "rows": {}, "fund_flags": {}, "meta": {}}
    if verbose:
        print(f"【建面板】win_start={win_start} · 待建截面 {len(dates_all)} 个")
    cov = defaultdict(list)
    _PERIODS_CACHE = {}      # code → 报告期倒序列表（与截面无关，全局缓存）
    for ii, d0 in enumerate(dates_all):
        i0 = tidx[d0]
        av = [c for c in DOM if (px.get(c) or {}).get(d0)]
        if len(av) < 40:
            continue
        fw = fwd.get(d0) or {}

        qrows = [{"code": c} for c in av]
        fs.score_rows(qrows, d0, em=em_fs)
        qmap = {r["code"]: r.get("score_quality") for r in qrows
                if is_num(r.get("score_quality"))}

        lvraw = {}
        for c in av:
            v = lowvol_raw(c, d0)
            if v is not None:
                lvraw[c] = v
        lvmap = _rank10_desc(lvraw) if len(lvraw) >= 20 else {}

        mvraw = {}
        for c in av:
            price = (px.get(c) or {}).get(d0)
            if shares_est.get(c) and price:
                mvraw[c] = shares_est[c] * price
        szmap = _rank10_desc(mvraw) if len(mvraw) >= 20 else {}

        epraw = {}
        for c in av:
            ind = em.get(c)
            if not ind:
                continue
            # ⚠️ 性能：报告期列表与截面日期无关，全局只算一次（原实现每截面每股重排
            #    一次集合，2000 只 × 233 截面 = 46.6 万次冗余排序，是主瓶颈）。
            periods = _PERIODS_CACHE.get(c)
            if periods is None:
                periods = sorted({p for dd in ind.values() for p in dd}, reverse=True)
                _PERIODS_CACHE[c] = periods
            P = r21.pit_period(periods, d0)
            if P is None:
                continue
            price = (px.get(c) or {}).get(d0)
            v = esi.raw_ep(ind, P, price)
            if is_num(v):
                epraw[c] = v
        epmap = _rank10_asc(epraw) if len(epraw) >= 10 else {}

        szipairs = [(c, esi.sector_l1_of(c, sw_map), mvraw.get(c)) for c in av]
        szin, _ = esi.group_winsorize_zneg(szipairs)
        szimap = _rank10_asc(szin) if len(szin) >= 10 else {}

        cur, flags = {}, {}
        for c in av:
            price = (px.get(c) or {}).get(d0)
            fwr = fw.get(c) or {}
            sf_raw = fwr.get("score_fund")
            # fund 修复口径：PE 与 FwdPE 均缺 → None（P0-2 精确作用域）
            no_val = not (is_pos_num(fwr.get("pe")) or is_pos_num(fwr.get("fwd_pe")))
            sf_new = float(sf_raw) if (is_num(sf_raw) and not no_val) else None
            pct = fwr.get("pct_chg")
            if not is_num(pct):
                prev = (px.get(c) or {}).get(tdays[i0 - 1]) if i0 >= 1 else None
                pct = ((price / prev - 1.0) * 100.0) if (prev and price) else 0.0
            tv = tech_of(c, d0, price, pct, fwr.get("cagr3"))
            t = (bf.get(d0) or {}).get(c)
            cur[c] = {
                "score_fund": sf_new,
                "score_tech_neg": (10.0 - tv) if is_num(tv) else None,
                "score_bias60": t[1] if t else None,
                "score_rsi_rev": t[2] if t else None,
                "score_quality": qmap.get(c),
                "score_lowvol20": lvmap.get(c),
                "score_size_ind": szimap.get(c),
                "score_ep": epmap.get(c),
            }
            flags[c] = {"score_fund_old": (float(sf_raw) if is_num(sf_raw) else None),
                        "no_val": bool(no_val)}
        panel["dates"].append(d0)
        panel["rows"][d0] = cur
        panel["fund_flags"][d0] = flags
        for k in ("score_fund", "score_quality", "score_tech_neg", "score_bias60",
                  "score_rsi_rev", "score_lowvol20", "score_size_ind", "score_ep"):
            cov[k].append(sum(1 for v in cur.values() if is_num(v.get(k))))
        if verbose and (ii + 1) % 10 == 0:
            print(f"   … {d0}  n={len(av)}  ({ii+1}/{len(dates_all)})", flush=True)

    # ---- 每维窗口跨度校验（本脚本的硬要求）
    dim_window = {}
    for k in cov:
        cs = sorted(cov[k])
        med = cs[len(cs) // 2] if cs else 0
        # 只统计「该维有值股票数 ≥ 域 50%」的截面，作为该维有效窗口
        eff = [panel["dates"][j] for j, v in enumerate(cov[k])
               if v >= max(20, med * 0.5)]
        if len(eff) >= 2:
            sp = month_span(eff[0], eff[-1])
            dim_window[k] = {"first": eff[0], "last": eff[-1], "n_dates": len(eff),
                             "days": sp[0], "months": round(sp[1], 2),
                             "ok_3m": sp[1] >= 3.0,
                             "cover_med": med}
        else:
            dim_window[k] = {"first": None, "last": None, "n_dates": len(eff),
                             "months": None, "ok_3m": False, "cover_med": med}

    panel["meta"] = {
        "round": 34,
        "purpose": "真·大池长面板（域不锁定冻结旧池），窗口 ≥3 个月由脚本自校验",
        "price_source": src,
        "price_source_detail": srcmsg,
        "price_range": [tdays[0], tdays[-1]],
        "price_days": n,
        "win_start": win_start,
        "domain_size": len(DOM),
        "n_dates": len(panel["dates"]),
        "date_range": [panel["dates"][0], panel["dates"][-1]] if panel["dates"] else None,
        "dim_window": dim_window,
        "shares_stat": shstat,
        "shares_caveat": "股本为跨期单一常数（median of circ_mv/price），未做拆股调整",
        "fund_caveat": "fund 值继承 data/fwd_hist（数值本身为当年代码产物，不可重算）；"
                       "仅『PE/FwdPE 均缺 → None』这一项被精确修正",
        "lowvol_caveat": "lowvol20 用整池 rank（池逐日变化，禁跨日拼接分位）",
    }
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(panel, f, ensure_ascii=False)
    if verbose:
        print("=" * 108)
        print(f"✅ 面板已写出 → {OUT}")
        print(f"   域 {len(DOM)} 只 · 截面 {len(panel['dates'])} 个 · "
              f"{panel['meta']['date_range']}")
        print("   【各维有效窗口】")
        for k in ["score_bias60", "score_rsi_rev", "score_lowvol20", "score_tech_neg",
                  "score_ep", "score_size_ind", "score_quality", "score_fund"]:
            w = dim_window[k]
            flag = "✅" if w["ok_3m"] else "❌<3月"
            print(f"     {k:18s} {str(w['first']):>11s} ~ {str(w['last']):>11s}  "
                  f"{w['n_dates']:3d} 截面  {w['months']} 月  "
                  f"覆盖中位 {w['cover_med']:4d}  {flag}")
        print("=" * 108)
    return panel


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--probe", action="store_true")
    ap.add_argument("--win-start", default="")
    ap.add_argument("--max-dates", type=int, default=0)
    args = ap.parse_args()

    tdays, px, src, srcmsg = load_price_source()
    fwd = load_fwd()
    print(f"【价格源】{src} · {srcmsg}")

    if args.probe:
        probe(tdays, px, fwd)
        return

    ws = args.win_start
    if not ws:
        raise SystemExit("必须给 --win-start（先用 --probe 看边界）")
    build(ws, max_dates=args.max_dates)


if __name__ == "__main__":
    main()
