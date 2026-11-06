# -*- coding: utf-8 -*-
"""round131 任务1：随机抽取 20 只个股做得分逻辑体检（2026-11-06）。

抽样：top200_scored_2026-11-05.json（2190 只）按 code 排序后
     random.Random(20261106).sample(..., 20)——固定种子可复现。

体检项（逐股）：
  ① 总分复算：scoring_core.scalar_total（唯一内核）vs 行存 total，容差 0.05
     —— 任何不一致都是 P0 级发现，不是脚本误差；
  ② 分维量程：加权各维值 ∈ [0, dim_max]（超界 = 截断前泄出）；
  ③ 中性填充一致性：值为 None ⟺ 列入 neutral_filled（禁「or 0」冒充实测）；
  ④ 成长锚定门槛：cagr3 缺失/≤0 时 score_growth 与 score_size_growth ≤ 5.0
     （growth_factor_b.anchor_gate，step2 与 enhance:292 两处落地）；
  ⑤ 现金红旗：quality_cash_flags 非空 → 记录（扣分是否落地看 quality 分位对比）；
  ⑥ PEG 留痕合理性：fund_reasons 含 PEG= 时值域 (0, 50]；
  ⑦ 陈旧预期交叉：命中 fwd_2026_refresh 陈旧集的标注「分数按陈旧预期计算」；
  ⑧ 市值方向：score_size_growth 与 log(circ_mv) 的池内秩相关（越小越高分）。

产物：data/audit_round131_random20.json + reports/round131_random20_score_logic_2026-11-06.md
"""
from __future__ import annotations

import json
import math
import os
import random
import re
import sys
from datetime import datetime

import scoring_core

HERE = os.path.dirname(os.path.abspath(__file__))
SEED = 20261106
N_SAMPLE = 20
TOL = 0.05


def _rank_corr(xs, ys):
    n = len(xs)
    if n < 3:
        return None
    def _ranks(v):
        order = sorted(range(n), key=lambda i: v[i])
        r = [0.0] * n
        for pos, i in enumerate(order):
            r[i] = pos
        return r
    rx, ry = _ranks(xs), _ranks(ys)
    mx, my = sum(rx) / n, sum(ry) / n
    num = sum((rx[i] - mx) * (ry[i] - my) for i in range(n))
    den = math.sqrt(sum((rx[i] - mx) ** 2 for i in range(n)) *
                    sum((ry[i] - my) ** 2 for i in range(n)))
    return num / den if den else None


def main():
    with open(os.path.join(HERE, "top200_scored_2026-11-05.json"), encoding="utf-8") as f:
        scored = json.load(f)
    with open(os.path.join(HERE, "factor_weights.json"), encoding="utf-8") as f:
        fw = json.load(f)
    dim_max = fw.get("dim_max") or {}
    weights = scored.get("weights") or {}
    active = {k: float(v) for k, v in weights.items() if float(v or 0) > 0}
    rows = scored.get("results") or []

    stale_codes = {}
    p = os.path.join(HERE, "data", "fwd_2026_refresh_2026-11-06.json")
    if os.path.exists(p):
        with open(p, encoding="utf-8") as f:
            ref = json.load(f)
        for s in ref.get("stale_scan") or []:
            stale_codes[s["code"]] = "stale"
        for s in ref.get("soft_scan") or []:
            stale_codes[s["code"]] = "stale_soft"

    # 池内市值方向参照（score_size_growth 应偏爱小市值）
    pool_mv, pool_sg = [], []
    for r in rows:
        mv = r.get("circ_mv")
        sg = r.get("score_size_growth")
        if isinstance(mv, (int, float)) and mv > 0 and isinstance(sg, (int, float)):
            pool_mv.append(math.log(mv))
            pool_sg.append(sg)
    size_corr = _rank_corr(pool_mv, pool_sg)

    rows_sorted = sorted(rows, key=lambda r: r.get("code") or "")
    rng = random.Random(SEED)
    sample = rng.sample(rows_sorted, N_SAMPLE)
    sample.sort(key=lambda r: -(r.get("total") or 0))

    findings, out_rows = [], []
    n_match = n_range = n_neutral = n_anchor = 0
    for r in sample:
        code, name = r.get("code"), r.get("name")
        checks, contrib = {}, {}
        # ① 总分复算
        vals = [r.get(d) for d in active]
        try:
            total_re = scoring_core.scalar_total(
                vals, list(active), weights, dim_max)
            diff = abs(float(total_re) - float(r.get("total") or 0))
            checks["total_recompute"] = diff <= TOL
            if checks["total_recompute"]:
                n_match += 1
            else:
                findings.append({"code": code, "name": name, "level": "P0",
                                 "issue": "总分复算不一致",
                                 "detail": "kernel=%.4f stored=%.4f diff=%.4f"
                                           % (total_re, r.get("total"), diff)})
            # 分维贡献（归一到百分制后加权）
            for d, w in active.items():
                v = r.get(d)
                mx = float(dim_max.get(d) or 10.0)
                if v is None:
                    norm = 0.5 * 10.0
                else:
                    norm = max(0.0, min(float(v), mx)) / mx * 10.0
                contrib[d] = round(norm * w, 3)
        except Exception as e:
            checks["total_recompute"] = False
            findings.append({"code": code, "name": name, "level": "P0",
                             "issue": "总分复算异常", "detail": str(e)[:200]})
        # ② 量程
        bad_rng = []
        for d in active:
            v = r.get(d)
            mx = float(dim_max.get(d) or 10.0)
            if isinstance(v, (int, float)) and (v < -1e-9 or v > mx + 1e-6):
                bad_rng.append("%s=%s>max%s" % (d, v, mx))
        checks["dim_range"] = not bad_rng
        n_range += (1 if not bad_rng else 0)
        if bad_rng:
            findings.append({"code": code, "name": name, "level": "P1",
                             "issue": "分维超量程", "detail": "; ".join(bad_rng)})
        # ③ 中性填充一致性
        nf = set(r.get("neutral_filled") or [])
        bad_nf = []
        for d in active:
            v = r.get(d)
            if (v is None) != (d in nf):
                bad_nf.append("%s(v=%s,in_nf=%s)" % (d, v, d in nf))
        checks["neutral_fill_consistent"] = not bad_nf
        n_neutral += (1 if not bad_nf else 0)
        if bad_nf:
            findings.append({"code": code, "name": name, "level": "P1",
                             "issue": "neutral_filled 与 None 值不一致",
                             "detail": "; ".join(bad_nf)})
        # ④ 成长锚定门槛
        c3 = r.get("cagr3")
        anchor_bad = []
        if (c3 is None or (isinstance(c3, (int, float)) and c3 <= 0)):
            for d in ("score_growth", "score_size_growth"):
                v = r.get(d)
                if isinstance(v, (int, float)) and v > 5.0 + 1e-6:
                    anchor_bad.append("%s=%s>5.0(cagr3=%s)" % (d, v, c3))
        checks["growth_anchor_gate"] = not anchor_bad
        n_anchor += (1 if not anchor_bad else 0)
        if anchor_bad:
            findings.append({"code": code, "name": name, "level": "P0",
                             "issue": "cagr3 锚定门槛未生效",
                             "detail": "; ".join(anchor_bad)})
        # ⑤ 现金红旗
        cf = r.get("quality_cash_flags") or []
        checks["cash_flags_recorded"] = True
        # ⑥ PEG 留痕
        fr_text = " ".join(r.get("fund_reasons") or [])
        m = re.search(r"PEG=([0-9.]+)", fr_text)
        peg_ok = True
        if m:
            try:
                peg = float(m.group(1))
                peg_ok = (0 < peg <= 50)
            except Exception:
                peg_ok = False
        checks["peg_sanity"] = peg_ok
        if not peg_ok:
            findings.append({"code": code, "name": name, "level": "P1",
                             "issue": "PEG 留痕值异常",
                             "detail": (m.group(0) if m else fr_text[:80])})
        # ⑦ 陈旧预期交叉
        st = stale_codes.get(code)
        # ⑧ 市值
        mv = r.get("circ_mv")
        out_rows.append({
            "code": code, "name": name, "sector": r.get("sector"),
            "total_stored": r.get("total"),
            "checks": checks, "contrib_pct": contrib,
            "key_dims": {d: r.get(d) for d in active},
            "cagr3": c3, "growth_raw": r.get("growth_raw"),
            "pe": r.get("pe"), "fwd_pe": r.get("fwd_pe"),
            "pe_ladder": r.get("pe_ladder"),
            "quality_cash_flags": cf,
            "neutral_filled": r.get("neutral_filled") or [],
            "fwd_2026_status": st,
            "circ_mv_yi": round(mv / 1e8, 1) if isinstance(mv, (int, float)) else None,
            "verdict": "OK" if all(checks.values()) else "REVIEW",
        })

    agg = {
        "n_sample": len(out_rows),
        "n_total_recompute_ok": n_match,
        "n_dim_range_ok": n_range,
        "n_neutral_consistent": n_neutral,
        "n_anchor_gate_ok": n_anchor,
        "pool_size_growth_vs_logmv_rank_corr": size_corr,
        "n_fwd_stale_in_sample": sum(1 for x in out_rows if x["fwd_2026_status"] == "stale"),
        "n_fwd_soft_in_sample": sum(1 for x in out_rows if x["fwd_2026_status"] == "stale_soft"),
        "n_findings": len(findings),
    }
    out = {
        "generated_at": datetime.now().strftime("%Y-%m-%dT%H:%M:%S+08:00"),
        "writer": "audit_round131_random20.py",
        "seed": SEED, "n_sample": N_SAMPLE, "tolerance": TOL,
        "weights_version": scored.get("weights_version"),
        "active_weights": active,
        "aggregate": agg,
        "findings": findings,
        "rows": out_rows,
    }
    out_path = os.path.join(HERE, "data", "audit_round131_random20.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print("✅ 抽样 %d 只（seed=%d）→ %s" % (len(out_rows), SEED, out_path))
    print("   总分复算 %d/%d 一致 | 量程 %d/%d | 中性一致 %d/%d | 锚定门槛 %d/%d"
          % (n_match, len(out_rows), n_range, len(out_rows),
             n_neutral, len(out_rows), n_anchor, len(out_rows)))
    print("   score_size_growth vs log(circ_mv) 秩相关 = %s（负=偏爱小市值）"
          % (("%.4f" % size_corr) if size_corr is not None else "N/A"))
    print("   发现 %d 条" % len(findings))
    for x in findings:
        print("   [%s] %s %s: %s" % (x["level"], x["code"], x["name"], x["issue"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
