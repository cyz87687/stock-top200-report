#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""round30b：fund 权重的边际贡献曲线（配对检验）—— 判定 0.075 该不该动

为什么要写这个脚本（承接 round30 + round29 的矛盾）
---------------------------------------------------
round30 实测（同口径、宽样本、86 截面）：
    fund 单维 IC  h5  +0.0615  t0=+4.52   ✅ 过 Bonferroni 3.08，也过严格阈值 3.9
    fund 单维 IC  h20  +0.0951  t0=+5.37   ✅ 同上
→ **fund 维修复后仍有显著正 alpha，不是 ≈0**，老大裁决「若仍≈0 再降权」的
  触发条件**不成立**。

但 round29 的 Q3b 曾报「剔除 fund 后复合分反而上升」：
    C 八维   h5 +0.1522 → C_noFund h5 +0.1548  （Δ = +0.0026）
    C 八维   h20 +0.1882 → C_noFund h20 +0.1921 （Δ = +0.0039）

两个结论看起来打架，必须查清。本脚本给出归因：
  ⚠️ Q3b 的「剔除 fund」= 把 0.075 **按比例摊给其余七维**（Σ 仍 = 1）。
     `prod_score` 的分母是 `max(0.5, Σw)`，Σw 恒 > 0.5 → 这就是纯**权重再分配**，
     不是「删掉 fund 的信息量」。所以 Q3b 测的是「其余七维多加 0.075 权重的效果」，
     **不是**「fund 有没有信息」。用它来论证「fund 该降权」属于口径误用。
  真正该问的是：**fund 从 0.075 挪到别处，复合 IC 是否显著变好？** → 需要配对检验。

实验设计（唯一变量 = fund 权重，其余维按比例反向摊平，Σ 恒 = 1）
---------------------------------------------------------------
    W0     : fund=0.0750  ← 现行，且用**修复后**的 fund 值（PE/FwdPE 均缺 → None）
    W0_old : fund=0.0750  ← 对照：用**旧** fund 值（含伪造分），验证修复本身的影响
    W_half : fund=0.0375  ← 减半
    W_zero : fund=0.0000  ← 归零（= 纯七维，权重摊给其余）
    W_dbl  : fund=0.1500  ← 加倍（回到 v4.8 水平）

每个方案都在**同一面板、同一 GATE 卡口、同一条生产线公式**上算逐截面 rank IC，
再做**逐截面配对检验**（NW t）——只看均值差会被截面间波动淹没。

⚠️ 口径披露（不隐藏）
--------------------
面板的 fund 值继承自 `data/fwd_hist/*.json`，其**数值本身**由当年代码生成，
含 ROE 量纲 bug 的 ±0.5 加减档影响，且 `roe/cagr3/gross_margin/q3_info` 未存档、
无法重算（round30 已实测复现率仅 7.8%）。因此本脚本能精确隔离的只有
**兜底修复（无估值 → None）** 这一项；ROE 量纲修复对历史面板的影响不可复原。
结论表述必须带上这个边界。

用法
----
  python audit_round30b.py            # 有缓存则直接用
  python audit_round30b.py --rebuild  # 强制重建面板（约 2~4 分钟）
输出
----
  data/panel_round30.json
  audit_round30b.json
"""

import os
import sys
import json
import math
import glob
import argparse
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
import audit_round29 as r29          # 复用统计函数，保证与 round29 逐字同口径

build_factors = r26.build_factors
load_em_all = r26.load_em_all
nw_t = r29.nw_t
ic_stats = r29.ic_stats
T_BONF = r29.T_BONF

PANEL = os.path.join(HERE, "data", "panel_round30.json")
WIN_START = r29.WIN_START


def is_pos_num(x):
    return isinstance(x, (int, float)) and not isinstance(x, bool) and x == x and x > 0


def is_num(x):
    return isinstance(x, (int, float)) and not isinstance(x, bool) and x == x


# ============================================================ 阶段 1：构建面板

def build_panel():
    """逐字复用 audit_round29.main() 的构建段（唯一改动：把 cur 落盘而不是只算 IC）。"""
    h = json.load(open(os.path.join(HERE, "factor_history.json"), encoding="utf-8"))
    tdays, px = fe.px_index(h)
    tidx = {d: i for i, d in enumerate(tdays)}
    em = load_em_all()
    em_fs = fs.load_em_cache()

    kl = json.load(open(os.path.join(HERE, "data", "universe_klines.json"), encoding="utf-8"))
    sw_map = json.load(open(os.path.join(HERE, "sector", "sw_map.json"), encoding="utf-8"))
    swl1 = {str(k)[-6:]: v for k, v in (sw_map.get("l1") or {}).items()}

    # 股本重建（与 round29 同法：历史 snapshot 中位 > scored 补覆盖）
    _sh = defaultdict(list)
    for s in h["snapshots"]:
        for r in s["rows"]:
            c = (r.get("code") or "")[-6:]
            mv, pr = r.get("circ_mv"), r.get("price")
            if isinstance(mv, (int, float)) and mv and isinstance(pr, (int, float)) and pr > 0:
                _sh[c].append(mv / pr)
    shares_hist = {c: sorted(v)[len(v) // 2] for c, v in _sh.items()}

    _sh2 = defaultdict(list)
    for _fp in sorted(glob.glob(os.path.join(HERE, "top200_scored_*.json"))):
        try:
            _o = json.load(open(_fp, encoding="utf-8"))
        except Exception:
            continue
        for r in (_o.get("results") or []):
            c = (r.get("code") or "")[-6:]
            mv, pr = r.get("circ_mv"), r.get("price")
            if isinstance(mv, (int, float)) and mv and isinstance(pr, (int, float)) and pr > 0:
                _sh2[c].append(mv / pr)
    shares_scored = {c: sorted(v)[len(v) // 2] for c, v in _sh2.items()}
    shares_est = dict(shares_scored)
    shares_est.update(shares_hist)

    fwd = {}
    for fp in glob.glob(os.path.join(HERE, "data", "fwd_hist", "*.json")):
        d = json.load(open(fp, encoding="utf-8"))
        fwd[d["date"]] = {(r.get("code") or "")[-6:]: r for r in d["rows"]}

    seen = set()
    for s in h["snapshots"]:
        for r in (s.get("rows") or []):
            c = (r.get("code") or "")[-6:]
            if c:
                seen.add(c)

    DOM = sorted(seen & set(px) & set(em) & set(swl1) & set(shares_est))

    # ---- 排名工具（并列以 code 为次级键，保证可复现 —— round29 的 P0 教训）
    def _rank10_asc(d):
        ks = sorted(d, key=lambda c: (d[c], c))
        n = len(ks)
        if n < 2:
            return {}
        return {c: round((i / (n - 1)) * 10.0, 2) for i, c in enumerate(ks)}

    def _rank10_desc(d):
        ks = sorted(d, key=lambda c: (-d[c], c))
        n = len(ks)
        if n < 2:
            return {}
        return {c: round((i / (n - 1)) * 10.0, 2) for i, c in enumerate(ks)}

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
        n = len(rets)
        mu = sum(rets) / n
        return (sum((r - mu) ** 2 for r in rets) / (n - 1)) ** 0.5

    def tech_of(c, d0, price, pct, cagr3):
        m = kl.get(c)
        if not m:
            return None
        ds = [x for x in sorted(m) if x <= d0][-201:]
        if len(ds) < 60:
            return None
        daily = [{"date": x, "open": m[x][0], "close": m[x][1], "high": m[x][2],
                  "low": m[x][3], "vol": m[x][4], "amount": m[x][4] * m[x][1]}
                 for x in ds]
        import datetime as _dt
        by = {}
        for x in ds:
            y, w, _ = _dt.date(*map(int, x.split("-"))).isocalendar()
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

    bf = rb.backfill_reversal(tdays, px)
    dates_all = [d for d in tdays if d >= WIN_START]

    panel = {"domain": DOM, "dates": [], "rows": {}, "fund_flags": {}, "meta": {}}
    print(f"构建面板：域 {len(DOM)} 只 · 交易日 {len(dates_all)} 个")
    for ii, d0 in enumerate(dates_all):
        av = [c for c in DOM if (px.get(c) or {}).get(d0)]
        if len(av) < 40:
            continue
        i0 = tidx[d0]
        fw = fwd.get(d0) or {}

        qrows = [{"code": c} for c in av]
        fs.score_rows(qrows, d0, em=em_fs)
        qmap = {r["code"]: r.get("score_quality") for r in qrows if is_num(r.get("score_quality"))}

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
            P = r21.pit_period(sorted({p for dd in ind.values() for p in dd}, reverse=True), d0)
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

        cur = {}
        flags = {}
        for c in av:
            price = (px.get(c) or {}).get(d0)
            fwr = fw.get(c) or {}
            sf_raw = fwr.get("score_fund")
            # ⚠️ 修复后口径：PE 与 FwdPE 均缺 → None（就是 P0-2 的精确作用域）
            no_val = not (is_pos_num(fwr.get("pe")) or is_pos_num(fwr.get("fwd_pe")))
            sf_new = None
            if is_num(sf_raw) and not no_val:
                sf_new = float(sf_raw)
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
            # 旧值（含伪分）留档，供 W0_old 对照
            flags[c] = {"score_fund_old": (float(sf_raw) if is_num(sf_raw) else None),
                        "no_val": bool(no_val)}
        panel["dates"].append(d0)
        panel["rows"][d0] = cur
        panel["fund_flags"][d0] = flags
        if (ii + 1) % 10 == 0:
            print(f"   … {d0}  n={len(av)}")

    panel["meta"] = {
        "domain_size": len(DOM),
        "n_dates": len(panel["dates"]),
        "window_start": WIN_START,
        "price_days": len(tdays),
        "note": "fund 值继承 fwd_hist（数值本身为当年代码产物），"
                "仅「PE/FwdPE 均缺 → None」这一项被精确修正",
    }
    os.makedirs(os.path.dirname(PANEL), exist_ok=True)
    with open(PANEL, "w", encoding="utf-8") as f:
        json.dump(panel, f, ensure_ascii=False)
    print(f"面板已写出 → {PANEL}")
    return panel


# ============================================================ 阶段 2：权重曲线实验

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rebuild", action="store_true")
    args = ap.parse_args()

    if args.rebuild or not os.path.exists(PANEL):
        panel = build_panel()
    else:
        panel = json.load(open(PANEL, encoding="utf-8"))
        print(f"载入缓存面板：{panel['meta']}")

    h = json.load(open(os.path.join(HERE, "factor_history.json"), encoding="utf-8"))
    tdays, px = fe.px_index(h)
    tidx = {d: i for i, d in enumerate(tdays)}

    fwj = json.load(open(os.path.join(HERE, "factor_weights.json"), encoding="utf-8"))
    W_NOW = {k: float(v or 0) for k, v in (fwj.get("weights") or {}).items()}
    DMAX = {k: float(v) for k, v in (fwj.get("dim_max") or {}).items()}
    EFF = [k for k, v in W_NOW.items() if v > 0]
    print(f"\n现行生效维 {len(EFF)} 个，Σ={sum(W_NOW[k] for k in EFF):.4f}")
    for k in EFF:
        print(f"   {k:20s} w={W_NOW[k]:.4f}  DMAX={DMAX.get(k)}")

    def reweight(fund_w):
        """把 fund 权重设为 fund_w，差额按比例摊给其余维，Σ 恒 = 1。"""
        base = {k: W_NOW[k] for k in EFF if k != "score_fund"}
        s = sum(base.values())
        rest = 1.0 - fund_w
        out = {k: v / s * rest for k, v in base.items()}
        if fund_w > 0:
            out["score_fund"] = fund_w
        return out

    def prod_score(vals, wmap, fund_key="score_fund"):
        """生产线同构：Σ min(v,DMAX)/DMAX×10×w / max(0.5,Σw) × 10
        ⚠️ 缺维从分子分母同时剔除（与生产一致），不是补 0。"""
        num = den = 0.0
        for k, w in wmap.items():
            if w <= 0:
                continue
            src = fund_key if k == "score_fund" else k
            v = vals.get(src)
            if not is_num(v):
                continue
            mx = DMAX.get(k) or 10.0
            num += min(float(v), mx) / mx * 10.0 * w
            den += w
        return (num / max(0.5, den) * 10.0) if den > 0 else None

    def fwd_ret(c, d0, hop):
        m = px.get(c)
        i0 = tidx.get(d0)
        if not m or i0 is None:
            return None
        j = i0 + hop
        if j >= len(tdays):
            return None
        p0, p1 = m.get(d0), m.get(tdays[j])
        if p0 and p1 and p0 > 0:
            return p1 / p0 - 1.0
        return None

    GATE = "score_size_ind"

    def ic_series(wmap, hop, fund_key="score_fund"):
        """返回 {date: ic}，供配对检验。"""
        out = {}
        for d0 in panel["dates"]:
            if tidx[d0] + hop >= len(tdays):
                continue
            cur = panel["rows"][d0]
            if fund_key == "score_fund_old":
                fl = panel["fund_flags"][d0]
                cur = {c: {**v, "score_fund_old": fl.get(c, {}).get("score_fund_old")}
                       for c, v in cur.items()}
            fv, rv = [], []
            for c, vals in cur.items():
                if not is_num(vals.get(GATE)):
                    continue
                v = prod_score(vals, wmap, fund_key)
                if v is None:
                    continue
                fr = fwd_ret(c, d0, hop)
                if fr is None:
                    continue
                fv.append(v)
                rv.append(fr)
            if len(fv) >= 40:
                ic = fe.rank_ic(fv, rv)
                if ic is not None:
                    out[d0] = {"ic": ic, "n": len(fv)}
        return out

    SCHEMES = [
        ("W0_fund0.075_修复后(现行)", 0.0750, "score_fund"),
        ("W0old_fund0.075_旧值(含伪分)", 0.0750, "score_fund_old"),
        ("W1_fund0.0375_减半", 0.0375, "score_fund"),
        ("W2_fund0_归零", 0.0000, "score_fund"),
        ("W3_fund0.150_加倍(回v4.8)", 0.1500, "score_fund"),
    ]

    print("\n" + "=" * 112)
    print("【权重曲线】fund 权重变动对复合分 rank IC 的影响（同一面板 / 同一 GATE / 生产线同构）")
    print("=" * 112)
    print("  %-32s %-4s %8s %8s %7s %7s %7s %7s %8s %s"
          % ("方案", "hop", "IC", "ICIR", "t0", "t3", "t6", "IC>0", "n_med", "Bonf"))
    all_res = {}
    series = {}
    for name, fw_, fk in SCHEMES:
        wm = reweight(fw_)
        for hop in (5, 20):
            s = ic_series(wm, hop, fk)
            if len(s) < 10:
                print("  %-32s h%-3d 无数据" % (name, hop))
                continue
            series[(name, hop)] = s
            ics = [v["ic"] for d0, v in sorted(s.items())]
            ns = [v["n"] for d0, v in sorted(s.items())]
            st = ic_stats(ics)
            ns_sorted = sorted(ns)
            st["n_per_snap_med"] = ns_sorted[len(ns_sorted) // 2]
            st["n_per_snap_min"] = ns_sorted[0]
            st["n_per_snap_max"] = ns_sorted[-1]
            st["n_stock_dates"] = sum(ns)
            all_res["%s|h%d" % (name, hop)] = st
            print("  %-32s h%-3d %+8.4f %+8.3f %+7.2f %+7.2f %+7.2f %6.1f%% %8d %s"
                  % (name, hop, st["ic"], st["icir"] or 0, st["t0"] or 0, st["t3"] or 0,
                     st["t6"] or 0, st["ic_pos_share"] * 100, st["n_per_snap_med"],
                     "✅" if st["pass_bonf"] else "❌"))
        print()

    # ---------- 配对检验：每个方案 vs W0（现行）
    print("=" * 112)
    print("【配对检验】各方案 vs W0_fund0.075_修复后（逐截面配对，NW 滞后 0）")
    print("=" * 112)
    print("  %-32s %-4s %8s %10s %8s %s" % ("对比", "hop", "均值差ΔIC", "NW t", "胜/负", "判定"))
    pairs = {}
    base_name = "W0_fund0.075_修复后(现行)"
    for name, fw_, fk in SCHEMES:
        if name == base_name:
            continue
        for hop in (5, 20):
            a = series.get((base_name, hop))
            b = series.get((name, hop))
            if not a or not b:
                continue
            common = sorted(set(a) & set(b))
            diffs = [b[d]["ic"] - a[d]["ic"] for d in common]
            if not diffs:
                continue
            mu = sum(diffs) / len(diffs)
            t = nw_t(diffs, 0)
            pos = sum(1 for x in diffs if x > 0)
            verdict = "显著改善" if (t is not None and t >= T_BONF) else \
                      ("显著恶化" if (t is not None and t <= -T_BONF) else "不可区分")
            pairs["%s|h%d" % (name, hop)] = {
                "n_pairs": len(diffs), "mean_diff": round(mu, 4),
                "nw_t": round(t, 2) if t is not None else None,
                "wins": pos, "losses": len(diffs) - pos, "verdict": verdict,
            }
            print("  %-32s h%-3d %+9.4f %+9.2f %5d/%-4d %s"
                  % (name, hop, mu, t or 0, pos, len(diffs) - pos, verdict))
        print()

    # ---------- 结论判定
    print("=" * 112)
    print("【判定】fund 权重 0.075 是否应下调（老大裁决条件：修复后 IC 若仍 ≈0）")
    print("=" * 112)
    w0_5 = all_res.get("%s|h5" % base_name) or {}
    w0_20 = all_res.get("%s|h20" % base_name) or {}
    w1_5 = all_res.get("W1_fund0.0375_减半|h5") or {}
    w1_20 = all_res.get("W1_fund0.0375_减半|h20") or {}
    w2_5 = all_res.get("W2_fund0_归零|h5") or {}
    w2_20 = all_res.get("W2_fund0_归零|h20") or {}
    w3_5 = all_res.get("W3_fund0.150_加倍(回v4.8)|h5") or {}
    w3_20 = all_res.get("W3_fund0.150_加倍(回v4.8)|h20") or {}

    verdict = {}
    verdict["current_w0075"] = {"h5": w0_5.get("ic"), "h20": w0_20.get("ic"),
                                "h5_t0": w0_5.get("t0"), "h20_t0": w0_20.get("t0")}
    verdict["fund_single_dim_from_round30"] = {
        "h5_ic": 0.0615, "h5_t0": 4.52, "h20_ic": 0.0951, "h20_t0": 5.37,
        "note": "单维 IC 双过 Bonferroni（含严格阈值 3.9），不是 ≈0",
    }
    verdict["reweight_effects"] = {
        "half_0.0375": {"h5_delta": round((w1_5.get("ic") or 0) - (w0_5.get("ic") or 0), 4),
                        "h20_delta": round((w1_20.get("ic") or 0) - (w0_20.get("ic") or 0), 4)},
        "zero": {"h5_delta": round((w2_5.get("ic") or 0) - (w0_5.get("ic") or 0), 4),
                 "h20_delta": round((w2_20.get("ic") or 0) - (w0_20.get("ic") or 0), 4)},
        "double_0.150": {"h5_delta": round((w3_5.get("ic") or 0) - (w0_5.get("ic") or 0), 4),
                         "h20_delta": round((w3_20.get("ic") or 0) - (w0_20.get("ic") or 0), 4)},
    }
    print(f"  现行 fund=0.075 复合 IC：h5 {w0_5.get('ic'):+.4f} (t{w0_5.get('t0'):+.2f}) · "
          f"h20 {w0_20.get('ic'):+.4f} (t{w0_20.get('t0'):+.2f})")
    print(f"  fund 单维（round30）：   h5 +0.0615 (t+4.52) · h20 +0.0951 (t+5.37) → 显著非零")
    for lbl, k in [("减半→0.0375", "W1_fund0.0375_减半"), ("归零→0", "W2_fund0_归零"),
                   ("加倍→0.150", "W3_fund0.150_加倍(回v4.8)")]:
        a = all_res.get("%s|h5" % k) or {}
        b = all_res.get("%s|h20" % k) or {}
        d5 = (a.get("ic") or 0) - (w0_5.get("ic") or 0)
        d20 = (b.get("ic") or 0) - (w0_20.get("ic") or 0)
        print(f"  {lbl:16s} 复合 IC h5 {a.get('ic'):+.4f} (Δ{d5:+.4f}) · "
              f"h20 {b.get('ic'):+.4f} (Δ{d20:+.4f})")

    with open(os.path.join(HERE, "audit_round30b.json"), "w", encoding="utf-8") as f:
        json.dump({
            "round": "30b",
            "purpose": "fund 权重的边际贡献曲线 + 配对检验 —— 判定 0.075 该不该降",
            "panel_meta": panel["meta"],
            "schemes": all_res,
            "paired_vs_current": pairs,
            "verdict": verdict,
            "thresholds": {"bonf": T_BONF, "bonf_strict": 3.9},
            "caveat": "面板 fund 数值继承 fwd_hist（含当年 ROE 量纲 bug 影响，不可复原）；"
                      "本脚本精确隔离的仅是 P0-2 兜底修复（无估值 → None）",
        }, f, ensure_ascii=False, indent=2)
    print("\n已写出 audit_round30b.json")


if __name__ == "__main__":
    main()
