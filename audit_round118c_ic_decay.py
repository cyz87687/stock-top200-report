#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""audit_round118c_ic_decay.py —— P1-4：IC 衰减曲线与半衰期

═══ 为什么必须做（round118 P1-4）═══

项目现在只用 h=1/5/20 三个点看 IC。三个点无法回答两个决策级问题：

  ① **信号在哪一天衰减到一半？**（半衰期）
     → 这直接决定「多久调一次仓」。调太勤 = 拿未衰减的 alpha 去付不必要的换手成本；
       调太懒 = 拿已经衰减完的 alpha 去持仓。

  ② **h=5 的 IC 比 h=1 还高，是真的还是噪声？**
     factor_ic 实测 composite h1 IC=0.054 / h5 IC=0.146。若 h5 > h1 是真实结构，
     说明信号有「发酵期」，短周期反而吃不到；若是噪声，则不能据此拉长持有期。
     三个点看不出来，必须铺开网格。

═══ 口径（严格复用生产，不另起一套）═══

· 前向收益：**直接用 `factor_eval._fwd_returns`**，不用自己实现
  → 理由：MEMORY 明确记着「同一口径两处实现必然漂移」。衰减曲线若自带一份
    前向收益实现，h=1/5/20 三点就会与 factor_ic.json 对不上，读者无从判断
    差异来自衰减还是来自实现差异。
· 统计量：**直接用 `factor_eval._agg_stats`**，含 NW t（lag 自适应）、
  门槛版字段、截面规模披露 —— 与页面完全同源。
· 交易日轴：`factor_eval.px_index(hist)` 的 tdays，与 IC 计算同一条轴。

═══ 半衰期的定义（必须写清，否则数字无法解释）═══

用**峰值**而非 h=1 作基准：

    half_life = 使 IC(h) ≤ IC_peak / 2 的最小 h

为什么不用 h=1：若 h=1 本身受微观结构噪声压制（本项目实测 h5 IC > h1 IC），
用 h=1 当基准会得出「负衰减」这种无意义的结论。峰值基准假设「信号在峰值处
最干净」，是业界通行做法（Novy-Marx & Velikov 用峰值月 IC 定义 turnover 衰减）。

若在网格最大 h 处仍未跌破峰值一半 → half_life = None，并明确报「> h_max，
本网格无法确定」（**不能**报 h_max，那是把「没测到」说成「测到了」）。

═══ 输出 ═══

data/audit_round118c_ic_decay.json：
  curves[field] = {
    label, peak_h, peak_ic, half_life, half_life_note,
    points: [{h, n, ic_mean, nw_t, nw_lag, win_rate, cross_section_median,
              gated:{n_after_gate, nw_t_after_gate, usable}, edge_days}]
  }
  meta = {generated_at, horizons, note, price_axis}
"""

import json
import os
import sys
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import factor_eval as F

HORIZONS_GRID = (1, 2, 3, 5, 10, 20, 30, 40, 60)
OUT = os.path.join(HERE, "data", "audit_round118c_ic_decay.json")

# 关注的口径：与页面 verdict 直接相关的四个 + 权重最大的三个单维
FIELDS = [
    ("total", "综合总分（页面主口径）"),
    ("score_fund", "基本面（权重 0.20）"),
    ("score_size_growth", "小盘成长（权重 0.20）"),
    ("score_quality", "质量（权重 0.10）"),
    ("score_growth", "成长（权重 0.10）"),
    ("score_dominant", "龙头壁垒（权重 0.10）"),
    ("score_ep", "估值分位（权重 0.10）"),
    ("score_tech_neg", "技术面反号（权重 0.10）"),
]


def _prep():
    """装配与生产完全一致的快照集 / 交易日轴 / 价格格。

    ⚠️ 关键：必须在这里就把 DERIVED_FIELDS 派生好，与主流程
    `factor_eval.main()` 的 `_aligned_derived` 完全同步。

    第一版漏了这一步，后果是 score_size_growth 只有 n=5、score_ep 只有 n=0 ——
    因为快照行里本来就没有这两个字段，它们靠 `_derive_dims` 从 K 线/pe 现算。
    漏派生看起来像「这两个维样本极少」，实际是「我根本没给它算」。
    这正是 round117 的老坑换个形式重演：**把"没测"显示成"测了但样本少"**。
    """
    hist = F.load_history()
    snaps = [s for s in hist.get("snapshots", []) if s.get("rows")]
    tdays, px = F.px_index(hist)
    tset = set(tdays)
    aligned = [s for s in snaps if s["date"] in tset]
    aligned, stale_px = F._drop_stale_price_snapshots(aligned)
    contaminated = F._strip_contaminated_fields(aligned)
    # 与主流程同构：逐截面派生（不能把截面拼起来一次性派生——池是逐日变的）
    # 派生过程会为每个截面打印「模块B 龙头壁垒 落分 N 只」，83 个截面 × 8 因子
    # 会把真正要看的结果淹掉，故此处临时静音 stdout（结果不受影响）。
    import contextlib
    import io
    _buf = io.StringIO()
    with contextlib.redirect_stdout(_buf):
        derived = [dict(s, rows=F._derive_dims(s["rows"], s.get("date"), tdays, px))
                   for s in aligned]
    _n_dom = sum(1 for s in derived for r in s["rows"]
                 if isinstance(r.get("score_dominant"), (int, float)))
    _n_all = sum(len(s["rows"]) for s in derived)
    print(f"派生完成：dominant 覆盖 {_n_dom}/{_n_all}（{_n_dom/max(_n_all,1)*100:.1f}%）")
    return hist, aligned, derived, tdays, px, stale_px, contaminated


def _coverage_warning(tdays, px, min_codes=100):
    """价格轴逐日覆盖体检 —— 若尾部断裂，衰减曲线会静默丢样本。

    round118 实测踩过：10-29 起 1766 只里只剩 2 只有收盘价，导致 d0≥10-28 的
    截面全部无法算前向收益，score_growth 因此 n=0。若不在此处告警，
    衰减曲线会「看起来正常地」少掉最后几天。
    """
    bad = []
    for d in tdays:
        n = sum(1 for m in px.values() if isinstance(m.get(d), (int, float)))
        if n < min_codes:
            bad.append((d, n))
    return bad


def _edge_days(h):
    """h 个「交易日」对应的自然日跨度下界（仅作可读性标注，不参与计算）。"""
    return h


def _overlap_diag(n, h):
    """重叠窗口诊断（round118 P1-4 新增）。

    ⚠️ 这是衰减曲线最容易骗人的地方。若截面是**连续交易日**（本项目实测就是），
    h 日前向收益的相邻窗口重叠 h−1 天，n 个截面里真正独立的观测数约 n/h。

    实测后果：`score_ep` 在 h=60 报出 NW t=+15.39（n=27）。但 27 个截面
    全是连续日，60 日窗口相互重叠 59 天 → 独立观测 ≈ 27/60 < 1 个。
    此时 `_safe_nw_lag` 把理想 lag=59 压到 lag=12（因为 n 只支撑得起 12），
    Bartett 核只覆盖 12/59 的自相关 → **校正严重不足，t 被系统性放大**。
    这不是 bug，是小样本 HAC 的固有极限（Andrews 1991 / Kiefer-Vogelsang 2005
    都指出 T<100 时数据驱动带宽不可依赖）。

    故本函数给出 `overlap_ratio = h/n` 与三档可引用性，供报告层判断：
      · h/n ≤ 0.25  正常（重叠可控，lag≈h−1 能被 n 支撑）
      · h/n ≤ 0.50  勉强（需 n ≥ 4h）
      · h/n > 0.50  **t 不可引用**，只能看 IC 点估计
    """
    if not n or h <= 0:
        return {"overlap_ratio": None, "effective_n": None, "t_trustworthy": False,
                "note": "无样本"}
    ratio = h / n
    eff_n = n / h
    if ratio <= 0.25:
        trust, note = True, "重叠可控"
    elif ratio <= 0.50:
        trust, note = True, "重叠较大，需谨慎（独立观测约 %.1f 个）" % eff_n
    else:
        trust, note = False, (
            f"⚠️ 重叠率 h/n={ratio:.2f} > 0.5 —— {n} 个连续截面里独立观测仅约 "
            f"{eff_n:.1f} 个，NW lag 只能取到 {min(h-1, n//2-1)}（理想 {h-1}），"
            f"校正严重不足，t 值被系统性放大，**不可用于判定显著性**")
    return {"overlap_ratio": round(ratio, 3), "effective_n": round(eff_n, 2),
            "t_trustworthy": trust, "note": note}


def decay_curve(snaps, field, tdays, px):
    """单因子的完整衰减曲线。"""
    pts = []
    for h in HORIZONS_GRID:
        # 统一走 _eval_one_h（与生产 `_eval_factor` 逐行同构）。
        # ⚠️ 不用 `_eval_factor(...)[str(h)]`：它每次只跑 HORIZONS=(1,5,20) 三个点，
        #    对 h=2/3/10/30/40/60 直接返回 None，然后仍然要落到 _eval_one_h；
        #    而对 h=1/5/20 调它等于每次白算三个 h。9 个网格点 × 8 个因子 = 72 次
        #    全量重算，实测会从 ~40s 涨到 ~10min，且两条路径并行会引入不可比风险。
        #    h=1/5/20 三点的一致性由 `_selftest_against_production()` 显式对拍。
        agg = _eval_one_h(snaps, field, h, tdays, px)
        g = agg.get("gated") or {}
        pts.append({
            "h": h,
            "n": agg.get("n"),
            "ic_mean": agg.get("ic_mean"),
            "nw_t": agg.get("nw_t"),
            "nw_lag": agg.get("nw_lag"),
            "win_rate": agg.get("win_rate"),
            "cross_section_median": agg.get("cross_section_median"),
            "overlap": _overlap_diag(agg.get("n"), h),
            "gated": {
                "n_after_gate": g.get("n_after_gate"),
                "nw_lag_after_gate": g.get("nw_lag_after_gate"),
                "nw_t_after_gate": g.get("nw_t_after_gate"),
                "usable": g.get("usable"),
                "usable_note": g.get("usable_note"),
            },
        })

    # ── 峰值与半衰期 ──
    # ⚠️ 只用「重叠可控」的点算峰值与半衰期。理由：h 很大时 IC 点估计本身
    #    仍然可用（它是无偏的截面 rank 相关均值），但**峰值位置**若由 h=60
    #    这种 n/h<1 的点决定，等于让一个独立观测不到 1 个的点来定义形态。
    #    实测：score_ep 的全网格峰值在 h=60（IC=+0.3323, t=+15.39），
    #    若照单全收，会得出「估值因子要拿 60 天」的结论 —— 而那个 IC 只由
    #    27 个重叠窗口给出、NW 校正又严重不足，不能支撑该决策。
    trusted = [p for p in pts
               if p["ic_mean"] is not None and p["n"] and p["overlap"]["t_trustworthy"]]
    valid = [p for p in pts if p["ic_mean"] is not None and p["n"]]
    if not valid:
        return {"points": pts, "peak_h": None, "peak_ic": None,
                "half_life": None, "half_life_note": "无有效观测"}
    _pool = trusted or valid
    peak = max(_pool, key=lambda p: p["ic_mean"])
    peak_ic, peak_h = peak["ic_mean"], peak["h"]
    _peak_basis = ("仅在重叠可信的点中取峰值" if trusted else
                   "⚠️ 无重叠可信点，退化为全网格取峰值（形态仅供参考）")

    half_life, note = None, ""
    _sign_flipped = False
    if peak_ic <= 0:
        note = (f"峰值为负（IC_peak={peak_ic:+.4f} @ h={peak_h}）—— "
                f"该因子在本样本内无正向预测力，半衰期无定义（不是 0）")
    else:
        target = peak_ic / 2.0
        after = [p for p in pts if p["h"] > peak_h and p["ic_mean"] is not None]
        hit = next((p for p in after if p["ic_mean"] <= target), None)
        if hit and hit["ic_mean"] > 0:
            half_life = hit["h"]
            note = (f"峰值 IC={peak_ic:+.4f} 出现在 h={peak_h}；"
                    f"h={hit['h']} 时降至 {hit['ic_mean']:+.4f} ≤ 峰值一半（{target:+.4f}）")
        elif hit:
            # ⚠️ 关键判据修正：IC 跌破峰值一半但**已翻负**，这不是「衰减到一半」，
            #    是「信号反向」。实测 total：峰值 h=3 IC=+0.0255，h=30 IC=−0.0175。
            #    若照原判据报「半衰期=30 日」，读者会理解成「拿到第 30 天还剩一半
            #    alpha」——而真相是「第 30 天已经反向」。这两者对调仓决策完全相反。
            #    故：翻负时 half_life 仍然给出（它确实是首次跌破点），但必须
            #    `sign_flipped=True` 并在 note 里说清，且不由它单独支撑「该拿多久」。
            half_life = hit["h"]
            note = (f"⚠️ 峰值 IC={peak_ic:+.4f} 出现在 h={peak_h}，但 h={hit['h']} 时 IC="
                    f"{hit['ic_mean']:+.4f} 已**翻负**——这不是『衰减到一半』，"
                    f"而是『信号反向』。不构成可持有区间的依据；"
                    f"真实含义是：该因子在 h>{peak_h} 后没有稳定正向 alpha")
            _sign_flipped = True
        else:
            tail = [p for p in after]
            _last = f"，网格内最低 {min(p['ic_mean'] for p in tail):+.4f} @ h={min(tail, key=lambda p: p['ic_mean'])['h']}" if tail else ""
            note = (f"峰值 IC={peak_ic:+.4f} 出现在 h={peak_h}；在网格最大 h={max(HORIZONS_GRID)} "
                    f"处仍未跌破峰值一半（{target:+.4f}）{_last} → "
                    f"**半衰期 > {max(HORIZONS_GRID)} 个交易日，本网格无法确定具体值**（不是等于 {max(HORIZONS_GRID)}）")

    # ── h=1 与峰值的对比（回答「是否真存在发酵期」）──
    h1 = next((p for p in pts if p["h"] == 1), None)
    ferment = None
    if h1 and h1["ic_mean"] is not None and peak_h != 1:
        ferment = {
            "h1_ic": h1["ic_mean"],
            "peak_ic": peak_ic,
            "ratio": round(peak_ic / h1["ic_mean"], 3) if h1["ic_mean"] else None,
            "interpretation": ("峰值晚于 h=1，存在『发酵期』形态；"
                               "但需注意 h=1 的 IC 序列 n 更多、噪声更低，"
                               "该形态也可能是不同 h 的样本集不同所致，不能直接当因果"
                               if peak_ic > (h1["ic_mean"] or 0) else
                               "峰值为 h=1，信号在次日即最强，无发酵期"),
        }

    return {
        "points": pts,
        "peak_h": peak_h,
        "peak_ic": round(peak_ic, 4),
        "peak_basis": _peak_basis,
        "half_life": half_life,
        "half_life_note": note,
        "half_life_sign_flipped": _sign_flipped,
        "fermentation": ferment,
        "n_trusted_points": len(trusted),
    }


def _eval_one_h(snaps, field, h, tdays, px):
    """`_eval_factor` 只覆盖 HORIZONS=(1,5,20)。网格外的 h 用同一套函数装配。

    ⚠️ 必须与 `_eval_factor` 逐行同构，否则 h=2/3/10 与 h=1/5/20 不可比。
    """
    fwd = F._fwd_returns(snaps, h, tdays, px)
    ics, spreads, dates, sizes = [], [], [], []
    bydate = {s["date"]: s for s in snaps}
    for d0, d1, rets in fwd:
        base = bydate.get(d0)
        if base is None:
            continue
        fv, rv = [], []
        for r in base["rows"]:
            if r["code"] in rets and isinstance(r.get(field), (int, float)):
                fv.append(r[field])
                rv.append(rets[r["code"]])
        ic = F.rank_ic(fv, rv)
        if ic is not None:
            ics.append(ic)
            spreads.append(F._quantile_spread(fv, rv))
            dates.append(d0)
            sizes.append(len(fv))
    return F._agg_stats(ics, spreads, dates, h, sizes=sizes)


def _selftest_against_production(snaps, tdays, px, fields=("total", "score_fund")):
    """对拍：`_eval_one_h` 必须与生产 `_eval_factor` 在 h=1/5/20 上逐字段一致。

    ⚠️ 这是本脚本唯一的正确性防线。衰减曲线跨 9 个 h 网格，其中 6 个是
    `_eval_factor` 覆盖不到的；若 `_eval_one_h` 与生产实现有任何一行差异
    （哪怕只是忘了传 sizes），h=2/3/10 与 h=1/5/20 就不可比，
    整条曲线会把「实现差异」画成「衰减形态」，而且**看起来非常像一个发现**。

    同类事故本项目已发生过两次：审计脚本 NW 口径差 21 倍、`cross_section_audit`
    取空字段导致连续性判定静默跳过。故此处必须硬对拍，不是可选项。
    """
    import math
    bad = []
    for field in fields:
        prod = F._eval_factor(snaps, field, tdays, px)
        for h in (1, 5, 20):
            a = prod.get(str(h)) or {}
            b = _eval_one_h(snaps, field, h, tdays, px)
            for k in ("n", "ic_mean", "nw_t", "nw_lag", "win_rate", "cross_section_median"):
                va, vb = a.get(k), b.get(k)
                if isinstance(va, float) and isinstance(vb, float):
                    if not math.isclose(va, vb, rel_tol=0, abs_tol=1e-9):
                        bad.append((field, h, k, va, vb))
                elif va != vb:
                    bad.append((field, h, k, va, vb))
            ga, gb = a.get("gated") or {}, b.get("gated") or {}
            for k in ("n_after_gate", "nw_t_after_gate", "nw_lag_after_gate", "usable"):
                if ga.get(k) != gb.get(k):
                    bad.append((field, h, "gated." + k, ga.get(k), gb.get(k)))
    if bad:
        print("❌ 对拍失败（_eval_one_h 与生产实现不一致）：")
        for r in bad:
            print(f"   {r[0]:16} h={r[1]:>2} {r[2]:22} prod={r[3]!r} mine={r[4]!r}")
    else:
        print("✅ 对拍通过：_eval_one_h 与 _eval_factor 在 h=1/5/20 上逐字段一致")
    return not bad


def main():
    hist, aligned, derived, tdays, px, stale_px, contaminated = _prep()
    print(f"快照 {len(aligned)} 个（已派生 {len(derived)} 个）/ "
          f"交易日轴 {len(tdays)} 天（{tdays[0]} ~ {tdays[-1]}）")
    if stale_px:
        print(f"🧹 接口陈旧快照已剔除：{stale_px}")
    if contaminated:
        print(f"🧹 污染字段已剔除：{[c['date'] for c in contaminated]}")

    bad = _coverage_warning(tdays, px)
    if bad:
        print(f"\n⚠️ 价格覆盖 <100 只的日期 {len(bad)} 个（会静默减少前向收益样本）：")
        print("   " + "；".join(f"{d}:{n}" for d, n in bad))

    print()
    if not _selftest_against_production(derived, tdays, px):
        print("❌ 对拍未通过 → 拒绝产出（口径不一致的衰减曲线比没有更坏）")
        return 2

    out = {
        "meta": {
            "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "horizons": list(HORIZONS_GRID),
            "n_snapshots": len(aligned),
            "price_axis": {"n": len(tdays), "first": tdays[0], "last": tdays[-1]},
            "price_coverage_warning": [{"date": d, "codes": n} for d, n in bad],
            "half_life_definition": (
                "half_life = 使 IC(h) ≤ IC_peak/2 的最小 h>peak_h。"
                "以**峰值**而非 h=1 作基准，理由：h=1 可能被微观结构噪声压制"
                "（本项目实测 h5 IC > h1 IC），用 h=1 会得出『负衰减』的无意义结论。"
                "网格内未跌破一半时报 None 并注明『> h_max，无法确定』，"
                "不报 h_max —— 那是把『没测到』说成『测到了』。"),
            "caveat": (
                "① 不同 h 的样本集不同（h 越大，尾部越少截面），跨 h 比较 IC 时"
                "样本量差异本身会改变噪声水平；② 这不是可交易策略的回测，"
                "未扣成本，也未做 T+1 可执行性约束；③ h=40/60 时截面数可能少于 "
                "NW 滞后所需，务必读 gated.usable 再引用。"),
        },
        "curves": {},
    }

    for field, label in FIELDS:
        c = decay_curve(derived, field, tdays, px)
        c["label"] = label
        out["curves"][field] = c
        hl = c.get("half_life")
        print(f"\n{field:20} 峰值 h={c.get('peak_h')} IC={c.get('peak_ic')} "
              f"半衰期={'拒定' if hl is None else str(hl)+'日'}"
              f"　（可信点 {c.get('n_trusted_points')}/{len(c['points'])}）")
        for p in c["points"]:
            g = p["gated"]
            _u = "可引用" if g.get("usable") else "不可引用"
            _ic = "None" if p["ic_mean"] is None else f"{p['ic_mean']:+.4f}"
            _t = "None" if p["nw_t"] is None else f"{p['nw_t']:+.2f}"
            _gt = "None" if g.get("nw_t_after_gate") is None else f"{g['nw_t_after_gate']:+.2f}"
            _ov = p["overlap"]
            _r = "—" if _ov["overlap_ratio"] is None else f"{_ov['overlap_ratio']:.2f}"
            _flag = "" if _ov["t_trustworthy"] else "  ⚠️h/n>0.5 t不可信"
            print(f"   h={p['h']:>3}  n={str(p['n']):>4}  IC={_ic:>8}  NWt={_t:>7}"
                  f"  lag={str(p['nw_lag']):>2}  h/n={_r:>4}"
                  f"  门槛后 n={str(g.get('n_after_gate')):>4}"
                  f" t={_gt:>7} [{_u}]{_flag}")

    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print(f"\n✅ 已写出 {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
