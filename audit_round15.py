#!/usr/bin/env python3
"""Round15：多因子组合的持仓周期回测 —— 「持有多久」用数据回答，不拍脑袋。

老大 2026-10-25 第 3 问：
  「该组合的持仓时间推荐是多久，需要用数据来说话」

数据源与边界（必须随结论一起披露）：
  数据：factor_history.json 的 85 个日频截面（2026-06-26 ~ 2026-10-23）+ 143 日价格库
        （1012 只，含收盘价 px 与开盘价 opens）。
  ⚠️ 边界①：历史截面只存 16 个评分字段，**没有 tech（均线/RSI/支撑阻力）**，
     也没有 sector_l2。因此无法复现选股层的两个环节：
       · RSI>70 追高剔除（需 tech.rsi）
       · 板块约束 L2≤2 / L1≤2（需 sector_l2）
     板块字段可从最新的 top200_scored_2026-10-23.json 近似回填（板块归属较稳定），
     但 RSI 无法回填 → 本回测的组合是「多因子核心打分 + attention/theme 硬剔除」，
     **比线上实际组合略宽松**（少了 RSI 一道闸）。这一点会降低绝对收益的乐观程度，
     但不影响「各持仓周期谁更优」的相对比较。

  边界②：入场价取 **次日开盘价**（信号在 d0 收盘后生成，d0 收盘价不可成交）。
     持有 h 日 = 次日开盘买入 → 第 d0+h 日收盘卖出（h=1 即当日开盘买、当日收盘卖）。
  边界③：基准 = 同一截面全部 490 只的**等权中位收益**（池内基准）。
     记忆已确认池子本身有负 alpha，故必须同时报绝对收益与超额，不能只报超额。

核心产出：
  1. h=1..20 的逐日累计曲线（平均收益 / 超额 / 胜率）—— 直接看出「边际在第几天衰减」
  2. 各标准周期（T+1/3/5/10/15/20）的统计量与 NW t（lag=h-1 与 Andrews 双口径）
  3. 非重叠样本：按 h 步进取样，避免重叠观测虚增 n
  4. 最优持仓期的判定规则（写死，可复算）
"""
import json
import math
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "audit_round15.json")

REV_BLOCK = ("score_oversold60", "score_bias60", "score_rsi_rev",
             "score_vol_pulse", "score_overbought")
DROP_HIGH = {"score_attention": 0.80, "score_theme": 0.80}
TOP_N = 10
MAX_PER_L2 = 2
MAX_PER_L1 = 2


def F(x, d=None):
    try:
        v = float(x)
        return v if v == v else d
    except (TypeError, ValueError):
        return d


def pct_rank(vals):
    idx = [(i, v) for i, v in enumerate(vals) if v is not None]
    idx.sort(key=lambda x: x[1])
    n = len(idx)
    out = {i: (0.0 if n <= 1 else k / (n - 1.0)) for k, (i, _) in enumerate(idx)}
    for i, v in enumerate(vals):
        if v is None:
            out.setdefault(i, 0.0)
    return out


def nw_t(xs, lag):
    n = len(xs)
    if n < 3:
        return None
    m = sum(xs) / n
    e = [x - m for x in xs]
    g0 = sum(v * v for v in e) / n
    if g0 <= 0:
        return None
    var = g0
    for L in range(1, min(lag, n - 1) + 1):
        w = 1.0 - L / (lag + 1.0)
        gl = sum(e[i] * e[i - L] for i in range(L, n)) / n
        var += 2.0 * w * gl
    if var <= 0:
        return None
    return m / math.sqrt(var / n)


def andrews_lag(xs):
    n = len(xs)
    if n < 5:
        return 0
    m = sum(xs) / n
    e = [x - m for x in xs]
    g0 = sum(v * v for v in e) / n
    if g0 <= 0:
        return 0
    rho = sum(e[i] * e[i - 1] for i in range(1, n)) / n / g0
    rho = max(min(rho, 0.97), -0.97)
    a = 4.0 * rho ** 2 / ((1 - rho) ** 2 * (1 + rho) ** 2)
    return max(0, min(int(math.ceil(1.1447 * (a * n) ** (1.0 / 3.0))), n - 1))


def med(xs):
    xs = sorted(xs)
    n = len(xs)
    if not n:
        return None
    return xs[n // 2] if n % 2 else (xs[n // 2 - 1] + xs[n // 2]) / 2.0


def load_weights():
    """与线上同源：优先 factor_weights.json，失败退 v4.2 先验。"""
    prior = {"score_fund": 0.32, "score_news": 0.16, "score_theme_neg": 0.12,
             "score_size": 0.12, "score_sector": 0.10,
             "score_oversold60": 0.045, "score_bias60": 0.045,
             "score_rsi_rev": 0.045, "score_vol_pulse": 0.045,
             "score_overbought": 0.0, "score_theme_intrinsic": 0.0,
             "score_attention": 0.0}
    try:
        d = json.load(open(os.path.join(HERE, "factor_weights.json"), encoding="utf-8"))
        w = d.get("weights") or {}
        if w and abs(sum(float(w[k]) for k in prior if k in w) - 1.0) < 0.05:
            return w, d.get("version")
    except Exception:
        pass
    return prior, "prior-v4.2"


def build_sector_map():
    """从最新 scored 回填 code → (sector_l2, sector_parent)。板块归属相对稳定。"""
    p = os.path.join(HERE, "top200_scored_2026-10-23.json")
    m = {}
    if not os.path.exists(p):
        return m
    d = json.load(open(p, encoding="utf-8"))
    for r in (d.get("results") or []):
        c = str(r.get("code") or "")
        if c:
            m[c] = (r.get("sector_l2") or r.get("sector") or "未分类",
                    r.get("sector_parent") or r.get("sector_l2") or "未分类")
    return m


def pick_one(snap, W, sector_map, use_cap):
    """复现 pick_multifactor 的组合构造（无 tech → 无 RSI 闸）。→ [code,...]"""
    rows = snap["rows"]
    n = len(rows)
    if n < 20:
        return []
    cols = {}
    for k in ("score_fund", "score_news", "score_attention", "score_theme"):
        cols[k] = [F(r.get(k)) for r in rows]

    def rev_score(i):
        vs = [F(rows[i].get(d), 0.0) or 0.0 for d in REV_BLOCK]
        return sum(vs) / len(vs) if vs else 0.0

    pr = {k: pct_rank(cols[k]) for k in ("score_fund", "score_news")}
    pr["__rev__"] = pct_rank([rev_score(i) for i in range(n)])
    pr_att = pct_rank(cols["score_attention"])
    pr_thm = pct_rank(cols["score_theme"])
    wf = float(W.get("score_fund") or 0)
    wn = float(W.get("score_news") or 0)
    wr = float(W.get("__rev__") or 0)
    if wr == 0:                      # factor_weights 里反转块是 5 个分项
        wr = sum(float(W.get(k) or 0) for k in REV_BLOCK)
    tot = (wf + wn + wr) or 1.0
    wf, wn, wr = wf / tot, wn / tot, wr / tot

    cand = []
    for i, r in enumerate(rows):
        if pr_att.get(i, 0) >= DROP_HIGH["score_attention"]:
            continue
        if pr_thm.get(i, 0) >= DROP_HIGH["score_theme"]:
            continue
        s = wf * pr["score_fund"].get(i, 0) + wn * pr["score_news"].get(i, 0) \
            + wr * pr["__rev__"].get(i, 0)
        cand.append((s, i))
    cand.sort(key=lambda x: -x[0])

    out, c_l2, c_l1 = [], {}, {}
    for s, i in cand:
        c = str(rows[i].get("code") or "")
        if not c:
            continue
        if use_cap:
            l2, l1 = sector_map.get(c, ("未分类", "未分类"))
            if c_l2.get(l2, 0) >= MAX_PER_L2 or c_l1.get(l1, 0) >= MAX_PER_L1:
                continue
            c_l2[l2] = c_l2.get(l2, 0) + 1
            c_l1[l1] = c_l1.get(l1, 0) + 1
        out.append(c)
        if len(out) >= TOP_N:
            break
    return out


def main():
    date_end = sys.argv[1] if len(sys.argv) > 1 else None
    hist = json.load(open(os.path.join(HERE, "factor_history.json"), encoding="utf-8"))
    snaps = hist["snapshots"]
    p = hist["prices"]
    tdays, px, op = p["dates"], p["px"], p.get("opens") or {}
    W, wver = load_weights()
    smap = build_sector_map()
    print(f"权重版本: {wver} · 板块映射回填 {len(smap)} 只\n")

    # ⚠️ 板块约束在历史上**不可复现**：历史截面只存 16 个评分字段，没有 sector_l2；
    #    用最新 scored 回填的板块映射与当日池不完全重合（实测 use_cap=True 只选 2 只，
    #    大量候选落进同一「未分类」桶，L2 上限立即打满）。
    #    因此主口径 = 无板块约束；带板块约束仅作敏感性分析，且不用于结论。
    MAXH = 20
    MIN_PICKS = 5
    variants = {"no_cap": False, "with_cap": True}
    recs = {k: [] for k in variants}     # 每个入场日一条：{date, i0, codes, rows}
    for snap in snaps:
        d0 = snap["date"]
        if d0 not in tdays:
            continue
        i0 = tdays.index(d0)
        rows_map = {str(r.get("code")): r for r in snap["rows"]}
        for vk, use_cap in variants.items():
            codes = pick_one(snap, W, smap, use_cap=use_cap)
            if len(codes) < MIN_PICKS:
                continue
            recs[vk].append({"date": d0, "i0": i0, "codes": codes, "rows": rows_map})
    for vk in variants:
        print(f"可用入场日 [{vk}]：{len(recs[vk])} / {len(snaps)} 个截面")

    # ---- 逐 h 计算组合与池内基准（两个变体各自跑一遍）----
    # 入场：d0 次日开盘；出场：d0+h 收盘
    rows_by_h = {}
    for vk, _rc in recs.items():
        rows_by_h[vk] = {}
        for h in range(1, MAXH + 1):
            port_rets, bench_rets, used = [], [], []
            for rc in _rc:
                i0 = rc["i0"]
                if i0 + h >= len(tdays):
                    continue
                ie = i0 + 1                       # 次日
                if ie >= len(tdays):
                    continue
                rs = []
                for c in rc["codes"]:
                    o = op.get(c) or []
                    e = px.get(c) or []
                    if ie >= len(o) or i0 + h >= len(e):
                        continue
                    po, pc = o[ie], e[i0 + h]
                    if not isinstance(po, (int, float)) or po <= 0:
                        continue
                    if not isinstance(pc, (int, float)) or pc <= 0:
                        continue
                    rs.append(pc / po - 1.0)
                if len(rs) < 5:                   # 有效样本不足则跳过该日
                    continue
                port_rets.append(sum(rs) / len(rs))
                # 池内基准：同截面全部标的、同窗口、同口径
                brs = []
                for c2 in rc["rows"]:
                    o2 = op.get(c2) or []
                    e2 = px.get(c2) or []
                    if ie >= len(o2) or i0 + h >= len(e2):
                        continue
                    po2, pc2 = o2[ie], e2[i0 + h]
                    if isinstance(po2, (int, float)) and po2 > 0 and isinstance(pc2, (int, float)) and pc2 > 0:
                        brs.append(pc2 / po2 - 1.0)
                if len(brs) < 20:
                    port_rets.pop()
                    continue
                bench_rets.append(sum(brs) / len(brs))
                used.append(rc["date"])
            n = len(port_rets)
            if n < 3:
                continue
            exc = [port_rets[i] - bench_rets[i] for i in range(n)]
            rows_by_h[vk][h] = {
                "h": h, "n": n, "dates": used,
                "port_mean_pct": round(sum(port_rets) / n * 100, 3),
                "bench_mean_pct": round(sum(bench_rets) / n * 100, 3),
                "exc_mean_pct": round(sum(exc) / n * 100, 3),
                "exc_med_pct": round(med(exc) * 100, 3),
                "win_pct": round(sum(1 for x in exc if x > 0) / n * 100, 1),
                "nw_t_laghm1": (round(nw_t(exc, h - 1), 2) if nw_t(exc, h - 1) is not None else None),
                "nw_t_andrews": (round(nw_t(exc, andrews_lag(exc)), 2)
                                 if nw_t(exc, andrews_lag(exc)) is not None else None),
                "exc_series_pct": [round(x * 100, 3) for x in exc],
            }

    # ---- 打印累计曲线 ----
    MAIN = "no_cap"
    print(f"\n=== h=1..20 逐日累计（主口径 {MAIN} · 入场=次日开盘，出场=d0+h 收盘）===")
    print(f"{'h':>3} {'n':>4} {'组合':>9} {'池内基准':>10} {'超额':>9} {'超额中位':>10} {'胜率':>7} {'NWt(h-1)':>10}")
    print("-" * 76)
    for h in range(1, MAXH + 1):
        r = rows_by_h[MAIN].get(h)
        if not r:
            print(f"{h:>3} {'—':>4}  样本不足")
            continue
        print(f"{h:>3} {r['n']:>4} {r['port_mean_pct']:>8.2f}% {r['bench_mean_pct']:>9.2f}% "
              f"{r['exc_mean_pct']:>+8.2f}% {r['exc_med_pct']:>+9.2f}% "
              f"{r['win_pct']:>6.1f}% {(r['nw_t_laghm1'] if r['nw_t_laghm1'] is not None else 0):>10.2f}")

    # ---- 标准周期汇总 ----
    STDS = [1, 3, 5, 10, 15, 20]
    print("\n=== 标准持仓周期（主口径）===")
    std_out = {}
    for h in STDS:
        r = rows_by_h[MAIN].get(h)
        if not r:
            continue
        # 非重叠：按 h 步进取样
        nonlap = r["exc_series_pct"][::h]
        nl_mean = round(sum(nonlap) / len(nonlap), 3) if nonlap else None
        nl_t = (round(nw_t([x / 100 for x in nonlap], 0), 2)
                if len(nonlap) >= 3 else None)
        std_out[str(h)] = {
            "n": r["n"], "port_mean_pct": r["port_mean_pct"],
            "bench_mean_pct": r["bench_mean_pct"],
            "exc_mean_pct": r["exc_mean_pct"], "exc_med_pct": r["exc_med_pct"],
            "win_pct": r["win_pct"],
            "nw_t_laghm1": r["nw_t_laghm1"], "nw_t_andrews": r["nw_t_andrews"],
            "n_nonoverlap": len(nonlap), "nonoverlap_exc_mean_pct": nl_mean,
            "nonoverlap_t": nl_t,
        }
        print(f"  T+{h:<2} n={r['n']:<3} 组合 {r['port_mean_pct']:>+6.2f}% · "
              f"基准 {r['bench_mean_pct']:>+6.2f}% · 超额 {r['exc_mean_pct']:>+6.2f}% "
              f"(中位 {r['exc_med_pct']:>+6.2f}%) · 胜率 {r['win_pct']:>4.1f}% · "
              f"NWt {r['nw_t_laghm1'] if r['nw_t_laghm1'] is not None else 0:>+5.2f} · "
              f"非重叠 n={len(nonlap):<2} 超额 {nl_mean if nl_mean is not None else 0:>+6.2f}%")

    # 敏感性：带板块约束口径（历史不可复现，仅参照）
    print("\n=== 敏感性：带板块约束（L2≤2/L1≤2，回填映射，仅参照）===")
    sens_out = {}
    for h in STDS:
        r = rows_by_h["with_cap"].get(h)
        if not r:
            continue
        sens_out[str(h)] = {"n": r["n"], "exc_mean_pct": r["exc_mean_pct"],
                            "win_pct": r["win_pct"],
                            "nw_t_laghm1": r["nw_t_laghm1"]}
        print(f"  T+{h:<2} n={r['n']:<3} 超额 {r['exc_mean_pct']:>+6.2f}% · "
              f"胜率 {r['win_pct']:>4.1f}% · "
              f"NWt {r['nw_t_laghm1'] if r['nw_t_laghm1'] is not None else 0:>+5.2f}")

    # ---- 最优持仓期判定（规则写死，可复算）----
    # ⚠️ v4.3 修订：**不能按「点估计超额最高」选周期**。
    #   第一版规则是 max(exc_mean_pct) → 选中 T+20（+3.47%），但那是错的：
    #   T+20 的 NW t 只有 2.09（全场最低，最接近 2.0 门槛），非重叠样本只剩 3 个
    #   —— 名义超额最高恰恰是统计上最不可信的。这正是「只看点估计」的典型陷阱。
    #   修正规则（t 值天然惩罚不确定性与样本量，比点估计更稳健）：
    #     ① 先要求 |NW t| >= 2.0（统计上站得住）
    #     ② 再要求非重叠样本 >= 5（避免 3 个观测撑起的结论）
    #     ③ 在满足①②的周期里取 **NW t 最大**者（而非超额最大者）
    #   实证结果：T+10（NWt 3.01、超额 +2.55%、非重叠 7）胜出。
    #   ⚠️ 数据源必须用 std_out（含 n_nonoverlap），不能用 rows_by_h（该字段不存在）。
    cands = [(int(h), r) for h, r in std_out.items()]
    _ok = [(h, r) for h, r in cands
           if r.get("nw_t_laghm1") is not None and abs(r["nw_t_laghm1"]) >= 2.0
           and (r.get("n_nonoverlap") or 0) >= 5]
    best = max(_ok, key=lambda x: abs(x[1]["nw_t_laghm1"])) if _ok else None
    curve = [(h, rows_by_h[MAIN][h]["exc_mean_pct"]) for h in range(1, MAXH + 1)
             if rows_by_h[MAIN].get(h)]
    peak = max(curve, key=lambda x: x[1]) if curve else None
    sig_h = [h for h, r in cands
             if r.get("nw_t_laghm1") is not None and abs(r["nw_t_laghm1"]) >= 2.0]

    print("\n=== 结论（规则化，可复算）===")
    if best:
        print(f"  推荐持仓周期：T+{best[0]}（NWt {best[1]['nw_t_laghm1']:+.2f}、"
              f"超额 {best[1]['exc_mean_pct']:+.2f}%、"
              f"非重叠 n={best[1].get('n_nonoverlap')}）")
    _bp = max(cands, key=lambda x: x[1]["exc_mean_pct"])
    print(f"  点估计超额最高的标准周期：T+{_bp[0]}（{_bp[1]['exc_mean_pct']:+.2f}%）"
          f" ← 仅点估计，不作推荐依据")
    print(f"  20 日内超额峰值：h={peak[0]}（{peak[1]:+.2f}%）" if peak else "  无")
    print(f"  |NW t|>=2.0 的周期：{sig_h if sig_h else '无（全部不显著）'}")
    print("  判定规则：|NWt|>=2.0 且 非重叠样本>=5 的周期里取 NWt 最大者"
          "（t 值天然惩罚不确定性与样本量，优于按点估计超额取最大）")

    res = {
        "date_run": date_end or "latest",
        "weights_version": wver,
        "entry_rule": "d0 次日开盘价买入；出场 = d0+h 日收盘价（h=1 即当日开盘买当日收盘卖）",
        "bench_rule": "同一截面全部标的同窗口等权平均收益（池内基准）",
        "main_variant": MAIN,
        "selection_rule": ("|NW t|>=2.0 且非重叠样本>=5 的周期里取 NWt 最大者。"
                           "⚠️ 不用「点估计超额最高」选周期：T+20 名义超额最高(+3.47%)"
                           "但 NWt 仅 2.09、非重叠样本仅 3 个，是最不可信的而非最优的"),
        "variant_note": ("主口径无板块约束：历史截面缺 sector_l2，用最新 scored 回填的映射与当日池"
                         "不重合，带约束只选得出 2 只 → 不可用于结论；with_cap 仅作敏感性参照"),
        "n_snapshots_total": len(snaps),
        "n_entry_days": {k: len(v) for k, v in recs.items()},
        "horizons": std_out,
        "sensitivity_with_cap": sens_out,
        "curve_by_h": {str(h): rows_by_h[MAIN][h] for h in range(1, MAXH + 1)
                       if rows_by_h[MAIN].get(h)},
        "best_std_horizon": best[0] if best else None,
        "best_reason": (f"NWt {best[1]['nw_t_laghm1']:+.2f}、超额 {best[1]['exc_mean_pct']:+.2f}%、"
                        f"非重叠 n={best[1].get('n_nonoverlap')}" if best else None),
        "peak_h": peak[0] if peak else None,
        "peak_exc_pct": peak[1] if peak else None,
        "significant_horizons": sig_h,
        "boundaries": [
            "历史截面只存 16 个评分字段，无 tech（均线/RSI/支撑阻力）→ 无法复现 RSI>70 剔除与买区判定",
            "板块约束无法在历史上复现（主口径已去掉）",
            "入场取次日开盘价（信号在 d0 收盘后生成，d0 收盘价不可成交）",
            "基准为池内等权均值，池子本身有负 alpha → 超额与绝对收益须同时看",
            "日频重叠观测：h 日持有期在连续入场日间高度重叠，非重叠样本量远小于 n",
            "绝对收益全周期为负（T+20 组合 -4.23% vs 基准 -7.70%）→ 持有多久都不赚钱，只是相对少亏",
        ],
    }
    json.dump(res, open(OUT, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"\n✅ 写入 {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
