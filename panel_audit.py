#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
panel_audit.py —— 研究面板（panel）新鲜度与权重覆盖审计（round117 P0-4）

━━━ 为什么需要这个文件 ━━━
`data/panel_round66.json` 是全项目 36 个脚本共用的**研究面板**（238 个截面 ×
2029 只股票 × 10 维）。它的唯一生产者是 `audit_round66_clean_panel.py` ——
一个**一次性脚本，从未接入日链**。

按项目铁律「一次性脚本不在日链 = 必然陈旧」：
  · panel 的日期范围停在 2026-10-27，而评估当天已是 2026-11-03 → 差 5 个交易日
  · 更严重的是**结构性盲区**：当前 9 维生效权重合计 1.0000，其中
    `score_growth`（权重 0.10）在 panel 里**根本没有这一列**
    → 任何基于 panel 的 IC / 权重门禁检验，都无法评估这 10% 的权重
  ⇒ 所以「G5 未过」不能简单读成「该维度无效」，而是「该维度没被测过」。

━━━ 本文件做什么 ━━━
不重建 panel（重建成本高、且属「补全数据」范畴，老大已明确「先放一下」），
只做**审计与披露**：
  ① panel 新鲜度：末日 vs 最新评分日差几天、缺哪些交易日
  ② 权重覆盖：9 维生效权重里哪些在 panel 有列、哪些是盲区、盲区权重占比
  ③ 落盘 panel_audit.json，供 gen_report 渲染

这样做的理由：**没测 ≠ 通过**。把盲区写在页面上，比假装它通过了更诚实。
"""
import glob
import json
import os
import sys
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
# ── round124 P1-2：优先用 2020 起长面板 ────────────────────────────────
# 旧首位是 panel_round66（238 截面，**无 score_growth 列**）→ 于是本文件
# 每天都在披露「growth 是盲区」，而 panel_round123/124 里 growth 是满覆盖。
# 结论与事实脱节，且 round66 的生产者 audit_round66_clean_panel.py 从未进日链
# → 按「脚本不在日链 = 必然陈旧」，它只会越来越旧。
PANEL_CANDIDATES = ["data/panel_round124_long.json",
                    "data/panel_round123.json",
                    "data/panel_round66.json",
                    "data/panel_round34.json"]

# 大面板的伴生矩阵（pack_panel_npz.py 生成）：用于**免 json.load 全量**
# 998 MB 的单行 JSON 直接 json.load 会把 dict 展开成 4~5 GB，
# 本机 16 GB / free 2.5 GB → 实测被 SIGTERM（Exit 137）。
# 审计只需要「列名 / 日期范围 / 截面规模 / 覆盖数」，这些在 npz 里都有。
MAT_HINTS = {
    "data/panel_round124_long.json": "data/panel_r124_mat.npz",
}


def _load_panel_light(rel):
    """用伴生 npz 做轻量审计，避免整体 json.load 大面板。"""
    import numpy as np
    mat = MAT_HINTS.get(rel)
    if not mat:
        return None
    mp = os.path.join(HERE, mat)
    if not os.path.exists(mp):
        return None
    z = np.load(mp, allow_pickle=False)
    dates = [str(d) for d in z["dates"]]
    codes = [str(c) for c in z["codes"]]
    dims = [str(d) for d in z["dims"]]
    pdims = [str(d) for d in z["pdims"]]
    S = z["S"]
    per_day = (~np.isnan(S).all(axis=2)).sum(axis=1)
    return {
        "meta": {"built_at": "(见 panel_round124_long.json meta)",
                 "src": f"轻量审计：读 {mat}"},
        "dates": dates,
        "domain": codes,
        # 只暴露「有哪些列」，不载入行数据
        "panel_columns": list(dims) + list(pdims),
        "coverage": {d: int((~np.isnan(S[:, :, i])).any(axis=1).sum())
                     for i, d in enumerate(dims)},
        "median_cs": int(np.median(per_day)) if len(per_day) else 0,
        "min_cs": int(per_day.min()) if len(per_day) else 0,
        "_light": True,
    }


def _load_panel():
    for rel in PANEL_CANDIDATES:
        p = os.path.join(HERE, rel)
        if not os.path.exists(p):
            continue
        light = _load_panel_light(rel)
        if light is not None:
            light["_path"] = p
            return p, light
        with open(p, encoding="utf-8") as f:
            return p, json.load(f)
    return None, None


def _latest_scored_dates(n=30):
    """取最近 n 个评分文件的日期（这是「应该有的交易日」的近似）。"""
    fs = sorted(glob.glob(os.path.join(HERE, "top200_scored_*.json")), reverse=True)
    ds = []
    for f in fs:
        b = os.path.basename(f)
        if "_watch_" in b:
            continue
        # top200_scored_YYYY-MM-DD.json
        d = b.replace("top200_scored_", "").replace(".json", "")
        if len(d) == 10 and d[4] == "-":
            ds.append(d)
        if len(ds) >= n:
            break
    return ds


def _eff_weights():
    p = os.path.join(HERE, "factor_weights.json")
    with open(p, encoding="utf-8") as f:
        w = json.load(f)
    ws = w.get("weights") or {}
    eff = {k: v for k, v in ws.items() if isinstance(v, (int, float)) and v > 0}
    return w.get("version"), eff


def audit():
    out = {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "panel_path": None,
        "available": False,
    }

    ppath, panel = _load_panel()
    if panel is None:
        out["error"] = "未找到 panel 文件（data/panel_round66.json / panel_round34.json）"
        return out

    out["panel_path"] = os.path.relpath(ppath, HERE)
    out["available"] = True
    out["built_at"] = (panel.get("meta") or {}).get("built_at")
    dates = panel.get("dates") or []
    out["n_dates"] = len(dates)
    out["date_first"] = dates[0] if dates else None
    out["date_last"] = dates[-1] if dates else None
    out["domain_size"] = len(panel.get("domain") or [])
    # panel 实际列
    if panel.get("_light"):
        # 轻量路径：列名已在 _load_panel_light 里从 npz 取到
        cols = set(panel.get("panel_columns") or [])
        out["coverage_by_dim"] = panel.get("coverage") or {}
        out["cross_section_median"] = panel.get("median_cs")
        out["cross_section_min"] = panel.get("min_cs")
        out["light_audit"] = True
    else:
        cols = set()
        for _dt, rows in (panel.get("rows") or {}).items():
            if isinstance(rows, dict):
                for _c, v in rows.items():
                    if isinstance(v, dict):
                        cols |= set(v.keys())
        out["light_audit"] = False
    out["panel_columns"] = sorted(cols)

    # ── ① 新鲜度 ─────────────────────────────────────────────────────
    scored_dates = _latest_scored_dates(30)
    out["latest_scored_dates"] = scored_dates[:5]
    newest_scored = scored_dates[0] if scored_dates else None
    out["latest_scored_date"] = newest_scored
    stale_days = None
    missing_dates = []
    if newest_scored and out["date_last"]:
        # 严格用日期比较；不臆造交易日历
        from datetime import date as _date

        def _p(s):
            try:
                return _date(int(s[:4]), int(s[5:7]), int(s[8:10]))
            except Exception:
                return None

        a, b = _p(newest_scored), _p(out["date_last"])
        if a and b:
            stale_days = (a - b).days
        # panel 缺哪些「已有评分」的交易日
        _dset = set(dates)
        missing_dates = sorted(d for d in scored_dates if d > out["date_last"] and d not in _dset)
    out["stale_calendar_days"] = stale_days
    out["missing_scored_dates"] = missing_dates
    out["n_missing_scored_dates"] = len(missing_dates)

    # ── ② 权重覆盖 ───────────────────────────────────────────────────
    wver, eff = _eff_weights()
    out["weights_version"] = wver
    covered, blind = [], []
    for k, v in sorted(eff.items(), key=lambda x: -x[1]):
        rec = {"dim": k, "weight": v, "in_panel": k in cols}
        (covered if rec["in_panel"] else blind).append(rec)
    out["weights_total"] = round(sum(eff.values()), 6)
    out["n_effective_dims"] = len(eff)
    out["covered"] = covered
    out["blind"] = blind
    out["blind_weight_sum"] = round(sum(x["weight"] for x in blind), 6)
    out["blind_weight_pct"] = round(100.0 * out["blind_weight_sum"] / (out["weights_total"] or 1), 2)
    out["fully_covered"] = not blind

    # ── ③ 结论文字（供页面直接用）─────────────────────────────────────
    _msgs = []
    if stale_days:
        _msgs.append(f"panel 末日 {out['date_last']} 比最新评分日 {newest_scored} 落后 "
                     f"{stale_days} 个自然日")
    if missing_dates:
        _msgs.append(f"缺 {len(missing_dates)} 个已有评分的交易日")
    if blind:
        _b = "、".join(f"{x['dim']}({x['weight']:.2f})" for x in blind)
        _msgs.append(f"{len(blind)} 个生效权重维度在 panel 中无列（合计权重 "
                     f"{out['blind_weight_sum']:.2f} = {out['blind_weight_pct']:.1f}%）：{_b}")
    out["findings"] = _msgs
    out["conclusion"] = ("；".join(_msgs) if _msgs else "panel 新鲜且权重维度全覆盖")
    out["disclaimer"] = (
        "⚠️ 「panel 无该列」只意味着**该维度未被检验**，不等于「该维度无效」，"
        "更不等于「已通过」。基于 panel 的 IC / 门禁结论必须标注这一盲区。"
    )
    return out


def main():
    out = audit()
    p = os.path.join(HERE, "panel_audit.json")
    with open(p, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print(f"✅ 已写入 {os.path.basename(p)}")
    print(f"   panel: {out.get('panel_path')}  末日 {out.get('date_last')}"
          f"（{out.get('n_dates')} 截面 × {out.get('domain_size')} 只）")
    for m in out.get("findings") or []:
        print(f"   · {m}")
    if not out.get("available"):
        print(f"   ⚠️ {out.get('error')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
