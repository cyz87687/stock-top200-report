"""build_panel_long.py —— 构建 2020 起长面板（改用 klines_2020.npz）。

与 backfill_panel_round123.py 的本质区别：
    round123 是「从 panel_round66 挪分数」—— 只有分数、没有原始字段，
    因此无法重算 ep / 无法做可交易性过滤 / 无法算容量（见 round124 审计）。
    本脚本**从原始行情 + 财务重算**，并把原始字段一并落盘。

维度口径（严格对齐 step2_stockscorer_v2.py，避免 round123b 那种「回填时用旧口径」）：
    score_bias60 / score_rsi_rev  → 池内分位连续（reversal_rank，DIM_MAX=5）
    score_tech_neg               → 偏离 20 日高点 + 缺口，DIM_MAX=10
    score_lowvol20               → 20 日已实现波动，DIM_MAX=10
    score_size_growth            → 流通市值分位（小市值优先），DIM_MAX=10
    score_ep                     → 1/PE 分位（用流通市值/净利推），DIM_MAX=10
    score_quality / score_growth → 财务 PIT（法定披露截止日截断），DIM_MAX=10
    score_dominant               → 业务份额组内前 30% 有值，DIM_MAX=10
    score_fund                   → 主力净流入，**历史不可回填** → 全 None（显式）

输出原始字段（round124 P0-B 缺口）：
    price / circ_mv / total_mv / amount / turnover_ratio / pe / pb

⚠️ 内存纪律：本脚本逐日累加、边算边写，不把整表留内存
    （5945 只 × 1655 日实测 PE 级对象会 SIGTERM）。

用法：
    python build_panel_long.py --start 2020-01-02 --end 2026-10-31
    python build_panel_long.py --limit-days 60        # 试跑
"""
import argparse
import json
import os
import sys
import time

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")
NPZ = os.path.join(DATA, "klines_2020.npz")
OUT = os.path.join(DATA, "panel_round124_long.json")
FIN_CACHE = os.path.join(DATA, "fin_cache_em")

sys.path.insert(0, HERE)

DIM_MAX_REV = 5.0      # bias60 / rsi_rev 量程（与 factor_weights.json dim_max 一致）
DIM_MAX_STD = 10.0     # 其余维度


# ═══════════════════════════════════════════════════════════════════════════
# round125 P0：score_dominant 回填（原实现硬编码 None，注释是**事实错误**）
# ═══════════════════════════════════════════════════════════════════════════
# 原注释（本文件 L325 旧版）："需业务份额数据，历史不可得 → 显式 None"
# 实测反驳：业务份额 = 个股 PIT TTM 营收/扣非净利 ÷ 申万二级同行业合计，
#   两个输入都**可历史重建**：
#     · 财报：data/fin_cache_em 每只 80 条指标 × **25 个报告期**
#       （最老 2018-12-31，营业总收入/扣非净利润 25/25 期全有值）
#     · 行业：sector/sw_map.json 静态映射（行业归属不随时间变）
#     · 市值：circ_mv 已由换手率反推，面板里本来就有
#   所以 0.10 权重被当成「盲区」是**脚本没算**，不是数据拿不到。
#   ⇒ 闸门覆盖率因此从 70% 提升到 90%，只剩 fund(0.20) 是真不可重建。
#
# ⚠️ 与 fund 的区别（不要一起处理）：
#   score_fund = 主力净流入，依赖**逐日资金流快照**，历史接口不提供 →
#   多家脚本一致声明不可重建（backfill_panel_round123.py:29/282 等）。
#   这一项确实是真盲区，只能披露、不能回填。
def _build_shares_for_day(day_rows, asof, em, sw_map, mv_key="circ_mv"):
    """当日截面的业务份额 + 龙头分。返回 {code6: score_dominant}。

    直接复用 ep_size_ind 的两个函数，**不重写算法** —— 任何口径分叉都会让
    面板里的 dominant 与生产 step2 算出的 dominant 不可比。
    """
    try:
        import ep_size_ind as EP
    except Exception as e:
        print(f"  ⚠️ 无法导入 ep_size_ind（{e}）→ dominant 保持 None")
        return {}
    try:
        shares = EP.build_shares(day_rows, asof, em, sw_map, mv_key=mv_key)
    except Exception as e:
        print(f"  ⚠️ build_shares 失败（{asof}: {e}）→ dominant 保持 None")
        return {}
    # 合成份额 rank（与 ep_size_ind.score_rows 内 syn 同口径：
    #   0.5×营收份额 rank + 0.5×利润份额 rank，再 ±10 分制）
    rev_r, prof_r = {}, {}
    for c6, s in shares.items():
        rs, ps = s.get("rev_share"), s.get("profit_share")
        if isinstance(rs, (int, float)):
            rev_r[c6] = rs
        if isinstance(ps, (int, float)):
            prof_r[c6] = ps
    def _pct_rank(d):
        if not d:
            return {}
        ks = sorted(d, key=lambda k: d[k])
        n = len(ks)
        return {k: (i / max(1, n - 1)) if n > 1 else 0.5
                for i, k in enumerate(ks)}
    rr, pr = _pct_rank(rev_r), _pct_rank(prof_r)
    out = {}
    for c6 in shares:
        a, b = rr.get(c6), pr.get(c6)
        if a is None and b is None:
            continue
        vals = [x for x in (a, b) if x is not None]
        syn = sum(vals) / len(vals)          # 0~1
        # 归一化：混合份额 rank 映射到 0-10（与 rank10 单维口径同量程）
        dom = syn * DIM_MAX_STD
        # 市值一致性校验 + 流动性惩罚（参数取 ep_size_ind 的模块常量，
        # 避免在此处复制魔数 → 未来调参只改一处）
        biz = max(shares[c6].get("rev_share") or 0.0,
                  shares[c6].get("profit_share") or 0.0)
        mc = shares[c6].get("mc_pct")
        _cons = getattr(EP, "DOM_MC_CONSIST", None)
        _disc = getattr(EP, "DOM_DISCOUNT", 1.0)
        if _cons is not None and biz > 0 and isinstance(mc, (int, float)) \
                and mc < _cons * biz:
            dom *= _disc
        out[c6] = round(max(0.0, min(10.0, dom)), 2)
    return out

# 法定披露截止日（唯一口径，与 growth_factor_b.py 一致）
_PERIOD_DEADLINE = {"0331": (4, 30), "0630": (8, 31),
                    "0930": (10, 31), "1231": (4, 30)}


def period_deadline(period):
    try:
        y, m = int(str(period)[:4]), str(period)[4:]
    except (ValueError, TypeError):
        return None
    md = _PERIOD_DEADLINE.get(m)
    if not md:
        return None
    yy = y + 1 if m == "1231" else y
    return f"{yy:04d}{md[0]:02d}{md[1]:02d}"


def load_fin(code):
    """读东财财务缓存 → {报告期: {指标: 值}}。"""
    fp = os.path.join(FIN_CACHE, f"{code}.json")
    if not os.path.exists(fp):
        return {}
    try:
        with open(fp, encoding="utf-8") as f:
            blob = json.load(f)
    except Exception:
        return {}
    out = {}
    for rec in blob.get("records") or []:
        ind = rec.get("指标")
        if not ind:
            continue
        for k, v in rec.items():
            if isinstance(k, str) and len(k) == 8 and k.isdigit():
                if isinstance(v, (int, float)) and v == v:
                    out.setdefault(k, {})[ind] = float(v)
    return out


def pit_pick(fin, asof, fields):
    """按法定披露截止日截断，返回最近一期有值的 fields。"""
    avail = [p for p in fin if period_deadline(p) and period_deadline(p) <= asof]
    if not avail:
        return {}
    for p in sorted(avail, reverse=True)[:3]:
        d = fin[p]
        if all(f in d for f in fields):
            return d
    return fin[max(avail)] if avail else {}


def rank5(vals, higher_better):
    """整池 rank → pct → (可选反转) → ×DIM_MAX_REV。与 step2.reversal_rank 同口径。"""
    items = [(c, v) for c, v in vals.items()
             if isinstance(v, (int, float)) and v == v]
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
        p = pct[k] if higher_better else (1.0 - pct[k])
        out[items[k][0]] = round(p * DIM_MAX_REV, 2)
    return out


def rank10(vals, higher_better):
    """同 rank5，量程 DIM_MAX_STD。"""
    items = [(c, v) for c, v in vals.items()
             if isinstance(v, (int, float)) and v == v]
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
        p = pct[k] if higher_better else (1.0 - pct[k])
        out[items[k][0]] = round(p * DIM_MAX_STD, 2)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2020-01-02")
    ap.add_argument("--end", default="2026-10-31")
    ap.add_argument("--limit-days", type=int, default=0)
    ap.add_argument("--day-offset", type=int, default=0,
                    help="跳过前 N 个交易日（分段跑用，本机长任务会被 SIGTERM）")
    ap.add_argument("--out", default=OUT)
    args = ap.parse_args()

    t00 = time.time()
    z = np.load(NPZ, allow_pickle=False)
    X = z["X"]
    dates = [str(d) for d in z["dates"]]
    codes = [str(c) for c in z["codes"]]
    cidx = {c: i for i, c in enumerate(codes)}
    c_arr = X[:, :, 1]
    to_arr = X[:, :, 6]
    v_arr = X[:, :, 4]
    amt_arr = X[:, :, 5]

    use_days = [i for i, d in enumerate(dates) if args.start <= d <= args.end]
    if args.day_offset:
        use_days = use_days[args.day_offset:]
    if args.limit_days:
        use_days = use_days[:args.limit_days]
    print(f"面板区间 {dates[use_days[0]]} ~ {dates[use_days[-1]]}  "
          f"{len(use_days)} 个交易日 × {len(codes)} 只")

    # ── 预载财务（一次性，后续按 as_of 截断） ──
    print("预载财务缓存 ...")
    fin = {}
    for c in codes:
        f = load_fin(c)
        if f:
            fin[c] = f
    print(f"  有财务数据 {len(fin)}/{len(codes)} 只")

    # round125：行业映射（dominant 回填要用；静态文件，不随时间变）
    _SW_MAP = {}
    try:
        with open(os.path.join(HERE, "sector", "sw_map.json"), encoding="utf-8") as _f:
            _SW_MAP = json.load(_f)
        print(f"  行业映射 {len((_SW_MAP.get('l2') or {}))} 只")
    except Exception as _e:
        print(f"  ⚠️ sw_map.json 读取失败（{_e}）→ dominant 无法回填")

    rows_out = {}
    dim_stat = {}
    t0 = time.time()

    for n, di in enumerate(use_days):
        d = dates[di]
        asof = d.replace("-", "")

        # 当日截面：收盘价 / 换手率 / 成交额
        px, tv, amt, sh = {}, {}, {}, {}
        for c in codes:
            i = cidx[c]
            p = c_arr[i, di]
            if not (np.isfinite(p) and p > 0):
                continue
            px[c] = float(p)
            t = to_arr[i, di]
            if np.isfinite(t) and t > 0:
                tv[c] = float(t)
            a = amt_arr[i, di]
            if np.isfinite(a) and a > 0:
                amt[c] = float(a)
            vv = v_arr[i, di]
            if np.isfinite(t) and t > 0 and np.isfinite(vv) and vv > 0:
                sh[c] = float(vv) / (float(t) / 100.0)   # 流通股本

        # ── 流通市值 ──
        cmv = {c: sh[c] * px[c] / 1e8 for c in sh}      # 亿元

        # ── 技术维：需要历史窗口 ──
        bias_raw, rsi_raw, lowvol_raw, techneg_raw = {}, {}, {}, {}
        if di >= 60:
            for c in px:
                i = cidx[c]
                # ⚠️ 不能要求 60 日**全** finite：停牌 1 天就整只作废，
                #    实测这样 bias60 有值截面 = 0/21（全灭）。
                #    改为「60 日窗口内 ≥50 个有效价」才计算，用有效价求均值。
                win = c_arr[i, di - 59:di + 1]
                wv = win[np.isfinite(win)]
                if len(wv) >= 50:
                    ma = float(wv.mean())
                    if ma > 0:
                        bias_raw[c] = (px[c] - ma) / ma * 100.0
                # 20 日已实现波动（同样放宽到 ≥16 个有效价）
                w20 = c_arr[i, di - 19:di + 1]
                w20v = w20[np.isfinite(w20)]
                if len(w20v) >= 16:
                    rr = np.diff(w20v) / w20v[:-1]
                    rr = rr[np.isfinite(rr)]
                    if len(rr) >= 10:
                        lowvol_raw[c] = float(np.std(rr) * 100.0)
                # 14 日 RSI（需连续 15 个有效价，RSI 对连续性要求高）
                w15 = c_arr[i, di - 14:di + 1]
                if np.all(np.isfinite(w15)) and len(w15) == 15:
                    ch = np.diff(w15)
                    g = float(ch[ch > 0].sum()) / 14.0
                    ls = float(-ch[ch < 0].sum()) / 14.0
                    rsi_raw[c] = 100.0 if ls == 0 else 100.0 - 100.0 / (1.0 + g / ls)
                # 技术面负面：偏离 20 日高点的距离
                if len(w20v) >= 16:
                    hi = float(w20v.max())
                    if hi > 0:
                        techneg_raw[c] = (px[c] / hi - 1.0) * 100.0

        # ── 分位化 ──
        s_bias = rank5(bias_raw, higher_better=False)     # 越低越好（超跌）
        s_rsi = rank5(rsi_raw, higher_better=False)       # 越低越好
        s_lowvol = rank10(lowvol_raw, higher_better=False)
        s_techneg = rank10(techneg_raw, higher_better=True)  # 越接近高点越好
        s_sizeg = rank10(cmv, higher_better=False)        # 小市值优先

        # ⚠️ score_ep 必须是「盈利收益率 = 净利润/流通市值」，不是 1/流通市值（本轮血案）
        #    旧实现写的是 rank10({c: 1.0/cmv[c]}, higher_better=True)，
        #    而 rank10(cmv, higher_better=False) 与 rank10(1/cmv, True) 在秩上**完全等价**
        #    → 两个「独立维度」实际是同一列。
        #    实测：score_size_growth 与 score_ep 的相关数 ρ=0.99999999，
        #    分维度 IC 也完全相同（都是 +0.0337 / NWt=2.27）→ 0.20+0.10 权重被重复计数。
        #    正确口径：EP = 归母净利润(最新一期,PIT) / 流通市值 → 大 EP（便宜）得分高。
        ep_raw = {}
        for c in cmv:
            f = fin.get(c)
            if not f or cmv[c] <= 0:
                continue
            dnp = pit_pick(f, asof, ["归母净利润", "净利润"])
            if not dnp:
                continue
            np_ = None
            for key in ("归母净利润", "净利润"):
                v = dnp.get(key)
                if isinstance(v, (int, float)) and v == v:
                    np_ = float(v)
                    break
            if np_ is None:
                continue
            # 净利润单位为元，cmv 单位为亿元 → 统一到亿元
            ep_raw[c] = (np_ / 1e8) / cmv[c]
        # 只对盈利为正的样本排序（亏损股 EP 无经济意义，显式剔除而非给低分）
        ep_pos = {c: v for c, v in ep_raw.items() if v > 0}
        s_ep = rank10(ep_pos, higher_better=True)

        # ── 财务维（PIT） ──
        # ⚠️ 指标名必须与 data/fin_cache_em 的真实键一致（实测 70 个指标）。
        #    踩过的坑：写成"归属母公司股东的净利润同比增长率"/"销售毛利率"
        #    → 全部匹配不上 → growth 静默全 None（有值截面 0/21）。
        #    真实键名：'归属母公司净利润增长率'、'毛利率'（无"销售"前缀）。
        q_raw, g_raw = {}, {}
        for c in px:
            f = fin.get(c)
            if not f:
                continue
            dq = pit_pick(f, asof, ["净资产收益率(ROE)"])
            if dq:
                roe = dq.get("净资产收益率(ROE)")
                if isinstance(roe, (int, float)) and roe == roe:
                    q_raw[c] = float(roe)
            dg = pit_pick(f, asof, ["归属母公司净利润增长率"])
            if dg:
                gg = dg.get("归属母公司净利润增长率")
                if isinstance(gg, (int, float)) and gg == gg:
                    g_raw[c] = float(gg)
        s_q = rank10(q_raw, higher_better=True)
        s_g = rank10(g_raw, higher_better=True)

        # ── round125 P0：dominant 回填（原为硬编码 None）──
        #   业务份额只需财报+行业映射，都可历史重建 → 真的算得出来。
        #   ⚠️ 必须整池一起算（份额分母=同行业本池合计），不能在单只上算。
        _day_rows = [{"code": c, "circ_mv": cmv.get(c),
                      "price": px.get(c)} for c in px]
        s_dom = _build_shares_for_day(_day_rows, asof, fin, _SW_MAP)

        # ── 组装当日行 ──
        day = {}
        for c in px:
            rec = {
                "price": round(px[c], 3),
                "circ_mv": round(cmv.get(c, 0.0), 2) if c in cmv else None,
                "amount": round(amt[c] / 1e4, 1) if c in amt else None,   # 万元
                "turnover_ratio": round(tv[c], 3) if c in tv else None,
                "score_bias60": s_bias.get(c),
                "score_rsi_rev": s_rsi.get(c),
                "score_lowvol20": s_lowvol.get(c),
                "score_tech_neg": s_techneg.get(c),
                "score_size_growth": s_sizeg.get(c),
                "score_ep": s_ep.get(c),
                "score_quality": s_q.get(c),
                "score_growth": s_g.get(c),
                "score_dominant": s_dom.get(c),   # round125：回填（原 None）
                "score_fund": None,          # 主力净流入无历史快照 → 真不可重建，显式 None
            }
            day[c] = rec

        rows_out[d] = day
        for k in ("score_bias60", "score_rsi_rev", "score_lowvol20",
                  "score_tech_neg", "score_size_growth", "score_ep",
                  "score_quality", "score_growth", "score_dominant"):
            v = sum(1 for r in day.values() if r.get(k) is not None)
            dim_stat.setdefault(k, []).append(v)

        if (n + 1) % 100 == 0:
            print(f"  {n+1}/{len(use_days)}  {d}  股票 {len(day)}  "
                  f"耗时 {time.time()-t0:.0f}s")

    keep = ["price", "circ_mv", "amount", "turnover_ratio",
            "score_bias60", "score_rsi_rev", "score_lowvol20", "score_tech_neg",
            "score_size_growth", "score_ep", "score_quality", "score_growth",
            "score_dominant", "score_fund"]
    panel = {
        "version": "round124-long",
        "meta": {
            "built_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "src": "data/klines_2020.npz (2020-01-02~2026-11-03) + data/fin_cache_em",
            "purpose": ("把样本从 238 个交易日扩到 ~1600 个，使 h=20 的"
                        "有效独立观测从 ≈11 提升到 ≈80，t 值才具备信息量"),
            "date_range": [dates[use_days[0]], dates[use_days[-1]]],
            "n_dates": len(use_days),
            "domain_size": len(codes),
            "raw_fields_included": ["price", "circ_mv", "amount", "turnover_ratio"],
            "circ_mv_source": "换手率反推：shares = vol_shares/(turnover/100)",
            "explicit_none": {
                "score_fund": "主力净流入无历史快照，不可回填；全 None 显式声明",
                "score_dominant": ("round125 起**已回填**（原为 None）。"
                                   "业务份额=个股 PIT TTM 营收/扣非净利 ÷ 申万二级同行业合计，"
                                   "财报 25 个报告期(2018-12~2026-09)全有值 + sw_map 静态映射 "
                                   "→ 可历史重建。与生产 step2 共用 ep_size_ind.build_shares，"
                                   "禁在此重写算法。"),
            },
            "dim_max": {"score_bias60": DIM_MAX_REV, "score_rsi_rev": DIM_MAX_REV,
                        "其余": DIM_MAX_STD},
            "pit": {"mode": "strict", "rule": _PERIOD_DEADLINE,
                    "note": "报告期按法定披露截止日截断，1231 算次年 4/30"},
        },
        "dates": [dates[i] for i in use_days],
        "domain": codes,
        "rows": rows_out,
    }
    tmp = args.out + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(panel, f, ensure_ascii=False)
    os.replace(tmp, args.out)
    sz = os.path.getsize(args.out) / 1e6
    print(f"写出 {args.out}  {sz:.1f} MB  耗时 {time.time()-t00:.0f}s")

    print("\n=== 逐维有值截面数（≥200 视为可用） ===")
    for k, arr in dim_stat.items():
        ok = sum(1 for x in arr if x >= 200)
        print(f"  {k:<20} {ok}/{len(arr)}  最低 {min(arr)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
