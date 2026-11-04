#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
guard_ic_driven_weights.py —— 「禁按 IC 排权重」可执行护栏
=========================================================================
把项目里那条只写在记忆/注释里的铁律变成**能被代码拦住**的约束。

实测依据（data/weight_scheme_curve.json + data/dim_ic_long.json，round124）：
    · lowvol20  IC +0.0828 / NW t 4.96（最强）→ TopN **全区间负**（N10 −0.73% … N300 −0.04%）
    · size_growth IC +0.0337 / t 2.27（弱）   → TopN **全程正**（+0.89 ~ +1.29%）
    · 现行复合自己：N10 −0.531% / N50 +0.578% / N100 +0.678%
    ⇒ IC 衡量的是**全池排序**，生产只持 6~13 只，目标函数是**头部超额**。
      两者方向相反，把 IC 排名映射成权重会系统性地买错。

三条护栏：
    G-A 目标函数锁：任何权重变更必须附 TopN 超额对照（N6/N10/N13/N30/N50），
                     只有 IC / t 值不算证据。
    G-B 反向检测：候选权重若与 |IC| 排名高度同序（Spearman ≥ 0.70）
                     且无 TopN 验证 → REJECT。
    G-C 证据准入：factor_ic.json 里 cross_section_audit 判 usable=False 的
                     口径，禁止作为权重依据（门槛下截面数不足，不可复现）。

⚠️ 本模块**不自动改权重**，只做裁决与留痕。改权重仍须 apply_round128_c1.py
   那套八处同步 + verify_weights_round36.py PASS。

用法：
    python3 guard_ic_driven_weights.py                # 审计当前状态 → data/weight_guard_report.json
    python3 guard_ic_driven_weights.py --check-change OLD.json NEW.json --justification J.json
"""
import argparse
import json
import os
import sys
import time

from scipy.stats import spearmanr

HERE = os.path.dirname(os.path.abspath(__file__))
CURVE = os.path.join(HERE, "data", "weight_scheme_curve.json")
DIMIC = os.path.join(HERE, "data", "dim_ic_long.json")
FACTOR_IC = os.path.join(HERE, "factor_ic.json")
FW = os.path.join(HERE, "factor_weights.json")
OUT = os.path.join(HERE, "data", "weight_guard_report.json")

PRODUCTION_HOLDING_RANGE = (6, 13)
TARGET_FUNCTION = "TopN_head_excess"
TOPN_REQUIRED = ("N6", "N10", "N13", "N30", "N50")
RHO_REJECT = 0.70


def _jload(p):
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def evidence_snapshot():
    """从**真实产物**读实证，读不到就如实 unknown，不编数字。"""
    curve, dimic = _jload(CURVE), _jload(DIMIC)
    out = {"curve_available": curve is not None,
           "dim_ic_available": dimic is not None,
           "horizon": (curve or {}).get("horizon"),
           "n_dates": (curve or {}).get("n_dates")}
    if curve:
        out["composite_topn_pct"] = {
            k: v.get("mean_pct")
            for k, v in ((curve.get("results") or {}).get("S0_v417_composite") or {}).items()
            if k in ("N5", "N10", "N15", "N30", "N50", "N100")
        }
        out["caveat"] = curve.get("caveat")
    if dimic:
        # 实际结构（decompose_dim_ic.py 产物）：
        #   {"horizon":20, "dim_ic": {"score_x": {"ic_mean":0.0725, "nw_t":..., ...}}}
        # 兼容旧结构 {dim: {h20: {...}}} / {dim: {"ic":..}}
        src = dimic.get("dim_ic") if isinstance(dimic.get("dim_ic"), dict) else dimic
        ic, nw = {}, {}
        for d, v in src.items():
            if not isinstance(v, dict) or not str(d).startswith("score_"):
                continue
            if v.get("ic_mean") is not None:
                ic[d] = float(v["ic_mean"])
                if v.get("nw_t") is not None:
                    nw[d] = float(v["nw_t"])
                continue
            for key in ("h20", "20", "ic"):
                x = v.get(key)
                if isinstance(x, dict) and x.get("ic_mean") is not None:
                    ic[d] = float(x["ic_mean"])
                    break
                if isinstance(x, (int, float)):
                    ic[d] = float(x)
                    break
        out["dim_ic_h20"] = ic
        out["dim_nw_t_h20"] = nw
        out["dim_ic_horizon"] = dimic.get("horizon")
    return out


def ic_evidence_audit(path=FACTOR_IC):
    """G-C：把 cross_section_audit 的 usable 裁决变成可查询的准入清单。"""
    d = _jload(path)
    if not d:
        return {"available": False, "note": "factor_ic.json 不可读"}
    a = d.get("cross_section_audit") or {}
    s = a.get("_summary") or {}
    banned, allowed = [], []
    for k, v in a.items():
        if k == "_summary" or not isinstance(v, dict):
            continue
        rec = {"scope": k, "usable": bool(v.get("usable")),
               "cross_section_median": v.get("cross_section_median"),
               "n_after_gate": v.get("n_after_gate"),
               "n_below_gate": v.get("n_below_gate"),
               "gated_note": v.get("gated_note")}
        (allowed if rec["usable"] else banned).append(rec)
    return {
        "available": True,
        "min_cross_section_gate": s.get("min_cross_section_gate"),
        "median_of_medians": s.get("median_of_medians"),
        "production_pool_size": s.get("production_pool_size"),
        "n_usable": len(allowed),
        "n_banned": len(banned),
        "usable_scopes": allowed,
        "banned_scopes": banned,
        "rule": ("usable=False 的口径在门槛下截面数不足、IC 不可复现，"
                 "禁止作为权重依据；长面板结论须改引 data/ic_long_2020.json"
                 "（median_cs 通常 >1500）。"),
    }


def ic_rank_agreement(weights, dim_ic):
    """G-B：权重与 |IC| 排名的 Spearman 同序度。"""
    common = [d for d in weights if d in dim_ic and float(weights[d] or 0) > 0]
    if len(common) < 3:
        return None, len(common)
    w = [float(weights[d]) for d in common]
    ic = [abs(float(dim_ic[d])) for d in common]
    rho = float(spearmanr(w, ic).statistic)
    return rho, len(common)


def check_change(old_w, new_w, justification, evidence=None):
    """G-A + G-B：裁决一次权重变更是否有合法依据。返回 (verdict, detail)。"""
    ev = evidence or evidence_snapshot()
    j = justification or {}
    has_topn = all(k in (j.get("topn_excess") or {}) for k in TOPN_REQUIRED)
    has_topn = has_topn or bool(j.get("topn_curve_path"))
    rho, n = ic_rank_agreement(new_w, ev.get("dim_ic_h20") or {})
    changed = {k: (old_w.get(k), new_w.get(k)) for k in set(old_w) | set(new_w)
               if abs(float(old_w.get(k, 0) or 0) - float(new_w.get(k, 0) or 0)) > 1e-9}
    reasons = []
    if not changed:
        return "NO_CHANGE", {"changed": {}}
    if not has_topn:
        reasons.append("缺 TopN 超额对照（N6/N10/N13/N30/N50）；只有 IC/t 值不算证据")
    if rho is not None and rho >= RHO_REJECT and not has_topn:
        reasons.append(f"权重与 |IC| 排名同序度 ρ={rho:.3f} ≥ {RHO_REJECT}，"
                       f"疑似直接按 IC 排权重")
    if str(j.get("target_function") or "") not in ("", TARGET_FUNCTION):
        reasons.append(f"目标函数写成 {j.get('target_function')}，"
                       f"应为 {TARGET_FUNCTION}")
    return ("REJECT" if reasons else "ALLOW"), {
        "changed": changed,
        "ic_rank_rho": rho,
        "n_common_dims": n,
        "has_topn_evidence": has_topn,
        "target_function": j.get("target_function") or TARGET_FUNCTION,
        "production_holding_range": list(PRODUCTION_HOLDING_RANGE),
        "reasons": reasons,
        "evidence_horizon": ev.get("horizon"),
        "evidence_n_dates": ev.get("n_dates"),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--check-change", nargs=2, metavar=("OLD", "NEW"))
    ap.add_argument("--justification", default=None)
    ap.add_argument("--out", default=OUT)
    args = ap.parse_args()

    ev = evidence_snapshot()
    audit = ic_evidence_audit()
    report = {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "rule": "禁按 IC 排权重 · 目标函数 = TopN 头部超额",
        "target_function": TARGET_FUNCTION,
        "production_holding_range": list(PRODUCTION_HOLDING_RANGE),
        "topn_required": list(TOPN_REQUIRED),
        "evidence": ev,
        "ic_evidence_audit": audit,
        "current_weights_file": FW,
        "current_version": (_jload(FW) or {}).get("version"),
    }

    if args.check_change:
        old, new = _jload(args.check_change[0]), _jload(args.check_change[1])
        j = _jload(args.justification) if args.justification else {}
        verdict, detail = check_change(old or {}, new or {}, j or {}, ev)
        report["change_check"] = {"verdict": verdict, **detail}
        print(f"变更裁决: {verdict}")
        for r in detail.get("reasons") or []:
            print("   ❌ " + r)
        rc = 2 if verdict == "REJECT" else 0
    else:
        cur = _jload(FW) or {}
        w = {k: float(v) for k, v in (cur.get("weights") or {}).items()
             if float(v or 0) > 0}
        rho, n = ic_rank_agreement(w, ev.get("dim_ic_h20") or {})
        report["current_state"] = {
            "ic_rank_rho": rho, "n_common_dims": n,
            "note": ("权重与 |IC| 同序度低 = 未按 IC 排序（符合 C1 裁决方向）"
                     if rho is not None and rho < RHO_REJECT
                     else "同序度偏高或不可测，须人工复核"),
        }
        print(f"当前权重与 |IC| 同序度 ρ={rho}（{n} 个共同维，拒绝线 {RHO_REJECT}）")
        print(f"短面板 IC 准入：可用 {audit.get('n_usable')} 个口径 / "
              f"禁用 {audit.get('n_banned')} 个；"
              f"截面中位 {audit.get('median_of_medians')} / "
              f"门槛 {audit.get('min_cross_section_gate')}")
        print(f"实证曲线：h={ev.get('horizon')} / {ev.get('n_dates')} 日 / "
              f"现行组合 TopN 超额 {ev.get('composite_topn_pct')}")
        rc = 0

    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=1)
    print(f"写出 {args.out}")
    return rc


if __name__ == "__main__":
    sys.exit(main())
