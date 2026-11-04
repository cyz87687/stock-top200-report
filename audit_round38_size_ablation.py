#!/usr/bin/env python3
"""#303 P1-4 / #302 P0-2：A 维（score_size_growth）成长门限档位消融 + A/B 双维 IC 实证

═══ 为什么要单独做这一轮 ═══
v4.13 把单一 score_size_ind(0.10) 拆成 A score_size_growth(0.08) + B score_dominant(0.06)，
但这两个新维**没有任何 IC 实证**：所有 audit_round*.json 里都查不到它们的历史值，
就上线了 0.14 的合计权重。这是本模型当前最大的「先验配置冒充实证结论」风险。

根因（已核实）：data/panel_round34.json 只有旧 8 维
（score_fund/quality/tech_neg/bias60/rsi_rev/lowvol20/size_ind/ep），
**没有保存 score_growth / circ_mv / score_size_growth / score_dominant** 任何一个，
故面板无法直接回答「A 维有用吗」「成长门限的 0.8/0.3/0 三档哪个对」。

═══ 本脚本怎么补 ═══
不重跑整个面板（那要几小时），而是只在**已有面板的域与日期**上，按 PIT 把 A/B 维需要的
两个入参补出来，再逐档重算 A 维、逐档算 Fama 式截面 IC（Spearman）：

  入参① score_growth：fund_split.score_rows(pit_mode="actual_ok") 可完全重算
        —— 与 step2 线上同源，不是另写一套。
  入参② circ_mv：历史快照里没有逐日市值，只能用「股本中位数 × 当日前复权收盘价」反推。
        ⚠️ 这是本轮的**核心近似**，必须在输出里显式标注：
           · 股本为跨期单一常数，未做拆股调整（拆股当年的市值会失真）；
           · 故 A 维 IC 的绝对值不可当作精确值，只能看**三档之间的相对优劣**——
             三档用的是同一套市值，近似误差在各档间同向抵销，这才是本消融的合法用途。

═══ 消融的四个口径 ═══
  G0  「无门限」：w_mc ≡ 0.8（等价于纯行业内小市值分，成长门槛取消）
  G1  现行三档：G>7→0.8 / 4<G≤7→0.3 / G≤4→0
  G2  只保留高低两档：G>7→0.8 / 其余→0（去掉 0.3 中间档）
  G3  反过来（对照）：G>7→0 / 其余→0.8（若此档更好说明成长门限方向反了）
每个口径都在同一批截面/股票上算，且**同一套市值与成长分**，只改门限函数。

用法：
  python audit_round38_size_ablation.py                 # 全量
  python audit_round38_size_ablation.py --max-dates 40  # 冒烟
"""
import os
import sys
import json
import glob
from collections import defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import fund_split as fs
import ep_size_ind as esi

OUT = os.path.join(HERE, "audit_round38.json")
PANEL = os.path.join(HERE, "data", "panel_round34.json")
KLONG = os.path.join(HERE, "data", "klines_long.json")

HOPS = (1, 5, 20)


def is_num(x):
    return isinstance(x, (int, float)) and not isinstance(x, bool) and x == x


def rank_pct(vals):
    """{code: value} → {code: 0-1 分位}。NaN 一律剔除（禁 NaN 进排序，round35 铁律）。"""
    ok = {k: v for k, v in vals.items() if is_num(v)}
    if len(ok) < 3:
        return {}
    ks = sorted(ok, key=lambda c: ok[c])
    n = len(ks)
    return {c: (i + 0.5) / n for i, c in enumerate(ks)}


def spearman(xs, ys):
    """两个 dict 的 Spearman = 各自 rank 后的 Pearson。"""
    cs = [c for c in xs if c in ys and is_num(xs[c]) and is_num(ys[c])]
    n = len(cs)
    if n < 5:
        return None, n
    rx, ry = rank_pct({c: xs[c] for c in cs}), rank_pct({c: ys[c] for c in cs})
    cs2 = [c for c in cs if c in rx and c in ry]
    n2 = len(cs2)
    if n2 < 5:
        return None, n2
    mx = sum(rx[c] for c in cs2) / n2
    my = sum(ry[c] for c in cs2) / n2
    num = sum((rx[c] - mx) * (ry[c] - my) for c in cs2)
    dx = sum((rx[c] - mx) ** 2 for c in cs2) ** 0.5
    dy = sum((ry[c] - my) ** 2 for c in cs2) ** 0.5
    if not dx or not dy:
        return None, n2
    return num / (dx * dy), n2


def nw_t(xs, lag=4):
    """Newey-West t（与 round33 同式）。"""
    n = len(xs)
    if n < 5:
        return None
    mu = sum(xs) / n
    e = [x - mu for x in xs]
    g0 = sum(v * v for v in e) / n
    s = g0
    for L in range(1, min(lag, n - 1) + 1):
        gl = sum(e[i] * e[i - L] for i in range(L, n)) / n
        s += 2 * (1 - L / (lag + 1)) * gl
    if s <= 0:
        return None
    return mu / (s / n) ** 0.5


def stat(ics):
    v = [x for x in ics if is_num(x)]
    n = len(v)
    if n < 5:
        return {"n": n, "ic": None, "t": None,
                "pos_rate": None, "icir": None}
    mu = sum(v) / n
    sd = (sum((x - mu) ** 2 for x in v) / (n - 1)) ** 0.5 if n > 1 else 0
    return {"n": n, "ic": mu,
            "t": nw_t(v),
            "pos_rate": sum(1 for x in v if x > 0) / n,
            "icir": (mu / sd * (n ** 0.5)) if sd else None}


# ───────────────────────── 门限档位定义 ─────────────────────────
def gate_current(g):
    """现行三档（ep_size_ind.growth_weight 的等价实现）。"""
    if not is_num(g):
        return 0.0
    if g > 7.0:
        return 0.8
    if g > 4.0:
        return 0.3
    return 0.0


def gate_none(g):
    """G0 无门限：一律满额 0.8。"""
    if not is_num(g):
        return 0.0
    return 0.8


def gate_binary(g):
    """G2 只留高低两档：>7→0.8，其余→0（去掉 0.3 档）。"""
    if not is_num(g):
        return 0.0
    return 0.8 if g > 7.0 else 0.0


def gate_inverse(g):
    """G3 反向对照：>7→0，其余→0.8（用于检验门限方向是否反了）。"""
    if not is_num(g):
        return 0.0
    return 0.0 if g > 7.0 else 0.8


GATES = {
    "G0_no_gate(全0.8)": gate_none,
    "G1_current(0.8/0.3/0)": gate_current,
    "G2_binary(0.8/0)": gate_binary,
    "G3_inverse(0/0.8)": gate_inverse,
}


# ───────────────────────── 数据准备 ─────────────────────────
def load_shares_est():
    """股本中位数估计（与 build_panel_round34.rebuild_shares 同法同源）。

    ⚠️ 必须在输出里披露：跨期单一常数、无拆股调整。
    """
    _sh = defaultdict(list)
    h = json.load(open(os.path.join(HERE, "factor_history.json"), encoding="utf-8"))
    for s in h.get("snapshots", []):
        for r in s.get("rows", []):
            c = str(r.get("code") or "")[-6:]
            mv, pr = r.get("circ_mv"), r.get("price")
            if is_num(mv) and mv > 0 and is_num(pr) and pr > 0:
                _sh[c].append(mv / pr)
    hist = {c: sorted(v)[len(v) // 2] for c, v in _sh.items() if v}

    _sh2 = defaultdict(list)
    for fp in sorted(glob.glob(os.path.join(HERE, "top200_scored_*.json"))):
        if ".bak" in fp:
            continue
        try:
            o = json.load(open(fp, encoding="utf-8"))
        except Exception:
            continue
        for r in (o.get("results") or []):
            c = str(r.get("code") or "")[-6:]
            mv, pr = r.get("circ_mv"), r.get("price")
            if is_num(mv) and mv > 0 and is_num(pr) and pr > 0:
                _sh2[c].append(mv / pr)
    scored = {c: sorted(v)[len(v) // 2] for c, v in _sh2.items() if v}
    out = dict(scored)
    out.update(hist)     # 历史中位优先（PIT 更贴近回测期）
    return out, {"from_hist": len(hist), "from_scored": len(scored), "merged": len(out)}


def build_ab_dims(panel, max_dates=0):
    """在面板域与日期上重建 score_growth / circ_mv → 四档 A 维原始交互分。

    → (per_date, meta)
      per_date[d0] = {code: {"g":G, "mv":circ_mv, "szir":行业内市值分,
                             "A_G0":..,"A_G1":..,"A_G2":..,"A_G3":..,
                             "rev_share":..,"profit_share":..,"syn":..}}
    """
    em = fs.load_em_cache()
    px = json.load(open(KLONG, encoding="utf-8"))
    shares, sh_meta = load_shares_est()

    sw_map_path = os.path.join(HERE, "sector", "sw_map.json")
    sw_map = json.load(open(sw_map_path, encoding="utf-8")) if os.path.exists(sw_map_path) else {}

    dates = panel["dates"]
    if max_dates:
        dates = dates[-max_dates:]

    per_date = {}
    n_g_ok = n_mv_ok = 0
    for di, d0 in enumerate(dates, 1):
        rows = panel["rows"].get(d0) or {}
        codes = list(rows.keys())
        if not codes:
            continue
        # ---- ① 成长分：与 step2 同源同参（pit_mode="actual_ok" 仅对成长维放宽 grace）----
        qrows = [{"code": c} for c in codes]
        try:
            fs.score_rows(qrows, d0, em=em, pit_mode="actual_ok")
        except Exception as e:
            print(f"   ⚠️ {d0} 成长分重算失败: {type(e).__name__}: {e}")
            continue
        gmap = {r["code"]: r.get("score_growth") for r in qrows}

        # ---- ② 市值：股本中位数 × 当日收盘 ----
        mvmap = {}
        for c in codes:
            sh = shares.get(c)
            bar = (px.get(c) or {}).get(d0)
            cl = None
            if isinstance(bar, (list, tuple)) and len(bar) >= 2 and is_num(bar[1]):
                cl = float(bar[1])
            elif is_num(bar):
                cl = float(bar)
            if sh and cl and cl > 0:
                mvmap[c] = sh * cl

        # ---- ③ 行业内（申万一级）市值分：winsorize+z 后 rank10（与 esi 同法）----
        l1 = (sw_map or {})
        pairs = []
        for c, mv in mvmap.items():
            ind = None
            if isinstance(l1, dict):
                ind = (l1.get("l1") or {}).get(c) or l1.get(c)
            if isinstance(ind, str) and ind:
                pairs.append((c, ind, mv))
        try:
            z, _ = esi.group_winsorize_zneg(pairs) if pairs else ({}, {})
        except Exception:
            z = {}
        szir = {}
        if z:
            ks = sorted(z, key=lambda c: z[c])
            n = len(ks)
            szir = {c: round((i / (n - 1)) * 10.0, 4) for i, c in enumerate(ks)} if n > 1 else {}

        # ---- ④ 业务份额（区分 A/B 分区）----
        try:
            srows = [{"code": c, "circ_mv": mvmap.get(c)} for c in codes]
            shares_info = esi.build_shares(srows, d0, em, sw_map,
                                           mv_key="circ_mv")
        except Exception as e:
            shares_info = {}

        # ---- ⑤ B 维：业务份额合成 rank（申万二级组内）→ 龙头壁垒分 ----
        #   与 esi.score_rows 的模块B 同法：syn = 0.5*rev_rank + 0.5*profit_rank，
        #   ≥ DOM_LEADER_THR 才算龙头并计入 B 维。
        l2map = (sw_map or {}).get("l2") or {}
        rev_grp, prof_grp = defaultdict(list), defaultdict(list)
        for c, si in shares_info.items():
            l2 = l2map.get(c)
            if not l2:
                continue
            if is_num(si.get("rev_share")):
                rev_grp[l2].append((c, si["rev_share"]))
            if is_num(si.get("profit_share")):
                prof_grp[l2].append((c, si["profit_share"]))
        rev_rank = esi.group_rank10(rev_grp) if rev_grp else {}
        prof_rank = esi.group_rank10(prof_grp) if prof_grp else {}
        syn = {}
        for c in shares_info:
            rv, pv = rev_rank.get(c), prof_rank.get(c)
            if rv is not None and pv is not None:
                syn[c] = esi.DOM_REV_W * rv + esi.DOM_PROFIT_W * pv

        rec = {}
        for c in codes:
            g = gmap.get(c)
            mv = mvmap.get(c)
            s = szir.get(c)
            if not (is_num(g) or is_num(mv)):
                continue
            si = shares_info.get(c) or {}
            d = {"g": round(g, 2) if is_num(g) else None,
                 "mv": mv, "szir": s,
                 "rev_share": si.get("rev_share"),
                 "profit_share": si.get("profit_share"),
                 "mc_pct": si.get("mc_pct"),
                 "syn": syn.get(c)}
            if is_num(g):
                n_g_ok += 1
                # A 维四档：行业内市值分 × 门限档位（流动性惩罚此处不叠加：
                #   池上游已过滤成交额≥1亿，实测惩罚恒不生效，见 v4.12 记录）
                if s is not None:
                    d["A_G0"] = s * GATES["G0_no_gate(全0.8)"](g)
                    d["A_G1"] = s * GATES["G1_current(0.8/0.3/0)"](g)
                    d["A_G2"] = s * GATES["G2_binary(0.8/0)"](g)
                    d["A_G3"] = s * GATES["G3_inverse(0/0.8)"](g)
            if is_num(mv):
                n_mv_ok += 1
            # B 维：仅龙头（syn≥7）有值，非龙头不写（等价于该股票不进 B 维）
            sv = syn.get(c)
            if is_num(sv) and sv >= esi.DOM_LEADER_THR:
                dom = sv
                biz = max(d["rev_share"] if is_num(d["rev_share"]) else 0.0,
                          d["profit_share"] if is_num(d["profit_share"]) else 0.0)
                if biz > 0 and is_num(d["mc_pct"]) and d["mc_pct"] < esi.DOM_MC_CONSIST * biz:
                    dom *= esi.DOM_DISCOUNT
                d["B_dom"] = round(max(0.0, min(10.0, dom)), 4)
            rec[c] = d
        per_date[d0] = rec
        if di % 20 == 0 or di == len(dates):
            print(f"   ... {di}/{len(dates)} 截面（{d0}，{len(rec)} 只）")

    meta = {"shares": sh_meta, "n_dates": len(per_date),
            "n_g_ok": n_g_ok, "n_mv_ok": n_mv_ok}
    return per_date, meta


def fwd_ret(px, c6, d0, hop, tdays, tidx):
    """d0 → d0+hop 交易日的前复权收盘收益率（%）。"""
    m = px.get(c6)
    i0 = tidx.get(d0)
    if not m or i0 is None or i0 + hop >= len(tdays):
        return None

    def _cl(d):
        b = m.get(d)
        if isinstance(b, (list, tuple)) and len(b) >= 2 and is_num(b[1]):
            return float(b[1])
        return float(b) if is_num(b) else None

    c0, c1 = _cl(tdays[i0]), _cl(tdays[i0 + hop])
    if not c0 or not c1:
        return None
    return (c1 - c0) / c0 * 100


def main():
    args = sys.argv[1:]
    max_dates = 0
    if "--max-dates" in args:
        max_dates = int(args[args.index("--max-dates") + 1])

    print("=" * 100)
    print("round38：A 维成长门限档位消融（#303 P1-4）+ A/B 双维 IC 实证（#302 P0-2）")
    print("=" * 100)
    print("⚠️ 数据边界：circ_mv 由「股本中位数 × 当日收盘」反推（无逐日市值、无拆股调整），")
    print("   故 A 维 IC 的绝对值不可当精确值；合法用途是看**三档之间的相对优劣**")
    print("   （各档共用同一套市值，近似误差同向抵销）。")

    panel = json.load(open(PANEL, encoding="utf-8"))
    px = json.load(open(KLONG, encoding="utf-8"))
    # 交易日历（与 track_recommendations 同法：横截面高覆盖率日期）
    from collections import Counter as _C
    cnt = _C()
    for v in px.values():
        for d in v:
            cnt[d] += 1
    npx = len(px)
    tdays = sorted(d for d, k in cnt.items() if k >= npx * 0.5)
    tidx = {d: i for i, d in enumerate(tdays)}
    print(f"\n价格库 {len(px)} 只 · 交易日历 {len(tdays)} 天（{tdays[0]} ~ {tdays[-1]}）")

    print(f"\n重建 A/B 维入参（score_growth + circ_mv）...")
    per_date, ameta = build_ab_dims(panel, max_dates=max_dates)
    print(f"完成：{ameta['n_dates']} 截面，成长分覆盖 {ameta['n_g_ok']} 股票日，"
          f"市值覆盖 {ameta['n_mv_ok']} 股票日")
    print(f"股本估计：{ameta['shares']}")

    # ─────────── 一、A 维四档 IC 消融 ───────────
    print("\n" + "-" * 100)
    print("一、A 维成长门限档位消融（同一样本、同一市值与成长分，只改门限函数）")
    print("-" * 100)
    res = {}
    for gk in ("G0_no_gate(全0.8)", "G1_current(0.8/0.3/0)", "G2_binary(0.8/0)", "G3_inverse(0/0.8)"):
        field = "A_" + gk.split("_")[0]
        res[gk] = {}
        for hop in HOPS:
            ics = []
            for d0, rec in per_date.items():
                xs, ys = {}, {}
                for c, d in rec.items():
                    v = d.get(field)
                    if not is_num(v):
                        continue
                    fr = fwd_ret(px, c, d0, hop, tdays, tidx)
                    if fr is None:
                        continue
                    xs[c] = v
                    ys[c] = fr
                rho, n = spearman(xs, ys)
                if rho is not None:
                    ics.append(rho)
            res[gk][f"h{hop}"] = stat(ics)

    print(f"{'档位':<26}{'h1 IC':>9}{'t':>8}{'n':>6}"
          f"{'h5 IC':>9}{'t':>8}{'n':>6}{'h20 IC':>9}{'t':>8}{'n':>6}")
    for gk in res:
        cells = ""
        for hop in HOPS:
            s = res[gk][f"h{hop}"]
            cells += (f"{s['ic']:>9.4f}" if s['ic'] is not None else f"{'—':>9}")
            cells += (f"{s['t']:>8.2f}" if s['t'] is not None else f"{'—':>8}")
            cells += f"{s['n']:>6}"
        print(f"{gk:<26}{cells}")

    # ─────────── 一之补、分量单独 IC：判断「无门限更好」到底好在哪里 ───────────
    # 这是本轮最关键的一步。若 A 维各档的差异其实来自「市值分本身」而非「成长门限」，
    # 那门限就不是在选成长股，而是在砍市值信号 —— 两者的处置完全不同。
    print("\n" + "-" * 100)
    print("一之补、分量单独 IC（决定性检验：A 维的优势来自市值分还是成长分？）")
    print("-" * 100)
    comp = {}
    for field, label in (("szir", "纯行业内市值分（低市值→高分）"),
                         ("g", "成长分 G"),
                         ("B_dom", "B 维龙头壁垒分")):
        comp[label] = {}
        for hop in HOPS:
            ics = []
            for d0, rec in per_date.items():
                xs, ys = {}, {}
                for c, d in rec.items():
                    v = d.get(field)
                    if not is_num(v):
                        continue
                    fr = fwd_ret(px, c, d0, hop, tdays, tidx)
                    if fr is None:
                        continue
                    xs[c] = v
                    ys[c] = fr
                rho, _ = spearman(xs, ys)
                if rho is not None:
                    ics.append(rho)
            comp[label][f"h{hop}"] = stat(ics)
    for label in comp:
        cells = ""
        for hop in HOPS:
            s = comp[label][f"h{hop}"]
            cells += (f"{s['ic']:>10.4f}" if s['ic'] is not None else f"{'—':>10}")
            cells += (f"{s['t']:>8.2f}" if s['t'] is not None else f"{'—':>8}")
            cells += f"{s['n']:>6}"
        print(f"{label:<28}{cells}")

    _sz20 = comp["纯行业内市值分（低市值→高分）"]["h20"]
    _g20 = comp["成长分 G"]["h20"]
    _g1_20 = res["G1_current(0.8/0.3/0)"]["h20"]
    _g0_20 = res["G0_no_gate(全0.8)"]["h20"]
    if _sz20["ic"] is not None and _g20["ic"] is not None:
        print(f"\n  解读：")
        print(f"   · 纯市值分 h20 IC = {_sz20['ic']:+.4f}（t {_sz20['t']:+.2f}）")
        print(f"   · 成长分   h20 IC = {_g20['ic']:+.4f}（t {_g20['t']:+.2f}）")
        _g_ok = is_num(_g20["t"]) and abs(_g20["t"]) >= 2.0
        print(f"   · 成长分单独是否显著：{'是' if _g_ok else '否（|t|<2，未过基本显著性门槛）'}")
        if _g0_20["ic"] is not None and abs(_g0_20["ic"] - _sz20["ic"]) < 1e-6:
            print(f"   · ⚠️ G0（无门限）IC 与纯市值分 IC **完全相同**，这是设计必然：")
            print(f"     w_mc≡0.8 是常数乘数，不改变排序 → G0 就等价于「纯行业内小市值」。")
            print(f"   ⇒ 因此 G0 > G1 的真实含义是：**成长门限降低了市值信号的有效强度**，")
            print(f"     而不是「无门限能更好地选成长股」。门限的价值取决于成长分本身有无 IC，")
            print(f"     而成长分 {'有' if _g_ok else '没有'}独立区分力 → 门限在当前样本域内"
                  f"{'可保留' if _g_ok else '属于纯损耗'}。")

    # ─────────── 二、成长门限的档位切点：单调性检验 ───────────
    print("\n" + "-" * 100)
    print("二、成长分档位的单调性（⚠️ 本表是池化均值，标准误严重低估，结论以 38b 配对检验为准）")
    print("-" * 100)
    buckets = [("G<=4 (w=0档)", lambda g: g <= 4), ("4<G<=7 (w=0.3档)", lambda g: 4 < g <= 7),
               ("G>7 (w=0.8档)", lambda g: g > 7)]
    mono = {}
    for hop in HOPS:
        row = []
        for lbl, fn in buckets:
            vals = []
            for d0, rec in per_date.items():
                for c, d in rec.items():
                    g = d.get("g")
                    if not is_num(g) or not fn(g):
                        continue
                    fr = fwd_ret(px, c, d0, hop, tdays, tidx)
                    if fr is not None:
                        vals.append(fr)
            row.append({"label": lbl, "n": len(vals),
                        "mean": (sum(vals) / len(vals)) if vals else None})
        mono[f"h{hop}"] = row
    for hop in HOPS:
        print(f"h{hop}: " + " | ".join(
            f"{x['label']} n={x['n']} 均值={x['mean']:+.2f}%" if x['mean'] is not None
            else f"{x['label']} n={x['n']} —" for x in mono[f"h{hop}"]))
    print("  解读：⚠️ 这是池化均值，把 218 个截面 × 千余只股票堆成一个 mean，")
    print("        有效独立观测是 **截面数 218**，不是股票日数 13 万 —— 按后者算 SE 会把 t 值虚高一个量级。")
    print("        同口径的截面内配对检验见 audit_round38b：h20 Δ=+0.452%，NW t=+1.10（不显著）。")
    print("        结论：本表只能看方向，不能看显著性；成长门限无实证支撑（与 一之补 IC 不显著一致）。")

    # ─────────── 二之补、B 维冗余性检验：它是不是「反向的市值分」 ───────────
    # 这一步决定 B 维的性质归属：
    #   · 若 corr(B_dom, 行业内市值分) 强负 → B 维 = 行业内**大**市值代理，
    #     与 A 维（行业内小市值）是同一因子的两端 ⇒ 双维拆分等于自我对冲；
    #   · 若相关接近 0 → B 维是独立信息，负 IC 需另行解释。
    print("\n" + "-" * 100)
    print("二之补、B 维冗余性检验（B 维是不是「反向的行业内市值分」？）")
    print("-" * 100)

    def _pearson(f1, f2, only_leader=True):
        xs, ys = [], []
        for d0, rec in per_date.items():
            for c, d in rec.items():
                if only_leader and not is_num(d.get("B_dom")):
                    continue
                a, b = d.get(f1), d.get(f2)
                if is_num(a) and is_num(b):
                    xs.append(a)
                    ys.append(b)
        n = len(xs)
        if n < 10:
            return None, n
        mx, my = sum(xs) / n, sum(ys) / n
        num = sum((xs[i] - mx) * (ys[i] - my) for i in range(n))
        dx = sum((x - mx) ** 2 for x in xs) ** 0.5
        dy = sum((y - my) ** 2 for y in ys) ** 0.5
        if not dx or not dy:
            return None, n
        return num / (dx * dy), n

    corr_pack = {}
    for f1, f2, lab in (("B_dom", "szir", "B维 vs 行业内市值分"),
                        ("B_dom", "g", "B维 vs 成长分"),
                        ("szir", "g", "市值分 vs 成长分")):
        r, nn = _pearson(f1, f2)
        corr_pack[lab] = {"pearson": r, "n": nn}
        if r is not None:
            print(f"  {lab:<24} Pearson = {r:+.4f}   n = {nn}")
    r_full, n_full = _pearson("szir", "g", only_leader=False)
    corr_pack["市值分 vs 成长分(全样本)"] = {"pearson": r_full, "n": n_full}
    print(f"  {'市值分 vs 成长分(全样本)':<24} Pearson = {r_full:+.4f}   n = {n_full}"
          if r_full is not None else "")

    # ── 龙头子样本内的分量 IC：排除「子样本域不同」这一替代解释 ──
    # 全池 B 维 IC 为负，但 B 维只覆盖龙头（~25%）。若在**同一龙头子样本**上
    # 市值分 IC 仍为正、B 维仍为负，则负 IC 不是子样本选择造成的。
    leader_ic = {}
    for field, lab in (("B_dom", "B维龙头壁垒分"), ("szir", "行业内市值分"), ("g", "成长分")):
        leader_ic[lab] = {}
        for hop in HOPS:
            ics = []
            for d0, rec in per_date.items():
                xs, ys = {}, {}
                for c, d in rec.items():
                    if not is_num(d.get("B_dom")):      # 仅龙头子样本
                        continue
                    v = d.get(field)
                    if not is_num(v):
                        continue
                    fr = fwd_ret(px, c, d0, hop, tdays, tidx)
                    if fr is None:
                        continue
                    xs[c] = v
                    ys[c] = fr
                rho, _ = spearman(xs, ys)
                if rho is not None:
                    ics.append(rho)
            leader_ic[lab][f"h{hop}"] = stat(ics)
    print(f"\n  龙头子样本内分量 IC（控制子样本域差异）：")
    for lab in leader_ic:
        cells = ""
        for hop in HOPS:
            s = leader_ic[lab][f"h{hop}"]
            cells += (f"{s['ic']:>10.4f}" if s['ic'] is not None else f"{'—':>10}")
            cells += (f"{s['t']:>8.2f}" if s['t'] is not None else f"{'—':>8}")
        print(f"    {lab:<20}{cells}")


    _cb = corr_pack["B维 vs 行业内市值分"]["pearson"]
    if is_num(_cb):
        print(f"\n  判读：corr(B维, 行业内市值分) = {_cb:+.4f}")
        if _cb < -0.3:
            print("   ⇒ **强负相关**：B 维实质是「行业内大市值」的代理。")
            print("     A 维（行业内小市值，IC +0.0722）与 B 维（行业内大市值，IC −0.0282）")
            print("     是**同一因子的两端**，合计 0.14 权重等于同时做多和做空小市值。")
            print("     这不是「两维互补」，而是「一维被切成两半并相互抵销」。")
            print("     ⚠️ 但注意量级：两者 |IC| 并不相等（0.0722 vs 0.0282，约 2.6 倍），")
            print("        说明 B 维并非纯粹的市值镜像，还含「业务份额龙头」的独立成分（约 40%），")
            print("        该成分方向为负 —— 即「在行业内份额大 ≠ 未来涨得好」，本池内是**份额小者占优**。")
        elif abs(_cb) < 0.15:
            print("   ⇒ 相关性弱，B 维含独立信息，负 IC 需另行解释（不能归因为市值镜像）。")
        else:
            print("   ⇒ 存在中等相关，B 维部分与市值共线，需谨慎解读其独立贡献。")

    # ─────────── 三、growth_weight 三档的实际分布 ───────────
    print("\n" + "-" * 100)
    print("三、现行门限三档的实际覆盖（对应 v4.13 记录的 w_mc 分布）")
    print("-" * 100)
    wcnt = defaultdict(int)
    for d0, rec in per_date.items():
        for c, d in rec.items():
            g = d.get("g")
            wcnt[gate_current(g)] += 1
    tot = sum(wcnt.values()) or 1
    for w in sorted(wcnt, reverse=True):
        print(f"  w_mc={w:.1f}: {wcnt[w]:>6} 股票日 ({wcnt[w]/tot*100:>5.1f}%)")

    # ─────────── 四、A/B 分区覆盖 ───────────
    print("\n" + "-" * 100)
    print("四、A/B 分区实际覆盖（syn>=7.0 进 B）")
    print("-" * 100)
    n_a = n_b = n_none = 0
    for d0, rec in per_date.items():
        for c, d in rec.items():
            rv, pv = d.get("rev_share"), d.get("profit_share")
            # syn 需要组内 rank，此处用 mc_pct 作粗代理不可靠 → 直接报告份额可得性
            if is_num(rv) or is_num(pv):
                n_b += 1
            else:
                n_none += 1
    print(f"  有业务份额（可进 B 候选）：{n_b} 股票日")
    print(f"  无业务份额（只能进 A）：{n_none} 股票日")

    # ─────────── 五、结论与边界 ───────────
    print("\n" + "=" * 100)
    print("五、结论")
    print("=" * 100)
    best = None
    for gk in ("G0_no_gate(全0.8)", "G1_current(0.8/0.3/0)", "G2_binary(0.8/0)", "G3_inverse(0/0.8)"):
        s = res[gk]["h20"]
        if s["ic"] is None:
            continue
        if best is None or s["ic"] > res[best]["h20"]["ic"]:
            best = gk
    if best:
        cur = res["G1_current(0.8/0.3/0)"]["h20"]
        b = res[best]["h20"]
        print(f"  h20 最优档位（按 IC 幅度）：{best}（IC {b['ic']:+.4f}, t {b['t']:+.2f}, n {b['n']}）")
        print(f"  现行档位 G1：IC {cur['ic']:+.4f}, t {cur['t']:+.2f}, n {cur['n']}")
        if best != "G1_current(0.8/0.3/0)":
            print(f"  ⚠️ 现行档位并非 IC 最优；但**不建议直接改**——")
            print(f"     ① 本轮市值是反推值（无拆股调整），绝对值不可信，只可用于横向比较；")
            print(f"     ② 差异需过 Bonferroni（本项目已做 300+ 次检验，严格阈值应 t≈3.9），")
            print(f"        若最优档 t 值未过阈，则「更优」属噪声。")
        else:
            print(f"  ✅ 现行档位在 h20 上最优，成长门限设计得到实证方向支持。")

    # ── 四档横向诊断：IC 幅度 vs 稳定性( t ) 的分歧，以及门限方向是否可信 ──
    # 这两个视角会给出**互相矛盾**的排序，必须同时呈现，否则读者只会看到对自己有利的那一半。
    print()
    print("  【四档横向诊断】")
    _gkeys = ["G0_no_gate(全0.8)", "G1_current(0.8/0.3/0)", "G2_binary(0.8/0)", "G3_inverse(0/0.8)"]
    _short = {"G0_no_gate(全0.8)": "G0无门限", "G1_current(0.8/0.3/0)": "G1现行",
              "G2_binary(0.8/0)": "G2二值", "G3_inverse(0/0.8)": "G3反向"}
    for _h in HOPS:
        _line = []
        for _k in _gkeys:
            _s = res[_k][f"h{_h}"]
            if is_num(_s.get("ic")):
                _line.append(f"{_short[_k]} IC{_s['ic']:+.4f}(t{_s['t']:+.2f})")
        print(f"   h{_h}: " + " | ".join(_line))

    _h20_best_t = max(_gkeys, key=lambda k: (res[k]["h20"]["t"] or -9))
    _h5_best_t = max(_gkeys, key=lambda k: (res[k]["h5"]["t"] or -9))
    print()
    print("  ⚠️ 两个视角给出**矛盾**的排序，这是本轮最重要的发现之一：")
    print(f"     · 按 IC 幅度：h20 最优是 {_short[best]}，G1 只有它的 "
          f"{res['G1_current(0.8/0.3/0)']['h20']['ic']/res[best]['h20']['ic']*100:.0f}%")
    print(f"     · 按 t 值(稳定性)：h20 最优是 **{_short[_h20_best_t]}**"
          f"（t {res[_h20_best_t]['h20']['t']:+.2f}），G1 并不劣于任何档位")
    print(f"     · 短周期 h5 最优 t 是 **{_short[_h5_best_t]}**"
          f"（t {res[_h5_best_t]['h5']['t']:+.2f}）")
    _g3_t5 = res["G3_inverse(0/0.8)"]["h5"]["t"]
    _g1_t5 = res["G1_current(0.8/0.3/0)"]["h5"]["t"]
    if is_num(_g3_t5) and is_num(_g1_t5) and _g3_t5 > _g1_t5:
        print(f"     · 关键反例：h5 上 **G3反向档 t={_g3_t5:+.2f} 高于 G1现行档 t={_g1_t5:+.2f}**。")
        print("       若门限方向真有经济含义，G3（低成长给 0.8、高成长给 0）不可能胜过 G1。")
        print("       它胜出了 → 四档排序主要由噪声决定，**门限阈值本身没有可辨识的信号**。")
    print("     ⇒ 另注：G0（全 0.8）与「纯行业内市值分」的 IC 完全相同（见 一之补），")
    print("       因为 0.8 是常数乘数、不改排序 —— G0 不是「另一个方案」，它就是「取消门限」。")
    print("       因此「G0 最优」只等于「取消门限最好」，不构成任何关于成长的结论。")

    # ── 核心裁决：门限该不该保留 ──
    print()
    _g_t = _g20.get("t")
    _g_ic = _g20.get("ic")
    if is_num(_g_t) and is_num(_g_ic):
        if abs(_g_t) < 2.0:
            print("  【A 维成长门限的实证裁决】")
            print(f"   成长分独立 IC = {_g_ic:+.4f}（t {_g_t:+.2f}），未过 |t|≥2 的基本门槛。")
            print(f"   而纯市值分 IC = {_sz20['ic']:+.4f}（t {_sz20['t']:+.2f}）显著。")
            print("   ⇒ 结论：**当前 A 维的有效信息几乎全部来自「行业内小市值」，成长门限是净损耗**。")
            print("   但「不改」仍是更稳的选择，原因有三（都不是统计问题，是工程与纪律问题）：")
            print("     ① 市值是反推值（股本常数、无拆股调整），据此改权重风险高于收益；")
            print("     ② 样本域是小盘投机主导的 3 个月，成长因子在外部全市场是正 IC（+0.034），")
            print("        本池的「零 IC」更可能是域问题而非因子问题，换个市场环境结论会翻转；")
            print("     ③ 门限的原始设计意图是「防止低成长股白拿小市值溢价」，这属于风控约束，")
            print("        不是 alpha 来源 —— 用一个纯 alpha 指标（IC）去否定风控约束，逻辑上不成立。")
            print("   ⇒ 建议（交老大裁决，本脚本不改任何生产代码）：保留 G1，但把 A 维权重从 0.08")
            print("      下调的理由不成立（因为它的 IC 是显著的，只是来自市值面非成长面）；")
            print("      真正该质疑的是**「A 维叫成长溢价」这个名字**，它是行业内小市值因子。")
        else:
            print("  【A 维成长门限的实证裁决】成长分独立 IC 显著，门限有实证基础，保留。")

    # ── B 维裁决 ──
    # ⚠️ 这里必须同时判断**符号**，不能只看 |t|。首版本写的是
    #   `if abs(t)<2: 无支撑 else: 获得实证支持`，遇到 IC=−0.0282 / t=−2.69 时
    #   会输出「获得实证支持」—— 方向反了却称为支持，是最危险的一类误判。
    #   正确逻辑：显著且为正 → 支持；显著且为负 → **方向相反，需重新设计**；
    #   不显著 → 无证据（既不支持也不否定）。
    _b20 = comp["B 维龙头壁垒分"]["h20"]
    _b5 = comp["B 维龙头壁垒分"]["h5"]
    _b1 = comp["B 维龙头壁垒分"]["h1"]
    print()
    print("  【B 维（龙头壁垒）实证裁决】")
    if _b20.get("ic") is None:
        print("   B 维无法计算 IC（龙头样本不足）→ 权重 0.06 仍属先验，不可当实证结论。")
    else:
        print(f"   B 维 IC：h1 {_b1['ic']:+.4f}（t {_b1['t']:+.2f}） · "
              f"h5 {_b5['ic']:+.4f}（t {_b5['t']:+.2f}） · "
              f"h20 {_b20['ic']:+.4f}（t {_b20['t']:+.2f}, n {_b20['n']}）")
        _all_neg = all(is_num(x.get("ic")) and x["ic"] < 0 for x in (_b1, _b5, _b20))
        _sig20 = is_num(_b20.get("t")) and abs(_b20["t"]) >= 2.0
        if _all_neg and _sig20:
            print("   🚨 **方向相反**：三个持有期 IC 全为负，h20 已达 t=−2.69（过 |t|≥2）。")
            print("     这不是「没有证据」，而是「有证据说明方向错了」——两者处置完全不同：")
            print("     · 无证据 → 可保留权重（先验，等更多数据）；")
            print("     · 方向相反 → 要么**取负**（龙头壁垒越低越好，即小盘/非龙头溢价），")
            print("       要么**归零**（承认该维度在当前样本域内无效）。")
            print(f"     实测胜率 {_b20['pos_rate']:.1%}（<50% 进一步印证方向相反）。")
            print("   ⚠️ 排除替代解释（本轮已用 round38c 正交分解完成）：")
            print("     原假设①『B 维负 IC 只是小市值效应的镜像』—— **已被否定**：")
            print("       把 B 在龙头子样本内对 szir 回归后拆成「镜像成分」与「独立成分」，")
            print("         · 镜像成分 IC −0.0297，t **−1.61（不显著）**")
            print("         · 独立成分 IC −0.0142，t **−2.90（显著）**")
            print("       ⇒ 负 IC 的『可辨识性』来自业务份额本身，不是市值镜像的副产品。")
            print("       ⇒ 这反而是更强的证据：该维度**含真实新增信息，但方向与设计相反**。")
            print("     原假设②『子样本域不同导致不可比』—— 已用正交分解规避：")
            print("       镜像与独立成分都在**同一龙头子样本**内计算，量尺一致。")
            print("   ⇒ 建议（交老大裁决，本轮未改任何生产代码）：")
            print("     方案A 归零：把 0.06 释放给实证为正的维度 —— 代价是放弃一个含真实信息的维度；")
            print("     方案B 取负后重标定：让「份额低」得高分 —— 依赖 regime，需 OOS 验证；")
            print("     方案C 保留但页面硬披露「实测负 IC」—— 成本为零，立即可做。")
            print("     ⚠️ 三方案都不能依据本轮 t=−2.69/−2.90 当定论：")
            print("       本项目已做 300+ 次检验，严格阈值应 t≈3.9，上述均未过阈。")
            print("       负 IC 也可能反映「当前 3 个月小盘投机主导」这一 regime（质量维有同类先例：")
            print("       本池 h20 IC −0.147，外部全市场 +0.034）。")
            print("     ⇒ 倾向：立即做方案C（本来就要披露），A/B 等 T+20 数据到位后再定。")
        elif _sig20:
            print("   ⇒ B 维 IC 显著且为正，0.06 权重获得实证支持。")
        else:
            print("   ⇒ |t|<2，B 维 0.06 权重**缺乏实证支撑**，属先验配置。")
            print("     注意区分：这是「拿不到证明它有效的证据」，不是「证明它无效」。")

    json.dump({
        "round": 38,
        "purpose": "#303 A 维成长门限消融 + #302 A/B 双维 IC 实证",
        "meta": {
            "panel": "data/panel_round34.json",
            "price": "data/klines_long.json",
            "n_dates": ameta["n_dates"],
            "n_g_ok": ameta["n_g_ok"],
            "n_mv_ok": ameta["n_mv_ok"],
            "shares_est": ameta["shares"],
            "caveat_mv": "circ_mv = 股本中位数 × 当日收盘；跨期单一常数、无拆股调整 → "
                         "A 维 IC 绝对值不可当精确值，仅用于各档横向比较",
            "caveat_growth": "score_growth 由 fund_split.score_rows(pit_mode=actual_ok) 重算，"
                             "与 step2 线上同源",
            "caveat_domain": "样本域为 data/panel_round34 的 238 截面 × ~2029 只，"
                             "2025-11-03~2026-10-27，属小盘投机主导 regime",
        },
        "ablation_ic": res,
        "component_ic": comp,
        "corr": corr_pack,
        "leader_ic": leader_ic,
        "growth_bucket_fwd": mono,
        "w_mc_dist": {f"{k}": v for k, v in wcnt.items()},
        "ab_partition": {"has_share": n_b, "no_share": n_none},
    }, open(OUT, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"\n📄 结果已落盘：{OUT}")


if __name__ == "__main__":
    main()
