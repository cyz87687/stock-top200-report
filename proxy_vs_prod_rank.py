"""proxy_vs_prod_rank.py —— 同一交易日 代理值 vs 生产值 秩相关监控（round141 / P0-3）。

数据源（均为项目本地真实产物）：
  代理侧  data/panel_r124_mat.npz     长面板 9 维，factor_parity=False / PROXY_ONLY
  生产侧  factor_history.json          日更快照 98 截面，生产逐维算法

判读标准沿用 probe_fund_calib.py:229-233（唯一口径，不另设阈值）：
  rho >= 0.85 可视为等价 / 0.70~0.85 仅代理 / < 0.70 红色告警，不得回填

⚠️ 诚实边界：本地不存在「带公告版本/修订号的 PIT vintage」数据集
（data/pit_audit_round130.json：0 / 272240 行），因此本脚本比对的是
「长面板代理值 vs 生产快照分」，**不是**「代理值 vs 严格 PIT 值」。
后者须等外部多版本财务库接入后才有对照对象。
"""
from __future__ import annotations

import json
import math
import os
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
THRESHOLD = 0.70
EQUIV_THRESHOLD = 0.85
MIN_PAIRS = 20
OUT_JSON = os.path.join(ROOT, "data", "proxy_vs_prod_rank_20261112.json")


def _ranks(a):
    order = sorted(range(len(a)), key=lambda i: a[i])
    out = [0.0] * len(a)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and a[order[j + 1]] == a[order[i]]:
            j += 1
        avg = (i + j) / 2.0 + 1.0
        for k in range(i, j + 1):
            out[order[k]] = avg
        i = j + 1
    return out


def _pearson(x, y):
    n = len(x)
    if n < MIN_PAIRS:
        return None
    mx, my = sum(x) / n, sum(y) / n
    num = sum((a - mx) * (b - my) for a, b in zip(x, y))
    dx = math.sqrt(sum((a - mx) ** 2 for a in x))
    dy = math.sqrt(sum((b - my) ** 2 for b in y))
    if dx <= 0 or dy <= 0:
        return None
    return num / (dx * dy)


def spearman(xs, ys):
    """Spearman 秩相关（平均秩处理 ties）；样本不足或零方差返回 None。"""
    px, py = [], []
    for a, b in zip(xs, ys):
        try:
            fa, fb = float(a), float(b)
        except (TypeError, ValueError):
            continue
        if math.isnan(fa) or math.isnan(fb):
            continue
        px.append(fa)
        py.append(fb)
    if len(px) < MIN_PAIRS:
        return None
    return _pearson(_ranks(px), _ranks(py))


def load_prod(path):
    with open(path, encoding="utf-8") as f:
        d = json.load(f)
    out = {}
    for s in d.get("snapshots", []):
        m = {}
        for r in s.get("rows", []):
            c = str(r.get("code", "")).zfill(6)
            m[c] = r
        out[str(s.get("date"))[:10]] = m
    return out


def load_proxy(path):
    import numpy as np
    with np.load(path, allow_pickle=True) as z:
        dates = [str(x)[:10] for x in z["dates"]]
        codes = [str(x).zfill(6) for x in z["codes"]]
        dims = [str(x) for x in z["dims"]]
        S = z["S"]
    return dates, codes, dims, S


def build_report(root=ROOT):
    prod = load_prod(os.path.join(root, "factor_history.json"))
    dates, codes, dims, S = load_proxy(
        os.path.join(root, "data", "panel_r124_mat.npz"))
    # ⚠️ 生产快照自身有历史字段断层：早期截面仅 7 个键，不能用首个截面代表全集。
    # 实测 2026-06-26 ~ 2026-09-07 为 7 键，2026-10-19 起 16 键，
    # 2026-11-02 起 24 键，2026-11-11 为 25 键。故取全截面并集并逐日留痕。
    prod_keys = set()
    field_coverage = []
    for date in sorted(prod):
        m = prod[date]
        first = next(iter(m.values()), {})
        keys = set(first.keys()) if isinstance(first, dict) else set()
        prod_keys |= keys
        field_coverage.append({
            "date": date,
            "n_fields": len(keys),
            "n_rows": len(m),
            "has_core_dims": all(d in keys for d in
                                 ("score_bias60", "score_rsi_rev",
                                  "score_quality", "score_growth")),
        })
    common_dims = [d for d in dims if d in prod_keys]
    panel_only = [d for d in dims if d not in prod_keys]
    prod_only = sorted(k for k in prod_keys
                       if k.startswith("score_") and k not in dims)
    date_matches = sorted(set(dates) & set(prod))

    records = []
    for di, date in enumerate(dates):
        pm = prod.get(date)
        if not pm:
            continue
        for k, dim in enumerate(dims):
            if dim not in common_dims:
                continue
            xs, ys = [], []
            for ci, c in enumerate(codes):
                r = pm.get(c)
                if r is None:
                    continue
                v = r.get(dim)
                a = float(S[di, ci, k])
                if v is None or math.isnan(a):
                    continue
                xs.append(a)
                ys.append(v)
            rho = spearman(xs, ys)
            if rho is not None:
                records.append({"date": date, "dim": dim,
                                "rho": round(rho, 4), "n": len(xs)})

    by_dim = {}
    for d in common_dims:
        rr = [x["rho"] for x in records if x["dim"] == d]
        if not rr:
            by_dim[d] = {"verdict": "NO_OVERLAP"}
            continue
        mean = sum(rr) / len(rr)
        by_dim[d] = {
            "n_days": len(rr),
            "rho_mean": round(mean, 4),
            "rho_min": round(min(rr), 4),
            "rho_max": round(max(rr), 4),
            "n_below_threshold": sum(1 for v in rr if v < THRESHOLD),
            "verdict": ("RED_ALERT_NOT_EQUIVALENT" if mean < THRESHOLD else
                        "PROXY_ONLY" if mean < EQUIV_THRESHOLD else
                        "EQUIVALENT_CANDIDATE"),
        }

    overlap_dates = sorted({x["date"] for x in records})
    alerts = [{"dim": d, "rho_mean": v["rho_mean"], "n_days": v["n_days"]}
              for d, v in by_dim.items()
              if v.get("verdict") == "RED_ALERT_NOT_EQUIVALENT"]
    return {
        "purpose": "同一交易日 代理面板值 vs 生产快照分 的 Spearman 秩相关",
        "generated_at_note": "由 proxy_vs_prod_rank.py 实跑生成",
        "method": {
            "rho": "Spearman（平均秩处理 ties），n >= %d" % MIN_PAIRS,
            "thresholds": {"equivalence": EQUIV_THRESHOLD, "red_alert": THRESHOLD},
            "threshold_source": "probe_fund_calib.py:229-233 唯一判读口径",
        },
        "honesty_boundary": (
            "本地无 PIT vintage 数据（0/272240 行带公告时点/修订号），"
            "本对照是「代理 vs 生产快照」，不是「代理 vs 严格 PIT」；"
            "长面板 factor_parity=False / PROXY_ONLY，结论仅用于因子对齐排期。"
            "另：生产快照存在历史字段断层，早期截面无八维因子分，"
            "故秩相关只在字段实际存在的日期上计算，不补齐、不插值。"),
        "panel": {"dims": dims, "panel_only_dims": panel_only,
                  "n_dates": len(dates), "date_range": [dates[0], dates[-1]]},
        "prod": {"n_snapshots": len(prod),
                 "prod_only_score_dims": prod_only,
                 "n_union_fields": len(prod_keys),
                 "field_coverage": field_coverage,
                 "n_dates_with_core_dims": sum(
                     1 for x in field_coverage if x["has_core_dims"])},
        "overlap": {"n_date_matches": len(date_matches),
                    "n_dates_with_rho": len(overlap_dates),
                    "range": ([overlap_dates[0], overlap_dates[-1]]
                              if overlap_dates else []),
                    "n_common_dims": len(common_dims),
                    "common_dims": common_dims},
        "by_dim": by_dim,
        "alerts": alerts,
        "records": records,
    }


def main():
    out = sys.argv[1] if len(sys.argv) > 1 else OUT_JSON
    r = build_report()
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        json.dump(r, f, ensure_ascii=False, indent=1)
    print("date matches: %d, dates with rho: %d, common dims: %d, records: %d" % (
        r["overlap"]["n_date_matches"], r["overlap"]["n_dates_with_rho"],
        r["overlap"]["n_common_dims"], len(r["records"])))
    print("prod snapshots with core dims: %d / %d" % (
        r["prod"]["n_dates_with_core_dims"], r["prod"]["n_snapshots"]))
    print("panel only dims (no prod counterpart):", r["panel"]["panel_only_dims"])
    print("prod only score dims (no panel dim):", r["prod"]["prod_only_score_dims"])
    print("\ndim                       n_days  rho_mean  rho_min  rho_max  <0.70  verdict")
    for d, v in r["by_dim"].items():
        if "rho_mean" not in v:
            print("%-26s %s" % (d, v["verdict"]))
            continue
        print("%-26s %6d  %8.4f %8.4f %8.4f %6d  %s" % (
            d, v["n_days"], v["rho_mean"], v["rho_min"], v["rho_max"],
            v["n_below_threshold"], v["verdict"]))
    print("\nRED alerts:", len(r["alerts"]))
    print("written:", out)


if __name__ == "__main__":
    main()
