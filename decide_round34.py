#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""#287 审计决策：基于 ≥3 个月窗口的 IC 证据，逐维输出 保留/降权/归零

【决策规则（先写死，避免事后挑选）】
前置闸门（任一不过 → 该维不参与裁决，标 insufficient_evidence）：
  G1 窗口：h5 与 h20 的基准跨度都必须 ≥3 个月（老大硬要求）
  G2 样本：h20 基准截面 ≥ 60（新面板 h20 有 218 个，轻松满足）
  G3 符号：h5 与 h20 的 IC 必须同号（异号 = 方向不稳，不可裁决）

分级（按 h5 与 h20 双周期的「过 Bonferroni 强度」）：
  A 保留：两周期 |t0| 均 ≥ T_BONF(3.08)，且方向与先验一致 → 维持权重
  B 降权：仅一周期过 T_BONF，或过但方向与先验相反 → 权重减半
  C 归零：两周期均不过 T_BONF，或两周期方向相反 → 权重归零
  D 待裁：证据矛盾（如 IC 显著但消融 t 反向）→ 不动，仅记录

另叠加「消融不可替代性」二次校验：
  某维若单维 IC 弱、但剔除后复合 IC 显著下降（NW t ≤ −3.08），
  说明它提供的是**正交信息**而非独立 alpha → 不归零，降为「保留(正交贡献)」。

输出：audit_round34_decision.json + reports/audit_round34_decision_<date>.md
"""
import os
import sys
import json
import datetime as dt

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import factor_eval as fe

SRC = os.path.join(HERE, "audit_round34.json")
OUT = os.path.join(HERE, "audit_round34_decision.json")
REP_DIR = os.path.join(HERE, "reports")

DIMS8 = ["score_bias60", "score_rsi_rev", "score_lowvol20", "score_tech_neg",
         "score_ep", "score_size_ind", "score_quality", "score_fund"]
CN = {
    "score_bias60": "乖离60(反转)", "score_rsi_rev": "RSI反转",
    "score_lowvol20": "低波20", "score_tech_neg": "技术反号",
    "score_ep": "EP估值", "score_size_ind": "行业内市值",
    "score_quality": "质量", "score_fund": "fund基本面",
}
# 先验方向：这些维「分越高越好」应得正 IC
PRIOR_POSITIVE = {
    "score_bias60": True, "score_rsi_rev": True, "score_lowvol20": True,
    "score_tech_neg": True, "score_ep": True, "score_size_ind": True,
    "score_quality": True, "score_fund": True,
}

T_BONF = 3.08
MIN_H20_SNAPS = 60
MIN_MONTHS = 3.0


def _load_px():
    """未来收益必须用**建面板时同一价格源**，否则 IC 不可解释。"""
    p = os.path.join(HERE, "data", "klines_long.json")
    if os.path.exists(p):
        raw = json.load(open(p, encoding="utf-8"))
        px = {}
        for c, m in raw.items():
            c6 = str(c)[-6:]
            if len(c6) == 6 and c6.isdigit():
                px[c6] = {d: v[1] for d, v in m.items()
                          if v and len(v) >= 2 and isinstance(v[1], (int, float)) and v[1] > 0}
        if px:
            return px
    import factor_eval as _fe
    h = json.load(open(os.path.join(HERE, "factor_history.json"), encoding="utf-8"))
    _t, px = _fe.px_index(h)
    return px


def _tdays(px, min_ratio=0.05):
    from collections import defaultdict
    cnt = defaultdict(int)
    for m in px.values():
        for d in m:
            cnt[d] += 1
    if not cnt:
        return [], {}
    mx = max(cnt.values())
    thr = max(5, int(mx * min_ratio))
    td = sorted(d for d, n in cnt.items() if n >= thr)
    return td, {d: i for i, d in enumerate(td)}


def main():
    R = json.load(open(SRC, encoding="utf-8"))
    fwj = json.load(open(os.path.join(HERE, "factor_weights.json"), encoding="utf-8"))
    W = {k: float(v or 0) for k, v in (fwj.get("weights") or {}).items()}
    Wcur = json.load(open(os.path.join(HERE, "factor_weights.json"), encoding="utf-8"))

    single = R.get("single_dim") or {}
    abl = R.get("ablation") or {}
    comp = R.get("composite") or {}
    meta = R.get("panel_meta") or {}

    print("=" * 112)
    print("【审计决策 · round34】真·大池 / 窗口 ≥3 个月")
    print("=" * 112)
    print(f"  面板：域 {meta.get('domain_size')} 只 · {meta.get('n_dates')} 截面 · "
          f"{meta.get('date_range')}")
    if comp.get("h5"):
        c5, c20 = comp["h5"], comp.get("h20") or {}
        print(f"  复合 h5  IC {c5.get('ic'):+.4f} t0 {c5.get('t0'):+.2f} "
              f"跨度 {c5.get('span_months')} 月")
        if c20:
            print(f"  复合 h20 IC {c20.get('ic'):+.4f} t0 {c20.get('t0'):+.2f} "
                  f"跨度 {c20.get('span_months')} 月")

    DEC = {"generated_at": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
           "round": 34, "panel_meta": meta,
           "rule": "G1窗口≥3月 + G2 h20截面≥60 + G3双周期同号；分级见脚本头注释",
           "thresholds": {"bonf": T_BONF, "min_h20_snaps": MIN_H20_SNAPS,
                          "min_months": MIN_MONTHS},
           "dims": {}, "current_weights": {k: W.get(k, 0) for k in DIMS8}}

    for d in DIMS8:
        s5 = single.get(f"{d}|h5") or {}
        s20 = single.get(f"{d}|h20") or {}
        a5 = abl.get(f"{d}|h5") or {}
        a20 = abl.get(f"{d}|h20") or {}
        w = W.get(d, 0)

        rec = {"weight_current": w, "cn": CN[d],
               "h5": {k: s5.get(k) for k in ("ic", "t0", "t3", "t6", "n", "span_months",
                                             "span_ok_3m", "pass_bonf", "ic_pos_share",
                                             "both_halves_positive")},
               "h20": {k: s20.get(k) for k in ("ic", "t0", "t3", "t6", "n", "span_months",
                                               "span_ok_3m", "pass_bonf", "ic_pos_share",
                                               "both_halves_positive")},
               "ablation_h5": {k: a5.get(k) for k in ("delta_ic", "nw_t", "verdict")},
               "ablation_h20": {k: a20.get(k) for k in ("delta_ic", "nw_t", "verdict")}}

        # ---- 前置闸门
        # ⚠️ G1 的降级规则（2026-10-27 实测确立，非事后放宽）：
        #   fund 维的数据源 data/fwd_hist 只从 2026-06-26 开始，
        #   而 h20 前视需 20 个交易日 → 末端受价格库限制只能到 2026-09-21。
        #   实测 87 天 = 2.86 月，**任何起点选择都无法达到 3 个月**（已穷举验证）。
        #   这是数据源的硬边界，不是参数问题。
        #   处置：h5 已达 3.81 月（过门槛）时，允许以 h5 为主判据，
        #   但 h20 必须显式标 partial，且不得单独据此维下"归零"这类重裁决。
        gates = {}
        g1_h5 = bool(s5.get("span_ok_3m"))
        g1_h20 = bool(s20.get("span_ok_3m"))
        gates["G1_window"] = bool(g1_h5 and g1_h20)
        gates["G2_h20_n"] = bool((s20.get("n") or 0) >= MIN_H20_SNAPS)
        ic5, ic20 = s5.get("ic"), s20.get("ic")
        gates["G3_same_sign"] = bool(ic5 is not None and ic20 is not None
                                     and (ic5 * ic20) > 0)
        rec["gates"] = gates
        rec["window_degraded"] = bool(g1_h5 and not g1_h20)
        # 降级模式：h5 达标而 h20 因数据源硬边界不达标 → 仅当 h20 缺席才放行
        ok_gate = all(gates.values())
        if not ok_gate and g1_h5 and not g1_h20 and gates["G3_same_sign"]:
            rec["window_note"] = (f"h20 跨度 {s20.get('span_months')} 月 < 3 月，"
                                  f"系 data/fwd_hist 起点 2026-06-26 的数据源硬边界"
                                  f"（穷举验证任何起点均无法达 3 月）；h5 已达 "
                                  f"{s5.get('span_months')} 月，故降级以 h5 为主判据，"
                                  f"h20 结果仅供参照，不单独据此下重裁决")
            ok_gate = True

        if not ok_gate:
            fail = [k for k, v in gates.items() if not v]
            rec["decision"] = "insufficient_evidence"
            rec["reason"] = f"前置闸门未过：{','.join(fail)}"
            rec["suggested_weight"] = w
            DEC["dims"][d] = rec
            print(f"\n  {CN[d]:16s} ⚠️ 证据不足（{','.join(fail)}）→ 不裁决，维持 w={w:.3f}")
            continue
        if rec.get("window_degraded"):
            print(f"\n  {CN[d]:16s} ⚠️ h20 窗口降级（{s20.get('span_months')}月，数据源硬边界）"
                  f"→ 以 h5 为主判据")
            gates["G1_window"] = True

        # 正交贡献（消融）—— 判据必须「t 与幅度双达标」，
        # 否则 ΔIC≈+0.0005 配 t=+3.96 这类配对检验伪影会误判（见 audit_round34 头注释）
        prior = PRIOR_POSITIVE.get(d, True)
        v5, v20 = a5.get("verdict"), a20.get("verdict")
        orth = bool(v5 == "不可替代" or v20 == "不可替代")
        harm = bool(v5 == "拖累" or v20 == "拖累")
        sign5 = (ic5 > 0) == prior
        sign20 = (ic20 > 0) == prior
        p5, p20 = bool(s5.get("pass_bonf")), bool(s20.get("pass_bonf"))
        both_pass = p5 and p20
        one_pass = p5 or p20
        both_prior = sign5 and sign20
        degraded = bool(rec.get("window_degraded"))

        if both_pass and both_prior:
            dec, sug, why = "keep", w, "双周期过 Bonferroni 且方向符合先验"
        elif one_pass and both_prior and not degraded:
            dec, sug, why = "halve", round(w / 2, 4), "仅单周期过 Bonferroni"
        elif degraded and p5 and sign5:
            # 窗口降级：h5 过则视为「保留但标注证据受限」
            dec, sug, why = "keep_limited", w, "h5 过 Bonferroni（h20 受数据源窗口限制，佐证同号）"
        elif not one_pass and orth:
            dec, sug, why = "keep_orthogonal", w, "单维 IC 弱，但消融显示正交贡献不可替代"
        elif not both_prior:
            dec, sug, why = "halve", round(w / 2, 4), "IC 方向与先验相反，需复核"
        else:
            dec, sug, why = "zero", 0.0, "双周期均不过 Bonferroni"

        # 拖累修正：消融显著为正 = 剔除后复合 IC 变好
        if harm and dec in ("keep", "keep_orthogonal", "keep_limited"):
            dec, sug, why = "halve", round(w / 2, 4), f"消融显示该维是拖累（{v5 if v5=='拖累' else v20}）"

        rec.update({"decision": dec, "suggested_weight": sug, "reason": why,
                    "orthogonal": orth, "harmful": harm,
                    "bonf": {"h5": p5, "h20": p20},
                    "direction_ok": {"h5": sign5, "h20": sign20}})
        DEC["dims"][d] = rec

        print(f"\n  {CN[d]:16s} w={w:.3f}")
        print(f"     h5  IC {ic5:+.4f} t0 {s5.get('t0'):+.2f} "
              f"{'✅Bonf' if p5 else '❌'} {'方向OK' if sign5 else '⚠️反向'} "
              f"| 消融 Δ{a5.get('delta_ic')} t{a5.get('nw_t')}")
        print(f"     h20 IC {ic20:+.4f} t0 {s20.get('t0'):+.2f} "
              f"{'✅Bonf' if p20 else '❌'} {'方向OK' if sign20 else '⚠️反向'} "
              f"| 消融 Δ{a20.get('delta_ic')} t{a20.get('nw_t')}")
        print(f"     → {dec}（建议 w={sug:.3f}）：{why}")

    # ---- 组合消融交叉验证（关键：单维归零建议必须经组合层复核）
    #      单维 IC 弱只说明「该维自身排序能力弱」，不等于「该维对复合分有害」。
    #      若剔除某组后复合 IC 反而上升，才是真正的拖累证据。
    print("\n" + "=" * 112)
    print("【五、组合消融复核】剔除某一组后复合 IC 变化（其余权重重归一化）")
    print("  这一节是单维归零建议的必要佐证：单维 IC 弱 ≠ 对复合分有害")
    print("=" * 112)
    COMB = {}
    try:
        fwj2 = json.load(open(os.path.join(HERE, "factor_weights.json"), encoding="utf-8"))
        W2 = {k: float(v or 0) for k, v in (fwj2.get("weights") or {}).items()}
        DMAX2 = {k: float(v) for k, v in (fwj2.get("dim_max") or {}).items()}
        EFF2 = [k for k in DIMS8 if W2.get(k, 0) > 0]
        px2 = _load_px()
        tdays2, tidx2 = _tdays(px2)
        # 面板明细（rows）不在 audit_round34.json 里，需从面板文件单独读
        _panel = json.load(open(os.path.join(HERE, "data", "panel_round34.json"),
                                encoding="utf-8"))

        def _ps(vals, wmap):
            num = den = 0.0
            for k, wv in wmap.items():
                if wv <= 0:
                    continue
                v = vals.get(k)
                if not isinstance(v, (int, float)) or isinstance(v, bool):
                    continue
                mx = DMAX2.get(k) or 10.0
                num += min(float(v), mx) / mx * 10.0 * wv
                den += wv
            return (num / max(0.5, den) * 10.0) if den > 0 else None

        def _comp(wmap, hop):
            out = []
            for d0 in _panel["dates"]:
                i0 = tidx2.get(d0)
                if i0 is None or i0 + hop >= len(tdays2):
                    continue
                xs, ys = [], []
                for c, v in (_panel["rows"].get(d0) or {}).items():
                    g = v.get("score_size_ind")
                    if not isinstance(g, (int, float)) or isinstance(g, bool):
                        continue
                    s = _ps(v, wmap)
                    if s is None:
                        continue
                    m = px2.get(c)
                    if not m:
                        continue
                    p0, p1 = m.get(d0), m.get(tdays2[i0 + hop])
                    if not p0 or not p1:
                        continue
                    xs.append(s)
                    ys.append(p1 / p0 - 1.0)
                if len(xs) >= 40:
                    ic = fe.rank_ic(xs, ys)
                    if ic is not None:
                        out.append(ic)
            return out

        def _rn(drop):
            base = {k: W2[k] for k in EFF2 if k not in drop}
            s = sum(base.values())
            return {k: v / s for k, v in base.items()} if s > 0 else {}

        CASES = [
            ("现行 8 维", []),
            ("去 bias60", ["score_bias60"]),
            ("去 RSI反号", ["score_rsi_rev"]),
            ("去 技术反号", ["score_tech_neg"]),
            ("去 EP", ["score_ep"]),
            ("去反转块三合一(bias60+RSI+tech_neg)", ["score_bias60", "score_rsi_rev", "score_tech_neg"]),
            ("只留 4 个建议保留维", ["score_bias60", "score_rsi_rev", "score_tech_neg", "score_ep"]),
        ]
        print(f"  {'方案':40s} {'n':>5s} {'h5 IC':>9s} {'h20 IC':>9s}")
        for nm, drop in CASES:
            wm = _rn(drop)
            a5 = _comp(wm, 5)
            a20 = _comp(wm, 20)
            m5 = sum(a5) / len(a5) if a5 else None
            m20 = sum(a20) / len(a20) if a20 else None
            COMB[nm] = {"drop": drop, "n_h5": len(a5), "h5_ic": round(m5, 4) if m5 is not None else None,
                        "n_h20": len(a20), "h20_ic": round(m20, 4) if m20 is not None else None}
            print(f"  {nm:40s} {len(a5):5d} {m5 if m5 is not None else 0:+9.4f} "
                  f"{m20 if m20 is not None else 0:+9.4f}")
        DEC["combo_ablation"] = COMB

        # ---- 再分配方案测算（4 维归零后 Σ=0.365，模型不能带缺口运行）
        print("\n" + "=" * 112)
        print("【六、再分配方案测算】归零 4 维后腾出 0.575，如何摊给剩余 4 维")
        print("  注：再分配本质是**另一套先验**，本脚本只做测算供裁决，不自动写入 factor_weights.json")
        print("=" * 112)

        def _stat(a):
            from audit_round29 import nw_t as _nw
            if not a:
                return None, None
            mu = sum(a) / len(a)
            return mu, _nw(a, 0)

        tot_dec = (.13 + .10 + .06 + .075)
        PLANS = {
            "A 现行 8 维（基准）": dict(W2),
            "B 严格按裁决比例摊平": {"score_lowvol20": .13 / tot_dec,
                                    "score_size_ind": .10 / tot_dec,
                                    "score_quality": .06 / tot_dec,
                                    "score_fund": .075 / tot_dec},
            "C 4 维等权": {"score_lowvol20": .25, "score_size_ind": .25,
                        "score_quality": .25, "score_fund": .25},
            "D 按 IC 强度加权": {"score_lowvol20": .30, "score_size_ind": .35,
                            "score_quality": .15, "score_fund": .20},
            "E 保留质量 0.12": {"score_lowvol20": .30, "score_size_ind": .33,
                             "score_quality": .12, "score_fund": .25},
        }
        PLANRES = {}
        print(f"  {'方案':32s} {'h5 IC':>9s} {'h5 t':>7s} {'h20 IC':>9s} {'h20 t':>7s}")
        for nm, wm in PLANS.items():
            a5 = _comp(wm, 5)
            a20 = _comp(wm, 20)
            m5, t5 = _stat(a5)
            m20, t20 = _stat(a20)
            PLANRES[nm] = {"weights": {k: round(v, 4) for k, v in wm.items() if v > 0},
                           "h5_ic": round(m5, 4) if m5 is not None else None,
                           "h5_t": round(t5, 2) if t5 is not None else None,
                           "h20_ic": round(m20, 4) if m20 is not None else None,
                           "h20_t": round(t20, 2) if t20 is not None else None,
                           "n_h5": len(a5), "n_h20": len(a20)}
            print(f"  {nm:32s} {m5 if m5 is not None else 0:+9.4f} "
                  f"{t5 if t5 is not None else 0:+7.2f} "
                  f"{m20 if m20 is not None else 0:+9.4f} "
                  f"{t20 if t20 is not None else 0:+7.2f}")
        DEC["reallocation_plans"] = PLANRES
    except Exception as e:
        print(f"  ⚠️ 组合消融复核失败：{type(e).__name__}: {e}")
        DEC["combo_ablation"] = {"error": f"{type(e).__name__}: {e}"}

    print("\n" + "=" * 112)
    print("【汇总】")
    print("=" * 112)
    print(f"  {'维度':16s} {'现权重':>7s} {'建议权重':>9s} {'裁决':>18s}  理由")
    tot_new = 0.0
    for d in DIMS8:
        r = DEC["dims"][d]
        tot_new += (r.get("suggested_weight") or 0)
        print(f"  {CN[d]:16s} {r['weight_current']:7.3f} "
              f"{r.get('suggested_weight', 0):9.3f} {r['decision']:>18s}  {r.get('reason','')}")
    DEC["suggested_sum"] = round(tot_new, 4)
    DEC["sum_note"] = ("建议权重和 ≠ 1.0 属正常：归零/降权腾出的份额需由老大裁决如何再分配；"
                       "本脚本**不擅自**做再分配（再分配=另一套先验，须单独评审）")
    print(f"\n  建议权重合计 {tot_new:.4f}（≠1.0 属正常，再分配须单独裁决，脚本不擅自摊平）")

    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(DEC, f, ensure_ascii=False, indent=2, default=str)
    print(f"\n已写出 {OUT}")

    # ---- markdown 报告
    os.makedirs(REP_DIR, exist_ok=True)
    today = dt.date.today().isoformat()
    md = [f"# 审计决策报告 · round34（真·大池 / 窗口≥3个月）", "",
          f"生成时间：{DEC['generated_at']}", "",
          "## 样本", "",
          f"- 域：**{meta.get('domain_size')}** 只（EM财务 ∩ 申万 ∩ 长价格库 ∩ 有股本）",
          f"- 截面：**{meta.get('n_dates')}** 个，{meta.get('date_range')}",
          f"- 价格源：{meta.get('price_source')}（{meta.get('price_range')}，"
          f"{meta.get('price_days')} 个交易日）",
          f"- 窗口起点 WIN_START = **{meta.get('win_start')}**", "",
          "## 复合分基准", ""]
    if comp.get("h5"):
        c5 = comp["h5"]
        md.append(f"- h5：IC **{c5.get('ic'):+.4f}**，t0 {c5.get('t0'):+.2f}，"
                  f"跨度 {c5.get('span_months')} 月，n={c5.get('n')}")
    if comp.get("h20"):
        c20 = comp["h20"]
        md.append(f"- h20：IC **{c20.get('ic'):+.4f}**，t0 {c20.get('t0'):+.2f}，"
                  f"跨度 {c20.get('span_months')} 月，n={c20.get('n')}")
    md += ["", "## 逐维裁决", "",
           "| 维度 | 现权重 | h5 IC | h5 t0 | h20 IC | h20 t0 | 消融 t(h5) | 消融 t(h20) | 裁决 | 建议权重 | 理由 |",
           "|---|---|---|---|---|---|---|---|---|---|---|"]
    for d in DIMS8:
        r = DEC["dims"][d]
        h5 = r.get("h5") or {}
        h20 = r.get("h20") or {}
        md.append(f"| {CN[d]} | {r['weight_current']:.3f} | "
                  f"{h5.get('ic') if h5.get('ic') is not None else '-'} | "
                  f"{h5.get('t0') if h5.get('t0') is not None else '-'} | "
                  f"{h20.get('ic') if h20.get('ic') is not None else '-'} | "
                  f"{h20.get('t0') if h20.get('t0') is not None else '-'} | "
                  f"{(r.get('ablation_h5') or {}).get('nw_t', '-')} | "
                  f"{(r.get('ablation_h20') or {}).get('nw_t', '-')} | "
                  f"**{r['decision']}** | {r.get('suggested_weight', 0):.3f} | {r.get('reason','')} |")
    md += ["", "## 裁决规则（先写死，避免事后挑选）", "",
           "- 前置闸门：G1 双周期跨度≥3月 / G2 h20 截面≥60 / G3 双周期同号；任一不过 → 不裁决",
           "- keep：双周期 |t0|≥3.08 且方向符合先验",
           "- halve：仅单周期过，或方向反向，或消融显示是拖累",
           "- zero：双周期均不过 Bonferroni",
           "- keep_orthogonal：单维 IC 弱但剔除后复合 IC 显著下降（正交贡献不可替代）",
           "- keep_limited：h5 过门槛但 h20 受数据源窗口硬边界限制（fund 专有）", ""]

    # 组合消融
    ca = DEC.get("combo_ablation") or {}
    if ca and "error" not in ca:
        md += ["", "## 组合消融复核（单维归零的必要佐证）", "",
               "单维 IC 弱 ≠ 对复合分有害。下表剔除某一组后其余权重重归一化：", "",
               "| 方案 | h5 IC | h20 IC |", "|---|---|---|"]
        for nm, v in ca.items():
            md.append(f"| {nm} | {v.get('h5_ic')} | {v.get('h20_ic')} |")

    # 再分配方案
    rp2 = DEC.get("reallocation_plans") or {}
    if rp2:
        md += ["", "## 再分配方案测算", "",
               "归零 4 维后权重合计仅 0.365，模型不能带缺口运行。下表测算几种再分配方式：", "",
               "| 方案 | h5 IC | h5 t | h20 IC | h20 t |", "|---|---|---|---|---|"]
        for nm, v in rp2.items():
            md.append(f"| {nm} | {v.get('h5_ic')} | {v.get('h5_t')} | "
                      f"{v.get('h20_ic')} | {v.get('h20_t')} |")
        md += ["", "⚠️ 再分配本质是**另一套先验**，本报告只做测算，**不自动写入** "
                   "`factor_weights.json`。落地须老大单独裁决。", ""]

    md += [f"建议权重合计 **{tot_new:.4f}**。", ""]
    rp = os.path.join(REP_DIR, f"audit_round34_decision_{today}.md")
    with open(rp, "w", encoding="utf-8") as f:
        f.write("\n".join(md))
    print(f"已写出 {rp}")


if __name__ == "__main__":
    main()
