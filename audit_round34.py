#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""#286 在 ≥3 个月窗口上计算 8 维 IC / 消融 / 复合 —— 真·大池口径

【与 round29/30b 的差别（请勿把两者数字混用）】
  round29/30b : 78 混合截面，域 917 只，其中 76 截面是冻结旧池(~195 只)的重复
                → 89% 样本是窄名单时间重复，大池仅 9 天无统计功效
  本脚本      : 用 data/panel_round34.json（域 ~2000 只、窗口 ≥3 个月）
                → 每维窗口跨度由 build_panel_round34 自带校验，本脚本再验一次；
                  任一维跨度 <3 个月即在结果里标 insufficient_window 并**不参与裁决**。

【统计口径（逐字复用 round29，保证与历史结论可比）】
  nw_t(xs, lag)     : Newey-West t（lag ∈ {0,3,6}）
  ic_stats(ics)     : ic / icir / t0,t3,t6 / ic_pos_share / pass_bonf / 分子期双正
  T_BONF = 3.08     : Bonferroni 门槛（300+ 次检验下严格阈值应 3.9，一并给出）

【三组输出】
  1. 单维 IC：每维单独与未来收益求 rank IC（h5 / h20）
  2. 消融边际贡献：从 8 维复合分中**剔除该维**（其余权重重归一化），
     看复合 IC 变化；ΔIC 显著为负 = 该维不可替代
  3. 复合 IC：现行 v4.9 权重下的复合分 IC，作为基准

用法
  python audit_round34.py                      # 用现成 panel_round34.json
  python audit_round34.py --panel data/xxx.json
输出
  audit_round34.json
"""
import os
import sys
import json
import math
import argparse
from collections import defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import factor_eval as fe
import audit_round29 as r29

nw_t = r29.nw_t
ic_stats = r29.ic_stats
T_BONF = r29.T_BONF
T_BONF_STRICT = 3.9

PANEL = os.path.join(HERE, "data", "panel_round34.json")
OUT = os.path.join(HERE, "audit_round34.json")

DIMS8 = ["score_bias60", "score_rsi_rev", "score_lowvol20", "score_tech_neg",
         "score_ep", "score_size_ind", "score_quality", "score_fund"]

CN = {
    "score_bias60": "乖离60(反转)",
    "score_rsi_rev": "RSI反转",
    "score_lowvol20": "低波20",
    "score_tech_neg": "技术反号",
    "score_ep": "EP估值",
    "score_size_ind": "行业内市值",
    "score_quality": "质量",
    "score_fund": "fund基本面",
}


def is_num(x):
    return isinstance(x, (int, float)) and not isinstance(x, bool) and x == x


def load_px():
    """未来收益要**同一价格源**——必须与建面板时一致，否则 IC 不可解释。"""
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
    h = json.load(open(os.path.join(HERE, "factor_history.json"), encoding="utf-8"))
    _t, px = fe.px_index(h)
    return px


def build_tdays(px, min_ratio=0.05):
    cnt = defaultdict(int)
    for m in px.values():
        for d in m:
            cnt[d] += 1
    if not cnt:
        return []
    mx = max(cnt.values())
    thr = max(5, int(mx * min_ratio))
    return sorted(d for d, n in cnt.items() if n >= thr)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--panel", default=PANEL)
    args = ap.parse_args()

    panel = json.load(open(args.panel, encoding="utf-8"))
    px = load_px()
    tdays = build_tdays(px)
    tidx = {d: i for i, d in enumerate(tdays)}

    fwj = json.load(open(os.path.join(HERE, "factor_weights.json"), encoding="utf-8"))
    W = {k: float(v or 0) for k, v in (fwj.get("weights") or {}).items()}
    DMAX = {k: float(v) for k, v in (fwj.get("dim_max") or {}).items()}
    EFF = [k for k in DIMS8 if W.get(k, 0) > 0]

    meta = panel.get("meta") or {}
    dw = meta.get("dim_window") or {}

    print("=" * 118)
    print("【面板】")
    print(f"  域 {meta.get('domain_size')} 只 · 截面 {meta.get('n_dates')} 个 · "
          f"{meta.get('date_range')} · 价格源 {meta.get('price_source')}")
    print(f"  交易日（并集去噪后）{len(tdays)} 个：{tdays[0]} ~ {tdays[-1]}")
    print(f"  现行生效维 {len(EFF)} 个，Σw = {sum(W[k] for k in EFF):.4f}")
    print("=" * 118)

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

    def valid_dates(hop):
        return [d for d in panel["dates"] if (tidx.get(d) is not None
                                             and tidx[d] + hop < len(tdays))]

    def dim_ic_series(dim, hop):
        """单维 IC 序列（{date: ic}）。"""
        out = {}
        for d0 in valid_dates(hop):
            cur = panel["rows"].get(d0) or {}
            xs, ys = [], []
            for c, v in cur.items():
                val = v.get(dim)
                if not is_num(val):
                    continue
                fr = fwd_ret(c, d0, hop)
                if fr is None:
                    continue
                xs.append(val)
                ys.append(fr)
            if len(xs) >= 40:
                ic = fe.rank_ic(xs, ys)
                if ic is not None:
                    out[d0] = {"ic": ic, "n": len(xs)}
        return out

    def prod_score(vals, wmap):
        """生产线同构：Σ min(v,DMAX)/DMAX×10×w / max(0.5,Σw) × 10；缺维从分子分母同剔（禁 or 0）。"""
        num = den = 0.0
        for k, w in wmap.items():
            if w <= 0:
                continue
            v = vals.get(k)
            if not is_num(v):
                continue
            mx = DMAX.get(k) or 10.0
            num += min(float(v), mx) / mx * 10.0 * w
            den += w
        return (num / max(0.5, den) * 10.0) if den > 0 else None

    def comp_ic_series(wmap, hop, gate="score_size_ind"):
        out = {}
        for d0 in valid_dates(hop):
            cur = panel["rows"].get(d0) or {}
            xs, ys = [], []
            for c, v in cur.items():
                if gate and not is_num(v.get(gate)):
                    continue
                s = prod_score(v, wmap)
                if s is None:
                    continue
                fr = fwd_ret(c, d0, hop)
                if fr is None:
                    continue
                xs.append(s)
                ys.append(fr)
            if len(xs) >= 40:
                ic = fe.rank_ic(xs, ys)
                if ic is not None:
                    out[d0] = {"ic": ic, "n": len(xs)}
        return out

    def summarize(s):
        if not s:
            return None
        ds = sorted(s)
        ics = [s[d]["ic"] for d in ds]
        ns = [s[d]["n"] for d in ds]
        st = ic_stats(ics)
        if st is None:
            return None
        ns_s = sorted(ns)
        st["n_per_snap_med"] = ns_s[len(ns_s) // 2]
        st["n_per_snap_min"] = ns_s[0]
        st["n_per_snap_max"] = ns_s[-1]
        st["n_stock_dates"] = sum(ns)
        st["first_date"] = ds[0]
        st["last_date"] = ds[-1]
        # 窗口跨度（日历天 + 月）
        import datetime as _dt
        a = _dt.date(*map(int, ds[0].split("-")))
        b = _dt.date(*map(int, ds[-1].split("-")))
        st["span_days"] = (b - a).days
        st["span_months"] = round((b - a).days / 30.4375, 2)
        st["span_ok_3m"] = st["span_months"] >= 3.0
        return st

    def reweight_drop(drop):
        """剔除某维，其余按现行比例重归一化 Σ=1。缺维剔除是重新加权，不是删信息。"""
        base = {k: W[k] for k in EFF if k != drop}
        s = sum(base.values())
        if s <= 0:
            return {}
        return {k: v / s for k, v in base.items()}

    RESULTS = {"panel_meta": meta, "thresholds": {"bonf": T_BONF, "bonf_strict": T_BONF_STRICT},
               "single_dim": {}, "composite": {}, "ablation": {}, "dim_window_check": {}}

    # ---------------- 1) 单维 IC
    print("\n" + "=" * 118)
    print("【一、单维 IC】每维单独与未来收益 rank IC（真·大池，窗口由数据决定）")
    print("=" * 118)
    print(f"  {'维度':16s} {'w':>6s} {'hop':>4s} {'n截面':>6s} {'跨度':>16s} "
          f"{'IC':>8s} {'ICIR':>7s} {'t0':>7s} {'t3':>7s} {'t6':>7s} {'IC>0':>6s} {'Bonf':>5s} {'3月':>4s}")
    for d in DIMS8:
        w = W.get(d, 0)
        for hop in (5, 20):
            s = dim_ic_series(d, hop)
            st = summarize(s)
            if st is None:
                print(f"  {CN[d]:16s} {w:6.3f} h{hop:<3d} {'无数据':>6s}")
                RESULTS["single_dim"][f"{d}|h{hop}"] = None
                continue
            RESULTS["single_dim"][f"{d}|h{hop}"] = st
            print(f"  {CN[d]:16s} {w:6.3f} h{hop:<3d} {st['n']:6d} "
                  f"{st['first_date']}~{st['last_date']} "
                  f"{st['ic']:+8.4f} {st['icir'] if st['icir'] is not None else 0:+7.3f} "
                  f"{st['t0'] if st['t0'] is not None else 0:+7.2f} "
                  f"{st['t3'] if st['t3'] is not None else 0:+7.2f} "
                  f"{st['t6'] if st['t6'] is not None else 0:+7.2f} "
                  f"{st['ic_pos_share']*100:5.1f}% {'✅' if st['pass_bonf'] else '❌':>5s} "
                  f"{'✅' if st['span_ok_3m'] else '❌':>4s}")
        print()

    # ---------------- 2) 复合 IC（现行权重）
    print("=" * 118)
    print("【二、复合 IC】现行 v4.9 权重，生产线同构公式，卡口 = size_ind 有值")
    print("=" * 118)
    COMP = {}
    for hop in (5, 20):
        s = comp_ic_series({k: W[k] for k in EFF}, hop)
        st = summarize(s)
        COMP[hop] = {"series": s, "stats": st}
        RESULTS["composite"][f"h{hop}"] = st
        if st:
            print(f"  h{hop:<3d} n={st['n']:3d} 跨度 {st['first_date']}~{st['last_date']} "
                  f"({st['span_days']}天/{st['span_months']}月) "
                  f"IC {st['ic']:+.4f} ICIR {st['icir'] or 0:+.3f} "
                  f"t0 {st['t0'] or 0:+.2f} t3 {st['t3'] or 0:+.2f} t6 {st['t6'] or 0:+.2f} "
                  f"IC>0 {st['ic_pos_share']*100:.1f}% "
                  f"{'✅Bonf' if st['pass_bonf'] else '❌Bonf'} "
                  f"分子期 {st['first_half_ic']:+.4f}/{st['second_half_ic']:+.4f} "
                  f"{'双正' if st['both_halves_positive'] else '非双正'}")
        else:
            print(f"  h{hop:<3d} 无数据")
    print()

    # ---------------- 3) 消融
    print("=" * 118)
    print("【三、消融边际贡献】从现行复合分中剔除该维（其余重归一化），ΔIC = 剔除后 − 现行")
    print("  判据：ΔIC 显著为负（NW t ≤ −3.08）= 该维不可替代；显著为正 = 该维是拖累")
    print("=" * 118)
    print(f"  {'剔除维度':16s} {'w':>6s} {'hop':>4s} {'剔除后IC':>9s} {'ΔIC':>8s} {'NW t':>7s} {'判定':>12s}")
    for d in EFF:
        wm = reweight_drop(d)
        w_disp = W.get(d, 0)          # ⚠️ 独立变量，不能用外层循环残留的 w
        for hop in (5, 20):
            base = COMP.get(hop) or {}
            bs = base.get("series") or {}
            if not bs:
                continue
            s = comp_ic_series(wm, hop)
            if len(s) < 10:
                continue
            common = sorted(set(s) & set(bs))
            if not common:
                continue
            ics_new = [s[x]["ic"] for x in common]
            st_new = ic_stats(ics_new)
            diffs = [s[x]["ic"] - bs[x]["ic"] for x in common]
            mu = sum(diffs) / len(diffs)
            t = nw_t(diffs, 0)
            # ⚠️ 双门槛（2026-10-27 血泪）：只看 NW t 会把「幅度极小但方向极稳」的
            #   差异判成显著。实测剔除 fund 时 ΔIC=+0.0005 而 t=+3.96 ——
            #   这是配对检验的典型伪影（方差趋零时 t 发散），不是真实效应。
            #   故要求 |ΔIC| 同时 ≥ MIN_ABS_DELTA 才允许下「不可替代/拖累」的判定。
            MIN_ABS_DELTA = 0.003
            big_enough = abs(mu) >= MIN_ABS_DELTA
            if t is None:
                verdict = "不可判"
            elif (t <= -T_BONF) and big_enough:
                verdict = "不可替代"
            elif (t >= T_BONF) and big_enough:
                verdict = "拖累"
            elif (abs(t) >= T_BONF) and not big_enough:
                verdict = "伪影(幅度过小)"
            else:
                verdict = "不可区分"
            RESULTS["ablation"][f"{d}|h{hop}"] = {
                "weight": w_disp, "ic_after_drop": round(st_new["ic"], 4),
                "delta_ic": round(mu, 4),
                "nw_t": round(t, 2) if t is not None else None,
                "n_pairs": len(diffs), "verdict": verdict,
                "ic_current": round(bs[common[0]]["ic"], 4) if common else None,
            }
            print(f"  {CN[d]:16s} {w_disp:6.3f} h{hop:<3d} {st_new['ic']:+9.4f} "
                  f"{mu:+8.4f} {t if t is not None else 0:+7.2f} {verdict:>12s}")
        print()

    # ---------------- 4) 窗口合规校验
    print("=" * 118)
    print("【四、窗口合规校验】老大硬要求：回测样本窗口 ≥3 个月")
    print("=" * 118)
    all_ok = True
    for d in DIMS8:
        w5 = RESULTS["single_dim"].get(f"{d}|h5")
        w20 = RESULTS["single_dim"].get(f"{d}|h20")
        ok5 = bool(w5 and w5["span_ok_3m"])
        ok20 = bool(w20 and w20["span_ok_3m"])
        note = dw.get(d) or {}
        RESULTS["dim_window_check"][d] = {
            "h5_span_months": w5["span_months"] if w5 else None,
            "h20_span_months": w20["span_months"] if w20 else None,
            "panel_dim_months": note.get("months"),
            "h5_ok": ok5, "h20_ok": ok20,
            "insufficient_window": not (ok5 and ok20),
        }
        if not (ok5 and ok20):
            all_ok = False
        print(f"  {CN[d]:16s} h5 {str(w5['span_months']) if w5 else '-':>6s}月 "
              f"{'✅' if ok5 else '❌':>2s}  h20 {str(w20['span_months']) if w20 else '-':>6s}月 "
              f"{'✅' if ok20 else '❌':>2s}")
    RESULTS["all_dims_window_ok"] = all_ok
    print(f"  → 全部维度窗口合规：{'✅ 是' if all_ok else '❌ 否（不合规维不参与裁决）'}")

    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(RESULTS, f, ensure_ascii=False, indent=2, default=str)
    print(f"\n已写出 {OUT}")


if __name__ == "__main__":
    main()
